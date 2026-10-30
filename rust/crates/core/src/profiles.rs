//! Profilformat der Elgato-Software (wie `freestream/profiles.py`).
//!
//! * `.streamDeckProfile` (v2: ZIP mit `<UUID>.sdProfile/`, v3: ZIP mit `package.json`,
//!   `Profiles/<UUID>.sdProfile/` und `Resources/`)
//! * Elgato-Backups (`.streamDeckProfilesBackup`) – ZIP mit vielen Profilen
//! * kopierte Windows-Ordner `ProfilesV2`/`ProfilesV3`
//!
//! Ein Profil hat Seiten; Ordner sind Unterseiten, die per `com.elgato.streamdeck.profile.openchild`
//! über ihre UUID geöffnet werden. Seiten-Ordner heißen nach der UUID (v3, Großbuchstaben) oder nach ihrer
//! Base32-Kodierung mit Elgato-Alphabet plus „Z“ (v2).

use serde_json::{Map, Value};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use crate::models::{model_geometry, model_name, Geometry};
use crate::paths;

/// Aktion einer Taste/eines Drehreglers: das JSON-Objekt aus dem Seiten-Manifest (UUID, Settings, States …).
pub type Action = Map<String, Value>;

/// UUID -> Ordnername im v2-Format (base32hex, U/V verschoben, Suffix Z).
pub fn encode_page_dir(page_uuid: &str) -> Option<String> {
    let u = uuid::Uuid::parse_str(page_uuid).ok()?;
    let s = data_encoding::BASE32HEX_NOPAD.encode(u.as_bytes());
    Some(s.chars().map(|c| match c { 'U' => 'V', 'V' => 'W', o => o }).collect::<String>() + "Z")
}

/// Ordnername (v3: UUID, v2: Base32 + „Z“) -> UUID in Kleinbuchstaben.
pub fn decode_page_dir(name: &str) -> Option<String> {
    if let Ok(u) = uuid::Uuid::parse_str(name) {
        return Some(u.hyphenated().to_string());
    }
    let raw = name.strip_suffix('Z').unwrap_or(name);
    let raw: String = raw.chars().map(|c| match c { 'V' => 'U', 'W' => 'V', o => o }).collect();
    let data = data_encoding::BASE32HEX_NOPAD.decode(raw.as_bytes()).ok()?;
    let bytes: [u8; 16] = data.get(..16)?.try_into().ok()?;
    Some(uuid::Uuid::from_bytes(bytes).hyphenated().to_string())
}

/// JSON-Datei lesen (mit oder ohne BOM).
pub fn read_json(p: &Path) -> anyhow::Result<Value> {
    let text = std::fs::read_to_string(p)?;
    Ok(serde_json::from_str(text.trim_start_matches('\u{feff}'))?)
}

/// „Spalte,Zeile“ -> (Spalte, Zeile).
pub fn parse_pos(key: &str) -> Option<(u8, u8)> {
    let (c, r) = key.split_once(',')?;
    Some((c.trim().parse().ok()?, r.trim().parse().ok()?))
}

/// Eine Seite (oder ein Ordner) eines Profils.
#[derive(Debug, Clone, Default)]
pub struct Page {
    pub uuid: String,
    pub dir: PathBuf,
    pub name: String,
    /// (Spalte, Zeile) -> Aktion
    pub keypad: BTreeMap<(u8, u8), Action>,
    /// Drehregler -> Aktion (Stream Deck +)
    pub encoders: BTreeMap<u8, Action>,
    /// Infoleiste des Neo -> Aktion
    pub infobar: BTreeMap<u8, Action>,
    /// Bild für den ganzen Touchstreifen
    pub encoder_background: String,
    /// FreeDeck: Hintergrundbild über alle Tasten, abgedunkelt um `background_dim` Prozent
    pub background: String,
    pub background_dim: u8,
}

impl Page {
    /// Bild relativ zum Seitenordner, falls vorhanden.
    pub fn image_path(&self, rel: &str) -> Option<PathBuf> {
        if rel.is_empty() {
            return None;
        }
        let p = self.dir.join(rel);
        p.exists().then_some(p)
    }

