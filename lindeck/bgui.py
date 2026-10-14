"""Dialog „Hintergrund“ im LinDeck-Fenster: Bild über alle Tasten einer Seite und Touchstreifen."""
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from . import editdoc as E
from . import render
from .theme import COL, Card, Dialog, Fader, Icons, label

IMAGE_TYPES = [("Bilder", "*.png *.jpg *.jpeg *.webp *.gif *.bmp"), ("Alle Dateien", "*")]


class BackgroundDialog(Dialog):
    def __init__(self, master, app, page_uuid):
        super().__init__(master, "Hintergrund", width=560)
        self.app, self.doc, self.page_uuid = app, app.doc, page_uuid
        self.icons = Icons(self)
        self._job = None
        g = self.doc.geometry
        b = self.body
        label(b, "Hintergrundbild", 15, "bold").pack(anchor="w")
        label(b, "Das Bild wird über alle Tasten dieser Seite verteilt – wie ein Hintergrundbild auf dem "
                 "Desktop. Tasten mit eigenem Symbol liegen darüber; leere Tasten zeigen das Bild.",
              9, color=COL["muted"], wraplength=500, justify="left").pack(anchor="w", pady=(2, 12))

        card = Card(b, pad=16, stretch=True)
        card.pack(fill="x")
        self.preview = tk.Label(card.body, bg=COL["panel"])
        self.preview.pack(side="left")
        side = tk.Frame(card.body, bg=COL["panel"])
        side.pack(side="left", padx=16, fill="x", expand=True, anchor="n")
        ttk.Button(side, text="Bild wählen …", style="Accent.TButton", command=self.choose).pack(anchor="w")
        self.remove_btn = ttk.Button(side, text="Entfernen", command=self.remove)
        self.remove_btn.pack(anchor="w", pady=(6, 0))
        self.all_btn = ttk.Button(side, text="Auf alle Seiten übernehmen", style="Ghost.TButton",
                                  command=self.apply_all)
        self.all_btn.pack(anchor="w", pady=(6, 0))
        label(side, "ABDUNKELN", 9, "bold", color=COL["muted"]).pack(anchor="w", pady=(14, 4))
        row = tk.Frame(side, bg=COL["panel"])
        row.pack(anchor="w")
        self.dim = Fader(row, length=200, thickness=28, orient="horizontal", from_=0, to=80, color=COL["accent"],
                         command=self._dim_changed)
        self.dim.pack(side="left")
        self.dim_val = tk.StringVar(self, value="")
        label(row, textvariable=self.dim_val, size=9, width=6).pack(side="left", padx=6)
        label(side, "Macht das Bild dunkler, damit Titel gut lesbar bleiben.", 8, color=COL["faint"]).pack(
            anchor="w", pady=(2, 0))

        if g.dials:
            label(b, "Touchstreifen", 13, "bold").pack(anchor="w", pady=(18, 2))
            label(b, "Eigenes Bild für den ganzen Touchstreifen des Stream Deck + (wie in der Elgato-App).",
                  9, color=COL["muted"]).pack(anchor="w", pady=(0, 8))
            sc = Card(b, pad=16, stretch=True)
            sc.pack(fill="x")
            self.strip_prev = tk.Label(sc.body, bg=COL["panel"])
            self.strip_prev.pack(side="left")
            sb = tk.Frame(sc.body, bg=COL["panel"])
            sb.pack(side="left", padx=16, anchor="n")
            ttk.Button(sb, text="Bild wählen …", command=self.choose_strip).pack(anchor="w")
            self.strip_remove = ttk.Button(sb, text="Entfernen", command=self.remove_strip)
            self.strip_remove.pack(anchor="w", pady=(6, 0))
        else:
            self.strip_prev = None
        ttk.Button(self.buttons, text="Fertig", style="Accent.TButton", command=self.destroy).pack(side="right")
        self.refresh()
        self.show()

    # ------------------------------------------------------------ Anzeige
    def refresh(self):
        g = self.doc.geometry
        path, dim = self.doc.background(self.page_uuid)
        ks = 34
        gap = int(ks * render.BG_GAP)
        W, H = g.cols * ks + (g.cols + 1) * gap, g.rows * ks + (g.rows + 1) * gap
        img = Image.new("RGBA", (W, H), (13, 14, 16, 255))
        tiles = render.background_tiles(path, g.cols, g.rows, (ks, ks), dim) if path else {}
        for r in range(g.rows):
            for c in range(g.cols):
                x, y = gap + c * (ks + gap), gap + r * (ks + gap)
                t = tiles.get((c, r)) or Image.new("RGBA", (ks, ks), (40, 41, 46, 255))
                img.alpha_composite(_round(t, 6), (x, y))
        self._ph = ImageTk.PhotoImage(_round(img, 12), master=self)
        self.preview.configure(image=self._ph)
        self.dim.set(dim if path else 30)
        self.dim_val.set(f"{dim if path else 30} %")
        state = ["!disabled"] if path else ["disabled"]
        for w in (self.remove_btn, self.all_btn):
            w.state(state)
        self.dim.enabled = path is not None
        self.dim.set_dimmed(path is None)
        if self.strip_prev is not None:
            sp = self.doc.strip_background(self.page_uuid)
            strip = Image.new("RGBA", (240, 40), (0, 0, 0, 255))
            if sp:
                fr = render.load_frames(sp, 240, 40)
                if fr:
                    strip.alpha_composite(fr[0][0].convert("RGBA").resize((240, 40)))
            self._sph = ImageTk.PhotoImage(_round(strip, 8), master=self)
            self.strip_prev.configure(image=self._sph)
            self.strip_remove.state(["!disabled"] if sp else ["disabled"])

    def _changed(self):
        self.refresh()
        self.app.refresh_all()

    # -------------------------------------------------------------- Aktionen
    def choose(self):
        f = filedialog.askopenfilename(parent=self, title="Hintergrundbild wählen", filetypes=IMAGE_TYPES)
        if not f:
            return
        try:
            self.doc.set_background(self.page_uuid, f)
        except E.EditError as e:
            messagebox.showerror("Hintergrund", str(e), parent=self)
            return
        self._changed()

    def remove(self):
        self.doc.set_background(self.page_uuid, None)
        self._changed()

    def apply_all(self):
        path, dim = self.doc.background(self.page_uuid)
        if not path:
            return
        n = 0
        for pid in self.doc.page_ids:
            if pid != self.page_uuid:
                self.doc.set_background(pid, path, dim)
                n += 1
        self.app.status(f"Hintergrund auf {n} weitere Seite(n) übernommen")
        self._changed()

    def _dim_changed(self, value):
        v = int(round(value))
        self.dim_val.set(f"{v} %")
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(250, lambda: self._apply_dim(v))

    def _apply_dim(self, v):
        self._job = None
        self.doc.set_background(self.page_uuid, None, dim=v, keep_image=True)
        self._changed()

    def choose_strip(self):
        f = filedialog.askopenfilename(parent=self, title="Bild für den Touchstreifen", filetypes=IMAGE_TYPES)
        if not f:
            return
        try:
            self.doc.set_strip_background(self.page_uuid, f)
        except E.EditError as e:
            messagebox.showerror("Hintergrund", str(e), parent=self)
            return
        self._changed()

    def remove_strip(self):
        self.doc.set_strip_background(self.page_uuid, None)
        self._changed()


def _round(img, r):
    from PIL import ImageDraw
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, img.size[0] - 1, img.size[1] - 1), radius=r, fill=255)
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out
