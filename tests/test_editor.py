"""Tests für den Profil-Editor (Bearbeitungslogik und Oberfläche).

Benötigen keine Elgato-Profile; die Oberflächentests laufen nur mit Display
(z. B. xvfb-run python3 -m pytest tests/test_editor.py).
"""
import json
import os
import zipfile

import pytest
from PIL import Image

from freestream import catalog as C
from freestream import editdoc as E
from freestream import profiles as P

HOTKEY = "com.elgato.streamdeck.system.hotkey"
TEXT = "com.elgato.streamdeck.system.text"


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    return root


@pytest.fixture
def doc(store):
    return E.ProfileDoc(E.create_profile("Test", "20GBD9901", store))


def _reload(store):
    return list(P.ProfileStore(store).profiles.values())[0]


def test_hotkey_roundtrip():
    for text in ("Strg+Umschalt+T", "Alt+F4", "Super+E", "F13", "Strg+Num +", "Strg+Entf", "Umschalt+1"):
        assert C.format_hotkey(C.parse_hotkey(text)) == text
    assert C.parse_hotkey("ctrl+shift+t") == C.parse_hotkey("Strg+Umschalt+T")
    assert C.hotkey_is_empty(C.parse_hotkey(""))
    with pytest.raises(ValueError):
        C.parse_hotkey("Strg+Gibtsnicht")


def test_hotkey_from_tk_matches_runtime():
    from freestream.keyboard import VirtualInput

    class NoDev(VirtualInput):
        def _open(self):
            pass
    vin = NoDev("de")
    hk = C.hotkey_from_tk("t", "\x14", 0x4 | 0x1)
    assert C.format_hotkey(hk) == "Strg+Umschalt+T"
    names = [vin.key_by_name(n) for n in ("KEY_LEFTCTRL", "KEY_LEFTSHIFT", "KEY_T")]
    assert vin.hotkey_codes(hk) == names
    assert C.hotkey_from_tk("Control_L", "", 0x4) is None
    # Umschalt+1 liefert unter X11 das Keysym "exclam" – über den Keycode trotzdem Ziffer 1
    assert C.format_hotkey(C.hotkey_from_tk("exclam", "!", 0x1, 10)) == "Umschalt+1"
    assert vin.hotkey_codes(C.parse_hotkey("Strg+#")) == [vin.key_by_name("KEY_LEFTCTRL"),
                                                          vin.key_by_name("KEY_BACKSLASH")]


def test_page_dir_codec():
    import uuid
    for _ in range(200):
        u = str(uuid.uuid4())
        assert P.decode_page_dir(P.encode_page_dir(u)) == u
        assert P.decode_page_dir(u.upper()) == u


def test_create_and_edit_profile(store, doc):
    p0 = doc.page_ids[0]
    doc.place(p0, ("key", 0, 0), HOTKEY)
    doc.set_hotkey(p0, ("key", 0, 0), 0, C.parse_hotkey("Strg+C"))
    doc.update_state(p0, ("key", 0, 0), 0, "Title", "Kopieren")
    doc.place(p0, ("dial", 1), "com.elgato.streamdeck.system.multimedia")
    assert doc.save()
    prof = _reload(store)
    assert prof.name == "Test" and prof.geometry.dials == 4
    pg = prof.page(prof.page_ids[0])
    assert pg.keypad[(0, 0)]["States"][0]["Title"] == "Kopieren"
    assert C.format_hotkey(pg.keypad[(0, 0)]["Settings"]["Hotkeys"][0]) == "Strg+C"
    assert pg.encoders[1]["UUID"] == "com.elgato.streamdeck.system.multimedia"
    assert not doc.save()  # nichts mehr zu schreiben


