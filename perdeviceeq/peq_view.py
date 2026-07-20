# -*- coding: utf-8 -*-
"""PeqView: the one parametric-EQ editor.

A response graph over a band table, rendered and driven identically
wherever an EQ is edited -- the device correction and the taste
layers must look and feel like the same instrument, because they
are. The view owns NO storage: every edit is reported through
``on_changed(bands, final)`` with the bands as plain dicts, and the
owner decides what persistence, undo and application mean (the
correction editor keeps an undo stack; a taste layer writes
through). Context curves -- a measurement behind a correction --
are injected via :meth:`set_curves`, never fetched.

Rendering and interaction are lifted verbatim from the main
window's editor: the same margins, palette, dB window around the
preamp, 11 px handle hit radius, create-on-empty-plot, the
frequency guard for sub-plot trim bands, remove on right click.
"""
import math

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from . import eq
from .eq import FMIN, FMAX

DB_MAX = 24.0
_TYPES = ["PK", "LSC", "HSC"]


def _log_freqs(n=240):
    a, b = math.log10(FMIN), math.log10(FMAX)
    return [10 ** (a + (b - a) * i / (n - 1)) for i in range(n)]


def _hsv(h, s, v):
    i = int(h * 6.0); f = h * 6.0 - i
    p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
    return [(v, t, p), (q, v, p), (p, v, t),
            (p, q, v), (t, p, v), (v, p, q)][i % 6]


def _band_color(f):
    """Rainbow by log frequency (blue=low .. red=high)."""
    lf = math.log10(min(FMAX, max(FMIN, f)))
    t = (lf - math.log10(FMIN)) / (math.log10(FMAX) - math.log10(FMIN))
    return _hsv((1.0 - t) * 0.66, 0.65, 1.0)


def _tame_scroll(widget, handler):
    ctrl = Gtk.EventControllerScroll.new(
        Gtk.EventControllerScrollFlags.VERTICAL)
    ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    ctrl.connect("scroll", handler)
    widget.add_controller(ctrl)


