"""Sicherung und Wiederherstellung aller Benutzerdaten von FreeDeck und FreeWave.

Eine Sicherung ist ein ZIP-Archiv (Endung .freestream-backup) mit manifest.json und
drei Bereichen:

  config/  – CONFIG_DIR (config.json: Einstellungen beider Programme, Mixer-Kanäle, Effekte …)
  data/    – DATA_DIR (Profile mit Bildern/Hintergründen, Ressourcen, Symbole, Plugins, Plugin-Daten)
  state/   – nur die Einstellungsdateien aus STATE_DIR (keine Logs, Prozessdaten, Updates)

Symbolische Links werden nicht verfolgt, sondern (nur wenn sie innerhalb des Bereichs bleiben)
als Link im Manifest gespeichert – so landet nichts doppelt und nichts außerhalb im Archiv.
"""
import json
import logging
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from . import __version__, desktop, paths
from .i18n import _, pgettext

log = logging.getLogger("freestream.backup")

EXT = ".freestream-backup"
FORMAT = "freestream-backup"
OLD_EXT, OLD_FORMATS = ".streamlin-backup", ("streamlin-backup",)  # Sicherungen von vor der Umbenennung
FORMAT_VERSION = 1
MANIFEST = "manifest.json"
SECTIONS = ("config", "data", "state")
# Aus STATE_DIR nur echte Einstellungen (aktives Profil/Helligkeit, Low Cut/Clipguard)
STATE_FILES = ("state.json", "wavemic.json")
# Nie sichern: Caches, Laufzeitdaten, Downloads, eigene Sicherungen
SKIP_DIRS = {"__pycache__", ".cache", "cache", "Cache", "logs", "updates", "backups"}
SKIP_SUFFIXES = (".pyc", ".pyo", ".tmp", ".log", ".lock", ".pid")
PLUGIN_DIRS = ("plugins",)
SAFETY_DIR = "backups"
KEEP_SAFETY = 3
MAX_FILES = 200_000
MAX_BYTES = 16 << 30          # 16 GiB entpackt
MAX_RATIO = 200               # Zip-Bombe: Kompressionsfaktor bei großen Dateien
CHUNK = 1 << 20
STAGE = ".freestream-restore-"


class BackupError(Exception):
    """Ungültige Sicherung oder Fehler beim Sichern/Wiederherstellen (Text für den Benutzer)."""


# ------------------------------------------------------------------ Hilfen
def _roots():
    """Bereich -> echter Ordner (aufgelöst, damit alte „freedeck“-Verweise nicht stören)."""
    return {"config": Path(os.path.realpath(paths.CONFIG_DIR)),
            "data": Path(os.path.realpath(paths.DATA_DIR)),
            "state": Path(os.path.realpath(paths.STATE_DIR))}


def safety_dir():
    return Path(paths.STATE_DIR) / SAFETY_DIR


def default_name(now=None):
    return f"freestream-{(now or datetime.now()).strftime('%Y-%m-%d')}{EXT}"


def _inside(path, root):
    path, root = os.path.normpath(path), os.path.normpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _skip(name, is_dir):
    if is_dir:
        return name in SKIP_DIRS or name.startswith(STAGE)
    return name.endswith(SKIP_SUFFIXES)


def _walk(section, root, plugins):
    """Liefert (archivname, datei) und (archivname, linkziel) eines Bereichs."""
    files, links = [], {}
    if section == "state":
        for n in STATE_FILES:
            f = root / n
            if f.is_file() and not f.is_symlink():
                files.append((f"state/{n}", f))
        return files, links
    if not root.is_dir():
        return files, links
    real = os.path.realpath(root)
    for d, dirs, names in os.walk(root, followlinks=False):
        rel_d = Path(d).relative_to(root)
        keep = []
        for n in sorted(dirs):
            p = Path(d) / n
            if _skip(n, True) or (section == "data" and not rel_d.parts and n in PLUGIN_DIRS and not plugins):
                continue
            if p.is_symlink():
                _link(section, root, real, p, links)
            else:
                keep.append(n)
        dirs[:] = keep
        for n in sorted(names):
            p = Path(d) / n
            if _skip(n, False):
                continue
            if p.is_symlink():
                _link(section, root, real, p, links)
            elif p.is_file():
                files.append((f"{section}/{(rel_d / n).as_posix()}", p))
    return files, links


def _link(section, root, real, p, links):
    """Verweis merken, wenn er im Bereich bleibt (Ziel ist dann ohnehin gesichert)."""
    try:
        target = os.readlink(p)
    except OSError:
        return
    resolved = os.path.realpath(p)
    if not _inside(resolved, real) or not os.path.exists(resolved):
        log.info(_("Skipping link outside the data folder: {path}").format(path=p))
        return
    rel = os.path.relpath(resolved, os.path.realpath(p.parent)) if os.path.isabs(target) else target
    links[f"{section}/{p.relative_to(root).as_posix()}"] = rel


