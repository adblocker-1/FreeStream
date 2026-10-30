//! Plugin-Host (wie `PluginHost` in `freestream/plugins.py`): startet die Plugins, spricht mit ihnen
//! über das WebSocket-Protokoll des Elgato-SDK und liefert ihre Einstellungsseiten (Property
//! Inspector) per HTTP auf demselben Port aus.
//!
//! * Ereignisse an das Plugin: keyDown/keyUp, willAppear/willDisappear, dialRotate/dialDown/dialUp,
//!   touchTap, deviceDidConnect/-Disconnect, didReceiveSettings, didReceiveGlobalSettings,
//!   propertyInspectorDidAppear/-Disappear, sendToPlugin
//! * Befehle vom Plugin: setTitle, setImage, setState, showAlert, showOk, setSettings, getSettings,
//!   setGlobalSettings, getGlobalSettings, openUrl, logMessage, switchToProfile, setFeedback,
//!   setFeedbackLayout (ohne Wirkung), sendToPropertyInspector
//!
//! Jede Verbindung hat einen eigenen Thread, der Nachrichten liest (mit kurzer Wartezeit) und
//! zwischendurch alles sendet, was andere Threads über ihren Kanal ([`Conn`]) eingereiht haben.

use freestream_core::profiles::ProfileStore;
use freestream_core::{paths, tr};
use image::RgbaImage;
use serde_json::{json, Map, Value};
use std::collections::{HashMap, HashSet};
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use super::{decode_image, installed, plugin_for_action, PiSlot, PluginInfo, Runtime, DEVICE_TYPES, SDK_APP_VERSION};
use crate::actions::{Command, Outbox};
use crate::system;

/// So lange nach willDisappear gelten Befehle eines Plugins für die Taste noch (z. B. setSettings nach
/// keyDown, Einstellungsseite gerade im Aufbau); danach wird der Kontext vergessen.
const HIDDEN_GRACE: Duration = Duration::from_secs(60);
/// Neustarts eines abgestürzten Plugins
const MAX_RESTARTS: u32 = 5;
/// Wartezeit beim Lesen einer Verbindung – so oft werden auch eingereihte Nachrichten gesendet
const POLL: Duration = Duration::from_millis(15);

// ================================================================================== Kontexte
/// Eine Aktion, wie das Plugin sie kennt (ein „context“ im SDK). Gehört gemeinsam dem Host und dem
/// [`super::PluginBehavior`] der Taste.
#[derive(Default)]
pub struct CtxState {
    pub id: String,
    /// UUID der Aktion
    pub action: String,
    /// Daten der Aktion aus dem Seiten-Manifest (Settings stehen in „Settings“)
    pub data: Map<String, Value>,
    pub page_dir: PathBuf,
    pub state: usize,
    pub dial: bool,
    /// Spalte/Zeile; None = Schritt einer Multi-Aktion
    pub coords: Option<(u8, u8)>,
    /// nur für die Einstellungsseite angelegt (nicht auf einem Gerät zu sehen)
    pub detached: bool,
    /// Seriennummer des Geräts
    pub device: String,
    /// vom Plugin gesetzt: Titel, Bild, Werte im Touchstreifen, Symbol im Touchstreifen
    pub title: Option<String>,
    pub image: Option<RgbaImage>,
    pub feedback: Map<String, Value>,
    pub encoder_icon: Option<RgbaImage>,
    /// Befehle an die Steuerung (Zustand, Fertig/Fehler)
    pub outbox: Option<Outbox>,
    /// neu zeichnen
    pub dirty: bool,
}

impl CtxState {
    pub fn settings(&self) -> Map<String, Value> {
        self.data.get("Settings").and_then(Value::as_object).cloned().unwrap_or_default()
    }

    fn state_count(&self) -> usize {
        self.data.get("States").and_then(Value::as_array).map(|a| a.len()).unwrap_or(1).max(1)
    }

    fn coordinates(&self) -> Value {
        let (c, r) = self.coords.unwrap_or((0, 0));
        json!({"column": c, "row": r})
    }

    fn controller(&self) -> &'static str {
        if self.dial { "Encoder" } else { "Keypad" }
    }

    fn post(&self, cmd: Command) {
        if let Some(o) = &self.outbox {
            o.send(cmd);
        }
    }
}

pub type Ctx = Arc<Mutex<CtxState>>;

/// Gerät, wie es den Plugins gemeldet wird.
#[derive(Debug, Clone, PartialEq)]
pub struct DeviceInfo {
    pub serial: String,
    pub model: String,
    pub cols: u8,
    pub rows: u8,
}

impl DeviceInfo {
    fn json(&self) -> Value {
        let code: String = self.model.chars().take(5).collect();
        let kind = DEVICE_TYPES.iter().find(|(c, _)| *c == code).map(|(_, t)| *t).unwrap_or(0);
        json!({"id": self.serial, "name": freestream_core::models::elgato_name(&self.model),
               "size": {"columns": self.cols, "rows": self.rows}, "type": kind})
    }
}

// =============================================================================== Verbindungen
enum Out {
    Text(String),
    Close,
}

/// Sendeseite einer WebSocket-Verbindung (thread-sicher, blockiert nicht).
#[derive(Clone)]
struct Conn {
    id: u64,
    tx: mpsc::Sender<Out>,
}

impl Conn {
    fn send(&self, msg: &Value) -> bool {
        self.tx.send(Out::Text(msg.to_string())).is_ok()
    }

    fn close(&self) {
        let _ = self.tx.send(Out::Close);
    }
}

fn next_conn_id() -> u64 {
    static N: AtomicU64 = AtomicU64::new(1);
    N.fetch_add(1, Ordering::Relaxed)
}

// ================================================================================== Prozesse
/// Ein laufendes Plugin.
struct Proc {
    info: Arc<PluginInfo>,
    child: Mutex<Option<Child>>,
    /// zählt die Starts: ein Wächter-Thread gehört nur zu „seinem“ Start
    generation: AtomicU64,
    conn: Mutex<Option<Conn>>,
    restarts: AtomicU32,
    stopping: AtomicBool,
}

impl Proc {
    fn new(info: Arc<PluginInfo>) -> Arc<Self> {
        Arc::new(Proc {
            info,
            child: Mutex::new(None),
            generation: AtomicU64::new(0),
            conn: Mutex::new(None),
            restarts: AtomicU32::new(0),
            stopping: AtomicBool::new(false),
        })
    }

    /// Ohne Verbindung wird verworfen – nach dem Anmelden gibt es ohnehin willAppear für alles Sichtbare.
    fn send(&self, msg: &Value) {
        let conn = self.conn.lock().unwrap().clone();
        if let Some(c) = conn {
            if !c.send(msg) {
                let mut g = self.conn.lock().unwrap();
                if g.as_ref().map(|x| x.id) == Some(c.id) {
                    *g = None;
                }
            }
        }
    }

    fn connected(&self) -> bool {
        self.conn.lock().unwrap().is_some()
    }

