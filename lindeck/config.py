"""Konfiguration (config.json) und Laufzeitzustand (state.json)."""
import json
import threading
from copy import deepcopy

from . import paths
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
            self.data = data

    def changed_on_disk(self) -> bool:
        try:
            return paths.CONFIG_FILE.stat().st_mtime != self.mtime
        except OSError:
            return False

    def save(self) -> None:
        with self._lock:
            paths.ensure_dirs()
            paths.CONFIG_FILE.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
            self.mtime = paths.CONFIG_FILE.stat().st_mtime

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
