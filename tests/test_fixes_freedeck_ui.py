"""Fehler aus dem Code-Review der FreeDeck-Oberfläche: Assistent, Ausklapplisten, Ziehen auf andere Seiten,
virtuelles Deck, Bildspeicher, Aktionsliste, Deckkraft und Mausrad.

Laufen nur mit Display (xvfb-run python3 -m pytest tests/test_fixes_freedeck_ui.py).
"""
import gc
import os
import shutil
import subprocess
import sys
import time
import types
import weakref
from pathlib import Path

import pytest
from PIL import Image

import test_editor
from freestream import editdoc as E

store, doc, app = test_editor.store, test_editor.doc, test_editor.app  # Fixtures
needs_x = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="braucht X (xvfb-run)")
ROOT = Path(__file__).resolve().parent.parent
HOTKEY = "com.elgato.streamdeck.system.hotkey"
TEXT = "com.elgato.streamdeck.system.text"


def _pump(root, n=10):
    for _i in range(n):
        root.update()
        time.sleep(0.02)


def _until(root, cond, secs=5.0):
    end = time.time() + secs
    while not cond() and time.time() < end:
        root.update()
        time.sleep(0.02)
    return cond()


class Ev:
    """Mausereignis auf der Bühne (x/y relativ zur Leinwand)."""

    def __init__(self, cv, x, y):
        self.x, self.y = x, y
        self.x_root, self.y_root = x + cv.winfo_rootx(), y + cv.winfo_rooty()


def _drag_key(app, slot, x_root, y_root):
    """Taste der Bühne mit der Maus nach (x_root, y_root) ziehen und loslassen."""
    cv = app.canvas
    x0, y0 = next((a, b) for a, b, _c, _d, s in app._hit if s == slot)
    app._canvas_press(Ev(cv, x0 + 4, y0 + 4))
    app._canvas_motion(Ev(cv, x0 + 40, y0 + 40))
    app._canvas_release(Ev(cv, x_root - cv.winfo_rootx(), y_root - cv.winfo_rooty()))


# ------------------------------------------------------------------ Assistent
def test_setup_wizard_keeps_edited_profiles(app, store, tmp_path, monkeypatch):
    """Assistent aus den Einstellungen: der alte Export in Downloads ersetzt das bearbeitete Profil nicht."""
    from freestream import decksetup
    monkeypatch.setattr(decksetup, "find_profiles", lambda: [])
    app._settings_cfg.save = lambda: None  # „Einrichtung erledigt“ nicht in die echte Konfiguration schreiben
    (tmp_path / "Downloads").mkdir()
    old = app.doc.export(tmp_path / "Downloads" / "Test.streamDeckProfile")
    pid, page = app.doc.id, app.page_uuid
    app.doc.rename("Bearbeitet")
    app.doc.place(page, ("key", 1, 0), TEXT)
    app.save_now()
    before = app.doc

    dlg = app.open_setup()
    dlg.found = [old]
    dlg.step = 2
    dlg.render()
    dlg.next()  # übernehmen
    assert dlg.step == 3 and dlg.imported == [pid]
    on_disk = E.ProfileDoc(store / f"{pid}.sdProfile")
    assert on_disk.name == "Bearbeitet" and on_disk.get(page, ("key", 1, 0))["UUID"] == TEXT
    dlg.next()  # fertig: das offene Profil bleibt offen (samt Rückgängig)
    assert app.doc is before and app.doc.name == "Bearbeitet"

    # eine neue Datei dazu: die kommt hinzu und wird geöffnet, das bearbeitete Profil bleibt
    src = tmp_path / "src"
    src.mkdir()
    new = E.ProfileDoc(E.create_profile("Neu", "20GBD9901", src))
    new_file = new.export(tmp_path / "Downloads" / "Neu.streamDeckProfile")
    dlg = app.open_setup()
    dlg.found = [old, new_file]
    dlg.step = 2
    dlg.render()
    dlg.next()
    assert dlg.step == 3 and dlg.imported == [new.id, pid]
    dlg.next()
    assert app.doc.id == new.id
    assert E.ProfileDoc(store / f"{pid}.sdProfile").name == "Bearbeitet"

    # noch einmal, nichts Neues dabei (das andere Profil zuerst): das offene bleibt offen, samt Rückgängig
    before = app.doc
    dlg = app.open_setup()
    dlg.found = [old, new_file]
    dlg.step = 2
    dlg.render()
    dlg.next()
    assert dlg.step == 3 and dlg.imported == [pid, new.id] and dlg.new_ids == []
    dlg.next()
    assert app.doc is before


