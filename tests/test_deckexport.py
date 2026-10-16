"""Linewave-Kanäle als Profil „Linewave“ auf das Stream Deck; Lautstärke max. 100 %; Kopfzeile ohne Flackern."""
import json

import pytest

from streamlin import deckexport, editdoc, paths
from streamlin.models import Geometry

CHANS = ["system", "music", "browser", "voice", "game", "sfx"]
PLUS, MK2, MINI, PEDAL = "20GBD9901", "20GBA9901", "20GAI9901", "20GBF9901"


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    root.mkdir()
    monkeypatch.setattr(paths, "PROFILES_DIR", root)
    monkeypatch.setattr(paths, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(paths, "PID_FILE", tmp_path / "streamlin.pid")  # kein echtes LinDeck anstoßen
    from streamlin import models
    monkeypatch.setattr(models, "USB_ROOT", str(tmp_path / "usb"))  # keine echten Geräte
    return root


def _plug(tmp_path, *pids):
    """Simulierte USB-Geräte im sysfs (Vendor Elgato)."""
    for i, pid in enumerate(pids):
        d = tmp_path / "usb" / f"1-{i + 1}"
        d.mkdir(parents=True)
        (d / "idVendor").write_text("0fd9\n")
        (d / "idProduct").write_text(f"{pid:04x}\n")
        (d / "serial").write_text(f"SER{i}\n")


def _profile(store, name):
    for d in store.iterdir():
        m = json.loads((d / "manifest.json").read_text())
        if m["Name"] == name:
            return d
    raise AssertionError(name)


def _pages(root):
    """{Seitenname: {slot: (uuid, kanal, aktion, mix, schritt)}} in Seitenreihenfolge."""
    doc = editdoc.ProfileDoc(root)
    out = []
    for page in doc.page_ids:
        pg = doc.page(page)
        items = {}
        for slot, a in pg.items():
            st = a.get("Settings") or {}
            items[slot] = (a["UUID"], st.get("channel"), st.get("action"), st.get("mix"), st.get("step"))
        out.append((pg.name, items))
    return out


def test_plan_layouts():
    # Stream Deck XL: oben Mix-Wahl und „Kanäle“, je Kanal Kachel + Fader über zwei Tasten
    xl = deckexport.plan(Geometry(8, 4), CHANS)
    assert len(xl) == 1
    assert [(s, k) for s, c, k in xl[0] if c is None] == [
        (("key", 0, 0), "mix:both"), (("key", 1, 0), "mix:monitor"), (("key", 2, 0), "mix:stream"),
        (("key", 7, 0), "pages")]
    assert (("key", 0, 1), "system", "tile") in xl[0] and (("key", 5, 2), "sfx", "fader_top") in xl[0]
    assert (("key", 5, 3), "sfx", "fader_bottom") in xl[0]
    assert len(deckexport.plan(Geometry(8, 4), CHANS * 2)) == 2
    # MK.2: letzte Spalte Mix der Reihe nach und Seiten
    mk2 = deckexport.plan(Geometry(5, 3), ["a", "b"])
    assert mk2 == [[(("key", 0, 0), "a", "tile"), (("key", 0, 1), "a", "fader_top"), (("key", 0, 2), "a", "fader_bottom"),
                    (("key", 1, 0), "b", "tile"), (("key", 1, 1), "b", "fader_top"), (("key", 1, 2), "b", "fader_bottom"),
                    (("key", 4, 0), None, "mix:cycle"), (("key", 4, 1), None, "pages")]]
    assert len(deckexport.plan(Geometry(5, 3), CHANS)) == 2
    fixed = deckexport.plan(Geometry(5, 3), ["a"], selectable=False)
    assert [k for _s, c, k in fixed[0] if c is None] == ["pages"]
    mini = deckexport.plan(Geometry(3, 2), ["a", "b", "c"])
    assert len(mini) == 2 and (("key", 0, 1), "a", "fader_bottom") in mini[0] and (("key", 2, 1), None, "pages") in mini[0]
    plus = deckexport.plan(Geometry(4, 2, dials=4), ["a", "b", "c", "d", "e"])
    assert len(plus) == 2
    assert (("dial", 0), "a", "dial") in plus[0] and (("key", 3, 0), "d", "tile") in plus[0]
    assert (("key", 1, 1), None, "mix:monitor") in plus[1] and (("key", 3, 1), None, "pages") in plus[1]
    with pytest.raises(deckexport.ExportError):
        deckexport.plan(Geometry(3, 1), ["a"])  # Pedal
    with pytest.raises(deckexport.ExportError):
        deckexport.plan(Geometry(5, 3), [])


def test_export_creates_own_profile(store):
    editdoc.create_profile("Gaming", MK2, store)
    name, pages, created = deckexport.export(PLUS, CHANS, mix="stream")
    assert (name, pages, created) == ("Linewave", 2, True)
    root = _profile(store, "Linewave")
    p1, p2 = _pages(root)
    assert p1[0] == "Linewave 1"
    assert p1[1][("dial", 0)] == ("com.lindeck.mixer.channel", "system", "mute", "stream", 2)
    assert p1[1][("key", 3, 0)] == ("com.lindeck.mixer.channel", "voice", "mute", "stream", 5)
    assert p2[1][("key", 3, 1)][0] == "com.lindeck.mixer.pages"
    assert not any(v[0] == "com.lindeck.mixer.mixselect" for v in p1[1].values())  # feste Mischung
    doc = editdoc.ProfileDoc(root)
    assert doc.page(doc.page_ids[1]).get(("key", 3, 1))["Settings"] == {"first": 5, "last": 6}
    meta = json.loads((root / "lindeck-meta.json").read_text())
    assert meta["linewave"] == {"channels": CHANS, "all": True, "mix": "stream", "mixes": ["monitor", "stream"]}
    # zweites Mal: dasselbe Profil wird ersetzt, kein zweites
    assert deckexport.export(PLUS, CHANS[:2]) == ("Linewave", 1, False)
    assert len(list(store.iterdir())) == 2 and len(_pages(root)) == 1
    # anderes Modell mit gleichem Namen: eigener Name
    assert deckexport.export(MK2, CHANS[:2])[0] == "Linewave (LinDeck MK.2)"
    with pytest.raises(deckexport.ExportError):
        deckexport.export(PEDAL, CHANS)


def test_export_pushes_to_running_lindeck(store, monkeypatch):
    import signal
    import subprocess
    import sys
    sent = []
    monkeypatch.setattr(deckexport.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    assert deckexport.export(MK2, CHANS[:1])[0] == "Linewave" and sent == []  # LinDeck läuft nicht
    fake = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "lindeck"])
    try:
        paths.PID_FILE.write_text(str(fake.pid))
        deckexport.export(MK2, CHANS[:2])
        assert sent == [(fake.pid, signal.SIGHUP)]
        paths.PID_FILE.write_text(str(deckexport.os.getpid()))  # nie sich selbst
        assert not deckexport.notify_lindeck()
    finally:
        monkeypatch.undo()  # os.kill wieder echt, sonst wird der Hilfsprozess nicht beendet
        fake.kill()
        fake.wait()


