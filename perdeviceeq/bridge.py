"""Frame bridge: the measured delta between two rigs.

Feed it two profiles of ONE reference device (the same IEM, deeply
and repeatably seated) measured on two different rigs, and it
returns D(f) = A - B on a common grid, with a trusted band derived
from the two rigs' take-to-take spreads. D is the empirical
composite of everything that differs between the frames: coupler
physics, capsule, and whatever calibration file each rig loaded.

The degeneracy, stated once: a pair of OUR rigs cannot separate the
coupler's physics from the vendor's cal file -- they ride the delta
as one sum. Judging the vendor file needs an external anchor
(``published``: a trusted measurement of the same device on a real
IEC 60318-4 rig); and splitting file from physics needs one extra
run of the clone with a flat cal. The report says which question
each number answers.

Absolute level is meaningless across rigs (different drives and
sensitivities), so every comparison is aligned to zero mean inside
ALIGN_LO..ALIGN_HI and the removed offset is reported, never
silently eaten.
"""

import hashlib
import json
import os

import numpy as np

from . import measure_core as mc
from . import refit

TRUST_SPREAD_DB = 1.5      # RSS spread above this ends the trusted band
ALIGN_LO = 200.0           # level-alignment window, Hz: high enough to
ALIGN_HI = 800.0           # dodge seal variance, low enough to dodge
                           # insertion-depth chaos

SMOOTH_FRACTION = 6        # channel_results' box smoothing, 1/6 oct

SCHEMA = 1


class BridgeError(RuntimeError):
    pass


def _mean_curves(profile):
    """Profile -> {channel: (freqs, mag_db, spread_db)} off its canvas.

    mag_db is the calibrated, box-smoothed per-channel average exactly
    as the fit sees it (refit.channel_results); spread_db is the
    take-to-take spread on the same grid.
    """
    meas = profile.get("measurement")
    if not meas or not meas.get("takes"):
        raise BridgeError("profile %r carries no measurement canvas"
                          % profile.get("name", profile.get("id")))
    results, _ = refit.channel_results(meas)
    out = {}
    for ch, r in results.items():
        d = r["data"]
        out[ch] = (np.asarray(d["freq_hz"], dtype=float),
                   np.asarray(d["mag_db_smoothed"], dtype=float),
                   np.asarray(d["spread_db"], dtype=float))
    return out


def _common_grid(meas_a, meas_b):
    """The intersection of the two canvases' grids, at the coarser
    density -- interpolation may only ever downsample."""
    def g(meas):
        gr = meas.get("grid") or {}
        return (float(gr.get("f_lo", mc.GRID_F_LO)),
                float(gr.get("f_hi", mc.GRID_F_HI)),
                int(gr.get("ppo", mc.GRID_PPO)))
    lo_a, hi_a, ppo_a = g(meas_a)
    lo_b, hi_b, ppo_b = g(meas_b)
    lo, hi = max(lo_a, lo_b), min(hi_a, hi_b)
    if not hi > lo:
        raise BridgeError("the two canvases share no frequency range")
    ppo = min(ppo_a, ppo_b)
    return mc.log_grid(lo, hi, ppo), ppo


def _resample(freqs_src, vals, freqs_dst):
    """dB curves interpolate linearly in log-f."""
    return np.interp(np.log10(freqs_dst), np.log10(freqs_src),
                     np.asarray(vals, dtype=float))


def _align_offset(freqs, vals):
    m = (freqs >= ALIGN_LO) & (freqs <= ALIGN_HI)
    if not m.any():          # a grid that misses the window entirely
        m = np.ones_like(freqs, dtype=bool)
    return float(np.mean(vals[m]))


def _erode(mask, w):
    """False spreads w points each way: the pipeline box-smooths the
    means, so a point within half a smoothing window of a spread
    violation has already borrowed from the chaos."""
    if w <= 0:
        return mask.copy()
    bad = (~mask).astype(float)
    kern = np.ones(2 * w + 1)
    return ~(np.convolve(bad, kern, mode="same") > 0.0)


def _longest_run(mask):
    """(start, end) indices of the longest True run, or None."""
    best = None
    i, n = 0, len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j + 1 < n and mask[j + 1]:
                j += 1
            if best is None or j - i > best[1] - best[0]:
                best = (i, j)
            i = j + 1
        else:
            i += 1
    return best


