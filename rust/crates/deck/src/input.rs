//! Virtuelle Tastatur und Maus über /dev/uinput (wie `freestream/keyboard.py`).
//!
//! Funktioniert unter X11 und Wayland, weil die Eingaben auf Kernel-Ebene entstehen – wie bei einer echten
//! Tastatur. Elgato speichert Hotkeys mit Windows-Virtual-Key-Codes (VKeyCode) und Qt-Keycodes (QTKeyCode).
//! VK-Codes hängen vom Tastaturlayout ab (auf deutschem Layout liegt VK_Z auf der physischen Y-Taste), daher
//! wird das Layout bei der Umrechnung berücksichtigt.

use evdev::uinput::VirtualDevice;
use evdev::{AttributeSet, EventType, InputEvent, KeyCode, RelativeAxisCode};
use serde_json::{Map, Value};
use std::str::FromStr;
use std::sync::{Mutex, OnceLock};
use std::time::Duration;

pub const MOD_SHIFT: u32 = 1;
pub const MOD_CTRL: u32 = 2;
pub const MOD_ALT: u32 = 4;
pub const MOD_META: u32 = 8;
/// QTKeyCode einer leeren Belegung
pub const EMPTY_QT: i64 = 33554431;

/// Tastaturlayout für die Umrechnung der VK-Codes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Layout {
    Us,
    De,
}

/// Keycode nach evdev-Name („KEY_A“).
pub fn key(name: &str) -> Option<u16> {
    KeyCode::from_str(name).ok().map(|k| k.code())
}

fn k(name: &str) -> u16 {
    // alle Namen in diesem Modul gibt es in evdev (geprüft im Test `all_names_exist`)
    key(name).unwrap_or(0)
}

// ------------------------------------------------------------------------------------- Tabellen
/// Windows-VK -> evdev (layoutunabhängige Tasten).
const VK_BASE: &[(i64, &str)] = &[
    (0x08, "KEY_BACKSPACE"), (0x09, "KEY_TAB"), (0x0C, "KEY_CLEAR"), (0x0D, "KEY_ENTER"),
    (0x10, "KEY_LEFTSHIFT"), (0x11, "KEY_LEFTCTRL"), (0x12, "KEY_LEFTALT"), (0x13, "KEY_PAUSE"),
    (0x14, "KEY_CAPSLOCK"), (0x1B, "KEY_ESC"), (0x20, "KEY_SPACE"), (0x21, "KEY_PAGEUP"),
    (0x22, "KEY_PAGEDOWN"), (0x23, "KEY_END"), (0x24, "KEY_HOME"), (0x25, "KEY_LEFT"),
    (0x26, "KEY_UP"), (0x27, "KEY_RIGHT"), (0x28, "KEY_DOWN"), (0x2C, "KEY_SYSRQ"),
    (0x2D, "KEY_INSERT"), (0x2E, "KEY_DELETE"), (0x2F, "KEY_HELP"),
    (0x5B, "KEY_LEFTMETA"), (0x5C, "KEY_RIGHTMETA"), (0x5D, "KEY_COMPOSE"), (0x5F, "KEY_SLEEP"),
    (0x6A, "KEY_KPASTERISK"), (0x6B, "KEY_KPPLUS"), (0x6C, "KEY_KPCOMMA"), (0x6D, "KEY_KPMINUS"),
    (0x6E, "KEY_KPDOT"), (0x6F, "KEY_KPSLASH"), (0x90, "KEY_NUMLOCK"), (0x91, "KEY_SCROLLLOCK"),
    (0xA0, "KEY_LEFTSHIFT"), (0xA1, "KEY_RIGHTSHIFT"), (0xA2, "KEY_LEFTCTRL"), (0xA3, "KEY_RIGHTCTRL"),
    (0xA4, "KEY_LEFTALT"), (0xA5, "KEY_RIGHTALT"),
    (0xA6, "KEY_BACK"), (0xA7, "KEY_FORWARD"), (0xA8, "KEY_REFRESH"), (0xA9, "KEY_STOP"),
    (0xAA, "KEY_SEARCH"), (0xAB, "KEY_BOOKMARKS"), (0xAC, "KEY_HOMEPAGE"),
    (0xAD, "KEY_MUTE"), (0xAE, "KEY_VOLUMEDOWN"), (0xAF, "KEY_VOLUMEUP"),
    (0xB0, "KEY_NEXTSONG"), (0xB1, "KEY_PREVIOUSSONG"), (0xB2, "KEY_STOPCD"), (0xB3, "KEY_PLAYPAUSE"),
    (0xB4, "KEY_MAIL"), (0xB5, "KEY_MEDIA"), (0xB6, "KEY_COMPUTER"), (0xB7, "KEY_CALC"),
    (0xE2, "KEY_102ND"),
];

