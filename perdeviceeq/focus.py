# -*- coding: utf-8 -*-
"""Tab order as grammar, not geometry.

GTK4 sorts Tab focus by allocation -- top to bottom, left to
right -- which reads well on prose-shaped layouts and turns to
noise on instruments: the measurement ring's mics sat above the
speakers, the band grid's on/off met the finger before the
filter type. These containers walk an EXPLICIT order instead:
set_focus_order() names the stops, do_focus() walks them, and
everything that is not Tab (arrows, clicks) keeps the stock
behavior. A stop may be a leaf or a container; child_focus()
handles both, including a container finishing its own interior
before the walk advances.

set_focus_neighbors() names outside widgets for BOTH exits:
backward Tab off the walk's head, and forward Tab off its tail
-- without the forward neighbor GTK falls back to positional
sort at the exit, which is how the field caught a play ->
auto-level -> ring loop.
"""

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402


def _walk(widget, order, prev, nxt, direction, default):
    if direction not in (Gtk.DirectionType.TAB_FORWARD,
                         Gtk.DirectionType.TAB_BACKWARD):
        return default(widget, direction)
    stops = [w for w in order
             if w.get_mapped() and w.get_sensitive()]
    if not stops:
        return False
    back = direction == Gtk.DirectionType.TAB_BACKWARD
    if back:
        stops = list(reversed(stops))
    cur = None
    root = widget.get_root()
    focus = root.get_focus() if root else None
    if focus is not None:
        for i, w in enumerate(stops):
            if focus is w or focus.is_ancestor(w):
                cur = i
                break
    if cur is None:
        return stops[0].child_focus(direction)
    # let the current stop finish its own interior first
    if stops[cur].child_focus(direction):
        return True
    for w in stops[cur + 1:]:
        if w.child_focus(direction):
            return True
    if back and prev is not None:
        return prev.grab_focus()
    if not back and nxt is not None:
        return nxt.child_focus(direction) or nxt.grab_focus()
    return False


class _Ordered:
    """Mixin state; the do_focus vfunc lives on each concrete
    class because PyGObject resolves vfuncs on the class dict."""

    def _init_order(self):
        self._order = []
        self._prev = None
        self._next = None

    def set_focus_order(self, widgets):
        self._order = list(widgets)

    def set_focus_neighbors(self, prev=None, nxt=None):
        self._prev = prev
        self._next = nxt


class OrderedFixed(Gtk.Fixed, _Ordered):
    def __init__(self):
        super().__init__()
        self._init_order()

    def do_focus(self, direction):
        return _walk(self, self._order, self._prev, self._next,
                     direction, Gtk.Fixed.do_focus)


class OrderedGrid(Gtk.Grid, _Ordered):
    def __init__(self, **kw):
        super().__init__(**kw)
        self._init_order()

    def do_focus(self, direction):
        return _walk(self, self._order, self._prev, self._next,
                     direction, Gtk.Grid.do_focus)
