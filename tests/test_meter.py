"""Tier-2 meter engine: the block-streamed filter path must equal the
offline audit exactly (same biquads, state carried across blocks), the
ballistics must behave (instant attack, constant decay, clip latch), and
the worker must survive a real pipe end to end."""
import os
import threading

import numpy as np
import pytest
import soundfile as sf

from perdeviceeq.pde_audit import DEMO_PROFILE, apply_chain, dbfs
from perdeviceeq import eq
from perdeviceeq.meter import BLOCK, Ballistics, MeterEngine, chain_sos


def demo_bands(ch):
    return [eq.Band(d["type"], d["freq"], d["gain"], d["q"],
                    d.get("on", True))
            for d in DEMO_PROFILE["channels"][ch]]


def test_block_streaming_equals_offline_audit(fixtures_dir):
    """Chunked sosfilt with carried zi == the audit's full-file cascade:
    the live meter and audit_headroom must be the same instrument."""
    x, fs = sf.read(fixtures_dir / "hot_master.wav", dtype="float64",
                    always_2d=True)
    bands = demo_bands("FR")
    ref = apply_chain(x[:, 1], DEMO_PROFILE["channels"]["FR"], fs, -7.5)

    sos = chain_sos(bands, fs)
    zi = np.zeros((sos.shape[0], 2))
    gain = 10 ** (-7.5 / 20)
    out = np.empty_like(ref)
    from scipy import signal as sg
    for off in range(0, len(ref), BLOCK):
        chunk = x[off:off + BLOCK, 1] * gain
        y, zi = sg.sosfilt(sos, chunk, zi=zi)
        out[off:off + len(y)] = y
    assert np.abs(out - ref).max() < 1e-9
    assert dbfs(out) == pytest.approx(dbfs(ref), abs=1e-9)


def test_ballistics_attack_decay_latch():
    b = Ballistics()
    bar, latched = b.update(0.0, -3.0)
    assert bar == -3.0 and not latched          # instant attack
    bar, _ = b.update(0.5, -60.0)
    assert bar == pytest.approx(-13.0)          # 20 dB/s decay
    bar, _ = b.update(0.5 + 2.85, -60.0)
    assert bar == pytest.approx(-60.0)          # clamped at the floor
    bar, latched = b.update(4.0, +1.5, clipped=132)
    assert bar == 1.5 and latched and b.clip_total == 132
    _, latched = b.update(5.9, -60.0)
    assert latched                              # latch holds ~2 s
    _, latched = b.update(6.1, -60.0)
    assert not latched
    b.update(7.0, -20.0, clipped=5)
    assert b.clip_total == 137                  # session counter accumulates
    b.reset_session()                           # a chain edit: new era
    assert b.clip_total == 0
    _, latched = b.update(7.1, -60.0)
    assert not latched


def test_engine_end_to_end_over_a_pipe():
    """Feed the worker interleaved f32 through a real pipe: identity chains,
    ch0 at -6 dBFS, ch1 carrying a >FS burst -> published frames must show
    the right peaks and a nonzero clip count on ch1 only."""
    frames = []
    eng = MeterEngine(frames.append, fs=48000)
    eng.set_chains(0.0, [[], []])               # identity (Bypass view)

    n = 48000
    x = np.zeros((n, 2), dtype=np.float32)
    x[:, 0] = 0.5 * np.sin(2 * np.pi * 997 * np.arange(n) / 48000)
    x[24000:24512, 1] = 1.2                     # 512-sample over-FS burst

    r, w = os.pipe()
    def feed():
        with os.fdopen(w, "wb") as fw:
            fw.write(x.tobytes())               # interleaved f32
    t = threading.Thread(target=feed)
    t.start()
    with os.fdopen(r, "rb") as fr:
        eng._run(fr)                            # synchronous: pipe EOF ends it
    t.join()

    assert frames, "worker published nothing"
    pk0 = max(f["peaks_db"][0] for f in frames)
    pk1 = max(f["peaks_db"][1] for f in frames)
    assert pk0 == pytest.approx(20 * np.log10(0.5), abs=0.05)
    assert pk1 == pytest.approx(20 * np.log10(1.2), abs=0.01)
    assert sum(f["clips"][1] for f in frames) == 512
    assert sum(f["clips"][0] for f in frames) == 0
    # ~30 Hz aggregation: 1 s of audio -> on the order of 30 frames
    assert 20 <= len(frames) <= 40


