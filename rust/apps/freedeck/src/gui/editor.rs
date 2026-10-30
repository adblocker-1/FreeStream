//! Hauptfenster von FreeDeck.

use gtk4 as gtk;
use gtk::prelude::*;
use gtk::{glib, Align, Orientation};

use freestream_core::editdoc::{Clip, ProfileDoc, Slot};
use freestream_core::models::{model_geometry, model_name, Geometry};
use freestream_core::profiles::ProfileStore;
use freestream_core::tr;
use freestream_deck::daemon::Daemon;
use freestream_deck::device::MemoryDeck;
use freestream_deck::engine::Controller;
use image::RgbaImage;
use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;
use std::sync::{Arc, Mutex};

use super::util::{icon_button, label, rgb, rounded, surface};

/// Größe der Tasten in der Vorschau (Pixel); die Bühne skaliert beim Zeichnen.
const KEY_PX: u32 = 144;

/// Zustand des Fensters.
pub struct State {
    pub store: Arc<Mutex<ProfileStore>>,
    pub daemon: Option<Arc<Mutex<Daemon>>>,
    /// Gerät, dessen Profil gezeigt wird (Seriennummer) – None: kein Gerät, nur das Profil
    pub serial: Option<String>,
    pub profile: Option<String>,
    /// Vorschau: Steuerung mit einem Deck im Speicher (zeichnet wie das Gerät)
    pub preview: Option<Controller>,
    pub selected: Option<u8>,
    pub status: String,
    pub keys: HashMap<u8, RgbaImage>,
    /// Profil zum Bearbeiten (Rückgängig, Speichern)
    pub doc: Option<ProfileDoc>,
    /// Zwischenablage für Tasten
    pub clip: Option<Clip>,
    /// Aktion aus der Liste auf die Taste legen (Doppelklick in der Aktionsliste)
    pub on_add: Option<Rc<dyn Fn(String)>>,
    /// Zustand der Taste, der bearbeitet wird (Tasten mit mehreren Zuständen)
    pub state_idx: usize,
    /// Speichern nach dem Tippen (verzögert)
    save_job: Option<glib::SourceId>,
}

pub struct Editor {
    pub win: gtk::ApplicationWindow,
    pub state: Rc<RefCell<State>>,
    dev_label: gtk::Label,
    prof_label: gtk::Label,
    dev_list: gtk::Box,
    prof_list: gtk::Box,
    pub(super) stage: gtk::DrawingArea,
    pages: gtk::Box,
    foot: gtk::CenterBox,
    status: gtk::Label,
    pub(super) insp_head: gtk::Box,
    pub(super) insp_body: gtk::Box,
    /// Vorschaubild in den Eigenschaften (wird beim Tippen nachgezogen)
    pub(super) insp_pic: RefCell<Option<gtk::Picture>>,
}

impl Editor {
    pub fn build(app: &gtk::Application, daemon: Option<Arc<Mutex<Daemon>>>) -> gtk::ApplicationWindow {
        let store = daemon
            .as_ref()
            .map(|d| d.lock().unwrap().store.clone())
            .unwrap_or_else(|| Arc::new(Mutex::new(ProfileStore::new(None))));
        let state = Rc::new(RefCell::new(State {
            store,
            daemon,
            serial: None,
            profile: None,
            preview: None,
            selected: None,
            status: String::new(),
            keys: HashMap::new(),
            doc: None,
            clip: None,
            on_add: None,
            state_idx: 0,
            save_job: None,
        }));

        let win = gtk::ApplicationWindow::builder().application(app).title("FreeDeck").default_width(1280).default_height(800).build();
        let body = gtk::Box::new(Orientation::Horizontal, 0);
        win.set_child(Some(&body));

        // ---------------------------------------------------------------- Mitte
        let main = gtk::Box::new(Orientation::Vertical, 0);
        main.add_css_class("main");
        main.set_hexpand(true);
        body.append(&main);

        // Kopf: Gerät groß, Profil darunter; rechts die Symbole
        let top = gtk::Box::new(Orientation::Horizontal, 0);
        top.set_margin_start(30);
        top.set_margin_end(30);
        top.set_margin_top(14);
        main.append(&top);
        let names = gtk::Box::new(Orientation::Vertical, 0);
        names.set_hexpand(true);
        top.append(&names);
        let (dev_btn, dev_label, dev_list) = title_drop("FreeDeck", "device-title");
        let (prof_btn, prof_label, prof_list) = title_drop(&tr!("No profile"), "profile-title");
        names.append(&dev_btn);
        names.append(&prof_btn);
        let icons = gtk::Box::new(Orientation::Horizontal, 6);
        icons.set_valign(Align::Start);
        top.append(&icons);
        let bell = icon_button("preferences-system-notifications-symbolic", &tr!("Updates"));
        let store_btn = icon_button("application-x-addon-symbolic", &tr!("Plugins"));
        let gear = icon_button("emblem-system-symbolic", &tr!("Settings"));
        for b in [&bell, &store_btn, &gear] {
            icons.append(b);
        }

        // Bühne mit den Tasten
        let stage = gtk::DrawingArea::new();
        stage.set_vexpand(true);
        stage.set_hexpand(true);
        stage.set_margin_start(12);
        stage.set_margin_end(12);
        stage.set_margin_top(12);
        main.append(&stage);

        // Statuszeile und Seitenzahlen
        let status = label("", &["status"]);
        status.set_margin_start(30);
        status.set_margin_bottom(6);
        main.append(&status);
        let foot = gtk::CenterBox::new();
        foot.set_margin_bottom(12);
        foot.set_margin_start(30);
        foot.set_margin_end(30);
        let pages = gtk::Box::new(Orientation::Horizontal, 0);
        pages.add_css_class("pages");
        foot.set_center_widget(Some(&pages));
        main.append(&foot);

        // Eigenschaften
        let insp = gtk::Box::new(Orientation::Vertical, 10);
        insp.add_css_class("panel");
        insp.set_size_request(-1, 250);
        let insp_head = gtk::Box::new(Orientation::Horizontal, 6);
        insp_head.set_margin_start(22);
        insp_head.set_margin_end(22);
        insp_head.set_margin_top(14);
        insp.append(&insp_head);
        let sep = gtk::Box::new(Orientation::Horizontal, 0);
        sep.add_css_class("sep");
        sep.set_margin_start(22);
        sep.set_margin_end(22);
        insp.append(&sep);
        let insp_body = gtk::Box::new(Orientation::Horizontal, 18);
        insp_body.set_margin_start(22);
        insp_body.set_margin_end(22);
        insp.append(&insp_body);
        main.append(&insp);

        // --------------------------------------------------------------- rechts
        let vsep = gtk::Box::new(Orientation::Vertical, 0);
        vsep.add_css_class("sep");
        body.append(&vsep);
        let lib = super::library::build(&state);
        body.append(&lib);

        let ed = Rc::new(Editor { win: win.clone(), state, dev_label, prof_label, dev_list, prof_list, stage, pages, foot, status, insp_head, insp_body, insp_pic: RefCell::new(None) });
        ed.connect(&bell, &store_btn, &gear);
        ed.start();
        win
    }

