//! FreeWave-Kanäle auf das Deck legen – als eigenes Profil „FreeWave“ (wie `freestream/deckexport.py`).
//!
//! FreeWave legt für ein Deck-Modell ein Profil an, dessen Tasten bzw. Drehregler die Lautstärke der
//! Mixer-Kanäle steuern, und hält es aktuell, wenn Kanäle dazukommen oder wegfallen:
//!
//!   * oben die Mix-Wahl (Alle / Monitor / Stream …) und „Kanäle 1–7“ (blättert weiter)
//!   * je Kanal eine Spalte: Kachel (drücken = stumm), darunter ein Fader über zwei Tasten
//!   * kleinere Geräte: Mix und Seiten in der letzten Spalte; zwei Reihen nur Fader
//!   * Geräte mit Drehreglern: Regler = Lautstärke, Tasten darüber = Kacheln
//!
//! Das Profil merkt sich in `lindeck-meta.json`, dass FreeWave es verwaltet (Kanalauswahl und Mix).

use std::path::{Path, PathBuf};
use std::sync::Arc;

use freestream_core::catalog;
use freestream_core::editdoc::{create_profile, init_profile, ProfileDoc, Slot};
use freestream_core::models::{connected_models, model_geometry, model_name, Connected, Geometry, MODELS};
use freestream_core::profiles::{Profile, ProfileStore};
use freestream_core::{paths, tr};
use freestream_wave::settings::FileSettings;
use freestream_wave::Mixer;
use serde_json::{json, Map, Value};

pub const MIXER_UUID: &str = "com.lindeck.mixer.channel";
pub const MIXSELECT_UUID: &str = "com.lindeck.mixer.mixselect";
pub const PAGES_UUID: &str = "com.lindeck.mixer.pages";
/// "selected": auf dem Deck wählbar
pub const MIXES: [&str; 4] = ["selected", "both", "monitor", "stream"];
pub const KEY_STEP: i64 = 5;
pub const DIAL_STEP: i64 = 2;
pub const META: &str = "lindeck-meta.json";
pub const PROFILE_NAME: &str = "FreeWave";
/// Belegung: 2 = Kanaltasten mit Platz-Nummer (Mix-Wahl zeigt nur die Kanäle des Mixes)
pub const LAYOUT: i64 = 2;
pub const DEFAULT_MIXES: [&str; 2] = ["monitor", "stream"];
/// Hintergrundbild, liegt neben „Profiles“ (überlebt das Neu-Belegen)
pub const BG_FILE: &str = "freewave-background.png";

/// Fehler beim Export (Text schon übersetzt).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExportError(pub String);

impl std::fmt::Display for ExportError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

type Result<T> = std::result::Result<T, ExportError>;

fn err(e: impl std::fmt::Display) -> ExportError {
    ExportError(e.to_string())
}

/// Hintergrundbild beim Export.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Background {
    /// wie beim letzten Export (auch ein in FreeDeck gesetztes)
    Keep,
    /// keins
    None,
    /// diese Bilddatei auf alle Seiten
    File(PathBuf),
}

// ------------------------------------------------------------------ Geräte
/// Modellcode ohne Varianten-Endung („20GAT9901-LL“ → „20GAT9901“).
pub fn base(model: &str) -> String {
    let b = model.split('-').next().unwrap_or("");
    b.strip_suffix("LL").unwrap_or(b).to_string()
}

fn known_model(m: &str) -> bool {
    MODELS.iter().any(|(c, ..)| *c == m)
}

/// Modelle der Profile, die gerade auf einem Gerät aktiv sind (state.json von FreeDeck).
fn in_use_models(store: &ProfileStore) -> Vec<String> {
    let data: Value = std::fs::read_to_string(paths::state_file()).ok()
        .and_then(|t| serde_json::from_str(&t).ok()).unwrap_or(Value::Null);
    let mut out = Vec::new();
    for d in data.as_object().into_iter().flat_map(|o| o.values()) {
        let Some(pid) = d.get("profile").and_then(Value::as_str) else { continue };
        if let Some(p) = store.get(pid) {
            let m = base(&p.model);
            if !p.model.is_empty() && !out.contains(&m) {
                out.push(m);
            }
        }
    }
    out
}

