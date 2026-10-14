"""Tests für Linewave (eigenes Programm für Mixer und Wave-Mikrofon)."""
import json
import re
import os
import subprocess
import sys
import time

import pytest

from test_integrations import FakeWave3


@pytest.fixture
def env(tmp_path, monkeypatch):
    from lindeck import paths, wavemic
    for name, sub in (("DATA_DIR", "data"), ("STATE_DIR", "state"), ("CONFIG_DIR", "config"),
                      ("CONFIG_FILE", "config/config.json"), ("PROFILES_DIR", "data/profiles"),
                      ("RESOURCES_DIR", "data/resources"), ("PID_FILE", "state/lindeck.pid")):
        monkeypatch.setattr(paths, name, tmp_path / sub)
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(wavemic, "OWNER", False)
    wavemic._instance.clear()
    yield tmp_path
    wavemic._instance.clear()


class FakePactl:
    """pactl mit Sinks (Index) und Streams, die einem Sink zugeordnet sind."""

    def __init__(self):
        self.cmds, self.next_id = [], 100
        self.sinks = [{"index": "1", "name": "alsa_output.headphones", "description": "Kopfhörer"},
                      {"index": "2", "name": "alsa_output.speakers", "description": "Lautsprecher"}]
        self.inputs = {"7": {"index": "7", "sink": "1", "volume": 80, "mute": False,
                             "props": {"application.name": "Spotify", "application.process.binary": "spotify"}},
                       "8": {"index": "8", "sink": "1", "volume": 80, "mute": False,
                             "props": {"application.name": "Mein Spiel", "application.process.binary": "game.exe"}}}

    def _sink(self, name):
        return next((s["index"] for s in self.sinks if s["name"] == name), "0")

    def __call__(self, args):
        self.cmds.append(args)
        if args[0] == "load-module":
            self.next_id += 1
            kv = dict(a.split("=", 1) for a in args[2:] if "=" in a)
            if args[1] == "module-null-sink":
                self.sinks.append({"index": str(self.next_id), "name": kv["sink_name"], "description": ""})
            name = next((re.search(r"linewave\.loop='([^']+)'", a).group(1) for a in args
                         if a.startswith("sink_input_properties=")), None)
            if name:
                self.inputs[str(self.next_id)] = {"index": str(self.next_id), "volume": 100, "mute": False,
                                                  "sink": self._sink(kv.get("sink", "")), "source": kv.get("source"),
                                                  "props": {"linewave.loop": name, "media.name": "lesbar"}}
            return str(self.next_id)
        if args[0] == "get-default-sink":
            return "alsa_output.headphones"
        if args[0] == "get-default-source":
            return "alsa_input.mic"
        if args[0] == "set-sink-input-volume":
            self.inputs[args[1]]["volume"] = int(args[2].rstrip("%"))
        if args[0] == "set-sink-input-mute":
            b = self.inputs[args[1]]
            b["mute"] = (not b["mute"]) if args[2] == "toggle" else args[2] == "1"
        if args[0] == "move-sink-input":
            self.inputs[args[1]]["sink"] = self._sink(args[2])
        if args[0] == "unload-module":
            self.sinks = [x for x in self.sinks if x["index"] != args[1]]
            self.inputs.pop(args[1], None)
        return ""

    def blocks(self, what):
        if what == "sink-inputs":
            return [dict(b) for b in self.inputs.values()]
        if what == "sinks":
            return [dict(s) for s in self.sinks]
        if what == "sources":
            return [{"name": "alsa_input.usb-capture", "description": "USB Capture"},
                    {"name": "alsa_output.headphones.monitor", "description": "Monitor"}]
        return []


