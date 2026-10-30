//! Elgato-Wave-Mikrofone (Wave:1, Wave:3, Wave Neo, Wave XLR) unter Linux – wie `freestream/wavemic.py`.
//!
//!   * Gain und Stummschaltung: über die Standard-USB-Audio-Steuerung (pactl).
//!   * Low Cut (80/120 Hz) und Clipguard: FreeWave bildet sie mit einer PipeWire-Filterkette nach
//!     (Hochpass + Limiter) und stellt das Ergebnis als Mikrofon „FreeWave Mikrofon“ bereit. Clipguard
//!     braucht den LADSPA-Limiter aus dem Paket swh-plugins.
//!   * Wave:3: Gain, Stumm, Low Cut, Clipguard, LEDs aus/umgekehrt, Kopfhörer und Mithören werden direkt
//!     im Mikrofon geschaltet ([`crate::wave3hw`]) – dann ist keine Software-Filterkette nötig.
//!   * Andere Wave-Modelle: Low Cut/Clipguard per Software, LED-Steuerung nicht möglich.
//!
//! Einstellungen (config.json, optional): `"wave_mic": {"lowcut": false, "lowcut_freq": 80,
//! "clipguard": false, "set_default": true}`; FreeWave merkt sich Änderungen in `wavemic.json`.

use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::Arc;
use std::time::{Duration, Instant};

use freestream_core::tr;
use indexmap::IndexMap;
use serde_json::{json, Map, Value};

use crate::host::running_instance;
use crate::mixer::truthy;
use crate::pactl::{self, english_env, which, Pactl};
use crate::procs;
use crate::wave3hw::{self, FieldValue, Wave3};
use crate::{Error, Result};

/// Statusdatei des Programms FreeWave (Pid, mit Fenster?).
pub const WAVE_APP_FILE: &str = "freewave.json";
pub const PROCESSED: &str = "linewave_micfilter";
pub const LIMITER_PLUGIN: &str = "fast_lookahead_limiter_1913";
pub const LADSPA_DIRS: [&str; 5] = ["/usr/lib/ladspa", "/usr/lib/x86_64-linux-gnu/ladspa",
    "/usr/lib/aarch64-linux-gnu/ladspa", "/usr/local/lib/ladspa", "/usr/lib64/ladspa"];

/// pactl-Name des angeschlossenen Wave-Mikrofons (oder None).
pub fn find_source(p: &dyn Pactl) -> Option<String> {
    for b in pactl::blocks(p, "sources").iter() {
        let name = b.name();
        let text = [name, b.description(), b.prop("device.vendor.name"), b.prop("device.product.name")].join(" ")
            .to_lowercase();
        if name.ends_with(".monitor") || name.starts_with("linewave_") || name.starts_with("lindeck_") {
            continue;
        }
        if text.contains("wave")
            && (text.contains("elgato") || text.contains("0fd9") || text.contains("wave:") || text.contains("wave_"))
        {
            return Some(name.to_string());
        }
    }
    None
}

pub fn limiter_available() -> bool {
    LADSPA_DIRS.iter().any(|d| Path::new(d).join(format!("{LIMITER_PLUGIN}.so")).exists())
}

