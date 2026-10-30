//! Discord-Steuerung über die lokale RPC-Schnittstelle (IPC-Socket der Desktop-App) – wie
//! `freestream/discord.py`.
//!
//! Discord erlaubt Stummschalten/Taubschalten per RPC nur angemeldeten Apps. Dafür einmalig eine eigene
//! App anlegen (https://discord.com/developers/applications, Redirect-URL `http://localhost`) und Client-ID
//! und Client-Secret eintragen: `"discord": {"client_id": "…", "client_secret": "…"}` (das Secret auch im
//! Schlüsselbund). Beim ersten Tastendruck fragt Discord einmal nach der Erlaubnis; der Zugang wird danach
//! gespeichert (`~/.local/state/freestream/discord-token.json`, wie in der Python-Version).
//!
//! Alle Befehle blockieren (Anmeldung bis zu 2 Minuten) – nur aus dem Arbeits-Thread aufrufen.

use freestream_core::{paths, secrets, tr};
use serde_json::{json, Map, Value};
use std::collections::HashMap;
use std::io::{Read, Write};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{self, SyncSender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use crate::native::Worker;
use crate::net::{self, Version};

pub const OP_HANDSHAKE: u32 = 0;
pub const OP_FRAME: u32 = 1;
pub const OP_CLOSE: u32 = 2;
pub const OP_PING: u32 = 3;
pub const OP_PONG: u32 = 4;
pub const SCOPES: [&str; 3] = ["rpc", "rpc.voice.read", "rpc.voice.write"];
pub const TOKEN_URL: &str = "https://discord.com/api/oauth2/token";
pub const REDIRECT_URI: &str = "http://localhost";

#[derive(Debug, Clone, PartialEq)]
pub enum DiscordError {
    Other(String),
    /// Discord lehnt die Zugangsdaten ab (Zugang widerrufen, abgelaufen, andere App) – nicht: offline
    Auth(String),
}

impl std::fmt::Display for DiscordError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            DiscordError::Other(m) | DiscordError::Auth(m) => f.write_str(m),
        }
    }
}

fn err(m: String) -> DiscordError {
    DiscordError::Other(m)
}

// ------------------------------------------------------------------------------------- Protokoll
/// Ein Rahmen: Opcode und Länge (je u32, Little Endian), dann JSON.
pub fn encode_frame(op: u32, payload: &Value) -> Vec<u8> {
    let data = payload.to_string().into_bytes();
    let mut out = Vec::with_capacity(8 + data.len());
    out.extend_from_slice(&op.to_le_bytes());
    out.extend_from_slice(&(data.len() as u32).to_le_bytes());
    out.extend_from_slice(&data);
    out
}

/// Kopf eines Rahmens lesen: (Opcode, Länge).
pub fn decode_header(h: &[u8; 8]) -> (u32, u32) {
    (u32::from_le_bytes([h[0], h[1], h[2], h[3]]), u32::from_le_bytes([h[4], h[5], h[6], h[7]]))
}

/// Befehl mit Nonce.
pub fn command_message(cmd: &str, args: Value, nonce: &str, evt: Option<&str>) -> Value {
    let mut p = json!({"cmd": cmd, "args": if args.is_null() { json!({}) } else { args }, "nonce": nonce});
    if let Some(e) = evt {
        p["evt"] = Value::from(e);
    }
    p
}

/// Mögliche Sockets der Discord-App (auch Flatpak, Snap, Vesktop).
pub fn socket_paths() -> Vec<PathBuf> {
    let uid = unsafe { libc::getuid() };
    let runtime = std::env::var("XDG_RUNTIME_DIR").ok().filter(|s| !s.is_empty()).unwrap_or_else(|| format!("/run/user/{uid}"));
    let tmp = std::env::var("TMPDIR").ok().filter(|s| !s.is_empty()).unwrap_or_else(|| "/tmp".into());
    let subs = ["", "app/com.discordapp.Discord", "snap.discord", ".flatpak/com.discordapp.Discord/xdg-run", "app/dev.vencord.Vesktop"];
    let mut out = Vec::new();
    for b in [runtime, tmp] {
        for s in subs {
            for i in 0..10 {
                let p = PathBuf::from(&b).join(s).join(format!("discord-ipc-{i}"));
                if p.exists() {
                    out.push(p);
                }
            }
        }
    }
    out
}

fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

