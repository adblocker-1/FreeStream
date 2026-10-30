//! Bausteine des Fensters, gezeichnet mit Cairo wie in `freestream/theme.py` und `waveui.py`: Regler
//! (Fader), Pegel (LevelMeter), runde Knöpfe (RoundToggle, MuteToggle, HearToggle, LinkToggle, FxButton),
//! Schalter (Switch) – dazu Knöpfe, Menüs, Seitenleisten-Einträge und Dialograhmen im FreeWave-Stil.

use std::cell::{Cell, RefCell};
use std::rc::Rc;

use gtk::prelude::*;
use gtk::{Align, Orientation};
use gtk4 as gtk;

use super::theme::{self, fill_rounded, mix, paint_glyph, set_source};

type Callback<T> = Rc<RefCell<Option<Box<dyn Fn(T)>>>>;

// ================================================================== Fader
/// Schieberegler wie in Wave Link: dünne graue Spur, weißer runder Knopf. `on_change` kommt bei jeder
/// Bewegung durch den Nutzer; [`Fader::set`] ruft ihn nicht auf.
#[derive(Clone)]
pub struct Fader {
    pub area: gtk::DrawingArea,
    st: Rc<FaderState>,
    cb: Callback<f64>,
}

struct FaderState {
    value: Cell<f64>,
    lo: f64,
    hi: f64,
    dimmed: Cell<bool>,
    enabled: Cell<bool>,
    drag_x: Cell<f64>,
}

impl Fader {
    pub fn new(width: i32, height: i32, lo: f64, hi: f64) -> Self {
        let area = gtk::DrawingArea::new();
        area.set_content_width(width.max(1));
        area.set_content_height(height);
        area.set_valign(Align::Center);
        let st = Rc::new(FaderState { value: Cell::new(lo), lo, hi, dimmed: Cell::new(false), enabled: Cell::new(true),
                                      drag_x: Cell::new(0.0) });
        let f = Self { area, st, cb: Rc::new(RefCell::new(None)) };
        let s = f.st.clone();
        f.area.set_draw_func(move |_, cr, w, h| Self::draw(&s, cr, w as f64, h as f64));
        let drag = gtk::GestureDrag::new();
        let me = f.clone();
        drag.connect_drag_begin(move |_, x, _| {
            me.st.drag_x.set(x);
            me.press(x);
        });
        let me = f.clone();
        drag.connect_drag_update(move |_, dx, _| me.press(me.st.drag_x.get() + dx));
        f.area.add_controller(drag);
        let scroll = gtk::EventControllerScroll::new(gtk::EventControllerScrollFlags::VERTICAL);
        let me = f.clone();
        scroll.connect_scroll(move |_, _, dy| {
            if !me.st.enabled.get() {
                return gtk::glib::Propagation::Proceed;
            }
            let step = (me.st.hi - me.st.lo) / 100.0 * 2.0;
            me.changed(me.st.value.get() + if dy < 0.0 { step } else { -step });
            gtk::glib::Propagation::Stop // das Mausrad gehört dem Regler: die Seite darunter nicht mitscrollen
        });
        f.area.add_controller(scroll);
        f
    }

    pub fn on_change(&self, f: impl Fn(f64) + 'static) {
        *self.cb.borrow_mut() = Some(Box::new(f));
    }

    pub fn get(&self) -> f64 {
        self.st.value.get()
    }

    pub fn set(&self, v: f64) {
        let v = v.clamp(self.st.lo, self.st.hi);
        if (v - self.st.value.get()).abs() > 1e-9 {
            self.st.value.set(v);
            self.area.queue_draw();
        }
    }

    pub fn set_dimmed(&self, d: bool) {
        if d != self.st.dimmed.get() {
            self.st.dimmed.set(d);
            self.area.queue_draw();
        }
    }

    pub fn set_enabled(&self, e: bool) {
        if e != self.st.enabled.get() {
            self.st.enabled.set(e);
            self.area.queue_draw();
        }
    }

