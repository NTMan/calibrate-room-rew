# -*- coding: utf-8 -*-
"""Tier-2 live post-EQ meter engine (ROADMAP Task 2). No GTK in here.

The sink monitor taps PRE-EQ in the in-node topology, so the true
post-EQ level is computed, not tapped: pipewire.monitor_capture() (the
module owning ALL PipeWire interaction) streams the monitor as raw f32 and a worker thread runs each channel through the
profile's biquads (eq.biquad -- coefficient-identical to PipeWire's
param_eq) block by block with filter state carried across blocks. Peaks
and >=FS sample counts are aggregated to ~30 Hz and handed to a
`publish(frame)` callable; the GUI wraps that in GLib.idle_add.

Ballistics (instant attack, constant-rate decay, clip latch) live here
as a pure class so they are unit-testable without a display.
"""
import os
import threading
import time

import numpy as np
from scipy import signal as sg

from .config import FS

BLOCK = 1024            # samples per filter call
PUBLISH_HZ = 30.0


def chain_sos(bands, fs=FS):
    """Second-order sections for the enabled bands (shape (n, 6))."""
    from . import eq
    rows = [eq.biquad(b.type, b.freq, b.gain, b.q, fs)
            for b in bands if b.enabled]
    return np.asarray(rows, dtype=np.float64).reshape(-1, 6)


class Ballistics:
    """Per-channel display state: instant attack, ~20 dB/s decay, and a
    clip latch that keeps the alarm visible long enough for a human --
    a 15-sample episode is 0.3 ms, no 30 Hz bar can show it unlatched."""
    DECAY_DB_S = 20.0
    LATCH_S = 2.0
    FLOOR_DB = -60.0

    def __init__(self):
        self.bar = self.FLOOR_DB
        self.clip_total = 0
        self._last = None
        self._latch_until = -1.0

    def update(self, now, peak_db, clipped=0):
        """Feed one aggregated frame; returns (bar_db, latched)."""
        if self._last is not None:
            self.bar -= self.DECAY_DB_S * max(0.0, now - self._last)
        self._last = now
        self.bar = max(self.bar, peak_db, self.FLOOR_DB)
        if clipped:
            self.clip_total += int(clipped)
            self._latch_until = now + self.LATCH_S
        return self.bar, now < self._latch_until

    def reset_session(self):
        """A chain edit starts a new measurement era: the old counters and
        the latch described the OLD chain and must not outlive it."""
        self.clip_total = 0
        self._latch_until = -1.0


class MeterEngine:
    """Owns the capture subprocess + worker thread.

    publish(frame) is called FROM THE WORKER THREAD roughly PUBLISH_HZ
    times a second with {"peaks_db": [...], "clips": [...], "t": float}
    (one entry per channel). Chains are swapped atomically at block
    boundaries via set_chains(); filter state resets on swap.
    """

    def __init__(self, publish, fs=FS, block=BLOCK):
        self.publish = publish
        self.fs = int(fs)
        self.block = int(block)
        self._proc = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._pending = None            # (gain_lin, [sos]) awaiting swap
        self._gain = 1.0
        self._sos = []                  # per-channel sos arrays
        self._zi = []

    # ---- configuration -----------------------------------------------------
    def set_chains(self, preamp_db, per_channel_bands, fs=None):
        """One shared preamp + per-channel enabled-band lists (an empty list
        per channel = identity: exactly what Bypass should show)."""
        if fs:
            self.fs = int(fs)
        gain = 10.0 ** (float(preamp_db) / 20.0)
        sos = [chain_sos(b, self.fs) for b in per_channel_bands]
        with self._lock:
            self._pending = (gain, sos)

    @property
    def n_channels(self):
        with self._lock:
            src = self._pending[1] if self._pending else self._sos
            return len(src)

    # ---- lifecycle ----------------------------------------------------------
    def start(self, node):
        """Spawn pw-record on the sink monitor and the worker thread."""
        self.stop()
        n = self.n_channels
        if not n:
            raise RuntimeError("set_chains() before start()")
        from . import pipewire
        self._proc = pipewire.monitor_capture(node, n, self.fs)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run,
                                        args=(self._proc.stdout,),
                                        daemon=True)
        self._thread.start()

    def alive(self):
        """The capture worker is running. False after the tap died
        with its pipe (node.dont-reconnect) so the owner can
        re-arm on the next beat instead of trusting a corpse."""
        return self._thread is not None and self._thread.is_alive()

    def stop(self):
        self._stop.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=1.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._sos = []
        self._zi = []

    # ---- worker (also driven directly by tests with any binary file) -------
    def _swap_if_pending(self):
        with self._lock:
            if self._pending is None:
                return
            # The channel count is pinned for the life of one capture; a
            # count-changing swap can only be the prelude to a restart, so
            # leave it pending for the next worker's initial swap instead
            # of crashing this one (stop() clears _sos, letting it through).
            if self._sos and len(self._pending[1]) != len(self._sos):
                return
            self._gain, self._sos = self._pending
            self._pending = None
        self._zi = [np.zeros((s.shape[0], 2)) for s in self._sos]

    def _run(self, f):
        dbg = bool(os.environ.get("PDE_METER_DEBUG"))
        self._swap_if_pending()
        n = len(self._sos)
        frame_bytes = self.block * n * 4
        agg = max(1, int(round(self.fs / self.block / PUBLISH_HZ)))
        peaks = np.zeros(n)
        clips = np.zeros(n, dtype=int)
        blocks = 0
        nsamp = 0
        if dbg:
            print("meter: run n=%d block=%d agg=%d" % (n, self.block, agg),
                  file=__import__("sys").stderr)
        while not self._stop.is_set():
            buf = f.read(frame_bytes)
            if not buf:
                break
            if len(buf) % (n * 4):                     # partial tail
                buf = buf[: len(buf) - len(buf) % (n * 4)]
                if not buf:
                    break
            x = np.frombuffer(buf, dtype=np.float32).reshape(-1, n)
            # The channel count is pinned for the life of one capture: any
            # change (device switch) goes through start(), never through a
            # mid-stream swap -- swaps only exchange gains/coefficients.
            self._swap_if_pending()
            for c in range(n):
                y = x[:, c].astype(np.float64) * self._gain
                if self._sos[c].size:
                    y, self._zi[c] = sg.sosfilt(self._sos[c], y,
                                                zi=self._zi[c])
                a = np.abs(y)
                peaks[c] = max(peaks[c], float(a.max()) if a.size else 0.0)
                clips[c] += int((a >= 1.0).sum())
            blocks += 1
            nsamp += x.shape[0]
            if blocks >= agg:
                self._flush(peaks, clips, nsamp)
                nsamp = 0
                peaks[:] = 0.0
                clips[:] = 0
                blocks = 0
        if blocks:
            self._flush(peaks, clips, nsamp)

    def _flush(self, peaks, clips, nsamp=0):
        db = [20.0 * np.log10(p) if p > 1e-7 else -140.0 for p in peaks]
        self.publish({"peaks_db": db, "clips": [int(c) for c in clips],
                      "samples": int(nsamp), "t": time.monotonic()})