/// Layoutabhängige Tasten: VK -> evdev-Name.
const VK_US: &[(i64, &str)] = &[
    (0xBA, "KEY_SEMICOLON"), (0xBB, "KEY_EQUAL"), (0xBC, "KEY_COMMA"), (0xBD, "KEY_MINUS"),
    (0xBE, "KEY_DOT"), (0xBF, "KEY_SLASH"), (0xC0, "KEY_GRAVE"), (0xDB, "KEY_LEFTBRACE"),
    (0xDC, "KEY_BACKSLASH"), (0xDD, "KEY_RIGHTBRACE"), (0xDE, "KEY_APOSTROPHE"),
];
const VK_DE: &[(i64, &str)] = &[
    (0xBA, "KEY_LEFTBRACE"),  // Ü
    (0xBB, "KEY_RIGHTBRACE"), // +
    (0xBC, "KEY_COMMA"), (0xBD, "KEY_SLASH"), // -
    (0xBE, "KEY_DOT"), (0xBF, "KEY_BACKSLASH"), // #
    (0xC0, "KEY_SEMICOLON"),  // Ö
    (0xDB, "KEY_MINUS"),      // ß
    (0xDC, "KEY_GRAVE"),      // ^
    (0xDD, "KEY_EQUAL"),      // ´
    (0xDE, "KEY_APOSTROPHE"), // Ä
];

/// Qt-Keycodes (z. B. für auf macOS erstellte Profile).
const QT: &[(i64, &str)] = &[
    (0x01000000, "KEY_ESC"), (0x01000001, "KEY_TAB"), (0x01000002, "KEY_TAB"), (0x01000003, "KEY_BACKSPACE"),
    (0x01000004, "KEY_ENTER"), (0x01000005, "KEY_KPENTER"), (0x01000006, "KEY_INSERT"), (0x01000007, "KEY_DELETE"),
    (0x01000008, "KEY_PAUSE"), (0x01000009, "KEY_SYSRQ"), (0x0100000A, "KEY_SYSRQ"), (0x0100000B, "KEY_CLEAR"),
    (0x01000010, "KEY_HOME"), (0x01000011, "KEY_END"), (0x01000012, "KEY_LEFT"), (0x01000013, "KEY_UP"),
    (0x01000014, "KEY_RIGHT"), (0x01000015, "KEY_DOWN"), (0x01000016, "KEY_PAGEUP"), (0x01000017, "KEY_PAGEDOWN"),
    (0x01000020, "KEY_LEFTSHIFT"), (0x01000021, "KEY_LEFTCTRL"), (0x01000022, "KEY_LEFTMETA"), (0x01000023, "KEY_LEFTALT"),
    (0x01000024, "KEY_CAPSLOCK"), (0x01000025, "KEY_NUMLOCK"), (0x01000026, "KEY_SCROLLLOCK"),
    (0x01000055, "KEY_COMPOSE"), (0x01000058, "KEY_HELP"),
    (0x01000070, "KEY_VOLUMEDOWN"), (0x01000071, "KEY_MUTE"), (0x01000072, "KEY_VOLUMEUP"),
    (0x01000080, "KEY_PLAYCD"), (0x01000081, "KEY_STOPCD"), (0x01000082, "KEY_PREVIOUSSONG"),
    (0x01000083, "KEY_NEXTSONG"), (0x01000085, "KEY_PAUSECD"), (0x01000086, "KEY_PLAYPAUSE"),
    (0x20, "KEY_SPACE"),
];

