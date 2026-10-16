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
    return root


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
    plus = deckexport.plan(Geometry(4, 2, dials=4), ["a", "b", "c", "d", "e"])
    assert len(plus) == 2
    assert (("dial", 0), "a", "dial") in plus[0] and (("key", 3, 0), "d", "mute") in plus[0]
    assert (("key", 2, 1), None, "prev") in plus[1] and (("key", 3, 1), None, "next") in plus[1]
    mk2 = deckexport.plan(Geometry(5, 3), ["a", "b"])
    assert mk2 == [[(("key", 0, 0), "a", "up"), (("key", 0, 1), "a", "mute"), (("key", 0, 2), "a", "down"),
                    (("key", 1, 0), "b", "up"), (("key", 1, 1), "b", "mute"), (("key", 1, 2), "b", "down")]]
    # mehr Kanäle als Spalten: letzte Spalte zum Blättern
    many = deckexport.plan(Geometry(5, 3), CHANS)
    assert len(many) == 2 and {k for _s, c, k in many[0] if c is None} == {"prev", "indicator", "next"}
    assert not any(s == ("key", 4, 0) and c for s, c, _k in many[0])
    mini = deckexport.plan(Geometry(3, 2), ["a", "b", "c", "d"])
    assert len(mini) == 2 and (("key", 0, 1), "a", "down") in mini[0] and (("key", 2, 1), None, "next") in mini[0]
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
    assert p2[1][("key", 3, 1)][0] == "com.elgato.streamdeck.page.next"
    meta = json.loads((root / "lindeck-meta.json").read_text())
    assert meta["linewave"] == {"channels": CHANS, "all": True, "mix": "stream"}
    # zweites Mal: dasselbe Profil wird ersetzt, kein zweites
    assert deckexport.export(PLUS, CHANS[:2]) == ("Linewave", 1, False)
    assert len(list(store.iterdir())) == 2 and len(_pages(root)) == 1
    # anderes Modell mit gleichem Namen: eigener Name
    assert deckexport.export(MK2, CHANS[:2])[0] == "Linewave (Stream Deck MK.2)"
    with pytest.raises(deckexport.ExportError):
        deckexport.export(PEDAL, CHANS)


def test_devices_in_use_first(store):
    gaming = editdoc.create_profile("Gaming", MK2, store)
    (store.parent / "state.json").write_text(json.dumps({"SERIAL": {"profile": gaming.name[:-10]}}))
    devs = deckexport.devices()
    assert devs[0] == {"model": MK2, "name": "Stream Deck MK.2", "in_use": True, "profile": ""}
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
    assert slots[("key", 2, 1)][1:3] == ("podcast", "mute")
    assert deckexport.sync(["system"]) == ["Linewave"]  # gelöschte Kanäle verschwinden
    plus = _profile(store, "Linewave (Stream Deck +)")
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
    assert {v[1] for v in slots.values()} == {"system", "music"} and {v[3] for v in slots.values()} == {"monitor"}
    meta = json.loads((_profile(store, "Linewave") / "lindeck-meta.json").read_text())
    assert meta["linewave"]["all"] is False
    dlg = DeckExportDialog(root, chans)
    assert dlg.create_btn.text == "Profil aktualisieren"
    dlg.destroy()
