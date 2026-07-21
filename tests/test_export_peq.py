# -*- coding: utf-8 -*-
"""Export wizard core: registry, composition, collapse, the native
writers and their null tests (ROADMAP sprint item 1)."""

import json

import pytest

from perdeviceeq import eq
from perdeviceeq import export_peq as ex


def _profile_all():
    return {"apply_all": True, "preamp": -2.5,
            "all": {"bands": [
                {"type": "PK", "freq": 1000.0, "gain": 3.0, "q": 1.0,
                 "enabled": True},
                {"type": "LSC", "freq": 100.0, "gain": -4.0, "q": 0.7,
                 "enabled": True},
                {"type": "PK", "freq": 5000.0, "gain": 2.0, "q": 2.0,
                 "enabled": False}]},
            "channels": {}, "ch_keys": []}


def _profile_channels():
    return {"apply_all": False, "preamp": -1.0,
            "ch_keys": ["FL", "FR"],
            "all": {"bands": []},
            "channels": {
                "FL": {"bands": [
                    {"type": "HSC", "freq": 0.0, "gain": -1.5,
                     "q": 1.0, "enabled": True},
                    {"type": "PK", "freq": 2000.0, "gain": 2.0,
                     "q": 1.0, "enabled": True}]},
                "FR": {"bands": [
                    {"type": "PK", "freq": 2000.0, "gain": 4.0,
                     "q": 1.0, "enabled": True}]}}}


_TASTE = [{"type": "PK", "freq": 3000.0, "gain": -2.0, "q": 1.0,
           "enabled": True}]


# ---- registry ----------------------------------------------------------

def test_builtin_targets_shape():
    ts = ex.load_targets(extra_dir="/nonexistent")
    ids = [t["id"] for t in ts]
    assert "peq-text" in ids and "graphiceq" in ids
    assert "vendor-graphic" in ids
    assert all(t["writer"] in ex.WRITERS for t in ts)
    # every writer is classified for the wizard's first page --
    # an unclassified writer would exist but never show a row
    assert set(ex.WRITERS) == \
        set(ex.FILE_WRITERS) | set(ex.HAND_WRITERS)
    assert not set(ex.FILE_WRITERS) & set(ex.HAND_WRITERS)


def test_targets_dropin_override_and_append(tmp_path):
    over = {"id": "vendor-graphic", "name": "ACME Buds",
            "writer": "fixed",
            "centers": [100.0, 1000.0, 10000.0],
            "gain_range": [-4.0, 4.0], "gain_step": 0.5}
    fresh = {"id": "acme-peq", "name": "ACME peq",
             "writer": "parametric"}
    bad = {"name": "no id", "writer": "parametric"}
    (tmp_path / "10-acme.json").write_text(
        json.dumps([over, fresh, bad]), encoding="utf-8")
    (tmp_path / "junk.json").write_text("{", encoding="utf-8")
    ts = ex.load_targets(extra_dir=str(tmp_path))
    ids = [t["id"] for t in ts]
    assert ids.count("vendor-graphic") == 1
    got = ts[ids.index("vendor-graphic")]
    assert got["name"] == "ACME Buds" and len(got["centers"]) == 3
    assert got["_src"].endswith("10-acme.json")
    assert "acme-peq" in ids
    assert ids.index("vendor-graphic") < ids.index("acme-peq")


# ---- composition -------------------------------------------------------

def test_composed_chains_apply_all_with_taste():
    ch = ex.composed_chains(_profile_all(), _TASTE)
    assert [k for k, _g, _b in ch] == ["all"]
    _k, g, bands = ch[0]
    assert g == -2.5
    assert [b["freq"] for b in bands][-1] == 3000.0
    assert len(bands) == 4


def test_composed_chains_per_channel_shared_preamp():
    ch = ex.composed_chains(_profile_channels(), _TASTE)
    assert [k for k, _g, _b in ch] == ["FL", "FR"]
    for _k, g, bands in ch:
        assert g == -1.0
        assert bands[-1]["freq"] == 3000.0


def test_composed_chains_empty_channels_falls_back_to_all():
    p = {"apply_all": False, "preamp": 0.5, "ch_keys": [],
         "channels": {},
         "all": {"bands": [{"type": "PK", "freq": 500.0,
                            "gain": 1.0, "q": 1.0, "enabled": True}]}}
    ch = ex.composed_chains(p, None)
    assert [k for k, _g, _b in ch] == ["all"]
    assert len(ch[0][2]) == 1


def test_fold_flat_trim_and_disabled():
    _k, g, bands = ex.composed_chains(_profile_channels(), None)[0]
    g2, b2, folded = ex.fold_flat(g, bands)
    assert folded == -1.5
    assert g2 == pytest.approx(-2.5)
    assert all(b["freq"] >= 1.0 for b in b2)
    g3, b3, _ = ex.fold_flat(-2.5, _profile_all()["all"]["bands"])
    assert len(b3) == 2               # the disabled band is dropped


def test_fit_band_from_params_and_default():
    assert ex.fit_band({}) == (20.0, 12000.0)
    p = {"fit": {"params": {"f_lo": 40.0, "f_hi": 10000.0}}}
    assert ex.fit_band(p) == (40.0, 10000.0)


# ---- collapse ----------------------------------------------------------

def test_collapse_choices():
    one = ex.composed_chains(_profile_all(), None)
    two = ex.composed_chains(_profile_channels(), None)
    assert ex.collapse_choices(one, band_domain=True) == ["all"]
    assert ex.collapse_choices(one, band_domain=False) == ["all"]
    # mean leads for every writer: the shared-channels default
    # (field feedback); band-domain realizes it via the export
    # re-fit
    assert ex.collapse_choices(two, band_domain=True) == \
        ["mean", "FL", "FR"]
    assert ex.collapse_choices(two, band_domain=False) == \
        ["mean", "FL", "FR"]


