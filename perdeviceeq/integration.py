# -*- coding: utf-8 -*-
"""System integration: install the static WirePlumber hook (+ its component
config) and the desktop entry + themed icon into the user's home, plus the
matching uninstall. Everything is reversible and writes only under the user's
~/.config and ~/.local/share (the system /usr/share files belong to a package).

No GTK; filesystem + a couple of best-effort CLI refreshes via pipewire._run.
"""

import os, re, sys

from .config import (HOOK_SRC_CANDIDATES, WP_SCRIPT, WP_SCRIPT_STALE, WP_CONF,
                     HOOK_CONF, SYS_DESKTOP_FILE, ICON_SRC_CANDIDATES,
                     DESKTOP_SRC_CANDIDATES, USER_ICON_FILE, USER_DESKTOP_FILE)
from .pipewire import _run


# ============================ WirePlumber hook ============================
def _write_if_changed(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    old = None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                old = f.read()
        except Exception:
            old = None
    if old != content:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    return False

def hook_source():
    """Path to the static hook .lua shipped with per-device-eq, or None."""
    for p in HOOK_SRC_CANDIDATES:
        if os.path.exists(p):
            return p
    return None

def install_hook():
    """Install the static WP hook (.lua copied verbatim from the repo) and the
    component config. Returns True if anything was written (new or changed) --
    the caller should then restart WirePlumber once so the new hook + metadata
    object are (re)loaded. In steady state nothing changes and no restart is
    needed; live edits flow through the metadata object, not the file."""
    src = hook_source()
    if src is None:
        raise FileNotFoundError(
            "per-device-eq hook script not found; looked in:\n  "
            + "\n  ".join(HOOK_SRC_CANDIDATES))
    with open(src, encoding="utf-8") as f:
        lua = f.read()
    # an earlier build also wrote the script under ~/.config/.../scripts; drop it
    try:
        if os.path.exists(WP_SCRIPT_STALE):
            os.remove(WP_SCRIPT_STALE)
    except Exception:
        pass
    lua_changed = _write_if_changed(WP_SCRIPT, lua)
    conf_changed = _write_if_changed(WP_CONF, HOOK_CONF)
    return lua_changed or conf_changed

def install_full():
    """One source of truth for --install and the GUI dialog: the
    hook (with one WirePlumber restart when it changed), then the
    desktop entry unless a system package owns it. Returns
    {"hook": changed, "desktop": "packaged"|"installed"|"missing"
    |"exported", "restarted": True|False|None} for the caller to
    narrate its own way -- restarted False means the hook landed
    but the service kept running the old world (a sandbox, or a
    system without systemd --user), and the caller MUST say so: a
    silently unloaded hook is the app doing nothing. Inside a
    Flatpak the desktop half is skipped ("exported"): Flatpak
    already exports the entry and icon from /app/share, and a
    hand-copied entry would carry Exec=per-device-eq -- a command
    that does not exist on the host."""
    changed = install_hook()
    restarted = None
    if changed:
        restarted = restart_wireplumber()
    if os.environ.get("FLATPAK_ID"):
        desk = "exported"          # Flatpak owns the entry + icon
    elif os.path.exists(SYS_DESKTOP_FILE):
        desk = "packaged"
    else:
        try:
            install_desktop_integration()
            desk = "installed"
        except FileNotFoundError:
            desk = "missing"
    return {"hook": changed, "desktop": desk,
            "restarted": restarted}


def hook_installed():
    """Both halves on disk: the question the GUI asks at startup."""
    return os.path.exists(WP_SCRIPT) and os.path.exists(WP_CONF)


def uninstall_hook():
    """Remove the hook script and the component config; True when
    anything was removed. The hook's saved-state directory stays:
    it is the user's data, not ours to erase."""
    removed = False
    for p in (WP_SCRIPT, WP_CONF):
        try:
            if os.path.exists(p):
                os.remove(p)
                removed = True
        except OSError:
            pass
    return removed


def uninstall_full():
    """The mirror of install_full, for the GUI trigger: remove
    the hook and try one restart so the engine actually unloads.
    Returns {"removed": bool, "restarted": True|False|None} --
    restarted False means the files are gone but WirePlumber
    still runs the loaded copy until a restart (a sandbox, or a
    system without systemd --user), and the caller MUST say so.
    The hook's saved EQ state stays on disk either way."""
    removed = uninstall_hook()
    restarted = None
    if removed:
        restarted = restart_wireplumber()
    return {"removed": removed, "restarted": restarted}


def restart_wireplumber():
    return _run(["systemctl", "--user", "restart", "wireplumber"]).returncode == 0


def _first_existing(paths):
    for p in paths:
        if os.path.exists(p):
            return p
    return None

def _launcher_exec():
    launcher = os.path.abspath(sys.argv[0])
    if os.access(launcher, os.X_OK):
        return launcher
    return (sys.executable or "python3") + " " + launcher

def install_desktop_integration():
    """Install the .desktop entry + themed icon into ~/.local/share so the
    launcher/dock show 'Per-Device EQ' with our icon (the window's Wayland app_id
    is APP_ID, so the entry is <APP_ID>.desktop and the icon is themed <APP_ID>).
    Both files are copied from the shipped data/ dir; this is intentionally strict
    -- a missing source is a packaging error and is reported, not hidden. No-op
    when a system package already provides the entry. Raises FileNotFoundError if
    a source file is missing; returns True if anything was written."""
    if os.path.exists(SYS_DESKTOP_FILE):
        return False                       # packaged install owns this; leave it
    isrc = _first_existing(ICON_SRC_CANDIDATES)
    dsrc = _first_existing(DESKTOP_SRC_CANDIDATES)
    if isrc is None or dsrc is None:
        missing = []
        if dsrc is None:
            missing.append("desktop entry, expected:\n    "
                           + "\n    ".join(DESKTOP_SRC_CANDIDATES))
        if isrc is None:
            missing.append("icon, expected:\n    "
                           + "\n    ".join(ICON_SRC_CANDIDATES))
        raise FileNotFoundError("missing data file(s):\n  " + "\n  ".join(missing))
    changed = False
    with open(isrc, encoding="utf-8") as f:
        changed |= _write_if_changed(USER_ICON_FILE, f.read())
    # take the shipped .desktop but point Exec= at how we were actually launched
    # (so it works when run from a source checkout, not only when in PATH)
    with open(dsrc, encoding="utf-8") as f:
        text = re.sub(r"(?m)^Exec=.*$", "Exec=" + _launcher_exec(), f.read())
    changed |= _write_if_changed(USER_DESKTOP_FILE, text)
    if changed:                            # best-effort cache refresh (ignore if absent)
        _run(["gtk-update-icon-cache", "-f", "-t",
              os.path.expanduser("~/.local/share/icons/hicolor")])
        _run(["update-desktop-database",
              os.path.expanduser("~/.local/share/applications")])
    return changed

def uninstall_desktop_integration():
    """Remove the user-local .desktop entry + icon that --install-desktop created.
    Only ever touches ~/.local/share (never the system /usr/share files, which
    belong to a package). Returns True if anything was removed."""
    removed = False
    for path in (USER_DESKTOP_FILE, USER_ICON_FILE):
        try:
            if os.path.exists(path):
                os.remove(path)
                removed = True
        except OSError:
            pass
    if removed:
        _run(["gtk-update-icon-cache", "-f", "-t",
              os.path.expanduser("~/.local/share/icons/hicolor")])
        _run(["update-desktop-database",
              os.path.expanduser("~/.local/share/applications")])
    return removed
