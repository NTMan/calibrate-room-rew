# -*- coding: utf-8 -*-
"""Runtime bridge to PipeWire and, through the 'per-device-eq' metadata object,
to the WirePlumber Lua hook.

The app never talks to Lua directly: it writes a device's inline graph string
into the metadata (`metadata_set`) and the hook -- subscribed to that object --
applies it to the live node and re-applies on every reconnect. Reading state
(sinks, channels, params, default) is done by shelling out to pw-dump /
pw-metadata. No GTK here; only stdlib + the PipeWire CLI tools.
"""

import json, re, shutil, subprocess, threading

from .config import METADATA_NAME


def _run(cmd, timeout=2.0):
    """Run a helper. A hung pw-* child is the classic way to freeze the GUI, so
    every call is bounded by a timeout; on timeout/failure we kill the child and
    return a sentinel CompletedProcess instead of blocking forever."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")
    except Exception as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def _in_thread(fn):
    """Run fn on a daemon thread (for off-main-loop subprocess work)."""
    threading.Thread(target=fn, daemon=True).start()


# The PipeWire command-line tools we shell out to. Names are the same across
# distributions; the *package* that ships them is not, so we check for the tools
# themselves and let the user install them however their distro prefers.
REQUIRED_TOOLS = ["pw-metadata", "pw-dump"]

def missing_tools(tools=REQUIRED_TOOLS):
    return [t for t in tools if shutil.which(t) is None]


def meter_available():
    """pw-record is needed only by the tier-2 live meter: its absence
    degrades the app to the static tier-1 estimate, nothing more."""
    return shutil.which("pw-record") is not None


def monitor_capture(node, channels, rate=48000):
    """Spawn pw-record on a sink's monitor (PRE-EQ tap in the in-node
    topology) streaming raw interleaved f32 to stdout. Returns the Popen;
    the caller owns its lifetime and reads .stdout.

    Privacy note (field-verified observations): this capture alone does
    NOT light GNOME's microphone indicator (monitor-source recordings are
    excluded), and gnome-control-center's Sound page alone does not
    either -- the icon appears only when BOTH run, and dies with the
    panel while this capture keeps going. The trigger is therefore an
    interaction (likely an extra meter stream the panel creates in
    reaction to a foreign recording); `pactl list source-outputs` while
    the icon is lit names the culprit. The stream stays named anyway, so
    mixer UIs show who is listening to what.

    node.dont-reconnect pins the tap to its pipe: without it,
    WirePlumber re-parents a capture whose target died onto the
    DEFAULT sink's monitor, and rerouted streams do not come home
    when the target returns. A sink that forks per card profile
    (IL-DSP: analog and iec958 alternate, one node at a time) can
    die and be reborn between two of our polls, so the wander was
    invisible to the app -- the meter kept dancing to another
    device's music (field catch). With the flag the tap dies with
    its pipe; the GUI notices the dead worker and re-arms."""
    cmd = ["pw-record", "--target", str(node),
           "-P", "{ stream.capture.sink = true, node.name = per-device-eq-meter,"
                 " node.dont-reconnect = true,"
                 " application.name = \"Per-Device EQ\" }",
           "--format", "f32", "--rate", str(int(rate)),
           "--channels", str(int(channels)), "-"]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)

def missing_tools_message(missing):
    return ("These PipeWire command-line tools are required but were not found "
            "in PATH:\n\n    %s\n\nInstall the PipeWire utilities with your "
            "distribution's package manager and try again." % "  ".join(missing))


def pw_dump():
    try:
        return json.loads(_run(["pw-dump"], timeout=5.0).stdout)
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


def default_sink_from_dump(dump):
    """Default sink name from the 'default' Metadata object in a pw dump,
    or None -- lets one dump yield the default without a pw-metadata call."""
    for o in dump:
        if o.get("type") != "PipeWire:Interface:Metadata":
            continue
        props = o.get("props") or (o.get("info") or {}).get("props") or {}
        if props.get("metadata.name") != "default":
            continue
        for e in (o.get("metadata") or []):
            if e.get("key") == "default.audio.sink":
                v = e.get("value")
                if isinstance(v, dict):
                    return v.get("name")
                if isinstance(v, str):
                    try:
                        return json.loads(v).get("name")
                    except Exception:
                        pass
    return None


def list_sinks(dump=None, default=None):
    dump = dump if dump is not None else pw_dump()
    if default is None:
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

def list_sources(dump=None):
    """Audio/Source nodes (measurement mics live here): id, name, desc,
    priority.session, sorted by priority. No 'default' flag on purpose --
    the system default source is the comms/webcam mic, never the
    measurement rig, so the measure window pre-selects the last-used
    source (per-sink recall) instead of the default."""
    dump = dump if dump is not None else pw_dump()
    sources = []
    for o in dump:
        if o.get("type") != "PipeWire:Interface:Node":
            continue
        p = (o.get("info") or {}).get("props") or {}
        if p.get("media.class") == "Audio/Source":
            name = p.get("node.name")
            if not name:
                continue
            sources.append({"id": o["id"], "name": name,
                            "desc": p.get("node.description") or name,
                            "prio": p.get("priority.session") or 0})
    sources.sort(key=lambda s: -(s["prio"] or 0))
    return sources

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

def metadata_set(node_name, graph):
    """Write a device's graph into the 'per-device-eq' metadata. The WP hook is
    subscribed and applies it to the live node (and on every later reconnect).
    Stored as a plain string (no type tag), which the hook reads verbatim."""
    r = _run(["pw-metadata", "-n", METADATA_NAME, "0", node_name, graph])
    return r.returncode == 0 and "Found" in (r.stdout + r.stderr)

def metadata_clear(node_name):
    """Delete a device's key (Clean / unbound). The hook flattens the live node."""
    r = _run(["pw-metadata", "-n", METADATA_NAME, "-d", "0", node_name])
    return r.returncode == 0

