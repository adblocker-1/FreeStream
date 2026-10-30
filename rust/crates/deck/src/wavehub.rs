//! FreeWave im FreeDeck-Prozess (wie `MixerAction.mixer`, `DeckMeters` und `_DECK_MIX` in
//! `freestream/native.py`): ein gemeinsamer [`Mixer`] für alle Tasten und Decks, ein Stand zum Zeichnen
//! und das gemeinsame Wave-Mikrofon.
//!
//! Wie in Python (ab 0.37) fragt beim Zeichnen niemand PipeWire: ein Hintergrund-Thread liest einmal pro
//! Sekunde alle Lautstärken mit EINER pactl-Abfrage ([`Snap`]), solange FreeWave-Tasten zu sehen sind.
//! Tastendrücke zeigen den neuen Stand sofort ([`Hub::show`], gilt auch für die anderen Tasten desselben
//! Kanals) und schreiben danach in einem Arbeits-Thread. Die Live-Pegel der Fader misst ein eigener Thread
//! (parec, etwa 12× pro Sekunde) nur, solange Fader-Tasten zu sehen sind.
//!
//! Mit FreeWave im Hintergrund (Dienst `freewave --hidden`) arbeiten beide über dieselben PipeWire-Objekte
//! und Zustandsdateien (`mixer.json`, `ducking.json`, `wavemic.json`, `freewave.json`) – wie in Python.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, OnceLock};
use std::time::{Duration, Instant};

use freestream_wave::levels::{self, LevelMonitor};
use freestream_wave::mixer::{Levels, Scene};
use freestream_wave::wavemic::WaveMic;
use freestream_wave::{Channel, Mix, Mixer};

/// So lange nach dem letzten Zeichnen/Takt einer FreeWave-Taste läuft das Nachlesen weiter.
const KEEP_POLLING: Duration = Duration::from_secs(3);
/// Abstand zwischen zwei Abfragen (wie der Sekundentakt in Python)
const POLL_EVERY: Duration = Duration::from_secs(1);
/// So lange gilt ein gezeigter Stand für den nächsten Schritt (wie `_fresh_level`)
const FRESH: Duration = Duration::from_millis(1500);
/// Live-Pegel: Messungen pro Sekunde (wie `DeckMeters.HZ`)
const METER_HZ: u64 = 12;
/// Live-Pegel: ohne Takt der Fader-Tasten endet die Messung nach dieser Zeit
const METER_KEEP: Duration = Duration::from_millis(1200);

/// Was die Tasten brauchen (nur das wird gelesen).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Need {
    /// Lautstärken (Kanal-Tasten, Szenen)
    Levels = 1,
    /// Name des Abhörgeräts (Ausgabe-Taste)
    Output = 2,
    /// aktive Szene
    Scenes = 4,
}

/// Stand des Mixers zum Zeichnen.
#[derive(Debug, Clone, Default)]
pub struct Snap {
    /// Wann die Lautstärken abgefragt wurden (None: noch nie)
    pub asked: Option<Instant>,
    pub channels: Vec<Channel>,
    pub mixes: Vec<Mix>,
    /// Lautstärken je „<Kanal>_<Mix>“ (beim Auto-Ducking die Werte des Nutzers)
    pub levels: Option<Levels>,
    pub scenes: Vec<Scene>,
    /// id der Szene, die dem Mixer gerade entspricht
    pub active_scene: Option<String>,
    pub output_name: String,
    /// gelesene Teile (Bits von [`Need`])
    have: u8,
}

impl Snap {
    /// Kanal nach id oder Name (ohne Groß-/Kleinschreibung) wie `Mixer.channel`.
    pub fn channel(&self, cid: &str) -> Option<&Channel> {
        let c = cid.to_lowercase();
        self.channels.iter().find(|x| x.id.to_lowercase() == c || x.name.to_lowercase() == c)
    }

    /// Mix nach id oder Name wie `Mixer.mix`.
    pub fn mix(&self, mid: &str) -> Option<&Mix> {
        let m = mid.to_lowercase();
        self.mixes.iter().find(|x| x.id == m || x.name.to_lowercase() == m)
    }

    pub fn mix_ids(&self) -> Vec<String> {
        self.mixes.iter().map(|m| m.id.clone()).collect()
    }