    fn knob(h: f64) -> f64 {
        (h - 4.0).min(18.0)
    }

    fn press(&self, x: f64) {
        if !self.st.enabled.get() {
            return;
        }
        let w = self.area.width() as f64;
        let k = Self::knob(self.area.height() as f64);
        let (left, right) = (k / 2.0 + 2.0, w - k / 2.0 - 2.0);
        let f = ((x - left) / (right - left).max(1.0)).clamp(0.0, 1.0);
        self.changed(self.st.lo + f * (self.st.hi - self.st.lo));
    }

    fn changed(&self, v: f64) {
        let v = v.clamp(self.st.lo, self.st.hi);
        if (v - self.st.value.get()).abs() > 1e-9 {
            self.st.value.set(v);
            self.area.queue_draw();
            if let Some(cb) = self.cb.borrow().as_ref() {
                cb(v);
            }
        }
    }

    fn draw(s: &FaderState, cr: &gtk::cairo::Context, w: f64, h: f64) {
        let f = (s.value.get() - s.lo) / ((s.hi - s.lo).abs().max(1e-9));
        let th = 6.0;
        let knob = Self::knob(h);
        let (left, right) = (knob / 2.0 + 2.0, w - knob / 2.0 - 2.0);
        let x = left + f * (right - left);
        let y0 = (h - th) / 2.0;
        fill_rounded(cr, left - th / 2.0, y0, right - left + th, th, th / 2.0, theme::RAISED);
        if f > 0.0 && !s.dimmed.get() {
            // eingestellter Teil: etwas heller als die Spur
            fill_rounded(cr, left - th / 2.0, y0, x - left + th / 2.0, th, th / 2.0, &mix(theme::FG, theme::RAISED, 0.78));
        }
        let r = knob / 2.0;
        // weicher Schatten, darüber der Knopf
        cr.set_source_rgba(0.0, 0.0, 0.0, 110.0 / 255.0);
        cr.arc(x, h / 2.0 + 3.0, r, 0.0, std::f64::consts::TAU);
        let _ = cr.fill();
        set_source(cr, if s.dimmed.get() || !s.enabled.get() { theme::FAINT } else { "#f2f2f2" });
        cr.arc(x, h / 2.0 + 1.0, r, 0.0, std::f64::consts::TAU);
        let _ = cr.fill();
    }
}

// ================================================================== Pegel
/// Pegelanzeige waagerecht grün → gelb → rot; mit `color` einfarbig.
#[derive(Clone)]
pub struct Meter {
    pub area: gtk::DrawingArea,
    level: Rc<Cell<f64>>,
}

impl Meter {
    pub fn new(width: i32, height: i32, color: Option<&str>) -> Self {
        let area = gtk::DrawingArea::new();
        area.set_content_width(width.max(1));
        area.set_content_height(height);
        let level = Rc::new(Cell::new(0.0));
        let l = level.clone();
        let color = color.map(str::to_string);
        area.set_draw_func(move |_, cr, w, h| {
            let (w, h) = (w as f64, h as f64);
            fill_rounded(cr, 0.0, 0.0, w, h, h / 2.0, theme::RAISED);
            let lv = l.get();
            if lv <= 0.0 {
                return;
            }
            if let Some(c) = &color {
                fill_rounded(cr, 0.0, 0.0, (lv * w).max(h), h, h / 2.0, c);
                return;
            }
            theme::rounded(cr, 0.0, 0.0, w, h, h / 2.0);
            cr.clip();
            for (a, b, col) in theme::METER_ZONES {
                let (lo, hi) = (a * w, b.min(lv) * w);
                if hi > lo {
                    set_source(cr, col);
                    cr.rectangle(lo, 0.0, hi - lo, h);
                    let _ = cr.fill();
                }
            }
        });
        Self { area, level }
    }

    pub fn set(&self, v: f64) {
        let v = v.clamp(0.0, 1.0);
        if (v - self.level.get()).abs() > 0.004 {
            self.level.set(v);
            self.area.queue_draw();
        }
    }
}

