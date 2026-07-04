"""Regression tests for the headroom audit math (ROADMAP Task 2).

Reference numbers come from the ROADMAP table (seed 20260704); dB values are
asserted to ±0.1 dB to survive minor scipy resampler changes. Re-derived
2026-07-05 after the shelf-parameterization fix (see pde_audit._shelf: the
slope form diverged from PipeWire's Q-form biquads); the -7.5 preamp
recommendation survived the fix unchanged.
"""
import numpy as np
import pytest
import soundfile as sf

from pde_audit import DEMO_PROFILE, apply_chain, chain_curve, clip_stats, dbfs


def run_demo(path):
    """Apply the demo FL/FR chains to a stereo capture, return per-channel stats."""
    x, fs = sf.read(path, dtype="float64", always_2d=True)
    out = {}
    for ch, name in [(0, "FL"), (1, "FR")]:
        post = apply_chain(x[:, ch], DEMO_PROFILE["channels"][name], fs,
                           DEMO_PROFILE.get("preamp", 0.0))
        n, pct, _ = clip_stats(post)
        out[name] = {"pre": dbfs(x[:, ch]), "post": dbfs(post),
                     "clips": n, "pct": pct}
    return out


def test_clean_master_has_headroom(fixtures_dir):
    r = run_demo(fixtures_dir / "clean_master.wav")
    assert r["FL"]["pre"] == pytest.approx(-4.28, abs=0.1)
    assert r["FR"]["pre"] == pytest.approx(-5.67, abs=0.1)
    assert r["FL"]["post"] == pytest.approx(-2.82, abs=0.1)
    assert r["FR"]["post"] == pytest.approx(-2.76, abs=0.1)
    assert r["FL"]["clips"] == 0
    assert r["FR"]["clips"] == 0
    # recommended preamp: no change needed
    worst = max(r["FL"]["post"], r["FR"]["post"])
    assert max(0.0, worst) == 0.0


def test_hot_master_clips_and_preamp_recommendation(fixtures_dir):
    r = run_demo(fixtures_dir / "hot_master.wav")
    # intersample overshoot: input alone already exceeds full scale
    assert r["FL"]["pre"] > 0
    assert r["FR"]["pre"] > 0
    assert r["FL"]["post"] == pytest.approx(7.10, abs=0.1)
    assert r["FR"]["post"] == pytest.approx(7.49, abs=0.1)
    assert r["FL"]["pct"] > 5.0
    assert r["FR"]["pct"] > 5.0
    worst = max(r["FL"]["post"], r["FR"]["post"])
    assert -worst == pytest.approx(-7.5, abs=0.1)


def test_biquad_curves_match_nominal_gains():
    fs = 48000.0
    # peaking: response at f0 equals the band gain
    pk = [{"type": "PK", "freq": 1000, "gain": -6.0, "q": 1.0}]
    assert chain_curve(pk, fs, np.array([1000.0]))[0] == pytest.approx(-6.0, abs=0.01)
    # low shelf: low-frequency asymptote equals the gain, HF asymptote ~0
    ls = [{"type": "LSC", "freq": 200, "gain": 4.0, "q": 0.707}]
    assert chain_curve(ls, fs, np.array([10.0]))[0] == pytest.approx(4.0, abs=0.05)
    assert chain_curve(ls, fs, np.array([10000.0]))[0] == pytest.approx(0.0, abs=0.05)
    # high shelf: mirror image
    hs = [{"type": "HSC", "freq": 2000, "gain": 3.0, "q": 0.707}]
    assert chain_curve(hs, fs, np.array([20000.0]))[0] == pytest.approx(3.0, abs=0.05)
    assert chain_curve(hs, fs, np.array([20.0]))[0] == pytest.approx(0.0, abs=0.05)


def test_disabled_bands_are_skipped():
    fs = 48000.0
    bands = [{"type": "PK", "freq": 1000, "gain": -15.0, "q": 1.0, "on": False}]
    rng = np.random.default_rng(1)
    x = rng.standard_normal(fs.__int__())
    y = apply_chain(x, bands, fs)
    assert np.allclose(x, y)
