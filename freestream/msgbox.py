"""Rückfragen, Meldungen, Texteingabe, Farb- und Dateiwahl im Design von FreeDeck und FreeWave.

Ersetzt die grauen Tk-Systemfenster mit denselben Aufrufen:
  * tkinter.messagebox:  askyesno, showinfo, showerror
  * tkinter.simpledialog: askstring
  * tkinter.colorchooser: askcolor
  * tkinter.filedialog:  askopenfilename, askopenfilenames, asksaveasfilename – über den
    Dateidialog des Desktops (zenity oder kdialog), nur ohne beide über Tk
Die Fenster sind modal und warten, bis sie geschlossen werden.
"""
import os
import re
import shutil
import subprocess
import tkinter as tk

from .i18n import _

_DANGER_WORDS = ("delete", "remove", "replace", "löschen", "entfernen", "ersetzen")


def _root(parent):
    if parent is not None:
        return parent.winfo_toplevel()
    return tk._default_root  # noqa: SLF001 - wie messagebox ohne parent


class _Box:
    """Dialog mit Symbol, Titel, Text und Knöpfen; result = Wert des gedrückten Knopfs."""

    def __init__(self, parent, title, message, icon, color, buttons, entry_value=None):
        from .dialogkit import entry, footer, header
        from .theme import COL, Button, Dialog
        self.result = None
        dlg = Dialog(_root(parent), title, width=440)
        dlg.resizable(False, False)
        self.dlg = dlg
        b = dlg.body
        header(b, title, message, icon=icon, color=color, wrap=360)
        self.var = None
        if entry_value is not None:
            self.var = tk.StringVar(dlg, value=entry_value)
            self.entry = entry(b, self.var)
            self.entry.box.pack(fill="x", pady=(0, 4))
        footer(dlg)
        first = None
        for text, value, kind in buttons:  # rechts nach links: der erste ist der Hauptknopf
            btn = Button(dlg.buttons, text, kind=kind, command=lambda v=value: self._close(v))
            btn.pack(side="right", padx=(8, 0))
            first = first or value
        dlg.protocol("WM_DELETE_WINDOW", lambda: self._close(None))
        dlg.bind("<Escape>", lambda e: self._close(None))
        dlg.bind("<Return>", lambda e: self._close(first))
        dlg.configure(bg=COL["bg"])
        dlg.show()
        if self.var is not None:
            self.entry.focus_set()
            self.entry.select_range(0, "end")
            self.entry.icursor("end")
        dlg.wait_window()

    def _close(self, value):
        if value is not None and self.var is not None:
            value = self.var.get() if value is True else value
        self.result = value
        try:
            self.dlg.grab_release()
            self.dlg.destroy()
        except tk.TclError:
            pass


def _danger(title, message):
    text = f"{title} {message}".lower()
    return any(w in text for w in _DANGER_WORDS)


def askyesno(title, message, parent=None, **_kw):
    """Ja/Nein-Frage; gibt True oder False zurück. Löschen o. Ä. bekommt einen roten Knopf."""
    from .theme import COL
    danger = _danger(title, message)
    box = _Box(parent, title, message, "trash" if danger else "check", COL["danger"] if danger else COL["accent"],
               [(_("Yes"), True, "danger" if danger else "primary"), (_("No"), False, "default")])
    return bool(box.result)


def showinfo(title, message, parent=None, **_kw):
    from .theme import COL
    _Box(parent, title, message, "check", COL["accent"], [(_("OK"), True, "primary")])
    return "ok"


def showerror(title, message, parent=None, **_kw):
    from .theme import COL
    _Box(parent, title, message, "close", COL["danger"], [(_("OK"), True, "primary")])
    return "ok"


def askstring(title, prompt, initialvalue="", parent=None, **_kw):
    """Texteingabe; gibt den Text oder None (Abbrechen) zurück."""
    from .theme import COL
    box = _Box(parent, title, prompt, "edit", COL["accent"], [(_("OK"), True, "primary"),
                                                               (_("Cancel"), None, "default")],
               entry_value=initialvalue or "")
    return box.result


# ------------------------------------------------------------------ Farbe
SWATCHES = ["#ffffff", "#d4d4d8", "#a1a1aa", "#52525b", "#18181b", "#000000",
            "#ef4444", "#f97316", "#f59e0b", "#eab308", "#84cc16", "#22c55e",
            "#10b981", "#14b8a6", "#06b6d4", "#0ea5e9", "#3b82f6", "#6366f1",
            "#8b5cf6", "#a855f7", "#d946ef", "#ec4899", "#f43f5e", "#78716c"]
_HEX = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")


def _norm(text):
    m = _HEX.match((text or "").strip())
    if not m:
        return None
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h.lower()


