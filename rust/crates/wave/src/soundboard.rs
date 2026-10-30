//! Soundboard von FreeWave: Klänge in `config.json` (Schlüssel `"soundboard"`, `"soundboard_play"`,
//! `"soundboard_monitor"`, `"soundboard_volume"` – dieselben wie in der Python-Version), Dateien als Kopie
//! im Datenordner (`sounds/`), Wiedergabe über die Mix-Sinks von FreeWave mit `pw-play --target <Sink>`
//! oder `paplay --device=<Sink>` als Kindprozess. Stopp beendet den Prozess, eine Schleife startet ihn neu.
//!
//! Gespeichert wird:
//! ```json
//! "soundboard": [{"id": "…", "name": "Applaus", "file": "/…/sounds/applaus.wav", "color": "#ec4899",
//!                 "volume": 100, "loop": false}],
//! "soundboard_play": "stream",      // "" = nicht ausgewählt, Mix-id oder "all" = alle Mixe
//! "soundboard_monitor": "monitor",  // "" = nicht ausgewählt oder Mix-id
//! "soundboard_volume": 80           // 0 … 100
//! ```

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use serde_json::{json, Map, Value};

use crate::mixer::{Mix, Mixer, LOOP_PROP};

pub const KEY_SOUNDS: &str = "soundboard";
pub const KEY_PLAY: &str = "soundboard_play";
pub const KEY_MONITOR: &str = "soundboard_monitor";
pub const KEY_VOLUME: &str = "soundboard_volume";
/// Wert von `soundboard_play` für „alle Mixe“
pub const ALL_MIXES: &str = "all";
pub const DEFAULT_VOLUME: i64 = 80;
/// Dateiendungen im Dateidialog
pub const EXTENSIONS: [&str; 6] = ["wav", "mp3", "ogg", "oga", "flac", "opus"];
/// Farben neuer Klänge (reihum)
pub const COLORS: [&str; 8] = ["#3d9bff", "#ec4899", "#22c55e", "#f59e0b", "#a855f7", "#36c5e8", "#f0484f", "#64748b"];

/// Ein Klang auf dem Soundboard.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Sound {
    pub id: String,
    pub name: String,
    pub file: String,
    pub color: String,
    /// 0 … 100
    pub volume: i64,
    pub looping: bool,
}

impl Sound {
    pub fn from_value(v: &Value) -> Option<Self> {
        let o = v.as_object()?;
        let s = |k: &str| o.get(k).and_then(Value::as_str).unwrap_or("").to_string();
        let id = s("id");
        if id.is_empty() {
            return None;
        }
        Some(Self {
            id,
            name: s("name"),
            file: s("file"),
            color: s("color"),
            volume: o.get("volume").and_then(Value::as_f64).map(|v| v.round() as i64).unwrap_or(100).clamp(0, 100),
            looping: o.get("loop").is_some_and(crate::mixer::truthy),
        })
    }

    pub fn to_value(&self) -> Value {
        json!({"id": self.id, "name": self.name, "file": self.file, "color": self.color, "volume": self.volume,
               "loop": self.looping})
    }
}

/// Alle Einstellungen des Soundboards.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Board {
    pub sounds: Vec<Sound>,
    pub play: String,
    pub monitor: String,
    pub volume: i64,
}

impl Default for Board {
    fn default() -> Self {
        Self { sounds: Vec::new(), play: String::new(), monitor: String::new(), volume: DEFAULT_VOLUME }
    }
}

impl Board {
    /// Aus der ganzen config.json lesen.
    pub fn from_conf(conf: &Map<String, Value>) -> Self {
        let s = |k: &str| conf.get(k).and_then(Value::as_str).unwrap_or("").to_string();
        Self {
            sounds: conf.get(KEY_SOUNDS).and_then(Value::as_array).into_iter().flatten().filter_map(Sound::from_value)
                .collect(),
            play: s(KEY_PLAY),
            monitor: s(KEY_MONITOR),
            volume: conf.get(KEY_VOLUME).and_then(Value::as_f64).map(|v| v.round() as i64).unwrap_or(DEFAULT_VOLUME)
                .clamp(0, 100),
        }
    }

