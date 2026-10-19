"""Regressionstests zum Code-Review von FreeWave (Fenster, Dialoge, Einrichtungsassistent, theme)."""
import json
import os
import threading
import types

import pytest

import test_waveapp
from test_waveapp import _config, _pump

env = test_waveapp.env  # Fixtures
pactl = test_waveapp.pactl

needs_x = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="braucht X (xvfb-run)")

CAT = [{"id": "ladspa:gate_1410.so:gate", "label": "gate", "name": "Gate"},
       {"id": "ladspa:sc4m_1916.so:sc4m", "label": "sc4m", "name": "Kompressor"},
       {"id": "ladspa:fast_lookahead_limiter_1913.so:fastLookaheadLimiter", "label": "fastLookaheadLimiter",
        "name": "Limiter"}]
GATE, COMP, LIM = (p["id"] for p in CAT)


def _window(env, data=None):
    import tkinter as tk

    from freestream.wavehost import WaveHost
    from freestream.waveui import WaveWindow
    host = WaveHost(_config(env, data or {"mixer": {"enabled": True}}))
    host.start()
    root = tk.Tk()
    win = WaveWindow(root, host, tray=False,
                     level_factory=lambda d: types.SimpleNamespace(level=0.0, stop=lambda: None))
    assert _pump(root, lambda: bool(win.strips) and win._apps_ids is not None)
    return host, root, win


def _close(root, win):
    win.quitting = True
    root.destroy()


def _settle(root, win):
    """Warten, bis der Arbeits-Thread alles Bisherige erledigt hat."""
    done = []
    win.worker.submit(lambda: None, lambda _r, _e: done.append(1))
    assert _pump(root, lambda: bool(done) and not win._refreshing, 10)


def _spy(monkeypatch, module, name):
    """Dialogklasse ersetzen, die sich jede Instanz merkt."""
    seen = []
    orig = getattr(module, name)

    class Spy(orig):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            seen.append(self)
    monkeypatch.setattr(module, name, Spy)
    return seen


def _sources(monkeypatch, pactl, sources):
    from freestream import system
    base = pactl.blocks
    monkeypatch.setattr(system, "_pactl_blocks", lambda what: (
        [{"name": n, "description": d} for n, d in sources] if what == "sources" else base(what)))


def _padx(w):
    v = w.grid_info()["padx"]
    return tuple(int(x) for x in (v if isinstance(v, tuple) else str(v).split()))


# ==========================================================================
# mic-edit-switches-source: nur Farbe/Symbol ändern lässt das Mikrofon, wie es ist
# ==========================================================================
@needs_x
def test_mic_colour_edit_keeps_configured_device(env, pactl, monkeypatch):
    from freestream import wavedialogs
    _sources(monkeypatch, pactl, [("alsa_input.webcam", "Webcam Mic"),
                                  ("alsa_input.usb-Elgato_Wave_3", "Elgato Wave:3")])
    host, root, win = _window(env, {"mixer": {"enabled": True, "mic": "alsa_input.usb-Elgato_Wave_3"}})
    try:
        dlgs = _spy(monkeypatch, wavedialogs, "ChannelDialog")
        mx = host.cfg.data["mixer"]

        def edit(n, color, device=None):
            win._edit_channel_id("mic")
            assert _pump(root, lambda: len(dlgs) == n)
            d = dlgs[-1]
            if device:
                d.dev.set(device)
            d.color.set(color, notify=True)
            d._save()
            assert _pump(root, lambda: (mx.get("mic_channel") or {}).get("color") == color, 10)
            _settle(root, win)
            return d
        d = edit(1, wavedialogs.COLORS[0])
        assert d.dev.get() == "Elgato Wave:3"  # das eingestellte Gerät, nicht das erste der Liste
        assert mx["mic"] == "alsa_input.usb-Elgato_Wave_3"
        # „automatisch“ (kein Gerät eingestellt) bleibt automatisch – das Feld bleibt dafür leer
        mx["mic"] = ""
        d = edit(2, wavedialogs.COLORS[1])
        assert d.dev.get() == "" and mx["mic"] == ""
        # ein Gerät gewählt (auch das erste der Liste): wird übernommen
        edit(3, wavedialogs.COLORS[2], device="Webcam Mic")
        assert mx["mic"] == "alsa_input.webcam"
    finally:
        _close(root, win)


