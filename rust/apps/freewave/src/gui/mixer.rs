//! Ansicht „Mixer“ (Entwurf A): einheitliches Raster – links je Kanal eine Karte mit Stumm, Hauptregler
//! (Pegel als feine Linie darunter), Prozent und Effekten; je Mix eine Spalte mit Kopf (Abhören, Master-Regler
//! des Mixes in seiner Farbe mit Pegel und dB) und je Kanal einer Zelle mit eigenem Regler – oder einer
//! gestrichelten leeren Zelle zum Hinzufügen. Die Kette zwischen den ersten beiden Mix-Reglern eines Kanals
//! koppelt sie; zu sehen ist sie nur gekoppelt oder beim Überfahren der Zeile (waveui.py: _build_table,
//! _channel_row, _mix_head, _mix_cell, _place_links, link_look).

use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::rc::Rc;
use std::sync::Arc;
use std::time::{Duration, Instant};

use gtk::prelude::*;
use gtk::Align;
use gtk4 as gtk;
use indexmap::IndexMap;

use freestream_core::i18n::pgettext;
use freestream_core::tr;
use freestream_wave::control::{ChannelView, MixView, Snapshot};
use freestream_wave::levels::{level_db, post_fader};
use freestream_wave::mixer::Mute;

use super::theme::{self, channel_look};
use super::widgets::{self, hbox, label, vbox, Fader, Toggle, ToggleKind};
use super::{dialogs, effects_ui, Win};

pub const CH_W: i32 = 352;
pub const ROW_H: i32 = 60;
pub const HEAD_H: i32 = 92;
/// Abstand der Spalten und der Zeilen
const CELL_GAP: i32 = 14;
const ROW_GAP: i32 = 8;
/// Hauptregler eines Kanals
const CH_FADER: i32 = 56;
const MIX_W_MIN: i32 = 176;
const MIX_W_MAX: i32 = 300;
/// Lücke hinter dem ersten Mix eines Kanals, darin die Kette
const LINK_GAP: i32 = 24;
/// Mix-Pegel ab −1 dB: „CLIP“ für 1,5 s
const CLIP_DB: f64 = -1.0;
const CLIP_HOLD: Duration = Duration::from_millis(1500);

/// Regler, Stumm und Wert einer Zelle (Hauptregler oder Mix); der Pegel steckt im Regler.
pub struct Part {
    pub fader: Fader,
    pub mute: Toggle,
    pub val: gtk::Label,
    pub pct: Cell<i64>,
}

/// Kanalzeile.
pub struct Strip {
    pub card: gtk::Box,
    pub sub: gtk::Label,
    pub master: Part,
    pub fx: Toggle,
    pub hear: Option<Toggle>,
    pub parts: RefCell<HashMap<String, Rc<Part>>>,
}

/// Kopf einer Mix-Spalte.
pub struct Head {
    pub card: gtk::Box,
    pub sub: gtk::Label,
    pub ear: Toggle,
    /// Master-Regler des Mixes (Pegel als Linie darunter)
    pub fader: Fader,
    pub db: gtk::Label,
    clip_until: Cell<Option<Instant>>,
}

/// Aufgebaute Tabelle.
pub struct Table {
    sig: String,
    grid: gtk::Grid,
    pub strips: IndexMap<String, Rc<Strip>>,
    pub heads: IndexMap<String, Rc<Head>>,
    links: HashMap<String, Toggle>,
    /// Kanal der Zeile unter der Maus (zeigt deren Kette)
    hover_row: RefCell<Option<String>>,
    /// alle Widgets mit Spaltenbreite (Kopf, Zellen) – beim Ändern der Fensterbreite angepasst
    sized: Vec<gtk::Widget>,
    mix_w: i32,
}

pub struct MixerView {
    pub root: gtk::Stack,
    grid_box: gtk::Box,
    /// Hinweiszeile: Text vor der Quelle, Quelle (fett), Text danach
    hint: [gtk::Label; 3],
    pub table: RefCell<Option<Table>>,
    last_width: Cell<i32>,
}

impl MixerView {
    pub fn new() -> Self {
        let root = gtk::Stack::new();
        // Mixer noch nicht eingerichtet
        let off = vbox(0);
        off.set_halign(Align::Center);
        off.set_valign(Align::Start);
        off.set_margin_top(60);
        let card = widgets::card(&["big"]);
        card.set_size_request(520, -1);
        let m = theme::mark(64);
        m.set_halign(Align::Center);
        card.append(&m);
        let t = label(&tr!("Set up mixer"), &["big-title"]);
        t.set_halign(Align::Center);
        widgets::margins(&t, 14, 0, 6, 0);
        card.append(&t);
        let txt = widgets::wrap(&tr!("Just like in Elgato Wave Link, every app gets a channel – with its own volume for your headphones (Personal Mix), your stream (Stream Mix) and any mix you create."), &["muted"], 56);
        txt.set_justify(gtk::Justification::Center);
        txt.set_xalign(0.5);
        card.append(&txt);
        off.append(&card);
        root.add_named(&off, Some("off"));
        // Mixer aktiv: Tabelle, darunter eine Hinweiszeile
        let on = vbox(0);
        let sc = gtk::ScrolledWindow::new();
        sc.set_vexpand(true);
        let inner = vbox(0);
        widgets::margins(&inner, 16, 10, 0, 22);
        let grid_box = vbox(0);
        inner.append(&grid_box);
        sc.set_child(Some(&inner));
        on.append(&sc);
        let foot = hbox(0);
        widgets::margins(&foot, 6, 24, 8, 24);
        let info = info_icon();
        info.set_margin_end(8);
        foot.append(&info);
        let hint = [label("", &["faint", "small"]), label("", &["hint-src", "small"]), label("", &["faint", "small"])];
        for h in &hint {
            foot.append(h);
        }
        on.append(&foot);
        root.add_named(&on, Some("on"));
        root.set_visible_child_name("off");
        Self { root, grid_box, hint, table: RefCell::new(None), last_width: Cell::new(0) }
    }
}