@pytest.fixture
def pactl(monkeypatch):
    from lindeck import mixer, system, wave3hw
    fake = FakePactl()
    monkeypatch.setattr(system, "_pactl_blocks", fake.blocks)
    monkeypatch.setattr(system, "have", lambda c: True)
    monkeypatch.setattr(mixer, "_run", fake)
    monkeypatch.setattr(mixer.time, "sleep", lambda s: None)
    monkeypatch.setattr(wave3hw, "present", lambda: True)
    monkeypatch.setattr(wave3hw, "USBTransport", lambda: FakeWave3())
    return fake


def _config(env, data):
    from lindeck.config import Config
    (env / "config").mkdir(exist_ok=True)
    (env / "config" / "config.json").write_text(json.dumps(data))
    return Config()


def test_pactl_sink_of_stream(monkeypatch):
    from lindeck import system
    out = "Sink Input #42\n\tDriver: PipeWire\n\tSink: 57\n\tMute: no\n\tVolume: front-left: 65536 / 100% / 0,00 dB\n"
    monkeypatch.setattr(system, "run_out", lambda *a, **k: out)
    b = system._pactl_blocks("sink-inputs")[0]
    assert b["index"] == "42" and b["sink"] == "57" and b["volume"] == 100


def test_host_sets_up_mixer_and_routes(env, pactl):
    from lindeck import wavemic
    from lindeck.wavehost import WaveHost
    cfg = _config(env, {"mixer": {"enabled": True, "routes": {"game.exe": "game"}}})
    host = WaveHost(cfg)
    assert wavemic.OWNER is True  # Linewave besitzt die Filterkette
    host.start()
    assert host.mixer.is_active() and not host.error
    host.tick()
    chans = {s["app"]: s["channel"] for s in host.mixer.streams()}
    assert chans == {"Spotify": "music", "Mein Spiel": "game"}
    # Programm per Fenster in einen anderen Kanal – wird gemerkt
    host.mixer.move_stream("7", "sfx", remember="spotify")
    assert {s["app"]: s["channel"] for s in host.mixer.streams()}["Spotify"] == "sfx"
    assert json.loads((env / "config" / "config.json").read_text())["mixer"]["routes"]["spotify"] == "sfx"
    with pytest.raises(RuntimeError):
        host.mixer.move_stream("7", "mic")
    host.enable_mixer(False)
    assert any(c[0] == "unload-module" for c in pactl.cmds)
    assert json.loads((env / "config" / "config.json").read_text())["mixer"]["enabled"] is False


def test_host_reload_applies_mic_settings(env, pactl):
    from lindeck.wavehost import WaveHost
    host = WaveHost(_config(env, {}))
    host.start()
    assert not host.mixer.is_active()  # Mixer erst nach „Mixer einrichten“
    assert host.mic.hw().get()["lowcut"] is True
    (env / "state" / "wavemic.json").write_text(json.dumps({"lowcut": False, "clipguard": True}))
    host.request_reload()  # SIGHUP von der Stream-Deck-App
    host.tick()
    assert host.mic.settings["lowcut"] is False and host.mic._hw.get()["lowcut"] is False


def test_stream_deck_app_delegates_filter_to_wave_app(env, monkeypatch):
    from lindeck import app, wave3hw, wavemic
    monkeypatch.setattr(wave3hw, "present", lambda: False)
    flag = env / "hup"
    code = ("import signal, sys, time\n"
            f"signal.signal(signal.SIGHUP, lambda *a: open({str(flag)!r}, 'w').write('x'))\n"
            "print('ok', flush=True)\ntime.sleep(30)\n")
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ok"
        (env / "state" / wavemic.WAVE_APP_FILE).write_text(json.dumps({"pid": proc.pid, "gui": True}))
        assert app.running_instance(wavemic.WAVE_APP_FILE) == proc.pid
        mic = wavemic.WaveMic({})
        mic.settings.update(lowcut=True)
        assert mic.apply() is False and not mic.running()
        for _ in range(50):
            if flag.exists():
                break
            time.sleep(0.05)
        assert flag.exists()
    finally:
        proc.kill()
        proc.wait()
    assert app.running_instance(wavemic.WAVE_APP_FILE) is None