// ---------------------------------------------------------------------------------------- Client
/// Verbindungseinstellung (Client-ID, Secret aus der Datei, Secret im Schlüsselbund vermerkt).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Settings {
    pub client_id: String,
    pub client_secret: String,
    pub in_keyring: bool,
}

impl Settings {
    pub fn from_cfg(cfg: &Map<String, Value>) -> Self {
        Self {
            client_id: secrets::in_file(cfg, "discord", "client_id").unwrap_or_default(),
            client_secret: secrets::in_file(cfg, "discord", "client_secret").unwrap_or_default(),
            in_keyring: secrets::listed(cfg, "discord", "client_secret"),
        }
    }
}

type Reply = SyncSender<Result<Value, String>>;

pub struct Client {
    pub settings: Settings,
    pub token_file: PathBuf,
    sock: Mutex<Option<UnixStream>>,
    /// zählt die Verbindungen (der Lese-Thread einer alten Verbindung räumt nichts Neues ab)
    generation: AtomicU64,
    ready: AtomicBool,
    voice: Mutex<(bool, bool)>,
    version: Version,
    pending: Mutex<HashMap<String, Reply>>,
    connecting: Mutex<()>,
    next_nonce: AtomicU64,
    pub worker: Worker,
}

impl Client {
    pub fn new(settings: Settings) -> Arc<Client> {
        Arc::new(Client {
            settings,
            token_file: token_file(),
            sock: Mutex::new(None),
            generation: AtomicU64::new(0),
            ready: AtomicBool::new(false),
            voice: Mutex::new((false, false)),
            version: Version::default(),
            pending: Mutex::new(HashMap::new()),
            connecting: Mutex::new(()),
            next_nonce: AtomicU64::new(1),
            worker: Worker::new("discord-jobs"),
        })
    }

    pub fn ready(&self) -> bool {
        self.ready.load(Ordering::Relaxed)
    }

    pub fn version(&self) -> u64 {
        self.version.get()
    }

    /// (stumm, taub)
    pub fn voice(&self) -> (bool, bool) {
        *self.voice.lock().unwrap()
    }

    // ------------------------------------------------------------------------------ Transport
    fn send(&self, op: u32, payload: &Value) -> Result<(), DiscordError> {
        let mut s = self.sock.lock().unwrap();
        let sock = s.as_mut().ok_or_else(|| err(tr!("Connection to Discord lost")))?;
        sock.write_all(&encode_frame(op, payload)).map_err(|e| err(e.to_string()))
    }

    fn recv(sock: &mut UnixStream) -> Result<(u32, Value), DiscordError> {
        let mut h = [0u8; 8];
        sock.read_exact(&mut h).map_err(|_| err(tr!("Discord closed the connection")))?;
        let (op, n) = decode_header(&h);
        let mut buf = vec![0u8; n as usize];
        sock.read_exact(&mut buf).map_err(|_| err(tr!("Discord closed the connection")))?;
        let v = if buf.is_empty() { json!({}) } else { serde_json::from_slice(&buf).map_err(|e| err(e.to_string()))? };
        Ok((op, v))
    }

    fn reader(self: Arc<Self>, mut sock: UnixStream, generation: u64) {
        while self.generation.load(Ordering::Relaxed) == generation {
            let Ok((op, msg)) = Self::recv(&mut sock) else { break };
            match op {
                OP_PING => {
                    let _ = self.send(OP_PONG, &msg);
                }
                OP_CLOSE => break,
                _ => {
                    if let Some(nonce) = msg.get("nonce").and_then(Value::as_str) {
                        if let Some(reply) = self.pending.lock().unwrap().remove(nonce) {
                            let _ = reply.send(Ok(msg));
                        }
                    } else if msg.get("evt").and_then(Value::as_str) == Some("VOICE_SETTINGS_UPDATE") {
                        self.apply_voice(msg.get("data").unwrap_or(&Value::Null));
                    }
                }
            }
        }
        if self.generation.load(Ordering::Relaxed) == generation {
            self.disconnect();
        }
    }

    /// Verbindung trennen; offene Befehle scheitern.
    pub fn disconnect(&self) {
        self.generation.fetch_add(1, Ordering::Relaxed);
        self.ready.store(false, Ordering::Relaxed);
        if let Some(s) = self.sock.lock().unwrap().take() {
            let _ = s.shutdown(std::net::Shutdown::Both); // weckt den Lese-Thread
        }
        let lost = tr!("Connection to Discord lost");
        for (_, reply) in self.pending.lock().unwrap().drain() {
            let _ = reply.send(Err(lost.clone()));
        }
        self.version.bump();
    }

