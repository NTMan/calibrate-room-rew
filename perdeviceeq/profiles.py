# -*- coding: utf-8 -*-
"""Profile store: load reusable EQ profiles (system read-only + user
read-write), the built-in Clean profile, and the node.name -> profile bindings.

Schema v3 = the v2 playback body (one shared preamp, per-channel bands)
plus four OPTIONAL dict blocks the store carries verbatim: `provenance`
(where the profile came from), `device` (what it corrects), `fit` (how
the bands were derived and from which takes) and `measurement` (the
canvas: the single rig with its per-capture-channel cal points, the
sessions, and the per-take uncalibrated magnitudes on the profile's
log grid). A profile without `measurement` is an import or a hand-made
one. The blocks' shape is owned by their producers; the store only
guarantees a save/load round-trip never strips them.

No GTK. Filesystem + JSON only.
"""

import hashlib
import os, sys, json, uuid

from .config import (SYS_PROFILE_DIRS, USER_PROFILES_DIR, BINDINGS_FILE,
                     CONFIG_DIR, CLEAN_ID, SCHEMA_VERSION, V3_BLOCKS)
from .eq import profile_graph, profile_has_content


def _new_id():
    return uuid.uuid4().hex[:12]


def _clean_profile():
    return {"id": CLEAN_ID, "name": "Clean (no EQ)", "apply_all": True,
            "version": SCHEMA_VERSION, "preamp": 0.0, "ch_keys": [],
            "all": {"bands": []}, "channels": {}, "builtin": True,
            "path": None}


PLAYBACK_KEYS = ("apply_all", "preamp", "ch_keys", "all", "channels")


def playback_sha256(p):
    """sha256 over the canonical playback body -- the five keys that
    make the sound. The fit stamps its output with this, so `edited`
    can be derived instead of sticky: diverge and it appears, undo
    back to the exact fitted sound and it clears."""
    body = {k: p.get(k) for k in PLAYBACK_KEYS}
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def editor_body(body, stored):
    """The editor rebuilds only the PLAYBACK body (it edits sound,
    not history). This reattaches the stored profile's v3 blocks so
    a debounced save can never strip a canvas, and keeps the fit's
    `edited` mark truthful. A fit carrying output_sha256 (the hash
    of the playback body it produced) gets the mark DERIVED: diverge
    and it appears, undo back to the exact fitted sound and it
    clears. A fit from before the output hash falls back to the
    sticky rule: any divergence sets the mark, only a re-fit clears
    it."""
    out = dict(body)
    for key in V3_BLOCKS:
        block = (stored or {}).get(key)
        if isinstance(block, dict) and block and key not in out:
            out[key] = block
    fit = out.get("fit")
    if isinstance(fit, dict):
        ref = fit.get("output_sha256")
        if ref:                     # derived: undo clears the mark
            ed = playback_sha256(out) != ref
            if bool(fit.get("edited")) != ed:
                out["fit"] = dict(fit, edited=ed)
        elif (isinstance(stored, dict) and not fit.get("edited")
                and any(out.get(k) != stored.get(k)
                        for k in PLAYBACK_KEYS)):
            out["fit"] = dict(fit, edited=True)   # pre-hash fits
    return out


