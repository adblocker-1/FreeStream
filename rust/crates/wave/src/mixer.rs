//! Wave-Link-Ersatz auf Basis von PipeWire (über pipewire-pulse/pactl) – wie `freestream/mixer.py`.
//!
//! Programme landen in virtuellen Kanälen (System, Musik, Browser, Voice Chat, Spiel, Effekte,
//! Mikrofon). Jeder Kanal hat je Mix eine eigene Lautstärke/Stummschaltung. Mixe:
//!
//!   * Persönlicher Mix („monitor“) – was du über Kopfhörer/Lautsprecher hörst
//!   * Stream-Mix („stream“)       – was OBS/Discord als Quelle „FreeWave Stream-Mix“ bekommt
//!   * eigene Mixe (z. B. „Chat-Mix“ für Discord) – auf Geräte und/oder als virtuelle Quelle
//!
//! Aufbau:
//! ```text
//!   Programm ─► Kanal (Null-Sink „FreeWave <Kanal>“) ┬─► Loopback ─► Kopfhörer      (Persönlicher Mix)
//!                                                     ├─► Loopback ─► Stream-Mix-Sink (Stream-Mix)
//!                                                     └─► Loopback ─► Mix-Sink ─► Ausgänge (eigene Mixe)
//!   Mikrofon ────────────────────────────────────────┬─► Loopback ─► Stream-Mix-Sink
//!                                                     └─► Loopback ─► Kopfhörer (standardmäßig stumm)
//! ```
//!
//! Ein Mix mit genau einem Gerät und ohne virtuelle Quelle geht direkt auf das Gerät; sonst ist er ein
//! Null-Sink, dessen Monitor-Quelle Programme aufnehmen und der über eigene Loopbacks auf seine Geräte
//! geht. Die Kanal-Sinks werden mit monitor.channel-volumes angelegt: ihre Lautstärke ist der
//! Hauptregler des Kanals und wirkt auf alle Mixe.
//!
//! Einstellungen: Abschnitt `"mixer"` in config.json – dieselben Schlüssel wie die Python-Version.
//! Zustand (geladene Module, Lautstärken beim Abbau): `~/.local/state/freestream/mixer.json`.

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock, RwLock, Weak};
use std::time::{Duration, Instant};

use freestream_core::{paths, tr};
use indexmap::IndexMap;
use serde_json::{json, Map, Value};

use crate::pactl::{self, Block, Pactl, SystemPactl};
use crate::procs;
use crate::settings::Settings;
use crate::{Error, Result};

pub const PREFIX: &str = "linewave_";
/// Namen bis Version 0.6
pub const LEGACY_PREFIXES: [&str; 1] = ["lindeck_"];
pub const STREAM_SINK: &str = "linewave_streammix";
pub const MIX_SINK_PREFIX: &str = "linewave_mix_";
/// stiller Sink: hier wartet der Persönliche Mix, während ein anderer abgehört wird
pub const PARK_SINK: &str = "linewave_park";
pub const LISTEN_LOOP: &str = "linewave_listen";
pub const MIC_FILTER: &str = "linewave_micfilter";
pub const RESERVED_IDS: [&str; 11] =
    ["mic", "streammix", "micfilter", "stream", "monitor", "mix", "park", "listen", "both", "selected", "cycle"];
/// feste Mixe; dazu kommen eigene ([`Mixer::mix_ids`])
pub const MIXES: [&str; 2] = ["monitor", "stream"];
/// wie Wave Link: nie über 100 % (keine Übersteuerung)
pub const MAX_VOLUME: i64 = 100;
pub const STATE_FILE: &str = "mixer.json";
pub const DUCK_FILE: &str = "ducking.json";
pub const DUCK_USER_FILE: &str = "ducking-user.json";
pub const LOOP_PROP: &str = "linewave.loop";

/// Eingebaute Zuordnungen Programm → Kanal (Reihenfolge wie in Python).
pub const DEFAULT_ROUTES: &[(&str, &str)] = &[
    ("spotify", "music"), ("rhythmbox", "music"), ("lollypop", "music"), ("elisa", "music"), ("amarok", "music"),
    ("firefox", "browser"), ("chromium", "browser"), ("chrome", "browser"), ("brave", "browser"),
    ("vivaldi", "browser"), ("opera", "browser"), ("librewolf", "browser"),
    ("discord", "voice"), ("vesktop", "voice"), ("teamspeak", "voice"), ("ts3client", "voice"), ("mumble", "voice"),
    ("zoom", "voice"), ("teams", "voice"), ("signal", "voice"), ("element", "voice"),
    ("steam", "game"), ("wine", "game"), ("proton", "game"), ("lutris", "game"), ("heroic", "game"),
    ("minecraft", "game"), ("java", "game"),
];

fn default_routes() -> impl Iterator<Item = (&'static str, &'static str)> {
    DEFAULT_ROUTES.iter().copied()
}

pub fn default_route(key: &str) -> Option<&'static str> {
    default_routes().find(|(k, _)| *k == key).map(|(_, v)| v)
}

/// Standardkanäle (Namen in der aktuellen Sprache).
pub fn default_channels() -> Vec<Value> {
    [("system", "System"), ("music", "Music"), ("browser", "Browser"), ("voice", "Voice Chat"), ("game", "Game"),
     ("sfx", "SFX")]
        .iter()
        .map(|(id, name)| json!({"id": id, "name": tr!(name)}))
        .collect()
}

fn mic_default() -> Map<String, Value> {
    json!({"id": "mic", "name": tr!("Microphone"), "kind": "input"}).as_object().cloned().unwrap_or_default()
}

/// (id, Name, Symbol, Farbe, virtuell) der festen Mixe.
fn builtin_mixes() -> [(&'static str, String, &'static str, &'static str, bool); 2] {
    [
        ("monitor", tr!("Personal Mix"), "headphones", "#22c3ee", false),
        ("stream", tr!("Stream Mix"), "stream", "#c084fc", true),
    ]
}

// ------------------------------------------------------------------ kleine Helfer
/// Wahrheitswert wie in Python (`bool(v)`).
pub fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().map(|f| f != 0.0).unwrap_or(false),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

fn s(v: Option<&Value>) -> String {
    match v {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => n.to_string(),
        _ => String::new(),
    }
}

fn str_list(v: Option<&Value>) -> Vec<String> {
    v.and_then(Value::as_array).map(|a| a.iter().filter_map(|x| x.as_str().map(str::to_string)).collect())
        .unwrap_or_default()
}

fn num(v: &Value) -> Option<f64> {
    match v {
        Value::Number(n) => n.as_f64(),
        Value::Bool(b) => Some(f64::from(u8::from(*b))),
        Value::String(s) => s.trim().parse().ok(),
        _ => None,
    }
}

/// Objekt unter `key` (wird angelegt bzw. ersetzt, wenn es keines ist).
pub(crate) fn obj<'a>(m: &'a mut Map<String, Value>, key: &str) -> &'a mut Map<String, Value> {
    let v = m.entry(key.to_string()).or_insert_with(|| Value::Object(Map::new()));
    if !v.is_object() {
        *v = Value::Object(Map::new());
    }
    v.as_object_mut().unwrap()
}

/// Liste unter `key` (wird angelegt bzw. ersetzt).
pub(crate) fn arr<'a>(m: &'a mut Map<String, Value>, key: &str) -> &'a mut Vec<Value> {
    let v = m.entry(key.to_string()).or_insert_with(|| Value::Array(Vec::new()));
    if !v.is_array() {
        *v = Value::Array(Vec::new());
    }
    v.as_array_mut().unwrap()
}

/// Ersetzt in Python-Formaten wie „{name}“ die Platzhalter.
fn fill(text: &str, args: &[(&str, &str)]) -> String {
    let mut out = text.to_string();
    for (k, v) in args {
        out = out.replace(&format!("{{{k}}}"), v);
    }
    out
}

/// Umlaute für Kennungen umschreiben (wie in Python vor dem Filtern).
fn ascii_base(name: &str) -> String {
    name.to_lowercase().replace('ä', "ae").replace('ö', "oe").replace('ü', "ue").replace('ß', "ss")
}

/// Property-Liste als ein Modulargument, z. B. `sink_properties=` + props(…).
///
/// Werte mit Leerzeichen („FreeWave Musik“) müssen doppelt gequotet sein: außen für das Modulargument,
/// innen für die Property-Liste – sonst schneiden PulseAudio und pipewire-pulse beim ersten Leerzeichen
/// ab. Schlüssel in der fertigen Schreibweise (`node.description`), Reihenfolge wie übergeben.
pub fn props(items: &[(&str, &str)]) -> String {
    let inner: Vec<String> =
        items.iter().map(|(k, v)| format!("{k}='{}'", v.replace(['\'', '"'], ""))).collect();
    format!("\"{}\"", inner.join(" "))
}

/// Gehört ein Name (Sink, Quelle, Loopback) zu FreeWave?
pub fn ours(name: &str) -> bool {
    name.starts_with(PREFIX) || LEGACY_PREFIXES.iter().any(|p| name.starts_with(p))
}

/// Kennung eines FreeWave-Loopbacks („linewave_music_monitor“) oder "".
///
/// Steht in der Property linewave.loop; media.name trägt den lesbaren Namen. Ältere Versionen hatten
/// die Kennung direkt in media.name.
pub fn loop_id(b: &Block) -> String {
    let lid = match b.props.get(LOOP_PROP) {
        Some(v) if !v.is_empty() => v.clone(),
        _ => b.prop("media.name").to_string(),
    };
    if ours(&lid) {
        lid
    } else {
        String::new()
    }
}

fn is_ours_stream(b: &Block) -> bool {
    !loop_id(b).is_empty() || ours(b.prop("media.name"))
}

fn word_re(key: &str) -> regex::Regex {
    static CACHE: OnceLock<Mutex<HashMap<String, regex::Regex>>> = OnceLock::new();
    let mut c = CACHE.get_or_init(Default::default).lock().unwrap();
    c.entry(key.to_string())
        .or_insert_with(|| {
            regex::Regex::new(&format!(r"\b{}", regex::escape(key)))
                .unwrap_or_else(|_| regex::Regex::new("$^").unwrap())
        })
        .clone()
}

/// Passt eine Programm-Regel (z. B. „firefox“) auf die Namen eines Streams? (Wortanfang wie in Python)
pub fn route_matches(key: &str, names: &str) -> bool {
    word_re(key).is_match(names)
}

/// Namen eines Streams für die Zuordnung (application.name, …binary, application.id, node.name).
pub fn stream_names(b: &Block) -> String {
    ["application.name", "application.process.binary", "application.id", "node.name"]
        .iter()
        .map(|k| b.prop(k))
        .filter(|x| !x.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase()
}

/// Abgesenkte Lautstärke (Prozent) zu einem Nutzerwert.
pub fn duck_volume(base: i64, factor: f64) -> i64 {
    let v = py_round(base as f64 * factor);
    v.clamp(0, MAX_VOLUME)
}

/// `round()` wie Python 3 (bei .5 zur geraden Zahl).
pub fn py_round(x: f64) -> i64 {
    let r = x.round();
    if (x - x.trunc()).abs() == 0.5 {
        let t = x.trunc();
        return if (t as i64) % 2 == 0 { t as i64 } else { r as i64 };
    }
    r as i64
}

// ------------------------------------------------------------------ Typen
/// Ein Kanal (Programme oder eigener Eingang, auch das Mikrofon).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Channel {
    pub id: String,
    pub name: String,
    pub icon: String,
    pub color: String,
    /// "apps" (Null-Sink für Programme) oder "input" (Eingangsgerät)
    pub kind: String,
    /// pactl-Quelle bei Eingängen
    pub source: String,
}

impl Channel {
    pub fn from_value(v: &Value) -> Option<Self> {
        let o = v.as_object()?;
        let id = s(o.get("id"));
        if id.is_empty() {
            return None;
        }
        let kind = s(o.get("kind"));
        Some(Self {
            id,
            name: s(o.get("name")),
            icon: s(o.get("icon")),
            color: s(o.get("color")),
            kind: if kind.is_empty() { "apps".into() } else { kind },
            source: s(o.get("source")),
        })
    }
    /// Kanal für Programme (Null-Sink)?
    pub fn is_apps(&self) -> bool {
        self.kind == "apps"
    }
}

/// Ein Mix (Persönlicher Mix, Stream-Mix oder eigener).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Mix {
    pub id: String,
    pub name: String,
    pub icon: String,
    pub color: String,
    /// eigene Quelle („Monitor of FreeWave …“) für OBS/Discord
    pub virtual_: bool,
    /// Ausgabegeräte (leer beim Persönlichen Mix = aktuelles Ausgabegerät)
    pub devices: Vec<String>,
    /// Kanäle, die nicht in diesen Mix gehen
    pub exclude: Vec<String>,
    pub builtin: bool,
}

/// Lautstärke und Stummschaltung.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Level {
    pub volume: i64,
    pub mute: bool,
}

/// Lautstärken je „<Kanal>_<Mix>“ (Reihenfolge wie von PipeWire gemeldet).
pub type Levels = IndexMap<String, Level>;

/// Wiedergabe-Stream eines Programms.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Stream {
    pub index: String,
    /// Kanal-id oder None (nicht im Mixer)
    pub channel: Option<String>,
    pub sink: String,
    pub app: String,
    pub key: String,
    pub names: String,
}

/// Ausgabe- oder Eingabegerät.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Device {
    pub name: String,
    pub description: String,
}

/// Programm, das einem Kanal zugeordnet ist.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct RouteEntry {
    pub key: String,
    pub name: String,
    pub own: bool,
}

/// Gespeicherte Mixer-Szene: Lautstärke/stumm je Kanal und Mix.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Scene {
    pub id: String,
    pub name: String,
    pub levels: IndexMap<String, IndexMap<String, (f64, bool)>>,
}

