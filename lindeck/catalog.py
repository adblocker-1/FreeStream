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

KEY, DIAL = "key", "dial"


@dataclass
class Field:
    kind: str            # text, multiline, int, bool, choice, file, hotkey, page, profile
    key: str             # Schlüssel in "Settings" (bei hotkey: Index in "Hotkeys")
    label: str
    options: list = None  # choice: [(Wert, Anzeige), ...]
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


MEDIA_CHOICES = [(1, "Wiedergabe / Pause"), (2, "Nächster Titel"), (0, "Vorheriger Titel"), (3, "Stopp"),
                 (4, "Stumm"), (5, "Lauter"), (6, "Leiser"), (13, "Vorspulen"), (14, "Zurückspulen"),
                 (18, "Lautstärke (Dial)")]
BRIGHTNESS_CHOICES = [(0, "Heller"), (1, "Dunkler"), (2, "Minimum"), (3, "Niedrig"), (4, "Mittel"),
                      (5, "Hoch"), (6, "Maximum")]
AUDIO_MODES = [(0, "Abspielen / Überlappen"), (1, "Abspielen / Stopp"), (2, "Abspielen / Neustart"),
               (3, "Schleife / Stopp")]
VOLUME_MODES = [("mute", "Stummschalten umschalten"), ("up", "Lauter"), ("down", "Leiser"),
                ("set", "Lautstärke setzen")]

_HK_KEY = [Field("hotkey", 0, "Tastenkombination")]
_HK_DIAL = [Field("hotkey", 0, "Linksdrehung"), Field("hotkey", 1, "Rechtsdrehung"),
            Field("hotkey", 2, "Drücken"), Field("hotkey", 3, "Berühren")]

