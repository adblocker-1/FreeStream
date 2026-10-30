//! pactl: Aufrufe, Auswertung der Textausgabe und der Zwischenspeicher für Abfragen
//! (wie `system.run_out`, `system._parse_blocks`, `system.pactl_snapshot` und `mixer._run`).
//!
//! Lesende Abfragen laufen immer mit `LC_ALL=C` (ohne `LANGUAGE`): übersetzt hieße es „Stumm: nein“
//! statt „Mute: no“ – keine Lautstärke, kein Stumm und kein Ereignis würde erkannt. Schreibende Befehle
//! laufen in der Sprache des Nutzers (ihre Fehlermeldungen sieht er).

use std::cell::RefCell;
use std::collections::{BTreeMap, HashMap};
use std::io::{BufRead, BufReader};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use freestream_core::tr;

use crate::Error;

/// Befehle, die nur lesen (laufen mit englischer Ausgabe und dürfen zwischengespeichert werden).
pub const READ_ONLY: [&str; 4] = ["get-default-sink", "get-default-source", "info", "list"];

/// Ein Eintrag aus `pactl list …` (Sink, Quelle, Stream oder Modul).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Block {
    pub index: String,
    pub name: Option<String>,
    pub description: Option<String>,
    /// Sink eines Streams (Index als Text)
    pub sink: Option<String>,
    /// Argument eines Moduls
    pub argument: Option<String>,
    pub mute: bool,
    /// erste Prozentangabe der Zeile „Volume:“
    pub volume: Option<i64>,
    pub props: BTreeMap<String, String>,
}

impl Block {
    pub fn name(&self) -> &str {
        self.name.as_deref().unwrap_or("")
    }
    pub fn description(&self) -> &str {
        self.description.as_deref().unwrap_or("")
    }
    pub fn prop(&self, key: &str) -> &str {
        self.props.get(key).map(String::as_str).unwrap_or("")
    }
    /// Lautstärke wie `b.get("volume", 0)`.
    pub fn vol(&self) -> i64 {
        self.volume.unwrap_or(0)
    }
}

fn header_re() -> &'static regex::Regex {
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| regex::Regex::new(r"^\S.*#\d+").unwrap())
}

fn pct_re() -> &'static regex::Regex {
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| regex::Regex::new(r"(\d+)%").unwrap())
}

/// Text nach dem ersten Doppelpunkt (wie `s.split(":", 1)[1].strip()`).
fn after_colon(s: &str) -> String {
    s.split_once(':').map(|(_, v)| v.trim().to_string()).unwrap_or_default()
}

/// Ausgabe von `pactl list <what>` zerlegen (wie `system._parse_blocks`).
pub fn parse_blocks(out: &str) -> Vec<Block> {
    let mut blocks: Vec<Block> = Vec::new();
    for line in out.lines() {
        if header_re().is_match(line) {
            let index = line.rsplit('#').next().unwrap_or("").trim().to_string();
            blocks.push(Block { index, ..Default::default() });
            continue;
        }
        let Some(cur) = blocks.last_mut() else { continue };
        let s = line.trim();
        if s.starts_with("Name:") {
            cur.name = Some(after_colon(s));
        } else if s.starts_with("Description:") {
            cur.description = Some(after_colon(s));
        } else if s.starts_with("Sink:") {
            cur.sink = Some(after_colon(s));
        } else if s.starts_with("Argument:") {
            cur.argument = Some(after_colon(s));
        } else if s.starts_with("Mute:") {
            cur.mute = s.ends_with("yes");
        } else if s.starts_with("Volume:") && cur.volume.is_none() {
            cur.volume = Some(pct_re().captures(s).and_then(|c| c[1].parse().ok()).unwrap_or(0));
        } else if let Some((k, v)) = s.split_once('=') {
            if k.trim().contains('.') {
                let v = v.trim();
                // Python: v.strip().strip('"') – Anführungszeichen an beiden Enden entfernen
                cur.props.insert(k.trim().to_string(), v.trim_matches('"').to_string());
            }
        }
    }
    blocks
}

/// Umgebung für pactl, dessen Ausgabe gelesen wird: immer englisch (wie `system.pactl_env`).
pub fn english_env(cmd: &mut Command) -> &mut Command {
    cmd.env("LC_ALL", "C").env_remove("LANGUAGE")
}

/// Liegt ein Programm im Suchpfad? (`shutil.which`)
pub fn which(cmd: &str) -> Option<std::path::PathBuf> {
    if cmd.contains('/') {
        let p = std::path::PathBuf::from(cmd);
        return is_exec(&p).then_some(p);
    }
    let path = std::env::var_os("PATH")?;
    std::env::split_paths(&path).map(|d| d.join(cmd)).find(|p| is_exec(p))
}

