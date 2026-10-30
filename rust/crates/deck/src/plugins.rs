//! Elgato-Plugins (wie `freestream/plugins.py` und die Teile von `freestream/pluginui.py` ohne Oberfläche).
//!
//! Elgato-Plugins sind eigenständige Programme, die sich per WebSocket mit der Elgato-App verbinden.
//! FreeDeck übernimmt diese Rolle:
//!
//! * Plugins liegen in `~/.local/share/freestream/plugins/<UUID>.sdPlugin` (Installation:
//!   [`install`] mit einer `.streamDeckPlugin`-Datei, einem ZIP oder einem `*.sdPlugin`-Ordner).
//! * Node.js-Plugins (CodePath `*.js`, alle Plugins mit dem offiziellen Elgato-SDK ab Version 6)
//!   laufen unter Linux mit dem System-Node.js. Plugins, die nur als Windows-/macOS-Programm vorliegen,
//!   können nicht gestartet werden. Linux-Plugins können zusätzlich „CodePathLin“ angeben.
//! * Gestartet wird wie von der Elgato-App:
//!   `<Programm> -port P -pluginUUID U -registerEvent registerPlugin -info JSON` ([`host`]).
//! * Aktionen der Plugins auf Tasten und Drehreglern: [`PluginBehavior`] (über [`behavior_for`]).
//! * Einstellungsseiten (Property Inspector): liefert der Host per HTTP auf demselben Port aus,
//!   siehe [`property_inspector_url`] und [`browser_command`].

pub mod action;
pub mod host;

pub use action::PluginBehavior;
pub use host::{host, set_host, CtxState, DeviceInfo, HostOptions, PluginHost, PluginStatus, StatusColor};

use freestream_core::catalog::{ActionType, Text, DIAL, KEY};
use freestream_core::i18n::ngettext;
use freestream_core::paths;
use freestream_core::tr;
use image::RgbaImage;
use serde_json::{Map, Value};
use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::SystemTime;

use crate::actions::Behavior;

/// Version der Elgato-App, die FreeDeck gegenüber den Plugins angibt.
pub const SDK_APP_VERSION: &str = "6.7.0";
/// Elgato-Gerätetypen im SDK (DeviceType) nach den ersten fünf Zeichen des Modellcodes.
pub const DEVICE_TYPES: &[(&str, u8)] = &[
    ("20GAA", 0), ("20GBA", 0), ("20GAI", 1), ("20GAT", 2), ("20GBF", 5), ("20GBD", 7), ("20GBX", 7),
    ("20GBJ", 9), ("20GBO", 10),
];
const NODE_EXT: [&str; 3] = [".js", ".mjs", ".cjs"];
/// Plugin-Marktplatz von Elgato (Knopf „Marketplace öffnen“).
pub const MARKET: &str = "https://marketplace.elgato.com/stream-deck/plugins";

// ======================================================================================== Plugins
/// Wie ein Plugin gestartet wird.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Runtime {
    /// mit Node.js
    Node,
    /// als Linux-Programm
    Exec,
}

impl Runtime {
    /// Name wie in der Python-Version („node“, „exec“) – für `freedeck plugin list`.
    pub fn as_str(&self) -> &'static str {
        match self {
            Runtime::Node => "node",
            Runtime::Exec => "exec",
        }
    }
}

/// Ein installiertes Plugin (`<UUID>.sdPlugin` mit `manifest.json`).
#[derive(Debug, Clone)]
pub struct PluginInfo {
    pub dir: PathBuf,
    pub manifest: Map<String, Value>,
    pub uuid: String,
    pub name: String,
    pub version: String,
    /// UUID der Aktion -> Eintrag aus „Actions“ im Manifest (Reihenfolge wie im Manifest)
    pub actions: Map<String, Value>,
    pub code_path: String,
    /// None = läuft unter Linux nicht (Grund in `reason`)
    pub runtime: Option<Runtime>,
    pub reason: String,
}

/// Wert wie in Python „wahr“? (nicht leer, nicht 0, nicht false/null)
fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64().map(|f| f != 0.0).unwrap_or(true),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

/// Wert als Text wie `str()` in Python (Zeichenketten ohne Anführungszeichen).
fn value_text(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        v => v.to_string(),
    }
}

