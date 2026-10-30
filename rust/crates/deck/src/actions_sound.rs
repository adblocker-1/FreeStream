//! Soundboard: com.elgato.streamdeck.soundboard.playaudio und .stopaudioplay (wie PlayAudio/StopAudio in
//! `freestream/actions.py`). Abgespielt wird über [`crate::sound`] auf dem Ausgang `soundboard_output`.

use serde_json::{Map, Value};
use std::path::PathBuf;

use crate::actions::{ActionData, Behavior, Command, Env};
use crate::{sound, system};

/// Audiodatei: importierte Ressource (Resources.file) oder Settings.path (Windows-Pfade über path_map).
fn audio_path(a: &ActionData, cfg: &Map<String, Value>) -> Option<PathBuf> {
    let res = a.data.get("Resources").and_then(|r| r.get("file")).and_then(Value::as_str).unwrap_or("");
    if !res.is_empty() {
        let dir = freestream_core::paths::resources_dir().join(res);
        if let Some(p) = std::fs::read_dir(dir).ok().and_then(|d| d.flatten().map(|e| e.path()).find(|p| p.is_file())) {
            return Some(p);
        }
    }
    let p = a.setting_str("path");
    if p.is_empty() {
        return None;
    }
    let mapped = PathBuf::from(system::map_path(&p, cfg.get("path_map").and_then(Value::as_object)));
    mapped.exists().then_some(mapped)
}

/// Ausgabegerät: outputType der Aktion (keine Windows-Geräte-IDs), sonst `soundboard_output`.
pub fn output_for(s: &Map<String, Value>, cfg: &Map<String, Value>) -> String {
    let fallback = cfg.get("soundboard_output").and_then(Value::as_str).unwrap_or("").to_string();
    let out = s.get("outputType").and_then(Value::as_str).unwrap_or("").to_string();
    if out.is_empty() || out.contains('{') || out.contains('\\') { fallback } else { out }
}

/// Wiedergabeart (actionType): 0 Abspielen/Überlappen, 1 Abspielen/Stopp, 2 Abspielen/Neustart, 3 Schleife/Stopp.
pub struct PlayAudio {
    key: String,
}

impl Default for PlayAudio {
    fn default() -> Self {
        Self { key: sound::new_key("sb") }
    }
}

impl Behavior for PlayAudio {
    fn key_down(&mut self, a: &ActionData, env: &Env) -> Command {
        let Some(path) = audio_path(a, env.cfg) else {
            log::warn!("{}", freestream_core::tr!("Audio file not found: {path}", path = a.setting_str("path")));
            return Command::Alert;
        };
        let s = a.settings();
        let mode = s.get("actionType").and_then(|v| v.as_i64().or_else(|| v.as_str()?.parse().ok())).unwrap_or(0);
        let playing = sound::is_playing(&self.key);
        if playing && (mode == 1 || mode == 3) {
            sound::stop(Some(&self.key));
            return Command::None;
        }
        if playing && mode == 2 {
            sound::stop(Some(&self.key));
        }
        let vol = s.get("volume").and_then(|v| v.as_f64().or_else(|| v.as_str()?.parse().ok())).unwrap_or(100.0).clamp(0.0, 100.0) as u32;
        let out = output_for(&s, env.cfg);
        if sound::play(&self.key, &path.to_string_lossy(), vol, &out, mode == 3) { Command::None } else { Command::Alert }
    }
}

/// Alle Wiedergaben beenden.
pub struct StopAudio;
impl Behavior for StopAudio {
    fn key_down(&mut self, _a: &ActionData, _env: &Env) -> Command {
        sound::stop(None);
        Command::None
    }
}

pub fn behavior_for(uuid: &str) -> Option<Box<dyn Behavior>> {
    Some(match uuid {
        "com.elgato.streamdeck.soundboard.playaudio" => Box::<PlayAudio>::default(),
        "com.elgato.streamdeck.soundboard.stopaudioplay" => Box::new(StopAudio),
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn output_device() {
        let cfg = json!({"soundboard_output": "alsa_output.usb"}).as_object().cloned().unwrap();
        let s = |v: Value| v.as_object().cloned().unwrap();
        assert_eq!(output_for(&s(json!({})), &cfg), "alsa_output.usb");
        assert_eq!(output_for(&s(json!({"outputType": "{0.0.0.00000000}.{abc}"})), &cfg), "alsa_output.usb");
        assert_eq!(output_for(&s(json!({"outputType": "headset"})), &cfg), "headset");
    }

    #[test]
    fn missing_file_alerts() {
        let a = ActionData::new(json!({"UUID": "com.elgato.streamdeck.soundboard.playaudio", "Settings": {"path": "/nonexistent/x.mp3"}})
            .as_object().cloned().unwrap(), PathBuf::new());
        let cfg = Map::new();
        assert_eq!(PlayAudio::default().key_down(&a, &Env::new(&cfg, 0, 1)), Command::Alert);
    }
}
