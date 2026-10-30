//! Einstellungen von FreeDeck (Zahnrad oben rechts) – wie `freestream/settingsui.py`: Reiter Allgemein,
//! Geräte, Profile, Plugins, Verbindungen und Stil. Die Reiter werden beim ersten Öffnen gebaut
//! (`settings_*.rs`); alles landet in config.json (dieselben Schlüssel wie in der Python-Version).
//!
//! Netzwerk und Schlüsselbund laufen in Threads ([`run_bg`]), das Fenster bleibt bedienbar.

use gtk4 as gtk;
use gtk::glib;
use gtk::prelude::*;
use gtk::{Align, Orientation};

use freestream_core::config::{save_keys, Config};
use freestream_core::{desktop, i18n, tr};
use serde_json::Value;
use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;
use std::sync::Mutex;
use std::time::Duration;

use super::editor::State;
use super::util::label;

/// Reiter (Schlüssel wie in der Python-Version).
const TABS: [&str; 6] = ["general", "devices", "profiles", "plugins", "connections", "style"];

/// Gemeinsames der Reiter: Fenster und Zustand des Editors.
pub struct Ctx {
    pub win: gtk::Window,
    pub state: Rc<RefCell<State>>,
}

impl Ctx {
    /// config.json ändern (frisch gelesen, gleich gespeichert) und den laufenden Geräten Bescheid geben.
    pub fn edit(&self, f: impl FnOnce(&mut Config)) {
        cfg_edit(f);
        self.sync();
    }

    /// Steuerungen der Geräte lesen config.json neu (sonst überschreiben sie Änderungen beim nächsten Speichern).
    pub fn sync(&self) {
        for c in self.controllers() {
            if let Ok(mut c) = c.try_lock() {
                c.cfg.reload_if_changed();
            }
        }
    }

    /// Steuerungen der angeschlossenen Geräte (Seriennummer, Steuerung).
    pub fn controllers(&self) -> Vec<std::sync::Arc<Mutex<freestream_deck::engine::Controller>>> {
        let st = self.state.borrow();
        st.daemon.as_ref()
            .map(|d| d.lock().unwrap().controllers.lock().unwrap().values().cloned().collect())
            .unwrap_or_default()
    }
}

// ------------------------------------------------------------------------------------- config.json
static CFG_LOCK: Mutex<()> = Mutex::new(());

/// config.json frisch lesen, ändern, speichern (auch aus Threads). Eine unlesbare Datei bleibt unverändert.
pub fn cfg_edit(f: impl FnOnce(&mut Config)) -> bool {
    let _g = CFG_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    if freestream_core::config::read_file().is_err() {
        log::warn!("config.json nicht lesbar – Einstellung nicht gespeichert");
        return false;
    }
    let mut cfg = Config::load();
    f(&mut cfg);
    match cfg.save() {
        Ok(()) => true,
        Err(e) => {
            log::warn!("config.json: {e}");
            false
        }
    }
}

/// Aktuelle Einstellungen.
pub fn cfg_read() -> Config {
    Config::load()
}

/// Abschnitt (Objekt) lesen, z. B. `obs`.
pub fn cfg_section(cfg: &Config, key: &str) -> serde_json::Map<String, Value> {
    cfg.get(key).and_then(Value::as_object).cloned().unwrap_or_default()
}

/// Feld eines Abschnitts setzen bzw. (leer) entfernen; ein leerer Abschnitt verschwindet (wie Python).
pub fn set_section_field(cfg: &mut Config, section: &str, field: &str, value: Option<Value>) {
    let sec = cfg.section(section);
    match value {
        Some(v) => {
            sec.insert(field.to_string(), v);
        }
        None => {
            sec.remove(field);
        }
    }
    if sec.is_empty() {
        cfg.data.remove(section);
    }
}

