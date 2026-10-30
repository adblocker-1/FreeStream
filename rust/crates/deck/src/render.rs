//! Tastenbilder (wie `freestream/render.py`): Symbol, Titel mit Umriss nach den Einstellungen des Zustands
//! (FontSize, FontStyle, TitleAlignment, TitleColor, ShowTitle …), Seiten-Hintergrund, Hinweise.

use ab_glyph::{point, Font, FontArc, Glyph, PxScale, ScaleFont};
use image::{imageops, Rgba, RgbaImage};
use serde_json::{Map, Value};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

/// Schriftschnitt.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Style {
    Regular,
    Bold,
    Italic,
    BoldItalic,
}

const FONT_CANDIDATES: &[(Style, &[&str])] = &[
    (Style::Regular, &["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
                       "/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf"]),
    (Style::Bold, &["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
                    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf"]),
    (Style::Italic, &["/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf", "/usr/share/fonts/truetype/noto/NotoSans-Italic.ttf"]),
    (Style::BoldItalic, &["/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf",
                          "/usr/share/fonts/truetype/noto/NotoSans-BoldItalic.ttf"]),
];

/// Schrift des Systems (DejaVu, sonst Noto); Kursiv fällt auf Normal zurück.
pub fn font(style: Style) -> Option<FontArc> {
    static FONTS: OnceLock<Mutex<HashMap<Style, Option<FontArc>>>> = OnceLock::new();
    let cache = FONTS.get_or_init(Default::default);
    let mut c = cache.lock().unwrap();
    if let Some(f) = c.get(&style) {
        return f.clone();
    }
    let load = |s: Style| {
        FONT_CANDIDATES
            .iter()
            .find(|(st, _)| *st == s)
            .and_then(|(_, files)| files.iter().find_map(|f| std::fs::read(f).ok()))
            .and_then(|b| FontArc::try_from_vec(b).ok())
    };
    let f = load(style).or_else(|| match style {
        Style::BoldItalic => load(Style::Bold).or_else(|| load(Style::Regular)),
        _ => load(Style::Regular),
    });
    c.insert(style, f.clone());
    f
}

/// Schrift einer bestimmten Familie (Titelstil „FontFamily“) über fc-match, wie die Python-Version; sonst
/// die Standardschrift.
pub fn font_family(family: &str, style: Style) -> Option<FontArc> {
    if family.trim().is_empty() {
        return font(style);
    }
    static FAMILIES: OnceLock<Mutex<HashMap<(String, Style), Option<FontArc>>>> = OnceLock::new();
    let cache = FAMILIES.get_or_init(Default::default);
    let key = (family.to_string(), style);
    if let Some(f) = cache.lock().unwrap().get(&key) {
        return f.clone().or_else(|| font(style));
    }
    let st = match style {
        Style::Regular => "regular",
        Style::Bold => "bold",
        Style::Italic => "italic",
        Style::BoldItalic => "bold:italic",
    };
    let file = std::process::Command::new("fc-match")
        .args(["-f", "%{file}", &format!("{family}:{st}")])
        .output()
        .ok()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .filter(|f| !f.is_empty());
    let f = file.and_then(|f| std::fs::read(f).ok()).and_then(|b| FontArc::try_from_vec(b).ok());
    cache.lock().unwrap().insert(key, f.clone());
    f.or_else(|| font(style))
}

fn style_of(state: &Map<String, Value>) -> Style {
    let s = state.get("FontStyle").and_then(Value::as_str).unwrap_or("").to_lowercase();
    match (s.contains("bold"), s.contains("italic")) {
        (true, true) => Style::BoldItalic,
        (true, false) => Style::Bold,
        (false, true) => Style::Italic,
        _ => Style::Regular,
    }
}

/// „#rrggbb“ / „#rgb“ -> Farbe.
pub fn parse_color(c: Option<&str>, default: [u8; 3]) -> [u8; 3] {
    let Some(c) = c.map(|c| c.trim().trim_start_matches('#')) else { return default };
    let hex = |s: &str| u8::from_str_radix(s, 16).ok();
    match c.len() {
        3 => {
            let v: Vec<u8> = c.chars().filter_map(|ch| hex(&format!("{ch}{ch}"))).collect();
            if v.len() == 3 { [v[0], v[1], v[2]] } else { default }
        }
        n if n >= 6 => match (hex(&c[0..2]), hex(&c[2..4]), hex(&c[4..6])) {
            (Some(r), Some(g), Some(b)) => [r, g, b],
            _ => default,
        },
        _ => default,
    }
}

// ------------------------------------------------------------------------------------------- Text
/// Breite einer Zeile in Pixeln.
pub fn text_width(f: &FontArc, size: f32, text: &str) -> f32 {
    let sf = f.as_scaled(PxScale::from(size));
    let mut w = 0.0;
    let mut prev = None;
    for ch in text.chars() {
        let id = sf.glyph_id(ch);
        if let Some(p) = prev {
            w += sf.kern(p, id);
        }
        w += sf.h_advance(id);
        prev = Some(id);
    }
    w
}

/// Zeile zeichnen; (x, y) = linke obere Ecke der Zeile.
pub fn draw_text(img: &mut RgbaImage, f: &FontArc, size: f32, x: f32, y: f32, text: &str, color: [u8; 3]) {
    let sf = f.as_scaled(PxScale::from(size));
    let mut caret = point(x, y + sf.ascent());
    let mut prev = None;
    for ch in text.chars() {
        let id = sf.glyph_id(ch);
        if let Some(p) = prev {
            caret.x += sf.kern(p, id);
        }
        let g: Glyph = id.with_scale_and_position(PxScale::from(size), caret);
        caret.x += sf.h_advance(id);
        prev = Some(id);
        if let Some(og) = f.outline_glyph(g) {
            let b = og.px_bounds();
            og.draw(|gx, gy, cov| {
                let px = b.min.x as i32 + gx as i32;
                let py = b.min.y as i32 + gy as i32;
                if px < 0 || py < 0 || px >= img.width() as i32 || py >= img.height() as i32 {
                    return;
                }
                blend(img.get_pixel_mut(px as u32, py as u32), color, cov.min(1.0));
            });
        }
    }
}

fn blend(p: &mut Rgba<u8>, c: [u8; 3], a: f32) {
    // „Über“-Mischung: auf durchsichtigem Grund (Felder des Touchstreifens) bleibt die Farbe erhalten
    let inv = 1.0 - a;
    let old_a = p[3] as f32 / 255.0;
    let out_a = a + old_a * inv;
    if out_a <= 0.0 {
        return;
    }
    for i in 0..3 {
        p[i] = ((c[i] as f32 * a + p[i] as f32 * old_a * inv) / out_a).round().min(255.0) as u8;
    }
    p[3] = (out_a * 255.0).round().min(255.0) as u8;
}

/// Titel einer Taste wie in der Elgato-App (Größe relativ zu 72 px, Umriss, Position, Unterstreichung).
pub fn draw_title(img: &mut RgbaImage, title: &str, state: &Map<String, Value>, scale: f32) {
    if title.is_empty() {
        return;
    }
    let style = style_of(state);
    let family = state.get("FontFamily").and_then(Value::as_str).unwrap_or("");
    let Some(f) = font_family(family, style) else { return };
    let (w, h) = (img.width() as f32, img.height() as f32);
    let mut size = (state.get("FontSize").and_then(Value::as_f64).unwrap_or(12.0) as f32 * 1.05 * scale).round();
    let lines: Vec<&str> = title.split('\n').collect();
    let widest = lines.iter().map(|l| text_width(&f, size, l)).fold(0.0, f32::max);
    if widest > w - 4.0 && widest > 0.0 {
        size = (size * (w - 4.0) / widest).floor().max(7.0);
    }
    let color = parse_color(state.get("TitleColor").and_then(Value::as_str), [255, 255, 255]);
    let outline = state.get("OutlineThickness").and_then(Value::as_f64).unwrap_or(2.0) as f32;
    let stroke = (outline * scale * 0.6).round() as i32;
    let line_h = (size * 1.15).floor();
    let total = line_h * lines.len() as f32;
    let pad = (4.0 * scale).floor();
    let align = state.get("TitleAlignment").and_then(Value::as_str).unwrap_or("bottom").to_lowercase();
    let mut y = match align.as_str() {
        "top" => pad,
        "middle" | "center" => ((h - total) / 2.0).floor(),
        _ => h - total - pad,
    };
    let underline = state.get("FontUnderline").and_then(Value::as_bool).unwrap_or(false);
    for line in lines {
        if !line.is_empty() {
            let tw = text_width(&f, size, line);
            let x = (w - tw) / 2.0;
            // Umriss: schwarzer Text ringsum versetzt, dann der Titel darüber
            for dx in -stroke..=stroke {
                for dy in -stroke..=stroke {
                    if (dx != 0 || dy != 0) && dx * dx + dy * dy <= stroke * stroke + 1 {
                        draw_text(img, &f, size, x + dx as f32, y + dy as f32, line, [0, 0, 0]);
                    }
                }
            }
            draw_text(img, &f, size, x, y, line, color);
            if underline {
                let uy = (y + size + 1.0) as i32;
                let th = (size as i32 / 12).max(1);
                for yy in uy..uy + th {
                    for xx in x as i32..(x + tw) as i32 {
                        if xx >= 0 && yy >= 0 && (xx as u32) < img.width() && (yy as u32) < img.height() {
                            img.put_pixel(xx as u32, yy as u32, Rgba([color[0], color[1], color[2], 255]));
                        }
                    }
                }
            }
        }
        y += line_h;
    }
}

// ----------------------------------------------------------------------------------------- Bilder
/// Bild laden (PNG/JPG/GIF/BMP/WebP oder SVG) und auf w×h bringen. Elgato speichert oft ohne Endung
/// („Images/btn_hotkey“) – dann werden die üblichen Endungen gesucht.
pub fn load_image(path: &Path, w: u32, h: u32) -> Option<RgbaImage> {
    static CACHE: OnceLock<Mutex<HashMap<(PathBuf, u32, u32, u64), RgbaImage>>> = OnceLock::new();
    let path = if path.exists() {
        path.to_path_buf()
    } else {
        [".png", ".svg", "@2x.png", ".gif", ".jpg"].iter().map(|e| PathBuf::from(format!("{}{e}", path.display()))).find(|p| p.exists())?
    };
    let mtime = std::fs::metadata(&path).and_then(|m| m.modified()).ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok()).map(|d| d.as_secs()).unwrap_or(0);
    let key = (path.clone(), w, h, mtime);
    let cache = CACHE.get_or_init(Default::default);
    if let Some(img) = cache.lock().unwrap().get(&key) {
        return Some(img.clone());
    }
    let img = if path.extension().map(|e| e.eq_ignore_ascii_case("svg")).unwrap_or(false) {
        load_svg(&path, w, h)?
    } else {
        let im = image::open(&path).ok()?.to_rgba8();
        fit(&im, w, h)
    };
    let mut c = cache.lock().unwrap();
    if c.len() > 512 {
        c.clear();
    }
    c.insert(key, img.clone());
    Some(img)
}

fn load_svg(path: &Path, w: u32, h: u32) -> Option<RgbaImage> {
    let data = std::fs::read(path).ok()?;
    let tree = resvg::usvg::Tree::from_data(&data, &resvg::usvg::Options::default()).ok()?;
    let mut pix = resvg::tiny_skia::Pixmap::new(w, h)?;
    let s = tree.size();
    let t = resvg::tiny_skia::Transform::from_scale(w as f32 / s.width(), h as f32 / s.height());
    resvg::render(&tree, t, &mut pix.as_mut());
    // tiny-skia speichert vormultipliziert
    let mut img = RgbaImage::new(w, h);
    for (i, p) in pix.pixels().iter().enumerate() {
        let c = p.demultiply();
        img.put_pixel(i as u32 % w, i as u32 / w, Rgba([c.red(), c.green(), c.blue(), c.alpha()]));
    }
    Some(img)
}

/// Auf w×h skalieren (wie Pillow LANCZOS).
pub fn fit(img: &RgbaImage, w: u32, h: u32) -> RgbaImage {
    if img.dimensions() == (w, h) {
        return img.clone();
    }
    imageops::resize(img, w, h, imageops::FilterType::Lanczos3)
}

/// Bild mit pct % Deckkraft (Einstellung „Deckkraft“).
pub fn with_opacity(img: &mut RgbaImage, pct: u8) {
    if pct >= 100 {
        return;
    }
    for p in img.pixels_mut() {
        p[3] = (p[3] as u32 * pct as u32 / 100) as u8;
    }
}

/// Ersatzbild ohne Symbol: dunkle Fläche mit dem Namen der Aktion.
pub fn fallback_icon(w: u32, h: u32, label: &str) -> RgbaImage {
    let mut img = RgbaImage::from_pixel(w, h, Rgba([40, 40, 46, 255]));
    if label.is_empty() {
        return img;
    }
    let Some(f) = font(Style::Bold) else { return img };
    let size = (w as f32 / 7.0).floor().max(9.0);
    let mut lines: Vec<String> = Vec::new();
    let mut cur = String::new();
    for word in label.split_whitespace() {
        let t = if cur.is_empty() { word.to_string() } else { format!("{cur} {word}") };
        if text_width(&f, size, &t) > w as f32 - 8.0 && !cur.is_empty() {
            lines.push(std::mem::replace(&mut cur, word.to_string()));
        } else {
            cur = t;
        }
    }
    lines.push(cur);
    lines.truncate(4);
    let lh = (size * 1.15).floor();
    let mut y = ((h as f32 - lh * lines.len() as f32) / 2.0).floor();
    for line in &lines {
        let tw = text_width(&f, size, line);
        draw_text(&mut img, &f, size, (w as f32 - tw) / 2.0, y, line, [200, 200, 210]);
        y += lh;
    }
    img
}

/// Hinweis auf der Taste nach einer Aktion (wie „Done“/„Error“ in der Python-Version).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Overlay {
    Ok,
    Alert,
    /// Fortschritt einer Multi-Aktion: `done` von `steps` Schritten erledigt
    Progress { done: u16, steps: u16 },
}

