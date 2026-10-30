//! Verhalten ohne PipeWire: Zuordnung der Programme, gekoppelte Regler, Ducking, Pegel, Wave:3,
//! Hintergrunddienst und Snapshot für das Fenster.

mod common;

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use common::{mixer, FakePactl, Input};
use freestream_wave::ducking::Ducker;
use freestream_wave::levels::{LevelMonitor, LevelSource, ParecSource};
use freestream_wave::mixer::{route_matches, Duck, DuckFile};
use freestream_wave::wave3hw::{FieldValue, Transport, Wave3, Wave3Error};
use freestream_wave::{Mute, WaveHost};
use serde_json::json;

fn add_stream(fake: &FakePactl, index: &str, props: &[(&str, &str)]) {
    let props: BTreeMap<String, String> = props.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect();
    fake.0.lock().unwrap().inputs.insert(index.into(), Input { index: index.into(), sink: "1".into(), volume: 80,
                                                               mute: false, props });
}

fn channel_of(mx: &freestream_wave::Mixer, index: &str) -> Option<String> {
    mx.streams().into_iter().find(|s| s.index == index).and_then(|s| s.channel)
}

#[test]
fn app_to_channel_rules() {
    assert!(route_matches("firefox", "firefox firefox-esr"));
    assert!(route_matches("steam", "steamwebhelper"));
    assert!(!route_matches("wine", "gwine"));
    assert!(route_matches("game.exe", "mein spiel game.exe"));
    let dir = tempfile::tempdir().unwrap();
    let (mx, fake) = mixer(dir.path(), json!({"mixer": {"enabled": true,
        "routes": {"Firefox-ESR": "voice", "spotify": "", "game.exe": "game", "zoom": "nochannel"}}}));
    mx.setup().unwrap();
    add_stream(&fake, "20", &[("application.name", "Discord"), ("application.process.binary", "Discord")]);
    add_stream(&fake, "21", &[("node.name", "steam_app_42"), ("application.id", "com.valvesoftware.Steam")]);
    add_stream(&fake, "22", &[("application.name", "ZOOM VoiceEngine")]);
    add_stream(&fake, "23", &[("media.name", "linewave_music_monitor")]); // eigener Loopback (alte Version)
    assert_eq!(mx.route_streams(), 4);
    assert_eq!(channel_of(&mx, "9").as_deref(), Some("voice")); // eigene Regel vor der eingebauten (firefox)
    assert_eq!(channel_of(&mx, "7"), None); // eingebaute Regel abgeschaltet
    assert_eq!(channel_of(&mx, "8").as_deref(), Some("game"));
    assert_eq!(channel_of(&mx, "20").as_deref(), Some("voice"));
    assert_eq!(channel_of(&mx, "21").as_deref(), Some("game"));
    assert_eq!(channel_of(&mx, "22"), None); // Kanal gibt es nicht: Regel übersprungen, eingebaute (zoom) ersetzt
    assert!(mx.streams().iter().all(|s| s.index != "23"));
    // schon gesehene Streams bleiben, wo der Nutzer sie hingelegt hat
    mx.move_stream("8", "sfx", "").unwrap();
    assert_eq!(mx.route_streams(), 0);
    assert_eq!(channel_of(&mx, "8").as_deref(), Some("sfx"));
    assert!(mx.move_stream("8", "mic", "").is_err());
    // dauerhaft merken und per Programmname zuordnen
    mx.move_stream("7", "music", "Spotify").unwrap();
    assert_eq!(mx.conf()["routes"]["spotify"], json!("music"));
    assert_eq!(mx.assign("discord", "browser").unwrap(), 1);
    assert_eq!(channel_of(&mx, "20").as_deref(), Some("browser"));
}