/// Zugangsdaten speichern wie `Config._file_data` der Python-Version: im Schlüsselbund, wenn es einen gibt
/// (config.json vermerkt das unter `in_keyring`), sonst in der Datei. Blockiert – nur in Threads aufrufen.
pub fn save_secret(section: &str, field: &str, value: &str) {
    use freestream_core::secrets;
    let key = format!("{section}.{field}");
    let value = value.trim().to_string();
    let mut in_ring = false;
    if value.is_empty() {
        let listed = secrets::listed(&cfg_read().data, section, field);
        if listed {
            secrets::clear(&key);
        }
    } else if secrets::available() && secrets::store(&key, &value) {
        in_ring = true;
    }
    cfg_edit(|cfg| {
        let v = (!in_ring && !value.is_empty()).then(|| Value::from(value.clone()));
        set_section_field(cfg, section, field, v);
        let mut list: Vec<Value> = cfg.get("in_keyring").and_then(Value::as_array).cloned().unwrap_or_default();
        list.retain(|v| v.as_str() != Some(key.as_str()));
        if in_ring {
            list.push(Value::from(key.clone()));
        }
        if list.is_empty() {
            cfg.data.remove("in_keyring");
        } else {
            cfg.set("in_keyring", Value::Array(list));
        }
    });
}

// ------------------------------------------------------------------------------------- Hintergrund
/// `work` in einem Thread ausführen, `done` danach im GTK-Thread mit dem Ergebnis.
pub fn run_bg<T: Send + 'static>(work: impl FnOnce() -> T + Send + 'static, done: impl FnOnce(T) + 'static) {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let _ = tx.send(work());
    });
    let mut done = Some(done);
    glib::timeout_add_local(Duration::from_millis(60), move || match rx.try_recv() {
        Ok(v) => {
            if let Some(d) = done.take() {
                d(v);
            }
            glib::ControlFlow::Break
        }
        Err(std::sync::mpsc::TryRecvError::Empty) => glib::ControlFlow::Continue,
        Err(_) => glib::ControlFlow::Break,
    });
}

/// Gesammelt ausführen (z. B. Speichern beim Tippen oder Ziehen am Regler).
#[derive(Clone, Default)]
pub struct Debounce(Rc<RefCell<Option<glib::SourceId>>>);

impl Debounce {
    pub fn run(&self, ms: u64, f: impl FnOnce() + 'static) {
        if let Some(id) = self.0.borrow_mut().take() {
            id.remove();
        }
        let me = self.0.clone();
        let id = glib::timeout_add_local_once(Duration::from_millis(ms), move || {
            me.borrow_mut().take();
            f();
        });
        *self.0.borrow_mut() = Some(id);
    }
}

// ------------------------------------------------------------------------------------- Bausteine
pub fn margins(w: &impl IsA<gtk::Widget>, top: i32, end: i32, bottom: i32, start: i32) {
    w.set_margin_top(top);
    w.set_margin_end(end);
    w.set_margin_bottom(bottom);
    w.set_margin_start(start);
}

/// Karte (dunkle Fläche mit runden Ecken).
pub fn card() -> gtk::Box {
    let b = gtk::Box::new(Orientation::Vertical, 0);
    b.add_css_class("set-card");
    b
}

/// Überschrift über einer Karte (Allgemein).
pub fn card_title(text: &str) -> gtk::Label {
    let l = label(text, &["set-card-title"]);
    l.set_margin_bottom(8);
    l
}

/// Kleine graue Überschrift in Großbuchstaben (in Karten).
pub fn section(text: &str) -> gtk::Label {
    label(&text.to_uppercase(), &["section"])
}

/// Grauer Hinweis mit Umbruch.
pub fn hint(text: &str) -> gtk::Label {
    let l = label(text, &["muted", "small"]);
    l.set_wrap(true);
    l.set_wrap_mode(gtk::pango::WrapMode::WordChar);
    l.set_max_width_chars(70);
    l
}

/// Statuszeile für Verbindungstests.
pub fn status_label() -> gtk::Label {
    let l = label("", &["muted"]);
    l.set_wrap(true);
    l.set_max_width_chars(60);
    l.set_selectable(true);
    l
}

/// Status setzen: kind = "ok", "error" oder "muted".
pub fn set_status(l: &gtk::Label, text: &str, kind: &str) {
    l.set_text(text);
    for c in ["ok", "danger", "muted"] {
        l.remove_css_class(c);
    }
    l.add_css_class(match kind {
        "ok" => "ok",
        "error" => "danger",
        _ => "muted",
    });
}