/// Zeichen -> (evdev-Name, Shift, AltGr) für den Tippmodus.
const US_CHARS: &[(char, &str, bool, bool)] = &[
    ('`', "KEY_GRAVE", false, false), ('~', "KEY_GRAVE", true, false), ('-', "KEY_MINUS", false, false),
    ('_', "KEY_MINUS", true, false), ('=', "KEY_EQUAL", false, false), ('+', "KEY_EQUAL", true, false),
    ('[', "KEY_LEFTBRACE", false, false), ('{', "KEY_LEFTBRACE", true, false), (']', "KEY_RIGHTBRACE", false, false),
    ('}', "KEY_RIGHTBRACE", true, false), ('\\', "KEY_BACKSLASH", false, false), ('|', "KEY_BACKSLASH", true, false),
    (';', "KEY_SEMICOLON", false, false), (':', "KEY_SEMICOLON", true, false), ('\'', "KEY_APOSTROPHE", false, false),
    ('"', "KEY_APOSTROPHE", true, false), (',', "KEY_COMMA", false, false), ('<', "KEY_COMMA", true, false),
    ('.', "KEY_DOT", false, false), ('>', "KEY_DOT", true, false), ('/', "KEY_SLASH", false, false),
    ('?', "KEY_SLASH", true, false), ('!', "KEY_1", true, false), ('@', "KEY_2", true, false), ('#', "KEY_3", true, false),
    ('$', "KEY_4", true, false), ('%', "KEY_5", true, false), ('^', "KEY_6", true, false), ('&', "KEY_7", true, false),
    ('*', "KEY_8", true, false), ('(', "KEY_9", true, false), (')', "KEY_0", true, false),
];
const DE_CHARS: &[(char, &str, bool, bool)] = &[
    ('^', "KEY_GRAVE", false, false), ('°', "KEY_GRAVE", true, false), ('!', "KEY_1", true, false), ('"', "KEY_2", true, false),
    ('²', "KEY_2", false, true), ('§', "KEY_3", true, false), ('³', "KEY_3", false, true), ('$', "KEY_4", true, false),
    ('%', "KEY_5", true, false), ('&', "KEY_6", true, false), ('/', "KEY_7", true, false), ('{', "KEY_7", false, true),
    ('(', "KEY_8", true, false), ('[', "KEY_8", false, true), (')', "KEY_9", true, false), (']', "KEY_9", false, true),
    ('=', "KEY_0", true, false), ('}', "KEY_0", false, true), ('ß', "KEY_MINUS", false, false), ('?', "KEY_MINUS", true, false),
    ('\\', "KEY_MINUS", false, true), ('´', "KEY_EQUAL", false, false), ('`', "KEY_EQUAL", true, false),
    ('@', "KEY_Q", false, true), ('€', "KEY_E", false, true), ('ü', "KEY_LEFTBRACE", false, false), ('Ü', "KEY_LEFTBRACE", true, false),
    ('+', "KEY_RIGHTBRACE", false, false), ('*', "KEY_RIGHTBRACE", true, false), ('~', "KEY_RIGHTBRACE", false, true),
    ('ö', "KEY_SEMICOLON", false, false), ('Ö', "KEY_SEMICOLON", true, false), ('ä', "KEY_APOSTROPHE", false, false),
    ('Ä', "KEY_APOSTROPHE", true, false), ('#', "KEY_BACKSLASH", false, false), ('\'', "KEY_BACKSLASH", true, false),
    ('<', "KEY_102ND", false, false), ('>', "KEY_102ND", true, false), ('|', "KEY_102ND", false, true),
    (',', "KEY_COMMA", false, false), (';', "KEY_COMMA", true, false), ('.', "KEY_DOT", false, false), (':', "KEY_DOT", true, false),
    ('-', "KEY_SLASH", false, false), ('_', "KEY_SLASH", true, false), ('µ', "KEY_M", false, true),
];

