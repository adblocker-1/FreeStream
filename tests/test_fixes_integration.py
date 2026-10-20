"""Regressionstests zur Integrations-Prüfung nach dem Code-Review – je Bereich ein eigener Block
(test_duck_, test_wizard_, test_trace_, test_pidfile_).

Die Oberflächentests laufen nur mit Display (xvfb-run python3 -m pytest tests/test_fixes_integration.py).
"""
import gc
import json
import os
import signal
import subprocess
import sys
import time
import types
import weakref
from pathlib import Path

import pytest

from freestream import editdoc as E
from freestream import paths

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



def test_wizard_rerun_after_finishing_with_everything_off_stays_off(env, pactl):
    """„Fertig“ mit allem aus (keine Effekte, Rauschunterdrückung aus), danach den Mixer ausgeschaltet: erneut
    geöffnet ist das ein erneuter Aufruf (die Kanäle sind geschrieben) – nicht wieder die Empfehlung."""
    import tkinter as tk

    from freestream import wavesetup
    from freestream.mixer import DEFAULT_CHANNELS
    from freestream.wavehost import WaveHost
    host = WaveHost(test_waveapp._config(env, {"linewave": {"setup_done": True}, "mixer": {
        "enabled": False, "channels": [dict(c) for c in DEFAULT_CHANNELS], "noise": {"enabled": False}}}))
    data = {"inputs": [("alsa_input.usb-capture", "USB Capture")], "outputs": [("alsa_output.headphones", "Kopfhörer")],
            "catalog": _WIZARD_CAT, "wave3": False, "noise": ["rnnoise"]}
    root = tk.Tk()
    try:
        wiz = wavesetup.SetupWizard(_wizard_types.SimpleNamespace(root=root, host=host, worker=_WIZARD_SYNC), data)
        assert (wiz.lowcut.get(), wiz.clipguard.get()) == (False, False)
        assert not any(v.get() for v in wiz.fx_choice.values())
        ch = wiz.choices()
        assert ch["mic_fx_set"] == {} and "noise" not in ch
        wiz.destroy()
    finally:
        root.destroy()

# ===================================================================================== traces
# Beobachtete Tk-Variablen (trace_add mit einer Funktion, die den Dialog kennt): Tcl hält die Funktion fest.
# dialogkit.trace löst die Beobachtung mit der Zeile oder dem Dialog – sonst lebten geschlossene Dialoge
# und jedes ersetzte Formular der Eigenschaften samt ihren Bildern weiter.
_trace_needs_x = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="braucht X (xvfb-run)")
_TRACE_WEBSITE = "com.elgato.streamdeck.system.website"  # zwei Textfelder
_TRACE_TIMER = "com.elgato.streamdeck.system.timer"  # Zahl und Datei
_TRACE_HA_STATE = "com.lindeck.homeassistant.state"  # Entität (hassui.entity_field), Text, Zahl


def _trace_pump(root, n=10):
    for _i in range(n):
        root.update()
        time.sleep(0.02)


def _trace_fields(widget, cls):
    """Namen der Tk-Variablen aller Eingabefelder der Klasse cls („Entry“, „TEntry“, „TSpinbox“) unter widget."""
    out = []
    for w in widget.winfo_children():
        if w.winfo_class() == cls and str(w.cget("textvariable")):
            out.append(str(w.cget("textvariable")))
        out += _trace_fields(w, cls)
    return out


def _trace_observed(root, name):
    """Beobachtet Tcl die Variable noch?"""
    return bool(root.tk.call("trace", "info", "variable", name))


def _trace_alive(cls):
    gc.collect()
    return [o for o in gc.get_objects() if isinstance(o, cls)]


