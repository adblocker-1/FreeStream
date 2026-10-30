"""FreeWave: Audio Flow (Graph, Weg zu einem angeklickten Element) und Soundboard (Konfiguration, Abspielen)."""
import json
import os
import wave

import pytest

import test_waveapp
from test_waveapp import _config, _pump

env = test_waveapp.env  # Fixtures
pactl = test_waveapp.pactl


def _state():
    return {
        "mixer_active": True,
        "channels": [{"id": "system", "name": "System"}, {"id": "music", "name": "Music"},
                     {"id": "voice", "name": "Voice Chat"},
                     {"id": "capture", "name": "Console", "kind": "input", "source": "alsa_input.capture"},
                     {"id": "mic", "name": "Microphone", "kind": "input"}],
        "streams": [{"index": "7", "channel": "music", "app": "Spotify", "key": "spotify"},
                    {"index": "8", "channel": "music", "app": "VLC", "key": "vlc"},
                    {"index": "9", "channel": "voice", "app": "Discord", "key": "discord"},
                    {"index": "10", "channel": None, "app": "Loose", "key": "loose"}],
        "mixes": [{"id": "monitor", "name": "Personal Mix", "virtual": False, "exclude": ["mic"],
                   "devices_eff": ["alsa_output.hp"]},
                  {"id": "stream", "name": "Stream Mix", "virtual": True, "exclude": ["system"],
                   "devices_eff": []},
                  {"id": "chat", "name": "Chat Mix", "virtual": False, "exclude": ["music", "system", "capture"],
                   "devices_eff": ["alsa_output.hp", "alsa_output.spk"]}],
        "levels": {"music_monitor": (70, False), "music_stream": (35, True), "voice_monitor": (100, False)},
        "masters": {"system": (100, False), "music": (10, False), "voice": (100, False), "mic": (90, True)},
        "outputs": [("alsa_output.hp", "Headphones"), ("alsa_output.spk", "Speakers")],
        "sink_levels": {"alsa_output.hp": (80, False), "alsa_output.spk": (50, True),
                        "linewave_streammix": (100, False), "linewave_mix_chat": (60, False)},
        "mix_sinks": {"monitor": "linewave_mix_monitor", "stream": "linewave_streammix", "chat": "linewave_mix_chat"},
        "source_names": {"alsa_input.wave": "Elgato Wave:3 Mono"},
        "inputs": {"alsa_input.capture": "USB Capture"},
        "mic": {"source": "alsa_input.wave", "hardware": True},
    }


def _edge(g, src, dst):
    return next((e for e in g["edges"] if e["src"] == src and e["dst"] == dst), None)


# ------------------------------------------------------------------ Audio Flow
def test_flow_graph_nodes_and_edges():
    from freestream import audioflow
    g = audioflow.build(_state())
    cols = g["columns"]
    assert cols["channel"] == ["ch:system", "ch:music", "ch:voice", "ch:capture", "ch:mic"]
    # Eingänge nach Kanälen geordnet, leere Zeile für den Kanal ohne Programm, Programm ohne Kanal am Ende
    assert cols["input"] == ["in:empty:system", "in:stream:7", "in:stream:8", "in:stream:9",
                             "in:src:alsa_input.capture", "in:mic", "in:stream:10"]
    n = g["nodes"]
    assert n["in:empty:system"]["kind"] == "empty" and n["in:stream:7"]["sub"] == "Software"
    assert n["in:src:alsa_input.capture"]["title"] == "USB Capture"
    assert n["in:mic"]["title"] == "Elgato Wave:3 Mono" and n["in:mic"]["kind"] == "hardware"
    assert n["ch:music"]["sub"] == "10 %" and n["ch:mic"]["muted"] and n["ch:mic"]["sub"] == "Stumm"
    assert cols["mix"] == ["mix:monitor", "mix:stream", "mix:chat"]
    assert cols["output"] == ["out:alsa_output.hp", "out:virtual:stream", "out:alsa_output.spk"]
    assert n["out:alsa_output.spk"]["muted"] and n["mix:chat"]["sub"] == "Lautstärke 60 %"
    assert n["mix:monitor"]["sub"] == "Direkt auf den Ausgang"  # Persönlicher Mix direkt aufs Gerät
    # Kanten und ihre Art
    assert _edge(g, "in:stream:7", "ch:music")["kind"] == "input"
    assert _edge(g, "in:empty:system", "ch:system") is not None
    assert not any(e["src"] == "in:stream:10" for e in g["edges"])  # nicht im Mixer: keine Verbindung
    assert _edge(g, "ch:music", "mix:monitor") == {"src": "ch:music", "dst": "mix:monitor", "kind": "channel",
                                                   "muted": False}
    assert _edge(g, "ch:music", "mix:stream")["muted"]  # im Stream-Mix stumm
    assert _edge(g, "ch:mic", "mix:stream")["muted"]  # Kanal ganz stumm
    assert _edge(g, "ch:system", "mix:stream") is None and _edge(g, "ch:mic", "mix:monitor") is None
    assert _edge(g, "mix:chat", "out:alsa_output.spk")["muted"] and _edge(g, "mix:chat", "out:alsa_output.spk")[
        "kind"] == "mix"
    assert not _edge(g, "mix:monitor", "out:alsa_output.hp")["muted"]
    assert _edge(g, "mix:stream", "out:virtual:stream") is not None
    # Mixer aus: leerer Graph
    assert audioflow.build({"mixer_active": True})["edges"] == []