    /// Mixe, in die ein Kanal geht.
    pub fn channel_mixes(&self, cid: &str) -> Vec<String> {
        self.mixes.iter().filter(|m| !m.exclude.iter().any(|x| x == cid)).map(|m| m.id.clone()).collect()
    }

    /// (Lautstärke, stumm) eines Kanals in einem Mix („both“: alle seine Mixe – die lauteste, stumm nur,
    /// wenn alle stumm sind) wie `MixerAction._level`. (None, false): Mixer aus / Kanal nicht da.
    pub fn level(&self, cid: &str, mix: &str) -> (Option<i64>, bool) {
        let Some(levels) = &self.levels else { return (None, false) };
        let mixes = if mix == "both" { self.channel_mixes(cid) } else { vec![mix.to_string()] };
        let vals: Vec<Option<(i64, bool)>> =
            mixes.iter().map(|m| levels.get(&format!("{cid}_{m}")).map(|l| (l.volume, l.mute))).collect();
        let vols: Vec<i64> = vals.iter().flatten().map(|v| v.0).collect();
        if vols.is_empty() {
            return (None, false);
        }
        (vols.iter().copied().max(), vals.iter().all(|v| v.map(|v| v.1).unwrap_or(false)))
    }
}

/// Zuletzt gezeigter eigener Stand eines Kanals/Mixes (nach einem Tastendruck).
#[derive(Debug, Clone, Copy)]
struct Known {
    vol: Option<i64>,
    mute: bool,
    at: Instant,
}

#[derive(Default)]
struct Inner {
    snap: Arc<Snap>,
    known: HashMap<(String, String), Known>,
    /// letzter Bedarf je [`Need`]
    beats: HashMap<u8, Instant>,
    polling: bool,
    refresh: bool,
}

#[derive(Default)]
struct Meters {
    /// Kanal -> zuletzt gewünscht
    want: HashMap<String, Instant>,
    running: bool,
    /// Rohpegel 0..1 je Kanal (vor dem Regler)
    raw: HashMap<String, f64>,
}

/// FreeWave für die Deck-Tasten.
pub struct Hub {
    mixer: Arc<Mixer>,
    inner: Mutex<Inner>,
    wake: Condvar,
    /// steigt bei jeder Änderung des Stands (Tasten zeichnen dann neu, falls sich ihr Bild ändert)
    version: AtomicU64,
    meters: Mutex<Meters>,
    meter_version: AtomicU64,
    /// Live-Pegel messen (Tests schalten das ab)
    meters_enabled: bool,
}

static HUB: Mutex<Option<Arc<Hub>>> = Mutex::new(None);

/// Der gemeinsame FreeWave-Zugang (beim ersten Aufruf mit `config.json`, echtem pactl und
/// `~/.local/state/freestream`).
pub fn hub() -> Arc<Hub> {
    let mut h = HUB.lock().unwrap();
    if let Some(h) = h.as_ref() {
        return h.clone();
    }
    // Änderungen aus FreeWave/pavucontrol sofort mitbekommen (sonst springt die Lautstärke)
    freestream_wave::mixer::want_watch();
    let new = Arc::new(Hub::new(Arc::new(Mixer::system()), levels::parec_available()));
    *h = Some(new.clone());
    new
}

/// Einen anderen Mixer benutzen (Tests; ein Programm mit eigenem FreeWave-Host im selben Prozess).
pub fn use_mixer(mixer: Arc<Mixer>, meters: bool) -> Arc<Hub> {
    let new = Arc::new(Hub::new(mixer, meters));
    *HUB.lock().unwrap() = Some(new.clone());
    new
}

impl Hub {
    fn new(mixer: Arc<Mixer>, meters_enabled: bool) -> Self {
        Self {
            mixer,
            inner: Mutex::new(Inner::default()),
            wake: Condvar::new(),
            version: AtomicU64::new(1),
            meters: Mutex::new(Meters::default()),
            meter_version: AtomicU64::new(1),
            meters_enabled,
        }
    }

    pub fn mixer(&self) -> &Arc<Mixer> {
        &self.mixer
    }

