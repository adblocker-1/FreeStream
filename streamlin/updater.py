"""Automatische Updates aus den GitHub-Releases.

Beim Start fragt LinDeck bzw. Linewave nach dem neuesten Release. Ist eine neuere
Version da, lädt das Programm nach Rückfrage die .deb- bzw. .rpm-Dateien der
installierten Pakete (streamlin, lindeck, linewave) herunter, prüft ihre
SHA-256-Summe und installiert sie mit `pkexec apt-get install` bzw.
`pkexec dnf install` (ohne dnf: `pkexec rpm -U`). Das Paket startet die
Programme danach selbst neu.

Abschalten: „Check for updates at start“ im Menü (config.json "check_updates")
oder STREAMLIN_NO_UPDATE_CHECK=1. Updates gibt es nur für die installierten
Pakete, nicht für einen Start aus dem Quellcode und nie in Flatpak (dort
aktualisiert das Software-Center bzw. `flatpak update`).

Kanal (config.json "update_channel"): "stable" (Standard) nimmt /releases/latest,
"beta" das neueste Release der Liste einschließlich Vorabversionen (nie Entwürfe).
Ältere Versionen zum Zurückstufen kommen aus derselben Liste.
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
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=100"
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
PACKAGES = ("streamlin", "lindeck", "linewave")
LATER_SECONDS = 15 * 60  # „Später“: das zweite Programm fragt beim gleichen Anmelden nicht noch einmal
STATE_FILE = "update.json"
CHANNELS = ("stable", "beta")


# ------------------------------------------------------------------ Einstellung
def _read_config():
    try:
        data = json.loads(paths.CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_config(key, value):
    data = _read_config()
    data[key] = value
    paths.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = paths.CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(paths.CONFIG_FILE)


def configured():
    """Einstellung „Beim Start nach Updates suchen“ (Standard: ja)."""
    return _read_config().get("check_updates", True) is not False


def enabled():
    """Beim Start nach Updates suchen? STREAMLIN_NO_UPDATE_CHECK schaltet es für einen Start ab."""
    return not os.environ.get("STREAMLIN_NO_UPDATE_CHECK") and configured()


def set_enabled(on):
    _write_config("check_updates", bool(on))


def channel():
    """Update-Kanal: "stable" (Standard) oder "beta"."""
    ch = _read_config().get("update_channel")
    return ch if ch in CHANNELS else "stable"


def set_channel(ch):
    _write_config("update_channel", ch if ch in CHANNELS else "stable")


def in_flatpak():
    """Läuft das Programm in Flatpak? Dann nie selbst aktualisieren."""
    return bool(os.environ.get("FLATPAK_ID")) or os.path.exists("/.flatpak-info")


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


def version_key(text):
    """Sortierschlüssel: Vorabversion (0.20.0-beta.1) vor der fertigen Version (0.20.0)."""
    text = (text or "").strip().lstrip("vV").split("+")[0].replace("~", "-")
    main, _sep, pre = text.partition("-")
    nums = parse_version(main)
    if not pre:
        return nums, (1, ())
    parts = tuple((0, int(p), "") if p.isdigit() else (1, 0, p.lower()) for p in re.findall(r"\d+|[A-Za-z]+", pre))
    return nums, (0, parts)


def is_newer(candidate, current=__version__):
    a, b = version_key(candidate), version_key(current)
    return bool(a[0]) and a > b


def is_older(candidate, current=__version__):
    a, b = version_key(candidate), version_key(current)
    return bool(a[0]) and a < b


def _query(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def dpkg_packages():
    """Per dpkg installierte Streamlin-Pakete: {Paket: Version}."""
    if not shutil.which("dpkg-query"):
        return {}
    found = {}
    for line in _query(["dpkg-query", "-W", "-f=${Package}\t${Status}\t${Version}\n", *PACKAGES]).splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0] in PACKAGES and parts[1].endswith(" installed"):
            found[parts[0]] = parts[2]
    return found


def rpm_packages():
    """Per rpm installierte Streamlin-Pakete: {Paket: Version} („package … is not installed“ fällt weg)."""
    if not shutil.which("rpm"):
        return {}
    found = {}
    for line in _query(["rpm", "-q", "--qf", "%{NAME}\t%{VERSION}\n", *PACKAGES]).splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[0] in PACKAGES and parts[1]:
            found[parts[0]] = parts[1]
    return found


def _runs_from_opt():
    here = Path(__file__).resolve()
    return len(here.parts) >= 3 and here.parts[1] == "opt" and here.parts[2] in PACKAGES


def detect_installed():
    """(Paketformat, {Paket: Version}): ("deb" | "rpm", …) oder (None, {}).

    Leer, wenn das Programm nicht aus /opt/<Paket> läuft (z. B. aus dem Quellcode)
    oder in Flatpak. dpkg hat Vorrang; rpm nur, wenn dpkg die Pakete nicht kennt.
    """
    if in_flatpak() or not _runs_from_opt():
        return None, {}
    found = dpkg_packages()
    if found:
        return "deb", found
    found = rpm_packages()
    if found:
        return "rpm", found
    return None, {}


def installed_packages():
    """Installierte Streamlin-Pakete, aus denen dieses Programm läuft: {Paket: Version}."""
    return detect_installed()[1]


# ------------------------------------------------------------------ Release abfragen
class Update:
    """Ein Release mit den passenden .deb- bzw. .rpm-Dateien."""

    def __init__(self, version, notes, page, files, kind="deb", date="", prerelease=False):
        self.version = version  # z. B. "0.17.0"
        self.notes = notes
        self.page = page  # Release-Seite im Browser
        self.files = files  # [{"name", "url", "sha256", "size"}]
        self.kind = kind  # "deb" oder "rpm"
        self.date = date  # Veröffentlichung, z. B. "2026-10-01"
        self.prerelease = prerelease

    @property
    def downgrade(self):
        return is_older(self.version)


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


def asset_names(pkg, version, kind="deb"):
    """Mögliche Dateinamen eines Pakets im Release (Vorabversion auch mit ~ statt -)."""
    out = []
    for v in dict.fromkeys((version, version.replace("-", "~"))):
        out.append(f"{pkg}_{v}_all.deb" if kind == "deb" else f"{pkg}-{v}-1.noarch.rpm")
    return out


def release_files(release, packages, kind="deb"):
    """Dateien der Pakete eines Releases ([{name, url, sha256, size}]); None, wenn eines fehlt
    oder eine Datei nicht sicher ist (fremde Adresse, keine SHA-256-Summe)."""
    version = (release.get("tag_name") or "").lstrip("v")
    assets = {a.get("name"): a for a in release.get("assets") or []}
    files = []
    for pkg in sorted(packages):
        a = next((assets[n] for n in asset_names(pkg, version, kind) if n in assets), None)
        if not a:
            log.info("Release %s has no %s package %s", version, kind, pkg)
            return None
        url = a.get("browser_download_url") or ""
        digest = a.get("digest") or ""
        if not url.startswith(DOWNLOAD_PREFIX) or not digest.startswith("sha256:"):
            return None
        files.append({"name": a["name"], "url": url, "sha256": digest.split(":", 1)[1].lower(),
                      "size": int(a.get("size") or 0)})
    return files


def make_update(release, packages, kind="deb"):
    """Update für genau dieses Release (ohne Versionsvergleich) oder None."""
    if not release or release.get("draft") or not packages:
        return None
    files = release_files(release, packages, kind)
    if files is None:
        return None
    version = (release.get("tag_name") or "").lstrip("v")
    return Update(version, english_notes(release.get("body")), release.get("html_url") or "", files, kind=kind,
                  date=(release.get("published_at") or release.get("created_at") or "")[:10],
                  prerelease=bool(release.get("prerelease")))


def release_update(release, packages, current=__version__, kind="deb", beta=False):
    """Update aus der API-Antwort, wenn sie neuer ist und alle Pakete enthält; sonst None."""
    if not release or release.get("draft") or (release.get("prerelease") and not beta):
        return None
    version = (release.get("tag_name") or "").lstrip("v")
    if not packages or not is_newer(version, current):
        return None
    return make_update(release, packages, kind)


def published(releases, beta=True):
    """Veröffentlichte Releases (nie Entwürfe), neueste zuerst; Vorabversionen nur mit beta."""
    out = [r for r in releases or [] if isinstance(r, dict) and r.get("tag_name") and not r.get("draft")
           and (beta or not r.get("prerelease"))]
    return sorted(out, key=lambda r: version_key(r["tag_name"]), reverse=True)


def newest_update(releases, packages, current=__version__, kind="deb", beta=True):
    """Beta-Kanal: das neueste Release der Liste (auch eine Vorabversion), wenn es neuer ist."""
    rel = published(releases, beta)
    return release_update(rel[0], packages, current, kind, beta) if rel else None


def earlier_versions(releases, packages, current=__version__, kind="deb", beta=False):
    """Ältere Versionen zum Zurückstufen, neueste zuerst (nur Releases mit allen Paketen)."""
    out = []
    for r in published(releases, beta):
        if is_older(r["tag_name"], current):
            up = make_update(r, packages, kind)
            if up:
                out.append(up)
    return out


def fetch_releases(timeout=10):
    """Liste der Releases (GitHub-API). Wirft OSError/ValueError bei Netzfehlern."""
    data = _get_json(RELEASES_URL, timeout)
    if not isinstance(data, list):
        raise ValueError("unexpected answer from GitHub")
    return data


def check(packages=None, timeout=10, kind=None, beta=None):
    """Neuere Version für die installierten Pakete oder None. Wirft OSError/ValueError bei Netzfehlern."""
    if packages is None:
        kind, packages = detect_installed()
    if not packages:
        return None
    kind = kind or "deb"
    beta = channel() == "beta" if beta is None else beta
    if beta:
        return newest_update(fetch_releases(timeout), packages, kind=kind)
    return release_update(_get_json(API_URL, timeout), packages, kind=kind)


def rollback_choices(timeout=10):
    """Ältere Versionen für die installierten Pakete (fragt GitHub)."""
    kind, packages = detect_installed()
    if not packages:
        return []
    return earlier_versions(fetch_releases(timeout), packages, kind=kind, beta=channel() == "beta")


# ------------------------------------------------------------------ Herunterladen und installieren
class UpdateError(Exception):
    pass


def download(update, dest=None, progress=None, timeout=30):
    """Lädt alle Dateien, prüft SHA-256 und gibt die Pfade zurück. progress(done, total) optional."""
    dest = Path(dest or paths.STATE_DIR / "updates")
    dest.mkdir(parents=True, exist_ok=True)
    for pattern in ("*.deb", "*.rpm"):
        for old in dest.glob(pattern):
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


def install_command(files, kind="deb", downgrade=False):
    """Befehl, der die Pakete mit Administratorrechten installiert, oder None (kein pkexec)."""
    pkexec = shutil.which("pkexec")
    if not pkexec:
        return None
    names = [str(Path(f).resolve()) for f in files]
    if kind == "rpm":
        dnf = shutil.which("dnf")
        if dnf:
            if downgrade:
                return [pkexec, dnf, "downgrade", "-y", "--allowerasing", *names]
            return [pkexec, dnf, "install", "-y", *names]
        rpm = shutil.which("rpm") or "/usr/bin/rpm"
        return [pkexec, rpm, "-U", *(["--oldpackage"] if downgrade else []), *names]
    apt = shutil.which("apt-get") or "/usr/bin/apt-get"
    # ohne Terminal: bei geänderten Konfigurationsdateien nie nachfragen, die eigene Fassung behalten
    return [pkexec, apt, "install", "-y", "--allow-downgrades", "-o", "Dpkg::Options::=--force-confdef",
            "-o", "Dpkg::Options::=--force-confold", *names]


def start_install(files, version, kind="deb", downgrade=False):
    """Startet die Installation in einer eigenen Sitzung (überlebt das Beenden des Programms
    durch das Paket). Gibt den Prozess zurück; None, wenn kein pkexec da ist."""
    cmd = install_command(files, kind, downgrade)
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
