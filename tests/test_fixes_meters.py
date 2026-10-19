"""Live-Pegel nach dem Code-Review: Pegel nach dem Regler, parec neu starten, Mikrofon-Fader,
Neuzeichnen der Fader-Tasten ohne PipeWire-Abfragen und nur bei sichtbarer Änderung."""
import io
import threading
import time
import types

import pytest

from freestream import levels, mixkeys, native


def _wait(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


# ------------------------------------------------------------------ Pegel nach dem Regler
def test_post_fader_follows_cubic_volume():
    """PipeWire-Prozent sind kubisch: 50 % = −18 dB, 10 % = −60 dB (wie ducking.factor)."""
    assert levels.post_fader(0.8, 100, False) == 0.8
    assert levels.post_fader(0.8, 50, False) == pytest.approx(0.8 - 18.06 / 60, abs=1e-3)
    assert levels.post_fader(0.9, 30, False) == pytest.approx(0.9 - 31.37 / 60, abs=1e-3)
    assert levels.post_fader(1.0, 10, False) == pytest.approx(0.0, abs=1e-9)  # −60 dB: nichts mehr zu sehen
    assert levels.post_fader(0.5, 150, False) == pytest.approx(0.5 + 10.57 / 60, abs=1e-3)


# ------------------------------------------------------------------ parec: fest an der Quelle, neu starten
class FakeProc:
    def __init__(self, args, rc=None):
        self.args, self.rc, self.stdout = args, rc, io.BytesIO(b"")
        self.terminated = False

    def poll(self):
        return self.rc

    def terminate(self):
        self.terminated = True
        self.rc = -15


def test_parec_does_not_follow_the_default_source():
    """Wird der Kanal-Sink neu angelegt, soll parec enden statt still die Standardquelle zu messen."""
    started = []
    src = levels.ParecSource("freewave_music.monitor", exe="/usr/bin/parec",
                             popen=lambda args, **kw: started.append(FakeProc(args)) or started[-1])
    assert "--property=node.dont-reconnect=true" in started[0].args
    assert "--device=freewave_music.monitor" in started[0].args
    assert not src.ended
    started[0].rc = 1  # „Stream error: Entity killed“
    assert src.ended
    src.stop()
    assert not src.ended  # selbst beendet: nichts mehr neu zu starten


def test_parec_that_could_not_start_is_not_restarted():
    def fail(args, **kw):
        raise OSError("kein parec")
    src = levels.ParecSource("freewave_music.monitor", exe="/usr/bin/parec", popen=fail)
    assert src.proc is None and not src.ended


def test_ended_parec_restarts_with_pause(tmp_path):
    """Echter Prozess, der gleich endet (Quelle fehlt noch): neu gestartet, aber höchstens alle RESTART_S."""
    exe = tmp_path / "parec"
    exe.write_text("#!/bin/sh\necho 'Stream error: No such entity' >&2\nexit 1\n")
    exe.chmod(0o755)
    now = [100.0]
    made = []

    def factory(dev):
        made.append(levels.ParecSource(dev, exe=str(exe)))
        return made[-1]
    mon = levels.LevelMonitor(factory=factory, clock=lambda: now[0])
    targets = {"music": "freewave_music.monitor"}
    mon.watch(targets)
    assert _wait(lambda: made[0].ended)
    for _i in range(20):  # 12× pro Sekunde, aber noch keine RESTART_S vergangen
        mon.watch(targets)
    assert len(made) == 1
    now[0] += levels.RESTART_S
    mon.watch(targets)
    assert len(made) == 2 and made[0].proc is None
    assert mon.read() == {"music": 0.0}
    mon.stop()


class EndingSource:
    def __init__(self, device):
        self.device, self.level, self.ended, self.stopped = device, 0.5, False, False

    def stop(self):
        self.stopped = True


def test_long_running_source_restarts_at_once_when_it_ends():
    now = [0.0]
    made = []
    mon = levels.LevelMonitor(factory=lambda dev: made.append(EndingSource(dev)) or made[-1], clock=lambda: now[0])
    mon.watch({"music": "freewave_music.monitor"})
    now[0] = 600.0
    mon.watch({"music": "freewave_music.monitor"})
    assert len(made) == 1  # läuft noch: bleibt
    made[0].ended = True   # PipeWire neu gestartet
    mon.watch({"music": "freewave_music.monitor"})
    assert len(made) == 2 and made[0].stopped and not made[1].stopped
    assert mon.sources["music"][1] is made[1]


# ------------------------------------------------------------------ Deck: Mess-Thread
class FakeSource:
    def __init__(self, device):
        self.device, self.level = device, 0.9

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
    def __init__(self, channel="music", mixer=None):
        self.channel, self.mixer, self.got = channel, mixer or FakeMixer(), []

    def set_meter(self, raw):
        self.got.append(raw)


def _stopped(meters):
    return _wait(lambda: meters.thread is None)


def test_mic_fader_gets_a_level(monkeypatch):
    """Mikrofon-Fader (FreeWave-Profil mit allen Kanälen): gemessen wie im Fenster; Tasten ohne Quelle
    (leerer Platz) bekommen None – ihre Leiste folgt der Lautstärke."""
    from freestream import mixer as M
    monkeypatch.setattr(native.DeckMeters, "ENABLED", True)
    mx = M.Mixer(None)
    assert "mic" not in [c["id"] for c in mx.channels()]
    monkeypatch.setattr(mx, "_ctx", lambda st=None: {"mic": "alsa_input.usb-mic"})
    monkeypatch.setattr(mx, "level_sources", lambda: {})
    devices = []
    meters = native.DeckMeters(monitor_factory=lambda: levels.LevelMonitor(
        factory=lambda dev: devices.append(dev) or FakeSource(dev)))
    mic, empty = FakeFader("mic", mx), FakeFader("", mx)
    meters.add(mic)
    meters.add(empty)
    try:
        assert _wait(lambda: len(mic.got) >= 3 and len(empty.got) >= 3)
        assert mic.got[-1] == 0.9 and "alsa_input.usb-mic" in devices
        assert empty.got[-1] is None
    finally:
        meters.remove(mic)
        meters.remove(empty)
    assert _stopped(meters)


class FlakyMonitor(levels.LevelMonitor):
    def __init__(self):
        super().__init__(factory=FakeSource)
        self.fails = 2

    def watch(self, targets):
        if self.fails:
            self.fails -= 1
            raise RuntimeError("pactl weg")
        super().watch(targets)


def test_meter_thread_survives_errors(monkeypatch):
    monkeypatch.setattr(native.DeckMeters, "ENABLED", True)
    meters = native.DeckMeters(monitor_factory=FlakyMonitor)
    a = FakeFader()
    meters.add(a)
    try:
        assert _wait(lambda: a.got and a.got[-1] == 0.9)
        assert meters.thread is not None and meters.thread.is_alive()
    finally:
        meters.remove(a)
    assert _stopped(meters)


def test_meter_thread_that_died_is_started_again(monkeypatch):
    monkeypatch.setattr(native.DeckMeters, "ENABLED", True)
    monkeypatch.setattr(threading, "excepthook", lambda args: None)
    tries = []

    def factory():
        tries.append(1)
        if len(tries) == 1:
            raise RuntimeError("kaputt")
        return levels.LevelMonitor(factory=FakeSource)
    meters = native.DeckMeters(monitor_factory=factory)
    a = FakeFader()
    meters.add(a)
    assert _stopped(meters)  # gestorben – aber nicht als „läuft noch“ gemerkt
    meters.add(a)
    try:
        assert _wait(lambda: a.got and a.got[-1] == 0.9)
    finally:
        meters.remove(a)
    assert _stopped(meters)


class HookLock:
    """Lock, der nach dem ersten Freigeben einmal hook() aufruft (zwischen zwei Zeilen eines Threads)."""

    def __init__(self, hook):
        self._lock, self.hook = threading.Lock(), hook

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()
        hook, self.hook = self.hook, None
        if hook:
            hook()


class StopCounter:
    def __init__(self):
        self.stopped = 0

    def stop(self):
        self.stopped += 1


def test_ending_meter_thread_stops_only_its_own_monitor():
    """Seite weg und gleich wieder da: der alte Thread endet, während der neue schon misst – er darf
    nicht die parec-Prozesse des neuen beenden (und seine eigenen nicht weiterlaufen lassen)."""
    old, new = StopCounter(), StopCounter()
    meters = native.DeckMeters(monitor_factory=lambda: old)

    def new_thread_starts():
        meters.monitor = new
    meters.lock = HookLock(new_thread_starts)
    meters._loop()  # keine Fader mehr: endet sofort
    assert old.stopped == 1 and new.stopped == 0


# ------------------------------------------------------------------ Deck: Neuzeichnen der Fader-Tasten
def _fader_action(monkeypatch, display="fader_bottom", vol=80):
    gets, drawn = [], []
    fake = types.SimpleNamespace(channel=lambda c: {"id": c, "name": c.title()}, mix=lambda m: {"name": m},
                                 mix_ids=lambda: ["monitor", "stream"], channel_mixes=lambda c: ["monitor"],
                                 get=lambda c, m: gets.append((c, m)) or (vol, False))
    monkeypatch.setattr(native.MixerAction, "mixer", property(lambda self: fake))
    ctrl = types.SimpleNamespace(preview=False, keys={}, dials={})
    ctrl.redraw_action = lambda a: drawn.append(a.frames((72, 72)))
    a = native.MixerAction({"UUID": "com.lindeck.mixer.channel",
                            "Settings": {"channel": "music", "mix": "monitor", "display": display}},
                           None, types.SimpleNamespace(ctrl=ctrl, cfg=None))
    return a, gets, drawn


def test_meter_redraw_does_not_ask_pipewire(monkeypatch):
    """12× pro Sekunde neu zeichnen (unter der Deck-Sperre) darf keine pactl-Abfrage starten – die
    Lautstärke liest der Sekundentakt, einmal für alle Tasten."""
    a, gets, drawn = _fader_action(monkeypatch)
    assert a._level() == (80, False) and len(gets) == 1
    a._lv_at = 0  # Stand älter als eine Sekunde
    for raw in (0.2, 0.5, 0.9, 0.1):
        a.set_meter(raw)
    assert len(drawn) == 4 and len(gets) == 1
    assert not getattr(native._METER, "drawing", False)
    assert a.active() is True and len(gets) == 2  # Sekundentakt: liest nach


def test_meter_redraw_flag_reset_after_error(monkeypatch):
    a, gets, _drawn = _fader_action(monkeypatch)
    a._lv = (80, False)

    def boom():
        raise RuntimeError("USB weg")
    a.redraw = boom
    with pytest.raises(RuntimeError):
        a.set_meter(0.9)
    assert not getattr(native._METER, "drawing", False)
    a._level()
    assert len(gets) == 1


def test_channel_without_measurement_follows_volume(monkeypatch):
    a, _gets, drawn = _fader_action(monkeypatch)
    a._lv = (80, False)
    a.set_meter(0.9)
    assert a._live is not None and len(drawn) == 1
    a.set_meter(None)
    assert a._live is None and len(drawn) == 2  # Leiste wieder nach der Lautstärke
    a.set_meter(None)
    assert len(drawn) == 2


@pytest.mark.parametrize("half,lit", [("top", range(7, 18)), ("bottom", range(0, 9))])
def test_fader_redraws_only_when_its_half_changes(monkeypatch, half, lit):
    """Ein Pegel von 0 bis 100 %: jede Taste wird genau so oft gezeichnet, wie sie verschiedene Bilder zeigt."""
    a, _gets, drawn = _fader_action(monkeypatch, display="fader_" + half, vol=100)
    a._lv = (100, False)
    for i in range(101):
        a.set_meter(i / 100)
    assert len(drawn) == len(lit)
    assert len({d[0][0].tobytes() for d in drawn}) == len(lit)


@pytest.mark.parametrize("size", [(72, 72), (80, 80), (96, 96), (120, 120)])
def test_fader_live_rounding_keeps_the_image(size):
    """Gerundet wie die Hälfte ihn zeigt: dasselbe Bild wie mit dem genauen Pegel."""
    args = ("music", "#22c55e", 60, False, "monitor")
    for half in ("top", "bottom"):
        for i in range(0, 69):
            live = i / 68
            exact = mixkeys.fader(size, half, *args, label="Alle", live=live)
            rounded = mixkeys.fader(size, half, *args, label="Alle", live=mixkeys.fader_live(half, live))
            assert exact.tobytes() == rounded.tobytes(), (half, live)
    assert mixkeys.fader_live("top", None) is None