    fn stop(&self) {
        self.stopping.store(true, Ordering::Relaxed);
        if let Some(c) = self.conn.lock().unwrap().take() {
            c.close();
        }
        let child = self.child.lock().unwrap().take();
        if let Some(mut child) = child {
            if matches!(child.try_wait(), Ok(None)) {
                unsafe {
                    libc::kill(child.id() as libc::pid_t, libc::SIGTERM);
                }
                let start = Instant::now();
                while start.elapsed() < Duration::from_secs(3) {
                    if !matches!(child.try_wait(), Ok(None)) {
                        return;
                    }
                    std::thread::sleep(Duration::from_millis(50));
                }
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
}

// ====================================================================================== Host
/// Einstellungen des Hosts (Vorgaben aus den Standardordnern und der Konfiguration).
#[derive(Debug, Clone)]
pub struct HostOptions {
    pub plugins_dir: PathBuf,
    /// globale Einstellungen der Plugins (`<UUID>.json`)
    pub plugin_data_dir: PathBuf,
    /// Ausgabe der Plugins (`<UUID>.log`)
    pub log_dir: PathBuf,
    pub profiles_dir: PathBuf,
    /// Node.js; None = nicht gefunden
    pub node: Option<PathBuf>,
    /// Port und Schlüssel in `plugin-host.json` ablegen (für Editor und Einstellungsseiten)
    pub publish: bool,
    /// Plugins gleich starten
    pub autostart: bool,
}

impl Default for HostOptions {
    fn default() -> Self {
        let cfg = freestream_core::config::Config::load();
        HostOptions {
            plugins_dir: paths::plugins_dir(),
            plugin_data_dir: paths::plugin_data_dir(),
            log_dir: paths::state_dir().join("plugins"),
            profiles_dir: paths::profiles_dir(),
            node: super::find_node(Some(&cfg.data)),
            publish: true,
            autostart: true,
        }
    }
}

/// Zustand eines Plugins in der Liste (wie `pluginui.status`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PluginStatus {
    /// läuft unter Linux nicht (Grund)
    NotRunnable(String),
    /// installiert, kein Host im Prozess
    Ready,
    NodeMissing,
    Launching,
    Exited,
    Starting,
    Running,
}

/// Farbe des Zustands (Farbnamen des Themes der Python-Version).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StatusColor {
    Danger,
    Muted,
    Warn,
    Ok,
}

impl PluginStatus {
    pub fn text(&self) -> String {
        match self {
            PluginStatus::NotRunnable(r) => r.clone(),
            PluginStatus::Ready => tr!("ready"),
            PluginStatus::NodeMissing => tr!("Node.js missing (package nodejs)"),
            PluginStatus::Launching => tr!("launching …"),
            PluginStatus::Exited => tr!("exited – see log"),
            PluginStatus::Starting => tr!("starting …"),
            PluginStatus::Running => tr!("running"),
        }
    }

    pub fn color(&self) -> StatusColor {
        match self {
            PluginStatus::NotRunnable(_) | PluginStatus::Exited => StatusColor::Danger,
            PluginStatus::Ready => StatusColor::Muted,
            PluginStatus::NodeMissing | PluginStatus::Launching | PluginStatus::Starting => StatusColor::Warn,
            PluginStatus::Running => StatusColor::Ok,
        }
    }
}

#[derive(Default)]
struct Shared {
    /// Kontext -> Aktion
    contexts: HashMap<String, Ctx>,
    /// Kontexte, die gerade angezeigt werden
    visible: HashSet<String>,
    /// Kontext -> seit wann ausgeblendet (älteste zuerst)
    hidden: Vec<(String, Instant)>,
    /// Kontext -> Verbindung der Einstellungsseite
    pis: HashMap<String, Conn>,
}

type Switcher = Arc<dyn Fn(&str, &str) + Send + Sync>;

struct Inner {
    opts: HostOptions,
    port: u16,
    token: String,
    /// Plugin-UUID -> Prozess (None = läuft unter Linux nicht)
    procs: Mutex<HashMap<String, Option<Arc<Proc>>>>,
    stamps: Mutex<HashMap<String, (String, Option<i128>)>>,
    shared: Mutex<Shared>,
    devices: Mutex<Vec<DeviceInfo>>,
    /// nur für sync(): das Beenden eines Plugins dauert bis zu 3 s
    sync_lock: Mutex<()>,
    stopped: AtomicBool,
    switcher: Mutex<Option<Switcher>>,
}

/// Schnittstelle zwischen Geräten und Elgato-Plugins.
#[derive(Clone)]
pub struct PluginHost {
    inner: Arc<Inner>,
}

fn global() -> &'static Mutex<Option<PluginHost>> {
    static H: OnceLock<Mutex<Option<PluginHost>>> = OnceLock::new();
    H.get_or_init(Default::default)
}

/// Plugin-Host dieses Prozesses (vom Dienst gesetzt).
pub fn host() -> Option<PluginHost> {
    global().lock().unwrap().clone()
}

/// Plugin-Host dieses Prozesses setzen (None beim Beenden).
pub fn set_host(h: Option<PluginHost>) {
    *global().lock().unwrap() = h;
}

fn random_token() -> String {
    let mut buf = [0u8; 18];
    let ok = std::fs::File::open("/dev/urandom").and_then(|mut f| f.read_exact(&mut buf)).is_ok();
    if !ok {
        for b in buf.iter_mut() {
            *b = fastrand::u8(..);
        }
    }
    data_encoding::BASE64URL_NOPAD.encode(&buf)
}

impl PluginHost {
    /// Host mit Standardeinstellungen starten (Server, `plugin-host.json`, Plugins).
    pub fn start_default() -> anyhow::Result<PluginHost> {
        Self::start(HostOptions::default())
    }

    /// WebSocket-/HTTP-Server auf 127.0.0.1 öffnen und (mit `autostart`) die Plugins starten.
    pub fn start(opts: HostOptions) -> anyhow::Result<PluginHost> {
        let listener = TcpListener::bind("127.0.0.1:0")?;
        let port = listener.local_addr()?.port();
        let inner = Arc::new(Inner {
            opts,
            port,
            token: random_token(),
            procs: Default::default(),
            stamps: Default::default(),
            shared: Default::default(),
            devices: Default::default(),
            sync_lock: Mutex::new(()),
            stopped: AtomicBool::new(false),
            switcher: Mutex::new(None),
        });
        let host = PluginHost { inner };
        if host.inner.opts.publish {
            let f = super::host_state_file();
            let text = json!({"port": port, "token": host.inner.token, "pid": std::process::id()}).to_string();
            freestream_core::config::write_atomic(&f, &text, 0o600)?;
        }
        let h = host.clone();
        std::thread::Builder::new().name("plugin-host".into()).spawn(move || {
            for stream in listener.incoming() {
                if h.inner.stopped.load(Ordering::Relaxed) {
                    break;
                }
                let Ok(stream) = stream else { continue };
                let h2 = h.clone();
                let _ = std::thread::Builder::new().name("plugin-conn".into()).spawn(move || h2.serve(stream));
            }
        })?;
        if host.inner.opts.autostart {
            host.sync();
        }
        Ok(host)
    }

    pub fn port(&self) -> u16 {
        self.inner.port
    }

    pub fn token(&self) -> &str {
        &self.inner.token
    }

    pub fn node(&self) -> Option<&Path> {
        self.inner.opts.node.as_deref()
    }

    /// Adresse der Einstellungsseite einer Aktion im Profil (dieser Host).
    pub fn pi_url(&self, profile_id: &str, page_uuid: &str, slot: PiSlot) -> String {
        super::pi_url_for(self.inner.port, &self.inner.token, profile_id, page_uuid, slot)
    }

    /// Wer `switchToProfile` ausführt: (Seriennummer oder leer = alle Geräte, Profil-ID oder -Name).
    pub fn set_profile_switcher(&self, f: impl Fn(&str, &str) + Send + Sync + 'static) {
        *self.inner.switcher.lock().unwrap() = Some(Arc::new(f));
    }

