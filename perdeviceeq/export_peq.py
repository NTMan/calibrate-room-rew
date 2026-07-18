# -*- coding: utf-8 -*-
"""Export wizard core (ROADMAP sprint item 1): bake the COMPOSED
chain -- device + active taste, preamp included -- for an external
equalizer picked from a registry.

Three writer classes:
  (a) native import files -- AutoEq-style ParametricEQ text (REW,
      EqualizerAPO, RootlessJamesDSP) and the Wavelet GraphicEQ line;
      band-exact where the format is parametric, and every export is
      null-tested against the in-app response before it is written;
  (b) a constrained fit for fixed-band vendor graphics
      (Soundcore-class companion apps): fixed centers, gains solved
      by bounded least squares against a MEASURED slider basis where
      a drop-in provides one, an assumed peaking basis otherwise,
      with the residual computed for display BEFORE export
      (see solve_fixed / fixed_sheet_text);
  (c) a hand-transfer sheet for targets with no import at all: a
      readable table, values rounded to the target's steps.

The registry is data: BUILTIN_TARGETS plus JSON drop-ins from
~/.config/per-device-eq/export-targets/ (one dict or a list per
file). A drop-in sharing a builtin id replaces it -- the intended
way to attach a measured basis_file to a vendor entry. This is also
where the mobile doctrine's per-vendor targets accumulate.

Pure computation -- no GTK. numpy/scipy are imported lazily inside
the fixed-band fit only, same dependency story as fit_peq.
"""

import bisect
import json
import math
import os
import sys

from . import eq
from .config import CONFIG_DIR

# The fit band the null test and the fixed-band fit default to when
# the profile carries no stamped fit params (fit_peq's own defaults).
FIT_LO_DEFAULT, FIT_HI_DEFAULT = 20.0, 12000.0
NULL_PASS_DB = 0.1          # the acceptance ceiling for native exports
GRAPHIC_POINTS = 127        # AutoEq-sized GraphicEQ grid

USER_TARGET_DIR = os.path.join(CONFIG_DIR, "export-targets")

WRITERS = ("parametric", "graphiceq", "fixed", "sheet")

BUILTIN_TARGETS = [
    {"id": "peq-text",
     "name": "Parametric EQ text",
     "note": "AutoEq style; REW and EqualizerAPO import it",
     "writer": "parametric", "ext": ".txt"},
    {"id": "jamesdsp",
     "name": "RootlessJamesDSP",
     "note": "Android; imports AutoEq ParametricEQ text",
     "writer": "parametric", "ext": ".txt"},
    {"id": "wavelet",
     "name": "Wavelet",
     "note": "Android; imports AutoEq GraphicEQ text",
     "writer": "graphiceq", "ext": ".txt"},
    {"id": "vendor-8band",
     "name": "Vendor graphic, 8 octave bands",
     "note": "Soundcore-class companion apps; sliders set by hand",
     "writer": "fixed", "ext": ".txt",
     "centers": [100.0, 200.0, 400.0, 800.0, 1600.0, 3200.0,
                 6400.0, 12800.0],
     "gain_range": [-6.0, 6.0], "gain_step": 1.0, "basis_q": 1.414},
    {"id": "hand-peq",
     "name": "Hand-transfer sheet",
     "note": "Any parametric EQ entered by hand",
     "writer": "sheet", "ext": ".txt",
     "gain_step": 0.1, "q_step": 0.01},
]


def load_targets(extra_dir=None):
    """The registry: builtins + JSON drop-ins (each file one target
    dict or a list of them). A drop-in sharing a builtin id replaces
    it in place; new ids append in filename order. Invalid files or
    entries are skipped with a note on stderr -- a broken drop-in
    must not take the wizard down."""
    out = [dict(t) for t in BUILTIN_TARGETS]
    d = extra_dir or USER_TARGET_DIR
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for n in names:
        if not n.endswith(".json"):
            continue
        path = os.path.join(d, n)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            print("per-device-eq: skipping export targets %s: %s"
                  % (path, e), file=sys.stderr)
            continue
        items = data if isinstance(data, list) else [data]
        for t in items:
            if (not isinstance(t, dict) or not t.get("id")
                    or not t.get("name")
                    or t.get("writer") not in WRITERS):
                print("per-device-eq: skipping export target in %s: "
                      "need id, name and a known writer" % path,
                      file=sys.stderr)
                continue
            t = dict(t, _src=path)
            for i, old in enumerate(out):
                if old["id"] == t["id"]:
                    out[i] = t
                    break
            else:
                out.append(t)
    return out


