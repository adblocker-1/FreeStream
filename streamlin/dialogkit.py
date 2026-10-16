"""Gemeinsame Bausteine der Dialoge im Streamlin-Design (Kanal, Effekte, Plugins, Hintergrund, Assistent).

Ergänzt theme.py: Kopfzeile mit Symbol-Kachel, Fußzeile mit feiner Linie,
Eingabefeld und Liste mit Bildlauf – jeweils in den Farben des aktiven Modus.
"""
import tkinter as tk
from tkinter import ttk

from .theme import COL, font, icon_tile, label, section


def header(master, title, subtitle="", icon=None, color=None, wrap=460, size=15):
    """Titel mit optionaler Symbol-Kachel links und grauer Unterzeile."""
    bg = master.cget("bg")
    row = tk.Frame(master, bg=bg)
    row.pack(fill="x", pady=(0, 16))
    if icon:
        tile = tk.Label(row, image=icon_tile(row, icon, 40, color or COL["accent"]), bg=bg)
        tile.pack(side="left", anchor="n", padx=(0, 14))
        row.tile = tile
    txt = tk.Frame(row, bg=bg)
    txt.pack(side="left", fill="x", expand=True)
    row.title = label(txt, title, size, "bold", bg=bg)
    row.title.pack(anchor="w")
    row.subtitle = label(txt, subtitle, 9, color=COL["muted"], wraplength=wrap, justify="left", bg=bg)
    if subtitle:
        row.subtitle.pack(anchor="w", pady=(3, 0))
    return row


def footer(dialog):
    """Fußzeile eines theme.Dialog im neuen Stil: Hintergrundfarbe, feine Linie darüber."""
    dialog.buttons.configure(bg=COL["bg"], padx=22, pady=14)
    dialog.buttons.pack_configure(before=dialog.body)  # Fußzeile zuerst: bleibt auch bei kleinem Fenster sichtbar
    line = tk.Frame(dialog, bg=COL["border"], height=1)
    line.pack(fill="x", side="bottom", after=dialog.buttons)
    return dialog.buttons


def field(master, text, pady=(0, 6)):
    """Abschnittsüberschrift über einem Eingabefeld."""
    lb = section(master, text)
    lb.pack(anchor="w", fill="x", pady=pady)
    return lb


def entry(master, textvariable, size=11):
    """Eingabefeld: leicht abgesetzt, feiner Rand, Akzent beim Fokus.

    Gibt das tk.Entry zurück; platziert wird der Rahmen entry.box.
    """
    box = tk.Frame(master, bg=COL["panel2"], highlightthickness=1, highlightbackground=COL["border"],
                   highlightcolor=COL["accent"], padx=10)
    ent = tk.Entry(box, textvariable=textvariable, bg=COL["panel2"], fg=COL["fg"], relief="flat",
                   insertbackground=COL["fg"], highlightthickness=0, disabledbackground=COL["panel2"],
                   disabledforeground=COL["muted"], font=font(master, size))
    ent.pack(fill="x", ipady=7)
    ent.bind("<FocusIn>", lambda e: box.configure(highlightbackground=COL["accent"]), add="+")
    ent.bind("<FocusOut>", lambda e: box.configure(highlightbackground=COL["border"]), add="+")
    ent.box = box
    return ent


def slim_scrollbar(widget):
    """Schmale Bildlaufleiste ohne Pfeile (Stilname zum Übergeben an ttk.Scrollbar)."""
    name = "Slim.Vertical.TScrollbar"
    st = ttk.Style(widget)
    st.layout(name, [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
        ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
    st.configure(name, background=COL["raised"], troughcolor=COL["bg"], bordercolor=COL["bg"],
                 lightcolor=COL["raised"], darkcolor=COL["raised"], arrowsize=8, gripcount=0)
    st.map(name, background=[("active", COL["faint"])], lightcolor=[("active", COL["faint"])],
           darkcolor=[("active", COL["faint"])])
    return name


class ScrollArea(tk.Frame):
    """Senkrecht scrollbarer Bereich; Inhalt in .inner (Mausrad nur über dem Bereich)."""

    def __init__(self, master, bg=None):
        bg = bg or master.cget("bg")
        super().__init__(master, bg=bg)
        self.cv = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.cv.yview, style=slim_scrollbar(self))
        self.cv.configure(yscrollcommand=self._on_scroll)
        self.cv.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.cv, bg=bg)
        self._win = self.cv.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.cv.configure(scrollregion=self.cv.bbox("all")))
        self.cv.bind("<Configure>", lambda e: self.cv.itemconfigure(self._win, width=e.width))
        for seq, d in (("<Button-4>", -2), ("<Button-5>", 2)):
            self.bind_all(seq, lambda e, d=d: self._wheel(e, d), add="+")
        self.bind_all("<MouseWheel>", lambda e: self._wheel(e, -1 if e.delta > 0 else 1), add="+")

    def _on_scroll(self, lo, hi):
        if float(lo) <= 0 and float(hi) >= 1:
            self.bar.pack_forget()
        elif not self.bar.winfo_manager():
            self.bar.pack(side="right", fill="y", padx=(8, 0), before=self.cv)
        self.bar.set(lo, hi)

    def _wheel(self, e, d):
        if self.bar.winfo_manager() == "":
            return
        w = e.widget
        while w is not None:
            if w is self:
                self.cv.yview_scroll(d, "units")
                return
            w = getattr(w, "master", None)
