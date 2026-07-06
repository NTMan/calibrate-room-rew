#!/usr/bin/env python3
"""PipeWire plumbing for the measurement wizard (ROADMAP Tasks 3/4).

Everything the live measurement needs below the CLI: graph inspection
over pw-dump, node resolution and identity checks, the per-device-eq
profile bypass, foreign-stream muting, sink volume control with the
adaptive auto-level controller, the raw f32 capture stream, playback
path verification and the single-sweep runner `run_take`. Extracted
verbatim from tools/measure_run.py so the GTK wizard (increment 4) can
drive takes from GUI callbacks without dragging in argparse, prompts
or the batch loop; measure_run.py remains the CLI on top.

Method notes (worth not re-deriving):

- No clock synchronization between playback and capture is attempted: the
  core aligns every take by the peak of its own linear impulse and averages
  in magnitude only (BT sink and USB mic run on independent clocks).
  pw-record simply starts BEFORE pw-play and stops after enough frames. The
  capture is pinned to the requested source with node.target (NOT --target,
  which the session manager overrides by relinking to the DEFAULT source --
  a wrong default silently records the wrong mic).
- EQ state (Task 4 lesson): the run bypasses our own profile on the target
  sink by deleting its key from the 'per-device-eq' metadata (the same
  mechanism the app's Bypass switch uses -- the WirePlumber hook flattens
  the node) and restores the exact graph string afterwards, including on
  any exception or ^C (context manager). The graph is read from the
  metadata, or, when the GUI has not published it this session (a cold
  PipeWire start seeds the hook from persisted state without touching the
  metadata), from that persisted state. What was found and from where, that
  it was bypassed and that it was restored is recorded in `eq_profile_state`.
  A failed restore is loudly reported with the manual recovery command.
- Path verification: shortly after pw-play starts, pw-dump must show our
  sweep stream linked DIRECTLY to the target node and to nothing else, and
  the target must be a real device (media.class Audio/Sink, device.api
  alsa*/bluez*). A dirty path (loopback sinks, effect chains, unknown
  nodes) aborts the run: a sweep through an unidentified chain is not a
  measurement of the device. Symmetrically, the capture stream must link
  FROM the requested source and no other, or the run aborts (a wrong
  default source hijacking the recording is a common, silent failure). The
  verdict and any unknown node names are kept in `path_clean`.
"""
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import measure_core as mc

METADATA_NAME = "per-device-eq"          # same object the app + WP hook use
PLAY_NODE = "pde-measure-sweep"
CAPTURE_NODE = "pde-measure-capture"
SINK_API_PREFIXES = ("alsa", "bluez")    # "real device" whitelist
AUTO_WINDOW = (-12.0, -6.0)              # capture peak target window, dBFS
AUTO_TARGET_DBFS = -8.0                  # aim high in the window for SNR
AUTO_MAX_ADJUST = 6                      # bounded steps need a little room
AUTO_START_VOLUME = 0.15                 # cubic; "start quiet"
AUTO_DEFAULT_SLOPE = 30.0                # dB per decade of cubic, 1st guess
AUTO_MAX_STEP = 3.0                      # cap the per-step cubic ratio
AUTO_EXPLORE_CEIL = 0.8                  # don't slam full volume while probing
AUTO_CLIP_BACKOFF = 0.85                 # stay this far below a clipping level
FULLSCALE = 0.999                        # |sample| >= this = clipped
HOT_DBFS = -1.0                          # peak above this = low headroom
REPAIR_MAX_MS = 2.0                      # interp this many ms of dropouts;
#                                          more non-finite than that = fault
VERIFY_AFTER_S = 0.4                     # pw-play start -> pw-dump link check
VERIFY_TIMEOUT_S = 3.0
CAPTURE_LEAD_S = 0.5                     # record head start (extra pre-roll)
EXTRA_TAIL_S = 1.0                       # decay + link latency margin


class MeasureError(RuntimeError):
    pass


class RefusalError(RuntimeError):
    """Precondition not met; nothing was played, nothing was changed."""


# --- subprocess plumbing -----------------------------------------------------

