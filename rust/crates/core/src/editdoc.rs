//! Profile bearbeiten – die Logik hinter dem grafischen Editor (ohne GUI), wie `freestream/editdoc.py`.
//!
//! Ein [`ProfileDoc`] hält das Profil-Manifest und alle Seiten-Manifeste im Speicher, ändert sie und
//! schreibt sie im Originalformat der Elgato-App zurück. Unbekannte Felder (z. B. von Plugins) bleiben
//! dabei erhalten.
//!
//! Plätze ([`Slot`]) auf einer Seite: Taste (Spalte, Zeile), Drehregler (Index) oder Infoleiste des
//! Neo (Index).
//!
//! Verschachtelte Aktionen (Multi Action, Key Logic …) werden über einen Pfad angesprochen: jeder
//! Index wählt einen Eintrag in `"Actions"` der aktuellen Aktion. Gruppen: `[Gruppe, Eintrag]`,
//! Plätze: `[Platz]`, leer = die Aktion auf dem Platz selbst.
//!
//! Seitenordner werden sofort angelegt, aber erst beim Schließen ([`ProfileDoc::cleanup`]) gelöscht –
//! so kann jede Änderung rückgängig gemacht werden.

use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime};

use crate::catalog as C;
use crate::i18n::gettext;
use crate::models::{model_geometry, Geometry};
use crate::paths;
use crate::profiles::{decode_page_dir, encode_page_dir, parse_pos, read_json, Action, Page};
use crate::tr;

pub const UNDO_LIMIT: usize = 200;
pub const ICON_SIZE: u32 = 144;
/// Gleiche Änderungen in diesem Abstand werden zu einem Rückgängig-Schritt zusammengefasst.
const COALESCE: Duration = Duration::from_secs(2);

/// Fehler beim Bearbeiten (Text ist bereits übersetzt).
#[derive(Debug, thiserror::Error)]
pub enum EditError {
    #[error("{0}")]
    Msg(String),
    #[error(transparent)]
    Io(#[from] std::io::Error),
}

pub type Result<T> = std::result::Result<T, EditError>;

fn msg(e: impl std::fmt::Display) -> EditError {
    EditError::Msg(e.to_string())
}

// ============================================================================================ Slot
/// Platz auf einer Seite.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Slot {
    /// Taste (Spalte, Zeile)
    Key(u8, u8),
    /// Drehregler (Index)
    Dial(u8),
    /// Infoleiste des Stream Deck Neo (Index)
    InfoBar(u8),
}

/// Controller-Typ der Infoleiste beim Anlegen (gelesen wird jeder andere Typ als Keypad/Encoder).
pub const INFOBAR_TYPE: &str = "Neo";

impl Slot {
    /// Controller-Typ im Seiten-Manifest.
    pub fn controller_type(&self) -> &'static str {
        match self {
            Slot::Key(..) => "Keypad",
            Slot::Dial(_) => "Encoder",
            Slot::InfoBar(_) => INFOBAR_TYPE,
        }
    }

    /// Positionsschlüssel im Manifest („Spalte,Zeile“ bzw. „Index,0“).
    pub fn pos_key(&self) -> String {
        match self {
            Slot::Key(c, r) => format!("{c},{r}"),
            Slot::Dial(i) | Slot::InfoBar(i) => format!("{i},0"),
        }
    }

    /// Taste oder Drehregler (für [`C::ActionType::fields_for`]); die Infoleiste zählt als Taste.
    pub fn controller(&self) -> C::Controller {
        match self {
            Slot::Dial(_) => C::DIAL,
            _ => C::KEY,
        }
    }

    pub fn is_key(&self) -> bool {
        matches!(self, Slot::Key(..))
    }

    pub fn is_dial(&self) -> bool {
        matches!(self, Slot::Dial(_))
    }

    /// Controller-Typ und Positionsschlüssel -> Slot.
    pub fn from_ref(ctype: &str, pos: &str) -> Option<Slot> {
        let (c, r) = parse_pos(pos)?;
        Some(match ctype {
            "Keypad" | "" => Slot::Key(c, r),
            "Encoder" => Slot::Dial(c),
            _ => Slot::InfoBar(c),
        })
    }
}

/// Slot -> (Controller-Typ, Positionsschlüssel).
pub fn slot_ref(slot: Slot) -> (&'static str, String) {
    (slot.controller_type(), slot.pos_key())
}

fn norm(u: &str) -> String {
    u.to_lowercase()
}

fn new_id() -> String {
    uuid::Uuid::new_v4().to_string()
}

fn hex_id() -> String {
    uuid::Uuid::new_v4().simple().to_string().to_uppercase()
}

/// Kompakt wie `json.dumps(..., ensure_ascii=False, separators=(",", ":"))`, über eine Zwischendatei.
fn write_json(path: &Path, data: &Value) -> std::io::Result<()> {
    let mut name = path.file_name().unwrap_or_default().to_os_string();
    name.push(".tmp");
    let tmp = path.with_file_name(name);
    std::fs::write(&tmp, serde_json::to_string(data).map_err(std::io::Error::other)?)?;
    std::fs::rename(tmp, path)
}

fn touch(p: &Path) {
    if let Ok(f) = std::fs::File::open(p) {
        let _ = f.set_modified(SystemTime::now());
    }
}

fn expanduser(p: &Path) -> PathBuf {
    match p.to_str().and_then(|s| s.strip_prefix("~/")) {
        Some(rest) => paths::home().join(rest),
        None if p == Path::new("~") => paths::home(),
        None => p.to_path_buf(),
    }
}

fn str_of<'a>(m: &'a Map<String, Value>, k: &str) -> &'a str {
    m.get(k).and_then(Value::as_str).unwrap_or("")
}

fn has_uuid(m: &Map<String, Value>) -> bool {
    !str_of(m, "UUID").is_empty()
}

fn obj(v: Value) -> Map<String, Value> {
    match v {
        Value::Object(m) => m,
        _ => Map::new(),
    }
}

/// Unterobjekt `key` (wird angelegt bzw. ersetzt, wenn es kein Objekt ist).
fn obj_entry<'a>(m: &'a mut Map<String, Value>, key: &str) -> &'a mut Map<String, Value> {
    if !m.get(key).map(Value::is_object).unwrap_or(false) {
        m.insert(key.into(), Value::Object(Map::new()));
    }
    m.get_mut(key).and_then(Value::as_object_mut).expect("gerade angelegt")
}

/// Liste `key` (wird angelegt bzw. ersetzt, wenn es keine Liste ist).
fn arr_entry<'a>(m: &'a mut Map<String, Value>, key: &str) -> &'a mut Vec<Value> {
    if !m.get(key).map(Value::is_array).unwrap_or(false) {
        m.insert(key.into(), Value::Array(Vec::new()));
    }
    m.get_mut(key).and_then(Value::as_array_mut).expect("gerade angelegt")
}

// ===================================================================================== Aktionen
/// Aktion und alle verschachtelten Aktionen (Gruppen und Plätze), veränderbar.
pub fn walk_actions_mut(data: &mut Action, f: &mut dyn FnMut(&mut Action)) {
    if has_uuid(data) {
        f(data);
    }
    let Some(list) = data.get_mut("Actions").and_then(Value::as_array_mut) else { return };
    for e in list {
        let Some(o) = e.as_object_mut() else { continue };
        if has_uuid(o) {
            walk_actions_mut(o, f);
        } else if let Some(inner) = o.get_mut("Actions").and_then(Value::as_array_mut) {
            for x in inner.iter_mut().filter_map(Value::as_object_mut) {
                walk_actions_mut(x, f);
            }
        }
    }
}

