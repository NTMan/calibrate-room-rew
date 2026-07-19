#!/usr/bin/env python3
"""ROADMAP Task 3, increment 3: fit a parametric EQ to a measurement.

Reads one or two measure_run result.json files (the core has already
averaged the takes into data.mag_db_smoothed), fits up to --bands biquads
per channel to a FLAT target (the HEQ intent: a neutral headphone measures
flat) over a trusted band, and writes a per-device-eq profile, per
channel, ready for the app's "Import profile...".

Filters are evaluated with the SAME biquad transfer function the app and
PipeWire use (perdeviceeq.eq.biquad), so the fitted curve is what will
actually be heard. Preamp is left at 0.0 -- the app derives Safe/Session
headroom itself. The fit is greedy (place a band at the largest residual,
peaking in the middle, low/high shelf at the edges) with a joint
least-squares refine of every band after each placement; it stops early
once the residual is within 0.5 dB everywhere in band. A pruning pass
then re-tries the fit without each band in turn and drops the ones the
remaining bands absorb: the greedy order can converge on cancelling
stacks (a deep cut lifted back by a near-cap shelf, a pile of sub-bass
shelves netting 2 dB) that waste the band budget and defeat hand
editing. Pruning is anchored to the original residual and may cost at
most PRUNE_EPS_DB at any grid point.

When the measurement carries the per-take drives (levels recorded by
measure_session) and the channels share one acoustic reference, a
per-channel balance trim is added as a freq-0 shelf band (flat gain,
the preamp trick), aligning the channels' true levels down to the
quietest one. See balance_trims for the exact validity gate.
"""
import argparse
import datetime
import json
import math
import sys

import numpy as np
from scipy.optimize import least_squares

from . import eq
from .config import FS, SCHEMA_VERSION

RESID_TARGET_DB = 0.5
GRID = 400
GREEDY_SPAN_OCT = 1.0       # a band may retune this far from placement
PRUNE_EPS_DB = 0.25         # pruning may cost at most this much, anywhere
PRUNE_OVERLAP_DB = 0.25     # a drop frees only bands it reaches this far
PRUNE_SPAN_OCT = 0.5        # a trial may retune a freed band this far
TRIM_MIN_DB = 0.05          # below this a trim is measurement noise
TRIM_WARN_DB = 3.0          # past this it smells like a seating problem


def _grid_interp(freq, mag, flo, fhi):
    """The fit grid and the measured curve interpolated onto it -- the
    ONE definition of the in-band mean, shared by the shape fit and the
    balance trim so the two close exactly."""
    fg = np.logspace(np.log10(flo), np.log10(fhi), GRID)
    return fg, np.interp(np.log10(fg), np.log10(freq), mag)


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


def _bounds(max_boost, limits):
    """The optimizer's gain/Q box, narrowed by a destination's
    declared ranges when given. The app's own sanity walls (-24 dB
    floor, Q 0.3..8) and the max_boost cap always apply; a target
    looser than the optimizer costs nothing, a tighter one wins."""
    lim = limits or {}
    g_lo, g_hi = lim.get("gain", (None, None))
    q_lo, q_hi = lim.get("q", (None, None))
    g_lo = -24.0 if g_lo is None else max(-24.0, float(g_lo))
    g_hi = max_boost if g_hi is None else min(float(g_hi),
                                              max_boost)
    q_lo = 0.3 if q_lo is None else max(0.3, float(q_lo))
    q_hi = 8.0 if q_hi is None else min(float(q_hi), 8.0)
    return g_lo, g_hi, q_lo, q_hi


