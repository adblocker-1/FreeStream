"""Ansicht „Audio Flow“ von FreeWave: der Signalweg als Graph auf einem Canvas.

Spalten Eingänge → Kanäle → Mixe → Ausgänge mit geschwungenen Verbindungen (Cyan: Eingang zu Kanal,
Magenta: Kanal zu Mix, Grün: Mix zu Ausgang, grau gestrichelt: stumm). Ein Klick auf ein Element hebt
seinen ganzen Weg hervor und blendet den Rest ab; „Auswahl aufheben“ (Kopfzeile) oder ein Klick ins
Leere zeigt wieder alles. Das Modell (welche Knoten, welche Kanten, welcher Weg) steht in audioflow.py;
hier wird nur gezeichnet. Die Daten kommen aus dem Zustand, den das Fenster ohnehin regelmäßig im
Arbeits-Thread einliest – die Ansicht selbst fragt PipeWire nie.
"""
import hashlib
import tkinter as tk

from PIL import Image, ImageDraw, ImageTk

from . import audioflow, theme
from .dialogkit import ScrollArea
from .i18n import _
from .theme import COL, rounded_rect

EDGE_COLORS = {"input": "#22c3ee", "channel": "#d946ef", "mix": "#2bd68a"}
ICON = 30
HEADS = {"input": (_("Inputs"), "mic"), "channel": (_("Channels"), "speaker"), "mix": (_("Mixes"), "layers"),
         "output": (_("Outputs"), "headphones")}


def _dim(im, factor=0.35):
    """Bild abblenden (Alpha verringern) – für Elemente außerhalb des gewählten Wegs."""
    im = im.copy()
    a = im.getchannel("A").point(lambda v: int(v * factor))
    im.putalpha(a)
    return im


def _tile(glyph, size, color, fg="#ffffff"):
    k = 4
    S = size * k
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle((0, 0, S - 1, S - 1), radius=S * 0.24, fill=theme.rgb(color) + (255,))
    im.alpha_composite(theme.glyph(glyph, int(S * 0.56), fg), (int(S * 0.22), int(S * 0.22)))
    return im.resize((size, size), Image.LANCZOS)


def _letter(text, size):
    """Ersatz-Symbol für ein Programm ohne eigenes Icon: Anfangsbuchstabe auf einer Farbe aus dem Namen."""
    colors = ("#2f7bff", "#30d158", "#ff8a3d", "#a66bff", "#ff4d5a", "#22c3ee", "#ffb020", "#ff6bd5")
    c = colors[int(hashlib.md5(text.encode()).hexdigest(), 16) % len(colors)]
    k = 4
    S = size * k
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, S - 1, S - 1), radius=S * 0.24, fill=theme.rgb(c) + (255,))
    ch = (text.strip()[:1] or "?").upper()
    try:
        from PIL import ImageFont
        fnt = ImageFont.truetype("DejaVuSans-Bold.ttf", int(S * 0.55))
    except OSError:
        fnt = None
    if fnt is not None:
        d.text((S / 2, S / 2), ch, font=fnt, fill=(255, 255, 255, 255), anchor="mm")
    return im.resize((size, size), Image.LANCZOS)


