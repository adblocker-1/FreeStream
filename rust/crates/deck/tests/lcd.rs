//! Touchstreifen (Stream Deck +), Infoleiste und Touch-Tasten (Neo) sowie der Fortschritt einer
//! Multi-Aktion mit einem Deck im Speicher.

use freestream_core::profiles::ProfileStore;
use freestream_deck::device::{model_lcd_size, Event, MemoryDeck};
use freestream_deck::engine::Controller;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

const P1: &str = "11111111-1111-1111-1111-111111111111";
const P2: &str = "22222222-2222-2222-2222-222222222222";

/// Ein gemeinsamer Ordner für Einstellungen und Zustand (Umgebungsvariablen gelten für alle Tests).
fn home() -> &'static Path {
    static HOME: OnceLock<tempfile::TempDir> = OnceLock::new();
    HOME.get_or_init(|| {
        let tmp = tempfile::tempdir().unwrap();
        std::env::set_var("XDG_STATE_HOME", tmp.path().join("state"));
        std::env::set_var("XDG_CONFIG_HOME", tmp.path().join("config"));
        tmp
    })
    .path()
}

fn write(dir: &Path, v: Value) {
    std::fs::create_dir_all(dir).unwrap();
    std::fs::write(dir.join("manifest.json"), serde_json::to_string(&v).unwrap()).unwrap();
}

/// Profil mit Seiten (je Seite die Controller) anlegen; gibt den Profilordner zurück.
fn profile(name: &str, model: &str, pages: &[(&str, Value)]) -> PathBuf {
    let root = home().join(name);
    let ids: Vec<&str> = pages.iter().map(|(id, _)| *id).collect();
    write(&root.join("X.sdProfile"), json!({"Name": name, "Device": {"Model": model}, "Pages": {"Pages": ids, "Current": ids[0]}, "Version": "3.0"}));
    for (id, controllers) in pages {
        write(&root.join("X.sdProfile/Profiles").join(id.to_uppercase()), json!({"Controllers": controllers}));
    }
    root
}

fn controller(root: &Path, dev: Arc<MemoryDeck>) -> Controller {
    let store = Arc::new(Mutex::new(ProfileStore::new(Some(root))));
    let mut c = Controller::new(dev, store);
    c.start();
    c
}

#[test]
fn plus_touch_strip() {
    let root = profile("plus", "20GBD9901", &[(P1, json!([
        {"Type": "Keypad", "Actions": {}},
        {"Type": "Encoder", "Actions": {
            "0,0": {"UUID": "com.elgato.streamdeck.system.keybrightness", "States": [{"Title": "Hell"}]},
            "3,0": {"UUID": "com.example.unknown.action"}}}]))]);
    let lcd = model_lcd_size("20GBD9901");
    assert_eq!(lcd, Some((800, 100)));
    let dev = Arc::new(MemoryDeck::with_lcd("20GBD9901", "PLUS1", (120, 120), lcd));
    let mut c = controller(&root, dev.clone());
    c.set_brightness(50);
    c.render_lcd();

    let strip = dev.lcd_image().expect("Touchstreifen gezeichnet");
    assert_eq!(strip.dimensions(), (800, 100));
    assert_eq!(c.lcd_image(), Some(&strip));
    // Feld 1 ohne Aktion: schwarz; Feld 0: Balken der Helligkeit (blau links, grau rechts)
    assert_eq!(strip.get_pixel(300, 50).0, [0, 0, 0, 255]);
    assert_eq!(strip.get_pixel(20, 86).0, [10, 132, 255, 255]);
    assert_eq!(strip.get_pixel(180, 86).0, [60, 60, 66, 255]);

    // Drehen ändert die Helligkeit und damit das Feld im Touchstreifen
    c.handle(Event::DialTurn(0, 4));
    assert_eq!(c.brightness, 70);
    let after = dev.lcd_image().unwrap();
    assert_ne!(after, strip);
    assert_eq!(after.get_pixel(130, 86).0, [10, 132, 255, 255]);
    assert_eq!(strip.get_pixel(130, 86).0, [60, 60, 66, 255]);

    // Aktion ohne Linux-Entsprechung meldet „Fehler“ auf ihrem Feld (abgedunkelt), die anderen bleiben
    c.handle(Event::DialDown(3));
    c.handle(Event::DialUp(3));
    let alert = dev.lcd_image().unwrap();
    assert_ne!(alert, after);
    let seg = |img: &image::RgbaImage, x0: u32| (x0..x0 + 200).flat_map(|x| (0..100).map(move |y| (x, y))).map(|(x, y)| *img.get_pixel(x, y)).collect::<Vec<_>>();
    assert_eq!(seg(&alert, 0), seg(&after, 0));
    assert_ne!(seg(&alert, 600), seg(&after, 600));
}

