"""Eigene Mixe in Linewave (wie „Mixes“ in Wave Link 2): anlegen, Kanäle zuordnen, Ausgänge,
Abhören, Hauptregler – und die Mix-Tasten auf dem Stream Deck (pactl simuliert)."""
import json
import os

import pytest

import test_waveapp
from test_waveapp import _config, _pump

env = test_waveapp.env  # Fixtures
pactl = test_waveapp.pactl


def _mixer(env, data=None):
    from streamlin.mixer import Mixer
    cfg = _config(env, data or {"mixer": {"enabled": True}})
    mx = Mixer(cfg)
    mx.setup()
    return mx, cfg


def _loop_sink(pactl, lid):
    """Name des Sinks, auf den ein Linewave-Loopback spielt."""
    b = next(b for b in pactl.inputs.values() if b["props"].get("linewave.loop") == lid)
    return next((s["name"] for s in pactl.sinks if s["index"] == b["sink"]), None)


def test_default_mixes_and_compatible_setup(env, pactl):
    mx, _cfg = _mixer(env)
    assert [m["id"] for m in mx.mixes()] == ["monitor", "stream"]
    assert mx.mix("monitor")["name"] == "Persönlicher Mix" and mx.mix("stream")["virtual"] is True
    # Persönlicher Mix mit einem Gerät geht wie bisher direkt aufs Gerät, kein eigener Sink
    assert _loop_sink(pactl, "linewave_music_monitor") == "alsa_output.headphones"
    assert _loop_sink(pactl, "linewave_music_stream") == "linewave_streammix"
    assert not any(s["name"].startswith("linewave_mix_") for s in pactl.sinks)
    # Kanal-Sinks mit monitor.channel-volumes: ihr Hauptregler wirkt auf alle Mixe
    sink_args = [a for c in pactl.cmds if c[:2] == ["load-module", "module-null-sink"] for a in c
                 if "sink_name=linewave_music" in " ".join(c)]
    assert any("monitor.channel-volumes='true'" in a for a in sink_args)
    mx.set_master("music", 140)
    assert ["set-sink-volume", "linewave_music", "100%"] in pactl.cmds  # nie über 100 %
    mx.set_master_mute("music", True)
    assert ["set-sink-mute", "linewave_music", "1"] in pactl.cmds
    mx.set_master("mic", 70)  # Mikrofon: an der Quelle, aus der es in die Mixe geht
    assert ["set-source-volume", "alsa_input.mic", "70%"] in pactl.cmds


def test_custom_mix_lifecycle(env, pactl):
    mx, _cfg = _mixer(env)
    mx.set_volume("music", "monitor", 70)
    chat = mx.add_mix("Chat-Mix", icon="chat", color="#22c55e", virtual=True)
    assert chat["id"] == "chatmix" and mx.mix_ids() == ["monitor", "stream", "chatmix"]
    assert any(s["name"] == "linewave_mix_chatmix" for s in pactl.sinks)
    assert _loop_sink(pactl, "linewave_music_chatmix") == "linewave_mix_chatmix"
    assert mx.get("music", "monitor") == (70, False)  # Neuaufbau übernimmt die Lautstärken
    conf = json.loads((env / "config" / "config.json").read_text())["mixer"]["mixes"]
    assert conf[0]["name"] == "Chat-Mix" and conf[0]["virtual"] is True
    # Kanal aus dem Mix nehmen und wieder aufnehmen – ohne Neuaufbau
    loads = len([c for c in pactl.cmds if c[0] == "load-module"])
    mx.set_member("music", "chatmix", False)
    assert "music_chatmix" not in mx.levels() and not mx.in_mix("music", "chatmix")
    assert mx.channel_mixes("music") == ["monitor", "stream"]
    mx.set_member("music", "chatmix", True)
    assert "music_chatmix" in mx.levels()
    assert len([c for c in pactl.cmds if c[0] == "load-module"]) == loads + 1
    # „alle Mixe“ (Deck-Taste, Kommandozeile) = die Mixe, in denen der Kanal ist
    mx.set_member("system", "chatmix", False)
    mx.set_volume("system", "both", 40)
    assert {k: v[0] for k, v in mx.levels().items() if k.startswith("system_")} == {"system_monitor": 40,
                                                                                     "system_stream": 40}
    # Ausgänge: zusätzlich auf die Lautsprecher
    mx.update_mix("chatmix", devices=["alsa_output.speakers"])
    assert _loop_sink(pactl, "linewave_mixout-chatmix-0") == "alsa_output.speakers"
    assert not mx.in_mix("system", "chatmix")  # Zuordnung bleibt beim Neuaufbau
    with pytest.raises(RuntimeError):
        mx.update_mix("chatmix", devices=[], virtual=False)  # ganz ohne Ausgang geht nicht
    with pytest.raises(RuntimeError):
        mx.remove_mix("stream")
    mx.remove_mix("chatmix")
    assert mx.mix_ids() == ["monitor", "stream"]
    assert not any(s["name"] == "linewave_mix_chatmix" for s in pactl.sinks)
    assert "music_chatmix" not in mx.levels()