def _run(cmd, timeout=5.0):
    """Bounded helper run: a hung pw-* child must never hang the runner."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")
    except Exception as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def require_tools(tools):
    missing = [t for t in tools if shutil.which(t) is None]
    if missing:
        raise RefusalError("required PipeWire tools not in PATH: %s"
                           % " ".join(missing))


# --- pw-dump graph inspection ------------------------------------------------

def pw_dump():
    r = _run(["pw-dump"], timeout=10.0)
    if r.returncode != 0:
        raise MeasureError("pw-dump failed: %s" % (r.stderr.strip() or
                                                   r.returncode))
    try:
        return json.loads(r.stdout)
    except ValueError as e:
        raise MeasureError("pw-dump returned unparsable JSON: %s" % e)


def _props(obj):
    return (obj.get("info") or {}).get("props") or {}


def _params(obj):
    return (obj.get("info") or {}).get("params") or {}


def _nodes(dump):
    return [o for o in dump if o.get("type") == "PipeWire:Interface:Node"]


def _links(dump):
    out = []
    for o in dump:
        if o.get("type") != "PipeWire:Interface:Link":
            continue
        i = o.get("info") or {}
        out.append((i.get("output-node-id"), i.get("input-node-id")))
    return out


def resolve_node(dump, ident, want_class):
    """id, exact node.name, or unique case-insensitive substring of
    node.name/node.description among nodes of `want_class`."""
    ns = _nodes(dump)
    if re.fullmatch(r"\d+", str(ident)):
        for o in ns:
            if o["id"] == int(ident):
                return o
        raise RefusalError("no node with id %s" % ident)
    for o in ns:
        if _props(o).get("node.name") == ident:
            return o
    needle = str(ident).lower()
    hits = [o for o in ns
            if _props(o).get("media.class") == want_class
            and (needle in (_props(o).get("node.name") or "").lower()
                 or needle in (_props(o).get("node.description")
                               or "").lower())]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise RefusalError("no %s matches %r (pw-dump for names)"
                           % (want_class, ident))
    names = ", ".join(_props(o).get("node.name") or "?" for o in hits)
    raise RefusalError("%r is ambiguous: %s" % (ident, names))


def node_ident(obj):
    p = _props(obj)
    return {"id": obj["id"], "name": p.get("node.name"),
            "description": p.get("node.description"),
            "media_class": p.get("media.class"),
            "device_api": p.get("device.api")}


def check_sink_identity(sink):
    """Refuse anything that is not a real output device: measuring into a
    loopback/effect sink is measuring the wrong thing."""
    p = _props(sink)
    problems = []
    if p.get("media.class") != "Audio/Sink":
        problems.append("media.class is %r, expected Audio/Sink"
                        % p.get("media.class"))
    api = p.get("device.api") or ""
    if not api.startswith(SINK_API_PREFIXES):
        problems.append("device.api is %r, expected alsa*/bluez* "
                        "(a virtual/effect sink is not the device)" % api)
    if problems:
        raise RefusalError("target %r is not a measurable device:\n  %s"
                           % (p.get("node.name"), "\n  ".join(problems)))


def props_param(obj):
    """The Props param block that carries volume/mute/channelVolumes."""
    for d in _params(obj).get("Props", []):
        if isinstance(d, dict) and "channelVolumes" in d:
            return d
    return {}


def foreign_streams(dump, sink_id):
    """Output streams currently linked into the sink, ours excluded."""
    linked = {a for a, b in _links(dump) if b == sink_id}
    out = []
    for o in _nodes(dump):
        if o["id"] not in linked:
            continue
        p = _props(o)
        if p.get("media.class") != "Stream/Output/Audio":
            continue
        name = p.get("node.name") or ""
        if name.startswith("pde-measure"):
            continue
        out.append({"id": o["id"], "node_name": name,
                    "app": p.get("application.name") or p.get("app.name"),
                    "prior_mute": bool(props_param(o).get("mute", False)),
                    "muted_for_measure": False})
    return out


# --- per-device-eq metadata (profile bypass) ---------------------------------

def metadata_get(key):
    r = _run(["pw-metadata", "-n", METADATA_NAME, "0", key])
    m = re.search(r"key:'%s' value:'(.*?)' type:" % re.escape(key),
                  r.stdout, re.S)
    return m.group(1) if m else None


def metadata_set(key, value):
    r = _run(["pw-metadata", "-n", METADATA_NAME, "0", key, value])
    return r.returncode == 0 and "Found" in (r.stdout + r.stderr)


def metadata_clear(key):
    return _run(["pw-metadata", "-n", METADATA_NAME, "-d", "0", key]) \
        .returncode == 0


def wpstate_get(key):
    """Read a sink's graph from the WirePlumber hook's persisted state
    (a GKeyFile at $XDG_STATE_HOME/wireplumber/per-device-eq). The hook
    seeds its runtime table from here on a cold start and does NOT
    publish persisted graphs into the metadata, so a freshly-booted
    session where the GUI was never opened has the profile ONLY here."""
    base = os.environ.get("XDG_STATE_HOME") \
        or os.path.expanduser("~/.local/state")
    path = os.path.join(base, "wireplumber", "per-device-eq")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if line.startswith("[") or "=" not in line:
                    continue
                k, v = line.split("=", 1)     # node.name has no '=' itself
                if k == key:
                    return v or None
    except OSError:
        pass
    return None


class ProfileBypass:
    """Bypass our EQ on the sink for the duration of the measurement.

    This is exactly the app's own Bypass: clear the sink's key from the
    'per-device-eq' metadata and the WP hook flattens the node (applies a
    0 dB graph); write the graph back on exit to un-bypass. The graph is
    read from the metadata, or -- when the GUI has not published it this
    session -- from the hook's persisted state. Restore ALWAYS runs,
    exceptions and ^C included. The returned dict is `eq_profile_state`.
    """

    def __init__(self, key):
        self.key = key
        self.state = {"metadata_key": key, "profile": None,
                      "profile_source": None, "bypass": False,
                      "restored": None}

    def __enter__(self):
        prof = metadata_get(self.key)
        src = "metadata" if prof is not None else None
        if prof is None:
            prof = wpstate_get(self.key)
            src = "wpstate" if prof is not None else None
        self.state["profile"] = prof
        self.state["profile_source"] = src
        if prof is not None:
            if not metadata_clear(self.key):
                raise MeasureError("failed to clear %r from the %s metadata"
                                   % (self.key, METADATA_NAME))
            self.state["bypass"] = True
        return self.state

    def __exit__(self, *exc):
        if self.state["profile"] is None:
            return False
        ok = metadata_set(self.key, self.state["profile"])
        self.state["restored"] = bool(ok)
        if not ok:
            print("CRITICAL: failed to restore the EQ profile; put it back "
                  "manually:\n  pw-metadata -n %s 0 '%s' '%s'"
                  % (METADATA_NAME, self.key, self.state["profile"]),
                  file=sys.stderr)
        return False


class MuteOthers:
    """Mute foreign streams (Props mute=true) for the measurement and
    restore each stream's previous mute state after."""

    def __init__(self, streams, enabled):
        self.streams = streams if enabled else []

    @staticmethod
    def _set_mute(node_id, mute):
        r = _run(["pw-cli", "set-param", str(node_id), "Props",
                  "{ mute = %s }" % ("true" if mute else "false")])
        return r.returncode == 0

    def __enter__(self):
        for s in self.streams:
            if self._set_mute(s["id"], True):
                s["muted_for_measure"] = True
            else:
                print("WARNING: could not mute stream %s (%s)"
                      % (s["id"], s["node_name"]), file=sys.stderr)
        return self.streams

    def __exit__(self, *exc):
        for s in self.streams:
            if s["muted_for_measure"] and not self._set_mute(
                    s["id"], s["prior_mute"]):
                print("WARNING: could not restore mute state of stream %s "
                      "(%s)" % (s["id"], s["node_name"]), file=sys.stderr)
        return False


