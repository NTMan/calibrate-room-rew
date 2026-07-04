"""Headless smoke test for the tier-1 headroom UI.

Run:  python3 tools/smoke_gui_headroom.py             (real display)
      xvfb-run -a python3 tools/smoke_gui_headroom.py (headless CI/container)

Hermetic by construction -- safe to run on a live desktop:

  * HOME points at a throwaway directory *before* perdeviceeq is imported,
    so the real ~/.config/per-device-eq is never read or written
    (config.CONFIG_DIR is resolved with expanduser at import time);
  * PipeWire is forced offline (pipewire.missing_tools is stubbed), so no
    test chain is ever published to the session metadata / live sinks;
  * the GApplication is NON_UNIQUE, so a running per-device-eq instance is
    never remote-activated by app.run().

Without all three the window boots on the real default sink's *bound*
profile -- whose tier-1 lamp may legitimately be on already -- and the
later steps would fork and save test chains into the real config.
"""
import os
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_home = tempfile.mkdtemp(prefix="pde-smoke-home-")
os.environ["HOME"] = _home                     # before the perdeviceeq import

import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import GLib, Gio

from perdeviceeq import eq, pipewire
from perdeviceeq.gui import EqApplication

pipewire.missing_tools = lambda *a, **k: ["pw-dump"]     # force offline mode

RC = [1]

DEMO_FR = [
    {"type": "LSC", "freq": 50,   "gain":   3.0, "q": 2.00},
    {"type": "PK",  "freq": 73,   "gain":  -3.4, "q": 2.01},
    {"type": "PK",  "freq": 105,  "gain":  -3.3, "q": 2.00},
    {"type": "PK",  "freq": 196,  "gain":   9.7, "q": 2.32},
    {"type": "PK",  "freq": 673,  "gain":   8.2, "q": 1.14},
    {"type": "PK",  "freq": 887,  "gain":  16.1, "q": 1.36},
    {"type": "PK",  "freq": 890,  "gain": -19.9, "q": 1.00},
    {"type": "PK",  "freq": 2073, "gain":  -6.5, "q": 1.43},
    {"type": "PK",  "freq": 2595, "gain":  -5.7, "q": 2.48},
]


def check(a):
    w = a.win
    try:
        # 1. Clean profile: neutral readout, no lamp
        assert not w.clip_icon.get_visible()
        st = w.preamp_row.get_subtitle()
        assert "post-EQ peak +0.0 dBFS" in st, st
        assert "clip-risk" not in w.preamp_row.get_css_classes()

        # 2. A +9.6 dB PK crosses 0 -> lamp + red + tooltip
        w.bands.append(eq.Band("PK", 200.0, 9.6, 2.25, True))
        w._on_edit()
        st = w.preamp_row.get_subtitle()
        assert w.clip_icon.get_visible()
        assert "+9.6 dBFS" in st and "can clip" in st, st
        assert "clip-risk" in w.preamp_row.get_css_classes()
        assert "error" in w.preamp_spin.get_css_classes()
        assert w.preamp_row.get_tooltip_text()

        # 3. Auto zeroes the estimate (curve max, not largest gain)
        w._on_auto(None)
        assert abs(w.preamp + 9.6) < 1e-6, w.preamp
        assert not w.clip_icon.get_visible(), w.preamp_row.get_subtitle()
        assert "+0.0 dBFS" in w.preamp_row.get_subtitle()

        # 4. Bypass -> neutral subtitle, no lamp
        w.bypass_row.set_active(True)
        assert w.preamp_row.get_subtitle() == w._preamp_subtitle
        assert not w.clip_icon.get_visible()
        w.bypass_row.set_active(False)

        # 5. Per-channel: the shared preamp's readout shows the WORST
        #    chain; every over-0 channel is flagged on its tab
        w.link_row.set_active(False)          # unlink; editor shows FL
        assert w.cur_ch == "FL"
        w.slots["FR"]["bands"].append(eq.Band("PK", 1000.0, 12.0, 1.0, True))
        w._update_headroom()
        exp = eq.headroom_bound_db(w.preamp, w.slots["FR"]["bands"])
        assert exp > 2.0, exp                 # scenario is meaningfully over
        st = w.preamp_row.get_subtitle()
        assert ("%+.1f dBFS on FR" % exp) in st and "can clip" in st, st
        assert w.clip_icon.get_visible()
        assert "clip-risk" not in w._chan_buttons["FL"].get_css_classes()
        assert "clip-risk" in w._chan_buttons["FR"].get_css_classes()
        assert ("%+.1f" % exp) in w._chan_buttons["FR"].get_tooltip_text()
        # the spin is shared: switching tabs changes neither it nor the
        # number -- only the "on FR" suffix drops on FR's own tab
        v_before = w.preamp_spin.get_value()
        w._chan_buttons["FR"].set_active(True)
        st = w.preamp_row.get_subtitle()
        assert ("%+.1f dBFS — can clip" % exp) in st and " on " not in st, st
        assert w.preamp_spin.get_value() == v_before
        w._chan_buttons["FL"].set_active(True)

        # 5b. Balanced Auto is structural now: ONE shared preamp, computed
        #     from the WORST channel, clears every tab from any tab
        import math as _m
        worst = max(eq.curve_max_db(0.0, w.slots[k]["bands"])
                    for k in w.ch_keys)
        tgt = -_m.ceil(worst * 10.0 - 1e-9) / 10.0
        w._on_auto(None)
        assert abs(w.preamp - tgt) < 1e-9, (w.preamp, tgt)
        assert "preamp" not in w.slots["FR"]        # slots carry bands only
        assert not w.clip_icon.get_visible()
        for k in ("FL", "FR"):
            assert "clip-risk" not in w._chan_buttons[k].get_css_classes()

        # 5c. Unlinked REW import IGNORES the file's preamp (a per-ear
        #     file cannot know the other channels): the shared value is
        #     recomputed from ALL curves -- neither -5 nor -20 may leak in
        w._apply_rew_import(-5.0, [eq.Band("PK", 100.0, 5.0, 1.0, True)])
        worst2 = max(eq.curve_max_db(0.0, w.slots[k]["bands"])
                     for k in w.ch_keys)
        tgt2 = -_m.ceil(worst2 * 10.0 - 1e-9) / 10.0
        assert abs(w.preamp - tgt2) < 1e-9, (w.preamp, tgt2)
        w._apply_rew_import(-20.0, [eq.Band("PK", 100.0, 5.0, 1.0, True)])
        assert abs(w.preamp - tgt2) < 1e-9, w.preamp

        # 6. Linked import overwrites the preamp (single chain, the file's
        #    number is the whole story); old-Auto regression: dueling
        #    demo-FR boosts/cuts must not over-attenuate (old rule: -16.1)
        w.link_row.set_active(True)
        w._apply_rew_import(-4.0, [eq.Band(d["type"], d["freq"], d["gain"],
                                           d["q"], True) for d in DEMO_FR])
        assert w.preamp == -4.0
        w._on_auto(None)
        assert abs(w.preamp + 8.5) < 1e-6, w.preamp

        print("GUI-SMOKE-OK")
        RC[0] = 0
    except Exception:
        traceback.print_exc()
    finally:
        a.quit()


print("smoke: sandbox HOME=%s, PipeWire forced offline" % _home)
app = EqApplication()
app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
app.connect("activate", lambda a: GLib.idle_add(check, a))
app.run(None)
sys.exit(RC[0])
