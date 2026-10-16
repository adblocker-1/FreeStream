"""Profile bearbeiten – die Logik hinter dem grafischen Editor (ohne GUI).

Ein ProfileDoc hält das Profil-Manifest und alle Seiten-Manifeste im
Speicher, ändert sie und schreibt sie im Originalformat der Elgato-App
zurück. Unbekannte Felder (z. B. von Plugins) bleiben dabei erhalten.

Plätze (Slots) auf einer Seite:  ("key", Spalte, Zeile)  oder  ("dial", Index)

Seitenordner werden sofort angelegt, aber erst beim Schließen (cleanup)
gelöscht – so kann jede Änderung rückgängig gemacht werden.
"""
import copy
import json
import os
import shutil
import time
import uuid as uuidlib
import zipfile
from pathlib import Path

from PIL import Image, ImageOps

from . import actions as A
from . import catalog as C
from . import paths, render
from .i18n import _
from .models import Geometry, model_geometry
from .profiles import decode_page_dir, encode_page_dir, load_page, read_json

UNDO_LIMIT = 200
ICON_SIZE = 144


class EditError(Exception):
    pass


def slot_ref(slot):
    """Slot -> (Controller-Typ, Positionsschlüssel)."""
    if slot[0] == "dial":
        return "Encoder", f"{slot[1]},0"
    return "Keypad", f"{slot[1]},{slot[2]}"


def _norm(u: str) -> str:
    return (u or "").lower()


