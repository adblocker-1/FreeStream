"""Virtuelles Deck: Tasten als Fenster auf dem Bildschirm – auch ohne angeschlossenes Gerät.

VirtualDeck spricht dieselbe Schnittstelle wie ein Gerät aus python-elgato-streamdeck,
damit DeckController (engine.py) es wie ein echtes Deck steuert: Profile, Seiten,
Ordner, Aktionen, Plugins. Statt per USB landen die Tastenbilder hier im Speicher;
das Fenster (virtualui.py) holt sie ab und meldet Klicks über press() zurück.

Einstellungen in config.json unter „virtual_deck“ (Einstellungen › Geräte › Virtuelles Deck):
  enabled   an/aus
  mode      "window" (schwebendes Fenster) oder "edge" (Leiste am Bildschirmrand)
  model     Größe, ein Code aus models.VIRTUAL_SIZES (quer; als Randleiste hochkant, siehe device_model)
  topmost   Fenster immer im Vordergrund
  edge      "right" oder "left" (nur bei der Leiste)
  shortcut  Tastenkürzel zum Ein-/Ausblenden, z. B. "Super+D"
  visible   zuletzt gezeigt oder ausgeblendet
  pos       [x, y] des schwebenden Fensters
Name, Helligkeit und Profil stehen wie bei echten Geräten unter devices[SERIAL].
"""
import threading

from .models import VIRTUAL_DEFAULT, VIRTUAL_MODELS, VIRTUAL_SIZES, VIRTUAL_UPRIGHT

SERIAL = "VIRTUAL"
KEY_PX = 96
SHORTCUT_ID = "freedeck-virtual-deck"
DEFAULTS = {"enabled": False, "mode": "window", "model": VIRTUAL_DEFAULT, "topmost": True, "edge": "right",
            "shortcut": "", "visible": True, "pos": None}


def settings(cfg):
    """Einstellungen des virtuellen Decks mit Standardwerten (ungültige Werte ersetzt)."""
    raw = cfg.get("virtual_deck") if cfg is not None else None
    s = dict(DEFAULTS)
    if isinstance(raw, dict):
        s.update({k: v for k, v in raw.items() if k in DEFAULTS})
    if s["model"] not in VIRTUAL_SIZES:
        s["model"] = VIRTUAL_DEFAULT
    if s["mode"] not in ("window", "edge"):
        s["mode"] = "window"
    if s["edge"] not in ("right", "left"):
        s["edge"] = "right"
    return s


def device_model(s):
    """Modellcode des laufenden Decks: als Randleiste hochkant (2 × 4 statt 4 × 2)."""
    return VIRTUAL_UPRIGHT[s["model"]] if s["mode"] == "edge" else s["model"]


def update(cfg, **changes):
    """Einstellungen ändern (ohne zu speichern)."""
    s = settings(cfg)
    s.update(changes)
    cfg.data["virtual_deck"] = s
    return s


class VirtualDeck:
    """Gerät ohne Hardware mit der Schnittstelle von StreamDeck.Devices.StreamDeck."""

    VIRTUAL = True
    TOUCH_KEY_COUNT = 0
    SCREEN_PIXEL_WIDTH = 0

    def __init__(self, model=VIRTUAL_DEFAULT):
        self.model_code = model if model in VIRTUAL_MODELS else VIRTUAL_DEFAULT
        g = VIRTUAL_MODELS[self.model_code]
        self.rows, self.cols = g.rows, g.cols
        self._lock = threading.RLock()
        self._key_cb = None
        self._open = False
        self.brightness = 100
        self.images = {}   # Tastenindex -> PIL-Bild (RGB, KEY_PX × KEY_PX)
        self.seq = 0       # zählt hoch, wenn sich ein Bild ändert (das Fenster zeichnet dann neu)

    # ----------------------------------------------------------- Kennung
    def id(self):
        return "virtual:" + self.model_code

    def get_serial_number(self):
        return SERIAL

    def get_firmware_version(self):
        return ""

    def deck_type(self):
        return "Virtual Deck"

    def product_id(self):
        return None

    def connected(self):
        return self._open

    # ----------------------------------------------------------- Aufbau
    def key_layout(self):
        return self.rows, self.cols

    def key_count(self):
        return self.rows * self.cols

    def dial_count(self):
        return 0

    def is_visual(self):
        return True

    def is_touch(self):
        return False

    def key_image_format(self):
        return {"size": (KEY_PX, KEY_PX), "format": "PIL", "flip": (False, False), "rotation": 0}

    def touchscreen_image_format(self):
        return {"size": (0, 0)}

    # ----------------------------------------------------------- Steuerung
    def open(self):
        self._open = True

    def close(self):
        self._open = False

    def reset(self):
        with self._lock:
            self.images = {}
            self.seq += 1

    def set_brightness(self, percent):
        self.brightness = max(0, min(100, int(percent)))
        self.seq += 1

    def set_poll_frequency(self, hz):
        pass

    def set_key_callback(self, cb):
        self._key_cb = cb

    def set_dial_callback(self, cb):
        pass

    def set_touchscreen_callback(self, cb):
        pass

    def set_key_image(self, key, image):
        if image is None or not 0 <= key < self.key_count():
            return
        with self._lock:
            self.images[key] = image
            self.seq += 1

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()

    # ----------------------------------------------------------- Fenster
    def press(self, key, pressed):
        """Klick im Fenster: wie ein Tastendruck am Gerät."""
        if self._key_cb and self._open and 0 <= key < self.key_count():
            self._key_cb(self, key, bool(pressed))

    def snapshot(self):
        """(seq, {Taste: Bild}, Helligkeit) – für das Fenster."""
        with self._lock:
            return self.seq, dict(self.images), self.brightness