def _refine(bands, fg, desired, flo, fhi, max_boost, span_oct=None,
            anchors=None, limits=None, tick=None):
    """Joint least-squares of every band against `desired`. With
    span_oct each band's frequency is leashed to that many octaves
    around its anchor -- the placement frequency in the greedy loop
    (passed via `anchors`), the band's current frequency in the
    prune's one-shot trials (the default). Without span_oct the
    frequency roams the whole fit range. `limits` narrows the
    gain/Q box to a destination's declared ranges (_bounds)."""
    types = [b[0] for b in bands]
    if anchors is None:
        anchors = [b[1] for b in bands]
    g_lo, g_hi, q_lo, q_hi = _bounds(max_boost, limits)
    x0, lo, hi = [], [], []
    for (_, f, g, q), fa in zip(bands, anchors):
        lf = np.log10(f)
        if span_oct is None:
            f_lo, f_hi = np.log10(flo), np.log10(fhi)
        else:
            s = span_oct * np.log10(2.0)
            f_lo = max(np.log10(flo), np.log10(fa) - s)
            f_hi = min(np.log10(fhi), np.log10(fa) + s)
        x0 += [min(max(lf, f_lo), f_hi),
               min(max(g, g_lo), g_hi),
               min(max(q, q_lo), q_hi)]
        lo += [f_lo, g_lo, q_lo]
        hi += [f_hi, g_hi, q_hi]

    def resfun(x):
        if tick is not None:
            tick()
        bl = [(types[i], 10 ** x[3 * i], x[3 * i + 1], x[3 * i + 2])
              for i in range(len(types))]
        return _response(bl, fg) - desired

    sol = least_squares(resfun, x0, bounds=(lo, hi), method="trf",
                        max_nfev=3000)
    return [(types[i], float(10 ** sol.x[3 * i]),
             float(np.clip(sol.x[3 * i + 1], g_lo, g_hi)),
             float(sol.x[3 * i + 2])) for i in range(len(types))]


def _prune(bands, fg, target, flo, fhi, max_boost,
           limits=None, tick=None):
    """Drop bands whose work their NEIGHBOURS absorb.

    The greedy placement is order-dependent and the joint refine only
    polishes the topology it is given, so a fit can converge on
    cancelling stacks (a -18 dB cut lifted back by a +6 shelf, or five
    sub-bass shelves netting +2 dB) that waste band budget and defeat
    hand editing. Try removing each band in turn, re-refine, and keep
    the removal when the residual against the (capped) target stays,
    at every grid point, within the ORIGINAL fit's local residual or
    the fit's own floor, plus PRUNE_EPS_DB -- anchored to the original
    so successive drops cannot ratchet the error upward. A genuinely
    working band fails the pointwise test at its own frequency and
    stays.

    The trial refine is local twice over, and both leashes exist
    because their absence was observed to do damage: a globally
    re-refined survivor set once walked distant bands across a
    residual-flat valley INTO a fresh cancelling stack while absorbing
    unrelated sub-bass drops. Only the bands whose centers the dropped
    band actually reaches (>= PRUNE_OVERLAP_DB) are refined, the rest
    are frozen and folded into the target as a constant; and a freed
    band's frequency may move at most PRUNE_SPAN_OCT octaves from its
    start -- absorbing a neighbour is reshaping, not relocation. A
    directly dropped stack member still dissolves (its partner is
    inside the overlap set); an unrelated drop can no longer rebuild
    the other end of the spectrum."""
    if not bands:
        return bands
    resid0 = np.abs(target - _response(bands, fg))
    allow = np.maximum(resid0, RESID_TARGET_DB) + PRUNE_EPS_DB
    changed = True
    while changed and bands:
        changed = False
        for i in range(len(bands)):
            dresp = _mag_db_vec(*bands[i], fg)
            rest = bands[:i] + bands[i + 1:]
            free, frozen = [], []
            for b in rest:
                k = int(np.argmin(np.abs(fg - b[1])))
                (free if abs(dresp[k]) >= PRUNE_OVERLAP_DB
                 else frozen).append(b)
            if free:
                base = _response(frozen, fg)
                trial = frozen + _refine(free, fg, target - base,
                                         flo, fhi, max_boost,
                                         span_oct=PRUNE_SPAN_OCT,
                                         limits=limits, tick=tick)
            else:
                trial = rest
            r = (np.abs(target - _response(trial, fg))
                 if trial else np.abs(target))
            if bool(np.all(r <= allow)):
                bands = trial
                changed = True
                break
    return bands


