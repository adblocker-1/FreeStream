"""FreeWave-Soundboard: Zuschnitt (start/end), gekürzte WAV-Kopie, Wellenform, Deck-Tasten, „Stop all“."""
import json
import os
import struct
import wave

import pytest

import test_waveapp
from freestream.i18n import _
from test_flow_soundboard import _Proc
from test_waveapp import _config, _pump

env = test_waveapp.env  # Fixtures
pactl = test_waveapp.pactl


def _wav(path, seconds=2.0, rate=8000, channels=1, width=2):
    """WAV mit lauter werdendem Ton (Wellenform steigt an)."""
    n = int(seconds * rate)
    full = (1 << (8 * width - 1)) - 1
    frames = bytearray()
    for i in range(n):
        v = int(full * 0.9 * i / n) * (1 if i % 2 else -1)
        if width == 1:
            sample = bytes([v // 256 + 128 if full > 255 else v + 128])
        else:
            sample = v.to_bytes(width, "little", signed=True)
        frames += sample * channels
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


def test_trim_config_round_trip(env):
    from freestream import soundboard
    from freestream.config import Config
    cfg = _config(env, {})
    s = soundboard.add(cfg, _wav(env / "horn.wav"))
    assert "start" not in s and "end" not in s  # nicht zugeschnitten: keine Schlüssel
    soundboard.update(cfg, s["id"], start=0.25, end=1.5)
    lst = soundboard.sounds(Config().data)
    assert (lst[0]["start"], lst[0]["end"]) == (0.25, 1.5)
    assert json.loads((env / "config" / "config.json").read_text())["soundboard"][0]["end"] == 1.5
    soundboard.update(cfg, s["id"], start=None)  # Anfang zurücksetzen, Ende bleibt
    assert "start" not in soundboard.sounds(Config().data)[0] and soundboard.sounds(Config().data)[0]["end"] == 1.5
    soundboard.update(cfg, s["id"], file=str(_wav(env / "other.wav")))  # neue Datei: Zuschnitt weg
    assert "end" not in soundboard.sounds(Config().data)[0]
    # ungültige Werte fallen weg
    n = soundboard.normalize
    assert "start" not in n({"start": 0}) and "start" not in n({"start": -1}) and "start" not in n({"start": "x"})
    assert "start" not in n({"start": True})
    assert "end" not in n({"start": 2.0, "end": 2.01})  # Ende (fast) vor dem Anfang
    assert n({"end": 1.23456})["end"] == 1.235
    # Bereich und Länge
    assert soundboard.trim_range({"start": 0.25, "end": 3.65}, 4.0) == (0.25, 3.65)
    assert soundboard.trim_range({"start": 0.25, "end": 9}, 2.0) == (0.25, 2.0)
    assert soundboard.trim_range({"start": 3, "end": 1}, 4.0) == (0.0, 4.0)
    assert soundboard.trim_range({}, 4.0) == (0.0, 4.0)


def test_time_formats():
    from freestream import soundboard as sb
    assert (sb.fmt_time(7.2), sb.fmt_time(0.3), sb.fmt_time(0), sb.fmt_time(65)) == ("0:07", "0:01", "0:00", "1:05")
    assert (sb.fmt_precise(3.65), sb.fmt_precise(0.25), sb.fmt_precise(4.0, 1), sb.fmt_precise(61.999)) == (
        "0:03.65", "0:00.25", "0:04.0", "1:02.00")
    assert sb.parse_time("0:03.65") == 3.65 and sb.parse_time("62,5") == 62.5 and sb.parse_time("1:02") == 62
    assert sb.parse_time("") is None and sb.parse_time("x") is None and sb.parse_time("-1") is None
    assert sb._fnv("abc") == "e71fa2190541574b"  # wie fnv() in FreeWave in Rust
    assert sb.key_text((1, 2)) == _("Key {n}").format(n=2)
    assert sb.key_text((2, 3)) == _("Page {page} · Key {key}").format(page=2, key=3)
    assert sb.key_text((1, 2), short=False) == _("Page {page} · Key {key}").format(page=1, key=2)


def test_trimmed_wav_output_length(env):
    from freestream import paths, soundboard
    src = _wav(env / "a.wav", seconds=2.0, rate=8000, channels=2)
    dst = env / "cut.wav"
    assert soundboard.write_trimmed(src, dst, 0.25, 1.5) == 10000
    with wave.open(str(dst)) as r, wave.open(str(src)) as o:
        assert (r.getnframes(), r.getnchannels(), r.getframerate(), r.getsampwidth()) == (10000, 2, 8000, 2)
        o.setpos(2000)
        assert r.readframes(10000) == o.readframes(10000)
    assert soundboard.write_trimmed(src, dst, 1.5, 9.0) == 4000  # über das Ende hinaus: begrenzt
    # playable(): gekürzte Kopie im Zustandsordner, ungekürzt ohne Zuschnitt oder bei anderen Formaten
    s = {"id": "x1", "file": str(src)}
    assert soundboard.playable(s) == str(src)
    p = soundboard.playable(dict(s, start=0.5))
    assert p.startswith(str(paths.STATE_DIR / "soundboard-trim" / "x1-"))
    with wave.open(p) as r:
        assert r.getnframes() == 12000
    p2 = soundboard.playable(dict(s, start=0.5, end=0.75))
    assert p2 != p and not os.path.exists(p)  # ältere Kopie desselben Klangs gelöscht
    assert soundboard.playable(dict(s, start=0, end=2.0)) == str(src)  # ganzer Klang
    ogg = env / "a.ogg"
    ogg.write_bytes(b"OggS")
    assert soundboard.playable({"id": "o", "file": str(ogg), "start": 1}) == str(ogg)
    # Player spielt die Kopie; Entfernen löscht sie
    calls = []
    pl = soundboard.Player(popen=lambda argv, **kw: calls.append(argv) or _Proc(argv, **kw),
                           have=lambda x: x == "pw-play")
    pl.toggle({"id": "x1", "file": str(src), "volume": 100, "loop": False, "start": 0.5, "end": 0.75}, 100, [""])
    assert calls[0][-1] == p2
    cfg = _config(env, {"soundboard": [{"id": "x1", "file": str(src)}]})
    soundboard.remove(cfg, "x1")
    assert not os.path.exists(p2)


def test_waveform_peaks(env):
    from freestream import soundboard
    f = _wav(env / "rise.wav", seconds=1.0)
    info = soundboard.analyze(f, n=40)
    assert info["duration"] == pytest.approx(1.0)
    assert len(info["peaks"]) == 40 and max(info["peaks"]) == 1.0
    assert info["peaks"][0] < 0.1 and info["peaks"][-1] > 0.9  # wird lauter
    assert soundboard.analyze(f, n=40) is info  # zwischengespeichert
    assert soundboard.play_length({"start": 0.25}, info) == pytest.approx(0.75)
    for width in (1, 3, 4):  # 8, 24 und 32 Bit
        g = soundboard.analyze(_wav(env / f"w{width}.wav", seconds=0.5, width=width), n=10)
        assert g["peaks"][-1] > 0.8 and g["peaks"][0] < 0.2
    # andere Formate, kaputte und fehlende Dateien: keine Wellenform
    ogg = env / "x.ogg"
    ogg.write_bytes(b"OggS")
    bad = env / "bad.wav"
    bad.write_bytes(b"RIFF0000WAVEjunk")
    assert soundboard.analyze(ogg) is None and soundboard.analyze(bad) is None
    assert soundboard.analyze(env / "missing.wav") is None and soundboard.analyze("") is None
    # geänderte Datei wird neu gelesen
    _wav(f, seconds=2.0)
    os.utime(f, ns=(1, 1))
    assert soundboard.analyze(f, n=40)["duration"] == pytest.approx(2.0)


def _profile(root, pid, pages):
    d = root / f"{pid}.sdProfile"
    ids = [f"{pid.lower()}-0000-4000-8000-00000000000{i}" for i in range(len(pages))]
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"Device": {"Model": "20GBA9901"}, "Name": pid,
                                                 "Pages": {"Default": ids[0], "Pages": ids}}))
    for pg, acts in zip(ids, pages):
        p = d / "Profiles" / pg.upper()
        p.mkdir(parents=True)
        (p / "manifest.json").write_text(json.dumps({"Controllers": [{"Type": "Keypad", "Actions": acts}]}))


