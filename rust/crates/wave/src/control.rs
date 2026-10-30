//! Schnittstelle für das Fenster (GTK4): der Hauptthread liest einen fertigen [`Snapshot`] und die
//! Pegel, ohne je auf pactl zu warten; Aktionen laufen in einem Arbeits-Thread.
//!
//! ```ignore
//! let host = Arc::new(WaveHost::system());
//! let ctl = Controller::new(host.clone());
//! // im GTK-Takt (z. B. glib::timeout_add_local 40 ms):
//! let snap = ctl.snapshot();          // Arc<Snapshot>, nur ein Lesezugriff
//! let meters = ctl.levels();          // {"music": 0.4, "mix:stream": 0.7, …}
//! for e in ctl.take_errors() { /* Meldung anzeigen */ }
//! // Regler bewegt:
//! ctl.set_volume("music", "monitor", 40);
//! ctl.act(|m| m.set_member("music", "stream", false));
//! ```

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{channel, Receiver, Sender};
use std::sync::{Arc, Mutex, RwLock};
use std::time::Duration;

use indexmap::IndexMap;

use crate::ducking::{self, DuckSettings};
use crate::host::WaveHost;
use crate::levels::{self, LevelMonitor};
use crate::mixer::{Channel, Device, Level, Mix, Mixer, Scene, Stream};
use crate::noise::NoiseStatus;
use crate::pactl;
use crate::wavemic::MicStatus;
use crate::Result;

/// Ein Kanal mit allem, was die Anzeige braucht.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct ChannelView {
    pub channel: Channel,
    /// Regler gekoppelt (Schloss)
    pub linked: bool,
    /// Hauptregler (None: Kanal nicht eingerichtet)
    pub master: Option<Level>,
    /// Lautstärke je Mix, in dem der Kanal ist (beim Ducking der Wert des Nutzers)
    pub levels: IndexMap<String, Level>,
    /// Mixe, in die der Kanal geht
    pub mixes: Vec<String>,
    pub effects_active: bool,
    pub effects_running: bool,
    /// Programme, die gerade in diesem Kanal spielen
    pub streams: Vec<Stream>,
    /// Eingangsgerät bei Eingangskanälen und dem Mikrofon (Name, Beschreibung; leer: keins)
    pub input: Device,
}

/// Ein Mix für die Anzeige.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct MixView {
    pub mix: Mix,
    /// tatsächliche Ausgabegeräte
    pub devices: Vec<String>,
    pub can_listen: bool,
    /// Lautstärke des Mixes (Mix-Sink, bei direktem Mix das Gerät; None: unbekannt)
    pub volume: Option<Level>,
    /// Lautstärke/stumm je Ausgabegerät des Mixes
    pub device_levels: IndexMap<String, Level>,
}

/// Auto-Ducking für die Anzeige.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct DuckView {
    pub settings: Option<DuckSettings>,
    /// 0 … 1
    pub depth: f64,
    /// Mikrofonpegel 0 … 1
    pub level: f64,
}

/// Zustand des Mixers zum Zeichnen – wird im Hintergrund erneuert.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Snapshot {
    /// steigt mit jeder Erneuerung
    pub version: u64,
    /// in den Einstellungen eingeschaltet
    pub enabled: bool,
    /// in PipeWire eingerichtet
    pub active: bool,
    /// Fehler beim Einrichten (Host)
    pub error: String,
    pub channels: Vec<ChannelView>,
    pub mixes: Vec<MixView>,
    /// Mix, der gerade auf dem Abhörgerät läuft
    pub listening: String,
    pub monitor_output: String,
    pub monitor_output_name: String,
    pub outputs: Vec<Device>,
    pub inputs: Vec<Device>,
    /// alle Programme (auch außerhalb des Mixers)
    pub streams: Vec<Stream>,
    pub scenes: Vec<Scene>,
    pub active_scene: Option<String>,
    pub ducking: DuckView,
    pub noise: NoiseStatus,
    pub mic: MicStatus,
}

impl Snapshot {
    pub fn channel(&self, cid: &str) -> Option<&ChannelView> {
        self.channels.iter().find(|c| c.channel.id == cid)
    }
}