// ================================================================== runde Knöpfe
/// Art eines kleinen, selbst gezeichneten Knopfs.
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum ToggleKind {
    /// Stumm-Knopf der Kanal-Karten: Symbol, beim Überfahren hinterlegt; an = rot (MuteToggle)
    Mute,
    /// Mithören: an = Akzentfläche mit weißem Symbol (HearToggle)
    Hear,
    /// runder Knopf mit Symbol (RoundToggle): an = Farbe `on_color`
    Round,
    /// Kette zwischen zwei Mix-Reglern (LinkToggle)
    Link,
    /// Effekte-Knopf in der Mixer-Tabelle: Regler-Symbol, an = grün (FxButton)
    Fx,
}

#[derive(Clone)]
pub struct Toggle {
    pub area: gtk::DrawingArea,
    on: Rc<Cell<bool>>,
    hover: Rc<Cell<bool>>,
    cb: Callback<bool>,
}

impl Toggle {
    /// `icons`: (Symbol aus, Symbol an); `on_color` für [`ToggleKind::Round`].
    pub fn new(kind: ToggleKind, w: i32, h: i32, icons: (&str, &str), on_color: &str) -> Self {
        let area = gtk::DrawingArea::new();
        area.set_content_width(w);
        area.set_content_height(h);
        area.set_valign(Align::Center);
        area.set_halign(Align::Center);
        area.set_cursor_from_name(Some("pointer"));
        let on = Rc::new(Cell::new(false));
        let hover = Rc::new(Cell::new(false));
        let (i_off, i_on, on_color) = (icons.0.to_string(), icons.1.to_string(), on_color.to_string());
        let (o, hv) = (on.clone(), hover.clone());
        area.set_draw_func(move |_, cr, w, h| {
            let (w, h) = (w as f64, h as f64);
            let (on, hover) = (o.get(), hv.get());
            match kind {
                ToggleKind::Mute => {
                    if hover {
                        fill_rounded(cr, 0.0, 0.0, w, h, 6.0, theme::RAISED);
                    }
                    let icon = if on { &i_on } else { &i_off };
                    paint_glyph(cr, icon, 18, if on { theme::DANGER } else { theme::FG }, w / 2.0, h / 2.0);
                }
                ToggleKind::Hear => {
                    if on || hover {
                        fill_rounded(cr, 0.0, 0.0, w, h, 6.0, if on { theme::ACCENT } else { theme::RAISED });
                    }
                    paint_glyph(cr, &i_off, 17, if on { "#ffffff" } else { theme::FG }, w / 2.0, h / 2.0);
                }
                ToggleKind::Round => {
                    let fill = if on { on_color.clone() } else if hover { mix(theme::RAISED, "#ffffff", 0.08) } else { theme::RAISED.into() };
                    set_source(cr, &fill);
                    cr.arc(w / 2.0, h / 2.0, w.min(h) / 2.0 - 1.0, 0.0, std::f64::consts::TAU);
                    let _ = cr.fill();
                    let icon = if on { &i_on } else { &i_off };
                    paint_glyph(cr, icon, (w.min(h) * 0.5) as i32, if on { "#ffffff" } else { theme::FG }, w / 2.0, h / 2.0);
                }
                ToggleKind::Link => {
                    let r = w.min(h) / 2.0 - 1.0;
                    if on || hover {
                        set_source(cr, if on { theme::ACCENT } else { theme::RAISED });
                        cr.arc(w / 2.0, h / 2.0, r, 0.0, std::f64::consts::TAU);
                        let _ = cr.fill();
                    }
                    paint_glyph(cr, if on { "link" } else { "link_off" }, 15, if on { "#ffffff" } else { theme::MUTED }, w / 2.0, h / 2.0);
                }
                ToggleKind::Fx => {
                    if hover {
                        fill_rounded(cr, 0.0, 0.0, w, h, 6.0, theme::RAISED);
                    }
                    paint_glyph(cr, "sliders", 16, if on { theme::OK } else { theme::FG }, w / 2.0, h / 2.0);
                }
            }
        });
        let t = Self { area, on, hover, cb: Rc::new(RefCell::new(None)) };
        let motion = gtk::EventControllerMotion::new();
        let me = t.clone();
        motion.connect_enter(move |_, _, _| me.set_hover(true));
        let me = t.clone();
        motion.connect_leave(move |_| me.set_hover(false));
        t.area.add_controller(motion);
        let click = gtk::GestureClick::new();
        click.set_button(1);
        let me = t.clone();
        click.connect_released(move |g, _, x, y| {
            let a = &me.area;
            if x < 0.0 || y < 0.0 || x > a.width() as f64 || y > a.height() as f64 {
                return;
            }
            g.set_state(gtk::EventSequenceState::Claimed);
            // Fx öffnet nur (kein Umschalten), alle anderen schalten um
            let new = if kind == ToggleKind::Fx { me.on.get() } else { !me.on.get() };
            me.on.set(new);
            me.area.queue_draw();
            if let Some(cb) = me.cb.borrow().as_ref() {
                cb(new);
            }
        });
        t.area.add_controller(click);
        t
    }

