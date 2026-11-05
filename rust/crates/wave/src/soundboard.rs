//! Soundboard von FreeWave: Klänge in `config.json` (Schlüssel `"soundboard"`, `"soundboard_play"`,
//! `"soundboard_monitor"`, `"soundboard_volume"` – dieselben wie in der Python-Version), Dateien als Kopie
//! im Datenordner (`sounds/`), Wiedergabe über die Mix-Sinks von FreeWave mit `pw-play --target <Sink>`
//! oder `paplay --device=<Sink>` als Kindprozess. Stopp beendet den Prozess, eine Schleife startet ihn neu.
//!
//! Zuschnitt (`"start"`/`"end"` in Sekunden, optional): pw-play und paplay können nicht springen, daher
//! wird für einen zugeschnittenen WAV-Klang eine gekürzte Kopie geschrieben (`<state>/soundboard-trim/`,
//! derselbe Dateiname wie in der Python-Version) und diese gespielt; andere Formate spielen ungekürzt.
//! WAV wird von Hand gelesen und geschrieben (PCM 8/16/24/32 Bit und Float, auch WAVE_FORMAT_EXTENSIBLE).
//!
//! Gespeichert wird:
//! ```json
//! "soundboard": [{"id": "…", "name": "Applaus", "file": "/…/sounds/applaus.wav", "color": "#ec4899",
//!                 "volume": 100, "loop": false, "start": 0.25, "end": 3.6}],
//! "soundboard_play": "stream",      // "" = nicht ausgewählt, Mix-id oder "all" = alle Mixe
//! "soundboard_monitor": "monitor",  // "" = nicht ausgewählt oder Mix-id
//! "soundboard_volume": 80           // 0 … 100
//! ```

use std::collections::HashMap;
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
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
/// FreeDeck-Taste „Soundboard: Klang abspielen“
pub const ACTION_UUID: &str = "com.lindeck.freewave.soundboard";
/// Balken der Wellenform-Vorschau
pub const PEAKS: usize = 120;
/// kürzester zugeschnittener Klang (Sekunden)
pub const MIN_LENGTH: f64 = 0.05;

/// Ein Klang auf dem Soundboard.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Sound {
    pub id: String,
    pub name: String,
    pub file: String,
    pub color: String,
    /// 0 … 100
    pub volume: i64,
    pub looping: bool,
    /// Zuschnitt: Anfang (> 0) und Ende in Sekunden; None = vom Anfang bzw. bis zum Ende
    pub start: Option<f64>,
    pub end: Option<f64>,
}

/// Zeitpunkt in Sekunden (≥ 0, auf ms gerundet) oder None.
fn secs(v: Option<&Value>) -> Option<f64> {
    let v = v?.as_f64()?;
    (v.is_finite() && (0.0..1e7).contains(&v)).then(|| (v * 1000.0).round() / 1000.0)
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
            ..Default::default()
        }.with_trim(secs(o.get("start")), secs(o.get("end"))))
    }

    /// Zuschnitt setzen (wie `normalize` in Python: Anfang nur > 0, Ende nur nach dem Anfang).
    pub fn with_trim(mut self, start: Option<f64>, end: Option<f64>) -> Self {
        self.start = start.filter(|s| *s > 0.0);
        self.end = end.filter(|e| e - self.start.unwrap_or(0.0) >= MIN_LENGTH);
        self
    }

    pub fn to_value(&self) -> Value {
        let mut v = json!({"id": self.id, "name": self.name, "file": self.file, "color": self.color,
                           "volume": self.volume, "loop": self.looping});
        if let Some(s) = self.start {
            v["start"] = json!(s);
        }
        if let Some(e) = self.end {
            v["end"] = json!(e);
        }
        v
    }
}

/// Alle Einstellungen des Soundboards.
#[derive(Debug, Clone, PartialEq)]
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
        ..Default::default()
    }
}

/// Klang aus dem Soundboard entfernen; die Kopie im Ordner `dir` wird gelöscht, wenn kein anderer Klang sie nutzt.
pub fn remove(board: &mut Board, id: &str, dir: &Path) -> Option<Sound> {
    let pos = board.sounds.iter().position(|s| s.id == id)?;
    let gone = board.sounds.remove(pos);
    drop_trimmed(&trim_dir(), &gone.id, None);
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
    let file = match playable(sound) {
        Ok(f) => f,
        Err(e) => {
            log::warn!("{}: {e}", sound.file);
            sound.file.clone()
        }
    };
    list.iter().filter_map(|t| player_cmd(&file, vol, t, &have)).collect()
}

