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
                                                  "sink": self._sink(kv.get("sink", "")),
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
        return ""

    def blocks(self, what):
        if what == "sink-inputs":
            return [dict(b) for b in self.inputs.values()]
        if what == "sinks":
            return [dict(s) for s in self.sinks]
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
        assert win.mic_name.get().startswith("Elgato Wave:3") and win.hw_frame.winfo_manager() == "grid"
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