def test_merged_parametric_and_max_bands_validator(tmp_path):
    t = {x["id"]: x for x in ex.load_targets(
        extra_dir="/nonexistent")}["peq-text"]
    assert t["writer"] == "parametric"
    assert "max_bands" not in t      # the budget is a page dial
    bad1 = {"id": "b1", "name": "b1", "writer": "parametric",
            "max_bands": 0}
    bad2 = {"id": "b2", "name": "b2", "writer": "parametric",
            "max_bands": "5"}
    bad3 = {"id": "b3", "name": "b3", "writer": "parametric",
            "max_bands": True}
    good = {"id": "g1", "name": "g1", "writer": "parametric",
            "max_bands": 5}
    import json as _json
    (tmp_path / "mb.json").write_text(
        _json.dumps([bad1, bad2, bad3, good]), encoding="utf-8")
    ids = [t["id"] for t in
           ex.load_targets(extra_dir=str(tmp_path))]
    assert "g1" in ids
    assert not any(i in ids for i in ("b1", "b2", "b3"))


def test_center_curve():
    c, off = ex.center_curve([1.0, 3.0, 5.0])
    assert off == 3.0 and c == [-2.0, 0.0, 2.0]
    assert abs(sum(c)) < 1e-12


def test_collapse_pick_and_mean():
    chains = ex.composed_chains(_profile_channels(), None)
    freqs = ex.log_grid(20.0, 12000.0, 64)
    l, note_l = ex.collapse(chains, "FL", freqs)
    r, _ = ex.collapse(chains, "FR", freqs)
    m, note_m = ex.collapse(chains, "mean", freqs)
    for a, b, c in zip(l, r, m):
        assert c == pytest.approx((a + b) / 2.0)
    assert "FL" in note_l and "mean" in note_m
    g, bands, note = ex.pick_chain(chains, "FR")
    assert g == -1.0 and "FR" in note
    with pytest.raises(KeyError):
        ex.collapse(chains, "XX", freqs)


# ---- native writers + null tests ---------------------------------------

def test_parametric_roundtrip_nulls():
    chains = ex.composed_chains(_profile_all(), _TASTE)
    _k, g0, b0 = chains[0]
    g, b, _ = ex.fold_flat(g0, b0)
    text = ex.parametric_text(g, b, header=(
        "per-device-eq export", "Collapse: single chain"))
    assert text.startswith("# per-device-eq export\n")
    pre, bands = eq.parse_autoeq(text)
    assert pre == pytest.approx(-2.5)
    assert len(bands) == 3            # disabled one never exported
    freqs = ex.log_grid(20.0, 12000.0, 480)
    ref = ex.chain_response(g0, b0, freqs)
    assert ex.null_test_parametric(text, freqs, ref) <= 0.03


def test_parametric_folds_trim_into_preamp():
    chains = ex.composed_chains(_profile_channels(), None)
    g0, b0, _note = ex.pick_chain(chains, "FL")
    g, b, folded = ex.fold_flat(g0, b0)
    assert folded == -1.5
    text = ex.parametric_text(g, b)
    assert "Fc 0" not in text
    freqs = ex.log_grid(20.0, 12000.0, 480)
    ref = ex.chain_response(g0, b0, freqs)
    assert ex.null_test_parametric(text, freqs, ref) <= 0.03


# extracted from a published AutoEq GraphicEQ.txt, independently
# of the constant in export_peq -- the file format Wavelet accepts
# is this list and nothing else
_AUTOEQ_FREQS = [
    20, 21, 22, 23, 24, 26, 27, 29, 30, 32, 34, 36,
    38, 40, 43, 45, 48, 50, 53, 56, 59, 63, 66, 70,
    74, 78, 83, 87, 92, 97, 103, 109, 115, 121, 128, 136,
    143, 151, 160, 169, 178, 188, 199, 210, 222, 235, 248, 262,
    277, 292, 309, 326, 345, 364, 385, 406, 429, 453, 479, 506,
    534, 565, 596, 630, 665, 703, 743, 784, 829, 875, 924, 977,
    1032, 1090, 1151, 1216, 1284, 1357, 1433, 1514, 1599, 1689, 1784, 1885,
    1991, 2103, 2221, 2347, 2479, 2618, 2766, 2921, 3086, 3260, 3443, 3637,
    3842, 4058, 4287, 4528, 4783, 5052, 5337, 5637, 5955, 6290, 6644, 7018,
    7414, 7831, 8272, 8738, 9230, 9749, 10298, 10878, 11490, 12137,
    12821, 13543, 14305, 15110, 15961, 16860, 17809, 18812, 19871]


def test_graphic_grid_is_the_autoeq_contract():
    assert ex.graphic_grid() == [float(f) for f in _AUTOEQ_FREQS]


def test_registry_graphiceq_is_one_bare_row():
    t = {x["id"]: x for x in ex.load_targets()}
    assert t["graphiceq"]["writer"] == "graphiceq"
    assert t["graphiceq"].get("bare") is True
    ids = [x["id"] for x in ex.load_targets()]
    assert "wavelet" not in ids and "jamesdsp" not in ids
    assert "qudelix" not in ids      # merged into peq-text


def test_graphiceq_bare_is_the_naked_line():
    # Wavelet refused a file with a leading "# Level shifted" note:
    # bare must silence every line we would otherwise add, and the
    # byte shape must match the published AutoEq artifact -- one
    # line, no trailing newline.
    resp = [4.2] * 127                     # forces a level shift
    text, shift = ex.graphiceq_text(ex.graphic_grid(), resp,
                                    header=["ignored"], bare=True)
    assert shift == -4.2
    assert text.startswith("GraphicEQ: 20 ")
    assert "\n" not in text and "#" not in text
    fs, gs = ex.parse_graphiceq(text)
    assert len(fs) == 127 and abs(max(gs)) < 0.05


def test_headers_are_apo_comments():
    text = ex.parametric_text(-1.0, [
        {"type": "PK", "freq": 100, "gain": 2.0, "q": 1.0}],
        header=["one", "two"])
    head = text.splitlines()[:2]
    assert head == ["# one", "# two"]
    gtext, _s = ex.graphiceq_text([20.0, 19871.0], [0.0, 0.0],
                                  header=["note"])
    assert gtext.splitlines()[0] == "# note"
    assert eq.parse_autoeq(text)[0] == -1.0