/// ⓘ vor der Hinweiszeile.
fn info_icon() -> gtk::DrawingArea {
    let a = gtk::DrawingArea::new();
    a.set_content_width(14);
    a.set_content_height(14);
    a.set_valign(Align::Center);
    a.set_draw_func(|_, cr, w, h| {
        let (cx, cy) = (w as f64 / 2.0, h as f64 / 2.0);
        theme::set_source(cr, theme::FAINT);
        cr.set_line_width(1.4);
        cr.arc(cx, cy, 6.2, 0.0, std::f64::consts::TAU);
        let _ = cr.stroke();
        cr.move_to(cx, cy - 0.7);
        cr.line_to(cx, cy + 3.4);
        let _ = cr.stroke();
        cr.arc(cx, cy - 3.2, 0.9, 0.0, std::f64::consts::TAU);
        let _ = cr.fill();
    });
    a
}

/// Knopf „Mixer einrichten“ in der leeren Ansicht verbinden.
pub fn init(w: &Rc<Win>) {
    if let Some(off) = w.mixer.root.child_by_name("off").and_then(|o| o.first_child()).and_then(|c| c.downcast::<gtk::Box>().ok()) {
        let b = widgets::button(&tr!("Set up mixer"), Some("wave"), "primary");
        b.set_halign(Align::Center);
        b.set_margin_top(18);
        let w2 = w.clone();
        b.connect_clicked(move |_| w2.mixer_enable(true));
        off.append(&b);
    }
}

/// Breite der Mix-Spalten: so, dass alle Mixe ins Fenster passen (wie Wave Link), in Grenzen.
fn fit_mix_w(content: i32, n: usize) -> i32 {
    let cw = if content < 100 { 1280 - 236 } else { content };
    // 20: Bildlaufleiste; jede Spalte hat rechts CELL_GAP Abstand (bei der ersten LINK_GAP)
    let avail = cw - 22 - 10 - CH_W - 20 - LINK_GAP;
    (avail / n.max(1) as i32 - CELL_GAP).clamp(MIX_W_MIN, MIX_W_MAX)
}

/// Fenster breiter/schmaler: Mix-Spalten neu verteilen (nur bei echter Änderung).
pub fn check_width(w: &Rc<Win>) {
    let cw = w.mixer.root.width();
    if cw == w.mixer.last_width.get() || cw < 100 {
        return;
    }
    w.mixer.last_width.set(cw);
    let mut t = w.mixer.table.borrow_mut();
    let Some(t) = t.as_mut() else { return };
    let mw = fit_mix_w(cw, t.heads.len());
    if mw != t.mix_w {
        t.mix_w = mw;
        for x in &t.sized {
            x.set_size_request(mw, x.height_request());
        }
    }
}

fn sig_of(s: &Snapshot) -> String {
    let ch: Vec<String> = s.channels.iter().map(|c| {
        let c = &c.channel;
        format!("{}|{}|{}|{}|{}", c.id, c.name, c.icon, c.color, c.kind)
    }).collect();
    let mx: Vec<String> = s.mixes.iter().map(|m| {
        format!("{}|{}|{}|{}|{}|{}", m.mix.id, m.mix.name, m.mix.icon, m.mix.color, m.mix.exclude.join(","), m.can_listen)
    }).collect();
    format!("{}#{}", ch.join(";"), mx.join(";"))
}

/// Kanäle in Anzeigereihenfolge (Mikrofon zuerst).
pub fn ordered(s: &Snapshot) -> Vec<&ChannelView> {
    let mut v: Vec<&ChannelView> = s.channels.iter().collect();
    v.sort_by_key(|c| c.channel.id != "mic");
    v
}

/// Prozentzahl rechts in den Zellen – breit genug für „100 %“ und „stumm“.
fn val_label() -> gtk::Label {
    let l = label("–", &["val"]);
    l.set_width_chars(pgettext("state", "muted").chars().count().max(5) as i32);
    l.set_xalign(1.0);
    l
}

/// Wert einer Zelle zeigen: Prozent, stumm rot „stumm“.
fn show_val(p: &Part, muted: bool) {
    let text = if muted { pgettext("state", "muted") } else { format!("{} %", p.pct.get()) };
    if p.val.text() != text {
        p.val.set_text(&text);
    }
    widgets::set_class(&p.val, "muted-val", muted);
}