    fn set_hover(&self, v: bool) {
        self.hover.set(v);
        self.area.queue_draw();
    }

    pub fn on_toggle(&self, f: impl Fn(bool) + 'static) {
        *self.cb.borrow_mut() = Some(Box::new(f));
    }

    pub fn get(&self) -> bool {
        self.on.get()
    }

    pub fn set(&self, on: bool) {
        if on != self.on.get() {
            self.on.set(on);
            self.area.queue_draw();
        }
    }
}

// ================================================================== Schalter
/// Schalter an/aus wie in den Einstellungen von Wave Link.
#[derive(Clone)]
pub struct Switch {
    pub area: gtk::DrawingArea,
    on: Rc<Cell<bool>>,
    enabled: Rc<Cell<bool>>,
    cb: Callback<bool>,
}

impl Switch {
    pub fn new() -> Self {
        let area = gtk::DrawingArea::new();
        area.set_content_width(42);
        area.set_content_height(24);
        area.set_valign(Align::Center);
        area.set_halign(Align::End);
        area.set_cursor_from_name(Some("pointer"));
        let on = Rc::new(Cell::new(false));
        let enabled = Rc::new(Cell::new(true));
        let (o, e) = (on.clone(), enabled.clone());
        area.set_draw_func(move |_, cr, w, h| {
            let (w, h) = (w as f64, h as f64);
            let track = if !e.get() { theme::PANEL2 } else if o.get() { theme::ACCENT } else { theme::RAISED };
            fill_rounded(cr, 1.0, 1.0, w - 2.0, h - 2.0, h / 2.0, track);
            let r = h / 2.0 - 4.0;
            let cx = if o.get() { w - h / 2.0 } else { h / 2.0 };
            set_source(cr, if e.get() { "#ffffff" } else { theme::FAINT });
            cr.arc(cx, h / 2.0, r, 0.0, std::f64::consts::TAU);
            let _ = cr.fill();
        });
        let s = Self { area, on, enabled, cb: Rc::new(RefCell::new(None)) };
        let click = gtk::GestureClick::new();
        let me = s.clone();
        click.connect_released(move |_, _, _, _| {
            if !me.enabled.get() {
                return;
            }
            me.on.set(!me.on.get());
            me.area.queue_draw();
            if let Some(cb) = me.cb.borrow().as_ref() {
                cb(me.on.get());
            }
        });
        s.area.add_controller(click);
        s
    }

    pub fn on_toggle(&self, f: impl Fn(bool) + 'static) {
        *self.cb.borrow_mut() = Some(Box::new(f));
    }

    pub fn get(&self) -> bool {
        self.on.get()
    }

    pub fn set(&self, on: bool) {
        if on != self.on.get() {
            self.on.set(on);
            self.area.queue_draw();
        }
    }

