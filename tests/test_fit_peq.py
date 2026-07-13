# -*- coding: utf-8 -*-
"""Tests for tools/fit_peq.py: the PEQ fit reduces the deviation from a
flat target, never boosts past the cap, and writes a v2 profile the app's
"Import profile..." accepts."""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

from perdeviceeq import fit_peq                     # noqa: E402
from perdeviceeq import eq                            # noqa: E402


def _synth():
    """A measured curve: a +8 dB peak at 2 kHz (fixable by a cut), a -4 dB
    dip at 300 Hz (fixable within a +6 cap), on an otherwise flat response."""
    f = np.logspace(np.log10(20), np.log10(20000), 400)
    lf = np.log10(f)
    y = 8 * np.exp(-((lf - np.log10(2000)) ** 2) / (2 * 0.08 ** 2))
    y += -4 * np.exp(-((lf - np.log10(300)) ** 2) / (2 * 0.10 ** 2))
    return f, y


def test_fit_reduces_deviation_and_caps_boost():
    f, y = _synth()
    bands, fg, desired, resid = fit_peq.fit_channel(f, y, 20, 12000, 10, 6.0)
    assert bands, "expected at least one band"
    # every band's boost is capped; cuts are unbounded
    assert all(g <= 6.0 + 1e-6 for _, _, g, _ in bands)
    # the correction brings a fixable curve close to flat
    assert float(np.max(np.abs(resid))) < 1.5
    # the fitted bands, evaluated with the app's own biquad, really do
    # invert the measured deviation (measured + correction ~ flat)
    corr = np.array(eq.response_db(
        0.0, [eq.Band(t, fr, g, q) for t, fr, g, q in bands], list(fg)))
    yg = np.interp(np.log10(fg), np.log10(f), y)
    flattened = yg + corr
    assert flattened.std() < (yg - yg.mean()).std() / 2


def test_deep_null_is_not_boosted_past_cap():
    # a -18 dB notch cannot be filled at +6; the fit must not exceed the
    # cap trying to, and the residual there stays large (honestly reported)
    f = np.logspace(np.log10(20), np.log10(20000), 400)
    y = -18 * np.exp(-((np.log10(f) - np.log10(9000)) ** 2) / (2 * 0.05 ** 2))
    bands, fg, desired, resid = fit_peq.fit_channel(f, y, 20, 12000, 10, 6.0)
    assert all(g <= 6.0 + 1e-6 for _, _, g, _ in bands)
    corr = np.array(eq.response_db(
        0.0, [eq.Band(t, fr, g, q) for t, fr, g, q in bands], list(fg)))
    assert float(np.max(corr)) < 6.5          # never boosted beyond the cap


def _write_result(path, f, y):
    path.write_text(json.dumps({
        "schema": "pde-measurement",
        "data": {"freq_hz": [float(x) for x in f],
                 "mag_db_smoothed": [float(v) for v in y]}}))


def test_cli_writes_importable_v2_profile(tmp_path):
    f, y = _synth()
    res = tmp_path / "r.json"
    _write_result(res, f, y)
    out = tmp_path / "profile.json"
    r = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "fit_peq.py"),
         "--left", str(res), "--right", str(res), "--bands", "8",
         "--name", "Test", "--out", str(out)],
        capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    p = json.loads(out.read_text())
    # exactly the shape gui._import_profile / ProfileStore expect (v2)
    assert p["version"] == 2
    assert p["apply_all"] is False
    assert p["ch_keys"] == ["FL", "FR"]
    assert p["preamp"] == 0.0                  # the app derives Safe/Session
    for key in ("FL", "FR"):
        bands = p["channels"][key]["bands"]
        assert bands and all(
            set(b) >= {"type", "freq", "gain", "q", "enabled"}
            and b["type"] in eq.TYPE_TO_LABEL for b in bands)


