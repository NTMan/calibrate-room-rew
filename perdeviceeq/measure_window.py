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

from . import pipewire, measure_build               # noqa: E402
from . import measure_session as ms                 # noqa: E402
from . import measure_prefs                         # noqa: E402

CARD_W, CARD_H = 150, 60
RING = 240
SPEAKER = 56
CLEAN_TARGET = 3            # clean takes per channel before "all clean"
FIT_BANDS = 12
FIT_FLO, FIT_FHI = 20.0, 12000.0
FMIN_PLOT, FMAX_PLOT = 20.0, 20000.0


def _log_x(freq, x0, w):
    """x pixel for a frequency on a log axis spanning FMIN..FMAX_PLOT."""
    lo, hi = math.log10(FMIN_PLOT), math.log10(FMAX_PLOT)
    f = min(max(float(freq), FMIN_PLOT), FMAX_PLOT)
    return x0 + (math.log10(f) - lo) / (hi - lo) * w


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
        self.cal = {}               # capture-channel index -> cal path
        self.session = None         # created on first measure
        self._entered = False
        self._busy = False
        self._loud_ack = False
        self._columns = {}          # ch index -> {box, header, ...}
        self._speakers = {}         # ch index -> Gtk.Button

        self._build_ui()
        self.connect("close-request", self._on_close)
        self._prefill_from_memory()
        self._refresh_all()

    # ---- layout -----------------------------------------------------------
    def _build_ui(self):
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        title = Adw.WindowTitle(title="Measure speakers",
                                subtitle=self.sink_desc)
        hb.set_title_widget(title)
        tv.add_top_bar(hb)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_top(12)
        outer.set_margin_bottom(12)
        outer.set_margin_start(12)
        outer.set_margin_end(12)

        outer.append(self._build_source_area())
        outer.append(self._build_ring())
        self.warning = Gtk.Label(xalign=0.0)
        self.warning.add_css_class("dim-label")
        self.warning.set_wrap(True)
        outer.append(self.warning)
        outer.append(self._build_columns())

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.level_label = Gtk.Label(xalign=0.0)
        self.level_label.add_css_class("dim-label")
        self.level_label.set_hexpand(True)
        footer.append(self.level_label)
        self.create_btn = Gtk.Button(label="Create profile")
        self.create_btn.add_css_class("suggested-action")
        self.create_btn.set_sensitive(False)
        self.create_btn.connect("clicked", self._on_create)
        footer.append(self.create_btn)
        outer.append(footer)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(outer)
        scroller.set_vexpand(True)
        tv.set_content(scroller)
        self.set_content(tv)

    def _build_source_area(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.append(Gtk.Label(label="Measurement mic", xalign=0.0))
        names = [s["desc"] for s in self.sources] or ["(no sources found)"]
        self.source_dd = Gtk.DropDown.new_from_strings(names)
        self.source_dd.set_hexpand(True)
        self.source_dd.connect("notify::selected", self._on_source_changed)
        row.append(self.source_dd)
        box.append(row)

        cal_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        cal_row.append(Gtk.Label(label="Calibration", xalign=0.0))
        self.cal_btns = {}
        for i, key in enumerate(self.ch_keys):
            btn = Gtk.Button(label="%s cal…" % key)
            btn.set_tooltip_text("The cal file's domain (RAW/HEQ/IDF/HPN) "
                                 "is the compensation")
            btn.connect("clicked", self._make_cal_cb(i))
            cal_row.append(btn)
            self.cal_btns[i] = btn
        box.append(cal_row)

        fit = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        fit.append(Gtk.Label(label="Fit up to", xalign=0.0))
        self.bands_spin = Gtk.SpinButton.new_with_range(1, 20, 1)
        self.bands_spin.set_value(FIT_BANDS)
        self.bands_spin.set_tooltip_text("Max biquads per channel; the fit "
                                         "stops early once the worst "
                                         "residual is under ~0.5 dB")
        fit.append(self.bands_spin)
        fit.append(Gtk.Label(label="bands, over"))
        self.flo_spin = Gtk.SpinButton.new_with_range(20, 1000, 5)
        self.flo_spin.set_value(FIT_FLO)
        fit.append(self.flo_spin)
        fit.append(Gtk.Label(label="–"))
        self.fhi_spin = Gtk.SpinButton.new_with_range(1000, 20000, 100)
        self.fhi_spin.set_value(FIT_FHI)
        self.fhi_spin.set_tooltip_text(
            "Upper fit edge. Above the cal-trust limit correct only with a "
            "broad shelf, the same L/R.")
        fit.append(self.fhi_spin)
        fit.append(Gtk.Label(label="Hz"))
        box.append(fit)
        return box

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
            ang = math.pi + (2 * math.pi * i / max(1, self.n_ch))
            if self.n_ch == 2:                      # L left, R right
                ang = math.pi if i == 0 else 0.0
            x = cx + r * math.cos(ang) - SPEAKER / 2.0
            y = cy + r * math.sin(ang) - SPEAKER / 2.0
            spk = Gtk.Button()
            spk.set_size_request(SPEAKER, SPEAKER)
            spk.add_css_class("circular")
            spk.set_child(Gtk.Image.new_from_icon_name(
                "audio-volume-high-symbolic"))
            spk.connect("clicked", self._make_speaker_cb(i))
            spk.set_tooltip_text("Measure %s" % key)
            self.ring.put(spk, int(x), int(y))
            self._speakers[i] = spk

        self.center = Gtk.Label(label="Click a speaker to measure")
        self.center.add_css_class("dim-label")
        self.center.set_size_request(RING - 2 * SPEAKER, -1)
        self.center.set_wrap(True)
        self.center.set_justify(Gtk.Justification.CENTER)
        self.ring.put(self.center, SPEAKER, int(RING / 2 - 12))
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
            for i in range(self.n_ch):
                path = prof.get("cal", {}).get(str(i))
                if path:
                    self.cal[i] = path
        self._sync_cal_labels()

    def _sync_cal_labels(self):
        for i, key in enumerate(self.ch_keys):
            path = self.cal.get(i)
            self.cal_btns[i].set_label(
                os.path.basename(path) if path else "%s cal…" % key)

    def _selected_source(self):
        i = self.source_dd.get_selected()
        if 0 <= i < len(self.sources):
            return self.sources[i]
        return None

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
        self.create_btn.set_sensitive(ready and not self._busy)
        v = self.memory.volume_for(self.sink_node)
        if v is not None:
            self.level_label.set_text("Level %d%% (remembered)"
                                      % round(100 * v))

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
    def _on_source_changed(self, *_):
        src = self._selected_source()
        if not src:
            return
        prof = self.mic_store.match(src["name"])
        self.cal = {}
        if prof:
            for i in range(self.n_ch):
                path = prof.get("cal", {}).get(str(i))
                if path:
                    self.cal[i] = path
        self._sync_cal_labels()
        self._persist_mic()

    def _make_cal_cb(self, ch):
        def cb(_btn):
            dialog = Gtk.FileDialog()
            dialog.set_title("Choose cal for %s" % self.ch_keys[ch])

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
        src = self._selected_source()
        if not src:
            self._error("Pick a measurement mic first.")
            return False
        cfg = ms.SessionConfig(
            sink=self.sink_node, source=src["name"], channels=self.n_ch,
            auto_level=True, mute_others=True,
            device=self.sink_desc)
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
                out = self.session.take(ch)
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
        if v is not None:
            self.memory.remember(self.sink_node, volume=v)
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
        cal = dict(self.cal)
        bands = self.bands_spin.get_value_as_int()
        f_lo = float(self.flo_spin.get_value())
        f_hi = float(self.fhi_spin.get_value())
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
        """Save the chosen mic + cal (bound to the source node) and
        remember it for this sink as soon as either changes -- not only at
        create. The compensation is read back from the cal filenames."""
        src = self._selected_source()
        if not src:
            return
        existing = self.mic_store.match(src["name"])
        cal = {str(i): p for i, p in self.cal.items() if p}
        if not cal and existing is None:
            return
        body = {"name": src["desc"], "node_match": src["name"],
                "serial": (existing or {}).get("serial", ""),
                "cal": cal}
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
        if self.session is not None and self._entered:
            try:
                self.session.__exit__(None, None, None)
            except Exception:
                pass
            self._entered = False
        return False
