//! Seiten, Ordner und Seitenanzeige mit einem Deck im Speicher.

use freestream_core::profiles::ProfileStore;
use freestream_deck::device::{Event, MemoryDeck};
use freestream_deck::engine::Controller;
use serde_json::json;
use std::sync::{Arc, Mutex};

const P1: &str = "11111111-1111-1111-1111-111111111111";
const P2: &str = "22222222-2222-2222-2222-222222222222";
const F1: &str = "33333333-3333-3333-3333-333333333333";

fn write(dir: &std::path::Path, v: serde_json::Value) {
    std::fs::create_dir_all(dir).unwrap();
    std::fs::write(dir.join("manifest.json"), serde_json::to_string(&v).unwrap()).unwrap();
}

fn keypad(actions: serde_json::Value) -> serde_json::Value {
    json!({"Controllers": [{"Type": "Keypad", "Actions": actions}]})
}

#[test]
fn pages_folders_and_back() {
    let tmp = tempfile::tempdir().unwrap();
    std::env::set_var("XDG_STATE_HOME", tmp.path().join("state"));
    std::env::set_var("XDG_CONFIG_HOME", tmp.path().join("config"));
    let root = tmp.path().join("profiles/ABC.sdProfile");
    write(&root, json!({"Name": "Test", "Device": {"Model": "20GBA9901"}, "Pages": {"Pages": [P1, P2], "Current": P1}, "Version": "3.0"}));
    write(&root.join("Profiles").join(P1.to_uppercase()), keypad(json!({
        "0,0": {"UUID": "com.elgato.streamdeck.page.next", "States": [{"Title": "Next"}]},
        "1,0": {"UUID": "com.elgato.streamdeck.profile.openchild", "Settings": {"ProfileUUID": F1}},
        "2,0": {"UUID": "com.elgato.streamdeck.page.indicator"}})));
    write(&root.join("Profiles").join(P2.to_uppercase()), keypad(json!({
        "0,0": {"UUID": "com.elgato.streamdeck.page.previous"}})));
    write(&root.join("Profiles").join(F1.to_uppercase()), keypad(json!({
        "0,0": {"UUID": "com.elgato.streamdeck.profile.backtoparent"}})));
    let store = Arc::new(Mutex::new(ProfileStore::new(Some(&tmp.path().join("profiles")))));
    let dev = Arc::new(MemoryDeck::new("20GBA9901", "T1", (72, 72)));
    let mut c = Controller::new(dev.clone(), store);
    c.start();
    assert_eq!(c.profile.as_ref().unwrap().name, "Test");
    assert_eq!(c.page_index, 0);
    assert!(dev.key_image(2).is_some(), "Seitenanzeige gezeichnet");
    let press = |c: &mut Controller, k| {
        c.handle(Event::KeyDown(k));
        c.handle(Event::KeyUp(k));
    };
    press(&mut c, 0); // nächste Seite
    assert_eq!(c.page_index, 1);
    press(&mut c, 0); // vorige Seite
    assert_eq!(c.page_index, 0);
    press(&mut c, 1); // Ordner
    assert_eq!(c.folders.len(), 1);
    assert_eq!(c.current_page().unwrap().uuid, F1);
    press(&mut c, 0); // zurück
    assert!(c.folders.is_empty());
    // Seite gemerkt (state.json)
    c.goto_page(1);
    let mut c2 = Controller::new(dev, Arc::new(Mutex::new(ProfileStore::new(Some(&tmp.path().join("profiles"))))));
    c2.start();
    assert_eq!(c2.page_index, 1);
}