CATALOG = [
    # ------------------------------------------------------------ System
    ActionType("com.elgato.streamdeck.system.hotkey", "Hotkey", "System",
               {"Coalesce": True, "Hotkeys": [empty_hotkey() for _ in range(4)]},
               controllers=(KEY, DIAL), fields=_HK_KEY, dial_fields=_HK_DIAL,
               description="Sendet eine Tastenkombination"),
    ActionType("com.elgato.streamdeck.system.hotkeyswitch", "Hotkey Switch", "System",
               {"Hotkeys": [empty_hotkey(), empty_hotkey()]}, states=2,
               fields=[Field("hotkey", 0, "Zustand 1"), Field("hotkey", 1, "Zustand 2")],
               description="Wechselt bei jedem Druck zwischen zwei Tastenkombinationen"),
    ActionType("com.elgato.streamdeck.system.text", "Text", "System",
               {"pastedText": "", "isSendingEnter": False, "isTypingMode": False},
               fields=[Field("multiline", "pastedText", "Text"),
                       Field("bool", "isSendingEnter", "Danach Enter senden"),
                       Field("bool", "isTypingMode", "Tippen statt Einfügen")],
               description="Fügt einen Text ein"),
    ActionType("com.elgato.streamdeck.system.open", "Öffnen", "System", {"path": ""},
               fields=[Field("file", "path", "Datei, Ordner oder URL")],
               description="Öffnet eine Datei, einen Ordner oder eine Adresse"),
    ActionType("com.elgato.streamdeck.system.openapp", "Programm öffnen", "System",
               {"exec": "", "args": "", "long_press": "none", "bring_to_front": True},
               fields=[Field("file", "exec", "Programm / Befehl", hint="z. B. firefox oder flatpak run …"),
                       Field("text", "args", "Argumente"),
                       Field("bool", "bring_to_front", "Laufendes Programm in den Vordergrund holen"),
                       Field("choice", "long_press", "Langes Drücken",
                             [("none", "nichts"), ("quit", "Programm beenden")])],
               description="Startet ein Programm"),
    ActionType("com.elgato.streamdeck.system.close", "Programm schließen", "System",
               {"exec": "", "app_name": ""},
               fields=[Field("file", "exec", "Programm"), Field("text", "app_name", "Prozessname")],
               description="Beendet ein Programm"),
    ActionType("com.elgato.streamdeck.system.website", "Website", "System",
               {"path": "", "browser": ""},
               fields=[Field("text", "path", "Adresse", hint="https://…"),
                       Field("text", "browser", "Browser (leer = Standard)")],
               description="Öffnet eine Website"),
    ActionType("com.elgato.streamdeck.system.multimedia", "Multimedia", "System", {"actionIdx": 1},
               controllers=(KEY, DIAL), fields=[Field("choice", "actionIdx", "Funktion", MEDIA_CHOICES)],
               description="Wiedergabe steuern und Lautstärke"),
    ActionType("com.elgato.streamdeck.system.timer", "Timer", "System", {"duration": 300, "alarm": ""},
               fields=[Field("int", "duration", "Dauer (Sekunden)", min=1, max=86400),
                       Field("file", "alarm", "Alarmton (optional)")],
               description="Countdown; langes Drücken setzt zurück"),
    ActionType("com.elgato.streamdeck.system.mouse-wheel", "Mausrad", "System",
               {"horizontal": False, "invert": False}, controllers=(DIAL,),
               fields=[Field("bool", "horizontal", "Horizontal scrollen"), Field("bool", "invert", "Umkehren")],
               description="Scrollt mit dem Dial"),
    # -------------------------------------------------------- Navigation
    ActionType("com.elgato.streamdeck.profile.openchild", "Ordner", "Navigation", {"ProfileUUID": ""},
               description="Unterseite mit eigenen Tasten"),
    ActionType("com.elgato.streamdeck.profile.backtoparent", "Übergeordneter Ordner", "Navigation",
               description="Zurück aus einem Ordner"),
    ActionType("com.elgato.streamdeck.page.next", "Nächste Seite", "Navigation"),
    ActionType("com.elgato.streamdeck.page.previous", "Vorherige Seite", "Navigation"),
    ActionType("com.elgato.streamdeck.page.goto", "Gehe zu Seite", "Navigation", {"PageIndex": 1},
               fields=[Field("page", "PageIndex", "Seite")]),
    ActionType("com.elgato.streamdeck.page.indicator", "Seitenanzeige", "Navigation"),
    ActionType("com.elgato.streamdeck.profile.rotate", "Profil wechseln", "Navigation", {"ProfileUUID": ""},
               fields=[Field("profile", "ProfileUUID", "Profil")]),
    # --------------------------------------------------------- Stream Deck
    ActionType("com.elgato.streamdeck.system.keybrightness", "Helligkeit", "Stream Deck", {"actionIdx": 0},
               controllers=(KEY, DIAL), fields=[Field("choice", "actionIdx", "Funktion", BRIGHTNESS_CHOICES)]),
    ActionType("com.elgato.streamdeck.system.sleep", "Sleep", "Stream Deck",
               description="Schaltet das Display aus"),
    ActionType("com.elgato.streamdeck.system.digitaltime", "Uhrzeit", "Stream Deck",
               {"24HourFormat": True, "theme": "dark"},
               fields=[Field("bool", "24HourFormat", "24-Stunden-Format"),
                       Field("choice", "theme", "Darstellung", [("dark", "dunkel"), ("light", "hell")])]),
    # -------------------------------------------------------- Multi Action
    ActionType("com.elgato.streamdeck.multiactions.routine", "Multi Action", "Multi Action",
               {"Actions": None}, children=("groups", ["Schritte"]),
               description="Mehrere Aktionen nacheinander"),
    ActionType("com.elgato.streamdeck.multiactions.routine2", "Multi Action Switch", "Multi Action",
               states=2, children=("groups", ["Zustand 1", "Zustand 2"]),
               description="Abwechselnd zwei Abläufe"),
    ActionType("com.elgato.streamdeck.multiactions.random", "Zufällige Aktion", "Multi Action",
               children=("groups", ["Auswahl"])),
    ActionType("com.elgato.streamdeck.multiactions.delay", "Verzögerung", "Multi Action", {"delay": 500},
               fields=[Field("int", "delay", "Wartezeit (ms)", min=0, max=600000)],
               description="Nur innerhalb einer Multi Action"),
    ActionType("com.elgato.streamdeck.keys.logic", "Key Logic", "Multi Action",
               children=("slots", ["Tippen", "Doppeltippen", "Halten"]),
               description="Unterschiedliche Aktionen für Tippen, Doppeltippen, Halten"),
    ActionType("com.elgato.streamdeck.keys.adaptor", "Action Trigger", "Multi Action",
               controllers=(DIAL,), children=("slots", ["Links drehen", "Drücken", "Rechts drehen"])),
    ActionType("com.elgato.streamdeck.keys.stack", "Action Wheel", "Multi Action", {"CurrentIdx": 0},
               controllers=(DIAL,), children=("groups", ["Aktionen"])),
    ActionType("com.elgato.streamdeck.dial.stack", "Dial Stack", "Multi Action", {"CurrentIdx": 0},
               controllers=(DIAL,), children=("groups", ["Dial-Aktionen"])),
    # ----------------------------------------------------------- Soundboard
    ActionType("com.elgato.streamdeck.soundboard.playaudio", "Audio abspielen", "Soundboard",
               {"path": "", "actionType": 0, "volume": 100},
               fields=[Field("file", "path", "Audiodatei"), Field("choice", "actionType", "Modus", AUDIO_MODES),
                       Field("int", "volume", "Lautstärke (%)", min=0, max=100)]),
    ActionType("com.elgato.streamdeck.soundboard.stopaudioplay", "Audio stoppen", "Soundboard"),
    # ---------------------------------------------------------------- Audio
    ActionType("com.elgato.volume-controller.output-device-control", "Ausgabegerät", "Audio",
               {"action": "mute", "volumeStep": 5, "friendlyName": ""}, controllers=(KEY, DIAL),
               fields=[Field("choice", "action", "Taste", VOLUME_MODES),
                       Field("int", "volumeStep", "Schrittweite (%)", min=1, max=50),
                       Field("text", "friendlyName", "Gerät (leer = Standard)"),
                       Field("text", "appName", "Nur dieses Programm (optional)")],
               plugin="com.elgato.volume-controller"),
    ActionType("com.elgato.volume-controller.input-device-control", "Eingabegerät", "Audio",
               {"action": "mute", "volumeStep": 5, "friendlyName": ""}, controllers=(KEY, DIAL),
               fields=[Field("choice", "action", "Taste", VOLUME_MODES),
                       Field("int", "volumeStep", "Schrittweite (%)", min=1, max=50),
                       Field("text", "friendlyName", "Gerät (leer = Standard)")],
               plugin="com.elgato.volume-controller"),
]

