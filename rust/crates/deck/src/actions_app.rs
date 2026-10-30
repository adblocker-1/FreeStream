//! com.elgato.streamdeck.system.close: Programm beenden (wie `CloseApp` in `freestream/actions.py`).
//! Der lange Druck mit „Beenden“ bei system.openapp steht bei `OpenApp` in [`crate::actions`].

use serde_json::Value;

use crate::actions::{ActionData, Behavior, Command, Env};
use crate::system;

/// Prozessnamen aus den Einstellungen (Befehl, Windows-Pfad, Programmname).
pub fn close_names(a: &ActionData, env: &Env) -> system::Names {
    let s = a.settings();
    let get = |k: &str| s.get(k).and_then(Value::as_str).unwrap_or("").to_string();
    let target = [get("exec"), get("bundle_path"), get("bundle_id")].into_iter().find(|t| !t.is_empty()).unwrap_or_default();
    let cmd = if !target.is_empty() && !system::is_windows_path(&target) && system::have(target.split_whitespace().next().unwrap_or("")) {
        Some(target.clone())
    } else {
        system::resolve_program(&target, env.cfg.get("app_map").and_then(Value::as_object), &get("app_name"))
    };
    let hint = [get("exec"), get("path"), get("bundle_path")].into_iter().find(|t| !t.is_empty()).unwrap_or_default();
    let mut names = system::process_names_for(cmd.as_deref(), &hint);
    let app = get("app_name").to_lowercase().replace(' ', "");
    if !app.is_empty() {
        names.insert(app);
    }
    names
}

pub struct CloseApp;
impl Behavior for CloseApp {
    fn key_up(&mut self, a: &ActionData, env: &Env, _held_ms: u64) -> Command {
        if system::kill_processes(&close_names(a, env)) { Command::None } else { Command::Alert }
    }
}

pub fn behavior_for(uuid: &str) -> Option<Box<dyn Behavior>> {
    (uuid == "com.elgato.streamdeck.system.close").then(|| Box::new(CloseApp) as Box<dyn Behavior>)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Map};
    use std::path::PathBuf;

    #[test]
    fn names_from_windows_profile() {
        let a = ActionData::new(json!({"UUID": "com.elgato.streamdeck.system.close",
            "Settings": {"path": "C:\\Program Files\\Mozilla Firefox\\firefox.exe", "app_name": "Mozilla Firefox"}})
            .as_object().cloned().unwrap(), PathBuf::new());
        let cfg = Map::new();
        let names = close_names(&a, &Env::new(&cfg, 0, 1));
        assert!(names.contains("firefox"));
        assert!(names.contains("mozillafirefox"));
    }
}
