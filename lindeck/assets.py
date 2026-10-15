"""Standard-Icons aus dem eigenen Elgato-Installer übernehmen.

Die Icons der eingebauten Aktionen gehören Elgato und werden deshalb nicht
mitgeliefert, sondern lokal aus dem Installer (Stream_Deck_*.msi) oder
einem kopierten Programmordner extrahiert.
"""
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import paths
from .i18n import _


def _find_install_root(base: Path):
    for cand in base.rglob("Plugins"):
        if cand.is_dir() and any(p.name.startswith("com.elgato.streamdeck.") for p in cand.iterdir()):
            return cand.parent
    return None


def extract(src) -> Path:
    src = Path(src).expanduser()
    tmp = None
    try:
        if src.is_file():
            tmp = Path(tempfile.mkdtemp(prefix="lindeck-msi-"))
            if shutil.which("msiextract"):
                subprocess.run(["msiextract", "-C", str(tmp), str(src)], check=True, capture_output=True)
            elif shutil.which("7z"):
                subprocess.run(["7z", "x", "-y", f"-o{tmp}", str(src)], check=True, capture_output=True)
            else:
                raise RuntimeError(_("Please install 'msitools': sudo apt install msitools"))
            base = tmp
        else:
            base = src
        root = _find_install_root(base)
        if not root:
            raise RuntimeError(_("No Elgato plugins found in the installer"))
        dst = paths.ASSETS_DIR
        if dst.exists():
            shutil.rmtree(dst)
        dst.mkdir(parents=True)
        for plugin in (root / "Plugins").iterdir():
            if (plugin / "Images").is_dir():
                shutil.copytree(plugin / "Images", dst / "Plugins" / plugin.name / "Images")
            if (plugin / "manifest.json").exists():
                shutil.copy2(plugin / "manifest.json", dst / "Plugins" / plugin.name / "manifest.json")
        for sub in ("DefaultProfiles", "PageIcons", "TouchBackgrounds"):
            if (root / sub).is_dir():
                shutil.copytree(root / sub, dst / sub)
        return dst
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
