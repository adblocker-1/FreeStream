//! Anbindung an OBS Studio über obs-websocket (Protokoll v5, in OBS ab 28 enthalten) – wie
//! `freestream/obs.py`.
//!
//! In OBS: Werkzeuge → WebSocket-Server-Einstellungen → „WebSocket-Server aktivieren“.
//! Verbindung in config.json: `"obs": {"host": "127.0.0.1", "port": 4455, "password": "…"}` (das Passwort
//! auch im Schlüsselbund, siehe [`freestream_core::secrets`]).
//!
//! Ein Hintergrund-Thread hält die Verbindung, beantwortet Anfragen und hält den Zustand (aktive Szene,
//! Aufnahme, Stream …) über OBS-Ereignisse aktuell; er verbindet sich nach einem Neustart von OBS von
//! selbst wieder. [`Client::request`] blockiert bis zur Antwort – nur aus Arbeits-Threads aufrufen.

use base64::Engine as _;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::net::{TcpStream, ToSocketAddrs};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender, SyncSender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use freestream_core::{secrets, tr};

use crate::net::Version;

/// Alle Ereignisse außer den sehr häufigen (Pegel, Szenenelement-Transformation)
pub const EVENTS_ALL: u32 = 0x7FF;
pub const DEFAULT_PORT: u16 = 4455;

// ------------------------------------------------------------------------------------- Protokoll
/// Antwort auf die Anmelde-Aufforderung von obs-websocket:
/// `base64(sha256(base64(sha256(Passwort + salt)) + challenge))`.
pub fn auth_string(password: &str, salt: &str, challenge: &str) -> String {
    let b64 = base64::engine::general_purpose::STANDARD;
    let secret = b64.encode(Sha256::digest(format!("{password}{salt}").as_bytes()));
    b64.encode(Sha256::digest(format!("{secret}{challenge}").as_bytes()))
}

/// Anmeldung (op 1) auf das „Hello“ (op 0) von OBS. `password`: None = (noch) keins bekannt.
pub fn identify_message(hello: &Value, password: Option<&str>) -> Result<Value, String> {
    if hello.get("op").and_then(Value::as_i64) != Some(0) {
        return Err(tr!("Unexpected response from OBS"));
    }
    let d = hello.get("d").cloned().unwrap_or(Value::Null);
    let mut ident = json!({"rpcVersion": 1, "eventSubscriptions": EVENTS_ALL});
    if let Some(auth) = d.get("authentication").filter(|a| a.is_object()) {
        let pw = password.filter(|p| !p.is_empty())
            .ok_or_else(|| tr!("OBS requires a password – enter it under Settings → Connections → OBS Studio"))?;
        let salt = auth.get("salt").and_then(Value::as_str).unwrap_or("");
        let challenge = auth.get("challenge").and_then(Value::as_str).unwrap_or("");
        ident["authentication"] = Value::from(auth_string(pw, salt, challenge));
    }
    Ok(json!({"op": 1, "d": ident}))
}

/// Braucht OBS ein Passwort (laut „Hello“)?
pub fn needs_auth(hello: &Value) -> bool {
    hello.get("d").and_then(|d| d.get("authentication")).map(Value::is_object).unwrap_or(false)
}

/// Anfrage (op 6).
pub fn request_message(rtype: &str, id: &str, data: Option<&Value>) -> Value {
    let mut d = json!({"requestType": rtype, "requestId": id});
    if let Some(data) = data.filter(|v| !v.is_null()) {
        d["requestData"] = data.clone();
    }
    json!({"op": 6, "d": d})
}

