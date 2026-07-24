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
    """H1: linked means ONE instrument.

    Facets of one control (undo/redo, segmented toggles), or --
    the --window round's verdict -- one value control with its
    ToggleButton modifiers: a spin whose Auto hands the value to
    automation, a picker whose pin follows the default. A plain
    action button with a toggle stays a violation: the modifier
    pattern is about a VALUE under automation, not two actions
    glued together. And the one letter the libadwaita
    style-classes doc actually writes: a linked box carries no
    spacing."""
    props = node.get("props", {})
    css = props.get("css") or []
    if "linked" not in css:
        return
    spacing = props.get("spacing")
    if spacing not in (None, 0):
        out.append({
            "rule": "H1", "path": path,
            "msg": "linked box carries spacing %s (the letter: "
                   "the box must have no spacing)" % spacing,
            "fix": "set spacing 0; the linked style draws the "
                   "group as one piece"})
    kids = node.get("children") or []
    classes = {k.get("class") for k in kids}
    facets = len(classes) == 1
    toggles = [k for k in kids
               if k.get("class") == "GtkToggleButton"]
    values = [k for k in kids
              if k.get("class") != "GtkToggleButton"]
    # value controls, not action clickers: a *Button suffix
    # cannot decide this (GtkSpinButton and GtkColorButton are
    # values), so plain action classes are named outright
    _actions = ("GtkButton", "GtkMenuButton", "GtkLinkButton")
    instrument = (len(values) == 1 and toggles
                  and values[0].get("class") not in _actions)
    if len(kids) < 2 or not (facets or instrument):
        out.append({
            "rule": "H1", "path": path,
            "msg": "linked group does not hold facets of one "
                   "control (%d children, classes: %s)"
                   % (len(kids), ", ".join(sorted(
                       c or "?" for c in classes)) or "none"),
            "fix": "unlink into a plain box (spacing 6), make "
                   "the children the same control (undo/redo, "
                   "spin +/-), or pair one value control with "
                   "its ToggleButton modifiers"})


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


# the type scale: every costume a Gtk.Label may wear. Size and
# style come from this wardrobe or the label goes bare (body
# text); anything else is a hand-rolled style the HIG never
# issued. Minted from the architect's verdict: "I do not trust
# eyes" -- the floor polices the wardrobe, the words stay the
# author's pen.
_WARDROBE = frozenset((
    "title-1", "title-2", "title-3", "title-4", "heading",
    "caption", "caption-heading", "dim-label", "numeric",
    "monospace", "accent", "error", "warning", "success",
    # the app stylesheet's own issue: the speaker count and
    # its three states
    "measure-count", "done", "warn", "bad"))

# dresses the TOOLKIT puts on labels it builds itself -- the
# .title/.subtitle of Adw rows and AdwWindowTitle, the floating
# .dimmed title of AdwEntryRow, the .body/.description of
# AdwPreferencesGroup headers, heading levels, GtkScale's value
# position. The first live run accused all of them; the floor
# judges what WE placed, and the toolkit's wardrobe is the
# toolkit's business.
_TOOLKIT_DRESS = frozenset((
    "title", "subtitle", "dimmed", "body", "description",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "top", "bottom", "left", "right"))


def _findings_h8(node, path, out):
    """H8: a label dresses from the type scale."""
    if node.get("class") != "GtkLabel":
        return
    css = set(node.get("props", {}).get("css") or ())
    alien = sorted(css - _WARDROBE - _TOOLKIT_DRESS)
    if alien:
        out.append({
            "rule": "H8", "path": path,
            "msg": "label wears an unsanctioned costume: %s"
                   % ", ".join(alien),
            "fix": "dress it from the type scale (title-*, "
                   "heading, caption, dim-label, or a stock "
                   "color class), or take the class off"})


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


def _findings_h10(node, path, out):
    """H10: the list dresses evenly. Information fills the
    interface uniformly; a row hoarding text against its own
    sisters is cramped and must decompose or shed its noise to
    the tooltip. The jurisdiction is the row's OWN population,
    so an archive that is uniformly verbose is uniformly
    clean -- the median carries the scope, not a constant."""
    if node.get("class") != "GtkListBox":
        return
    rows = [k for k in (node.get("children") or [])
            if k.get("title") is not None]
    if len(rows) < 3:
        return
    lines = [((k.get("subtitle") or "").count("\n") + 1)
             if k.get("subtitle") else 0 for k in rows]
    mass = [len(k.get("title") or "")
            + len(k.get("subtitle") or "") for k in rows]
    mode = max(set(lines), key=lines.count)
    med = sorted(mass)[len(mass) // 2]
    for k, ln, ms in zip(rows, lines, mass):
        over_lines = ln > mode
        over_mass = ms > max(2 * med, med + 80)
        if not (over_lines or over_mass):
            continue
        out.append({
            "rule": "H10",
            "path": "%s/%s" % (path, k.get("class") or "?"),
            "msg": "the row overdresses its list (%d subtitle "
                   "line%s where the list wears %d; %d chars "
                   "vs median %d)"
                   % (ln, "" if ln == 1 else "s", mode,
                      ms, med),
            "fix": "move the noise to the tooltip or "
                   "decompose the row"})


_RULES = (_findings_h1, _findings_h2, _findings_h3,
          _findings_h4, _findings_h5, _findings_h6,
          _findings_h7, _findings_h8, _findings_h10)


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