    /// Seite aus `<dir>/manifest.json` lesen.
    pub fn load(page_uuid: &str, dir: &Path) -> anyhow::Result<Self> {
        let m = read_json(&dir.join("manifest.json"))?;
        Ok(Self::from_manifest(page_uuid, dir, &m))
    }

    pub fn from_manifest(page_uuid: &str, dir: &Path, m: &Value) -> Self {
        let mut pg = Page {
            uuid: page_uuid.to_string(),
            dir: dir.to_path_buf(),
            name: m.get("Name").and_then(Value::as_str).unwrap_or("").to_string(),
            ..Default::default()
        };
        if let Some(bg) = m.get("FreeDeckBackground").and_then(Value::as_object) {
            pg.background = bg.get("Image").and_then(Value::as_str).unwrap_or("").to_string();
            pg.background_dim = bg.get("Dim").and_then(Value::as_u64).unwrap_or(0).min(90) as u8;
        }
        let legacy;
        let controllers = match m.get("Controllers").and_then(Value::as_array) {
            Some(c) => c,
            None => {
                legacy = vec![serde_json::json!({"Type": "Keypad", "Actions": m.get("Actions").cloned().unwrap_or(Value::Null)})];
                &legacy
            }
        };
        for c in controllers {
            let ctype = c.get("Type").and_then(Value::as_str).unwrap_or("Keypad");
            let Some(actions) = c.get("Actions").and_then(Value::as_object) else { continue };
            for (k, a) in actions {
                let Some(a) = a.as_object().filter(|a| a.get("UUID").map(|u| u != "").unwrap_or(false)) else { continue };
                let Some((col, row)) = parse_pos(k) else { continue };
                match ctype {
                    "Keypad" => {
                        pg.keypad.insert((col, row), a.clone());
                    }
                    "Encoder" => {
                        pg.encoders.insert(col, a.clone());
                    }
                    _ => {
                        pg.infobar.insert(col, a.clone());
                    }
                }
            }
            if ctype == "Encoder" {
                pg.encoder_background = c.get("Background").and_then(Value::as_str).unwrap_or("").to_string();
            }
        }
        pg
    }
}

/// Ein Profil (`<ID>.sdProfile`).
#[derive(Debug, Clone)]
pub struct Profile {
    pub root: PathBuf,
    pub id: String,
    pub manifest: Value,
    pub name: String,
    pub model: String,
    pub device_uuid: String,
    pub version: String,
    pub app_identifier: String,
    pub page_ids: Vec<String>,
    pub default_page: String,
    pub current_page: String,
    pub meta: Value,
}

impl Profile {
    pub fn open(root: &Path) -> anyhow::Result<Self> {
        let name = root.file_name().and_then(|n| n.to_str()).unwrap_or("");
        let id = name.strip_suffix(".sdProfile").unwrap_or(name).to_string();
        let m = read_json(&root.join("manifest.json"))?;
        let s = |v: Option<&Value>| v.and_then(Value::as_str).unwrap_or("").to_string();
        let dev = m.get("Device");
        let pages = m.get("Pages");
        let mut page_ids: Vec<String> = pages
            .and_then(|p| p.get("Pages"))
            .and_then(Value::as_array)
            .map(|a| a.iter().filter_map(|v| v.as_str().map(str::to_string)).collect())
            .unwrap_or_default();
        let default_page = s(pages.and_then(|p| p.get("Default")));
        if page_ids.is_empty() && !default_page.is_empty() {
            page_ids.push(default_page.clone()); // sehr alte Profile (v1)
        }
        let meta_p = root.join("lindeck-meta.json");
        let meta = if meta_p.exists() { read_json(&meta_p).unwrap_or(Value::Null) } else { Value::Null };
        Ok(Self {
            root: root.to_path_buf(),
            name: m.get("Name").and_then(Value::as_str).filter(|n| !n.is_empty()).unwrap_or(&id).to_string(),
            id,
            model: s(dev.and_then(|d| d.get("Model"))),
            device_uuid: s(dev.and_then(|d| d.get("UUID"))),
            version: m.get("Version").map(|v| v.as_str().map(str::to_string).unwrap_or_else(|| v.to_string())).unwrap_or_else(|| "1.0".into()),
            app_identifier: s(m.get("AppIdentifier")),
            current_page: s(pages.and_then(|p| p.get("Current"))),
            page_ids,
            default_page,
            manifest: m,
            meta,
        })
    }

