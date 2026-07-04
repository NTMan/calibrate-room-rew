#!/usr/bin/env python3
"""One-shot migration of per-device-eq profiles: schema v1 -> v2.

v1 stored a preamp per channel slot; schema v2 keeps ONE profile-level
"preamp" (slots carry bands only) and a "version": 2 stamp. The app reads
only v2 files and points here when it skips a v1 one.

The applied slots' preamps fold into their MINIMUM -- the only direction
that can never introduce clipping: a channel may only get quieter, and
the balance skew v1 allowed is normalized conservatively.

Usage:
    migrate_profiles_v1_to_v2.py [profiles-dir] [--dry-run]

Default dir: ~/.config/per-device-eq/profiles
Each converted file is backed up next to itself as <name>.json.v1 first;
already-v2 files are left untouched. Run once, then forget v1 existed.
"""
import json
import os
import sys

SCHEMA_VERSION = 2      # keep in sync with perdeviceeq.config.SCHEMA_VERSION
DEFAULT_DIR = os.path.expanduser("~/.config/per-device-eq/profiles")


def migrate_body(p):
    """v1 body -> v2 body. Idempotent: v2 bodies pass through unchanged."""
    if p.get("version") == SCHEMA_VERSION:
        return p
    if p.get("apply_all", True):
        applied = ["all"]
    else:
        applied = list(p.get("ch_keys")
                       or (p.get("channels") or {}).keys()) or ["all"]
    vals = []
    for k in applied:
        s = (p.get("all") if k == "all"
             else (p.get("channels") or {}).get(k)) or {}
        vals.append(float(s.get("preamp", 0.0)))

    def strip(s):
        return {"bands": list((s or {}).get("bands") or [])}

    out = dict(p)
    out["version"] = SCHEMA_VERSION
    out["preamp"] = min(vals) if vals else 0.0
    out["all"] = strip(p.get("all"))
    out["channels"] = {k: strip(v)
                       for k, v in (p.get("channels") or {}).items()}
    return out


def convert_dir(d, dry_run=False):
    """Convert every v1 *.json in `d`. Returns (converted, skipped_v2,
    failed) counts; prints one line per file."""
    converted = skipped = failed = 0
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(d, fn)
        try:
            with open(path, encoding="utf-8") as f:
                p = json.load(f)
            if not isinstance(p, dict):
                raise ValueError("not a JSON object")
        except Exception as e:
            print("FAIL  %s (%s)" % (path, e))
            failed += 1
            continue
        if p.get("version") == SCHEMA_VERSION:
            print("ok    %s (already v2)" % path)
            skipped += 1
            continue
        out = migrate_body(p)
        print("v1>v2 %s (preamp %.1f dB)%s"
              % (path, out["preamp"], "  [dry run]" if dry_run else ""))
        if not dry_run:
            bak = path + ".v1"
            if not os.path.exists(bak):
                os.replace(path, bak)
            else:                              # re-run after a failed write
                os.remove(path) if os.path.exists(path) else None
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(out, f, indent=2, ensure_ascii=False)
            os.replace(tmp, path)
        converted += 1
    return converted, skipped, failed


def main():
    args = [a for a in sys.argv[1:]]
    dry = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]
    if len(args) > 1:
        sys.exit(__doc__)
    d = args[0] if args else DEFAULT_DIR
    if not os.path.isdir(d):
        sys.exit("no such directory: %s" % d)
    c, s, f = convert_dir(d, dry_run=dry)
    print("converted %d, already v2 %d, failed %d%s"
          % (c, s, f, "  (dry run, nothing written)" if dry else ""))
    if c and not dry:
        print("v1 originals kept as *.json.v1 next to the converted files")


if __name__ == "__main__":
    main()