# ---- the composed chain -----------------------------------------------


def fit_band(profile):
    """(f_lo, f_hi) from the stamped fit params, or the defaults."""
    params = (profile.get("fit") or {}).get("params") or {}
    lo = float(params.get("f_lo") or FIT_LO_DEFAULT)
    hi = float(params.get("f_hi") or FIT_HI_DEFAULT)
    return lo, hi


def composed_chains(profile, taste_bands=None):
    """[(key, preamp, band_dicts)] of the chain the app actually
    plays: the profile's own slots with the active taste layer's
    bands appended to EVERY chain and the one shared preamp --
    mirrors eq.profile_graph, including the fall-back to the "all"
    slot when a per-channel profile has no channel sets."""
    tail = [dict(b) for b in (taste_bands or [])]
    g = float(profile.get("preamp", 0.0))
    if profile.get("apply_all", True):
        a = profile.get("all") or {"bands": []}
        return [("all", g, list(a.get("bands", [])) + tail)]
    chans = profile.get("channels") or {}
    keys = list(profile.get("ch_keys") or chans.keys())
    out = [(k, g, list((chans.get(k) or {}).get("bands", [])) + tail)
           for k in keys]
    if not out:
        a = profile.get("all") or {"bands": []}
        return [("all", g, list(a.get("bands", [])) + tail)]
    return out


def to_bands(band_dicts):
    return [eq.Band.from_dict(b) for b in band_dicts]


def chain_response(preamp, band_dicts, freqs):
    """The chain's response in dB on `freqs` -- the same biquad math
    the app plots and PipeWire renders."""
    return eq.response_db(preamp, to_bands(band_dicts), freqs)


def fold_flat(preamp, bands):
    """Fold flat-gain shelf bands (freq < 1 Hz: the balance-trim
    trick fit_peq uses) into the preamp -- importers reject Fc 0 and
    the in-band response is identical. Disabled bands are dropped
    here once: no writer exports them. Returns
    (preamp, bands, folded_db)."""
    out, folded = [], 0.0
    for b in bands:
        if not b.get("enabled", True):
            continue
        if (float(b.get("freq", 0.0)) < 1.0
                and b.get("type") in ("HSC", "LSC")):
            folded += float(b.get("gain", 0.0))
        else:
            out.append(dict(b))
    return preamp + folded, out, folded


# ---- collapse (the per-channel policy) --------------------------------


def collapse_choices(chains, band_domain):
    """The policies the wizard may offer: the chain keys, plus
    "mean" for response-domain writers (graphiceq / fixed) when the
    profile is per-channel. Band-domain writers (parametric / sheet)
    export one chain's bands verbatim, so mean is not on the menu --
    averaging band tables is not a defined operation."""
    keys = [k for k, _g, _b in chains]
    if len(keys) == 1 or band_domain:
        return keys
    return keys + ["mean"]


def pick_chain(chains, policy):
    """One chain by key for band-domain writers:
    (preamp, bands, note). The note is what the export header states
    as the collapse policy."""
    keys = [k for k, _g, _b in chains]
    for k, g, b in chains:
        if k == policy:
            if k == "all":
                return g, b, "single chain (apply-all)"
            return g, b, "channel %s of %s" % (k, ", ".join(keys))
    raise KeyError(policy)


