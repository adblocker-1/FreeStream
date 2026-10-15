"""Tests mit simulierten Geräten (Dummy-Transport der StreamDeck-Bibliothek).

Aufruf: STREAMLIN_TEST_PROFILES=<Ordner mit .streamDeckProfile> pytest -q tests
"""
import glob
import os
import time

import pytest
from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.Devices.StreamDeck import TouchscreenEventType

from streamlin import actions as A
from streamlin import profiles as P
from streamlin import system
from streamlin.config import Config, State
from streamlin.engine import DeckController
from streamlin.keyboard import VirtualInput

SRC = os.environ.get("STREAMLIN_TEST_PROFILES", "")


class RecInput(VirtualInput):
    def __init__(self):
        super().__init__("de")
        self.available = True
        self.taps = []

    def _open(self):
        pass

    def tap(self, codes, hold_ms=15):
        self.taps.append(list(codes))


class RecSound:
    def __init__(self):
        self.played = []

    def play(self, key, path, volume=100, output="", loop=False):
        self.played.append((path, volume, loop))

    def is_playing(self, key):
        return False

    def stop(self, key=None):
        pass


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    if not SRC:
        pytest.skip("STREAMLIN_TEST_PROFILES nicht gesetzt")
    root = tmp_path_factory.mktemp("store")
    for f in glob.glob(os.path.join(SRC, "*.streamDeckProfile")):
        P.import_path(f, root / "profiles", root / "res")
    return P.ProfileStore(root / "profiles")


@pytest.fixture
def launched(monkeypatch):
    calls = []
    monkeypatch.setattr(system, "run_bg", lambda cmd, shell=False: calls.append(cmd) or True)
    monkeypatch.setattr(system, "toggle_mute", lambda source=False: calls.append("mute"))
    monkeypatch.setattr(system, "change_volume", lambda d, source=False: calls.append(("vol", d)))
    monkeypatch.setattr(system, "get_volume", lambda source=False: (42, False))
    return calls


