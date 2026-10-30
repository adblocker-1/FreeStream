//! Automatische Updates aus den GitHub-Releases (wie `freestream/updater.py`).
//!
//! Beim Start fragt FreeDeck nach dem neuesten Release. Ist eine neuere Version da, lädt das Programm
//! nach Rückfrage die .deb-Dateien der installierten Pakete herunter, prüft ihre SHA-256-Summe und
//! installiert sie mit `pkexec apt-get install`.
//!
//! Die Rust-Version heißt als Paket `freedeck-rs` (Programm `/usr/bin/freedeck-rs`, Datei
//! `freedeck-rs_<Version>_<Architektur>[_<System>].deb`). Sind daneben die Python-Pakete installiert
//! (freestream, freedeck, freewave oder ihre alten Namen), werden sie wie in der Python-Version im
//! selben Schritt mit aktualisiert – alle Programme teilen dieselben Einstellungen und bleiben so auf
//! einem Stand. Updates gibt es nur für das installierte Paket, nicht für einen Start aus dem Quellcode.
//!
//! Abschalten: config.json `"check_updates": false` oder `FREESTREAM_NO_UPDATE_CHECK=1`.
//! Kanal (`"update_channel"`): `"stable"` nimmt /releases/latest, `"beta"` das neueste Release der Liste
//! einschließlich Vorabversionen (nie Entwürfe). Ältere Versionen zum Zurückstufen kommen aus derselben
//! Liste (apt-get mit `--allow-downgrades`).
//!
//! Zustand (übersprungen, „Später“, gerade installiert) steht in `STATE_DIR/update.json` – dieselbe Datei
//! wie in der Python-Version, damit FreeWave (Python) und FreeDeck (Rust) nicht doppelt fragen.
//!
//! Alle Netzaufrufe blockieren; die Oberfläche startet sie mit den `*_in_background`-Funktionen und
//! fragt den zurückgegebenen Kanal ab.

use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::Receiver;
use std::time::Duration;

use crate::background::{self, Event};
use crate::{config, paths, tr, VERSION};

/// Das Repository hieß bis 0.28 „Streamlin“: erst der neue Name, sonst der alte (GitHub leitet nach dem
/// Umbenennen vom alten auf den neuen um). Downloads werden von beiden Adressen angenommen.
pub const REPOS: [&str; 2] = ["adblocker-1/FreeStream", "adblocker-1/Streamlin"];
pub const REPO: &str = REPOS[0];
/// Pakete der Python-Version.
pub const NEW_PACKAGES: [&str; 3] = ["freestream", "freedeck", "freewave"];
/// Pakete unter den Namen vor der Umbenennung: werden beim nächsten Update durch die neuen ersetzt.
pub const LEGACY: [(&str, &str); 3] = [("streamlin", "freestream"), ("lindeck", "freedeck"), ("linewave", "freewave")];
/// Paket und Programm der Rust-Version.
pub const RUST_PACKAGE: &str = "freedeck-rs";
pub const RUST_EXE: &str = "/usr/bin/freedeck-rs";
/// „Später“: das zweite Programm fragt beim gleichen Anmelden nicht noch einmal.
pub const LATER_SECONDS: f64 = 15.0 * 60.0;
pub const STATE_FILE: &str = "update.json";
pub const CHANNELS: [&str; 2] = ["stable", "beta"];
/// Wartezeit für Anfragen an die GitHub-API.
pub const TIMEOUT: Duration = Duration::from_secs(10);
/// Wartezeit beim Herunterladen (Verbindung und Antwort).
pub const DOWNLOAD_TIMEOUT: Duration = Duration::from_secs(30);

pub fn api_urls() -> Vec<String> {
    REPOS.iter().map(|r| format!("https://api.github.com/repos/{r}/releases/latest")).collect()
}

pub fn releases_urls() -> Vec<String> {
    REPOS.iter().map(|r| format!("https://api.github.com/repos/{r}/releases?per_page=100")).collect()
}

pub fn download_prefixes() -> Vec<String> {
    REPOS.iter().map(|r| format!("https://github.com/{r}/releases/download/")).collect()
}

/// Alle Paketnamen, nach denen dpkg gefragt wird.
pub fn packages() -> Vec<&'static str> {
    NEW_PACKAGES.iter().copied().chain(LEGACY.iter().map(|(old, _)| *old)).chain([RUST_PACKAGE]).collect()
}

fn legacy_new_name(pkg: &str) -> &str {
    LEGACY.iter().find(|(old, _)| *old == pkg).map(|(_, new)| *new).unwrap_or(pkg)
}

fn legacy_old_name(pkg: &str) -> Option<&'static str> {
    LEGACY.iter().find(|(_, new)| *new == pkg).map(|(old, _)| *old)
}

// ------------------------------------------------------------------ Einstellung
fn read_config() -> Map<String, Value> {
    config::read_file().unwrap_or_default()
}

/// Einstellung „Beim Start nach Updates suchen“ (Standard: ja).
pub fn configured() -> bool {
    read_config().get("check_updates") != Some(&Value::Bool(false))
}

