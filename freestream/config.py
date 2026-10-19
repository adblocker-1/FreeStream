"""Konfiguration (config.json) und Laufzeitzustand (state.json)."""
import json
import logging
import os
import stat
import threading
import time
from copy import deepcopy

from . import paths, vault
from .i18n import _

log = logging.getLogger("freestream.config")

DEFAULTS = {
    # Helligkeit in Prozent für neue Geräte
    "brightness": 70,
    # Nach X Minuten ohne Tastendruck schlafen (0 = nie), wie in der Elgato-App
    "sleep_after_minutes": 0,
    # Tastaturlayout, mit dem die Profile unter Windows erstellt wurden: "auto", "de", "us"
    "keyboard_layout": "auto",
    # Gerätespezifisch: {"<Seriennummer>": {"profile": "<Profil-ID>", "brightness": 70}}
    "devices": {},
    # Windows-Pfade auf Linux-Pfade/Befehle abbilden (Präfix-Ersetzung)
    # z.B. {"C:/Users/felix/Music": "/home/felix/Musik"}
    "path_map": {},
    # Programme (exe-Name oder App-Name) auf Linux-Befehle abbilden
    # z.B. {"obs64.exe": "obs", "Spotify": "flatpak run com.spotify.Client"}
    "app_map": {},
    # Smart Profiles: Fensterklasse -> Profil-ID (zusätzlich zur Automatik)
    "smart_profiles": {},
    # Audioausgabe für Soundboard (PipeWire Node-Name oder leer = Standard)
    "soundboard_output": "",
    # Lange-Drücken-Schwelle in ms (Key Logic, Open Application)
    "long_press_ms": 500,
    "double_press_ms": 300,
}


def _write_atomic(f, text, mode, exact):
    """Datei über eine Zwischendatei ersetzen (Leser sehen nie eine halbe Datei).

    Die Zwischendatei heißt je Prozess und Thread anders: FreeDeck und FreeWave speichern dieselbe
    config.json, mit einem gemeinsamen Namen könnten sich ihre Bytes mischen.
    exact: Rechte genau mode, sonst gilt zusätzlich die umask."""
    tmp = f.with_name(f".{f.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            if exact:
                os.fchmod(fh.fileno(), mode)
            fh.write(text)
        os.replace(tmp, f)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _keep_broken(f):
    """Unlesbare config.json einmal daneben sichern, bevor das nächste save() Standardwerte darüber
    schreibt (Tippfehler beim Bearbeiten von Hand). Gibt den Pfad der Kopie zurück oder None."""
    try:
        st = f.stat()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(st.st_mtime))
        backup = f.with_name(f"{f.name}.broken-{stamp}")
        raw, mode = f.read_bytes(), stat.S_IMODE(st.st_mode)
        try:
            # gleich mit den Rechten der Datei anlegen (Zugangsdaten), nicht erst nach dem Kopieren
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        except FileExistsError:
            return backup  # diese Fassung ist schon gesichert
        try:
            with os.fdopen(fd, "wb") as fh:
                os.fchmod(fh.fileno(), mode)
                fh.write(raw)
        except OSError:
            backup.unlink(missing_ok=True)  # keine halbe Kopie, die als gesichert gälte
            raise
        return backup
    except OSError:
        return None


def _has_secret(data) -> bool:
    return any(isinstance(data.get(sec), dict) and data[sec].get(field) for sec, field in vault.FIELDS)