/// Ein Deck-Modell zur Auswahl im Dialog.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeviceChoice {
    pub model: String,
    pub name: String,
    pub connected: bool,
    pub in_use: bool,
    /// Name eines schon vorhandenen FreeWave-Profils für dieses Modell (sonst "")
    pub profile: String,
}

/// Modelle zur Auswahl: angeschlossene zuerst, dann die in FreeDeck benutzten, dann alle übrigen.
pub fn devices(store_root: Option<&Path>, connected: Option<Vec<Connected>>) -> Vec<DeviceChoice> {
    let store = ProfileStore::new(store_root);
    let mut plugged: Vec<String> = Vec::new();
    for d in connected.unwrap_or_else(connected_models) {
        let m = base(&d.model);
        if !plugged.contains(&m) {
            plugged.push(m);
        }
    }
    let used = in_use_models(&store);
    let known: Vec<String> = store.profiles.values().filter(|p| !p.model.is_empty()).map(|p| base(&p.model)).collect();
    let order = plugged.iter().chain(&used).chain(&known).cloned().chain(MODELS.iter().map(|(c, ..)| c.to_string()));
    let mut out: Vec<DeviceChoice> = Vec::new();
    for m in order {
        if !known_model(&m) || out.iter().any(|d| d.model == m) {
            continue;
        }
        let managed = managed_profile(&m, store_root);
        out.push(DeviceChoice {
            name: model_name(&m),
            connected: plugged.contains(&m),
            in_use: used.contains(&m),
            profile: managed.map(|p| p.name).unwrap_or_default(),
            model: m,
        });
    }
    out
}

/// Neu angeschlossenes Deck ohne FreeWave-Profil: Profil mit denselben Einstellungen wie beim letzten
/// Export anlegen – nur, wenn schon einmal exportiert wurde. Gibt neue Profilnamen zurück.
pub fn adopt_new_devices(store_root: Option<&Path>, connected: Option<Vec<Connected>>) -> Vec<String> {
    let managed = managed_profiles(store_root);
    if managed.is_empty() {
        return Vec::new();
    }
    let mtime = |p: &Profile| std::fs::metadata(p.root.join(META)).and_then(|m| m.modified()).ok();
    let Some(newest) = managed.iter().max_by_key(|p| mtime(p)) else { return Vec::new() };
    let lw = linewave_meta(&newest.root);
    let mut have: Vec<String> = managed.iter().map(|p| base(&p.model)).collect();
    let mut created = Vec::new();
    for d in connected.unwrap_or_else(connected_models) {
        let m = base(&d.model);
        if have.contains(&m) || !known_model(&m) {
            continue;
        }
        have.push(m.clone());
        let opts = ExportOptions {
            mix: lw.mix.clone(),
            all_channels: lw.all,
            store_root: store_root.map(Path::to_path_buf),
            ..Default::default()
        };
        // z. B. Pedal: keine Lautstärketasten möglich
        if let Ok((name, _, _)) = export(&m, &lw.channels, &opts) {
            created.push(name);
        }
    }
    created
}

// ------------------------------------------------------------------ Belegung
/// Art eines Platzes: Kanal-Arten "dial", "tile", "fader_top", "fader_bottom"; ohne Kanal "mix:<id>",
/// "mix:both", "mix:cycle" und "pages".
pub type Entry = (Slot, Option<String>, String);

fn channel_kind(kind: &str) -> &'static str {
    match kind {
        "fader_top" => "up",
        "fader_bottom" => "down",
        _ => "mute",
    }
}

/// Mix-Tasten für `room` Plätze: „Alle“ und jeder Mix; reicht der Platz nicht, zuletzt „nächster Mix“.
fn selectors(mixes: &[String], room: usize) -> Vec<String> {
    let mut keys: Vec<String> = std::iter::once("mix:both".to_string()).chain(mixes.iter().map(|m| format!("mix:{m}"))).collect();
    if keys.len() > room {
        keys.truncate(room.saturating_sub(1));
        keys.push("mix:cycle".into());
    }
    keys
}

/// Mix-ids aus der FreeWave-Konfiguration (Persönlicher Mix, Stream-Mix, eigene).
pub fn linewave_mixes() -> Vec<String> {
    let m = config_mixer();
    let ids = m.mix_ids();
    if ids.is_empty() {
        DEFAULT_MIXES.iter().map(|s| s.to_string()).collect()
    } else {
        ids
    }
}