#[test]
fn linked_faders_and_volume_steps() {
    let dir = tempfile::tempdir().unwrap();
    let (mx, fake) = mixer(dir.path(), json!({"mixer": {"enabled": true, "linked": ["music"]}}));
    mx.setup().unwrap();
    mx.set_volume("music", "both", 60).unwrap();
    mx.set_volume("music", "stream", 40).unwrap(); // gekoppelt: −20 auch im Persönlichen Mix
    assert_eq!(mx.levels()["music_monitor"].volume, 40);
    mx.set_linked("music", false);
    mx.set_volume("music", "monitor", 60).unwrap();
    mx.set_linked("music", true);
    mx.set_volume("music", "monitor", 80).unwrap(); // +20 auch im Stream-Mix
    let l = mx.levels();
    assert_eq!((l["music_monitor"].volume, l["music_stream"].volume), (80, 60));
    let out = mx.change_volume("music", "monitor", 30, false).unwrap(); // gekoppelt: alle Mixe, nie über 100
    assert_eq!(out.into_iter().collect::<Vec<_>>(), vec![("monitor".into(), 100), ("stream".into(), 90)]);
    let out = mx.change_volume("system", "stream", -150, true).unwrap();
    assert_eq!(out["stream"], 0);
    // zuletzt gelesener Stand (global wie in Python; parallele Tests können ihn verwerfen)
    if let Some(r) = mx.recent_level("system", "stream") {
        assert_eq!(r, (0, false));
    }
    assert_eq!(mx.get("system", "stream"), (Some(0), false));
    assert!(mx.set_mute("music", "all", Mute::Toggle, false).unwrap());
    assert!(!mx.set_mute("music", "all", Mute::Toggle, false).unwrap());
    assert!(mx.set_volume("nope", "monitor", 3).is_err());
    let err = mx.set_volume("music", "chat", 3).unwrap_err().0;
    assert!(err.contains("music/chat"), "{err}");
    // nie über 100 %
    assert!(fake.cmds().iter().filter(|c| c[0] == "set-sink-input-volume")
        .all(|c| c[2].trim_end_matches('%').parse::<i64>().unwrap() <= 100));
}

#[test]
fn ducking_lowers_and_restores() {
    let dir = tempfile::tempdir().unwrap();
    let (mx, fake) = mixer(dir.path(), json!({"mixer": {"enabled": true,
        "ducking": {"enabled": true, "channels": ["music", "mic"], "amount_db": 12, "attack_ms": 0,
                    "hold_ms": 0, "release_ms": 0}}}));
    mx.setup().unwrap();
    mx.set_volume("music", "monitor", 80).unwrap();
    let level = Arc::new(AtomicU64::new(0f64.to_bits()));
    let now = Arc::new(Mutex::new(0.0f64));
    let (l2, n2) = (level.clone(), now.clone());
    let d = Ducker::with(mx.clone(), Some(Box::new(move || f64::from_bits(l2.load(Ordering::SeqCst)))), None,
                         Box::new(move || *n2.lock().unwrap()));
    let vol = |lid: &str| fake.input_of(lid).unwrap().volume;
    assert_eq!(d.step(Some(0.04)), 0.0);
    level.store(0.9f64.to_bits(), Ordering::SeqCst); // −6 dB: spricht
    *now.lock().unwrap() = 1.0;
    assert_eq!(d.step(Some(0.04)), 1.0);
    assert_eq!(vol("linewave_music_monitor"), 50); // 80 · 10^(−12/60)
    assert_eq!(vol("linewave_music_stream"), 63);
    assert_eq!(vol("linewave_mic_monitor"), 100); // Mikrofon wird nie abgesenkt
    // während des Absenkens: Regler zeigen und setzen den Nutzerwert
    assert_eq!(mx.get("music", "monitor"), (Some(80), false));
    mx.set_volume("music", "monitor", 60).unwrap();
    assert_eq!(vol("linewave_music_monitor"), 38);
    assert_eq!(d.base("music_monitor"), Some(60));
    let state: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(dir.path().join("state/ducking.json")).unwrap()).unwrap();
    assert_eq!(state["bases"]["music_monitor"], json!(60));
    // ein anderes Programm (FreeDeck) sieht ducking.json und meldet seinen Wert über ducking-user.json
    let other = DuckFile::load(&dir.path().join("state")).unwrap();
    assert_eq!(other.base("music_monitor"), Some(60));
    other.user_set(&[("music_stream".into(), 50)]);
    *now.lock().unwrap() = 2.0;
    d.step(Some(0.04));
    assert_eq!(d.base("music_stream"), Some(50));
    assert_eq!(vol("linewave_music_stream"), 32);
    // fremde Änderung (pavucontrol) wird erkannt
    fake.0.lock().unwrap().inputs.values_mut().find(|b| b.props.get("linewave.loop").map(String::as_str)
        == Some("linewave_music_monitor")).unwrap().volume = 25;
    *now.lock().unwrap() = 3.0;
    d.step(Some(0.04));
    assert_eq!(d.base("music_monitor"), Some(40));
    // Stille: exakt zurück auf den Nutzerwert
    level.store(0f64.to_bits(), Ordering::SeqCst);
    *now.lock().unwrap() = 4.0;
    assert_eq!(d.step(Some(0.04)), 0.0);
    assert_eq!((vol("linewave_music_monitor"), vol("linewave_music_stream")), (40, 50));
    assert!(!dir.path().join("state/ducking.json").exists());
    assert_eq!(mx.get("music", "monitor"), (Some(40), false));
}