class _ColorBox:
    """Farbfelder, Hex-Eingabe und Vorschau; result = „#rrggbb“ oder None."""

    def __init__(self, parent, title, color):
        from .dialogkit import entry, footer, header, trace
        from .theme import COL, Button, Dialog
        self.result = None
        self.color = _norm(color) or "#ffffff"
        dlg = Dialog(_root(parent), title, width=400)
        dlg.resizable(False, False)
        self.dlg = dlg
        b = dlg.body
        header(b, title, _("Pick a colour or type a hex value."), icon="image", color=COL["accent"], wrap=320)
        grid = tk.Frame(b, bg=COL["bg"])
        grid.pack(anchor="w")
        self.cells = {}
        for i, c in enumerate(SWATCHES):
            cell = tk.Canvas(grid, width=36, height=36, bg=COL["bg"], highlightthickness=0, cursor="hand2")
            cell.grid(row=i // 6, column=i % 6, padx=3, pady=3)
            cell.bind("<Button-1>", lambda e, c=c: self.set(c))
            self.cells[c] = cell
        row = tk.Frame(b, bg=COL["bg"])
        row.pack(fill="x", pady=(14, 4))
        self.preview = tk.Canvas(row, width=36, height=36, bg=COL["bg"], highlightthickness=0)
        self.preview.pack(side="left", padx=(3, 10))
        self.var = tk.StringVar(dlg, value=self.color)
        self.entry = entry(row, self.var)
        self.entry.box.pack(side="left", fill="x", expand=True)
        trace(dlg, self.var, self._typed)
        footer(dlg)
        Button(dlg.buttons, _("OK"), kind="primary", command=self.ok).pack(side="right", padx=(8, 0))
        Button(dlg.buttons, _("Cancel"), command=self.close).pack(side="right")
        dlg.protocol("WM_DELETE_WINDOW", self.close)
        dlg.bind("<Escape>", lambda e: self.close())
        dlg.bind("<Return>", lambda e: self.ok())
        self._draw()
        dlg.show()
        dlg.wait_window()

    def _draw(self):
        from .theme import COL
        for c, cell in self.cells.items():
            cell.delete("all")
            if c == self.color:
                cell.create_oval(1, 1, 35, 35, outline=COL["accent"], width=2)
            cell.create_oval(5, 5, 31, 31, fill=c, outline=COL["border"])
        self.preview.delete("all")
        self.preview.create_rectangle(1, 1, 35, 35, fill=self.color, outline=COL["border"])

    def _typed(self):
        c = _norm(self.var.get())
        if c and c != self.color:
            self.color = c
            self._draw()

    def set(self, color):
        self.color = color
        self.var.set(color)
        self._draw()

    def ok(self):
        self.result = _norm(self.var.get()) or self.color
        self.close()

    def close(self):
        try:
            self.dlg.grab_release()
            self.dlg.destroy()
        except tk.TclError:
            pass


def askcolor(color=None, parent=None, title=None, **_kw):
    """Wie colorchooser.askcolor: ((r, g, b), „#rrggbb“) oder (None, None)."""
    box = _ColorBox(parent, title or _("Colour"), color)
    if not box.result:
        return None, None
    h = box.result
    return (int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)), h


# ------------------------------------------------------------------ Dateien
def _tool():
    for name in ("zenity", "kdialog"):
        if shutil.which(name):
            return name
    return None


def _patterns(filetypes):
    """[(Name, „*.png *.jpg“), …] – „*“ (alle Dateien) fällt bei den Filtern weg."""
    out = []
    for name, pat in filetypes or ():
        pats = pat.split() if isinstance(pat, str) else list(pat)
        out.append((name, [p for p in pats if p]))
    return out


def _command(tool, mode, title, initialdir, initialfile, filetypes):
    start = os.path.join(initialdir or os.path.expanduser("~"), initialfile or "")
    if tool == "zenity":
        cmd = ["zenity", "--file-selection", "--title", title or ""]
        if mode == "save":
            cmd.append("--save")
        if mode == "multi":
            cmd += ["--multiple", "--separator", "\n"]
        cmd.append("--filename=" + (start if initialfile else start.rstrip("/") + "/"))
        for name, pats in _patterns(filetypes):
            cmd.append("--file-filter=" + f"{name} | {' '.join(pats)}")
        return cmd
    flt = "\n".join(f"{' '.join(p)}|{n}" for n, p in _patterns(filetypes) if p != ["*"])
    op = {"open": "--getopenfilename", "multi": "--getopenfilename", "save": "--getsavefilename"}[mode]
    cmd = ["kdialog", "--title", title or "", op, start] + ([flt] if flt else [])
    if mode == "multi":
        cmd += ["--multiple", "--separate-output"]
    return cmd


def _run(parent, cmd):
    """Desktop-Dialog starten und warten, ohne das Fenster einzufrieren; Ausgabe oder None."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return None
    root = _root(parent)
    done = tk.BooleanVar(root, value=False)

    def poll():
        if proc.poll() is None:
            root.after(100, poll)
        else:
            done.set(True)
    poll()
    if not done.get():
        root.wait_variable(done)
    out = proc.stdout.read() if proc.stdout else ""
    return out.strip("\n") if proc.returncode == 0 and out.strip() else None


def _ask(mode, parent, title, initialdir, initialfile, filetypes, defaultextension):
    tool = _tool()
    if tool is None:
        from tkinter import filedialog as fd
        kw = {k: v for k, v in (("parent", parent), ("title", title), ("initialdir", initialdir),
                                 ("initialfile", initialfile), ("filetypes", filetypes),
                                 ("defaultextension", defaultextension)) if v}
        fn = {"open": fd.askopenfilename, "multi": fd.askopenfilenames, "save": fd.asksaveasfilename}[mode]
        return fn(**kw)
    out = _run(parent, _command(tool, mode, title, initialdir, initialfile, filetypes))
    if mode == "multi":
        return tuple(p for p in (out or "").splitlines() if p)
    if out and mode == "save" and defaultextension and not os.path.splitext(out)[1]:
        out += defaultextension
    return out or ""


def askopenfilename(parent=None, title=None, initialdir=None, initialfile=None, filetypes=None, **_kw):
    return _ask("open", parent, title, initialdir, initialfile, filetypes, None)


def askopenfilenames(parent=None, title=None, initialdir=None, initialfile=None, filetypes=None, **_kw):
    return _ask("multi", parent, title, initialdir, initialfile, filetypes, None)


def asksaveasfilename(parent=None, title=None, initialdir=None, initialfile=None, filetypes=None,
                      defaultextension=None, **_kw):
    return _ask("save", parent, title, initialdir, initialfile, filetypes, defaultextension)