fn is_exec(p: &std::path::Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(p).map(|m| m.is_file() && m.permissions().mode() & 0o111 != 0).unwrap_or(false)
}

pub fn have(cmd: &str) -> bool {
    which(cmd).is_some()
}

/// Ergebnis eines Befehls mit Zeitlimit.
pub struct Output {
    pub status: Option<i32>,
    pub stdout: String,
    pub stderr: String,
}

/// Befehl ausführen und höchstens `timeout` warten (danach beendet); None, wenn er nicht startet.
pub fn run_timeout(cmd: &mut Command, timeout: Duration) -> Option<Output> {
    cmd.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
    let child = cmd.spawn().ok()?;
    let pid = child.id() as libc::pid_t;
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let _ = tx.send(child.wait_with_output());
    });
    match rx.recv_timeout(timeout) {
        Ok(Ok(out)) => Some(Output {
            status: out.status.code(),
            stdout: String::from_utf8_lossy(&out.stdout).into_owned(),
            stderr: String::from_utf8_lossy(&out.stderr).into_owned(),
        }),
        Ok(Err(_)) => None,
        Err(_) => {
            // Zeitlimit: Prozess beenden (der Lese-Thread endet dann von selbst)
            unsafe {
                libc::kill(pid, libc::SIGKILL);
            }
            None
        }
    }
}

/// Zugang zu PipeWire über pactl – austauschbar für Tests.
pub trait Pactl: Send + Sync {
    /// `pactl <args>`: Ausgabe ohne Leerraum am Rand; Fehler mit der Meldung von pactl.
    fn run(&self, args: &[String]) -> Result<String, Error>;
    /// Rohe Ausgabe von `pactl list <what>` (leer, wenn pactl nicht antwortet).
    fn list(&self, what: &str) -> String;
    /// Zerlegte Einträge von `pactl list <what>`.
    fn blocks(&self, what: &str) -> Vec<Block> {
        parse_blocks(&self.list(what))
    }
    /// Gibt es pactl?
    fn available(&self) -> bool {
        true
    }
}

/// Das echte pactl.
#[derive(Debug, Default, Clone, Copy)]
pub struct SystemPactl;

impl Pactl for SystemPactl {
    fn run(&self, args: &[String]) -> Result<String, Error> {
        let read = args.first().is_some_and(|a| READ_ONLY.contains(&a.as_str()));
        let mut cmd = Command::new("pactl");
        cmd.args(args);
        if read {
            english_env(&mut cmd);
        }
        let Some(out) = run_timeout(&mut cmd, Duration::from_secs(5)) else {
            return Err(Error(tr!("pactl error")));
        };
        if out.status != Some(0) {
            let msg = if !out.stderr.trim().is_empty() { out.stderr } else { out.stdout };
            let msg = msg.trim().to_string();
            return Err(Error(if msg.is_empty() { tr!("pactl error") } else { msg }));
        }
        Ok(out.stdout.trim().to_string())
    }

    fn list(&self, what: &str) -> String {
        let mut cmd = Command::new("pactl");
        cmd.args(["list", what]);
        english_env(&mut cmd);
        run_timeout(&mut cmd, Duration::from_secs(3)).map(|o| o.stdout).unwrap_or_default()
    }

    fn available(&self) -> bool {
        have("pactl")
    }
}

// ------------------------------------------------------------------ Zwischenspeicher
#[derive(Default)]
struct Snap {
    depth: usize,
    runs: HashMap<Vec<String>, Result<String, Error>>,
    lists: HashMap<String, Arc<Vec<Block>>>,
}

thread_local! {
    static SNAP: RefCell<Option<Snap>> = const { RefCell::new(None) };
}

/// Innerhalb von `f` liefern gleiche lesende Abfragen das erste Ergebnis wieder, statt pactl jedes Mal
/// neu zu starten (wie `system.pactl_snapshot`). Gilt nur für den eigenen Thread; schreibende Befehle
/// leeren den Zwischenspeicher.
pub fn snapshot<R>(f: impl FnOnce() -> R) -> R {
    SNAP.with(|s| {
        let mut s = s.borrow_mut();
        s.get_or_insert_with(Snap::default).depth += 1;
    });
    struct Guard;
    impl Drop for Guard {
        fn drop(&mut self) {
            SNAP.with(|s| {
                let mut s = s.borrow_mut();
                let done = match s.as_mut() {
                    Some(snap) => {
                        snap.depth -= 1;
                        snap.depth == 0
                    }
                    None => false,
                };
                if done {
                    *s = None;
                }
            });
        }
    }
    let _g = Guard;
    f()
}

fn in_snapshot() -> bool {
    SNAP.with(|s| s.borrow().is_some())
}

