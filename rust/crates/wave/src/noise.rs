//! Rauschunterdrückung für das Mikrofon in FreeWave – wie `freestream/noise.py`.
//!
//! Sie sitzt als eigene Stufe vor den Effekten des Mikrofon-Kanals:
//!
//! ```text
//!   Mikrofon ─► Rauschunterdrückung („linewave_mic_ns“) ─► Effekte des Kanals ─► Mixe
//! ```
//!
//! Zwei Verfahren, automatisch gewählt (oder fest über "backend"):
//!   * rnnoise – LADSPA-Plugin librnnoise_ladspa.so in einer PipeWire-Filterkette; die Stärke ist die
//!     VAD-Schwelle des Plugins.
//!   * webrtc – das Echo-Cancel-Modul von PipeWire mit der WebRTC-Verarbeitung (ohne Referenzsignal
//!     arbeitet nur die Rauschunterdrückung; eine Stärke gibt es nicht).
//!
//! Beides läuft in einem eigenen `pipewire -c`-Prozess (PID in `noise.json`).
//!
//! Einstellungen (config.json): `"mixer": {"noise": {"enabled": true, "backend": "auto", "strength": 50}}`

use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use freestream_core::tr;
use serde_json::{json, Map, Value};

use crate::effects::{self, glob_any, head, q};
use crate::fmt::g;
use crate::mixer::{obj, truthy, Mixer};
use crate::pactl::{self, which};
use crate::procs;
use crate::{Error, Result};

pub const NODE: &str = "linewave_mic_ns";
pub const STATE: &str = "noise.json";
pub const RNNOISE_LIB: &str = "librnnoise_ladspa.so";
pub const RNNOISE_LABEL: &str = "noise_suppressor_mono";
pub const RNNOISE_VAD: &str = "VAD Threshold (%)";
pub const BACKENDS: [&str; 2] = ["rnnoise", "webrtc"];
/// VAD-Schwelle in %; darüber schneidet RNNoise auch Sprache weg
pub const MAX_STRENGTH: i64 = 95;

const WEBRTC_MODULE: [&str; 4] = [
    "/usr/lib*/pipewire-0.3/libpipewire-module-echo-cancel.so",
    "/usr/lib*/*/pipewire-0.3/libpipewire-module-echo-cancel.so",
    "/usr/local/lib*/pipewire-0.3/libpipewire-module-echo-cancel.so",
    "/usr/local/lib*/*/pipewire-0.3/libpipewire-module-echo-cancel.so",
];
const WEBRTC_AEC: [&str; 4] = [
    "/usr/lib*/spa-0.2/aec/libspa-aec-webrtc.so",
    "/usr/lib*/*/spa-0.2/aec/libspa-aec-webrtc.so",
    "/usr/local/lib*/spa-0.2/aec/libspa-aec-webrtc.so",
    "/usr/local/lib*/*/spa-0.2/aec/libspa-aec-webrtc.so",
];

/// Anzeigename eines Verfahrens.
pub fn backend_name(backend: &str) -> String {
    match backend {
        "rnnoise" => "RNNoise".into(),
        "webrtc" => tr!("PipeWire WebRTC"),
        "" => "–".into(),
        other => other.into(),
    }
}

/// Pfad zu librnnoise_ladspa.so oder None.
pub fn rnnoise_plugin() -> Option<String> {
    effects::ladspa_dirs().into_iter().filter(|d| !d.is_empty()).map(|d| Path::new(&d).join(RNNOISE_LIB))
        .find(|p| p.is_file()).map(|p| p.to_string_lossy().into_owned())
}

/// Echo-Cancel-Modul und WebRTC-Plugin von PipeWire vorhanden?
pub fn webrtc_available() -> bool {
    WEBRTC_MODULE.iter().any(|p| glob_any(p)) && WEBRTC_AEC.iter().any(|p| glob_any(p))
}

