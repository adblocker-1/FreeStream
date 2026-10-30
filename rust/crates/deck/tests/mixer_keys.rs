//! FreeWave-Tasten am Deck: Zuordnung der UUIDs, Tastenbilder und Lautstärke/Stumm gegen einen Mixer mit
//! simuliertem pactl (dasselbe wie in den Tests von crates/wave).

#[path = "../../wave/tests/common/mod.rs"]
mod common;

use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use freestream_deck::actions::{behavior_for, ActionData, Env};
use freestream_deck::mixkeys::{self, Look};
use freestream_deck::wavehub;
use image::RgbaImage;
use serde_json::{json, Map, Value};

/// Hub, Mix-Wahl und Arbeits-Thread gibt es einmal je Prozess: Tests nacheinander.
static SERIAL: Mutex<()> = Mutex::new(());

fn data(uuid: &str, settings: Value) -> ActionData {
    let v = json!({"UUID": uuid, "Settings": settings, "States": [{}]});
    ActionData::new(v.as_object().unwrap().clone(), PathBuf::new())
}

fn wait_until(what: &str, mut f: impl FnMut() -> bool) {
    let t = Instant::now();
    while !f() {
        assert!(t.elapsed() < Duration::from_secs(5), "{what}");
        std::thread::sleep(Duration::from_millis(10));
    }
}

fn near(p: image::Rgba<u8>, c: [u8; 3], tol: i32) -> bool {
    (0..3).all(|i| (p[i] as i32 - c[i] as i32).abs() <= tol)
}

fn any_near(img: &RgbaImage, c: [u8; 3], tol: i32) -> bool {
    img.pixels().any(|p| near(*p, c, tol))
}

#[test]
fn every_freewave_uuid_has_a_behavior() {
    let mut n = 0;
    for a in freestream_core::catalog::catalog() {
        if a.category.id() == freestream_core::catalog::FREEWAVE_CATEGORY {
            assert!(freestream_deck::extra::behavior_for(&a.uuid).is_some(), "{}", a.uuid);
            assert!(behavior_for(&a.uuid).supported(), "{}", a.uuid);
            n += 1;
        }
    }
    assert!(n >= 6, "FreeWave-Aktionen im Katalog: {n}");
    // Aktionen des Elgato-Plugins „Wave Link“ aus importierten Profilen
    for u in ["com.elgato.wavelink.mixermute", "com.elgato.wavelink.monitormixswitch", "com.elgato.wavelink.micmute",
              "com.elgato.wavelink.setlowcut", "com.elgato.wave-link.volume"] {
        assert!(freestream_deck::extra::behavior_for(u).is_some(), "{u}");
    }
    assert!(freestream_deck::actions_mixer::behavior_for("com.example.plugin.x").is_none());
}