// ------------------------------------------------------------------ WAV: Wellenform und Zuschnitt

/// Kopf einer WAV-Datei (nur PCM und Float).
#[derive(Debug, Clone)]
pub struct WavHead {
    pub rate: u32,
    pub channels: u16,
    pub bits: u16,
    pub float: bool,
    /// Lage und Länge der Daten in der Datei
    pub data_off: u64,
    pub data_len: u64,
    /// fmt-Abschnitt unverändert (für die gekürzte Kopie)
    fmt: Vec<u8>,
}

impl WavHead {
    pub fn frame_bytes(&self) -> u64 {
        (self.bits as u64 / 8).max(1) * self.channels.max(1) as u64
    }

    pub fn frames(&self) -> u64 {
        self.data_len / self.frame_bytes()
    }

    pub fn duration(&self) -> f64 {
        self.frames() as f64 / self.rate.max(1) as f64
    }
}

fn bad(msg: &str) -> std::io::Error {
    std::io::Error::new(std::io::ErrorKind::InvalidData, msg.to_string())
}

/// Kopf lesen: RIFF/WAVE, fmt (Format 1 = PCM, 3 = Float, 0xFFFE mit einem von beiden), data.
pub fn read_head(f: &mut std::fs::File) -> std::io::Result<WavHead> {
    let size = f.metadata()?.len();
    let mut b = [0u8; 12];
    f.read_exact(&mut b)?;
    if &b[0..4] != b"RIFF" || &b[8..12] != b"WAVE" {
        return Err(bad("no WAV file"));
    }
    let mut fmt: Option<Vec<u8>> = None;
    let mut pos = 12u64;
    loop {
        let mut h = [0u8; 8];
        f.seek(SeekFrom::Start(pos))?;
        if f.read_exact(&mut h).is_err() {
            return Err(bad("no data chunk"));
        }
        let len = u32::from_le_bytes([h[4], h[5], h[6], h[7]]) as u64;
        let body = pos + 8;
        if &h[0..4] == b"fmt " {
            let mut v = vec![0u8; len.min(1024) as usize];
            f.read_exact(&mut v)?;
            fmt = Some(v);
        } else if &h[0..4] == b"data" {
            let fmt = fmt.ok_or_else(|| bad("data before fmt"))?;
            if fmt.len() < 16 {
                return Err(bad("short fmt chunk"));
            }
            let u16le = |i: usize| u16::from_le_bytes([fmt[i], fmt[i + 1]]);
            let mut tag = u16le(0);
            if tag == 0xFFFE && fmt.len() >= 26 {
                tag = u16le(24); // WAVE_FORMAT_EXTENSIBLE: Unterformat
            }
            let bits = u16le(14);
            let float = tag == 3;
            if !(tag == 1 && matches!(bits, 8 | 16 | 24 | 32) || float && matches!(bits, 32 | 64)) {
                return Err(bad("unsupported WAV format"));
            }
            let rate = u32::from_le_bytes([fmt[4], fmt[5], fmt[6], fmt[7]]);
            let channels = u16le(2);
            if rate == 0 || channels == 0 {
                return Err(bad("bad WAV header"));
            }
            // Länge 0 oder zu groß (Aufnahme abgebrochen): bis zum Dateiende
            let avail = size.saturating_sub(body);
            let data_len = if len == 0 || len > avail { avail } else { len };
            return Ok(WavHead { rate, channels, bits, float, data_off: body, data_len, fmt });
        }
        pos = body + len + (len & 1);
        if pos >= size {
            return Err(bad("no data chunk"));
        }
    }
}

/// Größter Ausschlag (0 … 1) in einem Block Samples.
fn peak(data: &[u8], bits: u16, float: bool) -> f64 {
    let w = (bits / 8) as usize;
    let mut top = 0f64;
    for s in data.chunks_exact(w) {
        let v = match (w, float) {
            (1, _) => (s[0] as f64 - 128.0) / 128.0,
            (2, _) => i16::from_le_bytes([s[0], s[1]]) as f64 / 32768.0,
            (3, _) => (i32::from_le_bytes([0, s[0], s[1], s[2]]) >> 8) as f64 / 8_388_608.0,
            (4, false) => i32::from_le_bytes([s[0], s[1], s[2], s[3]]) as f64 / 2_147_483_648.0,
            (4, true) => f32::from_le_bytes([s[0], s[1], s[2], s[3]]) as f64,
            (8, true) => f64::from_le_bytes([s[0], s[1], s[2], s[3], s[4], s[5], s[6], s[7]]),
            _ => 0.0,
        };
        if v.is_finite() {
            top = top.max(v.abs());
        }
    }
    top.min(1.0)
}

