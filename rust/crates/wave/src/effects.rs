//! Effekt-Plugins für FreeWave-Kanäle (wie die VST-Effekte in Elgato Wave Link) – wie
//! `freestream/effects.py`.
//!
//! Jeder Kanal – auch das Mikrofon und eigene Eingänge – kann eine Kette von Effekten bekommen. Sie
//! läuft als PipeWire-Filterkette in einem eigenen `pipewire -c`-Prozess:
//!
//! ```text
//!   Kanal-Quelle ─► Effekt 1 ─► Effekt 2 … ─► „FreeWave <Kanal> (Effekte)“ ─► Monitor-/Stream-Mix
//! ```
//!
//! Verfügbare Effekte: eingebaute PipeWire-Filter (Hochpass, Tiefpass, Bass/Höhen, EQ-Band),
//! LADSPA-Plugins aus den üblichen Ordnern und LV2-Plugins (über `lv2ls`/`lv2info`). Kann die
//! Filterkette dieses PipeWire kein LV2, laufen LV2-Effekte über jalv (Brücken-Modus, per pw-link
//! verkabelt).
//!
//! LADSPA-Plugins werden in einem eigenen Prozess ausgelesen, damit ein fehlerhaftes Plugin FreeWave
//! nicht mitreißt: das Programm meldet dafür mit [`set_scanner`] einen Befehl an, der [`scan_json`]
//! ausgibt. Der Zwischenspeicher (`plugin-cache.json`) ist derselbe wie in der Python-Version.
//!
//! Einstellungen (config.json): `"mixer": {"effects": {"mic": [{"plugin": "builtin:highpass",
//! "enabled": true, "controls": {"Freq": 90}}, …], …}}`

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::{Mutex, OnceLock};
use std::time::Duration;

use freestream_core::tr;
use indexmap::IndexMap;
use serde_json::{json, Map, Value};

use crate::fmt::g;
use crate::mixer::{obj, truthy, Channel, Mixer};
use crate::pactl::{self, which};
use crate::procs;
use crate::{Error, Result};

pub const LADSPA_DIRS: [&str; 6] = ["/usr/lib/ladspa", "/usr/lib/x86_64-linux-gnu/ladspa",
    "/usr/lib/aarch64-linux-gnu/ladspa", "/usr/local/lib/ladspa", "/usr/lib64/ladspa", "~/.ladspa"];
pub const LV2_DIRS: [&str; 6] = ["/usr/lib/lv2", "/usr/lib/x86_64-linux-gnu/lv2", "/usr/lib/aarch64-linux-gnu/lv2",
    "/usr/local/lib/lv2", "/usr/lib64/lv2", "~/.lv2"];
pub const STATE: &str = "effects.json";
pub const CACHE: &str = "plugin-cache.json";

/// Regler eines Plugins.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Control {
    pub name: String,
    /// Anzeigename (LV2), sonst None
    pub label: Option<String>,
    pub min: f64,
    pub max: f64,
    pub default: f64,
    pub unit: Option<String>,
    pub log: bool,
    pub toggle: bool,
    pub int: bool,
}

impl Control {
    fn to_value(&self) -> Value {
        let mut m = Map::new();
        m.insert("name".into(), json!(self.name));
        if let Some(l) = &self.label {
            m.insert("label".into(), json!(l));
        }
        m.insert("min".into(), json!(self.min));
        m.insert("max".into(), json!(self.max));
        m.insert("default".into(), json!(self.default));
        if let Some(u) = &self.unit {
            m.insert("unit".into(), json!(u));
        }
        for (k, on) in [("toggle", self.toggle), ("int", self.int), ("log", self.log)] {
            if on {
                m.insert(k.into(), json!(true));
            }
        }
        Value::Object(m)
    }

    fn from_value(v: &Value) -> Option<Self> {
        let f = |k: &str| v.get(k).and_then(Value::as_f64).unwrap_or(0.0);
        Some(Self {
            name: v.get("name")?.as_str()?.to_string(),
            label: v.get("label").and_then(Value::as_str).map(str::to_string),
            min: f("min"),
            max: f("max"),
            default: f("default"),
            unit: v.get("unit").and_then(Value::as_str).map(str::to_string),
            log: v.get("log").map(truthy).unwrap_or(false),
            toggle: v.get("toggle").map(truthy).unwrap_or(false),
            int: v.get("int").map(truthy).unwrap_or(false),
        })
    }
}

/// Ein Effekt aus dem Katalog.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Plugin {
    /// "builtin:highpass", "ladspa:<Datei>:<Label>" oder "lv2:<URI>"
    pub id: String,
    /// "builtin", "ladspa" oder "lv2"
    pub kind: String,
    /// Pfad der Bibliothek (LADSPA) bzw. URI (LV2)
    pub plugin: String,
    pub label: String,
    pub name: String,
    pub info: String,
    pub inputs: Vec<String>,
    pub outputs: Vec<String>,
    pub controls: Vec<Control>,
}

impl Plugin {
    /// Eintrag im Format des Zwischenspeichers (wie Python).
    pub fn to_value(&self) -> Value {
        json!({"id": self.id, "type": self.kind, "plugin": self.plugin, "label": self.label, "name": self.name,
               "info": self.info, "inputs": self.inputs, "outputs": self.outputs,
               "controls": self.controls.iter().map(Control::to_value).collect::<Vec<_>>()})
    }

    pub fn from_value(v: &Value) -> Option<Self> {
        let st = |k: &str| v.get(k).and_then(Value::as_str).unwrap_or("").to_string();
        let list = |k: &str| -> Vec<String> {
            v.get(k).and_then(Value::as_array).map(|a| a.iter().filter_map(|x| x.as_str().map(str::to_string)).collect())
                .unwrap_or_default()
        };
        Some(Self {
            id: v.get("id")?.as_str()?.to_string(),
            kind: st("type"),
            plugin: st("plugin"),
            label: st("label"),
            name: st("name"),
            info: st("info"),
            inputs: list("inputs"),
            outputs: list("outputs"),
            controls: v.get("controls").and_then(Value::as_array).map(|a| a.iter().filter_map(Control::from_value).collect())
                .unwrap_or_default(),
        })
    }

    fn stereo(&self) -> bool {
        self.inputs.len() == 2
    }
}

fn ctl(name: &str, min: f64, max: f64, default: f64, unit: Option<&str>, log: bool) -> Control {
    Control { name: name.into(), min, max, default, unit: unit.map(str::to_string), log, ..Default::default() }
}

/// Eingebaute Filter von PipeWire (Typ builtin, Ports „In“/„Out“).
pub fn builtin() -> Vec<Plugin> {
    let q = |max: f64, d: f64| ctl("Q", 0.3, max, d, None, false);
    let mk = |id: &str, label: &str, name: &str, info: &str, controls: Vec<Control>| Plugin {
        id: id.into(),
        kind: "builtin".into(),
        plugin: String::new(),
        label: label.into(),
        name: tr!(name),
        info: tr!(info),
        inputs: vec!["In".into()],
        outputs: vec!["Out".into()],
        controls,
    };
    vec![
        mk("builtin:highpass", "bq_highpass", "High-pass (low cut)", "Removes hum and rumble below the frequency",
           vec![ctl("Freq", 20.0, 400.0, 90.0, Some("Hz"), true), q(2.0, 0.707)]),
        mk("builtin:lowpass", "bq_lowpass", "Low-pass", "Attenuates high frequencies, e.g. against hiss",
           vec![ctl("Freq", 2000.0, 20000.0, 12000.0, Some("Hz"), true), q(2.0, 0.707)]),
        mk("builtin:lowshelf", "bq_lowshelf", "Bass", "Boosts or cuts the lows",
           vec![ctl("Freq", 40.0, 500.0, 150.0, Some("Hz"), true), q(2.0, 0.707),
                ctl("Gain", -15.0, 15.0, 3.0, Some("dB"), false)]),
        mk("builtin:highshelf", "bq_highshelf", "Treble", "Boosts or cuts the highs (presence, brilliance)",
           vec![ctl("Freq", 2000.0, 16000.0, 6000.0, Some("Hz"), true), q(2.0, 0.707),
                ctl("Gain", -15.0, 15.0, 3.0, Some("dB"), false)]),
        mk("builtin:peaking", "bq_peaking", "EQ band", "A single equalizer band",
           vec![ctl("Freq", 60.0, 16000.0, 1000.0, Some("Hz"), true), q(6.0, 1.0),
                ctl("Gain", -15.0, 15.0, 0.0, Some("dB"), false)]),
    ]
}