#[test]
fn neo_infobar_and_touch_keys() {
    let keypad = json!([{"Type": "Keypad", "Actions": {}}]);
    let root = profile("neo", "20GBJ9901", &[(P1, keypad.clone()), (P2, keypad)]);
    let lcd = model_lcd_size("20GBJ9901");
    let dev = Arc::new(MemoryDeck::with_lcd("20GBJ9901", "NEO1", (96, 96), lcd));
    let mut c = controller(&root, dev.clone());
    let bar = dev.lcd_image().expect("Infoleiste gezeichnet");
    assert_eq!(bar.dimensions(), (248, 58));
    // zwei Seitenpunkte: der erste (aktuelle) weiß, der zweite grau
    assert_eq!(bar.get_pixel(118, 48).0, [255, 255, 255, 255]);
    assert_eq!(bar.get_pixel(130, 48).0, [90, 90, 90, 255]);
    // weitere Seiten: beide Touch-Tasten leuchten
    assert_eq!(dev.touch_key_color(0), Some([255, 255, 255]));
    assert_eq!(dev.touch_key_color(1), Some([255, 255, 255]));

    // rechte Touch-Taste: nächste Seite, Punkte wandern mit
    c.handle(Event::TouchKeyDown(1));
    c.handle(Event::TouchKeyUp(1));
    assert_eq!(c.page_index, 1);
    let bar = dev.lcd_image().unwrap();
    assert_eq!(bar.get_pixel(118, 48).0, [90, 90, 90, 255]);
    assert_eq!(bar.get_pixel(130, 48).0, [255, 255, 255, 255]);
    c.handle(Event::TouchKeyDown(0));
    assert_eq!(c.page_index, 0);
}

#[test]
fn multi_action_progress_on_key() {
    let root = profile("multi", "20GBA9901", &[(P1, json!([{"Type": "Keypad", "Actions": {
        "0,0": {"UUID": "com.elgato.streamdeck.multiactions.routine", "States": [{"Title": "M"}], "Actions": [
            {"UUID": "com.elgato.streamdeck.multiactions.delay", "Settings": {"delay": 150}},
            {"UUID": "com.elgato.streamdeck.page.indicator"}]}}}]))]);
    let dev = Arc::new(MemoryDeck::new("20GBA9901", "MK2", (72, 72)));
    let mut c = controller(&root, dev.clone());
    let idle = dev.key_image(0).unwrap();
    c.handle(Event::KeyDown(0));
    c.handle(Event::KeyUp(0));
    // während der Ablauf läuft, steht der Fortschritt auf der Taste
    let t = Instant::now();
    while dev.key_image(0).unwrap() == idle && t.elapsed() < Duration::from_secs(2) {
        std::thread::sleep(Duration::from_millis(10));
        c.tick();
    }
    assert_ne!(dev.key_image(0).unwrap(), idle, "Fortschritt gezeigt");
    // danach wieder die normale Taste
    let t = Instant::now();
    while dev.key_image(0).unwrap() != idle && t.elapsed() < Duration::from_secs(2) {
        std::thread::sleep(Duration::from_millis(10));
        c.tick();
    }
    assert_eq!(dev.key_image(0).unwrap(), idle);
}
