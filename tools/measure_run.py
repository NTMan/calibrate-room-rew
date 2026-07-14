#!/usr/bin/env python3
"""Real-path measurement runner (ROADMAP Task 3, increment 2).

Orchestrates a live sweep measurement on top of perdeviceeq/measure_core.py:
plays the Farina sweep through a PipeWire sink (pw-play) while capturing
the measurement mic (pw-record), then feeds the takes to the offline core.
CLI only; the GTK wizard is increment 4, the constrained fit increment 3.

Since increment 4 (part 1) this file is a thin driver over
measure_session.MeasureSession, which owns the measurement lifecycle
and the method notes that used to live here. This side keeps argparse,
the confirmations and reseat pauses, the mapping of structured
TakeOutcomes to the exact stdout/stderr wording, and the exit codes.

CI without hardware: tests/test_measure_run.py runs this file unmodified
against fake pw-* executables (tests/shims/) on PATH; the fake pw-record
answers with the sweep processed through a known pde_audit biquad chain,
so the whole orchestration closes against chain_curve() within the core
tolerance. Manual dry-run: PATH="tests/shims:$PATH" tools/measure_run.py ...

Exit codes: 0 ok, 1 measurement error, 2 environment/precondition refusal,
3 declined confirmation, 130 interrupted.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from perdeviceeq import measure_core as mc
from perdeviceeq.measure_session import (
    AUTO_MAX_ADJUST, AUTO_PEAK_CEIL, AUTO_START_VOLUME,
    FaultyCaptureError, MeasureError, MeasureSession, RefusalError,
    SessionConfig, default_save_base)


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


# --- main ----------------------------------------------------------------

def measure(a):
    if a.takes < 1:
        raise RefusalError("--takes must be >= 1")
    if not 0 <= a.channel < a.channels:
        raise RefusalError("--channel %d out of range for --channels %d"
                           % (a.channel, a.channels))
    cfg = SessionConfig(sink=a.sink, source=a.source, channels=a.channels,
                        samples=a.samples, fs=a.fs, f_start=a.f_start,
                        f_end=a.f_end, pre_silence=a.pre_silence,
                        post_silence=a.post_silence, cal=a.cal,
                        smoothing=a.smoothing, device=a.device, rig=a.rig,
                        mic=a.mic,
                        save_dir=(a.save_dir
                                  or default_save_base()),
                        mute_others=a.mute_others, auto_level=a.auto_level,
                        raw_capture_dump=a.raw_capture_dump)
    ses = MeasureSession(cfg)
    for w in ses.precondition_notes:
        print(w, file=sys.stderr)

    si, so = ses.sink_ident, ses.source_ident
    v0, sw = ses.volume_start, ses.sweep
    print("Target sink : %s (%s, id %s, %s)"
          % (si["description"] or si["name"], si["name"], si["id"],
             si["device_api"]))
    print("Mic source  : %s (id %s)"
          % (so["description"] or so["name"], so["id"]))
    print("Sink volume : %s (will NOT be changed%s)"
          % ("%.0f%%" % (100 * v0) if v0 is not None else "unknown",
             "" if not a.auto_level else "; --auto-level may raise it"))
    print("Sweep       : %.2f s, %g-%g Hz @ %g dBFS digital, stream "
          "volume 1.0, %d take(s)"
          % (sw.duration_s, sw.f_start, sw.f_end, sw.level_dbfs, a.takes))
    if not confirm("The sweep WILL PLAY on this device at the volume "
                   "above. Proceed?", a.yes):
        print("declined", file=sys.stderr)
        return 3
    if a.auto_level:
        if not confirm("--auto-level will adjust the sink volume "
                       "(start quiet at %.0f%%, up to %d raises, "
                       "target SNR >= %g dB at a peak below %g dBFS). "
                       "Proceed?"
                       % (100 * min(v0 or 1.0, AUTO_START_VOLUME),
                          AUTO_MAX_ADJUST, mc.SNR_WARN_DB,
                          AUTO_PEAK_CEIL), a.yes):
            print("declined", file=sys.stderr)
            return 3

    with ses:
        print("Artifacts   : %s" % ses.outdir)
        accepted = 0
        while accepted < a.takes:
            if accepted:
                pause_reseat(accepted + 1, a.takes)
            try:
                out = ses.take(a.channel)
            except FaultyCaptureError as e:
                raise MeasureError(
                    "%s Check that channel %d of the mic is wired/seated, "
                    "or point --channel at the live one (a %d-channel "
                    "capture exposes 0..%d)."
                    % (e, e.channel, e.channels, e.channels - 1))
            pk = out.take.peak_dbfs if out.take else out.level["peak_dbfs"]
            snr = (out.take.snr_db if out.take
                   else (out.level or {}).get("snr_db"))
            print("take %d/%d: capture peak %.1f dBFS, SNR %s"
                  % (accepted + 1, a.takes, pk,
                     "%.1f dB" % snr if snr is not None else "n/a"))
            for n in out.notes:
                print(n, file=sys.stderr)
            if out.kind == "level_probe":
                lv = out.level
                print("auto-level: sink volume %.0f%% -> %.0f%% "
                      "(step %d/%d), retrying the take"
                      % (100 * lv["volume_from"], 100 * lv["volume_to"],
                         lv["step"], lv["max_steps"]))
                continue
            if out.kind == "level_stuck":
                if not confirm("Continue at the current level anyway?",
                               a.yes):
                    raise MeasureError("aborted: %s"
                                       % (out.level or {}).get(
                                           "why", "leveling gave up"))
                out = ses.accept_level()
                for n in out.notes:
                    print(n, file=sys.stderr)
            accepted += 1

    out_path = a.out or os.path.join(ses.outdir, "result.json")
    result = ses.finalize(a.channel, out_path)

    t = result["takes"]
    print("%s: %d take(s), delay jitter %.2f ms, SNR min %s dB"
          % (out_path, t["count"], t["delay_jitter_ms"],
             t["snr_min_db"] if t["snr_min_db"] is not None else "n/a"))
    for w in result["warnings"]:
        print("WARNING: %s" % w)
    if ses.eq_state["restored"] is False:
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
                   help="adjust the sink volume until the capture is "
                        "clean (SNR >= %g dB) at a peak below %g dBFS "
                        "(max %d raises, confirmed); refuses when the "
                        "noise floor makes that impossible"
                        % (mc.SNR_WARN_DB, AUTO_PEAK_CEIL,
                           AUTO_MAX_ADJUST))
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
