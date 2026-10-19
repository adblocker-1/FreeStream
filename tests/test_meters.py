"""Live-Pegel: im FreeWave-Fenster unter den Reglern, auf dem Deck in der Leiste der Fader-Tasten."""
import threading
import time

from freestream import levels, mixkeys, native


def test_post_fader():
    assert levels.post_fader(0.8, 100, False) == 0.8
    assert levels.post_fader(0.8, 100, True) == 0.0
    assert levels.post_fader(0.8, None, False) == 0.0
    assert 0.0 < levels.post_fader(0.8, 50, False) < 0.8   # leiser gestellt: weniger Pegel


def test_fader_key_shows_live_level():
    """Mit Messung leuchtet die Leiste nach dem Pegel (oben gelb/rot), nicht nach der Lautstärke."""
    size = (72, 72)
    loud = mixkeys.fader(size, "bottom", "music", "#22c55e", 20, False, "monitor", live=1.0)
    quiet = mixkeys.fader(size, "bottom", "music", "#22c55e", 20, False, "monitor", live=0.0)
    by_vol = mixkeys.fader(size, "bottom", "music", "#22c55e", 20, False, "monitor")
    assert loud.tobytes() != quiet.tobytes() != by_vol.tobytes()
    top_loud = mixkeys.fader(size, "top", "music", "#22c55e", 20, False, "monitor", live=1.0)
    px = [top_loud.getpixel((x, y))[:3] for x in range(44, 56) for y in range(10, 30)]
    assert mixkeys.RED in px or any(p[0] > 200 and p[1] < 110 for p in px)  # Spitze rot


class FakeSource:
    def __init__(self, device):
        self.level = 0.9

    def stop(self):
        pass


class FakeMixer:
    def channels(self):
        return [{"id": "music"}]

    def all_channels(self):
        return self.channels()

    def sink_name(self, cid):
        return "freewave_" + cid

    def _ctx(self):
        return {}

    def level_sources(self):
        return {}


class FakeFader:
    mixer = FakeMixer()

    def __init__(self):
        self.channel, self.got, self.done = "music", [], threading.Event()

    def set_meter(self, raw):
        self.got.append(raw)
        if len(self.got) >= 3:
            self.done.set()


def test_deck_meters_feed_visible_faders_and_stop(monkeypatch):
    monkeypatch.setattr(native.DeckMeters, "ENABLED", True)
    meters = native.DeckMeters(monitor_factory=lambda: levels.LevelMonitor(factory=FakeSource))
    a = FakeFader()
    meters.add(a)
    assert a.done.wait(2)
    assert a.got[-1] == 0.9
    meters.remove(a)
    for _i in range(50):
        if meters.thread is None:
            break
        time.sleep(0.05)
    assert meters.thread is None  # keine Fader mehr sichtbar: Messung beendet


def test_mixer_fader_redraws_only_on_visible_change(monkeypatch):
    act = native.MixerAction.__new__(native.MixerAction)
    act.uuid, act.settings = "com.lindeck.mixer.channel", {"display": "fader_top"}
    act._lv = (100, False)
    drawn = []
    monkeypatch.setattr(act, "redraw", lambda: drawn.append(act._live), raising=False)
    act.set_meter(0.5)
    act.set_meter(0.501)   # gleiche Segmentzahl: nicht neu zeichnen
    act.set_meter(0.9)
    assert drawn == [mixkeys.fader_live("top", 0.5), mixkeys.fader_live("top", 0.9)]


def test_window_meter_green_yellow_red():
    """Pegel im FreeWave-Fenster: grün, ab 70 % gelb, ab 90 % rot."""
    import os

    import pytest
    if not os.environ.get("DISPLAY"):
        pytest.skip("braucht X (xvfb-run)")
    import tkinter as tk

    from freestream.theme import METER_ZONES, LevelMeter
    root = tk.Tk()
    try:
        m = LevelMeter(root, length=200, thickness=5, orient="horizontal")
        m.pack()
        m.set(0.95)
        root.update()
        fills = [m.itemcget(i, "fill") for i in m.find_all()]
        assert [z[2] for z in METER_ZONES] == [f for f in fills if f in [z[2] for z in METER_ZONES]]
        m.set(0.5)
        fills = [m.itemcget(i, "fill") for i in m.find_all()]
        assert METER_ZONES[1][2] not in fills and METER_ZONES[2][2] not in fills
    finally:
        root.destroy()