/// Bekannte LADSPA-Plugins mit verständlichem Namen (Label -> Name, Beschreibung).
pub fn friendly(label: &str) -> Option<(String, String)> {
    let (n, i) = match label {
        "noise_suppressor_mono" => ("Noise suppression (RNNoise)", "Filters out keyboard, fans and background"),
        "noise_suppressor_stereo" => ("Noise suppression stereo (RNNoise)", "Filters out background noise"),
        "deep_filter_mono" => ("Noise suppression (DeepFilterNet)", "AI noise filter for voices"),
        "deep_filter_stereo" => ("Noise suppression stereo (DeepFilterNet)", "AI noise filter for voices"),
        "sc4" => ("Compressor (SC4)", "Evens out loud and quiet passages"),
        "sc4m" => ("Compressor mono (SC4)", "Evens out loud and quiet passages"),
        "gate" => ("Noise gate", "Mutes the microphone while you are not speaking"),
        "fastLookaheadLimiter" => ("Limiter", "Prevents clipping"),
        "tap_equalizer" => ("Equalizer (8 bands)", "Tone control"),
        "tap_reverb" => ("Reverb", "Room ambience"),
        "tap_deesser" => ("De-esser", "Tames harsh S sounds"),
        _ => return None,
    };
    Some((tr!(n), tr!(i)))
}

/// Bessere Startwerte für den Einsatz am Mikrofon.
pub fn preset(label: &str) -> &'static [(&'static str, f64)] {
    match label {
        "gate" => &[("Threshold (dB)", -45.0), ("Attack (ms)", 5.0), ("Hold (ms)", 250.0), ("Decay (ms)", 250.0),
                    ("Range (dB)", -60.0)],
        "sc4m" | "sc4" => &[("Threshold level (dB)", -20.0), ("Ratio (1:n)", 3.0), ("Makeup gain (dB)", 4.0)],
        "fastLookaheadLimiter" => &[("Limit (dB)", -1.0)],
        _ => &[],
    }
}

pub const FAVORITES: [&str; 8] = ["noise_suppressor_mono", "deep_filter_mono", "gate", "sc4m", "sc4",
    "fastLookaheadLimiter", "tap_deesser", "tap_equalizer"];

/// Bekannte LV2-Plugins (URI -> Name, Beschreibung); Reihenfolge = Bevorzugung.
pub const LV2_FAVORITES: [&str; 11] = [
    "http://gareus.org/oss/lv2/darc#mono", "http://gareus.org/oss/lv2/darc#stereo",
    "http://gareus.org/oss/lv2/fil4#mono", "http://gareus.org/oss/lv2/fil4#stereo",
    "http://gareus.org/oss/lv2/dpl#mono", "http://gareus.org/oss/lv2/dpl#stereo",
    "http://calf.sourceforge.net/plugins/Gate", "http://calf.sourceforge.net/plugins/Compressor",
    "http://calf.sourceforge.net/plugins/Deesser", "http://calf.sourceforge.net/plugins/Equalizer5Band",
    "urn:jeremy.salwen:plugins:noise-repellent-mono",
];

pub fn friendly_lv2(uri: &str) -> Option<(String, String)> {
    let (n, i) = match uri {
        "http://gareus.org/oss/lv2/darc#mono" => ("Compressor (x42 darc)", "Evens out loud and quiet passages"),
        "http://gareus.org/oss/lv2/darc#stereo" => ("Compressor stereo (x42 darc)", "Evens out loud and quiet passages"),
        "http://gareus.org/oss/lv2/fil4#mono" => ("Equalizer (x42 fil4)", "Parametric EQ with high-pass and low-pass"),
        "http://gareus.org/oss/lv2/fil4#stereo" => ("Equalizer stereo (x42 fil4)", "Parametric EQ"),
        "http://gareus.org/oss/lv2/dpl#mono" => ("Limiter (x42 dpl)", "Prevents clipping"),
        "http://gareus.org/oss/lv2/dpl#stereo" => ("Limiter stereo (x42 dpl)", "Prevents clipping"),
        "http://calf.sourceforge.net/plugins/Gate" => ("Noise gate (Calf)", "Mutes while you are not speaking"),
        "http://calf.sourceforge.net/plugins/Compressor" => ("Compressor (Calf)", "Evens out loud and quiet passages"),
        "http://calf.sourceforge.net/plugins/Deesser" => ("De-esser (Calf)", "Tames harsh S sounds"),
        "http://calf.sourceforge.net/plugins/Equalizer5Band" => ("Equalizer 5 bands (Calf)", "Tone control"),
        "urn:jeremy.salwen:plugins:noise-repellent-mono" => ("Noise suppression (Noise Repellent)",
                                                             "Filters out constant noise"),
        _ => return None,
    };
    Some((tr!(n), tr!(i)))
}

fn expand(d: &str) -> String {
    match d.strip_prefix("~/") {
        Some(rest) => freestream_core::paths::home().join(rest).to_string_lossy().into_owned(),
        None => d.to_string(),
    }
}

/// Ordner mit LADSPA-Plugins (Standardordner und $LADSPA_PATH).
pub fn ladspa_dirs() -> Vec<String> {
    let mut v: Vec<String> = LADSPA_DIRS.iter().map(|d| expand(d)).collect();
    v.extend(std::env::var("LADSPA_PATH").unwrap_or_default().split(':').map(str::to_string));
    v
}

// =================================================================== LADSPA
const P_INPUT: i32 = 1;
const P_CONTROL: i32 = 4;
const P_AUDIO: i32 = 8;
const H_BELOW: i32 = 0x1;
const H_ABOVE: i32 = 0x2;
const H_TOGGLED: i32 = 0x4;
const H_SRATE: i32 = 0x8;
const H_LOG: i32 = 0x10;
const H_INT: i32 = 0x20;
const H_DEFAULT: i32 = 0x3C0;
const RATE: f64 = 48000.0;

#[repr(C)]
struct LadspaHint {
    hint: libc::c_int,
    lower: libc::c_float,
    upper: libc::c_float,
}

/// Anfang von `LADSPA_Descriptor` (die Funktionszeiger dahinter brauchen wir nicht).
#[repr(C)]
struct LadspaDesc {
    unique_id: libc::c_ulong,
    label: *const libc::c_char,
    properties: libc::c_int,
    name: *const libc::c_char,
    maker: *const libc::c_char,
    copyright: *const libc::c_char,
    port_count: libc::c_ulong,
    port_descriptors: *const libc::c_int,
    port_names: *const *const libc::c_char,
    port_hints: *const LadspaHint,
}

/// Standardwert eines LADSPA-Reglers aus seinen Hinweisen (wie `effects._default`).
pub fn ladspa_default(hint: i32, lo: f64, hi: f64) -> f64 {
    let d = hint & H_DEFAULT;
    let log = hint & H_LOG != 0 && lo > 0.0 && hi > 0.0;
    let between = |t: f64| {
        if log {
            (lo.ln() * (1.0 - t) + hi.ln() * t).exp()
        } else {
            lo * (1.0 - t) + hi * t
        }
    };
    match d {
        0x40 => lo,
        0x80 => between(0.25),
        0xC0 => between(0.5),
        0x100 => between(0.75),
        0x140 => hi,
        0x200 => 0.0,
        0x240 => 1.0,
        0x280 => 100.0,
        0x2C0 => 440.0,
        _ => {
            if hint & H_BELOW != 0 {
                lo
            } else {
                0.0
            }
        }
    }
}