def test_personal_mix_on_two_devices_and_listen(env, pactl):
    mx, _cfg = _mixer(env)
    mx.add_mix("Chat", virtual=True)
    # Persönlicher Mix auf Kopfhörer und Lautsprecher: eigener Sink, je Gerät ein Ausgang
    mx.update_mix("monitor", devices=["alsa_output.headphones", "alsa_output.speakers"])
    assert _loop_sink(pactl, "linewave_music_monitor") == "linewave_mix_monitor"
    assert _loop_sink(pactl, "linewave_mixout-monitor-0") == "alsa_output.headphones"
    assert _loop_sink(pactl, "linewave_mixout-monitor-1") == "alsa_output.speakers"
    assert mx.can_listen("chat") and mx.can_listen("stream")
    # Chat-Mix abhören: der Persönliche Mix wartet auf dem stillen Sink, das Gerät bekommt den Chat-Mix
    assert mx.listen("chat") == "chat" and mx.listening() == "chat"
    assert _loop_sink(pactl, "linewave_mixout-monitor-0") == "linewave_park"
    assert _loop_sink(pactl, "linewave_mixout-monitor-1") == "alsa_output.speakers"  # anderes Gerät bleibt
    assert _loop_sink(pactl, "linewave_listen") == "alsa_output.headphones"
    assert mx.listen("monitor") == "monitor"
    assert _loop_sink(pactl, "linewave_mixout-monitor-0") == "alsa_output.headphones"
    assert not any(b["props"].get("linewave.loop") == "linewave_listen" for b in pactl.inputs.values())
    # Mix direkt auf einem Gerät: abhören geht dort, nicht hier
    mx.add_mix("Speaker only", devices=["alsa_output.speakers"], virtual=False)
    assert not mx.can_listen("speakeronly")
    with pytest.raises(RuntimeError):
        mx.listen("speakeronly")


def test_direct_personal_mix_listen_and_output_switch(env, pactl):
    mx, _cfg = _mixer(env)
    mx.add_mix("Chat", virtual=True)
    mx.listen("chat")
    assert _loop_sink(pactl, "linewave_music_monitor") == "linewave_park"
    assert mx.set_monitor_output("Lautsprecher") == "alsa_output.speakers"  # beendet das Abhören
    assert mx.listening() == "monitor"
    assert _loop_sink(pactl, "linewave_music_monitor") == "alsa_output.speakers"


def test_old_state_is_rebuilt_and_ducking_uses_all_mixes(env, pactl):
    from streamlin import paths
    mx, _cfg = _mixer(env)
    st = json.loads((paths.STATE_DIR / "mixer.json").read_text())
    st.pop("loops")  # Zustand einer Version ohne eigene Mixe
    (paths.STATE_DIR / "mixer.json").write_text(json.dumps(st))
    mx.add_mix("Chat", virtual=True)
    assert "loops" in json.loads((paths.STATE_DIR / "mixer.json").read_text())
    assert "music_chat" in mx.raw_levels()
    from streamlin.ducking import Ducker
    d = Ducker(mx, cfg=mx.cfg, level=lambda: 0.0)
    assert set(d._mix_ids(0.0)) == {"monitor", "stream", "chat"}


