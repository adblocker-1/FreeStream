//! Farben, Stil und Bilder des FreeWave-Fensters – wie `freestream/theme.py` (PALETTES["dark"], icon_tile,
//! gradient_mark, CHANNEL_LOOK). Symbole kommen aus `freestream_deck::iconlib::glyph` (gleiche Formen wie
//! die Python-Version), werden je Größe/Farbe einmal gebaut und als Textur bzw. Cairo-Fläche gemerkt.

use std::cell::RefCell;
use std::collections::HashMap;

use gtk::prelude::*;
use gtk4 as gtk;
use image::{Rgba, RgbaImage};

// ------------------------------------------------------------------ Farben (PALETTES["dark"])
pub const SIDE: &str = "#1c1c1c";
pub const PANEL: &str = "#323232";
pub const PANEL2: &str = "#2c2c2c";
pub const RAISED: &str = "#454545";
pub const BORDER: &str = "#3a3a3a";
pub const FG: &str = "#f2f2f2";
pub const MUTED: &str = "#a8a8a8";
pub const FAINT: &str = "#707070";
pub const ACCENT: &str = "#3d9bff";
pub const ACCENT_SOFT: &str = "#22364d";
pub const MONITOR: &str = "#36c5e8";
pub const STREAM: &str = "#c084fc";
pub const DANGER: &str = "#f0484f";
pub const OK: &str = "#2bd665";
pub const WARN: &str = "#f5b13d";
/// Pegel: grün, ab 70 % gelb, ab 90 % rot (theme.METER_ZONES)
pub const METER_ZONES: [(f64, f64, &str); 3] = [(0.0, 0.7, "#2bd665"), (0.7, 0.9, "#f5c542"), (0.9, 1.0, "#f0484f")];

/// Mixer-Kanal -> (Symbol, Farbe) – theme.CHANNEL_LOOK.
pub fn channel_base(cid: &str, kind: &str) -> (&'static str, &'static str) {
    match cid {
        "system" => ("system", "#64748b"),
        "music" => ("music", "#1fa463"),
        "browser" => ("globe", "#ea580c"),
        "voice" => ("chat", "#5865f2"),
        "game" => ("game", "#f59e0b"),
        "sfx" => ("spark", "#ec4899"),
        "mic" => ("mic", "#e5484d"),
        _ if kind == "input" => ("input", "#64748b"),
        _ => ("wave", "#64748b"),
    }
}

/// (Symbol, Farbe) eines Kanals – eigene Angaben gehen vor (theme.channel_look).
pub fn channel_look(c: &freestream_wave::Channel) -> (String, String) {
    let (g, col) = channel_base(&c.id, &c.kind);
    (if c.icon.is_empty() { g.to_string() } else { c.icon.clone() },
     if c.color.is_empty() { col.to_string() } else { c.color.clone() })
}

pub fn rgb(hex: &str) -> [u8; 3] {
    let h = hex.trim_start_matches('#');
    let p = |i: usize| h.get(i..i + 2).and_then(|s| u8::from_str_radix(s, 16).ok()).unwrap_or(0);
    if h.len() >= 6 {
        [p(0), p(2), p(4)]
    } else {
        [0, 0, 0]
    }
}

/// Farbe für Cairo (0 … 1).
pub fn cairo_rgb(hex: &str) -> (f64, f64, f64) {
    let c = rgb(hex);
    (c[0] as f64 / 255.0, c[1] as f64 / 255.0, c[2] as f64 / 255.0)
}

/// Farbe zwischen a und b (0 … 1) – theme.mix.
pub fn mix(a: &str, b: &str, t: f64) -> String {
    let (ca, cb) = (rgb(a), rgb(b));
    let m = |i: usize| (ca[i] as f64 + (cb[i] as f64 - ca[i] as f64) * t).round() as u8;
    format!("#{:02x}{:02x}{:02x}", m(0), m(1), m(2))
}

pub fn set_source(cr: &gtk::cairo::Context, hex: &str) {
    let (r, g, b) = cairo_rgb(hex);
    cr.set_source_rgb(r, g, b);
}

/// Abgerundetes Rechteck als Pfad.
pub fn rounded(cr: &gtk::cairo::Context, x: f64, y: f64, w: f64, h: f64, r: f64) {
    let r = r.min(w / 2.0).min(h / 2.0).max(0.0);
    let (pi, h2) = (std::f64::consts::PI, std::f64::consts::FRAC_PI_2);
    cr.new_sub_path();
    cr.arc(x + w - r, y + r, r, -h2, 0.0);
    cr.arc(x + w - r, y + h - r, r, 0.0, h2);
    cr.arc(x + r, y + h - r, r, h2, pi);
    cr.arc(x + r, y + r, r, pi, 1.5 * pi);
    cr.close_path();
}

