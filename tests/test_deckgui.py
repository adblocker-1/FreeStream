"""Deck und Fenster gleich: Seitenwechsel am Deck im Fenster, Gedrückthalten, schneller Seitenwechsel."""
import os
import threading
import time
import types

import pytest
from PIL import Image

from freestream import actions as A
from freestream import editdoc as E
from freestream import mixkeys, system, theme


# ------------------------------------------------------------------ Gedrückthalten
class Counter(A.Action):
    REPEAT_DELAY, REPEAT_EVERY = 0.05, 0.02

    def __init__(self):
        super().__init__({"UUID": "x"}, None, None)
        self.n = 0
        self.lock = threading.Lock()

    def step(self):
        with self.lock:
            self.n += 1

    def key_down(self):
        self.step()
        self.start_repeat(self.step)

    def key_up(self, held_ms=0):
        self.stop_repeat()


def test_hold_repeats_until_release():
    a = Counter()
    a.pressed = True
    a.key_down()
    time.sleep(0.25)
    a.pressed = False
    a.key_up()
    n = a.n
    assert n >= 4  # erster Schritt + Wiederholungen
    time.sleep(0.1)
    assert a.n == n  # nach dem Loslassen nichts mehr


def test_short_press_and_multi_action_step_once():
    a = Counter()
    a.execute()  # Multi-Action: nie „gedrückt“ – darf nicht hängen bleiben oder wiederholen
    time.sleep(0.15)
    assert a.n == 1
    b = Counter()
    b.pressed = True
    b.key_down()
    b.pressed = False  # schon losgelassen, bevor die Wiederholung beginnt
    time.sleep(0.15)
    assert b.n == 1


def test_mixer_key_repeats_volume(monkeypatch):
    from freestream import native
    calls = []
    fake = types.SimpleNamespace(change_volume=lambda c, m, d, **kw: calls.append(d) or {m: 50}, channel=lambda c: None,
                                 mix_ids=lambda: ["monitor", "stream"], channel_mixes=lambda c: ["monitor"],
                                 get=lambda c, m: (50, False))
    monkeypatch.setattr(native.MixerAction, "mixer", property(lambda self: fake))
    monkeypatch.setattr(native.MixerAction, "REPEAT_DELAY", 0.05)
    monkeypatch.setattr(native.MixerAction, "REPEAT_EVERY", 0.02)
    a = native.MixerAction({"UUID": "com.lindeck.mixer.channel",
                            "Settings": {"channel": "music", "action": "down", "step": 3}}, None, None)
    a.redraw = lambda: None
    a.pressed = True
    a.key_down()
    time.sleep(0.2)
    a.pressed = False
    a.key_up()
    n = len(calls)
    time.sleep(0.1)
    assert n >= 3 and set(calls) == {-3} and len(calls) == n


# ------------------------------------------------------------------ schneller Seitenwechsel
def test_key_images_cached():
    mixkeys._CACHE.clear()
    a = mixkeys.tile((96, 96), "music", "#22c55e", 50, False, "both", label="All")
    b = mixkeys.tile((96, 96), "music", "#22c55e", 50, False, "both", label="All")
    c = mixkeys.tile((96, 96), "music", "#22c55e", 55, False, "both", label="All")
    assert a is b and c is not a and a.tobytes() != c.tobytes()
    g1, g2 = theme.glyph("mic", 24, (255, 255, 255)), theme.glyph("mic", 24, (255, 255, 255))
    assert g1 is not g2 and g1.tobytes() == g2.tobytes()  # Kopie: Aufrufer dürfen es verändern


