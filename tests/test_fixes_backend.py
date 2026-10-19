"""Fehler aus dem Code-Review (Mixer, pactl, Effekte, Ducking) – je ein Test, der den Fehler festhält."""
import os
import subprocess
import sys
import threading
import time
import types

import test_waveapp
from test_waveapp import FakePactl, _config

env = test_waveapp.env  # Fixtures
pactl = test_waveapp.pactl


def _channels(mx):
    return {s["app"]: s["channel"] for s in mx.streams()}


# ==========================================================================
# pactl immer englisch (pactl-locale)
# ==========================================================================
GERMAN_PACTL = """#!/bin/sh
# antwortet wie ein deutsches pactl – englisch nur mit LC_ALL=C
if [ "$LC_ALL" = "C" ] && [ -z "$LANGUAGE" ]; then en=1; else en=; fi
case "$1" in
  list)
    if [ -n "$en" ]; then
      printf 'Sink Input #87\\n\\tSink: 39\\n\\tMute: yes\\n\\tVolume: front-left: 52429 /  80%% / -5.81 dB\\n'
      printf '\\tProperties:\\n\\t\\tlinewave.loop = "linewave_music_monitor"\\n'
    else
      printf 'Ziel-Eingabe #87\\n\\tZiel: 39\\n\\tStumm: ja\\n\\tLautst\\303\\244rke: front-left: 52429 /  80%% / -5,81 dB\\n'
      printf '\\tEigenschaften:\\n\\t\\tlinewave.loop = "linewave_music_monitor"\\n'
    fi ;;
  get-sink-volume) echo "Volume: front-left: 52429 /  80% / -5,81 dB" ;;
  get-sink-mute) if [ -n "$en" ]; then echo "Mute: yes"; else echo "Stumm: ja"; fi ;;
  subscribe)
    if [ -n "$en" ]; then echo "Event 'new' on sink-input #5"; else echo "Ereignis »neu« auf Ziel-Eingabe #5"; fi
    sleep 5 ;;
esac
"""


def test_pactl_output_is_read_in_english_on_a_german_desktop(tmp_path, monkeypatch):
    from freestream import mixer, system
    fake = tmp_path / "pactl"
    fake.write_text(GERMAN_PACTL)
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    monkeypatch.setenv("LANGUAGE", "de")
    monkeypatch.delenv("LC_ALL", raising=False)
    b = system._pactl_blocks("sink-inputs")[0]
    assert (b["index"], b.get("sink"), b.get("volume"), b.get("mute")) == ("87", "39", 80, True)
    assert system.device_volume("sink", "x") == (80, True)
    assert mixer._run(["get-sink-mute", "x"]) == "Mute: yes"
    w = system.PulseWatch().start()
    try:
        end = time.time() + 3
        while w.new_streams < 1 and time.time() < end:
            w.wait(w.version, 0.2)
        assert w.new_streams == 1  # Ereignisse werden erkannt
    finally:
        w.stop()
    assert os.environ["LANGUAGE"] == "de"  # nur pactl bekommt die eigene Umgebung


# ==========================================================================
# Schnappschuss zerlegt jede Liste nur einmal (parse-per-call)
# ==========================================================================
LOOP = ('Sink Input #42\n\tSink: 5\n\tMute: no\n\tVolume: front-left: 52429 /  80% / -5.81 dB\n'
        '\tProperties:\n\t\tlinewave.loop = "linewave_music_monitor"\n')


def test_snapshot_parses_each_listing_once(env, monkeypatch):
    from freestream import mixer, system
    calls, parsed = [], []
    monkeypatch.setattr(system, "run_out", lambda cmd, timeout=2: calls.append(cmd) or LOOP)
    parse = system._parse_blocks
    monkeypatch.setattr(system, "_parse_blocks", lambda out: parsed.append(1) or parse(out))
    mx = mixer.Mixer({})
    with system.pactl_snapshot():
        for _i in range(21):  # FreeWave-Seite am Deck: alle Tasten im Sekundentakt
            assert mx.get("music", "monitor") == (80, False)
        assert system._pactl_blocks("sink-inputs") is system._pactl_blocks("sink-inputs")
    assert len(calls) == 1 and len(parsed) == 1
    assert mx.get("music", "monitor") == (80, False)  # außerhalb: immer frisch
    assert len(calls) == 2 and len(parsed) == 2


