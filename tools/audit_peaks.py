#!/usr/bin/env python3
"""Peak / clip statistics of a capture.

Usage:
    audit_peaks.py capture.wav [capture2.wav ...]

Captures must be float32 (pw-record --format f32), otherwise over-FS
peaks were already destroyed by the recording itself.
"""
import os
import sys
import soundfile as sf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from perdeviceeq.pde_audit import dbfs, clip_stats    # noqa: E402


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for path in sys.argv[1:]:
        info = sf.info(path)
        x, fs = sf.read(path, dtype="float64", always_2d=True)
        print(f"{path}: {len(x)/fs:.1f}s @ {fs} Hz, {info.subtype}")
        if "FLOAT" not in info.subtype:
            print("  WARNING: not float — over-FS peaks are unrecoverable")
        for ch in range(x.shape[1]):
            n, pct, runs = clip_stats(x[:, ch])
            print(
                f"  ch{ch}: peak {dbfs(x[:, ch]):+6.2f} dBFS"
                f" | >=FS: {n} ({pct:.3f}%), {runs} episodes"
            )


if __name__ == "__main__":
    main()