def test_page_switch_asks_pactl_once(tmp_path, monkeypatch):
    from StreamDeck.DeviceManager import DeviceManager

    from freestream import deckexport, paths
    from freestream import mixer as M
    from freestream import profiles as P
    from freestream.config import Config, State
    from freestream.engine import DeckController
    from freestream.keyboard import VirtualInput
    for name, sub in (("PROFILES_DIR", "profiles"), ("STATE_DIR", "state"), ("CONFIG_DIR", "cfg")):
        monkeypatch.setattr(paths, name, tmp_path / sub)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    mx = M.Mixer(Config())
    chans = [c["id"] for c in mx.channels()] + ["mic"]
    listing = "\n".join(
        f"Sink Input #{i}\n\tMute: no\n\tVolume: front-left: 50000 /  76% / -7 dB\n\tProperties:\n"
        f"\t\tnode.name = \"{M.PREFIX}{c}_{m}\"\n"
        for i, (c, m) in enumerate((c, m) for c in chans for m in mx.mix_ids()))
    calls = []

    def run_out(cmd, timeout=2):
        calls.append(cmd)
        return listing if cmd[:3] == ["pactl", "list", "sink-inputs"] else ""
    monkeypatch.setattr(system, "run_out", run_out)
    monkeypatch.setattr(M, "_run", lambda args: run_out(["pactl", *args]))
    monkeypatch.setattr(deckexport, "notify_freedeck", lambda: False)
    deckexport.export("20GAT9901", chans, store_root=paths.PROFILES_DIR)
    store = P.ProfileStore(paths.PROFILES_DIR)
    deck = next(d for d in DeviceManager(transport="dummy").enumerate() if d.deck_type() == "Stream Deck XL")
    deck.open()
    st = State()
    st.save = lambda: None
    ctrl = DeckController(deck, store, Config(), st, VirtualInput("de"), None)
    ctrl.serial = "xl"
    try:
        ctrl.load_profile(next(iter(store.profiles)))
        calls.clear()
        ctrl.next_page()
        assert len([c for c in calls if c[:2] == ["pactl", "list"]]) == 1  # früher: eine je Taste und Mix
        calls.clear()
        ctrl._second_tick(time.time() + 5)
        assert len([c for c in calls if c[:2] == ["pactl", "list"]]) <= 1
        # Live-Bild für das Fenster: Mixer-Tasten ändern sich von selbst
        k = next(k for k, a in ctrl.keys.items() if a.needs_tick)
        assert ctrl.live_image(k) is ctrl.shown[k]
    finally:
        deck.close()  # sonst liest der Dummy-Transport weiter und bremst alle folgenden Tests


# ------------------------------------------------------------------ Fenster folgt dem Deck
class DevCtrl:
    def __init__(self, serial, doc_root):
        from freestream.models import Geometry
        self.serial, self.model = serial, "20GBD9901"
        self.geometry = Geometry(5, 3)
        self.brightness, self.firmware = 70, "1.0"
        self.profile, self.page_index, self.folder_stack = None, 0, []
        self.pages = []
        self.shown, self.shown_seq = {}, 0

    def candidate_profiles(self):
        return []

    def current_page(self):
        return types.SimpleNamespace(uuid=self.pages[self.page_index]) if self.pages else None

    def live_image(self, k):
        return self.shown.get(k)

    def load_profile(self, pid, page_index=None):
        pass

    def goto_page(self, idx):
        pass


def test_window_follows_deck_page_and_live_keys(tmp_path, monkeypatch):
    tk = pytest.importorskip("tkinter")
    if not os.environ.get("DISPLAY"):
        pytest.skip("kein Display (xvfb-run verwenden)")
    from freestream import editor
    from freestream.config import Config
    monkeypatch.setenv("FREESTREAM_NO_SETUP", "1")
    store = tmp_path / "profiles"
    store.mkdir()
    doc = E.ProfileDoc(E.create_profile("Test", "20GBD9901", store))
    doc.add_page()
    doc.place(doc.page_ids[1], ("key", 0, 0), "com.elgato.streamdeck.system.hotkey")
    doc.save()
    ctrl = DevCtrl("A1", store)
    ctrl.profile = types.SimpleNamespace(id=doc.id)
    ctrl.pages = list(doc.page_ids)
    cfg = Config()
    cfg.save = lambda: None
    daemon = types.SimpleNamespace(controllers={"a": ctrl}, cfg=cfg, plugin_host=None, store=None)
    root = tk.Tk()
    monkeypatch.setattr(editor, "DEVICE_WATCH_MS", 20)
    app = editor.EditorApp(root, store_root=store, daemon=daemon)
    try:
        def pump(n=15):
            for _i in range(n):
                root.update()
                time.sleep(0.02)
        pump()
        assert app.nav == [doc.page_ids[0]]
        ctrl.page_index = 1  # am Deck weitergeblättert
        pump()
        assert app.nav == [doc.page_ids[1]]
        # Live-Taste: das Fenster zeigt, was das Deck zeigt
        red = Image.new("RGBA", (72, 72), (255, 0, 0, 255))
        ctrl.shown[0] = red
        ctrl.shown_seq += 1
        pump()
        img = app._render(("key", 0, 0), app.doc.get(app.page_uuid, ("key", 0, 0)), (60, 60))
        assert img.getpixel((30, 30))[:3] == (255, 0, 0)
        ctrl.brightness = 30
        pump()
        assert app.nav == [doc.page_ids[1]]
    finally:
        root.destroy()