    pub fn set_enabled(&self, e: bool) {
        if e != self.enabled.get() {
            self.enabled.set(e);
            self.area.queue_draw();
        }
    }
}

// ================================================================== Knöpfe, Texte
/// Label mit CSS-Klassen, links ausgerichtet.
pub fn label(text: &str, classes: &[&str]) -> gtk::Label {
    let l = gtk::Label::new(Some(text));
    l.set_xalign(0.0);
    for c in classes {
        l.add_css_class(c);
    }
    l
}

/// Mehrzeiliger Hinweis (umbrechend).
pub fn wrap(text: &str, classes: &[&str], chars: i32) -> gtk::Label {
    let l = label(text, classes);
    l.set_wrap(true);
    l.set_max_width_chars(chars);
    l.set_width_chars(chars.min(30));
    l
}

/// Kleine Abschnittsüberschrift in Großbuchstaben (theme.section).
pub fn section(text: &str) -> gtk::Label {
    label(&text.to_uppercase(), &["section"])
}

/// Knopf mit Symbol und Text; `kind`: "", "primary", "ghost", "danger-btn".
pub fn button(text: &str, icon: Option<&str>, kind: &str) -> gtk::Button {
    let b = gtk::Button::new();
    let row = gtk::Box::new(Orientation::Horizontal, 7);
    row.set_halign(Align::Center);
    if let Some(i) = icon {
        let color = match kind {
            "primary" => "#ffffff",
            "ghost" => theme::MUTED,
            "danger-btn" => theme::DANGER,
            _ => theme::FG,
        };
        row.append(&theme::glyph(i, 16, color));
    }
    if !text.is_empty() {
        row.append(&gtk::Label::new(Some(text)));
    }
    b.set_child(Some(&row));
    b.add_css_class("btn");
    if !kind.is_empty() {
        b.add_css_class(kind);
    }
    b.set_valign(Align::Center);
    b
}

/// Text eines mit [`button`] gebauten Knopfs ändern.
pub fn set_button_text(b: &gtk::Button, text: &str) {
    if let Some(row) = b.child() {
        let mut c = row.first_child();
        while let Some(w) = c {
            if let Ok(l) = w.clone().downcast::<gtk::Label>() {
                if l.text() != text {
                    l.set_text(text);
                }
                return;
            }
            c = w.next_sibling();
        }
    }
}

/// Filter-Chip (rund, mit Rand); aktiv = Akzent hinterlegt.
pub fn chip(text: &str, active: bool) -> gtk::Button {
    let b = gtk::Button::with_label(text);
    b.add_css_class("chip");
    if active {
        b.add_css_class("active");
    }
    b.set_valign(Align::Center);
    b
}

pub fn set_class(w: &impl IsA<gtk::Widget>, class: &str, on: bool) {
    if on {
        w.add_css_class(class);
    } else {
        w.remove_css_class(class);
    }
}

pub fn margins(w: &impl IsA<gtk::Widget>, top: i32, right: i32, bottom: i32, left: i32) {
    w.set_margin_top(top);
    w.set_margin_end(right);
    w.set_margin_bottom(bottom);
    w.set_margin_start(left);
}

pub fn hbox(spacing: i32) -> gtk::Box {
    gtk::Box::new(Orientation::Horizontal, spacing)
}

pub fn vbox(spacing: i32) -> gtk::Box {
    gtk::Box::new(Orientation::Vertical, spacing)
}

pub fn spacer() -> gtk::Box {
    let b = hbox(0);
    b.set_hexpand(true);
    b
}

pub fn sep() -> gtk::Box {
    let b = hbox(0);
    b.add_css_class("sep");
    b
}

/// Alle Kinder eines Containers entfernen.
pub fn clear(b: &gtk::Box) {
    while let Some(c) = b.first_child() {
        b.remove(&c);
    }
}

/// Weiß gefärbter Rand-Rahmen: Karte mit Hintergrund.
pub fn card(classes: &[&str]) -> gtk::Box {
    let b = vbox(0);
    b.add_css_class("card");
    for c in classes {
        b.add_css_class(c);
    }
    b
}

