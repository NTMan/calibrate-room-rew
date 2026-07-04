"""Shared pytest plumbing for per-device-eq audit tests.

Fixtures are generated on the fly by tools/make_fixtures.py (deterministic,
seed-pinned) — no binary test data is stored in git.
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))


@pytest.fixture(scope="session")
def fixtures_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("fixtures")
    subprocess.run(
        [sys.executable, str(ROOT / "tools" / "make_fixtures.py"), str(out)],
        check=True,
        capture_output=True,
    )
    return out