    /// Befehl senden und auf die Antwort warten.
    pub fn command(&self, cmd: &str, args: Value, timeout: Duration, evt: Option<&str>) -> Result<Value, DiscordError> {
        let nonce = format!("fd{}-{}", std::process::id(), self.next_nonce.fetch_add(1, Ordering::Relaxed));
        let (tx, rx) = mpsc::sync_channel(1);
        self.pending.lock().unwrap().insert(nonce.clone(), tx);
        if let Err(e) = self.send(OP_FRAME, &command_message(cmd, args, &nonce, evt)) {
            self.pending.lock().unwrap().remove(&nonce);
            return Err(e);
        }
        let msg = match rx.recv_timeout(timeout) {
            Ok(Ok(m)) => m,
            Ok(Err(e)) => return Err(err(e)),
            Err(_) => {
                self.pending.lock().unwrap().remove(&nonce);
                return Err(err(tr!("No response from Discord ({command})", command = cmd)));
            }
        };
        if msg.get("evt").and_then(Value::as_str) == Some("ERROR") {
            let m = msg.get("data").and_then(|d| d.get("message")).and_then(Value::as_str).map(str::to_string)
                .unwrap_or_else(|| tr!("Discord error"));
            return Err(err(m));
        }
        Ok(msg.get("data").cloned().filter(|d| !d.is_null()).unwrap_or_else(|| json!({})))
    }

    // ------------------------------------------------------------------------------- Anmeldung
    pub fn connect(self: &Arc<Self>) -> Result<(), DiscordError> {
        let _guard = self.connecting.lock().unwrap();
        if self.ready() {
            return Ok(());
        }
        if self.settings.client_id.is_empty() {
            return Err(err(tr!("Discord app missing: enter Client ID and Client Secret under Settings → Connections → Discord")));
        }
        let cands = socket_paths();
        if cands.is_empty() {
            return Err(err(tr!("Discord is not running (no IPC socket found)")));
        }
        let mut last = String::new();
        let sock = cands.iter().find_map(|p| UnixStream::connect(p).map_err(|e| last = e.to_string()).ok())
            .ok_or_else(|| err(tr!("Discord not reachable: {error}", error = last)))?;
        let mut reader = sock.try_clone().map_err(|e| err(e.to_string()))?;
        let generation = self.generation.fetch_add(1, Ordering::Relaxed) + 1;
        *self.sock.lock().unwrap() = Some(sock);
        let res = (|| {
            self.send(OP_HANDSHAKE, &json!({"v": 1, "client_id": self.settings.client_id}))?;
            reader.set_read_timeout(Some(Duration::from_secs(10))).map_err(|e| err(e.to_string()))?;
            let (op, msg) = Self::recv(&mut reader)?;
            if op == OP_CLOSE || msg.get("evt").and_then(Value::as_str) != Some("READY") {
                return Err(err(msg.get("message").and_then(Value::as_str).map(str::to_string)
                    .unwrap_or_else(|| tr!("Discord refused the connection"))));
            }
            reader.set_read_timeout(None).map_err(|e| err(e.to_string()))?;
            let me = self.clone();
            std::thread::Builder::new().name("discord".into()).spawn(move || me.reader(reader, generation))
                .map_err(|e| err(e.to_string()))?;
            self.authenticate()
        })();
        if let Err(e) = res {
            self.disconnect(); // Socket zu, der Lese-Thread endet damit
            return Err(e);
        }
        self.ready.store(true, Ordering::Relaxed);
        self.version.bump();
        match self.command("GET_VOICE_SETTINGS", json!({}), Duration::from_secs(5), None)
            .and_then(|v| {
                self.apply_voice(&v);
                self.command("SUBSCRIBE", json!({}), Duration::from_secs(5), Some("VOICE_SETTINGS_UPDATE"))
            }) {
            Ok(_) => {}
            Err(e) => log::debug!("{}", tr!("Voice status: {error}", error = e)),
        }
        log::info!("{}", tr!("Connected to Discord"));
        Ok(())
    }

