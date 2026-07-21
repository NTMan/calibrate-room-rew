# -*- coding: utf-8 -*-
"""GTK4 / libadwaita GUI for per-device-eq.

Structure mirrors a GNOME "Sound" settings panel (the long-term goal is to be
liftable into gnome-control-center):
  * device context = the active output (Follow default),
  * a Weather-style profile picker in the header (empty query = favorites,
    typing = the whole catalog; check on the active one, x to remove),
  * the EQ controls inline on the page (FR graph + bands + preamp + bypass),
  * channel tabs ([All], or FL | FR | ... with the inline "Separate
    channels" switch at the end of the same row),
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

import json, math, os, sys, threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gio, GLib, Gdk, Adw, Pango

from . import __version__, config, eq, pipewire, integration
from .config import (APP_ID, CLEAN_ID, FAVORITES_FILE, UI_STATE_FILE,
                     UI_FILE_CANDIDATES)
from .peq_view import CollapsibleCard, PeqView
from .preferences import PreferenceLayers
from .profiles import ProfileStore, editor_body

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


def _fmt_hz(f):
    """20, 512.5, 8.1k -- the plaque's band endpoints."""
    if f >= 1000:
        return "%gk" % round(f / 1000.0, 1)
    return "%g" % round(f, 1)


def _load_ui_state():
    try:
        with open(UI_STATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_ui_state(d):
    try:
        os.makedirs(os.path.dirname(UI_STATE_FILE), exist_ok=True)
        tmp = UI_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, UI_STATE_FILE)
    except OSError:
        pass


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
        self.set_size_request(620, 560)  # floor: the taste table

        self.store = ProfileStore()
        self.favorites = set(_load_favorites())
        self.node = None
        self.live = False
        self.current_pid = CLEAN_ID
        self._save_source = 0
        self._loading = False
        self.sinks = []
        self._dev_guard = False
        self._pw = pipewire.app_state()
        self._pw_unsub = None
        self._measure_win = None

        # editor state ("slots": "all" + one per channel key)
        self.apply_all = True
        self.ch_keys = ["FL", "FR"]
        self.slots = {"all": _new_slot()}
        self.cur_ch = "all"
        self.preamp = 0.0
        self.bands = self.slots["all"]["bands"]     # alias of the current slot
        self._chan_buttons = {}

        # tier-2 live meter (engine created lazily: scipy is optional)
        self._meter = None
        self._meter_node = None
        self._meter_state = None
        self._meter_areas = {}
        self._bal = []
        self._sess_peak = None      # measured session max, post-EQ dBFS
        self._dead_frames = []      # capture-link watchdog (see below)
        self._meter_relinks = 0
        self._sess_samples = 0      # metered samples per channel (session)
        self.connect("map", lambda *_: self._update_meter())
        self.connect("unmap", lambda *_: self._update_meter())
        self.connect("close-request", self._stop_meter_on_close)

        # undo/redo history (serialized snapshots); the timeline
        # is GLOBAL -- device and taste edits interleave in it
        self._hist = []
        self._hidx = -1
        self._restoring = False
        self._dev_dirty = False     # a device-side edit awaits saving
        self.preamp_auto = True     # preamp follows Safe (Next #4)
        self._preamp_syncing = False
        self._auto_syncing = False
        self._autofit_syncing = False
        self._clamped_note = None
        self._graveyard = {}        # deleted profiles, session-kept
        self._pending_sel = None    # a mid-history switch, deferred

        b = Gtk.Builder.new_from_file(_ui_path())
        self._builder = b
        self.set_content(b.get_object("content"))
        self.header_bar = b.get_object("header_bar")
        self.header_note = b.get_object("header_note")
        self.profile_button = b.get_object("profile_button")
        # the header picker is a suffix in an expander-style
        # row: flat in-row chrome, not a raised pill (HIG
        # ruling: the container was the fix, not the button)
        self.profile_button.add_css_class("flat")
        self.profile_popover = b.get_object("profile_popover")
        self.search_entry = b.get_object("search_entry")
        self.profile_list = b.get_object("profile_list")
        self.device_dd = b.get_object("device_dd")
        self.follow_btn = b.get_object("follow_btn")
        self.sep_switch = b.get_object("sep_switch")
        self.channel_row = b.get_object("channel_row")
        self.bypass_row = Gtk.ToggleButton(label="Bypass")
        self.bypass_row.set_tooltip_text(
            "Hear the device raw: profile and taste muted")
        self.channel_bar = b.get_object("channel_bar")
        self.bands_group = b.get_object("bands_group")

        # ---- the device card (Next #2): picker + trust in the
        # header, the editor collapsing underneath ------------------
        self._ui_state = _load_ui_state()
        self.device_card = CollapsibleCard(
            expanded=bool(self._ui_state.get("device_card", False)),
            on_toggled=self._on_device_card_toggled)
        dt = Gtk.Label(label="Device", xalign=0.0)
        dt.add_css_class("heading")
        self.device_card.add_header(dt)
        self.device_hdr = Gtk.Label(xalign=0.0)
        self.device_hdr.add_css_class("dim-label")
        self.device_hdr.add_css_class("caption")
        self.device_hdr.set_ellipsize(Pango.EllipsizeMode.END)
        self.device_card.add_header(self.device_hdr, expand=True)
        self.device_card.add_header(self.profile_button)
        card_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                            spacing=12)
        for side in ("start", "end", "bottom"):
            getattr(card_body, "set_margin_" + side)(12)
        card_body.append(self.channel_row)
        card_body.append(self.bands_group)
        self.device_card.set_body(card_body)
        self._device_body = card_body
        b.get_object("device_card_slot").append(self.device_card)

        # ---- the taste card: the layer picker in the header, the
        # active layer's editor underneath ---------------------------
        self.taste_card = CollapsibleCard(
            expanded=bool(self._ui_state.get("taste_card", False)),
            on_toggled=self._on_taste_card_toggled)
        tl = Gtk.Label(label="Taste", xalign=0.0)
        tl.add_css_class("heading")
        self.taste_card.add_header(tl)
        spacer = Gtk.Box()
        self.taste_card.add_header(spacer, expand=True)
        self.taste_button = Gtk.MenuButton()
        self.taste_button.set_valign(Gtk.Align.CENTER)
        self.taste_button.set_always_show_arrow(True)
        self.taste_button.add_css_class("flat")
        self.taste_button.set_popover(self._build_taste_popover())
        self.taste_card.add_header(self.taste_button)
        tbody = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                        spacing=12)
        for side in ("start", "end", "bottom"):
            getattr(tbody, "set_margin_" + side)(12)
        self.taste_view = PeqView(self._on_taste_view_changed,
                                  compact=True)
        self.taste_hint = Gtk.Label(
            label="No taste layer active. Pick or create one to "
                  "dial your EQ over every device.")
        self.taste_hint.set_wrap(True)
        self.taste_hint.set_max_width_chars(46)
        self.taste_hint.add_css_class("dim-label")
        tbody.append(self.taste_hint)
        tbody.append(self.taste_view)
        self.taste_card.set_body(tbody)
        b.get_object("taste_card_slot").append(self.taste_card)

        self._install_css()
        self._build_header_buttons()
        self._build_graph()
        self._build_preamp()
        self._build_bands_area()
        self._wire_picker_actions(b)
        self._install_shortcuts(app)

        self.search_entry.connect("search-changed", lambda *_: self._populate_picker())
        self.sep_switch.connect("notify::active", self._on_link)
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
            self._pw_unsub = self._pw.subscribe(self._on_pw_state)
            self._pw.start()

    # ---- widget construction ----------------------------------------------
    def _build_header_buttons(self):
        """Undo/redo at the start of the header bar, the primary menu
        and the bypass switch at its end."""
        self.undo_btn = Gtk.Button.new_from_icon_name("edit-undo-symbolic")
        self.undo_btn.set_tooltip_text("Undo (Ctrl+Z)")
        self.undo_btn.connect("clicked", lambda *_: self._undo())
        self.redo_btn = Gtk.Button.new_from_icon_name("edit-redo-symbolic")
        self.redo_btn.set_tooltip_text("Redo (Ctrl+Shift+Z)")
        self.redo_btn.connect("clicked", lambda *_: self._redo())
        hist_pair = Gtk.Box(spacing=0, valign=Gtk.Align.CENTER)
        hist_pair.add_css_class("linked")
        hist_pair.append(self.undo_btn)
        hist_pair.append(self.redo_btn)
        self.header_bar.pack_start(hist_pair)
        self.pref_layers = PreferenceLayers()
        menu = Gio.Menu()
        menu.append("Export EQ\u2026", "win.export-eq")
        menu.append("About Per-Device EQ", "win.about")
        self.menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic")
        self.menu_btn.set_tooltip_text("Main menu")
        self.menu_btn.set_menu_model(menu)
        self.menu_btn.set_primary(True)
        self.header_bar.pack_end(self.menu_btn)
        self.header_bar.pack_end(self.bypass_row)
        self._sync_taste_card()

    def _build_graph(self):
        """Create the response plot (DrawingArea) with drag + right-click gestures."""
        # The one EQ editor -- graph over band table, shared with
        # the taste layers. Edits return through _on_view_changed.
        self.view = PeqView(self._on_view_changed,
                            on_import_file=self._import_rew)
        self.view.graph.set_content_height(240)

        self._canvas = None          # measurement overlay cache
        self.show_meas = True
        # Text registers only: their strings feed the card header's
        # dim trust line. The bar that showed them under the table
        # duplicated that line and is gone; the eye and Re-fit moved
        # into the Bands header (see _build_bands_area).
        self.trust_label = Gtk.Label(xalign=0.0)
        self.fit_state_label = Gtk.Label()
        self.fit_bar = Gtk.ProgressBar()
        self.fit_bar.set_show_text(True)
        fit_lbl = Gtk.Label(label="Calculating new fit")
        fit_lbl.add_css_class("title-4")
        self.fit_overlay = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.fit_overlay.add_css_class("osd")
        self.fit_overlay.set_halign(Gtk.Align.CENTER)
        self.fit_overlay.set_valign(Gtk.Align.CENTER)
        for side in ("top", "bottom", "start", "end"):
            getattr(self.fit_overlay, "set_margin_" + side)(12)
        self.fit_overlay.append(fit_lbl)
        self.fit_overlay.append(self.fit_bar)
        self.fit_overlay.set_visible(False)
        over = Gtk.Overlay()
        over.set_child(self.view)
        over.add_overlay(self.fit_overlay)
        self._fitting = False
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                       spacing=4)
        wrap.append(over)
        self.bands_group.add(wrap)

    def _on_device_card_toggled(self, expanded):
        self._ui_state["device_card"] = bool(expanded)
        _save_ui_state(self._ui_state)

    def _on_meas_toggle(self, btn):
        self.show_meas = btn.get_active()
        self._sync_view_curves()

    def _sync_view_curves(self):
        """Push the measurement overlay (or its absence) into the
        editor: the view renders what it is handed, never fetches."""
        ov = self._overlay_curve()
        if ov is None:
            self.view.set_curves(None, None)
        else:
            f, meas, spread, band = ov
            self.view.set_curves(f, meas, spread, band)

    def _on_view_changed(self, bands, final):
        """The editor reports an edit of the shown slot: land it in
        the slot IN PLACE (the alias and the slot share the list)
        and run the classic pipeline -- fork built-ins, live apply,
        headroom, debounced save with undo history."""
        if self._loading:
            return
        self._ensure_audible()
        self.bands[:] = [eq.Band.from_dict(b) for b in bands]
        self._on_edit()

    def _overlay_curve(self):
        """(freqs, measured, spread, trust_band) for the slot on
        screen, or None: no canvas, overlay off, or the slot has no
        measured channel ('all' over a multi-channel canvas)."""
        c = self._canvas
        if not c or not self.show_meas or not c.get("curves"):
            return None
        key = self.cur_ch
        if key not in c["curves"]:
            if self.apply_all and len(c["curves"]) == 1:
                key = next(iter(c["curves"]))
            else:
                return None
        cv = c["curves"][key]
        return cv["f"], cv["meas"], cv["spread"], c["band"].get(key)

    def _canvas_refresh(self):
        """Recompute the measurement overlay and the trust plaque for
        the loaded profile: cheap (a few ms over the stored
        magnitudes), runs on every load and debounced save so the
        stale / incomplete / edited chips never lie."""
        p = self.store.get(self.current_pid) or {}
        m = p.get("measurement")
        has = isinstance(m, dict) and bool(m.get("takes"))
        self.meas_toggle.set_visible(bool(has))
        if not has:
            self._canvas = None
            self.device_hdr.set_text("")
            self._sync_view_curves()
            return
        from . import refit, trust      # heavy deps stay lazy
        from . import export_peq, fit_peq
        fit = p.get("fit") or {}
        params = fit.get("params") or {}
        cache = {"curves": {}, "band": {}, "err": None,
                 "ylo": None, "yhi": None}
        try:
            results, _ = refit.channel_results(
                m, smoothing=params.get("smoothing", 6))
        except refit.RefitError as e:
            cache["err"] = str(e)
            results = {}
        rep = trust.assess(p)
        lo_all = hi_all = None
        for key, r in results.items():
            d = r["data"]
            f = [float(v) for v in d["freq_hz"]]
            mag = [float(v) for v in d["mag_db_smoothed"]]
            band = ((rep or {}).get("channels", {})
                    .get(key, {}).get("band"))
            nlo = params.get("f_lo") or (band[0] if band else f[0])
            nhi = params.get("f_hi") or (band[1] if band else f[-1])
            sel = [v for fv, v in zip(f, mag) if nlo <= fv <= nhi]
            off = (sum(sel) / len(sel)) if sel else 0.0
            meas = [v - off for v in mag]
            sp = d.get("spread_db")
            spread = ([float(v) for v in sp]
                      if sp is not None else None)
            lo = min(v - (spread[i] if spread else 0.0)
                     for i, v in enumerate(meas))
            hi = max(v + (spread[i] if spread else 0.0)
                     for i, v in enumerate(meas))
            lo_all = lo if lo_all is None else min(lo_all, lo)
            hi_all = hi if hi_all is None else max(hi_all, hi)
            cache["curves"][key] = {"f": f, "meas": meas,
                                    "spread": spread}
            cache["band"][key] = band
            bl = [b for b in ((p.get("channels") or {})
                              .get(key, {}).get("bands") or [])
                  if b.get("enabled", True)]
            if bl and fit:
                fgd, des, _m = fit_peq.desired_curve(f, mag,
                                                     nlo, nhi)
                rv = export_peq.chain_fit_residual(
                    list(fgd), list(des), bl,
                    cap=float(params.get("max_boost", 6.0)))
                worst = (rv if cache.get("fit_resid") is None
                         else max(cache["fit_resid"], rv))
                cache["fit_resid"] = worst
        if lo_all is not None:
            cache["ylo"], cache["yhi"] = lo_all - 3.0, hi_all + 3.0
        self._canvas = cache

        ids = {t.get("id") for t in m.get("takes") or []}
        used = set(fit.get("takes") or [])
        stale = refit.fit_is_stale(p)
        incomplete = bool(fit) and bool(ids - used)
        edited = bool(fit.get("edited"))
        if cache["err"]:
            txt = "Measurement: %s" % cache["err"]
        elif rep:
            band = rep.get("band")
            btxt = ("%s–%s Hz" % (_fmt_hz(band[0]),
                                       _fmt_hz(band[1]))
                    if band else "no controlled band")
            txt = "Trust %d · %s" % (rep.get("score", 0), btxt)
            fr = cache.get("fit_resid")
            if fr is not None:
                txt += " · fit %.2f dB" % fr
        else:
            txt = "Measurement attached"
        self.trust_label.set_text(txt)
        tips = list((rep or {}).get("reasons") or [])
        if cache.get("fit_resid") is not None:
            tips.append("fit: the worst channel's tracking error"
                        " vs the capped desired correction")
        self.device_hdr.set_tooltip_text("\n".join(tips) or None)
        chips = [t for t, on in (("stale", stale),
                                 ("incomplete", incomplete),
                                 ("edited", edited)) if on]
        self.fit_state_label.set_visible(bool(chips))
        self.fit_state_label.set_text(" · ".join(chips))
        for cls in ("error", "warning", "dim-label"):
            self.fit_state_label.remove_css_class(cls)
        if chips:
            self.fit_state_label.add_css_class(
                "error" if stale else
                "warning" if incomplete else "dim-label")
        self._autofit_syncing = True
        try:
            self.refit_btn.set_active(bool(fit) and not edited
                                      and not stale)
        finally:
            self._autofit_syncing = False
        self.refit_btn.set_sensitive(
            not cache["err"] and bool(fit or ids)
            and self._editable(self.current_pid))
        bits = [self.trust_label.get_text(),
                self.fit_state_label.get_text()]
        self.device_hdr.set_text(
            " \u00b7 ".join(x for x in bits if x))
        self._sync_view_curves()

    def _on_refit(self, *_):
        """Re-fit the profile from its own canvas; hand edits ask."""
        p = self.store.get(self.current_pid) or {}
        if (p.get("fit") or {}).get("edited"):
            dlg = Adw.AlertDialog(
                heading="Discard hand edits?",
                body="The bands were edited by hand after the fit. "
                     "Re-fitting rebuilds them from the stored "
                     "measurement and discards those edits.")
            dlg.add_response("cancel", "Cancel")
            dlg.add_response("refit", "Re-fit")
            dlg.set_response_appearance(
                "refit", Adw.ResponseAppearance.DESTRUCTIVE)
            dlg.set_default_response("cancel")
            dlg.set_close_response("cancel")
            dlg.connect("response", lambda _d, r:
                        self._refit_now() if r == "refit" else None)
            dlg.present(self)
            return
        self._refit_now()

    def _refit_now(self):
        self._start_profile_fit(self.current_pid, allow_edited=True)

    def _start_profile_fit(self, pid, bands=None, f_lo=None,
                           f_hi=None, allow_edited=False):
        """The one fit runner: the plaque's Re-fit and the closing
        measurement window both land here. Runs refit_and_save on a
        worker while an OSD overlay with a real per-channel progress
        bar sits over the response plot; the graph is insensitive
        for the duration so a drag cannot race the reload."""
        if self._fitting or self.store.get(pid) is None:
            return
        self._fitting = True
        self.fit_bar.set_fraction(0.0)
        self.fit_bar.set_text("Fitting\u2026")
        self.fit_overlay.set_visible(True)
        self.view.set_sensitive(False)
        self.refit_btn.set_sensitive(False)
        res = {}

        def tick(frac, key, band, horizon, evals):
            def ui():
                self.fit_bar.set_fraction(frac)
                if key and horizon:
                    self.fit_bar.set_text(
                        "Fitting %s -- band %d/%d,"
                        " %d evaluations"
                        % (key, min(band + 1, horizon), horizon,
                           evals))
                else:
                    self.fit_bar.set_text(
                        "Fitting %s\u2026" % key if key
                        else "Done")
                return False
            GLib.idle_add(ui)

        def done():
            self._fitting = False
            self.fit_overlay.set_visible(False)
            self.view.set_sensitive(True)
            self.refit_btn.set_sensitive(True)
            if res.get("err") is not None:
                dlg = Adw.AlertDialog(
                    heading="Could not fit",
                    body=str(res["err"]))
                dlg.add_response("ok", "OK")
                dlg.set_default_response("ok")
                dlg.set_close_response("ok")
                dlg.present(self)
            elif self.current_pid == pid:
                self._load_profile(pid)
            else:
                self._populate_picker()
            return False

        def work():
            from . import measure_build
            try:
                measure_build.refit_and_save(
                    self.store, pid, bands=bands, f_lo=f_lo,
                    f_hi=f_hi, allow_edited=allow_edited,
                    progress=tick)
            except Exception as e:
                res["err"] = e
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()

    # ---- graph geometry / interaction (drag band handles) ------------------
    def _ensure_audible(self):
        """Drop Bypass so a graph edit is heard immediately."""
        if self.bypass_row.get_active():
            self._loading = True
            self.bypass_row.set_active(False)
            self._loading = False

    def _build_preamp(self):
        """The closing card, hand-rolled like the others: a header
        row (the Preamp title, the readout, the warning, the spin,
        the Auto mode) over the per-channel meters."""
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.add_css_class("card")
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                      spacing=8)
        for side in ("top", "bottom"):
            getattr(hdr, "set_margin_" + side)(8)
        for side in ("start", "end"):
            getattr(hdr, "set_margin_" + side)(12)
        pt = Gtk.Label(label="Preamp", xalign=0.0)
        pt.add_css_class("heading")
        hdr.append(pt)
        self.preamp_sub = Gtk.Label(xalign=0.0)
        self.preamp_sub.add_css_class("dim-label")
        self.preamp_sub.add_css_class("caption")
        self.preamp_sub.set_hexpand(True)
        self.preamp_sub.set_ellipsize(Pango.EllipsizeMode.END)
        hdr.append(self.preamp_sub)
        self.preamp_spin = Gtk.SpinButton.new_with_range(-DB_MAX, DB_MAX, 0.5)
        self.preamp_spin.set_digits(1)
        self.preamp_spin.set_valign(Gtk.Align.CENTER)
        self.preamp_spin.connect("value-changed", self._on_preamp)
        self._tame_scroll(self.preamp_spin)
        self.auto_button = Gtk.ToggleButton(label="Auto")
        self.auto_button.set_active(True)
        self.auto_button.set_valign(Gtk.Align.CENTER)
        self.auto_button.set_tooltip_text(
            "Preamp follows the Safe value on every edit of either "
            "layer. Turning the preamp by hand un-presses this and "
            "leaves you in charge; the session clamp still pulls the "
            "level down when a real peak clips. One shared value for "
            "all channels, so the balance encoded by the curves "
            "survives.")
        self.auto_button.connect("toggled", self._on_auto_toggled)
        # tier-1 clip lamp (ROADMAP Task 2): shown when the headroom estimate
        # crosses 0 dBFS; the row subtitle carries the estimate readout.
        self.clip_icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        self.clip_icon.add_css_class("error")
        self.clip_icon.set_valign(Gtk.Align.CENTER)
        self.clip_icon.set_visible(False)
        hdr.append(self.clip_icon)
        ppair = Gtk.Box(spacing=0, valign=Gtk.Align.CENTER)
        ppair.add_css_class("linked")
        ppair.append(self.preamp_spin)
        ppair.append(self.auto_button)
        hdr.append(ppair)
        card.append(hdr)
        self._preamp_subtitle = "Headroom for this device"
        # the closing card's second half: per-channel level meters
        # (rows are rebuilt whenever the channel set changes)
        self.meters_grid = Gtk.Grid(column_spacing=10, row_spacing=4)
        for side in ("top", "bottom", "start", "end"):
            getattr(self.meters_grid, "set_margin_" + side)(10)
        self._meters_row = self.meters_grid
        self._meters_row.set_visible(False)
        card.append(self.meters_grid)
        b = self._builder
        b.get_object("preamp_card_slot").append(card)

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
            " { min-height: 24px; padding-top: 1px;"
            " padding-bottom: 1px; }")
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
        """The editor's action strip inside the device card: the
        measurement eye on the left, Import and the Auto-fit mode
        on the right. The Bands caption died -- the channel tab
        above already says it; Clear died -- six trash cans and
        Ctrl+Z cover it."""
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                      spacing=6)
        self.meas_toggle = Gtk.ToggleButton()
        self.meas_toggle.set_icon_name("view-reveal-symbolic")
        self.meas_toggle.set_active(True)
        self.meas_toggle.add_css_class("flat")
        self.meas_toggle.set_visible(False)
        self.meas_toggle.set_tooltip_text(
            "Show the measurement behind this profile")
        self.meas_toggle.connect("toggled", self._on_meas_toggle)
        row.append(self.meas_toggle)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        row.append(spacer)
        self.refit_btn = Gtk.ToggleButton(label="Auto")
        self.refit_btn.set_tooltip_text(
            "The EQ follows the measurement. Hand edits un-press "
            "this; pressing it re-fits from the stored takes and "
            "returns the scientific correction")
        self.refit_btn.connect("toggled", self._on_autofit_toggled)
        row.append(self.refit_btn)
        self._device_body.insert_child_after(row, self.channel_row)

    def _ask_integration(self):
        """The hook is what keeps the EQ across reboots and
        reconnects. A portable run without it used to install
        silently; now it ASKS -- No quits (the app cannot keep its
        promise of persistence), Yes installs and says how to
        uninstall."""
        if integration.hook_installed():
            return False
        dlg = Adw.AlertDialog(
            heading="Install system integration?",
            body="per-device-eq keeps your EQ across reboots and "
                 "reconnects through a small WirePlumber hook in "
                 "your user session, plus a menu entry and icon "
                 "(everything under ~/.local and ~/.config). "
                 "Install it now?")
        dlg.add_response("no", "Quit")
        dlg.add_response("yes", "Install")
        dlg.set_response_appearance("yes",
                                    Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("yes")
        dlg.set_close_response("no")

        def done(_d, resp):
            if resp != "yes":
                self.get_application().quit()
                return
            # the dialog is the GUI face of --install, and they
            # SHARE the routine now -- one source of truth
            try:
                integration.install_full()
            except FileNotFoundError as e:
                err = Adw.AlertDialog(heading="Install failed",
                                      body=str(e))
                err.add_response("ok", "OK")
                err.present(self)
                return
            info = Adw.AlertDialog(
                heading="Integration installed",
                body="To remove it later, run:\n"
                     "per-device-eq.py --uninstall")
            info.add_response("ok", "OK")
            info.present(self)
        dlg.connect("response", done)
        dlg.present(self)
        return False

    def _on_autofit_toggled(self, btn):
        """Pressing Auto returns the scientific correction: a
        re-fit from the stored takes (hand edits ask first).
        Un-pressing by hand means nothing -- edits un-press it
        themselves."""
        if self._autofit_syncing or self._loading:
            return
        if btn.get_active():
            self._on_refit()
        else:
            self._autofit_syncing = True
            try:
                btn.set_active(True)
            finally:
                self._autofit_syncing = False

    def _wire_picker_actions(self, b):
        """The picker's actions live beside the search entry now --
        New and Import as flat icon buttons, Text-Editor style --
        and the list itself is a navigation sidebar: compact rows,
        no separators."""
        new_btn = b.get_object("picker_new_btn")
        new_btn.add_css_class("flat")
        new_btn.connect("clicked", self._on_create_new)
        imp_btn = b.get_object("picker_import_btn")
        imp_btn.add_css_class("flat")
        imp_btn.connect("clicked", lambda *_: self._import_profile())
        self.profile_list.connect("row-activated", self._on_pick_row)

    def _install_shortcuts(self, app):
        """Register the win.* actions: undo / redo with their
        accelerators, the primary-menu items without."""
        for name, cb, accels in (("undo", self._undo, ["<Control>z"]),
                                 ("redo", self._redo,
                                  ["<Control><Shift>z", "<Control>y"]),
                                 ("export-eq", self._on_export_eq, []),
                                 ("about", self._on_about, [])):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, cb=cb: cb())
            self.add_action(action)
            if accels:
                app.set_accels_for_action("win." + name, accels)

    def _on_export_eq(self):
        """Open the export wizard (primary menu). Imported lazily:
        the dialog module is only needed once someone exports."""
        from .export_wizard import ExportDialog
        ExportDialog(self).present(self)

    def _on_about(self):
        d = Adw.AboutDialog(
            application_name="Per-Device EQ",
            application_icon=APP_ID,
            developer_name="Mikhail Gavrilov",
            version=__version__,
            website="https://github.com/NTMan/per-device-eq",
            issue_url="https://github.com/NTMan/per-device-eq"
                      "/issues",
            license_type=Gtk.License.GPL_3_0)
        d.present(self)

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
            self.header_note.set_text("PipeWire tools not found")
            self.header_note.set_visible(True)
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
            self._maybe_follow(self._pw.default_sink)

    def _on_pw_state(self, st):
        """PWState refresh: keep the device model current and, with follow
        on and no measure window open, chase the default sink. One poll in
        pipewire feeds this instead of a per-window timer."""
        prev = [s["name"] for s in self.sinks]
        self.sinks = st.sinks
        if [s["name"] for s in st.sinks] != prev:
            self._refresh_device_model()
        self._maybe_follow(st.default_sink)
        return False

    def _maybe_follow(self, default):
        """Following works even while a measurement window is
        open (field verdict). Playback and measurement are not
        coupled: finalize binds by the measure window's OWN sink
        node, so the main window moving with the system default
        cannot corrupt the result. The old freeze bought no
        correctness and set a trap instead -- following resumed
        only on the NEXT default change, so after closing the
        measurement the app could sit on yesterday's sink until
        the default was toggled again by hand."""
        if (self.follow_btn.get_active() and default
                and default != self.node):
            self._select_device(default, load=True)

    # ---- slots / working profile body -------------------------------------
    def _slot(self, ch):
        """The slot dict for a channel key."""
        return self.slots.setdefault(ch, _new_slot())

    def _slot_to_dict(self, ch):
        """Serialize a slot into the plain profile-body form."""
        s = self.slots.get(ch) or _new_slot()
        return {"bands": [bnd.to_dict() for bnd in s["bands"]]}

    def _working_body(self):
        """Assemble the full profile body from the current editor
        state. editor_body() reattaches the stored v3 blocks (the
        editor edits sound, not the canvas) and marks a measured fit
        whose sound diverged as edited."""
        p = self.store.get(self.current_pid)
        body = {"id": self.current_pid,
                "name": (p or {}).get("name", self.current_pid),
                "apply_all": self.apply_all,
                "preamp": float(self.preamp),
                "preamp_auto": bool(self.preamp_auto),
                "ch_keys": list(self.ch_keys),
                "all": self._slot_to_dict("all"),
                "channels": {k: self._slot_to_dict(k)
                             for k in self.ch_keys}}
        return editor_body(body, p)

    def _load_slot(self, ch):
        """Show one channel slot in the editor (preamp, table, graph, title)."""
        prev = self._loading
        self._loading = True
        try:
            self.cur_ch = ch
            slot = self._slot(ch)
            self.bands = slot["bands"]               # alias: edits mutate the slot
            self.preamp_spin.set_value(self.preamp)
            self.view.set_bands([b.to_dict() for b in self.bands])
            self.view.set_preamp(self.preamp)
            self._sync_view_curves()
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
        """Rebuild the channel tab bar: a lone [All] tab while channels are
        linked, FL | FR | ... when separated. The row also hosts the
        "Separate channels" switch, so it is always visible on multichannel
        devices -- which gives the clip badge a home in both modes."""
        self._clear_box(self.channel_bar)
        self._chan_buttons = {}
        keys = ["all"] if self.apply_all else list(self.ch_keys)
        show_meters = pipewire.meter_available()
        first = None
        for k in keys:
            btn = Gtk.ToggleButton(
                label="All" if k == "all" else k)
            if first is None:
                first = btn
            else:
                btn.set_group(first)
            btn.set_active(k == self.cur_ch)
            if len(keys) == 1:
                btn.set_can_target(False)   # lone [All]: a tab, not a control
            btn.connect("toggled", self._make_chan_cb(k))
            self.channel_bar.append(btn)
            self._chan_buttons[k] = btn
        self.channel_row.set_visible(len(self.ch_keys) > 1)
        self._rebuild_meter_rows(show_meters)

    def _rebuild_meter_rows(self, show):
        """Per-channel post-composition levels with clip lamps, in
        the closing card next to the preamp -- they belong to the
        SUM of the layers, not to one, and they must survive the
        cards folding."""
        self._clear_box(self.meters_grid)
        self._meter_areas = {}
        self._meter_lamps = {}
        self._meters_row.set_visible(show and bool(self.ch_keys))
        for i, k in enumerate(self.ch_keys):
            lbl = Gtk.Label(label=k, xalign=0.0)
            lbl.add_css_class("dim-label")
            lbl.add_css_class("caption")
            self.meters_grid.attach(lbl, 0, i, 1, 1)
            area = Gtk.DrawingArea()
            area.set_hexpand(True)
            area.set_content_height(12)
            area.set_valign(Gtk.Align.CENTER)
            area.set_draw_func(self._draw_level_bar, i)
            self.meters_grid.attach(area, 1, i, 1, 1)
            lamp = Gtk.Image.new_from_icon_name(
                "dialog-warning-symbolic")
            lamp.add_css_class("error")
            lamp.set_visible(False)
            self.meters_grid.attach(lamp, 2, i, 1, 1)
            self._meter_areas[i] = area
            self._meter_lamps[i] = lamp

    def _draw_level_bar(self, _area, cr, w, h, i):
        """Post-EQ level, -24..+3 dB, horizontal: blue below the
        0 dBFS line, red above it -- the old tab meters' palette,
        moved to the closing card."""
        LO, HI = -24.0, 3.0
        st = self._meter_state

        def x_of(v):
            return (min(max(v, LO), HI) - LO) / (HI - LO) * w
        zero = x_of(0.0)
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.28)
        cr.rectangle(0, 0, w, h)
        cr.fill()
        db = st[i][0] if st and i < len(st) else LO
        if db > LO:
            xr = x_of(db)
            cr.set_source_rgba(0.22, 0.52, 0.90, 1.0)
            cr.rectangle(0, 0, min(xr, zero), h)
            cr.fill()
            if db > 0.0:
                cr.set_source_rgba(0.87, 0.19, 0.19, 1.0)
                cr.rectangle(zero, 0, xr - zero, h)
                cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.55)
        cr.rectangle(zero - 0.5, 0, 1, h)
        cr.fill()

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
        self.apply_all = not self.sep_switch.get_active()
        if self.apply_all:
            self.cur_ch = "all"
            self._build_channel_bar()
            self._load_slot("all")
        else:
            base = self._slot("all")
            for k in self.ch_keys:                 # seed empty channels from "all"
                s = self.slots.get(k)
                if not s or not s["bands"]:
                    self.slots[k] = _copy_slot(base)
            self.cur_ch = self.ch_keys[0] if self.ch_keys else "all"
            self._build_channel_bar()
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

    def _load_profile(self, pid, apply=True, born=False):
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
            self.preamp_auto = bool(p.get("preamp_auto", True))
            self._clamped_note = None
            self._auto_syncing = True
            try:
                self.auto_button.set_active(self.preamp_auto)
            finally:
                self._auto_syncing = False
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
            self.sep_switch.set_active(not self.apply_all)
            self._build_channel_bar()
            self._load_slot(self.cur_ch)
            self.store.set_binding(self.node, pid)
        finally:
            self._loading = False
        # land the composed Safe under Auto: a profile saved before
        # Auto existed (or whose cache went stale while nobody was
        # editing) carries a preamp the current chain never
        # blessed; in manual this only refreshes the readout
        self._update_headroom()
        # remember built-ins you select, so they stay in the default picker list
        if pid != CLEAN_ID and self.store.get(pid).get("builtin") \
                and pid not in self.favorites:
            self.favorites.add(pid)
            _save_favorites(self.favorites)
        # The timeline survives profile switches: the first load
        # seeds it; a later switch adds a SILENT selection baseline
        # that undo/redo walk through without counting it a step --
        # it exists so an edit made here has a pre-state to revert
        # to, since device snapshots carry one profile, not all.
        if not self._hist:
            self._hist = [self._snapshot()]
            self._hidx = 0
        elif not self._restoring:
            # A freshly created profile (clone, import) is a real
            # step: undo deletes it into the graveyard. A plain
            # switch is a selection baseline -- pushed at the tip,
            # but DEFERRED when we stand mid-history, so a mere
            # selection cannot destroy the redo branch; the next
            # real edit materializes it (and truncates rightfully).
            if born:
                self._push_history(born=pid)
            elif self._hidx < len(self._hist) - 1:
                self._pending_sel = self._snapshot()
            else:
                self._push_history(sel=True)
        self._update_undo_buttons()
        if apply:
            self._apply_now()
        self._canvas_refresh()

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
        self._dev_dirty = True
        self.view.graph.queue_draw()
        # any edit starts a new measurement era: peak, percentages and
        # the latch all belonged to the old chain
        self._sess_peak = None
        self._sess_samples = 0
        for b in self._bal:
            b.reset_session()
        self._update_headroom()
        self._schedule_save()

    def _schedule_save(self):
        """(Re)arm the save debounce timer."""
        if self._save_source:
            GLib.source_remove(self._save_source)
        self._save_source = GLib.timeout_add(_SAVE_DEBOUNCE_MS, self._save_now)

    def _save_now(self):
        """Land the debounce: persist and apply the device profile
        when a device-side edit is pending, and record ONE history
        entry either way -- the timeline is global, taste edits
        arrive here through the same timer."""
        self._save_source = 0
        if self._dev_dirty:
            self._dev_dirty = False
            if self._editable(self.current_pid):
                self.store.save_user(self._working_body())
            self._apply_now()
            if not self._restoring:
                self._push_history()
            self._canvas_refresh()
        elif not self._restoring:
            self._push_history()
        return GLib.SOURCE_REMOVE

    def _apply_now(self):
        """Publish the device's live state to the per-device-eq metadata:
        the graph string, or key removal when bypassed / empty.
        """
        self._update_meter()
        if not self.live or not self.node:
            return
        node = self.node
        body = self._working_body()
        extra = self.pref_layers.active_bands()
        silent = (not eq.profile_has_content(body)
                  and not self.pref_layers.active_has_content())
        if self.bypass_row.get_active() or silent:
            pipewire._in_thread(lambda: pipewire.metadata_clear(node))
        else:
            graph = eq.profile_graph(body, extra=extra)
            pipewire._in_thread(lambda: pipewire.metadata_set(node,
                                                              graph))

    # ---- undo / redo -------------------------------------------------------
    def _snapshot(self):
        """Serialize editor state for undo (the viewed channel is left out)."""
        keys = ["all"] + list(self.ch_keys)
        return {"pid": self.current_pid,
                "apply_all": self.apply_all,
                "preamp": float(self.preamp),
                "preamp_auto": bool(self.preamp_auto),
                "ch_keys": list(self.ch_keys),
                "slots": {k: self._slot_to_dict(k) for k in keys},
                "taste": {
                    "active": self.pref_layers.active_id,
                    "layers": [
                        {"id": l["id"], "name": l["name"],
                         "bands": [dict(x) for x in
                                   (l.get("bands") or [])]}
                        for l in self.pref_layers.layers]}}

    def _restore(self, snap):
        """Load an undo snapshot back into the editor. Snapshots
        carry the profile id: restoring one made under another
        profile switches to it without reseeding the timeline."""
        pid = snap.get("pid")
        if pid and pid != self.current_pid:
            if not self.store.has(pid):
                body = self._graveyard.get(pid)
                if body is None:
                    return           # truly gone; the walk skips these
                # undo rolls the deletion back: resurrect the body,
                # then the snapshot state lands on top of it
                self.store.save_user(json.loads(json.dumps(body)))
            self.current_pid = pid
            self.profile_button.set_label(
                self._display_name(self.store.get(pid)))
            if self.live and self.node:
                # rewind the sink binding too, or a restart would
                # resurrect the selection undo just unwound
                self.store.set_binding(self.node, pid)
            self._populate_picker()
        view = self.cur_ch          # keep the user's current tab if still valid
        self._loading = True
        try:
            self.apply_all = bool(snap["apply_all"])
            self.preamp = float(snap.get("preamp", 0.0))
            self.preamp_auto = bool(snap.get("preamp_auto", True))
            self._auto_syncing = True
            try:
                self.auto_button.set_active(self.preamp_auto)
            finally:
                self._auto_syncing = False
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
            self.sep_switch.set_active(not self.apply_all)
            self._build_channel_bar()
            self._load_slot(self.cur_ch)
        finally:
            self._loading = False
        t = snap.get("taste")
        if t is not None:
            self.pref_layers.restore(t.get("layers"),
                                     t.get("active"))
            self._sync_taste_card()
        if self._editable(self.current_pid):
            self.store.save_user(self._working_body())
        self._apply_now()
        self._update_headroom()
        self._canvas_refresh()

    def _push_history(self, sel=False, born=None):
        """Append a snapshot, dropping any redo tail (cap
        _HISTORY_CAP). sel=True marks a selection baseline (a state
        between edits, never an undo step of its own); born=pid
        marks a profile birth: undo, leaving that entry, buries the
        profile. A real push first materializes a deferred
        mid-history switch, so the edit's pre-state is on record.
        Dedup compares content without the marks."""
        snap = self._snapshot()
        if sel:
            snap["sel"] = True
        if born:
            snap["born"] = born
        if self._hidx < len(self._hist) - 1:        # drop the redo branch
            del self._hist[self._hidx + 1:]

        def _bare(d):
            return {k: v for k, v in d.items()
                    if k not in ("sel", "born")}
        pend = self._pending_sel
        self._pending_sel = None
        if pend is not None and not sel:
            pend = dict(pend)
            pend["sel"] = True
            if _bare(self._hist[self._hidx]) != _bare(pend):
                self._hist.append(pend)
                self._hidx = len(self._hist) - 1
        if not self._hist or _bare(self._hist[self._hidx]) \
                != _bare(snap):
            self._hist.append(snap)
            self._hidx = len(self._hist) - 1
            if len(self._hist) > _HISTORY_CAP:
                self._hist.pop(0)
                self._hidx -= 1
        self._update_undo_buttons()

    def _snap_alive(self, snap):
        pid = snap.get("pid")
        return (not pid or self.store.has(pid)
                or pid in self._graveyard)

    def _undo(self, *_):
        """ONE step back: restore the previous live entry, sel or
        not -- undoing an edit lands its pre-state and STAYS on
        that profile; only dead entries are skipped. A deferred
        switch is abandoned: undo navigates the recorded branch."""
        if self._hidx <= 0:
            return
        self._pending_sel = None
        self._restoring = True
        try:
            while self._hidx > 0:
                leaving = self._hist[self._hidx]
                bpid = leaving.get("born")
                if bpid and self.store.has(bpid):
                    # undoing a birth: bury it (redo resurrects)
                    self._graveyard[bpid] = json.loads(
                        json.dumps(self.store.get(bpid)))
                    self.store.delete_user(bpid)
                    self.favorites.discard(bpid)
                    _save_favorites(self.favorites)
                    self._populate_picker()
                self._hidx -= 1
                snap = self._hist[self._hidx]
                if not self._snap_alive(snap):
                    continue
                self._restore(snap)
                break
        finally:
            self._restoring = False
        self._update_undo_buttons()

    def _redo(self, *_):
        """ONE step forward, skipping only dead entries."""
        if self._hidx >= len(self._hist) - 1:
            return
        self._pending_sel = None
        self._restoring = True
        try:
            while self._hidx < len(self._hist) - 1:
                self._hidx += 1
                snap = self._hist[self._hidx]
                if not self._snap_alive(snap):
                    continue
                self._restore(snap)
                break
        finally:
            self._restoring = False
        self._update_undo_buttons()

    def _update_undo_buttons(self):
        """Selections and dead entries never light the arrows: undo
        is offered only when a real edit sits at or below the
        current position (the seed at index 0 is a baseline, not a
        step), redo -- when one sits ahead."""
        def _step(rng):
            return any(not self._hist[i].get("sel")
                       and self._snap_alive(self._hist[i])
                       for i in rng)
        self.undo_btn.set_sensitive(
            _step(range(1, self._hidx + 1)))
        self.redo_btn.set_sensitive(
            _step(range(self._hidx + 1, len(self._hist))))

    # ---- band table --------------------------------------------------------
    def _on_preamp(self, spin):
        """Preamp spin changed: one shared value for the whole
        profile. A HAND turn is a compromise -- it un-presses
        Auto."""
        self.preamp = float(spin.get_value())
        self.view.set_preamp(self.preamp)
        if self._loading or self._preamp_syncing:
            return
        self._clamped_note = None
        self._set_preamp_auto(False)
        self._on_edit()

    def _set_preamp_auto(self, on, land=False):
        """Flip the follow mode and sync the toggle without echo.
        The mode is profile state: it persists and rides undo."""
        on = bool(on)
        changed = on != self.preamp_auto
        self.preamp_auto = on
        self._auto_syncing = True
        try:
            self.auto_button.set_active(on)
        finally:
            self._auto_syncing = False
        if on and land:
            self._land_safe()
        if changed and not self._loading and not self._restoring:
            self._dev_dirty = True
            self._schedule_save()

    def _on_auto_toggled(self, btn):
        if self._auto_syncing or self._loading:
            return
        self._clamped_note = None
        self._set_preamp_auto(btn.get_active(), land=True)
        # landing rides the guarded path (no _on_edit echo), so the
        # readout -- the warning icon and the Over/Headroom line --
        # must be refreshed HERE; without playback no meter frame
        # would ever do it, and the stale warning outlives Safe
        self._update_headroom()

    def _land_safe(self):
        """Impose the Safe value: -(max of the summed EQ curve),
        dueling boosts and cuts cancelling in the sum; ONE shared
        value for all channels (the worst channel's requirement),
        so the balance encoded by the curves survives."""
        t = self._auto_preamp_db()
        v = -t if t else 0.0
        if abs(v - self.preamp) < 0.05:
            return
        self._preamp_syncing = True
        try:
            self.preamp_spin.set_value(v)
        finally:
            self._preamp_syncing = False
        self._dev_dirty = True
        self._apply_now()
        self._schedule_save()

    def _clamp_to_session(self):
        """A latched over-0 peak pulls the preamp down -- silently
        but attributed in the subtitle: the compromise keeps only
        as much loudness as the session proved safe. Never below
        Safe: a broadband transient can exceed the sine bound, and
        a compromise louder than safety is nonsense."""
        c = self._sess_c()
        if c is None or c <= self._CLIP_EPS_DB:
            return
        t = self._auto_preamp_db()
        v = max(-t if t else 0.0, self.preamp - c)
        if v >= self.preamp - 1e-9:
            return
        self._preamp_syncing = True
        try:
            self.preamp_spin.set_value(v)
        finally:
            self._preamp_syncing = False
        self._sess_peak = None          # consumed, like Session did
        self._clamped_note = "pulled to session %.1f" % v
        self._dev_dirty = True
        self._apply_now()               # clipping is live: right now
        self._schedule_save()

    def _auto_preamp_db(self):
        """Preamp that zeroes the tier-1 estimate: the max of the edited
        chain's band curve (no preamp) -- or, with unlinked channels, of
        the WORST channel's curve, so one shared value clears every slot.
        Rounded UP to the 0.1 dB step the spin can express, so the result
        lands at or below 0 dBFS."""
        tail = [eq.Band.from_dict(b)
                for b in self.pref_layers.active_bands()]
        if self.apply_all:
            peak = eq.curve_max_db(0.0, self.bands + tail)
        else:
            peak = max(eq.curve_max_db(0.0,
                                       self._slot(k)["bands"] + tail)
                       for k in self.ch_keys)
        return max(0.0, math.ceil(peak * 10.0 - 1e-9) / 10.0)

    def _on_bypass(self, *_):
        """Bypass toggled: republish the device state."""
        self.view.set_active(not self.bypass_row.get_active())
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
        row in the closing card; each lamp's tooltip carries that
        channel's clipped-sample percentage -- the same metric
        audit_headroom prints. In Bypass
        tier 1 has nothing to warn about -- the profile adds no gain and the
        input level is not measured yet -- so everything goes back to
        neutral (the tier-2 meter will keep flagging hot inputs here)."""
        if self.bypass_row.get_active():
            self.clip_icon.set_visible(False)
            self.preamp_sub.set_text(self._preamp_subtitle)
            self.preamp_sub.set_tooltip_text(None)
            return
        if (self.preamp_auto and not self._loading
                and not self._restoring):
            self._land_safe()       # AUTO: the preamp follows Safe
        tail = [eq.Band.from_dict(b)
                for b in self.pref_layers.active_bands()]
        bounds = {k: eq.headroom_bound_db(self.preamp,
                                          s["bands"] + tail)
                  for k, s in self._applied_chains()}
        bound, key = max(((v, k) for k, v in bounds.items()),
                         key=lambda t: t[0])
        over = bound > self._CLIP_EPS_DB
        shown = bound if abs(bound) >= self._CLIP_EPS_DB else 0.0  # no "-0.0"
        where = "" if key in ("all", self.cur_ch) else " on %s" % key
        offenders = [(k, v) for k, v in bounds.items()
                     if v > self._CLIP_EPS_DB]

        self.clip_icon.set_visible(over)
        t = self._auto_preamp_db()
        safe_txt = "Safe %.1f" % (-t if t else 0.0)
        if over:
            sub = ("Over 0 dBFS by %.1f dB%s · %s"
                   % (shown, where, safe_txt))
        else:
            sub = "Headroom %.1f dB%s · %s" % (0.0 - shown,
                                                    where, safe_txt)
        if self._clamped_note:
            sub = self._clamped_note + " · " + sub
        self.preamp_sub.set_text(sub)

        tip = None
        if over:
            tip = ("Estimated for content peaking at 0 dBFS: the profile "
                   "pushes it past full scale (the input side is not "
                   "measured yet). Lower the shared preamp; "
                   "press Auto to follow Safe.")
            if len(offenders) > 1:
                listed = ", ".join("%s %+.1f" % (k, v) for k, v in offenders)
                tip += "\nOver 0 dBFS: %s." % listed
        self.preamp_sub.set_tooltip_text(tip)
        self.clip_icon.set_tooltip_text(tip if over else None)


    # ---- live post-EQ meter (ROADMAP Task 2, tier 2) ------------------------
    def _sess_c(self):
        """The session peak on the 0.1 dB grid -- ONE formatter for the
        subtitle, the button and the applied value, so they always agree."""
        if self._sess_peak is None:
            return None
        return math.ceil(self._sess_peak * 10.0 - 1e-9) / 10.0

    def _meter_chains(self):
        """(preamp, per-input-channel band lists) the device actually runs:
        identity everywhere in Bypass -- the meter then shows the raw
        input, closing the hot-master-in-bypass blind spot."""
        if self.bypass_row.get_active():
            return 0.0, [[] for _ in self.ch_keys]
        tail = [eq.Band.from_dict(b)
                for b in self.pref_layers.active_bands()]
        if self.apply_all:
            b = self._slot("all")["bands"] + tail
            return self.preamp, [b for _ in self.ch_keys]
        return self.preamp, [self._slot(k)["bands"] + tail
                             for k in self.ch_keys]

    def _update_meter(self):
        """Single choke point (first line of _apply_now): keep the engine's
        chains fresh and its lifecycle matched to window visibility and the
        selected device. The capture holds the sink awake, so it runs only
        while the window is mapped."""
        want = bool(self.live and self.node and self.get_mapped()
                    and pipewire.meter_available())
        if want and self._meter is None:
            try:
                from .meter import Ballistics, MeterEngine  # lazy: scipy
            except ImportError:
                return
            self._Ballistics = Ballistics
            self._meter = MeterEngine(self._publish_meter)
        if self._meter is None:
            return
        restart = want and self._meter_node != self.node
        if restart:
            self._meter.stop()      # never swap a running worker's count
        pre, chains = self._meter_chains()
        self._meter.set_chains(pre, chains)
        if len(self._bal) != len(chains):
            self._bal = [self._Ballistics() for _ in chains]
            self._sess_samples = 0
        if restart:
            self._meter.start(self.node)
            self._meter_node = self.node
            self._dead_frames = []
            self._meter_relinks = 0
            # Empirically (BT sink, in-node graph): a capture stream comes
            # up with one monitor port unlinked, and what completes the
            # links is the WP hook's graph (re)apply -- a fresh stream does
            # NOT help. Nudge once after the capture starts.
            GLib.timeout_add(400, lambda: (self._apply_now(), False)[1])
        elif not want and self._meter_node is not None:
            self._meter.stop()
            self._meter_node = None
            self._live_db = None

    def _stop_meter_on_close(self, *_):
        if self._meter is not None:
            self._meter.stop()
        if self._pw_unsub is not None:
            self._pw_unsub()
            self._pw_unsub = None
        return False

    def _publish_meter(self, frame):        # called from the worker thread
        GLib.idle_add(self._on_meter_frame, frame)

    def _on_meter_frame(self, frame):
        """One aggregated engine frame (~30 Hz): ballistics, bars, the tab
        latch, and a throttled numeric readout in the Preamp row."""
        if not self._bal:
            return False
        if os.environ.get("PDE_METER_DEBUG"):
            print("meter: frame peaks=%s clips=%s bal=%d areas=%s"
                  % (["%.1f" % p for p in frame["peaks_db"]],
                     frame["clips"], len(self._bal),
                     list(self._meter_areas)), file=sys.stderr)
        # Capture-link watchdog: publishing the graph reconfigures the sink
        # at the same instant the capture links to its monitor, and
        # WirePlumber can leave a monitor port unlinked -- that channel then
        # reads EXACT digital zero forever while the others play. Real
        # linked audio never holds a true zero for a second, so restart the
        # capture (a fresh link) when we see it; cap the retries.
        pks = frame["peaks_db"]
        if len(self._dead_frames) != len(pks):
            self._dead_frames = [0] * len(pks)
        if max(pks) > -60.0:
            for i, p in enumerate(pks):
                self._dead_frames[i] = self._dead_frames[i] + 1 \
                    if p <= -139.0 else 0
            if (max(self._dead_frames) > 36 and self._meter_relinks < 3
                    and self._meter is not None and self._meter_node):
                self._meter_relinks += 1
                self._dead_frames = [0] * len(pks)
                if os.environ.get("PDE_METER_DEBUG"):
                    print("meter: dead channel, republishing graph to"
                          " relink (try %d)" % self._meter_relinks,
                          file=sys.stderr)
                self._apply_now()
                return False
        self._sess_samples += int(frame.get("samples", 0))
        st = []
        for i, (pk, cl) in enumerate(zip(frame["peaks_db"], frame["clips"])):
            if i >= len(self._bal):
                break
            bar, latched = self._bal[i].update(frame["t"], pk, cl)
            st.append((bar, latched, self._bal[i].clip_total))
        self._meter_state = st
        for i, area in self._meter_areas.items():
            area.queue_draw()
            lamp = self._meter_lamps.get(i)
            if lamp is None or i >= len(st):
                continue
            lamp.set_visible(bool(st[i][1]))
            if self._sess_samples:
                lamp.set_tooltip_text(
                    "%.3f%% clipped this session"
                    % (100.0 * st[i][2] / self._sess_samples))
        if not self.bypass_row.get_active():
            mx = max(frame["peaks_db"])
            if mx > self._CLIP_EPS_DB and (self._sess_peak is None
                                           or mx > self._sess_peak):
                self._sess_peak = mx
                self._clamp_to_session()
                self._update_headroom()
        return False

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
        box.set_margin_top(4); box.set_margin_bottom(4)
        box.set_margin_start(6); box.set_margin_end(6)

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
            edit.set_tooltip_text("Edit")
            edit.connect("clicked", self._make_edit_cb(p))
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
            # the session graveyard: undo entries made under this
            # profile can resurrect it, takes and all
            self._graveyard[pid] = json.loads(
                json.dumps(self.store.get(pid)))
            self.store.delete_user(pid)
            self.favorites.discard(pid)
            _save_favorites(self.favorites)
            if self.current_pid == pid:
                self._load_profile(CLEAN_ID)
            self._populate_picker()
        dlg.connect("response", on_resp)
        dlg.present(self)

    def _make_edit_cb(self, p):
        """Factory: open the measurement window on the row's
        profile."""
        def cb(_btn):
            self._edit_profile(p)
        return cb

    def _edit_profile(self, p):
        """The profile's Edit: the measurement window carries the
        name field, take appends and channel re-measures. It aims
        at the profile's OWN sink -- the current output is picked
        up only when creating a NEW profile. If the profile's sink
        is bound but not connected, a dialog offers the current
        output or cancels: the computer may be playing one device
        while another is being measured, and stealing is rude."""
        if not self.live or not self.node:
            return
        self.profile_popover.popdown()
        if self._measure_win is not None:
            self._measure_win.present()
            return
        homes = [n for n, x in self.store.bindings.items()
                 if x == p["id"]]
        here = {s["name"] for s in self.sinks}
        node = next((n for n in homes if n in here), None)
        if node is None and homes:
            dlg = Adw.AlertDialog(
                heading="Device not connected",
                body="\u201c%s\u201d belongs to %s, which is not "
                     "connected.\nMeasure on the current output "
                     "instead?" % (p.get("name") or "This profile",
                                   ", ".join(homes)))
            dlg.add_response("cancel", "Cancel")
            dlg.add_response("current", "Use current output")
            dlg.set_default_response("current")
            dlg.set_close_response("cancel")

            def done(_d, resp, pid=p["id"]):
                if resp == "current":
                    self._open_measure_for(self.node, pid)
            dlg.connect("response", done)
            dlg.present(self)
            return
        self._open_measure_for(node or self.node, p["id"])

    def _open_measure_for(self, node, pid):
        try:
            from .measure_window import MeasureWindow
        except Exception as e:
            dlg = Adw.AlertDialog(
                heading="Editing needs extra packages",
                body="The measurement window needs python3-numpy, "
                     "python3-scipy and python3-soundfile.\n\n%s" % e)
            dlg.add_response("ok", "OK")
            dlg.present(self)
            return
        desc = next((s["desc"] for s in self.sinks
                     if s["name"] == node), node)
        self._measure_win = MeasureWindow(self, node, desc,
                                          edit_pid=pid)
        self._measure_win.connect("close-request",
                                  self._on_measure_closed)
        self._measure_win.present()

    def _make_clone_cb(self, p):
        """Factory: duplicate the row's profile."""
        def cb(_btn):
            self._clone_profile(p)
        return cb

    def _clone_profile(self, p):
        """Deep-copy a profile into a new user profile and switch to it."""
        src = self.store.get(p["id"])
        # the store's canonical body carries the v3 blocks
        # (provenance, device, fit, measurement) verbatim -- a
        # copy of a measured profile stays a measured profile.
        # The json round-trip keeps the copy deep, sharing
        # nothing with the original's live record.
        body = json.loads(json.dumps(self.store._body(src)))
        body.pop("id", None)
        body["name"] = self._unique_name(
            self._display_name(p) + " copy")
        pid = self.store.save_user(body)
        self.favorites.add(pid)
        _save_favorites(self.favorites)
        self.profile_popover.popdown()
        self._load_profile(pid, born=True)   # a birth: undo removes it

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
    def _on_measure(self):
        """Open the measurement window for the current sink -- the
        New entry lands here. The window and its measurement math
        (numpy/scipy/soundfile) are optional weak deps, so a missing
        one degrades to a clear message, not a crash."""
        if not self.live or not self.node:
            return
        self.profile_popover.popdown()
        if self._measure_win is not None:
            self._measure_win.present()      # already open; bring it forward
            return
        try:
            from .measure_window import MeasureWindow
        except Exception as e:
            dlg = Adw.AlertDialog(
                heading="Measurement needs extra packages",
                body="The measurement wizard needs python3-numpy, "
                     "python3-scipy and python3-soundfile.\n\n%s" % e)
            dlg.add_response("ok", "OK")
            dlg.present(self)
            return
        desc = next((s["desc"] for s in self.sinks
                     if s["name"] == self.node), self.node)
        self._measure_win = MeasureWindow(self, self.node, desc)
        self._measure_win.connect("close-request", self._on_measure_closed)
        self._measure_win.present()

    def _on_measure_closed(self, win, *_):
        """Drop the reference when the measure window closes.
        Compare identity: during a retarget the old window closes
        after the new one is stored, and must not clear it. Also re-apply
        the current device's bound profile so the shown profile and the
        applied EQ match -- measuring or switching can leave the previous
        device's EQ live otherwise."""
        if win is self._measure_win:
            self._measure_win = None
            if self.live and self.node:
                self._load_profile(
                    self.store.binding_for(self.node) or CLEAN_ID)
        return False

    def _on_create_new(self, _btn):
        """New: the measurement window on a fresh profile. Measure
        into it and close for a fitted profile; close empty for an
        empty one -- the window creates it either way."""
        self._on_measure()

    def _import_rew(self):
        """Import a mono REW/AutoEQ text file into the CURRENT slot."""
        dialog = Gtk.FileDialog()
        dialog.set_title("Import EQ text")
        txt = Gtk.FileFilter()
        txt.set_name("Parametric EQ text (*.txt)")
        txt.add_pattern("*.txt")
        allf = Gtk.FileFilter()
        allf.set_name("All files")
        allf.add_pattern("*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(txt)
        filters.append(allf)
        dialog.set_filters(filters)
        dialog.set_default_filter(txt)

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
            if not bands:
                looks = False
                try:
                    j = json.loads(text)
                    looks = (isinstance(j, dict)
                             and "version" in j and "channels" in j)
                except Exception:
                    pass
                msg = ("This is a per-device-eq profile package. "
                       "Use Import profile in the profile menu; "
                       "this button replaces the bands of the "
                       "current channel from a parametric-EQ "
                       "text file."
                       if looks else
                       "No parametric-EQ bands found in the "
                       "file. Nothing was changed.")
                err = Adw.AlertDialog(heading="Cannot import",
                                      body=msg)
                err.add_response("ok", "OK")
                err.present(self)
                return
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
        """Import a .pdeq package (or an older raw-JSON export):
        validate, show the package's own claims for confirmation,
        then land it without destroying anything in the store."""
        self.profile_popover.popdown()
        dialog = Gtk.FileDialog()
        dialog.set_title("Import profile")
        flt = Gtk.FileFilter()
        flt.set_name("per-device-eq profiles (*.pdeq, *.json)")
        flt.add_pattern("*.pdeq")
        flt.add_pattern("*.json")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(flt)
        dialog.set_filters(filters)

        def done(d, res):
            try:
                gfile = d.open_finish(res)
            except GLib.Error:
                return
            path = gfile.get_path() if gfile else None
            if not path:
                return
            from . import pdeq
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                prof, sha = pdeq.pdeq_unpack(text)
            except (OSError, ValueError) as e:
                err = Adw.AlertDialog(heading="Import failed",
                                      body=str(e))
                err.add_response("ok", "OK")
                err.present(self)
                return
            taken = str(prof.get("name") or "") in {
                pp.get("name")
                for pp in self.store.profiles.values()}
            dlg = Adw.AlertDialog(
                heading="Import profile?",
                body="\n".join(pdeq.package_report(prof, sha)))
            dlg.add_response("cancel", "Cancel")
            dlg.add_response("import", "Import")
            dlg.set_response_appearance(
                "import", Adw.ResponseAppearance.SUGGESTED)
            dlg.set_default_response("import")

            def picked(_d, resp):
                if resp != "import":
                    return
                pid, lines = pdeq.absorb(self.store, text)
                if any("nothing imported" in ln for ln in lines):
                    info = Adw.AlertDialog(
                        heading="Already in the store",
                        body="An identical copy of this package "
                             "is already here.")
                    info.add_response("ok", "OK")
                    info.present(self)
                    self._load_profile(pid)
                    return
                rec = self.store.get(pid)
                if taken:
                    fresh = self._unique_name(
                        str(rec.get("name") or "Imported"))
                    self.store.save_user(dict(rec, name=fresh))
                self.favorites.add(pid)
                _save_favorites(self.favorites)
                self._load_profile(pid, born=True)
            dlg.connect("response", picked)
            dlg.present(self)
        dialog.open(self, None, done)

    @staticmethod
    def _safe_filename(name):
        """Filesystem-safe stem for export file names."""
        s = "".join(c if (c.isalnum() or c in " ._-") else "_" for c in name).strip()
        return s.replace(" ", "_") or "profile"

    # ---- FR graph ----------------------------------------------------------
    # ---- settings: preference EQ layers ---------------------------------
    def _taste_refresh(self):
        """After any layer change: re-apply, recompute the headroom
        hints and keep the card in step with the store."""
        self._apply_now()
        self._update_headroom()
        self._sync_taste_card()

    def _build_taste_popover(self):
        """The layer picker, patterned on the profile one: New in
        the header, per-row rename and delete, No layer on top."""
        self.taste_popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                      spacing=6)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + side)(6)
        head = Gtk.Box(spacing=6)
        title = Gtk.Label(label="Preference EQ layers", xalign=0.0)
        title.add_css_class("dim-label")
        title.add_css_class("caption")
        title.set_hexpand(True)
        new_btn = Gtk.Button.new_from_icon_name("list-add-symbolic")
        new_btn.add_css_class("flat")
        new_btn.set_tooltip_text("New layer")
        new_btn.connect("clicked", self._on_taste_new)
        head.append(title)
        head.append(new_btn)
        box.append(head)
        self.taste_list = Gtk.ListBox()
        self.taste_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.taste_list.add_css_class("navigation-sidebar")
        self.taste_list.connect("row-activated", self._on_taste_row)
        box.append(self.taste_list)
        self.taste_popover.set_child(box)
        return self.taste_popover

    def _taste_row_widget(self, lay):
        """One layer row: checkmark, editable name, pencil, X."""
        row = Gtk.ListBoxRow()
        row.lid = None if lay is None else lay["id"]
        h = Gtk.Box(spacing=6)
        chk = Gtk.Image.new_from_icon_name("object-select-symbolic")
        chk.set_opacity(
            1.0 if row.lid == self.pref_layers.active_id else 0.0)
        h.append(chk)
        if lay is None:
            name = Gtk.Label(label="No layer", xalign=0.0)
            name.set_hexpand(True)
            h.append(name)
        else:
            name = Gtk.EditableLabel()
            name.set_text(lay["name"])
            name.set_hexpand(True)
            name.set_valign(Gtk.Align.CENTER)
            name.connect("notify::editing",
                         self._make_taste_rename_cb(lay["id"], name))
            h.append(name)
            # EditableLabel starts editing on ANY click, and the
            # name spans most of the row -- a plain click kept
            # landing in rename instead of selection. The capture
            # gesture hands the click to selection; the pencil
            # starts editing programmatically, and while editing
            # the gesture stands aside.
            g = Gtk.GestureClick()
            g.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

            def _pick(gest, *_a, r=row, e=name):
                if e.get_editing():
                    return
                gest.set_state(Gtk.EventSequenceState.CLAIMED)
                self._on_taste_row(self.taste_list, r)
            g.connect("pressed", _pick)
            name.add_controller(g)
            pen = Gtk.Button.new_from_icon_name(
                "document-edit-symbolic")
            pen.add_css_class("flat")
            pen.set_valign(Gtk.Align.CENTER)
            pen.set_tooltip_text("Rename")
            pen.connect("clicked",
                        lambda *_a, e=name: e.start_editing())
            h.append(pen)
            x = Gtk.Button.new_from_icon_name(
                "window-close-symbolic")
            x.add_css_class("flat")
            x.set_valign(Gtk.Align.CENTER)
            x.set_tooltip_text("Delete")
            x.connect("clicked",
                      lambda *_a, l=lay: self._on_taste_delete(l))
            h.append(x)
        row.set_child(h)
        return row

    def _sync_taste_card(self):
        """The button label, the picker rows and the body all
        mirror the layer store."""
        lb = self.taste_list
        child = lb.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            lb.remove(child)
            child = nxt
        lb.append(self._taste_row_widget(None))
        for lay in self.pref_layers.layers:
            lb.append(self._taste_row_widget(lay))
        act = self.pref_layers.active()
        self.taste_button.set_label(act["name"] if act
                                    else "No layer")
        has = act is not None
        self.taste_hint.set_visible(not has)
        self.taste_view.set_visible(has)
        if has:
            self.taste_view.set_bands(act.get("bands") or [])

    def _make_taste_rename_cb(self, lid, editable):
        def cb(*_):
            if editable.get_editing():
                return               # commit happens on leave
            cur = self.pref_layers.get(lid)
            txt = editable.get_text().strip()
            if cur is None or not txt or txt == cur["name"]:
                return
            self.pref_layers.upsert(dict(cur, name=txt))
            self._sync_taste_card()
            self._schedule_save()    # renames join the timeline
        return cb

    def _on_taste_card_toggled(self, expanded):
        self._ui_state["taste_card"] = bool(expanded)
        _save_ui_state(self._ui_state)

    def _on_taste_row(self, _lb, row):
        self.taste_popover.popdown()
        self.pref_layers.set_active(getattr(row, "lid", None))
        self._taste_refresh()

    def _on_taste_new(self, *_):
        names = {l["name"] for l in self.pref_layers.layers}
        i = 1
        while ("Layer %d" % i) in names:
            i += 1
        lid = self.pref_layers.upsert({"name": "Layer %d" % i,
                                       "bands": []})
        self.pref_layers.set_active(lid)
        self._taste_refresh()
        self._schedule_save()        # creating a layer is an edit

    def _on_taste_delete(self, lay):
        self.taste_popover.popdown()    # popovers sit above dialogs
        dlg = Adw.AlertDialog(
            heading="Delete layer?",
            body="“%s” will be deleted. This cannot be "
                 "undone." % lay["name"])
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_response_appearance(
            "delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        def on_resp(_d, resp):
            if resp != "delete":
                return
            self.pref_layers.delete(lay["id"])
            self._taste_refresh()
            self._schedule_save()    # Ctrl+Z resurrects the layer
        dlg.connect("response", on_resp)
        dlg.present(self)

    def _on_taste_view_changed(self, bands, final):
        """Write-through: the active layer takes every edit; the
        session undo for taste returns as part of the GLOBAL
        history in the next patch."""
        act = self.pref_layers.active()
        if act is None:
            return
        self.pref_layers.upsert(dict(act, bands=bands))
        self._apply_now()
        self._schedule_save()        # one global-history entry per
        if final:                    # settled gesture
            self._update_headroom()

class EqApplication(Adw.Application):
    def __init__(self):
        """Single-instance Adw application wrapper."""
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.win = None

    def do_activate(self):
        """Present the (single) main window; on a portable first
        run, ask about the system integration before anything
        else."""
        if self.win is None:
            self.win = EqWindow(self)
            GLib.idle_add(self.win._ask_integration)
        self.win.present()


def launch_gui():
    """Entry point used by the launcher: run the Adw application."""
    try:                      # `kill -USR1 <pid>` dumps a live stack to stderr
        import faulthandler, signal
        faulthandler.register(signal.SIGUSR1)
    except Exception:
        pass
    return EqApplication().run(None)