def test_actions_created_by_editor_run(store, doc):
    """Alle Katalogaktionen lassen sich vom Dienst laden und zeichnen."""
    p0 = doc.page_ids[0]
    for t in C.CATALOG:
        slot = ("key", 0, 0) if C.KEY in t.controllers else ("dial", 0)
        data = doc.place(p0, slot, t.uuid)
        img = E.preview_action(doc, p0, data, (72, 72)) if slot[0] == "key" else \
            E.preview_dial(doc, p0, data, (200, 100))
        assert img.size in ((72, 72), (200, 100))
        assert __import__("freestream.actions", fromlist=["is_builtin"]).is_builtin(t.uuid), t.uuid


def test_folders_copy_move_and_cleanup(store, doc, tmp_path):
    p0 = doc.page_ids[0]
    folder = doc.place(p0, ("key", 1, 0), C.FOLDER_UUID)
    fid = folder["Settings"]["ProfileUUID"]
    assert doc.get(fid, ("key", 0, 0))["UUID"] == C.BACK_UUID
    doc.place(fid, ("key", 2, 0), TEXT)
    Image.new("RGB", (300, 200), (200, 0, 0)).save(tmp_path / "icon.png")
    doc.set_icon(fid, ("key", 2, 0), 0, tmp_path / "icon.png")
    # Ordner kopieren -> eigenständige Kopie mit eigenem Unterordner und Bild
    doc.paste(p0, ("key", 2, 0), doc.copy(p0, ("key", 1, 0)))
    fid2 = doc.get(p0, ("key", 2, 0))["Settings"]["ProfileUUID"]
    assert fid2 != fid and doc.get(fid2, ("key", 2, 0))["UUID"] == TEXT
    img_rel = doc.get(fid2, ("key", 2, 0))["States"][0]["Image"]
    assert (doc.page(fid2).dir / img_rel).is_file()
    with Image.open(doc.page(fid2).dir / img_rel) as im:
        assert im.size == (E.ICON_SIZE, E.ICON_SIZE)
    # auf neue Seite verschieben (tauschen)
    p1 = doc.add_page()
    doc.move(p0, ("key", 1, 0), p1, ("key", 4, 2))
    assert doc.get(p0, ("key", 1, 0)) is None and doc.get(p1, ("key", 4, 2))["UUID"] == C.FOLDER_UUID
    # ersten Ordner löschen -> beim Aufräumen verschwindet nur er
    doc.remove(p1, ("key", 4, 2))
    old_dir = doc.page(fid).dir
    doc.cleanup()
    assert not old_dir.exists() and doc.page(fid2).dir.exists()
    prof = _reload(store)
    assert len(prof.page_ids) == 2
    assert prof.page(fid2).keypad[(2, 0)]["UUID"] == TEXT


def test_undo_redo(doc):
    p0 = doc.page_ids[0]
    doc.place(p0, ("key", 0, 0), HOTKEY)
    for ch in "Hallo":
        cur = doc.get(p0, ("key", 0, 0))["States"][0].get("Title", "")
        doc.update_state(p0, ("key", 0, 0), 0, "Title", cur + ch)
    assert doc.get(p0, ("key", 0, 0))["States"][0]["Title"] == "Hallo"
    assert doc.undo()  # Tippen wird zu einem Schritt zusammengefasst
    assert doc.get(p0, ("key", 0, 0))["States"][0]["Title"] == ""
    assert doc.undo()
    assert doc.get(p0, ("key", 0, 0)) is None
    assert doc.redo() and doc.redo()
    assert doc.get(p0, ("key", 0, 0))["States"][0]["Title"] == "Hallo"


def test_pages(doc):
    p0 = doc.page_ids[0]
    p1 = doc.add_page()
    p2 = doc.add_page(after=p0)
    assert doc.page_ids == [p0, p2, p1]
    doc.move_page(p1, -2)
    assert doc.page_ids == [p1, p0, p2]
    doc.remove_page(p1)
    assert doc.page_ids == [p0, p2]
    doc.remove_page(p2)
    with pytest.raises(E.EditError):
        doc.remove_page(p0)


