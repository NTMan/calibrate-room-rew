#!/usr/bin/env python3
"""Walk a LIVE widget tree and run the mechanical HIG floor.

The rules live in perdeviceeq.hig and run on plain dicts; this
tool is the thin GTK side: describe() maps a realized widget
tree into that shape, main() builds what it can of the app and
lints it. The snapshots present nothing; the H9 walk phase at
the end DOES present both windows for live focus, so a local
run flashes them briefly -- the price of a keyboard the tool
can press. It still runs on your session display as-is:

    python3 tools/hig_audit.py --peq-view

Headless CI has no session; there, and only there, wrap it:

    xvfb-run -a python3 tools/hig_audit.py --peq-view

--peq-view audits the band card (PeqView) standalone -- the
surface that minted rules H2 and H3 in the field. --window
builds the REAL windows inside an application cycle and audits
both trees: the main window header to band card, then the
Measure window opened through the same door the Edit button
uses (born-gone design means it needs no live sink); it needs
a PipeWire to talk to -- the live one on a workstation, or the
shims:

    PATH="$PWD/tests/shims:$PATH" \
        xvfb-run -a python3 tools/hig_audit.py --window

Nothing is asked and nothing is shown: the audit application
overrides activation, skips the first-run dialog and the
present, walks the tree and quits.

Exit code 1 on any finding; the findings name rule, path, and a
suggested replacement, because an audit that cannot propose a
fix is a complaint.
"""

import os
import sys

# The walk phase presents the windows (live focus needs mapped
# widgets), and presenting wakes GSK's renderer; CI containers
# ship no libGLESv2 and the GL path aborts. The audit needs
# correctness, not frames -- default to cairo, overridable.
os.environ.setdefault("GSK_RENDERER", "cairo")

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio, GLib  # noqa: E402

sys.path.insert(0, ".")
from perdeviceeq import hig  # noqa: E402

_BARS = ("GtkHeaderBar", "AdwHeaderBar", "GtkActionBar",
         # the bench's second legal home for flat chrome: a
         # suffix inside a boxed row IS the row's own control
         "AdwActionRow", "AdwEntryRow", "AdwExpanderRow")

_ALIGN = {Gtk.Align.FILL: "fill", Gtk.Align.START: "start",
          Gtk.Align.CENTER: "center", Gtk.Align.END: "end"}

# Children the toolkit assembles are not the app's to answer
# for: the floor judges what WE placed. A sealed widget keeps
# its subtree to itself (a SpinButton's +/- chrome, a
# DropDown's innards, the window controls, the back button).
_SEALED = ("GtkSpinButton", "GtkDropDown", "GtkWindowControls",
           "AdwSheetControls", "AdwBackButton",
           # assembled from properties end to end, nothing in it
           # is placed by the app
           "AdwAboutDialog")


def _own_children(w, t):
    """The children the app placed. A MenuButton contributes
    only its popover, an AlertDialog only its extra child --
    that content is ours, the rest is assembled by the toolkit
    from properties and is the toolkit's to answer for."""
    if t in _SEALED:
        return []
    if isinstance(w, Gtk.MenuButton):
        return [c for c in _kids(w)
                if isinstance(c, Gtk.Popover)]
    if isinstance(w, Adw.AlertDialog):
        extra = w.get_extra_child()
        return [extra] if extra is not None else []
    return list(_kids(w))


def _gtype(w):
    try:
        return type(w).__gtype__.name
    except Exception:
        return type(w).__name__


def _kids(w):
    c = w.get_first_child()
    while c is not None:
        yield c
        c = c.get_next_sibling()


def _button_face(w):
    """(label, icon_only) for a button, looking through custom
    icon+label children the way a reader's eye does."""
    label = None
    if hasattr(w, "get_label"):
        label = w.get_label()
    has_icon = False
    stack = list(_kids(w))
    while stack:
        c = stack.pop()
        t = _gtype(c)
        if t == "GtkImage":
            has_icon = True
        elif t == "GtkLabel" and not label:
            label = c.get_label()
        stack.extend(_kids(c))
    return label, (has_icon and not label)


