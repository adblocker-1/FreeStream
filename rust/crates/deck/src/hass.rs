//! Anbindung an Home Assistant über die REST-API – wie `freestream/hass.py`.
//!
//! Verbindung in config.json:
//! `"homeassistant": {"url": "http://homeassistant.local:8123", "token": "<Long-Lived Access Token>", "verify_ssl": true}`
//! (der Token auch im Schlüsselbund, siehe [`freestream_core::secrets`]). HTTPS prüft gegen die
//! Zertifikate des Systems.
//!
//! Ein gemeinsamer Client je Verbindung hält einen Zwischenspeicher der Zustände. Ein Hintergrund-Thread
//! fragt nur die Entitäten ab, die gerade auf Tasten zu sehen sind (mit Mindestabstand und Backoff bei
//! Fehlern). Dienstaufrufe laufen in einem eigenen Arbeits-Thread, damit das Zeichnen nie blockiert.
//! Der Token wird nie protokolliert.

use freestream_core::{secrets, tr};
use serde_json::{json, Map, Value};
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crate::native::Worker;
use crate::net::{self, Version};

pub const CONFIG_KEY: &str = "homeassistant";
/// Sekunden zwischen zwei Abfragen
const POLL_INTERVAL: Duration = Duration::from_secs(2);
/// Mindestabstand zwischen Abfragen (auch nach Tastendruck)
const MIN_GAP: Duration = Duration::from_millis(400);
/// so lange bleibt eine Entität ohne Zugriff in der Abfrage
const WATCH_TTL: Duration = Duration::from_secs(15);
/// so lange gilt ein vorläufig gesetzter Zustand
const HOLD: Duration = Duration::from_millis(1500);
/// bis zu so vielen Entitäten einzeln abfragen, sonst /api/states
const MAX_SINGLE: usize = 8;

/// Fehlerarten
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    Config,
    Auth,
    Offline,
    NotFound,
    Other,
}

#[derive(Debug, Clone, PartialEq)]
pub struct HaError {
    pub message: String,
    pub kind: ErrorKind,
}

impl HaError {
    pub fn new(message: String, kind: ErrorKind) -> Self {
        Self { message, kind }
    }
}

impl std::fmt::Display for HaError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

// --------------------------------------------------------------------------------- Hilfsfunktionen
/// „homeassistant.local:8123/“ -> „http://homeassistant.local:8123“.
pub fn normalize_url(url: &str) -> String {
    let mut url = url.trim().trim_end_matches('/').to_string();
    if url.is_empty() {
        return url;
    }
    if !url.contains("://") {
        url = format!("http://{url}");
    }
    if let Some(u) = url.strip_suffix("/api") {
        url = u.to_string();
    }
    url
}

pub fn domain_of(entity_id: &str) -> &str {
    entity_id.split('.').next().unwrap_or("")
}

/// Anzeigename: friendly_name, sonst der Teil nach dem Punkt mit Leerzeichen.
pub fn friendly_name(st: Option<&Value>, entity_id: &str) -> String {
    if let Some(n) = st.and_then(|s| s.get("attributes")).and_then(|a| a.get("friendly_name")).filter(|n| !n.is_null()) {
        let n = n.as_str().map(str::to_string).unwrap_or_else(|| n.to_string());
        if !n.is_empty() {
            return n;
        }
    }
    let rest = entity_id.split_once('.').map(|(_, r)| r).unwrap_or("").replace('_', " ");
    if rest.is_empty() { entity_id.to_string() } else { rest }
}

/// Wert wie Pythons `str()` (Texte ohne Anführungszeichen, True/False).
fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Bool(b) => if *b { "True" } else { "False" }.into(),
        Value::Null => "None".into(),
        other => other.to_string(),
    }
}