def test_child_actions(doc):
    p0 = doc.page_ids[0]
    m = doc.place(p0, ("key", 0, 0), "com.elgato.streamdeck.multiactions.routine")
    group = C.child_lists(m)[0]["Actions"]
    group += [C.new_action(HOTKEY), C.new_action("com.elgato.streamdeck.multiactions.delay")]
    logic = doc.place(p0, ("key", 1, 0), "com.elgato.streamdeck.keys.logic")
    C.child_lists(logic)[2] = C.new_action(TEXT)
    doc.save()
    from freestream import actions as A
    ctx = A.Context(None, {}, None, None)
    ma = A.create_action(doc.get(p0, ("key", 0, 0)), None, ctx)
    assert [type(a).__name__ for g in ma.groups for a in g] == ["Hotkey", "Delay"]
    kl = A.create_action(doc.get(p0, ("key", 1, 0)), None, ctx)
    assert kl.single is None and kl.double is None and type(kl.hold).__name__ == "Text"


def test_preserves_unknown_fields_and_v3_names(store):
    """Fremde Profile: unbekannte Felder bleiben erhalten, v3-Ordnernamen werden weiterverwendet."""
    import uuid
    pid, page = str(uuid.uuid4()).upper(), str(uuid.uuid4())
    root = store / f"{pid}.sdProfile"
    (root / "Profiles" / page.upper()).mkdir(parents=True)
    plugin_action = {"UUID": "com.obsproject.obsstudio.scene", "Name": "Szene", "Settings": {"scene": "Live"},
                     "States": [{"Title": "Live"}], "Custom": {"x": 1}}
    (root / "Profiles" / page.upper() / "manifest.json").write_text(json.dumps(
        {"Controllers": [{"Type": "Keypad", "Actions": {"0,0": plugin_action}}], "Extra": 42}))
    (root / "manifest.json").write_text(json.dumps(
        {"Device": {"Model": "20GBA9901", "UUID": ""}, "Name": "Fremd",
         "Pages": {"Pages": [page], "Default": page, "Current": page}, "Version": "3.0", "Foo": "bar"}))
    doc = E.ProfileDoc(root)
    doc.place(page, ("key", 1, 0), HOTKEY)
    p1 = doc.add_page()
    doc.save()
    assert (root / "Profiles" / p1.upper() / "manifest.json").exists()
    m = json.loads((root / "Profiles" / page.upper() / "manifest.json").read_text())
    assert m["Extra"] == 42 and m["Controllers"][0]["Actions"]["0,0"] == plugin_action
    assert json.loads((root / "manifest.json").read_text())["Foo"] == "bar"


def test_export_import_roundtrip(store, doc, tmp_path):
    p0 = doc.page_ids[0]
    doc.place(p0, ("key", 0, 0), C.FOLDER_UUID)
    doc.place(p0, ("key", 1, 0), TEXT)
    out = doc.export(tmp_path / "Mein Profil")
    assert out.name == "Mein Profil.streamDeckProfile"
    names = zipfile.ZipFile(out).namelist()
    assert all(n.startswith(doc.root.name + "/") for n in names)
    other = tmp_path / "other"
    ids = P.import_path(out, other, tmp_path / "res")
    prof = list(P.ProfileStore(other).profiles.values())[0]
    assert ids == [doc.id] and prof.page(prof.page_ids[0]).keypad[(1, 0)]["UUID"] == TEXT


def test_duplicate_profile(store, doc):
    doc.place(doc.page_ids[0], ("key", 0, 0), TEXT)
    doc.save()
    dst = E.duplicate_profile(doc.root, "Kopie")
    names = sorted(p.name for p in P.ProfileStore(store).profiles.values())
    assert names == ["Kopie", "Test"] and dst.exists()


# --------------------------------------------------------------------------
# Oberfläche
# --------------------------------------------------------------------------
@pytest.fixture
def app(store, doc, monkeypatch):
    tk = pytest.importorskip("tkinter")
    if not os.environ.get("DISPLAY"):
        pytest.skip("kein Display (xvfb-run verwenden)")
    from freestream import editor
    monkeypatch.setattr(editor.messagebox, "askyesno", lambda *a, **k: True)
    doc.place(doc.page_ids[0], ("key", 0, 0), HOTKEY)
    doc.cleanup()
    root = tk.Tk()
    a = editor.EditorApp(root, store_root=store)
    root.update()
    yield a
    if root.winfo_exists():
        a.close()


