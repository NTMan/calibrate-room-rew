"""In-process tests for the MeasureSession single-take API (increment 4,
part 1 of the GTK wizard).

Same fake pw-* executables as the end-to-end CLI tests, but driven as a
library -- exactly what the wizard will do: construct a session, click a
"speaker" (take(channel)), watch the fan (takes_of/spread_db), throw a
bad take away (discard), finalize one result.json per channel. The CLI
contract itself is pinned by test_measure_run.py; this file pins the
API shapes and the accumulation semantics.
"""
import json
import os
from pathlib import Path

import numpy as np
import pytest

from perdeviceeq.pde_audit import DEMO_PROFILE, chain_curve
from perdeviceeq import measure_session as ms

ROOT = Path(__file__).resolve().parent.parent
SHIMS = ROOT / "tests" / "shims"
F_LO_CHECK, F_HI_CHECK = 40.0, 16000.0
TOL_DB = 0.5

# any non-trivial graph string; the session must treat it as opaque
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


def make_cfg(tmp_path, **kw):
    kw.setdefault("samples", 131072)      # 2.7 s sweep: enough for the
    return ms.SessionConfig(              # 0.5 dB closure, fast in CI
        sink="test_sink", source="test_source",
        save_dir=str(tmp_path / "takes"), **kw)


def assert_matches_chain(freqs, mag_db):
    ref = chain_curve(DEMO_PROFILE["channels"]["FL"], 48000,
                      np.asarray(freqs))
    band = (np.asarray(freqs) >= F_LO_CHECK) \
        & (np.asarray(freqs) <= F_HI_CHECK)
    err = np.asarray(mag_db, dtype=float)[band] - ref[band]
    err -= np.median(err)                 # absolute level is arbitrary
    assert np.max(np.abs(err)) < TOL_DB


# --- the fan lifecycle: take -> spread -> discard -> finalize --------------

def test_take_spread_discard_finalize(shim_state, tmp_path):
    ses = ms.MeasureSession(make_cfg(tmp_path))
    with ses:
        out1 = ses.take(0)
        assert out1.kind == "take"
        assert out1.take.id == 1
        assert out1.take.channel == 0
        assert out1.spread_db is None                 # one take, no fan yet
        assert out1.take.clipped == 0
        assert out1.take.repaired == 0
        # shim delay (~800 ms) + the wav's own 1.0 s pre-silence
        assert 1700.0 < out1.take.delay_ms < 1900.0
        assert os.path.basename(out1.take.wav_path) == "take01.wav"
        assert os.path.exists(out1.take.wav_path)
        assert_matches_chain(out1.take.freq_hz, out1.take.mag_db)
        assert ses.path_clean["verified"] is True
        assert ses.path_clean["capture"]["verified"] is True

        out2 = ses.take(0)
        assert out2.take.id == 2
        assert abs(out2.take.delay_ms - out1.take.delay_ms) < 2.0
        spread = out2.spread_db
        assert spread is not None and len(spread) == len(ses.freqs)
        assert np.all(np.isfinite(spread))
        assert float(np.max(spread)) < 0.5            # synthetic: tiny fan

        # the profile was bypassed DURING the sound
        snap = json.loads((shim_state / "meta_at_play_1.json").read_text())
        assert "test_sink" not in snap

        dropped = ses.discard(0, out1.take.id)
        assert dropped.id == 1
        assert [r.id for r in ses.takes_of(0)] == [2]
        assert ses.spread_db(0) is None               # fan collapsed
        assert os.path.exists(dropped.wav_path)       # evidence stays

        out3 = ses.take(0)
        assert out3.take.id == 3                      # ids never reused
        assert os.path.basename(out3.take.wav_path) == "take03.wav"
        assert out3.spread_db is not None

    # bypass restored verbatim on exit
    assert json.loads((shim_state / "metadata.json").read_text()) \
        == {"test_sink": GRAPH}
    assert ses.eq_state == {"metadata_key": "test_sink", "profile": GRAPH,
                            "profile_source": "metadata", "bypass": True,
                            "restored": True}

    out_json = tmp_path / "result.json"
    r = ses.finalize(0, str(out_json))
    assert out_json.exists()
    assert r["schema"] == "pde-measurement"
    assert r["takes"]["count"] == 2                   # takes 2 and 3
    assert len(r["levels"]["capture_peak_dbfs"]) == 2
    assert r["levels"]["sink_volume"] == pytest.approx(0.30, abs=1e-3)
    assert r["levels"]["auto_level"]["enabled"] is False
    assert_matches_chain(r["data"]["freq_hz"], r["data"]["mag_db_raw"])
    # the sink volume was never written
    assert not (shim_state / "volume_log.json").exists()


