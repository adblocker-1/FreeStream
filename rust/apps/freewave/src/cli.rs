//! Kommandozeile von FreeWave – wie `wave_main`, `cmd_mixer`, `_cmd_scene`, `cmd_wave` und `cmd_noise` in
//! `freestream/__main__.py`:
//!
//! ```text
//! freewave                         Fenster öffnen (läuft es schon: nach vorne holen)
//! freewave --hidden                ohne Fenster im Hintergrund (Autostart)
//! freewave --quit                  laufendes FreeWave beenden
//! freewave mixer [status|setup|remove|set|mute|assign|output|add|delete|scene|master|listen] …
//! freewave mic   [status|gain|mute|lowcut|clipguard|leds|flip|headphone|monitor|noise] …
//! ```

use std::sync::Arc;

use freestream_core::i18n::pgettext;
use freestream_core::{paths, tr};
use freestream_wave::host::running_instance;
use freestream_wave::mixer::Mute;
use freestream_wave::noise;
use freestream_wave::pactl::SystemPactl;
use freestream_wave::wavemic::{self, WaveMic, WAVE_APP_FILE};
use freestream_wave::Mixer;
use serde_json::{json, Value};

pub const MIXER_ACTIONS: [&str; 12] =
    ["status", "setup", "remove", "set", "mute", "assign", "output", "add", "delete", "scene", "master", "listen"];
pub const WAVE_ACTIONS: [&str; 10] =
    ["status", "gain", "mute", "lowcut", "clipguard", "leds", "flip", "headphone", "monitor", "noise"];
pub const ON_WORDS: [&str; 5] = ["on", "an", "1", "true", "ein"];
const OFF_WORDS: [&str; 5] = ["off", "aus", "0", "false", ""];

/// Was die Kommandozeile verlangt.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Cmd {
    /// Fenster (bzw. Dienst mit `hidden`)
    App { hidden: bool, quit: bool },
    Mixer { action: String, args: Vec<String> },
    Wave { action: String, args: Vec<String> },
    Version,
    Help,
    /// unbekannter Befehl oder Aktion (Text für die Fehlermeldung)
    Bad(String),
}

/// Argumente (ohne Programmnamen) auswerten – wie `wave_main`: ohne Befehl Fenster, `mixer …`, `mic …`.
pub fn parse(argv: &[String]) -> Cmd {
    let flags: Vec<&str> = argv.iter().map(String::as_str).filter(|a| a.starts_with('-')).collect();
    let rest: Vec<String> = argv.iter().filter(|a| !a.starts_with('-') || a.parse::<f64>().is_ok()).cloned().collect();
    if flags.iter().any(|f| *f == "--version") {
        return Cmd::Version;
    }
    if flags.iter().any(|f| *f == "-h" || *f == "--help") && rest.is_empty() {
        return Cmd::Help;
    }
    let Some(cmd) = rest.first().map(String::as_str) else {
        return Cmd::App { hidden: flags.contains(&"--hidden"), quit: flags.contains(&"--quit") };
    };
    let action = rest.get(1).cloned().unwrap_or_else(|| "status".into());
    let args = rest.iter().skip(2).cloned().collect();
    match cmd {
        "mixer" if MIXER_ACTIONS.contains(&action.as_str()) => Cmd::Mixer { action, args },
        "mic" | "wave" if WAVE_ACTIONS.contains(&action.as_str()) => Cmd::Wave { action, args },
        "mixer" | "mic" | "wave" => Cmd::Bad(action),
        "quit" => Cmd::App { hidden: false, quit: true },
        "freewave" | "open" | "gui" => Cmd::App { hidden: flags.contains(&"--hidden"), quit: flags.contains(&"--quit") },
        "help" => Cmd::Help,
        "version" => Cmd::Version,
        other => Cmd::Bad(other.to_string()),
    }
}

