//! Steuerung eines Decks (wie `freestream/engine.py`): Profil und Seite wählen, Tastenbilder zeichnen,
//! Eingaben an die Aktionen geben und ihre [`Command`]s ausführen (Seiten, Ordner, Profile, Helligkeit).

use freestream_core::config::{Config, State};
use freestream_core::models::Geometry;
use freestream_core::profiles::{Page, Profile, ProfileStore};
use image::RgbaImage;
use serde_json::{Map, Value};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crate::actions::{Action, Command, Env, Outbox};
use crate::device::{Device, Event};
use crate::render::{self, Overlay};

/// Bild, wenn kein Profil zum Gerät passt.
pub fn no_profile_key(size: (u32, u32), first: bool) -> RgbaImage {
    if first {
        render::text_key(size, "FreeDeck", &freestream_core::tr!("No profile"), [20, 20, 24], [120, 160, 255])
    } else {
        render::text_key(size, "", "", [0, 0, 0], [0, 0, 0])
    }
}

/// Uhr der Elgato-App (auch in der Infoleiste des Neo).
const DIGITAL_TIME: &str = "com.elgato.streamdeck.system.digitaltime";
/// So lange stehen „Fertig“/„Fehler“ auf der Taste bzw. dem Feld des Touchstreifens.
const OVERLAY_TIME: Duration = Duration::from_millis(1200);
/// Fortschritt einer Multi-Aktion: bis sie fertig meldet, höchstens so lange.
const PROGRESS_TIME: Duration = Duration::from_secs(600);

/// Wohin ein Befehl gehört.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Slot {
    Key(u8),
    Dial(u8),
    /// Drehregler, der Befehl kam aber aus einem Thread (ohne Nummer): nur der Touchstreifen
    Strip,
}

impl Slot {
    fn from_posted(key: Option<u8>) -> Slot {
        key.map(Slot::Key).unwrap_or(Slot::Strip)
    }
}

/// Steuerung eines Geräts.
pub struct Controller {
    pub dev: Arc<dyn Device>,
    pub store: Arc<Mutex<ProfileStore>>,
    pub cfg: Config,
    pub state: State,
    pub profile: Option<Profile>,
    pub page_index: usize,
    /// geöffnete Ordner (oberster zuletzt)
    pub folders: Vec<Page>,
    pub geometry: Geometry,
    pub brightness: u8,
    keys: HashMap<u8, Action>,
    pressed: HashMap<u8, Instant>,
    /// Aktionen der Drehregler (Stream Deck +)
    dials: HashMap<u8, Action>,
    dial_pressed: HashMap<u8, Instant>,
    /// Aktionen der Infoleiste (Neo)
    infobar: HashMap<u8, Action>,
    /// Befehle aus Threads der Aktionen (Multi-Aktionen, Key Logic …)
    outbox: Outbox,
    /// Hinweise auf Tasten: (Art, bis wann)
    overlays: HashMap<u8, (Overlay, Instant)>,
    /// „Fehler“ auf Feldern des Touchstreifens: bis wann
    dial_alerts: HashMap<u8, Instant>,
    tiles: HashMap<(u8, u8), RgbaImage>,
    /// Bilder, die gerade auf dem Gerät stehen (für das Fenster und um Doppeltes zu sparen)
    pub shown: HashMap<u8, RgbaImage>,
    /// Bild auf Touchstreifen bzw. Infoleiste
    lcd_shown: Option<RgbaImage>,
    /// Touch-Tasten des Neo an?
    touch_keys_on: Option<bool>,
    last_input: Instant,
    asleep: bool,
    /// Gerät steht auf dem Kopf (Einstellungen › Geräte › Drehung 180°)
    pub rotated: bool,
    /// Gerät aktiv (aus: dunkel, reagiert nicht)
    pub enabled: bool,
}

impl Controller {
    pub fn new(dev: Arc<dyn Device>, store: Arc<Mutex<ProfileStore>>) -> Self {
        let cfg = Config::load();
        let geometry = dev.geometry();
        let serial = dev.serial();
        let mut state = State::load();
        let brightness = cfg.device(&serial).get("brightness").and_then(Value::as_u64)
            .or_else(|| state.device(&serial).get("brightness").and_then(Value::as_u64))
            .unwrap_or_else(|| cfg.get_i64("brightness", 70) as u64)
            .min(100) as u8;
        Self {
            dev,
            store,
            cfg,
            state,
            profile: None,
            page_index: 0,
            folders: Vec::new(),
            geometry,
            brightness,
            keys: HashMap::new(),
            pressed: HashMap::new(),
            dials: HashMap::new(),
            dial_pressed: HashMap::new(),
            infobar: HashMap::new(),
            outbox: Outbox::default(),
            overlays: HashMap::new(),
            dial_alerts: HashMap::new(),
            tiles: HashMap::new(),
            shown: HashMap::new(),
            lcd_shown: None,
            touch_keys_on: None,
            last_input: Instant::now(),
            asleep: false,
            rotated: false,
            enabled: true,
        }
    }

