# -*- coding: utf-8 -*-
"""The sink picker, shared by the main window and the Measure
window.

Both headers carry the same widget with the same doctrine: the
picker mirrors the graph, but the current node is always listed
-- marked "-- gone" when the graph lost it -- so the selection
never dangles. Rebuilds RESTORE the selection; only a user pick
or an explicit select() moves it. Letting GtkDropDown default to
row 0 after a rebuild is what once painted a foreign sink over a
pinned panel when the pinned sink died.

The windows differ around the picker, not inside it: the main
window wraps it in Follow-the-default (on by default) and vetoes
picks while following; the Measure window never follows and
vetoes picks while a sweep runs. Both feed it from the one
PWState heartbeat.

Split in the pipewire.py tradition: PickerCore is plain data and
plain rules, importable and testable with no GTK at all;
SinkPicker is the thin GTK shell around a GtkDropDown. The shell
owns every model touch and never performs one inside the
dropdown's own notify::selected emission -- set_model there
tears down the model the widget is still walking (a field
segfault); user picks defer their reconciliation to idle,
after the emission unwinds.
"""

GONE = " -- gone"


class PickerCore:
    """Rows, placement and pick semantics, GTK-free."""

    def __init__(self):
        self.sinks = []          # [{"name":..., "desc":...}, ...]
        self.node = None
        self.desc = ""

    def set_sinks(self, sinks):
        """Adopt a fresh graph snapshot; while the node is alive
        its desc follows the graph (renames), while it is gone
        the last known desc is kept for the gone row."""
        self.sinks = list(sinks)
        self.desc = next((s["desc"] for s in self.sinks
                          if s["name"] == self.node), self.desc)

    def set_node(self, name, desc=None):
        """Move the current node; desc resolves from the graph
        when not given, falling back to the previous desc."""
        self.node = name
        if desc is not None:
            self.desc = desc
        else:
            self.desc = next((s["desc"] for s in self.sinks
                              if s["name"] == name), self.desc)

    def alive(self, name=None):
        name = self.node if name is None else name
        return any(s["name"] == name for s in self.sinks)

    def rows(self):
        """The visible rows: the graph, plus the gone row of the
        current node when the graph lost it (always at the top,
        like the Measure picker minted it)."""
        rows = [(s["name"], s["desc"]) for s in self.sinks]
        if self.node and all(n != self.node for n, _ in rows):
            rows.insert(0, (self.node, self.desc + GONE))
        return rows

    def index_of(self, name, rows=None):
        rows = self.rows() if rows is None else rows
        return next((i for i, (n, _) in enumerate(rows)
                     if n == name), -1)

    def pick(self, i, rows=None):
        """Resolve a user pick of row i AGAINST THE ROWS THE
        MODEL WAS BUILT FROM (the widget may lag the graph
        between a pick and its idle reconciliation). Returns
        (node, clean desc) for a real move, None for out of
        range or for re-picking the current node -- the gone row
        IS the current choice, so picking it is a no-op."""
        rows = self.rows() if rows is None else rows
        if not (0 <= i < len(rows)):
            return None
        node, desc = rows[i]
        if node == self.node:
            return None
        return node, desc.replace(GONE, "")