/// Zustand (oder Attribut) mit Einheit als Text, z. B. „21.5 °C“.
pub fn format_state(st: Option<&Value>, attribute: &str, decimals: Option<usize>) -> String {
    let Some(st) = st else { return String::new() };
    let attrs = st.get("attributes").cloned().unwrap_or(Value::Null);
    let val = if attribute.is_empty() { st.get("state") } else { attrs.get(attribute) };
    let unit = if attribute.is_empty() { attrs.get("unit_of_measurement").and_then(Value::as_str).unwrap_or("") } else { "" };
    let Some(val) = val.filter(|v| !v.is_null()) else { return "–".into() };
    let mut text = py_str(val);
    if !val.is_boolean() {
        let num = match val {
            Value::Number(n) => n.as_f64(),
            Value::String(s) => s.trim().parse::<f64>().ok().filter(|f| f.is_finite()),
            _ => None,
        };
        if let Some(num) = num {
            if let Some(d) = decimals {
                text = format!("{num:.d$}");
            } else if val.as_f64().is_some() && !val.is_i64() && !val.is_u64() || text.contains('.') {
                text = if num.abs() < 1000.0 {
                    let t = format!("{num:.1}");
                    t.trim_end_matches('0').trim_end_matches('.').to_string()
                } else {
                    format!("{num:.0}")
                };
            }
        }
    }
    match text.as_str() {
        "unavailable" => return tr!("unavailable"),
        "unknown" => return tr!("unknown"),
        _ => {}
    }
    if unit.is_empty() { text } else { format!("{text} {unit}").trim().to_string() }
}

const ON_STATES: &[&str] = &["on", "open", "opening", "unlocked", "playing", "home", "active", "heat", "cool",
                              "heat_cool", "auto", "dry", "fan_only"];
const OFF_STATES: &[&str] = &["off", "closed", "closing", "locked", "idle", "paused", "standby", "not_home", "stopped"];

/// An/Aus für schaltbare Entitäten, None wenn unbekannt.
pub fn is_on(st: Option<&Value>) -> Option<bool> {
    let s = py_str(st?.get("state").unwrap_or(&Value::String(String::new()))).to_lowercase();
    if matches!(s.as_str(), "unavailable" | "unknown" | "" | "none") {
        return None;
    }
    if ON_STATES.contains(&s.as_str()) {
        return Some(true);
    }
    OFF_STATES.contains(&s.as_str()).then_some(false)
}

/// „light.turn_on“ -> („light“, „turn_on“).
pub fn parse_service(text: &str) -> Result<(String, String), HaError> {
    match text.trim().split_once('.') {
        Some((d, s)) if !d.trim().is_empty() && !s.trim().is_empty() => Ok((d.trim().to_string(), s.trim().to_string())),
        _ => Err(HaError::new(tr!("Service must look like domain.service (e.g. light.turn_on)"), ErrorKind::Other)),
    }
}

/// Zusätzliche Daten als JSON-Objekt (leer = {}).
pub fn parse_data(v: Option<&Value>) -> Result<Map<String, Value>, HaError> {
    let text = match v {
        None | Some(Value::Null) => return Ok(Map::new()),
        Some(Value::Object(o)) => return Ok(o.clone()),
        Some(Value::String(s)) => s.trim(),
        Some(_) => return Err(HaError::new(tr!("A JSON object { … } is expected"), ErrorKind::Other)),
    };
    if text.is_empty() {
        return Ok(Map::new());
    }
    match serde_json::from_str::<Value>(text) {
        Ok(Value::Object(o)) => Ok(o),
        Ok(_) => Err(HaError::new(tr!("A JSON object { … } is expected"), ErrorKind::Other)),
        Err(e) => Err(HaError::new(tr!("Invalid JSON in the service data: {error}", error = e), ErrorKind::Other)),
    }
}

/// Pfad eines Dienstaufrufs: `/api/services/<domain>/<service>`.
pub fn service_path(domain: &str, service: &str) -> String {
    format!("/api/services/{}/{}", net::quote(domain), net::quote(service))
}

/// Pfad des Zustands einer Entität: `/api/states/<entity_id>`.
pub fn state_path(entity_id: &str) -> String {
    format!("/api/states/{}", net::quote(entity_id))
}

/// Dienstaufruf (Domäne, Dienst, Daten).
pub type Call = (String, String, Value);

fn call(d: &str, s: &str, data: Value) -> Call {
    (d.to_string(), s.to_string(), data)
}