def test_deck_key_lookup(tmp_path):
    from freestream import soundboard
    key = lambda sid: {"UUID": soundboard.ACTION_UUID, "Settings": {"sound": sid}}  # noqa: E731
    _profile(tmp_path, "AAAAAAAA", [{"2,1": key("a"), "1,0": key("b"), "3,2": {"UUID": "com.other"}},
                                    {"0,0": key("a"), "4,0": key("c"), "0,1": key("")}])
    _profile(tmp_path, "BBBBBBBB", [{"0,0": key("b"), "1,0": key("d")}])
    keys = soundboard.deck_keys(tmp_path)
    assert keys == {"a": (1, 8), "b": (1, 2), "c": (2, 5), "d": (1, 2)}  # Zeile 1, Spalte 2 bei 5 Spalten = 8
    assert soundboard.deck_keys(tmp_path / "none") == {}


def test_player_elapsed_and_stop_all(tmp_path, monkeypatch):
    from freestream import soundboard
    procs = []

    def popen(argv, **kw):
        procs.append(_Proc(argv, **kw))
        return procs[-1]
    monkeypatch.setattr(soundboard.os, "killpg", lambda pid, sig: next(p for p in procs if p.pid == pid).terminate())
    f = _wav(tmp_path / "a.wav", seconds=0.2)
    pl = soundboard.Player(popen=popen, have=lambda p: p == "pw-play")
    assert pl.elapsed("a") is None
    pl.play("a", str(f), 100, ["x"])
    pl.play("b", str(f), 100, ["x", "y"])
    assert pl.playing() == {"a", "b"} and pl.elapsed("a") >= 0
    pl.stop()  # alle
    assert pl.playing() == set() and pl.elapsed("a") is None and not any(p.running for p in procs)