/// Beim Start nach Updates suchen? `FREESTREAM_NO_UPDATE_CHECK` schaltet es für einen Start ab.
pub fn enabled() -> bool {
    std::env::var_os("FREESTREAM_NO_UPDATE_CHECK").filter(|v| !v.is_empty()).is_none() && configured()
}

/// Wie `Config` behält das die Rechte (Zugangsdaten) und schreibt nie über eine unlesbare Datei.
pub fn set_enabled(on: bool) -> bool {
    config::save_keys(&[("check_updates", Value::Bool(on))])
}

/// Update-Kanal: "stable" (Standard) oder "beta".
pub fn channel() -> &'static str {
    match read_config().get("update_channel").and_then(Value::as_str) {
        Some("beta") => "beta",
        _ => "stable",
    }
}

pub fn set_channel(ch: &str) -> bool {
    let ch = if CHANNELS.contains(&ch) { ch } else { "stable" };
    config::save_keys(&[("update_channel", Value::String(ch.into()))])
}

// ------------------------------------------------------------------ Ausgabe: Python oder Rust (Beta)
/// Gewählte Ausgabe (config.json "edition", wie die Python-Version): "python" (Standard) oder "rust".
pub fn edition() -> &'static str {
    let env = std::env::var("FREESTREAM_EDITION").ok();
    let cfg = read_config();
    match env.as_deref().or_else(|| cfg.get("edition").and_then(Value::as_str)) {
        Some("rust") => "rust",
        _ => "python",
    }
}

pub fn set_edition(e: &str) -> bool {
    config::save_keys(&[("edition", Value::String(if e == "rust" { "rust" } else { "python" }.into()))])
}

/// Pakete, die das Update für die Ausgabe `ed` installiert: Rust – freedeck-rs (dazu die installierten
/// Python-Pakete, damit beide auf demselben Stand bleiben); Python – die installierten Python-Pakete, sonst
/// freestream (und freedeck-rs, falls installiert).
pub fn packages_for(ed: &str, installed: &BTreeMap<String, String>) -> Vec<String> {
    let mut out: Vec<String> = installed.keys().filter(|p| p.as_str() != RUST_PACKAGE).cloned().collect();
    if ed == "python" && out.is_empty() {
        out.push("freestream".into());
    }
    if ed == "rust" || installed.contains_key(RUST_PACKAGE) {
        out.push(RUST_PACKAGE.into());
    }
    out
}

/// Dasselbe Release mit den Paketen der Ausgabe `ed`; None, wenn es sie nicht (für dieses System) enthält.
pub fn update_for_edition(update: &Update, ed: &str, installed: &BTreeMap<String, String>, target: &Target) -> Option<Update> {
    make_update(&update.release, &packages_for(ed, installed), target)
}

// ------------------------------------------------------------------ Zustand (übersprungen, später, installiert)
fn state_path() -> PathBuf {
    paths::state_dir().join(STATE_FILE)
}

/// Inhalt einer update.json (leer, wenn es sie nicht gibt oder sie unlesbar ist).
pub fn load_state_from(f: &Path) -> Map<String, Value> {
    std::fs::read_to_string(f)
        .ok()
        .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default()
}

/// Werte ändern; `None` entfernt den Schlüssel.
pub fn save_state_to(f: &Path, changes: &[(&str, Option<Value>)]) -> std::io::Result<()> {
    let mut data = load_state_from(f);
    for (k, v) in changes {
        match v {
            Some(v) => {
                data.insert((*k).to_string(), v.clone());
            }
            None => {
                data.remove(*k);
            }
        }
    }
    let text = serde_json::to_string_pretty(&Value::Object(data)).unwrap_or_default();
    config::write_atomic(f, &text, 0o644)
}

pub fn load_state() -> Map<String, Value> {
    load_state_from(&state_path())
}

pub fn save_state(changes: &[(&str, Option<Value>)]) -> std::io::Result<()> {
    save_state_to(&state_path(), changes)
}

fn now() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// „Diese Version überspringen“.
pub fn skip_version(version: &str) -> std::io::Result<()> {
    save_state(&[("skipped", Some(Value::String(version.into())))])
}

/// „Später“: für [`LATER_SECONDS`] nicht mehr nach dieser Version fragen.
pub fn remind_later(version: &str) -> std::io::Result<()> {
    save_state(&[("later", Some(later_entry(version, now())))])
}

fn later_entry(version: &str, now: f64) -> Value {
    serde_json::json!({"version": version, "until": now + LATER_SECONDS})
}

/// Hat der Benutzer diese Version übersprungen oder gerade „Später“ gewählt? (laut Zustand `st`)
pub fn is_suppressed(st: &Map<String, Value>, version: &str, now: f64) -> bool {
    if st.get("skipped").and_then(Value::as_str) == Some(version) {
        return true;
    }
    let Some(later) = st.get("later").and_then(Value::as_object) else { return false };
    later.get("version").and_then(Value::as_str) == Some(version)
        && now < later.get("until").and_then(Value::as_f64).unwrap_or(0.0)
}

/// Hat der Benutzer diese Version übersprungen oder gerade „Später“ gewählt?
pub fn suppressed(version: &str) -> bool {
    is_suppressed(&load_state(), version, now())
}

