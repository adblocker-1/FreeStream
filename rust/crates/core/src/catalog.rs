//! Katalog der eingebauten Aktionen für den Editor (wie `freestream/catalog.py`).
//!
//! Beschreibt für jede Elgato-Aktion Anzeigename, Kategorie, Standardwerte und die Felder, die im
//! Editor bearbeitet werden können. Neue Aktionen werden im Originalformat der Elgato-App angelegt,
//! damit die Profile austauschbar bleiben.
//!
//! Texte werden als msgid gespeichert ([`Text`]) und erst beim Abruf übersetzt – so stimmt die
//! Sprache auch nach einem Sprachwechsel zur Laufzeit.
//!
//! Außerdem: Umwandlung von Hotkeys zwischen Elgato-Format (Windows-VK-Code, Qt-Keycode,
//! Modifier-Bits) und lesbarem Text wie „Strg+Umschalt+T“.

use serde_json::{json, Map, Value};
use std::sync::{OnceLock, RwLock};

use crate::i18n::{gettext, pgettext};
use crate::profiles::Action;

// ============================================================================================ Typen
/// Anzeigetext: msgid (wird beim Abruf übersetzt) oder fester Text (Eigennamen wie „OBS Studio“).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Text {
    Tr(String),
    Raw(String),
}

impl Text {
    /// Übersetzter Text in der aktiven Sprache.
    pub fn get(&self) -> String {
        match self {
            Text::Tr(s) if s.is_empty() => String::new(),
            Text::Tr(s) => gettext(s),
            Text::Raw(s) => s.clone(),
        }
    }

    /// Unübersetzter Schlüssel (msgid bzw. fester Text) – unabhängig von der Sprache.
    pub fn id(&self) -> &str {
        match self {
            Text::Tr(s) | Text::Raw(s) => s,
        }
    }

    pub fn is_empty(&self) -> bool {
        self.id().is_empty()
    }
}

impl Default for Text {
    fn default() -> Self {
        Text::Raw(String::new())
    }
}

fn t(s: &str) -> Text {
    Text::Tr(s.to_string())
}

fn r(s: &str) -> Text {
    Text::Raw(s.to_string())
}

/// Wo eine Aktion liegen kann: Taste oder Drehregler.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Controller {
    Key,
    Dial,
}

pub const KEY: Controller = Controller::Key;
pub const DIAL: Controller = Controller::Dial;

/// Art eines Eingabefelds im Editor.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum FieldKind {
    Text,
    Multiline,
    /// Ganzzahl mit `min`/`max` (in Python „int“)
    Int,
    Bool,
    Choice,
    File,
    /// Index in `Settings.Hotkeys` (siehe [`Field::hotkey_index`])
    Hotkey,
    Page,
    Profile,
    /// Home-Assistant-Entität; erlaubte Domains in [`Field::domains`]
    Entity,
}

/// Ein Auswahlwert: gespeicherter Wert (Zahl oder Text) und Anzeige.
#[derive(Debug, Clone, PartialEq)]
pub struct Choice {
    pub value: Value,
    pub label: Text,
}

/// Auswahlliste eines Felds.
#[derive(Debug, Clone, PartialEq, Default)]
pub enum Choices {
    #[default]
    None,
    /// feste Liste
    Static(Vec<Choice>),
    /// Liste, die erst beim Anzeigen entsteht (Mixe, Szenen, Entitäten). Der Schlüssel wird mit
    /// [`resolve_choices`] aufgelöst; Home-Assistant-Entitäten löst die Oberfläche selbst auf.
    Dynamic(&'static str),
}

/// Dynamische Auswahl: Mixe für Kanal-Tasten (alle, einer davon oder per Mix-Taste).
pub const DYN_MIXER_MIXES: &str = "mixer.mixes";
/// Dynamische Auswahl: Mix der Mix-Taste (alle, jeder Mix, nächster Mix).
pub const DYN_MIXER_MIXSELECT: &str = "mixer.mixselect";
/// Dynamische Auswahl: in FreeWave gespeicherte Mixer-Szenen.
pub const DYN_MIXER_SCENES: &str = "mixer.scenes";
/// Dynamische Auswahl: Home-Assistant-Entitäten (nur die Oberfläche kennt die Verbindung).
pub const DYN_HA_ENTITIES: &str = "homeassistant.entities";
/// Dynamische Auswahl: Klänge aus dem Soundboard von FreeWave (config.json "soundboard").
pub const DYN_SOUNDBOARD: &str = "freewave.soundboard";

/// Ein Eingabefeld einer Aktion.
#[derive(Debug, Clone, PartialEq)]
pub struct Field {
    pub kind: FieldKind,
    /// Schlüssel in „Settings“ (bei Hotkeys: Index in „Hotkeys“ als Text, z. B. "0")
    pub key: String,
    pub label: Text,
    pub choices: Choices,
    /// Entität: erlaubte Domains (None = alle)
    pub domains: Option<Vec<String>>,
    pub min: i64,
    pub max: i64,
    pub hint: Text,
}

impl Field {
    pub fn new(kind: FieldKind, key: &str, label: Text) -> Self {
        Self { kind, key: key.to_string(), label, choices: Choices::None, domains: None, min: 0, max: 100000, hint: Text::default() }
    }

    fn opts(mut self, list: Vec<Choice>) -> Self {
        self.choices = Choices::Static(list);
        self
    }

    fn dynamic(mut self, key: &'static str) -> Self {
        self.choices = Choices::Dynamic(key);
        self
    }

    fn range(mut self, min: i64, max: i64) -> Self {
        self.min = min;
        self.max = max;
        self
    }

    fn with_hint(mut self, hint: Text) -> Self {
        self.hint = hint;
        self
    }

    fn with_domains(mut self, d: &[&str]) -> Self {
        self.domains = Some(d.iter().map(|s| s.to_string()).collect());
        self
    }

    /// Übersetzte Beschriftung.
    pub fn label(&self) -> String {
        self.label.get()
    }

    /// Übersetzter Hinweis (leer, wenn keiner).
    pub fn hint(&self) -> String {
        self.hint.get()
    }

    /// Index in `Settings.Hotkeys` bei Hotkey-Feldern.
    pub fn hotkey_index(&self) -> Option<usize> {
        if self.kind == FieldKind::Hotkey {
            self.key.parse().ok()
        } else {
            None
        }
    }

    /// Auswahl als (Wert, übersetzte Anzeige); dynamische Listen über [`resolve_choices`].
    pub fn resolved_choices(&self) -> Vec<(Value, String)> {
        match &self.choices {
            Choices::None => Vec::new(),
            Choices::Static(list) => list.iter().map(|c| (c.value.clone(), c.label.get())).collect(),
            Choices::Dynamic(key) => resolve_choices(key),
        }
    }
}

fn f(kind: FieldKind, key: &str, label: &str) -> Field {
    Field::new(kind, key, t(label))
}

fn ch(value: impl Into<Value>, label: Text) -> Choice {
    Choice { value: value.into(), label }
}

/// Liste aus (Wert, msgid).
fn tr_choices<V: Into<Value> + Clone>(list: &[(V, &str)]) -> Vec<Choice> {
    list.iter().map(|(v, l)| ch(v.clone(), t(l))).collect()
}

/// Art der Kindaktionen: Gruppen (`[{"Actions": [...]}, ...]`) oder feste Plätze (`[{...}, ...]`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChildKind {
    Groups,
    Slots,
}

/// Kindaktionen einer zusammengesetzten Aktion (Multi Action, Key Logic …).
#[derive(Debug, Clone, PartialEq)]
pub struct Children {
    pub kind: ChildKind,
    pub labels: Vec<Text>,
}

/// Eine Aktionsart des Katalogs.
#[derive(Debug, Clone, PartialEq)]
pub struct ActionType {
    pub uuid: String,
    pub name: Text,
    /// Kategorie; `category.id()` ist der sprachunabhängige Schlüssel (z. B. "System", "OBS Studio")
    pub category: Text,
    /// Standardwerte für „Settings“
    pub settings: Map<String, Value>,
    pub states: usize,
    pub controllers: Vec<Controller>,
    pub fields: Vec<Field>,
    pub dial_fields: Option<Vec<Field>>,
    pub children: Option<Children>,
    pub description: Text,
    /// UUID des Plugins (leer = Aktion selbst)
    pub plugin: String,
    pub plugin_version: String,
}

impl ActionType {
    /// Neue Aktionsart (z. B. für Plugin-Aktionen, die die Oberfläche selbst einliest).
    pub fn new(uuid: &str, name: Text, category: Text) -> Self {
        Self {
            uuid: uuid.to_string(),
            name,
            category,
            settings: Map::new(),
            states: 1,
            controllers: vec![KEY],
            fields: Vec::new(),
            dial_fields: None,
            children: None,
            description: Text::default(),
            plugin: String::new(),
            plugin_version: "1.0".into(),
        }
    }

    fn set(mut self, v: Value) -> Self {
        self.settings = v.as_object().cloned().unwrap_or_default();
        self
    }

    fn states(mut self, n: usize) -> Self {
        self.states = n;
        self
    }

    fn ctrl(mut self, c: &[Controller]) -> Self {
        self.controllers = c.to_vec();
        self
    }

    fn fields(mut self, f: Vec<Field>) -> Self {
        self.fields = f;
        self
    }

    fn dial(mut self, f: Vec<Field>) -> Self {
        self.dial_fields = Some(f);
        self
    }

    fn children(mut self, kind: ChildKind, labels: &[&str]) -> Self {
        self.children = Some(Children { kind, labels: labels.iter().map(|l| t(l)).collect() });
        self
    }

    fn desc(mut self, d: &str) -> Self {
        self.description = t(d);
        self
    }

    fn plugin(mut self, p: &str) -> Self {
        self.plugin = p.to_string();
        self
    }

    /// Übersetzter Anzeigename.
    pub fn name(&self) -> String {
        self.name.get()
    }

    /// Übersetzte Kategorie.
    pub fn category_label(&self) -> String {
        self.category.get()
    }

    /// Übersetzte Beschreibung.
    pub fn description(&self) -> String {
        self.description.get()
    }

    pub fn supports(&self, c: Controller) -> bool {
        self.controllers.contains(&c)
    }

    /// Felder für eine Taste oder einen Drehregler.
    pub fn fields_for(&self, kind: Controller) -> &[Field] {
        match (&self.dial_fields, kind) {
            (Some(d), Controller::Dial) => d,
            _ => &self.fields,
        }
    }
}