/// Länge und Wellenform eines Klangs.
#[derive(Debug, Clone, PartialEq)]
pub struct WavInfo {
    /// Sekunden
    pub duration: f64,
    /// Höchstwerte je Abschnitt, 0 … 1 (lautester Abschnitt = 1)
    pub peaks: Vec<f32>,
}

fn read_info(path: &Path, n: usize) -> std::io::Result<WavInfo> {
    let mut f = std::fs::File::open(path)?;
    let h = read_head(&mut f)?;
    let frames = h.frames();
    let fb = h.frame_bytes();
    // je Abschnitt höchstens ~64 KB lesen: große Dateien nur stichprobenweise
    let take = (frames / n as u64).max(1).min((64 << 10) / fb).max(1);
    let mut peaks = Vec::with_capacity(n);
    let mut buf = vec![0u8; (take * fb) as usize];
    for i in 0..if frames > 0 { n } else { 0 } {
        let at = (i as u64 * frames / n as u64).min(frames - 1);
        f.seek(SeekFrom::Start(h.data_off + at * fb))?;
        let want = ((frames - at).min(take) * fb) as usize;
        f.read_exact(&mut buf[..want])?;
        peaks.push(peak(&buf[..want], h.bits, h.float));
    }
    let top = peaks.iter().cloned().fold(0.0, f64::max);
    Ok(WavInfo {
        duration: h.duration(),
        peaks: peaks.into_iter().map(|p| if top > 0.0 { (p / top) as f32 } else { 0.0 }).collect(),
    })
}

type InfoKey = (String, u128, u64, usize);
static INFO: Mutex<Option<HashMap<InfoKey, Option<Arc<WavInfo>>>>> = Mutex::new(None);

fn stat(path: &Path) -> Option<(u128, u64)> {
    let m = std::fs::metadata(path).ok()?;
    let t = m.modified().ok()?.duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
    Some((t, m.len()))
}

fn is_wav(path: &str) -> bool {
    Path::new(path).extension().and_then(|e| e.to_str()).is_some_and(|e| e.eq_ignore_ascii_case("wav"))
}

/// Länge und Wellenform (n Werte) einer WAV-Datei; None für andere Formate oder bei Fehlern.
/// Zwischengespeichert je Pfad und Änderungszeit.
pub fn analyze(path: &str, n: usize) -> Option<Arc<WavInfo>> {
    if !is_wav(path) {
        return None;
    }
    let (mtime, size) = stat(Path::new(path))?;
    let key = (path.to_string(), mtime, size, n);
    if let Some(hit) = INFO.lock().unwrap().as_ref().and_then(|m| m.get(&key)) {
        return hit.clone();
    }
    let info = read_info(Path::new(path), n.max(1)).map_err(|e| log::debug!("{path}: {e}")).ok().map(Arc::new);
    let mut g = INFO.lock().unwrap();
    let m = g.get_or_insert_with(HashMap::new);
    if m.len() > 256 {
        m.clear();
    }
    m.insert(key, info.clone());
    info
}

/// (Anfang, Ende) in Sekunden, auf die Länge `duration` begrenzt.
pub fn trim_range(sound: &Sound, duration: f64) -> (f64, f64) {
    let start = sound.start.unwrap_or(0.0).clamp(0.0, duration);
    let end = sound.end.map(|e| e.clamp(0.0, duration)).unwrap_or(duration);
    if end - start < MIN_LENGTH {
        return (0.0, duration);
    }
    (start, end)
}

/// Wie lange der Klang (zugeschnitten) spielt, in Sekunden.
pub fn play_length(sound: &Sound, info: &WavInfo) -> f64 {
    let (a, b) = trim_range(sound, info.duration);
    b - a
}

