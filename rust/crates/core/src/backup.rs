//! Sicherung und Wiederherstellung aller Benutzerdaten (wie `freestream/backup.py`; dasselbe Format,
//! Sicherungen der Python- und der Rust-Version lassen sich gegenseitig einspielen).
//!
//! Eine Sicherung ist ein ZIP-Archiv (Endung `.freestream-backup`) mit `manifest.json` und drei Bereichen:
//!
//! * `config/` – CONFIG_DIR (config.json: Einstellungen beider Programme, Mixer-Kanäle, Effekte …)
//! * `data/`   – DATA_DIR (Profile mit Bildern/Hintergründen, Ressourcen, Symbole, Plugins, Plugin-Daten)
//! * `state/`  – nur die Einstellungsdateien aus STATE_DIR (keine Logs, Prozessdaten, Updates)
//!
//! Symbolische Links werden nicht verfolgt, sondern (nur wenn sie innerhalb des Bereichs bleiben) als
//! Link im Manifest gespeichert – so landet nichts doppelt und nichts außerhalb im Archiv.
//!
//! Vor dem Wiederherstellen wird der aktuelle Stand in `STATE_DIR/backups` gesichert (die letzten drei
//! bleiben). Laufende Programme (FreeWave, Dienst) sollten vorher beendet und danach neu gestartet werden.

use serde_json::{json, Map, Value};
use std::collections::BTreeSet;
use std::ffi::OsString;
use std::fs::File;
use std::io::{Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::{Component, Path, PathBuf};
use std::sync::mpsc::Receiver;

use crate::background::{self, Event};
use crate::{paths, pgettext, tr, VERSION};

pub const EXT: &str = ".freestream-backup";
pub const FORMAT: &str = "freestream-backup";
/// Sicherungen von vor der Umbenennung.
pub const OLD_EXT: &str = ".streamlin-backup";
pub const OLD_FORMATS: [&str; 1] = ["streamlin-backup"];
pub const FORMAT_VERSION: i64 = 1;
pub const MANIFEST: &str = "manifest.json";
pub const SECTIONS: [&str; 3] = ["config", "data", "state"];
/// Aus STATE_DIR nur echte Einstellungen (aktives Profil/Helligkeit, Low Cut/Clipguard).
pub const STATE_FILES: [&str; 2] = ["state.json", "wavemic.json"];
/// Nie sichern: Caches, Laufzeitdaten, Downloads, eigene Sicherungen.
pub const SKIP_DIRS: [&str; 7] = ["__pycache__", ".cache", "cache", "Cache", "logs", "updates", "backups"];
pub const SKIP_SUFFIXES: [&str; 6] = [".pyc", ".pyo", ".tmp", ".log", ".lock", ".pid"];
pub const PLUGIN_DIRS: [&str; 1] = ["plugins"];
pub const SAFETY_DIR: &str = "backups";
pub const KEEP_SAFETY: usize = 3;
pub const MAX_FILES: usize = 200_000;
/// 16 GiB entpackt.
pub const MAX_BYTES: u64 = 16 << 30;
/// Zip-Bombe: Kompressionsfaktor bei großen Dateien.
pub const MAX_RATIO: u64 = 200;
const CHUNK: usize = 1 << 20;
const STAGE: &str = ".freestream-restore-";

/// Ungültige Sicherung oder Fehler beim Sichern/Wiederherstellen (Text für den Benutzer, übersetzt).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct BackupError(pub String);

/// Fortschritt (fertig, gesamt) in Bytes.
pub type Progress<'a> = &'a mut dyn FnMut(u64, u64);

// ------------------------------------------------------------------ Hilfen
/// Die drei Ordner, die gesichert werden.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Roots {
    pub config: PathBuf,
    pub data: PathBuf,
    pub state: PathBuf,
}

impl Roots {
    /// Ordner dieses Benutzers (aufgelöst, damit alte „freedeck“-Verweise nicht stören).
    pub fn current() -> Self {
        Self::new(&paths::config_dir(), &paths::data_dir(), &paths::state_dir())
    }

    /// Eigene Ordner (z. B. für Tests); werden wie in Python aufgelöst.
    pub fn new(config: &Path, data: &Path, state: &Path) -> Self {
        Self { config: realpath(config), data: realpath(data), state: realpath(state) }
    }

    fn get(&self, section: &str) -> &Path {
        match section {
            "config" => &self.config,
            "data" => &self.data,
            _ => &self.state,
        }
    }

    /// Ordner der Sicherheitskopien vor dem Wiederherstellen.
    pub fn safety_dir(&self) -> PathBuf {
        self.state.join(SAFETY_DIR)
    }
}

