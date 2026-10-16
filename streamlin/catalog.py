"""Katalog der eingebauten Aktionen für den Editor.

Beschreibt für jede Elgato-Aktion Anzeigename, Kategorie, Standardwerte und
die Felder, die im Editor bearbeitet werden können. Neue Aktionen werden im
Originalformat der Elgato-App angelegt, damit die Profile austauschbar bleiben.

Außerdem: Umwandlung von Hotkeys zwischen Elgato-Format (Windows-VK-Code,
Qt-Keycode, Modifier-Bits) und lesbarem Text wie "Strg+Umschalt+T".
"""
import copy
import uuid as uuidlib
from dataclasses import dataclass, field

from . import parts
from .i18n import _, pgettext

KEY, DIAL = "key", "dial"


@dataclass
class Field:
    kind: str            # text, multiline, int, bool, choice, file, hotkey, page, profile, entity
    key: str             # Schlüssel in "Settings" (bei hotkey: Index in "Hotkeys")
    label: str
    options: list = None  # choice: [(Wert, Anzeige), ...] oder Funktion, die diese Liste liefert
    min: int = 0
    max: int = 100000
    hint: str = ""


@dataclass
class ActionType:
    uuid: str
    name: str
    category: str
    settings: dict = field(default_factory=dict)
    states: int = 1
    controllers: tuple = (KEY,)
    fields: list = field(default_factory=list)
    dial_fields: list = None
    children: tuple = None   # ("groups", [Beschriftungen]) oder ("slots", [Beschriftungen])
    description: str = ""
    plugin: str = ""

    def fields_for(self, slot_kind):
        if slot_kind == DIAL and self.dial_fields is not None:
            return self.dial_fields
        return self.fields


EMPTY_QT = 33554431


def empty_hotkey():
    return {"KeyCmd": False, "KeyCtrl": False, "KeyModifiers": 0, "KeyOption": False, "KeyShift": False,
            "NativeCode": -1, "QTKeyCode": EMPTY_QT, "VKeyCode": -1}


MEDIA_CHOICES = [(1, _("Play / Pause")), (2, _("Next track")), (0, _("Previous track")), (3, _("Stop")),
                 (4, _("Mute")), (5, _("Volume up")), (6, _("Volume down")), (13, _("Fast forward")), (14, _("Rewind")),
                 (18, _("Volume (dial)"))]
BRIGHTNESS_CHOICES = [(0, _("Brighter")), (1, _("Darker")), (2, _("Minimum")), (3, _("Low")), (4, _("Medium")),
                      (5, _("High")), (6, _("Maximum"))]
AUDIO_MODES = [(0, _("Play / Overlap")), (1, _("Play / Stop")), (2, _("Play / Restart")),
               (3, _("Loop / Stop"))]
VOLUME_MODES = [("mute", _("Toggle mute")), ("up", _("Volume up")), ("down", _("Volume down")),
                ("set", _("Set volume"))]

_HK_KEY = [Field("hotkey", 0, _("Key combination"))]
_HK_DIAL = [Field("hotkey", 0, _("Rotate left")), Field("hotkey", 1, _("Rotate right")),
            Field("hotkey", 2, _("Press")), Field("hotkey", 3, _("Touch"))]