/// Version, auf die gerade aktualisiert wurde (einmalig), sonst None.
pub fn just_updated() -> Option<String> {
    let st = load_state();
    if st.get("installing").and_then(Value::as_str) == Some(VERSION) {
        let _ = save_state(&[("installing", None)]);
        return Some(VERSION.to_string());
    }
    None
}

// ------------------------------------------------------------------ Versionen
/// Zahlen einer Version ("0.20.1-beta.2" → [0, 20, 1]), höchstens vier.
pub fn parse_version(text: &str) -> Vec<u64> {
    let main = text.split('-').next().unwrap_or("").split('+').next().unwrap_or("");
    digit_runs(main).into_iter().take(4).map(|n| n.parse().unwrap_or(u64::MAX)).collect()
}

fn digit_runs(text: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = None;
    for (i, c) in text.char_indices() {
        match (c.is_ascii_digit(), start) {
            (true, None) => start = Some(i),
            (false, Some(s)) => {
                out.push(&text[s..i]);
                start = None;
            }
            _ => {}
        }
    }
    if let Some(s) = start {
        out.push(&text[s..]);
    }
    out
}

/// Sortierschlüssel einer Version (wie `version_key` in Python): Vorabversion (0.20.0-beta.1) vor der
/// fertigen Version (0.20.0); Teile der Vorabversion: Zahlen vor Wörtern, Wörter ohne Groß/klein.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct VersionKey {
    pub nums: Vec<u64>,
    /// (0, Teile) für eine Vorabversion, (1, []) für eine fertige Version.
    pub pre: (u8, Vec<(u8, u64, String)>),
}

pub fn version_key(text: &str) -> VersionKey {
    let text = text.trim().trim_start_matches(['v', 'V']);
    let text = text.split('+').next().unwrap_or("").replace('~', "-");
    let (main, pre) = text.split_once('-').unwrap_or((&text, ""));
    let nums = parse_version(main);
    if pre.is_empty() {
        return VersionKey { nums, pre: (1, Vec::new()) };
    }
    // Teile: Zahlenfolgen und Buchstabenfolgen (alles andere trennt nur)
    let mut parts = Vec::new();
    let mut cur = String::new();
    let flush = |cur: &mut String, parts: &mut Vec<(u8, u64, String)>| {
        if cur.is_empty() {
            return;
        }
        if cur.starts_with(|c: char| c.is_ascii_digit()) {
            parts.push((0, cur.parse().unwrap_or(u64::MAX), String::new()));
        } else {
            parts.push((1, 0, cur.to_lowercase()));
        }
        cur.clear();
    };
    for c in pre.chars() {
        let same = cur.chars().next().map(|f| f.is_ascii_digit() == c.is_ascii_digit()).unwrap_or(true);
        if c.is_ascii_digit() || c.is_ascii_alphabetic() {
            if !same {
                flush(&mut cur, &mut parts);
            }
            cur.push(c);
        } else {
            flush(&mut cur, &mut parts);
        }
    }
    flush(&mut cur, &mut parts);
    VersionKey { nums, pre: (0, parts) }
}

/// Ist `candidate` neuer als `current`?
pub fn is_newer(candidate: &str, current: &str) -> bool {
    let a = version_key(candidate);
    !a.nums.is_empty() && a > version_key(current)
}

/// Ist `candidate` älter als `current`?
pub fn is_older(candidate: &str, current: &str) -> bool {
    let a = version_key(candidate);
    !a.nums.is_empty() && a < version_key(current)
}

// ------------------------------------------------------------------ installierte Pakete
/// Ausführbares Programm im PATH.
pub fn which(name: &str) -> Option<PathBuf> {
    use std::os::unix::fs::PermissionsExt;
    let path = std::env::var_os("PATH")?;
    std::env::split_paths(&path).map(|d| d.join(name)).find(|p| {
        std::fs::metadata(p).map(|m| m.is_file() && m.permissions().mode() & 0o111 != 0).unwrap_or(false)
    })
}

fn query(cmd: &str, args: &[&str]) -> String {
    Command::new(cmd)
        .args(args)
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).into_owned())
        .unwrap_or_default()
}

/// Antwort von `dpkg-query -W -f='${Package}\t${Status}\t${Version}\n'` auswerten.
pub fn parse_dpkg(output: &str) -> BTreeMap<String, String> {
    let known = packages();
    let mut found = BTreeMap::new();
    for line in output.lines() {
        let parts: Vec<&str> = line.split('\t').collect();
        if parts.len() == 3 && known.contains(&parts[0]) && parts[1].ends_with(" installed") {
            found.insert(parts[0].to_string(), parts[2].to_string());
        }
    }
    found
}

/// Per dpkg installierte FreeStream-Pakete (Python und Rust): {Paket: Version}.
pub fn dpkg_packages() -> BTreeMap<String, String> {
    if which("dpkg-query").is_none() {
        return BTreeMap::new();
    }
    let mut args = vec!["-W", "-f=${Package}\t${Status}\t${Version}\n"];
    args.extend(packages());
    parse_dpkg(&query("dpkg-query", &args))
}

/// Läuft dieses Programm als `/usr/bin/freedeck-rs` (aus dem Paket)?
pub fn runs_from_package() -> bool {
    let exe = std::env::current_exe().ok().and_then(|p| p.canonicalize().ok());
    let pkg = Path::new(RUST_EXE).canonicalize().ok();
    exe.is_some() && exe == pkg
}

