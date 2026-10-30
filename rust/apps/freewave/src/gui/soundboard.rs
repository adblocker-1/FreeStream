//! Ansicht „Soundboard“: Kacheln mit Klängen (Klick = abspielen/stoppen), rechts die Einstellungen des
//! gewählten Klangs, unten „Abspielen in“, „Abhören in“ und der Hauptregler.
//!
//! Gespeichert wird in config.json (Schlüssel siehe [`freestream_wave::soundboard`]) über den
//! Arbeits-Thread; abgespielt wird mit pw-play/paplay auf den Sinks der Mixe ([`Player`]).

use std::cell::RefCell;
use std::path::PathBuf;
use std::rc::Rc;
use std::sync::Arc;
use std::time::Duration;

use gtk::prelude::*;
use gtk::Align;
use gtk4 as gtk;

use freestream_core::tr;
use freestream_wave::control::Snapshot;
use freestream_wave::pactl::have;
use freestream_wave::soundboard::{self as sb, Board, Player, Sound, ALL_MIXES};

use super::theme;
use super::widgets::{self, hbox, label, vbox, Fader, Switch};
use super::Win;

const PAD_W: i32 = 104;
const PAD_H: i32 = 88;
const PANEL_W: i32 = 320;

pub struct SoundView {
    pub root: gtk::Box,
    pads: gtk::FlowBox,
    panel: gtk::Box,
    play_btn: gtk::Button,
    monitor_btn: gtk::Button,
    volume: Fader,
    volume_val: gtk::Label,
    pub board: RefCell<Board>,
    player: RefCell<Player>,
    selected: RefCell<Option<String>>,
    playing: RefCell<Vec<String>>,
    pad_btns: RefCell<Vec<(String, gtk::Button)>>,
    /// (Mix-id, Name, Ziel-Sink) aus dem letzten Snapshot
    mixes: RefCell<Vec<(String, String, Option<String>)>>,
    chooser: RefCell<Option<gtk::FileChooserNative>>,
}

/// Knopf „Titel Wert ▾“ der unteren Leiste.
fn picker(title: &str) -> gtk::Button {
    let b = gtk::Button::new();
    b.add_css_class("btn");
    let row = hbox(6);
    row.append(&label(title, &["muted", "small"]));
    let val = label("", &["sb-value"]);
    row.append(&val);
    let chev = theme::glyph("down", 10, theme::MUTED);
    chev.set_margin_start(4);
    row.append(&chev);
    b.set_child(Some(&row));
    b.set_valign(Align::Center);
    b
}

/// Wert eines [`picker`]-Knopfs setzen (zweites Label).
fn set_picker(b: &gtk::Button, text: &str) {
    if let Some(l) = b.child().and_then(|r| r.first_child()).and_then(|c| c.next_sibling())
        .and_then(|c| c.downcast::<gtk::Label>().ok()) {
        if l.text() != text {
            l.set_text(text);
        }
    }
}

impl SoundView {
    pub fn new() -> Self {
        let root = hbox(0);
        let left = vbox(0);
        left.set_hexpand(true);
        let pads = gtk::FlowBox::new();
        pads.set_selection_mode(gtk::SelectionMode::None);
        pads.set_homogeneous(true);
        pads.set_column_spacing(10);
        pads.set_row_spacing(10);
        pads.set_max_children_per_line(30);
        pads.set_valign(Align::Start);
        pads.set_halign(Align::Start);
        widgets::margins(&pads, 4, 22, 18, 22);
        let scroll = widgets::scroll(&pads);
        scroll.set_vexpand(true);
        left.append(&scroll);
        let line = widgets::sep();
        widgets::margins(&line, 0, 22, 0, 22);
        left.append(&line);
        let bar = hbox(10);
        widgets::margins(&bar, 10, 22, 12, 22);
        let play_btn = picker(&tr!("Play in"));
        let monitor_btn = picker(&tr!("Monitor in"));
        bar.append(&play_btn);
        bar.append(&monitor_btn);
        let vsep = widgets::sep();
        vsep.set_size_request(1, 20);
        vsep.set_valign(Align::Center);
        widgets::margins(&vsep, 0, 4, 0, 4);
        bar.append(&vsep);
        bar.append(&theme::glyph("volume_up", 16, theme::MUTED));
        let volume = Fader::new(120, 24, 0.0, 100.0);
        bar.append(&volume.area);
        let volume_val = label("80 %", &["val"]);
        volume_val.set_width_chars(5);
        bar.append(&volume_val);
        left.append(&bar);
        root.append(&left);
        let panel = vbox(0);
        panel.add_css_class("sb-panel");
        // feste Breite: die Inhalte bleiben schmaler, damit die Spalte nicht springt
        panel.set_size_request(PANEL_W, -1);
        panel.set_hexpand(false);
        widgets::margins(&panel, 0, 18, 14, 0);
        root.append(&panel);
        Self {
            root,
            pads,
            panel,
            play_btn,
            monitor_btn,
            volume,
            volume_val,
            board: RefCell::new(Board::default()),
            player: RefCell::new(Player::new()),
            selected: RefCell::new(None),
            playing: RefCell::new(Vec::new()),
            pad_btns: RefCell::new(Vec::new()),
            mixes: RefCell::new(Vec::new()),
            chooser: RefCell::new(None),
        }
    }
}

