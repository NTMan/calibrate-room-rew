#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
per-device-eq -- per-output-device parametric EQ for PipeWire 1.6+, with a
library of reusable EQ *profiles*.

EQ lives INSIDE the real sink via the in-node filter-graph Props param
"audioconvert.filter-graph.0" (index required; value is an INLINE graph string).
It instantiates only when the node is running with a negotiated format, so live
edits go through `pw-cli set-param` on the running node, and persistence is a
WirePlumber Lua hook that re-applies the saved graph when the node starts
playing (survives reboot / hotplug / reconnect). No phantom node, no daemon.

Profiles (one .json file each):
  * system  (read-only): /usr/share/per-device-eq/profiles + ./profiles
  * user (read-write):   ~/.config/per-device-eq/profiles
A built-in "Clean" profile is always present (flat / no EQ).
Bindings (~/.config/per-device-eq/bindings.json) map node.name -> profile id;
no binding == Clean (the hook does not touch the node).

  --list                 list sinks (default marked with *)
  --list-profiles        list known profiles
  --inspect <node.name>  dump node params (pw-dump info.params)
  --apply                apply each bound profile to its sink now
  --install-hook         (re)install the WirePlumber hook + config
  (no args)              launch the GTK4 GUI
"""

import argparse, json, math, cmath, os, re, subprocess, sys, uuid

FS = 48000.0
CONFIG_DIR        = os.path.expanduser("~/.config/per-device-eq")
USER_PROFILES_DIR = os.path.join(CONFIG_DIR, "profiles")
BINDINGS_FILE     = os.path.join(CONFIG_DIR, "bindings.json")
PRESETS_LUA       = os.path.join(CONFIG_DIR, "presets.lua")
LEGACY_CONFIG     = os.path.join(CONFIG_DIR, "config.json")

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SYS_PROFILE_DIRS = ["/usr/share/per-device-eq/profiles",
                    os.path.join(_SCRIPT_DIR, "profiles")]

WP_SCRIPT_DIR = os.path.expanduser("~/.local/share/wireplumber/scripts")
WP_SCRIPT     = os.path.join(WP_SCRIPT_DIR, "90-per-device-eq.lua")
WP_CONF_DIR   = os.path.expanduser("~/.config/wireplumber/wireplumber.conf.d")
WP_CONF       = os.path.join(WP_CONF_DIR, "90-per-device-eq.conf")

TYPE_TO_LABEL = {"PK": "bq_peaking", "LSC": "bq_lowshelf", "HSC": "bq_highshelf"}
TYPE_NAMES = ["PK", "LSC", "HSC"]
CLEAN_ID = "clean"

HOOK_LUA = r'''-- 90-per-device-eq.lua (auto-written by per-device-eq) -- manifest-driven.
local CONFIG_DIR = (os.getenv("XDG_CONFIG_HOME")
                    or ((os.getenv("HOME") or "") .. "/.config"))
local PRESETS_FILE = CONFIG_DIR .. "/per-device-eq/presets.lua"
local log = Log.open_topic("pde")
local applied = {}
local function load_presets()
  local ok, t = pcall(dofile, PRESETS_FILE)
  if ok and type(t) == "table" then return t end
  return {}
end
local function apply(node, name, graph)
  local ok, err = pcall(function()
    node:set_param("Props", Pod.Object {
      "Spa:Pod:Object:Param:Props", "Props",
      params = Pod.Struct { "audioconvert.filter-graph.0", graph },
    })
  end)
  log:info("apply " .. name .. " -> " .. (ok and "OK" or ("FAILED: " .. tostring(err))))
end
local function schedule(node, name)
  Core.timeout_add(300, function()
    local presets = load_presets()
    local graph = presets[name]
    if graph and node:get_state() == "running" and not applied[name] then
      local nout = select(1, node:get_n_output_ports())
      if nout and nout >= 1 then applied[name] = true; apply(node, name, graph) end
    end
    return false
  end)
end
pde_om = ObjectManager {
  Interest { type = "node", Constraint { "media.class", "equals", "Audio/Sink", type = "pw-global" } }
}
pde_om:connect("object-added", function(_, node)
  local name = tostring(node.properties["node.name"])
  pcall(function()
    node:connect("state-changed", function(n, _old, new)
      if new == "running" then schedule(n, name) else applied[name] = nil end
    end)
  end)
  if node:get_state() == "running" then schedule(node, name) end
end)
pde_om:activate()
log:info("per-device-eq hook loaded")
'''

HOOK_CONF = ('wireplumber.components = [\n'
             '  { name = "90-per-device-eq.lua", type = script/lua, provides = per-device-eq }\n'
             ']\n'
             'wireplumber.profiles = {\n'
             '  main = {\n'
             '    per-device-eq = required\n'
             '  }\n'
             '}\n')


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


# ============================ profile store ============================
def _new_id():
    return uuid.uuid4().hex[:12]


def _clean_profile():
    return {"id": CLEAN_ID, "name": "Clean (no EQ)", "apply_all": True,
            "ch_keys": [], "all": {"preamp": 0.0, "bands": []},
            "channels": {}, "builtin": True, "path": None}


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
    def _body(p):
        return {"id": p["id"], "name": p.get("name", p["id"]),
                "apply_all": bool(p.get("apply_all", True)),
                "ch_keys": list(p.get("ch_keys") or []),
                "all": p.get("all") or {"preamp": 0.0, "bands": []},
                "channels": p.get("channels") or {}}

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

    def regenerate_presets(self):
        """presets.lua for the hook: one entry per node bound to a non-Clean,
        existing profile."""
        os.makedirs(CONFIG_DIR, exist_ok=True)
        lines = ["-- auto-generated by per-device-eq; do not edit by hand", "return {"]
        for node, pid in self.bindings.items():
            if not pid or pid == CLEAN_ID:
                continue
            p = self.profiles.get(pid)
            if not p or not profile_has_content(p):
                continue
            lines.append('  ["%s"] = [==[%s]==],' % (node, profile_graph(p)))
        lines.append("}")
        tmp = PRESETS_LUA + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, PRESETS_LUA)


# ============================ PipeWire helpers ============================
def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)

def pw_dump():
    try:
        return json.loads(_run(["pw-dump"]).stdout)
    except Exception:
        return []

def default_sink_name():
    try:
        out = _run(["pw-metadata", "-n", "default", "0", "default.audio.sink"]).stdout
        m = re.search(r"value:'?(\{.*?\})'?", out)
        if m:
            return json.loads(m.group(1)).get("name")
    except Exception:
        pass
    return None

def list_sinks(dump=None):
    dump = dump if dump is not None else pw_dump()
    default = default_sink_name()
    sinks = []
    for o in dump:
        if o.get("type") != "PipeWire:Interface:Node":
            continue
        p = (o.get("info") or {}).get("props") or {}
        if p.get("media.class") == "Audio/Sink":
            name = p.get("node.name")
            if not name:
                continue
            sinks.append({"id": o["id"], "name": name,
                          "desc": p.get("node.description") or name,
                          "prio": p.get("priority.session") or 0,
                          "default": name == default})
    sinks.sort(key=lambda s: -(s["prio"] or 0))
    return sinks

def node_params(name, dump=None):
    dump = dump if dump is not None else pw_dump()
    for o in dump:
        if o.get("type") != "PipeWire:Interface:Node":
            continue
        p = (o.get("info") or {}).get("props") or {}
        if p.get("node.name") == name:
            return (o.get("info") or {}).get("params") or {}, o["id"]
    return None, None

def resolve_sink_id(name, dump=None):
    _, nid = node_params(name, dump)
    return nid

def graph_loaded(name, dump=None):
    """Best-effort: a loaded in-node graph shows up as an extra Props block
    whose only key is 'params'."""
    params, _ = node_params(name, dump)
    if not params:
        return False
    for d in params.get("Props", []):
        if isinstance(d, dict) and list(d.keys()) == ["params"]:
            return True
    return False

def set_graph(node_id, graph):
    props = '{ params = [ "audioconvert.filter-graph.0" "%s" ] }' % graph
    r = _run(["pw-cli", "set-param", str(node_id), "Props", props])
    return r.returncode == 0 and "Error" not in (r.stdout + r.stderr)

_POS_FALLBACK = ["FL", "FR", "FC", "LFE", "RL", "RR", "SL", "SR"]

def sink_channels(name, dump=None):
    """Channel keys for a sink (e.g. ['FL','FR']) from its negotiated Format
    position, falling back to channelVolumes length, then stereo."""
    params, _ = node_params(name, dump)
    pos, nch = None, None
    if params:
        for blk in (params.get("Format") or []):
            if isinstance(blk, dict):
                if blk.get("position"):
                    pos = blk["position"]
                if blk.get("channels"):
                    nch = blk["channels"]
        if nch is None:
            for d in (params.get("Props") or []):
                if isinstance(d, dict) and isinstance(d.get("channelVolumes"), list):
                    nch = len(d["channelVolumes"])
    if isinstance(pos, list) and pos:
        keys = [str(p) for p in pos]
    elif nch:
        keys = _POS_FALLBACK[:nch] if nch <= len(_POS_FALLBACK) \
               else ["Ch%d" % (i + 1) for i in range(nch)]
    else:
        keys = ["FL", "FR"]
    seen, out = {}, []
    for k in keys:
        if k in seen:
            seen[k] += 1; out.append("%s.%d" % (k, seen[k]))
        else:
            seen[k] = 0; out.append(k)
    return out


# ============================ WirePlumber hook ============================
def install_hook():
    """Write the WP hook script + component config if missing/changed.
    Returns True if anything was newly written."""
    changed = False
    os.makedirs(WP_SCRIPT_DIR, exist_ok=True)
    os.makedirs(WP_CONF_DIR, exist_ok=True)
    for path, content in ((WP_SCRIPT, HOOK_LUA), (WP_CONF, HOOK_CONF)):
        old = None
        if os.path.exists(path):
            try:
                with open(path) as f:
                    old = f.read()
            except Exception:
                old = None
        if old != content:
            with open(path, "w") as f:
                f.write(content)
            changed = True
    return changed

def restart_wireplumber():
    return _run(["systemctl", "--user", "restart", "wireplumber"]).returncode == 0


# ============================ CLI ============================
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
    store = ProfileStore()
    dump = pw_dump()
    n = 0
    for node, pid in store.bindings.items():
        if not pid or pid == CLEAN_ID:
            continue
        nid = resolve_sink_id(node, dump)
        if nid is None:
            print("skip (not present): %s" % node)
            continue
        graph = store.graph_for_node(node)
        if graph is None:
            continue
        ok = set_graph(nid, graph)
        print("%s %s -> %s" % ("applied" if ok else "FAILED ", node, pid))
        n += 1 if ok else 0
    return 0 if n else 1


# ============================ GUI ============================
def launch_gui():
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk, GLib, Gio, Gdk, Pango

    DB_MAX = 24.0
    FMIN, FMAX = 20.0, 20000.0

    def log_freqs(n=260):
        a, b = math.log10(FMIN), math.log10(FMAX)
        return [10 ** (a + (b - a) * i / (n - 1)) for i in range(n)]

    class EQWindow(Gtk.ApplicationWindow):
        def __init__(self, app):
            super().__init__(application=app, title="Per-Device EQ")
            self.set_default_size(1000, 700)
            self.store = ProfileStore()
            self.sinks = []
            self.current = None              # node.name
            self._profile_id = None          # currently loaded profile id
            self._apply_all = True
            self._ch_keys = ["FL", "FR"]
            self._ch_store = {"all": {"preamp": 0.0, "bands": []}}
            self._switch_idx = None
            self.bypass = False
            self._loading = False
            self._apply_src = 0
            self._undo = []          # editor-body snapshots (current session)
            self._redo = []
            self._restoring = False
            self._freqs = log_freqs()
            self._plot = None
            self._drag_band = None

            root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            root.set_margin_top(10); root.set_margin_bottom(10)
            root.set_margin_start(10); root.set_margin_end(10)
            self.set_child(root)

            # ---- top: device + toggles ----
            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            root.append(top)
            top.append(Gtk.Label(label="Device:"))
            self.dev_model = Gtk.StringList()
            self.dev_dd = Gtk.DropDown(model=self.dev_model)
            self.dev_dd.set_hexpand(True)
            self.dev_dd.connect("notify::selected", self.on_device_changed)
            top.append(self.dev_dd)
            self.follow_sw = Gtk.Switch(valign=Gtk.Align.CENTER)
            self.follow_sw.connect("notify::active", self.on_follow_toggled)
            top.append(self._labeled("Follow default", self.follow_sw))
            self.bypass_sw = Gtk.Switch(valign=Gtk.Align.CENTER)
            self.bypass_sw.connect("notify::active", self.on_bypass_toggled)
            top.append(self._labeled("Bypass", self.bypass_sw))
            self.allch_chk = Gtk.CheckButton(label="Apply to all channels")
            self.allch_chk.set_active(True)
            self.allch_chk.connect("toggled", self.on_allch_toggled)
            top.append(self.allch_chk)

            # ---- main split: editor (left) | profile library (right) ----
            self._hpaned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
            self._hpaned.set_wide_handle(True)
            self._hpaned.set_vexpand(True)
            root.append(self._hpaned)

            left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            self._hpaned.set_start_child(left)
            self._hpaned.set_resize_start_child(True)
            self._hpaned.set_shrink_start_child(False)

            # undo / redo toolbar, above the tabs and graph
            tools = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            self.undo_b = Gtk.Button(label="\u21b6")
            self.undo_b.set_tooltip_text("Undo (Ctrl+Z)")
            self.undo_b.add_css_class("flat")
            self.undo_b.connect("clicked", self.on_undo)
            self.redo_b = Gtk.Button(label="\u21b7")
            self.redo_b.set_tooltip_text("Redo (Ctrl+Shift+Z)")
            self.redo_b.add_css_class("flat")
            self.redo_b.connect("clicked", self.on_redo)
            tools.append(self.undo_b); tools.append(self.redo_b)
            left.append(tools)

            # channel selector (tabs); shown only when not "apply to all"
            self._notebook = Gtk.Notebook()
            self._notebook.set_show_border(False)
            self._notebook.set_scrollable(True)
            self._nb_handler = self._notebook.connect("switch-page", self.on_tab_switched)
            self._notebook.set_visible(False)
            left.append(self._notebook)

            # FR graph (top) and table (bottom) share a draggable splitter
            self.area = Gtk.DrawingArea()
            self.area.set_content_height(150)
            self.area.set_hexpand(True); self.area.set_vexpand(True)
            self.area.set_draw_func(self.draw_fr)
            drag = Gtk.GestureDrag()
            drag.connect("drag-begin", self._on_drag_begin)
            drag.connect("drag-update", self._on_drag_update)
            drag.connect("drag-end", self._on_drag_end)
            self.area.add_controller(drag)
            rclick = Gtk.GestureClick(); rclick.set_button(3)
            rclick.connect("pressed", self._on_right_click)
            self.area.add_controller(rclick)
            self.area.set_cursor(Gdk.Cursor.new_from_name("crosshair", None))
            frame = Gtk.Frame(); frame.set_child(self.area)

            self._vpaned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
            self._vpaned.set_wide_handle(True); self._vpaned.set_vexpand(True)
            self._vpaned.set_start_child(frame)
            self._vpaned.set_resize_start_child(True)
            self._vpaned.set_shrink_start_child(False)
            self._table_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            self._vpaned.set_end_child(self._table_box)
            self._vpaned.set_resize_end_child(True)
            self._vpaned.set_shrink_end_child(False)
            left.append(self._vpaned)

            # preamp
            pre = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            self._table_box.append(pre)
            pre.append(Gtk.Label(label="Preamp (dB):"))
            self.preamp_spin = Gtk.SpinButton.new_with_range(-24.0, 24.0, 0.1)
            self.preamp_spin.set_digits(1)
            self.preamp_spin.connect("value-changed", self.on_preamp_changed)
            self._no_scroll(self.preamp_spin)
            pre.append(self.preamp_spin)
            auto_pre = Gtk.Button(label="Auto")
            auto_pre.set_tooltip_text("Set preamp to -(peak of the EQ curve) so the "
                                      "EQ does not raise overall level / clip")
            auto_pre.connect("clicked", self.on_auto_preamp)
            pre.append(auto_pre)

            # band header (per-column size groups keep it aligned with the rows)
            self._colgroups = [Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
                               for _ in range(7)]
            hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            for i, txt in enumerate(("", "Type", "Freq (Hz)", "Gain (dB)", "Q", "On", "")):
                lbl = Gtk.Label(label=txt, xalign=0.0); lbl.add_css_class("dim-label")
                self._colgroups[i].add_widget(lbl); hdr.append(lbl)
            self._table_box.append(hdr)

            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroller.set_min_content_height(110); scroller.set_vexpand(True)
            self._rows_scroller = scroller
            self.rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            scroller.set_child(self.rows_box)
            self._table_box.append(scroller)

            # ---- right: profile library ----
            right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            right.set_size_request(240, -1)
            self._hpaned.set_end_child(right)
            self._hpaned.set_resize_end_child(False)
            self._hpaned.set_shrink_end_child(False)
            right.append(Gtk.Label(label="Profiles", xalign=0.0))
            self.psearch = Gtk.SearchEntry()
            self.psearch.set_placeholder_text("Search profiles…")
            self.psearch.connect("search-changed", lambda *_: self.plist.invalidate_filter())
            right.append(self.psearch)
            psc = Gtk.ScrolledWindow()
            psc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            psc.set_vexpand(True); psc.set_min_content_width(220)
            self.plist = Gtk.ListBox()
            self.plist.set_selection_mode(Gtk.SelectionMode.SINGLE)
            self.plist.set_filter_func(self._filter_row)
            self.plist.connect("row-selected", self.on_profile_selected)
            psc.set_child(self.plist)
            right.append(psc)
            pbtns = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            r1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, homogeneous=True)
            newb = Gtk.Button(label="New"); newb.connect("clicked", self.on_new_profile)
            dupb = Gtk.Button(label="Duplicate"); dupb.connect("clicked", self.on_duplicate)
            r1.append(newb); r1.append(dupb)
            r2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, homogeneous=True)
            self.rename_b = Gtk.Button(label="Rename")
            self.rename_b.connect("clicked", self.on_rename)
            self.delete_b = Gtk.Button(label="Delete")
            self.delete_b.add_css_class("destructive-action")
            self.delete_b.connect("clicked", self.on_delete)
            r2.append(self.rename_b); r2.append(self.delete_b)
            pbtns.append(r1); pbtns.append(r2)
            right.append(pbtns)

            # ---- bottom buttons ----
            btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            root.append(btns)
            add_b = Gtk.Button(label="Add band"); add_b.connect("clicked", self.on_add_band)
            btns.append(add_b)
            imp_b = Gtk.Button(label="Import REW/AutoEQ…"); imp_b.connect("clicked", self.on_import)
            btns.append(imp_b)
            expp_b = Gtk.Button(label="Export profile…")
            expp_b.set_tooltip_text("Export the whole profile as .json (to copy to "
                                    "another machine / share)")
            expp_b.connect("clicked", self.on_export_profile)
            btns.append(expp_b)
            expr_b = Gtk.Button(label="Export REW…")
            expr_b.set_tooltip_text("Export this channel in REW/AutoEQ text format")
            expr_b.connect("clicked", self.on_export_rew)
            btns.append(expr_b)
            spacer = Gtk.Box(); spacer.set_hexpand(True); btns.append(spacer)
            self.saved_lbl = Gtk.Label(label="", valign=Gtk.Align.CENTER)
            self.saved_lbl.add_css_class("dim-label")
            btns.append(self.saved_lbl)

            keyctl = Gtk.EventControllerKey()
            keyctl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            keyctl.connect("key-pressed", self._on_key)
            self.add_controller(keyctl)

            self._hook_installed = False
            self._fork_name_hint = None
            self._flash_src = 0
            self._rebuild_profile_list()
            self.reload_sinks()
            self.follow_sw.set_active(True)
            self._hpaned.set_position(720)
            self._vpaned.set_position(300)
            GLib.timeout_add_seconds(2, self._poll)

        # ---------- small helpers ----------
        def _labeled(self, text, widget):
            b = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            b.append(Gtk.Label(label=text)); b.append(widget)
            return b

        def _info(self, title, body):
            d = Gtk.AlertDialog()
            d.set_modal(True); d.set_message(title); d.set_detail(body)
            d.show(self)

        def _flash(self, text):
            self.saved_lbl.set_text(text)
            if self._flash_src:
                GLib.source_remove(self._flash_src)
            def revert():
                self._flash_src = 0
                self.saved_lbl.set_text(self._status_text())
                return False
            self._flash_src = GLib.timeout_add(1400, revert)

        # ---------- scroll guard (wheel must not change spin/dropdown values) ----------
        def _no_scroll(self, widget):
            sc = Gtk.EventControllerScroll(
                flags=Gtk.EventControllerScrollFlags.VERTICAL)
            sc.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            sc.connect("scroll", self._redirect_scroll)
            widget.add_controller(sc)

        def _redirect_scroll(self, _ctrl, _dx, dy):
            sc = getattr(self, "_rows_scroller", None)
            if sc is not None:
                adj = sc.get_vadjustment()
                if adj is not None:
                    step = adj.get_step_increment()
                    step = step if step and step > 1 else 40.0
                    new = adj.get_value() + dy * step
                    new = max(adj.get_lower(),
                              min(adj.get_upper() - adj.get_page_size(), new))
                    adj.set_value(new)
            return True   # consume -> the spin/dropdown never changes

        # ---------- undo / redo (autosave-friendly safety net) ----------
        def _snapshot(self):
            b = self._body_from_editor()
            return {"apply_all": b["apply_all"], "ch_keys": list(b["ch_keys"]),
                    "all": b["all"], "channels": b["channels"]}

        def _apply_body(self, body):
            self._apply_all = bool(body.get("apply_all", True))
            keys = body.get("ch_keys") or self._ch_keys or ["FL", "FR"]
            self._ch_keys = keys[:8]
            self._ch_store = {}
            a = body.get("all") or {"preamp": 0.0, "bands": []}
            self._ch_store["all"] = {"preamp": float(a.get("preamp", 0.0)),
                                     "bands": [Band.from_dict(x) for x in a.get("bands", [])]}
            for k, e in (body.get("channels") or {}).items():
                self._ch_store[k] = {"preamp": float(e.get("preamp", 0.0)),
                                     "bands": [Band.from_dict(x) for x in e.get("bands", [])]}
            if not self._apply_all:
                base = self._ch_store["all"]
                for k in self._ch_keys:
                    if k not in self._ch_store:
                        self._ch_store[k] = {"preamp": base["preamp"],
                                             "bands": [Band(x.type, x.freq, x.gain, x.q, x.enabled)
                                                       for x in base["bands"]]}
            self._loading = True
            self.allch_chk.set_active(self._apply_all)
            self._loading = False
            self._rebuild_tabs()
            self._reload_editor()

        def _reset_history(self):
            self._undo = [self._snapshot()]
            self._redo = []
            self._update_undo_buttons()

        def _push_history(self):
            snap = self._snapshot()
            if not self._undo or self._undo[-1] != snap:
                self._undo.append(snap)
                if len(self._undo) > 100:
                    self._undo.pop(0)
                self._redo = []
            self._update_undo_buttons()

        def _restore(self, snap):
            self._restoring = True
            self._apply_body(snap)
            self.apply_live()
            self._persist()
            self._restoring = False
            self._update_undo_buttons()
            self._update_status()
            self._flash("Reverted")

        def on_undo(self, *_):
            if len(self._undo) <= 1:
                return
            self._redo.append(self._undo.pop())
            self._restore(self._undo[-1])

        def on_redo(self, *_):
            if not self._redo:
                return
            snap = self._redo.pop()
            self._undo.append(snap)
            self._restore(snap)

        def _update_undo_buttons(self):
            if hasattr(self, "undo_b"):
                self.undo_b.set_sensitive(len(self._undo) > 1)
                self.redo_b.set_sensitive(bool(self._redo))

        def _on_key(self, _ctrl, keyval, _code, state):
            if not (state & Gdk.ModifierType.CONTROL_MASK):
                return False
            name = (Gdk.keyval_name(keyval) or "").lower()
            if name == "z" and (state & Gdk.ModifierType.SHIFT_MASK):
                self.on_redo(); return True
            if name == "z":
                self.on_undo(); return True
            if name == "y":
                self.on_redo(); return True
            return False

        # ---------- per-channel store ----------
        def _active_key(self):
            if self._apply_all or self._notebook is None:
                return "all"
            i = self._switch_idx if self._switch_idx is not None \
                else self._notebook.get_current_page()
            if 0 <= i < len(self._ch_keys):
                return self._ch_keys[i]
            return "all"

        def _active_set(self):
            return self._ch_store.setdefault(self._active_key(),
                                             {"preamp": 0.0, "bands": []})

        @property
        def bands(self):
            return self._active_set()["bands"]
        @bands.setter
        def bands(self, v):
            self._active_set()["bands"] = v
        @property
        def preamp(self):
            return self._active_set()["preamp"]
        @preamp.setter
        def preamp(self, v):
            self._active_set()["preamp"] = v

        def _set_has_content(self, store):
            return any(b.enabled for b in store.get("bands", [])) \
                   or abs(store.get("preamp", 0.0)) > 1e-9

        def _has_content(self):
            if self._apply_all:
                return self._set_has_content(self._ch_store.get("all") or {})
            for k in self._ch_keys:
                if self._set_has_content(self._ch_store.get(k) or {}):
                    return True
            return False

        enabled = property(lambda self: self._has_content())

        def _channel_sets(self):
            out = []
            for k in self._ch_keys:
                e = self._ch_store.get(k) or {"preamp": 0.0, "bands": []}
                out.append((e.get("preamp", 0.0), e.get("bands", [])))
            return out

        def _editor_graph(self):
            if self._apply_all:
                a = self._ch_store.get("all") or {"preamp": 0.0, "bands": []}
                return build_graph(a.get("preamp", 0.0), a.get("bands", []))
            return build_graph_channels(self._channel_sets())

        # ---------- serialize / compare ----------
        def _body_from_editor(self, pid=None, name=None):
            def sd(s):
                return {"preamp": s.get("preamp", 0.0),
                        "bands": [b.to_dict() for b in s.get("bands", [])]}
            pid = pid or self._profile_id or CLEAN_ID
            if name is None:
                name = self.store.get(pid).get("name", pid)
            return {"id": pid, "name": name, "apply_all": self._apply_all,
                    "ch_keys": list(self._ch_keys),
                    "all": sd(self._ch_store.get("all") or {}),
                    "channels": {k: sd(self._ch_store.get(k) or {}) for k in self._ch_keys}}

        # ---------- autosave (no explicit Save button) ----------
        def _unique_name(self, base):
            base = (base or "EQ").strip() or "EQ"
            names = {p["name"] for p in self.store.profiles.values()}
            if base not in names:
                return base
            i = 2
            while ("%s %d" % (base, i)) in names:
                i += 1
            return "%s %d" % (base, i)

        def _hook_once(self):
            if self._hook_installed:
                return
            self._hook_installed = True
            if install_hook():
                self._info("Hook installed",
                           "The WirePlumber hook was installed. Run once:\n\n"
                           "  systemctl --user restart wireplumber\n\n"
                           "After that, saved profiles are re-applied automatically "
                           "on playback (reboot / reconnect).")

        def _ensure_editable(self):
            """Return an editable (user) profile id for the current editor,
            forking the current built-in into a new draft on first real content.
            Returns None when there is nothing to persist (flat built-in)."""
            p = self.store.get(self._profile_id)
            if not p.get("builtin"):
                return self._profile_id
            if not self._has_content():
                return None
            name = self._unique_name(self._fork_name_hint or self._dev_desc() or "My EQ")
            self._fork_name_hint = None
            nid = self.store.save_user(self._body_from_editor(pid=_new_id(), name=name))
            self._profile_id = nid
            if self.current:
                self.store.set_binding(self.current, nid)
            self._hook_once()
            self._rebuild_profile_list()
            self._select_profile_row(nid)
            return nid

        def _persist(self):
            """Write the current editor to its (user) profile + refresh the hook.
            Built-ins are forked first; a flat editor on a built-in stays Clean."""
            pid = self._ensure_editable()
            if pid is None:
                if self.current:
                    self.store.set_binding(self.current, CLEAN_ID)
                self.store.regenerate_presets()
                return
            self.store.save_user(self._body_from_editor(
                pid=pid, name=self.store.get(pid).get("name", pid)))
            if self.current:
                self.store.set_binding(self.current, pid)
            self.store.regenerate_presets()
            self._hook_once()
            self._flash("Saved")

        def _update_pbtn_sensitivity(self):
            p = self.store.get(self._profile_id) if self._profile_id else None
            editable = bool(p and not p.get("builtin"))
            if hasattr(self, "rename_b"):
                self.rename_b.set_sensitive(editable)
                self.delete_b.set_sensitive(editable)

        def _status_text(self):
            p = self.store.get(self._profile_id) if self._profile_id else None
            if not p:
                return ""
            lock = " \U0001F512" if (p.get("builtin") and p["id"] != CLEAN_ID) else ""
            return p["name"] + lock

        def _update_status(self):
            if not hasattr(self, "saved_lbl"):
                return
            if not self._flash_src:
                self.saved_lbl.set_text(self._status_text())
            self._update_pbtn_sensitivity()
            dev = (" \u2014 %s" % self._dev_desc()) if self.current else ""
            self.set_title("Per-Device EQ%s" % dev)

        # ---------- sink list ----------
        def _index_of(self, name):
            for i, s in enumerate(self.sinks):
                if s["name"] == name:
                    return i
            return None

        def _rebuild_dev_labels(self):
            self._loading = True
            self.dev_model.splice(
                0, self.dev_model.get_n_items(),
                ["%s%s" % ("\u2605 " if s["default"] else "", s["desc"]) for s in self.sinks])
            self._loading = False

        def _set_selected(self, idx):
            self._loading = True
            self.dev_dd.set_selected(idx)
            self._loading = False

        def reload_sinks(self, prefer=None):
            self.sinks = list_sinks()
            self._rebuild_dev_labels()
            target = prefer
            if target is None and self.current and self._index_of(self.current) is not None:
                target = self.current
            if target is None:
                dn = default_sink_name()
                if dn and self._index_of(dn) is not None:
                    target = dn
            if target is None and self.sinks:
                target = self.sinks[0]["name"]
            if target is not None:
                self._set_selected(self._index_of(target) or 0)
                self.load_device(target)

        def _select_by_name(self, name):
            self._rebuild_dev_labels()
            idx = self._index_of(name)
            if idx is None:
                return
            self._set_selected(idx)
            self.load_device(name)

        def on_device_changed(self, *_):
            if self._loading:
                return
            i = self.dev_dd.get_selected()
            if 0 <= i < len(self.sinks):
                self.load_device(self.sinks[i]["name"])

        # ---------- profile list ----------
        def _filter_row(self, row):
            q = self.psearch.get_text().strip().lower()
            return (q in getattr(row, "_name", "").lower()) if q else True

        def _rebuild_profile_list(self):
            self._loading = True
            r = self.plist.get_first_child()
            while r:
                nx = r.get_next_sibling(); self.plist.remove(r); r = nx
            for p in self.store.ordered():
                row = Gtk.ListBoxRow()
                box = Gtk.Box(spacing=6)
                box.set_margin_top(3); box.set_margin_bottom(3)
                box.set_margin_start(6); box.set_margin_end(6)
                lock = "\U0001F512 " if (p["builtin"] and p["id"] != CLEAN_ID) else ""
                lbl = Gtk.Label(label=lock + p["name"], xalign=0.0)
                lbl.set_hexpand(True); lbl.set_ellipsize(Pango.EllipsizeMode.END)
                box.append(lbl)
                row.set_child(box)
                row._pid = p["id"]; row._name = p["name"]
                self.plist.append(row)
            self._loading = False
            self._select_profile_row(self._profile_id or CLEAN_ID)

        def _select_profile_row(self, pid):
            self._loading = True
            row = self.plist.get_first_child()
            sel = None
            while row:
                if getattr(row, "_pid", None) == pid:
                    sel = row; break
                row = row.get_next_sibling()
            if sel is not None:
                self.plist.select_row(sel)
            self._loading = False

        def on_profile_selected(self, _lb, row):
            if self._loading or row is None:
                return
            pid = getattr(row, "_pid", None)
            if pid is None or pid == self._profile_id:
                return
            self.load_profile(pid, self.current, bind=True)

        # ---------- load device / profile ----------
        def load_device(self, name):
            self.current = name
            pid = self.store.binding_for(name) or CLEAN_ID
            if pid not in self.store.profiles:
                pid = CLEAN_ID
            self.load_profile(pid, name, bind=False)

        def load_profile(self, pid, node, bind=False):
            p = self.store.get(pid); pid = p["id"]
            self._profile_id = pid
            keys = (sink_channels(node) if node else None) or p.get("ch_keys") or ["FL", "FR"]
            body = dict(self.store._body(p)); body["ch_keys"] = keys[:8]
            self._apply_body(body)
            self.bypass = False
            self._loading = True
            self.bypass_sw.set_active(False)
            self._loading = False
            self._reset_history()
            if bind and node:
                self.store.set_binding(node, pid)
                self.store.regenerate_presets()
                if pid != CLEAN_ID:
                    self._hook_once()
            self.apply_live()
            self._select_profile_row(pid)
            self._update_status()

        # ---------- profile operations ----------
        def _create_from_editor(self, name):
            """Fork the current editor contents into a new user profile, bind the
            device to it, and make it active."""
            nid = _new_id()
            self.store.save_user(self._body_from_editor(pid=nid, name=self._unique_name(name)))
            self._profile_id = nid
            if self.current:
                self.store.set_binding(self.current, nid)
            self.store.regenerate_presets()
            self._hook_once()
            self._rebuild_profile_list()
            self._select_profile_row(nid)
            self.apply_live()
            self._update_status()
            self._flash("Saved")

        def on_new_profile(self, *_):
            name = self._unique_name("New profile")
            pid = self.store.save_user({"id": _new_id(), "name": name, "apply_all": True,
                                        "ch_keys": [], "all": {"preamp": 0.0, "bands": []},
                                        "channels": {}})
            self._rebuild_profile_list()
            self.load_profile(pid, self.current, bind=True)

        def on_duplicate(self, *_):
            base = self.store.get(self._profile_id)["name"]
            self._prompt_name("Duplicate profile", base + " copy", self._create_from_editor)

        def on_rename(self, *_):
            p = self.store.get(self._profile_id)
            if p.get("builtin"):
                return
            def do(name):
                body = self.store._body(p); body["name"] = self._unique_name(name)
                self.store.save_user(body)
                self._rebuild_profile_list(); self._select_profile_row(self._profile_id)
                self._update_status()
            self._prompt_name("Rename profile", p["name"], do)

        def on_delete(self, *_):
            p = self.store.get(self._profile_id)
            if p.get("builtin"):
                return
            d = Gtk.AlertDialog()
            d.set_modal(True)
            d.set_message("Delete profile \u201c%s\u201d?" % p["name"])
            d.set_detail("Any device using it will fall back to Clean (no EQ).")
            d.set_buttons(["Cancel", "Delete"])
            d.set_cancel_button(0); d.set_default_button(1)
            d.choose(self, None, self._delete_choice)

        def _delete_choice(self, dlg, result):
            try:
                idx = dlg.choose_finish(result)
            except GLib.Error:
                idx = 0
            if idx != 1:
                return
            self.store.delete_user(self._profile_id)
            self.store.regenerate_presets()
            self._rebuild_profile_list()
            if self.current:                           # reload (falls back to Clean if needed)
                self.load_device(self.current)
            else:
                self.load_profile(CLEAN_ID, None)
            self._flash("Deleted")

        def _prompt_name(self, title, initial, on_ok):
            win = Gtk.Window(title=title, transient_for=self, modal=True)
            win.set_default_size(360, -1)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            box.set_margin_top(12); box.set_margin_bottom(12)
            box.set_margin_start(12); box.set_margin_end(12)
            win.set_child(box)
            box.append(Gtk.Label(label="Profile name:", xalign=0.0))
            entry = Gtk.Entry(); entry.set_text(initial or ""); entry.set_hexpand(True)
            box.append(entry)
            bb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                         halign=Gtk.Align.END)
            cancel = Gtk.Button(label="Cancel")
            ok = Gtk.Button(label="OK"); ok.add_css_class("suggested-action")
            bb.append(cancel); bb.append(ok); box.append(bb)
            def done(*_):
                name = entry.get_text().strip()
                win.close()
                if name:
                    on_ok(name)
            cancel.connect("clicked", lambda *_: win.close())
            ok.connect("clicked", done)
            entry.connect("activate", done)
            win.present(); entry.grab_focus()

        def _dev_desc(self):
            for s in self.sinks:
                if s["name"] == self.current:
                    return s["desc"]
            return self.current

        # ---------- channels / tabs ----------
        def _rebuild_tabs(self):
            nb = self._notebook
            nb.handler_block(self._nb_handler)
            while nb.get_n_pages() > 0:
                nb.remove_page(-1)
            if self._apply_all:
                nb.set_visible(False)
            else:
                for k in self._ch_keys:
                    page = Gtk.Box(); page.set_size_request(-1, 0)
                    nb.append_page(page, Gtk.Label(label=k))
                nb.set_current_page(0)
                nb.set_visible(True)
            nb.handler_unblock(self._nb_handler)

        def _reload_editor(self):
            self._loading = True
            self.preamp_spin.set_value(self.preamp)
            self._loading = False
            self.rebuild_rows()
            self.area.queue_draw()

        def on_tab_switched(self, nb, page, idx):
            if self._loading:
                return
            self._switch_idx = idx
            self._reload_editor()
            self._switch_idx = None

        def on_allch_toggled(self, chk):
            if self._loading:
                return
            self._apply_all = chk.get_active()
            if not self._apply_all:
                a = self._ch_store.get("all") or {"preamp": 0.0, "bands": []}
                for k in self._ch_keys:
                    if k not in self._ch_store:
                        self._ch_store[k] = {
                            "preamp": a.get("preamp", 0.0),
                            "bands": [Band(b.type, b.freq, b.gain, b.q, b.enabled)
                                      for b in a.get("bands", [])]}
            self._rebuild_tabs()
            self._reload_editor()
            self._changed()

        # ---------- band rows ----------
        def rebuild_rows(self):
            prev = self._loading
            self._loading = True
            child = self.rows_box.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                for i, c in enumerate(getattr(child, "_cells", [])):
                    self._colgroups[i].remove_widget(c)
                self.rows_box.remove(child)
                child = nxt
            self._row_bands = sorted(self.bands, key=lambda x: x.freq)
            for b in self._row_bands:
                self.rows_box.append(self._make_row(b))
            self._loading = prev

        def _maybe_resort(self):
            want = sorted(self.bands, key=lambda x: x.freq)
            if [id(b) for b in want] != [id(b) for b in getattr(self, "_row_bands", [])]:
                self.rebuild_rows()
            return False

        def _make_row(self, b):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.add_css_class("eqrow")
            sw = Gtk.Label(use_markup=True)
            r, g, bl = self._band_color(b.freq)
            sw.set_markup("<span foreground='#%02x%02x%02x' size='large'>\u25cf</span>"
                          % (int(r * 255), int(g * 255), int(bl * 255)))
            row.append(sw)
            tdd = Gtk.DropDown.new_from_strings(TYPE_NAMES)
            tdd.set_selected(TYPE_NAMES.index(b.type))
            tdd.connect("notify::selected",
                        lambda dd, _p, b=b: self._set_attr(b, "type", TYPE_NAMES[dd.get_selected()]))
            self._no_scroll(tdd)
            row.append(tdd)
            fs = Gtk.SpinButton.new_with_range(10.0, 24000.0, 1.0)
            fs.set_value(b.freq)
            fs.connect("value-changed", lambda sp, b=b: self._set_attr(b, "freq", sp.get_value()))
            ffoc = Gtk.EventControllerFocus()
            ffoc.connect("leave", lambda *_: GLib.idle_add(self._maybe_resort))
            fs.add_controller(ffoc)
            self._no_scroll(fs)
            row.append(fs)
            gs = Gtk.SpinButton.new_with_range(-24.0, 24.0, 0.1)
            gs.set_digits(1); gs.set_value(b.gain)
            gs.connect("value-changed", lambda sp, b=b: self._set_attr(b, "gain", sp.get_value()))
            self._no_scroll(gs)
            row.append(gs)
            qs = Gtk.SpinButton.new_with_range(0.1, 10.0, 0.05)
            qs.set_digits(2); qs.set_value(b.q)
            qs.connect("value-changed", lambda sp, b=b: self._set_attr(b, "q", sp.get_value()))
            self._no_scroll(qs)
            row.append(qs)
            ck = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            ck.set_active(b.enabled)
            ck.connect("toggled", lambda c, b=b: self._set_attr(b, "enabled", c.get_active()))
            row.append(ck)
            rm = Gtk.Button(label="\u2715")
            rm.connect("clicked", lambda _x, b=b: self._remove_band(b))
            row.append(rm)
            cells = [sw, tdd, fs, gs, qs, ck, rm]
            for i, c in enumerate(cells):
                self._colgroups[i].add_widget(c)
            row._cells = cells
            return row

        def _set_attr(self, b, attr, val):
            if self._loading:
                return
            setattr(b, attr, val)
            self._changed()

        def _remove_band(self, b):
            if b in self.bands:
                self.bands.remove(b)
            self.rebuild_rows(); self._changed()

        def on_add_band(self, *_):
            self.bands.append(Band("PK", 1000.0, 0.0, 1.0, True))
            self.rebuild_rows(); self._changed()

        # ---------- toggles / preamp ----------
        def on_bypass_toggled(self, sw, _p):
            if self._loading: return
            self.bypass = sw.get_active(); self.apply_live(); self.area.queue_draw()
        def on_preamp_changed(self, sp):
            if self._loading: return
            self.preamp = sp.get_value(); self._changed()
        def on_auto_preamp(self, *_):
            curve = response_db(0.0, self.bands, self._freqs)
            peak = max(curve) if curve else 0.0
            self.preamp = max(-24.0, min(0.0, -peak))
            self._loading = True; self.preamp_spin.set_value(self.preamp); self._loading = False
            self._changed()
        def on_follow_toggled(self, sw, _p):
            self.dev_dd.set_sensitive(not sw.get_active())
            if sw.get_active():
                self._poll()

        # ---------- live apply (debounced) + autosave ----------
        def _changed(self):
            self.area.queue_draw()
            p = self.store.get(self._profile_id)
            if p.get("builtin") and self._has_content():   # fork built-in at once
                before = self._profile_id
                self._ensure_editable()
                if self._profile_id != before:
                    self._update_status()
            if self._apply_src:
                GLib.source_remove(self._apply_src)
            self._apply_src = GLib.timeout_add(150, self._apply_now)

        def _apply_now(self):
            self._apply_src = 0
            self.apply_live()
            self._persist()
            if not self._restoring:
                self._push_history()
            self._update_status()
            self._fork_name_hint = None
            return False

        def apply_live(self):
            if not self.current:
                return
            nid = resolve_sink_id(self.current)
            if nid is None:
                return
            graph = build_graph(0.0, []) if self.bypass else self._editor_graph()
            set_graph(nid, graph)

        # ---------- import / export (Gtk.FileDialog, GTK 4.10+) ----------
        @staticmethod
        def _filters(name, pattern):
            store = Gio.ListStore.new(Gtk.FileFilter)
            f1 = Gtk.FileFilter(); f1.set_name(name); f1.add_pattern(pattern)
            f2 = Gtk.FileFilter(); f2.set_name("All files"); f2.add_pattern("*")
            store.append(f1); store.append(f2)
            return store, f1

        def on_import(self, *_):
            dlg = Gtk.FileDialog(); dlg.set_title("Import REW / AutoEQ filters")
            s, d = self._filters("Text / EQ (*.txt)", "*.txt")
            dlg.set_filters(s); dlg.set_default_filter(d)
            dlg.open(self, None, self._import_done)

        def _import_done(self, dlg, result):
            try:
                gf = dlg.open_finish(result)
            except GLib.Error:
                return
            if not gf:
                return
            try:
                with open(gf.get_path(), encoding="utf-8", errors="replace") as f:
                    text = f.read()
                preamp, bands = parse_autoeq(text)
                if bands:
                    stem = os.path.splitext(os.path.basename(gf.get_path()))[0]
                    self._fork_name_hint = stem
                    self.preamp = preamp; self.bands = bands
                    self._loading = True; self.preamp_spin.set_value(preamp); self._loading = False
                    self.rebuild_rows(); self._changed()
                    self._flash("Imported %d filters" % len(bands))
                else:
                    self._info("Nothing imported", "No 'Filter N: ON …' lines were found.")
            except Exception as e:
                self._info("Import failed", str(e))

        def _safe_name(self, extra=""):
            base = self.store.get(self._profile_id)["name"] if self._profile_id else "eq"
            if extra:
                base = "%s-%s" % (base, extra)
            return (re.sub(r"[^\w.+-]+", "_", base).strip("_") or "eq")

        def on_export_profile(self, *_):
            if not self._profile_id:
                return
            dlg = Gtk.FileDialog(); dlg.set_title("Export profile (.json)")
            dlg.set_initial_name(self._safe_name() + ".json")
            s, d = self._filters("Profile (*.json)", "*.json")
            dlg.set_filters(s); dlg.set_default_filter(d)
            dlg.save(self, None, self._export_profile_done)

        def _export_profile_done(self, dlg, result):
            try:
                gf = dlg.save_finish(result)
            except GLib.Error:
                return
            if not gf:
                return
            try:
                body = self._body_from_editor()
                with open(gf.get_path(), "w", encoding="utf-8") as f:
                    json.dump(body, f, indent=2, ensure_ascii=False)
                self._flash("Profile exported")
            except Exception as e:
                self._info("Export failed", str(e))

        def on_export_rew(self, *_):
            dlg = Gtk.FileDialog(); dlg.set_title("Export EQ (REW/AutoEQ format)")
            extra = "" if self._apply_all else self._active_key()
            dlg.set_initial_name(self._safe_name(extra) + ".txt")
            s, d = self._filters("Text / EQ (*.txt)", "*.txt")
            dlg.set_filters(s); dlg.set_default_filter(d)
            dlg.save(self, None, self._export_rew_done)

        def _export_rew_done(self, dlg, result):
            try:
                gf = dlg.save_finish(result)
            except GLib.Error:
                return
            if not gf:
                return
            try:
                with open(gf.get_path(), "w", encoding="utf-8") as f:
                    f.write(eq_text(self.preamp, self.bands))
                self._flash("Exported")
            except Exception as e:
                self._info("Export failed", str(e))

        # ---------- polling: follow default + star refresh ----------
        def _poll(self):
            dump = pw_dump()
            sinks = list_sinks(dump)
            default = next((s["name"] for s in sinks if s["default"]), None)
            prev_names = [s["name"] for s in self.sinks]
            prev_default = next((s["name"] for s in self.sinks if s["default"]), None)
            new_names = [s["name"] for s in sinks]
            self.sinks = sinks
            if self.follow_sw.get_active() and default and default != self.current:
                self._select_by_name(default)
            elif new_names != prev_names or default != prev_default:
                self._rebuild_dev_labels()
                idx = self._index_of(self.current) if self.current else None
                if idx is not None:
                    self._set_selected(idx)
            self._update_status()
            return True

        # ---------- graph interaction ----------
        def _x_of(self, f):
            ml, mt, pw_, ph = self._plot
            return ml + (math.log10(f) - math.log10(FMIN)) / \
                   (math.log10(FMAX) - math.log10(FMIN)) * pw_
        def _y_of(self, db):
            ml, mt, pw_, ph = self._plot
            return mt + (DB_MAX - db) / (2 * DB_MAX) * ph
        def _f_of(self, x):
            ml, mt, pw_, ph = self._plot
            if pw_ <= 0: return None
            t = min(1.0, max(0.0, (x - ml) / pw_))
            return 10 ** (math.log10(FMIN) + t * (math.log10(FMAX) - math.log10(FMIN)))
        def _db_of(self, y):
            ml, mt, pw_, ph = self._plot
            if ph <= 0: return None
            t = min(1.0, max(0.0, (y - mt) / ph))
            return DB_MAX - t * (2 * DB_MAX)

        @staticmethod
        def _hsv(h, s, v):
            i = int(h * 6.0); f = h * 6.0 - i
            p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
            return [(v, t, p), (q, v, p), (p, v, t),
                    (p, q, v), (t, p, v), (v, p, q)][i % 6]

        def _band_color(self, f):
            lf = math.log10(min(FMAX, max(FMIN, f)))
            t = (lf - math.log10(FMIN)) / (math.log10(FMAX) - math.log10(FMIN))
            return self._hsv((1.0 - t) * 0.66, 0.65, 1.0)

        def _hit_band(self, x, y, r=11):
            if not self._plot:
                return None
            best, bestd = None, r * r
            for b in self.bands:
                bx = self._x_of(b.freq); by = self._y_of(max(-DB_MAX, min(DB_MAX, b.gain)))
                d = (bx - x) ** 2 + (by - y) ** 2
                if d <= bestd:
                    best, bestd = b, d
            return best

        def _ensure_audible(self):
            if self.bypass:
                self.bypass = False
                self._loading = True; self.bypass_sw.set_active(False); self._loading = False

        def _on_drag_begin(self, gesture, sx, sy):
            self._drag_band = None
            if not self._plot:
                return
            b = self._hit_band(sx, sy)
            if b is None:
                f = self._f_of(sx); db = self._db_of(sy)
                if f is None or db is None:
                    return
                b = Band("PK", f, db, 1.0, True)
                self.bands.append(b)
            self._ensure_audible()
            self._drag_band = b
            self.area.queue_draw()

        def _on_drag_update(self, gesture, ox, oy):
            if self._drag_band is None or not self._plot:
                return
            ok, sx, sy = gesture.get_start_point()
            if not ok:
                return
            f = self._f_of(sx + ox); db = self._db_of(sy + oy)
            if f is not None: self._drag_band.freq = f
            if db is not None: self._drag_band.gain = db
            self._changed()

        def _on_drag_end(self, gesture, ox, oy):
            if self._drag_band is None:
                return
            self._drag_band = None
            self.rebuild_rows(); self._changed()

        def _on_right_click(self, gesture, n, x, y):
            b = self._hit_band(x, y)
            if b is not None:
                self._remove_band(b)

        def draw_fr(self, area, ctx, w, h, *_):
            ml, mr, mt, mb = 44, 10, 10, 22
            pw_, ph = max(1, w - ml - mr), max(1, h - mt - mb)
            self._plot = (ml, mt, pw_, ph)
            ctx.set_source_rgb(0.12, 0.12, 0.14); ctx.paint()
            ctx.rectangle(ml, mt, pw_, ph)
            ctx.set_source_rgb(0.08, 0.08, 0.10); ctx.fill()

            def x_of(f):
                return ml + (math.log10(f) - math.log10(FMIN)) / \
                       (math.log10(FMAX) - math.log10(FMIN)) * pw_
            def y_of(db):
                return mt + (DB_MAX - db) / (2 * DB_MAX) * ph

            ctx.set_line_width(1.0)
            ctx.select_font_face("Sans", 0, 0); ctx.set_font_size(9)
            for f in (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000):
                x = x_of(f)
                ctx.set_source_rgba(1, 1, 1, 0.10)
                ctx.move_to(x, mt); ctx.line_to(x, mt + ph); ctx.stroke()
                ctx.set_source_rgba(1, 1, 1, 0.45)
                lab = ("%dk" % (f // 1000)) if f >= 1000 else str(f)
                ctx.move_to(x - 8, mt + ph + 14); ctx.show_text(lab)
            for db in range(int(-DB_MAX), int(DB_MAX) + 1, 6):
                y = y_of(db)
                ctx.set_source_rgba(1, 1, 1, 0.16 if db == 0 else 0.08)
                ctx.move_to(ml, y); ctx.line_to(ml + pw_, y); ctx.stroke()
                ctx.set_source_rgba(1, 1, 1, 0.45)
                ctx.move_to(4, y + 3); ctx.show_text("%+d" % db)

            active = self.enabled and not self.bypass
            curve = response_db(self.preamp, self.bands, self._freqs)
            if active:
                ctx.set_source_rgb(0.30, 0.78, 1.0)
            else:
                ctx.set_source_rgba(0.6, 0.6, 0.6, 0.7)
            ctx.set_line_width(2.0)
            for i, f in enumerate(self._freqs):
                db = max(-DB_MAX, min(DB_MAX, curve[i]))
                px, py = x_of(f), y_of(db)
                if i == 0: ctx.move_to(px, py)
                else: ctx.line_to(px, py)
            ctx.stroke()
            if not active:
                ctx.set_source_rgba(0.30, 0.78, 1.0, 0.5)
                ctx.set_line_width(1.5); ctx.set_dash([4, 4], 0)
                ctx.move_to(ml, y_of(0)); ctx.line_to(ml + pw_, y_of(0)); ctx.stroke()
                ctx.set_dash([], 0)

            for b in self.bands:
                bx = x_of(b.freq); by = y_of(max(-DB_MAX, min(DB_MAX, b.gain)))
                r, g, bl = self._band_color(b.freq)
                ctx.arc(bx, by, 5.5, 0, 2 * math.pi)
                if b.enabled:
                    ctx.set_source_rgb(r, g, bl); ctx.fill_preserve()
                    ctx.set_source_rgba(0, 0, 0, 0.55); ctx.set_line_width(1.0); ctx.stroke()
                else:
                    ctx.set_source_rgba(r, g, bl, 0.7)
                    ctx.set_line_width(1.5); ctx.stroke()

    class EQApp(Gtk.Application):
        def __init__(self):
            super().__init__(application_id="io.github.ntman.PerDeviceEQ",
                             flags=Gio.ApplicationFlags.FLAGS_NONE)
        def _install_css(self):
            if getattr(self, "_css_done", False):
                return
            self._css_done = True
            data = (".eqrow spinbutton, .eqrow button, .eqrow dropdown { min-height: 26px; }"
                    " .eqrow spinbutton entry { min-height: 24px; padding-top: 1px;"
                    " padding-bottom: 1px; }")
            css = Gtk.CssProvider()
            if hasattr(css, "load_from_string"):
                css.load_from_string(data)
            else:
                css.load_from_data(data.encode())
            disp = Gdk.Display.get_default()
            if disp:
                Gtk.StyleContext.add_provider_for_display(
                    disp, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        def do_activate(self):
            self._install_css()
            win = self.props.active_window or EQWindow(self)
            win.present()

    EQApp().run([])


# ============================ main ============================
def main():
    ap = argparse.ArgumentParser(description="Per-device PipeWire EQ (profiles)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--list", action="store_true", help="list sinks")
    g.add_argument("--list-profiles", action="store_true", help="list profiles")
    g.add_argument("--inspect", metavar="NODE_NAME", help="dump node params")
    g.add_argument("--apply", action="store_true", help="apply bound profiles now")
    g.add_argument("--install-hook", action="store_true",
                   help="(re)install WirePlumber hook + config")
    args = ap.parse_args()

    if args.list:
        return cmd_list()
    if args.list_profiles:
        return cmd_list_profiles()
    if args.inspect:
        return cmd_inspect(args.inspect)
    if args.apply:
        return cmd_apply()
    if args.install_hook:
        changed = install_hook()
        ProfileStore().regenerate_presets()
        print("hook %s" % ("installed/updated -> restart wireplumber"
                           if changed else "already up to date"))
        return 0
    return launch_gui()


if __name__ == "__main__":
    sys.exit(main() or 0)
