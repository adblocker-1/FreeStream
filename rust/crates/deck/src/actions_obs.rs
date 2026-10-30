//! OBS-Aktionen (`OBSAction` in `freestream/native.py`): Szene, Aufnahme, Stream, Quelle ein/aus,
//! Audio-Eingang (Taste: stumm, Drehregler: Lautstärke), Replay-Puffer, virtuelle Kamera, Studio-Modus,
//! Übergang und Screenshot – für die eigenen UUIDs `com.lindeck.obs.*` und die der OBS-Plugins aus
//! importierten Profilen (z. B. `com.elgato.obsstudio.record`).
//!
//! Die Tasten zeigen den Zustand live (zwei Zustände: 0 = aus, 1 = an). Anfragen an OBS laufen im
//! Arbeits-Thread; das Zeichnen liest nur den Zustand des Clients.

use freestream_core::{pgettext, tr};
use image::RgbaImage;
use serde_json::json;
use std::sync::Arc;
use std::time::Duration;

use crate::actions::{ActionData, Behavior, Command, Env};
use crate::native::{self, Worker};
use crate::obs::{self, Client};

static WORKER: Worker = Worker::new("obs-jobs");

/// Art der OBS-Aktion.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Scene,
    Record,
    Stream,
    Source,
    Audio,
    Replay,
    VirtualCam,
    StudioMode,
    Transition,
    Screenshot,
}

/// Teil der UUID -> Art (Reihenfolge wichtig: „virtualcam“ vor „cam“, „sceneitem“ vor „scene“).
const KINDS: &[(&str, Kind)] = &[
    ("screenshot", Kind::Screenshot), ("virtualcam", Kind::VirtualCam), ("virtual", Kind::VirtualCam),
    ("replay", Kind::Replay), ("studio", Kind::StudioMode), ("transition", Kind::Transition),
    ("record", Kind::Record), ("stream", Kind::Stream), ("mixer", Kind::Audio), ("audio", Kind::Audio),
    ("mute", Kind::Audio), ("volume", Kind::Audio), ("input", Kind::Audio), ("source", Kind::Source),
    ("sceneitem", Kind::Source), ("item", Kind::Source), ("scene", Kind::Scene),
];

/// Ist das eine OBS-Aktion (eigene oder aus einem OBS-Plugin), und welche?
pub fn kind_for(uuid: &str) -> Option<Kind> {
    let u = uuid.to_lowercase();
    let (plugin, last) = u.rsplit_once('.').unwrap_or(("", u.as_str()));
    let is_obs = u.starts_with("com.lindeck.obs.") || plugin.split('.').any(|p| p == "obs") || plugin.contains("obsstudio");
    if !is_obs {
        return None;
    }
    KINDS.iter().find(|(k, _)| last.contains(k)).map(|(_, kind)| *kind)
}

pub struct ObsAction {
    kind: Kind,
    uuid: String,
    snap: Option<(u64, Option<bool>, String, String)>,
}

impl ObsAction {
    pub fn new(kind: Kind, uuid: &str) -> Self {
        Self { kind, uuid: uuid.to_lowercase(), snap: None }
    }

    fn scene(a: &ActionData) -> String {
        native::first(&a.settings(), &["scene", "sceneName", "scene_name", "sceneId", "SceneName"])
            .unwrap_or_else(|| native::first_title(a))
    }

    fn source(a: &ActionData) -> String {
        native::first(&a.settings(), &["source", "sourceName", "source_name", "sceneItem", "item", "input", "inputName"])
            .unwrap_or_else(|| native::first_title(a))
    }

    fn mode(a: &ActionData) -> String {
        native::first(&a.settings(), &["mode", "action"]).unwrap_or_else(|| "toggle".into()).to_lowercase()
    }

    fn active(&self, c: &Arc<Client>, a: &ActionData) -> Option<bool> {
        if !c.connected() {
            return None;
        }
        let s = c.state();
        match self.kind {
            Kind::Scene => {
                let sc = Self::scene(a);
                Some(s.scene == sc || (s.studio && s.preview == sc))
            }
            Kind::Record => Some(s.recording),
            Kind::Stream => Some(s.streaming),
            Kind::Replay => Some(s.replay),
            Kind::VirtualCam => Some(s.virtualcam),
            Kind::StudioMode => Some(s.studio),
            Kind::Audio => c.is_muted(&Self::source(a)).map(|m| !m),
            Kind::Source => c.item_visible(&a.setting_str("scene"), &Self::source(a)),
            Kind::Transition | Kind::Screenshot => None,
        }
    }

