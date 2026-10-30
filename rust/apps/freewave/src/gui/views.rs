//! Ansichten „Effekte“ (Effektketten je Kanal) und „Apps“ (Programme je Kanal, per Klick oder Ziehen
//! in einen anderen Kanal) – waveui.py: _fill_fx_view, _fill_apps_view, AppChip, _drag_app.

use std::cell::RefCell;
use std::rc::Rc;
use std::sync::Arc;

use gtk::prelude::*;
use gtk::Align;
use gtk4 as gtk;

use freestream_core::tr;
use freestream_wave::control::Snapshot;
use freestream_wave::mixer::Stream;

use super::theme::{self, channel_look};
use super::widgets::{self, hbox, label, vbox};
use super::{effects_ui, mixer, Win};

pub struct Views {
    pub fx_root: gtk::ScrolledWindow,
    fx_box: gtk::Box,
    fx_sig: RefCell<Option<String>>,
    pub apps_root: gtk::ScrolledWindow,
    apps_box: gtk::Box,
    apps_sig: RefCell<Option<String>>,
}

fn not_in_mixer() -> String {
    tr!("– not in mixer –")
}

impl Views {
    pub fn new() -> Self {
        let fx_box = vbox(0);
        widgets::margins(&fx_box, 18, 22, 18, 22);
        fx_box.append(&super::placeholder(&tr!("Set up the mixer to add effects to channels.")));
        let fx_root = widgets::scroll(&fx_box);
        let apps_box = vbox(0);
        widgets::margins(&apps_box, 18, 22, 18, 22);
        let apps_root = widgets::scroll(&apps_box);
        Self { fx_root, fx_box, fx_sig: RefCell::new(None), apps_root, apps_box, apps_sig: RefCell::new(None) }
    }
}

pub fn apply_off(w: &Rc<Win>) {
    let v = &w.views;
    if v.fx_sig.borrow_mut().take().is_some() || v.fx_box.first_child().is_none() {
        widgets::clear(&v.fx_box);
        v.fx_box.append(&super::placeholder(&tr!("Set up the mixer to add effects to channels.")));
    }
    if v.apps_sig.borrow_mut().take().is_some() {
        widgets::clear(&v.apps_box);
    }
    w.set_nav_count("effects", 0);
    w.set_nav_count("apps", 0);
}

pub fn apply(w: &Rc<Win>, s: &Arc<Snapshot>) {
    let conf = w.conf.borrow().clone();
    let v = &w.views;
    // --- Effekte
    let fx: Vec<(String, Vec<String>, usize)> = mixer::ordered(s).iter().map(|c| {
        let names = effects_ui::fx_names(&conf, &c.channel.id);
        let total = effects_ui::chain(&conf, &c.channel.id).len();
        (c.channel.id.clone(), names, total)
    }).collect();
    let sig = format!("{:?}{:?}", fx, s.channels.iter().map(|c| (&c.channel.name, &c.channel.icon, &c.channel.color)).collect::<Vec<_>>());
    if v.fx_sig.borrow().as_deref() != Some(&sig) {
        *v.fx_sig.borrow_mut() = Some(sig);
        fill_fx(w, s, &fx);
    }
    let count: usize = s.channels.iter().map(|c| effects_ui::chain(&conf, &c.channel.id).iter().filter(|f| f.enabled).count()).sum();
    w.set_nav_count("effects", count);
    // --- Apps (nicht neu bauen, während ein Programm gerade verschoben wurde)
    let sig = format!("{:?}{:?}", s.streams.iter().map(|x| (&x.index, &x.app, &x.channel)).collect::<Vec<_>>(),
                      s.channels.iter().map(|c| (&c.channel.id, &c.channel.name, &c.channel.icon, &c.channel.color, &c.channel.kind)).collect::<Vec<_>>());
    if v.apps_sig.borrow().as_deref() != Some(&sig) && !w.held("streams") {
        *v.apps_sig.borrow_mut() = Some(sig);
        fill_apps(w, s);
    }
    w.set_nav_count("apps", s.streams.len());
}

