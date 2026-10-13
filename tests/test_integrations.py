"""Tests für Plugin-Host, OBS, Discord, Mixer, Wave-Mikrofon und Selbsttest.

Die Gegenstellen (OBS, Discord, PipeWire) werden simuliert; echte Programme
oder Geräte sind nicht nötig. Der Node.js-Test läuft nur, wenn `node` da ist.
"""
import hashlib
import base64
import json
import os
import shutil
import socket
import struct
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

from lindeck import actions as A
from lindeck import catalog as C
from lindeck import native, ws
from lindeck.models import Geometry


@pytest.fixture(autouse=True)
def xdg(tmp_path, monkeypatch):
    from lindeck import paths
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(paths, "PLUGINS_DIR", tmp_path / "data" / "plugins")
    monkeypatch.setattr(paths, "PLUGIN_DATA_DIR", tmp_path / "data" / "plugin-data")
    monkeypatch.setattr(paths, "RESOURCES_DIR", tmp_path / "data" / "resources")
    monkeypatch.setattr(paths, "ASSETS_DIR", tmp_path / "data" / "assets")
    (tmp_path / "state").mkdir()
    return tmp_path


class Ctrl:
    """Was Aktionen und Plugin-Host vom Gerät brauchen."""
    serial, model, geometry = "SER1", "20GBA9901", Geometry(5, 3)

    def __init__(self):
        self.redraws, self.overlays, self.switched = [], [], []
        self.plugin_host = None

    def redraw_action(self, a):
        self.redraws.append(a)

    def show_overlay(self, a, kind, seconds=1.2):
        self.overlays.append(kind)

    def switch_profile(self, p):
        self.switched.append(p)


def make_action(data, cfg=None, ctrl=None, slot=("key", 0)):
    ctrl = ctrl or Ctrl()
    a = A.create_action(data, None, A.Context(ctrl, cfg or {}, None, None))
    a.slot = slot
    return a, ctrl