/// Aktion und alle verschachtelten Aktionen.
pub fn walk_actions(data: &Action, f: &mut dyn FnMut(&Action)) {
    if has_uuid(data) {
        f(data);
    }
    let Some(list) = data.get("Actions").and_then(Value::as_array) else { return };
    for o in list.iter().filter_map(Value::as_object) {
        if has_uuid(o) {
            walk_actions(o, f);
        } else if let Some(inner) = o.get("Actions").and_then(Value::as_array) {
            for x in inner.iter().filter_map(Value::as_object) {
                walk_actions(x, f);
            }
        }
    }
}

const ENCODER_IMAGE_KEYS: [&str; 3] = ["Icon", "background", "Background"];

/// Alle Bildverweise einer Aktion (rekursiv), veränderbar.
fn image_refs_mut(data: &mut Action, f: &mut dyn FnMut(&mut String)) {
    walk_actions_mut(data, &mut |a| {
        if let Some(states) = a.get_mut("States").and_then(Value::as_array_mut) {
            for st in states.iter_mut().filter_map(Value::as_object_mut) {
                if let Some(Value::String(s)) = st.get_mut("Image") {
                    if !s.is_empty() {
                        f(s);
                    }
                }
            }
        }
        if let Some(enc) = a.get_mut("Encoder").and_then(Value::as_object_mut) {
            for k in ENCODER_IMAGE_KEYS {
                if let Some(Value::String(s)) = enc.get_mut(k) {
                    if !s.is_empty() {
                        f(s);
                    }
                }
            }
        }
    });
}

/// Alle Bildverweise einer Aktion (relativ zum Seitenordner).
pub fn image_refs(data: &Action) -> Vec<String> {
    let mut out = Vec::new();
    walk_actions(data, &mut |a| {
        for st in a.get("States").and_then(Value::as_array).into_iter().flatten().filter_map(Value::as_object) {
            let s = str_of(st, "Image");
            if !s.is_empty() {
                out.push(s.to_string());
            }
        }
        if let Some(enc) = a.get("Encoder").and_then(Value::as_object) {
            for k in ENCODER_IMAGE_KEYS {
                let s = str_of(enc, k);
                if !s.is_empty() {
                    out.push(s.to_string());
                }
            }
        }
    });
    out
}

/// UUIDs der Ordner, die eine Aktion (auch verschachtelt) öffnet.
pub fn folder_refs(data: &Action) -> Vec<String> {
    let mut out = Vec::new();
    walk_actions(data, &mut |a| {
        if str_of(a, "UUID") == C::FOLDER_UUID {
            let u = a.get("Settings").and_then(Value::as_object).map(|s| str_of(s, "ProfileUUID")).unwrap_or("");
            if !u.is_empty() {
                out.push(u.to_string());
            }
        }
    });
    out
}

fn resolve<'a>(a: &'a Action, path: &[usize]) -> Option<&'a Action> {
    let mut cur = a;
    for &i in path {
        cur = cur.get("Actions")?.as_array()?.get(i)?.as_object()?;
    }
    Some(cur)
}

fn resolve_mut<'a>(a: &'a mut Action, path: &[usize]) -> Option<&'a mut Action> {
    let mut cur = a;
    for &i in path {
        cur = cur.get_mut("Actions")?.as_array_mut()?.get_mut(i)?.as_object_mut()?;
    }
    Some(cur)
}

// ===================================================================================== EditPage
/// Eine Seite (oder ein Ordner) im Speicher.
#[derive(Debug, Clone, PartialEq)]
pub struct EditPage {
    pub uuid: String,
    pub dir: PathBuf,
    pub manifest: Map<String, Value>,
}

impl EditPage {
    /// Seite aus einem Manifest; das alte Format (nur „Actions“) wird in „Controllers“ umgewandelt.
    pub fn new(uuid: &str, dir: &Path, mut manifest: Map<String, Value>) -> Self {
        if manifest.get("Controllers").map(Value::is_null).unwrap_or(true) {
            let ctrls = match manifest.remove("Actions") {
                Some(actions) => vec![json!({"Type": "Keypad", "Actions": actions})],
                None => Vec::new(),
            };
            manifest.insert("Controllers".into(), Value::Array(ctrls));
        }
        Self { uuid: uuid.to_string(), dir: dir.to_path_buf(), manifest }
    }

    pub fn name(&self) -> String {
        str_of(&self.manifest, "Name").to_string()
    }

    fn matches(c: &Map<String, Value>, ctype: &str) -> bool {
        let t = c.get("Type").and_then(Value::as_str).filter(|t| !t.is_empty()).unwrap_or("Keypad");
        match ctype {
            "Keypad" | "Encoder" => t == ctype,
            _ => t != "Keypad" && t != "Encoder",
        }
    }

    /// Controller eines Typs (Keypad, Encoder, sonst Infoleiste).
    pub fn controller(&self, ctype: &str) -> Option<&Map<String, Value>> {
        self.manifest.get("Controllers")?.as_array()?.iter().filter_map(Value::as_object).find(|c| Self::matches(c, ctype))
    }

    /// Controller zum Ändern; `create` legt ihn an. „Actions“ ist danach immer ein Objekt.
    pub fn controller_mut(&mut self, ctype: &str, create: bool) -> Option<&mut Map<String, Value>> {
        let ctrls = self.manifest.get_mut("Controllers")?.as_array_mut()?;
        let idx = match ctrls.iter().position(|c| c.as_object().map(|c| Self::matches(c, ctype)).unwrap_or(false)) {
            Some(i) => i,
            None if create => {
                ctrls.push(json!({"Actions": {}, "Type": ctype}));
                ctrls.len() - 1
            }
            None => return None,
        };
        let c = ctrls[idx].as_object_mut()?;
        obj_entry(c, "Actions");
        Some(c)
    }

    /// Aktion auf einem Platz (nur mit UUID).
    pub fn get(&self, slot: Slot) -> Option<&Action> {
        let c = self.controller(slot.controller_type())?;
        c.get("Actions")?.as_object()?.get(&slot.pos_key())?.as_object().filter(|a| has_uuid(a))
    }

    pub fn get_mut(&mut self, slot: Slot) -> Option<&mut Action> {
        let c = self.controller_mut(slot.controller_type(), false)?;
        c.get_mut("Actions")?.as_object_mut()?.get_mut(&slot.pos_key())?.as_object_mut().filter(|a| has_uuid(a))
    }

    /// Platz belegen (None = leeren).
    pub fn set(&mut self, slot: Slot, data: Option<Action>) {
        let Some(c) = self.controller_mut(slot.controller_type(), data.is_some()) else { return };
        let actions = obj_entry(c, "Actions");
        match data {
            Some(d) => {
                actions.insert(slot.pos_key(), Value::Object(d));
            }
            None => {
                actions.remove(&slot.pos_key());
            }
        }
    }

    /// Alle belegten Plätze.
    pub fn items(&self) -> Vec<(Slot, &Action)> {
        let mut out = Vec::new();
        let Some(ctrls) = self.manifest.get("Controllers").and_then(Value::as_array) else { return out };
        for c in ctrls.iter().filter_map(Value::as_object) {
            let ctype = str_of(c, "Type");
            let Some(actions) = c.get("Actions").and_then(Value::as_object) else { continue };
            for (pos, a) in actions {
                let (Some(slot), Some(a)) = (Slot::from_ref(ctype, pos), a.as_object()) else { continue };
                if has_uuid(a) {
                    out.push((slot, a));
                }
            }
        }
        out
    }