    fn connect(self: &Rc<Self>, bell: &gtk::Button, store: &gtk::Button, gear: &gtk::Button) {
        let ed = self.clone();
        self.stage.set_draw_func(move |_, cr, w, h| ed.draw_stage(cr, w as f64, h as f64));
        let click = gtk::GestureClick::new();
        let ed = self.clone();
        click.connect_pressed(move |_, n, x, y| ed.stage_click(n, x, y));
        self.stage.add_controller(click);
        self.connect_dnd();
        {
            let ed = Rc::downgrade(self);
            self.state.borrow_mut().on_add = Some(Rc::new(move |uuid: String| {
                if let Some(ed) = ed.upgrade() {
                    ed.add_action(&uuid, None);
                }
            }));
        }
        let st = self.state.clone();
        let win = self.win.clone();
        bell.connect_clicked(move |_| {
            let _ = &st;
            info(&win, &tr!("Updates"), &tr!("You are using the latest version ({version}).", version = freestream_core::VERSION));
        });
        let win = self.win.clone();
        store.connect_clicked(move |_| info(&win, &tr!("Plugins"), &tr!("Plugins")));
        let (win, st) = (self.win.clone(), self.state.clone());
        gear.connect_clicked(move |_| super::settings::open(&win, &st));
    }

    /// Erstes Bild: angeschlossenes Gerät mit seinem Profil, sonst das erste Profil.
    fn start(self: &Rc<Self>) {
        let first = {
            let st = self.state.borrow();
            st.daemon.as_ref().and_then(|d| {
                let d = d.lock().unwrap();
                let c = d.controllers.lock().unwrap();
                c.iter().next().map(|(s, c)| (s.clone(), c.lock().unwrap().profile.as_ref().map(|p| p.id.clone())))
            })
        };
        let pid = match first {
            Some((serial, pid)) => {
                self.state.borrow_mut().serial = Some(serial);
                pid
            }
            None => self.state.borrow().store.lock().unwrap().profiles.keys().next().cloned(),
        };
        self.open_profile(pid.as_deref());
        // Geräte kommen und gehen: alle 2 s nachsehen, Profile neu lesen, wenn sie sich geändert haben
        let ed = self.clone();
        glib::timeout_add_seconds_local(2, move || {
            ed.refresh_devices();
            glib::ControlFlow::Continue
        });
    }

    /// Profil öffnen (in der Vorschau) – und auf dem gewählten Gerät zeigen.
    pub fn open_profile(self: &Rc<Self>, pid: Option<&str>) {
        {
            let mut st = self.state.borrow_mut();
            let prof = pid.and_then(|p| st.store.lock().unwrap().get(p).cloned());
            st.profile = prof.as_ref().map(|p| p.id.clone());
            st.selected = None;
            st.doc = prof.as_ref().and_then(|p| ProfileDoc::open(&p.root).map_err(|e| log::warn!("{e}")).ok());
            st.preview = prof.map(|p| {
                let model = if model_geometry(&p.model).is_some() { p.model.clone() } else { "20GBA9901".into() };
                let dev = Arc::new(MemoryDeck::new(&model, "EDITOR", (KEY_PX, KEY_PX)));
                let mut c = Controller::new(dev, st.store.clone());
                c.load_profile(Some(&p.id), Some(0));
                c
            });
            st.status = match &st.preview {
                Some(c) => tr!("{name} opened – changes are saved automatically", name = c.profile.as_ref().map(|p| p.name.clone()).unwrap_or_default()),
                None => tr!("No profile"),
            };
            // auf dem Gerät zeigen
            if let (Some(serial), Some(pid), Some(d)) = (st.serial.clone(), st.profile.clone(), st.daemon.clone()) {
                if let Some(c) = d.lock().unwrap().controllers.lock().unwrap().get(&serial).cloned() {
                    let mut c = c.lock().unwrap();
                    if c.profile.as_ref().map(|p| p.id != pid).unwrap_or(true) {
                        c.load_profile(Some(&pid), None);
                        c.cfg.set_device(&serial, "profile", serde_json::Value::from(pid.clone()));
                        let _ = c.cfg.save();
                    }
                }
            }
        }
        self.refresh();
    }

    /// Alles neu: Kopf, Bühne, Seiten, Eigenschaften, Listen.
    pub fn refresh(self: &Rc<Self>) {
        self.render_keys();
        self.refresh_header();
        self.refresh_pages();
        self.refresh_inspector();
        self.refresh_profile_list();
        self.refresh_device_list();
        self.stage.queue_draw();
    }

    fn render_keys(&self) {
        let mut st = self.state.borrow_mut();
        let imgs: HashMap<u8, RgbaImage> = match &st.preview {
            Some(c) => (0..c.geometry.keys() as u8).map(|k| (k, c.key_image(k))).collect(),
            None => HashMap::new(),
        };
        st.keys = imgs;
    }