/// pactl-Befehl über den Zwischenspeicher (`pactl_cached(("run", *args), …)`).
pub fn cached_run(p: &dyn Pactl, args: &[String]) -> Result<String, Error> {
    let read = args.first().is_some_and(|a| READ_ONLY.contains(&a.as_str()));
    if !in_snapshot() {
        return p.run(args);
    }
    if !read {
        SNAP.with(|s| {
            if let Some(snap) = s.borrow_mut().as_mut() {
                snap.runs.clear();
                snap.lists.clear();
            }
        });
        return p.run(args);
    }
    if let Some(hit) = SNAP.with(|s| s.borrow().as_ref().and_then(|snap| snap.runs.get(args).cloned())) {
        return hit;
    }
    let r = p.run(args);
    SNAP.with(|s| {
        if let Some(snap) = s.borrow_mut().as_mut() {
            snap.runs.insert(args.to_vec(), r.clone());
        }
    });
    r
}

/// `pactl list <what>` zerlegt – innerhalb von [`snapshot`] nur einmal (wie `system._pactl_blocks`).
pub fn blocks(p: &dyn Pactl, what: &str) -> Arc<Vec<Block>> {
    if let Some(hit) = SNAP.with(|s| s.borrow().as_ref().and_then(|snap| snap.lists.get(what).cloned())) {
        return hit;
    }
    let b = Arc::new(p.blocks(what));
    SNAP.with(|s| {
        if let Some(snap) = s.borrow_mut().as_mut() {
            snap.lists.insert(what.to_string(), b.clone());
        }
    });
    b
}

/// Gibt es die Quelle `name`? Fragt jedes Mal neu, auch innerhalb von [`snapshot`]
/// (Warten auf die Quelle eines eben gestarteten Filter-Prozesses).
pub fn source_exists(p: &dyn Pactl, name: &str) -> bool {
    SNAP.with(|s| {
        if let Some(snap) = s.borrow_mut().as_mut() {
            snap.lists.remove("sources");
        }
    });
    blocks(p, "sources").iter().any(|b| b.name() == name)
}

/// Nur Kleinbuchstaben und Ziffern (wie `system._norm`).
pub fn norm(s: &str) -> String {
    s.to_lowercase().chars().filter(|c| c.is_ascii_lowercase() || c.is_ascii_digit()).collect()
}

/// Wiedergabe-Streams eines Programms (wie `system.app_streams`).
pub fn app_streams(p: &dyn Pactl, app: &str) -> Vec<Block> {
    // PureWindowsPath(app).name → .stem: Ordner (/ oder \) und letzte Endung weg
    let base = app.rsplit(['/', '\\']).next().unwrap_or("");
    let stem = match base.rfind('.') {
        Some(i) if i > 0 => &base[..i],
        _ => base,
    };
    let n = norm(stem);
    if n.is_empty() || !p.available() {
        return Vec::new();
    }
    blocks(p, "sink-inputs")
        .iter()
        .filter(|b| {
            ["application.name", "application.process.binary", "application.id", "node.name"].iter().any(|k| {
                let x = b.prop(k);
                if x.is_empty() {
                    return false;
                }
                let nx = norm(x);
                n.contains(&nx) || nx.contains(&n)
            })
        })
        .cloned()
        .collect()
}

// ------------------------------------------------------------------ PulseWatch
/// Meldet Änderungen in PipeWire/PulseAudio über `pactl subscribe` – statt ständig nachzufragen
/// (wie `system.PulseWatch`). `version` steigt bei jeder relevanten Änderung, `new_streams` bei jedem
/// neuen Wiedergabe-Stream. Läuft pactl nicht, ist `available()` false – dann fragt man regelmäßig.
pub struct PulseWatch {
    inner: Arc<WatchInner>,
}

struct WatchInner {
    version: Mutex<u64>,
    cond: Condvar,
    new_streams: AtomicU64,
    available: AtomicBool,
    running: AtomicBool,
    pid: Mutex<Option<u32>>,
}

const WATCH_KINDS: [&str; 6] = ["sink", "source", "sink-input", "module", "server", "card"];
const WATCH_GIVE_UP: u32 = 5;

/// Eine Zeile von `pactl subscribe` → (Ereignis, Art), z. B. ("new", "sink-input").
pub fn parse_event(line: &str) -> Option<(String, String)> {
    static RE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let re = RE.get_or_init(|| regex::Regex::new(r"^Event '(\w+)' on ([\w-]+)").unwrap());
    let c = re.captures(line.trim())?;
    let kind = c[2].to_string();
    WATCH_KINDS.contains(&kind.as_str()).then(|| (c[1].to_string(), kind))
}