/// Taste zusammensetzen: Grund (Hintergrund-Kachel oder Schwarz), Symbol, Titel, Hinweis.
pub fn compose_key(
    (w, h): (u32, u32),
    icon: Option<&RgbaImage>,
    title: &str,
    state: &Map<String, Value>,
    overlay: Option<Overlay>,
    base: Option<&RgbaImage>,
) -> RgbaImage {
    let mut img = base.map(|b| fit(b, w, h)).unwrap_or_else(|| RgbaImage::from_pixel(w, h, Rgba([0, 0, 0, 255])));
    if let Some(icon) = icon {
        let mut icon = fit(icon, w, h);
        let op = state.get("IconOpacity").and_then(Value::as_u64).unwrap_or(100).min(100) as u8;
        with_opacity(&mut icon, op);
        imageops::overlay(&mut img, &icon, 0, 0);
    }
    let show = state.get("ShowTitle").and_then(Value::as_bool).unwrap_or(true);
    if show && !title.is_empty() && overlay.is_none() {
        draw_title(&mut img, title, state, w as f32 / 72.0);
    }
    match overlay {
        Some(Overlay::Ok) => badge(&mut img, [46, 204, 113], &freestream_core::tr!("Done")),
        Some(Overlay::Alert) => badge(&mut img, [229, 72, 77], &freestream_core::tr!("Error")),
        Some(Overlay::Progress { done, steps }) => progress(&mut img, done, steps),
        None => {}
    }
    img
}

