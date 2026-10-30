//! Dialoge von FreeWave (wavedialogs.py): Kanal anlegen/bearbeiten samt Programmen („Apps“), Programm
//! wählen, Mix anlegen/bearbeiten, Mixer-Szenen – dazu die Rechtsklick-Menüs der Mixe.

use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::rc::Rc;

use gtk::prelude::*;
use gtk::Align;
use gtk4 as gtk;

use freestream_core::i18n::ngettext;
use freestream_core::tr;
use freestream_wave::control::MixView;
use freestream_wave::mixer::{Device, RouteEntry, Stream};
use freestream_wave::Channel;
use serde_json::Value;

use super::apps::{self, AppEntry};
use super::theme;
use super::widgets::{self, hbox, label, vbox, Dialog};
use super::{bg, Win};

pub const ICONS: [&str; 16] = ["music", "browser", "chat", "game", "sfx", "system", "mic", "headphones", "speaker", "wave",
    "record", "play", "input", "keyboard", "stream", "grid"];
pub const COLORS: [&str; 10] = ["#ff4d5a", "#ff8a3d", "#ffb020", "#30d158", "#2fc3ff", "#2f7bff", "#6b74ff", "#a66bff",
    "#ff6bd5", "#8e9bb0"];
pub const MIX_ICONS: [&str; 10] = ["headphones", "stream", "chat", "speaker", "record", "game", "music", "mic", "wave", "grid"];
pub const MIX_COLORS: [&str; 8] = ["#22c3ee", "#c084fc", "#22c55e", "#f5b13d", "#ff4d5a", "#5b7cff", "#ff6bd5", "#8e9bb0"];

// ================================================================== Auswahl Symbol/Farbe
/// Auswahl als Reihe runder Felder (Symbole oder Farben) – wavedialogs._Picker.
#[derive(Clone)]
pub struct Picker {
    pub root: gtk::Grid,
    pub value: Rc<RefCell<String>>,
    pub color: Rc<RefCell<String>>,
    cells: Rc<Vec<gtk::DrawingArea>>,
    watch: Rc<RefCell<Vec<Box<dyn Fn(&str)>>>>,
}

impl Picker {
    pub fn new(values: &[&str], value: &str, is_color: bool, color: &str) -> Self {
        let root = gtk::Grid::new();
        root.set_row_spacing(4);
        root.set_column_spacing(4);
        let per_row = if is_color { 10 } else { 8 };
        let p = Self { root, value: Rc::new(RefCell::new(value.to_string())), color: Rc::new(RefCell::new(color.to_string())),
                       cells: Rc::new(Vec::new()), watch: Rc::new(RefCell::new(Vec::new())) };
        let mut cells = Vec::new();
        for (i, v) in values.iter().enumerate() {
            let a = gtk::DrawingArea::new();
            a.set_content_width(38);
            a.set_content_height(38);
            a.set_cursor_from_name(Some("pointer"));
            let (val, col, v2) = (p.value.clone(), p.color.clone(), v.to_string());
            a.set_draw_func(move |_, cr, _, _| {
                let sel = *val.borrow() == v2;
                if is_color {
                    if sel {
                        theme::set_source(cr, theme::FG);
                        cr.set_line_width(2.0);
                        cr.arc(19.0, 19.0, 17.0, 0.0, std::f64::consts::TAU);
                        let _ = cr.stroke();
                    }
                    theme::set_source(cr, &v2);
                    cr.arc(19.0, 19.0, 11.0, 0.0, std::f64::consts::TAU);
                    let _ = cr.fill();
                } else if sel {
                    if let Some(s) = theme::surface(&theme::tile_image(&v2, 34, &col.borrow(), 0.28, "#ffffff")) {
                        let _ = cr.set_source_surface(&s, 2.0, 2.0);
                        let _ = cr.paint();
                    }
                } else {
                    theme::rounded(cr, 2.5, 2.5, 33.0, 33.0, 9.0);
                    theme::set_source(cr, theme::PANEL2);
                    let _ = cr.fill_preserve();
                    theme::set_source(cr, theme::BORDER);
                    cr.set_line_width(1.0);
                    let _ = cr.stroke();
                    theme::paint_glyph(cr, &v2, 16, theme::MUTED, 19.0, 19.0);
                }
            });
            let (me, v2) = (p.clone(), v.to_string());
            widgets::on_click(&a, move || me.set(&v2, true));
            p.root.attach(&a, (i % per_row) as i32, (i / per_row) as i32, 1, 1);
            cells.push(a);
        }
        Self { cells: Rc::new(cells), ..p }
    }

    pub fn get(&self) -> String {
        self.value.borrow().clone()
    }

    pub fn set(&self, v: &str, notify: bool) {
        *self.value.borrow_mut() = v.to_string();
        for c in self.cells.iter() {
            c.queue_draw();
        }
        if notify {
            for f in self.watch.borrow().iter() {
                f(v);
            }
        }
    }

    pub fn set_color(&self, c: &str) {
        *self.color.borrow_mut() = c.to_string();
        for x in self.cells.iter() {
            x.queue_draw();
        }
    }

    pub fn on_change(&self, f: impl Fn(&str) + 'static) {
        self.watch.borrow_mut().push(Box::new(f));
    }
}

/// Auswahlliste aus Texten.
fn dropdown(items: &[String]) -> gtk::DropDown {
    gtk::DropDown::from_strings(&items.iter().map(String::as_str).collect::<Vec<_>>())
}

fn error_label() -> gtk::Label {
    let l = label("", &["danger", "small"]);
    l.set_margin_top(10);
    l
}

// ================================================================== Kanal
/// Daten für den Reiter „Apps“ (im Hintergrund gesammelt).
#[derive(Clone, Default)]
pub struct AppChoices {
    pub routes: Vec<RouteEntry>,
    pub running: Vec<Stream>,
    pub installed: Vec<AppEntry>,
    /// Programmschlüssel -> Name des Kanals, in dem es sonst landet
    pub elsewhere: HashMap<String, String>,
}

