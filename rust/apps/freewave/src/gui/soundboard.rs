//! Ansicht „Soundboard“: Kacheln mit Klängen (Klick = abspielen/stoppen, Rechtsklick = bearbeiten),
//! rechts die Einstellungen des gewählten Klangs, unten „Abspielen in“, „Abhören in“, wie viele Klänge
//! spielen und der Hauptregler; „Stop all“ (oder Esc) steht oben rechts in der Kopfzeile.
//!
//! Jede Kachel zeigt die Wellenform ihres Klangs (beim Abspielen füllt sie sich als Fortschritt), Dauer
//! und Lautstärke bzw. die verstrichene Zeit, und unten Marken: auf welcher FreeDeck-Taste der Klang liegt
//! und ob er in der Schleife spielt. Rechts lässt sich der Klang zuschneiden (Griffe in der Wellenform
//! ziehen oder Anfang/Ende eintippen) – wie `freestream/soundview.py`.
//!
//! Gespeichert wird in config.json (Schlüssel siehe [`freestream_wave::soundboard`]) über den
//! Arbeits-Thread; abgespielt wird mit pw-play/paplay auf den Sinks der Mixe ([`Player`]).

use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::path::PathBuf;
use std::rc::Rc;
use std::sync::Arc;
use std::time::{Duration, Instant};

use gtk::prelude::*;
use gtk::Align;
use gtk4 as gtk;

use freestream_core::i18n::ngettext;
use freestream_core::tr;
use freestream_wave::control::Snapshot;
use freestream_wave::pactl::have;
use freestream_wave::soundboard::{self as sb, Board, Player, Sound, WavInfo, ALL_MIXES, PEAKS};

use super::theme;
use super::widgets::{self, hbox, label, vbox, Fader, Switch};
use super::Win;

const PAD_W: i32 = 160;
const PAD_H: i32 = 150;
const PANEL_W: i32 = 300;
const TRIM_H: i32 = 74;
/// Tasten auf dem Deck so oft neu lesen, solange das Soundboard zu sehen ist
const KEYS_EVERY: Duration = Duration::from_secs(5);

/// Eine Kachel: Fläche (gezeichnet) und darüber Name, Zeit und Marken.
struct Pad {
    id: String,
    button: gtk::Button,
    area: gtk::DrawingArea,
    name: gtk::Label,
    sub: gtk::Label,
    key: gtk::Box,
    key_label: gtk::Label,
    looping: gtk::Box,
}

/// Zuschnitt im Feld rechts.
#[derive(Default)]
struct TrimState {
    info: Option<Arc<WavInfo>>,
    color: String,
    start: f64,
    end: f64,
    playhead: Option<f64>,
    /// gezogener Griff: true = Anfang
    grab: Option<bool>,
    grab_x: f64,
}

/// Teile der Einstellungen, die sich ohne Neuaufbau ändern.
struct Side {
    id: String,
    trim: gtk::DrawingArea,
    st: Rc<RefCell<TrimState>>,
    start: Option<gtk::Entry>,
    end: Option<gtk::Entry>,
    plays: gtk::Label,
    trigger_name: gtk::Label,
    trigger_value: gtk::Label,
    play: gtk::Button,
}

pub struct SoundView {
    pub root: gtk::Box,
    /// „Stop all“ – steht in der Kopfzeile des Fensters
    pub stop_btn: gtk::Button,
    pads: gtk::FlowBox,
    panel: gtk::Box,
    play_btn: gtk::Button,
    monitor_btn: gtk::Button,
    volume: Fader,
    volume_val: gtk::Label,
    count: gtk::Box,
    count_label: gtk::Label,
    pub board: RefCell<Board>,
    player: RefCell<Player>,
    selected: RefCell<Option<String>>,
    playing: RefCell<Vec<String>>,
    pad_list: RefCell<Vec<Pad>>,
    /// Datei -> Wellenform und Länge (None: kein WAV)
    info: RefCell<HashMap<String, Option<Arc<WavInfo>>>>,
    /// Klang-id -> (Seite, Taste) auf dem Deck
    keys: RefCell<HashMap<String, (usize, usize)>>,
    keys_at: Cell<Option<Instant>>,
    side: RefCell<Option<Side>>,
    /// (Mix-id, Name, Ziel-Sink) aus dem letzten Snapshot
    mixes: RefCell<Vec<(String, String, Option<String>)>>,
    chooser: RefCell<Option<gtk::FileChooserNative>>,
}

