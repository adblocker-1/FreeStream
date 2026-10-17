"""Standard-Verzeichnisse nach XDG-Spezifikation."""
import os
from pathlib import Path


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or os.path.expanduser(default))


OLD_NAMES = ("streamlin", "lindeck")  # frühere Namen des Ordners (Streamlin bis 0.28, LinDeck bis 0.13)


def _dir(var: str, default: str) -> Path:
    """Ordner von FreeStream; ein Ordner unter einem früheren Namen wird einmalig umgezogen.

    Am alten Ort bleibt ein Verweis, damit gespeicherte Pfade (z. B. Sounds in
    Profilen) und noch laufende alte Programme weiter stimmen.
    """
    base = _xdg(var, default)
    new = base / "freestream"
    if new.exists():
        return new
    for name in OLD_NAMES:
        old = base / name
        if old.is_dir() and not old.is_symlink():
            try:
                old.rename(new)
                old.symlink_to(new.name)
            except OSError:
                return new if new.exists() else old
            break
    return new


CONFIG_DIR = _dir("XDG_CONFIG_HOME", "~/.config")
DATA_DIR = _dir("XDG_DATA_HOME", "~/.local/share")
STATE_DIR = _dir("XDG_STATE_HOME", "~/.local/state")

CONFIG_FILE = CONFIG_DIR / "config.json"
STATE_FILE = STATE_DIR / "state.json"
PROFILES_DIR = DATA_DIR / "profiles"
RESOURCES_DIR = DATA_DIR / "resources"
ASSETS_DIR = DATA_DIR / "elgato-assets"
PLUGINS_DIR = DATA_DIR / "plugins"
PLUGIN_DATA_DIR = DATA_DIR / "plugin-data"
LOG_FILE = STATE_DIR / "freestream.log"
PID_FILE = STATE_DIR / "freestream.pid"


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, DATA_DIR, STATE_DIR, PROFILES_DIR, RESOURCES_DIR):
        d.mkdir(parents=True, exist_ok=True)