thread_local! {
    static INSTALLED: RefCell<Option<Vec<AppEntry>>> = const { RefCell::new(None) };
}

pub fn edit_channel_id(w: &Rc<Win>, cid: &str) {
    let s = w.snap.borrow().clone();
    if let Some(c) = s.channel(cid) {
        channel_dialog(w, Some(c.channel.clone()));
    }
}

/// Kanal anlegen (`None`) oder bearbeiten: Name, Art (Programme/Eingang), Gerät, Symbol, Farbe und unter
/// „Apps“ die Programme, die von selbst in diesen Kanal kommen.
pub fn channel_dialog(w: &Rc<Win>, ch: Option<Channel>) {
    let with_apps = ch.as_ref().map(|c| c.kind == "apps" && c.id != "mic").unwrap_or(true);
    let host = w.ctl.host().clone();
    let installed = INSTALLED.with(|i| i.borrow().clone());
    let ch2 = ch.clone();
    let w2 = w.clone();
    bg(move || {
        let m = &host.mixer;
        let mut c = ch2.clone();
        if let Some(c) = c.as_mut().filter(|c| c.id == "mic") {
            // das Gerät des Mikrofon-Kanals steht in mixer.mic
            c.source = m.conf().get("mic").and_then(Value::as_str).unwrap_or("").to_string();
        }
        let inputs = m.inputs();
        let apps = with_apps.then(|| {
            let installed = installed.unwrap_or_else(|| apps::app_entries(&apps::app_dirs()));
            let running = m.streams();
            let known: Vec<String> = running.iter().map(|s| s.names.clone())
                .chain(installed.iter().map(|e| format!("{} {} {}", e.key, e.cls, e.name))).collect();
            let routes = ch2.as_ref().map(|c| m.channel_routes(&c.id, &known)).unwrap_or_default();
            let names: HashMap<String, String> = m.app_channels().into_iter().map(|c| (c.id, c.name)).collect();
            let cid = ch2.as_ref().map(|c| c.id.clone());
            let elsewhere = m.routes().into_iter().filter(|(_, v)| names.contains_key(v) && Some(v) != cid.as_ref())
                .map(|(k, v)| (k, names[&v].clone())).collect();
            AppChoices { routes, running, installed, elsewhere }
        });
        (c, inputs, apps)
    }, move |(c, inputs, apps)| {
        if let Some(a) = &apps {
            INSTALLED.with(|i| *i.borrow_mut() = Some(a.installed.clone()));
        }
        ChannelDialog::open(&w2, c, inputs, apps);
    });
}

struct ChannelDialog {
    w: Rc<Win>,
    orig: Option<Channel>,
    d: Dialog,
    name: gtk::Entry,
    kind: RefCell<String>,
    dev: gtk::DropDown,
    inputs: Vec<Device>,
    dev0: u32,
    icon: Picker,
    color: Picker,
    error: gtk::Label,
    apps: Option<AppChoices>,
    app_list: RefCell<Vec<(String, String, String)>>,
    added: RefCell<Vec<(String, String)>>,
    removed: RefCell<Vec<String>>,
    app_rows: gtk::Box,
    tab_apps: RefCell<Option<gtk::ToggleButton>>,
}