/// Passender Dienst zum Umschalten einer Entität (nach ihrem letzten bekannten Zustand).
pub fn toggle_call(entity_id: &str, st: Option<&Value>) -> Call {
    let dom = domain_of(entity_id);
    let data = json!({"entity_id": entity_id});
    match dom {
        "scene" | "script" => call(dom, "turn_on", data),
        "button" | "input_button" => call(dom, "press", data),
        "lock" => {
            let locked = st.and_then(|s| s.get("state")).and_then(Value::as_str) == Some("locked");
            call("lock", if locked { "unlock" } else { "lock" }, data)
        }
        "media_player" => call(dom, "media_play_pause", data),
        "climate" => call(dom, if is_on(st) == Some(true) { "turn_off" } else { "turn_on" }, data),
        "cover" | "light" | "switch" | "fan" | "input_boolean" | "automation" | "humidifier" | "siren" | "remote"
        | "vacuum" | "valve" => call(dom, "toggle", data),
        _ => call("homeassistant", "toggle", data),
    }
}

/// Zahl aus einem Attribut (auch als Text).
fn num(v: Option<&Value>) -> Option<f64> {
    v.and_then(|v| v.as_f64().or_else(|| v.as_str()?.trim().parse().ok()))
}

/// Vorläufige Anzeige und Dienstaufruf für eine Drehung um `ticks` (Helligkeit, Temperatur, Lautstärke …).
/// Ergebnis: (neuer Zustand oder None, geänderte Attribute, Aufruf ohne entity_id).
pub fn dial_call(entity_id: &str, st: Option<&Value>, step: i64, ticks: i64) -> Option<(Option<String>, Map<String, Value>, Call)> {
    let empty = json!({});
    let st = st.unwrap_or(&empty);
    let attrs = st.get("attributes").cloned().unwrap_or_else(|| json!({}));
    let a = |k: &str| num(attrs.get(k));
    let dom = domain_of(entity_id);
    let on = is_on(Some(st)) == Some(true);
    let mut local = Map::new();
    let pct = |cur: f64| (cur + (step * ticks) as f64).clamp(0.0, 100.0).round() as i64;
    let state_on = |new: i64, on: &str, off: &str| Some(if new != 0 { on } else { off }.to_string());
    Some(match dom {
        "light" => {
            let cur = if on { (a("brightness").unwrap_or(0.0) * 100.0 / 255.0).round() } else { 0.0 };
            let new = pct(cur);
            local.insert("brightness".into(), json!((new as f64 * 255.0 / 100.0).round() as i64));
            let c = if new != 0 { call("light", "turn_on", json!({"brightness_pct": new})) } else { call("light", "turn_off", json!({})) };
            (state_on(new, "on", "off"), local, c)
        }
        "climate" => {
            let cur = a("temperature").filter(|t| *t != 0.0).or_else(|| a("current_temperature")).unwrap_or(20.0);
            let tstep = a("target_temp_step").filter(|t| *t != 0.0).unwrap_or(0.5);
            let new = ((cur + tstep * ticks as f64) * 10.0).round() / 10.0;
            let new = new.min(a("max_temp").unwrap_or(100.0)).max(a("min_temp").unwrap_or(-50.0));
            local.insert("temperature".into(), json!(new));
            (None, local, call("climate", "set_temperature", json!({"temperature": new})))
        }
        "fan" => {
            let new = pct(a("percentage").unwrap_or(0.0));
            local.insert("percentage".into(), json!(new));
            (state_on(new, "on", "off"), local, call("fan", "set_percentage", json!({"percentage": new})))
        }
        "cover" => {
            let new = pct(a("current_position").unwrap_or(0.0));
            local.insert("current_position".into(), json!(new));
            (state_on(new, "open", "closed"), local, call("cover", "set_cover_position", json!({"position": new})))
        }
        "media_player" => {
            let v = (a("volume_level").unwrap_or(0.0) + step as f64 / 100.0 * ticks as f64).clamp(0.0, 1.0);
            let new = (v * 100.0).round() / 100.0;
            local.insert("volume_level".into(), json!(new));
            (None, local, call("media_player", "volume_set", json!({"volume_level": new})))
        }
        "input_number" | "number" => {
            let nstep = a("step").filter(|s| *s != 0.0).unwrap_or(1.0);
            let cur = num(st.get("state")).unwrap_or_else(|| a("min").unwrap_or(0.0));
            let new = (cur + nstep * ticks as f64).min(a("max").unwrap_or(cur + 1e9)).max(a("min").unwrap_or(cur - 1e9));
            (Some(format_float(new)), local, call(dom, "set_value", json!({"value": new})))
        }
        _ => return None,
    })
}