// ========================================================================================= Hotkeys
pub const EMPTY_QT: i64 = 33554431;

pub const MOD_SHIFT: u32 = 1;
pub const MOD_CTRL: u32 = 2;
pub const MOD_ALT: u32 = 4;
pub const MOD_META: u32 = 8;

/// Leerer Hotkey im Elgato-Format.
pub fn empty_hotkey() -> Value {
    json!({"KeyCmd": false, "KeyCtrl": false, "KeyModifiers": 0, "KeyOption": false, "KeyShift": false,
           "NativeCode": -1, "QTKeyCode": EMPTY_QT, "VKeyCode": -1})
}

// ========================================================================================= Katalog
pub const FOLDER_UUID: &str = "com.elgato.streamdeck.profile.openchild";
pub const BACK_UUID: &str = "com.elgato.streamdeck.profile.backtoparent";
/// Kategorie, die es nur mit FreeWave gibt (Mixer, Wave-Mikrofon).
pub const FREEWAVE_CATEGORY: &str = "FreeWave";
/// Kategorien Uhr und Systemmonitor (msgids).
pub const CLOCK_CAT: &str = "Clock & timer";
pub const SYSMON_CAT: &str = "System monitor";

/// Erlaubte Domains für „Entität umschalten“.
pub const HA_TOGGLE_DOMAINS: &[&str] = &[
    "light", "switch", "fan", "cover", "input_boolean", "automation", "lock", "climate", "media_player", "siren",
    "humidifier", "input_number", "number", "button", "input_button",
];

fn hk_settings(n: usize) -> Value {
    Value::Array((0..n).map(|_| empty_hotkey()).collect())
}