impl ChannelDialog {
    fn open(w: &Rc<Win>, c: Option<Channel>, inputs: Vec<Device>, apps: Option<AppChoices>) {
        let edit = c.is_some();
        let is_mic = c.as_ref().is_some_and(|c| c.id == "mic");
        let title = if edit { tr!("Edit channel") } else { tr!("New channel") };
        let d = widgets::dialog(w.win.upcast_ref(), &title, 520, -1);
        let (dg, dc) = c.as_ref().map(theme::channel_look).unwrap_or_else(|| ("wave".into(), COLORS[5].into()));
        let icon0 = dg;
        let color0 = if COLORS.contains(&dc.as_str()) { dc } else { COLORS[5].to_string() };
        let tile = widgets::header(&d.body, &if edit { tr!("Edit channel") } else { tr!("Create new channel") },
                                   &if edit { tr!("Changes are applied to the mixer right away.") } else { tr!("Under “Apps” you choose which apps play in this channel.") },
                                   Some((&icon0, &color0)), 50);
        let kind = c.as_ref().map(|c| c.kind.clone()).unwrap_or_else(|| "apps".into());
        let apps = apps.filter(|_| !is_mic && kind == "apps");
        // Allgemein
        let general = widgets::card(&["big"]);
        widgets::field(&general, &tr!("Name"));
        let name = widgets::entry(c.as_ref().map(|c| c.name.as_str()).unwrap_or(""));
        name.set_margin_bottom(14);
        name.set_sensitive(!is_mic);
        general.append(&name);
        let kind_box = vbox(0);
        let kind_hint = widgets::wrap("", &["muted", "small"], 55);
        let dev_frame = vbox(0);
        widgets::field(&dev_frame, &tr!("INPUT DEVICE"));
        let mut names: Vec<String> = inputs.iter().map(|d| d.description.clone()).collect();
        if names.is_empty() {
            names.push(tr!("No input device found"));
        }
        let dev = dropdown(&names);
        dev.set_margin_bottom(14);
        let cur = c.as_ref().map(|c| c.source.clone()).unwrap_or_default();
        // beim Bearbeiten ohne passendes Gerät leer lassen statt das erste der Liste vorzutäuschen
        let dev0 = inputs.iter().position(|d| d.name == cur).map(|i| i as u32)
            .unwrap_or(if edit { gtk::INVALID_LIST_POSITION } else { 0 });
        dev.set_selected(dev0);
        dev_frame.append(&dev);
        widgets::field(&general, &tr!("ICON"));
        let icon = Picker::new(&ICONS, &icon0, false, &color0);
        icon.root.set_margin_bottom(14);
        general.append(&icon.root);
        widgets::field(&general, &tr!("COLOR"));
        let color = Picker::new(&COLORS, &color0, true, "");
        general.append(&color.root);
        let app_rows = vbox(6);
        let me = Rc::new(Self {
            w: w.clone(), orig: c.clone(), d, name, kind: RefCell::new(kind.clone()), dev, inputs, dev0, icon, color,
            error: error_label(), apps, app_list: RefCell::new(Vec::new()), added: RefCell::new(Vec::new()),
            removed: RefCell::new(Vec::new()), app_rows, tab_apps: RefCell::new(None),
        });
        {
            let (ic, t) = (me.icon.clone(), tile.clone());
            me.color.on_change(move |col| {
                ic.set_color(col);
                if let Some(t) = &t {
                    theme::set_tile(t, &ic.get(), 40, col, 0.28, "#ffffff");
                }
            });
            let (col, t) = (me.color.clone(), tile);
            me.icon.on_change(move |i| {
                if let Some(t) = &t {
                    theme::set_tile(t, i, 40, &col.get(), 0.28, "#ffffff");
                }
            });
        }
        // Reiter Allgemein / Apps
        let apps_card = widgets::card(&["big"]);
        let tabs = me.apps.as_ref().map(|_| {
            let (tabs, btns) = widgets::segmented(&[("general", tr!("General"), Some("gear")), ("apps", tr!("Apps"), Some("grid"))], "general", {
                let (g, a) = (general.clone(), apps_card.clone());
                move |k| {
                    g.set_visible(k == "general");
                    a.set_visible(k == "apps");
                }
            });
            tabs.set_margin_bottom(12);
            *me.tab_apps.borrow_mut() = Some(btns[1].clone());
            me.d.body.append(&tabs);
            tabs
        });
        if !edit {
            widgets::field(&kind_box, &tr!("TYPE"));
            let me2 = me.clone();
            let (seg, _) = widgets::segmented(&[("apps", tr!("Apps"), Some("grid")), ("input", tr!("Input device"), Some("input"))], "apps", {
                let (kh, df, tabs) = (kind_hint.clone(), dev_frame.clone(), tabs.clone());
                move |k| me2.set_kind(&k, &kh, &df, tabs.as_ref())
            });
            seg.set_margin_bottom(6);
            kind_box.append(&seg);
            kind_hint.set_margin_bottom(14);
            kind_box.append(&kind_hint);
        }
        general.insert_child_after(&kind_box, Some(&me.name));
        general.insert_child_after(&dev_frame, Some(&kind_box));
        me.d.body.append(&general);
        if let Some(a) = &me.apps {
            let installed = a.installed.clone();
            let mut list: Vec<(String, String, String)> = a.routes.iter().map(|r| {
                let e = apps::entry_for(&r.key, &installed);
                let name = match e { Some(e) if !r.own => e.name.clone(), _ => r.name.clone() };
                (r.key.clone(), name, e.map(|e| e.icon.clone()).unwrap_or_default())
            }).collect();
            list.sort_by_key(|x| x.1.to_lowercase());
            *me.app_list.borrow_mut() = list;
            apps_card.append(&label(&tr!("Apps in this channel"), &["bold"]));
            let h = widgets::wrap(&tr!("These apps land in this channel by themselves – also apps that are not running yet."), &["muted", "small"], 55);
            widgets::margins(&h, 2, 0, 12, 0);
            apps_card.append(&h);
            apps_card.append(&me.app_rows);
            let add = widgets::button(&tr!("Add app"), Some("plus"), "");
            add.set_halign(Align::Start);
            add.set_margin_top(12);
            let me2 = me.clone();
            add.connect_clicked(move |_| me2.pick_app());
            apps_card.append(&add);
            apps_card.set_visible(false);
            me.d.body.append(&apps_card);
            me.draw_apps();
        }
        me.d.body.append(&me.error);
        // Fußzeile
        let save = widgets::button(&if edit { tr!("Save") } else { tr!("Create channel") }, (!edit).then_some("plus"), "primary");
        let cancel = widgets::button(&tr!("Cancel"), None, "");
        me.d.buttons.append(&cancel);
        me.d.buttons.append(&save);
        let win = me.d.win.clone();
        cancel.connect_clicked(move |_| win.close());
        let me2 = me.clone();
        save.connect_clicked(move |_| me2.save());
        let me2 = me.clone();
        me.name.connect_activate(move |_| me2.save());
        if let Some(ch) = c.filter(|_| !is_mic) {
            let del = widgets::button(&tr!("Delete channel"), Some("trash"), "danger-btn");
            me.d.left.append(&del);
            let (w2, win) = (w.clone(), me.d.win.clone());
            del.connect_clicked(move |_| {
                win.close();
                delete_channel(&w2, &ch);
            });
        }
        me.set_kind(&kind, &kind_hint, &dev_frame, tabs.as_ref());
        me.d.win.present();
        me.name.grab_focus();
    }

    fn set_kind(&self, kind: &str, hint: &gtk::Label, dev_frame: &gtk::Box, tabs: Option<&gtk::Box>) {
        *self.kind.borrow_mut() = kind.to_string();
        hint.set_text(&if kind == "apps" {
            tr!("A separate channel for apps – e.g. “Podcast” or “Second game”.")
        } else {
            tr!("Bring a device straight into the mix – e.g. a second microphone, line-in or a capture card.")
        });
        dev_frame.set_visible(kind == "input");
        if let Some(t) = tabs {
            t.set_visible(kind != "input"); // Eingang: keine Programme
        }
    }

    fn running(&self, key: &str) -> bool {
        self.apps.as_ref().is_some_and(|a| a.running.iter().any(|s| apps::matches(key, if s.names.is_empty() { &s.key } else { &s.names })))
    }

