//! Installierte Plugins im Elgato-Format (`<UUID>.sdPlugin`-Ordner) – wie `freestream/plugins.py`:
//! auflisten, aus einer `.streamDeckPlugin`-Datei (ZIP) oder einem Ordner installieren, entfernen.
//! Das Ausführen der Plugins (Plugin-Host) gehört nicht hierher.

use serde_json::{Map, Value};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use crate::{paths, tr};

const NODE_EXT: [&str; 3] = [".js", ".mjs", ".cjs"];

/// Ein installiertes Plugin.
#[derive(Debug, Clone)]
pub struct PluginInfo {
    pub dir: PathBuf,
    pub manifest: Map<String, Value>,
    pub uuid: String,
    pub name: String,
    pub version: String,
    /// UUIDs der Aktionen
    pub actions: Vec<String>,
    pub code_path: String,
    /// "node", "exec" oder None (läuft nicht unter Linux, Grund in `reason`)
    pub runtime: Option<&'static str>,
    pub reason: String,
}

fn s(m: &Map<String, Value>, k: &str) -> String {
    match m.get(k) {
        Some(Value::String(v)) => v.clone(),
        Some(Value::Number(n)) => n.to_string(),
        _ => String::new(),
    }
}

fn is_elf(p: &Path) -> bool {
    use std::io::Read;
    let mut b = [0u8; 4];
    std::fs::File::open(p).and_then(|mut f| f.read_exact(&mut b)).map(|_| &b == b"\x7fELF").unwrap_or(false)
}

/// Bilddatei zu einem Pfad ohne Endung (Elgato: „images/icon“ -> icon.png, icon@2x.png …).
pub fn image_file(base: &Path) -> Option<PathBuf> {
    for ext in ["", ".png", "@2x.png", ".svg", ".gif", ".jpg"] {
        let p = PathBuf::from(format!("{}{ext}", base.display()));
        if p.is_file() {
            return Some(p);
        }
    }
    None
}

impl PluginInfo {
    /// Plugin-Ordner lesen (manifest.json).
    pub fn open(dir: &Path) -> anyhow::Result<Self> {
        let text = std::fs::read_to_string(dir.join("manifest.json"))?;
        let manifest = match serde_json::from_str::<Value>(text.trim_start_matches('\u{feff}'))? {
            Value::Object(m) => m,
            _ => anyhow::bail!("manifest.json"),
        };
        let dir_name = dir.file_name().and_then(|n| n.to_str()).unwrap_or("");
        let uuid = Some(s(&manifest, "UUID")).filter(|u| !u.is_empty())
            .unwrap_or_else(|| dir_name.strip_suffix(".sdPlugin").unwrap_or(dir_name).to_string());
        let name = Some(s(&manifest, "Name")).filter(|n| !n.is_empty()).unwrap_or_else(|| uuid.clone());
        let version = s(&manifest, "Version");
        let actions = manifest.get("Actions").and_then(Value::as_array).into_iter().flatten()
            .filter_map(|a| a.get("UUID").and_then(Value::as_str)).filter(|u| !u.is_empty()).map(str::to_string).collect();
        let mut info = PluginInfo { dir: dir.to_path_buf(), manifest, uuid, name, version, actions,
                                    code_path: String::new(), runtime: None, reason: String::new() };
        let (code, runtime, reason) = info.detect_runtime();
        info.code_path = code;
        info.runtime = runtime;
        info.reason = reason;
        Ok(info)
    }

    fn detect_runtime(&self) -> (String, Option<&'static str>, String) {
        let m = &self.manifest;
        let node = |c: &str| NODE_EXT.iter().any(|e| c.ends_with(e));
        for key in ["CodePathLin", "CodePathLinux"] {
            let c = s(m, key);
            if !c.is_empty() {
                let rt = if node(&c) { "node" } else { "exec" };
                return (c, Some(rt), String::new());
            }
        }
        let code = s(m, "CodePath");
        let nodejs = m.get("Nodejs").map(|v| !v.is_null() && v != &Value::Bool(false)).unwrap_or(false);
        if node(&code) || (nodejs && !code.is_empty() && ![".exe", ".html", ".htm"].iter().any(|e| code.ends_with(e))) {
            return (code, Some("node"), String::new());
        }
        if code.ends_with(".html") || code.ends_with(".htm") {
            return (code, None, tr!("HTML plugin (runs only in the Elgato app)"));
        }
        if !code.is_empty() && self.dir.join(&code).is_file() && is_elf(&self.dir.join(&code)) {
            return (code, Some("exec"), String::new());
        }
        (code, None, tr!("built for Windows/macOS only"))
    }

    pub fn runnable(&self) -> bool {
        self.runtime.is_some()
    }

    /// Symbol des Plugins (manifest „Icon“), falls vorhanden.
    pub fn icon_path(&self) -> Option<PathBuf> {
        let rel = s(&self.manifest, "Icon");
        if rel.is_empty() {
            return None;
        }
        image_file(&self.dir.join(rel))
    }
}

/// Alle installierten Plugins, nach Namen sortiert.
pub fn installed(root: Option<&Path>) -> Vec<PluginInfo> {
    let root = root.map(Path::to_path_buf).unwrap_or_else(paths::plugins_dir);
    let mut dirs: Vec<PathBuf> = std::fs::read_dir(&root).map(|rd| rd.flatten().map(|e| e.path()).collect()).unwrap_or_default();
    dirs.sort();
    let mut out: Vec<PluginInfo> = Vec::new();
    for d in dirs {
        if d.is_dir() && d.join("manifest.json").exists() {
            match PluginInfo::open(&d) {
                Ok(info) => {
                    out.retain(|i| i.uuid != info.uuid);
                    out.push(info);
                }
                Err(e) => log::warn!("{}", tr!("Plugin {name} not readable: {error}",
                    name = d.file_name().and_then(|n| n.to_str()).unwrap_or(""), error = e)),
            }
        }
    }
    out.sort_by_key(|i| i.name.to_lowercase());
    out
}