    /// In die config.json schreiben (nur die Soundboard-Schlüssel).
    pub fn write(&self, conf: &mut Map<String, Value>) {
        conf.insert(KEY_SOUNDS.into(), Value::Array(self.sounds.iter().map(Sound::to_value).collect()));
        conf.insert(KEY_PLAY.into(), json!(self.play));
        conf.insert(KEY_MONITOR.into(), json!(self.monitor));
        conf.insert(KEY_VOLUME.into(), json!(self.volume));
    }

    pub fn sound(&self, id: &str) -> Option<&Sound> {
        self.sounds.iter().find(|s| s.id == id)
    }

    pub fn sound_mut(&mut self, id: &str) -> Option<&mut Sound> {
        self.sounds.iter_mut().find(|s| s.id == id)
    }
}

/// Ordner für die kopierten Klänge.
pub fn sounds_dir() -> PathBuf {
    freestream_core::paths::data_dir().join("sounds")
}

/// Neue, eindeutige id.
pub fn new_id() -> String {
    use std::sync::atomic::{AtomicU64, Ordering};
    static N: AtomicU64 = AtomicU64::new(0);
    let t = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
    format!("{:x}{:x}", t as u64 ^ std::process::id() as u64, N.fetch_add(1, Ordering::Relaxed))
}

/// Datei in den Ordner kopieren (freier Name: „x.wav“, „x-2.wav“ …); liegt sie schon dort, bleibt sie.
pub fn import(src: &Path, dir: &Path) -> std::io::Result<PathBuf> {
    if src.parent() == Some(dir) {
        return Ok(src.to_path_buf());
    }
    std::fs::create_dir_all(dir)?;
    let stem = src.file_stem().and_then(|s| s.to_str()).filter(|s| !s.is_empty()).unwrap_or("sound");
    let ext = src.extension().and_then(|s| s.to_str()).map(|e| format!(".{e}")).unwrap_or_default();
    let mut dest = dir.join(format!("{stem}{ext}"));
    let mut n = 2;
    while dest.exists() {
        dest = dir.join(format!("{stem}-{n}{ext}"));
        n += 1;
    }
    std::fs::copy(src, &dest)?;
    Ok(dest)
}

/// Neuer Klang zu einer (schon kopierten) Datei: Name aus dem Dateinamen, Farbe reihum.
pub fn new_sound(id: &str, file: &Path, existing: &[Sound]) -> Sound {
    let name = file.file_stem().and_then(|s| s.to_str()).unwrap_or("").replace(['_', '-'], " ").trim().to_string();
    Sound {
        id: id.to_string(),
        name,
        file: file.to_string_lossy().into_owned(),
        color: COLORS[existing.len() % COLORS.len()].to_string(),
        volume: 100,
        looping: false,
    }
}

/// Klang aus dem Soundboard entfernen; die Kopie im Ordner `dir` wird gelöscht, wenn kein anderer Klang sie nutzt.
pub fn remove(board: &mut Board, id: &str, dir: &Path) -> Option<Sound> {
    let pos = board.sounds.iter().position(|s| s.id == id)?;
    let gone = board.sounds.remove(pos);
    let f = Path::new(&gone.file);
    if f.parent() == Some(dir) && !board.sounds.iter().any(|s| s.file == gone.file) {
        let _ = std::fs::remove_file(f);
    }
    Some(gone)
}

/// Wohin ein Mix spielt: direkt auf sein Gerät (nicht virtuell, ein Gerät) oder in seinen Null-Sink.
pub fn mix_target(m: &Mix, devices: &[String]) -> Option<String> {
    if !m.virtual_ && devices.len() == 1 {
        return Some(devices[0].clone());
    }
    if devices.is_empty() && !m.virtual_ {
        return None;
    }
    Some(Mixer::mix_sink(m))
}

/// Sinks für „Abspielen in“ und „Abhören in“ (jeder einmal). `mixes`: (Mix-id, Ziel).
pub fn targets(play: &str, monitor: &str, mixes: &[(String, Option<String>)]) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    let mut add = |t: &Option<String>| {
        if let Some(t) = t {
            if !out.contains(t) {
                out.push(t.clone());
            }
        }
    };
    for (mid, t) in mixes {
        if play == ALL_MIXES || (!play.is_empty() && play == mid) {
            add(t);
        }
    }
    for (mid, t) in mixes {
        if !monitor.is_empty() && monitor == mid {
            add(t);
        }
    }
    out
}

