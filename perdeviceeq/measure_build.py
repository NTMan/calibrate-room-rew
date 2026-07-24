# -*- coding: utf-8 -*-
"""The canvas write side, kept GTK-free so it can be tested.

Three primitives carry the interruptible workflow: commit_take()
persists one accepted take into the profile the moment it exists
(creating the canvas, rig-gated, on first use), remove_takes()
physically deletes takes and prunes emptied sessions, and
refit_and_save() settles the profile from the whole stored canvas
when the caller decides the house is full. Plus the serialization
helpers they stand on: take_dict() puts an uncalibrated magnitude
onto the canvas grid, cal_entry() embeds a cal file as points + sha,
fit_fingerprint() hashes everything a fit consumed. Schema v4:
every take carries its own passport -- cal_sha into the append-only
measurement.cal_library, the rig stamp on its session -- and the
statistics judge a mixed canvas (per-take-calibrated curves feed the
spread and spread_trust_bound), not a name gate. Runtime reads ONE
shape; tools/migrate_profiles_v3_to_v4.py converts older files once.
No GTK and no store construction here; the store is injected.
"""
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone

import numpy as np

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


def _lib_add(m, path):
    """Parse the cal file at `path` into the canvas cal library
    (append-only, keyed by sha256) and return its sha."""
    e = cal_entry(path)
    lib = dict(m.get("cal_library") or {})
    lib.setdefault(e["sha256"], {"file": e["file"],
                                 "points": e["points"]})
    m["cal_library"] = lib
    return e["sha256"]


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
    cal = {t["id"]: t.get("cal_sha")
           for t in measurement.get("takes", [])
           if t["id"] in wanted}
    blob = json.dumps({"grid": measurement.get("grid"), "cal": cal,
                       "takes": takes, "params": params},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()




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


def commit_take(store, pid, session, ch_index, key, take_id,
                cal=None, source=None, canvas_session=None):
    """Persist ONE accepted take into profile `pid`'s canvas the
    moment it exists: the core of the interruptible workflow. The
    canvas and the session entry are created on first use; returns
    {"take": <canvas take id>, "session": <canvas session id>} so
    the caller threads the session id through subsequent commits and
    can map its live take onto the canvas for deletion. `cal` is the
    cal-file path for this take's capture column: it joins the
    append-only cal library and the take carries its sha --
    provenance is immutable, no entry is ever replaced. The rig
    stamp lands on the take's session. The fit is not touched:
    it goes incomplete or stale honestly, and refit_and_save settles
    it when the caller decides the house is full."""
    prof = store.get(pid)
    if prof is None:
        raise KeyError("no profile %s" % pid)
    rec = next((r for r in session.takes_of(ch_index)
                if r.id == take_id), None)
    if rec is None:
        raise KeyError("no take %r on channel %r"
                       % (take_id, ch_index))
    prof = dict(prof)
    src = source or {}
    ident = session.source_ident
    new_serial = src.get("serial") or session.cfg.rig or ""
    rig = {"name": (src.get("name") or session.cfg.mic
                    or ident.get("description")
                    or ident.get("name")),
           "serial": new_serial,
           "node_match": ident.get("name"),
           "channels": session.cfg.channels}
    m = prof.get("measurement")
    g = (m.get("grid") if m else None) or {}
    freqs = mc.log_grid(float(g.get("f_lo", mc.GRID_F_LO)),
                        float(g.get("f_hi", mc.GRID_F_HI)),
                        int(g.get("ppo", mc.GRID_PPO)))
    if not m:
        block = _session_block(session)
        m = {"grid": {"f_lo": mc.GRID_F_LO, "f_hi": mc.GRID_F_HI,
                      "ppo": mc.GRID_PPO},
             "cal_library": {}, "sessions": {}, "takes": []}
        prof.setdefault(
            "device",
            {"label": (session.cfg.device
                       or block["sink"].get("description")
                       or block["sink"].get("node_name")),
             "sink": dict(block["sink"])})
    else:
        m = dict(m)
        m["sessions"] = dict(m.get("sessions") or {})
        m["takes"] = list(m.get("takes") or [])
    sid = canvas_session or _new_id()
    if sid not in m["sessions"]:
        # the sitting wears the rig stamp: a mic change always
        # crosses a fresh session, so sitting and rig are 1:1
        m["sessions"][sid] = dict(_session_block(session),
                                  source=dict(rig))
    take = take_dict(rec, sid, key, freqs)
    take["cal_sha"] = None
    m["takes"].append(take)
    path = cal if cal is not None else session.cfg.cal
    col = rec.capture_channel
    if path and col is not None:
        take["cal_sha"] = _lib_add(m, path)
    prof["measurement"] = m
    prof["provenance"] = {"kind": "measured"}
    store.save_user(prof)
    return {"take": take["id"], "session": sid}


def cal_groups(measurement):
    """The canvas grouped by cal origin -- the shape the Manage
    calibrations dialog renders and reassign_cal operates on.
    One group per distinct cal_sha (None = raw takes), ordered
    by first appearance on the canvas: {"sha", "file" (None for
    raw), "count", "rigs": [distinct session rig names, in
    order], "rig_counts": {rig name: takes recorded on it}} --
    the sitting's word: the inventory answers not just which
    rigs a cal served but HOW MANY TIMES on each. Pure and
    GTK-free."""
    m = measurement or {}
    lib = m.get("cal_library") or {}
    sessions = m.get("sessions") or {}
    order = []
    groups = {}
    for t in (m.get("takes") or []):
        sha = t.get("cal_sha")
        g = groups.get(sha)
        if g is None:
            g = {"sha": sha,
                 "file": ((lib.get(sha) or {}).get("file")
                          if sha else None),
                 "count": 0, "rigs": [], "rig_counts": {}}
            groups[sha] = g
            order.append(sha)
        g["count"] += 1
        rig = ((sessions.get(t.get("session")) or {})
               .get("source") or {}).get("name")
        if rig:
            if rig not in g["rigs"]:
                g["rigs"].append(rig)
            g["rig_counts"][rig] = g["rig_counts"].get(rig, 0) + 1
    return [groups[s] for s in order]


def cal_biography(profiles, sha):
    """Every rig stamp that ever recorded through cal `sha`,
    across the given profiles -- the testimony the slot wears.
    One entry per distinct rig (name + node_match), ordered by
    first appearance, each carrying its take count; empty when
    the sha has no takes anywhere. Pure and GTK-free."""
    out = []
    idx = {}
    for p in (profiles or []):
        m = (p or {}).get("measurement") or {}
        sessions = m.get("sessions") or {}
        for t in (m.get("takes") or []):
            if t.get("cal_sha") != sha:
                continue
            src = ((sessions.get(t.get("session")) or {})
                   .get("source") or {})
            key = (src.get("name"), src.get("node_match"))
            e = idx.get(key)
            if e is None:
                e = {"name": src.get("name"),
                     "node_match": src.get("node_match"),
                     "serial": src.get("serial"),
                     "count": 0}
                idx[key] = e
                out.append(e)
            e["count"] += 1
    return [e for e in out if e["name"]]


def reassign_cal(store, pid, old_sha, new_path):
    """Move EVERY take that consumed cal `old_sha` onto the cal
    at `new_path`, in one stroke -- the operation is bulk by
    design (field verdict: nobody re-hangs takes one by one).
    The new cal joins the library; the old entry stays (the
    library is append-only history); takes with another sha or
    with none are untouched. The fit is not touched: a moved
    canvas stales it honestly through the fingerprint. Returns
    the number of takes moved."""
    prof = store.get(pid)
    if prof is None:
        raise KeyError("no profile %s" % pid)
    m = prof.get("measurement")
    if not m:
        return 0
    entry = cal_entry(new_path)
    takes = [dict(t) for t in (m.get("takes") or [])]
    moved = 0
    for t in takes:
        if t.get("cal_sha") == old_sha:
            t["cal_sha"] = entry["sha256"]
            moved += 1
    if not moved:
        return 0
    m = dict(m)
    lib = dict(m.get("cal_library") or {})
    lib.setdefault(entry["sha256"], {"file": entry["file"],
                                     "points": entry["points"]})
    m["cal_library"] = lib
    m["takes"] = takes
    prof = dict(prof)
    prof["measurement"] = m
    store.save_user(prof)
    return moved


def remove_takes(store, pid, take_ids):
    """Physically drop takes from the canvas -- bad takes are
    deleted, never flagged -- and prune sessions left with no takes.
    Returns the number removed. The fit is not touched: losing a
    take it consumed is exactly what flips it stale."""
    prof = store.get(pid)
    if prof is None:
        raise KeyError("no profile %s" % pid)
    m = prof.get("measurement")
    wanted = set(take_ids or ())
    if not m or not wanted:
        return 0
    takes = list(m.get("takes") or [])
    kept = [t for t in takes if t.get("id") not in wanted]
    removed = len(takes) - len(kept)
    if not removed:
        return 0
    prof = dict(prof)
    m = dict(m)
    m["takes"] = kept
    alive = {t.get("session") for t in kept}
    m["sessions"] = {sid: blk
                     for sid, blk in (m.get("sessions") or {}).items()
                     if sid in alive}
    prof["measurement"] = m
    store.save_user(prof)
    return removed


def refit_and_save(store, pid, bands=None, f_lo=None, f_hi=None,
                   max_boost=None, take_ids=None,
                   allow_edited=False, progress=None):
    """refit.refit_profile over the stored canvas + save under the
    same id. The measurement window's close-time auto-fit and the
    editor's Re-fit button both land here; `progress` is
    fit_profiles' per-band heartbeat -- progress(frac, key, band,
    horizon, evals) -- forwarded into the fit as-is."""
    prof = store.get(pid)
    if prof is None:
        raise KeyError("no profile %s" % pid)
    from . import refit               # circular at module level
    out = refit.refit_profile(prof, bands=bands, f_lo=f_lo,
                              f_hi=f_hi, max_boost=max_boost,
                              take_ids=take_ids,
                              allow_edited=allow_edited,
                              progress=progress)
    return store.save_user(out)