CATALOG = [
    # ------------------------------------------------------------ System
    ActionType("com.elgato.streamdeck.system.hotkey", "Hotkey", _("System"),
               {"Coalesce": True, "Hotkeys": [empty_hotkey() for _i in range(4)]},
               controllers=(KEY, DIAL), fields=_HK_KEY, dial_fields=_HK_DIAL,
               description=_("Sends a key combination")),
    ActionType("com.elgato.streamdeck.system.hotkeyswitch", "Hotkey Switch", _("System"),
               {"Hotkeys": [empty_hotkey(), empty_hotkey()]}, states=2,
               fields=[Field("hotkey", 0, _("State 1")), Field("hotkey", 1, _("State 2"))],
               description=_("Switches between two key combinations on each press")),
    ActionType("com.elgato.streamdeck.system.text", _("Text"), _("System"),
               {"pastedText": "", "isSendingEnter": False, "isTypingMode": False},
               fields=[Field("multiline", "pastedText", _("Text")),
                       Field("bool", "isSendingEnter", _("Send Enter afterwards")),
                       Field("bool", "isTypingMode", _("Type instead of pasting"))],
               description=_("Pastes a text")),
    ActionType("com.elgato.streamdeck.system.open", _("Open"), _("System"), {"path": ""},
               fields=[Field("file", "path", _("File, folder or URL"))],
               description=_("Opens a file, folder or web address")),
    ActionType("com.elgato.streamdeck.system.openapp", _("Open Application"), _("System"),
               {"exec": "", "args": "", "long_press": "none", "bring_to_front": True},
               fields=[Field("file", "exec", _("Program / command"), hint=_("e.g. firefox or flatpak run …")),
                       Field("text", "args", _("Arguments")),
                       Field("bool", "bring_to_front", _("Bring a running program to the front")),
                       Field("choice", "long_press", _("Long press"),
                             [("none", _("nothing")), ("quit", _("Quit program"))])],
               description=_("Launches a program")),
    ActionType("com.elgato.streamdeck.system.close", _("Close Application"), _("System"),
               {"exec": "", "app_name": ""},
               fields=[Field("file", "exec", _("Program")), Field("text", "app_name", _("Process name"))],
               description=_("Quits a program")),
    ActionType("com.elgato.streamdeck.system.website", "Website", _("System"),
               {"path": "", "browser": ""},
               fields=[Field("text", "path", _("Address"), hint="https://…"),
                       Field("text", "browser", _("Browser (empty = default)"))],
               description=_("Opens a website")),
    ActionType("com.elgato.streamdeck.system.multimedia", "Multimedia", _("System"), {"actionIdx": 1},
               controllers=(KEY, DIAL), fields=[Field("choice", "actionIdx", _("Function"), MEDIA_CHOICES)],
               description=_("Controls playback and volume")),
    ActionType("com.elgato.streamdeck.system.timer", "Timer", _("System"), {"duration": 300, "alarm": ""},
               fields=[Field("int", "duration", _("Duration (seconds)"), min=1, max=86400),
                       Field("file", "alarm", _("Alarm sound (optional)"))],
               description=_("Countdown; long press resets")),
    ActionType("com.elgato.streamdeck.system.mouse-wheel", _("Mouse Wheel"), _("System"),
               {"horizontal": False, "invert": False}, controllers=(DIAL,),
               fields=[Field("bool", "horizontal", _("Scroll horizontally")), Field("bool", "invert", _("Invert"))],
               description=_("Scrolls with the dial")),
    # -------------------------------------------------------- Navigation
    ActionType("com.elgato.streamdeck.profile.openchild", _("Create Folder"), _("Navigation"), {"ProfileUUID": ""},
               description=_("Sub-page with its own keys")),
    ActionType("com.elgato.streamdeck.profile.backtoparent", _("Parent Folder"), _("Navigation"),
               description=_("Goes back out of a folder")),
    ActionType("com.elgato.streamdeck.page.next", _("Next Page"), _("Navigation")),
    ActionType("com.elgato.streamdeck.page.previous", _("Previous Page"), _("Navigation")),
    ActionType("com.elgato.streamdeck.page.goto", _("Go to Page"), _("Navigation"), {"PageIndex": 1},
               fields=[Field("page", "PageIndex", _("Page"))]),
    ActionType("com.elgato.streamdeck.page.indicator", _("Page Indicator"), _("Navigation")),
    ActionType("com.elgato.streamdeck.profile.rotate", _("Switch Profile"), _("Navigation"), {"ProfileUUID": ""},
               fields=[Field("profile", "ProfileUUID", _("Profile"))]),
    # --------------------------------------------------------- Stream Deck
    ActionType("com.elgato.streamdeck.system.keybrightness", _("Brightness"), "Stream Deck", {"actionIdx": 0},
               controllers=(KEY, DIAL), fields=[Field("choice", "actionIdx", _("Function"), BRIGHTNESS_CHOICES)]),
    ActionType("com.elgato.streamdeck.system.sleep", "Sleep", "Stream Deck",
               description=_("Turns the display off")),
    ActionType("com.elgato.streamdeck.system.digitaltime", _("Time"), "Stream Deck",
               {"24HourFormat": True, "theme": "dark"},
               fields=[Field("bool", "24HourFormat", _("24-hour format")),
                       Field("choice", "theme", _("Appearance"), [("dark", _("dark")), ("light", _("light"))])]),
    # -------------------------------------------------------- Multi Action
    ActionType("com.elgato.streamdeck.multiactions.routine", "Multi Action", "Multi Action",
               {"Actions": None}, children=("groups", [_("Steps")]),
               description=_("Several actions in sequence")),
    ActionType("com.elgato.streamdeck.multiactions.routine2", "Multi Action Switch", "Multi Action",
               states=2, children=("groups", [_("State 1"), _("State 2")]),
               description=_("Alternates between two sequences")),
    ActionType("com.elgato.streamdeck.multiactions.random", _("Random Action"), "Multi Action",
               children=("groups", [_("Selection")])),
    ActionType("com.elgato.streamdeck.multiactions.delay", _("Delay"), "Multi Action", {"delay": 500},
               fields=[Field("int", "delay", _("Wait time (ms)"), min=0, max=600000)],
               description=_("Only inside a Multi Action")),
    ActionType("com.elgato.streamdeck.keys.logic", "Key Logic", "Multi Action",
               children=("slots", [_("Tap"), _("Double tap"), _("Hold")]),
               description=_("Different actions for tap, double tap and hold")),
    ActionType("com.elgato.streamdeck.keys.adaptor", "Action Trigger", "Multi Action",
               controllers=(DIAL,), children=("slots", [_("Turn left"), _("Press"), _("Turn right")])),
    ActionType("com.elgato.streamdeck.keys.stack", "Action Wheel", "Multi Action", {"CurrentIdx": 0},
               controllers=(DIAL,), children=("groups", [_("Actions")])),
    ActionType("com.elgato.streamdeck.dial.stack", "Dial Stack", "Multi Action", {"CurrentIdx": 0},
               controllers=(DIAL,), children=("groups", [_("Dial actions")])),
    # ----------------------------------------------------------- Soundboard
    ActionType("com.elgato.streamdeck.soundboard.playaudio", _("Play Audio"), "Soundboard",
               {"path": "", "actionType": 0, "volume": 100},
               fields=[Field("file", "path", _("Audio file")), Field("choice", "actionType", _("Mode"), AUDIO_MODES),
                       Field("int", "volume", _("Volume (%)"), min=0, max=100)]),
    ActionType("com.elgato.streamdeck.soundboard.stopaudioplay", _("Stop Audio"), "Soundboard"),
    # ---------------------------------------------------------------- Audio
    ActionType("com.elgato.volume-controller.output-device-control", _("Output Device"), _("Audio"),
               {"action": "mute", "volumeStep": 5, "friendlyName": ""}, controllers=(KEY, DIAL),
               fields=[Field("choice", "action", _("Key"), VOLUME_MODES),
                       Field("int", "volumeStep", _("Step size (%)"), min=1, max=50),
                       Field("text", "friendlyName", _("Device (empty = default)")),
                       Field("text", "appName", _("Only this program (optional)"))],
               plugin="com.elgato.volume-controller"),
    ActionType("com.elgato.volume-controller.input-device-control", _("Input Device"), _("Audio"),
               {"action": "mute", "volumeStep": 5, "friendlyName": ""}, controllers=(KEY, DIAL),
               fields=[Field("choice", "action", _("Key"), VOLUME_MODES),
                       Field("int", "volumeStep", _("Step size (%)"), min=1, max=50),
                       Field("text", "friendlyName", _("Device (empty = default)"))],
               plugin="com.elgato.volume-controller"),
]