    // ------------------------------------------------------------------------------- Plugins
    /// Startet neu installierte Plugins, startet aktualisierte neu und beendet entfernte.
    pub fn sync(&self) {
        let _g = self.inner.sync_lock.lock().unwrap();
        let plugins = installed(Some(&self.inner.opts.plugins_dir));
        for (uid, info) in &plugins {
            let stamp = info.stamp();
            let changed = self.inner.stamps.lock().unwrap().get(uid).map(|s| *s != stamp).unwrap_or(false);
            if changed {
                // neue Fassung über die laufende installiert: das alte Programm liefe sonst im ersetzten Ordner weiter
                let old = self.inner.procs.lock().unwrap().remove(uid).flatten();
                if let Some(p) = old {
                    p.stop();
                }
            }
            self.inner.stamps.lock().unwrap().insert(uid.clone(), stamp);
            let known = self.inner.procs.lock().unwrap().contains_key(uid);
            if known {
                continue;
            }
            if !info.runnable() {
                log::info!("{}", tr!("Plugin {name} will not be started: {reason}", name = info.name, reason = info.reason));
                self.inner.procs.lock().unwrap().insert(uid.clone(), None);
                continue;
            }
            let p = Proc::new(info.clone());
            self.inner.procs.lock().unwrap().insert(uid.clone(), Some(p.clone()));
            self.start_proc(&p);
        }
        let gone: Vec<String> = self.inner.procs.lock().unwrap().keys().filter(|u| !plugins.contains_key(*u)).cloned().collect();
        for uid in gone {
            let p = self.inner.procs.lock().unwrap().remove(&uid).flatten();
            self.inner.stamps.lock().unwrap().remove(&uid);
            if let Some(p) = p {
                p.stop();
            }
        }
    }

    /// Plugin beenden und neu starten (Knopf „Neu starten“).
    pub fn restart(&self, plugin_uuid: &str) {
        let p = self.inner.procs.lock().unwrap().remove(plugin_uuid).flatten();
        if let Some(p) = p {
            p.stop();
        }
        self.sync();
    }

    /// Befehlszeile eines Plugins wie von der Elgato-App.
    pub fn command_line(&self, info: &PluginInfo) -> Result<Vec<String>, String> {
        let exe = info.dir.join(&info.code_path).to_string_lossy().to_string();
        let mut cmd = match info.runtime {
            Some(Runtime::Node) => match &self.inner.opts.node {
                Some(n) => vec![n.to_string_lossy().to_string(), exe],
                None => return Err(tr!("Node.js not found (install the “nodejs” package or set node_path)")),
            },
            _ => vec![exe],
        };
        cmd.extend(["-port".into(), self.inner.port.to_string(), "-pluginUUID".into(), info.uuid.clone(),
                    "-registerEvent".into(), "registerPlugin".into(), "-info".into(), self.info_for(info).to_string()]);
        Ok(cmd)
    }

    fn start_proc(&self, p: &Arc<Proc>) -> bool {
        let cmd = match self.command_line(&p.info) {
            Ok(c) => c,
            Err(e) => {
                log::warn!("Plugin {}: {e}", p.info.name);
                return false;
            }
        };
        let _ = std::fs::create_dir_all(&self.inner.opts.log_dir);
        let log = std::fs::OpenOptions::new().create(true).append(true).open(self.inner.opts.log_dir.join(format!("{}.log", p.info.uuid)));
        let (out, err) = match log.and_then(|f| Ok((f.try_clone()?, f))) {
            Ok((a, b)) => (Stdio::from(a), Stdio::from(b)),
            Err(_) => (Stdio::null(), Stdio::null()),
        };
        use std::os::unix::process::CommandExt;
        let res = std::process::Command::new(&cmd[0]).args(&cmd[1..]).current_dir(&p.info.dir)
            .stdin(Stdio::null()).stdout(out).stderr(err).process_group(0).spawn();
        match res {
            Ok(child) => {
                *p.child.lock().unwrap() = Some(child);
                let gen = p.generation.fetch_add(1, Ordering::SeqCst) + 1;
                log::info!("{}", tr!("Plugin started: {name} {version}", name = p.info.name, version = p.info.version));
                let (h, p2) = (self.clone(), p.clone());
                let _ = std::thread::Builder::new().name("plugin-watch".into()).spawn(move || h.watch(p2, gen));
                true
            }
            Err(e) => {
                log::warn!("{}", tr!("Plugin {name} cannot be started: {error}", name = p.info.name, error = e));
                false
            }
        }
    }

    /// Prozess überwachen: beendet er sich unerwartet, nach kurzer Pause neu starten (höchstens 5-mal).
    fn watch(&self, p: Arc<Proc>, gen: u64) {
        let code = loop {
            std::thread::sleep(Duration::from_millis(200));
            if p.stopping.load(Ordering::Relaxed) || p.generation.load(Ordering::SeqCst) != gen {
                return;
            }
            let mut g = p.child.lock().unwrap();
            match g.as_mut().map(|c| c.try_wait()) {
                Some(Ok(Some(st))) => break st.code().map(|c| c.to_string()).unwrap_or_else(|| "-".into()),
                Some(Ok(None)) => continue,
                _ => return,
            }
        };
        *p.conn.lock().unwrap() = None;
        log::warn!("{}", tr!("Plugin {name} exited (code {code})", name = p.info.name, code = code));
        let n = p.restarts.load(Ordering::Relaxed);
        if n < MAX_RESTARTS {
            p.restarts.store(n + 1, Ordering::Relaxed);
            let wait = Duration::from_secs((1u64 << (n + 1)).min(30));
            let start = Instant::now();
            while start.elapsed() < wait {
                if p.stopping.load(Ordering::Relaxed) || self.inner.stopped.load(Ordering::Relaxed) {
                    return;
                }
                std::thread::sleep(Duration::from_millis(100));
            }
            self.start_proc(&p);
        }
    }

    /// Zustand eines Plugins (für die Plugin-Liste).
    pub fn status(&self, info: &PluginInfo) -> PluginStatus {
        if !info.runnable() {
            return PluginStatus::NotRunnable(info.reason.clone());
        }
        if info.runtime == Some(Runtime::Node) && self.inner.opts.node.is_none() {
            return PluginStatus::NodeMissing;
        }
        let p = self.inner.procs.lock().unwrap().get(&info.uuid).cloned().flatten();
        let Some(p) = p else { return PluginStatus::Launching };
        let mut g = p.child.lock().unwrap();
        let Some(child) = g.as_mut() else { return PluginStatus::Launching };
        if !matches!(child.try_wait(), Ok(None)) {
            return PluginStatus::Exited;
        }
        drop(g);
        if !p.connected() {
            return PluginStatus::Starting;
        }
        PluginStatus::Running
    }

