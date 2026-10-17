"""Konfiguration (config.json) und Laufzeitzustand (state.json)."""
import json
import os
import threading
from copy import deepcopy

from . import paths, vault
from .i18n import _

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
            if paths.CONFIG_FILE.exists():
                try:
                    user = json.loads(paths.CONFIG_FILE.read_text(encoding="utf-8"))
                    data.update(user)
                    self.mtime = paths.CONFIG_FILE.stat().st_mtime
                except (OSError, ValueError) as e:
                    print("[lindeck] " + _("Invalid configuration, using defaults: {error}").format(error=e))
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
        data = self.data if data is None else data
        return any(isinstance(data.get(sec), dict) and data[sec].get(field) for sec, field in vault.FIELDS)

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
            tmp = f.with_name(f.name + ".tmp")
            # nur für den Benutzer lesbar, wenn noch Zugangsdaten in der Datei stehen
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if self.has_secret(out) else 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(out, indent=2, ensure_ascii=False))
            if self.has_secret(out):
                os.chmod(tmp, 0o600)
            os.replace(tmp, f)
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