    fn draw_apps(self: &Rc<Self>) {
        widgets::clear(&self.app_rows);
        let list = self.app_list.borrow().clone();
        if list.is_empty() {
            self.app_rows.append(&label(&tr!("No apps yet"), &["faint"]));
        }
        for (key, name, icon) in &list {
            let row = hbox(10);
            row.append(&app_image(icon, 28));
            row.append(&label(name, &[]));
            if !self.running(key) {
                row.append(&label(&tr!("(not active)"), &["faint", "small"]));
            }
            row.append(&widgets::spacer());
            let del = widgets::button("", Some("trash"), "ghost");
            let (me, k) = (self.clone(), key.clone());
            del.connect_clicked(move |_| me.remove_app(&k));
            row.append(&del);
            self.app_rows.append(&row);
        }
        if let Some(t) = self.tab_apps.borrow().as_ref() {
            let text = if list.is_empty() { tr!("Apps") } else { tr!("Apps ({n})", n = list.len()) };
            if let Some(l) = t.child().and_then(|r| r.last_child()).and_then(|l| l.downcast::<gtk::Label>().ok()) {
                l.set_text(&text);
            }
        }
    }

    fn add_app(self: &Rc<Self>, key: &str, name: &str, icon: &str) {
        let key = key.trim().to_lowercase();
        if key.is_empty() || self.app_list.borrow().iter().any(|a| a.0 == key) {
            return;
        }
        let name = if name.is_empty() { freestream_wave::mixer::title(&key) } else { name.to_string() };
        self.app_list.borrow_mut().push((key.clone(), name.clone(), icon.to_string()));
        self.app_list.borrow_mut().sort_by_key(|a| a.1.to_lowercase());
        self.removed.borrow_mut().retain(|k| *k != key);
        self.added.borrow_mut().retain(|(k, _)| *k != key);
        self.added.borrow_mut().push((key, name));
        self.draw_apps();
    }

    fn remove_app(self: &Rc<Self>, key: &str) {
        self.app_list.borrow_mut().retain(|a| a.0 != key);
        let was_added = self.added.borrow().iter().any(|(k, _)| k == key);
        if was_added {
            self.added.borrow_mut().retain(|(k, _)| k != key);
        } else {
            self.removed.borrow_mut().push(key.to_string());
        }
        self.draw_apps();
    }

    fn pick_app(self: &Rc<Self>) {
        let Some(a) = self.apps.clone() else { return };
        let taken: Vec<String> = self.app_list.borrow().iter().map(|x| x.0.clone()).collect();
        let me = self.clone();
        app_picker(&self.d.win, a, taken, move |k, n, i| me.add_app(k, n, i));
    }

    fn save(self: &Rc<Self>) {
        let name = self.name.text().trim().to_string();
        let kind = self.kind.borrow().clone();
        let sel = self.dev.selected();
        let source = self.inputs.get(sel as usize).map(|d| d.name.clone()).unwrap_or_default();
        let source_changed = sel != self.dev0;
        if name.is_empty() && self.name.is_sensitive() {
            self.error.set_text(&tr!("Please enter a name"));
            return;
        }
        if kind == "input" && source.is_empty() && (self.orig.is_none() || source_changed) {
            self.error.set_text(&tr!("Please choose an input device"));
            return;
        }
        let (icon, color) = (self.icon.get(), self.color.get());
        let mut added = self.added.borrow().clone();
        added.sort();
        let mut removed = self.removed.borrow().clone();
        removed.sort();
        if kind != "apps" {
            added.clear();
            removed.clear();
        }
        self.d.win.close();
        let orig = self.orig.clone();
        let created = orig.is_none();
        self.w.job(move |h| {
            let m = &h.mixer;
            let res = match &orig {
                // neuer Kanal startet in keinem Mix – außer er bekommt gleich Programme (sonst wären sie stumm)
                None => m.add_channel(&name, &kind, &source, &icon, &color, !added.is_empty())?,
                Some(c) => {
                    let is_mic = c.id == "mic";
                    let src = ((c.kind == "input" || is_mic) && source_changed).then_some(source.as_str());
                    m.update_channel(&c.id, (!is_mic).then_some(name.as_str()), Some(&icon), Some(&color), src)?
                }
            };
            if !added.is_empty() || !removed.is_empty() {
                m.set_channel_routes(&res.id, &added, &removed)?;
            }
            Ok(res)
        }, move |w, res| {
            w.status(&if created { tr!("Channel “{name}” created", name = res.name) } else { tr!("Channel “{name}” saved", name = res.name) });
        });
    }
}

/// Programmsymbol (Themenname oder Datei), sonst Ersatzkachel.
fn app_image(icon: &str, size: i32) -> gtk::Image {
    if !icon.is_empty() {
        if icon.starts_with('/') {
            if std::path::Path::new(icon).exists() {
                let img = gtk::Image::from_file(icon);
                img.set_pixel_size(size);
                return img;
            }
        } else if let Some(d) = gtk::gdk::Display::default() {
            if gtk::IconTheme::for_display(&d).has_icon(icon) {
                let img = gtk::Image::from_icon_name(icon);
                img.set_pixel_size(size);
                return img;
            }
        }
    }
    theme::tile("grid", size, "#3a4252", 0.3, "#ffffff")
}

fn delete_channel(w: &Rc<Win>, ch: &Channel) {
    let (w2, c) = (w.clone(), ch.clone());
    widgets::confirm(w.win.upcast_ref(), &tr!("Delete channel"), &tr!("Really delete channel “{name}”?", name = ch.name), move || {
        let (id, name) = (c.id.clone(), c.name.clone());
        w2.job(move |h| h.mixer.remove_channel(&id), move |w, _| w.status(&tr!("Channel “{name}” deleted", name = name)));
    });
}