def _write_json(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


class EditPage:
    def __init__(self, uuid: str, d: Path, manifest: dict):
        self.uuid = uuid
        self.dir = Path(d)
        self.manifest = manifest

    @property
    def name(self):
        return self.manifest.get("Name") or ""

    def controller(self, ctype: str, create=False):
        ctrls = self.manifest.get("Controllers")
        if ctrls is None:
            if "Actions" in self.manifest:  # altes Format: nur Tasten
                self.manifest["Controllers"] = ctrls = [{"Type": "Keypad", "Actions": self.manifest.pop("Actions")}]
            else:
                self.manifest["Controllers"] = ctrls = []
        for c in ctrls:
            if (c.get("Type") or "Keypad") == ctype:
                if c.get("Actions") is None:
                    c["Actions"] = {}
                return c
        if not create:
            return None
        c = {"Actions": {}, "Type": ctype}
        ctrls.append(c)
        return c

    def get(self, slot):
        ctype, pos = slot_ref(slot)
        c = self.controller(ctype)
        a = (c or {}).get("Actions", {}).get(pos)
        return a if isinstance(a, dict) and a.get("UUID") else None

    def set(self, slot, data):
        ctype, pos = slot_ref(slot)
        c = self.controller(ctype, create=data is not None)
        if c is None:
            return
        if data is None:
            c["Actions"].pop(pos, None)
        else:
            c["Actions"][pos] = data

    def items(self):
        out = []
        for ctype, kind in (("Keypad", "key"), ("Encoder", "dial")):
            c = self.controller(ctype)
            for pos, a in ((c or {}).get("Actions") or {}).items():
                if not (isinstance(a, dict) and a.get("UUID")):
                    continue
                col, row = (int(x) for x in pos.split(","))
                out.append((("key", col, row) if kind == "key" else ("dial", col), a))
        return out

    def runtime_page(self):
        """profiles.Page für Vorschau/Rendering."""
        return load_page(self.uuid, self.dir, self.manifest)


def _walk_actions(data):
    """Aktion und alle verschachtelten Aktionen."""
    if not isinstance(data, dict):
        return
    if data.get("UUID"):
        yield data
    for e in data.get("Actions") or []:
        if isinstance(e, dict) and e.get("UUID"):
            yield from _walk_actions(e)
        elif isinstance(e, dict):
            for x in e.get("Actions") or []:
                yield from _walk_actions(x)


def _image_refs(data):
    """(dict, Schlüssel) aller Bildverweise in einer Aktion (rekursiv)."""
    for a in _walk_actions(data):
        for st in a.get("States") or []:
            if isinstance(st, dict) and st.get("Image"):
                yield st, "Image"
        enc = a.get("Encoder")
        if isinstance(enc, dict):
            for k in ("Icon", "background", "Background"):
                if enc.get(k):
                    yield enc, k


class ProfileDoc:
    def __init__(self, root):
        self.root = Path(root)
        if not (self.root / "manifest.json").exists():
            raise EditError(_("Not a profile: {path}").format(path=self.root))
        self.id = self.root.name[: -len(".sdProfile")] if self.root.name.endswith(".sdProfile") else self.root.name
        self.manifest = read_json(self.root / "manifest.json")
        self.pages = {}
        base = self.root / "Profiles"
        if base.is_dir():
            for d in sorted(base.iterdir()):
                if not (d / "manifest.json").exists():
                    continue
                try:
                    u = decode_page_dir(d.name)
                    self.pages[_norm(u)] = EditPage(u, d, read_json(d / "manifest.json"))
                except (ValueError, OSError):
                    continue
        if not self.pages and "Actions" in self.manifest:
            raise EditError(_("Profiles in format v1 (Stream Deck app 4.x) cannot be edited"))
        self.v3_names = any(self._is_uuid_name(p.dir.name) for p in self.pages.values())
        # Verweise im Manifest auf die tatsächliche Schreibweise der Seiten-UUIDs abbilden
        for pid in self.page_ids:
            pg = self.pages.get(_norm(pid))
            if pg:
                pg.uuid = pid
        self._disk = {k: json.dumps(p.manifest, sort_keys=True) for k, p in self.pages.items()}
        self._disk_profile = json.dumps(self.manifest, sort_keys=True)
        self._initially_reachable = self.reachable()
        self._created_dirs = set()
        self._created_images = set()
        self.undo_stack, self.redo_stack = [], []
        self._last_change = (None, 0.0)
        self.on_change = None

    @staticmethod
    def _is_uuid_name(name):
        try:
            uuidlib.UUID(name)
            return "-" in name
        except ValueError:
            return False

    # ================================================================ Profil
    @property
    def name(self):
        return self.manifest.get("Name") or self.id

    @property
    def model(self):
        dev = self.manifest.get("Device") or {}
        return dev.get("Model", "") if isinstance(dev, dict) else ""

    @property
    def geometry(self) -> Geometry:
        g = model_geometry(self.model)
        if g:
            return g
        cols = rows = dials = 0
        for p in self.pages.values():
            for slot, __ in p.items():
                if slot[0] == "key":
                    cols, rows = max(cols, slot[1] + 1), max(rows, slot[2] + 1)
                else:
                    dials = max(dials, slot[1] + 1)
        return Geometry(cols or 5, rows or 3, dials)

    def rename(self, name: str):
        self.checkpoint()
        self.manifest["Name"] = name.strip() or self.name
        self._changed()

    def set_model(self, code: str):
        self.checkpoint()
        dev = self.manifest.setdefault("Device", {})
        dev["Model"] = code
        self._changed()

    # ================================================================ Seiten
    @property
    def page_ids(self):
        return list((self.manifest.get("Pages") or {}).get("Pages") or [])

    def page(self, uuid: str):
        return self.pages.get(_norm(uuid))

    def is_folder(self, uuid: str) -> bool:
        return _norm(uuid) not in {_norm(p) for p in self.page_ids}

    def folder_refs(self, data):
        return [a.get("Settings", {}).get("ProfileUUID") for a in _walk_actions(data)
                if a.get("UUID") == C.FOLDER_UUID and (a.get("Settings") or {}).get("ProfileUUID")]

    def reachable(self):
        """Alle Seiten, die über Seitenliste und Ordner erreichbar sind."""
        seen, todo = set(), [_norm(p) for p in self.page_ids]
        while todo:
            k = todo.pop()
            if k in seen or k not in self.pages:
                continue
            seen.add(k)
            for __, a in self.pages[k].items():
                todo += [_norm(u) for u in self.folder_refs(a)]
        return seen

    def folder_title(self, uuid: str) -> str:
        pg = self.page(uuid)
        if pg and pg.name:
            return pg.name
        for p in self.pages.values():
            for __, a in p.items():
                if a.get("UUID") == C.FOLDER_UUID and _norm((a.get("Settings") or {}).get("ProfileUUID")) == _norm(uuid):
                    st = (a.get("States") or [{}])[0]
                    return st.get("Title") or _("Folder")
        return _("Folder")

    def _new_page(self, name="", folder=False):
        u = str(uuidlib.uuid4())
        dname = u.upper() if self.v3_names else encode_page_dir(u)
        d = self.root / "Profiles" / dname
        d.mkdir(parents=True, exist_ok=True)
        self._created_dirs.add(d)
        ctrls = [{"Actions": {}, "Type": "Keypad"}]
        if self.geometry.dials:
            ctrls.append({"Actions": {}, "Type": "Encoder"})
        pg = EditPage(u, d, {"Controllers": ctrls, "Icon": "", "Name": name})
        self.pages[_norm(u)] = pg
        if folder:
            back = C.new_action(C.BACK_UUID)
            pg.set(("key", 0, 0), back)
        return pg

    def add_page(self, after=None) -> str:
        self.checkpoint()
        pg = self._new_page()
        pages = self.manifest.setdefault("Pages", {})
        ids = list(pages.get("Pages") or [])
        idx = len(ids) if after is None else ids.index(after) + 1
        ids.insert(idx, pg.uuid)
        pages["Pages"] = ids
        if not pages.get("Default"):
            pages["Default"] = pg.uuid
        if not pages.get("Current"):
            pages["Current"] = pg.uuid
        self._changed()
        return pg.uuid

    def remove_page(self, uuid: str):
        ids = self.page_ids
        if len(ids) <= 1:
            raise EditError(_("The last page cannot be deleted"))
        self.checkpoint()
        ids = [p for p in ids if _norm(p) != _norm(uuid)]
        pages = self.manifest["Pages"]
        pages["Pages"] = ids
        for k in ("Default", "Current"):
            if _norm(pages.get(k)) == _norm(uuid):
                pages[k] = ids[0]
        self._changed()

    def move_page(self, uuid: str, delta: int):
        ids = self.page_ids
        i = next(i for i, p in enumerate(ids) if _norm(p) == _norm(uuid))
        j = max(0, min(len(ids) - 1, i + delta))
        if i == j:
            return
        self.checkpoint()
        ids.insert(j, ids.pop(i))
        self.manifest["Pages"]["Pages"] = ids
        self._changed()

    def rename_page(self, uuid: str, name: str):
        self.checkpoint(("page-name", uuid))
        self.page(uuid).manifest["Name"] = name
        self._changed()

    # ================================================================ Tasten
    def get(self, page_uuid, slot):
        pg = self.page(page_uuid)
        return pg.get(slot) if pg else None

    def place(self, page_uuid, slot, type_uuid) -> dict:
        """Neue Aktion aus dem Katalog auf einen Platz legen (ersetzt den alten Inhalt)."""
        self.checkpoint()
        data = C.new_action(type_uuid)
        if type_uuid == C.FOLDER_UUID:
            folder = self._new_page(folder=True)
            data["Settings"]["ProfileUUID"] = folder.uuid
            data["States"][0]["Title"] = _("Folder")
        self.page(page_uuid).set(slot, data)
        self._changed()
        return data

    def remove(self, page_uuid, slot):
        if self.get(page_uuid, slot) is None:
            return
        self.checkpoint()
        self.page(page_uuid).set(slot, None)
        self._changed()

    def move(self, src_page, src_slot, dst_page, dst_slot):
        """Verschiebt eine Aktion; ein belegter Zielplatz wird getauscht."""
        if (_norm(src_page), tuple(src_slot)) == (_norm(dst_page), tuple(dst_slot)):
            return
        a = self.get(src_page, src_slot)
        if a is None:
            return
        b = self.get(dst_page, dst_slot)
        self.checkpoint()
        sp, dp = self.page(src_page), self.page(dst_page)
        self._relocate(a, sp.dir, dp.dir)
        if b is not None:
            self._relocate(b, dp.dir, sp.dir)
        sp.set(src_slot, b)
        dp.set(dst_slot, a)
        self._changed()

    def copy(self, page_uuid, slot):
        """Inhalt für die Zwischenablage (unabhängig vom Profil)."""
        a = self.get(page_uuid, slot)
        if a is None:
            return None
        return {"data": copy.deepcopy(a), "dir": str(self.page(page_uuid).dir), "doc": self}

    def paste(self, page_uuid, slot, clip) -> dict:
        if not clip:
            return None
        self.checkpoint()
        data = copy.deepcopy(clip["data"])
        src_doc = clip.get("doc")
        dst = self.page(page_uuid)
        self._relocate(data, Path(clip["dir"]), dst.dir)
        for a in _walk_actions(data):
            a["ActionID"] = str(uuidlib.uuid4())
            if a.get("UUID") == C.FOLDER_UUID:
                src_folder = src_doc.page(a["Settings"].get("ProfileUUID", "")) if src_doc else None
                a["Settings"]["ProfileUUID"] = self._clone_folder(src_folder) if src_folder else \
                    self._new_page(folder=True).uuid
        dst.set(slot, data)
        self._changed()
        return data

    def _clone_folder(self, src: EditPage) -> str:
        pg = self._new_page(name=src.name)
        pg.manifest = copy.deepcopy(src.manifest)
        for slot, a in pg.items():
            self._relocate(a, src.dir, pg.dir)
            for x in _walk_actions(a):
                x["ActionID"] = str(uuidlib.uuid4())
                if x.get("UUID") == C.FOLDER_UUID:
                    sub = self._doc_page_any(x["Settings"].get("ProfileUUID", ""), src)
                    x["Settings"]["ProfileUUID"] = self._clone_folder(sub) if sub else self._new_page(folder=True).uuid
        return pg.uuid

    def _doc_page_any(self, uuid, src_page):
        # Ordner können aus einem anderen Profil stammen: im selben Profilordner suchen
        if self.page(uuid):
            return self.page(uuid)
        base = src_page.dir.parent
        for d in base.iterdir():
            try:
                if _norm(decode_page_dir(d.name)) == _norm(uuid) and (d / "manifest.json").exists():
                    return EditPage(uuid, d, read_json(d / "manifest.json"))
            except ValueError:
                continue
        return None

    def _relocate(self, data, src_dir: Path, dst_dir: Path):
        """Bilder einer Aktion in den Ordner der Zielseite kopieren."""
        if Path(src_dir) == Path(dst_dir):
            return
        for holder, key in _image_refs(data):
            rel = holder[key]
            src = Path(src_dir) / rel
            if not src.is_file():
                continue
            dst = Path(dst_dir) / rel
            if dst.exists() and dst.read_bytes() != src.read_bytes():
                rel = f"Images/{uuidlib.uuid4().hex.upper()}{src.suffix}"
                dst = Path(dst_dir) / rel
                holder[key] = rel
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                self._created_images.add(dst)

    # ------------------------------------------------------- Eigenschaften
    def update_settings(self, page_uuid, slot, key, value, action=None):
        a = action if action is not None else self.get(page_uuid, slot)
        if a is None:
            return
        self.checkpoint(("set", page_uuid, tuple(slot), id(a), key))
        a.setdefault("Settings", {})[key] = value
        self._changed()

    def update_state(self, page_uuid, slot, state_idx, key, value, action=None):
        a = action if action is not None else self.get(page_uuid, slot)
        if a is None:
            return
        self.checkpoint(("state", page_uuid, tuple(slot), id(a), state_idx, key))
        states = a.setdefault("States", [])
        while len(states) <= state_idx:
            states.append(C.default_state())
        states[state_idx][key] = value
        self._changed()

    def set_hotkey(self, page_uuid, slot, idx, hk, action=None):
        a = action if action is not None else self.get(page_uuid, slot)
        if a is None:
            return
        self.checkpoint()
        hks = a.setdefault("Settings", {}).setdefault("Hotkeys", [])
        while len(hks) <= idx:
            hks.append(C.empty_hotkey())
        hks[idx] = hk
        self._changed()

    def set_icon(self, page_uuid, slot, state_idx, image_file, action=None):
        """Bilddatei übernehmen (PNG/JPG/SVG/GIF) und dem Zustand zuweisen."""
        a = action if action is not None else self.get(page_uuid, slot)
        if a is None:
            return None
        rel = self.import_image(page_uuid, image_file)
        self.update_state(page_uuid, slot, state_idx, "Image", rel, action=a)
        return rel

    def import_image(self, page_uuid, image_file) -> str:
        src = Path(image_file).expanduser()
        pg = self.page(page_uuid)
        img_dir = pg.dir / "Images"
        img_dir.mkdir(parents=True, exist_ok=True)
        name = uuidlib.uuid4().hex.upper()
        ext = src.suffix.lower()
        if ext in (".svg", ".gif"):
            dst = img_dir / (name + ext)
            shutil.copy2(src, dst)
        else:
            try:
                im = Image.open(src).convert("RGBA")
            except (OSError, ValueError) as e:
                raise EditError(_("Image not readable: {error}").format(error=e)) from e
            im.thumbnail((ICON_SIZE, ICON_SIZE), Image.LANCZOS)
            canvas = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
            canvas.alpha_composite(im, ((ICON_SIZE - im.size[0]) // 2, (ICON_SIZE - im.size[1]) // 2))
            dst = img_dir / (name + ".png")
            canvas.save(dst)
        self._created_images.add(dst)
        return f"Images/{dst.name}"

    # ------------------------------------------------- Hintergrundbilder
    def background(self, page_uuid):
        """(Bildpfad oder None, Abdunkeln in %) des Seiten-Hintergrunds."""
        pg = self.page(page_uuid)
        bg = (pg.manifest.get("LinDeckBackground") or {}) if pg else {}
        p = pg.dir / bg["Image"] if bg.get("Image") else None
        return (p if p and p.exists() else None), int(bg.get("Dim") or 0)

    def set_background(self, page_uuid, image_file=None, dim=None, keep_image=False):
        """Hintergrundbild über alle Tasten setzen (None = entfernen); dim 0–90 %."""
        pg = self.page(page_uuid)
        self.checkpoint()
        cur = pg.manifest.get("LinDeckBackground") or {}
        if image_file is None and not keep_image:
            pg.manifest.pop("LinDeckBackground", None)
        else:
            rel = self._import_background(page_uuid, image_file) if image_file else cur.get("Image")
            pg.manifest["LinDeckBackground"] = {"Image": rel, "Dim": int(cur.get("Dim", 30) if dim is None else dim)}
        self._changed()

    def strip_background(self, page_uuid):
        pg = self.page(page_uuid)
        c = pg.controller("Encoder") if pg else None
        rel = (c or {}).get("Background")
        p = pg.dir / rel if rel else None
        return p if p and p.exists() else None

    def set_strip_background(self, page_uuid, image_file=None):
        """Hintergrund des Touchstreifens (Stream Deck +, wie in der Elgato-App)."""
        pg = self.page(page_uuid)
        self.checkpoint()
        c = pg.controller("Encoder", create=image_file is not None)
        if c is not None:
            if image_file is None:
                c.pop("Background", None)
            else:
                c["Background"] = self._import_background(page_uuid, image_file, strip=True)
        self._changed()

    def _import_background(self, page_uuid, image_file, strip=False):
        """Bild unverändert (nur verkleinert) in den Images-Ordner der Seite übernehmen."""
        src = Path(image_file).expanduser()
        pg = self.page(page_uuid)
        img_dir = pg.dir / "Images"
        img_dir.mkdir(parents=True, exist_ok=True)
        try:
            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((1600, 400) if strip else (1600, 1600), Image.LANCZOS)
                dst = img_dir / (("Strip" if strip else "Background") + uuidlib.uuid4().hex[:12].upper() + ".png")
                im.save(dst)
        except (OSError, ValueError) as e:
            raise EditError(_("Image not readable: {error}").format(error=e)) from e
        self._created_images.add(dst)
        return f"Images/{dst.name}"

    def set_settings_json(self, page_uuid, slot, settings: dict, action=None):
        a = action if action is not None else self.get(page_uuid, slot)
        if a is None:
            return
        self.checkpoint()
        a["Settings"] = settings
        self._changed()

    def changed(self):
        """Nach direkter Bearbeitung eines Aktions-Dicts (vorher checkpoint() aufrufen)."""
        self._changed()

    def disk_changed(self) -> bool:
        """Wurde das Profil-Manifest außerhalb geändert (z. B. neue Seite aus Linewave)?"""
        try:
            disk = read_json(self.root / "manifest.json")
        except (OSError, ValueError):
            return False
        return json.dumps(disk, sort_keys=True) != self._disk_profile

    def reload_external(self):
        """Seiten neu einlesen, die außerhalb des Editors geändert wurden (z. B. von einer Plugin-
        Einstellungsseite). Seiten mit ungespeicherten Änderungen im Editor bleiben unberührt."""
        changed = []
        for k, p in self.pages.items():
            f = p.dir / "manifest.json"
            try:
                disk = read_json(f)
            except (OSError, ValueError):
                continue
            text = json.dumps(disk, sort_keys=True)
            if text == self._disk.get(k):
                continue
            if json.dumps(p.manifest, sort_keys=True) == self._disk.get(k):
                p.manifest = disk
                self._disk[k] = text
                changed.append(p.uuid)
        return changed

    # ================================================================ Undo
    def _snapshot(self):
        return (copy.deepcopy(self.manifest),
                {k: (p.uuid, p.dir, copy.deepcopy(p.manifest)) for k, p in self.pages.items()})

    def _restore(self, snap):
        self.manifest = copy.deepcopy(snap[0])
        self.pages = {k: EditPage(u, d, copy.deepcopy(m)) for k, (u, d, m) in snap[1].items()}

    def checkpoint(self, coalesce=None):
        """Vor jeder Änderung aufrufen. Gleiche coalesce-Schlüssel kurz hintereinander = ein Schritt."""
        now = time.monotonic()
        if coalesce is not None and self._last_change[0] == coalesce and now - self._last_change[1] < 2.0:
            self._last_change = (coalesce, now)
            return
        self._last_change = (coalesce, now)
        self.undo_stack.append(self._snapshot())
        del self.undo_stack[:-UNDO_LIMIT]
        self.redo_stack.clear()

    def undo(self) -> bool:
        if not self.undo_stack:
            return False
        self.redo_stack.append(self._snapshot())
        self._restore(self.undo_stack.pop())
        self._last_change = (None, 0.0)
        self._changed()
        return True

    def redo(self) -> bool:
        if not self.redo_stack:
            return False
        self.undo_stack.append(self._snapshot())
        self._restore(self.redo_stack.pop())
        self._last_change = (None, 0.0)
        self._changed()
        return True

    def _changed(self):
        if self.on_change:
            self.on_change()

    # ============================================================ Speichern
    @property
    def dirty(self) -> bool:
        if json.dumps(self.manifest, sort_keys=True) != self._disk_profile:
            return True
        reach = self.reachable()
        return any(json.dumps(self.pages[k].manifest, sort_keys=True) != self._disk.get(k) for k in reach)

    def save(self) -> bool:
        """Geänderte Manifeste schreiben. Gibt True zurück, wenn etwas geschrieben wurde."""
        wrote = False
        for k in self.reachable():
            p = self.pages[k]
            text = json.dumps(p.manifest, sort_keys=True)
            if text != self._disk.get(k) or not (p.dir / "manifest.json").exists():
                p.dir.mkdir(parents=True, exist_ok=True)
                _write_json(p.dir / "manifest.json", p.manifest)
                self._disk[k] = text
                wrote = True
        text = json.dumps(self.manifest, sort_keys=True)
        if text != self._disk_profile:
            _write_json(self.root / "manifest.json", self.manifest)
            self._disk_profile = text
            wrote = True
        if wrote:
            # Der laufende Dienst lädt Profile neu, sobald sich der Profilordner ändert
            try:
                os.utime(self.root)
            except OSError:
                pass
        return wrote

    def cleanup(self):
        """Nicht mehr erreichbare Seiten und ungenutzte neue Bilder entfernen (beim Schließen)."""
        self.save()
        reach = self.reachable()
        removable = {k for k in self.pages if k not in reach and
                     (k in self._initially_reachable or self.pages[k].dir in self._created_dirs)}
        for k in removable:
            shutil.rmtree(self.pages[k].dir, ignore_errors=True)
            self.pages.pop(k, None)
            self._disk.pop(k, None)
        used = set()
        for k in reach:
            p = self.pages[k]
            for __, a in p.items():
                for holder, key in _image_refs(a):
                    used.add((p.dir / holder[key]).resolve())
            bg = (p.manifest.get("LinDeckBackground") or {}).get("Image")
            strip = (p.controller("Encoder") or {}).get("Background")
            for rel in (bg, strip):
                if rel:
                    used.add((p.dir / rel).resolve())
        for f in list(self._created_images):
            if f.exists() and f.resolve() not in used:
                f.unlink()
        self._created_images.clear()
        self._created_dirs.clear()
        self._initially_reachable = reach
        self.undo_stack.clear()
        self.redo_stack.clear()

    def export(self, target) -> Path:
        """Als .streamDeckProfile exportieren (ZIP mit <ID>.sdProfile/)."""
        self.save()
        target = Path(target).expanduser()
        if target.suffix != ".streamDeckProfile":
            target = target.with_name(target.name + ".streamDeckProfile")
        reach_dirs = {self.pages[k].dir.resolve() for k in self.reachable()}
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(self.root / "manifest.json", f"{self.root.name}/manifest.json")
            base = self.root / "Profiles"
            for d in sorted(base.iterdir()) if base.is_dir() else []:
                if d.resolve() not in reach_dirs:
                    continue
                for f in sorted(d.rglob("*")):
                    if f.is_file() and not f.name.endswith(".tmp"):
                        z.write(f, f"{self.root.name}/{f.relative_to(self.root).as_posix()}")
        return target

    # ============================================================ Vorschau
    def runtime_page(self, page_uuid):
        pg = self.page(page_uuid)
        return pg.runtime_page() if pg else None


# ----------------------------------------------------------------------
# Profile anlegen, duplizieren
# ----------------------------------------------------------------------
def create_profile(name: str, model: str, store_root=None) -> Path:
    """Leeres Profil im Format v2 der Elgato-App anlegen."""
    store_root = Path(store_root or paths.PROFILES_DIR)
    pid = str(uuidlib.uuid4()).upper()
    root = store_root / f"{pid}.sdProfile"
    page = str(uuidlib.uuid4())
    pdir = root / "Profiles" / encode_page_dir(page)
    pdir.mkdir(parents=True)
    ctrls = [{"Actions": {}, "Type": "Keypad"}]
    g = model_geometry(model)
    if g and g.dials:
        ctrls.append({"Actions": {}, "Type": "Encoder"})
    _write_json(pdir / "manifest.json", {"Controllers": ctrls, "Icon": "", "Name": ""})
    _write_json(root / "manifest.json", {
        "Device": {"Model": model, "UUID": ""},
        "Name": name.strip() or _("New profile"),
        "Pages": {"Current": page, "Default": page, "Pages": [page]},
        "Version": "2.0",
    })
    return root


def duplicate_profile(root, name: str) -> Path:
    root = Path(root)
    pid = str(uuidlib.uuid4()).upper()
    dst = root.parent / f"{pid}.sdProfile"
    shutil.copytree(root, dst, ignore=shutil.ignore_patterns("*.tmp"))
    m = read_json(dst / "manifest.json")
    m["Name"] = name
    m["AppIdentifier"] = ""  # Smart-Profile-Zuordnung nicht doppelt
    _write_json(dst / "manifest.json", m)
    os.utime(dst.parent)
    return dst


# ----------------------------------------------------------------------
# Vorschaubilder ohne Gerät
# ----------------------------------------------------------------------
class _PreviewProfile:
    def __init__(self, doc):
        self.page_ids = doc.page_ids
        self.id = doc.id

    def resource_path(self, res_id):
        cand = paths.RESOURCES_DIR / (res_id or "")
        if res_id and cand.is_dir():
            files = [f for f in cand.iterdir() if f.is_file()]
            return files[0] if files else None
        return None


class _PreviewCtrl:
    """Was Aktionen beim Zeichnen vom Controller erwarten."""
    preview = True

    def __init__(self, doc, page_index=0):
        self.profile = _PreviewProfile(doc)
        self.page_index = page_index
        self.brightness = 70
        self.folder_stack = []

    def redraw_action(self, action):
        pass

    def show_overlay(self, action, kind, seconds=1.2):
        pass


class _Cfg(dict):
    pass


def preview_action(doc, page_uuid, data, size=(72, 72), page_index=0, state=None, base=None):
    """Bild einer Taste wie auf dem Gerät (base: Kachel des Hintergrundbilds)."""
    from .config import DEFAULTS
    pg = doc.runtime_page(page_uuid)
    ctx = A.Context(_PreviewCtrl(doc, page_index), _Cfg(DEFAULTS), None, None)
    act = A.create_action(data, pg, ctx)
    if state is not None and act.states:
        act.state = max(0, min(state, len(act.states) - 1))
    try:
        frames = act.frames(size)
    except Exception:  # noqa: BLE001 - Vorschau darf nie abbrechen
        frames = [(render.fallback_icon(size[0], size[1], C.display_name(data)), 0)]
    frame = frames[0][0] if frames else None
    return render.compose_key(size, frame, act.title(), act.state_dict(), base=base)


def preview_dial(doc, page_uuid, data, size=(100, 100)):
    """Segment des Touchstreifens (ohne Live-Werte)."""
    from .config import DEFAULTS
    pg = doc.runtime_page(page_uuid)
    ctx = A.Context(_PreviewCtrl(doc), _Cfg(DEFAULTS), None, None)
    act = A.create_action(data, pg, ctx)
    try:
        path = act.encoder_icon_path()
    except Exception:  # noqa: BLE001
        path = None
    icon = render.load_frames(path, 48, 48) if path else None
    try:
        own = getattr(act, "encoder_icon_image", None)  # selbst gezeichnetes Symbol (z. B. Live-Tasten)
    except Exception:  # noqa: BLE001
        own = None
    if own is not None:
        icon = [(own, 0)]
    title = act.title() or C.display_name(data)
    return render.compose_dial_segment(size, icon[0][0] if icon else None, title, "", None)
