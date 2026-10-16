"""Rückfragen und Texteingabe im App-Design statt grauer tkinter-Systemdialoge."""
import os

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="braucht X (xvfb-run)")


def _buttons(w):
    from streamlin.theme import Button
    for c in w.winfo_children():
        if isinstance(c, Button):
            yield c
        yield from _buttons(c)


def _later(root, fn):
    """fn(dialog) aufrufen, sobald der Dialog offen ist."""
    def poll():
        tops = [w for w in root.winfo_children() if w.winfo_class() == "Toplevel"]
        if tops:
            try:
                fn(tops[-1])
            finally:  # nie hängen bleiben, auch wenn fn scheitert
                if tops[-1].winfo_exists():
                    tops[-1].destroy()
        else:
            root.after(20, poll)
    root.after(20, poll)


def test_dialogs_are_themed():
    import tkinter as tk
    from streamlin import msgbox
    root = tk.Tk()
    try:
        seen = {}

        def press(label):
            def fn(dlg):
                seen[label] = [b.text for b in _buttons(dlg)]
                next(b for b in _buttons(dlg) if b.text == label).command()
            return fn
        _later(root, press("Ja"))
        assert msgbox.askyesno("Profil löschen", "Wirklich?", parent=root) is True
        assert seen["Ja"] == ["Ja", "Nein"]
        _later(root, press("Nein"))
        assert msgbox.askyesno("Frage", "Weiter?", parent=root) is False
        _later(root, lambda d: d.event_generate("<Escape>"))
        assert msgbox.askyesno("Frage", "Weiter?", parent=root) is False

        def rename(dlg):
            entry = next(w for w in dlg.winfo_children()[0].winfo_children()
                         if w.winfo_class() == "Frame" and any(c.winfo_class() == "Entry" for c in w.winfo_children()))
            e = next(c for c in entry.winfo_children() if c.winfo_class() == "Entry")
            assert e.get() == "Test"
            e.delete(0, "end")
            e.insert(0, "Gaming")
            next(b for b in _buttons(dlg) if b.text == "OK").command()
        _later(root, rename)
        assert msgbox.askstring("Profil umbenennen", "Neuer Name:", initialvalue="Test", parent=root) == "Gaming"
        _later(root, press("Abbrechen"))
        assert msgbox.askstring("Profil umbenennen", "Neuer Name:", parent=root) is None
        _later(root, press("OK"))
        assert msgbox.showerror("Fehler", "Kaputt", parent=root) == "ok"
        _later(root, press("OK"))
        assert msgbox.showinfo("Info", "Fertig", parent=root) == "ok"
    finally:
        root.destroy()


def test_no_tkinter_system_dialogs_left():
    import pathlib
    src = pathlib.Path(__file__).resolve().parent.parent / "streamlin"
    for f in src.glob("*.py"):
        if f.name == "msgbox.py":  # Rückfall ohne zenity/kdialog
            continue
        text = f.read_text()
        assert "from tkinter import" not in text or not any(
            w in line for line in text.splitlines() if line.strip().startswith("from tkinter import")
            for w in ("messagebox", "simpledialog", "colorchooser", "filedialog")), f.name


def test_color_picker():
    import tkinter as tk
    from streamlin import msgbox
    root = tk.Tk()
    try:
        def pick(dlg):
            box = next(iter(_boxes))
            box.set("#22c55e")
            box.ok()
        _boxes = []
        orig = msgbox._ColorBox.__init__

        def spy(self, *a, **k):
            _boxes.append(self)
            orig(self, *a, **k)
        msgbox._ColorBox.__init__ = spy
        try:
            _later(root, pick)
            assert msgbox.askcolor(color="#fff", parent=root, title="Titelfarbe") == ((34, 197, 94), "#22c55e")
            _later(root, lambda d: d.event_generate("<Escape>"))
            assert msgbox.askcolor(color="#ffffff", parent=root) == (None, None)
        finally:
            msgbox._ColorBox.__init__ = orig
        assert msgbox._norm("ABC") == "#aabbcc" and msgbox._norm("#12345") is None
    finally:
        root.destroy()


def test_file_dialog_uses_desktop_tool(monkeypatch, tmp_path):
    import tkinter as tk
    from streamlin import msgbox
    types = [("Bilder", "*.png *.jpg"), ("Alle Dateien", "*")]
    z = msgbox._command("zenity", "save", "Export", str(tmp_path), "a.json", types)
    assert z[:4] == ["zenity", "--file-selection", "--title", "Export"] and "--save" in z
    assert f"--filename={tmp_path}/a.json" in z and "--file-filter=Bilder | *.png *.jpg" in z
    k = msgbox._command("kdialog", "multi", "Plugins", None, None, types)
    assert k[:4] == ["kdialog", "--title", "Plugins", "--getopenfilename"] and k[5] == "*.png *.jpg|Bilder"
    assert k[-2:] == ["--multiple", "--separate-output"]
    # Dialog per Skript simuliert: Fenster bleibt bedienbar, Ergebnis kommt an
    fake = tmp_path / "zenity"
    fake.write_text("#!/bin/sh\nprintf '/x/eins.png\\n/x/zwei.png\\n'\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    root = tk.Tk()
    try:
        assert msgbox.askopenfilenames(parent=root, title="Bilder", filetypes=types) == ("/x/eins.png", "/x/zwei.png")
        fake.write_text("#!/bin/sh\necho /x/profil\n")
        assert msgbox.asksaveasfilename(parent=root, defaultextension=".json") == "/x/profil.json"
        fake.write_text("#!/bin/sh\nexit 1\n")
        assert msgbox.askopenfilename(parent=root) == "" and msgbox.askopenfilenames(parent=root) == ()
    finally:
        root.destroy()
