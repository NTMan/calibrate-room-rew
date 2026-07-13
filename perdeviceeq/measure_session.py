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
  the capture is both hot enough (peak in AUTO_PEAK_FLOOR..CEIL) and
  clean enough (SNR at least SNR_WARN_DB + margin), after an explicit
  confirmation. It assumes nothing about the device's volume->gain law
  (a BT sink's is nothing like the software cube law): it brackets and
  bisects in log-volume between a too-quiet and a too-loud probe, capped
  per step and held below any level seen to clip -- so the first sound
  neither blasts nor overshoots into a clip. Peak and SNR rise together
  dB-for-dB with the volume (the acoustic floor stays put), so one
  hot-enough probe predicts the best SNR reachable below the safe peak
  ceiling; when that is under SNR_WARN_DB the leveling REFUSES with the
  numbers instead of parking at a level that only makes flagged takes
  (see AutoLevel). Without --auto-level the sink volume is never raised
  above its value at start (it is not written at all). Everything ends
  up in `levels`. The sink's applied volumes (channelVolumes and
  softVolumes) are read from its Props during every sweep and stored on
  the take: when the level was moved between takes of one channel (the
  manual override, or re-armed auto-level) and the move was applied in
  software, averaging and finalize align the takes onto the channel's
  quietest one by exactly the recorded gain ratio -- the known
  bookkeeping is removed, seating variation is kept. A hardware-volume
  device (softVolumes pinned at 1.0, e.g. BT absolute volume) records
  unity gains, the alignment is a no-op and mixed levels stay visible
  in the spread, which is the honest answer there.
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
from scipy.stats import chi2

from . import measure_core as mc
from .pipewire import sink_channels

METADATA_NAME = "per-device-eq"          # same object the app + WP hook use
PLAY_NODE = "pde-measure-sweep"
CAPTURE_NODE = "pde-measure-capture"
SINK_API_PREFIXES = ("alsa", "bluez")    # "real device" whitelist
AUTO_MAX_ADJUST = 8                      # ramp-up + a few bisection steps
AUTO_START_VOLUME = 0.15                 # cubic; "start quiet"
AUTO_RAMP = 2.0                          # geometric step up while hunting
AUTO_EXPLORE_CEIL = 0.8                  # don't slam full volume while probing
AUTO_CLIP_BACKOFF = 0.85                 # stay this far below a clipping level
FULLSCALE = 0.999                        # |sample| >= this = clipped
HOT_DBFS = -1.0                          # peak above this = low headroom
AUTO_PEAK_FLOOR = -12.0                  # quieter wastes capture robustness
AUTO_PEAK_CEIL = HOT_DBFS - 1.0          # aim strictly below the hot flag
AUTO_SNR_MARGIN_DB = 1.0                 # aim past clean, not onto its edge
AUTO_TRUST_FLOOR_PK = -20.0              # trust the room's floor read only
#                                          on a probe at least this hot
SPREAD_MAX_DB = 3.0                      # take-to-take spread above this
#                                          is untrustworthy (red on the
#                                          strip; the auto EQ ceiling)
DRIVER_MIN_OCT = 0.25                    # a spread-driver flag must win
#                                          back at least this much band
TRUST_CONFIDENCE = 0.68                  # the ceiling judges an upper
#                                          confidence bound on the spread,
#                                          not the point estimate: two or
#                                          three takes cannot certify calm
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


def sink_applied_volumes(dump, sink_id):
    """(channelVolumes, softVolumes) linear arrays from the sink's
    Props. channelVolumes is the user-facing volume cubed; softVolumes
    is the gain PipeWire actually multiplies into the samples -- equal
    on a software-volume sink, pinned at 1.0 when the device does the
    volume in hardware (a BT sink's absolute volume), where the applied
    gain is genuinely unknowable from the node."""
    for o in _nodes(dump):
        if o["id"] == sink_id:
            d = props_param(o)
            cv = [float(v) for v in d.get("channelVolumes") or []]
            sv = [float(v) for v in d.get("softVolumes") or []]
            return cv, sv
    return [], []


def gain_comp_factors(gains):
    """Per-take linear factors (each <= 1.0) that align takes captured
    at different software volumes onto the quietest one: a take
    recorded with gain g is scaled by min/g, removing exactly the known
    level move and nothing else. Downward only, so scaling the samples
    can never clip. Any unknown or unusable gain (None, <= 0,
    non-finite) returns None and disables compensation for the whole
    set -- aligning the known takes around an unknown one would shift
    real data by a guess."""
    vals = []
    for g in gains:
        try:
            g = float(g)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(g) or g <= 0.0:
            return None
        vals.append(g)
    if not vals:
        return None
    ref = min(vals)
    return [ref / g for g in vals]


