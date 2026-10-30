//! Hintergrundteil von FreeWave – wie `WaveHost` in `freestream/wavehost.py`: Mixer einrichten, neue
//! Streams auf die Kanäle verteilen, abgestürzte Effektketten neu starten, Software-Filterkette für
//! Wave-Mikrofone ohne Hardware-Filter und Auto-Ducking.
//!
//! `freewave --hidden` läuft mit [`run_service`]; das Fenster (später, GTK4) benutzt einen [`WaveHost`]
//! und lässt dessen [`WaveHost::run_loop`] in einem eigenen Thread laufen.

use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use freestream_core::{paths, tr};
use serde_json::{json, Map, Value};

use crate::ducking::Ducker;
use crate::mixer::Mixer;
use crate::pactl::{self, Pactl, PulseWatch, SystemPactl};
use crate::procs;
use crate::settings::Settings;
use crate::wavemic::{WaveMic, WaveMicService, WAVE_APP_FILE};

// ------------------------------------------------------------------ Statusdatei des Programms
/// Startzeit eines Prozesses (Takte seit dem Systemstart) – unterscheidet ihn von einem späteren
/// Prozess mit derselben Pid.
pub fn start_time(pid: i64) -> Option<u64> {
    let raw = std::fs::read(format!("/proc/{pid}/stat")).ok()?;
    let pos = raw.iter().rposition(|b| *b == b')')?;
    let rest = String::from_utf8_lossy(&raw[pos + 1..]).into_owned();
    rest.split_whitespace().nth(19)?.parse().ok()
}

/// `freewave.json` schreiben: {"pid", "gui", "start"}.
pub fn write_app_info(state_dir: &Path, gui: bool, name: &str) {
    let pid = i64::from(std::process::id());
    let info = json!({"pid": pid, "gui": gui, "start": start_time(pid)});
    let _ = procs::write_json(&state_dir.join(name), &info);
}

/// Statusdatei entfernen, wenn sie zu diesem Prozess gehört.
pub fn clear_app_info(state_dir: &Path, name: &str) {
    let f = state_dir.join(name);
    if procs::read_json(&f).get("pid").and_then(Value::as_i64) == Some(i64::from(std::process::id())) {
        let _ = std::fs::remove_file(f);
    }
}

/// Inhalt der Statusdatei (leer, wenn es sie nicht gibt).
pub fn app_info(state_dir: &Path, name: &str) -> Map<String, Value> {
    procs::read_json(&state_dir.join(name))
}

fn is_freewave(pid: i64) -> bool {
    let Ok(raw) = std::fs::read(format!("/proc/{pid}/cmdline")) else { return false };
    String::from_utf8_lossy(&raw).split('\0').any(|a| {
        let base = a.rsplit('/').next().unwrap_or(a);
        a.ends_with(".freewave") || a.ends_with(".linewave") || ["freewave", "wave-app", "linewave"].contains(&base)
    })
}

/// Pid einer laufenden Instanz laut Statusdatei oder None (nie dieser Prozess). Nach einem Absturz
/// kann die Pid inzwischen einem fremden Prozess gehören – die Startzeit verrät das.
pub fn running_instance(state_dir: &Path, name: &str) -> Option<i64> {
    let info = app_info(state_dir, name);
    let pid = info.get("pid").and_then(Value::as_i64)?;
    if pid == i64::from(std::process::id()) || !procs::alive(pid) {
        return None;
    }
    match info.get("start").and_then(Value::as_u64) {
        Some(start) => (start_time(pid) == Some(start)).then_some(pid),
        None if name == WAVE_APP_FILE => is_freewave(pid).then_some(pid),
        None => {
            let raw = std::fs::read(format!("/proc/{pid}/cmdline")).unwrap_or_default();
            let cmd = String::from_utf8_lossy(&raw);
            ["freestream", "freedeck", "freewave", "streamlin", "lindeck", "linewave"].iter().any(|n| cmd.contains(n))
                .then_some(pid)
        }
    }
}

// ------------------------------------------------------------------ Host
/// Hintergrundteil von FreeWave.
pub struct WaveHost {
    pub mixer: Arc<Mixer>,
    pub mic: Arc<Mutex<WaveMic>>,
    mic_service: Mutex<WaveMicService>,
    pub ducker: Arc<Ducker>,
    pulse: PulseWatch,
    error: Mutex<String>,
    reload: AtomicBool,
    running: AtomicBool,
    route: Mutex<(Option<u64>, Option<Instant>)>,
    fx_check: Mutex<Option<Instant>>,
}

/// Abschnitt "wave_mic" der Einstellungen.
fn wave_mic_conf(m: &Mixer) -> Option<Map<String, Value>> {
    m.with_settings(|s| s.data().get("wave_mic").and_then(Value::as_object).cloned())
}

