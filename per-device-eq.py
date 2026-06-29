#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
per-device-eq -- per-output-device parametric EQ for PipeWire 1.6+.

EQ lives INSIDE the real sink via the in-node filter-graph Props param
"audioconvert.filter-graph.0" (index required; value is an INLINE graph string).
It instantiates only when the node is running with a negotiated format, so live
edits go through `pw-cli set-param` on the running node, and persistence is a
WirePlumber Lua hook applying the saved graph when the node starts playing
(survives reboot / hotplug / reconnect). No phantom node, no daemon of ours.

  --list                 list sinks (default marked with *)
  --inspect <node.name>  dump node params (pw-dump info.params)
  --apply                apply all enabled presets now
  --install-hook         (re)install the WirePlumber hook + config
  (no args)              launch the GTK4 GUI
"""

import argparse, json, math, cmath, os, re, subprocess, sys

FS = 48000.0
CONFIG_DIR  = os.path.expanduser("~/.config/per-device-eq")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
PRESETS_LUA = os.path.join(CONFIG_DIR, "presets.lua")
WP_SCRIPT_DIR = os.path.expanduser("~/.local/share/wireplumber/scripts")
WP_SCRIPT     = os.path.join(WP_SCRIPT_DIR, "90-per-device-eq.lua")
WP_CONF_DIR   = os.path.expanduser("~/.config/wireplumber/wireplumber.conf.d")
WP_CONF       = os.path.join(WP_CONF_DIR, "90-per-device-eq.conf")

TYPE_TO_LABEL = {"PK": "bq_peaking", "LSC": "bq_lowshelf", "HSC": "bq_highshelf"}
TYPE_NAMES = ["PK", "LSC", "HSC"]

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
    # ensure uniqueness (some layouts repeat names) for stable dict keys / tabs
    seen, out = {}, []
    for k in keys:
        if k in seen:
            seen[k] += 1; out.append("%s.%d" % (k, seen[k]))
        else:
            seen[k] = 0; out.append(k)
    return out


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
    channel order; emitted as config.filters1, filters2, ... (1-based), which
    param_eq applies per channel."""
    parts = []
    for i, (preamp, bands) in enumerate(channel_sets, start=1):
        parts.append("filters%d = %s" % (i, build_filter_array(preamp, bands)))
    cfg = " ".join(parts)
    return ("{ nodes = [ { type = builtin name = eq label = param_eq "
            "config = { %s } } ] }" % cfg)


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


# ============================ REW / AutoEQ parser ============================
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
            btype = "PK"  # PK / LP / HP fallback to peaking (param_eq subset)
        bands.append(Band(btype, fc, gain, q, True))
    return preamp, bands


# ============================ config + persistence ============================
def load_config():
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    cfg.setdefault("devices", {})
    return cfg


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_FILE)


def device_graph_from_cfg(dev):
    """Build the device's inline graph from its config entry, honoring apply_all
    and per-channel sets, and migrating the old flat {preamp,bands} schema."""
    all_src = dev.get("all")
    if all_src is None and ("bands" in dev or "preamp" in dev):
        all_src = {"preamp": dev.get("preamp", 0.0), "bands": dev.get("bands", [])}
    all_src = all_src or {"preamp": 0.0, "bands": []}
    if dev.get("apply_all", True):
        bands = [Band.from_dict(b) for b in all_src.get("bands", [])]
        return build_graph(all_src.get("preamp", 0.0), bands)
    chans = dev.get("channels") or {}
    keys = dev.get("ch_keys") or list(chans.keys())
    sets = []
    for k in keys:
        e = chans.get(k) or {"preamp": 0.0, "bands": []}
        sets.append((e.get("preamp", 0.0),
                     [Band.from_dict(b) for b in e.get("bands", [])]))
    if not sets:
        bands = [Band.from_dict(b) for b in all_src.get("bands", [])]
        return build_graph(all_src.get("preamp", 0.0), bands)
    return build_graph_channels(sets)


