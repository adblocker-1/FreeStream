//! Pegelanzeige für FreeWave: echte Audiopegel über `parec` – wie `freestream/levels.py`.
//!
//! Für jede beobachtete Quelle (Monitor eines Kanal-Sinks, Mikrofon, die Mixe) läuft ein kleiner
//! `parec`-Prozess mit niedriger Rate (16 kHz, s16 mono, 25 Spitzenwerte pro Sekunde). Ein Lese-Thread
//! je Prozess merkt sich den letzten Spitzenwert; das Fenster fragt die Werte mit
//! [`LevelMonitor::read`] ab und bekommt sie mit sanftem Abfall (wie ein Peak-Meter).
//!
//! `parec` hängt fest an seiner Quelle: verschwindet sie, beendet er sich – [`LevelMonitor::watch`]
//! startet ihn dann neu (höchstens alle RESTART_S Sekunden).

use std::collections::HashMap;
use std::io::Read;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;

use indexmap::IndexMap;

use crate::mixer::{Channel, Mixer};
use crate::pactl::which;

pub const RATE: u32 = 16000;
pub const HZ: u32 = 25;
/// Bytes je Messung (s16 mono)
pub const CHUNK: usize = (RATE / HZ * 2) as usize;
pub const FLOOR_DB: f64 = -60.0;
/// so viel (von 0..1) fällt die Anzeige pro Sekunde höchstens
pub const FALL_PER_S: f64 = 1.6;
/// beendetes parec frühestens so lange nach dem Start neu starten (Quelle fehlt noch)
pub const RESTART_S: f64 = 2.0;

/// Spitzenwert eines s16le-Blocks als Anzeigewert 0..1 (−60 dB … 0 dB).
pub fn peak_level(data: &[u8]) -> f64 {
    let mut peak: i32 = 0;
    for ch in data.chunks_exact(2) {
        let v = i32::from(i16::from_le_bytes([ch[0], ch[1]]));
        peak = peak.max(v.abs());
    }
    if peak <= 0 {
        return 0.0;
    }
    let db = 20.0 * (f64::from(peak) / 32768.0).log10();
    ((db - FLOOR_DB) / -FLOOR_DB).clamp(0.0, 1.0)
}

/// Pegel nach dem Regler: Lautstärke in dB auf die Anzeige (−60…0 dB) umgerechnet.
///
/// PipeWire/PulseAudio-Prozent sind kubisch (Amplitude = (pct/100)³): 50 % = −18 dB, 10 % = −60 dB.
pub fn post_fader(level: f64, volume: Option<f64>, muted: bool) -> f64 {
    let Some(volume) = volume.filter(|v| *v > 0.0) else { return 0.0 };
    if muted || level <= 0.0 {
        return 0.0;
    }
    (level + 60.0 * (volume.min(150.0) / 100.0).log10() / -FLOOR_DB).clamp(0.0, 1.0)
}

/// Anzeigewert 0..1 zurück in dB (None bei Stille).
pub fn level_db(level: f64) -> Option<f64> {
    if level <= 0.0 {
        return None;
    }
    Some(FLOOR_DB + level * -FLOOR_DB)
}

/// Eine Pegelquelle (austauschbar für Tests).
pub trait LevelSource: Send {
    /// letzter Spitzenwert 0..1
    fn level(&self) -> f64;
    /// beendet (Quelle entfernt) – dann neu starten
    fn ended(&mut self) -> bool {
        false
    }
    fn stop(&mut self);
}

/// Ein `parec`-Prozess für eine Quelle.
pub struct ParecSource {
    pub device: String,
    level: Arc<AtomicU64>,
    proc: Option<Child>,
    stopped: Arc<AtomicBool>,
}

impl ParecSource {
    /// Befehlszeile für eine Quelle (wie in Python).
    pub fn command(exe: &str, device: &str) -> Vec<String> {
        vec![
            exe.to_string(),
            format!("--device={device}"),
            "--raw".into(),
            "--format=s16le".into(),
            format!("--rate={RATE}"),
            "--channels=1".into(),
            format!("--latency-msec={}", 1000 / HZ),
            // dont-reconnect: wird die Quelle entfernt, endet parec, statt die Standardquelle zu messen
            "--property=node.dont-reconnect=true".into(),
        ]
    }

