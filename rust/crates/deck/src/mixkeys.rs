//! Tastenbilder für das FreeWave-Profil – im Stil des Wave-Link-Profils (wie `freestream/mixkeys.py`).
//!
//!   * Kachel: farbiges Kanal-Symbol, darunter Mix und Lautstärke („Alle 80%“)
//!   * Fader: ein senkrechter Regler über zwei Tasten (obere und untere Hälfte), rechts die Pegel-Leiste
//!   * Mix-Wahl: Symbol und Name des Mixes, der gewählte ist grün umrandet
//!   * Seiten: „Kanäle 1–7“
//!
//! Alles wird selbst gezeichnet (keine Elgato-Grafiken), wie in Python dreifach überabgetastet und dann
//! verkleinert (glatte Kanten). Fertige Bilder werden gemerkt: Seitenwechsel und Sekundentakt zeichnen
//! meist dieselben Bilder.

use std::collections::{HashMap, VecDeque};
use std::sync::{Mutex, OnceLock};

use freestream_core::{pgettext, tr};
use image::{imageops, Rgba, RgbaImage};
use serde_json::{Map, Value};

use crate::iconlib;
use crate::render::{self, Anchor, Style};

pub type Rgb = [u8; 3];

pub const BG: Rgb = [16, 16, 20];
pub const TRACK: Rgb = [52, 52, 60];
pub const GREEN: Rgb = [46, 204, 113];
pub const RED: Rgb = [229, 72, 77];
pub const YELLOW: Rgb = [245, 177, 61];
pub const GREY: Rgb = [150, 150, 160];
/// Überabtastung für glatte Kanten
const SS: u32 = 3;
/// Pegel-Leiste der Fader: Segmente über beide Tasten ((1,6 − 0,14) / 0,085 Tastenhöhen, siehe [`fader`])
pub const FADER_SEGMENTS: u8 = 17;
/// dieses Segment liegt auf der Grenze: die obere Taste zeigt 7–16, die untere 0–7
pub const FADER_SPLIT: u8 = 7;
/// Symbole mit eigener „aus“-Form (sonst roter Strich über dem Symbol)
const OFF_GLYPHS: [&str; 4] = ["mic", "headphones", "speaker", "stream"];

/// Symbol eines Mixes ohne eigenes Symbol.
pub fn mix_icon(mix: &str) -> &'static str {
    match mix {
        "monitor" => "headphones",
        "stream" => "stream",
        _ => "wave",
    }
}

/// Anzeigename der eingebauten Mixe („Alle“, „Persönlich“, „Stream“).
pub fn mix_name(mix: &str) -> String {
    match mix {
        "both" => pgettext("mix", "All"),
        "monitor" => tr!("Personal"),
        "stream" => "Stream".into(),
        m => m.into(),
    }
}

/// (Symbol, Farbe) eines Mixer-Kanals wie `theme.channel_look` – eigene Angaben gehen vor.
pub fn channel_look(cid: &str, kind: Option<&str>, icon: &str, color: &str) -> (String, String) {
    let base = match cid {
        "system" => ("system", "#64748b"),
        "music" => ("music", "#1fa463"),
        "browser" => ("globe", "#ea580c"),
        "voice" => ("chat", "#5865f2"),
        "game" => ("game", "#f59e0b"),
        "sfx" => ("spark", "#ec4899"),
        "mic" => ("mic", "#e5484d"),
        _ if kind == Some("input") => ("input", "#64748b"),
        _ => ("wave", "#64748b"),
    };
    let pick = |own: &str, def: &str| if own.is_empty() { def.to_string() } else { own.to_string() };
    (pick(icon, base.0), pick(color, base.1))
}

/// Farbe aus „#rrggbb“, sonst Schiefergrau.
pub fn rgb(color: &str) -> Rgb {
    render::parse_color(Some(color), [100, 116, 139])
}

/// Deckkraft des Tasten-Hintergrunds aus der Einstellung „Deckkraft“ (IconOpacity, 0–100 %): bei
/// FreeWave-Tasten wird nur der dunkle Hintergrund durchsichtig – Symbol, Regler und Text bleiben.
pub fn bg_alpha(state: &Map<String, Value>) -> u8 {
    let pct = match state.get("IconOpacity") {
        None | Some(Value::Null) => 100,
        Some(v) => v.as_i64().or_else(|| v.as_f64().map(|f| f as i64))
            .or_else(|| v.as_str().and_then(|s| s.trim().parse().ok())).unwrap_or(100),
    };
    (255.0 * pct.clamp(0, 100) as f64 / 100.0).round() as u8
}

