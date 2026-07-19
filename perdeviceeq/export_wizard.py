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

Response-domain writers and the band-domain re-fits (the mean
policy, a target with a band budget) feed from the measurement
canvas when the fit provenance allows -- export_source: a fit that
exists, was not hand-edited and still matches its canvas -- and
from the playback chain otherwise. Every artifact header and every
status line names its source.

All math lives in export_peq (pure, tested); this module is GTK
plumbing only and stays out of the test run like gui.py.
"""

import math
import threading

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
        self.taste_bands = win.pref_layers.active_bands()
        self.chains = xp.composed_chains(self.body,
                                         self.taste_bands)
        self.chains_plain = (xp.composed_chains(self.body, None)
                             if self.taste_name else self.chains)
        self.flo, self.fhi = xp.fit_band(self.body)
        self.source, self.source_why = xp.export_source(self.body)
        self._canvas = None
        self.nav = Adw.NavigationView()
        self.nav.add(self._targets_page())
        self.set_child(self.nav)

    # ---- shared wording -------------------------------------------

    def _chain_summary(self, taste=True):
        """One sentence describing the SOURCE: profile, taste layer
        when included, chain count. No level figure: the profile
        preamp is not what an artifact carries (headroom lowers it,
        a graphic line has none at all), so each writer states its
        own effective level in the status instead. Phrased so no
        line ever matches the AutoEq "Preamp:" shape."""
        s = "Profile \u201c%s\u201d" % self.body.get("name", "?")
        if self.taste_name and taste:
            s += " + taste layer \u201c%s\u201d" % self.taste_name
        n = len(self.chains)
        ch = "one chain" if n == 1 else "%d channel chains" % n
        return "%s -- %s." % (s, ch)

    def _band_str(self):
        return "%g-%g Hz" % (self.flo, self.fhi)

    def _header(self, target, note, taste=True):
        from . import __version__
        return ["Exported by per-device-eq %s for %s"
                % (__version__, target["name"]),
                "Source: " + self._chain_summary(taste),
                "Chain collapse: %s." % note]

    def _source_line(self, canvas_note=None):
        """The artifact-header statement of what fed the export."""
        if self.source == "measurement":
            s = ("Fed from: the measurement canvas -- the desired"
                 " correction the fit was asked for")
            if canvas_note:
                s += " (%s)" % canvas_note
            return s + "."
        return ("Fed from: the playback chain (%s)."
                % self.source_why)

    def _collapse_note(self, policy):
        """The collapse wording for a canvas-fed bake, matching
        what collapse()/pick_chain() would say for the chain."""
        keys = [k for k, _g, _b in self.chains]
        if policy == "mean":
            return "mean of %s" % ", ".join(keys)
        if policy == "all":
            return "single chain (apply-all)"
        return "channel %s of %s" % (policy, ", ".join(keys))

    def _canvas_desired(self):
        """(fg, curves, note) rebuilt once, taste-free; a canvas
        that refuses flips the whole dialog to the chain source."""
        if self._canvas is None:
            from .refit import RefitError
            try:
                self._canvas = xp.desired_from_canvas(self.body)
            except RefitError as e:
                self.source = "chain"
                self.source_why = str(e)
                self._canvas = False
        return self._canvas or None

    def _boost_cap(self):
        return float(((self.body.get("fit") or {})
                      .get("params") or {}).get("max_boost", 6.0))

    def _measurement_vals(self, st, taste, cap=None):
        """(fg, vals, canvas_note, capped_by) for the current
        policy under the measurement source, or None -> bake from
        the chain. Order matters: the policy collapses the raw
        desired first; `cap`, when given, then clips the boost at
        the fit's own ceiling -- deep dips are not filled, the
        same doctrine the band fits obey -- and capped_by reports
        how far the ask exceeded it; the taste overlay lands LAST,
        uncapped, because a taste layer is intent, not a
        measurement artifact."""
        if self.source != "measurement":
            return None
        got = self._canvas_desired()
        if not got:
            return None
        fg, curves, note = got
        pol = st["policy"]
        if pol == "mean":
            vals = xp.mean_curve(curves)
        elif pol in curves:
            vals = list(curves[pol])
        elif len(curves) == 1:
            vals = list(next(iter(curves.values())))
        else:
            self.source = "chain"
            self.source_why = ("the canvas channels do not match"
                               " the chain keys")
            return None
        capped_by = 0.0
        if cap is not None:
            over = max(vals) - cap
            if over > 0:
                capped_by = over
                vals = [min(v, cap) for v in vals]
        if taste and self.taste_bands:
            tail = xp.chain_response(0.0, self.taste_bands, fg)
            vals = [v + d for v, d in zip(vals, tail)]
        return fg, vals, note, capped_by

    # ---- page 1: where is this going? -----------------------------

    def _targets_page(self):
        page = Adw.PreferencesPage()
        intro = Adw.PreferencesGroup()
        desc = self._chain_summary(taste=False)
        if self.taste_name:
            desc += ("\nTaste layer \u201c%s\u201d is active;"
                     " every target page carries the include"
                     " switch, on by default."
                     % self.taste_name)
        intro.set_description(desc)
        page.add(intro)
        targets = xp.load_targets()
        audit = {t["id"]: xp.audit_target(
            t, self.chains, xp.log_grid(self.flo, self.fhi, 240))
            for t in targets}
        dark = Adw.StyleManager.get_default().get_dark()
        red = "#ff7b63" if dark else "#c01c28"
        groups = (("Import files",
                   "Formats the target application reads in",
                   xp.FILE_WRITERS),
                  ("Set by hand",
                   "Values you key into the target yourself",
                   xp.HAND_WRITERS))
        for title, desc, writers in groups:
            grp = Adw.PreferencesGroup(title=title, description=desc)
            got = False
            rows = [t for t in targets
                    if t.get("writer") in writers]
            rows.sort(key=lambda t: audit[t["id"]][0])
            for t in rows:
                _score, tflag, gaps = audit[t["id"]]
                sub = GLib.markup_escape_text(t.get("note", ""))
                if gaps:
                    line = GLib.markup_escape_text(
                        ", ".join(gaps))
                    if tflag:
                        line = ("<span foreground='%s'>%s:"
                                " %s</span>"
                                % (red, tflag, line))
                    sub = ((sub + "\n") if sub else "") + line
                row = Adw.ActionRow(title=GLib.markup_escape_text(
                    t["name"]), subtitle=sub)
                row.set_use_markup(True)
                row.set_activatable(True)
                if tflag:
                    flag = Gtk.Label(label=tflag)
                    flag.add_css_class("error")
                    row.add_suffix(flag)
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
        if policy == "stereo":
            return "True stereo (per-band channels)"
        if policy == "all":
            return "Single chain (apply-all)"
        if policy == "mean":
            return "Mean of all channels"
        return "Channel %s" % policy

    def _target_page(self, target):
        stereo = (target["writer"] == "poweramp"
                  and xp.poweramp_stereo_keys(self.chains))
        band_domain = target["writer"] in ("parametric", "sheet",
                                           "poweramp")
        choices = (["stereo"] if stereo
                   else xp.collapse_choices(self.chains,
                                            band_domain))
        if target["writer"] == "poweramp":
            choices = [c for c in choices if c != "mean"]
        st = {"target": target, "policy": choices[0]}
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                      spacing=12, margin_top=12, margin_bottom=12,
                      margin_start=12, margin_end=12)
        head = Gtk.Label(xalign=0, wrap=True)
        head.add_css_class("dim-label")
        head.set_text(self._chain_summary(taste=False))
        box.append(head)
        lim = xp.limits_text(target)
        if lim:
            ll = Gtk.Label(xalign=0, wrap=True)
            ll.add_css_class("dim-label")
            ll.set_text("Target limits: " + lim)
            box.append(ll)
        st["taste"] = True
        rows = Adw.PreferencesGroup()
        got_rows = False
        if self.taste_name:
            sw = Adw.SwitchRow(
                title="Include taste layer \u201c%s\u201d"
                      % self.taste_name, active=True)

            def on_taste(row, _p):
                st["taste"] = row.get_active()
                self._bake(st)
            sw.connect("notify::active", on_taste)
            rows.add(sw)
            got_rows = True
        combo = None
        if len(choices) > 1:
            names = Gtk.StringList()
            for c in choices:
                names.append(self._policy_label(c))
            combo = Adw.ComboRow(title="Export which chain",
                                 model=names)
            rows.add(combo)
            got_rows = True
        if got_rows:
            box.append(rows)
        st["prog"] = Gtk.ProgressBar(visible=False)
        box.append(st["prog"])
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
        st["copy"] = copy_btn
        st["save"] = save_btn
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

    def _null_line(self, err, against="the in-app chain"):
        verdict = ("pass" if err <= xp.NULL_PASS_DB else
                   "CHECK, above the %.1f dB ceiling"
                   % xp.NULL_PASS_DB)
        return ("Null test vs %s: max %.2f dB over %s"
                " -- %s." % (against, err, self._band_str(),
                             verdict))

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
        st["gen"] = st.get("gen", 0) + 1
        for k in ("copy", "save"):
            if k in st:
                st[k].set_sensitive(True)
        if "prog" in st:
            st["prog"].set_visible(False)
        t = st["target"]
        writer = t["writer"]
        taste = st.get("taste", True)
        allc = self.chains if taste else self.chains_plain
        auto = bool(self.body.get("preamp_auto", True))
        nf = xp.log_grid(self.flo, self.fhi, _NULL_N)
        if writer == "poweramp":
            name = self.body.get("name", "profile")
            if self.taste_name and taste:
                name = "%s + %s" % (name, self.taste_name)
            if st["policy"] == "stereo":
                chains = allc
            else:
                g, bands, _note = xp.pick_chain(allc,
                                                st["policy"])
                chains = [("all", g, bands)]
            adj, moved = xp.headroom_preamp(
                chains[0][1], [b for _k, _g, b in chains],
                auto=auto)
            if moved:
                chains = [(k, adj, b) for k, _g, b in chains]
            text = xp.poweramp_json(t, chains, name)
            errs = xp.null_test_poweramp(text, chains, nf)
            worst = max(errs.values())
            per = ", ".join("%s %.2f" % (k, v)
                            for k, v in sorted(errs.items()))
            verdict = ("pass" if worst <= xp.NULL_PASS_DB else
                       "CHECK, above the %.1f dB ceiling"
                       % xp.NULL_PASS_DB)
            line = ("Null test per chain over %s: %s dB -- %s."
                    " Preset preamp %+.1f dB%s."
                    % (self._band_str(), per, verdict, adj,
                       (" (lowered %.1f dB for headroom)" % -moved)
                       if moved < 0 and not auto else ""))
            self._set_status(st, line, worst <= xp.NULL_PASS_DB)
        elif writer in ("parametric", "sheet"):
            maxb = t.get("max_bands")
            refit_why = None
            perr = None
            if st["policy"] == "mean":
                pm, pwhy = xp.parallel_mean(allc, nf)
                if pm:
                    g, bands, perr = pm
                    note = ("mean of %s -- pairwise band average"
                            % ", ".join(k for k, _g, _b in allc))
                else:
                    refit_why = "mean of channels -- " + pwhy
            else:
                g, bands, note = xp.pick_chain(allc, st["policy"])
            if refit_why is None:
                fg, fbands, folded = xp.fold_flat(g, bands)
                viol = xp.chain_violations(t, fbands)
                if viol:
                    refit_why = "; ".join(viol)
            if refit_why:
                self._bake_refit_async(st, t, writer, taste,
                                       allc, auto, nf, maxb,
                                       refit_why)
                return
            else:
                hdr = self._header(t, note, taste)
                if perr is not None:
                    hdr.append("Mean by pairwise band average of"
                               " parallel tables; verified within"
                               " %.2f dB of the true mean." % perr)
                tl = xp.limits_text(t)
                if tl:
                    hdr.append("Target limits: %s -- the chain"
                               " fits as-is." % tl)
                if folded:
                    hdr.append("Flat-gain trim folded into the"
                               " shared gain (%+.1f dB)." % folded)
                fg, moved = xp.headroom_preamp(fg, [fbands],
                                               auto=auto)
                if moved < 0 and not auto:
                    hdr.append("Shared gain lowered %.1f dB so the"
                               " composed chain stays under 0 dBFS."
                               % -moved)
                ref = xp.chain_response(g + moved, bands, nf)
                psuf = ("" if perr is None else
                        " Mean by band average, %.2f dB vs the"
                        " true mean." % perr)
                if writer == "parametric":
                    text = xp.parametric_text(fg, fbands,
                                              header=hdr)
                    err = xp.null_test_parametric(text, nf, ref)
                    self._set_status(
                        st, self._null_line(err)
                        + " Export preamp %+.1f dB." % fg + psuf,
                        err <= xp.NULL_PASS_DB)
                else:
                    text = xp.sheet_text(t, fg, fbands, header=hdr)
                    rp, rb = xp.rounded_chain(t, fg, fbands)
                    got = xp.chain_response(rp, rb, nf)
                    err = max(abs(a - b) for a, b in zip(got, ref))
                    self._set_status(
                        st, "Rounding to the target's steps costs"
                        " max %.2f dB over %s. Export preamp"
                        " %+.1f dB." % (err, self._band_str(), fg)
                        + psuf,
                        err <= xp.NULL_PASS_DB)
        elif writer == "graphiceq":
            grid = xp.graphic_grid()
            mv = self._measurement_vals(st, taste,
                                        cap=self._boost_cap())
            capped_by = 0.0
            if mv:
                fgc, vals, tnote, capped_by = mv
                note = self._collapse_note(st["policy"])
                resp = xp.sample_curve(fgc, vals, grid)
                ref = xp.sample_curve(fgc, vals, nf)
            else:
                tnote = None
                resp, note = xp.collapse(allc, st["policy"], grid)
                ref, _n = xp.collapse(allc, st["policy"], nf)
            hdr = self._header(t, note, taste)
            hdr.append(self._source_line(tnote))
            if capped_by > 0:
                hdr.append("Boost capped at %+.1f dB (the fit's"
                           " policy): the measurement asked %.2f"
                           " dB more to fill deep dips."
                           % (self._boost_cap(), capped_by))
            text, shift = xp.graphiceq_text(
                grid, resp, header=hdr, bare=bool(t.get("bare")))
            err = xp.null_test_graphic(text, nf, ref, shift)
            line = "Source: %s. %s" % (
                "canvas" if mv else "chain",
                self._null_line(err, "the measured desired" if mv
                                else "the in-app chain"))
            if shift:
                line += " Level shifted %+.1f dB." % shift
            if capped_by > 0:
                line += (" Boost capped at %+.1f dB."
                         % self._boost_cap())
            self._set_status(st, line, err <= xp.NULL_PASS_DB)
        else:                                   # fixed
            pf = xp.log_grid(self.flo, self.fhi, _PLOT_N)
            mv = self._measurement_vals(st, taste,
                                        cap=self._boost_cap())
            capped_by = 0.0
            if mv:
                fgc, vals, tnote, capped_by = mv
                note = self._collapse_note(st["policy"])
                desired = xp.sample_curve(fgc, vals, pf)
            else:
                tnote = None
                desired, note = xp.collapse(allc, st["policy"],
                                            pf)
            sol = xp.solve_fixed(t, pf, desired)
            st["sol"] = sol
            hdr = self._header(t, note, taste)
            hdr.append(self._source_line(tnote))
            if capped_by > 0:
                hdr.append("Boost capped at %+.1f dB (the fit's"
                           " policy): the measurement asked %.2f"
                           " dB more to fill deep dips."
                           % (self._boost_cap(), capped_by))
            text = xp.fixed_sheet_text(t, sol, header=hdr)
            self._set_status(
                st, "Source: %s. Fit over %s: residual max %.1f"
                " dB, rms %.1f dB across %s; level trim %+.1f dB."
                % ("canvas" if mv else "chain", sol["basis"],
                   sol["resid_max"], sol["resid_rms"],
                   self._band_str(), sol["offset"]), None)
            st["resid"].queue_draw()
        st["text"] = text
        st["view"].get_buffer().set_text(text)

    def _bake_refit_async(self, st, t, writer, taste, allc,
                          auto, nf, maxb, why):
        """The optimizer takes seconds on a real profile; freezing
        the main loop until GNOME offers Force Quit is not a
        progress report. The math runs on a worker thread -- pure
        export_peq calls, no GTK objects touched -- and lands via
        idle_add. A generation stamp on the page drops any result
        that a newer toggle or policy change has superseded."""
        gen = st["gen"]
        st["text"] = ""
        for k in ("copy", "save"):
            st[k].set_sensitive(False)
        st["prog"].set_fraction(0.0)
        st["prog"].set_visible(True)
        st["view"].get_buffer().set_text(
            "# re-fitting -- the preview lands when the optimizer"
            " does")
        self._set_status(
            st, "Source: %s. Re-fitting (%s)..."
            % ("canvas" if self.source == "measurement"
               else "chain", why), None)

        src = ("canvas" if self.source == "measurement"
               else "chain")

        def on_prog(frac, band, horizon, evals):
            def apply():
                if st["gen"] == gen:
                    st["prog"].set_fraction(frac)
                    self._set_status(
                        st, "Source: %s. Re-fitting (%s)... band"
                        " %d/%d, %d evaluations."
                        % (src, why, min(band + 1, horizon),
                           horizon, evals), None)
                return False
            GLib.idle_add(apply)

        def work():
            try:
                res = self._bake_refit(st, t, writer, taste,
                                       allc, auto, nf, maxb, why,
                                       progress=on_prog)
            except Exception as e:
                res = e

            def land():
                if st["gen"] != gen:
                    return False
                st["prog"].set_visible(False)
                for k in ("copy", "save"):
                    st[k].set_sensitive(True)
                if isinstance(res, Exception):
                    self._set_status(st, "Re-fit failed: %s"
                                     % res, False)
                    return False
                text, line, ok = res
                st["text"] = text
                st["view"].get_buffer().set_text(text)
                self._set_status(st, line, ok)
                return False
            GLib.idle_add(land)

        threading.Thread(target=work, daemon=True).start()

    def _bake_refit(self, st, t, writer, taste, allc, auto, nf,
                    maxb, why, progress=None):
        """A band-domain export through the export-time re-fit: the
        mean policy, or a chain over the target's band budget. The
        desired comes from the canvas under the measurement source,
        from the chain response otherwise; center_curve splits it
        into shape (the bands realize it) and level (rides in the
        preamp, and under Auto the composed Safe owns it outright).
        The band budget is the target's max_bands, else the
        profile's own fit budget (params.bands), else the richest
        chain -- so two unbudgeted parametric targets, or one
        matching the profile's budget, bake identical tables
        instead of diverging on greedy horizon. The optimizer's
        box is narrowed to the target's declared gain/Q/type
        ranges, and a declared freq_range narrows the fit band
        itself. The format roundtrip governs pass/fail; the re-fit
        residual is stated as its own number, like the fixed
        writer's."""
        flo, fhi = self.flo, self.fhi
        fr = t.get("freq_range")
        if fr:
            flo = max(flo, float(fr[0]))
            fhi = min(fhi, float(fr[1]))
        fgrid = xp.log_grid(flo, fhi, _NULL_N)
        mv = self._measurement_vals(st, taste)
        if mv:
            fgc, cvals, tnote, _cb = mv
            note = self._collapse_note(st["policy"])
            vals = xp.sample_curve(fgc, cvals, fgrid)
        else:
            tnote = None
            vals, note = xp.collapse(allc, st["policy"], fgrid)
        vals0, off = xp.center_curve(vals)
        params = (self.body.get("fit") or {}).get("params") or {}
        rich = max([len([b for b in bb
                         if b.get("enabled", True)])
                    for _k, _g, bb in allc] or [0])
        budget = maxb or int(params.get("bands", 0)) or rich or 10
        gcap = float(params.get("max_boost", 6.0))
        glim = xp.fit_limits(t).get("gain")
        ghi = min(gcap, glim[1]) if glim else gcap
        bands, rmax, rrms = xp.refit_bands(
            fgrid, vals0, flo, fhi, budget, gcap,
            limits=xp.fit_limits(t), progress=progress)
        got = xp.chain_response(0.0, bands, fgrid)
        ct = [min(v, ghi) for v in vals0]
        ce = [abs(a - b) for a, b in zip(got, ct)]
        cmax = max(ce)
        crms = (sum(e * e for e in ce) / len(ce)) ** 0.5
        ask = rmax - cmax
        base = (float(self.body.get("preamp", 0.0)) + off
                if mv else off)
        adj, _moved = xp.headroom_preamp(base, [bands], auto=auto)
        hdr = self._header(t, note, taste)
        hdr.append(self._source_line(tnote))
        tl = xp.limits_text(t)
        if tl:
            hdr.append("Target limits: %s." % tl)
        hdr.append("Re-fit to %d bands (%s); residual max %.2f,"
                   " rms %.2f dB vs the capped target."
                   % (len(bands), why, cmax, crms))
        if ask > 0.3:
            hdr.append("The uncapped ask exceeds the %+.1f dB"
                       " boost cap by up to %.2f dB: deep dips"
                       " stay unfilled." % (ghi, rmax))
        ref = xp.chain_response(adj, bands, nf)
        if writer == "parametric":
            text = xp.parametric_text(adj, bands, header=hdr)
            err = xp.null_test_parametric(text, nf, ref)
        else:
            text = xp.sheet_text(t, adj, bands, header=hdr)
            rp, rb = xp.rounded_chain(t, adj, bands)
            got = xp.chain_response(rp, rb, nf)
            err = max(abs(a - b) for a, b in zip(got, ref))
        unf = ("" if ask <= 0.3 else
               " Unfillable ask %.2f dB above the cap." % rmax)
        line = ("Source: %s. Re-fit to %d bands (%s): residual max"
                " %.2f, rms %.2f dB vs the capped target.%s Format"
                " roundtrip max %.2f dB -- %s. Export preamp"
                " %+.1f dB."
                % ("canvas" if mv else "chain", len(bands), why,
                   cmax, crms, unf, err,
                   "pass" if err <= xp.NULL_PASS_DB else "CHECK",
                   adj))
        return text, line, err <= xp.NULL_PASS_DB

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