/// Raster für Formulare: Beschriftung rechtsbündig links, Feld rechts.
pub fn form() -> gtk::Grid {
    let g = gtk::Grid::new();
    g.set_row_spacing(10);
    g.set_column_spacing(14);
    g
}

/// Zeile im Formular; gibt die Beschriftung zurück.
pub fn form_row(g: &gtk::Grid, r: i32, text: &str, w: &impl IsA<gtk::Widget>) -> gtk::Label {
    let l = label(text, &["field-label"]);
    l.set_xalign(1.0);
    l.set_width_chars(16);
    l.set_valign(Align::Center);
    g.attach(&l, 0, r, 1, 1);
    w.set_halign(Align::Start);
    w.set_valign(Align::Center);
    g.attach(w, 1, r, 1, 1);
    l
}

/// Hinweis unter einem Feld (eingerückt wie die Felder).
pub fn form_hint(g: &gtk::Grid, r: i32, text: &str) {
    let h = hint(text);
    h.set_max_width_chars(48);
    h.add_css_class("faint");
    h.set_margin_top(-6);
    g.attach(&h, 1, r, 1, 1);
}

/// Knopf im Stil der Fenster (`btn`), optional mit Symbol.
pub fn button(text: &str, icon: Option<&str>, kind: &str) -> gtk::Button {
    let b = gtk::Button::new();
    let row = gtk::Box::new(Orientation::Horizontal, 6);
    if let Some(i) = icon {
        row.append(&gtk::Image::from_icon_name(i));
    }
    if !text.is_empty() {
        row.append(&gtk::Label::new(Some(text)));
    }
    b.set_child(Some(&row));
    b.add_css_class("btn");
    if !kind.is_empty() {
        b.add_css_class(kind);
    }
    b.set_halign(Align::Start);
    b
}

/// Auswahlliste aus Texten.
pub fn dropdown(items: &[String]) -> gtk::DropDown {
    let d = gtk::DropDown::from_strings(&items.iter().map(String::as_str).collect::<Vec<_>>());
    d.set_size_request(230, -1);
    d
}

/// Schalter mit Text (und grauem Hinweis darunter) – wie `_switch_row`.
pub fn switch_row(text: &str, hint_text: &str, active: bool) -> (gtk::Box, gtk::Switch) {
    let row = gtk::Box::new(Orientation::Horizontal, 12);
    let sw = gtk::Switch::new();
    sw.set_active(active);
    sw.set_valign(Align::Start);
    row.append(&sw);
    let col = gtk::Box::new(Orientation::Vertical, 2);
    col.set_hexpand(true);
    let t = label(text, &[]);
    t.set_wrap(true);
    t.set_margin_top(3);
    col.append(&t);
    if !hint_text.is_empty() {
        col.append(&hint(hint_text));
    }
    row.append(&col);
    (row, sw)
}

/// Eintrag der Listen links (Gerät, Dienst): farbige Kachel, Titel, Unterzeile mit Punkt.
pub struct InfoCard {
    pub button: gtk::Button,
    pub title: gtk::Label,
    pub sub: gtk::Label,
    pub dot: gtk::Box,
}

impl InfoCard {
    pub fn new(title: &str, sub: &str, glyph: &str, color: &str) -> Self {
        let button = gtk::Button::new();
        button.add_css_class("info-card");
        let row = gtk::Box::new(Orientation::Horizontal, 10);
        row.append(&super::util::glyph_tile(glyph, color, 32));
        let col = gtk::Box::new(Orientation::Vertical, 1);
        col.set_valign(Align::Center);
        let t = label(title, &["info-title"]);
        t.set_ellipsize(gtk::pango::EllipsizeMode::End);
        col.append(&t);
        let line = gtk::Box::new(Orientation::Horizontal, 6);
        let dot = gtk::Box::new(Orientation::Horizontal, 0);
        dot.add_css_class("dot");
        dot.set_size_request(7, 7);
        dot.set_valign(Align::Center);
        line.append(&dot);
        let s = label(sub, &["muted", "small"]);
        s.set_ellipsize(gtk::pango::EllipsizeMode::End);
        line.append(&s);
        col.append(&line);
        row.append(&col);
        button.set_child(Some(&row));
        Self { button, title: t, sub: s, dot }
    }

