"""Linewave-Kanäle auf das Stream Deck legen – als eigenes Profil „Linewave“.

Wie das Wave-Link-Plugin der Elgato-App: Linewave legt für ein Stream-Deck-
Modell ein Profil an, dessen Tasten bzw. Drehregler die Lautstärke der Mixer-
Kanäle steuern, und hält es aktuell, wenn Kanäle dazukommen oder wegfallen.

Aufbau wie das Wave-Link-Profil (z. B. Stream Deck XL):

  * oben die Mix-Wahl (Alle / Monitor / Stream, der gewählte grün umrandet)
    und „Kanäle 1–7“ (blättert weiter, wenn es mehr Seiten gibt)
  * je Kanal eine Spalte: Kachel mit Kanal-Symbol und Lautstärke (drücken =
    stumm), darunter ein Fader über zwei Tasten (oben lauter, unten leiser)
  * kleinere Geräte: Mix und Seiten in der letzten Spalte; zwei Reihen nur Fader
  * Geräte mit Drehreglern: Regler = Lautstärke, Tasten darüber = Kacheln

Passen nicht alle Kanäle auf eine Seite, entstehen weitere Seiten. Das Profil merkt sich in lindeck-meta.json, dass Linewave es
verwaltet (Kanalauswahl und Mix).
"""
import json
import os
import shutil

from . import paths
from .editdoc import EditError, ProfileDoc, create_profile, init_profile
from .i18n import _
from .models import MODELS, model_geometry, model_name
from .profiles import ProfileStore

MIXER_UUID = "com.lindeck.mixer.channel"
MIXSELECT_UUID, PAGES_UUID = "com.lindeck.mixer.mixselect", "com.lindeck.mixer.pages"
MIXES = ("selected", "both", "monitor", "stream")  # "selected": auf dem Deck wählbar
KEY_STEP, DIAL_STEP = 5, 2
META = "lindeck-meta.json"
PROFILE_NAME = "Linewave"


class ExportError(Exception):
    pass