def test_cli_wave_app_quit_and_entry(env):
    from lindeck.__main__ import main
    assert main(["linewave", "--quit"]) == 1  # läuft nicht


def test_icon():
    from lindeck.wavehost import make_icon
    img = make_icon(64)
    assert img.size == (64, 64) and img.getpixel((32, 32))[3] == 255


def _pump(root, cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        root.update()
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="braucht X (xvfb-run)")
def test_wave_window(env, pactl):
    import tkinter as tk
    from lindeck.wavehost import WaveHost
    from lindeck.waveui import WaveWindow
    host = WaveHost(_config(env, {"mixer": {"enabled": True}}))
    host.start()
    root = tk.Tk()
    try:
        win = WaveWindow(root, host, tray=False)
        assert _pump(root, lambda: bool(win.strips))
        assert set(win.strips) == {"system", "music", "browser", "voice", "game", "sfx", "mic"}
        assert win.strips["mic"]["monitor"]["mute"].get() is True
        assert win.mic_name.get().startswith("Elgato Wave:3") and win.hw_frame.winfo_manager() == "pack"
        assert win.gain_val.get().startswith("100 %")
        # Regler bewegen -> nach kurzer Pause an PipeWire
        win._on_level("music", "stream", "35")
        assert _pump(root, lambda: host.mixer.get("music", "stream")[0] == 35)
        assert win.strips["music"]["stream"]["val"].get() == "35 %"
        # Mikrofon-Gain und LEDs (Wave:3)
        win._on_gain("50")
        assert _pump(root, lambda: host.mic._hw.get()["gain"] == 20.0)
        win.v["leds_off"].set(True)
        win._on_hw_bool("leds_off")
        assert _pump(root, lambda: host.mic._hw.get()["leds_off"] is True)
        # Fenster schließen = ausblenden, Beenden beendet die Hauptschleife
        win._hidden_once = True
        win.hide()
        assert root.state() == "withdrawn"
        win.request_quit()
        root.after(2000, root.quit)
        root.mainloop()
        assert win.quitting
    finally:
        root.destroy()


def test_linewave_command_routes_audio_subcommands(env, monkeypatch):
    from lindeck import __main__ as M
    seen = []
    monkeypatch.setattr(M, "main", lambda argv=None: seen.append(argv) or 0)
    M.wave_main(["mic", "lowcut", "on"])
    M.wave_main(["mixer", "set", "music", "stream", "40"])
    M.wave_main(["--hidden"])
    assert seen == [["wave", "lowcut", "on"], ["mixer", "set", "music", "stream", "40"], ["linewave", "--hidden"]]


def test_legacy_lindeck_channels_are_cleaned_up(env, pactl, monkeypatch):
    from lindeck import system
    from lindeck.mixer import Mixer
    old = {"index": "55", "argument": "sink_name=lindeck_music sink_properties=device.description='LinDeck", "props": {}}
    blocks = pactl.blocks
    monkeypatch.setattr(system, "_pactl_blocks", lambda what: [old] if what == "modules" else blocks(what))
    Mixer({}, run=pactl).setup()
    assert ["unload-module", "55"] in pactl.cmds


# ==========================================================================
# Eigene Kanäle
# ==========================================================================
def _loops(pactl, cid):
    return {b["props"]["linewave.loop"]: b for b in pactl.inputs.values()
            if b["props"].get("linewave.loop", "").startswith(f"linewave_{cid}_")}


