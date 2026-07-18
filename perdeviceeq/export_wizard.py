# -*- coding: utf-8 -*-
"""The export wizard: the one window-level action that bakes the
composite chain (device profile + active taste layer, one shared
preamp) for an external equalizer picked from the registry.

Two pages on an Adw.NavigationView:
  1. the registry, grouped by how a target consumes EQ (files it
     imports vs values keyed in by hand), under a plain statement of
     what exactly is being exported -- the wizard's one question is
     "where is this going?";
  2. the target page: the collapse choice when the profile is
     per-channel, the verification line (a parametric null test, the
     rounding cost, or the fixed-band fit residual -- the fixed path
     also draws the residual curve before anything leaves the app),
     a monospace preview, then Copy / Save.

All math lives in export_peq (pure, tested); this module is GTK
plumbing only and stays out of the test run like gui.py.
"""

import math

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, GLib, Adw

from . import export_peq as xp

_NULL_N = 480          # null-test grid density over the fit band
_PLOT_N = 240          # fixed-band fit / residual plot grid
_GRID_FREQS = (20, 50, 100, 200, 500, 1000,
               2000, 5000, 10000, 20000)


def _flabel(f):
    return "%gk" % (f / 1000.0) if f >= 1000 else "%g" % f


class ExportDialog(Adw.Dialog):
    """The two-page export wizard over the current editor state."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.set_title("Export EQ")
        self.set_content_width(600)
        self.set_content_height(680)
        self.body = win._working_body()
        layer = win.pref_layers.active()
        self.taste_name = layer["name"] if layer else None
        self.chains = xp.composed_chains(
            self.body, win.pref_layers.active_bands())
        self.flo, self.fhi = xp.fit_band(self.body)
        self.nav = Adw.NavigationView()
        self.nav.add(self._targets_page())
        self.set_child(self.nav)

    # ---- shared wording -------------------------------------------

    def _chain_summary(self):
        """One sentence stating what gets baked: profile, taste
        layer if one is on, chain count, the shared preamp. Shown on
        both pages and written into every export header. Phrased so
        no line ever matches the AutoEq "Preamp:" shape."""
        s = "Profile \u201c%s\u201d" % self.body.get("name", "?")
        if self.taste_name:
            s += " + taste layer \u201c%s\u201d" % self.taste_name
        n = len(self.chains)
        ch = "one chain" if n == 1 else "%d channel chains" % n
        return ("%s -- %s, shared preamp %+.1f dB."
                % (s, ch, float(self.body.get("preamp", 0.0))))

    def _band_str(self):
        return "%g-%g Hz" % (self.flo, self.fhi)

    def _header(self, target, note):
        from . import __version__
        return ["Exported by per-device-eq %s for %s"
                % (__version__, target["name"]),
                "Source: " + self._chain_summary(),
                "Chain collapse: %s." % note]

    # ---- page 1: where is this going? -----------------------------

    def _targets_page(self):
        page = Adw.PreferencesPage()
        intro = Adw.PreferencesGroup()
        intro.set_description(self._chain_summary())
        page.add(intro)
        targets = xp.load_targets()
        groups = (("Import files",
                   "Formats the target application reads in",
                   ("parametric", "graphiceq")),
                  ("Set by hand",
                   "Values you key into the target yourself",
                   ("fixed", "sheet")))
        for title, desc, writers in groups:
            grp = Adw.PreferencesGroup(title=title, description=desc)
            got = False
            for t in targets:
                if t.get("writer") not in writers:
                    continue
                row = Adw.ActionRow(title=t["name"],
                                    subtitle=t.get("note", ""))
                row.set_use_markup(False)
                row.set_activatable(True)
                row.add_suffix(Gtk.Image.new_from_icon_name(
                    "go-next-symbolic"))
                row.connect("activated", self._on_target, t)
                grp.add(row)
                got = True
            if got:
                page.add(grp)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        tv.set_content(page)
        return Adw.NavigationPage(title="Export EQ", child=tv)

    def _on_target(self, _row, target):
        self.nav.push(self._target_page(target))

    # ---- page 2: one target ---------------------------------------

    @staticmethod
    def _policy_label(policy):
        if policy == "all":
            return "Single chain (apply-all)"
        if policy == "mean":
            return "Mean of all channels"
        return "Channel %s" % policy

    def _target_page(self, target):
        band_domain = target["writer"] in ("parametric", "sheet")
        choices = xp.collapse_choices(self.chains, band_domain)
        st = {"target": target, "policy": choices[0]}
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                      spacing=12, margin_top=12, margin_bottom=12,
                      margin_start=12, margin_end=12)
        head = Gtk.Label(xalign=0, wrap=True)
        head.add_css_class("dim-label")
        head.set_text(self._chain_summary())
        box.append(head)
        combo = None
        if len(choices) > 1:
            names = Gtk.StringList()
            for c in choices:
                names.append(self._policy_label(c))
            combo = Adw.ComboRow(title="Export which chain",
                                 model=names)
            grp = Adw.PreferencesGroup()
            grp.add(combo)
            box.append(grp)
        st["status"] = Gtk.Label(xalign=0, wrap=True)
        box.append(st["status"])
        if target["writer"] == "fixed":
            area = Gtk.DrawingArea(content_height=170)
            area.set_draw_func(self._draw_resid, st)
            st["resid"] = area
            box.append(area)
        view = Gtk.TextView(editable=False, monospace=True,
                            cursor_visible=False)
        view.set_wrap_mode(Gtk.WrapMode.NONE)
        for setter in (view.set_left_margin, view.set_right_margin,
                       view.set_top_margin, view.set_bottom_margin):
            setter(8)
        st["view"] = view
        sw = Gtk.ScrolledWindow(min_content_height=180, vexpand=True)
        sw.add_css_class("card")
        sw.set_child(view)
        box.append(sw)
        btns = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        copy_btn = Gtk.Button(label="Copy")
        copy_btn.connect("clicked", lambda *_: self._copy(st))
        save_btn = Gtk.Button(label="Save\u2026")
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", lambda *_: self._save(st))
        btns.append(copy_btn)
        btns.append(save_btn)
        box.append(btns)
        if combo is not None:
            def on_sel(row, _p):
                st["policy"] = choices[row.get_selected()]
                self._bake(st)
            combo.connect("notify::selected", on_sel)
        self._bake(st)
        outer = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER)
        outer.set_child(Adw.Clamp(child=box, maximum_size=760))
        st["overlay"] = Adw.ToastOverlay()
        st["overlay"].set_child(outer)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        tv.set_content(st["overlay"])
        return Adw.NavigationPage(title=target["name"], child=tv)

    # ---- baking + verification --------------------------------------

    def _null_line(self, err):
        verdict = ("pass" if err <= xp.NULL_PASS_DB else
                   "CHECK, above the %.1f dB ceiling"
                   % xp.NULL_PASS_DB)
        return ("Null test vs the in-app chain: max %.2f dB over %s"
                " -- %s." % (err, self._band_str(), verdict))

    @staticmethod
    def _set_status(st, line, ok):
        lab = st["status"]
        for c in ("success", "warning"):
            lab.remove_css_class(c)
        if ok is True:
            lab.add_css_class("success")
        elif ok is False:
            lab.add_css_class("warning")
        lab.set_text(line)

    def _bake(self, st):
        """Render the export text for the current collapse policy,
        run its verification and refresh the page. Every branch
        states its truth: the null test, the rounding cost, or the
        fit residual -- nothing is copied or saved unverified."""
        t = st["target"]
        writer = t["writer"]
        nf = xp.log_grid(self.flo, self.fhi, _NULL_N)
        if writer in ("parametric", "sheet"):
            g, bands, note = xp.pick_chain(self.chains, st["policy"])
            fg, fbands, folded = xp.fold_flat(g, bands)
            hdr = self._header(t, note)
            if folded:
                hdr.append("Flat-gain trim folded into the shared"
                           " gain (%+.1f dB)." % folded)
            ref = xp.chain_response(g, bands, nf)
            if writer == "parametric":
                text = xp.parametric_text(fg, fbands, header=hdr)
                err = xp.null_test_parametric(text, nf, ref)
                self._set_status(st, self._null_line(err),
                                 err <= xp.NULL_PASS_DB)
            else:
                text = xp.sheet_text(t, fg, fbands, header=hdr)
                rp, rb = xp.rounded_chain(t, fg, fbands)
                got = xp.chain_response(rp, rb, nf)
                err = max(abs(a - b) for a, b in zip(got, ref))
                self._set_status(
                    st, "Rounding to the target's steps costs max "
                    "%.2f dB over %s." % (err, self._band_str()),
                    err <= xp.NULL_PASS_DB)
        elif writer == "graphiceq":
            grid = xp.graphic_grid()
            resp, note = xp.collapse(self.chains, st["policy"], grid)
            hdr = ([] if t.get("bare")
                   else self._header(t, note))
            text, shift = xp.graphiceq_text(grid, resp, header=hdr)
            ref, _n = xp.collapse(self.chains, st["policy"], nf)
            err = xp.null_test_graphic(text, nf, ref, shift)
            line = self._null_line(err)
            if shift:
                line += " Level shifted %+.1f dB." % shift
            self._set_status(st, line, err <= xp.NULL_PASS_DB)
        else:                                   # fixed
            pf = xp.log_grid(self.flo, self.fhi, _PLOT_N)
            desired, note = xp.collapse(self.chains, st["policy"],
                                        pf)
            sol = xp.solve_fixed(t, pf, desired)
            st["sol"] = sol
            text = xp.fixed_sheet_text(t, sol,
                                       header=self._header(t, note))
            self._set_status(
                st, "Fit over %s: residual max %.1f dB, rms %.1f dB"
                " across %s; level trim %+.1f dB."
                % (sol["basis"], sol["resid_max"], sol["resid_rms"],
                   self._band_str(), sol["offset"]), None)
            st["resid"].queue_draw()
        st["text"] = text
        st["view"].get_buffer().set_text(text)

    # ---- the residual plot (fixed-band targets) ---------------------

    def _draw_resid(self, _area, cr, w, h, st):
        sol = st.get("sol")
        if not sol:
            return
        cr.set_source_rgb(0.12, 0.12, 0.14)
        cr.paint()
        pad_l, pad_r, pad_t, pad_b = 34, 8, 8, 18
        pw, ph = w - pad_l - pad_r, h - pad_t - pad_b
        if pw <= 10 or ph <= 10:
            return
        cr.rectangle(pad_l, pad_t, pw, ph)
        cr.set_source_rgb(0.08, 0.08, 0.10)
        cr.fill()
        fs = sol["freqs"]
        la, lb = math.log10(fs[0]), math.log10(fs[-1])

        def fx(f):
            return pad_l + (math.log10(f) - la) / (lb - la) * pw

        m = 0.0
        for key in ("desired", "achieved", "resid"):
            m = max(m, max(abs(v) for v in sol[key]))
        m = max(3.0, math.ceil(m))

        def fy(db):
            return pad_t + (m - db) / (2.0 * m) * ph

        cr.set_line_width(1)
        cr.set_font_size(10)
        for f in _GRID_FREQS:
            if not fs[0] <= f <= fs[-1]:
                continue
            x = fx(f)
            cr.set_source_rgba(1, 1, 1, 0.10)
            cr.move_to(x, pad_t)
            cr.line_to(x, pad_t + ph)
            cr.stroke()
            cr.set_source_rgba(1, 1, 1, 0.45)
            cr.move_to(x - 6, h - 5)
            cr.show_text(_flabel(f))
        for db in (-m, -m / 2.0, 0.0, m / 2.0, m):
            y = fy(db)
            zero = abs(db) < 1e-9
            cr.set_source_rgba(1, 1, 1, 0.16 if zero else 0.08)
            cr.move_to(pad_l, y)
            cr.line_to(pad_l + pw, y)
            cr.stroke()
            cr.set_source_rgba(1, 1, 1, 0.45)
            cr.move_to(2, y + 3)
            cr.show_text("0" if zero else "%+g" % db)
        curves = (("desired", "desired", (0.85, 0.85, 0.90, 0.55)),
                  ("achieved", "achieved", (0.30, 0.78, 1.0, 0.90)),
                  ("resid", "residual", (0.45, 0.95, 0.55, 0.90)))
        for key, _n, rgba in curves:
            cr.set_source_rgba(*rgba)
            first = True
            for f, v in zip(fs, sol[key]):
                if first:
                    cr.move_to(fx(f), fy(v))
                    first = False
                else:
                    cr.line_to(fx(f), fy(v))
            cr.stroke()
        x = pad_l + 8
        for _k, name, rgba in curves:
            cr.set_source_rgba(*rgba)
            cr.move_to(x, pad_t + 12)
            cr.show_text(name)
            x += cr.text_extents(name).width + 14

    # ---- copy / save -------------------------------------------------

    def _toast(self, st, msg):
        st["overlay"].add_toast(Adw.Toast.new(msg))

    def _copy(self, st):
        self.win.get_clipboard().set(st["text"])
        self._toast(st, "Copied to clipboard")

    def _save(self, st):
        t = st["target"]
        dlg = Gtk.FileDialog()
        dlg.set_title("Export EQ")
        stem = self.win._safe_filename(
            "%s - %s" % (self.body.get("name", "profile"),
                         t["name"]))
        dlg.set_initial_name(stem + t.get("ext", ".txt"))

        def done(d, res):
            try:
                gfile = d.save_finish(res)
            except GLib.Error:
                return
            path = gfile.get_path() if gfile else None
            if not path:
                return
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(st["text"])
            except OSError as e:
                self._toast(st, "Could not save: %s" % e)
                return
            self.close()
        dlg.save(self.win, None, done)