/// Mixer nur aus config.json (ohne PipeWire-Abfragen beim Anlegen).
fn config_mixer() -> Mixer {
    let data = freestream_core::config::read_file().unwrap_or_default();
    let mut m = Map::new();
    if let Some(mx) = data.get("mixer") {
        m.insert("mixer".into(), mx.clone());
    }
    Mixer::new(Box::new(FileSettings::memory(m)), Arc::new(freestream_wave::pactl::SystemPactl), paths::state_dir())
}

/// Belegung je Seite – ohne Dateien (testbar). `selectable` false: feste Mischung, keine Mix-Wahl-Tasten.
pub fn plan(g: Geometry, channels: &[String], selectable: bool, mixes: &[String]) -> Result<Vec<Vec<Entry>>> {
    let (cols, rows, dials) = (g.cols, g.rows, g.dials);
    if channels.is_empty() {
        return Err(err(tr!("Choose at least one channel.")));
    }
    if rows < 2 && dials == 0 {
        return Err(err(tr!("This device has too few keys for volume controls.")));
    }
    let mut extra: Vec<(Slot, String)> = Vec::new();
    let per_page: usize;
    let kinds: &[&str];
    if dials > 0 {
        per_page = dials as usize;
        let bottom = rows.saturating_sub(1);
        if rows >= 2 {
            if selectable && cols >= 4 {
                extra = selectors(mixes, cols as usize - 1).into_iter().enumerate().map(|(i, k)| (Slot::Key(i as u8, bottom), k)).collect();
            } else if selectable {
                extra.push((Slot::Key(0, bottom), "mix:cycle".into()));
            }
            extra.push((Slot::Key(cols - 1, bottom), "pages".into()));
        }
        kinds = &[];
    } else if rows >= 4 && cols >= 4 {
        per_page = cols as usize;
        if selectable {
            extra = selectors(mixes, cols as usize - 1).into_iter().enumerate().map(|(i, k)| (Slot::Key(i as u8, 0), k)).collect();
        }
        extra.push((Slot::Key(cols - 1, 0), "pages".into()));
        kinds = &["tile", "fader_top", "fader_bottom"];
    } else {
        per_page = (cols as usize).saturating_sub(1); // letzte Spalte: Mix und Seiten
        kinds = if rows >= 3 { &["tile", "fader_top", "fader_bottom"] } else { &["fader_top", "fader_bottom"] };
        if selectable {
            extra.push((Slot::Key(cols - 1, 0), "mix:cycle".into()));
        }
        extra.push((Slot::Key(cols - 1, if selectable { 1 } else { 0 }), "pages".into()));
    }
    if per_page < 1 {
        return Err(err(tr!("This device has too few keys for volume controls.")));
    }
    let top = if dials == 0 && rows >= 4 && cols >= 4 { 1 } else { 0 };
    Ok(channels.chunks(per_page).map(|chs| {
        let mut out: Vec<Entry> = Vec::new();
        if dials > 0 {
            out.extend(chs.iter().enumerate().map(|(i, c)| (Slot::Dial(i as u8), Some(c.clone()), "dial".to_string())));
            out.extend(chs.iter().enumerate().filter(|(i, _)| *i < cols as usize && rows >= 2)
                .map(|(i, c)| (Slot::Key(i as u8, 0), Some(c.clone()), "tile".to_string())));
        } else {
            for (i, c) in chs.iter().enumerate() {
                for (r, k) in kinds.iter().enumerate() {
                    out.push((Slot::Key(i as u8, (r + top) as u8), Some(c.clone()), k.to_string()));
                }
            }
        }
        out.extend(extra.iter().map(|(s, k)| (*s, None, k.clone())));
        out
    }).collect())
}

/// Kurzer Satz, wie das Profil aufgebaut wird.
pub fn describe(g: Geometry) -> String {
    if g.dials > 0 {
        tr!("Dials: turn to change the volume, press to mute. The keys above show each channel and mute it.")
    } else if g.rows >= 3 {
        tr!("Per channel a tile (press to mute) and a fader over two keys: press the upper key for louder, the lower one for quieter.")
    } else if g.rows == 2 {
        tr!("Per channel a fader over two keys: press the upper key for louder, the lower one for quieter.")
    } else {
        tr!("This device has too few keys for volume controls.")
    }
}

pub fn geometry_of(model: &str) -> Result<Geometry> {
    model_geometry(model).ok_or_else(|| err(tr!("Unknown device.")))
}