impl PluginInfo {
    /// Plugin aus seinem Ordner lesen.
    pub fn load(dir: &Path) -> anyhow::Result<Self> {
        let text = std::fs::read_to_string(dir.join("manifest.json"))?;
        let manifest: Map<String, Value> = serde_json::from_str(text.trim_start_matches('\u{feff}'))?;
        let s = |k: &str| manifest.get(k).filter(|v| truthy(Some(v))).map(value_text).unwrap_or_default();
        let dir_name = dir.file_name().and_then(|n| n.to_str()).unwrap_or("");
        let uuid = Some(s("UUID")).filter(|u| !u.is_empty())
            .unwrap_or_else(|| dir_name.strip_suffix(".sdPlugin").unwrap_or(dir_name).to_string());
        let name = Some(s("Name")).filter(|n| !n.is_empty()).unwrap_or_else(|| uuid.clone());
        let version = s("Version");
        let mut actions = Map::new();
        for a in manifest.get("Actions").and_then(Value::as_array).into_iter().flatten() {
            if let Some(u) = a.get("UUID").filter(|u| truthy(Some(u))).map(value_text) {
                actions.insert(u, a.clone());
            }
        }
        let mut info = PluginInfo {
            dir: dir.to_path_buf(),
            manifest,
            uuid,
            name,
            version,
            actions,
            code_path: String::new(),
            runtime: None,
            reason: String::new(),
        };
        let (code, runtime, reason) = info.decide_runtime();
        info.code_path = code;
        info.runtime = runtime;
        info.reason = reason;
        Ok(info)
    }

    /// Wie das Plugin unter Linux läuft (wie `PluginInfo._runtime`): (Programm, Laufzeit, Grund).
    fn decide_runtime(&self) -> (String, Option<Runtime>, String) {
        let m = &self.manifest;
        let node_ext = |c: &str| NODE_EXT.iter().any(|e| c.ends_with(e));
        for key in ["CodePathLin", "CodePathLinux"] {
            if truthy(m.get(key)) {
                let c = value_text(&m[key]);
                let rt = if node_ext(&c) { Runtime::Node } else { Runtime::Exec };
                return (c, Some(rt), String::new());
            }
        }
        let code = m.get("CodePath").filter(|v| truthy(Some(v))).map(value_text).unwrap_or_default();
        let html = code.ends_with(".html") || code.ends_with(".htm");
        if node_ext(&code) || (truthy(m.get("Nodejs")) && !code.is_empty() && !code.ends_with(".exe") && !html) {
            return (code, Some(Runtime::Node), String::new());
        }
        if html {
            return (code, None, tr!("HTML plugin (runs only in the Elgato app)"));
        }
        if !code.is_empty() && self.dir.join(&code).is_file() && is_elf(&self.dir.join(&code)) {
            return (code, Some(Runtime::Exec), String::new());
        }
        (code, None, tr!("built for Windows/macOS only"))
    }

    /// Läuft das Plugin unter Linux?
    pub fn runnable(&self) -> bool {
        self.runtime.is_some()
    }

    /// Eintrag einer Aktion aus dem Manifest.
    pub fn action_meta(&self, action_uuid: &str) -> Option<&Map<String, Value>> {
        self.actions.get(action_uuid).and_then(Value::as_object)
    }

    /// Symbol einer Aktion im Plugin-Ordner (Bild des Zustands, sonst „Icon“).
    pub fn action_icon(&self, action_uuid: &str, state: usize) -> Option<PathBuf> {
        let a = self.action_meta(action_uuid)?;
        let states = a.get("States").and_then(Value::as_array).cloned().unwrap_or_default();
        let from_state = states.get(state.min(states.len().saturating_sub(1)))
            .and_then(|s| s.get("Image")).filter(|v| truthy(Some(v))).map(value_text);
        let rel = from_state.or_else(|| a.get("Icon").filter(|v| truthy(Some(v))).map(value_text))?;
        image_file(&self.dir.join(rel))
    }

    /// Symbol des Plugins (Manifest „Icon“).
    pub fn icon(&self) -> Option<PathBuf> {
        let rel = self.manifest.get("Icon").filter(|v| truthy(Some(v))).map(value_text)?;
        image_file(&self.dir.join(rel))
    }

