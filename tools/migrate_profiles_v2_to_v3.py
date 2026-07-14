#!/usr/bin/env python3
"""One-shot migration of per-device-eq profiles: schema v2 -> v3.

v3 keeps the v2 playback body verbatim (one shared preamp, per-channel
bands) and adds room for the optional dict blocks the app writes from
now on: "provenance", "device", "fit" and "measurement" (the canvas).
Converting an existing file therefore only restamps "version" and marks
its provenance as legacy -- nothing about how it sounds changes. The
app reads only v3 files and points here when it skips a v2 one.

v1 files (per-slot preamps, no version stamp) are refused: run
tools/migrate_profiles_v1_to_v2.py first, then this tool.

Usage:
    migrate_profiles_v2_to_v3.py [profiles-dir] [--dry-run]

Default dir: ~/.config/per-device-eq/profiles
Each converted file is backed up next to itself as <name>.json.v2 first;
already-v3 files are left untouched. Run once, then forget v2 existed.
"""
import json
import os
import sys

SCHEMA_VERSION = 3      # keep in sync with perdeviceeq.config.SCHEMA_VERSION
DEFAULT_DIR = os.path.expanduser("~/.config/per-device-eq/profiles")


def migrate_body(p):
    """v2 body -> v3 body. Idempotent: v3 bodies pass through unchanged;
    anything older than v2 is refused with a pointer to the v1 tool."""
    if p.get("version") == SCHEMA_VERSION:
        return p
    if p.get("version") != 2:
        raise ValueError(
            "schema v%s; run tools/migrate_profiles_v1_to_v2.py first"
            % p.get("version", 1))
    out = dict(p)
    out["version"] = SCHEMA_VERSION
    out.setdefault("provenance", {"kind": "legacy"})
    return out


def convert_dir(d, dry_run=False):
    """Convert every v2 *.json in `d`. Returns (converted, skipped_v3,
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
            if p.get("version") == SCHEMA_VERSION:
                print("ok    %s (already v3)" % path)
                skipped += 1
                continue
            out = migrate_body(p)
        except Exception as e:
            print("FAIL  %s (%s)" % (path, e))
            failed += 1
            continue
        print("v2>v3 %s%s" % (path, "  [dry run]" if dry_run else ""))
        if not dry_run:
            bak = path + ".v2"
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
    print("converted %d, already v3 %d, failed %d%s"
          % (c, s, f, "  (dry run, nothing written)" if dry else ""))
    if c and not dry:
        print("v2 originals kept as *.json.v2 next to the converted files")


if __name__ == "__main__":
    main()
