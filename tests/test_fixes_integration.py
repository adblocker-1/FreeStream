"""Fehler aus der Integrations-Prüfung – je Bereich ein Block, je Test der festgehaltene Fehler."""
import json
import time
import types

import pytest

import test_waveapp
from test_waveapp import _config

env = test_waveapp.env  # Fixtures
pactl = test_waveapp.pactl


# ==========================================================================
# Ducking: FreeDeck-Tasten auf allen Mixen, Wiederholung nach veraltetem Index (test_duck_)
# ==========================================================================
def _duck_pactl(fake, broken=()):
    """pactl wie PipeWire: Schreiben auf einen unbekannten (oder hängenden) Index schlägt fehl."""
    def run(args):
        if args[0] in ("set-sink-input-volume", "set-sink-input-mute") and (
                args[1] not in fake.inputs or args[1] in broken):
            raise RuntimeError("Failure: No such entity")
        return fake(args)
    return run


def _duck_index(fake, key):
    return next(i for i, b in fake.inputs.items() if b["props"].get("linewave.loop") == "linewave_" + key)


def _duck_reload(fake, key):
    """FreeWave hat den Loopback neu geladen: gleicher Name, neuer Index."""
    b = fake.inputs.pop(_duck_index(fake, key))
    fake.next_id += 1
    b["index"] = str(fake.next_id)
    fake.inputs[b["index"]] = b


def _duck_request(env):
    from freestream.mixer import DUCK_USER_FILE
    return env / "state" / DUCK_USER_FILE


def _duck_host(env, pactl, broken=()):
    """FreeWave-Host senkt Musik ab (beide Mixe auf 60); dazu FreeDeck als eigener Mixer (über ducking.json)."""
    from freestream.ducking import Ducker
    from freestream.mixer import Mixer
    cfg = _config(env, {"mixer": {"enabled": True, "ducking": {"enabled": True, "channels": ["music"],
                                                                "amount_db": 12}}})
    run = _duck_pactl(pactl, broken)
    host = Mixer(cfg, run=run)
    host.setup()
    host.set_volume("music", "both", 60)
    d = Ducker(host, cfg, level=lambda: 1.0)  # es wird gesprochen
    for _i in range(3):
        d.step(0.2)
    assert d.ducking and d.base("music_monitor") == d.base("music_stream") == 60
    return host, Mixer(cfg, run=run), d


def test_duck_key_on_all_mixes_records_every_mix(env, pactl):
    from freestream import mixer
    deck = mixer.Mixer(_config(env, {"mixer": {"enabled": True}}))
    deck.setup()
    req = _duck_request(env)
    for _i in range(20):  # je Tastendruck schreiben die Threads je Mix gleichzeitig
        req.unlink(missing_ok=True)
        (env / "state" / mixer.DUCK_FILE).write_text(json.dumps(
            {"time": time.time(), "factor": 0.5, "bases": {"music_monitor": 60, "music_stream": 60}}))
        assert deck.change_volume("music", "both", -10, recent=True) == {"monitor": 50, "stream": 50}
        assert json.loads(req.read_text()) == {"music_monitor": 50, "music_stream": 50}
    raw = deck.raw_levels()
    assert raw["music_monitor"][0] == raw["music_stream"][0] == 25  # abgesenkt geschrieben


def test_duck_host_takes_a_deck_press_on_all_mixes(env, pactl):
    from freestream.mixer import DUCK_FILE, duck_volume
    host, deck, d = _duck_host(env, pactl)
    state = env / "state" / DUCK_FILE
    # ducking.json schreibt der Host nur einmal pro Sekunde: FreeDeck rechnet mit einem älteren Faktor
    state.write_text(json.dumps({**json.loads(state.read_text()), "factor": 0.9}))
    assert deck.change_volume("music", "both", -10, recent=True) == {"monitor": 50, "stream": 50}
    d.step(0.04)
    assert d.base("music_monitor") == d.base("music_stream") == 50
    raw = host.raw_levels()
    assert raw["music_monitor"][0] == raw["music_stream"][0] == duck_volume(50, d.factor)


def test_duck_retry_after_stale_index_steps_each_mix_once(env, pactl):
    from freestream.mixer import Mixer
    deck = Mixer(_config(env, {"mixer": {"enabled": True}}), run=_duck_pactl(pactl))
    deck.setup()
    deck.set_volume("music", "both", 60)
    assert deck.get("music", "monitor") == (60, False)  # Deck-Takt: Loopbacks gemerkt
    _duck_reload(pactl, "music_monitor")  # nur der Persönliche Mix hat einen neuen Index
    assert deck.change_volume("music", "both", -5, recent=True) == {"monitor": 55, "stream": 55}
    raw = deck.raw_levels()
    assert raw["music_monitor"][0] == raw["music_stream"][0] == 55


def test_duck_retry_while_ducking_keeps_the_base(env, pactl):
    host, deck, d = _duck_host(env, pactl)
    assert deck.get("music", "monitor") == (60, False)  # Deck-Takt: Loopbacks gemerkt
    _duck_reload(pactl, "music_monitor")
    assert deck.change_volume("music", "monitor", -5, recent=True) == {"monitor": 55}
    assert json.loads(_duck_request(env).read_text()) == {"music_monitor": 55}
    d.step(0.04)
    assert d.base("music_monitor") == 55


def test_duck_failed_write_is_not_recorded(env, pactl):
    broken = set()
    host, deck, d = _duck_host(env, pactl, broken)
    broken.add(_duck_index(pactl, "music_monitor"))  # Loopback hängt: jeder Schreibversuch scheitert
    with pytest.raises(RuntimeError):
        deck.change_volume("music", "both", -5, recent=True)
    assert json.loads(_duck_request(env).read_text()) == {"music_stream": 55}  # nur der geschriebene Mix
    d.step(0.04)
    assert d.base("music_monitor") == 60 and d.base("music_stream") == 55


def test_duck_request_written_while_host_reads_is_kept(env, pactl, monkeypatch):
    from freestream import ducking
    host, deck, d = _duck_host(env, pactl)
    req = _duck_request(env)
    req.write_text(json.dumps({"music_monitor": 50}))
    loads = json.loads

    def reading(text):
        out = loads(text)
        if out == {"music_monitor": 50}:  # FreeDeck meldet den nächsten Wert, während der Host noch liest
            req.write_text(json.dumps({"music_stream": 40}))
        return out
    monkeypatch.setattr(ducking, "json", types.SimpleNamespace(loads=reading, dumps=json.dumps))
    d.step(0.04)
    d.step(0.04)
    assert d.base("music_monitor") == 50 and d.base("music_stream") == 40