pub fn fill_rounded(cr: &gtk::cairo::Context, x: f64, y: f64, w: f64, h: f64, r: f64, hex: &str) {
    rounded(cr, x, y, w, h, r);
    set_source(cr, hex);
    let _ = cr.fill();
}

// ------------------------------------------------------------------ Bilder
/// Symbol als RGBA-Bild (theme.glyph); „sliders“ (Mixer/Effekte) und „link_off“ zeichnet FreeWave selbst.
pub fn glyph_image(name: &str, size: u32, color: &str) -> RgbaImage {
    let c = rgb(color);
    match name {
        "sliders" => sliders_image(size, c),
        "link_off" => freestream_deck::iconlib::glyph("link", size, [c[0], c[1], c[2], 255]),
        _ => freestream_deck::iconlib::glyph(name, size, [c[0], c[1], c[2], 255]),
    }
}

/// Symbol „Regler“ (drei Schieber) – waveui._sliders_image.
fn sliders_image(size: u32, c: [u8; 3]) -> RgbaImage {
    let k = 4u32;
    let s = (size * k) as f64;
    let mut im = RgbaImage::new(size * k, size * k);
    let col = Rgba([c[0], c[1], c[2], 255]);
    let lw = (s * 0.09).max(1.0);
    let mut rect = |x0: f64, y0: f64, x1: f64, y1: f64| {
        for y in y0.max(0.0) as u32..(y1.min(s) as u32) {
            for x in x0.max(0.0) as u32..(x1.min(s) as u32) {
                im.put_pixel(x, y, col);
            }
        }
    };
    for (x, y) in [(0.22, 0.62), (0.5, 0.34), (0.78, 0.56)] {
        let xx = s * x;
        rect(xx - lw / 2.0, s * 0.1, xx + lw / 2.0, s * 0.9);
        rect(xx - s * 0.12, s * y - s * 0.07, xx + s * 0.12, s * y + s * 0.07);
    }
    image::imageops::resize(&im, size, size, image::imageops::FilterType::Triangle)
}

/// Farbiges, abgerundetes Quadrat mit Symbol – theme.icon_tile.
pub fn tile_image(name: &str, size: u32, color: &str, radius: f64, fg: &str) -> RgbaImage {
    let k = 4u32;
    let s = size * k;
    let mut im = RgbaImage::new(s, s);
    let c = rgb(color);
    rounded_mask(&mut im, radius, |_, _| Rgba([c[0], c[1], c[2], 255]));
    let g = glyph_image(name, (s as f64 * 0.5) as u32, fg);
    image::imageops::overlay(&mut im, &g, (s as f64 * 0.25) as i64, (s as f64 * 0.25) as i64);
    image::imageops::resize(&im, size, size, image::imageops::FilterType::Triangle)
}

/// App-Zeichen: abgerundetes Quadrat mit Farbverlauf (Cyan → Violett) und weißer Welle – theme.gradient_mark.
pub fn gradient_mark(size: u32) -> RgbaImage {
    let k = 4u32;
    let s = size * k;
    let mut im = RgbaImage::new(s, s);
    let (a, b) = (rgb("#22c3ee"), rgb("#c084fc"));
    rounded_mask(&mut im, 0.3, |x, y| {
        let t = (x as f64 + y as f64) / (2.0 * s as f64);
        let m = |i: usize| (a[i] as f64 + (b[i] as f64 - a[i] as f64) * t).round() as u8;
        Rgba([m(0), m(1), m(2), 255])
    });
    let g = glyph_image("wave", (s as f64 * 0.56) as u32, "#ffffff");
    image::imageops::overlay(&mut im, &g, (s as f64 * 0.22) as i64, (s as f64 * 0.22) as i64);
    image::imageops::resize(&im, size, size, image::imageops::FilterType::Triangle)
}

/// Ganze Fläche als abgerundetes Quadrat füllen (radius: Anteil der Kante).
fn rounded_mask(im: &mut RgbaImage, radius: f64, color: impl Fn(u32, u32) -> Rgba<u8>) {
    let s = im.width() as f64;
    let r = s * radius;
    for y in 0..im.height() {
        for x in 0..im.width() {
            let (fx, fy) = (x as f64 + 0.5, y as f64 + 0.5);
            let cx = fx.clamp(r, s - r);
            let cy = fy.clamp(r, s - r);
            if (fx - cx).powi(2) + (fy - cy).powi(2) <= r * r {
                im.put_pixel(x, y, color(x, y));
            }
        }
    }
}

