//! Hilfen: Bilder für GTK, kleine Bausteine.

use gtk4 as gtk;
use gtk::prelude::*;
use image::RgbaImage;

/// RGBA-Bild als GTK-Textur.
pub fn texture(img: &RgbaImage) -> gtk::gdk::Texture {
    let bytes = gtk::glib::Bytes::from(img.as_raw());
    gtk::gdk::MemoryTexture::new(
        img.width() as i32,
        img.height() as i32,
        gtk::gdk::MemoryFormat::R8g8b8a8,
        &bytes,
        img.width() as usize * 4,
    )
    .upcast()
}

/// RGBA-Bild als Cairo-Fläche (zum Zeichnen auf der Bühne).
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
                // Cairo: BGRA, vormultipliziert
                data[o] = (p[2] as u32 * a / 255) as u8;
                data[o + 1] = (p[1] as u32 * a / 255) as u8;
                data[o + 2] = (p[0] as u32 * a / 255) as u8;
                data[o + 3] = p[3];
            }
        }
    }
    Some(s)
}

/// Abgerundetes Rechteck als Pfad.
pub fn rounded(cr: &gtk::cairo::Context, x: f64, y: f64, w: f64, h: f64, r: f64) {
    let r = r.min(w / 2.0).min(h / 2.0);
    cr.new_sub_path();
    cr.arc(x + w - r, y + r, r, -std::f64::consts::FRAC_PI_2, 0.0);
    cr.arc(x + w - r, y + h - r, r, 0.0, std::f64::consts::FRAC_PI_2);
    cr.arc(x + r, y + h - r, r, std::f64::consts::FRAC_PI_2, std::f64::consts::PI);
    cr.arc(x + r, y + r, r, std::f64::consts::PI, 1.5 * std::f64::consts::PI);
    cr.close_path();
}

/// Farbe „#rrggbb“ für Cairo.
pub fn rgb(hex: &str) -> (f64, f64, f64) {
    let c = freestream_deck::render::parse_color(Some(hex), [0, 0, 0]);
    (c[0] as f64 / 255.0, c[1] as f64 / 255.0, c[2] as f64 / 255.0)
}

/// Label mit CSS-Klassen.
pub fn label(text: &str, classes: &[&str]) -> gtk::Label {
    let l = gtk::Label::new(Some(text));
    l.set_xalign(0.0);
    for c in classes {
        l.add_css_class(c);
    }
    l
}

/// Knopf nur mit Symbol (Name aus dem Icon-Thema).
pub fn icon_button(icon: &str, tip: &str) -> gtk::Button {
    let b = gtk::Button::from_icon_name(icon);
    b.add_css_class("icon-btn");
    b.set_tooltip_text(Some(tip));
    b
}

/// Symbol aus dem Icon-Thema für ein Glyph der Aktionsliste (Namen wie in der Python-Version).
pub fn glyph_icon(glyph: &str) -> &'static str {
    match glyph {
        "keyboard" => "input-keyboard-symbolic",
        "navigate" | "arrow" | "goto" => "go-next-symbolic",
        "arrow_left" => "go-previous-symbolic",
        "up" => "go-up-symbolic",
        "grid" | "deck" => "view-grid-symbolic",
        "layers" => "view-paged-symbolic",
        "sfx" | "music" => "audio-x-generic-symbolic",
        "speaker" => "audio-speakers-symbolic",
        "headphones" => "audio-headphones-symbolic",
        "mic" => "audio-input-microphone-symbolic",
        "record" | "rec" => "media-record-symbolic",
        "play" => "media-playback-start-symbolic",
        "stop" => "media-playback-stop-symbolic",
        "chat" => "user-available-symbolic",
        "wave" | "fx" | "mixer" => "multimedia-volume-control-symbolic",
        "home" => "go-home-symbolic",
        "clock" | "auto" | "hourglass" | "stopwatch" => "alarm-symbolic",
        "gauge" | "cpu" | "memory" | "network" | "disk" | "thermometer" | "battery" => "utilities-system-monitor-symbolic",
        "text" => "format-text-bold-symbolic",
        "folder" => "folder-symbolic",
        "close" => "window-close-symbolic",
        "globe" => "web-browser-symbolic",
        "sun" => "display-brightness-symbolic",
        "moon" => "weather-clear-night-symbolic",
        "camera" => "camera-video-symbolic",
        "stream" => "network-transmit-symbolic",
        "image" => "image-x-generic-symbolic",
        "undo" => "edit-undo-symbolic",
        "gear" => "emblem-system-symbolic",
        "spark" => "starred-symbolic",
        _ => "application-x-addon-symbolic",
    }
}

/// CSS-Klasse mit Hintergrundfarbe (für die farbigen Kacheln der Aktionsliste).
pub fn color_class(hex: &str) -> String {
    use std::cell::RefCell;
    use std::collections::HashSet;
    thread_local!(static DONE: RefCell<HashSet<String>> = RefCell::new(HashSet::new()));
    let name = format!("bg-{}", hex.trim_start_matches('#'));
    DONE.with(|d| {
        if d.borrow_mut().insert(name.clone()) {
            let css = gtk::CssProvider::new();
            #[allow(deprecated)]
            css.load_from_data(&format!(".{name} {{ background-color: {hex}; color: white; border-radius: 6px; }}"));
            if let Some(display) = gtk::gdk::Display::default() {
                gtk::style_context_add_provider_for_display(&display, &css, gtk::STYLE_PROVIDER_PRIORITY_APPLICATION);
            }
        }
    });
    name
}

/// Farbige Kachel mit Symbol (Aktionsliste, Kopf der Eigenschaften).
pub fn glyph_tile(glyph: &str, color: &str, size: i32) -> gtk::Box {
    let b = gtk::Box::new(gtk::Orientation::Horizontal, 0);
    b.add_css_class(&color_class(color));
    b.set_size_request(size, size);
    b.set_valign(gtk::Align::Center);
    b.set_halign(gtk::Align::Start);
    b.set_hexpand(false);
    let img = gtk::Image::from_icon_name(glyph_icon(glyph));
    img.set_pixel_size(size * 6 / 10);
    img.set_size_request(size, size);
    b.append(&img);
    b
}
