//! Ansicht „Audio Flow“: vier Spalten (Eingänge, Kanäle, Mixe, Ausgänge) mit geschwungenen Verbindungen.
//! Ein Klick auf ein Element hebt seinen ganzen Signalweg hervor und blendet den Rest ab.
//!
//! Das Modell (Elemente, Verbindungen, Weg) kommt aus [`freestream_wave::flow`] und wird nur aus dem
//! [`Snapshot`] gebaut. Die Elemente sind Knöpfe in einem `gtk::Fixed`; darunter liegt eine DrawingArea im
//! selben Koordinatensystem, die mit Cairo die Bezier-Kurven zeichnet.

use std::cell::{Cell, RefCell};
use std::rc::Rc;
use std::sync::Arc;

use gtk::prelude::*;
use gtk::Align;
use gtk4 as gtk;

use freestream_core::tr;
use freestream_wave::control::Snapshot;
use freestream_wave::flow::{self, Col, Graph, Link, Path, Sub};

use super::apps::{self, AppEntry};
use super::theme::{self, channel_look};
use super::widgets::{self, hbox, label, vbox};
use super::Win;

/// Abstand zum Rand der Ansicht
const MARGIN: f64 = 22.0;
const NODE_H: f64 = 52.0;
const ROW: f64 = 63.0;
/// oberer Rand der ersten Zeile (darüber die Spaltenköpfe)
const TOP: f64 = 46.0;
const ICON: i32 = 32;

pub const CYAN: &str = "#22c3d6";
pub const MAGENTA: &str = "#cf3fe0";
pub const GREEN: &str = "#2fcf8f";
const MUTED_LINE: &str = "#8c8c8c";
const FAINT_LINE: &str = "#4a4a4a";

pub struct FlowView {
    pub root: gtk::Stack,
    scroll: gtk::ScrolledWindow,
    fixed: gtk::Fixed,
    area: gtk::DrawingArea,
    graph: RefCell<Graph>,
    nodes: RefCell<Vec<gtk::Button>>,
    chips: RefCell<Vec<(Col, gtk::Box)>>,
    /// Position (x, y) je Element und Breite der Elemente
    pos: RefCell<Vec<(f64, f64)>>,
    node_w: Cell<f64>,
    laid_w: Cell<i32>,
    pub selected: RefCell<Option<String>>,
    path: RefCell<Option<Path>>,
    installed: RefCell<Option<Rc<Vec<AppEntry>>>>,
}

impl FlowView {
    pub fn new() -> Self {
        let root = gtk::Stack::new();
        let off = vbox(0);
        widgets::margins(&off, 18, 22, 18, 22);
        off.append(&super::placeholder(&tr!("The mixer is off – set it up to give every app its own channel.")));
        root.add_named(&off, Some("off"));
        let on = vbox(0);
        let fixed = gtk::Fixed::new();
        let area = gtk::DrawingArea::new();
        fixed.put(&area, 0.0, 0.0);
        let scroll = widgets::scroll(&fixed);
        scroll.set_vexpand(true);
        on.append(&scroll);
        on.append(&legend());
        root.add_named(&on, Some("on"));
        root.set_visible_child_name("off");
        Self {
            root,
            scroll,
            fixed,
            area,
            graph: RefCell::new(Graph::default()),
            nodes: RefCell::new(Vec::new()),
            chips: RefCell::new(Vec::new()),
            pos: RefCell::new(Vec::new()),
            node_w: Cell::new(230.0),
            laid_w: Cell::new(-1),
            selected: RefCell::new(None),
            path: RefCell::new(None),
            installed: RefCell::new(None),
        }
    }

    pub fn has_selection(&self) -> bool {
        self.selected.borrow().is_some()
    }
}

