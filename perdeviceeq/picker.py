# -*- coding: utf-8 -*-
"""The node picker: one doctrine for every graph-node chooser
-- the sink pickers of both windows and the Measure window's
mic picker.

Both headers carry the same widget with the same doctrine: the
picker mirrors the graph, but the current node is always
listed, even when the graph lost it, so the selection never
dangles. The row itself stays clean: the WINDOW names the gone
state (the banner under the main header, the ring note in
Measure); naming it twice was field-vetoed. Rebuilds RESTORE
the selection; only a user pick
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
NodePicker is the thin GTK shell around a GtkDropDown. The shell
owns every model touch and never performs one inside the
dropdown's own notify::selected emission -- set_model there
tears down the model the widget is still walking (a field
segfault); user picks defer their reconciliation to idle,
after the emission unwinds.
"""

class PickerCore:
    """Rows, placement and pick semantics, GTK-free."""

    def __init__(self):
        self.sinks = []          # [{"name":..., "desc":...}, ...]
        self.node = None
        self.desc = ""

    def set_sinks(self, sinks):
        """Adopt a fresh graph snapshot; while the node is alive
        its desc follows the graph (renames), while it is gone
        the last known desc keeps its row readable."""
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
        """The visible rows: the graph, plus the current node
        when the graph lost it (kept at the top, like the
        Measure picker minted it; no suffix -- the window
        already names the gone state)."""
        rows = [(s["name"], s["desc"]) for s in self.sinks]
        if self.node and all(n != self.node for n, _ in rows):
            rows.insert(0, (self.node, self.desc))
        return rows

    def index_of(self, name, rows=None):
        rows = self.rows() if rows is None else rows
        return next((i for i, (n, _) in enumerate(rows)
                     if n == name), -1)

    def pick(self, i, rows=None):
        """Resolve a user pick of row i AGAINST THE ROWS THE
        MODEL WAS BUILT FROM (the widget may lag the graph
        between a pick and its idle reconciliation). Returns
        (node, desc) for a real move, None for out of range or
        for re-picking the current node -- a gone row IS the
        current choice, so picking it is a no-op."""
        rows = self.rows() if rows is None else rows
        if not (0 <= i < len(rows)):
            return None
        node, desc = rows[i]
        if node == self.node:
            return None
        return node, desc


class NodePicker:
    """The GTK shell around a GtkDropDown, one per window.

    The shell owns every touch of the widget's model. refresh()
    and select() are the windows' two doors, legal from poll,
    idle and construction -- never from inside the dropdown's
    own notify::selected emission (set_model there tears down
    the model the widget is still walking; a field segfault).
    User picks arrive through that emission, so the shell does
    no model work in it: the pick is resolved against the rows
    snapshot the visible model was built from, handed to the
    window's on_pick, and the widget is reconciled at idle,
    after the emission unwinds. on_pick returning False is a
    veto: the core does not move and the row snaps back."""

    def __init__(self, dropdown, on_pick, ellipsis=None):
        # gi arrives here, not at module scope: the core above
        # stays importable in the GTK-less test sandbox (the
        # pipewire.py rule), and by construction time the app
        # has long loaded gi with its versions required.
        from gi.repository import Gtk, GLib
        self._Gtk = Gtk
        self._GLib = GLib
        self.core = PickerCore()
        self.dd = dropdown
        self.on_pick = on_pick
        self._ellipsis = ellipsis
        self._shown = None       # rows the visible model shows
        self._guard = False
        self._in_pick = False    # delivering on_pick right now
        self._sync_queued = False
        self.dd.connect("notify::selected", self._on_selected)

    def refresh(self, sinks):
        """Adopt a fresh graph snapshot and mirror it. Legal
        from any context: called while the pick is being
        delivered, the mirror lands at idle."""
        self.core.set_sinks(sinks)
        self._sync()

    def select(self, name, desc=None):
        """Move the selection from code -- the one legal mover
        besides a user pick. Legal from any context: called
        while the pick is being delivered (a retarget does),
        the mirror lands at idle."""
        self.core.set_node(name, desc)
        self._sync()

    def _clip(self, d):
        e = self._ellipsis
        if e and len(d) > e:
            return d[:e - 1] + "\u2026"
        return d

    def _queue_sync(self):
        if not self._sync_queued:
            self._sync_queued = True
            self._GLib.idle_add(self._idle_sync)

    def _idle_sync(self):
        self._sync_queued = False
        self._sync()             # _in_pick is False at idle

    def _sync(self):
        """Mirror the core: rebuild the model only when the rows
        changed, place the selection unconditionally. GTK resets
        a fresh model's selection to row 0 -- the placement is
        what keeps that reset from ever becoming the choice.
        While a pick is being delivered the mirror is deferred:
        set_model inside the dropdown's own notify::selected
        emission tears down the model the widget is still
        walking (three field segfaults now), and on_pick may
        legitimately walk back in through select() -- a Measure
        retarget does. The latch makes the doors safe by
        construction instead of by every caller's memory."""
        if self._in_pick:
            self._queue_sync()
            return
        rows = self.core.rows()
        if rows != self._shown:
            self._shown = rows
            self._guard = True
            try:
                model = self._Gtk.StringList()
                for _, d in rows:
                    model.append(self._clip(d))
                self.dd.set_model(model)
            finally:
                self._guard = False
        idx = self.core.index_of(self.core.node, rows)
        if idx >= 0 and self.dd.get_selected() != idx:
            self._guard = True
            try:
                self.dd.set_selected(idx)
            finally:
                self._guard = False

    def _on_selected(self, *_):
        if self._guard:
            return
        hit = self.core.pick(self.dd.get_selected(), self._shown)
        if hit is None:
            return
        node, desc = hit
        # The core LEADS the callback: everything the window does
        # inside on_pick (resolving the selection, rebuilding a
        # session, persisting) must see the PICKED node, not the
        # previous one -- the field ran every mic pick against
        # the pick before it. A veto rolls the core back.
        prev = (self.core.node, self.core.desc)
        self.core.set_node(node, desc)
        self._in_pick = True
        try:
            vetoed = self.on_pick(node, desc) is False
        finally:
            self._in_pick = False
        if vetoed:
            self.core.set_node(*prev)
        self._queue_sync()    # snap-back / stale gone row melts
