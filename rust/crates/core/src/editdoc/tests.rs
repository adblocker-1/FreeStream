//! Tests für das Bearbeiten von Profilen (wie `tests/test_editor.py`).

use super::*;
use crate::profiles::{import_path, Profile, ProfileStore};
use std::io::Read;

const HOTKEY: &str = "com.elgato.streamdeck.system.hotkey";
const TEXT: &str = "com.elgato.streamdeck.system.text";
const MULTIMEDIA: &str = "com.elgato.streamdeck.system.multimedia";

struct Fixture {
    _tmp: tempfile::TempDir,
    base: PathBuf,
    store: PathBuf,
}

fn fixture() -> Fixture {
    let tmp = tempfile::tempdir().unwrap();
    let base = tmp.path().to_path_buf();
    let store = base.join("profiles");
    std::fs::create_dir_all(&store).unwrap();
    Fixture { _tmp: tmp, base, store }
}

fn new_doc(fx: &Fixture) -> ProfileDoc {
    ProfileDoc::open(&create_profile("Test", "20GBD9901", Some(&fx.store)).unwrap()).unwrap()
}

fn reload(store: &Path) -> Profile {
    ProfileStore::new(Some(store)).profiles.into_values().next().unwrap()
}

fn uuid_of(a: Option<&Action>) -> Option<&str> {
    a.and_then(|a| a.get("UUID")).and_then(Value::as_str)
}

fn title(a: &Action) -> &str {
    a["States"][0]["Title"].as_str().unwrap_or("")
}

fn png(path: &Path, w: u32, h: u32, rgb: [u8; 3]) {
    image::RgbImage::from_pixel(w, h, image::Rgb(rgb)).save(path).unwrap();
}

#[test]
fn slots_map_to_controllers() {
    assert_eq!(slot_ref(Slot::Key(3, 1)), ("Keypad", "3,1".to_string()));
    assert_eq!(slot_ref(Slot::Dial(2)), ("Encoder", "2,0".to_string()));
    assert_eq!(slot_ref(Slot::InfoBar(1)), ("Neo", "1,0".to_string()));
    assert_eq!(Slot::from_ref("Encoder", "2,0"), Some(Slot::Dial(2)));
    assert_eq!(Slot::from_ref("{Invalid}", "1,0"), Some(Slot::InfoBar(1)));
    assert_eq!(Slot::from_ref("Keypad", "x"), None);
    assert_eq!(Slot::Dial(0).controller(), C::DIAL);
}

#[test]
fn create_and_edit_profile() {
    let fx = fixture();
    let mut doc = new_doc(&fx);
    let p0 = doc.page_ids()[0].clone();
    // neue Profile: Seitenordner im Format v2 (Base32 + „Z“) wie in Python
    assert!(!doc.v3_names);
    assert_eq!(doc.page(&p0).unwrap().dir.file_name().unwrap().to_str().unwrap(), encode_page_dir(&p0).unwrap());
    assert!(!doc.dirty());
    doc.place(&p0, Slot::Key(0, 0), HOTKEY).unwrap();
    assert!(doc.dirty());
    doc.set_hotkey(&p0, Slot::Key(0, 0), &[], 0, C::parse_hotkey("Strg+C").unwrap());
    doc.update_state(&p0, Slot::Key(0, 0), &[], 0, "Title", json!("Kopieren"));
    doc.place(&p0, Slot::Dial(1), MULTIMEDIA).unwrap();
    assert!(doc.save().unwrap());
    assert!(!doc.dirty());
    let prof = reload(&fx.store);
    assert_eq!(prof.name, "Test");
    assert_eq!(prof.geometry().dials, 4);
    let pg = prof.page(&prof.page_ids[0]).unwrap();
    assert_eq!(title(&pg.keypad[&(0, 0)]), "Kopieren");
    let hk = &pg.keypad[&(0, 0)]["Settings"]["Hotkeys"][0];
    assert_eq!((hk["VKeyCode"].as_i64(), hk["KeyCtrl"].as_bool()), (Some(0x43), Some(true)));
    assert_eq!(pg.encoders[&1]["UUID"], MULTIMEDIA);
    assert!(!doc.save().unwrap()); // nichts mehr zu schreiben
    // Dateien wie in Python: kompaktes JSON
    let text = std::fs::read_to_string(doc.root.join("manifest.json")).unwrap();
    assert!(text.starts_with("{\"Device\":{\"Model\":\"20GBD9901\",\"UUID\":\"\"},\"Name\":\"Test\",\"Pages\":{\"Current\":"));
}