    pub(super) fn geometry(&self) -> Geometry {
        self.state.borrow().preview.as_ref().map(|c| c.geometry).unwrap_or(Geometry::new(5, 3))
    }

    fn refresh_header(&self) {
        let st = self.state.borrow();
        let dev_name = match (&st.serial, &st.daemon) {
            (Some(serial), Some(d)) => d.lock().unwrap().controllers.lock().unwrap().get(serial)
                .map(|c| {
                    let c = c.lock().unwrap();
                    let custom = c.cfg.device(serial).get("name").and_then(|v| v.as_str()).map(str::to_string);
                    custom.filter(|n| !n.is_empty()).unwrap_or_else(|| model_name(&c.dev.model()))
                })
                .unwrap_or_else(|| tr!("No device connected")),
            _ => st.preview.as_ref().and_then(|c| c.profile.as_ref()).map(|p| p.model_name()).unwrap_or_else(|| "FreeDeck".into()),
        };
        self.dev_label.set_text(&dev_name);
        let prof = st.preview.as_ref().and_then(|c| c.profile.as_ref()).map(|p| p.name.clone()).unwrap_or_else(|| tr!("No profile"));
        self.prof_label.set_text(&prof);
        let page = st.preview.as_ref().map(|c| (c.page_index + 1, c.page_count())).unwrap_or((0, 0));
        let model = st.preview.as_ref().map(|c| model_name(&c.dev.model())).unwrap_or_default();
        let mut parts = vec![model];
        if page.1 > 0 {
            parts.push(tr!("Page {n} of {total}", n = page.0, total = page.1));
        }
        if !st.status.is_empty() {
            parts.push(st.status.clone());
        }
        self.status.set_text(&parts.join(" · "));
    }

    fn refresh_pages(self: &Rc<Self>) {
        while let Some(c) = self.pages.first_child() {
            self.pages.remove(&c);
        }
        let (cur, n) = self.state.borrow().preview.as_ref().map(|c| (c.page_index, c.page_count())).unwrap_or((0, 0));
        // links „↩ Zurück“ im Ordner, rechts „Hintergrund“
        let in_folder = self.state.borrow().preview.as_ref().map(|c| !c.folders.is_empty()).unwrap_or(false);
        if in_folder {
            let back = gtk::Button::with_label(&tr!("↩ Back"));
            back.add_css_class("btn");
            let ed = self.clone();
            back.connect_clicked(move |_| {
                if let Some(c) = ed.state.borrow_mut().preview.as_mut() {
                    c.back_to_parent();
                }
                ed.state.borrow_mut().selected = None;
                ed.refresh();
            });
            self.foot.set_start_widget(Some(&back));
        } else {
            self.foot.set_start_widget(None::<&gtk::Widget>);
        }
        if n > 0 {
            let has_bg = self.state.borrow().preview.as_ref().and_then(|c| c.current_page()).map(|p| !p.background.is_empty()).unwrap_or(false);
            let bg = gtk::Button::new();
            let row = gtk::Box::new(Orientation::Horizontal, 6);
            row.append(&gtk::Image::from_icon_name("image-x-generic-symbolic"));
            row.append(&gtk::Label::new(Some(&tr!("Background"))));
            bg.set_child(Some(&row));
            bg.add_css_class("btn");
            if has_bg {
                bg.add_css_class("suggested-action");
            }
            let ed = self.clone();
            bg.connect_clicked(move |_| super::bg_ui::open(&ed));
            self.foot.set_end_widget(Some(&bg));
        } else {
            self.foot.set_end_widget(None::<&gtk::Widget>);
        }
        for i in 0..n {
            let b = gtk::Button::with_label(&(i + 1).to_string());
            b.add_css_class("page-btn");
            if i == cur {
                b.add_css_class("current");
            }
            let ed = self.clone();
            b.connect_clicked(move |_| ed.goto_page(i));
            // Rechtsklick: Seite verschieben oder löschen
            let rc = gtk::GestureClick::new();
            rc.set_button(3);
            let (ed, bb) = (self.clone(), b.clone());
            rc.connect_pressed(move |_, _, _, _| ed.page_menu(&bb, i, n));
            b.add_controller(rc);
            // Taste auf eine Seitenzahl ziehen: auf diese Seite verschieben
            let drop = gtk::DropTarget::new(glib::Type::STRING, gtk::gdk::DragAction::MOVE);
            let ed = self.clone();
            drop.connect_drop(move |_, v, _, _| {
                let Some(from) = v.get::<String>().ok().and_then(|t| t.strip_prefix(KEY_DRAG).and_then(|s| s.parse::<u8>().ok())) else { return false };
                ed.move_key_to_page(from, i);
                true
            });
            b.add_controller(drop);
            self.pages.append(&b);
        }
        let add = gtk::Button::with_label("+");
        add.add_css_class("page-btn");
        add.set_tooltip_text(Some(&tr!("Add page")));
        let ed = self.clone();
        add.connect_clicked(move |_| ed.add_page());
        self.pages.append(&add);
        self.pages.set_visible(n > 0);
    }

    pub fn goto_page(self: &Rc<Self>, i: usize) {
        if let Some(c) = self.state.borrow_mut().preview.as_mut() {
            c.goto_page(i);
        }
        self.state.borrow_mut().selected = None;
        self.refresh();
    }

    // ------------------------------------------------------------------ Bühne
    /// Lage der Tasten: (x, y, Größe, Abstand) passend in die Fläche.
    fn layout(&self, w: f64, h: f64) -> (f64, f64, f64, f64) {
        let g = self.geometry();
        let (cols, rows) = (g.cols.max(1) as f64, g.rows.max(1) as f64);
        let gap_ratio = 0.14;
        let size = ((w - 40.0) / (cols + (cols - 1.0) * gap_ratio)).min((h - 20.0) / (rows + (rows - 1.0) * gap_ratio)).min(110.0).max(24.0);
        let gap = size * gap_ratio;
        let total_w = cols * size + (cols - 1.0) * gap;
        let total_h = rows * size + (rows - 1.0) * gap;
        ((w - total_w) / 2.0, (h - total_h) / 2.0, size, gap)
    }