    /// Einstellungsseite einer Aktion (PropertyInspectorPath der Aktion, sonst des Plugins).
    pub fn property_inspector_path(&self, action_uuid: &str) -> Option<String> {
        self.action_meta(action_uuid).and_then(|a| a.get("PropertyInspectorPath"))
            .filter(|v| truthy(Some(v)))
            .or_else(|| self.manifest.get("PropertyInspectorPath").filter(|v| truthy(Some(v))))
            .map(value_text)
    }

    /// Autor und Beschreibung aus dem Manifest (für die Plugin-Liste).
    pub fn author(&self) -> String {
        self.manifest.get("Author").map(value_text).unwrap_or_default()
    }

    pub fn description(&self) -> String {
        self.manifest.get("Description").map(value_text).unwrap_or_default()
    }

    /// Kennung der installierten Fassung: ändert sich, wenn das Plugin neu installiert wird (das Kopieren
    /// legt manifest.json neu an), aber nicht, wenn das Plugin in seinem Ordner Protokolle schreibt.
    pub(crate) fn stamp(&self) -> (String, Option<i128>) {
        use std::os::unix::fs::MetadataExt;
        let ctime = std::fs::metadata(self.dir.join("manifest.json")).ok()
            .map(|m| m.ctime() as i128 * 1_000_000_000 + m.ctime_nsec() as i128);
        (self.version.clone(), ctime)
    }
}

fn is_elf(p: &Path) -> bool {
    use std::io::Read;
    let mut buf = [0u8; 4];
    std::fs::File::open(p).and_then(|mut f| f.read_exact(&mut buf)).is_ok() && &buf == b"\x7fELF"
}

/// Bilddatei zu einem Pfad ohne oder mit Endung (wie `_image_file`).
pub fn image_file(base: &Path) -> Option<PathBuf> {
    for ext in ["", ".png", "@2x.png", ".svg", ".gif", ".jpg"] {
        let p = PathBuf::from(format!("{}{ext}", base.display()));
        if p.is_file() {
            return Some(p);
        }
    }
    None
}

type Installed = BTreeMap<String, Arc<PluginInfo>>;
type CacheKey = (Option<SystemTime>, Vec<Option<SystemTime>>);

fn cache() -> &'static Mutex<HashMap<PathBuf, (CacheKey, Installed)>> {
    static C: OnceLock<Mutex<HashMap<PathBuf, (CacheKey, Installed)>>> = OnceLock::new();
    C.get_or_init(Default::default)
}

fn root_of(plugins_dir: Option<&Path>) -> PathBuf {
    plugins_dir.map(Path::to_path_buf).unwrap_or_else(paths::plugins_dir)
}

/// UUID -> Plugin aller installierten Plugins (zwischengespeichert, neu gelesen bei Änderungen im Ordner).
pub fn installed(plugins_dir: Option<&Path>) -> Installed {
    let root = root_of(plugins_dir);
    let Ok(rd) = std::fs::read_dir(&root) else { return Installed::new() };
    let mut entries: Vec<PathBuf> = rd.flatten().map(|e| e.path()).collect();
    entries.sort();
    let mut mtimes: Vec<Option<SystemTime>> = entries.iter().map(|d| std::fs::metadata(d).and_then(|m| m.modified()).ok()).collect();
    mtimes.sort();
    let key = (std::fs::metadata(&root).and_then(|m| m.modified()).ok(), mtimes);
    if let Some((k, v)) = cache().lock().unwrap().get(&root) {
        if *k == key {
            return v.clone();
        }
    }
    let mut found = Installed::new();
    for d in entries {
        if d.is_dir() && d.join("manifest.json").exists() {
            match PluginInfo::load(&d) {
                Ok(info) => {
                    found.insert(info.uuid.clone(), Arc::new(info));
                }
                Err(e) => log::warn!("{}", tr!("Plugin {name} not readable: {error}",
                    name = d.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default(), error = e)),
            }
        }
    }
    cache().lock().unwrap().insert(root, (key, found.clone()));
    found
}

/// Plugin, zu dem eine Aktion gehört (Aktion im Manifest, sonst UUID mit dem Plugin als Präfix).
pub fn plugin_for_action(action_uuid: &str, plugins_dir: Option<&Path>) -> Option<Arc<PluginInfo>> {
    let all = installed(plugins_dir);
    all.values().find(|i| i.actions.contains_key(action_uuid))
        .or_else(|| all.values().find(|i| action_uuid.starts_with(&format!("{}.", i.uuid))))
        .cloned()
}