def wait_for(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


# ==========================================================================
# WebSocket
# ==========================================================================
def test_websocket_roundtrip():
    got = []

    def on_conn(conn):
        msg = conn.recv()
        got.append(msg)
        conn.send(msg.upper() * 3000)  # > 64 KiB: 8-Byte-Längenfeld
    srv = ws.Server(on_conn)
    c = ws.connect("127.0.0.1", srv.port)
    c.send("hallo")
    assert c.recv() == "HALLO" * 3000
    assert got == ["hallo"]
    c.close()
    srv.close()


# ==========================================================================
# Plugin-Host
# ==========================================================================
PY_PLUGIN = r'''
import json, sys
sys.path.insert(0, %r)
from lindeck import ws
a = sys.argv
port, uuid, reg = a[a.index("-port") + 1], a[a.index("-pluginUUID") + 1], a[a.index("-registerEvent") + 1]
info = json.loads(a[a.index("-info") + 1])
c = ws.connect("127.0.0.1", int(port))
c.send(json.dumps({"event": reg, "uuid": uuid}))
while True:
    e = json.loads(c.recv())
    ctx = e.get("context")
    if e["event"] == "willAppear":
        c.send(json.dumps({"event": "setTitle", "context": ctx, "payload": {"title": info["application"]["platform"]}}))
    elif e["event"] == "keyDown":
        n = e["payload"]["settings"].get("n", 0) + 1
        c.send(json.dumps({"event": "setSettings", "context": ctx, "payload": {"n": n}}))
        c.send(json.dumps({"event": "setTitle", "context": ctx, "payload": {"title": "n=%%d" %% n}}))
        c.send(json.dumps({"event": "setGlobalSettings", "payload": {"total": n}}))
        c.send(json.dumps({"event": "showOk", "context": ctx}))
    elif e["event"] == "dialRotate":
        c.send(json.dumps({"event": "setFeedback", "context": ctx, "payload": {"value": "t=%%d" %% e["payload"]["ticks"],
                                                                           "indicator": {"value": 40}}}))
    elif e["event"] == "keyUp":
        svg = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10" fill="#00ff00"/></svg>'
        c.send(json.dumps({"event": "setImage", "context": ctx, "payload": {"image": "data:image/svg+xml;charset=utf8," + svg}}))
'''

NODE_PLUGIN = r'''
const a = process.argv, get = (k) => a[a.indexOf(k) + 1];
const ws = new WebSocket("ws://127.0.0.1:" + get("-port"));
ws.onopen = () => ws.send(JSON.stringify({event: get("-registerEvent"), uuid: get("-pluginUUID")}));
ws.onmessage = (m) => { const e = JSON.parse(m.data);
  if (e.event === "keyDown") ws.send(JSON.stringify({event: "setTitle", context: e.context, payload: {title: "node " + process.version}}));
};
'''


def _make_plugin(tmp, uuid, code, manifest_extra):
    src = tmp / "src" / f"{uuid}.sdPlugin"
    (src / "bin").mkdir(parents=True)
    name = "bin/plugin.js" if code is NODE_PLUGIN else "bin/plugin"
    (src / name).write_text(code if code is NODE_PLUGIN else "#!" + sys.executable + "\n" + code)
    os.chmod(src / name, 0o755)
    manifest = {"UUID": uuid, "Name": uuid.split(".")[-1], "Version": "1.2", "CodePath": name,
                "Actions": [{"UUID": f"{uuid}.act", "Name": "Aktion", "States": [{}, {}],
                             "Controllers": ["Keypad", "Encoder"]}]}
    manifest.update(manifest_extra)
    (src / "manifest.json").write_text(json.dumps(manifest))
    zf = tmp / f"{uuid}.streamDeckPlugin"
    with zipfile.ZipFile(zf, "w") as z:
        for f in src.rglob("*"):
            z.write(f, f.relative_to(src.parent))
    return zf


def test_plugin_host_with_linux_plugin(xdg, tmp_path):
    from lindeck import plugins, profiles
    repo = str(Path(__file__).resolve().parents[1])
    zf = _make_plugin(tmp_path, "com.test.py", PY_PLUGIN % repo, {"CodePath": "bin/plugin.exe",
                                                                   "CodePathLin": "bin/plugin"})
    assert plugins.install(zf) == ["com.test.py"]
    info = plugins.installed()["com.test.py"]
    assert info.runnable and info.runtime == "exec"
    # Aktion liegt in einer echten Profilseite, damit setSettings gespeichert wird
    page = tmp_path / "page"
    page.mkdir()
    data = {"UUID": "com.test.py.act", "Settings": {}, "States": [{}, {}], "State": 0}
    (page / "manifest.json").write_text(json.dumps({"Controllers": [{"Type": "Keypad", "Actions": {"0,0": data}}]}))
    pg = profiles.load_page("p", page)
    ctrl = Ctrl()

    class Daemon:
        controllers = {"x": ctrl}
        cfg = None
    host = plugins.PluginHost(Daemon())
    ctrl.plugin_host = host
    try:
        act = A.create_action(pg.keypad[(0, 0)], pg, A.Context(ctrl, {}, None, None))
        act.slot = ("key", 7)
        assert type(act) is A.PluginAction and A.is_supported(act.uuid)
        host.will_appear(ctrl, act)
        assert wait_for(lambda: act.title() == "linux")
        host.key_down(act)
        assert wait_for(lambda: act.title() == "n=1" and "ok" in ctrl.overlays)
        host.key_up(act)
        assert act.state == 1  # automatischer Zustandswechsel bei zwei Zuständen
        assert wait_for(lambda: act.image_override is not None)
        assert act.image_override.getpixel((5, 5))[:3] == (0, 255, 0)
        saved = json.loads((page / "manifest.json").read_text())["Controllers"][0]["Actions"]["0,0"]
        assert saved["Settings"] == {"n": 1}
        from lindeck import paths
        assert wait_for(lambda: (paths.PLUGIN_DATA_DIR / "com.test.py.json").exists())
        # Dial mit Feedback
        dial, _ = make_action({"UUID": "com.test.py.act", "Settings": {}}, ctrl=ctrl, slot=("dial", 1))
        host.will_appear(ctrl, dial)
        host.dial_rotate(dial, -2)
        assert wait_for(lambda: dial.feedback().get("value") == "t=-2")
        assert dial.feedback()["indicator"] == 40
    finally:
        host.stop()


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js nicht installiert")
def test_plugin_host_with_node_plugin(xdg, tmp_path):
    from lindeck import plugins
    zf = _make_plugin(tmp_path, "com.test.node", NODE_PLUGIN, {"Nodejs": {"Version": "20"}})
    plugins.install(zf)
    assert plugins.installed()["com.test.node"].runtime == "node"
    ctrl = Ctrl()

    class Daemon:
        controllers = {"x": ctrl}
        cfg = None
    host = plugins.PluginHost(Daemon())
    try:
        act, _ = make_action({"UUID": "com.test.node.act", "Settings": {}}, ctrl=ctrl)
        host.will_appear(ctrl, act)
        assert wait_for(lambda: host.procs["com.test.node"].conn is not None)
        host.key_down(act)
        assert wait_for(lambda: (act.title() or "").startswith("node v"))
    finally:
        host.stop()


def test_windows_only_plugin_is_reported(xdg, tmp_path):
    from lindeck import plugins
    src = tmp_path / "com.win.only.sdPlugin"
    src.mkdir()
    (src / "manifest.json").write_text(json.dumps({"UUID": "com.win.only", "CodePath": "Plugin.exe",
                                                   "Actions": [{"UUID": "com.win.only.a"}]}))
    plugins.install(src)
    info = plugins.installed()["com.win.only"]
    assert not info.runnable and "Windows" in info.reason
    assert not A.is_supported("com.win.only.a")


def test_decode_image_variants():
    from PIL import Image
    import io
    from lindeck.plugins import decode_image
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (1, 2, 3)).save(buf, "PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    assert decode_image("data:image/png;base64," + b64).getpixel((0, 0))[:3] == (1, 2, 3)
    assert decode_image(b64).size == (4, 4)
    assert decode_image("") is None


# ==========================================================================
# OBS (simulierter obs-websocket-Server)
# ==========================================================================
class FakeOBS:
    PASSWORD, SALT, CHALLENGE = "geheim", "salz", "herausforderung"

    def __init__(self):
        self.scene, self.recording, self.muted = "Start", False, {"Mic/Aux": False}
        self.requests = []
        self.conns = []
        self.srv = ws.Server(self.on_conn)

    def on_conn(self, conn):
        conn.send(json.dumps({"op": 0, "d": {"rpcVersion": 1, "authentication": {
            "salt": self.SALT, "challenge": self.CHALLENGE}}}))
        ident = json.loads(conn.recv())["d"]
        from lindeck.obs import auth_string
        if ident.get("authentication") != auth_string(self.PASSWORD, self.SALT, self.CHALLENGE):
            conn.close()
            return
        conn.send(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))
        self.conns.append(conn)
        try:
            while True:
                d = json.loads(conn.recv())["d"]
                self.requests.append(d["requestType"])
                data, event = self.handle(d["requestType"], d.get("requestData") or {})
                conn.send(json.dumps({"op": 7, "d": {"requestType": d["requestType"], "requestId": d["requestId"],
                                                     "requestStatus": {"result": True, "code": 100},
                                                     "responseData": data}}))
                if event:
                    conn.send(json.dumps({"op": 5, "d": {"eventType": event[0], "eventData": event[1]}}))
        except ws.Closed:
            pass

    def handle(self, rt, data):
        if rt == "GetCurrentProgramScene":
            return {"currentProgramSceneName": self.scene}, None
        if rt == "SetCurrentProgramScene":
            self.scene = data["sceneName"]
            return {}, ("CurrentProgramSceneChanged", {"sceneName": self.scene})
        if rt == "GetRecordStatus":
            return {"outputActive": self.recording, "outputPaused": False}, None
        if rt == "ToggleRecord":
            self.recording = not self.recording
            return {}, ("RecordStateChanged", {"outputActive": self.recording, "outputState": "x"})
        if rt == "GetInputMute":
            return {"inputMuted": self.muted[data["inputName"]]}, None
        if rt == "ToggleInputMute":
            self.muted[data["inputName"]] = not self.muted[data["inputName"]]
            return {}, ("InputMuteStateChanged", {"inputName": data["inputName"],
                                                  "inputMuted": self.muted[data["inputName"]]})
        if rt == "GetInputVolume":
            return {"inputVolumeDb": -10.0}, None
        if rt == "GetStudioModeEnabled":
            return {"studioModeEnabled": False}, None
        return {"outputActive": False}, None


