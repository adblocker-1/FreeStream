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
    from streamlin import paths, wavemic
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
    from streamlin import mixer, system, wave3hw
    fake = FakePactl()
    monkeypatch.setattr(system, "_pactl_blocks", fake.blocks)
    monkeypatch.setattr(system, "have", lambda c: True)
    monkeypatch.setattr(mixer, "_run", fake)
    monkeypatch.setattr(mixer.time, "sleep", lambda s: None)
    monkeypatch.setattr(wave3hw, "present", lambda: True)
    monkeypatch.setattr(wave3hw, "USBTransport", lambda: FakeWave3())
    return fake


def _config(env, data):
    from streamlin.config import Config
    (env / "config").mkdir(exist_ok=True)
    (env / "config" / "config.json").write_text(json.dumps(data))
    return Config()


def test_pactl_sink_of_stream(monkeypatch):
    from streamlin import system
    out = "Sink Input #42\n\tDriver: PipeWire\n\tSink: 57\n\tMute: no\n\tVolume: front-left: 65536 / 100% / 0,00 dB\n"
    monkeypatch.setattr(system, "run_out", lambda *a, **k: out)
    b = system._pactl_blocks("sink-inputs")[0]
    assert b["index"] == "42" and b["sink"] == "57" and b["volume"] == 100


def test_host_sets_up_mixer_and_routes(env, pactl):
    from streamlin import wavemic
    from streamlin.wavehost import WaveHost
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
    from streamlin.wavehost import WaveHost
    host = WaveHost(_config(env, {}))
    host.start()
    assert not host.mixer.is_active()  # Mixer erst nach „Mixer einrichten“
    assert host.mic.hw().get()["lowcut"] is True
    (env / "state" / "wavemic.json").write_text(json.dumps({"lowcut": False, "clipguard": True}))
    host.request_reload()  # SIGHUP von der Stream-Deck-App
    host.tick()
    assert host.mic.settings["lowcut"] is False and host.mic._hw.get()["lowcut"] is False


def test_stream_deck_app_delegates_filter_to_wave_app(env, monkeypatch):
    from streamlin import app, wave3hw, wavemic
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
    from streamlin.__main__ import main
    assert main(["linewave", "--quit"]) == 1  # läuft nicht


def test_icon():
    from streamlin.wavehost import make_icon
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
    from streamlin.wavehost import WaveHost
    from streamlin.waveui import WaveWindow
    host = WaveHost(_config(env, {"mixer": {"enabled": True}}))
    host.start()
    root = tk.Tk()
    try:
        import types
        meters = {}

        def fake_level(device):
            meters[device] = types.SimpleNamespace(level=0.9, stop=lambda: meters.pop(device, None))
            return meters[device]
        win = WaveWindow(root, host, tray=False, level_factory=fake_level)
        assert _pump(root, lambda: bool(win.strips))
        assert set(win.strips) == {"system", "music", "browser", "voice", "game", "sfx", "mic"}
        # Pegel: laufen nur sichtbar, Anzeige nach dem Regler
        assert _pump(root, lambda: win.strips["music"]["monitor"]["meter"].level > 0.5)
        assert "linewave_music.monitor" in meters and "linewave_streammix.monitor" in meters
        assert win.mix_meters["stream"]["meter"].level == pytest.approx(0.9)
        assert win.strips["mic"]["monitor"]["meter"].level == 0  # stumm
        # Kopfzeile wird bei Aktualisierungen nicht neu angeordnet (kein Flackern)
        forgets = []
        win.scene_btn.pack_forget = lambda: forgets.append(1)
        win._update_header()
        win.refresh()
        assert _pump(root, lambda: not win._refreshing)
        win._update_header()
        assert forgets == [] and win.scene_btn.winfo_manager() == "pack"
        del win.scene_btn.pack_forget
        # Effekte- und Programme-Ansicht
        win.show_view("effects")
        win.show_view("apps")
        root.update()
        win.show_view("mixer")
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
        # Menü „…“: Updates beim Start ein-/ausschalten
        assert not hasattr(win, "more_btn")  # kein „…“-Menü mehr: alles steht in der Seitenleiste
        # Einstellungen: ein Fenster, beim zweiten Öffnen dasselbe
        dlg = win.open_settings()
        assert win.open_settings() is dlg
        dlg.destroy()
        # Fenster schließen = ausblenden, Beenden beendet die Hauptschleife
        win._hidden_once = True
        win.hide()
        assert root.state() == "withdrawn"
        assert not meters and not win.levels.running  # versteckt: alle Messungen beendet
        win.request_quit()
        root.after(2000, root.quit)
        root.mainloop()
        assert win.quitting
    finally:
        root.destroy()