/// Installierte FreeStream-Pakete, die dieses Programm aktualisiert: {Paket: Version}.
///
/// Leer, wenn das Programm nicht als installiertes `freedeck-rs` läuft (z. B. aus dem Quellcode, mit
/// `cargo run`); dann zeigt die Oberfläche den Hinweis aus [`source_message`].
pub fn installed_packages() -> BTreeMap<String, String> {
    if !runs_from_package() {
        return BTreeMap::new();
    }
    let found = dpkg_packages();
    if !found.contains_key(RUST_PACKAGE) {
        return BTreeMap::new(); // nur kopiert, nicht per apt installiert
    }
    found
}

/// Läuft das Programm aus dem Quellcode (keine Updates möglich)?
pub fn running_from_source() -> bool {
    installed_packages().is_empty()
}

/// Architektur und System, für die die Rust-Pakete gebaut sind.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Target {
    /// Debian-Architektur, z. B. "amd64".
    pub arch: String,
    /// System aus /etc/os-release, z. B. "ubuntu-24.04" oder "debian-12".
    pub distro: Option<String>,
}

impl Target {
    pub fn detect() -> Self {
        let arch = query("dpkg", &["--print-architecture"]).trim().to_string();
        let arch = if arch.is_empty() {
            match std::env::consts::ARCH {
                "x86_64" => "amd64",
                "aarch64" => "arm64",
                "x86" => "i386",
                "arm" => "armhf",
                other => other,
            }
            .to_string()
        } else {
            arch
        };
        let distro = std::fs::read_to_string("/etc/os-release").ok().and_then(|t| distro_from_os_release(&t));
        Self { arch, distro }
    }
}

/// "ubuntu-24.04" aus dem Inhalt von /etc/os-release.
pub fn distro_from_os_release(text: &str) -> Option<String> {
    let field = |key: &str| {
        text.lines()
            .find_map(|l| l.strip_prefix(key).and_then(|r| r.strip_prefix('=')))
            .map(|v| v.trim().trim_matches(['"', '\'']).to_string())
            .filter(|v| !v.is_empty())
    };
    Some(format!("{}-{}", field("ID")?, field("VERSION_ID")?))
}

// ------------------------------------------------------------------ Release auswerten
/// Eine Datei eines Updates.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UpdateFile {
    pub name: String,
    pub url: String,
    pub sha256: String,
    pub size: u64,
}

/// Ein Release mit den passenden .deb-Dateien.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Update {
    /// z. B. "0.40.0"
    pub version: String,
    /// englischer Teil der Release-Notizen
    pub notes: String,
    /// Release-Seite im Browser
    pub page: String,
    pub files: Vec<UpdateFile>,
    /// Veröffentlichung, z. B. "2026-10-01"
    pub date: String,
    pub prerelease: bool,
    /// Antwort der API (für den Wechsel der Ausgabe im Dialog)
    pub release: Value,
}

impl Update {
    /// Ältere Version als die laufende (Zurückstufen)?
    pub fn downgrade(&self) -> bool {
        is_older(&self.version, VERSION)
    }
}

fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

fn str_field<'a>(v: &'a Value, key: &str) -> &'a str {
    v.get(key).and_then(Value::as_str).unwrap_or("")
}

fn tag_version(release: &Value) -> String {
    str_field(release, "tag_name").trim_start_matches('v').to_string()
}

/// Nur der englische Teil der Release-Notizen (vor dem „---“), ohne die Überschrift „English“.
pub fn english_notes(body: &str) -> String {
    let text = body.split("\n---").next().unwrap_or("");
    let text: Vec<&str> = text.split('\n').map(|l| if l.trim() == "**English**" { "" } else { l }).collect();
    let text = text.join("\n");
    // drei und mehr Zeilenumbrüche → zwei
    let mut out = String::with_capacity(text.len());
    let mut newlines = 0;
    for c in text.chars() {
        if c == '\n' {
            newlines += 1;
            if newlines <= 2 {
                out.push(c);
            }
        } else {
            newlines = 0;
            out.push(c);
        }
    }
    out.trim().to_string()
}

/// Mögliche Dateinamen eines Pakets im Release (Vorabversion auch mit ~ statt -). Die Python-Pakete
/// sind architekturunabhängig (`_all`), das Rust-Paket gibt es je Architektur, vielleicht zusätzlich je
/// System (`freedeck-rs_0.40.0_amd64_ubuntu-24.04.deb`; das passende System kommt zuerst).
pub fn asset_names(pkg: &str, version: &str, target: &Target) -> Vec<String> {
    let mut versions = vec![version.to_string()];
    let tilde = version.replace('-', "~");
    if tilde != version {
        versions.push(tilde);
    }
    let mut out = Vec::new();
    for v in versions {
        if pkg == RUST_PACKAGE {
            if let Some(d) = &target.distro {
                out.push(format!("{pkg}_{v}_{}_{d}.deb", target.arch));
            }
            out.push(format!("{pkg}_{v}_{}.deb", target.arch));
        } else {
            out.push(format!("{pkg}_{v}_all.deb"));
        }
    }
    out
}

