# -*- coding: utf-8 -*-
"""EQ model: bands, the inline PipeWire filter-graph builders, the biquad
frequency response (for the GUI plot) and REW/AutoEQ text import/export.

Pure computation -- no GTK, no subprocess, no filesystem.
"""

import math, cmath, re

from .config import FS, TYPE_TO_LABEL


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


def profile_graph(p):
    """Inline graph string for a profile dict (apply_all or per-channel)."""
    if p.get("apply_all", True):
        a = p.get("all") or {"preamp": 0.0, "bands": []}
        return build_graph(a.get("preamp", 0.0),
                           [Band.from_dict(b) for b in a.get("bands", [])])
    chans = p.get("channels") or {}
    keys = p.get("ch_keys") or list(chans.keys())
    sets = []
    for k in keys:
        e = chans.get(k) or {"preamp": 0.0, "bands": []}
        sets.append((e.get("preamp", 0.0),
                     [Band.from_dict(b) for b in e.get("bands", [])]))
    if not sets:
        a = p.get("all") or {"preamp": 0.0, "bands": []}
        return build_graph(a.get("preamp", 0.0),
                           [Band.from_dict(b) for b in a.get("bands", [])])
    return build_graph_channels(sets)


def _set_has_content(s):
    return (abs(float((s or {}).get("preamp", 0.0))) > 1e-9
            or any(b.get("enabled", True) for b in (s or {}).get("bands", [])))


def profile_has_content(p):
    """True if the profile actually changes the sound (some enabled band or a
    non-zero preamp). A flat profile is equivalent to Clean / no binding."""
    if p.get("apply_all", True):
        return _set_has_content(p.get("all"))
    chans = p.get("channels") or {}
    for k in (p.get("ch_keys") or chans.keys()):
        if _set_has_content(chans.get(k)):
            return True
    return False


# ---- biquad frequency response (Audio EQ Cookbook), for the FR plot only ----
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