    /// Alle Aktionen eines Manifests zum Ändern.
    fn for_each_action_mut(manifest: &mut Map<String, Value>, f: &mut dyn FnMut(&mut Action)) {
        let Some(ctrls) = manifest.get_mut("Controllers").and_then(Value::as_array_mut) else { return };
        for c in ctrls.iter_mut().filter_map(Value::as_object_mut) {
            let Some(actions) = c.get_mut("Actions").and_then(Value::as_object_mut) else { continue };
            for a in actions.values_mut().filter_map(Value::as_object_mut) {
                if has_uuid(a) {
                    f(a);
                }
            }
        }
    }

    /// [`Page`] für Vorschau und Darstellung.
    pub fn runtime_page(&self) -> Page {
        Page::from_manifest(&self.uuid, &self.dir, &Value::Object(self.manifest.clone()))
    }
}

/// Inhalt der Zwischenablage (unabhängig vom Profil; Ordner werden beim Kopieren mitgenommen).
#[derive(Debug, Clone)]
pub struct Clip {
    pub data: Action,
    /// Seitenordner, aus dem die Aktion stammt (für Bilder)
    pub dir: PathBuf,
    /// Ordnerseiten, die die Aktion öffnet (auch verschachtelt): normierte UUID -> Seite
    pub folders: BTreeMap<String, EditPage>,
}

type Snapshot = (Map<String, Value>, BTreeMap<String, EditPage>);

// =================================================================================== ProfileDoc
/// Ein Profil zum Bearbeiten.
pub struct ProfileDoc {
    pub root: PathBuf,
    pub id: String,
    pub manifest: Map<String, Value>,
    /// normierte UUID (Kleinbuchstaben) -> Seite
    pub pages: BTreeMap<String, EditPage>,
    /// Seitenordner heißen nach der UUID (v3) statt Base32 (v2)
    pub v3_names: bool,
    disk: HashMap<String, Value>,
    disk_profile: Value,
    initially_reachable: HashSet<String>,
    created_dirs: HashSet<PathBuf>,
    created_images: HashSet<PathBuf>,
    undo_stack: Vec<Snapshot>,
    redo_stack: Vec<Snapshot>,
    last_change: (Option<String>, Option<Instant>),
    /// wird nach jeder Änderung aufgerufen
    pub on_change: Option<Box<dyn FnMut()>>,
}

impl std::fmt::Debug for ProfileDoc {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ProfileDoc").field("root", &self.root).field("pages", &self.pages.len()).finish()
    }
}

fn is_uuid_name(name: &str) -> bool {
    name.contains('-') && uuid::Uuid::parse_str(name).is_ok()
}

fn read_obj(p: &Path) -> Option<Value> {
    read_json(p).ok().filter(Value::is_object)
}

impl ProfileDoc {
    /// Profilordner (`<ID>.sdProfile`) öffnen.
    pub fn open(root: &Path) -> Result<Self> {
        let root = root.to_path_buf();
        if !root.join("manifest.json").exists() {
            return Err(EditError::Msg(tr!("Not a profile: {path}", path = root.display())));
        }
        let fname = root.file_name().and_then(|n| n.to_str()).unwrap_or("");
        let id = fname.strip_suffix(".sdProfile").unwrap_or(fname).to_string();
        let disk_profile = read_json(&root.join("manifest.json")).map_err(msg)?;
        let manifest = disk_profile.as_object().cloned().ok_or_else(|| EditError::Msg(tr!("Not a profile: {path}", path = root.display())))?;
        let mut pages = BTreeMap::new();
        let mut disk = HashMap::new();
        let base = root.join("Profiles");
        let mut dirs: Vec<PathBuf> = std::fs::read_dir(&base).map(|rd| rd.flatten().map(|e| e.path()).collect()).unwrap_or_default();
        dirs.sort();
        for d in dirs {
            if !d.join("manifest.json").exists() {
                continue;
            }
            let Some(u) = d.file_name().and_then(|n| n.to_str()).and_then(decode_page_dir) else { continue };
            let Some(m) = read_obj(&d.join("manifest.json")) else { continue };
            disk.insert(norm(&u), m.clone());
            pages.insert(norm(&u), EditPage::new(&u, &d, obj(m)));
        }
        if pages.is_empty() && manifest.contains_key("Actions") {
            return Err(EditError::Msg(gettext("Profiles in format v1 (Elgato app 4.x) cannot be edited")));
        }
        let v3_names = pages.values().any(|p| p.dir.file_name().and_then(|n| n.to_str()).map(is_uuid_name).unwrap_or(false));
        let mut doc = Self {
            root,
            id,
            manifest,
            pages,
            v3_names,
            disk,
            disk_profile,
            initially_reachable: HashSet::new(),
            created_dirs: HashSet::new(),
            created_images: HashSet::new(),
            undo_stack: Vec::new(),
            redo_stack: Vec::new(),
            last_change: (None, None),
            on_change: None,
        };
        // Verweise im Manifest auf die tatsächliche Schreibweise der Seiten-UUIDs abbilden
        for pid in doc.page_ids() {
            if let Some(pg) = doc.pages.get_mut(&norm(&pid)) {
                pg.uuid = pid;
            }
        }
        doc.initially_reachable = doc.reachable();
        Ok(doc)
    }

    // ============================================================================== Profil
    pub fn name(&self) -> String {
        let n = str_of(&self.manifest, "Name");
        if n.is_empty() { self.id.clone() } else { n.to_string() }
    }

    pub fn model(&self) -> String {
        self.manifest.get("Device").and_then(Value::as_object).map(|d| str_of(d, "Model").to_string()).unwrap_or_default()
    }

    /// Raster: aus dem Modell, sonst aus den belegten Plätzen.
    pub fn geometry(&self) -> Geometry {
        if let Some(g) = model_geometry(&self.model()) {
            return g;
        }
        let (mut cols, mut rows, mut dials) = (0u8, 0u8, 0u8);
        for p in self.pages.values() {
            for (slot, _) in p.items() {
                match slot {
                    Slot::Key(c, r) => {
                        cols = cols.max(c + 1);
                        rows = rows.max(r + 1);
                    }
                    Slot::Dial(i) => dials = dials.max(i + 1),
                    Slot::InfoBar(_) => {}
                }
            }
        }
        Geometry { cols: if cols == 0 { 5 } else { cols }, rows: if rows == 0 { 3 } else { rows }, dials, touch_keys: 0 }
    }

    pub fn rename(&mut self, name: &str) {
        self.checkpoint(None);
        let n = if name.trim().is_empty() { self.name() } else { name.trim().to_string() };
        self.manifest.insert("Name".into(), Value::String(n));
        self.changed();
    }

    pub fn set_model(&mut self, code: &str) {
        self.checkpoint(None);
        obj_entry(&mut self.manifest, "Device").insert("Model".into(), Value::String(code.to_string()));
        self.changed();
    }

    // ============================================================================== Seiten
    /// Seiten in der Reihenfolge des Profils (ohne Ordner).
    pub fn page_ids(&self) -> Vec<String> {
        self.manifest
            .get("Pages")
            .and_then(|p| p.get("Pages"))
            .and_then(Value::as_array)
            .map(|a| a.iter().filter_map(|v| v.as_str().map(str::to_string)).collect())
            .unwrap_or_default()
    }

    fn set_page_ids(&mut self, ids: Vec<String>) {
        obj_entry(&mut self.manifest, "Pages").insert("Pages".into(), json!(ids));
    }

    pub fn page(&self, uuid: &str) -> Option<&EditPage> {
        self.pages.get(&norm(uuid))
    }

    pub fn page_mut(&mut self, uuid: &str) -> Option<&mut EditPage> {
        self.pages.get_mut(&norm(uuid))
    }