def test_graphiceq_line_and_null():
    chains = ex.composed_chains(_profile_all(), None)
    gf = ex.graphic_grid()
    assert len(gf) == len(set(gf))
    assert gf[0] == 20.0 and gf[-1] == 19871.0
    resp, _note = ex.collapse(chains, "all", gf)
    text, shift = ex.graphiceq_text(gf, resp)
    assert shift < 0.0                # this chain peaks at +0.5 dB
    fs, gs = ex.parse_graphiceq(text)
    assert len(fs) == len(gf)
    freqs = ex.log_grid(20.0, 12000.0, 480)
    ref, _ = ex.collapse(chains, "all", freqs)
    assert ex.null_test_graphic(text, freqs, ref, shift) <= 0.1


def test_graphiceq_shifts_positive_chains():
    p = {"apply_all": True, "preamp": 0.0,
         "all": {"bands": [{"type": "PK", "freq": 1000.0,
                            "gain": 3.0, "q": 1.0,
                            "enabled": True}]},
         "channels": {}, "ch_keys": []}
    chains = ex.composed_chains(p, None)
    gf = ex.graphic_grid()
    resp, _ = ex.collapse(chains, "all", gf)
    text, shift = ex.graphiceq_text(gf, resp)
    assert shift == pytest.approx(-3.0, abs=0.05)
    assert "Level shifted" in text
    _fs, gs = ex.parse_graphiceq(text)
    assert max(gs) <= 0.0
    freqs = ex.log_grid(20.0, 12000.0, 480)
    ref, _ = ex.collapse(chains, "all", freqs)
    assert ex.null_test_graphic(text, freqs, ref, shift) <= 0.1


def test_graphiceq_no_shift_for_cut_only_chains():
    p = {"apply_all": True, "preamp": 0.0,
         "all": {"bands": [{"type": "PK", "freq": 1000.0,
                            "gain": -3.0, "q": 1.0,
                            "enabled": True}]},
         "channels": {}, "ch_keys": []}
    gf = ex.graphic_grid()
    resp, _ = ex.collapse(ex.composed_chains(p, None), "all", gf)
    _text, shift = ex.graphiceq_text(gf, resp)
    assert shift == 0.0


def test_null_test_catches_a_lie():
    chains = ex.composed_chains(_profile_all(), None)
    _k, g0, b0 = chains[0]
    g, b, _ = ex.fold_flat(g0, b0)
    text = ex.parametric_text(g + 1.0, b)      # wrong on purpose
    freqs = ex.log_grid(20.0, 12000.0, 64)
    ref = ex.chain_response(g0, b0, freqs)
    assert ex.null_test_parametric(text, freqs, ref) > ex.NULL_PASS_DB


# ---- the fixed-band fit (writer class b) --------------------------------

_T8 = {"id": "t8", "name": "T8", "writer": "fixed",
       "centers": [100.0 * 2 ** i for i in range(8)],
       "gain_range": [-12.0, 12.0], "gain_step": 0.1,
       "basis_q": 1.4}


def test_solve_fixed_recovers_a_basis_combination():
    freqs = ex.log_grid(50.0, 16000.0, 240)
    b = ex.peaking_basis(_T8["centers"], 1.4, freqs)
    truth = 2.0 * b[:, 2] - 3.0 * b[:, 5] + 1.5
    sol = ex.solve_fixed(_T8, freqs, [float(v) for v in truth])
    want = [0.0, 0.0, 2.0, 0.0, 0.0, -3.0, 0.0, 0.0]
    for got, exp in zip(sol["gains"], want):
        assert got == pytest.approx(exp, abs=0.1)
    assert sol["offset"] == pytest.approx(1.5, abs=0.05)
    assert sol["resid_max"] < 0.1
    assert "peaking" in sol["basis"]


def test_solve_fixed_respects_bounds_and_reports_residual():
    t = dict(_T8, gain_range=[-6.0, 6.0], gain_step=1.0)
    freqs = ex.log_grid(50.0, 16000.0, 240)
    b = ex.peaking_basis(t["centers"], 1.4, freqs)
    sol = ex.solve_fixed(t, freqs, [float(v) for v in 10.0 * b[:, 0]])
    assert sol["gains"][0] == 6.0             # clamped at the cap
    assert sol["resid_max"] > 1.0             # and honest about it
    assert len(sol["resid"]) == len(freqs)


def test_fixed_sheet_carries_fit_provenance_and_residual():
    freqs = ex.log_grid(50.0, 16000.0, 120)
    b = ex.peaking_basis(_T8["centers"], 1.4, freqs)
    sol = ex.solve_fixed(_T8, freqs, [float(v) for v in 1.0 * b[:, 3]])
    text = ex.fixed_sheet_text(_T8, sol, header=("Target: T8",))
    assert "Residual: max" in text
    assert "Level trim absorbed" in text
    assert "least squares" in text
    assert text.count(" Hz   ") == 8
    assert "800 Hz   +1.0" in text


def test_load_basis_roundtrip_and_mismatch(tmp_path, capsys):
    freqs = ex.log_grid(50.0, 16000.0, 96)
    unit = ex.peaking_basis(_T8["centers"], 1.4, freqs)
    basis = {"freq": freqs, "curve_gain_db": 6.0,
             "curves": [[6.0 * unit[i][j] for i in range(len(freqs))]
                        for j in range(8)]}
    bp = tmp_path / "t8.basis.json"
    bp.write_text(json.dumps(basis), encoding="utf-8")
    t = dict(_T8, basis_file=str(bp))
    got = ex.load_basis(t, freqs)
    assert got is not None
    for j in (0, 4, 7):
        for i in (0, 40, 95):
            assert got[i][j] == pytest.approx(unit[i][j], abs=1e-6)
    sol = ex.solve_fixed(t, freqs,
                         [float(2.0 * v) for v in unit[:, 4]])
    assert "measured basis" in sol["basis"]
    assert sol["gains"][4] == pytest.approx(2.0, abs=0.1)
    bad = dict(_T8, basis_file=str(bp),
               centers=[100.0, 1000.0])
    assert ex.load_basis(bad, freqs) is None
    assert "does not match" in capsys.readouterr().err


