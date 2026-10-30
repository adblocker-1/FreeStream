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

// ------------------------------------------------------------------------------ Hilfsprogramme
/// Programm ausführen und warten; true bei Erfolg.
pub fn run_ok(args: &[&str]) -> bool {
    let Some((prog, rest)) = args.split_first() else { return false };
    Command::new(prog).args(rest).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null())
        .status().map(|s| s.success()).unwrap_or(false)
}

/// Ausgabe eines Programms (leer bei Fehlern).
pub fn run_out(args: &[&str]) -> String {
    let Some((prog, rest)) = args.split_first() else { return String::new() };
    Command::new(prog).args(rest).stdin(Stdio::null()).stderr(Stdio::null()).output()
        .map(|o| String::from_utf8_lossy(&o.stdout).into_owned()).unwrap_or_default()
}

/// Befehlszeile wie `shlex.split` zerlegen (einfache und doppelte Anführungszeichen, Backslash).
pub fn shell_split(s: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut has = false;
    let mut quote: Option<char> = None;
    let mut chars = s.chars();
    while let Some(c) = chars.next() {
        match (quote, c) {
            (Some(q), c) if c == q => quote = None,
            (Some('"'), '\\') => {
                if let Some(n) = chars.next() {
                    cur.push(n);
                }
            }
            (Some(_), c) => cur.push(c),
            (None, '\'' | '"') => {
                quote = Some(c);
                has = true;
            }
            (None, '\\') => {
                if let Some(n) = chars.next() {
                    cur.push(n);
                    has = true;
                }
            }
            (None, c) if c.is_whitespace() => {
                if has || !cur.is_empty() {
                    out.push(std::mem::take(&mut cur));
                }
                has = false;
            }
            (None, c) => cur.push(c),
        }
    }
    if has || !cur.is_empty() {
        out.push(cur);
    }
    out
}

// ---------------------------------------------------------------------------------- Prozesse
pub type Names = std::collections::BTreeSet<String>;

/// Prozessnamen eines Programms (Befehl und/oder Windows-Pfad aus dem Profil), wie `process_names_for`.
pub fn process_names_for(cmd: Option<&str>, exe_hint: &str) -> Names {
    let mut names = Names::new();
    if let Some(cmd) = cmd {
        let parts = shell_split(cmd);
        if parts.first().map(String::as_str) == Some("flatpak") {
            if let Some(last) = parts.last() {
                names.insert(last.rsplit('.').next().unwrap_or("").to_lowercase());
            }
        } else if let Some(first) = parts.first() {
            names.insert(first.rsplit('/').next().unwrap_or("").to_string());
        }
    }
    if !exe_hint.is_empty() {
        let base = exe_hint.replace('\\', "/").rsplit('/').next().unwrap_or("").to_string();
        let stem = match base.rfind('.') {
            Some(i) if i > 0 => &base[..i],
            _ => base.as_str(),
        };
        names.insert(stem.to_lowercase());
    }
    names.retain(|n| !n.is_empty());
    names
}

/// Sonderzeichen für einen regulären Ausdruck (pkill -f) schützen.
pub fn regex_escape(s: &str) -> String {
    let mut out = String::new();
    for c in s.chars() {
        if "\\.+*?()|[]{}^$".contains(c) {
            out.push('\\');
        }
        out.push(c);
    }
    out
}

/// Prozessname für `pkill -x` (der Kernel kürzt auf 15 Zeichen).
fn comm(n: &str) -> String {
    n.chars().take(15).collect()
}

/// Programme beenden (pkill nach Name, sonst nach Befehlszeile); true, wenn etwas beendet wurde.
pub fn kill_processes(names: &Names) -> bool {
    let mut ok = false;
    for n in names {
        if run_ok(&["pkill", "-i", "-x", &comm(n)])
            || run_ok(&["pkill", "-i", "-f", &format!("(^|/){}( |$)", regex_escape(n))])
        {
            ok = true;
        }
    }
    ok
}

pub fn is_running(names: &Names) -> bool {
    names.iter().any(|n| run_ok(&["pgrep", "-i", "-x", &comm(n)]))
}

/// Fenster nach vorne holen (X11 mit wmctrl, KDE mit kdotool).
pub fn focus_window(names: &Names) -> bool {
    for n in names {
        if have("wmctrl") && std::env::var_os("DISPLAY").is_some() && run_ok(&["wmctrl", "-x", "-a", n]) {
            return true;
        }
        if have("kdotool") {
            let out = run_out(&["kdotool", "search", "--class", n]);
            if let Some(wid) = out.split_whitespace().next() {
                run_ok(&["kdotool", "windowactivate", wid]);
                return true;
            }
        }
    }
    false
}

// ---------------------------------------------------------------------------- Zwischenablage
pub fn wayland() -> bool {
    std::env::var_os("WAYLAND_DISPLAY").map(|v| !v.is_empty()).unwrap_or(false)
}

/// Inhalt der Zwischenablage (None, wenn es kein Werkzeug dafür gibt).
pub fn clipboard_get() -> Option<String> {
    if wayland() && have("wl-paste") {
        return Some(run_out(&["wl-paste", "--no-newline"]));
    }
    if have("xclip") {
        return Some(run_out(&["xclip", "-selection", "clipboard", "-o"]));
    }
    if have("xsel") {
        return Some(run_out(&["xsel", "--clipboard", "--output"]));
    }
    None
}