impl WaveHost {
    /// Host mit config.json und echtem pactl.
    pub fn system() -> Self {
        Self::new(Box::new(freestream_core::config::Config::load()), Arc::new(SystemPactl), paths::state_dir())
    }

    pub fn new(cfg: Box<dyn Settings>, pactl: Arc<dyn Pactl>, state_dir: std::path::PathBuf) -> Self {
        Self::with_mixer(Arc::new(Mixer::new(cfg, pactl, state_dir)))
    }

    pub fn with_mixer(mixer: Arc<Mixer>) -> Self {
        let mut mic = WaveMic::new(wave_mic_conf(&mixer).as_ref(), Arc::new(PactlRef(mixer.clone())),
                                   mixer.state_dir().to_path_buf());
        mic.owner = true; // FreeWave besitzt die Filterkette
        let ducker = Ducker::new(mixer.clone());
        Self {
            mixer,
            mic: Arc::new(Mutex::new(mic)),
            mic_service: Mutex::new(WaveMicService::new()),
            ducker,
            pulse: PulseWatch::new(),
            error: Mutex::new(String::new()),
            reload: AtomicBool::new(false),
            running: AtomicBool::new(true),
            route: Mutex::new((None, None)),
            fx_check: Mutex::new(None),
        }
    }

    /// Fehlermeldung beim Einrichten (für die Anzeige), sonst "".
    pub fn error(&self) -> String {
        self.error.lock().unwrap().clone()
    }

    pub fn mixer_enabled(&self) -> bool {
        self.mixer.enabled()
    }

    /// PipeWire-Ereignisse (für eigene Beobachter, z. B. das Fenster).
    pub fn pulse(&self) -> &PulseWatch {
        &self.pulse
    }

    pub fn start(&self) {
        self.pulse.start();
        if self.mixer_enabled() {
            let r = if !self.mixer.is_active() {
                self.mixer.setup().map(|_| ())
            } else {
                // Mixer lief schon: beendete Effekte/Rauschunterdrückung wieder anwerfen
                self.watch_effects(true);
                Ok(())
            };
            if let Err(e) = r {
                *self.error.lock().unwrap() = tr!("Mixer: {error}", error = e);
                log::warn!("{}", tr!("Mixer not set up: {error}", error = e));
            }
        }
        let mut mic = self.mic.lock().unwrap();
        if let Err(e) = self.mic_service.lock().unwrap().start(&mut mic) {
            log::warn!("{}", tr!("Wave microphone: {error}", error = e));
        }
        drop(mic);
        self.ducker.start();
    }

    /// Einstellungen neu laden lassen (SIGHUP von FreeDeck).
    pub fn request_reload(&self) {
        self.reload.store(true, Ordering::SeqCst);
    }

    pub fn tick(&self) {
        pactl::snapshot(|| self.tick_inner());
    }

    fn tick_inner(&self) {
        if self.reload.swap(false, Ordering::SeqCst) {
            self.mixer.with_settings_mut(|s| s.reload());
            let conf = wave_mic_conf(&self.mixer);
            if let Err(e) = self.mic.lock().unwrap().reload(conf.as_ref()) {
                log::warn!("{}", tr!("Wave microphone: {error}", error = e));
            }
        } else {
            self.mixer.with_settings_mut(|s| s.reload_if_changed());
        }
        if self.mixer_enabled()
            && self.mixer.conf().get("auto_route").map(crate::mixer::truthy).unwrap_or(true)
            && self.route_due()
        {
            self.mixer.route_streams();
        }
        let mut mic = self.mic.lock().unwrap();
        if let Err(e) = self.mic_service.lock().unwrap().tick(&mut mic) {
            log::debug!("{}", tr!("Wave microphone: {error}", error = e));
        }
        drop(mic);
        self.watch_effects(false);
    }

    /// Neue Streams verteilen: sobald PipeWire einen meldet (sonst jede Sekunde); zur Sicherheit auch
    /// ohne Meldung alle 30 s.
    fn route_due(&self) -> bool {
        let mut r = self.route.lock().unwrap();
        let now = Instant::now();
        let streams = self.pulse.new_streams();
        if self.pulse.available()
            && r.0 == Some(streams)
            && r.1.is_some_and(|t| now.duration_since(t) < Duration::from_secs(30))
        {
            return false;
        }
        *r = (Some(streams), Some(now));
        true
    }