/// Kann FreeDeck die Aktion über ein installiertes Plugin ausführen? (wie `is_supported` für Plugins)
pub fn action_supported(action_uuid: &str) -> bool {
    plugin_for_action(action_uuid, None).map(|i| i.runnable()).unwrap_or(false)
}

/// Hat die Aktion eine Einstellungsseite (Property Inspector), die FreeDeck öffnen kann?
pub fn has_property_inspector(action_uuid: &str) -> bool {
    plugin_for_action(action_uuid, None)
        .filter(|i| i.runnable())
        .map(|i| i.property_inspector_path(action_uuid).is_some())
        .unwrap_or(false)
}

/// Aktionen installierter Plugins für den Editor (wie `catalog.plugin_types`).
pub fn plugin_types(plugins_dir: Option<&Path>) -> Vec<ActionType> {
    let mut out = Vec::new();
    for info in installed(plugins_dir).values() {
        for (uuid, a) in &info.actions {
            let name = a.get("Name").filter(|v| truthy(Some(v))).map(value_text).unwrap_or_else(|| uuid.clone());
            let mut t = ActionType::new(uuid, Text::Raw(name), Text::Raw(format!("Plugin: {}", info.name)));
            let ctrls: Vec<Value> = a.get("Controllers").and_then(Value::as_array).filter(|c| !c.is_empty()).cloned()
                .unwrap_or_else(|| vec![Value::from("Keypad")]);
            t.controllers = ctrls.iter().map(|c| if c.as_str() == Some("Encoder") { DIAL } else { KEY }).collect();
            t.states = a.get("States").and_then(Value::as_array).map(|s| s.len()).unwrap_or(0).max(1);
            t.description = Text::Raw(a.get("Tooltip").filter(|v| truthy(Some(v))).map(value_text).unwrap_or_default());
            t.plugin = info.uuid.clone();
            t.plugin_version = info.version.clone();
            out.push(t);
        }
    }
    out
}

/// Aktionsart aus dem eingebauten Katalog, sonst aus den installierten Plugins (wie `catalog.find_type`).
pub fn find_type(uuid: &str) -> Option<ActionType> {
    freestream_core::catalog::by_uuid(uuid).cloned()
        .or_else(|| plugin_types(None).into_iter().find(|t| t.uuid == uuid))
}

// ================================================================================ Installieren
/// ZIP sicher entpacken: kein Eintrag darf aus dem Zielordner heraus zeigen.
fn safe_extract(file: &Path, dest: &Path) -> anyhow::Result<()> {
    let mut z = zip::ZipArchive::new(std::fs::File::open(file)?)?;
    for i in 0..z.len() {
        let mut f = z.by_index(i)?;
        let name = f.name().replace('\\', "/");
        let rel = Path::new(&name);
        if rel.is_absolute() || rel.components().any(|c| matches!(c, std::path::Component::ParentDir)) {
            anyhow::bail!(tr!("Unsafe path in archive: {path}", path = f.name()));
        }
        let target = dest.join(rel);
        if name.ends_with('/') {
            std::fs::create_dir_all(&target)?;
            continue;
        }
        if let Some(p) = target.parent() {
            std::fs::create_dir_all(p)?;
        }
        let mut out = std::fs::File::create(&target)?;
        std::io::copy(&mut f, &mut out)?;
    }
    Ok(())
}

fn copy_tree(src: &Path, dst: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dst)?;
    for e in std::fs::read_dir(src)? {
        let e = e?;
        let to = dst.join(e.file_name());
        if e.path().is_dir() {
            copy_tree(&e.path(), &to)?;
        } else {
            std::fs::copy(e.path(), to)?;
        }
    }
    Ok(())
}

fn walk_files(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(rd) = std::fs::read_dir(dir) else { return };
    for e in rd.flatten() {
        let p = e.path();
        if p.is_dir() {
            walk_files(&p, out);
        } else if p.is_file() {
            out.push(p);
        }
    }
}