/// Ordner der Sicherheitskopien vor dem Wiederherstellen.
pub fn safety_dir() -> PathBuf {
    paths::state_dir().join(SAFETY_DIR)
}

/// Vorgeschlagener Dateiname, z. B. `freestream-2026-10-30.freestream-backup`.
pub fn default_name() -> String {
    format!("freestream-{}{EXT}", jiff::Zoned::now().strftime("%Y-%m-%d"))
}

/// Pfad auflösen wie `os.path.realpath` (auch wenn er nicht oder nur teilweise existiert).
pub fn realpath(p: &Path) -> PathBuf {
    let abs = if p.is_absolute() {
        p.to_path_buf()
    } else {
        std::env::current_dir().unwrap_or_else(|_| PathBuf::from("/")).join(p)
    };
    let mut hops = 0;
    resolve(PathBuf::from("/"), &abs, &mut hops)
}

fn resolve(mut base: PathBuf, rest: &Path, hops: &mut u32) -> PathBuf {
    for comp in rest.components() {
        match comp {
            Component::RootDir => base = PathBuf::from("/"),
            Component::CurDir | Component::Prefix(_) => {}
            Component::ParentDir => {
                base.pop();
            }
            Component::Normal(name) => {
                let next = base.join(name);
                match std::fs::read_link(&next) {
                    // Schleifen: nach 40 Verweisen wie ein normaler Name behandeln
                    Ok(target) if *hops < 40 => {
                        *hops += 1;
                        base = resolve(base, &target, hops);
                    }
                    _ => base = next,
                }
            }
        }
    }
    base
}

/// Liegt `path` in `root` (beide aufgelöst)?
fn inside(path: &Path, root: &Path) -> bool {
    path.starts_with(root)
}

/// Relativer Weg von `base` nach `path` (beide absolut und aufgelöst), wie `os.path.relpath`.
fn relpath(path: &Path, base: &Path) -> String {
    let p: Vec<_> = path.components().collect();
    let b: Vec<_> = base.components().collect();
    let common = p.iter().zip(&b).take_while(|(x, y)| x == y).count();
    let mut parts: Vec<String> = vec!["..".into(); b.len() - common];
    parts.extend(p[common..].iter().map(|c| c.as_os_str().to_string_lossy().into_owned()));
    if parts.is_empty() {
        ".".into()
    } else {
        parts.join("/")
    }
}

fn skip(name: &str, is_dir: bool) -> bool {
    if is_dir {
        SKIP_DIRS.contains(&name) || name.starts_with(STAGE)
    } else {
        SKIP_SUFFIXES.iter().any(|s| name.ends_with(s))
    }
}

/// Dateien eines Bereichs: (Archivname, Datei) und Links (Archivname → Ziel).
type Walk = (Vec<(String, PathBuf)>, Map<String, Value>);

fn walk(section: &str, root: &Path, plugins: bool) -> Walk {
    let mut files = Vec::new();
    let mut links = Map::new();
    if section == "state" {
        for n in STATE_FILES {
            let f = root.join(n);
            let is_link = std::fs::symlink_metadata(&f).map(|m| m.file_type().is_symlink()).unwrap_or(false);
            if f.is_file() && !is_link {
                files.push((format!("state/{n}"), f));
            }
        }
        return (files, links);
    }
    if !root.is_dir() {
        return (files, links);
    }
    let real = realpath(root);
    walk_dir(section, root, &real, Path::new(""), plugins, &mut files, &mut links);
    (files, links)
}

/// Wie `os.walk(followlinks=False)`: erst die Dateien eines Ordners, dann die Unterordner (sortiert).
fn walk_dir(section: &str, root: &Path, real: &Path, rel: &Path, plugins: bool, files: &mut Vec<(String, PathBuf)>,
            links: &mut Map<String, Value>) {
    let dir = root.join(rel);
    let Ok(rd) = std::fs::read_dir(&dir) else { return };
    let mut dirs: Vec<(OsString, bool)> = Vec::new(); // (Name, ist Link)
    let mut names: Vec<(OsString, bool)> = Vec::new();
    for e in rd.flatten() {
        let name = e.file_name();
        let p = e.path();
        let is_link = e.file_type().map(|t| t.is_symlink()).unwrap_or(false);
        // os.walk ordnet auch Links auf Ordner bei den Ordnern ein
        if p.is_dir() {
            dirs.push((name, is_link));
        } else {
            names.push((name, is_link));
        }
    }
    dirs.sort();
    names.sort();
    let mut keep = Vec::new();
    for (n, is_link) in dirs {
        let name = n.to_string_lossy();
        if skip(&name, true)
            || (section == "data" && rel.as_os_str().is_empty() && PLUGIN_DIRS.contains(&name.as_ref()) && !plugins)
        {
            continue;
        }
        if is_link {
            link(section, root, real, &dir.join(&n), links);
        } else {
            keep.push(n);
        }
    }
    for (n, is_link) in names {
        if skip(&n.to_string_lossy(), false) {
            continue;
        }
        let p = dir.join(&n);
        if is_link {
            link(section, root, real, &p, links);
        } else if p.is_file() {
            files.push((format!("{section}/{}", posix(&rel.join(&n))), p));
        }
    }
    for n in keep {
        walk_dir(section, root, real, &rel.join(n), plugins, files, links);
    }
}