#[test]
fn folders_copy_move_and_cleanup() {
    let fx = fixture();
    let mut doc = new_doc(&fx);
    let p0 = doc.page_ids()[0].clone();
    let folder = doc.place(&p0, Slot::Key(1, 0), C::FOLDER_UUID).unwrap();
    let fid = folder["Settings"]["ProfileUUID"].as_str().unwrap().to_string();
    assert!(doc.is_folder(&fid) && !doc.is_folder(&p0));
    assert_eq!(uuid_of(doc.get(&fid, Slot::Key(0, 0))), Some(C::BACK_UUID));
    assert_eq!(doc.folder_title(&fid), title(&folder));
    doc.place(&fid, Slot::Key(2, 0), TEXT).unwrap();
    png(&fx.base.join("icon.png"), 300, 200, [200, 0, 0]);
    let rel = doc.set_icon(&fid, Slot::Key(2, 0), &[], 0, &fx.base.join("icon.png")).unwrap().unwrap();
    assert!(rel.starts_with("Images/") && rel.ends_with(".png") && rel.len() == "Images/".len() + 32 + 4);
    // Ordner kopieren -> eigenständige Kopie mit eigenem Unterordner und Bild
    let clip = doc.copy(&p0, Slot::Key(1, 0)).unwrap();
    doc.paste(&p0, Slot::Key(2, 0), &clip).unwrap();
    let fid2 = doc.get(&p0, Slot::Key(2, 0)).unwrap()["Settings"]["ProfileUUID"].as_str().unwrap().to_string();
    assert_ne!(fid2, fid);
    assert_ne!(doc.get(&p0, Slot::Key(2, 0)).unwrap()["ActionID"], doc.get(&p0, Slot::Key(1, 0)).unwrap()["ActionID"]);
    assert_eq!(uuid_of(doc.get(&fid2, Slot::Key(2, 0))), Some(TEXT));
    let img_rel = doc.get(&fid2, Slot::Key(2, 0)).unwrap()["States"][0]["Image"].as_str().unwrap().to_string();
    let img_path = doc.page(&fid2).unwrap().dir.join(&img_rel);
    assert!(img_path.is_file());
    let im = image::open(&img_path).unwrap();
    assert_eq!((im.width(), im.height()), (ICON_SIZE, ICON_SIZE));
    assert_eq!(im.to_rgba8().get_pixel(0, 0)[3], 0); // transparent aufgefüllt (300×200 -> 144×96)
    assert_eq!(im.to_rgba8().get_pixel(72, 72).0, [200, 0, 0, 255]);
    // auf neue Seite verschieben (tauschen)
    let p1 = doc.add_page(None);
    doc.move_action(&p0, Slot::Key(1, 0), &p1, Slot::Key(4, 2));
    assert!(doc.get(&p0, Slot::Key(1, 0)).is_none());
    assert_eq!(uuid_of(doc.get(&p1, Slot::Key(4, 2))), Some(C::FOLDER_UUID));
    // ersten Ordner löschen -> beim Aufräumen verschwindet nur er
    doc.remove(&p1, Slot::Key(4, 2));
    let old_dir = doc.page(&fid).unwrap().dir.clone();
    doc.cleanup().unwrap();
    assert!(!old_dir.exists() && doc.page(&fid2).unwrap().dir.exists());
    assert!(doc.page(&fid).is_none() && !doc.can_undo());
    let prof = reload(&fx.store);
    assert_eq!(prof.page_ids.len(), 2);
    assert_eq!(prof.page(&fid2).unwrap().keypad[&(2, 0)]["UUID"], TEXT);
}

#[test]
fn move_swaps_and_relocates_images() {
    let fx = fixture();
    let mut doc = new_doc(&fx);
    let p0 = doc.page_ids()[0].clone();
    let p1 = doc.add_page(None);
    doc.place(&p0, Slot::Key(0, 0), TEXT);
    doc.place(&p1, Slot::Key(1, 1), HOTKEY);
    png(&fx.base.join("a.png"), 10, 10, [1, 2, 3]);
    let rel = doc.set_icon(&p0, Slot::Key(0, 0), &[], 0, &fx.base.join("a.png")).unwrap().unwrap();
    // gleicher Name, anderer Inhalt auf der Zielseite -> neuer Name
    let clash = doc.page(&p1).unwrap().dir.join(&rel);
    std::fs::create_dir_all(clash.parent().unwrap()).unwrap();
    std::fs::write(&clash, b"anders").unwrap();
    doc.move_action(&p0, Slot::Key(0, 0), &p1, Slot::Key(1, 1));
    assert_eq!(uuid_of(doc.get(&p0, Slot::Key(0, 0))), Some(HOTKEY));
    let moved = doc.get(&p1, Slot::Key(1, 1)).unwrap();
    let new_rel = moved["States"][0]["Image"].as_str().unwrap();
    assert_ne!(new_rel, rel);
    assert!(doc.page(&p1).unwrap().dir.join(new_rel).is_file());
    // gleicher Platz: nichts passiert
    assert!(doc.can_undo());
    doc.move_action(&p1, Slot::Key(1, 1), &p1, Slot::Key(1, 1));
    assert_eq!(uuid_of(doc.get(&p1, Slot::Key(1, 1))), Some(TEXT));
    // Drehregler <-> Taste
    doc.move_action(&p0, Slot::Key(0, 0), &p0, Slot::Dial(2));
    assert_eq!(uuid_of(doc.get(&p0, Slot::Dial(2))), Some(HOTKEY));
}

