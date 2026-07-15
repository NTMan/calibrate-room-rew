"""Rebuilding the fit from the canvas (perdeviceeq/refit.py).

The pure-numpy tests pin the reconstruction arithmetic against known
inputs: gain alignment onto the quietest take, power averaging, cal
from the embedded points, the validity gates (unknown ids, off-grid
data, mixed cals under one channel) and the edited-fit protection.
The shim test is the ground truth: a live session's finalize() and
the canvas reconstruction must produce the same smoothed curve to
within the canvas' 0.01 dB storage rounding.
"""
import json
import math
import os
from pathlib import Path

import numpy as np
import pytest

from perdeviceeq import measure_build, profiles, refit
from perdeviceeq import measure_core as mc
from perdeviceeq import measure_session as ms
from perdeviceeq import profiles as profiles_mod
from perdeviceeq.profiles import ProfileStore

ROOT = Path(__file__).resolve().parent.parent
SHIMS = ROOT / "tests" / "shims"

GRAPH = ("{ nodes = [ { type = builtin name = eq label = param_eq "
         "config = { filters = [ { type = bq_peaking, freq = 200, "
         "gain = 9.6, q = 2.25 } ] } } ] }")

GRID = {"f_lo": 20.0, "f_hi": 20000.0, "ppo": 96}
FREQS = mc.log_grid()
N = len(FREQS)


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


def _take(tid, key, mag, soft=0.3, chan=0.3, col=0):
    return {"id": tid, "session": "s1", "channel": key,
            "capture_channel": col,
            "created_utc": "2026-07-14T00:00:00+00:00",
            "mag_db_uncal": [float(v) for v in mag],
            "delay_ms": 5.0, "snr_db": 45.0, "peak_dbfs": -6.0,
            "noise_dbfs": -80.0, "clipped": 0, "repaired": 0,
            "chan_vol": chan, "soft_vol": soft}


def _meas(takes, cal=None):
    return {"grid": dict(GRID),
            "source": {"name": "rig", "serial": "", "node_match": "n",
                       "channels": 2, "cal": cal or {}},
            "sessions": {"s1": {"created_utc": "x"}},
            "takes": takes}


def test_alignment_and_power_average():
    quiet = np.zeros(N)
    loud = quiet + 20.0 * math.log10(2.0)   # same take, double drive
    m = _meas([_take("a", "FL", quiet, soft=0.25),
               _take("b", "FL", loud, soft=0.5)])
    res, used = refit.channel_results(m)
    d = res["FL"]["data"]
    assert used == ["a", "b"]
    assert np.allclose(d["mag_db_uncal"], 0.0, atol=1e-9)
    assert np.allclose(d["spread_db"], 0.0, atol=1e-9)
    assert res["FL"]["levels"]["gain_comp_db"] == [0.0, -6.021]


def test_unknown_gain_disables_compensation():
    m = _meas([_take("a", "FL", np.zeros(N), soft=None),
               _take("b", "FL", np.full(N, 6.0206), soft=0.5)])
    res, _ = refit.channel_results(m)
    assert res["FL"]["levels"]["gain_comp_db"] is None
    want = 10.0 * math.log10((1.0 + 10 ** 0.60206) / 2.0)
    assert np.allclose(res["FL"]["data"]["mag_db_uncal"], want,
                       atol=1e-4)


def test_cal_applied_from_embedded_points():
    cal = {"0": {"file": "tilt.txt", "sha256": "abc",
                 "points": [[20.0, 0.0], [20000.0, -6.0]]}}
    m = _meas([_take("a", "FL", np.zeros(N))], cal=cal)
    res, _ = refit.channel_results(m)
    d = res["FL"]["data"]
    assert res["FL"]["cal_file"] == "abc"
    assert abs(d["mag_db_raw"][0]) < 1e-9
    assert abs(d["mag_db_raw"][-1] - 6.0) < 1e-9
    assert np.allclose(d["mag_db_uncal"], 0.0, atol=1e-9)


def test_mixed_cals_within_channel_refused():
    cal = {"0": {"sha256": "x", "points": [[20.0, 0.0]]},
           "1": {"sha256": "y", "points": [[20.0, 0.0]]}}
    m = _meas([_take("a", "FL", np.zeros(N), col=0),
               _take("b", "FL", np.zeros(N), col=1)], cal=cal)
    with pytest.raises(refit.RefitError, match="different cal"):
        refit.channel_results(m)


def test_take_selection_is_validated():
    m = _meas([_take("a", "FL", np.zeros(N))])
    with pytest.raises(refit.RefitError, match="unknown take"):
        refit.channel_results(m, take_ids=["a", "zz"])
    res, used = refit.channel_results(m, take_ids=["a"])
    assert used == ["a"] and set(res) == {"FL"}
    with pytest.raises(refit.RefitError, match="no takes"):
        refit.channel_results(_meas([]))


def test_off_grid_take_refused():
    m = _meas([_take("a", "FL", np.zeros(N - 1))])
    with pytest.raises(refit.RefitError, match="profile grid"):
        refit.channel_results(m)


def _bumpy(f0, gain):
    x = np.log(FREQS / f0)
    return gain * np.exp(-(x * x) / 0.08)


