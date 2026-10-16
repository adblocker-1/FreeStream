"""Automatische Updates aus den GitHub-Releases.

Beim Start fragt LinDeck bzw. Linewave nach dem neuesten Release. Ist eine neuere
Version da, lädt das Programm nach Rückfrage die .deb-Dateien der installierten
Pakete (streamlin, lindeck, linewave) herunter, prüft ihre SHA-256-Summe und
installiert sie mit `pkexec apt-get install`. Das Paket startet die Programme
danach selbst neu.

Abschalten: „Check for updates at start“ im Menü (config.json "check_updates")
oder STREAMLIN_NO_UPDATE_CHECK=1. Updates gibt es nur für die installierten
.deb-Pakete, nicht für einen Start aus dem Quellcode.
"""
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from . import __version__, paths

log = logging.getLogger("streamlin.updater")

REPO = "adblocker-1/Streamlin"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
PACKAGES = ("streamlin", "lindeck", "linewave")
LATER_SECONDS = 15 * 60  # „Später“: das zweite Programm fragt beim gleichen Anmelden nicht noch einmal
STATE_FILE = "update.json"


# ------------------------------------------------------------------ Einstellung
def _read_config():
    try:
        data = json.loads(paths.CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def enabled():
    """Beim Start nach Updates suchen? (Standard: ja)"""
    if os.environ.get("STREAMLIN_NO_UPDATE_CHECK"):
        return False
    return _read_config().get("check_updates", True) is not False


def set_enabled(on):
    data = _read_config()
    data["check_updates"] = bool(on)
    paths.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = paths.CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(paths.CONFIG_FILE)


# ------------------------------------------------------------------ Zustand (übersprungen, später, installiert)
def _state_path():
    return paths.STATE_DIR / STATE_FILE


def load_state():
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(**changes):
    data = load_state()
    for k, v in changes.items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v
    paths.ensure_dirs()
    tmp = _state_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(_state_path())


def skip_version(version):
    save_state(skipped=version)


def remind_later(version, now=None):
    save_state(later={"version": version, "until": (now or time.time()) + LATER_SECONDS})


def suppressed(version, now=None):
    """Hat der Benutzer diese Version übersprungen oder gerade „Später“ gewählt?"""
    st = load_state()
    if st.get("skipped") == version:
        return True
    later = st.get("later") or {}
    return later.get("version") == version and (now or time.time()) < later.get("until", 0)


def just_updated():
    """Version, auf die gerade aktualisiert wurde (einmalig), sonst None."""
    st = load_state()
    if st.get("installing") and st["installing"] == __version__:
        save_state(installing=None)
        return __version__
    return None


# ------------------------------------------------------------------ Versionen und Pakete
def parse_version(text):
    nums = re.findall(r"\d+", (text or "").split("-")[0].split("+")[0])
    return tuple(int(n) for n in nums[:4])


def is_newer(candidate, current=__version__):
    a, b = parse_version(candidate), parse_version(current)
    return bool(a) and a > b


def installed_packages():
    """Installierte Streamlin-Pakete, aus denen dieses Programm läuft: {Paket: Version}.

    Leer, wenn das Programm nicht aus /opt/<Paket> läuft (z. B. aus dem Quellcode).
    """
    here = Path(__file__).resolve()
    if len(here.parts) < 3 or here.parts[1] != "opt" or here.parts[2] not in PACKAGES:
        return {}
    if not shutil.which("dpkg-query"):
        return {}
    try:
        out = subprocess.run(["dpkg-query", "-W", "-f=${Package}\t${Status}\t${Version}\n", *PACKAGES],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    found = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[1].endswith(" installed"):
            found[parts[0]] = parts[2]
    return found


# ------------------------------------------------------------------ Release abfragen
class Update:
    """Ein verfügbares Release mit den passenden .deb-Dateien."""

    def __init__(self, version, notes, page, files):
        self.version = version  # z. B. "0.17.0"
        self.notes = notes
        self.page = page  # Release-Seite im Browser
        self.files = files  # [{"name", "url", "sha256", "size"}]


def _get_json(url, timeout):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": f"Streamlin/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - feste https-Adresse
        return json.loads(r.read().decode("utf-8"))


def english_notes(body):
    """Nur der englische Teil der Release-Notizen (vor dem „---“), ohne die Überschrift „English“."""
    text = (body or "").split("\n---")[0]
    text = re.sub(r"^\s*\*\*English\*\*\s*$", "", text, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def release_update(release, packages, current=__version__):
    """Update aus der API-Antwort, wenn sie neuer ist und alle Pakete enthält; sonst None."""
    if not release or release.get("draft") or release.get("prerelease"):
        return None
    version = (release.get("tag_name") or "").lstrip("v")
    if not packages or not is_newer(version, current):
        return None
    assets = {a.get("name"): a for a in release.get("assets") or []}
    files = []
    for pkg in sorted(packages):
        a = assets.get(f"{pkg}_{version}_all.deb")
        if not a:
            log.info("Release %s has no package %s", version, pkg)
            return None
        url = a.get("browser_download_url") or ""
        digest = a.get("digest") or ""
        if not url.startswith(DOWNLOAD_PREFIX) or not digest.startswith("sha256:"):
            return None
        files.append({"name": a["name"], "url": url, "sha256": digest.split(":", 1)[1].lower(),
                      "size": int(a.get("size") or 0)})
    return Update(version, english_notes(release.get("body")), release.get("html_url") or "", files)


def check(packages=None, timeout=10):
    """Neuere Version für die installierten Pakete oder None. Wirft OSError/ValueError bei Netzfehlern."""
    packages = installed_packages() if packages is None else packages
    if not packages:
        return None
    return release_update(_get_json(API_URL, timeout), packages)


# ------------------------------------------------------------------ Herunterladen und installieren
class UpdateError(Exception):
    pass


def download(update, dest=None, progress=None, timeout=30):
    """Lädt alle Dateien, prüft SHA-256 und gibt die Pfade zurück. progress(done, total) optional."""
    dest = Path(dest or paths.STATE_DIR / "updates")
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.glob("*.deb"):
        old.unlink(missing_ok=True)
    total = sum(f["size"] for f in update.files) or 1
    done = 0
    out = []
    for f in update.files:
        if not f["url"].startswith(DOWNLOAD_PREFIX) or "/" in f["name"]:
            raise UpdateError(f["name"])
        target = dest / f["name"]
        h = hashlib.sha256()
        req = urllib.request.Request(f["url"], headers={"User-Agent": f"Streamlin/{__version__}"})
        with urllib.request.urlopen(req, timeout=timeout) as r, open(target, "wb") as fh:  # noqa: S310
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                fh.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(min(done, total), total)
        if h.hexdigest() != f["sha256"]:
            target.unlink(missing_ok=True)
            raise UpdateError(f["name"])
        out.append(target)
    return out


def install_command(files):
    """Befehl, der die Pakete mit Administratorrechten installiert, oder None (kein pkexec)."""
    pkexec = shutil.which("pkexec")
    apt = shutil.which("apt-get") or "/usr/bin/apt-get"
    if not pkexec:
        return None
    # ohne Terminal: bei geänderten Konfigurationsdateien nie nachfragen, die eigene Fassung behalten
    return [pkexec, apt, "install", "-y", "--allow-downgrades", "-o", "Dpkg::Options::=--force-confdef",
            "-o", "Dpkg::Options::=--force-confold", *[str(Path(f).resolve()) for f in files]]


def start_install(files, version):
    """Startet die Installation in einer eigenen Sitzung (überlebt das Beenden des Programms
    durch das Paket). Gibt den Prozess zurück; None, wenn kein pkexec da ist."""
    cmd = install_command(files)
    if not cmd:
        return None
    save_state(installing=version)
    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, start_new_session=True)


# ------------------------------------------------------------------ nur ein Programm fragt
class PromptLock:
    """Läuft LinDeck und Linewave gleichzeitig, fragt nur eines der beiden."""

    def __init__(self):
        self.fh = None

    def acquire(self):
        try:
            paths.ensure_dirs()
            self.fh = open(paths.STATE_DIR / "update.lock", "w")
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.release()
            return False

    def release(self):
        if self.fh is not None:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_UN)
                self.fh.close()
            except OSError:
                pass
            self.fh = None