/// Mixer aus: leere Ansicht.
pub fn apply_off(w: &Rc<Win>) {
    w.mixer.root.set_visible_child_name("off");
    *w.mixer.table.borrow_mut() = None;
    widgets::clear(&w.mixer.grid_box);
}

/// Neuer Zustand: Tabelle (nur bei geänderten Kanälen/Mixen) neu bauen, Werte übernehmen.
pub fn apply(w: &Rc<Win>, s: &Arc<Snapshot>) {
    w.mixer.root.set_visible_child_name("on");
    let sig = sig_of(s);
    let rebuild = w.mixer.table.borrow().as_ref().map(|t| t.sig != sig).unwrap_or(true);
    if rebuild {
        build(w, s, sig);
    }
    let t = w.mixer.table.borrow();
    let Some(t) = t.as_ref() else { return };
    let streams_by: HashMap<&str, Vec<&str>> = s.streams.iter().filter_map(|x| x.channel.as_deref().map(|c| (c, x.app.as_str())))
        .fold(HashMap::new(), |mut m, (c, a)| {
            m.entry(c).or_default().push(a);
            m
        });
    let conf = w.conf.borrow().clone();
    for cv in &s.channels {
        let cid = cv.channel.id.as_str();
        let Some(strip) = t.strips.get(cid) else { continue };
        // Regler je Mix
        for (mid, part) in strip.parts.borrow().iter() {
            match cv.levels.get(mid) {
                Some(l) => {
                    if !w.held(&format!("lvl:{cid}:{mid}")) {
                        part.fader.set(l.volume.min(100) as f64);
                        part.pct.set(l.volume);
                    }
                    if !w.held(&format!("mute:{cid}:{mid}")) {
                        part.mute.set(l.mute);
                        part.fader.set_dimmed(l.mute);
                    }
                    show_val(part, part.mute.get());
                }
                None => {
                    // Mix ohne Ausgang: kein Regler
                    part.val.set_text("–");
                    widgets::set_class(&part.val, "muted-val", false);
                    part.fader.set_dimmed(true);
                }
            }
        }
        if let Some(hear) = &strip.hear {
            if !w.held("hear") {
                hear.set(cv.levels.get("monitor").is_some_and(|l| !l.mute));
            }
        }
        if let Some(m) = cv.master {
            if !w.held(&format!("master:{cid}")) {
                strip.master.fader.set(m.volume.min(100) as f64);
                strip.master.pct.set(m.volume);
            }
            if !w.held(&format!("mmute:{cid}")) {
                strip.master.mute.set(m.mute);
                strip.master.fader.set_dimmed(m.mute);
            }
            show_val(&strip.master, strip.master.mute.get());
        }
        // Unterzeile: Programme, Gerät oder Mikrofon
        let apps = streams_by.get(cid).map(|v| v.join(", ")).unwrap_or_default();
        let sub = if !apps.is_empty() {
            apps
        } else if cid == "mic" {
            if s.mic.hardware { "Wave:3".into() } else if s.mic.source.is_some() { "Wave".into() } else { " ".into() }
        } else if cv.channel.kind == "input" {
            let src = &cv.channel.source;
            s.inputs.iter().find(|d| &d.name == src).map(|d| d.description.clone()).unwrap_or_else(|| src.clone())
        } else {
            " ".into()
        };
        if strip.sub.text() != sub {
            strip.sub.set_text(&sub);
        }
        strip.fx.set(!effects_ui::fx_names(&conf, cid).is_empty());
        if let Some(link) = t.links.get(cid) {
            if !w.held(&format!("link:{cid}")) {
                link.set(cv.linked);
            }
        }
    }
    let listen = if w.held("listen") { w.listen_now.borrow().clone().unwrap_or_else(|| s.listening.clone()) } else { s.listening.clone() };
    if !w.held("listen") {
        *w.listen_now.borrow_mut() = Some(s.listening.clone());
    }
    for m in &s.mixes {
        if let Some(h) = t.heads.get(&m.mix.id) {
            let sub = w.mix_sub(m);
            if h.sub.text() != sub {
                h.sub.set_text(&sub);
            }
            h.ear.set(m.mix.id == listen);
            // Master-Regler des Mixes (ohne Ausgang: gesperrt)
            match m.volume {
                Some(l) => {
                    h.fader.set_enabled(true);
                    if !w.held(&format!("mixvol:{}", m.mix.id)) {
                        h.fader.set(l.volume.min(100) as f64);
                        h.fader.set_dimmed(l.mute);
                    }
                }
                None => {
                    h.fader.set_enabled(false);
                    h.fader.set_dimmed(true);
                }
            }
        }
    }
    // eine Hinweiszeile: OBS: add **Monitor of FreeWave Stream Mix** as audio source · Hover a row …
    let obs = tr!("OBS: add {source} as audio source");
    let (before, after) = obs.split_once("{source}").unwrap_or((obs.as_str(), ""));
    let source = if obs.contains("{source}") { tr!("Monitor of {device}", device = tr!("FreeWave Stream Mix")) } else { String::new() };
    let after = format!("{after}  ·  {}", tr!("Hover a row to couple faders or add it to a mix"));
    for (lb, text) in w.mixer.hint.iter().zip([before.to_string(), source, after]) {
        if lb.text() != text {
            lb.set_text(&text);
        }
    }
}

