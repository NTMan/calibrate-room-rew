#!/usr/bin/env python3
"""Real-path measurement runner (ROADMAP Task 3, increment 2).

Orchestrates a live sweep measurement on top of tools/measure_core.py:
plays the Farina sweep through a PipeWire sink (pw-play) while capturing
the measurement mic (pw-record), then feeds the takes to the offline core.
CLI only; the GTK wizard is increment 4, the constrained fit increment 3.

The PipeWire plumbing (graph inspection, profile bypass, the capture
stream, playback path verification, the auto-level controller and
run_take) lives in measure_session.py; this file is the CLI on top:
argparse, confirmations, the take loop and the printing.

Method notes (worth not re-deriving):

- Foreign streams: anything else playing into the sink during the sweep is
  measured too. By default their presence refuses the run with a list;
  --mute-others instead mutes them (Props mute=true via pw-cli) for the
  duration and restores the previous mute state after. The list, muted or
  not, goes into `foreign_streams` of the result.
- Levels policy: the digital sweep level is FIXED at -6 dBFS (core), the
  sweep stream volume is forced to 1.0 (pw-play --volume, verified from
  the node's Props), and the sink volume is never touched -- the protocol
  is to measure at the working listening level via the sink's own control.
  The only exception is --auto-level: starting from a quiet volume
  (min(current, 0.15) cubic) it adjusts the sink volume via wpctl until
  the capture peak lands in the -12..-6 dBFS window, after an explicit
  confirmation. It assumes nothing about the device's volume->gain law
  (a BT sink's is nothing like the software cube law): it brackets the
  window and interpolates in log-volume between a too-quiet and a
  too-loud probe, stepping with the slope measured from the last two
  probes until then, capped per step and held below any level seen to
  clip -- so the first sound neither blasts nor overshoots into a clip
  (see AutoLevel). Without --auto-level the sink volume is never raised
  above its value at start (it is not written at all). Everything ends
  up in `levels`.
- SNR: pw-record is asked for a bare stream with --raw; without it the
  stdout stream is prefixed with a format descriptor (rate/channels POD)
  whose bytes decode to a NaN at the start of channel 0 on every
  capture. Each take gets a quick pre-roll noise-floor check right after
  capture (same threshold and wording as the core) so a noisy room is
  caught on take 1, not after five reseats; up to REPAIR_MAX_MS of
  isolated non-finite (NaN/Inf) samples on the analyzed channel are
  interpolated as a capture xrun (with a warning) while a larger flood
  aborts as a faulty input; the non-finite scan covers ALL channels, not
  just the analyzed one, so a glitch on the other side is not invisible.
  A full-scale sample count flags a genuinely clipped (unusable) take
  and a peak above HOT_DBFS is only a low-headroom advisory. The
  authoritative numbers are still computed by the core from the aligned
  impulse.
- Raw takes (float32 wav, all captured channels) plus the sweep wav, its
  sidecar and the analytic inverse (REW cross-check) are saved under
  tests/fixtures-local/<device>_<stamp>/ -- .gitignore'd, real captures
  never enter git.

CI without hardware: tests/test_measure_run.py runs this file unmodified
against fake pw-* executables (tests/shims/) on PATH; the fake pw-record
answers with the sweep processed through a known pde_audit biquad chain,
so the whole orchestration closes against chain_curve() within the core
tolerance. Manual dry-run: PATH="tests/shims:$PATH" tools/measure_run.py ...

Exit codes: 0 ok, 1 measurement error, 2 environment/precondition refusal,
3 declined confirmation, 130 interrupted.
"""
import argparse
import math
import os
import re
import sys
from datetime import datetime

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import measure_core as mc
from measure_session import (
    AUTO_MAX_ADJUST, AUTO_START_VOLUME, AUTO_WINDOW, FULLSCALE, HOT_DBFS,
    REPAIR_MAX_MS, AutoLevel, MeasureError, MuteOthers, ProfileBypass,
    RefusalError, _props, check_sink_identity, default_save_base,
    foreign_streams, node_ident, peak_dbfs, pw_dump, repair_nonfinite,
    require_tools, resolve_node, run_take, save_take_wav, set_sink_volume,
    sink_volume_state, write_sweep_files)

# --- interaction ---------------------------------------------------------

def confirm(prompt, assume_yes):
    if assume_yes:
        print("%s [auto-confirmed: --yes]" % prompt)
        return True
    try:
        ans = input("%s [y/N] " % prompt)
    except EOFError:
        return False
    return ans.strip().lower() in ("y", "yes")