def fit_to_desired(fg, desired, flo, fhi, n_bands, max_boost,
                   limits=None, progress=None):
    """The greedy core over a GIVEN desired correction on fg:
    place, leash-refine, prune. Cuts are unbounded; boost is capped
    at max_boost (filling deep nulls wastes headroom and amplifies
    noise), so the fit targets the desired clipped from above while
    the residual is reported against the true (uncapped) curve so
    unfillable dips stay visible. Exposed for export-time re-fits
    (a mean of channels, a band-budget-limited destination); the
    measurement fit path goes through fit_channel below.

    The joint refine after each placement may retune a band at most
    GREEDY_SPAN_OCT octaves from where it was PLACED (the anchor,
    not the band's latest position, or it would creep an octave per
    iteration). Placement picked that spot because the residual
    peaked there and chose the type by it; unleashed, the refine
    used to slide a high-shelf placed above fhi/2 down under a mid
    dip, growing a cancelling -18 dB partner to carve the dip back
    out. The leash also makes the topology a function of the argmax
    sequence, so channels with similar curves come out with
    parallel band tables instead of unrecognizable decompositions
    of the same net response.

    `limits`, when given, narrows the box to a destination's
    declared ranges -- {"gain": (lo, hi), "q": (lo, hi),
    "types": (...)}; boost stays additionally capped by max_boost
    and a placement whose natural shelf type the target lacks
    falls back to a peaking band.

    `progress`, when given, is called as progress(frac, band,
    horizon, evals) while the optimizer works: the major steps are
    band placements, the minor motion inside each step comes from
    the residual evaluations of the joint refine (the optimizer is
    global over the spectrum on every iteration, so per-frequency
    progress does not exist -- per-evaluation is its true
    heartbeat). The inner fraction is hyperbolic, fev/(fev+350):
    it approaches the next step but never saturates, so a long
    refine keeps the bar creeping instead of pegging it -- and
    `evals` is the raw counter for a text readout that visibly
    ticks even when the pixels barely move. The prune's trials
    keep ticking in the last step; frac 1.0 is emitted exactly
    once, at the end.

    Returns (bands, resid) with bands as (type, f, g, q) tuples."""
    desired = np.asarray(desired, float)
    g_lo, g_hi, q_lo, q_hi = _bounds(max_boost, limits)
    allowed = tuple((limits or {}).get("types")
                    or ("PK", "LSC", "HSC"))
    target = np.minimum(desired, g_hi)
    horizon = max(int(n_bands), 1)
    prog = {"band": 0, "fev": 0, "tot": 0}

    def tick():
        prog["fev"] += 1
        prog["tot"] += 1
        if progress is not None and prog["tot"] % 20 == 0:
            inner = prog["fev"] / (prog["fev"] + 350.0)
            progress(min((prog["band"] + inner) / horizon,
                         0.999),
                     prog["band"], horizon, prog["tot"])

    bands, anchors = [], []
    for _ in range(n_bands):
        resid = target - _response(bands, fg)
        k = int(np.argmax(np.abs(resid)))
        if abs(resid[k]) < RESID_TARGET_DB:
            break
        f0 = fg[k]
        btype = ("LSC" if f0 <= flo * 2
                 else "HSC" if f0 >= fhi / 2 else "PK")
        if btype not in allowed:
            btype = "PK" if "PK" in allowed else allowed[0]
        g0 = float(np.clip(resid[k], g_lo, g_hi))
        bands.append((btype, f0, g0,
                      min(max(2.0, q_lo), q_hi)))
        anchors.append(f0)
        bands = _refine(bands, fg, target, flo, fhi, max_boost,
                        span_oct=GREEDY_SPAN_OCT, anchors=anchors,
                        limits=limits, tick=tick)
        prog["band"] = len(bands)
        prog["fev"] = 0
    bands = _prune(bands, fg, target, flo, fhi, max_boost,
                   limits=limits, tick=tick)
    if progress is not None:
        progress(1.0, prog["band"], horizon, prog["tot"])
    return bands, desired - _response(bands, fg)   # vs TRUE target


def desired_curve(freq, mag, flo, fhi):
    """(fg, desired, mean_db): the flat-target correction
    fit_channel derives -- mean minus smoothed magnitude over the
    fit band -- exposed so export-time re-fits ask for exactly what
    the fit was asked for."""
    fg, yg = _grid_interp(freq, mag, flo, fhi)
    return fg, yg.mean() - yg, float(yg.mean())


def fit_channel(freq, mag, flo, fhi, n_bands, max_boost):
    """Return (bands, fg, desired, resid): the flat-target desired
    from desired_curve, fit by fit_to_desired."""
    fg, desired, _mean = desired_curve(freq, mag, flo, fhi)
    bands, resid = fit_to_desired(fg, desired, flo, fhi, n_bands,
                                  max_boost)
    return bands, fg, desired, resid