impl Layout {
    /// Einstellung `keyboard_layout` („auto“, „de“, „us“ …).
    pub fn from_setting(s: &str) -> Layout {
        match s.trim().to_lowercase().as_str() {
            "" | "auto" => detect_layout(),
            "de" => Layout::De,
            _ => Layout::Us,
        }
    }

    /// Layout aus der Konfiguration.
    pub fn from_cfg(cfg: &Map<String, Value>) -> Layout {
        Layout::from_setting(cfg.get("keyboard_layout").and_then(Value::as_str).unwrap_or("auto"))
    }

    /// Windows-VK -> Keycode.
    pub fn vk(self, vk: i64) -> Option<u16> {
        if (0x30..=0x39).contains(&vk) {
            return key(&format!("KEY_{}", vk - 0x30));
        }
        if (0x41..=0x5A).contains(&vk) {
            let mut ch = (vk as u8) as char;
            if self == Layout::De && (ch == 'Y' || ch == 'Z') {
                ch = if ch == 'Y' { 'Z' } else { 'Y' };
            }
            return key(&format!("KEY_{ch}"));
        }
        if (0x60..=0x69).contains(&vk) {
            return key(&format!("KEY_KP{}", vk - 0x60));
        }
        if (0x70..0x70 + 24).contains(&vk) {
            return key(&format!("KEY_F{}", vk - 0x70 + 1));
        }
        let layout = if self == Layout::De { VK_DE } else { VK_US };
        layout.iter().chain(VK_BASE).find(|(v, _)| *v == vk).map(|(_, n)| k(n))
    }

    /// Zeichen -> (Keycode, Shift, AltGr) für den Tippmodus (nur Sonderzeichen).
    pub fn char_key(self, c: char) -> Option<(u16, bool, bool)> {
        let table = if self == Layout::De { DE_CHARS } else { US_CHARS };
        table.iter().find(|(ch, ..)| *ch == c).map(|(_, n, s, a)| (k(n), *s, *a))
    }
}

fn qt_key(qt: i64) -> Option<u16> {
    if (0x01000030..0x01000030 + 24).contains(&qt) {
        return key(&format!("KEY_F{}", qt - 0x01000030 + 1));
    }
    QT.iter().find(|(q, _)| *q == qt).map(|(_, n)| k(n))
}

/// Aktives Tastaturlayout (de/us) – einmal ermittelt.
pub fn detect_layout() -> Layout {
    static L: OnceLock<Layout> = OnceLock::new();
    *L.get_or_init(|| {
        let mut cands: Vec<String> = Vec::new();
        if let Ok(v) = std::env::var("XKB_DEFAULT_LAYOUT") {
            cands.push(v);
        }
        for line in crate::system::run_out(&["localectl", "status"]).lines() {
            if line.contains("Layout") {
                if let Some((_, v)) = line.split_once(':') {
                    cands.push(v.trim().to_string());
                }
            }
        }
        if let Ok(txt) = std::fs::read_to_string("/etc/default/keyboard") {
            for line in txt.lines() {
                if let Some(v) = line.strip_prefix("XKBLAYOUT=") {
                    cands.push(v.trim_matches('"').to_string());
                }
            }
        }
        layout_from_candidates(&cands, &std::env::var("LANG").unwrap_or_default())
    })
}

/// Erster brauchbarer Layoutname entscheidet; sonst die Sprache.
pub fn layout_from_candidates(cands: &[String], lang: &str) -> Layout {
    for c in cands {
        let first = c.split(',').next().unwrap_or("").trim().to_lowercase();
        if first.starts_with("de") || first.starts_with("ch") || first.starts_with("at") {
            return Layout::De;
        }
        if !first.is_empty() {
            return Layout::Us;
        }
    }
    if lang.starts_with("de") { Layout::De } else { Layout::Us }
}

