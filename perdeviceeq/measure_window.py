# -*- coding: utf-8 -*-
"""GTK4 / libadwaita measurement wizard window (ROADMAP increment 4).

A per-sink modal: pick the measurement mic and its per-channel cal above
a GNOME-"Test Speakers"-style ring, click a speaker to run one sweep on
that channel, watch the takes accumulate per channel below (mini FR
curve, SNR, clip flag) and delete the bad ones, and once each channel has
three clean takes build a profile and switch the main editor to it so the
result is heard at once.

This is a thin VIEW: all the real work is in tested, GTK-free modules --
measure_session (one sweep -> TakeOutcome, spread, discard, quality),
measure_build (finalize + fit_peq.fit_profiles + save + bind) and
measure_prefs (mic profiles + per-sink recall). A sweep blocks for
seconds, so take() runs on a worker thread and results are marshalled
back with GLib.idle_add, the same pattern meter.py uses for capture.
"""
import math
import os
import threading

import numpy as np

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, GLib, Gdk, Adw       # noqa: E402

from . import config, pipewire, measure_build       # noqa: E402
from . import measure_core as mc                    # noqa: E402
from . import measure_session as ms                 # noqa: E402
from . import measure_prefs                         # noqa: E402

RING = 280
SPEAKER = 56
CLEAN_TARGET = 3            # clean takes per channel before "all clean"

# Where each channel sits on the ring, as a compass angle from the front
# (0 = straight ahead, positive = clockwise toward the right), so a
# speaker is drawn where it physically belongs the way GNOME's speaker
# test lays them out, instead of being spread evenly in channel order.
# LFE has no direction; park it at the bottom. Screen angle = this - 90.
CHAN_ANGLE = {
    "FC": 0, "FL": -30, "FR": 30, "FLC": -15, "FRC": 15,
    "SL": -90, "SR": 90, "RL": -150, "RR": 150,
    "RC": 180, "LFE": 180,
}
FIT_BANDS = 12
FIT_FLO = 20.0
FMIN_PLOT, FMAX_PLOT = 20.0, 20000.0


SPEAKER_NAMES = {
    "FL": "Front Left", "FR": "Front Right", "FC": "Front Center",
    "LFE": "Subwoofer", "RL": "Rear Left", "RR": "Rear Right",
    "SL": "Side Left", "SR": "Side Right",
}


def _speaker_name(key):
    return SPEAKER_NAMES.get(key, key)