/// Pegel anzeigen (20-mal pro Sekunde, nur bei sichtbarem Mixer).
pub fn set_meters(w: &Rc<Win>, lv: &IndexMap<String, f64>) {
    let t = w.mixer.table.borrow();
    let Some(t) = t.as_ref() else { return };
    let s = w.snap.borrow().clone();
    for cv in &s.channels {
        let Some(strip) = t.strips.get(&cv.channel.id) else { continue };
        // gemessen wird nach dem Hauptregler (Kanal-Sink bzw. Quelle)
        let raw = lv.get(&cv.channel.id).copied().unwrap_or(0.0);
        strip.master.fader.set_level(raw);
        for (mid, part) in strip.parts.borrow().iter() {
            let vol = cv.levels.contains_key(mid).then(|| part.fader.get());
            part.fader.set_level(post_fader(raw, vol, part.mute.get()));
        }
    }
    let now = Instant::now();
    let running = !lv.is_empty();
    for (mid, h) in &t.heads {
        let v = lv.get(&format!("mix:{mid}")).copied().unwrap_or(0.0);
        h.fader.set_level(v);
        let db = level_db(v);
        if db.is_some_and(|d| d >= CLIP_DB) {
            h.clip_until.set(Some(now + CLIP_HOLD)); // übersteuert: kurz rot „CLIP“ halten
        }
        let clip = h.clip_until.get().is_some_and(|u| u > now);
        let text = if clip {
            "CLIP".to_string()
        } else if let Some(d) = db {
            format!("{d:.0} dB")
        } else if running {
            "−∞ dB".to_string()
        } else {
            String::new()
        };
        if h.db.text() != text {
            h.db.set_text(&text);
            widgets::set_class(&h.db, "clip", clip);
        }
    }
}

/// Maus über der Tabelle (y in Koordinaten des Rasters, None: hinaus): die Kette dieser Zeile zeigen.
fn hover_at(w: &Rc<Win>, y: Option<f64>) {
    let Ok(t) = w.mixer.table.try_borrow() else { return };
    let Some(t) = t.as_ref() else { return };
    let row = y.and_then(|y| {
        t.strips.iter().find(|(_, s)| {
            s.card.compute_bounds(&t.grid).is_some_and(|b| {
                let top = b.y() as f64;
                y >= top && y < top + b.height() as f64 + ROW_GAP as f64
            })
        }).map(|(c, _)| c.clone())
    });
    if *t.hover_row.borrow() == row {
        return;
    }
    for (cid, link) in &t.links {
        link.set_row_hover(row.as_deref() == Some(cid.as_str()));
    }
    *t.hover_row.borrow_mut() = row;
}