fn posix(p: &Path) -> String {
    p.components().map(|c| c.as_os_str().to_string_lossy().into_owned()).collect::<Vec<_>>().join("/")
}

/// Verweis merken, wenn er im Bereich bleibt (Ziel ist dann ohnehin gesichert).
fn link(section: &str, root: &Path, real: &Path, p: &Path, links: &mut Map<String, Value>) {
    let Ok(target) = std::fs::read_link(p) else { return };
    let resolved = realpath(p);
    if !inside(&resolved, real) || !resolved.exists() {
        log::info!("{}", tr!("Skipping link outside the data folder: {path}", path = p.display()));
        return;
    }
    let rel = if target.is_absolute() {
        relpath(&resolved, &realpath(p.parent().unwrap_or(root)))
    } else {
        target.to_string_lossy().into_owned()
    };
    let name = p.strip_prefix(root).map(posix).unwrap_or_default();
    links.insert(format!("{section}/{name}"), Value::String(rel));
}

/// Anzeigenamen aus manifest.json von Profilen bzw. Plugins (`suffix`: ".sdProfile", ".sdPlugin").
fn display_names(folder: &Path, suffix: &str) -> Vec<Value> {
    let Ok(rd) = std::fs::read_dir(folder) else { return Vec::new() };
    let mut entries: Vec<PathBuf> = rd
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.file_name().map(|n| n.to_string_lossy().ends_with(suffix)).unwrap_or(false))
        .collect();
    entries.sort();
    entries
        .iter()
        .map(|d| {
            let stem = d.file_stem().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default();
            let name = std::fs::read_to_string(d.join("manifest.json"))
                .ok()
                .and_then(|t| serde_json::from_str::<Value>(&t).ok())
                .and_then(|m| m.get("Name").cloned())
                .filter(|n| !matches!(n, Value::Null | Value::Bool(false)) && n != "" && n != 0);
            Value::String(match name {
                Some(Value::String(s)) => s,
                Some(other) => other.to_string(),
                None => stem,
            })
        })
        .collect()
}

fn hostname() -> String {
    let mut buf = [0u8; 256];
    // SAFETY: Puffer mit passender Länge.
    if unsafe { libc::gethostname(buf.as_mut_ptr().cast(), buf.len()) } == 0 {
        let end = buf.iter().position(|b| *b == 0).unwrap_or(buf.len());
        return String::from_utf8_lossy(&buf[..end]).into_owned();
    }
    String::new()
}

fn expanduser(p: &Path) -> PathBuf {
    match p.strip_prefix("~") {
        Ok(rest) => paths::home().join(rest),
        Err(_) => p.to_path_buf(),
    }
}

// ------------------------------------------------------------------ Sichern
/// Sicherung der Daten dieses Benutzers schreiben. Gibt das Manifest zurück (mit `"path"`).
pub fn create(dest: &Path, plugins: bool, progress: Progress) -> Result<Value, BackupError> {
    create_from(&Roots::current(), dest, plugins, progress)
}

/// Sicherung der Ordner `roots` schreiben.
pub fn create_from(roots: &Roots, dest: &Path, plugins: bool, progress: Progress) -> Result<Value, BackupError> {
    let mut dest = expanduser(dest);
    let name = dest.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    if !name.ends_with(EXT) {
        dest = dest.with_file_name(format!("{name}{EXT}"));
    }
    let mut files = Vec::new();
    let mut links = Map::new();
    for sec in SECTIONS {
        let (f, lk) = walk(sec, roots.get(sec), plugins);
        files.extend(f);
        links.extend(lk);
    }
    let dest_real = realpath(&dest);
    files.retain(|(_, p)| realpath(p) != dest_real);
    let size = |p: &Path| std::fs::metadata(p).map(|m| m.len()).unwrap_or(0);
    let total: u64 = files.iter().map(|(_, p)| size(p)).sum();
    let mut manifest = json!({
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "freestream_version": VERSION,
        "created": jiff::Zoned::now().strftime("%Y-%m-%dT%H:%M:%S%:z").to_string(),
        "host": hostname(),
        "plugins_included": plugins,
        "contents": {
            "profiles": display_names(&roots.data.join("profiles"), ".sdProfile"),
            "plugins": if plugins { display_names(&roots.data.join("plugins"), ".sdPlugin") } else { Vec::new() },
            "config": roots.config.join("config.json").is_file(),
            "files": files.len(),
            "bytes": total,
        },
        "files": files.iter().map(|(a, _)| a.clone()).collect::<Vec<_>>(),
        "links": links,
    });
    let fail = |e: &dyn std::fmt::Display| BackupError(tr!("The backup could not be written: {error}", error = e));
    if let Some(dir) = dest.parent() {
        std::fs::create_dir_all(dir).map_err(|e| fail(&e))?;
    }
    let file_name = dest.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    let tmp = dest.with_file_name(format!(".{file_name}.part"));
    let res = write_zip(&tmp, &manifest, &files, total, progress).and_then(|_| std::fs::rename(&tmp, &dest));
    if let Err(e) = res {
        let _ = std::fs::remove_file(&tmp);
        return Err(fail(&e));
    }
    manifest["path"] = Value::String(dest.to_string_lossy().into_owned());
    Ok(manifest)
}