    /// `-info` für ein Plugin (Anwendung, Plugin, Farben, Geräte).
    pub fn info_for(&self, info: &PluginInfo) -> Value {
        let devices: Vec<Value> = self.inner.devices.lock().unwrap().iter().map(DeviceInfo::json).collect();
        let lang = freestream_core::i18n::current();
        let release = std::fs::read_to_string("/proc/sys/kernel/osrelease").unwrap_or_default();
        json!({
            "application": {"font": "", "language": lang.split('_').next().unwrap_or("en"), "platform": "linux",
                            "platformVersion": release.trim(), "version": SDK_APP_VERSION},
            "plugin": {"uuid": info.uuid, "version": info.version},
            "devicePixelRatio": 2,
            "colors": {"buttonPressedBackgroundColor": "#303030FF", "buttonPressedBorderColor": "#646464FF",
                       "buttonPressedTextColor": "#969696FF", "disabledColor": "#007AFF59",
                       "highlightColor": "#007AFFFF", "mouseDownColor": "#2EA8FFFF"},
            "devices": devices,
            "freedeck": env!("CARGO_PKG_VERSION"),
        })
    }

    /// Alles beenden: Plugins, Einstellungsseiten, Server, `plugin-host.json`.
    pub fn stop(&self) {
        if self.inner.stopped.swap(true, Ordering::Relaxed) {
            return;
        }
        let procs: Vec<Arc<Proc>> = self.inner.procs.lock().unwrap().drain().filter_map(|(_, p)| p).collect();
        for p in procs {
            p.stop();
        }
        for c in self.inner.shared.lock().unwrap().pis.drain().map(|(_, c)| c) {
            c.close();
        }
        // accept() aufwecken
        let _ = TcpStream::connect(("127.0.0.1", self.inner.port));
        if self.inner.opts.publish {
            let _ = std::fs::remove_file(super::host_state_file());
        }
    }

    // ------------------------------------------------------------------------------- Geräte
    /// Gerät angeschlossen: merken und den Plugins melden.
    pub fn device_connected(&self, dev: DeviceInfo) {
        {
            let mut d = self.inner.devices.lock().unwrap();
            d.retain(|x| x.serial != dev.serial);
            d.push(dev.clone());
        }
        let msg = json!({"event": "deviceDidConnect", "device": dev.serial, "deviceInfo": dev.json()});
        for p in self.running() {
            p.send(&msg);
        }
    }

    pub fn device_disconnected(&self, serial: &str) {
        self.inner.devices.lock().unwrap().retain(|x| x.serial != serial);
        let msg = json!({"event": "deviceDidDisconnect", "device": serial});
        for p in self.running() {
            p.send(&msg);
        }
    }

    pub fn devices(&self) -> Vec<DeviceInfo> {
        self.inner.devices.lock().unwrap().clone()
    }

    fn running(&self) -> Vec<Arc<Proc>> {
        self.inner.procs.lock().unwrap().values().filter_map(|p| p.clone()).collect()
    }

    /// Gerät für eine Seite: das mit dem Modell des Profils, sonst das erste.
    pub(crate) fn device_for_page(&self, page_dir: &Path) -> String {
        let devs = self.inner.devices.lock().unwrap().clone();
        if devs.len() > 1 {
            let model = page_dir.ancestors()
                .find(|d| d.extension().map(|e| e == "sdProfile").unwrap_or(false))
                .and_then(|root| freestream_core::profiles::read_json(&root.join("manifest.json")).ok())
                .and_then(|m| m.get("Device")?.get("Model")?.as_str().map(str::to_string))
                .unwrap_or_default();
            let base = |m: &str| m.split('-').next().unwrap_or("").trim_end_matches('L').to_string();
            if let Some(d) = devs.iter().find(|d| !model.is_empty() && base(&d.model) == base(&model)) {
                return d.serial.clone();
            }
        }
        devs.first().map(|d| d.serial.clone()).unwrap_or_default()
    }

    // ----------------------------------------------------------------------------- Kontexte
    fn proc_for(&self, action_uuid: &str) -> Option<Arc<Proc>> {
        let info = plugin_for_action(action_uuid, Some(&self.inner.opts.plugins_dir))?;
        self.inner.procs.lock().unwrap().get(&info.uuid).cloned().flatten()
    }

    /// Gehört die Aktion zu einem laufenden Plugin?
    pub fn handles(&self, action_uuid: &str) -> bool {
        self.proc_for(action_uuid).is_some()
    }

    /// Kontext anmelden (unter seiner ID).
    pub(crate) fn register(&self, ctx: &Ctx) -> String {
        let id = ctx.lock().unwrap().id.clone();
        self.inner.shared.lock().unwrap().contexts.insert(id.clone(), ctx.clone());
        id
    }

    pub fn context(&self, id: &str) -> Option<Ctx> {
        self.inner.shared.lock().unwrap().contexts.get(id).cloned()
    }

    fn payload(c: &CtxState) -> Map<String, Value> {
        let mut m = Map::new();
        m.insert("settings".into(), Value::Object(c.settings()));
        m.insert("coordinates".into(), c.coordinates());
        m.insert("state".into(), Value::from(c.state));
        m.insert("isInMultiAction".into(), Value::from(c.coords.is_none()));
        m.insert("controller".into(), Value::from(c.controller()));
        m
    }

    /// Ereignis an das Plugin einer Aktion; false, wenn das Plugin nicht läuft.
    fn event(&self, event: &str, ctx: &Ctx, extra: Value) -> bool {
        let (action, device, id, mut payload) = {
            let c = ctx.lock().unwrap();
            (c.action.clone(), c.device.clone(), c.id.clone(), Self::payload(&c))
        };
        let Some(p) = self.proc_for(&action) else { return false };
        if let Value::Object(e) = extra {
            payload.extend(e);
        }
        self.register(ctx);
        let visible = self.inner.shared.lock().unwrap().visible.contains(&id);
        if !visible {
            // z. B. Plugin-Aktion in einer Multi-Aktion: bekommt kein willDisappear, das sie vergessen ließe
            self.forget_later(&id);
        }
        p.send(&json!({"event": event, "action": action, "context": id, "device": device, "payload": payload}));
        true
    }

    fn forget_later(&self, id: &str) {
        {
            let mut s = self.inner.shared.lock().unwrap();
            s.hidden.retain(|(c, _)| c != id);
            s.hidden.push((id.to_string(), Instant::now()));
        }
        self.prune();
    }

    /// Ausgeblendete Kontexte nach [`HIDDEN_GRACE`] vergessen – nicht, solange sie zu sehen sind oder
    /// ihre Einstellungsseite offen ist.
    fn prune(&self) {
        let mut s = self.inner.shared.lock().unwrap();
        while s.hidden.first().map(|(_, t)| t.elapsed() > HIDDEN_GRACE).unwrap_or(false) {
            let (id, _) = s.hidden.remove(0);
            if !s.visible.contains(&id) && !s.pis.contains_key(&id) {
                s.contexts.remove(&id);
            }
        }
    }

    // ------------------------------------------------------------------ Aufrufe der Aktionen
    pub fn will_appear(&self, ctx: &Ctx) -> bool {
        let action = ctx.lock().unwrap().action.clone();
        if !self.handles(&action) {
            return false;
        }
        self.prune();
        let id = self.register(ctx);
        self.inner.shared.lock().unwrap().visible.insert(id);
        self.event("willAppear", ctx, Value::Null)
    }

    pub fn will_disappear(&self, ctx: &Ctx) -> bool {
        let (id, action) = {
            let c = ctx.lock().unwrap();
            (c.id.clone(), c.action.clone())
        };
        self.inner.shared.lock().unwrap().visible.remove(&id);
        let sent = self.handles(&action) && self.event("willDisappear", ctx, Value::Null);
        {
            // die Steuerung legt bei jedem Seitenwechsel neue Aktionen an: altes Bild nicht ewig behalten
            let mut c = ctx.lock().unwrap();
            c.image = None;
            c.encoder_icon = None;
            c.outbox = None;
        }
        self.forget_later(&id);
        sent
    }