_MIX_CHANNELS = [("system", "System"), ("music", "Musik"), ("browser", "Browser"), ("voice", "Voice Chat"),
                 ("game", "Spiel"), ("sfx", "Effekte"), ("mic", "Mikrofon")]
_MIXES = [("both", "Monitor- und Stream-Mix"), ("monitor", "nur Monitor-Mix"), ("stream", "nur Stream-Mix")]
_TOGGLE = [("toggle", "umschalten"), ("start", "starten"), ("stop", "stoppen")]

CATALOG += [
    # ------------------------------------------------------------- OBS
    ActionType("com.lindeck.obs.scene", "Szene", "OBS Studio", {"scene": ""}, controllers=(KEY, DIAL),
               fields=[Field("text", "scene", "Szene", hint="Name wie in OBS")],
               description="Wechselt die Szene (Dial: durch Szenen blättern)"),
    ActionType("com.lindeck.obs.record", "Aufnahme", "OBS Studio", {"mode": "toggle"}, states=2,
               fields=[Field("choice", "mode", "Funktion", _TOGGLE + [("pause", "Pause umschalten")])]),
    ActionType("com.lindeck.obs.stream", "Stream", "OBS Studio", {"mode": "toggle"}, states=2,
               fields=[Field("choice", "mode", "Funktion", _TOGGLE)]),
    ActionType("com.lindeck.obs.source", "Quelle ein/aus", "OBS Studio", {"scene": "", "source": ""}, states=2,
               fields=[Field("text", "source", "Quelle"), Field("text", "scene", "Szene (leer = aktuelle)")]),
    ActionType("com.lindeck.obs.audio", "Audio-Eingang", "OBS Studio", {"source": "", "step_db": 1}, states=2,
               controllers=(KEY, DIAL),
               fields=[Field("text", "source", "Audio-Eingang", hint="z. B. Mic/Aux"),
                       Field("int", "step_db", "Schritt je Dial-Raste (dB)", min=1, max=10)],
               description="Taste: stumm schalten · Dial: Lautstärke"),
    ActionType("com.lindeck.obs.replay", "Replay-Puffer", "OBS Studio", {"mode": "save"},
               fields=[Field("choice", "mode", "Funktion", [("save", "Replay speichern"),
                                                            ("toggle", "Puffer ein/aus")])]),
    ActionType("com.lindeck.obs.virtualcam", "Virtuelle Kamera", "OBS Studio", states=2),
    ActionType("com.lindeck.obs.studiomode", "Studio-Modus", "OBS Studio", states=2),
    ActionType("com.lindeck.obs.transition", "Übergang ausführen", "OBS Studio",
               description="Vorschau → Programm im Studio-Modus"),
    ActionType("com.lindeck.obs.screenshot", "Screenshot", "OBS Studio", {"source": ""},
               fields=[Field("text", "source", "Quelle (leer = Programm)")]),
    # ------------------------------------------------------------ Medien
    ActionType("com.lindeck.media.player", "Medien-Player", "Spotify & Medien",
               {"player": "spotify", "function": "playpause"}, controllers=(KEY, DIAL),
               fields=[Field("text", "player", "Player", hint="spotify, vlc, firefox … (leer = aktiver)"),
                       Field("choice", "function", "Funktion",
                             [("playpause", "Wiedergabe/Pause"), ("next", "Nächster Titel"),
                              ("previous", "Vorheriger Titel"), ("nowplaying", "Aktueller Titel"),
                              ("shuffle", "Zufallswiedergabe"), ("repeat", "Wiederholen"),
                              ("volumeup", "Lauter"), ("volumedown", "Leiser"), ("stop", "Stopp")])],
               description="Steuert Spotify oder jeden MPRIS-Player"),
    # ----------------------------------------------------------- Discord
    ActionType("com.lindeck.discord.voice", "Discord", "Discord", {"function": "mute", "channel_id": ""}, states=2,
               fields=[Field("choice", "function", "Funktion",
                             [("mute", "Mikrofon stumm"), ("deafen", "Ton aus"), ("pushtotalk", "Push to Talk"),
                              ("pushtomute", "Push to Mute"), ("voicechannel", "Sprachkanal betreten"),
                              ("textchannel", "Textkanal öffnen"), ("leave", "Sprachkanal verlassen")]),
                       Field("text", "channel_id", "Kanal-ID (nur Kanal)")],
               description="Braucht eine eigene Discord-App (siehe README)"),
    # ------------------------------------------------------------- Mixer
    ActionType("com.lindeck.mixer.channel", "Mixer-Kanal", "Audio-Mixer",
               {"channel": "music", "mix": "both", "action": "mute", "step": 5}, controllers=(KEY, DIAL),
               fields=[Field("choice", "channel", "Kanal", _MIX_CHANNELS), Field("choice", "mix", "Mix", _MIXES),
                       Field("choice", "action", "Taste", [("mute", "stumm umschalten"), ("up", "lauter"),
                                                           ("down", "leiser"), ("set", "Lautstärke setzen")]),
                       Field("int", "step", "Schrittweite (%)", min=1, max=50),
                       Field("int", "volume", "Lautstärke für „setzen“ (%)", min=0, max=150)],
               description="Wave-Link-Ersatz: Kanal-Lautstärke je Mix"),
    ActionType("com.lindeck.mixer.output", "Monitor-Ausgang", "Audio-Mixer", {"device": ""},
               fields=[Field("text", "device", "Gerät (leer = nächstes)")],
               description="Monitor-Mix auf Kopfhörer/Lautsprecher umschalten"),
    # -------------------------------------------------------- Wave-Mikro
    ActionType("com.lindeck.wavemic.control", "Wave-Mikrofon", "Wave-Mikrofon", {"function": "mute", "step": 5},
               controllers=(KEY, DIAL),
               fields=[Field("choice", "function", "Funktion", [("mute", "stumm umschalten"), ("gain", "Gain +"),
                                                               ("lowcut", "Low Cut ein/aus"),
                                                               ("clipguard", "Clipguard ein/aus")]),
                       Field("int", "step", "Schrittweite Gain (%)", min=1, max=20)],
               description="Gain, Stumm, Low Cut, Clipguard (Dial: Gain)"),
]

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
        "States": [default_state() for _ in range(t.states if t else 1)],
        "UUID": uuid,
    }
    if t and t.children:
        kind, labels = t.children
        data["Actions"] = [{"Actions": []} for _ in labels] if kind == "groups" else [{} for _ in labels]
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
MOD_NAMES = [(MOD_CTRL, "Strg"), (MOD_SHIFT, "Umschalt"), (MOD_ALT, "Alt"), (MOD_META, "Super")]
_MOD_ALIASES = {"strg": MOD_CTRL, "ctrl": MOD_CTRL, "control": MOD_CTRL, "steuerung": MOD_CTRL,
                "umschalt": MOD_SHIFT, "shift": MOD_SHIFT, "alt": MOD_ALT, "option": MOD_ALT,
                "super": MOD_META, "win": MOD_META, "meta": MOD_META, "cmd": MOD_META, "windows": MOD_META}

