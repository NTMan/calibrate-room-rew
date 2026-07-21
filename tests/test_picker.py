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