# ==========================================================================
# Kanal neu aufbauen behält seine Programme (rebuild-dumps-apps)
# ==========================================================================
class MovingPactl(FakePactl):
    """Wie PipeWire: verschwindet ein Sink, landen seine Streams auf dem Standardgerät."""

    def __init__(self):
        super().__init__()
        self.default = "alsa_output.headphones"

    def __call__(self, args):
        if args[0] == "set-default-sink":
            self.default = args[1]
        if args[0] == "unload-module" and any(s["index"] == args[1] for s in self.sinks):
            for b in self.inputs.values():
                if b.get("sink") == args[1]:
                    b["sink"] = self._sink(self.default)
        return super().__call__(args)


def test_rebuild_channel_keeps_its_apps(env, monkeypatch):
    from freestream import mixer, system
    fake = MovingPactl()
    monkeypatch.setattr(system, "_pactl_blocks", fake.blocks)
    monkeypatch.setattr(system, "have", lambda c: True)
    monkeypatch.setattr(mixer.time, "sleep", lambda s: None)
    mx = mixer.Mixer(_config(env, {"mixer": {"enabled": True}}), run=fake)
    mx.setup()
    assert fake.default == "linewave_system"
    mx.route_streams()
    mx.move_stream("8", "system")
    assert _channels(mx) == {"Spotify": "music", "Mein Spiel": "system"}
    mx.rebuild_channel("music")  # neue Effekte, umbenannt, abgestürzte Effektkette
    assert _channels(mx) == {"Spotify": "music", "Mein Spiel": "system"}
    mx.rebuild_channel("system")
    assert _channels(mx) == {"Spotify": "music", "Mein Spiel": "system"}
    mx.remove_channel("music")  # Löschen: Programme gehen bewusst aufs Standardgerät
    assert _channels(mx)["Spotify"] == "system"


# ==========================================================================
# eigene Zuordnungen vor den eingebauten (user-route-loses)
# ==========================================================================
def test_own_routes_win_over_builtin_ones(env, pactl):
    from freestream.mixer import Mixer
    pactl.inputs.update({
        "9": {"index": "9", "sink": "1", "volume": 80, "mute": False,
              "props": {"application.name": "Firefox", "application.process.binary": "firefox-esr"}},
        "10": {"index": "10", "sink": "1", "volume": 80, "mute": False,
               "props": {"application.name": "Chromium", "application.process.binary": "chromium"}},
        "11": {"index": "11", "sink": "1", "volume": 80, "mute": False,
               "props": {"application.name": "Brave", "application.process.binary": "brave-browser"}}})
    cfg = _config(env, {"mixer": {"enabled": True, "routes": {"firefox-esr": "music", "brave": "sfx",
                                                              "brave-browser": "game"}}})
    mx = Mixer(cfg)
    mx.setup()
    mx.route_streams()
    chans = _channels(mx)
    assert chans["Firefox"] == "music"   # eigene vor eingebauter („firefox“ → Browser)
    assert chans["Brave"] == "game"      # genauere eigene vor kürzerer
    assert chans["Chromium"] == "browser" and chans["Spotify"] == "music"  # eingebaute gelten weiter