#[test]
fn undo_redo() {
    let fx = fixture();
    let mut doc = new_doc(&fx);
    let p0 = doc.page_ids()[0].clone();
    let calls = std::rc::Rc::new(std::cell::Cell::new(0));
    let c2 = calls.clone();
    doc.on_change = Some(Box::new(move || c2.set(c2.get() + 1)));
    assert!(!doc.undo() && !doc.redo());
    doc.place(&p0, Slot::Key(0, 0), HOTKEY);
    for ch in "Hallo".chars() {
        let cur = title(doc.get(&p0, Slot::Key(0, 0)).unwrap()).to_string();
        doc.update_state(&p0, Slot::Key(0, 0), &[], 0, "Title", json!(format!("{cur}{ch}")));
    }
    assert_eq!(title(doc.get(&p0, Slot::Key(0, 0)).unwrap()), "Hallo");
    assert_eq!(calls.get(), 6);
    assert!(doc.undo()); // Tippen wird zu einem Schritt zusammengefasst
    assert_eq!(title(doc.get(&p0, Slot::Key(0, 0)).unwrap()), "");
    assert!(doc.undo());
    assert!(doc.get(&p0, Slot::Key(0, 0)).is_none());
    assert!(doc.redo() && doc.redo());
    assert_eq!(title(doc.get(&p0, Slot::Key(0, 0)).unwrap()), "Hallo");
    assert!(!doc.redo());
    // andere Schlüssel werden nicht zusammengefasst; eine neue Änderung löscht Wiederholen
    doc.update_settings(&p0, Slot::Key(0, 0), &[], "Coalesce", json!(false));
    doc.update_state(&p0, Slot::Key(0, 0), &[], 0, "Title", json!("X"));
    assert!(doc.undo());
    assert_eq!(title(doc.get(&p0, Slot::Key(0, 0)).unwrap()), "Hallo");
    assert_eq!(doc.get(&p0, Slot::Key(0, 0)).unwrap()["Settings"]["Coalesce"], false);
    assert!(doc.undo());
    assert_eq!(doc.get(&p0, Slot::Key(0, 0)).unwrap()["Settings"]["Coalesce"], true);
    doc.rename("Neu");
    assert!(!doc.can_redo());
    // Seitenname: Tippen zusammengefasst
    doc.rename_page(&p0, "A");
    doc.rename_page(&p0, "AB");
    assert!(doc.undo());
    assert_eq!(doc.page(&p0).unwrap().name(), "");
    // Grenze der Rückgängig-Schritte
    for i in 0..(UNDO_LIMIT + 10) {
        doc.update_settings(&p0, Slot::Key(0, 0), &[], &format!("k{i}"), json!(i));
    }
    assert_eq!(doc.undo_stack.len(), UNDO_LIMIT);
}

#[test]
fn pages() {
    let fx = fixture();
    let mut doc = new_doc(&fx);
    let p0 = doc.page_ids()[0].clone();
    let p1 = doc.add_page(None);
    let p2 = doc.add_page(Some(&p0));
    assert_eq!(doc.page_ids(), vec![p0.clone(), p2.clone(), p1.clone()]);
    doc.move_page(&p1, -2);
    assert_eq!(doc.page_ids(), vec![p1.clone(), p0.clone(), p2.clone()]);
    doc.move_page(&p2, 5);
    assert_eq!(doc.page_ids(), vec![p1.clone(), p0.clone(), p2.clone()]);
    doc.remove_page(&p1).unwrap();
    assert_eq!(doc.page_ids(), vec![p0.clone(), p2.clone()]);
    doc.remove_page(&p2).unwrap();
    assert!(matches!(doc.remove_page(&p0), Err(EditError::Msg(_))));
    doc.rename_page(&p0, "Start");
    assert_eq!(doc.folder_title(&p0), "Start");
    // Default/Current zeigen nach dem Löschen auf eine vorhandene Seite
    let pages = doc.manifest["Pages"].clone();
    assert_eq!(pages["Default"], json!(p0));
    assert_eq!(pages["Current"], json!(p0));
    doc.save().unwrap();
    let prof = reload(&fx.store);
    assert_eq!(prof.page_ids, vec![p0.clone()]);
    assert_eq!(prof.page(&p0).unwrap().name, "Start");
}