def test_deck_mix_keys_follow_linewave_mixes(env, pactl, tmp_path, monkeypatch):
    from streamlin import deckexport, editdoc, mixkeys, native, paths
    from streamlin.models import Geometry
    xl = deckexport.plan(Geometry(8, 4), ["a"], mixes=["monitor", "stream", "chat"])
    assert [k for _s, c, k in xl[0] if c is None] == ["mix:both", "mix:monitor", "mix:stream", "mix:chat", "pages"]
    many = deckexport.plan(Geometry(5, 3), ["a"], mixes=["monitor", "stream", "chat"])
    assert [k for _s, c, k in many[0] if c is None] == ["mix:cycle", "pages"]
    plus = deckexport.plan(Geometry(4, 2, dials=4), ["a"], mixes=["monitor", "stream", "chat", "rec"])
    assert [k for _s, c, k in plus[0] if c is None] == ["mix:both", "mix:monitor", "mix:cycle", "pages"]
    # Mix-Taste mit eigenem Mix
    mx, cfg = _mixer(env)
    mx.add_mix("Chat", icon="chat", virtual=True)

    class Ctrl:
        preview = False
        keys, dials = {}, {}

        def redraw_action(self, a):
            pass

    class Ctx:
        ctrl = Ctrl()
    Ctx.cfg = cfg
    sel = native.MixSelectAction({"UUID": "com.lindeck.mixer.mixselect", "Settings": {"mix": "chat"}}, None, Ctx)
    cyc = native.MixSelectAction({"UUID": "com.lindeck.mixer.mixselect", "Settings": {"mix": "cycle"}}, None, Ctx)
    chan = native.MixerAction({"UUID": "com.lindeck.mixer.channel",
                               "Settings": {"channel": "music", "mix": "selected", "display": "tile"}}, None, Ctx)
    native._DECK_MIX["mix"] = "both"
    try:
        assert sel.target == "chat" and sel.main_text() == "Chat" and not sel.active()
        sel.key_down()
        assert sel.active() and chan.mix == "chat" and chan.mix_label() == "Chat"
        assert chan.frames((72, 72))[0][0].size == (72, 72)
        cyc.key_down()
        assert native.deck_mix() == "both"  # nach dem letzten Mix wieder „Alle“
        mx.remove_mix("chat")
        native._DECK_MIX["mix"] = "chat"
        assert chan.mix == "both"  # gelöschter Mix: Kanaltasten steuern alle Mixe
    finally:
        native._DECK_MIX["mix"] = "both"
    assert mixkeys.mix_key((72, 72), "chat", True, name="Ein sehr langer Mixname", icon="chat").size == (72, 72)
    # Export und Abgleich mit den Mixen
    monkeypatch.setattr(paths, "PID_FILE", tmp_path / "none.pid")
    mx.add_mix("Chat", icon="chat", virtual=True)
    name, _pages, _new = deckexport.export("20GAT9901", ["music"])
    root = next(d for d in paths.PROFILES_DIR.iterdir()
                if json.loads((d / "manifest.json").read_text())["Name"] == name)
    meta = json.loads((root / deckexport.META).read_text())["linewave"]
    assert meta["mixes"] == ["monitor", "stream", "chat"]
    doc = editdoc.ProfileDoc(root)
    assert doc.page(doc.page_ids[0]).get(("key", 3, 0))["Settings"] == {"mix": "chat"}
    assert deckexport.sync(["music"]) == []
    mx.remove_mix("chat")
    assert deckexport.sync(["music"]) == [name]
    assert json.loads((root / deckexport.META).read_text())["linewave"]["mixes"] == ["monitor", "stream"]


@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="braucht X (xvfb-run)")
def test_window_mix_table(env, pactl):
    import tkinter as tk
    from streamlin.wavehost import WaveHost
    from streamlin.waveui import EmptyCell, WaveWindow
    cfg = _config(env, {"mixer": {"enabled": True, "mixes": [
        {"id": "chat", "name": "Chat-Mix", "icon": "chat", "virtual": True, "exclude": ["music"]}]}})
    host = WaveHost(cfg)
    host.start()
    root = tk.Tk()
    try:
        win = WaveWindow(root, host, tray=False)
        assert _pump(root, lambda: bool(win.strips))
        assert list(win.mix_heads) == ["monitor", "stream", "chat"] and set(win.mix_cards) == set(win.mix_heads)
        assert "chat" in win.strips["system"] and "chat" not in win.strips["music"]
        empty = [w for w in win.table.winfo_children() if isinstance(w, EmptyCell)]
        assert len(empty) == 1
        empty[0].command()  # Musik in den Chat-Mix aufnehmen
        assert _pump(root, lambda: "chat" in win.strips.get("music", {}))
        win._on_level("music", "chat", "45")
        assert _pump(root, lambda: host.mixer.get("music", "chat")[0] == 45)
        win._on_master("music", "60")
        assert _pump(root, lambda: ["set-sink-volume", "linewave_music", "60%"] in pactl.cmds)
        win._on_listen("chat")
        assert _pump(root, lambda: host.mixer.listening() == "chat" and win.mix_heads["chat"]["ear"].get())
        win._on_listen("chat")  # nochmal: zurück zum Persönlichen Mix
        assert _pump(root, lambda: host.mixer.listening() == "monitor")
        win._save_mix(None, {"name": "Aufnahme", "icon": "record", "color": "#ff4d5a", "devices": [],
                             "virtual": True})
        assert _pump(root, lambda: "aufnahme" in win.mix_heads)
        # neuer Mix startet leer: man ordnet die Kanäle selbst zu
        assert not any("aufnahme" in st for st in win.strips.values())
        # Zuordnung ändern tauscht nur die eine Zelle, die Tabelle bleibt stehen
        keep = win.strips["system"]["master"]["fader"]
        win.strips["system"]["chat"]["remove"].command()  # X auf der Ecke der Zelle
        assert "chat" not in win.strips["system"]
        assert _pump(root, lambda: "system" in host.mixer.mix("chat")["exclude"])
        win.refresh()
        assert _pump(root, lambda: not win._refreshing) and win.strips["system"]["master"]["fader"] is keep
        win._toggle_member("system", "chat", True)
        assert "chat" in win.strips["system"]
        assert _pump(root, lambda: "system" not in host.mixer.mix("chat")["exclude"])
        assert win.strips["system"]["master"]["fader"] is keep
        win._edit_mix(win.state["mixes"][-1])
        root.update()
        dlg = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)][-1]
        assert dlg.values()["virtual"] is True and dlg.values()["name"] == "Aufnahme"
        dlg.destroy()
    finally:
        win.quitting = True
        root.destroy()
