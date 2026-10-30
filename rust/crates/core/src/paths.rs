//! Standard-Verzeichnisse nach XDG-Spezifikation (wie `freestream/paths.py`).

use std::path::{Path, PathBuf};
use std::sync::OnceLock;

/// Frühere Namen des Ordners (Streamlin bis 0.28, LinDeck bis 0.13).
const OLD_NAMES: [&str; 2] = ["streamlin", "lindeck"];

fn xdg(var: &str, default: &str) -> PathBuf {
    match std::env::var_os(var) {
        Some(v) if !v.is_empty() => PathBuf::from(v),
        _ => home().join(default),
    }
}

/// Heimatordner des Benutzers.
pub fn home() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/tmp"))
}

/// Ordner von FreeStream unter `base`; ein Ordner unter einem früheren Namen wird einmalig umgezogen.
/// Am alten Ort bleibt ein Verweis, damit gespeicherte Pfade weiter stimmen.
fn app_dir(base: &Path) -> PathBuf {
    let new = base.join("freestream");
    if new.exists() {
        return new;
    }
    for name in OLD_NAMES {
        let old = base.join(name);
        let is_link = std::fs::symlink_metadata(&old).map(|m| m.file_type().is_symlink()).unwrap_or(true);
        if old.is_dir() && !is_link {
            if std::fs::rename(&old, &new).is_ok() {
                let _ = std::os::unix::fs::symlink("freestream", &old);
                return new;
            }
            return old;
        }
    }
    new
}

struct Dirs {
    config: PathBuf,
    data: PathBuf,
    state: PathBuf,
}

fn dirs() -> &'static Dirs {
    static DIRS: OnceLock<Dirs> = OnceLock::new();
    DIRS.get_or_init(|| Dirs {
        config: app_dir(&xdg("XDG_CONFIG_HOME", ".config")),
        data: app_dir(&xdg("XDG_DATA_HOME", ".local/share")),
        state: app_dir(&xdg("XDG_STATE_HOME", ".local/state")),
    })
}

pub fn config_dir() -> PathBuf {
    dirs().config.clone()
}
pub fn data_dir() -> PathBuf {
    dirs().data.clone()
}
pub fn state_dir() -> PathBuf {
    dirs().state.clone()
}
pub fn config_file() -> PathBuf {
    config_dir().join("config.json")
}
pub fn state_file() -> PathBuf {
    state_dir().join("state.json")
}
pub fn profiles_dir() -> PathBuf {
    data_dir().join("profiles")
}
pub fn resources_dir() -> PathBuf {
    data_dir().join("resources")
}
pub fn assets_dir() -> PathBuf {
    data_dir().join("elgato-assets")
}
pub fn plugins_dir() -> PathBuf {
    data_dir().join("plugins")
}
pub fn plugin_data_dir() -> PathBuf {
    data_dir().join("plugin-data")
}
pub fn log_file() -> PathBuf {
    state_dir().join("freestream.log")
}
pub fn pid_file() -> PathBuf {
    state_dir().join("freestream.pid")
}

/// Alle Ordner anlegen, die FreeStream immer braucht.
pub fn ensure_dirs() -> std::io::Result<()> {
    for d in [config_dir(), data_dir(), state_dir(), profiles_dir(), resources_dir()] {
        std::fs::create_dir_all(d)?;
    }
    Ok(())
}