def test_linewave_command_routes_audio_subcommands(env, monkeypatch):
    from streamlin import __main__ as M
    seen = []
    monkeypatch.setattr(M, "main", lambda argv=None, prog="lindeck": seen.append(argv) or 0)
    M.wave_main(["mic", "lowcut", "on"])
    M.wave_main(["mixer", "set", "music", "stream", "40"])
    M.wave_main(["--hidden"])
    assert seen == [["wave", "lowcut", "on"], ["mixer", "set", "music", "stream", "40"], ["linewave", "--hidden"]]


def test_legacy_lindeck_channels_are_cleaned_up(env, pactl, monkeypatch):
    from streamlin import system
    from streamlin.mixer import Mixer
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
    from streamlin.mixer import Mixer
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
    from streamlin.mixer import Mixer
    mx = Mixer(_config(env, {"mixer": {"channels": [{"id": "system", "name": "System"}]}}), run=pactl)
    with pytest.raises(RuntimeError):
        mx.remove_channel("system")


# ==========================================================================
# Effekte (Plugins) je Kanal
# ==========================================================================
def test_effect_graph_mono_and_stereo():
    from streamlin import effects
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
    from streamlin import effects
    gate = {"id": "ladspa:gate_1410.so:gate", "label": "gate", "name": "Noise Gate",
            "controls": [{"name": "Threshold (dB)", "min": -70, "max": 20, "default": -70}]}
    assert effects.new_effect(gate)["controls"]["Threshold (dB)"] == -45.0  # sinnvoller Startwert
    cat = effects.catalog()
    assert [p["id"] for p in cat[:5]] == [b["id"] for b in effects.BUILTIN]


def test_ladspa_reader_on_real_plugin():
    from streamlin import effects
    lib = next(iter(effects._ladspa_files()), None)
    if lib is None:
        pytest.skip("keine LADSPA-Plugins installiert")
    found = effects._scan_all()
    assert found and all(len(p["inputs"]) == len(p["outputs"]) in (1, 2) for p in found)


def test_mixer_routes_channel_through_effects(env, pactl, monkeypatch):
    from streamlin import effects
    from streamlin.mixer import Mixer
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
    from streamlin import effects
    from streamlin.mixer import Mixer
    monkeypatch.setattr(effects.shutil, "which", lambda name: None)
    hp = effects.lookup("builtin:highpass")
    mx = Mixer(_config(env, {"mixer": {"effects": {"music": [effects.new_effect(hp)]}}}), run=pactl)
    mx.setup()  # ohne pipewire: Kanal läuft ohne Effekte weiter
    assert {b["source"] for b in _loops(pactl, "music").values()} == {"linewave_music.monitor"}


@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="braucht X (xvfb-run)")
def test_dialogs_open(env, pactl, monkeypatch):
    import tkinter as tk
    from streamlin import effects, paths
    from streamlin.pluginui import PluginManager
    from streamlin.wavedialogs import ChannelDialog, EffectsDialog, PluginPicker
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