# --- one session, both ears: the analyzed channel is a take argument -------

def test_two_channels_accumulate_side_by_side(shim_state, tmp_path):
    ses = ms.MeasureSession(make_cfg(tmp_path, channels=2))
    with ses:
        left = ses.take(0)
        right = ses.take(1)
    assert [r.id for r in ses.takes_of(0)] == [left.take.id] == [1]
    assert [r.id for r in ses.takes_of(1)] == [right.take.id] == [2]
    out_l = tmp_path / "left.json"
    out_r = tmp_path / "right.json"
    rl = ses.finalize(0, str(out_l))
    rr = ses.finalize(1, str(out_r))
    assert out_l.exists() and out_r.exists()
    for r in (rl, rr):
        assert r["takes"]["count"] == 1
        assert len(r["levels"]["capture_peak_dbfs"]) == 1
        assert_matches_chain(r["data"]["freq_hz"], r["data"]["mag_db_raw"])


# --- auto-level: probes move the volume and are not accumulated ------------

def test_auto_level_probes_not_accumulated(shim_state, tmp_path):
    ses = ms.MeasureSession(make_cfg(tmp_path, samples=65536,
                                     auto_level=True))
    kinds = []
    with ses:
        for _ in range(ms.AUTO_MAX_ADJUST + 2):
            out = ses.take(0)
            kinds.append(out.kind)
            if out.kind == "level_probe":
                assert out.level["volume_to"] > out.level["volume_from"]
            if out.kind == "take":
                break
    assert kinds[-1] == "take"
    assert "level_probe" in kinds                     # started too quiet
    assert [r.id for r in ses.takes_of(0)] == [out.take.id]
    assert ms.AUTO_WINDOW[0] <= out.take.peak_dbfs <= ms.AUTO_WINDOW[1]
    r = ses.finalize(0, str(tmp_path / "result.json"))
    auto = r["levels"]["auto_level"]
    assert auto["enabled"] is True
    assert auto["in_window"] is True
    assert auto["initial"] == pytest.approx(ms.AUTO_START_VOLUME, abs=1e-3)
    assert 1 <= auto["adjustments"] <= ms.AUTO_MAX_ADJUST
    vol = json.loads((shim_state / "volume.json").read_text())["cubic"]
    assert vol == pytest.approx(auto["final"], abs=1e-3)
    assert vol > 0.30                                 # allowed to exceed


def test_level_stuck_is_a_caller_decision(shim_state, tmp_path,
                                           monkeypatch):
    # no adjustments allowed: the very first too-quiet probe is "stuck"
    monkeypatch.setattr(ms, "AUTO_MAX_ADJUST", 0)
    ses = ms.MeasureSession(make_cfg(tmp_path, samples=65536,
                                     auto_level=True))
    with ses:
        with pytest.raises(ms.RefusalError):
            ses.take(5)                               # out of range: no
        out = ses.take(0)                             # sound was played
        assert out.kind == "level_stuck"
        assert out.level["why"] == "0 adjustments"
        assert any("gave up" in n for n in out.notes)
        assert ses.takes_of(0) == []                  # nothing accumulated
        kept = ses.accept_level()                     # "continue anyway"
        assert kept.kind == "take"
        assert kept.take.peak_dbfs == out.level["peak_dbfs"]
        assert [r.id for r in ses.takes_of(0)] == [kept.take.id]
        with pytest.raises(ms.MeasureError):
            ses.accept_level()                        # nothing pending now
    r = ses.finalize(0, str(tmp_path / "result.json"))
    auto = r["levels"]["auto_level"]
    assert auto["in_window"] is False
    assert auto["final"] == pytest.approx(ms.AUTO_START_VOLUME, abs=1e-3)


# --- refusals happen before any sound ---------------------------------------

def test_foreign_stream_refuses_in_the_constructor(shim_state, tmp_path,
                                                   monkeypatch):
    monkeypatch.setenv("PDE_SHIM_FOREIGN", "1")
    with pytest.raises(ms.RefusalError, match="firefox"):
        ms.MeasureSession(make_cfg(tmp_path))
    assert not (shim_state / "played.json").exists()  # nothing played
    assert json.loads((shim_state / "metadata.json").read_text()) \
        == {"test_sink": GRAPH}                       # bypass never engaged

    monkeypatch.setenv("PDE_SHIM_FOREIGN", "0")
    ses = ms.MeasureSession(make_cfg(tmp_path))
    with pytest.raises(ms.MeasureError, match="not entered"):
        ses.take(0)                                   # guard: no `with` yet
    assert not (shim_state / "played.json").exists()