def regenerate_presets(cfg):
    """Write presets.lua consumed by the WirePlumber hook. Graph strings carry
    no double quotes and no ]==] so a long-bracket literal is safe."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    lines = ["-- auto-generated by per-device-eq; do not edit by hand", "return {"]
    for name, dev in cfg.get("devices", {}).items():
        if not dev.get("enabled"):
            continue
        graph = device_graph_from_cfg(dev)
        lines.append('  ["%s"] = [==[%s]==],' % (name, graph))
    lines.append("}")
    tmp = PRESETS_LUA + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, PRESETS_LUA)


def install_hook():
    """Write the WP hook script + component config if missing/changed.
    Returns True if anything was newly written (caller should suggest a
    `systemctl --user restart wireplumber`)."""
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


def cmd_inspect(name):
    params, nid = node_params(name)
    if nid is None:
        print("sink not found: %s" % name, file=sys.stderr)
        return 1
    print("Sink id=%s name=%s\n" % (nid, name))
    print(json.dumps(params, indent=2, ensure_ascii=False))
    return 0


def cmd_apply():
    cfg = load_config()
    dump = pw_dump()
    n = 0
    for name, dev in cfg.get("devices", {}).items():
        if not dev.get("enabled"):
            continue
        nid = resolve_sink_id(name, dump)
        if nid is None:
            print("skip (not present): %s" % name)
            continue
        graph = device_graph_from_cfg(dev)
        ok = set_graph(nid, graph)
        print("%s %s" % ("applied" if ok else "FAILED ", name))
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
            self.set_default_size(820, 680)
            self.cfg = load_config()
            self.sinks = []
            self.current = None          # node.name
            # per-channel store: key -> {"preamp": float, "bands": [Band]}.
            # key "all" is the shared (apply-to-all) set; otherwise a channel key.
            self._apply_all = True
            self._ch_keys = ["FL", "FR"]
            self._ch_store = {"all": {"preamp": 0.0, "bands": []}}
            self._notebook = None
            self._switch_idx = None
            self.bypass = False
            self._loading = False
            self._force_close = False
            self._apply_src = 0
            self._freqs = log_freqs()
            self._plot = None            # (ml, mt, pw, ph) cached from draw_fr
            self._drag_band = None       # Band currently being dragged on the graph

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

            row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            root.append(row2)
            self.follow_sw = Gtk.Switch(valign=Gtk.Align.CENTER)
            self.follow_sw.connect("notify::active", self.on_follow_toggled)
            row2.append(self._labeled("Follow default", self.follow_sw))
            self.bypass_sw = Gtk.Switch(valign=Gtk.Align.CENTER)
            self.bypass_sw.connect("notify::active", self.on_bypass_toggled)
            row2.append(self._labeled("Bypass", self.bypass_sw))
            self.allch_chk = Gtk.CheckButton(label="Apply to all channels")
            self.allch_chk.set_active(True)
            self.allch_chk.connect("toggled", self.on_allch_toggled)
            row2.append(self.allch_chk)
            self.status = Gtk.Label(label="", xalign=1.0)
            self.status.set_hexpand(True)
            self.status.add_css_class("dim-label")
            row2.append(self.status)

            # ---- channel selector (tabs); shown only when not "apply to all" ----
            self._notebook = Gtk.Notebook()
            self._notebook.set_show_border(False)
            self._notebook.set_scrollable(True)
            self._nb_handler = self._notebook.connect("switch-page", self.on_tab_switched)
            self._notebook.set_visible(False)
            root.append(self._notebook)

            # ---- FR graph ----
            self.area = Gtk.DrawingArea()
            self.area.set_content_height(150)
            self.area.set_hexpand(True); self.area.set_vexpand(True)
            self.area.set_draw_func(self.draw_fr)
            drag = Gtk.GestureDrag()      # left button: add / move band points
            drag.connect("drag-begin", self._on_drag_begin)
            drag.connect("drag-update", self._on_drag_update)
            drag.connect("drag-end", self._on_drag_end)
            self.area.add_controller(drag)
            rclick = Gtk.GestureClick()   # right button: remove a band point
            rclick.set_button(3)
            rclick.connect("pressed", self._on_right_click)
            self.area.add_controller(rclick)
            self.area.set_cursor(Gdk.Cursor.new_from_name("crosshair", None))
            frame = Gtk.Frame()
            frame.set_child(self.area)

            # graph (top) and the table region (bottom) share a draggable splitter,
            # so the table can be enlarged (e.g. maximized) for more visible rows.
            self._paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
            self._paned.set_wide_handle(True)
            self._paned.set_vexpand(True)
            self._paned.set_start_child(frame)
            self._paned.set_resize_start_child(True)
            self._paned.set_shrink_start_child(False)
            self._table_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            self._paned.set_end_child(self._table_box)
            self._paned.set_resize_end_child(True)
            self._paned.set_shrink_end_child(False)
            root.append(self._paned)

            # ---- preamp ----
            pre = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            self._table_box.append(pre)
            pre.append(Gtk.Label(label="Preamp (dB):"))
            self.preamp_spin = Gtk.SpinButton.new_with_range(-24.0, 24.0, 0.1)
            self.preamp_spin.set_digits(1)
            self.preamp_spin.connect("value-changed", self.on_preamp_changed)
            pre.append(self.preamp_spin)
            auto_pre = Gtk.Button(label="Auto")
            auto_pre.set_tooltip_text("Set preamp to -(peak of the EQ curve) so the "
                                      "EQ does not raise overall level / clip")
            auto_pre.connect("clicked", self.on_auto_preamp)
            pre.append(auto_pre)

            # ---- bands ----
            # per-column size groups keep the header labels aligned with the row
            # widgets regardless of their natural width (7 columns incl. swatch).
            self._colgroups = [Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
                               for _ in range(7)]
            hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            for i, txt in enumerate(("", "Type", "Freq (Hz)", "Gain (dB)",
                                     "Q", "On", "")):
                lbl = Gtk.Label(label=txt, xalign=0.0)
                lbl.add_css_class("dim-label")
                self._colgroups[i].add_widget(lbl)
                hdr.append(lbl)
            self._table_box.append(hdr)

            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroller.set_min_content_height(110)
            scroller.set_vexpand(True)
            self.rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            scroller.set_child(self.rows_box)
            self._table_box.append(scroller)

            # ---- bottom buttons ----
            btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            root.append(btns)
            add_b = Gtk.Button(label="Add band")
            add_b.connect("clicked", self.on_add_band)
            btns.append(add_b)
            imp_b = Gtk.Button(label="Import REW/AutoEQ…")
            imp_b.connect("clicked", self.on_import)
            btns.append(imp_b)
            exp_b = Gtk.Button(label="Export…")
            exp_b.set_tooltip_text("Export this EQ in REW/AutoEQ text format "
                                   "(to copy to another machine)")
            exp_b.connect("clicked", self.on_export)
            btns.append(exp_b)
            reset_b = Gtk.Button(label="Clear")
            reset_b.connect("clicked", self.on_clear)
            btns.append(reset_b)
            reapply_b = Gtk.Button(label="Re-apply")
            reapply_b.set_tooltip_text("Re-assert this EQ onto the device "
                                       "(e.g. after another tool changed the graph)")
            reapply_b.connect("clicked", lambda *_: (self.apply_live(), self._flash("Re-applied")))
            btns.append(reapply_b)
            spacer = Gtk.Box(); spacer.set_hexpand(True); btns.append(spacer)
            self.saved_lbl = Gtk.Label(label="", valign=Gtk.Align.CENTER)
            self.saved_lbl.add_css_class("dim-label")
            btns.append(self.saved_lbl)
            save_b = Gtk.Button(label="Save & Apply")
            save_b.add_css_class("suggested-action")
            save_b.connect("clicked", self.on_save)
            btns.append(save_b)

            self.connect("close-request", self._on_close_request)
            self.reload_sinks()
            self.follow_sw.set_active(True)      # follow the default sink by default
            self._paned.set_position(300)        # initial graph/table split
            GLib.timeout_add_seconds(2, self._poll)

        # ---------- helpers ----------
        def _labeled(self, text, widget):
            b = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            b.append(Gtk.Label(label=text))
            b.append(widget)
            return b

        def dev_of(self, name, create=False):
            d = self.cfg["devices"].get(name)
            if d is None and create:
                d = {}
                self.cfg["devices"][name] = d
            return d

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
            """EQ is 'on' when there is anything to apply (an enabled band or a
            non-zero preamp) -- in all-mode the shared set, else any channel."""
            if self._apply_all:
                return self._set_has_content(self._ch_store.get("all") or {})
            for k in self._ch_keys:
                if self._set_has_content(self._ch_store.get(k) or {}):
                    return True
            return False

        # EQ-enabled is derived from content (no separate toggle); Bypass / Clear
        # are the ways to listen without / drop the correction.
        enabled = property(lambda self: self._has_content())

        def _channel_sets(self):
            """(preamp, bands) per channel in channel order, for the graph."""
            out = []
            for k in self._ch_keys:
                e = self._ch_store.get(k) or {"preamp": 0.0, "bands": []}
                out.append((e.get("preamp", 0.0), e.get("bands", [])))
            return out

        def _device_graph(self):
            if self._apply_all:
                a = self._ch_store.get("all") or {"preamp": 0.0, "bands": []}
                return build_graph(a.get("preamp", 0.0), a.get("bands", []))
            return build_graph_channels(self._channel_sets())

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
                ["%s%s" % ("★ " if s["default"] else "", s["desc"]) for s in self.sinks])
            self._loading = False

        def _set_selected(self, idx):
            self._loading = True
            self.dev_dd.set_selected(idx)
            self._loading = False

        def reload_sinks(self, prefer=None):
            """Full rescan. Selection target: prefer -> current (if present) ->
            default -> first."""
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
            self._rebuild_dev_labels()      # ensure ★ reflects the current default
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

        def _current_dev_serialized(self):
            """The dict that would be persisted for the current device (also used
            to detect unsaved changes by comparing against the on-disk config)."""
            a = self._ch_store.get("all") or {"preamp": 0.0, "bands": []}
            chans = {}
            for k in self._ch_keys:
                e = self._ch_store.get(k)
                if e:
                    chans[k] = {"preamp": e.get("preamp", 0.0),
                                "bands": [b.to_dict() for b in e.get("bands", [])]}
            return {
                "enabled": self._has_content(),
                "apply_all": self._apply_all,
                "ch_keys": list(self._ch_keys),
                "all": {"preamp": a.get("preamp", 0.0),
                        "bands": [b.to_dict() for b in a.get("bands", [])]},
                "channels": chans,
            }

        def _stash_current(self):
            """Serialize the in-memory per-channel store into cfg for the current
            device, so switching away/back (and Save) keep all edits. Replaces the
            whole entry (drops any legacy keys) so it matches what Save writes."""
            if not self.current:
                return
            self.cfg["devices"][self.current] = self._current_dev_serialized()

        def _is_unsaved(self):
            """True if the current device's EQ differs from persistent storage
            (or there is content but no saved entry yet)."""
            if not self.current:
                return False
            cur = self._current_dev_serialized()
            disk = load_config().get("devices", {}).get(self.current)
            if disk is None:
                return self._has_content()
            return disk != cur

        def _update_saved(self):
            if not hasattr(self, "saved_lbl"):
                return
            unsaved = self._is_unsaved()
            self.saved_lbl.set_text("\u25cf unsaved (not persisted)" if unsaved else "saved")
            dev = (" \u2014 %s" % self.current) if self.current else ""
            self.set_title("Per-Device EQ%s%s" % (dev, " \u25cf" if unsaved else ""))

        def _on_close_request(self, *_):
            if self._force_close or not self._is_unsaved():
                return False                       # allow close
            d = Gtk.AlertDialog()
            d.set_modal(True)
            d.set_message("Save EQ permanently?")
            d.set_detail("This device's EQ is not saved to persistent storage and "
                         "will be lost after a reboot unless you save it now.")
            d.set_buttons(["Cancel", "Discard", "Save"])
            d.set_cancel_button(0); d.set_default_button(2)
            d.choose(self, None, self._close_choice)
            return True                            # block; decide in the callback

        def _close_choice(self, dlg, result):
            try:
                idx = dlg.choose_finish(result)
            except GLib.Error:
                idx = 0
            if idx == 2:                           # Save
                self.on_save(None)
                self._force_close = True; self.close()
            elif idx == 1:                         # Discard
                self._force_close = True; self.close()
            # idx 0 (Cancel): keep the window open

        def load_device(self, name):
            if self.current and name != self.current:
                self._stash_current()
            self.current = name
            d = self.dev_of(name) or {}
            self._ch_keys = (sink_channels(name) or ["FL", "FR"])[:8]
            # build store from cfg, migrating the old flat {preamp,bands} schema
            self._ch_store = {}
            all_src = d.get("all")
            if all_src is None and ("bands" in d or "preamp" in d):
                all_src = {"preamp": d.get("preamp", 0.0), "bands": d.get("bands", [])}
            all_src = all_src or {"preamp": 0.0, "bands": []}
            self._ch_store["all"] = {
                "preamp": float(all_src.get("preamp", 0.0)),
                "bands": [Band.from_dict(b) for b in all_src.get("bands", [])]}
            for k, e in (d.get("channels") or {}).items():
                self._ch_store[k] = {
                    "preamp": float(e.get("preamp", 0.0)),
                    "bands": [Band.from_dict(b) for b in e.get("bands", [])]}
            self._apply_all = bool(d.get("apply_all", True))
            self.bypass = False
            self._loading = True
            self.bypass_sw.set_active(False)
            self.allch_chk.set_active(self._apply_all)
            self._loading = False
            self._rebuild_tabs()
            self._reload_editor()
            self.apply_live()
            self._update_saved()

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
                    page = Gtk.Box(); page.set_size_request(-1, 0)  # selector only
                    nb.append_page(page, Gtk.Label(label=k))
                nb.set_current_page(0)
                nb.set_visible(True)
            nb.handler_unblock(self._nb_handler)

        def _reload_editor(self):
            """Reflect the active channel's set in the shared editor."""
            self._loading = True
            self.preamp_spin.set_value(self.preamp)
            self._loading = False
            self.rebuild_rows()
            self.area.queue_draw()

        def on_tab_switched(self, nb, page, idx):
            if self._loading:
                return
            self._switch_idx = idx          # current-page not updated yet during switch
            self._reload_editor()
            self._switch_idx = None

        def on_allch_toggled(self, chk):
            if self._loading:
                return
            self._apply_all = chk.get_active()
            if not self._apply_all:
                # seed each channel from the shared "all" set so they start equal
                a = self._ch_store.get("all") or {"preamp": 0.0, "bands": []}
                for k in self._ch_keys:
                    if k not in self._ch_store:
                        self._ch_store[k] = {
                            "preamp": a.get("preamp", 0.0),
                            "bands": [Band(b.type, b.freq, b.gain, b.q, b.enabled)
                                      for b in a.get("bands", [])]}
            self._rebuild_tabs()
            self._reload_editor()
            self.apply_live()              # graph switches between filters / filtersN

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
            self._row_bands = sorted(self.bands, key=lambda x: x.freq)  # low freq on top
            for b in self._row_bands:
                self.rows_box.append(self._make_row(b))
            self._loading = prev

        def _maybe_resort(self):
            """Re-sort the table by frequency once the user leaves a Freq field
            (doing it on every keystroke would steal focus mid-edit)."""
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
            row.append(tdd)
            fs = Gtk.SpinButton.new_with_range(10.0, 24000.0, 1.0)
            fs.set_value(b.freq)
            fs.connect("value-changed", lambda sp, b=b: self._set_attr(b, "freq", sp.get_value()))
            ffoc = Gtk.EventControllerFocus()
            ffoc.connect("leave", lambda *_: GLib.idle_add(self._maybe_resort))
            fs.add_controller(ffoc)
            row.append(fs)
            gs = Gtk.SpinButton.new_with_range(-24.0, 24.0, 0.1)
            gs.set_digits(1); gs.set_value(b.gain)
            gs.connect("value-changed", lambda sp, b=b: self._set_attr(b, "gain", sp.get_value()))
            row.append(gs)
            qs = Gtk.SpinButton.new_with_range(0.1, 10.0, 0.05)
            qs.set_digits(2); qs.set_value(b.q)
            qs.connect("value-changed", lambda sp, b=b: self._set_attr(b, "q", sp.get_value()))
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
            self._changed()        # row order/swatch refresh deferred to next rebuild

        def _remove_band(self, b):
            if b in self.bands:
                self.bands.remove(b)
            self.rebuild_rows(); self._changed()

        def on_add_band(self, *_):
            self.bands.append(Band("PK", 1000.0, 0.0, 1.0, True))
            self.rebuild_rows(); self._changed()

        def on_clear(self, *_):
            self.bands = []; self.preamp = 0.0
            self._loading = True; self.preamp_spin.set_value(0.0); self._loading = False
            self.rebuild_rows(); self._changed()

        # ---------- toggles ----------
        def on_bypass_toggled(self, sw, _p):
            if self._loading: return
            self.bypass = sw.get_active(); self.apply_live(); self.area.queue_draw()
        def on_preamp_changed(self, sp):
            if self._loading: return
            self.preamp = sp.get_value(); self._changed()

        def on_auto_preamp(self, *_):
            # peak of the EQ curve WITHOUT preamp; set preamp = -peak (never boost)
            curve = response_db(0.0, self.bands, self._freqs)
            peak = max(curve) if curve else 0.0
            self.preamp = max(-24.0, min(0.0, -peak))
            self._loading = True
            self.preamp_spin.set_value(self.preamp)
            self._loading = False
            self._changed()
        def on_follow_toggled(self, sw, _p):
            self.dev_dd.set_sensitive(not sw.get_active())
            if sw.get_active():
                self._poll()

        # ---------- live apply (debounced) ----------
        def _changed(self):
            self.area.queue_draw()
            if self._apply_src:
                GLib.source_remove(self._apply_src)
            self._apply_src = GLib.timeout_add(150, self._apply_now)

        def _apply_now(self):
            self._apply_src = 0
            self.apply_live()
            self._update_saved()
            return False

        def apply_live(self):
            if not self.current:
                return
            nid = resolve_sink_id(self.current)
            if nid is None:
                return
            if self.bypass:
                graph = build_graph(0.0, [])      # transparent, all channels
            else:
                graph = self._device_graph()      # transparent too if no content
            set_graph(nid, graph)

        # ---------- save ----------
        def on_save(self, _b):
            if not self.current:
                return
            self._stash_current()
            save_config(self.cfg)
            regenerate_presets(self.cfg)
            newly = install_hook()
            self.apply_live()
            self._update_saved()
            if newly:
                self._info("Hook installed",
                           "The WirePlumber hook was installed. Run once:\n\n"
                           "  systemctl --user restart wireplumber\n\n"
                           "After that, EQ is applied automatically on playback.")
            else:
                self._flash("Saved & applied")

        # ---------- import / export (Gtk.FileDialog, GTK 4.10+) ----------
        @staticmethod
        def _txt_filters():
            store = Gio.ListStore.new(Gtk.FileFilter)
            f1 = Gtk.FileFilter(); f1.set_name("Text / EQ (*.txt)"); f1.add_pattern("*.txt")
            f2 = Gtk.FileFilter(); f2.set_name("All files"); f2.add_pattern("*")
            store.append(f1); store.append(f2)
            return store, f1

        def on_import(self, _b):
            dlg = Gtk.FileDialog()
            dlg.set_title("Import REW / AutoEQ filters")
            store, default = self._txt_filters()
            dlg.set_filters(store); dlg.set_default_filter(default)
            dlg.open(self, None, self._import_done)

        def _import_done(self, dlg, result):
            try:
                gf = dlg.open_finish(result)
            except GLib.Error:
                return                      # cancelled / dismissed
            if not gf:
                return
            try:
                with open(gf.get_path(), encoding="utf-8", errors="replace") as f:
                    text = f.read()
                preamp, bands = parse_autoeq(text)
                if bands:
                    self.preamp = preamp; self.bands = bands
                    self._loading = True
                    self.preamp_spin.set_value(preamp)
                    self._loading = False
                    self.rebuild_rows(); self._changed()
                    self._flash("Imported %d filters" % len(bands))
                else:
                    self._info("Nothing imported",
                               "No 'Filter N: ON …' lines were found.")
            except Exception as e:
                self._info("Import failed", str(e))

        @staticmethod
        def _eq_text(preamp, bands):
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

        def _export_basename(self):
            desc = self.current or "eq"
            for s in self.sinks:
                if s["name"] == self.current:
                    desc = s["desc"] or s["name"]; break
            if not self._apply_all:
                desc = "%s-%s" % (desc, self._active_key())
            safe = re.sub(r"[^\w.+-]+", "_", desc).strip("_") or "eq"
            return safe + ".txt"

        def on_export(self, _b):
            if not self.current:
                return
            dlg = Gtk.FileDialog()
            dlg.set_title("Export EQ (REW/AutoEQ format)")
            dlg.set_initial_name(self._export_basename())
            store, default = self._txt_filters()
            dlg.set_filters(store); dlg.set_default_filter(default)
            dlg.save(self, None, self._export_done)

        def _export_done(self, dlg, result):
            try:
                gf = dlg.save_finish(result)
            except GLib.Error:
                return
            if not gf:
                return
            try:
                with open(gf.get_path(), "w", encoding="utf-8") as f:
                    f.write(self._eq_text(self.preamp, self.bands))
                self._flash("Exported")
            except Exception as e:
                self._info("Export failed", str(e))

        # ---------- polling: follow default + star/indicator refresh ----------
        def _poll(self):
            dump = pw_dump()
            sinks = list_sinks(dump)
            default = next((s["name"] for s in sinks if s["default"]), None)
            prev_names = [s["name"] for s in self.sinks]
            prev_default = next((s["name"] for s in self.sinks if s["default"]), None)
            new_names = [s["name"] for s in sinks]
            self.sinks = sinks  # keep the label source in sync with reality

            if self.follow_sw.get_active() and default and default != self.current:
                self._select_by_name(default)            # auto-switch to new default
            elif new_names != prev_names or default != prev_default:
                self._rebuild_dev_labels()               # refresh ★ even when not following
                idx = self._index_of(self.current) if self.current else None
                if idx is not None:
                    self._set_selected(idx)              # keep current selection put

            if self.current:
                present = graph_loaded(self.current, dump)
                st = "EQ on" if (self.enabled and not self.bypass) else \
                     ("bypass" if self.bypass else "EQ off")
                self.status.set_text(("● graph present · %s" % st) if present
                                     else ("○ no graph · %s" % st))
                self._update_saved()
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
            if pw_ <= 0:
                return None
            t = min(1.0, max(0.0, (x - ml) / pw_))
            return 10 ** (math.log10(FMIN) + t * (math.log10(FMAX) - math.log10(FMIN)))

        def _db_of(self, y):
            ml, mt, pw_, ph = self._plot
            if ph <= 0:
                return None
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
            return self._hsv((1.0 - t) * 0.66, 0.65, 1.0)   # low=blue .. high=red

        def _hit_band(self, x, y, r=11):
            if not self._plot:
                return None
            best, bestd = None, r * r
            for b in self.bands:
                bx = self._x_of(b.freq)
                by = self._y_of(max(-DB_MAX, min(DB_MAX, b.gain)))
                d = (bx - x) ** 2 + (by - y) ** 2
                if d <= bestd:
                    best, bestd = b, d
            return best

        def _ensure_audible(self):
            """Dragging is an edit -> make sure the change is actually heard
            (creating a band already makes the EQ active; just drop Bypass)."""
            if self.bypass:
                self.bypass = False
                self._loading = True; self.bypass_sw.set_active(False); self._loading = False

        def _on_drag_begin(self, gesture, sx, sy):
            self._drag_band = None
            if not self._plot:
                return
            b = self._hit_band(sx, sy)
            if b is None:                       # empty spot -> create a band there
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
            if f is not None:
                self._drag_band.freq = f
            if db is not None:
                self._drag_band.gain = db
            self._changed()                     # live redraw + debounced apply

        def _on_drag_end(self, gesture, ox, oy):
            if self._drag_band is None:
                return
            self._drag_band = None
            self.rebuild_rows()                 # re-sort by freq + refresh values
            self._changed()

        def _on_right_click(self, gesture, n, x, y):
            b = self._hit_band(x, y)
            if b is not None:
                self._remove_band(b)

        # ---------- FR plot ----------
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

            # band handles, colored by frequency (drag to move freq/gain)
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

        # ---------- small dialogs ----------
        def _info(self, title, body):
            d = Gtk.AlertDialog()
            d.set_modal(True); d.set_message(title); d.set_detail(body)
            d.show(self)
        def _flash(self, text):
            self.status.set_text(text)

    class EQApp(Gtk.Application):
        def __init__(self):
            super().__init__(application_id="io.github.ntman.PerDeviceEQ",
                             flags=Gio.ApplicationFlags.FLAGS_NONE)
        def _install_css(self):
            if getattr(self, "_css_done", False):
                return
            self._css_done = True
            data = (".eqrow spinbutton, .eqrow button, .eqrow dropdown "
                    "{ min-height: 26px; }"
                    " .eqrow spinbutton entry { min-height: 24px;"
                    " padding-top: 1px; padding-bottom: 1px; }")
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
    ap = argparse.ArgumentParser(description="Per-device PipeWire EQ")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--list", action="store_true", help="list sinks")
    g.add_argument("--inspect", metavar="NODE_NAME", help="dump node params")
    g.add_argument("--apply", action="store_true", help="apply all enabled presets now")
    g.add_argument("--install-hook", action="store_true",
                   help="(re)install WirePlumber hook + config")
    args = ap.parse_args()

    if args.list:
        return cmd_list()
    if args.inspect:
        return cmd_inspect(args.inspect)
    if args.apply:
        return cmd_apply()
    if args.install_hook:
        changed = install_hook()
        regenerate_presets(load_config())
        print("hook %s" % ("installed/updated -> restart wireplumber"
                           if changed else "already up to date"))
        return 0
    return launch_gui()


if __name__ == "__main__":
    sys.exit(main() or 0)