def make(store, deck_type, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    deck = next(d for d in DeviceManager(transport="dummy").enumerate() if d.deck_type() == deck_type)
    deck.open()
    st = State()
    st.save = lambda: None
    ctrl = DeckController(deck, store, Config(), st, RecInput(), RecSound())
    ctrl.serial = "test-" + deck_type
    return ctrl


def find_key(ctrl, uuid, pred=lambda a: True):
    for k, a in ctrl.keys.items():
        if a.uuid == uuid and pred(a):
            return k, a
    raise AssertionError(f"{uuid} nicht auf Seite")


def press(ctrl, k, held=0.0):
    ctrl.on_key(k, True)
    if held:
        time.sleep(held)
    ctrl.on_key(k, False)
    time.sleep(0.15)


ALL_TYPES = ["Stream Deck Original", "Stream Deck Mini", "Stream Deck XL", "Stream Deck +",
             "Stream Deck + XL", "Stream Deck Neo", "Stream Deck Pedal", "Stream Deck Studio"]


@pytest.mark.parametrize("deck_type", ALL_TYPES)
def test_every_model_renders(store, deck_type, tmp_path, monkeypatch):
    ctrl = make(store, deck_type, tmp_path, monkeypatch)
    pid = ctrl._initial_profile_id()
    ctrl.load_profile(pid, 0)
    img = ctrl.preview_image()
    assert img.size[0] > 0
    if pid:
        p = store.get(pid)
        for pg in p.all_pages():
            ctrl.folder_stack = [pg]
            ctrl.show_current()


def test_model_matching(store, tmp_path, monkeypatch):
    for t, expect in [("Stream Deck +", "20GBD9901"), ("Stream Deck + XL", "20GBX9901"),
                      ("Stream Deck Neo", "20GBJ9901"), ("Stream Deck XL", "20GAT9902"),
                      ("Stream Deck Mini", "20GAI9901")]:
        ctrl = make(store, t, tmp_path, monkeypatch)
        p = store.get(ctrl._initial_profile_id())
        assert p.model.startswith(expect[:7]), (t, p.model)


def test_folders_pages_and_hotkeys(store, tmp_path, monkeypatch, launched):
    ctrl = make(store, "Stream Deck +", tmp_path, monkeypatch)
    pid = next(p.id for p in store.profiles.values() if p.model == "20GBD9901"
               and any(a.get("UUID", "").endswith("system.multimedia") for a in p.page(p.page_ids[0]).encoders.values()))
    ctrl.load_profile(pid, 0)
    # "Show Desktop" -> Super+D
    k, a = find_key(ctrl, "com.elgato.streamdeck.system.hotkey", lambda a: "Desktop" in a.title())
    press(ctrl, k)
    vin = ctrl.vin
    assert vin.taps[-1] == [vin.key_by_name("KEY_LEFTMETA"), vin.key_by_name("KEY_D")]
    # Ordner öffnen und zurück
    k, a = find_key(ctrl, "com.elgato.streamdeck.profile.openchild")
    press(ctrl, k)
    assert ctrl.folder_stack, "Ordner nicht geöffnet"
    kb, _ = find_key(ctrl, "com.elgato.streamdeck.profile.backtoparent")
    press(ctrl, kb)
    assert not ctrl.folder_stack
    # Seite weiter per Wischen
    ctrl.on_touch(TouchscreenEventType.DRAG, {"x": 600, "y": 50, "x_out": 100, "y_out": 50})
    assert ctrl.page_index == 1
    ctrl.goto_page(0)
    # Dial: System-Lautstärke
    vol = next(i for i, d in ctrl.dials.items() if d.uuid.endswith("multimedia"))
    ctrl.on_dial_rotate(vol, 3)
    ctrl.on_dial_push(vol, True)
    ctrl.on_dial_push(vol, False)
    time.sleep(0.2)
    assert ("vol", 6) in launched and "mute" in launched
    # Dial: Helligkeit
    br = next(i for i, d in ctrl.dials.items() if d.uuid.endswith("keybrightness"))
    before = ctrl.brightness
    ctrl.on_dial_rotate(br, -2)
    time.sleep(0.2)
    assert ctrl.brightness == before - 10
    # Dial-Hotkey: Linksdrehung = Strg+Z, Rechtsdrehung = Strg+Umschalt+Z
    hk = next(i for i, d in ctrl.dials.items() if d.uuid.endswith("system.hotkey"))
    ctrl.on_dial_rotate(hk, -1)
    time.sleep(0.2)
    assert vin.taps[-1] == [vin.key_by_name("KEY_LEFTCTRL"), vin.key_by_name("KEY_Y")]  # VK_Z auf DE = phys. Y
    ctrl.on_dial_rotate(hk, 1)
    time.sleep(0.2)
    assert vin.key_by_name("KEY_LEFTSHIFT") in vin.taps[-1]


def test_website_and_multimedia(store, tmp_path, monkeypatch, launched):
    ctrl = make(store, "Stream Deck XL", tmp_path, monkeypatch)
    pid = next(p.id for p in store.profiles.values() if p.model == "20GAT9902")
    ctrl.load_profile(pid, 0)
    p = store.get(pid)
    for pg in p.all_pages():
        ctrl.folder_stack = [pg]
        ctrl.show_current()
        for k, a in ctrl.keys.items():
            if a.uuid.endswith("system.website"):
                press(ctrl, k)
                assert any("xdg-open" in str(c) for c in launched)
                return
    pytest.fail("keine Website-Aktion gefunden")


def test_neo_touch_keys_and_infobar(store, tmp_path, monkeypatch):
    ctrl = make(store, "Stream Deck Neo", tmp_path, monkeypatch)
    ctrl.load_profile(ctrl._initial_profile_id(), 0)
    assert ctrl.screen_size == (248, 58)
    ctrl.on_key(ctrl.key_count + 1, True)   # rechte Touchtaste
    assert ctrl.page_index == 1
    ctrl.on_key(ctrl.key_count, True)
    assert ctrl.page_index == 0
    assert ctrl.infobar_image().size == (248, 58)


def test_text_paste(store, tmp_path, monkeypatch):
    ctrl = make(store, "Stream Deck XL", tmp_path, monkeypatch)
    got = {}
    monkeypatch.setattr(system, "clipboard_get", lambda: "alt")
    monkeypatch.setattr(system, "clipboard_set", lambda t: got.setdefault("t", t) or True)
    a = A.create_action({"UUID": "com.elgato.streamdeck.system.text",
                         "Settings": {"pastedText": "👍 Hallo", "isSendingEnter": True}}, None, ctrl.ctx or
                        A.Context(ctrl, ctrl.cfg, ctrl.vin, ctrl.sound))
    a.key_down()
    assert got["t"] == "👍 Hallo"
    vin = ctrl.vin
    assert vin.taps[-2] == [vin.key_by_name("KEY_LEFTCTRL"), vin.key_by_name("KEY_V")]
    assert vin.taps[-1] == [vin.key_by_name("KEY_ENTER")]


def test_multi_action_and_key_logic(store, tmp_path, monkeypatch):
    ctrl = make(store, "Stream Deck XL", tmp_path, monkeypatch)
    ctx = A.Context(ctrl, ctrl.cfg, ctrl.vin, ctrl.sound)
    hk = lambda vk: {"UUID": "com.elgato.streamdeck.system.hotkey",
                     "Settings": {"Hotkeys": [{"KeyModifiers": 0, "VKeyCode": vk, "QTKeyCode": vk}]}}
    ma = A.create_action({"UUID": "com.elgato.streamdeck.multiactions.routine", "Actions": [{"Actions": [
        hk(0x41), {"UUID": "com.elgato.streamdeck.multiactions.delay", "Settings": {"delay": 50}}, hk(0x42)]}]},
        None, ctx)
    ma.key_down()
    v = ctrl.vin
    for _ in range(40):
        if len(v.taps) >= 2:
            break
        time.sleep(0.05)
    assert v.taps[-2:] == [[v.key_by_name("KEY_A")], [v.key_by_name("KEY_B")]]
    kl = A.create_action({"UUID": "com.elgato.streamdeck.keys.logic",
                          "Actions": [hk(0x43), {"Actions": []}, hk(0x44)]}, None, ctx)
    kl.key_up(50)
    time.sleep(0.6)
    assert v.taps[-1] == [v.key_by_name("KEY_C")]
    kl.key_up(900)
    time.sleep(0.6)
    assert v.taps[-1] == [v.key_by_name("KEY_D")]


def test_soundboard_resource(store, tmp_path, monkeypatch):
    ctrl = make(store, "Stream Deck + XL", tmp_path, monkeypatch)
    pid = ctrl._initial_profile_id()
    p = store.get(pid)
    from streamlin import paths
    monkeypatch.setattr(paths, "RESOURCES_DIR", store.root.parent / "res")
    ctrl.load_profile(pid, 0)
    for pg in p.all_pages():
        ctrl.folder_stack = [pg]
        ctrl.show_current()
        for k, a in ctrl.keys.items():
            if a.uuid.endswith("playaudio") and (a.data.get("Resources") or {}).get("file"):
                a.key_down()
                assert ctrl.sound.played and ctrl.sound.played[-1][0].endswith(".mp3")
                return
    pytest.fail("keine Soundboard-Aktion mit Ressource")


def test_sleep_and_wake(store, tmp_path, monkeypatch):
    ctrl = make(store, "Stream Deck Mini", tmp_path, monkeypatch)
    ctrl.load_profile(ctrl._initial_profile_id(), 0)
    ctrl.sleep_device()
    assert ctrl.sleeping
    ctrl._handle(("key", 0, True))
    assert not ctrl.sleeping
