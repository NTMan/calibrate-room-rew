#!/usr/bin/env python3
"""Thin CLI wrapper: the fit logic lives in perdeviceeq/fit_peq.py so the
GUI can import it directly; this keeps `tools/fit_peq.py ...` working from
a checkout (adds the repo root to the path, then delegates to main)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from perdeviceeq.fit_peq import main                  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
