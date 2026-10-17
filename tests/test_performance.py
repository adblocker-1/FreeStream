"""Leistungs-Bausteine: PipeWire-Ereignisse, gebündelte pactl-Abfragen, Titel-Zwischenspeicher, Auswahlrahmen."""
import os
import time

import pytest

from freestream import render, system


def test_pulse_watch_events():
    w = system.PulseWatch()
    w._event("Event 'new' on sink-input #12\n")
    w._event("Event 'change' on sink #3\n")
    w._event("Event 'new' on client #99\n")          # eigene pactl-Aufrufe zählen nicht
    w._event("Event 'new' on source-output #7\n")    # Pegelmesser zählen nicht
    w._event("unsinn\n")
    assert w.version == 2 and w.new_streams == 1
    assert w.wait(0, 0.01) == 2  # schon neuer: kehrt sofort zurück


def test_pulse_watch_with_fake_pactl(tmp_path, monkeypatch):
    fake = tmp_path / "pactl"
    fake.write_text("#!/bin/sh\necho \"Event 'new' on sink-input #5\"\necho \"Event 'change' on sink #1\"\nsleep 5\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    w = system.PulseWatch().start()
    try:
        end = time.time() + 3
        while w.new_streams < 1 and time.time() < end:
            w.wait(w.version, 0.2)
        assert w.available and w.new_streams == 1 and w.version >= 2
    finally:
        w.stop()


def test_pactl_snapshot_runs_each_query_once(monkeypatch):
    calls = []
    monkeypatch.setattr(system, "run_out", lambda cmd, timeout=2: calls.append(cmd) or "Sink #1\n\tName: a\n")
    with system.pactl_snapshot():
        system._pactl_blocks("sinks")
        system._pactl_blocks("sinks")
        system._pactl_blocks("sources")
        system.pactl_cached(("run", "set-sink-volume"), lambda: None, write=True)  # schreibt: Speicher leer
        system._pactl_blocks("sinks")
    system._pactl_blocks("sinks")  # außerhalb: immer frisch
    assert [c[2] for c in calls] == ["sinks", "sources", "sinks", "sinks"]


def test_route_due_waits_for_new_streams():
    from freestream import wavehost
    host = wavehost.WaveHost.__new__(wavehost.WaveHost)
    host.pulse = system.PulseWatch()
    host._streams_seen, host._route_at = -1, 0.0
    assert host._route_due()          # erstes Mal immer
    host.pulse.available = True
    assert not host._route_due()      # nichts Neues
    host.pulse._event("Event 'new' on sink-input #8")
    assert host._route_due()
    assert not host._route_due()
    host.pulse.available = False      # ohne Ereignisse: wie bisher bei jedem Takt
    assert host._route_due()


def test_title_layer_cached():
    from PIL import Image
    render._TITLE_CACHE.clear()
    a = Image.new("RGBA", (72, 72), (10, 20, 30, 255))
    b = a.copy()
    render.draw_title(a, "Hallo", {"TitleColor": "#ff0000"}, 1.0)
    render.draw_title(b, "Hallo", {"TitleColor": "#ff0000"}, 1.0)
    assert len(render._TITLE_CACHE) == 1 and a.tobytes() == b.tobytes()
    assert a.getbbox() and any(px[0] > 200 for px in a.getdata())  # roter Titel gezeichnet
    render.draw_title(b, "Hallo", {"TitleColor": "#00ff00"}, 1.0)
    assert len(render._TITLE_CACHE) == 2  # andere Farbe: eigener Eintrag


def test_peak_level():
    import array
    from freestream import levels
    assert levels.peak_level(b"") == 0.0
    assert levels.peak_level(array.array("h", [0, -32768]).tobytes()) == 1.0
    half = levels.peak_level(array.array("h", [100, -16384, 50]).tobytes())
    assert 0.85 < half < 0.95  # −6 dB


@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="braucht X")
def test_select_moves_only_the_ring(tmp_path):
    import tkinter as tk

    from freestream import editdoc as E
    from freestream import editor
    store = tmp_path / "p"
    store.mkdir()
    d = E.ProfileDoc(E.create_profile("XL", "20GAT9901", store))
    d.place(d.page_ids[0], ("key", 0, 0), "com.elgato.streamdeck.system.hotkey")
    d.save()
    root = tk.Tk()
    try:
        root.geometry("1280x800")
        app = editor.EditorApp(root, store_root=store)
        root.update()
        app.select(("key", 1, 0))
        root.update()
        items = len(app.canvas.find_all())
        app.select(("key", 3, 2))
        root.update()
        assert len(app.canvas.find_all()) == items and len(app.canvas.find_withtag("sel")) == 1
        app.place_action("com.elgato.streamdeck.system.text", ("key", 3, 2))  # Inhalt geändert: neu zeichnen
        root.update()
        assert app.doc.get(app.page_uuid, ("key", 3, 2))["UUID"] == "com.elgato.streamdeck.system.text"
        assert len(app.canvas.find_withtag("sel")) == 1
    finally:
        root.destroy()


def test_pulse_watch_gives_up_when_pactl_exits_at_once(tmp_path, monkeypatch):
    fake = tmp_path / "pactl"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setattr(system.PulseWatch, "GIVE_UP", 2)
    w = system.PulseWatch().start()
    end = time.time() + 6
    while w._running and time.time() < end:
        time.sleep(0.1)
    assert not w._running and not w.available  # aufgegeben: FreeWave fragt wieder regelmäßig nach


@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="braucht X")
def test_gc_runs_in_main_thread_while_window_lives():
    import gc
    import tkinter as tk

    from freestream import theme
    root = tk.Tk()
    theme.gc_in_main_thread(root)
    assert not gc.isenabled()  # kein Aufräumen in Arbeits-Threads (Tk-Objekte dort freigeben hängt)
    end = time.time() + 1.5
    before = gc.get_count()[0]
    junk = []
    for _i in range(2000):
        a = []
        a.append(a)  # Kreisbezug: nur die Speicherbereinigung räumt ihn weg
        junk.append(a)
    del junk, a
    while time.time() < end:
        root.update()
        time.sleep(0.02)
    assert gc.get_count()[0] < before + 2000  # im Tk-Thread aufgeräumt
    root.destroy()
    assert gc.isenabled()