def parse_curve(path):
    """freq/dB text: whitespace or comma separated, '#' '*' ';'
    comments -- the dialect REW and AutoEq both emit."""
    freqs, vals = [], []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line[0] in "#*;":
                continue
            parts = line.replace(",", " ").split()
            if len(parts) < 2:
                continue
            try:
                f, v = float(parts[0]), float(parts[1])
            except ValueError:
                continue
            freqs.append(f)
            vals.append(v)
    if len(freqs) < 8:
        raise BridgeError("%s does not look like a freq/dB curve"
                          % path)
    order = np.argsort(freqs)
    return (np.asarray(freqs, dtype=float)[order],
            np.asarray(vals, dtype=float)[order])


def _rig_meta(profile):
    src = (profile.get("measurement") or {}).get("source") or {}
    cal = src.get("cal") or {}
    return {"profile_id": profile.get("id"),
            "profile_name": profile.get("name"),
            "rig": src.get("name"),
            "serial": src.get("serial"),
            "cal_files": {k: (v.get("path") if isinstance(v, dict)
                              else v)
                          for k, v in cal.items()}}


def compute_bridge(prof_a, prof_b, published=None):
    """The whole computation, pure. `published` is an optional
    (freqs, mag_db) tuple for the external-anchor audit of B."""
    curves_a = _mean_curves(prof_a)
    curves_b = _mean_curves(prof_b)
    chans = sorted(set(curves_a) & set(curves_b))
    if not chans:
        raise BridgeError("the two profiles share no channels")

    freqs, ppo = _common_grid(prof_a["measurement"],
                              prof_b["measurement"])
    guard = max(1, int(round(ppo / (2.0 * SMOOTH_FRACTION))))
    per_ch = {}
    for ch in chans:
        fa, ma, sa = curves_a[ch]
        fb, mb, sb = curves_b[ch]
        a = _resample(fa, ma, freqs)
        b = _resample(fb, mb, freqs)
        rss = np.sqrt(_resample(fa, sa, freqs) ** 2
                      + _resample(fb, sb, freqs) ** 2)
        d_raw = a - b
        off = _align_offset(freqs, d_raw)
        d = d_raw - off
        trust = rss <= TRUST_SPREAD_DB
        run = _longest_run(_erode(trust, guard))
        # the trusted BAND is the longest contiguous run: isolated
        # quiet islands beyond it are seating luck, not physics
        band = np.zeros_like(trust)
        if run:
            band[run[0]:run[1] + 1] = True
        entry = {"delta_db": d, "rss_spread_db": rss,
                 "offset_removed_db": off, "trust_mask": trust,
                 "band_mask": band,
                 "trusted_band_hz": ((float(freqs[run[0]]),
                                      float(freqs[run[1]]))
                                     if run else None)}
        if published is not None:
            pub = _resample(published[0], published[1], freqs)
            resid_raw = b - pub
            r_off = _align_offset(freqs, resid_raw)
            entry["published_residual_db"] = resid_raw - r_off
            entry["published_offset_removed_db"] = r_off
        per_ch[ch] = entry

    warnings = []
    if len(chans) >= 2:
        stack = np.stack([per_ch[c]["delta_db"] for c in chans])
        rsss = np.stack([per_ch[c]["rss_spread_db"] for c in chans])
        skew = stack.max(axis=0) - stack.min(axis=0)
        limit = rsss.max(axis=0)
        trust_all = np.all(
            np.stack([per_ch[c]["trust_mask"] for c in chans]), axis=0)
        bad = trust_all & (skew > limit)
        if bad.any():
            lo = float(freqs[np.argmax(bad)])
            hi = float(freqs[len(bad) - 1 - np.argmax(bad[::-1])])
            warnings.append(
                "channel deltas disagree beyond the combined spread "
                "between %.0f and %.0f Hz (max %.2f dB): a seating "
                "problem on one side, not a rig property"
                % (lo, hi, float(skew[bad].max())))

    return {"schema": SCHEMA, "freq_hz": freqs, "channels": per_ch,
            "a": _rig_meta(prof_a), "b": _rig_meta(prof_b),
            "warnings": warnings,
            "align_band_hz": (ALIGN_LO, ALIGN_HI),
            "trust_spread_db": TRUST_SPREAD_DB}


