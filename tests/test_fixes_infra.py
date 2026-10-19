"""Fehler aus dem Code-Review (Bereich Infrastruktur): Konfiguration, Plugins, Einzelinstanz, Discord,
Home Assistant, FreeWave-Export und Beenden von FreeDeck."""
import base64
import io
import json
import os
import random
import socket
import ssl
import stat
import struct
import subprocess
import sys
import threading
import time
import urllib.error

import pytest
from PIL import Image

from freestream import paths


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    for name, sub in (("CONFIG_DIR", "cfg"), ("DATA_DIR", "data"), ("STATE_DIR", "state"),
                      ("PROFILES_DIR", "data/profiles"), ("RESOURCES_DIR", "data/res"),
                      ("PLUGINS_DIR", "data/plugins"), ("PLUGIN_DATA_DIR", "data/plugin-data")):
        monkeypatch.setattr(paths, name, tmp_path / sub)
    monkeypatch.setattr(paths, "CONFIG_FILE", tmp_path / "cfg" / "config.json")
    monkeypatch.setattr(paths, "STATE_FILE", tmp_path / "state" / "state.json")
    monkeypatch.setattr(paths, "PID_FILE", tmp_path / "state" / "freestream.pid")
    for sub in ("cfg", "state", "data/profiles"):
        (tmp_path / sub).mkdir(parents=True)
    from freestream import models
    monkeypatch.setattr(models, "USB_ROOT", str(tmp_path / "usb"))  # keine echten Geräte
    return tmp_path


def wait_for(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def _sleeper(*extra):
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", *extra])


# ==========================================================================
# config.json: kaputte Datei, Rechte, Zwischendatei, Schlüsselbund
# ==========================================================================
BROKEN = '{"mixer": {"channels": [{"id": "music"}]}, "obs": {"password": "geheim"},}'  # Komma zu viel


def test_invalid_config_is_kept_and_not_overwritten_by_single_keys():
    from freestream import i18n, theme, updater
    from freestream.config import Config
    f = paths.CONFIG_FILE
    f.write_text(BROKEN)
    os.chmod(f, 0o600)
    cfg = Config()
    backups = list(f.parent.glob("config.json.broken-*"))
    assert len(backups) == 1 and backups[0].read_text() == BROKEN
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600  # Passwort darin
    assert not cfg.changed_on_disk()  # der Dienst lädt (und meldet) sie nicht alle 2 s neu
    Config()
    assert len(list(f.parent.glob("config.json.broken-*"))) == 1  # nur eine Kopie je Fassung
    # Updater und Sprache schrieben bisher {} + ihren Schlüssel über die Datei
    updater.set_enabled(False)
    assert i18n.save_language("fr") is False
    assert theme.save_appearance("light") is False  # Hell/Dunkel/Auto in den Einstellungen
    assert f.read_text() == BROKEN
    # Config.save (z. B. nach dem Einrichtungsassistenten) darf schreiben – die Kopie bleibt
    cfg.save()
    assert "mixer" not in json.loads(f.read_text())
    assert backups[0].read_text() == BROKEN


def test_broken_config_copy_is_private_from_the_start(monkeypatch):
    """Die Kopie entsteht gleich mit den Rechten der Datei – bisher copy2: erst 0644 samt Inhalt, dann chmod."""
    from freestream import config
    f = paths.CONFIG_FILE
    f.write_text(BROKEN)
    os.chmod(f, 0o600)
    created = []
    real_open = config.os.open

    def os_open(path, flags, mode=0o777, **kw):
        fd = real_open(path, flags, mode, **kw)
        if ".broken-" in str(path):
            created.append(stat.S_IMODE(os.fstat(fd).st_mode))  # noch leer: Rechte beim Anlegen
        return fd
    monkeypatch.setattr(config.os, "open", os_open)
    config.Config()
    config.Config()  # schon gesichert: keine zweite Kopie
    assert created == [0o600]
    assert [p.read_text() for p in f.parent.glob("config.json.broken-*")] == [BROKEN]


def test_config_that_is_not_an_object():
    from freestream.config import Config
    paths.CONFIG_FILE.write_text("[1, 2]")
    cfg = Config()  # bisher TypeError aus dict.update
    assert cfg.get("brightness") == 70
    assert list(paths.CONFIG_FILE.parent.glob("config.json.broken-*"))


def test_single_key_writes_keep_private_mode():
    from freestream import i18n, theme, updater
    from freestream.config import Config, save_keys
    f = paths.CONFIG_FILE
    cfg = Config()
    cfg.data["homeassistant"] = {"url": "http://ha:8123", "token": "geheim"}
    cfg.save()  # ohne Schlüsselbund (Tests): Token bleibt in der Datei
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    updater.set_enabled(False)
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    i18n.save_language("de")
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert theme.save_appearance("dark")  # bisher 0644 nach einem Klick auf Hell/Dunkel/Auto
    data = json.loads(f.read_text())
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert data["homeassistant"]["token"] == "geheim" and data["check_updates"] is False and data["language"] == "de"
    assert data["appearance"] == "dark" and theme.configured_appearance() == "dark"
    # ohne Zugangsdaten: Rechte bleiben, wie der Benutzer sie gesetzt hat
    f.write_text('{"language": "fr"}')
    os.chmod(f, 0o640)
    assert save_keys(check_updates=True)
    assert stat.S_IMODE(f.stat().st_mode) == 0o640 and json.loads(f.read_text())["language"] == "fr"
    assert not [p for p in f.parent.iterdir() if p.name.endswith(".tmp")]


def test_concurrent_saves_use_own_temp_files(monkeypatch):
    """FreeDeck und FreeWave (bzw. zwei Config) speichern gleichzeitig: früher dieselbe config.json.tmp."""
    from freestream import config, updater
    from freestream.config import Config
    tmps = set()
    real_replace = config.os.replace

    def replace(src, dst):
        tmps.add(str(src))
        return real_replace(src, dst)
    monkeypatch.setattr(config.os, "replace", replace)
    errors = []

    def save(cfg):
        try:
            for i in range(150):
                cfg.data["brightness"] = i
                cfg.save()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def toggle():
        try:
            for i in range(150):
                updater.set_enabled(i % 2 == 0)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=save, args=(Config(),)) for __ in range(2)]
    threads.append(threading.Thread(target=toggle))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert isinstance(json.loads(paths.CONFIG_FILE.read_text()), dict)
    assert len(tmps) == 3  # je Thread eine eigene Zwischendatei
    assert not [p for p in paths.CONFIG_FILE.parent.iterdir() if p.name.endswith(".tmp")]


