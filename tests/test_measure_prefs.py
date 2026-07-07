"""Tests for the measurement preference stores (mic profiles, per-sink
recall). The CONFIG_DIR paths are redirected into tmp so the real
~/.config is never touched.
"""
import pytest

from perdeviceeq import measure_prefs as mp


@pytest.fixture
def paths(tmp_path, monkeypatch):
    micf = tmp_path / "mic-profiles.json"
    memf = tmp_path / "measure-state.json"
    monkeypatch.setattr(mp, "MIC_PROFILES_FILE", str(micf))
    monkeypatch.setattr(mp, "MEASURE_STATE_FILE", str(memf))
    return micf, memf


# --- mic profiles ----------------------------------------------------------

def test_mic_profile_roundtrip(paths):
    micf, _ = paths
    s = mp.MicProfileStore()
    assert s.ordered() == []
    pid = s.save({"name": "miniDSP EARS", "serial": "860-3052",
                  "node_match": "alsa_input.usb-miniDSP_ears",
                  "cal": {"0": "/c/L_RAW.txt", "1": "/c/R_RAW.txt"}})
    assert micf.exists()
    s2 = mp.MicProfileStore()                    # reload from disk
    p = s2.get(pid)
    assert p["name"] == "miniDSP EARS"
    assert p["serial"] == "860-3052"
    assert s2.cal_for(pid, 0) == "/c/L_RAW.txt"
    assert s2.cal_for(pid, 1) == "/c/R_RAW.txt"
    assert s2.cal_for(pid, 2) is None            # unmapped channel
    assert s2.match("alsa_input.usb-miniDSP_ears")["id"] == pid
    assert s2.match("some_other_mic") is None


def test_mic_profile_overwrite_same_id(paths):
    s = mp.MicProfileStore()
    pid = s.save({"name": "first", "cal": {"0": "/a.txt"}})
    s.save({"id": pid, "name": "renamed", "cal": {"0": "/b.txt"}})
    assert len(mp.MicProfileStore().profiles) == 1
    assert mp.MicProfileStore().get(pid)["name"] == "renamed"
    assert mp.MicProfileStore().cal_for(pid, 0) == "/b.txt"


def test_mic_profile_delete(paths):
    s = mp.MicProfileStore()
    pid = s.save({"name": "x"})
    assert s.delete(pid) is True
    assert s.get(pid) is None
    assert mp.MicProfileStore().get(pid) is None
    assert s.delete("nope") is False


def test_mic_profile_store_survives_junk(paths):
    micf, _ = paths
    micf.write_text("this is not json")
    s = mp.MicProfileStore()                     # must not raise
    assert s.ordered() == []
    pid = s.save({"name": "ok"})                 # and still writable
    assert mp.MicProfileStore().get(pid)["name"] == "ok"


# --- per-sink recall -------------------------------------------------------

def test_measure_memory_per_sink_and_source(paths):
    _, memf = paths
    m = mp.MeasureMemory()
    assert m.mic_for("sink_a") is None
    assert m.volume_for("sink_a", "srcA") is None
    m.remember("sink_a", mic_profile="mic1", source="srcA", volume=0.62)
    assert memf.exists()
    m2 = mp.MeasureMemory()
    assert m2.mic_for("sink_a") == "mic1"
    assert m2.volume_for("sink_a", "srcA") == pytest.approx(0.62)
    # volume is per source: another mic on the same sink is separate
    assert m2.volume_for("sink_a", "srcB") is None
    m2.remember("sink_a", source="srcB", volume=0.40)
    m3 = mp.MeasureMemory()
    assert m3.volume_for("sink_a", "srcA") == pytest.approx(0.62)
    assert m3.volume_for("sink_a", "srcB") == pytest.approx(0.40)
    assert m3.mic_for("sink_a") == "mic1"      # unchanged by volume writes
    # sinks are independent
    assert m3.volume_for("sink_b", "srcA") is None
    # re-level drops just that pair's volume
    m3.forget_volume("sink_a", "srcA")
    m4 = mp.MeasureMemory()
    assert m4.volume_for("sink_a", "srcA") is None
    assert m4.volume_for("sink_a", "srcB") == pytest.approx(0.40)
    m4.forget("sink_a")
    assert mp.MeasureMemory().mic_for("sink_a") is None


def test_measure_memory_ignores_junk(paths):
    _, memf = paths
    memf.write_text("not json")
    m = mp.MeasureMemory()                        # must not raise
    assert m.mic_for("x") is None
    m.remember("x", mic_profile="m")             # and still writable
    assert mp.MeasureMemory().mic_for("x") == "m"


def test_mic_profile_channels_roundtrip(paths):
    s = mp.MicProfileStore()
    pid = s.save({"name": "Umik", "node_match": "umik.0",
                  "cal": {"0": "/c/umik.txt"}, "channels": 1})
    assert s.get(pid)["channels"] == 1
    s2 = mp.MicProfileStore()                    # reload from disk
    assert s2.get(pid)["channels"] == 1


def test_mic_profile_channels_defaults_none(paths):
    s = mp.MicProfileStore()
    pid = s.save({"name": "Ears", "node_match": "ears.0",
                  "cal": {"0": "/c/l.txt", "1": "/c/r.txt"}})
    assert s.get(pid)["channels"] is None      # unset -> auto
    pid2 = s.save({"name": "Bad", "node_match": "bad.0",
                   "cal": {}, "channels": 5})   # invalid -> None
    assert s.get(pid2)["channels"] is None
