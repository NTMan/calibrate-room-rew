"""Synthetic-loop tests for the measurement core (ROADMAP Task 3, incr. 1).

No audio hardware: the "room" is a known pde_audit biquad chain, the "mic" is
ideal. The loop must close within +-0.5 dB in 40 Hz - 16 kHz. If it does not,
fix the core, do not relax the tolerance: in synthetics there is no mic and
no room, only math.
"""
import json

import numpy as np
import pytest

from pde_audit import DEMO_PROFILE, apply_chain, chain_curve
import measure_core as mc

FS = 48000
F_LO_CHECK, F_HI_CHECK = 40.0, 16000.0
TOL_DB = 0.5


@pytest.fixture(scope="module")
def sweep():
    return mc.generate_sweep()          # 256k @ 48k, -6 dBFS, 20-20k


def band(freqs):
    return (freqs >= F_LO_CHECK) & (freqs <= F_HI_CHECK)


def synth_recording(sweep, bands, delay_s=1.0, tail_s=0.5, noise_sigma=1e-3,
                    seed=0, k2=0.0):
    """Sweep through a known chain (+ optional x + k2*x**2 distortion at the
    'speaker') into a recording with leading silence and additive noise."""
    y = apply_chain(sweep.signal, bands, sweep.fs) if bands \
        else sweep.signal.copy()
    if k2:
        y = y + k2 * y * y
    rec = np.concatenate([np.zeros(int(delay_s * sweep.fs)), y,
                          np.zeros(int(tail_s * sweep.fs))])
    rec += noise_sigma * np.random.default_rng(seed).standard_normal(len(rec))
    return rec


def raw_curve(result):
    return (np.asarray(result["data"]["freq_hz"]),
            np.asarray(result["data"]["mag_db_raw"], dtype=float))


# --- excitation ---------------------------------------------------------

def test_sweep_level_length_and_fades(sweep):
    assert sweep.n_samples == mc.DEFAULT_N
    assert np.max(np.abs(sweep.signal)) == pytest.approx(10 ** (-6 / 20),
                                                         rel=1e-4)
    assert abs(sweep.signal[0]) < 1e-6 and abs(sweep.signal[-1]) < 1e-3


def test_inverse_sweep_flattens_the_sweep(sweep):
    inv = mc.inverse_sweep(sweep)
    m = 2 * sweep.n_samples
    d = np.fft.irfft(np.fft.rfft(sweep.signal, m) * np.fft.rfft(inv, m), m)
    assert np.max(np.abs(d)) == pytest.approx(1.0, rel=1e-6)
    f = np.fft.rfftfreq(m, 1 / sweep.fs)
    sel = (f >= 100) & (f <= 10000)
    db = 20 * np.log10(np.abs(np.fft.rfft(d, m))[sel])
    db -= np.median(db)                 # peak-normalization offsets the level
    assert np.max(np.abs(db)) < 0.1     # mid-band flat within +-0.1 dB


# --- the loop -----------------------------------------------------------

@pytest.mark.parametrize("ch", ["FL", "FR"])
def test_loop_recovers_chain_curve(sweep, ch):
    bands = DEMO_PROFILE["channels"][ch]
    rec = synth_recording(sweep, bands, seed=1)
    result = mc.process_takes([rec], sweep)
    freqs, mag = raw_curve(result)
    sel = band(freqs)
    ref = chain_curve(bands, FS, freqs[sel])
    err = np.abs(mag[sel] - ref)
    assert err.max() <= TOL_DB, f"max err {err.max():.3f} dB"
    assert not result["warnings"]


def test_harmonic_distortion_is_windowed_out(sweep):
    bands = DEMO_PROFILE["channels"]["FL"]
    clean = mc.process_takes([synth_recording(sweep, bands, seed=2)], sweep)
    dirty = mc.process_takes(
        [synth_recording(sweep, bands, seed=2, k2=0.05)], sweep)
    freqs, mag_c = raw_curve(clean)
    _, mag_d = raw_curve(dirty)
    sel = band(freqs)
    ref = chain_curve(bands, FS, freqs[sel])
    assert np.abs(mag_d[sel] - ref).max() <= TOL_DB
    # Farina windowing: the curve must not move when distortion is added.
    assert np.abs(mag_d[sel] - mag_c[sel]).max() <= 0.2