    /// Farbe des Punkts: "ok", "faint", "muted" oder None (kein Punkt).
    pub fn set_dot(&self, kind: Option<&str>) {
        for c in ["dot-ok", "dot-faint", "dot-muted", "dot-danger"] {
            self.dot.remove_css_class(c);
        }
        self.dot.set_visible(kind.is_some());
        if let Some(k) = kind {
            self.dot.add_css_class(&format!("dot-{k}"));
        }
    }

    pub fn set_selected(&self, on: bool) {
        if on {
            self.button.add_css_class("selected");
        } else {
            self.button.remove_css_class("selected");
        }
    }
}

/// Zweispaltige Seite: links eine Karte fester Breite, rechts der Inhalt.
pub fn split(left_width: i32) -> (gtk::Box, gtk::Box, gtk::Box) {
    let page = gtk::Box::new(Orientation::Horizontal, 12);
    let left = card();
    left.set_size_request(left_width, -1);
    left.set_vexpand(true);
    left.set_hexpand(false);
    page.append(&left);
    let right = gtk::Box::new(Orientation::Vertical, 0);
    right.set_hexpand(true);
    right.set_vexpand(true);
    page.append(&right);
    (page, left, right)
}

/// Inhalt eines Containers leeren.
pub fn clear(b: &gtk::Box) {
    while let Some(c) = b.first_child() {
        b.remove(&c);
    }
}

/// Karte rechts, die den Platz füllt, mit Bildlauf.
pub fn panel(right: &gtk::Box) -> gtk::Box {
    clear(right);
    let c = card();
    c.set_vexpand(true);
    let sc = gtk::ScrolledWindow::new();
    sc.set_hscrollbar_policy(gtk::PolicyType::Never);
    sc.set_vexpand(true);
    let body = gtk::Box::new(Orientation::Vertical, 0);
    sc.set_child(Some(&body));
    c.append(&sc);
    right.append(&c);
    body
}

// ------------------------------------------------------------------------------------- Dialoge
fn dialog(parent: &gtk::Window, title: &str) -> (gtk::Window, gtk::Box, gtk::Box) {
    let win = gtk::Window::builder().title(title).transient_for(parent).modal(true).default_width(440).resizable(false).build();
    let outer = gtk::Box::new(Orientation::Vertical, 14);
    outer.add_css_class("main");
    margins(&outer, 18, 20, 16, 20);
    let body = gtk::Box::new(Orientation::Vertical, 8);
    outer.append(&body);
    let buttons = gtk::Box::new(Orientation::Horizontal, 8);
    buttons.set_halign(Align::End);
    outer.append(&buttons);
    win.set_child(Some(&outer));
    (win, body, buttons)
}

fn text_label(text: &str) -> gtk::Label {
    let l = label(text, &[]);
    l.set_wrap(true);
    l.set_max_width_chars(55);
    l
}

/// Hinweis mit „OK“.
pub fn message(parent: &gtk::Window, title: &str, text: &str) {
    let (win, body, buttons) = dialog(parent, title);
    body.append(&text_label(text));
    let ok = button(&tr!("OK"), None, "primary");
    buttons.append(&ok);
    let w = win.clone();
    ok.connect_clicked(move |_| w.close());
    win.present();
}

/// Ja/Nein-Frage; `danger` färbt den Ja-Knopf rot.
pub fn confirm(parent: &gtk::Window, title: &str, text: &str, yes: &str, danger: bool, on_yes: impl Fn() + 'static) {
    let (win, body, buttons) = dialog(parent, title);
    body.append(&text_label(text));
    let cancel = button(&tr!("Cancel"), None, "");
    let y = button(yes, None, if danger { "danger-btn" } else { "primary" });
    buttons.append(&cancel);
    buttons.append(&y);
    let w = win.clone();
    cancel.connect_clicked(move |_| w.close());
    let w = win.clone();
    y.connect_clicked(move |_| {
        w.close();
        on_yes();
    });
    win.present();
}