@needs_x
def test_channel_dialog_reports_only_a_changed_device(env):
    import tkinter as tk

    from freestream.wavedialogs import ChannelDialog
    root = tk.Tk()
    try:
        cam = {"id": "cam", "name": "Cam", "kind": "input", "source": "b"}
        d = ChannelDialog(root, [("a", "Gerät A"), ("b", "Gerät B")], channel=cam)
        assert d.dev.get() == "Gerät B" and d.values()["source_changed"] is False
        d.dev.set("Gerät A")
        assert d.values()["source"] == "a" and d.values()["source_changed"] is True
        d.destroy()
        # Gerät gerade nicht angeschlossen: Feld leer, Farbe ändern geht trotzdem, das Gerät bleibt
        saved = []
        for inputs in ([], [("a", "Gerät A")]):
            d = ChannelDialog(root, inputs, channel=cam, on_save=saved.append)
            assert d.dev.get() == ""
            d._save()
        assert len(saved) == 2 and not any(v["source_changed"] for v in saved)
        # neuer Eingangskanal ohne Gerät: weiterhin abgelehnt
        d = ChannelDialog(root, [], on_save=saved.append)
        d.name.set("Neu")
        d._kind("input")
        d._save()
        assert len(saved) == 2 and d.error.cget("text")
        d.destroy()
    finally:
        root.destroy()


# ==========================================================================
# stale-edit-dicts: Klick auf Mix/Kanal öffnet den aktuellen Stand
# ==========================================================================
@needs_x
def test_left_click_edit_uses_current_mix_and_channel(env, pactl, monkeypatch):
    from freestream import wavedialogs
    from freestream.mixer import DEFAULT_CHANNELS
    _sources(monkeypatch, pactl, [("alsa_input.capture", "Capture"), ("alsa_input.linein", "Line In")])
    chans = [dict(c) for c in DEFAULT_CHANNELS] + [{"id": "cam", "name": "Cam", "kind": "input",
                                                    "source": "alsa_input.capture"}]
    host, root, win = _window(env, {"mixer": {"enabled": True, "channels": chans, "mixes": [
        {"id": "chat", "name": "Chat", "icon": "chat", "virtual": True}]}})
    try:
        assert "chat" in win.mix_heads and "cam" in win.strips
        # Ausgänge und Gerät ändern sich – die Tabelle wird dafür nicht neu gebaut
        head, row = win.mix_heads["chat"]["name"], win.strips["cam"]["card"]
        host.mixer.update_mix("chat", devices=["alsa_output.speakers"])
        host.mixer.update_channel("cam", source="alsa_input.linein")
        win.refresh()
        assert _pump(root, lambda: (win._current("mixes", "chat") or {}).get("devices") == ["alsa_output.speakers"]
                     and win._current("channels", "cam")["source"] == "alsa_input.linein")
        assert win.mix_heads["chat"]["name"] is head and win.strips["cam"]["card"] is row
        seen = []
        monkeypatch.setattr(wavedialogs, "MixDialog", lambda master, outs, mix=None, current_devices=(),
                            on_save=None, on_delete=None: seen.append(list(current_devices)))
        head.event_generate("<Button-1>")  # Spaltenkopf
        win.mix_cards["chat"].event_generate("<Button-1>")  # Karte in der Seitenleiste
        root.update()
        assert seen == [["alsa_output.speakers"], ["alsa_output.speakers"]]
        win._edit_mix_id("weg")  # gerade gelöschter Mix: nichts passiert
        assert len(seen) == 2
        dlgs = _spy(monkeypatch, wavedialogs, "ChannelDialog")
        row.body.winfo_children()[0].event_generate("<Button-1>")  # Symbol der Kanalzeile
        assert _pump(root, lambda: bool(dlgs))
        assert dlgs[0].dev.get() == "Line In"
        dlgs[0].color.set(wavedialogs.COLORS[0], notify=True)
        dlgs[0]._save()
        assert _pump(root, lambda: host.mixer.channel("cam").get("color") == wavedialogs.COLORS[0], 10)
        assert host.mixer.channel("cam")["source"] == "alsa_input.linein"
        _settle(root, win)
    finally:
        _close(root, win)


