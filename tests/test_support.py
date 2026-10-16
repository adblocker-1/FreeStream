"""Support-Bericht: Geheimnisse entfernen, fehlende Programme, Datei wird geschrieben."""
import json

import pytest

from streamlin import paths, support


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    for name, sub in (("CONFIG_DIR", "cfg"), ("DATA_DIR", "data"), ("STATE_DIR", "state"),
                      ("PROFILES_DIR", "data/profiles"), ("PLUGINS_DIR", "data/plugins")):
        monkeypatch.setattr(paths, name, tmp_path / sub)
    monkeypatch.setattr(paths, "CONFIG_FILE", tmp_path / "cfg" / "config.json")
    monkeypatch.setattr(paths, "LOG_FILE", tmp_path / "state" / "streamlin.log")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path


CONFIG = {
    "brightness": 70,
    "keyboard_layout": "de",
    "obs": {"host": "127.0.0.1", "port": 4455, "password": "obs-secret-pw"},
    "home_assistant": {"url": "http://ha.local:8123", "token": "eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl"},
    "discord": {"client_id": "12345", "client_secret": "dc-secret", "access_token": "dc-token"},
    "integrations": [{"name": "x", "api_key": "k-123", "apiKey": "k-456", "webhook_url": "https://hook/abc"}],
    "twitch": {"stream_key": "live_123"},
    "elsewhere": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijk",
    "empty_password": "",
    "use_auth": True,
    "author": "Felix",
    "hotkey": "ctrl+a",
}
SECRETS = ("obs-secret-pw", "dc-secret", "dc-token", "k-123", "k-456", "https://hook/abc", "live_123",
           "eyJhbGciOi")


def test_redact_recursively_by_key_name():
    red = support.redact(CONFIG)
    text = json.dumps(red)
    for s in SECRETS:
        assert s not in text
    assert red["obs"] == {"host": "127.0.0.1", "port": 4455, "password": support.REDACTED}
    assert red["home_assistant"]["url"] == "http://ha.local:8123"
    assert red["discord"]["client_id"] == "12345"
    assert red["empty_password"] == ""  # leer bleibt sichtbar (hilft beim Helfen)
    assert red["use_auth"] is True and red["author"] == "Felix" and red["hotkey"] == "ctrl+a"
    assert red["keyboard_layout"] == "de" and red["brightness"] == 70
    assert CONFIG["obs"]["password"] == "obs-secret-pw"  # Original unverändert


def test_redact_text_in_logs(home):
    line = 'connect token=abc123 password: "pw1" Authorization: Bearer xyz home ' + str(home / "home") + "/x"
    out = support.redact_text(line)
    for s in ("abc123", "pw1", "xyz", str(home / "home")):
        assert s not in out
    assert "~/x" in out


def test_run_handles_missing_tools_errors_and_timeouts():
    assert "not available" in support.run(["definitely-not-a-tool-xyz"])
    assert "timed out" in support.run(["sleep", "5"], timeout=0.2)
    assert "exit 3" in support.run(["sh", "-c", "echo boom >&2; exit 3"])
    out = support.run(["sh", "-c", "seq 1 100"], max_lines=10)
    assert out.splitlines()[-1].startswith("…") and len(out.splitlines()) == 11


def test_report_file_without_any_tools(home, monkeypatch):
    paths.CONFIG_FILE.parent.mkdir(parents=True)
    paths.CONFIG_FILE.write_text(json.dumps(CONFIG))
    paths.STATE_DIR.mkdir(parents=True)
    paths.LOG_FILE.write_text("\n".join(f"line {i}" for i in range(1000)) + "\nlogin token=sekrit\n")
    (paths.STATE_DIR / "mixer.json").write_text('{"modules": [1, 2]}')
    (paths.STATE_DIR / "discord-token.json").write_text('{"access_token": "never-in-report"}')
    monkeypatch.setenv("PATH", str(home / "empty"))  # kein pactl, lsusb, dpkg, rpm …
    path = support.write_report(app="LinDeck")
    assert path.parent == home / "home" and path.name.startswith("streamlin-support-")
    text = path.read_text()
    for title in ("## Streamlin", "## System", "## Stream Deck devices", "## Audio", "## LinDeck / Linewave state",
                  "## config.json", "## Log"):
        assert title in text
    assert "(pactl not available)" in text and "(lsusb not available)" in text
    for s in SECRETS + ("sekrit", "never-in-report"):
        assert s not in text
    assert "line 999" in text and "line 700" not in text  # nur die letzten 300 Zeilen
    assert '"modules"' in text and "Discord authorized: True" in text
    assert support.__version__ in text


def test_report_target_file_and_broken_collector(home, monkeypatch):
    def boom():
        raise RuntimeError("kaputt")

    monkeypatch.setattr(support, "sections", lambda app="": [("Fine", lambda: "ok"), ("Broken", boom)])
    path = support.write_report(home / "sub" / "my.txt")
    assert path == home / "sub" / "my.txt"
    text = path.read_text()
    assert "ok" in text and "failed: RuntimeError: kaputt" in text


def test_devices_from_sysfs(tmp_path):
    usb = tmp_path / "usb"
    for name, vendor, product in (("1-2", "0fd9", "Stream Deck MK.2"), ("1-3", "046d", "Mouse")):
        d = usb / name
        d.mkdir(parents=True)
        (d / "idVendor").write_text(vendor + "\n")
        (d / "idProduct").write_text("0080\n")
        (d / "product").write_text(product + "\n")
        (d / "manufacturer").write_text("Elgato\n")
    assert support.usb_devices(str(usb)) == ["0fd9:0080 Stream Deck MK.2 (Elgato) at 1-2"]
    hid = tmp_path / "hidraw" / "hidraw3" / "device"
    hid.mkdir(parents=True)
    (hid / "uevent").write_text("HID_ID=0003:00000FD9:00000080\nHID_NAME=Elgato Stream Deck\n")
    out = support.hidraw_devices(str(tmp_path / "hidraw"))
    assert out == ["/dev/hidraw3 Elgato Stream Deck: missing"]


def test_cli_support_report(home, capsys):
    from streamlin.__main__ import main, wave_main
    out = home / "r1.txt"
    assert main(["support-report", str(out)]) == 0
    assert out.exists() and "Report created by: LinDeck" in out.read_text()
    assert str(out) in capsys.readouterr().out
    (home / "dir").mkdir()
    assert wave_main(["support-report", str(home / "dir")]) == 0
    [made] = (home / "dir").glob("streamlin-support-*.txt")
    assert "Report created by: Linewave" in made.read_text()
