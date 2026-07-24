# -*- coding: utf-8 -*-
"""EQ model: bands, the inline PipeWire filter-graph builders, the biquad
frequency response (GUI plot + the tier-1 headroom / clip estimate) and
REW/AutoEQ text import/export.

Pure computation -- no GTK, no subprocess, no filesystem.
"""

import math, cmath, re

from .config import FS, FMIN, FMAX, TYPE_TO_LABEL


# ============================ EQ model ============================
class Band:
    __slots__ = ("type", "freq", "gain", "q", "enabled")
    def __init__(self, type="PK", freq=1000.0, gain=0.0, q=1.0, enabled=True):
        self.type = type if type in TYPE_TO_LABEL else "PK"
        self.freq = float(freq); self.gain = float(gain)
        self.q = float(q); self.enabled = bool(enabled)
    def to_dict(self):
        return {"type": self.type, "freq": self.freq, "gain": self.gain,
                "q": self.q, "enabled": self.enabled}
    @classmethod
    def from_dict(cls, d):
        return cls(d.get("type", "PK"), d.get("freq", 1000.0),
                   d.get("gain", 0.0), d.get("q", 1.0), d.get("enabled", True))


def filter_entry(b):
    return "{ type = %s, freq = %g, gain = %g, q = %g }" % (
        TYPE_TO_LABEL[b.type], b.freq, b.gain, b.q)


def build_filter_array(preamp, bands):
    """The inline '[ ... ]' array of param_eq filters (no double quotes ->
    inline-safe). Preamp is emitted as a highshelf at freq 0 (== flat gain)."""
    filters = []
    if abs(preamp) > 1e-9:
        filters.append("{ type = bq_highshelf, freq = 0, gain = %g, q = 1.0 }" % preamp)
    for b in bands:
        if b.enabled:
            filters.append(filter_entry(b))
    if not filters:  # never emit an empty filter list -> one transparent filter
        filters.append("{ type = bq_peaking, freq = 1000, gain = 0.0, q = 1.0 }")
    return "[ %s ]" % " ".join(filters)


def build_graph(preamp, bands):
    """Single param_eq applied to all channels (config.filters)."""
    cfg = "filters = %s" % build_filter_array(preamp, bands)
    return ("{ nodes = [ { type = builtin name = eq label = param_eq "
            "config = { %s } } ] }" % cfg)


def build_graph_channels(channel_sets):
    """Per-channel param_eq. channel_sets is a list of (preamp, bands) in
    channel order; emitted as config.filters1, filters2, ... (1-based)."""
    parts = []
    for i, (preamp, bands) in enumerate(channel_sets, start=1):
        parts.append("filters%d = %s" % (i, build_filter_array(preamp, bands)))
    cfg = " ".join(parts)
    return ("{ nodes = [ { type = builtin name = eq label = param_eq "
            "config = { %s } } ] }" % cfg)


# The device floor: Taste asks, the zone disposes. The device
# renders the whole chain -- Taste included -- only inside its
# measured trust zone; below the zone's lower edge an LR8
# high-pass cascade (four biquads, the Butterworth-squared Q
# pair twice) protects the driver at 48 dB/oct. The frequency
# is not a new field: it IS the stored fit's f_lo, so a
# re-measure moves the floor by itself and no hand-kept copy
# can drift. Engaged only when the zone starts at or above
# FLOOR_MIN_HZ -- a zone reaching into deep bass means the
# device rendered it and there is nothing to protect.
FLOOR_MIN_HZ = 30.0
FLOOR_QS = (0.5412, 1.3066, 0.5412, 1.3066)


def floor_bands(p):
    """The sealed floor stages for a profile dict, or []. Band
    dicts (type HP at the fit's f_lo) that the graph builder
    and the preview both consume; they never enter the
    profile's band lists -- the zone places them, not the
    hand."""
    params = ((p or {}).get("fit") or {}).get("params") or {}
    try:
        lo = float(params.get("f_lo", 0.0))
    except (TypeError, ValueError):
        return []
    if lo < FLOOR_MIN_HZ:
        return []
    return [{"type": "HP", "freq": lo, "gain": 0.0, "q": q,
             "enabled": True} for q in FLOOR_QS]