/// Alle `*.sdPlugin`-Ordner mit manifest.json unterhalb von `base` (wie `rglob`).
fn find_plugin_dirs(base: &Path, out: &mut Vec<PathBuf>) {
    let Ok(rd) = std::fs::read_dir(base) else { return };
    let mut entries: Vec<PathBuf> = rd.flatten().map(|e| e.path()).collect();
    entries.sort();
    for p in entries {
        if !p.is_dir() {
            continue;
        }
        if p.file_name().and_then(|n| n.to_str()).map(|n| n.ends_with(".sdPlugin")).unwrap_or(false) && p.join("manifest.json").exists() {
            out.push(p.clone());
        }
        find_plugin_dirs(&p, out);
    }
}

/// Installiert eine `.streamDeckPlugin`-Datei (ZIP) oder einen `*.sdPlugin`-Ordner; gibt die
/// UUIDs der installierten Plugins zurück. Ein vorhandenes Plugin gleicher UUID wird ersetzt.
pub fn install(src: &Path, plugins_dir: Option<&Path>) -> anyhow::Result<Vec<String>> {
    let src = PathBuf::from(freestream_core_expand(src));
    let root = root_of(plugins_dir);
    std::fs::create_dir_all(&root)?;
    let tmp;
    let base = if src.is_file() {
        tmp = tempfile::Builder::new().prefix("freestream-plugin-").tempdir()?;
        safe_extract(&src, tmp.path())?;
        tmp.path().to_path_buf()
    } else {
        src.clone()
    };
    let dirs = if base.file_name().and_then(|n| n.to_str()).map(|n| n.ends_with(".sdPlugin")).unwrap_or(false) {
        vec![base.clone()]
    } else {
        let mut v = Vec::new();
        find_plugin_dirs(&base, &mut v);
        v
    };
    let mut out = Vec::new();
    for d in dirs {
        let info = PluginInfo::load(&d)?;
        let dst = root.join(format!("{}.sdPlugin", info.uuid));
        if dst.exists() {
            std::fs::remove_dir_all(&dst)?;
        }
        copy_tree(&d, &dst)?;
        // Programme ohne Endung und Linux-Programme ausführbar machen (ZIP kennt keine Rechte)
        let mut files = Vec::new();
        walk_files(&dst, &mut files);
        for f in files {
            if f.extension().is_none() || is_elf(&f) {
                use std::os::unix::fs::PermissionsExt;
                if let Ok(m) = std::fs::metadata(&f) {
                    let mut p = m.permissions();
                    p.set_mode(p.mode() | 0o111);
                    let _ = std::fs::set_permissions(&f, p);
                }
            }
        }
        out.push(info.uuid);
    }
    // Ordner als geändert markieren (Zwischenspeicher von installed())
    if let Ok(f) = std::fs::File::open(&root) {
        let _ = f.set_modified(SystemTime::now());
    }
    Ok(out)
}

/// `~` am Anfang durch den Heimatordner ersetzen (wie `Path.expanduser`).
fn freestream_core_expand(p: &Path) -> String {
    crate::system::expand_user(&p.to_string_lossy())
}

/// Plugin entfernen; false, wenn es nicht installiert ist.
pub fn remove(plugin_uuid: &str, plugins_dir: Option<&Path>) -> bool {
    let d = root_of(plugins_dir).join(format!("{plugin_uuid}.sdPlugin"));
    d.exists() && std::fs::remove_dir_all(&d).is_ok()
}

/// Protokolldatei eines Plugins (Ausgabe des Programms).
pub fn log_file(plugin_uuid: &str) -> PathBuf {
    paths::state_dir().join("plugins").join(format!("{plugin_uuid}.log"))
}

// ===================================================================================== Bilder
/// Base64 dekodieren; `strict` = nur gültige Zeichen (wie `b64decode(validate=True)`), sonst werden
/// fremde Zeichen (Zeilenumbrüche …) übergangen.
fn b64(s: &str, strict: bool) -> Option<Vec<u8>> {
    let ok = |c: char| c.is_ascii_alphanumeric() || matches!(c, '+' | '/' | '=');
    let clean: String = if strict {
        if !s.chars().all(ok) {
            return None;
        }
        s.to_string()
    } else {
        s.chars().filter(|c| ok(*c)).collect()
    };
    data_encoding::BASE64.decode(clean.as_bytes()).ok()
        .or_else(|| if strict { None } else { data_encoding::BASE64_NOPAD.decode(clean.trim_end_matches('=').as_bytes()).ok() })
}

