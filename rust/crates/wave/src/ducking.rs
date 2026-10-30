//! Auto-Ducking für FreeWave: Kanäle leiser, solange du ins Mikrofon sprichst – wie
//! `freestream/ducking.py`.
//!
//! Ein kleiner Mess-Prozess ([`crate::levels::ParecSource`]) beobachtet das Mikrofon. Liegt der Pegel
//! über der Schwelle, senkt FreeWave die gewählten Kanäle (Standard: Musik) in allen Mixen um den
//! eingestellten Betrag ab – mit weichem Einsatz (Attack), Haltezeit und weichem Ausklingen (Release).
//! Danach steht die Lautstärke wieder exakt auf dem Wert des Nutzers.
//!
//! Mit Reglern kämpft das Ducking nicht: es merkt sich je Kanal/Mix den Wert des Nutzers („base“) und
//! senkt relativ dazu ab. Regler im Fenster, Tasten in FreeDeck und die Kommandozeile setzen während des
//! Absenkens den Nutzerwert ([`Duck::user_set`]); fremde Programme erkennt der Abgleich mit den
//! tatsächlichen Lautstärken.
//!
//! Einstellungen (config.json, "mixer"): `"ducking": {"enabled": false, "channels": ["music"],
//! "amount_db": 12, "threshold_db": -30, "attack_ms": 150, "hold_ms": 800, "release_ms": 900}`

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::JoinHandle;
use std::time::Duration;

use indexmap::IndexMap;
use serde_json::{json, Value};

use crate::levels::{self, LevelMonitor};
use crate::mixer::{duck_volume, py_round, truthy, Duck, Mixer, DUCK_FILE, DUCK_USER_FILE, MAX_VOLUME};
use crate::procs;

pub const AMOUNTS: [i64; 4] = [6, 12, 18, 24];
/// 25 Schritte pro Sekunde
pub const STEP_S: f64 = 0.04;
/// Abgleich mit den tatsächlichen Lautstärken
pub const CHECK_S: f64 = 0.5;
/// Mikrofonquelle neu bestimmen
pub const MIC_S: f64 = 5.0;
/// prüfen, ob der Mixer läuft
pub const ACTIVE_S: f64 = 2.0;

/// Ducking-Einstellungen mit Standardwerten.
#[derive(Debug, Clone, PartialEq)]
pub struct DuckSettings {
    pub enabled: bool,
    pub channels: Vec<String>,
    pub amount_db: f64,
    pub threshold_db: f64,
    pub attack_ms: f64,
    pub hold_ms: f64,
    pub release_ms: f64,
}

impl Default for DuckSettings {
    fn default() -> Self {
        Self { enabled: false, channels: vec!["music".into()], amount_db: 12.0, threshold_db: -30.0,
               attack_ms: 150.0, hold_ms: 800.0, release_ms: 900.0 }
    }
}

/// Einstellungen aus dem Abschnitt "mixer" (fehlende/null-Werte = Standard).
pub fn settings(mixer_conf: &serde_json::Map<String, Value>) -> DuckSettings {
    let mut out = DuckSettings::default();
    let Some(d) = mixer_conf.get("ducking").and_then(Value::as_object) else { return out };
    let f = |k: &str| d.get(k).filter(|v| !v.is_null()).and_then(|v| v.as_f64().or_else(|| v.as_str()?.parse().ok()));
    if let Some(v) = d.get("enabled").filter(|v| !v.is_null()) {
        out.enabled = truthy(v);
    }
    if let Some(v) = d.get("channels").filter(|v| !v.is_null()) {
        out.channels = v.as_array().map(|a| a.iter().filter_map(|x| x.as_str().map(str::to_string)).collect())
            .unwrap_or_default();
    }
    for (k, slot) in [("amount_db", &mut out.amount_db), ("threshold_db", &mut out.threshold_db),
                      ("attack_ms", &mut out.attack_ms), ("hold_ms", &mut out.hold_ms),
                      ("release_ms", &mut out.release_ms)] {
        if let Some(v) = f(k) {
            *slot = v;
        }
    }
    out
}