# ---- Poweramp Equalizer ------------------------------------------------


def test_poweramp_enum_table_is_the_calibration():
    # two 2026-07-18 calibration exports: the type-cycled preset
    # pinned the set, a hand-entered correction (nine Peaking bands
    # landing as type 3) disambiguated Peaking from Band Pass
    assert ex.PA_TYPE == {"PK": 3, "LSC": 4, "HSC": 5}
    assert (ex.PA_BOTH, ex.PA_LEFT, ex.PA_RIGHT) == (0, 1, 2)
    t = {x["id"]: x for x in ex.load_targets()}["poweramp"]
    assert t["writer"] == "poweramp" and t["ext"] == ".json"
    assert t["freq_range"] == [20.0, 24000.0]


def test_poweramp_stereo_roundtrip_with_trim_and_taste():
    chains = ex.composed_chains(_profile_channels(), _TASTE)
    t = {x["id"]: x for x in ex.load_targets()}["poweramp"]
    text = ex.poweramp_json(t, chains, "soundcore Liberty 5")
    import json as _json
    preset = _json.loads(text)[0]
    assert preset["name"] == "soundcore Liberty 5"
    assert preset["parametric"] is True
    assert preset["preamp"] == -1.0
    chans = {b["channels"] for b in preset["bands"]}
    assert chans == {ex.PA_BOTH, ex.PA_LEFT, ex.PA_RIGHT}
    # the app's inert pair leads, exactly as native exports have
    # it -- the importer eats the first two bands without it
    assert preset["bands"][0] == dict(ex.PA_INERT_PAIR[0])
    assert preset["bands"][1] == dict(ex.PA_INERT_PAIR[1])
    assert all(b["gain"] == 0.0 for b in preset["bands"][:2])
    # the FL balance trim became a Low Shelf at the Nyquist ceiling
    trims = [b for b in preset["bands"]
             if b["type"] == ex.PA_TYPE["LSC"]
             and b["frequency"] == 24000.0]
    assert len(trims) == 1 and trims[0]["channels"] == ex.PA_LEFT
    assert trims[0]["gain"] == -1.5
    # byte shape of the app's own exports: tab indent, integer
    # frequencies, no trailing newline
    assert "\t" in text and text.endswith("]")
    import re as _re
    assert not _re.search(r'"frequency": \d+\.', text)
    freqs = ex.log_grid(20.0, 12000.0, 480)
    errs = ex.null_test_poweramp(text, chains, freqs)
    assert set(errs) == {"FL", "FR"}
    assert max(errs.values()) <= ex.NULL_PASS_DB, errs


def test_poweramp_single_chain_routes_both():
    chains = ex.composed_chains(_profile_all(), None)
    t = {x["id"]: x for x in ex.load_targets()}["poweramp"]
    text = ex.poweramp_json(t, chains, "mono")
    import json as _json
    bands = _json.loads(text)[0]["bands"]
    assert bands and all(b["channels"] == ex.PA_BOTH for b in bands)
    freqs = ex.log_grid(20.0, 12000.0, 480)
    errs = ex.null_test_poweramp(text, chains, freqs)
    assert max(errs.values()) <= ex.NULL_PASS_DB


def test_poweramp_clamps_and_refusals():
    t = dict({x["id"]: x for x in ex.load_targets()}["poweramp"])
    band = {"type": "PK", "freq": 10.0, "gain": 22.0, "q": 0.02,
            "enabled": True}
    out = ex._pa_band(band, t, ex.PA_BOTH)
    assert out["frequency"] == 20.0 and out["gain"] == 15.0
    assert out["q"] == 0.1
    with pytest.raises(ValueError):
        ex.poweramp_json(t, [("FC", 0.0, [])], "x")
    assert not ex.poweramp_stereo_keys([("FC", 0.0, [])])


def test_headroom_preamp_manual_and_auto():
    boost = [{"type": "LSC", "freq": 50.0, "gain": 12.0, "q": 1.0,
              "enabled": True}]
    calm = [{"type": "PK", "freq": 1000.0, "gain": 3.0, "q": 1.0,
             "enabled": True}]
    # manual: the stored value is intent -- respected, and only
    # ever lowered when the composition peaks past it
    adj, moved = ex.headroom_preamp(-6.4, [boost])
    assert adj < -11.0 and moved < -4.0
    assert abs((-6.4 + moved) - adj) < 1e-9
    keep, zero = ex.headroom_preamp(-6.4, [calm])
    assert keep == -6.4 and zero == 0.0
    # auto: the composed Safe of the EXPORTED chains, moving both
    # directions -- dropping a hot taste layer gives loudness back
    # -- with the desktop's own 0.1 dB ceil so numbers match home
    up, dm = ex.headroom_preamp(-16.3, [calm], auto=True)
    assert up == -3.0 and abs(dm - 13.3) < 1e-9
    down, _m = ex.headroom_preamp(-1.0, [boost], auto=True)
    assert down < -11.0


def test_poweramp_preamp_covers_taste_headroom():
    taste = [{"type": "LSC", "freq": 50.0, "gain": 12.0, "q": 1.0,
              "enabled": True}]
    chains = ex.composed_chains(_profile_channels(), taste)
    t = {x["id"]: x for x in ex.load_targets()}["poweramp"]
    adj, moved = ex.headroom_preamp(chains[0][1],
                                    [b for _k, _g, b in chains])
    assert moved < 0.0
    chains2 = [(k, adj, b) for k, _g, b in chains]
    text = ex.poweramp_json(t, chains2, "hp")
    import json as _json
    clamped, spill = ex.preamp_spill(adj, t)
    assert clamped + spill == adj and clamped >= -12.0
    assert _json.loads(text)[0]["preamp"] == clamped
    freqs = ex.log_grid(20.0, 12000.0, 480)
    errs = ex.null_test_poweramp(text, chains2, freqs)
    assert max(errs.values()) <= ex.NULL_PASS_DB