pub fn init(w: &Rc<Win>) {
    let v = &w.sb;
    let board = w.ctl.host().mixer.with_settings(|s| Board::from_conf(s.data()));
    v.volume.set(board.volume as f64);
    v.volume_val.set_text(&format!("{} %", board.volume));
    *v.board.borrow_mut() = board;
    let w2 = w.clone();
    v.volume.on_change(move |val| {
        let pct = val.round() as i64;
        w2.sb.volume_val.set_text(&format!("{pct} %"));
        w2.sb.board.borrow_mut().volume = pct;
        save(&w2);
    });
    let w2 = w.clone();
    v.play_btn.connect_clicked(move |b| route_menu(&w2, b, true));
    let w2 = w.clone();
    v.monitor_btn.connect_clicked(move |b| route_menu(&w2, b, false));
    fill_pads(w);
    fill_panel(w);
    update_pickers(w);
    // laufende Klänge: Schleifen neu starten, beendete abholen, Kacheln anpassen
    let w2 = w.clone();
    gtk::glib::timeout_add_local(Duration::from_millis(150), move || {
        let now = {
            let mut p = w2.sb.player.borrow_mut();
            p.tick();
            p.playing()
        };
        if *w2.sb.playing.borrow() != now {
            *w2.sb.playing.borrow_mut() = now;
            update_pads(&w2);
        }
        gtk::glib::ControlFlow::Continue
    });
    let w2 = w.clone();
    w.win.connect_destroy(move |_| w2.sb.player.borrow_mut().stop_all());
}

/// Neuer Zustand: Mixe für „Abspielen in“/„Abhören in“ merken.
pub fn apply(w: &Rc<Win>, s: &Arc<Snapshot>) {
    let mixes: Vec<(String, String, Option<String>)> = if s.active {
        s.mixes.iter().map(|m| (m.mix.id.clone(), m.mix.name.clone(), sb::mix_target(&m.mix, &m.devices))).collect()
    } else {
        Vec::new()
    };
    if *w.sb.mixes.borrow() != mixes {
        *w.sb.mixes.borrow_mut() = mixes;
        update_pickers(w);
    }
}

/// Einstellungen gesammelt im Arbeits-Thread speichern.
fn save(w: &Rc<Win>) {
    let b = w.sb.board.borrow().clone();
    w.debounced("soundboard", 300, move |h| {
        h.mixer.with_settings_mut(|s| {
            b.write(s.data_mut());
            if let Err(e) = s.save() {
                log::warn!("config.json: {e}");
            }
        });
        Ok(())
    });
}

fn mix_name(w: &Win, id: &str) -> String {
    if id.is_empty() {
        return tr!("Not selected");
    }
    if id == ALL_MIXES {
        return tr!("All mixes");
    }
    w.sb.mixes.borrow().iter().find(|(m, _, _)| m == id).map(|(_, n, _)| n.clone()).unwrap_or_else(|| id.to_string())
}

fn update_pickers(w: &Rc<Win>) {
    let b = w.sb.board.borrow();
    set_picker(&w.sb.play_btn, &mix_name(w, &b.play));
    set_picker(&w.sb.monitor_btn, &mix_name(w, &b.monitor));
}

/// Menü für „Abspielen in“ (mit „Alle Mixe“) oder „Abhören in“.
fn route_menu(w: &Rc<Win>, anchor: &gtk::Button, play: bool) {
    let cur = {
        let b = w.sb.board.borrow();
        if play { b.play.clone() } else { b.monitor.clone() }
    };
    let mut opts: Vec<(String, String, String)> = vec![(String::new(), tr!("Not selected"), "close".into())];
    for (id, name, _) in w.sb.mixes.borrow().iter() {
        opts.push((id.clone(), name.clone(), "sliders".into()));
    }
    if play {
        opts.push((ALL_MIXES.into(), tr!("All mixes"), "layers".into()));
    }
    let items = opts.into_iter().map(|(id, name, icon)| {
        let w2 = w.clone();
        let icon = if id == cur { "check".to_string() } else { icon };
        widgets::item(&name, &icon, move || {
            {
                let mut b = w2.sb.board.borrow_mut();
                if play {
                    b.play = id.clone();
                } else {
                    b.monitor = id.clone();
                }
            }
            update_pickers(&w2);
            save(&w2);
        })
    }).collect();
    widgets::popup(anchor, None, items, 220);
}