/// Faktor auf die PipeWire-Lautstärke (Prozent, kubische Kurve) für depth 0..1 der Absenkung.
pub fn factor(amount_db: f64, depth: f64) -> f64 {
    10f64.powf(-amount_db * depth.clamp(0.0, 1.0) / 60.0)
}

/// Hüllkurve: depth 0 (nicht abgesenkt) … 1 (voll abgesenkt).
///
/// `step(level_db, dt)`: level_db ist der Mikrofonpegel (None = Stille). Über der Schwelle steigt depth
/// in `attack` Sekunden auf 1, hält danach `hold` Sekunden und fällt in `release` Sekunden auf 0.
#[derive(Debug, Clone, PartialEq)]
pub struct Envelope {
    pub threshold_db: f64,
    pub attack: f64,
    pub hold: f64,
    pub release: f64,
    pub depth: f64,
    pub hold_left: f64,
}

impl Default for Envelope {
    fn default() -> Self {
        Self::new(-30.0, 0.15, 0.8, 0.9)
    }
}

impl Envelope {
    pub fn new(threshold_db: f64, attack: f64, hold: f64, release: f64) -> Self {
        Self { threshold_db, attack, hold, release, depth: 0.0, hold_left: 0.0 }
    }

    pub fn speaking(&self, level_db: Option<f64>) -> bool {
        level_db.is_some_and(|l| l >= self.threshold_db)
    }

    pub fn step(&mut self, level_db: Option<f64>, dt: f64) -> f64 {
        let dt = dt.max(0.0);
        if self.speaking(level_db) {
            self.hold_left = self.hold;
            self.depth = if self.attack <= 0.0 { 1.0 } else { (self.depth + dt / self.attack).min(1.0) };
        } else if self.hold_left >= dt {
            self.hold_left -= dt; // Pause zwischen Wörtern: abgesenkt bleiben
        } else {
            let rest = dt - self.hold_left;
            self.hold_left = 0.0;
            self.depth = if self.release <= 0.0 { 0.0 } else { (self.depth - rest / self.release).max(0.0) };
        }
        self.depth
    }
}

struct State {
    env: Envelope,
    /// "music_monitor" -> Lautstärke des Nutzers
    bases: IndexMap<String, i64>,
    /// "music_monitor" -> zuletzt geschriebene Lautstärke
    applied: HashMap<String, i64>,
    /// "music_monitor" -> Sink-Input-Index
    index: HashMap<String, String>,
    level: f64,
    factor: f64,
    mic: String,
    mic_at: f64,
    check_at: f64,
    active_at: f64,
    active: bool,
    file_at: f64,
    last: Option<f64>,
    known: HashSet<String>,
    mix_at: f64,
    mixes: Vec<String>,
}

type Clock = Box<dyn Fn() -> f64 + Send + Sync>;
type LevelFn = Box<dyn Fn() -> f64 + Send + Sync>;

/// Senkt Kanäle ab, solange das Mikrofon spricht (läuft als Thread im FreeWave-Host).
pub struct Ducker {
    mixer: Arc<Mixer>,
    st: Mutex<State>,
    level_fn: Option<LevelFn>,
    meter: Mutex<LevelMonitor>,
    clock: Clock,
    running: AtomicBool,
    thread: Mutex<Option<JoinHandle<()>>>,
    wake: (Mutex<bool>, Condvar),
}

fn mono() -> f64 {
    static T0: std::sync::OnceLock<std::time::Instant> = std::sync::OnceLock::new();
    T0.get_or_init(std::time::Instant::now).elapsed().as_secs_f64() + 1e6
}

impl Ducker {
    /// Ducker für einen Mixer; meldet sich dort an (Regler schreiben dann abgesenkt).
    pub fn new(mixer: Arc<Mixer>) -> Arc<Self> {
        Self::with(mixer, None, None, Box::new(mono))
    }