# --- volume ------------------------------------------------------------------

def sink_volume_state(dump, sink_id):
    """(cubic, raw channelVolumes, mute) from the sink's Props param.
    PipeWire stores channelVolumes linear; the user-facing value (wpctl,
    GNOME) is its cube root."""
    for o in _nodes(dump):
        if o["id"] == sink_id:
            d = props_param(o)
            cv = [float(v) for v in d.get("channelVolumes") or []]
            cubic = (sum(cv) / len(cv)) ** (1.0 / 3.0) if cv else None
            return cubic, cv, bool(d.get("mute", False))
    return None, [], False


def set_sink_volume(sink_id, cubic):
    """wpctl writes through to the device Route where one exists; raw
    Props writes on ALSA sinks do not stick."""
    r = _run(["wpctl", "set-volume", str(sink_id), "%.4f" % cubic])
    if r.returncode != 0:
        raise MeasureError("wpctl set-volume failed: %s" % r.stderr.strip())


def _clamp_vol(v):
    return max(0.02, min(1.0, v))


class AutoLevel:
    """Drive the sink volume so the capture peak lands in AUTO_WINDOW,
    with no assumption about the device's volume->gain law (a BT sink's
    is nothing like the software cube law). It brackets: once one probe
    is below the window and one is at/above it (or clipped), the level
    is interpolated in log-volume between the two and never exceeds the
    loud side. Before the bracket closes it steps with the slope
    measured from the last two probes (a conservative default for the
    first), capped per step and kept below any level seen to clip, so
    the first sound can neither blast nor run away."""

    def __init__(self):
        self.lo = None            # (v, peak): highest probe below the window
        self.hi = None            # (v, peak): lowest probe at/above / clipped
        self.prev = None          # (v, peak): the last probe, for the slope
        self.ceil = AUTO_EXPLORE_CEIL   # soft: lifts if we're stuck too quiet

    @staticmethod
    def in_window(peak):
        return AUTO_WINDOW[0] <= peak <= AUTO_WINDOW[1]

    def observe(self, v, peak, clipped):
        if clipped or peak > AUTO_WINDOW[1]:
            p = 0.0 if clipped else peak
            if self.hi is None or v < self.hi[0]:
                self.hi = (v, p)
        if not clipped and peak < AUTO_WINDOW[0]:
            if self.lo is None or v > self.lo[0]:
                self.lo = (v, peak)
            if v >= self.ceil - 1e-3:     # at the ceiling yet still too quiet
                self.ceil = 1.0           # -> the device needs more, lift it

    def _slope(self, v, peak):
        if self.prev is not None:
            v0, p0 = self.prev
            if v > 0 and v0 > 0 and abs(math.log10(v / v0)) > 1e-3:
                m = (peak - p0) / math.log10(v / v0)
                if m > 1.0:
                    return m
        return AUTO_DEFAULT_SLOPE

    def next_volume(self, v, peak):
        if self.lo and self.hi:                  # bracketed: interpolate
            (vl, pl), (vh, ph) = self.lo, self.hi
            if ph - pl > 1.0:
                frac = (AUTO_TARGET_DBFS - pl) / (ph - pl)
                nv = vl * (vh / vl) ** min(0.95, max(0.05, frac))
            else:
                nv = math.sqrt(vl * vh)
            nv = min(nv, vh * AUTO_CLIP_BACKOFF)
        else:
            slope = self._slope(v, peak)
            nv = v * 10 ** ((AUTO_TARGET_DBFS - peak) / slope)
            nv = min(nv, v * AUTO_MAX_STEP)       # no blind leap to a blast
            ceil = (self.hi[0] * AUTO_CLIP_BACKOFF if self.hi
                    else self.ceil)
            nv = min(nv, ceil)
        self.prev = (v, peak)                     # for the next slope estimate
        return _clamp_vol(nv)


