//! Aktionen der Tasten und Drehregler. Jede Aktion kennt ihre Daten aus dem Profil (UUID, Settings,
//! States) und ein Verhalten ([`Behavior`]). Was die Steuerung tun soll (Seite wechseln, Ordner öffnen …),
//! gibt sie als [`Command`] zurück – so braucht sie keinen Zugriff auf die Steuerung selbst.

use image::RgbaImage;
use serde_json::{Map, Value};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use crate::render;
use crate::system;

/// Was die Steuerung nach einem Ereignis tun soll.
#[derive(Debug, Clone, PartialEq)]
pub enum Command {
    None,
    OpenFolder(String),
    Back,
    NextPage,
    PreviousPage,
    GotoPage(usize),
    SwitchProfile(String),
    RotateProfile(i32),
    /// Helligkeit setzen (Prozent) oder ändern (+/-)
    SetBrightness(u8),
    ChangeBrightness(i32),
    Sleep,
    /// Kurz „Fertig“ bzw. „Fehler“ auf der Taste zeigen
    Ok,
    Alert,
    Redraw,
    /// Zustand der Aktion setzen (Hotkey-Umschalter, Multi Action Switch) und neu zeichnen
    SetState(usize),
    /// Mehrere Befehle nacheinander (z. B. aus [`Behavior::execute`])
    Run(Vec<Command>),
}

impl Command {
    /// Zwei Befehle hintereinander (None fällt weg).
    pub fn then(self, other: Command) -> Command {
        match (self, other) {
            (Command::None, b) => b,
            (a, Command::None) => a,
            (Command::Run(mut v), b) => {
                v.push(b);
                Command::Run(v)
            }
            (a, b) => Command::Run(vec![a, b]),
        }
    }
}

/// Befehle, die eine Aktion später aus einem eigenen Thread schickt (Multi-Aktionen, Key Logic, Text …).
/// Die Steuerung holt sie in [`crate::engine::Controller::tick`] ab; `key` ist die Taste (None = Drehregler).
/// Befehl mit Taste (None = Drehregler).
pub type Posted = (Option<u8>, Command);

#[derive(Clone, Default)]
pub struct Outbox {
    key: Option<u8>,
    queue: Arc<Mutex<Vec<Posted>>>,
}

impl Outbox {
    /// Dieselbe Warteschlange für eine bestimmte Taste (None = Drehregler).
    pub fn for_key(&self, key: Option<u8>) -> Outbox {
        Outbox { key, queue: self.queue.clone() }
    }

    pub fn send(&self, cmd: Command) {
        if cmd != Command::None {
            self.queue.lock().unwrap().push((self.key, cmd));
        }
    }

    pub fn drain(&self) -> Vec<Posted> {
        std::mem::take(&mut *self.queue.lock().unwrap())
    }
}

/// Was eine Aktion über ihre Umgebung wissen darf.
pub struct Env<'a> {
    pub cfg: &'a Map<String, Value>,
    pub page_index: usize,
    pub page_count: usize,
    /// Profil vom Mac: Cmd und Strg in Tastenkürzeln tauschen
    pub swap_cmd_ctrl: bool,
    /// für Befehle aus eigenen Threads
    pub outbox: Outbox,
}

impl<'a> Env<'a> {
    pub fn new(cfg: &'a Map<String, Value>, page_index: usize, page_count: usize) -> Self {
        Self { cfg, page_index, page_count, swap_cmd_ctrl: false, outbox: Outbox::default() }
    }

    /// Kopie zum Mitnehmen in einen Thread.
    pub fn snapshot(&self) -> OwnedEnv {
        OwnedEnv { cfg: self.cfg.clone(), page_index: self.page_index, page_count: self.page_count,
                   swap_cmd_ctrl: self.swap_cmd_ctrl, outbox: self.outbox.clone() }
    }

    /// Zahl aus der Konfiguration (z. B. `long_press_ms`).
    pub fn cfg_i64(&self, key: &str, default: i64) -> i64 {
        self.cfg.get(key).and_then(Value::as_i64).unwrap_or(default)
    }

    /// Langer Druck nach `long_press_ms`?
    pub fn is_long(&self, held_ms: u64) -> bool {
        held_ms as i64 >= self.cfg_i64("long_press_ms", 500)
    }
}

/// [`Env`] mit eigenen Daten (für Threads).
#[derive(Clone)]
pub struct OwnedEnv {
    pub cfg: Map<String, Value>,
    pub page_index: usize,
    pub page_count: usize,
    pub swap_cmd_ctrl: bool,
    pub outbox: Outbox,
}

