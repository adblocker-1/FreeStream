//! Rust und Python machen dasselbe: gleicher Ablauf (tests/py/scenario.py) mit simuliertem pactl – die
//! pactl-Befehle (Null-Sinks, Loopbacks, Namen, Properties), config.json, mixer.json und die Ergebnisse
//! müssen gleich sein. Ohne Python 3 mit freestream werden die Vergleiche übersprungen; der Ablauf selbst
//! wird dann trotzdem geprüft.

mod common;

use std::process::Command;

use common::{mixer, python, repo_root};
use freestream_wave::effects::{self, Effect, Plugin};
use freestream_wave::mixer::{Channel, Levels};
use freestream_wave::Mute;
use serde_json::{json, Value};

fn levels_json(l: &Levels) -> Value {
    Value::Object(l.iter().map(|(k, v)| (k.clone(), json!([v.volume, v.mute]))).collect())
}

fn mix_json(m: &freestream_wave::Mix) -> Value {
    json!({"id": m.id, "name": m.name, "icon": m.icon, "color": m.color, "virtual": m.virtual_,
           "devices": m.devices, "exclude": m.exclude, "builtin": m.builtin})
}

fn stream_json(s: &freestream_wave::mixer::Stream) -> Value {
    json!({"index": s.index, "channel": s.channel, "sink": s.sink, "app": s.app, "key": s.key, "names": s.names})
}

fn scene_json(s: &freestream_wave::mixer::Scene) -> Value {
    let levels: serde_json::Map<String, Value> = s.levels.iter().map(|(c, m)| {
        (c.clone(), Value::Object(m.iter().map(|(k, (v, mu))| (k.clone(), json!([*v as i64, mu]))).collect()))
    }).collect();
    json!({"id": s.id, "name": s.name, "levels": levels})
}

/// Python-Ablauf ausführen; None ohne Python.
fn run_python(part: &str) -> Option<Value> {
    let py = python()?;
    let dir = tempfile::tempdir().unwrap();
    let out = Command::new(py)
        .arg(repo_root().join("rust/crates/wave/tests/py/scenario.py"))
        .arg(dir.path())
        .arg(part)
        .env("PYTHONPATH", repo_root())
        .env("FREESTREAM_LANG", "en")
        .env("XDG_CONFIG_HOME", dir.path().join("c"))
        .env("XDG_STATE_HOME", dir.path().join("s"))
        .env("XDG_DATA_HOME", dir.path().join("d"))
        .env_remove("LADSPA_PATH")
        .current_dir(repo_root())
        .output()
        .unwrap();
    assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
    Some(serde_json::from_slice(&out.stdout).unwrap())
}

fn without_created(v: Option<Value>) -> Value {
    let mut v = v.unwrap_or(Value::Null);
    if let Some(o) = v.as_object_mut() {
        o.remove("created");
    }
    v
}