    /// Seite ist ein Ordner (steht nicht in der Seitenliste).
    pub fn is_folder(&self, uuid: &str) -> bool {
        let n = norm(uuid);
        !self.page_ids().iter().any(|p| norm(p) == n)
    }

    /// Alle Seiten (normierte UUIDs), die über Seitenliste und Ordner erreichbar sind.
    pub fn reachable(&self) -> HashSet<String> {
        let mut seen = HashSet::new();
        let mut todo: Vec<String> = self.page_ids().iter().map(|p| norm(p)).collect();
        while let Some(k) = todo.pop() {
            if seen.contains(&k) {
                continue;
            }
            let Some(pg) = self.pages.get(&k) else { continue };
            seen.insert(k);
            for (_, a) in pg.items() {
                todo.extend(folder_refs(a).iter().map(|u| norm(u)));
            }
        }
        seen
    }

    /// Titel eines Ordners: Seitenname, sonst Titel der Ordnertaste, sonst „Folder“.
    pub fn folder_title(&self, uuid: &str) -> String {
        if let Some(pg) = self.page(uuid) {
            if !pg.name().is_empty() {
                return pg.name();
            }
        }
        let n = norm(uuid);
        for p in self.pages.values() {
            for (_, a) in p.items() {
                let target = a.get("Settings").and_then(Value::as_object).map(|s| str_of(s, "ProfileUUID")).unwrap_or("");
                if str_of(a, "UUID") == C::FOLDER_UUID && norm(target) == n {
                    let title = a.get("States").and_then(|s| s.get(0)).and_then(|s| s.get("Title")).and_then(Value::as_str).unwrap_or("");
                    return if title.is_empty() { gettext("Folder") } else { title.to_string() };
                }
            }
        }
        gettext("Folder")
    }

    fn new_page(&mut self, name: &str, folder: bool) -> String {
        let u = new_id();
        let dname = if self.v3_names { u.to_uppercase() } else { encode_page_dir(&u).unwrap_or_else(|| u.to_uppercase()) };
        let d = self.root.join("Profiles").join(dname);
        if let Err(e) = std::fs::create_dir_all(&d) {
            log::warn!("{}: {e}", d.display());
        }
        self.created_dirs.insert(d.clone());
        let mut ctrls = vec![json!({"Actions": {}, "Type": "Keypad"})];
        if self.geometry().dials > 0 {
            ctrls.push(json!({"Actions": {}, "Type": "Encoder"}));
        }
        let mut pg = EditPage::new(&u, &d, obj(json!({"Controllers": ctrls, "Icon": "", "Name": name})));
        if folder {
            pg.set(Slot::Key(0, 0), Some(C::new_action(C::BACK_UUID)));
        }
        self.pages.insert(norm(&u), pg);
        u
    }

    /// Neue Seite (hinter `after`, sonst am Ende); gibt ihre UUID zurück.
    pub fn add_page(&mut self, after: Option<&str>) -> String {
        self.checkpoint(None);
        let u = self.new_page("", false);
        let mut ids = self.page_ids();
        let idx = after.and_then(|a| ids.iter().position(|p| norm(p) == norm(a))).map(|i| i + 1).unwrap_or(ids.len());
        ids.insert(idx, u.clone());
        self.set_page_ids(ids);
        let pages = obj_entry(&mut self.manifest, "Pages");
        for k in ["Default", "Current"] {
            if str_of(pages, k).is_empty() {
                pages.insert(k.into(), Value::String(u.clone()));
            }
        }
        self.changed();
        u
    }

    /// Seite aus der Seitenliste nehmen (der Ordner bleibt bis [`cleanup`](Self::cleanup)).
    pub fn remove_page(&mut self, uuid: &str) -> Result<()> {
        let ids = self.page_ids();
        if ids.len() <= 1 {
            return Err(EditError::Msg(gettext("The last page cannot be deleted")));
        }
        self.checkpoint(None);
        let n = norm(uuid);
        let ids: Vec<String> = ids.into_iter().filter(|p| norm(p) != n).collect();
        let first = ids.first().cloned().unwrap_or_default();
        self.set_page_ids(ids);
        let pages = obj_entry(&mut self.manifest, "Pages");
        for k in ["Default", "Current"] {
            if norm(str_of(pages, k)) == n {
                pages.insert(k.into(), Value::String(first.clone()));
            }
        }
        self.changed();
        Ok(())
    }

    /// Seite um `delta` Plätze verschieben.
    pub fn move_page(&mut self, uuid: &str, delta: i64) {
        let mut ids = self.page_ids();
        let n = norm(uuid);
        let Some(i) = ids.iter().position(|p| norm(p) == n) else { return };
        let j = (i as i64 + delta).clamp(0, ids.len() as i64 - 1) as usize;
        if i == j {
            return;
        }
        self.checkpoint(None);
        let p = ids.remove(i);
        ids.insert(j, p);
        self.set_page_ids(ids);
        self.changed();
    }

    pub fn rename_page(&mut self, uuid: &str, name: &str) {
        if self.page(uuid).is_none() {
            return;
        }
        self.checkpoint(Some(&format!("page-name|{}", norm(uuid))));
        if let Some(pg) = self.page_mut(uuid) {
            pg.manifest.insert("Name".into(), Value::String(name.to_string()));
        }
        self.changed();
    }

    // ============================================================================== Tasten
    /// Aktion auf einem Platz.
    pub fn get(&self, page: &str, slot: Slot) -> Option<&Action> {
        self.page(page)?.get(slot)
    }

    /// Aktion oder verschachtelte Aktion (Pfad siehe Modulbeschreibung).
    pub fn action(&self, page: &str, slot: Slot, path: &[usize]) -> Option<&Action> {
        resolve(self.get(page, slot)?, path)
    }

    /// Aktion zum direkten Bearbeiten – vorher [`checkpoint`](Self::checkpoint), danach
    /// [`changed`](Self::changed) aufrufen.
    pub fn action_mut(&mut self, page: &str, slot: Slot, path: &[usize]) -> Option<&mut Action> {
        resolve_mut(self.page_mut(page)?.get_mut(slot)?, path)
    }

    /// Neue Aktion aus dem Katalog auf einen Platz legen (ersetzt den alten Inhalt). Ordner bekommen
    /// gleich ihre eigene Seite mit Zurück-Taste.
    pub fn place(&mut self, page: &str, slot: Slot, type_uuid: &str) -> Option<Action> {
        self.page(page)?;
        self.checkpoint(None);
        let mut data = C::new_action(type_uuid);
        if type_uuid == C::FOLDER_UUID {
            let folder = self.new_page("", true);
            obj_entry(&mut data, "Settings").insert("ProfileUUID".into(), Value::String(folder));
            if let Some(st) = data.get_mut("States").and_then(|s| s.get_mut(0)).and_then(Value::as_object_mut) {
                st.insert("Title".into(), Value::String(gettext("Folder")));
            }
        }
        self.page_mut(page)?.set(slot, Some(data.clone()));
        self.changed();
        Some(data)
    }

    /// Fertige Aktion auf einen Platz legen (z. B. Plugin-Aktion aus [`C::new_action_of`]).
    pub fn place_action(&mut self, page: &str, slot: Slot, data: Action) {
        if self.page(page).is_none() {
            return;
        }
        self.checkpoint(None);
        if let Some(pg) = self.page_mut(page) {
            pg.set(slot, Some(data));
        }
        self.changed();
    }

    pub fn remove(&mut self, page: &str, slot: Slot) {
        if self.get(page, slot).is_none() {
            return;
        }
        self.checkpoint(None);
        if let Some(pg) = self.page_mut(page) {
            pg.set(slot, None);
        }
        self.changed();
    }