unsafe fn cstr(p: *const libc::c_char) -> String {
    if p.is_null() {
        return String::new();
    }
    std::ffi::CStr::from_ptr(p).to_string_lossy().into_owned()
}

/// Alle Plugins einer LADSPA-Bibliothek. Lädt die Bibliothek in diesen Prozess – nur im Scan-Prozess
/// aufrufen ([`scan_json`]).
pub fn read_ladspa(path: &Path) -> std::result::Result<Vec<Plugin>, String> {
    let c = std::ffi::CString::new(path.to_string_lossy().as_bytes()).map_err(|e| e.to_string())?;
    // SAFETY: fremde Bibliothek – deshalb läuft das in einem eigenen Prozess
    unsafe {
        let lib = libc::dlopen(c.as_ptr(), libc::RTLD_NOW | libc::RTLD_LOCAL);
        if lib.is_null() {
            return Err(cstr(libc::dlerror()));
        }
        let sym = libc::dlsym(lib, c"ladspa_descriptor".as_ptr());
        if sym.is_null() {
            return Err("ladspa_descriptor".into());
        }
        let f = std::mem::transmute::<*mut libc::c_void, extern "C" fn(libc::c_ulong) -> *const LadspaDesc>(sym);
        let fname = path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        let mut out = Vec::new();
        for i in 0..256 {
            let p = f(i);
            if p.is_null() {
                break;
            }
            let d = &*p;
            let (mut ins, mut outs, mut controls) = (Vec::new(), Vec::new(), Vec::new());
            for k in 0..d.port_count as usize {
                let pd = *d.port_descriptors.add(k);
                let name = cstr(*d.port_names.add(k));
                if pd & P_AUDIO != 0 {
                    if pd & P_INPUT != 0 {
                        ins.push(name);
                    } else {
                        outs.push(name);
                    }
                } else if pd & P_CONTROL != 0 && pd & P_INPUT != 0 {
                    let h = &*d.port_hints.add(k);
                    let rate = if h.hint & H_SRATE != 0 { RATE } else { 1.0 };
                    let mut lo = if h.hint & H_BELOW != 0 { f64::from(h.lower) * rate } else { 0.0 };
                    let mut hi = if h.hint & H_ABOVE != 0 { f64::from(h.upper) * rate } else { 1.0f64.max(lo + 1.0) };
                    if h.hint & H_TOGGLED != 0 {
                        lo = 0.0;
                        hi = 1.0;
                    }
                    controls.push(Control {
                        name,
                        min: lo,
                        max: hi,
                        default: ladspa_default(h.hint, lo, hi),
                        toggle: h.hint & H_TOGGLED != 0,
                        int: h.hint & H_INT != 0,
                        log: h.hint & H_LOG != 0,
                        ..Default::default()
                    });
                }
            }
            if !(ins.len() == 1 || ins.len() == 2) || outs.len() != ins.len() {
                continue; // nur Filter mit gleich vielen Ein- und Ausgängen (mono/stereo)
            }
            let label = cstr(d.label);
            let fr = friendly(&label);
            out.push(Plugin {
                id: format!("ladspa:{fname}:{label}"),
                kind: "ladspa".into(),
                plugin: path.to_string_lossy().into_owned(),
                name: fr.as_ref().map(|f| f.0.clone()).unwrap_or_else(|| cstr(d.name)),
                info: fr.map(|f| f.1).unwrap_or_else(|| cstr(d.maker)),
                label,
                inputs: ins,
                outputs: outs,
                controls,
            });
        }
        Ok(out)
    }
}

/// LADSPA-Bibliotheken (jede echte Datei nur einmal).
pub fn ladspa_files() -> Vec<PathBuf> {
    let mut seen = std::collections::HashSet::new();
    let mut out = Vec::new();
    for d in ladspa_dirs() {
        if d.is_empty() || !Path::new(&d).is_dir() {
            continue;
        }
        let mut files: Vec<PathBuf> = std::fs::read_dir(&d).into_iter().flatten().flatten().map(|e| e.path())
            .filter(|p| p.extension().is_some_and(|e| e == "so")).collect();
        files.sort();
        for f in files {
            let real = std::fs::canonicalize(&f).unwrap_or_else(|_| f.clone());
            if seen.insert(real) {
                out.push(f);
            }
        }
    }
    out
}

// ===================================================================== LV2
const LV2: &str = "http://lv2plug.in/ns/";

/// Features, die die Filterkette von PipeWire bereitstellt; Plugins mit anderen Pflicht-Features fehlen.
fn lv2_supported(f: &str) -> bool {
    let Some(rest) = f.strip_prefix(LV2) else { return false };
    ["ext/urid#map", "ext/urid#unmap", "ext/options#options", "ext/buf-size#boundedBlockLength",
     "ext/worker#schedule", "ext/log#log", "lv2core#isLive", "lv2core#hardRTCapable"].contains(&rest)
}

fn lv2_dirs() -> Vec<String> {
    let mut dirs: Vec<String> = LV2_DIRS.iter().map(|d| expand(d)).collect();
    dirs.extend(std::env::var("LV2_PATH").unwrap_or_default().split(':').map(str::to_string));
    let mut seen = std::collections::HashSet::new();
    dirs.into_iter()
        .filter(|d| !d.is_empty() && Path::new(d).is_dir())
        .filter(|d| seen.insert(std::fs::canonicalize(d).unwrap_or_else(|_| PathBuf::from(d))))
        .collect()
}

type Fields = IndexMap<String, Vec<String>>;

/// Ausgabe von `lv2info <URI>` -> Katalogeintrag (oder None, wenn kein passender Filter).
pub fn parse_lv2info(text: &str, uri: &str) -> Option<Plugin> {
    let mut head: Fields = IndexMap::new();
    let mut ports: Vec<Fields> = Vec::new();
    let mut in_port = false;
    let mut key: Option<String> = None;
    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() {
            continue;
        }
        if line.starts_with("Port ") && line.ends_with(':') {
            ports.push(IndexMap::new());
            in_port = true;
            key = None;
            continue;
        }
        let target = if in_port { ports.last_mut().unwrap() } else { &mut head };
        let keyed = line.split_once(':').filter(|(k, _)| {
            let k = k.replace(' ', "");
            !k.is_empty() && k.chars().all(char::is_alphabetic)
        });
        if let (Some((k, v)), false, false) = (keyed, line.starts_with("http"), line.starts_with("file:")) {
            let (k, v) = (k.trim().to_string(), v.trim().to_string());
            let e = target.entry(k.clone()).or_default();
            if !v.is_empty() {
                e.push(v);
            }
            key = Some(k);
        } else if let Some(k) = &key {
            target.entry(k.clone()).or_default().push(line.to_string());
        }
    }
    let get = |m: &Fields, k: &str| m.get(k).cloned().unwrap_or_default();
    if get(&head, "Required Features").iter().any(|f| !f.is_empty() && !lv2_supported(f)) {
        return None;
    }
    let name0 = get(&head, "Name").first().cloned().unwrap_or_default();
    let class = get(&head, "Class").join(" ");
    if ["Analy", "Instrument", "Generator"].iter().any(|x| class.contains(x))
        || ["Meter", "Display", "Scope", "Tuner", "Analy"].iter().any(|x| name0.contains(x))
    {
        return None; // Pegelanzeigen u. Ä. brauchen ihre eigene Oberfläche
    }
    let (mut ins, mut outs, mut controls) = (Vec::new(), Vec::new(), Vec::new());
    for pt in &ports {
        let types = get(pt, "Type").join(" ");
        let sym = get(pt, "Symbol").first().cloned().unwrap_or_default();
        let props = get(pt, "Properties").join(" ");
        if types.contains("AudioPort") {
            if types.contains("InputPort") {
                ins.push(sym);
            } else {
                outs.push(sym);
            }
        } else if types.contains("ControlPort") && types.contains("InputPort") {
            let num = |k: &str, d: f64| get(pt, k).first().and_then(|x| x.trim().parse::<f64>().ok()).unwrap_or(d);
            let lo = num("Minimum", 0.0);
            let mut hi = num("Maximum", 1.0);
            if hi <= lo {
                hi = lo + 1.0;
            }
            if get(pt, "Designation").join(" ").contains("enabled") {
                continue; // „Enable“-Schalter des Plugins: immer an
            }
            controls.push(Control {
                label: Some(get(pt, "Name").first().cloned().unwrap_or_else(|| sym.clone())),
                name: sym,
                min: lo,
                max: hi,
                default: num("Default", lo).clamp(lo, hi),
                toggle: props.contains("toggled"),
                int: props.contains("integer") || props.contains("enumeration"),
                log: props.contains("logarithmic"),
                unit: None,
            });
        } else if types.contains("ControlPort") {
            continue; // Ausgabewerte (Pegel, Gain-Reduktion)
        } else {
            return None; // Atom/CV/MIDI – nicht als Effekt nutzbar
        }
    }
    if !(ins.len() == 1 || ins.len() == 2) || outs.len() != ins.len() {
        return None;
    }
    let name = get(&head, "Name").first().cloned().unwrap_or_else(|| uri.to_string());
    let fr = friendly_lv2(uri);
    let author = get(&head, "Author").first().cloned().unwrap_or_default();
    let label = uri.rsplit('/').next().unwrap_or(uri).split('#').next().unwrap_or("").to_string();
    Some(Plugin {
        id: format!("lv2:{uri}"),
        kind: "lv2".into(),
        plugin: uri.into(),
        label,
        name: fr.as_ref().map(|f| f.0.clone()).unwrap_or(name),
        info: fr.map(|f| f.1).unwrap_or(author),
        inputs: ins,
        outputs: outs,
        controls,
    })
}