/// Nach einem Text fragen (Name).
pub fn ask_text(parent: &gtk::Window, title: &str, prompt: &str, initial: &str, ok: &str, on_ok: impl Fn(String) + 'static) {
    let (win, body, buttons) = dialog(parent, title);
    body.append(&label(prompt, &["muted"]));
    let e = gtk::Entry::new();
    e.set_text(initial);
    e.set_activates_default(true);
    body.append(&e);
    let cancel = button(&tr!("Cancel"), None, "");
    let okb = button(ok, None, "primary");
    buttons.append(&cancel);
    buttons.append(&okb);
    win.set_default_widget(Some(&okb));
    let w = win.clone();
    cancel.connect_clicked(move |_| w.close());
    let (w, e2) = (win.clone(), e.clone());
    okb.connect_clicked(move |_| {
        let t = e2.text().trim().to_string();
        if !t.is_empty() {
            w.close();
            on_ok(t);
        }
    });
    win.present();
    e.grab_focus();
    e.select_region(0, -1);
}

/// Datei wählen (GTK 4.6: FileChooserNative). `filters`: (Name, Muster).
pub fn choose_file(parent: &gtk::Window, title: &str, filters: &[(String, &[&str])], on_file: impl Fn(std::path::PathBuf) + 'static) {
    let dlg = gtk::FileChooserNative::new(Some(title), Some(parent), gtk::FileChooserAction::Open, Some(&tr!("Open")), Some(&tr!("Cancel")));
    for (name, pats) in filters {
        let f = gtk::FileFilter::new();
        f.set_name(Some(name));
        for p in *pats {
            f.add_pattern(p);
        }
        dlg.add_filter(&f);
    }
    dlg.connect_response(move |d, resp| {
        if resp == gtk::ResponseType::Accept {
            if let Some(p) = d.file().and_then(|f| f.path()) {
                on_file(p);
            }
        }
    });
    dlg.show();
    // FileChooserNative hält sich nicht selbst am Leben
    let keep = dlg.clone();
    dlg.connect_response(move |_, _| {
        let _ = &keep;
    });
}

/// Ordner oder Datei mit dem Standardprogramm öffnen.
pub fn open_uri(path_or_url: &str) {
    freestream_deck::system::run_bg(&["xdg-open", path_or_url]);
}

// ------------------------------------------------------------------------------------- Fenster
thread_local!(static OPEN: RefCell<Option<glib::WeakRef<gtk::Window>>> = const { RefCell::new(None) });

/// Fenster „Einstellungen“ öffnen (ist es schon offen: nach vorne holen).
pub fn open(parent: &gtk::ApplicationWindow, state: &Rc<RefCell<State>>) {
    open_tab(parent, state, None);
}

