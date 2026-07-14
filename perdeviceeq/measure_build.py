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

    results, recs = {}, {}
    for ch_index, key in channels.items():
        results[key] = session.finalize(ch_index,
                                        cal=_cal_path(ch_index))
        recs[key] = (ch_index, session.takes_of(ch_index))
    prof = fit_peq.fit_profiles(results, name=name, bands=bands,
                                f_lo=f_lo, f_hi=f_hi,
                                max_boost=max_boost)

    session_id = _new_id()
    takes, col_cal = [], {}
    for key, (ch_index, records) in recs.items():
        path = _cal_path(ch_index)
        for rec in records:
            takes.append(take_dict(rec, session_id, key,
                                   session.freqs))
            col = rec.capture_channel
            if path and col is not None:      # first path wins per col
                col_cal.setdefault(str(col), path)

    src = source or {}
    ident = session.source_ident
    sink = session.sink_ident
    sink_block = {"node_name": sink.get("name"),
                  "description": sink.get("description"),
                  "api": sink.get("device_api")}
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
        "sessions": {session_id: {
            "created_utc": session.started_utc or _utc_now(),
            "sweep": {"n_samples": session.sweep.n_samples,
                      "fs": session.sweep.fs,
                      "f_start": session.sweep.f_start,
                      "f_end": session.sweep.f_end,
                      "level_dbfs": session.sweep.level_dbfs,
                      "duration_s": round(session.sweep.duration_s,
                                          6)},
            "sink": dict(sink_block),
            "eq_profile_state": (session.eq_state
                                 or {"profile": None, "bypass": None}),
            "path_clean": (session.path_clean
                           or {"verified": None, "unknown_nodes": []}),
        }},
        "takes": takes,
    }
    params = {"bands": int(bands), "f_lo": float(f_lo),
              "f_hi": float(f_hi), "max_boost": float(max_boost),
              "smoothing": session.cfg.smoothing, "mono": False}
    take_ids = [t["id"] for t in takes]
    prof["provenance"] = {"kind": "measured"}
    prof["device"] = {"label": (session.cfg.device
                                or sink.get("description")
                                or sink.get("name")),
                      "sink": dict(sink_block)}
    prof["fit"] = {"at": _utc_now(), "algo": FIT_ALGO,
                   "params": params, "target": {"kind": "flat"},
                   "takes": take_ids,
                   "inputs_sha256": fit_fingerprint(measurement,
                                                    take_ids, params),
                   "edited": False}
    prof["measurement"] = measurement
    pid = store.save_user(prof)
    store.set_binding(sink_node, pid)
    return pid