impl OwnedEnv {
    pub fn env(&self) -> Env<'_> {
        Env { cfg: &self.cfg, page_index: self.page_index, page_count: self.page_count,
              swap_cmd_ctrl: self.swap_cmd_ctrl, outbox: self.outbox.clone() }
    }
}

/// Verhalten einer Aktion. Alle Methoden haben Vorgaben: eine Aktion überschreibt nur, was sie braucht.
pub trait Behavior: Send {
    fn key_down(&mut self, _a: &ActionData, _env: &Env) -> Command {
        Command::None
    }
    fn key_up(&mut self, _a: &ActionData, _env: &Env, _held_ms: u64) -> Command {
        Command::None
    }
    fn dial_turn(&mut self, _a: &ActionData, _env: &Env, _ticks: i8) -> Command {
        Command::None
    }
    fn dial_down(&mut self, a: &ActionData, env: &Env) -> Command {
        self.key_down(a, env)
    }
    fn dial_up(&mut self, a: &ActionData, env: &Env, held_ms: u64) -> Command {
        self.key_up(a, env, held_ms)
    }
    /// Tippen auf den Touchstreifen über dem Drehregler (lang = langes Tippen).
    fn touch(&mut self, a: &ActionData, env: &Env, long: bool) -> Command {
        let c = self.dial_down(a, env);
        c.then(self.dial_up(a, env, if long { 600 } else { 0 }))
    }
    /// Einmal auslösen, z. B. als Schritt einer Multi-Aktion (Drücken und sofort Loslassen).
    fn execute(&mut self, a: &ActionData, env: &Env) -> Command {
        let c = self.key_down(a, env);
        c.then(self.key_up(a, env, 0))
    }
    /// Eigenes Bild statt des Symbols aus dem Profil (Seitenanzeige, Uhr …); None = Profil-Symbol.
    fn image(&self, _a: &ActionData, _env: &Env, _size: (u32, u32)) -> Option<RgbaImage> {
        None
    }
    /// Eigener Titel; None = Titel aus dem Profil.
    fn title(&self, _a: &ActionData, _env: &Env) -> Option<String> {
        None
    }
    /// Jede Sekunde aufrufen? (Uhr, Timer …) – true aus [`Behavior::tick`] = neu zeichnen
    fn needs_tick(&self) -> bool {
        false
    }
    fn tick(&mut self, _a: &ActionData) -> bool {
        false
    }
    /// Wird die Aktion auf dem Linux-Rechner ausgeführt? (sonst ein Punkt auf der Taste im Editor)
    fn supported(&self) -> bool {
        true
    }
}

/// Daten einer Aktion aus dem Seiten-Manifest.
#[derive(Debug, Clone)]
pub struct ActionData {
    pub uuid: String,
    pub data: Map<String, Value>,
    pub page_dir: PathBuf,
    pub state: usize,
    /// liegt auf einem Drehregler (Stream Deck +)
    pub dial: bool,
}

impl ActionData {
    pub fn new(data: Map<String, Value>, page_dir: PathBuf) -> Self {
        let uuid = data.get("UUID").and_then(Value::as_str).unwrap_or("").to_string();
        let n = data.get("States").and_then(Value::as_array).map(|a| a.len()).unwrap_or(1).max(1);
        let state = (data.get("State").and_then(Value::as_u64).unwrap_or(0) as usize).min(n - 1);
        Self { uuid, data, page_dir, state, dial: false }
    }

    pub fn settings(&self) -> Map<String, Value> {
        self.data.get("Settings").and_then(Value::as_object).cloned().unwrap_or_default()
    }

    pub fn setting_str(&self, key: &str) -> String {
        self.data.get("Settings").and_then(|s| s.get(key)).and_then(|v| match v {
            Value::String(s) => Some(s.clone()),
            Value::Number(n) => Some(n.to_string()),
            _ => None,
        }).unwrap_or_default()
    }

    pub fn name(&self) -> String {
        self.data.get("Name").and_then(Value::as_str).map(str::to_string)
            .unwrap_or_else(|| self.uuid.rsplit('.').next().unwrap_or("").to_string())
    }

    /// Einstellungen des aktuellen Zustands (Titel, Schrift, Bild …).
    pub fn state_dict(&self) -> Map<String, Value> {
        self.data
            .get("States")
            .and_then(Value::as_array)
            .and_then(|a| a.get(self.state))
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default()
    }

    pub fn title(&self) -> String {
        self.state_dict().get("Title").and_then(Value::as_str).unwrap_or("").to_string()
    }

