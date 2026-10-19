"""Gemeinsame Bausteine der Dialoge im FreeStream-Design (Kanal, Effekte, Plugins, Hintergrund, Assistent).

Ergänzt theme.py: Kopfzeile mit Symbol-Kachel, Fußzeile mit feiner Linie,
Eingabefeld und Liste mit Bildlauf – jeweils in den Farben des aktiven Modus.
"""
import tkinter as tk
from tkinter import ttk

from .i18n import _
from .theme import COL, Button, Dialog, Icons, font, icon_tile, label, rounded_rect, section


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
        # Mausrad: ein Handler für alle Bereiche, nur einmal je Tk gebunden – ein eigener je Bereich bliebe
        # nach dem Schließen des Dialogs für immer an „all“ hängen (samt Dialog und seinen Bildern)
        root = self._root()
        if not root.__dict__.get("_scrollarea_wheel"):
            root._scrollarea_wheel = True
            for seq, d in (("<Button-4>", -2), ("<Button-5>", 2)):
                root.bind_all(seq, lambda e, d=d: _scrollarea_wheel(e, d), add="+")
            root.bind_all("<MouseWheel>", lambda e: _scrollarea_wheel(e, -1 if e.delta > 0 else 1), add="+")

    def _on_scroll(self, lo, hi):
        if float(lo) <= 0 and float(hi) >= 1:
            self.bar.pack_forget()
        elif not self.bar.winfo_manager():
            self.bar.pack(side="right", fill="y", padx=(8, 0), before=self.cv)
        self.bar.set(lo, hi)

    def _wheel(self, e, d):
        """Ein Rad-Schritt; False, wenn der Inhalt ganz hineinpasst (dann darf ein äußerer Bereich scrollen)."""
        try:
            if self.bar.winfo_manager() == "":
                return False
            self.cv.yview_scroll(d, "units")
            return True
        except tk.TclError:
            return False


def _scrollarea_wheel(e, d):
    """Mausrad: den innersten ScrollArea unter dem Zeiger scrollen, der eine Leiste hat."""
    w = e.widget  # Tk-interne Fenster (z. B. Liste einer Combobox) kommen als Text an
    while w is not None and not isinstance(w, str):
        if isinstance(w, ScrollArea) and w._wheel(e, d):
            return
        w = getattr(w, "master", None)


def draw_steps(cv, steps, step, icons):
    """Schrittanzeige eines Assistenten: nummerierte Kreise mit Namen, verbunden durch feine Linien."""
    from tkinter import font as tkfont
    cv.delete("all")
    fn, fb = font(cv, 9, "normal"), font(cv, 9, "bold")
    fnt = tkfont.Font(cv, font=fb)
    d, gap = 22, 10
    widths = [d + 8 + fnt.measure(name) for name in steps]
    avail = max(cv.winfo_width(), 200)
    line = max(12, (avail - sum(widths) - 2 * gap * (len(steps) - 1)) / max(1, len(steps) - 1))
    x, y = 0, 13
    for i, (name, w) in enumerate(zip(steps, widths)):
        done, cur = i < step, i == step
        if done or cur:
            cv.create_oval(x, y - d / 2, x + d, y + d / 2, fill=COL["accent"], outline="")
            if done:
                cv.create_image(x + d / 2, y, image=icons.get("check", 12, COL["on_accent"]))
            else:
                cv.create_text(x + d / 2, y, text=str(i + 1), fill=COL["on_accent"], font=fb)
        else:
            cv.create_oval(x, y - d / 2, x + d, y + d / 2, fill=COL["panel"], outline=COL["border"])
            cv.create_text(x + d / 2, y, text=str(i + 1), fill=COL["muted"], font=fn)
        cv.create_text(x + d + 8, y, text=name, anchor="w", fill=COL["fg"] if cur else COL["muted"],
                       font=fb if cur else fn)
        x += w
        if i < len(steps) - 1:
            cv.create_line(x + gap, y, x + gap + line, y, fill=COL["accent"] if done else COL["border"],
                           width=2 if done else 1)
            x += 2 * gap + line