#[test]
fn same_commands_config_and_state_as_python() {
    let dir = tempfile::tempdir().unwrap();
    let (mx, fake) = mixer(dir.path(), json!({}));
    let mut out = serde_json::Map::new();
    mx.setup().unwrap();
    out.insert("after_setup".into(), json!(fake.cmds().len()));
    mx.set_volume("music", "monitor", 70).unwrap();
    mx.set_mute("mic", "stream", Mute::Set(true), false).unwrap();
    let chat = mx.add_mix("Chat-Mix", "chat", "#22c55e", &[], true, &[]).unwrap();
    out.insert("chat".into(), mix_json(&chat));
    mx.set_member("music", "chatmix", false).unwrap();
    assert!(!mx.in_mix("music", "chatmix"));
    mx.set_member("music", "chatmix", true).unwrap();
    mx.update_mix("chatmix", None, None, None, Some(&["alsa_output.speakers".to_string()]), None).unwrap();
    assert_eq!(fake.sink_of("linewave_mixout-chatmix-0").as_deref(), Some("alsa_output.speakers"));
    let l1 = mx.listen("chatmix").unwrap();
    let l2 = mx.listening();
    assert!(fake.has_sink("linewave_park"));
    let l3 = mx.listen("monitor").unwrap();
    out.insert("listen".into(), json!([l1, l2, l3]));
    let cap = mx.add_channel("Capture", "input", "alsa_input.usb-capture", "", "", true).unwrap();
    out.insert("capture".into(), json!({"id": cap.id, "name": cap.name, "kind": cap.kind, "source": cap.source}));
    mx.set_master("music", 140).unwrap();
    mx.set_master_mute("music", Mute::Set(true)).unwrap();
    mx.set_master("mic", 70).unwrap();
    out.insert("routed".into(), json!(mx.route_streams()));
    out.insert("streams".into(), Value::Array(mx.streams().iter().map(stream_json).collect()));
    out.insert("monitor".into(), json!(mx.set_monitor_output("next").unwrap()));
    out.insert("scene".into(), scene_json(&mx.save_scene("Gaming").unwrap()));
    mx.set_volume("music", "monitor", 20).unwrap();
    mx.apply_scene("gaming").unwrap();
    out.insert("active_scene".into(), json!(mx.active_scene(None).map(|s| s.id)));
    mx.set_linked("game", true);
    mx.set_volume("game", "monitor", 50).unwrap();
    let routes = mx.channel_routes("browser", &["firefox-esr".to_string()]);
    out.insert("routes".into(), Value::Array(routes.iter().map(|r| json!({"key": r.key, "name": r.name, "own": r.own})).collect()));
    let moved = mx.set_channel_routes("sfx", &[("Spotify".into(), "Spotify".into())], &["firefox".into()]).unwrap();
    out.insert("moved".into(), json!(moved));
    mx.remove_mix("chatmix").unwrap();
    mx.update_channel("music", Some("Musik 2"), None, None, None).unwrap();
    mx.remove_channel("capture").unwrap();
    out.insert("levels".into(), levels_json(&mx.levels()));
    out.insert("masters".into(), levels_json(&mx.masters()));
    out.insert("level_sources".into(), Value::Object(mx.level_sources().into_iter().map(|(k, v)| (k, json!(v))).collect()));
    out.insert("mixes".into(), Value::Array(mx.mixes().iter().map(mix_json).collect()));
    let chans = mx.all_channels();
    mx.teardown(true);

    // ohne Python: wenigstens das Wichtigste selbst prüfen
    let cmds = fake.cmds();
    assert_eq!(out["routed"], json!(2));
    assert_eq!(out["levels"]["game_monitor"], json!([50, false]));
    assert!(cmds.iter().any(|c| c == &["set-sink-volume", "linewave_music", "100%"]));
    assert!(cmds.iter().any(|c| c == &["set-source-volume", "alsa_input.mic", "70%"]));
    assert!(cmds.iter().any(|c| c.len() > 3 && c[2] == "sink_name=linewave_music"
        && c[3].contains("monitor.channel-volumes='true'")));
    let cfg = mx.conf();
    assert!(cfg["routes"].get("firefox").is_none()); // gehört zu „browser“, nicht zu „sfx“
    assert_eq!(cfg["routes"]["spotify"], json!("sfx"));

    let Some(py) = run_python("main") else {
        eprintln!("Python 3 mit freestream fehlt – Vergleich übersprungen");
        return;
    };
    let py_cmds: Vec<Vec<String>> = serde_json::from_value(py["cmds"].clone()).unwrap();
    for (i, (a, b)) in cmds.iter().zip(&py_cmds).enumerate() {
        assert_eq!(a, b, "Befehl {i} unterschiedlich");
    }
    assert_eq!(cmds.len(), py_cmds.len());
    let pout = py["out"].as_object().unwrap();
    for (k, v) in &out {
        assert_eq!(v, &pout[k], "Ergebnis {k}");
    }
    let py_chans: Vec<Channel> = pout["channels"].as_array().unwrap().iter().filter_map(Channel::from_value).collect();
    assert_eq!(chans, py_chans);
    assert_eq!(Value::Object(cfg), py["config"], "config.json, Abschnitt mixer");
    let st = std::fs::read_to_string(dir.path().join("state/mixer.json")).ok().map(|t| serde_json::from_str(&t).unwrap());
    assert_eq!(without_created(st), without_created(Some(py["state"].clone())), "mixer.json");
}

/// Katalog wie im Python-Ablauf (eingebaute + zwei erfundene Plugins).
fn test_catalog() -> Vec<Plugin> {
    let mut cat = effects::builtin();
    cat.push(Plugin::from_value(&json!({"id": "ladspa:sc4m_1916.so:sc4m", "type": "ladspa",
        "plugin": "/usr/lib/ladspa/sc4m_1916.so", "label": "sc4m", "name": "SC4", "info": "", "inputs": ["Input"],
        "outputs": ["Output"], "controls": [{"name": "Ratio (1:n)", "min": 1, "max": 20, "default": 1},
                                            {"name": "Threshold level (dB)", "min": -30, "max": 0, "default": 0}]}))
        .unwrap());
    cat.push(Plugin::from_value(&json!({"id": "lv2:urn:x", "type": "lv2", "plugin": "urn:x", "label": "x",
        "name": "X", "info": "", "inputs": ["in_l", "in_r"], "outputs": ["out_l", "out_r"],
        "controls": [{"name": "gain", "min": 0, "max": 1, "default": 0.5}]})).unwrap());
    cat
}