/// Zustand aus PipeWire und den Einstellungen lesen (blockiert – im Arbeits-Thread aufrufen).
pub fn build_snapshot(host: &WaveHost, version: u64) -> Snapshot {
    let m: &Mixer = &host.mixer;
    pactl::snapshot(|| {
        let active = m.is_active();
        let streams = m.streams();
        let levels = m.levels();
        let masters = m.masters();
        let st = m.state();
        let mixes = m.mixes();
        let fx = m.fx();
        let sinks = m.sink_levels();
        let channels = m.all_channels().into_iter().map(|c| {
            let member: Vec<String> = mixes.iter().filter(|x| !x.exclude.contains(&c.id)).map(|x| x.id.clone()).collect();
            ChannelView {
                linked: m.is_linked(&c.id),
                master: masters.get(&c.id).copied(),
                levels: member.iter().filter_map(|mid| levels.get(&format!("{}_{mid}", c.id)).map(|l| (mid.clone(), *l)))
                    .collect(),
                mixes: member,
                effects_active: fx.active(&c.id),
                effects_running: fx.running(&c.id),
                streams: streams.iter().filter(|s| s.channel.as_deref() == Some(c.id.as_str())).cloned().collect(),
                input: m.channel_input(&c, &st),
                channel: c,
            }
        }).collect();
        let mix_views = mixes.into_iter().map(|x| {
            let devices = m.mix_devices(&x, Some(&st));
            let volume = sinks.get(&Mixer::mix_sink(&x))
                .or_else(|| if devices.len() == 1 && !x.virtual_ { sinks.get(&devices[0]) } else { None }).copied();
            MixView {
                device_levels: devices.iter().filter_map(|d| sinks.get(d).map(|l| (d.clone(), *l))).collect(),
                volume,
                devices,
                can_listen: m.can_listen(&x.id),
                mix: x,
            }
        }).collect();
        let conf = m.conf();
        let mic = host.mic.lock().map(|mut w| w.status()).unwrap_or_default();
        Snapshot {
            version,
            enabled: m.enabled(),
            active,
            error: host.error(),
            channels,
            mixes: mix_views,
            listening: m.listening(),
            monitor_output: st.get("monitor_output").and_then(|v| v.as_str()).unwrap_or("").to_string(),
            monitor_output_name: m.monitor_output_name(),
            outputs: m.outputs(),
            inputs: m.inputs(),
            active_scene: m.active_scene(Some(&levels)).map(|s| s.id),
            scenes: m.scenes(),
            streams,
            ducking: DuckView {
                settings: Some(ducking::settings(&conf)),
                depth: host.ducker.depth(),
                level: host.ducker.level(),
            },
            noise: m.noise().status(),
            mic,
        }
    })
}

type Job = Box<dyn FnOnce(&WaveHost) -> Result<()> + Send>;

enum Msg {
    Job(Job),
    Quit,
}

/// Verbindung zwischen Fenster und Mixer.
pub struct Controller {
    host: Arc<WaveHost>,
    snap: Arc<RwLock<Arc<Snapshot>>>,
    meter: Arc<Mutex<LevelMonitor>>,
    meters_on: Arc<AtomicBool>,
    jobs: Sender<Msg>,
    errors: Arc<Mutex<Vec<String>>>,
    pending: Arc<Mutex<HashMap<(String, String), i64>>>,
    wanted: Arc<AtomicU64>,
    quit: Arc<AtomicBool>,
}

impl Controller {
    /// Arbeits-Thread und Beobachter starten; der erste Snapshot kommt sofort im Hintergrund.
    pub fn new(host: Arc<WaveHost>) -> Self {
        let (tx, rx) = channel::<Msg>();
        let me = Self {
            host,
            snap: Arc::new(RwLock::new(Arc::new(Snapshot::default()))),
            meter: Arc::new(Mutex::new(LevelMonitor::new())),
            meters_on: Arc::new(AtomicBool::new(false)),
            jobs: tx,
            errors: Arc::new(Mutex::new(Vec::new())),
            pending: Arc::new(Mutex::new(HashMap::new())),
            wanted: Arc::new(AtomicU64::new(1)),
            quit: Arc::new(AtomicBool::new(false)),
        };
        me.spawn_worker(rx);
        me.spawn_watcher();
        me
    }

    pub fn host(&self) -> &Arc<WaveHost> {
        &self.host
    }

    /// Letzter Zustand (sofort, ohne pactl).
    pub fn snapshot(&self) -> Arc<Snapshot> {
        self.snap.read().unwrap().clone()
    }

    /// Pegel 0..1 je Kanal und "mix:<id>" (nur bei eingeschalteten Pegeln, sonst leer).
    pub fn levels(&self) -> IndexMap<String, f64> {
        if !self.meters_on.load(Ordering::SeqCst) {
            return IndexMap::new();
        }
        self.meter.lock().map(|mut m| m.read()).unwrap_or_default()
    }