fn fill_fx(w: &Rc<Win>, s: &Snapshot, fx: &[(String, Vec<String>, usize)]) {
    let b = &w.views.fx_box;
    widgets::clear(b);
    let grid = gtk::Grid::new();
    grid.set_column_homogeneous(true);
    grid.set_column_spacing(12);
    grid.set_row_spacing(10);
    b.append(&grid);
    for (i, (cid, names, total)) in fx.iter().enumerate() {
        let Some(cv) = s.channel(cid) else { continue };
        let ch = &cv.channel;
        let card = hbox(12);
        card.add_css_class("card");
        card.add_css_class("r12");
        let (glyph, color) = channel_look(ch);
        card.append(&theme::tile(&glyph, 34, &color, 0.3, "#ffffff"));
        let txt = vbox(0);
        txt.set_hexpand(true);
        txt.set_valign(Align::Center);
        txt.append(&label(&ch.name, &["bold"]));
        let summary = if !names.is_empty() {
            names.join(" · ")
        } else if *total > 0 {
            tr!("All effects are switched off.")
        } else {
            tr!("No effects yet")
        };
        let sl = label(&summary, &["small", if names.is_empty() { "muted" } else { "accent-text" }]);
        sl.set_ellipsize(gtk::pango::EllipsizeMode::End);
        txt.append(&sl);
        card.append(&txt);
        if !names.is_empty() {
            let n = label(&names.len().to_string(), &["small", "bold", "accent-text"]);
            n.set_margin_end(4);
            card.append(&n);
        }
        let edit = widgets::button(&tr!("Edit"), Some("fx"), "");
        let (w2, c2) = (w.clone(), ch.clone());
        edit.connect_clicked(move |_| effects_ui::open(&w2, &c2));
        card.append(&edit);
        grid.attach(&card, (i % 2) as i32, (i / 2) as i32, 1, 1);
    }
}

/// Inhalt beim Ziehen eines Programms: Index, Schlüssel, Name und bisheriger Kanal.
fn drag_value(s: &Stream) -> String {
    [s.index.as_str(), s.key.as_str(), s.app.as_str(), s.channel.as_deref().unwrap_or("")].join("\u{1f}")
}

/// `card` nimmt gezogene Programme an und legt sie in den Kanal `cid`.
pub fn drop_target(w: &Rc<Win>, card: &gtk::Box, cid: &str) {
    let t = gtk::DropTarget::new(String::static_type(), gtk::gdk::DragAction::MOVE | gtk::gdk::DragAction::COPY);
    let c = card.clone();
    t.connect_enter(move |_, _, _| {
        c.add_css_class("drop");
        gtk::gdk::DragAction::MOVE
    });
    let c = card.clone();
    t.connect_leave(move |_| c.remove_css_class("drop"));
    let (w2, c, id) = (w.clone(), card.clone(), cid.to_string());
    t.connect_drop(move |_, value, _, _| {
        c.remove_css_class("drop");
        let Ok(v) = value.get::<String>() else { return false };
        let p: Vec<&str> = v.split('\u{1f}').collect();
        if p.len() < 4 || p[3] == id {
            return false;
        }
        move_stream(&w2, p[0], p[1], p[2], &id);
        true
    });
    card.add_controller(t);
}

fn move_stream(w: &Rc<Win>, index: &str, key: &str, app: &str, cid: &str) {
    if cid.is_empty() {
        return;
    }
    w.touch("streams");
    let (i, k, c) = (index.to_string(), key.to_string(), cid.to_string());
    let msg = tr!("{app} → channel “{channel}” (remembered)", app = app, channel = cid);
    w.job(move |h| h.mixer.move_stream(&i, &c, &k), move |w, _| {
        w.status(&msg);
        w.release_all();
    });
}