#[test]
fn same_filter_chains_and_noise_configs_as_python() {
    let dir = tempfile::tempdir().unwrap();
    let (mx, _fake) = mixer(dir.path(), json!({}));
    let cat = test_catalog();
    let lk = |id: &str| cat.iter().find(|p| p.id == id).cloned();
    let chain: Vec<Effect> = vec![
        effects::new_effect(&lk("builtin:highpass").unwrap()),
        Effect::from_value(&json!({"plugin": "builtin:peaking", "name": "EQ", "enabled": true,
                                   "controls": {"Freq": 2500.5, "Q": 1.25, "Gain": -3, "Unknown": 1}})).unwrap(),
        Effect::from_value(&json!({"plugin": "builtin:lowpass", "name": "off", "enabled": false, "controls": {}})).unwrap(),
        Effect::from_value(&json!({"plugin": "ladspa:sc4m_1916.so:sc4m", "name": "Comp", "enabled": true,
                                   "controls": {"Ratio (1:n)": 3.0, "Threshold level (dB)": -20}})).unwrap(),
    ];
    let fx_monitor = effects::conf("music", "Music", "linewave_music.monitor", &chain, &lk, None).unwrap();
    assert!(fx_monitor.contains("target.object = \"linewave_music\"\n                stream.capture.sink = true"));
    assert!(fx_monitor.contains("{ type = builtin name = fx0l label = bq_highpass control = { \"Freq\" = 90 \"Q\" = 0.707 } }"));
    let fx_mic = effects::conf("mic", "Mic \"1\"", "alsa_input.mic", &chain[..2], &lk, None).unwrap();
    let mut chain2 = chain.clone();
    chain2.push(Effect::from_value(&json!({"plugin": "lv2:urn:x", "name": "X", "enabled": true,
                                            "controls": {"gain": 0.25}})).unwrap());
    let fx_stage = effects::conf("mic", "Mic", "alsa_input.mic", &chain2, &lk, Some(2)).unwrap();
    let ns = mx.noise().set(Some(true), Some("webrtc"), Some(73.6)).unwrap();
    assert_eq!(ns.strength, 74);
    assert!(mx.noise().set(None, Some("magic"), None).is_err());
    let rn = freestream_wave::noise::rnnoise_conf("alsa_input.usb-Elgato_Wave_3",
                                                  "/usr/lib/ladspa/librnnoise_ladspa.so", 42.0, "VAD Threshold (%)");
    let wr = freestream_wave::noise::webrtc_conf("alsa_input.usb-Elgato_Wave_3");
    let wm: Vec<String> = [(true, 120, true), (true, 80, false), (false, 80, true)].iter()
        .map(|(l, f, c)| freestream_wave::wavemic::filter_conf("alsa_input.wave", *l, *f, *c).unwrap()).collect();
    assert!(freestream_wave::wavemic::filter_conf("x", false, 80, false).is_err());
    mx.fx().set_chain("mic", &chain);
    assert_eq!(mx.fx().chain("mic"), chain);
    assert!(mx.fx().active("mic"));
    mx.set_linked("music", true);

    let Some(py) = run_python("noise") else {
        eprintln!("Python 3 mit freestream fehlt – Vergleich übersprungen");
        return;
    };
    let o = &py["out"];
    assert_eq!(o["fx_monitor"].as_str().unwrap(), fx_monitor);
    assert_eq!(o["fx_mic"].as_str().unwrap(), fx_mic);
    assert_eq!(o["fx_stage"].as_str().unwrap(), fx_stage);
    assert_eq!(o["rnnoise"].as_str().unwrap(), rn);
    assert_eq!(o["webrtc"].as_str().unwrap(), wr);
    for (i, w) in wm.iter().enumerate() {
        assert_eq!(o["wavemic"][i].as_str().unwrap(), w);
    }
    let py_chain: Vec<Effect> = o["chain"].as_array().unwrap().iter().filter_map(Effect::from_value).collect();
    assert_eq!(py_chain, chain);
    // config.json: gleiche Schlüssel und Werte (Zahlen als Zahl verglichen)
    let cfg = mx.conf();
    let pc = py["config"].as_object().unwrap();
    assert_eq!(cfg["noise"], pc["noise"]);
    assert_eq!(cfg["linked"], pc["linked"]);
    let ours: Vec<Effect> = cfg["effects"]["mic"].as_array().unwrap().iter().filter_map(Effect::from_value).collect();
    let theirs: Vec<Effect> = pc["effects"]["mic"].as_array().unwrap().iter().filter_map(Effect::from_value).collect();
    assert_eq!(ours, theirs);
}