/// Programm für einen Kanal wählen: gerade spielende oben, darunter alle installierten, mit Suche.
fn app_picker(parent: &gtk::Window, a: AppChoices, taken: Vec<String>, on_pick: impl Fn(&str, &str, &str) + 'static) {
    let d = widgets::dialog(parent, &tr!("Add app"), 460, 560);
    widgets::header(&d.body, &tr!("Add app"), &tr!("Its sound goes to this channel from now on, whenever it plays."), Some(("grid", "#2f7bff")), 45);
    widgets::field(&d.body, &tr!("SEARCH"));
    let search = gtk::SearchEntry::new();
    search.set_margin_bottom(10);
    d.body.append(&search);
    let list = vbox(0);
    d.body.append(&widgets::scroll(&list));
    let cancel = widgets::button(&tr!("Cancel"), None, "");
    d.buttons.append(&cancel);
    let win = d.win.clone();
    cancel.connect_clicked(move |_| win.close());
    let on_pick: Rc<dyn Fn(&str, &str, &str)> = Rc::new(on_pick);
    let a = Rc::new(a);
    // [(Abschnitt, [(Schlüssel, Name, Symbol, Unterzeile)])] passend zur Suche
    let candidates = {
        let a = a.clone();
        move |q: &str| -> Vec<(String, Vec<(String, String, String, String)>)> {
            let mut seen: Vec<String> = taken.clone();
            let (mut playing, mut installed) = (Vec::new(), Vec::new());
            for s in &a.running {
                let key = s.key.clone();
                if key.is_empty() || seen.contains(&key) {
                    continue;
                }
                let e = apps::entry_for(&key, &a.installed);
                let name = e.map(|e| e.name.clone()).unwrap_or_else(|| if s.app.is_empty() { freestream_wave::mixer::title(&key) } else { s.app.clone() });
                if !q.is_empty() && !name.to_lowercase().contains(q) && !key.contains(q) {
                    continue;
                }
                seen.push(key.clone());
                playing.push((key.clone(), name, e.map(|e| e.icon.clone()).unwrap_or_default(), a.elsewhere.get(&key).cloned().unwrap_or_default()));
            }
            for e in &a.installed {
                if seen.contains(&e.key) || (!q.is_empty() && !e.name.to_lowercase().contains(q) && !e.key.contains(q)) {
                    continue;
                }
                seen.push(e.key.clone());
                installed.push((e.key.clone(), e.name.clone(), e.icon.clone(), a.elsewhere.get(&e.key).cloned().unwrap_or_default()));
            }
            [(tr!("PLAYING NOW"), playing), (tr!("INSTALLED"), installed)].into_iter().filter(|(_, i)| !i.is_empty()).collect()
        }
    };
    let candidates = Rc::new(candidates);
    let fill = {
        let (list, search, cands, win, pick) = (list.clone(), search.clone(), candidates.clone(), d.win.clone(), on_pick.clone());
        move || {
            widgets::clear(&list);
            let q_raw = search.text().trim().to_string();
            let q = q_raw.to_lowercase();
            let row = |key: String, name: String, icon: String, sub: String, own: bool| {
                let b = gtk::Button::new();
                b.add_css_class("list-row");
                let r = hbox(10);
                r.append(&app_image(&icon, 28));
                r.append(&label(&name, &[]));
                if !sub.is_empty() {
                    let t = if own { sub } else { tr!("now in “{channel}”", channel = sub) };
                    r.append(&label(&t, &["faint", "small"]));
                }
                b.set_child(Some(&r));
                let (w2, p) = (win.clone(), pick.clone());
                let picked_name = if own { q_raw.clone() } else { name };
                b.connect_clicked(move |_| {
                    w2.close();
                    p(&key, &picked_name, &icon);
                });
                b
            };
            if !q.is_empty() {
                list.append(&row(q.clone(), tr!("Add “{name}”", name = q_raw), String::new(), tr!("by name – for apps not listed here"), true));
            }
            let groups = cands(&q);
            for (title, items) in &groups {
                let f = widgets::section(title);
                widgets::margins(&f, 10, 0, 4, 0);
                list.append(&f);
                for (k, n, i, s) in items.iter().take(200) {
                    list.append(&row(k.clone(), n.clone(), i.clone(), s.clone(), false));
                }
            }
            if groups.is_empty() && q.is_empty() {
                list.append(&label(&tr!("No apps found"), &["faint"]));
            }
        }
    };
    let fill = Rc::new(fill);
    let f = fill.clone();
    search.connect_search_changed(move |_| f());
    let (cands, win, pick) = (candidates.clone(), d.win.clone(), on_pick.clone());
    search.connect_activate(move |s| {
        let q = s.text().trim().to_string();
        let groups = cands(&q.to_lowercase());
        let all: Vec<_> = groups.iter().flat_map(|(_, i)| i.iter()).collect();
        if all.len() == 1 {
            win.close();
            pick(&all[0].0, &all[0].1, &all[0].2);
        } else if !q.is_empty() {
            win.close();
            pick(&q.to_lowercase(), &q, "");
        }
    });
    fill();
    d.win.present();
    search.grab_focus();
}

// ================================================================== Mixe
pub fn edit_mix_id(w: &Rc<Win>, mid: &str) {
    let s = w.snap.borrow().clone();
    if !s.active {
        return; // Mixer aus oder Mix gerade gelöscht: nichts zu bearbeiten
    }
    if let Some(m) = s.mixes.iter().find(|m| m.mix.id == mid) {
        mix_dialog(w, Some(m.clone()));
    }
}

/// Rechtsklick auf einen Mix (Seitenleiste oder Spaltenkopf): bearbeiten, abhören, löschen.
pub fn mix_menu(w: &Rc<Win>, mid: &str, anchor: &impl IsA<gtk::Widget>, x: f64, y: f64) {
    let s = w.snap.borrow().clone();
    if !s.active {
        return;
    }
    let Some(m) = s.mixes.iter().find(|m| m.mix.id == mid).cloned() else { return };
    let listening = w.listen_now.borrow().clone().unwrap_or_else(|| s.listening.clone()) == mid;
    let mut items = Vec::new();
    let (w2, m2) = (w.clone(), m.clone());
    items.push(widgets::item(&tr!("Edit …"), "edit", move || mix_dialog(&w2, Some(m2.clone()))));
    if m.can_listen && mid != "monitor" {
        let (w2, id) = (w.clone(), mid.to_string());
        items.push(widgets::item(&if listening { tr!("Stop listening") } else { tr!("Listen") }, "headphones", move || {
            let target = if listening { "monitor".to_string() } else { id.clone() };
            *w2.listen_now.borrow_mut() = Some(target.clone());
            w2.touch("listen");
            let name = w2.snap.borrow().mixes.iter().find(|x| x.mix.id == target).map(|x| x.mix.name.clone()).unwrap_or_else(|| target.clone());
            w2.job(move |h| h.mixer.listen(&target), move |w, _| w.status(&tr!("Listening to “{mix}”", mix = name)));
        }));
    }
    items.push(None);
    let (w2, m2) = (w.clone(), m.clone());
    let mut del = widgets::item(&tr!("Delete mix"), "trash", move || delete_mix(&w2, &m2));
    if let Some(d) = del.as_mut() {
        d.danger = true;
        if m.mix.builtin {
            d.action = None;
        }
    }
    items.push(del);
    widgets::popup(anchor, Some((x, y)), items, 232);
}