/// Wie [`open`], gleich mit einem Reiter (Schlüssel aus `TABS`).
pub fn open_tab(parent: &gtk::ApplicationWindow, state: &Rc<RefCell<State>>, tab: Option<&str>) {
    if let Some(w) = OPEN.with(|o| o.borrow().as_ref().and_then(|w| w.upgrade())) {
        w.present();
        return;
    }
    let win = gtk::Window::builder().title(tr!("Settings")).transient_for(parent).modal(true)
        .default_width(880).default_height(680).build();
    win.set_size_request(760, 520);
    OPEN.with(|o| *o.borrow_mut() = Some(win.downgrade()));
    let ctx = Rc::new(Ctx { win: win.clone(), state: state.clone() });
    let outer = gtk::Box::new(Orientation::Vertical, 0);
    outer.add_css_class("main");
    win.set_child(Some(&outer));

    // Reiter oben (wie Segmented in der Python-Version)
    let names = [tr!("General"), tr!("Devices"), tr!("Profiles"), tr!("Plugins"), tr!("Connections"), tr!("Style")];
    let bar = gtk::Box::new(Orientation::Horizontal, 0);
    bar.add_css_class("tabs");
    bar.set_halign(Align::Start);
    margins(&bar, 16, 22, 14, 22);
    outer.append(&bar);
    let stack = gtk::Stack::new();
    stack.set_vexpand(true);
    stack.set_transition_type(gtk::StackTransitionType::None);
    margins(&stack, 0, 22, 0, 22);
    outer.append(&stack);
    let built: Rc<RefCell<HashMap<&'static str, bool>>> = Rc::default();
    let mut first: Option<gtk::ToggleButton> = None;
    let mut buttons = Vec::new();
    for (key, name) in TABS.iter().zip(names.iter()) {
        let b = gtk::ToggleButton::with_label(name);
        b.add_css_class("tab");
        b.add_css_class("set-tab");
        if let Some(f) = &first {
            b.set_group(Some(f));
        } else {
            first = Some(b.clone());
        }
        bar.append(&b);
        let (ctx2, stack2, built2) = (ctx.clone(), stack.clone(), built.clone());
        let key: &'static str = key;
        b.connect_toggled(move |b| {
            if b.is_active() {
                show_tab(&ctx2, &stack2, &built2, key);
            }
        });
        buttons.push((key, b));
    }

    // Knopf unten
    let foot = gtk::Box::new(Orientation::Horizontal, 0);
    margins(&foot, 12, 22, 16, 22);
    let close = button(&tr!("Close"), None, "primary");
    close.set_hexpand(true);
    close.set_halign(Align::End);
    let w2 = win.clone();
    close.connect_clicked(move |_| w2.close());
    foot.append(&close);
    outer.append(&foot);

    let want = tab.filter(|t| TABS.contains(t)).unwrap_or("general");
    for (key, b) in &buttons {
        if *key == want {
            b.set_active(true);
        }
    }
    win.present();
}

fn show_tab(ctx: &Rc<Ctx>, stack: &gtk::Stack, built: &Rc<RefCell<HashMap<&'static str, bool>>>, key: &'static str) {
    if !built.borrow().contains_key(key) {
        built.borrow_mut().insert(key, true);
        let page: gtk::Widget = match key {
            "devices" => super::settings_devices::build(ctx),
            "profiles" => super::settings_profiles::build(ctx),
            "plugins" => super::settings_plugins::build(ctx),
            "connections" => super::settings_connections::build(ctx),
            "style" => super::settings_style::build(ctx),
            _ => general(ctx),
        };
        stack.add_named(&page, Some(key));
    }
    stack.set_visible_child_name(key);
}