def test_editor_choices_do_not_ask_the_keyring(monkeypatch):
    from freestream import catalog, deckexport, vault
    from freestream.config import Config
    calls = []
    monkeypatch.setattr(vault, "lookup", lambda key: calls.append(key) or "geheim")
    paths.CONFIG_FILE.write_text(json.dumps({
        "in_keyring": ["obs.password"],
        "mixer": {"mixes": [{"id": "talk", "name": "Talk"}], "scenes": [{"id": "s1", "name": "Szene"}]}}))
    assert ("talk", "Talk") in catalog._mix_list()
    assert catalog._scene_choices() == [("s1", "Szene")]
    assert "talk" in deckexport.linewave_mixes()
    assert deckexport._membership()("music", "talk")
    assert calls == []
    Config()
    assert calls == ["obs.password"]  # zum Vergleich: Config() fragt den Schlüsselbund


# ==========================================================================
# Einzelinstanz: Pid aus einer liegengebliebenen Statusdatei
# ==========================================================================
def test_running_instance_ignores_reused_pid():
    from freestream import desktop
    f = paths.STATE_DIR / "freewave.json"
    sleeper = _sleeper()
    try:
        f.write_text(json.dumps({"pid": sleeper.pid, "gui": True}))  # alte Datei, fremder Prozess
        assert desktop.running_instance("freewave.json") is None
        start = desktop._start_time(sleeper.pid)
        assert start
        f.write_text(json.dumps({"pid": sleeper.pid, "gui": True, "start": start - 1}))
        assert desktop.running_instance("freewave.json") is None  # dieselbe Pid, anderer Prozess
        f.write_text(json.dumps({"pid": sleeper.pid, "gui": True, "start": start}))
        assert desktop.running_instance("freewave.json") == sleeper.pid
    finally:
        sleeper.kill()
        sleeper.wait()
    legacy = _sleeper("freewave")  # Datei einer älteren Version: zählt, wenn es nach FreeWave aussieht
    try:
        f.write_text(json.dumps({"pid": legacy.pid, "gui": True}))
        assert desktop.running_instance("freewave.json") == legacy.pid
    finally:
        legacy.kill()
        legacy.wait()
    desktop._write_app_info(True, "own.json")
    info = json.loads((paths.STATE_DIR / "own.json").read_text())
    assert info["pid"] == os.getpid() and info["start"] == desktop._start_time(os.getpid())