def test_app_refreshes_profile_list_on_push():
    import threading

    from streamlin import app
    calls = []

    class Root:
        def after(self, ms, fn):
            calls.append("after")

    class Editor:
        def profiles_changed(self):
            calls.append("profiles")
    a = app.App.__new__(app.App)
    a.root, a.editor = Root(), Editor()
    a._want_show, a._want_quit, a._want_profiles = threading.Event(), threading.Event(), threading.Event()
    a.request_profiles()
    a._poll()
    a._poll()
    assert calls == ["profiles", "after", "after"]


def test_devices_in_use_first(store):
    gaming = editdoc.create_profile("Gaming", MK2, store)
    (store.parent / "state.json").write_text(json.dumps({"SERIAL": {"profile": gaming.name[:-10]}}))
    devs = deckexport.devices()
    assert devs[0] == {"model": MK2, "name": "LinDeck MK.2", "connected": False, "in_use": True, "profile": ""}
    assert {d["model"] for d in devs} >= {PLUS, MINI}
    deckexport.export(MK2, CHANS[:1])
    assert deckexport.devices()[0]["profile"] == "Linewave"


def test_sync_follows_channels(store):
    deckexport.export(MK2, ["system", "music"])
    deckexport.export(PLUS, ["music"], all_channels=False)
    assert deckexport.sync(["system", "music"]) == []  # nichts geändert
    changed = deckexport.sync(["system", "music", "podcast"])
    assert changed == ["Linewave"]  # nur das Profil mit „alle Kanäle“
    root = _profile(store, "Linewave")
    slots = _pages(root)[0][1]
    assert slots[("key", 2, 0)][1:4] == ("podcast", "mute", "selected")
    assert slots[("key", 2, 2)][1:3] == ("podcast", "down")
    assert deckexport.sync(["system"]) == ["Linewave"]  # gelöschte Kanäle verschwinden
    plus = _profile(store, "Linewave (LinDeck +)")
    assert deckexport.sync(["system"]) == []
    assert json.loads((plus / "lindeck-meta.json").read_text())["linewave"]["channels"] == ["music"]


