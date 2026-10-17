"""Tests für Mixer-Szenen, Auto-Ducking und globale Tastenkürzel (ohne PipeWire/Desktop)."""
import gc
import json
import os
import types

import pytest

import test_waveapp
from test_waveapp import _config, _pump

env = test_waveapp.env  # Fixtures
pactl = test_waveapp.pactl


def _mixer(env, data=None):
    from freestream.mixer import Mixer
    cfg = _config(env, data or {"mixer": {"enabled": True}})
    mx = Mixer(cfg)
    mx.setup()
    return mx, cfg


# ================================================================ Szenen
def test_scenes_save_apply_rename_delete(env, pactl):
    mx, cfg = _mixer(env)
    mx.set_volume("music", "monitor", 40)
    mx.set_mute("game", "stream", True)
    s = mx.save_scene("Gaming")
    assert s["id"] == "gaming" and s["levels"]["music"]["monitor"] == [40, False]
    assert s["levels"]["game"]["stream"] == [100, True]
    assert mx.active_scene()["id"] == "gaming"
    # anderer Zustand, zweite Szene
    mx.set_volume("music", "both", 90)
    mx.set_mute("game", "stream", False)
    assert mx.active_scene() is None
    mx.save_scene("Talk")
    assert mx.active_scene()["id"] == "talk"
    # Szene anwenden (auch über den Namen, ohne Groß-/Kleinschreibung)
    mx.apply_scene("GAMING")
    assert mx.get("music", "monitor") == (40, False) and mx.get("game", "stream") == (100, True)
    assert mx.active_scene()["id"] == "gaming"
    # gleicher Name = überschreiben
    mx.set_volume("music", "monitor", 55)
    assert mx.save_scene("gaming")["id"] == "gaming" and len(mx.scenes()) == 2
    # gespeichert in config.json beim Mixer
    saved = json.loads((env / "config" / "config.json").read_text())["mixer"]["scenes"]
    assert [x["name"] for x in saved] == ["gaming", "Talk"]
    mx.rename_scene("gaming", "Spiel")
    with pytest.raises(RuntimeError):
        mx.rename_scene("Spiel", "talk")  # Name schon vergeben
    assert mx.scene("spiel")["id"] == "gaming"
    mx.delete_scene("Spiel")
    assert [x["id"] for x in mx.scenes()] == ["talk"]
    with pytest.raises(RuntimeError):
        mx.apply_scene("gibtsnicht")
    # Kanal, den es nicht mehr gibt, wird übersprungen
    cfg.data["mixer"]["scenes"][0]["levels"]["weg"] = {"monitor": [10, False]}
    mx.apply_scene("talk")


def test_scene_cli(env, pactl, capsys):
    from freestream import __main__ as M
    from freestream.mixer import Mixer
    _mixer(env)
    assert M.main(["mixer", "scene", "list"]) == 0
    assert M.main(["mixer", "set", "music", "both", "30"]) == 0
    assert M.main(["mixer", "scene", "save", "Podcast", "Abend"]) == 0
    assert M.main(["mixer", "set", "music", "both", "80"]) == 0
    assert M.main(["mixer", "scene", "apply", "podcast abend"]) == 0
    from freestream.config import Config
    assert Mixer(Config()).get("music", "stream")[0] == 30
    capsys.readouterr()
    assert M.main(["mixer", "scene", "list"]) == 0
    assert "* podcast-abend" in capsys.readouterr().out
    assert M.main(["mixer", "scene", "rename", "podcast-abend", "Talk"]) == 0
    assert M.main(["mixer", "scene", "delete", "Talk"]) == 0
    assert M.main(["mixer", "scene", "apply", "Talk"]) == 1
    assert M.main(["mixer", "scene", "bogus"]) == 1
    # freewave mixer … landet beim selben Befehl
    seen = []
    orig = M.main
    try:
        M.main = lambda argv=None, prog="freedeck": seen.append(argv) or 0
        M.wave_main(["mixer", "scene", "apply", "Talk"])
        M.wave_main(["mic", "mute", "toggle"])
    finally:
        M.main = orig
    assert seen == [["mixer", "scene", "apply", "Talk"], ["wave", "mute", "toggle"]]