/// Legende unten: farbige Linien für die Verbindungsarten.
fn legend() -> gtk::Box {
    let row = hbox(22);
    widgets::margins(&row, 8, 22, 12, 22);
    for (text, color, dashed) in [(tr!("Input to channel"), CYAN, false), (tr!("Channel to mix"), MAGENTA, false),
                                  (tr!("Mix to output"), GREEN, false), (tr!("Muted"), MUTED_LINE, true),
                                  (tr!("Not in path"), "#6a6a6a", false)] {
        let item = hbox(8);
        let line = gtk::DrawingArea::new();
        line.set_content_width(18);
        line.set_content_height(10);
        line.set_valign(Align::Center);
        line.set_draw_func(move |_, cr, w, h| {
            theme::set_source(cr, color);
            cr.set_line_width(2.0);
            if dashed {
                cr.set_dash(&[4.0, 3.0], 0.0);
            }
            cr.move_to(0.0, h as f64 / 2.0);
            cr.line_to(w as f64, h as f64 / 2.0);
            let _ = cr.stroke();
        });
        item.append(&line);
        item.append(&label(&text, &["legend"]));
        row.append(&item);
    }
    row
}

pub fn init(w: &Rc<Win>) {
    let w2 = w.clone();
    w.flow.area.set_draw_func(move |_, cr, _, _| draw(&w2, cr));
    // installierte Programme (für Programmsymbole) einmal im Hintergrund lesen
    let w2 = w.clone();
    super::bg(|| apps::app_entries(&apps::app_dirs()), move |list| {
        *w2.flow.installed.borrow_mut() = Some(Rc::new(list));
        let s = w2.snap.borrow().clone();
        *w2.flow.graph.borrow_mut() = Graph::default(); // neu bauen, jetzt mit Symbolen
        apply(&w2, &s);
    });
}

pub fn apply_off(w: &Rc<Win>) {
    let v = &w.flow;
    v.root.set_visible_child_name("off");
    if !v.graph.borrow().nodes.is_empty() {
        *v.graph.borrow_mut() = Graph::default();
        rebuild(w, &Snapshot::default());
    }
}

/// Neuer Zustand: nur neu bauen, wenn sich der Graph geändert hat.
pub fn apply(w: &Rc<Win>, s: &Arc<Snapshot>) {
    let v = &w.flow;
    if !s.active {
        apply_off(w);
        return;
    }
    v.root.set_visible_child_name("on");
    let g = flow::build(s);
    if *v.graph.borrow() == g && !v.nodes.borrow().is_empty() {
        return;
    }
    *v.graph.borrow_mut() = g;
    rebuild(w, s);
}

/// Breite der Ansicht geändert? Dann Spalten neu anordnen (aus dem Takt des Fensters).
pub fn check_width(w: &Rc<Win>) {
    if *w.view.borrow() != "flow" {
        return;
    }
    let width = w.flow.scroll.width();
    if width > 0 && width != w.flow.laid_w.get() {
        layout(w);
    }
}

fn sub_text(sub: &Sub) -> String {
    match sub {
        Sub::None | Sub::Empty => String::new(),
        Sub::Software => tr!("Software"),
        Sub::Hardware => tr!("Hardware"),
        Sub::Percent(v) => format!("{v} %"),
        Sub::Volume(v) => tr!("Volume {value} %", value = v),
        Sub::Muted => tr!("Muted"),
    }
}

/// Symbol eines Programms aus dem Symbolthema (Programmschlüssel oder Icon= der .desktop-Datei).
fn app_icon(w: &Win, key: &str) -> Option<gtk::Image> {
    let display = gtk::gdk::Display::default()?;
    let theme_ = gtk::IconTheme::for_display(&display);
    let installed = w.flow.installed.borrow().clone();
    let entry_icon = installed.as_ref().and_then(|l| apps::entry_for(key, l)).map(|e| e.icon.clone()).unwrap_or_default();
    let img = if entry_icon.starts_with('/') && std::path::Path::new(&entry_icon).is_file() {
        gtk::Image::from_file(&entry_icon)
    } else {
        let name = [entry_icon.as_str(), key].into_iter().find(|n| !n.is_empty() && theme_.has_icon(n))?;
        gtk::Image::from_icon_name(name)
    };
    img.set_pixel_size(ICON);
    Some(img)
}