    /// Mit eigener Pegelquelle (`level`: () -> Pegel 0..1), eigenem Messer und eigener Uhr (Tests).
    pub fn with(mixer: Arc<Mixer>, level: Option<LevelFn>, meter: Option<LevelMonitor>, clock: Clock) -> Arc<Self> {
        let mut meter = meter.unwrap_or_default();
        meter.set_fall(1000.0);
        let d = Arc::new(Self {
            mixer: mixer.clone(),
            st: Mutex::new(State {
                env: Envelope::default(),
                bases: IndexMap::new(),
                applied: HashMap::new(),
                index: HashMap::new(),
                level: 0.0,
                factor: 1.0,
                mic: String::new(),
                mic_at: -1e9,
                check_at: -1e9,
                active_at: -1e9,
                active: false,
                file_at: -1e9,
                last: None,
                known: HashSet::new(),
                mix_at: -1e9,
                mixes: Vec::new(),
            }),
            level_fn: level,
            meter: Mutex::new(meter),
            clock,
            running: AtomicBool::new(false),
            thread: Mutex::new(None),
            wake: (Mutex::new(false), Condvar::new()),
        });
        let weak: std::sync::Weak<dyn Duck> = Arc::downgrade(&(d.clone() as Arc<dyn Duck>));
        mixer.set_ducker(Some(weak));
        d
    }

    /// Absenkung 0..1.
    pub fn depth(&self) -> f64 {
        self.st.lock().unwrap().env.depth
    }

    pub fn ducking(&self) -> bool {
        self.depth() > 0.0
    }

    /// Zuletzt gemessener Mikrofonpegel 0..1.
    pub fn level(&self) -> f64 {
        self.st.lock().unwrap().level
    }

    pub fn current_factor(&self) -> f64 {
        self.st.lock().unwrap().factor
    }

    /// Nutzerwerte der abgesenkten Kanäle.
    pub fn bases(&self) -> IndexMap<String, i64> {
        self.st.lock().unwrap().bases.clone()
    }

    // ---------------------------------------------------------- Ablauf
    pub fn start(self: &Arc<Self>) {
        let mut t = self.thread.lock().unwrap();
        if t.is_some() {
            return;
        }
        self.running.store(true, Ordering::SeqCst);
        *self.wake.0.lock().unwrap() = false;
        self.recover();
        let me = self.clone();
        *t = std::thread::Builder::new().name("freewave-ducking".into()).spawn(move || me.run_loop()).ok();
    }

    /// Thread beenden und abgesenkte Kanäle sofort auf den Nutzerwert zurückstellen.
    pub fn stop(&self) {
        self.running.store(false, Ordering::SeqCst);
        {
            let mut w = self.wake.0.lock().unwrap();
            *w = true;
            self.wake.1.notify_all();
        }
        let t = self.thread.lock().unwrap().take();
        if let Some(t) = t {
            if t.thread().id() != std::thread::current().id() {
                let _ = t.join();
            }
        }
        self.meter.lock().unwrap().stop();
        self.restore();
    }

    fn run_loop(&self) {
        while self.running.load(Ordering::SeqCst) {
            self.step(None);
            // Warten mit Wecker statt sleep: endet sofort bei stop()
            let w = self.wake.0.lock().unwrap();
            if *w {
                break;
            }
            let _ = self.wake.1.wait_timeout(w, Duration::from_secs_f64(STEP_S));
        }
    }

    fn read_level(&self, st: &mut State, now: f64) -> f64 {
        if let Some(f) = &self.level_fn {
            return f();
        }
        if now - st.mic_at > MIC_S {
            st.mic_at = now;
            let mic = self.mixer.mic_source().unwrap_or_default();
            st.mic = if mic.ends_with(".monitor") { String::new() } else { mic };
        }
        let mut meter = self.meter.lock().unwrap();
        let mut t = IndexMap::new();
        t.insert("mic".to_string(), st.mic.clone());
        meter.watch(&t);
        meter.read().get("mic").copied().unwrap_or(0.0)
    }