def profile_graph(p, extra=None):
    """Inline graph string for a schema-v2 profile dict: ONE shared preamp,
    slots carry bands only (apply_all or per-channel). `extra` is a list
    of preference-layer band dicts appended after EVERY chain -- taste
    composed over correction, whatever the profile's channel layout; the
    shared preamp stays the profile's own, and headroom over the
    composition is the caller's job (curve_max_db on the concatenation).
    """
    g = float(p.get("preamp", 0.0))
    tail = [Band.from_dict(b)
            for b in floor_bands(p) + list(extra or [])]
    if p.get("apply_all", True):
        a = p.get("all") or {"bands": []}
        return build_graph(g, [Band.from_dict(b)
                               for b in a.get("bands", [])] + tail)
    chans = p.get("channels") or {}
    keys = p.get("ch_keys") or list(chans.keys())
    sets = [(g, [Band.from_dict(b)
                 for b in (chans.get(k) or {}).get("bands", [])] + tail)
            for k in keys]
    if not sets:
        a = p.get("all") or {"bands": []}
        return build_graph(g, [Band.from_dict(b)
                               for b in a.get("bands", [])] + tail)
    return build_graph_channels(sets)


def _set_has_content(s):
    return any(b.get("enabled", True) for b in (s or {}).get("bands", []))


def profile_has_content(p):
    """True if the profile actually changes the sound (some enabled band or a
    non-zero preamp). A flat profile is equivalent to Clean / no binding."""
    if abs(float(p.get("preamp", 0.0))) > 1e-9:   # schema v2 shared preamp
        return True
    if p.get("apply_all", True):
        return _set_has_content(p.get("all"))
    chans = p.get("channels") or {}
    for k in (p.get("ch_keys") or chans.keys()):
        if _set_has_content(chans.get(k)):
            return True
    return False


# ---- biquad frequency response: FR plot + tier-1 headroom estimate ---------
# Audio EQ Cookbook with the Q parameterization for shelves -- coefficient-
# identical to PipeWire's biquad_{peaking,lowshelf,highshelf}
# (spa/plugins/audioconvert/biquad.c, linked into filter-graph's param_eq;
# verified against the 1.6.2 tag and master). Note this is NOT the RBJ "shelf
# slope" form sqrt((A+1/A)(1/S-1)+2): with S=q that one drifts up to ~2 dB
# from the real DSP on high-Q shelves.
def biquad(btype, f0, gain_db, q, fs=FS):
    f0 = min(max(f0, 1.0), fs / 2 - 1.0)
    q = max(q, 0.05)
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * math.pi * f0 / fs
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / (2 * q)
    if btype == "PK":
        b0, b1, b2 = 1 + alpha * A, -2 * cw, 1 - alpha * A
        a0, a1, a2 = 1 + alpha / A, -2 * cw, 1 - alpha / A
    elif btype == "LSC":
        s = 2 * math.sqrt(A) * alpha
        b0 = A * ((A + 1) - (A - 1) * cw + s)
        b1 = 2 * A * ((A - 1) - (A + 1) * cw)
        b2 = A * ((A + 1) - (A - 1) * cw - s)
        a0 = (A + 1) + (A - 1) * cw + s
        a1 = -2 * ((A - 1) + (A + 1) * cw)
        a2 = (A + 1) + (A - 1) * cw - s
    elif btype == "HP":
        b0 = (1 + cw) / 2
        b1 = -(1 + cw)
        b2 = (1 + cw) / 2
        a0, a1, a2 = 1 + alpha, -2 * cw, 1 - alpha
    elif btype == "LP":
        b0 = (1 - cw) / 2
        b1 = 1 - cw
        b2 = (1 - cw) / 2
        a0, a1, a2 = 1 + alpha, -2 * cw, 1 - alpha
    else:  # HSC
        s = 2 * math.sqrt(A) * alpha
        b0 = A * ((A + 1) + (A - 1) * cw + s)
        b1 = -2 * A * ((A - 1) + (A + 1) * cw)
        b2 = A * ((A + 1) + (A - 1) * cw - s)
        a0 = (A + 1) - (A - 1) * cw + s
        a1 = 2 * ((A - 1) - (A + 1) * cw)
        a2 = (A + 1) - (A - 1) * cw - s
    return (b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0)


