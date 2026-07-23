"""audit_headroom's app-profile support (Task 2 tier 3): schema conversion
(band key "enabled" -> "on", per-slot preamps, linked "all" replication),
--profile NAME resolution against the app's saved profiles, the 0.1 dB
ceil on suggestions, and byte-stable --demo output."""
import json

import numpy as np
import pytest
import soundfile as sf

import audit_headroom as ah


def test_app_bands_key_conversion():
    slot = {"bands": [
        {"type": "PK", "freq": 100, "gain": 3, "q": 2, "enabled": False},
        {"type": "LSC", "freq": 50, "gain": 1, "q": 0.7}]}
    out = ah._app_bands(slot)
    assert out[0]["on"] is False
    assert out[1]["on"] is True          # enabled defaults to True
    assert "enabled" not in out[0]


def test_normalize_three_modes():
    audit = {"preamp": -1.5, "channels": {"FL": [{"type": "PK", "freq": 1,
                                                  "gain": 0, "q": 1}]}}
    chains, meta = ah.normalize_profile(audit)
    assert meta["mode"] == "audit" and chains[0][1] == -1.5

    linked = {"apply_all": True, "preamp": -3.0, "all": {"bands": []}}
    chains, meta = ah.normalize_profile(linked)
    assert meta["mode"] == "all" and chains == [("all", -3.0, [])]

    per = {"apply_all": False, "preamp": -2.0, "ch_keys": ["FR", "FL"],
           "channels": {"FL": {"bands": []}, "FR": {"bands": []}}}
    chains, meta = ah.normalize_profile(per)
    assert meta["mode"] == "per-channel"
    assert [(k, g) for k, g, _ in chains] == [("FR", -2.0), ("FL", -2.0)]


def test_v1_app_profile_is_rejected():
    v1 = {"apply_all": False, "ch_keys": ["FL"],
          "channels": {"FL": {"preamp": -1.0, "bands": []}}}
    with pytest.raises(SystemExit):
        ah.normalize_profile(v1)


def test_resolve_profile_by_name(tmp_path):
    for pid, name in (("a1", "soundcore Liberty 5"), ("b2", "iLoud Micro")):
        (tmp_path / ("%s.json" % pid)).write_text(
            json.dumps({"id": pid, "name": name, "apply_all": True,
                        "version": 3, "preamp": 0.0, "all": {"bands": []}}))
    raw, path = ah.resolve_profile("soundcore liberty 5", str(tmp_path))
    assert raw["id"] == "a1"
    raw, path = ah.resolve_profile("micro", str(tmp_path))   # unique substring
    assert raw["id"] == "b2"
    with pytest.raises(SystemExit):
        ah.resolve_profile("o", str(tmp_path))               # ambiguous
    with pytest.raises(SystemExit):
        ah.resolve_profile("nope", str(tmp_path))            # missing


def test_ceil_db_grid():
    assert ah._ceil_db(1.41) == pytest.approx(1.5)
    assert ah._ceil_db(7.4869) == pytest.approx(7.5)
    assert ah._ceil_db(5.0) == pytest.approx(5.0)   # exact grid stays put
    assert ah._ceil_db(0.0) == 0.0


def test_main_app_profile_end_to_end(tmp_path, monkeypatch, capsys):
    """Stereo -6 dBFS sine at 1 kHz; FL boosts it +12 under a shared -1
    preamp (post ~ +5.0), FR is flat: the single shared suggestion must be
    set by the worst channel, -1 - 5.0 = -6.0."""
    fs = 48000
    t = np.arange(2 * fs) / fs
    sig = 10 ** (-6 / 20) * np.sin(2 * np.pi * 1000 * t)
    # Hann fade in/out: switching a sine into a resonant biquad overshoots
    # the steady-state envelope for a few cycles; the fade keeps the true
    # post-EQ peak at exactly the steady  -6 - 1 + 12 = +5.0 dBFS.
    n_f = int(0.1 * fs)
    ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(n_f) / n_f)
    sig[:n_f] *= ramp
    sig[-n_f:] *= ramp[::-1]
    wav = tmp_path / "cap.wav"
    sf.write(wav, np.column_stack([sig, sig]), fs, subtype="FLOAT")

    prof = {"id": "p1", "name": "Test Buds", "version": 3,
            "apply_all": False, "preamp": -1.0, "ch_keys": ["FL", "FR"],
            "channels": {"FL": {"bands": [
                             {"type": "PK", "freq": 1000, "gain": 12.0,
                              "q": 1.0, "enabled": True}]},
                         "FR": {"bands": []}}}
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    (pdir / "p1.json").write_text(json.dumps(prof))

    monkeypatch.setattr(ah, "PROFILES_DIR", str(pdir))
    monkeypatch.setattr(ah.sys, "argv", ["x", str(wav), "--profile", "buds"])
    ah.main()
    out = capsys.readouterr().out
    assert "profile: Test Buds (per-channel; preamps FL -1.0, FR -1.0)" in out
    assert "suggested preamp: -6.0 dB  (worst channel FL" in out


def test_demo_output_is_stable(fixtures_dir, monkeypatch, capsys):
    """The legacy --demo CLI is what the ROADMAP table and old notes quote;
    keep its recommendation line stable."""
    monkeypatch.setattr(
        ah.sys, "argv",
        ["x", str(fixtures_dir / "hot_master.wav"), "--demo"])
    ah.main()
    out = capsys.readouterr().out
    assert "recommended preamp: -7.5 dB" in out
    assert "profile:" not in out          # --demo header stays terse