class PeqView(Gtk.Box):
    """Graph + band table; edits go to ``on_changed(bands, final)``.

    ``final`` is False during a live drag and True when the edit
    settles (drag end, a spin/dropdown/toggle change, add, remove);
    write-through owners can persist on every call and do the
    expensive follow-ups only on final ones.
    """

    def __init__(self, on_changed, preamp=0.0, compact=False,
                 on_import_file=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL,
                         spacing=6)
        self._on_changed = on_changed
        # an editor verb, not an "import": replaces THIS view's
        # bands from a parametric-EQ text file. Rendered only
        # where the owner wires it (the device channel card) --
        # a document-level import lives in the profile menu.
        self._on_import_file = on_import_file
        self._preamp = float(preamp)
        self._bands = []
        self._curves = None         # (freqs, measured, spread, band)
        self._plot = None
        self._drag_band = None
        self._loading = False
        self._active = True

        self.graph = Gtk.DrawingArea()
        self.graph.set_content_height(150 if compact else 220)
        self.graph.set_hexpand(True)
        self.graph.set_draw_func(self._draw)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.graph.add_controller(drag)
        rclick = Gtk.GestureClick()
        rclick.set_button(3)
        rclick.connect("pressed", self._on_right_click)
        self.graph.add_controller(rclick)
        self.append(self.graph)

        self.grid = Gtk.Grid(column_spacing=4, row_spacing=4)
        for side in ("top", "bottom", "start", "end"):
            getattr(self.grid, "set_margin_" + side)(4)
        self.append(self.grid)
        self._rebuild_table()

    # ---- public API --------------------------------------------------
    def set_bands(self, band_dicts):
        """Replace the whole band list (dicts; the view keeps Band
        objects internally so a drag can mutate freq/gain)."""
        self._bands = [eq.Band.from_dict(b) for b in (band_dicts or [])]
        self._drag_band = None
        self._rebuild_table()
        self.graph.queue_draw()

    def get_bands(self):
        return [b.to_dict() for b in self._bands]

    def set_active(self, active):
        """Bypass dimming: an inactive EQ draws gray with a dashed
        zero line, exactly like the main editor always did."""
        self._active = bool(active)
        self.graph.queue_draw()

    def set_preamp(self, v):
        """The dB window follows the preamp, like the main editor."""
        self._preamp = float(v or 0.0)
        self.graph.queue_draw()

    def set_curves(self, freqs=None, measured=None, spread=None,
                   band=None):
        """Context behind the EQ: a measured curve (with an optional
        spread fan and certified band) plus the predicted result,
        which the view derives as measured + response. None clears."""
        if freqs is None or measured is None:
            self._curves = None
        else:
            meas = list(measured)
            spr = list(spread) if spread is not None else None
            fan = [m + (spr[i] if spr else 0.0)
                   for i, m in enumerate(meas)]
            dip = [m - (spr[i] if spr else 0.0)
                   for i, m in enumerate(meas)]
            # extremes cached HERE: _y_window runs per mapped point
            # during hit tests and must stay O(1); recomputing these
            # lists inside it made the draw quadratic
            self._curves = (list(freqs), meas, spr,
                            tuple(band) if band is not None else None,
                            min(dip), max(fan))
        self.graph.queue_draw()

    # ---- geometry -----------------------------------------------------
    def _y_window(self):
        lo, hi = -DB_MAX + self._preamp, DB_MAX + self._preamp
        c = self._curves
        if c is not None:
            lo = min(lo, c[4])
            hi = max(hi, c[5])
        return lo, hi

    def _x_of(self, f):
        ml, mt, pw_, ph = self._plot
        return ml + (math.log10(f) - math.log10(FMIN)) / \
            (math.log10(FMAX) - math.log10(FMIN)) * pw_

    def _y_of(self, db):
        ml, mt, pw_, ph = self._plot
        lo, hi = self._y_window()
        return mt + (hi - db) / (hi - lo) * ph

    def _f_of(self, x):
        ml, mt, pw_, ph = self._plot
        if pw_ <= 0:
            return None
        t = min(1.0, max(0.0, (x - ml) / pw_))
        return 10 ** (math.log10(FMIN)
                      + t * (math.log10(FMAX) - math.log10(FMIN)))

    def _db_of(self, y):
        ml, mt, pw_, ph = self._plot
        if ph <= 0:
            return None
        lo, hi = self._y_window()
        t = min(1.0, max(0.0, (y - mt) / ph))
        return hi - t * (hi - lo)

    def _hit_band(self, x, y, r=11):
        if not self._plot:
            return None
        best, bestd = None, r * r
        wlo, whi = self._y_window()
        for b in self._bands:
            bx = self._x_of(max(b.freq, FMIN))  # freq-0 trim: left edge
            by = self._y_of(max(wlo, min(whi, b.gain)))
            d = (bx - x) ** 2 + (by - y) ** 2
            if d <= bestd:
                best, bestd = b, d
        return best

    # ---- drawing ------------------------------------------------------
    def _draw(self, _area, cr, w, h, *_):
        ml, mr, mt, mb = 44, 10, 10, 22
        pw_, ph = max(1, w - ml - mr), max(1, h - mt - mb)
        self._plot = (ml, mt, pw_, ph)
        cr.set_source_rgb(0.12, 0.12, 0.14); cr.paint()
        cr.rectangle(ml, mt, pw_, ph)
        cr.set_source_rgb(0.08, 0.08, 0.10); cr.fill()
        wlo, whi = self._y_window()
        lg_lo = math.log10(FMIN)
        lg_span = math.log10(FMAX) - lg_lo

        def x_of(f):
            return ml + (math.log10(f) - lg_lo) / lg_span * pw_

        def y_of(db):
            return mt + (whi - db) / (whi - wlo) * ph

        cr.set_line_width(1.0)
        cr.select_font_face("Sans", 0, 0); cr.set_font_size(9)
        for f in (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000,
                  20000):
            x = x_of(f)
            cr.set_source_rgba(1, 1, 1, 0.10)
            cr.move_to(x, mt); cr.line_to(x, mt + ph); cr.stroke()
            cr.set_source_rgba(1, 1, 1, 0.45)
            lab = ("%dk" % (f // 1000)) if f >= 1000 else str(f)
            cr.move_to(x - 8, mt + ph + 14); cr.show_text(lab)
        for db in range(int(math.ceil(wlo / 6.0)) * 6,
                        int(math.floor(whi)) + 1, 6):
            y = y_of(db)
            cr.set_source_rgba(1, 1, 1, 0.16 if db == 0 else 0.08)
            cr.move_to(ml, y); cr.line_to(ml + pw_, y); cr.stroke()
            cr.set_source_rgba(1, 1, 1, 0.45)
            cr.move_to(4, y + 3); cr.show_text("%+d" % db)

        if self._curves is not None:
            fo, meas, spread, band = self._curves[:4]
            cr.save()
            cr.rectangle(ml, mt, pw_, ph)
            cr.clip()
            if spread is not None:
                cr.set_source_rgba(0.55, 0.65, 0.85, 0.14)
                for i, f in enumerate(fo):
                    x = x_of(f)
                    y = y_of(meas[i] + spread[i])
                    cr.move_to(x, y) if i == 0 else cr.line_to(x, y)
                for i in range(len(fo) - 1, -1, -1):
                    cr.line_to(x_of(fo[i]),
                               y_of(meas[i] - spread[i]))
                cr.close_path()
                cr.fill()
            cr.set_source_rgba(0.85, 0.85, 0.90, 0.55)
            cr.set_line_width(1.2)
            for i, f in enumerate(fo):
                x, y = x_of(f), y_of(meas[i])
                cr.move_to(x, y) if i == 0 else cr.line_to(x, y)
            cr.stroke()
            resp = eq.response_db(self._preamp, self._bands, fo)
            cr.set_source_rgba(0.45, 0.95, 0.55, 0.90)
            cr.set_line_width(1.5)
            for i, f in enumerate(fo):
                x, y = x_of(f), y_of(meas[i] + resp[i])
                cr.move_to(x, y) if i == 0 else cr.line_to(x, y)
            cr.stroke()
            cr.set_source_rgba(0, 0, 0, 0.30)
            if band is not None:
                blo, bhi = max(band[0], FMIN), min(band[1], FMAX)
                if blo > FMIN:
                    cr.rectangle(ml, mt, x_of(blo) - ml, ph)
                    cr.fill()
                if bhi < FMAX:
                    cr.rectangle(x_of(bhi), mt,
                                 ml + pw_ - x_of(bhi), ph)
                    cr.fill()
            else:                    # nothing certified: dim it all
                cr.rectangle(ml, mt, pw_, ph)
                cr.fill()
            cr.select_font_face("Sans", 0, 0)
            cr.set_font_size(9)
            lx, ly = ml + 10, mt + 14
            for lab, rgba in (
                    ("measured", (0.85, 0.85, 0.90, 0.9)),
                    ("predicted", (0.45, 0.95, 0.55, 0.9)),
                    ("EQ", (0.30, 0.78, 1.0, 0.9))):
                cr.set_source_rgba(*rgba)
                cr.set_line_width(2.0)
                cr.move_to(lx, ly - 3)
                cr.line_to(lx + 14, ly - 3)
                cr.stroke()
                cr.move_to(lx + 18, ly)
                cr.show_text(lab)
                lx += 18 + cr.text_extents(lab).width + 14
            cr.restore()

        freqs = _log_freqs(int(max(60, pw_)))
        curve = eq.response_db(self._preamp, self._bands, freqs)
        cr.set_source_rgb(0.30, 0.78, 1.0) if self._active \
            else cr.set_source_rgba(0.6, 0.6, 0.6, 0.7)
        cr.set_line_width(2.0)
        for i, f in enumerate(freqs):
            db = max(wlo, min(whi, curve[i]))
            px, py = x_of(f), y_of(db)
            cr.move_to(px, py) if i == 0 else cr.line_to(px, py)
        cr.stroke()
        if not self._active:
            cr.set_source_rgba(0.30, 0.78, 1.0, 0.5)
            cr.set_line_width(1.5); cr.set_dash([4, 4], 0)
            cr.move_to(ml, y_of(0))
            cr.line_to(ml + pw_, y_of(0)); cr.stroke()
            cr.set_dash([], 0)

        for b in self._bands:
            bx = x_of(max(b.freq, FMIN))
            by = y_of(max(wlo, min(whi, b.gain)))
            r, g, bl = _band_color(b.freq)
            cr.arc(bx, by, 5.5, 0, 2 * math.pi)
            if b.enabled:
                cr.set_source_rgb(r, g, bl); cr.fill_preserve()
                cr.set_source_rgba(0, 0, 0, 0.55)
                cr.set_line_width(1.0); cr.stroke()
            else:
                cr.set_source_rgba(r, g, bl, 0.7)
                cr.set_line_width(1.5); cr.stroke()

    # ---- graph interaction ---------------------------------------------
    def _on_drag_begin(self, gesture, sx, sy):
        self._drag_band = None
        if not self._plot:
            return
        b = self._hit_band(sx, sy)
        created = False
        if b is None:                 # empty spot -> a band is born
            f = self._f_of(sx); db = self._db_of(sy)
            if f is None or db is None:
                return
            b = eq.Band("PK", f, db, 1.0, True)
            self._bands.append(b)
            created = True
        self._drag_band = b
        if created:
            self._rebuild_table()
            self._emit(True)
        self.graph.queue_draw()

    def _on_drag_update(self, gesture, ox, oy):
        if self._drag_band is None or not self._plot:
            return
        ok, sx, sy = gesture.get_start_point()
        if not ok:
            return
        f = self._f_of(sx + ox); db = self._db_of(sy + oy)
        if f is not None and self._drag_band.freq >= FMIN:
            # a sub-plot band (the freq-0 balance trim) keeps its
            # frequency under drag -- the plot cannot express it and
            # a vertical gain drag must not retune it to 20 Hz
            self._drag_band.freq = f
        if db is not None:
            self._drag_band.gain = db
        self.graph.queue_draw()
        self._emit(False)             # live; no table rebuild

    def _on_drag_end(self, gesture, ox, oy):
        if self._drag_band is None:
            return
        self._drag_band = None
        self._rebuild_table()
        self._emit(True)

    def _on_right_click(self, gesture, n, x, y):
        b = self._hit_band(x, y)
        if b is not None and b in self._bands:
            self._bands.remove(b)
            self._rebuild_table()
            self.graph.queue_draw()   # the handle must die with it
            self._emit(True)

    # ---- the band table -------------------------------------------------
    def _rebuild_table(self):
        child = self.grid.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.grid.remove(child)
            child = nxt
        heads = ("", "Type", "Freq (Hz)", "Gain (dB)", "Q", "On", "")
        for c, t in enumerate(heads):
            lbl = Gtk.Label(label=t, xalign=0.0)
            lbl.add_css_class("dim-label")
            lbl.add_css_class("caption")
            self.grid.attach(lbl, c, 0, 1, 1)
        # rows sort by frequency, like the plot reads; storage
        # order is untouched (the response is order-agnostic)
        shown = sorted(self._bands, key=lambda b: b.freq)
        for i, b in enumerate(shown):
            self._attach_band(i, b)
        def _action(icon, label):
            # flat buttons carry their affordance in hover alone;
            # an icon restores it at rest (the GNOME list-action
            # idiom), without the visual weight of a border
            b = Gtk.Button()
            b.add_css_class("flat")
            box = Gtk.Box(spacing=6)
            box.append(Gtk.Image.new_from_icon_name(icon))
            box.append(Gtk.Label(label=label))
            b.set_child(box)
            return b
        acts = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                       spacing=6, halign=Gtk.Align.START)
        addb = _action("list-add-symbolic", "Add band")
        addb.connect("clicked", self._on_add)
        acts.append(addb)
        if self._on_import_file is not None:
            repl = _action("document-open-symbolic",
                           "Replace bands from file\u2026")
            repl.set_tooltip_text(
                "Replace this channel's bands from a "
                "parametric-EQ text file (REW / AutoEq)")
            repl.connect("clicked",
                         lambda *_: self._on_import_file())
            acts.append(repl)
        self.grid.attach(acts, 1, len(self._bands) + 1, 6, 1)

    def _attach_band(self, i, b):
        row = i + 1
        dot = Gtk.DrawingArea()
        dot.set_content_width(12)
        dot.set_content_height(12)
        dot.set_valign(Gtk.Align.CENTER)
        dot.set_draw_func(self._make_dot_draw(b))
        self.grid.attach(dot, 0, row, 1, 1)
        dd = Gtk.DropDown.new_from_strings(_TYPES)
        dd.set_selected(_TYPES.index(b.type)
                        if b.type in _TYPES else 0)
        dd.connect("notify::selected",
                   lambda d, *_a, b=b:
                   self._write(b, "type", _TYPES[d.get_selected()]))
        _tame_scroll(dd, self._on_widget_scroll)
        self.grid.attach(dd, 1, row, 1, 1)
        col = 2
        for key, lo, hi, step, dig, tip in (
                ("freq", 10.0, 20000.0, 1.0, 0, "Frequency, Hz"),
                ("gain", -24.0, 24.0, 0.1, 1, "Gain, dB"),
                ("q", 0.1, 10.0, 0.05, 2, "Q")):
            sp = Gtk.SpinButton.new_with_range(lo, hi, step)
            sp.set_digits(dig)
            sp.set_hexpand(True)
            sp.set_width_chars(5)
            sp.set_max_width_chars(5)
            sp.set_tooltip_text(tip)
            sp.set_value(float(getattr(b, key)))
            sp.connect("value-changed",
                       lambda spb, key=key, b=b:
                       self._write(b, key, spb.get_value()))
            _tame_scroll(sp, self._on_widget_scroll)
            self.grid.attach(sp, col, row, 1, 1)
            col += 1
        sw = Gtk.CheckButton()
        sw.set_valign(Gtk.Align.CENTER)
        sw.set_active(bool(b.enabled))
        sw.set_tooltip_text("Band on/off")
        sw.connect("toggled",
                   lambda swb, b=b:
                   self._write(b, "enabled", swb.get_active()))
        self.grid.attach(sw, 5, row, 1, 1)
        tr = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        tr.add_css_class("flat")
        tr.set_tooltip_text("Delete this band")
        tr.connect("clicked", lambda *_a, b=b: self._on_del(b))
        self.grid.attach(tr, 6, row, 1, 1)

    def _make_dot_draw(self, b):
        def draw(_a, cr, w, h, *_):
            r, g, bl = _band_color(b.freq)
            cr.arc(w / 2.0, h / 2.0, min(w, h) / 2.0 - 1,
                   0, 2 * math.pi)
            if b.enabled:
                cr.set_source_rgb(r, g, bl); cr.fill()
            else:
                cr.set_source_rgba(r, g, bl, 0.7)
                cr.set_line_width(1.5); cr.stroke()
        return draw

    # ---- edits ----------------------------------------------------------
    def _write(self, b, key, val):
        if self._loading or b not in self._bands:
            return
        setattr(b, key, val if key in ("type", "enabled")
                else float(val))
        self.graph.queue_draw()
        self._emit(True)

    def _on_add(self, *_):
        self._bands.append(eq.Band("PK", 1000.0, 0.0, 1.0, True))
        self._rebuild_table()
        self.graph.queue_draw()
        self._emit(True)

    def _on_del(self, b):
        if b in self._bands:
            self._bands.remove(b)
            self._rebuild_table()
            self.graph.queue_draw()
            self._emit(True)

    def _emit(self, final):
        if not self._loading:
            self._on_changed(self.get_bands(), final)

    # ---- scroll taming ---------------------------------------------------
    def _on_widget_scroll(self, ctrl, dx, dy):
        """Forward the wheel to the enclosing scrolled page; the
        hovered value stays untouched."""
        w = ctrl.get_widget()
        sw = w.get_ancestor(Gtk.ScrolledWindow) if w else None
        if sw is not None:
            adj = sw.get_vadjustment()
            if adj is not None:
                step = adj.get_step_increment()
                if step <= 0:
                    step = 30.0
                new = adj.get_value() + dy * step
                new = max(adj.get_lower(),
                          min(new,
                              adj.get_upper() - adj.get_page_size()))
                adj.set_value(new)
        return True


class CollapsibleCard(Gtk.Box):
    """The notification pattern GNOME Shell draws by hand, in GTK
    parts: a .card whose clickable header row (rotating chevron on
    the right) sits over a Gtk.Revealer body with a slide-down
    transition. Header children that consume clicks (buttons, menu
    buttons) keep them; a click anywhere else on the row toggles.
    on_toggled(expanded) fires after every user toggle, so the
    owner can persist the state."""

    def __init__(self, expanded=False, on_toggled=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("card")
        self._on_toggled = on_toggled
        self._last = None
        self._header = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for side in ("top", "bottom"):
            getattr(self._header, "set_margin_" + side)(8)
        for side in ("start", "end"):
            getattr(self._header, "set_margin_" + side)(12)
        self._chevron = Gtk.Image.new_from_icon_name(
            "pan-down-symbolic")
        self._chevron.set_valign(Gtk.Align.CENTER)
        self._header.append(self._chevron)
        click = Gtk.GestureClick()
        click.set_button(1)
        click.connect("released", self._on_header_click)
        self._header.add_controller(click)
        self._rev = Gtk.Revealer()
        self._rev.set_transition_type(
            Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._rev.set_transition_duration(200)
        self._rev.set_reveal_child(bool(expanded))
        self.append(self._header)
        self.append(self._rev)
        self._sync_chevron()

    def add_header(self, w, expand=False):
        """Insert a header widget before the chevron, in call
        order."""
        if expand:
            w.set_hexpand(True)
        if self._last is None:
            self._header.prepend(w)
        else:
            self._header.insert_child_after(w, self._last)
        self._last = w

    def set_body(self, w):
        self._rev.set_child(w)

    def get_expanded(self):
        return self._rev.get_reveal_child()

    def set_expanded(self, v):
        self._rev.set_reveal_child(bool(v))
        self._sync_chevron()

    def _sync_chevron(self):
        self._chevron.set_from_icon_name(
            "pan-up-symbolic" if self.get_expanded()
            else "pan-down-symbolic")

    def _on_header_click(self, *_):
        self.set_expanded(not self.get_expanded())
        if self._on_toggled:
            self._on_toggled(self.get_expanded())