_MIX_CHANNELS = [("system", _("System")), ("music", _("Music")), ("browser", _("Browser")), ("voice", _("Voice Chat")),
                 ("game", _("Game")), ("sfx", _("Effects")), ("mic", _("Microphone"))]
_MIXES = [("both", _("Monitor and Stream Mix")), ("monitor", _("Monitor Mix only")), ("stream", _("Stream Mix only"))]
_TOGGLE = [("toggle", _("toggle")), ("start", _("start")), ("stop", _("stop"))]
# Feldart "entity": options = erlaubte Domains für die Auswahl (None = alle)
HA_TOGGLE_DOMAINS = ["light", "switch", "fan", "cover", "input_boolean", "automation", "lock", "climate",
                     "media_player", "siren", "humidifier", "input_number", "number", "button", "input_button"]


def _scene_choices():
    """Mixer-Szenen aus der Linewave-Konfiguration (für das Auswahlfeld im Editor)."""
    try:
        from .config import Config
        scenes = [s for s in ((Config().get("mixer") or {}).get("scenes") or []) if isinstance(s, dict) and s.get("id")]
    except Exception:  # noqa: BLE001
        scenes = []
    return [(s["id"], s.get("name") or s["id"]) for s in scenes] or [("", _("(no scenes saved in Linewave yet)"))]


