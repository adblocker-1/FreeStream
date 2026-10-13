"""Virtuelle Tastatur/Maus über /dev/uinput.

Funktioniert unter X11 und Wayland (GNOME, KDE, wlroots), weil die Eingaben
auf Kernel-Ebene erzeugt werden – genau wie eine echte Tastatur.

Elgato speichert Hotkeys mit Windows-Virtual-Key-Codes (VKeyCode) und
Qt-Keycodes (QTKeyCode). Windows-VK-Codes hängen vom Tastaturlayout ab
(auf deutschem Layout liegt VK_Z auf der physischen Y-Taste), daher wird
für die Umrechnung das Layout berücksichtigt.
"""
import logging
import os
import subprocess
import threading
import time

log = logging.getLogger("lindeck.keyboard")

try:
    from evdev import UInput, ecodes as E
except ImportError:  # pragma: no cover - auf Systemen ohne evdev
    UInput = None
    E = None


def _k(name):
    return getattr(E, name) if E else name


# --------------------------------------------------------------------------
# Windows Virtual-Key -> evdev (layoutunabhängige Tasten)
# --------------------------------------------------------------------------
def _vk_base():
    m = {
        0x08: "KEY_BACKSPACE", 0x09: "KEY_TAB", 0x0C: "KEY_CLEAR", 0x0D: "KEY_ENTER",
        0x10: "KEY_LEFTSHIFT", 0x11: "KEY_LEFTCTRL", 0x12: "KEY_LEFTALT", 0x13: "KEY_PAUSE",
        0x14: "KEY_CAPSLOCK", 0x1B: "KEY_ESC", 0x20: "KEY_SPACE", 0x21: "KEY_PAGEUP",
        0x22: "KEY_PAGEDOWN", 0x23: "KEY_END", 0x24: "KEY_HOME", 0x25: "KEY_LEFT",
        0x26: "KEY_UP", 0x27: "KEY_RIGHT", 0x28: "KEY_DOWN", 0x2C: "KEY_SYSRQ",
        0x2D: "KEY_INSERT", 0x2E: "KEY_DELETE", 0x2F: "KEY_HELP",
        0x5B: "KEY_LEFTMETA", 0x5C: "KEY_RIGHTMETA", 0x5D: "KEY_COMPOSE", 0x5F: "KEY_SLEEP",
        0x6A: "KEY_KPASTERISK", 0x6B: "KEY_KPPLUS", 0x6C: "KEY_KPCOMMA", 0x6D: "KEY_KPMINUS",
        0x6E: "KEY_KPDOT", 0x6F: "KEY_KPSLASH", 0x90: "KEY_NUMLOCK", 0x91: "KEY_SCROLLLOCK",
        0xA0: "KEY_LEFTSHIFT", 0xA1: "KEY_RIGHTSHIFT", 0xA2: "KEY_LEFTCTRL", 0xA3: "KEY_RIGHTCTRL",
        0xA4: "KEY_LEFTALT", 0xA5: "KEY_RIGHTALT",
        0xA6: "KEY_BACK", 0xA7: "KEY_FORWARD", 0xA8: "KEY_REFRESH", 0xA9: "KEY_STOP",
        0xAA: "KEY_SEARCH", 0xAB: "KEY_BOOKMARKS", 0xAC: "KEY_HOMEPAGE",
        0xAD: "KEY_MUTE", 0xAE: "KEY_VOLUMEDOWN", 0xAF: "KEY_VOLUMEUP",
        0xB0: "KEY_NEXTSONG", 0xB1: "KEY_PREVIOUSSONG", 0xB2: "KEY_STOPCD", 0xB3: "KEY_PLAYPAUSE",
        0xB4: "KEY_MAIL", 0xB5: "KEY_MEDIA", 0xB6: "KEY_COMPUTER", 0xB7: "KEY_CALC",
        0xE2: "KEY_102ND",
    }
    for i in range(10):
        m[0x60 + i] = f"KEY_KP{i}"
    for i in range(24):
        m[0x70 + i] = f"KEY_F{i + 1}"
    return m


# Layoutabhängige Tasten: VK -> evdev-Name
_LAYOUT_VK = {
    "us": {
        0xBA: "KEY_SEMICOLON", 0xBB: "KEY_EQUAL", 0xBC: "KEY_COMMA", 0xBD: "KEY_MINUS",
        0xBE: "KEY_DOT", 0xBF: "KEY_SLASH", 0xC0: "KEY_GRAVE", 0xDB: "KEY_LEFTBRACE",
        0xDC: "KEY_BACKSLASH", 0xDD: "KEY_RIGHTBRACE", 0xDE: "KEY_APOSTROPHE",
    },
    "de": {
        0xBA: "KEY_LEFTBRACE",   # Ü
        0xBB: "KEY_RIGHTBRACE",  # +
        0xBC: "KEY_COMMA", 0xBD: "KEY_SLASH",  # -
        0xBE: "KEY_DOT", 0xBF: "KEY_BACKSLASH",  # #
        0xC0: "KEY_SEMICOLON",   # Ö
        0xDB: "KEY_MINUS",       # ß
        0xDC: "KEY_GRAVE",       # ^
        0xDD: "KEY_EQUAL",       # ´
        0xDE: "KEY_APOSTROPHE",  # Ä
    },
}