def describe(w, in_bar=False):
    """The neutral tree perdeviceeq.hig lints: class, the props
    the rules read, children. Unknown stays absent -- the floor
    accuses only on evidence."""
    t = _gtype(w)
    css = list(w.get_css_classes())
    in_bar = (in_bar or t in _BARS or "card-header" in css
              or "toolbar" in css)
    props = {"css": css,
             "in_bar": in_bar,
             "tooltip": w.get_tooltip_text(),
             "halign": _ALIGN.get(w.get_halign()),
             "margins": [w.get_margin_top(),
                         w.get_margin_bottom(),
                         w.get_margin_start(),
                         w.get_margin_end()]}
    if isinstance(w, Gtk.Box):
        props["spacing"] = w.get_spacing()
    if isinstance(w, Gtk.Button):
        label, icon_only = _button_face(w)
        props["label"] = label
        props["icon_only"] = icon_only
    elif isinstance(w, Gtk.MenuButton):
        # not a Gtk.Button subclass, but H4 judges it by face:
        # an icon-only menu opener needs a tooltip too
        label = w.get_label()
        props["label"] = label
        props["icon_only"] = bool(w.get_icon_name()) and not label
    return {"class": t, "props": props,
            "children": [describe(c, in_bar)
                         for c in _own_children(w, t)]}


class _FixtureRec:
    """A take record with every field the row builder reads --
    plain lists for the curves, no numpy, no audio."""

    def __init__(self, i, snr, peak, noise, clipped):
        self.id = i
        self.snr_db = snr
        self.peak_dbfs = peak
        self.noise_dbfs = noise
        self.clipped = clipped
        self.repaired = 0
        self.wav_path = None
        self.created_utc = "2026-07-26T00:00:00"
        self.freq_hz = [20.0 * (1000.0 ** (k / 63.0))
                        for k in range(64)]
        self.mag_db = [((k % 16) - 8) * 0.3 + i
                      for k in range(64)]


class _FixtureSession:
    """The architect's word: mocks, so the take rows exist
    before the floor. Three takes, three verdicts -- clean,
    flagged, clipped -- through the REAL row builder; a lint
    tool never plays a sweep."""

    def __init__(self):
        self._recs = [
            _FixtureRec(1, 41.0, -1.2, -55.0, 0),
            _FixtureRec(2, 5.0, -1.5, -40.0, 0),
            _FixtureRec(3, None, -0.1, None, 1),
        ]

    def takes_of(self, _ch):
        return list(self._recs)

    def average_and_spread(self, _ch):
        return None, None

    def comp_shift_db(self, _ch):
        return None


def _stop_name(w):
    """A short human handle for a focus stop."""
    name = w.__class__.__name__
    tip = None
    if hasattr(w, "get_tooltip_text"):
        tip = w.get_tooltip_text()
    if not tip and hasattr(w, "get_label"):
        try:
            tip = w.get_label()
        except Exception:
            tip = None
    return "%s(%s)" % (name, (tip or "")[:24])


def _lineage(w, depth=3):
    """ClassA/ClassB/Widget -- enough ancestry to find a
    nameless stop in the source."""
    parts = [w.__gtype__.name]
    p = w.get_parent()
    while p is not None and len(parts) < depth:
        parts.append(p.__gtype__.name)
        p = p.get_parent()
    return "/".join(reversed(parts))


