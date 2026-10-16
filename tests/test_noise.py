"""Tests für die Rauschunterdrückung des Mikrofons (Linewave)."""
import json
import os
import subprocess
import sys

import pytest

from test_waveapp import FakePactl, _config, _loops, _pump


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


class NsPactl(FakePactl):
    """Wie FakePactl, kennt zusätzlich die Quelle der Rauschunterdrückung, solange sie läuft."""

    def __init__(self):
        super().__init__()
        self.ns_up = False

    def blocks(self, what):
        out = super().blocks(what)
        if what == "sources" and self.ns_up:
            out.append({"name": "linewave_mic_ns", "description": "Linewave Mikrofon (Rauschunterdrückung)"})
        return out


@pytest.fixture
def pactl(monkeypatch):
    from streamlin import mixer, system, wave3hw
    fake = NsPactl()
    monkeypatch.setattr(system, "_pactl_blocks", fake.blocks)
    monkeypatch.setattr(system, "have", lambda c: True)
    monkeypatch.setattr(mixer, "_run", fake)
    monkeypatch.setattr(mixer.time, "sleep", lambda s: None)
    monkeypatch.setattr(wave3hw, "present", lambda: False)
    return fake


@pytest.fixture
def backends(monkeypatch, pactl):
    """Verfügbare Verfahren festlegen und `pipewire -c` durch einen harmlosen Prozess ersetzen."""
    from streamlin import noise
    have = {"rnnoise": False, "webrtc": True}
    monkeypatch.setattr(noise, "rnnoise_plugin", lambda: "/usr/lib/ladspa/librnnoise_ladspa.so"
                        if have["rnnoise"] else None)
    monkeypatch.setattr(noise, "webrtc_available", lambda: have["webrtc"])
    monkeypatch.setattr(noise, "_vad_control", lambda plugin: noise.RNNOISE_VAD)
    monkeypatch.setattr(noise.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "pipewire" else None)
    monkeypatch.setattr(noise.time, "sleep", lambda s: None)
    procs = []
    real_popen = subprocess.Popen

    def popen(args, *a, **kw):
        if list(args[:2]) != ["/usr/bin/pipewire", "-c"]:
            return real_popen(args, *a, **kw)
        pr = real_popen([sys.executable, "-c", "import time; time.sleep(30)", args[2]],
                        stdin=subprocess.DEVNULL)
        procs.append(pr)
        pactl.ns_up = True
        return pr
    monkeypatch.setattr(noise.subprocess, "Popen", popen)
    yield have
    for pr in procs:
        pr.kill()
        pr.wait()


def test_backend_choice(monkeypatch):
    from streamlin import noise
    monkeypatch.setattr(noise, "rnnoise_plugin", lambda: "/usr/lib/ladspa/librnnoise_ladspa.so")
    monkeypatch.setattr(noise, "webrtc_available", lambda: True)
    assert noise.available() == ["rnnoise", "webrtc"]
    assert noise.pick() == "rnnoise" and noise.pick("webrtc") == "webrtc"
    monkeypatch.setattr(noise, "rnnoise_plugin", lambda: None)
    assert noise.pick() == "webrtc" and noise.pick("rnnoise") == "webrtc"  # nicht installiert: das andere
    monkeypatch.setattr(noise, "webrtc_available", lambda: False)
    assert noise.pick() is None
    assert "noise-suppression-for-voice" in noise.install_hint()


def test_rnnoise_plugin_found_in_ladspa_path(tmp_path, monkeypatch):
    from streamlin import effects, noise
    monkeypatch.setattr(effects, "LADSPA_DIRS", [])
    monkeypatch.setenv("LADSPA_PATH", str(tmp_path))
    assert noise.rnnoise_plugin() is None
    (tmp_path / "librnnoise_ladspa.so").write_bytes(b"")
    assert noise.rnnoise_plugin() == str(tmp_path / "librnnoise_ladspa.so")


def test_configs():
    from streamlin import noise
    r = noise.rnnoise_conf("alsa_input.mic", "/usr/lib/ladspa/librnnoise_ladspa.so", 70)
    assert 'plugin = "/usr/lib/ladspa/librnnoise_ladspa.so" label = noise_suppressor_mono' in r
    assert '"VAD Threshold (%)" = 70' in r and 'target.object  = "alsa_input.mic"' in r
    assert 'node.name        = "linewave_mic_ns"' in r and "media.class      = Audio/Source" in r
    w = noise.webrtc_conf("alsa_input.mic")
    assert "libpipewire-module-echo-cancel" in w and "library.name = aec/libspa-aec-webrtc" in w
    assert "webrtc.noise_suppression = true" in w and "monitor.mode = true" in w
    assert 'target.object  = "alsa_input.mic"' in w and 'node.name        = "linewave_mic_ns"' in w
    # Referenz bleibt unverbunden, damit nur die Rauschunterdrückung wirkt
    assert w.count("node.autoconnect = false") == 2


def test_settings_saved_with_mixer_config(env):
    from streamlin.mixer import Mixer
    cfg = _config(env, {"mixer": {"enabled": True}})
    ns = Mixer(cfg).noise
    assert ns.settings() == {"enabled": False, "backend": "auto", "strength": 50}
    ns.set(enabled=True, strength=150)
    data = json.loads((env / "config" / "config.json").read_text())
    assert data["mixer"]["noise"] == {"enabled": True, "strength": 95}  # höchstens 95 %
    with pytest.raises(RuntimeError):
        ns.set(backend="magic")


