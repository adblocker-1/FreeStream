//! Simuliertes pactl für die Tests (wie FakePactl in tests/test_waveapp.py und tests/py/scenario.py).
#![allow(dead_code)]

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use freestream_wave::pactl::{Block, Pactl};
use freestream_wave::{Error, FileSettings, Mixer};
use indexmap::IndexMap;

#[derive(Clone, Debug)]
pub struct Input {
    pub index: String,
    pub sink: String,
    pub volume: i64,
    pub mute: bool,
    pub props: BTreeMap<String, String>,
}

#[derive(Default)]
pub struct State {
    pub cmds: Vec<Vec<String>>,
    pub next_id: u32,
    pub sinks: Vec<(String, String, String)>,
    pub inputs: IndexMap<String, Input>,
    pub sources: Vec<(String, String, String)>,
    /// Befehle, die fehlschlagen sollen (erstes Wort)
    pub fail: Vec<String>,
}

#[derive(Clone)]
pub struct FakePactl(pub Arc<Mutex<State>>);

fn input(index: &str, app: &str, bin: &str) -> Input {
    let mut props = BTreeMap::new();
    props.insert("application.name".to_string(), app.to_string());
    props.insert("application.process.binary".to_string(), bin.to_string());
    Input { index: index.into(), sink: "1".into(), volume: 80, mute: false, props }
}

impl FakePactl {
    pub fn new() -> Self {
        let mut st = State { next_id: 100, ..Default::default() };
        st.sinks = vec![("1".into(), "alsa_output.headphones".into(), "Kopfhörer".into()),
                        ("2".into(), "alsa_output.speakers".into(), "Lautsprecher".into())];
        for i in [input("7", "Spotify", "spotify"), input("8", "Mein Spiel", "game.exe"),
                  input("9", "Firefox", "firefox-esr")] {
            st.inputs.insert(i.index.clone(), i);
        }
        st.sources = vec![("50".into(), "alsa_input.usb-capture".into(), "USB Capture".into()),
                          ("51".into(), "alsa_output.headphones.monitor".into(), "Monitor".into())];
        Self(Arc::new(Mutex::new(st)))
    }

    pub fn cmds(&self) -> Vec<Vec<String>> {
        self.0.lock().unwrap().cmds.clone()
    }

    pub fn input_of(&self, lid: &str) -> Option<Input> {
        self.0.lock().unwrap().inputs.values().find(|b| b.props.get("linewave.loop").map(String::as_str) == Some(lid))
            .cloned()
    }

    /// Name des Sinks, auf den ein Loopback/Stream spielt.
    pub fn sink_of(&self, lid: &str) -> Option<String> {
        let b = self.input_of(lid)?;
        let st = self.0.lock().unwrap();
        st.sinks.iter().find(|s| s.0 == b.sink).map(|s| s.1.clone())
    }

    pub fn has_sink(&self, name: &str) -> bool {
        self.0.lock().unwrap().sinks.iter().any(|s| s.1 == name)
    }
}

impl Default for FakePactl {
    fn default() -> Self {
        Self::new()
    }
}