#[test]
fn key_images_for_typical_states() {
    let size = (72, 72);
    let base = |vol: Option<i64>, muted: bool| Look::Tile {
        icon: "music".into(), color: [31, 164, 99], vol, muted, label: "All".into(), alpha: 255,
    };
    let half = mixkeys::draw(&base(Some(50), false), size);
    let muted = mixkeys::draw(&base(Some(50), true), size);
    let off = mixkeys::draw(&base(None, false), size);
    assert_eq!(half.dimensions(), size);
    assert_ne!(half, muted);
    assert_ne!(half, off);
    // stumm: grauer Kasten mit rotem Strich statt der Kanalfarbe
    assert!(any_near(&muted, mixkeys::RED, 30));
    assert!(any_near(&half, [31, 164, 99], 4));
    assert!(!any_near(&muted, [31, 164, 99], 4));
    // durchsichtiger Hintergrund (Deckkraft), Symbol bleibt deckend
    let see_through = mixkeys::draw(&Look::Tile {
        icon: "music".into(), color: [31, 164, 99], vol: Some(50), muted: false, label: "All".into(), alpha: 0,
    }, size);
    assert_eq!(see_through.get_pixel(1, 1)[3], 0);
    assert!(see_through.pixels().any(|p| p[3] == 255));

    // Fader bei 50 %: der Knopf liegt auf der oberen Taste (bei 0,87 Tastenhöhen)
    let fader = |top: bool, live: Option<u8>, muted: bool| Look::Fader {
        top, icon: "music".into(), color: [31, 164, 99], vol: Some(50), muted, label: "All".into(), alpha: 255, live,
    };
    let top = mixkeys::draw(&fader(true, None, false), size);
    assert!(near(*top.get_pixel(27, 63), [245, 245, 248], 20), "{:?}", top.get_pixel(27, 63));
    // Pegel: oben rot bei voller Aussteuerung, ohne Pegel dunkel
    let loud = mixkeys::draw(&fader(true, Some(mixkeys::FADER_SEGMENTS), false), size);
    let quiet = mixkeys::draw(&fader(true, Some(mixkeys::FADER_SPLIT), false), size);
    assert!(near(*loud.get_pixel(51, 15), mixkeys::RED, 40), "{:?}", loud.get_pixel(51, 15));
    assert!(!near(*quiet.get_pixel(51, 15), mixkeys::RED, 40));
    // stumm: roter Knopf
    let muted_top = mixkeys::draw(&fader(true, None, true), size);
    assert!(near(*muted_top.get_pixel(27, 63), mixkeys::RED, 30));
    let bottom = mixkeys::draw(&fader(false, None, false), size);
    assert_ne!(top, bottom);

    // Mix-Wahl: grüner Rahmen nur beim gewählten Mix
    let mix = |selected: bool| Look::Mix { mix: "stream".into(), selected, sub: String::new(), name: "Stream".into(),
                                          icon: "stream".into(), alpha: 255 };
    let sel = mixkeys::draw(&mix(true), size);
    let unsel = mixkeys::draw(&mix(false), size);
    assert!(near(*sel.get_pixel(36, 3), mixkeys::GREEN, 40), "{:?}", sel.get_pixel(36, 3));
    assert!(!near(*unsel.get_pixel(36, 3), mixkeys::GREEN, 40));
    let pages = mixkeys::draw(&Look::Pages { first: 1, last: 7, alpha: 255 }, size);
    assert_eq!(pages.dimensions(), size);
}

fn setup(config: Value) -> (tempfile::TempDir, common::FakePactl, std::sync::Arc<wavehub::Hub>) {
    freestream_wave::mixer::WATCH.store(false, std::sync::atomic::Ordering::SeqCst);
    let dir = tempfile::tempdir().unwrap();
    let (mx, fake) = common::mixer(dir.path(), config);
    mx.setup().unwrap();
    let hub = wavehub::use_mixer(mx, false);
    (dir, fake, hub)
}