/// Ergebnis einer Antwort (op 7, Inhalt von „d“): responseData oder Fehlertext.
pub fn parse_response(d: &Value) -> Result<Value, String> {
    let st = d.get("requestStatus").cloned().unwrap_or(Value::Null);
    if st.get("result").and_then(Value::as_bool).unwrap_or(false) {
        return Ok(d.get("responseData").cloned().filter(|v| !v.is_null()).unwrap_or_else(|| json!({})));
    }
    match st.get("comment").and_then(Value::as_str).filter(|c| !c.is_empty()) {
        Some(c) => Err(c.to_string()),
        None => Err(tr!("Error {code}", code = st.get("code").map(|c| c.to_string()).unwrap_or_default())),
    }
}

/// Zustand von OBS, wie ihn die Ereignisse melden.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ObsState {
    pub scene: String,
    pub preview: String,
    pub recording: bool,
    pub rec_paused: bool,
    pub streaming: bool,
    pub replay: bool,
    pub virtualcam: bool,
    pub studio: bool,
    /// Eingang -> stumm
    pub muted: HashMap<String, bool>,
    /// Eingang -> dB
    pub volume_db: HashMap<String, f64>,
    /// (Szene, Quelle) -> (sceneItemId, sichtbar)
    pub items: HashMap<(String, String), (i64, bool)>,
}

impl ObsState {
    /// Ereignis (op 5) anwenden; true = etwas hat sich geändert.
    pub fn apply_event(&mut self, et: &str, data: &Value) -> bool {
        let s = |k: &str| data.get(k).and_then(Value::as_str).unwrap_or("").to_string();
        let b = |k: &str| data.get(k).and_then(Value::as_bool).unwrap_or(false);
        match et {
            "CurrentProgramSceneChanged" => self.scene = s("sceneName"),
            "CurrentPreviewSceneChanged" => self.preview = s("sceneName"),
            "RecordStateChanged" => {
                self.recording = b("outputActive");
                self.rec_paused = s("outputState") == "OBS_WEBSOCKET_OUTPUT_PAUSED";
            }
            "StreamStateChanged" => self.streaming = b("outputActive"),
            "ReplayBufferStateChanged" => self.replay = b("outputActive"),
            "VirtualcamStateChanged" => self.virtualcam = b("outputActive"),
            "StudioModeStateChanged" => self.studio = b("studioModeEnabled"),
            "InputMuteStateChanged" => {
                self.muted.insert(s("inputName"), b("inputMuted"));
            }
            "InputVolumeChanged" => {
                if let Some(db) = data.get("inputVolumeDb").and_then(Value::as_f64) {
                    self.volume_db.insert(s("inputName"), db);
                }
            }
            "SceneItemEnableStateChanged" => {
                let (scene, id) = (s("sceneName"), data.get("sceneItemId").and_then(Value::as_i64));
                for ((sc, _), (iid, on)) in self.items.iter_mut() {
                    if *sc == scene && Some(*iid) == id {
                        *on = b("sceneItemEnabled");
                    }
                }
            }
            "SceneItemCreated" | "SceneItemRemoved" | "SceneNameChanged" | "InputNameChanged" => self.items.clear(),
            _ => return false,
        }
        true
    }
}

// ---------------------------------------------------------------------------------------- Client
/// Nachricht an den Verbindungs-Thread.
enum Out {
    Request { rtype: String, data: Option<Value>, reply: SyncSender<Result<Value, String>> },
    /// sofort (neu) verbinden bzw. Ende prüfen
    Wake,
}

/// Verbindungseinstellung (Host, Port, Passwort aus der Datei, Passwort im Schlüsselbund vermerkt).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Settings {
    pub host: String,
    pub port: u16,
    pub password: String,
    pub in_keyring: bool,
}

impl Settings {
    pub fn from_cfg(cfg: &Map<String, Value>) -> Self {
        let c = cfg.get("obs").and_then(Value::as_object).cloned().unwrap_or_default();
        let host = c.get("host").and_then(Value::as_str).filter(|h| !h.trim().is_empty()).unwrap_or("127.0.0.1").trim().to_string();
        let port = c.get("port").and_then(|p| p.as_u64().or_else(|| p.as_str()?.trim().parse().ok()))
            .filter(|p| *p > 0 && *p <= 65535).unwrap_or(DEFAULT_PORT as u64) as u16;
        Self { host, port, password: secrets::in_file(cfg, "obs", "password").unwrap_or_default(),
               in_keyring: secrets::listed(cfg, "obs", "password") }
    }