    /// Bild des aktuellen Zustands (Pfad im Seitenordner).
    pub fn image_path(&self) -> Option<PathBuf> {
        let img = self.state_dict().get("Image").and_then(Value::as_str).unwrap_or("").to_string();
        if img.is_empty() {
            return None;
        }
        let p = self.page_dir.join(&img);
        if p.exists() {
            return Some(p);
        }
        // ohne Endung gespeichert: render::load_image sucht die Endung selbst
        Some(p)
    }

    pub fn state_count(&self) -> usize {
        self.data.get("States").and_then(Value::as_array).map(|a| a.len()).unwrap_or(1).max(1)
    }
}

/// Aktion mit Daten und Verhalten.
pub struct Action {
    pub data: ActionData,
    pub behavior: Box<dyn Behavior>,
}

impl Action {
    pub fn new(data: Map<String, Value>, page_dir: PathBuf) -> Self {
        let data = ActionData::new(data, page_dir);
        let behavior = behavior_for(&data.uuid);
        Self { data, behavior }
    }

    /// Tastenbild: eigenes Bild der Aktion, sonst Symbol aus dem Profil, sonst Ersatzbild mit Namen.
    pub fn image(&self, env: &Env, size: (u32, u32), base: Option<&RgbaImage>, overlay: Option<render::Overlay>) -> RgbaImage {
        let own = self.behavior.image(&self.data, env, size);
        let icon = own.clone().or_else(|| self.data.image_path().and_then(|p| render::load_image(&p, size.0, size.1)));
        let fallback;
        let icon = match icon {
            Some(i) => Some(i),
            None => {
                fallback = render::fallback_icon(size.0, size.1, &self.fallback_label());
                Some(fallback)
            }
        };
        let title = if own.is_some() && self.data.title().is_empty() {
            String::new()
        } else {
            self.behavior.title(&self.data, env).unwrap_or_else(|| self.data.title())
        };
        render::compose_key(size, icon.as_ref(), &title, &self.data.state_dict(), overlay, base)
    }

    /// Als Schritt einer anderen Aktion auslösen: Zustandswechsel gelten für diese Aktion selbst,
    /// alle anderen Befehle gehen an die Steuerung zurück.
    pub fn execute_nested(&mut self, env: &Env) -> Command {
        let cmd = self.behavior.execute(&self.data, env);
        self.absorb(cmd)
    }

    /// [`Command::SetState`] auf die eigenen Daten anwenden, den Rest zurückgeben.
    pub fn absorb(&mut self, cmd: Command) -> Command {
        match cmd {
            Command::SetState(s) => {
                self.data.state = s % self.data.state_count();
                Command::None
            }
            Command::Run(v) => v.into_iter().fold(Command::None, |acc, c| {
                let c = self.absorb(c);
                acc.then(c)
            }),
            c => c,
        }
    }

    fn fallback_label(&self) -> String {
        match self.data.uuid.as_str() {
            "com.elgato.streamdeck.profile.openchild" => freestream_core::tr!("Folder"),
            "com.elgato.streamdeck.profile.backtoparent" => freestream_core::tr!("Back"),
            _ => self.data.name(),
        }
    }
}

// --------------------------------------------------------------------------------------- Verhalten
/// Aktion, die es (noch) nicht gibt: tut nichts und meldet es im Protokoll.
struct Unsupported;
impl Behavior for Unsupported {
    fn key_down(&mut self, a: &ActionData, _env: &Env) -> Command {
        log::info!("Aktion {} wird noch nicht unterstützt", a.uuid);
        Command::Alert
    }
    fn supported(&self) -> bool {
        false
    }
}

struct Simple(fn(&ActionData, &Env) -> Command);
impl Behavior for Simple {
    fn key_down(&mut self, a: &ActionData, env: &Env) -> Command {
        (self.0)(a, env)
    }
}

/// Seitenanzeige: „2 von 5“.
struct PageIndicator;
impl Behavior for PageIndicator {
    fn image(&self, a: &ActionData, env: &Env, size: (u32, u32)) -> Option<RgbaImage> {
        if a.state_dict().get("Image").and_then(Value::as_str).map(|s| !s.is_empty()).unwrap_or(false) {
            return None;
        }
        let sub = freestream_core::tr!("of {n}", n = env.page_count.max(1));
        Some(render::text_key(size, &(env.page_index + 1).to_string(), &sub, [0, 0, 0], [255, 255, 255]))
    }
}

