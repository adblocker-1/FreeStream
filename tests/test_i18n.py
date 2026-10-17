"""Translations: .po parser, language detection, completeness of every language."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from freestream import i18n

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import i18n as tool  # noqa: E402  (tools/i18n.py)


@pytest.fixture
def lang_env(monkeypatch, tmp_path):
    for var in ("FREESTREAM_LANG", "FREEDECK_LANG", "LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(var, raising=False)
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(i18n.paths, "CONFIG_FILE", cfg)
    old = i18n.current()
    yield monkeypatch, cfg
    i18n.set_language(old)


def test_po_round_trip(tmp_path):
    ents = [{"msgid": 'Say "hi"\nnow', "refs": ["x.py:1"]},
            {"msgctxt": "verb", "msgid": "Open {name}", "refs": []},
            {"msgid": "{n} file", "msgid_plural": "{n} files", "refs": []}]
    trans = {(None, 'Say "hi"\nnow'): ['Sag „hi“\njetzt'],
             ("verb", "Open {name}"): ["{name} öffnen"],
             (None, "{n} file"): ["{n} Datei", "{n} Dateien"]}
    po = tmp_path / "xx.po"
    tool.write_po(po, ents, tool._header("xx"), trans)
    got = {(e.get("msgctxt"), e["msgid"]): e["msgstr"] for e in i18n.parse_po(po.read_text()) if e["msgid"]}
    assert got == trans
    plural = [e for e in i18n.parse_po(po.read_text()) if e.get("msgid_plural")][0]
    assert plural["msgid_plural"] == "{n} files" and "python-brace-format" in plural["flags"]


def test_lookup_context_and_plural(monkeypatch, tmp_path):
    monkeypatch.setattr(i18n, "LOCALE_DIR", tmp_path)
    tool.write_po(tmp_path / "de.po", [
        {"msgid": "Close", "refs": []}, {"msgctxt": "state", "msgid": "Open", "refs": []},
        {"msgid": "{n} key", "msgid_plural": "{n} keys", "refs": []}, {"msgid": "Untranslated", "refs": []},
    ], tool._header("de"), {(None, "Close"): ["Schließen"], ("state", "Open"): ["Offen"],
                            (None, "{n} key"): ["{n} Taste", "{n} Tasten"]})
    try:
        assert i18n.set_language("de") == "de"
        assert i18n._("Close") == "Schließen"
        assert i18n._("Open") == "Open" and i18n.pgettext("state", "Open") == "Offen"
        assert i18n.ngettext("{n} key", "{n} keys", 1) == "{n} Taste"
        assert i18n.ngettext("{n} key", "{n} keys", 5) == "{n} Tasten"
        assert i18n._("Untranslated") == "Untranslated"
        assert i18n.set_language("en") == "en" and i18n._("Close") == "Close"
    finally:
        monkeypatch.undo()
        i18n.set_language("de")


def test_detect_order(lang_env):
    mp, cfg = lang_env
    assert i18n.detect() == "en"                      # nothing set
    mp.setenv("LANG", "C.UTF-8")
    assert i18n.detect() == "en"
    mp.setenv("LANG", "de_AT.UTF-8")
    assert i18n.detect() == "de"                      # region falls back to the language
    mp.setenv("LANG", "pt_PT.UTF-8")
    assert i18n.detect() == "pt_BR"                   # only Brazilian Portuguese exists
    mp.setenv("LANGUAGE", "fr_CA:es")
    assert i18n.detect() == "fr"                      # LANGUAGE wins over LANG
    mp.setenv("LANGUAGE", "xx:es")
    assert i18n.detect() == "es"                      # first available one in the list
    cfg.write_text(json.dumps({"language": "es"}))
    mp.delenv("LANGUAGE")
    assert i18n.detect() == "es"                      # config wins over the system locale
    cfg.write_text(json.dumps({"language": "auto"}))
    assert i18n.detect() == "pt_BR"
    mp.setenv("FREESTREAM_LANG", "fr")
    assert i18n.detect() == "fr"                      # FREESTREAM_LANG wins over everything
    mp.setenv("FREESTREAM_LANG", "xx")
    assert i18n.detect() == "en"


def test_languages_offered():
    langs = i18n.available()
    assert langs[0] == "en"
    assert {"de", "es", "fr", "pt_BR"} <= set(langs)
    assert i18n.name("pt_BR") == "Português (Brasil)" and i18n.name("de") == "Deutsch"


def test_template_is_current():
    """freestream.pot lists exactly the strings used in the code (tools/i18n.py update)."""
    pot = {(e.get("msgctxt"), e["msgid"]) for e in i18n.parse_po(tool.POT.read_text(encoding="utf-8"))
           if e["msgid"]}
    code = {(e.get("msgctxt"), e["msgid"]) for e in tool.extract()}
    assert len(code) > 500
    assert pot == code, "run: python3 tools/i18n.py update"


@pytest.mark.parametrize("lang", ["de", "es", "fr", "pt_BR"])
def test_language_complete(lang, capsys):
    assert tool.check([lang], verbose=True) == 0, capsys.readouterr().out[-3000:]


@pytest.mark.parametrize("lang", ["en", "de", "es", "fr", "pt_BR"])
def test_all_modules_import(lang, tmp_path):
    """Every module loads in every language (module-level texts, placeholders)."""
    mods = sorted(p.stem for p in (ROOT / "freestream").glob("*.py") if p.stem not in ("__init__",))
    code = ("import importlib, sys\n"
            f"for m in {mods!r}:\n"
            "    if m in ('__main__', 'freewave'):\n"
            "        continue\n"
            "    try:\n"
            "        importlib.import_module('freestream.' + m)\n"
            "    except ImportError as e:\n"
            "        if 'tkinter' not in str(e) and 'StreamDeck' not in str(e) and 'evdev' not in str(e):\n"
            "            raise\n"
            "from freestream import i18n\n"
            "print(i18n.current())\n")
    env = {"PATH": "/usr/bin:/bin", "FREESTREAM_LANG": lang, "HOME": str(tmp_path)}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == lang


def test_cli_help_translated(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    en = subprocess.run([sys.executable, "-m", "freestream", "--help"], cwd=ROOT, capture_output=True,
                        text=True, env={**env, "FREESTREAM_LANG": "en"})
    de = subprocess.run([sys.executable, "-m", "freestream", "--help"], cwd=ROOT, capture_output=True,
                        text=True, env={**env, "FREESTREAM_LANG": "de"})
    assert en.returncode == 0 and de.returncode == 0
    assert en.stdout != de.stdout


@pytest.mark.parametrize("old_name", ["streamlin", "lindeck"])
def test_old_folders_are_moved(tmp_path, old_name):
    """Bis 0.28 hießen die Ordner „streamlin“, bis 0.13 „lindeck“: sie ziehen einmalig um, am alten Ort
    bleibt ein Verweis."""
    old = tmp_path / "config" / old_name
    old.mkdir(parents=True)
    (old / "config.json").write_text('{"language": "fr"}')
    code = "from freestream import paths; print(paths.CONFIG_DIR)"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "config"),
           "XDG_DATA_HOME": str(tmp_path / "data"), "XDG_STATE_HOME": str(tmp_path / "state")}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    new = tmp_path / "config" / "freestream"
    assert out.stdout.strip() == str(new)
    assert (new / "config.json").read_text() == '{"language": "fr"}'
    assert old.is_symlink() and (old / "config.json").exists()
    # zweiter Start: nichts mehr zu tun
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)
    assert out.stdout.strip() == str(new)


def test_folder_chain_lindeck_streamlin(tmp_path):
    """Wer von 0.13 über 0.28 kommt: lindeck → streamlin (Verweis) → freestream."""
    base = tmp_path / "config"
    (base / "streamlin").mkdir(parents=True)
    (base / "streamlin" / "config.json").write_text("{}")
    (base / "lindeck").symlink_to("streamlin")
    code = "from freestream import paths; print(paths.CONFIG_DIR)"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(base),
           "XDG_DATA_HOME": str(tmp_path / "data"), "XDG_STATE_HOME": str(tmp_path / "state")}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)
    assert out.stdout.strip() == str(base / "freestream")
    assert (base / "lindeck" / "config.json").exists() and (base / "streamlin" / "config.json").exists()