fn build_catalog() -> Vec<ActionType> {
    use ChildKind::{Groups, Slots};
    use FieldKind::*;
    let a = |uuid: &str, name: self::Text, cat: self::Text| ActionType::new(uuid, name, cat);

    let media = tr_choices(&[
        (1, "Play / Pause"), (2, "Next track"), (0, "Previous track"), (3, "Stop"), (4, "Mute"), (5, "Volume up"),
        (6, "Volume down"), (13, "Fast forward"), (14, "Rewind"), (18, "Volume (dial)"),
    ]);
    let brightness = tr_choices(&[(0, "Brighter"), (1, "Darker"), (2, "Minimum"), (3, "Low"), (4, "Medium"), (5, "High"), (6, "Maximum")]);
    let audio_modes = tr_choices(&[(0, "Play / Overlap"), (1, "Play / Stop"), (2, "Play / Restart"), (3, "Loop / Stop")]);
    let volume_modes = tr_choices(&[("mute", "Toggle mute"), ("up", "Volume up"), ("down", "Volume down"), ("set", "Set volume")]);
    let hk_key = vec![f(Hotkey, "0", "Key combination")];
    let hk_dial = vec![f(Hotkey, "0", "Rotate left"), f(Hotkey, "1", "Rotate right"), f(Hotkey, "2", "Press"), f(Hotkey, "3", "Touch")];
    let system = || t("System");
    let nav = || t("Navigation");
    let fd = || r("FreeDeck");
    let multi = || r("Multi Action");
    let audio = || t("Audio");
    let volume_fields = |app: bool| {
        let mut v = vec![
            f(Choice, "action", "Key").opts(volume_modes.clone()),
            f(Int, "volumeStep", "Step size (%)").range(1, 50),
            f(Text, "friendlyName", "Device (empty = default)"),
        ];
        if app {
            v.push(f(Text, "appName", "Only this program (optional)"));
        }
        v
    };

    let mut out = vec![
        // ------------------------------------------------------------ System
        a("com.elgato.streamdeck.system.hotkey", r("Hotkey"), system())
            .set(json!({"Coalesce": true, "Hotkeys": hk_settings(4)}))
            .ctrl(&[KEY, DIAL]).fields(hk_key).dial(hk_dial)
            .desc("Sends a key combination"),
        a("com.elgato.streamdeck.system.hotkeyswitch", r("Hotkey Switch"), system())
            .set(json!({"Hotkeys": hk_settings(2)})).states(2)
            .fields(vec![f(Hotkey, "0", "State 1"), f(Hotkey, "1", "State 2")])
            .desc("Switches between two key combinations on each press"),
        a("com.elgato.streamdeck.system.text", t("Text"), system())
            .set(json!({"pastedText": "", "isSendingEnter": false, "isTypingMode": false}))
            .fields(vec![f(Multiline, "pastedText", "Text"), f(Bool, "isSendingEnter", "Send Enter afterwards"),
                         f(Bool, "isTypingMode", "Type instead of pasting")])
            .desc("Pastes a text"),
        a("com.elgato.streamdeck.system.open", t("Open"), system())
            .set(json!({"path": ""}))
            .fields(vec![f(File, "path", "File, folder or URL")])
            .desc("Opens a file, folder or web address"),
        a("com.elgato.streamdeck.system.openapp", t("Open Application"), system())
            .set(json!({"exec": "", "args": "", "long_press": "none", "bring_to_front": true}))
            .fields(vec![
                f(File, "exec", "Program / command").with_hint(t("e.g. firefox or flatpak run …")),
                f(Text, "args", "Arguments"),
                f(Bool, "bring_to_front", "Bring a running program to the front"),
                f(Choice, "long_press", "Long press").opts(tr_choices(&[("none", "nothing"), ("quit", "Quit program")])),
            ])
            .desc("Launches a program"),
        a("com.elgato.streamdeck.system.close", t("Close Application"), system())
            .set(json!({"exec": "", "app_name": ""}))
            .fields(vec![f(File, "exec", "Program"), f(Text, "app_name", "Process name")])
            .desc("Quits a program"),
        a("com.elgato.streamdeck.system.website", r("Website"), system())
            .set(json!({"path": "", "browser": ""}))
            .fields(vec![f(Text, "path", "Address").with_hint(r("https://…")), f(Text, "browser", "Browser (empty = default)")])
            .desc("Opens a website"),
        a("com.elgato.streamdeck.system.multimedia", r("Multimedia"), system())
            .set(json!({"actionIdx": 1})).ctrl(&[KEY, DIAL])
            .fields(vec![f(Choice, "actionIdx", "Function").opts(media)])
            .desc("Controls playback and volume"),
        a("com.elgato.streamdeck.system.timer", r("Timer"), system())
            .set(json!({"duration": 300, "alarm": ""}))
            .fields(vec![f(Int, "duration", "Duration (seconds)").range(1, 86400), f(File, "alarm", "Alarm sound (optional)")])
            .desc("Countdown; long press resets"),
        a("com.elgato.streamdeck.system.mouse-wheel", t("Mouse Wheel"), system())
            .set(json!({"horizontal": false, "invert": false})).ctrl(&[DIAL])
            .fields(vec![f(Bool, "horizontal", "Scroll horizontally"), f(Bool, "invert", "Invert")])
            .desc("Scrolls with the dial"),
        // -------------------------------------------------------- Navigation
        a(FOLDER_UUID, t("Create Folder"), nav()).set(json!({"ProfileUUID": ""})).desc("Sub-page with its own keys"),
        a(BACK_UUID, t("Parent Folder"), nav()).desc("Goes back out of a folder"),
        a("com.elgato.streamdeck.page.next", t("Next Page"), nav()),
        a("com.elgato.streamdeck.page.previous", t("Previous Page"), nav()),
        a("com.elgato.streamdeck.page.goto", t("Go to Page"), nav())
            .set(json!({"PageIndex": 1})).fields(vec![f(Page, "PageIndex", "Page")]),
        a("com.elgato.streamdeck.page.indicator", t("Page Indicator"), nav()),
        a("com.elgato.streamdeck.profile.rotate", t("Switch Profile"), nav())
            .set(json!({"ProfileUUID": ""})).fields(vec![f(Profile, "ProfileUUID", "Profile")]),
        // --------------------------------------------------------- Stream Deck
        a("com.elgato.streamdeck.system.keybrightness", t("Brightness"), fd())
            .set(json!({"actionIdx": 0})).ctrl(&[KEY, DIAL])
            .fields(vec![f(Choice, "actionIdx", "Function").opts(brightness)]),
        a("com.elgato.streamdeck.system.sleep", r("Sleep"), fd()).desc("Turns the display off"),
        a("com.elgato.streamdeck.system.digitaltime", t("Time"), fd())
            .set(json!({"24HourFormat": true, "theme": "dark"}))
            .fields(vec![f(Bool, "24HourFormat", "24-hour format"),
                         f(Choice, "theme", "Appearance").opts(tr_choices(&[("dark", "dark"), ("light", "light")]))]),
        // -------------------------------------------------------- Multi Action
        a("com.elgato.streamdeck.multiactions.routine", r("Multi Action"), multi())
            .set(json!({"Actions": null})).children(Groups, &["Steps"])
            .desc("Several actions in sequence"),
        a("com.elgato.streamdeck.multiactions.routine2", r("Multi Action Switch"), multi())
            .states(2).children(Groups, &["State 1", "State 2"])
            .desc("Alternates between two sequences"),
        a("com.elgato.streamdeck.multiactions.random", t("Random Action"), multi()).children(Groups, &["Selection"]),
        a("com.elgato.streamdeck.multiactions.delay", t("Delay"), multi())
            .set(json!({"delay": 500})).fields(vec![f(Int, "delay", "Wait time (ms)").range(0, 600000)])
            .desc("Only inside a Multi Action"),
        a("com.elgato.streamdeck.keys.logic", r("Key Logic"), multi())
            .children(Slots, &["Tap", "Double tap", "Hold"])
            .desc("Different actions for tap, double tap and hold"),
        a("com.elgato.streamdeck.keys.adaptor", r("Action Trigger"), multi())
            .ctrl(&[DIAL]).children(Slots, &["Turn left", "Press", "Turn right"]),
        a("com.elgato.streamdeck.keys.stack", r("Action Wheel"), multi())
            .set(json!({"CurrentIdx": 0})).ctrl(&[DIAL]).children(Groups, &["Actions"]),
        a("com.elgato.streamdeck.dial.stack", r("Dial Stack"), multi())
            .set(json!({"CurrentIdx": 0})).ctrl(&[DIAL]).children(Groups, &["Dial actions"]),
        // ----------------------------------------------------------- Soundboard
        a("com.elgato.streamdeck.soundboard.playaudio", t("Play Audio"), r("Soundboard"))
            .set(json!({"path": "", "actionType": 0, "volume": 100}))
            .fields(vec![f(File, "path", "Audio file"), f(Choice, "actionType", "Mode").opts(audio_modes),
                         f(Int, "volume", "Volume (%)").range(0, 100)]),
        a("com.elgato.streamdeck.soundboard.stopaudioplay", t("Stop Audio"), r("Soundboard")),
        // ---------------------------------------------------------------- Audio
        a("com.elgato.volume-controller.output-device-control", t("Output Device"), audio())
            .set(json!({"action": "mute", "volumeStep": 5, "friendlyName": ""})).ctrl(&[KEY, DIAL])
            .fields(volume_fields(true)).plugin("com.elgato.volume-controller"),
        a("com.elgato.volume-controller.input-device-control", t("Input Device"), audio())
            .set(json!({"action": "mute", "volumeStep": 5, "friendlyName": ""})).ctrl(&[KEY, DIAL])
            .fields(volume_fields(false)).plugin("com.elgato.volume-controller"),
    ];

    let obs = || r("OBS Studio");
    let toggle = || tr_choices(&[("toggle", "toggle"), ("start", "start"), ("stop", "stop")]);
    let fw = || r(FREEWAVE_CATEGORY);
    let ha = || r("Home Assistant");
    let mix_channels = tr_choices(&[
        ("system", "System"), ("music", "Music"), ("browser", "Browser"), ("voice", "Voice Chat"), ("game", "Game"),
        ("sfx", "Effects"), ("mic", "Microphone"),
    ]);
    let displays = tr_choices(&[("", "Text"), ("tile", "Channel tile"), ("fader_top", "Fader, upper half"), ("fader_bottom", "Fader, lower half")]);
    let mut record_modes = toggle();
    record_modes.push(ch("pause", t("toggle pause")));

    out.extend([
        // ------------------------------------------------------------- OBS
        a("com.lindeck.obs.scene", t("Scene"), obs())
            .set(json!({"scene": ""})).ctrl(&[KEY, DIAL])
            .fields(vec![f(Text, "scene", "Scene").with_hint(t("Name as in OBS"))])
            .desc("Switches the scene (dial: cycles through scenes)"),
        a("com.lindeck.obs.record", t("Record"), obs())
            .set(json!({"mode": "toggle"})).states(2)
            .fields(vec![f(Choice, "mode", "Function").opts(record_modes)]),
        a("com.lindeck.obs.stream", r("Stream"), obs())
            .set(json!({"mode": "toggle"})).states(2)
            .fields(vec![f(Choice, "mode", "Function").opts(toggle())]),
        a("com.lindeck.obs.source", t("Source On/Off"), obs())
            .set(json!({"scene": "", "source": ""})).states(2)
            .fields(vec![f(Text, "source", "Source"), f(Text, "scene", "Scene (empty = current)")]),
        a("com.lindeck.obs.audio", t("Audio Input"), obs())
            .set(json!({"source": "", "step_db": 1})).states(2).ctrl(&[KEY, DIAL])
            .fields(vec![f(Text, "source", "Audio input").with_hint(t("e.g. Mic/Aux")),
                         f(Int, "step_db", "Step per dial notch (dB)").range(1, 10)])
            .desc("Key: mute · Dial: volume"),
        a("com.lindeck.obs.replay", t("Replay Buffer"), obs())
            .set(json!({"mode": "save"}))
            .fields(vec![f(Choice, "mode", "Function").opts(tr_choices(&[("save", "Save replay"), ("toggle", "Buffer on/off")]))]),
        a("com.lindeck.obs.virtualcam", t("Virtual Camera"), obs()).states(2),
        a("com.lindeck.obs.studiomode", t("Studio Mode"), obs()).states(2),
        a("com.lindeck.obs.transition", t("Run Transition"), obs()).desc("Preview → Program in Studio Mode"),
        a("com.lindeck.obs.screenshot", r("Screenshot"), obs())
            .set(json!({"source": ""})).fields(vec![f(Text, "source", "Source (empty = program)")]),
        // ------------------------------------------------------------ Medien
        a("com.lindeck.media.player", t("Media Player"), t("Spotify & Media"))
            .set(json!({"player": "spotify", "function": "playpause"})).ctrl(&[KEY, DIAL])
            .fields(vec![
                Field::new(Text, "player", r("Player")).with_hint(t("spotify, vlc, firefox … (empty = active)")),
                f(Choice, "function", "Function").opts(tr_choices(&[
                    ("playpause", "Play/Pause"), ("next", "Next track"), ("previous", "Previous track"),
                    ("nowplaying", "Now playing"), ("shuffle", "Shuffle"), ("repeat", "Repeat"),
                    ("volumeup", "Volume up"), ("volumedown", "Volume down"), ("stop", "Stop"),
                ])),
            ])
            .desc("Controls Spotify or any MPRIS player"),
        // ----------------------------------------------------------- Discord
        a("com.lindeck.discord.voice", r("Discord"), r("Discord"))
            .set(json!({"function": "mute", "channel_id": ""})).states(2)
            .fields(vec![
                f(Choice, "function", "Function").opts(vec![
                    ch("mute", t("Mute microphone")), ch("deafen", t("Deafen")), ch("pushtotalk", r("Push to Talk")),
                    ch("pushtomute", r("Push to Mute")), ch("voicechannel", t("Join voice channel")),
                    ch("textchannel", t("Open text channel")), ch("leave", t("Leave voice channel")),
                ]),
                f(Text, "channel_id", "Channel ID (channel only)"),
            ])
            .desc("Needs your own Discord app (see README)"),
        // ---------------------------------------------------- Home Assistant
        a("com.lindeck.homeassistant.toggle", t("Toggle Entity"), ha())
            .set(json!({"entity_id": "", "show_value": true, "step": 5})).states(2).ctrl(&[KEY, DIAL])
            .fields(vec![
                f(Entity, "entity_id", "Entity").dynamic(DYN_HA_ENTITIES).with_domains(HA_TOGGLE_DOMAINS),
                f(Bool, "show_value", "Show brightness / position"),
                f(Int, "step", "Dial step size (%)").range(1, 50),
            ])
            .desc("Switches a light, switch, fan, cover … (dial: brightness / temperature)"),
        a("com.lindeck.homeassistant.service", t("Call Service"), ha())
            .set(json!({"service": "", "entity_id": "", "data": ""}))
            .fields(vec![
                f(Text, "service", "Service").with_hint(t("e.g. light.turn_on")),
                f(Entity, "entity_id", "Entity (optional)").dynamic(DYN_HA_ENTITIES),
                f(Multiline, "data", "Service data (JSON, optional)"),
            ])
            .desc("Calls any Home Assistant service"),
        a("com.lindeck.homeassistant.scene", t("Scene / Script"), ha())
            .set(json!({"entity_id": ""}))
            .fields(vec![f(Entity, "entity_id", "Scene or script").dynamic(DYN_HA_ENTITIES).with_domains(&["scene", "script"])])
            .desc("Activates a scene or runs a script"),
        a("com.lindeck.homeassistant.state", t("Show State"), ha())
            .set(json!({"entity_id": "", "attribute": "", "decimals": -1})).ctrl(&[KEY, DIAL])
            .fields(vec![
                f(Entity, "entity_id", "Entity").dynamic(DYN_HA_ENTITIES),
                f(Text, "attribute", "Attribute (empty = state)"),
                f(Int, "decimals", "Decimal places (−1 = automatic)").range(-1, 6),
            ])
            .desc("Shows a sensor value with its unit, updated live"),
        // ------------------------------------------------------------- Mixer
        a("com.lindeck.mixer.channel", t("Mixer Channel"), fw())
            .set(json!({"channel": "music", "mix": "both", "action": "mute", "step": 5})).ctrl(&[KEY, DIAL])
            .fields(vec![
                f(Choice, "channel", "Channel").opts(mix_channels),
                Field::new(Choice, "mix", r("Mix")).dynamic(DYN_MIXER_MIXES),
                f(Choice, "action", "Key").opts(tr_choices(&[
                    ("mute", "toggle mute"), ("up", "volume up"), ("down", "volume down"), ("set", "Set volume"),
                ])),
                f(Int, "step", "Step size (%)").range(1, 50),
                f(Int, "volume", "Volume for “set” (%)").range(0, 100),
                f(Choice, "display", "Look").opts(displays),
            ])
            .desc("Wave Link replacement: channel volume per mix"),
        a("com.lindeck.mixer.mixselect", t("Choose Mix"), fw())
            .set(json!({"mix": "both"}))
            .fields(vec![Field::new(Choice, "mix", r("Mix")).dynamic(DYN_MIXER_MIXSELECT)])
            .desc("Chooses which mix the channel keys with “Chosen with the Mix key” control"),
        a("com.lindeck.mixer.pages", t("Channel Pages"), fw())
            .set(json!({"first": 1, "last": 1}))
            .fields(vec![f(Int, "first", "First channel").range(1, 99), f(Int, "last", "Last channel").range(1, 99)])
            .desc("Shows which channels this page controls; press to turn the page"),
        a("com.lindeck.mixer.output", t("Personal Mix Output"), fw())
            .set(json!({"device": ""})).fields(vec![f(Text, "device", "Device (empty = next)")])
            .desc("Switches the Personal Mix between headphones and speakers"),
        a("com.lindeck.mixer.scene", t("Mixer Scene"), fw())
            .set(json!({"scene": ""})).states(2)
            .fields(vec![f(Choice, "scene", "Scene").dynamic(DYN_MIXER_SCENES)])
            .desc("Applies a mixer scene saved in FreeWave and shows when it is active"),
        a("com.lindeck.freewave.soundboard", t("Soundboard: Play Sound"), fw())
            .set(json!({"sound": ""})).states(2)
            .fields(vec![f(Choice, "sound", "Sound").dynamic(DYN_SOUNDBOARD)])
            .desc("Plays a sound from the FreeWave soundboard in the chosen mixes (press again to stop)"),
        // -------------------------------------------------------- Wave-Mikro
        a("com.lindeck.wavemic.control", t("Wave Microphone"), fw())
            .set(json!({"function": "mute", "step": 5})).ctrl(&[KEY, DIAL])
            .fields(vec![
                f(Choice, "function", "Function").opts(vec![
                    ch("mute", t("toggle mute")), ch("gain", r("Gain +")), ch("lowcut", t("Low Cut on/off")),
                    ch("clipguard", t("Clipguard on/off")), ch("leds", t("LEDs on/off (Wave:3)")),
                    ch("ledsflip", t("Invert LEDs (Wave:3)")),
                ]),
                f(Int, "step", "Gain step size (%)").range(1, 20),
            ])
            .desc("Gain, mute, Low Cut, Clipguard, LEDs (dial: gain)"),
    ]);

    let colors = tr_choices(&[
        ("blue", "Blue"), ("green", "Green"), ("orange", "Orange"), ("red", "Red"), ("purple", "Purple"),
        ("teal", "Turquoise"), ("white", "White"),
    ]);
    let color = || f(Choice, "color", "Accent color").opts(colors.clone());
    let graph = || f(Bool, "graph", "Show graph");
    let clock = || t(CLOCK_CAT);
    let sysmon = || t(SYSMON_CAT);

    out.extend([
        // -------------------------------------------------------- Uhr & Timer
        a("com.lindeck.clock.clock", t("Clock"), clock())
            .set(json!({"hour24": true, "seconds": false, "show": "both", "date_format": "weekday", "timezone": "",
                        "color": "blue"}))
            .ctrl(&[KEY, DIAL])
            .fields(vec![
                f(Choice, "show", "Show").opts(tr_choices(&[("both", "Time and date"), ("time", "Time only"), ("date", "Date only")])),
                f(Choice, "date_format", "Date format").opts(vec![
                    ch("weekday", t("Weekday and date")), ch("dmy", r("31.12.2026")), ch("mdy", r("12/31/2026")),
                    ch("iso", r("2026-12-31")),
                ]),
                f(Bool, "hour24", "24-hour format"),
                f(Bool, "seconds", "Show seconds"),
                f(Text, "timezone", "Time zone").with_hint(t("e.g. Europe/Berlin (empty = local)")),
                color(),
            ])
            .desc("Shows the time and date, optionally in another time zone"),
        a("com.lindeck.clock.countdown", t("Countdown"), clock())
            .set(json!({"duration": "5:00", "label": "", "play_sound": true, "sound": "", "volume": 100,
                        "repeat_sound": false, "notify": true, "dial_step": 30, "color": "orange"}))
            .ctrl(&[KEY, DIAL])
            .fields(vec![
                f(Text, "duration", "Duration").with_hint(t("e.g. 5:00, 90s or 1h30m")),
                f(Text, "label", "Label"),
                color(),
                f(Bool, "play_sound", "Sound at the end"),
                f(Bool, "notify", "Notification at the end"),
                f(File, "sound", "Alarm sound").with_hint(t("empty = default sound")),
                f(Int, "volume", "Volume (%)").range(0, 100),
                f(Bool, "repeat_sound", "Repeat sound"),
                f(Int, "dial_step", "Dial step (seconds)").range(1, 3600),
            ])
            .desc("Press: start/pause · long press: reset · dial: change duration"),
        a("com.lindeck.clock.stopwatch", t("Stopwatch"), clock())
            .set(json!({"label": "", "color": "green"})).ctrl(&[KEY, DIAL])
            .fields(vec![f(Text, "label", "Label"), color()])
            .desc("Press: start/stop · long press: reset"),
        // ------------------------------------------------------ Systemmonitor
        a("com.lindeck.sysmon.cpu", t("CPU Load"), sysmon())
            .set(json!({"graph": true, "color": "blue"})).ctrl(&[KEY, DIAL])
            .fields(vec![graph(), color()]).desc("Processor load in percent"),
        a("com.lindeck.sysmon.memory", t("Memory"), sysmon())
            .set(json!({"graph": true, "color": "purple"})).ctrl(&[KEY, DIAL])
            .fields(vec![graph(), color()]).desc("Used RAM in percent"),
        a("com.lindeck.sysmon.temperature", t("Temperature"), sysmon())
            .set(json!({"graph": true, "unit": "c", "sensor": "", "color": "orange"})).ctrl(&[KEY, DIAL])
            .fields(vec![
                f(Choice, "unit", "Unit").opts(vec![ch("c", r("°C")), ch("f", r("°F"))]),
                graph(),
                f(Text, "sensor", "Sensor").with_hint(t("e.g. coretemp or k10temp (empty = automatic)")),
                color(),
            ])
            .desc("Temperature from the hardware sensors"),
        a("com.lindeck.sysmon.network", t("Network"), sysmon())
            .set(json!({"interface": "", "graph": true, "color": "teal"})).ctrl(&[KEY, DIAL])
            .fields(vec![f(Text, "interface", "Network interface").with_hint(t("e.g. wlan0 (empty = all)")), graph(), color()])
            .desc("Download and upload rate"),
        a("com.lindeck.sysmon.battery", t("Battery"), sysmon())
            .set(json!({"graph": false, "battery": "", "color": "green"})).ctrl(&[KEY, DIAL])
            .fields(vec![graph(), f(Text, "battery", "Battery").with_hint(t("e.g. BAT1 (empty = first)")), color()])
            .desc("Charge level and charging state"),
        a("com.lindeck.sysmon.disk", t("Disk Space"), sysmon())
            .set(json!({"path": "/", "graph": false, "color": "blue"})).ctrl(&[KEY, DIAL])
            .fields(vec![f(Text, "path", "Folder").with_hint(t("e.g. / or /home")), graph(), color()])
            .desc("Used space of a drive"),
    ]);
    out
}