impl Scene {
    fn from_value(v: &Value) -> Option<Self> {
        let o = v.as_object()?;
        let id = s(o.get("id"));
        if id.is_empty() {
            return None;
        }
        let mut levels = IndexMap::new();
        for (cid, mixes) in o.get("levels").and_then(Value::as_object).into_iter().flatten() {
            let mut per = IndexMap::new();
            for (mix, val) in mixes.as_object().into_iter().flatten() {
                if let Some(a) = val.as_array().filter(|a| a.len() >= 2) {
                    per.insert(mix.clone(), (num(&a[0]).unwrap_or(0.0), truthy(&a[1])));
                }
            }
            levels.insert(cid.clone(), per);
        }
        Some(Self { id, name: s(o.get("name")), levels })
    }
}

// ------------------------------------------------------------------ Ducking-Schnittstelle
/// Laufendes Auto-Ducking aus Sicht des Mixers (im selben Prozess: [`crate::ducking::Ducker`],
/// sonst über die Zustandsdatei: [`DuckFile`]).
pub trait Duck: Send + Sync {
    /// Nutzerwert eines Kanals/Mixes ("music_monitor"), solange abgesenkt wird.
    fn base(&self, key: &str) -> Option<i64>;
    /// Abgesenkte Lautstärke zu einem Nutzerwert.
    fn volume(&self, pct: i64) -> i64;
    /// Nutzerwerte merken (schon abgesenkt geschrieben).
    fn user_set(&self, values: &[(String, i64)]);
}

/// Auto-Ducking des FreeWave-Hosts aus Sicht anderer Programme (FreeDeck, Kommandozeile).
///
/// Der Host schreibt während des Absenkens STATE_DIR/ducking.json: {"time", "factor", "bases"}.
/// Lautstärken, die hier gesetzt werden, gelten als Nutzerwert und landen in ducking-user.json.
pub struct DuckFile {
    data: Mutex<Map<String, Value>>,
    dir: PathBuf,
}

static DUCK_LOCK: Mutex<()> = Mutex::new(());

impl DuckFile {
    pub const MAX_AGE: f64 = 3.0;

    pub fn load(dir: &std::path::Path) -> Option<Self> {
        let text = std::fs::read_to_string(dir.join(DUCK_FILE)).ok()?;
        let data = serde_json::from_str::<Value>(&text).ok()?.as_object()?.clone();
        let t = data.get("time").and_then(num).unwrap_or(0.0);
        if (procs::now() - t).abs() > Self::MAX_AGE {
            return None;
        }
        Some(Self { data: Mutex::new(data), dir: dir.to_path_buf() })
    }
}

impl Duck for DuckFile {
    fn base(&self, key: &str) -> Option<i64> {
        let d = self.data.lock().unwrap();
        d.get("bases").and_then(|b| b.get(key)).filter(|v| !v.is_null()).and_then(num).map(|f| f as i64)
    }

    fn volume(&self, pct: i64) -> i64 {
        let f = self.data.lock().unwrap().get("factor").and_then(num).filter(|f| *f != 0.0).unwrap_or(1.0);
        duck_volume(pct, f)
    }

    fn user_set(&self, values: &[(String, i64)]) {
        {
            let mut d = self.data.lock().unwrap();
            let b = obj(&mut d, "bases");
            for (k, v) in values {
                b.insert(k.clone(), json!(v));
            }
        }
        let f = self.dir.join(DUCK_USER_FILE);
        let _g = DUCK_LOCK.lock().unwrap(); // sonst überschreibt ein Schreib-Thread den Wert des anderen
        let mut req = procs::read_json(&f);
        for (k, v) in values {
            req.insert(k.clone(), json!(v));
        }
        let tmp = f.with_file_name(format!("{DUCK_USER_FILE}.{}.tmp", std::process::id()));
        if std::fs::write(&tmp, procs::py_dumps(&Value::Object(req))).is_ok() {
            let _ = std::fs::rename(&tmp, &f); // der Host liest nie eine halb geschriebene Datei
        }
    }
}

// ------------------------------------------------------------------ zuletzt gelesene Loopbacks
/// Zuletzt gelesene Loopbacks (Name -> Block): Tasten am Deck schreiben damit sofort, ohne erst
/// „pactl list“ abzuwarten. Nur kurz gültig; jede neue Abfrage frischt sie auf.
pub const RECENT_S: f64 = 2.0;
/// ändern nur Werte, nicht die Indizes
const KEEP_RECENT: [&str; 2] = ["set-sink-input-volume", "set-sink-input-mute"];
/// so lange gilt ein selbst geschriebener Wert mehr als eine Abfrage von PipeWire
pub const WRITTEN_S: f64 = 1.5;

#[derive(Default)]
struct Recent {
    at: Option<Instant>,
    loops: HashMap<String, Block>,
}

#[derive(Clone, Copy)]
struct Written {
    volume: Option<i64>,
    mute: Option<bool>,
    at: Instant,
}

static RECENT: Mutex<Option<Recent>> = Mutex::new(None);
static WRITTEN: Mutex<Option<HashMap<String, Written>>> = Mutex::new(None);
/// Tastendrücke (Halten = Wiederholung) lesen-rechnen-schreiben nacheinander
static CHANGE_LOCK: Mutex<()> = Mutex::new(());

fn recent_reset() {
    if let Some(r) = RECENT.lock().unwrap().as_mut() {
        r.at = None;
    }
}

fn wrote(index: &str, volume: Option<i64>, mute: Option<bool>) {
    let mut w = WRITTEN.lock().unwrap();
    let w = w.get_or_insert_with(HashMap::new);
    let e = w.entry(index.to_string()).or_insert(Written { volume: None, mute: None, at: Instant::now() });
    if volume.is_some() {
        e.volume = volume;
    }
    if mute.is_some() {
        e.mute = mute;
    }
    e.at = Instant::now();
}

fn forget_written(index: &str) {
    if let Some(w) = WRITTEN.lock().unwrap().as_mut() {
        w.remove(index);
    }
}

/// pactl set-sink-input-volume|-mute <Index> <Wert>: geschriebenen Wert merken (relative Angaben und
/// „toggle“ kennen wir nicht – dann gilt wieder, was PipeWire meldet).
fn note_write(args: &[String]) {
    let (cmd, index, value) = (args[0].as_str(), args[1].as_str(), args[2].as_str());
    if cmd == "set-sink-input-volume" && value.ends_with('%') && !value.starts_with(['+', '-']) {
        match value[..value.len() - 1].parse::<i64>() {
            Ok(v) => wrote(index, Some(v), None),
            Err(_) => forget_written(index),
        }
    } else if cmd == "set-sink-input-mute" && (value == "0" || value == "1") {
        wrote(index, None, Some(value == "1"));
    } else {
        forget_written(index);
    }
}

/// Eine Abfrage kann vor unserem letzten Schreiben gestellt worden sein und erst danach ankommen: dann
/// den eben geschriebenen Wert behalten. `asked_at`: wann die Abfrage gestellt wurde – war das nach dem
/// Schreiben, stimmt sie sicher und der gemerkte Wert ist erledigt.
fn apply_written(blocks: &mut [Block], asked_at: Option<Instant>) {
    let mut w = WRITTEN.lock().unwrap();
    let Some(w) = w.as_mut() else { return };
    if w.is_empty() {
        return;
    }
    let now = Instant::now();
    w.retain(|_, e| {
        now.duration_since(e.at).as_secs_f64() <= WRITTEN_S && asked_at.map(|a| e.at > a).unwrap_or(true)
    });
    for b in blocks {
        if let Some(e) = w.get(&b.index) {
            if let Some(v) = e.volume {
                b.volume = Some(v);
            }
            if let Some(m) = e.mute {
                b.mute = m;
            }
        }
    }
}

fn remember_loops(blocks: &[Block], asked_at: Option<Instant>) {
    watch_loops(); // sobald Deck-Tasten Lautstärken lesen: Änderungen anderer Programme mitbekommen
    let mut blocks = blocks.to_vec();
    apply_written(&mut blocks, asked_at);
    let mut loops = HashMap::new();
    for b in blocks {
        let lid = loop_id(&b);
        if !lid.is_empty() {
            loops.insert(lid, b);
        }
    }
    *RECENT.lock().unwrap() = Some(Recent { at: Some(Instant::now()), loops });
}

fn recent_loops() -> Option<HashMap<String, Block>> {
    watch_loops();
    let r = RECENT.lock().unwrap();
    let r = r.as_ref()?;
    let at = r.at?;
    (at.elapsed().as_secs_f64() < RECENT_S).then(|| r.loops.clone())
}

fn recent_update(lid: &str, volume: Option<i64>, mute: Option<bool>) {
    if let Some(r) = RECENT.lock().unwrap().as_mut() {
        if let Some(b) = r.loops.get_mut(lid) {
            if volume.is_some() {
                b.volume = volume;
            }
            if let Some(m) = mute {
                b.mute = m;
            }
        }
    }
}

/// Tests schalten das Mithören ab (eigener pactl-Prozess).
pub static WATCH: AtomicBool = AtomicBool::new(true);
static WATCH_WANTED: AtomicBool = AtomicBool::new(false);
static WATCH_RUNNING: AtomicBool = AtomicBool::new(false);

/// FreeDeck: Deck-Tasten zeigen Lautstärken – Änderungen anderer Programme mitbekommen.
pub fn want_watch() {
    WATCH_WANTED.store(true, Ordering::SeqCst);
}

/// Ändert ein anderes Programm (FreeWave-Fenster, pavucontrol) eine Lautstärke, die gemerkten Werte der
/// Deck-Tasten sofort neu einlesen. Läuft erst, wenn Deck-Tasten den Zwischenspeicher benutzen.
fn watch_loops() {
    if !WATCH.load(Ordering::SeqCst) || !WATCH_WANTED.load(Ordering::SeqCst) {
        return;
    }
    if WATCH_RUNNING.swap(true, Ordering::SeqCst) {
        return;
    }
    let _ = std::thread::Builder::new().name("freewave-loops-watch".into()).spawn(|| {
        let watch = pactl::PulseWatch::new();
        watch.start();
        let mut seen = watch.version();
        while WATCH.load(Ordering::SeqCst) {
            let v = watch.wait(seen, Duration::from_secs(30));
            if v == seen {
                if !watch.running() {
                    break; // pactl subscribe geht nicht: dann gilt der Sekundentakt
                }
                continue;
            }
            std::thread::sleep(Duration::from_millis(40)); // mehrere Meldungen zusammenfassen
            seen = watch.version();
            let asked = Instant::now();
            let blocks = SystemPactl.blocks("sink-inputs");
            remember_loops(&blocks, Some(asked));
        }
        watch.stop();
        WATCH_RUNNING.store(false, Ordering::SeqCst);
    });
}

// ------------------------------------------------------------------ Kontext beim Aufbau
#[derive(Debug, Clone, Default)]
struct Ctx {
    monitor: String,
    mic: String,
    devices: HashMap<String, Vec<String>>,
}

/// Ergebnis von `_load_channel`.
struct Loaded {
    sink: Option<String>,
    loops: IndexMap<String, String>,
    src: Option<String>,
}

// ------------------------------------------------------------------ Mixer
/// Der Mixer. Alle Methoden nehmen `&self` (intern synchronisiert): ein `Arc<Mixer>` kann vom
/// GTK-Hauptthread, von Arbeits-Threads und vom Ducking-Thread gleichzeitig benutzt werden. Methoden
/// rufen pactl auf und können dauern – im GTK-Hauptthread nur über [`crate::control::Controller`].
pub struct Mixer {
    cfg: Mutex<Box<dyn Settings>>,
    pactl: Arc<dyn Pactl>,
    state_dir: PathBuf,
    moved: Mutex<HashSet<String>>,
    ducker: RwLock<Option<Weak<dyn Duck>>>,
    sleep_fn: fn(f64),
}

fn real_sleep(s: f64) {
    std::thread::sleep(Duration::from_secs_f64(s));
}

/// Kurze Pause (Tests ersetzen sie).
fn no_sleep(_s: f64) {}

impl Mixer {
    /// Mixer mit eigenen Einstellungen, pactl und Zustandsordner (Tests, Werkzeuge).
    pub fn new(cfg: Box<dyn Settings>, pactl: Arc<dyn Pactl>, state_dir: PathBuf) -> Self {
        Self {
            cfg: Mutex::new(cfg),
            pactl,
            state_dir,
            moved: Mutex::new(HashSet::new()),
            ducker: RwLock::new(None),
            sleep_fn: real_sleep,
        }
    }

    /// Mixer mit config.json, echtem pactl und `~/.local/state/freestream`.
    pub fn system() -> Self {
        Self::new(Box::new(freestream_core::config::Config::load()), Arc::new(SystemPactl), paths::state_dir())
    }

    /// Pausen zwischen pactl-Aufrufen weglassen (Tests).
    pub fn without_sleep(mut self) -> Self {
        self.sleep_fn = no_sleep;
        self
    }

    pub(crate) fn sleep(&self, s: f64) {
        (self.sleep_fn)(s)
    }

    pub fn state_dir(&self) -> &std::path::Path {
        &self.state_dir
    }

    pub fn pactl(&self) -> &dyn Pactl {
        &*self.pactl
    }

    pub fn set_ducker(&self, d: Option<Weak<dyn Duck>>) {
        *self.ducker.write().unwrap() = d;
    }

    // ----------------------------------------------------------- pactl
    /// pactl-Befehl (wie `Mixer.run`): neu angelegte/verschobene Loopbacks machen die gemerkten
    /// Indizes ungültig; eigene Lautstärken/Stummschaltungen werden gemerkt.
    pub fn run<S: AsRef<str>>(&self, args: &[S]) -> Result<String> {
        let args: Vec<String> = args.iter().map(|a| a.as_ref().to_string()).collect();
        let first = args.first().map(String::as_str).unwrap_or("");
        if !first.is_empty() && !pactl::READ_ONLY.contains(&first) && !KEEP_RECENT.contains(&first) {
            recent_reset();
        }
        let out = pactl::cached_run(&*self.pactl, &args)?;
        if KEEP_RECENT.contains(&first) && args.len() >= 3 {
            note_write(&args);
        }
        Ok(out)
    }