/// Live-Pegel so gerundet, wie ihn diese Hälfte des Faders zeigt: Zahl der leuchtenden Segmente, und was
/// nur auf der anderen Taste zu sehen ist, zählt nicht. Gleicher Wert = gleiches Bild – das Deck sendet
/// eine Taste nur neu, wenn sich der Wert ändert.
pub fn fader_live(top: bool, live: Option<f64>) -> Option<u8> {
    let live = live?;
    let lit = (live.clamp(0.0, 1.0) * FADER_SEGMENTS as f64 + 0.5) as u8; // so viele Segmente leuchten
    Some(if top { lit.max(FADER_SPLIT) } else { lit.min(FADER_SPLIT + 1) })
}

// ------------------------------------------------------------------------------------- Bildbeschreibung
/// Alles, was ein FreeWave-Tastenbild bestimmt. Gleiche Beschreibung = gleiches Bild (Zwischenspeicher).
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum Look {
    /// Kanal-Kachel
    Tile { icon: String, color: Rgb, vol: Option<i64>, muted: bool, label: String, alpha: u8 },
    /// Hälfte eines Faders; `live` = leuchtende Segmente (None: die Leiste folgt der Lautstärke)
    Fader { top: bool, icon: String, color: Rgb, vol: Option<i64>, muted: bool, label: String, alpha: u8, live: Option<u8> },
    /// Mix-Wahl
    Mix { mix: String, selected: bool, sub: String, name: String, icon: String, alpha: u8 },
    /// „Kanäle 1–7“
    Pages { first: i64, last: i64, alpha: u8 },
}

/// Fertige Bilder merken (wie `_cached` in Python): neu zeichnen kostet je Taste einige Millisekunden.
pub fn draw(look: &Look, size: (u32, u32)) -> RgbaImage {
    type Key = (Look, (u32, u32), String);
    struct Cache {
        map: HashMap<Key, RgbaImage>,
        order: VecDeque<Key>,
    }
    const MAX: usize = 512;
    static CACHE: OnceLock<Mutex<Cache>> = OnceLock::new();
    let cache = CACHE.get_or_init(|| Mutex::new(Cache { map: HashMap::new(), order: VecDeque::new() }));
    let key = (look.clone(), size, freestream_core::i18n::current());
    if let Some(img) = cache.lock().unwrap().map.get(&key) {
        return img.clone();
    }
    let img = match look {
        Look::Tile { icon, color, vol, muted, label, alpha } => tile(size, icon, *color, *vol, *muted, label, *alpha),
        Look::Fader { top, icon, color, vol, muted, label, alpha, live } =>
            fader(size, *top, icon, *color, *vol, *muted, label, *alpha, *live),
        Look::Mix { mix, selected, sub, name, icon, alpha } => mix_key(size, mix, *selected, sub, name, icon, *alpha),
        Look::Pages { first, last, alpha } => pages_key(size, *first, *last, *alpha),
    };
    let mut c = cache.lock().unwrap();
    if !c.map.contains_key(&key) {
        c.order.push_back(key.clone());
    }
    c.map.insert(key, img.clone());
    while c.order.len() > MAX {
        if let Some(old) = c.order.pop_front() {
            c.map.remove(&old);
        }
    }
    img
}

// ------------------------------------------------------------------------------------- Zeichenhelfer
fn canvas(w: u32, h: u32, alpha: u8) -> RgbaImage {
    RgbaImage::from_pixel(w, h, Rgba([BG[0], BG[1], BG[2], alpha]))
}

/// Überabgetastetes Bild auf Tastengröße bringen.
fn done(im: &RgbaImage, w: u32, h: u32) -> RgbaImage {
    imageops::resize(im, w, h, imageops::FilterType::Lanczos3)
}

fn rrect(im: &mut RgbaImage, b: (f32, f32, f32, f32), r: f32, c: Rgb) {
    render::fill_round_rect(im, b, r, [c[0], c[1], c[2], 255]);
}