fn badge(img: &mut RgbaImage, color: [u8; 3], text: &str) {
    for p in img.pixels_mut() {
        for i in 0..3 {
            p[i] = (p[i] as u32 * 80 / 255) as u8;
        }
    }
    let (w, h) = img.dimensions();
    let Some(f) = font(Style::Bold) else { return };
    let mut size = h as f32 * 0.2;
    let tw = text_width(&f, size, text);
    if tw > w as f32 * 0.9 {
        size *= w as f32 * 0.9 / tw;
    }
    let tw = text_width(&f, size, text);
    draw_text(img, &f, size, (w as f32 - tw) / 2.0, (h as f32 - size) / 2.0, text, color);
}

/// Fortschritt einer Multi-Aktion (wie `_progress`): „Schritt n / m“ und unten ein Balken.
pub fn progress(img: &mut RgbaImage, done: u16, steps: u16) {
    let steps = steps.max(1);
    let step = (done + 1).min(steps);
    badge(img, ACCENT, &freestream_core::tr!("Step {n} / {m}", n = step, m = steps));
    let (w, h) = (img.width() as f32, img.height() as f32);
    let (pad, bh) = (w * 0.14, (h * 0.055).max(3.0));
    let y = h * 0.88;
    fill_round_rect(img, (pad, y, w - pad, y + bh), bh / 2.0, [255, 255, 255, 45]);
    let frac = (done as f32 / steps as f32).clamp(0.0, 1.0);
    if frac > 0.0 {
        fill_round_rect(img, (pad, y, pad + (w - 2.0 * pad) * frac, y + bh), bh / 2.0, [ACCENT[0], ACCENT[1], ACCENT[2], 255]);
    }
}