def pause_reseat(i, n):
    try:
        input("Take %d/%d: reseat the headphone/mic, then press Enter..."
              % (i, n))
    except EOFError:
        print("(stdin closed; continuing without the reseat pause)")


def quick_snr_check(x, sweep):
    """Fast per-take noise-floor check so a noisy room is caught before
    the next reseat. Onset = first sustained crossing of 10x the pre-roll
    RMS; threshold and wording match the core."""
    fs = sweep.fs
    head = x[:int(0.4 * fs)]
    noise = math.sqrt(float(np.mean(head ** 2))) if len(head) else 0.0
    thr = max(10.0 * noise, 1e-6)
    over = np.flatnonzero(np.abs(x) > thr)
    if not len(over):
        return None
    snr, _, _ = mc.estimate_snr(x, int(over[0]), sweep)
    if snr is not None and snr < mc.SNR_WARN_DB:
        print("WARNING: low SNR (%.1f dB): raise the level or kill the "
              "noise source" % snr, file=sys.stderr)
    return snr


# --- main ----------------------------------------------------------------

def measure(a):
    if a.takes < 1:
        raise RefusalError("--takes must be >= 1")
    if not 0 <= a.channel < a.channels:
        raise RefusalError("--channel %d out of range for --channels %d"
                           % (a.channel, a.channels))
    tools = ["pw-dump", "pw-metadata", "pw-play", "pw-record"]
    if a.auto_level:
        tools.append("wpctl")
    if a.mute_others:
        tools.append("pw-cli")
    require_tools(tools)

    dump = pw_dump()
    sink = resolve_node(dump, a.sink, "Audio/Sink")
    check_sink_identity(sink)
    source = resolve_node(dump, a.source, "Audio/Source")
    src_p = _props(source)
    if src_p.get("media.class") != "Audio/Source":
        raise RefusalError("capture target %r is %r, expected Audio/Source"
                           % (a.source, src_p.get("media.class")))
    if not (src_p.get("device.api") or "").startswith("alsa"):
        print("WARNING: mic source device.api is %r; measurement mics are "
              "expected on USB/ALSA" % src_p.get("device.api"),
              file=sys.stderr)

    v0, raw0, muted = sink_volume_state(dump, sink["id"])
    if muted:
        raise RefusalError("sink is muted; unmute it and set the working "
                           "listening level first")
    if v0 is None:
        print("WARNING: could not read the sink volume from pw-dump",
              file=sys.stderr)

    foreign = foreign_streams(dump, sink["id"])
    if foreign and not a.mute_others:
        raise RefusalError(
            "other streams are playing into this sink (a sweep on top of "
            "them is not a measurement):\n  %s\nstop them or re-run with "
            "--mute-others" % "\n  ".join(
                "id %(id)s  %(node_name)s  app=%(app)s" % s for s in foreign))

    sweep = mc.generate_sweep(a.samples, a.fs, a.f_start, a.f_end)
    wav_dur = a.pre_silence + sweep.duration_s + a.post_silence
    sink_id = node_ident(sink)

    print("Target sink : %s (%s, id %s, %s)"
          % (sink_id["description"] or sink_id["name"], sink_id["name"],
             sink_id["id"], sink_id["device_api"]))
    print("Mic source  : %s (id %s)"
          % (src_p.get("node.description") or src_p.get("node.name"),
             source["id"]))
    print("Sink volume : %s (will NOT be changed%s)"
          % ("%.0f%%" % (100 * v0) if v0 is not None else "unknown",
             "" if not a.auto_level else "; --auto-level may raise it"))
    print("Sweep       : %.2f s, %g-%g Hz @ %g dBFS digital, stream "
          "volume 1.0, %d take(s)"
          % (sweep.duration_s, sweep.f_start, sweep.f_end,
             sweep.level_dbfs, a.takes))
    if not confirm("The sweep WILL PLAY on this device at the volume "
                   "above. Proceed?", a.yes):
        print("declined", file=sys.stderr)
        return 3

    slug = re.sub(r"[^\w.+-]+", "_",
                  a.device or sink_id["name"] or "device").strip("_")
    outdir = os.path.join(a.save_dir or default_save_base(),
                          "%s_%s" % (slug,
                                     datetime.now().strftime("%Y%m%d-%H%M%S")))
    os.makedirs(outdir, exist_ok=True)
    wav = write_sweep_files(outdir, sweep, a.pre_silence, a.post_silence)
    print("Artifacts   : %s" % outdir)

    recordings, capture_peaks, path_clean = [], [], None
    auto = {"enabled": bool(a.auto_level), "adjustments": 0,
            "initial": None, "final": None, "in_window": None}
    v_cur = v0
    auto_ctl = AutoLevel()

    with MuteOthers(foreign, a.mute_others), \
         ProfileBypass(sink_id["name"]) as eq_state:
        if a.auto_level:
            if not confirm("--auto-level will adjust the sink volume "
                           "(start quiet at %.0f%%, up to %d raises, "
                           "target peak %g..%g dBFS). Proceed?"
                           % (100 * min(v0 or 1.0, AUTO_START_VOLUME),
                              AUTO_MAX_ADJUST, *AUTO_WINDOW), a.yes):
                print("declined", file=sys.stderr)
                return 3
            v_cur = min(v0 if v0 is not None else 1.0, AUTO_START_VOLUME)
            auto["initial"] = round(v_cur, 4)
            set_sink_volume(sink["id"], v_cur)

        leveled = not a.auto_level
        while len(recordings) < a.takes:
            if recordings:
                pause_reseat(len(recordings) + 1, a.takes)
            raw_path = (os.path.join(outdir, "raw%02d.wav"
                                     % (len(recordings) + 1))
                        if a.raw_capture_dump else None)
            data, info = run_take(sink, source, wav, wav_dur, a.channels,
                                  sweep.fs, verify=path_clean is None,
                                  raw_dump_path=raw_path)
            if info is not None:
                path_clean = info
            # diagnostic: scan ALL channels, not just the one we analyze,
            # so a glitch on the other channel isn't invisible
            for c in range(data.shape[1]):
                w = np.nonzero(~np.isfinite(data[:, c]))[0]
                if w.size:
                    print("note: %d non-finite sample(s) on channel %d at "
                          "%s of %d"
                          % (w.size, c, list(w[:6]), data.shape[0]),
                          file=sys.stderr)
            chan = data[:, a.channel]
            where = np.nonzero(~np.isfinite(chan))[0]
            bad = int(where.size)
            if bad:
                limit = max(1, int(REPAIR_MAX_MS / 1000.0 * sweep.fs))
                if bad > limit or bad >= len(chan):
                    raise MeasureError(
                        "channel %d capture has %d non-finite sample(s) "
                        "(NaN/Inf) -- too many to be a dropout; the input "
                        "is faulty, not merely quiet. Check that channel "
                        "%d of the mic is wired/seated, or point --channel "
                        "at the live one (a %d-channel capture exposes "
                        "0..%d)." % (a.channel, bad, a.channel,
                                     a.channels, a.channels - 1))
                chan = repair_nonfinite(chan)
                data = data.copy()
                data[:, a.channel] = chan     # keep the saved take finite
                print("WARNING: interpolated %d non-finite capture "
                      "sample(s) on channel %d at %s of %d -- a benign "
                      "single-sample glitch during the sweep; the take is "
                      "unaffected."
                      % (bad, a.channel, list(where[:6]), len(chan)),
                      file=sys.stderr)
            pk = peak_dbfs(chan)
            print("take %d/%d: capture peak %.1f dBFS"
                  % (len(recordings) + 1, a.takes, pk))
            clipped = int(np.count_nonzero(np.abs(chan) >= FULLSCALE))
            if clipped:
                print("WARNING: %d sample(s) at full scale -- the sweep "
                      "is clipped and this take is unusable; lower the "
                      "sink volume (or use --auto-level) and remeasure."
                      % clipped, file=sys.stderr)
            elif pk >= HOT_DBFS:
                print("WARNING: capture peak %.1f dBFS leaves little "
                      "headroom (risk of inter-sample clipping); consider "
                      "a lower level or --auto-level (targets %g..%g "
                      "dBFS)." % (pk, *AUTO_WINDOW), file=sys.stderr)

            if not leveled:
                auto_ctl.observe(v_cur, pk, bool(clipped))
                v_new = auto_ctl.next_volume(v_cur, pk)
                stuck = abs(v_new - v_cur) < 1e-3   # can't move any further
                if not clipped and auto_ctl.in_window(pk):
                    leveled, auto["in_window"] = True, True
                elif auto["adjustments"] >= AUTO_MAX_ADJUST or stuck:
                    auto["in_window"] = False
                    why = ("the level cannot be moved further (at %.0f%%)"
                           % (100 * v_cur) if stuck
                           else "%d adjustments" % AUTO_MAX_ADJUST)
                    print("WARNING: auto-level gave up after %s (peak %.1f "
                          "dBFS outside %g..%g)"
                          % (why, pk, *AUTO_WINDOW), file=sys.stderr)
                    if not confirm("Continue at the current level anyway?",
                                   a.yes):
                        raise MeasureError("aborted: capture level never "
                                           "reached the target window")
                    leveled = True
                else:
                    auto["adjustments"] += 1
                    print("auto-level: sink volume %.0f%% -> %.0f%% "
                          "(step %d/%d), retrying the take"
                          % (100 * v_cur, 100 * v_new,
                             auto["adjustments"], AUTO_MAX_ADJUST))
                    set_sink_volume(sink["id"], v_new)
                    v_cur = v_new
                    continue        # discard the leveling probe

            quick_snr_check(chan, sweep)
            save_take_wav(outdir, len(recordings) + 1, data, sweep.fs)
            recordings.append(chan)
            capture_peaks.append(round(pk, 2))

    dump = pw_dump()
    v_final, raw_final, _ = sink_volume_state(dump, sink["id"])
    v_report = v_final if v_final is not None else v_cur
    auto["final"] = round(v_report, 4) if a.auto_level else None
    levels = {
        "sink_volume": round(v_report, 4) if v_report is not None else None,
        "sink_volume_start": round(v0, 4) if v0 is not None else None,
        "sink_channel_volumes": raw_final or raw0,
        "stream_volume": (path_clean or {}).get("playback_stream",
                                                {}).get("volume"),
        "capture_peak_dbfs": capture_peaks,
        "auto_level": auto,
    }

    result = mc.process_takes(
        recordings, sweep, cal=a.cal, smoothing_fraction=a.smoothing,
        device=a.device or sink_id["description"] or sink_id["name"],
        rig=a.rig, mic=a.mic, eq_profile_state=eq_state, levels=levels,
        path_clean=path_clean, foreign_streams=foreign)
    out = a.out or os.path.join(outdir, "result.json")
    mc.save_result(result, out)

    t = result["takes"]
    print("%s: %d take(s), delay jitter %.2f ms, SNR min %s dB"
          % (out, t["count"], t["delay_jitter_ms"],
             t["snr_min_db"] if t["snr_min_db"] is not None else "n/a"))
    for w in result["warnings"]:
        print("WARNING: %s" % w)
    if eq_state["restored"] is False:
        return 1
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="exit codes: 0 ok, 1 measurement error, 2 refusal, "
               "3 declined, 130 interrupted")
    p.add_argument("--sink", required=True,
                   help="target device: node id, node.name or unique "
                        "substring")
    p.add_argument("--source", required=True,
                   help="measurement mic: node id, node.name or unique "
                        "substring")
    p.add_argument("--takes", type=int, default=1,
                   help="seatings to average (reseat prompt in between)")
    p.add_argument("--channels", type=int, default=1,
                   help="capture channel count (EARS = 2)")
    p.add_argument("--channel", type=int, default=0,
                   help="captured channel to analyze")
    p.add_argument("--device", help="device label for the result/dir name")
    p.add_argument("--rig")
    p.add_argument("--mic")
    p.add_argument("--cal", help="mic calibration file (miniDSP format)")
    p.add_argument("--smoothing", type=int, default=6,
                   help="1/N octave (0 = off)")
    p.add_argument("--samples", type=int, default=mc.DEFAULT_N)
    p.add_argument("--fs", type=int, default=mc.DEFAULT_FS)
    p.add_argument("--f-start", type=float, default=mc.DEFAULT_F_START)
    p.add_argument("--f-end", type=float, default=mc.DEFAULT_F_END)
    p.add_argument("--pre-silence", type=float, default=1.0)
    p.add_argument("--post-silence", type=float, default=0.5)
    p.add_argument("--mute-others", action="store_true",
                   help="mute foreign streams on the sink instead of "
                        "refusing to start")
    p.add_argument("--auto-level", action="store_true",
                   help="adjust the sink volume until the capture peak is "
                        "in %g..%g dBFS (max %d raises, confirmed)"
                        % (*AUTO_WINDOW, AUTO_MAX_ADJUST))
    p.add_argument("--yes", action="store_true",
                   help="assume yes on confirmations (NOT on reseat "
                        "pauses)")
    p.add_argument("--out", help="result JSON path (default: "
                                 "<save dir>/result.json)")
    p.add_argument("--save-dir",
                   help="base dir for raw takes (default: "
                        "tests/fixtures-local of the checkout, else cwd)")
    p.add_argument("--raw-capture-dump", action="store_true",
                   help="also save the untouched capture as raw<NN>.wav "
                        "for glitch diagnostics")
    a = p.parse_args(argv)

    try:
        return measure(a)
    except RefusalError as e:
        print("refusing to start: %s" % e, file=sys.stderr)
        return 2
    except MeasureError as e:
        print("measurement failed: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted (profile restore ran in the context exit)",
              file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