    fn authenticate(&self) -> Result<(), DiscordError> {
        let mut tok = self.load_token();
        let expired = tok.as_ref().map(|t| t.get("expires_at").and_then(Value::as_f64).unwrap_or(0.0) < now() + 60.0).unwrap_or(false);
        let refresh = tok.as_ref().and_then(|t| t.get("refresh_token")).and_then(Value::as_str).map(str::to_string);
        if expired {
            if let Some(r) = refresh {
                match self.token_request(&[("grant_type", "refresh_token"), ("refresh_token", &r)]) {
                    Ok(t) => tok = Some(t),
                    Err(DiscordError::Auth(e)) => {
                        // Zugang widerrufen: neu anmelden statt bei jedem Druck zu scheitern
                        log::info!("{}", tr!("Saved Discord access is invalid ({error}) – logging in again", error = e));
                        let _ = std::fs::remove_file(&self.token_file);
                        tok = None;
                    }
                    Err(e) => return Err(e),
                }
            }
        }
        if let Some(access) = tok.as_ref().and_then(|t| t.get("access_token")).and_then(Value::as_str) {
            match self.command("AUTHENTICATE", json!({"access_token": access}), Duration::from_secs(5), None) {
                Ok(_) => return Ok(()),
                Err(e) => log::info!("{}", tr!("Saved Discord access is invalid ({error}) – logging in again", error = e)),
            }
        }
        let data = self.command("AUTHORIZE", json!({"client_id": self.settings.client_id, "scopes": SCOPES}),
                                Duration::from_secs(120), None)?;
        let code = data.get("code").and_then(Value::as_str).unwrap_or("").to_string();
        let tok = self.token_request(&[("grant_type", "authorization_code"), ("code", &code), ("redirect_uri", REDIRECT_URI)])?;
        let access = tok.get("access_token").and_then(Value::as_str).unwrap_or("");
        self.command("AUTHENTICATE", json!({"access_token": access}), Duration::from_secs(5), None).map(|_| ())
    }

    fn load_token(&self) -> Option<Value> {
        let t = std::fs::read_to_string(&self.token_file).ok()?;
        serde_json::from_str::<Value>(&t).ok().filter(Value::is_object)
    }

    fn client_secret(&self) -> Option<String> {
        if !self.settings.client_secret.is_empty() {
            return Some(self.settings.client_secret.clone());
        }
        if self.settings.in_keyring {
            return secrets::lookup("discord.client_secret");
        }
        None
    }

    /// Zugang bei Discord holen bzw. erneuern und speichern (Rechte 0600).
    fn token_request(&self, fields: &[(&str, &str)]) -> Result<Value, DiscordError> {
        let secret = self.client_secret()
            .ok_or_else(|| err(tr!("Client Secret missing – enter it under Settings → Connections → Discord")))?;
        let mut all: Vec<(&str, &str)> = fields.to_vec();
        all.push(("client_id", &self.settings.client_id));
        all.push(("client_secret", &secret));
        let failed = |e: &dyn std::fmt::Display| tr!("Discord login failed: {error}", error = e);
        let resp = net::agent(Duration::from_secs(10), true).post(TOKEN_URL)
            .header("Content-Type", "application/x-www-form-urlencoded")
            .send(net::urlencode(&all).as_bytes())
            .map_err(|e| err(failed(&e)))?;
        let code = resp.status().as_u16();
        let body = resp.into_body().read_to_string().map_err(|e| err(failed(&e)))?;
        if !(200..300).contains(&code) {
            let msg = failed(&format!("HTTP Error {code}"));
            // invalid_grant, invalid_client
            return Err(if code == 400 || code == 401 { DiscordError::Auth(msg) } else { err(msg) });
        }
        let mut tok: Value = serde_json::from_str(&body).map_err(|e| err(failed(&e)))?;
        let expires_in = tok.get("expires_in").and_then(Value::as_f64).unwrap_or(0.0);
        tok["expires_at"] = json!(now() + expires_in);
        if let Some(dir) = self.token_file.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        let _ = freestream_core::config::write_atomic(&self.token_file, &tok.to_string(), 0o600);
        Ok(tok)
    }

    // ---------------------------------------------------------------------------------- Befehle
    fn apply_voice(&self, data: &Value) {
        let mut v = self.voice.lock().unwrap();
        let old = *v;
        if let Some(m) = data.get("mute").and_then(Value::as_bool) {
            v.0 = m;
        }
        if let Some(d) = data.get("deaf").and_then(Value::as_bool) {
            v.1 = d;
        }
        if *v != old {
            self.version.bump();
        }
    }