    /// Aktion verschieben; ein belegter Zielplatz wird getauscht.
    pub fn move_action(&mut self, src_page: &str, src_slot: Slot, dst_page: &str, dst_slot: Slot) {
        if norm(src_page) == norm(dst_page) && src_slot == dst_slot {
            return;
        }
        let Some(mut a) = self.get(src_page, src_slot).cloned() else { return };
        let Some(dp) = self.page(dst_page) else { return };
        let dst_dir = dp.dir.clone();
        let mut b = dp.get(dst_slot).cloned();
        let src_dir = self.page(src_page).map(|p| p.dir.clone()).unwrap_or_default();
        self.checkpoint(None);
        self.relocate(&mut a, &src_dir, &dst_dir);
        if let Some(b) = b.as_mut() {
            self.relocate(b, &dst_dir, &src_dir);
        }
        if let Some(sp) = self.page_mut(src_page) {
            sp.set(src_slot, b);
        }
        if let Some(dp) = self.page_mut(dst_page) {
            dp.set(dst_slot, Some(a));
        }
        self.changed();
    }

    /// Inhalt für die Zwischenablage (mit allen Ordnern, die die Aktion öffnet).
    pub fn copy(&self, page: &str, slot: Slot) -> Option<Clip> {
        let a = self.get(page, slot)?;
        let mut folders = BTreeMap::new();
        let mut todo = folder_refs(a);
        while let Some(u) = todo.pop() {
            let n = norm(&u);
            if folders.contains_key(&n) {
                continue;
            }
            let Some(pg) = self.page(&u) else { continue };
            for (_, x) in pg.items() {
                todo.extend(folder_refs(x));
            }
            folders.insert(n, pg.clone());
        }
        Some(Clip { data: a.clone(), dir: self.page(page)?.dir.clone(), folders })
    }

    /// Zwischenablage einfügen: neue Aktions-IDs, Ordner als eigenständige Kopien, Bilder umkopiert.
    pub fn paste(&mut self, page: &str, slot: Slot, clip: &Clip) -> Option<Action> {
        let dst_dir = self.page(page)?.dir.clone();
        self.checkpoint(None);
        let mut data = clip.data.clone();
        self.relocate(&mut data, &clip.dir, &dst_dir);
        self.renew_ids(&mut data, &clip.dir, clip, 0);
        self.page_mut(page)?.set(slot, Some(data.clone()));
        self.changed();
        Some(data)
    }

    /// Neue ActionIDs; Ordner werden geklont (oder leer neu angelegt, wenn die Quelle fehlt).
    fn renew_ids(&mut self, data: &mut Action, src_dir: &Path, clip: &Clip, depth: usize) {
        walk_actions_mut(data, &mut |a| {
            a.insert("ActionID".into(), Value::String(new_id()));
            if str_of(a, "UUID") == C::FOLDER_UUID {
                let settings = obj_entry(a, "Settings");
                let src = str_of(settings, "ProfileUUID").to_string();
                let found = if depth < 32 { self.find_folder(&src, src_dir, clip) } else { None };
                let new = match found {
                    Some(sub) => self.clone_folder(&sub, clip, depth + 1),
                    None => self.new_page("", true),
                };
                settings.insert("ProfileUUID".into(), Value::String(new));
            }
        });
    }

    fn clone_folder(&mut self, src: &EditPage, clip: &Clip, depth: usize) -> String {
        let u = self.new_page(&src.name(), false);
        let dst_dir = self.pages[&norm(&u)].dir.clone();
        let mut manifest = src.manifest.clone();
        EditPage::for_each_action_mut(&mut manifest, &mut |a| {
            self.relocate(a, &src.dir, &dst_dir);
            self.renew_ids(a, &src.dir, clip, depth);
        });
        if let Some(pg) = self.pages.get_mut(&norm(&u)) {
            pg.manifest = manifest;
        }
        u
    }

    /// Ordnerseite suchen: Zwischenablage, dieses Profil, dann der Profilordner der Quelle.
    fn find_folder(&self, uuid: &str, src_dir: &Path, clip: &Clip) -> Option<EditPage> {
        if uuid.is_empty() {
            return None;
        }
        let n = norm(uuid);
        if let Some(p) = clip.folders.get(&n).or_else(|| self.pages.get(&n)) {
            return Some(p.clone());
        }
        let base = src_dir.parent()?;
        for e in std::fs::read_dir(base).ok()?.flatten() {
            let d = e.path();
            let Some(dec) = d.file_name().and_then(|n| n.to_str()).and_then(decode_page_dir) else { continue };
            if norm(&dec) == n && d.join("manifest.json").exists() {
                let m = read_obj(&d.join("manifest.json"))?;
                return Some(EditPage::new(uuid, &d, obj(m)));
            }
        }
        None
    }

    /// Bilder einer Aktion in den Ordner der Zielseite kopieren (bei Namensgleichheit mit anderem
    /// Inhalt unter neuem Namen).
    fn relocate(&mut self, data: &mut Action, src_dir: &Path, dst_dir: &Path) {
        if src_dir == dst_dir {
            return;
        }
        let created = &mut self.created_images;
        image_refs_mut(data, &mut |rel| {
            let src = src_dir.join(rel.as_str());
            if !src.is_file() {
                return;
            }
            let mut dst = dst_dir.join(rel.as_str());
            if dst.exists() && std::fs::read(&dst).ok() != std::fs::read(&src).ok() {
                let ext = src.extension().and_then(|e| e.to_str()).map(|e| format!(".{e}")).unwrap_or_default();
                *rel = format!("Images/{}{ext}", hex_id());
                dst = dst_dir.join(rel.as_str());
            }
            if !dst.exists() {
                if let Some(p) = dst.parent() {
                    let _ = std::fs::create_dir_all(p);
                }
                match std::fs::copy(&src, &dst) {
                    Ok(_) => {
                        created.insert(dst);
                    }
                    Err(e) => log::warn!("{}: {e}", src.display()),
                }
            }
        });
    }

    // ------------------------------------------------------------------- Eigenschaften
    fn coalesce_key(kind: &str, page: &str, slot: Slot, path: &[usize], rest: &str) -> String {
        format!("{kind}|{}|{slot:?}|{path:?}|{rest}", norm(page))
    }

    /// Einstellung („Settings“) einer Aktion ändern; Tippen wird zu einem Rückgängig-Schritt.
    pub fn update_settings(&mut self, page: &str, slot: Slot, path: &[usize], key: &str, value: Value) {
        if self.action(page, slot, path).is_none() {
            return;
        }
        self.checkpoint(Some(&Self::coalesce_key("set", page, slot, path, key)));
        if let Some(a) = self.action_mut(page, slot, path) {
            obj_entry(a, "Settings").insert(key.to_string(), value);
        }
        self.changed();
    }

    /// Wert eines Zustands („States“) ändern; fehlende Zustände werden angelegt.
    pub fn update_state(&mut self, page: &str, slot: Slot, path: &[usize], state_idx: usize, key: &str, value: Value) {
        if self.action(page, slot, path).is_none() {
            return;
        }
        self.checkpoint(Some(&Self::coalesce_key("state", page, slot, path, &format!("{state_idx}|{key}"))));
        if let Some(a) = self.action_mut(page, slot, path) {
            let states = arr_entry(a, "States");
            while states.len() <= state_idx {
                states.push(Value::Object(C::default_state()));
            }
            if !states[state_idx].is_object() {
                states[state_idx] = Value::Object(C::default_state());
            }
            if let Some(st) = states[state_idx].as_object_mut() {
                st.insert(key.to_string(), value);
            }
        }
        self.changed();
    }