# ==========================================================================
# Einrichtungsassistent
# ==========================================================================
def test_setup_wizard_applies_choices(env, pactl, monkeypatch):
    from streamlin import effects, wavesetup
    from streamlin.wavehost import WaveHost
    host = WaveHost(_config(env, {}))
    data = wavesetup.collect(host)
    assert ("alsa_input.usb-capture", "USB Capture") in data["inputs"] and data["wave3"] is True
    assert ("alsa_output.headphones", "Kopfhörer") in data["outputs"]
    hp = effects.lookup("builtin:highpass")
    wavesetup.apply_choices(host, {"mic": "alsa_input.usb-capture", "output": "alsa_output.speakers",
                                   "wave3": False, "lowcut": True, "clipguard": False, "gain": None,
                                   "channels": ["music", "voice"], "mic_fx": [effects.new_effect(hp)],
                                   "noise": False})
    cfg = json.loads((env / "config" / "config.json").read_text())
    mx = cfg["mixer"]
    assert cfg["linewave"]["setup_done"] is True and mx["enabled"] is True
    assert mx["mic"] == "alsa_input.usb-capture" and mx["monitor_output"] == "alsa_output.speakers"
    assert [c["id"] for c in mx["channels"]] == ["music", "voice"]
    assert mx["effects"]["mic"][0]["plugin"] == "builtin:highpass"
    assert mx["noise"]["enabled"] is False  # Rauschunterdrückung: eigene Stufe, nicht in den Effekten
    assert "noise" in data and isinstance(data["noise"], list)
    assert host.mixer.is_active()
    loops = _loops(pactl, "music")
    assert {b["sink"] for b in loops.values()} >= {pactl._sink("alsa_output.speakers")}


def test_setup_wizard_wave3_hardware(env, pactl):
    from streamlin import wavesetup
    from streamlin.wavehost import WaveHost
    host = WaveHost(_config(env, {}))
    wavesetup.apply_choices(host, {"mic": "alsa_input.wave3", "output": "", "wave3": True, "lowcut": False,
                                   "clipguard": True, "gain": 50, "channels": [], "mic_fx": []})
    hw = host.mic.hw().get()
    assert hw["lowcut"] is False and hw["clipguard"] is True and hw["gain"] == 20.0
    cfg = json.loads((env / "config" / "config.json").read_text())
    assert [c["id"] for c in cfg["mixer"]["channels"]] == ["system"]  # mindestens ein Programm-Kanal


def test_test_tone_and_level_meter(env, monkeypatch):
    import wave as wav
    from streamlin import wavesetup
    p = wavesetup.test_tone_file()
    with wav.open(str(p)) as w:
        assert w.getnchannels() == 2 and w.getframerate() == 48000
    monkeypatch.setattr(wavesetup.shutil, "which", lambda n: None)
    m = wavesetup.LevelMeter("alsa_input.x")
    assert not m.available
    m.stop()


def test_level_parsing_and_scheduling(monkeypatch):
    """Pegel aus s16le-Blöcken, Start/Stopp der Quellen und sanfter Abfall der Anzeige."""
    import array
    import types
    from streamlin import levels
    # Stille, volle Aussteuerung und −20 dB
    assert levels.peak_level(b"") == 0.0
    assert levels.peak_level(array.array("h", [0] * 64).tobytes()) == 0.0
    assert levels.peak_level(array.array("h", [0, -32768, 5]).tobytes()) == pytest.approx(1.0)
    lvl = levels.peak_level(array.array("h", [3277, -100]).tobytes())
    assert lvl == pytest.approx(40 / 60, abs=0.01)
    assert levels.level_db(lvl) == pytest.approx(-20, abs=0.5) and levels.level_db(0) is None
    assert levels.peak_level(b"\x00\x40\x00") > 0  # ungerade Länge wird abgeschnitten

    started, stopped = [], []

    def factory(device):
        src = types.SimpleNamespace(level=0.0, device=device)
        src.stop = lambda: stopped.append(device)
        started.append(device)
        return src
    now = [0.0]
    mon = levels.LevelMonitor(factory=factory, clock=lambda: now[0], fall_per_s=1.0)
    assert mon.available and not mon.running
    mon.watch({"music": "linewave_music.monitor", "mix:stream": "linewave_streammix.monitor", "x": ""})
    assert sorted(started) == ["linewave_music.monitor", "linewave_streammix.monitor"] and mon.running
    mon.watch({"music": "linewave_music.monitor", "mix:stream": "linewave_streammix.monitor"})
    assert len(started) == 2  # unverändert: nichts neu gestartet
    mon.sources["music"][1].level = 0.8
    assert mon.read()["music"] == pytest.approx(0.8)
    mon.sources["music"][1].level = 0.0
    now[0] = 0.25
    assert mon.read()["music"] == pytest.approx(0.55)  # fällt langsam
    now[0] = 2.0
    assert mon.read()["music"] == 0.0
    # anderes Gerät für einen Kanal: alte Quelle stoppen, neue starten; weggefallene stoppen
    mon.watch({"music": "alsa_input.other"})
    assert "linewave_music.monitor" in stopped and "linewave_streammix.monitor" in stopped
    assert started[-1] == "alsa_input.other"
    mon.stop()
    assert "alsa_input.other" in stopped and not mon.running and mon.read() == {}

    # ohne parec: nichts starten, Pegel bleiben 0
    monkeypatch.setattr(levels.shutil, "which", lambda n: None)
    mon = levels.LevelMonitor()
    assert not mon.available
    mon.watch({"music": "linewave_music.monitor"})
    assert not mon.running and mon.read() == {}
    assert levels.ParecSource("linewave_music.monitor").proc is None