    pub fn key_down(&self, ctx: &Ctx) -> bool {
        self.event("keyDown", ctx, Value::Null)
    }

    pub fn key_up(&self, ctx: &Ctx) -> bool {
        self.event("keyUp", ctx, Value::Null)
    }

    pub fn dial_rotate(&self, ctx: &Ctx, ticks: i32) -> bool {
        self.event("dialRotate", ctx, json!({"ticks": ticks, "pressed": false}))
    }

    pub fn dial_down(&self, ctx: &Ctx) -> bool {
        self.event("dialDown", ctx, Value::Null)
    }

    pub fn dial_up(&self, ctx: &Ctx) -> bool {
        self.event("dialUp", ctx, Value::Null)
    }

    pub fn touch_tap(&self, ctx: &Ctx, pos: (i32, i32), long: bool) -> bool {
        self.event("touchTap", ctx, json!({"tapPos": [pos.0, pos.1], "hold": long}))
    }

    // -------------------------------------------------------------------- Befehle der Plugins
    fn handle(&self, p: &Arc<Proc>, msg: &Value) {
        let ev = msg.get("event").and_then(Value::as_str).unwrap_or("");
        let id = msg.get("context").and_then(Value::as_str).unwrap_or("");
        let payload = msg.get("payload").cloned().unwrap_or(Value::Null);
        let pl = |k: &str| payload.get(k).cloned().unwrap_or(Value::Null);
        let ctx = if id.is_empty() { None } else { self.context(id) };
        match (ev, ctx) {
            ("setTitle", Some(c)) => {
                let mut c = c.lock().unwrap();
                c.title = match pl("title") {
                    Value::Null => None,
                    v => Some(super::value_text(&v)),
                };
                c.dirty = true;
            }
            ("setImage", Some(c)) => {
                let img = decode_image(pl("image").as_str().unwrap_or(""), Some(&p.info.dir), 144);
                let mut c = c.lock().unwrap();
                c.image = img;
                c.dirty = true;
            }
            ("setState", Some(c)) => {
                let s = pl("state").as_u64().or_else(|| pl("state").as_str().and_then(|s| s.parse().ok())).unwrap_or(0) as usize;
                let mut c = c.lock().unwrap();
                c.state = s % c.state_count();
                c.dirty = true;
                c.post(Command::SetState(c.state));
            }
            ("showAlert", Some(c)) => c.lock().unwrap().post(Command::Alert),
            ("showOk", Some(c)) => c.lock().unwrap().post(Command::Ok),
            ("setSettings", Some(c)) => {
                self.store_settings(&c, payload.as_object().cloned().unwrap_or_default());
                let pi = self.inner.shared.lock().unwrap().pis.get(id).cloned();
                if let Some(pi) = pi {
                    pi.send(&self.settings_msg(&c));
                }
            }
            ("getSettings", Some(c)) => p.send(&self.settings_msg(&c)),
            ("setGlobalSettings", _) => {
                self.set_global(&p.info.uuid, &payload);
                let pis: Vec<(String, Conn)> = self.inner.shared.lock().unwrap().pis.iter().map(|(k, v)| (k.clone(), v.clone())).collect();
                for (cid, pi) in pis {
                    let belongs = self.context(&cid).map(|c| c.lock().unwrap().action.clone())
                        .and_then(|a| self.proc_for(&a)).map(|q| Arc::ptr_eq(&q, p)).unwrap_or(false);
                    if belongs {
                        pi.send(&json!({"event": "didReceiveGlobalSettings", "payload": {"settings": payload}}));
                    }
                }
            }
            ("getGlobalSettings", _) => p.send(&json!({"event": "didReceiveGlobalSettings", "payload": {"settings": self.get_global(&p.info.uuid)}})),
            ("openUrl", _) => open_url(pl("url").as_str().unwrap_or("")),
            ("logMessage", _) => log::info!("[{}] {}", p.info.name, super::value_text(&pl("message"))),
            ("switchToProfile", _) => {
                let device = msg.get("device").and_then(Value::as_str).unwrap_or("").to_string();
                let profile = pl("profile").as_str().unwrap_or("").to_string();
                let sw = self.inner.switcher.lock().unwrap().clone();
                if let (Some(sw), false) = (sw, profile.is_empty()) {
                    // eigener Thread: die Steuerung des Geräts kann gerade beschäftigt sein
                    std::thread::spawn(move || sw(&device, &profile));
                }
            }
            ("setFeedback", Some(c)) => {
                let mut icon = None;
                if let Value::Object(m) = &payload {
                    let mut c = c.lock().unwrap();
                    for (k, v) in m {
                        if k == "icon" {
                            let s = v.as_str().map(str::to_string).or_else(|| v.get("value").and_then(Value::as_str).map(str::to_string)).unwrap_or_default();
                            icon = Some(decode_image(&s, Some(&p.info.dir), 96));
                        } else {
                            c.feedback.insert(k.clone(), v.clone());
                        }
                    }
                    if let Some(i) = icon {
                        c.encoder_icon = i;
                    }
                    c.dirty = true;
                }
            }
            ("sendToPropertyInspector", Some(c)) => {
                let pi = self.inner.shared.lock().unwrap().pis.get(id).cloned();
                if let Some(pi) = pi {
                    let action = c.lock().unwrap().action.clone();
                    pi.send(&json!({"event": "sendToPropertyInspector", "action": action, "context": id, "payload": payload}));
                }
            }
            ("setFeedbackLayout" | "setTriggerDescription", _) => {}
            _ => log::debug!("{}", tr!("Plugin {name}: unknown command {command}", name = p.info.name, command = ev)),
        }
    }

    /// Neue Settings übernehmen und im Seiten-Manifest speichern.
    fn store_settings(&self, ctx: &Ctx, settings: Map<String, Value>) {
        let (old, page_dir) = {
            let mut c = ctx.lock().unwrap();
            let old = c.data.clone();
            c.data.insert("Settings".into(), Value::Object(settings.clone()));
            (old, c.page_dir.clone())
        };
        if page_dir.as_os_str().is_empty() {
            return;
        }
        if let Err(e) = save_action_settings(&page_dir, &old, &settings) {
            log::warn!("{}", tr!("Settings not saved: {error}", error = e));
        }
    }

    fn global_file(&self, plugin_uuid: &str) -> PathBuf {
        self.inner.opts.plugin_data_dir.join(format!("{plugin_uuid}.json"))
    }

    fn set_global(&self, plugin_uuid: &str, data: &Value) {
        let f = self.global_file(plugin_uuid);
        if let Some(d) = f.parent() {
            let _ = std::fs::create_dir_all(d);
        }
        let _ = std::fs::write(f, data.to_string());
    }

    /// Globale Einstellungen eines Plugins ({} ohne Datei).
    pub fn get_global(&self, plugin_uuid: &str) -> Value {
        std::fs::read_to_string(self.global_file(plugin_uuid)).ok()
            .and_then(|t| serde_json::from_str(&t).ok())
            .unwrap_or_else(|| json!({}))
    }

    fn settings_msg(&self, ctx: &Ctx) -> Value {
        let c = ctx.lock().unwrap();
        json!({"event": "didReceiveSettings", "action": c.action, "context": c.id, "device": c.device,
               "payload": {"settings": c.settings(), "coordinates": c.coordinates(),
                           "isInMultiAction": c.coords.is_none(), "state": c.state}})
    }