fn delete_mix(w: &Rc<Win>, m: &MixView) {
    let (w2, id, name) = (w.clone(), m.mix.id.clone(), m.mix.name.clone());
    widgets::confirm(w.win.upcast_ref(), &tr!("Delete mix"), &tr!("Really delete mix “{name}”?", name = m.mix.name), move || {
        let (id, name) = (id.clone(), name.clone());
        w2.job(move |h| h.mixer.remove_mix(&id), move |w, _| w.status(&tr!("Mix “{name}” deleted", name = name)));
    });
}

/// Zeile zum Anhaken: Kästchen, Symbol, Titel und Unterzeile (Ausgänge eines Mixes) – _CheckRow.
pub fn check_row(title: &str, sub: &str, icon: &str, checked: bool, enabled: bool, on: impl Fn(bool) + 'static) -> gtk::Button {
    let b = gtk::Button::new();
    b.add_css_class("check-row");
    let state = Rc::new(Cell::new(checked));
    let r = hbox(12);
    let bx = gtk::DrawingArea::new();
    bx.set_content_width(20);
    bx.set_content_height(20);
    let s2 = state.clone();
    bx.set_draw_func(move |_, cr, _, _| {
        let on = s2.get();
        theme::rounded(cr, 1.0, 1.0, 18.0, 18.0, 4.0);
        if on {
            theme::set_source(cr, theme::ACCENT);
            let _ = cr.fill();
            theme::paint_glyph(cr, "check", 13, "#ffffff", 10.0, 10.0);
        } else {
            theme::set_source(cr, theme::FAINT);
            cr.set_line_width(2.0);
            let _ = cr.stroke();
        }
    });
    r.append(&bx);
    r.append(&theme::glyph(icon, 17, theme::MUTED));
    let t = vbox(0);
    let tl = label(title, &["bold"]);
    if !enabled {
        tl.add_css_class("muted");
    }
    t.append(&tl);
    t.append(&label(sub, &["muted", "small"]));
    r.append(&t);
    b.set_child(Some(&r));
    widgets::set_class(&b, "selected", checked);
    if enabled {
        b.connect_clicked(move |b| {
            let v = !state.get();
            state.set(v);
            widgets::set_class(b, "selected", v);
            bx.queue_draw();
            on(v);
        });
    } else {
        b.set_can_target(false);
    }
    b
}