    /// Hotkey Nummer `idx` setzen (fehlende werden leer angelegt).
    pub fn set_hotkey(&mut self, page: &str, slot: Slot, path: &[usize], idx: usize, hk: Value) {
        if self.action(page, slot, path).is_none() {
            return;
        }
        self.checkpoint(None);
        if let Some(a) = self.action_mut(page, slot, path) {
            let hks = arr_entry(obj_entry(a, "Settings"), "Hotkeys");
            while hks.len() <= idx {
                hks.push(C::empty_hotkey());
            }
            hks[idx] = hk;
        }
        self.changed();
    }

    /// Bilddatei übernehmen (PNG/JPG/SVG/GIF …) und dem Zustand zuweisen; gibt den relativen Pfad zurück.
    pub fn set_icon(&mut self, page: &str, slot: Slot, path: &[usize], state_idx: usize, image_file: &Path) -> Result<Option<String>> {
        if self.action(page, slot, path).is_none() {
            return Ok(None);
        }
        let rel = self.import_image(page, image_file)?;
        self.update_state(page, slot, path, state_idx, "Image", Value::String(rel.clone()));
        Ok(Some(rel))
    }

    fn images_dir(&self, page: &str) -> Result<PathBuf> {
        let pg = self.page(page).ok_or_else(|| EditError::Msg(format!("{page}: ?")))?;
        let d = pg.dir.join("Images");
        std::fs::create_dir_all(&d)?;
        Ok(d)
    }

    /// Bild in den Ordner `Images/` der Seite übernehmen: SVG und GIF unverändert, alles andere als
    /// PNG 144×144 (eingepasst, transparent aufgefüllt). Gibt `Images/<Name>` zurück.
    pub fn import_image(&mut self, page: &str, image_file: &Path) -> Result<String> {
        let src = expanduser(image_file);
        let img_dir = self.images_dir(page)?;
        let name = hex_id();
        let ext = src.extension().and_then(|e| e.to_str()).map(str::to_lowercase).unwrap_or_default();
        let dst = if ext == "svg" || ext == "gif" {
            let dst = img_dir.join(format!("{name}.{ext}"));
            std::fs::copy(&src, &dst)?;
            dst
        } else {
            let im = image::open(&src).map_err(|e| EditError::Msg(tr!("Image not readable: {error}", error = e)))?.to_rgba8();
            let im = thumbnail(image::DynamicImage::ImageRgba8(im), ICON_SIZE, ICON_SIZE).to_rgba8();
            let mut canvas = image::RgbaImage::new(ICON_SIZE, ICON_SIZE);
            let (x, y) = ((ICON_SIZE - im.width()) / 2, (ICON_SIZE - im.height()) / 2);
            image::imageops::overlay(&mut canvas, &im, x as i64, y as i64);
            let dst = img_dir.join(format!("{name}.png"));
            canvas.save(&dst).map_err(msg)?;
            dst
        };
        let rel = format!("Images/{}", dst.file_name().and_then(|n| n.to_str()).unwrap_or(""));
        self.created_images.insert(dst);
        Ok(rel)
    }

    // ---------------------------------------------------------------- Hintergrundbilder
    /// (Bildpfad, falls vorhanden; Abdunkeln in %) des Seiten-Hintergrunds.
    pub fn background(&self, page: &str) -> (Option<PathBuf>, i64) {
        let Some(pg) = self.page(page) else { return (None, 0) };
        let Some(bg) = pg.manifest.get("FreeDeckBackground").and_then(Value::as_object) else { return (None, 0) };
        let rel = str_of(bg, "Image");
        let p = (!rel.is_empty()).then(|| pg.dir.join(rel)).filter(|p| p.exists());
        (p, bg.get("Dim").and_then(Value::as_i64).unwrap_or(0))
    }

    /// Hintergrundbild über alle Tasten setzen (`image_file` None und `keep_image` false = entfernen);
    /// `dim` 0–90 % (None = bisheriger Wert, sonst 30).
    pub fn set_background(&mut self, page: &str, image_file: Option<&Path>, dim: Option<i64>, keep_image: bool) -> Result<()> {
        let Some(pg) = self.page(page) else { return Ok(()) };
        let cur = pg.manifest.get("FreeDeckBackground").and_then(Value::as_object).cloned().unwrap_or_default();
        let rel = match image_file {
            Some(f) => Some(Value::String(self.import_background(page, f, false)?)),
            None if keep_image => Some(cur.get("Image").cloned().unwrap_or(Value::Null)),
            None => None,
        };
        self.checkpoint(None);
        let dim = dim.unwrap_or_else(|| cur.get("Dim").and_then(Value::as_i64).unwrap_or(30));
        if let Some(pg) = self.page_mut(page) {
            match rel {
                Some(rel) => {
                    pg.manifest.insert("FreeDeckBackground".into(), json!({"Image": rel, "Dim": dim}));
                }
                None => {
                    pg.manifest.remove("FreeDeckBackground");
                }
            }
        }
        self.changed();
        Ok(())
    }

    /// Hintergrund des Touchstreifens (falls vorhanden).
    pub fn strip_background(&self, page: &str) -> Option<PathBuf> {
        let pg = self.page(page)?;
        let rel = str_of(pg.controller("Encoder")?, "Background");
        let p = (!rel.is_empty()).then(|| pg.dir.join(rel))?;
        p.exists().then_some(p)
    }

    /// Hintergrund des Touchstreifens setzen (Stream Deck +, wie in der Elgato-App); None = entfernen.
    pub fn set_strip_background(&mut self, page: &str, image_file: Option<&Path>) -> Result<()> {
        if self.page(page).is_none() {
            return Ok(());
        }
        let rel = match image_file {
            Some(f) => Some(self.import_background(page, f, true)?),
            None => None,
        };
        self.checkpoint(None);
        if let Some(pg) = self.page_mut(page) {
            if let Some(c) = pg.controller_mut("Encoder", rel.is_some()) {
                match rel {
                    Some(r) => {
                        c.insert("Background".into(), Value::String(r));
                    }
                    None => {
                        c.remove("Background");
                    }
                }
            }
        }
        self.changed();
        Ok(())
    }

    /// Bild unverändert (nur verkleinert, EXIF-Drehung angewandt) als PNG übernehmen.
    fn import_background(&mut self, page: &str, image_file: &Path, strip: bool) -> Result<String> {
        let src = expanduser(image_file);
        let img_dir = self.images_dir(page)?;
        let not_readable = |e: &dyn std::fmt::Display| EditError::Msg(tr!("Image not readable: {error}", error = e));
        let im = (|| -> image::ImageResult<image::DynamicImage> {
            use image::ImageDecoder;
            let mut dec = image::ImageReader::open(&src)?.with_guessed_format()?.into_decoder()?;
            let orientation = dec.orientation()?;
            let mut im = image::DynamicImage::from_decoder(dec)?;
            im.apply_orientation(orientation);
            Ok(im)
        })()
        .map_err(|e| not_readable(&e))?;
        let (w, h) = if strip { (1600, 400) } else { (1600, 1600) };
        let im = image::DynamicImage::ImageRgb8(thumbnail(im, w, h).to_rgb8());
        let mut id = hex_id();
        id.truncate(12);
        let dst = img_dir.join(format!("{}{id}.png", if strip { "Strip" } else { "Background" }));
        im.save(&dst).map_err(|e| not_readable(&e))?;
        let rel = format!("Images/{}", dst.file_name().and_then(|n| n.to_str()).unwrap_or(""));
        self.created_images.insert(dst);
        Ok(rel)
    }

