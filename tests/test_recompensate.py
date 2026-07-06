# -*- coding: utf-8 -*-
"""Tests for tools/recompensate.py: switching the mic cal on a stored
measurement is exact arithmetic, works from mag_db_uncal, and falls back
to --from-cal for older files that lack it."""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import measure_core as mc                             # noqa: E402


def _cal(path, pairs):
    path.write_text("* test cal\nSens Factor =-1.0dB\n"
                    + "\n".join("%g %g" % (f, d) for f, d in pairs))


def _result(freqs, uncal, cal_curve):
    # emulate the core: stored raw = uncal - cal
    raw = uncal - cal_curve
    return {"schema": "pde-measurement", "schema_version": 3,
            "cal_file": "OLD.txt", "grid": {"f_lo": 20, "f_hi": 20000,
                                            "ppo": 24},
            "smoothing": {"fraction": 6}, "warnings": [],
            "data": {"freq_hz": [float(x) for x in freqs],
                     "mag_db_raw": [float(x) for x in raw],
                     "mag_db_smoothed": [float(x) for x in raw],
                     "mag_db_uncal": [float(x) for x in uncal],
                     "spread_db": None}}


def _run(args):
    return subprocess.run([sys.executable, str(ROOT / "tools" /
                          "recompensate.py")] + args, capture_output=True,
                          text=True, timeout=60)


def test_recompensate_from_uncal_is_exact(tmp_path):
    freqs = mc.log_grid(20, 20000, 24)
    rng = np.random.default_rng(0)
    uncal = rng.normal(0, 5, len(freqs))
    oldc = tmp_path / "OLD.txt"
    newc = tmp_path / "NEW.txt"
    _cal(oldc, [(20, 1.0), (1000, -2.0), (20000, 3.0)])
    _cal(newc, [(20, -1.0), (1000, 0.5), (20000, -4.0)])
    old_curve = np.interp(np.log(freqs), np.log([20, 1000, 20000]),
                          [1.0, -2.0, 3.0])
    res = tmp_path / "r.json"
    res.write_text(json.dumps(_result(freqs, uncal, old_curve)))
    out = tmp_path / "o.json"
    r = _run(["--result", str(res), "--cal", str(newc), "--out", str(out)])
    assert r.returncode == 0, r.stderr
    got = np.asarray(json.loads(out.read_text())["data"]["mag_db_raw"])
    want = mc.apply_mic_cal(freqs, uncal, *mc.load_mic_cal(str(newc)))
    assert np.allclose(got, want, atol=1e-9)


def test_reapplying_same_cal_round_trips(tmp_path):
    freqs = mc.log_grid(20, 20000, 24)
    uncal = np.linspace(-8, 4, len(freqs))
    calf = tmp_path / "SAME.txt"
    _cal(calf, [(20, 2.0), (1000, -1.0), (20000, 5.0)])
    curve = np.interp(np.log(freqs), np.log([20, 1000, 20000]),
                      [2.0, -1.0, 5.0])
    res = tmp_path / "r.json"
    r0 = _result(freqs, uncal, curve)
    res.write_text(json.dumps(r0))
    out = tmp_path / "o.json"
    assert _run(["--result", str(res), "--cal", str(calf),
                 "--out", str(out)]).returncode == 0
    got = np.asarray(json.loads(out.read_text())["data"]["mag_db_raw"])
    assert np.allclose(got, np.asarray(r0["data"]["mag_db_raw"]), atol=1e-9)


def test_fallback_from_cal_when_uncal_missing(tmp_path):
    freqs = mc.log_grid(20, 20000, 24)
    uncal = np.linspace(0, 6, len(freqs))
    oldc = tmp_path / "OLD.txt"
    newc = tmp_path / "NEW.txt"
    _cal(oldc, [(20, 1.0), (1000, 2.0), (20000, -1.0)])
    _cal(newc, [(20, -2.0), (1000, 0.0), (20000, 3.0)])
    old_curve = np.interp(np.log(freqs), np.log([20, 1000, 20000]),
                          [1.0, 2.0, -1.0])
    r0 = _result(freqs, uncal, old_curve)
    del r0["data"]["mag_db_uncal"]                    # older file
    res = tmp_path / "r.json"
    res.write_text(json.dumps(r0))
    out = tmp_path / "o.json"
    # without --from-cal it must refuse, not guess
    assert _run(["--result", str(res), "--cal", str(newc),
                 "--out", str(out)]).returncode != 0
    # with it, the recovered curve matches a direct uncal apply
    assert _run(["--result", str(res), "--from-cal", str(oldc),
                 "--cal", str(newc), "--out", str(out)]).returncode == 0
    got = np.asarray(json.loads(out.read_text())["data"]["mag_db_raw"])
    want = mc.apply_mic_cal(freqs, uncal, *mc.load_mic_cal(str(newc)))
    assert np.allclose(got, want, atol=1e-9)