def collapse(chains, policy, freqs):
    """The response-domain collapse: (response_db, note). `policy`
    is a chain key or "mean" (per-frequency mean across chains,
    in dB)."""
    if policy == "mean":
        cols = [chain_response(g, b, freqs) for _k, g, b in chains]
        n = float(len(cols))
        resp = [sum(c[i] for c in cols) / n
                for i in range(len(freqs))]
        keys = ", ".join(k for k, _g, _b in chains)
        return resp, "mean of %s" % keys
    for k, g, b in chains:
        if k == policy:
            note = ("single chain (apply-all)" if k == "all" else
                    "channel %s of %s" % (k, ", ".join(
                        c[0] for c in chains)))
            return chain_response(g, b, freqs), note
    raise KeyError(policy)


# ---- writers -----------------------------------------------------------


def _hdr(lines):
    if not lines:
        return ""
    return "".join("%s\n" % l for l in lines) + "\n"


def parametric_text(preamp, bands, header=()):
    """AutoEq-style ParametricEQ text of ONE chain, band-exact.
    Header lines ride on top: every parser of this format keys on
    the Preamp / Filter line shapes and skips the rest."""
    return _hdr(header) + eq.eq_text(preamp, to_bands(bands))


def graphic_grid(points=GRAPHIC_POINTS):
    """The GraphicEQ frequency grid: `points` log-spaced integer
    frequencies over 20..20000 Hz, deduplicated."""
    la, lb = math.log10(20.0), math.log10(20000.0)
    out, last = [], None
    for i in range(points):
        f = int(round(10 ** (la + (lb - la) * i / (points - 1))))
        if f != last:
            out.append(float(f))
            last = f
    return out


def graphiceq_text(freqs, resp, header=()):
    """The Wavelet / AutoEq GraphicEQ line for a response sampled on
    `freqs`. Points above 0 dB are level-shifted down as a whole (a
    graphic EQ has no preamp to carry headroom); the shift is stated
    in the header and returned so the null test can net it out.
    Returns (text, shift_db)."""
    lines = list(header)
    top = max(resp)
    # below 0.05 dB a shift is float noise, not headroom
    shift = -top if top > 0.05 else 0.0
    if shift:
        lines.append("Level shifted %+.1f dB so no point is above "
                     "0 dB." % shift)
    body = "GraphicEQ: " + "; ".join(
        "%d %.1f" % (int(round(f)), r + shift)
        for f, r in zip(freqs, resp))
    return _hdr(lines) + body + "\n", shift


def parse_graphiceq(text):
    """(freqs, gains) from the first GraphicEQ line, or ([], [])."""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("GraphicEQ:"):
            continue
        fs, gs = [], []
        for pair in line[len("GraphicEQ:"):].split(";"):
            a = pair.split()
            if len(a) >= 2:
                fs.append(float(a[0]))
                gs.append(float(a[1]))
        return fs, gs
    return [], []


_TYPE_NAMES = {"PK": "Peak", "LSC": "Low shelf", "HSC": "High shelf"}


def _step_decimals(step):
    if not step:
        return 2
    d = 0
    while d < 4 and abs(step * 10 ** d - round(step * 10 ** d)) > 1e-9:
        d += 1
    return d


def round_step(v, step):
    if not step:
        return float(v)
    return round(round(float(v) / step) * step, 6)


def rounded_chain(target, preamp, bands):
    """(preamp, bands) with values rounded to the target's steps
    (gain_step / q_step / freq_step, each optional) -- what the
    sheet prints, reusable by the wizard to state the rounding cost
    as a response delta."""
    gs = target.get("gain_step")
    qs = target.get("q_step")
    fs = target.get("freq_step")
    out = []
    for b in bands:
        out.append(dict(b,
                        freq=round_step(b.get("freq", 0.0), fs),
                        gain=round_step(b.get("gain", 0.0), gs),
                        q=round_step(b.get("q", 1.0), qs)))
    return round_step(preamp, gs), out