#[test]
fn volume_steps_and_mute_against_the_mixer() {
    let _g = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    let (_dir, fake, hub) = setup(json!({"mixer": {"enabled": true, "linked": ["game"]}}));
    let cfg = Map::new();
    let env = Env::new(&cfg, 0, 1);
    let vol = |lid: &str| fake.input_of(lid).unwrap().volume;
    let mute = |lid: &str| fake.input_of(lid).unwrap().mute;
    hub.mixer().set_volume("music", "monitor", 50).unwrap();
    hub.mixer().set_volume("music", "stream", 30).unwrap();

    // lauter: Taste zeigt den neuen Stand sofort, geschrieben wird im Hintergrund
    let up = data("com.lindeck.mixer.channel", json!({"channel": "music", "mix": "monitor", "action": "up", "step": 5,
                                                     "display": "tile"}));
    let mut b = behavior_for(&up.uuid);
    hub.refresh();
    let img0 = b.image(&up, &env, (72, 72)).unwrap();
    assert_eq!(b.title(&up, &env).as_deref(), Some(""));
    assert!(b.own_opacity(&up));
    b.key_down(&up, &env);
    b.key_up(&up, &env, 50); // vor der Wiederholung loslassen
    wait_until("lauter", || vol("linewave_music_monitor") == 55);
    assert_eq!(vol("linewave_music_stream"), 30); // nicht gekoppelt: nur dieser Mix
    let img1 = b.image(&up, &env, (72, 72)).unwrap();
    assert_ne!(img0, img1);
    let shown = mixkeys::draw(&Look::Tile { icon: "music".into(), color: [0x1f, 0xa4, 0x63], vol: Some(55), muted: false,
                                            label: hub.current().mix("monitor").unwrap().name.clone(), alpha: 255 },
                             (72, 72));
    assert_eq!(img1, shown);

    // leiser über den Drehregler (Schritt 2 je Raste)
    let mut dial = data("com.lindeck.mixer.channel", json!({"channel": "music", "mix": "monitor"}));
    dial.dial = true;
    let mut d = behavior_for(&dial.uuid);
    d.dial_turn(&dial, &env, -3);
    wait_until("leiser", || vol("linewave_music_monitor") == 49);
    let fb = d.feedback(&dial, &env);
    assert_eq!(fb.title, "Music");
    assert_eq!(fb.value, "49% · Personal Mix");
    assert_eq!(fb.indicator, Some(49.0));

    // Drücken: stumm und wieder an
    d.dial_down(&dial, &env);
    wait_until("stumm", || mute("linewave_music_monitor"));
    assert!(!mute("linewave_music_stream"));
    assert_eq!(d.feedback(&dial, &env).indicator, Some(0.0));
    d.dial_down(&dial, &env);
    wait_until("an", || !mute("linewave_music_monitor"));

    // Taste „stumm“ für alle Mixe des Kanals
    let all = data("com.lindeck.mixer.channel", json!({"channel": "music", "mix": "both"}));
    let mut m = behavior_for(&all.uuid);
    m.key_down(&all, &env);
    wait_until("alle stumm", || mute("linewave_music_monitor") && mute("linewave_music_stream"));

    // gekoppelter Kanal: alle Mixe gemeinsam, die Taste zeigt ihren Mix
    hub.mixer().set_volume("game", "both", 40).unwrap();
    let linked = data("com.lindeck.mixer.channel", json!({"channel": "game", "mix": "stream", "action": "down", "step": 10}));
    let mut l = behavior_for(&linked.uuid);
    l.key_down(&linked, &env);
    l.key_up(&linked, &env, 30);
    wait_until("gekoppelt", || vol("linewave_game_monitor") == 30 && vol("linewave_game_stream") == 30);
    let lv = hub.level(&hub.current(), "game", "stream");
    assert_eq!(lv, (Some(30), false));

    // Lautstärke setzen
    let set = data("com.lindeck.mixer.channel", json!({"channel": "voice", "mix": "monitor", "action": "set", "volume": 70}));
    let mut s = behavior_for(&set.uuid);
    s.key_down(&set, &env);
    wait_until("gesetzt", || vol("linewave_voice_monitor") == 70);

    // Gedrückt halten wiederholt, bis losgelassen wird
    hub.mixer().set_volume("sfx", "monitor", 10).unwrap();
    let hold = data("com.lindeck.mixer.channel", json!({"channel": "sfx", "mix": "monitor", "action": "up", "step": 1}));
    let mut h = behavior_for(&hold.uuid);
    h.key_down(&hold, &env);
    wait_until("wiederholt", || vol("linewave_sfx_monitor") >= 13);
    h.key_up(&hold, &env, 1200);
    std::thread::sleep(Duration::from_millis(400));
    let after = vol("linewave_sfx_monitor");
    std::thread::sleep(Duration::from_millis(400));
    assert_eq!(vol("linewave_sfx_monitor"), after);
}