/// Zahl wie Pythons `str(float)` („21.0“, „0.5“).
fn format_float(v: f64) -> String {
    if v.fract() == 0.0 && v.abs() < 1e16 { format!("{v:.1}") } else { v.to_string() }
}

// ------------------------------------------------------------------------------------------ Client
/// Verbindungseinstellung.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Settings {
    pub url: String,
    /// Token aus der Datei (leer: aus dem Schlüsselbund, wenn `in_keyring`)
    pub token: String,
    pub in_keyring: bool,
    pub verify_ssl: bool,
}

impl Settings {
    pub fn from_cfg(cfg: &Map<String, Value>) -> Self {
        let c = cfg.get(CONFIG_KEY).and_then(Value::as_object).cloned().unwrap_or_default();
        Self {
            url: normalize_url(c.get("url").and_then(Value::as_str).unwrap_or("")),
            token: secrets::in_file(cfg, CONFIG_KEY, "token").map(|t| t.trim().to_string()).unwrap_or_default(),
            in_keyring: secrets::listed(cfg, CONFIG_KEY, "token"),
            verify_ssl: c.get("verify_ssl") != Some(&Value::Bool(false)),
        }
    }

    pub fn configured(&self) -> bool {
        !self.url.is_empty() && (!self.token.is_empty() || self.in_keyring)
    }
}

type States = HashMap<String, Option<Value>>;

pub struct Client {
    pub settings: Settings,
    /// aufgelöster Token (aus Datei oder Schlüsselbund); None = noch nicht nachgesehen
    token: Mutex<Option<String>>,
    agent: ureq::Agent,
    error: Mutex<Option<HaError>>,
    version: Version,
    states: Mutex<States>,
    watch: Mutex<HashMap<String, Instant>>,
    hold: Mutex<HashMap<String, Instant>>,
    running: AtomicBool,
    poller: AtomicBool,
    wake_tx: Mutex<Sender<()>>,
    wake_rx: Mutex<Option<Receiver<()>>>,
    pub worker: Worker,
}

impl Client {
    pub fn new(settings: Settings, timeout: Duration) -> Arc<Client> {
        let (tx, rx) = mpsc::channel();
        let agent = net::agent(timeout, settings.verify_ssl);
        let c = Client {
            token: Mutex::new((!settings.token.is_empty()).then(|| settings.token.clone())),
            settings,
            agent,
            error: Mutex::new(None),
            version: Version::default(),
            states: Mutex::new(HashMap::new()),
            watch: Mutex::new(HashMap::new()),
            hold: Mutex::new(HashMap::new()),
            running: AtomicBool::new(true),
            poller: AtomicBool::new(false),
            wake_tx: Mutex::new(tx),
            wake_rx: Mutex::new(Some(rx)),
            worker: Worker::new("homeassistant-calls"),
        };
        if !c.settings.configured() {
            *c.error.lock().unwrap() = Some(HaError::new(tr!("Home Assistant is not set up yet"), ErrorKind::Config));
        }
        Arc::new(c)
    }

    pub fn configured(&self) -> bool {
        self.settings.configured()
    }

    pub fn version(&self) -> u64 {
        self.version.get()
    }

    pub fn error(&self) -> Option<HaError> {
        self.error.lock().unwrap().clone()
    }

    pub fn error_kind(&self) -> Option<ErrorKind> {
        self.error.lock().unwrap().as_ref().map(|e| e.kind)
    }

    fn close(&self) {
        self.running.store(false, Ordering::Relaxed);
        self.refresh();
    }

    // ------------------------------------------------------------------------------------- HTTP
    fn token(&self) -> Option<String> {
        let mut t = self.token.lock().unwrap();
        if t.is_none() && self.settings.in_keyring {
            *t = secrets::lookup("homeassistant.token");
        }
        t.clone()
    }