# ---- the measurement source --------------------------------------


def _canvas_profile(fl_tilt=6.0, fr_tilt=6.0, fl_lift=3.0):
    """A synthetic v3 profile: two channels, one take each, on the
    canvas grid; FL sits fl_lift dB above FR at equal recorded
    drives, so the balance trim gate opens and pulls FL down."""
    from perdeviceeq import measure_core as mc
    from perdeviceeq.measure_build import fit_fingerprint
    import math as m
    grid = {"ppo": 12, "f_lo": 20.0, "f_hi": 20000.0}
    freqs = mc.log_grid(grid["f_lo"], grid["f_hi"], grid["ppo"])
    lo, hi = m.log10(freqs[0]), m.log10(freqs[-1])

    def curve(tilt, lift):
        return [lift + tilt * (m.log10(f) - lo) / (hi - lo)
                for f in freqs]

    def take(tid, ch, mags):
        return {"id": tid, "channel": ch, "mag_db_uncal": mags,
                "soft_vol": 1.0, "chan_vol": 1.0,
                "capture_channel": 0, "snr_db": 40.0,
                "delay_ms": 1.0}

    meas = {"grid": grid, "source": {"cal": {}},
            "takes": [take("t1", "FL", curve(fl_tilt, fl_lift)),
                      take("t2", "FR", curve(fr_tilt, 0.0))]}
    params = {"bands": 10, "f_lo": 20.0, "f_hi": 12000.0,
              "max_boost": 6.0, "smoothing": 6, "mono": False}
    fit = {"at": "2026-07-19T00:00:00+00:00", "algo": "greedy",
           "params": params, "target": {"kind": "flat"},
           "takes": ["t1", "t2"], "edited": False,
           "inputs_sha256": fit_fingerprint(meas, ["t1", "t2"],
                                            params)}
    return {"name": "synth", "preamp": 0.0, "apply_all": False,
            "ch_keys": ["FL", "FR"], "all": {"bands": []},
            "channels": {"FL": {"bands": []},
                         "FR": {"bands": []}},
            "measurement": meas, "fit": fit}


def test_export_source_gate():
    p = _canvas_profile()
    assert ex.export_source(p) == ("measurement", "")
    edited = dict(p, fit=dict(p["fit"], edited=True))
    mode, why = ex.export_source(edited)
    assert mode == "chain" and "edited" in why
    stale = dict(p, fit=dict(p["fit"], params=dict(
        p["fit"]["params"], bands=4)))
    mode, why = ex.export_source(stale)
    assert mode == "chain" and "changed" in why
    bare_p = {k: v for k, v in p.items() if k != "measurement"}
    mode, why = ex.export_source(bare_p)
    assert mode == "chain" and "provenance" in why


def test_desired_from_canvas_trims_and_taste():
    p = _canvas_profile(fl_lift=3.0)
    fg, curves, note = ex.desired_from_canvas(p)
    assert set(curves) == {"FL", "FR"}
    assert 19.9 < fg[0] < 25.0 and 11000.0 < fg[-1] < 12000.1
    # each desired is mean-flat before the trim; FL carries the
    # -3 dB trim as a flat offset, FR stays at zero mean
    n = len(fg)
    m_fl = sum(curves["FL"]) / n
    m_fr = sum(curves["FR"]) / n
    assert "trims included" in note
    assert abs(m_fr) < 0.05 and abs(m_fl + 3.0) < 0.1
    taste = [{"type": "PK", "freq": 1000.0, "gain": 4.0, "q": 1.0,
              "enabled": True}]
    _fg2, tcurves, _n2 = ex.desired_from_canvas(p, taste)
    tail = ex.chain_response(0.0, taste, fg)
    k = max(range(n), key=lambda i: tail[i])
    assert abs((tcurves["FR"][k] - curves["FR"][k]) - tail[k]) \
        < 1e-6


def test_refit_bands_budget_and_residual():
    fg = ex.log_grid(20.0, 12000.0, 240)
    shape = [{"type": "PK", "freq": 200.0, "gain": 4.0, "q": 1.4,
              "enabled": True},
             {"type": "PK", "freq": 3000.0, "gain": -5.0, "q": 2.0,
              "enabled": True}]
    desired = ex.chain_response(0.0, shape, fg)
    bands, rmax, rrms = ex.refit_bands(fg, desired, 20.0, 12000.0,
                                       6, 6.0)
    assert len(bands) <= 6 and rmax < 0.75 and rrms <= rmax
    one, rmax1, _r = ex.refit_bands(fg, desired, 20.0, 12000.0,
                                    1, 6.0)
    assert len(one) <= 1 and rmax1 > rmax


def test_sample_curve_edge_hold_and_mean():
    fg = [100.0, 1000.0, 10000.0]
    c = [1.0, 3.0, 5.0]
    out = ex.sample_curve(fg, c, [20.0, 1000.0, 19871.0])
    assert out[0] == 1.0 and out[1] == 3.0 and out[2] == 5.0
    assert ex.mean_curve({"a": [0.0, 2.0], "b": [2.0, 4.0]}) == \
        [1.0, 3.0]


# ---- target limits -------------------------------------------------


def test_target_limits_validator(tmp_path):
    import json as _json
    bads = [{"id": "x1", "name": "x", "writer": "sheet",
             "gain_range": [5]},
            {"id": "x2", "name": "x", "writer": "sheet",
             "gain_range": [3, 3]},
            {"id": "x3", "name": "x", "writer": "sheet",
             "q_range": ["a", 2]},
            {"id": "x4", "name": "x", "writer": "sheet",
             "freq_range": [True, 100]},
            {"id": "x5", "name": "x", "writer": "sheet",
             "types": ["XX"]},
            {"id": "x6", "name": "x", "writer": "sheet",
             "types": []}]
    good = {"id": "ok1", "name": "ok", "writer": "parametric",
            "gain_range": [-12, 12], "q_range": [0.5, 6],
            "freq_range": [20, 16000], "types": ["PK"]}
    (tmp_path / "lim.json").write_text(
        _json.dumps(bads + [good]), encoding="utf-8")
    ids = [t["id"] for t in
           ex.load_targets(extra_dir=str(tmp_path))]
    assert "ok1" in ids
    assert not any("x%d" % i in ids for i in range(1, 7))