/// Bildchen links im Element.
fn node_icon(w: &Win, s: &Snapshot, n: &flow::Node) -> gtk::Widget {
    let plain = |name: &str| {
        let g = theme::glyph(name, 18, theme::MUTED);
        // Platz wie eine Kachel, Symbol in der Mitte
        let pad = (ICON - 18) / 2;
        widgets::margins(&g, pad, pad, pad, pad);
        g.set_valign(Align::Center);
        g.upcast::<gtk::Widget>()
    };
    match n.col {
        Col::Channel => {
            let cid = n.id.trim_start_matches("ch:");
            let (glyph, color) = s.channel(cid).map(|c| channel_look(&c.channel))
                .unwrap_or_else(|| ("wave".into(), theme::FAINT.into()));
            theme::tile(&glyph, ICON, &color, 0.22, "#ffffff").upcast()
        }
        Col::Input if n.sub == Sub::Software => match app_icon(w, &n.app) {
            Some(i) => i.upcast(),
            None => theme::tile("wave", ICON, theme::RAISED, 0.22, "#ffffff").upcast(),
        },
        Col::Mix => plain(if n.icon.is_empty() { "wave" } else { &n.icon }),
        Col::Output => plain(if n.sub == Sub::Muted { "speaker_off" } else { "speaker" }),
        _ => plain(if n.icon.is_empty() { "speaker" } else { &n.icon }),
    }
}

/// Elemente und Spaltenköpfe neu anlegen.
fn rebuild(w: &Rc<Win>, s: &Snapshot) {
    let v = &w.flow;
    for b in v.nodes.borrow_mut().drain(..) {
        v.fixed.remove(&b);
    }
    for (_, c) in v.chips.borrow_mut().drain(..) {
        v.fixed.remove(&c);
    }
    let g = v.graph.borrow().clone();
    if g.nodes.is_empty() {
        v.area.queue_draw();
        return;
    }
    for (col, text, icon) in [(Col::Input, tr!("Inputs"), "mic"), (Col::Channel, tr!("Channels"), "speaker"),
                              (Col::Mix, tr!("Mixes"), "sliders"), (Col::Output, tr!("Outputs"), "volume_up")] {
        let chip = hbox(6);
        chip.add_css_class("flow-chip");
        chip.append(&theme::glyph(icon, 12, theme::MUTED));
        chip.append(&label(&text.to_uppercase(), &[]));
        v.fixed.put(&chip, 0.0, 0.0);
        v.chips.borrow_mut().push((col, chip));
    }
    let mut nodes = Vec::new();
    for n in &g.nodes {
        let b = gtk::Button::new();
        b.add_css_class("flow-node");
        b.set_cursor_from_name(Some("pointer"));
        let row = hbox(10);
        row.append(&node_icon(w, s, n));
        let txt = vbox(1);
        txt.set_valign(Align::Center);
        txt.set_hexpand(true);
        let title = if n.sub == Sub::Empty { tr!("Empty") } else { n.title.clone() };
        let t = label(&title, &["flow-title"]);
        t.set_ellipsize(gtk::pango::EllipsizeMode::End);
        txt.append(&t);
        let sub = sub_text(&n.sub);
        if !sub.is_empty() {
            txt.append(&label(&sub, &["flow-sub"]));
        }
        b.set_tooltip_text(Some(&title));
        row.append(&txt);
        b.set_child(Some(&row));
        let (w2, id) = (w.clone(), n.id.clone());
        b.connect_clicked(move |_| {
            let same = w2.flow.selected.borrow().as_deref() == Some(id.as_str());
            select(&w2, if same { None } else { Some(id.clone()) });
        });
        v.fixed.put(&b, 0.0, 0.0);
        nodes.push(b);
    }
    *v.nodes.borrow_mut() = nodes;
    // Auswahl behalten, solange es das Element noch gibt
    let keep = v.selected.borrow().clone().filter(|id| g.find(id).is_some());
    v.laid_w.set(-1);
    layout(w);
    select(w, keep);
}