# ==========================================================================
# wheel-on-fader-scrolls: Mausrad über einem Regler scrollt die Seite nicht mit
# ==========================================================================
@needs_x
def test_wheel_over_fader_does_not_scroll_the_page():
    import tkinter as tk

    from freestream.dialogkit import ScrollArea
    from freestream.theme import Fader
    root = tk.Tk()
    try:
        root.geometry("300x200")
        area = ScrollArea(root, bg="#000000")
        area.pack(fill="both", expand=True)
        f = Fader(area.inner, length=200, thickness=22, orient="horizontal", value=50)
        f.pack()
        tk.Frame(area.inner, height=1000, bg="#000000").pack()
        root.update()
        assert area.bar.winfo_manager()  # die Seite ist scrollbar
        top = area.cv.yview()
        for _ in range(3):
            f.event_generate("<Button-5>", x=5, y=5)
        root.update()
        assert f.get() == 44 and area.cv.yview() == top
        f.enabled = False  # gesperrter Regler: das Rad scrollt wie gewohnt die Seite
        f.event_generate("<Button-5>", x=5, y=5)
        root.update()
        assert f.get() == 44 and area.cv.yview() != top
    finally:
        root.destroy()


# ==========================================================================
# drag-ghost-stuck: Neuaufbau der Ansicht „Apps“ mitten im Ziehen
# ==========================================================================
def _chips(win):
    from freestream.waveui import AppChip
    out, stack = [], [win.apps_list]
    while stack:
        w = stack.pop()
        out += [c for c in w.winfo_children() if isinstance(c, AppChip)]
        stack += w.winfo_children()
    return out


@needs_x
def test_apps_view_waits_for_drop_and_ghost_never_sticks(env, pactl):
    host, root, win = _window(env)
    try:
        win.show_view("apps")
        root.update()
        chip = _chips(win)[0]
        s = {"app": chip.app, "channel": "music", "index": "7"}

        def with_stream(index):
            st = dict(win.state)
            st["streams"] = list(st.get("streams") or []) + [
                {"index": index, "app": "Ping", "channel": "voice", "key": "ping", "names": "ping"}]
            return st
        win._drag_app(s, chip, "start", 100, 100)
        ghost = win._drag_ghost
        win.apply_state(with_stream("99"))  # ein Stream kommt dazu, während man zieht
        root.update()
        assert chip.winfo_exists() and ghost.winfo_exists()  # das gezogene Feld bleibt (samt Loslassen)
        win._drag_app(s, chip, "start", 100, 100)  # neues Ziehen: das alte Geisterbild verschwindet
        assert not ghost.winfo_exists() and win._drag_ghost.winfo_exists()
        win._drag_app(s, chip, "drop", 5000, 5000)
        assert win._drag_ghost is None
        win.apply_state(with_stream("99"))
        root.update()
        assert not chip.winfo_exists()  # nach dem Loslassen wird neu aufgebaut
        # Loslassen ging verloren (keine Bewegung mehr): der nächste Neuaufbau räumt das Geisterbild weg
        chip = _chips(win)[0]
        win._drag_app(s, chip, "start", 100, 100)
        ghost = win._drag_ghost
        win._touched["drag"] = 0
        win.apply_state(with_stream("98"))
        root.update()
        assert not ghost.winfo_exists() and win._drag_ghost is None
    finally:
        _close(root, win)


# ==========================================================================
# apply-state-font-measure: messen mit geladener Schrift, nichts Unverändertes neu zeichnen
# ==========================================================================
@needs_x
def test_apply_state_creates_no_fonts_and_skips_unchanged_redraws(env, pactl, monkeypatch):
    from tkinter import font as tkfont

    from freestream import theme, waveui
    host, root, win = _window(env)
    try:
        st = win.state
        win.apply_state(st)
        made, draws = [], []
        orig_init = tkfont.Font.__init__
        monkeypatch.setattr(tkfont.Font, "__init__", lambda self, *a, **k: made.append(1) or orig_init(self, *a, **k))
        for cls in (theme.InfoCard, theme.SoftSegmented, waveui.FxButton):
            orig = cls.__dict__["_draw"]
            monkeypatch.setattr(cls, "_draw", lambda self, *a, _o=orig: (draws.append(type(self).__name__),
                                                                          _o(self, *a))[1])
        win.apply_state(st)
        assert made == []  # keine Schrift-Objekte, die Tk bei jedem Messen neu lädt
        assert draws == []  # Mikrofon-/Mix-Karten, „Absenken um“ und Effekte-Knöpfe unverändert
        st2 = dict(st, mic=dict(st["mic"], mute=not st["mic"].get("mute")))
        win.apply_state(st2)
        assert "InfoCard" in draws  # eine echte Änderung wird weiter gezeichnet
    finally:
        _close(root, win)