def _coverage(win, visited):
    """Reachable focus stops the walk never met. A candidate is
    an outermost focusable (no focusable ancestor); it counts as
    visited when focus landed on it or anywhere inside it. The
    descent honors the same ownership seals as the snapshot --
    the first live run flagged a toolkit-internal button the
    static walk would never have judged."""
    missing = []

    def rec(w, under):
        t = w.__gtype__.name
        mine = (w is not win and w.get_focusable()
                and w.get_mapped() and w.get_sensitive())
        if mine and not under:
            hit = any(v is w or v.is_ancestor(w)
                      for v in visited)
            if not hit:
                missing.append(w)
        for c in _own_children(w, t):
            rec(c, under or mine)

    rec(win, False)
    if not missing:
        return []
    names = ", ".join("%s [%s]" % (_stop_name(w), _lineage(w))
                      for w in missing[:6])
    if len(missing) > 6:
        names += ", ..."
    return [{
        "rule": "H9",
        "path": "%s (keyboard walk)" % win.__class__.__name__,
        "msg": "the walk wraps but skips %d reachable "
               "stop%s: %s"
               % (len(missing),
                  "" if len(missing) == 1 else "s", names),
        "fix": "re-aim the forward neighbor to include them, "
               "or check what pulls focus past the group"}]


def _tab_walk(win, limit=400):
    """H9, the dynamic half of the floor: the Tab walk must
    round the room. The audit presses Tab itself, records the
    stops, and whistles when the walk repeats a stop without
    ever returning to the first -- a trapped subset, the loop
    class the field caught (play -> auto-level). A full wrap
    (back to stop one) is the healthy shape; a walk that never
    repeats within the limit is a finding too."""
    win.set_focus(None)
    seen = []
    index = {}
    for _ in range(limit):
        moved = win.child_focus(Gtk.DirectionType.TAB_FORWARD)
        f = win.get_focus()
        if not moved or f is None:
            return []          # walked out cleanly
        key = id(f)
        if key in index:
            if index[key] == 0:
                # full wrap -- now prove it ROUNDED the room:
                # every reachable stop must have been visited
                # (the field's second lesson: a wrap that skips
                # the takes list is round and still wrong)
                return _coverage(win, seen)
            cycle = seen[index[key]:]
            names = " -> ".join(_stop_name(w)
                                for w in cycle[:6])
            if len(cycle) > 6:
                names += " ..."
            return [{
                "rule": "H9",
                "path": "%s (keyboard walk)"
                        % win.__class__.__name__,
                "msg": "Tab traps in a cycle of %d stops "
                       "away from the walk's start: %s"
                       % (len(cycle), names),
                "fix": "give the trapping container focus "
                       "neighbors (focus.set_focus_neighbors) "
                       "or drop the custom jump"}]
        index[key] = len(seen)
        seen.append(f)
    return [{
        "rule": "H9",
        "path": "%s (keyboard walk)" % win.__class__.__name__,
        "msg": "Tab walk did not settle in %d steps" % limit,
        "fix": "the focus graph never repeats or exits; check "
               "dynamic focus handlers"}]


def audit_widget(root):
    """(findings, lines) for a realized widget."""
    findings = hig.lint(describe(root))
    return findings, hig.report(findings)


def _peq_view():
    from perdeviceeq.peq_view import PeqView
    return PeqView(lambda *a, **k: None,
                   on_import_file=lambda: None)


