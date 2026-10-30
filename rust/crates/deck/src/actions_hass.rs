//! Home-Assistant-Aktionen (`freestream/hasskeys.py`): Entität schalten (Drehregler: Helligkeit,
//! Temperatur, Position …), Dienst aufrufen, Szene/Skript starten, Zustand anzeigen.
//!
//! Die Tasten zeichnen ihre Symbole selbst und zeigen den Zustand farbig. Zustände kommen aus dem
//! Zwischenspeicher des Clients, Dienstaufrufe laufen in seinem Arbeits-Thread – Tastendruck und
//! Zeichnen blockieren nie. Fehler meldet der Arbeits-Thread über die Outbox (Warnsymbol).

use freestream_core::{pgettext, tr};
use image::{Rgba, RgbaImage};
use serde_json::{Map, Value};
use std::sync::{Arc, Mutex};

use crate::actions::{ActionData, Behavior, Command, Env};
use crate::hass::{self, Call, Client, ErrorKind};
use crate::livedraw as d;
use crate::native;

pub const PREFIX: &str = "com.lindeck.homeassistant.";
const OFF_BG: [u8; 3] = [40, 40, 46];
const ERR_BG: [u8; 3] = [110, 30, 30];
const IDLE_BG: [u8; 3] = [16, 78, 122];
const ON_BG: [u8; 3] = [22, 120, 64];
const MUTED: [u8; 3] = [200, 200, 210];

fn on_color(domain: &str) -> [u8; 3] {
    match domain {
        "light" => [196, 138, 22],
        "cover" => [28, 104, 176],
        "climate" => [196, 84, 32],
        "fan" => [16, 128, 140],
        "media_player" => [98, 70, 180],
        "lock" => [22, 120, 64],
        _ => ON_BG,
    }
}

fn glyph_for(domain: &str) -> Option<&'static str> {
    Some(match domain {
        "light" => "bulb",
        "switch" | "input_boolean" | "automation" | "button" | "input_button" => "power",
        "fan" => "fan",
        "cover" => "cover",
        "climate" | "weather" => "thermo",
        "sensor" | "binary_sensor" | "input_number" | "number" => "gauge",
        "lock" => "lock",
        "scene" => "spark",
        "script" | "media_player" => "play",
        _ => return None,
    })
}