CATALOG += [
    # ------------------------------------------------------------- OBS
    ActionType("com.lindeck.obs.scene", _("Scene"), "OBS Studio", {"scene": ""}, controllers=(KEY, DIAL),
               fields=[Field("text", "scene", _("Scene"), hint=_("Name as in OBS"))],
               description=_("Switches the scene (dial: cycles through scenes)")),
    ActionType("com.lindeck.obs.record", _("Record"), "OBS Studio", {"mode": "toggle"}, states=2,
               fields=[Field("choice", "mode", _("Function"), _TOGGLE + [("pause", _("toggle pause"))])]),
    ActionType("com.lindeck.obs.stream", "Stream", "OBS Studio", {"mode": "toggle"}, states=2,
               fields=[Field("choice", "mode", _("Function"), _TOGGLE)]),
    ActionType("com.lindeck.obs.source", _("Source On/Off"), "OBS Studio", {"scene": "", "source": ""}, states=2,
               fields=[Field("text", "source", _("Source")), Field("text", "scene", _("Scene (empty = current)"))]),
    ActionType("com.lindeck.obs.audio", _("Audio Input"), "OBS Studio", {"source": "", "step_db": 1}, states=2,
               controllers=(KEY, DIAL),
               fields=[Field("text", "source", _("Audio input"), hint=_("e.g. Mic/Aux")),
                       Field("int", "step_db", _("Step per dial notch (dB)"), min=1, max=10)],
               description=_("Key: mute · Dial: volume")),
    ActionType("com.lindeck.obs.replay", _("Replay Buffer"), "OBS Studio", {"mode": "save"},
               fields=[Field("choice", "mode", _("Function"), [("save", _("Save replay")),
                                                            ("toggle", _("Buffer on/off"))])]),
    ActionType("com.lindeck.obs.virtualcam", _("Virtual Camera"), "OBS Studio", states=2),
    ActionType("com.lindeck.obs.studiomode", _("Studio Mode"), "OBS Studio", states=2),
    ActionType("com.lindeck.obs.transition", _("Run Transition"), "OBS Studio",
               description=_("Preview → Program in Studio Mode")),
    ActionType("com.lindeck.obs.screenshot", "Screenshot", "OBS Studio", {"source": ""},
               fields=[Field("text", "source", _("Source (empty = program)"))]),
    # ------------------------------------------------------------ Medien
    ActionType("com.lindeck.media.player", _("Media Player"), _("Spotify & Media"),
               {"player": "spotify", "function": "playpause"}, controllers=(KEY, DIAL),
               fields=[Field("text", "player", "Player", hint=_("spotify, vlc, firefox … (empty = active)")),
                       Field("choice", "function", _("Function"),
                             [("playpause", _("Play/Pause")), ("next", _("Next track")),
                              ("previous", _("Previous track")), ("nowplaying", _("Now playing")),
                              ("shuffle", _("Shuffle")), ("repeat", _("Repeat")),
                              ("volumeup", _("Volume up")), ("volumedown", _("Volume down")), ("stop", _("Stop"))])],
               description=_("Controls Spotify or any MPRIS player")),
    # ----------------------------------------------------------- Discord
    ActionType("com.lindeck.discord.voice", "Discord", "Discord", {"function": "mute", "channel_id": ""}, states=2,
               fields=[Field("choice", "function", _("Function"),
                             [("mute", _("Mute microphone")), ("deafen", _("Deafen")), ("pushtotalk", "Push to Talk"),
                              ("pushtomute", "Push to Mute"), ("voicechannel", _("Join voice channel")),
                              ("textchannel", _("Open text channel")), ("leave", _("Leave voice channel"))]),
                       Field("text", "channel_id", _("Channel ID (channel only)"))],
               description=_("Needs your own Discord app (see README)")),
    # ---------------------------------------------------- Home Assistant
    ActionType("com.lindeck.homeassistant.toggle", _("Toggle Entity"), "Home Assistant",
               {"entity_id": "", "show_value": True, "step": 5}, states=2, controllers=(KEY, DIAL),
               fields=[Field("entity", "entity_id", _("Entity"), HA_TOGGLE_DOMAINS),
                       Field("bool", "show_value", _("Show brightness / position")),
                       Field("int", "step", _("Dial step size (%)"), min=1, max=50)],
               description=_("Switches a light, switch, fan, cover … (dial: brightness / temperature)")),
    ActionType("com.lindeck.homeassistant.service", _("Call Service"), "Home Assistant",
               {"service": "", "entity_id": "", "data": ""},
               fields=[Field("text", "service", _("Service"), hint=_("e.g. light.turn_on")),
                       Field("entity", "entity_id", _("Entity (optional)")),
                       Field("multiline", "data", _("Service data (JSON, optional)"))],
               description=_("Calls any Home Assistant service")),
    ActionType("com.lindeck.homeassistant.scene", _("Scene / Script"), "Home Assistant", {"entity_id": ""},
               fields=[Field("entity", "entity_id", _("Scene or script"), ["scene", "script"])],
               description=_("Activates a scene or runs a script")),
    ActionType("com.lindeck.homeassistant.state", _("Show State"), "Home Assistant",
               {"entity_id": "", "attribute": "", "decimals": -1}, controllers=(KEY, DIAL),
               fields=[Field("entity", "entity_id", _("Entity")),
                       Field("text", "attribute", _("Attribute (empty = state)")),
                       Field("int", "decimals", _("Decimal places (−1 = automatic)"), min=-1, max=6)],
               description=_("Shows a sensor value with its unit, updated live")),
    # ------------------------------------------------------------- Mixer
    ActionType("com.lindeck.mixer.channel", _("Mixer Channel"), "Linewave",
               {"channel": "music", "mix": "both", "action": "mute", "step": 5}, controllers=(KEY, DIAL),
               fields=[Field("choice", "channel", _("Channel"), _MIX_CHANNELS), Field("choice", "mix", "Mix", _MIXES),
                       Field("choice", "action", _("Key"), [("mute", _("toggle mute")), ("up", _("volume up")),
                                                           ("down", _("volume down")), ("set", _("Set volume"))]),
                       Field("int", "step", _("Step size (%)"), min=1, max=50),
                       Field("int", "volume", _("Volume for “set” (%)"), min=0, max=100)],
               description=_("Wave Link replacement: channel volume per mix")),
    ActionType("com.lindeck.mixer.output", _("Monitor Output"), "Linewave", {"device": ""},
               fields=[Field("text", "device", _("Device (empty = next)"))],
               description=_("Switches the Monitor Mix between headphones and speakers")),
    ActionType("com.lindeck.mixer.scene", _("Mixer Scene"), "Linewave", {"scene": ""}, states=2,
               fields=[Field("choice", "scene", _("Scene"), _scene_choices)],
               description=_("Applies a mixer scene saved in Linewave and shows when it is active")),
    # -------------------------------------------------------- Wave-Mikro
    ActionType("com.lindeck.wavemic.control", _("Wave Microphone"), "Linewave", {"function": "mute", "step": 5},
               controllers=(KEY, DIAL),
               fields=[Field("choice", "function", _("Function"), [("mute", _("toggle mute")), ("gain", "Gain +"),
                                                               ("lowcut", _("Low Cut on/off")),
                                                               ("clipguard", _("Clipguard on/off")),
                                                               ("leds", _("LEDs on/off (Wave:3)")),
                                                               ("ledsflip", _("Invert LEDs (Wave:3)"))]),
                       Field("int", "step", _("Gain step size (%)"), min=1, max=20)],
               description=_("Gain, mute, Low Cut, Clipguard, LEDs (dial: gain)")),
]

