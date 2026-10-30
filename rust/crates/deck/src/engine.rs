//! Steuerung eines Decks (wie `freestream/engine.py`): Profil und Seite wählen, Tastenbilder zeichnen,
//! Eingaben an die Aktionen geben und ihre [`Command`]s ausführen (Seiten, Ordner, Profile, Helligkeit).

use freestream_core::config::{Config, State};
use freestream_core::models::Geometry;
use freestream_core::profiles::{Page, Profile, ProfileStore};
use image::RgbaImage;
use serde_json::Value;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crate::actions::{Action, Command, Env};
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
    overlays: HashMap<u8, (Overlay, Instant)>,
    tiles: HashMap<(u8, u8), RgbaImage>,
    /// Bilder, die gerade auf dem Gerät stehen (für das Fenster und um Doppeltes zu sparen)
    pub shown: HashMap<u8, RgbaImage>,
    last_input: Instant,
    asleep: bool,
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
            overlays: HashMap::new(),
            tiles: HashMap::new(),
            shown: HashMap::new(),
            last_input: Instant::now(),
            asleep: false,
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
        let _ = self.dev.set_brightness(self.brightness);
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

    pub fn set_brightness(&mut self, v: u8) {
        self.brightness = v.min(100);
        let _ = self.dev.set_brightness(self.brightness);
        self.remember();
    }

    fn env(&self) -> (usize, usize) {
        (self.page_index, self.page_count())
    }

    /// Aktionen der Seite anlegen und alle Tasten zeichnen.
    pub fn show_current(&mut self) {
        self.keys.clear();
        self.overlays.clear();
        self.tiles.clear();
        let cols = self.geometry.cols;
        if let Some(pg) = self.current_page() {
            for ((c, r), data) in &pg.keypad {
                if *c < cols && *r < self.geometry.rows {
                    self.keys.insert(r * cols + c, Action::new(data.clone(), pg.dir.clone()));
                }
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
                let (page_index, page_count) = self.env();
                let env = Env { cfg: &self.cfg.data, page_index, page_count };
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
        if let Err(e) = self.dev.set_key(k, &img) {
            log::debug!("Taste {k}: {e}");
        }
        self.shown.insert(k, img);
    }

    pub fn render_all(&mut self) {
        for k in 0..self.geometry.keys() as u8 {
            self.render_key(k);
        }
        let _ = self.dev.flush();
    }

    // ------------------------------------------------------------------------------------ Eingaben
    pub fn handle(&mut self, ev: Event) {
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
            other => log::debug!("Eingabe {other:?} – noch nicht belegt"),
        }
    }

    fn with_action(&mut self, k: u8, f: impl FnOnce(&mut Action, &Env) -> Command) {
        let (page_index, page_count) = self.env();
        let cfg = self.cfg.data.clone();
        let Some(a) = self.keys.get_mut(&k) else { return };
        let env = Env { cfg: &cfg, page_index, page_count };
        let cmd = f(a, &env);
        self.run(k, cmd);
    }

    fn run(&mut self, k: u8, cmd: Command) {
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
            Command::Ok | Command::Alert => {
                let o = if cmd == Command::Ok { Overlay::Ok } else { Overlay::Alert };
                self.overlays.insert(k, (o, Instant::now()));
                self.render_key(k);
                let _ = self.dev.flush();
            }
            Command::Redraw => {
                self.render_key(k);
                let _ = self.dev.flush();
            }
        }
    }

    pub fn sleep(&mut self) {
        self.asleep = true;
        let _ = self.dev.set_brightness(0);
    }

    pub fn wake(&mut self) {
        self.asleep = false;
        let _ = self.dev.set_brightness(self.brightness);
    }

    /// Alle paar hundert Millisekunden: Hinweise ausblenden, Uhren nachziehen, Ruhezustand.
    pub fn tick(&mut self) {
        let expired: Vec<u8> = self.overlays.iter().filter(|(_, (_, t))| t.elapsed() > Duration::from_millis(1200)).map(|(k, _)| *k).collect();
        let mut dirty = false;
        for k in expired {
            self.overlays.remove(&k);
            self.render_key(k);
            dirty = true;
        }
        let keys: Vec<u8> = self.keys.iter().filter(|(_, a)| a.behavior.needs_tick()).map(|(k, _)| *k).collect();
        for k in keys {
            let redraw = self.keys.get_mut(&k).map(|a| a.behavior.tick(&a.data)).unwrap_or(false);
            if redraw {
                self.render_key(k);
                dirty = true;
            }
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

    /// Profile neu gelesen (Editor hat gespeichert): gleiche Seite neu zeichnen.
    pub fn reload(&mut self) {
        if self.cfg.reload_if_changed() {
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
