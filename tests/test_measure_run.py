"""End-to-end tests for the real-path runner (ROADMAP Task 3, incr. 2).

No hardware: tools/measure_run.py runs UNMODIFIED as a subprocess against
the fake PipeWire CLI tools in tests/shims/ (put first on PATH). The fake
pw-record answers with the deposited sweep processed through the known
DEMO_PROFILE FL chain, so the whole orchestration -- confirmation, foreign
stream policy, profile bypass, path verification, capture, core
processing -- closes against pde_audit.chain_curve().

The absolute level of a real measurement is arbitrary (mic sensitivity,
sink volume), so the curve comparison removes the median offset first;
the SHAPE must match within the core tolerance (same 0.5 dB, 40 Hz -
16 kHz as test_measure_core -- in synthetics there is no room, only
math).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from pde_audit import DEMO_PROFILE, chain_curve
import measure_run as mr

ROOT = Path(__file__).resolve().parent.parent
SHIMS = ROOT / "tests" / "shims"
F_LO_CHECK, F_HI_CHECK = 40.0, 16000.0
TOL_DB = 0.5

# any non-trivial graph string; the runner must treat it as opaque
GRAPH = ("{ nodes = [ { type = builtin name = eq label = param_eq "
         "config = { filters = [ { type = bq_peaking, freq = 200, "
         "gain = 9.6, q = 2.25 } ] } } ] }")


def run_measure(tmp_path, extra=(), env_extra=None, input_text="",
                seed_meta=True):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    if seed_meta:
        (state / "metadata.json").write_text(
            json.dumps({"test_sink": GRAPH}))
    (state / "volume.json").write_text(json.dumps({"cubic": 0.30}))
    env = os.environ.copy()
    env["PDE_SHIM_DIR"] = str(state)
    env["PDE_SHIM_REPO"] = str(ROOT)
    env["PDE_SHIM_PLAY_SECONDS"] = "0.9"
    env["XDG_STATE_HOME"] = str(tmp_path / "xdg-state")   # isolate WpState
    env["PATH"] = "%s%s%s" % (SHIMS, os.pathsep, env["PATH"])
    env.update(env_extra or {})
    out = tmp_path / "result.json"
    cmd = [sys.executable, str(ROOT / "tools" / "measure_run.py"),
           "--sink", "test_sink", "--source", "test_source", "--yes",
           "--save-dir", str(tmp_path / "takes"), "--out", str(out),
           *extra]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                          input=input_text, timeout=300)
    return proc, out, state


def meta_now(state):
    return json.loads((state / "metadata.json").read_text())


# --- the happy path: orchestration closes against the known chain --------

def test_end_to_end_two_takes(tmp_path):
    proc, out, state = run_measure(tmp_path, ["--takes", "2"],
                                   input_text="\n")
    assert proc.returncode == 0, proc.stderr

    r = json.loads(out.read_text())
    assert r["schema"] == "pde-measurement"
    assert r["takes"]["count"] == 2
    assert r["takes"]["delay_jitter_ms"] < 2.0        # no BT warning
    assert r["foreign_streams"] == []

    # bypass: found in the metadata -> cleared for the sound -> restored
    eq = r["eq_profile_state"]
    assert eq == {"metadata_key": "test_sink", "profile": GRAPH,
                  "profile_source": "metadata", "bypass": True,
                  "restored": True}
    assert meta_now(state) == {"test_sink": GRAPH}
    for n in (1, 2):
        snap = json.loads((state / ("meta_at_play_%d.json" % n))
                          .read_text())
        assert "test_sink" not in snap                # silent during sound

    # path verification: direct link into the real device, nothing else,
    # and the capture pinned to the requested source
    pc = r["path_clean"]
    assert pc["verified"] is True
    assert pc["target"]["name"] == "test_sink"
    assert pc["target"]["device_api"] == "alsa"
    assert pc["unknown_nodes"] == []
    assert pc["capture"]["verified"] is True
    assert pc["capture"]["source"]["name"] == "test_source"

    # levels: sink volume untouched, stream volume forced/verified 1.0
    lv = r["levels"]
    assert lv["sweep_level_dbfs"] == -6.0
    assert lv["stream_volume"] == 1.0
    assert lv["sink_volume"] == pytest.approx(0.30, abs=1e-3)
    assert lv["sink_volume_start"] == pytest.approx(0.30, abs=1e-3)
    assert lv["auto_level"]["enabled"] is False
    assert len(lv["capture_peak_dbfs"]) == 2

    # raw artifacts land in the fixtures-local layout
    outdir = next((tmp_path / "takes").iterdir())
    names = {p.name for p in outdir.iterdir()}
    assert {"sweep.wav", "sweep.wav.json", "sweep-inverse.wav",
            "take01.wav", "take02.wav"} <= names

    # the measured curve is the chain, up to a constant gain
    freqs = np.asarray(r["data"]["freq_hz"])
    raw = np.asarray(r["data"]["mag_db_raw"], dtype=float)
    ref = chain_curve(DEMO_PROFILE["channels"]["FL"], r["sample_rate"],
                      freqs)
    band = (freqs >= F_LO_CHECK) & (freqs <= F_HI_CHECK)
    err = raw[band] - ref[band]
    err -= np.median(err)
    assert np.max(np.abs(err)) < TOL_DB


# --- foreign streams -------------------------------------------------------

def test_foreign_stream_refuses_before_any_sound(tmp_path):
    proc, out, state = run_measure(tmp_path,
                                   env_extra={"PDE_SHIM_FOREIGN": "1"})
    assert proc.returncode == 2
    assert "firefox" in proc.stderr
    assert "--mute-others" in proc.stderr
    assert not out.exists()
    assert not (state / "played.json").exists()       # nothing played
    assert meta_now(state) == {"test_sink": GRAPH}    # bypass never engaged


def test_mute_others_corks_and_restores(tmp_path):
    proc, out, state = run_measure(tmp_path, ["--mute-others"],
                                   env_extra={"PDE_SHIM_FOREIGN": "1"})
    assert proc.returncode == 0, proc.stderr
    log = json.loads((state / "muted_log.json").read_text())
    assert log[0] == {"id": "70", "mute": True}
    assert log[-1] == {"id": "70", "mute": False}     # prior state restored
    r = json.loads(out.read_text())
    assert r["foreign_streams"] == [
        {"id": 70, "node_name": "firefox", "app": "Firefox",
         "prior_mute": False, "muted_for_measure": True}]


# --- bypass restore on a mid-measure failure -------------------------------

def test_profile_restored_when_playback_fails(tmp_path):
    proc, out, state = run_measure(tmp_path,
                                   env_extra={"PDE_SHIM_PLAY_FAIL": "1"})
    assert proc.returncode == 1
    assert "pw-play failed" in proc.stderr
    assert not out.exists()
    # the key was cleared for the (failed) sound...
    snap = json.loads((state / "meta_at_play_1.json").read_text())
    assert "test_sink" not in snap
    # ...and put back verbatim by the context exit
    assert meta_now(state) == {"test_sink": GRAPH}


# --- auto-level -------------------------------------------------------------

def test_auto_level_starts_quiet_and_lands_in_window(tmp_path):
    proc, out, state = run_measure(tmp_path, ["--auto-level"])
    assert proc.returncode == 0, proc.stderr
    r = json.loads(out.read_text())
    auto = r["levels"]["auto_level"]
    assert auto["enabled"] is True
    assert auto["initial"] == pytest.approx(mr.AUTO_START_VOLUME, abs=1e-3)
    assert 1 <= auto["adjustments"] <= mr.AUTO_MAX_ADJUST
    assert auto["in_window"] is True
    pk = r["levels"]["capture_peak_dbfs"][-1]
    assert mr.AUTO_WINDOW[0] <= pk <= mr.AUTO_WINDOW[1]
    vol = json.loads((state / "volume.json").read_text())["cubic"]
    assert vol == pytest.approx(auto["final"], abs=1e-3)
    assert vol > 0.30                       # allowed to exceed the start
    log = json.loads((state / "volume_log.json").read_text())
    assert log[0]["cubic"] == pytest.approx(mr.AUTO_START_VOLUME, abs=1e-3)


def test_auto_level_converges_on_a_nonlinear_gain(tmp_path):
    # a BT-like law so steep near the top that the target window is only
    # reached ABOVE the explore ceiling (Mikhail's Liberty 5): the soft
    # ceiling must lift and the run converge, not stall at the ceiling
    proc, out, state = run_measure(tmp_path, ["--auto-level"],
                                   env_extra={"PDE_SHIM_GAIN_EXP": "9"})
    assert proc.returncode == 0, proc.stderr
    r = json.loads(out.read_text())
    assert r["levels"]["auto_level"]["in_window"] is True
    pk = r["levels"]["capture_peak_dbfs"][-1]
    assert mr.AUTO_WINDOW[0] <= pk <= mr.AUTO_WINDOW[1]
    # the ceiling lifted past its start (the device needed more than 80%)
    assert r["levels"]["auto_level"]["final"] > mr.AUTO_EXPLORE_CEIL
    # the accepted level must not be a clipped one
    outdir = next((tmp_path / "takes").iterdir())
    import soundfile as sf
    x, _ = sf.read(str(outdir / "take01.wav"), always_2d=True)
    assert float(np.max(np.abs(x[:, 0]))) < mr.FULLSCALE


def test_without_auto_level_volume_is_never_written(tmp_path):
    proc, out, state = run_measure(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert not (state / "volume_log.json").exists()
    assert json.loads((state / "volume.json").read_text())["cubic"] == 0.30


# --- capture sanity: a dropout is repaired, a fault aborts -----------------

def test_raw_capture_dump_keeps_the_pre_repair_capture(tmp_path):
    # --raw-capture-dump keeps the untouched capture (glitch evidence)
    # while the saved take is post-repair and clean
    proc, out, state = run_measure(
        tmp_path, ["--takes", "1", "--raw-capture-dump"],
        env_extra={"PDE_SHIM_NAN_CH": "0", "PDE_SHIM_NAN_AT_START": "1"})
    assert proc.returncode == 0, proc.stderr
    import soundfile as sf
    outdir = next((tmp_path / "takes").iterdir())
    take, _ = sf.read(str(outdir / "take01.wav"), always_2d=True)
    raw, _ = sf.read(str(outdir / "raw01.wav"), always_2d=True)
    assert np.isfinite(take[:, 0]).all()          # analyzed take is clean
    assert not np.isfinite(raw[:, 0]).all()        # raw keeps the glitch


def test_capture_uses_raw_flag(tmp_path):
    # pw-record MUST be invoked with --raw, or its stdout carries a format
    # header that decodes to a NaN at the start of channel 0
    proc, out, state = run_measure(tmp_path, ["--takes", "1"])
    assert proc.returncode == 0, proc.stderr
    assert json.loads((state / "raw_flag.json").read_text())["raw"] is True


def test_glitch_on_unanalyzed_channel_is_reported(tmp_path):
    # a mid-stream glitch on the channel we are NOT analyzing must still be
    # surfaced (the diagnostic scans all channels), without failing the run
    proc, out, state = run_measure(
        tmp_path, ["--takes", "1", "--channels", "2", "--channel", "0"],
        env_extra={"PDE_SHIM_NAN_CH": "1"})   # mid-stream NaN on channel 1
    assert proc.returncode == 0, proc.stderr
    assert "channel 1" in proc.stderr


def test_isolated_dropout_is_repaired(tmp_path):
    # one NaN sample (a capture xrun) must be interpolated, not fatal,
    # and the saved take must come out finite
    proc, out, state = run_measure(tmp_path, ["--takes", "1"],
                                   env_extra={"PDE_SHIM_NAN_CH": "0"})
    assert proc.returncode == 0, proc.stderr
    assert "interpolated" in proc.stderr
    assert json.loads(out.read_text())["takes"]["count"] == 1
    outdir = next((tmp_path / "takes").iterdir())
    import soundfile as sf
    x, _ = sf.read(str(outdir / "take01.wav"), always_2d=True)
    assert np.isfinite(x[:, 0]).all()


def test_faulty_channel_aborts(tmp_path):
    # a flood of non-finite samples is a broken input, not a glitch:
    # the run stops rather than interpolating over a real hole
    proc, out, state = run_measure(
        tmp_path, env_extra={"PDE_SHIM_NAN_CH": "0",
                             "PDE_SHIM_NAN_COUNT": "5000"})
    assert proc.returncode == 1
    assert "faulty" in proc.stderr
    assert "--channel" in proc.stderr
    assert not out.exists()
    assert meta_now(state) == {"test_sink": GRAPH}    # profile restored
    # the healthy channel is still usable
    proc, out, _ = run_measure(
        tmp_path, extra=["--channels", "2", "--channel", "1"],
        env_extra={"PDE_SHIM_NAN_CH": "0", "PDE_SHIM_NAN_COUNT": "5000"})
    assert proc.returncode == 0, proc.stderr


def test_full_scale_capture_is_flagged_clipped(tmp_path):
    proc, out, state = run_measure(tmp_path,
                                   env_extra={"PDE_SHIM_CLIP": "1.05"})
    assert proc.returncode == 0, proc.stderr
    assert "clipped" in proc.stderr
    assert "unusable" in proc.stderr


def test_hot_but_unclipped_is_only_advisory(tmp_path):
    # peak near, but below, full scale: no clipping, just low headroom
    proc, out, state = run_measure(tmp_path,
                                   env_extra={"PDE_SHIM_CLIP": "0.97"})
    assert proc.returncode == 0, proc.stderr
    assert "headroom" in proc.stderr
    assert "clipped" not in proc.stderr
    pk = json.loads(out.read_text())["levels"]["capture_peak_dbfs"][0]
    assert mr.HOT_DBFS <= pk < 0.0


# --- capture path: a hijacked source aborts, doesn't record garbage --------

def test_capture_wrong_source_aborts(tmp_path):
    # a wrong default recording device grabs the capture stream: the run
    # must abort, not silently record the wrong (quiet) mic
    proc, out, state = run_measure(
        tmp_path, env_extra={"PDE_SHIM_CAPTURE_WRONG": "1"})
    assert proc.returncode == 1
    assert "wrong source" in proc.stderr
    assert "obsbot_wrong_default" in proc.stderr
    assert not out.exists()
    assert meta_now(state) == {"test_sink": GRAPH}    # profile restored


def test_playback_pinned_when_default_changes(tmp_path):
    # a second sink became the GNOME default mid-session: an unpinned
    # playback stream would follow it and sprawl onto both sinks (a sweep
    # into headphones AND speakers is not a measurement). node.target +
    # dont-reconnect must keep it on the target, and the run must pass.
    proc, out, state = run_measure(
        tmp_path, ["--takes", "1"], env_extra={"PDE_SHIM_OTHER_SINK": "1"})
    assert proc.returncode == 0, proc.stderr
    pc = json.loads(out.read_text())["path_clean"]
    assert pc["verified"] is True
    assert pc["unknown_nodes"] == []              # did not sprawl to default
    assert pc["target"]["name"] == "test_sink"


# --- no profile bound: nothing to bypass, nothing to restore ---------------

def test_clean_sink_needs_no_bypass(tmp_path):
    proc, out, state = run_measure(tmp_path, seed_meta=False)
    assert proc.returncode == 0, proc.stderr
    eq = json.loads(out.read_text())["eq_profile_state"]
    assert eq["profile"] is None
    assert eq["profile_source"] is None
    assert eq["bypass"] is False
    assert eq["restored"] is None


# --- persisted-only profile (cold boot, GUI unopened): seed from WpState ----

def test_bypass_seeds_from_wpstate_when_metadata_empty(tmp_path):
    # the hook applies a persisted profile without publishing it to the
    # metadata; bypass must still find it (in WpState) and restore it
    wp = tmp_path / "xdg-state" / "wireplumber"
    wp.mkdir(parents=True)
    (wp / "per-device-eq").write_text(
        "[per-device-eq]\ntest_sink=%s\n" % GRAPH)
    proc, out, state = run_measure(tmp_path, seed_meta=False)
    assert proc.returncode == 0, proc.stderr
    eq = json.loads(out.read_text())["eq_profile_state"]
    assert eq["profile"] == GRAPH
    assert eq["profile_source"] == "wpstate"
    assert eq["bypass"] is True
    assert eq["restored"] is True
    # restore re-publishes it to the metadata
    assert meta_now(state) == {"test_sink": GRAPH}


# --- pure helpers: the auto-level controller safety properties --------------

def test_autolevel_steps_up_but_never_blasts_when_quiet():
    ac = mr.AutoLevel()
    ac.observe(0.15, -45.0, False)
    nv = ac.next_volume(0.15, -45.0)
    assert nv > 0.15                                 # move toward the target
    assert nv <= 0.15 * mr.AUTO_MAX_STEP             # bounded per step
    assert nv <= mr.AUTO_EXPLORE_CEIL                # no full-volume probe


def test_autolevel_brackets_and_stays_below_the_loud_side():
    ac = mr.AutoLevel()
    ac.observe(0.2, -20.0, False)                    # below the window
    ac.observe(0.8, 0.0, True)                       # clipped -> loud bracket
    nv = ac.next_volume(0.8, 0.0)
    assert 0.2 < nv < 0.8                            # interpolated inside
    assert nv <= 0.8 * mr.AUTO_CLIP_BACKOFF          # kept below the clip


def test_autolevel_never_returns_to_a_clipping_level():
    ac = mr.AutoLevel()
    ac.observe(0.3, -30.0, False)
    ac.observe(1.0, 0.5, True)
    assert ac.next_volume(1.0, 0.5) <= 1.0 * mr.AUTO_CLIP_BACKOFF


def test_autolevel_ceiling_lifts_when_stuck_below_window():
    # a probe sitting at the explore ceiling but still below the window
    # means the device needs more: the ceiling must lift past its start
    ac = mr.AutoLevel()
    ac.observe(mr.AUTO_EXPLORE_CEIL, -20.0, False)   # at ceiling, too quiet
    nv = ac.next_volume(mr.AUTO_EXPLORE_CEIL, -20.0)
    assert nv > mr.AUTO_EXPLORE_CEIL                 # allowed to go higher now


def test_autolevel_uses_the_measured_slope():
    # two points 6 dB apart over 0.3 decades -> ~20 dB/decade, much shallower
    # than the 60 the old cube model assumed; the step must reflect that
    ac = mr.AutoLevel()
    ac.observe(0.1, -40.0, False)
    ac.next_volume(0.1, -40.0)                        # seeds prev
    ac.observe(0.2, -34.0, False)
    nv = ac.next_volume(0.2, -34.0)
    assert nv > 0.2
    assert nv <= 0.2 * mr.AUTO_MAX_STEP               # still bounded/safe


# --- the glitch probe imports and parses (hardware tool, smoke only) --------

def test_capture_glitch_probe_help():
    import subprocess
    r = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "capture_glitch_probe.py"),
         "--help"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    assert "glitch" in r.stdout.lower()


def test_capture_glitch_probe_runs_against_shims(tmp_path):
    # the probe must actually EXECUTE end-to-end: the --help smoke once
    # hid an AttributeError on a constant that never shipped in
    # measure_run (the pre---raw warmup-drop draft)
    import soundfile as sf
    import measure_core as mc
    state = tmp_path / "state"
    state.mkdir()
    (state / "volume.json").write_text(json.dumps({"cubic": 0.30}))
    env = os.environ.copy()
    env["PDE_SHIM_DIR"] = str(state)
    env["PDE_SHIM_REPO"] = str(ROOT)
    env["PDE_SHIM_PLAY_SECONDS"] = "0.5"
    env["PDE_SHIM_NAN_CH"] = "0"          # one mid-stream NaN: a real
    env["PATH"] = "%s%s%s" % (SHIMS, os.pathsep, env["PATH"])   # dropout
    sw = mc.generate_sweep(16384)
    wav = tmp_path / "sweep.wav"
    sf.write(str(wav), sw.signal.astype("float32"), sw.fs, subtype="FLOAT")
    r = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "capture_glitch_probe.py"),
         "--source", "test_source", "--sink", "test_sink",
         "--sweep", str(wav), "--runs", "1", "--seconds", "1.5"],
        capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "1/1 runs had non-finite samples" in r.stdout
    assert "dropout" in r.stdout
