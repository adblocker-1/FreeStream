"""Automatischer Import: sucht in Downloads, Desktop und USB-Sticks nach
Elgato-Profilen, Backups, ProfilesV2/V3-Ordnern und dem Elgato-Installer.

Bereits importierte Dateien werden sich gemerkt (Pfad + Änderungszeit),
damit nichts doppelt importiert wird.
"""
import getpass
import logging
import os
import subprocess
import zipfile
from pathlib import Path

from . import assets, paths, profiles

log = logging.getLogger("lindeck.autoimport")

PROFILE_EXT = (".streamdeckprofile", ".streamdeckprofilesbackup")


def _xdg_dir(name, fallback):
    try:
        out = subprocess.run(["xdg-user-dir", name], capture_output=True, text=True, timeout=2).stdout.strip()
        if out and out != str(Path.home()):
            return Path(out)
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.home() / fallback


def search_dirs():
    dirs = [_xdg_dir("DOWNLOAD", "Downloads"), _xdg_dir("DESKTOP", "Desktop"),
            Path.home() / "Downloads", Path.home() / "Schreibtisch"]
    user = getpass.getuser()
    for media in (Path("/media") / user, Path("/run/media") / user):
        if media.is_dir():
            try:
                dirs += [d for d in media.iterdir() if d.is_dir()]
            except OSError:
                pass
    seen, out = set(), []
    for d in dirs:
        try:
            r = d.resolve()
        except OSError:
            continue
        if r not in seen and r.is_dir():
            seen.add(r)
            out.append(r)
    return out


def _zip_has_profiles(p: Path) -> bool:
    try:
        with zipfile.ZipFile(p) as z:
            return any(".sdProfile/" in n or n.lower().endswith(PROFILE_EXT) for n in z.namelist()[:5000])
    except (zipfile.BadZipFile, OSError):
        return False


def candidates(base: Path, depth=2):
    """Findet Profil-Dateien/-Ordner und Elgato-Installer bis zu 'depth' Ebenen tief."""
    found = []
    stack = [(base, 0)]
    while stack:
        d, lvl = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for e in entries:
            name = e.name.lower()
            if name.startswith("."):
                continue
            if e.is_file():
                if name.endswith(PROFILE_EXT):
                    found.append(("profile", e))
                elif name.endswith(".msi") and "stream" in name and "deck" in name:
                    found.append(("msi", e))
                elif name.endswith(".zip") and ("profile" in name or "streamdeck" in name.replace("_", "")
                                                or "stream deck" in name) and _zip_has_profiles(e):
                    found.append(("profile", e))
            elif e.is_dir():
                if name in ("profilesv2", "profilesv3", "profiles") and any(
                        c.name.endswith(".sdProfile") for c in e.iterdir() if c.is_dir()):
                    found.append(("profile", e))
                elif e.name.endswith(".sdProfile"):
                    continue
                elif lvl < depth:
                    stack.append((e, lvl + 1))
    return found


def _stamp(p: Path):
    try:
        if p.is_dir():
            return max([p.stat().st_mtime] + [c.stat().st_mtime for c in p.iterdir()])
        return p.stat().st_mtime
    except OSError:
        return 0


def notify(title, body):
    try:
        subprocess.Popen(["notify-send", "-a", "LinDeck", "-i", "input-keyboard", title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def run_once(state) -> bool:
    """Gibt True zurück, wenn neue Profile importiert wurden."""
    done = state.data.setdefault("_auto_imported", {})
    imported_any = False
    for base in search_dirs():
        for kind, p in candidates(base):
            key = str(p)
            stamp = _stamp(p)
            if done.get(key) == stamp:
                continue
            try:
                if kind == "msi":
                    if not (paths.ASSETS_DIR / "Plugins").exists():
                        assets.extract(p)
                        log.info("Elgato-Icons aus %s übernommen", p.name)
                        notify("LinDeck", "Standard-Icons aus dem Elgato-Installer übernommen")
                        imported_any = True
                else:
                    ids = profiles.import_path(p)
                    if ids:
                        log.info("%d Profil(e) aus %s importiert", len(ids), p)
                        notify("LinDeck", f"{len(ids)} Stream-Deck-Profil(e) aus „{p.name}“ importiert")
                        imported_any = True
            except Exception as e:  # noqa: BLE001
                log.warning("Automatischer Import von %s fehlgeschlagen: %s", p, e)
            done[key] = stamp
    if imported_any:
        state.save()
    return imported_any


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
