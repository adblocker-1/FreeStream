"""Dialog „Hintergrund“ im LinDeck-Fenster: Bild über alle Tasten einer Seite und Touchstreifen."""
import tkinter as tk
from tkinter import filedialog, messagebox

from PIL import Image, ImageTk

from . import editdoc as E
from . import render
from .i18n import _, ngettext
from .dialogkit import field, footer, header
from .theme import COL, Button, Card, Dialog, Fader, Icons, icon_tile, label

IMAGE_TYPES = [(_("Images"), "*.png *.jpg *.jpeg *.webp *.gif *.bmp"), (_("All files"), "*")]


class BackgroundDialog(Dialog):
    def __init__(self, master, app, page_uuid):
        super().__init__(master, _("Background"), width=560)
        self.app, self.doc, self.page_uuid = app, app.doc, page_uuid
        self.icons = Icons(self)
        self._job = None
        g = self.doc.geometry
        b = self.body
        header(b, _("Background image"),
               _("The image is spread across all keys of this page – like a desktop wallpaper. "
                 "Keys with their own icon sit on top; empty keys show the image."), icon="image", wrap=480)

        card = Card(b, pad=18, stretch=True)
        card.pack(fill="x")
        self.preview = tk.Label(card.body, bg=COL["panel"])
        self.preview.pack(side="left", anchor="n")
        side = tk.Frame(card.body, bg=COL["panel"])
        side.pack(side="left", padx=(18, 0), fill="x", expand=True, anchor="n")
        row = tk.Frame(side, bg=COL["panel"])
        row.pack(anchor="w")
        Button(row, _("Choose image …"), icon="folder", kind="primary", command=self.choose).pack(side="left")
        self.remove_btn = Button(row, _("Remove"), icon="trash", command=self.remove)
        self.remove_btn.pack(side="left", padx=(8, 0))
        self.all_btn = Button(side, _("Apply to all pages"), icon="layers", kind="ghost", command=self.apply_all,
                              padx=6)
        self.all_btn.pack(anchor="w", pady=(8, 0))
        field(side, _("DIM"), pady=(16, 4))
        row = tk.Frame(side, bg=COL["panel"])
        row.pack(anchor="w", fill="x")
        self.dim = Fader(row, length=190, thickness=26, orient="horizontal", from_=0, to=80, color=COL["accent"],
                         command=self._dim_changed)
        self.dim.pack(side="left")
        self.dim_val = tk.StringVar(self, value="")
        label(row, textvariable=self.dim_val, size=9, width=6, anchor="e").pack(side="left", padx=(6, 0))
        label(side, _("Darkens the image so titles stay easy to read."), 9, color=COL["muted"],
              wraplength=260, justify="left").pack(anchor="w", pady=(2, 0))

        if g.dials:
            sc = Card(b, pad=18, stretch=True)
            sc.pack(fill="x", pady=(12, 0))
            head = tk.Frame(sc.body, bg=COL["panel"])
            head.pack(fill="x", pady=(0, 12))
            tk.Label(head, image=icon_tile(head, "layers", 32, COL["stream"]), bg=COL["panel"]).pack(
                side="left", anchor="n")
            txt = tk.Frame(head, bg=COL["panel"])
            txt.pack(side="left", padx=12, fill="x", expand=True)
            label(txt, _("Touch strip"), 11, "bold").pack(anchor="w")
            label(txt, _("Custom image for the whole touch strip of the Stream Deck + (as in the Elgato app)."),
                  9, color=COL["muted"], wraplength=420, justify="left").pack(anchor="w")
            body = tk.Frame(sc.body, bg=COL["panel"])
            body.pack(fill="x")
            self.strip_prev = tk.Label(body, bg=COL["panel"])
            self.strip_prev.pack(side="left")
            sb = tk.Frame(body, bg=COL["panel"])
            sb.pack(side="left", padx=(18, 0))
            Button(sb, _("Choose image …"), icon="folder", command=self.choose_strip).pack(anchor="w")
            self.strip_remove = Button(sb, _("Remove"), icon="trash", command=self.remove_strip)
            self.strip_remove.pack(anchor="w", pady=(8, 0))
        else:
            self.strip_prev = None
        footer(self)
        Button(self.buttons, _("Done"), kind="primary", command=self.destroy).pack(side="right")
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
        for w in (self.remove_btn, self.all_btn):
            w.configure_text(enabled=path is not None)
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
            self.strip_remove.configure_text(enabled=bool(sp))

    def _changed(self):
        self.refresh()
        self.app.refresh_all()

    # -------------------------------------------------------------- Aktionen
    def choose(self):
        f = filedialog.askopenfilename(parent=self, title=_("Choose background image"), filetypes=IMAGE_TYPES)
        if not f:
            return
        try:
            self.doc.set_background(self.page_uuid, f)
        except E.EditError as e:
            messagebox.showerror(_("Background"), str(e), parent=self)
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
        self.app.status(ngettext("Background applied to {n} more page", "Background applied to {n} more pages",
                                  n).format(n=n))
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
        f = filedialog.askopenfilename(parent=self, title=_("Image for the touch strip"), filetypes=IMAGE_TYPES)
        if not f:
            return
        try:
            self.doc.set_strip_background(self.page_uuid, f)
        except E.EditError as e:
            messagebox.showerror(_("Background"), str(e), parent=self)
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
