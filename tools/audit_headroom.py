#!/usr/bin/env python3
"""Offline headroom audit: pre-EQ capture x profile -> post-EQ verdict.

This is the prototype of per-device-eq "Check headroom" (Task 2 tier 3).
The sink monitor taps PRE-EQ in the in-node topology, so post-EQ level
must be computed, not tapped: we run the capture through the exact
biquads of the profile and report the peak the quantizer will see.

Usage:
    audit_headroom.py capture.wav profile.json      # audit or app schema
    audit_headroom.py capture.wav --profile NAME    # saved app profile by name
    audit_headroom.py capture.wav --demo            # investigation FL/FR chains
    audit_headroom.py --dump-demo > profile.json

profile.json is either this tool's own schema ({"preamp": g, "channels":
{"FL": [band, ...]}}, band key "on") or a schema-v2 profile saved by the
app (~/.config/per-device-eq/profiles/*.json: one shared preamp, band
key "enabled", optional linked "all" slot; v1 files must be converted
with tools/migrate_profiles_v1_to_v2.py first). --profile NAME resolves
a saved app profile by its "name" field: case-insensitive, an
unambiguous substring is enough. The suggestion is a single shared value
set by the worst channel -- inherently balance-preserving.
"""
import glob
import json
import math
import os
import sys

import numpy as np
import soundfile as sf

from pde_audit import DEMO_PROFILE, apply_chain, clip_stats, dbfs, load_profile

CH_NAMES = ["FL", "FR", "FC", "LFE", "RL", "RR", "SL", "SR"]
PROFILES_DIR = os.path.expanduser("~/.config/per-device-eq/profiles")


def _app_bands(slot):
    """App band dicts (key "enabled") -> audit band dicts (key "on")."""
    return [{"type": b.get("type", "PK"), "freq": b.get("freq", 1000.0),
             "gain": b.get("gain", 0.0), "q": b.get("q", 1.0),
             "on": b.get("enabled", True)}
            for b in (slot or {}).get("bands", [])]


def _is_app_schema(p):
    if "apply_all" in p or "all" in p or "ch_keys" in p:
        return True
    return any(isinstance(v, dict) for v in (p.get("channels") or {}).values())


def normalize_profile(raw):
    """-> (chains, meta). chains is an ordered [(name, preamp_db, bands)]
    with audit-style bands; meta = {"name", "mode"} where mode is "audit"
    (this tool's schema, one global preamp), "all" (linked app profile,
    one chain for every capture channel) or "per-channel"."""
    if not _is_app_schema(raw):
        g = float(raw.get("preamp", 0.0))
        chains = [(k, g, v) for k, v in (raw.get("channels") or {}).items()]
        return chains, {"name": raw.get("name"), "mode": "audit"}
    # app schema is v2: ONE shared top-level preamp, slots carry bands only
    if "preamp" not in raw:
        sys.exit("schema v1 app profile (per-slot preamps); run "
                 "tools/migrate_profiles_v1_to_v2.py once to convert")
    g = float(raw["preamp"])
    if raw.get("apply_all", True):
        return ([("all", g, _app_bands(raw.get("all")))],
                {"name": raw.get("name"), "mode": "all"})
    ch = raw.get("channels") or {}
    keys = raw.get("ch_keys") or list(ch.keys())
    chains = [(k, g, _app_bands(ch.get(k))) for k in keys]
    return chains, {"name": raw.get("name"), "mode": "per-channel"}


def resolve_profile(arg, profiles_dir=None):
    """A path loads directly; anything else resolves against the "name"
    fields of the app's saved profiles. Returns (raw_profile, path)."""
    if os.path.exists(arg):
        return load_profile(arg), arg
    d = profiles_dir or PROFILES_DIR
    cands = []
    for path in sorted(glob.glob(os.path.join(d, "*.json"))):
        try:
            with open(path) as f:
                body = json.load(f)
        except Exception:
            continue
        cands.append((body.get("name") or os.path.basename(path), body, path))
    low = arg.lower()
    hit = [c for c in cands if (c[0] or "").lower() == low]
    if not hit:
        subs = [c for c in cands if low in (c[0] or "").lower()]
        if len(subs) == 1:
            hit = subs
        elif len(subs) > 1:
            sys.exit("profile %r is ambiguous: %s"
                     % (arg, ", ".join(sorted(c[0] for c in subs))))
    if not hit:
        names = ", ".join(sorted(c[0] for c in cands)) or "(none)"
        sys.exit("profile %r not found in %s; saved profiles: %s"
                 % (arg, d, names))
    return hit[0][1], hit[0][2]