/// Alle LV2-Effekte über `lv2ls`/`lv2info` (Paket lilv-utils).
pub fn read_lv2() -> Vec<Plugin> {
    if which("lv2ls").is_none() || which("lv2info").is_none() {
        return Vec::new();
    }
    let Some(out) = procs::run_quiet(&["lv2ls"], 20.0) else { return Vec::new() };
    let uris: Vec<String> = out.stdout.split_whitespace().map(str::to_string).collect();
    // 8 gleichzeitig, Reihenfolge bleibt
    let mut res: Vec<Option<Plugin>> = vec![None; uris.len()];
    for (chunk_i, chunk) in uris.chunks(8).enumerate() {
        let found: Vec<Option<Plugin>> = std::thread::scope(|sc| {
            let hs: Vec<_> = chunk.iter().map(|u| sc.spawn(move || {
                procs::run_quiet(&["lv2info", u], 10.0).and_then(|o| parse_lv2info(&o.stdout, u))
            })).collect();
            hs.into_iter().map(|h| h.join().ok().flatten()).collect()
        });
        for (i, p) in found.into_iter().enumerate() {
            res[chunk_i * 8 + i] = p;
        }
    }
    res.into_iter().flatten().collect()
}

/// Alle Plugins einlesen (LADSPA in diesem Prozess!) – für den Scan-Prozess.
pub fn scan_all() -> Vec<Plugin> {
    let mut found = Vec::new();
    for f in ladspa_files() {
        match read_ladspa(&f) {
            Ok(p) => found.extend(p),
            Err(e) => eprintln!("{}", tr!("skipped {path}: {error}", path = f.display(), error = e)),
        }
    }
    found.extend(read_lv2());
    found
}

/// Ausgabe des Scan-Prozesses (JSON-Liste wie `python3 -m freestream.effects --scan`).
pub fn scan_json() -> String {
    procs::py_dumps(&Value::Array(scan_all().iter().map(Plugin::to_value).collect()))
}

static SCANNER: Mutex<Option<Vec<String>>> = Mutex::new(None);

/// Befehl für den Scan-Prozess anmelden (z. B. `[<eigenes Programm>, "--scan-plugins"]`); er muss
/// [`scan_json`] ausgeben. Ohne Anmeldung liest FreeWave die Plugins im eigenen Prozess.
pub fn set_scanner(cmd: Vec<String>) {
    *SCANNER.lock().unwrap() = Some(cmd);
}

fn mtime(p: &str) -> f64 {
    use std::os::unix::fs::MetadataExt;
    std::fs::metadata(p).map(|m| m.mtime() as f64 + m.mtime_nsec() as f64 * 1e-9).unwrap_or(0.0)
}

fn stamp_eq(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Array(x), Value::Array(y)) => x.len() == y.len() && x.iter().zip(y).all(|(p, q)| stamp_eq(p, q)),
        (Value::Number(x), Value::Number(y)) => {
            (x.as_f64().unwrap_or(0.0) - y.as_f64().unwrap_or(0.0)).abs() < 1e-5
        }
        _ => a == b,
    }
}

/// Alle verfügbaren Effekte: eingebaute zuerst, dann bekannte und übrige LADSPA- und LV2-Plugins.
pub fn catalog(state_dir: &Path, refresh: bool) -> Vec<Plugin> {
    let mut files: Vec<String> = ladspa_files().iter().map(|f| f.to_string_lossy().into_owned()).collect();
    files.extend(lv2_dirs());
    let mut stamp: Vec<Value> = files.iter().map(|f| json!([f, mtime(f)])).collect();
    stamp.push(json!(which("lv2info").is_some()));
    let stamp = Value::Array(stamp);
    let cache = state_dir.join(CACHE);
    let mut plugins: Option<Vec<Value>> = None;
    if !refresh {
        let data = procs::read_json(&cache);
        if data.get("stamp").is_some_and(|s| stamp_eq(s, &stamp)) {
            plugins = data.get("plugins").and_then(Value::as_array).cloned();
        }
    }
    let plugins = match plugins {
        Some(p) => p,
        None => {
            let mut found = Vec::new();
            if !files.is_empty() {
                let scanner = SCANNER.lock().unwrap().clone();
                match scanner {
                    Some(cmd) if !cmd.is_empty() => {
                        let mut c = Command::new(&cmd[0]);
                        c.args(&cmd[1..]);
                        match pactl::run_timeout(&mut c, Duration::from_secs(30)) {
                            Some(o) => match serde_json::from_str::<Value>(if o.stdout.trim().is_empty() { "[]" } else { &o.stdout }) {
                                Ok(Value::Array(a)) => found = a,
                                _ => log::warn!("{}", tr!("Could not read plugins: {error}", error = "JSON")),
                            },
                            None => log::warn!("{}", tr!("Could not read plugins: {error}", error = cmd[0])),
                        }
                    }
                    _ => found = scan_all().iter().map(Plugin::to_value).collect(),
                }
            }
            let _ = std::fs::create_dir_all(state_dir);
            let _ = std::fs::write(&cache, procs::py_dumps(&json!({"stamp": stamp, "plugins": found})));
            found
        }
    };
    let mut plugins: Vec<Plugin> = plugins.iter().filter_map(Plugin::from_value).collect();
    // Anzeigenamen in der aktuellen Sprache (der Zwischenspeicher kann aus einer anderen stammen)
    for p in plugins.iter_mut() {
        let fr = if p.kind == "lv2" { friendly_lv2(&p.plugin) } else { friendly(&p.label) };
        if let Some((n, i)) = fr {
            p.name = n;
            p.info = i;
        }
    }
    let rank = |p: &Plugin| -> usize {
        if let Some(i) = LV2_FAVORITES.iter().position(|u| format!("lv2:{u}") == p.id) {
            return 100 + i;
        }
        if let Some(i) = FAVORITES.iter().position(|l| *l == p.id) {
            return i;
        }
        if p.kind == "ladspa" {
            FAVORITES.iter().position(|l| *l == p.label).unwrap_or(999)
        } else {
            999
        }
    };
    plugins.sort_by_key(|p| (rank(p), p.name.to_lowercase()));
    let mut out = builtin();
    out.extend(plugins);
    out
}