    /// Ein Schritt: Pegel messen, Hüllkurve, Lautstärken schreiben. Gibt depth zurück.
    pub fn step(&self, dt: Option<f64>) -> f64 {
        let now = (self.clock)();
        let conf = self.mixer.conf();
        let s = settings(&conf);
        let on = s.enabled && conf.get("enabled").map(truthy).unwrap_or(false);
        let mut st = self.st.lock().unwrap();
        let dt = dt.unwrap_or_else(|| st.last.map(|l| (now - l).clamp(0.0, 0.5)).unwrap_or(0.0));
        st.last = Some(now);
        st.env.threshold_db = s.threshold_db;
        st.env.attack = (s.attack_ms / 1000.0).max(0.0);
        st.env.hold = (s.hold_ms / 1000.0).max(0.0);
        st.env.release = (s.release_ms / 1000.0).max(0.0);
        if on && now - st.active_at > ACTIVE_S {
            st.active_at = now;
            st.active = self.mixer.is_active();
        }
        if !on || !st.active {
            if self.level_fn.is_none() {
                let mut m = self.meter.lock().unwrap();
                if m.running() {
                    m.stop();
                }
            }
            st.level = 0.0;
            if !st.bases.is_empty() {
                self.restore_locked(&mut st); // ausgeschaltet oder Mixer weg: sofort zurück
            }
            st.env.depth = 0.0;
            st.env.hold_left = 0.0;
            return 0.0;
        }
        st.level = self.read_level(&mut st, now);
        let level = st.level;
        let depth = st.env.step(levels::level_db(level), dt);
        let mixes = self.mix_ids(&mut st, now);
        let keys: HashSet<String> = s.channels.iter().filter(|c| *c != "mic")
            .flat_map(|c| mixes.iter().map(move |m| format!("{c}_{m}"))).collect();
        let took = self.take_user_requests(&mut st);
        let gone: Vec<String> = st.bases.keys().filter(|k| !keys.contains(*k)).cloned().collect();
        for key in gone {
            self.restore_key(&mut st, &key); // Kanal abgewählt: zurückstellen
        }
        if depth <= 0.0 {
            if !st.bases.is_empty() {
                self.restore_locked(&mut st);
            }
            st.factor = 1.0;
            return 0.0;
        }
        st.factor = factor(s.amount_db, depth);
        if !keys.is_subset(&st.known) || now - st.check_at > CHECK_S {
            st.check_at = now;
            st.known = keys.clone();
            self.sync(&mut st, &keys);
        }
        let mut sorted: Vec<&String> = keys.iter().collect();
        sorted.sort();
        for key in sorted {
            if let Some(base) = st.bases.get(key).copied() {
                let v = duck_volume(base, st.factor);
                self.apply(&mut st, key, v);
            }
        }
        // neuer Nutzerwert sofort in ducking.json: FreeDeck rechnet beim nächsten Tastendruck davon aus
        self.write_file(&mut st, took);
        depth
    }

    /// Alle Mixe – aus der Konfiguration, höchstens alle 2 s gelesen.
    fn mix_ids(&self, st: &mut State, now: f64) -> Vec<String> {
        if now - st.mix_at > 2.0 || st.mixes.is_empty() {
            st.mix_at = now;
            st.mixes = self.mixer.mix_ids();
        }
        st.mixes.clone()
    }

    /// Tatsächliche Lautstärken lesen: neue Kanäle übernehmen, fremde Änderungen erkennen.
    fn sync(&self, st: &mut State, keys: &HashSet<String>) {
        let raw = self.mixer.raw_levels();
        for key in keys {
            let Some((lv, idx)) = raw.get(key) else {
                st.bases.shift_remove(key);
                st.applied.remove(key);
                continue;
            };
            st.index.insert(key.clone(), idx.clone());
            if !st.bases.contains_key(key) {
                st.bases.insert(key.clone(), lv.volume);
                st.applied.insert(key.clone(), lv.volume);
            } else if (lv.volume - st.applied.get(key).copied().unwrap_or(lv.volume)).abs() > 1 {
                // jemand anderes hat den Regler bewegt: das ist der neue (abgesenkte) Wert
                let f = if st.factor > 0.0 { st.factor } else { 1.0 };
                st.bases.insert(key.clone(), py_round(lv.volume as f64 / f).clamp(0, MAX_VOLUME));
                st.applied.insert(key.clone(), lv.volume);
            }
        }
    }