/// Ausschnitt `start` … `end` (Sekunden) einer WAV-Datei nach `dst` schreiben; gibt die Zahl der Frames zurück.
pub fn write_trimmed(src: &Path, dst: &Path, start: f64, end: f64) -> std::io::Result<u64> {
    let mut f = std::fs::File::open(src)?;
    let h = read_head(&mut f)?;
    let frames = h.frames();
    let a = ((start * h.rate as f64).round().max(0.0) as u64).min(frames);
    let b = ((end * h.rate as f64).round().max(0.0) as u64).min(frames).max(a);
    let fb = h.frame_bytes();
    let mut data = vec![0u8; ((b - a) * fb) as usize];
    f.seek(SeekFrom::Start(h.data_off + a * fb))?;
    f.read_exact(&mut data)?;
    let fmt_len = h.fmt.len() as u32;
    let pad = |n: u32| n & 1;
    let riff = 4 + 8 + fmt_len + pad(fmt_len) + 8 + data.len() as u32 + pad(data.len() as u32);
    let tmp = dst.with_extension("wav.tmp");
    {
        let mut o = std::io::BufWriter::new(std::fs::File::create(&tmp)?);
        o.write_all(b"RIFF")?;
        o.write_all(&riff.to_le_bytes())?;
        o.write_all(b"WAVEfmt ")?;
        o.write_all(&fmt_len.to_le_bytes())?;
        o.write_all(&h.fmt)?;
        if pad(fmt_len) == 1 {
            o.write_all(&[0])?;
        }
        o.write_all(b"data")?;
        o.write_all(&(data.len() as u32).to_le_bytes())?;
        o.write_all(&data)?;
        if pad(data.len() as u32) == 1 {
            o.write_all(&[0])?;
        }
        o.flush()?;
    }
    std::fs::rename(&tmp, dst)?;
    Ok(b - a)
}

/// Ordner für die zugeschnittenen Kopien.
pub fn trim_dir() -> PathBuf {
    freestream_core::paths::state_dir().join("soundboard-trim")
}

/// FNV-1a (64 Bit) – derselbe Dateiname der Kopie wie in der Python-Version.
pub fn fnv(text: &str) -> String {
    let mut h: u64 = 0xcbf29ce484222325;
    for b in text.bytes() {
        h = (h ^ b as u64).wrapping_mul(0x100000001b3);
    }
    format!("{h:016x}")
}

fn safe_id(id: &str) -> String {
    let s: String = id.chars().map(|c| if c.is_ascii_alphanumeric() || c == '_' || c == '-' { c } else { '_' }).collect();
    if s.is_empty() { "sound".into() } else { s }
}

/// Zugeschnittene Kopien eines Klangs löschen (außer `keep`).
fn drop_trimmed(dir: &Path, id: &str, keep: Option<&str>) {
    let prefix = format!("{}-", safe_id(id));
    for e in std::fs::read_dir(dir).into_iter().flatten().flatten() {
        let name = e.file_name().to_string_lossy().into_owned();
        if name.starts_with(&prefix) && name.ends_with(".wav") && Some(name.as_str()) != keep {
            let _ = std::fs::remove_file(e.path());
        }
    }
}

/// Datei, die für den Klang gespielt wird: bei einem zugeschnittenen WAV die gekürzte Kopie.
pub fn playable(sound: &Sound) -> std::io::Result<String> {
    playable_in(sound, &trim_dir())
}

/// [`playable`] mit den Kopien im Ordner `dir`.
pub fn playable_in(sound: &Sound, dir: &Path) -> std::io::Result<String> {
    if sound.start.is_none() && sound.end.is_none() {
        return Ok(sound.file.clone());
    }
    let Some(info) = analyze(&sound.file, PEAKS) else {
        return Ok(sound.file.clone()); // Trimmen geht nur mit WAV: ungekürzt spielen
    };
    let (start, end) = trim_range(sound, info.duration);
    if start <= 0.0 && end >= info.duration {
        return Ok(sound.file.clone());
    }
    let (mtime, size) = stat(Path::new(&sound.file)).ok_or_else(|| bad("file not found"))?;
    let key = format!("{}|{mtime}|{size}|{start:.3}|{end:.3}", sound.file);
    let name = format!("{}-{}.wav", safe_id(&sound.id), fnv(&key));
    let dst = dir.join(&name);
    if !dst.is_file() {
        std::fs::create_dir_all(dir)?;
        drop_trimmed(dir, &sound.id, Some(&name)); // ältere Zuschnitte desselben Klangs
        write_trimmed(Path::new(&sound.file), &dst, start, end)?;
    }
    Ok(dst.to_string_lossy().into_owned())
}

