//! Installierte Programme aus den .desktop-Dateien (wie `desktop.app_entries`) und die Zuordnung von
//! Programmschlüsseln zu ihnen (wavedialogs._matches, _entry_for) – für den Reiter „Apps“ eines Kanals.

use std::path::{Path, PathBuf};

/// Programmdateien, die nichts über das Programm sagen (Starter, Interpreter).
const GENERIC_EXEC: [&str; 18] = ["env", "flatpak", "sh", "bash", "python", "python3", "java", "wine", "wine64", "gamemoderun",
    "steam", "snap", "gtk-launch", "xdg-open", "mono", "electron", "pkexec", "sudo"];

/// Ein installiertes Programm.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AppEntry {
    pub name: String,
    /// Fensterklasse (klein)
    pub cls: String,
    /// wie das Programm in PipeWire heißt – Programmdatei, sonst Fensterklasse (klein)
    pub key: String,
    /// Name aus Icon= (Themenname oder Pfad)
    pub icon: String,
}

/// Ordner mit .desktop-Dateien (XDG, Flatpak).
pub fn app_dirs() -> Vec<PathBuf> {
    let home = freestream_core::paths::home();
    let mut dirs: Vec<PathBuf> = std::env::var("XDG_DATA_DIRS").ok().filter(|s| !s.is_empty())
        .unwrap_or_else(|| "/usr/local/share:/usr/share".into()).split(':').map(|p| Path::new(p).join("applications")).collect();
    let data_home = std::env::var("XDG_DATA_HOME").ok().filter(|s| !s.is_empty()).map(PathBuf::from)
        .unwrap_or_else(|| home.join(".local/share"));
    dirs.push(data_home.join("applications"));
    dirs.push(PathBuf::from("/var/lib/flatpak/exports/share/applications"));
    dirs.push(home.join(".local/share/flatpak/exports/share/applications"));
    dirs
}

/// Eine .desktop-Datei auswerten (None: versteckt oder kein Programm).
pub fn parse_desktop(stem: &str, text: &str) -> Option<AppEntry> {
    let entry = text.split("[Desktop Action").next().unwrap_or("");
    let mut vals: Vec<(String, String)> = Vec::new();
    for line in entry.lines() {
        if line.starts_with('#') {
            continue;
        }
        if let Some((k, v)) = line.split_once('=') {
            let k = k.trim().to_string();
            if !vals.iter().any(|(x, _)| *x == k) {
                vals.push((k, v.trim().to_string()));
            }
        }
    }
    let get = |k: &str| vals.iter().find(|(x, _)| x == k).map(|(_, v)| v.as_str());
    if get("NoDisplay").is_some_and(|v| v.eq_ignore_ascii_case("true")) || get("Type").unwrap_or("Application") != "Application" {
        return None;
    }
    let name = get("Name").filter(|n| !n.is_empty())?.to_string();
    let exec_first = get("Exec").and_then(|e| e.split_whitespace().next()).unwrap_or(stem);
    let base = exec_first.rsplit('/').next().unwrap_or(exec_first);
    let exe = match base.rsplit_once('.') {
        Some((a, _)) if !a.is_empty() => a,
        _ => base,
    };
    let mut cls = get("StartupWMClass").filter(|c| !c.is_empty()).unwrap_or(exe).to_string();
    if cls == "env" || cls == "flatpak" {
        cls = if stem.matches('.').count() >= 2 { stem.rsplit('.').next().unwrap_or(stem).to_string() } else { stem.to_string() };
    }
    let key = if !exe.is_empty() && !GENERIC_EXEC.contains(&exe.to_lowercase().as_str()) {
        exe.to_string()
    } else {
        get("StartupWMClass").filter(|c| !c.is_empty()).unwrap_or(&name).to_string()
    };
    Some(AppEntry { name, cls: cls.to_lowercase(), key: key.to_lowercase(), icon: get("Icon").unwrap_or("").to_string() })
}

