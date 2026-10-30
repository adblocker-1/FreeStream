//! Programme starten, Dateien und Adressen öffnen (wie die Helfer in `freestream/system.py`).

use serde_json::{Map, Value};
use std::path::Path;
use std::process::{Command, Stdio};

/// Gibt es das Programm im PATH?
pub fn have(prog: &str) -> bool {
    if prog.contains('/') {
        return Path::new(prog).exists();
    }
    std::env::var_os("PATH")
        .map(|p| std::env::split_paths(&p).any(|d| d.join(prog).is_file()))
        .unwrap_or(false)
}

/// Programm im Hintergrund starten (ohne auf das Ende zu warten).
pub fn run_bg(args: &[&str]) -> bool {
    let Some((prog, rest)) = args.split_first() else { return false };
    let res = Command::new(prog).args(rest).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn();
    match res {
        Ok(mut child) => {
            // Ende im Hintergrund abholen, damit keine Zombies bleiben
            std::thread::spawn(move || {
                let _ = child.wait();
            });
            true
        }
        Err(e) => {
            log::warn!("{prog}: {e}");
            false
        }
    }
}

/// Befehlszeile über die Shell starten (Einstellungen enthalten oft Argumente).
pub fn run_shell(cmd: &str) -> bool {
    run_bg(&["sh", "-c", cmd])
}

pub fn is_windows_path(p: &str) -> bool {
    let b = p.as_bytes();
    (b.len() > 2 && b[0].is_ascii_alphabetic() && b[1] == b':' && (b[2] == b'\\' || b[2] == b'/')) || p.starts_with("\\\\")
}

/// Windows-Pfad nach `path_map` (Präfix-Ersetzung) auf einen Linux-Pfad abbilden.
pub fn map_path(path: &str, path_map: Option<&Map<String, Value>>) -> String {
    let p = path.replace('\\', "/");
    let mut pairs: Vec<(String, String)> = path_map
        .map(|m| m.iter().filter_map(|(k, v)| Some((k.replace('\\', "/"), v.as_str()?.to_string()))).collect())
        .unwrap_or_default();
    pairs.sort_by_key(|(k, _)| std::cmp::Reverse(k.len()));
    for (src, dst) in pairs {
        if p.to_lowercase().starts_with(&src.to_lowercase()) {
            return expand_user(&format!("{dst}{}", &p[src.len()..]));
        }
    }
    expand_user(&p)
}

pub fn expand_user(p: &str) -> String {
    match p.strip_prefix('~') {
        Some(rest) => format!("{}{rest}", freestream_core::paths::home().display()),
        None => p.to_string(),
    }
}

/// Datei, Ordner, Adresse oder Programm öffnen (com.elgato.streamdeck.system.open).
pub fn open_target(path: &str, cfg: &Map<String, Value>) -> bool {
    let path = path.trim().trim_matches('"');
    if path.is_empty() {
        return false;
    }
    let is_url = path.find("://").map(|i| path[..i].chars().all(|c| c.is_ascii_alphanumeric() || "+.-".contains(c))).unwrap_or(false);
    if is_url || path.starts_with("mailto:") {
        return run_bg(&["xdg-open", path]);
    }
    let mapped = map_path(path, cfg.get("path_map").and_then(Value::as_object));
    if is_windows_path(&mapped) {
        log::warn!("{}", freestream_core::tr!("Windows path without mapping: {path} (add it under Settings → Connections → Windows profiles)", path = path));
        return false;
    }
    let p = Path::new(&mapped);
    if mapped.ends_with(".desktop") {
        return if have("gio") { run_bg(&["gio", "launch", &mapped]) } else { run_bg(&["xdg-open", &mapped]) };
    }
    if p.is_file() && is_executable(p) && !mapped.ends_with(".sh") {
        return run_bg(&[&mapped]);
    }
    if p.exists() {
        return run_bg(&["xdg-open", &mapped]);
    }
    false
}

fn is_executable(p: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(p).map(|m| m.permissions().mode() & 0o111 != 0).unwrap_or(false)
}

/// Windows-Programmnamen auf Linux-Befehle (`app_map` in config.json, dann bekannte Namen).
pub fn resolve_program(target: &str, app_map: Option<&Map<String, Value>>, app_name: &str) -> Option<String> {
    let exe = target.replace('\\', "/").rsplit('/').next().unwrap_or("").to_lowercase();
    let stem = exe.trim_end_matches(".exe").to_string();
    if let Some(m) = app_map {
        for key in [exe.as_str(), stem.as_str(), app_name] {
            if key.is_empty() {
                continue;
            }
            if let Some(v) = m.iter().find(|(k, _)| k.eq_ignore_ascii_case(key)).and_then(|(_, v)| v.as_str()) {
                return Some(v.to_string());
            }
        }
    }
    const KNOWN: &[(&str, &str)] = &[
        ("obs64", "obs"), ("obs", "obs"), ("spotify", "spotify"), ("discord", "discord"), ("chrome", "google-chrome"),
        ("firefox", "firefox"), ("code", "code"), ("steam", "steam"), ("vlc", "vlc"), ("notepad", "gedit"),
        ("explorer", "xdg-open ~"), ("calc", "gnome-calculator"), ("telegram", "telegram-desktop"),
    ];
    for name in [stem.as_str(), &app_name.to_lowercase()] {
        if let Some((_, cmd)) = KNOWN.iter().find(|(k, _)| *k == name) {
            if have(cmd.split_whitespace().next().unwrap_or("")) {
                return Some((*cmd).to_string());
            }
        }
    }
    (!target.is_empty() && !is_windows_path(target) && have(target.split_whitespace().next().unwrap_or(""))).then(|| target.to_string())
}