/// Segmentierte Auswahl (SoftSegmented): gibt den Rahmen und die Knöpfe zurück.
pub fn segmented(items: &[(&str, String, Option<&str>)], value: &str, on_pick: impl Fn(String) + 'static) -> (gtk::Box, Vec<gtk::ToggleButton>) {
    let b = hbox(0);
    b.add_css_class("seg");
    b.set_homogeneous(true);
    let on_pick = Rc::new(on_pick);
    let mut group: Option<gtk::ToggleButton> = None;
    let mut btns = Vec::new();
    for (key, text, icon) in items {
        let t = gtk::ToggleButton::new();
        let row = hbox(6);
        row.set_halign(Align::Center);
        if let Some(i) = icon {
            row.append(&theme::glyph(i, 13, theme::FG));
        }
        row.append(&gtk::Label::new(Some(text)));
        t.set_child(Some(&row));
        t.add_css_class("seg-btn");
        match &group {
            Some(g) => t.set_group(Some(g)),
            None => group = Some(t.clone()),
        }
        t.set_active(*key == value);
        let (k, f) = (key.to_string(), on_pick.clone());
        t.connect_toggled(move |t| {
            if t.is_active() {
                f(k.clone());
            }
        });
        b.append(&t);
        btns.push(t);
    }
    (b, btns)
}

// ================================================================== Menüs
/// Eintrag eines Menüs (PopupMenu); `None` = Trenner.
pub struct MenuItem {
    pub label: String,
    pub icon: Option<String>,
    pub danger: bool,
    pub action: Option<Box<dyn Fn()>>,
}

pub fn item(label: &str, icon: &str, action: impl Fn() + 'static) -> Option<MenuItem> {
    Some(MenuItem { label: label.to_string(), icon: (!icon.is_empty()).then(|| icon.to_string()), danger: false,
                    action: Some(Box::new(action)) })
}

/// Menü im Design von FreeWave an `parent` öffnen – an der Stelle (x, y) im Widget oder darunter.
pub fn popup(parent: &impl IsA<gtk::Widget>, at: Option<(f64, f64)>, items: Vec<Option<MenuItem>>, width: i32) {
    let pop = gtk::Popover::new();
    pop.set_has_arrow(false);
    pop.set_position(gtk::PositionType::Bottom);
    let b = vbox(0);
    b.set_size_request(width, -1);
    for it in items {
        let Some(it) = it else {
            let s = sep();
            margins(&s, 4, 4, 4, 4);
            b.append(&s);
            continue;
        };
        let btn = gtk::Button::new();
        btn.add_css_class("menu-row");
        if it.danger {
            btn.add_css_class("danger");
        }
        let row = hbox(10);
        if let Some(i) = &it.icon {
            let color = if it.action.is_none() { theme::FAINT } else if it.danger { theme::DANGER } else { theme::MUTED };
            row.append(&theme::glyph(i, 16, color));
        }
        let l = label(&it.label, &[]);
        l.set_ellipsize(gtk::pango::EllipsizeMode::End);
        row.append(&l);
        btn.set_child(Some(&row));
        match it.action {
            Some(a) => {
                let p = pop.clone();
                btn.connect_clicked(move |_| {
                    p.popdown();
                    a();
                });
            }
            None => btn.set_sensitive(false),
        }
        b.append(&btn);
    }
    pop.set_child(Some(&b));
    pop.set_parent(parent);
    if let Some((x, y)) = at {
        pop.set_pointing_to(Some(&gtk::gdk::Rectangle::new(x as i32, y as i32, 1, 1)));
    }
    pop.connect_closed(|p| {
        let p = p.clone();
        gtk::glib::idle_add_local_once(move || p.unparent());
    });
    pop.popup();
}

/// Rechtsklick auf `w` ruft `f(x, y)` (Koordinaten im Widget).
pub fn on_right_click(w: &impl IsA<gtk::Widget>, f: impl Fn(f64, f64) + 'static) {
    let g = gtk::GestureClick::new();
    g.set_button(3);
    g.connect_pressed(move |_, _, x, y| f(x, y));
    w.add_controller(g);
}