/// „Fehler“ auf einem Bild (Taste oder Feld des Touchstreifens), wie `_alert`.
pub fn alert(img: &mut RgbaImage) {
    badge(img, [229, 72, 77], &freestream_core::i18n::pgettext("overlay", "Error"));
}

const ACCENT: [u8; 3] = [91, 124, 255];

/// Gefülltes Rechteck mit runden Ecken (x0, y0, x1, y1), Farbe mit Deckkraft.
pub fn fill_round_rect(img: &mut RgbaImage, (x0, y0, x1, y1): (f32, f32, f32, f32), r: f32, color: [u8; 4]) {
    let r = r.min((x1 - x0) / 2.0).min((y1 - y0) / 2.0).max(0.0);
    let a = color[3] as f32 / 255.0;
    for py in y0.floor().max(0.0) as u32..(y1.ceil().max(0.0) as u32).min(img.height()) {
        for px in x0.floor().max(0.0) as u32..(x1.ceil().max(0.0) as u32).min(img.width()) {
            let (cx, cy) = (px as f32 + 0.5, py as f32 + 0.5);
            if cx < x0 || cx > x1 || cy < y0 || cy > y1 {
                continue;
            }
            // Abstand zur Ecke: nur in den Eckquadraten zählt der Radius
            let dx = (x0 + r - cx).max(cx - (x1 - r)).max(0.0);
            let dy = (y0 + r - cy).max(cy - (y1 - r)).max(0.0);
            if dx * dx + dy * dy > r * r {
                continue;
            }
            blend(img.get_pixel_mut(px, py), [color[0], color[1], color[2]], a);
        }
    }
}