_COLORS = [("blue", _("Blue")), ("green", _("Green")), ("orange", _("Orange")), ("red", _("Red")),
           ("purple", _("Purple")), ("teal", _("Turquoise")), ("white", _("White"))]
_COLOR = Field("choice", "color", _("Accent color"), _COLORS)
_GRAPH = Field("bool", "graph", _("Show graph"))
CLOCK_CAT, SYSMON_CAT = _("Clock & timer"), _("System monitor")

CATALOG += [
    # -------------------------------------------------------- Uhr & Timer
    ActionType("com.lindeck.clock.clock", _("Clock"), CLOCK_CAT,
               {"hour24": True, "seconds": False, "show": "both", "date_format": "weekday", "timezone": "",
                "color": "blue"}, controllers=(KEY, DIAL),
               fields=[Field("choice", "show", _("Show"), [("both", _("Time and date")), ("time", _("Time only")),
                                                          ("date", _("Date only"))]),
                       Field("choice", "date_format", _("Date format"),
                             [("weekday", _("Weekday and date")), ("dmy", "31.12.2026"), ("mdy", "12/31/2026"),
                              ("iso", "2026-12-31")]),
                       Field("bool", "hour24", _("24-hour format")), Field("bool", "seconds", _("Show seconds")),
                       Field("text", "timezone", _("Time zone"), hint=_("e.g. Europe/Berlin (empty = local)")),
                       _COLOR],
               description=_("Shows the time and date, optionally in another time zone")),
    ActionType("com.lindeck.clock.countdown", _("Countdown"), CLOCK_CAT,
               {"duration": "5:00", "label": "", "play_sound": True, "sound": "", "volume": 100,
                "repeat_sound": False, "notify": True, "dial_step": 30, "color": "orange"}, controllers=(KEY, DIAL),
               fields=[Field("text", "duration", _("Duration"), hint=_("e.g. 5:00, 90s or 1h30m")),
                       Field("text", "label", _("Label")), _COLOR,
                       Field("bool", "play_sound", _("Sound at the end")),
                       Field("bool", "notify", _("Notification at the end")),
                       Field("file", "sound", _("Alarm sound"), hint=_("empty = default sound")),
                       Field("int", "volume", _("Volume (%)"), min=0, max=100),
                       Field("bool", "repeat_sound", _("Repeat sound")),
                       Field("int", "dial_step", _("Dial step (seconds)"), min=1, max=3600)],
               description=_("Press: start/pause · long press: reset · dial: change duration")),
    ActionType("com.lindeck.clock.stopwatch", _("Stopwatch"), CLOCK_CAT, {"label": "", "color": "green"},
               controllers=(KEY, DIAL), fields=[Field("text", "label", _("Label")), _COLOR],
               description=_("Press: start/stop · long press: reset")),
    # ------------------------------------------------------ Systemmonitor
    ActionType("com.lindeck.sysmon.cpu", _("CPU Load"), SYSMON_CAT, {"graph": True, "color": "blue"},
               controllers=(KEY, DIAL), fields=[_GRAPH, _COLOR], description=_("Processor load in percent")),
    ActionType("com.lindeck.sysmon.memory", _("Memory"), SYSMON_CAT, {"graph": True, "color": "purple"},
               controllers=(KEY, DIAL), fields=[_GRAPH, _COLOR], description=_("Used RAM in percent")),
    ActionType("com.lindeck.sysmon.temperature", _("Temperature"), SYSMON_CAT,
               {"graph": True, "unit": "c", "sensor": "", "color": "orange"}, controllers=(KEY, DIAL),
               fields=[Field("choice", "unit", _("Unit"), [("c", "°C"), ("f", "°F")]), _GRAPH,
                       Field("text", "sensor", _("Sensor"), hint=_("e.g. coretemp or k10temp (empty = automatic)")),
                       _COLOR],
               description=_("Temperature from the hardware sensors")),
    ActionType("com.lindeck.sysmon.network", _("Network"), SYSMON_CAT,
               {"interface": "", "graph": True, "color": "teal"}, controllers=(KEY, DIAL),
               fields=[Field("text", "interface", _("Network interface"), hint=_("e.g. wlan0 (empty = all)")),
                       _GRAPH, _COLOR],
               description=_("Download and upload rate")),
    ActionType("com.lindeck.sysmon.battery", _("Battery"), SYSMON_CAT, {"graph": False, "battery": "",
                                                                         "color": "green"},
               controllers=(KEY, DIAL),
               fields=[_GRAPH, Field("text", "battery", _("Battery"), hint=_("e.g. BAT1 (empty = first)")), _COLOR],
               description=_("Charge level and charging state")),
    ActionType("com.lindeck.sysmon.disk", _("Disk Space"), SYSMON_CAT, {"path": "/", "graph": False,
                                                                         "color": "blue"},
               controllers=(KEY, DIAL),
               fields=[Field("text", "path", _("Folder"), hint=_("e.g. / or /home")), _GRAPH, _COLOR],
               description=_("Used space of a drive")),
]