def test_level_sources_and_parec(env, pactl):
    """Gemessen werden die Monitore der Kanal-Sinks, das Mikrofon und beide Mixe; parec mit niedriger Rate."""
    import io
    from streamlin import levels
    from streamlin.mixer import Mixer
    mx = Mixer(_config(env, {"mixer": {"enabled": True, "mic": "alsa_input.wave"}}))
    chans = [{"id": "mic", "name": "Mic", "kind": "input"}, {"id": "music", "name": "Music"},
             {"id": "cap", "name": "Cap", "kind": "input", "source": "alsa_input.usb-capture"}]
    src = levels.channel_sources(mx, chans, "alsa_output.headphones")
    assert src == {"mic": "alsa_input.wave", "music": "linewave_music.monitor", "cap": "alsa_input.usb-capture",
                   "mix:monitor": "alsa_output.headphones.monitor", "mix:stream": "linewave_streammix.monitor"}
    calls = []

    class Proc:
        def __init__(self, args, **kw):
            calls.append(args)
            self.stdout = io.BytesIO(b"\xff\x7f" * 10)
            self.done = False

        def poll(self):
            return 0 if self.done else None

        def terminate(self):
            self.done = True
    p = levels.ParecSource("linewave_music.monitor", exe="parec", popen=Proc)
    assert calls and "--device=linewave_music.monitor" in calls[0] and "--channels=1" in calls[0]
    assert "--rate=16000" in calls[0] and "--format=s16le" in calls[0]
    p.stop()
    assert p.proc is None and p.level == 0.0


@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="braucht X (xvfb-run)")
def test_setup_wizard_window(env, pactl):
    import tkinter as tk
    from streamlin import wavesetup
    from streamlin.wavehost import WaveHost
    from streamlin.waveui import WaveWindow
    host = WaveHost(_config(env, {}))
    root = tk.Tk()
    try:
        win = WaveWindow(root, host, tray=False)
        assert win.needs_setup()
        data = wavesetup.collect(host)
        wiz = wavesetup.SetupWizard(win, data)
        for _ in range(4):
            wiz.next()
            root.update()
        assert wiz.step == 4
        ch = wiz.choices()
        assert ch["mic"] and ch["channels"] == ["system", "music", "browser", "voice", "game", "sfx"]
        wiz.skip()
        assert not win.needs_setup()
    finally:
        root.destroy()


