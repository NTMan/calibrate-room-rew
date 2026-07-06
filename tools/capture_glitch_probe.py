#!/usr/bin/env python3
"""Repeatability probe for the capture non-finite glitch.

Not part of the measurement pipeline -- a diagnostic to decide whether the
occasional NaN in a capture is a PipeWire/driver artifact (reproducible by
the raw capture path) or something in the measurement orchestration.

It reuses measure_run.CaptureStream verbatim (same node.target pinning, same
reader thread and byte assembly), captures N times in a row -- optionally
with a sweep playing concurrently on a sink, matching the measurement -- and
reports, per channel, how many runs produced non-finite samples and where.
Crucially it splits the counts into the warmup region (first
CAPTURE_WARMUP_FRAMES, which the measurement drops) and after it (a real
mid-stream dropout), and reads the UNTRIMMED capture so nothing is hidden.

If NaN shows up here at a real rate on a stable position, that is material
for a PipeWire bug report (attach: pipewire --version, `pw-cli info 0`, the
per-run indices, and a raw<NN>.wav). If it never shows here but does inside
the full measurement, the cause is in the orchestration, not PipeWire.

Example:
  tools/capture_glitch_probe.py --source miniDSP --sink bluez_output.X \
      --sweep tests/fixtures-local/<run>/sweep.wav --runs 50
  tools/capture_glitch_probe.py --source miniDSP --runs 50   # silent, no play
"""
import argparse
import subprocess
import sys
import time

import numpy as np

import measure_run as mr


def _play(sink_id, wav):
    return subprocess.Popen(
        ["pw-play", "--volume", "1.0",
         "-P", "{ node.name = pde-probe-play, node.target = %d, "
               "node.dont-reconnect = true }" % sink_id, wav],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def probe(a):
    dump = mr.pw_dump()
    src = mr.resolve_node(dump, a.source, "Audio/Source")
    sink = mr.resolve_node(dump, a.sink, "Audio/Sink") if a.sink else None
    print("source: %s (id %d)" % (src["info"]["props"].get("node.name"),
                                  src["id"]))
    if sink and a.sweep:
        print("playing %s on %s (id %d) during capture"
              % (a.sweep, sink["info"]["props"].get("node.name"), sink["id"]))
    warmup = mr.CAPTURE_WARMUP_FRAMES
    need = int(a.seconds * a.rate)

    runs_with_nan = 0
    warm_hits = [0] * a.channels        # NaN inside the dropped warmup
    tail_hits = [0] * a.channels        # NaN after warmup (a real dropout)
    first_idx = []                      # first non-finite index seen per run

    for r in range(a.runs):
        cap = mr.CaptureStream(src["id"], a.channels, a.rate)
        play = None
        try:
            time.sleep(0.3)
            if sink and a.sweep:
                play = _play(sink["id"], a.sweep)
            cap.wait_frames(need, timeout=a.seconds + 30)
        finally:
            if play is not None and play.poll() is None:
                play.kill()
            cap.stop()
        x = cap.data(warmup_drop=False)
        run_bad = False
        run_first = None
        for c in range(min(a.channels, x.shape[1])):
            idx = np.nonzero(~np.isfinite(x[:, c]))[0]
            if idx.size:
                run_bad = True
                warm_hits[c] += int(np.count_nonzero(idx < warmup))
                tail_hits[c] += int(np.count_nonzero(idx >= warmup))
                run_first = idx[0] if run_first is None \
                    else min(run_first, idx[0])
        if run_bad:
            runs_with_nan += 1
            first_idx.append(int(run_first))
        print("  run %2d/%d: %s" % (r + 1, a.runs,
              "nonfinite at %s" % run_first if run_bad else "clean"))

    print("\n%d/%d runs had non-finite samples" % (runs_with_nan, a.runs))
    for c in range(a.channels):
        print("  channel %d: %d in warmup (<%d, dropped), %d after (real)"
              % (c, warm_hits[c], warmup, tail_hits[c]))
    if first_idx:
        vals, cnt = np.unique(np.array(first_idx), return_counts=True)
        print("  first-index histogram:",
              dict(zip(vals.tolist(), cnt.tolist())))
    if any(tail_hits):
        print("VERDICT: non-finite samples AFTER the warmup -- a real "
              "mid-stream capture dropout, worth a PipeWire report.")
    elif runs_with_nan:
        print("VERDICT: non-finite only in the start-of-stream warmup, "
              "which the measurement drops. Reproducible here with the raw "
              "path -> a PipeWire warmup artifact; the measurement is "
              "unaffected.")
    else:
        print("VERDICT: no non-finite samples in %d runs." % a.runs)
    return 0


def main(argv):
    p = argparse.ArgumentParser(description="capture non-finite glitch probe")
    p.add_argument("--source", required=True,
                   help="mic source (id, node.name, or substring)")
    p.add_argument("--sink", help="sink to play the sweep on during capture")
    p.add_argument("--sweep", help="wav to play on --sink (else capture is "
                                   "silent)")
    p.add_argument("--runs", type=int, default=30)
    p.add_argument("--channels", type=int, default=2)
    p.add_argument("--rate", type=int, default=48000)
    p.add_argument("--seconds", type=float, default=1.5)
    return probe(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