pub fn usage() -> String {
    let mut out = format!("FreeWave {}\n\n", freestream_core::VERSION);
    let rows = [
        ("freewave", tr!("open FreeWave: mixer and microphone (separate program)")),
        ("freewave --hidden", tr!("start hidden (autostart)")),
        ("freewave --quit", tr!("quit the running FreeWave")),
        ("freewave mixer …", tr!("audio mixer (Wave Link replacement): status, setup, remove, set, mute, assign, output")),
        ("", tr!("set <channel> <mix|both> <percent> · mute <channel> <mix> · master <channel> <percent> · listen <mix> · assign <program> <channel> · output [device] · add <name> [input device] · delete <channel>")),
        ("", tr!("Scenes: scene list | save <name> | apply <name> | rename <name> <new name> | delete <name>")),
        ("freewave mic …", tr!("microphone: status, gain, mute, lowcut, clipguard, leds, flip, headphone, monitor, noise")),
        ("", tr!("gain <percent> · lowcut on|off [80|120] · clipguard on|off · leds on|off · flip on|off · headphone <dB −60…0> · monitor <0…100> · noise on|off|status|strength <0…95>|backend auto|rnnoise|webrtc")),
    ];
    for (cmd, text) in rows {
        out.push_str(&format!("  {cmd:<20} {text}\n"));
    }
    out
}

/// FreeWave die geänderte Konfiguration neu laden lassen (falls es läuft).
pub fn signal_wave_app() -> bool {
    match running_instance(&paths::state_dir(), WAVE_APP_FILE) {
        // SAFETY: kill mit gültiger Pid und Signal
        Some(pid) => unsafe { libc::kill(pid as libc::pid_t, libc::SIGHUP) == 0 },
        None => false,
    }
}

/// Abschnitt `key` einer JSON-Map (wird angelegt, wenn er fehlt oder kein Objekt ist).
pub fn obj<'a>(m: &'a mut serde_json::Map<String, Value>, key: &str) -> &'a mut serde_json::Map<String, Value> {
    let v = m.entry(key.to_string()).or_insert_with(|| json!({}));
    if !v.is_object() {
        *v = json!({});
    }
    v.as_object_mut().unwrap()
}

fn system_mixer() -> Mixer {
    Mixer::system()
}

fn on_off(on: bool) -> String {
    pgettext("state", if on { "on" } else { "off" })
}

fn muted_suffix(mute: bool) -> String {
    if mute {
        pgettext("state", " (muted)")
    } else {
        String::new()
    }
}

fn error(e: impl std::fmt::Display) -> i32 {
    println!("{}", tr!("Error: {error}", error = e));
    1
}

