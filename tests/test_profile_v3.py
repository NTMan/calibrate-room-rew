"""Profile schema v3 (optional measurement blocks, "version": 3) and the
one-shot v2 converter in tools/migrate_profiles_v2_to_v3.py.

v3 keeps the v2 playback body verbatim and adds room for four optional
dict blocks -- "provenance", "device", "fit", "measurement" -- that the
store must carry through a save/load round-trip untouched (a build that
strips them on save would silently destroy the measurement canvas).
The app reads ONLY v3 files; the converter restamps the version, marks
the provenance legacy and keeps the original next to the file as
*.json.v2. v1 files are refused with a pointer to the older tool.
"""
import json

import pytest

from perdeviceeq import profiles
import migrate_profiles_v2_to_v3 as mig


def _v2():
    return {"id": "x", "name": "X", "version": 2, "apply_all": False,
            "preamp": -3.0, "ch_keys": ["FL"], "all": {"bands": []},
            "channels": {"FL": {"bands": [
                {"type": "PK", "freq": 200, "gain": 3.0, "q": 1.0}]}}}


def _blocks():
    return {
        "provenance": {"kind": "measured", "origin": None},
        "device": {"label": "Test Buds",
                   "sink": {"node_name": "bluez_output.x",
                            "api": "bluez5"}},
        "fit": {"algo": "fit_peq", "takes": ["t1"],
                "params": {"bands": 10}},
        "measurement": {
            "grid": {"f_lo": 20.0, "f_hi": 20000.0, "ppo": 96},
            "source": {"name": "miniDSP EARS", "serial": "861",
                       "cal": {"0": {"file": "L_RAW.txt",
                                     "points": [[20.0, -1.2]]}}},
            "sessions": {"m1": {"created_utc":
                                "2026-07-14T08:55:02+00:00"}},
            "takes": [{"id": "t1", "session": "m1", "channel": "FL",
                       "mag_db_uncal": [0.0, 1.5, -2.25]}]}}


def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "SYS_PROFILE_DIRS", [])
    monkeypatch.setattr(profiles, "USER_PROFILES_DIR", str(tmp_path))
    monkeypatch.setattr(profiles, "BINDINGS_FILE",
                        str(tmp_path / "bindings.json"))
    return profiles.ProfileStore()


def test_migrate_restamps_and_marks_legacy():
    m = mig.migrate_body(_v2())
    assert m["version"] == 3
    assert m["provenance"] == {"kind": "legacy"}
    assert m["preamp"] == -3.0                  # playback body untouched
    assert m["channels"]["FL"]["bands"]
    assert mig.migrate_body(m) is m             # v3 passes through as is


def test_migrate_refuses_v1():
    v1 = {"apply_all": True, "all": {"preamp": -1.0, "bands": []}}
    with pytest.raises(ValueError, match="migrate_profiles_v1_to_v2"):
        mig.migrate_body(v1)


def test_convert_dir_roundtrip(tmp_path):
    (tmp_path / "old.json").write_text(json.dumps(_v2()))
    (tmp_path / "new.json").write_text(
        json.dumps(mig.migrate_body(_v2())))
    (tmp_path / "ancient.json").write_text(json.dumps(
        {"apply_all": True, "all": {"preamp": 0.0, "bands": []}}))
    assert mig.convert_dir(str(tmp_path)) == (1, 1, 1)
    got = json.loads((tmp_path / "old.json").read_text())
    assert got["version"] == mig.SCHEMA_VERSION   # the TOOL's target
    assert got["provenance"]["kind"] == "legacy"
    assert (tmp_path / "old.json.v2").exists()       # backup kept
    # a second run converts nothing and still refuses the v1 file
    assert mig.convert_dir(str(tmp_path)) == (0, 2, 1)


def test_store_skips_old_loads_current(tmp_path, monkeypatch,
                                       capsys):
    (tmp_path / "old.json").write_text(json.dumps(_v2()))
    good = dict(mig.migrate_body(_v2()), id="good", name="Good",
                version=profiles.SCHEMA_VERSION)
    (tmp_path / "good.json").write_text(json.dumps(good))
    st = _store(tmp_path, monkeypatch)
    assert "good" in st.profiles and "x" not in st.profiles
    assert "migrate_profiles_v3_to_v4" in capsys.readouterr().err


def test_save_load_round_trip_keeps_blocks(tmp_path, monkeypatch):
    st = _store(tmp_path, monkeypatch)
    pid = st.save_user(dict(_v2(), version=profiles.SCHEMA_VERSION,
                            **_blocks()))
    got = _store(tmp_path, monkeypatch).profiles[pid]  # re-read the disk
    for key in profiles.V3_BLOCKS:
        assert got[key] == _blocks()[key]
    assert got["version"] == profiles.SCHEMA_VERSION


def test_save_drops_empty_or_non_dict_blocks(tmp_path, monkeypatch):
    st = _store(tmp_path, monkeypatch)
    pid = st.save_user(dict(_v2(), measurement={}, fit="stale"))
    raw = json.loads((tmp_path / ("%s.json" % pid)).read_text())
    assert "measurement" not in raw and "fit" not in raw


def test_editor_body_carries_blocks_and_marks_edited():
    stored = dict(_v2(), version=3, **_blocks())
    body = {k: stored[k] for k in
            ("id", "name", "apply_all", "preamp", "ch_keys",
             "all", "channels")}
    same = profiles.editor_body(dict(body), stored)
    for key in profiles.V3_BLOCKS:
        assert same[key] == _blocks()[key]
    assert same["fit"] == _blocks()["fit"]      # untouched sound
    # preamp is gain staging, never an edit
    calm = profiles.editor_body(dict(body, preamp=-6.0), stored)
    assert "edited" not in calm["fit"]
    bands = {"FL": {"bands": [{"type": "PK", "freq": 400,
                               "gain": 1.0, "q": 1.0}]}}
    out = profiles.editor_body(dict(body, channels=bands), stored)
    assert out["fit"]["edited"] is True
    assert "edited" not in stored["fit"]        # input not mutated
    again = profiles.editor_body(dict(body, preamp=-7.0,
                                      channels=bands), out)
    assert again["fit"]["edited"] is True
    # nothing stored / no fit: nothing invented
    assert "measurement" not in profiles.editor_body(dict(body), None)
    nofit = {k: v for k, v in stored.items() if k != "fit"}
    assert "fit" not in profiles.editor_body(
        dict(body, channels=bands), nofit)


def test_edited_mark_is_derived_from_the_fit_output():
    stored = dict(_v2(), version=3, **_blocks())
    stored["fit"] = dict(
        stored["fit"],
        output_sha256=profiles.playback_sha256(stored))
    body = {k: stored[k] for k in
            ("id", "name") + profiles.PLAYBACK_KEYS}
    calm = profiles.editor_body(dict(body, preamp=-6.0), stored)
    assert calm["fit"].get("edited") is not True   # gain staging
    bands = {"FL": {"bands": [{"type": "PK", "freq": 400,
                               "gain": 1.0, "q": 1.0}]}}
    hot = profiles.editor_body(dict(body, channels=bands), stored)
    assert hot["fit"]["edited"] is True
    cold = profiles.editor_body(dict(body), hot)   # undo landed
    assert cold["fit"]["edited"] is False
    # and riding the preamp on a fitted profile stays clean
    quiet = profiles.editor_body(dict(body, preamp=-6.2), cold)
    assert quiet["fit"]["edited"] is False