    /// Abgestürzte Effektketten und Rauschunterdrückung neu starten (alle 10 s).
    fn watch_effects(&self, force: bool) {
        {
            let mut last = self.fx_check.lock().unwrap();
            if (!force && last.is_some_and(|l| l.elapsed() < Duration::from_secs(10))) || !self.mixer_enabled() {
                return;
            }
            *last = Some(Instant::now());
        }
        if !self.mixer.is_active() {
            return;
        }
        let r = (|| -> crate::Result<()> {
            for c in self.mixer.all_channels() {
                let fx = self.mixer.fx();
                if fx.active(&c.id) && !fx.running(&c.id) {
                    log::info!("{}", tr!("Restarting effects for {name}", name = c.name));
                    self.mixer.rebuild_channel(&c.id)?;
                }
            }
            let ns = self.mixer.noise();
            if ns.enabled() && ns.backend().is_some() && !ns.running() {
                log::info!("{}", tr!("Restarting noise suppression"));
                self.mixer.rebuild_channel("mic")?;
            } else if !ns.enabled() && ns.running() {
                // in config.json ausgeschaltet
                self.mixer.rebuild_channel("mic")?;
            }
            Ok(())
        })();
        if let Err(e) = r {
            log::debug!("{}", tr!("Effects: {error}", error = e));
        }
    }

    /// Takt (jede Sekunde, bei SIGHUP sofort), bis `stop` gesetzt oder [`WaveHost::shutdown`] gerufen wird.
    pub fn run_loop(&self, stop: &AtomicBool) {
        while self.running.load(Ordering::SeqCst) && !stop.load(Ordering::SeqCst) {
            if RELOAD.swap(false, Ordering::SeqCst) {
                self.request_reload();
            }
            self.tick();
            for _ in 0..10 {
                if !self.running.load(Ordering::SeqCst) || stop.load(Ordering::SeqCst)
                    || self.reload.load(Ordering::SeqCst) || RELOAD.load(Ordering::SeqCst)
                {
                    break;
                }
                std::thread::sleep(Duration::from_millis(100));
            }
        }
    }

    pub fn shutdown(&self) {
        self.running.store(false, Ordering::SeqCst);
        self.pulse.stop();
    }

    /// Aufräumen beim Beenden: Ducking zurückstellen, Filterkette beenden. Kanäle bleiben bestehen,
    /// damit der Ton beim Neustart von FreeWave nicht abreißt.
    pub fn cleanup(&self) {
        self.pulse.stop();
        self.ducker.stop();
        let mut mic = self.mic.lock().unwrap();
        self.mic_service.lock().unwrap().stop(&mut mic);
    }

    /// Mixer ein- oder ausschalten (wird gespeichert).
    pub fn enable_mixer(&self, on: bool) -> crate::Result<()> {
        self.mixer.with_settings_mut(|s| {
            crate::mixer::obj(s.data_mut(), "mixer").insert("enabled".into(), json!(on));
            if let Err(e) = s.save() {
                log::warn!("config.json: {e}");
            }
        });
        if on {
            self.mixer.setup()?;
        } else {
            self.ducker.restore();
            self.mixer.teardown(true);
        }
        self.error.lock().unwrap().clear();
        Ok(())
    }
}

/// pactl des Mixers für das Wave-Mikrofon (gleiche Abfragen, gleiche Tests).
struct PactlRef(Arc<Mixer>);

impl Pactl for PactlRef {
    fn run(&self, args: &[String]) -> Result<String, crate::Error> {
        self.0.pactl().run(args)
    }
    fn list(&self, what: &str) -> String {
        self.0.pactl().list(what)
    }
    fn blocks(&self, what: &str) -> Vec<pactl::Block> {
        self.0.pactl().blocks(what)
    }
    fn available(&self) -> bool {
        self.0.pactl().available()
    }
}

static RELOAD: AtomicBool = AtomicBool::new(false);

extern "C" fn on_sighup(_: libc::c_int) {
    RELOAD.store(true, Ordering::SeqCst);
}

/// Hintergrunddienst von FreeWave (`freewave --hidden`): Kanäle beim Start anlegen bzw. wiederherstellen,
/// Programme in ihre Kanäle legen, Effekte/Rauschunterdrückung überwachen, Wave-Mikrofon-Filter und
/// Auto-Ducking – bis `stop` gesetzt wird. SIGHUP (FreeDeck hat Mikrofon-Einstellungen geändert) lädt
/// die Einstellungen neu. Schreibt `freewave.json` (ohne Fenster) und entfernt sie am Ende.
pub fn run_service(stop: Arc<AtomicBool>) {
    // SAFETY: der Handler setzt nur ein Atomic (async-signal-safe)
    unsafe {
        libc::signal(libc::SIGHUP, on_sighup as extern "C" fn(libc::c_int) as libc::sighandler_t);
    }
    let host = WaveHost::system();
    let dir = host.mixer.state_dir().to_path_buf();
    let _ = std::fs::create_dir_all(&dir);
    write_app_info(&dir, false, WAVE_APP_FILE);
    host.start();
    host.run_loop(&stop);
    host.shutdown();
    host.cleanup();
    clear_app_info(&dir, WAVE_APP_FILE);
}