    /// Zähler für „hat sich etwas geändert?“ (Stand, eigene Änderungen, Live-Pegel, Mix-Wahl).
    pub fn version(&self) -> u64 {
        self.version.load(Ordering::SeqCst) + self.meter_version.load(Ordering::SeqCst) + mix_generation()
    }

    /// Stand zum Zeichnen. Fehlt ein Teil (erstes Bild einer Seite), wird er sofort gelesen – danach liest
    /// der Hintergrund-Thread nach, solange Tasten danach fragen.
    pub fn snap(self: &Arc<Self>, need: &[Need]) -> Arc<Snap> {
        let missing = {
            let mut inner = self.inner.lock().unwrap();
            let now = Instant::now();
            for n in need {
                inner.beats.insert(*n as u8, now);
            }
            // Einstellungen braucht jede Taste (Kanäle, Mixe)
            inner.beats.insert(0, now);
            let have = inner.snap.have;
            let missing = inner.snap.asked.is_none() || need.iter().any(|n| have & *n as u8 == 0);
            if !inner.polling {
                inner.polling = true;
                let me = self.clone();
                let ok = std::thread::Builder::new().name("freewave-deck-poll".into()).spawn(move || me.poll_loop());
                if ok.is_err() {
                    inner.polling = false;
                }
            }
            missing
        };
        if missing {
            self.refresh();
        }
        self.inner.lock().unwrap().snap.clone()
    }

    /// Stand ohne Abfrage (für den schnellen Takt).
    pub fn current(&self) -> Arc<Snap> {
        self.inner.lock().unwrap().snap.clone()
    }

    /// Stand neu lesen (eine pactl-Abfrage für alle Tasten).
    pub fn refresh(&self) {
        let needs: u8 = {
            let inner = self.inner.lock().unwrap();
            inner.beats.iter().filter(|(_, t)| t.elapsed() < KEEP_POLLING).fold(0, |a, (n, _)| a | n)
        };
        let mx = &self.mixer;
        let asked = Instant::now();
        let snap = freestream_wave::pactl::snapshot(|| {
            // FreeWave-Fenster hat Kanäle oder Mixe geändert: config.json neu lesen
            mx.with_settings_mut(|s| s.reload_if_changed());
            let mut s = Snap { asked: Some(asked), channels: mx.all_channels(), mixes: mx.mixes(), have: needs, ..Default::default() };
            if needs & (Need::Levels as u8 | Need::Scenes as u8) != 0 {
                s.levels = Some(mx.levels());
                s.have |= Need::Levels as u8;
            }
            if needs & Need::Scenes as u8 != 0 {
                s.scenes = mx.scenes();
                s.active_scene = s.levels.as_ref().filter(|l| !l.is_empty())
                    .and_then(|l| mx.active_scene(Some(l))).map(|sc| sc.id);
            }
            if needs & Need::Output as u8 != 0 {
                s.output_name = mx.monitor_output_name();
            }
            s
        });
        let mut inner = self.inner.lock().unwrap();
        // eigene Stände, die älter als diese Abfrage sind, gelten nicht mehr
        inner.known.retain(|_, k| k.at > asked);
        inner.snap = Arc::new(snap);
        self.version.fetch_add(1, Ordering::SeqCst);
    }

    /// Bald neu lesen (nach einer Szene, einem Fehler …).
    pub fn request_refresh(&self) {
        let mut inner = self.inner.lock().unwrap();
        inner.refresh = true;
        self.wake.notify_all();
    }

    fn poll_loop(self: Arc<Self>) {
        loop {
            {
                let mut inner = self.inner.lock().unwrap();
                let wanted = inner.beats.values().any(|t| t.elapsed() < KEEP_POLLING);
                // ein neuerer Hub (anderer Mixer) übernimmt
                let current = HUB.lock().unwrap().as_ref().map(|h| Arc::ptr_eq(h, &self)).unwrap_or(false);
                if !wanted || !current {
                    inner.polling = false;
                    return;
                }
                if !inner.refresh {
                    inner = self.wake.wait_timeout(inner, POLL_EVERY).unwrap().0;
                }
                inner.refresh = false;
            }
            self.refresh();
        }
    }