# Windows-VK -> (Anzeigename, Qt-Keycode)
_NAMED = {
    0x08: ("Rücktaste", 0x01000003), 0x09: ("Tab", 0x01000001), 0x0D: ("Enter", 0x01000004),
    0x13: ("Pause", 0x01000008), 0x1B: ("Esc", 0x01000000), 0x20: ("Leertaste", 0x20),
    0x21: ("Bild↑", 0x01000016), 0x22: ("Bild↓", 0x01000017), 0x23: ("Ende", 0x01000011),
    0x24: ("Pos1", 0x01000010), 0x25: ("Links", 0x01000012), 0x26: ("Hoch", 0x01000013),
    0x27: ("Rechts", 0x01000014), 0x28: ("Runter", 0x01000015), 0x2C: ("Druck", 0x01000009),
    0x2D: ("Einfg", 0x01000006), 0x2E: ("Entf", 0x01000007),
    0x6A: ("Num *", 0x2A), 0x6B: ("Num +", 0x2B), 0x6D: ("Num -", 0x2D), 0x6E: ("Num ,", 0x2C),
    0x6F: ("Num /", 0x2F),
    0xAD: ("Stumm", 0x01000071), 0xAE: ("Leiser", 0x01000070), 0xAF: ("Lauter", 0x01000072),
    0xB0: ("Nächster Titel", 0x01000083), 0xB1: ("Vorheriger Titel", 0x01000082),
    0xB2: ("Stopp", 0x01000081), 0xB3: ("Play/Pause", 0x01000086),
}
for _i in range(10):
    _NAMED[0x60 + _i] = (f"Num {_i}", 0x30 + _i)