# ------------------------------------------------------------------ schnelle Mixer-Tasten
def _fake_pactl(monkeypatch, muted=("music_monitor",)):
    from freestream import mixer as M
    from freestream.config import Config
    mx = M.Mixer(Config())
    listing = "\n".join(
        f"Sink Input #{i}\n\tMute: {'yes' if f'{c}_{m}' in muted else 'no'}\n"
        f"\tVolume: front-left: 50000 /  60% / -7 dB\n\tProperties:\n\t\tlinewave.loop = \"{M.PREFIX}{c}_{m}\"\n"
        for i, (c, m) in enumerate((c["id"], m) for c in mx.channels() for m in mx.mix_ids()))
    calls = []

    def run_out(cmd, timeout=2):
        calls.append(cmd)
        return listing if cmd[:3] == ["pactl", "list", "sink-inputs"] else ""
    monkeypatch.setattr(system, "run_out", run_out)
    monkeypatch.setattr(M, "_run", lambda args: run_out(["pactl", *args]))
    monkeypatch.setattr(M.Mixer, "_duck", lambda self: None)
    return M.Mixer(Config()), calls  # erst jetzt: Mixer merkt sich _run beim Anlegen


def test_mute_and_volume_read_once(monkeypatch):
    mx, calls = _fake_pactl(monkeypatch)
    assert mx.set_mute("music", "both", "toggle") is True  # nicht alle stumm -> alle stumm
    lists = [c for c in calls if c[1] == "list"]
    sets = [c[1:] for c in calls if c[1] == "set-sink-input-mute"]
    assert len(lists) == 1 and len(sets) == len(mx.channel_mixes("music")) and all(s[-1] == "1" for s in sets)
    calls.clear()
    assert mx.change_volume("music", "both", 5, recent=True) == {m: 65 for m in mx.channel_mixes("music")}
    assert not [c for c in calls if c[1] == "list"]  # eben gelesen: gleich schreiben
    assert all(c[1] == "set-sink-input-volume" for c in calls) and len(calls) == len(mx.channel_mixes("music"))


def test_mixer_key_draws_known_state_without_asking_again(monkeypatch):
    from freestream import native
    from freestream.config import Config
    mx, calls = _fake_pactl(monkeypatch, muted=())
    ctx = types.SimpleNamespace(cfg=Config(), ctrl=None)
    a = native.MixerAction({"UUID": "com.lindeck.mixer.channel",
                            "Settings": {"channel": "music", "mix": "monitor", "action": "mute"}}, None, ctx)
    a.redraw = lambda: None
    a.tick()
    calls.clear()
    drawn = []
    a.redraw = lambda: drawn.append((a._level(), len(calls)))
    a.key_down()
    assert a._level() == (60, True)
    assert drawn[0] == ((60, True), 0)  # neuer Stand sofort gezeigt, noch bevor PipeWire etwas hört
    assert [c[1] for c in calls] == ["set-sink-input-mute"]  # gerade gelesen: nur schreiben


def test_mix_select_asks_pactl_once(monkeypatch):
    from freestream import native
    from freestream.config import Config
    mx, calls = _fake_pactl(monkeypatch)
    ctx = types.SimpleNamespace(cfg=Config(), ctrl=None)
    keys = {}
    for i, c in enumerate(mx.channels()):
        keys[i] = native.MixerAction({"UUID": "com.lindeck.mixer.channel",
                                      "Settings": {"channel": c["id"], "mix": "selected", "display": "tile"}}, None, ctx)
    sel = native.MixSelectAction({"UUID": "com.lindeck.mixer.mixselect", "Settings": {"mix": "stream"}}, None, ctx)
    keys[99] = sel
    order = []
    for k, a in keys.items():
        a.redraw = (lambda a=a: order.append(a))
    ctx.ctrl = types.SimpleNamespace(keys=keys, dials={})
    calls.clear()
    sel.key_down()
    assert native.deck_mix() == "stream"
    assert len([c for c in calls if c[1] == "list"]) == 1  # früher eine je Taste und Mix
    assert order[0] is sel  # die gedrückte Mix-Taste zuerst
    native._DECK_MIX["mix"] = "both"


def test_stale_loopback_index_is_read_again(monkeypatch):
    from freestream import mixer as M
    mx, calls = _fake_pactl(monkeypatch, muted=())
    mx.set_mute("music", "monitor", True, recent=True)  # liest und merkt sich die Indizes
    for b in M._RECENT["loops"].values():
        b["index"] = "999"  # Loopback inzwischen neu angelegt: alter Index gilt nicht mehr
    real = mx.run

    def run(args):
        if args[1:2] == ["999"]:
            raise RuntimeError("No such entity")
        return real(args)
    mx.run = run
    calls.clear()
    assert mx.set_mute("music", "monitor", False, recent=True) is False
    assert [c[1] for c in calls] == ["list", "set-sink-input-mute"]  # einmal frisch gelesen, dann geschrieben
