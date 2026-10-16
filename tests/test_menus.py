"""Menüs im App-Design (statt grauer Systemmenüs), Rechtsklick auf Profile und Mixe,
Stream-Deck-Modell automatisch erkannt."""
import os
import types

import pytest

import test_editor
import test_mixes
from test_waveapp import _config, _pump

store, doc, app = test_editor.store, test_editor.doc, test_editor.app  # Fixtures
env, pactl = test_mixes.env, test_mixes.pactl

pytestmark = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="braucht X (xvfb-run)")


@pytest.fixture
def usb(tmp_path, monkeypatch):
    """Simulierte USB-Geräte (sysfs); ohne Aufruf von plug() ist nichts angeschlossen."""
    from streamlin import models
    root = tmp_path / "usb"
    root.mkdir()
    monkeypatch.setattr(models, "USB_ROOT", str(root))

    def plug(pid, name="1-1"):
        d = root / name
        d.mkdir()
        (d / "idVendor").write_text("0fd9\n")
        (d / "idProduct").write_text(f"{pid:04x}\n")
    return plug


def test_popup_menu():
    import tkinter as tk
    from streamlin.theme import PopupMenu
    root = tk.Tk()
    try:
        done = []
        m = PopupMenu(root)
        m.add("Eins", lambda: done.append(1), icon="edit", hint="Strg+1")
        m.separator()
        m.separator()  # doppelte Trenner fallen weg
        m.add("Aus")  # ohne Befehl: ausgegraut
        m.add("Löschen", lambda: done.append(2), icon="trash", danger=True)
        m.separator()
        assert m.labels() == ["Eins", "Aus", "Löschen"]
        m.popup(10, 10)
        root.update()
        assert m.win is not None and m.entries[-1] is not None  # Trenner am Ende entfernt
        m.invoke("Aus")
        assert done == [] and m.win is not None
        m.invoke("Löschen")
        assert done == [2] and m.win is None and PopupMenu.current is None
        # ein neues Menü schließt das alte
        a, b = PopupMenu(root).add("a", lambda: None), PopupMenu(root).add("b", lambda: None)
        a.popup(0, 0)
        b.popup(0, 0)
        assert a.win is None and b.win is not None
        b.close()
    finally:
        root.destroy()


def test_lindeck_more_menu_and_profile_context(app, store, usb, monkeypatch):
    from streamlin import editdoc, editor
    assert not hasattr(app, "more_btn") and not hasattr(app, "_more_menu")  # kein „…“-Menü mehr
    other = editdoc.create_profile("Gaming", "20GAT9901", store)
    app.refresh_profiles()
    pid = other.name[:-len(".sdProfile")]
    menu = app._profile_context(pid, 20, 20)
    assert menu.labels() == ["Öffnen", "Umbenennen…", "Duplizieren", "Exportieren…", "Profil löschen"]
    monkeypatch.setattr(editor.simpledialog, "askstring", lambda *a, **k: "Gaming Neu")
    menu.invoke("Umbenennen…")
    assert app.doc.id == pid and app.doc.name == "Gaming Neu"
    app._profile_context(pid, 20, 20).invoke("Duplizieren")
    names = sorted(p.name for p in app.store.profiles.values())
    assert names == ["Gaming Neu", "Gaming Neu", "Test"] or "Gaming Neu" in names
    count = len(app.store.profiles)
    app._profile_context(app.doc.id, 20, 20).invoke("Profil löschen")
    app.store.reload()
    assert len(app.store.profiles) == count - 1