/// „0:07“ (gerundet; ein kurzer Klang zeigt mindestens 0:01).
pub fn fmt_time(sec: f64) -> String {
    let mut s = sec.max(0.0).round() as u64;
    if sec > 0.0 {
        s = s.max(1);
    }
    format!("{}:{:02}", s / 60, s % 60)
}

/// „0:03.65“ – Felder des Zuschnitts (`digits` Nachkommastellen).
pub fn fmt_precise(sec: f64, digits: u32) -> String {
    let k = 10u64.pow(digits);
    let cs = (sec.max(0.0) * k as f64).round() as u64;
    let (m, rest) = (cs / (60 * k), cs % (60 * k));
    if digits == 0 {
        return format!("{m}:{rest:02}");
    }
    format!("{m}:{:02}.{:0w$}", rest / k, rest % k, w = digits as usize)
}

/// „1:02.5“, „62,5“ oder „0:00.25“ -> Sekunden.
pub fn parse_time(text: &str) -> Option<f64> {
    let t = text.trim().replace(',', ".");
    if t.is_empty() {
        return None;
    }
    let v = match t.rsplit_once(':') {
        Some((m, s)) => (if m.is_empty() { 0 } else { m.parse::<u64>().ok()? }) as f64 * 60.0 + s.parse::<f64>().ok()?,
        None => t.parse::<f64>().ok()?,
    };
    (v.is_finite() && v >= 0.0).then_some(v)
}

// ------------------------------------------------------------------ Tasten auf dem Deck