    fn apply(&self, st: &mut State, key: &str, vol: i64) {
        if st.applied.get(key) == Some(&vol) {
            return;
        }
        let Some(idx) = st.index.get(key).cloned() else { return };
        match self.mixer.run(&["set-sink-input-volume".to_string(), idx, format!("{vol}%")]) {
            Ok(_) => {
                st.applied.insert(key.to_string(), vol);
            }
            Err(_) => {
                st.index.remove(key);
                st.known.clear(); // Index neu bestimmen (Kanal neu aufgebaut)
            }
        }
    }

    fn restore_key(&self, st: &mut State, key: &str) {
        let base = st.bases.shift_remove(key);
        let applied = st.applied.remove(key);
        let idx = st.index.get(key).cloned();
        let (Some(base), Some(idx)) = (base, idx) else { return };
        if applied == Some(base) {
            return;
        }
        if self.mixer.run(&["set-sink-input-volume".to_string(), idx, format!("{base}%")]).is_err() {
            // Index veraltet: über den Namen setzen (ohne Ducking-Umweg)
            if let Some((cid, mix)) = key.rsplit_once('_') {
                let name = Mixer::loop_name(cid, mix);
                let b = self.mixer.raw_levels().into_iter().find(|(k, _)| format!("linewave_{k}") == name);
                if let Some((_, (_, i))) = b {
                    let _ = self.mixer.run(&["set-sink-input-volume".to_string(), i, format!("{base}%")]);
                }
            }
        }
    }

    fn restore_locked(&self, st: &mut State) {
        self.take_user_requests(st);
        let keys: Vec<String> = st.bases.keys().cloned().collect();
        for key in keys {
            self.restore_key(st, &key);
        }
        st.bases.clear();
        st.applied.clear();
        st.known.clear();
        st.factor = 1.0;
        self.remove_file(st);
    }

    /// Alle abgesenkten Kanäle exakt auf den Wert des Nutzers zurückstellen.
    pub fn restore(&self) {
        let mut st = self.st.lock().unwrap();
        self.restore_locked(&mut st);
    }

    // ------------------------------------------- Zustand für andere Programme
    fn write_file(&self, st: &mut State, force: bool) {
        let now = procs::now();
        if !force && now - st.file_at < 1.0 {
            return;
        }
        st.file_at = now;
        let dir = self.mixer.state_dir();
        let _ = std::fs::create_dir_all(dir);
        let tmp = dir.join(format!("{DUCK_FILE}.tmp"));
        let data = json!({"time": now, "factor": st.factor,
                          "bases": st.bases.iter().map(|(k, v)| (k.clone(), json!(v))).collect::<serde_json::Map<_, _>>(),
                          "applied": st.applied.iter().map(|(k, v)| (k.clone(), json!(v))).collect::<serde_json::Map<_, _>>()});
        if std::fs::write(&tmp, procs::py_dumps(&data)).is_ok() {
            if let Err(e) = std::fs::rename(&tmp, dir.join(DUCK_FILE)) {
                log::debug!("ducking state: {e}");
            }
        }
    }

    fn remove_file(&self, st: &mut State) {
        for name in [DUCK_FILE, DUCK_USER_FILE] {
            let _ = std::fs::remove_file(self.mixer.state_dir().join(name));
        }
        st.file_at = -1e9;
    }

