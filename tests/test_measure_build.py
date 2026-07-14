"""Test the 'Create profile' pipeline (measure_build) end to end against
the pw-* shims: measure two channels, build a profile, and confirm it is
saved as a user profile, bound to the sink, and carries the v3
measurement canvas (source + session + takes + fit fingerprint).
ProfileStore's config paths are redirected into tmp so the real
~/.config is untouched.
"""
import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pytest

from perdeviceeq import measure_build, measure_session as ms
from perdeviceeq import measure_core as mc
from perdeviceeq import profiles
from perdeviceeq import profiles as profiles_mod
from perdeviceeq.profiles import ProfileStore

ROOT = Path(__file__).resolve().parent.parent
SHIMS = ROOT / "tests" / "shims"

GRAPH = ("{ nodes = [ { type = builtin name = eq label = param_eq "
         "config = { filters = [ { type = bq_peaking, freq = 200, "
         "gain = 9.6, q = 2.25 } ] } } ] }")


@pytest.fixture
def shim_state(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    (state / "metadata.json").write_text(json.dumps({"test_sink": GRAPH}))
    (state / "volume.json").write_text(json.dumps({"cubic": 0.30}))
    monkeypatch.setenv("PDE_SHIM_DIR", str(state))
    monkeypatch.setenv("PDE_SHIM_REPO", str(ROOT))
    monkeypatch.setenv("PDE_SHIM_PLAY_SECONDS", "0.9")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("PATH", "%s%s%s"
                       % (SHIMS, os.pathsep, os.environ["PATH"]))
    return state


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles_mod, "USER_PROFILES_DIR",
                        str(tmp_path / "profiles"))
    monkeypatch.setattr(profiles_mod, "SYS_PROFILE_DIRS", [])
    monkeypatch.setattr(profiles_mod, "CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(profiles_mod, "BINDINGS_FILE",
                        str(tmp_path / "cfg" / "bindings.json"))
    return ProfileStore()


def _cfg(tmp_path, **kw):
    kw.setdefault("samples", 131072)
    return ms.SessionConfig(sink="test_sink", source="test_source",
                            channels=2, save_dir=str(tmp_path / "takes"),
                            **kw)


def test_build_and_bind_creates_and_binds(shim_state, store, tmp_path):
    ses = ms.MeasureSession(_cfg(tmp_path))
    with ses:
        ses.take(0)
        ses.take(1)
    pid = measure_build.build_and_bind(
        ses, {0: "FL", 1: "FR"}, store, "test_sink",
        name="ATVEL S7 (EARS RAW)", bands=12)

    p = store.get(pid)
    assert p is not None
    assert p["name"] == "ATVEL S7 (EARS RAW)"
    assert p["version"] == 3
    assert p["apply_all"] is False
    assert set(p["ch_keys"]) == {"FL", "FR"}
    for key in ("FL", "FR"):
        assert p["channels"][key]["bands"]
    # bound to the sink, and heard at once when the editor loads pid
    assert store.binding_for("test_sink") == pid
    # the canvas replaces the result_*.json side files
    assert not list(Path(ses.outdir).glob("result_*.json"))
    m = p["measurement"]
    assert m["grid"] == {"f_lo": mc.GRID_F_LO, "f_hi": mc.GRID_F_HI,
                         "ppo": mc.GRID_PPO}
    assert m["source"]["node_match"] == "test_source"
    assert m["source"]["channels"] == 2
    (sid, sess), = m["sessions"].items()
    assert sess["sink"]["node_name"] == "test_sink"
    assert sess["sweep"]["fs"] == ses.sweep.fs
    assert isinstance(sess["created_utc"], str)
    n = len(mc.log_grid())
    assert {t["channel"] for t in m["takes"]} == {"FL", "FR"}
    for t in m["takes"]:
        assert t["session"] == sid
        assert len(t["mag_db_uncal"]) == n
        assert isinstance(t["created_utc"], str)
        assert t["capture_channel"] == {"FL": 0, "FR": 1}[t["channel"]]
    assert p["provenance"] == {"kind": "measured"}
    assert p["device"]["sink"]["node_name"] == "test_sink"
    fit = p["fit"]
    assert sorted(fit["takes"]) == sorted(t["id"] for t in m["takes"])
    assert fit["params"]["bands"] == 12
    assert fit["edited"] is False
    assert fit["inputs_sha256"] == measure_build.fit_fingerprint(
        m, fit["takes"], fit["params"])
    assert fit["output_sha256"] == profiles.playback_sha256(p)