/// `freewave mixer …`; gibt den Rückgabewert des Programms zurück.
pub fn cmd_mixer(action: &str, args: &[String]) -> i32 {
    let mx = system_mixer();
    let need = |n: usize| -> Result<(), String> {
        if args.len() < n {
            Err(tr!("set <channel> <mix|both> <percent> · mute <channel> <mix> · master <channel> <percent> · listen <mix> · assign <program> <channel> · output [device] · add <name> [input device] · delete <channel>"))
        } else {
            Ok(())
        }
    };
    let r: Result<i32, String> = (|| {
        match action {
            "setup" => {
                mx.setup().map_err(|e| e.0)?;
                mx.with_settings_mut(|s| {
                    obj(s.data_mut(), "mixer").insert("enabled".into(), json!(true));
                    s.save()
                }).map_err(|e| e.to_string())?;
                signal_wave_app();
                println!("{}", tr!("Mixer set up and enabled at startup. In OBS, choose “Monitor of FreeWave Stream Mix” as the audio source."));
            }
            "remove" => {
                mx.teardown(true);
                mx.with_settings_mut(|s| {
                    obj(s.data_mut(), "mixer").insert("enabled".into(), json!(false));
                    s.save()
                }).map_err(|e| e.to_string())?;
                println!("{}", tr!("Mixer removed"));
            }
            "set" => {
                need(3)?;
                let vol: i64 = args[2].trim_end_matches('%').parse().map_err(|e: std::num::ParseIntError| e.to_string())?;
                mx.set_volume(&args[0], &args[1], vol).map_err(|e| e.0)?;
            }
            "mute" => {
                need(2)?;
                mx.set_mute(&args[0], &args[1], Mute::Toggle, false).map_err(|e| e.0)?;
            }
            "assign" => {
                need(2)?;
                let (app, ch) = (&args[0], &args[1]);
                mx.with_settings_mut(|s| {
                    let m = obj(s.data_mut(), "mixer");
                    obj(m, "routes").insert(app.to_lowercase(), json!(ch));
                    s.save()
                }).map_err(|e| e.to_string())?;
                let n = mx.assign(app, ch).map_err(|e| e.0)?;
                println!("{}", tr!("{app} → {channel} ({count} running streams moved)", app = app, channel = ch, count = n));
            }
            "output" => {
                let dev = args.first().map(String::as_str).unwrap_or("next");
                println!("{}", tr!("Monitor output: {output}", output = mx.set_monitor_output(dev).map_err(|e| e.0)?));
            }
            "add" => {
                let Some(name) = args.first() else {
                    println!("{}", tr!("Specify a name: freewave mixer add <name> [input device]"));
                    return Ok(1);
                };
                let c = match args.get(1) {
                    Some(dev) => {
                        let ins = mx.inputs();
                        let Some(src) = ins.iter().find(|d| d.name == *dev || d.description.to_lowercase().contains(&dev.to_lowercase())) else {
                            let avail: Vec<&str> = ins.iter().map(|d| d.description.as_str()).collect();
                            println!("{}", tr!("Input device “{device}” not found. Available: {available}", device = dev, available = avail.join(", ")));
                            return Ok(1);
                        };
                        mx.add_channel(name, "input", &src.name, "", "", true)
                    }
                    None => mx.add_channel(name, "apps", "", "", "", true),
                }.map_err(|e| e.0)?;
                signal_wave_app();
                println!("{}", tr!("Channel created: {name} ({id})", name = c.name, id = c.id));
            }
            "delete" => {
                need(1)?;
                mx.remove_channel(&args[0]).map_err(|e| e.0)?;
                signal_wave_app();
                println!("{}", tr!("Channel deleted"));
            }
            "scene" => return Ok(cmd_scene(&mx, args)),
            "master" => {
                need(2)?;
                let vol: i64 = args[1].trim_end_matches('%').parse().map_err(|e: std::num::ParseIntError| e.to_string())?;
                mx.set_master(&args[0], vol).map_err(|e| e.0)?;
            }
            "listen" => {
                let mid = mx.listen(args.first().map(String::as_str).unwrap_or("monitor")).map_err(|e| e.0)?;
                let name = mx.mix(&mid).map(|m| m.name).unwrap_or(mid);
                println!("{}", tr!("Listening to “{mix}”", mix = name));
            }
            _ => {
                if !mx.is_active() {
                    println!("{}", tr!("Mixer not set up – 'freewave mixer setup'"));
                    return Ok(1);
                }
                println!("{}", tr!("Monitor output: {output}", output = mx.monitor_output_name()));
                let masters = mx.masters();
                for c in mx.all_channels() {
                    let mut cells: Vec<String> = masters.get(&c.id).map(|l| vec![format!("master: {}%", l.volume)]).unwrap_or_default();
                    for m in mx.channel_mixes(&c.id) {
                        let (vol, mute) = mx.get(&c.id, &m);
                        let shown = match vol {
                            None => "–".to_string(),
                            Some(_) if mute => pgettext("state", "muted"),
                            Some(v) => format!("{v}%"),
                        };
                        cells.push(format!("{m}: {shown}"));
                    }
                    println!("  {:<8} {:<12} {}", c.id, c.name, cells.join("   "));
                }
            }
        }
        Ok(0)
    })();
    r.unwrap_or_else(error)
}