def test_custom_channels(env, pactl):
    from lindeck.mixer import Mixer
    cfg = _config(env, {"mixer": {"enabled": True}})
    mx = Mixer(cfg, run=pactl)
    mx.setup()
    assert mx.new_id("Größe Ü") == "groesseue" and mx.new_id("Mikrofon!") == "mikrofon"
    assert mx.new_id("System") == "system2"  # belegt
    # Kanal für Programme
    c = mx.add_channel("Podcast Gast", "apps", icon="chat", color="#ff6bd5")
    assert c["id"] == "podcastgast"
    assert any(x["name"] == "linewave_podcastgast" for x in pactl.sinks)
    assert set(_loops(pactl, "podcastgast")) == {"linewave_podcastgast_monitor", "linewave_podcastgast_stream"}
    assert json.loads((env / "config" / "config.json").read_text())["mixer"]["channels"][-1]["name"] == "Podcast Gast"
    # Lautstärke bleibt beim Umbenennen erhalten
    mx.set_volume("podcastgast", "stream", 30)
    mx.update_channel("podcastgast", name="Gast")
    assert mx.get("podcastgast", "stream") == (30, False)
    assert mx.channel("podcastgast")["name"] == "Gast"
    # Eingangsgerät als Kanal (z. B. Capture-Karte)
    assert mx.inputs() == [("alsa_input.usb-capture", "USB Capture")]
    cap = mx.add_channel("Konsole", "input", source="alsa_input.usb-capture")
    loops = _loops(pactl, cap["id"])
    assert {b["source"] for b in loops.values()} == {"alsa_input.usb-capture"}
    assert not any(x["name"] == f"linewave_{cap['id']}" for x in pactl.sinks)  # kein Null-Sink
    assert cap["id"] not in [a["id"] for a in mx.app_channels()]
    with pytest.raises(RuntimeError):
        mx.move_stream("7", cap["id"])  # Programme nur in Programm-Kanäle
    with pytest.raises(RuntimeError):
        mx.add_channel("Ohne Gerät", "input")
    # Löschen
    mx.remove_channel("podcastgast")
    assert not _loops(pactl, "podcastgast")
    assert not any(x["name"] == "linewave_podcastgast" for x in pactl.sinks)
    assert "podcastgast" not in [c["id"] for c in mx.all_channels()]


def test_last_app_channel_stays(env, pactl):
    from lindeck.mixer import Mixer
    mx = Mixer(_config(env, {"mixer": {"channels": [{"id": "system", "name": "System"}]}}), run=pactl)
    with pytest.raises(RuntimeError):
        mx.remove_channel("system")


# ==========================================================================
# Effekte (Plugins) je Kanal
# ==========================================================================
def test_effect_graph_mono_and_stereo():
    from lindeck import effects
    stereo = {"id": "ladspa:x.so:lim", "type": "ladspa", "plugin": "/usr/lib/ladspa/x.so", "label": "lim",
              "name": "Limiter", "inputs": ["In L", "In R"], "outputs": ["Out L", "Out R"],
              "controls": [{"name": "Limit (dB)", "min": -20, "max": 0, "default": -1}]}
    cat = [effects.lookup("builtin:highpass"), stereo]
    chain = [effects.new_effect(cat[0]), dict(effects.new_effect(stereo), controls={"Limit (dB)": -3}),
             dict(effects.new_effect(cat[0]), enabled=False)]
    g = effects.graph(chain, cat)
    assert len(g["nodes"]) == 3  # Hochpass je Seite + ein Stereo-Limiter, ausgeschalteter Effekt fehlt
    assert g["inputs"] == ["fx0l:In", "fx0r:In"] and g["outputs"] == ["fx1:Out L", "fx1:Out R"]
    assert '{ output = "fx0l:Out" input = "fx1:In L" }' in g["links"]
    assert '{ output = "fx0r:Out" input = "fx1:In R" }' in g["links"]
    assert '"Limit (dB)" = -3' in g["nodes"][2] and 'plugin = "/usr/lib/ladspa/x.so"' in g["nodes"][2]
    conf = effects.conf("music", "Musik", "linewave_music.monitor", chain, cat)
    assert 'target.object = "linewave_music"' in conf and "stream.capture.sink = true" in conf
    assert 'node.name        = "linewave_music_fx"' in conf
    assert effects.graph([dict(chain[0], enabled=False)], cat) is None


