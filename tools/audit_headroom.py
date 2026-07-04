#!/usr/bin/env python3
"""Offline headroom audit: pre-EQ capture x profile -> post-EQ verdict.

This is the prototype of per-device-eq "Check headroom" (Task 2 tier 3).
The sink monitor taps PRE-EQ in the in-node topology, so post-EQ level
must be computed, not tapped: we run the capture through the exact RBJ
biquads of the profile and report the peak the quantizer will see.

Usage:
    audit_headroom.py capture.wav profile.json
    audit_headroom.py capture.wav --demo          # investigation FL/FR chains
    audit_headroom.py --dump-demo > profile.json
"""
import json
import sys
import numpy as np
import soundfile as sf

from pde_audit import DEMO_PROFILE, apply_chain, clip_stats, dbfs, load_profile

CH_NAMES = ["FL", "FR", "FC", "LFE", "RL", "RR"]


def main():
    args = sys.argv[1:]
    if args == ["--dump-demo"]:
        json.dump(DEMO_PROFILE, sys.stdout, indent=2)
        return
    if len(args) != 2:
        sys.exit(__doc__)
    capture, prof_arg = args
    profile = DEMO_PROFILE if prof_arg == "--demo" else load_profile(prof_arg)

    x, fs = sf.read(capture, dtype="float64", always_2d=True)
    preamp = profile.get("preamp", 0.0)
    worst = float("-inf")
    for ch in range(x.shape[1]):
        name = CH_NAMES[ch] if ch < len(CH_NAMES) else f"ch{ch}"
        bands = profile["channels"].get(name)
        if bands is None:
            print(f"{name}: no bands in profile, skipped")
            continue
        pre = x[:, ch]
        post = apply_chain(pre, bands, fs, preamp)
        n, pct, runs = clip_stats(post)
        pk = dbfs(post)
        worst = max(worst, pk)
        print(
            f"{name}: pre {dbfs(pre):+6.2f} -> post-EQ {pk:+6.2f} dBFS"
            f" | >=FS: {n} ({pct:.3f}%), {runs} episodes"
        )
    rec = max(0.0, worst)
    print(f"recommended preamp: {-rec:.1f} dB"
          + ("  (no change needed)" if rec == 0 else ""))


if __name__ == "__main__":
    main()
