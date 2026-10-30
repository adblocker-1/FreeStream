//! Zugangsdaten im Schlüsselbund des Systems (Secret Service: GNOME-Schlüsselbund, KWallet) – wie
//! `freestream/vault.py` über das Programm `secret-tool` (Paket libsecret-tools).
//!
//! config.json vermerkt unter `"in_keyring"`, welche Felder im Schlüsselbund liegen (z. B.
//! `"obs.password"`); steht der Wert noch in der Datei, gilt der. `FREESTREAM_SECRETS=file` schaltet den
//! Schlüsselbund ab (Tests, Fehlersuche).
//!
//! Achtung: [`lookup`] und [`credential`] starten `secret-tool` und können Sekunden dauern (Entsperr-
//! Dialog) – nur aus Hintergrund-Threads aufrufen.

use serde_json::{Map, Value};
use std::io::{Read, Write};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

pub const SERVICE: &str = "freestream";
/// Name vor der Umbenennung (0.28): Einträge ziehen beim ersten Lesen um
pub const OLD_SERVICE: &str = "streamlin";
/// (Abschnitt in config.json, Feld) – diese Felder kommen in den Schlüsselbund
pub const FIELDS: [(&str, &str); 3] = [("obs", "password"), ("discord", "client_secret"), ("homeassistant", "token")];
const TIMEOUT: Duration = Duration::from_secs(5);

fn tool() -> Option<std::path::PathBuf> {
    if std::env::var("FREESTREAM_SECRETS").map(|v| v.eq_ignore_ascii_case("file")).unwrap_or(false) {
        return None;
    }
    let bus = std::env::var_os("DBUS_SESSION_BUS_ADDRESS").is_some()
        || std::path::Path::new(&format!("/run/user/{}/bus", own_uid())).exists();
    if !bus {
        return None;
    }
    let path = std::env::var_os("PATH")?;
    std::env::split_paths(&path).map(|d| d.join("secret-tool")).find(|p| p.is_file())
}

fn own_uid() -> u32 {
    // Besitzer des eigenen Prozess-Ordners = Benutzer-ID (ohne libc)
    use std::os::unix::fs::MetadataExt;
    std::fs::metadata("/proc/self").map(|m| m.uid()).unwrap_or(0)
}

/// Gibt es einen Schlüsselbund?
pub fn available() -> bool {
    tool().is_some()
}

/// `secret-tool` mit Eingabe ausführen: (Erfolg, Ausgabe); None ohne Schlüsselbund oder bei Zeitüberschreitung.
fn run(args: &[&str], input: Option<&str>) -> Option<(bool, String)> {
    let tool = tool()?;
    let mut child = Command::new(tool)
        .args(args)
        .stdin(if input.is_some() { Stdio::piped() } else { Stdio::null() })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| log::debug!("secret-tool: {e}"))
        .ok()?;
    if let (Some(data), Some(mut stdin)) = (input, child.stdin.take()) {
        let _ = stdin.write_all(data.as_bytes());
    }
    let start = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                let mut out = String::new();
                if let Some(mut so) = child.stdout.take() {
                    let _ = so.read_to_string(&mut out);
                }
                if !status.success() {
                    let mut err = String::new();
                    if let Some(mut se) = child.stderr.take() {
                        let _ = se.read_to_string(&mut err);
                    }
                    log::debug!("secret-tool {}: {}", args.first().unwrap_or(&""), err.trim());
                }
                return Some((status.success(), out));
            }
            Ok(None) if start.elapsed() < TIMEOUT => std::thread::sleep(Duration::from_millis(20)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                log::debug!("secret-tool: Zeitüberschreitung");
                return None;
            }
        }
    }
}

fn lookup_in(service: &str, key: &str) -> Option<String> {
    let (ok, out) = run(&["lookup", "service", service, "key", key], None)?;
    let v = out.trim_end_matches('\n').to_string();
    (ok && !v.is_empty()).then_some(v)
}

/// Gespeicherten Wert lesen; None, wenn es ihn nicht gibt oder der Schlüsselbund fehlt.
pub fn lookup(key: &str) -> Option<String> {
    if let Some(v) = lookup_in(SERVICE, key) {
        return Some(v);
    }
    let v = lookup_in(OLD_SERVICE, key)?;
    // unter dem neuen Namen ablegen, alten Eintrag entfernen
    if store(key, &v) {
        let _ = run(&["clear", "service", OLD_SERVICE, "key", key], None);
    }
    Some(v)
}

/// Wert im Schlüsselbund ablegen.
pub fn store(key: &str, value: &str) -> bool {
    let label = format!("FreeStream ({key})");
    matches!(run(&["store", "--label", &label, "service", SERVICE, "key", key], Some(value)), Some((true, _)))
}

/// Wert aus dem Schlüsselbund entfernen.
pub fn clear(key: &str) -> bool {
    let _ = run(&["clear", "service", OLD_SERVICE, "key", key], None);
    matches!(run(&["clear", "service", SERVICE, "key", key], None), Some((true, _)))
}

/// Wert direkt aus config.json (leer = nicht gesetzt).
pub fn in_file(cfg: &Map<String, Value>, section: &str, field: &str) -> Option<String> {
    match cfg.get(section)?.get(field)? {
        Value::String(s) if !s.is_empty() => Some(s.clone()),
        Value::Number(n) => Some(n.to_string()),
        _ => None,
    }
}

/// Vermerkt config.json, dass `section.field` im Schlüsselbund liegt?
pub fn listed(cfg: &Map<String, Value>, section: &str, field: &str) -> bool {
    let key = format!("{section}.{field}");
    cfg.get("in_keyring").and_then(Value::as_array).map(|a| a.iter().any(|v| v.as_str() == Some(key.as_str()))).unwrap_or(false)
}

/// Gibt es den Wert (in der Datei oder laut Vermerk im Schlüsselbund)? Startet kein `secret-tool`.
pub fn configured(cfg: &Map<String, Value>, section: &str, field: &str) -> bool {
    in_file(cfg, section, field).is_some() || listed(cfg, section, field)
}

/// Zugangsdaten wie `Config._load_secrets`: Wert aus config.json, sonst aus dem Schlüsselbund, wenn die
/// Datei ihn dort vermerkt. Blockiert (secret-tool) – nur in Hintergrund-Threads aufrufen.
pub fn credential(cfg: &Map<String, Value>, section: &str, field: &str) -> Option<String> {
    if let Some(v) = in_file(cfg, section, field) {
        return Some(v);
    }
    if listed(cfg, section, field) {
        return lookup(&format!("{section}.{field}"));
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn file_value_wins_and_keyring_marker_is_read() {
        let cfg = json!({"obs": {"password": "geheim"}, "in_keyring": ["homeassistant.token"],
                         "discord": {"client_secret": ""}});
        let cfg = cfg.as_object().unwrap();
        assert_eq!(in_file(cfg, "obs", "password").as_deref(), Some("geheim"));
        assert_eq!(credential(cfg, "obs", "password").as_deref(), Some("geheim"));
        assert!(listed(cfg, "homeassistant", "token"));
        assert!(!listed(cfg, "obs", "password"));
        assert!(configured(cfg, "homeassistant", "token"));
        assert!(!configured(cfg, "discord", "client_secret"));
        assert_eq!(credential(cfg, "discord", "client_secret"), None);
    }
}
