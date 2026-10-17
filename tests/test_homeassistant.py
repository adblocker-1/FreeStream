"""Tests für die Home-Assistant-Anbindung gegen einen simulierten Server auf 127.0.0.1."""
import gc
import json
import os
import socket
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from freestream import actions as A
from freestream import catalog as C
from freestream import hass, hasskeys, native

TOKEN = "geheimer-token-123"


class FakeHA:
    """Winziger Home-Assistant-Ersatz: /api/, /api/config, /api/states, /api/services."""

    def __init__(self):
        self.states = {
            "light.kueche": {"entity_id": "light.kueche", "state": "off",
                             "attributes": {"friendly_name": "Küche", "brightness": None}},
            "switch.kaffee": {"entity_id": "switch.kaffee", "state": "on",
                              "attributes": {"friendly_name": "Kaffeemaschine"}},
            "sensor.temp": {"entity_id": "sensor.temp", "state": "21.46",
                            "attributes": {"friendly_name": "Wohnzimmer", "unit_of_measurement": "°C"}},
            "scene.abend": {"entity_id": "scene.abend", "state": "2024-01-01T00:00:00",
                            "attributes": {"friendly_name": "Abend"}},
        }
        self.calls = []
        self.requests = 0
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _auth(self):
                fake.requests += 1
                if self.headers.get("Authorization") != "Bearer " + TOKEN:
                    self._send(401, {"message": "401: Unauthorized"})
                    return False
                return True

            def do_GET(self):
                if not self._auth():
                    return
                p = self.path
                if p == "/api/":
                    self._send(200, {"message": "API running."})
                elif p == "/api/config":
                    self._send(200, {"version": "2026.10.1", "location_name": "Zuhause"})
                elif p == "/api/states":
                    self._send(200, list(fake.states.values()))
                elif p.startswith("/api/states/"):
                    st = fake.states.get(p.rsplit("/", 1)[1])
                    self._send(200, st) if st else self._send(404, {"message": "Entity not found."})
                else:
                    self._send(404, {"message": "not found"})

            def do_POST(self):
                if not self._auth():
                    return
                n = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(n) or b"{}")
                _a, _b, dom, svc = self.path.strip("/").split("/")
                fake.calls.append((dom, svc, data))
                changed = []
                eid = data.get("entity_id")
                st = fake.states.get(eid)
                if st is not None:
                    if svc == "toggle":
                        st["state"] = "off" if st["state"] == "on" else "on"
                    elif svc == "turn_on":
                        st["state"] = "on"
                        if "brightness_pct" in data:
                            st["attributes"]["brightness"] = round(data["brightness_pct"] * 255 / 100)
                    elif svc == "turn_off":
                        st["state"] = "off"
                    changed.append(st)
                self._send(200, changed)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake():
    f = FakeHA()
    yield f
    f.close()