# ==========================================================================
# _WRITTEN/_WATCH aus mehreren Threads (written-unsynchronized)
# ==========================================================================
def test_written_values_survive_parallel_threads():
    from freestream import mixer
    errors, stop = [], time.monotonic() + 0.4
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # häufige Thread-Wechsel: das Zeitfenster sicher treffen

    def writer():
        i = 0
        while time.monotonic() < stop:
            mixer._note_write(["set-sink-input-volume", str(1000 + i % 40), f"{i % 100}%"])
            i += 1

    def reader():
        blocks = [{"index": str(1000 + k), "volume": 1} for k in range(20)]
        while time.monotonic() < stop:
            try:
                mixer._apply_written(blocks, asked_at=time.monotonic() - 0.001)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
    try:
        threads = [threading.Thread(target=f) for f in (writer, writer, reader)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(old)
    assert errors == []


def test_loops_watcher_starts_once(monkeypatch):
    from freestream import mixer
    started = []

    class SlowThread:  # zwischen Prüfen und Eintragen vergeht Zeit
        def __init__(self, target, name, daemon):
            time.sleep(0.05)

        def start(self):
            started.append(self)
    monkeypatch.setattr(mixer, "threading", types.SimpleNamespace(Thread=SlowThread))
    monkeypatch.setattr(mixer, "WATCH", True)
    monkeypatch.setattr(mixer, "_WATCH", {"wanted": True})
    decks = [threading.Thread(target=mixer._watch_loops) for _i in range(2)]  # zwei Decks, je ein Engine-Thread
    for t in decks:
        t.start()
    for t in decks:
        t.join()
    assert len(started) == 1


def test_deck_repeat_uses_written_value_over_stale_recent_query(env, pactl):
    from freestream import mixer
    mx = mixer.Mixer(_config(env, {"mixer": {"enabled": True}}))
    mx.setup()
    mx.set_volume("music", "monitor", 50)
    assert mx.get("music", "monitor") == (50, False)  # Deck-Taste liest: Loopbacks gemerkt
    idx = mx.raw_levels(with_index=True)["music_monitor"][2]
    # Beobachter-Thread: Abfrage gestellt vor unserem Schreiben, gemerkt danach (alter Wert 80)
    mixer._RECENT["loops"]["linewave_music_monitor"]["volume"] = 80
    mixer._wrote(idx, volume=50)
    assert mx.change_volume("music", "monitor", -5, recent=True) == {"monitor": 45}


# ==========================================================================
# Warten auf die Effekt-Quelle im Schnappschuss (fx-poll-in-snapshot)
# ==========================================================================
def _sources(calls, node, after):
    def run_out(cmd, timeout=2):
        calls.append(cmd)
        out = "Source #1\n\tName: alsa_input.mic\n"
        return out + (f"Source #2\n\tName: {node}\n" if len(calls) >= after else "")
    return run_out


def test_source_exists_asks_again_inside_snapshot(monkeypatch):
    from freestream import system
    calls = []
    monkeypatch.setattr(system, "run_out", _sources(calls, "linewave_music_fx", 3))
    with system.pactl_snapshot():
        assert not any(b["name"] == "linewave_music_fx" for b in system._pactl_blocks("sources"))
        assert [system.source_exists("linewave_music_fx") for _i in range(3)] == [False, True, True]
        # danach sehen auch die übrigen Abfragen im Schnappschuss die neue Quelle
        assert any(b["name"] == "linewave_music_fx" for b in system._pactl_blocks("sources"))
    assert len(calls) == 4


def test_effects_start_inside_snapshot_does_not_wait_full_timeout(env, monkeypatch):
    from freestream import effects, mixer, system
    hp = effects.lookup("builtin:highpass")
    mx = mixer.Mixer(_config(env, {"mixer": {"enabled": True, "effects": {"music": [effects.new_effect(hp)]}}}),
                     run=FakePactl())
    calls, sleeps = [], []
    monkeypatch.setattr(system, "run_out", _sources(calls, mx.fx.node_name("music"), 3))
    monkeypatch.setattr(effects.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "pipewire" else None)
    monkeypatch.setattr(effects.time, "sleep", lambda s: sleeps.append(s))
    real_popen = subprocess.Popen

    def popen(args, *a, **kw):
        if args[0] != "/usr/bin/pipewire":
            return real_popen(args, *a, **kw)
        return types.SimpleNamespace(pid=2 ** 22 + 7, poll=lambda: None)  # Filter-Prozess läuft
    monkeypatch.setattr(effects.subprocess, "Popen", popen)
    c = mx.channel("music")
    with system.pactl_snapshot():  # wie WaveHost.tick → _watch_effects
        system._pactl_blocks("sources")
        assert mx.fx.start(c, "linewave_music.monitor") == mx.fx.node_name("music")
    assert len(sleeps) == 1  # die Quelle kam beim zweiten Nachsehen, nicht erst nach 3 s


# ==========================================================================
# FreeDeck-Tasten während des Duckings (duck-stale-base)
# ==========================================================================
def test_deck_presses_while_ducking_use_the_new_base(env, pactl):
    from freestream.ducking import Ducker
    from freestream.mixer import Mixer
    cfg = _config(env, {"mixer": {"enabled": True, "ducking": {"enabled": True, "channels": ["music"],
                                                                "amount_db": 12}}})
    host = Mixer(cfg)
    host.setup()
    host.set_volume("music", "monitor", 80)
    d = Ducker(host, cfg, level=lambda: 1.0)  # FreeWave-Host: es wird gesprochen
    for _i in range(3):
        d.step(0.2)
    assert d.ducking and d.base("music_monitor") == 80
    deck = Mixer(cfg)  # FreeDeck: anderer Prozess, liest ducking.json
    got = []
    for _i in range(4):  # Taste gehalten: Wiederholung alle 0,15 s, der Host läuft dazwischen
        got.append(deck.change_volume("music", "monitor", -5, recent=True)["monitor"])
        d.step(0.04)
    assert got == [75, 70, 65, 60] and d.base("music_monitor") == 60
    assert deck.get("music", "monitor")[0] == 60  # Anzeige am Deck: der Nutzerwert