@needs_x
def test_elide_measures_with_held_font_and_few_calls(monkeypatch):
    import tkinter as tk

    from freestream import theme
    root = tk.Tk()
    try:
        spec = theme.font(root, 8)
        measure = theme.measure
        assert measure(root, spec, "Spotify") > 0
        assert (root.tk, spec) in theme._FONT_HOLD  # ein Label hält die Schrift geladen
        text = "Spotify, Firefox, Discord, Chromium, Steam, " * 4
        expect = text
        while expect and measure(root, spec, expect + "…") > 112:
            expect = expect[:-1]
        calls = []
        monkeypatch.setattr(theme, "measure", lambda w, f, t: (calls.append(t), measure(w, f, t))[1])
        assert theme.elide(root, text, spec, 112) == expect.rstrip() + "…"
        assert len(calls) <= 10  # halbiert statt einer Messung je Zeichen (hier ~160)
        assert theme.elide(root, "kurz", spec, 112) == "kurz"
    finally:
        root.destroy()


# ==========================================================================
# duck-chip-lost-update: schnelle Klicks auf die Ducking-Kanäle
# ==========================================================================
@needs_x
def test_duck_chips_keep_fast_clicks_while_worker_is_busy(env, pactl):
    from freestream import ducking
    host, root, win = _window(env)
    try:
        gate = threading.Event()
        win.worker.submit(lambda: gate.wait(5))  # z. B. gerade eine Abfrage (viele pactl-Aufrufe)
        win._toggle_duck_channel("game")
        win._toggle_duck_channel("voice")
        assert set(win._duck_chip_ids[1]) == {"music", "game", "voice"}
        gate.set()
        assert _pump(root, lambda: set(ducking.settings(host.cfg)["channels"]) == {"music", "game", "voice"})
    finally:
        _close(root, win)


# ==========================================================================
# new-channel-apps-silent: neuer Kanal mit Programmen ist zu hören
# ==========================================================================
@needs_x
def test_new_channel_with_apps_is_in_the_default_mixes(env, pactl):
    from freestream.waveui import EmptyCell
    host, root, win = _window(env)
    try:
        win._save_channel(None, {"name": "Podcast", "kind": "apps", "source": "", "icon": "chat", "color": "",
                                 "apps_add": [("spotify", "Spotify")], "apps_remove": []})
        assert _pump(root, lambda: "podcast" in win.strips and "monitor" in win.strips["podcast"], 10)
        assert {"monitor", "stream"} <= set(host.mixer.channel_mixes("podcast"))
        assert not isinstance(win.cells[("podcast", "stream")][0], EmptyCell)
        # Spotify spielt jetzt im Kanal – und der Kanal geht auf Kopfhörer und Stream
        assert pactl.inputs["7"]["sink"] == pactl._sink("linewave_podcast")
        assert set(test_waveapp._loops(pactl, "podcast")) >= {"linewave_podcast_monitor", "linewave_podcast_stream"}
        _settle(root, win)
    finally:
        _close(root, win)


# ==========================================================================
# link-gap-not-reset: Lücke für die Kette wandert mit
# ==========================================================================
@needs_x
def test_link_gap_moves_back_with_the_chain(env, pactl):
    from freestream.waveui import LINK_GAP
    host, root, win = _window(env, {"mixer": {"enabled": True, "mixes": [
        {"id": "chat", "name": "Chat", "icon": "chat", "virtual": True}]}})
    try:
        assert len(win.mix_heads) == 3 and "music" in win.link_btns
        mixes = win.state["mixes"]

        def padx(mid):
            return _padx(win.strips["music"][mid]["card"])
        assert padx("monitor") == (0, LINK_GAP) and padx("stream") == (0, 10)
        win._toggle_member("music", "monitor", False)
        win._place_links(mixes, set())
        assert padx("stream") == (0, LINK_GAP)
        win._toggle_member("music", "monitor", True)
        win._place_links(mixes, set())
        assert padx("monitor") == (0, LINK_GAP) and padx("stream") == (0, 10)
        _settle(root, win)
    finally:
        _close(root, win)