// ------------------------------------------------------------------ Profil
fn read_meta(root: &Path) -> Map<String, Value> {
    std::fs::read_to_string(root.join(META)).ok().and_then(|t| serde_json::from_str::<Value>(&t).ok())
        .and_then(|v| v.as_object().cloned()).unwrap_or_default()
}

/// Abschnitt „linewave“ der Metadaten.
#[derive(Debug, Clone, PartialEq)]
struct LinewaveMeta {
    channels: Vec<String>,
    all: bool,
    mix: String,
    mixes: Option<Vec<String>>,
    layout: Option<i64>,
}

fn linewave_meta(root: &Path) -> LinewaveMeta {
    let meta = read_meta(root);
    let lw = meta.get("linewave").and_then(Value::as_object).cloned().unwrap_or_default();
    let strs = |v: Option<&Value>| -> Option<Vec<String>> {
        v.and_then(Value::as_array).map(|a| a.iter().filter_map(|x| x.as_str().map(str::to_string)).collect())
    };
    LinewaveMeta {
        channels: strs(lw.get("channels")).unwrap_or_default(),
        all: lw.get("all").and_then(Value::as_bool).unwrap_or(true),
        mix: lw.get("mix").and_then(Value::as_str).unwrap_or("selected").to_string(),
        mixes: strs(lw.get("mixes")),
        layout: lw.get("layout").and_then(Value::as_i64),
    }
}

fn is_managed(root: &Path) -> bool {
    read_meta(root).get("linewave").is_some_and(|v| !v.is_null())
}

/// Das von FreeWave verwaltete Profil für dieses Modell oder None.
pub fn managed_profile(model: &str, store_root: Option<&Path>) -> Option<Profile> {
    ProfileStore::new(store_root).profiles.into_values().find(|p| base(&p.model) == base(model) && is_managed(&p.root))
}

pub fn managed_profiles(store_root: Option<&Path>) -> Vec<Profile> {
    ProfileStore::new(store_root).profiles.into_values().filter(|p| is_managed(&p.root)).collect()
}

/// fn(Kanal, Mix) -> bool aus der FreeWave-Konfiguration ("both": in irgendeinem Mix).
fn membership() -> impl Fn(&str, &str) -> bool {
    let m = config_mixer();
    let mix_of: std::collections::HashMap<String, Vec<String>> =
        m.all_channels().into_iter().map(|c| (c.id.clone(), m.channel_mixes(&c.id))).collect();
    move |cid: &str, mix: &str| match mix_of.get(cid) {
        None => true, // unbekannter Kanal: lieber zeigen
        Some(mixes) if mix == "both" => !mixes.is_empty(),
        Some(mixes) => mixes.iter().any(|m| m == mix),
    }
}

/// Profil `root` neu belegen (alte Seiten werden ersetzt). Gibt die Seitenzahl zurück.
fn fill(root: &Path, channels: &[String], mix: &str, mixes: &[String]) -> Result<usize> {
    let mut doc = ProfileDoc::open(root).map_err(err)?;
    let all_channels: Vec<String> = channels.to_vec();
    let mut channels: Vec<String> = channels.to_vec();
    if mix != "selected" {
        let member = membership();
        let only: Vec<String> = channels.iter().filter(|c| member(c, mix)).cloned().collect();
        if !only.is_empty() {
            channels = only;
        }
    }
    let layout = plan(doc.geometry(), &channels, mix == "selected", mixes)?;
    let first = doc.page_ids().first().cloned().ok_or_else(|| err("no page"))?;
    let mut pages = vec![first];
    for _ in 1..layout.len() {
        let last = pages.last().cloned();
        pages.push(doc.add_page(last.as_deref()));
    }
    let idx = |c: &str| channels.iter().position(|x| x == c).unwrap_or(0);
    for (n, (page, entries)) in pages.iter().zip(&layout).enumerate() {
        if layout.len() > 1 {
            doc.rename_page(page, &format!("{PROFILE_NAME} {}", n + 1));
        }
        let on_page: Vec<usize> = entries.iter().filter_map(|(_, c, _)| c.as_deref().map(|c| idx(c) + 1)).collect();
        for (slot, cid, kind) in entries {
            let mut data = if let Some(m) = kind.strip_prefix("mix:") {
                let mut d = catalog::new_action(MIXSELECT_UUID);
                d.insert("Settings".into(), json!({"mix": m}));
                d
            } else if kind == "pages" {
                let mut d = catalog::new_action(PAGES_UUID);
                d.insert("Settings".into(), json!({"first": on_page.iter().min().copied().unwrap_or(1),
                                                     "last": on_page.iter().max().copied().unwrap_or(1)}));
                d
            } else {
                catalog::new_action(MIXER_UUID)
            };
            if let Some(cid) = cid {
                let st = data.entry("Settings").or_insert_with(|| json!({}));
                if !st.is_object() {
                    *st = json!({});
                }
                let st = st.as_object_mut().unwrap();
                st.insert("channel".into(), json!(cid));
                st.insert("mix".into(), json!(mix));
                st.insert("action".into(), json!(channel_kind(kind)));
                st.insert("step".into(), json!(if kind == "dial" { DIAL_STEP } else { KEY_STEP }));
                if mix == "selected" {
                    st.insert("slot".into(), json!(idx(cid)));
                    st.insert("channels".into(), json!(all_channels));
                }
                st.remove("volume");
                if kind != "dial" {
                    st.insert("display".into(), json!(kind));
                }
            }
            doc.place_action(page, *slot, std::mem::take(&mut data));
        }
    }
    doc.save().map_err(err)?;
    Ok(layout.len())
}

