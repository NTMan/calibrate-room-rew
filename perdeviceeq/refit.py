# -*- coding: utf-8 -*-
"""Rebuild the fit from the canvas alone. No GTK, no session.

A v3 profile's `fit` is a cache over its `measurement`: the bands were
derived from the stored takes, the embedded cal and the fit
parameters, and the fingerprint in fit.inputs_sha256 says from exactly
which. This module makes the cache recomputable: channel_results()
reconstructs the per-channel result dicts fit_peq consumes -- the same
align / power-average / cal / smooth pipeline process_takes runs on
fresh captures, minus the time domain -- and refit_profile() re-runs
the fit over them, grafting a new playback body and a new `fit` block
onto the profile without touching the canvas. fit_is_stale() is the
check a profile view runs on open: recompute the fingerprint over the
stored take ids + params and compare.

Hand edits are respected: a fit marked `edited` refuses to be thrown
away unless the caller says so explicitly (the GUI turns that into a
confirmation dialog).
"""
import math
from datetime import datetime, timezone

import numpy as np

from . import fit_peq
from . import measure_core as mc
from .measure_build import FIT_ALGO, fit_fingerprint
from .measure_session import gain_comp_factors
from .profiles import playback_sha256


class RefitError(RuntimeError):
    """The canvas cannot support the requested re-fit."""


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")



def channel_results(measurement, take_ids=None, smoothing=6):
    """Canvas -> {channel key: result dict}, plus the take ids used.

    Mirrors the live pipeline: each channel's takes are aligned DOWN
    onto the quietest recorded software gain (the session scales the
    raw samples by gain_comp_factors; in the magnitude domain that is
    an exact dB shift), corrected PER TAKE by each take's own cal
    from measurement.cal_library (schema v4: the statistics must
    judge calibrated curves -- uncalibrated ones from different mics
    differ by the mics' own responses, not by the seating), then
    power-averaged and box-smoothed on the stored grid. The result
    dicts carry data.freq_hz / mag_db_* / spread_db, the per-take
    drives under `levels` and the takes' cal shas as `cal_shas`, so
    fit_peq's balance trims keep their validity gate: channels
    measured on distinct couplers still refuse to cross-balance.

    take_ids, when given, restricts the fit to those takes (unknown
    ids are an error); default is every take on the canvas."""
    grid = measurement.get("grid") or {}
    ppo = int(grid.get("ppo", mc.GRID_PPO))
    freqs = mc.log_grid(float(grid.get("f_lo", mc.GRID_F_LO)),
                        float(grid.get("f_hi", mc.GRID_F_HI)), ppo)
    all_takes = measurement.get("takes") or []
    if take_ids is not None:
        wanted = set(take_ids)
        sel = [t for t in all_takes if t.get("id") in wanted]
        missing = wanted - {t.get("id") for t in sel}
        if missing:
            raise RefitError("unknown take id(s): %s"
                             % ", ".join(sorted(missing)))
    else:
        sel = list(all_takes)
    if not sel:
        raise RefitError("the canvas has no takes to fit")

    lib = measurement.get("cal_library") or {}
    by_ch = {}
    for t in sel:
        by_ch.setdefault(t.get("channel"), []).append(t)

    results = {}
    for key, takes in by_ch.items():
        for t in takes:
            if len(t.get("mag_db_uncal") or []) != len(freqs):
                raise RefitError("take %s of %s is not on the "
                                 "profile grid" % (t.get("id"), key))
        mags = np.array([t["mag_db_uncal"] for t in takes], float)
        factors = gain_comp_factors([t.get("soft_vol")
                                     for t in takes])
        comp_db = None
        if factors is not None:
            comp_db = [round(20.0 * math.log10(k), 3)
                       for k in factors]
            mags = mags + np.array(
                [20.0 * math.log10(k) for k in factors])[:, None]
        uncal = 10.0 * np.log10(np.mean(10.0 ** (mags / 10.0),
                                        axis=0))
        # per-take cal BEFORE the statistics: the spread judges
        # the seating only when every curve is on its own
        # acoustic reference
        for i, t in enumerate(takes):
            pts = np.asarray(((lib.get(t.get("cal_sha")) or {})
                              .get("points") or []), float)
            if pts.size:
                mags[i] = mc.apply_mic_cal(freqs, mags[i],
                                           pts[:, 0], pts[:, 1])
        avg = 10.0 * np.log10(np.mean(10.0 ** (mags / 10.0), axis=0))
        spread = (mags.std(axis=0, ddof=1) if len(takes) > 1
                  else None)
        smoothed = mc.smooth_fractional_octave(avg, ppo, smoothing)
        results[key] = {
            "grid": dict(grid),
            "smoothing": {"type": "fractional-octave",
                          "fraction": smoothing, "domain": "power"},
            "cal_shas": [t.get("cal_sha") for t in takes],
            "takes": {"count": len(takes),
                      "snr_db": [t.get("snr_db") for t in takes],
                      "delay_ms": [t.get("delay_ms")
                                   for t in takes]},
            "levels": {
                "take_soft_volumes": [t.get("soft_vol")
                                      for t in takes],
                "take_channel_volumes": [t.get("chan_vol")
                                         for t in takes],
                "gain_comp_db": comp_db},
            "data": {"freq_hz": freqs, "mag_db_raw": avg,
                     "mag_db_smoothed": smoothed,
                     "mag_db_uncal": uncal, "spread_db": spread},
        }
    return results, [t.get("id") for t in sel]