/// %XX in einer data-URL ohne Base64 auflösen.
fn percent_decode(s: &str) -> Vec<u8> {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() {
            if let Some(v) = std::str::from_utf8(&b[i + 1..i + 3]).ok().and_then(|h| u8::from_str_radix(h, 16).ok()) {
                out.push(v);
                i += 3;
                continue;
            }
        }
        out.push(b[i]);
        i += 1;
    }
    out
}

/// SVG in der Größe `size`×`size` zeichnen.
pub fn render_svg(data: &[u8], size: u32) -> Option<RgbaImage> {
    let tree = resvg::usvg::Tree::from_data(data, &resvg::usvg::Options::default()).ok()?;
    let mut pix = resvg::tiny_skia::Pixmap::new(size, size)?;
    let s = tree.size();
    let t = resvg::tiny_skia::Transform::from_scale(size as f32 / s.width(), size as f32 / s.height());
    resvg::render(&tree, t, &mut pix.as_mut());
    // tiny-skia speichert vormultipliziert
    let mut img = RgbaImage::new(size, size);
    for (p, c) in img.pixels_mut().zip(pix.pixels()) {
        let c = c.demultiply();
        *p = image::Rgba([c.red(), c.green(), c.blue(), c.alpha()]);
    }
    Some(img)
}

/// setImage-Daten (data-URL mit PNG/JPG/SVG, reines Base64, SVG-Text oder Dateipfad im Plugin-Ordner)
/// als Bild; SVG wird in `size`×`size` gezeichnet. None, wenn nichts Lesbares darin steht.
pub fn decode_image(data: &str, plugin_dir: Option<&Path>, size: u32) -> Option<RgbaImage> {
    if data.is_empty() {
        return None;
    }
    let trimmed = data.trim_start();
    let (raw, is_svg) = if let Some(rest) = data.strip_prefix("data:") {
        let (header, body) = rest.split_once(',').unwrap_or((rest, ""));
        let raw = if header.contains(";base64") { b64(body, false)? } else { percent_decode(body) };
        (raw, header.contains("svg"))
    } else if trimmed.starts_with("<svg") || trimmed.starts_with("<?xml") {
        (data.as_bytes().to_vec(), true)
    } else if let Some(p) = plugin_dir.filter(|_| data.len() < 1024).and_then(|d| image_file(&d.join(data))) {
        // kurzer Text: Pfad im Plugin-Ordner (Base64-Bilder sind länger)
        let svg = p.extension().map(|e| e == "svg").unwrap_or(false);
        (std::fs::read(&p).ok()?, svg)
    } else {
        (b64(data, true)?, false)
    };
    if is_svg {
        let img = render_svg(&raw, size);
        if img.is_none() {
            log::debug!("{}", tr!("SVG cannot be rendered: {error}", error = "usvg"));
        }
        return img;
    }
    image::load_from_memory(&raw).ok().map(|i| i.to_rgba8())
}

// ===================================================================================== Status
/// Node.js finden: `node_path` aus der Konfiguration, sonst `node` bzw. `nodejs` im PATH.
pub fn find_node(cfg: Option<&Map<String, Value>>) -> Option<PathBuf> {
    if let Some(p) = cfg.and_then(|c| c.get("node_path")).and_then(Value::as_str).filter(|p| !p.is_empty()) {
        return Some(PathBuf::from(p));
    }
    let path = std::env::var_os("PATH")?;
    for prog in ["node", "nodejs"] {
        if let Some(p) = std::env::split_paths(&path).map(|d| d.join(prog)).find(|p| p.is_file()) {
            return Some(p);
        }
    }
    None
}

/// Version von Node.js („v20.11.0“), leer, wenn sie sich nicht ermitteln lässt.
pub fn node_version(node: Option<&Path>) -> String {
    let Some(node) = node else { return String::new() };
    let Ok(mut child) = std::process::Command::new(node).arg("--version")
        .stdin(std::process::Stdio::null()).stdout(std::process::Stdio::piped()).stderr(std::process::Stdio::null()).spawn()
    else {
        return String::new();
    };
    let start = std::time::Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if start.elapsed() < std::time::Duration::from_secs(3) => std::thread::sleep(std::time::Duration::from_millis(20)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return String::new();
            }
        }
    }
    let mut out = String::new();
    if let Some(mut so) = child.stdout.take() {
        use std::io::Read;
        let _ = so.read_to_string(&mut out);
    }
    out.trim().to_string()
}