/// RGBA-Bild als GTK-Textur.
pub fn texture(img: &RgbaImage) -> gtk::gdk::Texture {
    let bytes = gtk::glib::Bytes::from(img.as_raw());
    gtk::gdk::MemoryTexture::new(img.width() as i32, img.height() as i32, gtk::gdk::MemoryFormat::R8g8b8a8, &bytes,
                                 img.width() as usize * 4).upcast()
}

/// RGBA-Bild als Cairo-Fläche.
pub fn surface(img: &RgbaImage) -> Option<gtk::cairo::ImageSurface> {
    let (w, h) = img.dimensions();
    let mut s = gtk::cairo::ImageSurface::create(gtk::cairo::Format::ARgb32, w as i32, h as i32).ok()?;
    let stride = s.stride() as usize;
    {
        let mut data = s.data().ok()?;
        for (y, row) in img.rows().enumerate() {
            for (x, p) in row.enumerate() {
                let a = p[3] as u32;
                let o = y * stride + x * 4;
                data[o] = (p[2] as u32 * a / 255) as u8;
                data[o + 1] = (p[1] as u32 * a / 255) as u8;
                data[o + 2] = (p[0] as u32 * a / 255) as u8;
                data[o + 3] = p[3];
            }
        }
    }
    Some(s)
}

thread_local! {
    static TEX: RefCell<HashMap<String, gtk::gdk::Texture>> = RefCell::new(HashMap::new());
    static SURF: RefCell<HashMap<String, gtk::cairo::ImageSurface>> = RefCell::new(HashMap::new());
}

fn cached_tex(key: String, make: impl FnOnce() -> RgbaImage) -> gtk::gdk::Texture {
    TEX.with(|c| {
        if let Some(t) = c.borrow().get(&key) {
            return t.clone();
        }
        let t = texture(&make());
        c.borrow_mut().insert(key, t.clone());
        t
    })
}

/// Symbol als Bild-Widget.
pub fn glyph(name: &str, size: i32, color: &str) -> gtk::Image {
    let t = cached_tex(format!("g|{name}|{size}|{color}"), || glyph_image(name, size as u32, color));
    let img = gtk::Image::from_paintable(Some(&t));
    img.set_pixel_size(size);
    img
}

/// Farbige Kachel mit Symbol als Bild-Widget (radius: Anteil der Kante, fg: Farbe des Symbols).
pub fn tile(name: &str, size: i32, color: &str, radius: f64, fg: &str) -> gtk::Image {
    let t = cached_tex(format!("t|{name}|{size}|{color}|{radius}|{fg}"), || tile_image(name, size as u32, color, radius, fg));
    let img = gtk::Image::from_paintable(Some(&t));
    img.set_pixel_size(size);
    img.set_valign(gtk::Align::Center);
    img
}

/// Kachel neu belegen (z. B. Vorschau im Dialogkopf).
pub fn set_tile(img: &gtk::Image, name: &str, size: i32, color: &str, radius: f64, fg: &str) {
    let t = cached_tex(format!("t|{name}|{size}|{color}|{radius}|{fg}"), || tile_image(name, size as u32, color, radius, fg));
    img.set_paintable(Some(&t));
}

pub fn mark(size: i32) -> gtk::Image {
    let t = cached_tex(format!("m|{size}"), || gradient_mark(size as u32));
    let img = gtk::Image::from_paintable(Some(&t));
    img.set_pixel_size(size);
    img
}

/// Symbol als Cairo-Fläche (zum Zeichnen in DrawingAreas).
pub fn glyph_surface(name: &str, size: i32, color: &str) -> Option<gtk::cairo::ImageSurface> {
    let key = format!("{name}|{size}|{color}");
    SURF.with(|c| {
        if let Some(s) = c.borrow().get(&key) {
            return Some(s.clone());
        }
        let s = surface(&glyph_image(name, size as u32, color))?;
        c.borrow_mut().insert(key, s.clone());
        Some(s)
    })
}

/// Symbol mittig bei (cx, cy) zeichnen.
pub fn paint_glyph(cr: &gtk::cairo::Context, name: &str, size: i32, color: &str, cx: f64, cy: f64) {
    if let Some(s) = glyph_surface(name, size, color) {
        let x = (cx - size as f64 / 2.0).round();
        let y = (cy - size as f64 / 2.0).round();
        if cr.set_source_surface(&s, x, y).is_ok() {
            let _ = cr.paint();
        }
    }
}

