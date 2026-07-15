# -*- coding: utf-8 -*-
"""Persistent measurement preferences for the wizard (no GTK; JSON only).

Two small stores let the measurement mic and its calibration be chosen
once and recalled per output:

  MicProfileStore  reusable measurement-mic profiles -- which PipeWire
                   source node to match, the rig serial, the per-capture-
                   channel calibration files, and the compensation domain
                   (RAW / HEQ / IDF / HPN, which shapes the fit target).
  MeasureMemory    per-sink recall -- the last mic profile used for a sink
                   and the last auto-level volume that measured it well,
                   so re-measuring the same output needs no re-setup.

Both save atomically (tmp + os.replace) and tolerate a missing or
corrupt file by starting empty, mirroring profiles.py. Filesystem + JSON
only, so this imports cleanly anywhere (CLI, tests, GUI) without GTK.
"""
import json
import os
import re
import uuid

from .config import MIC_PROFILES_FILE, MEASURE_STATE_FILE


def _new_id():
    return uuid.uuid4().hex[:12]



def _atomic_write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)



def serial_from_cal(paths):
    """The rig serial, read from the cal filenames. miniDSP ships
    per-unit cal as {L,R}_{RAW,HEQ,IDF,HPN}_<serial>.txt and UMIKs
    as <model>_<serial>.txt, so the unit's identity is usually
    sitting right in the file name. Every provided file must agree
    on exactly one candidate (a digit run of 5+), otherwise nothing
    is guessed -- a wrong serial is worse than an empty one."""
    sets = []
    for p in paths or ():
        if not p:
            continue
        runs = set(re.findall(r"[0-9]{5,}", os.path.basename(p)))
        if runs:
            sets.append(runs)
    if not sets:
        return ""
    common = set.intersection(*sets)
    return common.pop() if len(common) == 1 else ""


class MicProfileStore:
    """Reusable measurement-mic profiles, keyed by a stable id. A profile
    is a plain dict: {id, name, node_match, serial, cal, channels}, where
    cal maps a capture-channel index (as a string) to a cal-file path and
    channels is the rig's capsule count (1 or 2, or None if unset -- some
    mono mics enumerate as stereo). Stored as one JSON file, {id: body}."""

    def __init__(self):
        self.profiles = {}
        self.reload()

    def reload(self):
        self.profiles = {}
        try:
            with open(MIC_PROFILES_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict):
            for pid, body in data.items():
                if isinstance(body, dict):
                    self.profiles[pid] = self._sane(pid, body)

    @staticmethod
    def _sane(pid, body):
        cal = body.get("cal") or {}
        cal = {str(k): str(v) for k, v in cal.items() if v}
        ch = body.get("channels")
        ch = ch if ch in (1, 2) else None
        return {"id": pid, "name": body.get("name") or pid,
                "node_match": body.get("node_match") or "",
                "serial": body.get("serial") or "", "cal": cal,
                "channels": ch}

    @staticmethod
    def _body(p):
        return {k: p[k] for k in ("name", "node_match", "serial",
                                  "cal", "channels")}

    def get(self, pid):
        return self.profiles.get(pid)

    def ordered(self):
        return sorted(self.profiles.values(),
                      key=lambda p: p["name"].lower())

    def save(self, body):
        """Create or overwrite a profile; returns its id (minted if absent
        or if the given id collides with none)."""
        pid = body.get("id") or _new_id()
        self.profiles[pid] = self._sane(pid, body)
        self._flush()
        return pid

    def delete(self, pid):
        if pid in self.profiles:
            del self.profiles[pid]
            self._flush()
            return True
        return False

    def _flush(self):
        _atomic_write(MIC_PROFILES_FILE,
                      {pid: self._body(p)
                       for pid, p in self.profiles.items()})

    def match(self, node_name):
        """The profile whose node_match equals this live source node.name,
        or None. Exact match for v1 (a USB source's node.name is stable
        per device); the user re-picks if a port change renames it."""
        for p in self.profiles.values():
            if p["node_match"] and p["node_match"] == node_name:
                return p
        return None

    def cal_for(self, pid, channel):
        """The cal-file path a profile assigns to a capture-channel index,
        or None. This is what the window hands finalize(channel, cal=...)."""
        p = self.profiles.get(pid)
        return p["cal"].get(str(channel)) if p else None


class MeasureMemory:
    """Per-sink recall: {sink_node: {"mic_profile": id, "volume": float}}.
    On reopening the window for a sink, its last mic and a starting
    auto-level volume are restored so almost nothing is re-entered."""

    def __init__(self):
        self.state = {}
        self.reload()

    def reload(self):
        try:
            with open(MEASURE_STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        self.state = data if isinstance(data, dict) else {}

    def for_sink(self, sink):
        e = self.state.get(sink)
        return e if isinstance(e, dict) else {}

    def mic_for(self, sink):
        return self.for_sink(sink).get("mic_profile")

    def volume_for(self, sink, source):
        """The last good auto-level volume for this sink+source pair, or
        None. Volume is keyed by the pair because a more/less sensitive
        mic reads hotter/quieter at the same sink level, so the level that
        worked depends on which mic measured it."""
        vols = self.for_sink(sink).get("volumes")
        v = vols.get(source) if isinstance(vols, dict) else None
        return float(v) if isinstance(v, (int, float)) else None

    def remember(self, sink, mic_profile=None, source=None, volume=None):
        """Update a sink's recall; only the provided fields change. The
        mic profile is per-sink (which mic to preselect); the volume is
        stored under sink+source (needs source to key it)."""
        if not sink:
            return
        e = dict(self.for_sink(sink))
        if mic_profile is not None:
            e["mic_profile"] = mic_profile
        if volume is not None and source:
            vols = dict(e.get("volumes") or {})
            vols[source] = round(float(volume), 4)
            e["volumes"] = vols
        self.state[sink] = e
        _atomic_write(MEASURE_STATE_FILE, self.state)

    def forget_volume(self, sink, source):
        """Drop the remembered volume for a sink+source pair (the wizard's
        re-level: the next sweep finds the level afresh)."""
        e = self.for_sink(sink)
        vols = e.get("volumes")
        if isinstance(vols, dict) and source in vols:
            del vols[source]
            self.state[sink] = e
            _atomic_write(MEASURE_STATE_FILE, self.state)

    def forget(self, sink):
        if sink in self.state:
            del self.state[sink]
            _atomic_write(MEASURE_STATE_FILE, self.state)
