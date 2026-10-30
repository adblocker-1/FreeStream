//! Icon-Bibliothek für FreeDeck (wie `freestream/iconlib.py`): vordefinierte Tastenbilder, alle selbst
//! gezeichnet (keine Elgato-Grafiken).
//!
//! Jedes Symbol gibt es in drei Paketen:
//! * Farbe   – weißes Symbol auf farbiger Fläche (Farbe je Thema)
//! * Weiß    – weißes Symbol auf Schwarz
//! * Schwarz – schwarzes Symbol auf Weiß
//!
//! [`render`] liefert das fertige Tastenbild (144 × 144), [`glyph`] nur das Symbol.
//!
//! Gezeichnet wird wie in Python 4-fach überabgetastet (hier mit tiny-skia, kantengeglättet) und danach
//! mit Lanczos verkleinert. Die Geometrie der Symbole ist dieselbe wie in `iconlib.py`, `theme.py`
//! (`_draw_glyph`) und `livekeys.py` (`icon`); Kästen werden wie in Pillow behandelt (ganzzahlig,
//! rechter/unterer Rand eingeschlossen, Umrisse nach innen).

use crate::render;
use ab_glyph::{Font, PxScale, ScaleFont};
use freestream_core::{pgettext, tr};
use image::{imageops, ImageBuffer, Rgba, Rgba32FImage, RgbaImage};
use std::f32::consts::PI;
use tiny_skia::{BlendMode, FillRule, LineCap, LineJoin, Paint, Path, PathBuilder, Pixmap, Stroke, Transform};

/// Kantenlänge eines Tastenbilds.
pub const KEY_SIZE: u32 = 144;
/// Pakete: Farbe, Weiß, Schwarz.
pub const STYLES: [&str; 3] = ["color", "white", "black"];

/// Thema -> (Schlüssel, Farbe im Paket „Farbe“, Symbole).
pub const TOPICS: &[(&str, &str, &[&str])] = &[
    ("audio", "#3b82f6", &["mic", "mic_off", "headphones", "headphones_off", "speaker", "speaker_off", "volume_up",
                           "volume_down", "music", "wave", "sfx", "fx"]),
    ("media", "#ef4444", &["play", "pause", "stop", "prev", "next", "shuffle", "repeat", "record", "video",
                           "camera", "image", "film"]),
    ("stream", "#a855f7", &["stream", "stream_off", "live", "scene", "screen", "eye", "eye_off", "chat", "users",
                            "heart", "star", "bell"]),
    ("system", "#64748b", &["system", "gear", "power", "lock", "unlock", "refresh", "folder", "search", "keyboard",
                            "terminal", "download", "upload", "link", "copy", "save", "trash", "edit", "wifi",
                            "bluetooth", "cpu", "memory", "disk", "network", "battery"]),
    ("time", "#f59e0b", &["clock", "hourglass", "stopwatch", "calendar", "sun", "moon"]),
    ("home", "#10b981", &["home", "lightbulb", "plug", "thermometer", "fan", "door"]),
    ("nav", "#0ea5e9", &["up", "down", "left", "right", "layers", "deck", "plus", "minus",
                         "close", "check", "more", "undo", "redo", "import", "export"]),
    ("misc", "#ec4899", &["game", "globe", "mail", "phone", "coffee", "rocket", "trophy", "gift", "flag",
                          "pin", "smile", "thumbs_up", "spark", "puzzle", "text"]),
];

/// Titel eines Themas.
pub fn topic_title(key: &str) -> String {
    match key {
        "audio" => tr!("Audio"),
        "media" => tr!("Media"),
        "stream" => tr!("Streaming"),
        "system" => tr!("System"),
        "time" => tr!("Time"),
        "home" => tr!("Smart home"),
        "nav" => tr!("Navigation"),
        "misc" => tr!("Miscellaneous"),
        _ => key.to_string(),
    }
}

/// Titel eines Pakets.
pub fn style_title(style: &str) -> String {
    match style {
        "color" => tr!("FreeDeck Icons (Color)"),
        "white" => tr!("FreeDeck Icons (White)"),
        "black" => tr!("FreeDeck Icons (Black)"),
        _ => style.to_string(),
    }
}

/// Anzeigename (auch für die Suche).
pub fn label(name: &str) -> String {
    let msg = match name {
        "mic" => "Microphone",
        "mic_off" => "Microphone off",
        "headphones" => "Headphones",
        "headphones_off" => "Headphones off",
        "speaker" => "Speaker",
        "speaker_off" => "Speaker off",
        "volume_up" => "Volume up",
        "volume_down" => "Volume down",
        "music" => "Music",
        "wave" => "Wave",
        "sfx" => "Sound effects",
        "fx" => "Effects",
        "play" => "Play",
        "pause" => "Pause",
        "stop" => "Stop",
        "prev" => "Previous",
        "next" => "Next",
        "shuffle" => "Shuffle",
        "repeat" => "Repeat",
        "record" => "Record",
        "video" => "Video",
        "camera" => "Camera",
        "image" => "Image",
        "film" => "Film",
        "stream" => "Stream",
        "stream_off" => "Stream off",
        "live" => "Live",
        "scene" => "Scene",
        "screen" => "Screen",
        "eye" => "Visible",
        "eye_off" => "Hidden",
        "chat" => "Chat",
        "users" => "People",
        "heart" => "Heart",
        "star" => "Star",
        "bell" => "Notification",
        "system" => "Computer",
        "gear" => "Settings",
        "power" => "Power",
        "lock" => "Lock",
        "unlock" => "Unlock",
        "refresh" => "Refresh",
        "folder" => "Folder",
        "search" => "Search",
        "keyboard" => "Keyboard",
        "terminal" => "Terminal",
        "download" => "Download",
        "upload" => "Upload",
        "link" => "Link",
        "copy" => "Copy",
        "save" => "Save",
        "trash" => "Delete",
        "edit" => "Edit",
        "wifi" => "Wi-Fi",
        "bluetooth" => "Bluetooth",
        "cpu" => "Processor",
        "memory" => "Memory",
        "disk" => "Disk",
        "network" => "Network",
        "battery" => "Battery",
        "clock" => "Clock",
        "hourglass" => "Hourglass",
        "stopwatch" => "Stopwatch",
        "calendar" => "Calendar",
        "sun" => "Day",
        "moon" => "Night",
        "home" => "Home",
        "lightbulb" => return pgettext("icon", "Light"),
        "plug" => "Socket",
        "thermometer" => "Temperature",
        "fan" => "Fan",
        "door" => "Door",
        "up" => "Up",
        "down" => "Down",
        "left" => "Left",
        "right" => "Right",
        "navigate" => "Open",
        "grid" => "Grid",
        "layers" => "Layers",
        "deck" => "Deck",
        "plus" => "Plus",
        "minus" => "Minus",
        "close" => "Close",
        "check" => "Done",
        "more" => "More",
        "undo" => "Undo",
        "redo" => "Redo",
        "import" => "Import",
        "export" => "Export",
        "game" => "Game",
        "browser" => "Browser",
        "globe" => "Web",
        "mail" => "Mail",
        "phone" => "Phone",
        "coffee" => "Coffee",
        "rocket" => "Rocket",
        "trophy" => "Trophy",
        "gift" => "Gift",
        "flag" => "Flag",
        "pin" => "Location",
        "smile" => "Smiley",
        "thumbs_up" => "Thumbs up",
        "spark" => "Magic",
        "puzzle" => "Plugin",
        "text" => "Text",
        _ => return name.to_string(),
    };
    tr!(msg)
}

/// Alle Symbole in der Reihenfolge der Themen.
pub fn all_names() -> Vec<&'static str> {
    TOPICS.iter().flat_map(|(_, _, lst)| lst.iter().copied()).collect()
}

/// Thema eines Symbols („misc“, wenn unbekannt).
pub fn topic_of(name: &str) -> &'static str {
    TOPICS.iter().find(|(_, _, lst)| lst.contains(&name)).map(|(k, _, _)| *k).unwrap_or("misc")
}