def _jsonable(bridge):
    out = json.loads(json.dumps(bridge, default=lambda o:
                                o.tolist() if isinstance(o, np.ndarray)
                                else bool(o) if isinstance(o, np.bool_)
                                else float(o)))
    blob = json.dumps(out, sort_keys=True).encode()
    out["content_sha256"] = hashlib.sha256(blob).hexdigest()
    return out


def _mean_delta(bridge):
    stack = np.stack([bridge["channels"][c]["delta_db"]
                      for c in sorted(bridge["channels"])])
    return stack.mean(axis=0)


def write_outputs(bridge, outdir):
    """bridge.json + delta.txt (channel-mean, REW-readable) +
    report.txt; returns the report path."""
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "bridge.json"), "w") as fh:
        json.dump(_jsonable(bridge), fh, indent=1, sort_keys=True)
        fh.write("\n")

    freqs = bridge["freq_hz"]
    mean_d = _mean_delta(bridge)
    with open(os.path.join(outdir, "delta.txt"), "w") as fh:
        fh.write("* frame bridge: A minus B, channel mean, dB\n")
        fh.write("* A=%s  B=%s\n" % (bridge["a"]["profile_name"],
                                     bridge["b"]["profile_name"]))
        for f, v in zip(freqs, mean_d):
            fh.write("%.6g %.4f\n" % (f, v))

    rp = os.path.join(outdir, "report.txt")
    with open(rp, "w") as fh:
        fh.write(_report_text(bridge))
    return rp


def _report_text(bridge):
    lines = ["frame bridge: %s (A)  vs  %s (B)"
             % (bridge["a"]["profile_name"],
                bridge["b"]["profile_name"]),
             "rigs: A=%s  B=%s" % (bridge["a"]["rig"],
                                   bridge["b"]["rig"]), ""]
    for ch in sorted(bridge["channels"]):
        e = bridge["channels"][ch]
        band = e["trusted_band_hz"]
        t = e["band_mask"]
        if band:
            d_in = np.abs(e["delta_db"][t])
            lines.append(
                "%s: trusted %.0f-%.0f Hz (%.0f%% of the grid); "
                "max |D| inside it %.2f dB; level offset removed "
                "%.2f dB"
                % (ch, band[0], band[1],
                   100.0 * t.mean(), float(d_in.max()),
                   e["offset_removed_db"]))
        else:
            lines.append("%s: NO trusted band -- the combined spread "
                         "never drops under %.1f dB; reseat and "
                         "remeasure" % (ch, bridge["trust_spread_db"]))
        if "published_residual_db" in e and band:
            r_in = np.abs(e["published_residual_db"][t])
            lines.append(
                "%s vs published: max residual %.2f dB in the "
                "trusted band (offset removed %.2f dB)"
                % (ch, float(r_in.max()),
                   e["published_offset_removed_db"]))
    lines.append("")
    for w in bridge["warnings"]:
        lines.append("WARNING: " + w)
    if bridge["warnings"]:
        lines.append("")
    lines.append(
        "reading the numbers: D is the composite of coupler physics, "
        "capsule and whatever cal file each rig loaded -- a pair of "
        "rigs cannot split those. The published residual (if any) "
        "judges the SUM of B's hardware and its cal against a real "
        "60318-4. To split the vendor file from the physics, run B "
        "once more with a flat cal and bridge that against this "
        "profile.")
    lines.append("")
    lines.append("the trusted band ends where the RSS of the two "
                 "rigs' take-to-take spreads crosses %.1f dB; above "
                 "it insertion depth owns the curve."
                 % bridge["trust_spread_db"])
    return "\n".join(lines) + "\n"


def resolve_profile(store, key):
    """pid first, then an exact (case-insensitive) unique name."""
    p = store.get(key)
    if p is not None and store.has(key):
        return p
    hits = [q for q in store.ordered()
            if q.get("name", "").lower() == key.lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise BridgeError("no profile with id or name %r" % key)
    raise BridgeError("the name %r is ambiguous: %s"
                      % (key, ", ".join(q["id"] for q in hits)))
