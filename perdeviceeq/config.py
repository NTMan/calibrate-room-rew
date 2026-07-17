# -*- coding: utf-8 -*-
"""Constants, paths and the WirePlumber component config for per-device-eq.

No third-party imports -- this module is safe to import anywhere (CLI, tests,
GUI) without pulling in GTK.
"""

import os

FS = 48000.0
FMIN, FMAX = 20.0, 20000.0   # audible range: FR plot grid + headroom curve scan
CONFIG_DIR        = os.path.expanduser("~/.config/per-device-eq")
USER_PROFILES_DIR = os.path.join(CONFIG_DIR, "profiles")
BINDINGS_FILE     = os.path.join(CONFIG_DIR, "bindings.json")
UI_STATE_FILE     = os.path.join(CONFIG_DIR, "ui-state.json")
LEGACY_CONFIG     = os.path.join(CONFIG_DIR, "config.json")

# Data root: the directory that holds wireplumber/, data/ and profiles/ as
# siblings of this `perdeviceeq` package -- i.e. the repo root when running from
# a checkout, or /usr/share/per-device-eq when installed (the package lives at
# <root>/perdeviceeq/, so the data root is its parent directory).
_DATA_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYS_PROFILE_DIRS = ["/usr/share/per-device-eq/profiles",
                    os.path.join(_DATA_ROOT, "profiles")]

# Persistence is native to WirePlumber:
#   * a small, STATIC WP Lua hook (shipped in the repo, installed verbatim)
#     applies the in-node EQ on each sink and owns persistence,
#   * a PipeWire metadata object ("per-device-eq", created by WP's own
#     metadata.lua) carries live edits from the app to the hook,
#   * the hook persists graphs to WpState (~/.local/state/wireplumber/) and
#     reloads them at startup, so the EQ survives reboot with no user process.
# WirePlumber 0.5 looks up user scripts in ~/.local/share/wireplumber/scripts.
WP_SCRIPT_DIR  = os.path.expanduser("~/.local/share/wireplumber/scripts")
WP_SCRIPT_NAME = "90-per-device-eq.lua"
WP_SCRIPT      = os.path.join(WP_SCRIPT_DIR, WP_SCRIPT_NAME)
WP_SCRIPT_STALE = os.path.expanduser(  # removed: an earlier build wrote here too
    "~/.config/wireplumber/scripts/90-per-device-eq.lua")
WP_CONF_DIR    = os.path.expanduser("~/.config/wireplumber/wireplumber.conf.d")
WP_CONF        = os.path.join(WP_CONF_DIR, "90-per-device-eq.conf")
METADATA_NAME  = "per-device-eq"
# the static hook is shipped next to the package (repo) or system-wide (package)
HOOK_SRC_CANDIDATES = [os.path.join(_DATA_ROOT, "wireplumber", WP_SCRIPT_NAME),
                       "/usr/share/per-device-eq/wireplumber/" + WP_SCRIPT_NAME]
# where the hook persists graphs; used to drive a one-time migration from
# bindings.json into WpState (after that the hook owns the file)
WPSTATE_FILE   = os.path.expanduser("~/.local/state/wireplumber/" + METADATA_NAME)

# desktop integration (so the launcher/dock shows our name + icon). The window's
# Wayland app_id equals APP_ID, so the .desktop must be named <APP_ID>.desktop
# and ship an icon themed as <APP_ID>.
APP_ID = "io.github.ntman.PerDeviceEQ"
DESKTOP_FILE_NAME = APP_ID + ".desktop"
ICON_FILE_NAME    = APP_ID + ".svg"
DESKTOP_SRC_CANDIDATES = [os.path.join(_DATA_ROOT, "data", DESKTOP_FILE_NAME),
                          "/usr/share/applications/" + DESKTOP_FILE_NAME]
ICON_SRC_CANDIDATES = [
    os.path.join(_DATA_ROOT, "data", "icons", "hicolor", "scalable", "apps", ICON_FILE_NAME),
    "/usr/share/icons/hicolor/scalable/apps/" + ICON_FILE_NAME]
USER_DESKTOP_FILE = os.path.expanduser("~/.local/share/applications/" + DESKTOP_FILE_NAME)
USER_ICON_FILE    = os.path.expanduser(
    "~/.local/share/icons/hicolor/scalable/apps/" + ICON_FILE_NAME)
SYS_DESKTOP_FILE  = "/usr/share/applications/" + DESKTOP_FILE_NAME

# GtkBuilder design for the GUI (ships in data/ next to the package, or under
# /usr/share/per-device-eq/data when installed -> both resolve via _DATA_ROOT).
UI_FILE_NAME = APP_ID + ".ui"
UI_FILE_CANDIDATES = [os.path.join(_DATA_ROOT, "data", UI_FILE_NAME),
                      "/usr/share/per-device-eq/data/" + UI_FILE_NAME]
# the measurement wizard ships its own GtkBuilder design alongside
MEASURE_UI_FILE_NAME = APP_ID + ".Measure.ui"
MEASURE_UI_FILE_CANDIDATES = [
    os.path.join(_DATA_ROOT, "data", MEASURE_UI_FILE_NAME),
    "/usr/share/per-device-eq/data/" + MEASURE_UI_FILE_NAME]
# user's curated "favorites" set of profile ids (Weather-style quick list)
FAVORITES_FILE = os.path.join(CONFIG_DIR, "favorites.json")
PREF_LAYERS_FILE  = os.path.join(CONFIG_DIR, "preference-layers.json")

# measurement wizard persistence (see perdeviceeq/measure_prefs.py):
# reusable measurement-mic profiles, and per-sink recall of the last mic
# and the last auto-level volume that measured that sink well.
MIC_PROFILES_FILE  = os.path.join(CONFIG_DIR, "mic-profiles.json")
MEASURE_STATE_FILE = os.path.join(CONFIG_DIR, "measure-state.json")

TYPE_TO_LABEL = {"PK": "bq_peaking", "LSC": "bq_lowshelf", "HSC": "bq_highshelf"}
TYPE_NAMES = ["PK", "LSC", "HSC"]
SCHEMA_VERSION = 3      # profile body schema; older files convert once via
#                         tools/migrate_profiles_v1_to_v2.py (v1 -> v2) and
#                         tools/migrate_profiles_v2_to_v3.py (v2 -> v3)
# the optional v3 blocks the store/GUI carry verbatim through save /
# import / export; producers own their shape (see perdeviceeq/profiles.py)
V3_BLOCKS = ("provenance", "device", "fit", "measurement")
CLEAN_ID = "clean"



# ============================ WirePlumber hook (config) ============================
# The hook itself is a STATIC file shipped in the repo (wireplumber/90-per-device-eq.lua);
# integration.install_hook() copies it verbatim. Only the small component config
# is built here.
#
# Two components: WP's own metadata.lua creates our named object; our hook reads it.
HOOK_CONF = (
    'wireplumber.components = [\n'
    '  { name = metadata.lua, type = script/lua, provides = metadata.%(meta)s,\n'
    '    arguments = { metadata.name = %(meta)s } }\n'
    '  { name = "%(script)s", type = script/lua, provides = per-device-eq,\n'
    '    requires = [ metadata.%(meta)s ] }\n'
    ']\n'
    'wireplumber.profiles = {\n'
    '  main = {\n'
    '    metadata.%(meta)s = required\n'
    '    per-device-eq = required\n'
    '  }\n'
    '}\n'
) % {"meta": METADATA_NAME, "script": WP_SCRIPT_NAME}
