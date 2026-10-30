//! Eingabe-Aktionen über die virtuelle Tastatur/Maus (uinput): Hotkey, Hotkey-Umschalter, Text, Multimedia,
//! Mausrad – wie die gleichnamigen Klassen in `freestream/actions.py`.

use serde_json::{Map, Value};
use std::time::Duration;

use crate::actions::{ActionData, Behavior, Command, Env};
use crate::input::{self, Layout};
use crate::system;

/// Hotkey-Eintrag Nummer `i` aus Settings.Hotkeys (None, wenn leer).
fn hotkey(a: &ActionData, i: usize) -> Option<Map<String, Value>> {
    let hk = a.data.get("Settings")?.get("Hotkeys")?.as_array()?.get(i)?.as_object()?.clone();
    (!input::hotkey_empty(Some(&hk))).then_some(hk)
}

/// Hotkey senden: None = nicht belegt, Some(false) = Fehler (keine Taste, kein uinput).
fn send(a: &ActionData, env: &Env, i: usize) -> Option<bool> {
    let hk = hotkey(a, i)?;
    let codes = input::hotkey_codes(&hk, Layout::from_cfg(env.cfg), env.swap_cmd_ctrl);
    Some(!codes.is_empty() && input::tap(&codes))
}

fn result(ok: Option<bool>) -> Command {
    if ok == Some(true) { Command::None } else { Command::Alert }
}

/// com.elgato.streamdeck.system.hotkey – Hotkeys[0] = Taste/Linksdrehung, [1] = Rechtsdrehung,
/// [2] = Drücken, [3] = Tippen (Drehregler).
pub struct Hotkey;
impl Behavior for Hotkey {
    fn key_down(&mut self, a: &ActionData, env: &Env) -> Command {
        result(send(a, env, 0))
    }
    fn dial_turn(&mut self, a: &ActionData, env: &Env, ticks: i8) -> Command {
        let idx = if ticks > 0 { 1 } else { 0 };
        for _ in 0..ticks.unsigned_abs().min(20) {
            if send(a, env, idx) == Some(false) {
                return Command::Alert;
            }
        }
        Command::None
    }
    fn dial_down(&mut self, a: &ActionData, env: &Env) -> Command {
        match send(a, env, 2) {
            Some(false) => Command::Alert,
            _ => Command::None,
        }
    }
    fn dial_up(&mut self, _a: &ActionData, _env: &Env, _held_ms: u64) -> Command {
        Command::None
    }
    fn touch(&mut self, a: &ActionData, env: &Env, _long: bool) -> Command {
        match send(a, env, 3).or_else(|| send(a, env, 2)) {
            Some(false) => Command::Alert,
            _ => Command::None,
        }
    }
}

/// com.elgato.streamdeck.system.hotkeyswitch – Hotkey des aktuellen Zustands, dann Zustand wechseln.
pub struct HotkeySwitch;
impl Behavior for HotkeySwitch {
    fn key_down(&mut self, a: &ActionData, env: &Env) -> Command {
        let next = Command::SetState(a.state + 1);
        match send(a, env, a.state) {
            Some(false) => Command::Alert.then(next),
            _ => next,
        }
    }
}

/// com.elgato.streamdeck.system.text – Text einfügen (Zwischenablage + Strg+V) oder tippen.
pub struct Text;
impl Behavior for Text {
    fn key_down(&mut self, a: &ActionData, env: &Env) -> Command {
        let s = a.settings();
        let text = s.get("pastedText").and_then(Value::as_str).unwrap_or("").to_string();
        let enter = s.get("isSendingEnter").and_then(Value::as_bool).unwrap_or(false);
        let typing = s.get("isTypingMode").and_then(Value::as_bool).unwrap_or(false);
        let layout = Layout::from_cfg(env.cfg);
        let out = env.outbox.clone();
        // Einfügen wartet auf die Zwischenablage – nicht in der Steuerung warten
        std::thread::spawn(move || {
            if !paste(&text, enter, typing, layout) {
                out.send(Command::Alert);
            }
        });
        Command::None
    }
}

fn paste(text: &str, enter: bool, typing: bool, layout: Layout) -> bool {
    let key = |n: &str| input::key(n).unwrap_or(0);
    if !text.is_empty() {
        if typing && input::can_type(text, layout) {
            if !input::type_text(text, layout) {
                return false;
            }
        } else {
            let old = system::clipboard_get();
            if system::clipboard_set(text) {
                std::thread::sleep(Duration::from_millis(80));
                if !input::tap(&[key("KEY_LEFTCTRL"), key("KEY_V")]) {
                    return false;
                }
                // alte Zwischenablage zurück, wenn das Programm eingefügt hat
                if let Some(old) = old {
                    std::thread::spawn(move || {
                        std::thread::sleep(Duration::from_millis(600));
                        system::clipboard_set(&old);
                    });
                }
            } else if input::can_type(text, layout) {
                if !input::type_text(text, layout) {
                    return false;
                }
            } else {
                return false;
            }
        }
    }
    if enter {
        std::thread::sleep(Duration::from_millis(50));
        return input::tap(&[key("KEY_ENTER")]);
    }
    true
}

/// com.elgato.streamdeck.system.multimedia – Medientasten, auf dem Drehregler Lautstärke.
pub struct Multimedia;