def test_limits_text_and_fit_limits():
    t = {"max_bands": 10, "gain_range": [-12.0, 12.0],
         "q_range": [0.5, 6.0], "freq_range": [20.0, 16000.0],
         "types": ["PK", "LSC"]}
    s = ex.limits_text(t)
    assert "10 bands" in s and "gain -12..12 dB" in s
    assert "Q 0.5..6" in s and "20-16000 Hz" in s
    assert "types PK/LSC" in s
    assert ex.limits_text({}) == ""
    lim = ex.fit_limits(t)
    assert lim["gain"] == (-12.0, 12.0)
    assert lim["q"] == (0.5, 6.0)
    assert lim["types"] == ("PK", "LSC")
    assert "freq" not in lim and ex.fit_limits({}) == {}


def test_chain_violations():
    t = {"max_bands": 2, "gain_range": [-12, 6],
         "q_range": [0.3, 8], "types": ["PK"]}
    bands = [{"type": "PK", "freq": 100.0, "gain": -20.0,
              "q": 1.0, "enabled": True},
             {"type": "LSC", "freq": 50.0, "gain": 2.0,
              "q": 1.0, "enabled": True},
             {"type": "PK", "freq": 1000.0, "gain": 3.0,
              "q": 1.0, "enabled": True},
             {"type": "PK", "freq": 2000.0, "gain": 3.0,
              "q": 9.0, "enabled": False}]
    v = " | ".join(ex.chain_violations(t, bands))
    assert "3 chain bands over the target's 2" in v
    assert "band 1 outside the target's gain -12..6 dB" in v
    assert "band 2 of a type the target lacks (PK only)" in v
    assert "Q" not in v            # the Q 9 band is disabled
    assert ex.chain_violations({}, bands) == []
    clean = [{"type": "PK", "freq": 100.0, "gain": -3.0,
              "q": 1.0, "enabled": True}]
    assert ex.chain_violations(t, clean) == []


def test_fit_to_desired_honors_limits():
    fg = ex.log_grid(20.0, 12000.0, 240)
    deep = [{"type": "PK", "freq": 300.0, "gain": -18.0, "q": 1.0,
             "enabled": True},
            {"type": "LSC", "freq": 60.0, "gain": 4.0, "q": 0.8,
             "enabled": True}]
    desired = ex.chain_response(0.0, deep, fg)
    lim = {"gain": (-8.0, 8.0), "q": (0.5, 2.0), "types": ("PK",)}
    bands, rmax, rrms = ex.refit_bands(fg, desired, 20.0, 12000.0,
                                       6, 6.0, limits=lim)
    assert bands
    for b in bands:
        assert -8.0 - 1e-6 <= b["gain"] <= 6.0 + 1e-6
        assert 0.5 - 1e-6 <= b["q"] <= 2.0 + 1e-6
        assert b["type"] == "PK"
    free, fmax, _r = ex.refit_bands(fg, desired, 20.0, 12000.0,
                                    6, 6.0)
    assert fmax <= rmax + 1e-9


def test_parallel_mean_average_and_gates():
    freqs = ex.log_grid(20.0, 12000.0, 240)

    def ch(g1, g2, f2=2000.0, q2=1.6, trim=0.0, junk=False):
        bands = [{"type": "LSC", "freq": 80.0, "gain": g1,
                  "q": 0.9, "enabled": True},
                 {"type": "PK", "freq": f2, "gain": g2,
                  "q": q2, "enabled": True}]
        if trim:
            bands.insert(0, {"type": "HSC", "freq": 0.0,
                             "gain": trim, "q": 1.0,
                             "enabled": True})
        if junk:
            bands.append({"type": "PK", "freq": 5000.0,
                          "gain": 3.0, "q": 2.0,
                          "enabled": False})
        return bands

    chains = [("FL", -3.0, ch(2.0, -4.0, trim=-0.5, junk=True)),
              ("FR", -3.0, ch(3.0, -5.0, f2=2100.0))]
    pm, why = ex.parallel_mean(chains, freqs)
    assert pm and why == ""
    g_m, bands_m, err = pm
    # trims fold and average exactly; disabled bands never count
    assert abs(g_m - (-3.25)) < 1e-9
    assert len(bands_m) == 2 and bands_m[0]["type"] == "LSC"
    assert abs(bands_m[1]["gain"] + 4.5) < 1e-9
    assert err <= ex.NULL_PASS_DB
    got = ex.chain_response(g_m, bands_m, freqs)
    cols = [ex.chain_response(g, b, freqs)
            for _k, g, b in chains]
    true = [(a + b) / 2.0 for a, b in zip(*cols)]
    assert abs(max(abs(x - y) for x, y in zip(got, true))
               - err) < 1e-9

    # gate: counts
    pm2, why2 = ex.parallel_mean(
        [("FL", 0.0, ch(1.0, 1.0)),
         ("FR", 0.0, ch(1.0, 1.0)[:1])], freqs)
    assert pm2 is None and "band counts differ" in why2
    # gate: types
    other = ch(1.0, 1.0)
    other[0]["type"] = "PK"
    pm3, why3 = ex.parallel_mean(
        [("FL", 0.0, ch(1.0, 1.0)), ("FR", 0.0, other)], freqs)
    assert pm3 is None and "types are not parallel" in why3
    # gate: the verification against the true mean
    pm4, why4 = ex.parallel_mean(
        [("FL", 0.0, ch(2.0, -4.0)),
         ("FR", 0.0, ch(2.0, -4.0, f2=4000.0))], freqs)
    assert pm4 is None and "misses the true mean" in why4