/// Alle eingebauten Aktionen (auch die von FreeWave; ohne FreeWave: [`catalog_for`]).
pub fn catalog() -> &'static [ActionType] {
    static CATALOG: OnceLock<Vec<ActionType>> = OnceLock::new();
    CATALOG.get_or_init(build_catalog)
}

/// Katalog mit oder ohne die Aktionen von FreeWave (Mixer, Wave-Mikrofon).
pub fn catalog_for(freewave: bool) -> Vec<&'static ActionType> {
    catalog().iter().filter(|a| freewave || a.category.id() != FREEWAVE_CATEGORY).collect()
}

/// Kategorien in der Reihenfolge des Katalogs (`id()` als Schlüssel, `get()` zum Anzeigen).
pub fn categories(freewave: bool) -> Vec<Text> {
    let mut out: Vec<Text> = Vec::new();
    for a in catalog_for(freewave) {
        if !out.iter().any(|c| c.id() == a.category.id()) {
            out.push(a.category.clone());
        }
    }
    out
}

/// Aktionsart nach UUID (`BY_UUID` der Python-Version).
pub fn by_uuid(uuid: &str) -> Option<&'static ActionType> {
    catalog().iter().find(|a| a.uuid == uuid)
}

/// Gleich wie [`by_uuid`] (`lookup` der Python-Version).
pub fn lookup(uuid: &str) -> Option<&'static ActionType> {
    by_uuid(uuid)
}

/// Anzeigename einer Aktion (Katalog, sonst „Name“ oder letzter Teil der UUID).
pub fn display_name(data: &Action) -> String {
    if data.is_empty() {
        return String::new();
    }
    let uuid = data.get("UUID").and_then(Value::as_str).unwrap_or("");
    if let Some(t) = by_uuid(uuid) {
        return t.name();
    }
    match data.get("Name").and_then(Value::as_str) {
        Some(n) if !n.is_empty() => n.to_string(),
        _ => uuid.rsplit('.').next().unwrap_or("").to_string(),
    }
}

/// Schlüssel eines Zustands, die zum Titelstil gehören.
pub const TITLE_KEYS: [&str; 7] = ["ShowTitle", "TitleAlignment", "FontFamily", "FontSize", "FontStyle", "FontUnderline", "TitleColor"];

fn title_defaults() -> &'static RwLock<Map<String, Value>> {
    static D: OnceLock<RwLock<Map<String, Value>>> = OnceLock::new();
    D.get_or_init(|| RwLock::new(Map::new()))
}

/// Schrift, Position und Farbe für neu angelegte Tasten (Einstellungen › Stil).
pub fn set_title_defaults(style: &Map<String, Value>) {
    let mut d = title_defaults().write().unwrap_or_else(|e| e.into_inner());
    d.clear();
    for (k, v) in style {
        if TITLE_KEYS.contains(&k.as_str()) {
            d.insert(k.clone(), v.clone());
        }
    }
}

/// Zustand einer neuen Taste im Elgato-Format.
pub fn default_state() -> Map<String, Value> {
    let mut st = json!({"FontFamily": "", "FontSize": 12, "FontStyle": "", "FontUnderline": false, "Image": "",
                        "OutlineThickness": 2, "ShowTitle": true, "Title": "", "TitleAlignment": "bottom",
                        "TitleColor": "#ffffff"})
    .as_object()
    .cloned()
    .unwrap_or_default();
    for (k, v) in title_defaults().read().unwrap_or_else(|e| e.into_inner()).iter() {
        st.insert(k.clone(), v.clone());
    }
    st
}

/// Neue Aktion im Format der Elgato-App (unbekannte UUID: Aktion ohne Katalogeintrag).
pub fn new_action(uuid: &str) -> Action {
    match by_uuid(uuid) {
        Some(t) => new_action_of(t),
        None => build_action(None, uuid),
    }
}

/// Neue Aktion zu einer Aktionsart (auch Plugin-Aktionen, die nicht im Katalog stehen).
pub fn new_action_of(t: &ActionType) -> Action {
    build_action(Some(t), &t.uuid)
}

fn build_action(t: Option<&ActionType>, uuid: &str) -> Action {
    let mut settings = t.map(|t| t.settings.clone()).unwrap_or_default();
    settings.remove("Actions");
    let name = t.map(|t| t.name()).unwrap_or_else(|| uuid.rsplit('.').next().unwrap_or("").to_string());
    let plugin_name = t.map(|t| t.category_label().replace("Plugin: ", "")).unwrap_or_default();
    let plugin_uuid = t.filter(|t| !t.plugin.is_empty()).map(|t| t.plugin.clone()).unwrap_or_else(|| uuid.to_string());
    let version = t.map(|t| t.plugin_version.clone()).unwrap_or_else(|| "1.0".into());
    let states: Vec<Value> = (0..t.map(|t| t.states).unwrap_or(1)).map(|_| Value::Object(default_state())).collect();
    let mut data = json!({
        "ActionID": uuid::Uuid::new_v4().to_string(),
        "LinkedTitle": true,
        "Name": name,
        "Plugin": {"Name": plugin_name, "UUID": plugin_uuid, "Version": version},
        "Resources": null,
        "Settings": settings,
        "State": 0,
        "States": states,
        "UUID": uuid,
    })
    .as_object()
    .cloned()
    .unwrap_or_default();
    if let Some(c) = t.and_then(|t| t.children.as_ref()) {
        let list: Vec<Value> = c
            .labels
            .iter()
            .map(|_| match c.kind {
                ChildKind::Groups => json!({"Actions": []}),
                ChildKind::Slots => json!({}),
            })
            .collect();
        data.insert("Actions".into(), Value::Array(list));
    }
    data
}