def test_chain_swap_resets_state():
    """set_chains during streaming must swap at a block boundary and reset
    zi -- an edited profile meters with the new curve, not a hybrid."""
    frames = []
    got_frame = threading.Event()
    def pub(fr):
        frames.append(fr)
        got_frame.set()
    eng = MeterEngine(pub, fs=48000)
    eng.set_chains(0.0, [[eq.Band("PK", 1000.0, 12.0, 1.0, True)]])
    r, w = os.pipe()
    n = 4800
    sine = (0.25 * np.sin(2 * np.pi * 1000 * np.arange(n) / 48000)
            ).astype(np.float32)
    def feed():
        with os.fdopen(w, "wb") as fw:
            fw.write(sine.tobytes())
            fw.flush()
            assert got_frame.wait(5.0)          # first frame is pre-swap
            eng.set_chains(-6.0, [[]])          # swap mid-stream
            fw.write(sine.tobytes())
    t = threading.Thread(target=feed)
    t.start()
    with os.fdopen(r, "rb") as fr:
        eng._run(fr)
    t.join()
    first, last = frames[0], frames[-1]
    assert first["peaks_db"][0] == pytest.approx(-12.0 + 12.0, abs=0.3)
    assert last["peaks_db"][0] == pytest.approx(-12.0 - 6.0, abs=0.3)


def test_monitor_capture_command(monkeypatch):
    """The capture belongs to pipewire.py: right tool, the PRE-EQ monitor
    prop, pinned format/rate/channels, streaming to stdout."""
    from perdeviceeq import pipewire
    seen = {}
    class FakePopen:
        def __init__(self, cmd, **kw):
            seen["cmd"], seen["kw"] = cmd, kw
            self.stdout = None
    monkeypatch.setattr(pipewire.subprocess, "Popen", FakePopen)
    pipewire.monitor_capture("bluez_output.X.1", 2, 48000)
    c = seen["cmd"]
    assert c[0] == "pw-record" and c[-1] == "-"
    assert any("stream.capture.sink = true" in a for a in c)
    assert any("per-device-eq-meter" in a for a in c)
    assert c[c.index("--target") + 1] == "bluez_output.X.1"
    assert c[c.index("--channels") + 1] == "2"
    assert c[c.index("--format") + 1] == "f32"
    assert seen["kw"]["stdout"] is pipewire.subprocess.PIPE


def test_count_changing_swap_is_deferred_to_restart():
    """set_chains with a different channel count while running must never
    crash the worker (device-switch regression): it stays pending until
    stop() clears the state, and the next start picks it up."""
    frames = []
    got_frame = threading.Event()
    def pub(fr):
        frames.append(fr)
        got_frame.set()
    eng = MeterEngine(pub, fs=48000)
    eng.set_chains(0.0, [[]])                   # 1 channel, identity
    r, w = os.pipe()
    sine = (0.5 * np.sin(2 * np.pi * 500 * np.arange(4096) / 48000)
            ).astype(np.float32)
    def feed():
        with os.fdopen(w, "wb") as fw:
            fw.write(sine.tobytes()); fw.flush()
            assert got_frame.wait(5.0)          # worker latched 1-ch config
            eng.set_chains(0.0, [[], []])       # 2 channels mid-stream
            fw.write(sine.tobytes())
    th = threading.Thread(target=feed); th.start()
    with os.fdopen(r, "rb") as fr:
        eng._run(fr)                            # must not raise
    th.join()
    assert frames and all(len(f["peaks_db"]) == 1 for f in frames)
    eng.stop()                                  # clears state
    assert eng.n_channels == 2                  # pending survived for restart