/// Rand eines abgerundeten Rechtecks, `width` breit nach innen (wie Pillows `outline`).
fn rrect_outline(im: &mut RgbaImage, (x0, y0, x1, y1): (f32, f32, f32, f32), r: f32, width: f32, c: Rgb) {
    let inside = |x: f32, y: f32, (a0, b0, a1, b1): (f32, f32, f32, f32), r: f32| {
        if x < a0 || x > a1 || y < b0 || y > b1 {
            return false;
        }
        let r = r.min((a1 - a0) / 2.0).min((b1 - b0) / 2.0).max(0.0);
        let dx = (a0 + r - x).max(x - (a1 - r)).max(0.0);
        let dy = (b0 + r - y).max(y - (b1 - r)).max(0.0);
        dx * dx + dy * dy <= r * r
    };
    let inner = (x0 + width, y0 + width, x1 - width, y1 - width);
    let (w, h) = im.dimensions();
    for py in y0.floor().max(0.0) as u32..(y1.ceil().max(0.0) as u32).min(h) {
        for px in x0.floor().max(0.0) as u32..(x1.ceil().max(0.0) as u32).min(w) {
            let (cx, cy) = (px as f32 + 0.5, py as f32 + 0.5);
            if inside(cx, cy, (x0, y0, x1, y1), r) && !inside(cx, cy, inner, (r - width).max(0.0)) {
                im.put_pixel(px, py, Rgba([c[0], c[1], c[2], 255]));
            }
        }
    }
}

/// Gerade Linie mit `width` Pixeln Breite (flache Enden wie Pillows `line`).
fn line(im: &mut RgbaImage, (ax, ay): (f32, f32), (bx, by): (f32, f32), width: f32, c: Rgb) {
    let (dx, dy) = (bx - ax, by - ay);
    let len2 = (dx * dx + dy * dy).max(1e-6);
    let half = width / 2.0;
    let (w, h) = im.dimensions();
    let (minx, maxx) = (ax.min(bx) - half, ax.max(bx) + half);
    let (miny, maxy) = (ay.min(by) - half, ay.max(by) + half);
    for py in miny.floor().max(0.0) as u32..(maxy.ceil().max(0.0) as u32).min(h) {
        for px in minx.floor().max(0.0) as u32..(maxx.ceil().max(0.0) as u32).min(w) {
            let (cx, cy) = (px as f32 + 0.5, py as f32 + 0.5);
            let t = ((cx - ax) * dx + (cy - ay) * dy) / len2;
            if !(0.0..=1.0).contains(&t) {
                continue;
            }
            let (qx, qy) = (ax + t * dx, ay + t * dy);
            if (cx - qx).powi(2) + (cy - qy).powi(2) <= half * half {
                im.put_pixel(px, py, Rgba([c[0], c[1], c[2], 255]));
            }
        }
    }
}

fn paste_glyph(im: &mut RgbaImage, name: &str, size: f32, color: Rgb, (cx, cy): (f32, f32)) {
    let g = iconlib::glyph(name, size as u32, [color[0], color[1], color[2], 255]);
    let x = (cx - g.width() as f32 / 2.0) as i64;
    let y = (cy - g.height() as f32 / 2.0) as i64;
    imageops::overlay(im, &g, x, y);
}

/// Farbe c, nur zu Anteil t über dem Hintergrund (unbeleuchtete Segmente).
fn dim(c: Rgb) -> Rgb {
    [0, 1, 2].map(|i| (BG[i] as f32 + (c[i] as f32 - BG[i] as f32) * 0.3).round() as u8)
}

/// Schriftgröße für `text`, verkleinert, bis er in `width` passt (lange Mix-Namen).
fn fitting(text: &str, width: f32, size: f32) -> f32 {
    let mut s = size;
    let mut f = s.floor();
    while render::text_length(Style::Bold, f, text) > width && s > size * 0.55 {
        s *= 0.9;
        f = s.floor();
    }
    f
}

fn text(im: &mut RgbaImage, style: Style, size: f32, at: (f32, f32), anchor: Anchor, t: &str, color: Rgb) {
    render::draw_text_at(im, style, size, at, anchor, t, color, 0);
}