/// Änderungszeit als Ortszeit für den Zip-Eintrag (wie Pythons `zipfile.write`).
fn zip_time(meta: &std::fs::Metadata) -> zip::DateTime {
    let ts = meta
        .modified()
        .ok()
        .and_then(|t| jiff::Timestamp::try_from(t).ok())
        .unwrap_or_else(jiff::Timestamp::now);
    let dt = ts.to_zoned(jiff::tz::TimeZone::system()).datetime();
    zip::DateTime::from_date_and_time(
        dt.year().clamp(1980, 2107) as u16,
        dt.month() as u8,
        dt.day() as u8,
        dt.hour() as u8,
        dt.minute() as u8,
        dt.second() as u8,
    )
    .unwrap_or_default()
}

fn write_zip(tmp: &Path, manifest: &Value, files: &[(String, PathBuf)], total: u64, progress: Progress)
             -> std::io::Result<()> {
    use zip::write::SimpleFileOptions;
    let to_io = |e: zip::result::ZipError| std::io::Error::other(e);
    let mut z = zip::ZipWriter::new(File::create(tmp)?);
    let base = SimpleFileOptions::default()
        .compression_method(zip::CompressionMethod::Deflated)
        .compression_level(Some(6));
    let text = serde_json::to_string_pretty(manifest).unwrap_or_default();
    z.start_file(MANIFEST, base.unix_permissions(0o600)).map_err(to_io)?;
    z.write_all(text.as_bytes())?;
    let mut done = 0u64;
    for (arc, p) in files {
        let mut src = File::open(p)?;
        let meta = src.metadata()?;
        let opts = base
            .unix_permissions(meta.permissions().mode() & 0o7777)
            .last_modified_time(zip_time(&meta))
            .large_file(meta.len() >= 0xFFFF_FFFF);
        z.start_file(arc.as_str(), opts).map_err(to_io)?;
        std::io::copy(&mut src, &mut z)?;
        done += meta.len();
        progress(done, total);
    }
    z.finish().map_err(to_io)?.sync_all()
}

// ------------------------------------------------------------------ Prüfen
/// Archivname relativ, ohne „..“, Laufwerk oder Backslash; Bereich bekannt.
fn safe_name(name: &str) -> bool {
    if name.is_empty() || name.contains('\\') || name.contains('\0') || name.starts_with('/')
        || name.split('/').next().unwrap_or("").contains(':')
    {
        return false;
    }
    if name.ends_with('/') {
        return false;
    }
    // PurePosixPath lässt leere Teile und „.“ weg; „..“ bleibt
    let raw: Vec<&str> = name.split('/').collect();
    if raw.contains(&"..") {
        return false;
    }
    let parts: Vec<&str> = raw.into_iter().filter(|p| !p.is_empty() && *p != ".").collect();
    if parts.is_empty() {
        return false;
    }
    if name == MANIFEST {
        return true;
    }
    if parts.len() < 2 || !SECTIONS.contains(&parts[0]) {
        return false;
    }
    parts[0] != "state" || (parts.len() == 2 && STATE_FILES.contains(&parts[1]))
}

/// Linkziel relativ und innerhalb seines Bereichs.
fn link_ok(name: &str, target: &str) -> bool {
    if target.is_empty() || target.starts_with('/') || target.contains('\\') || target.contains('\0') {
        return false;
    }
    let parts = name.split('/').filter(|p| !p.is_empty() && *p != ".").count() as i64;
    let mut depth = parts - 2; // Ordnertiefe des Links im Bereich
    for part in target.split('/') {
        if part == ".." {
            depth -= 1;
            if depth < 0 {
                return false;
            }
        } else if !part.is_empty() && part != "." {
            depth += 1;
        }
    }
    true
}

