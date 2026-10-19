"""Fenster „Icon-Bibliothek“ im FreeDeck-Fenster: vordefinierte Tastenbilder suchen und zuweisen.

Aufbau wie gewohnt: Suchfeld oben, darunter die Pakete (aufklappbar, mit Anzahl), unten links
„Eigenes Bild …“, unten rechts Zoom. Ein Klick auf ein Symbol ruft on_pick(name, style) auf.
"""
import tkinter as tk

from PIL import ImageTk

from . import iconlib, theme
from .dialogkit import ScrollArea, entry, footer, trace
from .i18n import _
from .theme import COL, Button, Dialog, Icons

SIZES = (48, 64, 84)
GAP = 6
theme.EXTRA_GLYPHS.setdefault("minus", lambda size, color: iconlib.glyph("minus", size, color))


class IconLibrary(Dialog):
    def __init__(self, master, on_pick, on_file=None):
        super().__init__(master, _("Icon library"), width=460, height=600)
        self.on_pick, self.on_file = on_pick, on_file
        self.icons = Icons(self)
        self.size_idx = 1
        self.open = {"color"}  # aufgeklappte Pakete
        self._photos = {}
        b = self.body
        b.configure(padx=0, pady=0)
        top = tk.Frame(b, bg=COL["bg"], padx=16, pady=12)
        top.pack(fill="x")
        self.query = tk.StringVar(self)
        row = tk.Frame(top, bg=COL["panel2"])
        row.pack(fill="x")
        tk.Label(row, image=self.icons.get("search", 16, COL["muted"]), bg=COL["panel2"]).pack(side="left",
                                                                                               padx=(10, 0))
        self.search = entry(row, self.query)
        self.search.box.configure(highlightthickness=0, padx=6)
        self.search.box.pack(side="left", fill="x", expand=True)
        self.hint = tk.Label(self.search, text=_("Search icons …"), bg=COL["panel2"], fg=COL["faint"],
                             font=theme.font(self, 11))
        self.hint.place(x=2, rely=0.5, anchor="w")
        self.hint.bind("<Button-1>", lambda e: self.search.focus_set())
        trace(self, self.query, self._refresh)
        tk.Frame(b, bg=COL["border"], height=1).pack(fill="x")
        self.area = ScrollArea(b, bg=COL["bg"])
        self.area.pack(fill="both", expand=True)
        self.list = self.area.inner
        footer(self)
        if on_file:
            Button(self.buttons, _("Own image …"), "image", kind="ghost", command=self._file).pack(side="left")
        self.zoom_in = Button(self.buttons, "", "plus", kind="ghost", command=lambda: self._zoom(1))
        self.zoom_in.pack(side="right")
        self.zoom_out = Button(self.buttons, "", "minus", kind="ghost",
                               command=lambda: self._zoom(-1))
        self.zoom_out.pack(side="right", padx=(0, 4))
        self.bind("<Escape>", lambda e: self.destroy())
        self.area.cv.bind("<Configure>", lambda e: self._relayout(), add="+")
        self._refresh()
        self.show()
        self.search.focus_set()

    # ---------------------------------------------------------- Inhalt
    @property
    def tile(self):
        return SIZES[self.size_idx]

    def _matches(self):
        return iconlib.search(self.query.get())

    def _refresh(self):
        self.hint.place(x=2, rely=0.5, anchor="w") if not self.query.get() else self.hint.place_forget()
        for w in self.list.winfo_children():
            w.destroy()
        self.grids = {}
        found = self._matches()
        searching = bool(self.query.get().strip())
        for style in iconlib.STYLES:
            self._pack_row(style, found, expanded=searching or style in self.open)
        if searching and not found:
            tk.Label(self.list, text=_("No icons found"), bg=COL["bg"], fg=COL["muted"],
                     font=theme.font(self, 10)).pack(pady=24)
        self.zoom_in.configure_text(enabled=self.size_idx < len(SIZES) - 1)
        self.zoom_out.configure_text(enabled=self.size_idx > 0)

    def _pack_row(self, style, found, expanded):
        head = tk.Frame(self.list, bg=COL["bg"], cursor="hand2")
        head.pack(fill="x")
        inner = tk.Frame(head, bg=COL["bg"], padx=14, pady=11)
        inner.pack(fill="x")
        chev = tk.Label(inner, image=self.icons.get("chevron" if expanded else "arrow", 12, COL["muted"]),
                        bg=COL["bg"])
        chev.pack(side="left")
        logo = tk.Label(inner, image=self.icons.get("deck", 15, COL["fg"]), bg=COL["bg"])
        logo.pack(side="left", padx=(10, 8))
        title = tk.Label(inner, text=iconlib.style_title(style), bg=COL["bg"], fg=COL["fg"],
                         font=theme.font(self, 10, "bold"))
        title.pack(side="left")
        count = tk.Label(inner, text=f"({len(found)})", bg=COL["bg"], fg=COL["faint"], font=theme.font(self, 10))
        count.pack(side="left", padx=(6, 0))
        for w in (head, inner, chev, logo, title, count):
            w.bind("<Button-1>", lambda e, s=style: self._toggle(s))
        tk.Frame(self.list, bg=COL["border"], height=1).pack(fill="x")
        if expanded and found:
            grid = tk.Frame(self.list, bg=COL["bg"], padx=12, pady=10)
            grid.pack(fill="x")
            self.grids[style] = (grid, found)
            tk.Frame(self.list, bg=COL["border"], height=1).pack(fill="x")
            self._fill(style)

    def _fill(self, style):
        grid, found = self.grids[style]
        for w in grid.winfo_children():
            w.destroy()
        t, cols = self.tile, self._columns()
        for i, name in enumerate(found):
            key = (name, style, t)
            if key not in self._photos:
                self._photos[key] = ImageTk.PhotoImage(_rounded(iconlib.render(name, style, t),
                                                                outline=style == "black"), master=self)
            cell = tk.Canvas(grid, width=t + 4, height=t + 4, bg=COL["bg"], highlightthickness=0, cursor="hand2")
            cell.grid(row=i // cols, column=i % cols, padx=GAP // 2, pady=GAP // 2)
            cell.create_image(t / 2 + 2, t / 2 + 2, image=self._photos[key])
            ring = cell.create_rectangle(1, 1, t + 3, t + 3, outline="", width=2)
            cell.bind("<Enter>", lambda e, c=cell, r=ring: c.itemconfigure(r, outline=COL["accent"]))
            cell.bind("<Leave>", lambda e, c=cell, r=ring: c.itemconfigure(r, outline=""))
            cell.bind("<Button-1>", lambda e, n=name, s=style: self._pick(n, s))
            cell.label = iconlib.label(name)
        self._cols = cols

    def _columns(self):
        width = self.area.cv.winfo_width()
        width = (width if width > 50 else 440) - 2 * 12 - 4  # Rand des Rasters, Bildlaufleiste
        return max(1, width // (self.tile + 4 + GAP))

    def _relayout(self):
        if self._columns() != getattr(self, "_cols", None):
            for style in self.grids:
                self._fill(style)

    # ---------------------------------------------------------- Aktionen
    def _toggle(self, style):
        if self.query.get().strip():
            return
        self.open ^= {style}
        self._refresh()

    def _zoom(self, d):
        self.size_idx = max(0, min(len(SIZES) - 1, self.size_idx + d))
        self._refresh()

    def _pick(self, name, style):
        self.destroy()
        self.on_pick(name, style)

    def _file(self):
        self.destroy()
        self.on_file()


def _rounded(im, r=10, outline=False):
    """Ecken abrunden; outline: feiner Rand (helle Kacheln auf hellem Hintergrund)."""
    from PIL import Image, ImageDraw
    k = 4  # Maske überabgetastet: glatte Ecken
    mask = Image.new("L", (im.width * k, im.height * k), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, im.width * k - 1, im.height * k - 1), radius=r * k, fill=255)
    mask = mask.resize(im.size, Image.LANCZOS)
    out = Image.new("RGBA", im.size, (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    if outline:
        ring = Image.new("RGBA", (im.width * k, im.height * k), (0, 0, 0, 0))
        ImageDraw.Draw(ring).rounded_rectangle((0, 0, im.width * k - 1, im.height * k - 1), radius=r * k,
                                               outline=theme.rgb(COL["border"]) + (255,), width=k)
        out.alpha_composite(ring.resize(im.size, Image.LANCZOS))
    return out