    /// Eine Anfrage an die REST-API (blockiert); Fehler als [`HaError`] mit verständlicher Meldung.
    pub fn request(&self, method: &str, path: &str, body: Option<&Value>) -> Result<Option<Value>, HaError> {
        let not_set_up = || HaError::new(tr!("Home Assistant is not set up yet"), ErrorKind::Config);
        if !self.configured() {
            return Err(not_set_up());
        }
        let token = self.token().ok_or_else(not_set_up)?;
        let url = format!("{}{path}", self.settings.url);
        let auth = format!("Bearer {token}");
        let res = if method == "POST" {
            let text = body.map(Value::to_string).unwrap_or_else(|| "{}".into());
            self.agent.post(&url).header("Authorization", &auth).header("Content-Type", "application/json").send(text.as_bytes())
        } else {
            self.agent.get(&url).header("Authorization", &auth).header("Content-Type", "application/json").call()
        };
        let offline = |e: &dyn std::fmt::Display| HaError::new(tr!("Home Assistant not reachable: {error}", error = e), ErrorKind::Offline);
        let resp = res.map_err(|e| offline(&e))?;
        let code = resp.status().as_u16();
        let raw = resp.into_body().read_to_string().map_err(|e| offline(&e))?;
        match code {
            200..=299 => {}
            401 | 403 => return Err(HaError::new(tr!("Access denied – check the access token"), ErrorKind::Auth)),
            404 => return Err(HaError::new(tr!("Not found: {path}", path = path), ErrorKind::NotFound)),
            _ => {
                let msg = serde_json::from_str::<Value>(&raw).ok()
                    .and_then(|v| v.get("message").and_then(Value::as_str).map(str::to_string))
                    .filter(|m| !m.is_empty())
                    .unwrap_or_else(|| reason(code).to_string());
                return Err(HaError::new(tr!("Home Assistant reports error {code}: {message}", code = code, message = msg), ErrorKind::Other));
            }
        }
        if raw.trim().is_empty() {
            return Ok(None);
        }
        serde_json::from_str(&raw).map(Some)
            .map_err(|_| HaError::new(tr!("Unexpected response from Home Assistant"), ErrorKind::Other))
    }

    // --------------------------------------------------------------------------------- Abfragen
    /// Version und Standortname (für „Verbindung testen“).
    pub fn info(&self) -> Result<(String, String), HaError> {
        self.request("GET", "/api/", None)?;
        let cfg = self.request("GET", "/api/config", None)?.unwrap_or(Value::Null);
        self.set_ok();
        let s = |k: &str| cfg.get(k).and_then(Value::as_str).unwrap_or("").to_string();
        Ok((s("version"), s("location_name")))
    }

    /// Alle Entitäten (für viele beobachtete Entitäten auf einmal).
    pub fn states_all(&self) -> Result<Vec<Value>, HaError> {
        let lst = self.request("GET", "/api/states", None)?.and_then(|v| v.as_array().cloned()).unwrap_or_default();
        let watched: Vec<String> = self.watch.lock().unwrap().keys().cloned().collect();
        for st in &lst {
            if let Some(id) = st.get("entity_id").and_then(Value::as_str) {
                if watched.iter().any(|w| w == id) {
                    self.store(id, Some(st.clone()));
                }
            }
        }
        self.set_ok();
        Ok(lst)
    }

    /// Zustand einer Entität direkt abfragen (blockiert).
    pub fn fetch(&self, entity_id: &str) -> Result<Option<Value>, HaError> {
        match self.request("GET", &state_path(entity_id), None) {
            Ok(st) => {
                self.store(entity_id, st.clone());
                Ok(st)
            }
            Err(e) if e.kind == ErrorKind::NotFound => {
                self.store(entity_id, None);
                Err(HaError::new(tr!("Unknown entity: {entity}", entity = entity_id), ErrorKind::NotFound))
            }
            Err(e) => Err(e),
        }
    }

    // ------------------------------------------------------------------------ Zwischenspeicher
    /// Entität in die Hintergrundabfrage aufnehmen (bei jedem Zugriff erneuern).
    pub fn watch(self: &Arc<Self>, entity_id: &str) {
        if entity_id.is_empty() {
            return;
        }
        let new = self.watch.lock().unwrap().insert(entity_id.to_string(), Instant::now()).is_none();
        if self.configured() && !self.poller.swap(true, Ordering::Relaxed) {
            if let Some(rx) = self.wake_rx.lock().unwrap().take() {
                let me = self.clone();
                std::thread::Builder::new().name("homeassistant".into()).spawn(move || me.poll_loop(rx)).ok();
            }
        }
        if new {
            self.refresh();
        }
    }