    fn status(&self, c: &Arc<Client>, a: &ActionData) -> String {
        if !c.connected() {
            return "OBS offline".into();
        }
        let s = c.state();
        let on_off = |on: bool| if on { pgettext("state", "on") } else { pgettext("state", "off") };
        match self.kind {
            Kind::Record => {
                if s.rec_paused {
                    pgettext("state", "paused")
                } else if s.recording {
                    pgettext("state", "running")
                } else {
                    pgettext("state", "off")
                }
            }
            Kind::Stream => if s.streaming { "LIVE".into() } else { pgettext("state", "off") },
            Kind::Audio => {
                let src = Self::source(a);
                if s.muted.get(&src) == Some(&true) {
                    pgettext("state", "muted")
                } else {
                    s.volume_db.get(&src).map(|db| format!("{db:.0} dB")).unwrap_or_default()
                }
            }
            Kind::Replay | Kind::VirtualCam | Kind::StudioMode | Kind::Source => {
                self.active(c, a).map(on_off).unwrap_or_default()
            }
            Kind::Scene | Kind::Transition | Kind::Screenshot => String::new(),
        }
    }

    fn main_text(&self, a: &ActionData) -> String {
        let or = |s: String, d: String| if s.is_empty() { d } else { s };
        match self.kind {
            Kind::Scene => or(Self::scene(a), tr!("Scene")),
            Kind::Record => "REC".into(),
            Kind::Stream => "Stream".into(),
            Kind::Source => or(Self::source(a), tr!("Source")),
            Kind::Audio => or(Self::source(a), "Audio".into()),
            Kind::Replay => "Replay".into(),
            Kind::VirtualCam => "VirtCam".into(),
            Kind::StudioMode => "Studio".into(),
            Kind::Screenshot => tr!("Snapshot"),
            Kind::Transition => tr!("Transition"),
        }
    }

    /// Tastendruck ausführen (im Arbeits-Thread).
    fn execute(kind: Kind, uuid: &str, a: &ActionData, c: &Client) -> Result<Command, String> {
        if !c.wait_connected(Duration::from_secs(2)) {
            return Err(tr!("OBS not connected – enable the WebSocket server in OBS"));
        }
        let t = Duration::from_secs(3);
        let mode = Self::mode(a);
        let simple = |r: &str| c.request(r, None, t).map(|_| ());
        match kind {
            Kind::Scene => c.set_scene(&Self::scene(a))?,
            Kind::Record | Kind::Stream => {
                let base = if kind == Kind::Record { "Record" } else { "Stream" };
                match mode.as_str() {
                    "start" => simple(&format!("Start{base}"))?,
                    "stop" => simple(&format!("Stop{base}"))?,
                    "pause" if kind == Kind::Record => simple("ToggleRecordPause")?,
                    _ => simple(&format!("Toggle{base}"))?,
                }
            }
            Kind::Replay => simple(if mode == "save" || uuid.contains("save") { "SaveReplayBuffer" } else { "ToggleReplayBuffer" })?,
            Kind::VirtualCam => simple("ToggleVirtualCam")?,
            Kind::StudioMode => {
                let on = c.state().studio;
                c.request("SetStudioModeEnabled", Some(json!({"studioModeEnabled": !on})), t)?;
            }
            Kind::Transition => simple("TriggerStudioModeTransition")?,
            Kind::Source => c.toggle_source(&a.setting_str("scene"), &Self::source(a))?,
            Kind::Audio => {
                c.request("ToggleInputMute", Some(json!({"inputName": Self::source(a)})), t)?;
            }
            Kind::Screenshot => {
                let folder = pictures_dir().join("OBS");
                std::fs::create_dir_all(&folder).map_err(|e| e.to_string())?;
                let src = if a.setting_str("source").is_empty() { c.state().scene } else { Self::source(a) };
                let name = jiff::Zoned::now().strftime("OBS %Y-%m-%d %H-%M-%S.png").to_string();
                c.request("SaveSourceScreenshot", Some(json!({"sourceName": src, "imageFormat": "png",
                    "imageFilePath": folder.join(name).to_string_lossy()})), Duration::from_secs(10))?;
                return Ok(Command::Ok);
            }
        }
        Ok(Command::Redraw)
    }
}

/// Bilder-Ordner des Benutzers (xdg-user-dir PICTURES, sonst ~/Pictures).
fn pictures_dir() -> std::path::PathBuf {
    let home = freestream_core::paths::home();
    let out = crate::system::run_out(&["xdg-user-dir", "PICTURES"]);
    let out = out.trim();
    if !out.is_empty() && std::path::Path::new(out) != home {
        return out.into();
    }
    home.join("Pictures")
}

impl Behavior for ObsAction {
    fn key_down(&mut self, a: &ActionData, env: &Env) -> Command {
        let c = obs::client(env.cfg);
        let (kind, uuid, data) = (self.kind, self.uuid.clone(), a.clone());
        WORKER.run(&env.outbox, a.name(), move || Self::execute(kind, &uuid, &data, &c));
        Command::None
    }