def test_gui_place_move_and_save(app, store):
    page = app.page_uuid
    app.place_action(TEXT, ("key", 2, 1))
    app.root.update()
    hit = {s: (x0, y0) for x0, y0, _, _, s in app._hit}

    class Ev:
        def __init__(self, x, y):
            self.x, self.y = x, y
            self.x_root, self.y_root = x + app.canvas.winfo_rootx(), y + app.canvas.winfo_rooty()
    a, b = hit[("key", 0, 0)], hit[("key", 3, 1)]
    app._canvas_press(Ev(a[0] + 4, a[1] + 4))
    app._canvas_motion(Ev(a[0] + 40, a[1] + 40))
    app._canvas_release(Ev(b[0] + 4, b[1] + 4))
    assert app.doc.get(page, ("key", 3, 1))["UUID"] == HOTKEY
    app.copy_selected()
    app.select(("key", 0, 1))
    app.paste_selected()
    app.undo()
    assert app.doc.get(page, ("key", 0, 1)) is None
    app.save_now()
    prof = _reload(store)
    keypad = prof.page(prof.page_ids[0]).keypad
    assert keypad[(3, 1)]["UUID"] == HOTKEY and keypad[(2, 1)]["UUID"] == TEXT


def test_gui_inspector_for_all_actions(app):
    page = app.page_uuid
    for t in C.CATALOG:
        slot = ("key", 1, 0) if C.KEY in t.controllers else ("dial", 0)
        app.place_action(t.uuid, slot)
        app.root.update()
        for tab in app.INSPECTOR_TABS:  # alle Reiter der Eigenschaften
            app.set_inspector_tab(tab)
            app.root.update()
            assert app.inspector.inner.winfo_children()
        app.set_inspector_tab("appearance")
    app.open_folder(None)
    app.place_action(C.FOLDER_UUID, ("key", 3, 1))
    fid = app.doc.get(page, ("key", 3, 1))["Settings"]["ProfileUUID"]
    app.open_folder(fid)
    assert app.page_uuid == fid and len(app.nav) == 2
    app.folder_back()
    assert app.page_uuid == page


def test_gui_stage_all_models(app, store):
    """Vorschau zeichnet jedes Modell; jede Taste und jeder Regler ist anklickbar."""
    from freestream.models import model_geometry
    app.root.geometry("1280x800")
    for code in ("20GBA9901", "20GAI9902", "20GAT9901", "20GBX9901", "20GBJ9901", "20GBO9901", "20GBF9901"):
        d = E.ProfileDoc(E.create_profile(code, code, store))
        d.place(d.page_ids[0], ("key", 0, 0), HOTKEY)
        if model_geometry(code).dials:
            d.place(d.page_ids[0], ("dial", 0), HOTKEY)
        d.save()
        app.open_profile(d.id)
        app.root.update()
        g = model_geometry(code)
        slots = {s for *_box, s in app._hit}
        assert len(slots) == g.cols * g.rows + g.dials, code
        app.select(("dial", 0) if g.dials else ("key", 1, 0))
        app.root.update()


def test_gui_neo_touch_keys_turn_pages(app, store):
    d = E.ProfileDoc(E.create_profile("Neo", "20GBJ9901", store))
    second = d.add_page()
    d.save()
    app.root.geometry("1280x800")
    app.open_profile(d.id)
    app.root.update()
    cv = app.canvas
    x0, y0, x1, y1 = cv.bbox("neo1")
    cv.event_generate("<Button-1>", x=(x0 + x1) // 2, y=(y0 + y1) // 2)
    for _i in range(5):
        app.root.update()
    assert app.page_uuid == second
