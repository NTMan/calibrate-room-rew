# -*- coding: utf-8 -*-
"""GTK4 / libadwaita GUI for per-device-eq.

Structure mirrors a GNOME "Sound" settings panel (the long-term goal is to be
liftable into gnome-control-center):
  * device context = the active output (Follow default),
  * a Weather-style profile picker in the header (empty query = favorites,
    typing = the whole catalog; check on the active one, x to remove),
  * the EQ controls inline on the page (FR graph + bands + preamp + bypass),
  * a "Same EQ for all channels" switch; turn it off to reveal per-channel
    (FL | FR | ...) tabs and tune each channel separately,
  * undo/redo (Ctrl+Z / Ctrl+Shift+Z / Ctrl+Y; buttons in the header).

The static shell is loaded from data/<APP_ID>.ui; everything dynamic (graph,
band rows, channel buttons, picker rows, footer buttons, header buttons) is
built here. Backend logic (graph building, profiles, the metadata bridge to the
WP hook) lives in the sibling package modules and is reused as-is.

Editor state is kept as channel "slots": self.slots maps "all" + each channel
key to {"preamp": float, "bands": [eq.Band]}. self.cur_ch selects which slot the
single editor (graph + band table + preamp) is currently editing. On save/apply
the slots are assembled into a profile body and handed to the tested
profiles.save_user / eq.profile_graph.

Undo/redo is a history of serialized editor snapshots; a new snapshot is pushed
on each settled (debounced) edit, so a drag of a spin button collapses into one
undo step. Scroll over spin buttons / dropdowns is intercepted so the wheel
never changes a value by accident; the scroll is forwarded to the page instead.

Deferred to a later increment: dragging band handles on the graph, GtkColumnView
for the band table, per-row sparklines, the online AutoEQ catalog.
"""

import json, math, os, sys

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gio, GLib, Gdk, Adw

from . import config, eq, pipewire, integration
from .config import (APP_ID, TYPE_NAMES, CLEAN_ID, FAVORITES_FILE,
                     UI_FILE_CANDIDATES)
from .profiles import ProfileStore

DB_MAX = 24.0
FMIN, FMAX = config.FMIN, config.FMAX
_SAVE_DEBOUNCE_MS = 200
_HISTORY_CAP = 100


def _ui_path():
    """Return the first existing .ui path from UI_FILE_CANDIDATES."""
    for p in UI_FILE_CANDIDATES:
        if os.path.exists(p):
            return p
    raise FileNotFoundError(
        "GUI design not found; looked in:\n  " + "\n  ".join(UI_FILE_CANDIDATES))


def _log_freqs(n=240):
    """n log-spaced frequencies over FMIN..FMAX for plotting the response."""
    import math
    a, b = math.log10(FMIN), math.log10(FMAX)
    return [10 ** (a + (b - a) * i / (n - 1)) for i in range(n)]