/// Effekt nach id (eingebaute ohne Katalog).
pub fn lookup(id: &str, cat: Option<&[Plugin]>, state_dir: &Path) -> Option<Plugin> {
    if let Some(b) = builtin().into_iter().find(|b| b.id == id) {
        return Some(b);
    }
    match cat {
        Some(c) => c.iter().find(|p| p.id == id).cloned(),
        None => catalog(state_dir, false).into_iter().find(|p| p.id == id),
    }
}

/// Ein Effekt in der Kette eines Kanals.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Effect {
    pub plugin: String,
    pub name: String,
    pub enabled: bool,
    pub controls: IndexMap<String, f64>,
    /// weitere Schlüssel aus config.json (bleiben erhalten)
    pub extra: Map<String, Value>,
}

impl Effect {
    pub fn from_value(v: &Value) -> Option<Self> {
        let o = v.as_object()?;
        let mut extra = o.clone();
        for k in ["plugin", "name", "enabled", "controls"] {
            extra.shift_remove(k);
        }
        Some(Self {
            plugin: o.get("plugin")?.as_str()?.to_string(),
            name: o.get("name").and_then(Value::as_str).unwrap_or("").to_string(),
            enabled: o.get("enabled").map(truthy).unwrap_or(true),
            controls: o.get("controls").and_then(Value::as_object).into_iter().flatten()
                .filter_map(|(k, v)| v.as_f64().map(|f| (k.clone(), f))).collect(),
            extra,
        })
    }

    pub fn to_value(&self) -> Value {
        let mut m = Map::new();
        m.insert("plugin".into(), json!(self.plugin));
        m.insert("name".into(), json!(self.name));
        m.insert("enabled".into(), json!(self.enabled));
        m.insert("controls".into(), Value::Object(self.controls.iter().map(|(k, v)| (k.clone(), num_value(*v))).collect()));
        for (k, v) in &self.extra {
            m.insert(k.clone(), v.clone());
        }
        Value::Object(m)
    }
}

/// Ganze Zahlen als Zahl ohne Komma (wie sie Python aus 90 statt 90.0 schreibt).
fn num_value(v: f64) -> Value {
    if v.fract() == 0.0 && v.abs() < 1e15 {
        json!(v as i64)
    } else {
        json!(v)
    }
}

/// Eintrag für die Effektkette eines Kanals mit den Standardwerten des Plugins.
pub fn new_effect(p: &Plugin) -> Effect {
    let pre = preset(&p.label);
    Effect {
        plugin: p.id.clone(),
        name: p.name.clone(),
        enabled: true,
        controls: p.controls.iter()
            .map(|c| (c.name.clone(), pre.iter().find(|(n, _)| *n == c.name).map(|(_, v)| *v).unwrap_or(c.default)))
            .collect(),
        extra: Map::new(),
    }
}

// ================================================================ Filterkette
pub(crate) fn q(s: &str) -> String {
    format!("\"{}\"", s.replace('"', "'"))
}

/// filter.graph einer Kette.
#[derive(Debug, Clone, PartialEq)]
pub struct Graph {
    pub nodes: Vec<String>,
    pub links: Vec<String>,
    pub inputs: Vec<String>,
    pub outputs: Vec<String>,
}

/// filter.graph für eine Effektkette – immer stereo (Mono-Plugins laufen je Seite einmal).
pub fn graph(chain: &[Effect], lookup: &dyn Fn(&str) -> Option<Plugin>) -> Option<Graph> {
    let (mut nodes, mut links) = (Vec::new(), Vec::new());
    let mut lanes: [Option<String>; 2] = [None, None];
    let mut first: [Option<String>; 2] = [None, None];
    let mut n = 0;
    for fx in chain {
        if !fx.enabled {
            continue;
        }
        let Some(p) = lookup(&fx.plugin) else {
            log::warn!("{}", tr!("Effect {plugin} not found – skipped", plugin = fx.plugin));
            continue;
        };
        let ctl: Vec<String> = fx.controls.iter().filter(|(k, _)| p.controls.iter().any(|c| c.name == **k))
            .map(|(k, v)| format!("{} = {}", q(k), g(*v))).collect();
        let ctl = ctl.join(" ");
        let stereo = p.stereo();
        let names = if stereo { vec![format!("fx{n}")] } else { vec![format!("fx{n}l"), format!("fx{n}r")] };
        n += 1;
        for nm in &names {
            nodes.push(match p.kind.as_str() {
                "builtin" => format!("{{ type = builtin name = {nm} label = {} control = {{ {ctl} }} }}", p.label),
                "lv2" => format!("{{ type = lv2 name = {nm} plugin = {} control = {{ {ctl} }} }}", q(&p.plugin)),
                _ => format!("{{ type = ladspa name = {nm} plugin = {} label = {} control = {{ {ctl} }} }}",
                             q(&p.plugin), p.label),
            });
        }
        for lane in 0..2 {
            let nm = if stereo { &names[0] } else { &names[lane] };
            let port_in = format!("{nm}:{}", p.inputs[if stereo { lane } else { 0 }]);
            let port_out = format!("{nm}:{}", p.outputs[if stereo { lane } else { 0 }]);
            match &lanes[lane] {
                Some(prev) => links.push(format!("{{ output = {} input = {} }}", q(prev), q(&port_in))),
                None => first[lane] = Some(port_in),
            }
            lanes[lane] = Some(port_out);
        }
    }
    if nodes.is_empty() {
        return None;
    }
    let both = |a: [Option<String>; 2]| a.into_iter().map(|x| x.unwrap_or_default()).collect();
    Some(Graph { nodes, links, inputs: both(first), outputs: both(lanes) })
}

const HEAD: &str = "context.properties = { log.level = 0 }
context.spa-libs = {
    audio.convert.* = audioconvert/libspa-audioconvert
    support.*       = support/libspa-support
}
context.modules = [
    { name = libpipewire-module-rt args = { nice.level = -11 } flags = [ ifexists nofail ] }
    { name = libpipewire-module-protocol-native }
    { name = libpipewire-module-client-node }
    { name = libpipewire-module-adapter }
";

/// Kopf jeder erzeugten PipeWire-Konfiguration (Module ohne Filterkette).
pub(crate) fn head() -> &'static str {
    HEAD
}

fn graph_block(g: &Graph) -> String {
    let nl = "\n                    ";
    format!(
        "            filter.graph = {{
                nodes = [
                    {}
                ]
                links = [ {} ]
                inputs  = [ {} ]
                outputs = [ {} ]
            }}
",
        g.nodes.join(nl),
        g.links.join(" "),
        g.inputs.iter().map(|x| q(x)).collect::<Vec<_>>().join(" "),
        g.outputs.iter().map(|x| q(x)).collect::<Vec<_>>().join(" ")
    )
}

/// PipeWire-Konfiguration für `pipewire -c`. `stage = Some(i)`: nur eine Teilkette (Brücken-Modus) –
/// Ein- und Ausgang werden von FreeWave per pw-link verkabelt.
pub fn conf(cid: &str, label: &str, source: &str, chain: &[Effect], lookup: &dyn Fn(&str) -> Option<Plugin>,
            stage: Option<usize>) -> Result<String> {
    let Some(gr) = graph(chain, lookup) else {
        return Err(Error(tr!("No active effects")));
    };
    let node = format!("linewave_{cid}_fx");
    if let Some(i) = stage {
        return Ok(stage_conf(&node, i, label, &gr));
    }
    let target = match source.strip_suffix(".monitor") {
        Some(sink) => format!("target.object = {}\n                stream.capture.sink = true", q(sink)),
        None => format!("target.object = {}", q(source)),
    };
    let desc = q(&tr!("FreeWave {name} (Effects)", name = label));
    Ok(format!(
        "# Von FreeWave erzeugt – Effekte für {label}
{HEAD}    {{ name = libpipewire-module-filter-chain
        args = {{
            node.description = {desc}
            media.name       = {desc}
{graph}            audio.channels = 2
            audio.position = [ FL FR ]
            capture.props = {{
                node.name    = \"{node}_in\"
                node.passive = true
                {target}
            }}
            playback.props = {{
                node.name        = \"{node}\"
                node.description = {desc}
                media.class      = Audio/Source
            }}
        }}
    }}
]
",
        graph = graph_block(&gr)
    ))
}