// ------------------------------------------------------------------ Kacheln
fn fill_pads(w: &Rc<Win>) {
    let v = &w.sb;
    while let Some(c) = v.pads.first_child() {
        v.pads.remove(&c);
    }
    let mut btns = Vec::new();
    for s in v.board.borrow().sounds.iter() {
        let b = gtk::Button::new();
        b.add_css_class("sb-pad");
        b.set_size_request(PAD_W, PAD_H);
        b.set_cursor_from_name(Some("pointer"));
        let col = vbox(8);
        col.set_valign(Align::Center);
        let dot = gtk::DrawingArea::new();
        dot.set_content_width(30);
        dot.set_content_height(30);
        dot.set_halign(Align::Center);
        let (w2, id, color) = (w.clone(), s.id.clone(), s.color.clone());
        dot.set_draw_func(move |_, cr, dw, dh| {
            let playing = w2.sb.playing.borrow().contains(&id);
            let (cx, cy, r) = (dw as f64 / 2.0, dh as f64 / 2.0, dw.min(dh) as f64 / 2.0);
            theme::set_source(cr, if color.is_empty() { theme::ACCENT } else { &color });
            cr.arc(cx, cy, r, 0.0, std::f64::consts::TAU);
            let _ = cr.fill();
            theme::paint_glyph(cr, if playing { "stop" } else { "play" }, 14, "#ffffff", cx, cy);
        });
        col.append(&dot);
        let name = label(&s.name, &["small"]);
        name.set_xalign(0.5);
        name.set_ellipsize(gtk::pango::EllipsizeMode::End);
        name.set_max_width_chars(11);
        col.append(&name);
        b.set_child(Some(&col));
        b.set_tooltip_text(Some(&s.name));
        let (w2, id) = (w.clone(), s.id.clone());
        b.connect_clicked(move |_| {
            toggle(&w2, &id);
            if w2.sb.selected.borrow().as_deref() != Some(id.as_str()) {
                *w2.sb.selected.borrow_mut() = Some(id.clone());
                fill_panel(&w2);
            }
            update_pads(&w2);
        });
        v.pads.insert(&b, -1);
        btns.push((s.id.clone(), b));
    }
    // „+ Sound hinzufügen“
    let add = gtk::Button::new();
    add.add_css_class("sb-add");
    add.set_size_request(PAD_W, PAD_H);
    let col = vbox(6);
    col.set_valign(Align::Center);
    let plus = theme::glyph("plus", 16, theme::MUTED);
    plus.set_halign(Align::Center);
    col.append(&plus);
    let l = label(&tr!("Add sound"), &["muted", "small"]);
    l.set_xalign(0.5);
    col.append(&l);
    add.set_child(Some(&col));
    let w2 = w.clone();
    add.connect_clicked(move |_| choose_file(&w2, None));
    v.pads.insert(&add, -1);
    *v.pad_btns.borrow_mut() = btns;
    update_pads(w);
}

fn update_pads(w: &Rc<Win>) {
    let sel = w.sb.selected.borrow().clone();
    let playing = w.sb.playing.borrow().clone();
    for (id, b) in w.sb.pad_btns.borrow().iter() {
        widgets::set_class(b, "sel", sel.as_deref() == Some(id.as_str()));
        widgets::set_class(b, "playing", playing.contains(id));
        b.queue_draw();
        if let Some(d) = b.child().and_then(|c| c.first_child()) {
            d.queue_draw();
        }
    }
}

/// Klang abspielen oder, wenn er läuft, stoppen.
fn toggle(w: &Rc<Win>, id: &str) {
    let v = &w.sb;
    if v.player.borrow().is_playing(id) {
        v.player.borrow_mut().stop(id);
    } else {
        let Some(sound) = v.board.borrow().sound(id).cloned() else { return };
        let board = v.board.borrow().clone();
        let mixes: Vec<(String, Option<String>)> = v.mixes.borrow().iter().map(|(m, _, t)| (m.clone(), t.clone())).collect();
        let targets = sb::targets(&board.play, &board.monitor, &mixes);
        let cmds = sb::commands(&sound, board.volume, &targets, have);
        if cmds.is_empty() {
            w.status(&tr!("No audio player found (install mpv, ffplay or pipewire-bin)"));
            return;
        }
        if !v.player.borrow_mut().play(id, cmds, sound.looping) {
            w.status(&tr!("Audio playback failed: {error}", error = sound.file));
        }
    }
    *v.playing.borrow_mut() = v.player.borrow().playing();
}