// ================================================================== Aufbau
fn build(w: &Rc<Win>, s: &Arc<Snapshot>, sig: String) {
    widgets::clear(&w.mixer.grid_box);
    let mix_w = fit_mix_w(w.mixer.root.width(), s.mixes.len());
    let grid = gtk::Grid::new();
    grid.set_row_spacing(0);
    grid.set_column_spacing(0);
    grid.set_halign(Align::Start); // Spalten so breit wie bestellt (Regler mit hexpand würden sie dehnen)
    w.mixer.grid_box.append(&grid);
    let motion = gtk::EventControllerMotion::new();
    let w2 = w.clone();
    motion.connect_motion(move |_, _, y| hover_at(&w2, Some(y)));
    let w2 = w.clone();
    motion.connect_leave(move |_| hover_at(&w2, None));
    grid.add_controller(motion);
    let mut t = Table { sig, grid: grid.clone(), strips: IndexMap::new(), heads: IndexMap::new(), links: HashMap::new(),
                        hover_row: RefCell::new(None), sized: Vec::new(), mix_w };
    // oben links: kleine Spaltenköpfe „KANAL“ und „PEGEL“ über den Kanälen
    let corner = hbox(0);
    corner.set_size_request(CH_W, HEAD_H);
    corner.set_margin_end(CELL_GAP);
    corner.set_margin_bottom(ROW_GAP);
    let ch = label(&tr!("Channel").to_uppercase(), &["col-head"]);
    ch.set_hexpand(true);
    ch.set_valign(Align::End);
    widgets::margins(&ch, 0, 0, 8, 4);
    corner.append(&ch);
    let lvl = label(&tr!("Level").to_uppercase(), &["col-head"]);
    lvl.set_valign(Align::End);
    lvl.set_xalign(0.0);
    lvl.set_margin_bottom(8);
    // rechts davon: Prozentzahl, Effekte-Knopf und Rand der Kanalkarte – der Text steht über dem Hauptregler
    lvl.set_size_request(CH_FADER + 4 + 46 + 4 + 28 + 12, -1);
    corner.append(&lvl);
    grid.attach(&corner, 0, 0, 1, 1);
    let col_of = |i: usize| 1 + 2 * i as i32;
    // Kette je Kanal hinter seinem ersten Mix: in welcher Lücke?
    let order = ordered(s);
    let mut anchor: HashMap<String, usize> = HashMap::new();
    for cv in &order {
        let members: Vec<usize> = s.mixes.iter().enumerate().filter(|(_, m)| !m.mix.exclude.contains(&cv.channel.id)).map(|(i, _)| i).collect();
        if members.len() > 1 {
            anchor.insert(cv.channel.id.clone(), members[0]);
        }
    }
    for (i, m) in s.mixes.iter().enumerate() {
        let head = mix_head(w, m, mix_w);
        grid.attach(&head.card, col_of(i), 0, 1, 1);
        t.sized.push(head.card.clone().upcast());
        t.heads.insert(m.mix.id.clone(), head);
        if i + 1 < s.mixes.len() {
            let gap = hbox(0);
            gap.set_size_request(if anchor.values().any(|a| *a == i) { LINK_GAP } else { CELL_GAP }, 1);
            grid.attach(&gap, col_of(i) + 1, 0, 1, 1);
        }
    }
    for (r, cv) in order.iter().enumerate() {
        let row = r as i32 + 1;
        let strip = channel_row(w, cv);
        grid.attach(&strip.card, 0, row, 1, 1);
        for (i, m) in s.mixes.iter().enumerate() {
            let member = !m.mix.exclude.contains(&cv.channel.id);
            // (Zelle im Raster, Widget mit der Spaltenbreite)
            let (cell, sized) = if member {
                let (cell, card, part) = mix_cell(w, &cv.channel.id, m, mix_w);
                strip.parts.borrow_mut().insert(m.mix.id.clone(), part);
                (cell, card)
            } else {
                let e = empty_cell(w, &cv.channel.id, &m.mix.id, &m.mix.name, mix_w);
                (e.clone(), e)
            };
            cell.set_margin_bottom(ROW_GAP);
            grid.attach(&cell, col_of(i), row, 1, 1);
            t.sized.push(sized);
            if anchor.get(&cv.channel.id) == Some(&i) {
                let link = Toggle::new(ToggleKind::Link, 20, 20, ("link", "link"), theme::ACCENT);
                link.area.set_margin_bottom(ROW_GAP);
                let (w2, cid, name) = (w.clone(), cv.channel.id.clone(), cv.channel.name.clone());
                link.on_toggle(move |on| {
                    w2.touch(&format!("link:{cid}"));
                    w2.status(&if on {
                        tr!("“{channel}”: sliders linked – all mixes move together", channel = name)
                    } else {
                        tr!("“{channel}”: sliders unlinked", channel = name)
                    });
                    let c = cid.clone();
                    w2.ctl.act(move |m| {
                        m.set_linked(&c, on);
                        Ok(())
                    });
                });
                link.set(cv.linked);
                grid.attach(&link.area, col_of(i) + 1, row, 1, 1);
                t.links.insert(cv.channel.id.clone(), link);
            }
        }
        t.strips.insert(cv.channel.id.clone(), strip);
    }
    let add = gtk::Button::new();
    add.add_css_class("add-row");
    add.set_size_request(CH_W, 44);
    let row = hbox(8);
    row.set_halign(Align::Center);
    row.append(&theme::glyph("plus", 16, theme::MUTED));
    row.append(&gtk::Label::new(Some(&tr!("Create channel"))));
    add.set_child(Some(&row));
    add.set_margin_top(4);
    add.set_halign(Align::Start);
    let w2 = w.clone();
    add.connect_clicked(move |_| dialogs::channel_dialog(&w2, None));
    grid.attach(&add, 0, order.len() as i32 + 1, 1, 1);
    *w.mixer.table.borrow_mut() = Some(t);
}

fn mix_head(w: &Rc<Win>, m: &MixView, mix_w: i32) -> Rc<Head> {
    let color = if m.mix.color.is_empty() { theme::ACCENT.to_string() } else { m.mix.color.clone() };
    let card = vbox(0);
    card.add_css_class("cell");
    card.set_size_request(mix_w, HEAD_H);
    card.set_margin_bottom(ROW_GAP);
    let top = hbox(10);
    top.set_margin_top(12);
    let icon = if m.mix.icon.is_empty() { "wave" } else { m.mix.icon.as_str() };
    let tile = theme::tile(icon, 32, &theme::mix(theme::PANEL2, &color, 0.16), 0.25, &color);
    top.append(&tile);
    let names = vbox(0);
    names.set_hexpand(true);
    names.set_valign(Align::Center);
    let name = label(&m.mix.name, &["ch-name"]);
    name.set_ellipsize(gtk::pango::EllipsizeMode::End);
    name.set_width_chars(1);
    name.set_max_width_chars(1); // natürliche Breite klein: die Zelle bleibt so breit wie bestellt
    names.append(&name);
    let sub = label(&w.mix_sub(m), &["ch-sub"]);
    sub.set_ellipsize(gtk::pango::EllipsizeMode::End);
    sub.set_width_chars(1);
    sub.set_max_width_chars(1);
    names.append(&sub);
    top.append(&names);
    let ear = Toggle::new(ToggleKind::Round, 28, 28, ("headphones", "headphones"), &color);
    ear.area.set_visible(m.can_listen);
    let (w2, mid) = (w.clone(), m.mix.id.clone());
    ear.on_toggle(move |_| on_listen(&w2, &mid));
    top.append(&ear.area);
    card.append(&top);
    // Master-Regler des Mixes in seiner Farbe, darunter sein Pegel; rechts die dB-Zahl (rot „CLIP“)
    let mrow = hbox(8);
    mrow.set_margin_top(12);
    let fader = Fader::slim(40, Some(&color));
    fader.area.set_hexpand(true);
    mrow.append(&fader.area);
    let db = label("", &["db"]);
    db.set_width_chars(7);
    db.set_xalign(1.0);
    mrow.append(&db);
    card.append(&mrow);
    let (w2, mid) = (w.clone(), m.mix.id.clone());
    fader.on_change(move |v| {
        let pct = v.round() as i64;
        let id = mid.clone();
        w2.debounced(&format!("mixvol:{mid}"), 120, move |h| h.mixer.set_mix_volume(&id, pct).map(|_| ()));
    });
    for wd in [tile.upcast_ref::<gtk::Widget>(), names.upcast_ref()] {
        let (w2, mid) = (w.clone(), m.mix.id.clone());
        widgets::on_click(wd, move || dialogs::edit_mix_id(&w2, &mid));
    }
    let (w2, mid, c2) = (w.clone(), m.mix.id.clone(), card.clone());
    widgets::on_right_click(&card, move |x, y| dialogs::mix_menu(&w2, &mid, &c2, x, y));
    Rc::new(Head { card, sub, ear, fader, db, clip_until: Cell::new(None) })
}