/// Lautstärke eines Klangs mit dem Hauptregler des Soundboards (0 … 100).
pub fn effective_volume(sound: i64, board: i64) -> u32 {
    (sound.clamp(0, 100) * board.clamp(0, 100) / 100) as u32
}

/// Kennung der Soundboard-Streams (Property `linewave.loop`): FreeWave zählt sie nicht als Programme und
/// verschiebt sie nicht in Kanäle.
pub const STREAM_ID: &str = "linewave_soundboard";

/// Befehl zum Abspielen auf einem Sink (leer: Standardausgabe); pw-play, sonst paplay.
pub fn player_cmd(file: &str, volume: u32, target: &str, have: impl Fn(&str) -> bool) -> Option<Vec<String>> {
    let vol = volume.min(100);
    let mut c: Vec<String> = Vec::new();
    if have("pw-play") {
        c.push("pw-play".into());
        c.extend(["-P".to_string(), format!("{{ {LOOP_PROP} = \"{STREAM_ID}\" }}")]);
        if !target.is_empty() {
            c.extend(["--target".to_string(), target.to_string()]);
        }
        c.push(format!("--volume={:.2}", vol as f64 / 100.0));
    } else if have("paplay") {
        c.push("paplay".into());
        c.push(format!("--property={LOOP_PROP}={STREAM_ID}"));
        if !target.is_empty() {
            c.push(format!("--device={target}"));
        }
        c.push(format!("--volume={}", vol * 65536 / 100));
    } else {
        return None;
    }
    c.push(file.to_string());
    Some(c)
}

/// Befehle für einen Klang auf allen Zielen (ohne Ziel: einmal auf der Standardausgabe).
pub fn commands(sound: &Sound, board_volume: i64, targets: &[String], have: impl Fn(&str) -> bool) -> Vec<Vec<String>> {
    let vol = effective_volume(sound.volume, board_volume);
    let none = [String::new()];
    let list: &[String] = if targets.is_empty() { &none } else { targets };
    list.iter().filter_map(|t| player_cmd(&sound.file, vol, t, &have)).collect()
}

struct Proc {
    cmd: Vec<String>,
    child: Option<Child>,
    started: Instant,
}

struct Playing {
    procs: Vec<Proc>,
    looping: bool,
}

/// Laufende Wiedergaben je Klang-id.
#[derive(Default)]
pub struct Player {
    playing: HashMap<String, Playing>,
}

fn spawn(cmd: &[String]) -> Option<Child> {
    let (prog, args) = cmd.split_first()?;
    Command::new(prog).args(args).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn()
        .map_err(|e| log::warn!("{prog}: {e}")).ok()
}

impl Player {
    pub fn new() -> Self {
        Self::default()
    }

    /// Klang starten (läuft er schon, beginnt er von vorn); false, wenn kein Prozess startet.
    pub fn play(&mut self, id: &str, cmds: Vec<Vec<String>>, looping: bool) -> bool {
        self.stop(id);
        let procs: Vec<Proc> = cmds.into_iter()
            .filter_map(|cmd| spawn(&cmd).map(|c| Proc { cmd, child: Some(c), started: Instant::now() })).collect();
        if procs.is_empty() {
            return false;
        }
        self.playing.insert(id.to_string(), Playing { procs, looping });
        true
    }

    pub fn stop(&mut self, id: &str) {
        if let Some(p) = self.playing.remove(id) {
            for mut pr in p.procs {
                if let Some(mut c) = pr.child.take() {
                    let _ = c.kill();
                    let _ = c.wait();
                }
            }
        }
    }

    pub fn stop_all(&mut self) {
        let ids: Vec<String> = self.playing.keys().cloned().collect();
        for id in ids {
            self.stop(&id);
        }
    }

    /// Schleife für einen laufenden Klang ein-/ausschalten.
    pub fn set_looping(&mut self, id: &str, on: bool) {
        if let Some(p) = self.playing.get_mut(id) {
            p.looping = on;
        }
    }