def test_flow_path_highlight():
    from freestream import audioflow
    g = audioflow.build(_state())
    # Kanal: seine Eingänge, seine Mixe, deren Ausgänge – nicht die anderen Kanäle dieser Mixe
    nodes, edges = audioflow.path(g, "ch:music")
    assert nodes == {"ch:music", "in:stream:7", "in:stream:8", "mix:monitor", "mix:stream", "out:alsa_output.hp",
                     "out:virtual:stream"}
    assert {(g["edges"][i]["src"], g["edges"][i]["dst"]) for i in edges} == {
        ("in:stream:7", "ch:music"), ("in:stream:8", "ch:music"), ("ch:music", "mix:monitor"),
        ("ch:music", "mix:stream"), ("mix:monitor", "out:alsa_output.hp"), ("mix:stream", "out:virtual:stream")}
    # Programm: sein Kanal und alles danach
    nodes, _e = audioflow.path(g, "in:stream:9")
    assert nodes == {"in:stream:9", "ch:voice", "mix:monitor", "mix:stream", "mix:chat", "out:alsa_output.hp",
                     "out:virtual:stream", "out:alsa_output.spk"}
    # Ausgang: rückwärts bis zu den Eingängen
    nodes, _e = audioflow.path(g, "out:alsa_output.spk")
    assert nodes == {"out:alsa_output.spk", "mix:chat", "ch:voice", "ch:mic", "in:stream:9", "in:mic"}
    # Mix: Kanäle und Eingänge davor, Ausgänge danach – andere Mixe nicht
    nodes, _e = audioflow.path(g, "mix:chat")
    assert "mix:monitor" not in nodes and "out:alsa_output.hp" in nodes and "in:mic" in nodes
    assert audioflow.path(g, "nope") == (set(), set())
    # Element ohne Verbindung: nur es selbst
    assert audioflow.path(g, "in:stream:10")[0] == {"in:stream:10"}


def test_flow_layout_and_curve():
    from freestream import audioflow
    g = audioflow.build(_state())
    lay = audioflow.layout(g, 1000, top=40)
    xs = [lay["cols"][c][0] for c in audioflow.COLUMNS]
    assert xs == sorted(xs) and xs[0] == 0
    x, w = lay["cols"]["output"]
    assert x + w == pytest.approx(1000)
    boxes = lay["boxes"]
    assert set(boxes) == set(g["nodes"])
    assert boxes["in:stream:7"][1] > boxes["in:empty:system"][1]  # untereinander, ohne Überlappung
    assert boxes["in:stream:7"][1] - boxes["in:empty:system"][1] >= boxes["in:stream:7"][3]
    assert lay["height"] == max(y + h for _x, y, _w, h in boxes.values())
    # schmal: Spalten nicht unter die Mindestbreite
    assert audioflow.layout(g, 300)["cols"]["input"][1] >= 160
    assert audioflow.curve((0, 0), (100, 50)) == [0, 0, 50, 0, 50, 50, 100, 50]
    assert audioflow.signature(g) == audioflow.signature(audioflow.build(_state()))