# ------------------------------------------------------------ Ausklapplisten
def test_drop_panel_closes_for_menu_and_hidden_window(app, monkeypatch):
    from freestream import desktop, editor, theme
    monkeypatch.setattr(desktop, "notify", lambda *a, **k: None)
    root, panel = app.root, app.prof_panel
    app._toggle_drop("prof")
    _pump(root)
    assert panel.winfo_ismapped()
    # Rechtsklick auf ein Profil der Liste: das Menü nimmt den Grab – die Liste schließt vorher
    m = app._profile_context(app.doc.id, 200, 200)
    assert not panel.winfo_ismapped() and theme.PopupMenu.current is m
    m.close()
    # „Neues Profil“ unten in der Liste: die Liste schließt, bevor der Dialog aufgeht
    app._toggle_drop("prof")
    _pump(root)
    seen = []

    def cancel():
        seen.append(panel.winfo_ismapped())
        next(w for w in root.winfo_children() if isinstance(w, editor.NewProfileDialog)).destroy()
    root.after(300, cancel)
    [w for w in app.profile_list.inner.winfo_children() if isinstance(w, theme.NavItem)][-1].command()
    assert seen == [0] and not panel.winfo_ismapped()
    # Fenster ausgeblendet/minimiert (<Unmap>): Liste nicht über dem Desktop stehen lassen
    app._toggle_drop("prof")
    _pump(root)
    assert panel.winfo_ismapped()
    root.withdraw()
    _pump(root)
    assert not panel.winfo_ismapped() and not root.tk.call("grab", "current")
    root.deiconify()
    _pump(root)
    # Fenster schließen (App-Modus: es läuft im Tray weiter)
    app._toggle_drop("prof")
    _pump(root)
    assert panel.winfo_ismapped()
    app.hide()
    assert not panel.winfo_ismapped()


def test_drop_panel_click_outside_closes(app):
    """Die Liste hält keinen Grab: ein Klick irgendwo im Programm schließt sie, einer in die Liste nicht."""
    root, prof, dev = app.root, app.prof_panel, app.dev_panel
    app._toggle_drop("prof")
    _pump(root)
    assert prof.winfo_ismapped() and not root.tk.call("grab", "current")
    prof.body.event_generate("<ButtonPress-1>", x=5, y=5)
    assert prof.winfo_ismapped()
    app.canvas.event_generate("<ButtonPress-1>", x=5, y=5)
    assert not prof.winfo_ismapped()
    # Klick auf das andere Auswahlfeld: dessen Liste klappt auf, die offene zu; ein zweiter Klick schließt
    app._toggle_drop("dev")
    _pump(root)
    app.prof_dd.event_generate("<Button-1>", x=5, y=5)
    assert prof.winfo_ismapped() and not dev.winfo_ismapped()
    app.prof_dd.event_generate("<Button-1>", x=5, y=5)
    assert not prof.winfo_ismapped()