    // -------------------------------------------------------------------- Property Inspector
    /// Aktion aus einem Profil für die Einstellungsseite bereitstellen (auch wenn sie nicht sichtbar ist).
    pub fn open_context(&self, profile_id: &str, page_uuid: &str, slot: PiSlot) -> Result<Ctx, String> {
        let store = ProfileStore::new(Some(&self.inner.opts.profiles_dir));
        let prof = store.get(profile_id).ok_or_else(|| tr!("Profile not found"))?;
        let page = prof.page(page_uuid).ok_or_else(|| tr!("Page not found"))?;
        let (data, coords, dial) = match slot {
            PiSlot::Dial(i) => (page.encoders.get(&i).cloned(), (i, 0), true),
            PiSlot::Key(c, r) => (page.keypad.get(&(c, r)).cloned(), (c, r), false),
        };
        let data = data.ok_or_else(|| tr!("No action in this slot"))?;
        let uuid = data.get("UUID").and_then(Value::as_str).unwrap_or("").to_string();
        // Läuft die Aktion schon auf einem Gerät? Dann denselben Kontext nehmen (einen sichtbaren zuerst)
        let same: Vec<(String, Ctx, bool)> = {
            let s = self.inner.shared.lock().unwrap();
            s.contexts.iter().filter_map(|(id, c)| {
                let st = c.lock().unwrap();
                let aid = data.get("ActionID").filter(|v| super::truthy(Some(v)));
                let hit = st.page_dir == page.dir && st.action == uuid
                    && ((aid.is_some() && st.data.get("ActionID") == aid) || st.data == data);
                hit.then(|| (id.clone(), c.clone(), s.visible.contains(id)))
            }).collect()
        };
        if let Some((id, c, vis)) = same.iter().find(|x| x.2).or(same.last()).cloned() {
            if !vis {
                self.forget_later(&id); // Frist neu: die Einstellungsseite verbindet gleich
            }
            return Ok(c);
        }
        let ctx = new_ctx(&uuid, data, page.dir.clone(), dial, Some(coords));
        {
            let mut c = ctx.lock().unwrap();
            c.detached = true;
            c.device = self.device_for_page(&page.dir);
        }
        let id = self.register(&ctx);
        self.forget_later(&id); // falls die Einstellungsseite nie verbindet
        Ok(ctx)
    }

    /// Startskript wie in der Elgato-App (`connectElgatoStreamDeckSocket`) in die Seite einfügen.
    fn inject(&self, html: &[u8], info: &PluginInfo, ctx: &Ctx) -> Vec<u8> {
        let (id, action_info) = {
            let c = ctx.lock().unwrap();
            (c.id.clone(), json!({"action": c.action, "context": c.id, "device": c.device,
                "payload": {"settings": c.settings(), "coordinates": c.coordinates(), "controller": c.controller(), "state": c.state}}))
        };
        // JSON-Text als JS-String; "</" maskieren, damit nichts aus dem <script> ausbrechen kann
        let js = |v: &Value| Value::String(v.to_string()).to_string().replace("</", "<\\/");
        let boot = format!(
            "<script>/* FreeDeck: Start wie in der Elgato-App */(function(){{\
             var port={},uuid={},info={},ai={};\
             function go(){{if(typeof window.connectElgatoStreamDeckSocket==='function'){{\
             window.connectElgatoStreamDeckSocket(port,uuid,'registerPropertyInspector',info,ai);}}\
             else{{console.error('connectElgatoStreamDeckSocket missing');}}}}\
             if(document.readyState==='complete'){{setTimeout(go,0);}}else{{window.addEventListener('load',go);}}\
             }})();</script>",
            self.inner.port, Value::String(id), js(&self.info_for(info)), js(&action_info));
        let mut text = String::from_utf8_lossy(html).to_string();
        let lower = text.to_ascii_lowercase();
        let has_title = lower.contains("<title>");
        match lower.rfind("</body>") {
            Some(i) => text.insert_str(i, &boot),
            None => text.push_str(&boot),
        }
        if !has_title {
            let title = format!("<title>{} – FreeDeck</title>", info.name);
            match text.to_ascii_lowercase().find("<head>") {
                Some(h) => text.insert_str(h + 6, &title),
                None => text.insert_str(0, &title),
            }
        }
        text.into_bytes()
    }

    /// HTTP-Anfrage beantworten: (Status, Content-Type, Inhalt, weitere Kopfzeilen).
    fn on_http(&self, path: &str) -> (u16, String, Vec<u8>, Vec<(String, String)>) {
        let plain = "text/plain; charset=utf-8".to_string();
        let (p, q) = path.split_once('?').unwrap_or((path, ""));
        let parts: Vec<String> = p.split('/').filter(|x| !x.is_empty()).map(|x| String::from_utf8_lossy(&url_unquote(x, false)).to_string()).collect();
        let query: HashMap<String, String> = q.split('&').filter(|x| !x.is_empty()).map(|kv| {
            let (k, v) = kv.split_once('=').unwrap_or((kv, ""));
            (String::from_utf8_lossy(&url_unquote(k, true)).to_string(), String::from_utf8_lossy(&url_unquote(v, true)).to_string())
        }).collect();
        let qs = |k: &str| query.get(k).cloned().unwrap_or_default();
        if parts.len() < 2 || !const_eq(parts[1].as_bytes(), self.inner.token.as_bytes()) {
            return (403, plain, tr!("Access denied").into_bytes(), vec![]);
        }
        if parts[0] == "pi" {
            let ctx = PiSlot::parse(&qs("slot")).ok_or_else(|| tr!("No action in this slot"))
                .and_then(|slot| self.open_context(&qs("profile"), &qs("page"), slot));
            let ctx = match ctx {
                Ok(c) => c,
                Err(e) => return (404, plain, e.into_bytes(), vec![]),
            };
            let (action, id) = {
                let c = ctx.lock().unwrap();
                (c.action.clone(), c.id.clone())
            };
            let info = plugin_for_action(&action, Some(&self.inner.opts.plugins_dir));
            let rel = info.as_ref().and_then(|i| i.property_inspector_path(&action));
            let (Some(info), Some(rel)) = (info, rel) else {
                return (404, plain, tr!("This plugin has no settings page").into_bytes(), vec![]);
            };
            let loc = format!("/plugin/{}/{}/{}?ctx={}", self.inner.token, super::url_quote(&info.uuid, "/"), super::url_quote(&rel, "/"), id);
            return (302, "text/plain".into(), Vec::new(), vec![("Location".into(), loc)]);
        }
        if parts[0] == "plugin" && parts.len() >= 4 {
            let Some(info) = installed(Some(&self.inner.opts.plugins_dir)).get(&parts[2]).cloned() else {
                return (404, plain, tr!("Plugin not installed").into_bytes(), vec![]);
            };
            let base = info.dir.canonicalize().unwrap_or_else(|_| info.dir.clone());
            let f = base.join(parts[3..].join("/")).canonicalize().ok();
            let Some(f) = f.filter(|f| f.starts_with(&base) && *f != base && f.is_file()) else {
                return (404, plain, tr!("File not found").into_bytes(), vec![]);
            };
            let Ok(body) = std::fs::read(&f) else {
                return (404, plain, tr!("File not found").into_bytes(), vec![]);
            };
            let ext = f.extension().and_then(|e| e.to_str()).unwrap_or("").to_ascii_lowercase();
            let mut ctype = mime_type(&ext).to_string();
            let ctx = self.context(&qs("ctx"));
            if matches!(ext.as_str(), "html" | "htm") {
                if let Some(ctx) = ctx {
                    return (200, "text/html; charset=utf-8".into(), self.inject(&body, &info, &ctx), vec![]);
                }
            }
            if ctype.starts_with("text/") || ctype == "application/javascript" || ctype == "application/json" {
                ctype.push_str("; charset=utf-8");
            }
            return (200, ctype, body, vec![]);
        }
        (404, plain, tr!("Not found").into_bytes(), vec![])
    }

