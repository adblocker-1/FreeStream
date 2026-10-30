//! Hilfen für Hintergrundprozesse (`pipewire -c`, jalv) und kleine Zustandsdateien.

use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Child, Command, Stdio};

use serde_json::{Map, Value};

/// Prozess losgelöst starten (eigene Sitzung wie `start_new_session=True`), ohne Ein-/Ausgabe.
pub fn spawn_detached(cmd: &mut Command, stdin_piped: bool) -> std::io::Result<Child> {
    cmd.stdin(if stdin_piped { Stdio::piped() } else { Stdio::null() })
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    // SAFETY: setsid ist zwischen fork und exec erlaubt (async-signal-safe)
    unsafe {
        cmd.pre_exec(|| {
            libc::setsid();
            Ok(())
        });
    }
    let child = cmd.spawn()?;
    reap_later(child.id());
    Ok(child)
}

/// Beendete Kindprozesse einsammeln, ohne auf sie zu warten (sonst blieben Zombies, deren
/// /proc/<pid>/cmdline leer ist – „läuft noch“ wäre dann falsch).
fn reap_later(pid: u32) {
    let _ = std::thread::Builder::new().name("freewave-reap".into()).spawn(move || unsafe {
        let mut status = 0;
        libc::waitpid(pid as libc::pid_t, &mut status, 0);
    });
}

/// Läuft `pid` noch mit `tag` in der Befehlszeile? (gegen fremde Prozesse mit derselben Pid)
pub fn ours(pid: i64, tag: &str) -> bool {
    if pid <= 0 {
        return false;
    }
    match std::fs::read(format!("/proc/{pid}/cmdline")) {
        Ok(raw) => raw.split(|b| *b == 0).any(|a| a == tag.as_bytes()),
        Err(_) => false,
    }
}

pub fn terminate(pid: i64) {
    if pid > 0 {
        unsafe {
            libc::kill(pid as libc::pid_t, libc::SIGTERM);
        }
    }
}

pub fn alive(pid: i64) -> bool {
    pid > 0 && unsafe { libc::kill(pid as libc::pid_t, 0) } == 0
}

/// JSON-Objekt aus einer Datei (leer, wenn es sie nicht gibt oder sie kaputt ist).
pub fn read_json(f: &Path) -> Map<String, Value> {
    std::fs::read_to_string(f)
        .ok()
        .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default()
}

/// JSON kompakt schreiben (wie `json.dumps`: „, “ und „: “ als Trenner).
pub fn write_json(f: &Path, v: &Value) -> std::io::Result<()> {
    if let Some(d) = f.parent() {
        std::fs::create_dir_all(d)?;
    }
    std::fs::write(f, py_dumps(v))
}

/// Wie Pythons `json.dumps` ohne indent (Trenner „, “ und „: “, Nicht-ASCII als \uXXXX).
pub fn py_dumps(v: &Value) -> String {
    let mut out = String::new();
    dump(v, &mut out, None, 0);
    out
}

/// Wie Pythons `json.dumps(v, indent=2)`.
pub fn py_dumps_indent(v: &Value) -> String {
    let mut out = String::new();
    dump(v, &mut out, Some(2), 0);
    out
}

fn dump_str(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 || (c as u32) > 0x7e => {
                let mut buf = [0u16; 2];
                for u in c.encode_utf16(&mut buf) {
                    out.push_str(&format!("\\u{u:04x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

fn dump(v: &Value, out: &mut String, indent: Option<usize>, level: usize) {
    let nl = |out: &mut String, lvl: usize| {
        if let Some(n) = indent {
            out.push('\n');
            out.push_str(&" ".repeat(n * lvl));
        }
    };
    let sep = if indent.is_some() { "," } else { ", " };
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => {
            if let Some(f) = n.as_f64().filter(|_| n.is_f64()) {
                // Python schreibt Kommazahlen immer mit Punkt (1.0 statt 1)
                if f.fract() == 0.0 && f.abs() < 1e16 {
                    out.push_str(&format!("{f:.1}"));
                } else {
                    out.push_str(&n.to_string());
                }
            } else {
                out.push_str(&n.to_string());
            }
        }
        Value::String(s) => dump_str(s, out),
        Value::Array(a) => {
            if a.is_empty() {
                out.push_str("[]");
                return;
            }
            out.push('[');
            for (i, x) in a.iter().enumerate() {
                if i > 0 {
                    out.push_str(sep);
                }
                nl(out, level + 1);
                dump(x, out, indent, level + 1);
            }
            nl(out, level);
            out.push(']');
        }
        Value::Object(m) => {
            if m.is_empty() {
                out.push_str("{}");
                return;
            }
            out.push('{');
            for (i, (k, x)) in m.iter().enumerate() {
                if i > 0 {
                    out.push_str(sep);
                }
                nl(out, level + 1);
                dump_str(k, out);
                out.push_str(": ");
                dump(x, out, indent, level + 1);
            }
            nl(out, level);
            out.push('}');
        }
    }
}

/// Unix-Zeit in Sekunden (wie `time.time()`).
pub fn now() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// Programm „im Hintergrund“ ohne Ausgabe ausführen, Zeitlimit wie in Python.
pub fn run_quiet(args: &[&str], timeout_s: f64) -> Option<crate::pactl::Output> {
    let (exe, rest) = args.split_first()?;
    let mut cmd = Command::new(exe);
    cmd.args(rest);
    crate::pactl::run_timeout(&mut cmd, std::time::Duration::from_secs_f64(timeout_s))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn dumps_like_python() {
        let v = json!({"a": [1, 2.5, true, null], "b": "Kopfhörer", "c": {}});
        assert_eq!(py_dumps(&v), "{\"a\": [1, 2.5, true, null], \"b\": \"Kopfh\\u00f6rer\", \"c\": {}}");
        assert_eq!(py_dumps_indent(&json!({"x": [1]})), "{\n  \"x\": [\n    1\n  ]\n}");
        assert_eq!(py_dumps(&json!(1.0)), "1.0");
    }
}