def test_mic_mute_cli_modes(env, pactl, monkeypatch):
    from freestream import __main__ as M
    from freestream import wave3hw
    from test_integrations import FakeWave3
    dev = FakeWave3()
    monkeypatch.setattr(wave3hw, "USBTransport", lambda: dev)

    def muted():
        return wave3hw.Wave3().get()["mute"]
    assert M.main(["wave", "mute", "on"]) == 0 and muted() is True
    assert M.main(["wave", "mute", "on"]) == 0 and muted() is True  # bleibt stumm
    assert M.main(["wave", "mute", "toggle"]) == 0 and muted() is False
    assert M.main(["wave", "mute", "off"]) == 0 and muted() is False
    assert M.main(["wave", "mute"]) == 0 and muted() is True


def test_scene_key_action(env, pactl, monkeypatch):
    from freestream import catalog, native
    mx, cfg = _mixer(env)
    mx.save_scene("Gaming")
    t = catalog.BY_UUID["com.lindeck.mixer.scene"]
    assert t.category == "FreeWave"
    assert t.fields[0].options() == [("gaming", "Gaming")]
    cls = native.lookup("com.lindeck.mixer.scene")
    assert cls is native.MixerSceneAction
    assert native.lookup("com.lindeck.mixer.channel") is native.MixerAction
    a = cls.__new__(cls)
    a.settings = {"scene": "gaming"}
    a.states = [{}, {}]
    a.ctx = types.SimpleNamespace(cfg=cfg)
    a.run = lambda fn, *args: fn(*args)
    assert a.active() is True and a.main_text() == "Gaming" and a.status_text() == "aktiv"
    mx.set_volume("music", "both", 20)
    a._sc_at = 0
    assert a.active() is False
    a.key_down()
    assert a.active() is True and mx.get("music", "monitor")[0] == 100
    a.settings = {"scene": "weg"}
    a._sc_at = 0
    assert a.active() is None and a.status_text() == "nicht gefunden"


# ============================================================ Auto-Ducking
def test_envelope_attack_hold_release():
    from freestream.ducking import Envelope
    env_ = Envelope(threshold_db=-30, attack=0.1, hold=0.5, release=0.4)
    assert env_.step(None, 0.04) == 0
    assert env_.step(-40, 0.04) == 0  # unter der Schwelle
    assert env_.step(-10, 0.05) == pytest.approx(0.5)
    assert env_.step(-10, 0.05) == pytest.approx(1.0)
    assert env_.step(-10, 1.0) == 1.0
    # Pause zwischen Wörtern: Haltezeit
    assert env_.step(None, 0.3) == 1.0
    assert env_.step(None, 0.3) == pytest.approx(1.0 - 0.1 / 0.4)
    assert env_.step(None, 0.5) == 0.0


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _ducker(env, pactl, level, channels=("music",), **extra):
    from freestream.ducking import Ducker
    duck = {"enabled": True, "channels": list(channels), "amount_db": 12, "threshold_db": -30,
            "attack_ms": 100, "hold_ms": 200, "release_ms": 200}
    duck.update(extra)
    mx, cfg = _mixer(env, {"mixer": {"enabled": True, "ducking": duck}})
    clock = Clock()
    d = Ducker(mx, cfg, level=lambda: level[0], clock=clock)
    return d, mx, clock


def _run(d, clock, seconds, step=0.04):
    for _i in range(int(round(seconds / step))):
        clock.t += step
        d.step(step)