/// Dateien der Pakete eines Releases; None, wenn eines fehlt oder eine Datei nicht sicher ist
/// (fremde Adresse, keine SHA-256-Summe).
pub fn release_files<S: AsRef<str>>(release: &Value, packages: &[S], target: &Target) -> Option<Vec<UpdateFile>> {
    let version = tag_version(release);
    let mut assets: BTreeMap<&str, &Value> = BTreeMap::new();
    for a in release.get("assets").and_then(Value::as_array).into_iter().flatten() {
        if let Some(n) = a.get("name").and_then(Value::as_str) {
            assets.insert(n, a);
        }
    }
    let prefixes = download_prefixes();
    let wanted: BTreeSet<&str> = packages.iter().map(|p| legacy_new_name(p.as_ref())).collect();
    let mut files = Vec::new();
    for pkg in wanted {
        // neuer Paketname; ältere Releases (vor der Umbenennung) haben nur den alten
        let mut names = asset_names(pkg, &version, target);
        if let Some(old) = legacy_old_name(pkg) {
            names.extend(asset_names(old, &version, target));
        }
        let Some(a) = names.iter().find_map(|n| assets.get(n.as_str())) else {
            log::info!("Release {version} has no package {pkg}");
            return None;
        };
        let url = str_field(a, "browser_download_url");
        let digest = str_field(a, "digest");
        if !prefixes.iter().any(|p| url.starts_with(p.as_str())) || !digest.starts_with("sha256:") {
            return None;
        }
        let size = a.get("size").and_then(|s| s.as_u64().or_else(|| s.as_f64().map(|f| f as u64))).unwrap_or(0);
        files.push(UpdateFile {
            name: str_field(a, "name").to_string(),
            url: url.to_string(),
            sha256: digest["sha256:".len()..].to_lowercase(),
            size,
        });
    }
    Some(files)
}

/// Update für genau dieses Release (ohne Versionsvergleich) oder None.
pub fn make_update<S: AsRef<str>>(release: &Value, packages: &[S], target: &Target) -> Option<Update> {
    if !release.is_object() || truthy(release.get("draft")) || packages.is_empty() {
        return None;
    }
    let files = release_files(release, packages, target)?;
    let date = Some(str_field(release, "published_at"))
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| str_field(release, "created_at"));
    Some(Update {
        version: tag_version(release),
        notes: english_notes(str_field(release, "body")),
        page: str_field(release, "html_url").to_string(),
        files,
        date: date.chars().take(10).collect(),
        prerelease: truthy(release.get("prerelease")),
        release: release.clone(),
    })
}

/// Update aus der API-Antwort, wenn sie neuer ist und alle Pakete enthält; sonst None.
pub fn release_update<S: AsRef<str>>(release: &Value, packages: &[S], current: &str, beta: bool,
                                     target: &Target) -> Option<Update> {
    if !release.is_object() || truthy(release.get("draft")) || (truthy(release.get("prerelease")) && !beta) {
        return None;
    }
    if packages.is_empty() || !is_newer(&tag_version(release), current) {
        return None;
    }
    make_update(release, packages, target)
}

/// Veröffentlichte Releases (nie Entwürfe), neueste zuerst; Vorabversionen nur mit `beta`.
pub fn published(releases: &[Value], beta: bool) -> Vec<&Value> {
    let mut out: Vec<&Value> = releases
        .iter()
        .filter(|r| {
            r.is_object() && !str_field(r, "tag_name").is_empty() && !truthy(r.get("draft"))
                && (beta || !truthy(r.get("prerelease")))
        })
        .collect();
    // stabil wie sorted(..., reverse=True) in Python
    out.sort_by_key(|r| std::cmp::Reverse(version_key(str_field(r, "tag_name"))));
    out
}

/// Beta-Kanal: das neueste Release der Liste (auch eine Vorabversion), wenn es neuer ist.
pub fn newest_update<S: AsRef<str>>(releases: &[Value], packages: &[S], current: &str, beta: bool,
                                   target: &Target) -> Option<Update> {
    published(releases, beta).first().and_then(|r| release_update(r, packages, current, beta, target))
}

/// Ältere Versionen zum Zurückstufen, neueste zuerst (nur Releases mit allen Paketen).
pub fn earlier_versions<S: AsRef<str>>(releases: &[Value], packages: &[S], current: &str, beta: bool,
                                      target: &Target) -> Vec<Update> {
    published(releases, beta)
        .into_iter()
        .filter(|r| is_older(str_field(r, "tag_name"), current))
        .filter_map(|r| make_update(r, packages, target))
        .collect()
}

// ------------------------------------------------------------------ Fehler
/// Fehler beim Abfragen oder Herunterladen.
#[derive(Debug, thiserror::Error)]
pub enum UpdateError {
    /// Netz, HTTP-Status, TLS …
    #[error("{0}")]
    Network(String),
    /// Unerwartete Antwort von GitHub.
    #[error("unexpected answer from GitHub")]
    BadAnswer,
    /// Datei beschädigt (SHA-256 passt nicht) oder nicht sicher; enthält den Dateinamen.
    #[error("{0}")]
    Damaged(String),
    #[error("{0}")]
    Io(#[from] std::io::Error),
}

impl From<ureq::Error> for UpdateError {
    fn from(e: ureq::Error) -> Self {
        match e {
            ureq::Error::Io(e) => UpdateError::Io(e),
            other => UpdateError::Network(other.to_string()),
        }
    }
}

impl UpdateError {
    /// Text für „Nach Updates suchen“.
    pub fn check_message(&self) -> String {
        tr!("Could not check for updates: {error}", error = self)
    }

