//! pactl-Ausgaben zerlegen – mit echten Ausgaben von pipewire-pulse (PipeWire 1.0.5, Python-FreeWave
//! eingerichtet, Spotify und Firefox spielen; aufgenommen mit LC_ALL=C) und den Beispielen aus den
//! Python-Tests.

mod common;

use std::sync::Arc;

use freestream_wave::pactl::{parse_blocks, Block, Pactl};
use freestream_wave::{Error, FileSettings, Mixer};

const SINKS: &str = include_str!("fixtures/sinks.txt");
const SOURCES: &str = include_str!("fixtures/sources.txt");
const INPUTS: &str = include_str!("fixtures/sink-inputs.txt");
const MODULES: &str = include_str!("fixtures/modules.txt");
const STATE: &str = include_str!("fixtures/python-mixer.json");

/// pactl, das die aufgenommenen Ausgaben liefert (schreibende Befehle werden nur gemerkt).
struct FixturePactl(std::sync::Mutex<Vec<Vec<String>>>);

impl Pactl for FixturePactl {
    fn run(&self, args: &[String]) -> Result<String, Error> {
        self.0.lock().unwrap().push(args.to_vec());
        Ok(match args[0].as_str() {
            "get-default-sink" => "linewave_system".into(),
            "get-default-source" => "alsa_input.usb-Elgato_Wave_3".into(),
            _ => String::new(),
        })
    }
    fn list(&self, what: &str) -> String {
        match what {
            "sinks" => SINKS,
            "sources" => SOURCES,
            "sink-inputs" => INPUTS,
            "modules" => MODULES,
            _ => "",
        }
        .to_string()
    }
}

fn find<'a>(blocks: &'a [Block], name: &str) -> &'a Block {
    blocks.iter().find(|b| b.name() == name).unwrap_or_else(|| panic!("{name} fehlt"))
}

#[test]
fn parses_real_sinks_sources_and_modules() {
    let sinks = parse_blocks(SINKS);
    assert_eq!(sinks.len(), 9);
    let music = find(&sinks, "linewave_music");
    assert_eq!(music.description(), "FreeWave Musik");
    assert_eq!((music.volume, music.mute), (Some(100), false));
    assert_eq!(music.prop("monitor.channel-volumes"), "true");
    assert_eq!(music.prop("device.description"), "FreeWave Musik");
    let sources = parse_blocks(SOURCES);
    assert_eq!(sources.len(), 10);
    assert!(sources.iter().any(|b| b.name() == "linewave_streammix.monitor"));
    let modules = parse_blocks(MODULES);
    assert_eq!(modules.len(), 37);
    let m = modules.iter().find(|b| b.argument.as_deref().is_some_and(|a| a.starts_with("sink_name=linewave_music ")))
        .unwrap();
    assert_eq!(m.name(), "module-null-sink");
    assert!(m.argument.as_deref().unwrap().contains("monitor.channel-volumes='true'"));
    // Argumente über mehrere Zeilen (PipeWire-Module): nur die erste Zeile, Rest wird nicht zur Property
    let rt = &modules[0];
    assert_eq!((rt.index.as_str(), rt.name()), ("1", "libpipewire-module-rt"));
}

#[test]
fn parses_real_sink_inputs() {
    let inputs = parse_blocks(INPUTS);
    assert_eq!(inputs.len(), 16);
    let by_loop = |lid: &str| inputs.iter().find(|b| freestream_wave::mixer::loop_id(b) == lid).unwrap();
    let mm = by_loop("linewave_music_monitor");
    assert_eq!((mm.volume, mm.mute), (Some(65), false)); // „ 65%“ mit Leerzeichen davor
    assert_eq!(mm.prop("media.name"), "FreeWave Musik → Persönlicher Mix");
    assert!(by_loop("linewave_sfx_stream").mute);
    assert!(by_loop("linewave_mic_monitor").mute);
    let ff = inputs.iter().find(|b| b.prop("application.name") == "Firefox").unwrap();
    assert_eq!(ff.prop("application.process.binary"), "firefox-esr");
    assert_eq!(ff.sink.as_deref(), Some("184"));
    // wie in Python: auch die Format-Zeile landet (harmlos) bei den Properties
    assert!(ff.props.contains_key("Format: pcm, format.sample_format"));
}