# ==========================================================================
# LV2-Effekte
# ==========================================================================
LV2INFO = """http://example.org/comp#mono

\tName:              Kompressor Test
\tClass:             Plugin
\tAuthor:            Beispiel
\tRequired Features: http://lv2plug.in/ns/ext/urid#map
\tPort 0:
\t\tType:        http://lv2plug.in/ns/lv2core#InputPort
\t\t             http://lv2plug.in/ns/lv2core#ControlPort
\t\tSymbol:      enable
\t\tName:        Enable
\t\tDesignation: http://lv2plug.in/ns/lv2core#enabled
\t\tMinimum:     0.000000
\t\tMaximum:     1.000000
\t\tDefault:     1.000000
\tPort 1:
\t\tType:        http://lv2plug.in/ns/lv2core#ControlPort
\t\t             http://lv2plug.in/ns/lv2core#InputPort
\t\tSymbol:      threshold
\t\tName:        Threshold
\t\tMinimum:     -50.000000
\t\tMaximum:     -10.000000
\t\tDefault:     -30.000000
\tPort 2:
\t\tType:        http://lv2plug.in/ns/lv2core#ControlPort
\t\t             http://lv2plug.in/ns/lv2core#InputPort
\t\tSymbol:      hold
\t\tName:        Hold
\t\tMinimum:     0.000000
\t\tMaximum:     1.000000
\t\tDefault:     0.000000
\t\tProperties:  http://lv2plug.in/ns/lv2core#integer
\t\t             http://lv2plug.in/ns/lv2core#toggled
\tPort 3:
\t\tType:        http://lv2plug.in/ns/lv2core#OutputPort
\t\t             http://lv2plug.in/ns/lv2core#ControlPort
\t\tSymbol:      gr
\t\tName:        Gain Reduction
\tPort 4:
\t\tType:        http://lv2plug.in/ns/lv2core#AudioPort
\t\t             http://lv2plug.in/ns/lv2core#InputPort
\t\tSymbol:      in
\t\tName:        In
\tPort 5:
\t\tType:        http://lv2plug.in/ns/lv2core#OutputPort
\t\t             http://lv2plug.in/ns/lv2core#AudioPort
\t\tSymbol:      out
\t\tName:        Out
"""


def test_parse_lv2info():
    from streamlin import effects
    p = effects.parse_lv2info(LV2INFO, "http://example.org/comp#mono")
    assert p["id"] == "lv2:http://example.org/comp#mono" and p["type"] == "lv2" and p["name"] == "Kompressor Test"
    assert p["inputs"] == ["in"] and p["outputs"] == ["out"]
    ctl = {c["name"]: c for c in p["controls"]}
    assert set(ctl) == {"threshold", "hold"}  # „enable“ und Ausgabewerte fehlen
    assert ctl["threshold"]["label"] == "Threshold" and ctl["threshold"]["default"] == -30
    assert ctl["hold"]["toggle"] and ctl["hold"]["int"]
    # nicht nutzbar: unbekanntes Pflicht-Feature, MIDI-/Atom-Port, Pegelanzeige
    assert effects.parse_lv2info(LV2INFO.replace("urid#map", "state#mapPath"), "x") is None
    atom = LV2INFO + "\tPort 6:\n\t\tType:        http://lv2plug.in/ns/ext/atom#AtomPort\n\t\tSymbol: midi\n"
    assert effects.parse_lv2info(atom, "x") is None
    assert effects.parse_lv2info(LV2INFO.replace("Class:             Plugin", "Class: Analyser Plugin"), "x") is None


def _lv2(uri, stereo):
    return {"id": f"lv2:{uri}", "type": "lv2", "plugin": uri, "label": uri, "name": uri,
            "inputs": ["inL", "inR"] if stereo else ["in"], "outputs": ["outL", "outR"] if stereo else ["out"],
            "controls": [{"name": "threshold", "label": "Threshold", "min": -50, "max": 0, "default": -30}]}


def test_lv2_graph_and_stages():
    from streamlin import effects
    hp = effects.lookup("builtin:highpass")
    mono, st = _lv2("urn:mono", False), _lv2("urn:stereo", True)
    cat = [hp, mono, st]
    chain = [effects.new_effect(hp), effects.new_effect(mono), effects.new_effect(st), effects.new_effect(hp)]
    g = effects.graph(chain, cat)
    assert '{ type = lv2 name = fx1l plugin = "urn:mono" control = { "threshold" = -30 } }' in g["nodes"]
    assert '{ output = "fx1l:out" input = "fx2:inL" }' in g["links"]
    sts = effects.stages(chain, cat)
    assert [(k, [i for i, _, _ in items]) for k, items in sts] == [("fc", [0]), ("lv2", [1]), ("lv2", [2]),
                                                                   ("fc", [3])]


