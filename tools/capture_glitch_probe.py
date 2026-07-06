#!/usr/bin/env python3
"""Repeatability probe for the capture non-finite glitch.

Not part of the measurement pipeline -- a diagnostic to decide whether the
occasional NaN in a capture is a PipeWire/driver artifact (reproducible by
the raw capture path) or something in the measurement orchestration.

It reuses measure_session.CaptureStream verbatim (same node.target pinning,
same reader thread and byte assembly), captures N times in a row -- optionally
with a sweep playing concurrently on a sink, matching the measurement -- and
reports, per channel, how many runs produced non-finite samples and where.
Since the --raw fix nothing is dropped from a capture any more (the
measurement interpolates isolated non-finite samples and aborts on floods),
so every non-finite sample seen here is a real anomaly; the per-channel
counts and the first-index histogram localize it.

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

import measure_session as ms


def _play(sink_id, wav):
    return subprocess.Popen(
        ["pw-play", "--volume", "1.0",
         "-P", "{ node.name = pde-probe-play, node.target = %d, "
               "node.dont-reconnect = true }" % sink_id, wav],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def probe(a):
    dump = ms.pw_dump()
    src = ms.resolve_node(dump, a.source, "Audio/Source")
    sink = ms.resolve_node(dump, a.sink, "Audio/Sink") if a.sink else None
    print("source: %s (id %d)" % (src["info"]["props"].get("node.name"),
                                  src["id"]))
    if sink and a.sweep:
        print("playing %s on %s (id %d) during capture"
              % (a.sweep, sink["info"]["props"].get("node.name"), sink["id"]))
    need = int(a.seconds * a.rate)

    runs_with_nan = 0
    hits = [0] * a.channels             # non-finite samples per channel
    first_idx = []                      # first non-finite index seen per run

    for r in range(a.runs):
        cap = ms.CaptureStream(src["id"], a.channels, a.rate)
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
        x = cap.data()
        run_bad = False
        run_first = None
        for c in range(min(a.channels, x.shape[1])):
            idx = np.nonzero(~np.isfinite(x[:, c]))[0]
            if idx.size:
                run_bad = True
                hits[c] += int(idx.size)
                run_first = idx[0] if run_first is None \
                    else min(run_first, idx[0])
        if run_bad:
            runs_with_nan += 1
            first_idx.append(int(run_first))
        print("  run %2d/%d: %s" % (r + 1, a.runs,
              "nonfinite at %s" % run_first if run_bad else "clean"))

    print("\n%d/%d runs had non-finite samples" % (runs_with_nan, a.runs))
    for c in range(a.channels):
        print("  channel %d: %d non-finite sample(s)" % (c, hits[c]))
    if first_idx:
        vals, cnt = np.unique(np.array(first_idx), return_counts=True)
        print("  first-index histogram:",
              dict(zip(vals.tolist(), cnt.tolist())))
    if any(hits):
        print("VERDICT: non-finite samples in the raw capture path -- a "
              "real capture dropout, worth a PipeWire report.")
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