def test_obs_actions():
    from lindeck import obs
    fake = FakeOBS()
    cfg = {"obs": {"host": "127.0.0.1", "port": fake.srv.port, "password": FakeOBS.PASSWORD}}
    try:
        scene, _ = make_action({"UUID": "com.lindeck.obs.scene", "Settings": {"scene": "Live"}}, cfg)
        assert scene.kind == "scene"
        assert obs.wait_connected(scene.obs, 5)
        assert wait_for(lambda: scene.obs.state["scene"] == "Start")
        assert scene.active() is False
        scene.key_down()
        assert wait_for(lambda: scene.active() is True) and fake.scene == "Live"
        # Elgato-OBS-Plugin aus importiertem Profil: Szenenname aus dem Titel
        elg, _ = make_action({"UUID": "com.elgato.obsstudio.scene", "Settings": {},
                              "States": [{"Title": "Pause"}]}, cfg)
        elg.key_down()
        assert wait_for(lambda: fake.scene == "Pause")
        rec, _ = make_action({"UUID": "com.elgato.obsstudio.record", "States": [{}, {}]}, cfg)
        rec.key_down()
        assert wait_for(lambda: rec.obs.state["recording"])
        rec.tick()
        assert rec.state == 1 and rec.status_text() == "läuft"
        mic, _ = make_action({"UUID": "com.lindeck.obs.audio", "Settings": {"source": "Mic/Aux"}}, cfg)
        assert mic.active() is True
        mic.key_down()
        assert wait_for(lambda: mic.active() is False) and fake.muted["Mic/Aux"]
    finally:
        obs.client(cfg).close()
        fake.srv.close()