/// PipeWire-Konfiguration für `pipewire -c` (Filterkette Hochpass → Limiter).
pub fn filter_conf(source: &str, lowcut: bool, freq: i64, clipguard: bool) -> Result<String> {
    let mut nodes: Vec<String> = Vec::new();
    let mut chain: Vec<(&str, &str)> = Vec::new();
    if lowcut {
        nodes.push(format!("{{ type = builtin name = lowcut label = bq_highpass control = {{ \"Freq\" = {:.1} \"Q\" = 0.707 }} }}",
                           freq as f64));
        chain.push(("lowcut:In", "lowcut:Out"));
    }
    if clipguard {
        nodes.push(format!("{{ type = ladspa name = limiter plugin = {LIMITER_PLUGIN} label = fastLookaheadLimiter \
                            control = {{ \"Input gain (dB)\" = 0.0 \"Limit (dB)\" = -1.0 \"Release time (s)\" = 0.08 }} }}"));
        chain.push(("limiter:Input 1", "limiter:Output 1"));
    }
    if nodes.is_empty() {
        return Err(Error(tr!("Neither low cut nor Clipguard is active")));
    }
    let desc = format!("\"{}\"", tr!("FreeWave Microphone").replace('"', "'"));
    let links: Vec<String> = chain.windows(2).map(|w| format!("{{ output = \"{}\" input = \"{}\" }}", w[0].1, w[1].0))
        .collect();
    Ok(format!(
        "# Von FreeWave erzeugt – Wave-Mikrofon mit Low Cut/Clipguard
{head}    {{ name = libpipewire-module-filter-chain
        args = {{
            node.description = {desc}
            media.name       = {desc}
            filter.graph = {{
                nodes = [
                    {nodes}
                ]
                links = [ {links} ]
                inputs  = [ \"{first}\" ]
                outputs = [ \"{last}\" ]
            }}
            audio.channels = 1
            audio.position = [ MONO ]
            capture.props = {{
                node.name      = \"linewave_micfilter_capture\"
                node.passive   = true
                target.object  = \"{source}\"
                stream.dont-remix = true
            }}
            playback.props = {{
                node.name        = \"{PROCESSED}\"
                node.description = {desc}
                media.class      = Audio/Source
            }}
        }}
    }}
]
",
        head = crate::effects::head(),
        nodes = nodes.join("\n                    "),
        links = links.join(" "),
        first = chain[0].0,
        last = chain[chain.len() - 1].1
    ))
}

// ------------------------------------------------------------------ Gerätelautstärke über pactl
/// (Prozent, stumm) einer Quelle (wie `system.device_volume`).
pub fn device_volume(kind: &str, name: &str) -> Option<(i64, bool)> {
    let out = |args: &[&str]| {
        let mut c = Command::new("pactl");
        c.args(args);
        english_env(&mut c);
        pactl::run_timeout(&mut c, Duration::from_secs(2)).map(|o| o.stdout).unwrap_or_default()
    };
    let vol = out(&[&format!("get-{kind}-volume"), name]);
    let mute = out(&[&format!("get-{kind}-mute"), name]).contains("yes");
    let re = regex::Regex::new(r"(\d+)%").ok()?;
    re.captures(&vol).and_then(|c| c[1].parse().ok()).map(|v| (v, mute))
}

fn device_set(kind: &str, name: &str, pct: i64) {
    let pct = pct.clamp(0, 100);
    let _ = procs::run_quiet(&["pactl", &format!("set-{kind}-volume"), name, &format!("{pct}%")], 5.0);
}

fn device_mute(kind: &str, name: &str, mode: &str) {
    let _ = procs::run_quiet(&["pactl", &format!("set-{kind}-mute"), name, mode], 5.0);
}

/// Einstellungen des Wave-Mikrofons.
#[derive(Debug, Clone, PartialEq)]
pub struct MicSettings {
    pub lowcut: bool,
    pub lowcut_freq: i64,
    pub clipguard: bool,
    pub set_default: bool,
}

/// Zustand für die Anzeige.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct MicStatus {
    pub source: Option<String>,
    pub hardware: bool,
    /// Felder des Wave:3 (gain, mute, lowcut …)
    pub fields: IndexMap<String, FieldValue>,
    pub api: String,
    pub error: String,
}

/// Wave-Mikrofon: Gain, Stumm, Low Cut, Clipguard (Wave:3 in Hardware, sonst per Filterkette).
pub struct WaveMic {
    /// true im Programm FreeWave: es besitzt die Software-Filterkette. Andere Prozesse bitten es per
    /// SIGHUP, die Einstellungen neu zu laden.
    pub owner: bool,
    pactl: Arc<dyn Pactl>,
    state_dir: PathBuf,
    conf: Map<String, Value>,
    pub settings: Map<String, Value>,
    proc: Option<Child>,
    /// steigt bei jeder Änderung (für die Anzeige)
    pub version: u64,
    hw: Option<Wave3>,
    hw_error: String,
    /// Wave:3 öffnen (Tests ersetzen das)
    pub hw_open: fn() -> std::result::Result<Wave3, wave3hw::Wave3Error>,
    pub hw_present: fn() -> bool,
}

impl WaveMic {
    /// `wave_mic`: Abschnitt "wave_mic" aus config.json.
    pub fn new(wave_mic: Option<&Map<String, Value>>, pactl: Arc<dyn Pactl>, state_dir: PathBuf) -> Self {
        let mut me = Self {
            owner: false,
            pactl,
            state_dir,
            conf: wave_mic.cloned().unwrap_or_default(),
            settings: Map::new(),
            proc: None,
            version: 0,
            hw: None,
            hw_error: String::new(),
            hw_open: Wave3::open,
            hw_present: wave3hw::present,
        };
        me.settings = me.load();
        me
    }

    fn state_file(&self) -> PathBuf {
        self.state_dir.join("wavemic.json")
    }