    /// Text, wenn das Herunterladen scheitert.
    pub fn download_message(&self) -> String {
        match self {
            UpdateError::Damaged(file) => tr!("The download is damaged ({file}). Nothing was installed.", file = file),
            other => tr!("Download failed: {error}", error = other),
        }
    }

    /// Text, wenn die Liste älterer Versionen nicht geladen werden kann.
    pub fn versions_message(&self) -> String {
        tr!("Could not load the list of versions: {error}", error = self)
    }
}

/// „Sie verwenden die neueste Version“.
pub fn latest_message() -> String {
    tr!("You are using the latest version ({version}).", version = VERSION)
}

/// Hinweis, wenn sich diese Kopie nicht selbst aktualisieren kann (`app`: z. B. "FreeDeck").
pub fn source_message(app: &str) -> String {
    tr!("Automatic updates work for the installed .deb packages only. This copy of {app} was started from the source code.",
        app = app)
}

/// Text, wenn es keine älteren Versionen gibt.
pub fn no_earlier_message() -> String {
    tr!("No earlier versions are available.")
}

/// Benachrichtigung nach einem Update: (Titel, Text).
pub fn updated_notification(version: &str) -> (String, String) {
    (tr!("FreeStream was updated"), tr!("Version {version} is installed.", version = version))
}

// ------------------------------------------------------------------ Release abfragen
fn agent(timeout: Duration) -> ureq::Agent {
    // Proxy aus HTTPS_PROXY/ALL_PROXY/NO_PROXY ist bei ureq Standard
    ureq::Agent::config_builder()
        .timeout_global(Some(timeout))
        .user_agent(format!("FreeStream/{VERSION}"))
        .build()
        .into()
}

/// Größte erlaubte Antwort der API (100 Releases mit Notizen).
const MAX_JSON: u64 = 32 << 20;

fn get_json(agent: &ureq::Agent, url: &str) -> Result<Value, ureq::Error> {
    let mut resp = agent.get(url).header("Accept", "application/vnd.github+json").call()?;
    let text = resp.body_mut().with_config().limit(MAX_JSON).read_to_string()?;
    serde_json::from_str(&text).map_err(|e| ureq::Error::Io(std::io::Error::new(std::io::ErrorKind::InvalidData, e)))
}

/// Die erste Adresse, die antwortet (neuer Repo-Name, sonst bei 404 der alte).
fn get_first(urls: &[String], timeout: Duration) -> Result<Value, UpdateError> {
    let agent = agent(timeout);
    let (last, rest) = urls.split_last().expect("Adressen");
    for url in rest {
        match get_json(&agent, url) {
            Ok(v) => return Ok(v),
            Err(ureq::Error::StatusCode(404)) => continue,
            Err(e) => return Err(e.into()),
        }
    }
    get_json(&agent, last).map_err(|e| match e {
        ureq::Error::Io(e) if e.kind() == std::io::ErrorKind::InvalidData => UpdateError::BadAnswer,
        e => e.into(),
    })
}

/// Liste der Releases (GitHub-API).
pub fn fetch_releases(timeout: Duration) -> Result<Vec<Value>, UpdateError> {
    match get_first(&releases_urls(), timeout)? {
        Value::Array(a) => Ok(a),
        _ => Err(UpdateError::BadAnswer),
    }
}

/// Neuestes fertige Release (GitHub-API /releases/latest).
pub fn fetch_latest(timeout: Duration) -> Result<Value, UpdateError> {
    get_first(&api_urls(), timeout)
}

/// Neuere Version für diese Pakete oder None (`beta`: Vorabversionen zulassen).
pub fn check_for<S: AsRef<str>>(packages: &[S], beta: bool, target: &Target) -> Result<Option<Update>, UpdateError> {
    if packages.is_empty() {
        return Ok(None);
    }
    if beta {
        return Ok(newest_update(&fetch_releases(TIMEOUT)?, packages, VERSION, true, target));
    }
    Ok(release_update(&fetch_latest(TIMEOUT)?, packages, VERSION, false, target))
}

/// Neuere Version für die installierten Pakete im eingestellten Kanal oder None.
pub fn check() -> Result<Option<Update>, UpdateError> {
    let packages: Vec<String> = installed_packages().into_keys().collect();
    if packages.is_empty() {
        return Ok(None);
    }
    check_for(&packages, channel() == "beta", &Target::detect())
}

/// Ältere Versionen für die installierten Pakete (fragt GitHub).
pub fn rollback_choices() -> Result<Vec<Update>, UpdateError> {
    let packages: Vec<String> = installed_packages().into_keys().collect();
    if packages.is_empty() {
        return Ok(Vec::new());
    }
    let releases = fetch_releases(TIMEOUT)?;
    Ok(earlier_versions(&releases, &packages, VERSION, channel() == "beta", &Target::detect()))
}

// ------------------------------------------------------------------ Herunterladen und installieren
/// Ordner für heruntergeladene Pakete.
pub fn download_dir() -> PathBuf {
    paths::state_dir().join("updates")
}

/// Lädt alle Dateien nach `dest` (Standard: [`download_dir`]), prüft SHA-256 und gibt die Pfade zurück.
/// `progress(fertig, gesamt)` in Bytes.
pub fn download(update: &Update, dest: Option<&Path>, progress: &mut dyn FnMut(u64, u64))
                -> Result<Vec<PathBuf>, UpdateError> {
    let dest = dest.map(Path::to_path_buf).unwrap_or_else(download_dir);
    std::fs::create_dir_all(&dest)?;
    for old in std::fs::read_dir(&dest)?.flatten() {
        if old.path().extension().is_some_and(|e| e == "deb") {
            let _ = std::fs::remove_file(old.path());
        }
    }
    let total = update.files.iter().map(|f| f.size).sum::<u64>().max(1);
    let prefixes = download_prefixes();
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .timeout_connect(Some(DOWNLOAD_TIMEOUT))
        .timeout_recv_response(Some(DOWNLOAD_TIMEOUT))
        .user_agent(format!("FreeStream/{VERSION}"))
        .build()
        .into();
    let mut done = 0u64;
    let mut out = Vec::new();
    for f in &update.files {
        if !prefixes.iter().any(|p| f.url.starts_with(p.as_str())) || f.name.contains('/') || f.name.is_empty() {
            return Err(UpdateError::Damaged(f.name.clone()));
        }
        let target = dest.join(&f.name);
        let resp = agent.get(&f.url).call()?;
        let mut reader = resp.into_body().into_reader();
        let mut fh = std::fs::File::create(&target)?;
        let mut h = Sha256::new();
        let mut buf = vec![0u8; 65536];
        loop {
            let n = match reader.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => n,
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(e) => {
                    let _ = std::fs::remove_file(&target);
                    return Err(e.into());
                }
            };
            fh.write_all(&buf[..n])?;
            h.update(&buf[..n]);
            done += n as u64;
            progress(done.min(total), total);
        }
        fh.flush()?;
        let hex: String = h.finalize().iter().map(|b| format!("{b:02x}")).collect();
        if hex != f.sha256 {
            let _ = std::fs::remove_file(&target);
            return Err(UpdateError::Damaged(f.name.clone()));
        }
        out.push(target);
    }
    Ok(out)
}