    /// Gezeigter Stand (Lautstärke, stumm): eigener Stand nach einem Tastendruck, sonst die letzte Abfrage.
    pub fn level(&self, snap: &Snap, cid: &str, mix: &str) -> (Option<i64>, bool) {
        let inner = self.inner.lock().unwrap();
        match inner.known.get(&(cid.to_string(), mix.to_string())) {
            Some(k) if snap.asked.map(|a| k.at > a).unwrap_or(true) => (k.vol, k.mute),
            _ => snap.level(cid, mix),
        }
    }

    /// Stand für den nächsten Schritt (wie `_fresh_level`): der eben gezeigte eigene Stand, sonst die
    /// zuletzt gelesenen Loopbacks samt eigener Schreibvorgänge, sonst die letzte Abfrage, wenn sie frisch
    /// ist. None = erst nach dem Schreiben zeigen.
    pub fn fresh_level(&self, cid: &str, mix: &str) -> Option<(i64, bool)> {
        let snap = {
            let inner = self.inner.lock().unwrap();
            if let Some(k) = inner.known.get(&(cid.to_string(), mix.to_string())) {
                if k.at.elapsed() < FRESH {
                    if let Some(v) = k.vol {
                        return Some((v, k.mute));
                    }
                }
            }
            inner.snap.clone()
        };
        if let Some(lv) = self.mixer.recent_level(cid, mix) {
            return Some(lv);
        }
        if snap.asked.map(|a| a.elapsed() < FRESH).unwrap_or(false) {
            if let (Some(v), m) = self.level(&snap, cid, mix) {
                return Some((v, m));
            }
        }
        None
    }

    /// Neuen Stand sofort zeigen (alle Tasten dieses Kanals und Mixes) – geschrieben wird danach.
    pub fn show(&self, cid: &str, mix: &str, vol: Option<i64>, mute: Option<bool>) {
        let snap = self.current();
        let (old_vol, old_mute) = self.level(&snap, cid, mix);
        let k = Known { vol: vol.or(old_vol), mute: mute.unwrap_or(old_mute), at: Instant::now() };
        self.inner.lock().unwrap().known.insert((cid.to_string(), mix.to_string()), k);
        self.version.fetch_add(1, Ordering::SeqCst);
    }

    /// Nach dem Schreiben: den geschriebenen Stand zeigen – außer, ein neuerer Tastendruck hat seit
    /// `started` schon etwas anderes gezeigt (sonst springt die Anzeige beim schnellen Drehen zurück).
    pub fn confirm(&self, cid: &str, mix: &str, vol: Option<i64>, mute: Option<bool>, started: Instant) {
        let mut inner = self.inner.lock().unwrap();
        let key = (cid.to_string(), mix.to_string());
        let (old_vol, old_mute) = match inner.known.get(&key) {
            Some(k) if k.at > started => return,
            Some(k) => (k.vol, k.mute),
            None => inner.snap.level(cid, mix),
        };
        inner.known.insert(key, Known { vol: vol.or(old_vol), mute: mute.unwrap_or(old_mute), at: Instant::now() });
        drop(inner);
        self.version.fetch_add(1, Ordering::SeqCst);
    }

    /// Gezeigter Stand stimmt nicht (Schreiben fehlgeschlagen): wieder die Abfrage zeigen.
    pub fn forget(&self, cid: &str, mix: &str) {
        self.inner.lock().unwrap().known.remove(&(cid.to_string(), mix.to_string()));
        self.version.fetch_add(1, Ordering::SeqCst);
        self.request_refresh();
    }

    // ------------------------------------------------------------------------------------- Live-Pegel
    /// Fader-Taste sichtbar: Pegel dieses Kanals messen (gilt etwa eine Sekunde, der Takt erneuert es).
    pub fn want_meter(self: &Arc<Self>, cid: &str) {
        if !self.meters_enabled || cid.is_empty() {
            return;
        }
        let mut m = self.meters.lock().unwrap();
        m.want.insert(cid.to_string(), Instant::now());
        if !m.running {
            m.running = true;
            let me = self.clone();
            if std::thread::Builder::new().name("deck-meters".into()).spawn(move || me.meter_loop()).is_err() {
                m.running = false;
            }
        }
    }