@pytest.fixture(autouse=True)
def xdg(tmp_path, monkeypatch):
    from freestream import paths
    monkeypatch.setattr(paths, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(paths, "CONFIG_FILE", tmp_path / "config" / "config.json")
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(paths, "PROFILES_DIR", tmp_path / "data" / "profiles")
    monkeypatch.setattr(paths, "RESOURCES_DIR", tmp_path / "data" / "resources")
    monkeypatch.setattr(hass, "POLL_INTERVAL", 0.2)
    monkeypatch.setattr(hass, "MIN_GAP", 0.05)
    # Tk-Reste früherer Tests hier aufräumen: im Hintergrund-Thread würde ihr __del__ ohne mainloop warten
    gc.collect()
    yield tmp_path
    for cl in list(hass._clients.values()):
        cl.close()
    hass._clients.clear()


class Ctrl:
    def __init__(self):
        self.redraws, self.overlays = [], []

    def redraw_action(self, a):
        self.redraws.append(a)

    def show_overlay(self, a, kind, seconds=1.2):
        self.overlays.append(kind)


def cfg_for(url, token=TOKEN):
    return {"homeassistant": {"url": url, "token": token}}


def make(uuid, settings, cfg, slot=("key", 0)):
    data = C.new_action(uuid)
    data["Settings"].update(settings)
    ctrl = Ctrl()
    a = A.create_action(data, None, A.Context(ctrl, cfg, None, None))
    a.slot = slot
    return a, ctrl


def wait_for(cond, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.03)
    return cond()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def center(img):
    return img.getpixel((img.width // 2, 3))[:3]


# --------------------------------------------------------------------------
def test_lookup_and_catalog():
    for t in C.CATALOG:
        if t.uuid.startswith(hasskeys.PREFIX):
            assert t.category == "Home Assistant"
            assert issubclass(native.lookup(t.uuid), hasskeys.HAAction) and A.is_builtin(t.uuid)
    assert native.lookup("com.lindeck.homeassistant.gibtsnicht") is None


def test_connection_test(fake):
    info = hass.check_connection(fake.url, TOKEN)
    assert info == {"version": "2026.10.1", "location_name": "Zuhause"}
    # Adresse ohne Schema und mit Schrägstrich am Ende
    assert hass.check_connection(fake.url.replace("http://", "") + "/", TOKEN)["version"] == "2026.10.1"


def test_auth_error(fake):
    with pytest.raises(hass.HAError) as e:
        hass.check_connection(fake.url, "falsch")
    assert e.value.kind == hass.AUTH and TOKEN not in str(e.value)
    a, ctrl = make("com.lindeck.homeassistant.toggle", {"entity_id": "light.kueche"}, cfg_for(fake.url, "falsch"))
    a.will_appear()
    assert wait_for(lambda: a.ha.error_kind == hass.AUTH)
    assert a.problem() == "Token?"
    img = a.frames((72, 72))[0][0]
    assert center(img) == hasskeys.ERR_BG


def test_offline():
    url = f"http://127.0.0.1:{free_port()}"
    with pytest.raises(hass.HAError) as e:
        hass.check_connection(url, TOKEN, timeout=2)
    assert e.value.kind == hass.OFFLINE
    a, ctrl = make("com.lindeck.homeassistant.state", {"entity_id": "sensor.temp"}, cfg_for(url))
    a.will_appear()
    assert wait_for(lambda: a.ha.error_kind == hass.OFFLINE)
    assert a.problem() == "Offline"
    assert center(a.frames((72, 72))[0][0]) == hasskeys.ERR_BG
    # Tastendruck blockiert nicht und meldet den Fehler im nächsten tick()
    t0 = time.time()
    a.key_down()
    assert time.time() - t0 < 0.2
    assert wait_for(lambda: (a.tick(), "alert" in ctrl.overlays)[1])


def test_not_configured():
    a, ctrl = make("com.lindeck.homeassistant.toggle", {"entity_id": "light.kueche"}, {})
    assert a.problem() == "Einrichten"
    a.key_down()
    assert ctrl.overlays == ["alert"]
    assert a.frames((72, 72))[0][0].size == (72, 72)


def test_toggle(fake):
    a, ctrl = make("com.lindeck.homeassistant.toggle", {"entity_id": "switch.kaffee"}, cfg_for(fake.url))
    a.will_appear()
    assert wait_for(lambda: a.st() is not None)
    a.tick()
    assert a.active() is True and a.state == 1
    assert center(a.frames((72, 72))[0][0]) == hasskeys.ON_BG
    a.key_down()
    assert a.active() is False  # sofort (vorläufig) umgeschaltet
    assert wait_for(lambda: fake.calls)
    assert fake.calls[0] == ("switch", "toggle", {"entity_id": "switch.kaffee"})
    assert wait_for(lambda: fake.states["switch.kaffee"]["state"] == "off")
    a.tick()
    assert a.active() is False and a.state == 0
    assert center(a.frames((72, 72))[0][0]) == hasskeys.OFF_BG
    # Änderung in Home Assistant kommt über die Abfrage
    fake.states["switch.kaffee"]["state"] = "on"
    assert wait_for(lambda: a.active() is True)
    assert a.tick() is True


def test_light_brightness_and_dial(fake):
    a, ctrl = make("com.lindeck.homeassistant.toggle", {"entity_id": "light.kueche", "step": 10},
                   cfg_for(fake.url), slot=("dial", 0))
    a.will_appear()
    assert wait_for(lambda: a.st() is not None)
    assert a.status_text() == "aus"
    for _i in range(3):
        a.dial_rotate(1)
    assert a.feedback()["indicator"] == 30
    assert wait_for(lambda: fake.states["light.kueche"]["state"] == "on")
    # schnelle Drehungen werden zusammengefasst; der letzte Wert zählt
    assert wait_for(lambda: fake.calls[-1] == ("light", "turn_on", {"brightness_pct": 30,
                                                                    "entity_id": "light.kueche"}))
    assert len(fake.calls) <= 3
    assert wait_for(lambda: a.status_text() == "30%")
    assert a.encoder_icon_image.size == (48, 48)


def test_state_display(fake):
    a, ctrl = make("com.lindeck.homeassistant.state", {"entity_id": "sensor.temp"}, cfg_for(fake.url))
    a.will_appear()
    assert wait_for(lambda: a.status_text() == "21.5 °C")
    assert a.display_name() == "Wohnzimmer"
    a.tick()
    fake.states["sensor.temp"]["state"] = "22.04"
    assert wait_for(lambda: a.status_text() == "22 °C")
    assert a.tick() is True
    b, _c = make("com.lindeck.homeassistant.state", {"entity_id": "sensor.temp", "decimals": 2},
                 cfg_for(fake.url))
    assert wait_for(lambda: b.status_text() == "22.04 °C")
    c, _c = make("com.lindeck.homeassistant.state", {"entity_id": "sensor.gibtsnicht"}, cfg_for(fake.url))
    c.will_appear()
    assert wait_for(lambda: c.problem() == "Unbekannt")


def test_service_and_scene(fake):
    a, ctrl = make("com.lindeck.homeassistant.service",
                   {"service": "light.turn_on", "entity_id": "light.kueche", "data": '{"brightness_pct": 50}'},
                   cfg_for(fake.url))
    a.key_down()
    assert wait_for(lambda: fake.calls)
    assert fake.calls[0] == ("light", "turn_on", {"brightness_pct": 50, "entity_id": "light.kueche"})
    assert wait_for(lambda: (a.tick(), "ok" in ctrl.overlays)[1])
    bad, ctrl2 = make("com.lindeck.homeassistant.service", {"service": "light.turn_on", "data": "{kaputt"},
                      cfg_for(fake.url))
    bad.key_down()
    assert ctrl2.overlays == ["alert"]
    s, ctrl3 = make("com.lindeck.homeassistant.scene", {"entity_id": "scene.abend"}, cfg_for(fake.url))
    s.key_down()
    assert wait_for(lambda: ("scene", "turn_on", {"entity_id": "scene.abend"}) in fake.calls)


def test_preview_needs_no_network():
    class Prev(Ctrl):
        preview = True
    data = C.new_action("com.lindeck.homeassistant.toggle")
    data["Settings"]["entity_id"] = "light.kueche"
    a = A.create_action(data, None, A.Context(Prev(), cfg_for("http://192.0.2.1:9"), None, None))
    img = a.frames((72, 72))[0][0]
    assert img.size == (72, 72)
    assert all("light.kueche" not in c._watch for c in hass._clients.values())


def test_format_state():
    st = {"state": "1013.25", "attributes": {"unit_of_measurement": "hPa", "x": 3}}
    assert hass.format_state(st) == "1013 hPa"
    assert hass.format_state(st, "x") == "3"
    assert hass.format_state({"state": "unavailable", "attributes": {}}) == "nicht verfügbar"
    assert hass.format_state({"state": "on", "attributes": {}}) == "on"


def test_config_file_private(xdg):
    from freestream.config import Config
    from freestream import paths
    cfg = Config()
    cfg.save()
    hass.save_settings(cfg, "homeassistant.local:8123/", TOKEN)
    mode = stat.S_IMODE(os.stat(paths.CONFIG_FILE).st_mode)
    assert mode == 0o600
    data = json.loads(paths.CONFIG_FILE.read_text())
    assert data["homeassistant"] == {"url": "http://homeassistant.local:8123", "token": TOKEN, "verify_ssl": True}


def test_cli(fake, xdg, capsys):
    from freestream.__main__ import main
    from freestream.config import Config
    assert main(["homeassistant", "test"]) == 1
    cfg = Config()
    hass.save_settings(cfg, fake.url, TOKEN)
    assert main(["homeassistant", "test"]) == 0
    out = capsys.readouterr().out
    assert "Zuhause" in out and "2026.10.1" in out and TOKEN not in out
    assert main(["ha", "entities", "küche"]) == 0
    out = capsys.readouterr().out
    assert "light.kueche" in out and "sensor.temp" not in out


# --------------------------------------------------------------------------
# Oberfläche
# --------------------------------------------------------------------------
@pytest.fixture
def tkroot():
    tk = pytest.importorskip("tkinter")
    if not os.environ.get("DISPLAY"):
        pytest.skip("kein Display (xvfb-run verwenden)")
    from freestream import theme
    root = tk.Tk()
    theme.apply(root)
    yield root
    root.destroy()


def pump(root, cond, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        root.update()
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def test_connection_dialog(fake, tkroot, xdg):
    from freestream import hassui, paths
    saved = []
    dlg = hassui.ConnectionDialog(tkroot, None, on_saved=lambda: saved.append(1))
    dlg.url.set(fake.url)
    dlg.token.set("falsch")
    dlg.test()
    assert pump(tkroot, lambda: dlg.result is not None)
    assert dlg.result[0] is False and "Zugriff verweigert" in dlg.status.cget("text")
    dlg.token.set(TOKEN)
    dlg.result = None
    dlg.test()
    assert pump(tkroot, lambda: dlg.result is not None)
    assert dlg.result[0] is True and "Zuhause" in dlg.status.cget("text")
    dlg.save()
    assert saved and json.loads(paths.CONFIG_FILE.read_text())["homeassistant"]["token"] == TOKEN


def test_entity_picker(fake, tkroot, xdg):
    from freestream import hassui
    from freestream.config import Config
    hass.save_settings(Config(), fake.url, TOKEN)
    picked = []
    dlg = hassui.EntityPicker(tkroot, None, ["light", "switch"], on_pick=picked.append)
    assert pump(tkroot, lambda: dlg.items)
    assert [s[0] for s in dlg.shown] == ["switch.kaffee", "light.kueche"]
    dlg.query.set("küc")
    tkroot.update()
    assert [s[0] for s in dlg.shown] == ["light.kueche"]
    dlg.choose()
    assert picked == ["light.kueche"]