/// Aussehen des FreeWave-Profils dieses Modells.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Look {
    pub opacity: i64,
    pub background: Option<PathBuf>,
    pub dim: i64,
}

pub fn look(model: &str, store_root: Option<&Path>) -> Look {
    let p = managed_profile(&base(model), store_root);
    let lk = p.as_ref().and_then(|p| read_meta(&p.root).get("look").and_then(Value::as_object).cloned()).unwrap_or_default();
    let bg = p.as_ref().filter(|_| lk.get("background").is_some_and(|v| !v.is_null())).map(|p| p.root.join(BG_FILE));
    Look {
        opacity: lk.get("opacity").and_then(Value::as_i64).unwrap_or(100),
        background: bg.filter(|b| b.exists()),
        dim: lk.get("dim").and_then(Value::as_i64).unwrap_or(30),
    }
}

/// Hintergrund, den der Nutzer in FreeDeck auf die erste Seite gelegt hat, vor dem Neu-Belegen sichern.
fn keep_background(root: &Path, meta: &mut Map<String, Value>) {
    let lk = meta.entry("look").or_insert_with(|| json!({}));
    if !lk.is_object() {
        *lk = json!({});
    }
    let lk = lk.as_object_mut().unwrap();
    if lk.get("background").is_some_and(|v| !v.is_null()) {
        return;
    }
    let Ok(doc) = ProfileDoc::open(root) else { return };
    let Some(first) = doc.page_ids().first().cloned() else { return };
    if let (Some(path), dim) = doc.background(&first) {
        if std::fs::copy(&path, root.join(BG_FILE)).is_ok() {
            lk.insert("background".into(), json!(BG_FILE));
            lk.insert("dim".into(), json!(dim));
        }
    }
}

/// Deckkraft der Tasten und Hintergrundbild auf alle Seiten des frisch belegten Profils legen.
fn apply_look(root: &Path, lk: &Map<String, Value>) -> Result<()> {
    let opacity = lk.get("opacity").and_then(Value::as_i64).unwrap_or(100).clamp(0, 100);
    let bg = lk.get("background").is_some_and(|v| !v.is_null()).then(|| root.join(BG_FILE)).filter(|b| b.exists());
    if opacity >= 100 && bg.is_none() {
        return Ok(());
    }
    let mut doc = ProfileDoc::open(root).map_err(err)?;
    for page in doc.page_ids() {
        if let Some(bg) = &bg {
            doc.set_background(&page, Some(bg), Some(lk.get("dim").and_then(Value::as_i64).unwrap_or(30)), false).map_err(err)?;
        }
        if opacity < 100 {
            let slots: Vec<(Slot, usize)> = doc.page(&page).map(|pg| pg.items().into_iter()
                .map(|(s, a)| (s, a.get("States").and_then(Value::as_array).map(Vec::len).unwrap_or(0).max(1))).collect())
                .unwrap_or_default();
            for (slot, n) in slots {
                for i in 0..n {
                    doc.update_state(&page, slot, &[], i, "IconOpacity", json!(opacity));
                }
            }
        }
    }
    doc.save().map_err(err)?;
    Ok(())
}