/// Erste FreeDeck-Taste „Soundboard: Klang abspielen“ je Klang-id: (Seite, Taste), beide ab 1, die
/// Tasten zeilenweise wie auf dem Gerät; Profile in der Reihenfolge des Profilordners.
pub fn deck_keys(store_root: Option<&Path>) -> HashMap<String, (usize, usize)> {
    let store = freestream_core::profiles::ProfileStore::new(store_root);
    let mut out = HashMap::new();
    for p in store.profiles.values() {
        let cols = p.geometry().cols.max(1) as usize;
        for (n, pg) in p.pages().iter().enumerate() {
            let mut keys: Vec<_> = pg.keypad.iter().collect();
            keys.sort_by_key(|((c, r), _)| (*r, *c));
            for ((c, r), a) in keys {
                if a.get("UUID").and_then(Value::as_str) != Some(ACTION_UUID) {
                    continue;
                }
                let sid = a.get("Settings").and_then(|s| s.get("sound")).and_then(Value::as_str).unwrap_or("");
                if !sid.is_empty() && !out.contains_key(sid) {
                    out.insert(sid.to_string(), (n + 1, *r as usize * cols + *c as usize + 1));
                }
            }
        }
    }
    out
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

    /// Zeit seit dem Start (bei einer Schleife: seit dem letzten Neubeginn); None, wenn er nicht spielt.
    pub fn elapsed(&self, id: &str) -> Option<Duration> {
        self.playing.get(id)?.procs.first().map(|p| p.started.elapsed())
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
                                        color: "#ff0000".into(), volume: 70, looping: true, ..Default::default() });
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

    /// WAV (PCM 16 Bit) mit `frames` Frames schreiben: lauter werdender Sägezahn.
    fn wav16(path: &Path, rate: u32, channels: u16, frames: u32) {
        let mut d = Vec::new();
        for i in 0..frames {
            let v = ((i % 100) as i32 * 300 - 15000) * i as i32 / frames.max(1) as i32;
            for _ in 0..channels {
                d.extend_from_slice(&(v as i16).to_le_bytes());
            }
        }
        let mut o = Vec::new();
        o.extend_from_slice(b"RIFF");
        o.extend_from_slice(&(36 + 8 + d.len() as u32).to_le_bytes()); // mit einem LIST-Abschnitt davor
        o.extend_from_slice(b"WAVEfmt ");
        o.extend_from_slice(&16u32.to_le_bytes());
        o.extend_from_slice(&1u16.to_le_bytes());
        o.extend_from_slice(&channels.to_le_bytes());
        o.extend_from_slice(&rate.to_le_bytes());
        o.extend_from_slice(&(rate * channels as u32 * 2).to_le_bytes());
        o.extend_from_slice(&(channels * 2).to_le_bytes());
        o.extend_from_slice(&16u16.to_le_bytes());
        o.extend_from_slice(b"LIST\x03\0\0\0abc\0"); // ungerade Länge: Füllbyte
        o.extend_from_slice(b"data");
        o.extend_from_slice(&(d.len() as u32).to_le_bytes());
        o.extend_from_slice(&d);
        std::fs::write(path, o).unwrap();
    }

    #[test]
    fn trim_round_trip_in_config() {
        let conf = json!({"soundboard": [
            {"id": "a", "file": "/x/a.wav", "start": 0.25, "end": 3.65},
            {"id": "b", "file": "/x/b.wav", "start": 0, "end": -1},
            {"id": "c", "file": "/x/c.wav", "start": 2.0, "end": 2.01},
            {"id": "d", "file": "/x/d.wav", "start": "x", "end": 1.23456}]});
        let b = Board::from_conf(conf.as_object().unwrap());
        assert_eq!((b.sounds[0].start, b.sounds[0].end), (Some(0.25), Some(3.65)));
        assert_eq!((b.sounds[1].start, b.sounds[1].end), (None, None));
        assert_eq!((b.sounds[2].start, b.sounds[2].end), (Some(2.0), None)); // Ende vor dem Anfang
        assert_eq!((b.sounds[3].start, b.sounds[3].end), (None, Some(1.235)));
        let mut out = Map::new();
        b.write(&mut out);
        assert_eq!(out["soundboard"][0]["start"], json!(0.25));
        assert_eq!(out["soundboard"][0]["end"], json!(3.65));
        assert!(out["soundboard"][1].get("start").is_none() && out["soundboard"][1].get("end").is_none());
        assert_eq!(Board::from_conf(&out), b);
        // Bereich, Länge, Zeiten
        let s = &b.sounds[0];
        assert_eq!(trim_range(s, 4.0), (0.25, 3.65));
        assert_eq!(trim_range(s, 2.0), (0.25, 2.0));
        assert_eq!(trim_range(&Sound::default(), 4.0), (0.0, 4.0));
        assert!((play_length(s, &WavInfo { duration: 4.0, peaks: vec![] }) - 3.4).abs() < 1e-9);
        assert_eq!((fmt_time(7.2), fmt_time(0.3), fmt_time(0.0), fmt_time(65.0)), ("0:07".into(), "0:01".into(),
                   "0:00".into(), "1:05".into()));
        assert_eq!((fmt_precise(3.65, 2), fmt_precise(0.25, 2), fmt_precise(4.0, 1), fmt_precise(61.999, 2)),
                   ("0:03.65".into(), "0:00.25".into(), "0:04.0".into(), "1:02.00".into()));
        assert_eq!(parse_time("0:03.65"), Some(3.65));
        assert_eq!(parse_time("62,5"), Some(62.5));
        assert_eq!(parse_time("1:02"), Some(62.0));
        assert_eq!((parse_time(""), parse_time("x"), parse_time("-1")), (None, None, None));
        assert_eq!(fnv("abc"), "e71fa2190541574b"); // wie soundboard._fnv in Python
    }

    #[test]
    fn reads_peaks_and_writes_trimmed_wav() {
        let dir = tempfile::tempdir().unwrap();
        let f = dir.path().join("a.wav");
        wav16(&f, 8000, 2, 16000); // 2 s Stereo
        let mut file = std::fs::File::open(&f).unwrap();
        let h = read_head(&mut file).unwrap();
        assert_eq!((h.rate, h.channels, h.bits, h.frames()), (8000, 2, 16, 16000));
        let info = analyze(f.to_str().unwrap(), 50).unwrap();
        assert!((info.duration - 2.0).abs() < 1e-9);
        assert_eq!(info.peaks.len(), 50);
        assert!(info.peaks.iter().all(|p| (0.0..=1.0).contains(p)));
        assert!(info.peaks[0] < 0.1 && info.peaks[49] > 0.9); // wird lauter
        assert!(analyze("/nonexistent/x.wav", 50).is_none());
        let mp3 = dir.path().join("x.mp3");
        std::fs::write(&mp3, b"ID3").unwrap();
        assert!(analyze(mp3.to_str().unwrap(), 50).is_none()); // kein WAV: keine Wellenform
        // Ausschnitt 0.25 … 1.5 s = 10000 Frames
        let out = dir.path().join("cut.wav");
        assert_eq!(write_trimmed(&f, &out, 0.25, 1.5).unwrap(), 10000);
        let mut o = std::fs::File::open(&out).unwrap();
        let ho = read_head(&mut o).unwrap();
        assert_eq!((ho.rate, ho.channels, ho.frames()), (8000, 2, 10000));
        let src = std::fs::read(&f).unwrap();
        let cut = std::fs::read(&out).unwrap();
        let a = h.data_off as usize + 2000 * 4;
        assert_eq!(&cut[ho.data_off as usize..], &src[a..a + 40000]);
        assert_eq!(u32::from_le_bytes(cut[4..8].try_into().unwrap()) as usize, cut.len() - 8);
        // über das Ende hinaus: begrenzt
        assert_eq!(write_trimmed(&f, &out, 1.5, 9.0).unwrap(), 4000);
        // kaputte Datei
        std::fs::write(dir.path().join("bad.wav"), b"RIFF0000WAVEjunk").unwrap();
        assert!(read_head(&mut std::fs::File::open(dir.path().join("bad.wav")).unwrap()).is_err());
    }

    #[test]
    fn playable_writes_trimmed_copy() {
        let dir = tempfile::tempdir().unwrap();
        let td = dir.path().join("trim");
        let f = dir.path().join("a.wav");
        wav16(&f, 8000, 1, 8000);
        let mut s = Sound { id: "x1".into(), file: f.to_string_lossy().into_owned(), volume: 100, ..Default::default() };
        assert_eq!(playable_in(&s, &td).unwrap(), s.file); // nicht zugeschnitten: die Datei selbst
        s.start = Some(0.5);
        let p = playable_in(&s, &td).unwrap();
        assert!(p.starts_with(td.to_str().unwrap()) && p.contains("/x1-"));
        assert_eq!(read_head(&mut std::fs::File::open(&p).unwrap()).unwrap().frames(), 4000);
        s.end = Some(0.75);
        let p2 = playable_in(&s, &td).unwrap();
        assert!(p2 != p && !Path::new(&p).exists()); // alter Zuschnitt weg
        // anderes Format: ungekürzt
        let mut o = s.clone();
        o.file = dir.path().join("a.ogg").to_string_lossy().into_owned();
        assert_eq!(playable_in(&o, &td).unwrap(), o.file);
        drop_trimmed(&td, "x1", None);
        assert!(!Path::new(&p2).exists());
    }

    #[test]
    fn finds_deck_keys() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("P1.sdProfile");
        let pages = ["11111111-0000-4000-8000-000000000001", "11111111-0000-4000-8000-000000000002"];
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("manifest.json"), json!({"Device": {"Model": "20GBA9901"}, "Name": "A",
            "Pages": {"Default": pages[0], "Pages": pages}}).to_string()).unwrap();
        let key = |sid: &str| json!({"UUID": ACTION_UUID, "Settings": {"sound": sid}});
        let acts = [json!({"2,1": key("a"), "1,0": key("b"), "3,2": {"UUID": "com.other"}}),
                    json!({"0,0": key("a"), "4,0": key("c"), "0,1": key("")})];
        for (pg, a) in pages.iter().zip(acts) {
            let d = root.join("Profiles").join(pg.to_uppercase());
            std::fs::create_dir_all(&d).unwrap();
            std::fs::write(d.join("manifest.json"), json!({"Controllers": [{"Type": "Keypad", "Actions": a}]})
                .to_string()).unwrap();
        }
        let k = deck_keys(Some(dir.path()));
        assert_eq!(k.get("a"), Some(&(1, 8))); // Zeile 1, Spalte 2 bei 5 Spalten
        assert_eq!(k.get("b"), Some(&(1, 2)));
        assert_eq!(k.get("c"), Some(&(2, 5)));
        assert_eq!(k.len(), 3);
        assert!(deck_keys(Some(&dir.path().join("none"))).is_empty());
    }

    #[test]
    fn reports_elapsed_time() {
        let mut p = Player::new();
        assert!(p.elapsed("a").is_none());
        assert!(p.play("a", vec![vec!["sleep".into(), "5".into()]], false));
        std::thread::sleep(Duration::from_millis(50));
        assert!(p.elapsed("a").unwrap() >= Duration::from_millis(50));
        p.stop_all();
        assert!(p.elapsed("a").is_none());
    }
}