    pub fn serial(&self) -> String {
        self.dev.serial()
    }

    /// Profile, die zum Gerät passen (sonst alle).
    pub fn candidates(&self) -> Vec<String> {
        let store = self.store.lock().unwrap();
        let c: Vec<String> = store.for_model(&self.dev.model(), self.geometry).iter().map(|p| p.id.clone()).collect();
        if c.is_empty() { store.profiles.keys().cloned().collect() } else { c }
    }

    fn initial_profile(&mut self) -> Option<String> {
        let serial = self.serial();
        let store = self.store.lock().unwrap();
        let cands: Vec<String> = store.for_model(&self.dev.model(), self.geometry).iter().map(|p| p.id.clone()).collect();
        let from_cfg = self.cfg.device(&serial).get("profile").and_then(Value::as_str).map(str::to_string);
        let from_state = self.state.device(&serial).get("profile").and_then(Value::as_str).map(str::to_string);
        for pid in [from_cfg, from_state].into_iter().flatten() {
            if let Some(p) = store.get(&pid) {
                return Some(p.id.clone());
            }
        }
        let default = self.cfg.get_str("default_profile");
        if !default.is_empty() && cands.contains(&default) {
            return Some(default);
        }
        cands.first().cloned()
    }

    /// Starten: Helligkeit setzen, Profil laden, Tasten zeichnen.
    pub fn start(&mut self) {
        self.read_device_settings();
        let _ = self.dev.set_brightness(if self.enabled { self.brightness } else { 0 });
        let pid = self.initial_profile();
        self.load_profile(pid.as_deref(), None);
    }

    pub fn load_profile(&mut self, pid: Option<&str>, page_index: Option<usize>) {
        let serial = self.serial();
        self.profile = pid.and_then(|p| self.store.lock().unwrap().get(p).cloned());
        self.folders.clear();
        self.page_index = 0;
        if let Some(p) = &self.profile {
            let remembered = self.state.device(&serial).clone();
            let idx = page_index.unwrap_or_else(|| {
                if remembered.get("profile").and_then(Value::as_str) == Some(p.id.as_str()) {
                    remembered.get("page").and_then(Value::as_u64).unwrap_or(0) as usize
                } else {
                    p.page_ids.iter().position(|x| *x == p.current_page).unwrap_or(0)
                }
            });
            self.page_index = idx.min(p.page_ids.len().saturating_sub(1));
        }
        self.remember();
        self.show_current();
    }

    fn remember(&mut self) {
        let serial = self.serial();
        let (pid, page, b) = (self.profile.as_ref().map(|p| p.id.clone()), self.page_index, self.brightness);
        let d = self.state.device(&serial);
        d.insert("profile".into(), pid.map(Value::from).unwrap_or(Value::Null));
        d.insert("page".into(), Value::from(page));
        d.insert("brightness".into(), Value::from(b));
        let _ = self.state.save();
    }

    pub fn page_count(&self) -> usize {
        self.profile.as_ref().map(|p| p.page_ids.len()).unwrap_or(0)
    }

    /// Angezeigte Seite (oberster Ordner oder Seite des Profils).
    pub fn current_page(&self) -> Option<Page> {
        if let Some(f) = self.folders.last() {
            return Some(f.clone());
        }
        let p = self.profile.as_ref()?;
        p.page(p.page_ids.get(self.page_index)?)
    }

    pub fn goto_page(&mut self, idx: usize) {
        let n = self.page_count();
        if n == 0 {
            return;
        }
        self.folders.clear();
        self.page_index = idx.min(n - 1);
        self.remember();
        self.show_current();
    }

    pub fn open_folder(&mut self, page_uuid: &str) {
        let Some(pg) = self.profile.as_ref().and_then(|p| p.page(page_uuid)) else {
            log::warn!("{}", freestream_core::tr!("Folder {page} not found", page = page_uuid));
            return;
        };
        self.folders.push(pg);
        self.show_current();
    }