/// Python schreibt config.json (Kanäle, Mixe, Zuordnungen, Szenen, Ducking, Effekte), Rust liest sie –
/// und umgekehrt.
#[test]
fn config_round_trip_with_python() {
    let Some(py) = python() else {
        eprintln!("Python 3 mit freestream fehlt – übersprungen");
        return;
    };
    let dir = tempfile::tempdir().unwrap();
    let code = r##"
import json
from freestream import mixer
from freestream.config import Config
mixer._run = lambda args: "1"
mixer.Mixer.is_active = lambda self: False
cfg = Config()
m = mixer.Mixer(cfg)
m.add_channel("Zweites Mikro", kind="input", source="alsa_input.usb", icon="mic", color="#ff0000", in_mixes=False)
m.add_mix("Chat für Discord", icon="chat", devices=["alsa_output.speakers"], virtual=True, exclude=["music"])
m.update_mix("monitor", name="Kopfhörer", devices=["alsa_output.usb"])
m.set_linked("music", True)
m.set_channel_routes("game", add=[("minecraft-launcher", "Minecraft")], remove=["steam"])
m.update_channel("mic", icon="wave", color="#00ff00", source="alsa_input.wave")
cfg.data["mixer"]["scenes"] = [{"id": "gaming", "name": "Gaming", "levels": {"music": {"monitor": [40, False]}}}]
cfg.data["mixer"]["ducking"] = {"enabled": True, "channels": ["music", "voice"], "amount_db": 18, "attack_ms": None}
cfg.save()
print(json.dumps({"mixes": m.mixes(), "channels": m.all_channels(), "routes": m.routes(),
                  "game_routes": m.channel_routes("game", known=["minecraft-launcher"])}))
"##;
    let out = Command::new(&py).args(["-c", code])
        .env("PYTHONPATH", repo_root())
        .env("FREESTREAM_LANG", "en")
        .env("XDG_CONFIG_HOME", dir.path().join("c"))
        .env("XDG_STATE_HOME", dir.path().join("s"))
        .env("XDG_DATA_HOME", dir.path().join("d"))
        .current_dir(repo_root())
        .output().unwrap();
    assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
    let want: Value = serde_json::from_slice(&out.stdout).unwrap();
    let cfg_path = dir.path().join("c/freestream/config.json");
    freestream_core::i18n::set_language("en");
    let fake = common::FakePactl::new();
    let mx = freestream_wave::Mixer::new(Box::new(freestream_wave::FileSettings::load(&cfg_path)),
                                         std::sync::Arc::new(fake), dir.path().join("rs")).without_sleep();
    let mixes: Vec<Value> = mx.mixes().iter().map(mix_json).collect();
    assert_eq!(Value::Array(mixes), want["mixes"]);
    let pyc: Vec<Channel> = want["channels"].as_array().unwrap().iter().filter_map(Channel::from_value).collect();
    assert_eq!(mx.all_channels(), pyc);
    let routes: serde_json::Map<String, Value> = mx.routes().into_iter().map(|(k, v)| (k, json!(v))).collect();
    assert_eq!(Value::Object(routes), want["routes"]);
    let gr: Vec<Value> = mx.channel_routes("game", &["minecraft-launcher".into()]).iter()
        .map(|r| json!({"key": r.key, "name": r.name, "own": r.own})).collect();
    assert_eq!(Value::Array(gr), want["game_routes"]);
    let d = freestream_wave::ducking::settings(&mx.conf());
    assert!(d.enabled && d.channels == ["music", "voice"] && d.amount_db == 18.0 && d.attack_ms == 150.0);
    assert_eq!(mx.scenes()[0].levels["music"]["monitor"], (40.0, false));

    // Rust ändert, Python liest
    mx.add_mix("Aufnahme", "rec", "#123456", &[], true, &["sfx".into()]).unwrap();
    mx.set_member("voice", "aufnahme", false).unwrap();
    mx.remove_mix("Chat für Discord").unwrap();
    mx.set_linked("music", false);
    let check = r#"
import json
from freestream import mixer
from freestream.config import Config
m = mixer.Mixer(Config())
print(json.dumps({"mixes": m.mixes(), "linked": m.is_linked("music"), "channels": m.all_channels()}))
"#;
    let out = Command::new(&py).args(["-c", check])
        .env("PYTHONPATH", repo_root())
        .env("FREESTREAM_LANG", "en")
        .env("XDG_CONFIG_HOME", dir.path().join("c"))
        .env("XDG_STATE_HOME", dir.path().join("s"))
        .current_dir(repo_root())
        .output().unwrap();
    assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
    let got: Value = serde_json::from_slice(&out.stdout).unwrap();
    let mixes: Vec<Value> = mx.mixes().iter().map(mix_json).collect();
    assert_eq!(got["mixes"], Value::Array(mixes));
    assert_eq!(got["linked"], json!(false));
    let pyc: Vec<Channel> = got["channels"].as_array().unwrap().iter().filter_map(Channel::from_value).collect();
    assert_eq!(mx.all_channels(), pyc);
}