fn not_backup() -> BackupError {
    BackupError(tr!("This file is not a FreeStream backup."))
}

fn unsafe_path(path: &str) -> BackupError {
    BackupError(tr!("The backup contains an unsafe path: {path}", path = path))
}

fn damaged() -> BackupError {
    BackupError(tr!("The backup is damaged or too large."))
}

fn incomplete() -> BackupError {
    BackupError(tr!("The backup is incomplete or damaged."))
}

/// Sicherung prüfen (Manifest, Pfade, Größe). Gibt das Manifest zurück (mit `"total_bytes"`).
pub fn inspect(path: &Path) -> Result<Value, BackupError> {
    let path = expanduser(path);
    let file = File::open(&path).map_err(|_| not_backup())?;
    let mut z = zip::ZipArchive::new(file).map_err(|_| not_backup())?;
    let mut manifest = {
        let mut f = z.by_name(MANIFEST).map_err(|e| match e {
            zip::result::ZipError::FileNotFound => not_backup(),
            e => BackupError(tr!("The backup could not be read: {error}", error = e)),
        })?;
        let mut raw = Vec::new();
        f.read_to_end(&mut raw).map_err(|e| BackupError(tr!("The backup could not be read: {error}", error = e)))?;
        let text = String::from_utf8(raw).map_err(|_| not_backup())?;
        serde_json::from_str::<Value>(&text).map_err(|_| not_backup())?
    };
    let Some(m) = manifest.as_object() else { return Err(not_backup()) };
    let format = m.get("format").and_then(Value::as_str).unwrap_or("");
    if format != FORMAT && !OLD_FORMATS.contains(&format) {
        return Err(not_backup());
    }
    match m.get("format_version") {
        Some(Value::Number(n)) if n.as_i64().is_some_and(|v| v <= FORMAT_VERSION) => {}
        Some(Value::Bool(_)) => {} // in Python ist bool eine Zahl (≤ 1)
        _ => {
            return Err(BackupError(tr!(
                "This backup was made with a newer version of FreeStream. Please update first."
            )))
        }
    }
    if z.len() > MAX_FILES {
        return Err(BackupError(tr!("The backup contains too many files.")));
    }
    let mut seen = BTreeSet::new();
    let mut total = 0u64;
    for i in 0..z.len() {
        let f = z.by_index_raw(i).map_err(|e| BackupError(tr!("The backup could not be read: {error}", error = e)))?;
        let name = f.name().map_err(|_| damaged())?.to_string();
        if !safe_name(&name) || seen.contains(&name) {
            return Err(unsafe_path(&name));
        }
        if f.unix_mode().is_some_and(|m| m & 0o170000 == 0o120000) {
            return Err(unsafe_path(&name));
        }
        total = total.saturating_add(f.size());
        if f.size() > (64 << 20) && f.size() > MAX_RATIO * f.compressed_size().max(1) {
            return Err(damaged());
        }
        seen.insert(name);
    }
    if total > MAX_BYTES {
        return Err(damaged());
    }
    seen.remove(MANIFEST);
    let listed: Option<BTreeSet<String>> = m
        .get("files")
        .and_then(Value::as_array)
        .and_then(|a| a.iter().map(|v| v.as_str().map(str::to_string)).collect());
    if listed.as_ref() != Some(&seen) {
        return Err(incomplete());
    }
    let links = match m.get("links") {
        None | Some(Value::Null) => Map::new(),
        Some(Value::Object(o)) => o.clone(),
        Some(v) if !truthy(v) => Map::new(),
        Some(_) => return Err(incomplete()),
    };
    for (name, target) in &links {
        let ok = safe_name(name)
            && !name.starts_with("state/")
            && target.as_str().is_some_and(|t| link_ok(name, t));
        if !ok {
            return Err(unsafe_path(name));
        }
    }
    let obj = manifest.as_object_mut().expect("Objekt");
    obj.insert("total_bytes".into(), json!(total));
    obj.entry("contents").or_insert_with(|| json!({}));
    Ok(manifest)
}

fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Lesbare Größe ("512 B", "1.5 MB").
pub fn human_size(n: u64) -> String {
    let mut n = n as f64;
    for unit in ["B", "KB", "MB", "GB"] {
        if n < 1024.0 || unit == "GB" {
            return if unit == "B" { format!("{n:.0} {unit}") } else { format!("{n:.1} {unit}") };
        }
        n /= 1024.0;
    }
    unreachable!()
}

