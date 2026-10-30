"""Automatische Updates aus den GitHub-Releases.

Beim Start fragt FreeDeck bzw. FreeWave nach dem neuesten Release. Ist eine neuere
Version da, lädt das Programm nach Rückfrage die .deb-Dateien der installierten
Pakete (freestream, freedeck, freewave) herunter, prüft ihre SHA-256-Summe und
installiert sie mit `pkexec apt-get install`. Das Paket startet die Programme
danach selbst neu.

Abschalten: „Check for updates at start“ im Menü (config.json "check_updates")
oder FREESTREAM_NO_UPDATE_CHECK=1. Updates gibt es nur für die installierten
.deb-Pakete, nicht für einen Start aus dem Quellcode.

Kanal (config.json "update_channel"): "stable" (Standard) nimmt /releases/latest,
"beta" das neueste Release der Liste einschließlich Vorabversionen (nie Entwürfe).
Ältere Versionen zum Zurückstufen kommen aus derselben Liste (apt-get mit
--allow-downgrades).
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
from .config import save_keys

log = logging.getLogger("freestream.updater")

# Das Repository hieß bis 0.28 „Streamlin“: erst der neue Name, sonst der alte (GitHub leitet nach dem
# Umbenennen vom alten auf den neuen um). Downloads werden von beiden Adressen angenommen.
REPOS = ("adblocker-1/FreeStream", "adblocker-1/Streamlin")
REPO = REPOS[0]
API_URLS = tuple(f"https://api.github.com/repos/{r}/releases/latest" for r in REPOS)
RELEASES_URLS = tuple(f"https://api.github.com/repos/{r}/releases?per_page=100" for r in REPOS)
API_URL, RELEASES_URL = API_URLS[0], RELEASES_URLS[0]
DOWNLOAD_PREFIXES = tuple(f"https://github.com/{r}/releases/download/" for r in REPOS)
DOWNLOAD_PREFIX = DOWNLOAD_PREFIXES[0]
NEW_PACKAGES = ("freestream", "freedeck", "freewave")
# Pakete unter den Namen vor der Umbenennung: werden beim nächsten Update durch die neuen ersetzt
LEGACY = {"streamlin": "freestream", "lindeck": "freedeck", "linewave": "freewave"}
PACKAGES = NEW_PACKAGES + tuple(LEGACY)
LATER_SECONDS = 15 * 60  # „Später“: das zweite Programm fragt beim gleichen Anmelden nicht noch einmal
STATE_FILE = "update.json"
CHANNELS = ("stable", "beta")
# Welche Ausgabe läuft: die Python-Programme oder die Rust-Programme (Beta, Paket freedeck-rs mit
# /usr/bin/freedeck-rs und /usr/bin/freewave-rs). Wählbar im Update-Dialog (config.json "edition").
EDITIONS = ("python", "rust")
RUST_PACKAGE = "freedeck-rs"
RUST_PROGRAMS = {"freedeck": "freedeck-rs", "freewave": "freewave-rs"}


# ------------------------------------------------------------------ Einstellung
def _read_config():
    try:
        data = json.loads(paths.CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_config(key, value):
    # wie Config: behält die Rechte (Zugangsdaten) und schreibt nie über eine unlesbare Datei
    return save_keys(**{key: value})


def configured():
    """Einstellung „Beim Start nach Updates suchen“ (Standard: ja)."""
    return _read_config().get("check_updates", True) is not False


def enabled():
    """Beim Start nach Updates suchen? FREESTREAM_NO_UPDATE_CHECK schaltet es für einen Start ab."""
    return not os.environ.get("FREESTREAM_NO_UPDATE_CHECK") and configured()


def set_enabled(on):
    _write_config("check_updates", bool(on))


def channel():
    """Update-Kanal: "stable" (Standard) oder "beta"."""
    ch = _read_config().get("update_channel")
    return ch if ch in CHANNELS else "stable"


def set_channel(ch):
    _write_config("update_channel", ch if ch in CHANNELS else "stable")


def edition():
    """Gewählte Ausgabe: "python" (Standard) oder "rust". FREESTREAM_EDITION überschreibt sie für einen Start."""
    e = os.environ.get("FREESTREAM_EDITION") or _read_config().get("edition")
    return e if e in EDITIONS else "python"


def set_edition(e):
    _write_config("edition", e if e in EDITIONS else "python")


def rust_program(name):
    """Pfad des Rust-Programms zu „freedeck“/„freewave“, wenn die Rust-Ausgabe installiert ist; sonst None."""
    exe = RUST_PROGRAMS.get(name)
    return shutil.which(exe) if exe else None


def redirect_to_rust(name, argv):
    """Rust gewählt: die Python-Programme starten stattdessen die Rust-Programme (Menü, Autostart, Neustart
    nach dem Update). Nur Fenster und Hintergrunddienst, die Kommandozeile bleibt Python.
    FREESTREAM_PYTHON=1 erzwingt Python. Kehrt nur zurück, wenn nichts umgeleitet wird."""
    if os.environ.get("FREESTREAM_PYTHON") or edition() != "rust":
        return
    exe = rust_program(name)
    if not exe:
        return
    args = list(argv)
    cmd = next((a for a in args if not a.startswith("-")), "")
    if name == "freedeck":
        if cmd not in ("", "open", "edit", "run", "quit") or "--no-gui" in args:
            return
        args = ["run"] if cmd == "run" else (["quit"] if cmd == "quit" else [])
    else:
        if cmd:
            return
        args = [a for a in args if a in ("--hidden", "--quit")]
    log.info("Rust edition selected: starting %s", exe)
    os.execv(exe, [exe, *args])


def _deb_arch():
    return (_query(["dpkg", "--print-architecture"]).strip() or
            {"x86_64": "amd64", "aarch64": "arm64"}.get(os.uname().machine, os.uname().machine))


def _distro():
    """System aus /etc/os-release, z. B. "ubuntu-24.04" (Name der Rust-Pakete im Release)."""
    try:
        fields = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
    except OSError:
        return ""
    ident = fields.get("ID", "").strip().strip('"')
    ver = fields.get("VERSION_ID", "").strip().strip('"')
    return f"{ident}-{ver}" if ident and ver else ""


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
    """Per dpkg installierte FreeStream-Pakete: {Paket: Version}."""
    if not shutil.which("dpkg-query"):
        return {}
    found = {}
    for line in _query(["dpkg-query", "-W", "-f=${Package}\t${Status}\t${Version}\n", *PACKAGES,
                        RUST_PACKAGE]).splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0] in PACKAGES + (RUST_PACKAGE,) and parts[1].endswith(" installed"):
            found[parts[0]] = parts[2]
    return found


def _runs_from_opt():
    here = Path(__file__).resolve()
    return len(here.parts) >= 3 and here.parts[1] == "opt" and here.parts[2] in PACKAGES


def installed_packages():
    """Installierte FreeStream-Pakete, aus denen dieses Programm läuft: {Paket: Version}.

    Leer, wenn das Programm nicht aus /opt/<Paket> läuft (z. B. aus dem Quellcode).
    """
    if not _runs_from_opt():
        return {}
    return dpkg_packages()


# ------------------------------------------------------------------ Release abfragen
class Update:
    """Ein Release mit den passenden .deb-Dateien."""

    def __init__(self, version, notes, page, files, date="", prerelease=False, release=None):
        self.version = version  # z. B. "0.17.0"
        self.notes = notes
        self.page = page  # Release-Seite im Browser
        self.files = files  # [{"name", "url", "sha256", "size"}]
        self.date = date  # Veröffentlichung, z. B. "2026-10-01"
        self.prerelease = prerelease
        self.release = release  # Antwort der API (für den Wechsel der Ausgabe im Dialog)

    @property
    def downgrade(self):
        return is_older(self.version)


def _get_json(url, timeout):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": f"FreeStream/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - feste https-Adresse
        return json.loads(r.read().decode("utf-8"))


def _get_first(urls, timeout):
    """Die erste Adresse, die antwortet (neuer Repo-Name, sonst der alte)."""
    import urllib.error
    for url in urls[:-1]:
        try:
            return _get_json(url, timeout)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    return _get_json(urls[-1], timeout)


def english_notes(body):
    """Nur der englische Teil der Release-Notizen (vor dem „---“), ohne die Überschrift „English“."""
    text = (body or "").split("\n---")[0]
    text = re.sub(r"^\s*\*\*English\*\*\s*$", "", text, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def asset_names(pkg, version, arch=None, distro=None):
    """Mögliche Dateinamen eines Pakets im Release (Vorabversion auch mit ~ statt -). Das Rust-Paket gibt es je
    Architektur und System (`freedeck-rs_0.40.0_amd64_ubuntu-24.04.deb`; das passende System zuerst)."""
    versions = dict.fromkeys((version, version.replace("-", "~")))
    if pkg != RUST_PACKAGE:
        return [f"{pkg}_{v}_all.deb" for v in versions]
    arch = arch or _deb_arch()
    distro = _distro() if distro is None else distro
    out = []
    for v in versions:
        if distro:
            out.append(f"{pkg}_{v}_{arch}_{distro}.deb")
        out.append(f"{pkg}_{v}_{arch}.deb")
    return out


def release_files(release, packages):
    """Dateien der Pakete eines Releases ([{name, url, sha256, size}]); None, wenn eines fehlt
    oder eine Datei nicht sicher ist (fremde Adresse, keine SHA-256-Summe)."""
    version = (release.get("tag_name") or "").lstrip("v")
    assets = {a.get("name"): a for a in release.get("assets") or []}
    files = []
    for pkg in sorted({LEGACY.get(p, p) for p in packages}):
        # neuer Paketname; ältere Releases (vor der Umbenennung) haben nur den alten
        old = next((k for k, v in LEGACY.items() if v == pkg), None)
        names = asset_names(pkg, version) + (asset_names(old, version) if old else [])
        a = next((assets[n] for n in names if n in assets), None)
        if not a:
            log.info("Release %s has no package %s", version, pkg)
            return None
        url = a.get("browser_download_url") or ""
        digest = a.get("digest") or ""
        if not url.startswith(DOWNLOAD_PREFIXES) or not digest.startswith("sha256:"):
            return None
        files.append({"name": a["name"], "url": url, "sha256": digest.split(":", 1)[1].lower(),
                      "size": int(a.get("size") or 0)})
    return files


def make_update(release, packages):
    """Update für genau dieses Release (ohne Versionsvergleich) oder None."""
    if not release or release.get("draft") or not packages:
        return None
    files = release_files(release, packages)
    if files is None:
        return None
    version = (release.get("tag_name") or "").lstrip("v")
    return Update(version, english_notes(release.get("body")), release.get("html_url") or "", files,
                  date=(release.get("published_at") or release.get("created_at") or "")[:10],
                  prerelease=bool(release.get("prerelease")), release=release)


def packages_for(ed, installed=None):
    """Pakete, die das Update für die Ausgabe `ed` installiert: Python – die installierten Python-Pakete (sonst
    freestream); Rust – freedeck-rs. Die jeweils andere Ausgabe bleibt installiert."""
    installed = dpkg_packages() if installed is None else installed
    if ed == "rust":
        return [RUST_PACKAGE]
    py = [p for p in installed if p != RUST_PACKAGE]
    return py or ["freestream"]


def update_for_edition(update, ed):
    """Dasselbe Release mit den Paketen der Ausgabe `ed`; None, wenn das Release sie nicht enthält."""
    if update is None or not update.release:
        return None
    return make_update(update.release, packages_for(ed))


def release_update(release, packages, current=__version__, beta=False):
    """Update aus der API-Antwort, wenn sie neuer ist und alle Pakete enthält; sonst None."""
    if not release or release.get("draft") or (release.get("prerelease") and not beta):
        return None
    version = (release.get("tag_name") or "").lstrip("v")
    if not packages or not is_newer(version, current):
        return None
    return make_update(release, packages)


def published(releases, beta=True):
    """Veröffentlichte Releases (nie Entwürfe), neueste zuerst; Vorabversionen nur mit beta."""
    out = [r for r in releases or [] if isinstance(r, dict) and r.get("tag_name") and not r.get("draft")
           and (beta or not r.get("prerelease"))]
    return sorted(out, key=lambda r: version_key(r["tag_name"]), reverse=True)


def newest_update(releases, packages, current=__version__, beta=True):
    """Beta-Kanal: das neueste Release der Liste (auch eine Vorabversion), wenn es neuer ist."""
    rel = published(releases, beta)
    return release_update(rel[0], packages, current, beta) if rel else None


def earlier_versions(releases, packages, current=__version__, beta=False):
    """Ältere Versionen zum Zurückstufen, neueste zuerst (nur Releases mit allen Paketen)."""
    out = []
    for r in published(releases, beta):
        if is_older(r["tag_name"], current):
            up = make_update(r, packages)
            if up:
                out.append(up)
    return out


def fetch_releases(timeout=10):
    """Liste der Releases (GitHub-API). Wirft OSError/ValueError bei Netzfehlern."""
    data = _get_first(RELEASES_URLS, timeout)
    if not isinstance(data, list):
        raise ValueError("unexpected answer from GitHub")
    return data


def check(packages=None, timeout=10, beta=None):
    """Neuere Version für die installierten Pakete oder None. Wirft OSError/ValueError bei Netzfehlern."""
    packages = installed_packages() if packages is None else packages
    if not packages:
        return None
    # Python-Programme prüfen ihre eigenen Pakete (das Rust-Paket hat eigene Versionen und Dateien)
    packages = [p for p in packages if p != RUST_PACKAGE] or packages
    beta = channel() == "beta" if beta is None else beta
    if beta:
        return newest_update(fetch_releases(timeout), packages)
    return release_update(_get_first(API_URLS, timeout), packages)


def rollback_choices(timeout=10):
    """Ältere Versionen für die installierten Pakete (fragt GitHub)."""
    packages = installed_packages()
    if not packages:
        return []
    return earlier_versions(fetch_releases(timeout), packages, beta=channel() == "beta")


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
        if not f["url"].startswith(DOWNLOAD_PREFIXES) or "/" in f["name"]:
            raise UpdateError(f["name"])
        target = dest / f["name"]
        h = hashlib.sha256()
        req = urllib.request.Request(f["url"], headers={"User-Agent": f"FreeStream/{__version__}"})
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
    """Befehl, der die Pakete mit Administratorrechten installiert, oder None (kein pkexec).

    --allow-downgrades: dieselbe Zeile installiert auch eine ältere Version."""
    pkexec = shutil.which("pkexec")
    if not pkexec:
        return None
    names = [str(Path(f).resolve()) for f in files]
    apt = shutil.which("apt-get") or "/usr/bin/apt-get"
    # ohne Terminal: bei geänderten Konfigurationsdateien nie nachfragen, die eigene Fassung behalten
    return [pkexec, apt, "install", "-y", "--allow-downgrades", "-o", "Dpkg::Options::=--force-confdef",
            "-o", "Dpkg::Options::=--force-confold", *names]


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
    """Läuft FreeDeck und FreeWave gleichzeitig, fragt nur eines der beiden."""

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