/// `freewave mixer scene list|save NAME|apply NAME|rename NAME NEW|delete NAME`
pub fn cmd_scene(mx: &Mixer, argv: &[String]) -> i32 {
    let sub = argv.first().map(|s| s.to_lowercase()).unwrap_or_else(|| "list".into());
    let rest = argv.get(1..).unwrap_or(&[]);
    if sub == "list" {
        let scenes = mx.scenes();
        if scenes.is_empty() {
            println!("{}", tr!("No scenes saved yet – 'freewave mixer scene save <name>'"));
            return 0;
        }
        let active = mx.active_scene(None);
        for s in scenes {
            let mark = if active.as_ref().is_some_and(|a| a.id == s.id) { "*" } else { " " };
            println!("{mark} {:<20} {}", s.id, s.name);
        }
        return 0;
    }
    if !["save", "apply", "delete", "rename"].contains(&sub.as_str()) || rest.is_empty() || (sub == "rename" && rest.len() < 2) {
        println!("{}", tr!("Usage: freewave mixer scene list | save <name> | apply <name> | rename <name> <new name> | delete <name>"));
        return 1;
    }
    let all = rest.join(" ");
    let r = match sub.as_str() {
        "save" => mx.save_scene(&all).map(|s| tr!("Scene “{name}” saved", name = s.name)),
        "apply" => mx.apply_scene(&all).map(|s| tr!("Scene “{name}” applied", name = s.name)),
        "rename" => mx.rename_scene(&rest[0], &rest[1..].join(" ")).map(|s| tr!("Scene renamed to “{name}”", name = s.name)),
        _ => mx.delete_scene(&all).map(|s| tr!("Scene “{name}” deleted", name = s.name)),
    };
    match r {
        Ok(text) => {
            println!("{text}");
            signal_wave_app();
            0
        }
        Err(e) => error(e),
    }
}

fn is_on(arg: Option<&String>, default: &str) -> bool {
    ON_WORDS.contains(&arg.map(|s| s.to_lowercase()).unwrap_or_else(|| default.into()).as_str())
}

/// `freewave mic noise on|off|status|strength <0…95>|backend auto|rnnoise|webrtc`
pub fn cmd_noise(args: &[String]) -> i32 {
    let mx = system_mixer();
    let ns = mx.noise();
    let sub = args.first().map(|s| s.to_lowercase()).unwrap_or_else(|| "status".into());
    let r: Result<i32, String> = (|| {
        if ON_WORDS.contains(&sub.as_str()) || OFF_WORDS[..4].contains(&sub.as_str()) {
            let on = ON_WORDS.contains(&sub.as_str());
            if on && noise::available().is_empty() {
                println!("{}", noise::install_hint());
                return Ok(1);
            }
            ns.set(Some(on), None, None).map_err(|e| e.0)?;
            ns.apply().map_err(|e| e.0)?;
        } else if sub == "strength" {
            let v: f64 = args.get(1).ok_or("strength <0…95>")?.parse().map_err(|e: std::num::ParseFloatError| e.to_string())?;
            ns.set_strength(v).map_err(|e| e.0)?;
        } else if sub == "backend" {
            let b = args.get(1).ok_or("backend auto|rnnoise|webrtc")?.to_lowercase();
            ns.set(None, Some(&b), None).map_err(|e| e.0)?;
            if ns.enabled() {
                ns.apply().map_err(|e| e.0)?;
            }
        } else if sub != "status" {
            println!("{}", tr!("Unknown command: {name}", name = sub));
            return Ok(1);
        }
        let st = ns.status();
        let backend = st.backend.clone().unwrap_or_default();
        let state = if !st.enabled {
            on_off(false)
        } else if st.running {
            tr!("on ({backend}, active)", backend = noise::backend_name(&backend))
        } else if st.available.is_empty() {
            tr!("on – not available")
        } else {
            tr!("on – starts as soon as the mixer is set up (freewave mixer setup)")
        };
        println!("{}", tr!("Noise suppression: {state}", state = state));
        if !st.available.is_empty() {
            let names: Vec<String> = st.available.iter().map(|b| noise::backend_name(b)).collect();
            println!("{}", tr!("Available: {names}", names = names.join(", ")));
            if backend == "rnnoise" {
                println!("{}", tr!("Strength: {value} % (VAD threshold)", value = st.strength));
            }
        } else {
            println!("{}", st.hint);
        }
        Ok(0)
    })();
    r.unwrap_or_else(error)
}