    pub(crate) fn blocks(&self, what: &str) -> Arc<Vec<Block>> {
        pactl::blocks(&*self.pactl, what)
    }

    // ----------------------------------------------------------- Konfiguration
    /// Abschnitt "mixer" aus config.json (Kopie).
    pub fn conf(&self) -> Map<String, Value> {
        self.cfg.lock().unwrap().data().get("mixer").and_then(Value::as_object).cloned().unwrap_or_default()
    }

    /// Ganze Einstellungen lesen.
    pub fn with_settings<R>(&self, f: impl FnOnce(&dyn Settings) -> R) -> R {
        f(&**self.cfg.lock().unwrap())
    }

    /// Einstellungen ändern (ohne zu speichern).
    pub fn with_settings_mut<R>(&self, f: impl FnOnce(&mut dyn Settings) -> R) -> R {
        f(&mut **self.cfg.lock().unwrap())
    }

    /// Abschnitt "mixer" ändern (wird angelegt) und speichern.
    pub(crate) fn edit_conf<R>(&self, f: impl FnOnce(&mut Map<String, Value>) -> R) -> R {
        let mut cfg = self.cfg.lock().unwrap();
        let r = f(obj(cfg.data_mut(), "mixer"));
        if let Err(e) = cfg.save() {
            log::warn!("config.json: {e}");
        }
        r
    }

    /// Einstellungen speichern.
    pub fn save_cfg(&self) {
        if let Err(e) = self.cfg.lock().unwrap().save() {
            log::warn!("config.json: {e}");
        }
    }

    /// Ist der Mixer in den Einstellungen eingeschaltet?
    pub fn enabled(&self) -> bool {
        self.conf().get("enabled").map(truthy).unwrap_or(false)
    }

    /// Alle Kanäle außer dem Mikrofon (Programme und eigene Eingänge).
    pub fn channels(&self) -> Vec<Channel> {
        let conf = self.conf();
        let chans = match conf.get("channels") {
            Some(v) if truthy(v) => v.as_array().cloned().unwrap_or_default(),
            _ => default_channels(),
        };
        chans.iter().filter_map(Channel::from_value).filter(|c| c.id != "mic").collect()
    }

    /// Kanäle, in die Programme gelegt werden können (Null-Sinks).
    pub fn app_channels(&self) -> Vec<Channel> {
        self.channels().into_iter().filter(Channel::is_apps).collect()
    }

    pub fn all_channels(&self) -> Vec<Channel> {
        let mut mic = mic_default();
        if let Some(mc) = self.conf().get("mic_channel").and_then(Value::as_object) {
            for (k, v) in mc {
                mic.insert(k.clone(), v.clone());
            }
        }
        let mut out = self.channels();
        if let Some(c) = Channel::from_value(&Value::Object(mic)) {
            out.push(c);
        }
        out
    }

    /// Kanal nach id oder Name (ohne Groß-/Kleinschreibung).
    pub fn channel(&self, cid: &str) -> Option<Channel> {
        let cid = cid.to_lowercase();
        self.all_channels().into_iter().find(|c| c.id.to_lowercase() == cid || c.name.to_lowercase() == cid)
    }

    pub fn sink_name(cid: &str) -> String {
        format!("{PREFIX}{cid}")
    }

    pub fn loop_name(cid: &str, mix: &str) -> String {
        format!("{PREFIX}{cid}_{mix}")
    }

    /// Kanalliste in der Konfiguration (wird mit den Standardkanälen angelegt).
    fn cfg_channels(mx: &mut Map<String, Value>) -> &mut Vec<Value> {
        if !mx.get("channels").map(truthy).unwrap_or(false) {
            mx.insert("channels".into(), Value::Array(default_channels()));
        }
        arr(mx, "channels")
    }

    pub fn new_id(&self, name: &str) -> String {
        let base: String = ascii_base(name).chars().filter(|c| c.is_ascii_lowercase() || c.is_ascii_digit()).take(16)
            .collect();
        let base = if base.is_empty() { "kanal".to_string() } else { base };
        let mut taken: HashSet<String> = self.all_channels().into_iter().map(|c| c.id).collect();
        taken.extend(RESERVED_IDS.iter().map(|s| s.to_string()));
        let (mut cid, mut n) = (base.clone(), 2);
        while taken.contains(&cid) {
            cid = format!("{base}{n}");
            n += 1;
        }
        cid
    }

    /// Echte Eingangsgeräte (Mikrofone, Line-In, Capture-Karten).
    pub fn inputs(&self) -> Vec<Device> {
        self.blocks("sources")
            .iter()
            .filter(|b| !b.name().is_empty() && !b.name().ends_with(".monitor") && !ours(b.name()))
            .map(|b| Device {
                name: b.name().to_string(),
                description: if b.description().is_empty() { b.name() } else { b.description() }.to_string(),
            })
            .collect()
    }

    /// Eigenen Kanal anlegen; läuft der Mixer, wird er sofort eingerichtet.
    /// `in_mixes = false`: der Kanal ist zunächst in keinem Mix.
    pub fn add_channel(&self, name: &str, kind: &str, source: &str, icon: &str, color: &str, in_mixes: bool)
                       -> Result<Channel> {
        let name = name.trim();
        if name.is_empty() {
            return Err(Error(tr!("Please enter a name")));
        }
        if kind != "apps" && kind != "input" {
            return Err(Error(tr!("Unknown kind: {kind}", kind = kind)));
        }
        if kind == "input" && source.is_empty() {
            return Err(Error(tr!("Please choose an input device")));
        }
        let id = self.new_id(name);
        let mut c = Map::new();
        c.insert("id".into(), json!(id));
        c.insert("name".into(), json!(name));
        c.insert("kind".into(), json!(kind));
        if kind == "input" {
            c.insert("source".into(), json!(source));
        }
        if !icon.is_empty() {
            c.insert("icon".into(), json!(icon));
        }
        if !color.is_empty() {
            c.insert("color".into(), json!(color));
        }
        let mixes = self.mixes();
        self.edit_conf(|mx| {
            Self::cfg_channels(mx).push(Value::Object(c.clone()));
            if !in_mixes {
                for m in &mixes {
                    let mut ex: Vec<Value> = m.exclude.iter().filter(|x| **x != id).map(|x| json!(x)).collect();
                    ex.push(json!(id));
                    Self::cfg_mix(mx, &m.id).insert("exclude".into(), Value::Array(ex));
                }
            }
        });
        let ch = Channel::from_value(&Value::Object(c)).unwrap_or_default();
        if self.is_active() {
            self.add_live(&ch, None)?;
        }
        Ok(ch)
    }

    /// Name, Symbol, Farbe oder Eingangsgerät eines Kanals ändern (None = unverändert).
    pub fn update_channel(&self, cid: &str, name: Option<&str>, icon: Option<&str>, color: Option<&str>,
                          source: Option<&str>) -> Result<Channel> {
        if cid == "mic" {
            self.edit_conf(|mx| {
                if let Some(src) = source {
                    mx.insert("mic".into(), json!(src));
                }
                let mc = obj(mx, "mic_channel");
                if let Some(i) = icon {
                    mc.insert("icon".into(), json!(i));
                }
                if let Some(c) = color {
                    mc.insert("color".into(), json!(c));
                }
            });
            if self.is_active() {
                self.rebuild_channel("mic")?;
            }
            return self.channel("mic").ok_or_else(|| Error(tr!("Channel {id} not found", id = cid)));
        }
        let cur = self.conf().get("channels").and_then(Value::as_array).and_then(|a| {
            a.iter().find(|c| c.get("id").and_then(Value::as_str) == Some(cid)).cloned()
        });
        let cur = match cur {
            Some(c) => c,
            None if default_channels().iter().any(|c| c["id"] == cid) => {
                default_channels().into_iter().find(|c| c["id"] == cid).unwrap()
            }
            None => return Err(Error(tr!("Channel {id} not found", id = cid))),
        };
        if name.is_some_and(|n| n.trim().is_empty()) {
            return Err(Error(tr!("Please enter a name")));
        }
        let differs = |k: &str, v: Option<&str>| v.is_some_and(|v| cur.get(k).and_then(Value::as_str) != Some(v));
        let rebuild = differs("name", name) || differs("source", source);
        let out = self.edit_conf(|mx| {
            let chans = Self::cfg_channels(mx);
            let c = chans.iter_mut().find(|c| c.get("id").and_then(Value::as_str) == Some(cid))?;
            let o = c.as_object_mut()?;
            if let Some(n) = name {
                o.insert("name".into(), json!(n.trim()));
            }
            for (k, v) in [("icon", icon), ("color", color), ("source", source)] {
                if let Some(v) = v {
                    o.insert(k.into(), json!(v));
                }
            }
            Channel::from_value(c)
        });
        let out = out.ok_or_else(|| Error(tr!("Channel {id} not found", id = cid)))?;
        if rebuild && self.is_active() {
            self.rebuild_channel(cid)?;
        }
        Ok(out)
    }

    /// Kanal löschen. Programme darin landen wieder beim Standardgerät.
    pub fn remove_channel(&self, cid: &str) -> Result<()> {
        let chans = self.channels();
        let Some(c) = chans.iter().find(|c| c.id == cid) else {
            return Err(Error(tr!("Channel {id} not found", id = cid)));
        };
        if c.is_apps() && self.app_channels().len() <= 1 {
            return Err(Error(tr!("At least one channel for apps must remain")));
        }
        self.edit_conf(|mx| {
            Self::cfg_channels(mx).retain(|x| x.get("id").and_then(Value::as_str) != Some(cid));
            let routes: Map<String, Value> = mx.get("routes").and_then(Value::as_object).cloned().unwrap_or_default()
                .into_iter().filter(|(_, v)| v.as_str() != Some(cid)).collect();
            mx.insert("routes".into(), Value::Object(routes));
            if let Some(fx) = mx.get_mut("effects").and_then(Value::as_object_mut) {
                fx.remove(cid);
            }
        });
        if self.is_active() {
            self.unload_channel(cid);
        }
        self.fx().stop(cid);
        Ok(())
    }

    /// Programm → Kanal: eingebaute Zuordnungen, darüber die eigenen ("" = abgeschaltet).
    pub fn routes(&self) -> IndexMap<String, String> {
        let mut r: IndexMap<String, String> =
            default_routes().map(|(k, v)| (k.to_string(), v.to_string())).collect();
        for (k, v) in self.conf().get("routes").and_then(Value::as_object).into_iter().flatten() {
            r.insert(k.to_lowercase(), s(Some(v)));
        }
        r.retain(|_, v| !v.is_empty());
        r
    }

    /// Programme, die ein Kanal bekommt. Eigene Zuordnungen immer, die eingebauten nur für Programme aus
    /// `known` (Namen installierter oder gerade spielender Programme).
    pub fn channel_routes(&self, cid: &str, known: &[String]) -> Vec<RouteEntry> {
        let conf = self.conf();
        let own: HashSet<String> = conf.get("routes").and_then(Value::as_object).into_iter().flatten()
            .map(|(k, _)| k.to_lowercase()).collect();
        let names: HashMap<String, String> = conf.get("route_names").and_then(Value::as_object).into_iter()
            .flatten().map(|(k, v)| (k.to_lowercase(), s(Some(v)))).collect();
        let known = known.iter().filter(|k| !k.is_empty()).map(|k| k.to_lowercase()).collect::<Vec<_>>().join(" ");
        let mut out: Vec<RouteEntry> = self
            .routes()
            .into_iter()
            .filter(|(key, target)| target == cid && (own.contains(key) || route_matches(key, &known)))
            .map(|(key, _)| RouteEntry {
                name: names.get(&key).filter(|n| !n.is_empty()).cloned().unwrap_or_else(|| title(&key)),
                own: own.contains(&key),
                key,
            })
            .collect();
        out.sort_by_key(|e| e.name.to_lowercase());
        out
    }

    /// Programme einem Kanal zuordnen (`add`: (Schlüssel, Name)) oder wieder lösen (`remove`). Laufende
    /// Streams der neuen Programme wandern sofort in den Kanal. Gibt die Zahl verschobener zurück.
    pub fn set_channel_routes(&self, cid: &str, add: &[(String, String)], remove: &[String]) -> Result<usize> {
        if !self.app_channels().iter().any(|c| c.id == cid) {
            return Err(Error(tr!("Unknown channel: {id}", id = cid)));
        }
        let mut moved = 0;
        let mut keys = Vec::new();
        {
            let mut cfg = self.cfg.lock().unwrap();
            let mx = obj(cfg.data_mut(), "mixer");
            obj(mx, "routes");
            obj(mx, "route_names");
            for key in remove {
                let key = key.to_lowercase();
                let cur = mx["routes"].get(&key).and_then(Value::as_str).map(str::to_string);
                if default_route(&key) == Some(cid) {
                    obj(mx, "routes").insert(key, json!("")); // eingebaute Zuordnung abschalten
                } else if cur.as_deref() == Some(cid) {
                    obj(mx, "routes").remove(&key);
                    obj(mx, "route_names").remove(&key);
                }
            }
            for (key, name) in add {
                let key = key.to_lowercase().trim().to_string();
                if key.is_empty() {
                    continue;
                }
                obj(mx, "routes").insert(key.clone(), json!(cid));
                if !name.is_empty() {
                    obj(mx, "route_names").insert(key.clone(), json!(name));
                }
                keys.push(key);
            }
        }
        for key in keys {
            for b in pactl::app_streams(&*self.pactl, &key) {
                match self.run(&["move-sink-input", &b.index, &Self::sink_name(cid)]) {
                    Ok(_) => moved += 1,
                    Err(e) => log::debug!("move {key}: {e}"),
                }
            }
        }
        self.save_cfg();
        Ok(moved)
    }

