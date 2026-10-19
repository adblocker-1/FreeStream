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


# ==========================================================================
# test_wizard_: Einrichtungsassistent von FreeWave (wavesetup) – erneut aufgerufen bleibt der Persönliche Mix
# auf seinem Gerät; nach „Später einrichten“ zeigt er beim Öffnen aus den Einstellungen die Empfehlung
# ==========================================================================
import json as _wizard_json  # noqa: E402
import os as _wizard_os  # noqa: E402
import types as _wizard_types  # noqa: E402

import pytest as _wizard_pytest  # noqa: E402

import test_waveapp  # noqa: E402

env = test_waveapp.env  # Fixtures
pactl = test_waveapp.pactl

_wizard_needs_x = _wizard_pytest.mark.skipif(not _wizard_os.environ.get("DISPLAY"), reason="braucht X (xvfb-run)")
_WIZARD_CAT = [{"id": "ladspa:gate_1410.so:gate", "label": "gate", "name": "Gate"},
               {"id": "ladspa:sc4m_1916.so:sc4m", "label": "sc4m", "name": "Kompressor"},
               {"id": "ladspa:fast_lookahead_limiter_1913.so:fastLookaheadLimiter",
                "label": "fastLookaheadLimiter", "name": "Limiter"}]
_WIZARD_SYNC = _wizard_types.SimpleNamespace(submit=lambda fn, done=None: fn())  # Arbeits-Thread ohne Thread


def _wizard_saved(env):
    return _wizard_json.loads((env / "config" / "config.json").read_text())


def _wizard_monitor_sinks(pactl):
    """Sinks, auf denen die Loopbacks des Persönlichen Mix spielen."""
    return {b["sink"] for b in pactl.inputs.values() if b["props"].get("linewave.loop", "").endswith("_monitor")}


def _wizard_default_sink(monkeypatch, pactl, sink):
    """Standard-Sink wie bei PipeWire: setup() stellt ihn auf den eigenen Kanal (linewave_*)."""
    from freestream import mixer
    cur = [sink]

    def run(args):
        if args[0] == "get-default-sink":
            pactl.cmds.append(args)
            return cur[0]
        if args[0] == "set-default-sink":
            cur[0] = args[1]
        return pactl(args)
    monkeypatch.setattr(mixer, "_run", run)
    return cur


def _wizard_finish(root, wiz):
    """Bis „Kanäle“ durchklicken, ohne etwas umzuschalten, und die Auswahl liefern."""
    for _ in range(4):
        wiz.next()
        root.update()
    return wiz.choices()


@_wizard_needs_x
def test_wizard_rerun_keeps_live_monitor_output(env, pactl):
    """Ausgabe später im Fenster/am Deck umgestellt (nur der Stand des Mixers): der Assistent zeigt dieses Gerät,
    und „Fertig“ ohne Umschalten lässt den Persönlichen Mix dort."""
    import tkinter as tk

    from freestream import wavesetup
    from freestream.wavehost import WaveHost
    host = WaveHost(test_waveapp._config(env, {"linewave": {"setup_done": True}, "mixer": {
        "enabled": True, "mic": "alsa_input.usb-capture", "monitor_output": "alsa_output.speakers"}}))
    host.mixer.setup()
    assert _wizard_monitor_sinks(pactl) == {pactl._sink("alsa_output.speakers")}
    host.mixer.set_monitor_output("alsa_output.headphones")  # Ausgabe-Wahl im Fenster, Deck-Taste, CLI
    assert host.mixer._state()["monitor_output"] == "alsa_output.headphones"
    assert _wizard_saved(env)["mixer"]["monitor_output"] == "alsa_output.speakers"
    data = wavesetup.collect(host)
    assert data["monitor_output"] == "alsa_output.headphones"
    root = tk.Tk()
    try:
        wiz = wavesetup.SetupWizard(_wizard_types.SimpleNamespace(root=root, host=host, worker=_WIZARD_SYNC), data)
        assert wiz.out.get() == "alsa_output.headphones"
        ch = _wizard_finish(root, wiz)
        assert ch["output"] == "alsa_output.headphones"
        wavesetup.apply_choices(host, ch)
        wiz.destroy()
    finally:
        root.destroy()
    assert host.mixer._state()["monitor_output"] == "alsa_output.headphones"
    assert _wizard_saved(env)["mixer"]["monitor_output"] == "alsa_output.headphones"
    assert _wizard_monitor_sinks(pactl) == {pactl._sink("alsa_output.headphones")}


