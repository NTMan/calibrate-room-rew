#!/usr/bin/env python3
"""Real-path measurement runner (ROADMAP Task 3, increment 2).

Orchestrates a live sweep measurement on top of tools/measure_core.py:
plays the Farina sweep through a PipeWire sink (pw-play) while capturing
the measurement mic (pw-record), then feeds the takes to the offline core.
CLI only; the GTK wizard is increment 4, the constrained fit increment 3.

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
- Foreign streams: anything else playing into the sink during the sweep is
  measured too. By default their presence refuses the run with a list;
  --mute-others instead mutes them (Props mute=true via pw-cli) for the
  duration and restores the previous mute state after. The list, muted or
  not, goes into `foreign_streams` of the result.
- Path verification: shortly after pw-play starts, pw-dump must show our
  sweep stream linked DIRECTLY to the target node and to nothing else, and
  the target must be a real device (media.class Audio/Sink, device.api
  alsa*/bluez*). A dirty path (loopback sinks, effect chains, unknown
  nodes) aborts the run: a sweep through an unidentified chain is not a
  measurement of the device. Symmetrically, the capture stream must link
  FROM the requested source and no other, or the run aborts (a wrong
  default source hijacking the recording is a common, silent failure). The
  verdict and any unknown node names are kept in `path_clean`.
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

CI without hardware: tests/test_measure_run.py runs this file unmodified
against fake pw-* executables (tests/shims/) on PATH; the fake pw-record
answers with the sweep processed through a known pde_audit biquad chain,
so the whole orchestration closes against chain_curve() within the core
tolerance. Manual dry-run: PATH="tests/shims:$PATH" tools/measure_run.py ...

Exit codes: 0 ok, 1 measurement error, 2 environment/precondition refusal,
3 declined confirmation, 130 interrupted.
"""
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

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


# --- interaction ---------------------------------------------------------

def confirm(prompt, assume_yes):
    if assume_yes:
        print("%s [auto-confirmed: --yes]" % prompt)
        return True
    try:
        ans = input("%s [y/N] " % prompt)
    except EOFError:
        return False
    return ans.strip().lower() in ("y", "yes")


def pause_reseat(i, n):
    try:
        input("Take %d/%d: reseat the headphone/mic, then press Enter..."
              % (i, n))
    except EOFError:
        print("(stdin closed; continuing without the reseat pause)")


def quick_snr_check(x, sweep):
    """Fast per-take noise-floor check so a noisy room is caught before
    the next reseat. Onset = first sustained crossing of 10x the pre-roll
    RMS; threshold and wording match the core."""
    fs = sweep.fs
    head = x[:int(0.4 * fs)]
    noise = math.sqrt(float(np.mean(head ** 2))) if len(head) else 0.0
    thr = max(10.0 * noise, 1e-6)
    over = np.flatnonzero(np.abs(x) > thr)
    if not len(over):
        return None
    snr, _, _ = mc.estimate_snr(x, int(over[0]), sweep)
    if snr is not None and snr < mc.SNR_WARN_DB:
        print("WARNING: low SNR (%.1f dB): raise the level or kill the "
              "noise source" % snr, file=sys.stderr)
    return snr


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


# --- main ----------------------------------------------------------------

