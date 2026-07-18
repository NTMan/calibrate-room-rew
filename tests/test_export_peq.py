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
    assert ex.collapse_choices(two, band_domain=False) == \
        ["FL", "FR", "mean"]


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
    assert text.startswith("per-device-eq export\n")
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


def test_graphiceq_line_and_null():
    chains = ex.composed_chains(_profile_all(), None)
    gf = ex.graphic_grid()
    assert len(gf) == len(set(gf))
    assert gf[0] == 20.0 and gf[-1] == 20000.0
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