// ----------------------------------------------------------------------------------- Hotkeys
fn int(hk: &Map<String, Value>, key: &str) -> Option<i64> {
    hk.get(key).and_then(|v| v.as_i64().or_else(|| v.as_f64().map(|f| f as i64)))
}

fn flag(hk: &Map<String, Value>, key: &str) -> bool {
    hk.get(key).map(|v| v.as_bool().unwrap_or_else(|| v.as_i64().unwrap_or(0) != 0)).unwrap_or(false)
}

/// Modifier-Bits eines Hotkeys (KeyModifiers, sonst KeyShift/KeyCtrl/KeyOption/KeyCmd).
pub fn hotkey_mods(hk: &Map<String, Value>) -> u32 {
    match int(hk, "KeyModifiers") {
        Some(m) => m as u32,
        None => {
            (if flag(hk, "KeyShift") { MOD_SHIFT } else { 0 })
                | (if flag(hk, "KeyCtrl") { MOD_CTRL } else { 0 })
                | (if flag(hk, "KeyOption") { MOD_ALT } else { 0 })
                | (if flag(hk, "KeyCmd") { MOD_META } else { 0 })
        }
    }
}

/// Ist der Hotkey-Eintrag leer (keine Taste, keine Modifier)?
pub fn hotkey_empty(hk: Option<&Map<String, Value>>) -> bool {
    let Some(hk) = hk else { return true };
    if hk.is_empty() {
        return true;
    }
    let qt = int(hk, "QTKeyCode");
    let vk = int(hk, "VKeyCode");
    matches!(qt, None | Some(0) | Some(EMPTY_QT)) && matches!(vk, None | Some(-1)) && int(hk, "KeyModifiers").unwrap_or(0) == 0
}

/// Elgato-Hotkey-Eintrag -> Keycodes (Modifier zuerst). `swap_cmd_ctrl`: Profil vom Mac (Cmd = Strg).
pub fn hotkey_codes(hk: &Map<String, Value>, layout: Layout, swap_cmd_ctrl: bool) -> Vec<u16> {
    if hk.is_empty() {
        return Vec::new();
    }
    let qt = int(hk, "QTKeyCode").unwrap_or(0);
    let vk = int(hk, "VKeyCode").unwrap_or(-1);
    let native = int(hk, "NativeCode").unwrap_or(-1);
    let mods = hotkey_mods(hk);
    let mut key = None;
    if vk >= 0 {
        key = layout.vk(vk);
    }
    if key.is_none() {
        key = qt_key(qt);
    }
    if key.is_none() && 0x20 < qt && qt < 0x7F {
        let ch = qt as u8 as char;
        key = if ch.is_ascii_alphanumeric() { layout.vk(ch.to_ascii_uppercase() as i64) } else { layout.char_key(ch).map(|c| c.0) };
    }
    // Windows-Profile: NativeCode ist der VK-Code (auf dem Mac ein anderer Code – dort nicht verwenden)
    if key.is_none() && native >= 0 && !swap_cmd_ctrl {
        key = layout.vk(native);
    }
    let (mut ctrl, mut meta) = (mods & MOD_CTRL != 0, mods & MOD_META != 0);
    if swap_cmd_ctrl {
        std::mem::swap(&mut ctrl, &mut meta);
    }
    let mut out = Vec::new();
    if ctrl {
        out.push(k("KEY_LEFTCTRL"));
    }
    if mods & MOD_SHIFT != 0 {
        out.push(k("KEY_LEFTSHIFT"));
    }
    if mods & MOD_ALT != 0 {
        out.push(k("KEY_LEFTALT"));
    }
    if meta {
        out.push(k("KEY_LEFTMETA"));
    }
    if let Some(key) = key {
        if !out.contains(&key) {
            out.push(key);
        }
    }
    out
}