# ------------------------------------------------------------------ Soundboard
def _wav(path):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0" * 80)
    return path


def test_soundboard_config_round_trip(env):
    from freestream import paths, soundboard
    from freestream.config import Config
    cfg = _config(env, {})
    assert soundboard.sounds(cfg.data) == [] and soundboard.options(cfg.data) == ("", "", 80)
    src = _wav(env / "Air Horn.wav")
    s = soundboard.add(cfg, src)
    copied = paths.DATA_DIR / "sounds" / "Air Horn.wav"
    assert s["file"] == str(copied) and copied.is_file() and s["name"] == "Air Horn"
    assert s["volume"] == 100 and s["loop"] is False and s["color"] == soundboard.COLORS[0]
    s2 = soundboard.add(cfg, src)  # gleicher Name: eigene Kopie
    assert s2["file"].endswith("Air Horn-2.wav") and s2["id"] != s["id"]
    soundboard.update(cfg, s["id"], name="Horn", volume=150, loop=True, color="#ff4d5a", bogus=1)
    soundboard.set_options(cfg, play="all", monitor="monitor", volume=55)
    data = json.loads(paths.CONFIG_FILE.read_text())
    assert data["soundboard_play"] == "all" and data["soundboard_monitor"] == "monitor"
    assert data["soundboard_volume"] == 55
    again = Config()
    lst = soundboard.sounds(again.data)
    assert [x["name"] for x in lst] == ["Horn", "Air Horn"]
    assert lst[0] == {"id": s["id"], "name": "Horn", "file": str(copied), "color": "#ff4d5a", "volume": 100,
                      "loop": True}
    assert soundboard.options(again.data) == ("all", "monitor", 55)
    soundboard.remove(cfg, s["id"])
    assert not copied.exists() and len(soundboard.sounds(Config().data)) == 1
    with pytest.raises(RuntimeError):
        soundboard.update(cfg, "missing", name="x")
    with pytest.raises(RuntimeError):
        soundboard.import_file(env / "notes.txt")
    # unvollständige Einträge werden ergänzt
    assert soundboard.sounds({"soundboard": [{"file": "/x/beep.ogg", "volume": "abc"}, "junk"]})[0]["name"] == "beep"


def test_soundboard_targets():
    from freestream import soundboard
    mixes = {"monitor": "alsa_output.hp", "stream": "linewave_streammix", "chat": "linewave_mix_chat", "off": None}
    t = soundboard.targets
    assert t("", "", mixes) == [""]  # nichts gewählt: Standardausgabe (wie FreeWave in Rust)
    assert t("stream", "", mixes) == ["linewave_streammix"]
    assert t("all", "", mixes) == ["alsa_output.hp", "linewave_streammix", "linewave_mix_chat"]
    assert t("stream", "monitor", mixes) == ["linewave_streammix", "alsa_output.hp"]
    assert t("stream", "mix:monitor", mixes) == ["linewave_streammix", "alsa_output.hp"]  # alte Schreibweise
    assert t("all", "monitor", mixes) == ["alsa_output.hp", "linewave_streammix", "linewave_mix_chat"]
    assert t("stream", "out:alsa_output.spk", mixes) == ["linewave_streammix", "alsa_output.spk"]
    assert t("off", "", mixes) == [""]
    assert t("", "monitor", {}) == [""]  # Mixer aus: Standardausgabe
    assert t("", "out:", {}) == [""]


