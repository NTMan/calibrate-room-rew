# -*- coding: utf-8 -*-
"""CLI command implementations (no GTK). These back the --list / --list-profiles
/ --inspect / --apply flags; argument parsing and dispatch live in the launcher.
"""

import json, sys

from .config import CLEAN_ID
from .profiles import ProfileStore
from .pipewire import list_sinks, node_params, metadata_set, metadata_clear


def cmd_list():
    for s in list_sinks():
        mark = "*" if s["default"] else " "
        print("%s[%4d] %s\t%s" % (mark, s["id"], s["name"], s["desc"]))
    return 0

def cmd_list_profiles():
    store = ProfileStore()
    rev = {}
    for node, pid in store.bindings.items():
        rev.setdefault(pid, []).append(node)
    for p in store.ordered():
        kind = "clean" if p["id"] == CLEAN_ID else ("builtin" if p["builtin"] else "user")
        bound = rev.get(p["id"], [])
        extra = ("  <- " + ", ".join(bound)) if bound else ""
        print("[%-7s] %-28s %s%s" % (kind, p["name"], p["id"], extra))
    return 0

def cmd_inspect(name):
    params, nid = node_params(name)
    if nid is None:
        print("sink not found: %s" % name, file=sys.stderr)
        return 1
    print("Sink id=%s name=%s\n" % (nid, name))
    print(json.dumps(params, indent=2, ensure_ascii=False))
    return 0

def cmd_apply():
    """Push every bound device's graph into the 'per-device-eq' metadata; the WP
    hook applies it. Requires the hook to be installed (run --install-hook once)."""
    store = ProfileStore()
    presets = store.presets()
    n = 0
    for node, pid in store.bindings.items():
        if not pid or pid == CLEAN_ID:
            metadata_clear(node)
            continue
        graph = presets.get(node)
        if graph is None:
            continue
        ok = metadata_set(node, graph)
        print("%s %s -> %s" % ("metadata" if ok else "FAILED  ", node, pid))
        n += 1 if ok else 0
    if not n:
        print("nothing applied (is the hook installed? run --install-hook)")
    return 0 if n else 1
