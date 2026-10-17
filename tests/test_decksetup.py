"""Einrichtung von LinDeck beim ersten Start."""
import os
import types

import pytest

from streamlin import decksetup
from streamlin import editdoc as E

needs_x = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="braucht X (xvfb-run)")


@pytest.fixture
def wizard(tmp_path, monkeypatch):
    tk = pytest.importorskip("tkinter")
    if not os.environ.get("DISPLAY"):
        pytest.skip("kein Display")
    from streamlin import editor
    from streamlin.config import Config
    from test_settings_tabs import FakeCtrl, _pump, _shot
    monkeypatch.delenv("STREAMLIN_NO_SETUP", raising=False)
    monkeypatch.setattr(decksetup, "find_profiles", lambda: [])
    monkeypatch.setattr(decksetup, "connected_models", lambda: [])
    store = tmp_path / "profiles"
    store.mkdir()
    cfg = Config()
    cfg.save = lambda: None
    ctrl = FakeCtrl("A1", "20GBD9901", None)
    daemon = types.SimpleNamespace(controllers={"a": ctrl}, cfg=cfg, plugin_host=None, store=None)
    root = tk.Tk()
    opened = []
    monkeypatch.setattr(editor.EditorApp, "open_setup",
                        lambda self: opened.append(editor.EditorApp.__dict__["open_setup"]))
    app = editor.EditorApp(root, store_root=store, daemon=daemon)
    _pump(root, 30)
    yield app, cfg, _pump, _shot
    root.destroy()


def test_needed(tmp_path, monkeypatch):
    monkeypatch.delenv("STREAMLIN_NO_SETUP", raising=False)
    cfg = types.SimpleNamespace(get=lambda k, d=None: {}, device=lambda s: {})
    app = types.SimpleNamespace(store=types.SimpleNamespace(profiles={}), daemon=types.SimpleNamespace(cfg=cfg))
    assert decksetup.needed(app)
    app.store.profiles = {"x": 1}
    assert not decksetup.needed(app)
    app.store.profiles = {}
    cfg.get = lambda k, d=None: {"setup_done": True} if k == "lindeck" else d
    assert not decksetup.needed(app)


@needs_x
def test_wizard_new_profile(wizard):
    app, cfg, pump, shot = wizard
    from streamlin.decksetup import DeckSetup
    ids = []
    dlg = DeckSetup(app, on_done=ids.extend)
    pump(app.root)
    shot(dlg, "setup-1")
    dlg.next()  # Gerät
    pump(app.root)
    assert dlg.model.get() == "20GBD9901"  # angeschlossenes Gerät gewählt
    shot(dlg, "setup-2")
    dlg.next()  # Profile
    dlg.found = []
    dlg._paint_options()
    pump(app.root)
    dlg.next()  # Import ohne Dateien: bleibt stehen
    assert dlg.step == 2 and dlg.error.cget("text")
    dlg._choose("new")
    dlg.name.set("Stream")
    shot(dlg, "setup-3")
    dlg.next()
    assert dlg.step == 3
    shot(dlg, "setup-4")
    dlg.next()
    assert len(ids) == 1 and cfg.data["lindeck"]["setup_done"] is True
    pump(app.root)
    assert E.ProfileDoc(app.store_root / f"{ids[0]}.sdProfile").name == "Stream"


@needs_x
def test_wizard_imports_found_profiles(wizard, tmp_path):
    app, cfg, pump, _shot = wizard
    from streamlin.decksetup import DeckSetup
    src = tmp_path / "usb" / "ProfilesV3"
    src.mkdir(parents=True)
    E.create_profile("From Windows", "20GBD9901", src)
    dlg = DeckSetup(app)
    dlg.found = [src]
    dlg.step = 2
    dlg.render()
    pump(app.root)
    dlg.next()
    assert dlg.step == 3 and len(dlg.imported) == 1
    dlg.skip()
    assert cfg.data["lindeck"]["setup_done"] is True