#[test]
fn child_actions() {
    let fx = fixture();
    let mut doc = new_doc(&fx);
    let p0 = doc.page_ids()[0].clone();
    doc.place(&p0, Slot::Key(0, 0), "com.elgato.streamdeck.multiactions.routine");
    doc.checkpoint(None);
    {
        let m = doc.action_mut(&p0, Slot::Key(0, 0), &[]).unwrap();
        let groups = C::child_lists(m).unwrap();
        let group = groups[0]["Actions"].as_array_mut().unwrap();
        group.push(Value::Object(C::new_action(HOTKEY)));
        group.push(Value::Object(C::new_action("com.elgato.streamdeck.multiactions.delay")));
    }
    doc.changed();
    doc.update_settings(&p0, Slot::Key(0, 0), &[0, 1], "delay", json!(750));
    assert_eq!(doc.action(&p0, Slot::Key(0, 0), &[0, 1]).unwrap()["Settings"]["delay"], 750);
    doc.update_state(&p0, Slot::Key(0, 0), &[0, 0], 1, "Title", json!("Zwei"));
    assert_eq!(doc.action(&p0, Slot::Key(0, 0), &[0, 0]).unwrap()["States"].as_array().unwrap().len(), 2);
    assert!(doc.action(&p0, Slot::Key(0, 0), &[0, 5]).is_none());
    doc.place(&p0, Slot::Key(1, 0), "com.elgato.streamdeck.keys.logic");
    doc.checkpoint(None);
    {
        let logic = doc.action_mut(&p0, Slot::Key(1, 0), &[]).unwrap();
        C::child_lists(logic).unwrap()[2] = Value::Object(C::new_action(TEXT));
    }
    doc.set_hotkey(&p0, Slot::Key(0, 0), &[0, 0], 1, C::parse_hotkey("Alt+F4").unwrap());
    doc.save().unwrap();
    let prof = reload(&fx.store);
    let pg = prof.page(&p0).unwrap();
    let steps = pg.keypad[&(0, 0)]["Actions"][0]["Actions"].as_array().unwrap();
    let uuids: Vec<&str> = steps.iter().map(|a| a["UUID"].as_str().unwrap()).collect();
    assert_eq!(uuids, [HOTKEY, "com.elgato.streamdeck.multiactions.delay"]);
    assert_eq!(steps[0]["Settings"]["Hotkeys"][1]["VKeyCode"], 0x73);
    let logic = pg.keypad[&(1, 0)]["Actions"].as_array().unwrap();
    assert_eq!(logic[0], json!({}));
    assert_eq!(logic[2]["UUID"], TEXT);
}

#[test]
fn settings_json_and_unknown_slots() {
    let fx = fixture();
    let mut doc = new_doc(&fx);
    let p0 = doc.page_ids()[0].clone();
    doc.place(&p0, Slot::Key(0, 0), TEXT);
    doc.set_settings_json(&p0, Slot::Key(0, 0), &[], json!({"pastedText": "hi", "x": [1]}).as_object().unwrap().clone());
    assert_eq!(doc.get(&p0, Slot::Key(0, 0)).unwrap()["Settings"], json!({"pastedText": "hi", "x": [1]}));
    // leere Plätze und unbekannte Seiten werden ignoriert
    let n = doc.undo_stack.len();
    doc.update_settings(&p0, Slot::Key(4, 2), &[], "a", json!(1));
    doc.remove(&p0, Slot::Key(4, 2));
    assert!(doc.place("gibts-nicht", Slot::Key(0, 0), TEXT).is_none());
    assert_eq!(doc.undo_stack.len(), n);
    // Infoleiste (Neo)
    doc.place(&p0, Slot::InfoBar(1), "com.elgato.streamdeck.system.digitaltime");
    assert_eq!(uuid_of(doc.get(&p0, Slot::InfoBar(1))), Some("com.elgato.streamdeck.system.digitaltime"));
    let rt = doc.runtime_page(&p0).unwrap();
    assert!(rt.infobar.contains_key(&1) && rt.keypad.contains_key(&(0, 0)));
    let slots: Vec<Slot> = doc.page(&p0).unwrap().items().into_iter().map(|(s, _)| s).collect();
    assert_eq!(slots, vec![Slot::Key(0, 0), Slot::InfoBar(1)]);
}

#[test]
fn preserves_unknown_fields_and_v3_names() {
    let fx = fixture();
    let (pid, page) = (new_id().to_uppercase(), new_id());
    let root = fx.store.join(format!("{pid}.sdProfile"));
    std::fs::create_dir_all(root.join("Profiles").join(page.to_uppercase())).unwrap();
    let plugin_action = json!({"UUID": "com.obsproject.obsstudio.scene", "Name": "Szene", "Settings": {"scene": "Live"},
                               "States": [{"Title": "Live"}], "Custom": {"x": 1}});
    std::fs::write(
        root.join("Profiles").join(page.to_uppercase()).join("manifest.json"),
        json!({"Controllers": [{"Type": "Keypad", "Actions": {"0,0": plugin_action}}], "Extra": 42}).to_string(),
    )
    .unwrap();
    std::fs::write(
        root.join("manifest.json"),
        json!({"Device": {"Model": "20GBA9901", "UUID": ""}, "Name": "Fremd",
               "Pages": {"Pages": [page], "Default": page, "Current": page}, "Version": "3.0", "Foo": "bar"})
        .to_string(),
    )
    .unwrap();
    let mut doc = ProfileDoc::open(&root).unwrap();
    assert!(doc.v3_names);
    doc.place(&page, Slot::Key(1, 0), HOTKEY);
    let p1 = doc.add_page(None);
    doc.save().unwrap();
    assert!(root.join("Profiles").join(p1.to_uppercase()).join("manifest.json").exists());
    let m = read_json(&root.join("Profiles").join(page.to_uppercase()).join("manifest.json")).unwrap();
    assert_eq!(m["Extra"], 42);
    assert_eq!(m["Controllers"][0]["Actions"]["0,0"], plugin_action);
    assert_eq!(read_json(&root.join("manifest.json")).unwrap()["Foo"], "bar");
    // Geometrie aus dem Modell (MK.2 ohne Regler: neue Seiten ohne Encoder)
    assert_eq!(doc.geometry().dials, 0);
    assert_eq!(doc.page(&p1).unwrap().manifest["Controllers"].as_array().unwrap().len(), 1);
}

