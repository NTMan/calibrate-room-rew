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
    assert "peq-text" in ids and "wavelet" in ids
    assert "vendor-8band" in ids and "hand-peq" in ids
    assert all(t["writer"] in ex.WRITERS for t in ts)


def test_targets_dropin_override_and_append(tmp_path):
    over = {"id": "vendor-8band", "name": "ACME Buds",
            "writer": "fixed",
            "centers": [100.0, 1000.0, 10000.0],
            "gain_range": [-4.0, 4.0], "gain_step": 0.5}
    fresh = {"id": "acme-sheet", "name": "ACME sheet",
             "writer": "sheet", "gain_step": 0.5}
    bad = {"name": "no id", "writer": "sheet"}
    (tmp_path / "10-acme.json").write_text(
        json.dumps([over, fresh, bad]), encoding="utf-8")
    (tmp_path / "junk.json").write_text("{", encoding="utf-8")
    ts = ex.load_targets(extra_dir=str(tmp_path))
    ids = [t["id"] for t in ts]
    assert ids.count("vendor-8band") == 1
    got = ts[ids.index("vendor-8band")]
    assert got["name"] == "ACME Buds" and len(got["centers"]) == 3
    assert got["_src"].endswith("10-acme.json")
    assert "acme-sheet" in ids
    assert ids.index("vendor-8band") < ids.index("hand-peq")


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
    assert ex.collapse_choices(two, band_domain=True) == ["FL", "FR"]
    # mean leads: the shared-channels default (field feedback)
    assert ex.collapse_choices(two, band_domain=False) == \
        ["mean", "FL", "FR"]


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


def test_registry_jamesdsp_is_graphiceq():
    t = {x["id"]: x for x in ex.load_targets()}
    assert t["jamesdsp"]["writer"] == "graphiceq"
    assert t["wavelet"].get("bare") is True


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


# ---- the hand-transfer sheet -------------------------------------------

def test_sheet_rounds_to_steps():
    t = {"id": "x", "name": "X", "writer": "sheet",
         "gain_step": 0.5, "q_step": 0.01}
    bands = [{"type": "PK", "freq": 1234.5, "gain": -3.24,
              "q": 1.4142, "enabled": True},
             {"type": "HSC", "freq": 8000.0, "gain": 2.26,
              "q": 0.707, "enabled": True},
             {"type": "PK", "freq": 400.0, "gain": 5.0, "q": 1.0,
              "enabled": False}]
    text = ex.sheet_text(t, -3.24, bands,
                         header=("Target: X", "Collapse: FL"))
    assert "Collapse: FL" in text
    assert "Preamp: -3.0 dB" in text  # round(-6.48 steps) == -6
    assert "-3.0" in text and "+2.5" in text
    assert "1.41" in text and "0.71" in text
    assert "Peak" in text and "High shelf" in text
    assert text.count("\n 1  ") + text.count("\n 2  ") == 2
    assert "400" not in text          # disabled band not printed
    assert "gain 0.5 dB, Q 0.01" in text


def test_rounded_chain_reports_delta_material():
    t = {"gain_step": 1.0, "q_step": 0.1}
    g, b = ex.rounded_chain(t, -2.4,
                            [{"type": "PK", "freq": 997.0,
                              "gain": 2.6, "q": 1.16,
                              "enabled": True}])
    assert g == pytest.approx(-2.0)
    assert b[0]["gain"] == pytest.approx(3.0)
    assert b[0]["q"] == pytest.approx(1.2)
    assert b[0]["freq"] == pytest.approx(997.0)   # no freq_step


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