    /// Einstellungen einer Aktion vollständig ersetzen (JSON-Editor).
    pub fn set_settings_json(&mut self, page: &str, slot: Slot, path: &[usize], settings: Map<String, Value>) {
        if self.action(page, slot, path).is_none() {
            return;
        }
        self.checkpoint(None);
        if let Some(a) = self.action_mut(page, slot, path) {
            a.insert("Settings".into(), Value::Object(settings));
        }
        self.changed();
    }

    /// Nach direkter Bearbeitung über [`action_mut`](Self::action_mut) aufrufen.
    pub fn changed(&mut self) {
        if let Some(cb) = self.on_change.as_mut() {
            cb();
        }
    }

    /// Wurde das Profil-Manifest außerhalb geändert (z. B. neue Seite aus FreeWave)?
    pub fn disk_changed(&self) -> bool {
        match read_json(&self.root.join("manifest.json")) {
            Ok(disk) => disk != self.disk_profile,
            Err(_) => false,
        }
    }

    /// Seiten neu einlesen, die außerhalb des Editors geändert wurden (z. B. von einer Plugin-
    /// Einstellungsseite). Seiten mit ungespeicherten Änderungen im Editor bleiben unberührt.
    pub fn reload_external(&mut self) -> Vec<String> {
        let mut changed = Vec::new();
        for (k, p) in self.pages.iter_mut() {
            let Some(disk) = read_obj(&p.dir.join("manifest.json")) else { continue };
            let old = self.disk.get(k);
            if old == Some(&disk) {
                continue;
            }
            if old.and_then(Value::as_object) == Some(&p.manifest) {
                *p = EditPage::new(&p.uuid, &p.dir, obj(disk.clone()));
                self.disk.insert(k.clone(), disk);
                changed.push(p.uuid.clone());
            }
        }
        changed
    }

    // ============================================================================== Undo
    fn snapshot(&self) -> Snapshot {
        (self.manifest.clone(), self.pages.clone())
    }

    fn restore(&mut self, snap: Snapshot) {
        self.manifest = snap.0;
        self.pages = snap.1;
    }

    /// Vor jeder Änderung aufrufen. Gleiche `coalesce`-Schlüssel kurz hintereinander = ein Schritt.
    pub fn checkpoint(&mut self, coalesce: Option<&str>) {
        let now = Instant::now();
        if let (Some(c), (Some(last), Some(t))) = (coalesce, (&self.last_change.0, self.last_change.1)) {
            if c == last && now.duration_since(t) < COALESCE {
                self.last_change.1 = Some(now);
                return;
            }
        }
        self.last_change = (coalesce.map(str::to_string), Some(now));
        self.undo_stack.push(self.snapshot());
        if self.undo_stack.len() > UNDO_LIMIT {
            let n = self.undo_stack.len() - UNDO_LIMIT;
            self.undo_stack.drain(..n);
        }
        self.redo_stack.clear();
    }

    pub fn can_undo(&self) -> bool {
        !self.undo_stack.is_empty()
    }

    pub fn can_redo(&self) -> bool {
        !self.redo_stack.is_empty()
    }

    pub fn undo(&mut self) -> bool {
        let Some(snap) = self.undo_stack.pop() else { return false };
        self.redo_stack.push(self.snapshot());
        self.restore(snap);
        self.last_change = (None, None);
        self.changed();
        true
    }

    pub fn redo(&mut self) -> bool {
        let Some(snap) = self.redo_stack.pop() else { return false };
        self.undo_stack.push(self.snapshot());
        self.restore(snap);
        self.last_change = (None, None);
        self.changed();
        true
    }

    // ========================================================================== Speichern
    fn page_on_disk(&self, k: &str, p: &EditPage) -> bool {
        self.disk.get(k).and_then(Value::as_object) == Some(&p.manifest)
    }

    /// Gibt es ungespeicherte Änderungen?
    pub fn dirty(&self) -> bool {
        if self.disk_profile.as_object() != Some(&self.manifest) {
            return true;
        }
        self.reachable().iter().any(|k| self.pages.get(k).map(|p| !self.page_on_disk(k, p)).unwrap_or(false))
    }

    /// Geänderte Manifeste schreiben. Gibt true zurück, wenn etwas geschrieben wurde.
    pub fn save(&mut self) -> Result<bool> {
        let mut wrote = false;
        let mut reach: Vec<String> = self.reachable().into_iter().collect();
        reach.sort();
        for k in reach {
            let Some(p) = self.pages.get(&k) else { continue };
            let file = p.dir.join("manifest.json");
            if !self.page_on_disk(&k, p) || !file.exists() {
                std::fs::create_dir_all(&p.dir)?;
                let v = Value::Object(p.manifest.clone());
                write_json(&file, &v)?;
                self.disk.insert(k, v);
                wrote = true;
            }
        }
        if self.disk_profile.as_object() != Some(&self.manifest) {
            let v = Value::Object(self.manifest.clone());
            write_json(&self.root.join("manifest.json"), &v)?;
            self.disk_profile = v;
            wrote = true;
        }
        if wrote {
            // Der laufende Dienst lädt Profile neu, sobald sich der Profilordner ändert
            touch(&self.root);
        }
        Ok(wrote)
    }

    /// Nicht mehr erreichbare Seiten und ungenutzte neue Bilder entfernen (beim Schließen).
    pub fn cleanup(&mut self) -> Result<()> {
        self.save()?;
        let reach = self.reachable();
        let removable: Vec<String> = self
            .pages
            .iter()
            .filter(|(k, p)| !reach.contains(*k) && (self.initially_reachable.contains(*k) || self.created_dirs.contains(&p.dir)))
            .map(|(k, _)| k.clone())
            .collect();
        for k in removable {
            if let Some(p) = self.pages.remove(&k) {
                let _ = std::fs::remove_dir_all(&p.dir);
            }
            self.disk.remove(&k);
        }
        let canon = |p: &Path| p.canonicalize().unwrap_or_else(|_| p.to_path_buf());
        let mut used = HashSet::new();
        for k in &reach {
            let Some(p) = self.pages.get(k) else { continue };
            for (_, a) in p.items() {
                used.extend(image_refs(a).iter().map(|r| canon(&p.dir.join(r))));
            }
            let bg = p.manifest.get("FreeDeckBackground").and_then(Value::as_object).map(|b| str_of(b, "Image")).unwrap_or("");
            let strip = p.controller("Encoder").map(|c| str_of(c, "Background")).unwrap_or("");
            for rel in [bg, strip] {
                if !rel.is_empty() {
                    used.insert(canon(&p.dir.join(rel)));
                }
            }
        }
        for f in self.created_images.drain() {
            if f.exists() && !used.contains(&canon(&f)) {
                let _ = std::fs::remove_file(&f);
            }
        }
        self.created_dirs.clear();
        self.initially_reachable = reach;
        self.undo_stack.clear();
        self.redo_stack.clear();
        Ok(())
    }