    pub fn back_to_parent(&mut self) {
        self.folders.pop();
        self.show_current();
    }

    pub fn rotate_profile(&mut self, dir: i32) {
        let ids = self.candidates();
        if ids.is_empty() {
            return;
        }
        let cur = self.profile.as_ref().and_then(|p| ids.iter().position(|i| *i == p.id)).map(|i| i as i32).unwrap_or(-1);
        let next = (cur + dir).rem_euclid(ids.len() as i32) as usize;
        let id = ids[next].clone();
        self.load_profile(Some(&id), None);
    }

    /// Drehung und „Aktiv“ aus den Einstellungen des Geräts (wie Python: rotation == 180, enabled).
    fn read_device_settings(&mut self) {
        let d = self.cfg.device(&self.serial());
        self.rotated = d.get("rotation").and_then(Value::as_i64) == Some(180);
        self.enabled = d.get("enabled") != Some(&Value::Bool(false));
    }

    pub fn set_brightness(&mut self, v: u8) {
        self.brightness = v.min(100);
        if self.enabled {
            let _ = self.dev.set_brightness(self.brightness);
        }
        self.remember();
    }

    fn env(&self) -> (usize, usize) {
        (self.page_index, self.page_count())
    }

    /// Umgebung für eine Aktion (`cfg` getrennt, damit die Aktion selbst veränderlich geliehen werden kann).
    fn make_env<'a>(&self, cfg: &'a Map<String, Value>, outbox: Outbox) -> Env<'a> {
        let (page_index, page_count) = self.env();
        Env { cfg, page_index, page_count, swap_cmd_ctrl: self.swap_cmd_ctrl(), outbox, brightness: self.brightness }
    }

    /// Profil vom Mac (lindeck-meta.json „OSType“): Cmd und Strg in Tastenkürzeln tauschen.
    fn swap_cmd_ctrl(&self) -> bool {
        self.profile.as_ref()
            .and_then(|p| p.meta.get("OSType").and_then(Value::as_str))
            .map(|o| o.to_lowercase().starts_with("mac"))
            .unwrap_or(false)
    }

    /// Aktionen der Seite anlegen und alle Tasten zeichnen.
    pub fn show_current(&mut self) {
        self.keys.clear();
        self.dials.clear();
        self.infobar.clear();
        self.overlays.clear();
        self.dial_alerts.clear();
        self.tiles.clear();
        let cols = self.geometry.cols;
        if let Some(pg) = self.current_page() {
            for ((c, r), data) in &pg.keypad {
                if *c < cols && *r < self.geometry.rows {
                    self.keys.insert(r * cols + c, Action::new(data.clone(), pg.dir.clone()));
                }
            }
            for (i, data) in &pg.encoders {
                let mut a = Action::new(data.clone(), pg.dir.clone());
                a.data.dial = true;
                self.dials.insert(*i, a);
            }
            for (i, data) in &pg.infobar {
                self.infobar.insert(*i, Action::new(data.clone(), pg.dir.clone()));
            }
            if let Some(bg) = pg.image_path(&pg.background) {
                self.tiles = render::background_tiles(&bg, cols, self.geometry.rows, self.dev.key_size(), pg.background_dim);
            }
        }
        self.render_all();
    }

    /// Bild einer Taste (ohne Gerät – auch für die Vorschau im Fenster).
    pub fn key_image(&self, k: u8) -> RgbaImage {
        let size = self.dev.key_size();
        let cols = self.geometry.cols.max(1);
        let tile = self.tiles.get(&(k % cols, k / cols));
        if self.profile.is_none() {
            return no_profile_key(size, k == 0);
        }
        match self.keys.get(&k) {
            None => tile.map(|t| render::fit(t, size.0, size.1)).unwrap_or_else(|| RgbaImage::from_pixel(size.0, size.1, image::Rgba([0, 0, 0, 255]))),
            Some(a) => {
                let env = self.make_env(&self.cfg.data, self.outbox.for_key(Some(k)));
                let overlay = self.overlays.get(&k).map(|(o, _)| *o);
                a.image(&env, size, tile, overlay)
            }
        }
    }

    pub fn render_key(&mut self, k: u8) {
        if !self.dev.has_display() {
            return;
        }
        let img = self.key_image(k);
        if self.shown.get(&k) == Some(&img) {
            return;
        }
        // gedrehtes Gerät: Taste k liegt gegenüber, Bild auf dem Kopf
        let res = if self.rotated {
            let n = self.geometry.keys() as u8;
            self.dev.set_key(n.saturating_sub(1).saturating_sub(k), &image::imageops::rotate180(&img))
        } else {
            self.dev.set_key(k, &img)
        };
        if let Err(e) = res {
            log::debug!("Taste {k}: {e}");
        }
        self.shown.insert(k, img);
    }

    pub fn render_all(&mut self) {
        for k in 0..self.geometry.keys() as u8 {
            self.render_key(k);
        }
        self.render_lcd();
        self.update_touch_keys();
        let _ = self.dev.flush();
    }

    // -------------------------------------------------------------- Touchstreifen und Infoleiste
    /// Hat das Gerät einen Touchstreifen über den Drehreglern (Stream Deck +)? Sonst ist die LCD-Fläche
    /// die Infoleiste (Neo).
    fn has_strip(&self) -> bool {
        self.geometry.dials > 0
    }

    /// Bild des Touchstreifens (wie `touchscreen_image`): ein Feld je Drehregler über dem
    /// Hintergrund der Seite (Encoder-Hintergrund). None ohne Touchstreifen.
    pub fn touchscreen_image(&self) -> Option<RgbaImage> {
        let (w, h) = self.dev.lcd_size()?;
        if !self.has_strip() {
            return None;
        }
        let n = self.geometry.dials.max(1) as u32;
        let seg = (w / n, h);
        let pg = self.current_page();
        let bg = pg.as_ref().and_then(|p| p.image_path(&p.encoder_background)).and_then(|p| render::load_image(&p, w, h));
        let env = self.make_env(&self.cfg.data, self.outbox.for_key(None));
        let segs: Vec<RgbaImage> = (0..n as u8)
            .map(|i| match self.dials.get(&i) {
                Some(a) => a.dial_segment(&env, seg, self.dial_alerts.contains_key(&i)),
                None => RgbaImage::new(seg.0, seg.1),
            })
            .collect();
        Some(render::compose_strip((w, h), &segs, bg.as_ref()))
    }

    /// Bild der Infoleiste des Neo (wie `infobar_image`): die Uhr aus der Infoleiste der Seite, sonst
    /// Uhrzeit und Seitenpunkte. None ohne Infoleiste.
    pub fn infobar_image(&self) -> Option<RgbaImage> {
        let size = self.dev.lcd_size()?;
        if self.has_strip() {
            return None;
        }
        let env = self.make_env(&self.cfg.data, self.outbox.for_key(None));
        let mut slots: Vec<&u8> = self.infobar.keys().collect();
        slots.sort();
        let clock = slots.into_iter().filter_map(|i| self.infobar.get(i)).find(|a| a.data.uuid == DIGITAL_TIME);
        if let Some(img) = clock.and_then(|a| a.behavior.image(&a.data, &env, size)) {
            return Some(img);
        }
        let n = self.page_count().max(1);
        let now = jiff::Zoned::now();
        let hm = format!("{:02}:{:02}", now.hour(), now.minute());
        Some(render::compose_infobar(size, &hm, "", (n > 1).then_some((n, self.page_index))))
    }

    /// Werte eines Drehreglers (Titel, Wert, Balken) für die Ringe im Fenster; None: Regler leer.
    pub fn dial_feedback(&self, i: u8) -> Option<crate::actions::Feedback> {
        let a = self.dials.get(&i)?;
        let env = self.make_env(&self.cfg.data, self.outbox.for_key(None));
        Some(a.behavior.feedback(&a.data, &env))
    }

    /// Was gerade auf Touchstreifen bzw. Infoleiste gehört (ohne Gerät – auch für die Vorschau).
    pub fn lcd_frame(&self) -> Option<RgbaImage> {
        self.touchscreen_image().or_else(|| self.infobar_image())
    }

    /// Zuletzt gezeigtes Bild auf Touchstreifen bzw. Infoleiste.
    pub fn lcd_image(&self) -> Option<&RgbaImage> {
        self.lcd_shown.as_ref()
    }

    /// Touchstreifen bzw. Infoleiste zeichnen (nur, wenn sich das Bild geändert hat).
    pub fn render_lcd(&mut self) {
        if !self.dev.has_display() {
            return;
        }
        let Some(img) = self.lcd_frame() else { return };
        if self.lcd_shown.as_ref() == Some(&img) {
            return;
        }
        let res = if self.rotated { self.dev.set_lcd(&image::imageops::rotate180(&img)) } else { self.dev.set_lcd(&img) };
        if let Err(e) = res {
            log::debug!("LCD: {e}");
        }
        self.lcd_shown = Some(img);
    }

    /// Neo: die beiden Touch-Tasten leuchten weiß, wenn es weitere Seiten gibt und kein Ordner offen ist.
    fn update_touch_keys(&mut self) {
        if self.geometry.touch_keys == 0 {
            return;
        }
        let on = self.page_count() > 1 && self.folders.is_empty();
        if self.touch_keys_on == Some(on) {
            return;
        }
        for i in 0..self.geometry.touch_keys {
            if let Err(e) = self.dev.set_touch_key_color(i, if on { [255, 255, 255] } else { [0, 0, 0] }) {
                log::debug!("Touch-Taste {i}: {e}");
            }
        }
        self.touch_keys_on = Some(on);
    }

    // ------------------------------------------------------------------------------------ Eingaben
    pub fn handle(&mut self, ev: Event) {
        if !self.enabled {
            return;
        }
        let ev = if self.rotated { self.physical(ev) } else { ev };
        self.last_input = Instant::now();
        if self.asleep {
            self.wake();
            return;
        }
        match ev {
            Event::KeyDown(k) => {
                self.pressed.insert(k, Instant::now());
                self.with_action(k, |a, env| a.behavior.key_down(&a.data, env));
            }
            Event::KeyUp(k) => {
                let held = self.pressed.remove(&k).map(|t| t.elapsed().as_millis() as u64).unwrap_or(0);
                self.with_action(k, |a, env| a.behavior.key_up(&a.data, env, held));
            }
            Event::DialDown(d) => {
                self.dial_pressed.insert(d, Instant::now());
                self.with_dial(d, |a, env| a.behavior.dial_down(&a.data, env));
            }
            Event::DialUp(d) => {
                let held = self.dial_pressed.remove(&d).map(|t| t.elapsed().as_millis() as u64).unwrap_or(0);
                self.with_dial(d, |a, env| a.behavior.dial_up(&a.data, env, held));
            }
            Event::DialTurn(d, ticks) => self.with_dial(d, |a, env| a.behavior.dial_turn(&a.data, env, ticks)),
            Event::Touch { x, long } => {
                // Touchstreifen: gleich breite Felder über den Drehreglern
                let n = self.geometry.dials.max(1) as u32;
                let width = self.dev.lcd_size().map(|s| s.0).unwrap_or(800).max(1);
                let d = ((x as u32 * n) / width).min(n - 1) as u8;
                self.with_dial(d, |a, env| a.behavior.touch(&a.data, env, long));
            }
            // Wischen über den Touchstreifen: nach rechts = vorige Seite, nach links = nächste (wie on_touch)
            Event::Swipe { from, to } => {
                let dx = to as i32 - from as i32;
                if dx.abs() > 40 {
                    self.run(Slot::Strip, if dx > 0 { Command::PreviousPage } else { Command::NextPage });
                }
            }
            // Touch-Tasten des Neo: links = vorige Seite, rechts = nächste
            Event::TouchKeyDown(i) => self.run(Slot::Strip, if i == 0 { Command::PreviousPage } else { Command::NextPage }),
            Event::TouchKeyUp(_) => {}
        }
        // Drehregler zeigen ihren Wert im Touchstreifen
        if matches!(ev, Event::DialDown(_) | Event::DialUp(_) | Event::DialTurn(..) | Event::Touch { .. }) {
            self.render_lcd();
        }
    }

    fn with_action(&mut self, k: u8, f: impl FnOnce(&mut Action, &Env) -> Command) {
        let cfg = self.cfg.data.clone();
        let env = self.make_env(&cfg, self.outbox.for_key(Some(k)));
        let Some(a) = self.keys.get_mut(&k) else { return };
        let cmd = f(a, &env);
        self.run(Slot::Key(k), cmd);
    }

    fn with_dial(&mut self, d: u8, f: impl FnOnce(&mut Action, &Env) -> Command) {
        let cfg = self.cfg.data.clone();
        let env = self.make_env(&cfg, self.outbox.for_key(None));
        let Some(a) = self.dials.get_mut(&d) else { return };
        let cmd = f(a, &env);
        self.run(Slot::Dial(d), cmd);
    }

    /// Befehl ausführen; `slot` = woher er kommt (Taste: Hinweise auf der Taste, Drehregler: im Touchstreifen).
    fn run(&mut self, slot: Slot, cmd: Command) {
        let key = match slot {
            Slot::Key(k) => Some(k),
            _ => None,
        };
        match cmd {
            Command::None => {}
            Command::OpenFolder(u) => self.open_folder(&u),
            Command::Back => self.back_to_parent(),
            Command::NextPage => {
                let n = self.page_count();
                if n > 0 {
                    self.goto_page((self.page_index + 1) % n);
                }
            }
            Command::PreviousPage => {
                let n = self.page_count();
                if n > 0 {
                    self.goto_page((self.page_index + n - 1) % n);
                }
            }
            Command::GotoPage(i) => self.goto_page(i),
            Command::SwitchProfile(p) => self.load_profile(Some(&p), None),
            Command::RotateProfile(d) => self.rotate_profile(d),
            Command::SetBrightness(v) => self.set_brightness(v),
            Command::ChangeBrightness(d) => self.set_brightness((self.brightness as i32 + d).clamp(0, 100) as u8),
            Command::Sleep => self.sleep(),
            Command::Run(cmds) => {
                for c in cmds {
                    self.run(slot, c);
                }
            }
            Command::SetState(s) => match slot {
                Slot::Key(k) => {
                    if let Some(a) = self.keys.get_mut(&k) {
                        a.data.state = s % a.data.state_count();
                    }
                    self.render_key(k);
                    let _ = self.dev.flush();
                }
                Slot::Dial(d) => {
                    if let Some(a) = self.dials.get_mut(&d) {
                        a.data.state = s % a.data.state_count();
                    }
                    self.render_lcd();
                }
                Slot::Strip => {}
            },
            Command::Ok | Command::Alert => match slot {
                Slot::Key(k) => {
                    let o = if cmd == Command::Ok { Overlay::Ok } else { Overlay::Alert };
                    self.overlays.insert(k, (o, Instant::now() + OVERLAY_TIME));
                    self.render_key(k);
                    let _ = self.dev.flush();
                }
                // im Touchstreifen erscheint nur „Fehler“ (wie in der Python-Version)
                Slot::Dial(d) if cmd == Command::Alert => {
                    self.dial_alerts.insert(d, Instant::now() + OVERLAY_TIME);
                    self.render_lcd();
                }
                _ => {}
            },
            Command::Progress(p) => {
                let Some(k) = key else { return };
                match p {
                    Some((done, steps)) => {
                        self.overlays.insert(k, (Overlay::Progress { done, steps }, Instant::now() + PROGRESS_TIME));
                    }
                    None => {
                        self.overlays.remove(&k);
                    }
                }
                self.render_key(k);
                let _ = self.dev.flush();
            }
            Command::Redraw => match slot {
                Slot::Key(k) => {
                    self.render_key(k);
                    let _ = self.dev.flush();
                }
                Slot::Dial(_) | Slot::Strip => self.render_lcd(),
            },
        }
    }

    /// Nach geänderten Einstellungen: Drehung und „Aktiv“ übernehmen (`redraw`: gleich neu zeichnen).
    pub fn apply_device_settings(&mut self, redraw: bool) {
        let (rot, en) = (self.rotated, self.enabled);
        self.read_device_settings();
        if en != self.enabled {
            let _ = self.dev.set_brightness(if self.enabled && !self.asleep { self.brightness } else { 0 });
        }
        if rot != self.rotated {
            self.shown.clear();
            self.lcd_shown = None;
            if redraw {
                self.render_all();
                self.render_lcd();
            }
        }
    }

    /// Ereignis vom gedrehten Gerät auf Tasten/Regler im Profil umrechnen.
    fn physical(&self, ev: Event) -> Event {
        let n = self.geometry.keys() as u8;
        let d = self.geometry.dials;
        let t = self.geometry.touch_keys;
        let w = self.dev.lcd_size().map(|s| s.0 as u16).unwrap_or(800);
        let key = |k: u8| n.saturating_sub(1).saturating_sub(k);
        let dial = |i: u8| d.saturating_sub(1).saturating_sub(i);
        match ev {
            Event::KeyDown(k) => Event::KeyDown(key(k)),
            Event::KeyUp(k) => Event::KeyUp(key(k)),
            Event::DialDown(i) => Event::DialDown(dial(i)),
            Event::DialUp(i) => Event::DialUp(dial(i)),
            // gedreht: rechts herum wird links herum
            Event::DialTurn(i, v) => Event::DialTurn(dial(i), v),
            Event::Touch { x, long } => Event::Touch { x: w.saturating_sub(x), long },
            Event::Swipe { from, to } => Event::Swipe { from: w.saturating_sub(from), to: w.saturating_sub(to) },
            Event::TouchKeyDown(i) => Event::TouchKeyDown(t.saturating_sub(1).saturating_sub(i)),
            Event::TouchKeyUp(i) => Event::TouchKeyUp(t.saturating_sub(1).saturating_sub(i)),
        }
    }

    /// Beim Beenden: das Inaktiv-Bild (Einstellungen › Geräte) über alle Tasten und die LCD-Fläche legen,
    /// statt das Gerät leer zu lassen. Gibt true zurück, wenn es gezeigt wurde.
    pub fn show_idle_image(&mut self) -> bool {
        let path = self.cfg.device(&self.serial()).get("idle_image").and_then(Value::as_str).map(std::path::PathBuf::from);
        let Some(path) = path.filter(|p| p.is_file()) else { return false };
        if !self.dev.has_display() || self.serial() == crate::virtual_deck::SERIAL {
            return false;
        }
        let tiles = render::background_tiles(&path, self.geometry.cols, self.geometry.rows, self.dev.key_size(), 0);
        if tiles.is_empty() {
            return false;
        }
        let cols = self.geometry.cols.max(1);
        let n = self.geometry.keys() as u8;
        for k in 0..n {
            if let Some(t) = tiles.get(&(k % cols, k / cols)) {
                let (phys, img) = if self.rotated { (n - 1 - k, image::imageops::rotate180(t)) } else { (k, t.clone()) };
                let _ = self.dev.set_key(phys, &img);
            }
        }
        if let Some((w, h)) = self.dev.lcd_size() {
            if let Some(img) = render::load_image(&path, w, h).map(|i| render::fit(&i, w, h)) {
                let img = if self.rotated { image::imageops::rotate180(&img) } else { img };
                let _ = self.dev.set_lcd(&img);
            }
        }
        for i in 0..self.geometry.touch_keys {
            let _ = self.dev.set_touch_key_color(i, [0, 0, 0]);
        }
        let _ = self.dev.set_brightness(if self.enabled { self.brightness } else { 0 });
        let _ = self.dev.flush();
        true
    }

    pub fn sleep(&mut self) {
        self.asleep = true;
        let _ = self.dev.set_brightness(0);
    }

    pub fn wake(&mut self) {
        self.asleep = false;
        if self.enabled {
            let _ = self.dev.set_brightness(self.brightness);
        }
    }

    /// Alle paar hundert Millisekunden: Hinweise ausblenden, Uhren nachziehen, Ruhezustand.
    pub fn tick(&mut self) {
        for (key, cmd) in self.outbox.drain() {
            self.run(Slot::from_posted(key), cmd);
        }
        let now = Instant::now();
        let expired: Vec<u8> = self.overlays.iter().filter(|(_, (_, until))| *until <= now).map(|(k, _)| *k).collect();
        let mut dirty = false;
        for k in expired {
            self.overlays.remove(&k);
            self.render_key(k);
            dirty = true;
        }
        let before = self.dial_alerts.len();
        self.dial_alerts.retain(|_, until| *until > now);
        let mut lcd_dirty = self.dial_alerts.len() != before;
        let keys: Vec<u8> = self.keys.iter().filter(|(_, a)| a.behavior.needs_tick()).map(|(k, _)| *k).collect();
        for k in keys {
            let redraw = self.keys.get_mut(&k).map(|a| {
                let mut r = a.behavior.tick(&a.data);
                // Zustand aus der Live-Verbindung übernehmen (zeigt das Bild des passenden Zustands)
                if let Some(s) = a.behavior.live_state(&a.data) {
                    let s = s % a.data.state_count();
                    if s != a.data.state {
                        a.data.state = s;
                        r = true;
                    }
                }
                r
            }).unwrap_or(false);
            if redraw {
                self.render_key(k);
                dirty = true;
            }
        }
        // Drehregler: Zustand nachziehen (Countdown-Alarm, Systemmonitor …), dann den Touchstreifen
        for a in self.dials.values_mut() {
            if a.behavior.needs_tick() {
                lcd_dirty |= a.behavior.tick(&a.data);
            }
        }
        // Infoleiste: Uhr (das Bild geht nur bei einer Änderung ans Gerät)
        for a in self.infobar.values_mut() {
            if a.behavior.needs_tick() {
                a.behavior.tick(&a.data);
                if let Some(s) = a.behavior.live_state(&a.data) {
                    a.data.state = s % a.data.state_count();
                }
            }
        }
        if lcd_dirty || (!self.has_strip() && self.dev.lcd_size().is_some()) {
            self.render_lcd();
        }
        if dirty {
            let _ = self.dev.flush();
        }
        let sleep_min = self.cfg.device(&self.serial()).get("sleep_after_minutes").and_then(Value::as_i64)
            .unwrap_or_else(|| self.cfg.get_i64("sleep_after_minutes", 0));
        if sleep_min > 0 && !self.asleep && self.last_input.elapsed() > Duration::from_secs(sleep_min as u64 * 60) {
            self.sleep();
        }
    }

    /// Gibt es auf der Seite Tasten oder Drehregler mit schnellem Takt (Pegel der FreeWave-Fader)?
    pub fn has_fast_tick(&self) -> bool {
        self.keys.values().chain(self.dials.values()).any(|a| a.behavior.needs_fast_tick(&a.data))
    }

    /// Schneller Takt (etwa 12× pro Sekunde, siehe [`crate::daemon`]): Live-Anzeigen nachziehen. Eine Taste
    /// wird nur gezeichnet, wenn ihre Aktion eine Änderung meldet, und nur gesendet, wenn sich das Bild
    /// geändert hat.
    pub fn fast_tick(&mut self) {
        if self.asleep {
            return;
        }
        let keys: Vec<u8> = self.keys.iter().filter(|(_, a)| a.behavior.needs_fast_tick(&a.data)).map(|(k, _)| *k).collect();
        let mut dirty = false;
        for k in keys {
            if self.keys.get_mut(&k).map(|a| a.behavior.fast_tick(&a.data)).unwrap_or(false) {
                self.render_key(k);
                dirty = true;
            }
        }
        let mut lcd = false;
        for a in self.dials.values_mut() {
            if a.behavior.needs_fast_tick(&a.data) {
                lcd |= a.behavior.fast_tick(&a.data);
            }
        }
        if lcd {
            self.render_lcd();
        }
        if dirty {
            let _ = self.dev.flush();
        }
    }

    /// Profile neu gelesen (Editor hat gespeichert): gleiche Seite neu zeichnen.
    pub fn reload(&mut self) {
        if self.cfg.reload_if_changed() {
            self.apply_device_settings(false);
            // Profil fürs Gerät in den Einstellungen geändert?
            let want = self.cfg.device(&self.serial()).get("profile").and_then(Value::as_str).map(str::to_string);
            if want.is_some() && want != self.profile.as_ref().map(|p| p.id.clone()) {
                self.load_profile(want.as_deref(), None);
                return;
            }
        }
        let id = self.profile.as_ref().map(|p| p.id.clone());
        let (idx, folders): (usize, Vec<String>) = (self.page_index, self.folders.iter().map(|f| f.uuid.clone()).collect());
        self.profile = id.as_deref().and_then(|p| self.store.lock().unwrap().get(p).cloned());
        if self.profile.is_none() {
            let pid = self.initial_profile();
            self.load_profile(pid.as_deref(), None);
            return;
        }
        self.page_index = idx.min(self.page_count().saturating_sub(1));
        self.folders = folders.iter().filter_map(|u| self.profile.as_ref()?.page(u)).collect();
        self.shown.clear();
        self.lcd_shown = None;
        self.touch_keys_on = None;
        self.show_current();
    }

    /// Alle Tasten als ein Bild (Vorschau für Fenster und Doku), mit `gap` Pixeln Abstand.
    pub fn preview(&self, gap: u32) -> RgbaImage {
        let (w, h) = self.dev.key_size();
        let (cols, rows) = (self.geometry.cols as u32, self.geometry.rows as u32);
        let mut out = RgbaImage::from_pixel(cols * w + (cols + 1) * gap, rows * h + (rows + 1) * gap, image::Rgba([23, 23, 23, 255]));
        for k in 0..(cols * rows) {
            let img = self.key_image(k as u8);
            image::imageops::overlay(&mut out, &img, (gap + (k % cols) * (w + gap)) as i64, (gap + (k / cols) * (h + gap)) as i64);
        }
        out
    }
}