/// Mix anlegen oder bearbeiten: Name, Symbol, Farbe und Ausgänge (Geräte und/oder virtuelle Quelle).
pub fn mix_dialog(w: &Rc<Win>, m: Option<MixView>) {
    let s = w.snap.borrow().clone();
    let edit = m.is_some();
    let mid = m.as_ref().map(|m| m.mix.id.clone()).unwrap_or_default();
    let d = widgets::dialog(w.win.upcast_ref(), &if edit { tr!("Edit mix") } else { tr!("New mix") }, 560, 720);
    let icon0 = m.as_ref().map(|m| m.mix.icon.clone()).filter(|i| !i.is_empty()).unwrap_or_else(|| "chat".into());
    let color0 = m.as_ref().map(|m| m.mix.color.clone()).filter(|c| MIX_COLORS.contains(&c.as_str())).unwrap_or_else(|| MIX_COLORS[2].into());
    let tile = widgets::header(&d.body, &if edit { tr!("Edit mix") } else { tr!("Create new mix") }, &tr!("Where this mix goes and what it looks like"),
                               Some((&icon0, &color0)), 50);
    let cb = vbox(0);
    d.body.append(&widgets::scroll(&cb));
    widgets::field(&cb, &tr!("Name"));
    let name = widgets::entry(m.as_ref().map(|m| m.mix.name.as_str()).unwrap_or(""));
    name.set_margin_bottom(14);
    cb.append(&name);
    widgets::field(&cb, &tr!("ICON AND COLOR"));
    let icon = Picker::new(&MIX_ICONS, &icon0, false, &color0);
    icon.root.set_margin_bottom(6);
    cb.append(&icon.root);
    let color = Picker::new(&MIX_COLORS, &color0, true, "");
    color.root.set_margin_bottom(14);
    cb.append(&color.root);
    {
        let (ic, t) = (icon.clone(), tile.clone());
        color.on_change(move |c| {
            ic.set_color(c);
            if let Some(t) = &t {
                theme::set_tile(t, &ic.get(), 40, c, 0.28, "#ffffff");
            }
        });
        let (col, t) = (color.clone(), tile);
        icon.on_change(move |i| {
            if let Some(t) = &t {
                theme::set_tile(t, i, 40, &col.get(), 0.28, "#ffffff");
            }
        });
    }
    widgets::field(&cb, &tr!("OUTPUTS – THE MIX GOES TO ALL TICKED ONES"));
    let current: Vec<String> = m.as_ref().map(|m| if !m.mix.devices.is_empty() { m.mix.devices.clone() } else if m.mix.id == "monitor" { m.devices.clone() } else { Vec::new() })
        .unwrap_or_default();
    let devices = Rc::new(RefCell::new(current.clone()));
    let rows = vbox(6);
    if s.outputs.is_empty() {
        rows.append(&label(&tr!("No output device found"), &["muted", "small"]));
    }
    for o in &s.outputs {
        let (devs, n) = (devices.clone(), o.name.clone());
        rows.append(&check_row(&o.description, &tr!("Device"), "speaker", current.contains(&o.name), true, move |on| {
            devs.borrow_mut().retain(|d| *d != n);
            if on {
                devs.borrow_mut().push(n.clone());
            }
        }));
    }
    let is_stream = mid == "stream";
    let virt = Rc::new(Cell::new(m.as_ref().map(|m| m.mix.virtual_).unwrap_or(true)));
    let mname = m.as_ref().map(|m| m.mix.name.clone()).filter(|n| !n.is_empty()).unwrap_or_else(|| tr!("Mix"));
    let vname = if is_stream { tr!("FreeWave Stream Mix") } else { format!("FreeWave {mname}") };
    let v2 = virt.clone();
    rows.append(&check_row(&tr!("Virtual source “{name}”", name = vname),
                           &if is_stream { tr!("always on – OBS records the Stream Mix from here") } else { tr!("for apps like Discord or OBS") },
                           "stream", virt.get() || is_stream, !is_stream, move |on| v2.set(on)));
    cb.append(&rows);
    let hint = if mid == "monitor" {
        tr!("Without a ticked device the Personal Mix plays on the device chosen under “Listen”.")
    } else {
        tr!("Tip: in Discord choose “Monitor of FreeWave {name}” as microphone.", name = mname)
    };
    let h = widgets::wrap(&hint, &["muted", "small"], 55);
    h.set_margin_top(4);
    cb.append(&h);
    let error = error_label();
    d.body.append(&error);
    let save = widgets::button(&if edit { tr!("Save") } else { tr!("Create mix") }, (!edit).then_some("plus"), "primary");
    let cancel = widgets::button(&tr!("Cancel"), None, "");
    d.buttons.append(&cancel);
    d.buttons.append(&save);
    let win = d.win.clone();
    cancel.connect_clicked(move |_| win.close());
    if let Some(mv) = m.clone().filter(|m| !m.mix.builtin) {
        let del = widgets::button(&tr!("Delete mix"), Some("trash"), "danger-btn");
        d.left.append(&del);
        let (w2, win) = (w.clone(), d.win.clone());
        del.connect_clicked(move |_| {
            win.close();
            delete_mix(&w2, &mv);
        });
    }
    let outputs: Vec<String> = s.outputs.iter().map(|o| o.name.clone()).collect();
    let (w2, win) = (w.clone(), d.win.clone());
    let do_save = Rc::new(move || {
        let n = name.text().trim().to_string();
        // bisherige Geräte behalten ihre Reihenfolge (das erste ist beim Persönlichen Mix das Abhörgerät)
        let order: Vec<String> = current.iter().filter(|d| devices.borrow().contains(d)).cloned().chain(outputs.iter().cloned()).collect();
        let mut devs = devices.borrow().clone();
        devs.sort_by_key(|d| order.iter().position(|x| x == d).unwrap_or(order.len()));
        let v = virt.get() || is_stream;
        if n.is_empty() {
            error.set_text(&tr!("Please enter a name"));
            return;
        }
        if devs.is_empty() && !v && mid != "monitor" {
            error.set_text(&tr!("Choose at least one output"));
            return;
        }
        win.close();
        let (ic, co, id) = (icon.get(), color.get(), mid.clone());
        let created = id.is_empty();
        w2.job(move |h| {
            let m = &h.mixer;
            if id.is_empty() {
                // neuer Mix startet leer: welche Kanäle hinein, wählt man selbst in der Tabelle
                let ex: Vec<String> = m.all_channels().into_iter().map(|c| c.id).collect();
                m.add_mix(&n, &ic, &co, &devs, v, &ex)
            } else {
                m.update_mix(&id, Some(&n), Some(&ic), Some(&co), Some(&devs), Some(v))
            }
        }, move |w, res| {
            w.status(&if created { tr!("Mix “{name}” created", name = res.name) } else { tr!("Mix “{name}” saved", name = res.name) });
        });
    });
    let f = do_save.clone();
    save.connect_clicked(move |_| f());
    d.win.present();
}

// ================================================================== Szenen
/// Szenen-Menü am Knopf im Kopf: anwenden, aktuelle Einstellung speichern, verwalten.
pub fn scene_menu(w: &Rc<Win>, anchor: &gtk::Button) {
    let s = w.snap.borrow().clone();
    let active = s.active_scene.clone().unwrap_or_default();
    let mut items = Vec::new();
    for sc in &s.scenes {
        let (w2, id, name) = (w.clone(), sc.id.clone(), sc.name.clone());
        items.push(widgets::item(if sc.name.is_empty() { &sc.id } else { &sc.name }, if sc.id == active { "check" } else { "layers" },
                                 move || apply_scene(&w2, &id, &name)));
    }
    items.push(None);
    let w2 = w.clone();
    items.push(widgets::item(&tr!("Save current mix as scene …"), "plus", move || new_scene(&w2)));
    let w2 = w.clone();
    items.push(widgets::item(&tr!("Manage scenes …"), "edit", move || open_scenes(&w2)));
    widgets::popup(anchor, None, items, 260);
}

fn apply_scene(w: &Rc<Win>, id: &str, _name: &str) {
    w.release_all(); // Regler sofort auf die Szene springen lassen
    let id = id.to_string();
    w.job(move |h| h.mixer.apply_scene(&id), |w, sc| w.status(&tr!("Scene “{name}” applied", name = sc.name)));
}

fn new_scene(w: &Rc<Win>) {
    let s = w.snap.borrow().clone();
    let active = s.active_scene.as_ref().and_then(|id| s.scenes.iter().find(|x| &x.id == id)).map(|x| x.name.clone()).unwrap_or_default();
    let w2 = w.clone();
    scene_name_dialog(w, &active, false, move |name| {
        w2.job(move |h| h.mixer.save_scene(&name), |w, sc| w.status(&tr!("Scene “{name}” saved", name = sc.name)));
    });
}