ALT_TAB = """
import subprocess
import sys
import time
import tkinter as tk

from freestream import editdoc as E
from freestream import editor

OTHER = ("import tkinter as tk; r = tk.Tk(); r.geometry('200x200+700+600'); "
         "r.after(200, lambda: print(r.wm_frame(), flush=True)); r.after(8000, r.destroy); r.mainloop()")
E.create_profile("Test", "20GBD9901", sys.argv[1])
root = tk.Tk()
app = editor.EditorApp(root, store_root=sys.argv[1])
panel = app.prof_panel


def pump(secs=0.3):
    end = time.time() + secs
    while time.time() < end:
        root.update()
        time.sleep(0.02)


def xdotool(*args):
    subprocess.run(["xdotool", *args], check=True, timeout=10)


pump()
root.focus_force()  # wie der Fenstermanager nach dem Klick ins Fenster
pump()
app._toggle_drop("prof")
pump()
print("open", panel.winfo_ismapped())
xdotool("key", "Escape")
pump()
print("esc", panel.winfo_ismapped())
app._toggle_drop("prof")
pump()
other = subprocess.Popen([sys.executable, "-c", OTHER], stdout=subprocess.PIPE, text=True)
try:
    xdotool("windowfocus", "--sync", str(int(other.stdout.readline(), 0)))  # wie Alt+Tab
    pump()
    print("other", panel.winfo_ismapped())
finally:
    other.kill()
root.destroy()
"""


@needs_x
@pytest.mark.skipif(not shutil.which("xdotool"), reason="braucht xdotool")
def test_drop_panel_closes_when_other_program_gets_focus(store, tmp_path):
    """Esc (Tastatur geht auch ohne Grab an die Liste) und Alt+Tab in ein anderes Programm schließen sie.
    Als eigenes Programm: nach einem Fokuswechsel zu einem anderen X-Programm stürzt focus_force in einer
    späteren Tk-Instanz desselben Prozesses ab (Tk 8.6) – das träfe die folgenden Tests."""
    script = tmp_path / "alt_tab.py"
    script.write_text(ALT_TAB)
    out = subprocess.run([sys.executable, str(script), str(store)], capture_output=True, text=True, timeout=120,
                         env=dict(os.environ, PYTHONPATH=str(ROOT)))
    assert out.stdout.splitlines()[:3] == ["open 1", "esc 0", "other 0"], out.stdout + out.stderr


