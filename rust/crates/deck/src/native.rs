//! Gemeinsames der Verbindungs-Aktionen (OBS, Discord, Home Assistant) wie `NativeAction` in
//! `freestream/native.py`: Felder aus den Einstellungen lesen, Taste mit Text und Status zeichnen und
//! Aufträge in einem Arbeits-Thread ausführen (die Steuerung blockiert nie).

use image::RgbaImage;
use serde_json::{Map, Value};
use std::sync::mpsc::{self, Sender};
use std::sync::Mutex;

use crate::actions::{ActionData, Command, Outbox};
use crate::render;

pub const ON_BG: [u8; 3] = [22, 120, 64];
pub const OFF_BG: [u8; 3] = [40, 40, 46];
pub const ERR_BG: [u8; 3] = [110, 30, 30];

/// Erster gesetzter Wert unter `keys` (Objekte: name/value/id) als Text – wie `_first` in Python.
pub fn first(settings: &Map<String, Value>, keys: &[&str]) -> Option<String> {
    keys.iter().find_map(|k| {
        let mut v = settings.get(*k)?;
        if let Value::Object(o) = v {
            v = ["name", "value", "id"].iter().find_map(|f| o.get(*f).filter(|x| !is_empty(x)))?;
        }
        match v {
            Value::String(s) if !s.is_empty() => Some(s.clone()),
            Value::Number(n) => Some(n.to_string()),
            Value::Bool(b) => Some(if *b { "True" } else { "False" }.to_string()),
            _ => None,
        }
    })
}

fn is_empty(v: &Value) -> bool {
    matches!(v, Value::Null) || v.as_str() == Some("")
}

/// Titel des ersten Zustands (Ersatz für fehlende Einstellungen, z. B. Szenenname aus Elgato-Profilen).
pub fn first_title(a: &ActionData) -> String {
    a.data.get("States").and_then(Value::as_array).and_then(|s| s.first())
        .and_then(|s| s.get("Title")).and_then(Value::as_str).unwrap_or("").to_string()
}

/// Hat irgendein Zustand ein eigenes Bild? Dann zeigt die Taste das Bild des Zustands.
pub fn has_image(a: &ActionData) -> bool {
    a.data.get("States").and_then(Value::as_array)
        .map(|s| s.iter().any(|st| st.get("Image").and_then(Value::as_str).map(|i| !i.is_empty()).unwrap_or(false)))
        .unwrap_or(false)
}

/// Zustand 0/1 aus „aktiv“ (nur bei Aktionen mit zwei Zuständen).
pub fn state_of(a: &ActionData, active: Option<bool>) -> Option<usize> {
    if a.state_count() > 1 { active.map(usize::from) } else { None }
}

/// Taste mit Text und Status (grün = aktiv); mit eigenem Titel ohne Status.
pub fn text_key(a: &ActionData, size: (u32, u32), main: &str, status: &str, active: Option<bool>) -> Option<RgbaImage> {
    if has_image(a) {
        return None;
    }
    let bg = if active == Some(true) { ON_BG } else { OFF_BG };
    let sub = if a.title().is_empty() { status } else { "" };
    Some(render::text_key(size, &crate::net::fit(main, 9), sub, bg, [255, 255, 255]))
}

type Job = Box<dyn FnOnce() + Send>;

/// Arbeits-Thread für Aufträge einer Verbindung (in der Reihenfolge der Tastendrücke).
pub struct Worker {
    name: &'static str,
    tx: Mutex<Option<Sender<Job>>>,
}

impl Worker {
    pub const fn new(name: &'static str) -> Self {
        Self { name, tx: Mutex::new(None) }
    }

    /// Auftrag einreihen (der Thread startet beim ersten Auftrag).
    pub fn submit(&self, job: impl FnOnce() + Send + 'static) {
        let mut tx = self.tx.lock().unwrap();
        let job: Job = Box::new(job);
        let job = match tx.as_ref() {
            Some(t) => match t.send(job) {
                Ok(()) => return,
                Err(mpsc::SendError(job)) => job,
            },
            None => job,
        };
        let (t, rx) = mpsc::channel::<Job>();
        let spawned = std::thread::Builder::new().name(self.name.into()).spawn(move || {
            for job in rx {
                job();
            }
        });
        if spawned.is_ok() {
            let _ = t.send(job);
            *tx = Some(t);
        }
    }

    /// Auftrag mit Ergebnis: Fehler ins Protokoll und als Warnsymbol auf die Taste, sonst `Ok`-Befehl.
    pub fn run(&self, outbox: &Outbox, what: String, job: impl FnOnce() -> Result<Command, String> + Send + 'static) {
        let outbox = outbox.clone();
        self.submit(move || match job() {
            Ok(cmd) => outbox.send(cmd),
            Err(e) => {
                log::warn!("{what}: {e}");
                outbox.send(Command::Alert);
            }
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn first_reads_plain_and_object_values() {
        let s = json!({"scene": "", "sceneName": {"name": "Kamera"}, "port": 4455});
        let s = s.as_object().unwrap();
        assert_eq!(first(s, &["scene", "sceneName"]).as_deref(), Some("Kamera"));
        assert_eq!(first(s, &["port"]).as_deref(), Some("4455"));
        assert_eq!(first(s, &["missing"]), None);
    }

    #[test]
    fn worker_keeps_order() {
        let w = Worker::new("test");
        let (tx, rx) = mpsc::channel();
        for i in 0..5 {
            let tx = tx.clone();
            w.submit(move || tx.send(i).unwrap());
        }
        let got: Vec<i32> = (0..5).map(|_| rx.recv().unwrap()).collect();
        assert_eq!(got, vec![0, 1, 2, 3, 4]);
    }
}
