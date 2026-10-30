//! Virtuelles Deck: Tasten als Fenster auf dem Bildschirm – auch ohne angeschlossenes Gerät (wie
//! `freestream/virtualdeck.py`). Ein [`MemoryDeck`] mit der Seriennummer „VIRTUAL“ läuft im Dienst wie ein
//! echtes Gerät; das Fenster holt die Bilder ab und meldet Klicks als Tastendrücke.
//!
//! Einstellungen in config.json unter „virtual_deck“ (enabled, mode, model, topmost, edge, shortcut,
//! visible, pos) – dieselben Schlüssel wie in der Python-Version.

use freestream_core::config::Config;
use serde_json::{json, Map, Value};
use std::sync::Arc;

use crate::daemon::Daemon;
use crate::device::MemoryDeck;

pub const SERIAL: &str = "VIRTUAL";
pub const KEY_PX: u32 = 96;
/// Größen zur Wahl (quer) und dieselben hochkant (als Randleiste).
pub const SIZES: [&str; 4] = ["FDV3X2", "FDV4X2", "FDV5X3", "FDV8X4"];
pub const UPRIGHT: [(&str, &str); 4] = [("FDV3X2", "FDV2X3"), ("FDV4X2", "FDV2X4"), ("FDV5X3", "FDV3X5"), ("FDV8X4", "FDV4X8")];
pub const DEFAULT_MODEL: &str = "FDV4X2";

/// Einstellungen des virtuellen Decks mit Standardwerten (ungültige Werte ersetzt).
#[derive(Debug, Clone, PartialEq)]
pub struct Settings {
    pub enabled: bool,
    /// "window" (schwebendes Fenster) oder "edge" (Leiste am Bildschirmrand)
    pub mode: String,
    pub model: String,
    pub topmost: bool,
    pub edge: String,
    pub shortcut: String,
    pub visible: bool,
    pub pos: Option<(i32, i32)>,
}

pub fn settings(cfg: &Map<String, Value>) -> Settings {
    let raw = cfg.get("virtual_deck").and_then(Value::as_object).cloned().unwrap_or_default();
    let s = |k: &str| raw.get(k).and_then(Value::as_str).map(str::to_string);
    let b = |k: &str, d: bool| raw.get(k).and_then(Value::as_bool).unwrap_or(d);
    let model = s("model").filter(|m| SIZES.contains(&m.as_str())).unwrap_or_else(|| DEFAULT_MODEL.into());
    let mode = s("mode").filter(|m| m == "window" || m == "edge").unwrap_or_else(|| "window".into());
    let edge = s("edge").filter(|m| m == "right" || m == "left").unwrap_or_else(|| "right".into());
    let pos = raw.get("pos").and_then(Value::as_array).and_then(|a| Some((a.first()?.as_i64()? as i32, a.get(1)?.as_i64()? as i32)));
    Settings {
        enabled: b("enabled", false),
        mode,
        model,
        topmost: b("topmost", true),
        edge,
        shortcut: s("shortcut").unwrap_or_default(),
        visible: b("visible", true),
        pos,
    }
}

impl Settings {
    pub fn to_value(&self) -> Value {
        json!({"enabled": self.enabled, "mode": self.mode, "model": self.model, "topmost": self.topmost,
               "edge": self.edge, "shortcut": self.shortcut, "visible": self.visible,
               "pos": self.pos.map(|(x, y)| json!([x, y])).unwrap_or(Value::Null)})
    }

    /// Modellcode des laufenden Decks: als Randleiste hochkant (2 × 4 statt 4 × 2).
    pub fn device_model(&self) -> String {
        if self.mode == "edge" {
            UPRIGHT.iter().find(|(q, _)| *q == self.model).map(|(_, u)| u.to_string()).unwrap_or_else(|| self.model.clone())
        } else {
            self.model.clone()
        }
    }
}

/// Einstellungen ändern und speichern.
pub fn save(cfg: &mut Config, s: &Settings) {
    cfg.set("virtual_deck", s.to_value());
    let _ = cfg.save();
}

/// Virtuelles Deck im Dienst starten (läuft es schon mit derselben Größe, bleibt es).
pub fn start(daemon: &mut Daemon, s: &Settings) -> Arc<MemoryDeck> {
    let model = s.device_model();
    if let Some(dev) = daemon.virtual_device() {
        if dev.model_code == model {
            return dev;
        }
        daemon.detach(SERIAL);
    }
    let dev = Arc::new(MemoryDeck::new(&model, SERIAL, (KEY_PX, KEY_PX)));
    daemon.attach_virtual(dev.clone());
    dev
}

pub fn stop(daemon: &mut Daemon) {
    daemon.detach(SERIAL);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_and_upright() {
        let s = settings(&Map::new());
        assert!(!s.enabled);
        assert_eq!(s.model, DEFAULT_MODEL);
        let mut cfg = Map::new();
        cfg.insert("virtual_deck".into(), json!({"enabled": true, "mode": "edge", "model": "FDV5X3", "pos": [10, 20]}));
        let s = settings(&cfg);
        assert_eq!(s.device_model(), "FDV3X5");
        assert_eq!(s.pos, Some((10, 20)));
        cfg.insert("virtual_deck".into(), json!({"model": "XXX", "mode": "nonsense"}));
        let s = settings(&cfg);
        assert_eq!((s.model.as_str(), s.mode.as_str()), (DEFAULT_MODEL, "window"));
    }
}
