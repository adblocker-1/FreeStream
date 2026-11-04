"""Ansicht „Audio Flow“ von FreeWave: der Signalweg als Graph auf einem Canvas.

Spalten Eingänge → Kanäle → Mixe → Ausgänge mit geschwungenen Verbindungen (Cyan: Eingang zu Kanal,
Magenta: Kanal zu Mix, Grün: Mix zu Ausgang, grau gestrichelt: stumm). Ein Klick auf ein Element hebt
seinen ganzen Weg hervor und blendet den Rest ab; „Auswahl aufheben“ (Kopfzeile) oder ein Klick ins
Leere zeigt wieder alles. Das Modell (welche Knoten, welche Kanten, welcher Weg) steht in audioflow.py;
hier wird nur gezeichnet. Die Daten kommen aus dem Zustand, den das Fenster ohnehin regelmäßig im
Arbeits-Thread einliest – die Ansicht selbst fragt PipeWire nie.
"""
import hashlib
import logging
import time
import tkinter as tk

from PIL import Image, ImageDraw, ImageFilter, ImageTk

from . import audioflow, theme
from .dialogkit import ScrollArea
from .i18n import _
from .theme import COL, rounded_rect

log = logging.getLogger(__name__)
EDGE_COLORS = {"input": "#22c3ee", "channel": "#d946ef", "mix": "#2bd68a"}
ICON = 30
RADIUS = 10
SS = 2  # Überabtastung der Linien (Tk zeichnet ohne Kantenglättung)
DIM = 0.32  # Deckkraft von allem außerhalb des gewählten Wegs
PULSE = 2.0  # Sekunden, die ein Lichtpunkt vom Anfang bis zum Ende einer Verbindung braucht
FRAME_MS = 40
HEADS = {"input": (_("Inputs"), "mic"), "channel": (_("Channels"), "speaker"), "mix": (_("Mixes"), "layers"),
         "output": (_("Outputs"), "headphones")}


def _dim(im, factor=DIM):
    """Bild abblenden (Alpha verringern) – für Elemente außerhalb des gewählten Wegs."""
    im = im.copy()
    a = im.getchannel("A").point(lambda v: int(v * factor))
    im.putalpha(a)
    return im