/// Linksklick auf ein beliebiges Widget.
pub fn on_click(w: &impl IsA<gtk::Widget>, f: impl Fn() + 'static) {
    let g = gtk::GestureClick::new();
    g.set_button(1);
    g.connect_released(move |_, _, _, _| f());
    w.add_controller(g);
    w.set_cursor_from_name(Some("pointer"));
}

// ================================================================== Seitenleiste
/// Eintrag der Seitenleiste: Symbol, Text, rechts eine Zahl; gewählt = hinterlegt mit Akzentstrich.
pub struct NavItem {
    pub button: gtk::Button,
    count: gtk::Label,
}

impl NavItem {
    pub fn new(text: &str, icon: &str, accent: bool) -> Self {
        let button = gtk::Button::new();
        button.add_css_class("nav");
        if accent {
            button.add_css_class("accent");
        }
        let row = hbox(0);
        let bar = hbox(0);
        bar.add_css_class("nav-bar");
        bar.set_valign(Align::Center);
        row.append(&bar);
        let ic = theme::glyph(icon, 16, if accent { theme::ACCENT } else { theme::MUTED });
        ic.set_margin_start(8);
        row.append(&ic);
        let l = label(text, &[]);
        l.set_margin_start(12);
        l.set_hexpand(true);
        row.append(&l);
        let count = label("", &["nav-count"]);
        row.append(&count);
        button.set_child(Some(&row));
        Self { button, count }
    }

    pub fn set_active(&self, on: bool) {
        set_class(&self.button, "active", on);
    }

    pub fn set_count(&self, n: usize) {
        let t = if n == 0 { String::new() } else { n.to_string() };
        if self.count.text() != t {
            self.count.set_text(&t);
        }
    }
}

/// Listeneintrag mit Bild links, Titel und Untertitel (Mikrofon, Mixe) – InfoCard.
pub struct InfoCard {
    pub button: gtk::Button,
    title: gtk::Label,
    sub: gtk::Label,
    dot: gtk::Box,
}

impl InfoCard {
    pub fn new(title: &str, sub: &str, image: gtk::Image) -> Self {
        let button = gtk::Button::new();
        button.add_css_class("info");
        let row = hbox(10);
        let img = hbox(0);
        img.append(&image);
        row.append(&img);
        let col = vbox(1);
        col.set_valign(Align::Center);
        col.set_hexpand(true);
        let t = label(title, &["info-title"]);
        t.set_ellipsize(gtk::pango::EllipsizeMode::End);
        col.append(&t);
        let line = hbox(5);
        let dot = hbox(0);
        dot.add_css_class("dot");
        dot.set_valign(Align::Center);
        dot.set_visible(false);
        line.append(&dot);
        let s = label(sub, &["info-sub"]);
        s.set_ellipsize(gtk::pango::EllipsizeMode::End);
        s.set_max_width_chars(20);
        line.append(&s);
        col.append(&line);
        row.append(&col);
        button.set_child(Some(&row));
        Self { button, title: t, sub: s, dot }
    }

    /// Nur den Untertitel ändern (Titel bleibt).
    pub fn set_sub(&self, sub: &str, dim: bool) {
        if self.sub.text() != sub {
            self.sub.set_text(sub);
        }
        set_class(&self.button, "dim", dim);
    }

    /// dot: "" (keiner), "ok" oder "danger".
    pub fn set(&self, title: &str, sub: &str, dot: &str, dim: bool) {
        if self.title.text() != title {
            self.title.set_text(title);
        }
        if self.sub.text() != sub {
            self.sub.set_text(sub);
        }
        self.dot.set_visible(!dot.is_empty());
        set_class(&self.dot, "dot-ok", dot == "ok");
        set_class(&self.dot, "dot-danger", dot == "danger");
        set_class(&self.button, "dim", dim);
    }
}