def test_obs_auth_string():
    from lindeck.obs import auth_string
    secret = base64.b64encode(hashlib.sha256(b"pwsalt").digest()).decode()
    expect = base64.b64encode(hashlib.sha256((secret + "ch").encode()).digest()).decode()
    assert auth_string("pw", "salt", "ch") == expect


# ==========================================================================
# Discord (simulierter IPC-Socket)
# ==========================================================================
def test_discord_mute_and_ptt(tmp_path):
    from lindeck import discord
    sock_path = tmp_path / "discord-ipc-0"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)
    voice = {"mute": False, "deaf": False}
    seen = []

    def serve():
        c, _ = srv.accept()

        def recv():
            op, n = struct.unpack("<II", c.recv(8))
            data = b""
            while len(data) < n:
                data += c.recv(n - len(data))
            return op, json.loads(data)

        def send(op, obj):
            d = json.dumps(obj).encode()
            c.sendall(struct.pack("<II", op, len(d)) + d)
        op, hs = recv()
        assert hs["client_id"] == "123"
        send(1, {"cmd": "DISPATCH", "evt": "READY", "data": {"v": 1}})
        try:
            while True:
                op, m = recv()
                seen.append(m["cmd"])
                data = {}
                if m["cmd"] == "AUTHENTICATE":
                    assert m["args"]["access_token"] == "tok"
                elif m["cmd"] in ("GET_VOICE_SETTINGS", "SET_VOICE_SETTINGS"):
                    voice.update(m["args"])
                    data = dict(voice)
                send(1, {"cmd": m["cmd"], "nonce": m["nonce"], "data": data})
        except (struct.error, OSError):
            pass
    threading.Thread(target=serve, daemon=True).start()
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"access_token": "tok", "expires_at": time.time() + 3600}))
    cl = discord.DiscordClient("123", "s", token_file=token, sock_paths=[sock_path])
    discord._client.update(key=("123", "s"), obj=cl)
    cfg = {"discord": {"client_id": "123", "client_secret": "s"}}
    mute, _ = make_action({"UUID": "com.elgato.discord.mute", "States": [{}, {}]}, cfg)
    assert mute.func == "mute"
    mute.key_down()
    assert voice["mute"] is True and mute.active() is True
    ptt, _ = make_action({"UUID": "com.lindeck.discord.voice", "Settings": {"function": "pushtotalk"}}, cfg)
    ptt.key_down()
    assert voice["mute"] is False
    ptt.key_up()
    assert voice["mute"] is True
    assert seen[:3] == ["AUTHENTICATE", "GET_VOICE_SETTINGS", "SUBSCRIBE"]
    srv.close()


def test_discord_without_app_shows_alert():
    from lindeck import discord
    discord._client.update(key=None, obj=None)
    a, ctrl = make_action({"UUID": "com.lindeck.discord.voice", "Settings": {"function": "mute"}}, {})
    a.key_down()
    assert ctrl.overlays == ["alert"]


