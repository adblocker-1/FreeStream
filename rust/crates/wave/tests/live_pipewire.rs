//! Mit echtem PipeWire (pipewire-pulse): nur auf Wunsch, z. B.
//! `FREEWAVE_LIVE=1 cargo test -p freestream-wave --test live_pipewire -- --ignored`.
//! Legt Kanäle, Mixe und eine Effektkette an und räumt alles wieder ab. Achtung: ändert das
//! Standardgerät der laufenden Sitzung kurzzeitig.

use std::sync::Arc;

use freestream_wave::effects::{self, Effect};
use freestream_wave::pactl::{parse_blocks, Pactl, SystemPactl};
use freestream_wave::{FileSettings, Mixer, Mute};
use serde_json::json;

fn names(what: &str) -> Vec<String> {
    parse_blocks(&SystemPactl.list(what)).into_iter().filter_map(|b| b.name).collect()
}

#[test]
#[ignore]
fn setup_effects_and_teardown_on_real_pipewire() {
    if std::env::var("FREEWAVE_LIVE").is_err() {
        return;
    }
    freestream_core::i18n::set_language("de");
    let dir = tempfile::tempdir().unwrap();
    let cfg = dir.path().join("config.json");
    std::fs::write(&cfg, json!({"mixer": {"enabled": true}}).to_string()).unwrap();
    let mx = Arc::new(Mixer::new(Box::new(FileSettings::load(&cfg)), Arc::new(SystemPactl), dir.path().join("state")));
    mx.setup().unwrap();
    assert!(mx.is_active());
    let sinks = parse_blocks(&SystemPactl.list("sinks"));
    let music = sinks.iter().find(|b| b.name() == "linewave_music").unwrap();
    assert_eq!(music.description(), "FreeWave Musik"); // Leerzeichen bleiben dank doppelter Quotes
    assert_eq!(music.prop("monitor.channel-volumes"), "true");
    assert!(sinks.iter().any(|b| b.name() == "linewave_streammix" && b.description() == "FreeWave Stream-Mix"));
    mx.set_volume("music", "monitor", 55).unwrap();
    // PipeWire merkt sich Stummschaltungen je media.name (stream-restore) – definiert anfangen
    mx.set_mute("music", "all", Mute::Set(false), false).unwrap();
    assert_eq!(mx.get("music", "monitor"), (Some(55), false));
    assert!(mx.levels()["mic_monitor"].mute);
    assert!(mx.set_mute("music", "all", Mute::Toggle, false).unwrap());
    assert!(!mx.set_mute("music", "all", Mute::Toggle, false).unwrap());
    let chat = mx.add_mix("Chat", "chat", "", &[], true, &[]).unwrap();
    assert!(names("sinks").contains(&"linewave_mix_chat".to_string()));
    mx.listen(&chat.id).unwrap();
    assert!(names("sinks").contains(&"linewave_park".to_string()));
    mx.listen("monitor").unwrap();
    assert!(!names("sinks").contains(&"linewave_park".to_string()));
    // Effektkette mit eingebautem Hochpass: eigener pipewire-Prozess, Quelle linewave_music_fx
    if freestream_wave::pactl::have("pipewire") {
        let hp = effects::builtin().into_iter().next().unwrap();
        mx.fx().set_chain("music", &[effects::new_effect(&hp)]);
        mx.rebuild_channel("music").unwrap();
        assert!(mx.fx().running("music"), "Effektkette läuft nicht");
        assert!(names("sources").contains(&"linewave_music_fx".to_string()));
        let st = mx.state();
        assert_eq!(st["sources"]["music"], json!("linewave_music_fx"));
        assert_eq!(mx.levels()["music_monitor"].volume, 55); // Lautstärke übernommen
        mx.fx().set_chain("music", &Vec::<Effect>::new());
        mx.rebuild_channel("music").unwrap();
        assert!(!mx.fx().running("music"));
    }
    // Rauschunterdrückung (WebRTC, bei PipeWire dabei)
    if freestream_wave::noise::webrtc_available() {
        mx.noise().set(Some(true), Some("webrtc"), None).unwrap();
        mx.rebuild_channel("mic").unwrap();
        assert!(mx.noise().running(), "Rauschunterdrückung läuft nicht");
        assert_eq!(mx.state()["sources"]["mic"], json!("linewave_mic_ns"));
        assert!(mx.levels()["mic_monitor"].mute);
    }
    mx.teardown(true);
    assert!(!mx.noise().running());
    assert!(!mx.is_active());
    assert!(!names("sinks").iter().any(|n| n.starts_with("linewave_")));
    assert!(!parse_blocks(&SystemPactl.list("modules")).iter()
        .any(|b| b.argument.as_deref().unwrap_or("").contains("linewave_")));
}