def _names(folder, pattern):
    """Anzeigenamen aus manifest.json von Profilen bzw. Plugins."""
    out = []
    if not folder.is_dir():
        return out
    for d in sorted(folder.glob(pattern)):
        name = d.stem
        try:
            name = json.loads((d / "manifest.json").read_text(encoding="utf-8")).get("Name") or name
        except (OSError, ValueError, AttributeError):
            pass
        out.append(str(name))
    return out


# ------------------------------------------------------------------ Sichern
def create(dest, plugins=True, progress=None):
    """Sicherung schreiben. progress(fertig, gesamt) wird zwischendurch aufgerufen. Gibt das Manifest zurück."""
    dest = Path(dest).expanduser()
    if dest.suffix != EXT and not dest.name.endswith(EXT):
        dest = dest.with_name(dest.name + EXT)
    roots = _roots()
    files, links = [], {}
    for sec in SECTIONS:
        f, lk = _walk(sec, roots[sec], plugins)
        files += f
        links.update(lk)
    dest_real = os.path.realpath(dest)
    files = [(a, p) for a, p in files if os.path.realpath(p) != dest_real]
    total = sum(p.stat().st_size for __, p in files)
    data = roots["data"]
    manifest = {
        "format": FORMAT, "format_version": FORMAT_VERSION, "freestream_version": __version__,
        "created": datetime.now().astimezone().isoformat(timespec="seconds"), "host": socket.gethostname(),
        "plugins_included": bool(plugins),
        "contents": {
            "profiles": _names(data / "profiles", "*.sdProfile"),
            "plugins": _names(data / "plugins", "*.sdPlugin") if plugins else [],
            "config": (roots["config"] / "config.json").is_file(),
            "files": len(files), "bytes": total,
        },
        "files": [a for a, __ in files],
        "links": links,
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.part")
    done = 0
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.writestr(MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False))
            for arc, p in files:
                z.write(p, arc)
                done += p.stat().st_size
                if progress:
                    progress(done, total)
        tmp.replace(dest)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise BackupError(_("The backup could not be written: {error}").format(error=e)) from e
    manifest["path"] = str(dest)
    return manifest


# ------------------------------------------------------------------ Prüfen
def _safe_name(name):
    """Archivname relativ, ohne „..“, Laufwerk oder Backslash; Bereich bekannt."""
    if not name or "\\" in name or "\0" in name or name.startswith("/") or ":" in name.split("/")[0]:
        return False
    parts = PurePosixPath(name).parts
    if any(p in ("", ".", "..") for p in parts) or name.endswith("/"):
        return False
    if name == MANIFEST:
        return True
    if len(parts) < 2 or parts[0] not in SECTIONS:
        return False
    return parts[0] != "state" or (len(parts) == 2 and parts[1] in STATE_FILES)


def inspect(path):
    """Sicherung prüfen (Manifest, Pfade, Größe). Gibt das Manifest zurück oder wirft BackupError."""
    path = Path(path).expanduser()
    if not zipfile.is_zipfile(path):
        raise BackupError(_("This file is not a FreeStream backup."))
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            try:
                manifest = json.loads(z.read(MANIFEST).decode("utf-8"))
            except (KeyError, ValueError, UnicodeDecodeError) as e:
                raise BackupError(_("This file is not a FreeStream backup.")) from e
    except (OSError, zipfile.BadZipFile) as e:
        raise BackupError(_("The backup could not be read: {error}").format(error=e)) from e
    if not isinstance(manifest, dict) or manifest.get("format") not in (FORMAT, *OLD_FORMATS):
        raise BackupError(_("This file is not a FreeStream backup."))
    if not isinstance(manifest.get("format_version"), int) or manifest["format_version"] > FORMAT_VERSION:
        raise BackupError(_("This backup was made with a newer version of FreeStream. Please update first."))
    if len(infos) > MAX_FILES:
        raise BackupError(_("The backup contains too many files."))
    seen, total = set(), 0
    for i in infos:
        if not _safe_name(i.filename) or i.filename in seen:
            raise BackupError(_("The backup contains an unsafe path: {path}").format(path=i.filename))
        if stat.S_ISLNK(i.external_attr >> 16):
            raise BackupError(_("The backup contains an unsafe path: {path}").format(path=i.filename))
        seen.add(i.filename)
        total += i.file_size
        if i.file_size > (64 << 20) and i.file_size > MAX_RATIO * max(1, i.compress_size):
            raise BackupError(_("The backup is damaged or too large."))
    if total > MAX_BYTES:
        raise BackupError(_("The backup is damaged or too large."))
    listed = manifest.get("files")
    if not isinstance(listed, list) or set(listed) != seen - {MANIFEST}:
        raise BackupError(_("The backup is incomplete or damaged."))
    links = manifest.get("links") or {}
    if not isinstance(links, dict):
        raise BackupError(_("The backup is incomplete or damaged."))
    for name, target in links.items():
        if not _safe_name(name) or name.startswith("state/") or not isinstance(target, str) or not _link_ok(
                name, target):
            raise BackupError(_("The backup contains an unsafe path: {path}").format(path=name))
    manifest["total_bytes"] = total
    manifest.setdefault("contents", {})
    return manifest


