"""Tests für FreeDeck als eine App (Fenster + Dienst in einem Prozess)."""
import json
import os
import subprocess
import sys
import time

import pytest

from freestream import editdoc as E
from freestream.models import Geometry


@pytest.fixture
def env(tmp_path, monkeypatch):
    from freestream import paths
    for name, sub in (("DATA_DIR", "data"), ("STATE_DIR", "state"), ("PROFILES_DIR", "data/profiles"),
                      ("PLUGINS_DIR", "data/plugins"), ("PID_FILE", "state/freedeck.pid")):
        monkeypatch.setattr(paths, name, tmp_path / sub)
    (tmp_path / "data" / "profiles").mkdir(parents=True)
    (tmp_path / "state").mkdir()
    return tmp_path


def test_single_instance_detection(env):
    from freestream import app
    assert app.running_instance() is None
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (env / "state" / "app.json").write_text(json.dumps({"pid": sleeper.pid, "gui": True}))
        assert app.running_instance() == sleeper.pid
    finally:
        sleeper.kill()
        sleeper.wait()
    assert app.running_instance() is None  # tote Pid zählt nicht


def test_icon():
    from freestream.app import make_icon
    img = make_icon(64)
    assert img.size == (64, 64) and img.getpixel((32, 32))[3] == 255


class FakeCtrl:
    def __init__(self, serial, model, geometry, profile_id):
        self.serial, self.model, self.geometry = serial, model, geometry
        self.profile = type("P", (), {"id": profile_id})() if profile_id else None
        self.page_index = 0
        self.calls = []

    def load_profile(self, pid, page_index=None):
        self.calls.append(("load", pid, page_index))
        self.profile = type("P", (), {"id": pid})()
        self.page_index = page_index or 0

    def goto_page(self, idx):
        self.calls.append(("page", idx))
        self.page_index = idx


@pytest.fixture
def gui(env):
    tk = pytest.importorskip("tkinter")
    if not os.environ.get("DISPLAY"):
        pytest.skip("kein Display (xvfb-run verwenden)")
    from freestream import paths
    from freestream.profiles import ProfileStore
    plus = E.create_profile("Plus", "20GBD9901", paths.PROFILES_DIR)
    mk2 = E.create_profile("Klassisch", "20GBA9901", paths.PROFILES_DIR)
    doc = E.ProfileDoc(plus)
    doc.add_page()
    doc.cleanup()
    ctrl = FakeCtrl("SN1", "20GBD9901", Geometry(4, 2, 4), plus.name[:-10])

    class Daemon:
        controllers = {"a": ctrl}
        store = ProfileStore(paths.PROFILES_DIR)
        cfg = {}
    root = tk.Tk()
    from freestream.app import App
    a = App(root, Daemon(), hidden=True, tray=False)
    root.update()
    yield a, ctrl, plus.name[:-10], mk2.name[:-10]
    try:
        root.destroy()
    except tk.TclError:
        pass


def _pump(root, seconds=0.6):
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.02)


def test_app_window_device_and_quit(gui):
    a, ctrl, plus_id, mk2_id = gui
    root, ed = a.root, a.editor
    assert root.state() == "withdrawn"  # Autostart: unsichtbar
    a.request_show()  # z. B. zweiter Start oder Taskleiste
    _pump(root)
    assert root.state() == "normal"
    # Gerät erkannt, dessen Profil ist geöffnet
    assert ed.device == "SN1" and ed.doc.id == plus_id
    assert "SN1" in ed.device_cards and ed.device_cards["SN1"].selected
    # Seite im Fenster wählen -> Gerät folgt
    ed.goto_page(ed.doc.page_ids[1])
    _pump(root)
    assert ("page", 1) in ctrl.calls
    # Profil wechseln, das nicht zum Gerät passt -> Gerät bleibt
    ed.open_profile(mk2_id)
    _pump(root)
    assert not any(c[0] == "load" for c in ctrl.calls)
    # passendes Profil -> Gerät wechselt mit
    ed.open_profile(plus_id)
    _pump(root)
    # Fenster schließen = ausblenden, App läuft weiter
    ed.close()
    _pump(root, 0.3)
    assert root.state() == "withdrawn" and not a.quitting
    a.request_quit()
    _pump(root, 0.5)
    assert a.quitting
