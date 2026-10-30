//! `config.json` – dieselbe Datei wie in der Python-Version (FreeDeck und FreeWave teilen sie).
//!
//! Geschrieben wird immer über eine Zwischendatei (Leser sehen nie eine halbe Datei); die Rechte der
//! Datei bleiben erhalten. Eine unlesbare Datei wird einmal als `config.json.broken-<Zeit>` gesichert.

use serde_json::{json, Map, Value};
use std::io::Write;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::Path;

use crate::paths;

/// Standardwerte (wie `config.DEFAULTS`).
pub fn defaults() -> Map<String, Value> {
    let v = json!({
        "brightness": 70,
        "sleep_after_minutes": 0,
        "keyboard_layout": "auto",
        "devices": {},
        "path_map": {},
        "app_map": {},
        "smart_profiles": {},
        "soundboard_output": "",
        "long_press_ms": 500,
        "double_press_ms": 300,
    });
    v.as_object().cloned().unwrap_or_default()
}

/// config.json als Objekt (leer, wenn es sie nicht gibt); Fehler, wenn sie nicht lesbar ist.
pub fn read_file() -> anyhow::Result<Map<String, Value>> {
    read_path(&paths::config_file())
}

fn read_path(f: &Path) -> anyhow::Result<Map<String, Value>> {
    let text = match std::fs::read_to_string(f) {
        Ok(t) => t,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Map::new()),
        Err(e) => return Err(e.into()),
    };
    match serde_json::from_str::<Value>(&text)? {
        Value::Object(m) => Ok(m),
        other => anyhow::bail!("config.json: {} statt eines Objekts", type_name(&other)),
    }
}

fn type_name(v: &Value) -> &'static str {
    match v {
        Value::Null => "null",
        Value::Bool(_) => "bool",
        Value::Number(_) => "Zahl",
        Value::String(_) => "Text",
        Value::Array(_) => "Liste",
        Value::Object(_) => "Objekt",
    }
}

/// Datei über eine Zwischendatei ersetzen; `mode` = Rechte der neuen Datei.
pub fn write_atomic(f: &Path, text: &str, mode: u32) -> std::io::Result<()> {
    if let Some(dir) = f.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let name = f.file_name().and_then(|n| n.to_str()).unwrap_or("file");
    let tmp = f.with_file_name(format!(".{name}.{}.{:?}.tmp", std::process::id(), std::thread::current().id())
        .replace(['(', ')'], ""));
    let res = (|| {
        let mut fh = std::fs::OpenOptions::new().write(true).create(true).truncate(true).mode(mode).open(&tmp)?;
        fh.set_permissions(std::fs::Permissions::from_mode(mode))?;
        fh.write_all(text.as_bytes())?;
        fh.sync_all()?;
        std::fs::rename(&tmp, f)
    })();
    if res.is_err() {
        let _ = std::fs::remove_file(&tmp);
    }
    res
}

/// Unlesbare config.json einmal daneben sichern (gleiche Rechte), Pfad der Kopie.
fn keep_broken(f: &Path) -> Option<std::path::PathBuf> {
    let meta = std::fs::metadata(f).ok()?;
    let stamp = meta.modified().ok()?.duration_since(std::time::UNIX_EPOCH).ok()?.as_secs();
    let backup = f.with_file_name(format!("config.json.broken-{stamp}"));
    if backup.exists() {
        return Some(backup);
    }
    let raw = std::fs::read(f).ok()?;
    let mode = meta.permissions().mode() & 0o777;
    let mut fh = std::fs::OpenOptions::new().write(true).create_new(true).mode(mode).open(&backup).ok()?;
    fh.write_all(&raw).ok()?;
    Some(backup)
}

/// Stehen Zugangsdaten in den Einstellungen? Dann wird die Datei nur für den Benutzer lesbar gespeichert.
fn has_secret(data: &Map<String, Value>) -> bool {
    const FIELDS: [(&str, &str); 4] = [("obs", "password"), ("discord", "client_secret"),
                                       ("homeassistant", "token"), ("hass", "token")];
    FIELDS.iter().any(|(sec, field)| {
        data.get(*sec).and_then(|s| s.get(*field)).map(|v| !v.is_null() && v != "").unwrap_or(false)
    })
}

/// Einstellungen von FreeDeck/FreeWave.
#[derive(Debug, Clone)]
pub struct Config {
    pub data: Map<String, Value>,
    mtime: Option<std::time::SystemTime>,
}

impl Default for Config {
    fn default() -> Self {
        Self::load()
    }
}