/// Hinweis zu Node.js über der Plugin-Liste: (Text, Node.js vorhanden).
pub fn node_status_text(node: Option<&Path>) -> (String, bool) {
    let ver = node_version(node);
    if !ver.is_empty() {
        (tr!("Node.js {version} – plugins can run", version = ver), true)
    } else if node.is_some() {
        (tr!("Node.js found – plugins can run"), true)
    } else {
        (tr!("Node.js missing – please install the “nodejs” package, otherwise most plugins will not start"), false)
    }
}

/// Zustand eines Plugins für die Liste, ohne laufenden Host im selben Prozess (wie `status(info, None)`).
pub fn status(info: &PluginInfo, host: Option<&PluginHost>) -> PluginStatus {
    match host {
        Some(h) => h.status(info),
        None if !info.runnable() => PluginStatus::NotRunnable(info.reason.clone()),
        None => PluginStatus::Ready,
    }
}

/// Zeile unter dem Namen eines Plugins: „Version 1.2 · Autor · 3 Aktionen“.
pub fn meta_line(info: &PluginInfo) -> String {
    let n = info.actions.len();
    let ver = if info.version.is_empty() { String::new() } else { tr!("Version {version}", version = info.version) };
    let acts = ngettext("{n} action", "{n} actions", n as i64).replace("{n}", &n.to_string());
    [ver, info.author(), acts].into_iter().filter(|s| !s.is_empty()).collect::<Vec<_>>().join(" · ")
}

// ================================================================================ Befehlszeile
/// Ergebnis eines Befehls von `freedeck plugin …`: Ausgabezeilen und Rückgabewert.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CliResult {
    pub lines: Vec<String>,
    pub code: i32,
}

/// `freedeck plugin install <Datei …>`
pub fn cli_install(targets: &[PathBuf], plugins_dir: Option<&Path>) -> CliResult {
    if targets.is_empty() {
        return CliResult { lines: vec![tr!("Specify a file: freedeck plugin install <file.streamDeckPlugin>")], code: 1 };
    }
    let mut lines = Vec::new();
    let mut ids = Vec::new();
    for t in targets {
        match install(t, plugins_dir) {
            Ok(v) => ids.extend(v),
            Err(e) => lines.push(format!("{}: {e}", t.display())),
        }
    }
    let all = installed(plugins_dir);
    for uid in &ids {
        let Some(info) = all.get(uid) else { continue };
        lines.push(if info.runnable() {
            tr!("Installed: {name} ({uuid})", name = info.name, uuid = uid)
        } else {
            tr!("Installed: {name} ({uuid}) – does not start: {reason}", name = info.name, uuid = uid, reason = info.reason)
        });
    }
    CliResult { lines, code: if ids.is_empty() { 1 } else { 0 } }
}

/// `freedeck plugin remove <UUID …>`
pub fn cli_remove(targets: &[String], plugins_dir: Option<&Path>) -> CliResult {
    let ok = targets.iter().all(|t| remove(t, plugins_dir));
    CliResult { lines: vec![if ok { tr!("Removed") } else { tr!("Plugin not found") }], code: if ok { 0 } else { 1 } }
}

/// `freedeck plugin list`
pub fn cli_list(plugins_dir: Option<&Path>) -> CliResult {
    let found = installed(plugins_dir);
    let mut lines = Vec::new();
    if found.is_empty() {
        lines.push(tr!("No plugins installed. 'freedeck plugin install <file.streamDeckPlugin>'"));
    }
    for info in found.values() {
        let state = match info.runtime {
            Some(r) => r.as_str().to_string(),
            None => tr!("cannot start: {reason}", reason = info.reason),
        };
        let n = info.actions.len();
        let n_actions = ngettext("{n} action", "{n} actions", n as i64).replace("{n}", &n.to_string());
        lines.push(format!("{:<40} {:<28} {:<10} {n_actions}  [{state}]", info.uuid, info.name, info.version));
    }
    CliResult { lines, code: 0 }
}