/// Text als Tastenbild (Seitenanzeige, Uhr, Kein-Profil-Bild).
pub fn text_key((w, h): (u32, u32), text: &str, sub: &str, bg: [u8; 3], fg: [u8; 3]) -> RgbaImage {
    let mut img = RgbaImage::from_pixel(w, h, Rgba([bg[0], bg[1], bg[2], 255]));
    let Some(f) = font(Style::Bold) else { return img };
    let mut size = h as f32 * if sub.is_empty() { 0.42 } else { 0.36 };
    let tw = text_width(&f, size, text);
    if tw > w as f32 * 0.9 {
        size *= w as f32 * 0.9 / tw;
    }
    let tw = text_width(&f, size, text);
    let y = if sub.is_empty() { (h as f32 - size) / 2.0 } else { h as f32 * 0.2 };
    draw_text(&mut img, &f, size, (w as f32 - tw) / 2.0, y, text, fg);
    if !sub.is_empty() {
        if let Some(fr) = font(Style::Regular) {
            let s = h as f32 * 0.16;
            let sw = text_width(&fr, s, sub);
            draw_text(&mut img, &fr, s, (w as f32 - sw) / 2.0, h as f32 * 0.66, sub, [140, 147, 163]);
        }
    }
    img
}

/// Abstand zwischen den Tasten im Verhältnis zur Tastengröße (wie am Gerät).
pub const BG_GAP: f32 = 0.22;

/// Hintergrundbild einer Seite in Kacheln je Taste zerlegen: {(Spalte, Zeile): Bild}. Das Bild füllt die
/// ganze Fläche (zugeschnitten, nicht verzerrt); die Lücken zwischen den Tasten zählen mit.
pub fn background_tiles(path: &Path, cols: u8, rows: u8, (w, h): (u32, u32), dim: u8) -> HashMap<(u8, u8), RgbaImage> {
    let mut out = HashMap::new();
    let Ok(src) = image::open(path) else { return out };
    let gap = (w as f32 * BG_GAP).round() as u32;
    let full_w = cols as u32 * w + (cols as u32).saturating_sub(1) * gap;
    let full_h = rows as u32 * h + (rows as u32).saturating_sub(1) * gap;
    let mut img = src.resize_to_fill(full_w, full_h, imageops::FilterType::Lanczos3).to_rgba8();
    if dim > 0 {
        let k = 100 - dim.min(90) as u32;
        for p in img.pixels_mut() {
            for i in 0..3 {
                p[i] = (p[i] as u32 * k / 100) as u8;
            }
        }
    }
    for r in 0..rows {
        for c in 0..cols {
            let tile = imageops::crop_imm(&img, c as u32 * (w + gap), r as u32 * (h + gap), w, h).to_image();
            out.insert((c, r), tile);
        }
    }
    out
}