    fn dial_turn(&mut self, a: &ActionData, env: &Env, ticks: i8) -> Command {
        let c = obs::client(env.cfg);
        let data = a.clone();
        match self.kind {
            Kind::Audio => {
                let step = a.settings().get("step_db").and_then(|v| v.as_f64().or_else(|| v.as_str()?.parse().ok()))
                    .filter(|s| *s != 0.0).unwrap_or(1.0);
                WORKER.run(&env.outbox, a.name(), move || {
                    c.change_volume_db(&Self::source(&data), step * ticks as f64).map(|_| Command::Redraw)
                });
            }
            Kind::Scene => WORKER.run(&env.outbox, a.name(), move || {
                let scenes = c.scenes()?;
                if !scenes.is_empty() {
                    let cur = c.state().scene;
                    let i = match scenes.iter().position(|s| *s == cur) {
                        Some(i) => (i as i64 + if ticks > 0 { 1 } else { -1 }).rem_euclid(scenes.len() as i64) as usize,
                        None => 0,
                    };
                    c.set_scene(&scenes[i])?;
                }
                Ok(Command::Redraw)
            }),
            _ => {}
        }
        Command::None
    }

    fn image(&self, a: &ActionData, env: &Env, size: (u32, u32)) -> Option<RgbaImage> {
        let c = obs::client(env.cfg); // Verbindung aufbauen (im Hintergrund)
        let active = self.active(&c, a);
        native::text_key(a, size, &self.main_text(a), &self.status(&c, a), active)
    }

    fn needs_tick(&self) -> bool {
        true
    }

    fn tick(&mut self, a: &ActionData) -> bool {
        let Some(c) = obs::current() else { return false };
        let snap = Some((c.version(), self.active(&c, a), self.status(&c, a), self.main_text(a)));
        let changed = snap != self.snap;
        self.snap = snap;
        changed
    }

    fn live_state(&self, a: &ActionData) -> Option<usize> {
        let c = obs::current()?;
        native::state_of(a, self.active(&c, a))
    }
}

pub fn behavior_for(uuid: &str) -> Option<Box<dyn Behavior>> {
    kind_for(uuid).map(|k| Box::new(ObsAction::new(k, uuid)) as Box<dyn Behavior>)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Map, Value};
    use std::path::PathBuf;

    fn settings_of(v: Value) -> Map<String, Value> {
        v.as_object().cloned().unwrap_or_default()
    }

    #[test]
    fn kinds_from_uuid() {
        assert_eq!(kind_for("com.lindeck.obs.scene"), Some(Kind::Scene));
        assert_eq!(kind_for("com.lindeck.obs.record"), Some(Kind::Record));
        assert_eq!(kind_for("com.lindeck.obs.stream"), Some(Kind::Stream));
        assert_eq!(kind_for("com.lindeck.obs.source"), Some(Kind::Source));
        assert_eq!(kind_for("com.lindeck.obs.audio"), Some(Kind::Audio));
        assert_eq!(kind_for("com.lindeck.obs.replay"), Some(Kind::Replay));
        assert_eq!(kind_for("com.lindeck.obs.virtualcam"), Some(Kind::VirtualCam));
        assert_eq!(kind_for("com.lindeck.obs.studiomode"), Some(Kind::StudioMode));
        assert_eq!(kind_for("com.lindeck.obs.transition"), Some(Kind::Transition));
        assert_eq!(kind_for("com.lindeck.obs.screenshot"), Some(Kind::Screenshot));
        // Plugins aus importierten Profilen
        assert_eq!(kind_for("com.elgato.obsstudio.record"), Some(Kind::Record));
        assert_eq!(kind_for("com.elgato.obsstudio.sceneitem"), Some(Kind::Source));
        assert_eq!(kind_for("com.elgato.obsstudio.mixeraudio"), Some(Kind::Audio));
        assert_eq!(kind_for("com.example.obs.scene"), Some(Kind::Scene));
        assert_eq!(kind_for("com.elgato.obsstudio.unknownthing"), None);
        assert_eq!(kind_for("com.elgato.streamdeck.system.hotkey"), None);
        assert_eq!(kind_for("com.example.jobs.scene"), None);
    }

    #[test]
    fn names_from_settings_or_title() {
        let mut data = settings_of(json!({"UUID": "com.lindeck.obs.scene", "Settings": {"sceneName": "Kamera"},
                                           "States": [{"Title": "Szene 1"}, {}]}));
        let a = ActionData::new(data.clone(), PathBuf::new());
        assert_eq!(ObsAction::scene(&a), "Kamera");
        data.insert("Settings".into(), json!({}));
        let a = ActionData::new(data, PathBuf::new());
        assert_eq!(ObsAction::scene(&a), "Szene 1");
        assert_eq!(ObsAction::mode(&a), "toggle");
        let obs = ObsAction::new(Kind::Record, "com.lindeck.obs.record");
        assert_eq!(obs.main_text(&a), "REC");
    }
}