def measure(a):
    if a.takes < 1:
        raise RefusalError("--takes must be >= 1")
    if not 0 <= a.channel < a.channels:
        raise RefusalError("--channel %d out of range for --channels %d"
                           % (a.channel, a.channels))
    tools = ["pw-dump", "pw-metadata", "pw-play", "pw-record"]
    if a.auto_level:
        tools.append("wpctl")
    if a.mute_others:
        tools.append("pw-cli")
    require_tools(tools)

    dump = pw_dump()
    sink = resolve_node(dump, a.sink, "Audio/Sink")
    check_sink_identity(sink)
    source = resolve_node(dump, a.source, "Audio/Source")
    src_p = _props(source)
    if src_p.get("media.class") != "Audio/Source":
        raise RefusalError("capture target %r is %r, expected Audio/Source"
                           % (a.source, src_p.get("media.class")))
    if not (src_p.get("device.api") or "").startswith("alsa"):
        print("WARNING: mic source device.api is %r; measurement mics are "
              "expected on USB/ALSA" % src_p.get("device.api"),
              file=sys.stderr)

    v0, raw0, muted = sink_volume_state(dump, sink["id"])
    if muted:
        raise RefusalError("sink is muted; unmute it and set the working "
                           "listening level first")
    if v0 is None:
        print("WARNING: could not read the sink volume from pw-dump",
              file=sys.stderr)

    foreign = foreign_streams(dump, sink["id"])
    if foreign and not a.mute_others:
        raise RefusalError(
            "other streams are playing into this sink (a sweep on top of "
            "them is not a measurement):\n  %s\nstop them or re-run with "
            "--mute-others" % "\n  ".join(
                "id %(id)s  %(node_name)s  app=%(app)s" % s for s in foreign))

    sweep = mc.generate_sweep(a.samples, a.fs, a.f_start, a.f_end)
    wav_dur = a.pre_silence + sweep.duration_s + a.post_silence
    sink_id = node_ident(sink)

    print("Target sink : %s (%s, id %s, %s)"
          % (sink_id["description"] or sink_id["name"], sink_id["name"],
             sink_id["id"], sink_id["device_api"]))
    print("Mic source  : %s (id %s)"
          % (src_p.get("node.description") or src_p.get("node.name"),
             source["id"]))
    print("Sink volume : %s (will NOT be changed%s)"
          % ("%.0f%%" % (100 * v0) if v0 is not None else "unknown",
             "" if not a.auto_level else "; --auto-level may raise it"))
    print("Sweep       : %.2f s, %g-%g Hz @ %g dBFS digital, stream "
          "volume 1.0, %d take(s)"
          % (sweep.duration_s, sweep.f_start, sweep.f_end,
             sweep.level_dbfs, a.takes))
    if not confirm("The sweep WILL PLAY on this device at the volume "
                   "above. Proceed?", a.yes):
        print("declined", file=sys.stderr)
        return 3

    slug = re.sub(r"[^\w.+-]+", "_",
                  a.device or sink_id["name"] or "device").strip("_")
    outdir = os.path.join(a.save_dir or default_save_base(),
                          "%s_%s" % (slug,
                                     datetime.now().strftime("%Y%m%d-%H%M%S")))
    os.makedirs(outdir, exist_ok=True)
    wav = write_sweep_files(outdir, sweep, a.pre_silence, a.post_silence)
    print("Artifacts   : %s" % outdir)

    recordings, capture_peaks, path_clean = [], [], None
    auto = {"enabled": bool(a.auto_level), "adjustments": 0,
            "initial": None, "final": None, "in_window": None}
    v_cur = v0
    auto_ctl = AutoLevel()

    with MuteOthers(foreign, a.mute_others), \
         ProfileBypass(sink_id["name"]) as eq_state:
        if a.auto_level:
            if not confirm("--auto-level will adjust the sink volume "
                           "(start quiet at %.0f%%, up to %d raises, "
                           "target peak %g..%g dBFS). Proceed?"
                           % (100 * min(v0 or 1.0, AUTO_START_VOLUME),
                              AUTO_MAX_ADJUST, *AUTO_WINDOW), a.yes):
                print("declined", file=sys.stderr)
                return 3
            v_cur = min(v0 if v0 is not None else 1.0, AUTO_START_VOLUME)
            auto["initial"] = round(v_cur, 4)
            set_sink_volume(sink["id"], v_cur)

        leveled = not a.auto_level
        while len(recordings) < a.takes:
            if recordings:
                pause_reseat(len(recordings) + 1, a.takes)
            raw_path = (os.path.join(outdir, "raw%02d.wav"
                                     % (len(recordings) + 1))
                        if a.raw_capture_dump else None)
            data, info = run_take(sink, source, wav, wav_dur, a.channels,
                                  sweep.fs, verify=path_clean is None,
                                  raw_dump_path=raw_path)
            if info is not None:
                path_clean = info
            # diagnostic: scan ALL channels, not just the one we analyze,
            # so a glitch on the other channel isn't invisible
            for c in range(data.shape[1]):
                w = np.nonzero(~np.isfinite(data[:, c]))[0]
                if w.size:
                    print("note: %d non-finite sample(s) on channel %d at "
                          "%s of %d"
                          % (w.size, c, list(w[:6]), data.shape[0]),
                          file=sys.stderr)
            chan = data[:, a.channel]
            where = np.nonzero(~np.isfinite(chan))[0]
            bad = int(where.size)
            if bad:
                limit = max(1, int(REPAIR_MAX_MS / 1000.0 * sweep.fs))
                if bad > limit or bad >= len(chan):
                    raise MeasureError(
                        "channel %d capture has %d non-finite sample(s) "
                        "(NaN/Inf) -- too many to be a dropout; the input "
                        "is faulty, not merely quiet. Check that channel "
                        "%d of the mic is wired/seated, or point --channel "
                        "at the live one (a %d-channel capture exposes "
                        "0..%d)." % (a.channel, bad, a.channel,
                                     a.channels, a.channels - 1))
                chan = repair_nonfinite(chan)
                data = data.copy()
                data[:, a.channel] = chan     # keep the saved take finite
                print("WARNING: interpolated %d non-finite capture "
                      "sample(s) on channel %d at %s of %d -- a benign "
                      "single-sample glitch during the sweep; the take is "
                      "unaffected."
                      % (bad, a.channel, list(where[:6]), len(chan)),
                      file=sys.stderr)
            pk = peak_dbfs(chan)
            print("take %d/%d: capture peak %.1f dBFS"
                  % (len(recordings) + 1, a.takes, pk))
            clipped = int(np.count_nonzero(np.abs(chan) >= FULLSCALE))
            if clipped:
                print("WARNING: %d sample(s) at full scale -- the sweep "
                      "is clipped and this take is unusable; lower the "
                      "sink volume (or use --auto-level) and remeasure."
                      % clipped, file=sys.stderr)
            elif pk >= HOT_DBFS:
                print("WARNING: capture peak %.1f dBFS leaves little "
                      "headroom (risk of inter-sample clipping); consider "
                      "a lower level or --auto-level (targets %g..%g "
                      "dBFS)." % (pk, *AUTO_WINDOW), file=sys.stderr)

            if not leveled:
                auto_ctl.observe(v_cur, pk, bool(clipped))
                v_new = auto_ctl.next_volume(v_cur, pk)
                stuck = abs(v_new - v_cur) < 1e-3   # can't move any further
                if not clipped and auto_ctl.in_window(pk):
                    leveled, auto["in_window"] = True, True
                elif auto["adjustments"] >= AUTO_MAX_ADJUST or stuck:
                    auto["in_window"] = False
                    why = ("the level cannot be moved further (at %.0f%%)"
                           % (100 * v_cur) if stuck
                           else "%d adjustments" % AUTO_MAX_ADJUST)
                    print("WARNING: auto-level gave up after %s (peak %.1f "
                          "dBFS outside %g..%g)"
                          % (why, pk, *AUTO_WINDOW), file=sys.stderr)
                    if not confirm("Continue at the current level anyway?",
                                   a.yes):
                        raise MeasureError("aborted: capture level never "
                                           "reached the target window")
                    leveled = True
                else:
                    auto["adjustments"] += 1
                    print("auto-level: sink volume %.0f%% -> %.0f%% "
                          "(step %d/%d), retrying the take"
                          % (100 * v_cur, 100 * v_new,
                             auto["adjustments"], AUTO_MAX_ADJUST))
                    set_sink_volume(sink["id"], v_new)
                    v_cur = v_new
                    continue        # discard the leveling probe

            quick_snr_check(chan, sweep)
            save_take_wav(outdir, len(recordings) + 1, data, sweep.fs)
            recordings.append(chan)
            capture_peaks.append(round(pk, 2))

    dump = pw_dump()
    v_final, raw_final, _ = sink_volume_state(dump, sink["id"])
    v_report = v_final if v_final is not None else v_cur
    auto["final"] = round(v_report, 4) if a.auto_level else None
    levels = {
        "sink_volume": round(v_report, 4) if v_report is not None else None,
        "sink_volume_start": round(v0, 4) if v0 is not None else None,
        "sink_channel_volumes": raw_final or raw0,
        "stream_volume": (path_clean or {}).get("playback_stream",
                                                {}).get("volume"),
        "capture_peak_dbfs": capture_peaks,
        "auto_level": auto,
    }

    result = mc.process_takes(
        recordings, sweep, cal=a.cal, smoothing_fraction=a.smoothing,
        device=a.device or sink_id["description"] or sink_id["name"],
        rig=a.rig, mic=a.mic, eq_profile_state=eq_state, levels=levels,
        path_clean=path_clean, foreign_streams=foreign)
    out = a.out or os.path.join(outdir, "result.json")
    mc.save_result(result, out)

    t = result["takes"]
    print("%s: %d take(s), delay jitter %.2f ms, SNR min %s dB"
          % (out, t["count"], t["delay_jitter_ms"],
             t["snr_min_db"] if t["snr_min_db"] is not None else "n/a"))
    for w in result["warnings"]:
        print("WARNING: %s" % w)
    if eq_state["restored"] is False:
        return 1
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="exit codes: 0 ok, 1 measurement error, 2 refusal, "
               "3 declined, 130 interrupted")
    p.add_argument("--sink", required=True,
                   help="target device: node id, node.name or unique "
                        "substring")
    p.add_argument("--source", required=True,
                   help="measurement mic: node id, node.name or unique "
                        "substring")
    p.add_argument("--takes", type=int, default=1,
                   help="seatings to average (reseat prompt in between)")
    p.add_argument("--channels", type=int, default=1,
                   help="capture channel count (EARS = 2)")
    p.add_argument("--channel", type=int, default=0,
                   help="captured channel to analyze")
    p.add_argument("--device", help="device label for the result/dir name")
    p.add_argument("--rig")
    p.add_argument("--mic")
    p.add_argument("--cal", help="mic calibration file (miniDSP format)")
    p.add_argument("--smoothing", type=int, default=6,
                   help="1/N octave (0 = off)")
    p.add_argument("--samples", type=int, default=mc.DEFAULT_N)
    p.add_argument("--fs", type=int, default=mc.DEFAULT_FS)
    p.add_argument("--f-start", type=float, default=mc.DEFAULT_F_START)
    p.add_argument("--f-end", type=float, default=mc.DEFAULT_F_END)
    p.add_argument("--pre-silence", type=float, default=1.0)
    p.add_argument("--post-silence", type=float, default=0.5)
    p.add_argument("--mute-others", action="store_true",
                   help="mute foreign streams on the sink instead of "
                        "refusing to start")
    p.add_argument("--auto-level", action="store_true",
                   help="adjust the sink volume until the capture peak is "
                        "in %g..%g dBFS (max %d raises, confirmed)"
                        % (*AUTO_WINDOW, AUTO_MAX_ADJUST))
    p.add_argument("--yes", action="store_true",
                   help="assume yes on confirmations (NOT on reseat "
                        "pauses)")
    p.add_argument("--out", help="result JSON path (default: "
                                 "<save dir>/result.json)")
    p.add_argument("--save-dir",
                   help="base dir for raw takes (default: "
                        "tests/fixtures-local of the checkout, else cwd)")
    p.add_argument("--raw-capture-dump", action="store_true",
                   help="also save the untouched capture as raw<NN>.wav "
                        "for glitch diagnostics")
    a = p.parse_args(argv)

    try:
        return measure(a)
    except RefusalError as e:
        print("refusing to start: %s" % e, file=sys.stderr)
        return 2
    except MeasureError as e:
        print("measurement failed: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted (profile restore ran in the context exit)",
              file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