    /// Gemessener Pegel eines Kanals (vor dem Regler); None = ohne Messung (die Leiste folgt der Lautstärke).
    pub fn meter(&self, cid: &str) -> Option<f64> {
        self.meters.lock().unwrap().raw.get(cid).copied()
    }

    /// Nur für Tests: gemessene Pegel vorgeben.
    pub fn set_meter(&self, cid: &str, raw: Option<f64>) {
        let mut m = self.meters.lock().unwrap();
        match raw {
            Some(v) => m.raw.insert(cid.to_string(), v),
            None => m.raw.remove(cid),
        };
        self.meter_version.fetch_add(1, Ordering::SeqCst);
    }

    fn meter_loop(self: Arc<Self>) {
        // eigener Monitor je Thread: endet dieser Thread erst, wenn schon ein neuer läuft, beendet er nur
        // seine eigenen parec-Prozesse
        let mut mon = LevelMonitor::new();
        let mut sources: HashMap<String, String> = HashMap::new();
        let mut sources_at: Option<Instant> = None;
        loop {
            let chans: Vec<String> = {
                let mut m = self.meters.lock().unwrap();
                m.want.retain(|_, t| t.elapsed() < METER_KEEP);
                if m.want.is_empty() {
                    m.running = false;
                    m.raw.clear();
                    break;
                }
                m.want.keys().cloned().collect()
            };
            // PipeWire-Quellen aller Kanäle samt Mikrofon (alle 10 s neu nachgesehen)
            if sources_at.map(|t| t.elapsed() > Duration::from_secs(10)).unwrap_or(true) {
                sources_at = Some(Instant::now());
                sources = freestream_wave::pactl::snapshot(|| {
                    levels::channel_sources(&self.mixer, &self.mixer.all_channels()).into_iter().collect()
                });
            }
            let targets: indexmap::IndexMap<String, String> =
                chans.iter().filter_map(|c| sources.get(c).map(|s| (c.clone(), s.clone()))).collect();
            mon.watch(&targets);
            let lv = mon.read();
            {
                let mut m = self.meters.lock().unwrap();
                let raw: HashMap<String, f64> = lv.into_iter().collect();
                if raw != m.raw {
                    m.raw = raw;
                    self.meter_version.fetch_add(1, Ordering::SeqCst);
                }
            }
            std::thread::sleep(Duration::from_millis(1000 / METER_HZ));
        }
        mon.stop();
        self.meter_version.fetch_add(1, Ordering::SeqCst);
    }
}

// ------------------------------------------------------------------------------------- Mix-Wahl
static DECK_MIX: Mutex<String> = Mutex::new(String::new());
static MIX_GEN: AtomicU64 = AtomicU64::new(0);

/// Mix, den die Mix-Wahl-Tasten gerade gewählt haben (gilt für Kanäle mit mix „selected“).
pub fn deck_mix() -> String {
    let m = DECK_MIX.lock().unwrap();
    if m.is_empty() { "both".into() } else { m.clone() }
}

pub fn set_deck_mix(mix: &str) {
    *DECK_MIX.lock().unwrap() = mix.to_string();
    MIX_GEN.fetch_add(1, Ordering::SeqCst);
}

fn mix_generation() -> u64 {
    MIX_GEN.load(Ordering::SeqCst)
}

// ------------------------------------------------------------------------------------- Wave-Mikrofon
/// Das gemeinsame Wave-Mikrofon (wie `wavemic.instance`).
pub fn wave_mic() -> &'static Mutex<WaveMic> {
    static MIC: OnceLock<Mutex<WaveMic>> = OnceLock::new();
    MIC.get_or_init(|| {
        let cfg = freestream_core::config::Config::load();
        let conf = cfg.data.get("wave_mic").and_then(serde_json::Value::as_object).cloned();
        Mutex::new(WaveMic::new(conf.as_ref(), Arc::new(freestream_wave::pactl::SystemPactl),
                                freestream_core::paths::state_dir()))
    })
}

// ------------------------------------------------------------------------------------- Arbeits-Thread
/// Schreiben an PipeWire/Wave:3 in der Reihenfolge der Tastendrücke, ohne die Steuerung aufzuhalten.
pub static WORKER: crate::native::Worker = crate::native::Worker::new("freewave-deck");