def test_fit_profiles_direct_call():
    # the callable core the wizard uses: feed result dicts, get a v2 body
    from perdeviceeq import fit_peq, measure_core as mc
    from perdeviceeq.pde_audit import DEMO_PROFILE, chain_curve
    freqs = mc.log_grid()

    def result_for(ch_key):
        mag = chain_curve(DEMO_PROFILE["channels"][ch_key], 48000, freqs)
        return {"data": {"freq_hz": freqs.tolist(),
                         "mag_db_smoothed": mag.tolist(),
                         "mag_db_raw": mag.tolist()}}

    results = {"FL": result_for("FL"), "FR": result_for("FR")}
    prof = fit_peq.fit_profiles(results, name="Unit", bands=12,
                                f_lo=20.0, f_hi=12000.0)
    assert prof["name"] == "Unit"
    assert prof["version"] == 2
    assert prof["apply_all"] is False
    assert prof["ch_keys"] == ["FL", "FR"]
    assert prof["preamp"] == 0.0
    for key in ("FL", "FR"):
        bnds = prof["channels"][key]["bands"]
        assert bnds and all(b["enabled"] for b in bnds)
        assert all(b["gain"] <= 6.0 + 1e-6 for b in bnds)

    mono = fit_peq.fit_profiles({"all": result_for("FL")}, mono=True)
    assert mono["apply_all"] is True
    assert mono["all"]["bands"]
    assert mono["channels"] == {}


# --- balance trim: equalize the channels' TRUE acoustic levels -------------

def _result_flat(level_db, soft, chan, cal="R_RAW.txt"):
    f = np.logspace(np.log10(20), np.log10(20000), 240)
    return {"data": {"freq_hz": [float(x) for x in f],
                     "mag_db_smoothed": [level_db] * len(f)},
            "levels": {"take_soft_volumes": list(soft),
                       "take_channel_volumes": list(chan)},
            "cal_file": cal}


def test_balance_trims_correct_the_drive_not_the_raw_means():
    """Mirrors a measured pair: FL driven one click louder (+0.64 dB)
    yet truly quieter. The trim must equalize the DRIVE-corrected
    levels; from the raw means it would miss by exactly that click."""
    fr = _result_flat(0.0, [0.064] * 3, [0.064] * 3)
    fl = _result_flat(-0.42, [0.0689] * 3, [0.0689] * 3)
    trims, why = fit_peq.balance_trims({"FL": fl, "FR": fr},
                                       {"FL": -0.42, "FR": 0.0})
    assert why == ""
    assert abs(trims["FL"]) < 1e-9            # the true-quietest channel
    assert abs(trims["FR"] - (-1.061)) < 0.01


def test_balance_trims_validity_gate():
    a = _result_flat(0.0, [0.064], [0.064])
    # distinct cal files: distinct couplers, no shared reference
    b = _result_flat(-2.0, [0.064], [0.064], cal="L_RAW.txt")
    trims, why = fit_peq.balance_trims({"FL": a, "FR": b},
                                       {"FL": 0.0, "FR": -2.0})
    assert trims is None and "cal" in why
    # hardware volume (soft pinned 1.0) at two positions: unknowable
    hw1 = _result_flat(0.0, [1.0], [0.064])
    hw2 = _result_flat(-2.0, [1.0], [0.0689])
    trims, why = fit_peq.balance_trims({"FL": hw1, "FR": hw2},
                                       {"FL": 0.0, "FR": -2.0})
    assert trims is None and "hardware" in why
    # hardware volume but ONE shared position: the unknown law cancels
    hw3 = _result_flat(-2.0, [1.0], [0.064])
    trims, why = fit_peq.balance_trims({"FL": hw1, "FR": hw3},
                                       {"FL": 0.0, "FR": -2.0})
    assert why == ""
    assert abs(trims["FL"] - (-2.0)) < 1e-9
    assert abs(trims["FR"]) < 1e-9
    # a result recorded before per-take gains existed
    old = {"data": a["data"], "levels": {}, "cal_file": "R_RAW.txt"}
    trims, why = fit_peq.balance_trims({"FL": a, "FR": old},
                                       {"FL": 0.0, "FR": 0.0})
    assert trims is None
    # single channel: nothing to balance
    trims, why = fit_peq.balance_trims({"FL": a}, {"FL": 0.0})
    assert trims is None


def test_fit_profiles_prepends_the_trim_band():
    loud = _result_flat(0.0, [0.064] * 3, [0.064] * 3)
    quiet = _result_flat(-2.0, [0.064] * 3, [0.064] * 3)
    prof = fit_peq.fit_profiles({"FL": loud, "FR": quiet})
    fl = prof["channels"]["FL"]["bands"]
    fr = prof["channels"]["FR"]["bands"]
    # flat curves need no shape bands: the loud channel carries exactly
    # one freq-0 trim band, the quiet (reference) channel none at all
    assert len(fr) == 0
    assert len(fl) == 1 and fl[0]["freq"] == 0.0
    assert fl[0]["type"] == "HSC" and fl[0]["enabled"] is True
    assert abs(fl[0]["gain"] - (-2.0)) < 0.01
    # evaluated with the app's own biquad the trim is flat gain
    freqs = np.logspace(np.log10(20), np.log10(20000), 50)
    resp = np.array(eq.response_db(
        0.0, [eq.Band.from_dict(fl[0])], list(freqs)))
    assert float(np.max(np.abs(resp + 2.0))) < 0.05