    // ----------------------------------------------------------- Mixe
    /// Alle Mixe in Reihenfolge: Persönlicher Mix, Stream-Mix, eigene.
    pub fn mixes(&self) -> Vec<Mix> {
        let conf_all = self.conf();
        let mut conf: IndexMap<String, Map<String, Value>> = IndexMap::new();
        for m in conf_all.get("mixes").and_then(Value::as_array).into_iter().flatten() {
            if let Some(o) = m.as_object() {
                let id = s(o.get("id"));
                if !id.is_empty() {
                    conf.insert(id, o.clone());
                }
            }
        }
        let mut out = Vec::new();
        let empty = Map::new();
        for (mid, name, icon, color, virt) in builtin_mixes() {
            let c = conf.get(mid).unwrap_or(&empty);
            let mut m = Mix {
                id: mid.into(),
                name,
                icon: icon.into(),
                color: color.into(),
                virtual_: virt,
                devices: Vec::new(),
                exclude: Vec::new(),
                builtin: true,
            };
            let set = |k: &str| c.get(k).filter(|v| !v.is_null() && v.as_str() != Some(""));
            if let Some(v) = set("name") {
                m.name = s(Some(v));
            }
            if let Some(v) = set("icon") {
                m.icon = s(Some(v));
            }
            if let Some(v) = set("color") {
                m.color = s(Some(v));
            }
            if let Some(v) = set("devices") {
                m.devices = str_list(Some(v));
            }
            if let Some(v) = set("exclude") {
                m.exclude = str_list(Some(v));
            }
            m.virtual_ = mid == "stream" || c.get("virtual").map(truthy).unwrap_or(virt);
            out.push(m);
        }
        for (mid, c) in &conf {
            if MIXES.contains(&mid.as_str()) {
                continue;
            }
            let or = |k: &str, d: &str| {
                let v = s(c.get(k));
                if v.is_empty() {
                    d.to_string()
                } else {
                    v
                }
            };
            out.push(Mix {
                id: mid.clone(),
                name: or("name", mid),
                icon: or("icon", "wave"),
                color: or("color", "#22c55e"),
                virtual_: c.get("virtual").map(truthy).unwrap_or(false),
                devices: str_list(c.get("devices")),
                exclude: str_list(c.get("exclude")),
                builtin: false,
            });
        }
        out
    }

    /// Mix nach id oder Name (ohne Groß-/Kleinschreibung).
    pub fn mix(&self, mid: &str) -> Option<Mix> {
        let mid = mid.to_lowercase();
        self.mixes().into_iter().find(|m| m.id == mid || m.name.to_lowercase() == mid)
    }

    pub fn mix_ids(&self) -> Vec<String> {
        self.mixes().into_iter().map(|m| m.id).collect()
    }

    pub fn in_mix(&self, cid: &str, mid: &str) -> bool {
        self.mix(mid).is_some_and(|m| !m.exclude.iter().any(|x| x == cid))
    }

    /// Mixe, in die ein Kanal geht.
    pub fn channel_mixes(&self, cid: &str) -> Vec<String> {
        self.mixes().into_iter().filter(|m| !m.exclude.iter().any(|x| x == cid)).map(|m| m.id).collect()
    }

    /// Regler des Kanals gekoppelt (wie das Schloss in Wave Link): alle Mixe bewegen sich gemeinsam.
    pub fn is_linked(&self, cid: &str) -> bool {
        str_list(self.conf().get("linked")).iter().any(|c| c == cid)
    }

    pub fn set_linked(&self, cid: &str, on: bool) {
        self.edit_conf(|mx| {
            let mut linked: Vec<Value> =
                str_list(mx.get("linked")).into_iter().filter(|c| c != cid).map(Value::String).collect();
            if on {
                linked.push(json!(cid));
            }
            mx.insert("linked".into(), Value::Array(linked));
        });
    }