# ==========================================================================
# Mixer (pactl simuliert)
# ==========================================================================
class FakePactl:
    def __init__(self):
        self.cmds, self.next_id = [], 100
        self.inputs = {}   # Index -> props/volume/mute
        self.default_sink = "alsa_output.headphones"

    def __call__(self, args):
        self.cmds.append(args)
        if args[0] == "load-module":
            self.next_id += 1
            name = next((a.split("=", 2)[-1] for a in args if a.startswith("sink_input_properties=")), None)
            if name:
                self.inputs[str(self.next_id)] = {"index": str(self.next_id), "volume": 100, "mute": False,
                                                  "props": {"media.name": name}}
            return str(self.next_id)
        if args[0] == "get-default-sink":
            return self.default_sink
        if args[0] == "get-default-source":
            return "alsa_input.mic"
        if args[0] == "set-sink-input-volume":
            self.inputs[args[1]]["volume"] = int(args[2].rstrip("%"))
        if args[0] == "set-sink-input-mute":
            b = self.inputs[args[1]]
            b["mute"] = (not b["mute"]) if args[2] == "toggle" else args[2] == "1"
        return ""

    def blocks(self, what):
        if what == "sink-inputs":
            return list(self.inputs.values()) + [
                {"index": "7", "volume": 80, "mute": False, "props": {"application.name": "Spotify"}},
                {"index": "8", "volume": 80, "mute": False, "props": {"application.name": "Firefox"}},
                {"index": "9", "volume": 80, "mute": False, "props": {"application.name": "Unbekannt"}}]
        if what == "sinks":
            return [{"name": "alsa_output.headphones", "description": "Kopfhörer"},
                    {"name": "alsa_output.speakers", "description": "Lautsprecher"}]
        return []


@pytest.fixture
def pactl(monkeypatch):
    from lindeck import system
    fake = FakePactl()
    monkeypatch.setattr(system, "_pactl_blocks", fake.blocks)
    monkeypatch.setattr(system, "have", lambda c: True)
    from lindeck import mixer
    monkeypatch.setattr(mixer, "_run", fake)
    return fake


def test_mixer_setup_volumes_routing(pactl):
    from lindeck.mixer import Mixer
    mx = Mixer({"mixer": {"routes": {"firefox": "game"}}}, run=pactl)
    mods = mx.setup()
    loads = [c for c in pactl.cmds if c[0] == "load-module"]
    assert len(mods) == len(loads) == 1 + 6 * 3 + 2
    assert any("sink_name=lindeck_music" in " ".join(c) for c in loads)
    assert any("sink=alsa_output.headphones" in c and "source=lindeck_music.monitor" in c for c in loads)
    assert ["set-default-sink", "lindeck_system"] in pactl.cmds
    assert mx.get("mic", "monitor") == (100, True)  # eigenes Mikro nicht auf dem Kopfhörer
    mx.set_volume("music", "stream", 40)
    assert mx.get("music", "stream") == (40, False) and mx.get("music", "monitor") == (100, False)
    mx.change_volume("music", "both", -10)
    assert mx.get("music", "stream")[0] == 30 and mx.get("music", "monitor")[0] == 90
    assert mx.route_streams() == 2
    assert ["move-sink-input", "7", "lindeck_music"] in pactl.cmds
    assert ["move-sink-input", "8", "lindeck_game"] in pactl.cmds  # eigene Regel schlägt Standard
    assert mx.route_streams() == 0  # jeder Stream nur einmal
    assert mx.set_monitor_output("Lautsprecher") == "alsa_output.speakers"
    # Aktion (auch Wave-Link-Plugin-Aktion aus einem Elgato-Profil)
    a, _ = make_action({"UUID": "com.elgato.wavelink.mixermute", "Settings": {"mixerId": "Musik", "mixType": "local"}},
                       {})
    assert type(a).__name__ == "MixerAction" and a.channel == "music" and a.mix == "monitor"
    a.key_down()
    assert mx.get("music", "monitor")[1] is True
    mx.teardown()
    assert any(c[0] == "unload-module" for c in pactl.cmds)
    assert ["set-default-sink", "alsa_output.headphones"] in pactl.cmds


# ==========================================================================
# Wave-Mikrofon
# ==========================================================================
def test_wave_filter_conf():
    from lindeck.wavemic import PROCESSED, filter_conf
    both = filter_conf("alsa_input.usb-Elgato_Wave_3", True, 120, True)
    assert '"Freq" = 120.0' in both and "fast_lookahead_limiter_1913" in both
    assert 'output = "lowcut:Out" input = "limiter:Input 1"' in both
    assert 'target.object  = "alsa_input.usb-Elgato_Wave_3"' in both and PROCESSED in both
    only_cut = filter_conf("x", True, 80, False)
    assert 'outputs = [ "lowcut:Out" ]' in only_cut and "limiter" not in only_cut
    with pytest.raises(ValueError):
        filter_conf("x", False, 80, False)