    /// Pegel messen (Fenster sichtbar) oder nicht (versteckt: alle parec-Prozesse enden).
    pub fn set_meters(&self, on: bool) {
        self.meters_on.store(on, Ordering::SeqCst);
        if !on {
            if let Ok(mut m) = self.meter.lock() {
                m.stop();
            }
        }
        self.refresh();
    }

    /// Zustand bald neu lesen.
    pub fn refresh(&self) {
        self.wanted.fetch_add(1, Ordering::SeqCst);
    }

    /// Fehlermeldungen der Aktionen seit dem letzten Aufruf.
    pub fn take_errors(&self) -> Vec<String> {
        std::mem::take(&mut *self.errors.lock().unwrap())
    }

    /// Aktion im Arbeits-Thread ausführen; danach wird der Zustand neu gelesen. Fehler landen in
    /// [`Controller::take_errors`].
    pub fn act(&self, f: impl FnOnce(&Mixer) -> Result<()> + Send + 'static) {
        self.act_host(move |h| f(&h.mixer));
    }

    /// Wie [`Controller::act`], mit Zugriff auf den Host (Mikrofon, Ducking, Mixer an/aus).
    pub fn act_host(&self, f: impl FnOnce(&WaveHost) -> Result<()> + Send + 'static) {
        let _ = self.jobs.send(Msg::Job(Box::new(f)));
    }

    /// Lautstärke setzen; schnelle Folgen (Regler ziehen) werden zusammengefasst – es gilt der letzte Wert.
    pub fn set_volume(&self, cid: &str, mix: &str, pct: i64) {
        let key = (cid.to_string(), mix.to_string());
        let first = self.pending.lock().unwrap().insert(key.clone(), pct).is_none();
        if first {
            let pending = self.pending.clone();
            self.act(move |m| {
                let Some(v) = pending.lock().unwrap().remove(&key) else { return Ok(()) };
                m.set_volume(&key.0, &key.1, v)
            });
        }
    }

    fn spawn_worker(&self, rx: Receiver<Msg>) {
        let host = self.host.clone();
        let errors = self.errors.clone();
        let wanted = self.wanted.clone();
        let _ = std::thread::Builder::new().name("freewave-actions".into()).spawn(move || {
            while let Ok(Msg::Job(job)) = rx.recv() {
                if let Err(e) = job(&host) {
                    errors.lock().unwrap().push(e.0);
                }
                wanted.fetch_add(1, Ordering::SeqCst);
            }
        });
    }

    fn spawn_watcher(&self) {
        let host = self.host.clone();
        let snap = self.snap.clone();
        let wanted = self.wanted.clone();
        let quit = self.quit.clone();
        let meter = self.meter.clone();
        let meters_on = self.meters_on.clone();
        let _ = std::thread::Builder::new().name("freewave-snapshot".into()).spawn(move || {
            let (mut done, mut version) = (0u64, 0u64);
            let mut seen_pulse = host.pulse().version();
            let mut last = std::time::Instant::now() - Duration::from_secs(10);
            while !quit.load(Ordering::SeqCst) {
                let want = wanted.load(Ordering::SeqCst);
                let pulse = host.pulse().version();
                // erneuern: auf Wunsch, bei PipeWire-Ereignissen, sonst jede Sekunde
                if want != done || pulse != seen_pulse || last.elapsed() >= Duration::from_secs(1) {
                    done = want;
                    seen_pulse = pulse;
                    last = std::time::Instant::now();
                    version += 1;
                    let s = build_snapshot(&host, version);
                    if meters_on.load(Ordering::SeqCst) {
                        let chans: Vec<Channel> = s.channels.iter().map(|c| c.channel.clone()).collect();
                        let targets = levels::channel_sources(&host.mixer, &chans);
                        if let Ok(mut m) = meter.lock() {
                            m.watch(&targets);
                        }
                    }
                    *snap.write().unwrap() = Arc::new(s);
                }
                std::thread::sleep(Duration::from_millis(50));
            }
        });
    }
}

impl Drop for Controller {
    fn drop(&mut self) {
        self.quit.store(true, Ordering::SeqCst);
        let _ = self.jobs.send(Msg::Quit);
        if let Ok(mut m) = self.meter.lock() {
            m.stop();
        }
    }
}

/// Warten, bis ein Snapshot mit mindestens `version` da ist (Tests, Kommandozeile).
pub fn wait_for(ctl: &Controller, version: u64, timeout: Duration) -> Option<Arc<Snapshot>> {
    let end = std::time::Instant::now() + timeout;
    loop {
        let s = ctl.snapshot();
        if s.version >= version {
            return Some(s);
        }
        if std::time::Instant::now() >= end {
            return None;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}