/// actionIdx -> Medientaste
const MEDIA_KEYS: &[(i64, &str)] = &[
    (0, "KEY_PREVIOUSSONG"), (1, "KEY_PLAYPAUSE"), (2, "KEY_NEXTSONG"), (3, "KEY_STOPCD"), (4, "KEY_MUTE"),
    (5, "KEY_VOLUMEUP"), (6, "KEY_VOLUMEDOWN"), (7, "KEY_BRIGHTNESSDOWN"), (8, "KEY_BRIGHTNESSUP"),
    (9, "KEY_EJECTCD"), (10, "KEY_KBDILLUMDOWN"), (11, "KEY_KBDILLUMUP"), (12, "KEY_KBDILLUMTOGGLE"),
    (13, "KEY_FASTFORWARD"), (14, "KEY_REWIND"),
];
/// actionIdx -> playerctl-Befehl
const PLAYERCTL: &[(i64, &str)] = &[(0, "previous"), (1, "play-pause"), (2, "next"), (3, "stop"), (13, "position 10+"), (14, "position 10-")];

fn lookup(t: &[(i64, &'static str)], i: i64) -> Option<&'static str> {
    t.iter().find(|(k, _)| *k == i).map(|(_, v)| *v)
}

/// Gewählte Medienfunktion (Standard: Taste = Play/Pause, Drehregler = Lautstärke).
pub fn media_idx(a: &ActionData) -> i64 {
    let v = a.data.get("Settings").and_then(|s| s.get("actionIdx"));
    v.and_then(|v| v.as_i64().or_else(|| v.as_str()?.trim().parse().ok())).unwrap_or(if a.dial { 18 } else { 1 })
}

impl Behavior for Multimedia {
    fn key_down(&mut self, a: &ActionData, _env: &Env) -> Command {
        let i = media_idx(a);
        if i == 18 {
            system::toggle_mute(false);
            return Command::Redraw;
        }
        if (i == 13 || i == 14) && system::media(lookup(PLAYERCTL, i).unwrap_or("")) {
            return Command::None;
        }
        if let Some(name) = lookup(MEDIA_KEYS, i) {
            if input::available() && input::tap(&[input::key(name).unwrap_or(0)]) {
                return Command::None;
            }
        }
        if let Some(cmd) = lookup(PLAYERCTL, i) {
            if system::media(cmd) {
                return Command::None;
            }
        }
        match i {
            4 => system::toggle_mute(false),
            5 | 6 => system::change_volume(if i == 5 { 5 } else { -5 }, false),
            _ => return Command::Alert,
        }
        Command::None
    }
    fn dial_turn(&mut self, a: &ActionData, _env: &Env, ticks: i8) -> Command {
        let i = media_idx(a);
        if matches!(i, 18 | 4 | 5 | 6) {
            system::change_volume(2 * ticks as i32, false);
        } else if matches!(i, 0 | 1 | 2 | 13 | 14) {
            system::media(&format!("position {}{}", ticks.unsigned_abs() as u32 * 5, if ticks > 0 { "+" } else { "-" }));
        }
        Command::Redraw
    }
    fn dial_down(&mut self, a: &ActionData, env: &Env) -> Command {
        if matches!(media_idx(a), 18 | 4 | 5 | 6) {
            system::toggle_mute(false);
            Command::Redraw
        } else {
            self.key_down(a, env).then(Command::Redraw)
        }
    }
    fn dial_up(&mut self, _a: &ActionData, _env: &Env, _held_ms: u64) -> Command {
        Command::None
    }
}

/// com.elgato.streamdeck.system.mouse-wheel – Drehregler als Mausrad.
pub struct MouseWheel;
impl Behavior for MouseWheel {
    fn dial_turn(&mut self, a: &ActionData, _env: &Env, ticks: i8) -> Command {
        let s = a.settings();
        let truthy = |k: &str| s.get(k).map(|v| v.as_bool().unwrap_or_else(|| v.as_i64().unwrap_or(0) != 0)).unwrap_or(false);
        let horizontal = truthy("horizontal") || s.get("direction").and_then(Value::as_str) == Some("horizontal");
        let inv = if truthy("invert") { -1 } else { 1 };
        if input::scroll(-(ticks as i32) * inv, horizontal) { Command::None } else { Command::Alert }
    }
}

pub fn behavior_for(uuid: &str) -> Option<Box<dyn Behavior>> {
    Some(match uuid {
        "com.elgato.streamdeck.system.hotkey" => Box::new(Hotkey),
        "com.elgato.streamdeck.system.hotkeyswitch" => Box::new(HotkeySwitch),
        "com.elgato.streamdeck.system.text" => Box::new(Text),
        "com.elgato.streamdeck.system.multimedia" => Box::new(Multimedia),
        "com.elgato.streamdeck.system.mouse-wheel" => Box::new(MouseWheel),
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::path::PathBuf;

    fn data(v: Value) -> ActionData {
        ActionData::new(v.as_object().cloned().unwrap(), PathBuf::new())
    }

    #[test]
    fn media_defaults() {
        let mut a = data(json!({"UUID": "com.elgato.streamdeck.system.multimedia"}));
        assert_eq!(media_idx(&a), 1);
        a.dial = true;
        assert_eq!(media_idx(&a), 18);
        let a = data(json!({"UUID": "x", "Settings": {"actionIdx": "5"}}));
        assert_eq!(media_idx(&a), 5);
        assert_eq!(lookup(MEDIA_KEYS, 2), Some("KEY_NEXTSONG"));
        for (_, n) in MEDIA_KEYS {
            assert!(input::key(n).is_some(), "{n}");
        }
    }

    #[test]
    fn empty_hotkey_is_not_sent() {
        let a = data(json!({"UUID": "com.elgato.streamdeck.system.hotkey",
            "Settings": {"Hotkeys": [{"KeyModifiers": 0, "QTKeyCode": 33554431, "VKeyCode": -1}]}}));
        assert!(hotkey(&a, 0).is_none());
        assert!(hotkey(&a, 3).is_none());
        let cfg = Map::new();
        assert_eq!(Hotkey.key_down(&a, &Env::new(&cfg, 0, 1)), Command::Alert);
    }
}