    /// Adresse für den WebSocket (IPv6 in Klammern).
    pub fn url(&self) -> String {
        if self.host.contains(':') && !self.host.starts_with('[') {
            format!("ws://[{}]:{}/", self.host, self.port)
        } else {
            format!("ws://{}:{}/", self.host, self.port)
        }
    }
}

/// Gemeinsamer OBS-Client (ein Verbindungs-Thread).
pub struct Client {
    pub settings: Settings,
    state: Mutex<ObsState>,
    connected: AtomicBool,
    running: AtomicBool,
    error: Mutex<String>,
    version: Version,
    tx: Mutex<Sender<Out>>,
    /// Eingänge/Szenenelemente, die gerade abgefragt werden (bzw. wann zuletzt erfolglos)
    asked: Mutex<HashMap<String, Instant>>,
    busy: Mutex<HashSet<String>>,
    next_id: AtomicU64,
}

impl Client {
    fn start(settings: Settings) -> Arc<Client> {
        let (tx, rx) = mpsc::channel();
        let c = Arc::new(Client {
            settings,
            state: Mutex::new(ObsState::default()),
            connected: AtomicBool::new(false),
            running: AtomicBool::new(true),
            error: Mutex::new(String::new()),
            version: Version::default(),
            tx: Mutex::new(tx),
            asked: Mutex::new(HashMap::new()),
            busy: Mutex::new(HashSet::new()),
            next_id: AtomicU64::new(1),
        });
        let me = c.clone();
        std::thread::Builder::new().name("obs".into()).spawn(move || me.run(rx)).ok();
        c
    }

    pub fn connected(&self) -> bool {
        self.connected.load(Ordering::Relaxed)
    }

    pub fn error(&self) -> String {
        self.error.lock().unwrap().clone()
    }

    pub fn version(&self) -> u64 {
        self.version.get()
    }

    /// Kopie des Zustands.
    pub fn state(&self) -> ObsState {
        self.state.lock().unwrap().clone()
    }

    fn wake(&self) {
        let _ = self.tx.lock().unwrap().send(Out::Wake);
    }

    fn close(&self) {
        self.running.store(false, Ordering::Relaxed);
        self.wake();
    }

    fn set_connected(&self, on: bool) {
        if self.connected.swap(on, Ordering::Relaxed) != on {
            self.version.bump();
        }
    }

    // ------------------------------------------------------------------------- Verbindungs-Thread
    fn run(self: Arc<Self>, rx: Receiver<Out>) {
        let mut delay = Duration::from_secs(1);
        let mut password: Option<String> = None;
        while self.running.load(Ordering::Relaxed) {
            let res = self.connect(&mut password).and_then(|ws| {
                delay = Duration::from_secs(1);
                self.serve(ws, &rx)
            });
            if let Err(e) = res {
                let mut err = self.error.lock().unwrap();
                if self.connected() || *err != e {
                    log::info!("{}", tr!("OBS not connected: {error}", error = e));
                }
                *err = e;
            }
            self.set_connected(false);
            if !self.running.load(Ordering::Relaxed) {
                break;
            }
            // warten (Anfragen in der Zeit scheitern sofort und wecken auf)
            match rx.recv_timeout(delay) {
                Ok(Out::Request { reply, .. }) => {
                    let _ = reply.send(Err(self.not_connected()));
                }
                Ok(Out::Wake) | Err(RecvTimeoutError::Timeout) => {}
                Err(RecvTimeoutError::Disconnected) => break,
            }
            delay = (delay * 2).min(Duration::from_secs(10));
        }
    }

