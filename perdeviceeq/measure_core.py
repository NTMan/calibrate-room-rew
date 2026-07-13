#!/usr/bin/env python3
"""Offline DSP core for the measurement wizard (ROADMAP Task 3, increment 1).

No GUI, no hardware: sweep -> impulse -> magnitude math, CI-testable against
pde_audit's biquad chains. Increment 2 adds the PipeWire front-end
(pw-play/pw-record, link-path verification, foreign-stream detection),
increment 3 the constrained fit, increment 4 the GTK wizard.

Method notes (worth not re-deriving):

- Excitation is a Farina exponential sine sweep at a FIXED -6 dBFS digital
  level. Level methodology: measure at the working listening level via the
  sink's own volume control, never bypass or normalize volumes; the actual
  sink/stream volumes are *recorded* in the result metadata instead.
- Deconvolution is a regularized spectral division by the FFT of the ACTUAL
  generated sweep (fades and all), not the analytic inverse: for the linear
  part this is exact within the sweep band, so the synthetic loop closes to
  numerical noise. The classic time-reversed analytic inverse
  (`inverse_sweep`) is still provided; increment 2 saves it next to the
  sweep wav for cross-checks against REW.
- Farina's property holds either way: harmonic distortion deconvolves to
  impulses BEFORE the linear IR (k-th harmonic at t = -L*ln k; ~0.55 s for
  the 2nd with the default sweep), so a window around the linear peak drops
  them.
- Every take is aligned by the peak of its OWN impulse and takes are averaged
  in magnitude (power/RMS) only. Playback (BT sink) and capture (USB mic)
  clocks are independent: vector/time-domain averaging is wrong by
  construction. Inter-take delay jitter > ~2 ms raises the ROADMAP BT
  warning; per-frequency magnitude spread across takes is stored alongside.
- A measurement is only interpretable against a known EQ state (Task 4
  lesson): it must run with our profile bypassed, and `eq_profile_state`
  must be filled by the caller. Increment 1 keeps the field as a stub, but
  the schema carries it from day one.
"""
import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
from scipy import signal as sg

DEFAULT_N = 262144            # 256k samples, ~5.46 s @ 48 kHz
DEFAULT_FS = 48000
SWEEP_LEVEL_DBFS = -6.0       # fixed digital sweep level, by protocol
DEFAULT_F_START = 20.0
DEFAULT_F_END = 20000.0
SNR_WARN_DB = 40.0
JITTER_WARN_MS = 2.0          # ROADMAP Task 3 BT sanity threshold
BT_JITTER_WARNING = "wireless link unstable, HF may be unreliable"
SCHEMA = "pde-measurement"
SCHEMA_VERSION = 1


# --- excitation ------------------------------------------------------------

@dataclass
class Sweep:
    signal: np.ndarray        # float64, peak = 10**(level_dbfs/20)
    fs: int
    f_start: float
    f_end: float
    level_dbfs: float

    @property
    def n_samples(self):
        return len(self.signal)

    @property
    def duration_s(self):
        return self.n_samples / self.fs

    @property
    def sweep_rate_l(self):
        """Farina L = T / ln(f2/f1); k-th harmonic image sits at -L*ln k."""
        return self.duration_s / math.log(self.f_end / self.f_start)


def generate_sweep(n_samples=DEFAULT_N, fs=DEFAULT_FS, f_start=DEFAULT_F_START,
                   f_end=DEFAULT_F_END, fade_in_ms=30.0, fade_out_ms=10.0):
    """Farina log sweep at the fixed -6 dBFS digital level.

    Raised-cosine fades avoid on/off clicks; their spectral splash cancels in
    deconvolution because the division uses this exact signal.
    """
    t = np.arange(n_samples) / fs
    L = (n_samples / fs) / math.log(f_end / f_start)
    x = np.sin(2 * np.pi * f_start * L * (np.exp(t / L) - 1.0))
    n_in = int(round(fade_in_ms * fs / 1000))
    n_out = int(round(fade_out_ms * fs / 1000))
    if n_in:
        x[:n_in] *= 0.5 - 0.5 * np.cos(np.pi * np.arange(n_in) / n_in)
    if n_out:
        x[-n_out:] *= 0.5 + 0.5 * np.cos(np.pi * (np.arange(n_out) + 1) / n_out)
    return Sweep(10 ** (SWEEP_LEVEL_DBFS / 20) * x, fs, f_start, f_end,
                 SWEEP_LEVEL_DBFS)