/// Befehl, der die Pakete mit Administratorrechten installiert, oder None (kein pkexec).
/// `--allow-downgrades`: dieselbe Zeile installiert auch eine ältere Version.
pub fn install_command(files: &[PathBuf]) -> Option<Vec<String>> {
    let pkexec = which("pkexec")?;
    let apt = which("apt-get").unwrap_or_else(|| PathBuf::from("/usr/bin/apt-get"));
    let mut cmd = vec![pkexec.to_string_lossy().into_owned(), apt.to_string_lossy().into_owned()];
    // ohne Terminal: bei geänderten Konfigurationsdateien nie nachfragen, die eigene Fassung behalten
    cmd.extend(["install", "-y", "--allow-downgrades", "-o", "Dpkg::Options::=--force-confdef", "-o",
                "Dpkg::Options::=--force-confold"].map(String::from));
    for f in files {
        let abs = f.canonicalize().unwrap_or_else(|_| f.clone());
        cmd.push(abs.to_string_lossy().into_owned());
    }
    Some(cmd)
}

/// Startet die Installation in einer eigenen Sitzung (überlebt das Beenden des Programms durch das Paket).
/// Ausgabe und Fehlerausgabe kommen zusammen über `stdout`. None, wenn kein pkexec da ist.
pub fn start_install(files: &[PathBuf], version: &str) -> std::io::Result<Option<Child>> {
    use std::os::unix::process::CommandExt;
    let Some(cmd) = install_command(files) else { return Ok(None) };
    save_state(&[("installing", Some(Value::String(version.into())))])?;
    let mut c = Command::new("/bin/sh");
    // sh legt nur die Fehlerausgabe auf die Ausgabe und ersetzt sich dann durch pkexec
    c.args(["-c", "exec \"$@\" 2>&1", "sh"]).args(&cmd).stdin(Stdio::null()).stdout(Stdio::piped())
        .stderr(Stdio::null());
    // SAFETY: setsid ist zwischen fork und exec erlaubt (async-signal-safe).
    unsafe {
        c.pre_exec(|| {
            libc::setsid();
            Ok(())
        });
    }
    c.spawn().map(Some)
}

/// Ergebnis einer Installation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum InstallOutcome {
    /// Installiert; das Programm sollte sich jetzt neu starten.
    Installed,
    /// Passwortabfrage abgebrochen oder nicht berechtigt (pkexec 126/127).
    Cancelled,
    /// apt-get ist gescheitert; letzte Zeile der Ausgabe.
    Failed(String),
    /// Der Befehl ließ sich nicht starten.
    StartFailed(String),
    /// Kein pkexec: die Pakete liegen in diesem Ordner (zum Installieren per Doppelklick öffnen).
    NoPkexec(PathBuf),
}

