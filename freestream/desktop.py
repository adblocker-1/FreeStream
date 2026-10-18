"""Gemeinsames für FreeDeck und FreeWave: Einzelinstanz, Taskleiste, Benachrichtigungen, Sitzung.

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

log = logging.getLogger("freestream.desktop")


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


def notify(title, body, app="FreeDeck", icon="input-keyboard"):
    try:
        subprocess.Popen(["notify-send", "-a", app, "-i", icon, title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass



def adopt_session_env():
    """Übernimmt DISPLAY/WAYLAND_DISPLAY usw. aus der laufenden Desktop-Sitzung,
    falls FreeDeck ohne diese Variablen gestartet wurde (z.B. direkt nach der Installation)."""
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


# --------------------------------------------------------------------------
# Autostart (Einstellungen › Allgemein › „Bei der Anmeldung automatisch starten“)
# --------------------------------------------------------------------------
AUTOSTART = {"freedeck": "freedeck run", "freewave": "freewave --hidden"}
SYSTEM_AUTOSTART = Path("/etc/xdg/autostart")


def _user_autostart_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart"


def _desktop_flags(path):
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    off = any(line.strip().lower() in ("hidden=true", "x-gnome-autostart-enabled=false")
              for line in text.splitlines())
    return not off


OLD_AUTOSTART = {"freedeck": "lindeck", "freewave": "linewave"}  # Dateinamen vor der Umbenennung


def _migrate_autostart(name):
    """Eigene Autostart-Datei unter altem Namen (lindeck/linewave.desktop) übernehmen und entfernen –
    sonst bliebe ein ausgeschalteter Autostart nach der Umbenennung wirkungslos (oder liefe doppelt)."""
    old = _user_autostart_dir() / f"{OLD_AUTOSTART.get(name, '')}.desktop"
    if name not in OLD_AUTOSTART or not old.exists():
        return
    on = _desktop_flags(old)
    try:
        old.unlink()
    except OSError:
        return
    if not (_user_autostart_dir() / f"{name}.desktop").exists():
        set_autostart(name, bool(on))


def migrate_autostart():
    """Beim Programmstart: Autostart-Einstellungen von vor der Umbenennung übernehmen."""
    for name in OLD_AUTOSTART:
        try:
            _migrate_autostart(name)
        except OSError as e:
            log.debug("autostart migration: %s", e)


def autostart_enabled(name):
    """Startet das Programm bei der Anmeldung? (eigene Datei geht vor der des Pakets)"""
    _migrate_autostart(name)
    user = _desktop_flags(_user_autostart_dir() / f"{name}.desktop")
    if user is not None:
        return user
    return bool(_desktop_flags(SYSTEM_AUTOSTART / f"{name}.desktop"))


def set_autostart(name, on):
    """Autostart an/aus – nur im Benutzerordner, die Datei des Pakets bleibt unangetastet."""
    _migrate_autostart(name)
    d = _user_autostart_dir()
    f = d / f"{name}.desktop"
    system_on = bool(_desktop_flags(SYSTEM_AUTOSTART / f"{name}.desktop"))
    if on and system_on:
        try:
            f.unlink()
        except FileNotFoundError:
            pass
        return
    d.mkdir(parents=True, exist_ok=True)
    title = "FreeDeck" if name == "freedeck" else "FreeWave"
    lines = ["[Desktop Entry]", "Type=Application", f"Name={title}", f"Exec={AUTOSTART.get(name, name)}",
             f"Icon={name}", "NoDisplay=true"]
    lines += ["X-GNOME-Autostart-enabled=true", "X-KDE-autostart-phase=2"] if on else \
        ["Hidden=true", "X-GNOME-Autostart-enabled=false"]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")


GENERIC_EXEC = {"env", "flatpak", "sh", "bash", "python", "python3", "java", "wine", "wine64", "gamemoderun",
                "steam", "snap", "gtk-launch", "xdg-open", "mono", "electron", "pkexec", "sudo"}


def _icon_dirs():
    data = [Path(p) for p in (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")]
    data += [Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share"),
             Path("/var/lib/flatpak/exports/share"), Path.home() / ".local/share/flatpak/exports/share"]
    return data


def app_icon(name):
    """Pfad zu einem Programmsymbol (PNG/XPM) zum Icon-Namen einer .desktop-Datei, sonst None."""
    if not name:
        return None
    p = Path(name)
    if p.is_absolute():
        return p if p.is_file() and p.suffix.lower() in (".png", ".xpm") else None
    for base in _icon_dirs():
        for size in ("48x48", "64x64", "128x128", "256x256", "32x32", "96x96", "512x512"):
            f = base / "icons" / "hicolor" / size / "apps" / f"{name}.png"
            if f.is_file():
                return f
    for ext in (".png", ".xpm"):
        f = Path("/usr/share/pixmaps") / f"{name}{ext}"
        if f.is_file():
            return f
    return None


def app_entries():
    """Programme aus den .desktop-Dateien: [{"name", "cls" (Fensterklasse), "key" (wie das Programm in
    PipeWire heißt – Programmdatei, sonst Fensterklasse), "icon" (Name aus Icon=)}], nach Namen sortiert."""
    dirs = [Path(p) / "applications" for p in
            (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")]
    dirs += [Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "applications",
             Path("/var/lib/flatpak/exports/share/applications"),
             Path.home() / ".local/share/flatpak/exports/share/applications"]
    found = {}
    for d in dirs:
        try:
            files = sorted(d.glob("*.desktop"))
        except OSError:
            continue
        for f in files:
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            entry = text.split("[Desktop Action", 1)[0]
            vals = {}
            for line in entry.splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    vals.setdefault(k.strip(), v.strip())
            if vals.get("NoDisplay", "").lower() == "true" or vals.get("Type", "Application") != "Application":
                continue
            name = vals.get("Name")
            if not name or name in found:
                continue
            exe = os.path.basename((vals.get("Exec") or f.stem).split()[0]).rsplit(".", 1)[0]
            cls = vals.get("StartupWMClass") or exe
            if cls in ("env", "flatpak"):
                cls = f.stem.rsplit(".", 1)[-1] if f.stem.count(".") >= 2 else f.stem
            key = exe if exe and exe.lower() not in GENERIC_EXEC else (vals.get("StartupWMClass") or name)
            found[name] = {"name": name, "cls": cls.lower(), "key": key.lower(), "icon": vals.get("Icon", "")}
    return sorted(found.values(), key=lambda e: e["name"].lower())


def installed_apps():
    """Programme aus den .desktop-Dateien: [(Name, Fensterklasse)] – für „Profil je Programm“."""
    return [(e["name"], e["cls"]) for e in app_entries()]