def test_soundboard_command_lines():
    from freestream import soundboard
    c = soundboard.command
    pw = ["pw-play", "-P", '{ linewave.loop = "linewave_soundboard" }']  # FreeWave erkennt den Stream als eigenen
    prop = {"PULSE_PROP": "linewave.loop=linewave_soundboard"}
    assert c("pw-play", "/s/a.wav", 50, "linewave_streammix") == (
        pw + ["--target", "linewave_streammix", "--volume=0.50", "/s/a.wav"], {})
    assert c("pw-play", "/s/a.wav", 100, "") == (pw + ["--volume=1.00", "/s/a.wav"], {})
    assert c("paplay", "/s/a.wav", 50, "alsa_output.hp") == (
        ["paplay", "--property=linewave.loop=linewave_soundboard", "--device=alsa_output.hp", "--volume=32768",
         "/s/a.wav"], {})
    assert c("mpv", "/s/a.mp3", 80, "linewave_mix_chat", loop=True) == (
        ["mpv", "--no-video", "--really-quiet", "--volume=80", "--audio-device=pulse/linewave_mix_chat",
         "--loop-file=inf", "/s/a.mp3"], prop)
    assert c("ffplay", "/s/a.mp3", 120, "linewave_streammix", loop=True) == (
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-volume", "100", "-loop", "0", "/s/a.mp3"],
        dict(prop, PULSE_SINK="linewave_streammix"))
    argv, _env = c("pw-play", "/s/a.wav", 100, "x", loop=True)  # Schleife über die Shell
    assert argv[:4] == ["sh", "-c", 'while "$@"; do :; done', "sh"] and argv[4:] == pw + [
        "--target", "x", "--volume=1.00", "/s/a.wav"]
    with pytest.raises(RuntimeError):
        c(None, "/s/a.wav")
    have_all = lambda p: True  # noqa: E731
    assert soundboard.pick_player("/s/a.wav", have_all) == "pw-play"
    assert soundboard.pick_player("/s/a.MP3", have_all) == "mpv"
    assert soundboard.pick_player("/s/a.flac", lambda p: p == "ffplay") == "ffplay"
    assert soundboard.pick_player("/s/a.ogg", lambda p: False) is None


class _Proc:
    _next = 1000

    def __init__(self, argv, **kw):
        self.argv, self.kw = argv, kw
        self.pid = _Proc._next
        _Proc._next += 1
        self.running = True

    def poll(self):
        return None if self.running else 0

    def terminate(self):
        self.running = False


def test_soundboard_player(tmp_path, monkeypatch):
    from freestream import soundboard
    procs = []

    def popen(argv, **kw):
        procs.append(_Proc(argv, **kw))
        return procs[-1]
    killed = []
    monkeypatch.setattr(soundboard.os, "killpg", lambda pid, sig: (killed.append(pid), next(
        p for p in procs if p.pid == pid).terminate()))
    f = _wav(tmp_path / "a.wav")
    pl = soundboard.Player(popen=popen, have=lambda p: p == "pw-play")
    s = {"id": "s1", "file": str(f), "volume": 50, "loop": False}
    assert pl.toggle(s, 80, ["linewave_streammix", "alsa_output.hp"]) is True
    assert [p.argv[3:] for p in procs] == [["--target", "linewave_streammix", "--volume=0.40", str(f)],
                                           ["--target", "alsa_output.hp", "--volume=0.40", str(f)]]
    assert all(p.kw["start_new_session"] for p in procs)  # eigene Prozessgruppe: Stoppen trifft auch die Schleife
    assert pl.is_playing("s1") and pl.playing() == {"s1"}
    assert pl.toggle(s, 80, ["x"]) is False  # zweiter Klick stoppt
    assert sorted(killed) == sorted(p.pid for p in procs) and not pl.is_playing("s1")
    procs[0].running = True  # ein beendeter Klang gilt nicht mehr als spielend
    pl.play("s1", str(f), 100, ["x"])
    procs[-1].running = False
    assert not pl.is_playing("s1")
    with pytest.raises(RuntimeError):
        pl.play("s2", str(tmp_path / "missing.wav"), 100, ["x"])
    with pytest.raises(RuntimeError):
        soundboard.Player(popen=popen, have=lambda p: False).play("s3", str(f), 100, ["x"])


def test_soundboard_key_action(env, pactl, monkeypatch):
    from freestream import catalog, native, soundboard
    cfg = _config(env, {"mixer": {"enabled": True}})
    s = soundboard.add(cfg, _wav(env / "ding.wav"))
    soundboard.set_options(cfg, play="stream", monitor="")
    assert soundboard.sound_choices() == [(s["id"], "ding")]
    at = next(a for a in catalog.CATALOG if a.uuid == "com.lindeck.freewave.soundboard")
    assert at.category == "FreeWave"
    assert native.lookup("com.lindeck.freewave.soundboard") is native.SoundboardAction
    calls = []
    pl = soundboard.Player(popen=lambda argv, **kw: calls.append(argv) or _Proc(argv, **kw),
                           have=lambda p: p == "pw-play")
    from freestream.mixer import Mixer
    Mixer(cfg).setup()
    assert soundboard.play_from_config(s["id"], cfg, player=pl) is True
    assert calls[0][-4:-2] == ["--target", "linewave_streammix"] and calls[0][-1] == s["file"]
    with pytest.raises(RuntimeError):
        soundboard.play_from_config("missing", cfg, player=pl)


