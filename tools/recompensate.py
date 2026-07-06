#!/usr/bin/env python3
"""Re-apply a different mic calibration to an existing measurement.

The mic cal is a per-frequency dB offset the core SUBTRACTS from the
deconvolved response, so switching the miniDSP compensation (HEQ <-> IDF
<-> RAW <-> HPN) is just arithmetic on the stored curve -- no re-measuring.
This is why measuring an over-ear vs an in-ear, or comparing two
headphones in one reference, does not need a fresh sweep per cal.

It reads the uncalibrated magnitude the core now stores (data.mag_db_uncal)
and applies the new cal. For older result.json written before that field
existed, pass --from-cal with the cal the file was measured through (its
name is in cal_file) so the old offset can be added back first.

  recompensate.py --result liberty5_L.json --cal L_RAW_8603052.txt \
      --out liberty5_L_raw.json
  recompensate.py --result old.json --from-cal L_HEQ_8603052.txt \
      --cal L_IDF_8603052.txt --out new.json
"""
import argparse
import copy
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import measure_core as mc                             # noqa: E402


def main(argv):
    p = argparse.ArgumentParser(description="re-apply a mic cal to a "
                                            "measurement without re-measuring")
    p.add_argument("--result", required=True, help="input result.json")
    p.add_argument("--cal", required=True, help="new mic cal file to apply")
    p.add_argument("--from-cal", help="cal the input was measured through "
                                      "(only needed if it lacks mag_db_uncal)")
    p.add_argument("--out", required=True, help="output result.json")
    a = p.parse_args(argv)

    r = json.load(open(a.result))
    d = r["data"]
    freqs = np.asarray(d["freq_hz"], float)

    if "mag_db_uncal" in d and d["mag_db_uncal"] is not None:
        uncal = np.asarray(d["mag_db_uncal"], float)
    elif a.from_cal:
        # add the old offset back: stored = uncal - old_cal  ->  uncal =
        # stored + old_cal
        of, od = mc.load_mic_cal(a.from_cal)
        uncal = np.asarray(d["mag_db_raw"], float) \
            + np.interp(np.log(freqs), np.log(of), od)
    else:
        p.error("this result.json has no mag_db_uncal; pass --from-cal with "
                "the cal it was measured through (%s)"
                % r.get("cal_file", "unknown"))

    nf, nd = mc.load_mic_cal(a.cal)
    avg = mc.apply_mic_cal(freqs, uncal, nf, nd)
    ppo = r["grid"]["ppo"]
    frac = r["smoothing"]["fraction"]
    smoothed = mc.smooth_fractional_octave(avg, ppo, frac)

    out = copy.deepcopy(r)
    out["cal_file"] = os.path.basename(a.cal)
    out["data"]["mag_db_raw"] = [float(x) for x in avg]
    out["data"]["mag_db_smoothed"] = [float(x) for x in smoothed]
    out["data"]["mag_db_uncal"] = [float(x) for x in uncal]
    out.setdefault("warnings", [])
    note = "recompensated from %s to %s" % (
        r.get("cal_file", "?"), os.path.basename(a.cal))
    if note not in out["warnings"]:
        out["warnings"].append(note)

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print("%s: re-applied %s (was %s)"
          % (a.out, os.path.basename(a.cal), r.get("cal_file", "?")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
