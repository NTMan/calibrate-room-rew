#!/usr/bin/env python3
"""One-shot migration of per-device-eq profiles: schema v3 -> v4.

v4 unglues provenance from correction on the measurement canvas:
measurement.cal_library holds every cal curve ever consumed
(append-only, keyed by the file's sha256), each take carries its own
cal_sha (null = raw), and each session wears the rig stamp -- name,
serial, node_match, channels -- so the takes reach their rig through
the session id they already carry. The single mutable
measurement.source block (whose per-column cal map was REPLACED on
every commit, destroying history) is deleted: the lift moves its cal
entries into the library, stamps its rig onto every session, and
hangs a cal_sha on every take by its capture column. The backfill is
correct for every canvas born under the old append gate (one rig,
one cal per column); a canvas mixed in the short gateless window is
labeled with its first rig wholesale -- its sessions never recorded
a source, so there is nothing truer to copy.

The stored fit is preserved honestly: when its fingerprint verified
against the v3 shape (the fit was fresh), it is recomputed under the
v4 shape, value-preserving -- same takes, same cal curves, same
parameters. A fit already stale under v3 keeps its old fingerprint
and stays stale; recomputing it would launder a lie (v3's cal
replacement destroyed the very points that fit consumed).

Profiles without a measurement canvas only get their version
restamped. v2 and older files are refused: run
tools/migrate_profiles_v2_to_v3.py first, then this tool.

Usage:
    migrate_profiles_v3_to_v4.py [profiles-dir] [--dry-run]

Default dir: ~/.config/per-device-eq/profiles
Each converted file is backed up next to itself as <name>.json.v3
first; already-v4 files are left untouched. Run once, then forget v3
existed.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from perdeviceeq.measure_build import fit_fingerprint  # noqa: E402

SCHEMA_VERSION = 4      # keep in sync with perdeviceeq.config
DEFAULT_DIR = os.path.expanduser("~/.config/per-device-eq/profiles")


def _v3_fingerprint(measurement, take_ids, params):
    """The v3 fingerprint, ported verbatim so freshness can be
    judged BEFORE the lift: {column: sha} over source.cal."""
    import hashlib
    wanted = set(take_ids)
    takes = {t["id"]: t["mag_db_uncal"]
             for t in measurement.get("takes", [])
             if t["id"] in wanted}
    cal = {c: (e or {}).get("sha256")
           for c, e in ((measurement.get("source") or {})
                        .get("cal") or {}).items()}
    blob = json.dumps({"grid": measurement.get("grid"), "cal": cal,
                       "takes": takes, "params": params},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def migrate_body(p):
    """v3 body -> v4 body. Idempotent: v4 bodies pass through
    unchanged; anything older than v3 is refused with a pointer to
    the v2 tool."""
    if p.get("version") == SCHEMA_VERSION:
        return p
    if p.get("version") != 3:
        raise ValueError(
            "schema v%s; run tools/migrate_profiles_v2_to_v3.py first"
            % p.get("version", 1))
    out = dict(p)
    out["version"] = SCHEMA_VERSION
    m = out.get("measurement")
    if not m:
        return out

    fit = out.get("fit") or {}
    fresh = bool(
        fit.get("inputs_sha256")
        and _v3_fingerprint(m, fit.get("takes") or [],
                            fit.get("params") or {})
        == fit["inputs_sha256"])

    m = dict(m)
    src = dict(m.get("source") or {})
    lib = dict(m.get("cal_library") or {})
    col_sha = {}
    for col, e in (src.get("cal") or {}).items():
        sha = (e or {}).get("sha256")
        if not sha:
            continue
        lib.setdefault(sha, {"file": e.get("file"),
                             "points": e.get("points") or []})
        col_sha[str(col)] = sha
    m["cal_library"] = lib
    m["takes"] = [
        dict(t, cal_sha=(t.get("cal_sha") if "cal_sha" in t else
                         col_sha.get(str(t.get("capture_channel")))))
        for t in (m.get("takes") or [])]
    stamp = {k: src.get(k) for k in ("name", "serial",
                                     "node_match", "channels")}
    m["sessions"] = {
        sid: (blk if (blk or {}).get("source")
              else dict(blk or {}, source=dict(stamp)))
        for sid, blk in (m.get("sessions") or {}).items()}
    del m["source"]
    out["measurement"] = m

    if fresh:
        out = dict(out)
        out["fit"] = dict(fit)
        out["fit"]["inputs_sha256"] = fit_fingerprint(
            m, fit.get("takes") or [], fit.get("params") or {})
    return out


def convert_dir(d, dry_run=False):
    """Convert every v3 *.json in `d`. Returns (converted,
    skipped_v4, failed) counts; prints one line per file."""
    converted = skipped = failed = 0
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(d, fn)
        try:
            with open(path, "r", encoding="utf-8") as f:
                body = json.load(f)
        except (OSError, ValueError) as e:
            print("  FAIL  %s: %s" % (fn, e))
            failed += 1
            continue
        if not isinstance(body, dict):
            print("  FAIL  %s: not a profile object" % fn)
            failed += 1
            continue
        if body.get("version") == SCHEMA_VERSION:
            print("  keep  %s (already v%d)" % (fn, SCHEMA_VERSION))
            skipped += 1
            continue
        try:
            out = migrate_body(body)
        except ValueError as e:
            print("  FAIL  %s: %s" % (fn, e))
            failed += 1
            continue
        if dry_run:
            print("  would %s" % fn)
            converted += 1
            continue
        backup = path + ".v3"
        if not os.path.exists(backup):
            os.replace(path, backup)
        else:
            os.remove(path)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1, ensure_ascii=False)
            f.write("\n")
        print("  conv  %s (backup: %s)" % (fn,
                                           os.path.basename(backup)))
        converted += 1
    return converted, skipped, failed


def main(argv):
    args = [a for a in argv[1:] if a != "--dry-run"]
    dry = "--dry-run" in argv[1:]
    d = args[0] if args else DEFAULT_DIR
    if not os.path.isdir(d):
        print("no such directory: %s" % d, file=sys.stderr)
        return 2
    conv, skip, fail = convert_dir(d, dry_run=dry)
    print("%d converted, %d already v%d, %d failed"
          % (conv, skip, SCHEMA_VERSION, fail))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