/// Knopf „Titel Wert ▾“ der unteren Leiste.
fn picker(title: &str) -> gtk::Button {
    let b = gtk::Button::new();
    b.add_css_class("btn");
    b.add_css_class("sb-pick");
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

/// Kleine Marke unten auf der Kachel (Taste, Schleife).
fn badge(icon: &str, text: Option<&gtk::Label>) -> gtk::Box {
    let b = hbox(5);
    b.add_css_class("sb-badge");
    b.append(&theme::glyph(icon, 12, "#e6e6e6"));
    if let Some(l) = text {
        b.append(l);
    }
    b.set_valign(Align::Center);
    b
}

/// „Key 2“ (kurz, Seite 1) bzw. „Page 2 · Key 3“.
fn key_text(pos: (usize, usize), short: bool) -> String {
    if short && pos.0 == 1 {
        return tr!("Key {n}", n = pos.1);
    }
    tr!("Page {page} · Key {key}", page = pos.0, key = pos.1)
}

/// Wellenform auf n Balken bringen (Höchstwert je Abschnitt).
fn bars(peaks: &[f32], n: usize) -> Vec<f64> {
    let m = peaks.len();
    if m == 0 {
        return Vec::new();
    }
    (0..n).map(|i| {
        let a = i * m / n;
        let b = ((i + 1) * m / n).max(a + 1).min(m);
        peaks[a..b].iter().cloned().fold(0.0f32, f32::max) as f64
    }).collect()
}

/// Spielposition als Anteil der ganzen Datei (für die Wellenform).
fn progress(sound: &Sound, info: &WavInfo, elapsed: Option<Duration>) -> Option<f64> {
    let e = elapsed?.as_secs_f64();
    if info.duration <= 0.0 {
        return None;
    }
    let (a, b) = sb::trim_range(sound, info.duration);
    Some((a + e.min(b - a)) / info.duration)
}

/// Schere (Zuschnitt): zwei Ringe, zwei Klingen.
fn paint_scissors(cr: &gtk::cairo::Context, x: f64, y: f64, s: f64, color: &str) {
    theme::set_source(cr, color);
    cr.set_line_width((s * 0.1).max(1.0));
    for cy in [0.26, 0.74] {
        cr.new_sub_path();
        cr.arc(x + s * 0.26, y + s * cy, s * 0.15, 0.0, std::f64::consts::TAU);
    }
    cr.move_to(x + s * 0.38, y + s * 0.34);
    cr.line_to(x + s * 0.94, y + s * 0.78);
    cr.move_to(x + s * 0.38, y + s * 0.66);
    cr.line_to(x + s * 0.94, y + s * 0.22);
    let _ = cr.stroke();
}

impl SoundView {
    pub fn new() -> Self {
        let root = hbox(0);
        let left = vbox(0);
        left.set_hexpand(true);
        let pads = gtk::FlowBox::new();
        pads.set_selection_mode(gtk::SelectionMode::None);
        pads.set_homogeneous(true);
        pads.set_column_spacing(12);
        pads.set_row_spacing(12);
        pads.set_max_children_per_line(30);
        pads.set_valign(Align::Start);
        pads.set_halign(Align::Start);
        widgets::margins(&pads, 4, 16, 12, 22);
        let scroll = widgets::scroll(&pads);
        scroll.set_vexpand(true);
        left.append(&scroll);
        let bar = hbox(10);
        bar.add_css_class("sb-bar");
        widgets::margins(&bar, 0, 16, 14, 22);
        let play_btn = picker(&tr!("Play in"));
        let monitor_btn = picker(&tr!("Monitor in"));
        bar.append(&play_btn);
        bar.append(&monitor_btn);
        bar.append(&widgets::spacer());
        let count = hbox(6);
        let dot = hbox(0);
        dot.add_css_class("sb-dot");
        dot.set_valign(Align::Center);
        count.append(&dot);
        let count_label = label("", &["sb-count"]);
        count.append(&count_label);
        count.set_margin_end(8);
        count.set_visible(false);
        bar.append(&count);
        bar.append(&theme::glyph("volume_up", 16, theme::MUTED));
        let volume = Fader::new(110, 24, 0.0, 100.0);
        bar.append(&volume.area);
        let volume_val = label("80 %", &["sb-vol"]);
        volume_val.set_width_chars(5);
        volume_val.set_xalign(1.0);
        bar.append(&volume_val);
        left.append(&bar);
        root.append(&left);
        let panel = vbox(0);
        panel.add_css_class("sb-panel");
        // feste Breite: die Inhalte bleiben schmaler, damit die Spalte nicht springt
        panel.set_size_request(PANEL_W, -1);
        panel.set_hexpand(false);
        widgets::margins(&panel, 0, 22, 14, 0);
        root.append(&panel);
        // „Stop all  Esc“
        let stop_btn = gtk::Button::new();
        stop_btn.add_css_class("btn");
        stop_btn.add_css_class("sb-stop");
        stop_btn.add_css_class("tall");
        let row = hbox(8);
        row.append(&theme::glyph("stop", 13, theme::DANGER));
        row.append(&gtk::Label::new(Some(&tr!("Stop all"))));
        let kbd = label("Esc", &["sb-kbd"]);
        kbd.set_valign(Align::Center);
        row.append(&kbd);
        stop_btn.set_child(Some(&row));
        stop_btn.set_valign(Align::Center);
        Self {
            root,
            stop_btn,
            pads,
            panel,
            play_btn,
            monitor_btn,
            volume,
            volume_val,
            count,
            count_label,
            board: RefCell::new(Board::default()),
            player: RefCell::new(Player::new()),
            selected: RefCell::new(None),
            playing: RefCell::new(Vec::new()),
            pad_list: RefCell::new(Vec::new()),
            info: RefCell::new(HashMap::new()),
            keys: RefCell::new(HashMap::new()),
            keys_at: Cell::new(None),
            side: RefCell::new(None),
            mixes: RefCell::new(Vec::new()),
            chooser: RefCell::new(None),
        }
    }

    fn info_of(&self, file: &str) -> Option<Arc<WavInfo>> {
        self.info.borrow().get(file).cloned().flatten()
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
    let w2 = w.clone();
    v.stop_btn.connect_clicked(move |_| stop_all(&w2));
    // Esc: alles stoppen (nur im Soundboard und nur, wenn etwas spielt)
    let keys = gtk::EventControllerKey::new();
    let w2 = w.clone();
    keys.connect_key_pressed(move |_, key, _, _| {
        if key == gtk::gdk::Key::Escape && w2.view.borrow().as_str() == "soundboard"
            && !w2.sb.playing.borrow().is_empty() {
            stop_all(&w2);
            return gtk::glib::Propagation::Stop;
        }
        gtk::glib::Propagation::Proceed
    });
    w.win.add_controller(keys);
    fill_pads(w);
    fill_panel(w);
    update_pickers(w);
    load_info(w);
    load_keys(w);
    // laufende Klänge: Schleifen neu starten, beendete abholen, Kacheln und Fortschritt anpassen
    let w2 = w.clone();
    gtk::glib::timeout_add_local(Duration::from_millis(100), move || {
        let now = {
            let mut p = w2.sb.player.borrow_mut();
            p.tick();
            p.playing()
        };
        if *w2.sb.playing.borrow() != now {
            *w2.sb.playing.borrow_mut() = now.clone();
            update_pads(&w2);
        } else if !now.is_empty() {
            update_progress(&w2);
        }
        if w2.view.borrow().as_str() == "soundboard"
            && w2.sb.keys_at.get().is_none_or(|t| t.elapsed() >= KEYS_EVERY) {
            load_keys(&w2);
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

/// Wellenformen und Längen im Hintergrund lesen (zwischengespeichert je Datei).
fn load_info(w: &Rc<Win>) {
    let files: Vec<String> = {
        let known = w.sb.info.borrow();
        w.sb.board.borrow().sounds.iter().map(|s| s.file.clone()).filter(|f| !known.contains_key(f)).collect()
    };
    if files.is_empty() {
        return;
    }
    let w2 = w.clone();
    super::bg(move || files.into_iter().map(|f| {
        let i = sb::analyze(&f, PEAKS);
        (f, i)
    }).collect::<Vec<_>>(), move |res| {
        let sel_file = w2.sb.selected.borrow().as_deref().and_then(|id| w2.sb.board.borrow().sound(id).map(|s| s.file.clone()));
        let mut panel = false;
        for (f, i) in res {
            panel |= sel_file.as_deref() == Some(f.as_str());
            w2.sb.info.borrow_mut().insert(f, i);
        }
        update_pads(&w2);
        if panel {
            fill_panel(&w2);
        }
    });
}

/// Welche FreeDeck-Taste welchen Klang spielt (im Hintergrund).
fn load_keys(w: &Rc<Win>) {
    w.sb.keys_at.set(Some(Instant::now()));
    let w2 = w.clone();
    super::bg(|| sb::deck_keys(None), move |keys| {
        if *w2.sb.keys.borrow() != keys {
            *w2.sb.keys.borrow_mut() = keys;
            update_pads(&w2);
            update_trigger(&w2);
        }
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
/// Fläche einer Kachel: Hintergrund, Rand, runder Knopf, Wellenform mit Fortschritt.
fn draw_pad(w: &Win, id: &str, hover: bool, cr: &gtk::cairo::Context, dw: f64, dh: f64) {
    let v = &w.sb;
    let Some(s) = v.board.borrow().sound(id).cloned() else { return };
    let playing = v.playing.borrow().iter().any(|p| p == id);
    let selected = v.selected.borrow().as_deref() == Some(id);
    let color = if s.color.is_empty() { theme::ACCENT.to_string() } else { s.color.clone() };
    let mut fill = theme::mix(theme::BG, &color, if playing { 0.27 } else { 0.2 });
    if hover {
        fill = theme::mix(&fill, theme::FG, 0.05);
    }
    theme::fill_rounded(cr, 1.0, 1.0, dw - 2.0, dh - 2.0, 12.0, &fill);
    let (out, ow) = if playing {
        (theme::OK.to_string(), 2.0)
    } else if selected {
        (theme::FG.to_string(), 2.0)
    } else {
        (theme::mix(&fill, theme::FG, 0.07), 1.0)
    };
    theme::rounded(cr, 1.0 + ow / 2.0, 1.0 + ow / 2.0, dw - 2.0 - ow, dh - 2.0 - ow, 12.0 - ow / 2.0);
    theme::set_source(cr, &out);
    cr.set_line_width(ow);
    let _ = cr.stroke();
    // runder Knopf oben links: Abspielen bzw. Stoppen
    theme::set_source(cr, &color);
    cr.arc(31.0, 31.0, 17.0, 0.0, std::f64::consts::TAU);
    let _ = cr.fill();
    theme::paint_glyph(cr, if playing { "stop" } else { "play" }, 13, "#ffffff", 31.0, 31.0);
    // Wellenform
    let (x0, y0, x1, y1) = (14.0, 62.0, dw - 14.0, 102.0);
    let boxc = theme::mix(&fill, "#000000", 0.18);
    theme::fill_rounded(cr, x0, y0, x1 - x0, y1 - y0, 6.0, &boxc);
    let (cy, half) = ((y0 + y1) / 2.0, (y1 - y0) / 2.0 - 4.0);
    let Some(info) = v.info_of(&s.file).filter(|i| !i.peaks.is_empty()) else {
        // kein WAV: neutraler Platzhalter
        theme::fill_rounded(cr, x0 + 8.0, cy - 2.0, x1 - x0 - 16.0, 4.0, 2.0, &theme::mix(&boxc, theme::FG, 0.2));
        return;
    };
    let dur = info.duration.max(0.001);
    let (a, b) = sb::trim_range(&s, dur);
    let prog = if playing { progress(&s, &info, v.player.borrow().elapsed(id)) } else { None };
    let (on, off, cut) = ("#ffffff".to_string(), theme::mix(&boxc, theme::FG, 0.36), theme::mix(&boxc, theme::FG, 0.12));
    let step = 3.0;
    let n = ((x1 - x0 - 12.0) / step) as usize;
    for (i, p) in bars(&info.peaks, n).into_iter().enumerate() {
        let pos = (i as f64 + 0.5) / n as f64;
        let col = if pos < a / dur || pos > b / dur {
            &cut
        } else if prog.is_some_and(|g| pos <= g) {
            &on
        } else {
            &off
        };
        let hh = (p * half).max(1.0);
        theme::set_source(cr, col);
        cr.rectangle(x0 + 6.0 + i as f64 * step, cy - hh, 2.0, 2.0 * hh);
        let _ = cr.fill();
    }
}

/// Zeile unter dem Namen: „0:03 / 0:07“ (spielt) bzw. „0:04 · 95 %“.
fn sub_text(w: &Win, s: &Sound, playing: bool) -> String {
    let info = w.sb.info_of(&s.file);
    let len = info.as_ref().map(|i| sb::play_length(s, i));
    match (len, playing) {
        (Some(l), true) => {
            let e = w.sb.player.borrow().elapsed(&s.id).map(|d| d.as_secs_f64()).unwrap_or(0.0).min(l);
            format!("{} / {}", sb::fmt_time(e), sb::fmt_time(l))
        }
        (Some(l), false) => format!("{} · {} %", sb::fmt_time(l), s.volume),
        _ => format!("{} %", s.volume),
    }
}

fn fill_pads(w: &Rc<Win>) {
    let v = &w.sb;
    while let Some(c) = v.pads.first_child() {
        v.pads.remove(&c);
    }
    let mut list = Vec::new();
    for s in v.board.borrow().sounds.iter() {
        let b = gtk::Button::new();
        b.add_css_class("sb-pad");
        b.set_cursor_from_name(Some("pointer"));
        let ov = gtk::Overlay::new();
        let area = gtk::DrawingArea::new();
        area.set_content_width(PAD_W);
        area.set_content_height(PAD_H);
        let hover = Rc::new(Cell::new(false));
        let (w2, id, h2) = (w.clone(), s.id.clone(), hover.clone());
        area.set_draw_func(move |_, cr, dw, dh| draw_pad(&w2, &id, h2.get(), cr, dw as f64, dh as f64));
        ov.set_child(Some(&area));
        let top = vbox(0);
        top.set_can_target(false);
        let text = vbox(2);
        widgets::margins(&text, 15, 12, 0, 58);
        let name = label(&s.name, &["sb-name"]);
        name.set_ellipsize(gtk::pango::EllipsizeMode::End);
        name.set_max_width_chars(1);
        name.set_hexpand(true);
        text.append(&name);
        let sub = label("", &["sb-sub"]);
        text.append(&sub);
        top.append(&text);
        let fill = vbox(0);
        fill.set_vexpand(true);
        top.append(&fill);
        let badges = hbox(5);
        widgets::margins(&badges, 0, 14, 13, 14);
        let key_label = label("", &[]);
        let key = badge("deck", Some(&key_label));
        badges.append(&key);
        badges.append(&widgets::spacer());
        let looping = badge("repeat", None);
        badges.append(&looping);
        badges.set_size_request(-1, 21);
        top.append(&badges);
        ov.add_overlay(&top);
        b.set_child(Some(&ov));
        b.set_tooltip_text(Some(&s.name));
        let motion = gtk::EventControllerMotion::new();
        let (a2, h2) = (area.clone(), hover.clone());
        motion.connect_enter(move |_, _, _| {
            h2.set(true);
            a2.queue_draw();
        });
        let (a2, h2) = (area.clone(), hover.clone());
        motion.connect_leave(move |_| {
            h2.set(false);
            a2.queue_draw();
        });
        b.add_controller(motion);
        let (w2, id) = (w.clone(), s.id.clone());
        b.connect_clicked(move |_| {
            toggle(&w2, &id);
            select(&w2, &id);
            update_pads(&w2);
        });
        let (w2, id) = (w.clone(), s.id.clone());
        widgets::on_right_click(&b, move |_, _| select(&w2, &id));
        v.pads.insert(&b, -1);
        list.push(Pad { id: s.id.clone(), button: b, area, name, sub, key, key_label, looping });
    }
    // „+ Sound hinzufügen“
    let add = gtk::Button::new();
    add.add_css_class("sb-add");
    add.set_size_request(PAD_W, PAD_H);
    let col = vbox(8);
    col.set_valign(Align::Center);
    let plus = theme::glyph("plus", 22, theme::MUTED);
    plus.set_halign(Align::Center);
    col.append(&plus);
    let l = label(&tr!("Add sound"), &["muted"]);
    l.set_xalign(0.5);
    col.append(&l);
    add.set_child(Some(&col));
    let w2 = w.clone();
    add.connect_clicked(move |_| choose_file(&w2, None));
    v.pads.insert(&add, -1);
    *v.pad_list.borrow_mut() = list;
    update_pads(w);
}

/// Kacheln an Auswahl, Wiedergabe, Tasten und Wellenform anpassen; dazu Zähler und „Stop all“.
fn update_pads(w: &Rc<Win>) {
    let v = &w.sb;
    let playing = v.playing.borrow().clone();
    let keys = v.keys.borrow().clone();
    for p in v.pad_list.borrow().iter() {
        let Some(s) = v.board.borrow().sound(&p.id).cloned() else { continue };
        let on = playing.contains(&p.id);
        if p.name.text() != s.name {
            p.name.set_text(&s.name);
            p.button.set_tooltip_text(Some(&s.name));
        }
        p.sub.set_text(&sub_text(w, &s, on));
        widgets::set_class(&p.sub, "playing", on);
        match keys.get(&p.id) {
            Some(pos) => {
                p.key_label.set_text(&key_text(*pos, true));
                p.key.set_visible(true);
            }
            None => p.key.set_visible(false),
        }
        p.looping.set_visible(s.looping);
        p.area.queue_draw();
    }
    let n = playing.len();
    v.count.set_visible(n > 0);
    v.count_label.set_text(&ngettext("{count} playing", "{count} playing", n as i64).replace("{count}", &n.to_string()));
    widgets::set_class(&v.stop_btn, "idle", n == 0);
    update_progress(w);
    if let Some(side) = v.side.borrow().as_ref() {
        let on = playing.contains(&side.id);
        widgets::set_button_text(&side.play, &if on { tr!("Stop") } else { tr!("Play") });
        if let Some(img) = side.play.child().and_then(|r| r.first_child()).and_then(|c| c.downcast::<gtk::Image>().ok()) {
            img.set_paintable(theme::glyph(if on { "stop" } else { "play" }, 16, "#ffffff").paintable().as_ref());
        }
    }
}

/// Fortschritt der laufenden Klänge: Zeit unter dem Namen, Wellenform, Abspielpunkt im Zuschnitt.
fn update_progress(w: &Rc<Win>) {
    let v = &w.sb;
    let playing = v.playing.borrow().clone();
    for p in v.pad_list.borrow().iter().filter(|p| playing.contains(&p.id)) {
        if let Some(s) = v.board.borrow().sound(&p.id) {
            p.sub.set_text(&sub_text(w, s, true));
        }
        p.area.queue_draw();
    }
    if let Some(side) = v.side.borrow().as_ref() {
        let head = if playing.contains(&side.id) {
            let b = v.board.borrow();
            let info = v.info_of(&b.sound(&side.id).map(|s| s.file.clone()).unwrap_or_default());
            b.sound(&side.id).zip(info).and_then(|(s, i)| progress(s, &i, v.player.borrow().elapsed(&side.id))
                .map(|g| g * i.duration))
        } else {
            None
        };
        let mut st = side.st.borrow_mut();
        if st.playhead != head {
            st.playhead = head;
            side.trim.queue_draw();
        }
    }
}

fn select(w: &Rc<Win>, id: &str) {
    if w.sb.selected.borrow().as_deref() != Some(id) {
        *w.sb.selected.borrow_mut() = Some(id.to_string());
        fill_panel(w);
        update_pads(w);
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

fn stop_all(w: &Rc<Win>) {
    w.sb.player.borrow_mut().stop_all();
    w.sb.playing.borrow_mut().clear();
    update_pads(w);
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
                    // neue Datei: der Zuschnitt der alten passt nicht mehr
                    s.start = None;
                    s.end = None;
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
        load_info(&w2);
    });
}

// ------------------------------------------------------------------ Einstellungen rechts
/// Wellenform des Zuschnitts mit den beiden Griffen und dem Abspielpunkt.
fn draw_trim(st: &TrimState, cr: &gtk::cairo::Context, w: f64, h: f64) {
    let bg = theme::PANEL2;
    theme::fill_rounded(cr, 0.0, 0.0, w, h, 8.0, bg);
    theme::rounded(cr, 0.5, 0.5, w - 1.0, h - 1.0, 8.0);
    theme::set_source(cr, theme::BORDER);
    cr.set_line_width(1.0);
    let _ = cr.stroke();
    let cy = h / 2.0;
    let Some(info) = st.info.as_ref().filter(|i| !i.peaks.is_empty() && i.duration > 0.0) else {
        theme::fill_rounded(cr, 12.0, cy - 3.0, w - 24.0, 6.0, 3.0, &theme::mix(bg, theme::FG, 0.16));
        return;
    };
    let x = |t: f64| 10.0 + (w - 20.0) * t / info.duration;
    let (xs, xe) = (x(st.start), x(st.end));
    // abgeschnittene Teile dunkler
    let dark = theme::mix(bg, "#000000", 0.35);
    let _ = cr.save();
    theme::rounded(cr, 1.0, 1.0, w - 2.0, h - 2.0, 7.5);
    cr.clip();
    theme::set_source(cr, &dark);
    cr.rectangle(0.0, 0.0, xs, h);
    cr.rectangle(xe, 0.0, w - xe, h);
    let _ = cr.fill();
    let _ = cr.restore();
    let (step, half) = (5.0, h / 2.0 - 10.0);
    let n = ((w - 20.0) / step) as usize;
    let off = theme::mix(&dark, &st.color, 0.35);
    for (i, p) in bars(&info.peaks, n).into_iter().enumerate() {
        let bx = 10.0 + i as f64 * step + 1.0;
        theme::set_source(cr, if (xs..=xe).contains(&(bx + 1.5)) { &st.color } else { &off });
        let hh = (p * half).max(1.0);
        cr.rectangle(bx, cy - hh, 3.0, 2.0 * hh);
        let _ = cr.fill();
    }
    if let Some(t) = st.playhead {
        theme::set_source(cr, "#ffffff");
        cr.rectangle(x(t) - 1.0, 4.0, 2.0, h - 8.0);
        let _ = cr.fill();
    }
    for gx in [xs, xe] {
        theme::set_source(cr, theme::WARN);
        cr.rectangle(gx - 2.0, 2.0, 4.0, h - 4.0);
        let _ = cr.fill();
        theme::fill_rounded(cr, gx - 6.0, cy - 9.0, 12.0, 18.0, 3.0, theme::WARN);
    }
}

/// Texte des Zuschnitts (Felder, „plays 0:03.4“) an den Stand der Griffe anpassen.
fn trim_labels(side: &Side) {
    let st = side.st.borrow();
    if let (Some(a), Some(b)) = (&side.start, &side.end) {
        a.set_text(&sb::fmt_precise(st.start, 2));
        b.set_text(&sb::fmt_precise(st.end, 2));
    }
    side.plays.set_text(&tr!("plays {time}", time = sb::fmt_precise(st.end - st.start, 1)));
}

/// Zuschnitt übernehmen: ganz vorn bzw. ganz hinten = nicht zugeschnitten.
fn set_trim(w: &Rc<Win>, id: &str) {
    let (start, end) = {
        let side = w.sb.side.borrow();
        let Some(side) = side.as_ref() else { return };
        let st = side.st.borrow();
        let dur = st.info.as_ref().map(|i| i.duration).unwrap_or(0.0);
        let r = |v: f64| (v * 1000.0).round() / 1000.0;
        ((st.start > 0.005).then(|| r(st.start)), (st.end < dur - 0.005).then(|| r(st.end)))
    };
    let changed = {
        let mut b = w.sb.board.borrow_mut();
        match b.sound_mut(id) {
            Some(s) if (s.start, s.end) != (start, end) => {
                s.start = start;
                s.end = end;
                true
            }
            _ => false,
        }
    };
    if changed {
        save(w);
        update_pads(w);
    }
    if let Some(side) = w.sb.side.borrow().as_ref() {
        trim_labels(side);
    }
}

fn update_trigger(w: &Rc<Win>) {
    let side = w.sb.side.borrow();
    let Some(side) = side.as_ref() else { return };
    match w.sb.keys.borrow().get(&side.id) {
        Some(pos) => {
            side.trigger_name.set_text(&tr!("Deck key"));
            widgets::set_class(&side.trigger_name, "muted", false);
            side.trigger_value.set_text(&key_text(*pos, false));
        }
        None => {
            side.trigger_name.set_text(&tr!("Not on a deck key"));
            widgets::set_class(&side.trigger_name, "muted", true);
            side.trigger_value.set_text("");
        }
    }
}

/// Feld „START 0:00.25“ bzw. „END 0:03.65“.
fn time_field(cap: &str) -> (gtk::Box, gtk::Entry) {
    let b = hbox(6);
    b.add_css_class("sb-tf");
    b.set_hexpand(true);
    let l = label(&cap.to_uppercase(), &["sb-tf-cap"]);
    l.set_valign(Align::Center);
    b.append(&l);
    let e = gtk::Entry::new();
    e.add_css_class("sb-time");
    e.set_width_chars(3);
    e.set_max_width_chars(8);
    e.set_hexpand(true);
    gtk::prelude::EditableExt::set_alignment(&e, 1.0);
    b.append(&e);
    (b, e)
}

fn fill_panel(w: &Rc<Win>) {
    let v = &w.sb;
    widgets::clear(&v.panel);
    *v.side.borrow_mut() = None;
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
    let id = sound.id.clone();
    let body = vbox(0);
    widgets::margins(&body, 18, 18, 18, 18);
    body.set_vexpand(true);
    // in einem Scrollbereich: die Spalte bleibt so breit wie vorgesehen (natürliche Breite der Inhalte
    // zählt nicht) und passt auch in ein niedriges Fenster
    let sc = widgets::scroll(&body);
    sc.set_propagate_natural_width(false);
    v.panel.append(&sc);
    // Kopf
    let head = hbox(12);
    head.set_margin_bottom(16);
    head.append(&theme::tile("wave", 36, if sound.color.is_empty() { theme::ACCENT } else { &sound.color }, 0.25,
                             "#ffffff"));
    head.append(&label(&tr!("Sound settings"), &["sb-head"]));
    body.append(&head);
    // Name
    widgets::field(&body, &tr!("Name"));
    let name = widgets::entry(&sound.name);
    name.set_width_chars(10);
    name.set_margin_bottom(14);
    let (w2, id2) = (w.clone(), id.clone());
    name.connect_changed(move |e| {
        let text = e.text().to_string();
        if let Some(s) = w2.sb.board.borrow_mut().sound_mut(&id2) {
            s.name = text.clone();
        }
        update_pads(&w2);
        save(&w2);
    });
    body.append(&name);
    // Datei: Überschrift mit „Ersetzen …“, darunter Name und Länge
    let frow = hbox(8);
    let fcap = widgets::section(&tr!("Audio file"));
    fcap.set_hexpand(true);
    frow.append(&fcap);
    let pick = widgets::button(&tr!("Replace …"), Some("folder"), "");
    pick.add_css_class("sb-small");
    let (w2, id2) = (w.clone(), id.clone());
    pick.connect_clicked(move |_| choose_file(&w2, Some(id2.clone())));
    frow.append(&pick);
    body.append(&frow);
    let info = v.info_of(&sound.file);
    let nrow = hbox(0);
    widgets::margins(&nrow, 6, 0, 12, 0);
    let fname = std::path::Path::new(&sound.file).file_name().map(|f| f.to_string_lossy().into_owned())
        .unwrap_or_else(|| sound.file.clone());
    let exists = std::path::Path::new(&sound.file).is_file();
    let fl = label(&fname, if exists { &[] } else { &["danger"] });
    fl.set_ellipsize(gtk::pango::EllipsizeMode::Middle);
    fl.set_max_width_chars(22);
    fl.set_tooltip_text(Some(&sound.file));
    nrow.append(&fl);
    if let Some(i) = &info {
        nrow.append(&label(&format!(" · {}", sb::fmt_precise(i.duration, 1)), &["faint", "small"]));
    }
    body.append(&nrow);
    // Zuschnitt
    let trow = hbox(4);
    trow.set_margin_bottom(8);
    let tcap = widgets::section(&tr!("Trim"));
    tcap.set_hexpand(true);
    trow.append(&tcap);
    let plays = label("", &["muted", "small"]);
    if info.is_some() {
        let sc = gtk::DrawingArea::new();
        sc.set_content_width(12);
        sc.set_content_height(12);
        sc.set_valign(Align::Center);
        sc.set_draw_func(|_, cr, dw, _| paint_scissors(cr, 0.0, 0.0, dw as f64, theme::MUTED));
        trow.append(&sc);
        trow.append(&plays);
    }
    body.append(&trow);
    let (start, end) = info.as_ref().map(|i| sb::trim_range(&sound, i.duration)).unwrap_or((0.0, 0.0));
    let st = Rc::new(RefCell::new(TrimState { info: info.clone(), color: sound.color.clone(), start, end,
                                              ..Default::default() }));
    let trim = gtk::DrawingArea::new();
    trim.set_content_height(TRIM_H);
    trim.set_hexpand(true);
    let s2 = st.clone();
    trim.set_draw_func(move |_, cr, dw, dh| draw_trim(&s2.borrow(), cr, dw as f64, dh as f64));
    body.append(&trim);
    let (mut start_e, mut end_e) = (None, None);
    if info.is_some() {
        trim.set_cursor_from_name(Some("ew-resize"));
        let drag = gtk::GestureDrag::new();
        let (s2, t2, w2) = (st.clone(), trim.clone(), w.clone());
        let at = move |st: &mut TrimState, x: f64, width: f64| {
            let dur = st.info.as_ref().map(|i| i.duration).unwrap_or(0.0);
            ((x - 10.0) / (width - 20.0).max(1.0)).clamp(0.0, 1.0) * dur
        };
        drag.connect_drag_begin(move |_, x, _| {
            let mut st = s2.borrow_mut();
            let t = at(&mut st, x, t2.width() as f64);
            st.grab = Some((t - st.start).abs() <= (t - st.end).abs());
            st.grab_x = x;
            drop(st);
            move_handle(&w2, x);
        });
        let (s2, w2) = (st.clone(), w.clone());
        drag.connect_drag_update(move |_, dx, _| {
            let x = s2.borrow().grab_x + dx;
            move_handle(&w2, x);
        });
        let (s2, w2, id2) = (st.clone(), w.clone(), id.clone());
        drag.connect_drag_end(move |_, _, _| {
            if s2.borrow_mut().grab.take().is_some() {
                set_trim(&w2, &id2);
            }
        });
        trim.add_controller(drag);
        let tf = hbox(8);
        tf.set_margin_top(10);
        let (bs, es) = time_field(&tr!("Start"));
        let (be, ee) = time_field(&tr!("End"));
        tf.append(&bs);
        tf.append(&be);
        body.append(&tf);
        for (e, is_start) in [(es.clone(), true), (ee.clone(), false)] {
            let (s2, w2, id2) = (st.clone(), w.clone(), id.clone());
            let commit = Rc::new(move |e: &gtk::Entry| {
                {
                    let mut st = s2.borrow_mut();
                    let dur = st.info.as_ref().map(|i| i.duration).unwrap_or(0.0);
                    if let Some(t) = sb::parse_time(&e.text()) {
                        if is_start {
                            st.start = t.min(st.end - sb::MIN_LENGTH).max(0.0);
                        } else {
                            st.end = t.max(st.start + sb::MIN_LENGTH).min(dur);
                        }
                    }
                }
                if let Some(side) = w2.sb.side.borrow().as_ref() {
                    side.trim.queue_draw();
                }
                set_trim(&w2, &id2);
            });
            let c2 = commit.clone();
            e.connect_activate(move |e| c2(e));
            let focus = gtk::EventControllerFocus::new();
            let e2 = e.clone();
            focus.connect_leave(move |_| commit(&e2));
            e.add_controller(focus);
        }
        start_e = Some(es);
        end_e = Some(ee);
    } else {
        let hint = widgets::wrap(&tr!("Trimming works with WAV files only"), &["muted", "tiny"], 34);
        hint.set_margin_top(6);
        body.append(&hint);
    }
    // Farbe
    let ccap = widgets::field(&body, &tr!("Colour"));
    ccap.set_margin_top(16);
    let colors = gtk::Box::new(gtk::Orientation::Horizontal, 0);
    colors.set_homogeneous(true);
    colors.set_margin_bottom(14);
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
                theme::set_source(cr, theme::PANEL);
                cr.arc(cx, cy, 9.8, 0.0, std::f64::consts::TAU);
                let _ = cr.fill();
            }
            theme::set_source(cr, c);
            cr.arc(cx, cy, if current { 8.5 } else { 10.0 }, 0.0, std::f64::consts::TAU);
            let _ = cr.fill();
        });
        let (w2, id2) = (w.clone(), id.clone());
        widgets::on_click(&sw, move || {
            if let Some(s) = w2.sb.board.borrow_mut().sound_mut(&id2) {
                s.color = c.to_string();
            }
            save(&w2);
            update_pads(&w2);
            fill_panel(&w2);
        });
        colors.append(&sw);
    }
    body.append(&colors);
    // Lautstärke: Überschrift und Wert in einer Zeile, Regler darunter
    let vrow = hbox(8);
    let vcap = widgets::section(&tr!("Volume"));
    vcap.set_hexpand(true);
    vrow.append(&vcap);
    let val = label(&format!("{} %", sound.volume), &["sb-value"]);
    vrow.append(&val);
    body.append(&vrow);
    let fader = Fader::new(160, 24, 0.0, 100.0);
    fader.area.set_hexpand(true);
    fader.area.set_margin_top(6);
    fader.area.set_margin_bottom(12);
    fader.set(sound.volume as f64);
    let (w2, id2, v2) = (w.clone(), id.clone(), val.clone());
    fader.on_change(move |x| {
        let pct = x.round() as i64;
        v2.set_text(&format!("{pct} %"));
        if let Some(s) = w2.sb.board.borrow_mut().sound_mut(&id2) {
            s.volume = pct;
        }
        update_pads(&w2);
        save(&w2);
    });
    body.append(&fader.area);
    // Schleife
    let lrow = hbox(10);
    lrow.set_margin_bottom(16);
    let ltext = vbox(2);
    ltext.set_hexpand(true);
    ltext.append(&label(&tr!("Loop"), &[]));
    ltext.append(&label(&tr!("Repeat until stopped"), &["muted", "tiny"]));
    lrow.append(&ltext);
    let sw = Switch::new();
    sw.set(sound.looping);
    let (w2, id2) = (w.clone(), id.clone());
    sw.on_toggle(move |on| {
        if let Some(s) = w2.sb.board.borrow_mut().sound_mut(&id2) {
            s.looping = on;
        }
        w2.sb.player.borrow_mut().set_looping(&id2, on);
        save(&w2);
        update_pads(&w2);
    });
    lrow.append(&sw.area);
    body.append(&lrow);
    // Auslöser: die Taste auf dem Deck
    widgets::field(&body, &tr!("Triggers"));
    let trg = hbox(10);
    trg.add_css_class("sb-row");
    trg.append(&theme::glyph("deck", 14, theme::MUTED));
    let trigger_name = label("", &[]);
    trigger_name.set_hexpand(true);
    trg.append(&trigger_name);
    let trigger_value = label("", &["muted", "small"]);
    trg.append(&trigger_value);
    body.append(&trg);
    let spacer = vbox(0);
    spacer.set_vexpand(true);
    body.append(&spacer);
    // Abspielen und Löschen
    let brow = hbox(8);
    brow.set_margin_top(14);
    let on = v.player.borrow().is_playing(&id);
    let play = widgets::button(&if on { tr!("Stop") } else { tr!("Play") }, Some(if on { "stop" } else { "play" }),
                               "primary");
    let (w2, id2) = (w.clone(), id.clone());
    play.connect_clicked(move |_| {
        toggle(&w2, &id2);
        update_pads(&w2);
    });
    brow.append(&play);
    brow.append(&widgets::spacer());
    let del = widgets::button(&tr!("Delete sound"), Some("trash"), "danger-btn");
    let (w2, id2) = (w.clone(), id.clone());
    del.connect_clicked(move |_| delete(&w2, &id2));
    brow.append(&del);
    body.append(&brow);
    let side = Side { id, trim, st, start: start_e, end: end_e, plays, trigger_name, trigger_value, play };
    trim_labels(&side);
    *v.side.borrow_mut() = Some(side);
    update_trigger(w);
    update_progress(w);
}

/// Griff des Zuschnitts an die Stelle x ziehen (während des Ziehens: nur Anzeige).
fn move_handle(w: &Rc<Win>, x: f64) {
    let side = w.sb.side.borrow();
    let Some(side) = side.as_ref() else { return };
    {
        let mut st = side.st.borrow_mut();
        let dur = st.info.as_ref().map(|i| i.duration).unwrap_or(0.0);
        let width = side.trim.width() as f64;
        let t = ((x - 10.0) / (width - 20.0).max(1.0)).clamp(0.0, 1.0) * dur;
        match st.grab {
            Some(true) => st.start = t.min(st.end - sb::MIN_LENGTH).max(0.0),
            Some(false) => st.end = t.max(st.start + sb::MIN_LENGTH).min(dur),
            None => return,
        }
    }
    side.trim.queue_draw();
    trim_labels(side);
}

fn delete(w: &Rc<Win>, id: &str) {
    w.sb.player.borrow_mut().stop(id);
    let gone: Option<Sound> = sb::remove(&mut w.sb.board.borrow_mut(), id, &sb::sounds_dir());
    if gone.is_some() {
        *w.sb.selected.borrow_mut() = None;
        *w.sb.playing.borrow_mut() = w.sb.player.borrow().playing();
        save(w);
        fill_pads(w);
        fill_panel(w);
    }
}