    fn not_connected(&self) -> String {
        let e = self.error();
        if e.is_empty() { tr!("OBS is not connected") } else { tr!("OBS is not connected ({error})", error = e) }
    }

    fn connect(&self, password: &mut Option<String>) -> Result<tungstenite::WebSocket<TcpStream>, String> {
        let s = &self.settings;
        let addr = (s.host.as_str(), s.port).to_socket_addrs().map_err(|e| e.to_string())?.next()
            .ok_or_else(|| format!("{}: ?", s.host))?;
        let stream = TcpStream::connect_timeout(&addr, Duration::from_secs(3)).map_err(|e| e.to_string())?;
        stream.set_read_timeout(Some(Duration::from_secs(3))).map_err(|e| e.to_string())?;
        stream.set_write_timeout(Some(Duration::from_secs(3))).map_err(|e| e.to_string())?;
        let (mut ws, _) = tungstenite::client(s.url(), stream).map_err(|e| match e {
            tungstenite::HandshakeError::Failure(e) => tr!("Handshake rejected: {status}", status = e),
            tungstenite::HandshakeError::Interrupted(_) => tr!("Connection closed"),
        })?;
        let hello = read_json(&mut ws)?;
        if needs_auth(&hello) && password.is_none() {
            // Passwort erst jetzt holen (secret-tool kann dauern; hier blockiert es nur diesen Thread)
            *password = if s.password.is_empty() && s.in_keyring { secrets::lookup("obs.password") } else { Some(s.password.clone()) };
        }
        let ident = identify_message(&hello, password.as_deref())?;
        ws.send(tungstenite::Message::text(ident.to_string())).map_err(|e| e.to_string())?;
        let msg = read_json(&mut ws).map_err(|_| tr!("Login to OBS failed (check the password)"))?;
        if msg.get("op").and_then(Value::as_i64) != Some(2) {
            return Err(tr!("Login to OBS failed (check the password)"));
        }
        ws.get_mut().set_read_timeout(Some(Duration::from_millis(100))).map_err(|e| e.to_string())?;
        Ok(ws)
    }

    /// Verbunden: Anfragen senden, Antworten zuordnen, Ereignisse anwenden – bis die Verbindung endet.
    fn serve(self: &Arc<Self>, mut ws: tungstenite::WebSocket<TcpStream>, rx: &Receiver<Out>) -> Result<(), String> {
        self.error.lock().unwrap().clear();
        {
            let mut st = self.state.lock().unwrap();
            st.muted.clear();
            st.volume_db.clear();
            st.items.clear();
        }
        self.asked.lock().unwrap().clear();
        self.set_connected(true);
        log::info!("{}", tr!("Connected to OBS ({host}:{port})", host = self.settings.host, port = self.settings.port));
        let me = self.clone();
        std::thread::spawn(move || me.initial_state());
        let mut pending: HashMap<String, SyncSender<Result<Value, String>>> = HashMap::new();
        let res = (|| loop {
            if !self.running.load(Ordering::Relaxed) {
                let _ = ws.close(None);
                let _ = ws.flush();
                return Ok(());
            }
            while let Ok(out) = rx.try_recv() {
                if let Out::Request { rtype, data, reply } = out {
                    let id = format!("fd{}", self.next_id.fetch_add(1, Ordering::Relaxed));
                    let msg = request_message(&rtype, &id, data.as_ref());
                    pending.insert(id, reply);
                    ws.send(tungstenite::Message::text(msg.to_string())).map_err(|e| e.to_string())?;
                }
            }
            match ws.read() {
                Ok(tungstenite::Message::Text(t)) => {
                    let Ok(msg) = serde_json::from_str::<Value>(t.as_str()) else { continue };
                    let d = msg.get("d").cloned().unwrap_or(Value::Null);
                    match msg.get("op").and_then(Value::as_i64) {
                        Some(7) => {
                            let id = d.get("requestId").and_then(Value::as_str).unwrap_or("");
                            if let Some(reply) = pending.remove(id) {
                                let _ = reply.send(parse_response(&d));
                            }
                        }
                        Some(5) => {
                            let et = d.get("eventType").and_then(Value::as_str).unwrap_or("");
                            let data = d.get("eventData").cloned().unwrap_or(Value::Null);
                            if self.state.lock().unwrap().apply_event(et, &data) {
                                self.version.bump();
                            }
                        }
                        _ => {}
                    }
                }
                Ok(tungstenite::Message::Close(_)) => return Err(tr!("Connection closed")),
                Ok(_) => {}
                Err(tungstenite::Error::Io(e)) if matches!(e.kind(), std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut) => {
                    let _ = ws.flush();
                }
                Err(e) => return Err(e.to_string()),
            }
        })();
        let lost = tr!("Connection to OBS lost");
        for (_, reply) in pending.drain() {
            let _ = reply.send(Err(lost.clone()));
        }
        res
    }