def _load_favorites():
    """Read the pinned built-in profile ids from FAVORITES_FILE."""
    try:
        with open(FAVORITES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return list(data) if isinstance(data, list) else []
    except Exception:
        return []


def _save_favorites(ids):
    """Persist the pinned built-in profile ids to FAVORITES_FILE."""
    try:
        os.makedirs(config.CONFIG_DIR, exist_ok=True)
        tmp = FAVORITES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(list(ids), f, indent=2)
        os.replace(tmp, FAVORITES_FILE)
    except Exception:
        pass


def _new_slot():
    """Fresh empty per-channel slot (preamp 0, no bands)."""
    return {"bands": []}


def _copy_slot(s):
    """Deep copy of a slot with independent Band objects."""
    return {"bands": [eq.Band.from_dict(b.to_dict())
                      for b in s.get("bands", [])]}


class EqWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        """Build the window from the .ui design and wire all behavior to it."""
        super().__init__(application=app)
        self.set_title("Per-Device EQ")
        self.set_default_size(640, 820)

        self.store = ProfileStore()
        self.favorites = set(_load_favorites())
        self.node = None
        self.live = False
        self.current_pid = CLEAN_ID
        self._save_source = 0
        self._loading = False
        self.sinks = []
        self._dev_guard = False
        self._poll_busy = False

        # editor state ("slots": "all" + one per channel key)
        self.apply_all = True
        self.ch_keys = ["FL", "FR"]
        self.slots = {"all": _new_slot()}
        self.cur_ch = "all"
        self.preamp = 0.0
        self.bands = self.slots["all"]["bands"]     # alias of the current slot
        self._chan_buttons = {}

        # undo/redo history (serialized snapshots)
        self._hist = []
        self._hidx = -1
        self._restoring = False

        b = Gtk.Builder.new_from_file(_ui_path())
        self.set_content(b.get_object("content"))
        self.header_bar = b.get_object("header_bar")
        self.window_title = b.get_object("window_title")
        self.profile_button = b.get_object("profile_button")
        self.profile_popover = b.get_object("profile_popover")
        self.search_entry = b.get_object("search_entry")
        self.profile_list = b.get_object("profile_list")
        self.popover_footer = b.get_object("popover_footer")
        self.device_dd = b.get_object("device_dd")
        self.follow_btn = b.get_object("follow_btn")
        self.link_row = b.get_object("link_row")
        self.preamp_row = b.get_object("preamp_row")
        self.bypass_row = b.get_object("bypass_row")
        self.channel_bar = b.get_object("channel_bar")
        self.graph_frame = b.get_object("graph_frame")
        self.bands_group = b.get_object("bands_group")

        self._install_css()
        self._build_header_buttons()
        self._build_graph()
        self._build_preamp()
        self._build_bands_area()
        self._build_picker_footer()
        self._install_shortcuts(app)

        self.search_entry.connect("search-changed", lambda *_: self._populate_picker())
        self.link_row.connect("notify::active", self._on_link)
        self.bypass_row.connect("notify::active", self._on_bypass)
        self.profile_button.connect("notify::active", self._on_picker_toggle)
        self.device_dd.connect("notify::selected", self._on_device_changed)
        self.follow_btn.connect("toggled", self._on_follow_toggled)

        self._init_devices()
        self.current_pid = self.store.binding_for(self.node) or CLEAN_ID
        # apply=True primes the session metadata key for the startup device.
        # Before the app starts, EQ is applied by the WP hook from its own
        # saved state and the metadata key does not exist yet; clearing a
        # non-existent key emits no change event, so the first Bypass would
        # silently do nothing. Publishing the (identical) graph here is
        # inaudible and keeps the hook and the GUI in sync from turn one.
        self._load_profile(self.current_pid, apply=True)
        self._populate_picker()
        if self.live:
            GLib.timeout_add_seconds(2, self._poll)

    # ---- widget construction ----------------------------------------------
    def _build_header_buttons(self):
        """Add the undo/redo buttons to the end of the header bar."""
        self.undo_btn = Gtk.Button.new_from_icon_name("edit-undo-symbolic")
        self.undo_btn.set_tooltip_text("Undo (Ctrl+Z)")
        self.undo_btn.connect("clicked", lambda *_: self._undo())
        self.redo_btn = Gtk.Button.new_from_icon_name("edit-redo-symbolic")
        self.redo_btn.set_tooltip_text("Redo (Ctrl+Shift+Z)")
        self.redo_btn.connect("clicked", lambda *_: self._redo())
        # pack_end fills right-to-left: redo first (rightmost), then undo to its left
        self.header_bar.pack_end(self.redo_btn)
        self.header_bar.pack_end(self.undo_btn)

    def _build_graph(self):
        """Create the response plot (DrawingArea) with drag + right-click gestures."""
        self.graph_area = Gtk.DrawingArea()
        self.graph_area.set_content_height(240)
        self.graph_area.set_hexpand(True)
        self.graph_area.set_draw_func(self._draw_graph)
        self._plot = None
        self._drag_band = None
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.graph_area.add_controller(drag)
        rclick = Gtk.GestureClick()
        rclick.set_button(3)
        rclick.connect("pressed", self._on_right_click)
        self.graph_area.add_controller(rclick)
        self.graph_frame.set_child(self.graph_area)

    # ---- graph geometry / interaction (drag band handles) ------------------
    def _x_of(self, f):
        """Map a frequency in Hz to a plot x pixel."""
        ml, mt, pw_, ph = self._plot
        return ml + (math.log10(f) - math.log10(FMIN)) / \
            (math.log10(FMAX) - math.log10(FMIN)) * pw_

    def _y_of(self, db):
        """Map a dB value to a plot y pixel."""
        ml, mt, pw_, ph = self._plot
        return mt + (DB_MAX - db) / (2 * DB_MAX) * ph

    def _f_of(self, x):
        """Inverse of _x_of: plot x pixel back to frequency (clamped)."""
        ml, mt, pw_, ph = self._plot
        if pw_ <= 0:
            return None
        t = min(1.0, max(0.0, (x - ml) / pw_))
        return 10 ** (math.log10(FMIN) + t * (math.log10(FMAX) - math.log10(FMIN)))

    def _db_of(self, y):
        """Inverse of _y_of: plot y pixel back to dB (clamped)."""
        ml, mt, pw_, ph = self._plot
        if ph <= 0:
            return None
        t = min(1.0, max(0.0, (y - mt) / ph))
        return DB_MAX - t * (2 * DB_MAX)

    @staticmethod
    def _hsv(h, s, v):
        """Tiny HSV->RGB helper for band colors."""
        i = int(h * 6.0); f = h * 6.0 - i
        p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
        return [(v, t, p), (q, v, p), (p, v, t),
                (p, q, v), (t, p, v), (v, p, q)][i % 6]

    def _band_color(self, f):
        """Rainbow color for a band by log frequency (blue=low .. red=high)."""
        lf = math.log10(min(FMAX, max(FMIN, f)))
        t = (lf - math.log10(FMIN)) / (math.log10(FMAX) - math.log10(FMIN))
        return self._hsv((1.0 - t) * 0.66, 0.65, 1.0)

    def _hit_band(self, x, y, r=11):
        """The band whose handle covers (x, y), or None (11 px hit radius)."""
        if not self._plot:
            return None
        best, bestd = None, r * r
        for b in self.bands:
            bx = self._x_of(b.freq)
            by = self._y_of(max(-DB_MAX, min(DB_MAX, b.gain)))
            d = (bx - x) ** 2 + (by - y) ** 2
            if d <= bestd:
                best, bestd = b, d
        return best

    def _ensure_audible(self):
        """Drop Bypass so a graph edit is heard immediately."""
        if self.bypass_row.get_active():
            self._loading = True
            self.bypass_row.set_active(False)
            self._loading = False

    def _on_drag_begin(self, gesture, sx, sy):
        """Grab the band handle under the pointer, or create a band on empty plot."""
        self._drag_band = None
        if not self._plot:
            return
        b = self._hit_band(sx, sy)
        created = False
        if b is None:                       # empty spot -> create a band there
            f = self._f_of(sx); db = self._db_of(sy)
            if f is None or db is None:
                return
            b = eq.Band("PK", f, db, 1.0, True)
            self.bands.append(b)
            created = True
        self._ensure_audible()
        self._drag_band = b
        if created:
            self._rebuild_bands()
            self._on_edit()
        self.graph_area.queue_draw()

    def _on_drag_update(self, gesture, ox, oy):
        """Move the dragged band with the pointer (freq/gain), applying live."""
        if self._drag_band is None or not self._plot:
            return
        ok, sx, sy = gesture.get_start_point()
        if not ok:
            return
        f = self._f_of(sx + ox); db = self._db_of(sy + oy)
        if f is not None:
            self._drag_band.freq = f
        if db is not None:
            self._drag_band.gain = db
        self.graph_area.queue_draw()
        self._on_edit()                     # live apply (debounced); no row rebuild

    def _on_drag_end(self, gesture, ox, oy):
        """Finish a drag: rebuild the (sorted) table and settle the save."""
        if self._drag_band is None:
            return
        self._drag_band = None
        self._rebuild_bands()               # sync the table to the new freq/gain
        self._on_edit()

    def _on_right_click(self, gesture, n, x, y):
        """Remove the band nearest to a right click (within hit radius)."""
        b = self._hit_band(x, y)
        if b is not None and b in self.bands:
            self.bands.remove(b)
            self._rebuild_bands()
            self._on_edit()

    def _build_preamp(self):
        """Put the preamp SpinButton and the Auto button onto preamp_row."""
        self.preamp_spin = Gtk.SpinButton.new_with_range(-DB_MAX, DB_MAX, 0.5)
        self.preamp_spin.set_digits(1)
        self.preamp_spin.set_valign(Gtk.Align.CENTER)
        self.preamp_spin.connect("value-changed", self._on_preamp)
        self._tame_scroll(self.preamp_spin)
        self.auto_button = Gtk.Button(label="Auto")
        self.auto_button.set_valign(Gtk.Align.CENTER)
        self.auto_button.set_tooltip_text(
            "Set preamp to -(max of the EQ curve), so full-scale content "
            "stays below 0 dBFS after EQ. With per-channel EQ, sets the "
            "SAME value on every channel (the worst channel's requirement) "
            "so the balance encoded by the curves survives")
        self.auto_button.connect("clicked", self._on_auto)
        # tier-1 clip lamp (ROADMAP Task 2): shown when the headroom estimate
        # crosses 0 dBFS; the row subtitle carries the estimate readout.
        self.clip_icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        self.clip_icon.add_css_class("error")
        self.clip_icon.set_valign(Gtk.Align.CENTER)
        self.clip_icon.set_visible(False)
        self.preamp_row.add_suffix(self.clip_icon)
        self.preamp_row.add_suffix(self.preamp_spin)
        self.preamp_row.add_suffix(self.auto_button)
        self.preamp_row.set_activatable_widget(self.preamp_spin)
        self._preamp_subtitle = self.preamp_row.get_subtitle() or ""

    _CSS_INSTALLED = False

    def _install_css(self):
        """Install the compact .eqrow style, once per process (ported from the
        monolith): spin buttons / dropdowns tighter than the libadwaita default.
        """
        if EqWindow._CSS_INSTALLED:
            return
        EqWindow._CSS_INSTALLED = True
        data = (
            ".eqrow spinbutton, .eqrow button, .eqrow dropdown,"
            " .eqrow dropdown > button { min-height: 24px; }"
            " .eqrow spinbutton text, .eqrow spinbutton entry,"
            " .eqrow dropdown > button, .eqrow button"
            " { min-height: 24px; padding-top: 1px; padding-bottom: 1px; }"
            # tier-1 clip lamp: paint the Preamp row with the theme error color
            " row.clip-risk label.subtitle { color: @error_color; }"
            " row.clip-risk spinbutton text { color: @error_color; }"
            # ...and flag every over-0 channel on its tab in the channel bar
            " button.clip-risk label { color: @error_color; font-weight: 600; }")
        css = Gtk.CssProvider()
        if hasattr(css, "load_from_string"):
            css.load_from_string(data)
        else:
            css.load_from_data(data.encode())
        disp = Gdk.Display.get_default()
        if disp is not None:
            Gtk.StyleContext.add_provider_for_display(
                disp, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _build_bands_area(self):
        """Build the Bands header actions (Clear/Import REW/add) and the band grid."""
        suffix = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        clear_btn = Gtk.Button.new_from_icon_name("edit-clear-all-symbolic")
        clear_btn.add_css_class("flat")
        clear_btn.set_tooltip_text("Remove all bands shown here")
        clear_btn.connect("clicked", self._on_clear_bands)
        add_btn = Gtk.Button.new_from_icon_name("list-add-symbolic")
        add_btn.add_css_class("flat")
        add_btn.set_tooltip_text("Add band")
        add_btn.connect("clicked", self._on_add_band)
        imp_content = Adw.ButtonContent(icon_name="document-open-symbolic",
                                        label="Import REW")
        imp_btn = Gtk.Button(child=imp_content)
        imp_btn.set_tooltip_text("Replace the bands shown here from a REW/AutoEQ file")
        imp_btn.connect("clicked", lambda *_: self._import_rew())
        suffix.append(clear_btn)
        suffix.append(imp_btn)
        suffix.append(add_btn)
        self.bands_group.set_header_suffix(suffix)

        self.bands_grid = Gtk.Grid(column_spacing=10, row_spacing=4)
        self.bands_grid.add_css_class("eqrow")
        self.bands_grid.set_hexpand(True)
        self.bands_grid.set_margin_top(6)
        self.bands_grid.set_margin_bottom(6)
        self.bands_group.add(self.bands_grid)

    def _build_picker_footer(self):
        """Create the picker footer actions (new / import / export profile)."""
        new_btn = Gtk.Button(label="Create new…")
        new_btn.add_css_class("flat")
        new_btn.set_halign(Gtk.Align.START)
        new_btn.connect("clicked", self._on_create_new)
        imp_btn = Gtk.Button(label="Import profile…")
        imp_btn.add_css_class("flat")
        imp_btn.set_halign(Gtk.Align.START)
        imp_btn.set_tooltip_text("Import a per-device-eq profile shared by someone else")
        imp_btn.connect("clicked", lambda *_: self._import_profile())
        exp_btn = Gtk.Button(label="Export this profile…")
        exp_btn.add_css_class("flat")
        exp_btn.set_halign(Gtk.Align.START)
        exp_btn.set_tooltip_text("Save the current profile to a file to share")
        exp_btn.connect("clicked", lambda *_: self._export_current())
        self.popover_footer.append(new_btn)
        self.popover_footer.append(imp_btn)
        self.popover_footer.append(exp_btn)
        self.profile_list.connect("row-activated", self._on_pick_row)

    def _install_shortcuts(self, app):
        """Register win.undo / win.redo actions with their accelerators."""
        for name, cb, accels in (("undo", self._undo, ["<Control>z"]),
                                 ("redo", self._redo,
                                  ["<Control><Shift>z", "<Control>y"])):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, cb=cb: cb())
            self.add_action(action)
            app.set_accels_for_action("win." + name, accels)

    # ---- scroll taming (wheel must not change spin/dropdown values) --------
    def _tame_scroll(self, widget):
        """Keep the wheel from editing a value; scroll the page instead."""
        ctrl = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL)
        ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        ctrl.connect("scroll", self._on_widget_scroll)
        widget.add_controller(ctrl)

    def _on_widget_scroll(self, ctrl, dx, dy):
        """CAPTURE-phase scroll handler backing _tame_scroll: forward the
        wheel to the enclosing scrolled page and swallow it here, so the
        spin button / dropdown value is left untouched.
        """
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
                          min(new, adj.get_upper() - adj.get_page_size()))
                adj.set_value(new)
        return True

    # ---- device ------------------------------------------------------------
    # ---- device picker (header) + follow default (live) -------------------
    def _init_devices(self):
        """Fill the device dropdown from PipeWire and select the default sink."""
        self.sinks = []
        miss = pipewire.missing_tools()
        if miss:
            self.live = False
            self.node = None
            self.window_title.set_subtitle("PipeWire tools not found")
            self.device_dd.set_sensitive(False)
            self.follow_btn.set_sensitive(False)
            return
        self.live = True
        try:
            self.device_dd.set_expression(
                Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
        except Exception:
            pass
        dump = pipewire.pw_dump()
        self.sinks = pipewire.list_sinks(dump)
        default = next((s["name"] for s in self.sinks if s["default"]), None)
        self.node = default or (self.sinks[0]["name"] if self.sinks else None)
        self._refresh_device_model()
        self.device_dd.set_sensitive(not self.follow_btn.get_active())

    def _refresh_device_model(self):
        """Rebuild the dropdown model after the sink set changed."""
        self._dev_guard = True
        try:
            model = Gtk.StringList()
            for s in self.sinks:
                model.append(s["desc"])
            self.device_dd.set_model(model)
            idx = next((i for i, s in enumerate(self.sinks) if s["name"] == self.node), -1)
            if idx >= 0:
                self.device_dd.set_selected(idx)
        finally:
            self._dev_guard = False

    def _select_device(self, name, load=True):
        """Programmatically select a sink; optionally load its bound profile."""
        idx = next((i for i, s in enumerate(self.sinks) if s["name"] == name), -1)
        if idx < 0:
            return
        self._dev_guard = True
        self.device_dd.set_selected(idx)
        self._dev_guard = False
        self.node = name
        if load:
            self._load_profile(self.store.binding_for(name) or CLEAN_ID)

    def _on_device_changed(self, *_):
        """Manual sink pick from the dropdown (ignored while following default)."""
        if self._dev_guard or self.follow_btn.get_active():
            return
        i = self.device_dd.get_selected()
        if 0 <= i < len(self.sinks):
            self.node = self.sinks[i]["name"]
            self._load_profile(self.store.binding_for(self.node) or CLEAN_ID)

    def _on_follow_toggled(self, *_):
        """Follow-default toggled; snap to the current default when turned on."""
        follow = self.follow_btn.get_active()
        self.device_dd.set_sensitive(not follow)
        if follow and self.live:
            self._poll()                    # snap to the current default now

    def _poll(self):
        """2 s heartbeat: fetch sinks and the default sink off the main thread."""
        if self._poll_busy:
            return True                      # previous poll still running
        self._poll_busy = True

        def work():
            sinks = default = None
            try:
                dump = pipewire.pw_dump()
                sinks = pipewire.list_sinks(dump)
                default = next((s["name"] for s in sinks if s["default"]), None)
            finally:
                GLib.idle_add(self._apply_poll, sinks, default)
        pipewire._in_thread(work)
        return True                          # keep the timer running

    def _apply_poll(self, sinks, default):
        """Consume poll results on the main loop (refresh model, follow default)."""
        self._poll_busy = False
        if sinks is None:
            return False
        prev_names = [s["name"] for s in self.sinks]
        new_names = [s["name"] for s in sinks]
        self.sinks = sinks
        if new_names != prev_names:
            self._refresh_device_model()
        if self.follow_btn.get_active() and default and default != self.node:
            self._select_device(default, load=True)
        return False

    # ---- slots / working profile body -------------------------------------
    def _slot(self, ch):
        """The slot dict for a channel key."""
        return self.slots.setdefault(ch, _new_slot())

    def _slot_to_dict(self, ch):
        """Serialize a slot into the plain profile-body form."""
        s = self.slots.get(ch) or _new_slot()
        return {"bands": [bnd.to_dict() for bnd in s["bands"]]}

    def _working_body(self):
        """Assemble the full profile body from the current editor state."""
        p = self.store.get(self.current_pid)
        return {"id": self.current_pid,
                "name": p.get("name", self.current_pid),
                "apply_all": self.apply_all,
                "preamp": float(self.preamp),
                "ch_keys": list(self.ch_keys),
                "all": self._slot_to_dict("all"),
                "channels": {k: self._slot_to_dict(k) for k in self.ch_keys}}

    def _load_slot(self, ch):
        """Show one channel slot in the editor (preamp, table, graph, title)."""
        prev = self._loading
        self._loading = True
        try:
            self.cur_ch = ch
            slot = self._slot(ch)
            self.bands = slot["bands"]               # alias: edits mutate the slot
            self.bands_group.set_title("Bands" if self.apply_all
                                       else "Bands · %s" % ch)
            self.preamp_spin.set_value(self.preamp)
            self._rebuild_bands()
            self.graph_area.queue_draw()
            self._update_headroom()
        finally:
            self._loading = prev

    # ---- channel selector --------------------------------------------------
    def _clear_box(self, box):
        """Remove every child of a container widget."""
        child = box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt

    def _build_channel_bar(self):
        """Rebuild the FL/FR toggle bar (hidden while channels are linked)."""
        self._clear_box(self.channel_bar)
        self._chan_buttons = {}
        first = None
        for k in self.ch_keys:
            btn = Gtk.ToggleButton(label=k)
            if first is None:
                first = btn
            else:
                btn.set_group(first)
            btn.set_active(k == self.cur_ch)
            btn.connect("toggled", self._make_chan_cb(k))
            self.channel_bar.append(btn)
            self._chan_buttons[k] = btn
        self.channel_bar.set_visible(not self.apply_all and len(self.ch_keys) > 1)

    def _make_chan_cb(self, key):
        """Factory: switch the edited channel to `ch`."""
        def cb(btn):
            if btn.get_active() and not self._loading:
                self._load_slot(key)     # view change only; nothing to re-apply
        return cb

    def _on_link(self, *_):
        """Handle the 'Same EQ for all channels' switch (profile apply_all)."""
        if self._loading:
            return
        self.apply_all = self.link_row.get_active()
        if self.apply_all:
            self.channel_bar.set_visible(False)
            self._load_slot("all")
        else:
            base = self._slot("all")
            for k in self.ch_keys:                 # seed empty channels from "all"
                s = self.slots.get(k)
                if not s or not s["bands"]:
                    self.slots[k] = _copy_slot(base)
            self.cur_ch = self.ch_keys[0] if self.ch_keys else "all"
            self._build_channel_bar()
            self.channel_bar.set_visible(len(self.ch_keys) > 1)
            self._load_slot(self.cur_ch)
        self._on_edit()

    # ---- profile load / edit ----------------------------------------------
    def _display_name(self, p):
        """Profile name for display ('Default (no EQ)' for Clean)."""
        if p["id"] == CLEAN_ID:
            return "Default (no EQ)"
        return p.get("name", p["id"])

    def _editable(self, pid):
        """True when the profile may be edited in place (a user profile)."""
        p = self.store.profiles.get(pid)
        return bool(p and not p.get("builtin") and pid != CLEAN_ID)

    def _load_profile(self, pid, apply=True):
        """Load profile `pid` into the editor and bind it to the device.
        With apply=True also publish its graph to the session metadata
        (primes the key for the first Bypass; see _apply_now).
        """
        self._loading = True
        try:
            self.current_pid = pid
            p = self.store.get(pid)
            self.apply_all = bool(p.get("apply_all", True))

            dev = []
            if self.live and self.node:
                try:
                    dev = pipewire.sink_channels(self.node)
                except Exception:
                    dev = []
            pch = list(p.get("ch_keys") or list((p.get("channels") or {}).keys()))
            self.ch_keys = dev or pch or ["FL", "FR"]

            self.preamp = float(p.get("preamp", 0.0))
            a = p.get("all") or {"bands": []}
            self.slots = {"all": {"bands": [eq.Band.from_dict(x)
                                            for x in a.get("bands", [])]}}
            pchan = p.get("channels") or {}
            for k in self.ch_keys:
                src = pchan.get(k) or {"bands": []}
                self.slots[k] = {"bands": [eq.Band.from_dict(x)
                                           for x in src.get("bands", [])]}

            self.cur_ch = "all" if self.apply_all else (self.ch_keys[0]
                                                        if self.ch_keys else "all")
            self.profile_button.set_label(self._display_name(p))
            self.bypass_row.set_active(False)
            self.link_row.set_active(self.apply_all)
            self._build_channel_bar()
            self._load_slot(self.cur_ch)
            self.store.set_binding(self.node, pid)
        finally:
            self._loading = False
        # remember built-ins you select, so they stay in the default picker list
        if pid != CLEAN_ID and self.store.get(pid).get("builtin") \
                and pid not in self.favorites:
            self.favorites.add(pid)
            _save_favorites(self.favorites)
        # fresh undo history for the newly loaded profile
        self._hist = [self._snapshot()]
        self._hidx = 0
        self._update_undo_buttons()
        if apply:
            self._apply_now()

    def _ensure_editable(self):
        """If the current profile is read-only (built-in / Clean), fork it to a
        new user profile so edits have somewhere to live."""
        if self._editable(self.current_pid):
            return
        body = self._working_body()
        src = self.store.get(self.current_pid)
        if self.current_pid == CLEAN_ID:
            body["name"] = self._unique_name("Custom EQ")
        else:
            body["name"] = self._unique_name(src.get("name", "Custom") + " (custom)")
        body.pop("id", None)               # let save_user mint a fresh id
        pid = self.store.save_user(body)
        self.favorites.add(pid)
        _save_favorites(self.favorites)
        self.current_pid = pid
        self.store.set_binding(self.node, pid)
        self.profile_button.set_label(self._display_name(self.store.get(pid)))

    def _on_edit(self):
        """Any edit: fork built-ins if needed, redraw, debounce the save."""
        if self._loading:
            return
        self._ensure_editable()
        self.graph_area.queue_draw()
        self._update_headroom()
        self._schedule_save()

    def _schedule_save(self):
        """(Re)arm the save debounce timer."""
        if self._save_source:
            GLib.source_remove(self._save_source)
        self._save_source = GLib.timeout_add(_SAVE_DEBOUNCE_MS, self._save_now)

    def _save_now(self):
        """Persist the working profile, apply it, and record undo history."""
        self._save_source = 0
        if self._editable(self.current_pid):
            self.store.save_user(self._working_body())
        self._apply_now()
        if not self._restoring:
            self._push_history()
        return GLib.SOURCE_REMOVE

    def _apply_now(self):
        """Publish the device's live state to the per-device-eq metadata:
        the graph string, or key removal when bypassed / empty.
        """
        if not self.live or not self.node:
            return
        node = self.node
        body = self._working_body()
        if self.bypass_row.get_active() or not eq.profile_has_content(body):
            pipewire._in_thread(lambda: pipewire.metadata_clear(node))
        else:
            graph = eq.profile_graph(body)
            pipewire._in_thread(lambda: pipewire.metadata_set(node, graph))

    # ---- undo / redo -------------------------------------------------------
    def _snapshot(self):
        """Serialize editor state for undo (the viewed channel is left out)."""
        keys = ["all"] + list(self.ch_keys)
        return {"apply_all": self.apply_all,
                "preamp": float(self.preamp),
                "ch_keys": list(self.ch_keys),
                "slots": {k: self._slot_to_dict(k) for k in keys}}

    def _restore(self, snap):
        """Load an undo snapshot back into the editor."""
        view = self.cur_ch          # keep the user's current tab if still valid
        self._loading = True
        try:
            self.apply_all = bool(snap["apply_all"])
            self.preamp = float(snap.get("preamp", 0.0))
            self.ch_keys = list(snap["ch_keys"])
            self.slots = {}
            for k, sd in snap["slots"].items():
                self.slots[k] = {"bands": [eq.Band.from_dict(x)
                                           for x in sd.get("bands", [])]}
            self.slots.setdefault("all", _new_slot())
            for k in self.ch_keys:
                self.slots.setdefault(k, _new_slot())
            if self.apply_all:
                self.cur_ch = "all"
            elif view in self.ch_keys:
                self.cur_ch = view
            else:
                self.cur_ch = self.ch_keys[0] if self.ch_keys else "all"
            self.link_row.set_active(self.apply_all)
            self._build_channel_bar()
            self._load_slot(self.cur_ch)
        finally:
            self._loading = False
        if self._editable(self.current_pid):
            self.store.save_user(self._working_body())
        self._apply_now()

    def _push_history(self):
        """Append a snapshot, dropping any redo tail (cap _HISTORY_CAP)."""
        snap = self._snapshot()
        if self._hidx < len(self._hist) - 1:        # drop the redo branch
            del self._hist[self._hidx + 1:]
        if not self._hist or self._hist[self._hidx] != snap:
            self._hist.append(snap)
            self._hidx = len(self._hist) - 1
            if len(self._hist) > _HISTORY_CAP:
                self._hist.pop(0)
                self._hidx -= 1
        self._update_undo_buttons()

    def _undo(self, *_):
        """Step one snapshot back in history."""
        if self._hidx <= 0:
            return
        self._hidx -= 1
        self._restoring = True
        try:
            self._restore(self._hist[self._hidx])
        finally:
            self._restoring = False
        self._update_undo_buttons()

    def _redo(self, *_):
        """Step one snapshot forward in history."""
        if self._hidx >= len(self._hist) - 1:
            return
        self._hidx += 1
        self._restoring = True
        try:
            self._restore(self._hist[self._hidx])
        finally:
            self._restoring = False
        self._update_undo_buttons()

    def _update_undo_buttons(self):
        """Sync undo/redo button sensitivity with the history position."""
        self.undo_btn.set_sensitive(self._hidx > 0)
        self.redo_btn.set_sensitive(self._hidx < len(self._hist) - 1)

    # ---- band table --------------------------------------------------------
    def _rebuild_bands(self):
        """Rebuild the band table as a freq-sorted view of self.bands."""
        self._clear_box(self.bands_grid)
        headers = ["", "Type", "Freq (Hz)", "Gain (dB)", "Q", "On", ""]
        for col, text in enumerate(headers):
            lbl = Gtk.Label(label=text, xalign=0.0)
            lbl.add_css_class("dim-label")
            lbl.add_css_class("caption")
            self.bands_grid.attach(lbl, col, 0, 1, 1)
        # The table is a freq-sorted VIEW; self.bands keeps its own order
        # (data order does not matter to the graph or the saved profile).
        self._row_bands = sorted(self.bands, key=lambda b: b.freq)
        for i, bnd in enumerate(self._row_bands):
            self._attach_band_row(i, bnd)

    def _maybe_resort(self):
        """Rebuild the table only if the sorted order actually changed;
        called on focus leaving a Freq field so rows never jump mid-typing.
        """
        want = sorted(self.bands, key=lambda b: b.freq)
        if [id(b) for b in want] != [id(b) for b in getattr(self, "_row_bands", [])]:
            self._rebuild_bands()
        return False

    def _attach_band_row(self, i, bnd):
        """Create one table row: dot, type, freq, gain, Q, On, remove."""
        row = i + 1
        dot = Gtk.Label()
        dot.set_use_markup(True)
        dot.set_valign(Gtk.Align.CENTER)
        dot.set_halign(Gtk.Align.CENTER)
        dot.set_markup(self._dot_markup(bnd.freq))
        self.bands_grid.attach(dot, 0, row, 1, 1)

        type_dd = Gtk.DropDown.new_from_strings(TYPE_NAMES)
        type_dd.set_selected(TYPE_NAMES.index(bnd.type) if bnd.type in TYPE_NAMES else 0)
        type_dd.set_valign(Gtk.Align.CENTER)
        type_dd.connect("notify::selected", self._make_type_cb(bnd))
        self._tame_scroll(type_dd)
        self.bands_grid.attach(type_dd, 1, row, 1, 1)

        freq = Gtk.SpinButton.new_with_range(FMIN, FMAX, 1.0)
        freq.set_digits(0); freq.set_value(bnd.freq)
        freq.set_hexpand(True); freq.set_halign(Gtk.Align.START)
        freq.set_valign(Gtk.Align.CENTER)
        freq.connect("value-changed", self._make_num_cb(bnd, "freq", dot))
        ffoc = Gtk.EventControllerFocus()
        ffoc.connect("leave", lambda *_: GLib.idle_add(self._maybe_resort))
        freq.add_controller(ffoc)
        self._tame_scroll(freq)
        self.bands_grid.attach(freq, 2, row, 1, 1)

        gain = Gtk.SpinButton.new_with_range(-DB_MAX, DB_MAX, 0.1)
        gain.set_digits(1); gain.set_value(bnd.gain)
        gain.set_hexpand(True); gain.set_halign(Gtk.Align.START)
        gain.set_valign(Gtk.Align.CENTER)
        gain.connect("value-changed", self._make_num_cb(bnd, "gain"))
        self._tame_scroll(gain)
        self.bands_grid.attach(gain, 3, row, 1, 1)

        q = Gtk.SpinButton.new_with_range(0.1, 10.0, 0.01)
        q.set_digits(2); q.set_value(bnd.q)
        q.set_hexpand(True); q.set_halign(Gtk.Align.START)
        q.set_valign(Gtk.Align.CENTER)
        q.connect("value-changed", self._make_num_cb(bnd, "q"))
        self._tame_scroll(q)
        self.bands_grid.attach(q, 4, row, 1, 1)

        on = Gtk.Switch()
        on.set_active(bnd.enabled)
        on.set_valign(Gtk.Align.CENTER)
        on.set_halign(Gtk.Align.CENTER)
        on.connect("notify::active", self._make_enable_cb(bnd))
        self.bands_grid.attach(on, 5, row, 1, 1)

        rm = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        rm.add_css_class("flat")
        rm.set_valign(Gtk.Align.CENTER)
        rm.set_halign(Gtk.Align.END)
        rm.connect("clicked", self._make_remove_cb(bnd))
        self.bands_grid.attach(rm, 6, row, 1, 1)

    def _dot_markup(self, freq):
        """Pango markup for the colored legend dot of a frequency."""
        r, g, bl = self._band_color(freq)
        return ("<span foreground='#%02x%02x%02x' size='large'>\u25cf</span>"
                % (int(r * 255), int(g * 255), int(bl * 255)))

    def _make_type_cb(self, bnd):
        """Factory: apply a filter-type change from the row's DropDown."""
        def cb(dd, _p):
            idx = dd.get_selected()
            if 0 <= idx < len(TYPE_NAMES):
                bnd.type = TYPE_NAMES[idx]
                self._on_edit()
        return cb

    def _make_num_cb(self, bnd, attr, dot=None):
        """Factory: apply a numeric change; freq changes also recolor the dot."""
        def cb(spin):
            setattr(bnd, attr, float(spin.get_value()))
            if dot is not None and attr == "freq":
                dot.set_markup(self._dot_markup(bnd.freq))
            self._on_edit()
        return cb

    def _make_enable_cb(self, bnd):
        """Factory: toggle a band on/off from the row's Switch."""
        def cb(sw, _p):
            bnd.enabled = sw.get_active()
            self._on_edit()
        return cb

    def _make_remove_cb(self, bnd):
        """Factory: delete the row's band."""
        def cb(_btn):
            if bnd in self.bands:
                self.bands.remove(bnd)
                self._rebuild_bands()
                self._on_edit()
        return cb

    def _on_add_band(self, _btn):
        """Append a fresh 1 kHz band and re-render."""
        self.bands.append(eq.Band("PK", 1000.0, 0.0, 1.0, True))
        self._rebuild_bands()
        self._on_edit()

    def _on_clear_bands(self, _btn):
        """Remove all bands of the shown slot (Ctrl+Z restores them)."""
        if not self.bands:
            return
        self.bands.clear()
        self._rebuild_bands()
        self._on_edit()

    def _on_preamp(self, spin):
        """Preamp spin changed: one shared value for the whole profile."""
        self.preamp = float(spin.get_value())
        self._on_edit()

    def _auto_preamp_db(self):
        """Preamp that zeroes the tier-1 estimate: the max of the edited
        chain's band curve (no preamp) -- or, with unlinked channels, of
        the WORST channel's curve, so one shared value clears every slot.
        Rounded UP to the 0.1 dB step the spin can express, so the result
        lands at or below 0 dBFS."""
        if self.apply_all:
            peak = eq.curve_max_db(0.0, self.bands)
        else:
            peak = max(eq.curve_max_db(0.0, self._slot(k)["bands"])
                       for k in self.ch_keys)
        return max(0.0, math.ceil(peak * 10.0 - 1e-9) / 10.0)

    def _on_auto(self, _btn):
        """Auto headroom (ROADMAP Task 2, tier 1): preamp = -(max of the
        summed EQ curve). Dueling boosts/cuts cancel in the sum, unlike the
        old -(largest single positive gain), which over-attenuated stacked-
        filter profiles by 2x and more (demo FR: -16.1 vs the correct -8.5).

        The preamp is ONE shared per-profile value (per-channel curves
        encode the driver matching; unequal preamps would shift the
        interchannel balance right back out of match), so with unlinked
        channels the requirement is the WORST channel's curve max.
        Deliberate offsets belong in the curves."""
        v = self._auto_preamp_db()
        self.preamp_spin.set_value(-v if v else 0.0)  # triggers _on_preamp

    def _on_bypass(self, *_):
        """Bypass toggled: republish the device state."""
        if not self._loading:
            self._apply_now()
        self._update_headroom()

    # ---- headroom / clip estimate (ROADMAP Task 2, tier 1) -----------------
    _CLIP_EPS_DB = 0.05     # "crossed 0 dBFS" once it can show as +0.1 dB

    def _applied_chains(self):
        """(key, slot) pairs the device actually runs: the linked "all" slot,
        or every per-channel slot when channels are unlinked."""
        if self.apply_all:
            return [("all", self._slot("all"))]
        return ([(k, self._slot(k)) for k in self.ch_keys]
                or [("all", self._slot("all"))])

    def _update_headroom(self):
        """Tier-1 clip estimate, no capture running:  monitor_peak +
        max(total EQ curve)  per applied chain, with the monitor peak pinned
        to 0 dBFS (legal full-scale content) until the live meter (tier 2)
        exists. The preamp is ONE shared per-profile value, so the row
        reads out the WORST applied chain -- exactly what its spin and
        Auto act on -- and turns error-red past 0 dBFS. Clipping is still
        per output channel: every over-0 channel is also flagged on its
        tab in the channel bar, with the numbers in the tab tooltips. In Bypass
        tier 1 has nothing to warn about -- the profile adds no gain and the
        input level is not measured yet -- so everything goes back to
        neutral (the tier-2 meter will keep flagging hot inputs here)."""
        chan_btns = getattr(self, "_chan_buttons", {})
        if self.bypass_row.get_active():
            self.clip_icon.set_visible(False)
            self.preamp_row.remove_css_class("clip-risk")
            self.preamp_spin.remove_css_class("error")
            self.preamp_row.set_subtitle(self._preamp_subtitle)
            self.preamp_row.set_tooltip_text(None)
            for b in chan_btns.values():
                b.remove_css_class("clip-risk")
                b.set_tooltip_text(None)
            return
        bounds = {k: eq.headroom_bound_db(self.preamp, s["bands"])
                  for k, s in self._applied_chains()}
        bound, key = max(((v, k) for k, v in bounds.items()),
                         key=lambda t: t[0])
        over = bound > self._CLIP_EPS_DB
        shown = bound if abs(bound) >= self._CLIP_EPS_DB else 0.0  # no "-0.0"
        where = "" if key in ("all", self.cur_ch) else " on %s" % key
        offenders = [(k, v) for k, v in bounds.items()
                     if v > self._CLIP_EPS_DB]

        self.clip_icon.set_visible(over)
        if over:
            self.preamp_row.add_css_class("clip-risk")
            self.preamp_spin.add_css_class("error")
            self.preamp_row.set_subtitle(
                "Worst-case post-EQ peak %+.1f dBFS%s — can clip"
                % (shown, where))
        else:
            self.preamp_row.remove_css_class("clip-risk")
            self.preamp_spin.remove_css_class("error")
            self.preamp_row.set_subtitle(
                "Worst-case post-EQ peak %+.1f dBFS%s" % (shown, where))

        tip = None
        if over:
            tip = ("Estimated for content peaking at 0 dBFS: the profile "
                   "pushes it past full scale (the input side is not "
                   "measured yet). Lower the shared preamp — Auto does it.")
            if len(offenders) > 1:
                listed = ", ".join("%s %+.1f" % (k, v) for k, v in offenders)
                tip += "\nOver 0 dBFS: %s." % listed
        self.preamp_row.set_tooltip_text(tip)
        self.clip_icon.set_tooltip_text(tip if over else None)

        for k, btn in chan_btns.items():
            v = bounds.get(k)
            if v is not None and v > self._CLIP_EPS_DB:
                btn.add_css_class("clip-risk")
                btn.set_tooltip_text(
                    "Worst-case post-EQ peak %+.1f dBFS — can clip" % v)
            else:
                btn.remove_css_class("clip-risk")
                btn.set_tooltip_text(None)

    # ---- profile picker ----------------------------------------------------
    def _on_picker_toggle(self, *_):
        """Refill the picker whenever the popover opens (GTK4 has no 'show')."""
        if self.profile_button.get_active():
            self._populate_picker()

    def _removable(self, p):
        """True when a picker row gets an X: user profiles (delete from disk)
        and built-ins you've pinned (unpin from favorites).
        """
        if p["id"] == CLEAN_ID:
            return False
        return (not p.get("builtin")) or (p["id"] in self.favorites)

    def _populate_picker(self):
        """Fill the profile list for the current search query."""
        self._clear_box(self.profile_list)
        query = self.search_entry.get_text().strip().lower()
        allp = self.store.ordered()
        if query:
            shown = [p for p in allp if query in self._display_name(p).lower()]
        else:
            # default view: Clean + all user profiles + built-ins you've used
            shown = [p for p in allp
                     if p["id"] == CLEAN_ID
                     or not p.get("builtin")
                     or p["id"] in self.favorites
                     or p["id"] == self.current_pid]
        shown.sort(key=lambda p: (p["id"] != CLEAN_ID,
                                  bool(p.get("builtin")),
                                  self._display_name(p).lower()))
        for p in shown:
            self.profile_list.append(self._picker_row(p))

    def _picker_row(self, p):
        """Build one picker row: check, name, clone/rename/delete buttons."""
        pid = p["id"]
        row = Gtk.ListBoxRow()
        row.pid = pid
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_top(8); box.set_margin_bottom(8)
        box.set_margin_start(10); box.set_margin_end(8)

        check = Gtk.Image.new_from_icon_name("object-select-symbolic")
        check.set_opacity(1.0 if pid == self.current_pid else 0.0)
        box.append(check)

        label = Gtk.Label(label=self._display_name(p), xalign=0.0)
        label.set_hexpand(True)
        box.append(label)

        is_user = (not p.get("builtin")) and pid != CLEAN_ID
        if pid != CLEAN_ID:
            clone = Gtk.Button.new_from_icon_name("edit-copy-symbolic")
            clone.add_css_class("flat")
            clone.set_valign(Gtk.Align.CENTER)
            clone.set_tooltip_text("Duplicate")
            clone.connect("clicked", self._make_clone_cb(p))
            box.append(clone)
        if is_user:
            edit = Gtk.Button.new_from_icon_name("document-edit-symbolic")
            edit.add_css_class("flat")
            edit.set_valign(Gtk.Align.CENTER)
            edit.set_tooltip_text("Rename")
            edit.connect("clicked", self._make_rename_cb(p))
            box.append(edit)

        if self._removable(p):
            x = Gtk.Button.new_from_icon_name("window-close-symbolic")
            x.add_css_class("flat")
            x.set_valign(Gtk.Align.CENTER)
            x.set_tooltip_text("Delete" if is_user else "Remove from list")
            x.connect("clicked", self._make_remove_fav_cb(p))
            box.append(x)

        row.set_child(box)
        return row

    def _on_pick_row(self, _listbox, row):
        """A profile row was activated: close the popover and load it."""
        pid = getattr(row, "pid", None)
        if pid is None:
            return
        self.profile_popover.popdown()
        self._load_profile(pid)

    def _make_remove_fav_cb(self, p):
        """Factory: unpin a built-in profile from favorites."""
        def cb(_btn):
            pid = p["id"]
            if not p.get("builtin"):     # user profile -> delete from disk (confirm)
                self._confirm_delete(p)
            else:                        # built-in / downloaded -> just unpin
                self.favorites.discard(pid)
                _save_favorites(self.favorites)
                self._populate_picker()
        return cb

    def _confirm_delete(self, p):
        """Confirm (AlertDialog) and delete a user profile from disk."""
        pid = p["id"]
        self.profile_popover.popdown()      # popovers sit above dialogs in GTK4
        dlg = Adw.AlertDialog(
            heading="Delete profile?",
            body="“%s” will be permanently deleted from disk. This cannot be undone."
                 % self._display_name(p))
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        def on_resp(_d, resp):
            if resp != "delete":
                return
            self.store.delete_user(pid)
            self.favorites.discard(pid)
            _save_favorites(self.favorites)
            if self.current_pid == pid:
                self._load_profile(CLEAN_ID)
            self._populate_picker()
        dlg.connect("response", on_resp)
        dlg.present(self)

    def _make_rename_cb(self, p):
        """Factory: open the rename dialog for the row's profile."""
        def cb(_btn):
            self._rename_profile(p)
        return cb

    def _rename_profile(self, p):
        """Rename a user profile in place (same id) via a dialog."""
        pid = p["id"]
        entry = Gtk.Entry()
        entry.set_text(p.get("name", ""))
        entry.set_activates_default(True)
        dlg = Adw.AlertDialog(heading="Rename profile",
                              body="Choose a new name for this profile.")
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("rename", "Rename")
        dlg.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("rename")
        dlg.set_close_response("cancel")

        def on_resp(_d, resp):
            if resp != "rename":
                return
            new = entry.get_text().strip()
            if not new:
                return
            prof = dict(self.store.get(pid))
            prof["name"] = new
            self.store.save_user(prof)       # same id, new name
            if pid == self.current_pid:
                self.profile_button.set_label(self._display_name(self.store.get(pid)))
            self._populate_picker()
        dlg.connect("response", on_resp)
        self.profile_popover.popdown()       # so the dialog isn't covered
        dlg.present(self)

    def _make_clone_cb(self, p):
        """Factory: duplicate the row's profile."""
        def cb(_btn):
            self._clone_profile(p)
        return cb

    def _clone_profile(self, p):
        """Deep-copy a profile into a new user profile and switch to it."""
        src = self.store.get(p["id"])
        body = {"name": self._unique_name(self._display_name(p) + " copy"),
                "apply_all": bool(src.get("apply_all", True)),
                "preamp": float(src.get("preamp", 0.0)),
                "ch_keys": list(src.get("ch_keys") or []),
                "channels": json.loads(json.dumps(src.get("channels") or {})),
                "all": json.loads(json.dumps(src.get("all") or {"preamp": 0.0, "bands": []}))}
        pid = self.store.save_user(body)
        self.favorites.add(pid)
        _save_favorites(self.favorites)
        self.profile_popover.popdown()
        self._load_profile(pid)              # switch to the new copy to edit it

    def _unique_name(self, base):
        """First of `base`, `base 2`, ... not taken by an existing profile."""
        names = {pp.get("name") for pp in self.store.profiles.values()}
        if base not in names:
            return base
        i = 2
        while "%s %d" % (base, i) in names:
            i += 1
        return "%s %d" % (base, i)

    # ---- create / import ---------------------------------------------------
    def _on_create_new(self, _btn):
        """Create and load a fresh empty user profile."""
        self.profile_popover.popdown()
        body = {"name": self._unique_name("New profile"), "apply_all": True,
                "preamp": 0.0, "ch_keys": [], "channels": {},
                "all": {"bands": []}}
        pid = self.store.save_user(body)
        self.favorites.add(pid)
        _save_favorites(self.favorites)
        self._load_profile(pid)

    def _import_rew(self):
        """Import a mono REW/AutoEQ text file into the CURRENT slot."""
        dialog = Gtk.FileDialog()
        dialog.set_title("Import REW / AutoEQ")

        def done(d, res):
            try:
                gfile = d.open_finish(res)
            except GLib.Error:
                return
            path = gfile.get_path() if gfile else None
            if not path:
                return
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                return
            preamp, bands = eq.parse_autoeq(text)     # REW/AutoEQ is mono
            self._apply_rew_import(preamp, bands)
        dialog.open(self, None, done)

    def _apply_rew_import(self, preamp, bands):
        """Load a parsed REW/AutoEQ result into the CURRENT slot. The file's
        preamp is honored only where the file sees the whole picture -- a
        linked profile's single chain. A per-ear file cannot know the other
        channels, so with unlinked channels its number is ignored and the
        shared preamp is recomputed from ALL channels' curves (the
        balanced-Auto requirement): exact, never stale, and better math
        than REW's -(largest gain) anyway. The content-aware tier-3 audit
        stays the authority for refining below this static bound."""
        self.slots[self.cur_ch] = {"bands": bands}
        if self.apply_all:
            self.preamp = float(preamp)
        else:
            v = self._auto_preamp_db()
            self.preamp = -v if v else 0.0
        self._load_slot(self.cur_ch)
        self._on_edit()

    def _import_profile(self):
        """Import a per-device-eq profile (our JSON, may be per-channel)."""
        self.profile_popover.popdown()
        dialog = Gtk.FileDialog()
        dialog.set_title("Import profile")

        def done(d, res):
            try:
                gfile = d.open_finish(res)
            except GLib.Error:
                return
            path = gfile.get_path() if gfile else None
            if not path:
                return
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                return
            if not isinstance(data, dict):
                return
            if data.get("version") != config.SCHEMA_VERSION:
                print("per-device-eq: not importing %s (profile schema v%s; "
                      "run tools/migrate_profiles_v1_to_v2.py once to "
                      "convert)" % (path, data.get("version", 1)),
                      file=sys.stderr)
                return
            fallback = os.path.splitext(os.path.basename(path))[0] or "Imported"
            body = {"name": self._unique_name(str(data.get("name") or fallback)),
                    "apply_all": bool(data.get("apply_all", True)),
                    "preamp": float(data.get("preamp", 0.0)),
                    "ch_keys": list(data.get("ch_keys") or []),
                    "channels": data.get("channels") or {},
                    "all": data.get("all") or {"bands": []}}
            pid = self.store.save_user(body)
            self.favorites.add(pid)
            _save_favorites(self.favorites)
            self._load_profile(pid)
        dialog.open(self, None, done)

    def _export_current(self):
        """Write the current profile (our JSON, per-channel) to a file to share."""
        self.profile_popover.popdown()
        p = self.store.get(self.current_pid)
        body = {"name": self._display_name(p),
                "version": config.SCHEMA_VERSION,
                "apply_all": bool(p.get("apply_all", True)),
                "preamp": float(p.get("preamp", 0.0)),
                "ch_keys": list(p.get("ch_keys") or []),
                "channels": p.get("channels") or {},
                "all": p.get("all") or {"bands": []}}
        dialog = Gtk.FileDialog()
        dialog.set_title("Export profile")
        dialog.set_initial_name(self._safe_filename(body["name"]) + ".json")

        def done(d, res):
            try:
                gfile = d.save_finish(res)
            except GLib.Error:
                return
            path = gfile.get_path() if gfile else None
            if not path:
                return
            if not path.endswith(".json"):
                path += ".json"
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(body, f, indent=2, ensure_ascii=False)
            except OSError:
                return
        dialog.save(self, None, done)

    @staticmethod
    def _safe_filename(name):
        """Filesystem-safe stem for export file names."""
        s = "".join(c if (c.isalnum() or c in " ._-") else "_" for c in name).strip()
        return s.replace(" ", "_") or "profile"

    # ---- FR graph ----------------------------------------------------------
    def _draw_graph(self, _area, cr, w, h, *_):
        """Cairo draw: grid + axis labels, response curve, band handles."""
        ml, mr, mt, mb = 44, 10, 10, 22
        pw_, ph = max(1, w - ml - mr), max(1, h - mt - mb)
        self._plot = (ml, mt, pw_, ph)
        cr.set_source_rgb(0.12, 0.12, 0.14); cr.paint()
        cr.rectangle(ml, mt, pw_, ph)
        cr.set_source_rgb(0.08, 0.08, 0.10); cr.fill()

        def x_of(f):
            return ml + (math.log10(f) - math.log10(FMIN)) / \
                (math.log10(FMAX) - math.log10(FMIN)) * pw_

        def y_of(db):
            return mt + (DB_MAX - db) / (2 * DB_MAX) * ph

        cr.set_line_width(1.0)
        cr.select_font_face("Sans", 0, 0); cr.set_font_size(9)
        for f in (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000):
            x = x_of(f)
            cr.set_source_rgba(1, 1, 1, 0.10)
            cr.move_to(x, mt); cr.line_to(x, mt + ph); cr.stroke()
            cr.set_source_rgba(1, 1, 1, 0.45)
            lab = ("%dk" % (f // 1000)) if f >= 1000 else str(f)
            cr.move_to(x - 8, mt + ph + 14); cr.show_text(lab)
        for db in range(int(-DB_MAX), int(DB_MAX) + 1, 6):
            y = y_of(db)
            cr.set_source_rgba(1, 1, 1, 0.16 if db == 0 else 0.08)
            cr.move_to(ml, y); cr.line_to(ml + pw_, y); cr.stroke()
            cr.set_source_rgba(1, 1, 1, 0.45)
            cr.move_to(4, y + 3); cr.show_text("%+d" % db)

        active = not self.bypass_row.get_active()
        freqs = _log_freqs(int(max(60, pw_)))
        curve = eq.response_db(self.preamp, self.bands, freqs)
        cr.set_source_rgb(0.30, 0.78, 1.0) if active \
            else cr.set_source_rgba(0.6, 0.6, 0.6, 0.7)
        cr.set_line_width(2.0)
        for i, f in enumerate(freqs):
            db = max(-DB_MAX, min(DB_MAX, curve[i]))
            px, py = x_of(f), y_of(db)
            cr.move_to(px, py) if i == 0 else cr.line_to(px, py)
        cr.stroke()
        if not active:
            cr.set_source_rgba(0.30, 0.78, 1.0, 0.5)
            cr.set_line_width(1.5); cr.set_dash([4, 4], 0)
            cr.move_to(ml, y_of(0)); cr.line_to(ml + pw_, y_of(0)); cr.stroke()
            cr.set_dash([], 0)

        for b in self.bands:
            bx = x_of(b.freq); by = y_of(max(-DB_MAX, min(DB_MAX, b.gain)))
            r, g, bl = self._band_color(b.freq)
            cr.arc(bx, by, 5.5, 0, 2 * math.pi)
            if b.enabled:
                cr.set_source_rgb(r, g, bl); cr.fill_preserve()
                cr.set_source_rgba(0, 0, 0, 0.55); cr.set_line_width(1.0); cr.stroke()
            else:
                cr.set_source_rgba(r, g, bl, 0.7)
                cr.set_line_width(1.5); cr.stroke()


class EqApplication(Adw.Application):
    def __init__(self):
        """Single-instance Adw application wrapper."""
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.win = None

    def do_activate(self):
        """Present the (single) main window."""
        if self.win is None:
            self.win = EqWindow(self)
        self.win.present()


def launch_gui():
    """Entry point used by the launcher: run the Adw application."""
    try:                      # `kill -USR1 <pid>` dumps a live stack to stderr
        import faulthandler, signal
        faulthandler.register(signal.SIGUSR1)
    except Exception:
        pass
    return EqApplication().run(None)