def test_soundboard_streams_are_not_apps(pactl):
    """Klänge des Soundboards (linewave.loop=linewave_soundboard) sind keine Programme: nicht in der Liste,
    nicht in Kanäle verschoben."""
    from freestream import mixer, soundboard
    pactl.inputs["30"] = {"index": "30", "sink": "1", "volume": 100, "mute": False,
                          "props": {mixer.LOOP_PROP: soundboard.STREAM_ID, "application.name": "pw-play",
                                    "application.process.binary": "pw-play"}}
    mx = mixer.Mixer(None)
    assert "30" not in {s["index"] for s in mx.streams()}
    mx.route_streams()
    assert not any(c[:2] == ["move-sink-input", "30"] for c in pactl.cmds)
    assert "soundboard" not in mx.raw_levels()


# ------------------------------------------------------------------ Fenster
@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="braucht X (xvfb-run)")
def test_wave_window_flow_and_soundboard(env, pactl, monkeypatch):
    import tkinter as tk
    from freestream import msgbox, soundboard
    from freestream.wavehost import WaveHost
    from freestream.waveui import WaveWindow
    host = WaveHost(_config(env, {"mixer": {"enabled": True}}))
    host.start()
    root = tk.Tk()
    try:
        win = WaveWindow(root, host, tray=False, level_factory=lambda d: type("M", (), {"level": 0, "stop":
                                                                                         lambda self: None})())
        assert _pump(root, lambda: bool(win.state.get("mixer_active")))
        assert "flow" in win.nav and "soundboard" in win.nav
        # Audio Flow: Graph aus dem Zustand des Fensters, Klick hebt den Weg hervor
        win.show_view("flow")
        root.update()
        assert win.title_var.get() == "Audio Flow"
        assert "ch:music" in win.flow.graph["nodes"] and "in:stream:7" in win.flow.graph["nodes"]
        assert win.flow.cv.find_all()
        win.flow.select("ch:music")
        root.update()
        assert win.clear_btn.winfo_manager() == "pack"
        win.clear_btn.command()
        root.update()
        assert win.flow.selected is None and win.clear_btn.winfo_manager() == ""
        # Soundboard: Klang hinzufügen (Dateiauswahl), auswählen, abspielen
        win.show_view("soundboard")
        sv = win.soundview
        f = _wav(env / "beep.wav")
        monkeypatch.setattr(msgbox, "askopenfilename", lambda **kw: str(f))
        sv.add_sound()
        assert _pump(root, lambda: len(sv.pads) == 1)
        sid = sv.sounds[0]["id"]
        assert sv.selected == sid
        assert soundboard.sounds(host.cfg.data)[0]["file"].endswith("sounds/beep.wav")
        calls = []
        sv.player = soundboard.Player(popen=lambda argv, **kw: calls.append(argv) or _Proc(argv, **kw),
                                      have=lambda p: p == "pw-play")
        sv.toggle(sv.sounds[0])  # „Abspielen in“ nicht gewählt, Mithören auch nicht: Standardausgabe
        assert len(calls) == 1 and "--target" not in calls[0]
        sv.toggle(sv.sounds[0])
        calls.clear()
        sv._set_option(play="stream", monitor="monitor")
        assert sv.mon_btn.value == "Persönlicher Mix"
        sv.toggle(sv.sounds[0])
        assert [c[4] for c in calls] == ["linewave_streammix", win.state["mix_targets"]["monitor"]]
        assert sv.pads[sid].playing
        sv.toggle(sv.sounds[0])
        assert not sv.pads[sid].playing
        assert _pump(root, lambda: soundboard.options(host.cfg.data)[0] == "stream")
        sv._change(sid, loop=True, name="Beep")
        assert _pump(root, lambda: soundboard.sounds(host.cfg.data)[0]["loop"] is True)
        sv._delete(sid)
        assert _pump(root, lambda: not sv.pads and sv.selected == "")
    finally:
        root.destroy()