    /// Als `.streamDeckProfile` exportieren (ZIP mit `<ID>.sdProfile/`, nur erreichbare Seiten).
    pub fn export(&mut self, target: &Path) -> Result<PathBuf> {
        self.save()?;
        let mut target = expanduser(target);
        if target.extension().and_then(|e| e.to_str()) != Some("streamDeckProfile") {
            let mut n = target.file_name().unwrap_or_default().to_os_string();
            n.push(".streamDeckProfile");
            target = target.with_file_name(n);
        }
        let canon = |p: &Path| p.canonicalize().unwrap_or_else(|_| p.to_path_buf());
        let reach_dirs: HashSet<PathBuf> = self.reachable().iter().filter_map(|k| self.pages.get(k)).map(|p| canon(&p.dir)).collect();
        let root_name = self.root.file_name().and_then(|n| n.to_str()).unwrap_or("").to_string();
        let mut z = zip::ZipWriter::new(std::fs::File::create(&target)?);
        let opts = zip::write::SimpleFileOptions::default().compression_method(zip::CompressionMethod::Deflated);
        let add = |z: &mut zip::ZipWriter<std::fs::File>, file: &Path, name: &str| -> Result<()> {
            z.start_file(name, opts).map_err(msg)?;
            z.write_all(&std::fs::read(file)?)?;
            Ok(())
        };
        add(&mut z, &self.root.join("manifest.json"), &format!("{root_name}/manifest.json"))?;
        let base = self.root.join("Profiles");
        let mut dirs: Vec<PathBuf> = std::fs::read_dir(&base).map(|rd| rd.flatten().map(|e| e.path()).collect()).unwrap_or_default();
        dirs.sort();
        for d in dirs {
            if !reach_dirs.contains(&canon(&d)) {
                continue;
            }
            let mut files = Vec::new();
            walk_files(&d, &mut files);
            files.sort();
            for f in files {
                if f.to_string_lossy().ends_with(".tmp") {
                    continue;
                }
                let rel = f.strip_prefix(&self.root).unwrap_or(&f);
                let rel: Vec<String> = rel.components().map(|c| c.as_os_str().to_string_lossy().into_owned()).collect();
                add(&mut z, &f, &format!("{root_name}/{}", rel.join("/")))?;
            }
        }
        z.finish().map_err(msg)?;
        Ok(target)
    }

    // ========================================================================== Vorschau
    /// [`Page`] einer Seite für Vorschau und Darstellung.
    pub fn runtime_page(&self, page: &str) -> Option<Page> {
        self.page(page).map(EditPage::runtime_page)
    }
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

/// Verkleinern wie PIL `thumbnail`: Seitenverhältnis bleibt, nie vergrößern.
fn thumbnail(im: image::DynamicImage, max_w: u32, max_h: u32) -> image::DynamicImage {
    let (w, h) = (im.width(), im.height());
    if w <= max_w && h <= max_h {
        return im;
    }
    let scale = (max_w as f64 / w as f64).min(max_h as f64 / h as f64);
    let nw = ((w as f64 * scale).round() as u32).clamp(1, max_w);
    let nh = ((h as f64 * scale).round() as u32).clamp(1, max_h);
    im.resize_exact(nw, nh, image::imageops::FilterType::Lanczos3)
}

// ===================================================================== Profile anlegen, duplizieren
/// Leeres Profil im Format v2 der Elgato-App im Profilordner anlegen.
pub fn create_profile(name: &str, model: &str, store_root: Option<&Path>) -> Result<PathBuf> {
    let store = store_root.map(Path::to_path_buf).unwrap_or_else(paths::profiles_dir);
    let pid = new_id().to_uppercase();
    init_profile(&store.join(format!("{pid}.sdProfile")), name, model)
}

/// Profilordner `root` (neu oder geleert) als leeres Profil mit einer Seite anlegen.
pub fn init_profile(root: &Path, name: &str, model: &str) -> Result<PathBuf> {
    let page = new_id();
    let pdir = root.join("Profiles").join(encode_page_dir(&page).unwrap_or_else(|| page.to_uppercase()));
    std::fs::create_dir_all(&pdir)?;
    let mut ctrls = vec![json!({"Actions": {}, "Type": "Keypad"})];
    if model_geometry(model).map(|g| g.dials > 0).unwrap_or(false) {
        ctrls.push(json!({"Actions": {}, "Type": "Encoder"}));
    }
    write_json(&pdir.join("manifest.json"), &json!({"Controllers": ctrls, "Icon": "", "Name": ""}))?;
    let name = if name.trim().is_empty() { gettext("New profile") } else { name.trim().to_string() };
    write_json(
        &root.join("manifest.json"),
        &json!({
            "Device": {"Model": model, "UUID": ""},
            "Name": name,
            "Pages": {"Current": page, "Default": page, "Pages": [page]},
            "Version": "2.0",
        }),
    )?;
    Ok(root.to_path_buf())
}

fn copy_tree(src: &Path, dst: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dst)?;
    for e in std::fs::read_dir(src)? {
        let e = e?;
        let name = e.file_name();
        if name.to_string_lossy().ends_with(".tmp") {
            continue;
        }
        let to = dst.join(&name);
        if e.file_type()?.is_dir() {
            copy_tree(&e.path(), &to)?;
        } else {
            std::fs::copy(e.path(), to)?;
        }
    }
    Ok(())
}

/// Profil unter neuem Namen kopieren (ohne Smart-Profile-Zuordnung); gibt den neuen Ordner zurück.
pub fn duplicate_profile(root: &Path, name: &str) -> Result<PathBuf> {
    let pid = new_id().to_uppercase();
    let parent = root.parent().unwrap_or(Path::new("."));
    let dst = parent.join(format!("{pid}.sdProfile"));
    copy_tree(root, &dst)?;
    let mut m = obj(read_json(&dst.join("manifest.json")).map_err(msg)?);
    m.insert("Name".into(), Value::String(name.to_string()));
    m.insert("AppIdentifier".into(), Value::String(String::new())); // Smart-Profile-Zuordnung nicht doppelt
    write_json(&dst.join("manifest.json"), &Value::Object(m))?;
    touch(parent);
    Ok(dst)
}

/// Einstellungen › Stil: Schrift, Position und Farbe der Titel in allen Profilen setzen.
/// Gibt die Zahl der geänderten Tasten/Regler zurück.
pub fn apply_title_style(store_root: &Path, key_style: &Map<String, Value>, dial_style: &Map<String, Value>) -> usize {
    let key_style: Map<String, Value> = key_style.iter().filter(|(k, _)| C::TITLE_KEYS.contains(&k.as_str())).map(|(k, v)| (k.clone(), v.clone())).collect();
    let dial_style: Map<String, Value> =
        dial_style.iter().filter(|(k, _)| k.as_str() == "ShowTitle" || k.as_str() == "TitleColor").map(|(k, v)| (k.clone(), v.clone())).collect();
    let mut roots: Vec<PathBuf> = std::fs::read_dir(store_root)
        .map(|rd| rd.flatten().map(|e| e.path()).filter(|p| p.extension().map(|e| e == "sdProfile").unwrap_or(false)).collect())
        .unwrap_or_default();
    roots.sort();
    let mut n = 0;
    for root in roots {
        let Ok(mut doc) = ProfileDoc::open(&root) else { continue };
        for page in doc.pages.values_mut() {
            let Some(ctrls) = page.manifest.get_mut("Controllers").and_then(Value::as_array_mut) else { continue };
            for c in ctrls.iter_mut().filter_map(Value::as_object_mut) {
                let style = match str_of(c, "Type") {
                    "Keypad" | "" => &key_style,
                    "Encoder" => &dial_style,
                    _ => continue,
                };
                let Some(actions) = c.get_mut("Actions").and_then(Value::as_object_mut) else { continue };
                for a in actions.values_mut().filter_map(Value::as_object_mut).filter(|a| has_uuid(a)) {
                    let mut changed = false;
                    walk_actions_mut(a, &mut |act| {
                        for st in act.get_mut("States").and_then(Value::as_array_mut).into_iter().flatten().filter_map(Value::as_object_mut) {
                            if style.iter().any(|(k, v)| st.get(k) != Some(v)) {
                                for (k, v) in style {
                                    st.insert(k.clone(), v.clone());
                                }
                                changed = true;
                            }
                        }
                    });
                    n += usize::from(changed);
                }
            }
        }
        if let Err(e) = doc.save() {
            log::warn!("{}: {e}", root.display());
        }
    }
    n
}

#[cfg(test)]
mod tests;