class FlowView:
    """Graph im Rahmen frame; update(st) mit dem Fensterzustand, on_select(sel) meldet die Auswahl."""

    def __init__(self, frame, root, on_select=None):
        self.root, self.on_select = root, on_select
        self.graph = {"nodes": {}, "columns": {c: [] for c in audioflow.COLUMNS}, "edges": []}
        self.st = {}
        self.selected = None
        self._sig = None
        self._drawn = None
        self._imgs = {}
        self._app_imgs = {}
        self.visible = False
        self.legend = tk.Canvas(frame, height=34, bg=COL["bg"], highlightthickness=0, bd=0)
        self.legend.pack(side="bottom", fill="x", padx=22, pady=(0, 4))
        self.legend.bind("<Configure>", lambda e: self._draw_legend())
        self.area = ScrollArea(frame, bg=COL["bg"])
        self.area.pack(fill="both", expand=True, padx=(22, 10), pady=(4, 0))
        self.cv = tk.Canvas(self.area.inner, bg=COL["bg"], highlightthickness=0, bd=0, height=400)
        self.cv.pack(fill="x", expand=True)
        self.cv.bind("<Configure>", self._resized)
        self.cv.bind("<Button-1>", self._click_bg)
        self.f_title = theme.font(root, 10, "bold")
        self.f_sub = theme.font(root, 8)
        self.f_head = theme.font(root, 8, "bold")
        self.icons = theme.Icons(self.cv)

    # ------------------------------------------------------------ Daten
    def update(self, st):
        self.st = st
        self.graph = audioflow.build(st) if st.get("mixer_active") else \
            {"nodes": {}, "columns": {c: [] for c in audioflow.COLUMNS}, "edges": []}
        if self.selected is not None and self.selected not in self.graph["nodes"]:
            self.select(None)
        self._sig = (audioflow.signature(self.graph), bool(st.get("mixer_active")))
        if self.visible:
            self.draw()

    def show(self):
        self.visible = True
        self.draw()

    def hide(self):
        self.visible = False

    def select(self, nid):
        if nid == self.selected:
            return
        self.selected = nid
        self._drawn = None
        if self.visible:
            self.draw()
        if self.on_select:
            self.on_select(nid)

    def _click_bg(self, e):
        if not self.cv.find_withtag("current") or "bg" in self.cv.gettags("current"):
            self.select(None)

    def _resized(self, e):
        if self.visible and e.width != getattr(self, "_w", None):
            self._w = e.width
            job = getattr(self, "_job", None)
            if job:
                self.root.after_cancel(job)
            self._job = self.root.after(60, self._redraw)

    def _redraw(self):
        self._job = None
        self._drawn = None
        self.draw()

    # ------------------------------------------------------------ Bilder
    def _photo(self, key, make):
        img = self._imgs.get(key)
        if img is None:
            if len(self._imgs) > 400:
                self._imgs.clear()
            img = self._imgs[key] = ImageTk.PhotoImage(make(), master=self.cv)
        return img

    def _app_image(self, n, dim):
        path = (self.st.get("app_icons") or {}).get(n.get("app") or "")
        key = ("app", path or n["title"], dim)

        def make():
            im = None
            if path:
                try:
                    im = Image.open(path).convert("RGBA").resize((ICON, ICON), Image.LANCZOS)
                except Exception:  # noqa: BLE001 – kaputtes Symbol: Buchstabe statt Absturz
                    im = None
            if im is None:
                im = _letter(n["title"], ICON)
            return _dim(im) if dim else im
        return self._photo(key, make)

    def _node_image(self, n, dim):
        kind = n["kind"]
        if kind == "stream":
            return self._app_image(n, dim)
        if kind == "channel":
            glyph, color = theme.channel_look(n["ref"])
            return self._photo(("tile", glyph, color, dim),
                               lambda: _dim(_tile(glyph, ICON, color)) if dim else _tile(glyph, ICON, color))
        if kind == "hardware":
            glyph = "mic"
        elif kind == "empty":
            glyph = "speaker"
        elif kind == "mix":
            glyph = (n["ref"] or {}).get("icon") or "wave"
        elif kind == "virtual":
            glyph = "stream"
        else:
            glyph = "speaker_off" if n["muted"] else "speaker"
        color = COL["faint"] if dim else (COL["muted"] if kind == "empty" else COL["fg"])
        return self.icons.get(glyph, 18, color)

    # ------------------------------------------------------------ Zeichnen
    def draw(self):
        width = max(self.cv.winfo_width(), 200)
        key = (self._sig, self.selected, width)
        if key == self._drawn:
            return
        self._drawn = key
        cv = self.cv
        cv.delete("all")
        if not self.st.get("mixer_active"):
            cv.configure(height=200)
            cv.create_text(4, 30, anchor="w", fill=COL["muted"], font=theme.font(self.root, 10),
                           text=_("Set up the mixer to see the audio flow."))
            return
        g = self.graph
        lay = audioflow.layout(g, width - 6, top=44)
        boxes = lay["boxes"]
        cv.configure(height=lay["height"] + 16)
        cv.create_rectangle(0, 0, lay["width"] + 10, lay["height"] + 20, fill=COL["bg"], outline="", tags="bg")
        on_nodes, on_edges = audioflow.path(g, self.selected) if self.selected else (None, None)
        # Spaltenköpfe
        for col, (x, w) in lay["cols"].items():
            text, glyph = HEADS[col]
            text = text.upper()
            tw = theme.measure(cv, self.f_head, text) + 16 + 22
            cx = x + w / 2
            rounded_rect(cv, cx - tw / 2, 6, cx + tw / 2, 32, 6, fill=COL["panel"], outline="", tags="bg")
            cv.create_image(cx - tw / 2 + 16, 19, image=self.icons.get(glyph, 12, COL["muted"]), tags="bg")
            cv.create_text(cx - tw / 2 + 28, 19, text=text, anchor="w", fill=COL["muted"], font=self.f_head,
                           tags="bg")
        # Verbindungen: erst die abgeblendeten, dann die farbigen darüber
        faint = theme.mix(COL["bg"], COL["fg"], 0.16)
        order = sorted(range(len(g["edges"])), key=lambda i: on_edges is None or i in on_edges)
        for i in order:
            e = g["edges"][i]
            if e["src"] not in boxes or e["dst"] not in boxes:
                continue
            x0, y0, w0, h0 = boxes[e["src"]]
            x1, y1, _w1, h1 = boxes[e["dst"]]
            pts = audioflow.curve((x0 + w0, y0 + h0 / 2), (x1, y1 + h1 / 2))
            active = on_edges is None or i in on_edges
            if not active:
                cv.create_line(*pts, smooth="raw", splinesteps=24, fill=faint, width=1.5, tags="bg")
            elif e["muted"]:
                cv.create_line(*pts, smooth="raw", splinesteps=24, fill=COL["muted"], width=1.5, dash=(4, 4),
                               tags="bg")
            else:
                cv.create_line(*pts, smooth="raw", splinesteps=24, fill=EDGE_COLORS[e["kind"]],
                               width=2.5 if on_edges is not None else 2, tags="bg")
        # Karten
        for k, (nid, (x, y, w, h)) in enumerate(boxes.items()):
            n = g["nodes"][nid]
            dim = on_nodes is not None and nid not in on_nodes
            tag = f"n{k}"
            if nid == self.selected:
                fill = COL["raised"]
            elif dim:
                fill = theme.mix(COL["panel"], COL["bg"], 0.55)
            else:
                fill = COL["panel"]
            rounded_rect(cv, x, y, x + w, y + h, 8, fill=fill, outline="", tags=(tag, "card"))
            img = self._node_image(n, dim)
            cv.create_image(x + 12 + ICON / 2, y + h / 2, image=img, tags=(tag,))
            tx = x + 12 + ICON + 12
            avail = w - (tx - x) - 10
            fg = COL["faint"] if dim else (COL["muted"] if n["kind"] == "empty" else COL["fg"])
            title = theme.elide(cv, n["title"], self.f_title, avail)
            if n["sub"]:
                cv.create_text(tx, y + h / 2 - 8, text=title, anchor="w", fill=fg, font=self.f_title, tags=(tag,))
                sub_fg = COL["faint"] if dim else (COL["danger"] if n["muted"] and n["kind"] != "stream"
                                                   else COL["muted"])
                cv.create_text(tx, y + h / 2 + 9, text=theme.elide(cv, n["sub"], self.f_sub, avail), anchor="w",
                               fill=sub_fg, font=self.f_sub, tags=(tag,))
            else:
                cv.create_text(tx, y + h / 2, text=title, anchor="w", fill=fg, font=self.f_title, tags=(tag,))
            cv.tag_bind(tag, "<Button-1>", lambda e, nid=nid: (self.select(None if nid == self.selected else nid),
                                                                "break")[1])
            cv.tag_bind(tag, "<Enter>", lambda e, t=tag, nid=nid: self._hover(t, nid, True))
            cv.tag_bind(tag, "<Leave>", lambda e, t=tag, nid=nid: self._hover(t, nid, False))
        self._draw_legend()

    def _hover(self, tag, nid, on):
        self.cv.configure(cursor="hand2" if on else "")
        if nid == self.selected:
            return
        for item in self.cv.find_withtag(tag):
            if "card" in self.cv.gettags(item):
                on_nodes = audioflow.path(self.graph, self.selected)[0] if self.selected else None
                dim = on_nodes is not None and nid not in on_nodes
                base = theme.mix(COL["panel"], COL["bg"], 0.55) if dim else COL["panel"]
                self.cv.itemconfigure(item, fill=theme.mix(base, COL["fg"], 0.06) if on else base)

    def _draw_legend(self):
        cv = self.legend
        cv.delete("all")
        f = self.f_sub
        x, y = 2, 17
        for text, color, dash in ((_("Input to channel"), EDGE_COLORS["input"], None),
                                  (_("Channel to mix"), EDGE_COLORS["channel"], None),
                                  (_("Mix to output"), EDGE_COLORS["mix"], None),
                                  (_("Muted"), COL["muted"], (4, 3)),
                                  (_("Not in path"), theme.mix(COL["bg"], COL["fg"], 0.3), None)):
            kw = {"dash": dash} if dash else {}
            cv.create_line(x, y, x + 20, y, fill=color, width=2, **kw)
            cv.create_text(x + 28, y, text=text, anchor="w", fill=COL["muted"], font=f)
            x += 28 + theme.measure(cv, f, text) + 24
