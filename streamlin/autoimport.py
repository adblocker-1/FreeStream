"""Automatischer Import: sucht in Downloads, Desktop und USB-Sticks nach
Elgato-Profilen, Backups, ProfilesV2/V3-Ordnern und dem Elgato-Installer.

Bereits importierte Dateien werden sich gemerkt (Pfad + Änderungszeit),
damit nichts doppelt importiert wird.
"""
import getpass
import logging
import subprocess
import time
import zipfile
from pathlib import Path

from . import assets, paths, profiles
from .i18n import _, ngettext

from .desktop import notify

log = logging.getLogger("streamlin.autoimport")

PROFILE_EXT = (".streamdeckprofile", ".streamdeckprofilesbackup")


_XDG_CACHE = {}   # name -> (Zeit, Pfad): xdg-user-dir nicht bei jedem Durchlauf starten
_ZIP_CACHE = {}   # (Pfad, mtime, Größe) -> enthält Profile


def _xdg_dir(name, fallback):
    hit = _XDG_CACHE.get(name)
    if hit and time.monotonic() - hit[0] < 300:
        return hit[1]
    path = _xdg_dir_uncached(name, fallback)
    _XDG_CACHE[name] = (time.monotonic(), path)
    return path


def _xdg_dir_uncached(name, fallback):
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
        st = p.stat()
    except OSError:
        return False
    key = (str(p), st.st_mtime, st.st_size)
    if key not in _ZIP_CACHE:
        if len(_ZIP_CACHE) > 500:
            _ZIP_CACHE.clear()
        _ZIP_CACHE[key] = _zip_scan(p)
    return _ZIP_CACHE[key]


def _zip_scan(p: Path) -> bool:
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
                elif name.endswith(".streamdeckplugin"):
                    found.append(("plugin", e))
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
                        log.info(_("Elgato icons imported from {name}").format(name=p.name))
                        notify("LinDeck", _("Default icons imported from the Elgato installer"))
                        imported_any = True
                elif kind == "plugin":
                    from . import plugins
                    for uid in plugins.install(p):
                        info = plugins.installed().get(uid)
                        log.info(_("Plugin {uuid} installed").format(uuid=uid))
                        name = info.name if info else uid
                        if info and not info.runnable:
                            msg = _("Plugin “{name}” installed – cannot run on Linux ({reason})").format(
                                name=name, reason=info.reason)
                        else:
                            msg = _("Plugin “{name}” installed").format(name=name)
                        notify("LinDeck", msg)
                        imported_any = True
                else:
                    ids = profiles.import_path(p)
                    if ids:
                        n = len(ids)
                        log.info(ngettext("{n} profile imported from {path}", "{n} profiles imported from {path}",
                                          n).format(n=n, path=p))
                        notify("LinDeck", ngettext("{n} profile imported from “{name}”",
                                                   "{n} profiles imported from “{name}”",
                                                   n).format(n=n, name=p.name))
                        imported_any = True
            except Exception as e:  # noqa: BLE001
                log.warning(_("Automatic import of {path} failed: {error}").format(path=p, error=e))
            done[key] = stamp
    if imported_any:
        state.save()
    return imported_any


