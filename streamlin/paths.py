"""Standard-Verzeichnisse nach XDG-Spezifikation."""
import os
from pathlib import Path


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or os.path.expanduser(default))


def _dir(var: str, default: str) -> Path:
    """Ordner von Streamlin; der alte Ordner „lindeck“ (bis 0.13) wird einmalig umgezogen.

    Am alten Ort bleibt ein Verweis, damit gespeicherte Pfade (z. B. Sounds in
    Profilen) weiter stimmen.
    """
    base = _xdg(var, default)
    new, old = base / "streamlin", base / "lindeck"
    try:
        if not new.exists() and old.is_dir() and not old.is_symlink():
            old.rename(new)
            old.symlink_to(new.name)
    except OSError:
        if not new.exists() and old.is_dir():
            return old
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
LOG_FILE = STATE_DIR / "streamlin.log"
PID_FILE = STATE_DIR / "streamlin.pid"


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, DATA_DIR, STATE_DIR, PROFILES_DIR, RESOURCES_DIR):
        d.mkdir(parents=True, exist_ok=True)