    fn initial_state(&self) {
        let get = |req: &str, field: &str| self.request(req, None, Duration::from_secs(3)).ok().and_then(|d| d.get(field).cloned());
        let scene = get("GetCurrentProgramScene", "currentProgramSceneName");
        let rec = self.request("GetRecordStatus", None, Duration::from_secs(3)).ok();
        let stream = get("GetStreamStatus", "outputActive");
        let vcam = get("GetVirtualCamStatus", "outputActive");
        let replay = get("GetReplayBufferStatus", "outputActive");
        let studio = get("GetStudioModeEnabled", "studioModeEnabled");
        let preview = studio.as_ref().and_then(Value::as_bool).unwrap_or(false)
            .then(|| get("GetCurrentPreviewScene", "currentPreviewSceneName")).flatten();
        {
            let mut s = self.state.lock().unwrap();
            if let Some(v) = scene.as_ref().and_then(Value::as_str) {
                s.scene = v.to_string();
            }
            if let Some(v) = preview.as_ref().and_then(Value::as_str) {
                s.preview = v.to_string();
            }
            if let Some(r) = &rec {
                s.recording = r.get("outputActive").and_then(Value::as_bool).unwrap_or(s.recording);
                s.rec_paused = r.get("outputPaused").and_then(Value::as_bool).unwrap_or(false);
            }
            let set = |b: &mut bool, v: &Option<Value>| {
                if let Some(x) = v.as_ref().and_then(Value::as_bool) {
                    *b = x;
                }
            };
            set(&mut s.streaming, &stream);
            set(&mut s.virtualcam, &vcam);
            set(&mut s.replay, &replay);
            set(&mut s.studio, &studio);
        }
        self.version.bump();
    }

    // ---------------------------------------------------------------------------------- Anfragen
    /// Anfrage an OBS; blockiert bis zur Antwort (höchstens `timeout`).
    pub fn request(&self, rtype: &str, data: Option<Value>, timeout: Duration) -> Result<Value, String> {
        if !self.connected() {
            self.wake();
            return Err(self.not_connected());
        }
        let (reply, answer) = mpsc::sync_channel(1);
        self.tx.lock().unwrap().send(Out::Request { rtype: rtype.to_string(), data, reply })
            .map_err(|_| self.not_connected())?;
        match answer.recv_timeout(timeout) {
            Ok(r) => r,
            Err(_) => Err(tr!("No response from OBS to {request}", request = rtype)),
        }
    }

    fn req(&self, rtype: &str, data: Value) -> Result<Value, String> {
        self.request(rtype, Some(data), Duration::from_secs(3))
    }

    /// Bis zu `timeout` auf die Verbindung warten (nach einem Tastendruck, wenn OBS gerade startet).
    pub fn wait_connected(&self, timeout: Duration) -> bool {
        let end = Instant::now() + timeout;
        while !self.connected() && Instant::now() < end {
            self.wake();
            std::thread::sleep(Duration::from_millis(50));
        }
        self.connected()
    }