// ------------------------------------------------------------------------------------- Tastenbilder
/// Kanal-Kachel: abgerundetes Quadrat mit Symbol, darunter „Alle 80%“ bzw. „stumm“.
pub fn tile((w, h): (u32, u32), icon: &str, color: Rgb, vol: Option<i64>, muted: bool, label: &str, alpha: u8) -> RgbaImage {
    let (bw, bh) = (w * SS, h * SS);
    let mut im = canvas(bw, bh, alpha);
    let (fw, fh) = (bw as f32, bh as f32);
    let side = fh * 0.5;
    let (x0, y0) = ((fw - side) / 2.0, fh * 0.1);
    rrect(&mut im, (x0, y0, x0 + side, y0 + side), side * 0.24, if muted { [70, 70, 78] } else { color });
    let has_off = OFF_GLYPHS.contains(&icon);
    let glyph = if muted && has_off { format!("{icon}_off") } else { icon.to_string() };
    paste_glyph(&mut im, &glyph, side * 0.56, if muted { [200, 200, 205] } else { [255, 255, 255] },
                (fw / 2.0, y0 + side / 2.0));
    if muted && !has_off {
        line(&mut im, (x0 + side * 0.12, y0 + side * 0.88), (x0 + side * 0.88, y0 + side * 0.12), (SS * 3) as f32, RED);
    }
    let fs = (fh * 0.15).floor();
    match vol {
        None => text(&mut im, Style::Bold, fs, (fw / 2.0, fh * 0.8), Anchor::Middle, &tr!("Mixer off"), GREY),
        Some(_) if muted => text(&mut im, Style::Bold, fs, (fw / 2.0, fh * 0.8), Anchor::Middle,
                                 &pgettext("state", "muted"), RED),
        Some(v) => {
            let name = format!("{label} ");
            let value = format!("{v}%");
            let fs = fitting(&format!("{name}{value}"), fw * 0.94, fh * 0.15);
            let lw = render::text_length(Style::Bold, fs, &name);
            let vw = render::text_length(Style::Bold, fs, &value);
            let x = (fw - lw - vw) / 2.0;
            text(&mut im, Style::Bold, fs, (x, fh * 0.8), Anchor::LeftMiddle, &name, GREY);
            text(&mut im, Style::Bold, fs, (x + lw, fh * 0.8), Anchor::LeftMiddle, &value, [255, 255, 255]);
        }
    }
    done(&im, w, h)
}

/// Hälfte (`top` oder untere) eines Faders, der über zwei übereinanderliegende Tasten geht.
///
/// `live`: leuchtende Segmente des Pegels nach dem Regler (wie im FreeWave-Fenster) – dann zeigt die
/// Leiste rechts das Signal, oben gelb/rot bei lauten Stellen; None = die Leiste folgt der Lautstärke.
#[allow(clippy::too_many_arguments)]
pub fn fader((w, h): (u32, u32), top: bool, icon: &str, color: Rgb, vol: Option<i64>, muted: bool, label: &str,
             alpha: u8, live: Option<u8>) -> RgbaImage {
    let (bw, bh) = (w * SS, h * SS);
    let mut im = canvas(bw, 2 * bh, alpha);
    let (fw, fh) = (bw as f32, bh as f32);
    let (y_top, bottom) = (fh * 0.14, 2.0 * fh - fh * 0.4);
    let (tx, tw) = (fw * 0.38, fw * 0.13);
    let level = vol.map(|v| v.clamp(0, 100)).unwrap_or(0);
    let y = bottom - (bottom - y_top) * level as f32 / 100.0;
    rrect(&mut im, (tx - tw / 2.0, y_top, tx + tw / 2.0, bottom), tw / 2.0, TRACK);
    if level > 0 {
        rrect(&mut im, (tx - tw / 2.0, y, tx + tw / 2.0, bottom), tw / 2.0, if muted { [95, 95, 104] } else { color });
    }
    let (kw, kh) = (fw * 0.4, fh * 0.13);
    rrect(&mut im, (tx - kw / 2.0, y - kh / 2.0, tx + kw / 2.0, y + kh / 2.0), kh / 2.0,
          if muted { RED } else { [245, 245, 248] });
    // Pegel-Leiste rechts: Live-Pegel (oder ohne Messung: Segmente bis zur Lautstärke)
    let (mx, mw, step) = (fw * 0.66, fw * 0.1, fh * 0.085);
    let n = FADER_SEGMENTS;
    for i in 0..n {
        let sy = bottom - (i as f32 + 1.0) * step;
        let pos = (i as f32 + 0.5) / n as f32;
        let (lit, seg) = match live {
            Some(on) => (!muted && i < on, if pos > 0.9 { RED } else if pos > 0.7 { YELLOW } else { color }),
            None => (!muted && vol.is_some() && pos * 100.0 <= level as f32, color),
        };
        let fill = if lit { seg } else if !muted { dim(seg) } else { TRACK };
        rrect(&mut im, (mx, sy + step * 0.22, mx + mw, sy + step), step * 0.2, fill);
    }
    // unten: kleines Kanal-Symbol und Mix
    let ly = 2.0 * fh - fh * 0.18;
    let t = if muted { pgettext("state", "muted") } else { label.to_string() };
    let fs = fitting(&t, fw * 0.7, fh * 0.15);
    let tw_ = render::text_length(Style::Bold, fs, &t);
    let gs = fh * 0.17;
    let x = (fw - gs - fw * 0.05 - tw_) / 2.0;
    paste_glyph(&mut im, icon, gs, if muted { RED } else { color }, (x + gs / 2.0, ly));
    text(&mut im, Style::Bold, fs, (x + gs + fw * 0.05, ly), Anchor::LeftMiddle, &t, if muted { RED } else { GREY });
    let part = imageops::crop_imm(&im, 0, if top { 0 } else { bh }, bw, bh).to_image();
    done(&part, w, h)
}

