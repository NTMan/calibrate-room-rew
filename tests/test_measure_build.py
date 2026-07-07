"""Test the 'Create profile' pipeline (measure_build) end to end against
the pw-* shims: measure two channels, build a profile, and confirm it is
saved as a user profile and bound to the sink. ProfileStore's config
paths are redirected into tmp so the real ~/.config is untouched.
"""
import json
import os
from pathlib import Path

import pytest

from perdeviceeq import measure_build, measure_session as ms
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
    assert p["version"] == 2
    assert p["apply_all"] is False
    assert set(p["ch_keys"]) == {"FL", "FR"}
    for key in ("FL", "FR"):
        assert p["channels"][key]["bands"]
    # bound to the sink, and heard at once when the editor loads pid
    assert store.binding_for("test_sink") == pid
    # per-channel result.json kept for later recompensate / re-fit
    assert os.path.exists(os.path.join(ses.outdir, "result_FL.json"))
    assert os.path.exists(os.path.join(ses.outdir, "result_FR.json"))


def test_build_and_bind_uses_per_channel_cal(shim_state, store, tmp_path):
    flat = tmp_path / "flat.txt"
    flat.write_text("20 0.0\n1000 0.0\n20000 0.0\n")
    tilt = tmp_path / "tilt.txt"
    tilt.write_text("20 0.0\n1000 -3.0\n20000 -6.0\n")
    ses = ms.MeasureSession(_cfg(tmp_path))
    with ses:
        ses.take(0)
        ses.take(1)
    measure_build.build_and_bind(
        ses, {0: "FL", 1: "FR"}, store, "test_sink", name="cal test",
        cal={0: str(flat), 1: str(tilt)})
    fl = json.loads((Path(ses.outdir) / "result_FL.json").read_text())
    fr = json.loads((Path(ses.outdir) / "result_FR.json").read_text())
    assert os.path.basename(fl["cal_file"]) == "flat.txt"
    assert os.path.basename(fr["cal_file"]) == "tilt.txt"
