"""Gemeinsames für LinDeck und Linewave: Einzelinstanz, Taskleiste, Benachrichtigungen, Sitzung.

Beide Programme lassen sich einzeln installieren; was hier steht, brauchen beide.
"""
import json
import logging
import os
import subprocess
import threading
from pathlib import Path

from . import paths
from .i18n import _

log = logging.getLogger("streamlin.desktop")


def _app_info(name):
    try:
        return json.loads((paths.STATE_DIR / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_app_info(gui, name):
    paths.ensure_dirs()
    (paths.STATE_DIR / name).write_text(json.dumps({"pid": os.getpid(), "gui": gui}), encoding="utf-8")


def _clear_app_info(name):
    try:
        if _app_info(name).get("pid") == os.getpid():
            (paths.STATE_DIR / name).unlink()
    except OSError:
        pass


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def running_instance(name):
    """Pid einer laufenden Instanz laut Statusdatei oder None."""
    pid = _app_info(name).get("pid")
    if pid and pid != os.getpid() and _alive(pid):
        return pid
    return None


class Tray:
    """Symbol in der Taskleiste (optional, über pystray)."""

    def __init__(self, on_show, on_quit, icon, title, ident, open_label):
        self.icon = None
        try:
            import pystray
        except Exception as e:  # noqa: BLE001 - pystray fehlt oder kein Backend
            log.info(_("No tray icon (pystray not available: {error})").format(error=e))
            return
        menu = pystray.Menu(pystray.MenuItem(open_label, lambda *a: on_show(), default=True),
                            pystray.MenuItem(_("Quit"), lambda *a: on_quit()))
        try:
            self.icon = pystray.Icon(ident, icon, title, menu)
            threading.Thread(target=self.icon.run, name="tray", daemon=True).start()
        except Exception as e:  # noqa: BLE001
            log.info(_("Tray icon not possible: {error}").format(error=e))
            self.icon = None

    def stop(self):
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:  # noqa: BLE001
                pass


def notify(title, body, app="LinDeck", icon="input-keyboard"):
    try:
        subprocess.Popen(["notify-send", "-a", app, "-i", icon, title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass



def adopt_session_env():
    """Übernimmt DISPLAY/WAYLAND_DISPLAY usw. aus der laufenden Desktop-Sitzung,
    falls LinDeck ohne diese Variablen gestartet wurde (z.B. direkt nach der Installation)."""
    wanted = ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR",
              "XDG_CURRENT_DESKTOP", "XAUTHORITY", "XDG_SESSION_TYPE", "HYPRLAND_INSTANCE_SIGNATURE", "SWAYSOCK")
    if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"):
        return
    uid = os.getuid()
    shells = ("gnome-shell", "plasmashell", "kwin_wayland", "kwin_x11", "xfce4-session", "cinnamon",
              "mate-session", "budgie-wm", "sway", "Hyprland", "gnome-session-binary", "lxqt-session")
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if proc.stat().st_uid != uid:
                continue
            comm = (proc / "comm").read_text().strip()
            if comm not in shells:
                continue
            env = dict(kv.split("=", 1) for kv in (proc / "environ").read_bytes().decode(errors="ignore").split("\0")
                       if "=" in kv)
        except OSError:
            continue
        if env.get("WAYLAND_DISPLAY") or env.get("DISPLAY"):
            for k in wanted:
                if env.get(k) and not os.environ.get(k):
                    os.environ[k] = env[k]
            return
