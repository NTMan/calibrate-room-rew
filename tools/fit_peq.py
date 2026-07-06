#!/usr/bin/env python3
"""ROADMAP Task 3, increment 3: fit a parametric EQ to a measurement.

Reads one or two measure_run result.json files (the core has already
averaged the takes into data.mag_db_smoothed), fits up to --bands biquads
per channel to a FLAT target (the HEQ intent: a neutral headphone measures
flat) over a trusted band, and writes a per-device-eq v2 profile, per
channel, ready for the app's "Import profile...".

Filters are evaluated with the SAME biquad transfer function the app and
PipeWire use (perdeviceeq.eq.biquad), so the fitted curve is what will
actually be heard. Preamp is left at 0.0 -- the app derives Safe/Session
headroom itself. The fit is greedy (place a band at the largest residual,
peaking in the middle, low/high shelf at the edges) with a joint
least-squares refine of every band after each placement; it stops early
once the residual is within 0.5 dB everywhere in band.
"""
import argparse
import datetime
import json
import os
import sys

import numpy as np
from scipy.optimize import least_squares

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from perdeviceeq import eq                       # noqa: E402
from perdeviceeq.config import FS                # noqa: E402

RESID_TARGET_DB = 0.5
GRID = 400


def _mag_db_vec(btype, f0, gain, q, freqs):
    """Magnitude (dB) of one biquad across freqs -- coeffs from the app's
    exact eq.biquad, only the frequency evaluation vectorized."""
    b0, b1, b2, a0, a1, a2 = eq.biquad(btype, f0, gain, q, FS)
    w = 2 * np.pi * freqs / FS
    z1, z2 = np.exp(-1j * w), np.exp(-2j * w)
    h = (b0 + b1 * z1 + b2 * z2) / (a0 + a1 * z1 + a2 * z2)
    return 20 * np.log10(np.maximum(np.abs(h), 1e-12))


def _response(bands, freqs):
    out = np.zeros_like(freqs)
    for t, f, g, q in bands:
        out += _mag_db_vec(t, f, g, q, freqs)
    return out


def _refine(bands, fg, desired, flo, fhi, max_boost):
    types = [b[0] for b in bands]
    x0, lo, hi = [], [], []
    for _, f, g, q in bands:
        x0 += [np.log10(f), g, q]
        lo += [np.log10(flo), -24.0, 0.3]
        hi += [np.log10(fhi), max_boost, 8.0]

    def resfun(x):
        bl = [(types[i], 10 ** x[3 * i], x[3 * i + 1], x[3 * i + 2])
              for i in range(len(types))]
        return _response(bl, fg) - desired

    sol = least_squares(resfun, x0, bounds=(lo, hi), method="trf",
                        max_nfev=3000)
    return [(types[i], float(10 ** sol.x[3 * i]),
             float(np.clip(sol.x[3 * i + 1], -24.0, max_boost)),
             float(sol.x[3 * i + 2])) for i in range(len(types))]


def fit_channel(freq, mag, flo, fhi, n_bands, max_boost):
    """Return (bands, fg, desired, resid). bands: list of (type,f,g,q).
    Cuts are unbounded; boost is capped at max_boost (filling deep nulls
    wastes headroom and amplifies noise), so the fit targets the desired
    correction clipped from above, while the residual is reported against
    the true (uncapped) target so unfillable dips stay visible."""
    fg = np.logspace(np.log10(flo), np.log10(fhi), GRID)
    yg = np.interp(np.log10(fg), np.log10(freq), mag)
    desired = yg.mean() - yg                       # flat target correction
    target = np.minimum(desired, max_boost)        # never ask beyond the cap
    bands = []
    for _ in range(n_bands):
        resid = target - _response(bands, fg)
        k = int(np.argmax(np.abs(resid)))
        if abs(resid[k]) < RESID_TARGET_DB:
            break
        f0 = fg[k]
        btype = "LSC" if f0 <= flo * 2 else "HSC" if f0 >= fhi / 2 else "PK"
        g0 = float(np.clip(resid[k], -24.0, max_boost))
        bands.append((btype, f0, g0, 2.0))
        bands = _refine(bands, fg, target, flo, fhi, max_boost)
    resid = desired - _response(bands, fg)         # vs TRUE target
    return bands, fg, desired, resid