def test_ducker_lowers_and_restores_exactly(env, pactl):
    from freestream import ducking
    level = [0.0]
    d, mx, clock = _ducker(env, pactl, level)
    mx.set_volume("music", "monitor", 80)
    mx.set_volume("music", "stream", 63)
    mx.set_volume("game", "both", 90)
    _run(d, clock, 0.2)
    assert mx.raw_levels()["music_monitor"][0] == 80 and not d.ducking
    level[0] = 0.8  # −12 dB: spricht
    _run(d, clock, 0.2)
    f = ducking.factor(12, 1)
    raw = mx.raw_levels()
    assert raw["music_monitor"][0] == round(80 * f) and raw["music_stream"][0] == round(63 * f)
    assert raw["game_monitor"][0] == 90  # nicht gewählt
    # Fenster/FreeDeck sehen den Wert des Nutzers
    assert mx.get("music", "monitor") == (80, False) and mx.levels()["music_stream"] == (63, False)
    assert json.loads((env / "state" / "ducking.json").read_text())["bases"]["music_monitor"] == 80
    level[0] = 0.0
    _run(d, clock, 0.1)
    assert d.ducking  # Haltezeit
    _run(d, clock, 0.5)
    assert not d.ducking
    raw = mx.raw_levels()
    assert raw["music_monitor"][0] == 80 and raw["music_stream"][0] == 63
    assert not (env / "state" / "ducking.json").exists()


def test_ducker_follows_user_faders(env, pactl):
    from freestream import ducking
    from freestream.mixer import Mixer
    level = [0.9]
    d, mx, clock = _ducker(env, pactl, level)
    mx.set_volume("music", "both", 80)
    _run(d, clock, 0.3)
    f = ducking.factor(12, 1)
    # Regler im Fenster (gleicher Prozess) während des Absenkens: Nutzerwert 50
    mx.set_volume("music", "monitor", 50)
    assert mx.raw_levels()["music_monitor"][0] == round(50 * f)
    assert mx.get("music", "monitor")[0] == 50
    # FreeDeck-Taste (anderer Prozess, über ducking.json): +10
    other = Mixer(mx.cfg)
    other.change_volume("music", "stream", 10)
    assert mx.raw_levels()["music_stream"][0] == round(90 * f)
    _run(d, clock, 0.1)
    assert d.base("music_stream") == 90
    # fremdes Programm (pavucontrol) stellt den abgesenkten Wert direkt
    idx = mx.raw_levels(with_index=True)["music_stream"][2]
    pactl(["set-sink-input-volume", idx, f"{round(100 * f)}%"])
    _run(d, clock, 0.6)
    assert d.base("music_stream") == 100
    # Szene während des Absenkens: Szenenwerte sind Nutzerwerte
    mx.cfg.data["mixer"]["scenes"] = [{"id": "s", "name": "S", "levels": {"music": {"monitor": [70, False]}}}]
    mx.apply_scene("s")
    assert d.base("music_monitor") == 70 and mx.active_scene()["id"] == "s"
    level[0] = 0.0
    _run(d, clock, 1.0)
    raw = mx.raw_levels()
    assert raw["music_monitor"][0] == 70 and raw["music_stream"][0] == 100


def test_ducker_off_and_channel_change_restore(env, pactl):
    level = [0.9]
    d, mx, clock = _ducker(env, pactl, level, channels=("music", "browser"))
    mx.set_volume("browser", "both", 60)
    _run(d, clock, 0.3)
    assert mx.raw_levels()["browser_monitor"][0] < 60
    mx.cfg.data["mixer"]["ducking"]["channels"] = ["music"]  # Browser abgewählt
    _run(d, clock, 0.08)
    assert mx.raw_levels()["browser_monitor"][0] == 60
    assert mx.raw_levels()["music_monitor"][0] < 100
    mx.cfg.data["mixer"]["ducking"]["enabled"] = False
    _run(d, clock, 0.04)
    assert mx.raw_levels()["music_monitor"][0] == 100 and not d.bases
    # Absturz während des Absenkens: beim nächsten Start zurück auf den Nutzerwert
    mx.cfg.data["mixer"]["ducking"]["enabled"] = True
    _run(d, clock, 0.3)
    assert mx.raw_levels()["music_monitor"][0] < 100
    d._write_file(force=True)
    from freestream.ducking import Ducker
    d2 = Ducker(mx, mx.cfg, level=lambda: 0.0, clock=clock)
    d2._recover()
    assert mx.raw_levels()["music_monitor"][0] == 100