# ==========================================================================
# wizard-rerun-resets-config: Assistent erneut durchklicken ändert nichts
# ==========================================================================
@needs_x
def test_wizard_rerun_keeps_channels_effects_and_noise(env, pactl, monkeypatch):
    import tkinter as tk

    from freestream import wavesetup
    from freestream.wavehost import WaveHost
    my_eq = {"plugin": "builtin:highpass", "name": "Mein EQ", "enabled": True, "controls": {"Frequency": 140.0}}
    bass = {"plugin": "builtin:lowshelf", "name": "Bass", "enabled": True, "controls": {}}
    gate = {"plugin": GATE, "name": "Gate", "enabled": False, "controls": {"Threshold": -40}}
    chans = [{"id": "system", "name": "System"},
             {"id": "music", "name": "Spotify", "icon": "headphones", "color": "#ff4d5a"},
             {"id": "podcast", "name": "Podcast", "kind": "apps"},
             {"id": "voice", "name": "Discord", "color": "#6b74ff"}]
    host = WaveHost(_config(env, {"linewave": {"setup_done": True}, "mixer": {
        "enabled": True, "mic": "alsa_input.usb-capture", "channels": chans,
        "effects": {"mic": [my_eq, bass, gate]}, "noise": {"enabled": False}}}))
    monkeypatch.setattr(host, "enable_mixer", lambda on: None)
    data = {"inputs": [("alsa_input.usb-capture", "USB Capture")], "outputs": [("alsa_output.headphones", "Kopfhörer")],
            "catalog": CAT, "wave3": False, "noise": ["rnnoise"]}

    def saved():
        return json.loads((env / "config" / "config.json").read_text())["mixer"]
    root = tk.Tk()
    try:
        win = types.SimpleNamespace(root=root, host=host, worker=None)
        wiz = wavesetup.SetupWizard(win, data)
        for _ in range(4):  # bis „Kanäle“ durchklicken, ohne etwas umzuschalten
            wiz.next()
            root.update()
        # der Schritt „Klang“ zeigt den jetzigen Stand, nicht die Empfehlung
        assert (wiz.lowcut.get(), wiz.clipguard.get()) == (True, False)
        assert {k: v.get() for k, v in wiz.fx_choice.items()} == {"noise": False, "gate": False, "sc4m": False}
        ch = wiz.choices()
        assert ch["mic_fx_set"] == {} and "noise" not in ch
        wavesetup.apply_choices(host, ch)
        mx = saved()
        assert mx["channels"] == chans  # Namen, Symbole, Farben und Reihenfolge bleiben
        assert mx["effects"]["mic"] == [my_eq, bass, gate]
        assert mx["noise"]["enabled"] is False
        wiz.destroy()
        # zweites Mal: Low Cut aus, Kompressor an, Kanal „Game“ dazu – nur das ändert sich
        wiz = wavesetup.SetupWizard(win, data)
        wiz.lowcut.set(False)
        wiz.fx_choice["sc4m"].set(True)
        wiz.channels["game"].set(True)
        wavesetup.apply_choices(host, wiz.choices())
        mx = saved()
        assert [c["id"] for c in mx["channels"]] == ["system", "music", "podcast", "voice", "game"]
        assert mx["channels"][1] == chans[1]
        assert [f["plugin"] for f in mx["effects"]["mic"]] == ["builtin:lowshelf", GATE, COMP]
        assert mx["effects"]["mic"][1] == gate  # nicht umgeschaltet: bleibt aus, mit seinen Reglern
        wiz.destroy()
    finally:
        root.destroy()