    fn draw_stage(&self, cr: &gtk::cairo::Context, w: f64, h: f64) {
        let st = self.state.borrow();
        let g = self.geometry();
        let (x0, y0, size, gap) = self.layout(w, h);
        for k in 0..g.keys() as u8 {
            let (c, r) = ((k % g.cols) as f64, (k / g.cols) as f64);
            let (x, y) = (x0 + c * (size + gap), y0 + r * (size + gap));
            let radius = size / 8.0;
            let occupied = st.preview.as_ref().map(|p| p.current_page().map(|pg| pg.keypad.contains_key(&(k % g.cols, k / g.cols))).unwrap_or(false)).unwrap_or(false);
            match st.keys.get(&k).filter(|_| occupied || st.preview.as_ref().map(|p| p.current_page().map(|pg| !pg.background.is_empty()).unwrap_or(false)).unwrap_or(false)) {
                Some(img) => {
                    if let Some(s) = surface(img) {
                        let _ = cr.save();
                        rounded(cr, x, y, size, size, radius);
                        cr.clip();
                        let scale = size / img.width() as f64;
                        cr.translate(x, y);
                        cr.scale(scale, scale);
                        let _ = cr.set_source_surface(&s, 0.0, 0.0);
                        cr.source().set_filter(gtk::cairo::Filter::Good);
                        let _ = cr.paint();
                        let _ = cr.restore();
                    }
                }
                None => {
                    let (r_, g_, b_) = rgb("#262626");
                    cr.set_source_rgb(r_, g_, b_);
                    rounded(cr, x, y, size, size, radius);
                    let _ = cr.fill();
                }
            }
            if st.selected == Some(k) {
                let (r_, g_, b_) = rgb("#3d9bff");
                cr.set_source_rgb(r_, g_, b_);
                cr.set_line_width(2.5);
                rounded(cr, x - 3.0, y - 3.0, size + 6.0, size + 6.0, radius + 3.0);
                let _ = cr.stroke();
            }
        }
    }

    fn key_at(&self, x: f64, y: f64) -> Option<u8> {
        let g = self.geometry();
        let (x0, y0, size, gap) = self.layout(self.stage.width() as f64, self.stage.height() as f64);
        let (c, r) = (((x - x0) / (size + gap)).floor(), ((y - y0) / (size + gap)).floor());
        if c < 0.0 || r < 0.0 || c >= g.cols as f64 || r >= g.rows as f64 {
            return None;
        }
        let (kx, ky) = (x0 + c * (size + gap), y0 + r * (size + gap));
        (x - kx <= size && y - ky <= size).then_some(r as u8 * g.cols + c as u8)
    }

    fn stage_click(self: &Rc<Self>, n: i32, x: f64, y: f64) {
        let k = self.key_at(x, y);
        {
            let mut st = self.state.borrow_mut();
            if st.selected != k {
                st.state_idx = 0;
            }
            st.selected = k;
        }
        if n == 2 {
            // Doppelklick auf einen Ordner öffnet ihn
            let folder = {
                let st = self.state.borrow();
                let g = self.geometry();
                k.and_then(|k| st.preview.as_ref()?.current_page()?.keypad.get(&(k % g.cols, k / g.cols)).cloned())
                    .filter(|a| a.get("UUID").and_then(|u| u.as_str()) == Some("com.elgato.streamdeck.profile.openchild"))
                    .and_then(|a| a.get("Settings")?.get("ProfileUUID")?.as_str().map(str::to_string))
            };
            if let Some(f) = folder {
                if let Some(c) = self.state.borrow_mut().preview.as_mut() {
                    c.open_folder(&f);
                }
                self.state.borrow_mut().selected = None;
                self.refresh();
                return;
            }
        }
        self.refresh_inspector();
        self.stage.queue_draw();
    }

    // ---------------------------------------------------------- Eigenschaften
    pub fn refresh_inspector(self: &Rc<Self>) {
        super::inspector::build(self);
    }

    // -------------------------------------------------------- Geräte, Profile
    fn refresh_profile_list(self: &Rc<Self>) {
        while let Some(c) = self.prof_list.first_child() {
            self.prof_list.remove(&c);
        }
        self.prof_list.append(&label(&tr!("Profiles").to_uppercase(), &["section"]));
        let newb = gtk::Button::new();
        newb.add_css_class("title-drop");
        let nrow = gtk::Box::new(Orientation::Horizontal, 10);
        nrow.add_css_class("menu-row");
        nrow.add_css_class("accent");
        nrow.append(&gtk::Image::from_icon_name("list-add-symbolic"));
        nrow.append(&label(&tr!("New profile"), &[]));
        newb.set_child(Some(&nrow));
        let ed = self.clone();
        newb.connect_clicked(move |b| {
            if let Some(p) = b.ancestor(gtk::Popover::static_type()) {
                p.downcast::<gtk::Popover>().unwrap().popdown();
            }
            super::profiles_ui::new_profile(&ed);
        });
        self.prof_list.append(&newb);
        let (profiles, cur) = {
            let st = self.state.borrow();
            let store = st.store.lock().unwrap();
            let mut v: Vec<(String, String, String, usize)> = store.profiles.values()
                .map(|p| (p.id.clone(), p.name.clone(), p.model_name(), p.page_ids.len())).collect();
            v.sort_by_key(|p| p.1.to_lowercase());
            (v, st.profile.clone())
        };
        for (id, name, model, pages) in profiles {
            let b = gtk::Button::new();
            b.add_css_class("title-drop");
            let row = gtk::Box::new(Orientation::Vertical, 0);
            row.add_css_class("menu-row");
            if Some(&id) == cur.as_ref() {
                row.add_css_class("current");
            }
            row.append(&label(&name, &[]));
            row.append(&label(&format!("{model} · {}", freestream_core::i18n::ngettext("{n} page", "{n} pages", pages as i64).replace("{n}", &pages.to_string())), &["faint"]));
            b.set_child(Some(&row));
            let rc = gtk::GestureClick::new();
            rc.set_button(3);
            let (ed, id2, bb) = (self.clone(), id.clone(), b.clone());
            rc.connect_pressed(move |_, _, _, _| super::profiles_ui::context_menu(&ed, bb.upcast_ref(), &id2));
            b.add_controller(rc);
            let ed = self.clone();
            b.connect_clicked(move |b| {
                if let Some(p) = b.ancestor(gtk::Popover::static_type()) {
                    p.downcast::<gtk::Popover>().unwrap().popdown();
                }
                ed.open_profile(Some(&id));
            });
            self.prof_list.append(&b);
        }
        let imp = gtk::Button::with_label(&tr!("Import"));
        imp.add_css_class("btn");
        imp.set_margin_top(8);
        let ed = self.clone();
        imp.connect_clicked(move |b| {
            if let Some(p) = b.ancestor(gtk::Popover::static_type()) {
                p.downcast::<gtk::Popover>().unwrap().popdown();
            }
            ed.import_dialog();
        });
        self.prof_list.append(&imp);
    }