// ------------------------------------------------------------------- Touchstreifen und Infoleiste
/// PxScale für eine Schriftgröße wie in Pillow (Geviert in Pixeln; ab_glyph rechnet mit der Zeilenhöhe).
fn em_scale(f: &FontArc, px: f32) -> f32 {
    let upem = f.units_per_em().unwrap_or(1000.0);
    px * f.height_unscaled() / upem
}

/// Wo Text ausgerichtet wird (wie die Anker von Pillow: „mm“ = Mitte, „rm“ = rechts, senkrecht Mitte).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Anchor {
    Middle,
    RightMiddle,
    /// „lm“: links, senkrecht Mitte
    LeftMiddle,
}

/// Breite eines Texts in einer Schriftgröße wie in Pillow (`textlength`, passend zu [`draw_text_at`]).
pub fn text_length(style: Style, size: f32, text: &str) -> f32 {
    match font(style) {
        Some(f) => text_width(&f, em_scale(&f, size), text),
        None => 0.0,
    }
}

/// Text an einem Anker zeichnen; `size` wie in Pillow, `stroke` = Umriss in Schwarz.
#[allow(clippy::too_many_arguments)]
pub fn draw_text_at(img: &mut RgbaImage, style: Style, size: f32, (x, y): (f32, f32), anchor: Anchor, text: &str, color: [u8; 3], stroke: i32) {
    if text.is_empty() {
        return;
    }
    let Some(f) = font(style) else { return };
    let scale = em_scale(&f, size);
    let sf = f.as_scaled(PxScale::from(scale));
    let tw = text_width(&f, scale, text);
    let left = match anchor {
        Anchor::Middle => x - tw / 2.0,
        Anchor::RightMiddle => x - tw,
        Anchor::LeftMiddle => x,
    };
    let top = y - (sf.ascent() - sf.descent()) / 2.0;
    for dx in -stroke..=stroke {
        for dy in -stroke..=stroke {
            if dx != 0 || dy != 0 {
                draw_text(img, &f, scale, left + dx as f32, top + dy as f32, text, [0, 0, 0]);
            }
        }
    }
    draw_text(img, &f, scale, left, top, text, color);
}

/// Bild mit Alpha auf ein anderes legen (wie Pillows alpha_composite).
fn composite(dst: &mut RgbaImage, src: &RgbaImage, x: i64, y: i64) {
    imageops::overlay(dst, src, x, y);
}

/// Ein Feld des Touchstreifens (wie `compose_dial_segment`): Titel oben, Symbol links, Wert rechts,
/// Balken unten. Durchsichtig, damit der Hintergrund des ganzen Streifens zu sehen ist.
pub fn compose_dial_segment(
    (w, h): (u32, u32),
    icon: Option<&RgbaImage>,
    title: &str,
    value: &str,
    indicator: Option<f32>,
    background: Option<&RgbaImage>,
) -> RgbaImage {
    let mut img = RgbaImage::new(w, h);
    if let Some(bg) = background {
        composite(&mut img, &fit(bg, w, h), 0, 0);
    }
    let wf = w as f32;
    draw_text_at(&mut img, Style::Bold, 16.0, (wf / 2.0, 14.0), Anchor::Middle, title, [255, 255, 255], 1);
    let ic = (h as f32 * 0.48) as u32;
    if let Some(icon) = icon {
        composite(&mut img, &fit(icon, ic, ic), 12, 26);
    }
    draw_text_at(&mut img, Style::Bold, 20.0, (wf - 14.0, 50.0), Anchor::RightMiddle, value, [255, 255, 255], 1);
    if let Some(ind) = indicator {
        let (x0, x1, y) = (14.0, wf - 14.0, h as f32 - 14.0);
        fill_round_rect(&mut img, (x0, y - 4.0, x1, y + 4.0), 4.0, [60, 60, 66, 255]);
        let frac = (ind / 100.0).clamp(0.0, 1.0);
        if frac > 0.0 {
            fill_round_rect(&mut img, (x0, y - 4.0, x0 + (x1 - x0) * frac, y + 4.0), 4.0, [10, 132, 255, 255]);
        }
    }
    img
}