def inverse_sweep(sweep):
    """Analytic Farina inverse: time-reversed sweep with an exponential
    amplitude envelope (-6 dB/oct overall), normalized so that convolving it
    with the sweep peaks at 1.0. Provided for REW cross-checks; the pipeline
    itself deconvolves by exact spectral division (see `deconvolve`)."""
    n = sweep.n_samples
    t = np.arange(n) / sweep.fs
    inv = sweep.signal[::-1] * np.exp(-t / sweep.sweep_rate_l)
    m = 2 * n
    ref = np.fft.irfft(np.fft.rfft(sweep.signal, m) * np.fft.rfft(inv, m), m)
    inv /= np.max(np.abs(ref))
    return inv


# --- recording -> impulse -> magnitude --------------------------------------

def deconvolve(recording, sweep, reg=1e-8, hp_hz=None):
    """Recording -> full (circular) impulse response.

    Tikhonov-regularized division by the sweep spectrum: exact for the linear
    part in-band, bounded out-of-band. The linear IR lands at the system
    delay; distortion harmonics land ~L*ln(k) earlier (wrapping to the buffer
    end when the delay is short) and are removed by `extract_linear_ir`.
    hp_hz defaults to f_start/4 (0 disables the highpass).
    """
    rec = np.asarray(recording, dtype=float)
    if hp_hz is None:
        hp_hz = sweep.f_start / 4
    if hp_hz:
        # Kill sub-band junk BEFORE the division, causally. Even-order
        # distortion carries a slow near-DC envelope term; left in, the
        # regularized division turns it into broadband time smear that leaks
        # through the IR window and tilts the low end by tenths of a dB.
        # A minimum-phase highpass rings only forward in time (window-safe;
        # a zero-phase gate on H was tried and its acausal LF ringing,
        # truncated by the window, cost ~0.35 dB at 40 Hz) and biases the
        # passband by < 1e-4 dB at 2*hp_hz. Real captures get DC/rumble
        # removal for free.
        sos = sg.butter(4, hp_hz, "highpass", fs=sweep.fs, output="sos")
        rec = sg.sosfilt(sos, rec)
    n_fft = 1 << int(math.ceil(math.log2(len(rec) + sweep.n_samples)))
    r = np.fft.rfft(rec, n_fft)
    x = np.fft.rfft(sweep.signal, n_fft)
    px = np.abs(x) ** 2
    h = r * np.conj(x) / (px + reg * px.max())
    return np.fft.irfft(h, n_fft)


