//! Icon-Bibliothek: jedes Symbol wird gezeichnet, Suche, Vergleich mit der Python-Version.

use freestream_deck::iconlib::{self, STYLES};
use image::RgbaImage;
use std::path::Path;
use std::process::Command;

/// Anteil der Pixel in der Farbe `fg` (jeder Kanal höchstens 40 entfernt, deckend).
fn share_of(img: &RgbaImage, fg: [u8; 3]) -> f32 {
    let hits = img
        .pixels()
        .filter(|p| p[3] > 200 && (0..3).all(|i| (p[i] as i32 - fg[i] as i32).abs() <= 40))
        .count();
    hits as f32 / (img.width() * img.height()) as f32
}

#[test]
fn every_icon_renders_in_every_style() {
    let names = iconlib::all_names();
    assert!(names.len() > 100);
    for name in &names {
        for style in STYLES {
            let img = iconlib::render(name, style);
            assert_eq!(img.dimensions(), (144, 144), "{name}/{style}");
            let fg = if style == "black" { [17, 17, 20] } else { [255, 255, 255] };
            let share = share_of(&img, fg);
            assert!(share > 0.005, "{name}/{style}: kaum Symbol ({share})");
            // Fläche deckend (Ecke oben links)
            assert_eq!(img.get_pixel(0, 0)[3], 255);
        }
    }
}

#[test]
fn glyph_is_transparent_with_foreground() {
    for name in iconlib::all_names() {
        let g = iconlib::glyph(name, 64, [255, 0, 0, 255]);
        assert_eq!(g.dimensions(), (64, 64));
        assert!(share_of(&g, [255, 0, 0]) > 0.02, "{name}");
        assert!(g.pixels().any(|p| p[3] == 0), "{name}: keine transparenten Pixel");
    }
}

#[test]
fn names_topics_and_search() {
    let names = iconlib::all_names();
    let mut uniq = names.clone();
    uniq.sort();
    uniq.dedup();
    assert_eq!(uniq.len(), names.len(), "doppelte Namen");
    assert_eq!(iconlib::topic_of("mic"), "audio");
    assert_eq!(iconlib::topic_color("cpu"), "#64748b");
    assert_eq!(iconlib::topic_of("unbekannt"), "misc");

    let found = iconlib::search("mic");
    assert!(found.contains(&"mic") && found.contains(&"mic_off"), "{found:?}");
    assert_eq!(iconlib::search("  ").len(), names.len());
    assert!(iconlib::search("MICRO").contains(&"mic"));
    // Treffer über das Thema: alle Navigationssymbole
    assert!(iconlib::search("navigation").contains(&"undo"));
    assert!(iconlib::search("zzzz-nichts").is_empty());
    assert_eq!(iconlib::search_in("off", &["mic", "mic_off", "eye_off"]), vec!["mic_off", "eye_off"]);
}

#[test]
fn rounded_tile_cuts_corners() {
    let img = iconlib::render_sized("play", "black", 64);
    let t = iconlib::rounded_tile(&img, 10.0, None);
    assert_eq!(t.get_pixel(0, 0)[3], 0);
    assert_eq!(t.get_pixel(32, 32)[3], 255);
    let o = iconlib::rounded_tile(&img, 10.0, Some([0xc0, 0xc0, 0xc0]));
    assert_eq!(o.get_pixel(0, 0)[3], 0);
    assert!(o.get_pixel(32, 0)[0] < 236, "Rand fehlt");
}

// ------------------------------------------------------------ Vergleich mit Python (optional)
const PY: &str = r#"
import sys, types
root, out = sys.argv[1], sys.argv[2]
sys.path.insert(0, root)
for m in ('tkinter', 'tkinter.font', 'tkinter.ttk', 'PIL.ImageTk'):  # nur zum Importieren von theme
    mod = types.ModuleType(m)
    mod.__getattr__ = lambda name: type(name, (), {})
    sys.modules[m] = mod
sys.modules['tkinter'].font = sys.modules['tkinter.font']
sys.modules['tkinter'].ttk = sys.modules['tkinter.ttk']
import PIL
PIL.ImageTk = sys.modules['PIL.ImageTk']
from freestream import iconlib
for name in iconlib.names():
    for style in iconlib.STYLES:
        iconlib.render(name, style).save(f"{out}/{name}.{style}.png")
"#;

#[test]
fn matches_python_rendering() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..");
    if !root.join("freestream/iconlib.py").exists() {
        eprintln!("übersprungen: freestream/iconlib.py fehlt");
        return;
    }
    let dir = tempfile::tempdir().unwrap();
    let run = Command::new("python3")
        .arg("-c")
        .arg(PY)
        .arg(&root)
        .arg(dir.path())
        .env("FREESTREAM_APPEARANCE", "dark")
        .env("HOME", dir.path())
        .env("XDG_CONFIG_HOME", dir.path())
        .output();
    match run {
        Ok(o) if o.status.success() => {}
        Ok(o) => {
            eprintln!("übersprungen: Python/Pillow nicht nutzbar: {}", String::from_utf8_lossy(&o.stderr));
            return;
        }
        Err(e) => {
            eprintln!("übersprungen: python3 fehlt ({e})");
            return;
        }
    }
    let mut worst = (0.0f32, String::new());
    let mut report = Vec::new();
    for name in iconlib::all_names() {
        for style in STYLES {
            let py = image::open(dir.path().join(format!("{name}.{style}.png"))).unwrap().to_rgba8();
            let rs = iconlib::render(name, style);
            let diff = py
                .pixels()
                .zip(rs.pixels())
                .filter(|(a, b)| (0..4).any(|i| (a[i] as i32 - b[i] as i32).abs() > 64))
                .count() as f32
                / (144.0 * 144.0);
            report.push((diff, format!("{name}/{style}")));
            if diff > worst.0 {
                worst = (diff, format!("{name}/{style}"));
            }
        }
    }
    if std::env::var_os("ICONLIB_REPORT").is_some() {
        report.sort_by(|a, b| b.0.total_cmp(&a.0));
        for (d, n) in report.iter().take(25) {
            eprintln!("{n}: {:.2} %", d * 100.0);
        }
    }
    assert!(worst.0 < 0.05, "zu verschieden: {} ({:.2} %)", worst.1, worst.0 * 100.0);
}