#[test]
fn level_meter_rises_fast_and_falls_slowly() {
    struct Src(Arc<Mutex<f64>>, Arc<Mutex<bool>>);
    impl LevelSource for Src {
        fn level(&self) -> f64 {
            *self.0.lock().unwrap()
        }
        fn ended(&mut self) -> bool {
            *self.1.lock().unwrap()
        }
        fn stop(&mut self) {}
    }
    let lv = Arc::new(Mutex::new(0.0));
    let ended = Arc::new(Mutex::new(false));
    let started = Arc::new(Mutex::new(Vec::<String>::new()));
    let clock = Arc::new(Mutex::new(0.0f64));
    let (l2, e2, s2, c2) = (lv.clone(), ended.clone(), started.clone(), clock.clone());
    let mut mon = LevelMonitor::with(
        Box::new(move |dev| {
            s2.lock().unwrap().push(dev.to_string());
            Some(Box::new(Src(l2.clone(), e2.clone())) as Box<dyn LevelSource>)
        }),
        Box::new(move || *c2.lock().unwrap()),
        1.0,
    );
    let mut t = indexmap::IndexMap::new();
    t.insert("music".to_string(), "linewave_music.monitor".to_string());
    t.insert("mic".to_string(), String::new()); // ohne Gerät: nicht messen
    mon.watch(&t);
    assert_eq!(*started.lock().unwrap(), ["linewave_music.monitor"]);
    *lv.lock().unwrap() = 0.8;
    assert_eq!(mon.read()["music"], 0.8);
    *lv.lock().unwrap() = 0.1;
    *clock.lock().unwrap() = 0.25;
    assert!((mon.read()["music"] - 0.55).abs() < 1e-9);
    *clock.lock().unwrap() = 1.0;
    assert!((mon.read()["music"] - 0.1).abs() < 1e-9);
    // beendet (Quelle verschwunden): erst nach RESTART_S neu starten
    *ended.lock().unwrap() = true;
    *clock.lock().unwrap() = 1.5;
    mon.watch(&t);
    assert_eq!(started.lock().unwrap().len(), 1);
    *clock.lock().unwrap() = 2.5;
    mon.watch(&t);
    assert_eq!(started.lock().unwrap().len(), 2);
    mon.watch(&indexmap::IndexMap::new());
    assert!(!mon.running());
    assert_eq!(ParecSource::command("parec", "linewave_music.monitor"),
               ["parec", "--device=linewave_music.monitor", "--raw", "--format=s16le", "--rate=16000", "--channels=1",
                "--latency-msec=40", "--property=node.dont-reconnect=true"]);
}

/// Wave:3 im Speicher (wie FakeWave3 in tests/test_integrations.py).
struct FakeWave3 {
    block: Arc<Mutex<Vec<u8>>>,
    api: (u8, u8),
    /// ändert beim Schreiben zusätzlich dieses Byte (fehlerhafte Firmware)
    meddle: Option<usize>,
}

impl Transport for FakeWave3 {
    fn control(&mut self, req_type: u8, req: u8, value: u16, data: Option<&[u8]>, len: usize)
               -> Result<Vec<u8>, Wave3Error> {
        let mut b = self.block.lock().unwrap();
        match (req_type, req, value) {
            (0xA1, 0x85, 0x0A) => Ok(vec![self.api.0, self.api.1]),
            (0xA1, 0x85, 0x00) => Ok(b[..len].to_vec()),
            (0x21, 0x05, 0x00) => {
                let d = data.unwrap();
                b[..d.len()].copy_from_slice(d);
                if let Some(i) = self.meddle.take() {
                    b[i] ^= 1;
                }
                Ok(d.to_vec())
            }
            _ => Err(Wave3Error("?".into())),
        }
    }
}