fn on_listen(w: &Rc<Win>, mid: &str) {
    let s = w.snap.borrow().clone();
    let cur = w.listen_now.borrow().clone().unwrap_or_else(|| s.listening.clone());
    let target = if mid == cur { "monitor".to_string() } else { mid.to_string() };
    *w.listen_now.borrow_mut() = Some(target.clone()); // gilt sofort, auch vor der nächsten Aktualisierung
    w.touch("listen");
    if let Some(t) = w.mixer.table.borrow().as_ref() {
        for (k, h) in &t.heads {
            h.ear.set(*k == target);
        }
    }
    let name = s.mixes.iter().find(|m| m.mix.id == target).map(|m| m.mix.name.clone()).unwrap_or_else(|| target.clone());
    w.job(move |h| h.mixer.listen(&target), move |w, _| w.status(&tr!("Listening to “{mix}”", mix = name)));
}

fn channel_row(w: &Rc<Win>, cv: &ChannelView) -> Rc<Strip> {
    let c = &cv.channel;
    let cid = c.id.clone();
    let (glyph, color) = channel_look(c);
    let card = hbox(0);
    card.add_css_class("cell");
    card.set_size_request(CH_W, ROW_H);
    card.set_margin_end(CELL_GAP);
    card.set_margin_bottom(ROW_GAP);
    let tile = theme::tile(&glyph, 34, &color, 0.24, "#ffffff");
    tile.set_margin_end(10);
    card.append(&tile);
    let names = vbox(0);
    names.set_hexpand(true);
    names.set_valign(Align::Center);
    let name = label(&c.name, &["ch-name"]);
    name.set_ellipsize(gtk::pango::EllipsizeMode::End);
    name.set_width_chars(1);
    name.set_max_width_chars(1); // natürliche Breite klein: die Zelle bleibt so breit wie bestellt
    names.append(&name);
    let sub = label(" ", &["ch-sub"]);
    sub.set_ellipsize(gtk::pango::EllipsizeMode::End);
    sub.set_width_chars(1);
    sub.set_max_width_chars(1);
    names.append(&sub);
    card.append(&names);
    for wd in [tile.upcast_ref::<gtk::Widget>(), names.upcast_ref()] {
        let (w2, id) = (w.clone(), cid.clone());
        widgets::on_click(wd, move || dialogs::edit_channel_id(&w2, &id));
    }
    // sich selbst hören (Mikrofon): vor dem Stumm-Knopf, damit Regler und Prozent in einer Flucht bleiben
    let hear = (cid == "mic").then(|| {
        let h = Toggle::new(ToggleKind::Hear, 26, 26, ("headphones", "headphones"), "");
        h.area.set_margin_start(4);
        card.append(&h.area);
        h
    });
    let mute = Toggle::new(ToggleKind::Mute, 24, 24, ("speaker", "speaker_off"), "");
    widgets::margins(&mute.area, 0, 4, 0, 4);
    card.append(&mute.area);
    let fader = Fader::slim(CH_FADER, None);
    card.append(&fader.area);
    let val = val_label();
    val.set_margin_start(4);
    card.append(&val);
    let fx = Toggle::new(ToggleKind::Fx, 28, 28, ("sliders", "sliders"), "");
    fx.area.set_margin_start(4);
    card.append(&fx.area);
    let strip = Rc::new(Strip {
        card: card.clone(),
        sub,
        master: Part { fader, mute, val, pct: Cell::new(0) },
        fx,
        hear,
        parts: RefCell::new(HashMap::new()),
    });
    // Hauptregler
    let (w2, id, st) = (w.clone(), cid.clone(), Rc::downgrade(&strip));
    strip.master.fader.on_change(move |v| {
        let pct = v.round() as i64;
        if let Some(st) = st.upgrade() {
            st.master.pct.set(pct);
            show_val(&st.master, st.master.mute.get());
        }
        let c = id.clone();
        w2.debounced(&format!("master:{id}"), 120, move |h| h.mixer.set_master(&c, pct).map(|_| ()));
    });
    let (w2, id, st) = (w.clone(), cid.clone(), Rc::downgrade(&strip));
    strip.master.mute.on_toggle(move |on| {
        if let Some(st) = st.upgrade() {
            st.master.fader.set_dimmed(on);
            show_val(&st.master, on);
        }
        w2.touch(&format!("mmute:{id}"));
        let c = id.clone();
        w2.ctl.act(move |m| m.set_master_mute(&c, Mute::Set(on)));
    });
    let (w2, ch) = (w.clone(), c.clone());
    strip.fx.on_toggle(move |_| effects_ui::open(&w2, &ch));
    if let Some(h) = &strip.hear {
        let w2 = w.clone();
        h.on_toggle(move |on| {
            // sich selbst hören: Mikrofon im Persönlichen Mix laut/stumm
            w2.touch("hear");
            w2.status(&if on { tr!("You now hear yourself on your headphones (Personal Mix).") } else { tr!("You no longer hear yourself.") });
            w2.ctl.act(move |m| {
                if !m.channel_mixes("mic").iter().any(|x| x == "monitor") {
                    m.set_member("mic", "monitor", true)?;
                }
                m.set_mute("mic", "monitor", Mute::Set(!on), false).map(|_| ())
            });
        });
    }
    // Programme per Ziehen hierher legen (nur Kanäle für Programme)
    if cid != "mic" && c.kind == "apps" {
        super::views::drop_target(w, &card, &cid);
    }
    strip
}