def _clamp_vol(v):
    return max(0.02, min(1.0, v))


class AutoLevel:
    """Drive the sink volume until the capture is both hot enough
    (peak in AUTO_PEAK_FLOOR..AUTO_PEAK_CEIL) and CLEAN enough (SNR at
    least SNR_WARN_DB + AUTO_SNR_MARGIN_DB, so takes don't straddle
    the clean threshold), with no assumption about the device's
    volume->gain law (a BT sink's is nothing like the software cube
    law). It brackets and then bisects: ramp up cautiously (never
    straight to full volume, which would blast a coupler) until a
    probe is too loud, then halve the interval in log-volume between
    the loudest-too-quiet and quietest-too-loud probe. Bisection needs
    only that louder means louder. The peak/SNR trade is law-free too:
    both rise dB-for-dB with the volume while the acoustic noise floor
    stays put, so one hot-enough probe predicts the best SNR the rig
    can reach below the safe ceiling (snr_ceiling) -- when that is
    under SNR_WARN_DB, no volume produces a clean take and the caller
    should refuse honestly instead of hunting."""

    def __init__(self):
        self.lo = None            # (v, peak): highest too-quiet probe
        self.hi = None            # (v, peak): lowest too-loud / clipped
        self.ceil = AUTO_EXPLORE_CEIL   # soft: lifts if stuck too quiet

    @staticmethod
    def verdict(peak, snr, clipped=False):
        """'loud' past the safe peak ceiling, 'ok' when hot enough AND
        clean enough, else 'quiet'. Within the last dB below the
        ceiling plain-clean SNR is accepted -- the rig cannot do
        better there without a hot take."""
        if clipped or peak > AUTO_PEAK_CEIL:
            return "loud"
        if peak >= AUTO_PEAK_FLOOR and snr is not None:
            if snr >= mc.SNR_WARN_DB + AUTO_SNR_MARGIN_DB:
                return "ok"
            if snr >= mc.SNR_WARN_DB and peak >= AUTO_PEAK_CEIL - 1.0:
                return "ok"
        return "quiet"

    @staticmethod
    def snr_ceiling(peak, snr):
        """Best SNR reachable at the safe peak ceiling, predicted from
        one probe: peak and SNR rise together dB-for-dB with volume.
        None when SNR is unknown or the probe is too quiet to trust
        that its floor read is the room's (and not electronics under
        a nearly-silent capture)."""
        if snr is None or peak < AUTO_TRUST_FLOOR_PK:
            return None
        return snr + (AUTO_PEAK_CEIL - peak)

    def observe(self, v, peak, snr, clipped):
        verdict = self.verdict(peak, snr, clipped)
        if verdict == "loud":
            p = 0.0 if clipped else peak
            if self.hi is None or v < self.hi[0]:
                self.hi = (v, p)
        elif verdict == "quiet":
            if self.lo is None or v > self.lo[0]:
                self.lo = (v, peak)
            if v >= self.ceil - 1e-3:     # at the ceiling, still quiet
                self.ceil = 1.0           # -> the device needs more

    def next_volume(self, v, peak):
        if self.lo and self.hi:                  # bracketed: bisect
            nv = math.sqrt(self.lo[0] * self.hi[0])
        elif self.hi:                            # too loud, no floor yet
            nv = self.hi[0] * AUTO_CLIP_BACKOFF
        else:                                    # hunt up for the loud end
            nv = min(v * AUTO_RAMP, self.ceil)
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
             verify, raw_dump_path=None, cancel=None, channel_map=None):
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
        play_cmd = ["pw-play", "--volume", "1.0",
                    "-P", "{ node.name = %s, node.target = %d, "
                          "node.dont-reconnect = true, application.name = "
                          "\"per-device-eq measure\" }"
                          % (PLAY_NODE, sink["id"])]
        if channel_map:
            play_cmd += ["--channel-map", channel_map]
        play_cmd.append(wav_path)
        play = subprocess.Popen(
            play_cmd,
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
    chan_vol: object = None   # sink channelVolumes entry (linear) for
                              # the played channel at sweep time
    soft_vol: object = None   # softVolumes ditto -- the gain PipeWire
                              # actually multiplied into the samples;
                              # 1.0 when the device does the volume
    noise_dbfs: object = None  # core pre-sweep noise-floor estimate


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
        self.sink_layout = sink_channels(self.sink_ident["name"], dump)

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
                self._v_cur = v
            elif self.cfg.start_volume is not None:
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

    def set_level(self, cubic):
        """Manual override of the measurement level (the wizard's editable
        %); freezes auto-level so the chosen level sticks for the sweep."""
        self._v_cur = max(0.0, min(1.0, float(cubic)))
        self._leveled = True

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

    def _set_meas_volume(self, on):
        """Set the measurement level for one sweep (on) or restore the
        user's listening volume after (off) -- per sweep, like the mute and
        EQ, so opening the wizard or measuring never leaves the device
        parked at the measurement level."""
        if self._v_cur is None or self.volume_start is None:
            return
        if abs(self._v_cur - self.volume_start) < 1e-4:
            return                          # nothing to change; leave it be
        set_sink_volume(self.sink["id"],
                        self._v_cur if on else self.volume_start)

    def _applied_gains(self, channel):
        """The sink's applied volumes for the played channel, read from
        the node while the measurement level is engaged: (channelVolumes
        entry, softVolumes entry), linear, None when unreadable. Read
        AFTER the sweep so the server had the whole take to settle any
        just-written volume. Metadata must never break a sweep, hence
        the broad catch."""
        try:
            cv, sv = sink_applied_volumes(pw_dump(), self.sink["id"])
        except Exception:
            return None, None

        def pick(arr):
            if not arr:
                return None
            if 0 <= channel < len(arr):
                return arr[channel]
            return sum(arr) / len(arr)
        return pick(cv), pick(sv)

    def _channel_map(self, channel):
        """The target speaker's position for pw-play --channel-map, e.g.
        'FL'. A mono sweep tagged with that single position plays only that
        speaker (PipeWire routes FL->FL, no up-mix). None for a mono sink
        or an out-of-range index -- just play the plain mono sweep."""
        n = len(self.sink_layout)
        if n <= 1 or not 0 <= channel < n:
            return None
        return self.sink_layout[channel].split(".")[0]

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
        cmap = self._channel_map(channel)   # route the sweep to THIS speaker
        self._mute_foreign(True)            # silence others for THIS sweep
        try:
            eq = ProfileBypass(self.sink_ident["name"])
            self.eq_state = eq.__enter__()  # bypass the device EQ for it
            try:
                self._set_meas_volume(True)     # measurement level
                try:
                    data, info = run_take(self.sink, self.source, self.wav,
                                          self.wav_duration, cfg.channels,
                                          self.sweep.fs,
                                          verify=self.path_clean is None,
                                          raw_dump_path=raw_path,
                                          cancel=self._cancel,
                                          channel_map=cmap)
                    gains = self._applied_gains(channel)
                finally:
                    self._set_meas_volume(False)  # restore listening volume
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
                         "SNR >= %g dB at a peak below %g dBFS)."
                         % (pk, mc.SNR_WARN_DB, AUTO_PEAK_CEIL))

        if not self._leveled:
            auto = self._auto_state
            snr_q, noise_q = self._quick_snr(chan)
            self._auto_ctl.observe(self._v_cur, pk, snr_q, bool(clipped))
            v_new = self._auto_ctl.next_volume(self._v_cur, pk)
            stuck = abs(v_new - self._v_cur) < 1e-3
            ceiling = (None if clipped
                       else AutoLevel.snr_ceiling(pk, snr_q))
            hopeless = (ceiling is not None
                        and ceiling < mc.SNR_WARN_DB)
            ok = (not clipped
                  and self._auto_ctl.verdict(pk, snr_q) == "ok")
            if ok:
                self._leveled, auto["in_window"] = True, True
            elif hopeless or auto["adjustments"] >= AUTO_MAX_ADJUST \
                    or stuck:
                auto["in_window"] = False
                if hopeless:
                    why = ("the noise floor (%.0f dBFS) tops out near "
                           "SNR %.0f dB at any safe peak -- kill the "
                           "noise source or move the rig"
                           % (noise_q if noise_q is not None
                              else float("nan"), ceiling))
                elif stuck:
                    why = ("the level cannot be moved further (at %.0f%%)"
                           % (100 * self._v_cur))
                else:
                    why = "%d adjustments" % AUTO_MAX_ADJUST
                snr_txt = ("%.1f dB" % snr_q if snr_q is not None
                           else "n/a")
                notes.append("WARNING: auto-level gave up: %s (peak %.1f "
                             "dBFS, SNR %s; target SNR >= %g dB at a "
                             "peak below %g dBFS)"
                             % (why, pk, snr_txt, mc.SNR_WARN_DB,
                                AUTO_PEAK_CEIL))
                self._pending = (channel, data, chan, pk, clipped, bad,
                                 gains)
                return TakeOutcome(
                    "level_stuck", notes=notes,
                    level={"peak_dbfs": pk, "snr_db": snr_q,
                           "noise_dbfs": noise_q,
                           "achievable_snr": ceiling,
                           "volume": self._v_cur, "why": why})
            else:
                auto["adjustments"] += 1
                level = {"peak_dbfs": pk, "snr_db": snr_q,
                         "volume_from": self._v_cur,
                         "volume_to": v_new, "step": auto["adjustments"],
                         "max_steps": AUTO_MAX_ADJUST}
                self._v_cur = v_new             # next sweep sets the sink
                return TakeOutcome("level_probe", level=level, notes=notes)
        return self._accept(channel, data, chan, pk, clipped, bad, notes,
                            gains)

    def accept_level(self):
        """Keep the pending level_stuck capture as a take at the current
        level -- the caller's 'continue anyway' decision."""
        if self._pending is None:
            raise MeasureError("no leveling decision is pending")
        (channel, data, chan, pk, clipped, repaired,
         gains) = self._pending
        self._pending = None
        self._leveled = True
        return self._accept(channel, data, chan, pk, clipped, repaired,
                            [], gains)

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
        self._v_cur = v

    def _accept(self, channel, data, chan, pk, clipped, repaired, notes,
                gains=(None, None)):
        snr, _ = self._quick_snr(chan)
        if snr is not None and snr < mc.SNR_WARN_DB:
            notes.append("WARNING: low SNR (%.1f dB): raise the level or "
                         "kill the noise source" % snr)
        self._take_seq += 1
        path = save_take_wav(self.outdir, self._take_seq, data,
                             self.sweep.fs)
        t = mc.analyze_take(chan, self.sweep, self.freqs)
        rec = TakeRecord(self._take_seq, channel, self.freqs, t.mag_db,
                         t.delay_ms, t.snr_db, pk, clipped, repaired, path,
                         chan_vol=gains[0], soft_vol=gains[1],
                         noise_dbfs=t.noise_dbfs)
        self._takes.setdefault(channel, []).append((rec, chan))
        return TakeOutcome("take", take=rec,
                           spread_db=self.spread_db(channel), notes=notes)

    def _quick_snr(self, chan):
        """Fast per-take (snr, noise_dbfs) so a noisy room is caught
        before the next reseat and the auto-level can target SNR.
        Onset = first sustained crossing of 10x the pre-roll RMS;
        threshold and wording match the core. (None, None) when no
        onset is found."""
        fs = self.sweep.fs
        head = chan[:int(0.4 * fs)]
        noise = math.sqrt(float(np.mean(head ** 2))) if len(head) else 0.0
        thr = max(10.0 * noise, 1e-6)
        over = np.flatnonzero(np.abs(chan) > thr)
        if not len(over):
            return None, None
        snr, _, noise_db = mc.estimate_snr(chan, int(over[0]),
                                           self.sweep)
        return snr, noise_db

    # -- the accumulated fan --------------------------------------------------

    def takes_of(self, channel):
        """The channel's accepted TakeRecords, oldest first."""
        return [rec for rec, _ in self._takes.get(channel, [])]

    def _comp_factors(self, entries):
        """gain_comp_factors over the entries' recorded soft gains;
        None disables compensation (an unknown gain, or no takes)."""
        return gain_comp_factors([rec.soft_vol for rec, _ in entries])

    def comp_shift_db(self, channel):
        """Per-take dB shifts (all <= 0) aligning the channel's
        accepted takes onto its quietest recorded software gain, in
        takes_of() order; None when compensation is off (an unknown
        gain somewhere, or no takes)."""
        f = self._comp_factors(self._takes.get(channel, []))
        if f is None:
            return None
        return [20.0 * math.log10(k) for k in f]

    def average_and_spread(self, channel, exclude_id=None):
        """Mean curve + take-to-take spread of the channel's accepted
        takes, with recorded level moves compensated: each curve is
        shifted down by its take's known software-gain excess over the
        channel's quietest take, so a manual level change or a re-level
        between takes neither smears the mean nor widens the corridor.
        Unknown gains fall back to the raw curves. The exact alignment
        finalize applies to the samples, so the live fan shows what the
        result will average. (None, None) without takes; spread is None
        until there are two."""
        entries = [e for e in self._takes.get(channel, [])
                   if exclude_id is None or e[0].id != exclude_id]
        if not entries:
            return None, None
        mags = [rec.mag_db for rec, _ in entries]
        factors = self._comp_factors(entries)
        if factors is not None:
            mags = [m + 20.0 * math.log10(k)
                    for m, k in zip(mags, factors)]
        return mc.average_takes(mags)

    def spread_db(self, channel, exclude_id=None):
        """Per-frequency std (ddof=1) across the channel's accepted
        takes, level moves compensated; None until there are two. The
        live fan's width."""
        return self.average_and_spread(channel, exclude_id)[1]

    def _trust_mask(self, thresh, exclude=None):
        """(freqs, ok) under the confidence bound, or None without
        statistics. The judged quantity is not the point estimate but
        the upper TRUST_CONFIDENCE bound on each channel's spread,
        s*sqrt(df/chi2_a(df)): a sample std of three takes must not
        flip trust back up while an outlier seating sits in the
        sample. Trust is earned by accumulating agreeing takes --
        x2.42 at two, x1.61 at three, approaching 1 -- or restored by
        deleting the outlier, never by dilution. Shared by the
        ceiling (the auto EQ handle) and the spread driver."""
        combined = None
        for c, entries in self._takes.items():
            if exclude is not None:
                entries = [e for e in entries if e[0].id != exclude]
            if len(entries) < 2:
                continue
            sp = self.spread_db(c, exclude_id=exclude)
            if sp is None:
                continue
            df = len(entries) - 1
            k = math.sqrt(df / chi2.ppf(1.0 - TRUST_CONFIDENCE, df))
            sp = np.asarray(sp, float) * k
            combined = (sp if combined is None
                        else np.maximum(combined, sp))
        if combined is None:
            return None
        return np.asarray(self.freqs, float), combined <= thresh

    def trusted_ceiling_hz(self, thresh=SPREAD_MAX_DB):
        """Highest frequency the take-to-take statistics still trust:
        scanning DOWN from the top of the grid, the top of the first
        at-least-1/6-octave run where the bound (see _trust_mask)
        stays under `thresh`. A red island lower in the band does not
        pull the ceiling (it is visible on the strip and is the left
        handle's business); the HF cliff does, to its edge exactly.
        None while no channel has two takes. The bars on the strip
        keep showing the point estimate: they say what happened, the
        ceiling says what cannot be ruled out, so it may sit below
        the red. The statistics only mean what the takes vary over:
        reseat between takes, or the spread flatters the seating."""
        m = self._trust_mask(thresh)
        if m is None:
            return None
        f, ok = m
        min_ratio = 2.0 ** (1.0 / 6.0)
        i = len(f) - 1
        while i >= 0:
            if not ok[i]:
                i -= 1
                continue
            j = i
            while j >= 0 and ok[j]:
                j -= 1
            if j < 0 or f[i] / f[j + 1] >= min_ratio:
                return float(f[i])
            i = j
        return float(f[0])

    def trusted_floor_hz(self, thresh=SPREAD_MAX_DB):
        """Mirror of trusted_ceiling_hz for the bottom of the band:
        scanning UP from the bottom of the grid, the bottom of the
        first at-least-1/6-octave trusted run under the bound. A red
        island mid-band does not push the floor up; a bass cliff (a
        seal that seats differently every take) does, to its edge
        exactly. None while no channel has two takes."""
        m = self._trust_mask(thresh)
        if m is None:
            return None
        f, ok = m
        min_ratio = 2.0 ** (1.0 / 6.0)
        i, n = 0, len(f)
        while i < n:
            if not ok[i]:
                i += 1
                continue
            j = i
            while j < n and ok[j]:
                j += 1
            if j >= n or f[j - 1] / f[i] >= min_ratio:
                return float(f[i])
            i = j
        return float(f[-1])

    def _trusted_octaves(self, thresh, exclude=None):
        """Total trustworthy bandwidth in octaves under the bound,
        with an optional take excluded."""
        m = self._trust_mask(thresh, exclude)
        if m is None:
            return None
        f, ok = m
        step = math.log2(f[-1] / f[0]) / max(1, len(f) - 1)
        return float(ok.sum()) * step

    def spread_driver(self, thresh=SPREAD_MAX_DB):
        """The one accepted take whose removal wins back the most
        trustworthy BANDWIDTH, as (take_id, octaves_regained), or
        None. Judged over the whole band, not the ceiling: a
        seal-leak take poisons the bass while the ceiling stays
        pinned by an HF region red in every take, and a ceiling-only
        verdict stays silent about it (observed in the field within
        a day of shipping it). Leave-one-out over channels with at
        least three takes (removing one of two leaves no statistics
        at all), the reduced sample honestly paying the higher
        confidence factor. A real improvement is required
        (DRIVER_MIN_OCT): when the scatter is spread evenly over the
        takes, deleting any one of them fixes nothing and nothing is
        flagged -- a highlight that cannot deliver on its promise
        would be a lie."""
        base = self._trusted_octaves(thresh)
        if base is None:
            return None
        best = None
        for c, entries in self._takes.items():
            if len(entries) < 3:
                continue
            for rec, _ in entries:
                oc = self._trusted_octaves(thresh, exclude=rec.id)
                if oc is None:
                    continue
                gain = oc - base
                if gain < DRIVER_MIN_OCT:
                    continue
                if best is None or gain > best[1]:
                    best = (rec.id, gain)
        return best

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
        regardless, so a different cal can still be applied later.

        Takes captured at different software volumes are aligned onto
        the channel's quietest one before averaging (recordings scaled
        by the recorded gain ratio, downward only); the per-take gains
        and the applied shifts land in `levels` so a stored result can
        be re-fit later with full knowledge of how it was driven."""
        entries = self._takes.get(channel, [])
        if not entries:
            raise MeasureError("no accepted takes on channel %d" % channel)
        factors = self._comp_factors(entries)
        recordings = [samples for _, samples in entries]
        comp_db = None
        if factors is not None:
            comp_db = [round(20.0 * math.log10(k), 3) for k in factors]
            if any(abs(k - 1.0) > 1e-9 for k in factors):
                # align the takes onto the channel's quietest recorded
                # gain: exact, downward only, so it can never clip
                recordings = [s * k
                              for s, k in zip(recordings, factors)]
        dump = pw_dump()
        v_final, raw_final, _ = sink_volume_state(dump, self.sink["id"])
        # per-channel truth: the (compensated) result sits at the level
        # of THIS channel's quietest take; the session-wide scalar (the
        # last sweep's level, possibly another channel's) is only the
        # fallback when the applied gains could not be read
        v_report = None
        if factors is not None:
            k = min(range(len(entries)),
                    key=lambda i: entries[i][0].soft_vol)
            cv = entries[k][0].chan_vol
            if cv is not None and cv > 0:
                v_report = cv ** (1.0 / 3.0)
        if v_report is None:
            v_report = self._v_cur if self._v_cur is not None else v_final
        auto = dict(self._auto_state)
        # gate on the live auto state, not cfg: relevel() re-arms the
        # leveling on a session constructed with a remembered volume,
        # and its final level is just as real
        auto["final"] = (round(v_report, 4) if auto.get("enabled")
                         else None)

        def _r6(v):
            return None if v is None else round(v, 6)
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
            "take_channel_volumes": [_r6(r.chan_vol)
                                     for r, _ in entries],
            "take_soft_volumes": [_r6(r.soft_vol) for r, _ in entries],
            "take_noise_dbfs": [round(r.noise_dbfs, 1)
                                if r.noise_dbfs is not None else None
                                for r, _ in entries],
            "gain_comp_db": comp_db,
            "auto_level": auto,
        }
        result = mc.process_takes(
            recordings, self.sweep,
            cal=(cal if cal is not None else self.cfg.cal),
            smoothing_fraction=self.cfg.smoothing,
            device=(self.cfg.device or self.sink_ident["description"]
                    or self.sink_ident["name"]),
            rig=self.cfg.rig, mic=self.cfg.mic,
            sink_api=self.sink_ident.get("device_api"),
            eq_profile_state=self.eq_state, levels=levels,
            path_clean=self.path_clean, foreign_streams=self.foreign)
        out = out_path or os.path.join(self.outdir, "result.json")
        mc.save_result(result, out)
        return result