def test_three_takes_with_delay_scatter_converge(sweep):
    bands = DEMO_PROFILE["channels"]["FR"]
    recs = [synth_recording(sweep, bands, delay_s=d, seed=s)
            for d, s in [(0.96, 3), (1.00, 4), (1.04, 5)]]   # +-40 ms
    result = mc.process_takes(recs, sweep)
    freqs, mag = raw_curve(result)
    sel = band(freqs)
    ref = chain_curve(bands, FS, freqs[sel])
    assert np.abs(mag[sel] - ref).max() <= TOL_DB
    spread = np.asarray(result["data"]["spread_db"], dtype=float)
    assert spread[sel].max() < 0.3      # magnitude-only averaging converged
    # 80 ms peak-to-peak delay jitter must trip the ROADMAP BT warning.
    assert result["takes"]["delay_jitter_ms"] == pytest.approx(80.0, abs=1.0)
    assert mc.BT_JITTER_WARNING in result["warnings"]


def test_stable_delays_do_not_warn(sweep):
    bands = DEMO_PROFILE["channels"]["FL"]
    recs = [synth_recording(sweep, bands, seed=s) for s in (6, 7)]
    result = mc.process_takes(recs, sweep)
    assert result["takes"]["delay_jitter_ms"] <= 1.0
    assert mc.BT_JITTER_WARNING not in result["warnings"]


# --- SNR ----------------------------------------------------------------

def test_low_snr_warns(sweep):
    bands = DEMO_PROFILE["channels"]["FL"]
    y = apply_chain(sweep.signal, bands, FS)
    sigma = float(np.sqrt(np.mean(y ** 2))) * 10 ** (-20 / 20)
    rec = synth_recording(sweep, bands, noise_sigma=sigma, seed=8)
    result = mc.process_takes([rec], sweep)
    assert result["takes"]["snr_min_db"] == pytest.approx(20.0, abs=3.0)
    assert any(w.startswith("low SNR") for w in result["warnings"])


def test_clean_loop_snr_is_sane(sweep):
    rec = synth_recording(sweep, DEMO_PROFILE["channels"]["FL"], seed=9)
    result = mc.process_takes([rec], sweep)
    assert result["takes"]["snr_min_db"] > mc.SNR_WARN_DB


# --- mic calibration ------------------------------------------------------

def test_mic_cal_parse_and_apply(sweep, tmp_path):
    cal = tmp_path / "umik.txt"
    cal.write_text('"Sens Factor =-.6383dB, SERNO: 7000000"\n'
                   "* comment line\n"
                   "20 0.0\n"
                   "1000 1.0 45.0\n"      # optional phase column
                   "20000 3.0\n")
    fr, db = mc.load_mic_cal(str(cal))
    assert list(fr) == [20.0, 1000.0, 20000.0]
    rec = synth_recording(sweep, [], seed=10)      # flat chain
    result = mc.process_takes([rec], sweep, cal=str(cal))
    freqs, mag = raw_curve(result)
    for f, expect in [(1000.0, -1.0), (100.0, -0.411)]:
        i = int(np.argmin(np.abs(freqs - f)))
        assert mag[i] == pytest.approx(expect, abs=0.1)
    assert result["cal_file"] == str(cal)


# --- result schema ----------------------------------------------------------

def test_result_schema_carries_increment2_stubs(sweep, tmp_path):
    rec = synth_recording(sweep, [], seed=11)
    result = mc.process_takes([rec], sweep, device="bt_sink.node.name")
    out = tmp_path / "m.json"
    mc.save_result(result, out)
    r = json.loads(out.read_text())
    assert r["schema"] == mc.SCHEMA and r["schema_version"] == 1
    assert r["device"] == "bt_sink.node.name"
    assert r["eq_profile_state"] == {"profile": None, "bypass": None}
    assert r["levels"]["sweep_level_dbfs"] == -6.0
    assert set(r["levels"]) == {"sink_volume", "stream_volume",
                                "sweep_level_dbfs"}
    assert r["path_clean"] == {"verified": None, "unknown_nodes": []}
    assert r["foreign_streams"] == []
    n = len(r["data"]["freq_hz"])
    assert n == len(r["data"]["mag_db_raw"]) == len(
        r["data"]["mag_db_smoothed"])
    assert r["data"]["spread_db"] is None          # single take
    assert r["takes"]["count"] == 1