impl InstallOutcome {
    /// Text für den Dialog (`app`: z. B. "FreeDeck"; `rollback`: ältere Version installiert).
    pub fn message(&self, app: &str, version: &str, rollback: bool) -> String {
        match self {
            InstallOutcome::Installed if rollback => {
                tr!("Version {version} installed. {app} restarts now.", version = version, app = app)
            }
            InstallOutcome::Installed => tr!("Update installed. {app} restarts now.", app = app),
            InstallOutcome::Cancelled => tr!("The installation was cancelled. Nothing was changed."),
            InstallOutcome::Failed(e) => tr!("The installation failed: {error}", error = e),
            InstallOutcome::StartFailed(e) => tr!("Could not start the installation: {error}", error = e),
            InstallOutcome::NoPkexec(folder) => tr!(
                "Automatic installation is not possible here (pkexec is missing). The packages are in {folder} – install them with a double-click.",
                folder = folder.display()
            ),
        }
    }
}

/// Auf eine mit [`start_install`] gestartete Installation warten.
pub fn wait_install(mut child: Child) -> InstallOutcome {
    let mut output = String::new();
    if let Some(mut out) = child.stdout.take() {
        let mut raw = Vec::new();
        let _ = out.read_to_end(&mut raw);
        output = String::from_utf8_lossy(&raw).into_owned();
    }
    let status = match child.wait() {
        Ok(s) => s,
        Err(e) => return InstallOutcome::Failed(e.to_string()),
    };
    if status.success() {
        return InstallOutcome::Installed;
    }
    let _ = save_state(&[("installing", None)]);
    let rc = status.code();
    if matches!(rc, Some(126) | Some(127)) {
        return InstallOutcome::Cancelled;
    }
    let tail: String = output.chars().rev().take(2000).collect::<Vec<_>>().into_iter().rev().collect();
    log::warn!("Update installation failed ({rc:?}): {tail}");
    let last = output.trim().lines().last().map(str::to_string);
    InstallOutcome::Failed(last.unwrap_or_else(|| rc.map(|c| c.to_string()).unwrap_or_else(|| status.to_string())))
}

/// Pakete installieren und auf das Ende warten (blockiert, bis das Passwort eingegeben ist).
pub fn install(files: &[PathBuf], version: &str) -> InstallOutcome {
    match start_install(files, version) {
        Ok(Some(child)) => wait_install(child),
        Ok(None) => InstallOutcome::NoPkexec(
            files.first().and_then(|f| f.parent()).map(Path::to_path_buf).unwrap_or_else(download_dir),
        ),
        Err(e) => InstallOutcome::StartFailed(e.to_string()),
    }
}

// ------------------------------------------------------------------ im Hintergrund
/// [`check`] in einem eigenen Thread.
pub fn check_in_background() -> Receiver<Result<Option<Update>, UpdateError>> {
    background::spawn(check)
}

/// [`rollback_choices`] in einem eigenen Thread.
pub fn rollback_choices_in_background() -> Receiver<Result<Vec<Update>, UpdateError>> {
    background::spawn(rollback_choices)
}

/// [`download`] nach [`download_dir`] in einem eigenen Thread, mit Fortschritt in Bytes.
pub fn download_in_background(update: Update) -> Receiver<Event<Result<Vec<PathBuf>, UpdateError>>> {
    background::spawn_with_progress(move |p| download(&update, None, p))
}

/// [`install`] in einem eigenen Thread.
pub fn install_in_background(files: Vec<PathBuf>, version: String) -> Receiver<InstallOutcome> {
    background::spawn(move || install(&files, &version))
}

// ------------------------------------------------------------------ nur ein Programm fragt
/// Laufen mehrere Programme (FreeDeck, FreeWave, auch die Python-Version) gleichzeitig, fragt nur eines.
#[derive(Debug, Default)]
pub struct PromptLock {
    fh: Option<std::fs::File>,
}

impl PromptLock {
    pub fn new() -> Self {
        Self::default()
    }

    /// Sperre nehmen; false, wenn ein anderes Programm gerade fragt.
    pub fn acquire(&mut self) -> bool {
        self.acquire_at(&paths::state_dir().join("update.lock"))
    }

    pub fn acquire_at(&mut self, f: &Path) -> bool {
        use std::os::unix::io::AsRawFd;
        if let Some(dir) = f.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        let Ok(fh) = std::fs::OpenOptions::new().write(true).create(true).truncate(true).open(f) else {
            return false;
        };
        // SAFETY: gültiger Dateideskriptor, solange `fh` lebt.
        if unsafe { libc::flock(fh.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            return false;
        }
        self.fh = Some(fh);
        true
    }

    pub fn release(&mut self) {
        use std::os::unix::io::AsRawFd;
        if let Some(fh) = self.fh.take() {
            // SAFETY: wie oben
            unsafe { libc::flock(fh.as_raw_fd(), libc::LOCK_UN) };
        }
    }
}

impl Drop for PromptLock {
    fn drop(&mut self) {
        self.release();
    }
}

#[cfg(test)]
pub(crate) mod tests;
