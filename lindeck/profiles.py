"""Elgato-Profilformat lesen und importieren.

Unterstützt:
  * .streamDeckProfile (Format v2 = ZIP mit <UUID>.sdProfile/,
    Format v3 = ZIP mit package.json, Profiles/<UUID>.sdProfile/ und Resources/)
  * Elgato-Backups (.streamDeckProfilesBackup) – ZIP mit vielen Profilen
  * kopierte Windows-Ordner %APPDATA%\\Elgato\\StreamDeck\\ProfilesV2 / ProfilesV3

Ein Profil hat Seiten (Pages); Ordner (Folders) sind Unterseiten, die per
com.elgato.streamdeck.profile.openchild über ihre UUID geöffnet werden.
Seiten-Ordnernamen sind entweder die UUID in Großbuchstaben (v3) oder eine
Base32-Kodierung der UUID mit Elgato-Alphabet plus "Z" (v2).
"""
import base64
import json
import shutil
import tempfile
import uuid as uuidlib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from .models import Geometry, model_geometry, model_name

_B32_FIX = str.maketrans("UV", "VW")
_B32_UNFIX = str.maketrans("VW", "UV")


def encode_page_dir(page_uuid: str) -> str:
    """UUID -> Ordnername im v2-Format (base32hex, U/V verschoben, Suffix Z)."""
    raw = uuidlib.UUID(page_uuid).bytes
    return base64.b32hexencode(raw).decode().rstrip("=").translate(_B32_FIX) + "Z"


def decode_page_dir(name: str) -> str:
    """Ordnername (v3: UUID, v2: Base32 + "Z") -> UUID in Kleinbuchstaben."""
    try:
        return str(uuidlib.UUID(name))
    except ValueError:
        pass
    raw = name[:-1] if name.endswith("Z") else name
    raw = raw.translate(_B32_UNFIX)
    data = base64.b32hexdecode(raw + "=" * (-len(raw) % 8))
    return str(uuidlib.UUID(bytes=data[:16]))


def read_json(p: Path):
    return json.loads(Path(p).read_text(encoding="utf-8-sig"))


def parse_pos(key: str):
    """"col,row" -> (col, row)"""
    c, r = key.split(",")
    return int(c), int(r)


@dataclass
class Page:
    uuid: str
    dir: Path
    name: str = ""
    keypad: dict = field(default_factory=dict)      # (col,row) -> action dict
    encoders: dict = field(default_factory=dict)    # dial index -> action dict
    infobar: dict = field(default_factory=dict)     # Neo-Infobar: index -> action dict
    encoder_background: str = ""                    # Bildpfad für den ganzen Touchstreifen
    background: str = ""                            # LinDeck: Hintergrundbild über alle Tasten
    background_dim: int = 0                         # … abgedunkelt um Prozent

    def image_path(self, rel: str):
        if not rel:
            return None
        p = self.dir / rel
        return p if p.exists() else None


class Profile:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.id = self.root.name[: -len(".sdProfile")] if self.root.name.endswith(".sdProfile") else self.root.name
        m = read_json(self.root / "manifest.json")
        self.manifest = m
        self.name = m.get("Name") or self.id
        dev = m.get("Device") or {}
        self.model = dev.get("Model", "") if isinstance(dev, dict) else ""
        self.device_uuid = dev.get("UUID", "") if isinstance(dev, dict) else ""
        self.version = str(m.get("Version", "1.0"))
        self.app_identifier = m.get("AppIdentifier") or ""
        pages = m.get("Pages") or {}
        self.page_ids = list(pages.get("Pages") or [])
        self.default_page = pages.get("Default") or ""
        self.current_page = pages.get("Current") or ""
        if not self.page_ids:
            # Sehr alte Profile (v1): Tasten direkt im Hauptmanifest
            if self.default_page:
                self.page_ids = [self.default_page]
        meta_p = self.root / "lindeck-meta.json"
        self.meta = read_json(meta_p) if meta_p.exists() else {}
        self.os_type = self.meta.get("OSType", "Windows")
        self._page_cache = {}

    # -- Geometrie -------------------------------------------------------
    @property
    def geometry(self):
        g = model_geometry(self.model)
        if g:
            return g
        cols = rows = dials = 0
        for pid in self.page_ids:
            pg = self.page(pid)
            if not pg:
                continue
            for (c, r) in pg.keypad:
                cols, rows = max(cols, c + 1), max(rows, r + 1)
            if pg.encoders:
                dials = max(dials, max(pg.encoders) + 1)
        return Geometry(cols or 5, rows or 3, dials)

    @property
    def model_name(self):
        return model_name(self.model)

    # -- Seiten ----------------------------------------------------------
    def _page_dir(self, page_uuid: str):
        base = self.root / "Profiles"
        if not page_uuid:
            return None
        for cand in (page_uuid.upper(), page_uuid, page_uuid.lower()):
            if (base / cand / "manifest.json").exists():
                return base / cand
        try:
            enc = encode_page_dir(page_uuid)
        except ValueError:
            return None
        if (base / enc / "manifest.json").exists():
            return base / enc
        return None

    def page(self, page_uuid: str):
        if page_uuid in self._page_cache:
            return self._page_cache[page_uuid]
        d = self._page_dir(page_uuid)
        pg = None
        if d is not None:
            pg = load_page(page_uuid, d)
        elif page_uuid == self.default_page and "Actions" in self.manifest:
            # v1: Aktionen im Profil-Manifest selbst
            pg = Page(page_uuid, self.root)
            for k, a in (self.manifest.get("Actions") or {}).items():
                pg.keypad[parse_pos(k)] = a
        self._page_cache[page_uuid] = pg
        return pg

    def pages(self):
        return [p for p in (self.page(pid) for pid in self.page_ids) if p]

    def all_pages(self):
        """Alle Seiten inkl. Ordner (für Berichte)."""
        out = []
        base = self.root / "Profiles"
        if base.exists():
            for d in sorted(base.iterdir()):
                if (d / "manifest.json").exists():
                    out.append(load_page(d.name, d))
        return out

    def resource_path(self, res_id: str):
        if not res_id:
            return None
        cand = paths.RESOURCES_DIR / res_id
        if cand.is_dir():
            files = [f for f in cand.iterdir() if f.is_file()]
            if files:
                return files[0]
        return None

    def __repr__(self):
        return f"<Profile {self.name!r} {self.model_name} {len(self.page_ids)} Seiten>"