    /// Beendete Prozesse abholen; Schleifen neu starten (nicht, wenn der Player sofort mit Fehler endet).
    pub fn tick(&mut self) {
        for p in self.playing.values_mut() {
            let looping = p.looping;
            for pr in &mut p.procs {
                let Some(c) = pr.child.as_mut() else { continue };
                let Ok(Some(status)) = c.try_wait() else { continue };
                pr.child = None;
                let broken = !status.success() && pr.started.elapsed() < Duration::from_millis(500);
                if looping && !broken {
                    pr.child = spawn(&pr.cmd);
                    pr.started = Instant::now();
                }
            }
            p.procs.retain(|pr| pr.child.is_some());
        }
        self.playing.retain(|_, p| !p.procs.is_empty());
    }

    pub fn is_playing(&self, id: &str) -> bool {
        self.playing.contains_key(id)
    }

    /// ids der laufenden Klänge.
    pub fn playing(&self) -> Vec<String> {
        let mut v: Vec<String> = self.playing.keys().cloned().collect();
        v.sort();
        v
    }
}

impl Drop for Player {
    fn drop(&mut self) {
        self.stop_all();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_and_writes_config() {
        let conf = json!({
            "soundboard": [{"id": "a", "name": "Applause", "file": "/x/a.wav", "color": "#ff0000", "volume": 70, "loop": true},
                           {"name": "no id"}, {"id": "b", "file": "/x/b.mp3"}],
            "soundboard_play": "all", "soundboard_monitor": "monitor", "soundboard_volume": 120
        });
        let b = Board::from_conf(conf.as_object().unwrap());
        assert_eq!(b.sounds.len(), 2);
        assert_eq!(b.sounds[0], Sound { id: "a".into(), name: "Applause".into(), file: "/x/a.wav".into(),
                                        color: "#ff0000".into(), volume: 70, looping: true });
        assert_eq!((b.sounds[1].volume, b.sounds[1].looping), (100, false));
        assert_eq!((b.play.as_str(), b.monitor.as_str(), b.volume), ("all", "monitor", 100));
        let mut out = Map::new();
        b.write(&mut out);
        assert_eq!(out["soundboard"][0], json!({"id": "a", "name": "Applause", "file": "/x/a.wav", "color": "#ff0000",
                                                "volume": 70, "loop": true}));
        assert_eq!(out["soundboard_play"], json!("all"));
        assert_eq!(out["soundboard_volume"], json!(100));
        assert_eq!(Board::from_conf(&out), b);
        assert_eq!(Board::from_conf(&Map::new()), Board::default());
        assert_eq!(Board::default().volume, 80);
    }

    #[test]
    fn imports_and_removes_files() {
        let src = tempfile::tempdir().unwrap();
        let dir = src.path().join("sounds");
        let f = src.path().join("air_horn.wav");
        std::fs::write(&f, b"RIFF").unwrap();
        let a = import(&f, &dir).unwrap();
        let b = import(&f, &dir).unwrap();
        assert_eq!(a, dir.join("air_horn.wav"));
        assert_eq!(b, dir.join("air_horn-2.wav"));
        assert_eq!(import(&a, &dir).unwrap(), a); // schon im Ordner
        let s = new_sound("x1", &a, &[]);
        assert_eq!((s.name.as_str(), s.color.as_str(), s.volume), ("air horn", COLORS[0], 100));
        let mut board = Board { sounds: vec![s.clone(), new_sound("x2", &b, &[s])], ..Default::default() };
        assert_eq!(board.sounds[1].color, COLORS[1]);
        assert!(remove(&mut board, "x1", &dir).is_some());
        assert!(!a.exists() && b.exists());
        assert!(remove(&mut board, "nope", &dir).is_none());
        assert!(new_id() != new_id());
    }

    #[test]
    fn picks_targets() {
        let virt = Mix { id: "stream".into(), virtual_: true, ..Default::default() };
        let direct = Mix { id: "monitor".into(), ..Default::default() };
        let multi = Mix { id: "chat".into(), ..Default::default() };
        assert_eq!(mix_target(&virt, &[]), Some("linewave_streammix".into()));
        assert_eq!(mix_target(&direct, &["alsa_output.a".into()]), Some("alsa_output.a".into()));
        assert_eq!(mix_target(&multi, &["a".into(), "b".into()]), Some("linewave_mix_chat".into()));
        assert_eq!(mix_target(&multi, &[]), None);
        let mixes = vec![("monitor".to_string(), Some("alsa_output.a".to_string())),
                         ("stream".to_string(), Some("linewave_streammix".to_string())),
                         ("chat".to_string(), None)];
        assert_eq!(targets("stream", "monitor", &mixes), ["linewave_streammix", "alsa_output.a"]);
        assert_eq!(targets("all", "monitor", &mixes), ["alsa_output.a", "linewave_streammix"]);
        assert_eq!(targets("", "", &mixes), Vec::<String>::new());
        assert_eq!(targets("chat", "", &mixes), Vec::<String>::new());
    }

    const PW_PROPS: &str = "{ linewave.loop = \"linewave_soundboard\" }";
    const PA_PROP: &str = "--property=linewave.loop=linewave_soundboard";

    #[test]
    fn builds_command_lines() {
        let only = |p: &'static str| move |x: &str| x == p;
        assert_eq!(player_cmd("/s/a.wav", 80, "linewave_streammix", only("pw-play")).unwrap(),
                   ["pw-play", "-P", PW_PROPS, "--target", "linewave_streammix", "--volume=0.80", "/s/a.wav"]);
        assert_eq!(player_cmd("/s/a.wav", 50, "linewave_mix_chat", only("paplay")).unwrap(),
                   ["paplay", PA_PROP, "--device=linewave_mix_chat", "--volume=32768", "/s/a.wav"]);
        assert_eq!(player_cmd("/s/a.wav", 150, "", only("pw-play")).unwrap(),
                   ["pw-play", "-P", PW_PROPS, "--volume=1.00", "/s/a.wav"]);
        assert!(player_cmd("/s/a.wav", 50, "x", |_| false).is_none());
        assert_eq!(effective_volume(50, 80), 40);
        let s = Sound { id: "a".into(), file: "/s/a.wav".into(), volume: 50, ..Default::default() };
        let c = commands(&s, 80, &["one".into(), "two".into()], only("pw-play"));
        assert_eq!(c, vec![vec!["pw-play", "-P", PW_PROPS, "--target", "one", "--volume=0.40", "/s/a.wav"],
                           vec!["pw-play", "-P", PW_PROPS, "--target", "two", "--volume=0.40", "/s/a.wav"]]);
        assert_eq!(commands(&s, 100, &[], only("paplay")), vec![vec!["paplay", PA_PROP, "--volume=32768", "/s/a.wav"]]);
        // FreeWave erkennt die Streams als eigene
        let b = crate::pactl::Block { props: [(LOOP_PROP.to_string(), STREAM_ID.to_string())].into_iter().collect(),
                                      ..Default::default() };
        assert_eq!(crate::mixer::loop_id(&b), STREAM_ID);
    }

    #[test]
    fn plays_stops_and_loops() {
        let mut p = Player::new();
        assert!(!p.play("bad", vec![vec!["/nonexistent/player".into()]], false));
        // „Wiedergabe“ mit sleep: läuft, bis sie gestoppt wird
        assert!(p.play("a", vec![vec!["sleep".into(), "5".into()]], false));
        assert!(p.is_playing("a"));
        p.stop("a");
        assert!(!p.is_playing("a"));
        // kurze Wiedergabe endet von selbst …
        assert!(p.play("b", vec![vec!["true".into()]], false));
        std::thread::sleep(Duration::from_millis(200));
        p.tick();
        assert!(!p.is_playing("b"));
        // … in der Schleife beginnt sie neu
        assert!(p.play("c", vec![vec!["sleep".into(), "0.1".into()]], true));
        std::thread::sleep(Duration::from_millis(300));
        p.tick();
        assert!(p.is_playing("c"));
        assert_eq!(p.playing(), ["c"]);
        // ein Player, der sofort mit Fehler endet, startet nicht endlos neu
        assert!(p.play("d", vec![vec!["false".into()]], true));
        std::thread::sleep(Duration::from_millis(200));
        p.tick();
        assert!(!p.is_playing("d"));
        p.stop_all();
        assert!(p.playing().is_empty());
    }
}