def test_effect_presets_and_catalog(env):
    from lindeck import effects
    gate = {"id": "ladspa:gate_1410.so:gate", "label": "gate", "name": "Noise Gate",
            "controls": [{"name": "Threshold (dB)", "min": -70, "max": 20, "default": -70}]}
    assert effects.new_effect(gate)["controls"]["Threshold (dB)"] == -45.0  # sinnvoller Startwert
    cat = effects.catalog()
    assert [p["id"] for p in cat[:5]] == [b["id"] for b in effects.BUILTIN]


def test_ladspa_reader_on_real_plugin():
    from lindeck import effects
    lib = next(iter(effects._ladspa_files()), None)
    if lib is None:
        pytest.skip("keine LADSPA-Plugins installiert")
    found = effects._scan_all()
    assert found and all(len(p["inputs"]) == len(p["outputs"]) in (1, 2) for p in found)


def test_mixer_routes_channel_through_effects(env, pactl, monkeypatch):
    from lindeck import effects
    from lindeck.mixer import Mixer
    hp = effects.lookup("builtin:highpass")
    cfg = _config(env, {"mixer": {"enabled": True, "effects": {"music": [effects.new_effect(hp)]}}})
    started = []
    monkeypatch.setattr(effects.Effects, "start", lambda self, c, src: started.append((c["id"], src))
                        or self.node_name(c["id"]))
    mx = Mixer(cfg, run=pactl)
    mx.setup()
    assert started == [("music", "linewave_music.monitor")]
    assert {b["source"] for b in _loops(pactl, "music").values()} == {"linewave_music_fx"}
    assert {b["source"] for b in _loops(pactl, "system").values()} == {"linewave_system.monitor"}
    # Effekte aus -> Kanal wieder direkt
    mx.fx.set_chain("music", [])
    mx.rebuild_channel("music")
    assert {b["source"] for b in _loops(pactl, "music").values()} == {"linewave_music.monitor"}


def test_effects_fall_back_without_pipewire(env, pactl, monkeypatch):
    from lindeck import effects
    from lindeck.mixer import Mixer
    monkeypatch.setattr(effects.shutil, "which", lambda name: None)
    hp = effects.lookup("builtin:highpass")
    mx = Mixer(_config(env, {"mixer": {"effects": {"music": [effects.new_effect(hp)]}}}), run=pactl)
    mx.setup()  # ohne pipewire: Kanal läuft ohne Effekte weiter
    assert {b["source"] for b in _loops(pactl, "music").values()} == {"linewave_music.monitor"}


@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="braucht X (xvfb-run)")
def test_dialogs_open(env, pactl, monkeypatch):
    import tkinter as tk
    from lindeck import effects, paths
    from lindeck.pluginui import PluginManager
    from lindeck.wavedialogs import ChannelDialog, EffectsDialog, PluginPicker
    root = tk.Tk()
    try:
        saved = []
        d = ChannelDialog(root, [("alsa_input.x", "Gerät X")], on_save=saved.append)
        d.name.set("Neu")
        d._save()
        assert saved and saved[0]["name"] == "Neu" and saved[0]["kind"] == "apps"
        cat = [effects.lookup("builtin:highpass")]
        changes = []
        e = EffectsDialog(root, {"id": "mic", "name": "Mikrofon"}, [], cat,
                          on_change=lambda chain, rebuild: changes.append(len(chain)), on_control=None)
        e._added(cat[0])
        assert changes == [1]
        pk = PluginPicker(root, cat, lambda p: None)
        pk.destroy()
        e.destroy()
        monkeypatch.setattr(paths, "PLUGINS_DIR", env / "plugins")
        pm = PluginManager(root)
        root.update()
        pm.destroy()
    finally:
        root.destroy()