#[test]
fn mix_selection_pages_and_meters() {
    let _g = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    let (_dir, _fake, hub) = setup(json!({"mixer": {"enabled": true, "mixes": [{"id": "stream", "exclude": ["music"]}]}}));
    let cfg = Map::new();
    let env = Env::new(&cfg, 0, 1);
    wavehub::set_deck_mix("both");
    hub.refresh();

    // Platz 0 zeigt den ersten Kanal des gewählten Mixes
    let placed = data("com.lindeck.mixer.channel",
                      json!({"channels": ["music", "game"], "slot": 0, "mix": "selected", "display": "fader_top"}));
    let mut p = behavior_for(&placed.uuid);
    let second = data("com.lindeck.mixer.channel",
                      json!({"channels": ["music", "game"], "slot": 1, "mix": "selected", "display": "tile"}));
    let mut q = behavior_for(&second.uuid);
    let all_first = p.image(&placed, &env, (72, 72)).unwrap();
    let all_second = q.image(&second, &env, (72, 72)).unwrap();
    assert!(any_near(&all_second, [255, 255, 255], 0));

    // Mix-Wahl „Stream“: Musik ist nicht im Stream-Mix – Platz 1 bleibt leer (dunkel)
    let sel = data("com.lindeck.mixer.mixselect", json!({"mix": "stream"}));
    let mut ms = behavior_for(&sel.uuid);
    let before = ms.image(&sel, &env, (72, 72)).unwrap();
    ms.key_down(&sel, &env);
    assert_eq!(wavehub::deck_mix(), "stream");
    assert!(ms.fast_tick(&sel));
    let after = ms.image(&sel, &env, (72, 72)).unwrap();
    assert_ne!(before, after);
    assert!(p.fast_tick(&placed));
    assert!(q.fast_tick(&second));
    assert_ne!(p.image(&placed, &env, (72, 72)).unwrap(), all_first);
    let empty = q.image(&second, &env, (72, 72)).unwrap();
    assert!(empty.pixels().all(|px| px.0 == [0, 0, 0, 255]));
    assert_eq!(q.title(&second, &env).as_deref(), Some(""));

    // „cycle“ schaltet weiter: Stream -> Alle
    let cyc = data("com.lindeck.mixer.mixselect", json!({"mix": "cycle"}));
    let mut c = behavior_for(&cyc.uuid);
    c.key_down(&cyc, &env);
    assert_eq!(wavehub::deck_mix(), "both");

    // Seiten-Taste blättert weiter
    let pages = data("com.lindeck.mixer.pages", json!({"first": 1, "last": 7}));
    let mut pg = behavior_for(&pages.uuid);
    assert_eq!(pg.key_down(&pages, &env), freestream_deck::actions::Command::NextPage);
    assert!(pg.image(&pages, &env, (72, 72)).is_some());

    // Live-Pegel: neue Messung = neues Bild, gleiche Segmentzahl = kein neues Bild
    p.image(&placed, &env, (72, 72));
    hub.set_meter("music", Some(1.0));
    assert!(p.fast_tick(&placed));
    let loud = p.image(&placed, &env, (72, 72)).unwrap();
    hub.set_meter("music", Some(0.999));
    assert!(!p.fast_tick(&placed));
    hub.set_meter("music", None);
    assert!(p.fast_tick(&placed));
    assert_ne!(p.image(&placed, &env, (72, 72)).unwrap(), loud);
}

#[test]
fn scenes_and_output_keys() {
    let _g = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    freestream_core::i18n::set_language("en");
    let (_dir, fake, hub) = setup(json!({"mixer": {"enabled": true}}));
    let cfg = Map::new();
    let env = Env::new(&cfg, 0, 1);
    hub.mixer().set_volume("music", "both", 40).unwrap();
    hub.mixer().save_scene("Ruhig").unwrap();
    hub.mixer().set_volume("music", "both", 90).unwrap();
    let v = json!({"UUID": "com.lindeck.mixer.scene", "Settings": {"scene": "ruhig"}, "States": [{}, {}]});
    let sc = ActionData::new(v.as_object().unwrap().clone(), PathBuf::new());
    let mut b = behavior_for(&sc.uuid);
    b.tick(&sc);
    assert_eq!(b.live_state(&sc), Some(0));
    assert_eq!(b.feedback(&sc, &env).title, "Ruhig");
    b.key_down(&sc, &env);
    wait_until("Szene", || fake.input_of("linewave_music_monitor").unwrap().volume == 40);
    hub.refresh();
    b.tick(&sc);
    assert_eq!(b.live_state(&sc), Some(1));
    assert_eq!(b.feedback(&sc, &env).value, "active");
    let missing = data("com.lindeck.mixer.scene", json!({"scene": "gibt es nicht"}));
    assert_eq!(behavior_for(&missing.uuid).feedback(&missing, &env).value, "not found");

    // Ausgabe des Persönlichen Mixes: nächstes Gerät
    let out = data("com.lindeck.mixer.output", json!({"device": ""}));
    let mut o = behavior_for(&out.uuid);
    assert!(o.image(&out, &env, (72, 72)).is_some());
    o.key_down(&out, &env);
    wait_until("Ausgabe", || fake.cmds().iter().any(|c| c[0] == "move-sink-input"));
}