/// Einstellungen für [`export`].
#[derive(Debug, Clone)]
pub struct ExportOptions {
    /// "selected", "both" oder eine Mix-id
    pub mix: String,
    /// später neue Kanäle automatisch dazunehmen
    pub all_channels: bool,
    pub store_root: Option<PathBuf>,
    /// Mix-ids für die Mix-Tasten (None: aus der FreeWave-Konfiguration)
    pub mixes: Option<Vec<String>>,
    /// Deckkraft der Tasten in % (0 = durchsichtig; None = wie zuletzt)
    pub opacity: Option<i64>,
    pub background: Background,
    pub dim: Option<i64>,
}

impl Default for ExportOptions {
    fn default() -> Self {
        Self { mix: "selected".into(), all_channels: true, store_root: None, mixes: None, opacity: None,
               background: Background::Keep, dim: None }
    }
}

/// FreeWave-Profil für dieses Modell anlegen oder neu belegen. Gibt (Profilname, Seitenzahl, neu angelegt)
/// zurück.
pub fn export(model: &str, channels: &[String], o: &ExportOptions) -> Result<(String, usize, bool)> {
    let mixes = o.mixes.clone().unwrap_or_else(linewave_mixes);
    if !MIXES.contains(&o.mix.as_str()) && !mixes.contains(&o.mix) {
        return Err(err(&o.mix));
    }
    let model = base(model);
    plan(geometry_of(&model)?, channels, true, &mixes)?; // vorher prüfen, damit kein halbes Profil entsteht
    // Bild ebenfalls vorher lesen: ist es kaputt, bleibt das bisherige Profil unangetastet
    let bg_img = match &o.background {
        Background::File(f) => {
            let im = image::open(f).map_err(|e| err(tr!("Image not readable: {error}", error = e)))?;
            Some(im.thumbnail(1600, 1600).to_rgb8())
        }
        _ => None,
    };
    let store_root = o.store_root.clone().unwrap_or_else(paths::profiles_dir);
    let (root, name, created, mut meta) = match managed_profile(&model, Some(&store_root)) {
        Some(p) => {
            let mut meta = read_meta(&p.root);
            if o.background == Background::Keep {
                keep_background(&p.root, &mut meta);
            }
            let _ = std::fs::remove_dir_all(p.root.join("Profiles"));
            let m = if p.model.is_empty() { model.clone() } else { p.model.clone() };
            init_profile(&p.root, &p.name, &m).map_err(err)?;
            (p.root, p.name, false, meta)
        }
        None => {
            let taken: Vec<String> = ProfileStore::new(Some(&store_root)).profiles.values().map(|p| p.name.clone()).collect();
            let name = if taken.iter().any(|n| n == PROFILE_NAME) {
                format!("{PROFILE_NAME} ({})", model_name(&model))
            } else {
                PROFILE_NAME.to_string()
            };
            let root = create_profile(&name, &model, Some(&store_root)).map_err(err)?;
            (root, name, true, Map::new())
        }
    };
    let lk = meta.entry("look").or_insert_with(|| json!({}));
    if !lk.is_object() {
        *lk = json!({});
    }
    let lk = lk.as_object_mut().unwrap();
    if let Some(op) = o.opacity {
        lk.insert("opacity".into(), json!(op.clamp(0, 100)));
    }
    if let Some(d) = o.dim {
        lk.insert("dim".into(), json!(d));
    }
    match (&o.background, bg_img) {
        (Background::None, _) => {
            lk.insert("background".into(), Value::Null);
            let _ = std::fs::remove_file(root.join(BG_FILE));
        }
        (_, Some(img)) => {
            img.save(root.join(BG_FILE)).map_err(|e| err(tr!("Image not readable: {error}", error = e)))?;
            lk.insert("background".into(), json!(BG_FILE));
        }
        _ => {}
    }
    let lk = lk.clone();
    let pages = fill(&root, channels, &o.mix, &mixes)?;
    apply_look(&root, &lk)?;
    meta.insert("linewave".into(), json!({"channels": channels, "all": o.all_channels, "mix": o.mix, "mixes": mixes,
                                          "layout": LAYOUT}));
    std::fs::write(root.join(META), serde_json::to_string_pretty(&Value::Object(meta)).unwrap_or_default()).map_err(err)?;
    touch(&root);
    notify_freedeck();
    Ok((name, pages, created))
}