def test_editor_notices_change(store):
    deckexport.export(MK2, CHANS[:1])
    root = _profile(store, "Linewave")
    doc = editdoc.ProfileDoc(root)
    assert not doc.disk_changed()
    deckexport.export(MK2, CHANS[:3])
    assert doc.disk_changed() and not doc.dirty


def test_volume_keys_show_direction():
    from streamlin import native

    class Fake(native.MixerAction):
        def __init__(self, action, slot):
            self.settings, self.slot, self.uuid = {"action": action, "mix": "both"}, slot, "com.lindeck.mixer.channel"

        def _level(self):
            return 40, False
    assert Fake("up", ("key", 0)).status_text() == "+ 40%"
    assert Fake("down", ("key", 1)).status_text() == "− 40%"
    assert Fake("mute", ("key", 2)).status_text() == "40%"
    assert Fake("mute", ("dial", 0)).status_text() == "40%"


def test_volume_never_above_100():
    from streamlin import mixer
    written = []

    class M(mixer.Mixer):
        def __init__(self):
            pass

        def _duck(self):
            return None

        def _loop_input(self, cid, mix):
            return {"index": "7"}

        def get(self, cid, mix):
            return 98, False

        def run(self, args):
            written.append(args)
    m = M()
    m.change_volume("music", "monitor", 5)
    m.set_volume("music", "stream", 150)
    assert [w[-1] for w in written] == ["100%", "100%"]
    assert mixer.duck_volume(140, 1.0) == 100


# ------------------------------------------------------------------ Oberfläche
tk = pytest.importorskip("tkinter")


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("kein Display")
    yield r
    r.destroy()


def test_button_redraws_only_on_change(root):
    from streamlin.theme import Button
    b = Button(root, "Szenen")
    calls = []
    b._draw = lambda: calls.append(1)
    b.configure_text(text="Szenen")
    b.configure_text(enabled=True)
    assert calls == []
    b.configure_text(text="Gaming")
    assert calls == [1]


def test_export_dialog(root, store):
    from streamlin.wavedialogs import DeckExportDialog
    gaming = editdoc.create_profile("Gaming", PLUS, store)
    (store.parent / "state.json").write_text(json.dumps({"S": {"profile": gaming.name[:-10]}}))
    done = []
    chans = [("system", "System"), ("music", "Musik"), ("browser", "Browser")]
    dlg = DeckExportDialog(root, chans, on_done=lambda name, created: done.append((name, created)))
    assert dlg.create_btn.enabled and dlg._selected_device()["model"] == PLUS
    dlg._toggle("browser")
    dlg._set_mix("monitor")
    dlg._create()
    assert done == [("Linewave", True)]
    slots = _pages(_profile(store, "Linewave"))[0][1]
    mixers = {k: v for k, v in slots.items() if v[0] == "com.lindeck.mixer.channel"}
    assert {v[1] for v in mixers.values()} == {"system", "music"} and {v[3] for v in mixers.values()} == {"monitor"}
    meta = json.loads((_profile(store, "Linewave") / "lindeck-meta.json").read_text())
    assert meta["linewave"]["all"] is False
    dlg = DeckExportDialog(root, chans)
    assert dlg.create_btn.text == "Profil aktualisieren"
    dlg.destroy()