// ------------------------------------------------------------------------------------ Kindaktionen
/// Gruppen einer zusammengesetzten Aktion (Multi Action, Key Logic, Dial Stack …), vereinheitlicht:
/// Gruppen als `[{"Actions": [...]}, ...]`, Plätze als `[{...}, ...]`. None bei einfachen Aktionen.
pub fn child_lists(data: &mut Action) -> Option<&mut Vec<Value>> {
    let uuid = data.get("UUID").and_then(Value::as_str).unwrap_or("");
    let c = by_uuid(uuid)?.children.as_ref()?;
    let n = c.labels.len();
    if !data.get("Actions").map(Value::is_array).unwrap_or(false) {
        data.insert("Actions".into(), Value::Array(Vec::new()));
    }
    let entries = data.get_mut("Actions")?.as_array_mut()?;
    match c.kind {
        ChildKind::Slots => {
            while entries.len() < n {
                entries.push(json!({}));
            }
        }
        ChildKind::Groups => {
            // ältere Profile: direkt eine Liste von Aktionen statt Gruppen
            let flat = !entries.is_empty()
                && entries.iter().all(|e| e.get("UUID").and_then(Value::as_str).map(|u| !u.is_empty()).unwrap_or(false));
            if flat {
                let old = std::mem::take(entries);
                entries.push(json!({"Actions": old}));
            }
            while entries.len() < n {
                entries.push(json!({"Actions": []}));
            }
            for e in entries.iter_mut() {
                if let Some(o) = e.as_object_mut() {
                    if !o.get("Actions").map(Value::is_array).unwrap_or(false) {
                        o.insert("Actions".into(), Value::Array(Vec::new()));
                    }
                }
            }
        }
    }
    Some(entries)
}

// ---------------------------------------------------------------------------- Dynamische Auswahl
fn mixer_section() -> Map<String, Value> {
    crate::config::read_file()
        .ok()
        .and_then(|c| c.get("mixer").and_then(Value::as_object).cloned())
        .unwrap_or_default()
}

/// [(id, Name)] aller Mixe aus der FreeWave-Konfiguration (feste zuerst).
fn mix_list(mixer: &Map<String, Value>) -> Vec<(String, String)> {
    let mut names = vec![("monitor".to_string(), gettext("Personal Mix")), ("stream".to_string(), gettext("Stream Mix"))];
    let conf: Vec<&Map<String, Value>> = mixer
        .get("mixes")
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(Value::as_object).filter(|m| m.get("id").and_then(Value::as_str).map(|s| !s.is_empty()).unwrap_or(false)).collect())
        .unwrap_or_default();
    let s = |m: &Map<String, Value>, k: &str| m.get(k).and_then(Value::as_str).unwrap_or("").to_string();
    for m in &conf {
        let (id, name) = (s(m, "id"), s(m, "name"));
        if let Some(e) = names.iter_mut().find(|(k, _)| *k == id) {
            if !name.is_empty() {
                e.1 = name;
            }
        }
    }
    for m in &conf {
        let (id, name) = (s(m, "id"), s(m, "name"));
        if id != "monitor" && id != "stream" {
            names.push((id.clone(), if name.is_empty() { id } else { name }));
        }
    }
    names
}

/// Dynamische Auswahlliste nach Schlüssel ([`Choices::Dynamic`]) aus config.json.
/// Home-Assistant-Entitäten liefern eine leere Liste – die löst die Oberfläche auf.
pub fn resolve_choices(key: &str) -> Vec<(Value, String)> {
    if key == DYN_SOUNDBOARD {
        return sound_choices(&crate::config::read_file().unwrap_or_default());
    }
    resolve_choices_in(key, &mixer_section())
}

/// [(id, Name)] der Klänge im Soundboard (wie `soundboard.sound_choices` der Python-Version).
pub fn sound_choices(cfg: &Map<String, Value>) -> Vec<(Value, String)> {
    let out: Vec<(Value, String)> = cfg
        .get("soundboard")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_object)
                .filter_map(|s| {
                    let id = s.get("id").and_then(Value::as_str).filter(|i| !i.is_empty())?;
                    let name = s.get("name").and_then(Value::as_str).filter(|n| !n.is_empty()).unwrap_or(id);
                    Some((Value::String(id.into()), name.to_string()))
                })
                .collect()
        })
        .unwrap_or_default();
    if out.is_empty() {
        vec![(Value::String(String::new()), gettext("(no sounds in the FreeWave soundboard yet)"))]
    } else {
        out
    }
}

/// Wie [`resolve_choices`] mit einem gegebenen Abschnitt „mixer“ der Konfiguration.
pub fn resolve_choices_in(key: &str, mixer: &Map<String, Value>) -> Vec<(Value, String)> {
    let v = |s: &str| Value::String(s.to_string());
    match key {
        DYN_MIXER_MIXES => {
            let mut out = vec![(v("both"), gettext("All mixes"))];
            out.extend(mix_list(mixer).into_iter().map(|(k, n)| (Value::String(k), crate::tr!("{name} only", name = n))));
            out.push((v("selected"), gettext("Chosen with the Mix key")));
            out
        }
        DYN_MIXER_MIXSELECT => {
            let mut out = vec![(v("both"), gettext("All"))];
            out.extend(mix_list(mixer).into_iter().map(|(k, n)| (Value::String(k), n)));
            out.push((v("cycle"), gettext("Next mix (cycle)")));
            out
        }
        DYN_MIXER_SCENES => {
            let out: Vec<(Value, String)> = mixer
                .get("scenes")
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .filter_map(Value::as_object)
                        .filter_map(|s| {
                            let id = s.get("id").and_then(Value::as_str).filter(|i| !i.is_empty())?;
                            let name = s.get("name").and_then(Value::as_str).filter(|n| !n.is_empty()).unwrap_or(id);
                            Some((v(id), name.to_string()))
                        })
                        .collect()
                })
                .unwrap_or_default();
            if out.is_empty() {
                vec![(v(""), gettext("(no scenes saved in FreeWave yet)"))]
            } else {
                out
            }
        }
        _ => Vec::new(),
    }
}

// ------------------------------------------------------------------------- Aussehen in der Liste
/// Kategorie (msgid bzw. fester Name) -> (Symbol, Farbe) für die Aktionsliste des Editors.
pub const CATEGORY_LOOK: &[(&str, &str, &str)] = &[
    ("System", "keyboard", "#64748b"),
    ("Navigation", "navigate", "#3b82f6"),
    ("FreeDeck", "grid", "#3b82f6"),
    ("Multi Action", "layers", "#8b5cf6"),
    ("Soundboard", "sfx", "#ec4899"),
    ("Audio", "speaker", "#22c55e"),
    ("OBS Studio", "record", "#ef4444"),
    ("Spotify & Media", "play", "#16a34a"),
    ("Discord", "chat", "#6366f1"),
    ("FreeWave", "wave", "#06b6d4"),
    ("Home Assistant", "home", "#0ea5e9"),
    (CLOCK_CAT, "clock", "#f97316"),
    (SYSMON_CAT, "gauge", "#14b8a6"),
];

/// Eigenes Symbol für einzelne Aktionen (UUID ohne Präfix; sonst das der Kategorie).
pub const ACTION_GLYPH: &[(&str, &str)] = &[
    ("system.hotkey", "keyboard"), ("system.hotkeyswitch", "keyboard"), ("system.text", "text"),
    ("system.open", "folder"), ("system.openapp", "play"), ("system.close", "close"), ("system.website", "globe"),
    ("system.multimedia", "play"), ("system.timer", "auto"), ("system.mouse-wheel", "navigate"),
    ("profile.openchild", "folder"), ("profile.backtoparent", "up"), ("page.next", "arrow"),
    ("page.previous", "arrow_left"), ("page.goto", "goto"), ("page.indicator", "grid"), ("profile.rotate", "deck"),
    ("system.keybrightness", "sun"), ("system.sleep", "moon"), ("system.digitaltime", "auto"),
    ("soundboard.playaudio", "sfx"), ("soundboard.stopaudioplay", "close"),
    ("volume-controller.output-device-control", "speaker"), ("volume-controller.input-device-control", "mic"),
    ("obs.scene", "camera"), ("obs.record", "record"), ("obs.stream", "stream"), ("obs.source", "image"),
    ("obs.audio", "mic"), ("obs.replay", "undo"), ("obs.virtualcam", "camera"), ("obs.studiomode", "grid"),
    ("obs.transition", "arrow"), ("obs.screenshot", "image"), ("media.player", "music"), ("discord.voice", "chat"),
    ("mixer.channel", "fx"), ("mixer.output", "headphones"), ("mixer.mixselect", "wave"), ("mixer.pages", "grid"),
    ("wavemic.control", "mic"),
    ("clock.clock", "clock"), ("clock.countdown", "hourglass"), ("clock.stopwatch", "stopwatch"), ("sysmon.cpu", "cpu"),
    ("sysmon.memory", "memory"), ("sysmon.temperature", "thermometer"), ("sysmon.network", "network"),
    ("sysmon.battery", "battery"), ("sysmon.disk", "disk"),
    ("homeassistant.toggle", "home"), ("homeassistant.service", "gear"), ("homeassistant.scene", "spark"),
    ("homeassistant.state", "gauge"),
];

/// Symbol und Farbe einer Kategorie (Schlüssel oder übersetzte Anzeige; Plugins: Puzzleteil).
pub fn category_look(cat: &str) -> (&'static str, &'static str) {
    if cat.starts_with("Plugin:") {
        return ("puzzle", "#f59e0b");
    }
    CATEGORY_LOOK
        .iter()
        .find(|(c, _, _)| *c == cat)
        .or_else(|| CATEGORY_LOOK.iter().find(|(c, _, _)| gettext(c) == cat))
        .map(|(_, g, col)| (*g, *col))
        .unwrap_or(("puzzle", "#f59e0b"))
}

