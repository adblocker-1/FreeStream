//! Wiedergabe von Audiodateien (Soundboard, Timer-Alarm) wie `system.SoundPlayer`: mpv, ffplay, pw-play oder
//! paplay im Hintergrund, je Schlüssel (Aktion) gemerkt, damit Stopp/Neustart/Schleife funktionieren.

use std::collections::HashMap;
use std::process::{Child, Command, Stdio};
use std::sync::{Mutex, OnceLock};

use crate::system::have;

fn procs() -> &'static Mutex<HashMap<String, Vec<Child>>> {
    static P: OnceLock<Mutex<HashMap<String, Vec<Child>>>> = OnceLock::new();
    P.get_or_init(Default::default)
}

/// Befehl zum Abspielen (Lautstärke 0–100, Ausgabegerät oder leer, Endlosschleife).
pub fn player_cmd(path: &str, volume: u32, output: &str, looping: bool, have: impl Fn(&str) -> bool) -> Option<Vec<String>> {
    let vol = volume.min(100);
    let mut c: Vec<String> = Vec::new();
    if have("mpv") {
        c.extend(["mpv", "--no-video", "--really-quiet"].map(String::from));
        c.push(format!("--volume={vol}"));
        if !output.is_empty() {
            c.push(format!("--audio-device=pipewire/{output}"));
        }
        if looping {
            c.push("--loop-file=inf".into());
        }
    } else if have("ffplay") {
        c.extend(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-volume"].map(String::from));
        c.push(vol.to_string());
        if looping {
            c.extend(["-loop", "0"].map(String::from));
        }
    } else if have("pw-play") {
        c.push("pw-play".into());
        c.push(format!("--volume={:.2}", vol as f32 / 100.0));
        if !output.is_empty() {
            c.extend(["--target".to_string(), output.to_string()]);
        }
    } else if have("paplay") {
        c.push("paplay".into());
        c.push(format!("--volume={}", vol * 65536 / 100));
        if !output.is_empty() {
            c.push(format!("--device={output}"));
        }
    } else {
        return None;
    }
    c.push(path.to_string());
    Some(c)
}

/// Beendete Wiedergaben abholen (keine Zombies).
fn reap(map: &mut HashMap<String, Vec<Child>>) {
    for v in map.values_mut() {
        v.retain_mut(|c| matches!(c.try_wait(), Ok(None)));
    }
    map.retain(|_, v| !v.is_empty());
}

/// Datei abspielen; false, wenn es keinen Player gibt oder er nicht startet.
pub fn play(key: &str, path: &str, volume: u32, output: &str, looping: bool) -> bool {
    let Some(cmd) = player_cmd(path, volume, output, looping, have) else {
        log::warn!("{}", freestream_core::tr!("No audio player found (install mpv, ffplay or pipewire-bin)"));
        return false;
    };
    match Command::new(&cmd[0]).args(&cmd[1..]).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn() {
        Ok(child) => {
            let mut map = procs().lock().unwrap();
            reap(&mut map);
            map.entry(key.to_string()).or_default().push(child);
            true
        }
        Err(e) => {
            log::warn!("{}", freestream_core::tr!("Audio playback failed: {error}", error = e));
            false
        }
    }
}

pub fn is_playing(key: &str) -> bool {
    let mut map = procs().lock().unwrap();
    reap(&mut map);
    map.contains_key(key)
}

/// Wiedergabe beenden (None = alle).
pub fn stop(key: Option<&str>) {
    let mut map = procs().lock().unwrap();
    let keys: Vec<String> = match key {
        Some(k) => vec![k.to_string()],
        None => map.keys().cloned().collect(),
    };
    for k in keys {
        for mut c in map.remove(&k).unwrap_or_default() {
            let _ = c.kill();
            let _ = c.wait();
        }
    }
}

/// Eindeutiger Schlüssel für eine Aktion (wie `id(self)` in Python).
pub fn new_key(prefix: &str) -> String {
    use std::sync::atomic::{AtomicU64, Ordering};
    static N: AtomicU64 = AtomicU64::new(1);
    format!("{prefix}-{}", N.fetch_add(1, Ordering::Relaxed))
}

/// Systemklingel, wenn es keine Alarmdatei gibt.
pub fn bell() {
    use std::io::Write;
    let mut out = std::io::stdout();
    let _ = out.write_all(b"\x07\x07\x07");
    let _ = out.flush();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn picks_player_and_output() {
        let only = |p: &'static str| move |x: &str| x == p;
        let c = player_cmd("/a.mp3", 80, "alsa_output.usb", false, only("pw-play")).unwrap();
        assert_eq!(c, vec!["pw-play", "--volume=0.80", "--target", "alsa_output.usb", "/a.mp3"]);
        let c = player_cmd("/a.mp3", 50, "sink1", true, only("paplay")).unwrap();
        assert_eq!(c, vec!["paplay", "--volume=32768", "--device=sink1", "/a.mp3"]);
        let c = player_cmd("/a.mp3", 150, "", true, only("mpv")).unwrap();
        assert_eq!(c, vec!["mpv", "--no-video", "--really-quiet", "--volume=100", "--loop-file=inf", "/a.mp3"]);
        assert!(player_cmd("/a.mp3", 50, "", false, |_| false).is_none());
    }
}