def load_page(page_uuid: str, d: Path, manifest: dict = None) -> Page:
    """Seite aus <d>/manifest.json lesen (oder aus einem bereits geladenen Manifest)."""
    m = manifest if manifest is not None else read_json(d / "manifest.json")
    pg = Page(page_uuid, d, name=m.get("Name") or "")
    bg = m.get("LinDeckBackground") or {}
    if isinstance(bg, dict):
        pg.background, pg.background_dim = bg.get("Image") or "", int(bg.get("Dim") or 0)
    controllers = m.get("Controllers")
    if controllers is None and "Actions" in m:
        controllers = [{"Type": "Keypad", "Actions": m["Actions"]}]
    for c in controllers or []:
        ctype = c.get("Type") or "Keypad"
        actions = c.get("Actions") or {}
        if ctype == "Keypad":
            for k, a in actions.items():
                if isinstance(a, dict) and a.get("UUID"):
                    pg.keypad[parse_pos(k)] = a
        elif ctype == "Encoder":
            pg.encoder_background = c.get("Background") or ""
            for k, a in actions.items():
                if isinstance(a, dict) and a.get("UUID"):
                    col, _ = parse_pos(k)
                    pg.encoders[col] = a
        else:
            # "Neo" und "{Invalid}" – Infobar-Aktionen des Stream Deck Neo
            for k, a in actions.items():
                if isinstance(a, dict) and a.get("UUID"):
                    col, _ = parse_pos(k)
                    pg.infobar[col] = a
    return pg


# ----------------------------------------------------------------------
# Profilspeicher
# ----------------------------------------------------------------------
class ProfileStore:
    def __init__(self, root: Path = None):
        self.root = Path(root or paths.PROFILES_DIR)
        self.profiles = {}
        self.mtime = 0.0
        self.reload()

    def _scan_mtime(self) -> float:
        try:
            mt = self.root.stat().st_mtime
            for d in self.root.iterdir():
                mt = max(mt, d.stat().st_mtime)
            return mt
        except OSError:
            return 0.0

    def changed_on_disk(self) -> bool:
        return self._scan_mtime() != self.mtime

    def reload(self) -> None:
        profiles = {}
        if self.root.exists():
            for d in sorted(self.root.iterdir()):
                if d.is_dir() and (d / "manifest.json").exists():
                    try:
                        p = Profile(d)
                        profiles[p.id] = p
                    except (OSError, ValueError, KeyError) as e:
                        print(f"[lindeck] Profil {d.name} nicht lesbar: {e}")
        self.profiles = profiles
        self.mtime = self._scan_mtime()

    def get(self, pid: str):
        if pid in self.profiles:
            return self.profiles[pid]
        for p in self.profiles.values():
            if p.id.lower() == (pid or "").lower() or p.name == pid:
                return p
        return None

    def for_model(self, model: str, geometry: Geometry):
        """Profile, die zum Gerät passen: erst exakter Modellcode, dann gleiche Geometrie."""
        exact = [p for p in self.profiles.values() if p.model.split("-")[0].rstrip("L") == model]
        geo = [p for p in self.profiles.values() if p not in exact and _geo_match(p.geometry, geometry)]
        return sorted(exact, key=lambda p: p.name.lower()) + sorted(geo, key=lambda p: p.name.lower())