/// Symbol und Farbe einer Aktion (Kachel in der Bibliothek, Kopf der Eigenschaften).
pub fn action_look(uuid: &str, category: &str) -> (&'static str, &'static str) {
    let (glyph, color) = category_look(category);
    for prefix in ["com.elgato.streamdeck.", "com.elgato.", "com.lindeck."] {
        if let Some(rest) = uuid.strip_prefix(prefix) {
            let g = ACTION_GLYPH.iter().find(|(k, _)| *k == rest).map(|(_, g)| *g).unwrap_or(glyph);
            return (g, color);
        }
    }
    (glyph, color)
}

// ========================================================================================= Hotkeys
/// Modifier-Bits und ihre Anzeigenamen in der aktiven Sprache (Reihenfolge der Anzeige).
pub fn mod_names() -> Vec<(u32, String)> {
    vec![
        (MOD_CTRL, pgettext("key name", "Ctrl")),
        (MOD_SHIFT, pgettext("key name", "Shift")),
        (MOD_ALT, "Alt".into()),
        (MOD_META, "Super".into()),
    ]
}

const MOD_ALIASES: &[(&str, u32)] = &[
    ("strg", MOD_CTRL), ("ctrl", MOD_CTRL), ("control", MOD_CTRL), ("steuerung", MOD_CTRL),
    ("umschalt", MOD_SHIFT), ("shift", MOD_SHIFT), ("alt", MOD_ALT), ("option", MOD_ALT),
    ("super", MOD_META), ("win", MOD_META), ("meta", MOD_META), ("cmd", MOD_META), ("windows", MOD_META),
    ("maj", MOD_SHIFT), ("mayús", MOD_SHIFT), ("mayus", MOD_SHIFT),
];

fn mod_alias(low: &str) -> Option<u32> {
    MOD_ALIASES
        .iter()
        .find(|(k, _)| *k == low)
        .map(|(_, m)| *m)
        .or_else(|| mod_names().into_iter().find(|(_, n)| n.to_lowercase() == low).map(|(m, _)| m))
}

/// Benannte Tasten: (Windows-VK, Anzeigename in der aktiven Sprache, Qt-Keycode).
pub fn named_keys() -> Vec<(i64, String, i64)> {
    let k = |n: &str| pgettext("key name", n);
    let raw = |n: &str| n.to_string();
    let mut v = vec![
        (0x08, k("Backspace"), 0x01000003), (0x09, raw("Tab"), 0x01000001), (0x0D, raw("Enter"), 0x01000004),
        (0x13, raw("Pause"), 0x01000008), (0x1B, raw("Esc"), 0x01000000), (0x20, k("Space"), 0x20),
        (0x21, k("PgUp"), 0x01000016), (0x22, k("PgDn"), 0x01000017), (0x23, k("End"), 0x01000011),
        (0x24, k("Home"), 0x01000010), (0x25, k("Left"), 0x01000012), (0x26, k("Up"), 0x01000013),
        (0x27, k("Right"), 0x01000014), (0x28, k("Down"), 0x01000015), (0x2C, k("Print"), 0x01000009),
        (0x2D, k("Ins"), 0x01000006), (0x2E, k("Del"), 0x01000007), (0x6A, raw("Num *"), 0x2A),
        (0x6B, raw("Num +"), 0x2B), (0x6D, raw("Num -"), 0x2D), (0x6E, raw("Num ,"), 0x2C), (0x6F, raw("Num /"), 0x2F),
        (0xAD, k("Mute"), 0x01000071), (0xAE, k("Volume Down"), 0x01000070), (0xAF, k("Volume Up"), 0x01000072),
        (0xB0, k("Next Track"), 0x01000083), (0xB1, k("Previous Track"), 0x01000082), (0xB2, k("Stop"), 0x01000081),
        (0xB3, raw("Play/Pause"), 0x01000086),
    ];
    for i in 0..10 {
        v.push((0x60 + i, format!("Num {i}"), 0x30 + i));
    }
    for i in 0..24 {
        v.push((0x70 + i, format!("F{}", i + 1), 0x01000030 + i));
    }
    v
}

fn named(vk: i64) -> Option<(String, i64)> {
    named_keys().into_iter().find(|(v, _, _)| *v == vk).map(|(_, n, q)| (n, q))
}

/// Eingabenamen -> VK (Anzeigenamen der aktiven Sprache, englische und frühere deutsche Namen).
fn name_to_vk() -> Vec<(String, i64)> {
    let mut out: Vec<(String, i64)> = named_keys().into_iter().map(|(vk, n, _)| (n.to_lowercase(), vk)).collect();
    let extra: Vec<(String, i64)> = [
        ("backspace", 0x08), ("return", 0x0D), ("eingabe", 0x0D), ("escape", 0x1B), ("space", 0x20), ("leer", 0x20),
        ("pageup", 0x21), ("pgup", 0x21), ("bildauf", 0x21), ("pagedown", 0x22), ("pgdn", 0x22), ("bildab", 0x22),
        ("end", 0x23), ("home", 0x24), ("left", 0x25), ("up", 0x26), ("right", 0x27), ("down", 0x28), ("print", 0x2C),
        ("insert", 0x2D), ("ins", 0x2D), ("delete", 0x2E), ("del", 0x2E), ("mute", 0xAD), ("volumedown", 0xAE),
        ("volumeup", 0xAF), ("playpause", 0xB3), ("next", 0xB0), ("prev", 0xB1),
        ("esc", 0x1B), ("tab", 0x09), ("enter", 0x0D), ("pause", 0x13), ("rücktaste", 0x08), ("leertaste", 0x20),
        ("bild↑", 0x21), ("bild↓", 0x22), ("ende", 0x23), ("pos1", 0x24), ("links", 0x25), ("hoch", 0x26),
        ("rechts", 0x27), ("runter", 0x28), ("druck", 0x2C), ("einfg", 0x2D), ("entf", 0x2E), ("stumm", 0xAD),
    ]
    .iter()
    .map(|(k, v)| (k.to_string(), *v))
    .chain([(gettext("volume down"), 0xAE), (gettext("volume up"), 0xAF)])
    .chain(
        [("nächster titel", 0xB0), ("vorheriger titel", 0xB1), ("stopp", 0xB2), ("stop", 0xB2), ("next track", 0xB0),
         ("previous track", 0xB1), ("play/pause", 0xB3)]
        .iter()
        .map(|(k, v)| (k.to_string(), *v)),
    )
    .collect();
    for (k, v) in extra {
        match out.iter_mut().find(|(n, _)| *n == k) {
            Some(e) => e.1 = v,
            None => out.push((k, v)),
        }
    }
    out
}

fn is_digit_or_letter(vk: i64) -> bool {
    (0x30..=0x39).contains(&vk) || (0x41..=0x5A).contains(&vk)
}

/// Hotkey aus Modifier-Bits, Windows-VK und (optional) Qt-Keycode.
pub fn make_hotkey(mods: u32, vk: i64, qt: Option<i64>) -> Value {
    let qt = qt.unwrap_or_else(|| {
        if let Some((_, q)) = named(vk) {
            q
        } else if is_digit_or_letter(vk) {
            vk
        } else {
            EMPTY_QT
        }
    });
    let native = if vk >= 0 { vk } else if qt != EMPTY_QT { qt } else { -1 };
    json!({"KeyCmd": mods & MOD_META != 0, "KeyCtrl": mods & MOD_CTRL != 0, "KeyModifiers": mods,
           "KeyOption": mods & MOD_ALT != 0, "KeyShift": mods & MOD_SHIFT != 0,
           "NativeCode": native, "QTKeyCode": qt, "VKeyCode": vk})
}

/// Hotkey ist nicht belegt.
pub fn hotkey_is_empty(hk: &Value) -> bool {
    let Some(o) = hk.as_object().filter(|o| !o.is_empty()) else { return true };
    let qt_empty = match o.get("QTKeyCode") {
        None | Some(Value::Null) => true,
        Some(v) => v.as_i64().map(|q| q == EMPTY_QT || q == 0).unwrap_or(false),
    };
    let vk_empty = match o.get("VKeyCode") {
        None | Some(Value::Null) => true,
        Some(v) => v.as_i64() == Some(-1),
    };
    let no_mods = match o.get("KeyModifiers") {
        None | Some(Value::Null) => true,
        Some(v) => v.as_i64() == Some(0) || v.as_bool() == Some(false),
    };
    qt_empty && vk_empty && no_mods
}

fn mods_of(hk: &Map<String, Value>) -> u32 {
    if let Some(m) = hk.get("KeyModifiers").filter(|v| !v.is_null()) {
        return m.as_u64().unwrap_or(0) as u32;
    }
    let b = |k: &str| hk.get(k).and_then(Value::as_bool).unwrap_or(false);
    (if b("KeyShift") { MOD_SHIFT } else { 0 })
        | (if b("KeyCtrl") { MOD_CTRL } else { 0 })
        | (if b("KeyOption") { MOD_ALT } else { 0 })
        | (if b("KeyCmd") { MOD_META } else { 0 })
}

/// Elgato-Hotkey -> „Strg+Umschalt+T“ (leer, wenn nicht belegt).
pub fn format_hotkey(hk: &Value) -> String {
    if hotkey_is_empty(hk) {
        return String::new();
    }
    let Some(o) = hk.as_object() else { return String::new() };
    let mods = mods_of(o);
    let mut parts: Vec<String> = mod_names().into_iter().filter(|(b, _)| mods & b != 0).map(|(_, n)| n).collect();
    let vk = o.get("VKeyCode").and_then(Value::as_i64).unwrap_or(-1);
    let qt = o.get("QTKeyCode").and_then(Value::as_i64).unwrap_or(EMPTY_QT);
    let mut key = String::new();
    if vk >= 0 {
        key = if let Some((n, _)) = named(vk) {
            n
        } else if is_digit_or_letter(vk) {
            char::from(vk as u8).to_string()
        } else {
            format!("VK{vk:02X}")
        };
    } else if qt != EMPTY_QT {
        key = named_keys()
            .into_iter()
            .find(|(_, _, q)| *q == qt && *q >= 0x01000000)
            .map(|(_, n, _)| n)
            .unwrap_or_else(|| {
                if qt > 0x20 && qt < 0x110000 {
                    char::from_u32(qt as u32).map(|c| c.to_uppercase().collect()).unwrap_or_default()
                } else {
                    String::new()
                }
            });
    }
    if !key.is_empty() {
        parts.push(key);
    }
    parts.join("+")
}