# Qt-Keycodes (Fallback, z.B. für auf macOS erstellte Profile)
_QT = {
    0x01000000: "KEY_ESC", 0x01000001: "KEY_TAB", 0x01000002: "KEY_TAB", 0x01000003: "KEY_BACKSPACE",
    0x01000004: "KEY_ENTER", 0x01000005: "KEY_KPENTER", 0x01000006: "KEY_INSERT", 0x01000007: "KEY_DELETE",
    0x01000008: "KEY_PAUSE", 0x01000009: "KEY_SYSRQ", 0x0100000A: "KEY_SYSRQ", 0x0100000B: "KEY_CLEAR",
    0x01000010: "KEY_HOME", 0x01000011: "KEY_END", 0x01000012: "KEY_LEFT", 0x01000013: "KEY_UP",
    0x01000014: "KEY_RIGHT", 0x01000015: "KEY_DOWN", 0x01000016: "KEY_PAGEUP", 0x01000017: "KEY_PAGEDOWN",
    0x01000020: "KEY_LEFTSHIFT", 0x01000021: "KEY_LEFTCTRL", 0x01000022: "KEY_LEFTMETA", 0x01000023: "KEY_LEFTALT",
    0x01000024: "KEY_CAPSLOCK", 0x01000025: "KEY_NUMLOCK", 0x01000026: "KEY_SCROLLLOCK",
    0x01000055: "KEY_COMPOSE", 0x01000058: "KEY_HELP",
    0x01000070: "KEY_VOLUMEDOWN", 0x01000071: "KEY_MUTE", 0x01000072: "KEY_VOLUMEUP",
    0x01000080: "KEY_PLAYCD", 0x01000081: "KEY_STOPCD", 0x01000082: "KEY_PREVIOUSSONG",
    0x01000083: "KEY_NEXTSONG", 0x01000085: "KEY_PAUSECD", 0x01000086: "KEY_PLAYPAUSE",
    0x20: "KEY_SPACE",
}
for _i in range(24):
    _QT[0x01000030 + _i] = f"KEY_F{_i + 1}"

# Zeichen -> (evdev-Name, Shift, AltGr) für den Tippmodus
_US_CHARS = {
    "`": ("KEY_GRAVE", 0, 0), "~": ("KEY_GRAVE", 1, 0), "-": ("KEY_MINUS", 0, 0), "_": ("KEY_MINUS", 1, 0),
    "=": ("KEY_EQUAL", 0, 0), "+": ("KEY_EQUAL", 1, 0), "[": ("KEY_LEFTBRACE", 0, 0), "{": ("KEY_LEFTBRACE", 1, 0),
    "]": ("KEY_RIGHTBRACE", 0, 0), "}": ("KEY_RIGHTBRACE", 1, 0), "\\": ("KEY_BACKSLASH", 0, 0),
    "|": ("KEY_BACKSLASH", 1, 0), ";": ("KEY_SEMICOLON", 0, 0), ":": ("KEY_SEMICOLON", 1, 0),
    "'": ("KEY_APOSTROPHE", 0, 0), '"': ("KEY_APOSTROPHE", 1, 0), ",": ("KEY_COMMA", 0, 0),
    "<": ("KEY_COMMA", 1, 0), ".": ("KEY_DOT", 0, 0), ">": ("KEY_DOT", 1, 0), "/": ("KEY_SLASH", 0, 0),
    "?": ("KEY_SLASH", 1, 0), "!": ("KEY_1", 1, 0), "@": ("KEY_2", 1, 0), "#": ("KEY_3", 1, 0),
    "$": ("KEY_4", 1, 0), "%": ("KEY_5", 1, 0), "^": ("KEY_6", 1, 0), "&": ("KEY_7", 1, 0),
    "*": ("KEY_8", 1, 0), "(": ("KEY_9", 1, 0), ")": ("KEY_0", 1, 0),
}
_DE_CHARS = {
    "^": ("KEY_GRAVE", 0, 0), "°": ("KEY_GRAVE", 1, 0), "!": ("KEY_1", 1, 0), '"': ("KEY_2", 1, 0),
    "²": ("KEY_2", 0, 1), "§": ("KEY_3", 1, 0), "³": ("KEY_3", 0, 1), "$": ("KEY_4", 1, 0),
    "%": ("KEY_5", 1, 0), "&": ("KEY_6", 1, 0), "/": ("KEY_7", 1, 0), "{": ("KEY_7", 0, 1),
    "(": ("KEY_8", 1, 0), "[": ("KEY_8", 0, 1), ")": ("KEY_9", 1, 0), "]": ("KEY_9", 0, 1),
    "=": ("KEY_0", 1, 0), "}": ("KEY_0", 0, 1), "ß": ("KEY_MINUS", 0, 0), "?": ("KEY_MINUS", 1, 0),
    "\\": ("KEY_MINUS", 0, 1), "´": ("KEY_EQUAL", 0, 0), "`": ("KEY_EQUAL", 1, 0),
    "@": ("KEY_Q", 0, 1), "€": ("KEY_E", 0, 1), "ü": ("KEY_LEFTBRACE", 0, 0), "Ü": ("KEY_LEFTBRACE", 1, 0),
    "+": ("KEY_RIGHTBRACE", 0, 0), "*": ("KEY_RIGHTBRACE", 1, 0), "~": ("KEY_RIGHTBRACE", 0, 1),
    "ö": ("KEY_SEMICOLON", 0, 0), "Ö": ("KEY_SEMICOLON", 1, 0), "ä": ("KEY_APOSTROPHE", 0, 0),
    "Ä": ("KEY_APOSTROPHE", 1, 0), "#": ("KEY_BACKSLASH", 0, 0), "'": ("KEY_BACKSLASH", 1, 0),
    "<": ("KEY_102ND", 0, 0), ">": ("KEY_102ND", 1, 0), "|": ("KEY_102ND", 0, 1),
    ",": ("KEY_COMMA", 0, 0), ";": ("KEY_COMMA", 1, 0), ".": ("KEY_DOT", 0, 0), ":": ("KEY_DOT", 1, 0),
    "-": ("KEY_SLASH", 0, 0), "_": ("KEY_SLASH", 1, 0), "µ": ("KEY_M", 0, 1),
}