    /// Eintrag eines Mixes in der Konfiguration (wird angelegt).
    fn cfg_mix<'a>(mx: &'a mut Map<String, Value>, mid: &str) -> &'a mut Map<String, Value> {
        let lst = arr(mx, "mixes");
        let pos = lst.iter().position(|x| x.get("id").and_then(Value::as_str) == Some(mid));
        let pos = match pos {
            Some(p) => p,
            None => {
                lst.push(json!({"id": mid}));
                lst.len() - 1
            }
        };
        if !lst[pos].is_object() {
            lst[pos] = json!({"id": mid});
        }
        lst[pos].as_object_mut().unwrap()
    }

    pub fn new_mix_id(&self, name: &str) -> String {
        let base: String = ascii_base(name).chars().filter(|c| c.is_ascii_lowercase()).take(16).collect();
        let base = if base.is_empty() { "mix".to_string() } else { base };
        let mut taken: HashSet<String> = self.mix_ids().into_iter().collect();
        taken.extend(RESERVED_IDS.iter().map(|s| s.to_string()));
        let (mut mid, mut n) = (base.clone(), 2u32);
        while taken.contains(&mid) {
            // nur Buchstaben: „chatb“, „chatc“ …
            mid = format!("{base}{}", char::from_u32(96 + n).unwrap_or('z'));
            n += 1;
        }
        mid
    }

    fn check_outputs(devices: &[String], virt: bool) -> Result<()> {
        if devices.is_empty() && !virt {
            return Err(Error(tr!("Choose at least one output")));
        }
        Ok(())
    }

    /// Eigenen Mix anlegen; läuft der Mixer, wird er sofort eingerichtet.
    pub fn add_mix(&self, name: &str, icon: &str, color: &str, devices: &[String], virt: bool, exclude: &[String])
                   -> Result<Mix> {
        let name = name.trim();
        if name.is_empty() {
            return Err(Error(tr!("Please enter a name")));
        }
        let devices: Vec<String> = devices.iter().filter(|d| !d.is_empty()).cloned().collect();
        Self::check_outputs(&devices, virt)?;
        let mid = self.new_mix_id(name);
        self.edit_conf(|mx| {
            let m = Self::cfg_mix(mx, &mid);
            m.insert("name".into(), json!(name));
            m.insert("icon".into(), json!(if icon.is_empty() { "wave" } else { icon }));
            m.insert("color".into(), json!(if color.is_empty() { "#22c55e" } else { color }));
            m.insert("devices".into(), json!(devices));
            m.insert("virtual".into(), json!(virt));
            m.insert("exclude".into(), json!(exclude));
        });
        if self.is_active() {
            if let Some(m) = self.mix(&mid) {
                self.add_mix_live(&m)?;
            }
        }
        self.mix(&mid).ok_or_else(|| Error(tr!("Mix {id} not found", id = mid)))
    }

    /// Name, Symbol, Farbe, Geräte oder virtuelle Quelle eines Mixes ändern (None = unverändert).
    pub fn update_mix(&self, mid: &str, name: Option<&str>, icon: Option<&str>, color: Option<&str>,
                      devices: Option<&[String]>, virt: Option<bool>) -> Result<Mix> {
        let Some(cur) = self.mix(mid) else {
            return Err(Error(tr!("Mix {id} not found", id = mid)));
        };
        if name.is_some_and(|n| n.trim().is_empty()) {
            return Err(Error(tr!("Please enter a name")));
        }
        let devices: Vec<String> =
            devices.unwrap_or(&cur.devices).iter().filter(|d| !d.is_empty()).cloned().collect();
        let virt = if cur.id == "stream" { true } else { virt.unwrap_or(cur.virtual_) };
        if cur.id != "monitor" {
            // der Persönliche Mix nimmt ohne Gerät das Standardgerät
            Self::check_outputs(&devices, virt)?;
        }
        let mut rebuild = devices != cur.devices || virt != cur.virtual_;
        self.edit_conf(|mx| {
            let m = Self::cfg_mix(mx, &cur.id);
            for (k, v) in [("name", name.map(str::trim)), ("icon", icon), ("color", color)] {
                if let Some(v) = v.filter(|v| !v.is_empty()) {
                    m.insert(k.into(), json!(v));
                }
            }
            m.insert("devices".into(), json!(devices));
            m.insert("virtual".into(), json!(virt));
        });
        if cur.id == "monitor" && !devices.is_empty() {
            // erstes Gerät = Abhörgerät
            let mut st = self.state();
            if st.get("monitor_output").and_then(Value::as_str) != Some(devices[0].as_str()) {
                st.insert("monitor_output".into(), json!(devices[0]));
                self.save_state(&st);
                rebuild = true;
            }
        }
        if rebuild && self.is_active() {
            self.rebuild_mixes()?;
        }
        self.mix(&cur.id).ok_or_else(|| Error(tr!("Mix {id} not found", id = mid)))
    }

    pub fn remove_mix(&self, mid: &str) -> Result<()> {
        let Some(m) = self.mix(mid) else {
            return Err(Error(tr!("Mix {id} not found", id = mid)));
        };
        if m.builtin {
            return Err(Error(tr!("The Personal Mix and the Stream Mix cannot be deleted")));
        }
        self.edit_conf(|mx| arr(mx, "mixes").retain(|x| x.get("id").and_then(Value::as_str) != Some(&m.id)));
        if self.is_active() {
            self.remove_mix_live(&m.id);
        }
        Ok(())
    }

    /// Kanal in einen Mix aufnehmen (on) oder herausnehmen – sofort, ohne Neuaufbau.
    pub fn set_member(&self, cid: &str, mid: &str, on: bool) -> Result<()> {
        let (Some(m), Some(c)) = (self.mix(mid), self.channel(cid)) else {
            return Err(Error(tr!("Channel {id}/{mix} not found", id = cid, mix = mid)));
        };
        self.edit_conf(|mx| {
            let mut excl: Vec<Value> = m.exclude.iter().filter(|x| **x != c.id).map(|x| json!(x)).collect();
            if !on {
                excl.push(json!(c.id));
            }
            Self::cfg_mix(mx, &m.id).insert("exclude".into(), Value::Array(excl));
        });
        if !self.is_active() {
            return Ok(());
        }
        let mut st = self.state();
        let has = obj(obj(&mut st, "loops"), &c.id).contains_key(&m.id);
        if on && !has {
            let src = st.get("sources").and_then(|s| s.get(&c.id)).and_then(Value::as_str).map(str::to_string);
            let mix = self.mix(&m.id).unwrap_or(m.clone());
            let ctx = self.ctx(&st)?;
            let target = self.mix_target(&mix, &ctx);
            if let (Some(src), Some(target)) = (src.filter(|s| !s.is_empty()), target) {
                let module = self.load_loop(&c, &mix, &src, &target)?;
                obj(obj(&mut st, "loops"), &c.id).insert(m.id.clone(), json!(module));
                arr(obj(&mut st, "channels"), &c.id).push(json!(module));
                arr(&mut st, "modules").push(json!(module));
                self.save_state(&st);
                if c.id == "mic" && m.id == "monitor" {
                    self.sleep(0.2);
                    // eigene Stimme nicht ungewollt auf dem Kopfhörer
                    self.set_mute("mic", "monitor", Mute::Set(true), false)?;
                }
            }
        } else if !on && has {
            let module = obj(obj(&mut st, "loops"), &c.id).shift_remove(&m.id).unwrap_or(Value::Null);
            let _ = self.run(&["unload-module", &s(Some(&module))]);
            arr(obj(&mut st, "channels"), &c.id).retain(|x| *x != module);
            arr(&mut st, "modules").retain(|x| *x != module);
            self.save_state(&st);
        }
        Ok(())
    }

    // ----------------------------------------------------------- Zustand
    pub fn state_file(&self) -> PathBuf {
        self.state_dir.join(STATE_FILE)
    }

    /// Inhalt von mixer.json.
    pub fn state(&self) -> Map<String, Value> {
        procs::read_json(&self.state_file())
    }

    pub(crate) fn save_state(&self, st: &Map<String, Value>) {
        let f = self.state_file();
        let _ = std::fs::create_dir_all(&self.state_dir);
        // erst in eine Nachbardatei, dann umbenennen: wer gleichzeitig liest, sieht nie eine halbe Datei
        let tmp = f.with_file_name(format!("{STATE_FILE}.{}.{:?}.tmp", std::process::id(),
                                           std::thread::current().id()).replace(['(', ')'], ""));
        if std::fs::write(&tmp, procs::py_dumps_indent(&Value::Object(st.clone()))).is_ok() {
            if let Err(e) = std::fs::rename(&tmp, &f) {
                log::warn!("mixer.json: {e}");
            }
        }
    }

    /// Ist der Mixer eingerichtet (gibt es den Stream-Mix-Sink)?
    pub fn is_active(&self) -> bool {
        self.blocks("sinks").iter().any(|b| b.name() == STREAM_SINK)
    }

    pub fn default_sink(&self) -> Result<String> {
        self.run(&["get-default-sink"])
    }

    pub fn default_source(&self) -> Result<String> {
        self.run(&["get-default-source"])
    }

    // ----------------------------------------------------------- Auf- und Abbau
    /// Kanäle und Mixe anlegen (vorhandene FreeWave-Module werden vorher entfernt).
    pub fn setup(&self) -> Result<Vec<String>> {
        if !self.pactl.available() {
            return Err(Error(tr!(
                "pactl is missing (package pulseaudio-utils) – PipeWire with pipewire-pulse is required"
            )));
        }
        let old = self.state();
        self.teardown(false);
        let mut prev_default = s(old.get("previous_default"));
        if prev_default.is_empty() {
            prev_default = self.default_sink()?;
        }
        if ours(&prev_default) {
            prev_default = self.default_sink()?;
        }
        let conf = self.conf();
        let mut monitor_out = s(conf.get("monitor_output"));
        if monitor_out.is_empty() {
            monitor_out = prev_default.clone();
        }
        let has_filter = self.blocks("sources").iter().any(|b| b.name() == MIC_FILTER);
        let mut mic = s(conf.get("mic"));
        if mic.is_empty() {
            mic = if has_filter { MIC_FILTER.to_string() } else { self.default_source()? };
        }
        let mut ctx = Ctx { monitor: monitor_out.clone(), mic: mic.clone(), devices: HashMap::new() };
        let mut st = Map::new();
        st.insert("modules".into(), json!([]));
        st.insert("channels".into(), json!({}));
        st.insert("loops".into(), json!({}));
        st.insert("sources".into(), json!({}));
        st.insert("mixes".into(), json!({}));
        st.insert("previous_default".into(), json!(prev_default));
        st.insert("monitor_output".into(), json!(monitor_out));
        st.insert("mic".into(), json!(mic));
        st.insert("created".into(), json!(procs::now()));
        st.insert("listen".into(), json!("monitor"));
        for m in self.mixes() {
            let mods = self.load_mix(&m, &mut ctx)?;
            obj(&mut st, "mixes").insert(m.id.clone(), json!(mods));
            arr(&mut st, "modules").extend(mods.iter().map(|x| json!(x)));
        }
        for c in self.all_channels() {
            let res = self.load_channel(&c, &ctx)?;
            Self::record_channel(&mut st, &c, &res);
        }
        if let Some(first) = self.app_channels().first() {
            if let Err(e) = self.run(&["set-default-sink", &Self::sink_name(&first.id)]) {
                // z. B. ohne Session-Manager (WirePlumber)
                log::warn!("{}", tr!("Default output not set to FreeWave {name}: {error}", name = first.name,
                                     error = e));
            }
        }
        self.save_state(&st);
        self.sleep(0.3);
        // eigenes Mikrofon standardmäßig nicht auf den Kopfhörer
        let _ = self.set_mute("mic", "monitor", Mute::Set(true), false);
        self.restore_levels(&levels_from_value(old.get("levels")));
        self.restore_masters(&levels_from_value(old.get("masters")));
        self.moved.lock().unwrap().clear();
        log::info!("{}", tr!("Audio mixer set up: {count} channels, monitor → {output}",
                             count = self.channels().len(), output = monitor_out));
        Ok(str_list(st.get("modules")))
    }

    fn restore_levels(&self, levels: &Levels) {
        for (key, lv) in levels {
            let Some((cid, mix)) = key.rsplit_once('_') else { continue };
            if self.set_volume(cid, mix, lv.volume).is_ok() {
                let _ = self.set_mute(cid, mix, Mute::Set(lv.mute), false);
            }
        }
    }

    fn restore_masters(&self, masters: &Levels) {
        for (cid, lv) in masters {
            if self.set_master(cid, lv.volume).is_ok() {
                let _ = self.set_master_mute(cid, Mute::Set(lv.mute));
            }
        }
    }

    fn loop_props(&self, cid: &str, name: &str, mix: &str) -> String {
        let mname = self.mix(mix).map(|m| m.name).unwrap_or_else(|| mix.to_string());
        let label = tr!("FreeWave {name} → {mix}", name = name, mix = mname);
        props(&[(LOOP_PROP, &Self::loop_name(cid, mix)), ("media.name", &label), ("node.description", &label)])
    }

    /// Quelle, aus der ein Kanal in die Mixe geht (ggf. über Rauschunterdrückung und Effekte).
    fn source_of(&self, c: &Channel, ctx: &Ctx) -> Option<String> {
        let src = if c.id == "mic" {
            ctx.mic.clone()
        } else if !c.is_apps() {
            c.source.clone()
        } else {
            Self::sink_name(&c.id) + ".monitor"
        };
        if src.is_empty() || (c.id == "mic" && src.ends_with(".monitor")) {
            if c.id == "mic" {
                self.noise().stop();
            }
            return None;
        }
        let src = if c.id == "mic" { self.noise().source_for(&src) } else { src };
        Some(self.fx().source_for(c, &src))
    }

    // ----------------------------------------------------------- Mix-Aufbau
    /// Geräte, auf die ein Mix geht (Persönlicher Mix ohne eigene Angabe: das Monitor-Ausgabegerät).
    pub fn mix_devices(&self, m: &Mix, st: Option<&Map<String, Value>>) -> Vec<String> {
        if !m.devices.is_empty() {
            return m.devices.clone();
        }
        if m.id == "monitor" {
            let own;
            let st = match st {
                Some(st) => st,
                None => {
                    own = self.state();
                    &own
                }
            };
            let mut dev = s(st.get("monitor_output"));
            if dev.is_empty() {
                dev = s(self.conf().get("monitor_output"));
            }
            if dev.is_empty() {
                dev = self.default_sink().unwrap_or_default();
            }
            return if dev.is_empty() { Vec::new() } else { vec![dev] };
        }
        Vec::new()
    }

    /// Null-Sink eines Mixes (auch wenn er gerade direkt auf ein Gerät geht).
    pub fn mix_sink(m: &Mix) -> String {
        if m.id == "stream" {
            STREAM_SINK.to_string()
        } else {
            format!("{MIX_SINK_PREFIX}{}", m.id)
        }
    }

    fn direct(m: &Mix, devices: &[String]) -> bool {
        !m.virtual_ && devices.len() == 1
    }

    /// Wohin die Kanäle eines Mixes spielen: direkt auf das Gerät oder in den Mix-Sink.
    fn mix_target(&self, m: &Mix, ctx: &Ctx) -> Option<String> {
        let devices = match ctx.devices.get(&m.id) {
            Some(d) => d.clone(),
            None if m.id == "monitor" && m.devices.is_empty() => vec![ctx.monitor.clone()],
            None => m.devices.clone(),
        };
        if Self::direct(m, &devices) {
            return Some(devices[0].clone());
        }
        if devices.is_empty() && !m.virtual_ {
            return None;
        }
        Some(Self::mix_sink(m))
    }

    /// Null-Sink eines Mixes und je Ausgabegerät ein Loopback (entfällt, wenn der Mix direkt aufs Gerät geht).
    fn load_mix(&self, m: &Mix, ctx: &mut Ctx) -> Result<Vec<String>> {
        let devices =
            if m.id == "monitor" && m.devices.is_empty() { vec![ctx.monitor.clone()] } else { m.devices.clone() };
        ctx.devices.insert(m.id.clone(), devices.clone());
        if Self::direct(m, &devices) || (devices.is_empty() && !m.virtual_) {
            return Ok(Vec::new());
        }
        let sink = Self::mix_sink(m);
        let desc = if m.id == "stream" { tr!("FreeWave Stream Mix") } else { format!("FreeWave {}", m.name) };
        let mut mods = vec![self.run(&[
            "load-module".to_string(),
            "module-null-sink".into(),
            format!("sink_name={sink}"),
            format!("sink_properties={}", props(&[("device.description", &desc), ("node.description", &desc)])),
        ])?];
        for (i, dev) in devices.iter().enumerate() {
            let lid = format!("{PREFIX}mixout-{}-{i}", m.id);
            let label = format!("{desc} → {dev}");
            mods.push(self.run(&[
                "load-module".to_string(),
                "module-loopback".into(),
                format!("source={sink}.monitor"),
                format!("sink={dev}"),
                "latency_msec=20".into(),
                "source_dont_move=true".into(),
                format!("sink_input_properties={}",
                        props(&[(LOOP_PROP, &lid), ("media.name", &label), ("node.description", &label)])),
            ])?);
        }
        Ok(mods)
    }

    fn load_loop(&self, c: &Channel, m: &Mix, src: &str, target: &str) -> Result<String> {
        let mut args = vec![
            "load-module".to_string(),
            "module-loopback".into(),
            format!("source={src}"),
            format!("sink={target}"),
            "latency_msec=20".into(),
        ];
        if src.ends_with(".monitor") {
            args.push("source_dont_move=true".into());
        }
        args.push(format!("sink_input_properties={}", self.loop_props(&c.id, &c.name, &m.id)));
        self.run(&args)
    }

    /// Null-Sink (bei Programm-Kanälen) und je Mix ein Loopback.
    fn load_channel(&self, c: &Channel, ctx: &Ctx) -> Result<Loaded> {
        let mut out = Loaded { sink: None, loops: IndexMap::new(), src: None };
        if c.is_apps() && c.id != "mic" {
            let desc = format!("FreeWave {}", c.name);
            // monitor.channel-volumes: die Lautstärke des Kanals (Hauptregler) wirkt auch auf die Mixe
            out.sink = Some(self.run(&[
                "load-module".to_string(),
                "module-null-sink".into(),
                format!("sink_name={}", Self::sink_name(&c.id)),
                format!("sink_properties={}", props(&[("device.description", &desc), ("node.description", &desc),
                                                       ("monitor.channel-volumes", "true")])),
            ])?);
        }
        let src = self.source_of(c, ctx);
        out.src = src.clone();
        if let Some(src) = src {
            for m in self.mixes() {
                if m.exclude.contains(&c.id) {
                    continue;
                }
                if let Some(target) = self.mix_target(&m, ctx) {
                    let module = self.load_loop(c, &m, &src, &target)?;
                    out.loops.insert(m.id.clone(), module);
                }
            }
        }
        Ok(out)
    }

    fn record_channel(st: &mut Map<String, Value>, c: &Channel, res: &Loaded) {
        let mods: Vec<String> = res.sink.iter().cloned().chain(res.loops.values().cloned()).collect();
        obj(st, "channels").insert(c.id.clone(), json!(mods));
        let loops: Map<String, Value> = res.loops.iter().map(|(k, v)| (k.clone(), json!(v))).collect();
        obj(st, "loops").insert(c.id.clone(), Value::Object(loops));
        obj(st, "sources").insert(c.id.clone(), res.src.as_ref().map(|s| json!(s)).unwrap_or(Value::Null));
        arr(st, "modules").extend(mods.iter().map(|m| json!(m)));
    }

    fn ctx(&self, st: &Map<String, Value>) -> Result<Ctx> {
        let mut mic = s(self.conf().get("mic"));
        if mic.is_empty() {
            mic = s(st.get("mic"));
        }
        if mic.is_empty() {
            let has_filter = self.blocks("sources").iter().any(|b| b.name() == MIC_FILTER);
            mic = if has_filter { MIC_FILTER.to_string() } else { self.default_source()? };
        }
        let mut monitor = s(st.get("monitor_output"));
        if monitor.is_empty() {
            monitor = self.default_sink()?;
        }
        Ok(Ctx { monitor, mic, devices: HashMap::new() })
    }

    /// Quelle des Mikrofons, wie der Mixer sie gerade benutzt (Pegelanzeige, Ducking).
    pub fn mic_source(&self) -> Result<String> {
        Ok(self.ctx(&self.state())?.mic)
    }

    fn add_live(&self, c: &Channel, levels: Option<&IndexMap<String, Level>>) -> Result<()> {
        let mut st = self.state();
        if !st.contains_key("loops") {
            // Zustand einer älteren Version: alles neu aufbauen
            self.setup()?;
            return Ok(());
        }
        let ctx = self.ctx(&st)?;
        let res = self.load_channel(c, &ctx)?;
        Self::record_channel(&mut st, c, &res);
        self.save_state(&st);
        self.sleep(0.2);
        for (mix, lv) in levels.into_iter().flatten() {
            if self.set_volume(&c.id, mix, lv.volume).is_ok() {
                let _ = self.set_mute(&c.id, mix, Mute::Set(lv.mute), false);
            }
        }
        if c.id == "mic" && levels.map(|l| l.is_empty()).unwrap_or(true) {
            let _ = self.set_mute("mic", "monitor", Mute::Set(true), false);
        }
        Ok(())
    }

    /// Module eines Kanals entladen; gibt seine Lautstärken zurück.
    fn unload_channel(&self, cid: &str) -> IndexMap<String, Level> {
        let mix_ids = self.mix_ids();
        let mut levels = IndexMap::new();
        for (key, v) in self.levels() {
            for mix in &mix_ids {
                if key == format!("{cid}_{mix}") {
                    levels.insert(mix.clone(), v);
                }
            }
        }
        let mut st = self.state();
        let mods = st.get_mut("channels").and_then(Value::as_object_mut).and_then(|c| c.shift_remove(cid));
        if let Some(l) = st.get_mut("loops").and_then(Value::as_object_mut) {
            l.shift_remove(cid);
        }
        if let Some(l) = st.get_mut("sources").and_then(Value::as_object_mut) {
            l.shift_remove(cid);
        }
        let mods: Vec<Value> = match mods {
            Some(Value::Array(a)) => a,
            Some(_) => Vec::new(),
            None => {
                // ältere Zustandsdatei ohne Zuordnung: Module über ihre Namen suchen
                let mut found = Vec::new();
                for b in self.blocks("modules").iter() {
                    let a = b.argument.clone().unwrap_or_default();
                    if (format!("{a} ").contains(&format!("sink_name={} ", Self::sink_name(cid)))
                        || mix_ids.iter().any(|m| a.contains(&Self::loop_name(cid, m))))
                        && !b.index.is_empty()
                    {
                        found.push(json!(b.index));
                    }
                }
                found
            }
        };
        for m in mods.iter().rev() {
            let _ = self.run(&["unload-module", &s(Some(m))]);
        }
        arr(&mut st, "modules").retain(|m| !mods.contains(m));
        self.save_state(&st);
        levels
    }

    /// Kanal neu einrichten (z. B. nach Umbenennen oder geänderten Effekten); Lautstärken bleiben.
    pub fn rebuild_channel(&self, cid: &str) -> Result<()> {
        let Some(c) = self.channel(cid) else {
            return Err(Error(tr!("Channel {id} not found", id = cid)));
        };
        let masters: Levels = self.masters().into_iter().filter(|(k, _)| *k == c.id).collect();
        // Programme im Kanal: mit dem Null-Sink verschwindet ihr Ziel – danach selbst zurückholen
        let mut apps: Vec<String> = if c.is_apps() {
            self.streams().into_iter().filter(|s| s.channel.as_deref() == Some(&c.id)).map(|s| s.index).collect()
        } else {
            Vec::new()
        };
        let levels = self.unload_channel(&c.id);
        self.fx().stop(&c.id);
        self.add_live(&c, Some(&levels))?;
        self.restore_masters(&masters);
        for attempt in 0..2 {
            for idx in &apps {
                if let Err(e) = self.run(&["move-sink-input", idx, &Self::sink_name(&c.id)]) {
                    log::debug!("move {idx}: {e}"); // Stream inzwischen beendet
                }
            }
            if apps.is_empty() || attempt > 0 {
                break;
            }
            // PipeWire verliert bei mehreren Verschiebungen kurz nacheinander manchmal eine – einmal nachsehen
            self.sleep(0.3);
            apps = self.streams().into_iter()
                .filter(|s| apps.contains(&s.index) && s.channel.as_deref() != Some(&c.id))
                .map(|s| s.index).collect();
        }
        Ok(())
    }

    /// Nur den neuen Mix einrichten; alle anderen Verbindungen laufen ungestört weiter.
    fn add_mix_live(&self, m: &Mix) -> Result<()> {
        let mut st = self.state();
        if !st.contains_key("loops") {
            return self.rebuild_mixes();
        }
        let mut ctx = self.ctx(&st)?;
        let mods = self.load_mix(m, &mut ctx)?;
        obj(&mut st, "mixes").insert(m.id.clone(), json!(mods));
        arr(&mut st, "modules").extend(mods.iter().map(|x| json!(x)));
        let target = self.mix_target(m, &ctx);
        for c in self.all_channels() {
            let src = st.get("sources").and_then(|s| s.get(&c.id)).and_then(Value::as_str).unwrap_or("").to_string();
            let Some(target) = target.as_ref() else { continue };
            if src.is_empty() || m.exclude.contains(&c.id) {
                continue;
            }
            let module = self.load_loop(&c, m, &src, target)?;
            obj(obj(&mut st, "loops"), &c.id).insert(m.id.clone(), json!(module));
            arr(obj(&mut st, "channels"), &c.id).push(json!(module));
            arr(&mut st, "modules").push(json!(module));
        }
        self.save_state(&st);
        Ok(())
    }

    /// Nur die Verbindungen des gelöschten Mixes entfernen.
    fn remove_mix_live(&self, mid: &str) {
        let mut st = self.state();
        if !st.contains_key("loops") {
            let _ = self.rebuild_mixes();
            return;
        }
        if st.get("listen").and_then(Value::as_str) == Some(mid) {
            self.stop_listen(&mut st);
        }
        let mut gone: Vec<Value> = Vec::new();
        let cids: Vec<String> = obj(&mut st, "loops").keys().cloned().collect();
        for cid in cids {
            let module = obj(obj(&mut st, "loops"), &cid).shift_remove(mid);
            if let Some(module) = module {
                arr(obj(&mut st, "channels"), &cid).retain(|x| *x != module);
                gone.push(module);
            }
        }
        if let Some(Value::Array(mods)) = obj(&mut st, "mixes").shift_remove(mid) {
            gone.extend(mods.into_iter().rev());
        }
        for module in &gone {
            let _ = self.run(&["unload-module", &s(Some(module))]);
        }
        arr(&mut st, "modules").retain(|x| !gone.contains(x));
        self.save_state(&st);
    }

    /// Mixe neu aufbauen (neuer/gelöschter Mix, andere Geräte). Kanal-Sinks und Programme bleiben,
    /// Lautstärken werden übernommen.
    pub fn rebuild_mixes(&self) -> Result<()> {
        let mut st = self.state();
        if !st.contains_key("loops") {
            // Zustand einer älteren Version
            self.setup()?;
            return Ok(());
        }
        let levels = self.levels();
        self.stop_listen(&mut st);
        let mut gone: Vec<Value> = Vec::new();
        let cids: Vec<String> = obj(&mut st, "loops").keys().cloned().collect();
        for cid in &cids {
            let loops: Vec<Value> = obj(obj(&mut st, "loops"), cid).values().cloned().collect();
            gone.extend(loops.iter().cloned());
            arr(obj(&mut st, "channels"), cid).retain(|x| !loops.contains(x));
            obj(&mut st, "loops").insert(cid.clone(), json!({}));
        }
        let mixes_st: Vec<Value> = obj(&mut st, "mixes").values().cloned().collect();
        for mods in mixes_st {
            if let Value::Array(a) = mods {
                gone.extend(a.into_iter().rev());
            }
        }
        for module in &gone {
            let _ = self.run(&["unload-module", &s(Some(module))]);
        }
        arr(&mut st, "modules").retain(|x| !gone.contains(x));
        st.insert("mixes".into(), json!({}));
        let mut ctx = self.ctx(&st)?;
        for m in self.mixes() {
            let mods = self.load_mix(&m, &mut ctx)?;
            obj(&mut st, "mixes").insert(m.id.clone(), json!(mods));
            arr(&mut st, "modules").extend(mods.iter().map(|x| json!(x)));
        }
        for c in self.all_channels() {
            let src = st.get("sources").and_then(|s| s.get(&c.id)).and_then(Value::as_str).unwrap_or("").to_string();
            if src.is_empty() {
                continue;
            }
            for m in self.mixes() {
                let target = self.mix_target(&m, &ctx);
                let Some(target) = target.filter(|_| !m.exclude.contains(&c.id)) else { continue };
                let module = self.load_loop(&c, &m, &src, &target)?;
                obj(obj(&mut st, "loops"), &c.id).insert(m.id.clone(), json!(module));
                arr(obj(&mut st, "channels"), &c.id).push(json!(module));
                arr(&mut st, "modules").push(json!(module));
            }
        }
        self.save_state(&st);
        self.sleep(0.2);
        if !levels.contains_key("mic_monitor") {
            let _ = self.set_mute("mic", "monitor", Mute::Set(true), false);
        }
        self.restore_levels(&levels);
        Ok(())
    }

    /// Alles entfernen (Kanäle, Mixe, Abhören, Effekte, Rauschunterdrückung); Lautstärken bleiben für das
    /// nächste Einrichten in mixer.json. `restore_default`: vorheriges Standardgerät wieder einstellen.
    pub fn teardown(&self, restore_default: bool) {
        let mut st = self.state();
        if st.get("modules").map(truthy).unwrap_or(false) {
            st.insert("levels".into(), levels_to_value(&self.levels()));
            st.insert("masters".into(), levels_to_value(&self.masters()));
        }
        let mods: Vec<Value> = st.get("modules").and_then(Value::as_array).cloned().unwrap_or_default();
        for m in mods.iter().rev() {
            let _ = self.run(&["unload-module", &s(Some(m))]);
        }
        // Reste früherer Läufe (z. B. nach Absturz) und Abhören/Parken
        for b in self.blocks("modules").iter() {
            let args = format!("{} {}", b.props.values().cloned().collect::<Vec<_>>().join(" "),
                               b.argument.clone().unwrap_or_default());
            let hit = args.contains(PREFIX) || LEGACY_PREFIXES.iter().any(|p| args.contains(p));
            if hit && !b.index.is_empty() {
                let _ = self.run(&["unload-module", &b.index]);
            }
        }
        if restore_default {
            let prev = s(st.get("previous_default"));
            if !prev.is_empty() {
                let _ = self.run(&["set-default-sink", &prev]);
            }
        }
        st.insert("modules".into(), json!([]));
        st.insert("channels".into(), json!({}));
        for k in ["loops", "sources", "mixes", "listen_mods", "parked"] {
            st.shift_remove(k);
        }
        st.insert("listen".into(), json!("monitor"));
        self.save_state(&st);
        self.fx().stop_all();
        self.noise().stop();
    }

    // ----------------------------------------------------------- Lautstärke/Mute
    fn loop_input(&self, cid: &str, mix: &str) -> Option<Block> {
        let name = Self::loop_name(cid, mix);
        let blocks = self.blocks("sink-inputs");
        remember_loops(&blocks, None);
        blocks.iter().find(|b| loop_id(b) == name).cloned()
    }

    /// Laufendes Auto-Ducking (im selben Prozess oder über dessen Zustandsdatei) oder None.
    pub fn duck(&self) -> Option<Arc<dyn Duck>> {
        if let Some(d) = self.ducker.read().unwrap().as_ref().and_then(Weak::upgrade) {
            return Some(d);
        }
        DuckFile::load(&self.state_dir).map(|d| Arc::new(d) as Arc<dyn Duck>)
    }

    /// (Lautstärke, stumm). Während Auto-Ducking gilt die Lautstärke des Nutzers.
    pub fn get(&self, cid: &str, mix: &str) -> (Option<i64>, bool) {
        let Some(b) = self.loop_input(cid, mix) else { return (None, false) };
        let base = self.duck().and_then(|d| d.base(&format!("{cid}_{mix}")));
        (Some(base.unwrap_or(b.vol())), b.mute)
    }

    /// (Lautstärke, stumm) aus der zuletzt gelesenen Abfrage samt eigener Schreibvorgänge – ohne
    /// PipeWire zu fragen; None, wenn nichts Frisches da ist. Für die sofortige Anzeige am Deck.
    pub fn recent_level(&self, cid: &str, mix: &str) -> Option<(i64, bool)> {
        let known = recent_loops()?;
        let mixes = self.mixes_of(cid, mix).ok()?;
        let mut blocks: Vec<Block> = Vec::new();
        for m in &mixes {
            blocks.push(known.get(&Self::loop_name(cid, m))?.clone());
        }
        if blocks.is_empty() {
            return None;
        }
        apply_written(&mut blocks, None);
        let duck = self.duck();
        let vols = mixes.iter().zip(&blocks).map(|(m, b)| {
            duck.as_ref().and_then(|d| d.base(&format!("{cid}_{m}"))).unwrap_or(b.vol())
        });
        Some((vols.max().unwrap_or(0), blocks.iter().all(|b| b.mute)))
    }

    /// Die Mixe hinter einer Angabe: "both"/"all" = alle Mixe, in denen der Kanal ist.
    fn mixes_of(&self, cid: &str, mix: &str) -> Result<Vec<String>> {
        if mix == "both" || mix == "all" {
            let ids = self.channel_mixes(cid);
            if ids.is_empty() {
                return Err(Error(tr!("Channel {id} is in no mix", id = cid)));
            }
            return Ok(ids);
        }
        Ok(vec![mix.to_string()])
    }

    /// [(Mix, Loopback-Block)] aller Mixe hinter `mix` – aus einer einzigen pactl-Abfrage.
    /// `recent`: eine höchstens RECENT_S alte Abfrage genügt (Tasten am Deck).
    fn loops(&self, cid: &str, mix: &str, hint: Option<&str>, recent: bool) -> Result<Vec<(String, Block)>> {
        let mixes = self.mixes_of(cid, mix)?;
        if recent {
            if let Some(known) = recent_loops() {
                if mixes.iter().all(|m| known.contains_key(&Self::loop_name(cid, m))) {
                    let mut blocks: Vec<Block> =
                        mixes.iter().map(|m| known[&Self::loop_name(cid, m)].clone()).collect();
                    // der Beobachter kann eine Abfrage gemerkt haben, die vor unserem Schreiben gestellt war
                    apply_written(&mut blocks, None);
                    return Ok(mixes.into_iter().zip(blocks).collect());
                }
            }
        }
        let out: Vec<(String, Option<Block>)> =
            pactl::snapshot(|| mixes.iter().map(|m| (m.clone(), self.loop_input(cid, m))).collect());
        let mut res = Vec::new();
        for (m, b) in out {
            match b {
                Some(b) => res.push((m, b)),
                None => {
                    return Err(Error(match hint {
                        Some(h) => fill(h, &[("id", cid), ("mix", &m)]),
                        None => tr!("Channel {id}/{mix} not found", id = cid, mix = m),
                    }))
                }
            }
        }
        Ok(res)
    }

    /// Lautstärke eines Kanals in einem Mix ("both"/"all" = alle seine Mixe). Gekoppelte Kanäle nehmen
    /// die anderen Mixe um denselben Betrag mit.
    pub fn set_volume(&self, cid: &str, mix: &str, pct: i64) -> Result<()> {
        let duck = self.duck();
        let hint = tr!("Channel {id}/{mix} not found – is the mixer set up? (freewave mixer setup)");
        if mix != "both" && mix != "all" && self.is_linked(cid) && self.channel_mixes(cid).len() > 1 {
            let loops: IndexMap<String, Block> = self.loops(cid, "both", Some(&hint), false)?.into_iter().collect();
            if let Some(b) = loops.get(mix) {
                let base = duck.as_ref().and_then(|d| d.base(&format!("{cid}_{mix}")));
                let delta = pct - base.unwrap_or(b.vol());
                for (m, b) in &loops {
                    let k = format!("{cid}_{m}");
                    let cur = if m == mix {
                        pct
                    } else {
                        duck.as_ref().and_then(|d| d.base(&k)).unwrap_or(b.vol()) + delta
                    };
                    self.write_volume(&b.index, &k, cur, duck.as_deref(), None)?;
                }
                return Ok(());
            }
        }
        for (m, b) in self.loops(cid, mix, Some(&hint), false)? {
            self.write_volume(&b.index, &format!("{cid}_{m}"), pct, duck.as_deref(), None)?;
        }
        Ok(())
    }

    /// Während Auto-Ducking abgesenkt schreiben und den Nutzerwert erst danach merken. `later`:
    /// Nutzerwerte sammeln und gemeinsam melden (alle Mixe eines Tastendrucks).
    fn write_volume(&self, index: &str, key: &str, pct: i64, duck: Option<&dyn Duck>,
                    later: Option<&Mutex<Vec<(String, i64)>>>) -> Result<()> {
        let pct = pct.clamp(0, MAX_VOLUME);
        let Some(duck) = duck.filter(|d| d.base(key).is_some()) else {
            self.run(&["set-sink-input-volume", index, &format!("{pct}%")])?;
            return Ok(());
        };
        self.run(&["set-sink-input-volume", index, &format!("{}%", duck.volume(pct))])?;
        match later {
            Some(l) => l.lock().unwrap().push((key.to_string(), pct)),
            None => duck.user_set(&[(key.to_string(), pct)]),
        }
        Ok(())
    }

    /// `write(Mix, Block)` für alle Mixe hinter `mix` – gleichzeitig. Stimmt ein gemerkter Index nicht
    /// mehr, einmal frisch auslesen und nur die fehlgeschlagenen Mixe wiederholen.
    fn write_all(&self, cid: &str, mix: &str, write: &(dyn Fn(&str, &Block) -> Result<()> + Sync), recent: bool,
                 loops: Option<Vec<(String, Block)>>) -> Result<Vec<(String, Block)>> {
        let loops = match loops {
            Some(l) => l,
            None => self.loops(cid, mix, None, recent)?,
        };
        let errors: Mutex<Vec<(String, Error)>> = Mutex::new(Vec::new());
        let one = |m: &str, b: &Block| {
            if let Err(e) = write(m, b) {
                errors.lock().unwrap().push((m.to_string(), e));
            }
        };
        if loops.len() == 1 {
            one(&loops[0].0, &loops[0].1);
        } else {
            std::thread::scope(|sc| {
                for (m, b) in &loops {
                    let one = &one;
                    sc.spawn(move || one(m, b));
                }
            });
        }
        let errors = errors.into_inner().unwrap();
        if !errors.is_empty() {
            if recent {
                recent_reset();
                let failed: Vec<&String> = errors.iter().map(|(m, _)| m).collect();
                let again: Vec<(String, Block)> =
                    self.loops(cid, mix, None, false)?.into_iter().filter(|(m, _)| failed.contains(&m)).collect();
                return self.write_all(cid, mix, write, false, Some(again));
            }
            return Err(errors.into_iter().next().unwrap().1);
        }
        Ok(loops)
    }

    /// Lautstärke in allen Mixen hinter `mix` um `delta` ändern; gibt die neuen Werte zurück.
    /// `recent`: zuletzt gelesene Loopbacks genügen (Tasten am Deck – sofort schreiben).
    pub fn change_volume(&self, cid: &str, mix: &str, delta: i64, recent: bool) -> Result<IndexMap<String, i64>> {
        let duck = self.duck();
        let out: Mutex<IndexMap<String, i64>> = Mutex::new(IndexMap::new());
        let bases: Mutex<Vec<(String, i64)>> = Mutex::new(Vec::new());
        let mix = if mix != "both" && mix != "all" && self.is_linked(cid) { "both" } else { mix };
        let write = |m: &str, b: &Block| -> Result<()> {
            let key = format!("{cid}_{m}");
            let base = duck.as_ref().and_then(|d| d.base(&key));
            let pct = (base.unwrap_or(b.vol()) + delta).clamp(0, MAX_VOLUME);
            self.write_volume(&b.index, &key, pct, duck.as_deref(), Some(&bases))?;
            if base.is_none() {
                recent_update(&Self::loop_name(cid, m), Some(pct), None); // gemerkten Stand nachführen
            }
            out.lock().unwrap().insert(m.to_string(), pct);
            Ok(())
        };
        let res = {
            let _g = CHANGE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
            self.write_all(cid, mix, &write, recent, None)
        };
        let bases = bases.into_inner().unwrap();
        if !bases.is_empty() {
            // Ducking: alle geschriebenen Mixe auf einmal melden
            if let Some(d) = &duck {
                d.user_set(&bases);
            }
        }
        res?;
        let mut out = out.into_inner().unwrap();
        // Reihenfolge der Mixe wie angefragt
        let order = self.mixes_of(cid, mix).unwrap_or_default();
        out.sort_by_key(|k, _| order.iter().position(|m| m == k).unwrap_or(usize::MAX));
        Ok(out)
    }

    /// Stumm schalten; Toggle schaltet alle Mixe gemeinsam um. Gibt den neuen Zustand zurück.
    pub fn set_mute(&self, cid: &str, mix: &str, mute: Mute, recent: bool) -> Result<bool> {
        let _g = CHANGE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let loops = self.loops(cid, mix, None, recent)?;
        let mute = match mute {
            Mute::Toggle => !loops.iter().all(|(_, b)| b.mute),
            Mute::Set(m) => m,
        };
        let write = |m: &str, b: &Block| -> Result<()> {
            self.run(&["set-sink-input-mute", &b.index, if mute { "1" } else { "0" }])?;
            recent_update(&Self::loop_name(cid, m), None, Some(mute));
            Ok(())
        };
        self.write_all(cid, mix, &write, recent, Some(loops))?;
        Ok(mute)
    }

    /// Tatsächliche Lautstärken der Loopbacks: {"music_monitor": (Prozent, stumm, Index)}.
    pub fn raw_levels(&self) -> IndexMap<String, (Level, String)> {
        let mixes: HashSet<String> = self.mix_ids().into_iter().collect();
        let mut out = IndexMap::new();
        for b in self.blocks("sink-inputs").iter() {
            let name = loop_id(b);
            let key = name.strip_prefix(PREFIX).unwrap_or("");
            if let Some((_, m)) = key.rsplit_once('_') {
                if mixes.contains(m) {
                    out.insert(key.to_string(), (Level { volume: b.vol(), mute: b.mute }, b.index.clone()));
                }
            }
        }
        out
    }

    // ----------------------------------------------------------- Hauptregler
    /// ("sink"|"source", Name) – wo der Hauptregler eines Kanals sitzt.
    fn master_target(&self, cid: &str, st: Option<&Map<String, Value>>) -> Result<(&'static str, String)> {
        let Some(c) = self.channel(cid) else {
            return Err(Error(tr!("Channel {id} not found", id = cid)));
        };
        if c.is_apps() && c.id != "mic" {
            return Ok(("sink", Self::sink_name(&c.id)));
        }
        let own;
        let st = match st {
            Some(s) => s,
            None => {
                own = self.state();
                &own
            }
        };
        let src = st.get("sources").and_then(|s| s.get(&c.id)).and_then(Value::as_str).unwrap_or("");
        if src.is_empty() {
            return Err(Error(tr!("Channel {id} has no input", id = cid)));
        }
        Ok(("source", src.to_string()))
    }

    /// Hauptregler je Kanal.
    pub fn masters(&self) -> Levels {
        let st = self.state();
        let sinks = self.blocks("sinks");
        let sources = self.blocks("sources");
        let mut out = IndexMap::new();
        for c in self.all_channels() {
            let Ok((kind, name)) = self.master_target(&c.id, Some(&st)) else { continue };
            let list = if kind == "sink" { &sinks } else { &sources };
            if let Some(b) = list.iter().find(|b| b.name() == name) {
                out.insert(c.id, Level { volume: b.volume.unwrap_or(100), mute: b.mute });
            }
        }
        out
    }

    pub fn set_master(&self, cid: &str, pct: i64) -> Result<i64> {
        let (kind, name) = self.master_target(cid, None)?;
        let pct = pct.clamp(0, MAX_VOLUME);
        self.run(&[format!("set-{kind}-volume"), name, format!("{pct}%")])?;
        Ok(pct)
    }

    pub fn set_master_mute(&self, cid: &str, mute: Mute) -> Result<()> {
        let (kind, name) = self.master_target(cid, None)?;
        let v = match mute {
            Mute::Toggle => "toggle",
            Mute::Set(true) => "1",
            Mute::Set(false) => "0",
        };
        self.run(&[format!("set-{kind}-mute"), name, v.to_string()])?;
        Ok(())
    }

    // ----------------------------------------------------------- Abhören
    /// Mix, der gerade auf dem Abhörgerät (Gerät des Persönlichen Mixes) läuft.
    pub fn listening(&self) -> String {
        let l = s(self.state().get("listen"));
        if l.is_empty() {
            "monitor".into()
        } else {
            l
        }
    }

    /// Abhören geht bei Mixen mit eigenem Sink (virtuell oder mehrere Geräte).
    pub fn can_listen(&self, mid: &str) -> bool {
        let Some(m) = self.mix(mid) else { return false };
        if m.id == "monitor" {
            return true;
        }
        !Self::direct(&m, &self.mix_devices(&m, None)) && (m.virtual_ || !m.devices.is_empty())
    }

    fn stop_listen(&self, st: &mut Map<String, Value>) {
        let mods = st.shift_remove("listen_mods").and_then(|v| v.as_array().cloned()).unwrap_or_default();
        for module in mods {
            let _ = self.run(&["unload-module", &s(Some(&module))]);
            arr(st, "modules").retain(|x| *x != module);
        }
        let parked = st.shift_remove("parked").and_then(|v| v.as_object().cloned()).unwrap_or_default();
        if !parked.is_empty() {
            for b in self.blocks("sink-inputs").iter() {
                let lid = loop_id(b);
                if let Some(dev) = parked.get(&lid) {
                    let _ = self.run(&["move-sink-input", &b.index, &s(Some(dev))]);
                }
            }
        }
        st.insert("listen".into(), json!("monitor"));
    }

    /// Einen Mix auf dem Gerät des Persönlichen Mixes abhören ("monitor" = wieder normal). Der
    /// Persönliche Mix wartet so lange auf einem stillen Sink.
    pub fn listen(&self, mid: &str) -> Result<String> {
        let Some(m) = self.mix(mid) else {
            return Err(Error(tr!("Mix {id} not found", id = mid)));
        };
        let mut st = self.state();
        self.stop_listen(&mut st);
        if m.id != "monitor" {
            if !self.can_listen(&m.id) {
                return Err(Error(tr!("“{name}” plays directly on a device – listen to it there", name = m.name)));
            }
            let mut device = s(st.get("monitor_output"));
            if device.is_empty() {
                device = self.default_sink()?;
            }
            let mut mods = vec![self.run(&[
                "load-module".to_string(),
                "module-null-sink".into(),
                format!("sink_name={PARK_SINK}"),
                format!("sink_properties={}", props(&[("device.description", "FreeWave (parked)"),
                                                       ("node.description", "FreeWave (parked)")])),
            ])?];
            let mut parked = Map::new();
            let mon = self.mix("monitor").unwrap_or_default();
            let direct = Self::direct(&mon, &self.mix_devices(&mon, Some(&st)));
            let sinks: HashMap<String, String> =
                self.blocks("sinks").iter().map(|b| (b.index.clone(), b.name().to_string())).collect();
            for b in self.blocks("sink-inputs").iter() {
                let lid = loop_id(b);
                let mine = (direct && lid.ends_with("_monitor")) || lid.starts_with(&format!("{PREFIX}mixout-monitor-"));
                let on = b.sink.as_ref().and_then(|x| sinks.get(x)).cloned().unwrap_or_else(|| device.clone());
                if mine && on == device {
                    parked.insert(lid, json!(device));
                    self.run(&["move-sink-input", &b.index, PARK_SINK])?;
                }
            }
            let label = tr!("FreeWave listen: {mix}", mix = m.name);
            mods.push(self.run(&[
                "load-module".to_string(),
                "module-loopback".into(),
                format!("source={}.monitor", Self::mix_sink(&m)),
                format!("sink={device}"),
                "latency_msec=20".into(),
                "source_dont_move=true".into(),
                format!("sink_input_properties={}",
                        props(&[(LOOP_PROP, LISTEN_LOOP), ("media.name", &label), ("node.description", &label)])),
            ])?);
            st.insert("listen_mods".into(), json!(mods));
            st.insert("parked".into(), Value::Object(parked));
            st.insert("listen".into(), json!(m.id));
            arr(&mut st, "modules").extend(mods.iter().map(|x| json!(x)));
        }
        self.save_state(&st);
        Ok(s(st.get("listen")))
    }

    /// Pegel je Mix: {"mix:<id>": Quelle} (Mix-Sink oder bei direkten Mixen das Gerät).
    pub fn level_sources(&self) -> IndexMap<String, String> {
        let st = self.state();
        let mut out = IndexMap::new();
        for m in self.mixes() {
            let devices = self.mix_devices(&m, Some(&st));
            if Self::direct(&m, &devices) {
                out.insert(format!("mix:{}", m.id), format!("{}.monitor", devices[0]));
            } else if !devices.is_empty() || m.virtual_ {
                out.insert(format!("mix:{}", m.id), format!("{}.monitor", Self::mix_sink(&m)));
            }
        }
        out
    }

    /// Lautstärken und Stummschaltung je Kanal und Mix (beim Auto-Ducking die Werte des Nutzers).
    pub fn levels(&self) -> Levels {
        let duck = self.duck();
        self.raw_levels()
            .into_iter()
            .map(|(k, (lv, _))| {
                let vol = duck.as_ref().and_then(|d| d.base(&k)).unwrap_or(lv.volume);
                (k, Level { volume: vol, mute: lv.mute })
            })
            .collect()
    }

    // ----------------------------------------------------------- Szenen
    /// Gespeicherte Mixer-Szenen.
    pub fn scenes(&self) -> Vec<Scene> {
        self.conf().get("scenes").and_then(Value::as_array).into_iter().flatten().filter_map(Scene::from_value)
            .collect()
    }

    fn scene_values(&self) -> Vec<Value> {
        self.conf().get("scenes").and_then(Value::as_array).into_iter().flatten()
            .filter(|s| s.is_object() && s.get("id").map(truthy).unwrap_or(false)).cloned().collect()
    }

    /// Szene nach id oder Name (ohne Groß-/Kleinschreibung).
    pub fn scene(&self, r: &str) -> Option<Scene> {
        let r = r.trim().to_lowercase();
        if r.is_empty() {
            return None;
        }
        let scenes = self.scenes();
        if let Some(s) = scenes.iter().find(|s| s.id.to_lowercase() == r) {
            return Some(s.clone());
        }
        scenes.into_iter().find(|s| s.name.to_lowercase() == r)
    }

    fn need_scene(&self, r: &str) -> Result<Scene> {
        self.scene(r).ok_or_else(|| Error(tr!("Scene “{name}” not found", name = r)))
    }

    /// Aktueller Mixer-Zustand für eine Szene: {Kanal: {Mix: [Prozent, stumm]}}.
    pub fn scene_snapshot(&self) -> Map<String, Value> {
        let mut out = Map::new();
        for (key, lv) in self.levels() {
            let Some((cid, mix)) = key.rsplit_once('_') else { continue };
            obj(&mut out, cid).insert(mix.to_string(), json!([lv.volume, lv.mute]));
        }
        out
    }

    /// Aktuellen Zustand unter einem Namen speichern (gleicher Name = überschreiben).
    pub fn save_scene(&self, name: &str) -> Result<Scene> {
        let name = name.trim();
        if name.is_empty() {
            return Err(Error(tr!("Please enter a name")));
        }
        let snap = self.scene_snapshot();
        if snap.is_empty() {
            return Err(Error(tr!("Mixer not set up – 'freewave mixer setup'")));
        }
        let mut scenes = self.scene_values();
        let lname = name.to_lowercase();
        let pos = scenes.iter().position(|x| s(x.get("name")).to_lowercase() == lname);
        let pos = match pos {
            Some(p) => p,
            None => {
                static DASH: OnceLock<regex::Regex> = OnceLock::new();
                let re = DASH.get_or_init(|| regex::Regex::new("[^a-z0-9]+").unwrap());
                let base: String = re.replace_all(&ascii_base(name), "-").trim_matches('-').chars().take(24).collect();
                let base = if base.is_empty() { "scene".to_string() } else { base };
                let taken: HashSet<String> = scenes.iter().map(|x| s(x.get("id"))).collect();
                let (mut sid, mut n) = (base.clone(), 2);
                while taken.contains(&sid) {
                    sid = format!("{base}-{n}");
                    n += 1;
                }
                scenes.push(json!({"id": sid, "name": name}));
                scenes.len() - 1
            }
        };
        if let Some(o) = scenes[pos].as_object_mut() {
            o.insert("name".into(), json!(name));
            o.insert("levels".into(), Value::Object(snap));
        }
        let out = Scene::from_value(&scenes[pos]).unwrap_or_default();
        self.edit_conf(|mx| mx.insert("scenes".into(), Value::Array(scenes)));
        Ok(out)
    }

    /// Szene anwenden: Lautstärken und Stummschaltungen aller gespeicherten Kanäle setzen.
    pub fn apply_scene(&self, r: &str) -> Result<Scene> {
        let sc = self.need_scene(r)?;
        let live = self.raw_levels();
        if live.is_empty() {
            return Err(Error(tr!("Mixer not set up – 'freewave mixer setup'")));
        }
        let duck = self.duck();
        for (cid, mixes) in &sc.levels {
            for (mix, (vol, mute)) in mixes {
                let key = format!("{cid}_{mix}");
                let Some((cur, idx)) = live.get(&key) else { continue }; // Kanal gibt es nicht mehr
                self.write_volume(idx, &key, *vol as i64, duck.as_deref(), None)?;
                if *mute != cur.mute {
                    self.run(&["set-sink-input-mute", idx, if *mute { "1" } else { "0" }])?;
                }
            }
        }
        Ok(sc)
    }

    pub fn rename_scene(&self, r: &str, name: &str) -> Result<Scene> {
        let name = name.trim();
        if name.is_empty() {
            return Err(Error(tr!("Please enter a name")));
        }
        let sc = self.need_scene(r)?;
        if self.scenes().iter().any(|x| x.id != sc.id && x.name.to_lowercase() == name.to_lowercase()) {
            return Err(Error(tr!("A scene named “{name}” already exists", name = name)));
        }
        let mut scenes = self.scene_values();
        for x in scenes.iter_mut() {
            if s(x.get("id")) == sc.id {
                x["name"] = json!(name);
            }
        }
        self.edit_conf(|mx| mx.insert("scenes".into(), Value::Array(scenes)));
        self.need_scene(&sc.id)
    }

    pub fn delete_scene(&self, r: &str) -> Result<Scene> {
        let sc = self.need_scene(r)?;
        let scenes: Vec<Value> = self.scene_values().into_iter().filter(|x| s(x.get("id")) != sc.id).collect();
        self.edit_conf(|mx| mx.insert("scenes".into(), Value::Array(scenes)));
        Ok(sc)
    }

    /// Die Szene, die dem aktuellen Zustand entspricht (±1 %), oder None.
    pub fn active_scene(&self, levels: Option<&Levels>) -> Option<Scene> {
        let own;
        let levels = match levels {
            Some(l) => l,
            None => {
                own = self.levels();
                &own
            }
        };
        if levels.is_empty() {
            return None;
        }
        self.scenes().into_iter().find(|sc| {
            let items: Vec<(String, (f64, bool))> = sc.levels.iter()
                .flat_map(|(cid, mixes)| mixes.iter().map(move |(m, v)| (format!("{cid}_{m}"), *v)))
                .filter(|(k, _)| levels.contains_key(k))
                .collect();
            !items.is_empty() && items.iter().all(|(k, (vol, mute))| {
                let l = levels[k];
                (l.volume as f64 - vol).abs() <= 1.0 && l.mute == *mute
            })
        })
    }

    // ----------------------------------------------------------- Ausgänge
    /// Echte Ausgabegeräte (ohne FreeWave-Kanäle).
    pub fn outputs(&self) -> Vec<Device> {
        self.blocks("sinks")
            .iter()
            .filter(|b| !ours(b.name()))
            .map(|b| Device { name: b.name().into(), description: b.description().into() })
            .collect()
    }

    /// Lautstärke/stumm aller Sinks (Name -> Pegel), auch der FreeWave-Mix-Sinks.
    pub fn sink_levels(&self) -> Levels {
        self.blocks("sinks").iter().filter(|b| !b.name().is_empty())
            .map(|b| (b.name().to_string(), Level { volume: b.volume.unwrap_or(100), mute: b.mute }))
            .collect()
    }

    /// Eingangsgerät eines Eingangskanals oder des Mikrofons (leer bei Programm-Kanälen oder ohne Gerät).
    pub fn channel_input(&self, c: &Channel, st: &Map<String, Value>) -> Device {
        let name = if c.id == "mic" {
            let mut mic = s(self.conf().get("mic"));
            if mic.is_empty() {
                mic = s(st.get("mic"));
            }
            mic
        } else if !c.is_apps() {
            c.source.clone()
        } else {
            String::new()
        };
        if name.is_empty() || name.ends_with(".monitor") {
            return Device::default();
        }
        let description = self.blocks("sources").iter().find(|b| b.name() == name)
            .map(|b| b.description().to_string()).filter(|d| !d.is_empty()).unwrap_or_else(|| name.clone());
        Device { name, description }
    }

    /// Persönlichen Mix auf ein anderes Ausgabegerät legen (Name, Teil der Beschreibung oder "next"/"").
    pub fn set_monitor_output(&self, device: &str) -> Result<String> {
        let outs = self.outputs();
        if outs.is_empty() {
            return Err(Error(tr!("No output device found")));
        }
        let mut st = self.state();
        let cur = s(st.get("monitor_output"));
        let target = if device.is_empty() || device == "next" {
            let names: Vec<&String> = outs.iter().map(|o| &o.name).collect();
            match names.iter().position(|n| **n == cur) {
                Some(i) => names[(i + 1) % names.len()].clone(),
                None => names[0].clone(),
            }
        } else {
            let d = device.to_lowercase();
            outs.iter().find(|o| o.name == device || o.description.to_lowercase().contains(&d)).map(|o| o.name.clone())
                .ok_or_else(|| Error(tr!("Output device “{device}” not found", device = device)))?
        };
        let mon = self.mix("monitor").unwrap_or_default();
        if !mon.devices.is_empty() {
            // Persönlicher Mix mit eigener Geräteliste: erstes Gerät tauschen
            let mut devices = vec![target.clone()];
            devices.extend(mon.devices[1..].iter().filter(|d| **d != target).cloned());
            st.insert("monitor_output".into(), json!(target));
            self.save_state(&st);
            self.update_mix("monitor", None, None, None, Some(&devices), None)?;
            return Ok(target);
        }
        self.stop_listen(&mut st);
        for b in self.blocks("sink-inputs").iter() {
            let name = loop_id(b);
            if name.starts_with(PREFIX)
                && (name.ends_with("_monitor") || name.starts_with(&format!("{PREFIX}mixout-monitor-")))
            {
                self.run(&["move-sink-input", &b.index, &target])?;
            }
        }
        st.insert("monitor_output".into(), json!(target));
        self.save_state(&st);
        Ok(target)
    }

    /// Lesbarer Name des Abhörgeräts.
    pub fn monitor_output_name(&self) -> String {
        let cur = s(self.state().get("monitor_output"));
        for o in self.outputs() {
            if o.name == cur {
                return if o.description.is_empty() { cur } else { o.description };
            }
        }
        cur
    }

    // ----------------------------------------------------------- Routing
    /// Neue Wiedergabe-Streams anhand der Regeln in Kanäle verschieben.
    pub fn route_streams(&self) -> usize {
        // eigene Zuordnungen vor den eingebauten, genauere vor kürzeren („firefox-esr“ vor „firefox“)
        let own: HashSet<String> = self.conf().get("routes").and_then(Value::as_object).into_iter().flatten()
            .map(|(k, _)| k.to_lowercase()).collect();
        let mut routes: Vec<(String, String)> = self.routes().into_iter().collect();
        routes.sort_by_key(|(k, _)| (!own.contains(k), -(k.chars().count() as i64)));
        let chans: HashSet<String> = self.app_channels().into_iter().map(|c| c.id).collect();
        let mut moved = 0;
        for b in self.blocks("sink-inputs").iter() {
            {
                let mut seen = self.moved.lock().unwrap();
                if seen.contains(&b.index) || is_ours_stream(b) {
                    continue;
                }
                seen.insert(b.index.clone());
            }
            let names = stream_names(b);
            for (key, cid) in &routes {
                if chans.contains(cid) && route_matches(key, &names) {
                    match self.run(&["move-sink-input", &b.index, &Self::sink_name(cid)]) {
                        Ok(_) => moved += 1,
                        Err(e) => log::debug!("{}", tr!("Moving {stream} failed: {error}", stream = b.index, error = e)),
                    }
                    break;
                }
            }
        }
        moved
    }

    /// Wiedergabe-Streams der Programme mit ihrem Kanal.
    pub fn streams(&self) -> Vec<Stream> {
        let sinks: HashMap<String, String> =
            self.blocks("sinks").iter().map(|b| (b.index.clone(), b.name().to_string())).collect();
        let mut out = Vec::new();
        for b in self.blocks("sink-inputs").iter() {
            if is_ours_stream(b) {
                continue;
            }
            let sink = b.sink.as_ref().and_then(|x| sinks.get(x)).cloned().unwrap_or_default();
            let mut cid = if sink.starts_with(PREFIX) && sink != STREAM_SINK {
                Some(sink[PREFIX.len()..].to_string()).filter(|c| !c.is_empty())
            } else {
                None
            };
            if cid.is_some() && (sink.starts_with(MIX_SINK_PREFIX) || sink == PARK_SINK) {
                cid = None;
            }
            let first = |keys: &[&str]| keys.iter().map(|k| b.prop(k)).find(|v| !v.is_empty()).map(str::to_string);
            out.push(Stream {
                index: b.index.clone(),
                channel: cid,
                sink: sink.clone(),
                app: first(&["application.name", "application.process.binary", "node.name"])
                    .unwrap_or_else(|| tr!("Stream {index}", index = b.index)),
                key: first(&["application.process.binary", "application.name"]).unwrap_or_default().to_lowercase(),
                names: stream_names(b),
            });
        }
        out
    }

    /// Einen Stream in einen Kanal verschieben; mit `remember` (Programmname) dauerhaft merken.
    pub fn move_stream(&self, index: &str, cid: &str, remember: &str) -> Result<()> {
        if !self.app_channels().iter().any(|c| c.id == cid) {
            return Err(Error(tr!("Unknown channel: {id}", id = cid)));
        }
        self.run(&["move-sink-input", index, &Self::sink_name(cid)])?;
        if !remember.is_empty() {
            self.edit_conf(|mx| obj(mx, "routes").insert(remember.to_lowercase(), json!(cid)));
        }
        Ok(())
    }

    /// Programm einem Kanal zuordnen (laufende Streams werden sofort verschoben).
    pub fn assign(&self, app: &str, cid: &str) -> Result<usize> {
        let c = self.channel(cid).filter(|c| self.app_channels().iter().any(|a| a.id == c.id))
            .ok_or_else(|| Error(tr!("Unknown channel: {id}", id = cid)))?;
        let mut n = 0;
        for b in pactl::app_streams(&*self.pactl, app) {
            self.run(&["move-sink-input", &b.index, &Self::sink_name(&c.id)])?;
            n += 1;
        }
        Ok(n)
    }
}