/// Kurzbeschreibung des Inhalts (für Dialog und Befehlszeile), eine Zeile je Eintrag.
pub fn describe(manifest: &Value) -> Vec<String> {
    let empty = json!({});
    let c = manifest.get("contents").filter(|c| c.is_object()).unwrap_or(&empty);
    let raw_created = manifest.get("created").and_then(Value::as_str).filter(|s| !s.is_empty()).unwrap_or("?");
    let created = format_created(raw_created).unwrap_or_else(|| raw_created.to_string());
    let field = |v: Option<&Value>| v.and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_string);
    let host = field(manifest.get("host")).unwrap_or_else(|| "?".into());
    let version = field(manifest.get("freestream_version"))
        .or_else(|| field(manifest.get("streamlin_version")))
        .unwrap_or_else(|| "?".into());
    let mut lines = vec![
        tr!("Created: {date} on {host}", date = created, host = host),
        tr!("FreeStream version: {version}", version = version),
    ];
    let profiles: Vec<String> = c
        .get("profiles")
        .and_then(Value::as_array)
        .map(|a| a.iter().map(|v| v.as_str().map(str::to_string).unwrap_or_else(|| v.to_string())).collect())
        .unwrap_or_default();
    let mut line = tr!("Profiles: {count}", count = profiles.len());
    if !profiles.is_empty() {
        let shown = profiles.iter().take(6).cloned().collect::<Vec<_>>().join(", ");
        line += &format!(" ({shown}{})", if profiles.len() > 6 { ", …" } else { "" });
    }
    lines.push(line);
    if manifest.get("plugins_included").map(truthy).unwrap_or(true) {
        let n = c.get("plugins").and_then(Value::as_array).map(Vec::len).unwrap_or(0);
        lines.push(tr!("Plugins: {count}", count = n));
    } else {
        lines.push(tr!("Plugins: not included (installed plugins are kept)"));
    }
    let yes_no = if c.get("config").map(truthy).unwrap_or(false) {
        pgettext("answer", "yes")
    } else {
        pgettext("answer", "no")
    };
    lines.push(tr!("Settings: {state}", state = yes_no));
    let bytes = manifest
        .get("total_bytes")
        .or_else(|| c.get("bytes"))
        .and_then(|v| v.as_u64().or_else(|| v.as_f64().map(|f| f as u64)))
        .unwrap_or(0);
    lines.push(tr!("Size: {size}", size = human_size(bytes)));
    lines
}

/// "2026-10-30T16:45:00+01:00" → "2026-10-30 16:45" (Zeit, wie sie gespeichert ist).
fn format_created(text: &str) -> Option<String> {
    let dt: jiff::civil::DateTime = text
        .parse()
        .ok()
        .or_else(|| text.get(..19).and_then(|s| s.parse().ok()))
        .or_else(|| text.get(..16).and_then(|s| s.parse().ok()))
        .or_else(|| text.get(..10).and_then(|s| s.parse::<jiff::civil::Date>().ok()).map(|d| d.at(0, 0, 0, 0)))?;
    Some(dt.strftime("%Y-%m-%d %H:%M").to_string())
}

// ------------------------------------------------------------- Wiederherstellen
/// Aktuellen Stand vor dem Wiederherstellen sichern; nur die letzten [`KEEP_SAFETY`] bleiben.
pub fn safety_backup(roots: &Roots, plugins: bool) -> Result<PathBuf, BackupError> {
    let d = roots.safety_dir();
    std::fs::create_dir_all(&d)
        .map_err(|e| BackupError(tr!("The backup could not be written: {error}", error = e)))?;
    let stamp = jiff::Zoned::now().strftime("%Y%m%d-%H%M%S").to_string();
    let mut dest = d.join(format!("before-restore-{stamp}{EXT}"));
    let mut i = 1;
    while dest.exists() {
        dest = d.join(format!("before-restore-{stamp}-{i}{EXT}"));
        i += 1;
    }
    create_from(roots, &dest, plugins, &mut |_, _| {})?;
    let mut old: Vec<(std::time::SystemTime, PathBuf)> = std::fs::read_dir(&d)
        .map(|rd| {
            rd.flatten()
                .map(|e| e.path())
                .filter(|p| {
                    let n = p.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
                    n.starts_with("before-restore-") && n.ends_with(EXT)
                })
                .filter_map(|p| Some((std::fs::metadata(&p).and_then(|m| m.modified()).ok()?, p)))
                .collect()
        })
        .unwrap_or_default();
    old.sort();
    let n = old.len().saturating_sub(KEEP_SAFETY);
    for (_, p) in old.into_iter().take(n) {
        let _ = std::fs::remove_file(p);
    }
    Ok(dest)
}

/// Fehler beim Wiederherstellen: eigene Meldung oder Dateifehler.
enum Fail {
    Backup(BackupError),
    Io(std::io::Error),
}

impl From<BackupError> for Fail {
    fn from(e: BackupError) -> Self {
        Fail::Backup(e)
    }
}