    fn conf_file(&self) -> PathBuf {
        self.state_dir.join("wave-filter.conf")
    }

    fn load(&self) -> Map<String, Value> {
        let mut base = json!({"lowcut": false, "lowcut_freq": 80, "clipguard": false, "set_default": true})
            .as_object().cloned().unwrap_or_default();
        for (k, v) in &self.conf {
            base.insert(k.clone(), v.clone());
        }
        for (k, v) in procs::read_json(&self.state_file()) {
            base.insert(k, v);
        }
        base
    }

    fn save(&self) {
        let keep: Map<String, Value> = ["lowcut", "lowcut_freq", "clipguard"].iter()
            .filter_map(|k| self.settings.get(*k).map(|v| (k.to_string(), v.clone()))).collect();
        let _ = procs::write_json(&self.state_file(), &Value::Object(keep));
    }

    /// Einstellungen als Typ.
    pub fn mic_settings(&self) -> MicSettings {
        let b = |k: &str, d: bool| self.settings.get(k).map(truthy).unwrap_or(d);
        MicSettings {
            lowcut: b("lowcut", false),
            lowcut_freq: self.settings.get("lowcut_freq").and_then(Value::as_f64).map(|f| f as i64).filter(|f| *f != 0)
                .unwrap_or(80),
            clipguard: b("clipguard", false),
            set_default: b("set_default", true),
        }
    }

    // ------------------------------------------------ Wave:3-Hardware
    /// Wave:3, wenn eines angeschlossen und erreichbar ist.
    pub fn hw(&mut self) -> Option<&Wave3> {
        if self.hw.is_none() && (self.hw_present)() {
            match (self.hw_open)() {
                Ok(h) => {
                    self.hw = Some(h);
                    self.hw_error.clear();
                }
                Err(e) => {
                    if e.0 != self.hw_error {
                        log::warn!("Wave:3: {e}");
                    }
                    self.hw_error = e.0;
                }
            }
        }
        self.hw.as_ref()
    }

    fn hw_do<T>(&mut self, f: impl FnOnce(&Wave3) -> std::result::Result<T, wave3hw::Wave3Error>) -> Result<T> {
        let r = match self.hw.as_ref() {
            Some(h) => f(h),
            None => Err(wave3hw::Wave3Error(tr!("Only possible with an Elgato Wave:3"))),
        };
        self.version += 1;
        r.map_err(|e| {
            if let Some(h) = self.hw.take() {
                h.close();
            }
            Error(e.0)
        })
    }

    fn hw_get(&mut self) -> Option<IndexMap<String, FieldValue>> {
        self.hw()?;
        match self.hw.as_ref()?.get() {
            Ok(v) => Some(v),
            Err(_) => {
                self.hw = None;
                None
            }
        }
    }

    /// Low Cut, Clipguard oder LED-Schalter: aktueller Zustand (None = unbekannt/nicht möglich).
    pub fn is_on(&mut self, key: &str) -> Option<bool> {
        if self.hw().is_some() {
            return self.hw_get().and_then(|v| v.get(key).map(|f| f.as_bool()));
        }
        if key == "leds_off" || key == "leds_flip" {
            return None;
        }
        Some(self.settings.get(key).map(truthy).unwrap_or(false))
    }

    pub fn hw_set(&mut self, key: &str, value: f64) -> Result<FieldValue> {
        if self.hw().is_none() {
            return Err(Error(if self.hw_error.is_empty() {
                tr!("Only possible with an Elgato Wave:3")
            } else {
                tr!("Only possible with an Elgato Wave:3 ({error})", error = self.hw_error)
            }));
        }
        self.hw_do(|h| h.set(key, value))
    }

    pub fn status(&mut self) -> MicStatus {
        let mut st = MicStatus { source: find_source(&*self.pactl), ..Default::default() };
        if self.hw().is_some() {
            let h = self.hw.as_ref().unwrap();
            match h.get() {
                Ok(v) => {
                    st.fields = v;
                    st.hardware = true;
                    st.api = format!("{}.{}", h.api.0, h.api.1);
                }
                Err(e) => st.error = e.0,
            }
        }
        st
    }

    // ------------------------------------------------------- Gain / Mute
    pub fn source(&self) -> Result<String> {
        find_source(&*self.pactl).ok_or_else(|| Error(tr!("No Elgato Wave microphone found")))
    }

