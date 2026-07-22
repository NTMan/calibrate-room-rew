"""One version across the tree -- the ratchet. Three files carry
the app's version (perdeviceeq/__init__.py, the RPM spec, the
newest metainfo release) and the field kept catching them apart:
3.1.0 in About, 3.2.0 in the spec, 3.0.0 in metainfo, all at
once. tools/bump_version.py converges them in one command; this
test makes a diverged tree unmergeable, so the discipline stops
being a memory exercise. Regex over the files, no imports -- the
sandbox has no GTK and needs none here."""
import re
from pathlib import Path

import bump_version as bv

ROOT = Path(__file__).resolve().parent.parent


def _init_version(root=ROOT):
    s = (root / "perdeviceeq" / "__init__.py").read_text()
    return re.search(r'__version__ = "([^"]+)"', s).group(1)


def _spec_version(root=ROOT):
    s = (root / "per-device-eq.spec").read_text()
    return re.search(r"(?m)^Version:\s+(\S+)$", s).group(1)


def _meta_version(root=ROOT):
    s = (root / "data"
         / "io.github.ntman.PerDeviceEQ.metainfo.xml").read_text()
    return re.search(r'<release version="([^"]+)"', s).group(1)


def test_one_version_everywhere():
    v = _init_version()
    assert _spec_version() == v
    assert _meta_version() == v


def _tree(tmp_path):
    (tmp_path / "perdeviceeq").mkdir()
    (tmp_path / "perdeviceeq" / "__init__.py").write_text(
        '"""pkg"""\n__version__ = "1.0.0"\n')
    (tmp_path / "per-device-eq.spec").write_text(
        "Name: per-device-eq\nVersion:        1.0.0\n"
        "Release: 1\n")
    (tmp_path / "data").mkdir()
    (tmp_path / "data"
     / "io.github.ntman.PerDeviceEQ.metainfo.xml").write_text(
        "<component>\n  <releases>\n"
        '    <release version="1.0.0" date="2026-01-01">\n'
        "      <description>\n        <p>First.</p>\n"
        "      </description>\n    </release>\n"
        "  </releases>\n</component>\n")
    return tmp_path


def test_bump_converges_all_three(tmp_path):
    root = _tree(tmp_path)
    changed = bv.bump(str(root), "2.0.0", "Second coming",
                      today="2026-07-22")
    assert len(changed) == 3
    assert _init_version(root) == "2.0.0"
    assert _spec_version(root) == "2.0.0"
    assert _meta_version(root) == "2.0.0"
    meta = (root / "data"
            / "io.github.ntman.PerDeviceEQ.metainfo.xml"
            ).read_text()
    assert "<p>Second coming</p>" in meta
    assert '<release version="1.0.0"' in meta   # history kept


def test_bump_rerun_refreshes_not_duplicates(tmp_path):
    root = _tree(tmp_path)
    bv.bump(str(root), "2.0.0", "Second", today="2026-07-01")
    bv.bump(str(root), "2.0.0", "Second, dated",
            today="2026-07-22")
    meta = (root / "data"
            / "io.github.ntman.PerDeviceEQ.metainfo.xml"
            ).read_text()
    assert meta.count('version="2.0.0"') == 1     # no duplicate
    assert 'date="2026-07-22"' in meta            # date refreshed
    assert "<p>Second, dated</p>" in meta


def test_bump_new_entry_demands_a_summary(tmp_path):
    root = _tree(tmp_path)
    try:
        bv.bump(str(root), "2.0.0")
    except ValueError as e:
        assert "summary" in str(e)
    else:
        raise AssertionError("summaryless new release accepted")