def test_build_and_bind_uses_per_channel_cal(shim_state, store, tmp_path):
    flat = tmp_path / "flat.txt"
    flat.write_text("20 0.0\n1000 0.0\n20000 0.0\n")
    tilt = tmp_path / "tilt.txt"
    tilt.write_text("20 0.0\n1000 -3.0\n20000 -6.0\n")
    ses = ms.MeasureSession(_cfg(tmp_path))
    with ses:
        ses.take(0)
        ses.take(1)
    pid = measure_build.build_and_bind(
        ses, {0: "FL", 1: "FR"}, store, "test_sink", name="cal test",
        cal={0: str(flat), 1: str(tilt)},
        source={"name": "miniDSP EARS", "serial": "8613"})
    src = store.get(pid)["measurement"]["source"]
    assert src["name"] == "miniDSP EARS" and src["serial"] == "8613"
    assert src["cal"]["0"]["file"] == "flat.txt"
    assert src["cal"]["1"]["file"] == "tilt.txt"
    assert src["cal"]["1"]["sha256"] == hashlib.sha256(
        tilt.read_bytes()).hexdigest()
    assert src["cal"]["0"]["points"][0] == [20.0, 0.0]
    assert src["cal"]["1"]["points"][-1] == [20000.0, -6.0]


def test_take_dict_resamples_onto_profile_grid():
    freqs = mc.log_grid()
    coarse = freqs[::4]
    rec = ms.TakeRecord(1, 0, coarse,
                        np.linspace(-3.0, 3.0, len(coarse)),
                        5.0, 40.0, -6.0, 0, 0, None,
                        capture_channel=1, created_utc="t")
    d = measure_build.take_dict(rec, "s1", "FL", freqs)
    assert len(d["mag_db_uncal"]) == len(freqs)
    assert d["channel"] == "FL" and d["session"] == "s1"
    assert d["capture_channel"] == 1 and d["created_utc"] == "t"
    # edges survive the log-f interpolation (and the 0.01 dB rounding)
    assert d["mag_db_uncal"][0] == -3.0
    assert d["mag_db_uncal"][-1] == 3.0


def test_fingerprint_tracks_takes_cal_and_params():
    m = {"grid": {"f_lo": 20.0}, "source": {"cal": {}},
         "takes": [{"id": "a", "mag_db_uncal": [0.0, 1.0]},
                   {"id": "b", "mag_db_uncal": [2.0]}]}
    p = {"bands": 10}
    base = measure_build.fit_fingerprint(m, ["a", "b"], p)
    assert measure_build.fit_fingerprint(m, ["a", "b"], p) == base
    assert measure_build.fit_fingerprint(m, ["a"], p) != base
    assert measure_build.fit_fingerprint(m, ["a", "b"],
                                         {"bands": 12}) != base
    m["takes"][0]["mag_db_uncal"][0] = 0.5
    assert measure_build.fit_fingerprint(m, ["a", "b"], p) != base
    m["source"]["cal"] = {"0": {"sha256": "x"}}
    assert measure_build.fit_fingerprint(m, ["a", "b"], p) != base


def _second_session(tmp_path, take_channels):
    ses = ms.MeasureSession(_cfg(tmp_path))
    with ses:
        for ch, analyze in take_channels:
            ses.take(ch, analyze=analyze)
    return ses


def test_append_grows_the_canvas_and_refits(shim_state, store,
                                            tmp_path):
    from perdeviceeq import refit
    flat = tmp_path / "flat.txt"
    flat.write_text("20 0.0\n1000 0.0\n20000 0.0\n")
    tilt = tmp_path / "tilt.txt"
    tilt.write_text("20 0.0\n1000 -3.0\n20000 -6.0\n")
    ses = ms.MeasureSession(_cfg(tmp_path))
    with ses:
        ses.take(0)
        ses.take(1)
    pid = measure_build.build_and_bind(
        ses, {0: "FL", 1: "FR"}, store, "test_sink", name="v1",
        cal={0: str(flat)}, source={"name": "EARS", "serial": "861"})
    ses2 = _second_session(tmp_path, [(0, 0)])
    out = measure_build.append_and_bind(
        ses2, {0: "FL"}, store, "test_sink", pid,
        cal={0: str(tilt)},                 # cal update on column 0
        source={"serial": "861"}, name="v2")
    assert out == pid
    p = store.get(pid)
    m = p["measurement"]
    assert p["name"] == "v2"
    assert len(m["sessions"]) == 2
    by = {}
    for t in m["takes"]:
        by.setdefault(t["channel"], []).append(t)
    assert len(by["FL"]) == 2 and len(by["FR"]) == 1
    # the freshly selected cal replaced the stored column entry
    assert m["source"]["cal"]["0"]["file"] == "tilt.txt"
    # the fit was rebuilt over the merged canvas: coherent again
    assert sorted(p["fit"]["takes"]) == sorted(t["id"]
                                               for t in m["takes"])
    assert not refit.fit_is_stale(p)
    assert store.binding_for("test_sink") == pid


