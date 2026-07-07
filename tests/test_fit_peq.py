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