/// Programm öffnen (beim Loslassen, wie in der Elgato-App).
struct OpenApp;
impl Behavior for OpenApp {
    fn key_up(&mut self, a: &ActionData, env: &Env, held_ms: u64) -> Command {
        let s = a.settings();
        let get = |k: &str| s.get(k).and_then(Value::as_str).unwrap_or("").to_string();
        let target = [get("exec"), get("bundle_path"), get("bundle_id"), get("path")].into_iter().find(|t| !t.is_empty()).unwrap_or_default();
        let cmd = system::resolve_program(&target, env.cfg.get("app_map").and_then(Value::as_object), &get("app_name"));
        let hint = if get("exec").is_empty() { get("bundle_path") } else { get("exec") };
        let names = system::process_names_for(cmd.as_deref(), &hint);
        // langer Druck mit „Programm beenden“ (Einstellung long_press)
        if env.is_long(held_ms) && matches!(get("long_press").as_str(), "quit" | "close" | "kill" | "force_quit") {
            return if system::kill_processes(&names) { Command::None } else { Command::Alert };
        }
        let front = s.get("bring_to_front").and_then(Value::as_bool).unwrap_or(true);
        if front && cmd.is_some() && system::is_running(&names) && system::focus_window(&names) {
            return Command::None;
        }
        match cmd {
            Some(c) => {
                let args = get("args");
                let full = if args.is_empty() { c } else { format!("{c} {args}") };
                if system::run_shell(&full) { Command::None } else { Command::Alert }
            }
            None => {
                log::warn!("{}", freestream_core::tr!("Program '{name}' not found – add it under Settings → Connections → Windows profiles",
                    name = if get("app_name").is_empty() { target } else { get("app_name") }));
                Command::Alert
            }
        }
    }
}

fn open(a: &ActionData, env: &Env) -> Command {
    let p = a.setting_str("path");
    let p = if p.is_empty() { a.setting_str("openInBrowser") } else { p };
    if system::open_target(&p, env.cfg) { Command::None } else { Command::Alert }
}

fn website(a: &ActionData, _env: &Env) -> Command {
    let url = a.setting_str("path").trim().to_string();
    if url.is_empty() {
        return Command::Alert;
    }
    let url = if url.contains("://") { url } else { format!("https://{url}") };
    if system::run_bg(&["xdg-open", &url]) { Command::None } else { Command::Alert }
}

fn brightness(a: &ActionData, _env: &Env) -> Command {
    // Elgato: Settings.brightness (Wert) oder .keybrightness mit „Increase/Decrease/Set“
    let s = a.settings();
    let val = s.get("value").or_else(|| s.get("brightness")).and_then(|v| v.as_i64().or_else(|| v.as_str()?.parse().ok())).unwrap_or(50);
    match s.get("action").or_else(|| s.get("mode")).and_then(Value::as_str).unwrap_or("set").to_lowercase().as_str() {
        "increase" | "up" | "plus" => Command::ChangeBrightness(val.clamp(1, 100) as i32),
        "decrease" | "down" | "minus" => Command::ChangeBrightness(-(val.clamp(1, 100) as i32)),
        _ => Command::SetBrightness(val.clamp(0, 100) as u8),
    }
}

/// Verhalten nach UUID der Aktion.
pub fn behavior_for(uuid: &str) -> Box<dyn Behavior> {
    match uuid {
        "com.elgato.streamdeck.profile.openchild" => Box::new(Simple(|a, _| Command::OpenFolder(a.setting_str("ProfileUUID")))),
        "com.elgato.streamdeck.profile.backtoparent" => Box::new(Simple(|_, _| Command::Back)),
        "com.elgato.streamdeck.page.next" => Box::new(Simple(|_, _| Command::NextPage)),
        "com.elgato.streamdeck.page.previous" => Box::new(Simple(|_, _| Command::PreviousPage)),
        "com.elgato.streamdeck.page.goto" => Box::new(Simple(|a, _| {
            Command::GotoPage(a.setting_str("PageIndex").parse::<usize>().unwrap_or(1).saturating_sub(1))
        })),
        "com.elgato.streamdeck.page.indicator" => Box::new(PageIndicator),
        "com.elgato.streamdeck.profile.rotate" => Box::new(Simple(|a, _| {
            let back = a.setting_str("direction").to_lowercase().contains("prev") || a.setting_str("direction") == "-1";
            Command::RotateProfile(if back { -1 } else { 1 })
        })),
        "com.elgato.streamdeck.system.open" => Box::new(Simple(open)),
        "com.elgato.streamdeck.system.website" => Box::new(Simple(website)),
        "com.elgato.streamdeck.system.openapp" => Box::new(OpenApp),
        "com.elgato.streamdeck.system.sleep" => Box::new(Simple(|_, _| Command::Sleep)),
        "com.elgato.streamdeck.system.keybrightness" => Box::new(Simple(brightness)),
        _ => crate::extra::behavior_for(uuid).unwrap_or_else(|| Box::new(Unsupported)),
    }
}
