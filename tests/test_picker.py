"""PickerCore: the shared sink-picker doctrine, GTK-free.

The selection never dangles: the current node is always among
the rows, marked "-- gone" when the graph lost it; placement
restores, only a pick moves; picks resolve against the rows the
widget was built from, not against a fresher graph."""
from perdeviceeq.picker import GONE, PickerCore


def _s(name, desc=None):
    return {"name": name, "desc": desc or name}


def _core(sinks, node=None, desc=None):
    c = PickerCore()
    c.set_sinks(sinks)
    if node is not None:
        c.set_node(node, desc)
    return c


def test_rows_mirror_the_graph():
    c = _core([_s("a", "A"), _s("b", "B")], "b")
    assert c.rows() == [("a", "A"), ("b", "B")]
    assert c.index_of("b") == 1
    assert c.alive()


def test_gone_row_tops_the_list_and_keeps_the_desc():
    c = _core([_s("a", "A"), _s("b", "B")], "b")
    c.set_sinks([_s("a", "A")])          # b left the graph
    assert c.rows()[0] == ("b", "B" + GONE)
    assert c.index_of("b") == 0
    assert not c.alive()
    assert c.alive("a")


def test_desc_follows_the_graph_while_alive():
    c = _core([_s("a", "old name")], "a")
    c.set_sinks([_s("a", "new name")])   # the sink was renamed
    assert c.desc == "new name"
    c.set_sinks([])                      # gone: last desc kept
    assert c.rows() == [("a", "new name" + GONE)]


def test_pick_moves_strips_and_rejects():
    c = _core([_s("a", "A"), _s("b", "B")], "b")
    c.set_sinks([_s("a", "A")])          # rows: gone b, then a
    rows = c.rows()
    assert c.pick(0, rows) is None       # the gone row IS the
    assert c.pick(9, rows) is None       # choice; range guarded
    assert c.pick(1, rows) == ("a", "A")


def test_pick_resolves_against_the_stale_rows():
    c = _core([_s("a", "A"), _s("b", "B")], "b")
    c.set_sinks([_s("a", "A")])
    stale = c.rows()                     # the widget shows these
    c.set_node("a")                      # ...but the core moved
    # picking the old gone row deliberately re-pins the dead b
    assert c.pick(0, stale) == ("b", "B")
    # and against FRESH rows the same index is a self-pick
    assert c.pick(0) is None


def test_select_resolves_desc_with_fallback():
    c = _core([_s("a", "A"), _s("b", "B")], "a")
    c.set_node("b")                      # desc from the graph
    assert c.desc == "B"
    c.set_node("ghost")                  # not listed: keep last
    assert c.desc == "B"
    assert c.rows()[0] == ("ghost", "B" + GONE)


# ---- the GTK shell, executed against a stub gi ----------------
# The fake dropdown mimics the one GTK behavior that caused the
# field bug: set_model resets the selection to row 0 and emits.

import sys
import types

INVALID = 4294967295


class FakeStringList:
    def __init__(self):
        self.items = []

    def append(self, s):
        self.items.append(s)


class FakeDropDown:
    def __init__(self):
        self.model = None
        self.selected = INVALID
        self.set_model_calls = 0
        self._handlers = []

    def connect(self, _sig, cb):
        self._handlers.append(cb)

    def _emit(self):
        for cb in list(self._handlers):
            cb(self, None)

    def set_model(self, model):
        self.model = model
        self.set_model_calls += 1
        new = 0 if model.items else INVALID
        if new != self.selected:
            self.selected = new
            self._emit()          # GTK's reset-to-row-0

    def set_selected(self, i):
        if i != self.selected:
            self.selected = i
            self._emit()

    def get_selected(self):
        return self.selected

    def user_pick(self, i):
        self.set_selected(i)


def _shell(monkeypatch, veto=False, ellipsis=None):
    idle = []
    glib = types.SimpleNamespace(
        idle_add=lambda fn, *a: idle.append((fn, a)))
    gtk = types.SimpleNamespace(StringList=FakeStringList)
    repo = types.ModuleType("gi.repository")
    repo.Gtk, repo.GLib = gtk, glib
    gi = types.ModuleType("gi")
    gi.repository = repo
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repo)
    from perdeviceeq.picker import SinkPicker
    dd = FakeDropDown()
    picks = []

    def cb(node, desc):
        picks.append((node, desc))
        if veto:
            return False

    return SinkPicker(dd, cb, ellipsis=ellipsis), dd, picks, idle


def _run_idle(idle):
    while idle:
        fn, a = idle.pop(0)
        fn(*a)


def test_shell_rebuild_restores_selection_not_row0(monkeypatch):
    p, dd, picks, idle = _shell(monkeypatch)
    p.refresh([_s("a", "A"), _s("b", "B")])
    p.select("b")
    assert dd.get_selected() == 1
    # the graph grows; GTK resets a fresh model to row 0 -- the
    # placement must take the choice back (the field bug)
    p.refresh([_s("a", "A"), _s("b", "B"), _s("c", "C")])
    assert dd.get_selected() == 1
    assert picks == []


def test_shell_death_moves_onto_the_gone_row(monkeypatch):
    p, dd, picks, idle = _shell(monkeypatch)
    p.refresh([_s("a", "A"), _s("b", "B")])
    p.select("b")
    p.refresh([_s("a", "A")])            # b left the graph
    assert dd.model.items[0] == "B" + GONE
    assert dd.get_selected() == 0
    assert picks == []                   # nobody picked anything


def test_shell_pick_defers_all_model_surgery(monkeypatch):
    p, dd, picks, idle = _shell(monkeypatch)
    p.refresh([_s("a", "A"), _s("b", "B")])
    p.select("b")
    p.refresh([_s("a", "A")])            # gone-b tops the list
    calls = dd.set_model_calls
    dd.user_pick(1)                      # the user picks a
    assert picks == [("a", "A")]
    assert p.core.node == "a"
    assert dd.set_model_calls == calls   # none inside the emission
    _run_idle(idle)
    assert dd.model.items == ["A"]       # the gone row melted
    assert dd.get_selected() == 0


def test_shell_veto_snaps_the_row_back(monkeypatch):
    p, dd, picks, idle = _shell(monkeypatch, veto=True)
    p.refresh([_s("a", "A"), _s("b", "B")])
    p.select("b")
    dd.user_pick(0)
    assert picks == [("a", "A")]
    assert p.core.node == "b"            # the core did not move
    _run_idle(idle)
    assert dd.get_selected() == 1        # the row snapped back


def test_shell_clips_like_the_measure_picker(monkeypatch):
    p, dd, picks, idle = _shell(monkeypatch, ellipsis=6)
    p.refresh([_s("a", "ABCDEFGH")])
    p.select("a")
    assert dd.model.items == ["ABCDE\u2026"]