    pub fn new(device: &str) -> Self {
        let mut me = Self {
            device: device.to_string(),
            level: Arc::new(AtomicU64::new(0f64.to_bits())),
            proc: None,
            stopped: Arc::new(AtomicBool::new(false)),
        };
        let Some(exe) = which("parec") else { return me };
        if device.is_empty() {
            return me;
        }
        let args = Self::command(&exe.to_string_lossy(), device);
        let child = Command::new(&args[0]).args(&args[1..]).stdin(Stdio::null()).stdout(Stdio::piped())
            .stderr(Stdio::null()).spawn();
        let mut child = match child {
            Ok(c) => c,
            Err(e) => {
                log::debug!("parec {device}: {e}");
                return me;
            }
        };
        let out = child.stdout.take();
        let (level, stopped) = (me.level.clone(), me.stopped.clone());
        let _ = std::thread::Builder::new().name("freewave-level".into()).spawn(move || {
            let Some(mut out) = out else { return };
            let mut buf = vec![0u8; CHUNK];
            while !stopped.load(Ordering::Relaxed) {
                match read_chunk(&mut out, &mut buf) {
                    Some(n) if n > 0 => level.store(peak_level(&buf[..n]).to_bits(), Ordering::Relaxed),
                    _ => break,
                }
            }
            level.store(0f64.to_bits(), Ordering::Relaxed);
        });
        me.proc = Some(child);
        me
    }
}

/// Einen ganzen Block lesen (wie `stdout.read(CHUNK)`); Some(0) am Ende.
fn read_chunk(r: &mut impl Read, buf: &mut [u8]) -> Option<usize> {
    let mut n = 0;
    while n < buf.len() {
        match r.read(&mut buf[n..]) {
            Ok(0) => break,
            Ok(k) => n += k,
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => return None,
        }
    }
    Some(n)
}

impl LevelSource for ParecSource {
    fn level(&self) -> f64 {
        f64::from_bits(self.level.load(Ordering::Relaxed))
    }

    fn ended(&mut self) -> bool {
        match self.proc.as_mut() {
            Some(p) => !matches!(p.try_wait(), Ok(None)),
            None => false,
        }
    }

    fn stop(&mut self) {
        self.stopped.store(true, Ordering::Relaxed);
        if let Some(mut p) = self.proc.take() {
            let _ = p.kill();
            let _ = p.wait();
        }
        self.level.store(0f64.to_bits(), Ordering::Relaxed);
    }
}

impl Drop for ParecSource {
    fn drop(&mut self) {
        self.stop();
    }
}

pub fn parec_available() -> bool {
    which("parec").is_some()
}

type Factory = Box<dyn Fn(&str) -> Option<Box<dyn LevelSource>> + Send>;

/// Verwaltet die Pegel-Quellen: `watch()` legt fest, was gemessen wird, `read()` liefert die Werte.
pub struct LevelMonitor {
    factory: Factory,
    pub available: bool,
    clock: Box<dyn Fn() -> f64 + Send>,
    fall: f64,
    sources: IndexMap<String, (String, Box<dyn LevelSource>)>,
    started: HashMap<String, f64>,
    shown: HashMap<String, f64>,
    last: Option<f64>,
}

fn monotonic() -> f64 {
    static T0: std::sync::OnceLock<Instant> = std::sync::OnceLock::new();
    T0.get_or_init(Instant::now).elapsed().as_secs_f64()
}

impl Default for LevelMonitor {
    fn default() -> Self {
        Self::new()
    }
}

impl LevelMonitor {
    /// Mit parec.
    pub fn new() -> Self {
        Self {
            factory: Box::new(|d| Some(Box::new(ParecSource::new(d)) as Box<dyn LevelSource>)),
            available: parec_available(),
            clock: Box::new(monotonic),
            fall: FALL_PER_S,
            sources: IndexMap::new(),
            started: HashMap::new(),
            shown: HashMap::new(),
            last: None,
        }
    }

    /// Mit eigener Quelle und Uhr (Tests).
    pub fn with(factory: Factory, clock: Box<dyn Fn() -> f64 + Send>, fall_per_s: f64) -> Self {
        Self { factory, available: true, clock, fall: fall_per_s, ..Self::new() }
    }

    /// Wie schnell die Anzeige fällt (Ducking: praktisch sofort).
    pub fn set_fall(&mut self, fall_per_s: f64) {
        self.fall = fall_per_s;
    }

    pub fn running(&self) -> bool {
        !self.sources.is_empty()
    }