def _take_gains(result):
    """(soft, chan) linear gain pairs per take from a result's levels,
    or None when absent or unusable (a result recorded before the
    per-take gains existed, an unreadable Props, a non-positive
    value)."""
    lv = result.get("levels") or {}
    soft = lv.get("take_soft_volumes")
    chan = lv.get("take_channel_volumes")
    if not soft or not chan or len(soft) != len(chan):
        return None
    vals = []
    for s, c in zip(soft, chan):
        try:
            s, c = float(s), float(c)
        except (TypeError, ValueError):
            return None
        if not (math.isfinite(s) and math.isfinite(c)) \
                or s <= 0 or c <= 0:
            return None
        vals.append((s, c))
    return vals


def balance_trims(results, means):
    """Per-channel level trims (dB, <= 0) equalizing the channels' true
    acoustic levels from the drives recorded per take, or (None, why).

    The shape fit flattens each channel to its own in-band mean, so
    after EQ a channel plays at that mean minus its measurement drive;
    the trim closes the remaining difference: with Gm = 20*log10 of the
    channel's reference soft gain (the quietest take, which is where
    the session's gain compensation put the curve), comp = mean - Gm
    and trim = min(comp) - comp. Cuts only: channels align DOWN to the
    quietest one, headroom untouched.

    Valid only when the recorded data can carry it: at least two
    channels; every channel's per-take gains recorded and usable; one
    shared acoustic reference (the same cal file -- distinct
    mics/couplers are not cross-calibrated, their SPL difference is
    not the earpieces'); and the drive difference actually known --
    either the sink does the volume in software (softVolumes track
    channelVolumes) or every take of every channel sat at one
    identical volume, which cancels an unknown hardware law outright.
    """
    if len(results) < 2:
        return None, "single channel, nothing to balance"
    cals = {r.get("cal_file") or None for r in results.values()}
    if len(cals) != 1:
        return None, ("channels measured on different mics/couplers "
                      "(distinct cal files): no shared level reference")
    gains = {}
    for key, r in results.items():
        g = _take_gains(r)
        if g is None:
            return None, ("channel %s has no usable per-take gains "
                          "recorded" % key)
        gains[key] = g
    flat = [g for gs in gains.values() for g in gs]
    software = all(math.isclose(s, c, rel_tol=1e-3, abs_tol=1e-6)
                   for s, c in flat)
    one_vol = all(math.isclose(c, flat[0][1], rel_tol=1e-3,
                               abs_tol=1e-6) for _, c in flat)
    if not (software or one_vol):
        return None, ("the device does the volume in hardware and the "
                      "channels were measured at different volumes: "
                      "the drive difference is unknowable")
    comp = {k: means[k] - 20.0 * math.log10(min(s for s, _ in g))
            for k, g in gains.items()}
    ref = min(comp.values())
    return {k: ref - v for k, v in comp.items()}, ""


def _bands_to_dicts(bands):
    return [{"type": t, "freq": round(f, 1), "gain": round(g, 2),
             "q": round(q, 3), "enabled": True} for t, f, g, q in bands]


def _report(tag, bands, fg, resid, flo, fhi, trim_db=None):
    """Console report of one channel's fit. trim_db, when given, is
    the channel's balance trim: printed as the band row it becomes in
    the profile (counted in the header, so the console table matches
    the editor's row for row) and folded into the safe-preamp estimate
    -- a flat trim moves this channel's whole curve."""
    has_trim = trim_db is not None and abs(trim_db) >= TRIM_MIN_DB
    print("\n[%s] %d bands, fit %g-%g Hz"
          % (tag, len(bands) + (1 if has_trim else 0), flo, fhi))
    if has_trim:
        print("  %-3s  %8.1f Hz  %+6.2f dB  Q %5.2f   balance trim"
              % ("HSC", 0.0, trim_db, 1.0))
    for t, f, g, q in sorted(bands, key=lambda b: b[1]):
        print("  %-3s  %8.1f Hz  %+6.2f dB  Q %5.2f" % (t, f, g, q))
    if trim_db is not None and not has_trim:
        print("  balance trim %+.2f dB -- below %g, no band added"
              % (trim_db, TRIM_MIN_DB))
    grid = np.concatenate([fg, np.array([b[1] for b in bands] or [flo])])
    peak = float(np.max(_response(bands, grid)))
    if has_trim:
        peak += trim_db
    print("  residual: RMS %.2f dB, max %.2f dB   safe preamp ~ %+.1f dB"
          % (float(np.sqrt(np.mean(resid ** 2))),
             float(np.max(np.abs(resid))), -max(peak, 0.0)))
    hi = (fg >= 8000) & (fg <= fhi)
    if hi.any():
        print("  residual 8-%g kHz: max %.2f dB (edge of cal trust)"
              % (fhi / 1000, float(np.max(np.abs(resid[hi])))))