def _geo_match(a: Geometry, b: Geometry) -> bool:
    return a.cols == b.cols and a.rows == b.rows and a.dials == b.dials


# ----------------------------------------------------------------------
# Import
# ----------------------------------------------------------------------
def _find_profile_roots(base: Path):
    """Alle obersten *.sdProfile-Ordner unterhalb von base (nicht verschachtelt)."""
    roots = []
    for d in sorted(base.rglob("*.sdProfile")):
        if not d.is_dir() or not (d / "manifest.json").exists():
            continue
        if any(parent.name.endswith(".sdProfile") for parent in d.parents):
            continue
        roots.append(d)
    return roots


def _find_packages(base: Path):
    """package.json-Dateien (Format v3) mit ihren Resources."""
    return [p.parent for p in base.rglob("package.json")]


def import_path(src, store_root: Path = None, resources_root: Path = None, replace=True):
    """Importiert eine Profildatei, ein Backup oder einen Ordner.

    Gibt eine Liste der importierten Profil-IDs zurück.
    """
    src = Path(src).expanduser()
    store_root = Path(store_root or paths.PROFILES_DIR)
    resources_root = Path(resources_root or paths.RESOURCES_DIR)
    store_root.mkdir(parents=True, exist_ok=True)
    resources_root.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        if src.is_file():
            tmp = Path(tempfile.mkdtemp(prefix="lindeck-import-"))
            with zipfile.ZipFile(src) as z:
                _safe_extract(z, tmp)
            base = tmp
            # Backups können verschachtelte ZIPs (.streamDeckProfile) enthalten
            for inner in list(base.rglob("*.streamDeckProfile")):
                if inner.is_file():
                    sub = inner.with_suffix(".extracted")
                    with zipfile.ZipFile(inner) as z:
                        _safe_extract(z, sub)
        elif src.is_dir():
            base = src
        else:
            raise FileNotFoundError(src)

        imported = []
        # Metadaten aus package.json (v3) den enthaltenen Profilen zuordnen
        pkg_meta = {}
        for pkg_dir in _find_packages(base):
            try:
                meta = read_json(pkg_dir / "package.json")
            except (OSError, ValueError):
                meta = {}
            res_manifest = pkg_dir / "Resources" / "manifest.json"
            if res_manifest.exists():
                try:
                    res = read_json(res_manifest).get("resources", {})
                except (OSError, ValueError):
                    res = {}
                for rid, info in res.items():
                    f = pkg_dir / "Resources" / info.get("file", "")
                    if f.is_file():
                        dst = resources_root / rid
                        dst.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(f, dst / f.name)
            for root in _find_profile_roots(pkg_dir):
                pkg_meta[root.resolve()] = meta

        for root in _find_profile_roots(base):
            dst = store_root / root.name
            if dst.exists():
                if not replace:
                    continue
                shutil.rmtree(dst)
            shutil.copytree(root, dst)
            meta = pkg_meta.get(root.resolve(), {})
            if meta:
                (dst / "lindeck-meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
            imported.append(dst.name[: -len(".sdProfile")])
        return imported
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def _safe_extract(z: zipfile.ZipFile, dest: Path) -> None:
    dest = dest.resolve()
    for member in z.infolist():
        name = member.filename.replace("\\", "/")
        target = (dest / name).resolve()
        if dest not in target.parents and target != dest:
            raise ValueError(f"Unsicherer Pfad im Archiv: {member.filename}")
        if name.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with z.open(member) as fsrc, open(target, "wb") as fdst:
            shutil.copyfileobj(fsrc, fdst)


def save_action_settings(page_dir: Path, old_json: str, settings: dict) -> bool:
    """Neue Settings für eine Aktion im Seiten-Manifest speichern (z. B. von einem Plugin).

    Die Aktion wird über ihren bisherigen Inhalt (old_json, sort_keys) gefunden – auch in Multi-Actions.
    """
    mf = Path(page_dir) / "manifest.json"
    m = read_json(mf)
    found = []

    def walk(obj):
        if isinstance(obj, dict):
            if obj.get("UUID") and json.dumps(obj, sort_keys=True) == old_json:
                found.append(obj)
                return
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
    walk(m)
    if not found:
        return False
    found[0]["Settings"] = settings
    tmp = mf.with_name("manifest.json.tmp")
    tmp.write_text(json.dumps(m, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(mf)
    return True


def delete_profile(pid: str, store_root: Path = None) -> bool:
    d = Path(store_root or paths.PROFILES_DIR) / f"{pid}.sdProfile"
    if d.exists():
        shutil.rmtree(d)
        return True
    return False