def _bands_to_dicts(bands):
    return [{"type": t, "freq": round(f, 1), "gain": round(g, 2),
             "q": round(q, 3), "enabled": True} for t, f, g, q in bands]


def _report(tag, bands, fg, resid, flo, fhi):
    print("\n[%s] %d bands, fit %g-%g Hz" % (tag, len(bands), flo, fhi))
    for t, f, g, q in sorted(bands, key=lambda b: b[1]):
        print("  %-3s  %8.1f Hz  %+6.2f dB  Q %5.2f" % (t, f, g, q))
    grid = np.concatenate([fg, np.array([b[1] for b in bands] or [flo])])
    peak = float(np.max(_response(bands, grid)))
    print("  residual: RMS %.2f dB, max %.2f dB   safe preamp ~ %+.1f dB"
          % (float(np.sqrt(np.mean(resid ** 2))),
             float(np.max(np.abs(resid))), -max(peak, 0.0)))
    hi = (fg >= 8000) & (fg <= fhi)
    if hi.any():
        print("  residual 8-%g kHz: max %.2f dB (edge of cal trust)"
              % (fhi / 1000, float(np.max(np.abs(resid[hi])))))


def _load(path):
    r = json.load(open(path))
    d = r["data"]
    return np.asarray(d["freq_hz"], float), np.asarray(d["mag_db_smoothed"],
                                                       float)


def main(argv):
    p = argparse.ArgumentParser(description="fit a PEQ profile to a "
                                            "measurement")
    p.add_argument("--left", help="result.json for the left channel (FL)")
    p.add_argument("--right", help="result.json for the right channel (FR)")
    p.add_argument("--mono", help="result.json applied to all channels")
    p.add_argument("--bands", type=int, default=10,
                   help="max biquads per channel (default 10)")
    p.add_argument("--f-lo", type=float, default=20.0)
    p.add_argument("--f-hi", type=float, default=12000.0)
    p.add_argument("--max-boost", type=float, default=6.0,
                   help="cap positive gain (dB); cuts are "
                        "unbounded (default 6)")
    p.add_argument("--name", help="profile name (default from --device/date)")
    p.add_argument("--out", required=True, help="profile JSON to write")
    a = p.parse_args(argv)

    if not (a.left or a.right or a.mono):
        p.error("give --left/--right or --mono")
    name = a.name or "Measured %s" % datetime.date.today().isoformat()
    prof = {"name": name, "version": 2, "preamp": 0.0,
            "all": {"bands": []}, "channels": {}, "ch_keys": []}

    if a.mono:
        freq, mag = _load(a.mono)
        bands, fg, desired, resid = fit_channel(freq, mag, a.f_lo, a.f_hi,
                                                a.bands, a.max_boost)
        _report("all", bands, fg, resid, a.f_lo, a.f_hi)
        prof["apply_all"] = True
        prof["all"] = {"bands": _bands_to_dicts(bands)}
    else:
        prof["apply_all"] = False
        prof["ch_keys"] = ["FL", "FR"]
        for key, path in (("FL", a.left), ("FR", a.right)):
            if not path:
                continue
            freq, mag = _load(path)
            bands, fg, desired, resid = fit_channel(freq, mag, a.f_lo,
                                                    a.f_hi, a.bands,
                                                    a.max_boost)
            _report(key, bands, fg, resid, a.f_lo, a.f_hi)
            prof["channels"][key] = {"bands": _bands_to_dicts(bands)}

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(prof, f, indent=2, ensure_ascii=False)
    print("\nwrote %s (%r) -- import via the app's \"Import profile...\""
          % (a.out, name))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