def test_lindeck_model_detected_automatically(app, store, usb):
    from streamlin import editdoc, editor
    # nichts angeschlossen: Modell des offenen Profils (Stream Deck +)
    assert app.auto_model() == ("20GBD9901", False)
    usb(0x006C)  # Stream Deck XL angesteckt
    assert app.auto_model() == ("20GAT9901", True)
    dlg = editor.NewProfileDialog(app.root, "20GAT9901", True, wait=False)
    dlg.name.set("Streaming XL")
    dlg.ok()
    assert dlg.result == "Streaming XL"
    # Profile anderer Geräte: ausgegraut, mit Modellname statt Seitenzahl
    editdoc.create_profile("Mini", "20GAI9901", store)
    app.refresh_profiles()
    mini = next(i for p, i in app.profile_items.items() if app.store.get(p).name == "Mini")
    test = next(i for p, i in app.profile_items.items() if app.store.get(p).name == "Test")
    assert mini.dim and mini.count == "Mini"
    assert test.dim and test.active is True  # offenes Profil bleibt hervorgehoben


def test_lindeck_new_profile_uses_connected_model(app, store, usb, monkeypatch):
    from streamlin import editor
    usb(0x0084)  # Stream Deck +

    class Dlg:
        def __init__(self, root, model, plugged, wait=True):
            Dlg.args = (model, plugged)
            self.result = "Neu"
    monkeypatch.setattr(editor, "NewProfileDialog", Dlg)
    app.new_profile()
    assert Dlg.args == ("20GBD9901", True)
    assert app.doc.name == "Neu" and app.doc.model == "20GBD9901"


def test_lindeck_devices_only_connected(app):
    app.daemon = types.SimpleNamespace(controllers={}, cfg={"devices": {"OLD": {"profile": ""}}})
    app._device_key = None
    app.build_devices()
    titles = [c.title for c in app.devbox.winfo_children()]
    assert titles == ["Kein Stream Deck verbunden"]
    app.daemon = None


def test_linewave_mix_menu_and_create_in_sidebar(env, pactl, monkeypatch):
    import tkinter as tk
    from streamlin.wavehost import WaveHost
    from streamlin.waveui import WaveWindow
    cfg = _config(env, {"mixer": {"enabled": True, "mixes": [{"id": "chat", "name": "Chat", "virtual": True}]}})
    host = WaveHost(cfg)
    host.start()
    root = tk.Tk()
    try:
        win = WaveWindow(root, host, tray=False)
        assert _pump(root, lambda: "chat" in win.mix_cards)
        assert not hasattr(win, "addmix_btn") and win.addmix_item.text == "Mix anlegen"
        menu = win._mix_menu("stream", 10, 10)
        assert menu.labels() == ["Bearbeiten …", "Abhören", "Mix löschen"]
        assert not next(e for e in menu.entries if e and e["label"] == "Mix löschen")["enabled"]  # fester Mix
        menu.close()
        from tkinter import messagebox
        monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
        win._mix_menu("chat", 10, 10).invoke("Mix löschen")
        assert _pump(root, lambda: "chat" not in win.mix_cards)
        assert host.mixer.mix_ids() == ["monitor", "stream"]
        assert not hasattr(win, "open_lindeck") and not hasattr(win, "more_btn")
        for fn in (win._scene_menu, win._output_menu):
            m = fn()
            assert m.labels()
            m.close()
    finally:
        win.quitting = True
        root.destroy()


def test_settings_offer_mixer_removal_in_linewave(env):
    import tkinter as tk
    from streamlin.settingsui import SettingsDialog
    from streamlin.theme import Button
    root = tk.Tk()
    try:
        calls = []
        dlg = SettingsDialog(root, "Linewave", lambda: None, mixer=(True, calls.append))

        def buttons(w):
            for c in w.winfo_children():
                if isinstance(c, Button):
                    yield c
                yield from buttons(c)
        btn = next(b for b in buttons(dlg) if b.text == "Mixer entfernen")
        btn.command()
        assert calls == [False]
        lindeck = SettingsDialog(root, "LinDeck", lambda: None)
        assert not any(b.text in ("Mixer entfernen", "Mixer einrichten") for b in buttons(lindeck))
        lindeck.destroy()
    finally:
        root.destroy()
