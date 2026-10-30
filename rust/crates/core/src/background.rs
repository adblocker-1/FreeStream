//! Arbeit außerhalb des Fenster-Threads (Netz, Sichern, Wiederherstellen).
//!
//! Die Arbeit läuft in einem eigenen Thread; Fortschritt und Ergebnis kommen über einen
//! `std::sync::mpsc`-Kanal zurück. Die Oberfläche fragt den Kanal regelmäßig mit `try_recv` ab
//! (z. B. aus `glib::timeout_add_local`) und blockiert dabei nie.

use std::sync::mpsc::{channel, Receiver};

/// Nachricht aus einer Hintergrundarbeit.
#[derive(Debug)]
pub enum Event<T> {
    /// Fortschritt: (fertig, gesamt), z. B. Bytes.
    Progress(u64, u64),
    /// Ergebnis; danach kommt nichts mehr.
    Done(T),
}

/// Arbeit mit Fortschritt im Hintergrund starten. `work` bekommt eine Funktion für den Fortschritt.
pub fn spawn_with_progress<T, F>(work: F) -> Receiver<Event<T>>
where
    T: Send + 'static,
    F: FnOnce(&mut dyn FnMut(u64, u64)) -> T + Send + 'static,
{
    let (tx, rx) = channel();
    std::thread::spawn(move || {
        let progress_tx = tx.clone();
        let mut last = (u64::MAX, u64::MAX);
        let mut report = move |done: u64, total: u64| {
            // gleiche Werte nicht mehrfach schicken
            if (done, total) != last {
                last = (done, total);
                let _ = progress_tx.send(Event::Progress(done, total));
            }
        };
        let result = work(&mut report);
        // Hat die Oberfläche den Kanal schon verworfen (Fenster zu), geht das Ergebnis verloren.
        let _ = tx.send(Event::Done(result));
    });
    rx
}

/// Arbeit ohne Fortschritt im Hintergrund starten; das Ergebnis kommt genau einmal.
pub fn spawn<T, F>(work: F) -> Receiver<T>
where
    T: Send + 'static,
    F: FnOnce() -> T + Send + 'static,
{
    let (tx, rx) = channel();
    std::thread::spawn(move || {
        let _ = tx.send(work());
    });
    rx
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn delivers_progress_and_result() {
        let rx = spawn_with_progress(|p| {
            p(1, 2);
            p(1, 2);
            p(2, 2);
            "fertig"
        });
        let events: Vec<_> = rx.iter().collect();
        assert_eq!(events.len(), 3);
        assert!(matches!(events[0], Event::Progress(1, 2)));
        assert!(matches!(events[2], Event::Done("fertig")));
        assert_eq!(spawn(|| 7).recv().unwrap(), 7);
    }
}
