"""Linewave-Kanäle auf das Stream Deck legen; Kopfzeile des Mixers ohne Flackern."""
import json

import pytest

from streamlin import deckexport, editdoc, paths
from streamlin.models import Geometry

CHANS = [("system", "System"), ("music", "Musik"), ("browser", "Browser"), ("voice", "Voice"),
         ("game", "Game"), ("sfx", "SFX")]


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    root.mkdir()
    monkeypatch.setattr(paths, "PROFILES_DIR", root)
    monkeypatch.setattr(paths, "STATE_FILE", tmp_path / "state.json")
    return root


def _make(store, model, name):
    root = editdoc.create_profile(name, model, store)
    return root.name[: -len(".sdProfile")]


def _actions(store, pid):
    doc = editdoc.ProfileDoc(store / f"{pid}.sdProfile")
    out = {}
    for page in doc.page_ids[1:]:  # erste Seite = leere Seite aus create_profile
        pg = doc.page(page)
        out[pg.name] = {slot: (a["UUID"], a["Settings"]["channel"], a["Settings"]["action"], a["Settings"]["mix"],
                               a["Settings"]["step"]) for slot, a in pg.items()}
    return out


def test_plan_layouts():
    plus = deckexport.plan(Geometry(4, 2, dials=4), ["a", "b", "c", "d", "e"])
    assert len(plus) == 2
    assert (("dial", 0), "a", "dial") in plus[0] and (("key", 3, 0), "d", "mute") in plus[0]
    assert plus[1] == [(("dial", 0), "e", "dial"), (("key", 0, 0), "e", "mute")]
    mk2 = deckexport.plan(Geometry(5, 3), ["a", "b"])
    assert mk2 == [[(("key", 0, 0), "a", "up"), (("key", 0, 1), "a", "mute"), (("key", 0, 2), "a", "down"),
                    (("key", 1, 0), "b", "up"), (("key", 1, 1), "b", "mute"), (("key", 1, 2), "b", "down")]]
    mini = deckexport.plan(Geometry(3, 2), ["a", "b", "c", "d"])
    assert len(mini) == 2 and (("key", 0, 1), "a", "down") in mini[0]
    with pytest.raises(deckexport.ExportError):
        deckexport.plan(Geometry(3, 1), ["a"])  # Pedal
    with pytest.raises(deckexport.ExportError):
        deckexport.plan(Geometry(5, 3), [])


def test_export_stream_deck_plus(store):
    pid = _make(store, "20GBD9901", "Streaming")
    pages = deckexport.export(pid, CHANS, mix="stream")
    assert pages == ["Linewave 1", "Linewave 2"]
    acts = _actions(store, pid)
    first = acts["Linewave 1"]
    assert first[("dial", 0)] == ("com.lindeck.mixer.channel", "system", "mute", "stream", 2)
    assert first[("key", 3, 0)] == ("com.lindeck.mixer.channel", "voice", "mute", "stream", 5)
    assert set(acts["Linewave 2"]) == {("dial", 0), ("dial", 1), ("key", 0, 0), ("key", 1, 0)}


def test_export_keys_only_and_targets(store):
    pid = _make(store, "20GBA9901", "Gaming")
    other = _make(store, "20GAI9901", "Mini")
    (store.parent / "state.json").write_text(json.dumps({"SERIAL": {"profile": pid, "page": 0}}))
    t = deckexport.targets()
    assert [x["id"] for x in t] == [pid, other] and t[0]["in_use"] and t[0]["model"] == "Stream Deck MK.2"
    assert deckexport.export(pid, CHANS[:2]) == ["Linewave"]
    acts = _actions(store, pid)["Linewave"]
    assert acts[("key", 1, 0)][1:3] == ("music", "up") and acts[("key", 1, 2)][1:3] == ("music", "down")
    assert acts[("key", 0, 1)] == ("com.lindeck.mixer.channel", "system", "mute", "both", 5)
    with pytest.raises(deckexport.ExportError):
        deckexport.export("does-not-exist", CHANS)


def test_editor_notices_new_page(store):
    pid = _make(store, "20GBA9901", "Gaming")
    doc = editdoc.ProfileDoc(store / f"{pid}.sdProfile")
    assert not doc.disk_changed()
    deckexport.export(pid, CHANS[:1])
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
    pid = _make(store, "20GBD9901", "Streaming")
    done = []
    dlg = DeckExportDialog(root, CHANS[:3], on_done=lambda name, pages: done.append((name, pages)))
    assert dlg.create_btn.enabled
    dlg._toggle("browser")
    dlg._set_mix("monitor")
    dlg._create()
    assert done == [("Streaming", ["Linewave"])]
    acts = _actions(store, pid)["Linewave"]
    assert {v[1] for v in acts.values()} == {"system", "music"} and {v[3] for v in acts.values()} == {"monitor"}


def test_export_dialog_without_profiles(root, store):
    from streamlin.wavedialogs import DeckExportDialog
    dlg = DeckExportDialog(root, CHANS)
    assert not dlg.create_btn.enabled
    dlg.destroy()