/// Plugin-Dateien installieren und je Datei eine Meldung liefern (wie `pluginui.install_files` ohne Dialog).
pub fn install_report(files: &[PathBuf], plugins_dir: Option<&Path>) -> Vec<String> {
    let mut msgs = Vec::new();
    for f in files {
        match install(f, plugins_dir) {
            Ok(ids) => {
                let all = installed(plugins_dir);
                for uid in ids {
                    msgs.push(match all.get(&uid) {
                        Some(i) if !i.runnable() => tr!("“{name}” installed – does not run on Linux ({reason})", name = i.name, reason = i.reason),
                        Some(i) => tr!("“{name}” installed", name = i.name),
                        None => tr!("“{name}” installed", name = uid),
                    });
                }
            }
            Err(e) => msgs.push(format!("{}: {e}", f.display())),
        }
    }
    msgs
}

// ===================================================================================== Aktionen
/// Verhalten für Aktionen installierter, unter Linux lauffähiger Plugins (für [`crate::extra`]).
pub fn behavior_for(uuid: &str) -> Option<Box<dyn Behavior>> {
    let info = plugin_for_action(uuid, None)?;
    info.runnable().then(|| Box::new(PluginBehavior::new(uuid)) as Box<dyn Behavior>)
}

// ======================================================================= Property Inspector
/// Wo die Einstellungsseite einer Aktion liegt.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PiSlot {
    /// Taste (Spalte, Zeile)
    Key(u8, u8),
    /// Drehregler
    Dial(u8),
}

impl PiSlot {
    /// Schreibweise in der Adresse: „key:2,1“ bzw. „dial:0“.
    pub fn query(&self) -> String {
        match self {
            PiSlot::Key(c, r) => format!("key:{c},{r}"),
            PiSlot::Dial(i) => format!("dial:{i}"),
        }
    }

    pub fn parse(s: &str) -> Option<Self> {
        let (kind, pos) = s.split_once(':')?;
        if kind == "dial" {
            return Some(PiSlot::Dial(pos.trim().parse().ok()?));
        }
        let (c, r) = pos.split_once(',')?;
        Some(PiSlot::Key(c.trim().parse().ok()?, r.trim().parse().ok()?))
    }
}

/// Datei mit Port, Schlüssel und Pid des laufenden Plugin-Hosts.
pub fn host_state_file() -> PathBuf {
    paths::state_dir().join("plugin-host.json")
}

/// Teil einer Adresse kodieren (wie `urllib.parse.quote`/`urlencode`).
pub(crate) fn url_quote(s: &str, safe: &str) -> String {
    let mut out = String::new();
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || b"_.-~".contains(&b) || safe.as_bytes().contains(&b) {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// Adresse der Einstellungsseite für eine Aktion im Profil – über den laufenden Dienst
/// (`plugin-host.json`). None, wenn kein Dienst läuft: dann „FreeDeck must be running for this …“ zeigen.
pub fn property_inspector_url(profile_id: &str, page_uuid: &str, slot: PiSlot) -> Option<String> {
    let text = std::fs::read_to_string(host_state_file()).ok()?;
    let v: Value = serde_json::from_str(&text).ok()?;
    let pid = v.get("pid")?.as_i64()?;
    // läuft der Dienst noch?
    if unsafe { libc::kill(pid as libc::pid_t, 0) } != 0 {
        return None;
    }
    let port = v.get("port")?.as_u64()?;
    let token = v.get("token")?.as_str()?;
    Some(pi_url_for(port as u16, token, profile_id, page_uuid, slot))
}

pub(crate) fn pi_url_for(port: u16, token: &str, profile_id: &str, page_uuid: &str, slot: PiSlot) -> String {
    format!("http://127.0.0.1:{port}/pi/{token}?profile={}&page={}&slot={}",
        url_quote(profile_id, ""), url_quote(page_uuid, ""), url_quote(&slot.query(), ""))
}

/// Befehl, der eine Einstellungsseite in einem eigenen Fenster öffnet (Chromium-artiger Browser im
/// App-Modus, sonst `xdg-open`) – wie `open_property_inspector` im Editor.
pub fn browser_command(url: &str) -> Vec<String> {
    for exe in ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "brave-browser", "microsoft-edge"] {
        if crate::system::have(exe) {
            return vec![exe.into(), format!("--app={url}"), "--window-size=460,720".into(), "--new-window".into()];
        }
    }
    vec!["xdg-open".into(), url.into()]
}

#[cfg(test)]
mod tests;