fn mix_cell(w: &Rc<Win>, cid: &str, m: &MixView, mix_w: i32) -> (gtk::Widget, gtk::Widget, Rc<Part>) {
    let mid = m.mix.id.clone();
    let card = hbox(0);
    card.add_css_class("cell");
    card.set_size_request(mix_w, ROW_H);
    let mute = Toggle::new(ToggleKind::Mute, 24, 24, ("speaker", "speaker_off"), "");
    mute.area.set_margin_end(8);
    card.append(&mute.area);
    let fader = Fader::slim(40, None);
    fader.area.set_hexpand(true);
    card.append(&fader.area);
    let val = val_label();
    val.set_margin_start(8);
    card.append(&val);
    let part = Rc::new(Part { fader, mute, val, pct: Cell::new(0) });
    let (w2, c, mx, p) = (w.clone(), cid.to_string(), mid.clone(), Rc::downgrade(&part));
    part.fader.on_change(move |v| on_level(&w2, &c, &mx, v, &p));
    let (w2, c, mx, p) = (w.clone(), cid.to_string(), mid.clone(), Rc::downgrade(&part));
    part.mute.on_toggle(move |on| {
        if let Some(p) = p.upgrade() {
            p.fader.set_dimmed(on);
            show_val(&p, on);
        }
        w2.touch(&format!("mute:{c}:{mx}"));
        let (c, mx) = (c.clone(), mx.clone());
        w2.ctl.act(move |m| m.set_mute(&c, &mx, Mute::Set(on), false).map(|_| ()));
    });
    let mname = m.mix.name.clone();
    let (w2, c, mx, cd) = (w.clone(), cid.to_string(), mid.clone(), card.clone());
    widgets::on_right_click(&card, move |x, y| {
        let (w3, c2, m2) = (w2.clone(), c.clone(), mx.clone());
        widgets::popup(&cd, Some((x, y)), vec![widgets::item(&tr!("Remove from “{mix}”", mix = mname), "close",
                                                             move || toggle_member(&w3, &c2, &m2, false))], 232);
    });
    // X zum Entfernen auf der Ecke, nur beim Überfahren
    let overlay = gtk::Overlay::new();
    overlay.set_child(Some(&card));
    let badge = remove_badge();
    badge.set_visible(false);
    overlay.add_overlay(&badge);
    let (w2, c, mx) = (w.clone(), cid.to_string(), mid);
    widgets::on_click(&badge, move || toggle_member(&w2, &c, &mx, false));
    let motion = gtk::EventControllerMotion::new();
    let b = badge.clone();
    motion.connect_enter(move |_, _, _| b.set_visible(true));
    let b = badge.clone();
    motion.connect_leave(move |_| b.set_visible(false));
    overlay.add_controller(motion);
    (overlay.upcast(), card.upcast(), part)
}

/// Kleines X im Kreis: Kanal aus dem Mix nehmen. Rot beim Überfahren.
fn remove_badge() -> gtk::DrawingArea {
    let a = gtk::DrawingArea::new();
    a.set_content_width(14);
    a.set_content_height(14);
    a.set_halign(Align::End);
    a.set_valign(Align::Start);
    widgets::margins(&a, 2, 2, 0, 0);
    let hover = Rc::new(Cell::new(false));
    let h = hover.clone();
    a.set_draw_func(move |_, cr, w, hh| {
        let (w, hh) = (w as f64, hh as f64);
        let (fill, ring, x) = if h.get() { (theme::DANGER, theme::DANGER, "#ffffff") } else { (theme::PANEL2, theme::BORDER, theme::MUTED) };
        cr.arc(w / 2.0, hh / 2.0, w / 2.0 - 0.5, 0.0, std::f64::consts::TAU);
        theme::set_source(cr, fill);
        let _ = cr.fill_preserve();
        theme::set_source(cr, ring);
        cr.set_line_width(1.0);
        let _ = cr.stroke();
        let r = w * 0.17;
        theme::set_source(cr, x);
        cr.set_line_width(1.6);
        cr.move_to(w / 2.0 - r, hh / 2.0 - r);
        cr.line_to(w / 2.0 + r, hh / 2.0 + r);
        cr.move_to(w / 2.0 - r, hh / 2.0 + r);
        cr.line_to(w / 2.0 + r, hh / 2.0 - r);
        let _ = cr.stroke();
    });
    let m = gtk::EventControllerMotion::new();
    let (h2, a2) = (hover.clone(), a.clone());
    m.connect_enter(move |_, _, _| {
        h2.set(true);
        a2.queue_draw();
    });
    let a2 = a.clone();
    m.connect_leave(move |_| {
        hover.set(false);
        a2.queue_draw();
    });
    a.add_controller(m);
    a
}