def _curve(result):
    """(freq, smoothed magnitude) from a process_takes result dict -- the
    same curve _load reads from result.json, but from an in-memory dict so
    the wizard can fit without a round-trip through disk."""
    d = result["data"]
    return (np.asarray(d["freq_hz"], float),
            np.asarray(d["mag_db_smoothed"], float))


def fit_profiles(results, name=None, bands=10, f_lo=20.0, f_hi=12000.0,
                 max_boost=6.0, mono=False, report=False,
                 progress=None):
    """Fit a profile dict from measurement result dicts. `results` maps
    a channel key (e.g. "FL") to a process_takes result. With mono=True a
    single result is fit once and applied to all channels (apply_all);
    otherwise each channel is fit separately and ch_keys follows the
    mapping's order. This is what the CLI main() and the measurement
    wizard both call; only the target (flat) is shared, the per-channel
    curves are not. When the results carry usable per-take drives and
    share one cal (see balance_trims), a freq-0 shelf band with the
    channel's balance trim is prepended -- an ordinary, editable band
    that PipeWire renders as flat gain, exactly like the preamp.
    Returns the profile body (preamp 0.0: the app derives
    Safe/Session)."""
    name = name or "Measured %s" % datetime.date.today().isoformat()
    prof = {"name": name, "version": SCHEMA_VERSION, "preamp": 0.0,
            "all": {"bands": []}, "channels": {}, "ch_keys": []}
    if progress:                # (done, total, key-being-fit | None)
        progress(0, max(1, len(results)),
                 next(iter(results), None))
    if mono:
        (_key, result), = results.items()
        freq, mag = _curve(result)
        bnds, fg, _desired, resid = fit_channel(freq, mag, f_lo, f_hi,
                                                bands, max_boost)
        if report:
            _report("all", bnds, fg, resid, f_lo, f_hi)
        prof["apply_all"] = True
        prof["all"] = {"bands": _bands_to_dicts(bnds)}
    else:
        prof["apply_all"] = False
        prof["ch_keys"] = list(results.keys())
        fits, means = {}, {}
        keys = list(results.keys())
        for i, key in enumerate(keys):
            if progress and i:
                progress(i, len(keys), key)
            result = results[key]
            freq, mag = _curve(result)
            fits[key] = fit_channel(freq, mag, f_lo, f_hi, bands,
                                    max_boost)
            _fg, yg = _grid_interp(freq, mag, f_lo, f_hi)
            means[key] = float(yg.mean())
        trims, why = balance_trims(results, means)
        for key, (bnds, fg, _desired, resid) in fits.items():
            t = (trims or {}).get(key, 0.0)
            if report:
                _report(key, bnds, fg, resid, f_lo, f_hi,
                        trim_db=(t if trims is not None else None))
            bd = _bands_to_dicts(bnds)
            if abs(t) >= TRIM_MIN_DB:
                bd.insert(0, {"type": "HSC", "freq": 0.0,
                              "gain": round(t, 2), "q": 1.0,
                              "enabled": True})
            prof["channels"][key] = {"bands": bd}
        if report:
            if trims is None:
                print("\nno balance trim: %s" % why)
            elif min(trims.values()) <= -TRIM_WARN_DB:
                print("\nNOTE: a balance trim past %g dB usually means "
                      "a seating/seal difference between the channels "
                      "-- reseat and remeasure rather than EQ it away"
                      % TRIM_WARN_DB)
    if progress:
        progress(max(1, len(results)), max(1, len(results)), None)
    return prof


def _load_result(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


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

    if a.mono:
        results = {"all": _load_result(a.mono)}
        prof = fit_profiles(results, name=a.name, bands=a.bands,
                            f_lo=a.f_lo, f_hi=a.f_hi,
                            max_boost=a.max_boost, mono=True, report=True)
    else:
        results = {}
        for key, path in (("FL", a.left), ("FR", a.right)):
            if path:
                results[key] = _load_result(path)
        prof = fit_profiles(results, name=a.name, bands=a.bands,
                            f_lo=a.f_lo, f_hi=a.f_hi,
                            max_boost=a.max_boost, report=True)

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(prof, f, indent=2, ensure_ascii=False)
    print("\nwrote %s (%r) -- import via the app's \"Import profile...\""
          % (a.out, prof["name"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