@needs_x
def test_wizard_rerun_switching_to_wave3_drops_software_lowcut_and_limiter(env, pactl, monkeypatch):
    """Erneut aufgerufen, USB-Mikrofon mit [Low Cut, Gate, Limiter] -> Wave:3 gewählt: dessen eigener Low Cut
    und Clipguard ersetzen die Effekte, die der Assistent dafür angelegt hatte; das Gate bleibt."""
    import tkinter as tk

    from freestream import wavesetup
    from freestream.wavehost import WaveHost
    wave = "alsa_input.usb-Elgato_Systems_Elgato_Wave_3-00.mono-fallback"
    hp = {"plugin": "builtin:highpass", "name": "Low Cut", "enabled": True, "controls": {}}
    gate = {"plugin": GATE, "name": "Gate", "enabled": True, "controls": {}}
    lim = {"plugin": LIM, "name": "Limiter", "enabled": True, "controls": {}}
    host = WaveHost(_config(env, {"linewave": {"setup_done": True}, "mixer": {
        "enabled": True, "mic": "alsa_input.usb-capture", "effects": {"mic": [hp, gate, lim]}}}))
    monkeypatch.setattr(host, "enable_mixer", lambda on: None)
    data = {"inputs": [("alsa_input.usb-capture", "USB"), (wave, "Elgato Wave:3")],
            "outputs": [("alsa_output.headphones", "Kopfhörer")], "catalog": CAT, "wave3": True, "noise": []}
    root = tk.Tk()
    try:
        wiz = wavesetup.SetupWizard(types.SimpleNamespace(root=root, host=host, worker=None), data)
        wiz.mic.set(wave)
        ch = wiz.choices()
        assert ch["wave3"] and ch["mic_fx_set"] == {"builtin:highpass": False, LIM: False}
        wavesetup.apply_choices(host, ch)
        assert host.cfg.data["mixer"]["effects"]["mic"] == [gate]
        wiz.destroy()
    finally:
        root.destroy()


@needs_x
def test_colour_edit_of_unplugged_input_keeps_its_device(env, pactl, monkeypatch):
    """Ist das Gerät eines Eingangs-Kanals ausgesteckt, bleibt das Feld leer – nur die Farbe zu ändern darf das
    eingestellte Gerät trotzdem nicht löschen (_save_channel übernimmt source nur, wenn umgestellt)."""
    from freestream import wavedialogs
    from freestream.mixer import DEFAULT_CHANNELS
    _sources(monkeypatch, pactl, [("alsa_input.capture", "Capture")])
    chans = [dict(c) for c in DEFAULT_CHANNELS] + [{"id": "cam", "name": "Cam", "kind": "input",
                                                    "source": "alsa_input.gone"}]
    host, root, win = _window(env, {"mixer": {"enabled": True, "channels": chans}})
    try:
        dlgs = _spy(monkeypatch, wavedialogs, "ChannelDialog")
        win._edit_channel_id("cam")
        assert _pump(root, lambda: bool(dlgs))
        d = dlgs[0]
        assert d.dev.get() == ""
        d.color.set(wavedialogs.COLORS[0], notify=True)
        d._save()
        assert _pump(root, lambda: host.mixer.channel("cam").get("color") == wavedialogs.COLORS[0], 10)
        assert host.mixer.channel("cam")["source"] == "alsa_input.gone"
        _settle(root, win)
    finally:
        _close(root, win)


def test_merge_chain_inserts_in_wizard_order_and_keeps_own_effects():
    from freestream.wavesetup import merge_chain
    reverb = {"plugin": "lv2:reverb", "enabled": True}
    lim = {"plugin": LIM, "enabled": True, "controls": {"Limit": -3}}
    gate_off = {"plugin": GATE, "enabled": False, "controls": {"Threshold": -50}}
    new = [{"plugin": "builtin:highpass", "enabled": True}, {"plugin": GATE, "enabled": True},
           {"plugin": LIM, "enabled": True}]
    out = merge_chain([reverb, gate_off, lim], new, {"builtin:highpass": True, GATE: True})
    assert [f["plugin"] for f in out] == ["lv2:reverb", "builtin:highpass", GATE, LIM]
    assert out[2] == dict(gate_off, enabled=True) and out[3] is lim  # Regler bleiben
    assert merge_chain(out, [], {LIM: False, GATE: False, "builtin:highpass": False}) == [reverb]
    # erste Einrichtung: alle angeboten, leere Kette -> Reihenfolge des Assistenten
    assert merge_chain([], new, {f["plugin"]: True for f in new}) == new