@_wizard_needs_x
def test_wizard_rerun_without_configured_output_keeps_live_one(env, pactl, monkeypatch):
    """Mixer ohne Assistent eingeschaltet (kein monitor_output in config.json) und der Standard-Sink ist der
    eigene linewave_*: der Assistent zeigt das Gerät des Persönlichen Mix, nicht das erste der Liste."""
    import tkinter as tk

    from freestream import wavesetup
    from freestream.wavehost import WaveHost
    default = _wizard_default_sink(monkeypatch, pactl, "alsa_output.speakers")
    host = WaveHost(test_waveapp._config(env, {"linewave": {"setup_done": True}, "mixer": {
        "mic": "alsa_input.usb-capture"}}))
    host.enable_mixer(True)  # Schalter „Mixer“ bzw. `freedeck mixer setup` nach „Später einrichten“
    assert default[0].startswith("linewave_")
    assert host.mixer._state()["monitor_output"] == "alsa_output.speakers"
    assert "monitor_output" not in _wizard_saved(env)["mixer"]
    data = wavesetup.collect(host)
    assert data["default_sink"] == "" and data["outputs"][0][0] == "alsa_output.headphones"
    assert data["monitor_output"] == "alsa_output.speakers"
    root = tk.Tk()
    try:
        wiz = wavesetup.SetupWizard(_wizard_types.SimpleNamespace(root=root, host=host, worker=_WIZARD_SYNC), data)
        assert wiz.out.get() == "alsa_output.speakers"
        wavesetup.apply_choices(host, _wizard_finish(root, wiz))
        wiz.destroy()
    finally:
        root.destroy()
    assert host.mixer._state()["monitor_output"] == "alsa_output.speakers"
    assert _wizard_saved(env)["mixer"]["monitor_output"] == "alsa_output.speakers"
    assert _wizard_monitor_sinks(pactl) == {pactl._sink("alsa_output.speakers")}


def test_wizard_collect_offers_no_stale_or_unplugged_monitor_output(env, pactl):
    """Nur ein laufender Mixer zählt (kein alter Stand von vor dem Neustart), und nur ein vorhandenes Gerät."""
    from freestream import wavesetup
    from freestream.wavehost import WaveHost
    host = WaveHost(test_waveapp._config(env, {"mixer": {"enabled": True}}))
    host.mixer._save_state({"monitor_output": "alsa_output.speakers"})
    assert not host.mixer.is_active()
    assert wavesetup.collect(host)["monitor_output"] == ""
    host.mixer.setup()
    st = host.mixer._state()
    st["monitor_output"] = "alsa_output.usb-headset"  # inzwischen abgesteckt
    host.mixer._save_state(st)
    assert wavesetup.collect(host)["monitor_output"] == ""


@_wizard_needs_x
def test_wizard_after_skip_opened_from_settings_shows_recommendation(env, pactl):
    """„Später einrichten“ setzt nur setup_done: aus den Einstellungen geöffnet ist das noch die erste Einrichtung
    – Low Cut, Clipguard, Rauschunterdrückung und Noise Gate sind an, nicht alles aus."""
    import tkinter as tk

    from freestream import wavesetup
    from freestream.config import Config
    from freestream.wavehost import WaveHost
    host = WaveHost(test_waveapp._config(env, {}))
    data = {"inputs": [("alsa_input.usb-capture", "USB Capture")], "outputs": [("alsa_output.headphones", "Kopfhörer")],
            "catalog": _WIZARD_CAT, "wave3": False, "noise": ["rnnoise"]}
    rec = {"lowcut": True, "clipguard": True, "noise": True, "gate": True, "sc4m": False}

    def shown(wiz):
        return {"lowcut": wiz.lowcut.get(), "clipguard": wiz.clipguard.get(),
                **{k: v.get() for k, v in wiz.fx_choice.items()}}
    root = tk.Tk()
    try:
        win = _wizard_types.SimpleNamespace(root=root, host=host, worker=_WIZARD_SYNC)
        wiz = wavesetup.SetupWizard(win, data)  # erster Start
        assert shown(wiz) == rec
        wiz.skip()
        assert _wizard_saved(env)["linewave"]["setup_done"] is True
        for h in (host, WaveHost(Config())):  # gleich danach und nach einem Neustart von FreeWave
            wiz = wavesetup.SetupWizard(_wizard_types.SimpleNamespace(root=root, host=h, worker=_WIZARD_SYNC), data)
            assert shown(wiz) == rec
            wiz.destroy()
        wiz = wavesetup.SetupWizard(win, data)
        ch = _wizard_finish(root, wiz)
        wavesetup.apply_choices(host, ch)
        wiz.destroy()
    finally:
        root.destroy()
    mx = _wizard_saved(env)["mixer"]
    lim = _WIZARD_CAT[2]["id"]
    assert [f["plugin"] for f in mx["effects"]["mic"]] == ["builtin:highpass", _WIZARD_CAT[0]["id"], lim]
    assert mx["noise"]["enabled"] is True and mx["enabled"] is True