def detect_layout() -> str:
    """Ermittelt das aktive Tastaturlayout (de/us)."""
    candidates = []
    for env in ("XKB_DEFAULT_LAYOUT",):
        if os.environ.get(env):
            candidates.append(os.environ[env])
    try:
        out = subprocess.run(["localectl", "status"], capture_output=True, text=True, timeout=2).stdout
        for line in out.splitlines():
            if "Layout" in line and ":" in line:
                candidates.append(line.split(":", 1)[1].strip())
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        txt = open("/etc/default/keyboard", encoding="utf-8").read()
        for line in txt.splitlines():
            if line.startswith("XKBLAYOUT="):
                candidates.append(line.split("=", 1)[1].strip('"'))
    except OSError:
        pass
    for c in candidates:
        first = c.split(",")[0].strip().lower()
        if first.startswith(("de", "ch", "at")):
            return "de"
        if first:
            return "us"
    lang = os.environ.get("LANG", "")
    return "de" if lang.startswith("de") else "us"


class VirtualInput:
    """Erzeugt Tastatur- und Mausereignisse."""

    MOD_SHIFT, MOD_CTRL, MOD_ALT, MOD_META = 1, 2, 4, 8

    def __init__(self, layout: str = "auto"):
        self.layout = detect_layout() if layout in ("auto", "", None) else layout
        if self.layout not in _LAYOUT_VK:
            self.layout = "us"
        self._lock = threading.Lock()
        self.dev = None
        self.available = False
        self.vk = {k: _k(v) for k, v in _vk_base().items()}
        self.vk.update({k: _k(v) for k, v in _LAYOUT_VK[self.layout].items()})
        for i in range(10):
            self.vk[0x30 + i] = _k(f"KEY_{i}")
        for i in range(26):
            ch = chr(ord("A") + i)
            name = f"KEY_{ch}"
            if self.layout == "de" and ch in "YZ":
                name = "KEY_Z" if ch == "Y" else "KEY_Y"
            self.vk[0x41 + i] = _k(name)
        self.qt = {k: _k(v) for k, v in _QT.items()}
        chars = dict(_DE_CHARS if self.layout == "de" else _US_CHARS)
        self.chars = {c: (_k(n), s, a) for c, (n, s, a) in chars.items()}
        if E:
            self._open()

    def _open(self):
        try:
            keys = [v for k, v in E.ecodes.items() if k.startswith("KEY_") and isinstance(v, int) and v < 0x2FF]
            keys += [E.BTN_LEFT, E.BTN_RIGHT, E.BTN_MIDDLE]
            caps = {E.EV_KEY: sorted(set(keys)), E.EV_REL: [E.REL_X, E.REL_Y, E.REL_WHEEL, E.REL_HWHEEL]}
            self.dev = UInput(caps, name="LinDeck Virtual Input")
            self.available = True
            time.sleep(0.2)  # Compositor muss das Gerät erst erkennen
        except Exception as e:  # noqa: BLE001 - OSError, UInputError
            log.warning("uinput nicht verfügbar (%s) – Hotkeys deaktiviert. "
                        "Installer ausführen oder Benutzer zur Gruppe 'input' hinzufügen.", e)
            self.available = False

    # ---------------------------------------------------------------- Keys
    def _emit(self, code, value):
        if self.dev:
            self.dev.write(E.EV_KEY, code, value)
            self.dev.syn()

    def tap(self, codes, hold_ms=15):
        """Drückt eine Kombination (Liste von Keycodes) und lässt sie wieder los."""
        if not self.available or not codes:
            log.info("tap %s (uinput nicht verfügbar)", codes)
            return
        with self._lock:
            for c in codes:
                self._emit(c, 1)
                time.sleep(0.004)
            time.sleep(hold_ms / 1000)
            for c in reversed(codes):
                self._emit(c, 0)
                time.sleep(0.004)

    def press(self, codes):
        with self._lock:
            for c in codes:
                self._emit(c, 1)

    def release(self, codes):
        with self._lock:
            for c in reversed(codes):
                self._emit(c, 0)

    def key_by_name(self, name):
        return _k(name)

    def hotkey_codes(self, hk: dict, swap_cmd_ctrl=False):
        """Elgato-Hotkey-Eintrag -> Liste von evdev-Codes (Modifier zuerst)."""
        if not hk:
            return []
        qt = hk.get("QTKeyCode", 0)
        vk = hk.get("VKeyCode", -1)
        mods = hk.get("KeyModifiers")
        if mods is None:
            mods = (hk.get("KeyShift") and 1 or 0) | (hk.get("KeyCtrl") and 2 or 0) | \
                   (hk.get("KeyOption") and 4 or 0) | (hk.get("KeyCmd") and 8 or 0)
        key = None
        if isinstance(vk, int) and vk >= 0 and vk in self.vk:
            key = self.vk[vk]
        elif qt in self.qt:
            key = self.qt[qt]
        elif isinstance(qt, int) and 0x20 < qt < 0x7F:
            ch = chr(qt)
            if ch.isalnum():
                key = self.vk.get(ord(ch.upper()))
            elif ch in self.chars:
                key = self.chars[ch][0]
        out = []
        ctrl, meta = bool(mods & 2), bool(mods & 8)
        if swap_cmd_ctrl:
            ctrl, meta = meta, ctrl
        if ctrl:
            out.append(_k("KEY_LEFTCTRL"))
        if mods & 1:
            out.append(_k("KEY_LEFTSHIFT"))
        if mods & 4:
            out.append(_k("KEY_LEFTALT"))
        if meta:
            out.append(_k("KEY_LEFTMETA"))
        if key is not None and key not in out:
            out.append(key)
        if key is None and not out:
            return []
        return out

    @staticmethod
    def hotkey_empty(hk: dict) -> bool:
        return not hk or (hk.get("QTKeyCode") in (33554431, 0, None) and hk.get("VKeyCode", -1) in (-1, None)
                          and not hk.get("KeyModifiers"))

    # ---------------------------------------------------------------- Text
    def can_type(self, text: str) -> bool:
        return all(c.isascii() and (c.isalnum() or c in " \n\t") or c in self.chars or
                   (c.lower() in "abcdefghijklmnopqrstuvwxyz") for c in text)

    def type_text(self, text: str):
        for ch in text:
            if ch == "\n":
                self.tap([_k("KEY_ENTER")])
            elif ch == "\t":
                self.tap([_k("KEY_TAB")])
            elif ch == " ":
                self.tap([_k("KEY_SPACE")])
            elif ch.isascii() and ch.isalpha():
                code = self.vk[ord(ch.upper())]
                self.tap(([_k("KEY_LEFTSHIFT")] if ch.isupper() else []) + [code], hold_ms=5)
            elif ch.isascii() and ch.isdigit():
                self.tap([self.vk[ord(ch)]], hold_ms=5)
            elif ch in self.chars:
                code, shift, altgr = self.chars[ch]
                mods = ([_k("KEY_LEFTSHIFT")] if shift else []) + ([_k("KEY_RIGHTALT")] if altgr else [])
                self.tap(mods + [code], hold_ms=5)
            time.sleep(0.006)

    # --------------------------------------------------------------- Mouse
    def scroll(self, amount: int, horizontal=False):
        if not self.available:
            return
        with self._lock:
            self.dev.write(E.EV_REL, E.REL_HWHEEL if horizontal else E.REL_WHEEL, amount)
            self.dev.syn()

    def close(self):
        if self.dev:
            try:
                self.dev.close()
            except OSError:
                pass