    /// Zustand aus dem Zwischenspeicher (nie blockierend).
    pub fn state(self: &Arc<Self>, entity_id: &str) -> Option<Value> {
        self.watch(entity_id);
        self.states.lock().unwrap().get(entity_id).cloned().flatten()
    }

    /// False, wenn Home Assistant die Entität nicht kennt.
    pub fn known(&self, entity_id: &str) -> bool {
        !matches!(self.states.lock().unwrap().get(entity_id), Some(None))
    }

    fn store(&self, entity_id: &str, st: Option<Value>) {
        if self.hold.lock().unwrap().get(entity_id).map(|t| Instant::now() < *t).unwrap_or(false) {
            return; // gerade selbst gesetzt (Dial/Taste): ältere Antworten nicht zurückspringen lassen
        }
        let mut states = self.states.lock().unwrap();
        let changed = match states.get(entity_id) {
            None => true,
            Some(old) => {
                let part = |v: &Option<Value>, k: &str| v.as_ref().and_then(|s| s.get(k).cloned());
                part(old, "state") != part(&st, "state") || part(old, "attributes") != part(&st, "attributes")
                    || old.is_none() != st.is_none()
            }
        };
        if changed {
            states.insert(entity_id.to_string(), st);
            self.version.bump();
        }
    }

    /// Zustand vorläufig selbst setzen (sofortige Anzeige nach Tastendruck).
    pub fn set_local(&self, entity_id: &str, state: Option<&str>, attrs: &Map<String, Value>) {
        let mut states = self.states.lock().unwrap();
        let mut st = states.get(entity_id).cloned().flatten()
            .unwrap_or_else(|| json!({"entity_id": entity_id, "attributes": {}}));
        if let Some(s) = state {
            st["state"] = Value::from(s);
        }
        if !attrs.is_empty() {
            let mut a = st.get("attributes").and_then(Value::as_object).cloned().unwrap_or_default();
            a.extend(attrs.clone());
            st["attributes"] = Value::Object(a);
        }
        states.insert(entity_id.to_string(), Some(st));
        self.hold.lock().unwrap().insert(entity_id.to_string(), Instant::now() + HOLD);
        self.version.bump();
    }

    fn set_error(&self, e: &HaError) {
        let mut err = self.error.lock().unwrap();
        if err.as_ref().map(|x| &x.message) != Some(&e.message) {
            if e.kind != ErrorKind::Config {
                log::info!("{}", tr!("Home Assistant: {error}", error = e));
            }
            self.version.bump();
        }
        *err = Some(e.clone());
    }

    fn set_ok(&self) {
        let mut err = self.error.lock().unwrap();
        if err.is_some() {
            *err = None;
            self.version.bump();
            log::info!("{}", tr!("Connected to Home Assistant ({url})", url = self.settings.url));
        }
    }

    /// Sofort neu abfragen (nach einem Dienstaufruf).
    pub fn refresh(&self) {
        let _ = self.wake_tx.lock().unwrap().send(());
    }

    fn active(&self) -> Vec<String> {
        let mut w = self.watch.lock().unwrap();
        w.retain(|_, t| t.elapsed() <= WATCH_TTL);
        w.keys().cloned().collect()
    }

    fn poll_once(&self) -> Result<(), HaError> {
        let ids = self.active();
        if ids.is_empty() {
            return Ok(());
        }
        if ids.len() > MAX_SINGLE {
            return self.states_all().map(|_| ());
        }
        for id in ids {
            match self.fetch(&id) {
                Ok(_) => {}
                Err(e) if e.kind == ErrorKind::NotFound => {}
                Err(e) => return Err(e),
            }
        }
        self.set_ok();
        Ok(())
    }

    fn poll_loop(self: Arc<Self>, rx: Receiver<()>) {
        let mut last = Instant::now() - MIN_GAP;
        let mut fails = 0u32;
        while self.running.load(Ordering::Relaxed) {
            let gap = last.elapsed();
            if gap < MIN_GAP {
                std::thread::sleep(MIN_GAP - gap);
            }
            last = Instant::now();
            let mut wait = POLL_INTERVAL;
            match self.poll_once() {
                Ok(()) => fails = 0,
                Err(e) => {
                    self.set_error(&e);
                    fails += 1;
                    wait = (POLL_INTERVAL * 2u32.pow(fails.min(5))).min(Duration::from_secs(30));
                }
            }
            match rx.recv_timeout(wait) {
                Ok(()) | Err(mpsc::RecvTimeoutError::Timeout) => {}
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
            }
            while rx.try_recv().is_ok() {}
        }
    }