def _dashed(draw, xy, fill, width, dash):
    """Gestrichelte Linie entlang der Punkte xy (Strich und Lücke je dash lang)."""
    run, on, seg = 0.0, True, [xy[0]]
    for (x0, y0), (x1, y1) in zip(xy, xy[1:]):
        run += ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        seg.append((x1, y1))
        if run >= dash:
            if on:
                draw.line(seg, fill=fill, width=width)
            on, run, seg = not on, 0.0, [(x1, y1)]
    if on and len(seg) > 1:
        draw.line(seg, fill=fill, width=width)


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
        self._pulses = []  # [(Punkt-Items, Kurvenpunkte)] auf dem gewählten Weg
        self._pulse_job = None
        self._card_clicked = False
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
        empty = {"nodes": {}, "columns": {c: [] for c in audioflow.COLUMNS}, "edges": []}
        try:
            self.graph = audioflow.build(st) if st.get("mixer_active") else empty
        except Exception:  # noqa: BLE001 – unerwartete Daten: lieber leer als eingefroren
            log.exception("Audio Flow: could not build the graph")
            self.graph = empty
        if self.selected is not None and self.selected not in self.graph["nodes"]:
            self.select(None)
        self._sig = (audioflow.signature(self.graph), bool(st.get("mixer_active")))
        if self.visible:
            self.draw()

    def show(self):
        self.visible = True
        self.draw()
        if self._pulses and not self._pulse_job:  # Ansicht wieder sichtbar, nichts neu gezeichnet
            self._pulse()

    def hide(self):
        self.visible = False
        self._stop_pulse()

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
        # Tk ruft nach dem Klick auf eine Karte auch diesen Klick auf die Fläche auf („break“ wirkt dort nicht) –
        # dann ist schon neu gezeichnet und unter dem Zeiger liegt nur der Hintergrund: Auswahl nicht aufheben
        if self._card_clicked:
            self._card_clicked = False
            return
        if not self.cv.find_withtag("current") or "bg" in self.cv.gettags("current"):
            self.select(None)

    def _click_card(self, nid):
        self._card_clicked = True
        self.select(None if nid == self.selected else nid)

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
        color = theme.mix(COL["bg"], COL["fg"], DIM) if dim else (COL["faint"] if kind == "empty" else COL["fg"])
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
        lay = audioflow.layout(g, width - 6, top=44, row_h=48, gap=12)
        boxes = lay["boxes"]
        cv.configure(height=lay["height"] + 16)
        back = cv.create_rectangle(0, 0, lay["width"] + 10, lay["height"] + 20, fill=COL["bg"], outline="",
                                   tags="bg")
        on_nodes, on_edges = audioflow.path(g, self.selected) if self.selected else (None, None)
        # Spaltenköpfe: Name und Anzahl linksbündig über der Spalte, feine Linie darunter
        for col, (x, w) in lay["cols"].items():
            text = HEADS[col][0].upper()
            cv.create_text(x + 2, 18, text=text, anchor="w", fill=COL["muted"], font=self.f_head, tags="bg")
            tw = theme.measure(cv, self.f_head, text)
            cv.create_text(x + 10 + tw, 18, text=str(len(g["columns"][col])), anchor="w", fill=COL["faint"],
                           font=self.f_head, tags="bg")
            cv.create_line(x, 32, x + w, 32, fill=COL["border"], tags="bg")
        # Verbindungen (und gestrichelte leere Eingänge) als ein geglättetes Bild unter den Karten
        self._stop_pulse()
        self._pulses = []
        failed = []
        ends = audioflow.anchors(g, boxes)
        try:
            self._edge_photo = ImageTk.PhotoImage(self._edge_image(g, boxes, ends, on_edges, lay), master=cv)
            cv.tag_lower(cv.create_image(0, 0, image=self._edge_photo, anchor="nw", tags="bg"))
            cv.tag_lower(back)  # Spaltenköpfe liegen darüber
        except Exception as exc:  # noqa: BLE001 – ohne Linien lieber trotzdem die Karten zeigen
            failed.append(exc)
        # Karten
        for k, (nid, box) in enumerate(boxes.items()):
            try:
                self._draw_card(k, nid, box, on_nodes)
            except Exception as exc:  # noqa: BLE001 – wie oben: die übrigen Karten trotzdem zeichnen
                failed.append(exc)
        # gewählter Weg: Punkte an beiden Enden, Lichtpunkte laufen entlang (über allem, auch am Kartenrand)
        if on_edges is not None:
            for i in sorted(on_edges):
                e = g["edges"][i]
                if i not in ends or e["muted"]:
                    continue
                color = EDGE_COLORS[e["kind"]]
                (ax, ay), (bx, by) = ends[i]
                for x, y in ((ax, ay), (bx, by)):
                    cv.create_oval(x - 3.5, y - 3.5, x + 3.5, y + 3.5, fill=color, outline="", tags="bg")
                items = (cv.create_oval(0, 0, 0, 0, fill=theme.mix(COL["bg"], color, 0.45), outline="", tags="bg"),
                         cv.create_oval(0, 0, 0, 0, fill="#ffffff", outline="", tags="bg"))
                self._pulses.append((items, audioflow.points(ends[i][0], ends[i][1])))
        if self._pulses:
            self._pulse()
        if failed:
            log.error("Audio Flow: %d element(s) could not be drawn", len(failed), exc_info=failed[0])
        self._draw_legend()

    def _edge_image(self, g, boxes, ends, on_edges, lay):
        """Alle Verbindungen in doppelter Größe gezeichnet und verkleinert (glatte Kanten); der gewählte Weg
        dicker und mit Leuchten, der Rest dünn bzw. abgeblendet; leere Eingänge als gestrichelter Umriss."""
        k = SS
        size = (int(lay["width"] + 10), int(lay["height"] + 20))
        im = Image.new("RGB", (size[0] * k, size[1] * k), theme.rgb(COL["bg"]))  # deckend: kein Alpha nötig
        faint = theme.rgb(theme.mix(COL["bg"], COL["fg"], 0.16))
        lit = on_edges is not None
        curves = {i: audioflow.points(*ends[i], steps=40) for i in ends}
        if lit:  # Leuchten in einfacher Größe und nur im nötigen Ausschnitt weichzeichnen (schnell)
            glow = Image.new("RGBA", size, (0, 0, 0, 0))
            dg = ImageDraw.Draw(glow)
            for i in on_edges:
                if i in curves and not g["edges"][i]["muted"]:
                    dg.line(curves[i], fill=theme.rgb(EDGE_COLORS[g["edges"][i]["kind"]]) + (150,), width=7,
                            joint="curve")
            box = glow.getbbox()
            if box:
                box = (max(box[0] - 12, 0), max(box[1] - 12, 0), min(box[2] + 12, size[0]), min(box[3] + 12, size[1]))
                part = glow.crop(box).filter(ImageFilter.GaussianBlur(4))
                part = part.resize(((box[2] - box[0]) * k, (box[3] - box[1]) * k), Image.BILINEAR)
                im.paste(part, (box[0] * k, box[1] * k), part)
        dl = ImageDraw.Draw(im)
        for i in sorted(curves, key=lambda i: on_edges is None or i in on_edges):
            e = g["edges"][i]
            pts = curves[i]
            xy = [(pts[j] * k, pts[j + 1] * k) for j in range(0, len(pts), 2)]
            if not (on_edges is None or i in on_edges):
                dl.line(xy, fill=faint, width=round(1.4 * k), joint="curve")
            elif e["muted"]:
                _dashed(dl, xy, theme.rgb(COL["muted"]), round(1.5 * k), 4 * k)
            else:
                color = theme.rgb(EDGE_COLORS[e["kind"]])
                if not lit:  # ohne Auswahl etwas zurückhaltender: Farbe leicht zum Hintergrund
                    color = theme.rgb(theme.mix(COL["bg"], EDGE_COLORS[e["kind"]], 0.85))
                dl.line(xy, fill=color, width=round((2.4 if lit else 1.6) * k), joint="curve")
        # leere Eingänge: nur ein gestrichelter Umriss (die Karte darüber ist unsichtbar, nur klickbar)
        d = ImageDraw.Draw(im)
        for nid, (x, y, w, h) in boxes.items():
            if g["nodes"][nid]["kind"] != "empty":
                continue
            dim = on_edges is not None and nid not in audioflow.path(g, self.selected)[0]
            col = theme.mix(COL["bg"], COL["border"], DIM) if dim else COL["border"]
            d.rounded_rectangle([x * k, y * k, (x + w) * k, (y + h) * k], RADIUS * k, outline=theme.rgb(col),
                                width=k)
            for xx in range(int(x + RADIUS + 4), int(x + w - RADIUS - 4), 9):  # Lücken in die Linie
                for yy in (y, y + h):
                    d.line([(xx * k, yy * k), ((xx + 4) * k, yy * k)], fill=theme.rgb(COL["bg"]), width=2 * k)
        return im.reduce(k)

    def _pulse(self):
        """Lichtpunkte ein Stück weiterschieben (alle gleichzeitig, wie ein Signal, das durch den Weg läuft)."""
        self._pulse_job = None
        if not self._pulses or not self.visible:
            return
        t = (time.monotonic() % PULSE) / PULSE
        for (glow, dot), pts in self._pulses:
            n = len(pts) // 2 - 1
            f = t * n
            i = min(int(f), n - 1)
            u = f - i
            x = pts[2 * i] + (pts[2 * i + 2] - pts[2 * i]) * u
            y = pts[2 * i + 1] + (pts[2 * i + 3] - pts[2 * i + 1]) * u
            self.cv.coords(glow, x - 7, y - 7, x + 7, y + 7)
            self.cv.coords(dot, x - 3.5, y - 3.5, x + 3.5, y + 3.5)
        self._pulse_job = self.root.after(FRAME_MS, self._pulse)

    def _stop_pulse(self):
        if self._pulse_job:
            self.root.after_cancel(self._pulse_job)
            self._pulse_job = None

    def _draw_card(self, k, nid, box, on_nodes):
        cv = self.cv
        x, y, w, h = box
        n = self.graph["nodes"][nid]
        dim = on_nodes is not None and nid not in on_nodes
        tag = f"n{k}"
        empty = n["kind"] == "empty"
        fill, outline, width = self._card_look(nid, dim, empty)
        if empty:  # Umriss steckt im Linienbild; die Fläche hier nur zum Anklicken
            rounded_rect(cv, x + 2, y + 2, x + w - 2, y + h - 2, RADIUS - 2, fill=COL["bg"], outline="",
                         tags=(tag, "card"))
        else:
            rounded_rect(cv, x, y, x + w, y + h, RADIUS, fill=fill, outline=outline, width=width,
                         tags=(tag, "card"))
        cv.tag_bind(tag, "<Button-1>", lambda e, nid=nid: self._click_card(nid))
        cv.tag_bind(tag, "<Enter>", lambda e, t=tag, nid=nid: self._hover(t, nid, True))
        cv.tag_bind(tag, "<Leave>", lambda e, t=tag, nid=nid: self._hover(t, nid, False))
        tx = x + 12 + ICON + 12
        avail = w - (tx - x) - 10
        fg = theme.mix(COL["bg"], COL["fg"], DIM) if dim else (COL["faint"] if empty else COL["fg"])
        title = theme.elide(cv, n["title"], self.f_title, avail)
        if n["sub"]:
            cv.create_text(tx, y + h / 2 - 8, text=title, anchor="w", fill=fg, font=self.f_title, tags=(tag,))
            sub_fg = theme.mix(COL["bg"], COL["muted"], DIM) if dim else \
                (COL["danger"] if n["muted"] and n["kind"] != "stream" else COL["muted"])
            cv.create_text(tx, y + h / 2 + 9, text=theme.elide(cv, n["sub"], self.f_sub, avail), anchor="w",
                           fill=sub_fg, font=self.f_sub, tags=(tag,))
        else:
            cv.create_text(tx, y + h / 2, text=title, anchor="w", fill=fg, font=self.f_title, tags=(tag,))
        # Symbol zuletzt: fehlt es (kaputtes Bild), stehen Karte und Text trotzdem da
        cv.create_image(x + 12 + ICON / 2, y + h / 2, image=self._node_image(n, dim), tags=(tag,))

    def _card_look(self, nid, dim, empty):
        """(Fläche, Rand, Randbreite) einer Karte: gewählt mit Rand in der Akzentfarbe, abgeblendet blasser."""
        if nid == self.selected:
            return COL["raised"], COL["accent"], 1.5
        if dim:
            return theme.mix(COL["bg"], COL["panel"], DIM), theme.mix(COL["bg"], COL["border"], DIM), 1
        return COL["panel"], COL["border"], 1

    def _hover(self, tag, nid, on):
        self.cv.configure(cursor="hand2" if on else "")
        if nid == self.selected or self.graph["nodes"].get(nid, {}).get("kind") == "empty":
            return
        on_nodes = audioflow.path(self.graph, self.selected)[0] if self.selected else None
        base = self._card_look(nid, on_nodes is not None and nid not in on_nodes, False)[0]
        for item in self.cv.find_withtag(tag):
            if "card" in self.cv.gettags(item):
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
