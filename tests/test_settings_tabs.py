"""Einstellungen mit Reitern wie in der Elgato-App: Allgemein, Geräte, Profile, Plugins, Stil."""
import os
import subprocess
import time
import types

import pytest

import test_editor
from streamlin import editdoc as E
from streamlin import plugins as _plugins

ORIG_INSTALLED = _plugins.installed

store, doc, app = test_editor.store, test_editor.doc, test_editor.app  # Fixtures
SHOTS = os.environ.get("STREAMLIN_SHOTS", "")
needs_x = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="braucht X (xvfb-run)")


class FakeCtrl:
    """Angeschlossenes Gerät, wie es der Dienst liefert."""

    def __init__(self, serial, model, store):
        self.serial, self.model, self.store = serial, model, store
        self.brightness, self.firmware = 70, "1.01.012"
        self.profile, self.page_index = None, 0
        self.calls = []

    def candidate_profiles(self):
        return sorted(self.store.profiles.values(), key=lambda p: p.name)

    def set_enabled(self, on):
        self.calls.append(("enabled", on))

    def set_rotation(self, deg):
        self.calls.append(("rotation", deg))

    def set_brightness(self, v):
        self.brightness = v
        self.calls.append(("brightness", v))

    def load_profile(self, pid, page_index=None):
        self.calls.append(("profile", pid))


def _pump(root, n=10):
    for _i in range(n):
        root.update()
        time.sleep(0.02)


def _shot(dlg, name):
    if not SHOTS:
        return
    _pump(dlg, 15)
    subprocess.run(["import", "-window", "root", "-crop",
                    f"{dlg.winfo_width()}x{dlg.winfo_height()}+{dlg.winfo_rootx()}+{dlg.winfo_rooty()}",
                    os.path.join(SHOTS, name + ".png")])


@pytest.fixture
def settings(app, monkeypatch, tmp_path):
    from streamlin import desktop, plugins
    from streamlin.config import Config
    from streamlin.settingsui import open_settings
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(desktop, "SYSTEM_AUTOSTART", tmp_path / "sys-autostart")
    monkeypatch.setattr(desktop, "installed_apps", lambda: [("OBS Studio", "obs"), ("Spotify", "spotify")])
    monkeypatch.setattr(plugins, "installed", lambda: {})
    ctrl = FakeCtrl("CL12K1A01234", "20GAT9901", app.store)
    app.daemon = types.SimpleNamespace(controllers={"a": ctrl}, cfg=Config(), plugin_host=None,
                                       store=app.store)
    app.device = ctrl.serial
    app.root.geometry("+0+0")
    dlg = open_settings(app.root, "LinDeck", lambda: None, lindeck=app)
    dlg.geometry("820x660+0+0")
    _pump(app.root)
    yield dlg, ctrl, app.daemon.cfg
    if dlg.winfo_exists():
        dlg.destroy()
    app.daemon = None


@needs_x
def test_general_tab_and_autostart(settings, tmp_path):
    from streamlin import desktop
    dlg, _ctrl, _cfg = settings
    assert dlg.tabs.value == "general" and [k for k, _t in dlg.tabs.items] == list(dlg.TABS)
    _shot(dlg, "settings-general")
    assert desktop.autostart_enabled("lindeck") is False  # keine Systemdatei im Test
    dlg.autostart.set(True)
    dlg._toggle_autostart()
    f = tmp_path / "xdg" / "autostart" / "lindeck.desktop"
    assert "Exec=lindeck run" in f.read_text() and desktop.autostart_enabled("lindeck")
    dlg.autostart.set(False)
    dlg._toggle_autostart()
    assert "Hidden=true" in f.read_text() and not desktop.autostart_enabled("lindeck")


