"""Standard-Verzeichnisse nach XDG-Spezifikation."""
import os
from pathlib import Path


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or os.path.expanduser(default))


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", "~/.config") / "lindeck"
DATA_DIR = _xdg("XDG_DATA_HOME", "~/.local/share") / "lindeck"
STATE_DIR = _xdg("XDG_STATE_HOME", "~/.local/state") / "lindeck"

CONFIG_FILE = CONFIG_DIR / "config.json"
STATE_FILE = STATE_DIR / "state.json"
PROFILES_DIR = DATA_DIR / "profiles"
RESOURCES_DIR = DATA_DIR / "resources"
ASSETS_DIR = DATA_DIR / "elgato-assets"
PLUGINS_DIR = DATA_DIR / "plugins"
PLUGIN_DATA_DIR = DATA_DIR / "plugin-data"
LOG_FILE = STATE_DIR / "lindeck.log"
PID_FILE = STATE_DIR / "lindeck.pid"


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, DATA_DIR, STATE_DIR, PROFILES_DIR, RESOURCES_DIR):
        d.mkdir(parents=True, exist_ok=True)