def test_append_refuses_a_different_rig(shim_state, store, tmp_path):
    ses = ms.MeasureSession(_cfg(tmp_path))
    with ses:
        ses.take(0)
    pid = measure_build.build_and_bind(
        ses, {0: "FL"}, store, "test_sink", name="p",
        source={"serial": "861"})
    ses2 = _second_session(tmp_path, [(0, 0)])
    with pytest.raises(measure_build.SourceMismatch):
        measure_build.append_and_bind(
            ses2, {0: "FL"}, store, "test_sink", pid,
            source={"serial": "999"})
    # serial unknown on both sides: the node identity decides
    p = dict(store.get(pid))
    p["measurement"] = dict(p["measurement"])
    p["measurement"]["source"] = dict(p["measurement"]["source"],
                                      serial="", node_match="other")
    store.save_user(p)
    with pytest.raises(measure_build.SourceMismatch):
        measure_build.append_and_bind(
            ses2, {0: "FL"}, store, "test_sink", pid)


def test_replace_drops_the_channels_old_takes(shim_state, store,
                                              tmp_path):
    ses = ms.MeasureSession(_cfg(tmp_path))
    with ses:
        ses.take(0)
        ses.take(1)
    pid = measure_build.build_and_bind(
        ses, {0: "FL", 1: "FR"}, store, "test_sink", name="p")
    old_fl = {t["id"] for t in
              store.get(pid)["measurement"]["takes"]
              if t["channel"] == "FL"}
    ses2 = _second_session(tmp_path, [(0, 0)])
    measure_build.append_and_bind(ses2, {0: "FL"}, store,
                                  "test_sink", pid,
                                  replace=("FL",))
    m = store.get(pid)["measurement"]
    fl = [t for t in m["takes"] if t["channel"] == "FL"]
    fr = [t for t in m["takes"] if t["channel"] == "FR"]
    assert len(fl) == 1 and not (old_fl & {t["id"] for t in fl})
    assert len(fr) == 1                       # untouched channel


def test_append_creates_canvas_on_a_bare_profile(shim_state, store,
                                                 tmp_path):
    pid = store.save_user({"id": "bare", "name": "Bare",
                           "version": 3, "apply_all": True,
                           "preamp": 0.0, "ch_keys": [],
                           "all": {"bands": []}, "channels": {}})
    ses = _second_session(tmp_path, [(0, 0), (1, 1)])
    out = measure_build.append_and_bind(
        ses, {0: "FL", 1: "FR"}, store, "test_sink", pid)
    p = store.get(out)
    assert out == pid and p["name"] == "Bare"
    assert p["provenance"] == {"kind": "measured"}
    assert len(p["measurement"]["takes"]) == 2
    for key in ("FL", "FR"):
        assert p["channels"][key]["bands"]


def test_append_rename_only_and_edited_gate(shim_state, store,
                                            tmp_path):
    from perdeviceeq import refit
    ses = ms.MeasureSession(_cfg(tmp_path))
    with ses:
        ses.take(0)
    pid = measure_build.build_and_bind(
        ses, {0: "FL"}, store, "test_sink", name="old")
    before = store.get(pid)["measurement"]
    measure_build.append_and_bind(None, {}, store, "test_sink",
                                  pid, name="new")
    p = store.get(pid)
    assert p["name"] == "new"
    assert p["measurement"] == before         # rename moved nothing
    p = dict(p)
    p["fit"] = dict(p["fit"], edited=True)
    store.save_user(p)
    ses2 = _second_session(tmp_path, [(0, 0)])
    with pytest.raises(refit.RefitError, match="edited"):
        measure_build.append_and_bind(ses2, {0: "FL"}, store,
                                      "test_sink", pid)
    measure_build.append_and_bind(ses2, {0: "FL"}, store,
                                  "test_sink", pid,
                                  allow_edited=True)
    assert store.get(pid)["fit"]["edited"] is False