/// Programme aus den .desktop-Dateien, nach Namen sortiert (jeder Name einmal).
pub fn app_entries(dirs: &[PathBuf]) -> Vec<AppEntry> {
    let mut found: Vec<AppEntry> = Vec::new();
    for d in dirs {
        let Ok(rd) = std::fs::read_dir(d) else { continue };
        let mut files: Vec<PathBuf> = rd.flatten().map(|e| e.path()).filter(|p| p.extension().is_some_and(|e| e == "desktop")).collect();
        files.sort();
        for f in files {
            let Ok(raw) = std::fs::read(&f) else { continue };
            let stem = f.file_stem().and_then(|s| s.to_str()).unwrap_or("");
            if let Some(e) = parse_desktop(stem, &String::from_utf8_lossy(&raw)) {
                if !found.iter().any(|x| x.name == e.name) {
                    found.push(e);
                }
            }
        }
    }
    found.sort_by_key(|e| e.name.to_lowercase());
    found
}

/// Passt der Programmschlüssel zum Text? Wie beim Verteilen der Streams (Wortanfang).
pub fn matches(key: &str, text: &str) -> bool {
    if key.is_empty() {
        return false;
    }
    let (k, t) = (key.to_lowercase(), text.to_lowercase());
    let mut start = 0;
    while let Some(pos) = t[start..].find(&k) {
        let i = start + pos;
        let prev = t[..i].chars().next_back();
        let word_char = |c: char| c.is_alphanumeric() || c == '_';
        // \b vor dem Schlüssel: Wortgrenze, wenn sich Wort-/Nicht-Wort-Zeichen abwechseln
        let first_word = k.chars().next().is_some_and(word_char);
        if prev.map(word_char).unwrap_or(false) != first_word {
            return true;
        }
        start = i + t[i..].chars().next().map(char::len_utf8).unwrap_or(1);
    }
    false
}

/// Installiertes Programm zu einem Schlüssel (für Namen und Symbol).
pub fn entry_for<'a>(key: &str, installed: &'a [AppEntry]) -> Option<&'a AppEntry> {
    installed.iter().find(|e| e.key == key || e.cls == key)
        .or_else(|| installed.iter().find(|e| matches(key, &format!("{} {} {}", e.key, e.cls, e.name))))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_desktop_files() {
        let e = parse_desktop("firefox", "[Desktop Entry]\nName=Firefox\nExec=/usr/lib/firefox/firefox %u\nIcon=firefox\n[Desktop Action new]\nName=New").unwrap();
        assert_eq!(e, AppEntry { name: "Firefox".into(), cls: "firefox".into(), key: "firefox".into(), icon: "firefox".into() });
        let e = parse_desktop("com.spotify.Client", "[Desktop Entry]\nName=Spotify\nExec=flatpak run com.spotify.Client\nStartupWMClass=Spotify").unwrap();
        assert_eq!((e.key.as_str(), e.cls.as_str()), ("spotify", "spotify"));
        let e = parse_desktop("org.gnome.Game.app", "Name=Game\nExec=env FOO=1 game").unwrap();
        assert_eq!((e.key.as_str(), e.cls.as_str()), ("game", "app"));
        assert!(parse_desktop("x", "Name=Hidden\nNoDisplay=true").is_none());
        assert!(parse_desktop("x", "Name=Link\nType=Link").is_none());
        assert!(parse_desktop("x", "Exec=foo").is_none());
    }

    #[test]
    fn reads_folders_and_matches() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("b.desktop"), "Name=Beta\nExec=beta").unwrap();
        std::fs::write(dir.path().join("a.desktop"), "Name=alpha\nExec=alpha.sh").unwrap();
        std::fs::write(dir.path().join("dup.desktop"), "Name=Beta\nExec=other").unwrap();
        let list = app_entries(&[dir.path().to_path_buf()]);
        assert_eq!(list.iter().map(|e| e.name.as_str()).collect::<Vec<_>>(), ["alpha", "Beta"]);
        assert_eq!(list[0].key, "alpha");
        assert!(matches("spot", "Spotify music"));
        assert!(matches("music", "Spotify music"));
        assert!(!matches("usic", "Spotify music"));
        assert!(!matches("", "x"));
        assert_eq!(entry_for("beta", &list).map(|e| e.name.as_str()), Some("Beta"));
        assert_eq!(entry_for("alp", &list).map(|e| e.name.as_str()), Some("alpha"));
    }
}