def _ceil_db(v):
    """Round up to the 0.1 dB the preamp spin can express (never leaves a
    positive residual, unlike round-to-nearest)."""
    return math.ceil(v * 10.0 - 1e-9) / 10.0


def main():
    args = sys.argv[1:]
    if args == ["--dump-demo"]:
        json.dump(DEMO_PROFILE, sys.stdout, indent=2)
        return
    if len(args) == 2 and args[1] == "--demo":
        capture, raw, prof_path = args[0], DEMO_PROFILE, None
    elif len(args) == 3 and args[1] == "--profile":
        capture = args[0]
        raw, prof_path = resolve_profile(args[2])
    elif len(args) == 2:
        capture = args[0]
        raw, prof_path = resolve_profile(args[1])
    else:
        sys.exit(__doc__)
    chains, meta = normalize_profile(raw)

    x, fs = sf.read(capture, dtype="float64", always_2d=True)
    n_ch = x.shape[1]

    per_ch = {}          # capture channel index -> (name, preamp, bands)
    if meta["mode"] == "audit":
        for i in range(n_ch):
            nm = CH_NAMES[i] if i < len(CH_NAMES) else "ch%d" % i
            bands = (raw.get("channels") or {}).get(nm)
            per_ch[i] = None if bands is None else (
                nm, float(raw.get("preamp", 0.0)), bands)
    elif meta["mode"] == "all":
        _, g, bands = chains[0]
        for i in range(n_ch):
            nm = CH_NAMES[i] if i < len(CH_NAMES) else "ch%d" % i
            per_ch[i] = (nm, g, bands)
    else:
        for i in range(n_ch):
            per_ch[i] = chains[i] if i < len(chains) else None

    if meta["mode"] != "audit":
        pres = (("preamp %+.1f" % chains[0][1]) if meta["mode"] == "all" else
                "preamps " + ", ".join("%s %+.1f" % (k, g)
                                       for k, g, _ in chains))
        print("profile: %s (%s; %s)%s"
              % (meta.get("name") or prof_path, meta["mode"], pres,
                 (" [%s]" % prof_path) if prof_path else ""))

    worst = float("-inf")
    posts = []           # (name, cur_preamp, post_peak) for suggestions
    for i in range(n_ch):
        if per_ch[i] is None:
            nm = CH_NAMES[i] if i < len(CH_NAMES) else "ch%d" % i
            print("%s: no bands in profile, skipped" % nm)
            continue
        nm, pre_db, bands = per_ch[i]
        pre = x[:, i]
        post = apply_chain(pre, bands, fs, pre_db)
        n, pct, runs = clip_stats(post)
        pk = dbfs(post)
        worst = max(worst, pk)
        posts.append((nm, pre_db, pk))
        print("%s: pre %+6.2f -> post-EQ %+6.2f dBFS | >=FS: %d (%.3f%%),"
              " %d episodes" % (nm, dbfs(pre), pk, n, pct, runs))

    if meta["mode"] == "audit":
        rec = _ceil_db(max(0.0, worst))
        print("recommended preamp: %.1f dB%s"
              % (-rec, "  (no change needed)" if rec == 0 else ""))
    elif posts:
        # schema v2 has ONE shared preamp, so the suggestion is inherently
        # balance-preserving: the worst channel sets it for everyone
        nm, g, pk = max(posts, key=lambda t: t[2])
        if pk <= 0.0:
            print("current preamp keeps %.1f dB headroom (no change needed)"
                  % -pk)
        else:
            print("suggested preamp: %.1f dB  (worst channel %s; post-EQ "
                  "peak -> 0 dBFS)" % (g - _ceil_db(pk), nm))


if __name__ == "__main__":
    main()