/// Mix-Wahl: Symbol und Name; der gewählte Mix ist grün umrandet.
pub fn mix_key((w, h): (u32, u32), mix: &str, selected: bool, sub: &str, name: &str, icon: &str, alpha: u8) -> RgbaImage {
    let (bw, bh) = (w * SS, h * SS);
    let mut im = canvas(bw, bh, alpha);
    let (fw, fh) = (bw as f32, bh as f32);
    if selected {
        let b = fw * 0.05;
        rrect_outline(&mut im, (b / 2.0, b / 2.0, fw - b / 2.0, fh - b / 2.0), fw * 0.16, b.floor(), GREEN);
    }
    let icon = if icon.is_empty() { mix_icon(mix) } else { icon };
    paste_glyph(&mut im, icon, fh * 0.34, if selected { [255, 255, 255] } else { GREY }, (fw / 2.0, fh * 0.38));
    let name = if name.is_empty() { mix_name(mix) } else { name.to_string() };
    let fs = fitting(&name, fw * 0.88, fh * 0.16);
    text(&mut im, Style::Bold, fs, (fw / 2.0, fh * if sub.is_empty() { 0.74 } else { 0.7 }), Anchor::Middle, &name,
         if selected { GREEN } else { [220, 220, 226] });
    if !sub.is_empty() {
        text(&mut im, Style::Regular, (fh * 0.11).floor(), (fw / 2.0, fh * 0.85), Anchor::Middle, sub, GREY);
    }
    done(&im, w, h)
}

/// „Kanäle 1–7“ – zeigt, welche Kanäle diese Seite steuert; Drücken blättert weiter.
pub fn pages_key((w, h): (u32, u32), first: i64, last: i64, alpha: u8) -> RgbaImage {
    let (bw, bh) = (w * SS, h * SS);
    let mut im = canvas(bw, bh, alpha);
    let (fw, fh) = (bw as f32, bh as f32);
    text(&mut im, Style::Bold, (fh * 0.17).floor(), (fw / 2.0, fh * 0.4), Anchor::Middle, &tr!("Channels"), GREEN);
    let span = if last != first { format!("{first}–{last}") } else { first.to_string() };
    text(&mut im, Style::Bold, (fh * 0.2).floor(), (fw / 2.0, fh * 0.64), Anchor::Middle, &span, GREEN);
    done(&im, w, h)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn fader_live_rounds_per_half() {
        assert_eq!(fader_live(true, None), None);
        assert_eq!(fader_live(true, Some(0.0)), Some(FADER_SPLIT));
        assert_eq!(fader_live(false, Some(0.0)), Some(0));
        assert_eq!(fader_live(false, Some(1.0)), Some(FADER_SPLIT + 1));
        assert_eq!(fader_live(true, Some(1.0)), Some(FADER_SEGMENTS));
        // gleiche Segmentzahl = gleicher Wert (kein neues Bild)
        assert_eq!(fader_live(true, Some(0.70)), fader_live(true, Some(0.72)));
    }

    #[test]
    fn opacity_and_looks() {
        assert_eq!(bg_alpha(json!({}).as_object().unwrap()), 255);
        assert_eq!(bg_alpha(json!({"IconOpacity": 50}).as_object().unwrap()), 128);
        assert_eq!(bg_alpha(json!({"IconOpacity": "0"}).as_object().unwrap()), 0);
        assert_eq!(channel_look("music", None, "", ""), ("music".into(), "#1fa463".into()));
        assert_eq!(channel_look("cam", Some("input"), "", "#ff0000"), ("input".into(), "#ff0000".into()));
        assert_eq!(rgb("nonsense"), [100, 116, 139]);
    }
}