def _stride_idx(n, cap=240):
    """Indices for drawing at most ~cap points of an n-point curve.
    Resize-time redraws are Python-loop-bound, every DrawingArea in
    the window repaints on every frame of a drag, and a thumbnail
    cannot show 958 points anyway. The last point always rides."""
    if n <= cap:
        return range(n)
    step = max(1, n // cap)
    idx = list(range(0, n, step))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    return idx


def _log_x(freq, x0, w):
    """x pixel for a frequency on a log axis spanning FMIN..FMAX_PLOT."""
    lo, hi = math.log10(FMIN_PLOT), math.log10(FMAX_PLOT)
    f = min(max(float(freq), FMIN_PLOT), FMAX_PLOT)
    return x0 + (math.log10(f) - lo) / (hi - lo) * w


def _ui_path():
    """First existing measurement .ui path (ships in data/, or installed)."""
    for p in config.MEASURE_UI_FILE_CANDIDATES:
        if os.path.exists(p):
            return p
    raise FileNotFoundError(
        "measurement design not found; looked in:\n  "
        + "\n  ".join(config.MEASURE_UI_FILE_CANDIDATES))


_CSS_INSTALLED = False


def _ensure_css():
    """Install the ring's style classes once: the count bubble and its
    status colours, and the error outline. Named libadwaita colours so it
    tracks the theme; load path mirrors the main window's."""
    global _CSS_INSTALLED
    if _CSS_INSTALLED:
        return
    data = """
    .measure-count {
        background-color: alpha(@window_fg_color, 0.12);
        border-radius: 9999px;
        padding: 0 5px;
        margin-top: 1px;
    }
    .measure-count.done { background-color: @success_bg_color;
                          color: @success_fg_color; }
    .measure-count.warn { background-color: @warning_bg_color;
                          color: @warning_fg_color; }
    .measure-count.bad  { background-color: @error_bg_color;
                          color: @error_fg_color; }
    button.measure-error { box-shadow: inset 0 0 0 2px @error_color; }
    button.speaker-on { box-shadow: 0 0 0 2px @window_fg_color; }
    """
    css = Gtk.CssProvider()
    if hasattr(css, "load_from_string"):
        css.load_from_string(data)
    else:
        css.load_from_data(data.encode())
    disp = Gdk.Display.get_default()
    if disp is not None:
        Gtk.StyleContext.add_provider_for_display(
            disp, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _CSS_INSTALLED = True


class MeasureWindow(Adw.Window):
    """Measurement wizard for one output sink."""

    def __init__(self, parent, sink_node, sink_desc, edit_pid=None):
        super().__init__()
        self.parent = parent
        self.sink_node = sink_node
        self.sink_desc = sink_desc
        self.edit_pid = edit_pid            # editing this profile
        self.edit_prof = (parent.store.get(edit_pid)
                          if edit_pid else None)
        self.set_title("Edit profile" if edit_pid
                       else "Measure speakers")
        self.set_default_size(1100, 760)  # opens two-column
        self.set_size_request(480, 600)   # the narrow floor
        # NOT modal: GNOME attaches modal transients to the parent
        # and re-centers them on every frame of an interactive
        # resize, so the window fights the pointer. The incremental
        # model needs no modality anyway -- the profile is live and
        # every take is persisted the moment it lands. transient_for
        # stays for stacking.
        self.set_transient_for(parent)
        # The one hazard modality used to mask: closing the MAIN
        # window mid-session would kill this one without teardown,
        # leaving foreign streams muted and the bypass engaged.
        # Close ourselves first (cancelling a sweep if one is in the
        # air), then let the parent go.
        self._parent_close_id = parent.connect(
            "close-request", self._on_parent_close)
        # Client-side modality, the same trick Adw.Dialog plays
        # inside a window: the parent goes insensitive for the
        # session -- input blocked, header buttons included -- while
        # the compositor sees a plain window and leaves the resize
        # alone. Restored on any close.
        parent.set_sensitive(False)

        self.mic_store = measure_prefs.MicProfileStore()
        self.memory = measure_prefs.MeasureMemory()
        try:
            self.ch_keys = pipewire.sink_channels(sink_node) or ["FL", "FR"]
        except Exception:
            self.ch_keys = ["FL", "FR"]
        self.n_ch = len(self.ch_keys)

        self.sources = pipewire.list_sources()
        self.cal = {}               # mic capture-channel idx -> cal
        self.mic_ch = 2             # rig capture channels (1 or 2)
        self.mic_of = {}            # sink channel -> analyzed mic ch
        self.session = None         # created on first measure
        self._entered = False
        self._busy = False
        self._loud_ack = False
        self._canvas_ids = {}       # (ch, live rec.id) -> canvas id
        self._canvas_session = None  # one session entry per sitting
        self._rig_blocked = False   # profile belongs to another rig
        self._relevel_pending = False
        self._sink_gone = False
        self.fit_lo, self.fit_hi = FIT_FLO, FMAX_PLOT
        # each handle follows the statistics until dragged
        self._hi_auto = True
        self._lo_auto = True
        self._spread_driver = None      # LOO verdict, set on refresh
        try:                       # bundled action icons: needed when
            theme = Gtk.IconTheme.get_for_display(  # run from git; the
                Gdk.Display.get_default())          # RPM installs them
            theme.add_search_path(os.path.join(     # into hicolor
                os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__))), "data", "icons"))
        except Exception:
            pass
        self._page = None            # selected channel's page widgets
        self._selected_ch = 0        # channel the ring has selected
        self._speakers = {}         # ch index -> Gtk.Button
        self._speaker_counts = {}   # ch index -> Gtk.Label (# takes)

        self._pw = pipewire.app_state()   # needed by _sink_present below
        self._pw_unsub = None
        self._build_ui()
        self._select_channel(0)
        self.connect("close-request", self._on_close)
        self._prefill_from_memory()
        self._ensure_session(arm=False, quiet=True)
        self._refresh_all()
        self._pw_unsub = self._pw.subscribe(self._on_pw_state)
        self._pw.start()

    # ---- layout -----------------------------------------------------------
    def _build_ui(self):
        _ensure_css()
        b = Gtk.Builder.new_from_file(_ui_path())
        self.set_content(b.get_object("content"))
        # ---- adaptive layout. Adw.MultiLayoutView owns both
        # arrangements declaratively (narrow single column / wide two
        # columns with a pinned left side and the single scroller on
        # the right); the breakpoint only names which one applies.
        # No reparenting in allocation callbacks, no scroller games:
        # the upstream-blessed pattern.
        mlv = b.get_object("mlv")
        bp = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("min-width: 940sp"))
        bp.add_setter(mlv, "layout-name", "wide")
        self.add_breakpoint(bp)

        b.get_object("window_title").set_subtitle(self.sink_desc)
        self.center = b.get_object("status")
        self.warning = b.get_object("warning")
        self.name_row = b.get_object("name_row")
        self.name_row.set_text(
            (self.edit_prof or {}).get("name") or self.sink_desc)

        self._build_mic_controls(b.get_object("mic_controls"),
                                 b.get_object("cal_controls"))

        self.map_left_slot = Gtk.Box()
        self.map_left_slot.set_valign(Gtk.Align.CENTER)
        self.map_right_slot = Gtk.Box()
        self.map_right_slot.set_valign(Gtk.Align.CENTER)
        ring_host = b.get_object("ring_host")
        ring_host.set_spacing(12)
        ring_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                           spacing=6)
        ring_col.append(self._build_ring())
        self.ready_hint = Gtk.Label(xalign=0.5)
        self.ready_hint.add_css_class("success")
        self.ready_hint.set_wrap(True)
        self.ready_hint.set_text(
            "Ready to fit -- close this window to hear your "
            "best version.")
        self.ready_hint.set_visible(False)
        ring_col.append(self.ready_hint)
        ring_host.append(self._vol_col)  # slider, auto-level, state
        ring_host.append(ring_col)
        self._rebuild_map_slots()

        b.get_object("channel_host").append(self._build_page())
        b.get_object("fit_host").append(self._build_fit_area())

    def _build_mic_controls(self, mic_controls, cal_controls):
        # the dropdown's widest label IS the window's minimum width;
        # long ALSA descriptions get an ellipsis, the full name lives
        # in the mic memory and the tooltips
        names = [(n if len(n) <= 34 else n[:33] + "\u2026")
                 for n in ([s["desc"] for s in self.sources]
                           or ["(no sources found)"])]
        self.source_dd = Gtk.DropDown.new_from_strings(names)
        self.source_dd.set_valign(Gtk.Align.CENTER)
        self.source_dd.connect("notify::selected", self._on_source_changed)
        self._tame_scroll(self.source_dd)
        self.source_dd.set_hexpand(True)
        mic_controls.append(self.source_dd)
        self.chan_dd = Gtk.DropDown.new_from_strings(["Mono", "Stereo"])
        self.chan_dd.set_valign(Gtk.Align.CENTER)
        self.chan_dd.set_tooltip_text("Capsules on the rig; a UMIK-1 is "
                                      "mono even if it enumerates as "
                                      "stereo")
        self.chan_dd.connect("notify::selected", self._on_chan_changed)
        self._tame_scroll(self.chan_dd)
        mic_controls.append(self.chan_dd)  # one row: mic + capsules
        self.cal_controls = cal_controls
        self._recompute_mic()
        self._rebuild_cal_row()

    def _build_ring(self):
        self.ring = Gtk.Fixed()
        self.ring.set_size_request(RING, RING)
        self.ring.set_halign(Gtk.Align.CENTER)
        disc = Gtk.DrawingArea()
        disc.set_content_width(RING)
        disc.set_content_height(RING)
        disc.set_draw_func(self._draw_disc)
        self._disc = disc
        self.ring.put(disc, 0, 0)

        cx = cy = RING / 2.0
        r = RING / 2.0 - SPEAKER / 2.0 - 6
        for i, key in enumerate(self.ch_keys):
            if self.n_ch == 2:                      # familiar L / R split
                ang = math.pi if i == 0 else 0.0
            elif key in CHAN_ANGLE:                 # its real position
                ang = math.radians(CHAN_ANGLE[key] - 90)
            else:                                   # unknown: spread it
                ang = math.pi + (2 * math.pi * i / max(1, self.n_ch))
            x = cx + r * math.cos(ang) - SPEAKER / 2.0
            y = cy + r * math.sin(ang) - SPEAKER / 2.0
            spk = Gtk.ToggleButton()
            spk.set_size_request(SPEAKER, SPEAKER)
            spk.add_css_class("circular")
            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            body.set_valign(Gtk.Align.CENTER)
            body.append(Gtk.Image.new_from_icon_name(
                "audio-volume-high-symbolic"))
            count = Gtk.Label()
            count.add_css_class("caption")
            count.add_css_class("measure-count")
            count.set_visible(False)
            body.append(count)
            spk.set_child(body)
            spk.connect("clicked", self._make_speaker_cb(i))
            spk.set_tooltip_text(_speaker_name(key))
            self.ring.put(spk, int(x), int(y))
            self._speakers[i] = spk
            self._speaker_counts[i] = count

        center_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                             spacing=6)
        center_box.set_size_request(RING - 2 * SPEAKER, -1)
        center_box.set_halign(Gtk.Align.CENTER)
        # The mics live INSIDE the ring now, where the volume used
        # to sit: capsule-to-speaker mapping is spatial information,
        # so it belongs in the spatial widget.
        map_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                          spacing=10)
        map_row.set_halign(Gtk.Align.CENTER)
        map_row.append(self.map_left_slot)
        map_row.append(self.map_right_slot)
        center_box.append(map_row)

        pult = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        pult.set_halign(Gtk.Align.CENTER)
        self.play_btn = self._pult_btn(
            "media-playback-start-symbolic",
            "Measure the selected channel", self._on_play)
        self.stop_btn = self._pult_btn(
            "media-playback-stop-symbolic", "Stop the sweep", self._on_stop)
        self.stop_btn.set_sensitive(False)
        pult.append(self.play_btn)
        pult.append(self.stop_btn)
        center_box.append(pult)
        self.ring.put(center_box, SPEAKER, int(RING / 2 - 56))
        self._center_box = center_box
        # The gone note lives in the disc's EMPTY top arc, so its
        # appearance moves nothing (Gtk.Fixed never reflows) and the
        # controls stay put, merely insensitive.
        self.ring_gone = Gtk.Label()
        self.ring_gone.set_markup(
            "<span size='large'>Unavailable</span>")
        self.ring_gone.add_css_class("error")
        self.ring_gone.set_halign(Gtk.Align.CENTER)
        self.ring_gone.set_hexpand(True)
        gone_holder = Gtk.Box()
        gone_holder.set_size_request(RING, -1)
        gone_holder.append(self.ring_gone)
        gone_holder.set_visible(False)
        self._gone_holder = gone_holder
        self.ring.put(gone_holder, 0, 26)

        # The volume is a fader now, on the ring's left; auto-level
        # sits under it -- the two speak the same language, and the
        # ring keeps only play and stop.
        adj = Gtk.Adjustment(lower=0, upper=100, step_increment=1,
                             page_increment=5)
        self.vol_spin = Gtk.Scale(
            orientation=Gtk.Orientation.VERTICAL, adjustment=adj)
        self.vol_spin.set_inverted(True)      # up is louder
        self.vol_spin.set_draw_value(True)
        self.vol_spin.set_value_pos(Gtk.PositionType.BOTTOM)
        self.vol_spin.set_digits(0)
        self.vol_spin.set_size_request(-1, RING - 72)
        self.vol_spin.set_tooltip_text(
            "Sweep playback level (%). Auto-level sets it; drag to "
            "override if it misses.")
        self.vol_spin.connect("value-changed", self._on_vol_edited)
        self._tame_scroll(self.vol_spin)
        self.relevel_btn = self._pult_btn(
            "pde-level-symbolic",
            "Measure the playback level now (probe sweeps only)",
            self._on_relevel)
        self.relevel_btn.set_halign(Gtk.Align.CENTER)
        self._vol_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                spacing=6)
        self._vol_col.set_valign(Gtk.Align.CENTER)
        self._vol_col.append(self.vol_spin)
        self._vol_col.append(self.relevel_btn)
        return self.ring

    def _pult_btn(self, icon, tip, cb):
        b = Gtk.Button()
        b.add_css_class("flat")
        b.add_css_class("circular")
        b.set_valign(Gtk.Align.CENTER)
        b.set_child(Gtk.Image.new_from_icon_name(icon))
        b.set_tooltip_text(tip)
        b.connect("clicked", cb)
        return b

    def _build_page(self):
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        header = Gtk.Label(xalign=0.0)
        col.append(header)
        summary = Gtk.DrawingArea()
        summary.set_content_height(120)
        summary.set_visible(False)
        summary.set_hexpand(True)
        # The summary IS the accordion's face: the card's first row
        # shows the channel's result with a chevron on the right --
        # the Adw look, not Gtk.Expander's corner triangle -- and a
        # click on it folds the take rows underneath.
        lb = Gtk.ListBox()
        lb.add_css_class("boxed-list")
        lb.set_selection_mode(Gtk.SelectionMode.NONE)
        face = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                       spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(face, "set_margin_" + side)(8)
        face.append(summary)
        chev = Gtk.Image.new_from_icon_name("pan-up-symbolic")
        chev.set_valign(Gtk.Align.CENTER)
        face.append(chev)
        face_row = Gtk.ListBoxRow()
        face_row.set_child(face)
        lb.append(face_row)
        lb.connect("row-activated", self._on_takes_face)
        col.append(lb)
        self._takes_open = True
        self._page = {"header": header, "summary": summary,
                      "takes_list": lb, "face_row": face_row,
                      "chevron": chev, "take_rows": []}
        return col

    def _on_takes_face(self, _lb, row):
        if row is not self._page["face_row"]:
            return
        self._takes_open = not self._takes_open
        self._page["chevron"].set_from_icon_name(
            "pan-up-symbolic" if self._takes_open
            else "pan-down-symbolic")
        for r in self._page["take_rows"]:
            r.set_visible(self._takes_open)

    def _make_curve_draw(self, rec, lo, hi, mean=None, shift=0.0):
        """The take's raw curve; where the (gain-compensated) take
        strays from the channel mean past the trust threshold the
        segment is painted red -- a lone bad take lights up alone, a
        collective scatter lights the same region on EVERY row, and
        "which take shrank the range" reads off the list."""
        freqs = rec.freq_hz
        mag = rec.mag_db

        def draw(_area, cr, w, h, *_):
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.10)
            cr.rectangle(0, 0, w, h)
            cr.fill()
            span = max(1e-6, hi - lo)

            def xy(j):
                x = _log_x(freqs[j], 2, w - 4)
                y = h - 3 - (float(mag[j]) - lo) / span * (h - 6)
                return x, max(1, min(h - 1, y))

            idx = _stride_idx(len(freqs))
            cr.set_source_rgb(0.22, 0.52, 0.90)
            cr.set_line_width(1.4)
            first = True
            for j in idx:
                x, y = xy(j)
                cr.move_to(x, y) if first else cr.line_to(x, y)
                first = False
            cr.stroke()
            if mean is None:
                return
            cr.set_source_rgb(0.87, 0.23, 0.23)
            cr.set_line_width(1.8)
            pen = False
            for j in idx:
                bad = abs(float(mag[j]) + shift
                          - float(mean[j])) > ms.SPREAD_MAX_DB
                if bad:
                    x, y = xy(j)
                    cr.move_to(x, y) if not pen else cr.line_to(x, y)
                    pen = True
                elif pen:
                    cr.stroke()
                    pen = False
            if pen:
                cr.stroke()
        return draw

    def _make_take_row(self, ch, rec, lo, hi, driver=None,
                       mean=None, shift=0.0):
        q = ms.take_quality(rec)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        body.set_margin_top(6)
        body.set_margin_bottom(6)
        body.set_margin_start(12)
        body.set_margin_end(12)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        dot = Gtk.Label(label="\u25cf")
        dot.add_css_class({ms.TAKE_CLEAN: "success",
                           ms.TAKE_FLAGGED: "warning",
                           ms.TAKE_CLIPPED: "error"}.get(q, "dim-label"))
        head.append(dot)
        if rec.clipped:
            info = "clipped  %.1f dBFS" % rec.peak_dbfs
        else:
            snr = ("SNR %.1f dB" % rec.snr_db
                   if rec.snr_db is not None else "SNR n/a")
            info = "%s  %.1f dBFS" % (snr, rec.peak_dbfs)
            if rec.noise_dbfs is not None:
                info += "  noise %.0f" % rec.noise_dbfs
        if rec.wav_path is None and rec.created_utc:
            info = "%s  \u00b7  %s" % (str(rec.created_utc)[:10],
                                       info)
        drives = driver is not None and driver[0] == rec.id
        if drives:
            info += "  ·  spread driver"
        lbl = Gtk.Label(label=info, xalign=0.0, hexpand=True)
        lbl.add_css_class("warning" if drives else "dim-label")
        if drives:
            lbl.set_tooltip_text(
                "This take drives the spread: deleting it wins back "
                "%.1f octaves of trustworthy band. Reseat and "
                "remeasure." % driver[1])
        head.append(lbl)
        rm = Gtk.Button()
        rm.add_css_class("flat")
        rm.set_child(Gtk.Image.new_from_icon_name("user-trash-symbolic"))
        rm.set_tooltip_text("Delete this take")
        rm.connect("clicked", self._make_discard_cb(ch, rec.id))
        head.append(rm)
        body.append(head)

        curve = Gtk.DrawingArea()
        curve.set_content_width(150)
        curve.set_content_height(60)
        curve.set_draw_func(
            self._make_curve_draw(rec, lo, hi, mean, shift))
        body.append(curve)

        # wrap in an explicit row: add_row auto-wraps a bare widget in a
        # GtkListBoxRow, and then remove() cannot drop it, so rows pile up
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.set_child(body)
        return row

    def _build_fit_area(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        lbl = Gtk.Label(xalign=0.0)
        lbl.set_markup("<b>EQ range</b>  <span size='small'>red bars "
                       "are the take-to-take spread; the handles "
                       "follow it until you drag them (cautious while "
                       "takes are few, so they may sit inside the "
                       "red). Reseat between takes or the spread "
                       "flatters the seating.</span>")
        lbl.set_wrap(True)
        box.append(lbl)
        self.range_area = Gtk.DrawingArea()
        self.range_area.set_content_height(90)
        self.range_area.set_hexpand(True)
        self.range_area.set_draw_func(self._draw_range)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._range_drag_begin)
        drag.connect("drag-update", self._range_drag_update)
        self.range_area.add_controller(drag)
        box.append(self.range_area)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.range_label = Gtk.Label(xalign=0.0)
        self.range_label.add_css_class("dim-label")
        self.range_label.set_hexpand(True)
        row.append(self.range_label)
        row.append(Gtk.Label(label="Bands"))
        self.bands_spin = Gtk.SpinButton.new_with_range(1, 20, 1)
        self._tame_scroll(self.bands_spin)
        self.bands_spin.set_value(FIT_BANDS)
        self.bands_spin.set_tooltip_text("Max biquads per channel; the fit "
                                         "stops early once the worst "
                                         "residual is under ~0.5 dB")
        row.append(self.bands_spin)
        box.append(row)
        self._range_plot = None
        self._drag_handle = None
        self._update_range_label()
        return box

    def _freq_to_x(self, f):
        if not self._range_plot:
            return 0
        ml, _mt, pw_, _ph = self._range_plot
        lo, hi = math.log10(FMIN_PLOT), math.log10(FMAX_PLOT)
        f = min(max(float(f), FMIN_PLOT), FMAX_PLOT)
        return ml + (math.log10(f) - lo) / (hi - lo) * pw_

    def _x_to_freq(self, x):
        ml, _mt, pw_, _ph = self._range_plot
        lo, hi = math.log10(FMIN_PLOT), math.log10(FMAX_PLOT)
        frac = min(1.0, max(0.0, (x - ml) / max(1, pw_)))
        return 10 ** (lo + frac * (hi - lo))

    def _max_spread(self):
        if self.session is None:
            return None, None
        freqs, best = None, None
        for i in range(self.n_ch):
            sp = self.session.spread_db(i)
            if sp is None:
                continue
            vals = [float(x) for x in sp]
            freqs = list(self.session.takes_of(i)[0].freq_hz)
            best = vals if best is None else [max(a, b)
                                              for a, b in zip(best, vals)]
        return best, freqs

    def _draw_range(self, _area, cr, w, h, *_):
        ml, mr, mt, mb = 6, 6, 6, 16
        pw_ = max(1, w - ml - mr)
        ph = max(1, h - mt - mb)
        self._range_plot = (ml, mt, pw_, ph)
        lo, hi = math.log10(FMIN_PLOT), math.log10(FMAX_PLOT)
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.10)
        cr.rectangle(ml, mt, pw_, ph)
        cr.fill()
        spread, freqs = self._max_spread()
        if spread and freqs:
            top = max(ms.SPREAD_MAX_DB, max(spread))
            idx = _stride_idx(len(freqs))
            bw = max(1.0, pw_ / max(1, len(idx)))
            for j in idx:
                gx = self._freq_to_x(freqs[j])
                if spread[j] >= ms.SPREAD_MAX_DB:
                    cr.set_source_rgba(0.87, 0.19, 0.19, 0.85)
                else:
                    cr.set_source_rgba(0.5, 0.5, 0.5, 0.5)
                bar = min(1.0, spread[j] / top) * ph
                cr.rectangle(gx, mt + ph - bar, bw, bar)
                cr.fill()
        xlo = self._freq_to_x(self.fit_lo)
        xhi = self._freq_to_x(self.fit_hi)
        cr.set_source_rgba(0.22, 0.52, 0.90, 0.18)
        cr.rectangle(xlo, mt, max(1, xhi - xlo), ph)
        cr.fill()
        for hx in (xlo, xhi):
            cr.set_source_rgb(0.22, 0.52, 0.90)
            cr.set_line_width(2)
            cr.move_to(hx, mt)
            cr.line_to(hx, mt + ph)
            cr.stroke()
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.8)
        cr.set_font_size(10)
        for fhz, txt in ((100, "100"), (1000, "1k"), (10000, "10k")):
            gx = ml + (math.log10(fhz) - lo) / (hi - lo) * pw_
            cr.move_to(gx + 2, h - 4)
            cr.show_text(txt)

    def _range_drag_begin(self, _g, sx, _sy):
        self._drag_handle = None
        if not self._range_plot:
            return
        xlo = self._freq_to_x(self.fit_lo)
        xhi = self._freq_to_x(self.fit_hi)
        self._drag_handle = "lo" if abs(sx - xlo) <= abs(sx - xhi) else "hi"

    def _range_drag_update(self, g, ox, _oy):
        if self._drag_handle is None or not self._range_plot:
            return
        ok, sx, _sy = g.get_start_point()
        if not ok:
            return
        f = self._x_to_freq(sx + ox)
        if self._drag_handle == "lo":
            self.fit_lo = max(FMIN_PLOT, min(f, self.fit_hi - 1))
            self._lo_auto = False           # the user took the handle
        else:
            self.fit_hi = min(FMAX_PLOT, max(f, self.fit_lo + 1))
            self._hi_auto = False           # the user took the handle
        self.range_area.queue_draw()
        self._update_range_label()

    def _update_range_label(self):
        lo_a = getattr(self, "_lo_auto", True)
        hi_a = getattr(self, "_hi_auto", True)
        auto = (" · auto" if lo_a and hi_a else
                " · auto hi" if hi_a else
                " · auto lo" if lo_a else "")
        self.range_label.set_text(
            "Fit %d – %d Hz%s"
            % (round(self.fit_lo), round(self.fit_hi), auto))

    def _auto_fit_range(self):
        """Park each handle at its edge of trust after every change
        to the takes: the full sweep while there are no statistics,
        the start of the red otherwise -- the ceiling from the top,
        the floor from the bottom (a leaky-seal bass is a blind zone
        exactly like an HF cliff). A manual drag of a handle
        disengages that handle's automation for the rest of the
        window; the other keeps following."""
        if self.session is None:
            return
        hi, lo = self.fit_hi, self.fit_lo
        if self._hi_auto:
            ceil = self.session.trusted_ceiling_hz()
            hi = FMAX_PLOT if ceil is None else min(FMAX_PLOT, ceil)
        if self._lo_auto:
            floor = self.session.trusted_floor_hz()
            lo = FIT_FLO if floor is None else max(FIT_FLO, floor)
        lo = min(lo, hi / 2.0)              # keep at least an octave
        if abs(hi - self.fit_hi) >= 1.0:
            self.fit_hi = hi
        if abs(lo - self.fit_lo) >= 0.1:
            self.fit_lo = lo
        self._update_range_label()

    # ---- drawing ----------------------------------------------------------
    def _draw_disc(self, _area, cr, w, h, *_):
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.16)
        cr.arc(w / 2.0, h / 2.0, min(w, h) / 2.0 - 1, 0, 2 * math.pi)
        cr.fill()
        if self._sink_gone:              # attention: the device left
            cr.set_source_rgb(0.87, 0.19, 0.19)
            cr.set_line_width(3)
            cr.arc(w / 2.0, h / 2.0, min(w, h) / 2.0 - 2,
                   0, 2 * math.pi)
            cr.stroke()

    # ---- prefill / refresh ------------------------------------------------
    def _prefill_from_memory(self):
        pid = self.memory.mic_for(self.sink_node)
        prof = self.mic_store.get(pid) if pid else None
        idx = None
        if prof and prof.get("node_match"):
            idx = next((k for k, s in enumerate(self.sources)
                        if s["name"] == prof["node_match"]), None)
        if idx is not None:
            self.source_dd.set_selected(idx)
        if prof:
            for i in range(self.mic_ch):
                path = prof.get("cal", {}).get(str(i))
                if path:
                    self.cal[i] = path
        self._sync_cal_labels()

    def _sync_cal_labels(self):
        labels = self._mic_labels()
        for i in range(self.mic_ch):
            path = self.cal.get(i)
            self.cal_btns[i].set_label(
                os.path.basename(path) if path else "%s cal…" % labels[i])

    def _selected_source(self):
        i = self.source_dd.get_selected()
        if 0 <= i < len(self.sources):
            return self.sources[i]
        return None

    def _source_name(self):
        s = self._selected_source()
        return s["name"] if s else None

    def _query_volume(self):
        try:
            dump = pipewire.pw_dump()
            sid = next((s["id"] for s in pipewire.list_sinks(dump)
                        if s["name"] == self.sink_node), None)
            if sid is None:
                return None
            v, _, _ = ms.sink_volume_state(dump, sid)
            return v
        except Exception:
            return None

    def _set_volume_display(self, v):
        if self._sink_gone or v is None:
            return
        self.vol_spin.handler_block_by_func(self._on_vol_edited)
        self.vol_spin.set_value(round(100 * v))
        self.vol_spin.handler_unblock_by_func(self._on_vol_edited)

    def _on_vol_edited(self, _spin):
        """Manual override of the sweep level -- the stop-crane when
        auto-level misses. Set it on the session and stop auto-levelling
        so it sticks for the next sweep. Before a session it cancels a
        pending relevel: the hand on the knob wins."""
        if self.session is not None:
            self.session.set_level(self.vol_spin.get_value() / 100.0)
        else:
            self._relevel_pending = False
        self._refresh_volume()

    def _refresh_volume(self):
        """The spin always shows the last established sweep level --
        the session's current, the remembered one, or the level the
        hunt will start from. Never the sink's LISTENING volume: that
        fallback once invented a number nobody asked for."""
        if self.session is not None:
            v = getattr(self.session, "_v_cur", None)
        else:
            src = self._source_name()
            v = (self.memory.volume_for(self.sink_node, src)
                 if src else None)
            if v is None:
                v = ms.AUTO_START_VOLUME
        if v is not None:
            self._set_volume_display(v)

    def _on_relevel(self, _btn):
        """Measure the level here and now: forget the remembered
        value, re-arm the leveling and run probe sweeps immediately on
        the selected channel. The spin follows the hunt live and ends
        at the found level -- the number is always the last one
        established, no captions needed. The locking sweep is
        discarded (this button measures the LEVEL, not a take)."""
        if self._busy:
            return
        src = self._source_name()
        if src:
            self.memory.forget_volume(self.sink_node, src)
        if self.session is not None:
            try:
                self.session.relevel()
            except Exception:
                pass
        self._relevel_pending = True
        self._refresh_all()
        self._start_measure(self._selected_ch, level_only=True)

    def _clean_count(self, ch):
        if self.session is None:
            return 0
        return sum(1 for r in self.session.takes_of(ch)
                   if ms.take_quality(r) == ms.TAKE_CLEAN)

    def _refresh_all(self):
        ready = self.session is not None
        for i in range(self.n_ch):
            n = self._clean_count(i)
            if n < CLEAN_TARGET:
                ready = False
            spk = self._speakers[i]
            spk.remove_css_class("suggested-action")   # legacy
            status = self._channel_status(i)
            if status == "bad":
                spk.add_css_class("measure-error")
            else:
                spk.remove_css_class("measure-error")
            total = len(self.session.takes_of(i)) if self.session else 0
            lbl = self._speaker_counts.get(i)
            if lbl is not None:
                lbl.set_text(str(total))
                lbl.set_visible(total > 0)
                for cls in ("done", "warn", "bad"):
                    lbl.remove_css_class(cls)
                if status:
                    lbl.add_css_class(status)
        self._spread_driver = (self.session.spread_driver()
                               if self.session else None)
        self._rebuild_page()
        self._update_pult()
        show = (bool(ready) and not self._busy
                and self.edit_pid is not None
                and self._should_autofit(self.edit_pid))
        self.ready_hint.set_visible(show)
        self._refresh_volume()
        self._auto_fit_range()
        if getattr(self, "range_area", None) is not None:
            self.range_area.queue_draw()

    def _channel_status(self, ch):
        """Ring status for a channel: 'done' (enough clean takes), 'bad'
        (a clipped take), 'warn' (takes disagree, max spread past the
        threshold), or '' (neutral / still going)."""
        if self.session is None:
            return ""
        takes = self.session.takes_of(ch)
        if not takes:
            return ""
        if self._clean_count(ch) >= CLEAN_TARGET:
            return "done"
        if any(ms.take_quality(r) == ms.TAKE_CLIPPED for r in takes):
            return "bad"
        spread = self.session.spread_db(ch)
        if spread is not None and len(spread) \
                and max(spread) >= ms.SPREAD_MAX_DB:
            return "warn"
        return ""

    def _rebuild_page(self):
        if self._page is None:
            return
        ch = self._selected_ch
        n = self._clean_count(ch)
        has_bad = self.session is not None and any(
            ms.take_quality(r) != ms.TAKE_CLEAN
            for r in self.session.takes_of(ch))
        mark = " \u2713" if n >= CLEAN_TARGET else ""
        warn = " \u26a0" if has_bad else ""
        self._page["header"].set_markup(
            "<b>%s</b>  <span size='small'>%d/%d clean%s</span>%s"
            % (self.ch_keys[ch], n, CLEAN_TARGET, mark, warn))
        lb = self._page["takes_list"]
        for row in self._page["take_rows"]:
            lb.remove(row)
        self._page["take_rows"].clear()
        takes = self.session.takes_of(ch) if self.session else []
        if takes:
            lo = min(float(min(r.mag_db)) for r in takes) - 1.0
            hi = max(float(max(r.mag_db)) for r in takes) + 1.0
        else:
            lo, hi = -1.0, 1.0
        mean, shifts = None, {}
        if self.session is not None and takes:
            mean, _sp = self.session.average_and_spread(ch)
            sh = self.session.comp_shift_db(ch)
            if sh is not None:
                shifts = {r.id: s for r, s in zip(takes, sh)}
        for rec in takes:
            row = self._make_take_row(ch, rec, lo, hi,
                                      driver=self._spread_driver,
                                      mean=mean,
                                      shift=shifts.get(rec.id, 0.0))
            row.set_visible(self._takes_open)
            lb.append(row)
            self._page["take_rows"].append(row)
        lb.set_visible(bool(takes))
        self._refresh_summary(ch, takes)

    def _refresh_summary(self, ch, takes):
        """The channel's result at a glance: the mean response over the
        takes with the take-to-take spread as a band around it, greyed
        outside the EQ range, red where the spread is untrustworthy.
        Level moves between takes are compensated with the session's
        recorded gains, so the mean matches what finalize will build.
        When the channel's L<->R mirror partner is measured on the
        same capsule, its drive-corrected mean is drawn as a dashed
        ghost: the pair's symmetry, visible before Create profile."""
        area = self._page["summary"]
        if not takes:
            area.set_visible(False)
            return
        shifts = self.session.comp_shift_db(ch)
        by_id = {}
        if shifts is not None:
            by_id = {r.id: s for r, s
                     in zip(self.session.takes_of(ch), shifts)}
        clean = [r for r in takes
                 if ms.take_quality(r) == ms.TAKE_CLEAN]
        base = clean if clean else takes
        mean = sum(r.mag_db + by_id.get(r.id, 0.0)
                   for r in base) / len(base)
        spread = self.session.spread_db(ch)
        sp = spread if spread is not None else mean * 0.0
        ghost, glabel = self._partner_ghost(ch, mean)
        lo = float((mean - sp / 2.0).min()) - 1.0
        hi = float((mean + sp / 2.0).max()) + 1.0
        if ghost is not None:
            lo = min(lo, float(ghost.min()) - 1.0)
            hi = max(hi, float(ghost.max()) + 1.0)
        area.set_draw_func(self._make_summary_draw(
            base[0].freq_hz, mean, sp, lo, hi, ghost, glabel))
        area.set_visible(True)
        area.queue_draw()

    def _partner_ghost(self, ch, mean):
        """(curve, label) of the mirror partner's compensated mean --
        a dashed reference for the PAIR's symmetry -- or (None, None).
        Level-true (shifted onto this channel's drive, the trim's
        accounting) when the drive difference is knowable; when it is
        NOT (a hardware-volume device releveled between channels), the
        ghost falls back to shape-only: aligned by the 200-2000 Hz
        band means, the level claim explicitly dropped and the label
        saying so -- a stable seal leak is a SHAPE difference, and
        silently hiding the ghost once hid exactly that. Nothing is
        drawn when the channel has no L<->R partner, the partner has
        no takes, or the capsules differ (two couplers share no
        reference of any kind)."""
        if self.session is None:
            return None, None
        pk = ms.mirror_key(self.ch_keys[ch])
        if pk is None or pk not in self.ch_keys:
            return None, None
        p = self.ch_keys.index(pk)
        if not self.session.takes_of(p):
            return None, None
        if self.mic_of.get(p, p) != self.mic_of.get(ch, ch):
            return None, None
        pavg, _sp = self.session.average_and_spread(p)
        if pavg is None:
            return None, None
        shift = self.session.drive_shift_db(p, ch)
        if shift is not None:
            return pavg + shift, pk
        fa = np.asarray(self.session.freqs, float)
        ref = (fa >= 200.0) & (fa <= 2000.0)
        if not ref.any():
            return None, None
        aligned = pavg - float(np.asarray(pavg)[ref].mean()) \
            + float(np.asarray(mean)[ref].mean())
        return aligned, "%s · shape only" % pk

    def _make_summary_draw(self, freqs, mean, spread, lo, hi,
                           ghost=None, ghost_label=None):
        flo, fhi = self.fit_lo, self.fit_hi

        def draw(_area, cr, w, h, *_):
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.10)
            cr.rectangle(0, 0, w, h)
            cr.fill()
            span = max(1e-6, hi - lo)

            def yof(v):
                y = h - 3 - (float(v) - lo) / span * (h - 6)
                return max(1, min(h - 1, y))

            if ghost is not None:
                cr.save()
                cr.set_source_rgba(0.45, 0.45, 0.45, 0.9)
                cr.set_line_width(1.2)
                cr.set_dash([4.0, 3.0])
                first = True
                for j in _stride_idx(len(freqs)):
                    x = _log_x(freqs[j], 2, w - 4)
                    y = yof(ghost[j])
                    cr.move_to(x, y) if first else cr.line_to(x, y)
                    first = False
                cr.stroke()
                cr.restore()
                if ghost_label:
                    cr.set_source_rgba(0.45, 0.45, 0.45, 0.9)
                    cr.set_font_size(10)
                    ext = cr.text_extents(ghost_label)
                    cr.move_to(w - ext.width - 6, 12)
                    cr.show_text(ghost_label)
            idx = _stride_idx(len(freqs))
            bw = max(1.0, (w - 4) / max(1, len(idx)))
            for j in idx:
                x = _log_x(freqs[j], 2, w - 4)
                sv = float(spread[j]) if j < len(spread) else 0.0
                yt, yb = yof(mean[j] + sv / 2.0), yof(mean[j] - sv / 2.0)
                if sv >= ms.SPREAD_MAX_DB:
                    cr.set_source_rgba(0.87, 0.19, 0.19, 0.35)
                else:
                    cr.set_source_rgba(0.22, 0.52, 0.90, 0.18)
                cr.rectangle(x, yt, bw, max(1.0, yb - yt))
                cr.fill()
            cr.set_source_rgb(0.22, 0.52, 0.90)
            cr.set_line_width(1.6)
            first = True
            for j in idx:
                x = _log_x(freqs[j], 2, w - 4)
                y = yof(mean[j])
                cr.move_to(x, y) if first else cr.line_to(x, y)
                first = False
            cr.stroke()
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.38)
            xlo, xhi = _log_x(flo, 2, w - 4), _log_x(fhi, 2, w - 4)
            if xlo > 0:
                cr.rectangle(0, 0, xlo, h)
                cr.fill()
            if xhi < w:
                cr.rectangle(xhi, 0, w - xhi, h)
                cr.fill()
        return draw

    # ---- callbacks (config) -----------------------------------------------
    def _on_pw_state(self, st):
        """The shared PWState refresh drives the whole window: keep the
        input list current, then reconcile the target sink against the
        graph. One pipewire poll feeds this instead of a window timer."""
        self._refresh_sources_from(st.sources)
        self._reconcile_sink(st)
        return False

    def _reconcile_sink(self, st):
        """The session belongs to its sink: alive or Unavailable,
        nothing else. Chasing the default and asking stay-or-go made
        sense when the wizard owned unsaved takes; with takes
        persisted per profile a retarget mid-session is a category
        error, so the machinery is gone."""
        alive = any(s["name"] == self.sink_node for s in st.sinks)
        self._set_sink_gone(not alive)

    def _refresh_sources_from(self, sources):
        prev = [s["name"] for s in self.sources]
        new = [s["name"] for s in sources]
        if new == prev:
            return                           # nothing plugged/unplugged
        cur = self._source_name()
        self.sources = sources
        names = [s["desc"] for s in sources] or ["(no sources found)"]
        self.source_dd.handler_block_by_func(self._on_source_changed)
        self.source_dd.set_model(Gtk.StringList.new(names))
        idx = next((i for i, s in enumerate(sources)
                    if s["name"] == cur), 0)
        self.source_dd.set_selected(idx)
        self.source_dd.handler_unblock_by_func(self._on_source_changed)
        if not sources or sources[idx]["name"] != cur:
            self._on_source_changed()        # selection actually changed
        self._refresh_all()

    def _sink_present(self):
        return any(s["name"] == self.sink_node for s in self._pw.sinks)

    def _set_sink_gone(self, gone):
        if gone == self._sink_gone:
            return
        self._sink_gone = gone
        self._center_box.set_sensitive(not gone)
        self._gone_holder.set_visible(gone)
        self.vol_spin.set_sensitive(not gone)
        self.relevel_btn.set_sensitive(not gone)
        self._set_ring_sensitive(not gone)
        self._disc.queue_draw()          # the red edge follows
        if gone:
            self.warning.set_text(
                "The output device is gone -- its channel "
                "configuration changed, or it was unplugged. Bring "
                "it back to keep measuring; you can still save what "
                "you have measured.")
        else:
            self.warning.set_text("")
            self._refresh_all()

    def _on_source_changed(self, *_):
        src = self._selected_source()
        if not src:
            return
        self._recompute_mic()
        prof = self.mic_store.match(src["name"])
        self.cal = {}
        if prof:
            for i in range(self.mic_ch):
                path = prof.get("cal", {}).get(str(i))
                if path:
                    self.cal[i] = path
        self._rebuild_cal_row()
        self._rebuild_map_slots()
        self._sync_cal_labels()
        self._persist_mic()
        self._reset_unarmed_session()

    def _reset_unarmed_session(self):
        """The mic or its capsule count changed before anything was
        measured: the session's cfg is baked at construction, so
        rebuild it (and re-adopt the stored takes) instead of
        measuring with a stale source. An armed session is locked --
        exactly the old behavior."""
        if self.session is None or self._entered:
            return
        self.session = None
        self._canvas_ids = {}
        self._canvas_session = None
        self._rig_blocked = False
        self.warning.set_text("")
        self._ensure_session(arm=False, quiet=True)
        self._refresh_all()

    def _recompute_mic(self):
        self.mic_ch = self._mic_channels()
        self.mic_of = self._default_mic_of()
        self._sync_chan_dd()

    def _sync_chan_dd(self):
        if not getattr(self, "chan_dd", None):
            return
        self.chan_dd.handler_block_by_func(self._on_chan_changed)
        self.chan_dd.set_selected(1 if self.mic_ch >= 2 else 0)
        self.chan_dd.handler_unblock_by_func(self._on_chan_changed)

    def _on_chan_changed(self, *_):
        self.mic_ch = 2 if self.chan_dd.get_selected() == 1 else 1
        self.mic_of = self._default_mic_of()
        self.cal = {k: v for k, v in self.cal.items() if k < self.mic_ch}
        self._rebuild_cal_row()
        self._rebuild_map_slots()
        self._sync_cal_labels()
        self._persist_mic()
        self._reset_unarmed_session()

    def _mic_channels(self):
        src = self._selected_source()
        if not src:
            return 2
        prof = self.mic_store.match(src["name"])
        if prof and prof.get("channels") in (1, 2):
            return prof["channels"]     # the user pinned it for this rig
        try:
            n = len(pipewire.source_channels(src["name"]))
        except Exception:
            n = 2
        return max(1, min(2, n))        # a measurement rig is 1- or 2-ch

    def _mic_labels(self):
        return ["L", "R"] if self.mic_ch >= 2 else ["Mono"]

    def _default_mic_of(self):
        m = {}
        for k, key in enumerate(self.ch_keys):
            right = self.mic_ch >= 2 and key.upper().endswith("R")
            m[k] = 1 if right else 0
        return m

    def _rebuild_cal_row(self):
        for btn in getattr(self, "cal_btns", {}).values():
            self.cal_controls.remove(btn)
        self.cal_btns = {}
        labels = self._mic_labels()
        for i in range(self.mic_ch):
            btn = Gtk.Button(label="%s cal…" % labels[i])
            btn.set_valign(Gtk.Align.CENTER)
            btn.set_hexpand(True)
            btn.set_tooltip_text("Calibration for the rig's %s capture "
                                 "channel; its RAW/HEQ/IDF/HPN domain is "
                                 "the compensation" % labels[i])
            btn.connect("clicked", self._make_cal_cb(i))
            self.cal_controls.append(btn)
            self.cal_btns[i] = btn

    def _rebuild_map_slots(self):
        for slot in (self.map_left_slot, self.map_right_slot):
            child = slot.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                slot.remove(child)
                child = nxt
        self.map_dds = {}
        if self.n_ch == 2 and self.mic_ch == 2:
            for k, slot in ((0, self.map_left_slot),
                            (1, self.map_right_slot)):
                col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                              spacing=2)
                dd = Gtk.DropDown.new_from_strings(["L", "R"])
                dd.set_selected(self.mic_of.get(k, k))
                dd.set_tooltip_text("Which mic capsule captures %s"
                                    % _speaker_name(self.ch_keys[k]))
                dd.connect("notify::selected", self._make_map_cb(k))
                icon = Gtk.Image.new_from_icon_name(
                    "audio-input-microphone-symbolic")
                col.set_halign(Gtk.Align.CENTER)
                col.append(icon)            # picker under the mic:
                col.append(dd)              # a column fits the ring
                slot.append(col)
                self.map_dds[k] = dd

    def _make_map_cb(self, k):
        def cb(dd, _p):
            self.mic_of[k] = dd.get_selected()
        return cb

    def _make_cal_cb(self, ch):
        def cb(_btn):
            dialog = Gtk.FileDialog()
            dialog.set_title("Choose cal for rig channel %s"
                             % self._mic_labels()[ch])

            def done(d, res):
                try:
                    gfile = d.open_finish(res)
                except GLib.Error:
                    return
                path = gfile.get_path() if gfile else None
                if path:
                    self.cal[ch] = path
                    self._sync_cal_labels()
                    self._persist_mic()
            dialog.open(self, None, done)
        return cb

    def _make_discard_cb(self, ch, take_id):
        def cb(_btn):
            if self.session is None or self._busy:
                return
            try:
                self.session.discard(ch, take_id)
            except ms.MeasureError:
                return
            cid = self._canvas_ids.pop((ch, take_id), None)
            if cid is not None and self.edit_pid:
                try:
                    measure_build.remove_takes(
                        self.parent.store, self.edit_pid, [cid])
                except Exception as e:
                    self._error("Could not delete the stored take: "
                                "%s" % e)
            self._refresh_all()
        return cb

    # ---- measurement ------------------------------------------------------
    def _make_speaker_cb(self, ch):
        def cb(_btn):
            self._select_channel(ch)
        return cb

    def _select_channel(self, ch):
        self._selected_ch = ch
        for i, spk in self._speakers.items():
            spk.set_active(i == ch)
            if i == ch:                      # plus a hard outline
                spk.add_css_class("speaker-on")
            else:
                spk.remove_css_class("speaker-on")
        self._rebuild_page()
        self._update_pult()

    def _on_play(self, _btn):
        self._start_measure(self._selected_ch)

    def _on_stop(self, _btn):
        if self.session is not None and self._busy:
            self.session.cancel()            # aborts the sweep in flight

    def _update_pult(self):
        live = self._sink_present() and not self._sink_gone
        self.play_btn.set_sensitive(not self._busy and live)
        self.stop_btn.set_sensitive(self._busy)

    def _ensure_session(self, arm=True, quiet=False):
        """Construct the session separately from ARMING it.
        Construction is read-only (node identities, layout, sweep
        synthesis) and happens at window open, so the profile's
        stored takes are adopted and visible before a single sweep;
        __enter__ (the tempdir, foreign-stream muting, the profile
        bypass, the start volume) waits for the first sweep. quiet
        suppresses the dialogs for the opportunistic open-time
        attempt -- no mic picked yet is not an error there."""
        if self.session is None:
            if not self._sink_present():
                if not quiet:
                    self._error(
                        "This output no longer exists -- the "
                        "device's channel configuration changed and "
                        "renamed its sink. Close and reopen the "
                        "measurement for the current device.")
                return False
            src = self._selected_source()
            if not src:
                if not quiet:
                    self._error("Pick a measurement mic first.")
                return False
            remembered = self.memory.volume_for(self.sink_node,
                                                src["name"])
            use_auto = remembered is None or self._relevel_pending
            cfg = ms.SessionConfig(
                sink=self.sink_node, source=src["name"],
                channels=self.mic_ch, auto_level=use_auto,
                mute_others=True, device=self.sink_desc,
                start_volume=(None if use_auto else remembered))
            self._relevel_pending = False
            try:
                self.session = ms.MeasureSession(cfg)
            except ms.RefusalError as e:
                self.session = None
                if not quiet:
                    self._error(str(e))
                return False
            except Exception as e:               # missing tools, etc.
                self.session = None
                if not quiet:
                    self._error("Could not start: %s" % e)
                return False
            self._adopt_canvas()
        if arm and not self._entered:
            try:
                self.session.__enter__()
                self._entered = True
            except ms.RefusalError as e:
                if not quiet:
                    self._error(str(e))
                return False
            except Exception as e:
                if not quiet:
                    self._error("Could not start: %s" % e)
                return False
        return True

    def _start_measure(self, ch, level_only=False):
        if self._busy:
            return
        if not self._loud_ack:
            self._confirm_loud(
                lambda: self._start_measure(ch, level_only))
            return
        if not self._ensure_session():
            return
        if self._rig_blocked:
            self._error("This profile was measured with a different "
                        "rig; measuring here is blocked. Create a "
                        "new profile for this rig.")
            return
        self._level_only = level_only
        self._busy = True
        self._set_ring_sensitive(False)
        self._update_pult()
        self.center.set_text(
            "Measuring the level on %s\u2026" % self.ch_keys[ch]
            if level_only else
            "Measuring %s\u2026" % self.ch_keys[ch])
        t = threading.Thread(target=self._measure_worker, args=(ch,),
                             daemon=True)
        t.start()

    def _measure_worker(self, ch):
        """Runs one accepted take on a worker thread: loop through
        auto-level probes (each already moved the volume), and if the
        level gets stuck accept it so a take is always produced (its
        quality is flagged for the user). Result marshalled to the UI."""
        result = {"error": None, "outcome": None}
        try:
            guard = 0
            while True:
                guard += 1
                out = self.session.take(
                    ch, analyze=self.mic_of.get(ch, 0))
                if out.kind == "level_probe" and guard < 12:
                    lv = out.level or {}
                    snr = lv.get("snr_db")
                    self._post_status(
                        "%s: leveling %d%% → %d%%  "
                        "(peak %.1f dBFS, SNR %s, step %d/%d)"
                        % (self.ch_keys[ch],
                           round(100 * lv.get("volume_from", 0)),
                           round(100 * lv.get("volume_to", 0)),
                           lv.get("peak_dbfs", float("nan")),
                           "%.1f" % snr if snr is not None else "n/a",
                           lv.get("step", 0), lv.get("max_steps", 0)))
                    GLib.idle_add(self._set_volume_display,
                                  lv.get("volume_to"))
                    continue
                if out.kind == "level_stuck":
                    lv = out.level or {}
                    why = lv.get("why") or "level stuck"
                    self._post_status(
                        "%s: auto-level gave up -- %s; keeping %d%% "
                        "(peak %.1f dBFS)"
                        % (self.ch_keys[ch], why,
                           round(100 * lv.get("volume", 0)),
                           lv.get("peak_dbfs", float("nan"))))
                    out = self.session.accept_level()
                if getattr(self, "_level_only", False) \
                        and out.kind == "take" and out.take is not None:
                    # this button measures the LEVEL; the locking
                    # sweep is evidence, not a take
                    self.session.discard(ch, out.take.id)
                    self._post_status(
                        "%s: level %d%% (probe sweeps only)"
                        % (self.ch_keys[ch],
                           round(100 * (self.session._v_cur or 0))))
                result["outcome"] = out
                break
        except Exception as e:
            result["error"] = e
        GLib.idle_add(self._measure_done, ch, result)

    def _post_status(self, text):
        GLib.idle_add(self.center.set_text, text)

    def _measure_done(self, ch, result):
        self._busy = False
        self._set_ring_sensitive(True)
        self.center.set_text("Click a speaker to measure")
        err = result["error"]
        if isinstance(err, ms.MeasureCancelled):
            self._refresh_all()              # Stop: quiet, nothing stored
            return False
        if err is not None:
            self._error("Measurement failed: %s" % err)
            self._refresh_all()
            return False
        out = result.get("outcome")
        if (out is not None and getattr(out, "kind", "") == "take"
                and out.take is not None
                and not getattr(self, "_level_only", False)):
            self._commit_live_take(ch, out.take)
        v = getattr(self.session, "_v_cur", self.session.volume_start)
        src = self._source_name()
        if v is not None and src:
            self.memory.remember(self.sink_node, source=src, volume=v)
        self._refresh_all()
        return False

    def _set_ring_sensitive(self, on):
        for spk in self._speakers.values():
            spk.set_sensitive(on)

    # ---- the incremental contract ------------------------------------
    def _ensure_pid(self):
        """The profile this window edits. A fresh window creates it
        on first need -- the first committed take, or the plain close
        that still leaves an empty profile behind (New's contract) --
        and binds it to the sink."""
        if self.edit_pid:
            return self.edit_pid
        store = self.parent.store
        pid = store.save_user({
            "name": self._profile_name(),
            "apply_all": True, "preamp": 0.0, "ch_keys": [],
            "all": {"bands": []}, "channels": {}})
        store.set_binding(self.sink_node, pid)
        self.edit_pid = pid
        return pid

    def _apply_name(self, pid):
        name = self._profile_name()
        store = self.parent.store
        prof = store.get(pid)
        if prof and prof.get("name") != name:
            store.save_user(dict(prof, name=name))

    def _commit_live_take(self, ch, rec):
        """An accepted take is a fact of the profile the moment it
        exists: kill the app, pull the plug -- the take survives.
        Creates the profile on a fresh window's first take, threads
        one canvas session entry through the sitting, and remembers
        the canvas id so this row's trash can deletes from the
        profile too."""
        try:
            pid = self._ensure_pid()
            col = rec.capture_channel
            cal = None
            if col is not None:
                cal = self.cal.get(col, self.cal.get(str(col)))
            ids = measure_build.commit_take(
                self.parent.store, pid, self.session, ch,
                self.ch_keys[ch], rec.id, cal=cal,
                source=self._source_info(),
                canvas_session=self._canvas_session)
            self._canvas_session = ids["session"]
            self._canvas_ids[(ch, rec.id)] = ids["take"]
        except Exception as e:
            self._error("Could not store the take: %s" % e)

    def _adopt_canvas(self):
        """Seed the fresh session with the profile's stored takes so
        the counts, the spread statistics and the take list span the
        whole history instead of one sitting. Only under a matching
        rig; adopted takes come back as records without samples (the
        canvas magnitudes on the canvas grid) and their trash cans
        delete from the profile."""
        if not self.edit_pid or self.session is None:
            return
        prof = self.parent.store.get(self.edit_pid) or {}
        m = prof.get("measurement") or {}
        takes = m.get("takes") or []
        if not takes:
            return
        src = self._source_info() or {}
        node = self.session.source_ident.get("name")
        stored_src = m.get("source")
        if stored_src and not measure_build.rig_matches(
                stored_src, src.get("serial"), node):
            self.warning.set_text(
                "This profile was measured with %s; its stored "
                "takes stay hidden and measuring here is blocked."
                % (stored_src.get("name")
                   or stored_src.get("node_match")
                   or "another rig"))
            self._rig_blocked = True
            return
        self._rig_blocked = False
        g = m.get("grid") or {}
        freqs = mc.log_grid(float(g.get("f_lo", mc.GRID_F_LO)),
                            float(g.get("f_hi", mc.GRID_F_HI)),
                            int(g.get("ppo", mc.GRID_PPO)))
        key_to_ch = {k: i for i, k in
                     enumerate(self.ch_keys[:self.n_ch])}
        for t in takes:
            ch = key_to_ch.get(t.get("channel"))
            if ch is None:
                continue
            rec = ms.TakeRecord(
                t.get("id"), ch, freqs,
                t.get("mag_db_uncal") or [],
                t.get("delay_ms"), t.get("snr_db"),
                t.get("peak_dbfs"), int(t.get("clipped") or 0),
                int(t.get("repaired") or 0), None,
                chan_vol=t.get("chan_vol"),
                soft_vol=t.get("soft_vol"),
                noise_dbfs=t.get("noise_dbfs"),
                capture_channel=t.get("capture_channel"),
                created_utc=t.get("created_utc"))
            self.session.adopt_take(ch, rec)
            self._canvas_ids[(ch, rec.id)] = rec.id
        self._refresh_all()

    def _should_autofit(self, pid):
        """Full house and an unsettled fit: three clean takes on
        every channel (the session adopted the history, so the
        counts span it) and a fit that is absent, stale or does not
        cover the canvas. Hand-edited fits are never discarded
        silently -- the editor's Re-fit asks."""
        if self.session is None or self._rig_blocked:
            return False
        if any(self._clean_count(i) < CLEAN_TARGET
               for i in range(self.n_ch)):
            return False
        prof = self.parent.store.get(pid) or {}
        m = prof.get("measurement") or {}
        if not m.get("takes"):
            return False
        fit = prof.get("fit")
        if not fit:
            return True
        if fit.get("edited"):
            return False
        from . import refit
        ids = {t.get("id") for t in m["takes"]}
        return (refit.fit_is_stale(prof)
                or bool(ids - set(fit.get("takes") or [])))

    def _parent_reload(self, pid):
        """Freshen the parent after a session. Only steal its
        selection when we measured ITS current device; a foreign
        profile's session refreshes the picker and leaves the
        playing profile alone -- the computer may be playing one
        device while another was being measured."""
        try:
            if self.parent.node == self.sink_node:
                self.parent._select_device(self.sink_node,
                                           load=False)
                self.parent._load_profile(pid)
            else:
                self.parent._populate_picker()
        except Exception:
            pass
        return False

    def _persist_mic(self):
        """Save the chosen mic + its per-capture-channel cal (bound to the
        source node) and remember the mic for this sink as soon as either
        changes -- not only at create."""
        src = self._selected_source()
        if not src:
            return
        existing = self.mic_store.match(src["name"])
        cal = {str(i): p for i, p in self.cal.items() if p}
        if not cal and existing is None:
            return
        body = {"name": src["desc"], "node_match": src["name"],
                "serial": ((existing or {}).get("serial", "")
                           or measure_prefs.serial_from_cal(cal.values())),
                "cal": cal, "channels": self.mic_ch}
        if existing:
            body["id"] = existing["id"]
        pid = self.mic_store.save(body)
        self.memory.remember(self.sink_node, mic_profile=pid)

    def _source_info(self):
        """What the session cannot know about the rig: its display
        name and serial (from the saved mic profile matching the
        selected source, when one exists). Feeds measure_build's
        source block; called on the main thread before the worker."""
        src = self._selected_source()
        if not src:
            return None
        existing = self.mic_store.match(src["name"])
        return {"name": src["desc"],
                "serial": ((existing or {}).get("serial", "")
                           or measure_prefs.serial_from_cal(
                               self.cal.values()))}

    def _profile_name(self):
        return (self.name_row.get_text().strip() or self.sink_desc)

    # ---- scroll taming (wheel must not change spin/dropdown values) ----
    def _tame_scroll(self, widget):
        """Keep the wheel from editing a value; scroll the page."""
        ctrl = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL)
        ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        ctrl.connect("scroll", self._on_widget_scroll)
        widget.add_controller(ctrl)

    def _on_widget_scroll(self, ctrl, dx, dy):
        """CAPTURE-phase handler backing _tame_scroll: forward the
        wheel to the enclosing scrolled page and swallow it here, so
        the hovered value is left untouched."""
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

    # ---- dialogs / teardown -----------------------------------------------
    def _confirm_loud(self, on_ok):
        dlg = Adw.AlertDialog(
            heading="This will play loudly",
            body="A measurement sweep will now play on this device at the "
                 "measurement level. Take your headphones off your head if "
                 "they are not on the rig.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("go", "Play sweep")
        dlg.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("go")
        dlg.set_close_response("cancel")

        def on_resp(_d, resp):
            if resp == "go":
                self._loud_ack = True
                on_ok()
        dlg.connect("response", on_resp)
        dlg.present(self)

    def _error(self, text):
        dlg = Adw.AlertDialog(heading="Measurement", body=text)
        dlg.add_response("ok", "OK")
        dlg.set_default_response("ok")
        dlg.present(self)

    def _on_parent_close(self, *_):
        if self._busy and self.session is not None:
            try:
                self.session.cancel()
            except Exception:
                pass
            self._busy = False
        self.close()
        return False                     # the parent proceeds

    def _on_close(self, *_):
        if self._busy:
            return True                  # a sweep is in the air
        pid = self._ensure_pid()         # New: even empty stays
        self._apply_name(pid)
        try:
            self._persist_mic()
        except Exception:
            pass
        fit = self._should_autofit(pid)
        bands = self.bands_spin.get_value_as_int()
        f_lo, f_hi = float(self.fit_lo), float(self.fit_hi)
        if getattr(self, "_parent_close_id", None) is not None:
            try:
                self.parent.disconnect(self._parent_close_id)
            except Exception:
                pass
            self._parent_close_id = None
        self.parent.set_sensitive(True)
        self._teardown()
        GLib.idle_add(self._parent_reload, pid)
        if fit:                # the parent shows the progress OSD
            GLib.idle_add(self.parent._start_profile_fit, pid,
                          bands, f_lo, f_hi)
        return False

    def _teardown(self):
        if getattr(self, "_pw_unsub", None) is not None:
            self._pw_unsub()
            self._pw_unsub = None
        if self.session is not None and self._entered:
            try:
                self.session.__exit__(None, None, None)
            except Exception:
                pass
            self._entered = False