    // ---------------------------------------------------------------------------- Dienstaufrufe
    /// Dienst aufrufen (blockiert); geänderte Zustände landen im Zwischenspeicher.
    pub fn call_service(&self, domain: &str, service: &str, data: &Value) -> Result<Option<Value>, HaError> {
        if domain.is_empty() || service.is_empty() {
            return Err(HaError::new(tr!("No service specified (e.g. light.turn_on)"), ErrorKind::Other));
        }
        let res = self.request("POST", &service_path(domain, service), Some(data));
        let res = match res {
            Err(e) => {
                if matches!(e.kind, ErrorKind::Auth | ErrorKind::Offline) {
                    self.set_error(&e);
                }
                self.refresh();
                return Err(e);
            }
            Ok(r) => r,
        };
        if let Some(Value::Array(lst)) = &res {
            for st in lst {
                if let Some(id) = st.get("entity_id").and_then(Value::as_str) {
                    self.store(id, Some(st.clone()));
                }
            }
        }
        self.set_ok();
        self.refresh();
        Ok(res)
    }

    /// Passenden Dienst zum Umschalten aufrufen (blockiert).
    pub fn toggle(&self, entity_id: &str) -> Result<Option<Value>, HaError> {
        let st = self.states.lock().unwrap().get(entity_id).cloned().flatten();
        let (d, s, data) = toggle_call(entity_id, st.as_ref());
        self.call_service(&d, &s, &data)
    }
}

fn reason(code: u16) -> &'static str {
    match code {
        400 => "Bad Request",
        405 => "Method Not Allowed",
        500 => "Internal Server Error",
        502 => "Bad Gateway",
        503 => "Service Unavailable",
        504 => "Gateway Timeout",
        _ => "Error",
    }
}

static CLIENT: Mutex<Option<Arc<Client>>> = Mutex::new(None);

/// Gemeinsamer Client je Verbindungseinstellung (blockiert nicht).
pub fn client(cfg: &Map<String, Value>) -> Arc<Client> {
    let settings = Settings::from_cfg(cfg);
    let mut cur = CLIENT.lock().unwrap();
    if let Some(c) = cur.as_ref() {
        if c.settings == settings {
            return c.clone();
        }
        c.close();
    }
    let c = Client::new(settings, Duration::from_secs(4));
    *cur = Some(c.clone());
    c
}

/// Der zuletzt angelegte Client (ohne neuen anzulegen).
pub fn current() -> Option<Arc<Client>> {
    CLIENT.lock().unwrap().clone()
}

