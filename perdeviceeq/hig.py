"""The mechanical floor of the HIG audit.

There is no complete mechanical judge of the GNOME HIG -- the
HIG is prose with judgment, and GNOME itself reviews Circle
apps with human eyes. What CAN be mechanized is a floor: rules
that hold deterministically over a widget tree, each with an id,
a message, and a suggested replacement. The floor RATCHETS:
every design round the review settles by hand gets minted into a
new rule here, so the auditor grows stricter with every dispute
instead of re-arguing it.

The rules run on a neutral tree description, not on GTK -- the
same shim philosophy the test suite uses for PipeWire. A node is
a plain dict:

    {"class": "GtkButton",         # GType name
     "props": {...},               # see describe() in the tool
     "children": [node, ...]}

tools/hig_audit.py walks a LIVE widget tree under a display and
produces this shape; the rules stay importable and testable in a
sandbox with no GTK at all. A finding names the rule, the path
into the tree, what is wrong, and what to do instead -- an audit
that cannot propose a fix is a complaint, not an audit.

Props the rules read (all optional; absent means unknown, and
unknown never fires a rule -- the floor accuses only on
evidence): label (str), icon_only (bool), tooltip (str), css
(list of style classes), spacing (int), halign (one of fill,
start, center, end), in_bar (bool -- inside a
header bar, toolbar, or an expander-style card header, css
class card-header, where flat is the container's own idiom),
responses
(list of {"id", "label"} for alert dialogs), margins (list of
four ints).

Ratchet provenance: H1-H3 were earned in the .pdeq sprint's
design rounds (see HIG.md); H4-H6 are the HIG's own letter;
H7 was minted in the gone-state round (loose prose and a
homebrew badge lost to the banner and plain insensitivity).

No GTK. Pure dicts in, findings out.
"""

# a button label longer than this is prose, not a glyph; flat
# stops reading as a control around here (field round three)
FLAT_LABEL_MAX = 12

# the HIG spacing grid: multiples of 6, with 3 for tight pairs
SPACING_GRID = (0, 3, 6, 12, 18, 24, 30, 36)

# dialog responses that name no action
_MUTE_VERBS = ("yes", "no")


def _findings_h1(node, path, out):
    """H1: linked means facets of ONE control."""
    css = node.get("props", {}).get("css") or []
    if "linked" not in css:
        return
    kids = node.get("children") or []
    classes = {k.get("class") for k in kids}
    if len(kids) < 2 or len(classes) > 1:
        out.append({
            "rule": "H1", "path": path,
            "msg": "linked group does not hold facets of one "
                   "control (%d children, classes: %s)"
                   % (len(kids), ", ".join(sorted(
                       c or "?" for c in classes)) or "none"),
            "fix": "unlink into a plain box (spacing 6), or make "
                   "the children the same control (undo/redo, "
                   "spin +/-)"})


def _findings_h2(node, path, out):
    """H2: an action group anchors to an edge, never floats."""
    kids = node.get("children") or []
    if len(kids) < 2 or not node.get("class", "").endswith("Box"):
        return
    if any(not k.get("class", "").endswith("Button")
           for k in kids):
        return
    css = node.get("props", {}).get("css") or []
    if "linked" in css or node.get("props", {}).get("in_bar"):
        return
    halign = node.get("props", {}).get("halign")
    if halign in ("center", "fill"):
        out.append({
            "rule": "H2", "path": path,
            "msg": "action row of %d buttons floats (halign=%s)"
                   % (len(kids), halign),
            "fix": "anchor it: halign start under lists and "
                   "tables, end for dialog-style confirm rows"})


def _findings_h3(node, path, out):
    """H3: flat lives in a structured container."""
    if not node.get("class", "").endswith("Button"):
        return
    p = node.get("props", {})
    css = p.get("css") or []
    label = p.get("label") or ""
    if ("flat" in css and not p.get("in_bar")
            and len(label) > FLAT_LABEL_MAX):
        out.append({
            "rule": "H3", "path": path,
            "msg": "flat button with a long label (%r) outside "
                   "a bar reads as text, not a control" % label,
            "fix": "drop the flat class (Calculator is the "
                   "precedent), shorten the label, or move the "
                   "action into a toolbar"})


def _findings_h4(node, path, out):
    """H4: an icon-only button describes its action."""
    if not node.get("class", "").endswith("Button"):
        return
    p = node.get("props", {})
    if p.get("icon_only") and not (p.get("tooltip") or "").strip():
        out.append({
            "rule": "H4", "path": path,
            "msg": "icon-only button without a tooltip",
            "fix": "set tooltip_text naming the action"})


def _findings_h5(node, path, out):
    """H5: spacing and margins sit on the 6px grid."""
    p = node.get("props", {})
    vals = []
    if isinstance(p.get("spacing"), int):
        vals.append(("spacing", p["spacing"]))
    for m in (p.get("margins") or []):
        if isinstance(m, int):
            vals.append(("margin", m))
    for kind, v in vals:
        if v not in SPACING_GRID:
            out.append({
                "rule": "H5", "path": path,
                "msg": "%s %d is off the 6px grid" % (kind, v),
                "fix": "use one of %s" % (SPACING_GRID,)})


def _findings_h6(node, path, out):
    """H6: dialog buttons name the action."""
    if not node.get("class", "").endswith("AlertDialog"):
        return
    for r in node.get("props", {}).get("responses") or []:
        lbl = (r.get("label") or "").strip().lower()
        if lbl in _MUTE_VERBS:
            out.append({
                "rule": "H6", "path": path,
                "msg": "dialog response %r names no action"
                       % r.get("label"),
                "fix": "use a verb: Import, Delete, Keep "
                       "editing -- the HIG bans Yes/No pairs"})


def _findings_h7(node, path, out):
    """H7: prose belongs to a card, a banner, or a toast --
    never loose in a bare column."""
    if not node.get("class", "").endswith("Box"):
        return
    props = node.get("props", {})
    css = props.get("css") or []
    if props.get("in_bar") or "card" in css \
            or "boxed-list" in css:
        return
    for k in node.get("children") or []:
        if k.get("class") != "GtkLabel":
            continue
        kp = k.get("props", {})
        text = kp.get("label") or ""
        kcss = set(kp.get("css") or [])
        if len(text) > 40 and not kcss & {"heading", "title-1",
                                          "title-2", "title-3",
                                          "title-4"}:
            out.append({
                "rule": "H7", "path": path,
                "msg": "loose prose in a bare column: %r"
                       % (text[:40] + "..."),
                "fix": "house it in a card or boxed list, or "
                       "promote the state to a banner / the "
                       "event to a toast"})


_RULES = (_findings_h1, _findings_h2, _findings_h3,
          _findings_h4, _findings_h5, _findings_h6,
          _findings_h7)


def lint(tree):
    """All findings over a described widget tree, depth-first.
    Paths look like Window/Box[0]/Button[2] -- stable enough to
    diff between runs; an empty list is a pass."""
    out = []

    def walk(node, path):
        for rule in _RULES:
            rule(node, path, out)
        for i, kid in enumerate(node.get("children") or []):
            walk(kid, "%s/%s[%d]" % (path, kid.get("class", "?"),
                                     i))

    walk(tree, tree.get("class", "?"))
    return out


def report(findings):
    """Human lines, one per finding: rule, path, message, fix."""
    lines = []
    for f in findings:
        lines.append("%s %s: %s" % (f["rule"], f["path"],
                                    f["msg"]))
        lines.append("   fix: %s" % f["fix"])
    return lines
