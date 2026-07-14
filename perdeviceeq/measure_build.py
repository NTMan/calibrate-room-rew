# -*- coding: utf-8 -*-
"""The wizard's 'Create profile' step, kept GTK-free so it can be tested.

Ties a finished measure_session (the accepted takes) to fit_peq (the
fit) and a ProfileStore (save + bind): finalize each measured channel
with its own cal, fit the bands, attach the v3 blocks -- `provenance`,
`device`, `fit` and `measurement` (the canvas: the rig with its cal
points embedded, the session context, and every take's uncalibrated
magnitude on the profile's log grid) -- save it all as one user
profile, bind it to the sink and return the new id. Nothing lands next
to the takes: the profile file IS the artifact (result JSONs remain a
CLI affair). The window calls build_and_bind on a worker thread and
then switches the main editor to that id so the result is heard at
once. No GTK and no store construction here (the store is injected).
"""
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone

import numpy as np

from . import fit_peq
from . import measure_core as mc
from .profiles import playback_sha256

FIT_ALGO = "fit_peq"


def _new_id():
    return uuid.uuid4().hex[:12]


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _num(v, nd):
    return None if v is None else round(float(v), nd)


def cal_entry(path):
    """One source.cal entry: the file's identity (basename + sha256)
    and its parsed `freq dB` points, embedded so the profile can be
    viewed and re-fit on a machine that never saw the file."""
    with open(path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    fr, db = mc.load_mic_cal(path)
    return {"file": os.path.basename(path), "sha256": digest,
            "points": [[_num(a, 4), _num(b, 4)]
                       for a, b in zip(fr, db)]}


def take_dict(rec, session_id, key, freqs):
    """One canvas take: the record's UNCALIBRATED magnitude resampled
    onto the profile grid (log-f interp; a no-op on the session's own
    grid) plus the vitals needed to re-align, re-judge and re-fit it
    later. Rounding: 0.01 dB keeps a take at ~7 KB of JSON."""
    mag = np.asarray(rec.mag_db, float)
    if (len(rec.freq_hz) != len(freqs)
            or not np.allclose(rec.freq_hz, freqs)):
        mag = np.interp(np.log(freqs), np.log(rec.freq_hz), mag)
    return {"id": _new_id(), "session": session_id, "channel": key,
            "capture_channel": rec.capture_channel,
            "created_utc": rec.created_utc,
            "mag_db_uncal": [_num(v, 2) for v in mag],
            "delay_ms": _num(rec.delay_ms, 2),
            "snr_db": _num(rec.snr_db, 1),
            "peak_dbfs": _num(rec.peak_dbfs, 2),
            "noise_dbfs": _num(rec.noise_dbfs, 1),
            "clipped": int(rec.clipped),
            "repaired": int(rec.repaired),
            "chan_vol": _num(rec.chan_vol, 6),
            "soft_vol": _num(rec.soft_vol, 6)}


def fit_fingerprint(measurement, take_ids, params):
    """sha256 over everything the fit consumed: the selected takes'
    magnitudes, the rig cal and the fit parameters. A stored fit whose
    fingerprint no longer matches its canvas is stale (takes removed,
    cal swapped, parameters changed) and the UI offers a re-fit."""
    wanted = set(take_ids)
    takes = {t["id"]: t["mag_db_uncal"]
             for t in measurement.get("takes", [])
             if t["id"] in wanted}
    cal = {c: (e or {}).get("sha256")
           for c, e in ((measurement.get("source") or {})
                        .get("cal") or {}).items()}
    blob = json.dumps({"grid": measurement.get("grid"), "cal": cal,
                       "takes": takes, "params": params},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class SourceMismatch(RuntimeError):
    """The session's rig is not the profile's rig."""


def rig_matches(stored, serial, node_match):
    """The append gate: serials decide when both sides have one (two
    different units on one node name are two rigs); otherwise the
    node identity does. `stored` is measurement.source."""
    s_old = (stored or {}).get("serial") or ""
    s_new = serial or ""
    if s_old and s_new:
        return s_old == s_new
    return (((stored or {}).get("node_match") or "")
            == (node_match or ""))


def _session_block(session):
    """The canvas entry for one live session."""
    sink = session.sink_ident
    return {
        "created_utc": session.started_utc or _utc_now(),
        "sweep": {"n_samples": session.sweep.n_samples,
                  "fs": session.sweep.fs,
                  "f_start": session.sweep.f_start,
                  "f_end": session.sweep.f_end,
                  "level_dbfs": session.sweep.level_dbfs,
                  "duration_s": round(session.sweep.duration_s, 6)},
        "sink": {"node_name": sink.get("name"),
                 "description": sink.get("description"),
                 "api": sink.get("device_api")},
        "eq_profile_state": (session.eq_state
                             or {"profile": None, "bypass": None}),
        "path_clean": (session.path_clean
                       or {"verified": None, "unknown_nodes": []}),
    }


def _collect_takes(session, channels, cal_of, session_id, freqs):
    """(takes, col_cal) for the session's accepted takes under the
    caller's channel map, serialized onto `freqs` (the canvas grid);
    cal paths gathered per capture column, first path wins."""
    takes, col_cal = [], {}
    for ch_index, key in channels.items():
        path = cal_of(ch_index)
        for rec in session.takes_of(ch_index):
            takes.append(take_dict(rec, session_id, key, freqs))
            col = rec.capture_channel
            if path and col is not None:
                col_cal.setdefault(str(col), path)
    return takes, col_cal


def build_and_bind(session, channels, store, sink_node, name,
                   cal=None, bands=10, f_lo=20.0, f_hi=12000.0,
                   max_boost=6.0, source=None):
    """Finalize the measured channels, fit a profile, attach the v3
    blocks and save + bind it.

    channels maps a capture-channel index to a profile channel key,
    e.g. {0: "FL", 1: "FR"}. cal maps a capture-channel index (int or
    str) to a cal-file path (from the mic profile); a channel absent
    from cal falls back to the session's cfg.cal. `source` optionally
    carries what the session cannot know about the rig (its display
    name, its serial); node identity and capsule count come from the
    session itself. Returns the new user-profile id, already bound to
    sink_node."""
    cal = cal or {}

    def _cal_path(ch):
        c = cal.get(ch)
        if c is None:
            c = cal.get(str(ch))
        return c if c is not None else session.cfg.cal

    results = {}
    for ch_index, key in channels.items():
        results[key] = session.finalize(ch_index,
                                        cal=_cal_path(ch_index))
    prof = fit_peq.fit_profiles(results, name=name, bands=bands,
                                f_lo=f_lo, f_hi=f_hi,
                                max_boost=max_boost)

    session_id = _new_id()
    takes, col_cal = _collect_takes(session, channels, _cal_path,
                                    session_id, session.freqs)
    src = source or {}
    ident = session.source_ident
    block = _session_block(session)
    sink_block = dict(block["sink"])
    measurement = {
        "grid": {"f_lo": mc.GRID_F_LO, "f_hi": mc.GRID_F_HI,
                 "ppo": mc.GRID_PPO},
        "source": {
            "name": (src.get("name") or session.cfg.mic
                     or ident.get("description") or ident.get("name")),
            "serial": src.get("serial") or session.cfg.rig or "",
            "node_match": ident.get("name"),
            "channels": session.cfg.channels,
            "cal": {c: cal_entry(p)
                    for c, p in sorted(col_cal.items())},
        },
        "sessions": {session_id: block},
        "takes": takes,
    }
    params = {"bands": int(bands), "f_lo": float(f_lo),
              "f_hi": float(f_hi), "max_boost": float(max_boost),
              "smoothing": session.cfg.smoothing, "mono": False}
    take_ids = [t["id"] for t in takes]
    prof["provenance"] = {"kind": "measured"}
    prof["device"] = {"label": (session.cfg.device
                                or sink_block.get("description")
                                or sink_block.get("node_name")),
                      "sink": dict(sink_block)}
    prof["fit"] = {"at": _utc_now(), "algo": FIT_ALGO,
                   "params": params, "target": {"kind": "flat"},
                   "takes": take_ids,
                   "inputs_sha256": fit_fingerprint(measurement,
                                                    take_ids, params),
                   "edited": False}
    prof["measurement"] = measurement
    prof["fit"]["output_sha256"] = playback_sha256(prof)
    pid = store.save_user(prof)
    store.set_binding(sink_node, pid)
    return pid


def append_and_bind(session, channels, store, sink_node, pid,
                    cal=None, source=None, name=None, replace=(),
                    bands=None, f_lo=None, f_hi=None,
                    max_boost=None, allow_edited=False):
    """The Edit twin of build_and_bind: fold a session into an
    EXISTING profile's canvas and re-fit from the whole of it.

    With no measured channels this is a rename: only `name` lands
    and nothing else moves. Otherwise the session's takes append to
    measurement.takes -- channels listed in `replace` first drop
    their stored takes (the re-measure) -- the session lands in
    measurement.sessions, and the cal files selected now REPLACE the
    stored entries for their capture columns: the mic profile's cal
    is the current truth, and a changed sha is exactly what makes an
    old fit stale. A profile without a canvas grows one, keeping its
    id and name; provenance becomes {"kind": "measured"} either way.
    The rig gate guards the append (rig_matches against
    measurement.source, or SourceMismatch). The fit is then rebuilt
    by refit.refit_profile over the merged canvas (fit-parameter
    overrides pass through; a hand-edited fit needs
    allow_edited=True), and the profile is saved under the same id
    and bound to sink_node."""
    prof = store.get(pid)
    if prof is None:
        raise KeyError("no profile %s" % pid)
    prof = dict(prof)
    if name:
        prof["name"] = str(name)
    if not channels:
        pid = store.save_user(prof)
        store.set_binding(sink_node, pid)
        return pid

    cal = cal or {}

    def _cal_path(ch):
        c = cal.get(ch)
        if c is None:
            c = cal.get(str(ch))
        return c if c is not None else session.cfg.cal

    src = source or {}
    ident = session.source_ident
    new_serial = src.get("serial") or session.cfg.rig or ""
    m = prof.get("measurement")
    if m and m.get("source"):
        stored = m["source"]
        if not rig_matches(stored, new_serial, ident.get("name")):
            raise SourceMismatch(
                "this profile was measured with %s (serial %r, "
                "node %r); measuring with a different rig needs a "
                "new profile"
                % (stored.get("name") or "another rig",
                   stored.get("serial") or "",
                   stored.get("node_match") or ""))

    g = (m.get("grid") if m else None) or {}
    freqs = mc.log_grid(float(g.get("f_lo", mc.GRID_F_LO)),
                        float(g.get("f_hi", mc.GRID_F_HI)),
                        int(g.get("ppo", mc.GRID_PPO)))
    session_id = _new_id()
    new_takes, col_cal = _collect_takes(session, channels,
                                        _cal_path, session_id,
                                        freqs)
    block = _session_block(session)
    if not m:
        m = {"grid": {"f_lo": mc.GRID_F_LO, "f_hi": mc.GRID_F_HI,
                      "ppo": mc.GRID_PPO},
             "source": {
                 "name": (src.get("name") or session.cfg.mic
                          or ident.get("description")
                          or ident.get("name")),
                 "serial": new_serial,
                 "node_match": ident.get("name"),
                 "channels": session.cfg.channels,
                 "cal": {}},
             "sessions": {}, "takes": []}
        prof.setdefault(
            "device",
            {"label": (session.cfg.device
                       or block["sink"].get("description")
                       or block["sink"].get("node_name")),
             "sink": dict(block["sink"])})
    else:
        m = dict(m)
        m["source"] = dict(m.get("source") or {})
        m["sessions"] = dict(m.get("sessions") or {})
        m["takes"] = list(m.get("takes") or [])
        if not m["source"].get("serial") and new_serial:
            m["source"]["serial"] = new_serial
    drop = set(replace or ())
    if drop:                          # the re-measure: old takes go
        m["takes"] = [t for t in m["takes"]
                      if t.get("channel") not in drop]
    m["takes"].extend(new_takes)
    m["sessions"][session_id] = block
    calmap = dict(m["source"].get("cal") or {})
    for colkey, path in sorted(col_cal.items()):
        calmap[colkey] = cal_entry(path)  # the current truth wins
    m["source"]["cal"] = calmap
    prof["measurement"] = m
    prof["provenance"] = {"kind": "measured"}

    from . import refit               # circular at module level
    prof = refit.refit_profile(prof, bands=bands, f_lo=f_lo,
                               f_hi=f_hi, max_boost=max_boost,
                               allow_edited=allow_edited)
    pid = store.save_user(prof)
    store.set_binding(sink_node, pid)
    return pid