impl From<std::io::Error> for Fail {
    fn from(e: std::io::Error) -> Self {
        Fail::Io(e)
    }
}

/// Archivname unter `base` – leere Teile und „.“ fallen weg („data//x“ bleibt so im Bereich).
fn under(base: &Path, rest: &str) -> PathBuf {
    let mut p = base.to_path_buf();
    p.extend(rest.split('/').filter(|s| !s.is_empty() && *s != "."));
    p
}

/// Eine Datei entpacken; bricht ab, wenn mehr kommt als angegeben.
fn extract(src: &mut impl Read, size: u64, mode: u32, dest: &Path, budget: u64) -> Result<u64, Fail> {
    if let Some(dir) = dest.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let mut out = File::create(dest)?;
    let mut buf = vec![0u8; CHUNK];
    let mut n = 0u64;
    loop {
        let got = match src.read(&mut buf) {
            Ok(0) => break,
            Ok(got) => got,
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(e.into()),
        };
        n += got as u64;
        if n > size || n > budget {
            return Err(damaged().into());
        }
        out.write_all(&buf[..got])?;
    }
    let mode = mode & 0o777;
    if mode != 0 {
        std::fs::set_permissions(dest, std::fs::Permissions::from_mode(mode | 0o600))?;
    }
    Ok(n)
}

fn free_space(p: &Path) -> Option<u64> {
    use std::os::unix::ffi::OsStrExt;
    let c = std::ffi::CString::new(p.as_os_str().as_bytes()).ok()?;
    let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
    // SAFETY: gültiger C-String und Zielstruktur.
    if unsafe { libc::statvfs(c.as_ptr(), &mut st) } != 0 {
        return None;
    }
    #[allow(clippy::unnecessary_cast)]
    Some(st.f_bavail as u64 * st.f_frsize as u64)
}

/// Sicherung in die Ordner dieses Benutzers einspielen. Gibt (Manifest, Sicherheitskopie) zurück.
pub fn restore(path: &Path, progress: Progress, safety: bool) -> Result<(Value, Option<PathBuf>), BackupError> {
    restore_to(&Roots::current(), path, progress, safety)
}

/// Sicherung in die Ordner `roots` einspielen.
pub fn restore_to(roots: &Roots, path: &Path, progress: Progress, safety: bool)
                  -> Result<(Value, Option<PathBuf>), BackupError> {
    let path = expanduser(path);
    let manifest = inspect(&path)?;
    let with_plugins = manifest.get("plugins_included").map(truthy).unwrap_or(true);
    match restore_inner(roots, &path, &manifest, with_plugins, progress, safety) {
        Ok(backup) => Ok((manifest, backup)),
        Err(Fail::Backup(e)) => Err(e),
        Err(Fail::Io(e)) => Err(BackupError(tr!("The backup could not be restored: {error}", error = e))),
    }
}