def test_host_runs_ducker(env, pactl):
    from freestream.wavehost import WaveHost
    host = WaveHost(_config(env, {"mixer": {"enabled": True}}))
    assert host.mixer.ducker is host.ducker
    host.ducker._level_fn = lambda: 0.0
    host.start()
    try:
        assert host.ducker._thread is not None
    finally:
        host.cleanup()
    assert host.ducker._thread is None


# ========================================================== Tastenkürzel
def test_shortcut_conversion():
    from freestream import shortcuts as S
    assert S.normalize("ctrl+alt+m") == "Ctrl+Alt+M"
    assert S.normalize("<Control><Super>F9") == "Ctrl+Super+F9"
    assert S.normalize("m") == "" and S.normalize("Pause") == "Pause" and S.normalize("Hyper+m") == ""
    assert S.to_gtk("Ctrl+Alt+M") == "<Control><Alt>m"
    assert S.to_gtk("Ctrl+Shift+PageUp", primary=True) == "<Primary><Shift>Page_Up"
    assert S.to_kde("Ctrl+Super+Delete") == "Meta+Ctrl+Del"
    assert S.from_tk("m", 0x4 | 0x8) == "Ctrl+Alt+M"
    assert S.from_tk("Control_L", 0x4) == "" and S.from_tk("a", 0) == ""
    assert S.from_tk("Prior", 0x40) == "Super+PageUp"
    assert S.label("Ctrl+Alt+M") == "Ctrl + Alt + M"
    assert S.desktop({"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}) == "gnome"
    assert S.desktop({"XDG_CURRENT_DESKTOP": "pop:GNOME"}) == "gnome"
    assert S.desktop({"XDG_CURRENT_DESKTOP": "X-Cinnamon"}) == "cinnamon"
    assert S.desktop({"XDG_CURRENT_DESKTOP": "KDE"}) == "kde"
    assert S.desktop({"XDG_CURRENT_DESKTOP": "XFCE"}) == "xfce"
    assert S.desktop({"XDG_CURRENT_DESKTOP": "MATE"}) == "mate"
    assert S.desktop({"XDG_CURRENT_DESKTOP": "Hyprland"}) == ""
    assert S._desktop_exec("/usr/bin/freewave mixer scene apply 'a b'") == '/usr/bin/freewave mixer scene apply "a b"'


class FakeGSettings:
    """gsettings/xfconf-query als Wörterbuch."""

    def __init__(self):
        self.store = {}
        self.calls = []

    def __call__(self, args, capture_output=True, text=True, timeout=None):
        self.calls.append(args)
        ok = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "gsettings":
            op, schema, key = args[1], args[2], args[3]
            if op == "get":
                ok.stdout = self.store.get((schema, key), "@as []") + "\n"
            elif op == "set":
                self.store[(schema, key)] = args[4]
            elif op == "reset":
                self.store.pop((schema, key), None)
            return ok
        if args[0] == "xfconf-query":
            prop = args[args.index("-p") + 1]
            if "-r" in args:
                self.store.pop(prop, None)
            elif "-s" in args:
                if "-n" not in args and prop not in self.store:
                    return types.SimpleNamespace(returncode=1, stdout="", stderr="no such property")
                self.store[prop] = args[args.index("-s") + 1]
            elif prop in self.store:
                ok.stdout = self.store[prop] + "\n"
            else:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="Property does not exist")
            return ok
        raise AssertionError(args)


@pytest.fixture
def fake_desktop(env, monkeypatch):
    from freestream import shortcuts
    fake = FakeGSettings()
    monkeypatch.setattr(shortcuts.subprocess, "run", fake)
    monkeypatch.setattr(shortcuts.shutil, "which", lambda name: f"/usr/bin/{name}")
    return fake


def test_shortcuts_gnome(fake_desktop):
    from freestream import shortcuts as S
    env_ = {"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}
    other = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/"
    fake_desktop.store[(S.GNOME_SCHEMA, "custom-keybindings")] = f"['{other}']"
    cmd = "/usr/bin/freewave mic mute toggle"
    r = S.register("linewave-mic-mute", "FreeWave: Mikro", cmd, "ctrl+alt+m", env=env_)
    assert r["desktop"] == "gnome" and r["binding"] == "Ctrl+Alt+M"
    S.register("linewave-mic-mute", "FreeWave: Mikro", cmd, "Ctrl+Alt+M", env=env_)  # wiederholbar
    ours = S.GNOME_BASE + "streamlin-linewave-mic-mute/"
    lst = S._parse_strv(fake_desktop.store[(S.GNOME_SCHEMA, "custom-keybindings")])
    assert lst == [other, ours]
    rel = f"{S.GNOME_SCHEMA}.custom-keybinding:{ours}"
    assert fake_desktop.store[(rel, "binding")] == "'<Control><Alt>m'"
    assert S._parse_str(fake_desktop.store[(rel, "command")]) == cmd
    assert S.get("linewave-mic-mute") == "Ctrl+Alt+M"
    # Befehl mit Anführungszeichen (Szenenname) bleibt intakt
    S.register("linewave-scene-x", "Szene „Rock'n'Roll“", "freewave mixer scene apply 'x y'", "Super+1", env=env_)
    rel2 = f"{S.GNOME_SCHEMA}.custom-keybinding:{S.GNOME_BASE}streamlin-linewave-scene-x/"
    assert S._parse_str(fake_desktop.store[(rel2, "name")]) == "Szene „Rock'n'Roll“"
    assert S.unregister("linewave-mic-mute", env=env_) is True
    assert S.unregister("linewave-mic-mute", env=env_) is False
    lst = S._parse_strv(fake_desktop.store[(S.GNOME_SCHEMA, "custom-keybindings")])
    assert other in lst and ours not in lst and (rel, "binding") not in fake_desktop.store
    assert S.get("linewave-mic-mute") == ""


def test_shortcuts_cinnamon_and_mate(fake_desktop):
    from freestream import shortcuts as S
    env_ = {"XDG_CURRENT_DESKTOP": "X-Cinnamon"}
    fake_desktop.store[(S.CINNAMON_SCHEMA, "custom-list")] = "['__dummy__', 'custom0']"
    S.register("linewave-mic-mute", "Mic", "freewave mic mute toggle", "Super+M", env=env_)
    S.register("linewave-mic-mute", "Mic", "freewave mic mute toggle", "Super+M", env=env_)
    assert S._parse_strv(fake_desktop.store[(S.CINNAMON_SCHEMA, "custom-list")]) == [
        "__dummy__", "custom0", "streamlin-linewave-mic-mute"]
    rel = f"{S.CINNAMON_SCHEMA}.custom-keybinding:{S.CINNAMON_BASE}streamlin-linewave-mic-mute/"
    assert fake_desktop.store[(rel, "binding")] == "['<Super>m']"
    S.unregister("linewave-mic-mute", env=env_)
    assert S._parse_strv(fake_desktop.store[(S.CINNAMON_SCHEMA, "custom-list")]) == ["__dummy__", "custom0"]
    env_ = {"XDG_CURRENT_DESKTOP": "MATE"}
    S.register("linewave-mic-mute", "Mic", "freewave mic mute toggle", "Ctrl+F9", env=env_)
    rel = f"{S.MATE_SCHEMA}:{S.MATE_BASE}streamlin-linewave-mic-mute/"
    assert fake_desktop.store[(rel, "binding")] == "'<Control>F9'"
    S.unregister("linewave-mic-mute", env=env_)
    assert (rel, "action") not in fake_desktop.store


def test_shortcuts_xfce_only_touches_own_entries(fake_desktop):
    from freestream import shortcuts as S
    env_ = {"XDG_CURRENT_DESKTOP": "XFCE"}
    fake_desktop.store["/commands/custom/<Primary><Alt>t"] = "exo-open --launch TerminalEmulator"
    with pytest.raises(RuntimeError, match="TerminalEmulator"):
        S.register("linewave-mic-mute", "Mic", "freewave mic mute toggle", "Ctrl+Alt+T", env=env_)
    S.register("linewave-mic-mute", "Mic", "freewave mic mute toggle", "Ctrl+Alt+M", env=env_)
    S.register("linewave-mic-mute", "Mic", "freewave mic mute toggle", "Ctrl+Alt+M", env=env_)
    assert fake_desktop.store["/commands/custom/<Primary><Alt>m"] == "freewave mic mute toggle"
    # Taste ändern: alter Eintrag weg
    S.register("linewave-mic-mute", "Mic", "freewave mic mute toggle", "Super+M", env=env_)
    assert "/commands/custom/<Primary><Alt>m" not in fake_desktop.store
    assert fake_desktop.store["/commands/custom/<Super>m"] == "freewave mic mute toggle"
    # Nutzer hat die Taste inzwischen anders belegt: nicht anfassen
    fake_desktop.store["/commands/custom/<Super>m"] = "firefox"
    S.unregister("linewave-mic-mute", env=env_)
    assert fake_desktop.store["/commands/custom/<Super>m"] == "firefox"
    assert fake_desktop.store["/commands/custom/<Primary><Alt>t"] == "exo-open --launch TerminalEmulator"


def test_shortcuts_kde(env, tmp_path):
    from freestream import shortcuts as S
    home = tmp_path / "home"
    rc = home / ".config" / "kglobalshortcutsrc"
    rc.parent.mkdir(parents=True)
    rc.write_text("[kwin]\nExpose=Ctrl+F9,Ctrl+F9,Toggle\n\n[services][org.kde.konsole.desktop]\n_launch=Ctrl+Alt+T\n")
    env_ = {"XDG_CURRENT_DESKTOP": "KDE", "KDE_SESSION_VERSION": "6", "HOME": str(home)}
    r = S.register("linewave-mic-mute", "Mic", "/usr/bin/freewave mic mute toggle", "Super+Ctrl+M", env=env_)
    assert r["note"]
    S.register("linewave-mic-mute", "Mic", "/usr/bin/freewave mic mute toggle", "Super+Ctrl+M", env=env_)
    text = rc.read_text()
    assert text.count("[services][streamlin-linewave-mic-mute.desktop]") == 1
    assert "_launch=Meta+Ctrl+M" in text and "[kwin]" in text and "konsole" in text
    desk = home / ".local/share/applications/streamlin-linewave-mic-mute.desktop"
    assert "Exec=/usr/bin/freewave mic mute toggle" in desk.read_text()
    S.unregister("linewave-mic-mute", env=env_)
    assert not desk.exists() and "freestream" not in rc.read_text() and "konsole" in rc.read_text()
    # Plasma 5: eigene Gruppe mit Anzeigename
    env_["KDE_SESSION_VERSION"] = "5"
    S.register("linewave-mic-mute", "Mic", "freewave mic mute toggle", "Ctrl+Alt+M", env=env_)
    assert "[streamlin-linewave-mic-mute.desktop]\n_k_friendly_name=Mic\n_launch=Ctrl+Alt+M,none,Mic" in rc.read_text()
    S.unregister("linewave-mic-mute", env=env_)
    assert "freestream" not in rc.read_text()


def test_shortcuts_unknown_desktop(env):
    from freestream import shortcuts as S
    with pytest.raises(S.Unsupported) as e:
        S.register("linewave-mic-mute", "Mic", "freewave mic mute toggle", "Ctrl+Alt+M",
                   env={"XDG_CURRENT_DESKTOP": "Hyprland"})
    assert e.value.command == "freewave mic mute toggle"
    with pytest.raises(RuntimeError):
        S.register("linewave-mic-mute", "Mic", "x", "m", env={"XDG_CURRENT_DESKTOP": "GNOME"})


# ================================================================== Fenster
@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="braucht X (xvfb-run)")
def test_window_scenes_ducking_shortcut(env, pactl, monkeypatch):
    import tkinter as tk
    from freestream import shortcuts
    from freestream.wavehost import WaveHost
    from freestream.waveui import WaveWindow
    fake = FakeGSettings()
    monkeypatch.setattr(shortcuts.subprocess, "run", fake)
    monkeypatch.setattr(shortcuts.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    host = WaveHost(_config(env, {"mixer": {"enabled": True}}))
    host.ducker._level_fn = lambda: 0.0
    host.start()
    root = tk.Tk()
    win = None
    try:
        win = WaveWindow(root, host, tray=False, level_factory=lambda dev: types.SimpleNamespace(level=0.0,
                                                                                               stop=lambda: None))
        assert _pump(root, lambda: bool(win.strips))
        assert win.scene_btn.winfo_manager() == "pack"
        host.mixer.set_volume("music", "both", 45)
        host.mixer.save_scene("Abend")
        win.refresh()
        assert _pump(root, lambda: win.scene_btn.text == "Abend")
        dlg = win.open_scenes()
        assert win.open_scenes() is dlg
        root.update()
        # Tastenkürzel für die Szene
        win._scene_shortcut(host.mixer.scene("abend"), "Ctrl+Alt+1")
        assert _pump(root, lambda: shortcuts.get("linewave-scene-abend") == "Ctrl+Alt+1")
        host.mixer.set_volume("music", "both", 90)
        win.refresh()
        assert _pump(root, lambda: win.scene_btn.text == "Szenen")
        win._apply_scene(host.mixer.scene("abend"))
        assert _pump(root, lambda: host.mixer.get("music", "monitor")[0] == 45)
        dlg.destroy()
        # Mikrofon: Ducking-Einstellungen und Tastenkürzel
        win.show_view("mic")
        root.update()
        win.duck["enabled"].set(True)
        win._on_duck("enabled", True)
        win._toggle_duck_channel("browser")
        assert _pump(root, lambda: host.cfg.data["mixer"].get("ducking", {}).get("channels") == ["music", "browser"])
        assert host.cfg.data["mixer"]["ducking"]["enabled"] is True
        win.mic_shortcut.capture()
        ev = types.SimpleNamespace(keysym="m", state=0x4 | 0x40)
        win.mic_shortcut._key(ev)
        assert _pump(root, lambda: shortcuts.get("linewave-mic-mute") == "Ctrl+Super+M")
        # ein veralteter Zustand (vor dem Eintragen gesammelt) setzt das Feld nicht zurück
        win.apply_state(dict(win.state, shortcuts={}))
        assert win.mic_shortcut.binding == "Ctrl+Super+M"
        win.mic_shortcut._remove()
        assert _pump(root, lambda: shortcuts.get("linewave-mic-mute") == "")
        win.request_quit()
        root.after(2000, root.quit)
        root.mainloop()
    finally:
        host.cleanup()
        root.destroy()
        win = root = None
        gc.collect()  # Tk-Bilder hier freigeben, nicht später in einem fremden Thread