# ------------------------------------------------------- auf eine Seite ziehen
def test_drag_key_onto_page_dots_moves_it(app):
    first = app.page_uuid
    second = app.doc.add_page()
    app.goto_page(first)
    app.root.update()
    dots = app.page_dots
    _drag_key(app, ("key", 0, 0), dots.winfo_rootx() + dots._x(1), dots.winfo_rooty() + dots.H // 2)
    assert app.doc.get(first, ("key", 0, 0)) is None
    assert app.doc.get(second, ("key", 0, 0))["UUID"] == HOTKEY
    # „‹“ steht für die Seite davor
    app.goto_page(second)
    app.root.update()
    dots = app.page_dots
    _drag_key(app, ("key", 0, 0), dots.winfo_rootx() + 10, dots.winfo_rooty() + dots.H // 2)
    assert app.doc.get(first, ("key", 0, 0))["UUID"] == HOTKEY
    assert app.doc.get(second, ("key", 0, 0)) is None


# -------------------------------------------------------------- Bildspeicher
class LiveCtrl:
    """Gerät, das gerade die bearbeitete Seite zeigt – mit Live-Tasten (Pegel, Uhr)."""

    serial, model, brightness, page_index, folder_stack = "A1", "20GBD9901", 70, 0, []

    def __init__(self, doc):
        from freestream.models import model_geometry
        self.geometry = model_geometry(self.model)
        self.doc = doc
        self.profile = types.SimpleNamespace(id=doc.id)
        self.shown, self.shown_seq = {}, 0

    def current_page(self):
        return types.SimpleNamespace(uuid=self.doc.page_ids[0])

    def live_image(self, k):
        return self.shown.get(k)

    def load_profile(self, *a):
        pass

    def goto_page(self, *a):
        pass

    def candidate_profiles(self):
        return []


@needs_x
def test_live_keys_do_not_fill_image_cache(store, doc):
    import tkinter as tk

    from freestream import editor
    from freestream.config import Config
    for c in range(5):
        for r in range(3):
            doc.place(doc.page_ids[0], ("key", c, r), HOTKEY)
    doc.save()
    ctrl = LiveCtrl(doc)
    cfg = Config()
    cfg.save = lambda: None
    daemon = types.SimpleNamespace(controllers={"a": ctrl}, cfg=cfg, plugin_host=None, store=None)
    root = tk.Tk()
    try:
        app = editor.EditorApp(root, store_root=store, daemon=daemon)
        _pump(root)
        sizes, static = [], None
        for poll in range(40):  # alle 300 ms ein neues Bild je Live-Taste
            for k in range(8):
                ctrl.shown[k] = Image.new("RGB", (72, 72), (poll, k * 20, 0))
            ctrl.shown_seq += 1
            app.redraw()
            sizes.append((len(app._img_cache), len(app._tk_cache)))
            if static is None:
                static = {k: v for k, v in app._img_cache.items() if k[0] != "live"}
        assert sizes[-1] == sizes[0]  # nichts sammelt sich an
        assert static and all(app._img_cache.get(k) is v for k, v in static.items())  # feste Bilder bleiben
    finally:
        root.destroy()


def _with_background(app, tmp_path):
    pic = tmp_path / "bg.png"
    Image.effect_noise((800, 450), 60).convert("RGB").save(pic)
    app.doc.set_background(app.page_uuid, str(pic))
    app.save_now()
    app.redraw()
    assert app._tiles


def test_background_tiles_cached(app, tmp_path, monkeypatch):
    from freestream import editor
    _with_background(app, tmp_path)
    made = []
    orig = editor.ImageTk.PhotoImage
    monkeypatch.setattr(editor.ImageTk, "PhotoImage", lambda *a, **k: (made.append(1), orig(*a, **k))[1])
    app.redraw()
    assert made == []  # leere Tasten auf dem Hintergrund: gemerkte Kachel, kein neues Tk-Bild


def test_background_tiles_one_entry_per_key_when_resizing(app, tmp_path):
    """Fenster größer ziehen: je Taste bleibt eine gemerkte Kachel, das Tk-Bild der vorigen Größe geht weg."""
    _with_background(app, tmp_path)
    root, sizes = app.root, set()
    for w in range(900, 1300, 40):
        root.geometry(f"{w}x{w * 3 // 4}")
        root.update()
        app.redraw()
        sizes.add(app._hit[0][2] - app._hit[0][0])
    assert len(sizes) > 2  # die Tasten wurden wirklich größer
    g = app.doc.geometry
    assert 0 < sum(1 for k in app._img_cache if k[0] == "tile") <= g.cols * g.rows
    cached = {id(v[1]) if isinstance(v, tuple) else id(v) for v in app._img_cache.values()}
    assert all(k in cached for k in app._tk_cache)  # kein Tk-Bild zu einem nicht mehr gemerkten Bild


def test_pedal_ignores_background_tiles(app, store, tmp_path):
    _with_background(app, tmp_path)
    # danach ein Pedal: ohne die Kacheln der vorigen Seite (sonst falsche Farbe und Größe)
    pedal = E.ProfileDoc(E.create_profile("Pedal", "20GBF9901", store))
    pedal.place(pedal.page_ids[0], ("key", 1, 0), HOTKEY)
    pedal.save()
    app.open_profile(pedal.id)
    app.root.update()
    assert app._tiles == {} and app._tiles_id is None
    data = app.doc.get(app.page_uuid, ("key", 1, 0))
    assert app._render(("key", 1, 0), data, (60, 60)).size == (60, 60)


# ------------------------------------------------------------ Eigenschaften
def test_opacity_is_one_undo_step_for_all_states(app):
    import tkinter as tk

    from freestream import editor
    page, slot = app.page_uuid, ("key", 1, 0)
    app.place_action("com.elgato.streamdeck.system.hotkeyswitch", slot)
    data = app.doc.get(page, slot)
    assert len(data["States"]) == 2
    form = editor.ActionForm(app, app.inspector.inner, data, page, slot)
    calls = []
    orig = app.doc.on_change
    app.doc.on_change = lambda: (calls.append(1), orig())
    n0 = len(app.doc.undo_stack)
    var = tk.StringVar(app.root)
    for v in (80, 60, 40):  # Regler ziehen: jede Stufe wird nach 60 ms übernommen
        form._set_opacity(v, var)
        assert _until(app.root, lambda: form._opacity_job is None)
    assert len(app.doc.undo_stack) - n0 == 1 and len(calls) == 3
    assert [s["IconOpacity"] for s in app.doc.get(page, slot)["States"]] == [40, 40]
    app.undo()  # ein Schritt zurück: alle Zustände wieder wie vorher
    assert [s.get("IconOpacity", 100) for s in app.doc.get(page, slot)["States"]] == [100, 100]


# ------------------------------------------------------------ Aktionsliste
def test_category_toggle_keeps_scroll_position(app):
    from freestream import editor
    root, cv = app.root, app.library.canvas
    _pump(root)
    cv.yview_moveto(1.0)
    _pump(root)
    top = cv.canvasy(0)
    assert top > 0  # die Liste ist länger als ihr Bereich
    last = [w for w in app.library.inner.winfo_children() if isinstance(w, editor.CategoryRow)][-1]
    assert not last.open
    app.toggle_category(last.cat)
    _pump(root)
    assert cv.canvasy(0) == top  # bleibt stehen …
    row = next(w for w in app.library.inner.winfo_children()
               if isinstance(w, editor.CategoryRow) and w.cat == last.cat)
    assert row.open and top <= row.winfo_y() <= top + cv.winfo_height()  # … und die Kategorie ist zu sehen
    app.search_var.set("zeit")  # neue Suche: wieder von oben
    _pump(root)
    assert cv.yview()[0] == 0


# ---------------------------------------------------------------- Mausrad
def test_wheel_handlers_bound_once_and_areas_freed(app):
    import tkinter as tk

    from freestream import editor
    from freestream.dialogkit import ScrollArea
    root = app.root

    def handlers(seq):
        return root.bind_all(seq).count("if {")

    def make(cls):
        top = tk.Toplevel(root)
        top.geometry("300x200")
        area = cls(top)
        area.pack(fill="both", expand=True)
        for i in range(60):
            tk.Label(area.inner, text=f"Zeile {i}").pack()
        return top, area

    for cls in (editor.ScrollFrame, ScrollArea):
        top, area = make(cls)
        _pump(root)
        cv = area.canvas if cls is editor.ScrollFrame else area.cv
        label = area.inner.winfo_children()[3]
        assert cv.yview()[0] == 0
        label.event_generate("<Button-5>", x=2, y=2)  # Rad über dem Inhalt: scrollt
        assert cv.yview()[0] > 0
        label.event_generate("<Button-4>", x=2, y=2)
        assert cv.yview()[0] == 0
        top.destroy()
    counts = {seq: handlers(seq) for seq in ("<Button-4>", "<Button-5>", "<MouseWheel>")}
    refs = []
    for cls in (editor.ScrollFrame, ScrollArea) * 5:  # Dialoge öffnen und schließen
        top, area = make(cls)
        top.update()
        refs.append(weakref.ref(area))
        top.destroy()
    del top, area
    gc.collect()
    assert {seq: handlers(seq) for seq in counts} == counts  # kein Handler je Bereich an „all“
    assert not [r for r in refs if r() is not None]  # geschlossene Bereiche werden freigegeben
    # Rad danach irgendwo im Fenster: keine Fehler von toten Bereichen
    errors = []
    root.report_callback_exception = lambda *a: errors.append(a[1])
    app.canvas.event_generate("<Button-5>", x=5, y=5)
    root.update()
    assert errors == []


@needs_x
def test_pickers_freed_after_close():
    """Suchfeld der Auswahl-Dialoge (FreeWave): die Beobachtung endet mit dem Dialog – sonst hielte Tcl die
    Funktion fest und mit ihr den Dialog samt seinen Bildern."""
    import tkinter as tk

    from freestream import effects
    from freestream.wavedialogs import AppPicker, PluginPicker
    apps = {"running": [], "elsewhere": {},
            "installed": [{"key": "firefox", "cls": "firefox", "name": "Firefox", "icon": "", "icon_path": None},
                          {"key": "vlc", "cls": "vlc", "name": "VLC", "icon": "", "icon_path": None}]}
    root = tk.Tk()
    try:
        refs = []
        pk = AppPicker(root, apps, set(), lambda a: None)
        pk.query.set("fire")  # Suche wirkt weiter
        assert [a["key"] for _t, items in pk.candidates() for a in items] == ["firefox"]
        refs.append(weakref.ref(pk))
        pk.destroy()
        pk = PluginPicker(root, [effects.lookup("builtin:highpass")], lambda p: None)
        pk.q.set("high")
        refs.append(weakref.ref(pk))
        pk.destroy()
        del pk
        root.update()
        gc.collect()
        assert [r() for r in refs] == [None, None]
    finally:
        root.destroy()


# --------------------------------------------------------- virtuelles Deck
@needs_x
def test_virtual_deck_does_not_cover_its_menu():
    import tkinter as tk
    from tkinter import ttk

    from freestream import theme
    from freestream import virtualdeck as V
    from freestream.models import Geometry
    from freestream.virtualui import VirtualDeckWindow

    class Deck:
        seq = 1

        def snapshot(self):
            return 1, {k: Image.new("RGB", (96, 96), (k * 30, 0, 0)) for k in range(8)}, 100

        def press(self, k, p):
            pass
    profs = [types.SimpleNamespace(id=f"p{i}", name=f"Profil {i}") for i in range(3)]
    ctrl = types.SimpleNamespace(page_count=2, page_index=0, folder_stack=[], profile=profs[0], deck=Deck(),
                                 geometry=Geometry(4, 2), key_count=8, serial="VIRTUAL", model="FDV4X2",
                                 candidate_profiles=lambda: profs, load_profile=lambda pid: None)
    cfg = types.SimpleNamespace(data={}, save=lambda: None)
    cfg.get = lambda k, d=None: cfg.data.get(k, d)
    cfg.device = lambda serial: {}
    V.update(cfg, enabled=True)  # Fenster, immer im Vordergrund
    root = tk.Tk()
    root.withdraw()
    try:
        w = VirtualDeckWindow(root, types.SimpleNamespace(cfg=cfg, virtual_controller=ctrl))
        assert _until(root, lambda: w.win is not None and w.win.winfo_ismapped() and w._hits)
        root.after_cancel(w._job)  # Takt hier selbst
        lifts = []
        w.win.lift = lambda *a: lifts.append(1)

        def ticks():
            lifts.clear()
            for _i in range(60):
                w.update()
            return len(lifts)
        assert ticks()  # ohne Menü: alle 25 Takte nach oben
        # Profilwahl in der Fußzeile: das Menü liegt über dem Deck und muss dort bleiben
        x0, y0, x1, y1, _what = next(h for h in w._hits if h[4] == ("profiles",))
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        w._press(types.SimpleNamespace(x=cx, y=cy, x_root=w.cv.winfo_rootx() + cx, y_root=w.cv.winfo_rooty() + cy))
        m = theme.PopupMenu.current
        assert m is not None
        assert ticks() == 0 and theme.PopupMenu.current is m
        m.close()
        # offene Liste einer Combobox: hält den Grab (grab_current() scheitert daran mit KeyError)
        cb = ttk.Combobox(tk.Toplevel(root), values=["a", "b"])
        cb.pack()
        root.update()
        root.tk.call("ttk::combobox::Post", str(cb))
        root.update()
        assert ticks() == 0
        root.tk.call("ttk::combobox::Unpost", str(cb))
        root.update()
        assert ticks()
        # aufgeklappte Liste der FreeDeck-Kopfzeile (hält keinen Grab)
        w.editor = types.SimpleNamespace(drop_open=lambda: True)
        assert ticks() == 0
    finally:
        root.destroy()