fn find_plugin_dirs(base: &Path, out: &mut Vec<PathBuf>) {
    let Ok(rd) = std::fs::read_dir(base) else { return };
    let mut entries: Vec<PathBuf> = rd.flatten().map(|e| e.path()).collect();
    entries.sort();
    for p in entries {
        if !p.is_dir() || p.is_symlink() {
            continue;
        }
        if p.extension().map(|e| e == "sdPlugin").unwrap_or(false) && p.join("manifest.json").exists() {
            out.push(p.clone());
        }
        find_plugin_dirs(&p, out);
    }
}

fn copy_tree(src: &Path, dst: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dst)?;
    for e in std::fs::read_dir(src)?.flatten() {
        let to = dst.join(e.file_name());
        let ft = e.file_type()?;
        if ft.is_dir() {
            copy_tree(&e.path(), &to)?;
        } else if ft.is_file() {
            std::fs::copy(e.path(), &to)?;
            // Programme ohne Endung und ELF-Dateien ausführbar machen
            if to.extension().is_none() || is_elf(&to) {
                let mut perm = std::fs::metadata(&to)?.permissions();
                perm.set_mode(perm.mode() | 0o111);
                std::fs::set_permissions(&to, perm)?;
            }
        }
    }
    Ok(())
}

/// `.streamDeckPlugin` (ZIP) oder `*.sdPlugin`-Ordner installieren. Gibt die UUIDs zurück.
pub fn install(src: &Path, root: Option<&Path>) -> anyhow::Result<Vec<String>> {
    let root = root.map(Path::to_path_buf).unwrap_or_else(paths::plugins_dir);
    std::fs::create_dir_all(&root)?;
    let tmp;
    let base: PathBuf = if src.is_file() {
        tmp = tempfile::Builder::new().prefix("freestream-plugin-").tempdir()?;
        crate::profiles::extract_zip(src, tmp.path())?;
        tmp.path().to_path_buf()
    } else {
        src.to_path_buf()
    };
    let mut dirs = Vec::new();
    if base.extension().map(|e| e == "sdPlugin").unwrap_or(false) {
        dirs.push(base.clone());
    } else {
        find_plugin_dirs(&base, &mut dirs);
    }
    let mut out = Vec::new();
    for d in dirs {
        let info = PluginInfo::open(&d)?;
        let dst = root.join(format!("{}.sdPlugin", info.uuid));
        if dst.exists() {
            std::fs::remove_dir_all(&dst)?;
        }
        copy_tree(&d, &dst)?;
        out.push(info.uuid);
    }
    Ok(out)
}

/// Plugin entfernen (Ordner löschen).
pub fn remove(uuid: &str, root: Option<&Path>) -> bool {
    if uuid.is_empty() || uuid.contains('/') || uuid.starts_with('.') {
        return false;
    }
    let root = root.map(Path::to_path_buf).unwrap_or_else(paths::plugins_dir);
    let d = root.join(format!("{uuid}.sdPlugin"));
    d.is_dir() && std::fs::remove_dir_all(d).is_ok()
}

/// Protokolldatei eines Plugins (wie die Python-Version: `<state>/plugins/<uuid>.log`).
pub fn log_file(uuid: &str) -> PathBuf {
    paths::state_dir().join("plugins").join(format!("{uuid}.log"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn install_list_remove() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("plugins");
        // Plugin als ZIP wie aus dem Marketplace
        let zip_path = tmp.path().join("demo.streamDeckPlugin");
        {
            let mut z = zip::ZipWriter::new(std::fs::File::create(&zip_path).unwrap());
            let o = zip::write::SimpleFileOptions::default();
            z.start_file("com.example.demo.sdPlugin/manifest.json", o).unwrap();
            z.write_all(br#"{"UUID":"com.example.demo","Name":"Demo","Version":"1.0","CodePath":"bin/plugin.js",
                            "Actions":[{"UUID":"com.example.demo.a"}]}"#).unwrap();
            z.start_file("com.example.demo.sdPlugin/bin/plugin.js", o).unwrap();
            z.write_all(b"// js").unwrap();
            z.finish().unwrap();
        }
        assert_eq!(install(&zip_path, Some(&root)).unwrap(), vec!["com.example.demo".to_string()]);
        let win = root.join("w.sdPlugin");
        std::fs::create_dir_all(&win).unwrap();
        std::fs::write(win.join("manifest.json"), r#"{"Name":"Win","CodePath":"x.exe"}"#).unwrap();
        let list = installed(Some(&root));
        assert_eq!(list.iter().map(|i| i.uuid.as_str()).collect::<Vec<_>>(), vec!["com.example.demo", "w"]);
        assert_eq!(list[0].runtime, Some("node"));
        assert_eq!(list[0].actions, vec!["com.example.demo.a".to_string()]);
        assert!(!list[1].runnable());
        assert!(remove("com.example.demo", Some(&root)));
        assert!(!remove("../x", Some(&root)));
        assert_eq!(installed(Some(&root)).len(), 1);
    }
}
