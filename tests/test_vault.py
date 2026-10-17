"""Zugangsdaten im Schlüsselbund: config.json enthält danach kein Passwort mehr."""
import json
import os
import stat

import pytest

from freestream import paths, vault

FAKE = """#!/bin/sh
# Attrappe für secret-tool: speichert in $VAULT_DIR/<key>
cmd=$1; shift
key=""
while [ $# -gt 0 ]; do
  case "$1" in --label) shift 2; continue;; key) key=$2; shift 2; continue;; esac; shift
done
f="$VAULT_DIR/$key"
case $cmd in
  store) cat > "$f";;
  lookup) [ -f "$f" ] && cat "$f" || exit 1;;
  clear) rm -f "$f";;
esac
"""


@pytest.fixture
def fake_vault(tmp_path, monkeypatch):
    bin_dir, store = tmp_path / "bin", tmp_path / "vault"
    bin_dir.mkdir()
    store.mkdir()
    tool = bin_dir / "secret-tool"
    tool.write_text(FAKE)
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("VAULT_DIR", str(store))
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/dev/null")
    monkeypatch.setenv("FREESTREAM_SECRETS", "")
    monkeypatch.setattr(paths, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(paths, "CONFIG_FILE", tmp_path / "cfg" / "config.json")
    return store


def test_secrets_move_to_keyring(fake_vault):
    from freestream.config import Config
    assert vault.available()
    cfg = Config()
    cfg.data["obs"] = {"host": "10.0.0.2", "port": 4455, "password": "geheim"}
    cfg.data["homeassistant"] = {"url": "http://ha:8123", "token": "tok123"}
    cfg.save()
    text = paths.CONFIG_FILE.read_text()
    assert "geheim" not in text and "tok123" not in text
    on_disk = json.loads(text)
    assert on_disk["obs"]["host"] == "10.0.0.2" and sorted(on_disk["in_keyring"]) == ["homeassistant.token",
                                                                                         "obs.password"]
    assert (fake_vault / "obs.password").read_text() == "geheim"
    again = Config()
    assert again.data["obs"]["password"] == "geheim" and again.data["homeassistant"]["token"] == "tok123"
    assert "in_keyring" not in again.data
    # entfernen löscht auch den Eintrag im Schlüsselbund
    again.data["obs"]["password"] = ""
    again.save()
    assert not (fake_vault / "obs.password").exists()
    assert Config().data["homeassistant"]["token"] == "tok123"


def test_old_file_secret_migrates(fake_vault):
    from freestream.config import Config
    paths.CONFIG_FILE.parent.mkdir(parents=True)
    paths.CONFIG_FILE.write_text(json.dumps({"discord": {"client_id": "1", "client_secret": "s3"}}))
    cfg = Config()
    assert cfg.data["discord"]["client_secret"] == "s3"
    cfg.save()
    assert "s3" not in paths.CONFIG_FILE.read_text()
    assert Config().data["discord"]["client_secret"] == "s3"


def test_without_keyring_file_is_private(tmp_path, monkeypatch):
    from freestream.config import Config
    monkeypatch.setenv("FREESTREAM_SECRETS", "file")
    monkeypatch.setattr(paths, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(paths, "CONFIG_FILE", tmp_path / "cfg" / "config.json")
    cfg = Config()
    cfg.data["obs"] = {"password": "pw"}
    cfg.save()
    assert json.loads(paths.CONFIG_FILE.read_text())["obs"]["password"] == "pw"
    assert stat.S_IMODE(paths.CONFIG_FILE.stat().st_mode) == 0o600


def test_unreadable_keyring_keeps_note(fake_vault, monkeypatch):
    from freestream.config import Config
    cfg = Config()
    cfg.data["obs"] = {"password": "pw"}
    cfg.save()
    monkeypatch.setenv("FREESTREAM_SECRETS", "file")  # z. B. Schlüsselbund gesperrt
    other = Config()
    assert not other.data["obs"].get("password")
    other.data["brightness"] = 50
    other.save()
    monkeypatch.setenv("FREESTREAM_SECRETS", "")
    assert Config().data["obs"]["password"] == "pw"