@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="braucht X (xvfb-run)")
def test_soundboard_view_design(env, pactl, monkeypatch):
    """Kacheln mit Wellenform und Deck-Taste, Zuschnitt in den Einstellungen, „Stop all“ und Esc."""
    import tkinter as tk
    from freestream import soundboard
    from freestream.wavehost import WaveHost
    from freestream.waveui import WaveWindow
    sounds = [{"id": "s1", "name": "Rise", "file": str(_wav(env / "rise.wav")), "start": 0.5},
              {"id": "s2", "name": "Ogg", "file": str(env / "x.ogg")}]
    (env / "x.ogg").write_bytes(b"OggS")
    monkeypatch.setattr(soundboard, "deck_keys", lambda store_root=None: {"s1": (1, 3)})
    host = WaveHost(_config(env, {"mixer": {"enabled": True}, "soundboard": sounds}))
    host.start()
    root = tk.Tk()
    try:
        win = WaveWindow(root, host, tray=False, level_factory=lambda d: type("M", (), {"level": 0, "stop":
                                                                                         lambda self: None})())
        assert _pump(root, lambda: bool(win.state.get("mixer_active")))
        sv = win.soundview
        win.show_view("soundboard")
        assert win.title_var.get() == "Soundboard" and sv.stop_btn.winfo_manager() == "pack"
        assert _pump(root, lambda: sv.pads["s1"].info is not None and sv.pads["s1"].key == (1, 3))
        assert sv.pads["s1"].info["duration"] == pytest.approx(2.0) and sv.pads["s2"].info is None
        # Einstellungen: Zuschnitt mit Feldern; bei OGG nicht möglich
        sv.select(sv.sounds[0])
        assert sv._side["start_var"].get() == "0:00.50" and sv._side["end_var"].get() == "0:02.00"
        assert sv._side["plays_var"].get() == _("plays {time}").format(time="0:01.5")
        assert sv._side["trigger"]["value"].cget("text") == _("Page {page} · Key {key}").format(page=1, key=3)
        trim = sv._side["trim"]
        trim.on_change(0.5, 1.25)  # Griff losgelassen
        assert _pump(root, lambda: soundboard.sounds(host.cfg.data)[0].get("end") == 1.25)
        assert sv._side["end_var"].get() == "0:01.25"
        sv.select(sv.sounds[1])
        assert "trim" in sv._side and "start_var" not in sv._side and sv._side["trim"].info is None
        assert sv._side["trigger"]["name"].cget("text") == _("Not on a deck key")
        # abspielen: Zähler und „Stop all“; Esc stoppt alles
        calls = []
        sv.player = soundboard.Player(popen=lambda argv, **kw: calls.append(argv) or _Proc(argv, **kw),
                                      have=lambda p: p == "pw-play")
        monkeypatch.setattr(soundboard.os, "killpg", lambda pid, sig: None)
        sv.toggle(sv.sounds[0])
        assert calls[0][-1].startswith(str(env / "state" / "soundboard-trim"))  # zugeschnittene Kopie
        sv.toggle(sv.sounds[1])
        assert sv.count.n == 2 and sv.stop_btn.active and sv.pads["s1"].playing
        sv._escape()
        assert sv.player.playing() == set() and sv.count.n == 0 and not sv.stop_btn.active
        assert not sv.pads["s1"].playing
        win.show_view("mixer")
        assert sv.stop_btn.winfo_manager() == ""
    finally:
        root.destroy()


def test_wav_helper_is_pcm(tmp_path):
    f = _wav(tmp_path / "x.wav", seconds=0.01, channels=2)
    data = f.read_bytes()
    assert data[:4] == b"RIFF" and struct.unpack("<H", data[20:22])[0] == 1
