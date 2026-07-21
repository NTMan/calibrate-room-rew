#!/usr/bin/env python3
"""Walk a LIVE widget tree and run the mechanical HIG floor.

The rules live in perdeviceeq.hig and run on plain dicts; this
tool is the thin GTK side: describe() maps a realized widget
tree into that shape, main() builds what it can of the app and
lints it. Needs a display -- in CI that is xvfb:

    xvfb-run -a python3 tools/hig_audit.py --peq-view

--peq-view audits the band card (PeqView) standalone -- the
surface that minted rules H2 and H3 in the field. --window
attempts the full main window and is expected to grow teeth on
the CI day, when the PipeWire shims from tests/shims ride along.

Exit code 1 on any finding; the findings name rule, path, and a
suggested replacement, because an audit that cannot propose a
fix is a complaint.
"""

import sys

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw  # noqa: E402

sys.path.insert(0, ".")
from perdeviceeq import hig  # noqa: E402

_BARS = ("GtkHeaderBar", "AdwHeaderBar", "GtkActionBar",
         # the bench's second legal home for flat chrome: a
         # suffix inside a boxed row IS the row's own control
         "AdwActionRow", "AdwEntryRow", "AdwExpanderRow")

_ALIGN = {Gtk.Align.FILL: "fill", Gtk.Align.START: "start",
          Gtk.Align.CENTER: "center", Gtk.Align.END: "end"}


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
    return {"class": t, "props": props,
            "children": [describe(c, in_bar) for c in _kids(w)]}


def audit_widget(root):
    """(findings, lines) for a realized widget."""
    findings = hig.lint(describe(root))
    return findings, hig.report(findings)


def _peq_view():
    from perdeviceeq.peq_view import PeqView
    return PeqView(lambda *a, **k: None,
                   on_import_file=lambda: None)


def main(argv):
    Adw.init()
    if "--peq-view" in argv:
        root = _peq_view()
    elif "--window" in argv:
        print("hig_audit: --window lands with the CI day "
              "(needs the PipeWire shims and an Adw.Application "
              "activation cycle)", file=sys.stderr)
        return 2
    else:
        print(__doc__)
        return 2
    win = Gtk.Window()
    win.set_child(root)
    findings, lines = audit_widget(root)
    for ln in lines:
        print(ln)
    print("hig_audit: %d finding%s"
          % (len(findings), "" if len(findings) == 1 else "s"))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