_POS_FALLBACK = ["FL", "FR", "FC", "LFE", "RL", "RR", "SL", "SR"]

def _node_channels(name, dump=None):
    """Channel keys for any node (sink or source) from its negotiated
    Format position, falling back to channelVolumes length, then stereo."""
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


def sink_channels(name, dump=None):
    return _node_channels(name, dump)


def source_channels(name, dump=None):
    """Capture-channel keys for a source (mic/rig), e.g. ['FL','FR']. A
    measurement rig is 1- or 2-channel; the count is how many calibration
    files it needs, one per capture channel."""
    return _node_channels(name, dump)


PW_POLL_S = 2                        # seconds between graph refreshes


class PWState:
    """Process-wide snapshot of the PipeWire graph the app cares about --
    sinks, sources and the default sink -- all from ONE pw_dump per
    refresh, so there is a single poll instead of one per window. Windows
    read it on demand or subscribe for a callback on change. The core
    (update/subscribe/_notify) is synchronous and GTK-free for tests;
    start()/stop() add a GLib timer that refreshes off the main loop."""

    def __init__(self):
        self.sinks = []
        self.sources = []
        self.default_sink = None
        self._subs = []
        self._snap = None
        self._timer = 0
        self._busy = False
        self._glib = None

    def update(self, dump=None):
        """Refresh from one pw_dump (fetched if not given). Returns True if
        the snapshot changed. Synchronous; call it directly in tests."""
        if dump is None:
            dump = pw_dump()
        default = default_sink_from_dump(dump)
        if default is None:
            default = default_sink_name()    # rare fallback if not in dump
        self.default_sink = default
        self.sinks = list_sinks(dump, default=default)
        self.sources = list_sources(dump)
        snap = (tuple(s["name"] for s in self.sinks),
                tuple(s["name"] for s in self.sources),
                self.default_sink)
        changed = snap != self._snap
        self._snap = snap
        return changed

    def subscribe(self, cb):
        """Register cb(state), called after each change. Returns a callable
        that unsubscribes; callers MUST call it on teardown so a closed
        window's callback is not fired on dead widgets."""
        self._subs.append(cb)

        def off():
            try:
                self._subs.remove(cb)
            except ValueError:
                pass
        return off

    def _notify(self):
        for cb in list(self._subs):
            try:
                cb(self)
            except Exception:
                pass

    def start(self, interval_s=PW_POLL_S):
        """Begin the periodic refresh (idempotent). Uses GLib; only the app
        calls this -- tests drive update() directly."""
        if self._timer:
            return
        from gi.repository import GLib
        self._glib = GLib
        self._timer = GLib.timeout_add_seconds(interval_s, self._tick)

    def stop(self):
        if self._timer and self._glib is not None:
            self._glib.source_remove(self._timer)
        self._timer = 0

    def _tick(self):
        if self._busy:
            return True                      # previous refresh still running
        self._busy = True

        def work():
            dump = None
            try:
                dump = pw_dump()
            finally:
                self._glib.idle_add(self._apply, dump)
        _in_thread(work)
        return True                          # keep the timer running

    def _apply(self, dump):
        self._busy = False
        if dump is not None and self.update(dump):
            self._notify()
        return False


_app_state = None


def app_state():
    """The process-wide PWState singleton (one poll feeds every window)."""
    global _app_state
    if _app_state is None:
        _app_state = PWState()
    return _app_state