/// Gleich wie [`format_hotkey`]: Beschriftung eines Hotkeys („Ctrl+C“).
pub fn hotkey_label(hk: &Value) -> String {
    format_hotkey(hk)
}

/// „Strg+Umschalt+T“ -> Elgato-Hotkey. Leerer Text -> leerer Hotkey; ungültig -> Fehlermeldung.
pub fn parse_hotkey(text: &str) -> Result<Value, String> {
    let text = text.trim();
    if text.is_empty() {
        return Ok(empty_hotkey());
    }
    // „+“ als Taste: „Strg++“ bzw. „Num +“
    let mut marked = text.replace("++", "+PLUS");
    if marked.to_lowercase().ends_with("num +") {
        marked.pop();
        marked.push_str("PLUS");
    }
    let tokens: Vec<String> = marked.split('+').map(|t| t.trim().replace("PLUS", "+")).collect();
    let (mut mods, mut vk, mut qt): (u32, i64, Option<i64>) = (0, -1, None);
    let last = tokens.len() - 1;
    let table = name_to_vk();
    for (i, tok) in tokens.iter().enumerate() {
        let low = tok.to_lowercase();
        let alias = mod_alias(&low);
        if let Some(m) = alias.filter(|_| i < last) {
            mods |= m;
            continue;
        }
        if i != last {
            return Err(crate::tr!("Unknown modifier key: {key}", key = tok));
        }
        let compact = low.replace(' ', "");
        let mut chars = tok.chars();
        let single = match (chars.next(), chars.next()) {
            (Some(c), None) => Some(c),
            _ => None,
        };
        if let Some(m) = alias {
            mods |= m;
        } else if let Some((_, v)) = table.iter().find(|(k, _)| k.replace(' ', "") == compact) {
            vk = *v;
        } else if let Some(c) = single.filter(|c| c.is_ascii_alphanumeric()) {
            vk = c.to_ascii_uppercase() as i64;
        } else if let Some(c) = single {
            qt = Some(c as i64);
        } else {
            return Err(crate::tr!("Unknown key: {key}", key = tok));
        }
    }
    if vk < 0 && qt.is_none() && mods == 0 {
        return Err(gettext("No key specified"));
    }
    let qt = qt.or(if vk >= 0 { None } else { Some(EMPTY_QT) });
    Ok(make_hotkey(mods, vk, qt))
}

/// X11-Keysym (auch GDK-Keyval-Name) -> Windows-VK.
fn keysym_vk(keysym: &str) -> Option<i64> {
    const T: &[(&str, i64)] = &[
        ("BackSpace", 0x08), ("Tab", 0x09), ("ISO_Left_Tab", 0x09), ("Return", 0x0D), ("KP_Enter", 0x0D),
        ("Pause", 0x13), ("Escape", 0x1B), ("space", 0x20), ("Prior", 0x21), ("Page_Up", 0x21), ("Next", 0x22),
        ("Page_Down", 0x22), ("End", 0x23), ("Home", 0x24), ("Left", 0x25), ("Up", 0x26), ("Right", 0x27),
        ("Down", 0x28), ("Print", 0x2C), ("Insert", 0x2D), ("Delete", 0x2E), ("KP_Multiply", 0x6A),
        ("KP_Add", 0x6B), ("KP_Subtract", 0x6D), ("KP_Decimal", 0x6E), ("KP_Separator", 0x6E), ("KP_Divide", 0x6F),
        ("XF86AudioMute", 0xAD), ("XF86AudioLowerVolume", 0xAE), ("XF86AudioRaiseVolume", 0xAF),
        ("XF86AudioNext", 0xB0), ("XF86AudioPrev", 0xB1), ("XF86AudioStop", 0xB2), ("XF86AudioPlay", 0xB3),
    ];
    if let Some((_, v)) = T.iter().find(|(k, _)| *k == keysym) {
        return Some(*v);
    }
    if let Some(d) = keysym.strip_prefix("KP_").and_then(|d| d.parse::<i64>().ok()).filter(|d| (0..10).contains(d)) {
        return Some(0x60 + d);
    }
    keysym.strip_prefix('F').and_then(|n| n.parse::<i64>().ok()).filter(|n| (1..=24).contains(n)).map(|n| 0x70 + n - 1)
}

/// Keysyms reiner Sondertasten (lösen allein keinen Hotkey aus).
pub const MODIFIER_KEYSYMS: &[&str] = &[
    "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Meta_L", "Meta_R", "Super_L", "Super_R",
    "ISO_Level3_Shift", "Caps_Lock", "Num_Lock", "Hyper_L", "Hyper_R",
];

/// X11-Modifier-Zustand (Shift 0x1, Control 0x4, Mod1 0x8, Mod4 0x40) -> MOD_*-Bits.
pub fn mods_from_x11_state(state: u32) -> u32 {
    let mut m = 0;
    if state & 0x0001 != 0 {
        m |= MOD_SHIFT;
    }
    if state & 0x0004 != 0 {
        m |= MOD_CTRL;
    }
    if state & 0x0008 != 0 {
        m |= MOD_ALT;
    }
    if state & 0x0040 != 0 {
        m |= MOD_META;
    }
    m
}

/// GDK-ModifierType (Shift 1<<0, Control 1<<2, Alt 1<<3, Super 1<<26, Meta 1<<28) -> MOD_*-Bits.
pub fn mods_from_gdk_state(state: u32) -> u32 {
    let mut m = mods_from_x11_state(state & 0x000D);
    if state & ((1 << 26) | (1 << 28)) != 0 {
        m |= MOD_META;
    }
    m
}

/// Tastendruck -> Elgato-Hotkey (None bei reinen Sondertasten). `keysym` ist der X11/GDK-Name,
/// `ch` das erzeugte Zeichen, `mods` MOD_*-Bits, `keycode` der X11-Keycode (0 = unbekannt).
pub fn hotkey_from_keysym(keysym: &str, ch: &str, mods: u32, keycode: u32) -> Option<Value> {
    if MODIFIER_KEYSYMS.contains(&keysym) {
        return None;
    }
    if let Some(vk) = keysym_vk(keysym) {
        return Some(make_hotkey(mods, vk, None));
    }
    let mut it = keysym.chars();
    if let (Some(c), None) = (it.next(), it.next()) {
        if c.is_ascii_alphanumeric() {
            return Some(make_hotkey(mods, c.to_ascii_uppercase() as i64, None));
        }
    }
    // Ziffernreihe mit Umschalt (Keysym „exclam“ …): X11-Keycodes 10–19 = 1…0
    if (10..=19).contains(&keycode) {
        return Some(make_hotkey(mods, 0x30 + ((keycode as i64 - 9) % 10), None));
    }
    let mut it = ch.chars();
    if let (Some(c), None) = (it.next(), it.next()) {
        if !c.is_control() && !c.is_whitespace() {
            if c.is_ascii_alphabetic() {
                return Some(make_hotkey(mods, c.to_ascii_uppercase() as i64, None));
            }
            return Some(make_hotkey(mods, -1, Some(c as i64)));
        }
    }
    None
}