def sheet_text(target, preamp, bands, header=()):
    """The hand-transfer sheet: one readable table of the chain,
    values rounded to the target's steps, preamp on top."""
    rp, rb = rounded_chain(target, preamp,
                           [b for b in bands
                            if b.get("enabled", True)])
    gd = _step_decimals(target.get("gain_step"))
    qd = _step_decimals(target.get("q_step"))
    lines = list(header)
    lines.append("")
    lines.append("Preamp: %+.*f dB" % (gd, rp))
    lines.append("")
    lines.append(" #  %-10s  %9s  %9s  %7s"
                 % ("Type", "Freq [Hz]", "Gain [dB]", "Q"))
    for i, b in enumerate(rb, 1):
        lines.append("%2d  %-10s  %9g  %+9.*f  %7.*f"
                     % (i, _TYPE_NAMES.get(b["type"], b["type"]),
                        b["freq"], gd, b["gain"], qd, b["q"]))
    steps = []
    if target.get("gain_step"):
        steps.append("gain %g dB" % target["gain_step"])
    if target.get("q_step"):
        steps.append("Q %g" % target["q_step"])
    if target.get("freq_step"):
        steps.append("freq %g Hz" % target["freq_step"])
    if steps:
        lines.append("")
        lines.append("Values rounded to the target's steps: %s."
                     % ", ".join(steps))
    return "\n".join(lines) + "\n"


# ---- verification ------------------------------------------------------


def log_grid(flo, fhi, n):
    la, lb = math.log10(flo), math.log10(fhi)
    return [10 ** (la + (lb - la) * i / (n - 1)) for i in range(n)]


def _interp_logf(fs, gs, f):
    """Linear-in-log-frequency interpolation, clamped at the ends --
    how graphic EQ importers read the point list."""
    if f <= fs[0]:
        return gs[0]
    if f >= fs[-1]:
        return gs[-1]
    i = bisect.bisect_right(fs, f) - 1
    x0, x1 = math.log10(fs[i]), math.log10(fs[i + 1])
    t = (math.log10(f) - x0) / (x1 - x0)
    return gs[i] + t * (gs[i + 1] - gs[i])


def null_test_parametric(text, freqs, ref_resp):
    """Max |exported - in-app| in dB over `freqs`: the export is
    parsed back with the app's own parser and evaluated with the
    app's own biquads, so this is the roundtrip the acceptance
    criterion names, not a formatting check."""
    pre, bands = eq.parse_autoeq(text)
    got = eq.response_db(pre, bands, freqs)
    return max(abs(a - b) for a, b in zip(got, ref_resp))


# ---- the fixed-band fit (writer class b) -------------------------------


def peaking_basis(centers, q, freqs):
    """One unit column per slider: the response of a 1 dB peaking
    biquad at each center, scaled linearly by the solver. The biquad
    shape is mildly gain-dependent, so linear scaling is an
    approximation -- the standard one for graphic-EQ work, and the
    residual is computed against it and shown either way."""
    import numpy as np
    cols = [eq.response_db(0.0, [eq.Band("PK", c, 1.0, q)], freqs)
            for c in centers]
    return np.array(cols, dtype=float).T


def load_basis(target, freqs):
    """The measured slider basis interpolated onto `freqs`, or None.
    The JSON contract ({"freq": [...], "curve_gain_db": g,
    "curves": [[dB, ...] one per center]}) is the rig procedure from
    the ROADMAP made a file: each curve is one slider at `g` dB with
    the zero run subtracted. Relative paths resolve next to the
    drop-in that named them. A basis that does not match the
    target's centers is refused loudly and the caller falls back to
    the assumed basis -- a silently wrong basis would defeat the
    residual's whole point."""
    path = target.get("basis_file")
    if not path:
        return None
    if not os.path.isabs(path):
        base = os.path.dirname(target.get("_src", ""))
        path = os.path.join(base or USER_TARGET_DIR, path)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        print("per-device-eq: export basis %s unreadable: %s"
              % (path, e), file=sys.stderr)
        return None
    fs = [float(v) for v in (data.get("freq") or [])]
    curves = data.get("curves") or []
    g = float(data.get("curve_gain_db") or 0.0)
    if (len(fs) < 2 or not g
            or len(curves) != len(target.get("centers") or [])):
        print("per-device-eq: export basis %s does not match "
              "target %s (need freq, curve_gain_db and one curve "
              "per center)" % (path, target.get("id")),
              file=sys.stderr)
        return None
    import numpy as np
    cols = [[_interp_logf(fs, [float(v) for v in c], f) / g
             for f in freqs] for c in curves]
    return np.array(cols, dtype=float).T