def extract_linear_ir(ir, fs, pre_flat_ms=10.0, pre_taper_ms=10.0,
                      post_ms=350.0):
    """Window the linear impulse out of the deconvolution buffer.

    Aligned to the peak of THIS impulse (per-take alignment, no shared time
    reference). The IR |max| of a resonant system lags the causal onset by
    milliseconds, so the window keeps full weight over pre_flat_ms BEFORE the
    peak and only tapers (half-Hann) further out, where a causal response is
    silent -- a taper touching the peak eats real onset energy and tilts the
    low end by ~0.4 dB. The whole pre region stays two orders of magnitude
    under the 2nd-harmonic offset (~548 ms for the default sweep), so
    Farina's pre-impulse distortion images are cut. Returns
    (windowed_ir, peak_index); the tail is half-Hann over the last fifth of
    post_ms.
    """
    ir = np.asarray(ir)
    peak = int(np.argmax(np.abs(ir)))
    taper = int(round(pre_taper_ms * fs / 1000))
    pre = taper + int(round(pre_flat_ms * fs / 1000))
    post = int(round(post_ms * fs / 1000))
    seg = ir[(peak + np.arange(-pre, post)) % len(ir)].copy()
    if taper:
        seg[:taper] *= 0.5 - 0.5 * np.cos(np.pi * np.arange(taper) / taper)
    fall = max(1, post // 5)
    seg[-fall:] *= 0.5 + 0.5 * np.cos(np.pi * (np.arange(fall) + 1) / fall)
    return seg, peak


def log_grid(f_lo=20.0, f_hi=20000.0, ppo=96):
    """Uniform-in-log2 frequency grid, `ppo` points per octave."""
    n = int(round(math.log2(f_hi / f_lo) * ppo)) + 1
    return f_lo * 2.0 ** (np.arange(n) / ppo)


def ir_to_magnitude(ir_seg, fs, freqs):
    """Windowed IR -> magnitude (dB) sampled on `freqs` (log-f interp)."""
    n_fft = 2 ** (int(math.ceil(math.log2(len(ir_seg)))) + 3)
    mag = np.abs(np.fft.rfft(ir_seg, n_fft))
    f_lin = np.fft.rfftfreq(n_fft, 1.0 / fs)
    db = 20 * np.log10(np.maximum(mag, 1e-12))
    return np.interp(np.log(freqs), np.log(f_lin[1:]), db[1:])


def smooth_fractional_octave(mag_db, ppo, fraction=6):
    """1/`fraction`-octave box smoothing in the POWER domain on a uniform-log
    grid (`ppo` points/octave). Psychoacoustic (variable-window) smoothing is
    a later refinement; fraction=None/0 returns the input unchanged."""
    mag_db = np.asarray(mag_db, dtype=float)
    if not fraction:
        return mag_db.copy()
    half = max(1, int(round(ppo / (2 * fraction))))
    p = np.pad(10 ** (mag_db / 10), half, mode="edge")
    ker = np.full(2 * half + 1, 1.0 / (2 * half + 1))
    return 10 * np.log10(np.convolve(p, ker, mode="valid"))


# --- mic calibration ---------------------------------------------------------

def load_mic_cal(path):
    """miniDSP/REW mic calibration text: `freq dB [phase]` rows. Header junk
    ('Sens Factor = ...', '*' comments) is skipped by the float parse."""
    fr, db = [], []
    with open(path) as f:
        for line in f:
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                fr.append(float(parts[0]))
                db.append(float(parts[1]))
            except ValueError:
                continue
    if len(fr) < 2:
        raise ValueError(f"{path}: no parsable 'freq dB' rows")
    order = np.argsort(fr)
    return np.asarray(fr)[order], np.asarray(db)[order]


def apply_mic_cal(freqs, mag_db, cal_freq, cal_db):
    """Subtract the mic's own response (log-f interp, edge values held)."""
    return np.asarray(mag_db) - np.interp(np.log(freqs), np.log(cal_freq),
                                          cal_db)


# --- SNR ---------------------------------------------------------------------

def estimate_snr(recording, peak_index, sweep, guard_ms=50.0,
                 min_noise_ms=100.0):
    """Noise floor from the pre-sweep silence vs in-sweep signal RMS.

    `peak_index` (linear-IR position from `extract_linear_ir`) doubles as the
    sweep onset in the recording. Returns (snr_db, signal_dbfs, noise_dbfs);
    all None when the recording has no usable pre-roll.
    """
    rec = np.asarray(recording, dtype=float)
    fs = sweep.fs
    n_noise = peak_index - int(guard_ms * fs / 1000)
    if n_noise < int(min_noise_ms * fs / 1000):
        return None, None, None
    noise = math.sqrt(float(np.mean(rec[:n_noise] ** 2)))
    s1 = min(len(rec), peak_index + sweep.n_samples)
    sig = math.sqrt(float(np.mean(rec[peak_index:s1] ** 2)))
    sig_db = 20 * math.log10(sig) if sig > 0 else float("-inf")
    noise_db = 20 * math.log10(noise) if noise > 0 else float("-inf")
    snr = sig_db - noise_db
    return snr, sig_db, noise_db


# --- takes, averaging, result ------------------------------------------------

@dataclass
class Take:
    freq: np.ndarray
    mag_db: np.ndarray        # raw magnitude on the grid, no cal, no smoothing
    delay_ms: float           # linear-IR peak position in the recording
    snr_db: object
    signal_dbfs: object
    noise_dbfs: object


def analyze_take(recording, sweep, freqs, pre_flat_ms=10.0,
                 pre_taper_ms=10.0, post_ms=350.0, reg=1e-8):
    """One recording of one sweep -> raw magnitude curve + per-take stats."""
    ir = deconvolve(recording, sweep, reg)
    seg, peak = extract_linear_ir(ir, sweep.fs, pre_flat_ms, pre_taper_ms,
                                  post_ms)
    mag = ir_to_magnitude(seg, sweep.fs, freqs)
    snr, sig_db, noise_db = estimate_snr(recording, peak, sweep)
    return Take(freqs, mag, 1000.0 * peak / sweep.fs, snr, sig_db, noise_db)


def average_takes(takes):
    """Magnitude-only (power/RMS) average + per-frequency spread (dB std,
    ddof=1). Items are Take-likes (anything with .mag_db) or bare
    magnitude arrays -- the session passes gain-compensated arrays.
    Alignment is inherent: each take was windowed around its own
    impulse peak. Returns (avg_db, spread_db_or_None)."""
    mags = np.vstack([getattr(t, "mag_db", t) for t in takes])
    avg = 10 * np.log10(np.mean(10 ** (mags / 10), axis=0))
    spread = mags.std(axis=0, ddof=1) if len(takes) > 1 else None
    return avg, spread


def process_takes(recordings, sweep, cal=None, smoothing_fraction=6,
                  f_lo=20.0, f_hi=20000.0, ppo=96, pre_flat_ms=10.0,
                  pre_taper_ms=10.0, post_ms=350.0, reg=1e-8, device=None, rig=None, mic=None, cal_file=None,
                  sink_api=None, eq_profile_state=None, levels=None,
                  path_clean=None, foreign_streams=None):
    """Full offline pipeline: N recordings of the same sweep -> result dict.

    `cal` is a path or a (freq, db) pair; both raw and smoothed output curves
    are cal-corrected ("raw" = unsmoothed). The increment-2 metadata
    (`device`, `eq_profile_state`, `levels`, `path_clean`, `foreign_streams`)
    is carried as stubs so the schema never has to migrate:
    - eq_profile_state: the measurement MUST run with our profile bypassed
      and the state pinned (a capture against an unknown EQ state is
      archaeology, Task 4 lesson);
    - levels: sink volume, sweep-stream volume, fixed digital sweep level
      (measure at the working listening level via the sink's own control);
    - path_clean: link-path verification verdict + unidentified nodes between
      the sweep stream and the device;
    - foreign_streams: other streams on the sink at measure time (increment 2
      refuses to start when non-empty; muting them is opt-in only).
    """
    freqs = log_grid(f_lo, f_hi, ppo)
    if isinstance(cal, str):
        cal_file = cal_file or cal
        cal = load_mic_cal(cal)
    takes = [analyze_take(r, sweep, freqs, pre_flat_ms, pre_taper_ms,
                          post_ms, reg) for r in recordings]
    avg, spread = average_takes(takes)
    uncal = np.asarray(avg).copy()          # before any cal: lets a different
    #                                         cal (HEQ/IDF/RAW/HPN) be applied
    #                                         later without re-measuring
    if cal is not None:
        avg = apply_mic_cal(freqs, avg, cal[0], cal[1])
    smoothed = smooth_fractional_octave(avg, ppo, smoothing_fraction)

    delays = [t.delay_ms for t in takes]
    jitter = max(delays) - min(delays) if len(takes) > 1 else 0.0
    snrs = [t.snr_db for t in takes]
    known_snrs = [s for s in snrs if s is not None]
    snr_min = min(known_snrs) if known_snrs else None

    warnings = []
    if jitter > JITTER_WARN_MS:
        api = sink_api or ""
        if api.startswith("bluez"):
            warnings.append("%s (delay jitter %.1f ms)"
                            % (BT_JITTER_WARNING, jitter))
        elif not api:
            # offline processing: the sink is unknown, stay cautious
            warnings.append("delay jitter %.1f ms between takes; on a "
                            "wireless link HF may be unreliable"
                            % jitter)
        # a known wired sink's start-time jitter is pw-play spawn
        # timing, not link clock drift: each take aligns on its own
        # impulse, magnitude is unaffected -- say nothing
    if snr_min is not None and snr_min < SNR_WARN_DB:
        warnings.append(f"low SNR ({snr_min:.1f} dB): raise the level or "
                        f"kill the noise source")
    if len(known_snrs) < len(takes):
        warnings.append("no usable pre-sweep silence in some takes, "
                        "noise floor not estimated")

    lv = {"sink_volume": None, "stream_volume": None}
    lv.update(levels or {})
    lv["sweep_level_dbfs"] = sweep.level_dbfs

    result = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "sample_rate": sweep.fs,
        "sweep": {"n_samples": sweep.n_samples, "f_start": sweep.f_start,
                  "f_end": sweep.f_end, "level_dbfs": sweep.level_dbfs,
                  "duration_s": round(sweep.duration_s, 6)},
        "grid": {"f_lo": f_lo, "f_hi": f_hi, "ppo": ppo},
        "smoothing": {"type": "fractional-octave",
                      "fraction": smoothing_fraction, "domain": "power"},
        "window": {"pre_flat_ms": pre_flat_ms, "pre_taper_ms": pre_taper_ms,
                   "post_ms": post_ms},
        "sink_api": sink_api,
        "device": device,
        "rig": rig,
        "mic": mic,
        "cal_file": cal_file,
        "takes": {"count": len(takes), "delay_ms": delays,
                  "delay_jitter_ms": jitter, "snr_db": snrs,
                  "snr_min_db": snr_min,
                  "signal_dbfs": [t.signal_dbfs for t in takes],
                  "noise_floor_dbfs": [t.noise_dbfs for t in takes]},
        "spread": None if spread is None else {
            "max_db": float(spread.max()),
            "median_db": float(np.median(spread))},
        "eq_profile_state": eq_profile_state or {"profile": None,
                                                 "bypass": None},
        "levels": lv,
        "path_clean": path_clean or {"verified": None, "unknown_nodes": []},
        "foreign_streams": foreign_streams or [],
        "warnings": warnings,
        "data": {"freq_hz": freqs, "mag_db_raw": avg,
                 "mag_db_smoothed": smoothed, "mag_db_uncal": uncal,
                 "spread_db": spread},
    }
    return _jsonable(result)