def test_reload_signal_not_sent_to_foreign_pid():
    from freestream.__main__ import _signal_daemon
    sleeper = _sleeper()
    try:
        paths.PID_FILE.write_text(str(sleeper.pid))  # freestream.pid nach einem Absturz
        assert _signal_daemon() is False
        with pytest.raises(subprocess.TimeoutExpired):
            sleeper.wait(0.3)  # SIGHUP hätte ihn beendet
    finally:
        sleeper.kill()
        sleeper.wait()


# ==========================================================================
# Plugins
# ==========================================================================
def _plugin_src(tmp_path, uid, version):
    src = tmp_path / "src" / version / f"{uid}.sdPlugin"
    (src / "bin").mkdir(parents=True)
    (src / "bin" / "plugin").write_text("#!" + sys.executable + "\nimport time\ntime.sleep(60)\n")
    os.chmod(src / "bin" / "plugin", 0o755)
    (src / "manifest.json").write_text(json.dumps({
        "UUID": uid, "Name": "Test", "Version": version, "CodePathLin": "bin/plugin",
        "Actions": [{"UUID": f"{uid}.act", "States": [{}]}]}))
    return src


class _Ctrl:
    serial, model = "SER1", "20GBA9901"

    def __init__(self):
        from freestream.models import Geometry
        self.geometry = Geometry(5, 3)
        self.plugin_host = None

    def redraw_action(self, a):
        pass

    def show_overlay(self, a, kind, seconds=1.2):
        pass


class _FakeProc:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)

    def stop(self):
        pass


@pytest.fixture
def host(tmp_path):
    from freestream import plugins
    plugins.install(_plugin_src(tmp_path, "com.t", "1.0"))
    h = plugins.PluginHost(None, autostart=False)
    h.procs["com.t"] = _FakeProc()
    yield h
    h.stop()


def _act(ctrl, slot=0):
    from freestream import actions as A
    a = A.create_action({"UUID": "com.t.act", "Settings": {}, "States": [{}]}, None, A.Context(ctrl, {}, None, None))
    a.slot = ("key", slot)
    return a


def test_plugin_contexts_are_forgotten_after_page_switches(host, monkeypatch):
    from freestream import plugins
    ctrl = _Ctrl()
    a = _act(ctrl)
    host.will_appear(ctrl, a)
    a.image_override = Image.new("RGBA", (144, 144))
    ctx = a.sdk_context
    host.will_disappear(ctrl, a)
    assert a.image_override is None  # Bild sofort frei
    # kurz danach gilt der Kontext noch (setSettings nach keyDown, Antwort kommt später)
    host.handle(host.procs["com.t"], {"event": "setSettings", "context": ctx, "payload": {"n": 1}})
    assert a.settings == {"n": 1}
    monkeypatch.setattr(plugins, "HIDDEN_GRACE", -1)  # Frist abgelaufen
    for __ in range(50):  # 50 Seitenwechsel mit 10 Plugin-Tasten
        acts = [_act(ctrl, i) for i in range(10)]
        for b in acts:
            host.will_appear(ctrl, b)
            b.image_override = Image.new("RGBA", (144, 144))
        for b in acts:
            host.will_disappear(ctrl, b)
    assert host.contexts == {} and host.visible == set()  # bisher 501 Aktionen samt Bild
    shown = _act(ctrl)
    host.will_appear(ctrl, shown)
    assert list(host.contexts) == [shown.sdk_context]  # sichtbare bleiben


def test_plugin_context_kept_while_settings_page_is_open(host, monkeypatch):
    from freestream import plugins, ws
    ctrl = _Ctrl()
    a = _act(ctrl)
    host.will_appear(ctrl, a)
    ctx = a.sdk_context

    class Conn:
        def __init__(self):
            self.gone = threading.Event()

        def recv(self):
            self.gone.wait()
            raise ws.Closed()

        def send(self, msg):
            pass

        def close(self):
            self.gone.set()
    conn = Conn()
    t = threading.Thread(target=host._pi_session, args=(conn, ctx), daemon=True)
    t.start()
    assert wait_for(lambda: ctx in host.pis)
    monkeypatch.setattr(plugins, "HIDDEN_GRACE", -1)
    host.will_disappear(ctrl, a)  # Seitenwechsel bei offener Einstellungsseite
    host.will_appear(ctrl, _act(ctrl, 1))
    assert host.contexts.get(ctx) is a
    conn.close()
    t.join(5)
    assert ctx not in host.contexts  # nach dem Schließen vergessen