/// Zelle, wenn der Kanal nicht im Mix ist: gestrichelt mit blassem „+“; beim Überfahren hervorgehoben mit
/// „+ Zu <Mix> hinzufügen“ – ein Klick nimmt den Kanal in den Mix.
fn empty_cell(w: &Rc<Win>, cid: &str, mid: &str, mix_name: &str, mix_w: i32) -> gtk::Widget {
    let b = hbox(0);
    b.add_css_class("empty-cell");
    b.set_size_request(mix_w, ROW_H);
    b.set_cursor_from_name(Some("pointer"));
    let inner = hbox(6);
    inner.set_halign(Align::Center);
    inner.set_valign(Align::Center);
    inner.set_hexpand(true);
    let faint = theme::glyph("plus", 14, &theme::mix(theme::FAINT, theme::BG, 0.35));
    let plus = theme::glyph("plus", 12, theme::ACCENT);
    let text = label(&tr!("Add to {mix}", mix = mix_name), &["empty-add"]);
    text.set_ellipsize(gtk::pango::EllipsizeMode::End);
    text.set_max_width_chars(((mix_w - 60) / 7).max(4)); // die Zelle wird beim Überfahren nicht breiter
    plus.set_visible(false);
    text.set_visible(false);
    inner.append(&faint);
    inner.append(&plus);
    inner.append(&text);
    b.append(&inner);
    let m = gtk::EventControllerMotion::new();
    let (b2, f2, p2, t2) = (b.clone(), faint.clone(), plus.clone(), text.clone());
    m.connect_enter(move |_, _, _| {
        b2.add_css_class("hover");
        f2.set_visible(false);
        p2.set_visible(true);
        t2.set_visible(true);
    });
    let b2 = b.clone();
    m.connect_leave(move |_| {
        b2.remove_css_class("hover");
        faint.set_visible(true);
        plus.set_visible(false);
        text.set_visible(false);
    });
    b.add_controller(m);
    let (w2, c, mx) = (w.clone(), cid.to_string(), mid.to_string());
    widgets::on_click(&b, move || toggle_member(&w2, &c, &mx, true));
    b.upcast()
}

fn on_level(w: &Rc<Win>, cid: &str, mix: &str, v: f64, p: &std::rc::Weak<Part>) {
    let pct = v.round() as i64;
    let Some(part) = p.upgrade() else { return };
    let linked = w.snap.borrow().channel(cid).is_some_and(|c| c.linked);
    let linked = if w.held(&format!("link:{cid}")) {
        w.mixer.table.borrow().as_ref().and_then(|t| t.links.get(cid).map(|l| l.get())).unwrap_or(linked)
    } else {
        linked
    };
    if linked {
        // gekoppelt: die anderen Regler des Kanals gleich mitziehen (der Mixer macht es genauso)
        let delta = pct - part.pct.get();
        if let Some(t) = w.mixer.table.borrow().as_ref() {
            if let Some(strip) = t.strips.get(cid) {
                for (mid, o) in strip.parts.borrow().iter() {
                    if mid == mix {
                        continue;
                    }
                    let n = (o.pct.get() + delta).clamp(0, 100);
                    o.pct.set(n);
                    o.fader.set(n as f64);
                    show_val(o, o.mute.get());
                    w.touch(&format!("lvl:{cid}:{mid}"));
                }
            }
        }
    }
    part.pct.set(pct);
    show_val(&part, part.mute.get());
    w.touch(&format!("lvl:{cid}:{mix}"));
    w.ctl.set_volume(cid, mix, pct);
}

/// Kanal in einen Mix nehmen oder herausnehmen.
pub fn toggle_member(w: &Rc<Win>, cid: &str, mid: &str, on: bool) {
    w.touch("member");
    let (c, m) = (cid.to_string(), mid.to_string());
    w.job(move |h| h.mixer.set_member(&c, &m, on), move |w, _| {
        w.status(&if on { tr!("Channel added to the mix") } else { tr!("Channel removed from the mix") });
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mix_columns_fit() {
        // drei Mixe im 1280 px breiten Fenster: Spalten wie im Entwurf (~190 px), in Grenzen
        let w = fit_mix_w(1044, 3);
        assert!((180..=200).contains(&w), "{w}");
        assert_eq!(fit_mix_w(5000, 1), MIX_W_MAX);
        assert_eq!(fit_mix_w(200, 4), MIX_W_MIN);
    }
}
