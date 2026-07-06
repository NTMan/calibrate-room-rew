"""Tier-1 headroom bound (ROADMAP Task 2): the GUI-side pure-python curve
math (perdeviceeq.eq, no numpy/scipy allowed there) against the scipy
reference in tools/pde_audit.py, plus the bound's behavior on the
deterministic fixtures.

These tests pin the two biquad implementations together -- they used to
disagree on shelves by up to ~2 dB (RBJ slope form vs PipeWire's Q form;
see the note in pde_audit._shelf) -- and record the tier-1 reference
numbers for the demo profile (no preamp):

    max(total EQ curve):  FL +9.57 dB @ 200 Hz,  FR +8.48 dB @ 196 Hz
"""
import math

import numpy as np
import pytest
import soundfile as sf

from perdeviceeq.pde_audit import (
    DEMO_PROFILE, apply_chain, chain_curve, dbfs)
from perdeviceeq import eq

FS = 48000.0


def demo_bands(ch):
    """DEMO_PROFILE channel bands as perdeviceeq.eq.Band objects."""
    return [eq.Band(d["type"], d["freq"], d["gain"], d["q"], d.get("on", True))
            for d in DEMO_PROFILE["channels"][ch]]


def test_response_matches_scipy_reference_pointwise():
    """perdeviceeq.eq and pde_audit must be the same filter, everywhere --
    not just at the maximum. A pointwise mismatch here means one of them no
    longer models what PipeWire's param_eq actually runs."""
    la, lb = math.log10(20.0), math.log10(20000.0)
    freqs = [10 ** (la + (lb - la) * i / 239) for i in range(240)]
    for ch in ("FL", "FR"):
        ours = np.array(eq.response_db(0.0, demo_bands(ch), freqs))
        ref = chain_curve(DEMO_PROFILE["channels"][ch], FS, np.array(freqs))
        assert np.abs(ours - ref).max() < 1e-6


def test_curve_max_reference_numbers():
    assert eq.curve_max_db(0.0, demo_bands("FL")) == pytest.approx(9.57, abs=0.05)
    assert eq.curve_max_db(0.0, demo_bands("FR")) == pytest.approx(8.48, abs=0.05)


def test_curve_max_sees_narrow_peaks_between_grid_points():
    """The log grid is augmented with band centers: a Q=10 spike parked
    between the points of a deliberately coarse grid must still report its
    full +12 dB (a PK filter's response at its center IS its nominal gain)."""
    b = [eq.Band("PK", 3456.0, 12.0, 10.0, True)]
    assert eq.curve_max_db(0.0, b, n=60) == pytest.approx(12.0, abs=0.05)


def test_preamp_shifts_bound_linearly():
    bands = demo_bands("FR")
    b0 = eq.headroom_bound_db(0.0, bands)
    assert eq.headroom_bound_db(-7.5, bands) == pytest.approx(b0 - 7.5, abs=1e-9)
    # the GUI Auto rule (curve max, ceil'd to the 0.1 dB the spin can
    # express) must land the bound at or below 0 dBFS
    auto = -math.ceil(eq.curve_max_db(0.0, bands) * 10.0 - 1e-9) / 10.0
    assert eq.headroom_bound_db(auto, bands) <= 0.0


def test_flat_and_disabled_bands():
    assert eq.headroom_bound_db(0.0, []) == 0.0
    assert eq.headroom_bound_db(-3.0, []) == pytest.approx(-3.0)
    off = [eq.Band("PK", 200.0, 24.0, 1.0, False)]
    assert eq.headroom_bound_db(0.0, off) == 0.0


def test_bound_is_conservative_on_fixtures(fixtures_dir):
    """monitor_peak + max(curve) stays above the true post-EQ peak on both
    fixtures (tier-1's whole point), and so does the worst-case-0 variant
    the GUI shows -- even for hot_master, whose pre-EQ overshoot (+1.7)
    eats into, but does not exhaust, the crest-vs-sine-gain slack."""
    for fx in ("clean_master.wav", "hot_master.wav"):
        x, fs = sf.read(fixtures_dir / fx, dtype="float64", always_2d=True)
        for i, ch in enumerate(("FL", "FR")):
            pre = dbfs(x[:, i])
            post = dbfs(apply_chain(x[:, i], DEMO_PROFILE["channels"][ch],
                                    fs, 0.0))
            cm = eq.curve_max_db(0.0, demo_bands(ch))
            assert pre + cm >= post                     # true monitor peak
            assert eq.headroom_bound_db(0.0, demo_bands(ch)) >= post  # tier 1