def _window_audit():
    """Build the real EqWindow inside an application cycle and
    audit it. NON_UNIQUE keeps the audit local even when the
    real app is running on the same session -- without it the
    second instance remote-activates the first and this
    function would audit nothing."""
    from perdeviceeq import gui as G
    out = {}

    from perdeviceeq.measure_window import MeasureWindow

    class AuditApp(G.EqApplication):
        def do_activate(self):
            self.win = G.EqWindow(self)

            def grab_measure():
                # the take rows join the judged tree: a
                # fixture session mounted before the snapshot,
                # rows born through the real builder
                self.mwin.session = _FixtureSession()
                self.mwin._selected_ch = 0
                self.mwin._rebuild_page()
                # the cal-history door: a hand-built dialog,
                # mounted into THIS window's host; on the
                # audit's empty canvas it shows its honest
                # empty list, which is a UI state too
                self._dlgs.append(self.mwin._open_cal_manager())
                f2, _ = audit_widget(self.mwin)
                f1, _ = out["result"]
                seen = set()
                fs = []
                for f in f1 + f2:
                    key = (f.get("rule"), f.get("path"),
                           f.get("msg"))
                    if key in seen:
                        continue
                    seen.add(key)
                    fs.append(f)
                out["result"] = (fs, None)
                # the walk phase: H9 needs LIVE focus, so the
                # door dialogs close (a modal dialog owns the
                # window's focus and would be all the walk
                # sees) and the windows present
                for d in self._dlgs:
                    if d is not None:
                        d.force_close()
                self.win.present()
                self.mwin.present()
                GLib.idle_add(walk_all)
                return False

            def walk_all():
                fs, _ = out["result"]
                fs = list(fs)
                fs += _tab_walk(self.win)
                fs += _tab_walk(self.mwin)
                out["result"] = (fs, hig.report(fs))
                self.quit()
                return False

            def grab():
                # the third door first: the command dialog (the
                # restart-WirePlumber window) mounts into this
                # window's AdwDialogHost, so one snapshot holds
                # the window and the dialog together
                self._dlgs = []
                self._dlgs.append(self.win._command_dialog(
                    "System integration",
                    "The hook is installed. Restart WirePlumber "
                    "to load it.",
                    "systemctl --user restart wireplumber"))
                # the fourth door: About (sealed type -- the
                # walk proves it builds and mounts)
                self._dlgs.append(self.win._on_about())
                # the fifth: the export wizard, targets page
                # plus EVERY target's preview page pushed --
                # NavigationView keeps the whole stack in the
                # tree, one snapshot judges them all; on the
                # audit's empty body the statuses speak their
                # honest refusals, which are UI states too
                from perdeviceeq.export_wizard import (
                    ExportDialog)
                from perdeviceeq import export_peq as xp
                xdlg = ExportDialog(self.win)
                xdlg.present(self.win)
                self._dlgs.append(xdlg)
                for t in xp.load_targets():
                    xdlg._on_target(None, t)
                f1, l1 = audit_widget(self.win)
                # the same window in its second costume: the
                # separated-channels mode populates the linked
                # FL|FR bar and the per-channel band cards --
                # strictly more widgets; the merge dedups what
                # both snapshots share
                try:
                    self.win.sep_switch.set_active(True)
                except Exception:
                    pass
                f2, l2 = audit_widget(self.win)
                out["result"] = (f1 + f2, l1 + l2)
                # the second door: the Measure window births on
                # any node (born-gone), unpresented, unresolved
                node = getattr(self.win, "node", "") or "audit"
                # Edit mode: the cal-manager door guards on an
                # edited profile, and Edit builds strictly more
                pid = getattr(self.win, "current_pid", None)
                self.mwin = MeasureWindow(self.win, node, node,
                                          edit_pid=pid)
                GLib.idle_add(grab_measure)
                return False
            GLib.idle_add(grab)

    app = AuditApp()
    app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
    app.run([])
    if "result" not in out:
        raise SystemExit("hig_audit: the activation cycle never "
                         "reached the audit")
    return out["result"]


def main(argv):
    if not Gtk.init_check():
        print("hig_audit: no display available -- run inside "
              "your session (nothing is shown), or under "
              "xvfb-run in headless CI", file=sys.stderr)
        return 2
    Adw.init()
    if "--peq-view" in argv:
        root = _peq_view()
        win = Gtk.Window()
        win.set_child(root)
        findings, lines = audit_widget(root)
    elif "--window" in argv:
        findings, lines = _window_audit()
    else:
        print(__doc__)
        return 2
    for ln in lines:
        print(ln)
    print("hig_audit: %d finding%s"
          % (len(findings), "" if len(findings) == 1 else "s"))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