    fn refresh_device_list(self: &Rc<Self>) {
        while let Some(c) = self.dev_list.first_child() {
            self.dev_list.remove(&c);
        }
        self.dev_list.append(&label(&tr!("Devices").to_uppercase(), &["section"]));
        let devs: Vec<(String, String, Option<String>)> = {
            let st = self.state.borrow();
            st.daemon.as_ref().map(|d| {
                d.lock().unwrap().controllers.lock().unwrap().iter().map(|(s, c)| {
                    let c = c.lock().unwrap();
                    (s.clone(), model_name(&c.dev.model()), c.profile.as_ref().map(|p| p.id.clone()))
                }).collect()
            }).unwrap_or_default()
        };
        if devs.is_empty() {
            self.dev_list.append(&label(&tr!("No device connected"), &["muted"]));
        }
        let cur = self.state.borrow().serial.clone();
        for (serial, name, pid) in devs {
            let b = gtk::Button::new();
            b.add_css_class("title-drop");
            let row = gtk::Box::new(Orientation::Vertical, 0);
            row.add_css_class("menu-row");
            if Some(&serial) == cur.as_ref() {
                row.add_css_class("current");
            }
            row.append(&label(&name, &[]));
            row.append(&label(&serial, &["faint"]));
            b.set_child(Some(&row));
            let ed = self.clone();
            b.connect_clicked(move |b| {
                if let Some(p) = b.ancestor(gtk::Popover::static_type()) {
                    p.downcast::<gtk::Popover>().unwrap().popdown();
                }
                ed.state.borrow_mut().serial = Some(serial.clone());
                ed.open_profile(pid.as_deref());
            });
            self.dev_list.append(&b);
        }
    }

    fn refresh_devices(self: &Rc<Self>) {
        let (changed, serial_gone) = {
            let st = self.state.borrow();
            let changed = st.store.lock().unwrap().changed_on_disk();
            let gone = match (&st.serial, &st.daemon) {
                (Some(s), Some(d)) => !d.lock().unwrap().controllers.lock().unwrap().contains_key(s),
                _ => false,
            };
            (changed, gone)
        };
        if serial_gone {
            self.state.borrow_mut().serial = None;
        }
        // erstes Gerät angesteckt, während das Fenster offen ist
        let new_dev = {
            let st = self.state.borrow();
            if st.serial.is_none() {
                st.daemon.as_ref().and_then(|d| d.lock().unwrap().controllers.lock().unwrap().keys().next().cloned())
            } else {
                None
            }
        };
        if let Some(s) = new_dev {
            self.state.borrow_mut().serial = Some(s);
        }
        if changed {
            self.state.borrow().store.lock().unwrap().reload();
            let pid = self.state.borrow().profile.clone();
            self.open_profile(pid.as_deref());
        } else {
            self.refresh_header();
            self.refresh_device_list();
        }
    }

    fn import_dialog(self: &Rc<Self>) {
        let dlg = gtk::FileChooserNative::new(Some(&tr!("Import profiles")), Some(&self.win), gtk::FileChooserAction::Open,
                                              Some(&tr!("Import")), Some(&tr!("Cancel")));
        let ed = self.clone();
        dlg.connect_response(move |d, resp| {
            if resp == gtk::ResponseType::Accept {
                if let Some(path) = d.file().and_then(|f| f.path()) {
                    let res = freestream_core::profiles::import_path(&path, None, None, true);
                    match res {
                        Ok(ids) => {
                            ed.state.borrow().store.lock().unwrap().reload();
                            ed.state.borrow_mut().status = freestream_core::i18n::ngettext("{n} profile imported", "{n} profiles imported", ids.len() as i64).replace("{n}", &ids.len().to_string());
                            ed.open_profile(ids.first().map(String::as_str));
                        }
                        Err(e) => info(&ed.win, &tr!("Import"), &e.to_string()),
                    }
                }
            }
        });
        dlg.show();
    }
}

/// Titel mit ⌄ ohne Rahmen; die Liste klappt darunter auf.
fn title_drop(text: &str, class: &str) -> (gtk::MenuButton, gtk::Label, gtk::Box) {
    let b = gtk::MenuButton::new();
    b.add_css_class("title-drop");
    b.set_halign(Align::Start);
    let row = gtk::Box::new(Orientation::Horizontal, 6);
    let l = label(text, &[class]);
    row.append(&l);
    let chev = gtk::Image::from_icon_name("pan-down-symbolic");
    chev.add_css_class("muted");
    row.append(&chev);
    b.set_child(Some(&row));
    let pop = gtk::Popover::new();
    pop.set_has_arrow(false);
    let list = gtk::Box::new(Orientation::Vertical, 2);
    list.set_size_request(280, -1);
    list.set_margin_start(6);
    list.set_margin_end(6);
    list.set_margin_top(6);
    list.set_margin_bottom(6);
    let sc = gtk::ScrolledWindow::new();
    sc.set_child(Some(&list));
    sc.set_propagate_natural_height(true);
    sc.set_max_content_height(420);
    sc.set_hscrollbar_policy(gtk::PolicyType::Never);
    pop.set_child(Some(&sc));
    b.set_popover(Some(&pop));
    (b, l, list)
}