def test_mixer_routes_mic_through_noise_suppression(env, pactl, backends, monkeypatch):
    from streamlin import effects
    from streamlin.mixer import Mixer
    hp = effects.lookup("builtin:highpass")
    cfg = _config(env, {"mixer": {"enabled": True, "noise": {"enabled": True},
                                  "effects": {"mic": [effects.new_effect(hp)]}}})
    started = []
    monkeypatch.setattr(effects.Effects, "start", lambda self, c, src: started.append((c["id"], src))
                        or self.node_name(c["id"]))
    mx = Mixer(cfg, run=pactl)
    mx.setup()
    # Mikrofon ─► Rauschunterdrückung ─► Effekte ─► Mixe
    assert started == [("mic", "linewave_mic_ns")]
    assert {b["source"] for b in _loops(pactl, "mic").values()} == {"linewave_mic_fx"}
    st = mx.noise.status()
    assert st["running"] and st["backend"] == "webrtc"
    text = (env / "state" / "noise.conf").read_text()
    assert "echo-cancel" in text and '"alsa_input.mic"' in text
    # RNNoise wird bevorzugt, sobald es installiert ist
    backends["rnnoise"] = True
    mx.rebuild_channel("mic")
    assert mx.noise.status()["backend"] == "rnnoise"
    assert "noise_suppressor_mono" in (env / "state" / "noise.conf").read_text()
    # Aus: Mikrofon wieder direkt in die Effekte, Prozess beendet
    mx.noise.set(enabled=False)
    assert mx.noise.apply() is True
    assert started[-1] == ("mic", "alsa_input.mic") and not mx.noise.running()
    # Mixer entfernen räumt auf
    mx.noise.set(enabled=True)
    mx.noise.apply()
    assert mx.noise.running()
    mx.teardown()
    assert not mx.noise.running() and json.loads((env / "state" / "noise.json").read_text()) == {}


def test_noise_falls_back_without_backend(env, pactl, backends):
    from streamlin.mixer import Mixer
    backends["webrtc"] = False
    mx = Mixer(_config(env, {"mixer": {"enabled": True, "noise": {"enabled": True}}}), run=pactl)
    mx.setup()  # nichts installiert: Mikrofon läuft ohne Rauschunterdrückung weiter
    assert {b["source"] for b in _loops(pactl, "mic").values()} == {"alsa_input.mic"}
    assert not mx.noise.running() and mx.noise.status()["hint"]


def test_host_restarts_noise_suppression(env, pactl, backends):
    from streamlin.wavehost import WaveHost
    cfg = _config(env, {"mixer": {"enabled": True}})
    host = WaveHost(cfg)
    host.start()
    assert host.mixer.is_active() and not host.mixer.noise.running()
    host.mixer.noise.set(enabled=True)
    # Linewave neu gestartet, Mixer lief weiter: Rauschunterdrückung wird wieder angeworfen
    host2 = WaveHost(cfg)
    host2.start()
    assert host2.mixer.noise.running()
    assert {b["source"] for b in _loops(pactl, "mic").values()} == {"linewave_mic_ns"}


def test_cli_noise(env, pactl, backends, capsys):
    from streamlin.__main__ import main, wave_main
    from streamlin.config import Config
    _config(env, {"mixer": {"enabled": True}})
    assert main(["wave", "noise", "status"]) == 0
    assert "Rauschunterdrückung: aus" in capsys.readouterr().out
    assert wave_main(["mic", "noise", "on"]) == 0
    out = capsys.readouterr().out
    assert "startet, sobald der Mixer eingerichtet ist" in out and "PipeWire WebRTC" in out
    assert Config().get("mixer")["noise"]["enabled"] is True
    from streamlin.mixer import Mixer
    Mixer(Config()).setup()
    assert main(["wave", "noise"]) == 0
    assert "an (PipeWire WebRTC, aktiv)" in capsys.readouterr().out
    backends["rnnoise"] = True
    assert main(["wave", "noise", "strength", "80"]) == 0
    assert Config().get("mixer")["noise"]["strength"] == 80
    assert main(["wave", "noise", "backend", "rnnoise"]) == 0
    assert "Stärke: 80 %" in capsys.readouterr().out
    assert main(["wave", "noise", "off"]) == 0
    assert not Mixer(Config()).noise.running()
    backends["rnnoise"] = backends["webrtc"] = False
    assert main(["wave", "noise", "on"]) == 1
    assert "noise-suppression-for-voice" in capsys.readouterr().out
    assert main(["wave", "noise", "loud"]) == 1


@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="braucht X (xvfb-run)")
def test_mic_view_noise_card(env, pactl, backends):
    import tkinter as tk
    from streamlin.wavehost import WaveHost
    from streamlin.waveui import WaveWindow
    host = WaveHost(_config(env, {"mixer": {"enabled": True}}))
    host.start()
    root = tk.Tk()
    try:
        win = WaveWindow(root, host, tray=False, level_factory=lambda dev: None)
        win.show_view("mic")
        assert _pump(root, lambda: win.noise_status.get().startswith("Aus"))
        assert not win.noise_strength.winfo_manager()  # WebRTC: keine Stärke
        win.v["noise"].set(True)
        win._on_noise()
        assert _pump(root, lambda: host.mixer.noise.running())
        assert _pump(root, lambda: win.noise_status.get().startswith("Aktiv: PipeWire WebRTC"))
        assert "Rauschunterdrückung" in win.state["channels"][-1]["fx_names"]
        backends["rnnoise"] = True
        host.mixer.rebuild_channel("mic")
        win.refresh()
        assert _pump(root, lambda: win.noise_strength.winfo_manager() == "pack")
        win._on_noise_strength("72")
        assert _pump(root, lambda: host.mixer.noise.settings()["strength"] == 72)
        win.v["noise"].set(False)
        win._on_noise()
        assert _pump(root, lambda: not host.mixer.noise.running())
    finally:
        root.destroy()