def test_audit_target_scores_and_reasons():
    freqs = ex.log_grid(20.0, 12000.0, 240)

    def ch(g2, f2=2000.0, n_extra=0):
        bands = [{"type": "LSC", "freq": 80.0, "gain": 2.0,
                  "q": 0.9, "enabled": True},
                 {"type": "PK", "freq": f2, "gain": g2,
                  "q": 1.6, "enabled": True}]
        for i in range(n_extra):
            bands.append({"type": "PK", "freq": 300.0 + 200 * i,
                          "gain": 1.0, "q": 2.0, "enabled": True})
        return bands

    par = [("FL", -3.0, ch(-4.0)), ("FR", -3.0, ch(-5.0))]
    div = [("FL", -3.0, ch(-4.0)), ("FR", -3.0, ch(-4.0,
                                                   f2=4000.0))]
    pa = {"writer": "poweramp"}
    ge = {"writer": "graphiceq"}
    fx = {"writer": "fixed"}
    pt = {"writer": "parametric"}
    tight = {"writer": "parametric", "gain_range": [-3.0, 3.0]}
    budget = {"writer": "parametric", "max_bands": 1}

    assert ex.audit_target(pa, par, freqs) == (0, "", [])
    s, f, r = ex.audit_target(ge, par, freqs)
    assert s == 2 and f == "" and "mean of channels" in r
    assert r[0].startswith("response projection, 127-point grid")
    assert "dB here" in r[0]
    e = float(r[0].split("--")[1].split("dB")[0])
    assert 0.0 <= e < ex.NULL_PASS_DB
    s, f, r = ex.audit_target(ge, par[:1], freqs)
    assert s == 2 and len(r) == 1 and "dB here" in r[0]
    fx8 = dict(fx, centers=[1.0] * 8, gain_range=[-6.0, 6.0])
    s, f, r = ex.audit_target(fx8, par, freqs)
    assert s == 4 and f == "fit"
    assert r == ["8 fixed sliders", "gain -6..6 dB",
                 "mean of channels"]
    s, f, r = ex.audit_target(pt, par, freqs)
    assert (s, f) == (1, "")
    assert r == ["mean of channels -- band average"]
    assert ex.audit_target(pt, par[:1], freqs) == (0, "", [])
    s, f, r = ex.audit_target(tight, par, freqs)
    assert (s, f, r) == (3, "re-fit", ["gain limit"])
    s, f, r = ex.audit_target(budget, par[:1], freqs)
    assert (s, f, r) == (3, "re-fit", ["band budget 1"])
    s, f, r = ex.audit_target(pt, div, freqs)
    assert (s, f, r) == (3, "re-fit", ["no per-channel EQ"])
    s, f, r = ex.audit_target(budget, div, freqs)
    assert (s, f) == (3, "re-fit")
    assert r == ["no per-channel EQ", "band budget 1"]


def test_refit_progress_is_alive_and_bounded():
    fg = ex.log_grid(20.0, 12000.0, 240)
    shape = [{"type": "PK", "freq": 150.0, "gain": 4.0, "q": 1.2,
              "enabled": True},
             {"type": "PK", "freq": 900.0, "gain": -5.0, "q": 2.0,
              "enabled": True},
             {"type": "PK", "freq": 5200.0, "gain": 3.0, "q": 1.4,
              "enabled": True}]
    desired = ex.chain_response(0.0, shape, fg)
    seen = []
    bands, _rm, _rr = ex.refit_bands(
        fg, desired, 20.0, 12000.0, 6, 6.0,
        progress=lambda *a: seen.append(a))
    assert bands and seen
    fr = [s[0] for s in seen]
    assert fr[-1] == 1.0 and fr.count(1.0) == 1
    assert all(0.0 <= v <= 1.0 for v in fr)
    assert fr == sorted(fr)              # never walks backwards
    assert len(fr) >= 5                  # alive, not a two-stepper
    ev = [s[3] for s in seen]
    assert ev == sorted(ev) and ev[-1] >= ev[0] + 80
    assert all(s[2] == 6 and 0 <= s[1] <= 6 for s in seen)


def test_shelf_q_ceiling_dissolves_edge_stacks():
    from perdeviceeq.fit_peq import SHELF_Q_MAX
    fg = ex.log_grid(20.0, 12000.0, 240)
    base = [{"type": "LSC", "freq": 35.0, "gain": 4.5, "q": 0.4,
             "enabled": True},
            {"type": "PK", "freq": 3000.0, "gain": -12.0,
             "q": 0.7, "enabled": True}]
    desired = list(ex.chain_response(0.0, base, fg))
    desired[0] += 1.5           # the edge kink that bred a stack
    desired[1] += 0.8           # of four resonant +/-24 shelves
    bands, _rm, _rr = ex.refit_bands(fg, desired, 20.0, 12000.0,
                                     10, 6.0)
    assert bands
    for b in bands:
        if b["type"] in ("LSC", "HSC"):
            assert b["q"] <= SHELF_Q_MAX + 1e-6
            assert b["gain"] > -12.0    # no cancelling-stack limbs
    assert len([b for b in bands if b["freq"] < 150.0]) <= 3


def test_chain_fit_residual_is_level_free_and_capped():
    fg = ex.log_grid(20.0, 12000.0, 240)
    bands = [{"type": "LSC", "freq": 60.0, "gain": 4.0, "q": 0.5,
              "enabled": True},
             {"type": "PK", "freq": 2500.0, "gain": -8.0,
              "q": 0.8, "enabled": True}]
    desired = list(ex.chain_response(0.0, bands, fg))
    assert ex.chain_fit_residual(fg, desired, bands) < 1e-9
    shifted = [v + 3.0 for v in desired]      # level rides in trims
    assert ex.chain_fit_residual(fg, shifted, bands) < 1e-9
    wrong = [dict(bands[0], gain=1.0), bands[1]]
    assert ex.chain_fit_residual(fg, desired, wrong) > 1.0
    spiked = list(desired)
    for i, f in enumerate(fg):               # an unfillable ask
        if f > 9000.0:
            spiked[i] += 9.0
    big = ex.chain_fit_residual(fg, spiked, bands)
    capped = ex.chain_fit_residual(fg, spiked, bands, cap=6.0)
    assert capped < big