    /// Lautstärken, die FreeDeck/Kommandozeile während des Absenkens gesetzt haben. true, wenn eine
    /// übernommen wurde.
    fn take_user_requests(&self, st: &mut State) -> bool {
        let f = self.mixer.state_dir().join(DUCK_USER_FILE);
        let taken = f.with_file_name(format!("{DUCK_USER_FILE}.host"));
        // erst wegnehmen, dann lesen: was FreeDeck danach schreibt, landet in einer neuen Datei
        if std::fs::rename(&f, &taken).is_err() {
            return false;
        }
        let req = std::fs::read_to_string(&taken).ok().and_then(|t| serde_json::from_str::<Value>(&t).ok());
        let _ = std::fs::remove_file(&taken);
        let Some(Value::Object(req)) = req else { return false }; // kaputte Anfrage verwerfen
        let mut took = false;
        for (key, pct) in req {
            if st.bases.contains_key(&key) {
                let v = pct.as_f64().unwrap_or(0.0) as i64;
                st.bases.insert(key.clone(), v.clamp(0, MAX_VOLUME));
                // FreeDeck hat mit dem Faktor aus ducking.json geschrieben: neu schreiben
                st.applied.remove(&key);
                took = true;
            }
        }
        took
    }

    /// Nach einem Absturz noch abgesenkte Kanäle zurückstellen (alte ducking.json).
    fn recover(&self) {
        let Ok(text) = std::fs::read_to_string(self.mixer.state_dir().join(DUCK_FILE)) else { return };
        let Ok(Value::Object(data)) = serde_json::from_str::<Value>(&text) else { return };
        let raw = self.mixer.raw_levels();
        let applied = data.get("applied").and_then(Value::as_object).cloned().unwrap_or_default();
        let mut st = self.st.lock().unwrap();
        for (key, base) in data.get("bases").and_then(Value::as_object).into_iter().flatten() {
            let Some((lv, idx)) = raw.get(key) else { continue };
            let was = applied.get(key).and_then(Value::as_f64).map(|f| f as i64).unwrap_or(lv.volume);
            if (lv.volume - was).abs() <= 1 {
                st.index.insert(key.clone(), idx.clone());
                st.bases.insert(key.clone(), base.as_f64().unwrap_or(0.0) as i64);
                st.applied.insert(key.clone(), lv.volume);
            }
        }
        self.restore_locked(&mut st);
    }
}

impl Duck for Ducker {
    fn base(&self, key: &str) -> Option<i64> {
        self.st.lock().unwrap().bases.get(key).copied()
    }

    fn volume(&self, pct: i64) -> i64 {
        duck_volume(pct, self.st.lock().unwrap().factor)
    }

    fn user_set(&self, values: &[(String, i64)]) {
        let mut st = self.st.lock().unwrap();
        for (key, pct) in values {
            st.bases.insert(key.clone(), *pct);
            // nächster Schritt schreibt neu – auch wenn er dazwischen noch den alten Wert gesetzt hat
            st.applied.remove(key);
        }
        self.write_file(&mut st, true);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn envelope_attack_hold_release() {
        let mut e = Envelope::new(-30.0, 0.1, 0.2, 0.4);
        assert_eq!(e.step(Some(-10.0), 0.05), 0.5);
        assert_eq!(e.step(Some(-10.0), 0.05), 1.0);
        assert_eq!(e.step(None, 0.1), 1.0); // Haltezeit
        assert_eq!(e.step(Some(-40.0), 0.1), 1.0);
        assert!((e.step(None, 0.2) - 0.5).abs() < 1e-9); // 0.2 s von 0.4 s Release
        assert_eq!(e.step(None, 1.0), 0.0);
    }

    #[test]
    fn factor_math() {
        assert_eq!(factor(12.0, 0.0), 1.0);
        assert!((factor(12.0, 1.0) - 10f64.powf(-0.2)).abs() < 1e-12);
        assert!((factor(60.0, 2.0) - 0.1).abs() < 1e-12); // depth wird begrenzt
        assert_eq!(duck_volume(80, factor(12.0, 1.0)), 50);
    }
}