    /// (Prozent, stumm). Beim Wave:3 ist 100 % = 40 dB Hardware-Gain.
    pub fn gain(&mut self) -> Result<(i64, bool)> {
        if let Some(c) = self.hw_get() {
            let gain = c.get("gain").map(|v| v.as_f64()).unwrap_or(0.0);
            return Ok((crate::mixer::py_round(gain * 2.5), c.get("mute").is_some_and(|m| m.as_bool())));
        }
        let src = self.source()?;
        device_volume("source", &src).ok_or_else(|| Error(tr!("pactl error")))
    }

    pub fn change_gain(&mut self, delta: f64) -> Result<()> {
        if self.hw().is_some() {
            return self.hw_do(|h| h.change("gain", delta * 0.4)).map(|_| ());
        }
        let src = self.source()?;
        let (cur, _) = device_volume("source", &src).unwrap_or((0, false));
        device_set("source", &src, cur + delta as i64);
        self.version += 1;
        Ok(())
    }

    pub fn set_gain(&mut self, pct: f64) -> Result<()> {
        if self.hw().is_some() {
            return self.hw_do(|h| h.set("gain", pct * 0.4)).map(|_| ());
        }
        device_set("source", &self.source()?, pct as i64);
        self.version += 1;
        Ok(())
    }

    /// Mikrofon stumm (Some(true)), an (Some(false)) oder umschalten (None). Gibt den neuen Zustand
    /// zurück. Ohne Wave-Mikrofon wird das Standardmikrofon des Systems geschaltet (für Tastenkürzel).
    pub fn set_mute(&mut self, mode: Option<bool>) -> Result<Option<bool>> {
        if self.hw().is_some() {
            let v = self.hw_do(|h| match mode {
                None => h.toggle("mute"),
                Some(m) => h.set("mute", if m { 1.0 } else { 0.0 }),
            })?;
            return Ok(Some(v.as_bool()));
        }
        let src = find_source(&*self.pactl).unwrap_or_else(|| "@DEFAULT_SOURCE@".into());
        device_mute("source", &src, match mode {
            None => "toggle",
            Some(true) => "1",
            Some(false) => "0",
        });
        self.version += 1;
        Ok(device_volume("source", &src).map(|v| v.1))
    }

    // ------------------------------------------------ Low Cut / Clipguard
    /// Einstellungen ändern, merken und anwenden.
    pub fn set(&mut self, values: &[(&str, Value)]) -> Result<bool> {
        for (k, v) in values {
            self.settings.insert(k.to_string(), v.clone());
        }
        self.save();
        let r = self.apply();
        self.version += 1;
        r
    }

    /// Schalter umlegen (lowcut, clipguard, leds_off, leds_flip). Gibt den neuen Zustand zurück.
    pub fn toggle(&mut self, key: &str) -> Result<bool> {
        if self.hw().is_some() {
            let new = self.hw_do(|h| h.toggle(key))?.as_bool();
            if key == "lowcut" || key == "clipguard" {
                self.settings.insert(key.into(), json!(new));
                self.save();
            }
            return Ok(new);
        }
        if key == "leds_off" || key == "leds_flip" {
            return Err(Error(tr!("LED control is only available for the Elgato Wave:3")));
        }
        let cur = self.settings.get(key).map(truthy).unwrap_or(false);
        self.set(&[(key, json!(!cur))])?;
        Ok(!cur)
    }

    pub fn running(&mut self) -> bool {
        match self.proc.as_mut() {
            Some(p) => matches!(p.try_wait(), Ok(None)) && procs::alive(i64::from(p.id())),
            None => false,
        }
    }