    pub fn model_name(&self) -> String {
        model_name(&self.model)
    }

    /// Ordner einer Seite (v3 groß/klein oder v2-Base32).
    pub fn page_dir(&self, page_uuid: &str) -> Option<PathBuf> {
        if page_uuid.is_empty() {
            return None;
        }
        let base = self.root.join("Profiles");
        for cand in [page_uuid.to_uppercase(), page_uuid.to_string(), page_uuid.to_lowercase()] {
            let d = base.join(&cand);
            if d.join("manifest.json").exists() {
                return Some(d);
            }
        }
        let enc = encode_page_dir(page_uuid)?;
        let d = base.join(enc);
        d.join("manifest.json").exists().then_some(d)
    }

    /// Seite oder Ordner lesen.
    pub fn page(&self, page_uuid: &str) -> Option<Page> {
        if let Some(d) = self.page_dir(page_uuid) {
            return Page::load(page_uuid, &d).ok();
        }
        if page_uuid == self.default_page {
            if let Some(actions) = self.manifest.get("Actions").and_then(Value::as_object) {
                let mut pg = Page { uuid: page_uuid.into(), dir: self.root.clone(), ..Default::default() };
                for (k, a) in actions {
                    if let (Some(pos), Some(a)) = (parse_pos(k), a.as_object()) {
                        pg.keypad.insert(pos, a.clone());
                    }
                }
                return Some(pg);
            }
        }
        None
    }

    pub fn pages(&self) -> Vec<Page> {
        self.page_ids.iter().filter_map(|p| self.page(p)).collect()
    }

    /// Raster des Profils: aus dem Modell, sonst aus den belegten Tasten.
    pub fn geometry(&self) -> Geometry {
        if let Some(g) = model_geometry(&self.model) {
            return g;
        }
        let (mut cols, mut rows, mut dials) = (0u8, 0u8, 0u8);
        for pg in self.pages() {
            for (c, r) in pg.keypad.keys() {
                cols = cols.max(c + 1);
                rows = rows.max(r + 1);
            }
            if let Some(d) = pg.encoders.keys().max() {
                dials = dials.max(d + 1);
            }
        }
        Geometry { cols: if cols == 0 { 5 } else { cols }, rows: if rows == 0 { 3 } else { rows }, dials, touch_keys: 0 }
    }

    /// Bild aus den importierten Ressourcen (v3).
    pub fn resource_path(&self, res_id: &str) -> Option<PathBuf> {
        if res_id.is_empty() {
            return None;
        }
        let dir = paths::resources_dir().join(res_id);
        std::fs::read_dir(dir).ok()?.flatten().map(|e| e.path()).find(|p| p.is_file())
    }
}

/// Alle Profile im Profilordner.
#[derive(Debug, Default)]
pub struct ProfileStore {
    pub root: PathBuf,
    pub profiles: BTreeMap<String, Profile>,
    mtime: Option<std::time::SystemTime>,
}

impl ProfileStore {
    pub fn new(root: Option<&Path>) -> Self {
        let mut s = Self { root: root.map(Path::to_path_buf).unwrap_or_else(paths::profiles_dir), ..Default::default() };
        s.reload();
        s
    }

    fn scan_mtime(&self) -> Option<std::time::SystemTime> {
        let mut mt = std::fs::metadata(&self.root).and_then(|m| m.modified()).ok()?;
        for e in std::fs::read_dir(&self.root).ok()?.flatten() {
            if let Ok(t) = e.metadata().and_then(|m| m.modified()) {
                mt = mt.max(t);
            }
        }
        Some(mt)
    }

    pub fn changed_on_disk(&self) -> bool {
        self.scan_mtime() != self.mtime
    }

    pub fn reload(&mut self) {
        let mut profiles = BTreeMap::new();
        if let Ok(rd) = std::fs::read_dir(&self.root) {
            let mut dirs: Vec<_> = rd.flatten().map(|e| e.path()).collect();
            dirs.sort();
            for d in dirs {
                if d.is_dir() && d.join("manifest.json").exists() {
                    match Profile::open(&d) {
                        Ok(p) => {
                            profiles.insert(p.id.clone(), p);
                        }
                        Err(e) => log::warn!("{}", crate::tr!("Profile {name} not readable: {error}", name = d.display(), error = e)),
                    }
                }
            }
        }
        self.profiles = profiles;
        self.mtime = self.scan_mtime();
    }