/// Kurze Meldung.
pub fn info(win: &gtk::ApplicationWindow, title: &str, text: &str) {
    #[allow(deprecated)]
    let d = gtk::MessageDialog::new(Some(win), gtk::DialogFlags::MODAL | gtk::DialogFlags::DESTROY_WITH_PARENT,
                                    gtk::MessageType::Info, gtk::ButtonsType::Ok, text);
    #[allow(deprecated)]
    d.set_title(Some(title));
    #[allow(deprecated)]
    d.connect_response(|d, _| d.close());
    d.present();
}

// ================================================================================== Bearbeiten
impl Editor {
    /// UUID der gezeigten Seite (auch eines geöffneten Ordners).
    pub fn page_uuid(&self) -> Option<String> {
        self.state.borrow().preview.as_ref()?.current_page().map(|p| p.uuid)
    }

    /// Platz einer Taste.
    pub fn slot_of(&self, k: u8) -> Slot {
        let g = self.geometry();
        Slot::Key(k % g.cols.max(1), k / g.cols.max(1))
    }

    /// Änderung am Profil: `f` bekommt das Dokument und die Seite; danach speichern und neu zeichnen.
    pub fn edit(self: &Rc<Self>, f: impl FnOnce(&mut ProfileDoc, &str)) {
        let Some(page) = self.page_uuid() else { return };
        {
            let mut st = self.state.borrow_mut();
            let Some(doc) = st.doc.as_mut() else { return };
            f(doc, &page);
        }
        self.commit();
    }

    /// Gespeichertes Profil überall neu laden: Vorschau, Geräte, Fenster.
    pub fn commit(self: &Rc<Self>) {
        let err = {
            let mut st = self.state.borrow_mut();
            let r = st.doc.as_mut().map(|d| d.save());
            match r {
                Some(Err(e)) => Some(e.to_string()),
                _ => None,
            }
        };
        if let Some(e) = err {
            self.state.borrow_mut().status = e;
        }
        self.reload_everywhere();
        self.refresh();
    }

    /// Aktion aus der Liste auf eine Taste legen (None: gewählte Taste, sonst erste freie).
    pub fn add_action(self: &Rc<Self>, uuid: &str, key: Option<u8>) {
        let g = self.geometry();
        let target = key.or(self.state.borrow().selected).or_else(|| {
            let st = self.state.borrow();
            let pg = st.preview.as_ref()?.current_page()?;
            (0..g.keys() as u8).find(|k| !pg.keypad.contains_key(&(k % g.cols, k / g.cols)))
        });
        let Some(k) = target else {
            self.state.borrow_mut().status = tr!("No free space – select a key to replace it");
            self.refresh_header();
            return;
        };
        let slot = self.slot_of(k);
        self.edit(|doc, page| {
            doc.place(page, slot, uuid);
        });
        self.state.borrow_mut().selected = Some(k);
        self.refresh_inspector();
        self.stage.queue_draw();
    }

    pub fn delete_key(self: &Rc<Self>, k: u8) {
        let slot = self.slot_of(k);
        self.edit(|doc, page| doc.remove(page, slot));
    }

    pub fn move_key(self: &Rc<Self>, from: u8, to: u8) {
        if from == to {
            return;
        }
        let (a, b) = (self.slot_of(from), self.slot_of(to));
        self.edit(|doc, page| doc.move_action(page, a, page, b));
        self.state.borrow_mut().selected = Some(to);
        self.refresh_inspector();
    }

    pub fn copy_key(self: &Rc<Self>, k: u8) {
        let (Some(page), slot) = (self.page_uuid(), self.slot_of(k)) else { return };
        let clip = self.state.borrow().doc.as_ref().and_then(|d| d.copy(&page, slot));
        if clip.is_some() {
            self.state.borrow_mut().clip = clip;
        }
    }

    pub fn paste_key(self: &Rc<Self>, k: u8) {
        let Some(clip) = self.state.borrow_mut().clip.take() else { return };
        let slot = self.slot_of(k);
        self.edit(|doc, page| {
            doc.paste(page, slot, &clip);
        });
        self.state.borrow_mut().clip = Some(clip);
    }

    pub fn undo(self: &Rc<Self>, redo: bool) {
        let done = {
            let mut st = self.state.borrow_mut();
            st.doc.as_mut().map(|d| if redo { d.redo() } else { d.undo() }).unwrap_or(false)
        };
        if done {
            self.commit();
        }
    }

    pub fn add_page(self: &Rc<Self>) {
        let new = {
            let mut st = self.state.borrow_mut();
            let cur = st.preview.as_ref().and_then(|c| c.current_page()).map(|p| p.uuid);
            st.doc.as_mut().map(|d| d.add_page(cur.as_deref()))
        };
        if new.is_none() {
            return;
        }
        self.commit();
        let n = self.state.borrow().preview.as_ref().map(|c| c.page_count()).unwrap_or(0);
        let idx = {
            let st = self.state.borrow();
            let ids = st.doc.as_ref().map(|d| d.page_ids()).unwrap_or_default();
            ids.iter().position(|u| Some(u.to_lowercase()) == new.as_ref().map(|n| n.to_lowercase())).unwrap_or(n.saturating_sub(1))
        };
        self.goto_page(idx);
    }