    /// Filterkette passend zu den Einstellungen starten, neu starten oder beenden.
    pub fn apply(&mut self) -> Result<bool> {
        self.stop();
        if self.hw().is_some() {
            // Wave:3: Low Cut und Clipguard schaltet das Mikrofon selbst
            for key in ["lowcut", "clipguard"] {
                let want = self.settings.get(key).map(truthy).unwrap_or(false);
                let h = self.hw.as_ref().unwrap();
                let r = h.get().and_then(|v| {
                    if v.get(key).map(|f| f.as_bool()) != Some(want) {
                        h.set(key, if want { 1.0 } else { 0.0 }).map(|_| ())
                    } else {
                        Ok(())
                    }
                });
                if let Err(e) = r {
                    log::warn!("Wave:3 {key}: {e}");
                }
            }
            return Ok(false);
        }
        if !self.owner && self.delegate() {
            return Ok(false);
        }
        let s = self.mic_settings();
        let mut clip = s.clipguard;
        if clip && !limiter_available() {
            log::warn!("{}", tr!("Clipguard needs the LADSPA limiter (package swh-plugins) – skipped"));
            clip = false;
        }
        if !(s.lowcut || clip) {
            return Ok(false);
        }
        let Some(src) = find_source(&*self.pactl) else {
            log::info!("{}", tr!("Wave microphone not connected – the filter starts when it is plugged in"));
            return Ok(false);
        };
        let exe = which("pipewire").ok_or_else(|| Error(tr!("pipewire not found")))?;
        let _ = std::fs::create_dir_all(&self.state_dir);
        std::fs::write(self.conf_file(), filter_conf(&src, s.lowcut, s.lowcut_freq, clip)?)
            .map_err(|e| Error(e.to_string()))?;
        let conf = self.conf_file().to_string_lossy().into_owned();
        self.proc = Some(procs::spawn_detached(Command::new(exe).args(["-c", &conf]), false)
            .map_err(|e| Error(e.to_string()))?);
        log::info!("{}", tr!("Wave filter active (low cut: {lowcut}, Clipguard: {clipguard})",
                             lowcut = py_bool(s.lowcut), clipguard = py_bool(clip)));
        if s.set_default {
            std::thread::sleep(Duration::from_millis(500));
            let _ = procs::run_quiet(&["pactl", "set-default-source", PROCESSED], 5.0);
        }
        Ok(true)
    }

    /// Läuft „FreeWave“, übernimmt es die Filterkette (Einstellungen liegen in wavemic.json).
    fn delegate(&self) -> bool {
        match running_instance(&self.state_dir, WAVE_APP_FILE) {
            Some(pid) => unsafe { libc::kill(pid as libc::pid_t, libc::SIGHUP) == 0 },
            None => false,
        }
    }

    /// Einstellungen neu lesen und anwenden (z. B. nach Änderung durch FreeDeck).
    pub fn reload(&mut self, wave_mic: Option<&Map<String, Value>>) -> Result<bool> {
        self.conf = wave_mic.cloned().unwrap_or_default();
        self.settings = self.load();
        self.version += 1;
        self.apply()
    }

    pub fn stop(&mut self) {
        if let Some(mut p) = self.proc.take() {
            let pid = i64::from(p.id());
            if matches!(p.try_wait(), Ok(None)) && procs::alive(pid) {
                procs::terminate(pid);
                let end = Instant::now() + Duration::from_secs(3);
                while procs::alive(pid) && matches!(p.try_wait(), Ok(None)) && Instant::now() < end {
                    std::thread::sleep(Duration::from_millis(20));
                }
                if procs::alive(pid) && matches!(p.try_wait(), Ok(None)) {
                    let _ = p.kill();
                }
            }
        }
    }
}

/// True/False wie Python in Meldungen.
fn py_bool(b: bool) -> &'static str {
    if b {
        "True"
    } else {
        "False"
    }
}

/// Dienst des Hosts: Filterkette beim Start und beim Ein-/Ausstecken (alle 10 s geprüft).
pub struct WaveMicService {
    present: Option<bool>,
    last: Option<Instant>,
}

impl Default for WaveMicService {
    fn default() -> Self {
        Self::new()
    }
}

impl WaveMicService {
    pub fn new() -> Self {
        Self { present: None, last: None }
    }

    pub fn start(&mut self, mic: &mut WaveMic) -> Result<()> {
        let s = mic.mic_settings();
        if !(s.lowcut || s.clipguard) {
            return Ok(());
        }
        let present = find_source(&*mic.pactl).is_some();
        self.present = Some(present);
        if present {
            mic.apply()?;
        }
        Ok(())
    }

    pub fn tick(&mut self, mic: &mut WaveMic) -> Result<()> {
        if self.last.is_some_and(|l| l.elapsed() < Duration::from_secs(10)) {
            return Ok(());
        }
        self.last = Some(Instant::now());
        let s = mic.mic_settings();
        let wants = s.lowcut || s.clipguard;
        if !wants && !mic.running() {
            return Ok(());
        }
        if (mic.hw_present)() {
            if mic.running() {
                mic.stop(); // Wave:3 filtert selbst
            }
            return Ok(());
        }
        let present = find_source(&*mic.pactl).is_some();
        if present && wants && (!mic.running() || self.present != Some(true)) {
            mic.apply()?;
        } else if !present && self.present == Some(true) {
            mic.stop();
        }
        self.present = Some(present);
        Ok(())
    }

    pub fn stop(&mut self, mic: &mut WaveMic) {
        mic.stop();
    }
}
