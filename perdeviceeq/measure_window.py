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
import re
import threading

import numpy as np

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, GLib, Gdk, Adw, Pango  # noqa: E402

from . import config, pipewire, measure_build       # noqa: E402
from . import focus                                  # noqa: E402
from .picker import NodePicker                       # noqa: E402
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


def _node_identity(node):
    """A rig's node identity past the usb enumeration tail:
    alsa_input.usb-X_00002-00.analog-stereo and _00003-00 are
    the same instrument replugged."""
    return re.sub(r"_\d+-\d+(?=\.)", "", node or "")


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
        self._mic_gone = False      # selected rig left the graph
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
        self._select_profile_rig()
        self._ensure_session(arm=False, quiet=True)
        self._refresh_all()
        self._pw_unsub = self._pw.subscribe(self._on_pw_state)
        self._pw.start()
        # Birth reconcile: PWState notifies on CHANGE only, so a
        # home already gone at open would stay un-announced until
        # the graph happens to move -- no banner, no locks, a
        # split state (reachable since the edit opens on an
        # absent home). At idle, so every widget the gone costume
        # touches exists: ordering has bitten this constructor
        # before. Guarded on a non-empty pump; a still-empty pump
        # fills on the first poll, which IS a change and
        # notifies.
        if self._pw.sinks:
            GLib.idle_add(self._on_pw_state, self._pw)

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

        # the sink is a choice, not a sentence, and it IS the
        # window's title -- the same centered picker grammar as
        # the main window's device picker, minus following. New
        # profiles arrive with the main window's sink; edits
        # arrive with the profile's own home; this switches to
        # any other route without losing the sitting.
        self.gone_banner = b.get_object("gone_banner")
        self.mic_banner = b.get_object("mic_banner")
        self.sink_dd = b.get_object("sink_dd")
        self.picker = NodePicker(self.sink_dd, self._on_sink_pick,
                                 ellipsis=34)
        self._tame_scroll(self.sink_dd)
        self.picker.select(self.sink_node, self.sink_desc)
        self._refresh_sinks_from(self._pw)
        self.center = b.get_object("status")
        self.name_row = b.get_object("name_row")
        self.name_row.set_text(
            (self.edit_prof or {}).get("name") or self.sink_desc)

        self._build_mic_controls(b.get_object("source_row"),
                                 b.get_object("chan_row"),
                                 b.get_object("mic_group"))

        self.map_left_slot = Gtk.Box()
        self.map_left_slot.set_valign(Gtk.Align.CENTER)
        self.map_right_slot = Gtk.Box()
        self.map_right_slot.set_valign(Gtk.Align.CENTER)
        ring_host = b.get_object("ring_host")
        ring_host.set_spacing(12)
        ring_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                           spacing=6)
        # the ring centers in the space RIGHT of the fader, and
        # the status line below shares that axis (its lead bin
        # is size-grouped with the fader)
        ring_col.set_hexpand(True)
        ring_col.append(self._build_ring())
        self.ready_hint = Gtk.Label(xalign=0.5)
        self.ready_hint.add_css_class("success")
        self.ready_hint.set_wrap(True)
        self.ready_hint.set_max_width_chars(46)
        self.ready_hint.set_text(
            "Ready to fit -- close this window to hear your "
            "best version.")
        self.ready_hint.set_visible(False)
        ring_col.append(self.ready_hint)
        self.vol_spin.set_valign(Gtk.Align.START)
        ring_host.append(self.vol_spin)  # pinned to the left edge
        ring_host.append(ring_col)
        lead = b.get_object("status_lead")
        # the size group matches the bin's WIDTH to the fader,
        # but a Box hands a child only its natural width -- and
        # hexpand is NOT the tool (expand flags propagate up:
        # the bin would start expanding too and drag the whole
        # status line off both axes, field-proven). Homogeneous
        # hands the lone child the bin's full width without
        # touching expand, and center-halign lands the button
        # on the fader's trough axis.
        lead.set_homogeneous(True)
        lead.append(self.relevel_btn)
        # The architect's word on the walk: auto-level rides
        # SECOND, right after the fader -- the two speak the
        # same language. The widget keeps its settled home
        # under the fader; the jump is focus-only: Tab off the
        # fader lands on auto-level, Tab off auto-level enters
        # the ring, the ring's backward exit returns here, and
        # its FORWARD exit hands the fit area -- without that
        # neighbor GTK fell back to positional sort and looped
        # play -> auto-level (field-caught).

        def _tab(kv, back_to, fwd_to):
            def on_key(_c, keyval, _code, state):
                shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
                if keyval == Gdk.KEY_Tab and not shift:
                    return fwd_to()
                if (keyval == Gdk.KEY_ISO_Left_Tab
                        or (keyval == Gdk.KEY_Tab and shift)):
                    return back_to()
                return False
            c = Gtk.EventControllerKey()
            c.connect("key-pressed", on_key)
            kv.add_controller(c)

        _tab(self.vol_spin,
             back_to=lambda: False,
             fwd_to=lambda: self.relevel_btn.grab_focus())
        _tab(self.relevel_btn,
             back_to=lambda: self.vol_spin.grab_focus(),
             fwd_to=lambda: self.ring.child_focus(
                 Gtk.DirectionType.TAB_FORWARD))
        sg = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        sg.add_widget(self.vol_spin)
        sg.add_widget(lead)
        self._rebuild_map_slots()

        b.get_object("channel_host").append(self._build_page())
        fa = self._build_fit_area()
        # the ring's forward exit hands the whole takes card,
        # not the fit area past it: the walk enters the takes
        # top-down (a row, its delete, the next row) and reaches
        # the fit area last, inside the same card. The first aim
        # skipped every take -- field-caught, second lesson of
        # the same neighbor.
        self.ring.set_focus_neighbors(prev=self.relevel_btn,
                                      nxt=self._page["card"])
        for side in ("start", "end", "bottom"):
            getattr(fa, "set_margin_" + side)(12)
        fa.set_margin_top(6)
        # always in sight: the range lives on the card, below
        # the fold -- only the take rows tuck away
        self._page["card"].append(fa)

    def _build_mic_controls(self, source_row, chan_row, mic_group):
        # the row IS the picker (AdwComboRow); the popup shows
        # the full name, the row ellipsizes the selected one --
        # the ellipsis cap stays so a monster ALSA description
        # never dictates the window's minimum width
        self.source_dd = source_row
        self.mic_picker = NodePicker(self.source_dd,
                                     self._on_mic_pick,
                                     ellipsis=34)
        self.mic_picker.refresh(self.sources)
        self.source_dd.set_sensitive(bool(self.sources))
        self._tame_scroll(self.source_dd)
        self.chan_dd = chan_row
        self.chan_dd.set_model(Gtk.StringList.new(["Mono", "Stereo"]))
        self.chan_dd.connect("notify::selected", self._on_chan_changed)
        self._tame_scroll(self.chan_dd)
        self.mic_group = mic_group
        self._recompute_mic()
        self._rebuild_cal_row()

    def _build_ring(self):
        self.ring = focus.OrderedFixed()
        self.ring.set_size_request(RING, RING)
        self.ring.set_halign(Gtk.Align.CENTER)
        disc = Gtk.DrawingArea()
        disc.update_property(
            [Gtk.AccessibleProperty.LABEL],
            ["Speaker ring: measured channels around the "
             "listening position"])
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
        # A fixed-height stage with the grid glued to its BOTTOM:
        # the transport is the last grid row in every capsule
        # mode, so play and stop stand still relative to the disc
        # while the mic map row appears ABOVE them (field verdict:
        # the pult must not jump between mono and stereo).
        center_box.set_size_request(RING - 2 * SPEAKER, 96)
        center_box.set_halign(Gtk.Align.CENTER)
        # The mics live INSIDE the ring now, where the volume used
        # to sit: capsule-to-speaker mapping is spatial information,
        # so it belongs in the spatial widget.
        # the invisible grid made visible to the code: two
        # column axes shared by the mic icons, the capsule
        # pickers and the transport -- nothing floats between
        # the columns
        self._center_grid = Gtk.Grid()
        self._center_grid.set_halign(Gtk.Align.CENTER)
        self._center_grid.set_row_spacing(6)
        self._center_grid.set_column_spacing(10)
        self._center_grid.set_column_homogeneous(True)
        self._center_grid.set_vexpand(True)
        self._center_grid.set_valign(Gtk.Align.END)
        self.play_btn = self._pult_btn(
            "media-playback-start-symbolic",
            "Measure the selected channel", self._on_play)
        self.stop_btn = self._pult_btn(
            "media-playback-stop-symbolic", "Stop the sweep", self._on_stop)
        self.stop_btn.set_sensitive(False)
        center_box.append(self._center_grid)
        self.ring.put(center_box, SPEAKER, int(RING / 2 - 56))
        # Tab walks the pult grammar, not the geometry: the
        # speakers first (the targets you set up), then the
        # center -- capsule map, then transport. GTK sorts Tab
        # by position, which put the mics before the speakers;
        # the grammar is groups, and it survives surround
        # (field verdict from the keyboard walk).
        self.ring.set_focus_order(
            [self._speakers[i]
             for i in sorted(self._speakers)] + [center_box])

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
        self.vol_spin.set_size_request(-1, RING)
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
        summary = Gtk.DrawingArea()
        summary.update_property(
            [Gtk.AccessibleProperty.LABEL],
            ["Measured responses of every channel, overlaid"])
        summary.set_content_height(120)
        summary.set_visible(False)
        summary.set_hexpand(True)
        # The summary IS the accordion's face: the channel's
        # result with a chevron on the right -- the same expander
        # grammar as the main window's cards, Revealer breath
        # included; a click anywhere on the face folds the take
        # rows underneath.
        lb = Gtk.ListBox()
        lb.set_show_separators(True)
        lb.set_selection_mode(Gtk.SelectionMode.NONE)
        face = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                       spacing=6)
        for side in ("top", "bottom", "start", "end"):
            getattr(face, "set_margin_" + side)(12)
        trow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                       spacing=6)
        title = Gtk.Label(xalign=0.0)
        title.add_css_class("heading")
        trow.append(title)
        header = Gtk.Label(xalign=0.0)
        header.add_css_class("dim-label")
        header.add_css_class("caption")
        header.set_hexpand(True)
        trow.append(header)
        chev = Gtk.Image.new_from_icon_name("pan-up-symbolic")
        chev.set_valign(Gtk.Align.CENTER)
        # born hidden: a fresh session opens with zero takes and
        # _refresh_takes is not called until something changes
        # (field round five caught the gap), so the initial
        # state must not promise a fold either
        chev.set_visible(False)
        trow.append(chev)
        face.append(trow)
        face.append(summary)
        face.add_css_class("card-header")
        click = Gtk.GestureClick()
        click.set_button(1)
        click.connect("released", self._on_takes_face)
        face.add_controller(click)
        rev = Gtk.Revealer()
        rev.set_transition_type(
            Gtk.RevealerTransitionType.SLIDE_DOWN)
        rev.set_transition_duration(200)
        rev.set_reveal_child(True)
        lb.set_header_func(self._take_header)
        rev.set_child(lb)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.add_css_class("card")
        card.append(face)
        card.append(rev)
        col.append(card)
        self._takes_open = True
        self._page = {"title": title, "header": header,
                      "summary": summary, "takes_list": lb,
                      "takes_rev": rev, "card": card,
                      "chevron": chev, "take_rows": []}
        return col

    def _on_takes_face(self, *_):
        if not self._page["take_rows"]:
            return
        self._takes_open = not self._takes_open
        self._page["chevron"].set_from_icon_name(
            "pan-up-symbolic" if self._takes_open
            else "pan-down-symbolic")
        self._page["takes_rev"].set_reveal_child(
            self._takes_open)

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

    def _take_passport(self, ch, rec):
        """The take's provenance off its canvas passport (schema
        v4: the session's rig stamp, the take's own cal). Returns
        (group, tooltip): group is (key, label) for a take
        whose rig differs from the current one, None otherwise.
        The label is the FULL rig name for a ListBox section
        header -- the architect's palette verdict: a truncated
        mark repeated on every row is the smell, several rows
        under one dictionary value get ONE full-width title,
        and the native rig stays headerless (unmarked is the
        home team). The tooltip keeps the take's own passport,
        degrading gracefully:
        name falls back to the node, the serial appears when
        known, the cal names its file and sha, raw says raw. The
        serial-else-node comparison is informational; nothing is
        gated on it (field doctrine: statistics judge)."""
        if not self.edit_pid:
            return None, None
        m = ((self.parent.store.get(self.edit_pid) or {})
             .get("measurement") or {})
        cid = self._canvas_ids.get((ch, rec.id), rec.id)
        take = next((t for t in m.get("takes") or []
                     if t.get("id") == cid), None)
        if take is None:
            return None, None
        stamp = (((m.get("sessions") or {})
                  .get(take.get("session")) or {})
                 .get("source") or {})
        # Identity law (field-diagnosed on the liberty
        # profile): the NODE decides, normalized past its usb
        # instance tail, so a replugged rig stays itself; the
        # serial speaks only to tell twins apart. Serials copy
        # through the mic store and serial_from_cal, and a
        # store entry saved with a foreign cal in the slots
        # carried the E.A.R.S serial into the Umik -- serial
        # equality must never veto what the nodes say.
        s = stamp.get("serial") or ""
        cur = (self._source_info() or {}).get("serial") or ""
        same_node = (_node_identity(stamp.get("node_match"))
                     == _node_identity(
                         self.mic_picker.core.node))
        if not same_node:
            foreign = True
        elif s and cur and s != cur:
            foreign = True          # twin models, one node name
        else:
            foreign = False
        name = stamp.get("name") or stamp.get("node_match")
        parts = []
        if name:
            head = "Captured with %s" % name
            if s:
                head += " S/N %s" % s
            parts.append(head)
        sha = take.get("cal_sha")
        if sha:
            e = (m.get("cal_library") or {}).get(sha) or {}
            parts.append("cal %s (sha %s)"
                         % (e.get("file") or "?", sha[:16]))
        else:
            parts.append("raw capture")
        tip = " \u00b7 ".join(parts) if parts else None
        group = None
        if foreign and name:
            group = (s or stamp.get("node_match") or name,
                     head)
        return group, tip

    def _take_header(self, row, before):
        """One rig, one header: rows sharing a foreign rig get
        a single full-width section title instead of a
        truncated per-row mark. The clamp owns content width,
        so the header may wrap; the native rig gets no header
        at all."""
        group = getattr(row, "_rig_group", None)
        prev = (getattr(before, "_rig_group", None)
                if before is not None else None)
        if group and group != prev:
            lbl = Gtk.Label(label=group[1], xalign=0.0,
                            wrap=True)
            lbl.add_css_class("caption")
            lbl.add_css_class("dim-label")
            lbl.set_margin_start(12)
            lbl.set_margin_end(12)
            lbl.set_margin_top(6)
            lbl.set_margin_bottom(3)
            row.set_header(lbl)
        else:
            row.set_header(None)

    def _make_take_row(self, ch, rec, lo, hi, driver=None,
                       mean=None, shift=0.0):
        q = ms.take_quality(rec)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
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
        group, passport = self._take_passport(ch, rec)
        lbl = Gtk.Label(label=info, xalign=0.0, hexpand=True)
        # the info line must never dictate the window's width:
        # with a foreign-rig mark appended, its natural width
        # exceeded the window and AdwToolbarView complained on
        # every resize (requested 1110, 1100 available). The
        # mark yields first -- ellipsis at the end -- and the
        # row's tooltip already carries the full passport.
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
        lbl.add_css_class("caption")
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
        if passport:
            # the full passport rides the row, not the info
            # label -- the spread-driver tooltip keeps the label
            body.set_tooltip_text(passport)
        body.append(head)

        curve = Gtk.DrawingArea()
        curve.update_property(
            [Gtk.AccessibleProperty.LABEL],
            ["Frequency response of this take"])
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
        row._rig_group = group
        return row

    def _build_fit_area(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
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
        self.range_area.update_property(
            [Gtk.AccessibleProperty.LABEL],
            ["EQ range over the take-to-take spread"])
        self.range_area.set_content_height(90)
        self.range_area.set_hexpand(True)
        self.range_area.set_draw_func(self._draw_range)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._range_drag_begin)
        drag.connect("drag-update", self._range_drag_update)
        self.range_area.add_controller(drag)
        box.append(self.range_area)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
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

    # ---- prefill / refresh ------------------------------------------------
    def _prefill_from_memory(self):
        pid = self.memory.mic_for(self.sink_node)
        prof = self.mic_store.get(pid) if pid else None
        if prof:
            # cal BEFORE the dropdown: set_selected fires the
            # source-change handler synchronously, and
            # _persist_mic must never see an empty self.cal
            # while a remembered rig exists (the field wipe).
            # Restore every stored capsule, not range(mic_ch)
            # -- the channel count may not be restored yet.
            for k, path in (prof.get("cal") or {}).items():
                try:
                    self.cal[int(k)] = path
                except (TypeError, ValueError):
                    pass
        if prof and prof.get("node_match"):
            self.mic_picker.select(
                prof["node_match"],
                next((s["desc"] for s in self.sources
                      if s["name"] == prof["node_match"]),
                     prof.get("name") or prof["node_match"]))
            self._adopt_selected_source()
        self._sync_cal_labels()

    def _select_profile_rig(self):
        """An edit belongs to its rig: the mic of the profile's
        LAST sitting is selected at birth, present or gone --
        never a silent substitute (field doctrine). Per-sink
        memory still rules new profiles."""
        m = ((self.edit_prof or {}).get("measurement") or {})
        takes = m.get("takes") or []
        sid = takes[-1].get("session") if takes else None
        stored = (((m.get("sessions") or {}).get(sid) or {})
                  .get("source") or {})
        node = stored.get("node_match")
        if not node:
            return
        self.mic_picker.select(
            node, next((s["desc"] for s in self.sources
                        if s["name"] == node),
                       stored.get("name") or node))
        self._adopt_selected_source()

    def _sync_cal_labels(self):
        """Set and unset must read apart at a glance: the row's
        subtitle wears the check mark and the chosen file's
        name, the button flips Choose/Change, and the row's
        tooltip carries the path plus the cal sha16 -- the same
        fingerprint the profile's rig block records. Unset says
        plainly that the capture channel runs raw."""
        labels = self._mic_labels()
        for i, row in enumerate(getattr(self, "cal_rows", [])):
            path = self.cal.get(i)
            btn = self.cal_btns[i]
            if not path:
                badge = self.cal_badges.get(i)
                if badge is not None:
                    badge.set_visible(False)
                row.set_subtitle("not set -- the capture "
                                 "channel runs raw")
                row.set_tooltip_text(
                    "Calibration for the rig's %s capture "
                    "channel; its RAW/HEQ/IDF/HPN domain is "
                    "the compensation" % labels[i])
                btn.set_label("Choose\u2026")
                continue
            row.set_subtitle("\u2713 " + os.path.basename(path))
            cnt, sev, brk = self._cal_testimony(path)
            badge = self.cal_badges.get(i)
            if badge is not None:
                for c in ("green", "amber", "red"):
                    badge.remove_css_class(c)
                if cnt is not None:
                    badge.set_label(str(cnt))
                    badge.add_css_class(sev)
                badge.set_visible(cnt is not None)
            tip = path
            try:
                sha = measure_build.cal_sha_cached(path)
                tip += ("\nsha256 %s -- the profile's rig "
                        "fingerprint records this" % sha[:16])
            except OSError:
                pass
            if brk:
                tip += "\n" + brk
            row.set_tooltip_text(tip)
            btn.set_label("Change\u2026")

    def _selected_source(self):
        """The LIVE source entry behind the picker's choice; None
        when nothing is chosen or the chosen rig is gone (callers
        that can live with a gone rig ask the picker itself)."""
        name = self.mic_picker.core.node
        if name is None:
            return None
        return next((s for s in self.sources
                     if s["name"] == name), None)

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

    def _refresh_cal_manage(self):
        """The Manage row states the canvas's cal reality --
        "2 calibrations \u00b7 12 takes" -- and hides on a
        canvas with no history (nothing to manage)."""
        row = getattr(self, "cal_manage_row", None)
        if row is None:
            return
        m = (((self.parent.store.get(self.edit_pid) or {})
              .get("measurement")) if self.edit_pid else None) or {}
        groups = measure_build.cal_groups(m)
        row.set_visible(bool(groups))
        if groups:
            takes = sum(g["count"] for g in groups)
            cals = sum(1 for g in groups if g["sha"])
            row.set_subtitle("%d calibration%s \u00b7 %d take%s"
                             % (cals, "" if cals == 1 else "s",
                                takes, "" if takes == 1 else "s"))

    def _refresh_all(self):
        ready = self.session is not None
        self._refresh_cal_manage()
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
        self._page["title"].set_text(
            "Takes %s" % self.ch_keys[ch])
        self._page["header"].set_markup(
            "%d/%d clean%s%s" % (n, CLEAN_TARGET, mark, warn))
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
            lb.append(row)
            self._page["take_rows"].append(row)
        lb.set_visible(bool(takes))
        # an empty fold has nothing to promise: the chevron
        # leaves with the takes instead of wagging at nothing
        self._page["chevron"].set_visible(bool(takes))
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
        self._reconcile_source(st)
        self._refresh_sinks_from(st)
        self._reconcile_sink(st)
        return False

    def _refresh_sinks_from(self, st):
        """The shared picker mirrors the graph; the doctrine
        (the target sink always listed, gone when the graph
        lost it, the selection never dangling) lives in
        picker.py now, one implementation for both windows."""
        self.picker.refresh(st.sinks)

    def _on_sink_pick(self, node, desc):
        """A user pick from the shared picker; vetoed while a
        sweep runs (the dropdown is insensitive then, this is
        the second lock on the same door)."""
        if self._busy:
            return False
        self._retarget(node, desc)

    def _retarget(self, node, desc):
        """Move the sitting to another output, keeping everything
        that is the sitting's: the profile, its takes, the rig and
        the cals. The park-then-rebuild is _on_close's own
        sequence -- the canvas is persisted first, the session
        torn down, then reconstructed on the new sink, which
        re-adopts the stored takes. Volume memory and the mic
        home are per-sink and follow the switch."""
        if node == self.sink_node or self._busy:
            return
        pid = self._ensure_pid()
        self._apply_name(pid)
        if self.session is not None and self._entered:
            try:
                self.session.__exit__(None, None, None)
            except Exception:
                pass
            self._entered = False
        self.session = None
        self.sink_node = node
        self.sink_desc = desc
        # legal even when the retarget arrives through a pick:
        # the shell defers its mirror to idle (the synchronous
        # select here is what segfaulted the gone-to-live
        # retarget in the field)
        self.picker.select(node, desc)
        try:
            self._persist_mic()          # the new home learns the rig
        except Exception:
            pass
        self._ensure_session(arm=False, quiet=True)
        self._refresh_volume()
        GLib.idle_add(self._on_pw_state, self._pw)
        self._refresh_all()
        self._update_pult()

    def _reconcile_sink(self, st):
        """The session belongs to its sink: alive or Unavailable.
        Auto-chasing the default stays dead -- stay-or-go prompts
        made sense only when the wizard owned unsaved takes. A
        USER retarget is different (field verdict): the profile
        is the headphones, the sink is merely the route, so the
        header picker moves the sitting deliberately -- parked
        first, rebuilt on the new sink, takes re-adopted."""
        alive = any(s["name"] == self.sink_node for s in st.sinks)
        self._set_sink_gone(not alive)

    def _refresh_sources_from(self, sources):
        """The mic picker wears the sink picker's doctrine: the
        selected rig is never substituted. An unplugged mic
        keeps its row, the mic banner names the state, and only
        the user (or the rig's return) moves anything --
        auto-falling to row 0 is how a foreign mic used to hide
        the measured takes."""
        prev = [s["name"] for s in self.sources]
        self.sources = list(sources)
        self.mic_picker.refresh(self.sources)
        self.source_dd.set_sensitive(
            bool(self.sources or self.mic_picker.core.node))
        if [s["name"] for s in self.sources] != prev:
            self._refresh_all()

    def _sink_present(self):
        return any(s["name"] == self.sink_node for s in self._pw.sinks)

    def _source_present(self):
        name = self.mic_picker.core.node
        return bool(name) and any(
            s["name"] == name for s in self._pw.sources)

    def _reconcile_source(self, st):
        """The rig is never substituted: a vanished mic keeps
        the selection, the banner names the state, the pult
        locks, measuring waits. On the rig's return the mic
        state re-derives and an unarmed session rebuilds
        against it."""
        name = self.mic_picker.core.node
        gone = bool(name) and not any(
            s["name"] == name for s in st.sources)
        if gone == self._mic_gone:
            return
        self._mic_gone = gone
        self.mic_banner.set_revealed(gone)
        self._update_pult()
        if not gone and name:
            self._adopt_selected_source()


    def _set_sink_gone(self, gone):
        if gone == self._sink_gone:
            return
        self._sink_gone = gone
        self.vol_spin.set_sensitive(not gone)
        self._update_pult()
        # field verdict: the banner names the state and the
        # insensitivity shows where it bites -- no homebrew
        # badges, no loose prose outside a card
        self.gone_banner.set_revealed(gone)
        if not gone:
            self._refresh_all()

    def _on_mic_pick(self, node, desc):
        """A deliberate re-pick from the mic picker: the pick IS
        the exit from the gone state (field doctrine: only the
        user or the rig's return moves the mic)."""
        if self._mic_gone:
            self._mic_gone = False
            self.mic_banner.set_revealed(False)
            self._update_pult()
        self._adopt_selected_source()

    def _adopt_selected_source(self):
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
        # the rig is row context: take passports compare against
        # the SELECTED mic, so a rig switch rebuilds the rows.
        # Field-caught with the liberty profile: opened native
        # (E.A.R.S), switched to the Umik in place -- the rows
        # kept their open-time _rig_group=None and no header
        # appeared, while the tooltip (built unconditionally)
        # kept telling the truth. The data was clean; the rows
        # were stale.
        self._rebuild_page()
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

    def _cal_testimony(self, path):
        """The slot's cloud: ONE number -- foreign profiles --
        in a colored pill, everything verbal in the tooltip
        (the architect's dress code: every row equally
        dressed, the noise off the surface). The color is the
        WEIGHT of the statistical anomaly, never a verdict --
        the analog doctrine stands, the one who knows the
        analog layer judges. Native-only biography is an echo
        and shows nothing. Returns (count, severity, tip) or
        three Nones."""
        try:
            sha = measure_build.cal_sha_cached(path)
        except OSError:
            return None, None, None
        entries = measure_build.cal_biography(
            self.parent.store.profiles.values(), sha)
        if not entries:
            return None, None, None
        me = _node_identity(self.mic_picker.core.node)
        f_prof, n_prof = set(), set()
        f_takes = 0
        f_lines = []
        for e in entries:
            if _node_identity(e["node_match"]) == me:
                n_prof.update(e["profiles"])
                continue
            f_prof.update(e["profiles"])
            f_takes += e["count"]
            f_lines.append("%s: %s" % (e["name"], ", ".join(
                "%s (%d)" % (pn, c)
                for pn, c in sorted(e["profiles"].items()))))
        sev = measure_build.badge_severity(
            len(n_prof), len(f_prof))
        if sev is None:
            return None, None, None
        tip = ("used with another rig in %d profile%s "
               "\u00b7 %d take%s"
               % (len(f_prof), "" if len(f_prof) == 1 else "s",
                  f_takes, "" if f_takes == 1 else "s"))
        for ln in f_lines:
            tip += "\n" + ln
        if not n_prof:
            tip += "\nthis pairing has no prior takes"
        return len(f_prof), sev, tip

    _badge_css_installed = False

    @classmethod
    def _install_badge_css(cls):
        if cls._badge_css_installed:
            return
        cls._badge_css_installed = True
        css = Gtk.CssProvider()
        data = """
        .cal-badge {
          border-radius: 10px;
          padding: 1px 8px;
          font-size: 0.85em;
        }
        .cal-badge.green {
          background-color: alpha(@success_bg_color, .2);
          color: @success_color;
        }
        .cal-badge.amber {
          background-color: alpha(@warning_bg_color, .25);
          color: @warning_color;
        }
        .cal-badge.red {
          background-color: alpha(@error_bg_color, .2);
          color: @error_color;
        }
        """
        if hasattr(css, "load_from_string"):
            css.load_from_string(data)
        else:
            css.load_from_data(data.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _rebuild_cal_row(self):
        self._install_badge_css()
        for row in getattr(self, "cal_rows", []):
            self.mic_group.remove(row)
        self.cal_rows = []
        self.cal_btns = {}
        self.cal_badges = {}
        labels = self._mic_labels()
        for i in range(self.mic_ch):
            row = Adw.ActionRow()
            row.set_title("%s calibration" % labels[i])
            badge = Gtk.Label()
            badge.add_css_class("cal-badge")
            badge.set_valign(Gtk.Align.CENTER)
            badge.set_visible(False)
            row.add_suffix(badge)
            self.cal_badges[i] = badge
            btn = Gtk.Button(label="Choose\u2026")
            btn.set_valign(Gtk.Align.CENTER)
            btn.add_css_class("flat")
            btn.connect("clicked", self._make_cal_cb(i))
            row.add_suffix(btn)
            row.set_activatable_widget(btn)
            self.mic_group.add(row)
            self.cal_rows.append(row)
            self.cal_btns[i] = btn
        if not hasattr(self, "cal_manage_row"):
            r = Adw.ActionRow()
            r.set_title("Recorded calibrations")
            b = Gtk.Button(label="Manage\u2026")
            b.set_valign(Gtk.Align.CENTER)
            b.add_css_class("flat")
            b.connect("clicked", lambda *_: self._open_cal_manager())
            r.add_suffix(b)
            r.set_activatable_widget(b)
            self.cal_manage_row = r
        else:
            self.mic_group.remove(self.cal_manage_row)
        self.mic_group.add(self.cal_manage_row)
        self._refresh_cal_manage()
        self._sync_cal_labels()

    def _rebuild_map_slots(self):
        g = self._center_grid
        child = g.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            g.remove(child)
            child = nxt
        for slot in (self.map_left_slot, self.map_right_slot):
            child = slot.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                slot.remove(child)
                child = nxt
        for b in (self.play_btn, self.stop_btn):
            parent = b.get_parent()
            if parent is not None:
                parent.remove(b)
        self.map_dds = {}
        if self.n_ch == 2 and self.mic_ch == 2:
            for k, slot in ((0, self.map_left_slot),
                            (1, self.map_right_slot)):
                col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                              spacing=3)
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
                slot.set_halign(Gtk.Align.CENTER)
                self.map_dds[k] = dd
            g.attach(self.map_left_slot, 0, 0, 1, 1)
            g.attach(self.map_right_slot, 1, 0, 1, 1)
            self.play_btn.set_halign(Gtk.Align.CENTER)
            self.stop_btn.set_halign(Gtk.Align.CENTER)
            g.attach(self.play_btn, 0, 1, 1, 1)
            g.attach(self.stop_btn, 1, 1, 1, 1)
        else:
            # no column axes to honor: the transport rides as
            # a centered pair, exactly as before
            pult = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                           spacing=6)
            pult.set_halign(Gtk.Align.CENTER)
            pult.append(self.play_btn)
            pult.append(self.stop_btn)
            g.attach(pult, 0, 0, 1, 1)

    def _make_map_cb(self, k):
        def cb(dd, _p):
            self.mic_of[k] = dd.get_selected()
        return cb

    def _open_cal_manager(self):
        """The cal history, reified the HIG way: a boxed list in
        a dialog, one row per cal origin, bulk reassign on each
        (the operation is by-sha by design). The refilled list
        after a move IS the feedback."""
        if not self.edit_pid:
            return
        dlg = Adw.Dialog()
        dlg.set_title("Recorded calibrations")
        dlg.set_content_width(440)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_margin_top(12)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)
        lb = Gtk.ListBox()
        lb.set_selection_mode(Gtk.SelectionMode.NONE)
        lb.add_css_class("boxed-list")
        box.append(lb)
        tv.set_content(box)
        dlg.set_child(tv)

        def refill():
            child = lb.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                lb.remove(child)
                child = nxt
            m = ((self.parent.store.get(self.edit_pid) or {})
                 .get("measurement")) or {}
            for g in measure_build.cal_groups(m):
                row = Adw.ActionRow()
                row.set_title(g["file"] or "Raw capture")
                n = g["count"]
                sub = "%d take%s" % (n, "" if n == 1 else "s")
                if len(g["rigs"]) == 1:
                    sub += " \u00b7 " + g["rigs"][0]
                elif g["rigs"]:
                    # a cal that served several rigs answers
                    # HOW MANY TIMES on each -- the sitting's
                    # word on the inventory
                    sub += " \u00b7 " + ", ".join(
                        "%s (%d)" % (r, g["rig_counts"][r])
                        for r in g["rigs"])
                row.set_subtitle(sub)
                b = Gtk.Button(label=("Reassign\u2026" if g["sha"]
                                      else "Assign\u2026"))
                b.set_valign(Gtk.Align.CENTER)
                b.add_css_class("flat")
                b.connect("clicked", self._make_reassign_cb(
                    g["sha"], g["file"], g["count"], refill))
                row.add_suffix(b)
                row.set_activatable_widget(b)
                lb.append(row)
        refill()
        dlg.present(self)
        return dlg

    def _make_reassign_cb(self, sha, fname, count, refill):
        """Chooser, then a plain-words confirmation, then the
        bulk move; the fit stales honestly through its
        fingerprint and the Re-fit machinery offers the
        recompute."""
        def cb(_btn):
            dialog = Gtk.FileDialog()
            dialog.set_title("Choose the calibration to move "
                             "%d take%s onto"
                             % (count, "" if count == 1 else "s"))

            def done(d, res):
                try:
                    gfile = d.open_finish(res)
                except GLib.Error:
                    return
                path = gfile.get_path() if gfile else None
                if not path:
                    return
                ask = Adw.AlertDialog(
                    heading="Move %d take%s?"
                            % (count, "" if count == 1 else "s"),
                    body="%s \u2192 %s\nThe old calibration "
                         "stays in the library; the fit will be "
                         "marked stale."
                         % (fname or "raw capture",
                            os.path.basename(path)))
                ask.add_response("cancel", "Cancel")
                ask.add_response("move", "Move")
                ask.set_default_response("move")
                ask.set_close_response("cancel")

                def done2(_d, resp):
                    if resp != "move":
                        return
                    measure_build.reassign_cal(
                        self.parent.store, self.edit_pid,
                        sha, path)
                    refill()
                    self._refresh_all()
                ask.connect("response", done2)
                ask.present(self)
            dialog.open(self, None, done)
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
        """The pult is the shared gone lock (field verdict): a
        sweep needs a speaker AND a mic, so both sweep triggers
        -- play and the releveler -- obey both ends of the
        chain. The rig's identity gates nothing: a mixed canvas
        is judged by its own spread (spread_trust_bound sinks
        the trust and shrinks the trusted band), and the mic row
        names a foreign rig as a fact. The speakers stay free:
        takes are per channel, and browsing the neighbor's pile
        must survive a gone device."""
        live = (self._sink_present() and not self._sink_gone
                and self._source_present() and not self._mic_gone)
        self.play_btn.set_sensitive(not self._busy and live)
        if getattr(self, "relevel_btn", None) is not None:
            self.relevel_btn.set_sensitive(not self._busy and live)
        self.stop_btn.set_sensitive(self._busy)
        if getattr(self, "sink_dd", None) is not None:
            # mid-sweep the route is not a choice
            self.sink_dd.set_sensitive(not self._busy)

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
            mic = self.mic_picker.core.node
            if not mic:
                if not quiet:
                    self._error("Pick a measurement mic first.")
                return False
            remembered = self.memory.volume_for(self.sink_node,
                                                mic)
            use_auto = remembered is None or self._relevel_pending
            cfg = ms.SessionConfig(
                sink=self.sink_node, source=mic,
                channels=self.mic_ch, auto_level=use_auto,
                mute_others=True, device=self.sink_desc,
                start_volume=(None if use_auto else remembered))
            self._relevel_pending = False
            try:
                # an absent home births the session unresolved:
                # the canvas adopts, statistics and refits run,
                # parking works; the graph (and every live
                # precondition, now FRESH) waits for arming
                self.session = ms.MeasureSession(
                    cfg, resolve=(self._sink_present()
                                  and self._source_present()))
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
        if self._sink_gone or self._mic_gone:
            return    # play is locked; stray starts no-op here
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
        self._update_pult()
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
        whole history instead of one sitting. Adoption is
        unconditional -- a foreign rig blocks ADDING, not viewing
        -- and adopted takes come back as records without samples
        (the canvas magnitudes on the canvas grid); their trash
        cans delete from the profile."""
        if not self.edit_pid or self.session is None:
            return
        prof = self.parent.store.get(self.edit_pid) or {}
        m = prof.get("measurement") or {}
        takes = m.get("takes") or []
        if not takes:
            return
        # The rig's identity gates nothing (field doctrine): a
        # mixed canvas is judged by its own statistics -- the
        # per-take-calibrated spread feeds spread_trust_bound,
        # trust sinks, the trusted band shrinks. The per-take
        # passports (schema v4) will mark foreign takes in the
        # take rows; the whole-canvas subtitle enumeration died
        # with measurement.source.
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
        if self.session is None:
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
        if not cal and existing:
            # never downgrade a remembered rig to
            # calibration-less because a change handler fired
            # before the cals were in RAM
            cal = dict(existing.get("cal") or {})
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
        name = self.mic_picker.core.node
        if not name:
            return None
        existing = self.mic_store.match(name)
        return {"name": self.mic_picker.core.desc,
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
        dlg.add_response("close", "Close")
        dlg.set_default_response("close")
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