// ------------------------------------------------------------------------------------- Allgemein
fn general(_ctx: &Rc<Ctx>) -> gtk::Widget {
    let sc = gtk::ScrolledWindow::new();
    sc.set_hscrollbar_policy(gtk::PolicyType::Never);
    let b = gtk::Box::new(Orientation::Vertical, 0);
    b.set_margin_end(4);
    sc.set_child(Some(&b));
    let cfg = freestream_core::config::read_file().unwrap_or_default();

    // Programm
    b.append(&card_title(&tr!("Program")));
    let c = card();
    c.set_spacing(12);
    c.set_margin_bottom(16);
    c.append(&label(&tr!("Installed version: {version}", version = freestream_core::VERSION), &[]));
    let (row, updates) = switch_row(&tr!("Check for updates every time {app} starts", app = "FreeDeck"), "",
                                    cfg.get("check_updates") != Some(&Value::Bool(false)));
    updates.connect_active_notify(|s| {
        save_keys(&[("check_updates", Value::from(s.is_active()))]);
    });
    c.append(&row);
    let (row, beta) = switch_row(&tr!("Include beta versions"),
                                 &tr!("Beta versions bring new features earlier but may contain more bugs."),
                                 cfg.get("update_channel").and_then(Value::as_str) == Some("beta"));
    beta.connect_active_notify(|s| {
        save_keys(&[("update_channel", Value::from(if s.is_active() { "beta" } else { "stable" }))]);
    });
    c.append(&row);
    let (row, auto) = switch_row(&tr!("Start automatically when you log in"),
                                 &tr!("{app} then runs in the background and is ready as soon as your desktop appears.", app = "FreeDeck"),
                                 desktop::autostart_enabled("freedeck"));
    auto.connect_active_notify(|s| {
        if let Err(e) = desktop::set_autostart("freedeck", s.is_active()) {
            log::warn!("Autostart: {e}");
            if let Some(w) = s.root().and_then(|r| r.downcast::<gtk::Window>().ok()) {
                message(&w, &tr!("Settings"), &e.to_string());
            }
        }
    });
    c.append(&row);
    b.append(&c);

    // Aussehen und Sprache
    b.append(&card_title(&tr!("Appearance")));
    let c = card();
    c.set_spacing(8);
    c.set_margin_bottom(16);
    let modes = [("light", tr!("Light")), ("dark", tr!("Dark")), ("auto", tr!("Auto"))];
    let cur = cfg.get("appearance").and_then(Value::as_str).unwrap_or("auto").to_string();
    let seg = gtk::Box::new(Orientation::Horizontal, 0);
    seg.add_css_class("tabs");
    seg.set_homogeneous(true);
    let mut group: Option<gtk::ToggleButton> = None;
    for (key, name) in modes.iter() {
        let t = gtk::ToggleButton::with_label(name);
        t.add_css_class("tab");
        if let Some(g) = &group {
            t.set_group(Some(g));
        } else {
            group = Some(t.clone());
        }
        t.set_active(*key == cur);
        let key = key.to_string();
        t.connect_toggled(move |t| {
            if t.is_active() {
                save_keys(&[("appearance", Value::from(key.as_str()))]);
                if let Some(s) = gtk::Settings::default() {
                    s.set_gtk_application_prefer_dark_theme(key != "light");
                }
            }
        });
        seg.append(&t);
    }
    c.append(&seg);
    c.append(&hint(&tr!("Auto follows the light or dark setting of your desktop.")));
    let l = label(&tr!("Language"), &[]);
    l.set_margin_top(10);
    c.append(&l);
    let langs: Vec<&str> = i18n::available();
    let mut names = vec![tr!("System language")];
    names.extend(langs.iter().map(|l| i18n::name(l).to_string()));
    let lang = dropdown(&names);
    lang.set_hexpand(true);
    lang.set_halign(Align::Fill);
    let cur = cfg.get("language").and_then(Value::as_str).unwrap_or("auto").to_string();
    lang.set_selected(langs.iter().position(|l| *l == cur).map(|i| i + 1).unwrap_or(0) as u32);
    lang.connect_selected_notify(move |d| {
        let i = d.selected() as usize;
        let key = if i == 0 { "auto".to_string() } else { langs[i - 1].to_string() };
        save_keys(&[("language", Value::from(key))]);
        // wie in der Python-Version: Neustart anbieten
        let Some(win) = d.root().and_then(|r| r.downcast::<gtk::Window>().ok()) else { return };
        let text = tr!("{app} will be shown in {language} after a restart. Restart now?", app = "FreeDeck", language = names[i]);
        ask_restart(&win, &text);
    });
    c.append(&lang);
    b.append(&c);
    sc.upcast()
}

/// Neustart anbieten („Neu starten“ / „Später“). Das neue FreeDeck startet, sobald dieses beendet ist.
fn ask_restart(parent: &gtk::Window, text: &str) {
    let (win, body, buttons) = dialog(parent, "FreeDeck");
    body.append(&text_label(text));
    let later = button(&tr!("Later"), None, "");
    let now = button(&tr!("Restart"), None, "primary");
    buttons.append(&later);
    buttons.append(&now);
    let w = win.clone();
    later.connect_clicked(move |_| w.close());
    now.connect_clicked(|_| {
        if let Ok(exe) = std::env::current_exe() {
            let pid = std::process::id();
            // warten, bis dieser Prozess (und mit ihm der Dienst) beendet ist
            let script = format!("while kill -0 {pid} 2>/dev/null; do sleep 0.2; done; exec \"$0\" gui");
            let _ = std::process::Command::new("sh").arg("-c").arg(script).arg(exe).spawn();
        }
        if let Some(app) = gtk::gio::Application::default() {
            app.quit();
        }
    });
    win.present();
}