/// Verbindung prüfen, ohne den gemeinsamen Client anzufassen (blockiert): (Version, Standortname).
pub fn check_connection(url: &str, token: &str, verify_ssl: bool) -> Result<(String, String), HaError> {
    let s = Settings { url: normalize_url(url), token: token.trim().to_string(), in_keyring: false, verify_ssl };
    Client::new(s, Duration::from_secs(5)).info()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn urls() {
        assert_eq!(normalize_url(" homeassistant.local:8123/ "), "http://homeassistant.local:8123");
        assert_eq!(normalize_url("https://ha.example.org/api/"), "https://ha.example.org");
        assert_eq!(normalize_url(""), "");
        assert_eq!(service_path("light", "turn_on"), "/api/services/light/turn_on");
        assert_eq!(state_path("sensor.temp wohnzimmer"), "/api/states/sensor.temp%20wohnzimmer");
        let cfg = json!({"homeassistant": {"url": "ha:8123", "verify_ssl": false}, "in_keyring": ["homeassistant.token"]});
        let s = Settings::from_cfg(cfg.as_object().unwrap());
        assert_eq!(s, Settings { url: "http://ha:8123".into(), token: String::new(), in_keyring: true, verify_ssl: false });
        assert!(s.configured());
        assert!(!Settings::from_cfg(&Map::new()).configured());
    }

    #[test]
    fn toggle_bodies() {
        let st = json!({"state": "locked"});
        assert_eq!(toggle_call("lock.door", Some(&st)), call("lock", "unlock", json!({"entity_id": "lock.door"})));
        assert_eq!(toggle_call("lock.door", None).1, "lock");
        assert_eq!(toggle_call("light.desk", None), call("light", "toggle", json!({"entity_id": "light.desk"})));
        assert_eq!(toggle_call("scene.movie", None).1, "turn_on");
        assert_eq!(toggle_call("input_button.bell", None).1, "press");
        assert_eq!(toggle_call("media_player.tv", None).1, "media_play_pause");
        assert_eq!(toggle_call("climate.office", Some(&json!({"state": "heat"}))).1, "turn_off");
        assert_eq!(toggle_call("group.all", None).0, "homeassistant");
    }

    #[test]
    fn dial_bodies() {
        let st = json!({"state": "on", "attributes": {"brightness": 128}});
        let (state, attrs, c) = dial_call("light.desk", Some(&st), 5, 2).unwrap();
        assert_eq!(state.as_deref(), Some("on"));
        assert_eq!(c, call("light", "turn_on", json!({"brightness_pct": 60})));
        assert_eq!(attrs["brightness"], json!(153));
        let (state, _, c) = dial_call("light.desk", Some(&st), 50, -3).unwrap();
        assert_eq!((state.as_deref(), c.1.as_str()), (Some("off"), "turn_off"));
        let st = json!({"state": "heat", "attributes": {"temperature": 21.0, "target_temp_step": 0.5, "max_temp": 21.5}});
        let (_, _, c) = dial_call("climate.office", Some(&st), 5, 3).unwrap();
        assert_eq!(c.2, json!({"temperature": 21.5}));
        let (_, _, c) = dial_call("media_player.tv", Some(&json!({"attributes": {"volume_level": 0.5}})), 5, -1).unwrap();
        assert_eq!(c.2, json!({"volume_level": 0.45}));
        assert!(dial_call("sensor.x", None, 5, 1).is_none());
    }

    #[test]
    fn states_and_formatting() {
        let st = json!({"state": "21.50", "attributes": {"unit_of_measurement": "°C", "friendly_name": "Büro", "level": 3}});
        assert_eq!(format_state(Some(&st), "", None), "21.5 °C");
        assert_eq!(format_state(Some(&st), "", Some(2)), "21.50 °C");
        assert_eq!(format_state(Some(&st), "level", None), "3");
        assert_eq!(format_state(Some(&st), "missing", None), "–");
        assert_eq!(format_state(Some(&json!({"state": "1234.5"})), "", None), "1234");
        assert_eq!(format_state(None, "", None), "");
        assert_eq!(friendly_name(Some(&st), "sensor.buero"), "Büro");
        assert_eq!(friendly_name(None, "sensor.buero_temp"), "buero temp");
        assert_eq!(is_on(Some(&json!({"state": "on"}))), Some(true));
        assert_eq!(is_on(Some(&json!({"state": "closed"}))), Some(false));
        assert_eq!(is_on(Some(&json!({"state": "unavailable"}))), None);
        assert_eq!(is_on(Some(&json!({"state": "42"}))), None);
    }

    #[test]
    fn services_and_data() {
        assert_eq!(parse_service(" light.turn_on ").unwrap(), ("light".into(), "turn_on".into()));
        assert!(parse_service("light").is_err());
        assert!(parse_service(".x").is_err());
        assert_eq!(parse_data(Some(&json!(""))).unwrap(), Map::new());
        assert_eq!(parse_data(Some(&json!("{\"brightness_pct\": 40}"))).unwrap()["brightness_pct"], json!(40));
        assert_eq!(parse_data(Some(&json!({"a": 1}))).unwrap()["a"], json!(1));
        assert!(parse_data(Some(&json!("[1]"))).is_err());
        assert!(parse_data(Some(&json!("{kaputt"))).is_err());
    }

    #[test]
    fn unconfigured_client_does_not_touch_network() {
        let c = Client::new(Settings::from_cfg(&Map::new()), Duration::from_secs(1));
        assert_eq!(c.error_kind(), Some(ErrorKind::Config));
        assert_eq!(c.request("GET", "/api/", None).unwrap_err().kind, ErrorKind::Config);
        assert_eq!(c.call_service("", "x", &json!({})).unwrap_err().kind, ErrorKind::Other);
    }
}