@pytest.fixture
def _trace_app(tmp_path, monkeypatch):
    """FreeDeck-Fenster mit Konfiguration im Testordner und drei Tasten mit Text-, Zahl- und Entitätsfeldern."""
    tk = pytest.importorskip("tkinter")
    if not os.environ.get("DISPLAY"):
        pytest.skip("kein Display (xvfb-run verwenden)")
    from freestream import catalog, editor, paths
    monkeypatch.setattr(paths, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(paths, "CONFIG_FILE", tmp_path / "cfg" / "config.json")
    monkeypatch.setattr(catalog, "_title_defaults", dict(catalog._title_defaults))  # Stil-Reiter ändert sie
    monkeypatch.setattr(editor.messagebox, "askyesno", lambda *a, **k: True)
    store = tmp_path / "profiles"
    store.mkdir()
    doc = E.ProfileDoc(E.create_profile("Test", "20GBD9901", store))
    p0 = doc.page_ids[0]
    for i, uuid in enumerate((_TRACE_WEBSITE, _TRACE_TIMER, _TRACE_HA_STATE)):
        doc.place(p0, ("key", i, 0), uuid)
    doc.save()
    root = tk.Tk()
    app = editor.EditorApp(root, store_root=store)
    root.update()
    yield app
    if root.winfo_exists():
        app.close()


@_trace_needs_x
def test_trace_icon_library_freed_after_close():
    """Icon-Bibliothek dreimal öffnen und schließen: jede wird samt ihren rund 100 Kachelbildern freigegeben."""
    import tkinter as tk

    from freestream.iconlibui import IconLibrary
    root = tk.Tk()
    try:
        refs, images = [], []
        for _i in range(3):
            dlg = IconLibrary(root, on_pick=lambda n, s: None, on_file=lambda: None)
            _trace_pump(root)
            assert len(dlg._photos) > 50  # Kacheln gezeichnet
            dlg.query.set("kopf")  # die Suche wirkt, solange das Fenster offen ist
            root.update()
            assert dlg.grids and all(found == ["headphones", "headphones_off"] for _g, found in dlg.grids.values())
            refs.append(weakref.ref(dlg))
            dlg.destroy()
            del dlg
            root.update()
            gc.collect()
            images.append(len(root.tk.call("image", "names")))
        assert [r() for r in refs] == [None, None, None]
        assert images[2] == images[0]  # keine Bilder je Öffnen übrig
    finally:
        root.destroy()


@_trace_needs_x
def test_trace_settings_connections_and_style_freed_after_close(_trace_app, monkeypatch, tmp_path):
    """Einstellungen mit den Reitern Geräte, Verbindungen (alle Dienste, Zuordnungs-Paare) und Stil: Felder wirken,
    beim Wechsel des Dienstes endet die Beobachtung der alten Felder, das geschlossene Fenster wird freigegeben."""
    from test_settings_tabs import FakeCtrl

    from freestream import desktop, plugins
    from freestream.config import Config
    from freestream.settingsui import open_settings
    app = _trace_app
    root = app.root
    monkeypatch.setattr(desktop, "SYSTEM_AUTOSTART", tmp_path / "sys-autostart")
    monkeypatch.setattr(desktop, "installed_apps", lambda: [])
    monkeypatch.setattr(plugins, "installed", lambda: {})
    cfg = Config()
    ctrl = FakeCtrl("CL12K1A01234", "20GAT9901", app.store)
    app.daemon = types.SimpleNamespace(controllers={"a": ctrl}, cfg=cfg, plugin_host=None, store=app.store)
    app.device = ctrl.serial
    refs = []
    try:
        for n in range(2):
            cfg.data["app_map"] = {"obs64.exe": "obs"}
            dlg = open_settings(root, "FreeDeck", lambda: None, freedeck=app)
            _trace_pump(root, 5)
            dlg.show_tab("devices")
            dlg.pages["devices"].name.set(f"Studio {n}")
            assert cfg.device(ctrl.serial)["name"] == f"Studio {n}"
            dlg.show_tab("connections")
            page = dlg.pages["connections"]
            page.select("obs")
            page.obs_host.set(f"10.0.0.{n + 2}")
            assert cfg.data["obs"]["host"] == f"10.0.0.{n + 2}"
            old = str(page.obs_host)
            for key, *_r in page.services():
                page.select(key)
                root.update()
            assert not _trace_observed(root, old)  # Zeile neu aufgebaut: alte Beobachtung beendet
            pair = [v for v in _trace_fields(page, "Entry") if root.getvar(v) == "obs"]
            assert len(pair) == 1  # Erweitert: Zuordnung obs64.exe → obs
            root.setvar(pair[0], "obs --minimize")
            assert cfg.data["app_map"] == {"obs64.exe": "obs --minimize"}
            dlg.show_tab("style")
            style = dlg.pages["style"]
            style.size.set(str(15 + n))
            assert cfg.data["title_style"]["FontSize"] == 15 + n
            refs.append(weakref.ref(dlg))
            dlg.destroy()
            del dlg, page, style
            root._settings_dialog = None  # merkt sich das zuletzt geöffnete Fenster
            _trace_pump(root, 3)
        gc.collect()
        assert [r() for r in refs] == [None, None]
    finally:
        app.daemon = None


@_trace_needs_x
def test_trace_action_forms_freed_on_select(_trace_app):
    """Tasten nacheinander wählen (Darstellung und Aktion): die Felder schreiben in die Taste, und jedes
    ersetzte Formular wird freigegeben – nur das gerade gezeigte lebt."""
    from freestream import editor
    app = _trace_app
    root, page = app.root, app.page_uuid
    slots = [("key", i, 0) for i in range(3)]
    old = []
    for tab in ("appearance", "action"):
        app.set_inspector_tab(tab)
        for i in range(9):
            app.select(slots[i % 3])
            root.update()
            old += [weakref.ref(f) for f in _trace_alive(editor.ActionForm)]
    assert len(_trace_alive(editor.ActionForm)) == 1
    assert sum(r() is not None for r in old) == 1  # das gezeigte Formular

    # Aktion: Text- und Zahlenfelder samt Entität schreiben weiter in die gewählte Taste
    app.select(slots[0])
    root.setvar(_trace_fields(app.inspector.inner, "TEntry")[0], "https://example.org")
    assert app.doc.get(page, slots[0])["Settings"]["path"] == "https://example.org"
    app.select(slots[1])
    root.setvar(_trace_fields(app.inspector.inner, "TSpinbox")[0], "42")
    assert app.doc.get(page, slots[1])["Settings"]["duration"] == 42
    app.select(slots[2])
    root.setvar(_trace_fields(app.inspector.inner, "TEntry")[0], " light.kueche ")
    assert app.doc.get(page, slots[2])["Settings"]["entity_id"] == "light.kueche"
    names = _trace_fields(app.inspector.inner, "TEntry") + _trace_fields(app.inspector.inner, "TSpinbox")
    assert names and all(_trace_observed(root, v) for v in names)
    app.select(slots[0])
    assert not any(_trace_observed(root, v) for v in names)  # Formular ersetzt: Beobachtung beendet
    # Darstellung: Schriftgröße (T⌄ neben dem Titel)
    app.set_inspector_tab("main")
    app._toggle_title_panel(app._insp_form, app.insp_head)
    size = _trace_fields(app.title_panel.body, "TSpinbox")
    assert len(size) == 1
    root.setvar(size[0], "17")
    assert app.doc.get(page, slots[0])["States"][0]["FontSize"] == 17
    app.select(slots[1])
    assert not _trace_observed(root, size[0])
    gc.collect()
    assert len(_trace_alive(editor.ActionForm)) == 1


@_trace_needs_x
def test_trace_child_dialogs_freed_after_close(_trace_app):
    """Verschachtelte Aktion (Multi-Aktion) mit Text- und Zahlenfeld bearbeiten und schließen: freigegeben."""
    from freestream import catalog as C
    from freestream import editor
    app = _trace_app
    refs = []
    for uuid, cls, key, text, value in ((_TRACE_WEBSITE, "TEntry", "path", "https://example.org", "https://example.org"),
                                        ("com.elgato.streamdeck.multiactions.delay", "TSpinbox", "delay", "250", 250)):
        data = C.new_action(uuid)
        dlg = editor.ChildDialog(app, data, app.page_uuid, ("key", 0, 0))
        app.root.update()
        app.root.setvar(_trace_fields(dlg, cls)[0], text)
        assert data["Settings"][key] == value
        refs.append(weakref.ref(dlg))
        dlg.done()
        del dlg
        app.root.update()
    gc.collect()
    assert [r() for r in refs] == [None, None]


@_trace_needs_x
def test_trace_entity_picker_and_colour_box_freed_after_close(tmp_path, monkeypatch):
    """Entitätsauswahl (Home Assistant) und Farbwahl: Suche bzw. Hex-Feld wirken, geschlossen freigegeben."""
    import tkinter as tk

    from freestream import hassui, msgbox, paths
    monkeypatch.setattr(paths, "CONFIG_DIR", tmp_path / "cfg")  # Home Assistant nicht eingerichtet
    monkeypatch.setattr(paths, "CONFIG_FILE", tmp_path / "cfg" / "config.json")
    root = tk.Tk()
    try:
        dlg = hassui.EntityPicker(root, None, ["light"])
        dlg.items = [("light.kueche", "Küche", "an"), ("light.flur", "Flur", "aus")]
        dlg.query.set("kü")
        assert [s[0] for s in dlg.shown] == ["light.kueche"]
        ref = weakref.ref(dlg)
        dlg.destroy()
        del dlg

        boxes = []
        orig = msgbox._ColorBox.__init__

        def spy(self, *a, **k):
            boxes.append(weakref.ref(self))
            orig(self, *a, **k)
        monkeypatch.setattr(msgbox._ColorBox, "__init__", spy)

        seen = []

        def typed():
            box = boxes[-1]() if boxes else None
            if box is None or not getattr(box, "dlg", None) or not box.dlg.winfo_exists():
                root.after(20, typed)
                return
            try:
                box.var.set("#123abc")  # Hex-Feld: Farbe übernommen
                seen.append(box.color)
            finally:  # nie hängen bleiben
                box.ok()
        root.after(20, typed)
        assert msgbox.askcolor(color="#ffffff", parent=root) == ((0x12, 0x3a, 0xbc), "#123abc")
        assert seen == ["#123abc"]
        root.update()
        gc.collect()
        assert ref() is None and [b() for b in boxes] == [None]
    finally:
        root.destroy()


# ==========================================================================
# pidfile: liegengebliebene freestream.pid (Startzeit statt Name in der Befehlszeile)
# ==========================================================================
@pytest.fixture
def _pidfile_home(tmp_path, monkeypatch):
    state = tmp_path / "state" / "freestream"  # wie ~/.local/state/freestream: „freestream“ im Pfad
    for name, sub in (("CONFIG_DIR", "cfg"), ("DATA_DIR", "data"), ("PROFILES_DIR", "data/profiles")):
        monkeypatch.setattr(paths, name, tmp_path / sub)
    monkeypatch.setattr(paths, "STATE_DIR", state)
    monkeypatch.setattr(paths, "PID_FILE", state / "freestream.pid")
    state.mkdir(parents=True)
    return tmp_path


def _pidfile_proc(*extra):
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", *extra])


def _pidfile_readers():
    """Was jeder Leser der Pid-Datei für FreeDeck hält (ohne Signale zu schicken)."""
    from freestream import app, backup
    from freestream.daemon import Daemon
    return Daemon.already_running(), app.running_instance(), backup._lindeck_pid()[0]


def _pidfile_stop(procs):
    for p in procs:
        p.kill()
        p.wait()


def test_pidfile_daemon_start_writes_start_time(_pidfile_home, monkeypatch):
    from freestream import desktop, plugins
    from freestream.daemon import Daemon
    monkeypatch.setattr(plugins, "PluginHost", lambda daemon: None)
    fake = types.SimpleNamespace(_auto_import=lambda: None,
                                 store=types.SimpleNamespace(reload=lambda: None, profiles={}))
    Daemon.start(fake)
    pid = os.getpid()
    assert paths.PID_FILE.read_text().split() == [str(pid), str(desktop._start_time(pid))]
    assert _pidfile_readers() == (None, None, None)  # nie der eigene Prozess


def test_pidfile_stale_file_does_not_hit_freewave_or_effect_chain(_pidfile_home):
    from freestream import app, backup, deckexport, desktop
    from freestream.__main__ import _signal_daemon
    procs = [_pidfile_proc("-m", "freestream.freewave", "--hidden"),  # FreeWave, wie der Starter des Pakets
             _pidfile_proc("-c", str(paths.STATE_DIR / "effects-mic.conf"))]  # wie pipewire -c …/effects-*.conf
    try:
        for p in procs:
            start = desktop._start_time(p.pid)
            assert start
            # FreeDeck abgestürzt, seine Pid hat jetzt FreeWave/die Effektkette: alte und neue Datei
            for content in (str(p.pid), f"{p.pid} {start - 1}"):
                paths.PID_FILE.write_text(content)
                assert _pidfile_readers() == (None, None, None)
                assert backup.running_programs() == []
                assert not deckexport.notify_freedeck()
                assert not _signal_daemon()
                assert not app.quit_running()
        for p in procs:
            with pytest.raises(subprocess.TimeoutExpired):
                p.wait(0.3)  # weder SIGHUP noch SIGTERM bekommen
    finally:
        _pidfile_stop(procs)


def test_pidfile_legacy_file_accepts_only_freedeck_command_line(_pidfile_home):
    cases = [(("-m", "freestream", "--hidden"), True),  # Starter des Pakets: python3 -m freestream
             (("/usr/local/bin/freedeck", "run", "--no-gui"), True),  # pip: …/bin/freedeck
             (("-m", "freestream", "freewave", "--hidden"), False),  # „freedeck freewave“ startet FreeWave
             (("/usr/local/bin/lindeck", "-v", "linewave"), False),  # alter Befehl, ebenfalls FreeWave
             (("/usr/local/bin/freewave", "--hidden"), False),
             (("-m", "freestream.freewave"), False),
             (("-c", str(paths.STATE_DIR / "effects-mic.conf")), False)]
    procs = [_pidfile_proc(*extra) for extra, __ in cases]
    try:
        for p, (extra, ours) in zip(procs, cases):
            paths.PID_FILE.write_text(str(p.pid))  # Datei einer älteren Version (ohne Startzeit)
            expected = p.pid if ours else None
            assert _pidfile_readers() == (expected, expected, expected), extra
    finally:
        _pidfile_stop(procs)


def test_pidfile_legacy_status_files_accept_only_their_own_program(_pidfile_home):
    """app.json/freewave.json einer älteren Version (ohne Startzeit) nach einem Absturz: die Pid hat jetzt das
    jeweils andere Programm oder eine Effektkette – kein Leser hält es für sein Programm."""
    import json

    from freestream import app, backup, desktop
    freedeck = _pidfile_proc("-m", "freestream", "--hidden")
    freewave = _pidfile_proc("-m", "freestream.freewave", "--hidden")
    chain = _pidfile_proc("-c", str(paths.STATE_DIR / "effects-mic.conf"))
    procs = [freedeck, freewave, chain]

    def legacy(name, proc):
        (paths.STATE_DIR / name).write_text(json.dumps({"pid": proc.pid, "gui": False}))
    try:
        for other in (freewave, chain):
            legacy("app.json", other)
            assert app.running_instance() is None and backup._lindeck_pid()[0] is None
        for other in (freedeck, chain):
            legacy("freewave.json", other)
            assert desktop.running_instance("freewave.json") is None
        legacy("app.json", freedeck)
        legacy("freewave.json", freewave)
        assert app.running_instance() == freedeck.pid
        assert desktop.running_instance("freewave.json") == freewave.pid
        assert backup.running_programs() == [("freedeck", freedeck.pid, False), ("freewave", freewave.pid, False)]
    finally:
        _pidfile_stop(procs)


def test_pidfile_running_freedeck_found_by_start_time(_pidfile_home):
    from freestream import app, backup, deckexport, desktop
    code = ("import sys, time; from pathlib import Path; from freestream import desktop, paths; "
            "paths.PID_FILE = Path(sys.argv[1]); desktop._write_pid_file(); print('ok', flush=True); time.sleep(30)")
    proc = subprocess.Popen([sys.executable, "-c", code, str(paths.PID_FILE)], stdout=subprocess.PIPE,
                            cwd=Path(__file__).resolve().parents[1], text=True)
    try:
        assert proc.stdout.readline().strip() == "ok"
        assert paths.PID_FILE.read_text() == f"{proc.pid} {desktop._start_time(proc.pid)}"
        assert _pidfile_readers() == (proc.pid, proc.pid, proc.pid)  # Befehlszeile egal, die Startzeit passt
        assert backup.running_programs() == [("freedeck", proc.pid, False)]
        assert deckexport.notify_freedeck()
        assert proc.wait(5) == -signal.SIGHUP  # SIGHUP kam an (der Hilfsprozess fängt es nicht ab)
        assert _pidfile_readers() == (None, None, None)  # Datei liegt noch, der Prozess ist weg
        assert not app.quit_running()
    finally:
        _pidfile_stop([proc])