/// Wie `hotkey_from_tk` der Python-Version: Tk-/X11-Zustand statt MOD_*-Bits.
pub fn hotkey_from_tk(keysym: &str, ch: &str, state: u32, keycode: u32) -> Option<Value> {
    hotkey_from_keysym(keysym, ch, mods_from_x11_state(state), keycode)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn all_texts() -> Vec<Text> {
        let mut out = Vec::new();
        for a in catalog() {
            out.extend([a.name.clone(), a.category.clone(), a.description.clone()]);
            let fields = a.fields.iter().chain(a.dial_fields.iter().flatten());
            for f in fields {
                out.extend([f.label.clone(), f.hint.clone()]);
                if let Choices::Static(l) = &f.choices {
                    out.extend(l.iter().map(|c| c.label.clone()));
                }
            }
            if let Some(c) = &a.children {
                out.extend(c.labels.iter().cloned());
            }
        }
        out
    }

    #[test]
    fn all_msgids_exist_in_german_catalog() {
        let po = crate::i18n::parse_po(include_str!("../../../../freestream/locale/de.po"));
        let ids: std::collections::HashSet<&str> =
            po.iter().filter(|e| e.msgctxt.is_none() && !e.msgstr.iter().all(|s| s.is_empty())).map(|e| e.msgid.as_str()).collect();
        let missing: Vec<String> =
            all_texts().into_iter().filter_map(|t| match t { Text::Tr(s) if !s.is_empty() && !ids.contains(s.as_str()) => Some(s), _ => None }).collect();
        assert!(missing.is_empty(), "fehlende msgids: {missing:?}");
        let ctx: std::collections::HashSet<&str> =
            po.iter().filter(|e| e.msgctxt.as_deref() == Some("key name")).map(|e| e.msgid.as_str()).collect();
        for k in ["Ctrl", "Shift", "Backspace", "Space", "PgUp", "PgDn", "End", "Home", "Left", "Up", "Right", "Down", "Print",
                  "Ins", "Del", "Mute", "Volume Down", "Volume Up", "Next Track", "Previous Track", "Stop"] {
            assert!(ctx.contains(k), "key name {k}");
        }
    }

    #[test]
    fn catalog_structure() {
        let c = catalog();
        assert!(c.len() >= 60);
        let mut seen = std::collections::HashSet::new();
        for a in c {
            assert!(seen.insert(a.uuid.as_str()), "doppelt: {}", a.uuid);
            assert!(!a.controllers.is_empty());
        }
        assert!(by_uuid(FOLDER_UUID).is_some() && by_uuid(BACK_UUID).is_some());
        let cats: Vec<String> = categories(true).iter().map(|c| c.id().to_string()).collect();
        assert_eq!(
            cats,
            ["System", "Navigation", "FreeDeck", "Multi Action", "Soundboard", "Audio", "OBS Studio", "Spotify & Media",
             "Discord", "Home Assistant", "FreeWave", CLOCK_CAT, SYSMON_CAT]
        );
        assert!(!categories(false).iter().any(|c| c.id() == FREEWAVE_CATEGORY));
        assert!(catalog_for(false).len() < catalog_for(true).len());
        let hk = by_uuid("com.elgato.streamdeck.system.hotkey").unwrap();
        assert_eq!(hk.fields_for(KEY).len(), 1);
        assert_eq!(hk.fields_for(DIAL).len(), 4);
        assert_eq!(hk.fields_for(DIAL)[3].hotkey_index(), Some(3));
        let wheel = by_uuid("com.elgato.streamdeck.system.mouse-wheel").unwrap();
        assert!(wheel.supports(DIAL) && !wheel.supports(KEY));
        let mix = by_uuid("com.lindeck.mixer.channel").unwrap();
        assert_eq!(mix.fields[1].choices, Choices::Dynamic(DYN_MIXER_MIXES));
        let ha = by_uuid("com.lindeck.homeassistant.scene").unwrap();
        assert_eq!(ha.fields[0].domains.as_deref(), Some(&["scene".to_string(), "script".to_string()][..]));
        let delay = by_uuid("com.elgato.streamdeck.multiactions.delay").unwrap();
        assert_eq!((delay.fields[0].min, delay.fields[0].max), (0, 600000));
        let media = by_uuid("com.elgato.streamdeck.system.multimedia").unwrap();
        assert_eq!(media.fields[0].resolved_choices()[0].0, json!(1));
    }

    #[test]
    fn new_actions_in_elgato_format() {
        let a = new_action("com.elgato.streamdeck.system.hotkey");
        assert_eq!(a["Settings"]["Hotkeys"].as_array().unwrap().len(), 4);
        assert_eq!(a["States"].as_array().unwrap().len(), 1);
        assert_eq!(a["States"][0]["TitleAlignment"], "bottom");
        assert_eq!(a["Plugin"]["UUID"], "com.elgato.streamdeck.system.hotkey");
        assert!(a["Resources"].is_null() && a["State"] == 0);
        let keys: Vec<&str> = a.keys().map(String::as_str).collect();
        assert_eq!(keys, ["ActionID", "LinkedTitle", "Name", "Plugin", "Resources", "Settings", "State", "States", "UUID"]);
        let r = new_action("com.elgato.streamdeck.multiactions.routine");
        assert!(r["Settings"].get("Actions").is_none());
        assert_eq!(r["Actions"], json!([{"Actions": []}]));
        assert_eq!(new_action("com.elgato.streamdeck.keys.logic")["Actions"], json!([{}, {}, {}]));
        assert_eq!(new_action("com.lindeck.obs.record")["States"].as_array().unwrap().len(), 2);
        let v = new_action("com.elgato.volume-controller.output-device-control");
        assert_eq!(v["Plugin"]["UUID"], "com.elgato.volume-controller");
        let u = new_action("com.example.plugin.thing");
        assert_eq!(u["Name"], "thing");
        assert_eq!(u["Plugin"]["UUID"], "com.example.plugin.thing");
        assert_ne!(new_action(FOLDER_UUID)["ActionID"], new_action(FOLDER_UUID)["ActionID"]);
    }

    #[test]
    fn display_names() {
        let mut a = Map::new();
        assert_eq!(display_name(&a), "");
        a.insert("UUID".into(), json!("com.example.x.do-it"));
        assert_eq!(display_name(&a), "do-it");
        a.insert("Name".into(), json!("Mach"));
        assert_eq!(display_name(&a), "Mach");
        a.insert("UUID".into(), json!("com.elgato.streamdeck.system.hotkey"));
        assert_eq!(display_name(&a), "Hotkey");
    }

    #[test]
    fn child_lists_normalize() {
        let mut m = new_action("com.elgato.streamdeck.multiactions.routine2");
        m.insert("Actions".into(), json!([new_action("com.elgato.streamdeck.system.text")]));
        let g = child_lists(&mut m).unwrap();
        assert_eq!(g.len(), 2);
        assert_eq!(g[0]["Actions"].as_array().unwrap().len(), 1);
        assert_eq!(g[1]["Actions"], json!([]));
        let mut l = new_action("com.elgato.streamdeck.keys.logic");
        l.insert("Actions".into(), json!([]));
        assert_eq!(child_lists(&mut l).unwrap().len(), 3);
        let mut plain = new_action("com.elgato.streamdeck.system.text");
        assert!(child_lists(&mut plain).is_none());
    }

    #[test]
    fn default_state_uses_title_defaults() {
        assert_eq!(default_state()["FontSize"], 12);
        let keys: Vec<String> = default_state().keys().cloned().collect();
        set_title_defaults(json!({"FontSize": 16, "Title": "nein", "TitleColor": "#ff0000"}).as_object().unwrap());
        let st = default_state();
        assert_eq!(st["FontSize"], 16);
        assert_eq!(st["TitleColor"], "#ff0000");
        assert_eq!(st["Title"], "");
        assert_eq!(st.keys().cloned().collect::<Vec<_>>(), keys); // Reihenfolge bleibt
        set_title_defaults(&Map::new());
        assert_eq!(default_state()["FontSize"], 12);
    }

    #[test]
    fn parse_hotkeys() {
        let hk = parse_hotkey("ctrl+shift+t").unwrap();
        assert_eq!(hk["VKeyCode"], 0x54);
        assert_eq!(hk["QTKeyCode"], 0x54);
        assert_eq!(hk["KeyModifiers"], MOD_CTRL | MOD_SHIFT);
        assert_eq!(hk["KeyCtrl"], true);
        assert_eq!(hk["KeyShift"], true);
        assert_eq!(hk["KeyOption"], false);
        assert_eq!(hk, parse_hotkey("Strg+Umschalt+T").unwrap());
        assert_eq!(parse_hotkey("Strg+Num +").unwrap()["VKeyCode"], 0x6B);
        assert_eq!(parse_hotkey("Ctrl++").unwrap()["QTKeyCode"], '+' as i64);
        assert_eq!(parse_hotkey("Alt+F4").unwrap()["VKeyCode"], 0x73);
        assert_eq!(parse_hotkey("F13").unwrap()["QTKeyCode"], 0x01000030 + 12);
        assert_eq!(parse_hotkey("Strg+Entf").unwrap()["VKeyCode"], 0x2E);
        assert_eq!(parse_hotkey("Super+Page Up").unwrap()["VKeyCode"], 0x21);
        assert_eq!(parse_hotkey("Strg+#").unwrap()["QTKeyCode"], '#' as i64);
        assert_eq!(parse_hotkey("Strg+#").unwrap()["NativeCode"], '#' as i64);
        let only_mod = parse_hotkey("Shift").unwrap();
        assert_eq!((only_mod["KeyModifiers"].as_i64(), only_mod["VKeyCode"].as_i64()), (Some(1), Some(-1)));
        assert!(hotkey_is_empty(&parse_hotkey("").unwrap()));
        assert!(hotkey_is_empty(&empty_hotkey()));
        assert!(!hotkey_is_empty(&hk));
        assert!(parse_hotkey("Strg+Gibtsnicht").is_err());
        assert!(parse_hotkey("Gibts+T").is_err());
        assert_eq!(format_hotkey(&empty_hotkey()), "");
        assert_eq!(format_hotkey(&json!({"VKeyCode": 0x07, "QTKeyCode": 0})), "VK07");
    }

    #[test]
    fn hotkeys_from_keysyms() {
        let hk = hotkey_from_tk("t", "\x14", 0x4 | 0x1, 0).unwrap();
        assert_eq!(hk, parse_hotkey("ctrl+shift+t").unwrap());
        assert!(hotkey_from_tk("Control_L", "", 0x4, 0).is_none());
        let one = hotkey_from_tk("exclam", "!", 0x1, 10).unwrap();
        assert_eq!((one["VKeyCode"].as_i64(), one["KeyShift"].as_bool()), (Some(0x31), Some(true)));
        assert_eq!(hotkey_from_keysym("F5", "", 0, 0).unwrap()["VKeyCode"], 0x74);
        assert_eq!(hotkey_from_keysym("KP_3", "3", 0, 0).unwrap()["VKeyCode"], 0x63);
        assert_eq!(hotkey_from_keysym("numbersign", "#", MOD_CTRL, 0).unwrap()["QTKeyCode"], '#' as i64);
        assert_eq!(mods_from_gdk_state(1 | 4 | (1 << 26)), MOD_SHIFT | MOD_CTRL | MOD_META);
    }

    #[test]
    fn dynamic_choices() {
        let conf = json!({"mixes": [{"id": "monitor", "name": "Kopfhörer"}, {"id": "rec", "name": ""}, {"name": "kaputt"}],
                          "scenes": [{"id": "s1", "name": "Abend"}, {"id": "s2"}]});
        let conf = conf.as_object().unwrap();
        let sel = resolve_choices_in(DYN_MIXER_MIXSELECT, conf);
        let ids: Vec<&str> = sel.iter().map(|(v, _)| v.as_str().unwrap()).collect();
        assert_eq!(ids, ["both", "monitor", "stream", "rec", "cycle"]);
        assert_eq!(sel[1].1, "Kopfhörer");
        assert_eq!(sel[3].1, "rec");
        let mixes = resolve_choices_in(DYN_MIXER_MIXES, conf);
        assert_eq!(mixes.first().unwrap().0, "both");
        assert_eq!(mixes.last().unwrap().0, "selected");
        assert_eq!(mixes.len(), 5);
        let scenes = resolve_choices_in(DYN_MIXER_SCENES, conf);
        assert_eq!(scenes, vec![(json!("s1"), "Abend".to_string()), (json!("s2"), "s2".to_string())]);
        let none = resolve_choices_in(DYN_MIXER_SCENES, &Map::new());
        assert_eq!(none.len(), 1);
        assert_eq!(none[0].0, "");
        assert!(resolve_choices_in(DYN_HA_ENTITIES, conf).is_empty());
    }

    #[test]
    fn looks() {
        assert_eq!(action_look("com.elgato.streamdeck.system.hotkey", "System"), ("keyboard", "#64748b"));
        assert_eq!(action_look("com.lindeck.obs.replay", "OBS Studio"), ("undo", "#ef4444"));
        assert_eq!(action_look("com.lindeck.obs.unknown", "OBS Studio"), ("record", "#ef4444"));
        assert_eq!(action_look("com.foo.bar", "Plugin: Foo"), ("puzzle", "#f59e0b"));
        assert_eq!(action_look("com.lindeck.sysmon.cpu", SYSMON_CAT), ("cpu", "#14b8a6"));
        for a in catalog() {
            assert_ne!(category_look(a.category.id()).0, "puzzle", "{}", a.uuid);
        }
    }
}