    /// Szene wechseln (im Studio-Modus die Vorschau).
    pub fn set_scene(&self, name: &str) -> Result<(), String> {
        let studio = self.state.lock().unwrap().studio;
        let rtype = if studio { "SetCurrentPreviewScene" } else { "SetCurrentProgramScene" };
        self.req(rtype, json!({"sceneName": name})).map(|_| ())
    }

    pub fn input_volume_db(&self, input: &str) -> Option<f64> {
        if let Some(v) = self.state.lock().unwrap().volume_db.get(input) {
            return Some(*v);
        }
        let v = self.req("GetInputVolume", json!({"inputName": input})).ok()?.get("inputVolumeDb")?.as_f64()?;
        self.state.lock().unwrap().volume_db.insert(input.to_string(), v);
        Some(v)
    }

    /// Lautstärke eines Eingangs um `delta` dB ändern (-100 … +26 dB).
    pub fn change_volume_db(&self, input: &str, delta: f64) -> Result<(), String> {
        let cur = self.input_volume_db(input).filter(|v| *v >= -100.0).unwrap_or(-20.0);
        let new = (cur + delta).clamp(-100.0, 26.0);
        self.req("SetInputVolume", json!({"inputName": input, "inputVolumeDb": new}))?;
        self.state.lock().unwrap().volume_db.insert(input.to_string(), new);
        self.version.bump();
        Ok(())
    }

    /// Szenenelement (Szene leer = aktuelle Szene): (Szene, (sceneItemId, sichtbar)).
    pub fn item(&self, scene: &str, source: &str) -> Result<(String, (i64, bool)), String> {
        let scene = if scene.is_empty() { self.state.lock().unwrap().scene.clone() } else { scene.to_string() };
        let key = (scene.clone(), source.to_string());
        if let Some(it) = self.state.lock().unwrap().items.get(&key) {
            return Ok((scene, *it));
        }
        let iid = self.req("GetSceneItemId", json!({"sceneName": scene, "sourceName": source}))?
            .get("sceneItemId").and_then(Value::as_i64).ok_or_else(|| tr!("Unexpected response from OBS"))?;
        let on = self.req("GetSceneItemEnabled", json!({"sceneName": scene, "sceneItemId": iid}))?
            .get("sceneItemEnabled").and_then(Value::as_bool).unwrap_or(false);
        self.state.lock().unwrap().items.insert(key, (iid, on));
        self.version.bump();
        Ok((scene, (iid, on)))
    }

    pub fn toggle_source(&self, scene: &str, source: &str) -> Result<(), String> {
        let (scene, (iid, on)) = self.item(scene, source)?;
        self.req("SetSceneItemEnabled", json!({"sceneName": scene, "sceneItemId": iid, "sceneItemEnabled": !on}))?;
        self.state.lock().unwrap().items.insert((scene, source.to_string()), (iid, !on));
        self.version.bump();
        Ok(())
    }

    /// Szenen in der Reihenfolge von OBS (oben zuerst).
    pub fn scenes(&self) -> Result<Vec<String>, String> {
        let list = self.request("GetSceneList", None, Duration::from_secs(3))?;
        Ok(list.get("scenes").and_then(Value::as_array).map(|a| {
            a.iter().rev().filter_map(|s| s.get("sceneName").and_then(Value::as_str).map(str::to_string)).collect()
        }).unwrap_or_default())
    }

    /// Im Hintergrund nachfragen (einmal je Schlüssel, nach Fehlern frühestens nach 5 s wieder).
    fn fetch_once(self: &Arc<Self>, key: String, f: impl FnOnce(&Client) -> bool + Send + 'static) {
        if !self.connected() {
            return;
        }
        if let Some(t) = self.asked.lock().unwrap().get(&key) {
            if t.elapsed() < Duration::from_secs(5) {
                return;
            }
        }
        if !self.busy.lock().unwrap().insert(key.clone()) {
            return;
        }
        let me = self.clone();
        std::thread::spawn(move || {
            if !f(&me) {
                me.asked.lock().unwrap().insert(key.clone(), Instant::now());
            }
            me.busy.lock().unwrap().remove(&key);
        });
    }

