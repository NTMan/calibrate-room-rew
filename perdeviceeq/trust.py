# -*- coding: utf-8 -*-
"""Trust: what the canvas actually certifies, computed at open time.

Nothing here is stored. A score written into the file would go stale
the moment a take is deleted or a cal swapped, so the profile view
recomputes on every open, from the canvas alone. Two answers:

The CONTROLLED BAND: where the measurement can back the EQ. Per
frequency the judged quantity is the upper-confidence bound on the
take-to-take spread (spread_trust_bound -- the session's own
statistic, imported, not reimplemented), gated at SPREAD_MAX_DB,
edges found by the same at-least-1/6-octave-run scan the wizard
uses (trusted_band_hz), then intersected with the sweep coverage of
the takes that fed the average. Per channel and for the profile as
a whole (channels combined by max, exactly like the live session).

The SCORE: 0..100. The clean-take count sets the base --
repeatability is earned, not assumed -- degraded multiplicatively by
the median in-band spread, the worst take SNR, the age of the newest
take and, when the stored fit reaches past the controlled band, by
the uncovered fraction of the fit range. Every deduction lands in
`reasons` as text the view can show verbatim. The constants below
are starting points: tune freely, nothing persists.
"""
import math
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np

from . import measure_core as mc
from . import measure_session as ms
from . import refit

BASE_BY_CLEAN = {0: 25, 1: 45, 2: 70}   # >= 3 clean takes -> 100
SPREAD_GOOD_DB = 0.5     # median spread at or under this: no cost
SPREAD_BAD_DB = 2.0      # this or worse: the full spread penalty
SPREAD_MIN_FACTOR = 0.6
SNR_SPAN_DB = 15.0       # full SNR penalty this far under the warn
SNR_MIN_FACTOR = 0.5
AGE_FRESH_DAYS = 90.0    # younger than this: no cost
AGE_STALE_DAYS = 730.0   # this old or older: the full age penalty
AGE_MIN_FACTOR = 0.8
FIT_COVER_MIN_FACTOR = 0.5


def _linear_factor(x, good, bad, floor):
    """1.0 at `good` or better, `floor` at `bad` or worse, linear in
    between; works in either direction; None is costless."""
    if x is None or bad == good:
        return 1.0
    t = (x - good) / (bad - good)
    t = min(1.0, max(0.0, t))
    return 1.0 - t * (1.0 - floor)


def _grade(t):
    """take_quality over a canvas take dict: the same single source
    of truth, fed through an attribute shim."""
    return ms.take_quality(SimpleNamespace(
        clipped=t.get("clipped"),
        peak_dbfs=(t.get("peak_dbfs")
                   if t.get("peak_dbfs") is not None else -200.0),
        snr_db=t.get("snr_db")))


def _coverage(takes, sessions):
    """(lo, hi) the sweeps of these takes actually excited: the
    intersection across takes; (None, None) when unknown."""
    los, his = [], []
    for t in takes:
        sw = ((sessions.get(t.get("session")) or {}).get("sweep")
              or {})
        if sw.get("f_start") is not None:
            los.append(float(sw["f_start"]))
        if sw.get("f_end") is not None:
            his.append(float(sw["f_end"]))
    return ((max(los) if los else None),
            (min(his) if his else None))


def _newest(takes, now):
    """(age_days, iso) of the newest take timestamp, or (None, None)."""
    best = None
    for t in takes:
        s = t.get("created_utc")
        if not s:
            continue
        try:
            d = datetime.fromisoformat(s)
        except ValueError:
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        if best is None or d > best:
            best = d
    if best is None:
        return None, None
    age = (now - best).total_seconds() / 86400.0
    return age, best.isoformat(timespec="seconds")


def _clip_band(floor, ceiling, cov_lo, cov_hi):
    lo = floor if cov_lo is None else max(floor, cov_lo)
    hi = ceiling if cov_hi is None else min(ceiling, cov_hi)
    if lo >= hi:
        return None
    return (round(lo, 1), round(hi, 1))


def _fit_cover(band, fitp):
    """(factor, reason_fraction): how much of the stored fit range
    the controlled band actually covers, in octaves."""
    f_lo, f_hi = fitp.get("f_lo"), fitp.get("f_hi")
    if band is None or not f_lo or not f_hi or f_hi <= f_lo:
        return 1.0, None
    lo, hi = max(band[0], f_lo), min(band[1], f_hi)
    total = math.log2(f_hi / f_lo)
    inside = math.log2(hi / lo) if hi > lo else 0.0
    frac = min(1.0, max(0.0, inside / total))
    if frac >= 0.999:
        return 1.0, None
    return (FIT_COVER_MIN_FACTOR
            + (1.0 - FIT_COVER_MIN_FACTOR) * frac), frac