/// Farbe des Themas eines Symbols (Paket „Farbe“).
pub fn topic_color(name: &str) -> &'static str {
    TOPICS.iter().find(|(_, _, lst)| lst.contains(&name)).map(|(_, c, _)| *c).unwrap_or("#64748b")
}

/// Symbole, deren Name, Anzeigename oder Thema den Suchtext enthält (leer: alle).
pub fn search(query: &str) -> Vec<&'static str> {
    search_in(query, &all_names())
}

/// Wie [`search`], aber nur unter `items`.
pub fn search_in(query: &str, items: &[&'static str]) -> Vec<&'static str> {
    let q = query.trim().to_lowercase();
    if q.is_empty() {
        return items.to_vec();
    }
    items
        .iter()
        .copied()
        .filter(|n| {
            n.to_lowercase().contains(&q)
                || label(n).to_lowercase().contains(&q)
                || topic_title(topic_of(n)).to_lowercase().contains(&q)
        })
        .collect()
}

// ============================================================ Zeichnen
/// Symbole aus `livekeys.icon` (ohne Rand gezeichnet).
const LIVE: &[&str] = &["clock", "hourglass", "stopwatch", "cpu", "memory", "thermometer", "network", "battery", "disk"];

/// Symbole aus `iconlib._draw_new`; alle übrigen kommen aus `theme._draw_glyph`.
const NEW: &[&str] = &["volume_up", "volume_down", "pause", "stop", "prev", "next", "shuffle", "repeat", "video",
    "film", "live", "scene", "screen", "eye", "eye_off", "users", "heart", "star", "bell", "power", "lock", "unlock",
    "refresh", "terminal", "download", "upload", "link", "copy", "save", "wifi", "bluetooth", "calendar",
    "lightbulb", "plug", "fan", "door", "left", "right", "minus", "mail", "phone", "coffee", "rocket", "trophy",
    "gift", "flag", "pin", "smile", "thumbs_up"];

/// Rot des Schrägstrichs bei „aus“-Symbolen (`COL["danger"]` des dunklen Modus).
const DANGER: [u8; 4] = [0xf0, 0x48, 0x4f, 0xff];

/// Nur das Symbol (RGBA, transparent), `size` × `size`.
pub fn glyph(name: &str, size: u32, color: [u8; 4]) -> RgbaImage {
    let size = size.max(1);
    if LIVE.contains(&name) {
        let mut pen = Pen::new(size * 4, size * 4, color, 0.0);
        draw_live(name, &mut pen);
        return downsample(&pen.finish(), size, size);
    }
    let mut pen = Pen::new(size * 4, size * 4, color, 0.06);
    if NEW.contains(&name) {
        draw_new(name, &mut pen);
        let mut big = pen.finish();
        if name == "live" {
            live_text(&mut big, &pen);
        }
        return downsample(&big, size, size);
    }
    draw_theme(name, &mut pen);
    downsample(&pen.finish(), size, size)
}

/// Fertiges Tastenbild (144 × 144): Fläche + Symbol in der Mitte.
pub fn render(name: &str, style: &str) -> RgbaImage {
    render_sized(name, style, KEY_SIZE)
}

/// Wie [`render`] in beliebiger Größe (z. B. Kacheln der Bibliothek).
pub fn render_sized(name: &str, style: &str, size: u32) -> RgbaImage {
    let size = size.max(1);
    let (bg, fg) = match style {
        "color" => (render::parse_color(Some(topic_color(name)), [100, 116, 139]), [255, 255, 255]),
        "black" => ([244, 244, 246], [17, 17, 20]),
        _ => ([0, 0, 0], [255, 255, 255]),
    };
    let mut im = RgbaImage::from_pixel(size, size, Rgba([bg[0], bg[1], bg[2], 255]));
    if style == "color" {
        // leichter Verlauf nach unten
        for y in 0..size {
            let a = (46 * y / size) as u8;
            for x in 0..size {
                over(im.get_pixel_mut(x, y), Rgba([0, 0, 0, a]));
            }
        }
    }
    let g = glyph(name, size / 2, [fg[0], fg[1], fg[2], 255]);
    let (ox, oy) = ((size - g.width()) / 2, (size - g.height()) / 2);
    for (x, y, p) in g.enumerate_pixels() {
        over(im.get_pixel_mut(ox + x, oy + y), *p);
    }
    im
}

/// Ecken einer Kachel abrunden (Radius `r` in Pixeln); `outline`: feiner Rand in dieser Farbe (helle
/// Kacheln auf hellem Hintergrund). Wie `_rounded` in `iconlibui.py`.
pub fn rounded_tile(im: &RgbaImage, r: f32, outline: Option<[u8; 3]>) -> RgbaImage {
    const K: u32 = 4; // Maske überabgetastet: glatte Ecken
    let (w, h) = im.dimensions();
    let (sw, sh) = (w * K, h * K);
    let mut mask = Pen::new(sw, sh, [255, 255, 255, 255], 0.0);
    mask.rrect_px((0.0, 0.0, (sw - 1) as f32, (sh - 1) as f32), r * K as f32, mask.c, None);
    let mask = downsample(&mask.finish(), w, h);
    let mut out = im.clone();
    for (p, m) in out.pixels_mut().zip(mask.pixels()) {
        p[3] = ((p[3] as u32 * m[3] as u32 + 127) / 255) as u8;
    }
    if let Some(c) = outline {
        let mut ring = Pen::new(sw, sh, [c[0], c[1], c[2], 255], 0.0);
        ring.rrect_px((0.0, 0.0, (sw - 1) as f32, (sh - 1) as f32), r * K as f32, ring.c, Some(K as f32));
        let ring = downsample(&ring.finish(), w, h);
        for (p, q) in out.pixels_mut().zip(ring.pixels()) {
            over(p, *q);
        }
    }
    out
}

// ------------------------------------------------------------ Hilfen
/// `src` über `dst` legen (gerade Alphawerte, wie `Image.alpha_composite`).
fn over(dst: &mut Rgba<u8>, src: Rgba<u8>) {
    let sa = src[3] as f32 / 255.0;
    if sa <= 0.0 {
        return;
    }
    let da = dst[3] as f32 / 255.0;
    let oa = sa + da * (1.0 - sa);
    for i in 0..3 {
        let v = (src[i] as f32 * sa + dst[i] as f32 * da * (1.0 - sa)) / oa;
        dst[i] = v.round().clamp(0.0, 255.0) as u8;
    }
    dst[3] = (oa * 255.0).round().clamp(0.0, 255.0) as u8;
}

/// Verkleinern mit Lanczos auf vormultiplizierten Werten (wie Pillow bei RGBA).
fn downsample(big: &RgbaImage, w: u32, h: u32) -> RgbaImage {
    let pre: Rgba32FImage = ImageBuffer::from_fn(big.width(), big.height(), |x, y| {
        let p = big.get_pixel(x, y);
        // f32-Bilder rechnen in 0 … 1
        let a = p[3] as f32 / 255.0;
        Rgba([p[0] as f32 / 255.0 * a, p[1] as f32 / 255.0 * a, p[2] as f32 / 255.0 * a, a])
    });
    let small = imageops::resize(&pre, w, h, imageops::FilterType::Lanczos3);
    ImageBuffer::from_fn(w, h, |x, y| {
        let p = small.get_pixel(x, y);
        let a = p[3].clamp(0.0, 1.0);
        if a * 255.0 < 0.5 {
            return Rgba([0, 0, 0, 0]);
        }
        let ch = |v: f32| (v / a * 255.0).round().clamp(0.0, 255.0) as u8;
        Rgba([ch(p[0]), ch(p[1]), ch(p[2]), (a * 255.0).round() as u8])
    })
}

/// Runden wie Python (`round`: bei .5 zur geraden Zahl).
fn py_round(v: f32) -> f32 {
    let r = v.round();
    if (v - v.trunc()).abs() == 0.5 && r % 2.0 != 0.0 {
        r - v.signum()
    } else {
        r
    }
}

/// Punkte einer Ellipse im Kasten (cx, cy, a, b) zwischen den Winkeln `s` und `e` (Grad, im
/// Uhrzeigersinn ab 3 Uhr; Winkel geometrisch vom Mittelpunkt aus wie in Pillow).
fn ellipse_pts(cx: f32, cy: f32, a: f32, b: f32, s: f32, e: f32) -> Vec<(f32, f32)> {
    let n = ((e - s).abs().ceil() as usize).max(2);
    (0..=n)
        .map(|i| {
            let t = (s + (e - s) * i as f32 / n as f32).to_radians();
            let (c, si) = (t.cos(), t.sin());
            let d = ((b * c).powi(2) + (a * si).powi(2)).sqrt();
            let r = if d > 0.0 { a * b / d } else { 0.0 };
            (cx + r * c, cy + r * si)
        })
        .collect()
}

/// Winkel wie Pillow: Ende hinter den Anfang, höchstens ein voller Kreis.
fn norm_angles(s: f32, mut e: f32) -> (f32, f32) {
    while e < s {
        e += 360.0;
    }
    (s, e.min(s + 360.0))
}

fn path_of(pts: &[(f32, f32)], close: bool) -> Option<Path> {
    let mut pb = PathBuilder::new();
    let (first, rest) = pts.split_first()?;
    pb.move_to(first.0, first.1);
    for p in rest {
        pb.line_to(p.0, p.1);
    }
    if close {
        pb.close();
    }
    pb.finish()
}

fn push_poly(pb: &mut PathBuilder, pts: &[(f32, f32)]) {
    if let Some((first, rest)) = pts.split_first() {
        pb.move_to(first.0, first.1);
        for p in rest {
            pb.line_to(p.0, p.1);
        }
        pb.close();
    }
}

/// Kasten wie in Pillow: ganzzahlig, rechter/unterer Rand eingeschlossen.
fn pil_box((x0, y0, x1, y1): (f32, f32, f32, f32)) -> (f32, f32, f32, f32) {
    (x0.trunc(), y0.trunc(), x1.trunc() + 1.0, y1.trunc() + 1.0)
}

/// Zeichenfläche (überabgetastet) mit dem Koordinatensystem der Python-Helfer.
struct Pen {
    pm: Pixmap,
    /// Ursprung und Kantenlänge des Symbolfelds in Pixeln
    x0: f32,
    y0: f32,
    w: f32,
    /// Strichstärke
    lw: f32,
    c: [u8; 4],
}

const CLEAR: [u8; 4] = [0, 0, 0, 0];

impl Pen {
    fn new(sw: u32, sh: u32, c: [u8; 4], pad: f32) -> Self {
        let s = sw as f32;
        let pad = s * pad;
        Pen {
            pm: Pixmap::new(sw, sh).expect("Bildgröße"),
            x0: pad,
            y0: pad,
            w: s - 2.0 * pad,
            lw: py_round(s * 0.075).max(2.0),
            c,
        }
    }

    /// Fertiges Bild (gerade Alphawerte) in voller Auflösung.
    fn finish(&self) -> RgbaImage {
        let (w, h) = (self.pm.width(), self.pm.height());
        let mut out = RgbaImage::new(w, h);
        for (p, q) in out.pixels_mut().zip(self.pm.pixels()) {
            let c = q.demultiply();
            *p = Rgba([c.red(), c.green(), c.blue(), c.alpha()]);
        }
        out
    }

    fn p(&self, (x, y): (f32, f32)) -> (f32, f32) {
        (self.x0 + x * self.w, self.y0 + y * self.w)
    }

    fn bx(&self, a: f32, b: f32, c: f32, d: f32) -> (f32, f32, f32, f32) {
        let (p0, p1) = (self.p((a, b)), self.p((c, d)));
        (p0.0, p0.1, p1.0, p1.1)
    }

    fn paint(color: [u8; 4]) -> Paint<'static> {
        let mut paint = Paint { anti_alias: true, blend_mode: BlendMode::Source, ..Default::default() };
        paint.set_color_rgba8(color[0], color[1], color[2], color[3]);
        paint
    }

    fn fill(&mut self, path: Option<Path>, color: [u8; 4], rule: FillRule) {
        if let Some(path) = path {
            self.pm.fill_path(&path, &Self::paint(color), rule, Transform::identity(), None);
        }
    }

    // ---------------------------------------------------- Pixelkoordinaten (wie ImageDraw)
    /// Fläche eines Vielecks (Punkte sind Pixelmitten).
    fn poly_px(&mut self, pts: &[(f32, f32)], color: [u8; 4]) {
        let pts: Vec<_> = pts.iter().map(|&(x, y)| (x + 0.5, y + 0.5)).collect();
        self.fill(path_of(&pts, true), color, FillRule::Winding);
    }

    /// Linienzug; `round`: runde Enden und Ecken (Helfer `line`), sonst stumpfe Enden.
    fn stroke_px(&mut self, pts: &[(f32, f32)], width: f32, color: [u8; 4], caps: bool, joints: bool) {
        let pts: Vec<_> = pts.iter().map(|&(x, y)| (x + 0.5, y + 0.5)).collect();
        let Some(path) = path_of(&pts, false) else { return };
        let stroke = Stroke {
            width,
            line_cap: if caps { LineCap::Round } else { LineCap::Butt },
            line_join: if joints { LineJoin::Round } else { LineJoin::Miter },
            ..Default::default()
        };
        self.pm.stroke_path(&path, &Self::paint(color), &stroke, Transform::identity(), None);
    }

    fn rect_px(&mut self, bx: (f32, f32, f32, f32), color: [u8; 4]) {
        let (x0, y0, x1, y1) = pil_box(bx);
        self.fill(path_of(&[(x0, y0), (x1, y0), (x1, y1), (x0, y1)], true), color, FillRule::Winding);
    }

    /// Ellipsenbogen von `s` bis `e`; `width`: Ring nach innen, sonst Tortenstück (`pie`) bzw. Sehne.
    fn sector_px(&mut self, bx: (f32, f32, f32, f32), s: f32, e: f32, width: Option<f32>, pie: bool, color: [u8; 4]) {
        let (x0, y0, x1, y1) = pil_box(bx);
        let (cx, cy, a, b) = ((x0 + x1) / 2.0, (y0 + y1) / 2.0, (x1 - x0) / 2.0, (y1 - y0) / 2.0);
        let (s, e) = norm_angles(s, e);
        let full = e - s >= 360.0;
        let outer = ellipse_pts(cx, cy, a, b, s, e);
        let mut pb = PathBuilder::new();
        match width {
            Some(wd) if wd < a.min(b) => {
                let inner = ellipse_pts(cx, cy, a - wd, b - wd, s, e);
                if full {
                    push_poly(&mut pb, &outer);
                    push_poly(&mut pb, &inner);
                } else {
                    let mut ring = outer;
                    ring.extend(inner.into_iter().rev());
                    push_poly(&mut pb, &ring);
                }
            }
            Some(_) if !full => {
                let mut wedge = vec![(cx, cy)];
                wedge.extend(outer);
                push_poly(&mut pb, &wedge);
            }
            _ if pie && !full => {
                let mut wedge = vec![(cx, cy)];
                wedge.extend(outer);
                push_poly(&mut pb, &wedge);
            }
            _ => push_poly(&mut pb, &outer),
        }
        self.fill(pb.finish(), color, FillRule::EvenOdd);
    }

    fn ellipse_px(&mut self, bx: (f32, f32, f32, f32), color: [u8; 4], width: Option<f32>) {
        self.sector_px(bx, 0.0, 360.0, width, false, color);
    }

    /// Abgerundetes Rechteck (`rounded_rectangle`); `width`: Umriss nach innen.
    fn rrect_px(&mut self, bx: (f32, f32, f32, f32), r: f32, color: [u8; 4], width: Option<f32>) {
        let (x0, y0, x1, y1) = pil_box(bx);
        let outline = |x0: f32, y0: f32, x1: f32, y1: f32, r: f32| -> Vec<(f32, f32)> {
            let r = r.min((x1 - x0) / 2.0).min((y1 - y0) / 2.0).max(0.0);
            if r <= 0.0 {
                return vec![(x0, y0), (x1, y0), (x1, y1), (x0, y1)];
            }
            let mut pts = ellipse_pts(x1 - r, y0 + r, r, r, 270.0, 360.0);
            pts.extend(ellipse_pts(x1 - r, y1 - r, r, r, 0.0, 90.0));
            pts.extend(ellipse_pts(x0 + r, y1 - r, r, r, 90.0, 180.0));
            pts.extend(ellipse_pts(x0 + r, y0 + r, r, r, 180.0, 270.0));
            pts
        };
        let mut pb = PathBuilder::new();
        push_poly(&mut pb, &outline(x0, y0, x1, y1, r));
        if let Some(wd) = width {
            if 2.0 * wd < (x1 - x0).min(y1 - y0) {
                push_poly(&mut pb, &outline(x0 + wd, y0 + wd, x1 - wd, y1 - wd, r - wd));
            }
        }
        self.fill(pb.finish(), color, FillRule::EvenOdd);
    }

    // ---------------------------------------------------- Helfer der Python-Symbole (Einheitskoordinaten)
    fn line(&mut self, pts: &[(f32, f32)]) {
        let lw = self.lw;
        self.line_w(pts, lw);
    }

    fn line_w(&mut self, pts: &[(f32, f32)], width: f32) {
        let px: Vec<_> = pts.iter().map(|&p| self.p(p)).collect();
        self.stroke_px(&px, width, self.c, true, true);
    }

    fn rect(&mut self, a: f32, b: f32, c: f32, d: f32, r: f32, fill: bool) {
        let bx = self.bx(a, b, c, d);
        let (col, lw) = (self.c, self.lw);
        self.rrect_px(bx, r * self.w, col, (!fill).then_some(lw));
    }

    fn ell(&mut self, a: f32, b: f32, c: f32, d: f32, fill: bool) {
        let bx = self.bx(a, b, c, d);
        let (col, lw) = (self.c, self.lw);
        self.ellipse_px(bx, col, (!fill).then_some(lw));
    }

    fn arc(&mut self, a: f32, b: f32, c: f32, d: f32, s: f32, e: f32) {
        let bx = self.bx(a, b, c, d);
        let (col, lw) = (self.c, self.lw);
        self.sector_px(bx, s, e, Some(lw), false, col);
    }

    fn pie(&mut self, a: f32, b: f32, c: f32, d: f32, s: f32, e: f32) {
        let bx = self.bx(a, b, c, d);
        let col = self.c;
        self.sector_px(bx, s, e, None, true, col);
    }

    fn chord(&mut self, a: f32, b: f32, c: f32, d: f32, s: f32, e: f32) {
        let bx = self.bx(a, b, c, d);
        let col = self.c;
        self.sector_px(bx, s, e, None, false, col);
    }

    fn poly(&mut self, pts: &[(f32, f32)]) {
        let px: Vec<_> = pts.iter().map(|&p| self.p(p)).collect();
        let col = self.c;
        self.poly_px(&px, col);
    }

    /// Transparente Fläche ausschneiden.
    fn cut(&mut self, pts: &[(f32, f32)]) {
        let px: Vec<_> = pts.iter().map(|&p| self.p(p)).collect();
        self.poly_px(&px, CLEAR);
    }

    /// `d.rectangle(...)` in der Symbolfarbe bzw. transparent.
    fn fill_rect(&mut self, a: f32, b: f32, c: f32, d: f32, clear: bool) {
        let bx = self.bx(a, b, c, d);
        let col = if clear { CLEAR } else { self.c };
        self.rect_px(bx, col);
    }

    /// Ellipse transparent ausschneiden.
    fn cut_ell(&mut self, a: f32, b: f32, c: f32, d: f32) {
        let bx = self.bx(a, b, c, d);
        self.ellipse_px(bx, CLEAR, None);
    }
}

// ------------------------------------------------------------ theme._draw_glyph
fn draw_theme(n: &str, d: &mut Pen) {
    let slash = |d: &mut Pen| {
        let pts = [d.p((0.08, 0.92)), d.p((0.92, 0.08))];
        let lw = d.lw;
        d.stroke_px(&pts, lw + 2.0, DANGER, false, false);
    };
    match n {
        "mic" | "mic_off" => {
            d.rect(0.34, 0.02, 0.66, 0.62, 0.16, true);
            d.arc(0.18, 0.28, 0.82, 0.78, 0.0, 180.0);
            d.line(&[(0.5, 0.78), (0.5, 0.94)]);
            d.line(&[(0.32, 0.95), (0.68, 0.95)]);
            if n == "mic_off" {
                slash(d);
            }
        }
        "headphones" | "headphones_off" => {
            d.arc(0.08, 0.06, 0.92, 0.9, 180.0, 360.0);
            d.line(&[(0.08, 0.48), (0.08, 0.62)]);
            d.line(&[(0.92, 0.48), (0.92, 0.62)]);
            d.rect(0.02, 0.56, 0.28, 0.96, 0.08, true);
            d.rect(0.72, 0.56, 0.98, 0.96, 0.08, true);
            if n == "headphones_off" {
                slash(d);
            }
        }
        "speaker" | "speaker_off" => {
            d.poly(&[(0.04, 0.36), (0.24, 0.36), (0.5, 0.1), (0.5, 0.9), (0.24, 0.64), (0.04, 0.64)]);
            if n == "speaker" {
                d.arc(0.36, 0.28, 0.72, 0.72, -50.0, 50.0);
                d.arc(0.3, 0.1, 0.96, 0.9, -50.0, 50.0);
            } else {
                d.line(&[(0.64, 0.34), (0.94, 0.66)]);
                d.line(&[(0.94, 0.34), (0.64, 0.66)]);
            }
        }
        "stream" | "stream_off" => {
            d.ell(0.4, 0.4, 0.6, 0.6, true);
            d.arc(0.24, 0.24, 0.76, 0.76, 135.0, 225.0);
            d.arc(0.24, 0.24, 0.76, 0.76, -45.0, 45.0);
            d.arc(0.04, 0.04, 0.96, 0.96, 135.0, 225.0);
            d.arc(0.04, 0.04, 0.96, 0.96, -45.0, 45.0);
            if n == "stream_off" {
                slash(d);
            }
        }
        "music" => {
            d.ell(0.04, 0.68, 0.36, 0.96, true);
            d.ell(0.6, 0.58, 0.92, 0.86, true);
            d.line(&[(0.34, 0.82), (0.34, 0.12)]);
            d.line(&[(0.9, 0.72), (0.9, 0.04)]);
            d.poly(&[(0.34, 0.1), (0.92, 0.0), (0.92, 0.2), (0.34, 0.3)]);
        }
        "browser" => {
            d.ell(0.04, 0.04, 0.96, 0.96, false);
            d.ell(0.3, 0.04, 0.7, 0.96, false);
            d.line(&[(0.06, 0.5), (0.94, 0.5)]);
        }
        "chat" => {
            d.rect(0.02, 0.08, 0.98, 0.72, 0.18, true);
            d.poly(&[(0.2, 0.68), (0.16, 0.96), (0.46, 0.7)]);
        }
        "game" => {
            d.rect(0.02, 0.24, 0.98, 0.8, 0.26, true);
            d.fill_rect(0.2, 0.47, 0.42, 0.57, true);
            d.fill_rect(0.26, 0.4, 0.36, 0.64, true);
            d.cut_ell(0.62, 0.38, 0.72, 0.48);
            d.cut_ell(0.72, 0.52, 0.82, 0.62);
        }
        "sfx" => d.poly(&star(0.52)),
        "system" => {
            d.rect(0.02, 0.06, 0.98, 0.7, 0.08, false);
            d.line(&[(0.5, 0.72), (0.5, 0.88)]);
            d.line(&[(0.26, 0.92), (0.74, 0.92)]);
        }
        "wave" => {
            for (i, hh) in [0.3, 0.6, 0.95, 0.6, 0.3].into_iter().enumerate() {
                let x = 0.1 + i as f32 * 0.2;
                d.line(&[(x, 0.5 - hh / 2.0), (x, 0.5 + hh / 2.0)]);
            }
        }
        "keyboard" => {
            d.rect(0.0, 0.2, 1.0, 0.8, 0.1, false);
            for ry in [0.38, 0.52] {
                for i in 0..5 {
                    let xx = 0.14 + i as f32 * 0.18;
                    d.fill_rect(xx, ry, xx + 0.08, ry + 0.07, false);
                }
            }
            d.line(&[(0.3, 0.68), (0.7, 0.68)]);
        }
        "navigate" => {
            d.line(&[(0.12, 0.5), (0.88, 0.5)]);
            d.line(&[(0.56, 0.18), (0.88, 0.5), (0.56, 0.82)]);
        }
        "grid" => {
            for gy in [0.04, 0.54] {
                for gx in [0.04, 0.54] {
                    d.rect(gx, gy, gx + 0.42, gy + 0.42, 0.1, true);
                }
            }
        }
        "layers" => {
            d.poly(&[(0.5, 0.06), (0.96, 0.3), (0.5, 0.54), (0.04, 0.3)]);
            d.line(&[(0.06, 0.52), (0.5, 0.74), (0.94, 0.52)]);
            d.line(&[(0.06, 0.74), (0.5, 0.96), (0.94, 0.74)]);
        }
        "record" => {
            d.ell(0.04, 0.04, 0.96, 0.96, false);
            d.ell(0.26, 0.26, 0.74, 0.74, true);
        }
        "play" => d.poly(&[(0.18, 0.06), (0.18, 0.94), (0.92, 0.5)]),
        "puzzle" => {
            d.rect(0.06, 0.24, 0.8, 0.94, 0.08, true);
            d.ell(0.3, 0.02, 0.56, 0.32, true);
            d.ell(0.68, 0.46, 0.98, 0.72, true);
        }
        "search" => {
            d.ell(0.04, 0.04, 0.7, 0.7, false);
            d.line(&[(0.62, 0.62), (0.94, 0.94)]);
        }
        "image" => {
            d.rect(0.02, 0.1, 0.98, 0.9, 0.1, false);
            d.ell(0.62, 0.22, 0.82, 0.42, true);
            d.poly(&[(0.1, 0.82), (0.38, 0.42), (0.58, 0.68), (0.7, 0.56), (0.9, 0.82)]);
        }
        "more" => {
            for x in [0.15, 0.5, 0.85] {
                d.ell(x - 0.1, 0.4, x + 0.1, 0.6, true);
            }
        }
        "fx" => {
            for i in 0..3 {
                let x = 0.18 + i as f32 * 0.32;
                d.line(&[(x, 0.1), (x, 0.9)]);
            }
            for (i, y) in [0.35, 0.7, 0.45].into_iter().enumerate() {
                let x = 0.18 + i as f32 * 0.32;
                d.rect(x - 0.13, y - 0.08, x + 0.13, y + 0.08, 0.04, true);
            }
        }
        "close" => {
            d.line(&[(0.15, 0.15), (0.85, 0.85)]);
            d.line(&[(0.85, 0.15), (0.15, 0.85)]);
        }
        "up" => d.line(&[(0.15, 0.68), (0.5, 0.3), (0.85, 0.68)]),
        "down" => d.line(&[(0.15, 0.32), (0.5, 0.7), (0.85, 0.32)]),
        "input" => d.poly(&[(0.04, 0.34), (0.46, 0.34), (0.46, 0.12), (0.86, 0.5), (0.46, 0.88), (0.46, 0.66),
            (0.04, 0.66)]),
        "plus" => {
            d.line(&[(0.5, 0.1), (0.5, 0.9)]);
            d.line(&[(0.1, 0.5), (0.9, 0.5)]);
        }
        "globe" => {
            d.ell(0.04, 0.04, 0.96, 0.96, false);
            d.ell(0.3, 0.04, 0.7, 0.96, false);
            d.line(&[(0.06, 0.5), (0.94, 0.5)]);
            d.arc(0.12, -0.3, 0.88, 0.36, 40.0, 140.0);
            d.arc(0.12, 0.64, 0.88, 1.3, 220.0, 320.0);
        }
        "sun" => {
            d.ell(0.3, 0.3, 0.7, 0.7, false);
            for i in 0..8 {
                let a = i as f32 * PI / 4.0;
                d.line(&[(0.5 + 0.34 * a.cos(), 0.5 + 0.34 * a.sin()), (0.5 + 0.46 * a.cos(), 0.5 + 0.46 * a.sin())]);
            }
        }
        "moon" => {
            d.ell(0.06, 0.06, 0.94, 0.94, true);
            d.cut_ell(0.34, -0.12, 1.12, 0.66);
        }
        "auto" => {
            d.ell(0.08, 0.08, 0.92, 0.92, false);
            d.pie(0.08, 0.08, 0.92, 0.92, 90.0, 270.0);
        }
        "gear" => {
            // Zahnrad: Scheibe mit 8 Zähnen und Loch
            let (cx, cy) = d.p((0.5, 0.5));
            let w = d.w;
            let hw = 0.085 * w;
            for i in 0..8 {
                let a = i as f32 * PI / 4.0;
                let (ca, sa) = (a.cos(), a.sin());
                let pts: Vec<_> = [(0.26, -1.0), (0.48, -1.0), (0.48, 1.0), (0.26, 1.0)]
                    .iter()
                    .map(|&(r, t): &(f32, f32)| (cx + r * w * ca - t * hw * sa, cy + r * w * sa + t * hw * ca))
                    .collect();
                let col = d.c;
                d.poly_px(&pts, col);
            }
            d.ell(0.17, 0.17, 0.83, 0.83, true);
            let r = 0.15 * w;
            d.ellipse_px((cx - r, cy - r, cx + r, cy + r), CLEAR, None);
        }
        "undo" | "redo" => {
            let sx = |x: f32| if n == "undo" { x } else { 1.0 - x };
            d.line(&[(sx(0.36), 0.6), (sx(0.1), 0.36), (sx(0.36), 0.12)]);
            d.line(&[(sx(0.12), 0.36), (sx(0.62), 0.36)]);
            if n == "undo" {
                d.arc(0.42, 0.36, 0.94, 0.86, -90.0, 90.0);
            } else {
                d.arc(0.06, 0.36, 0.58, 0.86, 90.0, 270.0);
            }
            d.line(&[(sx(0.66), 0.86), (sx(0.48), 0.86)]);
        }
        "spark" => d.poly(&[(0.5, 0.02), (0.62, 0.38), (0.98, 0.5), (0.62, 0.62), (0.5, 0.98), (0.38, 0.62),
            (0.02, 0.5), (0.38, 0.38)]),
        "arrow" => {
            d.line(&[(0.1, 0.5), (0.9, 0.5)]);
            d.line(&[(0.58, 0.2), (0.9, 0.5), (0.58, 0.8)]);
        }
        "arrow_left" => {
            d.line(&[(0.1, 0.5), (0.9, 0.5)]);
            d.line(&[(0.42, 0.2), (0.1, 0.5), (0.42, 0.8)]);
        }
        "chevron" => d.line(&[(0.2, 0.36), (0.5, 0.66), (0.8, 0.36)]),
        "chevron_right" => d.line(&[(0.36, 0.2), (0.66, 0.5), (0.36, 0.8)]),
        "folder" => d.poly(&[(0.04, 0.16), (0.38, 0.16), (0.48, 0.28), (0.96, 0.28), (0.96, 0.86), (0.04, 0.86)]),
        "text" => {
            d.line(&[(0.14, 0.16), (0.86, 0.16)]);
            d.line(&[(0.5, 0.16), (0.5, 0.9)]);
        }
        "camera" => {
            d.rect(0.02, 0.22, 0.68, 0.78, 0.1, true);
            d.poly(&[(0.7, 0.42), (0.98, 0.24), (0.98, 0.76), (0.7, 0.58)]);
        }
        "check" => d.line(&[(0.12, 0.52), (0.4, 0.8), (0.9, 0.2)]),
        "trash" => {
            d.line(&[(0.1, 0.22), (0.9, 0.22)]);
            d.line(&[(0.38, 0.1), (0.62, 0.1)]);
            d.poly(&[(0.18, 0.28), (0.82, 0.28), (0.74, 0.94), (0.26, 0.94)]);
        }
        "edit" => d.poly(&[(0.66, 0.08), (0.92, 0.34), (0.36, 0.9), (0.1, 0.9), (0.1, 0.64)]),
        "import" | "export" => {
            d.rect(0.06, 0.56, 0.94, 0.94, 0.08, false);
            if n == "import" {
                d.line(&[(0.5, 0.06), (0.5, 0.62)]);
                d.line(&[(0.28, 0.42), (0.5, 0.64), (0.72, 0.42)]);
            } else {
                d.line(&[(0.5, 0.64), (0.5, 0.08)]);
                d.line(&[(0.28, 0.28), (0.5, 0.06), (0.72, 0.28)]);
            }
        }
        "deck" => {
            for gy in [0.08, 0.54] {
                for gx in [0.08, 0.54] {
                    d.rect(gx, gy, gx + 0.38, gy + 0.38, 0.12, true);
                }
            }
        }
        "home" => d.poly(&[(0.5, 0.04), (0.96, 0.46), (0.84, 0.46), (0.84, 0.94), (0.58, 0.94), (0.58, 0.64),
            (0.42, 0.64), (0.42, 0.94), (0.16, 0.94), (0.16, 0.46), (0.04, 0.46)]),
        "list" => {
            for y in [0.2, 0.5, 0.8] {
                d.ell(0.04, y - 0.07, 0.18, y + 0.07, true);
                d.line(&[(0.32, y), (0.96, y)]);
            }
        }
        "gauge" => {
            d.arc(0.04, 0.14, 0.96, 1.06, 180.0, 360.0);
            d.line(&[(0.5, 0.6), (0.76, 0.3)]);
            d.ell(0.4, 0.5, 0.6, 0.7, true);
        }
        // Punkt als Ersatz
        _ => d.ell(0.3, 0.3, 0.7, 0.7, true),
    }
}

/// Stern mit fünf Zacken um (0.5, cy).
fn star(cy: f32) -> Vec<(f32, f32)> {
    (0..10)
        .map(|i| {
            let r = if i % 2 == 0 { 0.48 } else { 0.2 };
            let a = -PI / 2.0 + i as f32 * PI / 5.0;
            (0.5 + r * a.cos(), cy + r * a.sin())
        })
        .collect()
}

// ------------------------------------------------------------ iconlib._draw_new
fn draw_new(n: &str, d: &mut Pen) {
    let speaker = [(0.04, 0.36), (0.24, 0.36), (0.5, 0.1), (0.5, 0.9), (0.24, 0.64), (0.04, 0.64)];
    let mirror = |pts: &[(f32, f32)]| -> Vec<(f32, f32)> { pts.iter().map(|&(x, y)| (1.0 - x, y)).collect() };
    match n {
        "volume_up" => {
            d.poly(&speaker);
            d.line(&[(0.62, 0.5), (0.94, 0.5)]);
            d.line(&[(0.78, 0.34), (0.78, 0.66)]);
        }
        "volume_down" => {
            d.poly(&speaker);
            d.line(&[(0.62, 0.5), (0.94, 0.5)]);
        }
        "pause" => {
            d.rect(0.18, 0.1, 0.4, 0.9, 0.06, true);
            d.rect(0.6, 0.1, 0.82, 0.9, 0.06, true);
        }
        "stop" => d.rect(0.14, 0.14, 0.86, 0.86, 0.1, true),
        "prev" | "next" => {
            let mut pts = vec![(0.14, 0.12), (0.7, 0.5), (0.14, 0.88)];
            let mut bar = (0.74, 0.12, 0.88, 0.88);
            if n == "prev" {
                pts = mirror(&pts);
                bar = (0.12, 0.12, 0.26, 0.88);
            }
            d.poly(&pts);
            d.rect(bar.0, bar.1, bar.2, bar.3, 0.04, true);
        }
        "shuffle" => {
            d.line(&[(0.06, 0.26), (0.3, 0.26), (0.66, 0.74), (0.88, 0.74)]);
            d.line(&[(0.06, 0.74), (0.3, 0.74), (0.66, 0.26), (0.88, 0.26)]);
            d.poly(&[(0.82, 0.12), (0.98, 0.26), (0.82, 0.4)]);
            d.poly(&[(0.82, 0.6), (0.98, 0.74), (0.82, 0.88)]);
        }
        "repeat" => {
            d.line(&[(0.12, 0.6), (0.12, 0.3), (0.82, 0.3)]);
            d.line(&[(0.88, 0.4), (0.88, 0.7), (0.18, 0.7)]);
            d.poly(&[(0.76, 0.14), (0.96, 0.3), (0.76, 0.46)]);
            d.poly(&[(0.24, 0.54), (0.04, 0.7), (0.24, 0.86)]);
        }
        "video" => {
            d.rect(0.02, 0.22, 0.68, 0.78, 0.12, true);
            d.poly(&[(0.72, 0.4), (0.98, 0.24), (0.98, 0.76), (0.72, 0.6)]);
        }
        "film" => {
            d.rect(0.12, 0.04, 0.88, 0.96, 0.08, true);
            for y in [0.12, 0.3, 0.48, 0.66, 0.84] {
                for x in [0.18, 0.74] {
                    d.fill_rect(x, y, x + 0.08, y + 0.08, true);
                }
            }
            d.fill_rect(0.32, 0.12, 0.68, 0.46, true);
            d.fill_rect(0.32, 0.54, 0.68, 0.88, true);
        }
        "live" => {
            // Schrift „LIVE“ folgt nach dem Zeichnen (live_text)
            d.rect(0.0, 0.26, 1.0, 0.74, 0.12, false);
            d.ell(0.1, 0.43, 0.24, 0.57, true);
        }
        "scene" => {
            // Filmklappe
            d.rect(0.06, 0.42, 0.94, 0.94, 0.06, true);
            d.poly(&[(0.04, 0.24), (0.86, 0.04), (0.92, 0.24), (0.1, 0.44)]);
            for x in [0.28f32, 0.52, 0.76] {
                let o = (x - 0.28) * 0.25;
                d.cut(&[(x, 0.2 - o + 0.02), (x + 0.1, 0.18 - o + 0.02), (x - 0.02, 0.4 - o), (x - 0.12, 0.42 - o)]);
            }
        }
        "screen" => {
            d.rect(0.04, 0.1, 0.96, 0.7, 0.06, false);
            d.line(&[(0.5, 0.72), (0.5, 0.86)]);
            d.line(&[(0.28, 0.9), (0.72, 0.9)]);
        }
        "eye" | "eye_off" => {
            d.ell(0.02, 0.18, 0.98, 0.82, false);
            d.ell(0.36, 0.36, 0.64, 0.64, true);
            if n == "eye_off" {
                d.line(&[(0.1, 0.9), (0.9, 0.1)]);
            }
        }
        "users" => {
            d.ell(0.12, 0.08, 0.46, 0.42, true);
            d.pie(0.0, 0.48, 0.58, 1.1, 180.0, 360.0);
            d.ell(0.56, 0.14, 0.86, 0.44, true);
            d.pie(0.5, 0.52, 1.0, 1.06, 180.0, 360.0);
        }
        "heart" => {
            d.ell(0.04, 0.12, 0.52, 0.58, true);
            d.ell(0.48, 0.12, 0.96, 0.58, true);
            d.poly(&[(0.07, 0.44), (0.93, 0.44), (0.5, 0.92)]);
        }
        "star" => d.poly(&star(0.53)),
        "bell" => {
            d.chord(0.18, 0.1, 0.82, 0.74, 180.0, 360.0);
            d.rect(0.18, 0.4, 0.82, 0.72, 0.0, true);
            d.poly(&[(0.18, 0.66), (0.82, 0.66), (0.94, 0.8), (0.06, 0.8)]);
            d.ell(0.4, 0.8, 0.6, 0.98, true);
            d.ell(0.44, 0.02, 0.56, 0.14, true);
        }
        "power" => {
            d.arc(0.1, 0.12, 0.9, 0.92, -55.0, 235.0);
            d.line(&[(0.5, 0.04), (0.5, 0.46)]);
        }
        "lock" | "unlock" => {
            d.rect(0.14, 0.44, 0.86, 0.96, 0.08, true);
            if n == "lock" {
                d.arc(0.26, 0.04, 0.74, 0.62, 180.0, 360.0);
                d.line(&[(0.26, 0.33), (0.26, 0.46)]);
                d.line(&[(0.74, 0.33), (0.74, 0.46)]);
            } else {
                d.arc(0.46, 0.04, 0.94, 0.62, 180.0, 360.0);
                d.line(&[(0.46, 0.33), (0.46, 0.46)]);
            }
            d.ell(0.43, 0.6, 0.57, 0.74, false);
        }
        "refresh" => {
            d.arc(0.1, 0.1, 0.9, 0.9, 30.0, 320.0);
            d.poly(&[(0.64, 0.02), (0.92, 0.2), (0.62, 0.34)]);
        }
        "terminal" => {
            d.rect(0.02, 0.1, 0.98, 0.9, 0.08, false);
            d.line(&[(0.2, 0.34), (0.38, 0.5), (0.2, 0.66)]);
            d.line(&[(0.48, 0.68), (0.76, 0.68)]);
        }
        "download" | "upload" => {
            d.line(&[(0.1, 0.7), (0.1, 0.92), (0.9, 0.92), (0.9, 0.7)]);
            if n == "download" {
                d.line(&[(0.5, 0.06), (0.5, 0.66)]);
                d.line(&[(0.26, 0.44), (0.5, 0.68), (0.74, 0.44)]);
            } else {
                d.line(&[(0.5, 0.7), (0.5, 0.08)]);
                d.line(&[(0.26, 0.3), (0.5, 0.06), (0.74, 0.3)]);
            }
        }
        "link" => {
            for (a, b) in [(0.04, 0.3), (0.42, 0.3)] {
                d.rect(a, b, a + 0.54, b + 0.4, 0.2, false);
            }
            d.fill_rect(0.42, 0.36, 0.58, 0.64, true);
            d.line(&[(0.34, 0.5), (0.66, 0.5)]);
        }
        "copy" => {
            d.rect(0.3, 0.04, 0.96, 0.7, 0.08, false);
            d.rect(0.04, 0.3, 0.7, 0.96, 0.08, true);
        }
        "save" => {
            d.poly(&[(0.06, 0.06), (0.76, 0.06), (0.94, 0.24), (0.94, 0.94), (0.06, 0.94)]);
            d.fill_rect(0.22, 0.06, 0.66, 0.32, true);
            d.ell(0.34, 0.5, 0.66, 0.82, false);
        }
        "wifi" => {
            d.ell(0.42, 0.78, 0.58, 0.94, true);
            d.arc(0.26, 0.5, 0.74, 0.98, 225.0, 315.0);
            d.arc(0.12, 0.3, 0.88, 1.06, 225.0, 315.0);
            d.arc(-0.02, 0.1, 1.02, 1.14, 225.0, 315.0);
        }
        "bluetooth" => d.line(&[(0.26, 0.28), (0.74, 0.7), (0.5, 0.92), (0.5, 0.08), (0.74, 0.3), (0.26, 0.72)]),
        "calendar" => {
            d.rect(0.06, 0.14, 0.94, 0.94, 0.08, false);
            d.fill_rect(0.06, 0.14, 0.94, 0.34, false);
            d.line(&[(0.3, 0.04), (0.3, 0.2)]);
            d.line(&[(0.7, 0.04), (0.7, 0.2)]);
            for y in [0.48, 0.7] {
                for x in [0.22, 0.44, 0.66] {
                    d.rect(x, y, x + 0.12, y + 0.12, 0.03, true);
                }
            }
        }
        "lightbulb" => {
            d.ell(0.18, 0.02, 0.82, 0.66, true);
            d.poly(&[(0.3, 0.52), (0.7, 0.52), (0.64, 0.76), (0.36, 0.76)]);
            d.line(&[(0.38, 0.84), (0.62, 0.84)]);
            d.line(&[(0.42, 0.95), (0.58, 0.95)]);
        }
        "plug" => {
            d.rect(0.18, 0.3, 0.82, 0.7, 0.12, true);
            d.line(&[(0.36, 0.06), (0.36, 0.3)]);
            d.line(&[(0.64, 0.06), (0.64, 0.3)]);
            d.line(&[(0.5, 0.7), (0.5, 0.96)]);
        }
        "fan" => {
            // drei Flügel um die Nabe
            for ang in [-90.0f32, 30.0, 150.0] {
                let a = ang.to_radians();
                let pts: Vec<_> = (0..24)
                    .map(|i| {
                        let t = 2.0 * PI * i as f32 / 24.0;
                        let (ex, ey) = (0.25 + 0.22 * t.cos(), 0.11 * t.sin());
                        let (ex, ey) = (ex * a.cos() - ey * a.sin(), ex * a.sin() + ey * a.cos());
                        (0.5 + ex, 0.5 + ey)
                    })
                    .collect();
                d.poly(&pts);
            }
            d.ell(0.38, 0.38, 0.62, 0.62, true);
            d.cut_ell(0.44, 0.44, 0.56, 0.56);
        }
        "door" => {
            d.rect(0.18, 0.04, 0.82, 0.96, 0.04, false);
            d.ell(0.62, 0.48, 0.72, 0.58, true);
        }
        "left" | "right" => {
            let mut pts = vec![(0.86, 0.5), (0.14, 0.5)];
            let mut head = vec![(0.44, 0.2), (0.14, 0.5), (0.44, 0.8)];
            if n == "right" {
                pts = mirror(&pts);
                head = mirror(&head);
            }
            d.line(&pts);
            d.line(&head);
        }
        "minus" => d.line(&[(0.14, 0.5), (0.86, 0.5)]),
        "mail" => {
            d.rect(0.02, 0.16, 0.98, 0.84, 0.08, false);
            d.line(&[(0.06, 0.22), (0.5, 0.56), (0.94, 0.22)]);
        }
        "phone" => {
            d.rect(0.24, 0.02, 0.76, 0.98, 0.12, false);
            d.line(&[(0.42, 0.86), (0.58, 0.86)]);
        }
        "coffee" => {
            d.rect(0.08, 0.34, 0.7, 0.92, 0.1, true);
            d.arc(0.56, 0.42, 0.94, 0.76, -90.0, 90.0);
            d.line(&[(0.26, 0.06), (0.26, 0.22)]);
            d.line(&[(0.42, 0.06), (0.42, 0.22)]);
            d.line(&[(0.58, 0.06), (0.58, 0.22)]);
        }
        "rocket" => {
            d.poly(&[(0.5, 0.02), (0.66, 0.2), (0.66, 0.68), (0.34, 0.68), (0.34, 0.2)]);
            d.poly(&[(0.34, 0.42), (0.14, 0.72), (0.14, 0.8), (0.34, 0.68)]);
            d.poly(&[(0.66, 0.42), (0.86, 0.72), (0.86, 0.8), (0.66, 0.68)]);
            d.poly(&[(0.4, 0.74), (0.6, 0.74), (0.5, 0.98)]);
            d.cut_ell(0.42, 0.26, 0.58, 0.42);
        }
        "trophy" => {
            d.poly(&[(0.22, 0.06), (0.78, 0.06), (0.72, 0.46), (0.5, 0.6), (0.28, 0.46)]);
            d.arc(0.02, 0.1, 0.32, 0.46, 90.0, 270.0);
            d.arc(0.68, 0.1, 0.98, 0.46, -90.0, 90.0);
            d.line(&[(0.5, 0.6), (0.5, 0.8)]);
            d.rect(0.28, 0.8, 0.72, 0.94, 0.03, true);
        }
        "gift" => {
            d.rect(0.06, 0.32, 0.94, 0.52, 0.04, true);
            d.rect(0.12, 0.52, 0.88, 0.96, 0.04, true);
            d.fill_rect(0.45, 0.32, 0.55, 0.96, true);
            d.arc(0.18, 0.06, 0.5, 0.36, 180.0, 360.0);
            d.arc(0.5, 0.06, 0.82, 0.36, 180.0, 360.0);
        }
        "flag" => {
            d.line(&[(0.16, 0.04), (0.16, 0.96)]);
            d.poly(&[(0.2, 0.06), (0.9, 0.06), (0.74, 0.3), (0.9, 0.54), (0.2, 0.54)]);
        }
        "pin" => {
            d.ell(0.16, 0.02, 0.84, 0.7, true);
            d.poly(&[(0.2, 0.5), (0.8, 0.5), (0.5, 0.98)]);
            d.cut_ell(0.37, 0.23, 0.63, 0.49);
        }
        "smile" => {
            d.ell(0.04, 0.04, 0.96, 0.96, false);
            d.ell(0.3, 0.3, 0.4, 0.42, true);
            d.ell(0.6, 0.3, 0.7, 0.42, true);
            d.arc(0.26, 0.3, 0.74, 0.76, 20.0, 160.0);
        }
        "thumbs_up" => {
            d.rect(0.04, 0.44, 0.24, 0.94, 0.04, true);
            d.poly(&[(0.3, 0.44), (0.5, 0.06), (0.62, 0.1), (0.58, 0.38), (0.9, 0.38), (0.96, 0.5), (0.86, 0.94),
                (0.3, 0.94)]);
        }
        _ => {}
    }
}

/// Schrift „LIVE“ (fett, mittig um P(0.62, 0.51)) in voller Auflösung.
fn live_text(img: &mut RgbaImage, pen: &Pen) {
    let Some(f) = render::font(render::Style::Bold) else { return };
    // Pillow: Größe = Geviert in Pixeln; ab_glyph: Größe = Zeilenhöhe (Ober- bis Unterlänge)
    let em = ((pen.w * 0.24) as i32).max(6) as f32;
    let px = match f.units_per_em() {
        Some(upm) if upm > 0.0 => em * f.height_unscaled() / upm,
        _ => em,
    };
    let sf = f.as_scaled(PxScale::from(px));
    let (cx, cy) = pen.p((0.62, 0.51));
    let tw = render::text_width(&f, px, "LIVE");
    let top = cy - (sf.ascent() - sf.descent()) / 2.0;
    render::draw_text(img, &f, px, cx - tw / 2.0, top, "LIVE", [pen.c[0], pen.c[1], pen.c[2]]);
}

// ------------------------------------------------------------ livekeys.icon
fn draw_live(n: &str, d: &mut Pen) {
    let s = d.w;
    let c = d.c;
    let lw = d.lw;
    let p = |x: f32, y: f32| (x * s, y * s);
    let bx = |a: f32, b: f32, cc: f32, dd: f32| (a * s, b * s, cc * s, dd * s);
    // d.line ohne Enden; `joint`: runde Ecken
    let seg = |d: &mut Pen, pts: &[(f32, f32)], width: f32, joint: bool| {
        let px: Vec<_> = pts.iter().map(|&(x, y)| p(x, y)).collect();
        d.stroke_px(&px, width, c, false, joint);
    };
    match n {
        "clock" => {
            d.ellipse_px(bx(0.06, 0.06, 0.94, 0.94), c, Some(lw));
            seg(d, &[(0.5, 0.5), (0.5, 0.24)], lw, false);
            seg(d, &[(0.5, 0.5), (0.7, 0.6)], lw, false);
        }
        "hourglass" => {
            seg(d, &[(0.2, 0.06), (0.8, 0.06)], lw, false);
            seg(d, &[(0.2, 0.94), (0.8, 0.94)], lw, false);
            seg(d, &[(0.26, 0.08), (0.26, 0.2), (0.74, 0.8), (0.74, 0.92)], lw, true);
            seg(d, &[(0.74, 0.08), (0.74, 0.2), (0.26, 0.8), (0.26, 0.92)], lw, true);
            d.poly_px(&[p(0.32, 0.92), p(0.5, 0.7), p(0.68, 0.92)], c);
        }
        "stopwatch" => {
            d.ellipse_px(bx(0.1, 0.18, 0.9, 0.98), c, Some(lw));
            seg(d, &[(0.38, 0.05), (0.62, 0.05)], lw, false);
            seg(d, &[(0.5, 0.05), (0.5, 0.18)], lw, false);
            seg(d, &[(0.5, 0.58), (0.66, 0.4)], lw, false);
            seg(d, &[(0.82, 0.2), (0.9, 0.28)], lw, false);
        }
        "cpu" => {
            d.rrect_px(bx(0.2, 0.2, 0.8, 0.8), s * 0.08, c, Some(lw));
            d.rect_px(bx(0.38, 0.38, 0.62, 0.62), c);
            for t in [0.34, 0.5, 0.66] {
                seg(d, &[(t, 0.02), (t, 0.18)], lw, false);
                seg(d, &[(t, 0.82), (t, 0.98)], lw, false);
                seg(d, &[(0.02, t), (0.18, t)], lw, false);
                seg(d, &[(0.82, t), (0.98, t)], lw, false);
            }
        }
        "memory" => {
            d.rrect_px(bx(0.04, 0.26, 0.96, 0.66), s * 0.05, c, Some(lw));
            for i in 0..4 {
                let x = 0.16 + i as f32 * 0.19;
                d.rect_px(bx(x, 0.36, x + 0.11, 0.56), c);
            }
            for i in 0..6 {
                let x = 0.12 + i as f32 * 0.152;
                seg(d, &[(x, 0.68), (x, 0.82)], lw, false);
            }
        }
        "thermometer" => {
            d.ellipse_px(bx(0.3, 0.6, 0.7, 0.98), c, None);
            d.rrect_px(bx(0.38, 0.04, 0.62, 0.72), s * 0.12, c, Some(lw));
            seg(d, &[(0.5, 0.36), (0.5, 0.75)], lw, false);
            let thin = ((lw as i32) / 2).max(1) as f32;
            for y in [0.18, 0.3, 0.42] {
                seg(d, &[(0.7, y), (0.86, y)], thin, false);
            }
        }
        "network" => {
            seg(d, &[(0.32, 0.08), (0.32, 0.9)], lw, false);
            seg(d, &[(0.12, 0.7), (0.32, 0.92), (0.52, 0.7)], lw, true);
            seg(d, &[(0.68, 0.92), (0.68, 0.1)], lw, false);
            seg(d, &[(0.48, 0.3), (0.68, 0.08), (0.88, 0.3)], lw, true);
        }
        "battery" | "battery_charging" => {
            d.rrect_px(bx(0.04, 0.26, 0.86, 0.74), s * 0.08, c, Some(lw));
            d.rect_px(bx(0.88, 0.4, 0.97, 0.6), c);
            if n == "battery_charging" {
                d.poly_px(&[p(0.5, 0.3), p(0.3, 0.54), p(0.46, 0.54), p(0.4, 0.72), p(0.62, 0.46), p(0.46, 0.46)], c);
            } else {
                d.rect_px(bx(0.14, 0.36, 0.56, 0.64), c);
            }
        }
        "disk" => {
            d.rrect_px(bx(0.06, 0.2, 0.94, 0.8), s * 0.1, c, Some(lw));
            seg(d, &[(0.08, 0.56), (0.92, 0.56)], lw, false);
            d.ellipse_px(bx(0.7, 0.62, 0.82, 0.74), c, None);
        }
        "gauge" => {
            d.sector_px(bx(0.04, 0.14, 0.96, 1.06), 180.0, 360.0, Some(lw), false, c);
            seg(d, &[(0.5, 0.6), (0.74, 0.32)], lw, false);
            d.ellipse_px(bx(0.42, 0.52, 0.58, 0.68), c, None);
        }
        _ => d.ellipse_px(bx(0.3, 0.3, 0.7, 0.7), c, None),
    }
}