// ================================================================== Dialoge
/// Dialogfenster im FreeWave-Stil: Inhalt oben, Fußzeile mit feiner Linie (dialogkit.footer).
pub struct Dialog {
    pub win: gtk::Window,
    pub body: gtk::Box,
    pub left: gtk::Box,
    pub buttons: gtk::Box,
}

pub fn dialog(parent: &gtk::Window, title: &str, width: i32, height: i32) -> Dialog {
    let win = gtk::Window::builder().title(title).transient_for(parent).modal(true).default_width(width).build();
    if height > 0 {
        win.set_default_height(height);
    }
    let outer = vbox(0);
    outer.add_css_class("main");
    let body = vbox(0);
    margins(&body, 20, 22, 16, 22);
    body.set_vexpand(true);
    outer.append(&body);
    let foot = hbox(8);
    foot.add_css_class("dialog-foot");
    margins(&foot, 14, 22, 14, 22);
    let left = hbox(8);
    left.set_hexpand(true);
    foot.append(&left);
    let buttons = hbox(8);
    foot.append(&buttons);
    outer.append(&foot);
    win.set_child(Some(&outer));
    let esc = gtk::EventControllerKey::new();
    let w = win.clone();
    esc.connect_key_pressed(move |_, key, _, _| {
        if key == gtk::gdk::Key::Escape {
            w.close();
            return gtk::glib::Propagation::Stop;
        }
        gtk::glib::Propagation::Proceed
    });
    win.add_controller(esc);
    Dialog { win, body, left, buttons }
}

/// Titel mit Symbol-Kachel links und grauer Unterzeile (dialogkit.header); gibt die Kachel zurück.
pub fn header(parent: &gtk::Box, title: &str, subtitle: &str, icon: Option<(&str, &str)>, wrap_chars: i32) -> Option<gtk::Image> {
    let row = hbox(14);
    row.set_margin_bottom(16);
    let tile = icon.map(|(i, c)| {
        let t = theme::tile(i, 40, c, 0.28, "#ffffff");
        t.set_valign(Align::Start);
        row.append(&t);
        t
    });
    let txt = vbox(3);
    txt.set_hexpand(true);
    txt.append(&label(title, &["h-title"]));
    if !subtitle.is_empty() {
        txt.append(&wrap(subtitle, &["muted", "small"], wrap_chars));
    }
    row.append(&txt);
    parent.append(&row);
    tile
}

/// Abschnittsüberschrift über einem Eingabefeld (dialogkit.field).
pub fn field(parent: &gtk::Box, text: &str) -> gtk::Label {
    let l = section(text);
    l.set_margin_bottom(6);
    parent.append(&l);
    l
}

/// Einzeiliges Eingabefeld.
pub fn entry(text: &str) -> gtk::Entry {
    let e = gtk::Entry::new();
    e.set_text(text);
    e
}

/// Ja/Nein-Frage (msgbox.askyesno).
pub fn confirm(parent: &gtk::Window, title: &str, text: &str, on_yes: impl Fn() + 'static) {
    let d = dialog(parent, title, 420, -1);
    d.body.append(&wrap(text, &[], 50));
    let no = button(&freestream_core::i18n::gettext("No"), None, "");
    let yes = button(&freestream_core::i18n::gettext("Yes"), None, "primary");
    d.buttons.append(&no);
    d.buttons.append(&yes);
    let w = d.win.clone();
    no.connect_clicked(move |_| w.close());
    let w = d.win.clone();
    yes.connect_clicked(move |_| {
        w.close();
        on_yes();
    });
    d.win.present();
}

/// Bildlauf-Fläche (senkrecht) mit Inhalt.
pub fn scroll(child: &impl IsA<gtk::Widget>) -> gtk::ScrolledWindow {
    let sc = gtk::ScrolledWindow::new();
    sc.set_hscrollbar_policy(gtk::PolicyType::Never);
    sc.set_child(Some(child));
    sc.set_vexpand(true);
    sc
}
