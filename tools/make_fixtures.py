#!/usr/bin/env python3
"""Generate deterministic test fixtures for per-device-eq audit tests.

Real captures of commercial tracks cannot live in a public repo, so the
fixtures are synthesized with the same *properties* that mattered in the
July 2026 investigation:

  clean_master.wav  — EDM-ish material limited to a -4.3 dBFS ceiling
                      (stands in for a sanely mastered library).
  hot_master.wav    — same material driven into a hard clipper at 44.1 kHz,
                      then resampled to 48 kHz: the resampling materializes
                      intersample overshoot above 0 dBFS, exactly like a
                      brickwalled commercial master through any honest
                      resampler.

Both are stereo float32 @ 48 kHz. Deterministic (fixed seed): the numbers
printed at the end are stable and can be hardcoded in tests.

Usage:
    make_fixtures.py [outdir]        # default: tests/fixtures
"""
import os
import sys
import numpy as np
from scipy import signal as sg
import soundfile as sf

from pde_audit import dbfs

FS_SRC = 44100
FS_OUT = 48000
DUR = 10.0
SEED = 20260704


def synth(rng):
    n = int(DUR * FS_SRC)
    t = np.arange(n) / FS_SRC
    x = np.zeros((n, 2))

    # kick: 130 BPM, 55 Hz exponentially decaying sine, slightly L-heavy
    beat = 60 / 130
    for k in np.arange(0, DUR, beat):
        i = int(k * FS_SRC)
        seg = np.arange(min(n - i, int(0.25 * FS_SRC)))
        burst = np.sin(2 * np.pi * 55 * seg / FS_SRC) * np.exp(-seg / (0.06 * FS_SRC))
        x[i:i + len(seg), 0] += 1.00 * burst
        x[i:i + len(seg), 1] += 0.82 * burst

    # offbeat 200 Hz bass stabs (energy where the FL demo profile boosts)
    for k in np.arange(beat / 2, DUR, beat):
        i = int(k * FS_SRC)
        seg = np.arange(min(n - i, int(0.15 * FS_SRC)))
        stab = np.sin(2 * np.pi * 200 * seg / FS_SRC) * np.exp(-seg / (0.05 * FS_SRC))
        x[i:i + len(seg), 0] += 0.45 * stab
        x[i:i + len(seg), 1] += 0.55 * stab

    # pink-ish noise bed + hats
    white = rng.standard_normal((n, 2))
    b, a = sg.butter(1, 800 / (FS_SRC / 2))
    bed = sg.lfilter(b, a, white, axis=0)
    x += 0.15 * bed / np.max(np.abs(bed))
    hat_env = (np.sin(2 * np.pi * t / beat * 2) > 0.95).astype(float)
    hats = rng.standard_normal((n, 2))
    bh, ah = sg.butter(2, 6000 / (FS_SRC / 2), "highpass")
    x += 0.10 * sg.lfilter(bh, ah, hats, axis=0) * hat_env[:, None]
    return x


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures"
    os.makedirs(outdir, exist_ok=True)
    rng = np.random.default_rng(SEED)
    x = synth(rng)

    # clean: peak-limit to -4.3 dBFS ceiling, resample to graph rate
    clean = x / np.max(np.abs(x)) * 10 ** (-4.3 / 20)
    clean48 = sg.resample_poly(clean, 160, 147, axis=0).astype(np.float32)
    sf.write(f"{outdir}/clean_master.wav", clean48, FS_OUT, subtype="FLOAT")

    # hot: drive into hard clipper at source rate, then resample
    hot = np.clip(x / np.max(np.abs(x)) * 10 ** (6 / 20), -0.998, 0.998)
    hot48 = sg.resample_poly(hot, 160, 147, axis=0).astype(np.float32)
    sf.write(f"{outdir}/hot_master.wav", hot48, FS_OUT, subtype="FLOAT")

    for name, y in [("clean_master", clean48), ("hot_master", hot48)]:
        pks = " / ".join(f"{dbfs(y[:, c]):+.2f}" for c in range(2))
        print(f"{name}.wav: peaks L/R {pks} dBFS")


if __name__ == "__main__":
    main()
