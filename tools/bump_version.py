#!/usr/bin/env python3
"""One version, bumped once.

Three files carry the app's version and the field kept finding
them disagreeing: perdeviceeq/__init__.py (__version__ -- the
About dialog and the export wizard read it at runtime), the RPM
spec (Version:), and the AppStream metainfo (the newest
<release> entry, which Flathub requires). This tool converges
all three to one number in one command:

    tools/bump_version.py 3.2.0 Add export to external PEQ/GEQ

Everything after the version is the release summary, one
sentence, required when a NEW metainfo release entry is born.
Re-running with the current version refreshes the entry's date
(and its summary, when given) instead of duplicating it -- bump
right before `gh release create vX.Y.Z` and the tag ships files
that already carry the number.

tests/test_version_sync.py is the ratchet: a tree whose three
carriers disagree cannot merge, so the discipline stops being a
memory exercise.
"""
import datetime
import os
import re
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__),
                                     ".."))
SPEC = "per-device-eq.spec"
META = os.path.join("data", "io.github.ntman.PerDeviceEQ."
                            "metainfo.xml")
INIT = os.path.join("perdeviceeq", "__init__.py")


def _rw(path, pattern, repl, what):
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    out, n = re.subn(pattern, repl, src, count=1)
    if n != 1:
        raise ValueError("could not find %s in %s" % (what, path))
    if out != src:
        with open(path, "w", encoding="utf-8") as f:
            f.write(out)
        return True
    return False


def _xml_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def bump(root, version, summary=None, today=None):
    """Converge the three carriers to `version`. Returns the list
    of files actually rewritten."""
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("version must be X.Y.Z, got %r" % version)
    date = today or datetime.date.today().isoformat()
    changed = []

    p = os.path.join(root, INIT)
    if _rw(p, r'__version__ = "[^"]+"',
           '__version__ = "%s"' % version, "__version__"):
        changed.append(INIT)

    p = os.path.join(root, SPEC)
    if _rw(p, r"(?m)^Version:(\s+)\S+$",
           lambda m: "Version:%s%s" % (m.group(1), version),
           "Version:"):
        changed.append(SPEC)

    p = os.path.join(root, META)
    with open(p, "r", encoding="utf-8") as f:
        src = f.read()
    m = re.search(r'(?m)^(\s*)<release version="([^"]+)" '
                  r'date="[^"]+">', src)
    if m is None:
        raise ValueError("no <release> entry in %s" % META)
    indent, newest = m.group(1), m.group(2)
    if newest == version:
        out = src[:m.start()] + '%s<release version="%s" ' \
              'date="%s">' % (indent, version, date) \
              + src[m.end():]
        if summary:
            head = out[:m.start()]
            tail = out[m.start():]
            tail, n = re.subn(r"<p>.*?</p>",
                              "<p>%s</p>" % _xml_escape(summary),
                              tail, count=1, flags=re.S)
            if n == 1:
                out = head + tail
    else:
        if not summary:
            raise ValueError(
                "a new release entry needs a summary: "
                "bump_version.py %s <one sentence>" % version)
        block = ('{i}<release version="{v}" date="{d}">\n'
                 "{i}  <description>\n"
                 "{i}    <p>{s}</p>\n"
                 "{i}  </description>\n"
                 "{i}</release>\n").format(
            i=indent, v=version, d=date, s=_xml_escape(summary))
        out = src[:m.start()] + block + src[m.start():]
    if out != src:
        with open(p, "w", encoding="utf-8") as f:
            f.write(out)
        changed.append(META)
    return changed


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    version, summary = argv[1], " ".join(argv[2:]) or None
    try:
        changed = bump(ROOT, version, summary)
    except ValueError as e:
        print("bump_version: %s" % e, file=sys.stderr)
        return 1
    for f in changed:
        print("  bump  %s" % f)
    if not changed:
        print("  keep  everything already at %s" % version)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