/// Stummschaltung: setzen oder umschalten.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mute {
    Set(bool),
    Toggle,
}

/// „Titel“-Schreibweise wie Pythons `str.title()`.
pub fn title(s: &str) -> String {
    let mut out = String::new();
    let mut prev_alpha = false;
    for c in s.chars() {
        if c.is_alphabetic() {
            if prev_alpha {
                out.extend(c.to_lowercase());
            } else {
                out.extend(c.to_uppercase());
            }
            prev_alpha = true;
        } else {
            out.push(c);
            prev_alpha = false;
        }
    }
    out
}

/// {"music_monitor": [Prozent, stumm]} aus mixer.json.
fn levels_from_value(v: Option<&Value>) -> Levels {
    let mut out = IndexMap::new();
    for (k, val) in v.and_then(Value::as_object).into_iter().flatten() {
        if let Some(a) = val.as_array().filter(|a| a.len() >= 2) {
            out.insert(k.clone(), Level { volume: num(&a[0]).unwrap_or(0.0) as i64, mute: truthy(&a[1]) });
        }
    }
    out
}

fn levels_to_value(l: &Levels) -> Value {
    Value::Object(l.iter().map(|(k, v)| (k.clone(), json!([v.volume, v.mute]))).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn props_quoting() {
        assert_eq!(props(&[("device.description", "FreeWave Mu'sik"), ("node.description", "a\"b")]),
                   "\"device.description='FreeWave Musik' node.description='ab'\"");
    }

    #[test]
    fn rounding_like_python() {
        assert_eq!(py_round(2.5), 2);
        assert_eq!(py_round(3.5), 4);
        assert_eq!(py_round(-0.5), 0);
        assert_eq!(duck_volume(80, 0.5), 40);
        assert_eq!(duck_volume(150, 1.0), 100);
    }

    #[test]
    fn title_case() {
        assert_eq!(title("game.exe"), "Game.Exe");
        assert_eq!(title("firefox-esr"), "Firefox-Esr");
    }
}
