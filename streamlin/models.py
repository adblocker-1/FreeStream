"""Zuordnung der Elgato-Gerätecodes zu Geometrie und USB-Product-IDs."""
from dataclasses import dataclass

from .i18n import _


@dataclass(frozen=True)
class Geometry:
    cols: int
    rows: int
    dials: int = 0
    touch_keys: int = 0

    def __str__(self):
        s = f"{self.cols}x{self.rows}"
        if self.dials:
            s += f" +{self.dials} Dials"
        return s


# Elgato-Modellcode -> (Name, Geometrie)
MODELS = {
    "20GAA9901": ("Stream Deck (Original)", Geometry(5, 3)),
    "20GAA9902": ("Stream Deck (Original V2)", Geometry(5, 3)),
    "20GBA9901": ("Stream Deck MK.2", Geometry(5, 3)),
    "20GAI9901": ("Stream Deck Mini", Geometry(3, 2)),
    "20GAI9902": ("Stream Deck Mini MK.2", Geometry(3, 2)),
    "20GAT9901": ("Stream Deck XL", Geometry(8, 4)),
    "20GAT9902": ("Stream Deck XL V2", Geometry(8, 4)),
    "20GBD9901": ("Stream Deck +", Geometry(4, 2, dials=4)),
    "20GBX9901": ("Stream Deck + XL", Geometry(9, 4, dials=6)),
    "20GBJ9901": ("Stream Deck Neo", Geometry(4, 2, touch_keys=2)),
    "20GBF9901": ("Stream Deck Pedal", Geometry(3, 1)),
    "20GBO9901": ("Stream Deck Studio", Geometry(16, 2, dials=2)),
}

# USB Product-ID (Vendor 0x0fd9) -> Elgato-Modellcode
PID_TO_MODEL = {
    0x0060: "20GAA9901",
    0x006D: "20GAA9902",
    0x0080: "20GBA9901",
    0x00A5: "20GBA9901",
    0x00B9: "20GBA9901",
    0x0063: "20GAI9901",
    0x00B3: "20GAI9901",
    0x0090: "20GAI9902",
    0x00B8: "20GAI9902",
    0x006C: "20GAT9901",
    0x008F: "20GAT9902",
    0x00BA: "20GAT9902",
    0x0084: "20GBD9901",
    0x00C6: "20GBX9901",
    0x009A: "20GBJ9901",
    0x0086: "20GBF9901",
    0x00AA: "20GBO9901",
}

# Neben deck_type als Rückfall, falls eine PID unbekannt ist
DECKTYPE_TO_MODEL = {
    "Stream Deck Original": "20GBA9901",
    "Stream Deck Mini": "20GAI9902",
    "Stream Deck XL": "20GAT9902",
    "Stream Deck +": "20GBD9901",
    "Stream Deck + XL": "20GBX9901",
    "Stream Deck Neo": "20GBJ9901",
    "Stream Deck Pedal": "20GBF9901",
    "Stream Deck Studio": "20GBO9901",
}


def model_for_deck(deck) -> str:
    try:
        pid = deck.product_id()
    except Exception:
        pid = None
    code = PID_TO_MODEL.get(pid, "")
    if code and model_geometry(code) == geometry_for_deck(deck):
        return code
    return DECKTYPE_TO_MODEL.get(deck.deck_type(), code)


def geometry_for_deck(deck) -> Geometry:
    rows, cols = deck.key_layout()
    return Geometry(cols, rows, deck.dial_count(), getattr(deck, "TOUCH_KEY_COUNT", 0))


def elgato_name(code: str) -> str:
    """Modellname wie bei Elgato („Stream Deck XL“) – für Plugins, die ihn so erwarten."""
    base = (code or "").split("-")[0]
    if base.endswith("LL"):
        base = base[:-2]
    return MODELS.get(base, (code or _("unknown"), None))[0]


def model_name(code: str) -> str:
    """Modellname in LinDeck und Linewave: „LinDeck XL“, „LinDeck +“, „LinDeck Mini MK.2“ …"""
    return elgato_name(code).replace("Stream Deck", "LinDeck")


def model_geometry(code: str):
    base = (code or "").split("-")[0]
    if base.endswith("LL"):
        base = base[:-2]
    m = MODELS.get(base)
    return m[1] if m else None


ELGATO_VENDOR = "0fd9"
USB_ROOT = "/sys/bus/usb/devices"


def connected_models(usb_root=None):
    """Angeschlossene Stream Decks laut USB (sysfs) – ohne das Gerät zu öffnen, aus jedem Programm.

    Gibt [{"model", "name", "serial"}] zurück (Elgato-Vendor 0x0fd9, bekannte Product-IDs)."""
    import os
    root = usb_root or USB_ROOT
    out = []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return out

    def read(d, name):
        try:
            with open(os.path.join(root, d, name), encoding="ascii", errors="ignore") as f:
                return f.read().strip()
        except OSError:
            return ""
    for d in entries:
        if read(d, "idVendor").lower() != ELGATO_VENDOR:
            continue
        try:
            pid = int(read(d, "idProduct"), 16)
        except ValueError:
            continue
        code = PID_TO_MODEL.get(pid)
        if code:
            out.append({"model": code, "name": model_name(code), "serial": read(d, "serial")})
    return out