class ProfileStore:
    """Loads profiles from system (read-only) + user dirs and the bindings map.
    A built-in Clean profile is always present. 'No binding == Clean'."""
    def __init__(self):
        self.profiles = {}
        self.bindings = {}
        self.reload()

    def reload(self):
        self.profiles = {}
        for d in SYS_PROFILE_DIRS:          # system first (read-only)
            self._load_dir(d, builtin=True)
        self._load_dir(USER_PROFILES_DIR, builtin=False)   # user can override
        if CLEAN_ID not in self.profiles:
            self.profiles[CLEAN_ID] = _clean_profile()
        else:
            self.profiles[CLEAN_ID]["builtin"] = True
        self.bindings = self._load_bindings()

    def _load_dir(self, d, builtin):
        if not os.path.isdir(d):
            return
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(d, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    p = json.load(f)
            except Exception:
                continue
            if not isinstance(p, dict):
                continue
            if p.get("version") != SCHEMA_VERSION:
                print("per-device-eq: skipping %s (profile schema v%s; run "
                      "tools/migrate_profiles_v2_to_v3.py once to convert)"
                      % (path, p.get("version", 1)), file=sys.stderr)
                continue
            pid = p.get("id") or os.path.splitext(fn)[0]
            p["id"] = pid
            p.setdefault("name", pid)
            p.setdefault("apply_all", True)
            p.setdefault("all", {"preamp": 0.0, "bands": []})
            p.setdefault("channels", {})
            p.setdefault("ch_keys", [])
            p["builtin"] = builtin
            p["path"] = path
            self.profiles[pid] = p          # user dir overrides system on id clash

    def get(self, pid):
        return self.profiles.get(pid) or self.profiles[CLEAN_ID]

    def ordered(self):
        def key(p):
            grp = 0 if p["id"] == CLEAN_ID else (1 if p["builtin"] else 2)
            return (grp, p["name"].lower())
        return sorted(self.profiles.values(), key=key)

    @staticmethod
    def _sane_slot(s):
        return {"bands": list((s or {}).get("bands") or [])}

    @classmethod
    def _body(cls, p):
        body = {"id": p["id"], "name": p.get("name", p["id"]),
                "version": SCHEMA_VERSION,
                "apply_all": bool(p.get("apply_all", True)),
                "preamp": float(p.get("preamp", 0.0)),
                "ch_keys": list(p.get("ch_keys") or []),
                "all": cls._sane_slot(p.get("all")),
                "channels": {k: cls._sane_slot(v)
                             for k, v in (p.get("channels") or {}).items()}}
        for key in V3_BLOCKS:            # carried verbatim, never made up
            block = p.get(key)
            if isinstance(block, dict) and block:
                body[key] = block
        return body

    def save_user(self, prof):
        """Write/overwrite a user profile (.json named by id). Returns the id."""
        os.makedirs(USER_PROFILES_DIR, exist_ok=True)
        pid = prof.get("id") or _new_id()
        body = self._body({**prof, "id": pid})
        path = os.path.join(USER_PROFILES_DIR, "%s.json" % pid)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(body, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        rec = dict(body); rec["builtin"] = False; rec["path"] = path
        self.profiles[pid] = rec
        return pid

    def delete_user(self, pid):
        p = self.profiles.get(pid)
        if not p or p.get("builtin") or not p.get("path"):
            return False
        try:
            os.remove(p["path"])
        except OSError:
            pass
        self.profiles.pop(pid, None)
        # any bindings pointing here fall back to Clean (drop the entry)
        for node in [n for n, i in self.bindings.items() if i == pid]:
            self.bindings.pop(node, None)
        self.save_bindings()
        return True

    # ---- bindings ----
    def _load_bindings(self):
        try:
            with open(BINDINGS_FILE, encoding="utf-8") as f:
                b = json.load(f)
            return {k: v for k, v in b.items()} if isinstance(b, dict) else {}
        except Exception:
            return {}

    def save_bindings(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = BINDINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.bindings, f, indent=2, ensure_ascii=False)
        os.replace(tmp, BINDINGS_FILE)

    def binding_for(self, node):
        return self.bindings.get(node)

    def set_binding(self, node, pid):
        if not node:
            return
        if pid is None or pid == CLEAN_ID:   # no binding == Clean
            self.bindings.pop(node, None)
        else:
            self.bindings[node] = pid
        self.save_bindings()

    def graph_for_node(self, node):
        pid = self.bindings.get(node)
        if not pid or pid == CLEAN_ID:
            return None                      # hook leaves the node alone
        p = self.profiles.get(pid)
        return profile_graph(p) if p else None

    def presets(self):
        """{node.name: graph_string} for every node bound to a non-Clean,
        content-ful profile. Pushed into the metadata (--apply, and the one-time
        migration of existing bindings into the hook's persistent state)."""
        out = {}
        for node, pid in self.bindings.items():
            if not pid or pid == CLEAN_ID:
                continue
            p = self.profiles.get(pid)
            if not p or not profile_has_content(p):
                continue
            out[node] = profile_graph(p)
        return out