fn stage_conf(node: &str, stage: usize, label: &str, gr: &Graph) -> String {
    let desc = q(&tr!("FreeWave {name} (Effects {n})", name = label, n = stage + 1));
    format!(
        "# Von FreeWave erzeugt – Teilkette {stage} der Effekte für {label}
{HEAD}    {{ name = libpipewire-module-filter-chain
        args = {{
            node.description = {desc}
{graph}            audio.channels = 2
            audio.position = [ FL FR ]
            capture.props = {{
                node.name        = \"{node}_s{stage}_in\"
                node.autoconnect = false
                node.passive     = true
            }}
            playback.props = {{
                node.name        = \"{node}_s{stage}_out\"
                node.autoconnect = false
            }}
        }}
    }}
]
",
        graph = graph_block(gr)
    )
}

/// Dateien, die zu einem Muster wie `/usr/lib*/*/pipewire-0.3/x.so` passen (einfaches glob mit `*`).
pub(crate) fn glob_any(pattern: &str) -> bool {
    fn walk(base: PathBuf, parts: &[&str]) -> bool {
        let Some((first, rest)) = parts.split_first() else { return base.exists() };
        if !first.contains('*') {
            return walk(base.join(first), rest);
        }
        let (pre, post) = first.split_once('*').unwrap_or((first, ""));
        let Ok(rd) = std::fs::read_dir(&base) else { return false };
        rd.flatten().any(|e| {
            let n = e.file_name().to_string_lossy().into_owned();
            !n.starts_with('.') && n.starts_with(pre) && n.ends_with(post) && n.len() >= pre.len() + post.len()
                && walk(base.join(&n), rest)
        })
    }
    let parts: Vec<&str> = pattern.trim_start_matches('/').split('/').collect();
    walk(PathBuf::from("/"), &parts)
}

/// Kann die Filterkette dieses PipeWire LV2-Plugins laden? (nicht jede Distribution baut das mit)
pub fn pipewire_lv2() -> bool {
    ["/usr/lib*/pipewire-0.3/libpipewire-module-filter-chain-lv2.so",
     "/usr/lib*/*/pipewire-0.3/libpipewire-module-filter-chain-lv2.so",
     "/usr/lib*/spa-0.2/filter-graph/libspa-filter-graph-plugin-lv2.so",
     "/usr/lib*/*/spa-0.2/filter-graph/libspa-filter-graph-plugin-lv2.so",
     "/usr/local/lib*/pipewire-0.3/libpipewire-module-filter-chain-lv2.so"]
        .iter()
        .any(|p| glob_any(p))
}

/// LV2 über jalv (pipewire-jack), wenn die Filterkette selbst kein LV2 kann.
pub fn lv2_bridge_available() -> bool {
    ["jalv", "pw-jack", "pw-link"].iter().all(|x| which(x).is_some())
}

/// (verfügbar, Hinweis) für LV2-Effekte auf diesem System.
pub fn lv2_status() -> (bool, String) {
    if pipewire_lv2() {
        return (true, String::new());
    }
    if lv2_bridge_available() {
        return (true, tr!("via jalv"));
    }
    (false, tr!("LV2 needs the packages jalv and pipewire-jack (or a PipeWire with LV2 support)"))
}

/// Teil einer Kette im Brücken-Modus.
#[derive(Debug, Clone)]
pub enum Stage {
    /// Filterkette mit (Ketten-Index, Effekt, Plugin)
    Fc(Vec<(usize, Effect, Plugin)>),
    /// ein LV2-Effekt über jalv
    Lv2(usize, Box<Effect>, Box<Plugin>),
}

/// Kette für den Brücken-Modus aufteilen.
pub fn stages(chain: &[Effect], lookup: &dyn Fn(&str) -> Option<Plugin>) -> Vec<Stage> {
    let mut out: Vec<Stage> = Vec::new();
    for (i, fx) in chain.iter().enumerate() {
        if !fx.enabled {
            continue;
        }
        let Some(p) = lookup(&fx.plugin) else { continue };
        if p.kind == "lv2" {
            out.push(Stage::Lv2(i, Box::new(fx.clone()), Box::new(p)));
        } else if let Some(Stage::Fc(items)) = out.last_mut() {
            items.push((i, fx.clone(), p));
        } else {
            out.push(Stage::Fc(vec![(i, fx.clone(), p)]));
        }
    }
    out
}

/// Kanal -> {Name: jalv-Prozess} – für Live-Regler über die Standardeingabe von jalv.
fn jalv_procs() -> &'static Mutex<HashMap<String, HashMap<String, Child>>> {
    static P: OnceLock<Mutex<HashMap<String, HashMap<String, Child>>>> = OnceLock::new();
    P.get_or_init(Default::default)
}

/// Läuft ein Kind noch? (nach dem Einsammeln durch den Reaper: nein)
fn child_alive(c: &mut Child) -> bool {
    matches!(c.try_wait(), Ok(None)) && procs::alive(i64::from(c.id()))
}

/// Startet und beendet die Effektketten der Kanäle (ein pipewire-Prozess je Kanal). Die Prozess-IDs
/// stehen in `effects.json`, damit jeder FreeWave-Prozess sie verwalten kann.
pub struct Effects<'a> {
    mixer: &'a Mixer,
}

impl Mixer {
    /// Effektketten der Kanäle.
    pub fn fx(&self) -> Effects<'_> {
        Effects { mixer: self }
    }
}

