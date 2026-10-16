"""Linewave-Kanäle auf das Stream Deck legen.

Legt in einem LinDeck-Profil neue Seiten „Linewave“ an, mit denen sich die
Lautstärke der gewählten Mixer-Kanäle direkt am Gerät steuern lässt:

  * Geräte mit Drehreglern (Stream Deck +, + XL, Studio): Regler drehen =
    Lautstärke, drücken = stumm; die Tasten darüber schalten stumm.
  * Nur Tasten, mindestens drei Reihen: je Kanal eine Spalte mit
    lauter / stumm (zeigt die Lautstärke) / leiser.
  * Zwei Reihen (Mini, Neo): je Kanal lauter / leiser.

Passen nicht alle Kanäle auf eine Seite, entstehen weitere Seiten.
"""
import json
import os

from . import paths
from .editdoc import EditError, ProfileDoc
from .i18n import _
from .models import model_geometry, model_name
from .profiles import ProfileStore

MIXER_UUID = "com.lindeck.mixer.channel"
MIXES = ("both", "monitor", "stream")
KEY_STEP, DIAL_STEP = 5, 2


class ExportError(Exception):
    pass


def _in_use():
    """Profil-IDs, die gerade auf einem Gerät aktiv sind (state.json von LinDeck)."""
    try:
        data = json.loads(paths.STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {d.get("profile") for d in data.values() if isinstance(d, dict) and d.get("profile")}


def targets(store_root=None):
    """Profile als Ziel: [{"id", "name", "model", "in_use"}], gerade benutzte zuerst."""
    store = ProfileStore(store_root)
    used = _in_use()
    out = []
    for p in store.profiles.values():
        out.append({"id": p.id, "name": p.name, "model": model_name(p.model) if p.model else "",
                    "in_use": p.id in used, "root": str(p.root)})
    return sorted(out, key=lambda t: (not t["in_use"], t["name"].lower()))


def plan(geometry, channels):
    """Belegung je Seite: [[(slot, kanal, aktion), …], …] – rein, ohne Dateien (testbar)."""
    cols, rows, dials = geometry.cols, geometry.rows, geometry.dials
    if dials:
        per_page = dials
        def page(chs):
            out = [(("dial", i), c, "dial") for i, c in enumerate(chs)]
            if rows:
                out += [(("key", i, 0), c, "mute") for i, c in enumerate(chs) if i < cols]
            return out
    elif rows >= 3:
        per_page = cols
        def page(chs):
            out = []
            for i, c in enumerate(chs):
                out += [(("key", i, 0), c, "up"), (("key", i, 1), c, "mute"), (("key", i, 2), c, "down")]
            return out
    elif rows == 2:
        per_page = cols
        def page(chs):
            out = []
            for i, c in enumerate(chs):
                out += [(("key", i, 0), c, "up"), (("key", i, 1), c, "down")]
            return out
    else:
        raise ExportError(_("This device has too few keys for volume controls."))
    if not channels:
        raise ExportError(_("Choose at least one channel."))
    return [page(channels[i:i + per_page]) for i in range(0, len(channels), per_page)]


def describe(geometry):
    """Kurzer Satz, wie die Seite aufgebaut wird."""
    if geometry.dials:
        return _("Dials: turn to change the volume, press to mute. The keys above mute the channels.")
    if geometry.rows >= 3:
        return _("One column per channel: volume up, mute (shows the volume), volume down.")
    if geometry.rows == 2:
        return _("One column per channel: volume up and volume down.")
    return _("This device has too few keys for volume controls.")


def profile_geometry(profile_id, store_root=None):
    p = ProfileStore(store_root).get(profile_id)
    if p is None:
        raise ExportError(_("Profile not found."))
    return model_geometry(p.model) or p.geometry


def export(profile_id, channels, mix="both", store_root=None):
    """Seiten anlegen; channels: [(id, name), …]. Gibt die Namen der neuen Seiten zurück."""
    if mix not in MIXES:
        raise ExportError(mix)
    p = ProfileStore(store_root).get(profile_id)
    if p is None:
        raise ExportError(_("Profile not found."))
    try:
        doc = ProfileDoc(p.root)
    except EditError as e:
        raise ExportError(str(e)) from e
    geometry = doc.geometry
    layout = plan(geometry, [cid for cid, _name in channels])
    created = []
    last = doc.page_ids[-1] if doc.page_ids else None
    for n, entries in enumerate(layout):
        page = doc.add_page(after=last)
        last = page
        title = "Linewave" if len(layout) == 1 else f"Linewave {n + 1}"
        doc.rename_page(page, title)
        for slot, cid, kind in entries:
            data = doc.place(page, slot, MIXER_UUID)
            data["Settings"].update({"channel": cid, "mix": mix,
                                     "action": "mute" if kind == "dial" else kind,
                                     "step": DIAL_STEP if kind == "dial" else KEY_STEP})
            data["Settings"].pop("volume", None)
        created.append(title)
    doc.save()
    os.utime(p.root)  # LinDeck lädt geänderte Profile neu
    return created