# ================================================================ Tastenkürzel
class ShortcutField(tk.Frame):
    """Tastenkürzel festlegen: Feld zeigt die Tasten, „Festlegen“ nimmt sie auf, „Entfernen“ löscht sie.

    on_set(binding) / on_remove() werden nach der Eingabe aufgerufen.
    """

    def __init__(self, master, binding="", on_set=None, on_remove=None, width=170):
        from . import shortcuts
        self._sc = shortcuts
        bg = master.cget("bg")
        super().__init__(master, bg=bg)
        self.binding, self.on_set, self.on_remove = binding or "", on_set, on_remove
        self.capturing, self.w, self.h = False, width, 32
        self.icons = Icons(self)
        self.f = font(self, 10, "bold")
        self.fs = font(self, 9)
        self.cv = tk.Canvas(self, width=width, height=self.h, bg=bg, highlightthickness=0, bd=0, cursor="hand2",
                            takefocus=1)
        self.cv.pack(side="left")
        self.cv.bind("<Button-1>", lambda e: self.capture())
        self.cv.bind("<KeyPress>", self._key)
        self.cv.bind("<FocusOut>", lambda e: self._cancel())
        self.set_btn = Button(self, _("Set"), command=self.capture)
        self.set_btn.pack(side="left", padx=(8, 0))
        self.rm_btn = Button(self, _("Remove"), kind="ghost", command=self._remove)
        self.rm_btn.pack(side="left", padx=(4, 0))
        self._draw()

    def set(self, binding):
        self.binding = binding or ""
        self.capturing = False
        self._draw()

    def capture(self):
        self.capturing = True
        self.cv.focus_set()
        self._draw()

    def _cancel(self):
        if self.capturing:
            self.capturing = False
            self._draw()

    def _key(self, e):
        if not self.capturing:
            return None
        if e.keysym == "Escape":
            self._cancel()
            return "break"
        b = self._sc.from_tk(e.keysym, e.state)
        if not b:
            return "break"  # nur Modifier gedrückt oder Taste ohne Modifier
        self.capturing = False
        self.binding = b
        self._draw()
        if self.on_set:
            self.on_set(b)
        return "break"

    def _remove(self):
        if not self.binding:
            return
        self.binding = ""
        self.capturing = False
        self._draw()
        if self.on_remove:
            self.on_remove()

    def _draw(self):
        cv = self.cv
        cv.delete("all")
        w, h = self.w, self.h
        rounded_rect(cv, 0, 0, w - 1, h - 1, 9, fill=COL["panel2"],
                     outline=COL["accent"] if self.capturing else COL["border"])
        if self.capturing:
            text, fg, fnt = _("Press keys …"), COL["accent"], self.fs
        elif self.binding:
            text, fg, fnt = self._sc.label(self.binding), COL["fg"], self.f
        else:
            text, fg, fnt = _("Not set"), COL["faint"], self.fs
        cv.create_image(16, h / 2, image=self.icons.get("keyboard", 15, COL["accent"] if self.capturing
                                                         else COL["muted"]))
        cv.create_text(32, h / 2, text=text, anchor="w", font=fnt, fill=fg)
        self.rm_btn.configure_text(enabled=bool(self.binding))


class ManualShortcutDialog(Dialog):
    """Unbekannter Desktop: Befehl zum Kopieren, damit du ihn selbst als Tastenkürzel einträgst."""

    def __init__(self, master, message, command):
        super().__init__(master, _("Keyboard shortcut"), width=560)
        header(self.body, _("Set up the shortcut in your desktop"), message, icon="keyboard", wrap=480)
        self.var = tk.StringVar(self, value=command)
        ent = entry(self.body, self.var, size=10)
        ent.box.pack(fill="x")
        ent.configure(state="readonly", readonlybackground=COL["panel2"])
        footer(self)
        Button(self.buttons, _("Close"), kind="primary", command=self.destroy).pack(side="right")
        Button(self.buttons, _("Copy command"), command=self._copy).pack(side="left")
        self.show()

    def _copy(self):
        self.clipboard_clear()
        self.clipboard_append(self.var.get())