    /// targets: {Schlüssel: Gerät}. Startet neue Quellen und beendet nicht mehr benötigte.
    pub fn watch(&mut self, targets: &IndexMap<String, String>) {
        if !self.available {
            return;
        }
        let now = (self.clock)();
        let keys: Vec<String> = self.sources.keys().cloned().collect();
        for key in keys {
            let started = self.started.get(&key).copied().unwrap_or(now);
            let (dev, src) = self.sources.get_mut(&key).unwrap();
            let want = targets.get(&key).filter(|d| !d.is_empty());
            // beendetes parec neu starten – fehlt die Quelle noch, nur alle RESTART_S Sekunden
            if want != Some(dev) || (now - started >= RESTART_S && src.ended()) {
                src.stop();
                self.sources.shift_remove(&key);
                self.started.remove(&key);
                self.shown.remove(&key);
            }
        }
        for (key, dev) in targets {
            if dev.is_empty() || self.sources.contains_key(key) {
                continue;
            }
            if let Some(src) = (self.factory)(dev) {
                self.sources.insert(key.clone(), (dev.clone(), src));
                self.started.insert(key.clone(), now);
            }
        }
    }

    /// {Schlüssel: Pegel 0..1} – steigt sofort, fällt höchstens um fall_per_s pro Sekunde.
    pub fn read(&mut self) -> IndexMap<String, f64> {
        let now = (self.clock)();
        let dt = self.last.map(|l| (now - l).max(0.0)).unwrap_or(0.0);
        self.last = Some(now);
        let mut out = IndexMap::new();
        for (key, (_dev, src)) in &self.sources {
            let raw = src.level().clamp(0.0, 1.0);
            let raw = if raw.is_nan() { 0.0 } else { raw };
            let prev = self.shown.get(key).copied().unwrap_or(0.0);
            let val = if raw >= prev { raw } else { raw.max(prev - self.fall * dt) };
            self.shown.insert(key.clone(), val);
            out.insert(key.clone(), val);
        }
        out
    }

    /// Alle Messungen beenden (Fenster versteckt oder Programm beendet).
    pub fn stop(&mut self) {
        for (_, (_, src)) in self.sources.iter_mut() {
            src.stop();
        }
        self.sources.clear();
        self.started.clear();
        self.shown.clear();
        self.last = None;
    }
}

/// Welche PipeWire-Quellen gemessen werden: je Kanal und je Mix.
/// Schlüssel: Kanal-id und "mix:<Mix-id>" (z. B. "mix:monitor", "mix:stream", "mix:chat").
pub fn channel_sources(mixer: &Mixer, channels: &[Channel]) -> IndexMap<String, String> {
    let mut out = IndexMap::new();
    let mic = mixer.mic_source().unwrap_or_default();
    for c in channels {
        let src = if c.id == "mic" {
            if mic.ends_with(".monitor") { String::new() } else { mic.clone() }
        } else if !c.is_apps() {
            c.source.clone()
        } else {
            Mixer::sink_name(&c.id) + ".monitor"
        };
        if !src.is_empty() {
            out.insert(c.id.clone(), src);
        }
    }
    out.extend(mixer.level_sources());
    out
}

/// Gemeinsam benutzbarer Pegelmesser (GTK-Hauptthread liest, Arbeits-Thread stellt die Quellen ein).
pub type SharedLevels = Arc<Mutex<LevelMonitor>>;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn peak_math() {
        assert_eq!(peak_level(&[]), 0.0);
        assert_eq!(peak_level(&[0, 0, 0, 0]), 0.0);
        let full = i16::MAX.to_le_bytes();
        assert!((peak_level(&full) - 1.0).abs() < 1e-3);
        // −20 dB = 3277 → (−20 + 60) / 60
        let v = 3277i16.to_le_bytes();
        assert!((peak_level(&v) - 40.0 / 60.0).abs() < 1e-3);
        let neg = i16::MIN.to_le_bytes();
        assert!((peak_level(&neg) - 1.0).abs() < 1e-9);
    }

    #[test]
    fn fader_math() {
        assert_eq!(post_fader(0.8, Some(100.0), false), 0.8);
        assert_eq!(post_fader(0.8, Some(100.0), true), 0.0);
        assert_eq!(post_fader(0.8, None, false), 0.0);
        // 50 % = −18 dB → 0.8 − 18/60
        assert!((post_fader(0.8, Some(50.0), false) - (0.8 - std::f64::consts::LOG10_2)).abs() < 1e-9);
        assert_eq!(post_fader(0.1, Some(10.0), false), 0.0);
        assert_eq!(post_fader(0.9, Some(200.0), false), 1.0);
        assert_eq!(level_db(0.0), None);
        assert_eq!(level_db(0.5), Some(-30.0));
    }
}
