"""Shared DSP helpers for per-device-eq audit tools.

RBJ cookbook biquads matching PipeWire's module-filter-chain bq_* nodes.
Profile JSON format:
{
  "preamp": 0.0,
  "channels": {
    "FL": [{"type": "LSC", "freq": 50, "gain": 3.0, "q": 2.0, "on": true}, ...],
    "FR": [...]
  }
}
"""
import json
import numpy as np
from scipy import signal as sg


def _peaking(f0, gain, q, fs):
    A = 10 ** (gain / 40)
    w = 2 * np.pi * f0 / fs
    al = np.sin(w) / (2 * q)
    b = np.array([1 + al * A, -2 * np.cos(w), 1 - al * A])
    a = np.array([1 + al / A, -2 * np.cos(w), 1 - al / A])
    return b / a[0], a / a[0]


def _shelf(f0, gain, q, fs, high):
    A = 10 ** (gain / 40)
    w = 2 * np.pi * f0 / fs
    al = np.sin(w) / 2 * np.sqrt((A + 1 / A) * (1 / q - 1) + 2)
    c = np.cos(w)
    s2A = 2 * np.sqrt(A) * al
    sgn = -1 if high else 1
    b = np.array([
        A * ((A + 1) - sgn * (A - 1) * c + s2A),
        sgn * 2 * A * ((A - 1) - sgn * (A + 1) * c),
        A * ((A + 1) - sgn * (A - 1) * c - s2A),
    ])
    a = np.array([
        (A + 1) + sgn * (A - 1) * c + s2A,
        sgn * -2 * ((A - 1) + sgn * (A + 1) * c),
        (A + 1) + sgn * (A - 1) * c - s2A,
    ])
    return b / a[0], a / a[0]


def band_coeffs(band, fs):
    t = band["type"].upper()
    f0, g, q = band["freq"], band["gain"], band["q"]
    if t == "PK":
        return _peaking(f0, g, q, fs)
    if t == "LSC":
        return _shelf(f0, g, q, fs, high=False)
    if t == "HSC":
        return _shelf(f0, g, q, fs, high=True)
    raise ValueError(f"unknown band type {t!r}")


def apply_chain(x, bands, fs, preamp_db=0.0):
    """Run one channel through the active bands (+ preamp), return output."""
    y = x * 10 ** (preamp_db / 20)
    for band in bands:
        if not band.get("on", True):
            continue
        b, a = band_coeffs(band, fs)
        y = sg.lfilter(b, a, y)
    return y


def chain_curve(bands, fs, freqs):
    """Total magnitude response (dB) of the active bands at `freqs`."""
    H = np.ones_like(freqs, dtype=complex)
    for band in bands:
        if not band.get("on", True):
            continue
        b, a = band_coeffs(band, fs)
        _, h = sg.freqz(b, a, worN=2 * np.pi * np.asarray(freqs) / fs)
        H *= h
    return 20 * np.log10(np.abs(H))


def load_profile(path):
    with open(path) as f:
        return json.load(f)


def dbfs(x):
    p = float(np.max(np.abs(x)))
    return 20 * np.log10(p) if p > 0 else float("-inf")


def clip_stats(x, thr=1.0):
    over = np.abs(x) >= thr
    n = int(over.sum())
    runs = 0
    if n:
        d = np.diff(over.astype(int))
        runs = int((d == 1).sum()) + int(over[0])
    return n, 100.0 * n / len(x), runs


# The FL/FR chains from the July 2026 investigation — used by --demo and tests.
DEMO_PROFILE = {
    "preamp": 0.0,
    "channels": {
        "FL": [
            {"type": "LSC", "freq": 50, "gain": 3.0, "q": 2.00, "on": True},
            {"type": "PK", "freq": 161, "gain": 0.8, "q": 7.46, "on": True},
            {"type": "PK", "freq": 174, "gain": 0.5, "q": 7.03, "on": True},
            {"type": "PK", "freq": 200, "gain": 9.6, "q": 2.25, "on": True},
            {"type": "PK", "freq": 554, "gain": 1.6, "q": 2.88, "on": True},
            {"type": "PK", "freq": 2041, "gain": -5.6, "q": 1.48, "on": True},
            {"type": "PK", "freq": 2781, "gain": -9.7, "q": 1.32, "on": True},
        ],
        "FR": [
            {"type": "LSC", "freq": 50, "gain": 3.0, "q": 2.00, "on": True},
            {"type": "PK", "freq": 73, "gain": -3.4, "q": 2.01, "on": True},
            {"type": "PK", "freq": 105, "gain": -3.3, "q": 2.00, "on": True},
            {"type": "PK", "freq": 196, "gain": 9.7, "q": 2.32, "on": True},
            {"type": "PK", "freq": 673, "gain": 8.2, "q": 1.14, "on": True},
            {"type": "PK", "freq": 887, "gain": 16.1, "q": 1.36, "on": True},
            {"type": "PK", "freq": 890, "gain": -19.9, "q": 1.00, "on": True},
            {"type": "PK", "freq": 2073, "gain": -6.5, "q": 1.43, "on": True},
            {"type": "PK", "freq": 2595, "gain": -5.7, "q": 2.48, "on": True},
        ],
    },
}
