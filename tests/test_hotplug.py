"""Geräte im laufenden Betrieb an- und abstecken (simulierte Geräte)."""
import pytest

from freestream import editdoc as E


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    pytest.importorskip("StreamDeck")
    import StreamDeck.Transport.Dummy as dummy

    from freestream import paths
    from freestream.config import Config, State
    from freestream.daemon import Daemon
    from freestream.profiles import ProfileStore
    monkeypatch.setattr(dummy.Dummy.Device, "read", lambda self, length: None)  # wie echte Geräte ohne Eingabe
    monkeypatch.setattr(paths, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(paths, "STATE_FILE", tmp_path / "state.json")
    store = tmp_path / "profiles"
    store.mkdir()
    doc = E.ProfileDoc(E.create_profile("Plus", "20GBD9901", store))
    doc.place(doc.page_ids[0], ("key", 0, 0), "com.elgato.streamdeck.system.hotkey")
    doc.save()
    d = Daemon.__new__(Daemon)
    d.cfg, d.state, d.store = Config(), State(), ProfileStore(store)
    d.state.save = lambda: None
    d.vin = d.sound = d.plugin_host = None
    d.transport, d.controllers, d.running = "dummy", {}, True
    d._last_window, d._smart_prev = "", {}
    yield d
    for c in list(d.controllers.values()):
        c.detach()


def test_unplug_unloads_actions(daemon):
    daemon.scan()
    plus = next(c for c in daemon.controllers.values() if c.model == "20GBD9901")
    assert plus.keys, "Profil geladen"
    gone = []
    for a in plus.keys.values():
        a.will_disappear = lambda a=a: gone.append(a)
    did = next(k for k, c in daemon.controllers.items() if c is plus)
    plus.deck.connected = lambda: False
    daemon.scan()
    assert did not in daemon.controllers
    assert gone and not plus.keys and not plus.running
    plus._ticker.join(2)
    assert not plus._ticker.is_alive()  # Takt beendet sich sofort


def test_tick_sleeps_without_animation(daemon):
    """Ohne Animation wacht der Takt höchstens einmal pro Sekunde auf."""
    import time
    daemon.scan()
    ctrl = next(iter(daemon.controllers.values()))
    calls = []
    orig = ctrl._second_tick
    ctrl._second_tick = lambda now: (calls.append(now), orig(now))
    ctrl.anim.clear()
    time.sleep(2.5)
    assert 1 <= len(calls) <= 4