#[test]
fn legacy_page_format_and_errors() {
    let fx = fixture();
    assert!(matches!(ProfileDoc::open(&fx.base.join("nichts")), Err(EditError::Msg(_))));
    // v1: Tasten direkt im Hauptmanifest
    let v1 = fx.store.join("V1.sdProfile");
    std::fs::create_dir_all(&v1).unwrap();
    std::fs::write(v1.join("manifest.json"), r#"{"Actions": {"0,0": {"UUID": "x"}}, "Name": "Alt"}"#).unwrap();
    assert!(ProfileDoc::open(&v1).is_err());
    // Seite ohne „Controllers“: wird beim Speichern umgewandelt
    let root = fx.store.join("OLD.sdProfile");
    let page = new_id();
    let pdir = root.join("Profiles").join(encode_page_dir(&page).unwrap());
    std::fs::create_dir_all(&pdir).unwrap();
    std::fs::write(pdir.join("manifest.json"), json!({"Actions": {"1,1": {"UUID": TEXT, "States": [{}]}}}).to_string()).unwrap();
    std::fs::write(root.join("manifest.json"), json!({"Name": "", "Pages": {"Pages": [page]}}).to_string()).unwrap();
    let mut doc = ProfileDoc::open(&root).unwrap();
    assert_eq!(doc.name(), "OLD");
    assert_eq!(doc.geometry(), Geometry { cols: 2, rows: 2, dials: 0, touch_keys: 0 });
    assert_eq!(uuid_of(doc.get(&page, Slot::Key(1, 1))), Some(TEXT));
    assert!(doc.dirty());
    doc.save().unwrap();
    let m = read_json(&pdir.join("manifest.json")).unwrap();
    assert!(m.get("Actions").is_none());
    assert_eq!(m["Controllers"][0]["Actions"]["1,1"]["UUID"], TEXT);
}

#[test]
fn export_zip_and_import_roundtrip() {
    let fx = fixture();
    let mut doc = new_doc(&fx);
    let p0 = doc.page_ids()[0].clone();
    doc.place(&p0, Slot::Key(0, 0), C::FOLDER_UUID);
    doc.place(&p0, Slot::Key(1, 0), TEXT);
    png(&fx.base.join("i.png"), 50, 50, [0, 0, 255]);
    doc.set_icon(&p0, Slot::Key(1, 0), &[], 0, &fx.base.join("i.png")).unwrap();
    // nicht erreichbare Seite wird nicht exportiert
    let gone = doc.add_page(None);
    doc.remove_page(&gone).unwrap();
    let out = doc.export(&fx.base.join("Mein Profil")).unwrap();
    assert_eq!(out.file_name().unwrap().to_str().unwrap(), "Mein Profil.streamDeckProfile");
    let mut z = zip::ZipArchive::new(std::fs::File::open(&out).unwrap()).unwrap();
    let names: Vec<String> = z.file_names().map(|n| n.unwrap().to_string()).collect();
    let root_name = doc.root.file_name().unwrap().to_str().unwrap().to_string();
    assert!(names.iter().all(|n| n.starts_with(&format!("{root_name}/"))), "{names:?}");
    assert!(names.contains(&format!("{root_name}/manifest.json")));
    let gone_dir = doc.page(&gone).unwrap().dir.file_name().unwrap().to_str().unwrap().to_string();
    assert!(!names.iter().any(|n| n.contains(&gone_dir)));
    assert_eq!(names.iter().filter(|n| n.ends_with("/manifest.json")).count(), 3); // Profil, Seite, Ordner
    assert!(names.iter().any(|n| n.contains("/Images/") && n.ends_with(".png")));
    let mut f = z.by_name(&format!("{root_name}/manifest.json")).unwrap();
    assert_eq!(f.compression(), zip::CompressionMethod::Deflated);
    let mut text = String::new();
    f.read_to_string(&mut text).unwrap();
    assert!(text.contains("\"Name\":\"Test\""));
    drop(f);
    let other = fx.base.join("other");
    let ids = import_path(&out, Some(&other), Some(&fx.base.join("res")), true).unwrap();
    assert_eq!(ids, vec![doc.id.clone()]);
    let prof = reload(&other);
    let pg = prof.page(&prof.page_ids[0]).unwrap();
    assert_eq!(pg.keypad[&(1, 0)]["UUID"], TEXT);
    let img = pg.keypad[&(1, 0)]["States"][0]["Image"].as_str().unwrap();
    assert!(pg.image_path(img).is_some());
    // Endung bleibt, wenn sie schon stimmt
    let out2 = doc.export(&fx.base.join("x.streamDeckProfile")).unwrap();
    assert_eq!(out2, fx.base.join("x.streamDeckProfile"));
}

#[test]
fn duplicate() {
    let fx = fixture();
    let mut doc = new_doc(&fx);
    doc.place(&doc.page_ids()[0].clone(), Slot::Key(0, 0), TEXT);
    doc.save().unwrap();
    std::fs::write(doc.root.join("junk.tmp"), "x").unwrap();
    let dst = duplicate_profile(&doc.root, "Kopie").unwrap();
    assert!(dst.exists() && !dst.join("junk.tmp").exists());
    let mut names: Vec<String> = ProfileStore::new(Some(&fx.store)).profiles.values().map(|p| p.name.clone()).collect();
    names.sort();
    assert_eq!(names, ["Kopie", "Test"]);
    let m = read_json(&dst.join("manifest.json")).unwrap();
    assert_eq!(m["AppIdentifier"], "");
}

#[test]
fn backgrounds() {
    let fx = fixture();
    let mut doc = new_doc(&fx);
    let p0 = doc.page_ids()[0].clone();
    assert_eq!(doc.background(&p0), (None, 0));
    png(&fx.base.join("bg.png"), 2000, 1000, [10, 20, 30]);
    doc.set_background(&p0, Some(&fx.base.join("bg.png")), None, false).unwrap();
    let (p, dim) = doc.background(&p0);
    let p = p.unwrap();
    assert_eq!(dim, 30);
    let name = p.file_name().unwrap().to_str().unwrap();
    assert!(name.starts_with("Background") && name.ends_with(".png") && name.len() == "Background".len() + 12 + 4);
    let im = image::open(&p).unwrap();
    assert_eq!((im.width(), im.height()), (1600, 800));
    doc.set_background(&p0, None, Some(55), true).unwrap();
    assert_eq!(doc.background(&p0), (Some(p.clone()), 55));
    doc.save().unwrap();
    let rt = reload(&fx.store).page(&p0).unwrap();
    assert_eq!((rt.background_dim, rt.background.starts_with("Images/Background")), (55, true));
    doc.set_background(&p0, None, None, false).unwrap();
    assert!(!doc.page(&p0).unwrap().manifest.contains_key("FreeDeckBackground"));
    // Touchstreifen
    assert!(doc.strip_background(&p0).is_none());
    doc.set_strip_background(&p0, Some(&fx.base.join("bg.png"))).unwrap();
    let s = doc.strip_background(&p0).unwrap();
    assert!(s.file_name().unwrap().to_str().unwrap().starts_with("Strip"));
    let im = image::open(&s).unwrap();
    assert_eq!((im.width(), im.height()), (800, 400));
    doc.set_strip_background(&p0, None).unwrap();
    assert!(doc.strip_background(&p0).is_none());
    // unlesbares Bild
    std::fs::write(fx.base.join("kaputt.png"), b"kein Bild").unwrap();
    assert!(matches!(doc.set_background(&p0, Some(&fx.base.join("kaputt.png")), None, false), Err(EditError::Msg(_))));
    // Aufräumen: ungenutzte neue Bilder verschwinden
    doc.cleanup().unwrap();
    assert!(!p.exists() && !s.exists());
}

#[test]
fn import_image_variants() {
    let fx = fixture();
    let mut doc = new_doc(&fx);
    let p0 = doc.page_ids()[0].clone();
    std::fs::write(fx.base.join("a.SVG"), "<svg xmlns='http://www.w3.org/2000/svg'/>").unwrap();
    let rel = doc.import_image(&p0, &fx.base.join("a.SVG")).unwrap();
    assert!(rel.ends_with(".svg"));
    assert_eq!(std::fs::read_to_string(doc.page(&p0).unwrap().dir.join(&rel)).unwrap(), "<svg xmlns='http://www.w3.org/2000/svg'/>");
    png(&fx.base.join("small.png"), 20, 40, [9, 9, 9]);
    let rel = doc.import_image(&p0, &fx.base.join("small.png")).unwrap();
    let im = image::open(doc.page(&p0).unwrap().dir.join(&rel)).unwrap().to_rgba8();
    assert_eq!(im.dimensions(), (ICON_SIZE, ICON_SIZE)); // nicht vergrößert, mittig
    assert_eq!(im.get_pixel(72, 72).0, [9, 9, 9, 255]);
    assert_eq!(im.get_pixel(50, 72)[3], 0);
    std::fs::write(fx.base.join("x.jpg"), b"nein").unwrap();
    assert!(doc.import_image(&p0, &fx.base.join("x.jpg")).is_err());
}

#[test]
fn external_changes() {
    let fx = fixture();
    let mut doc = new_doc(&fx);
    let p0 = doc.page_ids()[0].clone();
    let p1 = doc.add_page(None);
    doc.save().unwrap();
    assert!(!doc.disk_changed());
    assert!(doc.reload_external().is_empty());
    // Plugin-Einstellungsseite ändert p0, der Editor hat p1 ungespeichert geändert
    let f0 = doc.page(&p0).unwrap().dir.join("manifest.json");
    let mut m = read_json(&f0).unwrap();
    m["Controllers"][0]["Actions"]["2,0"] = Value::Object(C::new_action(TEXT));
    std::fs::write(&f0, m.to_string()).unwrap();
    let f1 = doc.page(&p1).unwrap().dir.join("manifest.json");
    std::fs::write(&f1, json!({"Controllers": [], "Name": "fremd"}).to_string()).unwrap();
    doc.rename_page(&p1, "eigen");
    assert_eq!(doc.reload_external(), vec![p0.clone()]);
    assert_eq!(uuid_of(doc.get(&p0, Slot::Key(2, 0))), Some(TEXT));
    assert_eq!(doc.page(&p1).unwrap().name(), "eigen");
    // FreeWave legt eine Seite an
    let mut pm = read_json(&doc.root.join("manifest.json")).unwrap();
    pm["Name"] = json!("anders");
    std::fs::write(doc.root.join("manifest.json"), pm.to_string()).unwrap();
    assert!(doc.disk_changed());
}

#[test]
fn paste_between_profiles() {
    let fx = fixture();
    let mut a = new_doc(&fx);
    let mut b = ProfileDoc::open(&create_profile("B", "20GBA9901", Some(&fx.store)).unwrap()).unwrap();
    let pa = a.page_ids()[0].clone();
    let pb = b.page_ids()[0].clone();
    let folder = a.place(&pa, Slot::Key(0, 0), C::FOLDER_UUID).unwrap();
    let fid = folder["Settings"]["ProfileUUID"].as_str().unwrap().to_string();
    // verschachtelter Ordner im Ordner
    let inner = a.place(&fid, Slot::Key(1, 0), C::FOLDER_UUID).unwrap();
    let iid = inner["Settings"]["ProfileUUID"].as_str().unwrap().to_string();
    a.place(&iid, Slot::Key(2, 0), TEXT);
    png(&fx.base.join("i.png"), 8, 8, [5, 5, 5]);
    a.set_icon(&iid, Slot::Key(2, 0), &[], 0, &fx.base.join("i.png")).unwrap();
    a.rename_page(&fid, "Ordner");
    let clip = a.copy(&pa, Slot::Key(0, 0)).unwrap();
    assert_eq!(clip.folders.len(), 2);
    let pasted = b.paste(&pb, Slot::Key(3, 1), &clip).unwrap();
    let nf = pasted["Settings"]["ProfileUUID"].as_str().unwrap().to_string();
    assert!(b.page(&nf).is_some() && nf != fid);
    assert_eq!(b.folder_title(&nf), "Ordner");
    let ni = b.get(&nf, Slot::Key(1, 0)).unwrap()["Settings"]["ProfileUUID"].as_str().unwrap().to_string();
    assert_ne!(ni, iid);
    let t = b.get(&ni, Slot::Key(2, 0)).unwrap();
    let rel = t["States"][0]["Image"].as_str().unwrap();
    assert!(b.page(&ni).unwrap().dir.join(rel).is_file());
    assert!(b.page(&ni).unwrap().dir.starts_with(&b.root));
    // MK.2-Profil: Ordnerseiten sind v2-Namen
    assert!(b.page(&nf).unwrap().dir.file_name().unwrap().to_str().unwrap().ends_with('Z'));
    b.save().unwrap();
    assert_eq!(b.reachable().len(), 3);
    let prof = Profile::open(&b.root).unwrap();
    assert_eq!(prof.page(&ni).unwrap().keypad[&(2, 0)]["UUID"], TEXT);
    // Undo entfernt die eingefügte Taste wieder
    assert!(b.undo());
    assert!(b.get(&pb, Slot::Key(3, 1)).is_none());
}

#[test]
fn title_style_for_all_profiles() {
    let fx = fixture();
    let mut doc = new_doc(&fx);
    let p0 = doc.page_ids()[0].clone();
    doc.place(&p0, Slot::Key(0, 0), TEXT);
    doc.place(&p0, Slot::Key(1, 0), "com.elgato.streamdeck.multiactions.routine");
    doc.place(&p0, Slot::Dial(0), MULTIMEDIA);
    doc.save().unwrap();
    let key = json!({"FontSize": 20, "TitleColor": "#00ff00", "Unbekannt": 1});
    let dial = json!({"TitleColor": "#0000ff", "FontSize": 99});
    let n = apply_title_style(&fx.store, key.as_object().unwrap(), dial.as_object().unwrap());
    assert_eq!(n, 3);
    let pg = reload(&fx.store).page(&p0).unwrap();
    assert_eq!(pg.keypad[&(0, 0)]["States"][0]["FontSize"], 20);
    assert!(pg.keypad[&(0, 0)]["States"][0].get("Unbekannt").is_none());
    assert_eq!(pg.encoders[&0]["States"][0]["TitleColor"], "#0000ff");
    assert_eq!(pg.encoders[&0]["States"][0]["FontSize"], 12);
    assert_eq!(apply_title_style(&fx.store, key.as_object().unwrap(), dial.as_object().unwrap()), 0);
}

/// Mit Rust angelegtes Profil: liest `profiles::Profile` (Rust) und – falls vorhanden – Python.
#[test]
fn compatible_with_profile_reader_and_python() {
    let fx = fixture();
    let mut doc = new_doc(&fx);
    let p0 = doc.page_ids()[0].clone();
    let p1 = doc.add_page(None);
    doc.rename_page(&p1, "Zwei");
    doc.place(&p0, Slot::Key(0, 0), HOTKEY);
    doc.set_hotkey(&p0, Slot::Key(0, 0), &[], 0, C::parse_hotkey("ctrl+shift+t").unwrap());
    let folder = doc.place(&p0, Slot::Key(1, 0), C::FOLDER_UUID).unwrap();
    let fid = folder["Settings"]["ProfileUUID"].as_str().unwrap().to_string();
    doc.place(&fid, Slot::Key(2, 1), TEXT);
    doc.update_settings(&fid, Slot::Key(2, 1), &[], "pastedText", json!("Grüße"));
    doc.place(&p1, Slot::Dial(3), MULTIMEDIA);
    png(&fx.base.join("bg.png"), 100, 60, [1, 1, 1]);
    doc.set_background(&p1, Some(&fx.base.join("bg.png")), Some(40), false).unwrap();
    doc.cleanup().unwrap();

    let prof = Profile::open(&doc.root).unwrap();
    assert_eq!((prof.name.as_str(), prof.model.as_str(), prof.version.as_str()), ("Test", "20GBD9901", "2.0"));
    assert_eq!(prof.page_ids, vec![p0.clone(), p1.clone()]);
    assert_eq!(prof.default_page, p0);
    let pg0 = prof.page(&p0).unwrap();
    assert_eq!(pg0.keypad.keys().copied().collect::<Vec<_>>(), vec![(0, 0), (1, 0)]);
    assert_eq!(pg0.keypad[&(0, 0)]["Settings"]["Hotkeys"][0]["VKeyCode"], 0x54);
    let fpg = prof.page(&fid).unwrap();
    assert_eq!(fpg.keypad[&(0, 0)]["UUID"], C::BACK_UUID);
    assert_eq!(fpg.keypad[&(2, 1)]["Settings"]["pastedText"], "Grüße");
    let pg1 = prof.page(&p1).unwrap();
    assert_eq!(pg1.name, "Zwei");
    assert_eq!(pg1.encoders[&3]["UUID"], MULTIMEDIA);
    assert_eq!(pg1.background_dim, 40);
    assert!(pg1.image_path(&pg1.background).is_some());

    // Python (freestream.profiles und freestream.editdoc), falls python3.12 da ist
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..").canonicalize().unwrap();
    let script = r#"
import json, sys
sys.path.insert(0, sys.argv[1])
from freestream.profiles import Profile
from freestream import editdoc as E
p = Profile(sys.argv[2])
out = {"name": p.name, "pages": p.page_ids, "data": {}}
for u in p.page_ids + [sys.argv[3]]:
    pg = p.page(u)
    out["data"][u] = {"name": pg.name, "keys": sorted(f"{c},{r}:{a['UUID']}" for (c, r), a in pg.keypad.items()),
                      "dials": sorted(f"{i}:{a['UUID']}" for i, a in pg.encoders.items()), "dim": pg.background_dim}
doc = E.ProfileDoc(sys.argv[2])
out["dirty"] = doc.dirty
out["folder"] = doc.is_folder(sys.argv[3])
doc.place(p.page_ids[0], ("key", 3, 1), "com.elgato.streamdeck.system.text")
doc.save()
print(json.dumps(out))
"#;
    let res = std::process::Command::new("python3.12")
        .arg("-I")
        .arg("-c")
        .arg(script)
        .arg(&repo)
        .arg(&doc.root)
        .arg(&fid)
        .env("XDG_CONFIG_HOME", fx.base.join("xdg/config"))
        .env("XDG_DATA_HOME", fx.base.join("xdg/data"))
        .env("XDG_STATE_HOME", fx.base.join("xdg/state"))
        .env("FREESTREAM_LANG", "en")
        .output();
    let out = match res {
        Ok(o) => o,
        Err(e) => {
            eprintln!("python3.12 nicht verfügbar ({e}) – Python-Prüfung übersprungen");
            return;
        }
    };
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr);
        if err.contains("No module named 'PIL'") {
            eprintln!("Pillow fehlt – Python-Prüfung übersprungen");
            return;
        }
        panic!("Python: {err}");
    }
    let v: Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(v["name"], "Test");
    assert_eq!(v["pages"], json!([p0, p1]));
    assert_eq!(v["data"][&p0]["keys"], json!([format!("0,0:{HOTKEY}"), format!("1,0:{}", C::FOLDER_UUID)]));
    assert_eq!(v["data"][&p1]["dials"], json!([format!("3:{MULTIMEDIA}")]));
    assert_eq!(v["data"][&p1]["dim"], 40);
    assert_eq!(v["data"][&fid]["keys"], json!([format!("0,0:{}", C::BACK_UUID), format!("2,1:{TEXT}")]));
    assert_eq!(v["dirty"], false, "Python sieht ungespeicherte Änderungen");
    assert_eq!(v["folder"], true);
    // und zurück: von Python gespeichert, von Rust gelesen
    assert!(doc.disk_changed() || !doc.dirty());
    let doc2 = ProfileDoc::open(&doc.root).unwrap();
    assert_eq!(uuid_of(doc2.get(&p0, Slot::Key(3, 1))), Some(TEXT));
    assert_eq!(uuid_of(doc2.get(&fid, Slot::Key(2, 1))), Some(TEXT));
    assert!(!doc2.dirty());
}