/// Nutzbare Verfahren in der Reihenfolge der Bevorzugung.
pub fn available() -> Vec<String> {
    let mut out = Vec::new();
    if rnnoise_plugin().is_some() {
        out.push("rnnoise".to_string());
    }
    if webrtc_available() {
        out.push("webrtc".to_string());
    }
    out
}

/// Verfahren für die Einstellung "backend" (auto = RNNoise, sonst WebRTC) oder None.
pub fn pick(pref: &str) -> Option<String> {
    let have = available();
    if BACKENDS.contains(&pref) && have.iter().any(|h| h == pref) {
        return Some(pref.to_string());
    }
    have.into_iter().next()
}

pub fn install_hint() -> String {
    tr!("No noise suppression available. Install the package noise-suppression-for-voice (RNNoise) or \
         libspa-0.2-modules and libpipewire-0.3-modules (PipeWire WebRTC).")
}

/// Konfiguration mit RNNoise.
pub fn rnnoise_conf(source: &str, plugin: &str, strength: f64, control: &str) -> String {
    let desc = q(&tr!("FreeWave Microphone (noise suppression)"));
    format!(
        "# Von FreeWave erzeugt – Rauschunterdrückung (RNNoise)
{head}    {{ name = libpipewire-module-filter-chain
        args = {{
            node.description = {desc}
            media.name       = {desc}
            filter.graph = {{
                nodes = [
                    {{ type = ladspa name = ns plugin = {plugin} label = {RNNOISE_LABEL}
                      control = {{ {control} = {strength} }} }}
                ]
            }}
            audio.channels = 1
            audio.position = [ MONO ]
            capture.props = {{
                node.name      = \"{NODE}_in\"
                node.passive   = true
                target.object  = {source}
                stream.dont-remix = true
            }}
            playback.props = {{
                node.name        = \"{NODE}\"
                node.description = {desc}
                media.class      = Audio/Source
            }}
        }}
    }}
]
",
        head = head(),
        plugin = q(plugin),
        control = q(control),
        strength = g(strength),
        source = q(source)
    )
}

/// Echo-Cancel-Modul nur zur Rauschunterdrückung: die Referenz bleibt unverbunden (Stille).
pub fn webrtc_conf(source: &str) -> String {
    let desc = q(&tr!("FreeWave Microphone (noise suppression)"));
    let r = q(&tr!("FreeWave noise suppression (reference)"));
    format!(
        "# Von FreeWave erzeugt – Rauschunterdrückung (PipeWire WebRTC)
{head}    {{ name = libpipewire-module-echo-cancel
        args = {{
            library.name = aec/libspa-aec-webrtc
            monitor.mode = true
            node.latency = 480/48000
            audio.rate     = 48000
            audio.channels = 1
            audio.position = [ MONO ]
            aec.args = {{
                webrtc.noise_suppression = true
                webrtc.high_pass_filter  = true
                webrtc.gain_control      = false
                webrtc.extended_filter   = false
                webrtc.delay_agnostic    = false
                webrtc.voice_detection   = false
            }}
            capture.props = {{
                node.name      = \"{NODE}_in\"
                node.passive   = true
                target.object  = {source}
            }}
            source.props = {{
                node.name        = \"{NODE}\"
                node.description = {desc}
            }}
            sink.props = {{
                node.name        = \"{NODE}_ref\"
                node.description = {r}
                node.autoconnect = false
                node.passive     = true
                priority.session = 0
                priority.driver  = 0
            }}
            playback.props = {{
                node.name        = \"{NODE}_ref_out\"
                node.autoconnect = false
                node.passive     = true
            }}
        }}
    }}
]
",
        head = head(),
        source = q(source)
    )
}

/// Einstellungen der Rauschunterdrückung.
#[derive(Debug, Clone, PartialEq)]
pub struct NoiseSettings {
    pub enabled: bool,
    /// "auto", "rnnoise" oder "webrtc"
    pub backend: String,
    pub strength: i64,
}

/// Zustand für die Anzeige.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct NoiseStatus {
    pub enabled: bool,
    pub strength: i64,
    pub backend_pref: String,
    pub backend: Option<String>,
    pub running: bool,
    pub available: Vec<String>,
    pub hint: String,
}