def _read_file() -> dict:
    """config.json als dict ({} wenn es sie nicht gibt); OSError/ValueError, wenn sie nicht lesbar ist."""
    try:
        data = json.loads(paths.CONFIG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError(type(data).__name__)
    return data


def read_section(name) -> dict:
    """Einen Abschnitt aus config.json lesen, ohne Zugangsdaten aus dem Schlüsselbund zu holen –
    für Auswahllisten im Editor (Config() ruft dafür jedes Mal secret-tool auf)."""
    try:
        sec = _read_file().get(name)
    except (OSError, ValueError):
        return {}
    return sec if isinstance(sec, dict) else {}


def save_keys(**changes) -> bool:
    """Einzelne Einstellungen direkt in config.json schreiben (Updater, Sprache, Aussehen), ohne Config.

    Ist die Datei nicht lesbar, bleibt sie unverändert – sonst stünde danach nur noch dieser Schlüssel
    darin. Die Rechte der Datei bleiben erhalten (0600, solange Zugangsdaten darin stehen)."""
    f = paths.CONFIG_FILE
    try:
        data = _read_file()
    except (OSError, ValueError) as e:
        log.warning(_("Settings not saved: {error}").format(error=f"{f}: {e}"))
        return False
    data.update(changes)
    f.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode, exact = stat.S_IMODE(f.stat().st_mode), True
    except OSError:
        mode, exact = 0o644, False
    if _has_secret(data):
        mode, exact = 0o600, True  # wie Config.save
    _write_atomic(f, json.dumps(data, indent=2, ensure_ascii=False), mode, exact)
    return True


class Config:
    def __init__(self):
        self._lock = threading.RLock()
        self.data = deepcopy(DEFAULTS)
        self.mtime = 0.0
        self._stored = {}  # was zuletzt im Schlüsselbund stand: "obs.password" -> Wert
        self._listed = set()  # laut config.json im Schlüsselbund
        self.load()

    def load(self) -> None:
        with self._lock:
            data = deepcopy(DEFAULTS)
            f = paths.CONFIG_FILE
            if f.exists():
                try:
                    user = json.loads(f.read_text(encoding="utf-8"))
                    if not isinstance(user, dict):
                        raise ValueError(type(user).__name__)
                    data.update(user)
                    self.mtime = f.stat().st_mtime
                except (OSError, ValueError) as e:
                    # Kopie behalten: das nächste save() schreibt die Standardwerte über die Datei
                    backup = _keep_broken(f) if isinstance(e, ValueError) else None
                    print("[freedeck] " + _("Invalid configuration, using defaults: {error}").format(error=e)
                          + (f" → {backup}" if backup else ""))
                    try:
                        self.mtime = f.stat().st_mtime  # nicht alle 2 s erneut laden und melden
                    except OSError:
                        pass
            self._load_secrets(data)
            self.data = data

    def _load_secrets(self, data):
        """Zugangsdaten aus dem Schlüsselbund dazuholen (nur die, die config.json dort vermerkt)."""
        self._stored = {}
        listed = data.pop("in_keyring", None)
        self._listed = set(listed) if isinstance(listed, list) else set()
        if not self._listed:
            return
        for sec, field in vault.FIELDS:
            key = f"{sec}.{field}"
            if key not in listed:
                continue
            section = data.get(sec)
            if isinstance(section, dict) and section.get(field):
                continue  # steht (noch) in der Datei
            value = vault.lookup(key)
            if value:
                if not isinstance(section, dict):
                    section = data[sec] = {}
                section[field] = value
                self._stored[key] = value

    def changed_on_disk(self) -> bool:
        try:
            return paths.CONFIG_FILE.stat().st_mtime != self.mtime
        except OSError:
            return False

    def has_secret(self, data=None) -> bool:
        """True, wenn Zugangsdaten (Passwort, Token) in der Datei stehen."""
        return _has_secret(self.data if data is None else data)

    def _file_data(self):
        """Was in config.json geschrieben wird: Zugangsdaten möglichst im Schlüsselbund statt in der Datei."""
        out = deepcopy(self.data)
        listed = []
        use_vault = vault.available()
        for sec, field in vault.FIELDS:
            key = f"{sec}.{field}"
            section = out.get(sec)
            value = section.get(field) if isinstance(section, dict) else None
            if not value:
                if self._stored.pop(key, None) is not None:
                    vault.clear(key)  # vom Benutzer entfernt
                    self._listed.discard(key)
                elif key in self._listed:
                    listed.append(key)  # Schlüsselbund gerade nicht lesbar: Vermerk behalten
                continue
            if not use_vault:
                continue
            if self._stored.get(key) == value or vault.store(key, value):
                self._stored[key] = value
                del section[field]
                listed.append(key)
        if listed:
            out["in_keyring"] = listed
        return out

    def save(self) -> None:
        with self._lock:
            paths.ensure_dirs()
            f = paths.CONFIG_FILE
            out = self._file_data()
            # nur für den Benutzer lesbar, wenn noch Zugangsdaten in der Datei stehen
            secret = self.has_secret(out)
            _write_atomic(f, json.dumps(out, indent=2, ensure_ascii=False), 0o600 if secret else 0o644, secret)
            self.mtime = f.stat().st_mtime

    def get(self, key, default=None):
        return self.data.get(key, default)

    def device(self, serial: str) -> dict:
        return self.data.setdefault("devices", {}).setdefault(serial, {})


class State:
    """Merkt sich pro Gerät aktives Profil, Seite und Helligkeit."""

    def __init__(self):
        self._lock = threading.RLock()
        self.data = {}
        if paths.STATE_FILE.exists():
            try:
                self.data = json.loads(paths.STATE_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.data = {}

    def device(self, serial: str) -> dict:
        with self._lock:
            return self.data.setdefault(serial, {})

    def save(self) -> None:
        with self._lock:
            paths.ensure_dirs()
            tmp = paths.STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            tmp.replace(paths.STATE_FILE)