def test_cap_soft_smooths_the_corner_and_ships_clean():
    cap = 6.0
    import math as m
    fg = ex.log_grid(20.0, 18000.0, 480)
    lo, hi = m.log10(fg[0]), m.log10(fg[-1])
    raw = [-2.0 + 14.0 * (m.log10(f) - lo) / (hi - lo)
           for f in fg]                     # crosses the cap
    soft = ex.cap_soft(raw, cap)
    hard = [min(v, cap) for v in raw]
    assert all(s <= min(v, cap) + 1e-9
               for s, v in zip(soft, raw))
    assert all(abs(s - v) < 0.02 for s, v in zip(soft, raw)
               if v < cap - 4.0)            # untouched far below
    assert all(cap - s < 0.02 for s, v in zip(soft, raw)
               if v > cap + 4.0)            # asymptotic above
    grid = list(ex.AUTOEQ_GEQ_FREQS)

    def null_of(vals):
        resp = ex.sample_curve(fg, vals, grid)
        ref = ex.sample_curve(fg, vals, fg)
        text, shift = ex.graphiceq_text(grid, resp)
        return ex.null_test_graphic(text, fg, ref, shift)

    nh, ns = null_of(hard), null_of(soft)
    assert ns < nh                          # the corner was the cost
    assert ns <= ex.NULL_PASS_DB


def test_vendor_presets_shape_and_validator(tmp_path):
    t = {x["id"]: x for x in ex.load_targets(
        extra_dir="/nonexistent")}["vendor-graphic"]
    pres = t["presets"]
    names = [p["name"] for p in pres]
    assert any("Soundcore" in n for n in names)
    assert any("Denon" in n for n in names)
    assert any("ATVEL" in n for n in names)
    assert any("JBL" in n for n in names)
    for p in pres:
        cs = p["centers"]
        assert len(cs) >= 2 and all(b > a
                                    for a, b in zip(cs, cs[1:]))
        assert p["gain_range"][0] < p["gain_range"][1]
        assert p["gain_step"] > 0
    d = next(p for p in pres if "Denon" in p["name"])
    assert len(d["centers"]) == 5 and d["gain_step"] == 0.1
    bad = {"id": "vx", "name": "vx", "writer": "fixed",
           "centers": [100.0, 1000.0],
           "gain_range": [-6.0, 6.0], "gain_step": 1.0,
           "presets": [
               {"name": "ok", "centers": [100.0, 1000.0],
                "gain_range": [-6.0, 6.0], "gain_step": 1.0},
               {"name": "descending",
                "centers": [1000.0, 100.0],
                "gain_range": [-6.0, 6.0], "gain_step": 1.0},
               {"name": "no step", "centers": [100.0, 1000.0],
                "gain_range": [-6.0, 6.0]}]}
    json_path = tmp_path / "vx.json"
    json_path.write_text(json.dumps([bad]), encoding="utf-8")
    got = {x["id"]: x for x in ex.load_targets(
        extra_dir=str(tmp_path))}["vx"]
    assert [p["name"] for p in got["presets"]] == ["ok"]


def test_poweramp_preamp_spills_past_the_range():
    t = {x["id"]: x for x in ex.load_targets()}["poweramp"]
    assert t["preamp_range"] == [-12.0, 12.0]
    assert "preamp -12..12 dB" in ex.limits_text(t)
    fl = [{"type": "PK", "freq": 3000.0, "gain": -14.35,
           "q": 0.7, "enabled": True}]
    fr = [{"type": "HSC", "freq": 0.0, "gain": -0.18, "q": 1.0,
           "enabled": True},
          {"type": "PK", "freq": 3064.0, "gain": -11.68,
           "q": 1.0, "enabled": True}]
    chains = [("FL", -16.2, fl), ("FR", -16.2, fr)]
    text = ex.poweramp_json(t, chains, "spill")
    import json as _json
    preset = _json.loads(text)[0]
    assert preset["preamp"] == -12.0
    flats = {b["channels"]: b["gain"]
             for b in preset["bands"]
             if b["type"] == ex.PA_TYPE["LSC"]
             and b["frequency"] == 24000}
    # FL grows a fresh flat band; FR's trim absorbs the spill --
    # clean two-decimal gains, no float64 dust in the artifact
    assert flats[ex.PA_LEFT] == -4.2
    assert flats[ex.PA_RIGHT] == -4.38
    assert '"gain": -4.2,' in text
    freqs = ex.log_grid(20.0, 12000.0, 480)
    errs = ex.null_test_poweramp(text, chains, freqs)
    assert max(errs.values()) <= ex.NULL_PASS_DB, errs
    # inside the range nothing spills and no flat band appears
    calm = [("all", -6.0, fl)]
    t2 = _json.loads(ex.poweramp_json(t, calm, "calm"))[0]
    assert t2["preamp"] == -6.0
    assert not [b for b in t2["bands"]
                if b["frequency"] == 24000]
    assert "preamp_range" in ex._limits_invalid(
        {"preamp_range": [5.0, -5.0]})


def test_native_package_row_takes_rank_zero():
    """The native .pdeq row: first in the registry, verbatim
    rank in the audit, no limits to declare -- and the taste
    doctrine written on the page note."""
    targets = ex.load_targets()
    t = targets[0]
    assert t["id"] == "pdeq" and t["ext"] == ".pdeq"
    assert t["writer"] in ex.FILE_WRITERS
    assert "taste stays yours" in t["note"]
    assert "falsify" in ex.PDEQ_PAGE_DOCTRINE
    chains = [("FL", -6.0, []), ("FR", -6.0, [])]
    score, flag, reasons = ex.audit_target(
        t, chains, ex.log_grid(20.0, 20000.0, 60))
    assert score == 0 and flag == ""
    assert any("canvas" in r for r in reasons)
    assert ex.limits_text(t) == ""
    assert not ex._limits_invalid(t)