# ------------------------------------------------------------------ Geräte
def _in_use_models(store):
    """Modelle der Profile, die gerade auf einem Gerät aktiv sind (state.json von LinDeck)."""
    try:
        data = json.loads(paths.STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    out = []
    for d in data.values():
        p = store.get(d.get("profile")) if isinstance(d, dict) and d.get("profile") else None
        if p and p.model and _base(p.model) not in out:
            out.append(_base(p.model))
    return out


def _base(model):
    base = (model or "").split("-")[0]
    return base[:-2] if base.endswith("LL") else base


def devices(store_root=None):
    """Stream-Deck-Modelle zur Auswahl: [{"model", "name", "in_use", "profile"}], benutzte zuerst.

    "profile" ist der Name eines schon vorhandenen Linewave-Profils für dieses Modell (sonst "")."""
    store = ProfileStore(store_root)
    used = _in_use_models(store)
    known = [_base(p.model) for p in store.profiles.values() if p.model]
    order = used + [m for m in known if m not in used] + [m for m in MODELS if m not in used and m not in known]
    out, seen = [], set()
    for m in order:
        if m in seen or m not in MODELS:
            continue
        seen.add(m)
        managed = managed_profile(m, store_root)
        out.append({"model": m, "name": model_name(m), "in_use": m in used,
                    "profile": managed.name if managed else ""})
    return out


# ------------------------------------------------------------------ Belegung
CHANNEL_KINDS = {"dial": "mute", "tile": "mute", "fader_top": "up", "fader_bottom": "down"}
SELECTORS = ("mix:both", "mix:monitor", "mix:stream")


def plan(geometry, channels, selectable=True):
    """Belegung je Seite: [[(slot, kanal, art), …], …] – ohne Dateien (testbar).

    Kanal-Arten: "dial" (Drehregler), "tile" (Kachel, stumm), "fader_top" / "fader_bottom"
    (Fader über zwei Tasten: oben lauter, unten leiser). Ohne Kanal: "mix:both|monitor|stream"
    (Mix-Wahl), "mix:cycle" (Mix der Reihe nach) und "pages" („Kanäle 1–7“, blättert weiter).
    selectable=False: feste Mischung, keine Mix-Wahl-Tasten."""
    cols, rows, dials = geometry.cols, geometry.rows, geometry.dials
    if not channels:
        raise ExportError(_("Choose at least one channel."))
    if rows < 2 and not dials:
        raise ExportError(_("This device has too few keys for volume controls."))
    extra = []  # (slot, art) auf jeder Seite
    if dials:
        per_page = dials
        bottom = rows - 1
        if rows >= 2:
            if selectable and cols >= 4:
                extra = [(("key", i, bottom), k) for i, k in enumerate(SELECTORS)]
            elif selectable:
                extra = [(("key", 0, bottom), "mix:cycle")]
            extra.append((("key", cols - 1, bottom), "pages"))

        def page(chs):
            out = [(("dial", i), c, "dial") for i, c in enumerate(chs)]
            return out + [(("key", i, 0), c, "tile") for i, c in enumerate(chs) if i < cols and rows >= 2]
    elif rows >= 4 and cols >= 4:
        per_page = cols
        extra = [(("key", i, 0), k) for i, k in enumerate(SELECTORS if selectable else ())]
        extra.append((("key", cols - 1, 0), "pages"))
        kinds = ("tile", "fader_top", "fader_bottom")

        def page(chs):
            return [(("key", i, r + 1), c, k) for i, c in enumerate(chs) for r, k in enumerate(kinds)]
    else:
        per_page = cols - 1  # letzte Spalte: Mix und Seiten
        kinds = ("tile", "fader_top", "fader_bottom") if rows >= 3 else ("fader_top", "fader_bottom")
        extra = ([(("key", cols - 1, 0), "mix:cycle")] if selectable else []) + \
            [(("key", cols - 1, 1 if selectable else 0), "pages")]

        def page(chs):
            return [(("key", i, r), c, k) for i, c in enumerate(chs) for r, k in enumerate(kinds)]
    if per_page < 1:
        raise ExportError(_("This device has too few keys for volume controls."))
    return [page(channels[i:i + per_page]) + [(slot, None, kind) for slot, kind in extra]
            for i in range(0, len(channels), per_page)]


def describe(geometry):
    """Kurzer Satz, wie das Profil aufgebaut wird."""
    if geometry.dials:
        return _("Dials: turn to change the volume, press to mute. The keys above show each channel and mute it.")
    if geometry.rows >= 3:
        return _("Per channel a tile (press to mute) and a fader over two keys: press the upper key for louder, "
                 "the lower one for quieter.")
    if geometry.rows == 2:
        return _("Per channel a fader over two keys: press the upper key for louder, the lower one for quieter.")
    return _("This device has too few keys for volume controls.")


def geometry_of(model):
    g = model_geometry(model)
    if g is None:
        raise ExportError(_("Unknown device."))
    return g


# ------------------------------------------------------------------ Profil
def _meta(root):
    try:
        return json.loads((root / META).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def managed_profile(model, store_root=None):
    """Das von Linewave verwaltete Profil für dieses Modell oder None."""
    for p in ProfileStore(store_root).profiles.values():
        if _base(p.model) == _base(model) and (_meta(p.root).get("linewave") is not None):
            return p
    return None


def managed_profiles(store_root=None):
    return [p for p in ProfileStore(store_root).profiles.values() if _meta(p.root).get("linewave") is not None]


def _fill(root, model, channels, mix):
    """Profil root neu belegen (alte Seiten werden ersetzt)."""
    doc = ProfileDoc(root)
    layout = plan(doc.geometry, channels, selectable=mix == "selected")
    first = doc.page_ids[0]
    pages = [first] + [doc.add_page() for _i in layout[1:]]
    for n, (page, entries) in enumerate(zip(pages, layout)):
        if len(layout) > 1:
            doc.rename_page(page, f"{PROFILE_NAME} {n + 1}")
        on_page = [channels.index(c) + 1 for _s, c, k in entries if c is not None]
        for slot, cid, kind in entries:
            if kind.startswith("mix:"):
                doc.place(page, slot, MIXSELECT_UUID)["Settings"] = {"mix": kind[4:]}
            elif kind == "pages":
                doc.place(page, slot, PAGES_UUID)["Settings"] = {"first": min(on_page), "last": max(on_page)}
            else:
                data = doc.place(page, slot, MIXER_UUID)
                data["Settings"].update({"channel": cid, "mix": mix, "action": CHANNEL_KINDS[kind],
                                         "step": DIAL_STEP if kind == "dial" else KEY_STEP})
                data["Settings"].pop("volume", None)
                if kind != "dial":
                    data["Settings"]["display"] = kind
    doc.save()
    return len(layout)


def export(model, channels, mix="selected", all_channels=True, store_root=None):
    """Linewave-Profil für dieses Modell anlegen oder neu belegen.

    channels: Kanal-ids in Reihenfolge. all_channels: später neue Kanäle automatisch dazunehmen.
    Gibt (Profilname, Seitenzahl, neu angelegt) zurück."""
    if mix not in MIXES:
        raise ExportError(mix)
    model = _base(model)
    plan(geometry_of(model), channels)  # vorher prüfen, damit kein halbes Profil entsteht
    store_root = store_root or paths.PROFILES_DIR
    existing = managed_profile(model, store_root)
    if existing is not None:
        root, name, created = existing.root, existing.name, False
        meta = _meta(root)
        shutil.rmtree(root / "Profiles", ignore_errors=True)
        init_profile(root, name, existing.model or model)
    else:
        name = PROFILE_NAME
        taken = {p.name for p in ProfileStore(store_root).profiles.values()}
        if name in taken:
            name = f"{PROFILE_NAME} ({model_name(model)})"
        root, created, meta = create_profile(name, model, store_root), True, {}
    try:
        pages = _fill(root, model, list(channels), mix)
    except EditError as e:
        raise ExportError(str(e)) from e
    meta["linewave"] = {"channels": list(channels), "all": bool(all_channels), "mix": mix}
    (root / META).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    os.utime(root)  # LinDeck lädt geänderte Profile neu
    return name, pages, created


def sync(channels, store_root=None):
    """Linewave-Profile an die aktuellen Kanäle anpassen (neue dazu, gelöschte weg).

    channels: alle Kanal-ids in Mixer-Reihenfolge. Gibt die Namen geänderter Profile zurück."""
    changed = []
    for p in managed_profiles(store_root):
        lw = _meta(p.root).get("linewave") or {}
        old = [c for c in lw.get("channels") or []]
        if lw.get("all", True):
            want = list(channels)
        else:
            want = [c for c in old if c in channels]
        if want == old or not want:
            continue
        try:
            export(p.model, want, lw.get("mix", "selected"), lw.get("all", True), store_root)
            changed.append(p.name)
        except ExportError:
            continue
    return changed