    /// Stummschaltung eines Eingangs (None = noch unbekannt, wird im Hintergrund abgefragt).
    pub fn is_muted(self: &Arc<Self>, input: &str) -> Option<bool> {
        if let Some(m) = self.state.lock().unwrap().muted.get(input) {
            return Some(*m);
        }
        let name = input.to_string();
        self.fetch_once(format!("in:{input}"), move |c| {
            let Ok(r) = c.req("GetInputMute", json!({"inputName": name})) else { return false };
            let m = r.get("inputMuted").and_then(Value::as_bool).unwrap_or(false);
            c.state.lock().unwrap().muted.insert(name.clone(), m);
            let _ = c.input_volume_db(&name);
            c.version.bump();
            true
        });
        None
    }

    /// Sichtbarkeit eines Szenenelements (None = noch unbekannt, wird im Hintergrund abgefragt).
    pub fn item_visible(self: &Arc<Self>, scene: &str, source: &str) -> Option<bool> {
        {
            let st = self.state.lock().unwrap();
            let sc = if scene.is_empty() { st.scene.clone() } else { scene.to_string() };
            if let Some((_, on)) = st.items.get(&(sc, source.to_string())) {
                return Some(*on);
            }
        }
        let (scene, source) = (scene.to_string(), source.to_string());
        self.fetch_once(format!("item:{scene}\u{1}{source}"), move |c| c.item(&scene, &source).is_ok());
        None
    }
}

fn read_json(ws: &mut tungstenite::WebSocket<TcpStream>) -> Result<Value, String> {
    loop {
        match ws.read().map_err(|e| e.to_string())? {
            tungstenite::Message::Text(t) => return serde_json::from_str(t.as_str()).map_err(|_| tr!("Unexpected response from OBS")),
            tungstenite::Message::Close(_) => return Err(tr!("Connection closed")),
            _ => {}
        }
    }
}

static CLIENT: Mutex<Option<Arc<Client>>> = Mutex::new(None);

/// Gemeinsamer OBS-Client je Verbindungseinstellung (andere Einstellung: alten Client beenden).
/// Blockiert nicht – die Verbindung baut der Hintergrund-Thread auf.
pub fn client(cfg: &Map<String, Value>) -> Arc<Client> {
    let settings = Settings::from_cfg(cfg);
    let mut cur = CLIENT.lock().unwrap();
    if let Some(c) = cur.as_ref() {
        if c.settings == settings {
            return c.clone();
        }
        c.close();
    }
    let c = Client::start(settings);
    *cur = Some(c.clone());
    c
}