def test_commit_take_builds_the_canvas_incrementally(shim_state,
                                                     store, tmp_path):
    from perdeviceeq import refit
    pid = store.save_user({"id": "inc", "name": "Inc", "version": 3,
                           "apply_all": True, "preamp": 0.0,
                           "ch_keys": [], "all": {"bands": []},
                           "channels": {}})
    flat = tmp_path / "flat.txt"
    flat.write_text("20 0.0\n1000 0.0\n20000 0.0\n")
    ses = ms.MeasureSession(_cfg(tmp_path))
    with ses:
        ses.take(0)
        r0 = ses.takes_of(0)[-1]
        ids = measure_build.commit_take(
            store, pid, ses, 0, "FL", r0.id, cal=str(flat),
            source={"name": "EARS", "serial": "861"})
        m = store.get(pid)["measurement"]
        assert len(m["takes"]) == 1          # landed immediately
        assert m["source"]["serial"] == "861"
        assert m["source"]["cal"]["0"]["file"] == "flat.txt"
        ses.take(0)
        r1 = ses.takes_of(0)[-1]
        ids2 = measure_build.commit_take(
            store, pid, ses, 0, "FL", r1.id,
            canvas_session=ids["session"])
        ses.take(1)
        r2 = ses.takes_of(1)[-1]
        measure_build.commit_take(store, pid, ses, 1, "FR", r2.id,
                                  canvas_session=ids["session"])
    p = store.get(pid)
    m = p["measurement"]
    assert ids2["session"] == ids["session"]
    assert len(m["sessions"]) == 1           # one live session, once
    assert len(m["takes"]) == 3
    assert p["provenance"] == {"kind": "measured"}
    assert "fit" not in p                    # commits never fit
    events = []
    measure_build.refit_and_save(
        store, pid,
        progress=lambda d, n, k: events.append((d, n, k)))
    p = store.get(pid)
    assert not refit.fit_is_stale(p)
    assert sorted(p["fit"]["takes"]) == sorted(t["id"]
                                               for t in m["takes"])
    assert [e[0] for e in events] == [0, 1, 2]
    assert events[-1] == (2, 2, None)
    assert {e[2] for e in events[:-1]} == {"FL", "FR"}


def test_commit_take_respects_the_rig_gate(shim_state, store,
                                           tmp_path):
    ses = ms.MeasureSession(_cfg(tmp_path))
    with ses:
        ses.take(0)
    pid = measure_build.build_and_bind(
        ses, {0: "FL"}, store, "test_sink", name="p",
        source={"serial": "861"})
    before = store.get(pid)["measurement"]
    ses2 = _second_session(tmp_path, [(0, 0)])
    rid = ses2.takes_of(0)[-1].id
    with pytest.raises(measure_build.SourceMismatch):
        measure_build.commit_take(store, pid, ses2, 0, "FL", rid,
                                  source={"serial": "999"})
    assert store.get(pid)["measurement"] == before


def test_remove_takes_prunes_sessions_and_stales_the_fit(
        shim_state, store, tmp_path):
    from perdeviceeq import refit
    ses = ms.MeasureSession(_cfg(tmp_path))
    with ses:
        ses.take(0)
        ses.take(0)
    pid = measure_build.build_and_bind(
        ses, {0: "FL"}, store, "test_sink", name="p")
    m = store.get(pid)["measurement"]
    first, second = [t["id"] for t in m["takes"]]
    sid, = m["sessions"]
    assert measure_build.remove_takes(store, pid, ["nope"]) == 0
    assert measure_build.remove_takes(store, pid, [first]) == 1
    p = store.get(pid)
    assert [t["id"] for t in p["measurement"]["takes"]] == [second]
    assert sid in p["measurement"]["sessions"]   # still referenced
    assert refit.fit_is_stale(p)                 # consumed take gone
    assert measure_build.remove_takes(store, pid, [second]) == 1
    p = store.get(pid)
    assert p["measurement"]["takes"] == []
    assert p["measurement"]["sessions"] == {}    # pruned with it
