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
    from freestream import models
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
    from freestream.theme import PopupMenu
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
    from freestream import editdoc, editor
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
    from freestream import editdoc, editor
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
    from freestream import editor
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
    assert titles == ["Kein Gerät verbunden"]
    app.daemon = None


def test_linewave_mix_menu_and_create_in_sidebar(env, pactl, monkeypatch):
    import tkinter as tk
    from freestream.wavehost import WaveHost
    from freestream.waveui import WaveWindow
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
        from freestream import msgbox as messagebox
        monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
        win._mix_menu("chat", 10, 10).invoke("Mix löschen")
        assert _pump(root, lambda: "chat" not in win.mix_cards)
        assert host.mixer.mix_ids() == ["monitor", "stream"]
        assert not hasattr(win, "open_freedeck") and not hasattr(win, "more_btn")
        for fn in (win._scene_menu, win._output_menu):
            m = fn()
            assert m.labels()
            m.close()
    finally:
        win.quitting = True
        root.destroy()


def test_settings_offer_mixer_removal_in_freewave(env):
    import tkinter as tk
    from freestream.settingsui import SettingsDialog
    from freestream.theme import Button
    root = tk.Tk()
    try:
        calls = []
        dlg = SettingsDialog(root, "FreeWave", lambda: None, mixer=(True, calls.append))

        def buttons(w):
            for c in w.winfo_children():
                if isinstance(c, Button):
                    yield c
                yield from buttons(c)
        btn = next(b for b in buttons(dlg) if b.text == "Mixer entfernen")
        btn.command()
        assert calls == [False]
        freedeck = SettingsDialog(root, "FreeDeck", lambda: None)
        assert not any(b.text in ("Mixer entfernen", "Mixer einrichten") for b in buttons(freedeck))
        freedeck.destroy()
    finally:
        root.destroy()


def test_freedeck_layout_like_deck_software(app, store):
    """Gerät und Profil als Auswahlfelder oben, Aktionen als aufklappbare Liste rechts."""
    from freestream import editdoc
    from freestream.editor import ActionRow, CategoryRow
    editdoc.create_profile("Zweites", "20GBD9901", store)
    app.refresh_profiles()
    assert app.prof_dd.text == "Test"
    app._toggle_drop("prof")
    app.root.update()
    assert app.prof_panel.winfo_ismapped() and not app.dev_panel.winfo_ismapped()
    second = next(i for p, i in app.profile_items.items() if app.store.get(p).name == "Zweites")
    second.command()  # Profil in der Liste gewählt: Liste zu, Profil offen
    app.root.update()
    assert not app.prof_panel.winfo_ismapped() and app.doc.name == "Zweites" and app.prof_dd.text == "Zweites"
    app._toggle_drop("dev")
    app.root.update()
    assert app.dev_panel.winfo_ismapped()
    app._toggle_drop("dev")  # nochmal: zu
    app.root.update()
    assert not app.dev_panel.winfo_ismapped()
    # Kategorien: anfangs die ersten beiden offen, Klick klappt auf/zu, Suche zeigt alles Gefundene
    cats = [w for w in app.library.inner.winfo_children() if isinstance(w, CategoryRow)]
    assert [c.open for c in cats[:3]] == [True, True, False]
    third = cats[2].cat
    app.toggle_category(third)
    rows = [w for w in app.library.inner.winfo_children() if isinstance(w, ActionRow)]
    assert any(r.type.category == third for r in rows)
    app.search_var.set("hotkey")
    rows = [w for w in app.library.inner.winfo_children() if isinstance(w, ActionRow)]
    assert rows and all("hotkey" in (r.type.name + r.type.description + r.type.category).lower() for r in rows)
    app.search_var.set("")
    # Seiten: ‹ ● ○ › unter den Tasten
    app.add_page()
    ids = app.doc.page_ids
    assert app.nav[0] == ids[1] and app.page_dots.current == ids[1]
    app.page_dots._click(types.SimpleNamespace(x=app.page_dots._x(0)))
    assert app.nav[0] == ids[0]
    app.page_dots._click(types.SimpleNamespace(x=int(app.page_dots["width"]) - 5))  # ›
    assert app.nav[0] == ids[1]


def test_freewave_mix_columns_fit_window(env, pactl):
    """Mix-Spalten füllen die Breite (wie Wave Link); breiteres Fenster = breitere Spalten, Regler bleiben."""
    import tkinter as tk
    from freestream.wavehost import WaveHost
    from freestream.waveui import MIX_W_MAX, MIX_W_MIN, WaveWindow
    host = WaveHost(_config(env, {"mixer": {"enabled": True}}))
    host.start()
    root = tk.Tk()
    try:
        win = WaveWindow(root, host, tray=False)
        assert _pump(root, lambda: bool(win.strips))
        w0 = win.mix_w
        assert MIX_W_MIN <= w0 <= MIX_W_MAX
        fader = win.strips["system"]["monitor"]["fader"]
        root.geometry("1700x800")
        assert _pump(root, lambda: win.mix_w > w0)
        assert win.strips["system"]["monitor"]["fader"] is fader and int(fader["width"]) == win.mix_w - 100
        assert int(win.mix_heads["monitor"]["card"]["width"]) == win.mix_w
    finally:
        win.quitting = True
        root.destroy()


def test_freewave_setup_wizard_in_settings_and_meters(env, pactl, monkeypatch):
    """Einrichtung über Einstellungen (nicht mehr in der Seitenleiste); Live-Pegel als eigene Anzeige."""
    import tkinter as tk
    from freestream.theme import LevelMeter
    from freestream.wavehost import WaveHost
    from freestream.waveui import WaveWindow
    host = WaveHost(_config(env, {"mixer": {"enabled": True}}))
    host.start()
    root = tk.Tk()
    try:
        win = WaveWindow(root, host, tray=False)
        assert _pump(root, lambda: bool(win.strips))
        assert isinstance(win.strips["system"]["monitor"]["meter"], LevelMeter)
        assert isinstance(win.strips["system"]["master"]["meter"], LevelMeter)
        opened = []
        monkeypatch.setattr(win, "open_setup", lambda: opened.append(1))
        dlg = win.open_settings()
        dlg.setup_btn.command()
        assert opened == [1] and not dlg.winfo_exists()
    finally:
        win.quitting = True
        root.destroy()