fn touch(p: &Path) {
    if let Ok(f) = std::fs::File::open(p) {
        let _ = f.set_modified(std::time::SystemTime::now());
    }
}

/// Laufendem FreeDeck Bescheid geben (SIGHUP): es lädt die Profile neu. True, wenn FreeDeck lief.
pub fn notify_freedeck() -> bool {
    match freestream_deck::daemon::already_running() {
        // SAFETY: kill mit gültiger Pid und Signal
        Some(pid) => unsafe { libc::kill(pid as libc::pid_t, libc::SIGHUP) == 0 },
        None => false,
    }
}

/// FreeWave-Profile an die aktuellen Kanäle und Mixe anpassen (neue dazu, gelöschte weg). `force`: neu
/// belegen, auch wenn sich nur geändert hat, welcher Kanal in welchem Mix ist. Gibt geänderte Profile zurück.
pub fn sync(channels: &[String], store_root: Option<&Path>, mixes: Option<Vec<String>>, force: bool) -> Vec<String> {
    let mixes = mixes.unwrap_or_else(linewave_mixes);
    let mut changed = Vec::new();
    for p in managed_profiles(store_root) {
        let lw = linewave_meta(&p.root);
        let want: Vec<String> = if lw.all {
            channels.to_vec()
        } else {
            lw.channels.iter().filter(|c| channels.contains(c)).cloned().collect()
        };
        let old_mixes = lw.mixes.clone().unwrap_or_else(|| DEFAULT_MIXES.iter().map(|s| s.to_string()).collect());
        let mut mix = lw.mix.clone();
        if !MIXES.contains(&mix.as_str()) && !mixes.contains(&mix) {
            mix = "selected".into(); // fester Mix wurde gelöscht
        }
        let outdated = lw.layout != Some(LAYOUT); // mit älterer Version exportiert: neu belegen
        if want.is_empty() || (!force && !outdated && want == lw.channels && old_mixes == mixes && mix == lw.mix) {
            continue;
        }
        let opts = ExportOptions { mix, all_channels: lw.all, store_root: store_root.map(Path::to_path_buf),
                                   mixes: Some(mixes.clone()), ..Default::default() };
        if export(&p.model, &want, &opts).is_ok() {
            changed.push(p.name.clone());
        }
    }
    changed
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(v: &[&str]) -> Vec<String> {
        v.iter().map(|x| x.to_string()).collect()
    }

    #[test]
    fn plan_xl_has_selectors_tiles_and_faders() {
        let g = model_geometry("20GAT9901").unwrap();
        let pages = plan(g, &s(&["mic", "music", "game"]), true, &s(&["monitor", "stream"])).unwrap();
        assert_eq!(pages.len(), 1);
        let p = &pages[0];
        assert!(p.contains(&(Slot::Key(0, 1), Some("mic".into()), "tile".into())));
        assert!(p.contains(&(Slot::Key(1, 2), Some("music".into()), "fader_top".into())));
        assert!(p.contains(&(Slot::Key(2, 3), Some("game".into()), "fader_bottom".into())));
        assert!(p.contains(&(Slot::Key(0, 0), None, "mix:both".into())));
        assert!(p.contains(&(Slot::Key(2, 0), None, "mix:stream".into())));
        assert!(p.contains(&(Slot::Key(7, 0), None, "pages".into())));
    }

    #[test]
    fn plan_pages_and_small_devices() {
        let chans = s(&["a", "b", "c", "d", "e", "f", "g", "h", "i"]);
        let xl = model_geometry("20GAT9901").unwrap();
        assert_eq!(plan(xl, &chans, true, &s(&["monitor", "stream"])).unwrap().len(), 2);
        let mk2 = model_geometry("20GBA9901").unwrap();
        let p = plan(mk2, &chans[..4], true, &s(&["monitor"])).unwrap();
        assert_eq!(p.len(), 1);
        assert!(p[0].contains(&(Slot::Key(4, 0), None, "mix:cycle".into())));
        assert!(p[0].contains(&(Slot::Key(4, 1), None, "pages".into())));
        let fixed = plan(mk2, &chans[..1], false, &[]).unwrap();
        assert!(fixed[0].contains(&(Slot::Key(4, 0), None, "pages".into())));
        let mini = model_geometry("20GAI9901").unwrap();
        let p = plan(mini, &chans[..2], true, &[]).unwrap();
        assert!(p[0].contains(&(Slot::Key(0, 0), Some("a".into()), "fader_top".into())));
        let pedal = model_geometry("20GBF9901").unwrap();
        assert!(plan(pedal, &chans, true, &[]).is_err());
        assert!(plan(mk2, &[], true, &[]).is_err());
    }

    #[test]
    fn plan_dials() {
        let plus = model_geometry("20GBD9901").unwrap();
        let p = plan(plus, &s(&["a", "b", "c", "d", "e"]), true, &s(&["monitor", "stream", "chat"])).unwrap();
        assert_eq!(p.len(), 2);
        assert!(p[0].contains(&(Slot::Dial(3), Some("d".into()), "dial".into())));
        assert!(p[0].contains(&(Slot::Key(0, 0), Some("a".into()), "tile".into())));
        // 3 Plätze für 4 Mix-Tasten: zuletzt „nächster Mix“
        assert!(p[0].contains(&(Slot::Key(2, 1), None, "mix:cycle".into())));
        assert!(p[0].contains(&(Slot::Key(3, 1), None, "pages".into())));
    }

    #[test]
    fn base_strips_variants() {
        assert_eq!(base("20GAT9901-LL"), "20GAT9901");
        assert_eq!(base("20GBA9901LL"), "20GBA9901");
    }

    #[test]
    fn export_writes_profile_and_sync_updates_it() {
        let dir = tempfile::tempdir().unwrap();
        let opts = ExportOptions { store_root: Some(dir.path().to_path_buf()), mixes: Some(s(&["monitor", "stream"])),
                                   ..Default::default() };
        let (name, pages, created) = export("20GBA9901", &s(&["mic", "music"]), &opts).unwrap();
        assert_eq!((name.as_str(), pages, created), ("FreeWave", 1, true));
        let p = managed_profile("20GBA9901", Some(dir.path())).unwrap();
        let doc = ProfileDoc::open(&p.root).unwrap();
        let page = doc.page_ids()[0].clone();
        let a = doc.get(&page, Slot::Key(1, 1)).unwrap();
        assert_eq!(a["UUID"], MIXER_UUID);
        assert_eq!(a["Settings"]["channel"], "music");
        assert_eq!(a["Settings"]["action"], "up");
        assert_eq!(a["Settings"]["slot"], 1);
        assert_eq!(doc.get(&page, Slot::Key(4, 0)).unwrap()["Settings"]["mix"], "cycle");
        assert_eq!(linewave_meta(&p.root).channels, s(&["mic", "music"]));
        // neuer Kanal: sync nimmt ihn dazu, ohne Änderung passiert nichts
        let changed = sync(&s(&["mic", "music", "game"]), Some(dir.path()), Some(s(&["monitor", "stream"])), false);
        assert_eq!(changed, vec!["FreeWave".to_string()]);
        assert!(sync(&s(&["mic", "music", "game"]), Some(dir.path()), Some(s(&["monitor", "stream"])), false).is_empty());
        // zweiter Export ersetzt, legt kein neues Profil an
        let (_, _, created) = export("20GBA9901", &s(&["mic"]), &opts).unwrap();
        assert!(!created);
        assert_eq!(managed_profiles(Some(dir.path())).len(), 1);
        // Gerät ohne Profil angeschlossen: übernimmt die Einstellungen
        let conn = vec![Connected { model: "20GAT9901".into(), name: "XL".into(), serial: "X".into() }];
        assert_eq!(adopt_new_devices(Some(dir.path()), Some(conn.clone())), vec![format!("FreeWave ({})", model_name("20GAT9901"))]);
        let devs = devices(Some(dir.path()), Some(conn));
        assert_eq!(devs[0].model, "20GAT9901");
        assert!(devs[0].connected);
        assert_eq!(devs[0].profile, format!("FreeWave ({})", model_name("20GAT9901")));
    }

    #[test]
    fn export_rejects_unknown_mix_and_device() {
        let dir = tempfile::tempdir().unwrap();
        let opts = ExportOptions { store_root: Some(dir.path().to_path_buf()), mixes: Some(s(&["monitor"])),
                                   mix: "nope".into(), ..Default::default() };
        assert!(export("20GBA9901", &s(&["mic"]), &opts).is_err());
        assert!(geometry_of("XYZ").is_err());
        assert!(managed_profiles(Some(dir.path())).is_empty());
    }
}