impl Pactl for FakePactl {
    fn run(&self, args: &[String]) -> Result<String, Error> {
        let mut st = self.0.lock().unwrap();
        st.cmds.push(args.to_vec());
        if st.fail.iter().any(|f| *f == args[0]) {
            return Err(Error("Failure: No such entity".into()));
        }
        let sink_idx = |st: &State, name: &str| st.sinks.iter().find(|s| s.1 == name).map(|s| s.0.clone())
            .unwrap_or_else(|| "0".into());
        match args[0].as_str() {
            "load-module" => {
                st.next_id += 1;
                let id = st.next_id.to_string();
                let kv: BTreeMap<&str, &str> = args[2..].iter().filter_map(|a| a.split_once('=')).collect();
                if args[1] == "module-null-sink" {
                    st.sinks.push((id.clone(), kv["sink_name"].to_string(), String::new()));
                }
                let re = regex::Regex::new(r"linewave\.loop='([^']+)'").unwrap();
                let name = args.iter().filter(|a| a.starts_with("sink_input_properties="))
                    .find_map(|a| re.captures(a).map(|c| c[1].to_string()));
                if let Some(name) = name {
                    let sink = sink_idx(&st, kv.get("sink").copied().unwrap_or(""));
                    let mut props = BTreeMap::new();
                    props.insert("linewave.loop".to_string(), name);
                    props.insert("media.name".to_string(), "lesbar".to_string());
                    st.inputs.insert(id.clone(), Input { index: id.clone(), sink, volume: 100, mute: false, props });
                }
                return Ok(id);
            }
            "get-default-sink" => return Ok("alsa_output.headphones".into()),
            "get-default-source" => return Ok("alsa_input.mic".into()),
            "set-sink-input-volume" => {
                let v: i64 = args[2].trim_end_matches('%').parse().unwrap();
                match st.inputs.get_mut(&args[1]) {
                    Some(b) => b.volume = v,
                    None => return Err(Error("Failure: No such entity".into())),
                }
            }
            "set-sink-input-mute" => {
                let Some(b) = st.inputs.get_mut(&args[1]) else { return Err(Error("Failure: No such entity".into())) };
                b.mute = if args[2] == "toggle" { !b.mute } else { args[2] == "1" };
            }
            "move-sink-input" => {
                let s = sink_idx(&st, &args[2]);
                if let Some(b) = st.inputs.get_mut(&args[1]) {
                    b.sink = s;
                }
            }
            "unload-module" => {
                let gone: Vec<String> = st.sinks.iter().filter(|x| x.0 == args[1]).map(|x| x.0.clone()).collect();
                st.sinks.retain(|x| x.0 != args[1]);
                st.inputs.shift_remove(&args[1]);
                for b in st.inputs.values_mut() {
                    // wie PipeWire: Streams eines verschwundenen Sinks landen auf dem Standardgerät
                    if gone.contains(&b.sink) {
                        b.sink = "1".into();
                    }
                }
            }
            _ => {}
        }
        Ok(String::new())
    }

    fn list(&self, _what: &str) -> String {
        String::new()
    }

    fn blocks(&self, what: &str) -> Vec<Block> {
        let st = self.0.lock().unwrap();
        match what {
            "sink-inputs" => st.inputs.values().map(|b| Block {
                index: b.index.clone(),
                sink: Some(b.sink.clone()),
                volume: Some(b.volume),
                mute: b.mute,
                props: b.props.clone(),
                ..Default::default()
            }).collect(),
            "sinks" => st.sinks.iter().map(|s| Block {
                index: s.0.clone(), name: Some(s.1.clone()), description: Some(s.2.clone()), ..Default::default()
            }).collect(),
            "sources" => st.sources.iter().map(|s| Block {
                index: s.0.clone(), name: Some(s.1.clone()), description: Some(s.2.clone()), ..Default::default()
            }).collect(),
            _ => Vec::new(),
        }
    }
}

/// Mixer mit simuliertem pactl, config.json und Zustand in einem eigenen Ordner.
pub fn mixer(dir: &std::path::Path, config: serde_json::Value) -> (Arc<Mixer>, FakePactl) {
    freestream_core::i18n::set_language("en");
    let cfg_path: PathBuf = dir.join("config").join("config.json");
    std::fs::create_dir_all(cfg_path.parent().unwrap()).unwrap();
    std::fs::write(&cfg_path, config.to_string()).unwrap();
    let fake = FakePactl::new();
    let m = Mixer::new(Box::new(FileSettings::load(&cfg_path)), Arc::new(fake.clone()), dir.join("state"))
        .without_sleep();
    (Arc::new(m), fake)
}

/// Wurzel des Repositorys (für die Python-Version).
pub fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..").canonicalize().unwrap()
}

/// Python 3 mit importierbarem freestream-Paket, sonst None (dann werden Vergleiche übersprungen).
pub fn python() -> Option<String> {
    for exe in ["python3.12", "python3"] {
        let ok = std::process::Command::new(exe)
            .args(["-c", "import freestream.mixer, freestream.effects, freestream.noise, freestream.wavemic"])
            .env("PYTHONPATH", repo_root())
            .current_dir(repo_root())
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false);
        if ok {
            return Some(exe.to_string());
        }
    }
    None
}
