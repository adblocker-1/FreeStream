//! Anmeldung am Desktop: Autostart (wie `freestream/desktop.py`).

use std::path::PathBuf;

/// Befehl im Autostart je Programm.
fn autostart_exec(name: &str) -> &'static str {
    if name == "freewave" { "freewave --hidden" } else { "freedeck run" }
}

fn user_autostart_dir() -> PathBuf {
    std::env::var_os("XDG_CONFIG_HOME").map(PathBuf::from).unwrap_or_else(|| crate::paths::home().join(".config")).join("autostart")
}

const SYSTEM_AUTOSTART: &str = "/etc/xdg/autostart";

/// An/aus laut einer .desktop-Datei (None: gibt es nicht).
fn desktop_flag(path: &std::path::Path) -> Option<bool> {
    let text = std::fs::read_to_string(path).ok()?;
    let off = text.lines().any(|l| {
        let l = l.trim().to_lowercase();
        l == "hidden=true" || l == "x-gnome-autostart-enabled=false"
    });
    Some(!off)
}

/// Startet das Programm beim Anmelden? (Benutzerdatei vor der des Pakets)
pub fn autostart_enabled(name: &str) -> bool {
    desktop_flag(&user_autostart_dir().join(format!("{name}.desktop")))
        .or_else(|| desktop_flag(&PathBuf::from(SYSTEM_AUTOSTART).join(format!("{name}.desktop"))))
        .unwrap_or(false)
}

/// Autostart an/aus – nur im Benutzerordner, die Datei des Pakets bleibt unangetastet.
pub fn set_autostart(name: &str, on: bool) -> std::io::Result<()> {
    let dir = user_autostart_dir();
    let f = dir.join(format!("{name}.desktop"));
    let system_on = desktop_flag(&PathBuf::from(SYSTEM_AUTOSTART).join(format!("{name}.desktop"))).unwrap_or(false);
    if on && system_on {
        return match std::fs::remove_file(&f) {
            Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(e),
            _ => Ok(()),
        };
    }
    std::fs::create_dir_all(&dir)?;
    let title = if name == "freedeck" { "FreeDeck" } else { "FreeWave" };
    let mut lines = vec!["[Desktop Entry]".to_string(), "Type=Application".into(), format!("Name={title}"),
                         format!("Exec={}", autostart_exec(name)), format!("Icon={name}"), "NoDisplay=true".into()];
    if on {
        lines.extend(["X-GNOME-Autostart-enabled=true".into(), "X-KDE-autostart-phase=2".into()]);
    } else {
        lines.extend(["Hidden=true".into(), "X-GNOME-Autostart-enabled=false".into()]);
    }
    std::fs::write(f, lines.join("\n") + "\n")
}