for _i in range(24):
    _NAMED[0x70 + _i] = (f"F{_i + 1}", 0x01000030 + _i)

_NAME_TO_VK = {n.lower(): vk for vk, (n, _) in _NAMED.items()}
_NAME_TO_VK.update({
    "backspace": 0x08, "return": 0x0D, "eingabe": 0x0D, "escape": 0x1B, "space": 0x20, "leer": 0x20,
    "pageup": 0x21, "pgup": 0x21, "bildauf": 0x21, "pagedown": 0x22, "pgdn": 0x22, "bildab": 0x22,
    "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "print": 0x2C,
    "insert": 0x2D, "ins": 0x2D, "delete": 0x2E, "del": 0x2E, "mute": 0xAD, "volumedown": 0xAE,
    "volumeup": 0xAF, "playpause": 0xB3, "next": 0xB0, "prev": 0xB1,
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
            raise ValueError(f"Unbekannte Sondertaste: {tok}")
        if low in _MOD_ALIASES:
            mods |= _MOD_ALIASES[low]
        elif low.replace(" ", "") in {k.replace(" ", "") for k in _NAME_TO_VK}:
            vk = next(v for k, v in _NAME_TO_VK.items() if k.replace(" ", "") == low.replace(" ", ""))
        elif len(tok) == 1 and (tok.isascii() and tok.isalnum()):
            vk = ord(tok.upper())
        elif len(tok) == 1:
            qt = ord(tok)
        else:
            raise ValueError(f"Unbekannte Taste: {tok}")
    if vk < 0 and qt is None and not mods:
        raise ValueError("Keine Taste angegeben")
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