/// Programm mit Kanal: [Symbol] Spotify │ → Musik. Klick öffnet die Kanalauswahl, Ziehen verschiebt.
fn app_chip(w: &Rc<Win>, s: &Stream, snap: &Snapshot) -> gtk::Button {
    let ch = s.channel.as_deref().and_then(|c| snap.channel(c));
    let (glyph, color) = match ch {
        Some(c) => channel_look(&c.channel),
        None => ("speaker".to_string(), theme::FAINT.to_string()),
    };
    let target = ch.map(|c| c.channel.name.clone()).unwrap_or_else(not_in_mixer);
    let b = gtk::Button::new();
    b.add_css_class("app-chip");
    b.set_halign(Align::Start);
    let row = hbox(0);
    row.append(&theme::tile(&glyph, 24, &color, 0.3, "#ffffff"));
    let name = label(&s.app, &[]);
    name.set_ellipsize(gtk::pango::EllipsizeMode::End);
    name.set_max_width_chars(20);
    widgets::margins(&name, 0, 10, 0, 8);
    row.append(&name);
    let line = widgets::sep();
    line.set_size_request(1, 18);
    line.set_valign(Align::Center);
    row.append(&line);
    let arrow = theme::glyph("arrow", 11, theme::MUTED);
    widgets::margins(&arrow, 0, 5, 0, 9);
    row.append(&arrow);
    row.append(&label(&target, &["muted", "small"]));
    b.set_child(Some(&row));
    let (w2, st) = (w.clone(), s.clone());
    b.connect_clicked(move |b| stream_menu(&w2, &st, b));
    let src = gtk::DragSource::new();
    src.set_actions(gtk::gdk::DragAction::MOVE);
    let v = drag_value(s);
    src.connect_prepare(move |_, _, _| Some(gtk::gdk::ContentProvider::for_value(&v.to_value())));
    let w2 = w.clone();
    src.connect_drag_begin(move |_, _| w2.touch("streams"));
    b.add_controller(src);
    b
}

/// Kanal für ein Programm wählen.
fn stream_menu(w: &Rc<Win>, s: &Stream, anchor: &gtk::Button) {
    let snap = w.snap.borrow().clone();
    let mut items = Vec::new();
    for c in snap.channels.iter().filter(|c| c.channel.id != "mic" && c.channel.kind == "apps") {
        let (glyph, _) = channel_look(&c.channel);
        let cur = s.channel.as_deref() == Some(c.channel.id.as_str());
        let (w2, st, cid) = (w.clone(), s.clone(), c.channel.id.clone());
        items.push(widgets::item(&c.channel.name, if cur { "check" } else { &glyph }, move || {
            move_stream(&w2, &st.index, &st.key, &st.app, &cid);
        }));
    }
    widgets::popup(anchor, None, items, 232);
}

fn fill_apps(w: &Rc<Win>, s: &Snapshot) {
    let b = &w.views.apps_box;
    widgets::clear(b);
    let grid = gtk::Grid::new();
    grid.set_column_homogeneous(true);
    grid.set_column_spacing(12);
    grid.set_row_spacing(12);
    b.append(&grid);
    let mut groups: Vec<(Option<&freestream_wave::control::ChannelView>, Vec<&Stream>)> = s.channels.iter()
        .filter(|c| c.channel.id != "mic" && c.channel.kind == "apps")
        .map(|c| (Some(c), s.streams.iter().filter(|x| x.channel.as_deref() == Some(c.channel.id.as_str())).collect()))
        .collect();
    let loose: Vec<&Stream> = s.streams.iter().filter(|x| x.channel.as_deref().map(|c| s.channel(c).is_none()).unwrap_or(true)).collect();
    if !loose.is_empty() {
        groups.push((None, loose));
    }
    for (i, (c, items)) in groups.iter().enumerate() {
        let card = vbox(10);
        card.add_css_class("card");
        card.set_valign(Align::Fill);
        let top = hbox(10);
        match c {
            Some(c) => {
                let (glyph, color) = channel_look(&c.channel);
                top.append(&theme::tile(&glyph, 30, &color, 0.3, "#ffffff"));
                top.append(&label(&c.channel.name, &["bold"]));
                drop_target(w, &card, &c.channel.id);
            }
            None => {
                top.append(&theme::tile("speaker", 30, theme::FAINT, 0.3, "#ffffff"));
                top.append(&label(&not_in_mixer(), &["bold"]));
            }
        }
        top.append(&widgets::spacer());
        top.append(&label(&items.len().to_string(), &["faint", "small"]));
        card.append(&top);
        let list = vbox(6);
        if items.is_empty() {
            list.append(&label(&tr!("No app is playing audio right now."), &["faint", "small"]));
        }
        for st in items {
            list.append(&app_chip(w, st, s));
        }
        card.append(&list);
        grid.attach(&card, (i % 3) as i32, (i / 3) as i32, 1, 1);
    }
}
