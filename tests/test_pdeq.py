# -*- coding: utf-8 -*-
"""The .pdeq package: deterministic bytes, honest validation, and a
byte-stable trip through the user store."""

import json

import pytest

from perdeviceeq import pdeq
from perdeviceeq import profiles as profiles_mod
from perdeviceeq.config import SCHEMA_VERSION
from perdeviceeq.profiles import ProfileStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles_mod, "USER_PROFILES_DIR",
                        str(tmp_path / "profiles"))
    monkeypatch.setattr(profiles_mod, "SYS_PROFILE_DIRS", [])
    monkeypatch.setattr(profiles_mod, "CONFIG_DIR",
                        str(tmp_path / "cfg"))
    monkeypatch.setattr(profiles_mod, "BINDINGS_FILE",
                        str(tmp_path / "cfg" / "bindings.json"))
    return ProfileStore()


def _profile():
    return {
        "id": "abc123def456", "name": "TANCHJIM ORIGIN",
        "version": SCHEMA_VERSION, "apply_all": False,
        "preamp": -16.2, "ch_keys": ["FL", "FR"],
        "all": {"bands": []},
        "channels": {
            "FL": {"bands": [{"type": "PK", "freq": 2985.0,
                              "gain": -14.35, "q": 0.696,
                              "enabled": True}]},
            "FR": {"bands": [{"type": "LSC", "freq": 27.7,
                              "gain": 5.89, "q": 0.429,
                              "enabled": True}]}},
        "provenance": {"kind": "measured"},
        "device": {"label": "IL-DSP Analog Stereo"},
        "fit": {"algo": "greedy+refine", "target": "flat",
                "at": "2026-07-19T12:00:00Z",
                "takes": ["t1", "t2"], "edited": False},
        "measurement": {
            "grid": {"f_lo": 20.0, "f_hi": 20000.0, "ppo": 96},
            "sessions": [{"id": "s1"}],
            "takes": [{"id": "t1", "channel": "FL"},
                      {"id": "t2", "channel": "FR"}],
            "source": {"name": "E.A.R.S Gain 0dB",
                       "cal": {"1": {"file": "R.txt",
                                     "sha256": "af45a026" + "0" * 56},
                               "2": {"file": "L.txt",
                                     "sha256": "bc56b137" + "0" * 56}}}},
        # runtime keys the packer must strip
        "builtin": False, "path": "/tmp/somewhere.json",
    }


def test_pack_is_deterministic_and_strips_runtime():
    p = _profile()
    t1 = pdeq.pdeq_pack(p)
    shuffled = dict(reversed(list(p.items())))
    t2 = pdeq.pdeq_pack(shuffled)
    assert t1 == t2
    assert "builtin" not in t1 and "/tmp/somewhere" not in t1
    # bare canonical body: the file's own sha256 IS the address
    body = json.loads(t1)
    assert body["version"] == SCHEMA_VERSION
    import hashlib
    assert (hashlib.sha256(t1.encode("utf-8")).hexdigest()
            == pdeq.payload_sha256(p))


def test_unpack_roundtrip_and_validation():
    p = _profile()
    text = pdeq.pdeq_pack(p)
    prof, sha = pdeq.pdeq_unpack(text)
    assert prof["name"] == "TANCHJIM ORIGIN"
    assert sha == pdeq.payload_sha256(prof)
    # a hand-edited file is not "corrupt": edited bytes simply ARE
    # a different package with a different computed address
    prof2, sha2 = pdeq.pdeq_unpack(
        text.replace("TANCHJIM", "TANCHJIN"))
    assert prof2["name"] == "TANCHJIN ORIGIN" and sha2 != sha
    with pytest.raises(ValueError, match="not JSON"):
        pdeq.pdeq_unpack("не пакет")
    with pytest.raises(ValueError, match="no schema version"):
        pdeq.pdeq_unpack(json.dumps({"profile": {}}))
    old = _profile()
    old["version"] = 2
    with pytest.raises(ValueError, match="schema v2"):
        pdeq.pdeq_pack(old)
    # foreign bodies inside a hand-built envelope: each direction
    # names its own way out
    def bare(version):
        return json.dumps(dict(_profile(), version=version))
    with pytest.raises(ValueError, match="newer build"):
        pdeq.pdeq_unpack(bare(SCHEMA_VERSION + 1))
    with pytest.raises(ValueError, match="migration tool"):
        pdeq.pdeq_unpack(bare(2))


def test_absorb_is_byte_stable_through_the_store(store):
    p = _profile()
    text = pdeq.pdeq_pack(p)
    pid, report = pdeq.absorb(store, text)
    assert pid == "abc123def456"          # id was free: kept
    again = pdeq.pdeq_pack(store.get(pid))
    assert again == text                  # export == the original
    # provenance untouched on the clean path
    assert "package_sha256" not in (store.get(pid)["provenance"])
    # importing the very same package again is a spoken no-op
    pid2, rep2 = pdeq.absorb(store, text)
    assert pid2 == pid
    assert any("nothing imported" in ln for ln in rep2)
    assert len([p for p in store.profiles.values()
                if not p.get("builtin")]) == 1


def test_absorb_remints_on_collision(store):
    p = _profile()
    store.save_user(dict(p, name="already here"))
    pid, _rep = pdeq.absorb(store, pdeq.pdeq_pack(p))
    assert pid != "abc123def456"
    prov = store.get(pid)["provenance"]
    assert prov["original_id"] == "abc123def456"
    assert prov["package_sha256"] == pdeq.payload_sha256(
        pdeq.pdeq_unpack(pdeq.pdeq_pack(p))[0])
    # both live side by side
    assert store.has("abc123def456") and store.has(pid)


def test_report_names_the_provenance():
    p = _profile()
    text = pdeq.pdeq_pack(p)
    prof, sha = pdeq.pdeq_unpack(text)
    lines = "\n".join(pdeq.package_report(prof, sha))
    assert "TANCHJIM ORIGIN" in lines
    assert "measured" in lines
    assert "IL-DSP Analog Stereo" in lines
    assert "greedy+refine" in lines and "2 takes" in lines
    assert "20-20000 Hz" in lines and "96 pts/oct" in lines
    assert "E.A.R.S" in lines
    assert "af45a026" in lines and "bc56b137" in lines
    assert sha[:16] in lines
    bare = {"id": "x", "name": "bare",
            "version": SCHEMA_VERSION, "preamp": 0.0,
            "apply_all": True, "ch_keys": [],
            "all": {"bands": []}, "channels": {}}
    blines = "\n".join(pdeq.package_report(
        bare, pdeq.payload_sha256(bare)))
    assert "bands only" in blines


def test_package_text_is_not_eq_text():
    """The wrong-import field incident: a .pdeq fed to the EQ-text
    parser must yield zero bands, so the GUI guard refuses instead
    of applying emptiness to the current slot."""
    from perdeviceeq import eq
    pre, bands = eq.parse_autoeq(pdeq.pdeq_pack(_profile()))
    assert bands == [] and pre == 0.0