@needs_x
def test_devices_tab(settings):
    dlg, ctrl, cfg = settings
    dlg.show_tab("devices")
    page = dlg.pages["devices"]
    _shot(dlg, "settings-devices")
    page.name.set("Studio-Deck")
    assert cfg.device(ctrl.serial)["name"] == "Studio-Deck"
    assert page.lindeck.device_cards[ctrl.serial].title == "Studio-Deck"  # Seitenleiste
    page.rotation.current(1)
    page._set_rotation()
    page.enabled.set(False)
    page._set_enabled()
    page.sleep.current(2)
    page._set_sleep()
    page._set_brightness("40")
    time.sleep(0.1)
    assert ("enabled", False) in ctrl.calls and ctrl.brightness == 40
    d = cfg.device(ctrl.serial)
    assert d["rotation"] == 180 and d["enabled"] is False and d["sleep_after_minutes"] == 5 and d["brightness"] == 40


@needs_x
def test_profiles_tab(settings):
    dlg, _ctrl, cfg = settings
    dlg.show_tab("profiles")
    page = dlg.pages["profiles"]
    pid = page.selected
    page.program.set("Spotify")
    page._set_program()
    assert cfg.get("smart_profiles") == {"spotify": pid}
    page.default.set(True)
    page._set_default()
    assert cfg.get("default_profile") == pid
    _shot(dlg, "settings-profiles")
    page.program.set(page.program.cget("values")[0])  # „Keins“
    page._set_program()
    assert cfg.get("smart_profiles") == {}


@needs_x
def test_plugins_and_style_tabs(settings, app):
    from streamlin import catalog
    dlg, _ctrl, cfg = settings
    dlg.show_tab("plugins")
    _shot(dlg, "settings-plugins")
    assert dlg.pages["plugins"].rows == {}
    dlg.show_tab("style")
    page = dlg.pages["style"]
    page.align.set("top")
    page._set("TitleAlignment", "top")
    page.toggles["bold"].set(True)
    page._toggle_style("bold")
    page.size.set("16")
    assert cfg.get("title_style")["FontStyle"] == "Bold" and cfg.get("title_style")["FontSize"] == 16
    assert catalog.default_state()["TitleAlignment"] == "top"  # neue Tasten
    page.progress.set(False)
    page._set_progress()
    assert cfg.get("multi_progress") is False
    _shot(dlg, "settings-style")
    from streamlin import msgbox
    msgbox_calls = []
    import pytest as _p
    mp = _p.MonkeyPatch()
    mp.setattr(msgbox, "askyesno", lambda *a, **k: True)
    mp.setattr(msgbox, "showinfo", lambda *a, **k: msgbox_calls.append(a))
    try:
        page.apply_all()
    finally:
        mp.undo()
    st = app.doc.get(app.page_uuid, ("key", 0, 0))["States"][0]
    assert st["TitleAlignment"] == "top" and st["FontStyle"] == "Bold" and st["FontSize"] == 16
    page.reset()
    assert catalog.default_state()["TitleAlignment"] == "bottom"


def test_apply_title_style_counts(store, doc):
    page = doc.page_ids[0]
    doc.place(page, ("key", 0, 0), "com.elgato.streamdeck.system.hotkey")
    doc.place(page, ("dial", 0), "com.elgato.streamdeck.system.hotkey")
    doc.save()
    n = E.apply_title_style(store, {"TitleColor": "#ff0000", "Bogus": 1}, {"TitleColor": "#00ff00"})
    assert n == 2
    d = E.ProfileDoc(doc.root)
    assert d.get(page, ("key", 0, 0))["States"][0]["TitleColor"] == "#ff0000"
    assert "Bogus" not in d.get(page, ("key", 0, 0))["States"][0]
    assert d.get(page, ("dial", 0))["States"][0]["TitleColor"] == "#00ff00"
    assert E.apply_title_style(store, {"TitleColor": "#ff0000"}, {"TitleColor": "#00ff00"}) == 0


def test_progress_overlay():
    from PIL import Image
    from streamlin import render
    half = render.compose_key((72, 72), None, "", {}, overlay=("progress", 0.5, 2, 4))
    y = int(72 * 0.88) + 1
    assert half.getpixel((14, y))[2] > 200 and half.getpixel((58, y))[2] < 120  # links gefüllt, rechts nicht
    assert isinstance(half, Image.Image)
    assert render.compose_key((72, 72), None, "", {}, overlay=("progress", 0.5)).size == (72, 72)  # alte Form