def test_plugin_contexts_in_multi_action_are_forgotten(host, monkeypatch):
    """Plugin-Aktionen in einer Multi-Aktion bekommen kein willAppear/willDisappear, nur keyDown/keyUp."""
    from freestream import actions as A
    from freestream import plugins
    ctrl = _Ctrl()
    ctrl.plugin_host = host

    def press():  # Seite neu gezeigt (neue Aktionen), dann die Multi-Aktion gedrückt
        ma = A.create_action({"UUID": "com.elgato.streamdeck.multiactions.routine", "Actions": [{"Actions": [
            {"UUID": "com.t.act", "Settings": {}, "States": [{}]}]}]}, None, A.Context(ctrl, {}, None, None))
        ma.slot = ("key", 0)
        ma.key_down()
        assert wait_for(lambda: not ma._running)
        return ma.groups[0][0]
    child = press()
    assert host.contexts.get(child.sdk_context) is child  # kurz danach gilt der Kontext noch (setImage)
    monkeypatch.setattr(plugins, "HIDDEN_GRACE", -1)
    for __ in range(20):
        press()
    sent = [m["event"] for m in host.procs["com.t"].sent]
    assert sent.count("keyDown") == 21 and sent.count("keyUp") == 21
    assert host.contexts == {}  # bisher je Seitenwechsel und Druck eine Aktion mehr


def test_raw_base64_image_with_plugin_dir(tmp_path):
    from freestream import plugins
    img = Image.frombytes("RGB", (144, 144), random.Random(1).randbytes(144 * 144 * 3))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    data = base64.b64encode(buf.getvalue()).decode()
    assert len(data) > 4096
    out = plugins.decode_image(data, tmp_path)  # bisher OSError „File name too long“
    assert out is not None and out.size == (144, 144)
    Image.new("RGB", (8, 8), (0, 255, 0)).save(tmp_path / "icon.png")
    assert plugins.decode_image("icon", tmp_path).getpixel((1, 1))[:3] == (0, 255, 0)  # Dateiname geht weiter


def test_updated_plugin_is_restarted(tmp_path):
    from freestream import plugins

    class Daemon:
        controllers = {}
        cfg = None
    plugins.install(_plugin_src(tmp_path, "com.up", "1.0"))
    h = plugins.PluginHost(Daemon(), publish=False)
    try:
        old = h.procs["com.up"]
        assert old.proc.poll() is None
        h.sync()
        assert h.procs["com.up"] is old  # nichts geändert: läuft weiter
        v2 = _plugin_src(tmp_path, "com.up", "2.0")
        plugins.install(v2)
        h.sync()
        new = h.procs["com.up"]
        assert new is not old and new.info.version == "2.0" and new.proc.poll() is None
        assert old.stopping and old.proc.poll() is not None
        plugins.install(v2)  # dieselbe Version noch einmal installiert
        h.sync()
        assert h.procs["com.up"] is not new
    finally:
        h.stop()