    /// Profil nach ID (auch ohne Groß-/Kleinschreibung) oder Name.
    pub fn get(&self, pid: &str) -> Option<&Profile> {
        self.profiles.get(pid).or_else(|| {
            self.profiles.values().find(|p| p.id.eq_ignore_ascii_case(pid) || p.name == pid)
        })
    }

    /// Profile, die zum Gerät passen: erst exakter Modellcode, dann gleiches Raster.
    pub fn for_model(&self, model: &str, geometry: Geometry) -> Vec<&Profile> {
        let base = |m: &str| m.split('-').next().unwrap_or("").trim_end_matches('L').to_string();
        let mut exact: Vec<&Profile> = self.profiles.values().filter(|p| base(&p.model) == model).collect();
        let mut geo: Vec<&Profile> = self
            .profiles
            .values()
            .filter(|p| base(&p.model) != model)
            .filter(|p| {
                let g = p.geometry();
                g.cols == geometry.cols && g.rows == geometry.rows && g.dials == geometry.dials
            })
            .collect();
        exact.sort_by_key(|p| p.name.to_lowercase());
        geo.sort_by_key(|p| p.name.to_lowercase());
        exact.extend(geo);
        exact
    }
}

// --------------------------------------------------------------------------------------------- Import
fn walk(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(rd) = std::fs::read_dir(dir) else { return };
    let mut entries: Vec<_> = rd.flatten().map(|e| e.path()).collect();
    entries.sort();
    for p in entries {
        if p.is_dir() {
            out.push(p.clone());
            walk(&p, out);
        } else {
            out.push(p);
        }
    }
}

/// Oberste `*.sdProfile`-Ordner unterhalb von `base` (nicht verschachtelt).
fn find_profile_roots(base: &Path) -> Vec<PathBuf> {
    let mut all = Vec::new();
    walk(base, &mut all);
    all.into_iter()
        .filter(|d| d.is_dir() && d.extension().map(|e| e == "sdProfile").unwrap_or(false))
        .filter(|d| d.join("manifest.json").exists())
        .filter(|d| !d.ancestors().skip(1).any(|a| a.extension().map(|e| e == "sdProfile").unwrap_or(false)))
        .collect()
}

fn copy_dir(src: &Path, dst: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dst)?;
    for e in std::fs::read_dir(src)? {
        let e = e?;
        let to = dst.join(e.file_name());
        if e.file_type()?.is_dir() {
            copy_dir(&e.path(), &to)?;
        } else {
            std::fs::copy(e.path(), to)?;
        }
    }
    Ok(())
}

