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
    mixer UIs show who is listening to what."""
    cmd = ["pw-record", "--target", str(node),
           "-P", "{ stream.capture.sink = true, node.name = per-device-eq-meter,"
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