// ------------------------------------------------------------------------------------ Tippen
/// Kann der Text Zeichen für Zeichen getippt werden?
pub fn can_type(text: &str, layout: Layout) -> bool {
    text.chars().all(|c| c.is_ascii_alphanumeric() || " \n\t".contains(c) || layout.char_key(c).is_some())
}

/// Tastendrücke für einen Text (jede Liste ist eine Kombination).
pub fn text_combos(text: &str, layout: Layout) -> Vec<Vec<u16>> {
    let mut out = Vec::new();
    for c in text.chars() {
        let combo = match c {
            '\n' => vec![k("KEY_ENTER")],
            '\t' => vec![k("KEY_TAB")],
            ' ' => vec![k("KEY_SPACE")],
            c if c.is_ascii_alphabetic() => {
                let code = layout.vk(c.to_ascii_uppercase() as i64).unwrap_or(0);
                if c.is_ascii_uppercase() { vec![k("KEY_LEFTSHIFT"), code] } else { vec![code] }
            }
            c if c.is_ascii_digit() => vec![layout.vk(c as i64).unwrap_or(0)],
            c => match layout.char_key(c) {
                Some((code, shift, altgr)) => {
                    let mut v = Vec::new();
                    if shift {
                        v.push(k("KEY_LEFTSHIFT"));
                    }
                    if altgr {
                        v.push(k("KEY_RIGHTALT"));
                    }
                    v.push(code);
                    v
                }
                None => continue,
            },
        };
        out.push(combo);
    }
    out
}

// ------------------------------------------------------------------------------------ Gerät
/// Gemeinsames virtuelles Gerät (wird beim ersten Gebrauch angelegt).
fn device() -> &'static Mutex<Option<VirtualDevice>> {
    static DEV: OnceLock<Mutex<Option<VirtualDevice>>> = OnceLock::new();
    DEV.get_or_init(|| {
        let dev = open().map_err(|e| {
            log::warn!("{}", freestream_core::tr!(
                "uinput not available ({error}) – hotkeys disabled. Run the installer or add the user to the 'input' group.",
                error = e));
        }).ok();
        if dev.is_some() {
            std::thread::sleep(Duration::from_millis(200)); // Compositor muss das Gerät erst erkennen
        }
        Mutex::new(dev)
    })
}

fn open() -> std::io::Result<VirtualDevice> {
    let mut keys = AttributeSet::<KeyCode>::new();
    // alle Tasten, aber keine Joystick-/Touch-Knöpfe (sonst hält das System das Gerät für ein Tablet)
    for c in (1u16..0x100).chain(0x160..0x2FF) {
        keys.insert(KeyCode::new(c));
    }
    for b in [KeyCode::BTN_LEFT, KeyCode::BTN_RIGHT, KeyCode::BTN_MIDDLE] {
        keys.insert(b);
    }
    let mut rel = AttributeSet::<RelativeAxisCode>::new();
    for r in [RelativeAxisCode::REL_X, RelativeAxisCode::REL_Y, RelativeAxisCode::REL_WHEEL, RelativeAxisCode::REL_HWHEEL] {
        rel.insert(r);
    }
    VirtualDevice::builder()?.name("FreeDeck Virtual Input").with_keys(&keys)?.with_relative_axes(&rel)?.build()
}

/// Gibt es das virtuelle Gerät (oder lässt es sich anlegen)?
pub fn available() -> bool {
    device().lock().map(|d| d.is_some()).unwrap_or(false)
}

fn emit(dev: &mut VirtualDevice, code: u16, value: i32) {
    if let Err(e) = dev.emit(&[InputEvent::new(EventType::KEY.0, code, value)]) {
        log::debug!("uinput: {e}");
    }
}

/// Kombination drücken und wieder loslassen; false ohne uinput.
pub fn tap(codes: &[u16]) -> bool {
    tap_hold(codes, 15)
}