/// Der zuletzt angelegte Client (ohne neuen anzulegen).
pub fn current() -> Option<Arc<Client>> {
    CLIENT.lock().unwrap().clone()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn auth_string_matches_documented_algorithm() {
        // Beispiel aus der obs-websocket-Doku (Protokoll v5, „Creating an authentication string“):
        // secret = base64(sha256(password + salt)); auth = base64(sha256(secret + challenge))
        let (pw, salt, challenge) = ("supersecretpassword", "lM1GncleQOaCu9lT1yeUZhFYnqhsLLP1G5lAGo3ixaI=",
                                     "+IxH4CnCiqpX1rM9scsNynZzbOe4KhDeYcTNS3PDaeY=");
        assert_eq!(auth_string(pw, salt, challenge), "1Ct943GAT+6YQUUX47Ia/ncufilbe6+oD6lY+5kaCu4=");
        // gleich dem Ergebnis von freestream/obs.py auth_string
        assert_eq!(auth_string("pw", "salt", "challenge"), "u2LyppGkAjwgvCNaKsAc4tztRWLqv1i85slC7KiH71Q=");
    }

    #[test]
    fn identify_with_and_without_auth() {
        let hello = json!({"op": 0, "d": {"obsWebSocketVersion": "5.1.0", "rpcVersion": 1}});
        let id = identify_message(&hello, None).unwrap();
        assert_eq!(id, json!({"op": 1, "d": {"rpcVersion": 1, "eventSubscriptions": EVENTS_ALL}}));
        let hello = json!({"op": 0, "d": {"rpcVersion": 1, "authentication": {"salt": "salt", "challenge": "challenge"}}});
        assert!(needs_auth(&hello));
        assert!(identify_message(&hello, None).is_err());
        assert!(identify_message(&hello, Some("")).is_err());
        let id = identify_message(&hello, Some("pw")).unwrap();
        assert_eq!(id["d"]["authentication"], json!(auth_string("pw", "salt", "challenge")));
        assert!(identify_message(&json!({"op": 2}), None).is_err());
    }

    #[test]
    fn request_and_response_json() {
        assert_eq!(request_message("GetRecordStatus", "fd1", None),
                   json!({"op": 6, "d": {"requestType": "GetRecordStatus", "requestId": "fd1"}}));
        assert_eq!(request_message("SetCurrentProgramScene", "fd2", Some(&json!({"sceneName": "Kamera"}))),
                   json!({"op": 6, "d": {"requestType": "SetCurrentProgramScene", "requestId": "fd2",
                                          "requestData": {"sceneName": "Kamera"}}}));
        let ok = json!({"requestType": "GetRecordStatus", "requestId": "fd1",
                        "requestStatus": {"result": true, "code": 100}, "responseData": {"outputActive": true}});
        assert_eq!(parse_response(&ok).unwrap()["outputActive"], json!(true));
        let ok_empty = json!({"requestStatus": {"result": true, "code": 100}});
        assert_eq!(parse_response(&ok_empty).unwrap(), json!({}));
        let err = json!({"requestStatus": {"result": false, "code": 600, "comment": "No source was found"}});
        assert_eq!(parse_response(&err).unwrap_err(), "No source was found");
        let err = json!({"requestStatus": {"result": false, "code": 204}});
        assert!(parse_response(&err).unwrap_err().contains("204"));
    }

    #[test]
    fn events_update_state() {
        let mut s = ObsState::default();
        assert!(s.apply_event("CurrentProgramSceneChanged", &json!({"sceneName": "Kamera"})));
        assert_eq!(s.scene, "Kamera");
        s.apply_event("RecordStateChanged", &json!({"outputActive": true, "outputState": "OBS_WEBSOCKET_OUTPUT_PAUSED"}));
        assert!(s.recording && s.rec_paused);
        s.apply_event("InputMuteStateChanged", &json!({"inputName": "Mic/Aux", "inputMuted": true}));
        assert_eq!(s.muted.get("Mic/Aux"), Some(&true));
        s.items.insert(("Kamera".into(), "Logo".into()), (7, true));
        s.apply_event("SceneItemEnableStateChanged", &json!({"sceneName": "Kamera", "sceneItemId": 7, "sceneItemEnabled": false}));
        assert_eq!(s.items[&("Kamera".to_string(), "Logo".to_string())], (7, false));
        s.apply_event("SceneItemRemoved", &json!({}));
        assert!(s.items.is_empty());
        assert!(!s.apply_event("InputActiveStateChanged", &json!({})));
    }

    #[test]
    fn settings_from_config() {
        let cfg = json!({"obs": {"host": "::1", "port": "4456", "password": "x"}, "in_keyring": ["obs.password"]});
        let s = Settings::from_cfg(cfg.as_object().unwrap());
        assert_eq!((s.port, s.password.as_str(), s.in_keyring), (4456, "x", true));
        assert_eq!(s.url(), "ws://[::1]:4456/");
        let s = Settings::from_cfg(&Map::new());
        assert_eq!(s.url(), "ws://127.0.0.1:4455/");
    }
}