    /// Sprach-Einstellungen setzen, z. B. `{"mute": true}`.
    pub fn set_voice(self: &Arc<Self>, args: Value) -> Result<(), DiscordError> {
        self.connect()?;
        let v = self.command("SET_VOICE_SETTINGS", args, Duration::from_secs(5), None)?;
        self.apply_voice(&v);
        Ok(())
    }

    /// „mute“ bzw. „deaf“ umschalten.
    pub fn toggle(self: &Arc<Self>, key: &str) -> Result<(), DiscordError> {
        self.connect()?;
        let (mute, deaf) = self.voice();
        let cur = if key == "deaf" { deaf } else { mute };
        self.set_voice(json!({ key: !cur }))
    }

    /// Sprachkanal betreten (leer = auflegen).
    pub fn select_voice_channel(self: &Arc<Self>, channel_id: &str) -> Result<(), DiscordError> {
        self.connect()?;
        let id = if channel_id.is_empty() { Value::Null } else { Value::from(channel_id) };
        self.command("SELECT_VOICE_CHANNEL", json!({"channel_id": id, "force": true}), Duration::from_secs(10), None).map(|_| ())
    }

    pub fn select_text_channel(self: &Arc<Self>, channel_id: &str) -> Result<(), DiscordError> {
        self.connect()?;
        let id = if channel_id.is_empty() { Value::Null } else { Value::from(channel_id) };
        self.command("SELECT_TEXT_CHANNEL", json!({"channel_id": id}), Duration::from_secs(10), None).map(|_| ())
    }
}

static CLIENT: Mutex<Option<Arc<Client>>> = Mutex::new(None);

/// Gemeinsamer Client je Einstellung (blockiert nicht; verbunden wird erst beim Tastendruck).
pub fn client(cfg: &Map<String, Value>) -> Arc<Client> {
    let settings = Settings::from_cfg(cfg);
    let mut cur = CLIENT.lock().unwrap();
    if let Some(c) = cur.as_ref() {
        if c.settings == settings {
            return c.clone();
        }
        c.disconnect(); // alte Verbindung (und ihren Lese-Thread) nicht liegen lassen
    }
    let c = Client::new(settings);
    *cur = Some(c.clone());
    c
}

pub fn current() -> Option<Arc<Client>> {
    CLIENT.lock().unwrap().clone()
}

/// Datei mit dem gespeicherten Discord-Zugang.
pub fn token_file() -> PathBuf {
    paths::state_dir().join("discord-token.json")
}

/// Ist ein Discord-Zugang gespeichert (angemeldet)?
pub fn logged_in() -> bool {
    token_file().is_file()
}

/// Abmelden (Einstellungen › Verbindungen): Verbindung trennen, gespeicherten Zugang löschen.
pub fn logout() {
    if let Some(c) = current() {
        c.disconnect();
    }
    let _ = std::fs::remove_file(token_file());
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frames() {
        let f = encode_frame(OP_HANDSHAKE, &json!({"v": 1, "client_id": "42"}));
        let (op, n) = decode_header(f[..8].try_into().unwrap());
        assert_eq!((op, n as usize), (OP_HANDSHAKE, f.len() - 8));
        assert_eq!(serde_json::from_slice::<Value>(&f[8..]).unwrap(), json!({"v": 1, "client_id": "42"}));
        assert_eq!(&f[..4], &[0, 0, 0, 0]);
    }

    #[test]
    fn commands() {
        assert_eq!(command_message("SET_VOICE_SETTINGS", json!({"mute": true}), "n1", None),
                   json!({"cmd": "SET_VOICE_SETTINGS", "args": {"mute": true}, "nonce": "n1"}));
        assert_eq!(command_message("SUBSCRIBE", Value::Null, "n2", Some("VOICE_SETTINGS_UPDATE")),
                   json!({"cmd": "SUBSCRIBE", "args": {}, "nonce": "n2", "evt": "VOICE_SETTINGS_UPDATE"}));
    }

    #[test]
    fn settings_and_voice() {
        let cfg = json!({"discord": {"client_id": 123456, "client_secret": ""}, "in_keyring": ["discord.client_secret"]});
        let s = Settings::from_cfg(cfg.as_object().unwrap());
        assert_eq!(s, Settings { client_id: "123456".into(), client_secret: String::new(), in_keyring: true });
        let c = Client::new(s);
        c.apply_voice(&json!({"mute": true}));
        assert_eq!(c.voice(), (true, false));
        assert!(c.version() > 0);
        assert!(!c.ready());
    }
}