    fn pi_handle(&self, p: &Arc<Proc>, ctx: &Ctx, conn: &Conn, msg: &Value) {
        let ev = msg.get("event").and_then(Value::as_str).unwrap_or("");
        let payload = msg.get("payload").cloned().unwrap_or(Value::Null);
        match ev {
            "setSettings" => {
                self.store_settings(ctx, payload.as_object().cloned().unwrap_or_default());
                p.send(&self.settings_msg(ctx));
            }
            "getSettings" => {
                conn.send(&self.settings_msg(ctx));
            }
            "sendToPlugin" => {
                let (action, id) = {
                    let c = ctx.lock().unwrap();
                    (c.action.clone(), c.id.clone())
                };
                let pl = if payload.is_null() { json!({}) } else { payload };
                p.send(&json!({"event": "sendToPlugin", "action": action, "context": id, "payload": pl}));
            }
            "setGlobalSettings" => {
                let pl = if payload.is_null() { json!({}) } else { payload };
                self.set_global(&p.info.uuid, &pl);
                p.send(&json!({"event": "didReceiveGlobalSettings", "payload": {"settings": pl}}));
            }
            "getGlobalSettings" => {
                conn.send(&json!({"event": "didReceiveGlobalSettings", "payload": {"settings": self.get_global(&p.info.uuid)}}));
            }
            "openUrl" => open_url(payload.get("url").and_then(Value::as_str).unwrap_or("")),
            "logMessage" => log::info!("{}", tr!("[{name} settings] {message}", name = p.info.name,
                message = super::value_text(payload.get("message").unwrap_or(&Value::Null)))),
            _ => {}
        }
    }

    // ------------------------------------------------------------------------- Verbindungen
    /// Eine eingehende Verbindung: HTTP (Einstellungsseiten) oder WebSocket (Plugin, Einstellungsseite).
    fn serve(&self, stream: TcpStream) {
        let _ = stream.set_read_timeout(Some(Duration::from_secs(10)));
        let Some((method, path, headers, head_len)) = peek_request(&stream) else { return };
        let upgrade = headers.get("upgrade").map(|u| u.to_ascii_lowercase().contains("websocket")).unwrap_or(false);
        if !upgrade || !headers.contains_key("sec-websocket-key") {
            let mut stream = stream;
            let mut discard = vec![0u8; head_len];
            let _ = stream.read_exact(&mut discard);
            let (status, ctype, body, extra) = if method == "GET" {
                self.on_http(&path)
            } else {
                (404, "text/plain; charset=utf-8".into(), tr!("Not found").into_bytes(), vec![])
            };
            write_http(&mut stream, status, &ctype, &body, &extra);
            return;
        }
        // Nur Plugins (ohne Origin) und die eigenen Einstellungsseiten dürfen sich verbinden
        if let Some(origin) = headers.get("origin") {
            let port = self.inner.port;
            if *origin != format!("http://127.0.0.1:{port}") && *origin != format!("http://localhost:{port}") {
                log::warn!("{}", tr!("WebSocket connection from foreign origin rejected: {origin}", origin = origin));
                let mut stream = stream;
                write_http(&mut stream, 403, "text/plain; charset=utf-8", tr!("Access denied").as_bytes(), &[]);
                return;
            }
        }
        let Ok(mut ws) = tungstenite::accept(stream) else { return };
        let _ = ws.get_mut().set_read_timeout(Some(POLL));
        let (tx, rx) = mpsc::channel::<Out>();
        let conn = Conn { id: next_conn_id(), tx };
        let mut role: Option<Role> = None;
        'outer: loop {
            if self.inner.stopped.load(Ordering::Relaxed) {
                let _ = ws.close(None);
                let _ = ws.flush();
                break;
            }
            // eingereihte Nachrichten senden
            loop {
                match rx.try_recv() {
                    Ok(Out::Text(t)) => {
                        if ws.send(tungstenite::Message::text(t)).is_err() {
                            break 'outer;
                        }
                    }
                    Ok(Out::Close) => {
                        let _ = ws.close(None);
                        let _ = ws.flush();
                        break 'outer;
                    }
                    Err(_) => break,
                }
            }
            let text = match ws.read() {
                Ok(tungstenite::Message::Text(t)) => t.to_string(),
                Ok(tungstenite::Message::Binary(b)) => String::from_utf8_lossy(&b).to_string(),
                Ok(tungstenite::Message::Close(_)) => break,
                Ok(_) => continue,
                Err(tungstenite::Error::Io(e)) if matches!(e.kind(), std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut) => continue,
                Err(_) => break,
            };
            let Ok(msg) = serde_json::from_str::<Value>(&text) else { continue };
            match &role {
                None => match self.register_conn(&msg, &conn) {
                    Some(r) => role = Some(r),
                    None => {
                        let _ = ws.close(None);
                        let _ = ws.flush();
                        break;
                    }
                },
                Some(Role::Plugin(p)) => self.handle(p, &msg),
                Some(Role::Pi(p, ctx)) => self.pi_handle(p, ctx, &conn, &msg),
            }
        }
        // Verbindung zu Ende
        match role {
            Some(Role::Plugin(p)) => {
                let mut g = p.conn.lock().unwrap();
                if g.as_ref().map(|c| c.id) == Some(conn.id) {
                    *g = None;
                }
            }
            Some(Role::Pi(p, ctx)) => self.pi_closed(&p, &ctx, &conn),
            None => {}
        }
    }

    /// Erste Nachricht einer Verbindung: registerPlugin oder registerPropertyInspector.
    fn register_conn(&self, first: &Value, conn: &Conn) -> Option<Role> {
        let ev = first.get("event").and_then(Value::as_str).unwrap_or("");
        let uuid = first.get("uuid").and_then(Value::as_str).unwrap_or("");
        if ev == "registerPropertyInspector" {
            return self.pi_opened(uuid, conn);
        }
        if ev != "registerPlugin" {
            return None;
        }
        let p = self.inner.procs.lock().unwrap().get(uuid).cloned().flatten();
        let Some(p) = p else {
            log::warn!("{}", tr!("Unknown plugin tried to register: {uuid}", uuid = uuid));
            return None;
        };
        log::info!("{}", tr!("Plugin connected: {name}", name = p.info.name));
        *p.conn.lock().unwrap() = Some(conn.clone());
        for d in self.devices() {
            p.send(&json!({"event": "deviceDidConnect", "device": d.serial, "deviceInfo": d.json()}));
        }
        let visible: Vec<Ctx> = {
            let s = self.inner.shared.lock().unwrap();
            s.visible.iter().filter_map(|id| s.contexts.get(id).cloned()).collect()
        };
        for c in visible {
            let action = c.lock().unwrap().action.clone();
            if self.proc_for(&action).map(|q| Arc::ptr_eq(&q, &p)).unwrap_or(false) {
                self.event("willAppear", &c, Value::Null);
            }
        }
        Some(Role::Plugin(p))
    }

    fn pi_opened(&self, id: &str, conn: &Conn) -> Option<Role> {
        let ctx = self.context(id)?;
        let (action, device, detached) = {
            let c = ctx.lock().unwrap();
            (c.action.clone(), c.device.clone(), c.detached)
        };
        let p = self.proc_for(&action)?;
        let old = self.inner.shared.lock().unwrap().pis.insert(id.to_string(), conn.clone());
        if let Some(old) = old {
            old.close();
        }
        let visible = self.inner.shared.lock().unwrap().visible.contains(id);
        if detached && !visible {
            self.event("willAppear", &ctx, Value::Null);
        }
        p.send(&json!({"event": "propertyInspectorDidAppear", "action": action, "context": id, "device": device}));
        Some(Role::Pi(p, ctx))
    }

    fn pi_closed(&self, p: &Arc<Proc>, ctx: &Ctx, conn: &Conn) {
        let (id, action, device, detached) = {
            let c = ctx.lock().unwrap();
            (c.id.clone(), c.action.clone(), c.device.clone(), c.detached)
        };
        let visible = {
            let mut s = self.inner.shared.lock().unwrap();
            if s.pis.get(&id).map(|c| c.id) == Some(conn.id) {
                s.pis.remove(&id);
            }
            s.visible.contains(&id)
        };
        p.send(&json!({"event": "propertyInspectorDidDisappear", "action": action, "context": id, "device": device}));
        if !visible {
            if detached {
                self.event("willDisappear", ctx, Value::Null);
            }
            self.forget_later(&id);
        }
    }
}