def test_engine_rotation_enabled_sleep_default(tmp_path, monkeypatch):
    pytest.importorskip("StreamDeck")
    from StreamDeck.DeviceManager import DeviceManager

    from streamlin.config import Config, State
    from streamlin.engine import DeckController
    from streamlin.profiles import ProfileStore
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    store_dir = tmp_path / "profiles"
    store_dir.mkdir()
    E.create_profile("A", "20GBD9901", store_dir)  # alphabetisch zuerst – ohne Standardprofil wäre es A
    b = E.ProfileDoc(E.create_profile("B", "20GBD9901", store_dir))
    b.place(b.page_ids[0], ("key", 0, 0), "com.elgato.streamdeck.system.hotkey")
    b.save()
    deck = next(d for d in DeviceManager(transport="dummy").enumerate() if d.deck_type() == "Stream Deck +")
    deck.open()
    cfg = Config()
    serial = deck.get_serial_number() or deck.id()
    cfg.device(serial).update({"rotation": 180, "sleep_after_minutes": 1})
    cfg.data["default_profile"] = b.id
    st = State()
    st.save = lambda: None
    ctrl = DeckController(deck, ProfileStore(store_dir), cfg, st, None, None)
    assert ctrl._initial_profile_id() == b.id  # Standardprofil
    ctrl.load_profile(b.id, 0)
    shown = []
    monkeypatch.setattr(deck, "set_key_image", lambda k, img: shown.append(k))
    ctrl.render_key(0)
    assert shown == [ctrl.key_count - 1]  # gedreht: Taste 0 liegt unten rechts
    ctrl._run = lambda fn, *args: None  # nur die Zuordnung prüfen, nichts ausführen
    ctrl._handle(("key", ctrl.key_count - 1, True))
    assert 0 in ctrl.key_down_at
    ctrl._handle(("key", ctrl.key_count - 1, False))
    assert ctrl._physical(("dial", 0, "turn", 1))[1] == ctrl.dial_count - 1
    ctrl.set_rotation(0)
    shown.clear()
    ctrl.render_key(0)
    assert shown == [0]
    # aus: keine Reaktion, nichts gezeichnet
    ctrl.set_enabled(False)
    shown.clear()
    ctrl.render_all()
    ctrl._handle(("key", 1, True))
    assert shown == [] and 1 not in ctrl.key_down_at
    ctrl.set_enabled(True)
    # Ruhezustand je Gerät (1 Minute)
    ctrl.last_input -= 61
    ctrl._second_tick(time.time())
    assert ctrl.sleeping
    ctrl.stop()


@needs_x
def test_plugins_tab_with_plugin(settings, monkeypatch, tmp_path):
    import json
    from streamlin import msgbox, paths, plugins
    root = tmp_path / "plugins"
    d = root / "com.test.demo.sdPlugin"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({
        "UUID": "com.test.demo", "Name": "Demo", "Version": "1.4.0", "Author": "Test",
        "Description": "Steuert Demo-Dinge direkt vom Deck.", "CodePath": "bin/plugin.js",
        "Actions": [{"UUID": "com.test.demo.act", "Name": "Aktion", "States": [{}]}]}))
    monkeypatch.setattr(paths, "PLUGINS_DIR", root)
    monkeypatch.setattr(plugins, "installed", ORIG_INSTALLED)  # echte Suche im Testordner
    dlg, _ctrl, _cfg = settings
    dlg.show_tab("plugins")
    page = dlg.pages["plugins"]
    assert list(page.rows) == ["com.test.demo"] and page.selected == "com.test.demo"
    _shot(dlg, "settings-plugins")
    monkeypatch.setattr(msgbox, "askyesno", lambda *a, **k: True)
    page._remove()
    assert not d.exists() and page.rows == {}