/// Datei wählen: neuer Klang (`replace` = None) oder neue Datei für einen Klang.
fn choose_file(w: &Rc<Win>, replace: Option<String>) {
    #[allow(deprecated)]
    let dlg = gtk::FileChooserNative::new(Some(&tr!("Add sound")), Some(&w.win), gtk::FileChooserAction::Open,
                                          Some(&tr!("Open")), Some(&tr!("Cancel")));
    let filter = gtk::FileFilter::new();
    filter.set_name(Some(&tr!("Audio file")));
    filter.add_mime_type("audio/*");
    for e in sb::EXTENSIONS {
        filter.add_pattern(&format!("*.{e}"));
        filter.add_pattern(&format!("*.{}", e.to_uppercase()));
    }
    #[allow(deprecated)]
    dlg.add_filter(&filter);
    let w2 = w.clone();
    #[allow(deprecated)]
    dlg.connect_response(move |d, resp| {
        let path = if resp == gtk::ResponseType::Accept { d.file().and_then(|f| f.path()) } else { None };
        *w2.sb.chooser.borrow_mut() = None;
        if let Some(p) = path {
            add_file(&w2, p, replace.clone());
        }
    });
    #[allow(deprecated)]
    dlg.show();
    *w.sb.chooser.borrow_mut() = Some(dlg);
}

/// Datei in den Datenordner kopieren (im Hintergrund) und eintragen.
fn add_file(w: &Rc<Win>, src: PathBuf, replace: Option<String>) {
    let w2 = w.clone();
    super::bg(move || sb::import(&src, &sb::sounds_dir()).map_err(|e| e.to_string()), move |r| {
        let dest = match r {
            Ok(d) => d,
            Err(e) => {
                w2.status(&tr!("Error: {error}", error = e));
                return;
            }
        };
        let id = {
            let mut b = w2.sb.board.borrow_mut();
            match replace.as_deref().and_then(|id| b.sound_mut(id)) {
                Some(s) => {
                    s.file = dest.to_string_lossy().into_owned();
                    s.id.clone()
                }
                None => {
                    let s = sb::new_sound(&sb::new_id(), &dest, &b.sounds);
                    let id = s.id.clone();
                    b.sounds.push(s);
                    id
                }
            }
        };
        *w2.sb.selected.borrow_mut() = Some(id);
        save(&w2);
        fill_pads(&w2);
        fill_panel(&w2);
    });
}