enum Role {
    Plugin(Arc<Proc>),
    Pi(Arc<Proc>, Ctx),
}

/// Neuer Kontext für eine Aktion.
pub(crate) fn new_ctx(uuid: &str, data: Map<String, Value>, page_dir: PathBuf, dial: bool, coords: Option<(u8, u8)>) -> Ctx {
    let n = data.get("States").and_then(Value::as_array).map(|a| a.len()).unwrap_or(1).max(1);
    let state = (data.get("State").and_then(Value::as_u64).unwrap_or(0) as usize).min(n - 1);
    Arc::new(Mutex::new(CtxState {
        id: uuid::Uuid::new_v4().simple().to_string().to_uppercase(),
        action: uuid.to_string(),
        data,
        page_dir,
        state,
        dial,
        coords,
        ..Default::default()
    }))
}

fn open_url(url: &str) {
    if url.starts_with("http://") || url.starts_with("https://") || url.starts_with("mailto:") {
        system::run_bg(&["xdg-open", url]);
    }
}

fn const_eq(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// %XX (und bei Formularwerten „+“) auflösen.
fn url_unquote(s: &str, plus: bool) -> Vec<u8> {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        match b[i] {
            b'%' if i + 2 < b.len() => {
                if let Some(v) = std::str::from_utf8(&b[i + 1..i + 3]).ok().and_then(|h| u8::from_str_radix(h, 16).ok()) {
                    out.push(v);
                    i += 3;
                    continue;
                }
                out.push(b'%');
            }
            b'+' if plus => out.push(b' '),
            c => out.push(c),
        }
        i += 1;
    }
    out
}

fn mime_type(ext: &str) -> &'static str {
    match ext {
        "html" | "htm" => "text/html",
        "js" | "mjs" | "cjs" => "application/javascript",
        "css" => "text/css",
        "json" => "application/json",
        "map" => "application/json",
        "txt" => "text/plain",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "svg" => "image/svg+xml",
        "webp" => "image/webp",
        "ico" => "image/vnd.microsoft.icon",
        "woff" => "font/woff",
        "woff2" => "font/woff2",
        "ttf" => "font/ttf",
        "otf" => "font/otf",
        "wasm" => "application/wasm",
        "mp3" => "audio/mpeg",
        "wav" => "audio/x-wav",
        _ => "application/octet-stream",
    }
}

/// Anfragekopf lesen, ohne ihn zu verbrauchen (für die WebSocket-Übergabe):
/// (Methode, Pfad, Kopfzeilen klein geschrieben, Länge des Kopfs).
fn peek_request(stream: &TcpStream) -> Option<(String, String, HashMap<String, String>, usize)> {
    let mut buf = vec![0u8; 8192];
    let start = Instant::now();
    loop {
        let n = stream.peek(&mut buf).ok()?;
        if n == 0 {
            return None;
        }
        if let Some(end) = buf[..n].windows(4).position(|w| w == b"\r\n\r\n") {
            let head = String::from_utf8_lossy(&buf[..end]).to_string();
            let mut lines = head.split("\r\n");
            let first: Vec<&str> = lines.next().unwrap_or("").split(' ').collect();
            let (method, path) = if first.len() >= 2 { (first[0].to_string(), first[1].to_string()) } else { (String::new(), "/".into()) };
            let headers = lines.filter_map(|l| l.split_once(':')).map(|(k, v)| (k.trim().to_ascii_lowercase(), v.trim().to_string())).collect();
            return Some((method, path, headers, end + 4));
        }
        if n == buf.len() {
            if buf.len() >= 65536 {
                return None; // Kopf zu groß
            }
            buf.resize(buf.len() * 2, 0);
        } else if start.elapsed() > Duration::from_secs(10) {
            return None;
        } else {
            std::thread::sleep(Duration::from_millis(5));
        }
    }
}

fn write_http(stream: &mut TcpStream, status: u16, ctype: &str, body: &[u8], extra: &[(String, String)]) {
    let reason = match status {
        200 => "OK",
        302 => "Found",
        403 => "Forbidden",
        404 => "Not Found",
        _ => "Error",
    };
    let mut head = format!("HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {}\r\nCache-Control: no-store\r\nConnection: close\r\n", body.len());
    for (k, v) in extra {
        head.push_str(&format!("{k}: {v}\r\n"));
    }
    head.push_str("\r\n");
    let _ = stream.write_all(head.as_bytes());
    let _ = stream.write_all(body);
    let _ = stream.flush();
}

/// Neue Settings einer Aktion im Seiten-Manifest speichern (wie `profiles.save_action_settings`).
/// Die Aktion wird über ihren bisherigen Inhalt gefunden – auch in Multi-Aktionen. false = nicht gefunden.
pub fn save_action_settings(page_dir: &Path, old: &Map<String, Value>, settings: &Map<String, Value>) -> anyhow::Result<bool> {
    let mf = page_dir.join("manifest.json");
    let mut m = freestream_core::profiles::read_json(&mf)?;
    fn walk<'a>(v: &'a mut Value, old: &Map<String, Value>) -> Option<&'a mut Map<String, Value>> {
        match v {
            Value::Object(o) => {
                if o.get("UUID").map(|u| super::truthy(Some(u))).unwrap_or(false) && o == old {
                    return Some(o);
                }
                o.values_mut().find_map(|x| walk(x, old))
            }
            Value::Array(a) => a.iter_mut().find_map(|x| walk(x, old)),
            _ => None,
        }
    }
    let Some(found) = walk(&mut m, old) else { return Ok(false) };
    found.insert("Settings".into(), Value::Object(settings.clone()));
    freestream_core::config::write_atomic(&mf, &serde_json::to_string(&m)?, 0o644)?;
    Ok(true)
}
