"""Icon-Bibliothek: selbst gezeichnete Tastenbilder in drei Paketen, Suche, Fenster und Zuweisen."""
import os

import pytest

import test_editor
from freestream import iconlib

store, doc, app = test_editor.store, test_editor.doc, test_editor.app  # Fixtures


def test_every_icon_renders_and_differs():
    seen = {}
    for name in iconlib.names():
        for style in iconlib.STYLES:
            im = iconlib.render(name, style)
            assert im.size == (144, 144) and im.mode == "RGBA"
            assert len(set(im.resize((36, 36)).getdata())) > 4, (name, style)  # nicht nur Fläche
        key = iconlib.render(name, "white", 48).tobytes()
        assert key not in seen, (name, seen.get(key))  # keine doppelten Symbole
        seen[key] = name
    assert len(iconlib.names()) == len(set(iconlib.names())) >= 100


def test_search_uses_translated_names():
    assert iconlib.search("kopf") == ["headphones", "headphones_off"]  # Kopfhörer (Deutsch)
    assert "volume_up" in iconlib.search("laut")
    assert iconlib.search("") == iconlib.names()
    assert iconlib.search("gibtsnicht") == []
    assert set(iconlib.search("audio")) >= {"mic", "music"}  # Thema
    assert "lightbulb" in iconlib.search("licht")


@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="braucht X (xvfb-run)")
def test_library_window():
    import time
    import tkinter as tk
    from freestream.iconlibui import IconLibrary
    root = tk.Tk()
    try:
        picked = []
        dlg = IconLibrary(root, on_pick=lambda n, s: picked.append((n, s)), on_file=lambda: picked.append("file"))
        root.update()
        assert list(dlg.grids) == ["color"]  # nur das erste Paket offen
        dlg._toggle("white")
        assert list(dlg.grids) == ["color", "white"]
        dlg.query.set("kopf")
        root.update()
        assert all(found == ["headphones", "headphones_off"] for _g, found in dlg.grids.values())
        assert len(dlg.grids) == 3  # Suche klappt alle Pakete mit Treffern auf
        size = dlg.tile
        dlg._zoom(1)
        assert dlg.tile > size
        for _ in range(10):  # neu aufgebaute Kacheln anzeigen lassen
            root.update()
            time.sleep(0.02)
        grid, _found = dlg.grids["color"]  # sichtbar (oben)
        cell = grid.winfo_children()[0]
        assert cell.label == "Kopfhörer"
        cell.event_generate("<Button-1>", x=5, y=5)
        root.update()
        assert picked == [("headphones", "color")] and not dlg.winfo_exists()
    finally:
        root.destroy()


def test_pick_sets_key_image(app):
    from freestream import editor
    import tkinter as tk
    page = app.page_uuid
    data = app.doc.get(page, ("key", 0, 0))
    form = editor.ActionForm(app, tk.Frame(app.root), data, page, ("key", 0, 0))
    form.build_appearance = lambda: None
    form._set_library_icon("music", "color")
    img = app.doc.get(page, ("key", 0, 0))["States"][0]["Image"]
    assert img.startswith("Images/") and (app.doc.page(page).dir / img).exists()