/// Spalten auf die Breite der Ansicht verteilen.
fn layout(w: &Rc<Win>) {
    let v = &w.flow;
    let width = v.scroll.width().max(600);
    v.laid_w.set(width);
    let inner = width as f64 - 2.0 * MARGIN - 2.0; // ohne waagerechten Rollbalken
    let nw = ((inner - 3.0 * 64.0) / 4.0).clamp(150.0, 250.0).floor();
    v.node_w.set(nw);
    let colx = |c: Col| {
        let i = match c {
            Col::Input => 0.0,
            Col::Channel => 1.0,
            Col::Mix => 2.0,
            Col::Output => 3.0,
        };
        (MARGIN + i * (inner - nw).max(0.0) / 3.0).round()
    };
    let g = v.graph.borrow();
    let mut pos = vec![(0.0, 0.0); g.nodes.len()];
    let mut rows = 0usize;
    for c in [Col::Input, Col::Channel, Col::Mix, Col::Output] {
        let list = g.column(c);
        rows = rows.max(list.len());
        for (r, i) in list.into_iter().enumerate() {
            pos[i] = (colx(c), TOP + r as f64 * ROW);
        }
    }
    for (i, b) in v.nodes.borrow().iter().enumerate() {
        if let Some(&(x, y)) = pos.get(i) {
            b.set_size_request(nw as i32, NODE_H as i32);
            v.fixed.move_(b, x, y);
        }
    }
    for (c, chip) in v.chips.borrow().iter() {
        let (_, cw, _, _) = chip.measure(gtk::Orientation::Horizontal, -1);
        v.fixed.move_(chip, (colx(*c) + (nw - cw as f64) / 2.0).round(), 6.0);
    }
    *v.pos.borrow_mut() = pos;
    v.area.set_content_width((width - 2).max(1));
    v.area.set_content_height((TOP + rows as f64 * ROW + 10.0) as i32);
    v.area.queue_draw();
}

/// Element auswählen (None: Auswahl aufheben) – Weg hervorheben, Rest abblenden.
pub fn select(w: &Rc<Win>, id: Option<String>) {
    let v = &w.flow;
    let g = v.graph.borrow();
    let path = id.as_deref().and_then(|i| g.find(i)).map(|i| g.path(i));
    let sel_idx = id.as_deref().and_then(|i| g.find(i));
    for (i, b) in v.nodes.borrow().iter().enumerate() {
        widgets::set_class(b, "sel", sel_idx == Some(i));
        widgets::set_class(b, "dim", path.as_ref().is_some_and(|p| !p.nodes.contains(&i)));
    }
    *v.selected.borrow_mut() = if path.is_some() { id } else { None };
    *v.path.borrow_mut() = path;
    drop(g);
    v.area.queue_draw();
    w.refresh_header();
}

/// Verbindungen zeichnen: erst die abgeblendeten, darüber der hervorgehobene Weg.
fn draw(w: &Rc<Win>, cr: &gtk::cairo::Context) {
    let v = &w.flow;
    let g = v.graph.borrow();
    let pos = v.pos.borrow();
    let path = v.path.borrow();
    let nw = v.node_w.get();
    if pos.len() != g.nodes.len() {
        return;
    }
    cr.set_line_cap(gtk::cairo::LineCap::Round);
    for pass in [false, true] {
        for (i, e) in g.edges.iter().enumerate() {
            let on_path = path.as_ref().map(|p| p.edges.contains(&i)).unwrap_or(true);
            if on_path != pass {
                continue;
            }
            let (x0, y0) = (pos[e.from].0 + nw, pos[e.from].1 + NODE_H / 2.0);
            let (x1, y1) = (pos[e.to].0, pos[e.to].1 + NODE_H / 2.0);
            let color = if !on_path {
                FAINT_LINE
            } else if e.muted {
                MUTED_LINE
            } else {
                match e.link {
                    Link::InputChannel => CYAN,
                    Link::ChannelMix => MAGENTA,
                    Link::MixOutput => GREEN,
                }
            };
            theme::set_source(cr, color);
            cr.set_line_width(if path.is_some() && on_path { 2.5 } else { 1.8 });
            if e.muted {
                cr.set_dash(&[5.0, 4.0], 0.0);
            } else {
                cr.set_dash(&[], 0.0);
            }
            let mid = (x1 - x0) / 2.0;
            cr.move_to(x0, y0);
            cr.curve_to(x0 + mid, y0, x1 - mid, y1, x1, y1);
            let _ = cr.stroke();
            if path.is_some() && on_path {
                // Punkte an den Enden des hervorgehobenen Wegs
                cr.set_dash(&[], 0.0);
                for (x, y) in [(x0, y0), (x1, y1)] {
                    cr.arc(x, y, 3.5, 0.0, std::f64::consts::TAU);
                    let _ = cr.fill();
                }
            }
        }
    }
}