def _link_ok(name, target):
    """Linkziel relativ und innerhalb seines Bereichs."""
    if not target or target.startswith("/") or "\\" in target or "\0" in target:
        return False
    depth = len(PurePosixPath(name).parts) - 2  # Ordnertiefe des Links im Bereich
    for part in target.split("/"):
        if part == "..":
            depth -= 1
            if depth < 0:
                return False
        elif part not in ("", "."):
            depth += 1
    return True


def describe(manifest):
    """Kurzbeschreibung des Inhalts (für Dialog und Befehlszeile)."""
    c = manifest.get("contents") or {}
    created = manifest.get("created") or "?"
    try:
        created = datetime.fromisoformat(created).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        pass
    lines = [_("Created: {date} on {host}").format(date=created, host=manifest.get("host") or "?"),
             _("FreeStream version: {version}").format(version=manifest.get("freestream_version") or manifest.get("streamlin_version") or "?")]
    profiles = c.get("profiles") or []
    lines.append(_("Profiles: {count}").format(count=len(profiles)) +
                 (f" ({', '.join(map(str, profiles[:6]))}{', …' if len(profiles) > 6 else ''})" if profiles else ""))
    if manifest.get("plugins_included", True):
        lines.append(_("Plugins: {count}").format(count=len(c.get("plugins") or [])))
    else:
        lines.append(_("Plugins: not included (installed plugins are kept)"))
    yes_no = pgettext("answer", "yes") if c.get("config") else pgettext("answer", "no")
    lines.append(_("Settings: {state}").format(state=yes_no))
    lines.append(_("Size: {size}").format(size=human_size(manifest.get("total_bytes", c.get("bytes", 0)))))
    return lines


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# ------------------------------------------------------------- Wiederherstellen
def safety_backup(plugins=True):
    """Aktuellen Stand vor dem Wiederherstellen sichern; nur die letzten KEEP_SAFETY bleiben."""
    d = safety_dir()
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = d / f"before-restore-{stamp}{EXT}"
    i = 1
    while dest.exists():
        dest = d / f"before-restore-{stamp}-{i}{EXT}"
        i += 1
    create(dest, plugins=plugins)
    for old in sorted(d.glob(f"before-restore-*{EXT}"), key=lambda f: f.stat().st_mtime)[:-KEEP_SAFETY]:
        old.unlink(missing_ok=True)
    return dest


