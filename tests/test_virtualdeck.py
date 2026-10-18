"""Virtuelles Deck: Gerät ohne Hardware, Fenster/Randleiste und Einstellungen › Geräte."""
import os
import signal
import time
import types

import pytest

import test_editor
from freestream import editdoc as E
from freestream import virtualdeck as V
import test_settings_tabs
from test_settings_tabs import _pump, _shot

store, doc, app = test_editor.store, test_editor.doc, test_editor.app  # Fixtures
settings = test_settings_tabs.settings
needs_x = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="braucht X (xvfb-run)")
HOTKEY = "com.elgato.streamdeck.system.hotkey"


def _until(root, cond, secs=5.0):
    """Tk laufen lassen, bis cond() stimmt (der Takt des Fensters läuft alle 40–300 ms)."""
    end = time.time() + secs
    while not cond() and time.time() < end:
        root.update()
        time.sleep(0.02)
    return cond()


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    from freestream import paths
    from freestream.config import Config, State
    from freestream.daemon import Daemon
    from freestream.profiles import ProfileStore
    monkeypatch.setattr(paths, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(paths, "STATE_FILE", tmp_path / "state.json")
    root = tmp_path / "profiles"
    root.mkdir()
    d = Daemon.__new__(Daemon)
    d.cfg, d.state, d.store = Config(), State(), ProfileStore(root)
    d.state.save = lambda: None
    d.vin = d.sound = d.plugin_host = None
    d.transport, d.controllers, d.running = None, {}, True
    d._last_window, d._smart_prev = "", {}
    d._manager = lambda: types.SimpleNamespace(enumerate=lambda: [])  # keine Hardware
    yield d
    for c in list(d.controllers.values()):
        c.detach()


def test_virtual_models():
    from freestream import models as M
    assert M.model_name("FDV4X2") == "Virtuelles Deck 4 × 2" and M.model_name("FDV2X4") == "Virtuelles Deck 2 × 4"
    assert V.device_model({"model": "FDV8X4", "mode": "edge"}) == "FDV4X8"
    assert M.model_geometry("FDV8X4") == M.Geometry(8, 4) and M.is_virtual("FDV3X2")
    assert not M.is_virtual("20GBA9901") and M.elgato_name("FDV5X3") == "Virtual Deck"
    assert "Stream Deck" not in M.model_name("FDV5X3")
    s = V.settings(types.SimpleNamespace(get=lambda k: {"model": "kaputt", "mode": "x", "edge": "oben"}))
    assert (s["model"], s["mode"], s["edge"], s["enabled"]) == ("FDV4X2", "window", "right", False)


def test_daemon_adds_resizes_and_removes_virtual_deck(daemon):
    daemon.scan()
    assert not daemon.controllers  # aus: kein Gerät
    V.update(daemon.cfg, enabled=True)
    daemon.scan()
    ctrl = daemon.virtual_controller
    assert ctrl is not None and ctrl.virtual and ctrl.serial == "VIRTUAL" and ctrl.model == "FDV4X2"
    # ohne passendes Profil legt FreeDeck eins an
    assert ctrl.profile and ctrl.profile.name == "Virtuelles Deck 4 × 2"
    assert len(ctrl.deck.images) == 8 and ctrl.deck.seq > 0  # Bilder für das Fenster, nicht per USB
    # Klick im Fenster = Tastendruck am Gerät
    got = []
    ctrl.on_key = lambda k, pressed: got.append((k, pressed))
    ctrl.deck.press(3, True)
    ctrl.deck.press(3, False)
    for _i in range(50):
        if len(got) == 2:
            break
        time.sleep(0.02)
    assert got == [(3, True), (3, False)]
    # Größe ändern: neues Gerät, altes beendet
    V.update(daemon.cfg, model="FDV5X3")
    daemon.sync_virtual()
    new = daemon.virtual_controller
    assert new is not ctrl and new.geometry.cols == 5 and not ctrl.running
    assert list(daemon.controllers) == ["virtual:FDV5X3"]
    # das leere, automatisch angelegte Profil zieht mit um – kein zweites Profil
    assert new.profile.name == "Virtuelles Deck 5 × 3" and len(daemon.store.profiles) == 1
    assert new.profile.id == ctrl.profile.id
    # belegt: bleibt bei seiner Größe, die Randleiste (hochkant 3 × 5) bekommt ein eigenes
    doc = E.ProfileDoc(new.profile.root)
    doc.place(doc.page_ids[0], ("key", 0, 0), HOTKEY)
    doc.save()
    daemon.store.reload()
    V.update(daemon.cfg, mode="edge")
    daemon.sync_virtual()
    edge = daemon.virtual_controller
    assert edge.model == "FDV3X5" and (edge.geometry.cols, edge.geometry.rows) == (3, 5)
    assert edge.profile.name == "Virtuelles Deck 3 × 5" and len(daemon.store.profiles) == 2
    new = edge
    V.update(daemon.cfg, enabled=False)
    daemon.scan()
    assert not daemon.controllers and not new.running


def test_virtual_deck_uses_matching_profiles(daemon):
    """Gleiches Raster wie ein echtes Modell: dessen Profile passen auch (hier Original 5×3)."""
    E.create_profile("Studio", "20GBA9901", daemon.store.root)
    daemon.store.reload()
    V.update(daemon.cfg, enabled=True, model="FDV5X3")
    daemon.scan()
    ctrl = daemon.virtual_controller
    assert ctrl.profile.name == "Studio" and len(daemon.store.profiles) == 1


@needs_x
def test_window_and_edge_bar(daemon, tmp_path):
    import tkinter as tk

    from freestream.virtualui import VirtualDeckWindow
    E.create_profile("Neo", "20GBJ9901", daemon.store.root)  # 4×2 wie das virtuelle Deck
    doc = E.ProfileDoc(daemon.store.root / next(p.name for p in daemon.store.root.iterdir()))
    doc.place(doc.page_ids[0], ("key", 0, 0), HOTKEY)
    doc.update_state(doc.page_ids[0], ("key", 0, 0), 0, "Title", "Copy")
    doc.add_page()
    doc.save()
    daemon.store.reload()
    V.update(daemon.cfg, enabled=True)
    daemon.scan()
    ctrl = daemon.virtual_controller
    root = tk.Tk()
    try:
        w = VirtualDeckWindow(root, daemon)
        assert _until(root, lambda: w.win is not None and w.win.winfo_ismapped() and w._seq >= 0)
        assert w.win.overrideredirect()
        assert (w.cols, w.rows) == (4, 2) and len([h for h in w._hits if h[4][0] == "key"]) == 8
        # Taste klicken
        got = []
        ctrl.on_key = lambda k, pressed: got.append((k, pressed))
        x, y = w._key_xy(1)
        w._press(types.SimpleNamespace(x=x + 5, y=y + 5, x_root=0, y_root=0))
        assert w._down == 1
        w._release(types.SimpleNamespace(x=x + 5, y=y + 5))
        time.sleep(0.2)
        assert got == [(1, True), (1, False)]
        # Seiten: Punkte unten
        assert ctrl.page_count == 2
        hit = next(h for h in w._hits if h[4] == ("goto", 1))
        w._press(types.SimpleNamespace(x=hit[0] + 3, y=hit[1] + 3, x_root=0, y_root=0))
        for _i in range(50):
            if ctrl.page_index == 1:
                break
            time.sleep(0.02)
        assert ctrl.page_index == 1
        # Ausblenden (Kürzel / ×) und wieder einblenden
        w.request_toggle()
        assert _until(root, lambda: not w.win.winfo_ismapped())
        assert V.settings(daemon.cfg)["visible"] is False
        w.toggle()
        assert _until(root, lambda: w.win.winfo_ismapped())
        # Randleiste rechts: hochkant (eigenes Gerät 2 × 4, im Editor genauso), klappt ein und wieder aus
        V.update(daemon.cfg, mode="edge")
        daemon.sync_virtual()  # wie Einstellungen › Geräte
        assert daemon.virtual_controller.model == "FDV2X4"
        assert _until(root, lambda: (w.cols, w.rows) == (2, 4) and w.win.winfo_ismapped() and w.win.winfo_x() > 0)
        assert w.win.winfo_x() + w.win.winfo_width() >= root.winfo_screenwidth() - 2
        sw = root.winfo_screenwidth()
        from freestream.virtualui import GRIP
        # Klick auf den Griff (innen, links): gleitet hinaus, nur der Griff bleibt sichtbar
        gx, gy = GRIP // 2, w.height // 2
        assert w._in_grip(gx, gy) and not w._in_grip(w.width - 5, gy)
        assert not w._in_grip(gx, 8) and not w._in_grip(gx, w.height - 8)  # nur der Griff, nicht die ganze Kante
        w._hover(types.SimpleNamespace(x=gx, y=gy))
        assert w.grip_hover
        w._hover(types.SimpleNamespace(x=gx, y=10))
        assert not w.grip_hover
        w._hover(types.SimpleNamespace(x=gx, y=gy))
        w._press(types.SimpleNamespace(x=gx, y=12, x_root=0, y_root=0))  # Rand neben dem Griff: nichts
        assert not w.collapsed
        steps = []
        w.win.geometry = (lambda orig: lambda g=None: (steps.append(g), orig(g))[1] if g else orig())(w.win.geometry)
        w._press(types.SimpleNamespace(x=gx, y=gy, x_root=0, y_root=0))
        assert w.collapsed and _until(root, lambda: w.win.winfo_x() == sw - GRIP)
        # die Maus steht nicht über dem Griff (X meldet beim Weggleiten kein <Leave>): nicht mehr hell
        assert _until(root, lambda: not w.grip_hover)
        xs = [int(g.split("+")[1]) for g in steps if g and g.startswith("+")]
        assert len(xs) >= 10 and xs == sorted(xs)  # gleitet in vielen kleinen Schritten, nur nach außen
        assert xs[1] - xs[0] > xs[-1] - xs[-2]      # schnell los, langsam ankommen
        # Tasten sind eingeklappt nicht erreichbar, der Griff schon: wieder herein
        w._press(types.SimpleNamespace(x=gx, y=gy, x_root=0, y_root=0))
        assert not w.collapsed and _until(root, lambda: w.win.winfo_x() == sw - w.width)
        # ausgeschaltet: Fenster weg
        V.update(daemon.cfg, enabled=False)
        daemon.scan()
        assert _until(root, lambda: w.win is None)
        w.close()
    finally:
        root.destroy()


@pytest.fixture
def own_config(tmp_path, monkeypatch):
    from freestream import paths
    monkeypatch.setattr(paths, "CONFIG_FILE", tmp_path / "config.json")


@needs_x
def test_settings_virtual_form(own_config, settings, monkeypatch):
    from freestream import shortcuts
    dlg, ctrl, cfg = settings
    calls = []
    dlg.show_tab("devices")
    page = dlg.pages["devices"]
    page.freedeck.daemon.sync_virtual = lambda: calls.append("sync")
    assert [c.serial for c in page.controllers()] == [ctrl.serial]
    page.select(V.SERIAL)
    assert page.vcard.selected and page.vcard.subtitle == "Aus"
    assert not page.v_modes["window"].enabled and str(page.v_edge.cget("state")) == "disabled"
    page.v_on.set(True)
    page._v_set(enabled=True, visible=True)
    assert calls == ["sync"] and V.settings(cfg)["enabled"] and page.vcard.subtitle == "Ein · Fenster"
    assert page.v_modes["window"].selected and str(page.v_edge.cget("state")) == "disabled"
    _shot(dlg, "settings-virtual")
    assert page.v_size.cget("values")[1] == "4 × 2 Tasten"
    page.v_modes["edge"].command()
    assert V.settings(cfg)["mode"] == "edge" and page.v_modes["edge"].selected
    assert page.v_size.cget("values")[1] == "2 × 4 Tasten" and calls == ["sync", "sync"]  # hochkant
    assert str(page.v_edge.cget("state")) == "readonly" and not page.v_top.enabled
    page.v_size.current(3)
    page._v_set(model=page.v_codes[3])
    assert V.settings(cfg)["model"] == "FDV8X4" and calls == ["sync", "sync", "sync"]
    # Tastenkürzel beim Desktop eintragen
    reg = []
    monkeypatch.setattr(shortcuts, "register", lambda *a, **k: reg.append(a) or {})
    page._v_shortcut("Super+D")
    for _i in range(30):
        if reg:
            break
        _pump(dlg, 1)
    assert V.settings(cfg)["shortcut"] == "Super+D"
    assert reg[0][0] == V.SHORTCUT_ID and reg[0][2].endswith("virtual-deck") and reg[0][3] == "Super+D"


def test_cli_toggles_running_app(monkeypatch):
    from freestream import __main__ as M
    from freestream import app
    sent = []
    monkeypatch.setattr(app, "running_instance", lambda name=app.APP_FILE: 4321)
    monkeypatch.setattr(app, "_app_info", lambda name=app.APP_FILE: {"pid": 4321, "gui": True})
    monkeypatch.setattr(M.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    assert M.main(["virtual-deck"]) == 0 and sent == [(4321, signal.SIGUSR2)]
    monkeypatch.setattr(app, "_app_info", lambda name=app.APP_FILE: {"pid": 4321, "gui": False})
    assert M.main(["virtual-deck", "toggle"]) == 1