fn restore_inner(roots: &Roots, path: &Path, manifest: &Value, with_plugins: bool, progress: Progress,
                 safety: bool) -> Result<Option<PathBuf>, Fail> {
    // Zwischenordner im Ziel (gleiches Dateisystem; beim Sichern übersprungen), am Ende gelöscht
    std::fs::create_dir_all(&roots.config)?;
    let config_stage = tempfile::Builder::new().prefix(STAGE).tempdir_in(&roots.config)?;
    std::fs::create_dir_all(&roots.data)?;
    let data_stage = tempfile::Builder::new().prefix(STAGE).tempdir_in(&roots.data)?;
    let state_stage = tempfile::Builder::new().prefix(STAGE).tempdir_in(data_stage.path())?;
    let stage_of = |sec: &str| -> PathBuf {
        match sec {
            "config" => config_stage.path().to_path_buf(),
            "data" => data_stage.path().to_path_buf(),
            _ => state_stage.path().to_path_buf(),
        }
    };
    let total = manifest.get("total_bytes").and_then(Value::as_u64).unwrap_or(0);
    // 1. Alles in die Zwischenordner entpacken
    if free_space(data_stage.path()).is_some_and(|free| total > free) {
        return Err(BackupError(tr!("Not enough free disk space to restore this backup.")).into());
    }
    let mut done = 0u64;
    let mut z = zip::ZipArchive::new(File::open(path)?).map_err(|_| not_backup())?;
    for i in 0..z.len() {
        let mut f = z.by_index(i).map_err(|e| std::io::Error::other(e.to_string()))?;
        let name = f.name().map_err(|_| damaged())?.to_string();
        if name == MANIFEST {
            continue;
        }
        let Some((sec, rest)) = name.split_once('/') else { return Err(unsafe_path(&name).into()) };
        let size = f.size();
        let mode = f.unix_mode().unwrap_or(0);
        done += extract(&mut f, size, mode, &under(&stage_of(sec).join("new"), rest), MAX_BYTES - done)?;
        progress(done, total);
    }
    let links = manifest.get("links").and_then(Value::as_object).cloned().unwrap_or_default();
    for (name, target) in &links {
        let Some((sec, rest)) = name.split_once('/') else { continue };
        let link = under(&stage_of(sec).join("new"), rest);
        if let Some(dir) = link.parent() {
            std::fs::create_dir_all(dir)?;
        }
        if std::fs::symlink_metadata(&link).is_err() {
            std::os::unix::fs::symlink(target.as_str().unwrap_or(""), &link)?;
        }
    }
    // Link-Ketten dürfen den Bereich auch nicht verlassen
    for name in links.keys() {
        let Some((sec, rest)) = name.split_once('/') else { continue };
        let base = realpath(&stage_of(sec).join("new"));
        if !inside(&realpath(&under(&stage_of(sec).join("new"), rest)), &base) {
            return Err(unsafe_path(name).into());
        }
    }
    // 2. Sicherheitskopie des aktuellen Stands
    let backup = if safety { Some(safety_backup(roots, with_plugins)?) } else { None };
    // 3. Austauschen
    for (root, stage) in [(&roots.config, config_stage.path()), (&roots.data, data_stage.path())] {
        let mut keep = vec![SAFETY_DIR];
        if !with_plugins {
            keep.extend(PLUGIN_DIRS);
        }
        swap(root, stage, &keep)?;
    }
    std::fs::create_dir_all(&roots.state)?;
    for n in STATE_FILES {
        let src = state_stage.path().join("new").join(n);
        if src.is_file() {
            std::fs::copy(&src, roots.state.join(n))?;
        } else {
            match std::fs::remove_file(roots.state.join(n)) {
                Err(e) if e.kind() != std::io::ErrorKind::NotFound => return Err(e.into()),
                _ => {}
            }
        }
    }
    Ok(backup)
}

/// Inhalt von `root` durch `stage/new` ersetzen; bei Fehlern zurückrollen.
fn swap(root: &Path, stage: &Path, keep: &[&str]) -> std::io::Result<()> {
    let new = stage.join("new");
    let old = stage.join("old");
    std::fs::create_dir_all(&new)?;
    std::fs::create_dir_all(&old)?;
    std::fs::create_dir_all(root)?;
    let mut moved: Vec<OsString> = Vec::new();
    let mut placed: Vec<OsString> = Vec::new();
    let real_root = realpath(root);
    let res = (|| -> std::io::Result<()> {
        for e in std::fs::read_dir(root)?.flatten() {
            let n = e.file_name();
            let name = n.to_string_lossy();
            if keep.contains(&name.as_ref()) || name.starts_with(STAGE) {
                continue;
            }
            let p = e.path();
            // eigene Verweise nach außerhalb (nicht gesichert) bleiben erhalten
            let is_link = e.file_type().map(|t| t.is_symlink()).unwrap_or(false);
            if is_link && !new.join(&n).exists() && !inside(&realpath(&p), &real_root) {
                continue;
            }
            std::fs::rename(&p, old.join(&n))?;
            moved.push(n);
        }
        for e in std::fs::read_dir(&new)?.flatten() {
            let n = e.file_name();
            if keep.contains(&n.to_string_lossy().as_ref()) {
                continue;
            }
            std::fs::rename(e.path(), root.join(&n))?;
            placed.push(n);
        }
        Ok(())
    })();
    if res.is_err() {
        for n in &placed {
            let _ = std::fs::rename(root.join(n), new.join(n));
        }
        for n in &moved {
            let _ = std::fs::rename(old.join(n), root.join(n));
        }
    }
    res
}

// ------------------------------------------------------------- im Hintergrund
/// [`create`] in einem eigenen Thread, mit Fortschritt in Bytes.
pub fn create_in_background(dest: PathBuf, plugins: bool) -> Receiver<Event<Result<Value, BackupError>>> {
    background::spawn_with_progress(move |p| create(&dest, plugins, p))
}

/// [`restore`] in einem eigenen Thread, mit Fortschritt in Bytes.
#[allow(clippy::type_complexity)]
pub fn restore_in_background(path: PathBuf, safety: bool)
                             -> Receiver<Event<Result<(Value, Option<PathBuf>), BackupError>>> {
    background::spawn_with_progress(move |p| restore(&path, p, safety))
}

/// [`inspect`] in einem eigenen Thread (große Sicherungen auf langsamen Datenträgern).
pub fn inspect_in_background(path: PathBuf) -> Receiver<Result<Value, BackupError>> {
    background::spawn(move || inspect(&path))
}

#[cfg(test)]
mod tests;
