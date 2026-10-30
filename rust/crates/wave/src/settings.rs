//! Zugriff auf `config.json` für den Mixer.
//!
//! Der Mixer liest und schreibt dieselben Schlüssel wie die Python-Version (Abschnitte `"mixer"` und
//! `"wave_mic"`). Normalerweise ist das die [`Config`] aus dem Kern; Tests und Werkzeuge können eine
//! Datei an einem anderen Ort benutzen ([`FileSettings`]).

use std::path::PathBuf;

use freestream_core::config::{self, Config};
use serde_json::{Map, Value};

/// Einstellungen, die der Mixer liest und ändert.
pub trait Settings: Send {
    fn data(&self) -> &Map<String, Value>;
    fn data_mut(&mut self) -> &mut Map<String, Value>;
    fn save(&mut self) -> std::io::Result<()>;
    /// Neu laden, wenn ein anderes Programm die Datei geändert hat (true = neu geladen).
    fn reload_if_changed(&mut self) -> bool {
        false
    }
    /// Immer neu laden (SIGHUP an FreeWave).
    fn reload(&mut self) {}
}

impl Settings for Config {
    fn data(&self) -> &Map<String, Value> {
        &self.data
    }
    fn data_mut(&mut self) -> &mut Map<String, Value> {
        &mut self.data
    }
    fn save(&mut self) -> std::io::Result<()> {
        Config::save(self)
    }
    fn reload_if_changed(&mut self) -> bool {
        Config::reload_if_changed(self)
    }
    fn reload(&mut self) {
        *self = Config::load();
    }
}

/// config.json an einem frei gewählten Ort (gleiches Format wie [`Config`]).
#[derive(Debug, Clone)]
pub struct FileSettings {
    pub path: PathBuf,
    pub data: Map<String, Value>,
    mtime: Option<std::time::SystemTime>,
}

impl FileSettings {
    pub fn load(path: impl Into<PathBuf>) -> Self {
        let path = path.into();
        let data = std::fs::read_to_string(&path)
            .ok()
            .and_then(|t| serde_json::from_str::<Value>(&t).ok())
            .and_then(|v| v.as_object().cloned())
            .unwrap_or_default();
        let mtime = std::fs::metadata(&path).and_then(|m| m.modified()).ok();
        Self { path, data, mtime }
    }

    /// Nur im Speicher (save schreibt nichts).
    pub fn memory(data: Map<String, Value>) -> Self {
        Self { path: PathBuf::new(), data, mtime: None }
    }
}

impl Settings for FileSettings {
    fn data(&self) -> &Map<String, Value> {
        &self.data
    }
    fn data_mut(&mut self) -> &mut Map<String, Value> {
        &mut self.data
    }
    fn save(&mut self) -> std::io::Result<()> {
        if self.path.as_os_str().is_empty() {
            return Ok(());
        }
        let text = serde_json::to_string_pretty(&Value::Object(self.data.clone())).unwrap_or_default();
        config::write_atomic(&self.path, &text, 0o644)?;
        self.mtime = std::fs::metadata(&self.path).and_then(|m| m.modified()).ok();
        Ok(())
    }
    fn reload_if_changed(&mut self) -> bool {
        if self.path.as_os_str().is_empty() {
            return false;
        }
        let now = std::fs::metadata(&self.path).and_then(|m| m.modified()).ok();
        if now != self.mtime {
            self.reload();
            return true;
        }
        false
    }
    fn reload(&mut self) {
        if !self.path.as_os_str().is_empty() {
            *self = Self::load(self.path.clone());
        }
    }
}
