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

MeasureSession is that wizard-facing API: preconditions in the
constructor (refuses before any sound), profile bypass + foreign-stream
muting + the quiet auto-level start on __enter__ (restored on ANY
exit), take(channel) for one physical sweep returning a structured
TakeOutcome -- the analyzed curve plus the running per-frequency spread
across the channel's accepted takes, the GUI's live fan --, discard()
to drop a bad take, accept_level() to keep measuring at a stuck level,
finalize(channel) writing one result.json per channel (fit_peq
--left/--right takes it from there). No printing, no prompts.

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
- Foreign streams: anything else playing into the sink during the sweep is
  measured too. By default their presence refuses the run with a list;
  --mute-others instead mutes them (Props mute=true via pw-cli) for the
  duration and restores the previous mute state after. The list, muted or
  not, goes into `foreign_streams` of the result.
- Levels policy: the digital sweep level is FIXED at -6 dBFS (core), the
  sweep stream volume is forced to 1.0 (pw-play --volume, verified from
  the node's Props), and the sink volume is never touched -- the protocol
  is to measure at the working listening level via the sink's own control.
  The only exception is --auto-level: starting from a quiet volume
  (min(current, 0.15) cubic) it adjusts the sink volume via wpctl until
  the capture peak lands in the -12..-6 dBFS window, after an explicit
  confirmation. It assumes nothing about the device's volume->gain law
  (a BT sink's is nothing like the software cube law): it brackets the
  window and interpolates in log-volume between a too-quiet and a
  too-loud probe, stepping with the slope measured from the last two
  probes until then, capped per step and held below any level seen to
  clip -- so the first sound neither blasts nor overshoots into a clip
  (see AutoLevel). Without --auto-level the sink volume is never raised
  above its value at start (it is not written at all). Everything ends
  up in `levels`.
- SNR: pw-record is asked for a bare stream with --raw; without it the
  stdout stream is prefixed with a format descriptor (rate/channels POD)
  whose bytes decode to a NaN at the start of channel 0 on every
  capture. Each take gets a quick pre-roll noise-floor check right after
  capture (same threshold and wording as the core) so a noisy room is
  caught on take 1, not after five reseats; up to REPAIR_MAX_MS of
  isolated non-finite (NaN/Inf) samples on the analyzed channel are
  interpolated as a capture xrun (with a warning) while a larger flood
  aborts as a faulty input; the non-finite scan covers ALL channels, not
  just the analyzed one, so a glitch on the other side is not invisible.
  A full-scale sample count flags a genuinely clipped (unusable) take
  and a peak above HOT_DBFS is only a low-headroom advisory. The
  authoritative numbers are still computed by the core from the aligned
  impulse.
- Raw takes (float32 wav, all captured channels) plus the sweep wav, its
  sidecar and the analytic inverse (REW cross-check) are saved under
  tests/fixtures-local/<device>_<stamp>/ -- .gitignore'd, real captures
  never enter git.
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
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from . import measure_core as mc

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


class FaultyCaptureError(MeasureError):
    """A flood of non-finite samples: a broken input, not a dropout.
    Neutral wording; the CLI appends its --channel flag hint."""

    def __init__(self, channel, channels, bad):
        super().__init__(
            "channel %d capture has %d non-finite sample(s) (NaN/Inf) -- "
            "too many to be a dropout; the input is faulty, not merely "
            "quiet." % (channel, bad))
        self.channel = channel
        self.channels = channels
        self.bad = bad


class MeasureCancelled(Exception):
    """A sweep was cancelled by the user (Stop). A control-flow signal,
    not an error: the child processes are killed and the partial capture
    is discarded, so nothing is stored."""


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

    def wait_frames(self, n_frames, timeout, cancel=None):
        need = n_frames * self.channels * 4
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                raise MeasureCancelled()
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
             verify, raw_dump_path=None, cancel=None):
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
        deadline = time.monotonic() + wav_duration_s + 30
        while True:
            if cancel is not None and cancel.is_set():
                raise MeasureCancelled()
            rc = play.poll()
            if rc is not None:
                break
            if time.monotonic() > deadline:
                raise MeasureError("pw-play did not finish in time")
            time.sleep(0.05)
        if rc != 0:
            raise _play_error(play)
        need = int((CAPTURE_LEAD_S + wav_duration_s + EXTRA_TAIL_S) * rate)
        cap.wait_frames(need, timeout=wav_duration_s + 60, cancel=cancel)
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


# --- session: the wizard-facing single-take API ---------------------------

@dataclass
class SessionConfig:
    """Everything a measurement session needs up front. The analyzed
    channel is deliberately NOT here: it is an argument of every take,
    so one session accumulates L and R side by side."""
    sink: str
    source: str
    channels: int = 1
    samples: int = mc.DEFAULT_N
    fs: int = mc.DEFAULT_FS
    f_start: float = mc.DEFAULT_F_START
    f_end: float = mc.DEFAULT_F_END
    pre_silence: float = 1.0
    post_silence: float = 0.5
    cal: str = None
    smoothing: int = 6
    device: str = None
    rig: str = None
    mic: str = None
    save_dir: str = None
    mute_others: bool = False
    auto_level: bool = False
    raw_capture_dump: bool = False
    start_volume: float = None      # applied on enter when not auto_level


@dataclass
class TakeRecord:
    """One accepted take: the analyzed curve and its vital signs."""
    id: int                   # monotonic; also the take%02d.wav number
    channel: int              # the capture channel this take analyzed
    freq_hz: np.ndarray       # analysis grid (log, shared per session)
    mag_db: np.ndarray        # raw magnitude, no cal, no smoothing
    delay_ms: float           # linear-IR peak position in the recording
    snr_db: object            # core estimate from the aligned impulse
    peak_dbfs: float
    clipped: int              # full-scale sample count (0 = clean)
    repaired: int             # interpolated non-finite samples
    wav_path: str


TAKE_CLEAN = "clean"        # counts toward a channel's three good takes
TAKE_FLAGGED = "flagged"    # usable but not ideal; does NOT count
TAKE_CLIPPED = "clipped"    # unusable


def take_quality(rec):
    """Classify an accepted take. Single source of truth for the wizard's
    ring/row status and the 'three clean takes' rule -- CLI, GUI and tests
    all judge quality here, using the same thresholds the live take() path
    warns on. Clipping is unusable (red); a hot peak (>= HOT_DBFS) or low
    SNR (< SNR_WARN_DB) is usable-but-flagged (amber) and does not count;
    everything else is clean (green). A repaired single-sample glitch
    stays clean -- the take is unaffected by an interpolated sample --
    while a None SNR (no onset found) is treated as unknown, not low."""
    if rec.clipped:
        return TAKE_CLIPPED
    if rec.peak_dbfs >= HOT_DBFS:
        return TAKE_FLAGGED
    if rec.snr_db is not None and rec.snr_db < mc.SNR_WARN_DB:
        return TAKE_FLAGGED
    return TAKE_CLEAN


@dataclass
class TakeOutcome:
    """What one MeasureSession.take() call produced.

    kind == "take": `take` is the accepted TakeRecord and `spread_db`
    the per-frequency std (ddof=1) across the channel's accepted takes
    (None until there are two) -- the live fan and its width.
    kind == "level_probe": auto-level moved the sink volume and threw
    the capture away; `level` says from/to/step. Just take() again.
    kind == "level_stuck": auto-level cannot reach the target window;
    the capture is held pending. accept_level() keeps it as a take at
    the current level (the old confirm() path); the next take() drops
    it instead.
    `notes` are printable warnings in the CLI's exact wording.
    """
    kind: str
    take: TakeRecord = None
    spread_db: object = None
    level: dict = None
    notes: list = field(default_factory=list)


class MeasureSession:
    """Single-take measurement lifecycle for the CLI and the wizard.

    Preconditions run in the constructor and raise RefusalError before
    anything is played or changed. __enter__ writes the sweep files and
    engages foreign-stream muting and the profile bypass (restored on
    ANY exit) and, with auto_level, sets the quiet start volume.
    take(channel) runs one physical sweep and returns a TakeOutcome;
    discard() drops a bad take from the accumulation (the wav stays on
    disk as evidence, ids are never reused); finalize(channel)
    assembles the channel's result via measure_core and writes
    result.json. No printing, no prompts: decisions surface as
    outcomes, warning texts as `notes`.
    """

    def __init__(self, cfg):
        if cfg.channels < 1:
            raise RefusalError("channels must be >= 1")
        tools = ["pw-dump", "pw-metadata", "pw-play", "pw-record"]
        if cfg.auto_level:
            tools.append("wpctl")
        if cfg.mute_others:
            tools.append("pw-cli")
        require_tools(tools)
        self.cfg = cfg
        self.precondition_notes = []

        dump = pw_dump()
        self.sink = resolve_node(dump, cfg.sink, "Audio/Sink")
        check_sink_identity(self.sink)
        self.source = resolve_node(dump, cfg.source, "Audio/Source")
        src_p = _props(self.source)
        if src_p.get("media.class") != "Audio/Source":
            raise RefusalError(
                "capture target %r is %r, expected Audio/Source"
                % (cfg.source, src_p.get("media.class")))
        if not (src_p.get("device.api") or "").startswith("alsa"):
            self.precondition_notes.append(
                "WARNING: mic source device.api is %r; measurement mics "
                "are expected on USB/ALSA" % src_p.get("device.api"))
        self.sink_ident = node_ident(self.sink)
        self.source_ident = node_ident(self.source)

        v0, raw0, muted = sink_volume_state(dump, self.sink["id"])
        if muted:
            raise RefusalError("sink is muted; unmute it and set the "
                               "working listening level first")
        if v0 is None:
            self.precondition_notes.append(
                "WARNING: could not read the sink volume from pw-dump")
        self.volume_start = v0
        self._raw0 = raw0

        self.foreign = foreign_streams(dump, self.sink["id"])
        if self.foreign and not cfg.mute_others:
            raise RefusalError(
                "other streams are playing into this sink (a sweep on top "
                "of them is not a measurement):\n  %s\nstop them or re-run "
                "with --mute-others" % "\n  ".join(
                    "id %(id)s  %(node_name)s  app=%(app)s" % s
                    for s in self.foreign))

        self.sweep = mc.generate_sweep(cfg.samples, cfg.fs, cfg.f_start,
                                       cfg.f_end)
        self.wav_duration = (cfg.pre_silence + self.sweep.duration_s
                             + cfg.post_silence)
        self.freqs = mc.log_grid()          # process_takes' exact grid
        slug = re.sub(r"[^\w.+-]+", "_",
                      cfg.device or self.sink_ident["name"]
                      or "device").strip("_")
        self.outdir = os.path.join(
            cfg.save_dir or default_save_base(),
            "%s_%s" % (slug, datetime.now().strftime("%Y%m%d-%H%M%S")))

        self.wav = None                     # written on __enter__
        self.path_clean = None
        self.eq_state = None
        self._stack = None
        self._cancel = threading.Event()    # set by cancel() to abort a sweep
        self._v_cur = v0
        self._leveled = not cfg.auto_level
        self._auto_ctl = AutoLevel()
        self._auto_state = {"enabled": bool(cfg.auto_level),
                            "adjustments": 0, "initial": None,
                            "final": None, "in_window": None}
        self._take_seq = 0                  # take%02d numbers, never reused
        self._takes = {}                    # channel -> [(record, samples)]
        self._pending = None                # capture awaiting accept_level

    # -- lifecycle ----------------------------------------------------------

    def __enter__(self):
        os.makedirs(self.outdir, exist_ok=True)
        self.wav = write_sweep_files(self.outdir, self.sweep,
                                     self.cfg.pre_silence,
                                     self.cfg.post_silence)
        with ExitStack() as stack:
            if self.cfg.auto_level:
                v = min(self.volume_start
                        if self.volume_start is not None else 1.0,
                        AUTO_START_VOLUME)
                self._auto_state["initial"] = round(v, 4)
                set_sink_volume(self.sink["id"], v)
                self._v_cur = v
            elif self.cfg.start_volume is not None:
                set_sink_volume(self.sink["id"], self.cfg.start_volume)
                self._v_cur = self.cfg.start_volume
            self._stack = stack.pop_all()
        return self

    def __exit__(self, *exc):
        stack, self._stack = self._stack, None
        if stack is not None:
            return stack.__exit__(*exc)
        return False

    # -- one physical sweep --------------------------------------------------

    def cancel(self):
        """Abort the sweep in flight (from the Stop button, on another
        thread): the running take() raises MeasureCancelled, its child
        processes are killed and the partial capture is dropped. A no-op
        when nothing is playing -- take() clears the flag as it starts."""
        self._cancel.set()

    def _mute_foreign(self, on):
        """Mute (on) or restore (off) the foreign streams for ONE sweep, so
        other audio is silenced only while the sweep plays and comes back
        immediately after, not at window close. No-op unless mute_others."""
        if not self.cfg.mute_others:
            return
        for s in self.foreign:
            if on:
                if MuteOthers._set_mute(s["id"], True):
                    s["muted_for_measure"] = True
            elif s["muted_for_measure"]:
                MuteOthers._set_mute(s["id"], s["prior_mute"])

    def take(self, channel, analyze=None):
        """One sweep played and captured, analyzed on capture column
        `analyze` (defaults to `channel`) but stored under `channel`, the
        profile channel, so one capture column can feed several profile
        channels (e.g. measure the left cup on the right mic)."""
        cfg = self.cfg
        a = channel if analyze is None else analyze
        if not 0 <= a < cfg.channels:
            raise RefusalError("capture column %d out of range for a "
                               "%d-channel capture" % (a, cfg.channels))
        if self.wav is None:
            raise MeasureError("session not entered (use `with session:`)")
        self._pending = None                # a new sweep supersedes it
        self._cancel.clear()                # fresh; cancel() sets it to abort
        raw_path = (os.path.join(self.outdir,
                                 "raw%02d.wav" % (self._take_seq + 1))
                    if cfg.raw_capture_dump else None)
        self._mute_foreign(True)            # silence others for THIS sweep
        try:
            eq = ProfileBypass(self.sink_ident["name"])
            self.eq_state = eq.__enter__()  # bypass the device EQ for it
            try:
                data, info = run_take(self.sink, self.source, self.wav,
                                      self.wav_duration, cfg.channels,
                                      self.sweep.fs,
                                      verify=self.path_clean is None,
                                      raw_dump_path=raw_path,
                                      cancel=self._cancel)
            finally:
                eq.__exit__(None, None, None)   # restore the EQ right after
        finally:
            self._mute_foreign(False)       # unmute right after the sweep
        if info is not None:
            self.path_clean = info

        notes = []
        # diagnostic: scan ALL channels, not just the one we analyze,
        # so a glitch on the other channel isn't invisible
        for c in range(data.shape[1]):
            w = np.nonzero(~np.isfinite(data[:, c]))[0]
            if w.size:
                notes.append("note: %d non-finite sample(s) on channel %d "
                             "at %s of %d"
                             % (w.size, c, list(w[:6]), data.shape[0]))
        chan = data[:, a]
        where = np.nonzero(~np.isfinite(chan))[0]
        bad = int(where.size)
        if bad:
            limit = max(1, int(REPAIR_MAX_MS / 1000.0 * self.sweep.fs))
            if bad > limit or bad >= len(chan):
                raise FaultyCaptureError(a, cfg.channels, bad)
            chan = repair_nonfinite(chan)
            data = data.copy()
            data[:, a] = chan               # keep the saved take finite
            notes.append("WARNING: interpolated %d non-finite capture "
                         "sample(s) on column %d at %s of %d -- a benign "
                         "single-sample glitch during the sweep; the take "
                         "is unaffected."
                         % (bad, a, list(where[:6]), len(chan)))
        pk = peak_dbfs(chan)
        clipped = int(np.count_nonzero(np.abs(chan) >= FULLSCALE))
        if clipped:
            notes.append("WARNING: %d sample(s) at full scale -- the sweep "
                         "is clipped and this take is unusable; lower the "
                         "sink volume (or use --auto-level) and remeasure."
                         % clipped)
        elif pk >= HOT_DBFS:
            notes.append("WARNING: capture peak %.1f dBFS leaves little "
                         "headroom (risk of inter-sample clipping); "
                         "consider a lower level or --auto-level (targets "
                         "%g..%g dBFS)." % (pk, *AUTO_WINDOW))

        if not self._leveled:
            auto = self._auto_state
            self._auto_ctl.observe(self._v_cur, pk, bool(clipped))
            v_new = self._auto_ctl.next_volume(self._v_cur, pk)
            stuck = abs(v_new - self._v_cur) < 1e-3
            if not clipped and self._auto_ctl.in_window(pk):
                self._leveled, auto["in_window"] = True, True
            elif auto["adjustments"] >= AUTO_MAX_ADJUST or stuck:
                auto["in_window"] = False
                why = ("the level cannot be moved further (at %.0f%%)"
                       % (100 * self._v_cur) if stuck
                       else "%d adjustments" % AUTO_MAX_ADJUST)
                notes.append("WARNING: auto-level gave up after %s (peak "
                             "%.1f dBFS outside %g..%g)"
                             % (why, pk, *AUTO_WINDOW))
                self._pending = (channel, data, chan, pk, clipped, bad)
                return TakeOutcome(
                    "level_stuck", notes=notes,
                    level={"peak_dbfs": pk, "volume": self._v_cur,
                           "why": why, "window": AUTO_WINDOW})
            else:
                auto["adjustments"] += 1
                level = {"peak_dbfs": pk, "volume_from": self._v_cur,
                         "volume_to": v_new, "step": auto["adjustments"],
                         "max_steps": AUTO_MAX_ADJUST}
                set_sink_volume(self.sink["id"], v_new)
                self._v_cur = v_new
                return TakeOutcome("level_probe", level=level, notes=notes)
        return self._accept(channel, data, chan, pk, clipped, bad, notes)

    def accept_level(self):
        """Keep the pending level_stuck capture as a take at the current
        level -- the caller's 'continue anyway' decision."""
        if self._pending is None:
            raise MeasureError("no leveling decision is pending")
        channel, data, chan, pk, clipped, repaired = self._pending
        self._pending = None
        self._leveled = True
        return self._accept(channel, data, chan, pk, clipped, repaired, [])

    def relevel(self):
        """Re-arm auto-level: the next take() ramps from a safe-low volume
        and finds the level again -- the wizard's 're-measure the level',
        for when the remembered level no longer fits (mic moved, fit on
        the rig changed). Existing takes are kept; only future sweeps
        re-level. Only valid inside the session (after __enter__)."""
        self._leveled = False
        self._pending = None
        self._auto_ctl = AutoLevel()
        self._auto_state = {"enabled": True, "adjustments": 0,
                            "initial": None, "final": None,
                            "in_window": None}
        v = min(self._v_cur if self._v_cur is not None else 1.0,
                AUTO_START_VOLUME)
        self._auto_state["initial"] = round(v, 4)
        set_sink_volume(self.sink["id"], v)
        self._v_cur = v

    def _accept(self, channel, data, chan, pk, clipped, repaired, notes):
        snr = self._quick_snr(chan)
        if snr is not None and snr < mc.SNR_WARN_DB:
            notes.append("WARNING: low SNR (%.1f dB): raise the level or "
                         "kill the noise source" % snr)
        self._take_seq += 1
        path = save_take_wav(self.outdir, self._take_seq, data,
                             self.sweep.fs)
        t = mc.analyze_take(chan, self.sweep, self.freqs)
        rec = TakeRecord(self._take_seq, channel, self.freqs, t.mag_db,
                         t.delay_ms, t.snr_db, pk, clipped, repaired, path)
        self._takes.setdefault(channel, []).append((rec, chan))
        return TakeOutcome("take", take=rec,
                           spread_db=self.spread_db(channel), notes=notes)

    def _quick_snr(self, chan):
        """Fast per-take noise-floor check so a noisy room is caught
        before the next reseat. Onset = first sustained crossing of 10x
        the pre-roll RMS; threshold and wording match the core."""
        fs = self.sweep.fs
        head = chan[:int(0.4 * fs)]
        noise = math.sqrt(float(np.mean(head ** 2))) if len(head) else 0.0
        thr = max(10.0 * noise, 1e-6)
        over = np.flatnonzero(np.abs(chan) > thr)
        if not len(over):
            return None
        snr, _, _ = mc.estimate_snr(chan, int(over[0]), self.sweep)
        return snr

    # -- the accumulated fan --------------------------------------------------

    def takes_of(self, channel):
        """The channel's accepted TakeRecords, oldest first."""
        return [rec for rec, _ in self._takes.get(channel, [])]

    def spread_db(self, channel):
        """Per-frequency std (ddof=1) across the channel's accepted
        takes; None until there are two. The live fan's width."""
        recs = self.takes_of(channel)
        if len(recs) < 2:
            return None
        return mc.average_takes(recs)[1]

    def discard(self, channel, take_id):
        """Drop a bad take from the accumulation. The wav stays on disk
        as evidence; ids and file numbers are never reused."""
        entries = self._takes.get(channel, [])
        for i, (rec, _) in enumerate(entries):
            if rec.id == take_id:
                del entries[i]
                return rec
        raise MeasureError("no take %s on channel %d" % (take_id, channel))

    # -- result ---------------------------------------------------------------

    def finalize(self, channel, out_path=None, cal=None):
        """Average the channel's accepted takes into a result dict via
        measure_core.process_takes and write it as result.json.

        cal defaults to the session's cfg.cal; pass cal= to override per
        channel. The wizard measures both ears in one session but each
        coupler has its own mic-cal file (L_RAW vs R_RAW), so it finalizes
        each channel with that channel's cal. mag_db_uncal is stored
        regardless, so a different cal can still be applied later."""
        entries = self._takes.get(channel, [])
        if not entries:
            raise MeasureError("no accepted takes on channel %d" % channel)
        dump = pw_dump()
        v_final, raw_final, _ = sink_volume_state(dump, self.sink["id"])
        v_report = v_final if v_final is not None else self._v_cur
        auto = dict(self._auto_state)
        auto["final"] = (round(v_report, 4) if self.cfg.auto_level
                         else None)
        levels = {
            "sink_volume": (round(v_report, 4)
                            if v_report is not None else None),
            "sink_volume_start": (round(self.volume_start, 4)
                                  if self.volume_start is not None
                                  else None),
            "sink_channel_volumes": raw_final or self._raw0,
            "stream_volume": (self.path_clean or {}).get(
                "playback_stream", {}).get("volume"),
            "capture_peak_dbfs": [round(r.peak_dbfs, 2)
                                  for r, _ in entries],
            "auto_level": auto,
        }
        result = mc.process_takes(
            [samples for _, samples in entries], self.sweep,
            cal=(cal if cal is not None else self.cfg.cal),
            smoothing_fraction=self.cfg.smoothing,
            device=(self.cfg.device or self.sink_ident["description"]
                    or self.sink_ident["name"]),
            rig=self.cfg.rig, mic=self.cfg.mic,
            eq_profile_state=self.eq_state, levels=levels,
            path_clean=self.path_clean, foreign_streams=self.foreign)
        out = out_path or os.path.join(self.outdir, "result.json")
        mc.save_result(result, out)
        return result
