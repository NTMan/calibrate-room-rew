"""Profile schema v2 (one shared preamp, "version": 2) and the one-shot
v1 converter in tools/migrate_profiles_v1_to_v2.py.

The app reads ONLY v3 files (ProfileStore skips others with a pointer to
the converter); the converter folds the applied slots' preamps into
their minimum -- the only direction that can never introduce clipping --
strips per-slot leftovers and stamps the version. v1 originals stay next
to the converted files as *.json.v1.
"""
import json

from perdeviceeq import eq, profiles
import migrate_profiles_v1_to_v2 as mig


def _v1_unlinked():
    return {"id": "x", "apply_all": False, "ch_keys": ["FL", "FR"],
            "all": {"preamp": 0.0, "bands": []},
            "channels": {
                "FL": {"preamp": -9.6, "bands": [
                    {"type": "PK", "freq": 200, "gain": 9.6, "q": 2.25}]},
                "FR": {"preamp": -8.5, "bands": [
                    {"type": "PK", "freq": 196, "gain": 8.5, "q": 2.3}]}}}


def test_migrate_linked_takes_all_slot():
    p = {"apply_all": True, "all": {"preamp": -3.0, "bands": []},
         "channels": {"FL": {"preamp": -99.0, "bands": []}}}  # not applied
    m = mig.migrate_body(p)
    assert m["preamp"] == -3.0 and m["version"] == 2


def test_migrate_unlinked_takes_min_and_strips_slots():
    m = mig.migrate_body(_v1_unlinked())
    assert m["preamp"] == -9.6           # min(-9.6, -8.5): FR only gets quieter
    assert all("preamp" not in s for s in m["channels"].values())
    assert "preamp" not in m["all"]
    assert mig.migrate_body(m) is m      # v2 passes through untouched


def test_migrated_graph_equals_shared_preamp_graph():
    m = mig.migrate_body(_v1_unlinked())
    sets = [(-9.6, [eq.Band.from_dict(b)
                    for b in m["channels"][k]["bands"]])
            for k in ("FL", "FR")]
    assert eq.profile_graph(m) == eq.build_graph_channels(sets)


def test_has_content_sees_shared_preamp():
    flat = {"apply_all": True, "preamp": 0.0, "all": {"bands": []},
            "channels": {}}
    assert not eq.profile_has_content(flat)
    flat["preamp"] = -1.0
    assert eq.profile_has_content(flat)


def test_convert_dir_roundtrip(tmp_path):
    (tmp_path / "old.json").write_text(json.dumps(_v1_unlinked()))
    v2 = mig.migrate_body(_v1_unlinked())
    (tmp_path / "new.json").write_text(json.dumps(v2))
    c, s, f = mig.convert_dir(str(tmp_path))
    assert (c, s, f) == (1, 1, 0)
    got = json.loads((tmp_path / "old.json").read_text())
    assert got["version"] == 2 and got["preamp"] == -9.6
    assert (tmp_path / "old.json.v1").exists()          # backup kept
    # idempotent: a second run converts nothing
    assert mig.convert_dir(str(tmp_path))[0] == 0


def test_store_skips_v1_files(tmp_path, monkeypatch, capsys):
    v2 = mig.migrate_body(_v1_unlinked())
    v2["id"] = "good"; v2["name"] = "Good"
    v2["version"] = profiles.SCHEMA_VERSION   # store reads current only
    (tmp_path / "good.json").write_text(json.dumps(v2))
    old = dict(_v1_unlinked(), id="old", name="Old")
    (tmp_path / "old.json").write_text(json.dumps(old))

    monkeypatch.setattr(profiles, "SYS_PROFILE_DIRS", [])
    monkeypatch.setattr(profiles, "USER_PROFILES_DIR", str(tmp_path))
    monkeypatch.setattr(profiles, "BINDINGS_FILE",
                        str(tmp_path / "bindings.json"))
    st = profiles.ProfileStore()
    assert "good" in st.profiles and "old" not in st.profiles
    assert "migrate_profiles_v2_to_v3" in capsys.readouterr().err