// ------------------------------------------------------------------ Einstellungen rechts
fn fill_panel(w: &Rc<Win>) {
    let v = &w.sb;
    widgets::clear(&v.panel);
    let sel = v.selected.borrow().clone();
    let Some(sound) = sel.as_deref().and_then(|id| v.board.borrow().sound(id).cloned()) else {
        let empty = vbox(10);
        empty.set_vexpand(true);
        empty.set_valign(Align::Center);
        empty.set_halign(Align::Center);
        let g = theme::glyph("music", 22, theme::MUTED);
        g.set_halign(Align::Center);
        empty.append(&g);
        let l = widgets::wrap(&tr!("Select a sound to edit its settings"), &["small"], 30);
        l.set_justify(gtk::Justification::Center);
        l.set_xalign(0.5);
        empty.append(&l);
        v.panel.append(&empty);
        return;
    };
    let body = vbox(0);
    widgets::margins(&body, 20, 18, 18, 18);
    body.set_vexpand(true);
    v.panel.append(&body);
    // Name
    widgets::field(&body, &tr!("Name"));
    let name = widgets::entry(&sound.name);
    name.set_width_chars(10);
    name.set_margin_bottom(16);
    let (w2, id) = (w.clone(), sound.id.clone());
    name.connect_changed(move |e| {
        let text = e.text().to_string();
        if let Some(s) = w2.sb.board.borrow_mut().sound_mut(&id) {
            s.name = text.clone();
        }
        if let Some((_, b)) = w2.sb.pad_btns.borrow().iter().find(|(i, _)| *i == id) {
            b.set_tooltip_text(Some(&text));
            if let Some(l) = b.child().and_then(|c| c.last_child()).and_then(|c| c.downcast::<gtk::Label>().ok()) {
                l.set_text(&text);
            }
        }
        save(&w2);
    });
    body.append(&name);
    // Datei
    widgets::field(&body, &tr!("Audio file"));
    let frow = hbox(8);
    frow.set_margin_bottom(16);
    let fname = std::path::Path::new(&sound.file).file_name().map(|f| f.to_string_lossy().into_owned())
        .unwrap_or_else(|| sound.file.clone());
    let fl = label(&fname, &["muted", "small"]);
    fl.set_ellipsize(gtk::pango::EllipsizeMode::Middle);
    fl.set_max_width_chars(14);
    fl.set_hexpand(true);
    fl.set_tooltip_text(Some(&sound.file));
    frow.append(&fl);
    let pick = widgets::button(&tr!("Choose file"), Some("folder"), "");
    let (w2, id) = (w.clone(), sound.id.clone());
    pick.connect_clicked(move |_| choose_file(&w2, Some(id.clone())));
    frow.append(&pick);
    body.append(&frow);
    // Farbe
    widgets::field(&body, &tr!("Colour"));
    let colors = hbox(8);
    colors.set_margin_bottom(16);
    for c in sb::COLORS {
        let sw = gtk::DrawingArea::new();
        sw.set_content_width(24);
        sw.set_content_height(24);
        sw.set_cursor_from_name(Some("pointer"));
        let current = sound.color.eq_ignore_ascii_case(c);
        sw.set_draw_func(move |_, cr, dw, dh| {
            let (cx, cy) = (dw as f64 / 2.0, dh as f64 / 2.0);
            if current {
                theme::set_source(cr, theme::FG);
                cr.arc(cx, cy, 11.5, 0.0, std::f64::consts::TAU);
                let _ = cr.fill();
            }
            theme::set_source(cr, c);
            cr.arc(cx, cy, if current { 9.0 } else { 10.0 }, 0.0, std::f64::consts::TAU);
            let _ = cr.fill();
        });
        let (w2, id) = (w.clone(), sound.id.clone());
        widgets::on_click(&sw, move || {
            if let Some(s) = w2.sb.board.borrow_mut().sound_mut(&id) {
                s.color = c.to_string();
            }
            save(&w2);
            fill_pads(&w2);
            fill_panel(&w2);
        });
        colors.append(&sw);
    }
    body.append(&colors);
    // Lautstärke
    widgets::field(&body, &tr!("Volume"));
    let vrow = hbox(10);
    vrow.set_margin_bottom(16);
    let fader = Fader::new(160, 24, 0.0, 100.0);
    fader.area.set_hexpand(true);
    fader.set(sound.volume as f64);
    let val = label(&format!("{} %", sound.volume), &["val"]);
    val.set_width_chars(5);
    let (w2, id, v2) = (w.clone(), sound.id.clone(), val.clone());
    fader.on_change(move |x| {
        let pct = x.round() as i64;
        v2.set_text(&format!("{pct} %"));
        if let Some(s) = w2.sb.board.borrow_mut().sound_mut(&id) {
            s.volume = pct;
        }
        save(&w2);
    });
    vrow.append(&fader.area);
    vrow.append(&val);
    body.append(&vrow);
    // Schleife
    let lrow = hbox(10);
    lrow.set_margin_bottom(20);
    let ll = label(&tr!("Loop"), &[]);
    ll.set_hexpand(true);
    lrow.append(&ll);
    let sw = Switch::new();
    sw.set(sound.looping);
    let (w2, id) = (w.clone(), sound.id.clone());
    sw.on_toggle(move |on| {
        if let Some(s) = w2.sb.board.borrow_mut().sound_mut(&id) {
            s.looping = on;
        }
        w2.sb.player.borrow_mut().set_looping(&id, on);
        save(&w2);
    });
    lrow.append(&sw.area);
    body.append(&lrow);
    let spacer = vbox(0);
    spacer.set_vexpand(true);
    body.append(&spacer);
    // Löschen
    let del = widgets::button(&tr!("Delete sound"), Some("trash"), "danger-btn");
    del.set_halign(Align::Fill);
    let (w2, id) = (w.clone(), sound.id.clone());
    del.connect_clicked(move |_| delete(&w2, &id));
    body.append(&del);
}

fn delete(w: &Rc<Win>, id: &str) {
    w.sb.player.borrow_mut().stop(id);
    let gone: Option<Sound> = sb::remove(&mut w.sb.board.borrow_mut(), id, &sb::sounds_dir());
    if gone.is_some() {
        *w.sb.selected.borrow_mut() = None;
        save(w);
        fill_pads(w);
        fill_panel(w);
    }
}
