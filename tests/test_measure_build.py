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