#[test]
fn samples_from_python_tests() {
    // tests/test_waveapp.py: deutsches Komma in dB
    let b = &parse_blocks("Sink Input #42\n\tDriver: PipeWire\n\tSink: 57\n\tMute: no\n\tVolume: front-left: 65536 / 100% / 0,00 dB\n")[0];
    assert_eq!((b.index.as_str(), b.sink.as_deref(), b.volume), ("42", Some("57"), Some(100)));
    // tests/test_fixes_backend.py
    let loop_ = "Sink Input #42\n\tSink: 5\n\tMute: no\n\tVolume: front-left: 52429 /  80% / -5.81 dB\n\tProperties:\n\t\tlinewave.loop = \"linewave_music_monitor\"\n";
    let b = &parse_blocks(loop_)[0];
    assert_eq!((b.volume, b.mute), (Some(80), false));
    assert_eq!(freestream_wave::mixer::loop_id(b), "linewave_music_monitor");
    let b = &parse_blocks("Sink Input #87\n\tSink: 39\n\tMute: yes\n\tVolume: front-left: 52429 /  80% / -5.81 dB\n")[0];
    assert!(b.mute);
    // ältere Versionen: Kennung in media.name, fremde Namen zählen nicht
    let old = &parse_blocks("Sink Input #3\n\tProperties:\n\t\tmedia.name = \"lindeck_music_monitor\"\n")[0];
    assert_eq!(freestream_wave::mixer::loop_id(old), "lindeck_music_monitor");
    let other = &parse_blocks("Sink Input #4\n\tProperties:\n\t\tmedia.name = \"Song\"\n")[0];
    assert_eq!(freestream_wave::mixer::loop_id(other), "");
    // Text vor dem ersten Eintrag und leere Ausgabe
    assert!(parse_blocks("").is_empty());
    assert_eq!(parse_blocks("Fehler\nSink #1\n\tName: a\n").len(), 1);
}

#[test]
fn mixer_reads_real_pipewire_state() {
    freestream_core::i18n::set_language("en");
    let dir = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(dir.path().join("state")).unwrap();
    std::fs::write(dir.path().join("state/mixer.json"), STATE).unwrap();
    let mut data = serde_json::Map::new();
    data.insert("mixer".into(), serde_json::json!({"enabled": true}));
    let pactl = Arc::new(FixturePactl(Default::default()));
    let mx = Mixer::new(Box::new(FileSettings::memory(data)), pactl.clone(), dir.path().join("state")).without_sleep();
    assert!(mx.is_active());
    let levels = mx.levels();
    assert_eq!(levels.len(), 14);
    assert_eq!(levels["music_monitor"].volume, 65);
    assert!(levels["sfx_stream"].mute && levels["mic_monitor"].mute);
    assert_eq!(mx.get("music", "monitor"), (Some(65), false));
    let streams = mx.streams();
    let chans: Vec<(&str, Option<&str>)> = streams.iter().map(|s| (s.app.as_str(), s.channel.as_deref())).collect();
    assert_eq!(chans, vec![("Firefox", Some("browser")), ("Spotify", Some("music"))]);
    assert_eq!(streams[0].key, "firefox-esr");
    let outs: Vec<String> = mx.outputs().into_iter().map(|d| d.name).collect();
    assert_eq!(outs, ["alsa_output.usb-headphones", "alsa_output.speakers"]);
    let ins: Vec<String> = mx.inputs().into_iter().map(|d| d.name).collect();
    assert_eq!(ins, ["alsa_input.usb-Elgato_Wave_3"]);
    let masters = mx.masters();
    assert_eq!(masters["music"].volume, 100);
    assert_eq!(masters["mic"].volume, 100); // an der Quelle des Mikrofons
    assert_eq!(mx.monitor_output_name(), "USB");
    let ls = mx.level_sources();
    assert_eq!(ls["mix:monitor"], "alsa_output.usb-headphones.monitor");
    assert_eq!(ls["mix:stream"], "linewave_streammix.monitor");
    assert_eq!(freestream_wave::wavemic::find_source(&*pactl).as_deref(), Some("alsa_input.usb-Elgato_Wave_3"));
    // Abfragen allein schreiben nichts
    assert!(pactl.0.lock().unwrap().iter().all(|c| c[0].starts_with("get-")));
    // Lautstärke über die echten Indizes
    mx.set_volume("music", "both", 30).unwrap();
    let cmds = pactl.0.lock().unwrap().clone();
    assert!(cmds.contains(&vec!["set-sink-input-volume".into(), "156".into(), "30%".into()]));
    assert!(cmds.contains(&vec!["set-sink-input-volume".into(), "170".into(), "30%".into()]));
    // schon verteilte Programme werden nicht noch einmal verschoben, FreeWave-Loopbacks nie
    pactl.0.lock().unwrap().clear();
    assert_eq!(mx.route_streams(), 2); // beide laufen schon im richtigen Kanal – PipeWire nimmt es hin
    let moves: Vec<Vec<String>> = pactl.0.lock().unwrap().iter().filter(|c| c[0] == "move-sink-input").cloned().collect();
    assert_eq!(moves, vec![vec!["move-sink-input".to_string(), "359".into(), "linewave_browser".into()],
                           vec!["move-sink-input".to_string(), "362".into(), "linewave_music".into()]]);
    assert_eq!(mx.route_streams(), 0);
}