if not parts.has_linewave():  # Mixer und Wave-Mikrofon gibt es nur mit Linewave
    CATALOG = [a for a in CATALOG if a.category != "Linewave"]
BY_UUID = {a.uuid: a for a in CATALOG}
CATEGORIES = list(dict.fromkeys(a.category for a in CATALOG))


def plugin_types():
    """Aktionen installierter Elgato-Plugins (für den Editor)."""
    from . import plugins
    out = []
    for info in plugins.installed().values():
        for a in info.actions.values():
            ctrls = tuple({"Keypad": KEY, "Encoder": DIAL}.get(c, KEY) for c in (a.get("Controllers") or ["Keypad"]))
            t = ActionType(a["UUID"], a.get("Name") or a["UUID"], f"Plugin: {info.name}",
                           states=max(1, len(a.get("States") or [])), controllers=ctrls,
                           description=a.get("Tooltip") or "", plugin=info.uuid)
            t.plugin_version = info.version
            out.append(t)
    return out


def find_type(uuid: str):
    t = BY_UUID.get(uuid)
    if t:
        return t
    try:
        return next((p for p in plugin_types() if p.uuid == uuid), None)
    except OSError:
        return None
FOLDER_UUID = "com.elgato.streamdeck.profile.openchild"
BACK_UUID = "com.elgato.streamdeck.profile.backtoparent"


def lookup(uuid: str):
    return BY_UUID.get(uuid)


def display_name(data: dict) -> str:
    if not data:
        return ""
    t = BY_UUID.get(data.get("UUID", ""))
    if t:
        return t.name
    return data.get("Name") or data.get("UUID", "").rsplit(".", 1)[-1]


def default_state():
    return {"FontFamily": "", "FontSize": 12, "FontStyle": "", "FontUnderline": False, "Image": "",
            "OutlineThickness": 2, "ShowTitle": True, "Title": "", "TitleAlignment": "bottom",
            "TitleColor": "#ffffff"}


def new_action(uuid: str) -> dict:
    """Neue Aktion im Format der Elgato-App."""
    t = find_type(uuid)
    settings = copy.deepcopy(t.settings) if t else {}
    settings.pop("Actions", None)
    data = {
        "ActionID": str(uuidlib.uuid4()),
        "LinkedTitle": True,
        "Name": t.name if t else uuid.rsplit(".", 1)[-1],
        "Plugin": {"Name": t.category.replace("Plugin: ", "") if t else "",
                   "UUID": (t.plugin if t and t.plugin else uuid), "Version": getattr(t, "plugin_version", "1.0")},
        "Resources": None,
        "Settings": settings,
        "State": 0,
        "States": [default_state() for _i in range(t.states if t else 1)],
        "UUID": uuid,
    }
    if t and t.children:
        kind, labels = t.children
        data["Actions"] = [{"Actions": []} for _i in labels] if kind == "groups" else [{} for _i in labels]
    return data


# --------------------------------------------------------------------------
# Kindaktionen (Multi Action, Key Logic, Dial Stack …)
# --------------------------------------------------------------------------
def child_lists(data: dict):
    """Gruppen einer zusammengesetzten Aktion als Liste von Listen (bearbeitbare Referenzen)."""
    t = BY_UUID.get(data.get("UUID", ""))
    if not t or not t.children:
        return []
    kind, labels = t.children
    entries = data.setdefault("Actions", [])
    if kind == "slots":
        while len(entries) < len(labels):
            entries.append({})
        return entries
    # Gruppen: entweder [{"Actions": [...]}, ...] oder (älter) direkt eine Liste von Aktionen
    if entries and all(isinstance(e, dict) and e.get("UUID") for e in entries):
        data["Actions"] = entries = [{"Actions": list(entries)}]
    while len(entries) < len(labels):
        entries.append({"Actions": []})
    for e in entries:
        e.setdefault("Actions", [])
    return entries


# --------------------------------------------------------------------------
# Hotkeys
# --------------------------------------------------------------------------
MOD_SHIFT, MOD_CTRL, MOD_ALT, MOD_META = 1, 2, 4, 8
MOD_NAMES = [(MOD_CTRL, pgettext("key name", "Ctrl")), (MOD_SHIFT, pgettext("key name", "Shift")),
             (MOD_ALT, "Alt"), (MOD_META, "Super")]
_MOD_ALIASES = {"strg": MOD_CTRL, "ctrl": MOD_CTRL, "control": MOD_CTRL, "steuerung": MOD_CTRL,
                "umschalt": MOD_SHIFT, "shift": MOD_SHIFT, "alt": MOD_ALT, "option": MOD_ALT,
                "super": MOD_META, "win": MOD_META, "meta": MOD_META, "cmd": MOD_META, "windows": MOD_META}
_MOD_ALIASES.update({"maj": MOD_SHIFT, "mayús": MOD_SHIFT, "mayus": MOD_SHIFT})
_MOD_ALIASES.update({n.lower(): m for m, n in MOD_NAMES})  # Namen der aktiven Sprache