/// `freewave mic …` (Wave-Mikrofon).
pub fn cmd_wave(action: &str, args: &[String]) -> i32 {
    if action == "noise" {
        return cmd_noise(args);
    }
    let conf = freestream_core::config::read_file().ok().and_then(|d| d.get("wave_mic").and_then(Value::as_object).cloned());
    let mut mic = WaveMic::new(conf.as_ref(), Arc::new(SystemPactl), paths::state_dir());
    let on = is_on(args.first(), "on");
    let num = |i: usize| -> Result<f64, String> {
        args.get(i).ok_or_else(|| tr!("Error: {error}", error = "?"))?.parse::<f64>().map_err(|e| e.to_string())
    };
    let r: Result<i32, String> = (|| {
        match action {
            "lowcut" | "clipguard" => {
                if mic.hw().is_some() {
                    mic.hw_set(action, if on { 1.0 } else { 0.0 }).map_err(|e| e.0)?;
                    println!("{}", tr!("{setting}: {state} (directly on the Wave:3)", setting = action, state = on_off(on)));
                    return Ok(0);
                }
                let mut kw: Vec<(&str, Value)> = vec![(action, json!(on))];
                if action == "lowcut" && args.len() > 1 {
                    kw.push(("lowcut_freq", json!(num(1)? as i64)));
                }
                mic.set(&kw).map_err(|e| e.0)?;
                if running_instance(&paths::state_dir(), WAVE_APP_FILE).is_some() {
                    println!("{}", tr!("{setting}: {state} (software filter in FreeWave)", setting = action, state = on_off(on)));
                } else {
                    println!("{}", tr!("{setting}: {state} – saved; the software filter runs as soon as FreeWave is started (freewave)", setting = action, state = on_off(on)));
                }
                if action == "clipguard" && on && !wavemic::limiter_available() {
                    println!("{}", tr!("Note: Clipguard needs the swh-plugins package"));
                }
            }
            "leds" => {
                mic.hw_set("leds_off", if on { 0.0 } else { 1.0 }).map_err(|e| e.0)?;
            }
            "flip" => {
                mic.hw_set("leds_flip", if on { 1.0 } else { 0.0 }).map_err(|e| e.0)?;
            }
            "headphone" => {
                mic.hw_set("headphone_volume", num(0)?).map_err(|e| e.0)?;
            }
            "monitor" => {
                mic.hw_set("direct_monitor", num(0)?).map_err(|e| e.0)?;
            }
            "gain" => {
                mic.set_gain(num(0)?.trunc()).map_err(|e| e.0)?;
            }
            "mute" => {
                let arg = args.first().map(|s| s.to_lowercase()).unwrap_or_else(|| "toggle".into());
                let mode = if arg == "toggle" { None } else { Some(on) };
                if let Some(muted) = mic.set_mute(mode).map_err(|e| e.0)? {
                    println!("{}", if muted { tr!("Microphone muted") } else { tr!("Microphone unmuted") });
                }
            }
            _ => {
                let st = mic.status();
                if st.source.is_none() && !st.hardware {
                    println!("{}", tr!("No Elgato Wave microphone found"));
                    return Ok(1);
                }
                println!("{}", tr!("Microphone: {source}", source = st.source.clone().unwrap_or_else(|| "–".into())));
                let f = |k: &str| st.fields.get(k).map(|v| v.as_f64()).unwrap_or(0.0);
                let b = |k: &str| st.fields.get(k).is_some_and(|v| v.as_bool());
                if st.hardware {
                    println!("{}", tr!("Wave:3 (firmware API {api}) – settings stored directly in the microphone", api = st.api));
                    println!("Gain:       {:.1} dB{}", f("gain"), muted_suffix(b("mute")));
                    println!("{}", tr!("Low cut:    {state}", state = on_off(b("lowcut"))));
                    println!("Clipguard:  {}", on_off(b("clipguard")));
                    println!("{}", tr!("Headphones: {volume} dB{muted}", volume = format!("{:.1}", f("headphone_volume")),
                                       muted = muted_suffix(b("headphone_mute"))));
                    println!("{}", tr!("Monitoring: {percent} % microphone", percent = format!("{:.0}", f("direct_monitor"))));
                    let flip = if st.fields.contains_key("leds_flip") {
                        let answer = pgettext("answer", if b("leds_flip") { "yes" } else { "no" });
                        tr!(", flipped: {answer}", answer = answer)
                    } else {
                        String::new()
                    };
                    println!("LEDs:       {}{flip}", on_off(!b("leds_off")));
                } else {
                    if !st.error.is_empty() {
                        println!("{}", tr!("Cannot control the Wave:3: {error}", error = st.error));
                    }
                    let (vol, mute) = mic.gain().unwrap_or((0, false));
                    let s = mic.mic_settings();
                    println!("Gain:       {vol}%{}", muted_suffix(mute));
                    let lowcut = if s.lowcut { tr!("{freq} Hz (software)", freq = s.lowcut_freq) } else { on_off(false) };
                    println!("{}", tr!("Low cut:    {state}", state = lowcut));
                    println!("Clipguard:  {}", if s.clipguard { tr!("on (software)") } else { on_off(false) });
                    println!("{}", tr!("LEDs:       only controllable on the Wave:3"));
                }
            }
        }
        Ok(0)
    })();
    r.unwrap_or_else(error)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn a(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn parses_app_flags() {
        assert_eq!(parse(&[]), Cmd::App { hidden: false, quit: false });
        assert_eq!(parse(&a(&["--hidden"])), Cmd::App { hidden: true, quit: false });
        assert_eq!(parse(&a(&["--quit"])), Cmd::App { hidden: false, quit: true });
        assert_eq!(parse(&a(&["--hidden", "--no-tray"])), Cmd::App { hidden: true, quit: false });
        assert_eq!(parse(&a(&["quit"])), Cmd::App { hidden: false, quit: true });
        assert_eq!(parse(&a(&["--version"])), Cmd::Version);
        assert_eq!(parse(&a(&["--help"])), Cmd::Help);
    }

    #[test]
    fn parses_mixer_commands() {
        assert_eq!(parse(&a(&["mixer"])), Cmd::Mixer { action: "status".into(), args: vec![] });
        assert_eq!(parse(&a(&["mixer", "set", "music", "both", "40"])),
                   Cmd::Mixer { action: "set".into(), args: a(&["music", "both", "40"]) });
        assert_eq!(parse(&a(&["-v", "mixer", "scene", "save", "Talk", "show"])),
                   Cmd::Mixer { action: "scene".into(), args: a(&["save", "Talk", "show"]) });
        assert_eq!(parse(&a(&["mixer", "bogus"])), Cmd::Bad("bogus".into()));
        for act in MIXER_ACTIONS {
            assert!(matches!(parse(&a(&["mixer", act])), Cmd::Mixer { .. }));
        }
    }

    #[test]
    fn parses_mic_commands() {
        assert_eq!(parse(&a(&["mic"])), Cmd::Wave { action: "status".into(), args: vec![] });
        assert_eq!(parse(&a(&["wave", "lowcut", "on", "120"])), Cmd::Wave { action: "lowcut".into(), args: a(&["on", "120"]) });
        // negative Zahlen sind Werte, keine Schalter
        assert_eq!(parse(&a(&["mic", "headphone", "-20"])), Cmd::Wave { action: "headphone".into(), args: a(&["-20"]) });
        assert_eq!(parse(&a(&["mic", "noise", "strength", "40"])),
                   Cmd::Wave { action: "noise".into(), args: a(&["strength", "40"]) });
        assert_eq!(parse(&a(&["mic", "laser"])), Cmd::Bad("laser".into()));
        assert_eq!(parse(&a(&["frobnicate"])), Cmd::Bad("frobnicate".into()));
    }

    #[test]
    fn on_words() {
        assert!(is_on(Some(&"EIN".to_string()), "on"));
        assert!(!is_on(Some(&"off".to_string()), "on"));
        assert!(is_on(None, "on"));
        assert!(usage().contains("freewave mixer"));
    }
}