# --- pruning: cancelling stacks die, working bands survive ------------------

def test_prune_removes_a_cancelling_stack():
    """A pair of opposing bands whose net the others can absorb is a
    local-minimum artifact; pruning must collapse it to the minimal
    set while honoring the residual contract."""
    fg = np.logspace(np.log10(20), np.log10(20000), 400)
    real = ("PK", 1000.0, -6.0, 1.0)
    target = fit_peq._response([real], fg)
    stack = [real,
             ("PK", 3000.0, 5.0, 2.0),
             ("PK", 3000.0, -5.0, 2.0)]
    out = fit_peq._prune(list(stack), fg, target, 20.0, 20000.0, 6.0)
    assert len(out) == 1
    r = np.abs(target - fit_peq._response(out, fg))
    assert float(r.max()) \
        <= fit_peq.RESID_TARGET_DB + fit_peq.PRUNE_EPS_DB


def test_prune_keeps_a_working_band():
    fg = np.logspace(np.log10(20), np.log10(20000), 400)
    real = ("PK", 1000.0, -6.0, 1.0)
    target = fit_peq._response([real], fg)
    out = fit_peq._prune([real], fg, target, 20.0, 20000.0, 6.0)
    assert out == [real]


def test_prune_can_drop_the_last_band_on_a_flat_target():
    fg = np.logspace(np.log10(20), np.log10(20000), 400)
    target = np.zeros_like(fg)
    out = fit_peq._prune([("PK", 500.0, 0.3, 1.0)], fg, target,
                         20.0, 20000.0, 6.0)
    assert out == []


def test_prune_does_not_touch_bands_out_of_the_drops_reach():
    """The locality contract: dropping a band may reshape only the
    bands its response reaches; everything else must come out
    IDENTICAL, not merely equivalent. A globally re-refined survivor
    set was observed to rebuild distant bands into a fresh cancelling
    stack while absorbing unrelated drops."""
    fg = np.logspace(np.log10(20), np.log10(20000), 400)
    real_lo = ("PK", 1000.0, -6.0, 1.0)
    real_hi = ("HSC", 6000.0, 4.0, 1.0)
    target = fit_peq._response([real_lo, real_hi], fg)
    junk = ("PK", 100.0, 0.2, 1.0)
    out = fit_peq._prune([real_lo, junk, real_hi], fg, target,
                         20.0, 20000.0, 6.0)
    assert junk not in out
    assert real_lo in out and real_hi in out


def test_refine_honors_the_anchor_leash():
    """A band may not relocate past its anchor's leash even when the
    desired correction sits elsewhere entirely."""
    fg = np.logspace(np.log10(20), np.log10(20000), 400)
    want = fit_peq._response([("PK", 3500.0, -6.0, 1.0)], fg)
    out = fit_peq._refine([("PK", 1000.0, -6.0, 1.0)], fg, want,
                          20.0, 20000.0, 6.0, span_oct=1.0,
                          anchors=[1000.0])
    assert out[0][1] <= 2000.0 + 1.0


def test_greedy_does_not_grow_a_cancelling_stack():
    """A mid dip under a capped HF plateau used to come out as a
    shelf slid under the dip plus a -18 dB partner carving it back;
    with the anchored leash the bands stay near the features and no
    gain wildly overshoots them."""
    fg = np.logspace(np.log10(20), np.log10(20000), 300)
    shape = fit_peq._response([("PK", 3000.0, -12.0, 1.0),
                               ("HSC", 9000.0, 6.0, 0.7)], fg)
    bands, _, _, resid = fit_peq.fit_channel(fg, -shape, 20.0,
                                             20000.0, 15, 6.0)
    # the residual bound is sanity only (the synthetic plateau ends at
    # the grid edge, where a leashed shelf cannot match the reference
    # exactly); the regression guard is the gain bound -- the stack
    # solution carried -18.7 for a -12 feature
    assert float(np.max(np.abs(resid))) < 2.0
    assert max(abs(g) for _, _, g, _ in bands) <= 14.0