    pub fn delete_page(self: &Rc<Self>, i: usize) {
        let uuid = self.state.borrow().doc.as_ref().and_then(|d| d.page_ids().get(i).cloned());
        let Some(uuid) = uuid else { return };
        let res = self.state.borrow_mut().doc.as_mut().map(|d| d.remove_page(&uuid));
        if let Some(Err(e)) = res {
            self.state.borrow_mut().status = e.to_string();
            self.refresh_header();
            return;
        }
        self.commit();
        let n = self.state.borrow().preview.as_ref().map(|c| c.page_count()).unwrap_or(0);
        self.goto_page(i.min(n.saturating_sub(1)));
    }

    pub fn move_page(self: &Rc<Self>, i: usize, delta: i64) {
        let uuid = self.state.borrow().doc.as_ref().and_then(|d| d.page_ids().get(i).cloned());
        let Some(uuid) = uuid else { return };
        if let Some(d) = self.state.borrow_mut().doc.as_mut() {
            d.move_page(&uuid, delta);
        }
        self.commit();
        let n = self.state.borrow().preview.as_ref().map(|c| c.page_count()).unwrap_or(1) as i64;
        self.goto_page((i as i64 + delta).clamp(0, n - 1) as usize);
    }

    /// Ziehen auf der Bühne (Tasten verschieben) und Ablegen aus der Aktionsliste.
    fn connect_dnd(self: &Rc<Self>) {
        let drop = gtk::DropTarget::new(glib::Type::STRING, gtk::gdk::DragAction::COPY | gtk::gdk::DragAction::MOVE);
        let ed = self.clone();
        drop.connect_drop(move |_, v, x, y| {
            let Ok(text) = v.get::<String>() else { return false };
            let Some(k) = ed.key_at(x, y) else { return false };
            if let Some(uuid) = text.strip_prefix(super::library::DRAG_PREFIX) {
                ed.add_action(uuid, Some(k));
                return true;
            }
            if let Some(from) = text.strip_prefix(KEY_DRAG).and_then(|s| s.parse::<u8>().ok()) {
                ed.move_key(from, k);
                return true;
            }
            false
        });
        self.stage.add_controller(drop);

        let drag = gtk::DragSource::new();
        drag.set_actions(gtk::gdk::DragAction::MOVE);
        let ed = self.clone();
        drag.connect_prepare(move |src, x, y| {
            let k = ed.key_at(x, y)?;
            let g = ed.geometry();
            let st = ed.state.borrow();
            st.preview.as_ref()?.current_page()?.keypad.get(&(k % g.cols, k / g.cols))?;
            if let Some(img) = st.keys.get(&k) {
                let small = image::imageops::resize(img, 72, 72, image::imageops::FilterType::Triangle);
                src.set_icon(Some(&super::util::texture(&small)), 36, 36);
            }
            Some(gtk::gdk::ContentProvider::for_value(&format!("{KEY_DRAG}{k}").to_value()))
        });
        self.stage.add_controller(drag);

        // Rechtsklick: Kopieren, Einfügen, Löschen
        let menu = gtk::GestureClick::new();
        menu.set_button(3);
        let ed = self.clone();
        menu.connect_pressed(move |_, _, x, y| {
            if let Some(k) = ed.key_at(x, y) {
                ed.state.borrow_mut().selected = Some(k);
                ed.refresh_inspector();
                ed.stage.queue_draw();
                ed.key_menu(k, x, y);
            }
        });
        self.stage.add_controller(menu);

        // Tastenkürzel
        let keys = gtk::EventControllerKey::new();
        let ed = self.clone();
        keys.connect_key_pressed(move |_, key, _, mods| {
            use gtk::gdk::{Key, ModifierType};
            let ctrl = mods.contains(ModifierType::CONTROL_MASK);
            let shift = mods.contains(ModifierType::SHIFT_MASK);
            // Eingabefelder bekommen ihre Tasten selbst
            if gtk::prelude::GtkWindowExt::focus(&ed.win).map(|w| w.is::<gtk::Text>() || w.is::<gtk::TextView>()).unwrap_or(false) {
                return glib::Propagation::Proceed;
            }
            let sel = ed.state.borrow().selected;
            match key.to_lower() {
                Key::z if ctrl => ed.undo(shift),
                Key::y if ctrl => ed.undo(true),
                Key::c if ctrl => {
                    if let Some(k) = sel {
                        ed.copy_key(k)
                    }
                }
                Key::x if ctrl => {
                    if let Some(k) = sel {
                        ed.copy_key(k);
                        ed.delete_key(k);
                    }
                }
                Key::v if ctrl => {
                    if let Some(k) = sel {
                        ed.paste_key(k)
                    }
                }
                Key::Delete | Key::BackSpace => {
                    if let Some(k) = sel {
                        ed.delete_key(k)
                    }
                }
                _ => return glib::Propagation::Proceed,
            }
            glib::Propagation::Stop
        });
        self.win.add_controller(keys);
    }

    fn key_menu(self: &Rc<Self>, k: u8, x: f64, y: f64) {
        let pop = gtk::Popover::new();
        pop.set_has_arrow(false);
        pop.set_parent(&self.stage);
        pop.set_pointing_to(Some(&gtk::gdk::Rectangle::new(x as i32, y as i32, 1, 1)));
        let list = gtk::Box::new(Orientation::Vertical, 2);
        list.set_margin_start(4);
        list.set_margin_end(4);
        list.set_margin_top(4);
        list.set_margin_bottom(4);
        let occupied = {
            let g = self.geometry();
            let st = self.state.borrow();
            st.preview.as_ref().and_then(|c| c.current_page()).map(|p| p.keypad.contains_key(&(k % g.cols, k / g.cols))).unwrap_or(false)
        };
        let has_clip = self.state.borrow().clip.is_some();
        let items: Vec<(String, &str, bool, u8)> = vec![
            (tr!("Copy"), "edit-copy-symbolic", occupied, 0),
            (tr!("Paste"), "edit-paste-symbolic", has_clip, 1),
            (tr!("Delete"), "user-trash-symbolic", occupied, 2),
        ];
        for (text, icon, enabled, what) in items {
            let b = gtk::Button::new();
            b.add_css_class("title-drop");
            let row = gtk::Box::new(Orientation::Horizontal, 10);
            row.add_css_class("menu-row");
            row.append(&gtk::Image::from_icon_name(icon));
            row.append(&label(&text, if what == 2 { &["danger"] } else { &[] }));
            b.set_child(Some(&row));
            b.set_sensitive(enabled);
            let (ed, p) = (self.clone(), pop.clone());
            b.connect_clicked(move |_| {
                p.popdown();
                match what {
                    0 => ed.copy_key(k),
                    1 => ed.paste_key(k),
                    _ => ed.delete_key(k),
                }
            });
            list.append(&b);
        }
        pop.set_child(Some(&list));
        pop.connect_closed(|p| p.unparent());
        pop.popup();
    }
}