/// Ganzer Touchstreifen (wie `compose_strip`): Schwarz, Hintergrund, Felder nebeneinander.
pub fn compose_strip((w, h): (u32, u32), segments: &[RgbaImage], background: Option<&RgbaImage>) -> RgbaImage {
    let mut img = RgbaImage::from_pixel(w, h, Rgba([0, 0, 0, 255]));
    if let Some(bg) = background {
        composite(&mut img, &fit(bg, w, h), 0, 0);
    }
    let mut x = 0i64;
    for seg in segments {
        composite(&mut img, seg, x, 0);
        x += seg.width() as i64;
    }
    img
}

/// Infoleiste des Neo (wie `compose_infobar`): große Zeile, darunter Seitenpunkte (n, aktuelle) oder
/// eine kleine Zeile.
pub fn compose_infobar((w, h): (u32, u32), big: &str, small: &str, dots: Option<(usize, usize)>) -> RgbaImage {
    let mut img = RgbaImage::from_pixel(w, h, Rgba([0, 0, 0, 255]));
    let (wf, hf) = (w as f32, h as f32);
    draw_text_at(&mut img, Style::Bold, (hf * 0.5).floor(), (wf / 2.0, hf * 0.42), Anchor::Middle, big, [255, 255, 255], 0);
    if let Some((n, cur)) = dots {
        let (r, gap) = (3.0, 12.0);
        let x0 = wf / 2.0 - (n.saturating_sub(1)) as f32 * gap / 2.0;
        let cy = hf - 10.0;
        for i in 0..n {
            let c = if i == cur { [255, 255, 255, 255] } else { [90, 90, 90, 255] };
            let cx = x0 + i as f32 * gap;
            fill_round_rect(&mut img, (cx - r, cy - r, cx + r + 1.0, cy + r + 1.0), r + 0.5, c);
        }
    } else if !small.is_empty() {
        draw_text_at(&mut img, Style::Regular, (hf * 0.2).floor(), (wf / 2.0, hf * 0.85), Anchor::Middle, small, [170, 170, 180], 0);
    }
    img
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn composes_key_with_title() {
        let mut st = Map::new();
        st.insert("FontSize".into(), Value::from(14));
        let img = compose_key((72, 72), None, "Hi", &st, None, None);
        assert_eq!(img.dimensions(), (72, 72));
        // Titel unten: dort gibt es helle Pixel, oben nicht
        let bright = |y0: u32, y1: u32| (y0..y1).any(|y| (0..72).any(|x| img.get_pixel(x, y)[0] > 200));
        if font(Style::Regular).is_some() {
            assert!(bright(40, 72));
            assert!(!bright(0, 20));
        }
    }

    #[test]
    fn strip_and_segments() {
        let seg = compose_dial_segment((200, 100), None, "", "", Some(50.0), None);
        // Balken: links blau, rechts grau, darüber durchsichtig
        assert_eq!(seg.get_pixel(30, 86).0, [10, 132, 255, 255]);
        assert_eq!(seg.get_pixel(170, 86).0, [60, 60, 66, 255]);
        assert_eq!(seg.get_pixel(100, 60)[3], 0);
        let strip = compose_strip((800, 100), &[RgbaImage::new(200, 100), seg], None);
        assert_eq!(strip.dimensions(), (800, 100));
        assert_eq!(strip.get_pixel(230, 86).0, [10, 132, 255, 255]);
        assert_eq!(strip.get_pixel(30, 86).0, [0, 0, 0, 255]);
    }

    #[test]
    fn infobar_dots() {
        let img = compose_infobar((248, 58), "", "", Some((3, 1)));
        assert_eq!(img.get_pixel(112, 48).0, [90, 90, 90, 255]);
        assert_eq!(img.get_pixel(124, 48).0, [255, 255, 255, 255]);
        assert_eq!(img.get_pixel(136, 48).0, [90, 90, 90, 255]);
    }

    #[test]
    fn progress_overlay() {
        let img = compose_key((72, 72), None, "", &Map::new(), Some(Overlay::Progress { done: 1, steps: 2 }), None);
        // halber Balken: links Akzent, rechts nur die Spur
        let y = (72.0 * 0.88 + 1.0) as u32;
        assert_eq!(img.get_pixel(15, y).0[..3], ACCENT);
        assert_ne!(img.get_pixel(55, y).0[..3], ACCENT);
    }

    #[test]
    fn colors() {
        assert_eq!(parse_color(Some("#ff8000"), [0, 0, 0]), [255, 128, 0]);
        assert_eq!(parse_color(Some("#fff"), [0, 0, 0]), [255, 255, 255]);
        assert_eq!(parse_color(None, [1, 2, 3]), [1, 2, 3]);
    }
}