def test_key_looks_and_mix_select():
    from streamlin import mixkeys, native
    for img in (mixkeys.tile((72, 72), "music", "#1fa463", 40, False, "both"),
                mixkeys.tile((72, 72), "mic", "#e5484d", 40, True, "stream"),
                mixkeys.tile((72, 72), "music", "#1fa463", None, False, "both"),
                mixkeys.fader((72, 72), "top", "game", "#f59e0b", 100, False, "monitor"),
                mixkeys.fader((96, 96), "bottom", "game", "#f59e0b", 0, True, "monitor"),
                mixkeys.mix_key((72, 72), "monitor", True), mixkeys.pages_key((72, 72), 1, 7)):
        assert img.size in ((72, 72), (96, 96))
    # Fader: bei 100 % ist der Knopf oben, bei 0 % unten – die Hälften unterscheiden sich
    full_top = mixkeys.fader((72, 72), "top", "game", "#f59e0b", 100, False, "both")
    empty_top = mixkeys.fader((72, 72), "top", "game", "#f59e0b", 0, False, "both")
    assert full_top.tobytes() != empty_top.tobytes()

    class Ctrl:
        preview = False

        def __init__(self):
            self.keys, self.dials = {}, {}

        def redraw_action(self, a):
            pass

    class Ctx:
        ctrl = Ctrl()

    def make(cls, uuid, settings):
        return cls({"UUID": uuid, "Settings": settings}, None, Ctx)
    monitor = make(native.MixSelectAction, "com.lindeck.mixer.mixselect", {"mix": "monitor"})
    cycle = make(native.MixSelectAction, "com.lindeck.mixer.mixselect", {"mix": "cycle"})
    chan = make(native.MixerAction, "com.lindeck.mixer.channel", {"channel": "music", "mix": "selected"})
    assert native.lookup("com.lindeck.mixer.mixselect") is native.MixSelectAction
    assert native.lookup("com.lindeck.mixer.pages") is native.MixerPagesAction
    native._DECK_MIX["mix"] = "both"
    try:
        assert not monitor.active() and chan.mix == "both"
        monitor.key_down()
        assert monitor.active() and chan.mix == "monitor"
        cycle.key_down()
        assert native.deck_mix() == "stream" and chan.mix == "stream"
        cycle.key_down()
        assert native.deck_mix() == "both"
    finally:
        native._DECK_MIX["mix"] = "both"


def test_connected_decks_come_first_and_new_ones_get_a_profile(store, tmp_path):
    from streamlin import models
    assert models.connected_models() == []
    _plug(tmp_path, 0x008F, 0x0084)  # Stream Deck XL V2 und Stream Deck +
    (tmp_path / "usb" / "2-1").mkdir()
    (tmp_path / "usb" / "2-1" / "idVendor").write_text("046d\n")  # andere Firma
    assert [d["model"] for d in models.connected_models()] == ["20GAT9902", "20GBD9901"]
    devs = deckexport.devices()
    assert [d["model"] for d in devs[:2]] == ["20GAT9902", "20GBD9901"] and devs[0]["connected"]
    assert not devs[2]["connected"]
    # noch nie exportiert: nichts automatisch anlegen
    assert deckexport.adopt_new_devices() == []
    name, _p, _n = deckexport.export("20GAT9902", ["system", "music"], mix="selected")
    # das zweite angeschlossene Gerät bekommt automatisch ein Profil mit denselben Kanälen
    assert deckexport.adopt_new_devices() == ["Linewave (LinDeck +)"]
    plus = _profile(store, "Linewave (LinDeck +)")
    meta = json.loads((plus / deckexport.META).read_text())["linewave"]
    assert meta["channels"] == ["system", "music"] and meta["mix"] == "selected"
    assert deckexport.adopt_new_devices() == []  # nur einmal


def test_export_dialog_uses_connected_deck(root, store, tmp_path):
    from streamlin.wavedialogs import DeckExportDialog
    _plug(tmp_path, 0x006C, 0x0084)
    chans = [("system", "System"), ("music", "Musik")]
    dlg = DeckExportDialog(root, chans)
    assert dlg.device_box is None and len(dlg.cards) == 2
    assert dlg._selected_device()["model"] == "20GAT9901"
    dlg.cards[1][1].toggle()  # zweites Gerät anklicken
    assert dlg._selected_device()["model"] == "20GBD9901"
    assert [c.checked for _d, c in dlg.cards] == [False, True]
    dlg.destroy()