def _jsonable(obj):
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [_jsonable(float(v)) for v in obj]
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return round(f, 4) if math.isfinite(f) else None
    if isinstance(obj, np.integer):
        return int(obj)
    return obj


def save_result(result, path):
    with open(path, "w") as f:
        json.dump(result, f, indent=1)
        f.write("\n")


# --- CLI (offline: generate sweep wavs, process recorded wavs) ---------------

def _cli_gen(a):
    import soundfile as sf
    sw = generate_sweep(a.samples, a.fs, a.f_start, a.f_end)
    pad0 = np.zeros(int(a.pre_silence * a.fs))
    pad1 = np.zeros(int(a.post_silence * a.fs))
    sf.write(a.out, np.concatenate([pad0, sw.signal, pad1]).astype("float32"),
             a.fs, subtype="FLOAT")
    if a.inverse:
        sf.write(a.inverse, inverse_sweep(sw).astype("float32"), a.fs,
                 subtype="FLOAT")
    sidecar = {"n_samples": sw.n_samples, "fs": sw.fs,
               "f_start": sw.f_start, "f_end": sw.f_end,
               "level_dbfs": sw.level_dbfs, "pre_silence_s": a.pre_silence,
               "post_silence_s": a.post_silence}
    with open(a.out + ".json", "w") as f:
        json.dump(sidecar, f, indent=1)
    print(f"{a.out}: {sw.duration_s:.2f} s sweep {sw.f_start:g}-{sw.f_end:g} "
          f"Hz @ {sw.level_dbfs:g} dBFS, sidecar {a.out}.json")