def test_concurrent_plugin_syncs_start_one_process(tmp_path, monkeypatch):
    """Dienst (alle 2 s) und Plugin-Seite im Fenster rufen sync() gleichzeitig auf."""
    from freestream import plugins

    class Daemon:
        controllers = {}
        cfg = None
    started = []
    real_start = plugins.PluginProcess.start

    def start(self):
        started.append(self)
        time.sleep(0.05)  # Zeit für einen zweiten sync()
        return real_start(self)
    monkeypatch.setattr(plugins.PluginProcess, "start", start)
    plugins.install(_plugin_src(tmp_path, "com.up", "1.0"))
    h = plugins.PluginHost(Daemon(), publish=False)

    class Slow(dict):  # Thread-Wechsel zwischen Prüfung und pop (wie unter Last)
        def __contains__(self, key):
            found = super().__contains__(key)
            time.sleep(0.01)
            return found
    h.procs = Slow(h.procs)
    errors = []

    def sync():
        try:
            h.sync()
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    try:
        plugins.install(_plugin_src(tmp_path, "com.up", "2.0"))
        threads = [threading.Thread(target=sync) for __ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors  # bisher KeyError aus dem zweiten pop (beendet die Schleife des Dienstes)
        assert len(started) == 2 and h.procs["com.up"] is started[1]  # 2.0 genau einmal gestartet
        assert started[0].stopping and started[1].info.version == "2.0"
    finally:
        h.stop()
        for p in started:
            p.stop()


# ==========================================================================
# Discord
# ==========================================================================
def _fake_discord(path, seen, closed):
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(5)

    def recv(c, n):
        buf = b""
        while len(buf) < n:
            chunk = c.recv(n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return buf

    def send(c, obj):
        d = json.dumps(obj).encode()
        c.sendall(struct.pack("<II", 1, len(d)) + d)

    def handle(c):
        try:
            op, n = struct.unpack("<II", recv(c, 8))
            recv(c, n)
            send(c, {"cmd": "DISPATCH", "evt": "READY", "data": {"v": 1}})
            while True:
                op, n = struct.unpack("<II", recv(c, 8))
                m = json.loads(recv(c, n))
                seen.append(m["cmd"])
                data = {"code": "abc"} if m["cmd"] == "AUTHORIZE" else {}
                send(c, {"cmd": m["cmd"], "nonce": m["nonce"], "data": data})
        except (EOFError, OSError):
            closed.append(c)

    def serve():
        while True:
            try:
                c, __ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(c,), daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()
    return srv


def _discord_threads():
    return {t for t in threading.enumerate() if t.name == "discord"}


def test_discord_revoked_refresh_logs_in_again(tmp_path):
    from freestream import discord
    seen, closed = [], []
    srv = _fake_discord(tmp_path / "discord-ipc-0", seen, closed)
    tok = tmp_path / "token.json"
    tok.write_text(json.dumps({"access_token": "alt", "refresh_token": "widerrufen", "expires_at": time.time() - 9}))
    cl = discord.DiscordClient("123", "s", token_file=tok, sock_paths=[tmp_path / "discord-ipc-0"])
    grants = []

    def token_request(fields):
        grants.append(fields["grant_type"])
        if fields["grant_type"] == "refresh_token":
            raise discord.DiscordAuthError("invalid_grant")
        return {"access_token": "neu", "expires_at": time.time() + 3600}
    cl._token_request = token_request
    try:
        cl.connect()  # bisher: invalid_grant bei jedem Tastendruck, AUTHORIZE nie erreicht
        assert cl.ready and grants == ["refresh_token", "authorization_code"]
        assert seen[:2] == ["AUTHORIZE", "AUTHENTICATE"]
        assert not tok.exists()  # widerrufener Zugang ist weg
    finally:
        cl._disconnect()
        srv.close()


def test_discord_failed_connect_closes_socket_and_reader(tmp_path):
    from freestream import discord
    seen, closed = [], []
    srv = _fake_discord(tmp_path / "discord-ipc-0", seen, closed)
    tok = tmp_path / "token.json"
    tok.write_text(json.dumps({"access_token": "alt", "refresh_token": "r", "expires_at": time.time() - 9}))
    cl = discord.DiscordClient("123", "s", token_file=tok, sock_paths=[tmp_path / "discord-ipc-0"])

    def offline(fields):
        raise discord.DiscordError("offline")
    cl._token_request = offline
    before = _discord_threads()
    try:
        for __ in range(3):
            with pytest.raises(discord.DiscordError):
                cl.connect()
        assert "AUTHORIZE" not in seen  # offline: kein Zustimmungsdialog bei jedem Druck
        assert tok.exists()
        assert wait_for(lambda: len(closed) == 3)  # Discord sieht jede Verbindung wieder zu
        assert wait_for(lambda: not (_discord_threads() - before))  # kein Lese-Thread bleibt hängen
    finally:
        srv.close()


def test_discord_token_errors_and_client_switch(tmp_path, monkeypatch):
    from freestream import discord
    cl = discord.DiscordClient("123", "s", token_file=tmp_path / "t.json")

    def rejected(req, timeout=0):
        raise urllib.error.HTTPError(discord.TOKEN_URL, 400, "Bad Request", {}, None)
    monkeypatch.setattr(discord.urllib.request, "urlopen", rejected)
    with pytest.raises(discord.DiscordAuthError):
        cl._token_request({"grant_type": "refresh_token", "refresh_token": "x"})

    def offline(req, timeout=0):
        raise urllib.error.URLError("offline")
    monkeypatch.setattr(discord.urllib.request, "urlopen", offline)
    with pytest.raises(discord.DiscordError) as e:
        cl._token_request({"grant_type": "refresh_token", "refresh_token": "x"})
    assert not isinstance(e.value, discord.DiscordAuthError)
    # andere Client-ID in den Einstellungen: alte Verbindung wird geschlossen
    a, b = socket.socketpair()
    old = discord.DiscordClient("1", "a")
    old.sock = a
    discord._client.update(key=("1", "a"), obj=old)
    try:
        new = discord.client({"discord": {"client_id": "2", "client_secret": "b"}})
        assert new is not old and old.sock is None
        b.settimeout(2)
        assert b.recv(1) == b""
    finally:
        discord._client.update(key=None, obj=None)
        b.close()


# ==========================================================================
# Home Assistant
# ==========================================================================
def test_hass_reuses_tls_context(monkeypatch):
    from freestream import hass
    made = []
    real = hass.ssl.create_default_context
    monkeypatch.setattr(hass.ssl, "create_default_context", lambda *a, **k: made.append(1) or real(*a, **k))
    seen = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"state": "on"}'

    def urlopen(req, timeout=None, context=None):
        seen.append(context)
        return Resp()
    monkeypatch.setattr(hass.urllib.request, "urlopen", urlopen)
    cl = hass.HAClient("https://ha.local:8123", "tok", verify_ssl=False)
    for __ in range(5):
        assert cl.request("GET", "/api/states/light.x") == {"state": "on"}
    assert len(made) == 1 and all(c is seen[0] for c in seen)  # bisher ein neuer Kontext je Abfrage
    assert seen[0].verify_mode == ssl.CERT_NONE and not seen[0].check_hostname
    seen.clear()
    cl = hass.HAClient("https://ha.local:8123", "tok")
    cl.request("GET", "/api/")
    cl.request("GET", "/api/")
    assert len(made) == 2 and seen[0] is seen[1]
    assert seen[0].verify_mode == ssl.CERT_REQUIRED and seen[0].check_hostname
    seen.clear()
    hass.HAClient("http://ha.local:8123", "tok").request("GET", "/api/")
    assert seen == [None]


# ==========================================================================
# FreeWave-Export mit unlesbarem Hintergrundbild
# ==========================================================================
def test_export_keeps_profile_when_background_unreadable(tmp_path, monkeypatch):
    from freestream import deckexport, editdoc
    monkeypatch.setattr(deckexport, "notify_freedeck", lambda: False)
    mk2, mini = "20GBA9901", "20GAI9901"
    chans, mixes = ["system", "music", "browser"], ["monitor", "stream"]
    good = tmp_path / "bg.png"
    Image.new("RGB", (400, 200), (10, 200, 10)).save(good)
    deckexport.export(mk2, chans, mixes=mixes, background=str(good))
    root = deckexport.managed_profile(mk2).root

    def keys():
        doc = editdoc.ProfileDoc(root)
        return sum(len(doc.page(pg).items()) for pg in doc.page_ids)
    before, bg = keys(), (root / deckexport.BG_FILE).read_bytes()
    assert before
    bad = tmp_path / "kaputt.png"
    bad.write_text("kein Bild")
    with pytest.raises(deckexport.ExportError):
        deckexport.export(mk2, chans, mixes=mixes, background=str(bad))
    assert keys() == before  # bisher: Profil leer
    assert (root / deckexport.BG_FILE).read_bytes() == bg
    with pytest.raises(deckexport.ExportError):
        deckexport.export(mini, chans, mixes=mixes, background=str(bad))
    assert deckexport.managed_profile(mini) is None  # kein leeres Profil angelegt


# ==========================================================================
# Beenden: Inaktiv-Bild (Tasten, Neo-Infoleiste)
# ==========================================================================
def _deck_ctrl(tmp_path, monkeypatch, deck_type):
    pytest.importorskip("StreamDeck")
    import StreamDeck.Transport.Dummy as dummy
    from StreamDeck.DeviceManager import DeviceManager

    from freestream.config import Config, State
    from freestream.engine import DeckController
    from freestream.profiles import ProfileStore
    monkeypatch.setattr(dummy.Dummy.Device, "read", lambda self, length: None)
    deck = next(d for d in DeviceManager(transport="dummy").enumerate() if d.deck_type() == deck_type)
    deck.open()
    st = State()
    st.save = lambda: None
    ctrl = DeckController(deck, ProfileStore(paths.PROFILES_DIR), Config(), st, None, None)
    idle = tmp_path / "idle.png"
    Image.new("RGB", (400, 200), (200, 40, 40)).save(idle)
    ctrl.cfg.device(ctrl.serial)["idle_image"] = str(idle)
    calls = []
    real_close = deck.close
    deck.reset = lambda: calls.append("reset")
    deck.set_key_image = lambda k, img: calls.append(("key", k, threading.current_thread().name))
    deck.set_touchscreen_image = lambda *a: calls.append("touch")
    deck.set_screen_image = lambda img: calls.append("screen")
    deck.set_key_color = lambda k, r, g, b: calls.append(("color", k, (r, g, b)))
    deck.set_brightness = lambda v: calls.append("brightness")
    deck.close = lambda: calls.append("close")
    return ctrl, calls, real_close


def test_late_redraws_do_not_overwrite_idle_image(tmp_path, monkeypatch):
    ctrl, calls, real_close = _deck_ctrl(tmp_path, monkeypatch, "Stream Deck Original")
    done = threading.Event()

    def late():  # wie der Pegel-Thread oder der Takt, der gerade noch eine Taste zeichnet
        while not done.is_set():
            for k in range(ctrl.key_count):
                ctrl.render_key(k)
    t = threading.Thread(target=late, name="late")
    try:
        t.start()
        assert wait_for(lambda: any(c[0] == "key" and c[2] == "late" for c in calls if isinstance(c, tuple)))
        ctrl.stop()
        time.sleep(0.05)
        done.set()
        t.join()
        last = {}
        for c in calls:
            if isinstance(c, tuple) and c[0] == "key":
                last[c[1]] = c[2]
        assert set(last) == set(range(ctrl.key_count))
        assert set(last.values()) == {"MainThread"}  # zuletzt überall das Inaktiv-Bild
        n = len(calls)
        ctrl.render_all()
        ctrl.render_infobar()
        ctrl.update_touch_keys()
        assert len(calls) == n
    finally:
        done.set()
        real_close()


def test_neo_idle_image_clears_info_bar_and_touch_keys(tmp_path, monkeypatch):
    ctrl, calls, real_close = _deck_ctrl(tmp_path, monkeypatch, "Stream Deck Neo")
    try:
        assert ctrl.screen_size[0] and ctrl.touch_key_count == 2
        ctrl.stop()
        assert "reset" not in calls and calls[-1] == "close"
        assert "screen" in calls  # sonst bliebe die Uhr der Infoleiste stehen
        assert ("color", ctrl.key_count, (0, 0, 0)) in calls and ("color", ctrl.key_count + 1, (0, 0, 0)) in calls
        n = len(calls)
        ctrl.render_infobar()  # später Sekundentakt
        ctrl.update_touch_keys()
        assert len(calls) == n
    finally:
        real_close()



def test_late_page_switch_after_stop_registers_nothing(tmp_path, monkeypatch):
    """Tastendruck, der auf die Sperre wartet, während stop() läuft (z. B. virtuelles Deck entfernt)."""
    from freestream import editdoc
    ctrl, calls, real_close = _deck_ctrl(tmp_path, monkeypatch, "Stream Deck Original")
    doc = editdoc.ProfileDoc(editdoc.create_profile("Test", ctrl.model, paths.PROFILES_DIR))
    doc.place(doc.page_ids[0], ("key", 0, 0), "com.elgato.streamdeck.system.hotkey")
    doc.save()
    ctrl.store.reload()
    pid = next(iter(ctrl.store.profiles))
    seen = []

    class Host:
        def will_appear(self, c, a):
            seen.append(("appear", a))

        def will_disappear(self, c, a):
            seen.append(("disappear", a))
    ctrl.plugin_host = Host()
    try:
        ctrl.load_profile(pid)
        assert ctrl.keys and seen == [("appear", ctrl.keys[0])]
        with ctrl.lock:  # stop() hält die Sperre, der Ereignis-Thread wartet auf sie
            late = threading.Thread(target=ctrl.goto_page, args=(0,))
            late.start()
            ctrl.stop()
        late.join(5)
        assert not ctrl.keys and seen[-1][0] == "disappear"  # bisher neue Aktionen am beendeten Gerät
        ctrl.switch_profile(pid)
        assert not ctrl.keys and seen[-1][0] == "disappear"
    finally:
        real_close()