pub(crate) fn extract_zip(file: &Path, dest: &Path) -> anyhow::Result<()> {
    let mut z = zip::ZipArchive::new(std::fs::File::open(file)?)?;
    for i in 0..z.len() {
        let mut f = z.by_index(i)?;
        let name = f.name()?.replace('\\', "/");
        // Pfade aus dem Archiv dürfen nicht aus dem Zielordner heraus zeigen
        let rel = Path::new(&name);
        if rel.is_absolute() || rel.components().any(|c| matches!(c, std::path::Component::ParentDir)) {
            anyhow::bail!(crate::tr!("Unsafe path in archive: {path}", path = name));
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

/// Profildatei, Backup oder Ordner importieren; gibt die IDs der übernommenen Profile zurück.
/// `replace = false` lässt vorhandene Profile unverändert.
pub fn import_path(src: &Path, store_root: Option<&Path>, resources_root: Option<&Path>, replace: bool) -> anyhow::Result<Vec<String>> {
    let store_root = store_root.map(Path::to_path_buf).unwrap_or_else(paths::profiles_dir);
    let resources_root = resources_root.map(Path::to_path_buf).unwrap_or_else(paths::resources_dir);
    std::fs::create_dir_all(&store_root)?;
    std::fs::create_dir_all(&resources_root)?;
    let tmp;
    let base = if src.is_file() {
        tmp = tempfile::Builder::new().prefix("freestream-import-").tempdir()?;
        extract_zip(src, tmp.path())?;
        // Backups enthalten oft weitere ZIPs (.streamDeckProfile)
        let mut all = Vec::new();
        walk(tmp.path(), &mut all);
        for inner in all.into_iter().filter(|p| p.is_file() && p.extension().map(|e| e == "streamDeckProfile").unwrap_or(false)) {
            extract_zip(&inner, &inner.with_extension("extracted"))?;
        }
        tmp.path().to_path_buf()
    } else if src.is_dir() {
        src.to_path_buf()
    } else {
        anyhow::bail!("{}: not found", src.display());
    };
    // package.json (v3): Ressourcen übernehmen und Metadaten den Profilen zuordnen
    let mut all = Vec::new();
    walk(&base, &mut all);
    let mut pkg_meta: Vec<(PathBuf, Value)> = Vec::new();
    for pkg in all.iter().filter(|p| p.file_name().map(|n| n == "package.json").unwrap_or(false)) {
        let pkg_dir = pkg.parent().unwrap_or(&base).to_path_buf();
        let meta = read_json(pkg).unwrap_or(Value::Null);
        if let Ok(res) = read_json(&pkg_dir.join("Resources/manifest.json")) {
            if let Some(map) = res.get("resources").and_then(Value::as_object) {
                for (rid, info) in map {
                    let f = pkg_dir.join("Resources").join(info.get("file").and_then(Value::as_str).unwrap_or(""));
                    if f.is_file() {
                        let dst = resources_root.join(rid);
                        std::fs::create_dir_all(&dst)?;
                        std::fs::copy(&f, dst.join(f.file_name().unwrap()))?;
                    }
                }
            }
        }
        for root in find_profile_roots(&pkg_dir) {
            pkg_meta.push((root, meta.clone()));
        }
    }
    let mut imported = Vec::new();
    for root in find_profile_roots(&base) {
        let dst = store_root.join(root.file_name().unwrap());
        if dst.exists() {
            if !replace {
                continue;
            }
            std::fs::remove_dir_all(&dst)?;
        }
        copy_dir(&root, &dst)?;
        if let Some((_, meta)) = pkg_meta.iter().find(|(r, _)| *r == root) {
            if !meta.is_null() {
                std::fs::write(dst.join("lindeck-meta.json"), serde_json::to_string_pretty(meta)?)?;
            }
        }
        let name = dst.file_name().and_then(|n| n.to_str()).unwrap_or("");
        imported.push(name.strip_suffix(".sdProfile").unwrap_or(name).to_string());
    }
    Ok(imported)
}

/// Profil löschen.
pub fn delete_profile(pid: &str, store_root: Option<&Path>) -> bool {
    let d = store_root.map(Path::to_path_buf).unwrap_or_else(paths::profiles_dir).join(format!("{pid}.sdProfile"));
    d.exists() && std::fs::remove_dir_all(d).is_ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn page_dir_codec_roundtrip() {
        let u = "3f2a1c5e-8b7d-4e6f-9a0b-1c2d3e4f5a6b";
        let enc = encode_page_dir(u).unwrap();
        assert!(enc.ends_with('Z'));
        assert_eq!(decode_page_dir(&enc).as_deref(), Some(u));
        assert_eq!(decode_page_dir(&u.to_uppercase()).as_deref(), Some(u));
    }

    #[test]
    fn reads_page_controllers() {
        let m = serde_json::json!({"Name": "P", "Controllers": [
            {"Type": "Keypad", "Actions": {"1,0": {"UUID": "a"}, "2,1": {"UUID": ""}}},
            {"Type": "Encoder", "Background": "bg.png", "Actions": {"3,0": {"UUID": "d"}}}]});
        let pg = Page::from_manifest("x", Path::new("/tmp"), &m);
        assert_eq!(pg.keypad.len(), 1);
        assert!(pg.keypad.contains_key(&(1, 0)));
        assert_eq!(pg.encoders.keys().copied().collect::<Vec<_>>(), vec![3]);
        assert_eq!(pg.encoder_background, "bg.png");
    }
}