/// Szene speichern oder umbenennen: nur ein Name.
fn scene_name_dialog(w: &Rc<Win>, name: &str, rename: bool, on_save: impl Fn(String) + 'static) {
    let title = if rename { tr!("Rename scene") } else { tr!("Save scene") };
    let d = widgets::dialog(w.win.upcast_ref(), &title, 460, -1);
    let sub = if rename { String::new() } else { tr!("Saves the volume and mute state of every channel in both mixes.") };
    widgets::header(&d.body, &title, &sub, Some(("layers", theme::ACCENT)), 45);
    widgets::field(&d.body, &tr!("Name"));
    let e = widgets::entry(name);
    d.body.append(&e);
    let error = error_label();
    d.body.append(&error);
    let save = widgets::button(&tr!("Save"), None, "primary");
    let cancel = widgets::button(&tr!("Cancel"), None, "");
    d.buttons.append(&cancel);
    d.buttons.append(&save);
    let win = d.win.clone();
    cancel.connect_clicked(move |_| win.close());
    let (win, e2) = (d.win.clone(), e.clone());
    let go = Rc::new(move || {
        let n = e2.text().trim().to_string();
        if n.is_empty() {
            error.set_text(&tr!("Please enter a name"));
            return;
        }
        win.close();
        on_save(n);
    });
    let g = go.clone();
    save.connect_clicked(move |_| g());
    e.connect_activate(move |_| go());
    d.win.present();
    e.grab_focus();
    e.select_region(0, -1);
}

thread_local! {
    static SCENES: RefCell<Option<(gtk::Window, gtk::Box, String)>> = const { RefCell::new(None) };
}

/// Mixer-Szenen verwalten: anwenden, umbenennen, löschen, neue speichern.
pub fn open_scenes(w: &Rc<Win>) {
    if let Some(win) = SCENES.with(|s| s.borrow().as_ref().map(|x| x.0.clone())) {
        win.present();
        return;
    }
    let d = widgets::dialog(w.win.upcast_ref(), &tr!("Mixer scenes"), 640, 520);
    widgets::header(&d.body, &tr!("Mixer scenes"),
                    &tr!("A scene remembers the volume and mute state of every channel. Apply it with one click, a keyboard shortcut or a FreeDeck key."),
                    Some(("layers", theme::ACCENT)), 60);
    let list = vbox(8);
    d.body.append(&widgets::scroll(&list));
    let done = widgets::button(&tr!("Done"), None, "primary");
    d.buttons.append(&done);
    let win = d.win.clone();
    done.connect_clicked(move |_| win.close());
    let new = widgets::button(&tr!("Save current mix"), Some("plus"), "");
    d.left.append(&new);
    let w2 = w.clone();
    new.connect_clicked(move |_| new_scene(&w2));
    d.win.connect_close_request(|_| {
        SCENES.with(|s| *s.borrow_mut() = None);
        gtk::glib::Propagation::Proceed
    });
    SCENES.with(|s| *s.borrow_mut() = Some((d.win.clone(), list, String::new())));
    update_scenes(w);
    d.win.present();
}

/// Liste im offenen Szenen-Dialog an den Zustand anpassen.
pub fn update_scenes(w: &Rc<Win>) {
    let Some((_, list, old)) = SCENES.with(|s| s.borrow().clone()) else { return };
    let s = w.snap.borrow().clone();
    let active = s.active_scene.clone().unwrap_or_default();
    let sig = format!("{:?}|{active}", s.scenes.iter().map(|x| (&x.id, &x.name, x.levels.len())).collect::<Vec<_>>());
    if sig == old {
        return;
    }
    SCENES.with(|x| {
        if let Some(e) = x.borrow_mut().as_mut() {
            e.2 = sig;
        }
    });
    widgets::clear(&list);
    if s.scenes.is_empty() {
        list.append(&widgets::wrap(&tr!("No scenes yet. Set up the mixer the way you like it and save it as a scene."), &["muted"], 60));
        return;
    }
    for sc in &s.scenes {
        let on = sc.id == active;
        let card = hbox(12);
        card.add_css_class("card");
        card.add_css_class("r12");
        if on {
            card.add_css_class("active");
        }
        card.append(&theme::tile("layers", 32, if on { theme::ACCENT } else { theme::FAINT }, 0.3, "#ffffff"));
        let txt = vbox(0);
        txt.set_hexpand(true);
        txt.append(&label(if sc.name.is_empty() { &sc.id } else { &sc.name }, &["bold"]));
        let n = sc.levels.len() as i64;
        let sub = if on { tr!("Active") } else { ngettext("{count} channel", "{count} channels", n).replace("{count}", &n.to_string()) };
        txt.append(&label(&sub, &["small", if on { "accent-text" } else { "muted" }]));
        card.append(&txt);
        let apply = widgets::button(&tr!("Apply"), None, "");
        let (w2, id, name) = (w.clone(), sc.id.clone(), sc.name.clone());
        apply.connect_clicked(move |_| apply_scene(&w2, &id, &name));
        card.append(&apply);
        let ren = widgets::button("", Some("edit"), "ghost");
        let (w2, id, name) = (w.clone(), sc.id.clone(), sc.name.clone());
        ren.connect_clicked(move |_| {
            let (w3, id) = (w2.clone(), id.clone());
            scene_name_dialog(&w2, &name, true, move |n| {
                let id = id.clone();
                w3.job(move |h| h.mixer.rename_scene(&id, &n), |w, sc| w.status(&tr!("Scene renamed to “{name}”", name = sc.name)));
            });
        });
        card.append(&ren);
        let del = widgets::button("", Some("trash"), "ghost");
        let (w2, id, name) = (w.clone(), sc.id.clone(), sc.name.clone());
        del.connect_clicked(move |_| {
            let (w3, id) = (w2.clone(), id.clone());
            widgets::confirm(w2.win.upcast_ref(), &tr!("Delete scene"), &tr!("Really delete scene “{name}”?", name = name), move || {
                let id = id.clone();
                w3.job(move |h| h.mixer.delete_scene(&id), |w, sc| w.status(&tr!("Scene “{name}” deleted", name = sc.name)));
            });
        });
        card.append(&del);
        list.append(&card);
    }
}