def peak_dbfs(x):
    if not len(x):
        return float("-inf")
    p = float(np.max(np.abs(x)))
    if not math.isfinite(p):
        return float("nan")               # NaN/Inf in the capture
    return 20.0 * math.log10(p) if p > 0 else float("-inf")


def repair_nonfinite(x):
    """Replace isolated non-finite samples (a capture xrun/dropout) with
    a linear interpolation of the surrounding good samples."""
    bad = ~np.isfinite(x)
    idx = np.arange(len(x))
    out = x.copy()
    out[bad] = np.interp(idx[bad], idx[~bad], x[~bad])
    return out


# --- capture -----------------------------------------------------------------

class CaptureStream:
    """pw-record streaming raw interleaved f32 to stdout (filename '-'),
    accumulated on a reader thread. Raw-to-stdout instead of letting
    pw-record write the wav: no header-finalization worries on kill, and
    the stop condition is an exact frame count, not a timer."""

    def __init__(self, target, channels, rate):
        self.channels = channels
        self.rate = rate
        self.target = int(target)
        # --raw is REQUIRED: without it pw-record prefixes the stdout stream
        # with a format descriptor (rate/channels POD), whose bytes we would
        # read as audio -- its 0xffffffff field decodes to a NaN at the start
        # of channel 0 every capture. --raw gives a bare interleaved f32
        # stream. Pin to the source via node.target (NOT --target, which the
        # session manager relinks to the default source); node.dont-reconnect
        # keeps it there if the source blinks.
        cmd = ["pw-record", "--raw",
               "-P", "{ node.name = %s, node.target = %d, "
                     "node.dont-reconnect = true, application.name = "
                     "\"per-device-eq measure\" }"
                     % (CAPTURE_NODE, self.target),
               "--format", "f32", "--rate", str(int(rate)),
               "--channels", str(int(channels)), "-"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
        self._chunks = []
        self._bytes = 0
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _reader(self):
        while True:
            chunk = self.proc.stdout.read(65536)
            if not chunk:
                return
            with self._lock:
                self._chunks.append(chunk)
                self._bytes += len(chunk)

    def wait_frames(self, n_frames, timeout):
        need = n_frames * self.channels * 4
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._bytes >= need:
                    return
            if self.proc.poll() is not None:
                raise MeasureError("pw-record exited early (rc=%s)"
                                   % self.proc.returncode)
            time.sleep(0.05)
        raise MeasureError("capture timed out: got %d of %d frames "
                           "(is the mic source alive?)"
                           % (self._bytes // (4 * self.channels), n_frames))

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self._thread.join(timeout=3)

    def data(self):
        buf = b"".join(self._chunks)
        n = len(buf) // (4 * self.channels) * self.channels
        return np.frombuffer(buf[:n * 4], dtype="<f4") \
            .reshape(-1, self.channels).astype(np.float64)


# --- playback + path verification --------------------------------------------

def _play_error(play):
    return MeasureError("pw-play failed (rc=%d): %s"
                        % (play.returncode,
                           (play.stderr.read() or "").strip()))


def verify_path(sink, play):
    """The sweep stream must exist and link into the target node and
    NOTHING else. Returns the path_clean dict; raises on a dirty path
    (and on pw-play dying before the stream ever links)."""
    deadline = time.monotonic() + VERIFY_TIMEOUT_S
    stream, targets = None, set()
    while time.monotonic() < deadline:
        if play.poll() is not None and play.returncode != 0:
            raise _play_error(play)
        dump = pw_dump()
        for o in _nodes(dump):
            if _props(o).get("node.name") == PLAY_NODE:
                stream = o
        if stream is not None:
            targets = {b for a, b in _links(dump) if a == stream["id"]}
            if targets:
                break
        time.sleep(0.2)
    if stream is None or not targets:
        raise MeasureError("sweep stream never appeared/linked; "
                           "cannot verify the playback path")
    unknown = []
    for t in sorted(targets - {sink["id"]}):
        name = "?"
        for o in _nodes(dump):
            if o["id"] == t:
                name = _props(o).get("node.name") or "?"
        unknown.append({"id": t, "node_name": name})
    d = props_param(stream)
    cv = d.get("channelVolumes") or [None]
    vol = d.get("volume")
    stream_volume = vol if vol is not None else cv[0]
    info = {"verified": not unknown and sink["id"] in targets,
            "target": node_ident(sink),
            "playback_stream": {"id": stream["id"], "name": PLAY_NODE,
                                "volume": stream_volume},
            "unknown_nodes": unknown}
    if unknown:
        raise MeasureError(
            "playback path is not clean, refusing to measure through an "
            "unidentified chain: %s"
            % ", ".join("%(node_name)s (id %(id)s)" % u for u in unknown))
    if stream_volume is not None and abs(stream_volume - 1.0) > 1e-3:
        print("WARNING: sweep stream volume is %.3f, not 1.0 (session "
              "manager restore rule?)" % stream_volume, file=sys.stderr)
    return info


def verify_capture(source, cap):
    """The capture stream must link FROM the requested source and no
    other. Raises if it is linked to a different source -- a wrong
    default source hijacks the stream and silently records the wrong mic
    (quiet, garbage SNR) instead of erroring. Mirrors verify_path."""
    deadline = time.monotonic() + VERIFY_TIMEOUT_S
    node, sources = None, set()
    while time.monotonic() < deadline:
        if cap.proc.poll() is not None:
            raise MeasureError("pw-record exited early (rc=%s)"
                               % cap.proc.returncode)
        dump = pw_dump()
        for o in _nodes(dump):
            if _props(o).get("node.name") == CAPTURE_NODE:
                node = o
        if node is not None:
            sources = {a for a, b in _links(dump) if b == node["id"]}
            if sources:
                break
        time.sleep(0.2)
    if node is None or not sources:
        raise MeasureError("capture stream never appeared/linked; "
                           "cannot verify the mic path (is the source "
                           "alive?)")
    wrong = []
    for s in sorted(sources - {source["id"]}):
        name = "?"
        for o in _nodes(dump):
            if o["id"] == s:
                name = _props(o).get("node.name") or "?"
        wrong.append({"id": s, "node_name": name})
    if source["id"] not in sources or wrong:
        raise MeasureError(
            "capture is linked to the wrong source (a wrong default "
            "recording device hijacked the stream): got %s, wanted %s. "
            "node.target pinning failed."
            % (", ".join("%(node_name)s (id %(id)s)" % w for w in wrong)
               or "nothing", node_ident(source)["name"]))
    return {"verified": True, "source": node_ident(source)}


def run_take(sink, source, wav_path, wav_duration_s, channels, rate,
             verify, raw_dump_path=None):
    """One sweep: start capture, play the wav, collect exactly enough
    frames. Returns (frames x channels array, path_clean or None). With
    raw_dump_path, the untouched capture is written there first,
    for glitch diagnostics."""
    cap = CaptureStream(source["id"], channels, rate)
    play = None
    path_info = None
    try:
        time.sleep(CAPTURE_LEAD_S)
        if verify:
            cap_info = verify_capture(source, cap)
        play = subprocess.Popen(
            ["pw-play", "--volume", "1.0",
             "-P", "{ node.name = %s, node.target = %d, "
                   "node.dont-reconnect = true, application.name = "
                   "\"per-device-eq measure\" }" % (PLAY_NODE, sink["id"]),
             wav_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if verify:
            time.sleep(VERIFY_AFTER_S)
            path_info = verify_path(sink, play)
            path_info["capture"] = cap_info
        rc = play.wait(timeout=wav_duration_s + 30)
        if rc != 0:
            raise _play_error(play)
        need = int((CAPTURE_LEAD_S + wav_duration_s + EXTRA_TAIL_S) * rate)
        cap.wait_frames(need, timeout=wav_duration_s + 60)
    finally:
        if play is not None and play.poll() is None:
            play.kill()
        cap.stop()
    if raw_dump_path is not None:
        import soundfile as sf
        sf.write(raw_dump_path, cap.data(), rate, subtype="FLOAT")
    return cap.data(), path_info


# --- sweep files ---------------------------------------------------------

def write_sweep_files(outdir, sweep, pre_s, post_s):
    import soundfile as sf
    pad0 = np.zeros(int(pre_s * sweep.fs))
    pad1 = np.zeros(int(post_s * sweep.fs))
    wav = os.path.join(outdir, "sweep.wav")
    sf.write(wav, np.concatenate([pad0, sweep.signal, pad1])
             .astype("float32"), sweep.fs, subtype="FLOAT")
    sf.write(os.path.join(outdir, "sweep-inverse.wav"),
             mc.inverse_sweep(sweep).astype("float32"), sweep.fs,
             subtype="FLOAT")
    with open(wav + ".json", "w") as f:
        json.dump({"n_samples": sweep.n_samples, "fs": sweep.fs,
                   "f_start": sweep.f_start, "f_end": sweep.f_end,
                   "level_dbfs": sweep.level_dbfs, "pre_silence_s": pre_s,
                   "post_silence_s": post_s}, f, indent=1)
    return wav


def save_take_wav(outdir, index, data, rate):
    import soundfile as sf
    path = os.path.join(outdir, "take%02d.wav" % index)
    sf.write(path, data.astype("float32"), rate, subtype="FLOAT")
    return path


def default_save_base():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    local = os.path.join(root, "tests", "fixtures-local")
    return local if os.path.isdir(local) else os.getcwd()