/// Text in die Zwischenablage (wl-copy, xclip oder xsel bleiben im Hintergrund und halten ihn).
pub fn clipboard_set(text: &str) -> bool {
    use std::io::Write;
    let cmd: &[&str] = if wayland() && have("wl-copy") {
        &["wl-copy"]
    } else if have("xclip") {
        &["xclip", "-selection", "clipboard"]
    } else if have("xsel") {
        &["xsel", "--clipboard", "--input"]
    } else {
        log::warn!("{}", freestream_core::tr!("No clipboard available (install wl-clipboard or xclip)"));
        return false;
    };
    let child = Command::new(cmd[0]).args(&cmd[1..]).stdin(Stdio::piped()).stdout(Stdio::null()).stderr(Stdio::null()).spawn();
    match child {
        Ok(mut c) => {
            // stdin schließen (fallen lassen), damit das Programm den Text übernimmt
            let ok = c.stdin.take().map(|mut i| i.write_all(text.as_bytes()).is_ok()).unwrap_or(false);
            std::thread::spawn(move || {
                let _ = c.wait();
            });
            ok
        }
        Err(_) => false,
    }
}

// ------------------------------------------------------------------- Lautstärke und Medien
fn wpctl_target(source: bool) -> &'static str {
    if source { "@DEFAULT_AUDIO_SOURCE@" } else { "@DEFAULT_AUDIO_SINK@" }
}

/// Ausgabe von `wpctl get-volume` -> (Prozent, stumm).
pub fn parse_wpctl_volume(out: &str) -> Option<(u32, bool)> {
    let rest = out.split("Volume:").nth(1)?;
    let num: String = rest.trim_start().chars().take_while(|c| c.is_ascii_digit() || *c == '.').collect();
    let v: f64 = num.parse().ok()?;
    Some(((v * 100.0).round() as u32, out.contains("MUTED")))
}

/// Lautstärke der Standardausgabe (bzw. des Mikrofons) in Prozent und ob stumm.
pub fn get_volume(source: bool) -> (u32, bool) {
    if have("wpctl") {
        if let Some(v) = parse_wpctl_volume(&run_out(&["wpctl", "get-volume", wpctl_target(source)])) {
            return v;
        }
    }
    if have("pactl") {
        let what = if source { "source" } else { "sink" };
        let dev = format!("@DEFAULT_{}@", what.to_uppercase());
        let out = run_out(&["pactl", &format!("get-{what}-volume"), &dev]);
        let pct = out.split('%').next().and_then(|s| s.rsplit(|c: char| !c.is_ascii_digit()).next()).and_then(|s| s.parse().ok());
        let mute = run_out(&["pactl", &format!("get-{what}-mute"), &dev]);
        if let Some(p) = pct {
            return (p, mute.contains("yes"));
        }
    }
    (0, false)
}

pub fn change_volume(delta_pct: i32, source: bool) {
    if have("wpctl") {
        let sign = if delta_pct >= 0 { "+" } else { "-" };
        run_ok(&["wpctl", "set-volume", "-l", "1.0", wpctl_target(source), &format!("{}%{sign}", delta_pct.abs())]);
    } else if have("pactl") {
        let what = if source { "source" } else { "sink" };
        run_ok(&["pactl", &format!("set-{what}-volume"), &format!("@DEFAULT_{}@", what.to_uppercase()), &format!("{delta_pct:+}%")]);
    }
}

pub fn toggle_mute(source: bool) {
    if have("wpctl") {
        run_ok(&["wpctl", "set-mute", wpctl_target(source), "toggle"]);
    } else if have("pactl") {
        let what = if source { "source" } else { "sink" };
        run_ok(&["pactl", &format!("set-{what}-mute"), &format!("@DEFAULT_{}@", what.to_uppercase()), "toggle"]);
    }
}

/// playerctl-Steuerung (play-pause, next, previous, stop, position …); false ohne playerctl.
pub fn media(cmd: &str) -> bool {
    if !have("playerctl") {
        return false;
    }
    let mut args = vec!["playerctl"];
    args.extend(cmd.split_whitespace());
    run_ok(&args);
    true
}

/// Desktop-Benachrichtigung (notify-send), falls vorhanden.
pub fn notify(title: &str, body: &str, icon: &str) {
    if have("notify-send") {
        run_bg(&["notify-send", "-a", "FreeDeck", "-i", icon, title, body]);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splits_command_lines() {
        assert_eq!(shell_split(r#"flatpak run "com.spotify.Client" --x='a b'"#), vec!["flatpak", "run", "com.spotify.Client", "--x=a b"]);
        assert_eq!(shell_split("  a\\ b  c "), vec!["a b", "c"]);
    }

    #[test]
    fn process_names() {
        let n = process_names_for(Some("flatpak run com.spotify.Client"), r"C:\Program Files\Spotify\Spotify.exe");
        assert_eq!(n.into_iter().collect::<Vec<_>>(), vec!["client", "spotify"]);
        let n = process_names_for(Some("/usr/bin/obs --startreplaybuffer"), "");
        assert_eq!(n.into_iter().collect::<Vec<_>>(), vec!["obs"]);
        assert!(process_names_for(None, "").is_empty());
        assert_eq!(regex_escape("a.b+c"), "a\\.b\\+c");
    }

    #[test]
    fn parses_wpctl() {
        assert_eq!(parse_wpctl_volume("Volume: 0.45"), Some((45, false)));
        assert_eq!(parse_wpctl_volume("Volume: 1.00 [MUTED]\n"), Some((100, true)));
        assert_eq!(parse_wpctl_volume("nothing"), None);
    }
}
