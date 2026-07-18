#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
per-device-eq -- per-output-device parametric EQ for PipeWire 1.6+ (entry point).

Thin launcher: locate the `perdeviceeq` package (next to this script when run
from a checkout, or /usr/share/per-device-eq when installed), dispatch the CLI,
and launch the GTK/libadwaita GUI. Implementation lives in the package:
config, eq, profiles, pipewire, integration, cli, gui.

  --list-sinks           list sinks (default marked with *)
  --list-sources         list capture sources
  --list-profiles        list known profiles
  --inspect <node.name>  dump node params (pw-dump info.params)
  --apply                apply each bound profile to its sink now
  --install              install the hook + desktop integration
  --uninstall            remove the hook + desktop integration
  (no args)              launch the GTK4 GUI
"""

import argparse, os, sys

# ---- locate the perdeviceeq package (repo checkout first, then system) -------
_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (_HERE, "/usr/share/per-device-eq"):
    if os.path.isdir(os.path.join(_cand, "perdeviceeq")):
        if _cand not in sys.path:
            sys.path.insert(0, _cand)
        break

from perdeviceeq.config import SYS_DESKTOP_FILE
from perdeviceeq.pipewire import missing_tools, missing_tools_message
from perdeviceeq.integration import (install_full, uninstall_hook,
                                      restart_wireplumber,
                                      uninstall_desktop_integration)
from perdeviceeq.cli import (cmd_list, cmd_list_sources,
                             cmd_list_profiles, cmd_inspect,
                             cmd_apply)


def main():
    try:                      # `kill -USR1 <pid>` dumps a live stack to stderr
        import faulthandler, signal
        faulthandler.register(signal.SIGUSR1)
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Per-device PipeWire EQ (profiles)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--list-sinks", action="store_true",
                   help="list sinks (default marked with *)")
    g.add_argument("--list-sources", action="store_true",
                   help="list capture sources")
    g.add_argument("--list-profiles", action="store_true", help="list profiles")
    g.add_argument("--inspect", metavar="NODE_NAME", help="dump node params")
    g.add_argument("--apply", action="store_true",
                   help="push bound profiles into the per-device-eq metadata now")
    g.add_argument("--install", action="store_true",
                   help="install the WirePlumber hook + the desktop entry")
    g.add_argument("--uninstall", action="store_true",
                   help="remove the hook + the desktop entry")
    g.add_argument("--bridge", nargs=2, metavar=("A", "B"),
                   help="frame bridge: the measured delta between two "
                        "profiles of one reference device on two rigs")
    ap.add_argument("--published", metavar="CURVE",
                    help="with --bridge: freq/dB text of the same "
                         "device on a trusted rig, for the external-"
                         "anchor audit of B")
    ap.add_argument("--out", metavar="DIR", default="bridge-out",
                    help="with --bridge: output directory "
                         "(default: %(default)s)")
    args = ap.parse_args()

    if (args.list_sinks or args.list_sources or args.inspect
            or args.apply):
        miss = missing_tools()
        if miss:
            print(missing_tools_message(miss), file=sys.stderr)
            return 2

    if args.bridge:
        from perdeviceeq import bridge as br
        from perdeviceeq.profiles import ProfileStore
        try:
            store = ProfileStore()
            a = br.resolve_profile(store, args.bridge[0])
            b = br.resolve_profile(store, args.bridge[1])
            pub = (br.parse_curve(args.published)
                   if args.published else None)
            res = br.compute_bridge(a, b, published=pub)
            rp = br.write_outputs(res, args.out)
        except (br.BridgeError, OSError) as e:
            print(str(e), file=sys.stderr)
            return 2
        sys.stdout.write(open(rp).read())
        print("written: %s" % args.out)
        return 0
    if args.list_sinks:
        return cmd_list()
    if args.list_sources:
        return cmd_list_sources()
    if args.list_profiles:
        return cmd_list_profiles()
    if args.inspect:
        return cmd_inspect(args.inspect)
    if args.apply:
        return cmd_apply()
    if args.install:
        try:
            res = install_full()
        except FileNotFoundError as e:
            print(str(e), file=sys.stderr)
            return 2
        print("hook + config installed; WirePlumber restarted once..."
              if res["hook"] else "hook already up to date")
        print({"packaged": "desktop entry: provided by the system "
                           "package",
               "installed": "desktop entry + icon installed",
               "missing": "desktop entry skipped: assets not found"}
              [res["desktop"]])
        print("uninstall everything with: per-device-eq.py --uninstall")
        return 0
    if args.uninstall:
        if uninstall_hook():
            print("hook + config removed; restarting WirePlumber once...")
            restart_wireplumber()
        else:
            print("hook was not installed")
        if os.path.exists(SYS_DESKTOP_FILE):
            print("desktop entry: the system package owns it; use "
                  "your package manager")
        else:
            removed = uninstall_desktop_integration()
            print("desktop entry + icon %s"
                  % ("removed" if removed else "were not installed"))
        return 0
    # no CLI flag -> launch the GUI (import here so CLI paths never need GTK)
    from perdeviceeq.gui import launch_gui
    return launch_gui()


if __name__ == "__main__":
    sys.exit(main() or 0)
