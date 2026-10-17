"""FreeDeck und FreeWave einzeln: jedes Paket läuft allein, beide zusammen arbeiten zusammen."""
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
    files = {p.name for p in (ROOT / "freestream").iterdir() if p.name != "__pycache__"}
    listed = P.COMMON + P.FREEDECK + P.FREEWAVE
    assert len(listed) == len(set(listed))
    assert set(listed) == files


def _install(tmp, name):
    """Paketinhalt wie im .deb nach tmp/<name>/freestream kopieren."""
    dst = tmp / name / "freestream"
    dst.mkdir(parents=True)
    for f in P.PACKAGES[name]:
        src = ROOT / "freestream" / f
        (shutil.copytree if src.is_dir() else shutil.copy2)(src, dst / f)
    return tmp / name


def _run(code_or_args, path, tmp, join=None):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp / "home"), "PYTHONPATH": str(path),
           "FREESTREAM_LANG": "en", "FREESTREAM_JOIN_DIRS": os.pathsep.join(str(j) for j in join or [tmp / "none"])}
    args = code_or_args if isinstance(code_or_args, list) else ["-c", code_or_args]
    return subprocess.run([sys.executable] + args, cwd=tmp, env=env, capture_output=True, text=True, timeout=120)


def _imports(mods):
    return "import importlib\n" + "".join(f"importlib.import_module('freestream.{m[:-3]}')\n" for m in mods
                                           if m.endswith(".py") and m != "__main__.py")


@pytest.fixture
def tmp(tmp_path):
    (tmp_path / "home").mkdir()
    return tmp_path


def test_lindeck_alone(tmp):
    path = _install(tmp, "freedeck")
    r = _run(_imports(P.COMMON + P.FREEDECK) + """
from freestream import parts, catalog, native
assert parts.has_freedeck() and not parts.has_freewave()
assert not any(a.category == "FreeWave" for a in catalog.CATALOG)
assert native.lookup("com.lindeck.mixer.channel") is None
assert native.lookup("com.lindeck.obs.record") is not None
""", path, tmp)
    assert r.returncode == 0, r.stderr
    r = _run(["-m", "freestream", "--help"], path, tmp)
    assert r.returncode == 0 and "freedeck" in r.stdout, r.stderr
    r = _run(["-m", "freestream", "mixer"], path, tmp)
    assert r.returncode == 2 and "FreeWave is not installed" in r.stderr


def test_linewave_alone(tmp):
    path = _install(tmp, "freewave")
    r = _run(_imports(P.COMMON + P.FREEWAVE) + """
from freestream import parts
assert parts.has_freewave() and not parts.has_freedeck()
""", path, tmp)
    assert r.returncode == 0, r.stderr
    r = _run(["-m", "freestream.freewave", "mixer", "--help"], path, tmp)
    assert r.returncode == 0 and "freewave" in r.stdout, r.stderr
    r = _run(["-m", "freestream", "profiles"], path, tmp)
    assert r.returncode == 2 and "FreeDeck is not installed" in r.stderr


def test_both_installed_separately_work_together(tmp):
    deck, wave = _install(tmp, "freedeck"), _install(tmp, "freewave")
    code = """
from freestream import parts, catalog, native
assert parts.has_freedeck() and parts.has_freewave()
assert any(a.category == "FreeWave" for a in catalog.CATALOG)
assert native.lookup("com.lindeck.mixer.channel") is native.MixerAction
from freestream import mixer, wavemic  # Module aus dem FreeWave-Paket
"""
    r = _run(code, deck, tmp, join=[deck, wave])
    assert r.returncode == 0, r.stderr
    r = _run("from freestream import parts, daemon; assert parts.has_freedeck()", wave, tmp, join=[deck, wave])
    assert r.returncode == 0, r.stderr