def solve_fixed(target, freqs, desired):
    """Writer class (b): gains for the target's fixed centers by
    bounded least squares, a free level offset absorbing what
    sliders cannot reach (a vendor graphic has no preamp; level is
    the volume knob's job, not the fit's error). Gains are rounded
    to the target's step and clamped, then the offset is re-derived
    and the residual computed AGAINST THE ROUNDED GAINS -- the sheet
    must state the error of what the person will actually enter.
    Returns the dict the wizard plots and fixed_sheet_text prints;
    the residual is in it by construction, so the fixed-band path
    cannot produce an export that never computed one."""
    import numpy as np
    from scipy.optimize import lsq_linear
    centers = [float(c) for c in target["centers"]]
    k = len(centers)
    basis = load_basis(target, freqs)
    if basis is not None:
        note = "measured basis (%s)" % os.path.basename(
            target.get("basis_file", ""))
    else:
        q = float(target.get("basis_q") or 1.414)
        basis = peaking_basis(centers, q, freqs)
        note = "assumed peaking basis, Q %g" % q
    d = np.asarray(desired, dtype=float)
    glo, ghi = [float(v) for v in
                (target.get("gain_range") or [-6.0, 6.0])]
    a = np.hstack([basis, np.ones((len(freqs), 1))])
    lo = np.array([glo] * k + [-np.inf])
    hi = np.array([ghi] * k + [np.inf])
    sol = lsq_linear(a, d, bounds=(lo, hi))
    step = float(target.get("gain_step") or 0.0)
    gains = [min(ghi, max(glo, round_step(v, step)))
             for v in sol.x[:k]]
    shaped = basis @ np.asarray(gains)
    offset = float(np.mean(d - shaped))
    achieved = shaped + offset
    resid = d - achieved
    return {"centers": centers, "gains": gains, "offset": offset,
            "freqs": list(freqs),
            "desired": [float(v) for v in d],
            "achieved": [float(v) for v in achieved],
            "resid": [float(v) for v in resid],
            "resid_max": float(np.max(np.abs(resid))),
            "resid_rms": float(math.sqrt(float(np.mean(resid
                                                       * resid)))),
            "basis": note}


def fixed_sheet_text(target, sol, header=()):
    """The slider sheet for a solve_fixed result. The fit's basis,
    the absorbed level trim and the residual figures are printed
    INTO the sheet: even the artifact carries what the wizard
    showed."""
    gd = _step_decimals(target.get("gain_step"))
    lines = list(header)
    lines.append("Fit: bounded least squares over %s."
                 % sol["basis"])
    lines.append("Level trim absorbed by the fit: %+.1f dB "
                 "(volume, not a slider)." % sol["offset"])
    lines.append("Residual: max %.1f dB, rms %.1f dB across the "
                 "fit band." % (sol["resid_max"], sol["resid_rms"]))
    lines.append("")
    for c, g in zip(sol["centers"], sol["gains"]):
        lines.append("%8s Hz   %+.*f" % ("%g" % c, gd, g))
    return "\n".join(lines) + "\n"


def null_test_graphic(text, freqs, ref_resp, shift=0.0):
    """Max |exported - (in-app + shift)| in dB over `freqs`, reading
    the point list the way importers do (_interp_logf). `shift` is
    the level shift graphiceq_text reported."""
    fs, gs = parse_graphiceq(text)
    if not fs:
        return float("inf")
    return max(abs(_interp_logf(fs, gs, f) - (r + shift))
               for f, r in zip(freqs, ref_resp))