impl Default for PulseWatch {
    fn default() -> Self {
        Self::new()
    }
}

impl PulseWatch {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(WatchInner {
                version: Mutex::new(0),
                cond: Condvar::new(),
                new_streams: AtomicU64::new(0),
                available: AtomicBool::new(false),
                running: AtomicBool::new(false),
                pid: Mutex::new(None),
            }),
        }
    }

    pub fn start(&self) -> &Self {
        if self.inner.running.load(Ordering::SeqCst) || !have("pactl") {
            return self;
        }
        self.inner.running.store(true, Ordering::SeqCst);
        let inner = self.inner.clone();
        let _ = std::thread::Builder::new().name("pulse-watch".into()).spawn(move || watch_loop(&inner));
        self
    }

    pub fn stop(&self) {
        self.inner.running.store(false, Ordering::SeqCst);
        if let Some(pid) = *self.inner.pid.lock().unwrap() {
            unsafe {
                libc::kill(pid as libc::pid_t, libc::SIGTERM);
            }
        }
        let _g = self.inner.version.lock().unwrap();
        self.inner.cond.notify_all();
    }

    pub fn version(&self) -> u64 {
        *self.inner.version.lock().unwrap()
    }
    pub fn new_streams(&self) -> u64 {
        self.inner.new_streams.load(Ordering::SeqCst)
    }
    pub fn available(&self) -> bool {
        self.inner.available.load(Ordering::SeqCst)
    }
    pub fn running(&self) -> bool {
        self.inner.running.load(Ordering::SeqCst)
    }

    /// Bis zu `timeout` warten, bis die Version größer als `since` ist; gibt die Version zurück.
    pub fn wait(&self, since: u64, timeout: Duration) -> u64 {
        let g = self.inner.version.lock().unwrap();
        if *g > since {
            return *g;
        }
        let (g, _) = self.inner.cond.wait_timeout(g, timeout).unwrap();
        *g
    }
}

impl WatchInner {
    fn bump(&self, new_stream: bool) {
        let mut v = self.version.lock().unwrap();
        *v += 1;
        if new_stream {
            self.new_streams.fetch_add(1, Ordering::SeqCst);
        }
        self.cond.notify_all();
    }
}

fn watch_loop(w: &WatchInner) {
    let mut delay = 1.0f64;
    let mut quick = 0u32;
    while w.running.load(Ordering::SeqCst) {
        let started = Instant::now();
        let mut cmd = Command::new("pactl");
        cmd.arg("subscribe").stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null());
        english_env(&mut cmd);
        if let Ok(mut child) = cmd.spawn() {
            *w.pid.lock().unwrap() = Some(child.id());
            w.available.store(true, Ordering::SeqCst);
            if let Some(out) = child.stdout.take() {
                for line in BufReader::new(out).lines() {
                    let Ok(line) = line else { break };
                    if let Some((ev, kind)) = parse_event(&line) {
                        w.bump(kind == "sink-input" && ev == "new");
                    }
                    if !w.running.load(Ordering::SeqCst) {
                        break;
                    }
                }
            }
            let _ = child.kill();
            let _ = child.wait();
            *w.pid.lock().unwrap() = None;
        }
        w.available.store(false, Ordering::SeqCst);
        w.bump(false); // Zustand unbekannt: einmal neu einlesen lassen
        if !w.running.load(Ordering::SeqCst) {
            break;
        }
        let lasted = started.elapsed().as_secs_f64();
        quick = if lasted > 5.0 { 0 } else { quick + 1 };
        if quick >= WATCH_GIVE_UP {
            log::debug!("pactl subscribe endet immer sofort – FreeWave fragt wieder regelmäßig nach");
            w.running.store(false, Ordering::SeqCst);
            break;
        }
        delay = if lasted > 30.0 { 1.0 } else { (delay * 2.0).min(30.0) };
        let g = w.version.lock().unwrap();
        let _ = w.cond.wait_timeout(g, Duration::from_secs_f64(delay));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_header_and_fields() {
        let out = "Sink Input #42\n\tDriver: PipeWire\n\tSink: 57\n\tMute: no\n\tVolume: front-left: 65536 / 100% / 0,00 dB\n";
        let b = &parse_blocks(out)[0];
        assert_eq!((b.index.as_str(), b.sink.as_deref(), b.volume), ("42", Some("57"), Some(100)));
    }

    #[test]
    fn events() {
        assert_eq!(parse_event("Event 'new' on sink-input #123"), Some(("new".into(), "sink-input".into())));
        assert_eq!(parse_event("Event 'change' on client #5"), None);
    }

    #[test]
    fn app_stream_names() {
        assert_eq!(norm("Firefox-ESR"), "firefoxesr");
    }
}