def mag_db(c, f, fs=FS):
    b0, b1, b2, a0, a1, a2 = c
    w = 2 * math.pi * f / fs
    z1, z2 = cmath.exp(-1j * w), cmath.exp(-2j * w)
    H = (b0 + b1 * z1 + b2 * z2) / (a0 + a1 * z1 + a2 * z2)
    m = abs(H)
    return 20 * math.log10(m) if m > 1e-12 else -120.0


def response_db(preamp, bands, freqs):
    coeffs = [biquad(b.type, b.freq, b.gain, b.q) for b in bands if b.enabled]
    out = []
    for f in freqs:
        s = preamp
        for c in coeffs:
            s += mag_db(c, f)
        out.append(s)
    return out


# ---- headroom / clip estimate (ROADMAP Task 2, tier 1) ---------------------
def curve_max_db(preamp, bands, n=240, fmin=FMIN, fmax=FMAX):
    """Max of the total EQ curve (preamp + enabled bands) in dB: the largest
    gain the chain applies to any single frequency. Evaluated on an n-point
    log grid PLUS every enabled band's center frequency, so narrow (high-Q)
    peaks cannot fall between grid points. Pinned to the scipy reference
    (perdeviceeq.pde_audit.chain_curve) by tests/test_headroom_bound.py."""
    la, lb = math.log10(fmin), math.log10(fmax)
    freqs = [10 ** (la + (lb - la) * i / (n - 1)) for i in range(n)]
    freqs += [min(max(b.freq, fmin), fmax) for b in bands if b.enabled]
    return max(response_db(preamp, bands, freqs))


def headroom_bound_db(preamp, bands, monitor_peak_db=0.0):
    """Instant post-EQ peak estimate in dBFS (ROADMAP Task 2, tier 1):
    monitor_peak + max(total EQ curve). Until the live capture meter
    (tier 2) exists the monitor peak is taken as 0 dBFS -- legal full-scale
    content, the worst case. Two honest limitations, both handled by the
    capture tiers: inputs can overshoot FS on their own (hot lossy masters
    after any resampler -- the hot_master fixture sits at pre-EQ +1.7 dBFS),
    and a broadband crest can in principle recombine above this sine-gain
    figure; on the fixtures the estimate stays conservative (see tests)."""
    return monitor_peak_db + curve_max_db(preamp, bands)


# ============================ REW / AutoEQ text ============================
_RE_PREAMP = re.compile(r"Preamp:\s*(-?\d+(?:\.\d+)?)\s*dB", re.I)
_RE_FILTER = re.compile(
    r"Filter\s+\d+:\s*ON\s+(PK|LS|LSC|HS|HSC|LP|HP|LPQ|HPQ)\s+"
    r"Fc\s+(\d+(?:\.\d+)?)\s*Hz"
    r"(?:\s+Gain\s+(-?\d+(?:\.\d+)?)\s*dB)?"
    r"(?:\s+Q\s+(\d+(?:\.\d+)?))?", re.I)


def parse_autoeq(text):
    preamp = 0.0
    m = _RE_PREAMP.search(text)
    if m:
        preamp = float(m.group(1))
    bands = []
    for mt in _RE_FILTER.finditer(text):
        kind = mt.group(1).upper()
        fc = float(mt.group(2))
        gain = float(mt.group(3)) if mt.group(3) else 0.0
        q = float(mt.group(4)) if mt.group(4) else 1.0
        if kind in ("LS", "LSC"):
            btype = "LSC"
        elif kind in ("HS", "HSC"):
            btype = "HSC"
        else:
            btype = "PK"
        bands.append(Band(btype, fc, gain, q, True))
    return preamp, bands


def eq_text(preamp, bands):
    """REW/AutoEQ ParametricEQ text (re-importable). Only enabled bands."""
    lines = ["Preamp: %.1f dB" % preamp]
    i = 1
    for b in bands:
        if not b.enabled:
            continue
        lines.append("Filter %d: ON %s Fc %g Hz Gain %.2f dB Q %.4f"
                     % (i, b.type, b.freq, b.gain, b.q))
        i += 1
    return "\n".join(lines) + "\n"