def _cli_process(a):
    import soundfile as sf
    if a.sweep_json:
        with open(a.sweep_json) as f:
            sc = json.load(f)
        sw = generate_sweep(sc["n_samples"], sc["fs"], sc["f_start"],
                            sc["f_end"])
    else:
        sw = generate_sweep(a.samples, a.fs, a.f_start, a.f_end)
    recs = []
    for path in a.recording:
        x, fs = sf.read(path, dtype="float64", always_2d=True)
        if fs != sw.fs:
            sys.exit(f"{path}: rate {fs} != sweep rate {sw.fs}")
        recs.append(x[:, a.channel])
    result = process_takes(recs, sw, cal=a.cal,
                           smoothing_fraction=a.smoothing, device=a.device,
                           rig=a.rig, mic=a.mic)
    save_result(result, a.out)
    t = result["takes"]
    print(f"{a.out}: {t['count']} take(s), delay jitter "
          f"{t['delay_jitter_ms']:.2f} ms, SNR min "
          f"{t['snr_min_db'] if t['snr_min_db'] is not None else 'n/a'} dB")
    for w in result["warnings"]:
        print(f"WARNING: {w}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gen", help="write sweep wav (+ sidecar, + inverse)")
    g.add_argument("out")
    g.add_argument("--inverse", help="also write the analytic inverse wav")
    g.add_argument("--samples", type=int, default=DEFAULT_N)
    g.add_argument("--fs", type=int, default=DEFAULT_FS)
    g.add_argument("--f-start", type=float, default=DEFAULT_F_START)
    g.add_argument("--f-end", type=float, default=DEFAULT_F_END)
    g.add_argument("--pre-silence", type=float, default=1.0)
    g.add_argument("--post-silence", type=float, default=0.5)
    g.set_defaults(func=_cli_gen)

    r = sub.add_parser("process", help="recorded take wavs -> result JSON")
    r.add_argument("out", help="output JSON path")
    r.add_argument("recording", nargs="+", help="one wav per take")
    r.add_argument("--sweep-json", help="sidecar from `gen`")
    r.add_argument("--samples", type=int, default=DEFAULT_N)
    r.add_argument("--fs", type=int, default=DEFAULT_FS)
    r.add_argument("--f-start", type=float, default=DEFAULT_F_START)
    r.add_argument("--f-end", type=float, default=DEFAULT_F_END)
    r.add_argument("--channel", type=int, default=0)
    r.add_argument("--cal", help="mic calibration file (miniDSP format)")
    r.add_argument("--smoothing", type=int, default=6,
                   help="1/N octave (0 = off)")
    r.add_argument("--device")
    r.add_argument("--rig")
    r.add_argument("--mic")
    r.set_defaults(func=_cli_process)

    a = p.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