impl Editor {
    /// Änderung ohne die Eigenschaften neu aufzubauen (Tippen in Feldern): kurz warten, dann speichern
    /// und Vorschau, Gerät und Bild der Taste nachziehen.
    pub fn edit_soon(self: &Rc<Self>, f: impl FnOnce(&mut ProfileDoc, &str)) {
        let Some(page) = self.page_uuid() else { return };
        {
            let mut st = self.state.borrow_mut();
            let Some(doc) = st.doc.as_mut() else { return };
            f(doc, &page);
            if let Some(job) = st.save_job.take() {
                job.remove();
            }
        }
        let ed = self.clone();
        let id = glib::timeout_add_local_once(std::time::Duration::from_millis(250), move || {
            ed.state.borrow_mut().save_job = None;
            ed.commit_quiet();
        });
        self.state.borrow_mut().save_job = Some(id);
    }

    /// Speichern und neu zeichnen, die Eigenschaften bleiben stehen.
    pub fn commit_quiet(self: &Rc<Self>) {
        if let Some(job) = self.state.borrow_mut().save_job.take() {
            job.remove();
        }
        let _ = self.state.borrow_mut().doc.as_mut().map(|d| d.save());
        self.reload_everywhere();
        self.render_keys();
        self.refresh_header();
        self.stage.queue_draw();
        let k = self.state.borrow().selected;
        if let (Some(k), Some(pic)) = (k, self.insp_pic.borrow().as_ref()) {
            if let Some(img) = self.state.borrow().keys.get(&k) {
                pic.set_paintable(Some(&super::util::texture(img)));
            }
        }
    }

    fn reload_everywhere(&self) {
        let st = self.state.borrow();
        match &st.daemon {
            Some(d) => {
                let d = d.lock().unwrap();
                d.store.lock().unwrap().reload();
                for c in d.controllers.lock().unwrap().values() {
                    let mut c = c.lock().unwrap();
                    if c.profile.as_ref().map(|p| Some(&p.id) == st.profile.as_ref()).unwrap_or(false) {
                        c.reload();
                    }
                }
            }
            None => st.store.lock().unwrap().reload(),
        }
        drop(st);
        if let Some(c) = self.state.borrow_mut().preview.as_mut() {
            c.reload();
        }
    }

    /// Taste auf eine andere Seite verschieben (auf die Seitenzahl gezogen): erster freier Platz dort.
    pub fn move_key_to_page(self: &Rc<Self>, from: u8, page_idx: usize) {
        let g = self.geometry();
        let src_slot = self.slot_of(from);
        let (src, dst) = {
            let st = self.state.borrow();
            let Some(doc) = st.doc.as_ref() else { return };
            (self.page_uuid(), doc.page_ids().get(page_idx).cloned())
        };
        let (Some(src), Some(dst)) = (src, dst) else { return };
        if src.eq_ignore_ascii_case(&dst) {
            return;
        }
        let free = {
            let st = self.state.borrow();
            let doc = st.doc.as_ref().unwrap();
            (0..g.keys() as u8).map(|k| Slot::Key(k % g.cols, k / g.cols)).find(|s| doc.get(&dst, *s).is_none())
        };
        let Some(free) = free else {
            self.state.borrow_mut().status = tr!("There is no free space on the target page");
            self.refresh_header();
            return;
        };
        if let Some(d) = self.state.borrow_mut().doc.as_mut() {
            d.move_action(&src, src_slot, &dst, free);
        }
        self.state.borrow_mut().selected = None;
        self.commit();
    }

    fn page_menu(self: &Rc<Self>, anchor: &gtk::Button, i: usize, n: usize) {
        let pop = gtk::Popover::new();
        pop.set_has_arrow(false);
        pop.set_parent(anchor);
        let list = gtk::Box::new(Orientation::Vertical, 2);
        for m in [&list] {
            m.set_margin_start(4);
            m.set_margin_end(4);
            m.set_margin_top(4);
            m.set_margin_bottom(4);
        }
        let items: Vec<(String, &str, bool, u8)> = vec![
            (tr!("Move left"), "go-previous-symbolic", i > 0, 0),
            (tr!("Move right"), "go-next-symbolic", i + 1 < n, 1),
            (tr!("Delete page"), "user-trash-symbolic", n > 1, 2),
        ];
        for (text, icon, enabled, what) in items {
            let b = gtk::Button::new();
            b.add_css_class("title-drop");
            let row = gtk::Box::new(Orientation::Horizontal, 10);
            row.add_css_class("menu-row");
            row.append(&gtk::Image::from_icon_name(icon));
            row.append(&label(&text, if what == 2 { &["danger"] } else { &[] }));
            b.set_child(Some(&row));
            b.set_sensitive(enabled);
            let (ed, p) = (self.clone(), pop.clone());
            b.connect_clicked(move |_| {
                p.popdown();
                match what {
                    0 => ed.move_page(i, -1),
                    1 => ed.move_page(i, 1),
                    _ => ed.delete_page(i),
                }
            });
            list.append(&b);
        }
        pop.set_child(Some(&list));
        pop.connect_closed(|p| p.unparent());
        pop.popup();
    }
}

/// Text im Drag & Drop für eine Taste der Bühne.
const KEY_DRAG: &str = "freedeck-key:";