# Windows-VK -> (Anzeigename, Qt-Keycode)
_NAMED = {
    0x08: (pgettext("key name", "Backspace"), 0x01000003), 0x09: ("Tab", 0x01000001), 0x0D: ("Enter", 0x01000004),
    0x13: ("Pause", 0x01000008), 0x1B: ("Esc", 0x01000000), 0x20: (pgettext("key name", "Space"), 0x20),
    0x21: (pgettext("key name", "PgUp"), 0x01000016), 0x22: (pgettext("key name", "PgDn"), 0x01000017),
    0x23: (pgettext("key name", "End"), 0x01000011), 0x24: (pgettext("key name", "Home"), 0x01000010),
    0x25: (pgettext("key name", "Left"), 0x01000012), 0x26: (pgettext("key name", "Up"), 0x01000013),
    0x27: (pgettext("key name", "Right"), 0x01000014), 0x28: (pgettext("key name", "Down"), 0x01000015),
    0x2C: (pgettext("key name", "Print"), 0x01000009), 0x2D: (pgettext("key name", "Ins"), 0x01000006),
    0x2E: (pgettext("key name", "Del"), 0x01000007), 0x6A: ("Num *", 0x2A), 0x6B: ("Num +", 0x2B),
    0x6D: ("Num -", 0x2D), 0x6E: ("Num ,", 0x2C), 0x6F: ("Num /", 0x2F),
    0xAD: (pgettext("key name", "Mute"), 0x01000071), 0xAE: (pgettext("key name", "Volume Down"), 0x01000070),
    0xAF: (pgettext("key name", "Volume Up"), 0x01000072), 0xB0: (pgettext("key name", "Next Track"), 0x01000083),
    0xB1: (pgettext("key name", "Previous Track"), 0x01000082), 0xB2: (pgettext("key name", "Stop"), 0x01000081),
    0xB3: ("Play/Pause", 0x01000086),
}
for _i in range(10):
    _NAMED[0x60 + _i] = (f"Num {_i}", 0x30 + _i)
for _i in range(24):
    _NAMED[0x70 + _i] = (f"F{_i + 1}", 0x01000030 + _i)

_NAME_TO_VK = {n.lower(): vk for vk, (n, _q) in _NAMED.items()}
_NAME_TO_VK.update({
    "backspace": 0x08, "return": 0x0D, "eingabe": 0x0D, "escape": 0x1B, "space": 0x20, "leer": 0x20,
    "pageup": 0x21, "pgup": 0x21, "bildauf": 0x21, "pagedown": 0x22, "pgdn": 0x22, "bildab": 0x22,
    "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "print": 0x2C,
    "insert": 0x2D, "ins": 0x2D, "delete": 0x2E, "del": 0x2E, "mute": 0xAD, "volumedown": 0xAE,
    "volumeup": 0xAF, "playpause": 0xB3, "next": 0xB0, "prev": 0xB1,
    # Anzeigenamen beider Sprachen (Englisch = Quelltext, Deutsch = frühere Anzeige)
    "esc": 0x1B, "tab": 0x09, "enter": 0x0D, "pause": 0x13, "rücktaste": 0x08, "leertaste": 0x20,
    "bild↑": 0x21, "bild↓": 0x22, "ende": 0x23, "pos1": 0x24, "links": 0x25, "hoch": 0x26,
    "rechts": 0x27, "runter": 0x28, "druck": 0x2C, "einfg": 0x2D, "entf": 0x2E, "stumm": 0xAD,
    _("volume down"): 0xAE, _("volume up"): 0xAF, "nächster titel": 0xB0, "vorheriger titel": 0xB1, "stopp": 0xB2,
    "stop": 0xB2, "next track": 0xB0, "previous track": 0xB1, "play/pause": 0xB3,
})

# Tk-Keysym -> Windows-VK
_TK_KEYSYMS = {
    "BackSpace": 0x08, "Tab": 0x09, "ISO_Left_Tab": 0x09, "Return": 0x0D, "KP_Enter": 0x0D, "Pause": 0x13,
    "Escape": 0x1B, "space": 0x20, "Prior": 0x21, "Next": 0x22, "End": 0x23, "Home": 0x24, "Left": 0x25,
    "Up": 0x26, "Right": 0x27, "Down": 0x28, "Print": 0x2C, "Insert": 0x2D, "Delete": 0x2E,
    "KP_Multiply": 0x6A, "KP_Add": 0x6B, "KP_Subtract": 0x6D, "KP_Decimal": 0x6E, "KP_Separator": 0x6E,
    "KP_Divide": 0x6F, "XF86AudioMute": 0xAD, "XF86AudioLowerVolume": 0xAE, "XF86AudioRaiseVolume": 0xAF,
    "XF86AudioNext": 0xB0, "XF86AudioPrev": 0xB1, "XF86AudioStop": 0xB2, "XF86AudioPlay": 0xB3,
}
for _i in range(10):
    _TK_KEYSYMS[f"KP_{_i}"] = 0x60 + _i
for _i in range(24):
    _TK_KEYSYMS[f"F{_i + 1}"] = 0x70 + _i

TK_MODIFIER_KEYSYMS = {"Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Meta_L", "Meta_R",
                       "Super_L", "Super_R", "ISO_Level3_Shift", "Caps_Lock", "Num_Lock", "Hyper_L", "Hyper_R"}