def _prof():
    takes = [_take("a", "FL", _bumpy(1000.0, 6.0)),
             _take("b", "FR", _bumpy(2000.0, -5.0), col=1)]
    m = _meas(takes)
    params = {"bands": 6, "f_lo": 20.0, "f_hi": 12000.0,
              "max_boost": 6.0, "smoothing": 6, "mono": False}
    return {"id": "p1", "name": "P", "version": 3,
            "apply_all": True, "preamp": -2.0, "ch_keys": [],
            "all": {"bands": []}, "channels": {},
            "provenance": {"kind": "measured"},
            "device": {"label": "dev"},
            "measurement": m,
            "fit": {"at": "old", "algo": "fit_peq",
                    "params": params, "target": {"kind": "flat"},
                    "takes": ["a", "b"],
                    "inputs_sha256": measure_build.fit_fingerprint(
                        m, ["a", "b"], params),
                    "edited": False}}


def test_refit_updates_fit_and_keeps_canvas():
    prof = _prof()
    assert refit.fit_is_stale(prof) is False
    out = refit.refit_profile(prof)
    assert out is not prof and prof["fit"]["at"] == "old"
    assert out["measurement"] == prof["measurement"]
    assert out["provenance"] == {"kind": "measured"}
    assert out["apply_all"] is False
    assert set(out["ch_keys"]) == {"FL", "FR"}
    for k in ("FL", "FR"):
        assert out["channels"][k]["bands"]      # the bump got bands
    assert out["preamp"] == 0.0                 # fresh-fit semantics
    f = out["fit"]
    assert f["at"] != "old" and f["edited"] is False
    assert f["takes"] == ["a", "b"]
    assert f["inputs_sha256"] == measure_build.fit_fingerprint(
        out["measurement"], f["takes"], f["params"])
    assert f["output_sha256"] == profiles.playback_sha256(out)
    assert refit.fit_is_stale(out) is False


def test_edited_fit_is_protected():
    prof = _prof()
    prof["fit"]["edited"] = True
    with pytest.raises(refit.RefitError, match="edited"):
        refit.refit_profile(prof)
    out = refit.refit_profile(prof, allow_edited=True)
    assert out["fit"]["edited"] is False


def test_stale_tracks_canvas_edits():
    prof = _prof()
    prof["measurement"]["takes"][0]["mag_db_uncal"][0] += 1.0
    assert refit.fit_is_stale(prof)
    del prof["fit"]
    assert not refit.fit_is_stale(prof)     # nothing to compare
    assert not refit.fit_is_stale({"id": "x"})
    gone = _prof()
    gone["measurement"]["takes"] = gone["measurement"]["takes"][:1]
    assert refit.fit_is_stale(gone)         # a consumed take vanished


def test_param_overrides_land_in_fit():
    prof = _prof()
    out = refit.refit_profile(prof, bands=4, f_hi=10000.0)
    assert out["fit"]["params"]["bands"] == 4
    assert out["fit"]["params"]["f_hi"] == 10000.0
    assert out["fit"]["inputs_sha256"] != prof["fit"]["inputs_sha256"]


def test_reconstruction_matches_live_pipeline(shim_state, store,
                                              tmp_path):
    flat = tmp_path / "flat.txt"
    flat.write_text("20 0.0\n1000 0.0\n20000 0.0\n")
    tilt = tmp_path / "tilt.txt"
    tilt.write_text("20 0.0\n1000 -3.0\n20000 -6.0\n")
    cfg = ms.SessionConfig(sink="test_sink", source="test_source",
                           channels=2, samples=131072,
                           save_dir=str(tmp_path / "takes"))
    pid = store.save_user({"id": "e2e", "name": "e2e", "version": 3,
                           "apply_all": True, "preamp": 0.0,
                           "ch_keys": [], "all": {"bands": []},
                           "channels": {}})
    ses = ms.MeasureSession(cfg)
    with ses:
        ses.take(0)
        ids = measure_build.commit_take(
            store, pid, ses, 0, "FL", ses.takes_of(0)[-1].id,
            cal=str(flat))
        ses.take(1)
        measure_build.commit_take(
            store, pid, ses, 1, "FR", ses.takes_of(1)[-1].id,
            cal=str(tilt), canvas_session=ids["session"])
        ses.take(1)          # two takes on FR: exercise averaging
        measure_build.commit_take(
            store, pid, ses, 1, "FR", ses.takes_of(1)[-1].id,
            cal=str(tilt), canvas_session=ids["session"])
    measure_build.refit_and_save(store, pid)
    prof = store.get(pid)
    assert not refit.fit_is_stale(prof)     # settled and coherent
    ref = {"FL": ses.finalize(0, cal=str(flat)),
           "FR": ses.finalize(1, cal=str(tilt))}
    res, used = refit.channel_results(prof["measurement"],
                                      smoothing=cfg.smoothing)
    assert len(used) == 3
    for key in ("FL", "FR"):
        got = np.asarray(res[key]["data"]["mag_db_smoothed"])
        want = np.asarray(ref[key]["data"]["mag_db_smoothed"])
        assert np.max(np.abs(got - want)) < 0.05
    for key in ("FL", "FR"):
        assert prof["channels"][key]["bands"]
