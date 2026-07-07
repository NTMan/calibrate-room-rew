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

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, GLib, Adw            # noqa: E402

from . import config, pipewire, measure_build       # noqa: E402
from . import measure_session as ms                 # noqa: E402
from . import measure_prefs                         # noqa: E402

CARD_W, CARD_H = 150, 60
RING = 240
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
FIT_FLO, FIT_FHI = 20.0, 12000.0
FMIN_PLOT, FMAX_PLOT = 20.0, 20000.0


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


class MeasureWindow(Adw.Window):
    """Measurement wizard for one output sink."""

    def __init__(self, parent, sink_node, sink_desc):
        super().__init__()
        self.parent = parent
        self.sink_node = sink_node
        self.sink_desc = sink_desc
        self.set_title("Measure speakers")
        self.set_default_size(620, 760)
        self.set_modal(True)
        self.set_transient_for(parent)

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
        self._relevel_pending = False
        self._sink_gone = False
        self._pinned = False        # user chose "stay": ignore the default
        self._popup_open = False    # a stay/go dialog is on screen
        self.fit_lo, self.fit_hi = FIT_FLO, FIT_FHI
        self._columns = {}          # ch index -> {box, header, ...}
        self._speakers = {}         # ch index -> Gtk.Button
        self._speaker_counts = {}   # ch index -> Gtk.Label (# takes)

        self._build_ui()
        self.connect("close-request", self._on_close)
        self._prefill_from_memory()
        self._refresh_all()
        self._pw = pipewire.app_state()
        self._pw_unsub = self._pw.subscribe(self._on_pw_state)
        self._pw.start()

    # ---- layout -----------------------------------------------------------
    def _build_ui(self):
        b = Gtk.Builder.new_from_file(_ui_path())
        self.set_content(b.get_object("content"))
        b.get_object("window_title").set_subtitle(self.sink_desc)
        self.center = b.get_object("status")
        self.warning = b.get_object("warning")
        self.create_btn = b.get_object("create_btn")
        self.create_btn.connect("clicked", self._on_create)

        self._build_mic_controls(b.get_object("mic_row"),
                                 b.get_object("capsules_row"),
                                 b.get_object("cal_row"))

        self.map_left_slot = Gtk.Box()
        self.map_left_slot.set_valign(Gtk.Align.CENTER)
        self.map_right_slot = Gtk.Box()
        self.map_right_slot.set_valign(Gtk.Align.CENTER)
        ring_host = b.get_object("ring_host")
        ring_host.set_spacing(8)
        ring_host.append(self.map_left_slot)
        ring_host.append(self._build_ring())
        ring_host.append(self.map_right_slot)
        self._rebuild_map_slots()

        b.get_object("channel_host").append(self._build_columns())
        b.get_object("fit_host").append(self._build_fit_area())

    def _build_mic_controls(self, mic_row, capsules_row, cal_row):
        names = [s["desc"] for s in self.sources] or ["(no sources found)"]
        self.source_dd = Gtk.DropDown.new_from_strings(names)
        self.source_dd.set_valign(Gtk.Align.CENTER)
        self.source_dd.connect("notify::selected", self._on_source_changed)
        mic_row.add_suffix(self.source_dd)
        self.chan_dd = Gtk.DropDown.new_from_strings(["Mono", "Stereo"])
        self.chan_dd.set_valign(Gtk.Align.CENTER)
        self.chan_dd.set_tooltip_text("Capsules on the rig; a UMIK-1 is "
                                      "mono even if it enumerates as "
                                      "stereo")
        self.chan_dd.connect("notify::selected", self._on_chan_changed)
        capsules_row.add_suffix(self.chan_dd)
        self.cal_row = cal_row
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
            spk = Gtk.Button()
            spk.set_size_request(SPEAKER, SPEAKER)
            spk.add_css_class("circular")
            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            body.set_valign(Gtk.Align.CENTER)
            body.append(Gtk.Image.new_from_icon_name(
                "audio-volume-high-symbolic"))
            count = Gtk.Label()
            count.add_css_class("caption")
            count.set_visible(False)
            body.append(count)
            spk.set_child(body)
            spk.connect("clicked", self._make_speaker_cb(i))
            spk.set_tooltip_text("Measure %s" % key)
            self.ring.put(spk, int(x), int(y))
            self._speakers[i] = spk
            self._speaker_counts[i] = count

        center_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        center_box.set_size_request(RING - 2 * SPEAKER, -1)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        top.set_halign(Gtk.Align.CENTER)
        self.vol_label = Gtk.Label()
        top.append(self.vol_label)
        self.relevel_btn = Gtk.Button()
        self.relevel_btn.add_css_class("flat")
        self.relevel_btn.add_css_class("circular")
        self.relevel_btn.set_valign(Gtk.Align.CENTER)
        self.relevel_btn.set_child(Gtk.Image.new_from_icon_name(
            "view-refresh-symbolic"))
        self.relevel_btn.set_tooltip_text(
            "Re-measure the playback level (auto-level the next sweep)")
        self.relevel_btn.connect("clicked", self._on_relevel)
        top.append(self.relevel_btn)
        center_box.append(top)
        self.ring.put(center_box, SPEAKER, int(RING / 2 - 16))
        return self.ring

    def _build_columns(self):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                      homogeneous=True)
        for i, key in enumerate(self.ch_keys):
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            col.add_css_class("card")
            header = Gtk.Label(xalign=0.0)
            header.set_markup("<b>%s</b>" % key)
            col.append(header)
            cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            col.append(cards)
            spread = Gtk.DrawingArea()
            spread.set_content_height(28)
            spread.set_visible(False)
            col.append(spread)
            row.append(col)
            self._columns[i] = {"header": header, "cards": cards,
                                "spread": spread}
        return row

    def _build_fit_area(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        lbl = Gtk.Label(xalign=0.0)
        lbl.set_markup("<b>EQ range</b>  <span size='small'>drag the "
                       "handles; red bars are the take-to-take spread "
                       "(untrustworthy, don't EQ there)</span>")
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
            top = max(3.0, max(spread))
            bw = max(1.0, pw_ / len(freqs))
            for j in range(len(freqs)):
                gx = self._freq_to_x(freqs[j])
                if spread[j] >= 3.0:
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
        else:
            self.fit_hi = min(FMAX_PLOT, max(f, self.fit_lo + 1))
        self.range_area.queue_draw()
        self._update_range_label()

    def _update_range_label(self):
        self.range_label.set_text("Fit %d – %d Hz"
                                  % (round(self.fit_lo), round(self.fit_hi)))

    # ---- drawing ----------------------------------------------------------
    def _draw_disc(self, _area, cr, w, h, *_):
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.16)
        cr.arc(w / 2.0, h / 2.0, min(w, h) / 2.0 - 1, 0, 2 * math.pi)
        cr.fill()

    def _make_curve_draw(self, rec, lo, hi):
        freqs = rec.freq_hz
        mag = rec.mag_db

        def draw(_area, cr, w, h, *_):
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.10)
            cr.rectangle(0, 0, w, h)
            cr.fill()
            span = max(1e-6, hi - lo)
            cr.set_source_rgb(0.22, 0.52, 0.90)
            cr.set_line_width(1.4)
            for j in range(len(freqs)):
                x = _log_x(freqs[j], 2, w - 4)
                y = h - 3 - (float(mag[j]) - lo) / span * (h - 6)
                y = max(1, min(h - 1, y))
                cr.move_to(x, y) if j == 0 else cr.line_to(x, y)
            cr.stroke()
        return draw

    def _make_spread_draw(self, spread, freqs):
        def draw(_area, cr, w, h, *_):
            top = max(1e-6, float(max(spread)) if len(spread) else 1.0)
            for j in range(len(freqs)):
                x = _log_x(freqs[j], 0, w)
                frac = min(1.0, float(spread[j]) / max(top, 3.0))
                over = float(spread[j]) >= 3.0
                if over:
                    cr.set_source_rgba(0.87, 0.19, 0.19, 0.85)
                else:
                    cr.set_source_rgba(0.22, 0.52, 0.90, 0.7)
                bar = frac * h
                cr.rectangle(x, h - bar, max(1, w / len(freqs)), bar)
                cr.fill()
        return draw

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
        if self._sink_gone:
            return                           # keep the Unavailable marker
        if v is None:
            self.vol_label.set_markup("<span size='x-large'>—</span>")
        else:
            self.vol_label.set_markup(
                "<span size='x-large'>%d%%</span>" % round(100 * v))

    def _refresh_volume(self):
        if self.session is not None:
            v = getattr(self.session, "_v_cur", None)
        else:
            src = self._source_name()
            v = (self.memory.volume_for(self.sink_node, src)
                 if src else None)
            if v is None:
                v = self._query_volume()
        self._set_volume_display(v)

    def _on_relevel(self, _btn):
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

    def _clean_count(self, ch):
        if self.session is None:
            return 0
        return sum(1 for r in self.session.takes_of(ch)
                   if ms.take_quality(r) == ms.TAKE_CLEAN)

    def _refresh_all(self):
        ready = self.session is not None
        for i in range(self.n_ch):
            self._rebuild_column(i)
            n = self._clean_count(i)
            has_bad = self.session is not None and any(
                ms.take_quality(r) != ms.TAKE_CLEAN
                for r in self.session.takes_of(i))
            mark = " \u2713" if n >= CLEAN_TARGET else ""
            warn = " \u26a0" if has_bad else ""
            self._columns[i]["header"].set_markup(
                "<b>%s</b>  <span size='small'>%d/%d clean%s</span>%s"
                % (self.ch_keys[i], n, CLEAN_TARGET, mark, warn))
            if n < CLEAN_TARGET:
                ready = False
            spk = self._speakers[i]
            if n >= CLEAN_TARGET:
                spk.add_css_class("suggested-action")
            else:
                spk.remove_css_class("suggested-action")
            total = len(self.session.takes_of(i)) if self.session else 0
            lbl = self._speaker_counts.get(i)
            if lbl is not None:
                lbl.set_text(str(total))
                lbl.set_visible(total > 0)
        self.create_btn.set_sensitive(ready and not self._busy)
        self._refresh_volume()
        if getattr(self, "range_area", None) is not None:
            self.range_area.queue_draw()

    def _rebuild_column(self, ch):
        col = self._columns[ch]
        child = col["cards"].get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            col["cards"].remove(child)
            child = nxt
        takes = self.session.takes_of(ch) if self.session else []
        if takes:
            lo = min(float(min(r.mag_db)) for r in takes) - 1.0
            hi = max(float(max(r.mag_db)) for r in takes) + 1.0
        else:
            lo, hi = -1.0, 1.0
        for rec in takes:
            col["cards"].append(self._make_card(ch, rec, lo, hi))
        spread = self.session.spread_db(ch) if self.session else None
        area = col["spread"]
        if spread is not None:
            area.set_draw_func(self._make_spread_draw(
                spread, takes[0].freq_hz))
            area.set_visible(True)
            area.queue_draw()
        else:
            area.set_visible(False)

    def _make_card(self, ch, rec, lo, hi):
        q = ms.take_quality(rec)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        card.add_css_class("card")
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        dot = Gtk.Label(label="\u25cf")
        dot.add_css_class({ms.TAKE_CLEAN: "success",
                           ms.TAKE_FLAGGED: "warning",
                           ms.TAKE_CLIPPED: "error"}.get(q, "dim-label"))
        head.append(dot)
        head.append(Gtk.Label(label="Take %d" % rec.id, xalign=0.0,
                              hexpand=True))
        rm = Gtk.Button()
        rm.add_css_class("flat")
        rm.set_child(Gtk.Image.new_from_icon_name("user-trash-symbolic"))
        rm.set_tooltip_text("Delete this take")
        rm.connect("clicked", self._make_discard_cb(ch, rec.id))
        head.append(rm)
        card.append(head)

        curve = Gtk.DrawingArea()
        curve.set_content_width(CARD_W)
        curve.set_content_height(CARD_H)
        curve.set_draw_func(self._make_curve_draw(rec, lo, hi))
        card.append(curve)

        if rec.clipped:
            foot = "clipped  %.1f dBFS" % rec.peak_dbfs
        else:
            snr = ("SNR %.0f dB" % rec.snr_db
                   if rec.snr_db is not None else "SNR n/a")
            foot = "%s  %.1f dBFS" % (snr, rec.peak_dbfs)
        flabel = Gtk.Label(label=foot, xalign=0.0)
        flabel.add_css_class("dim-label")
        card.append(flabel)
        return card

    # ---- callbacks (config) -----------------------------------------------
    def _on_pw_state(self, st):
        """The shared PWState refresh drives the whole window: keep the
        input list current, then reconcile the target sink against the
        graph. One pipewire poll feeds this instead of a window timer."""
        self._refresh_sources_from(st.sources)
        self._reconcile_sink(st)
        return False

    def _auto(self):
        """True when the window should chase the default: following is on
        in the main window and the user has not pinned this sink via
        'stay'. Following off behaves as a pinned sink."""
        try:
            return self.parent.follow_btn.get_active() and not self._pinned
        except Exception:
            return False

    def _reconcile_sink(self, st):
        alive = any(s["name"] == self.sink_node for s in st.sinks)
        dflt = st.default_sink
        if not alive:
            if self._auto() and dflt and dflt != self.sink_node:
                self._retarget(dflt)         # follow to the new device
            else:
                self._set_sink_gone(True)    # Unavailable; Create stays
            return
        self._set_sink_gone(False)
        if (self._auto() and dflt and dflt != self.sink_node
                and not self._popup_open and not self._busy):
            self._ask_stay_or_go(dflt)

    def _ask_stay_or_go(self, new_sink):
        self._popup_open = True
        new_desc = next((s["desc"] for s in self._pw.sinks
                         if s["name"] == new_sink), new_sink)
        dlg = Adw.AlertDialog(
            heading="The default output changed",
            body='The system default is now "%s". Switch the measurement '
                 'to it and discard the current takes, or keep measuring '
                 '"%s"?' % (new_desc, self.sink_desc))
        dlg.add_response("stay", "Keep measuring")
        dlg.add_response("go", "Switch")
        dlg.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("stay")
        dlg.set_close_response("stay")

        def on_resp(_d, resp):
            self._popup_open = False
            st = self._pw
            if resp == "go":
                tgt = st.default_sink
                if tgt and any(s["name"] == tgt for s in st.sinks):
                    self._retarget(tgt)
                elif not any(s["name"] == self.sink_node
                             for s in st.sinks):
                    self._set_sink_gone(True)   # nowhere to go, old dead
            else:
                self._pinned = True             # stop asking
                self._reconcile_sink(st)        # reflect reality now
        dlg.connect("response", on_resp)
        dlg.present(self)

    def _retarget(self, new_sink):
        """Abandon this measurement and reopen the wizard for new_sink -- a
        different channel layout means a fresh measurement. The main window
        moves too, and the new window is shown before this one closes so
        there is no blank flash."""
        self.parent._retarget_measure(new_sink)

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
        if gone:
            self.vol_label.set_markup(
                "<span size='large'>Unavailable</span>")
            self.warning.set_text(
                "The output device is gone -- its channel configuration "
                "changed, or it was unplugged. Bring it back to keep "
                "measuring; you can still save what you have measured.")
            self._set_ring_sensitive(False)
            self.relevel_btn.set_sensitive(False)
        else:
            self.warning.set_text("")
            self.relevel_btn.set_sensitive(True)
            self._set_ring_sensitive(True)
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
            self.cal_row.remove(btn)
        self.cal_btns = {}
        labels = self._mic_labels()
        for i in range(self.mic_ch):
            btn = Gtk.Button(label="%s cal…" % labels[i])
            btn.set_valign(Gtk.Align.CENTER)
            btn.set_tooltip_text("Calibration for the rig's %s capture "
                                 "channel; its RAW/HEQ/IDF/HPN domain is "
                                 "the compensation" % labels[i])
            btn.connect("clicked", self._make_cal_cb(i))
            self.cal_row.add_suffix(btn)
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
                col.append(Gtk.Label(label=self.ch_keys[k]))
                dd = Gtk.DropDown.new_from_strings(["L", "R"])
                dd.set_selected(self.mic_of.get(k, k))
                dd.set_tooltip_text("Which rig channel captures %s"
                                    % self.ch_keys[k])
                dd.connect("notify::selected", self._make_map_cb(k))
                col.append(dd)
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
            if self.session is not None and not self._busy:
                try:
                    self.session.discard(ch, take_id)
                except ms.MeasureError:
                    return
                self._refresh_all()
        return cb

    # ---- measurement ------------------------------------------------------
    def _make_speaker_cb(self, ch):
        def cb(_btn):
            self._start_measure(ch)
        return cb

    def _ensure_session(self):
        if self.session is not None:
            return True
        if not self._sink_present():
            self._error(
                "This output no longer exists -- the device's channel "
                "configuration changed and renamed its sink. Close and "
                "reopen the measurement for the current device.")
            return False
        src = self._selected_source()
        if not src:
            self._error("Pick a measurement mic first.")
            return False
        remembered = self.memory.volume_for(self.sink_node, src["name"])
        use_auto = remembered is None or self._relevel_pending
        cfg = ms.SessionConfig(
            sink=self.sink_node, source=src["name"], channels=self.mic_ch,
            auto_level=use_auto, mute_others=True, device=self.sink_desc,
            start_volume=(None if use_auto else remembered))
        self._relevel_pending = False
        try:
            self.session = ms.MeasureSession(cfg)
            self.session.__enter__()
            self._entered = True
        except ms.RefusalError as e:
            self.session = None
            self._error(str(e))
            return False
        except Exception as e:                       # missing tools, etc.
            self.session = None
            self._error("Could not start: %s" % e)
            return False
        return True

    def _start_measure(self, ch):
        if self._busy:
            return
        if not self._loud_ack:
            self._confirm_loud(lambda: self._start_measure(ch))
            return
        if not self._ensure_session():
            return
        self._busy = True
        self._set_ring_sensitive(False)
        self.create_btn.set_sensitive(False)
        self.center.set_text("Measuring %s\u2026" % self.ch_keys[ch])
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
                    self._post_status(
                        "%s: leveling %d%% → %d%%  "
                        "(peak %.1f dBFS, step %d/%d)"
                        % (self.ch_keys[ch],
                           round(100 * lv.get("volume_from", 0)),
                           round(100 * lv.get("volume_to", 0)),
                           lv.get("peak_dbfs", float("nan")),
                           lv.get("step", 0), lv.get("max_steps", 0)))
                    GLib.idle_add(self._set_volume_display,
                                  lv.get("volume_to"))
                    continue
                if out.kind == "level_stuck":
                    lv = out.level or {}
                    self._post_status(
                        "%s: level stuck at %d%% (peak %.1f dBFS), "
                        "keeping it" % (self.ch_keys[ch],
                                        round(100 * lv.get("volume", 0)),
                                        lv.get("peak_dbfs", float("nan"))))
                    out = self.session.accept_level()
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
        if result["error"] is not None:
            self._error("Measurement failed: %s" % result["error"])
            self._refresh_all()
            return False
        v = getattr(self.session, "_v_cur", self.session.volume_start)
        src = self._source_name()
        if v is not None and src:
            self.memory.remember(self.sink_node, source=src, volume=v)
        self._refresh_all()
        return False

    def _set_ring_sensitive(self, on):
        for spk in self._speakers.values():
            spk.set_sensitive(on)

    # ---- create profile ---------------------------------------------------
    def _on_create(self, _btn):
        if self.session is None or self._busy:
            return
        name = self._profile_name()
        channels = {i: self.ch_keys[i] for i in range(self.n_ch)
                    if self.session.takes_of(i)}
        cal = {}
        for i in channels:
            path = self.cal.get(self.mic_of.get(i, 0))
            if path:
                cal[i] = path
        bands = self.bands_spin.get_value_as_int()
        f_lo = float(self.fit_lo)
        f_hi = float(self.fit_hi)
        self._busy = True
        self.create_btn.set_sensitive(False)
        self._set_ring_sensitive(False)
        self.center.set_text("Building profile…")
        res = {"pid": None, "error": None}

        def work():
            try:
                res["pid"] = measure_build.build_and_bind(
                    self.session, channels, self.parent.store,
                    self.sink_node, name, cal=cal, bands=bands,
                    f_lo=f_lo, f_hi=f_hi)
            except Exception as e:
                res["error"] = e
            GLib.idle_add(self._create_done, res)
        threading.Thread(target=work, daemon=True).start()

    def _create_done(self, res):
        self._busy = False
        self._set_ring_sensitive(True)
        self.center.set_text("Click a speaker to measure")
        if res["error"] is not None:
            self._error("Could not build the profile: %s" % res["error"])
            self._refresh_all()
            return False
        self._persist_mic()
        pid = res["pid"]
        try:
            self.parent._select_device(self.sink_node, load=False)
            self.parent._load_profile(pid)
        except Exception:
            pass
        self.close()
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
                "serial": (existing or {}).get("serial", ""),
                "cal": cal, "channels": self.mic_ch}
        if existing:
            body["id"] = existing["id"]
        pid = self.mic_store.save(body)
        self.memory.remember(self.sink_node, mic_profile=pid)

    def _profile_name(self):
        return "Measured %s" % self.sink_desc

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

    def _on_close(self, *_):
        if getattr(self, "_pw_unsub", None) is not None:
            self._pw_unsub()
            self._pw_unsub = None
        if self.session is not None and self._entered:
            try:
                self.session.__exit__(None, None, None)
            except Exception:
                pass
            self._entered = False
        return False