pub fn tap_hold(codes: &[u16], hold_ms: u64) -> bool {
    if codes.is_empty() {
        return false;
    }
    let Ok(mut guard) = device().lock() else { return false };
    let Some(dev) = guard.as_mut() else {
        log::info!("{}", freestream_core::tr!("tap {codes} (uinput not available)", codes = format!("{codes:?}")));
        return false;
    };
    for c in codes {
        emit(dev, *c, 1);
        std::thread::sleep(Duration::from_millis(4));
    }
    std::thread::sleep(Duration::from_millis(hold_ms));
    for c in codes.iter().rev() {
        emit(dev, *c, 0);
        std::thread::sleep(Duration::from_millis(4));
    }
    true
}

/// Text tippen (Zeichen für Zeichen).
pub fn type_text(text: &str, layout: Layout) -> bool {
    for combo in text_combos(text, layout) {
        if !tap_hold(&combo, 5) {
            return false;
        }
        std::thread::sleep(Duration::from_millis(6));
    }
    true
}

/// Mausrad drehen (positiv = nach oben bzw. rechts).
pub fn scroll(amount: i32, horizontal: bool) -> bool {
    let Ok(mut guard) = device().lock() else { return false };
    let Some(dev) = guard.as_mut() else { return false };
    let axis = if horizontal { RelativeAxisCode::REL_HWHEEL } else { RelativeAxisCode::REL_WHEEL };
    dev.emit(&[InputEvent::new(EventType::RELATIVE.0, axis.0, amount)]).is_ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn hk(v: Value) -> Map<String, Value> {
        v.as_object().cloned().unwrap()
    }

    #[test]
    fn all_names_exist() {
        for (_, n) in VK_BASE.iter().chain(VK_US).chain(VK_DE).chain(QT) {
            assert!(key(n).is_some(), "{n}");
        }
        for (_, n, ..) in US_CHARS.iter().chain(DE_CHARS) {
            assert!(key(n).is_some(), "{n}");
        }
        for n in ["KEY_LEFTCTRL", "KEY_LEFTSHIFT", "KEY_LEFTALT", "KEY_LEFTMETA", "KEY_RIGHTALT", "KEY_ENTER", "KEY_TAB", "KEY_SPACE"] {
            assert!(key(n).is_some(), "{n}");
        }
    }

    #[test]
    fn ctrl_shift_z_us_and_de() {
        // Strg+Umschalt+Z (Elgato unter Windows)
        let h = hk(json!({"KeyCmd": false, "KeyCtrl": true, "KeyModifiers": 3, "KeyOption": false, "KeyShift": true,
                          "NativeCode": 90, "QTKeyCode": 90, "VKeyCode": 90}));
        let ctrl = KeyCode::KEY_LEFTCTRL.code();
        let shift = KeyCode::KEY_LEFTSHIFT.code();
        assert_eq!(hotkey_codes(&h, Layout::Us, false), vec![ctrl, shift, KeyCode::KEY_Z.code()]);
        // deutsches Layout: VK_Z liegt auf der physischen Y-Taste
        assert_eq!(hotkey_codes(&h, Layout::De, false), vec![ctrl, shift, KeyCode::KEY_Y.code()]);
    }

    #[test]
    fn layout_dependent_keys() {
        let h = hk(json!({"KeyModifiers": 0, "QTKeyCode": EMPTY_QT, "VKeyCode": 0xBD}));
        assert_eq!(hotkey_codes(&h, Layout::Us, false), vec![KeyCode::KEY_MINUS.code()]);
        assert_eq!(hotkey_codes(&h, Layout::De, false), vec![KeyCode::KEY_SLASH.code()]);
        let f5 = hk(json!({"KeyModifiers": 4, "QTKeyCode": 0x01000034, "VKeyCode": 0x74}));
        assert_eq!(hotkey_codes(&f5, Layout::De, false), vec![KeyCode::KEY_LEFTALT.code(), KeyCode::KEY_F5.code()]);
    }

    #[test]
    fn mac_profiles_swap_cmd_and_ctrl() {
        // Cmd+C vom Mac: nur QTKeyCode und KeyCmd, kein KeyModifiers
        let h = hk(json!({"KeyCmd": true, "KeyCtrl": false, "KeyOption": false, "KeyShift": false,
                          "NativeCode": 8, "QTKeyCode": 67, "VKeyCode": -1}));
        assert_eq!(hotkey_mods(&h), MOD_META);
        assert_eq!(hotkey_codes(&h, Layout::Us, true), vec![KeyCode::KEY_LEFTCTRL.code(), KeyCode::KEY_C.code()]);
        assert_eq!(hotkey_codes(&h, Layout::Us, false), vec![KeyCode::KEY_LEFTMETA.code(), KeyCode::KEY_C.code()]);
        // KeyCtrl auf dem Mac wird zur Super-Taste
        let h = hk(json!({"KeyCtrl": true, "QTKeyCode": 0x01000012, "VKeyCode": -1}));
        assert_eq!(hotkey_codes(&h, Layout::De, true), vec![KeyCode::KEY_LEFTMETA.code(), KeyCode::KEY_LEFT.code()]);
    }

    #[test]
    fn qt_chars_and_native_code() {
        // Qt-Zeichencode „-“ ohne VK: Zeichentabelle des Layouts
        let h = hk(json!({"KeyModifiers": 2, "QTKeyCode": 45, "VKeyCode": -1}));
        assert_eq!(hotkey_codes(&h, Layout::De, false), vec![KeyCode::KEY_LEFTCTRL.code(), KeyCode::KEY_SLASH.code()]);
        assert_eq!(hotkey_codes(&h, Layout::Us, false), vec![KeyCode::KEY_LEFTCTRL.code(), KeyCode::KEY_MINUS.code()]);
        // nur NativeCode (Windows-VK) belegt
        let h = hk(json!({"KeyModifiers": 0, "NativeCode": 0x24, "QTKeyCode": EMPTY_QT, "VKeyCode": -1}));
        assert_eq!(hotkey_codes(&h, Layout::Us, false), vec![KeyCode::KEY_HOME.code()]);
    }

    #[test]
    fn empty_hotkeys() {
        assert!(hotkey_empty(None));
        assert!(hotkey_empty(Some(&hk(json!({"KeyModifiers": 0, "NativeCode": -1, "QTKeyCode": EMPTY_QT, "VKeyCode": -1})))));
        assert!(!hotkey_empty(Some(&hk(json!({"KeyModifiers": 2, "QTKeyCode": EMPTY_QT, "VKeyCode": -1})))));
        assert!(!hotkey_empty(Some(&hk(json!({"QTKeyCode": 65, "VKeyCode": 65})))));
    }

    #[test]
    fn typing_text() {
        assert!(can_type("Hallo Welt!", Layout::De));
        assert!(can_type("äöü@€", Layout::De));
        assert!(!can_type("äöü", Layout::Us));
        let combos = text_combos("Zy@", Layout::De);
        assert_eq!(combos[0], vec![KeyCode::KEY_LEFTSHIFT.code(), KeyCode::KEY_Y.code()]);
        assert_eq!(combos[1], vec![KeyCode::KEY_Z.code()]);
        assert_eq!(combos[2], vec![KeyCode::KEY_RIGHTALT.code(), KeyCode::KEY_Q.code()]);
        let combos = text_combos("@", Layout::Us);
        assert_eq!(combos[0], vec![KeyCode::KEY_LEFTSHIFT.code(), KeyCode::KEY_2.code()]);
    }

    #[test]
    fn layout_detection() {
        assert_eq!(layout_from_candidates(&["de,us".into()], "en_US"), Layout::De);
        assert_eq!(layout_from_candidates(&["us".into(), "de".into()], "de_DE"), Layout::Us);
        assert_eq!(layout_from_candidates(&[], "de_AT.UTF-8"), Layout::De);
        assert_eq!(Layout::from_setting("de"), Layout::De);
        assert_eq!(Layout::from_setting("us"), Layout::Us);
    }
}