def test_wave_detection_and_actions(monkeypatch, xdg):
    from lindeck import system, wavemic
    monkeypatch.setattr(system, "_pactl_blocks", lambda what: [
        {"name": "alsa_input.pci-0000_00_1f.3.analog-stereo", "description": "Eingebautes Mikrofon", "props": {}},
        {"name": "alsa_input.usb-Elgato_Systems_Elgato_Wave_3_BS123-00.mono-fallback",
         "description": "Elgato Wave:3 Mono", "props": {"device.vendor.name": "Elgato Systems"}}])
    assert wavemic.find_source().startswith("alsa_input.usb-Elgato")
    wavemic._instance.clear()
    applied = []
    monkeypatch.setattr(wavemic.WaveMic, "apply", lambda self: applied.append(dict(self.settings)))
    a, _ = make_action({"UUID": "com.lindeck.wavemic.control", "Settings": {"function": "lowcut"}}, {})
    assert a.active() is False
    a.key_down()
    assert a.active() is True and applied[-1]["lowcut"] is True
    assert json.loads((xdg / "state" / "wavemic.json").read_text())["lowcut"] is True
    assert native.lookup("com.elgato.wavelink.micmute") is native.WaveMicAction


# ==========================================================================
# Zuordnung und Katalog
# ==========================================================================
@pytest.mark.parametrize("uuid,cls,kind", [
    ("com.elgato.obsstudio.scene", native.OBSAction, "scene"),
    ("com.elgato.obsstudio.record", native.OBSAction, "record"),
    ("com.elgato.obsstudio.stream", native.OBSAction, "stream"),
    ("com.elgato.obsstudio.source", native.OBSAction, "source"),
    ("com.elgato.obsstudio.mixeraudio", native.OBSAction, "audio"),
    ("com.elgato.obsstudio.replaybuffer", native.OBSAction, "replay"),
    ("com.elgato.spotify.playpause", native.MediaAction, None),
    ("com.elgato.discord.deafen", native.DiscordAction, None),
    ("com.elgato.wavelink.setmonitormixoutput", native.MixerAction, None),
    ("com.foo.unbekannt.action", None, None),
])
def test_native_lookup(uuid, cls, kind):
    found = native.lookup(uuid)
    if cls is None:
        assert found is None and not A.is_builtin(uuid)
    else:
        assert issubclass(found, cls) and A.is_builtin(uuid)
        if kind:
            assert found.kind == kind


def test_native_catalog_actions_render(xdg):
    from lindeck import obs
    obs_cfg = {"obs": {"host": "127.0.0.1", "port": 1}}
    for t in C.CATALOG:
        if not t.uuid.startswith("com.lindeck."):
            continue
        data = C.new_action(t.uuid)
        a, _ = make_action(data, obs_cfg, slot=("dial", 0) if C.KEY not in t.controllers else ("key", 0))
        assert isinstance(a, native.NativeAction), t.uuid
        img = a.frames((72, 72))[0][0]
        assert img.size == (72, 72)
        a.feedback()
    obs.client(obs_cfg).close()


# ==========================================================================
# Hardware-Selbsttest (simuliertes Gerät)
# ==========================================================================
def test_selftest_with_simulated_presses(xdg):
    pytest.importorskip("StreamDeck")
    from StreamDeck.DeviceManager import DeviceManager
    from StreamDeck.Devices.StreamDeck import DialEventType, TouchscreenEventType
    from lindeck.selftest import SelfTest, passed, report_text
    deck = next(d for d in DeviceManager(transport="dummy").enumerate() if d.dial_count() == 4)
    deck.open()
    t = SelfTest(deck, out=lambda *a: None)

    def user():
        time.sleep(0.3)
        for k in range(t.keys):
            t.on_key(k, True)
            t.on_key(k, False)
        for i in range(t.dials):
            t.on_dial(i, DialEventType.TURN, 1)
            t.on_dial(i, DialEventType.TURN, -1)
            t.on_dial(i, DialEventType.PUSH, True)
        w = deck.touchscreen_image_format()["size"][0]
        for i in range(t.dials):
            t.on_touch(TouchscreenEventType.SHORT, {"x": i * w // t.dials + 5, "y": 10})
        t.on_touch(TouchscreenEventType.DRAG, {"x": 10, "y": 10, "x_out": 300, "y_out": 10})
    threading.Thread(target=user, daemon=True).start()
    res = t.run(timeout=10)
    deck.close()
    assert passed(res), res
    assert "Stream Deck +" in report_text(res)