#[test]
fn wave3_transactions() {
    let block = Arc::new(Mutex::new(vec![0u8; 16]));
    let w = Wave3::with(Box::new(FakeWave3 { block: block.clone(), api: (5, 3), meddle: None }));
    assert_eq!(w.api, (5, 3));
    assert_eq!(w.set("gain", 21.3).unwrap(), FieldValue::Num(21.5));
    assert_eq!(w.toggle("lowcut").unwrap(), FieldValue::Bool(true));
    assert_eq!(w.change("gain", -2.0).unwrap(), FieldValue::Num(19.5));
    assert_eq!(w.set("leds_flip", 1.0).unwrap(), FieldValue::Bool(true));
    assert!(w.set("rainbow", 1.0).is_err());
    let before = block.lock().unwrap().clone();
    let bad = Wave3::with(Box::new(FakeWave3 { block: block.clone(), api: (5, 3), meddle: Some(9) }));
    let e = bad.set("mute", 1.0).unwrap_err();
    assert!(e.0.contains("[9]"), "{e}");
    assert_eq!(*block.lock().unwrap(), before); // zurückgeschrieben
    let old = Wave3::with(Box::new(FakeWave3 { block, api: (5, 2), meddle: None }));
    assert_eq!(old.config_len(), 14);
    assert!(old.set("gain_lock", 1.0).is_err());
    assert_eq!(old.get().unwrap().len(), 9);
}

#[test]
fn host_and_snapshot_for_the_window() {
    let dir = tempfile::tempdir().unwrap();
    freestream_core::i18n::set_language("en");
    let cfg = dir.path().join("config.json");
    std::fs::write(&cfg, json!({"mixer": {"enabled": true, "routes": {"game.exe": "game"}}}).to_string()).unwrap();
    let fake = FakePactl::new();
    let mixer = Arc::new(freestream_wave::Mixer::new(Box::new(freestream_wave::FileSettings::load(&cfg)),
                                                     Arc::new(fake.clone()), dir.path().join("state")).without_sleep());
    let host = Arc::new(WaveHost::with_mixer(mixer));
    host.mic.lock().unwrap().hw_present = || false;
    host.start();
    assert!(host.mixer.is_active() && host.error().is_empty());
    host.tick();
    let chans: Vec<(String, Option<String>)> = host.mixer.streams().into_iter().map(|s| (s.app, s.channel)).collect();
    assert!(chans.contains(&("Mein Spiel".into(), Some("game".into()))));
    assert!(chans.contains(&("Spotify".into(), Some("music".into()))));
    // config.json von außen geändert (Python-FreeWave, FreeDeck): wird beim nächsten Takt gelesen
    std::thread::sleep(std::time::Duration::from_millis(20));
    std::fs::write(&cfg, json!({"mixer": {"enabled": true, "linked": ["music"]}}).to_string()).unwrap();
    host.tick();
    assert!(host.mixer.is_linked("music"));

    let ctl = freestream_wave::Controller::new(host.clone());
    let snap = freestream_wave::control::wait_for(&ctl, 1, std::time::Duration::from_secs(10)).unwrap();
    assert!(snap.active && snap.enabled);
    assert_eq!(snap.mixes.iter().map(|m| m.mix.id.as_str()).collect::<Vec<_>>(), ["monitor", "stream"]);
    let music = snap.channel("music").unwrap();
    assert!(music.linked);
    assert_eq!(music.levels["monitor"].volume, 100);
    assert_eq!(music.streams[0].app, "Spotify");
    assert!(snap.channel("mic").unwrap().levels["monitor"].mute);
    let v = snap.version;
    for pct in [10, 20, 30, 42] {
        ctl.set_volume("system", "monitor", pct); // Regler ziehen: es zählt der letzte Wert
    }
    ctl.act(|m| m.set_member("system", "stream", false));
    ctl.act(|m| m.remove_mix("monitor"));
    let snap = freestream_wave::control::wait_for(&ctl, v + 3, std::time::Duration::from_secs(10)).unwrap();
    let _ = snap;
    std::thread::sleep(std::time::Duration::from_millis(300));
    let snap = ctl.snapshot();
    let system = snap.channel("system").unwrap();
    assert_eq!(system.levels["monitor"].volume, 42);
    assert_eq!(system.mixes, ["monitor"]);
    assert_eq!(ctl.take_errors(), [freestream_core::tr!("The Personal Mix and the Stream Mix cannot be deleted")]);
    assert!(ctl.levels().is_empty()); // Pegel aus, solange das Fenster sie nicht will
    drop(ctl);
    host.enable_mixer(false).unwrap();
    assert!(!host.mixer.is_active());
    host.shutdown();
    host.cleanup();
}
