//! Dienst: Geräte finden (auch beim Ein- und Ausstecken), je Gerät eine Steuerung im eigenen Thread,
//! Profile neu lesen, wenn der Editor gespeichert hat.

use freestream_core::profiles::ProfileStore;
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use crate::device::{discover, Device, HidDeck};
use crate::engine::Controller;

/// Laufendes Gerät.
struct Running {
    stop: Arc<AtomicBool>,
    thread: JoinHandle<()>,
}

/// FreeDeck-Dienst.
pub struct Daemon {
    pub store: Arc<Mutex<ProfileStore>>,
    pub controllers: Arc<Mutex<HashMap<String, Arc<Mutex<Controller>>>>>,
    running: HashMap<String, Running>,
    pub stop: Arc<AtomicBool>,
}

impl Default for Daemon {
    fn default() -> Self {
        Self::new()
    }
}

impl Daemon {
    pub fn new() -> Self {
        let _ = freestream_core::paths::ensure_dirs();
        Self {
            store: Arc::new(Mutex::new(ProfileStore::new(None))),
            controllers: Default::default(),
            running: HashMap::new(),
            stop: Arc::new(AtomicBool::new(false)),
        }
    }

    /// Gerät betreiben (eigener Thread: Eingaben lesen, Ticks, Neuladen).
    pub fn attach(&mut self, dev: Arc<dyn Device>) {
        let serial = dev.serial();
        let ctrl = Arc::new(Mutex::new(Controller::new(dev.clone(), self.store.clone())));
        ctrl.lock().unwrap().start();
        self.controllers.lock().unwrap().insert(serial.clone(), ctrl.clone());
        let stop = Arc::new(AtomicBool::new(false));
        let (stop2, all_stop) = (stop.clone(), self.stop.clone());
        let ctrls = self.controllers.clone();
        let serial2 = serial.clone();
        let thread = std::thread::Builder::new()
            .name(format!("deck-{serial}"))
            .spawn(move || {
                let mut last_tick = Instant::now();
                while !stop2.load(Ordering::Relaxed) && !all_stop.load(Ordering::Relaxed) {
                    match dev.read(Duration::from_millis(100)) {
                        Ok(evs) => {
                            for ev in evs {
                                ctrl.lock().unwrap().handle(ev);
                            }
                        }
                        Err(e) => {
                            log::info!("{}: {e} – getrennt", serial2);
                            break;
                        }
                    }
                    if last_tick.elapsed() > Duration::from_millis(250) {
                        ctrl.lock().unwrap().tick();
                        last_tick = Instant::now();
                    }
                }
                ctrls.lock().unwrap().remove(&serial2);
            })
            .expect("Thread");
        self.running.insert(serial, Running { stop, thread });
    }

    /// Geräte suchen: neue anschließen, getrennte vergessen.
    pub fn scan(&mut self, hid: &mut hidapi::HidApi) {
        self.running.retain(|_, r| !r.thread.is_finished());
        for (kind, serial) in discover(hid) {
            if self.running.contains_key(&serial) {
                continue;
            }
            match HidDeck::open(hid, kind, &serial) {
                Ok(d) => {
                    log::info!("{}", freestream_core::tr!("{name} connected ({serial})",
                        name = freestream_core::models::model_name(&d.model()), serial = serial));
                    self.attach(Arc::new(d));
                }
                Err(e) => log::warn!("{serial}: {e} (udev-Regel installiert? Siehe README)"),
            }
        }
    }

    /// Profile neu lesen, wenn sie sich auf der Platte geändert haben; alle Geräte frisch zeichnen.
    pub fn reload_if_changed(&self) {
        let changed = self.store.lock().unwrap().changed_on_disk();
        if changed {
            self.store.lock().unwrap().reload();
            for c in self.controllers.lock().unwrap().values() {
                c.lock().unwrap().reload();
            }
        }
    }

    /// Hauptschleife: alle 2 s nach Geräten und geänderten Profilen sehen.
    pub fn run(&mut self) -> anyhow::Result<()> {
        let mut hid = elgato_streamdeck::new_hidapi()?;
        write_pid_file();
        while !self.stop.load(Ordering::Relaxed) {
            self.scan(&mut hid);
            self.reload_if_changed();
            for _ in 0..20 {
                if self.stop.load(Ordering::Relaxed) {
                    break;
                }
                std::thread::sleep(Duration::from_millis(100));
            }
        }
        for (_, r) in self.running.drain() {
            r.stop.store(true, Ordering::Relaxed);
            let _ = r.thread.join();
        }
        let _ = std::fs::remove_file(freestream_core::paths::pid_file());
        Ok(())
    }
}

/// Startzeit eines Prozesses (Takte seit Systemstart) – unterscheidet ihn von einem späteren mit derselben Pid.
pub fn start_time(pid: u32) -> Option<u64> {
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    stat.rsplit_once(')')?.1.split_whitespace().nth(19)?.parse().ok()
}

/// Pid-Datei „<pid> <startzeit>“ wie die Python-Version (für Signale aus FreeWave und dem Editor).
pub fn write_pid_file() {
    let pid = std::process::id();
    let text = format!("{pid} {}", start_time(pid).unwrap_or(0));
    let _ = freestream_core::config::write_atomic(&freestream_core::paths::pid_file(), &text, 0o644);
}

/// Läuft schon ein FreeDeck-Dienst? (Pid-Datei mit passender Startzeit)
pub fn already_running() -> Option<u32> {
    let text = std::fs::read_to_string(freestream_core::paths::pid_file()).ok()?;
    let mut it = text.split_whitespace();
    let pid: u32 = it.next()?.parse().ok()?;
    let start: u64 = it.next()?.parse().ok()?;
    (pid != std::process::id() && start_time(pid) == Some(start)).then_some(pid)
}