impl Effects<'_> {
    fn state_file(&self) -> PathBuf {
        self.mixer.state_dir().join(STATE)
    }

    fn all(&self) -> Map<String, Value> {
        self.mixer.conf().get("effects").and_then(Value::as_object).cloned().unwrap_or_default()
    }

    /// Effektkette eines Kanals.
    pub fn chain(&self, cid: &str) -> Vec<Effect> {
        self.all().get(cid).and_then(Value::as_array).map(|a| a.iter().filter_map(Effect::from_value).collect())
            .unwrap_or_default()
    }

    /// Hat der Kanal eingeschaltete Effekte?
    pub fn active(&self, cid: &str) -> bool {
        self.chain(cid).iter().any(|fx| fx.enabled)
    }

    /// Kette speichern (wirksam nach [`Mixer::rebuild_channel`]).
    pub fn set_chain(&self, cid: &str, chain: &[Effect]) {
        self.mixer.edit_conf(|mx| {
            let fx = obj(mx, "effects");
            if chain.is_empty() {
                fx.shift_remove(cid);
            } else {
                fx.insert(cid.into(), Value::Array(chain.iter().map(Effect::to_value).collect()));
            }
        });
    }

    pub fn node_name(cid: &str) -> String {
        format!("linewave_{cid}_fx")
    }

    /// Katalog (aus dem Zwischenspeicher, wenn er passt).
    pub fn catalog(&self, refresh: bool) -> Vec<Plugin> {
        catalog(self.mixer.state_dir(), refresh)
    }

    fn state(&self) -> Map<String, Value> {
        procs::read_json(&self.state_file())
    }

    fn save(&self, st: &Map<String, Value>) {
        let _ = procs::write_json(&self.state_file(), &Value::Object(st.clone()));
    }

    /// Läuft die Effektkette des Kanals?
    pub fn running(&self, cid: &str) -> bool {
        let st = self.state();
        let Some(e) = st.get(cid).filter(|e| truthy(e)) else { return false };
        if e.get("mode").and_then(Value::as_str) == Some("bridge") {
            let ps = e.get("procs").and_then(Value::as_array).cloned().unwrap_or_default();
            return !ps.is_empty() && ps.iter().all(|x| {
                procs::ours(x.get("pid").and_then(Value::as_i64).unwrap_or(0), x.get("tag").and_then(Value::as_str).unwrap_or(""))
            });
        }
        procs::ours(e.get("pid").and_then(Value::as_i64).unwrap_or(0), e.get("conf").and_then(Value::as_str).unwrap_or(""))
    }

    fn lookup_fn<'c>(&'c self, cat: Option<&'c [Plugin]>) -> impl Fn(&str) -> Option<Plugin> + 'c {
        move |id: &str| lookup(id, cat, self.mixer.state_dir())
    }

    fn needs_bridge(&self, cid: &str, cat: Option<&[Plugin]>) -> bool {
        if pipewire_lv2() {
            return false;
        }
        stages(&self.chain(cid), &self.lookup_fn(cat)).iter().any(|s| matches!(s, Stage::Lv2(..)))
    }

    /// Effektkette eines Kanals starten; gibt den Namen der bearbeiteten Quelle zurück.
    pub fn start(&self, c: &Channel, source: &str) -> Result<String> {
        let cid = &c.id;
        self.stop(cid);
        let chain = self.chain(cid);
        let cat = chain.iter().any(|fx| fx.plugin.starts_with("lv2:")).then(|| self.catalog(false));
        if self.needs_bridge(cid, cat.as_deref()) {
            return self.start_bridge(c, source, cat.as_deref());
        }
        let exe = which("pipewire").ok_or_else(|| Error(tr!("pipewire not found")))?;
        let path = self.mixer.state_dir().join(format!("effects-{cid}.conf"));
        let _ = std::fs::create_dir_all(self.mixer.state_dir());
        let text = conf(cid, &c.name, source, &chain, &self.lookup_fn(cat.as_deref()), None)?;
        std::fs::write(&path, text).map_err(|e| Error(e.to_string()))?;
        let path_s = path.to_string_lossy().into_owned();
        let mut child = procs::spawn_detached(Command::new(exe).args(["-c", &path_s]), false)
            .map_err(|e| Error(e.to_string()))?;
        let mut st = self.state();
        st.insert(cid.clone(), json!({"pid": child.id(), "conf": path_s}));
        self.save(&st);
        let node = Self::node_name(cid);
        for _ in 0..30 {
            // bis die neue Quelle bei PipeWire angemeldet ist
            if pactl::source_exists(self.mixer.pactl(), &node) {
                break;
            }
            if !child_alive(&mut child) {
                return Err(Error(tr!("Effect chain could not start (plugin missing or invalid setting)")));
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        log::info!("{}", tr!("Effects for {name} active ({count})", name = c.name,
                             count = chain.iter().filter(|f| f.enabled).count()));
        Ok(node)
    }

    // ------------------------------------------------------ Brücken-Modus
    fn ports(direction: &str) -> Vec<String> {
        let flag = if direction == "out" { "-o" } else { "-i" };
        procs::run_quiet(&["pw-link", flag], 3.0)
            .map(|o| o.stdout.lines().map(|l| l.trim().to_string()).filter(|l| !l.is_empty()).collect())
            .unwrap_or_default()
    }

    /// Zwei Ports (links, rechts) aus einer Liste; Mono wird auf beide Seiten gelegt.
    pub fn pair(ports: Vec<String>) -> Option<[String; 2]> {
        let order = |p: &String| {
            let n = p.rsplit(':').next().unwrap_or("").to_uppercase();
            let side = if ["FL", "_L", "LEFT", "_1"].iter().any(|s| n.ends_with(s)) {
                0
            } else if ["FR", "_R", "RIGHT", "_2"].iter().any(|s| n.ends_with(s)) {
                1
            } else {
                2
            };
            (side, p.clone())
        };
        let mut ports = ports;
        ports.sort_by_key(order);
        let a = ports.first()?.clone();
        let b = ports.get(1).cloned().unwrap_or_else(|| a.clone());
        Some([a, b])
    }

    fn source_ports(source: &str) -> Option<[String; 2]> {
        let outs = Self::ports("out");
        match source.strip_suffix(".monitor") {
            Some(node) => Self::pair(outs.into_iter().filter(|p| p.starts_with(&format!("{node}:")) && p.contains(":monitor_")).collect()),
            None => Self::pair(outs.into_iter().filter(|p| p.starts_with(&format!("{source}:")) && !p.contains(":monitor_")).collect()),
        }
    }

    /// Kette mit LV2-Effekten ohne LV2 in PipeWire: Teilketten + jalv, per pw-link verbunden.
    fn start_bridge(&self, c: &Channel, source: &str, cat: Option<&[Plugin]>) -> Result<String> {
        let cid = &c.id;
        if !lv2_bridge_available() {
            return Err(Error(tr!("LV2 effects need the packages jalv and pipewire-jack")));
        }
        let node = Self::node_name(cid);
        let desc = tr!("FreeWave {name} (Effects)", name = c.name);
        let mid = self.mixer.run(&[
            "load-module".to_string(),
            "module-null-sink".into(),
            "media.class=Audio/Source/Virtual".into(),
            format!("sink_name={node}"),
            "channel_map=front-left,front-right".into(),
            format!("sink_properties={}", crate::mixer::props(&[("device.description", &desc),
                                                                 ("node.description", &desc)])),
        ])?;
        let mut entry = json!({"mode": "bridge", "module": mid, "procs": [], "stages": []});
        let mut st = self.state();
        st.insert(cid.clone(), entry.clone());
        self.save(&st);
        let chain = self.chain(cid);
        let lk = self.lookup_fn(cat);
        let mut wanted: Vec<([String; 2], [String; 2])> = Vec::new();
        let mut jalv: HashMap<String, Child> = HashMap::new();
        for (i, stg) in stages(&chain, &lk).into_iter().enumerate() {
            match stg {
                Stage::Fc(items) => {
                    let path = self.mixer.state_dir().join(format!("effects-{cid}-s{i}.conf"));
                    let sub: Vec<Effect> = items.iter().map(|(_, fx, _)| fx.clone()).collect();
                    let text = conf(cid, &c.name, source, &sub, &lk, Some(i))?;
                    std::fs::write(&path, text).map_err(|e| Error(e.to_string()))?;
                    let path_s = path.to_string_lossy().into_owned();
                    let exe = which("pipewire").map(|p| p.to_string_lossy().into_owned()).unwrap_or("pipewire".into());
                    let pr = procs::spawn_detached(Command::new(exe).args(["-c", &path_s]), false)
                        .map_err(|e| Error(e.to_string()))?;
                    entry["procs"].as_array_mut().unwrap().push(json!({"pid": pr.id(), "tag": path_s}));
                    entry["stages"].as_array_mut().unwrap().push(json!({"kind": "fc",
                        "index": items.iter().map(|(ix, _, _)| ix).collect::<Vec<_>>(), "node": format!("{node}_s{i}_in")}));
                    wanted.push(([format!("{node}_s{i}_in:input_FL"), format!("{node}_s{i}_in:input_FR")],
                                 [format!("{node}_s{i}_out:output_FL"), format!("{node}_s{i}_out:output_FR")]));
                }
                Stage::Lv2(ix, fx, p) => {
                    let stereo = p.stereo();
                    let names = if stereo { vec![format!("{node}_s{i}")] } else { vec![format!("{node}_s{i}l"), format!("{node}_s{i}r")] };
                    let ctl: Vec<String> = fx.controls.iter().filter(|(k, _)| p.controls.iter().any(|cc| cc.name == **k))
                        .map(|(k, v)| format!("{k}={}", g(*v))).collect();
                    for nm in &names {
                        let mut args = vec!["jalv".to_string(), "-x".into(), "-n".into(), nm.clone()];
                        for x in &ctl {
                            args.push("-c".into());
                            args.push(x.clone());
                        }
                        args.push(p.plugin.clone());
                        let pr = procs::spawn_detached(Command::new("pw-jack").args(&args), true)
                            .map_err(|e| Error(e.to_string()))?;
                        entry["procs"].as_array_mut().unwrap().push(json!({"pid": pr.id(), "tag": nm}));
                        jalv.insert(nm.clone(), pr);
                    }
                    let (ins, outs) = if stereo {
                        ([format!("{}:{}", names[0], p.inputs[0]), format!("{}:{}", names[0], p.inputs[1])],
                         [format!("{}:{}", names[0], p.outputs[0]), format!("{}:{}", names[0], p.outputs[1])])
                    } else {
                        ([format!("{}:{}", names[0], p.inputs[0]), format!("{}:{}", names[1], p.inputs[0])],
                         [format!("{}:{}", names[0], p.outputs[0]), format!("{}:{}", names[1], p.outputs[0])])
                    };
                    entry["stages"].as_array_mut().unwrap().push(json!({"kind": "lv2", "index": [ix], "jalv": names}));
                    wanted.push((ins, outs));
                }
            }
        }
        jalv_procs().lock().unwrap().insert(cid.clone(), jalv);
        st.insert(cid.clone(), entry);
        self.save(&st);
        let fin = [format!("{node}:input_FL"), format!("{node}:input_FR")];
        let mut src = None;
        let mut ready = false;
        for _ in 0..60 {
            // bis alle Ports da sind (jalv braucht einen Moment)
            let have_in: std::collections::HashSet<String> = Self::ports("in").into_iter().collect();
            src = Self::source_ports(source);
            if src.is_some() && wanted.iter().all(|(ins, _)| ins.iter().all(|p| have_in.contains(p)))
                && fin.iter().all(|p| have_in.contains(p))
            {
                let have_out: std::collections::HashSet<String> = Self::ports("out").into_iter().collect();
                if wanted.iter().all(|(_, outs)| outs.iter().all(|p| have_out.contains(p))) {
                    ready = true;
                    break;
                }
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        if !ready {
            self.stop(cid);
            return Err(Error(tr!("Effect chain (LV2 via jalv) could not start")));
        }
        let mut prev = src.unwrap_or_default().to_vec();
        for (ins, outs) in wanted.iter().map(|(i, o)| (i.clone(), Some(o.clone()))).chain([(fin.clone(), None)]) {
            for (a, b) in prev.iter().zip(ins.iter()) {
                let _ = procs::run_quiet(&["pw-link", a, b], 3.0);
            }
            prev = outs.map(|o| o.to_vec()).unwrap_or_default();
        }
        log::info!("{}", tr!("Effects for {name} active (LV2 via jalv, {count} stages)", name = c.name,
                             count = wanted.len()));
        Ok(node)
    }

    /// Quelle für die Mixe: mit aktiven Effekten die Effektkette, sonst die Kanal-Quelle selbst.
    pub fn source_for(&self, c: &Channel, source: &str) -> String {
        if !self.active(&c.id) {
            self.stop(&c.id);
            return source.to_string();
        }
        match self.start(c, source) {
            Ok(n) => n,
            Err(e) => {
                log::warn!("{}", tr!("Effects for {name} not started: {error}", name = c.name, error = e));
                source.to_string()
            }
        }
    }

    /// Effektkette eines Kanals beenden.
    pub fn stop(&self, cid: &str) {
        let mut st = self.state();
        let e = st.shift_remove(cid);
        let list: Vec<(i64, String)> = match &e {
            Some(e) if e.get("mode").and_then(Value::as_str) == Some("bridge") => e.get("procs").and_then(Value::as_array)
                .into_iter().flatten()
                .map(|x| (x.get("pid").and_then(Value::as_i64).unwrap_or(0), x.get("tag").and_then(Value::as_str).unwrap_or("").to_string()))
                .collect(),
            Some(e) if truthy(e) => vec![(e.get("pid").and_then(Value::as_i64).unwrap_or(0),
                                          e.get("conf").and_then(Value::as_str).unwrap_or("").to_string())],
            _ => Vec::new(),
        };
        for (pid, tag) in list {
            if procs::ours(pid, &tag) {
                procs::terminate(pid);
            }
        }
        if let Some(m) = e.as_ref().and_then(|e| e.get("module")).filter(|m| truthy(m)) {
            let m = m.as_str().map(str::to_string).unwrap_or_else(|| m.to_string());
            let _ = self.mixer.run(&["unload-module", &m]);
        }
        // Standardeingabe von jalv schließen (beendet jalv)
        jalv_procs().lock().unwrap().remove(cid);
        if e.is_some() {
            self.save(&st);
        }
    }

    pub fn stop_all(&self) {
        for cid in self.state().keys().cloned().collect::<Vec<_>>() {
            self.stop(&cid);
        }
    }

    /// Reglerwert ohne Neustart setzen (pw-cli bzw. jalv); false, wenn das nicht geht.
    pub fn set_live(&self, cid: &str, chain_index: usize, control: &str, value: f64) -> bool {
        let e = self.state().get(cid).cloned().unwrap_or(Value::Null);
        let mut want = format!("{}_in", Self::node_name(cid));
        let mut offset = 0;
        if e.get("mode").and_then(Value::as_str) == Some("bridge") {
            let stage = e.get("stages").and_then(Value::as_array).into_iter().flatten().find(|s| {
                s.get("index").and_then(Value::as_array).is_some_and(|ix| ix.iter().any(|x| x.as_u64() == Some(chain_index as u64)))
            });
            let Some(stage) = stage else { return false };
            if stage.get("kind").and_then(Value::as_str) == Some("lv2") {
                use std::io::Write;
                let mut ok = false;
                let mut all = jalv_procs().lock().unwrap();
                for nm in stage.get("jalv").and_then(Value::as_array).into_iter().flatten().filter_map(Value::as_str) {
                    let Some(pr) = all.get_mut(cid).and_then(|m| m.get_mut(nm)) else { return false };
                    if !child_alive(pr) {
                        return false;
                    }
                    let Some(stdin) = pr.stdin.as_mut() else { return false };
                    if stdin.write_all(format!("{control} = {}\n", g(value)).as_bytes()).and_then(|_| stdin.flush()).is_err() {
                        return false;
                    }
                    ok = true;
                }
                return ok;
            }
            want = stage.get("node").and_then(Value::as_str).unwrap_or("").to_string();
            offset = stage.get("index").and_then(Value::as_array).into_iter().flatten().filter_map(Value::as_u64)
                .min().unwrap_or(0) as usize;
        }
        let Some(nid) = pw_node_id(&want) else { return false };
        // Name des Knotens im Graphen (nur aktive Effekte zählen, ab Beginn der Teilkette)
        let chain = self.chain(cid);
        let mut k: i64 = -1;
        for (i, fx) in chain.iter().enumerate() {
            if i >= offset && fx.enabled {
                k += 1;
            }
            if i == chain_index {
                break;
            }
        }
        let Some(fx) = chain.get(chain_index) else { return false };
        let p = lookup(&fx.plugin, None, self.mixer.state_dir());
        let names = if p.is_some_and(|p| p.stereo()) { vec![format!("fx{k}")] } else { vec![format!("fx{k}l"), format!("fx{k}r")] };
        let params: Vec<String> = names.iter().map(|nm| format!("\"{nm}:{control}\" {}", g(value))).collect();
        pw_set_props(nid, &format!("{{ params = [ {} ] }}", params.join(" ")))
    }
}

/// Id eines PipeWire-Knotens nach node.name (über pw-dump).
pub(crate) fn pw_node_id(name: &str) -> Option<i64> {
    if which("pw-cli").is_none() || which("pw-dump").is_none() {
        return None;
    }
    let out = procs::run_quiet(&["pw-dump", "Node"], 3.0)?;
    let nodes: Value = serde_json::from_str(if out.stdout.trim().is_empty() { "[]" } else { &out.stdout }).ok()?;
    nodes.as_array()?.iter().find(|n| {
        n.get("info").and_then(|i| i.get("props")).and_then(|p| p.get("node.name")).and_then(Value::as_str) == Some(name)
    }).and_then(|n| n.get("id")).and_then(Value::as_i64)
}

/// `pw-cli set-param <id> Props <Wert>`; true bei Erfolg.
pub(crate) fn pw_set_props(id: i64, value: &str) -> bool {
    procs::run_quiet(&["pw-cli", "set-param", &id.to_string(), "Props", value], 3.0).is_some_and(|o| o.status == Some(0))
}