def make_hotkey(mods: int, vk: int = -1, qt: int = None) -> dict:
    if qt is None:
        if vk in _NAMED:
            qt = _NAMED[vk][1]
        elif 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
            qt = vk
        else:
            qt = EMPTY_QT
    return {"KeyCmd": bool(mods & MOD_META), "KeyCtrl": bool(mods & MOD_CTRL), "KeyModifiers": mods,
            "KeyOption": bool(mods & MOD_ALT), "KeyShift": bool(mods & MOD_SHIFT),
            "NativeCode": vk if vk >= 0 else (qt if qt != EMPTY_QT else -1), "QTKeyCode": qt, "VKeyCode": vk}


def hotkey_is_empty(hk) -> bool:
    return not hk or (hk.get("QTKeyCode") in (EMPTY_QT, 0, None) and hk.get("VKeyCode", -1) in (-1, None)
                      and not hk.get("KeyModifiers"))


def _mods_of(hk) -> int:
    mods = hk.get("KeyModifiers")
    if mods is None:
        mods = (hk.get("KeyShift") and MOD_SHIFT or 0) | (hk.get("KeyCtrl") and MOD_CTRL or 0) | \
               (hk.get("KeyOption") and MOD_ALT or 0) | (hk.get("KeyCmd") and MOD_META or 0)
    return int(mods or 0)


def format_hotkey(hk) -> str:
    """Elgato-Hotkey -> "Strg+Umschalt+T" (leer, wenn nicht belegt)."""
    if hotkey_is_empty(hk):
        return ""
    mods = _mods_of(hk)
    parts = [name for bit, name in MOD_NAMES if mods & bit]
    vk = hk.get("VKeyCode", -1)
    qt = hk.get("QTKeyCode", EMPTY_QT)
    key = ""
    if isinstance(vk, int) and vk >= 0:
        if vk in _NAMED:
            key = _NAMED[vk][0]
        elif 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
            key = chr(vk)
        else:
            key = f"VK{vk:02X}"
    elif isinstance(qt, int) and qt != EMPTY_QT:
        named = next((n for n, q in _NAMED.values() if q == qt and q >= 0x01000000), None)
        key = named or (chr(qt).upper() if 0x20 < qt < 0x110000 else "")
    if key:
        parts.append(key)
    return "+".join(parts)


def parse_hotkey(text: str):
    """"Strg+Umschalt+T" -> Elgato-Hotkey. Leerer Text -> leerer Hotkey, ungültig -> ValueError."""
    text = (text or "").strip()
    if not text:
        return empty_hotkey()
    # "+" als Taste: "Strg++" bzw. "Num +"
    tokens = []
    marked = text.replace("++", "+PLUS")
    if marked.lower().endswith("num +"):
        marked = marked[:-1] + "PLUS"
    for raw in marked.split("+"):
        tokens.append(raw.strip().replace("PLUS", "+"))
    mods, vk, qt = 0, -1, None
    for i, tok in enumerate(tokens):
        low = tok.lower()
        if low in _MOD_ALIASES and i < len(tokens) - 1:
            mods |= _MOD_ALIASES[low]
            continue
        if i != len(tokens) - 1:
            raise ValueError(_("Unknown modifier key: {key}").format(key=tok))
        if low in _MOD_ALIASES:
            mods |= _MOD_ALIASES[low]
        elif low.replace(" ", "") in {k.replace(" ", "") for k in _NAME_TO_VK}:
            vk = next(v for k, v in _NAME_TO_VK.items() if k.replace(" ", "") == low.replace(" ", ""))
        elif len(tok) == 1 and (tok.isascii() and tok.isalnum()):
            vk = ord(tok.upper())
        elif len(tok) == 1:
            qt = ord(tok)
        else:
            raise ValueError(_("Unknown key: {key}").format(key=tok))
    if vk < 0 and qt is None and not mods:
        raise ValueError(_("No key specified"))
    return make_hotkey(mods, vk, qt if qt is not None else (None if vk >= 0 else EMPTY_QT))


def hotkey_from_tk(keysym: str, char: str, state: int, keycode: int = 0):
    """Tastendruck aus Tk -> Elgato-Hotkey (None bei reinen Sondertasten)."""
    mods = 0
    if state & 0x0001:
        mods |= MOD_SHIFT
    if state & 0x0004:
        mods |= MOD_CTRL
    if state & 0x0008:
        mods |= MOD_ALT
    if state & 0x0040:
        mods |= MOD_META
    if keysym in TK_MODIFIER_KEYSYMS:
        return None
    if keysym in _TK_KEYSYMS:
        return make_hotkey(mods, _TK_KEYSYMS[keysym])
    if len(keysym) == 1 and keysym.isascii() and keysym.isalnum():
        return make_hotkey(mods, ord(keysym.upper()))
    # Ziffernreihe mit Umschalt (Keysym "exclam" …): X11-Keycodes 10–19 = 1…0
    if 10 <= keycode <= 19:
        return make_hotkey(mods, 0x30 + (keycode - 9) % 10)
    ch = (char or "")
    if ch and ch.isprintable() and not ch.isspace():
        if ch.isascii() and ch.isalpha():
            return make_hotkey(mods, ord(ch.upper()))
        return make_hotkey(mods, -1, ord(ch))
    return None