impl Config {
    /// Laden; fehlende Werte kommen aus den Standardwerten.
    pub fn load() -> Self {
        let f = paths::config_file();
        let mut data = defaults();
        match read_path(&f) {
            Ok(m) => data.extend(m),
            Err(e) => {
                let kept = keep_broken(&f);
                log::warn!("config.json nicht lesbar ({e}); gesichert als {kept:?}");
            }
        }
        let mtime = std::fs::metadata(&f).and_then(|m| m.modified()).ok();
        Self { data, mtime }
    }

    /// Hat ein anderes Programm die Datei geändert? Dann neu laden (true).
    pub fn reload_if_changed(&mut self) -> bool {
        let now = std::fs::metadata(paths::config_file()).and_then(|m| m.modified()).ok();
        if now != self.mtime {
            *self = Self::load();
            return true;
        }
        false
    }

    pub fn get(&self, key: &str) -> Option<&Value> {
        self.data.get(key)
    }

    pub fn get_i64(&self, key: &str, default: i64) -> i64 {
        self.data.get(key).and_then(Value::as_i64).unwrap_or(default)
    }

    pub fn get_str(&self, key: &str) -> String {
        self.data.get(key).and_then(Value::as_str).unwrap_or("").to_string()
    }

    pub fn set(&mut self, key: &str, value: Value) {
        self.data.insert(key.to_string(), value);
    }

    /// Abschnitt (Objekt) holen und bei Bedarf anlegen.
    pub fn section(&mut self, key: &str) -> &mut Map<String, Value> {
        let v = self.data.entry(key.to_string()).or_insert_with(|| Value::Object(Map::new()));
        if !v.is_object() {
            *v = Value::Object(Map::new());
        }
        v.as_object_mut().unwrap()
    }

    /// Einstellungen eines Geräts (`devices.<Seriennummer>`).
    pub fn device(&self, serial: &str) -> Map<String, Value> {
        self.data.get("devices").and_then(|d| d.get(serial)).and_then(Value::as_object).cloned().unwrap_or_default()
    }

    pub fn set_device(&mut self, serial: &str, key: &str, value: Value) {
        let devs = self.section("devices");
        let d = devs.entry(serial.to_string()).or_insert_with(|| Value::Object(Map::new()));
        if let Some(o) = d.as_object_mut() {
            o.insert(key.to_string(), value);
        }
    }

    /// Speichern; Rechte bleiben, mit Zugangsdaten nur für den Benutzer (0600).
    pub fn save(&mut self) -> std::io::Result<()> {
        let f = paths::config_file();
        let mut mode = std::fs::metadata(&f).map(|m| m.permissions().mode() & 0o777).unwrap_or(0o644);
        if has_secret(&self.data) {
            mode = 0o600;
        }
        let text = serde_json::to_string_pretty(&Value::Object(self.data.clone())).unwrap_or_default();
        write_atomic(&f, &text, mode)?;
        self.mtime = std::fs::metadata(&f).and_then(|m| m.modified()).ok();
        Ok(())
    }
}

/// Einzelne Schlüssel direkt in config.json schreiben (Sprache, Aussehen, Updates). Eine unlesbare Datei
/// bleibt unverändert (false).
pub fn save_keys(changes: &[(&str, Value)]) -> bool {
    let f = paths::config_file();
    let Ok(mut data) = read_path(&f) else { return false };
    for (k, v) in changes {
        data.insert((*k).to_string(), v.clone());
    }
    let mut mode = std::fs::metadata(&f).map(|m| m.permissions().mode() & 0o777).unwrap_or(0o644);
    if has_secret(&data) {
        mode = 0o600;
    }
    let text = serde_json::to_string_pretty(&Value::Object(data)).unwrap_or_default();
    write_atomic(&f, &text, mode).is_ok()
}

/// Laufzeitzustand (`state.json`): je Gerät aktives Profil, Seite und Helligkeit.
#[derive(Debug, Clone, Default)]
pub struct State {
    pub data: Map<String, Value>,
}

impl State {
    pub fn load() -> Self {
        let data = std::fs::read_to_string(paths::state_file())
            .ok()
            .and_then(|t| serde_json::from_str::<Value>(&t).ok())
            .and_then(|v| v.as_object().cloned())
            .unwrap_or_default();
        Self { data }
    }

    /// Eintrag eines Geräts (wird angelegt).
    pub fn device(&mut self, serial: &str) -> &mut Map<String, Value> {
        let v = self.data.entry(serial.to_string()).or_insert_with(|| Value::Object(Map::new()));
        if !v.is_object() {
            *v = Value::Object(Map::new());
        }
        v.as_object_mut().unwrap()
    }

    pub fn save(&self) -> std::io::Result<()> {
        let text = serde_json::to_string_pretty(&Value::Object(self.data.clone())).unwrap_or_default();
        write_atomic(&paths::state_file(), &text, 0o644)
    }
}
