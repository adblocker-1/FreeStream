//! Zeichenhelfer für selbst gezeichnete Tasten (Uhr, Countdown, Stoppuhr, Systemmonitor) wie die
//! Hilfsfunktionen in `freestream/livekeys.py`.

use ab_glyph::{Font, PxScale, ScaleFont};
use image::{Rgba, RgbaImage};

use crate::render::{self, Style};

pub type Rgb = [u8; 3];

pub const BG: Rgb = [14, 15, 19];
pub const FG: Rgb = [255, 255, 255];
pub const MUTED: Rgb = [150, 152, 166];
pub const TRACK: Rgb = [52, 54, 64];
pub const ALARM_BG: Rgb = [210, 36, 36];
pub const WARN: Rgb = [255, 159, 10];
pub const CRIT: Rgb = [255, 69, 58];

/// Akzentfarbe aus Settings.color.
pub fn accent(name: &str, default: &str) -> Rgb {
    fn get(n: &str) -> Option<Rgb> {
        Some(match n {
            "blue" => [10, 132, 255],
            "green" => [48, 209, 88],
            "orange" => [255, 159, 10],
            "red" => [255, 69, 58],
            "purple" => [191, 90, 242],
            "teal" => [64, 200, 224],
            "white" => [235, 235, 240],
            _ => return None,
        })
    }
    get(name).or_else(|| get(default)).unwrap_or([10, 132, 255])
}

pub fn canvas((w, h): (u32, u32), bg: Rgb) -> RgbaImage {
    RgbaImage::from_pixel(w.max(1), h.max(1), Rgba([bg[0], bg[1], bg[2], 255]))
}

pub fn mix(a: Rgb, b: Rgb, t: f32) -> Rgb {
    [0, 1, 2].map(|i| (a[i] as f32 + (b[i] as f32 - a[i] as f32) * t) as u8)
}

/// Farbe nach Füllstand (reverse: wenig = kritisch, z. B. Akku).
pub fn level(frac: f32, acc: Rgb, warn: f32, crit: f32, reverse: bool) -> Rgb {
    if reverse {
        if frac <= crit { CRIT } else if frac <= warn { WARN } else { acc }
    } else if frac >= crit {
        CRIT
    } else if frac >= warn {
        WARN
    } else {
        acc
    }
}

/// Text mittig um (cx, cy), höchstens `max_w` breit und `max_h` hoch.
#[allow(clippy::too_many_arguments)]
pub fn text(img: &mut RgbaImage, cx: f32, cy: f32, t: &str, max_w: f32, max_h: f32, color: Rgb, bold: bool) {
    if t.is_empty() {
        return;
    }
    let Some(f) = render::font(if bold { Style::Bold } else { Style::Regular }) else { return };
    let mut size = max_h.max(6.0);
    let tw = render::text_width(&f, size, t);
    if tw > max_w && max_w > 0.0 {
        size = (size * max_w / tw).max(6.0);
    }
    let tw = render::text_width(&f, size, t);
    let sf = f.as_scaled(PxScale::from(size));
    let height = sf.ascent() - sf.descent();
    render::draw_text(img, &f, size, cx - tw / 2.0, cy - height / 2.0, t, color);
}

fn rect(img: &mut RgbaImage, x0: f32, y0: f32, x1: f32, y1: f32, c: Rgb) {
    let (w, h) = img.dimensions();
    let (x0, x1) = (x0.round().max(0.0) as u32, (x1.round().max(0.0) as u32).min(w));
    let (y0, y1) = (y0.round().max(0.0) as u32, (y1.round().max(0.0) as u32).min(h));
    for y in y0..y1 {
        for x in x0..x1 {
            img.put_pixel(x, y, Rgba([c[0], c[1], c[2], 255]));
        }
    }
}

/// Fortschrittsbalken in Höhe `y` (Anteil der Tastenhöhe).
pub fn bar(img: &mut RgbaImage, frac: f32, color: Rgb, y: f32) {
    let (w, h) = (img.width() as f32, img.height() as f32);
    let (x0, x1) = (w * 0.14, w * 0.86);
    let t = (h * 0.055).max(2.0);
    let yc = h * y;
    rect(img, x0, yc - t / 2.0, x1, yc + t / 2.0, TRACK);
    let frac = frac.clamp(0.0, 1.0);
    if frac > 0.0 {
        rect(img, x0, yc - t / 2.0, (x0 + (x1 - x0) * frac).max(x0 + t), yc + t / 2.0, color);
    }
}

/// Kleiner Verlaufsgraph (Fläche + Linie) im Rechteck (x0, y0, x1, y1).
pub fn spark(img: &mut RgbaImage, (x0, y0, x1, y1): (f32, f32, f32, f32), values: &[Option<f64>], vmax: f64, color: Rgb) {
    let vals: Vec<f64> = values.iter().flatten().copied().collect();
    if vals.len() < 2 {
        return;
    }
    let top = vals.iter().copied().fold(vmax.max(1e-9), f64::max);
    let n = vals.len();
    let step = (x1 - x0) / (n - 1).max(1) as f32;
    let y_of = |v: f64| y1 - (y1 - y0) * (v / top).clamp(0.0, 1.0) as f32;
    let fill = mix(BG, color, 0.32);
    let line = ((y1 - y0) / 12.0).max(1.0);
    let (w, _) = img.dimensions();
    for x in x0.round() as u32..(x1.round() as u32).min(w) {
        // Wert an dieser Stelle (linear zwischen zwei Messungen)
        let pos = ((x as f32 - x0) / step).max(0.0);
        let i = (pos.floor() as usize).min(n - 2);
        let t = (pos - i as f32).min(1.0) as f64;
        let y = y_of(vals[i] + (vals[i + 1] - vals[i]) * t);
        rect(img, x as f32, y, x as f32 + 1.0, y1, fill);
        rect(img, x as f32, y - line / 2.0, x as f32 + 1.0, y + line / 2.0, color);
    }
}