def assess(prof, now=None, thresh=ms.SPREAD_MAX_DB):
    """Trust report for a v3 profile, or None when it has no canvas.

    Returns {"score", "band", "spread_max_db", "reasons",
    "newest_utc", "channels"}; channels maps a key to its own
    {"score", "band", "coverage", "n_takes", "n_clean", "n_flagged",
    "n_clipped", "spread_median_db", "snr_min_db", "age_days",
    "reasons"}. `band` is None when the statistics cannot certify
    one (fewer than two takes, or the spread bound never holds a
    1/6-octave run inside the coverage). A canvas that cannot even
    be reconstructed (off-grid takes, mixed cals) scores 0 with the
    reconstruction error as the reason. `now` is injectable for
    tests; the spread gate defaults to the session's SPREAD_MAX_DB."""
    m = prof.get("measurement")
    if not isinstance(m, dict) or not m:
        return None
    now = now or datetime.now(timezone.utc)
    takes = m.get("takes") or []
    if not takes:
        return {"score": 0, "band": None, "spread_max_db": thresh,
                "reasons": ["the canvas has no takes"],
                "channels": {}, "newest_utc": None}
    try:
        results, _ = refit.channel_results(m)
    except refit.RefitError as e:
        return {"score": 0, "band": None, "spread_max_db": thresh,
                "reasons": [str(e)], "channels": {},
                "newest_utc": None}

    sessions = m.get("sessions") or {}
    fitp = (prof.get("fit") or {}).get("params") or {}
    channels, order = {}, []
    combined, freqs = None, None
    newest_all = None
    for key, r in results.items():
        d = r["data"]
        freqs = np.asarray(d["freq_hz"], float)
        ch_takes = [t for t in takes if t.get("channel") == key]
        n = len(ch_takes)
        grades = [_grade(t) for t in ch_takes]
        n_clean = sum(g == ms.TAKE_CLEAN for g in grades)
        n_flag = sum(g == ms.TAKE_FLAGGED for g in grades)
        n_clip = sum(g == ms.TAKE_CLIPPED for g in grades)
        cov_lo, cov_hi = _coverage(ch_takes, sessions)
        sp = d.get("spread_db")
        band = None
        if sp is not None and n >= 2:
            bound = ms.spread_trust_bound(sp, n)
            combined = (bound if combined is None
                        else np.maximum(combined, bound))
            floor, ceiling = ms.trusted_band_hz(freqs,
                                                bound <= thresh)
            band = _clip_band(floor, ceiling, cov_lo, cov_hi)

        reasons = []
        base = 100 if n_clean >= 3 else BASE_BY_CLEAN[n_clean]
        if n_clean < 3:
            extra = ""
            if n_flag or n_clip:
                extra = (" (%d flagged, %d clipped)"
                         % (n_flag, n_clip))
            reasons.append("%d clean take(s)%s; three make the "
                           "statistics" % (n_clean, extra))
        if n < 2:
            reasons.append("fewer than two takes: no repeatability, "
                           "no controlled band")
        elif band is None:
            reasons.append("the spread bound never holds %.1f dB "
                           "over a 1/6-octave run inside the "
                           "coverage" % thresh)

        med = None
        if sp is not None:
            sel = np.ones(len(freqs), bool)
            lo, hi = band if band else (cov_lo, cov_hi)
            if lo is not None:
                sel &= freqs >= lo
            if hi is not None:
                sel &= freqs <= hi
            if sel.any():
                med = float(np.median(np.asarray(sp)[sel]))
        f_spread = _linear_factor(med, SPREAD_GOOD_DB,
                                  SPREAD_BAD_DB, SPREAD_MIN_FACTOR)
        if f_spread < 1.0:
            reasons.append("median in-band spread %.2f dB" % med)

        known = [t.get("snr_db") for t in ch_takes
                 if t.get("snr_db") is not None]
        snr_min = min(known) if known else None
        f_snr = _linear_factor(snr_min, mc.SNR_WARN_DB,
                               mc.SNR_WARN_DB - SNR_SPAN_DB,
                               SNR_MIN_FACTOR)
        if f_snr < 1.0:
            reasons.append("worst take SNR %.1f dB (warn at %g)"
                           % (snr_min, mc.SNR_WARN_DB))

        age, newest = _newest(ch_takes, now)
        if newest is not None and (newest_all is None
                                   or newest > newest_all):
            newest_all = newest
        f_age = _linear_factor(age, AGE_FRESH_DAYS, AGE_STALE_DAYS,
                               AGE_MIN_FACTOR)
        if f_age < 1.0:
            reasons.append("newest take is %d days old" % age)

        f_fit, frac = _fit_cover(band, fitp)
        if frac is not None:
            reasons.append("the fit range %g-%g Hz reaches past the "
                           "controlled band"
                           % (fitp.get("f_lo"), fitp.get("f_hi")))

        score = int(round(base * f_spread * f_snr * f_age * f_fit))
        channels[key] = {
            "score": max(0, min(100, score)), "band": band,
            "coverage": ((cov_lo, cov_hi)
                         if cov_lo is not None and cov_hi is not None
                         else None),
            "n_takes": n, "n_clean": n_clean, "n_flagged": n_flag,
            "n_clipped": n_clip,
            "spread_median_db": (round(med, 2)
                                 if med is not None else None),
            "snr_min_db": (round(snr_min, 1)
                           if snr_min is not None else None),
            "age_days": round(age, 1) if age is not None else None,
            "reasons": reasons,
        }
        order.append(key)

    band = None
    if combined is not None:
        floor, ceiling = ms.trusted_band_hz(freqs, combined <= thresh)
        cov_lo, cov_hi = _coverage(takes, sessions)
        band = _clip_band(floor, ceiling, cov_lo, cov_hi)
    score = min(c["score"] for c in channels.values())
    order.sort(key=lambda k: channels[k]["score"])
    reasons = []
    for key in order:
        for r in channels[key]["reasons"]:
            line = "%s: %s" % (key, r)
            if line not in reasons:
                reasons.append(line)
    return {"score": score, "band": band, "spread_max_db": thresh,
            "reasons": reasons, "newest_utc": newest_all,
            "channels": channels}
