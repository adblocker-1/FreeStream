"""LinDeck und Linewave einzeln: jedes Paket läuft allein, beide zusammen arbeiten zusammen."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packaging"))
import parts as P  # noqa: E402  (packaging/parts.py)


def test_every_module_belongs_to_exactly_one_part():
    files = {p.name for p in (ROOT / "streamlin").iterdir() if p.name != "__pycache__"}
    listed = P.COMMON + P.LINDECK + P.LINEWAVE
    assert len(listed) == len(set(listed))
    assert set(listed) == files


def _install(tmp, name):
    """Paketinhalt wie im .deb nach tmp/<name>/streamlin kopieren."""
    dst = tmp / name / "streamlin"
    dst.mkdir(parents=True)
    for f in P.PACKAGES[name]:
        src = ROOT / "streamlin" / f
        (shutil.copytree if src.is_dir() else shutil.copy2)(src, dst / f)
    return tmp / name


def _run(code_or_args, path, tmp, join=None):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp / "home"), "PYTHONPATH": str(path),
           "STREAMLIN_LANG": "en", "STREAMLIN_JOIN_DIRS": os.pathsep.join(str(j) for j in join or [tmp / "none"])}
    args = code_or_args if isinstance(code_or_args, list) else ["-c", code_or_args]
    return subprocess.run([sys.executable] + args, cwd=tmp, env=env, capture_output=True, text=True, timeout=120)


def _imports(mods):
    return "import importlib\n" + "".join(f"importlib.import_module('streamlin.{m[:-3]}')\n" for m in mods
                                           if m.endswith(".py") and m != "__main__.py")


@pytest.fixture
def tmp(tmp_path):
    (tmp_path / "home").mkdir()
    return tmp_path


def test_lindeck_alone(tmp):
    path = _install(tmp, "lindeck")
    r = _run(_imports(P.COMMON + P.LINDECK) + """
from streamlin import parts, catalog, native
assert parts.has_lindeck() and not parts.has_linewave()
assert not any(a.category == "Linewave" for a in catalog.CATALOG)
assert native.lookup("com.lindeck.mixer.channel") is None
assert native.lookup("com.lindeck.obs.record") is not None
""", path, tmp)
    assert r.returncode == 0, r.stderr
    r = _run(["-m", "streamlin", "--help"], path, tmp)
    assert r.returncode == 0 and "lindeck" in r.stdout, r.stderr
    r = _run(["-m", "streamlin", "mixer"], path, tmp)
    assert r.returncode == 2 and "Linewave is not installed" in r.stderr


def test_linewave_alone(tmp):
    path = _install(tmp, "linewave")
    r = _run(_imports(P.COMMON + P.LINEWAVE) + """
from streamlin import parts
assert parts.has_linewave() and not parts.has_lindeck()
""", path, tmp)
    assert r.returncode == 0, r.stderr
    r = _run(["-m", "streamlin.linewave", "mixer", "--help"], path, tmp)
    assert r.returncode == 0 and "linewave" in r.stdout, r.stderr
    r = _run(["-m", "streamlin", "profiles"], path, tmp)
    assert r.returncode == 2 and "LinDeck is not installed" in r.stderr


def test_both_installed_separately_work_together(tmp):
    deck, wave = _install(tmp, "lindeck"), _install(tmp, "linewave")
    code = """
from streamlin import parts, catalog, native
assert parts.has_lindeck() and parts.has_linewave()
assert any(a.category == "Linewave" for a in catalog.CATALOG)
assert native.lookup("com.lindeck.mixer.channel") is native.MixerAction
from streamlin import mixer, wavemic  # Module aus dem Linewave-Paket
"""
    r = _run(code, deck, tmp, join=[deck, wave])
    assert r.returncode == 0, r.stderr
    r = _run("from streamlin import parts, daemon; assert parts.has_lindeck()", wave, tmp, join=[deck, wave])
    assert r.returncode == 0, r.stderr