// ------------------------------------------------------------------ Stil
pub const CSS: &str = r#"
/* FreeWave: neutrales Grau wie Wave Link (Werte aus theme.PALETTES["dark"]) */
window, .main { background-color: #262626; color: #f2f2f2; }
.side { background-color: #1c1c1c; }
.brand { font-weight: bold; font-size: 11pt; }
.side-section { color: #a8a8a8; font-weight: bold; font-size: 9pt; }
.sep { background-color: #3a3a3a; min-height: 1px; min-width: 1px; }
.nav { background: none; border: none; box-shadow: none; border-radius: 6px; padding: 0 10px 0 1px; min-height: 32px; color: #a8a8a8; }
.nav:hover, .nav.active { background-color: #2e2e2e; color: #f2f2f2; }
.nav.accent, .nav.accent:hover { color: #3d9bff; }
.nav-bar { background-color: transparent; border-radius: 2px; min-width: 3px; min-height: 16px; }
.nav.active .nav-bar { background-color: #3d9bff; }
.nav-count { color: #707070; font-size: 9pt; }
.info { background: none; border: none; box-shadow: none; border-radius: 6px; padding: 4px 12px; min-height: 42px; color: #f2f2f2; }
.info:hover { background-color: #2e2e2e; }
.info-title { font-weight: bold; font-size: 10pt; }
.info-sub { color: #a8a8a8; font-size: 9pt; }
.info.dim .info-title { color: #a8a8a8; }
.info.dim .info-sub { color: #707070; }
.dot { border-radius: 999px; min-width: 7px; min-height: 7px; }
.dot-ok { background-color: #2bd665; }
.dot-danger { background-color: #f0484f; }
.title { font-size: 24pt; font-weight: bold; }
.sub, .muted { color: #a8a8a8; }
.faint { color: #707070; }
.small { font-size: 9pt; }
.tiny { font-size: 8pt; }
.bold { font-weight: bold; }
.danger { color: #f0484f; }
.ok { color: #2bd665; }
.warn { color: #f5b13d; }
.accent-text { color: #3d9bff; }
.status { color: #a8a8a8; font-size: 9pt; }
.btn { background-image: none; background-color: #2c2c2c; border: 1px solid #3a3a3a; color: #f2f2f2; border-radius: 9px; padding: 0 12px; min-height: 30px; box-shadow: none; }
.btn:hover { background-color: #454545; }
.btn.primary { background-color: #3d9bff; border-color: #3d9bff; color: #ffffff; font-weight: bold; }
.btn.primary:hover { background-color: #62afff; }
.btn.ghost { background: none; border-color: transparent; color: #a8a8a8; }
.btn.ghost:hover { background-color: #454545; }
.btn.danger-btn { color: #f0484f; }
.btn:disabled { color: #707070; }
.btn.tall { min-height: 34px; }
.cell { background-color: #323232; border-radius: 6px; padding: 0 10px; }
.cell.drop { box-shadow: inset 0 0 0 2px #3d9bff; }
.empty-cell { background-color: #2c2c2c; border-radius: 6px; }
.empty-cell:hover { background-color: #454545; }
.add-row { background-image: none; background-color: #2c2c2c; border: none; border-radius: 6px; box-shadow: none; color: #f2f2f2; }
.add-row:hover { background-color: #323232; }
.ch-name { font-weight: bold; font-size: 10pt; }
.ch-sub { color: #a8a8a8; font-size: 8pt; }
.val { color: #a8a8a8; font-size: 9pt; }
.db { color: #a8a8a8; font-size: 8pt; }
.db.clip { color: #f0484f; }
.card { background-color: #323232; border-radius: 14px; padding: 14px; }
.card.big { border-radius: 16px; padding: 20px; }
.card.r12 { border-radius: 12px; padding: 12px; }
.card.active { box-shadow: inset 0 0 0 1px #3d9bff; }
.card.drop { box-shadow: inset 0 0 0 2px #3d9bff; }
.section { color: #707070; font-size: 8pt; font-weight: bold; }
.chip { background-image: none; background-color: #323232; border: 1px solid #3a3a3a; color: #a8a8a8; border-radius: 13px; padding: 0 10px; min-height: 24px; box-shadow: none; font-size: 9pt; }
.chip.active { background-color: #22364d; border-color: #22364d; color: #3d9bff; }
.app-chip { background-image: none; background-color: #2c2c2c; border: 1px solid #3a3a3a; border-radius: 10px; padding: 3px 10px 3px 6px; box-shadow: none; color: #f2f2f2; }
.app-chip:hover { background-color: #454545; }
.seg { background-color: #2c2c2c; border: 1px solid #3a3a3a; border-radius: 9px; padding: 2px; }
.seg-btn { background: none; border: none; box-shadow: none; color: #a8a8a8; border-radius: 7px; min-height: 28px; font-weight: bold; font-size: 9pt; }
.seg-btn:checked { background-color: #454545; color: #f2f2f2; }
popover contents { background-color: #2c2c2c; border: 1px solid #3a3a3a; border-radius: 10px; padding: 6px; box-shadow: none; }
popover, popover.background { background: none; box-shadow: none; margin: 0; padding: 0; }
.menu-row:focus-visible, .btn:focus-visible, .nav:focus-visible, .info:focus-visible, .chip:focus-visible, .seg-btn:focus-visible { outline: none; }
.menu-row { background: none; border: none; box-shadow: none; border-radius: 6px; padding: 0 10px; min-height: 32px; color: #f2f2f2; }
.menu-row:hover { background-color: #454545; }
.menu-row.danger { color: #f0484f; }
.menu-row:disabled { color: #707070; }
entry, .entry { background-color: #2c2c2c; color: #f2f2f2; border: 1px solid #3a3a3a; border-radius: 6px; min-height: 34px; box-shadow: none; }
entry:focus-within { border-color: #3d9bff; }
dropdown > button { background-image: none; background-color: #2c2c2c; border: 1px solid #3a3a3a; color: #f2f2f2; box-shadow: none; }
.dialog-foot { border-top: 1px solid #3a3a3a; }
.h-title { font-size: 15pt; font-weight: bold; }
.big-title { font-size: 17pt; font-weight: bold; }
.mic-name { font-size: 13pt; font-weight: bold; }
.welcome { font-size: 20pt; font-weight: bold; }
.row-pick { background-image: none; background-color: #323232; border: 1px solid #3a3a3a; border-radius: 6px; box-shadow: none; padding: 10px 14px; color: #f2f2f2; }
.row-pick.selected { background-color: #22364d; border-color: #3d9bff; }
.check-row { background-image: none; background-color: #2c2c2c; border: 1px solid #3a3a3a; border-radius: 9px; box-shadow: none; padding: 6px 12px; color: #f2f2f2; }
.check-row.selected { border-color: #3d9bff; }
.list-row { background: none; border: none; box-shadow: none; border-radius: 6px; padding: 4px 6px; color: #f2f2f2; }
.list-row:hover { background-color: #2c2c2c; }
.step-dot { padding: 0; min-width: 22px; min-height: 22px; border-radius: 11px; background-color: #3d9bff; color: #ffffff; font-weight: bold; font-size: 9pt; }
.step-dot.todo { background-color: #323232; box-shadow: inset 0 0 0 1px #3a3a3a; color: #a8a8a8; font-weight: normal; }
.step-line { min-height: 1px; background-color: #3a3a3a; }
.step-line.done { min-height: 2px; background-color: #3d9bff; }
scrollbar slider { background-color: #454545; min-width: 6px; min-height: 6px; }
scrolledwindow, viewport { background: none; }
"#;

pub fn install_css() {
    let css = gtk::CssProvider::new();
    #[allow(deprecated)]
    css.load_from_data(CSS);
    if let Some(display) = gtk::gdk::Display::default() {
        gtk::style_context_add_provider_for_display(&display, &css, gtk::STYLE_PROVIDER_PRIORITY_APPLICATION);
    }
    if let Some(s) = gtk::Settings::default() {
        s.set_gtk_application_prefer_dark_theme(true);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn colors_and_looks() {
        assert_eq!(rgb("#3d9bff"), [0x3d, 0x9b, 0xff]);
        assert_eq!(mix("#000000", "#ffffff", 0.5), "#808080");
        let c = freestream_wave::Channel { id: "music".into(), kind: "apps".into(), ..Default::default() };
        assert_eq!(channel_look(&c), ("music".to_string(), "#1fa463".to_string()));
        let c = freestream_wave::Channel { id: "x".into(), kind: "input".into(), icon: "game".into(), ..Default::default() };
        assert_eq!(channel_look(&c), ("game".to_string(), "#64748b".to_string()));
        let t = tile_image("mic", 40, "#e5484d", 0.18, "#ffffff");
        assert_eq!(t.dimensions(), (40, 40));
        assert_eq!(t.get_pixel(0, 0)[3], 0); // Ecke rund
        assert_eq!(t.get_pixel(20, 2)[3], 255);
        assert_eq!(sliders_image(16, [255, 255, 255]).dimensions(), (16, 16));
    }
}