def test_lv2_bridge_wiring(env, pactl, monkeypatch):
    """Ohne LV2 in PipeWire: Teilketten + jalv, per pw-link der Reihe nach verbunden."""
    from streamlin import effects
    from streamlin.mixer import Mixer
    hp = effects.lookup("builtin:highpass")
    mono, st = _lv2("urn:mono", False), _lv2("urn:stereo", True)
    monkeypatch.setattr(effects, "catalog", lambda refresh=False: [hp, mono, st])
    monkeypatch.setattr(effects, "pipewire_lv2", lambda: False)
    monkeypatch.setattr(effects, "lv2_bridge_available", lambda: True)
    import io
    started, links = [], []

    class P:
        pid = 999999

        def __init__(self, args, **kw):
            started.append(args)
            self.stdin = io.StringIO()

        def poll(self):
            return None
    monkeypatch.setattr(effects.subprocess, "Popen", P)
    node = "linewave_music_fx"
    ins = [f"{node}_s0_in:input_FL", f"{node}_s0_in:input_FR", f"{node}_s1l:in", f"{node}_s1r:in",
           f"{node}_s2:inL", f"{node}_s2:inR", f"{node}:input_FL", f"{node}:input_FR"]
    outs = ["linewave_music:monitor_FL", "linewave_music:monitor_FR", f"{node}_s0_out:output_FL",
            f"{node}_s0_out:output_FR", f"{node}_s1l:out", f"{node}_s1r:out", f"{node}_s2:outL", f"{node}_s2:outR"]
    monkeypatch.setattr(effects.Effects, "_ports", staticmethod(lambda d: outs if d == "out" else ins))
    real_run = effects.subprocess.run
    monkeypatch.setattr(effects.subprocess, "run", lambda args, **kw: links.append(tuple(args[1:]))
                        if args[0] == "pw-link" else real_run(args, **kw))
    cfg = _config(env, {"mixer": {"enabled": True, "effects": {"music": [
        effects.new_effect(hp), effects.new_effect(mono), effects.new_effect(st)]}}})
    mx = Mixer(cfg, run=pactl)
    mx.setup()
    assert any("media.class=Audio/Source/Virtual" in c and f"sink_name={node}" in c for c in pactl.cmds)
    jalv = [a for a in started if "jalv" in a]
    assert [a[a.index("-n") + 1] for a in jalv] == [f"{node}_s1l", f"{node}_s1r", f"{node}_s2"]
    assert ["-c", "threshold=-30"] == jalv[0][jalv[0].index("-c"):jalv[0].index("-c") + 2]
    assert links == [("linewave_music:monitor_FL", f"{node}_s0_in:input_FL"),
                     ("linewave_music:monitor_FR", f"{node}_s0_in:input_FR"),
                     (f"{node}_s0_out:output_FL", f"{node}_s1l:in"), (f"{node}_s0_out:output_FR", f"{node}_s1r:in"),
                     (f"{node}_s1l:out", f"{node}_s2:inL"), (f"{node}_s1r:out", f"{node}_s2:inR"),
                     (f"{node}_s2:outL", f"{node}:input_FL"), (f"{node}_s2:outR", f"{node}:input_FR")]
    assert {b["source"] for b in _loops(pactl, "music").values()} == {node}
    # Live-Regler eines LV2-Effekts: Befehl an jalv
    assert mx.fx.set_live("music", 1, "threshold", -20)
    assert effects._PROCS["music"][f"{node}_s1l"].stdin.getvalue() == "threshold = -20\n"
    mx.fx.stop("music")
    assert any(c[0] == "unload-module" for c in pactl.cmds)