def _extract(z, info, dest, budget):
    """Eine Datei entpacken; bricht ab, wenn mehr kommt als angegeben."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with z.open(info) as src, open(dest, "wb") as out:
        while True:
            buf = src.read(CHUNK)
            if not buf:
                break
            n += len(buf)
            if n > info.file_size or n > budget:
                raise BackupError(_("The backup is damaged or too large."))
            out.write(buf)
    mode = (info.external_attr >> 16) & 0o777
    if mode:
        os.chmod(dest, mode | 0o600)
    return n


def restore(path, progress=None, safety=True):
    """Sicherung einspielen. Gibt (Manifest, Pfad der Sicherheitskopie oder None) zurück.

    Die laufenden Programme sollten vorher beendet sein (stop_programs) bzw. danach neu starten.
    """
    manifest = inspect(path)
    with_plugins = bool(manifest.get("plugins_included", True))
    roots = _roots()
    stages = {}
    for sec in SECTIONS:
        if sec == "state":
            continue
        roots[sec].mkdir(parents=True, exist_ok=True)
        stages[sec] = Path(tempfile.mkdtemp(prefix=STAGE, dir=roots[sec]))
    try:
        # 1. Alles in Zwischenordner im Ziel entpacken (gleiches Dateisystem, wird übersprungen)
        state_stage = Path(tempfile.mkdtemp(prefix=STAGE, dir=stages["data"]))
        stage_of = dict(stages, state=state_stage)
        free = shutil.disk_usage(stages["data"]).free
        if manifest["total_bytes"] > free:
            raise BackupError(_("Not enough free disk space to restore this backup."))
        total, done = manifest["total_bytes"], 0
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if info.filename == MANIFEST:
                    continue
                sec, rest = info.filename.split("/", 1)
                done += _extract(z, info, stage_of[sec] / "new" / rest, MAX_BYTES - done)
                if progress:
                    progress(done, total)
        for name, target in (manifest.get("links") or {}).items():
            sec, rest = name.split("/", 1)
            link = stage_of[sec] / "new" / rest
            link.parent.mkdir(parents=True, exist_ok=True)
            if not link.exists() and not link.is_symlink():
                os.symlink(target, link)
        for name in manifest.get("links") or {}:  # Link-Ketten dürfen den Bereich auch nicht verlassen
            sec, rest = name.split("/", 1)
            base = os.path.realpath(stage_of[sec] / "new")
            if not _inside(os.path.realpath(stage_of[sec] / "new" / rest), base):
                raise BackupError(_("The backup contains an unsafe path: {path}").format(path=name))
        # 2. Sicherheitskopie des aktuellen Stands
        backup = safety_backup(plugins=with_plugins) if safety else None
        # 3. Austauschen
        for sec in ("config", "data"):
            keep = {SAFETY_DIR} | (set() if with_plugins else set(PLUGIN_DIRS))
            _swap(roots[sec], stages[sec], keep)
        st = roots["state"]
        st.mkdir(parents=True, exist_ok=True)
        for n in STATE_FILES:
            src = state_stage / "new" / n
            if src.is_file():
                shutil.copyfile(src, st / n)
            else:
                (st / n).unlink(missing_ok=True)
    except OSError as e:
        raise BackupError(_("The backup could not be restored: {error}").format(error=e)) from e
    finally:
        for s in stages.values():
            shutil.rmtree(s, ignore_errors=True)
    return manifest, backup


def _swap(root, stage, keep):
    """Inhalt von root durch stage/new ersetzen; bei Fehlern zurückrollen."""
    new, old = stage / "new", stage / "old"
    new.mkdir(exist_ok=True)
    old.mkdir(exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    moved, placed = [], []
    try:
        for p in list(root.iterdir()):
            if p.name in keep or p.name.startswith(STAGE):
                continue
            # eigene Verweise nach außerhalb (nicht gesichert) bleiben erhalten
            if p.is_symlink() and not (new / p.name).exists() and not _inside(os.path.realpath(p),
                                                                                os.path.realpath(root)):
                continue
            p.rename(old / p.name)
            moved.append(p.name)
        for p in list(new.iterdir()):
            if p.name in keep:
                continue
            p.rename(root / p.name)
            placed.append(p.name)
    except OSError:
        for n in placed:
            (root / n).rename(new / n)
        for n in moved:
            (old / n).rename(root / n)
        raise


# ------------------------------------------------------------- Programme neu starten
def _lindeck_pid():
    pid = desktop.running_instance("app.json")
    if pid:
        return pid, bool(desktop._app_info("app.json").get("gui"))
    try:
        pid = int(Path(paths.PID_FILE).read_text())
        cmd = open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="ignore")
        if pid != os.getpid() and any(n in cmd for n in ("freestream", "freedeck", "streamlin", "lindeck")):
            return pid, False
    except (OSError, ValueError):
        pass
    return None, False


def running_programs():
    """[(programm, pid, mit_fenster)] der laufenden FreeDeck/FreeWave (ohne diesen Prozess)."""
    out = []
    pid, gui = _lindeck_pid()
    if pid and pid != os.getpid():
        out.append(("freedeck", pid, gui))
    pid = desktop.running_instance("freewave.json")
    if pid and pid != os.getpid():
        out.append(("freewave", pid, bool(desktop._app_info("freewave.json").get("gui"))))
    return out


def stop_programs(timeout=10):
    """Laufende Programme sauber beenden (sie speichern dabei noch offene Änderungen)."""
    progs = running_programs()
    for __, pid, __g in progs:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    end = time.time() + timeout
    for __, pid, __g in progs:
        while desktop._alive(pid) and time.time() < end:
            time.sleep(0.1)
    return progs


def start_programs(progs):
    """Vorher beendete Programme wieder starten (unsichtbar wie beim Anmelden)."""
    for name, __, gui in progs:
        exe = shutil.which(name)
        if name == "freedeck":
            cmd = ([exe] if exe else [sys.executable, "-m", "freestream"]) + ["run"] + ([] if gui else ["--no-gui"])
        else:
            cmd = ([exe] if exe else [sys.executable, "-m", "freestream.freewave"]) + ["--hidden"]
        try:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError as e:
            log.warning(_("Could not start {program}: {error}").format(program=name, error=e))