/// Startet/beendet die Rauschunterdrückung für den Mikrofon-Kanal des Mixers.
pub struct NoiseSuppression<'a> {
    mixer: &'a Mixer,
}

impl Mixer {
    /// Rauschunterdrückung des Mikrofons.
    pub fn noise(&self) -> NoiseSuppression<'_> {
        NoiseSuppression { mixer: self }
    }
}

impl NoiseSuppression<'_> {
    fn state_file(&self) -> PathBuf {
        self.mixer.state_dir().join(STATE)
    }

    pub fn settings(&self) -> NoiseSettings {
        let ns = self.mixer.conf().get("noise").and_then(Value::as_object).cloned().unwrap_or_default();
        NoiseSettings {
            enabled: ns.get("enabled").map(truthy).unwrap_or(false),
            backend: ns.get("backend").and_then(Value::as_str).unwrap_or("auto").to_string(),
            strength: ns.get("strength").and_then(Value::as_f64).map(|f| f as i64).unwrap_or(50),
        }
    }

    pub fn enabled(&self) -> bool {
        self.settings().enabled
    }

    /// Einstellungen ändern und mit der Mixer-Konfiguration speichern (None = unverändert).
    pub fn set(&self, enabled: Option<bool>, backend: Option<&str>, strength: Option<f64>) -> Result<NoiseSettings> {
        if let Some(b) = backend {
            if !BACKENDS.contains(&b) && b != "auto" {
                return Err(Error(tr!("Unknown method: {name}", name = b)));
            }
        }
        self.mixer.edit_conf(|mx| {
            let ns = obj(mx, "noise");
            if let Some(e) = enabled {
                ns.insert("enabled".into(), json!(e));
            }
            if let Some(b) = backend {
                ns.insert("backend".into(), json!(b));
            }
            if let Some(s) = strength {
                ns.insert("strength".into(), json!((crate::mixer::py_round(s)).clamp(0, MAX_STRENGTH)));
            }
        });
        Ok(self.settings())
    }

    /// Mikrofon-Kanal neu verbinden, damit die Einstellung wirkt (nur bei eingerichtetem Mixer).
    pub fn apply(&self) -> Result<bool> {
        if self.mixer.is_active() {
            self.mixer.rebuild_channel("mic")?;
            return Ok(true);
        }
        Ok(false)
    }

    /// Stärke speichern; läuft RNNoise, live setzen, sonst den Kanal neu verbinden.
    pub fn set_strength(&self, value: f64) -> Result<i64> {
        let s = self.set(None, None, Some(value))?;
        if s.enabled && self.backend().as_deref() == Some("rnnoise") && !self.set_live(s.strength) {
            self.apply()?;
        }
        Ok(s.strength)
    }

    pub fn backend(&self) -> Option<String> {
        let b = self.settings().backend;
        pick(if b.is_empty() { "auto" } else { &b })
    }

    fn state(&self) -> Map<String, Value> {
        procs::read_json(&self.state_file())
    }

    fn save(&self, st: Value) {
        let _ = procs::write_json(&self.state_file(), &st);
    }

    pub fn running(&self) -> bool {
        let st = self.state();
        let pid = st.get("pid").and_then(Value::as_i64).unwrap_or(0);
        pid != 0 && procs::ours(pid, st.get("conf").and_then(Value::as_str).unwrap_or(""))
    }

    pub fn status(&self) -> NoiseStatus {
        let s = self.settings();
        let st = self.state();
        let run = self.running();
        let have = available();
        NoiseStatus {
            enabled: s.enabled,
            strength: s.strength,
            backend_pref: if s.backend.is_empty() { "auto".into() } else { s.backend },
            backend: if run { st.get("backend").and_then(Value::as_str).map(str::to_string) } else { self.backend() },
            running: run,
            hint: if have.is_empty() { install_hint() } else { String::new() },
            available: have,
        }
    }

    /// Name des Reglers für die VAD-Schwelle (aus dem Plugin-Katalog, sonst der übliche Name).
    fn vad_control(&self, plugin: &str) -> String {
        let fname = Path::new(plugin).file_name().map(|f| f.to_owned());
        effects::catalog(self.mixer.state_dir(), false).into_iter()
            .find(|p| p.label == RNNOISE_LABEL && Path::new(&p.plugin).file_name().map(|f| f.to_owned()) == fname)
            .map(|p| p.controls.iter().find(|c| c.name.to_lowercase().starts_with("vad threshold")).map(|c| c.name.clone())
                .unwrap_or_else(|| RNNOISE_VAD.to_string()))
            .unwrap_or_else(|| RNNOISE_VAD.to_string())
    }

    /// Rauschunterdrückung für eine Quelle starten; gibt den Namen der bearbeiteten Quelle zurück.
    pub fn start(&self, source: &str) -> Result<String> {
        self.stop();
        let backend = self.backend().ok_or_else(|| Error(install_hint()))?;
        let exe = which("pipewire").ok_or_else(|| Error(tr!("pipewire not found")))?;
        let path = self.mixer.state_dir().join("noise.conf");
        let _ = std::fs::create_dir_all(self.mixer.state_dir());
        let mut control = String::new();
        let text = if backend == "rnnoise" {
            let plugin = rnnoise_plugin().unwrap_or_default();
            control = self.vad_control(&plugin);
            rnnoise_conf(source, &plugin, self.settings().strength as f64, &control)
        } else {
            webrtc_conf(source)
        };
        std::fs::write(&path, text).map_err(|e| Error(e.to_string()))?;
        let path_s = path.to_string_lossy().into_owned();
        let child = procs::spawn_detached(Command::new(exe).args(["-c", &path_s]), false)
            .map_err(|e| Error(e.to_string()))?;
        let pid = i64::from(child.id());
        self.save(json!({"pid": pid, "conf": path_s, "backend": backend, "source": source, "control": control}));
        for _ in 0..30 {
            // bis die neue Quelle bei PipeWire angemeldet ist
            if pactl::source_exists(self.mixer.pactl(), NODE) {
                break;
            }
            if !procs::ours(pid, &path_s) {
                self.save(json!({}));
                return Err(Error(tr!("Noise suppression could not start ({backend})",
                                     backend = backend_name(&backend))));
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        log::info!("{}", tr!("Noise suppression active ({backend})", backend = backend_name(&backend)));
        Ok(NODE.to_string())
    }

    /// Quelle für den Mikrofon-Kanal: mit Rauschunterdrückung deren Ausgang, sonst die Quelle selbst.
    pub fn source_for(&self, source: &str) -> String {
        if !self.enabled() || source.is_empty() {
            self.stop();
            return source.to_string();
        }
        match self.start(source) {
            Ok(n) => n,
            Err(e) => {
                log::warn!("{}", tr!("Noise suppression not started: {error}", error = e));
                source.to_string()
            }
        }
    }

    pub fn stop(&self) {
        let st = self.state();
        if st.is_empty() {
            return;
        }
        if self.running() {
            procs::terminate(st.get("pid").and_then(Value::as_i64).unwrap_or(0));
        }
        self.save(json!({}));
    }

    /// Stärke ohne Neustart setzen (nur RNNoise, über pw-cli); false, wenn das nicht geht.
    pub fn set_live(&self, strength: i64) -> bool {
        let st = self.state();
        if st.get("backend").and_then(Value::as_str) != Some("rnnoise") || !self.running() {
            return false;
        }
        let Some(nid) = effects::pw_node_id(&format!("{NODE}_in")) else { return false };
        let control = st.get("control").and_then(Value::as_str).filter(|c| !c.is_empty()).unwrap_or(RNNOISE_VAD);
        effects::pw_set_props(nid, &format!("{{ params = [ \"ns:{control}\" {} ] }}", g(strength as f64)))
    }
}