def refit_profile(prof, bands=None, f_lo=None, f_hi=None,
                  max_boost=None, smoothing=None, take_ids=None,
                  allow_edited=False, progress=None):
    """Re-derive the playback body from the profile's own canvas.

    Fit parameters default to the stored fit.params (falling back to
    the build defaults for a canvas that never had a fit); any of
    them can be overridden. Returns a NEW profile dict: same id, name
    and canvas, new bands/ch_keys/apply_all; the preamp is NOT
    touched -- the mode governs it (Auto lands the composed Safe,
    manual is protected by the session clamp)
    exactly like a fresh fit (the bands changed, the app re-derives
    Safe/Session), and a new `fit` block with a fresh timestamp, the
    take ids actually consumed and a recomputed inputs_sha256.

    Raises RefitError when the profile has no canvas, when the fit is
    marked hand-edited and allow_edited is False, or when the takes
    cannot be combined (unknown ids, off-grid data, a mono fit
    over several channels)."""
    m = prof.get("measurement")
    if not m:
        raise RefitError("the profile carries no measurement canvas")
    fit = prof.get("fit") or {}
    if fit.get("edited") and not allow_edited:
        raise RefitError("the bands were edited by hand after the "
                         "fit; re-fitting discards those edits "
                         "(pass allow_edited=True to proceed)")
    old = fit.get("params") or {}
    params = {"bands": int(bands if bands is not None
                           else old.get("bands", 10)),
              "f_lo": float(f_lo if f_lo is not None
                            else old.get("f_lo", 20.0)),
              "f_hi": float(f_hi if f_hi is not None
                            else old.get("f_hi", 12000.0)),
              "max_boost": float(max_boost if max_boost is not None
                                 else old.get("max_boost", 6.0)),
              "smoothing": (smoothing if smoothing is not None
                            else old.get("smoothing", 6)),
              "mono": bool(old.get("mono", False))}
    results, used = channel_results(m, take_ids=take_ids,
                                    smoothing=params["smoothing"])
    if params["mono"] and len(results) != 1:
        raise RefitError("a mono fit needs exactly one channel; the "
                         "canvas has %d" % len(results))
    fitted = fit_peq.fit_profiles(results, name=prof.get("name"),
                                  bands=params["bands"],
                                  f_lo=params["f_lo"],
                                  f_hi=params["f_hi"],
                                  max_boost=params["max_boost"],
                                  mono=params["mono"],
                                  progress=progress)
    out = dict(prof)
    for k in ("apply_all", "ch_keys", "all", "channels"):
        out[k] = fitted[k]

    # The fit lands BANDS ONLY. Its old gain staging predated the
    # preamp mode and knew nothing of the taste layer: in Auto the
    # composed Safe (device + taste) lands right after the reload,
    # and a manual compromise is the user's to keep -- the session
    # clamp protects it either way.
    out["fit"] = {"at": _utc_now(), "algo": FIT_ALGO,
                  "params": params, "target": {"kind": "flat"},
                  "takes": list(used),
                  "inputs_sha256": fit_fingerprint(m, used, params),
                  "edited": False}
    # The zone rides the fit. The floor's source is the MEASURED
    # controlled band, not the hand-draggable fit range -- the
    # architect's own field caught the difference (his stored
    # f_lo was the 20.0 default while the trust header said
    # 38.3). The trust doctrine keeps the SCORE unstored, but
    # the fit block is the house's derived-but-stored pattern
    # and inputs_sha256 already staleness-guards it; a re-fit
    # re-mints the zone from the same takes it consumed. The
    # import is function-level to sidestep the trust->refit
    # module cycle.
    from . import trust
    rep_ = trust.assess(out)
    band = rep_.get("band") if isinstance(rep_, dict) else None
    out["fit"]["zone"] = ({"lo": float(band[0]),
                           "hi": float(band[1])}
                          if band else None)
    out["fit"]["output_sha256"] = playback_sha256(out)
    return out


def fit_is_stale(prof):
    """True when the stored fit's fingerprint no longer matches the
    canvas it claims to come from (takes removed or reweighed, cal
    swapped, parameters edited). A profile missing either block is
    never 'stale' -- there is nothing to compare."""
    m, fit = prof.get("measurement"), prof.get("fit")
    if not m or not fit or not fit.get("inputs_sha256"):
        return False
    return (fit_fingerprint(m, fit.get("takes") or [],
                            fit.get("params") or {})
            != fit["inputs_sha256"])