// ---------------------------------------------------------------------------------------- Zeichnen
/// Symbol als SVG (Fläche 100×100, Farbe `c`) – nachgezeichnet nach `draw_glyph` in Python.
fn glyph_svg(kind: &str, c: &str) -> String {
    let s = format!(r#"fill="none" stroke="{c}" stroke-width="8" stroke-linecap="round" stroke-linejoin="round""#);
    let f = format!(r#"fill="{c}""#);
    let body = match kind {
        "bulb" => format!(r#"<g {s}><circle cx="50" cy="34" r="30"/><path d="M36 60V76M64 60V76M34 78H66M38 90H62"/></g>"#),
        "power" => format!(r#"<g {s}><path d="M69 19.1A38 38 0 1 1 31 19.1"/><path d="M50 4V48"/></g>"#),
        "fan" => format!(r#"<g {f}><circle cx="76" cy="50" r="20"/><circle cx="37" cy="72.5" r="20"/><circle cx="37" cy="27.5" r="20"/></g><circle cx="50" cy="50" r="8" fill="black" fill-opacity="0.63"/>"#),
        "cover" => format!(r#"<g {s}><rect x="10" y="6" width="80" height="88"/><path d="M18 28H82M18 46H82M18 64H82" stroke-width="5"/></g>"#),
        "thermo" => format!(r#"<g {s}><rect x="40" y="4" width="20" height="62" rx="10"/><path d="M50 30V70"/></g><ellipse cx="50" cy="78" rx="20" ry="18" {f}/>"#),
        "gauge" => format!(r#"<g {s}><path d="M6 60A44 44 0 0 1 94 60"/><path d="M50 60L76 32"/></g><circle cx="50" cy="60" r="8" {f}/>"#),
        "lock" => format!(r#"<g {s}><path d="M26 46V30A24 24 0 0 1 74 30V46"/></g><rect x="14" y="44" width="72" height="52" rx="8" {f}/>"#),
        "spark" => format!(r#"<g {f}><path d="M42 6L54 40L88 52L54 64L42 98L30 64L0 52L30 40Z"/><path d="M82 2L86 14L98 18L86 22L82 34L78 22L66 18L78 14Z"/></g>"#),
        "play" => format!(r#"<path d="M22 8L22 92L88 50Z" {f}/>"#),
        "bolt" => format!(r#"<path d="M58 2L16 56L46 56L38 98L84 40L54 40Z" {f}/>"#),
        "alert" => format!(r#"<g {s}><path d="M50 4L2 92H98Z"/><path d="M50 34V62"/></g><circle cx="50" cy="77" r="5" {f}/>"#),
        _ => format!(r#"<path fill-rule="evenodd" d="M50 4L96 46H84V94H16V46H4ZM42 62V94H58V62Z" {f}/>"#),
    };
    format!(r#"<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100" height="100">{body}</svg>"#)
}

/// Symbol in der Größe `size` als Bild.
pub fn glyph_image(kind: &str, size: u32, color: [u8; 3]) -> Option<RgbaImage> {
    let svg = glyph_svg(kind, &format!("#{:02x}{:02x}{:02x}", color[0], color[1], color[2]));
    let tree = resvg::usvg::Tree::from_str(&svg, &resvg::usvg::Options::default()).ok()?;
    let size = size.max(1);
    let mut pix = resvg::tiny_skia::Pixmap::new(size, size)?;
    let k = size as f32 / 100.0;
    resvg::render(&tree, resvg::tiny_skia::Transform::from_scale(k, k), &mut pix.as_mut());
    let mut img = RgbaImage::new(size, size);
    for (i, p) in pix.pixels().iter().enumerate() {
        let c = p.demultiply();
        img.put_pixel(i as u32 % size, i as u32 / size, Rgba([c.red(), c.green(), c.blue(), c.alpha()]));
    }
    Some(img)
}

fn put_glyph(img: &mut RgbaImage, kind: &str, x: f32, y: f32, size: f32, color: [u8; 3]) {
    if let Some(g) = glyph_image(kind, size.round() as u32, color) {
        image::imageops::overlay(img, &g, x.round() as i64, y.round() as i64);
    }
}

/// Taste: Symbol oben, Name und Wert darunter (`big`: Wert groß in der Mitte).
pub fn draw_key(size: (u32, u32), glyph: &str, bg: [u8; 3], name: &str, value: &str, big: bool) -> RgbaImage {
    let (w, h) = (size.0 as f32, size.1 as f32);
    let mut img = d::canvas(size, bg);
    if big {
        let g = h * 0.22;
        put_glyph(&mut img, glyph, w * 0.08, h * 0.07, g, MUTED);
        d::text(&mut img, w / 2.0, h * 0.52, value, w - 6.0, h * 0.3, [255, 255, 255], true);
        d::text(&mut img, w / 2.0, h * 0.84, name, w - 6.0, h * 0.14, MUTED, false);
        return img;
    }
    let texts = !name.is_empty() || !value.is_empty();
    let g = h * if texts { 0.36 } else { 0.5 };
    let top = h * if texts { 0.1 } else { 0.25 };
    put_glyph(&mut img, glyph, (w - g) / 2.0, top, g, [255, 255, 255]);
    let mut y = top + g + h * 0.12;
    if !name.is_empty() {
        d::text(&mut img, w / 2.0, y, name, w - 6.0, h * 0.15, [255, 255, 255], true);
        y += h * 0.17;
    }
    d::text(&mut img, w / 2.0, y.min(h * 0.9), value, w - 6.0, h * 0.13, MUTED, false);
    img
}

// ---------------------------------------------------------------------------------------- Aktionen
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    /// Entität schalten; zeigt An/Aus farbig, bei Lampen die Helligkeit
    Toggle,
    /// beliebigen Dienst aufrufen (domain.service, Entität, JSON-Daten)
    Service,
    /// Szene aktivieren oder Skript starten
    Scene,
    /// Wert einer Entität anzeigen (z. B. Temperatur mit Einheit)
    State,
}

pub fn kind_for(uuid: &str) -> Option<Kind> {
    Some(match uuid.to_lowercase().strip_prefix(PREFIX)? {
        "toggle" => Kind::Toggle,
        "service" => Kind::Service,
        "scene" => Kind::Scene,
        "state" => Kind::State,
        _ => return None,
    })
}

pub struct HaAction {
    kind: Kind,
    snap: Option<(u64, Option<ErrorKind>, Option<Value>)>,
    /// letzte Drehung, noch nicht gesendet (schnelle Drehungen werden ein Aufruf)
    dial: Arc<Mutex<Option<Call>>>,
}

impl HaAction {
    pub fn new(kind: Kind) -> Self {
        Self { kind, snap: None, dial: Arc::new(Mutex::new(None)) }
    }

    fn entity(a: &ActionData) -> String {
        a.setting_str("entity_id").trim().to_string()
    }

    fn st(c: &Arc<Client>, a: &ActionData) -> Option<Value> {
        let e = Self::entity(a);
        if e.is_empty() { None } else { c.state(&e) }
    }

    /// Kurzer Fehlertext für die Taste oder "".
    fn problem(c: &Client, a: &ActionData) -> String {
        if !c.configured() {
            return tr!("Set up");
        }
        match c.error_kind() {
            Some(ErrorKind::Auth) => return tr!("Token?"),
            Some(_) => return tr!("Offline"),
            None => {}
        }
        let e = Self::entity(a);
        if !e.is_empty() && !c.known(&e) {
            return tr!("Unknown");
        }
        String::new()
    }

    fn glyph(&self, a: &ActionData) -> &'static str {
        let e = Self::entity(a);
        let dom = hass::domain_of(&e);
        match self.kind {
            Kind::Service => "bolt",
            Kind::Scene => if dom == "script" { "play" } else { "spark" },
            Kind::Toggle => glyph_for(dom).unwrap_or("power"),
            Kind::State => glyph_for(dom).unwrap_or("gauge"),
        }
    }

    fn display_name(&self, st: Option<&Value>, a: &ActionData) -> String {
        let e = Self::entity(a);
        let name = if e.is_empty() { String::new() } else { hass::friendly_name(st, &e) };
        if name.is_empty() && self.kind == Kind::Service { a.setting_str("service") } else { name }
    }

    fn active(&self, st: Option<&Value>, a: &ActionData) -> Option<bool> {
        let e = Self::entity(a);
        match self.kind {
            Kind::Toggle => hass::is_on(st),
            Kind::Service if !e.is_empty() => hass::is_on(st),
            Kind::Scene if hass::domain_of(&e) == "script" => hass::is_on(st),
            _ => None,
        }
    }

    fn colors(&self, st: Option<&Value>, a: &ActionData) -> [u8; 3] {
        let e = Self::entity(a);
        match (self.kind, self.active(st, a)) {
            (Kind::State, _) => OFF_BG,
            (Kind::Service | Kind::Scene, None) => IDLE_BG,
            (_, Some(true)) => on_color(hass::domain_of(&e)),
            _ => OFF_BG,
        }
    }

    fn status_text(&self, st: Option<&Value>, a: &ActionData) -> String {
        let e = Self::entity(a);
        let dom = hass::domain_of(&e);
        match self.kind {
            Kind::Service => if e.is_empty() { String::new() } else { a.setting_str("service") },
            Kind::Scene => if dom == "script" { tr!("Script") } else { tr!("Scene") },
            Kind::State => {
                let dec = a.settings().get("decimals").and_then(|v| v.as_i64().or_else(|| v.as_str()?.trim().parse().ok()))
                    .filter(|d| *d >= 0).map(|d| d as usize);
                hass::format_state(st, a.setting_str("attribute").trim(), dec)
            }
            Kind::Toggle => {
                let Some(stv) = st else { return String::new() };
                let attrs = stv.get("attributes").cloned().unwrap_or(Value::Null);
                let num = |k: &str| attrs.get(k).and_then(|v| v.as_f64().or_else(|| v.as_str()?.parse().ok()));
                let on = hass::is_on(st);
                let show = a.settings().get("show_value") != Some(&Value::Bool(false));
                if show && on == Some(true) {
                    match dom {
                        "light" if num("brightness").is_some() => return format!("{}%", (num("brightness").unwrap_or(0.0) * 100.0 / 255.0).round()),
                        "cover" if num("current_position").is_some() => return format!("{}%", num("current_position").unwrap_or(0.0)),
                        "fan" if num("percentage").is_some() => return format!("{}%", num("percentage").unwrap_or(0.0)),
                        _ => {}
                    }
                }
                if dom == "climate" && show {
                    if let Some(t) = attrs.get("temperature").filter(|t| !t.is_null()) {
                        return format!("{}°", t.as_str().map(str::to_string).unwrap_or_else(|| t.to_string()));
                    }
                }
                match on {
                    None => hass::format_state(st, "", None),
                    Some(on) => match dom {
                        "cover" => if on { tr!("open") } else { tr!("closed") },
                        "lock" => if on { tr!("unlocked") } else { tr!("locked") },
                        _ => if on { pgettext("state", "on") } else { pgettext("state", "off") },
                    },
                }
            }
        }
    }

    /// Vor einem Aufruf prüfen; false (mit Meldung im Protokoll), wenn es nicht geht.
    fn ready(c: &Client, a: &ActionData, need_entity: bool) -> bool {
        if !c.configured() {
            log::warn!("{}", tr!("Home Assistant is not set up yet"));
            return false;
        }
        if need_entity && Self::entity(a).is_empty() {
            log::warn!("{}", tr!("{action}: no entity chosen", action = a.name()));
            return false;
        }
        true
    }

    /// Aufruf im Arbeits-Thread; Fehler: „Home Assistant: …“ im Protokoll und Warnsymbol auf der Taste.
    fn submit(c: &Arc<Client>, env: &Env, ok: Command, job: impl FnOnce(&Client) -> Result<(), hass::HaError> + Send + 'static) {
        let me = c.clone();
        c.worker.run(&env.outbox, "Home Assistant".into(), move || match job(&me) {
            Ok(()) => Ok(ok),
            Err(e) => {
                me.refresh();
                Err(e.message)
            }
        });
    }
}

/// Einstellungswert als Zahl (auch als Text).
fn setting_i64(s: &Map<String, Value>, k: &str) -> Option<i64> {
    s.get(k).and_then(|v| v.as_i64().or_else(|| v.as_str()?.trim().parse().ok()))
}

impl Behavior for HaAction {
    fn key_down(&mut self, a: &ActionData, env: &Env) -> Command {
        let c = hass::client(env.cfg);
        let need_entity = self.kind != Kind::Service;
        if !Self::ready(&c, a, need_entity) {
            return Command::Alert;
        }
        let eid = Self::entity(a);
        let name = a.name();
        match self.kind {
            Kind::Toggle => {
                let st = c.state(&eid);
                let on = hass::is_on(st.as_ref());
                let dom = hass::domain_of(&eid);
                if let Some(on) = on {
                    if matches!(dom, "light" | "switch" | "fan" | "input_boolean" | "automation" | "siren" | "humidifier") {
                        c.set_local(&eid, Some(if on { "off" } else { "on" }), &Map::new()); // sofort anzeigen
                    }
                }
                Self::submit(&c, env, Command::Redraw, move |c| c.toggle(&eid).map(|_| ()));
                Command::Redraw
            }
            Kind::Service => {
                let s = a.settings();
                let parsed = hass::parse_service(s.get("service").and_then(Value::as_str).unwrap_or(""))
                    .and_then(|svc| hass::parse_data(s.get("data")).map(|d| (svc, d)));
                let ((dom, svc), mut data) = match parsed {
                    Ok(p) => p,
                    Err(e) => {
                        log::warn!("{name}: {e}");
                        return Command::Alert;
                    }
                };
                if !eid.is_empty() {
                    data.entry("entity_id").or_insert(Value::from(eid));
                }
                Self::submit(&c, env, Command::Ok, move |c| c.call_service(&dom, &svc, &Value::Object(data)).map(|_| ()));
                Command::None
            }
            Kind::Scene => {
                Self::submit(&c, env, Command::Ok, move |c| c.toggle(&eid).map(|_| ()));
                Command::None
            }
            Kind::State => {
                Self::submit(&c, env, Command::Redraw, move |c| c.fetch(&eid).map(|_| ()));
                Command::None
            }
        }
    }

    fn dial_turn(&mut self, a: &ActionData, env: &Env, ticks: i8) -> Command {
        if self.kind != Kind::Toggle {
            return Command::None;
        }
        let c = hass::client(env.cfg);
        if !Self::ready(&c, a, true) {
            return Command::Alert;
        }
        let eid = Self::entity(a);
        let step = setting_i64(&a.settings(), "step").filter(|s| *s >= 1).unwrap_or(5);
        let st = c.state(&eid);
        let Some((state, attrs, call)) = hass::dial_call(&eid, st.as_ref(), step, ticks as i64) else { return Command::None };
        c.set_local(&eid, state.as_deref(), &attrs);
        let queued = self.dial.lock().unwrap().replace(call).is_some();
        if !queued {
            let pending = self.dial.clone();
            Self::submit(&c, env, Command::None, move |c| {
                let Some((dom, svc, mut data)) = pending.lock().unwrap().take() else { return Ok(()) };
                data["entity_id"] = Value::from(eid);
                c.call_service(&dom, &svc, &data).map(|_| ())
            });
        }
        Command::Redraw
    }

    fn dial_up(&mut self, _a: &ActionData, _env: &Env, _held_ms: u64) -> Command {
        Command::None
    }

    fn image(&self, a: &ActionData, env: &Env, size: (u32, u32)) -> Option<RgbaImage> {
        if native::has_image(a) {
            return None;
        }
        let c = hass::client(env.cfg);
        let st = Self::st(&c, a);
        let name = if a.title().is_empty() { self.display_name(st.as_ref(), a) } else { String::new() };
        let prob = Self::problem(&c, a);
        if !prob.is_empty() {
            let g = if Self::entity(a).is_empty() { "alert" } else { self.glyph(a) };
            return Some(draw_key(size, g, ERR_BG, &name, &prob, false));
        }
        let value = self.status_text(st.as_ref(), a);
        Some(match self.kind {
            Kind::State => draw_key(size, self.glyph(a), OFF_BG, &name, if value.is_empty() { "–" } else { &value }, true),
            _ => draw_key(size, self.glyph(a), self.colors(st.as_ref(), a), &name, &value, false),
        })
    }

    fn needs_tick(&self) -> bool {
        true
    }

    fn tick(&mut self, a: &ActionData) -> bool {
        let Some(c) = hass::current() else { return false };
        let st = Self::st(&c, a); // hält die Entität in der Abfrage
        let snap = Some((c.version(), c.error_kind(), st));
        let changed = snap != self.snap;
        self.snap = snap;
        changed
    }

    fn live_state(&self, a: &ActionData) -> Option<usize> {
        let c = hass::current()?;
        let st = Self::st(&c, a);
        native::state_of(a, self.active(st.as_ref(), a))
    }
}

pub fn behavior_for(uuid: &str) -> Option<Box<dyn Behavior>> {
    kind_for(uuid).map(|k| Box::new(HaAction::new(k)) as Box<dyn Behavior>)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn kinds() {
        assert_eq!(kind_for("com.lindeck.homeassistant.toggle"), Some(Kind::Toggle));
        assert_eq!(kind_for("com.lindeck.homeassistant.service"), Some(Kind::Service));
        assert_eq!(kind_for("com.lindeck.homeassistant.scene"), Some(Kind::Scene));
        assert_eq!(kind_for("com.lindeck.homeassistant.state"), Some(Kind::State));
        assert_eq!(kind_for("com.lindeck.homeassistant.other"), None);
        assert_eq!(kind_for("com.lindeck.obs.scene"), None);
    }

    #[test]
    fn glyphs_render() {
        for g in ["bulb", "power", "fan", "cover", "thermo", "gauge", "lock", "spark", "play", "bolt", "alert", "home"] {
            let img = glyph_image(g, 40, [255, 255, 255]).unwrap_or_else(|| panic!("{g}"));
            assert!(img.pixels().any(|p| p[3] > 0), "{g}");
        }
        let key = draw_key((72, 72), "bulb", ON_BG, "Büro", "60%", false);
        assert_eq!(key.dimensions(), (72, 72));
    }
}
