"""Grafischer Profil-Editor (Tk).

Aufbau wie in der Elgato-App: links das Gerät mit Tasten, Dials und Seiten,
rechts die Aktionsliste, darunter die Eigenschaften der gewählten Taste.
Aktionen werden per Drag & Drop (oder Doppelklick) auf Tasten gelegt,
Tasten per Drag & Drop verschoben. Jede Änderung wird sofort gespeichert;
der laufende FreeDeck-Dienst zeigt sie wenige Sekunden später auf dem Gerät.

Start: freedeck edit [Profil]
"""
import json
import logging
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from tkinter import font as tkfont

from PIL import Image, ImageDraw, ImageTk

from . import actions as A
from . import catalog as C
from . import editdoc as E
from . import msgbox as colorchooser
from . import msgbox as filedialog
from . import msgbox as messagebox
from . import msgbox as simpledialog
from . import livekeys, paths, render, theme
from .i18n import _, ngettext
from .models import MODELS, connected_models, model_geometry, model_name
from .profiles import ProfileStore, delete_profile, import_path

DEVICE_WATCH_MS = 300  # so oft schaut das Fenster nach, was am Deck passiert
log = logging.getLogger("freestream.editor")

COL = theme.COL
# Kategorie der Aktionsliste -> (Symbol, Farbe)
CATEGORY_LOOK = {
    _("System"): ("keyboard", "#64748b"), _("Navigation"): ("navigate", "#3b82f6"), "FreeDeck": ("grid", "#3b82f6"),
    "Multi Action": ("layers", "#8b5cf6"), "Soundboard": ("sfx", "#ec4899"), _("Audio"): ("speaker", "#22c55e"),
    "OBS Studio": ("record", "#ef4444"), _("Spotify & Media"): ("play", "#16a34a"), "Discord": ("chat", "#6366f1"),
    "FreeWave": ("wave", "#06b6d4"), "Home Assistant": ("home", "#0ea5e9"),
    C.CLOCK_CAT: ("clock", "#f97316"), C.SYSMON_CAT: ("gauge", "#14b8a6"),
}
# eigenes Symbol für einzelne Aktionen (sonst das der Kategorie)
ACTION_GLYPH = {
    "system.hotkey": "keyboard", "system.hotkeyswitch": "keyboard", "system.text": "text", "system.open": "folder",
    "system.openapp": "play", "system.close": "close", "system.website": "globe", "system.multimedia": "play",
    "system.timer": "auto", "system.mouse-wheel": "navigate", "profile.openchild": "folder",
    "profile.backtoparent": "up", "page.next": "arrow", "page.previous": "arrow_left", "page.goto": "goto",
    "page.indicator": "grid", "profile.rotate": "deck", "system.keybrightness": "sun", "system.sleep": "moon",
    "system.digitaltime": "auto", "soundboard.playaudio": "sfx", "soundboard.stopaudioplay": "close",
    "volume-controller.output-device-control": "speaker", "volume-controller.input-device-control": "mic",
    "obs.scene": "camera", "obs.record": "record", "obs.stream": "stream", "obs.source": "image",
    "obs.audio": "mic", "obs.replay": "undo", "obs.virtualcam": "camera", "obs.studiomode": "grid",
    "obs.transition": "arrow", "obs.screenshot": "image", "media.player": "music", "discord.voice": "chat",
    "mixer.channel": "fx", "mixer.output": "headphones", "mixer.mixselect": "wave", "mixer.pages": "grid",
    "wavemic.control": "mic",
    "clock.clock": "clock", "clock.countdown": "hourglass", "clock.stopwatch": "stopwatch", "sysmon.cpu": "cpu",
    "sysmon.memory": "memory", "sysmon.temperature": "thermometer", "sysmon.network": "network",
    "sysmon.battery": "battery", "sysmon.disk": "disk",
    "homeassistant.toggle": "home", "homeassistant.service": "gear", "homeassistant.scene": "spark",
    "homeassistant.state": "gauge",
}
theme.EXTRA_GLYPHS.update(livekeys.GLYPHS)  # selbst gezeichnete Symbole der Live-Tasten


def category_look(cat):
    if cat.startswith("Plugin:"):
        return ("puzzle", "#f59e0b")
    return CATEGORY_LOOK.get(cat, ("puzzle", "#f59e0b"))


def action_look(uuid, cat):
    """Symbol und Farbe einer Aktion (Kachel in der Bibliothek, Kopf der Eigenschaften)."""
    glyph, color = category_look(cat)
    for prefix in ("com.elgato.streamdeck.", "com.elgato.", "com.lindeck."):
        if uuid.startswith(prefix):
            return ACTION_GLYPH.get(uuid[len(prefix):], glyph), color
    return glyph, color


def profile_glyph(name):
    """Passendes Symbol für ein Profil nach seinem Namen."""
    n = name.lower()
    for words, glyph in ((("stream", "live", "twitch"), "stream"), (("gam", "spiel"), "game"),
                         (("video", "edit", "schnitt", "film"), "camera"), (("meet", "call", "chat", "discord", "zoom",
                                                                            "teams"), "chat"),
                         (("music", "musik", "spotify", "audio"), "music"), (("code", "dev", "work", "arbeit"),
                                                                            "keyboard")):
        if any(w in n for w in words):
            return glyph
    return "grid"


ALIGN = [("top", _("top")), ("middle", _("middle")), ("bottom", _("bottom"))]
STYLES = [("", _("Regular")), ("Bold", _("Bold")), ("Italic", _("Italic")), ("Bold Italic", _("Bold italic"))]
TEXT_WIDGETS = ("Entry", "TEntry", "Text", "Spinbox", "TSpinbox", "TCombobox")
FOOTER_H = 34  # Fußzeile der Bühne (Hinweis, Helligkeit)


PEDAL, STUDIO = "20GBF9901", "20GBO9901"


def _stage_kind(model, g):
    """Wie das Gerät gezeichnet wird: plain, plus (Touchstreifen), neo, studio, pedal."""
    if model == PEDAL:
        return "pedal"
    if model == STUDIO:
        return "studio"
    if g.touch_keys:
        return "neo"
    return "plus" if g.dials else "plain"


def _layout(g, width, height, pad=18, kind="plain"):
    """Größte Kachelgröße, bei der das Gerät samt Touchstreifen/Reglern in den Bereich passt."""
    for ts in range(96, 19, -2):
        gap = max(6, ts // 7)
        L = {"ts": ts, "gap": gap, "kind": kind, "side": 0, "extra": 0}
        if kind == "pedal":
            L["pw"], L["ph"] = int(ts * 1.5), int(ts * 1.9)
            grid_w, grid_h = 3 * L["pw"] + 2 * gap * 2, L["ph"]
        else:
            grid_w, grid_h = g.cols * ts + (g.cols - 1) * gap, g.rows * ts + (g.rows - 1) * gap
        if kind == "plus":
            L["card_h"] = max(40, min(50, int(ts * 0.56)))
            L["ring"] = max(14, min(24, int(ts * 0.27)))
            L["seg_w"] = (grid_w - (g.dials - 1) * 6) / g.dials
            L["extra"] = 16 + L["card_h"] + 12 + 2 * L["ring"] + 4
        elif kind == "neo":
            L["bar_h"] = max(28, int(ts * 0.5))
            L["extra"] = gap + L["bar_h"]
        elif kind == "studio":
            L["ring"] = max(16, int(ts * 0.62))
            L["side"] = 2 * L["ring"] + 3 * gap
        L["grid_w"], L["grid_h"] = grid_w, grid_h
        L["w"], L["h"] = grid_w + 2 * L["side"], grid_h + L["extra"]
        if L["w"] + 2 * pad <= width and L["h"] + 2 * pad <= height:
            break
    return L


def _bg(w):
    try:
        return w.cget("bg")
    except tk.TclError:
        try:
            return ttk.Style(w).lookup(w.cget("style") or w.winfo_class(), "background") or COL["bg"]
        except tk.TclError:
            return COL["bg"]


class ScrollFrame(tk.Frame):
    """Senkrecht scrollbarer Bereich; die Leiste erscheint nur, wenn der Inhalt nicht passt."""

    def __init__(self, master, bg=None, padding=0, style=None):
        bg = bg or COL["panel"]
        super().__init__(master, bg=bg, padx=padding, pady=padding)
        self.canvas = tk.Canvas(self, highlightthickness=0, bg=bg, bd=0)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        # Mausrad: ein Handler für alle Bereiche, nur einmal je Tk gebunden – ein eigener je Bereich bliebe
        # nach dem Schließen des Dialogs für immer an „all“ hängen (und mit ihm der ganze Dialog)
        root = self._root()
        if not root.__dict__.get("_scrollframe_wheel"):
            root._scrollframe_wheel = True
            for seq in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
                root.bind_all(seq, _scrollframe_wheel, add="+")

    def _on_scroll(self, lo, hi):
        if float(lo) <= 0 and float(hi) >= 1:
            if self.bar.winfo_manager():
                self.bar.pack_forget()
        elif not self.bar.winfo_manager():
            self.bar.pack(side="right", fill="y", before=self.canvas)
        self.bar.set(lo, hi)

    def _wheel(self, e):
        """Ein Rad-Schritt; False, wenn der Inhalt ganz hineinpasst (dann darf ein äußerer Bereich scrollen)."""
        try:
            if not self.bar.winfo_manager():
                return False
            step = -1 if (e.num == 4 or getattr(e, "delta", 0) > 0) else 1
            self.canvas.yview_scroll(step * 2, "units")
            return True
        except tk.TclError:
            return False

    def clear(self, keep_scroll=False):
        for c in self.inner.winfo_children():
            c.destroy()
        if not keep_scroll:
            self.canvas.yview_moveto(0)


def _scrollframe_wheel(e):
    """Mausrad über einem ScrollFrame: den innersten Bereich unter dem Zeiger scrollen, der eine Leiste hat."""
    w = e.widget  # Tk-interne Fenster (z. B. Liste einer Combobox) kommen als Text an
    while w is not None and not isinstance(w, str):
        sf = getattr(w, "master", None)
        if isinstance(sf, ScrollFrame) and w is sf.canvas and sf._wheel(e):
            return
        w = sf


class RoundBox(tk.Canvas):
    """Fläche mit runden Ecken und feinem Rand, die ihren Platz ganz ausfüllt; Inhalt in .body."""

    def __init__(self, master, color=None, outline=None, radius=16, pad=16, padx=None, **kw):
        super().__init__(master, bg=_bg(master), highlightthickness=0, bd=0, **kw)
        self.color = color or COL["panel"]
        self.outline = COL["border"] if outline is None else outline
        self.radius, self.pad, self.padx = radius, pad, pad if padx is None else padx
        self.body = tk.Frame(self, bg=self.color)
        self._win = self.create_window(self.padx, self.pad, window=self.body, anchor="nw")
        self.bind("<Configure>", self._draw)

    def _draw(self, _e=None):
        self.delete("bg")
        w, h = self.winfo_width(), self.winfo_height()
        theme.rounded_rect(self, 0, 0, w - 1, h - 1, self.radius, fill=self.color, outline=self.outline, tags="bg")
        self.tag_lower("bg")
        self.itemconfigure(self._win, width=max(1, w - 2 * self.padx), height=max(1, h - 2 * self.pad))


class CategoryRow(tk.Canvas):
    """Kopf einer Kategorie in der Aktionsliste: Pfeil, Name, Anzahl; Klick klappt auf/zu."""

    H = 34

    def __init__(self, master, app, cat, is_open, count):
        super().__init__(master, bg=_bg(master), highlightthickness=0, bd=0, height=self.H, cursor="hand2")
        self.app, self.cat, self.open, self.count = app, cat, is_open, count
        self.hover = False
        self.fb = theme.font(app.root, 10, "bold")
        self.fs = theme.font(app.root, 9)
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", lambda e: app.toggle_category(cat))
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))

    def _set_hover(self, v):
        self.hover = v
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = max(self.winfo_width(), 60), self.H
        if self.hover:
            theme.rounded_rect(self, 0, 1, w - 1, h - 1, 6, fill=COL["nav_on"], outline="")
        self.create_image(14, h / 2, image=self.app.icons.get("chevron" if self.open else "chevron_right", 13,
                                                               COL["muted"]))
        name = self.cat.split(":", 1)[-1].strip() if self.cat.startswith("Plugin:") else self.cat
        self.create_text(28, h / 2, text=_clip(name, _measure_font(self, self.fb), w - 70), anchor="w",
                         fill=COL["fg"], font=self.fb)
        self.create_text(w - 10, h / 2, text=str(self.count), anchor="e", fill=COL["faint"], font=self.fs)


class ActionRow(tk.Canvas):
    """Aktion in der Liste: kleines Symbol und Name; ziehen auf eine Taste oder doppelklicken."""

    H = 38

    def __init__(self, master, app, atype):
        super().__init__(master, bg=_bg(master), highlightthickness=0, bd=0, height=self.H, cursor="hand2")
        self.app, self.type = app, atype
        self.selected = self.hover = False
        glyph, color = action_look(atype.uuid, atype.category)
        self.img = theme.icon_tile(app.root, glyph, 26, color, radius=0.22)
        self.f = theme.font(app.root, 10)
        self.fs = theme.font(app.root, 9)
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonPress-1>", lambda e: app._lib_press(e, atype.uuid))
        self.bind("<B1-Motion>", app._lib_motion)
        self.bind("<ButtonRelease-1>", app._lib_release)
        self.bind("<Double-Button-1>", lambda e: app._lib_double(atype.uuid))

    def _set_hover(self, v):
        self.hover = v
        self._draw()

    def set_selected(self, v):
        self.selected = v
        self._draw()

    def _draw(self, force=True):
        self.delete("all")
        w, h = max(self.winfo_width(), 60), self.H
        if self.selected or self.hover:
            theme.rounded_rect(self, 0, 1, w - 1, h - 1, 6, outline="",
                               fill=COL["accent_soft"] if self.selected else COL["nav_on"])
        self.create_image(24, h / 2, image=self.img, anchor="w")
        x = 60
        room = w - x - 24
        self.create_text(x, h / 2, text=_clip(self.type.name, _measure_font(self, self.f), room), anchor="w",
                         fill=COL["fg"], font=self.f)
        if C.DIAL in self.type.controllers and C.KEY not in self.type.controllers:
            self.create_text(w - 10, h / 2, text="◎", anchor="e", fill=COL["faint"], font=self.fs)


_CLIPPED = {}     # (Text, Schrift, Breite, Zeilen) -> gekürzter Text


def _measure_font(widget, fnt):
    """Schrift zum Messen, je Fenster zwischengespeichert (anlegen ist teuer). Der Speicher hängt am
    Hauptfenster selbst: Tk-Objekte dürfen nicht länger leben als ihr Fenster (und nie in einem anderen
    Thread freigegeben werden)."""
    root = widget._root()
    cache = root.__dict__.setdefault("_measure_fonts", {})
    f = cache.get(str(fnt))
    if f is None:
        f = cache[str(fnt)] = tkfont.Font(widget, font=fnt)
    return f


_SIMPLE = frozenset("…–—„“”‘’‚·×°€")  # Satzzeichen, die sich wie lateinische Buchstaben verhalten


def _width(f, text):
    """Breite von text in Pixeln. Jedes measure() ist ein Tk-Aufruf (unter X rund 0,5 ms) – deshalb
    zählen bei lateinischer Schrift die einzeln gemessenen Zeichen zusammen (Tk unterschneidet nicht,
    das Ergebnis ist gleich); andere Schriften und Emoji werden als Ganzes gemessen."""
    cache = f.__dict__.setdefault("_widths", {})
    w = cache.get(text)
    if w is not None:
        return w
    if len(cache) > 20000:
        cache.clear()
    if len(text) > 1 and all(ord(ch) < 0x300 or ch in _SIMPLE for ch in text):
        w = sum(_width(f, ch) for ch in text)
    else:
        w = f.measure(text)
    cache[text] = w
    return w


def _cut(text, f, width):
    """Längster Anfang von text, der mit „…“ in width passt (binäre Suche)."""
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _width(f, text[:mid].rstrip() + "…") <= width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "…"


def _clip(text, f, width):
    """Eine Zeile auf `width` Pixel kürzen (mit …); f: tkfont.Font."""
    text = " ".join(str(text or "").split())
    if _width(f, text) <= width:
        return text
    return _cut(text, f, width)


def _clip_lines(widget, text, fnt, width, lines):
    """Text auf höchstens `lines` Zeilen der Breite `width` kürzen (mit …)."""
    key = (text, str(fnt), width, lines)
    hit = _CLIPPED.get(key)
    if hit is not None:
        return hit
    f = _measure_font(widget, fnt)
    space = _width(f, " ")
    words, out, cur, cur_w = text.split(), [], "", 0
    for wd in words:
        ww = _width(f, wd)  # Zeilenbreite aus Wortbreiten: ein Tk-Aufruf je Wort statt je Zwischenstand
        if not cur or cur_w + space + ww <= width:
            cur, cur_w = (cur + " " + wd if cur else wd), (cur_w + space + ww if cur else ww)
        else:
            out.append(cur)
            cur, cur_w = wd, ww
            if len(out) > lines:
                break  # mehr Zeilen braucht niemand
    if cur:
        out.append(cur)
    cut = len(out) > lines
    out = out[:lines]
    for i, line in enumerate(out):
        if _width(f, line) > width or (cut and i == len(out) - 1):
            out[i] = _cut(line, f, width)
    if len(_CLIPPED) > 4000:
        _CLIPPED.clear()
    _CLIPPED[key] = result = "\n".join(out)
    return result


def _autowrap(label):
    """Zeilenumbruch eines Labels an seine Breite anpassen."""
    label.bind("<Configure>", lambda e: label.configure(wraplength=max(100, e.width - 4)), add="+")


class PageDots(tk.Canvas):
    """Seitenwahl unter den Tasten: ‹ Punkte › – Klick wählt, Rechtsklick öffnet das Seitenmenü."""

    GAP, H = 18, 28

    def __init__(self, master, app, ids, current):
        self.app, self.ids, self.current = app, list(ids), current
        n = min(len(self.ids), 12)
        super().__init__(master, width=72 + max(0, n - 1) * self.GAP, height=self.H, bg=_bg(master),
                         highlightthickness=0, bd=0, cursor="hand2")
        self.f = theme.font(app.root, 12, "bold")
        self.bind("<Button-1>", self._click)
        self.bind("<Button-3>", self._menu)
        self._draw()

    def _x(self, i):
        return 36 + i * self.GAP

    def _draw(self):
        self.delete("all")
        h = self.H
        self.create_text(12, h / 2 - 1, text="‹", fill=COL["muted"], font=self.f)
        cur = self.ids.index(self.current) if self.current in self.ids else -1
        for i in range(min(len(self.ids), 12)):
            x, r = self._x(i), 4 if i == cur else 3.5
            self.create_oval(x - r, h / 2 - r, x + r, h / 2 + r, outline="",
                             fill=COL["fg"] if i == cur else COL["faint"])
        self.create_text(int(self["width"]) - 12, h / 2 - 1, text="›", fill=COL["muted"], font=self.f)

    def _index_at(self, x):
        n = min(len(self.ids), 12)
        if x < 24:
            return "prev"
        if x > int(self["width"]) - 24:
            return "next"
        i = round((x - 36) / self.GAP)
        return i if 0 <= i < n else None

    def _click(self, e):
        hit = self._index_at(e.x)
        cur = self.ids.index(self.current) if self.current in self.ids else 0
        if hit == "prev":
            hit = cur - 1
        elif hit == "next":
            hit = cur + 1
        if isinstance(hit, int) and 0 <= hit < len(self.ids) and self.ids[hit] != self.current:
            self.app.goto_page(self.ids[hit])

    def _menu(self, e):
        hit = self._index_at(e.x)
        if isinstance(hit, int):
            self.app._page_menu(e, self.ids[hit])


class DropButton(tk.Canvas):
    """Auswahlfeld der Kopfzeile (Gerät, Profil): Symbol, Text, ⌄ – Klick klappt die Liste darunter auf."""

    def __init__(self, master, icon, text, width=220, command=None, height=36):
        super().__init__(master, width=width, height=height, bg=_bg(master), highlightthickness=0, bd=0,
                         cursor="hand2")
        self.icon, self.text, self.command, self.w, self.h = icon, text, command, width, height
        self.hover = False
        self.icons = theme.Icons(self)
        self.f = theme.font(self, 10)
        self.bind("<Button-1>", lambda e: self.command and self.command())
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self._draw()

    def _set_hover(self, v):
        self.hover = v
        self._draw()

    def set(self, text, icon=None):
        if (text, icon or self.icon) != (self.text, self.icon):
            self.text, self.icon = text, icon or self.icon
            self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.w, self.h
        theme.rounded_rect(self, 0, 0, w - 1, h - 1, 6, fill=COL["raised"] if self.hover else COL["panel2"],
                           outline=COL["border"])
        x = 12
        if self.icon:
            self.create_image(x + 8, h / 2, image=self.icons.get(self.icon, 16, COL["fg"]))
            x += 26
        self.create_text(x, h / 2, text=_clip(self.text, _measure_font(self, self.f), w - x - 30), anchor="w",
                         fill=COL["fg"], font=self.f)
        self.create_image(w - 16, h / 2, image=self.icons.get("chevron", 14, COL["muted"]))


class DropPanel(tk.Toplevel):
    """Aufgeklappte Liste unter einem DropButton; schließt bei Klick daneben, Esc oder wenn ein anderes
    Programm den Fokus bekommt (Alt+Tab)."""

    def __init__(self, root, width=280):
        super().__init__(root, bg=COL["border"])
        self.withdraw()
        self.overrideredirect(True)
        self.width = width
        self.anchor = None
        self.body = tk.Frame(self, bg=COL["side"], padx=6, pady=6)
        self.body.pack(fill="both", expand=True, padx=1, pady=1)
        self.bind("<Escape>", lambda e: self.hide())
        self.bind("<FocusOut>", lambda e: self.after(100, self._check_focus), add="+")
        # Klick daneben: ein Handler je Tk an „all“ statt eines Grabs. Der Grab hielte das FocusOut des
        # Hauptfensters zurück (die Liste selbst bekommt als Fenster ohne Rahmen nie den X-Fokus), und nach
        # Alt+Tab stünde sie über dem anderen Programm
        top = self._root()
        if "_drop_panels" not in top.__dict__:
            top._drop_panels = []
            top.bind_all("<ButtonPress>", lambda e: _drop_press(top, e), add="+")
        top._drop_panels.append(self)

    def show(self, anchor):
        self.anchor = anchor
        self.update_idletasks()
        w = max(self.width, anchor.winfo_width())
        h = self.body.winfo_reqheight() + 2
        x, y = anchor.winfo_rootx(), anchor.winfo_rooty() + anchor.winfo_height() + 4
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.deiconify()
        self.lift()
        try:
            self.attributes("-topmost", True)
            self.focus_force()  # für Esc
        except tk.TclError:
            pass

    def hide(self):
        try:
            # Fokus zurück ans Hauptfenster (sonst gehen Tastenkürzel ins Leere, bis man hineinklickt);
            # roher Tcl-Aufruf: focus_get() scheitert an Tk-internen Fenstern
            focus = str(self.tk.call("focus"))
            had_focus = focus == str(self) or focus.startswith(str(self) + ".")
            if self.winfo_ismapped():
                self.withdraw()
            if had_focus:
                self.master.focus_set()
        except tk.TclError:
            pass

    def _check_focus(self):
        """Fokus ging an ein anderes Programm (Alt+Tab): Liste schließen, sonst schwebt sie darüber."""
        try:
            if self.winfo_ismapped() and not self.tk.call("focus"):
                self.hide()
        except tk.TclError:
            pass

    def _inside(self, x_root, y_root):
        x, y = x_root - self.winfo_rootx(), y_root - self.winfo_rooty()
        return 0 <= x < self.winfo_width() and 0 <= y < self.winfo_height()


def _drop_press(top, e):
    """Mausklick irgendwo im Programm: offene Listen schließen, die er nicht trifft. Ein Klick auf ihr
    Auswahlfeld bleibt außen vor – das klappt sie selbst auf und zu (Widget-Bindungen laufen vor „all“)."""
    for p in top._drop_panels:
        try:
            if p.winfo_ismapped() and e.widget is not p.anchor and not p._inside(e.x_root, e.y_root):
                p.hide()
        except tk.TclError:
            pass


class EditorApp:
    def __init__(self, root: tk.Tk, store_root=None, profile=None, daemon=None, on_quit=None):
        self.root = root
        theme.gc_in_main_thread(root)  # Tk-Objekte nie in Geräte-/Arbeits-Threads aufräumen (sonst hängen sie)
        self.daemon = daemon        # laufender Dienst im selben Programm (App-Modus)
        self.on_quit = on_quit
        self.device = None          # Seriennummer des gewählten Stream Decks
        self._device_ids = []
        self._device_key = None
        self.device_cards = {}      # Seriennummer -> InfoCard
        self._hidden_once = False
        self.store_root = Path(store_root or paths.PROFILES_DIR)
        self.store_root.mkdir(parents=True, exist_ok=True)
        self.store = ProfileStore(self.store_root)
        self.doc = None
        self.nav = []          # [Seite, Ordner, Unterordner …]
        self.selected = None   # Slot
        self.clipboard = None
        self._photos = []
        self._img_cache = {}
        self._tk_cache = {}    # id(PIL-Bild) -> (Bild, PhotoImage)
        self._live_keys = {}   # Slot -> Schlüssel seines Live-Bildes in _img_cache
        self._tiles, self._tiles_id = {}, None  # Hintergrund-Kacheln der gezeichneten Seite
        self._hit = []
        self._drag = None
        self._save_job = None
        self._followed_at = 0.0
        self._shown_seq = None
        self._profile_ids = []
        self._types = dict(C.BY_UUID)
        self._insp_tab = "appearance"
        self._lib_selected = None
        self._tiles_by_uuid = {}

        try:  # Einstellungen › Stil: Schrift für neue Tasten
            from .freedecksettings import config_of
            C.set_title_defaults(config_of(self).get("title_style") or {})
        except Exception:  # noqa: BLE001
            pass

        root.title("FreeDeck")
        root.geometry("1280x800")
        root.minsize(1100, 720)
        root.configure(bg=COL["bg"])
        self._style()
        self._build()
        self._bind_keys()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<FocusIn>", lambda e: self._check_disk() if e.widget is root else None, add="+")
        root.bind("<Unmap>", lambda e: self._hide_drops() if e.widget is root else None, add="+")  # minimiert

        self.refresh_profiles()
        target = self.store.get(profile) if profile else None
        if target is None and self.store.profiles:
            target = sorted(self.store.profiles.values(), key=lambda p: p.name.lower())[0]
        if target:
            self.open_profile(target.id)
        else:
            self.show_empty()
            from . import decksetup
            if decksetup.needed(self):
                root.after(300, self.open_setup)
        if self.daemon is not None:
            self.refresh_devices(initial=True)
            self._dev_state = self._device_state()
            self.root.after(DEVICE_WATCH_MS, self._watch_device)

    def _device_state(self):
        ctrl = self._ctrl()
        if ctrl is None:
            return None
        return (ctrl.serial, ctrl.profile.id if ctrl.profile else None, ctrl.page_index,
                tuple(pg.uuid for pg in list(getattr(ctrl, "folder_stack", ()))), getattr(ctrl, "brightness", None))

    def _watch_device(self):
        """Was am Deck passiert (Seite, Ordner, Profil, Helligkeit), auch im Fenster zeigen."""
        try:
            if self.root.winfo_viewable() and not self._save_job:
                st = self._device_state()
                old, self._dev_state = self._dev_state, st
                ours = time.monotonic() - self._followed_at < 1.5  # Wechsel kam vom Fenster selbst
                if st is not None and old is not None and st[0] == old[0] and st != old and not ours:
                    self._follow_device(old, st)
                ctrl = self._device_page()
                seq = getattr(ctrl, "shown_seq", None)
                if seq != self._shown_seq:
                    self._shown_seq = seq
                    if ctrl is not None and not self._drag:
                        self.redraw()  # Live-Tasten (Lautstärke, Stumm …) wie am Deck
        except Exception as e:  # noqa: BLE001 - die Überwachung darf nie stehen bleiben
            log.debug("watch device: %s", e)
        try:
            self.root.after(DEVICE_WATCH_MS, self._watch_device)
        except tk.TclError:
            pass

    def _follow_device(self, old, st):
        __, pid, page, folders, bright = st
        if (pid, page, folders) == old[1:4]:
            if bright != old[4]:  # nur Helligkeit: Fußzeile neu
                self.redraw()
            return
        if not pid:
            return
        if not self.doc or self.doc.id != pid:
            self.open_profile(pid, from_device=True)
        if not self.doc or self.doc.id != pid:
            return
        ids = self.doc.page_ids
        if not 0 <= page < len(ids):
            return
        nav = [ids[page]] + [u for u in folders if self.doc.page(u)]
        if nav != self.nav:
            self.nav = nav
            self.selected = None
            self.refresh_all()

    def open_setup(self):
        """Einrichtung (erster Start): Gerät erkennen, Profile übernehmen oder neu anlegen."""
        from .decksetup import DeckSetup

        def done(ids):
            self.refresh_profiles()
            # ein neu übernommenes Profil öffnen; sonst bleibt das offene offen (samt Rückgängig-Verlauf)
            if dlg.new_ids:
                self.open_profile(dlg.new_ids[0])
            elif ids and not self.doc:
                self.open_profile(ids[0])
        dlg = DeckSetup(self, on_done=done)
        return dlg

    # ================================================================ Geräte
    def _controllers(self):
        return list(getattr(self.daemon, "controllers", {}).values()) if self.daemon is not None else []

    def _ctrl(self):
        return next((c for c in self._controllers() if c.serial == self.device), None)

    def refresh_devices(self, initial=False):
        """Liste der angeschlossenen Stream Decks aktualisieren (alle 2 s)."""
        if self.daemon is None:
            return
        ctrls = sorted(self._controllers(), key=lambda c: (model_name(c.model), c.serial))
        ids = [c.serial for c in ctrls]
        if ids != self._device_ids:
            self._device_ids = ids
            self.root.after_idle(self.refresh_profiles)  # Profile anderer Modelle aus-/eingrauen
            if self.device not in ids:
                self.device = ids[0] if ids else None
                if self.device and not initial:
                    self._show_device_profile()
            if initial and self.device:
                self._show_device_profile()
        self.build_devices()
        try:
            self.root.after_cancel(self._dev_job)
        except (AttributeError, tk.TclError):
            pass
        self._dev_job = self.root.after(2000, self.refresh_devices)

    def build_devices(self):
        """Gerätekarten in der Seitenleiste (nur neu aufbauen, wenn sich etwas geändert hat)."""
        if self.daemon is not None:
            # nur angeschlossene Geräte – das Modell erkennt FreeDeck selbst
            ctrls = sorted(self._controllers(), key=lambda c: (model_name(c.model), c.serial))
            key = ("app", tuple(c.serial for c in ctrls), self.device)
        else:
            ctrls = []
            key = ("edit", self.doc.model if self.doc else None)
        if key == self._device_key:
            return
        self._device_key = key
        for c in self.devbox.winfo_children():
            c.destroy()
        self.device_cards = {}
        on_img = theme.icon_tile(self.root, "deck", 30, COL["device"], radius=0.2, fg="#c9cfda")
        off_img = theme.icon_tile(self.root, "deck", 30, theme.mix(COL["device"], COL["muted"], 0.45),
                                  radius=0.2, fg="#c9cfda")
        if self.daemon is None:
            if self.doc:
                card = theme.InfoCard(self.devbox, model_name(self.doc.model) if self.doc.model else _("unknown"),
                                      _("Layout of this profile"), image=on_img)
                card.pack(fill="x", pady=(0, 2))
                self.device_cards[""] = card
            self.dev_dd.set(model_name(self.doc.model) if self.doc and self.doc.model else "FreeDeck")
            return
        from .freedecksettings import config_of, device_title
        cfg = config_of(self)
        for c in ctrls:
            card = theme.InfoCard(self.devbox, device_title(cfg, c), _("Virtual") if getattr(c, "virtual", False)
                                  else _("Connected"), image=on_img, dot=COL["ok"],
                                  command=lambda s=c.serial: self.select_device(s))
            card.selected = c.serial == self.device
            card.pack(fill="x", pady=(0, 2))
            self.device_cards[c.serial] = card
        if not ctrls:
            theme.InfoCard(self.devbox, _("No device connected"), "", image=off_img, dim=True).pack(
                fill="x", pady=(0, 8))
        cur = next((c for c in ctrls if c.serial == self.device), None)
        self.dev_dd.set(device_title(cfg, cur) if cur else _("No device connected"))

    def select_device(self, serial):
        """Gerät in der Liste gewählt: dessen laufendes Profil öffnen."""
        self._hide_drops()
        if serial in self._device_ids:
            self.device = serial
            self._device_key = None
            self.build_devices()
            self._show_device_profile()
            self._update_header()

    def _show_device_profile(self):
        """Das Profil öffnen, das gerade auf dem gewählten Gerät läuft."""
        ctrl = self._ctrl()
        if ctrl is None or not ctrl.profile:
            return
        if not self.doc or self.doc.id != ctrl.profile.id:
            self.open_profile(ctrl.profile.id, from_device=True)
        ids = self.doc.page_ids if self.doc else []
        if 0 <= ctrl.page_index < len(ids) and self.nav and self.nav[0] != ids[ctrl.page_index]:
            self.nav = [ids[ctrl.page_index]]
            self.refresh_all()

    def _device_follow(self, page_index=None):
        """Gewähltes Gerät auf das bearbeitete Profil bzw. die Seite umschalten (wie in der Elgato-App)."""
        ctrl = self._ctrl()
        if ctrl is None or not self.doc:
            return
        g = self.doc.geometry
        if (g.cols, g.rows) != (ctrl.geometry.cols, ctrl.geometry.rows):
            return  # Profil passt nicht zu diesem Gerät
        pid = self.doc.id
        self._followed_at = time.monotonic()

        def work():
            try:
                if not ctrl.profile or ctrl.profile.id != pid:
                    if not self.daemon.store.get(pid):
                        self.daemon.store.reload()
                    ctrl.load_profile(pid, page_index or 0)
                elif page_index is not None and page_index != ctrl.page_index:
                    ctrl.goto_page(page_index)
            except Exception as e:  # noqa: BLE001
                log.debug(_("Switching device failed: %s"), e)
        import threading
        threading.Thread(target=work, daemon=True).start()

    def _check_disk(self, _e=None):
        """Fenster bekommt den Fokus: Profil neu laden, wenn es außerhalb geändert wurde
        (z. B. Kanäle aus FreeWave auf das Deck gelegt) und hier nichts ungespeichert ist."""
        doc = self.doc
        if doc is None or self._save_job or doc.dirty or not doc.disk_changed():
            return
        page = self.page_uuid
        self.open_profile(doc.id, from_device=True)
        if self.doc and page in self.doc.page_ids:
            self.nav = [page]
            self.refresh_all()

    def profiles_changed(self):
        """Push von außen (FreeWave-Export, „freedeck reload“): neue Profile sofort in der Liste zeigen."""
        self._check_disk()
        self.refresh_profiles()
        if self.doc is None and self._profile_ids:  # bisher leer: neues Profil gleich öffnen
            self.open_profile(self._profile_ids[0])

    # ======================================================= Fenster
    def show(self):
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass
        self.refresh_profiles()

    def hide(self):
        self.save_now()
        self._hide_drops()  # offene Liste nicht über dem Desktop stehen lassen
        self.root.withdraw()
        if not self._hidden_once:
            self._hidden_once = True
            from .desktop import notify
            notify(_("FreeDeck keeps running"), _("FreeDeck stays active. Open it again via its icon in the tray."))

    def open_background(self):
        """Hintergrundbild der aktuellen Seite (und des Touchstreifens) einstellen."""
        if not self.doc or not self.nav:
            return
        from .bgui import BackgroundDialog
        BackgroundDialog(self.root, self, self.nav[-1])

    def open_plugins(self):
        """Plugin-Verwaltung: Einstellungen › Plugins."""
        from .settingsui import open_settings
        return open_settings(self.root, "FreeDeck", self._restart, freedeck=self, tab="plugins", setup=self.open_setup)

    def _plugins_changed(self):
        self.fill_catalog()
        self._update_plugin_count()

    def _update_plugin_count(self):
        try:
            from . import plugins
            n = len(plugins.installed())
        except Exception:  # noqa: BLE001
            n = ""
        self.plugins_btn.configure_text(text=_("Plugins") + (f" · {n}" if n else ""))

    def _restart(self):
        theme.restart_later("freestream")
        self.quit_app()

    def quit_app(self):
        self.close_doc()
        if self.on_quit:
            self.on_quit()
        else:
            self.root.destroy()

    # ================================================================ Aufbau
    def _style(self):
        theme.apply(self.root)
        self.icons = theme.Icons(self.root)
        self._fam = theme.font(self.root)[0]
        self._small = tkfont.Font(self.root, font=theme.font(self.root, 8))
        self._small_bold = tkfont.Font(self.root, font=theme.font(self.root, 8, "bold"))

    def _build(self):
        """Aufbau wie in der Deck-Software von Elgato: oben Gerät und Profil, Mitte die Tasten, darunter die
        Eigenschaften der gewählten Taste, rechts die Aktionen."""
        r = self.root
        self.status_var = tk.StringVar(value="")
        self._model_codes = list(MODELS)
        self.btn = {}
        self._build_topbar(r)
        tk.Frame(r, bg=COL["border"], height=1).pack(side="top", fill="x")
        body = tk.Frame(r, bg=COL["bg"])
        body.pack(side="top", fill="both", expand=True)
        lib = tk.Frame(body, bg=COL["side"], width=310)
        lib.pack(side="right", fill="y")
        lib.pack_propagate(False)
        tk.Frame(body, bg=COL["border"], width=1).pack(side="right", fill="y")
        self._build_library(lib)
        main = tk.Frame(body, bg=COL["canvas"])
        main.pack(side="left", fill="both", expand=True)
        self._build_center(main)

    # ------------------------------------------------------------- Kopfzeile
    def _build_topbar(self, r):
        top = tk.Frame(r, bg=COL["side"], height=58, padx=14)
        top.pack(side="top", fill="x")
        top.pack_propagate(False)
        self._mark = theme.gradient_mark(r, 26, "deck")
        tk.Label(top, image=self._mark, bg=COL["side"]).pack(side="left", padx=(0, 12))
        # Gerät und Profil als Auswahlfelder; ihre Listen klappen darunter auf
        self.dev_dd = DropButton(top, "deck", "FreeDeck", width=230, command=lambda: self._toggle_drop("dev"))
        self.dev_dd.pack(side="left", padx=(0, 8))
        self.prof_dd = DropButton(top, None, _("No profile"), width=230, command=lambda: self._toggle_drop("prof"))
        self.prof_dd.pack(side="left")
        self.dev_panel = DropPanel(r, 280)
        self.devbox = tk.Frame(self.dev_panel.body, bg=COL["side"])
        self.devbox.pack(fill="x")
        self.prof_panel = DropPanel(r, 300)
        tk.Label(self.prof_panel.body, text=_("Profiles"), bg=COL["side"], fg=COL["muted"],
                 font=theme.font(r, 9, "bold"), anchor="w").pack(fill="x", padx=8, pady=(4, 6))
        self.profile_list = ScrollFrame(self.prof_panel.body, bg=COL["side"])
        self.profile_list.pack(fill="both", expand=True)
        # rechts: Einstellungen, Plugins, Export/Import, Rückgängig
        self.settings_btn = theme.Button(top, icon="gear", command=self.open_settings)
        self.settings_btn.pack(side="right", padx=(8, 0))
        self.plugins_btn = theme.Button(top, _("Plugins"), "puzzle", command=self.open_plugins)
        self.plugins_btn.pack(side="right", padx=(8, 0))
        self.btn["exp"] = theme.Button(top, _("Export"), "export", command=self.export_profile)
        self.btn["exp"].pack(side="right", padx=(8, 0))
        self.btn["imp"] = theme.Button(top, _("Import"), "import", command=self.import_profiles)
        self.btn["imp"].pack(side="right", padx=(8, 0))
        self.btn["redo"] = theme.Button(top, icon="redo", command=self.redo)
        self.btn["redo"].pack(side="right", padx=(8, 0))
        self.btn["undo"] = theme.Button(top, icon="undo", command=self.undo)
        self.btn["undo"].pack(side="right", padx=(8, 0))
        self._update_plugin_count()

    def _toggle_drop(self, which):
        panel, anchor = (self.dev_panel, self.dev_dd) if which == "dev" else (self.prof_panel, self.prof_dd)
        if panel.winfo_ismapped():
            panel.hide()
            return
        self._hide_drops()
        if which == "prof":  # Liste so hoch wie nötig, höchstens 420 px
            n = len(self.profile_items) + 1
            self.profile_list.canvas.configure(height=min(420, n * 36 + 4))
        panel.show(anchor)

    def _hide_drops(self):
        for p in (getattr(self, "dev_panel", None), getattr(self, "prof_panel", None)):
            if p is not None:
                p.hide()

    def drop_open(self):
        """Ist eine Liste der Kopfzeile aufgeklappt? (das virtuelle Deck hebt sich dann nicht darüber)"""
        try:
            return any(p.winfo_ismapped() for p in (self.dev_panel, self.prof_panel))
        except tk.TclError:
            return False

    def _profile_context(self, pid, x, y):
        """Rechtsklick auf ein Profil in der Liste. Die Liste schließt vorher: das Menü nimmt den Fokus und den
        Grab, sie bliebe sonst offen, ohne auf Esc zu reagieren (und läge über den Dialogen danach)."""
        self._hide_drops()
        m = theme.PopupMenu(self.root, width=220)
        m.add(_("Open"), lambda: self._on_profile_selected(pid), icon="folder")
        m.add(_("Rename…"), lambda: self._with_profile(pid, self.rename_profile), icon="edit")
        m.add(_("Duplicate"), lambda: self._with_profile(pid, self.duplicate_profile), icon="layers")
        m.add(_("Export…"), lambda: self._with_profile(pid, self.export_profile), icon="export")
        m.separator()
        m.add(_("Delete profile"), lambda: self._with_profile(pid, self.delete_profile), icon="trash", danger=True)
        return m.popup(x, y)

    def _with_profile(self, pid, fn):
        """Aktion für ein Profil aus der Liste: vorher öffnen, falls ein anderes offen ist."""
        if not self.doc or self.doc.id != pid:
            self.open_profile(pid)
        if self.doc and self.doc.id == pid:
            fn()

    def open_settings(self):
        """Design, Sprache und Updates."""
        from .settingsui import open_settings
        return open_settings(self.root, "FreeDeck", self._restart, freedeck=self, setup=self.open_setup)

    def _update_header(self):
        st = self.status_var.get()
        if not self.doc:
            self.prof_dd.set(_("No profile"))
            self._set_sub(st)
            return
        self.prof_dd.set(self.doc.name, profile_glyph(self.doc.name))
        ids = self.doc.page_ids
        parts = [model_name(self.doc.model) if self.doc.model else _("unknown")]
        if self.nav and self.nav[0] in ids:
            parts.append(_("Page {n} of {total}").format(n=ids.index(self.nav[0]) + 1, total=len(ids)))
        parts += [self.doc.folder_title(u) for u in self.nav[1:]]
        sub = "  ›  ".join(parts[1:])
        self._set_sub(parts[0] + (" · " + sub if sub else "") + (" · " + st if st else ""))

    def _set_sub(self, text):
        """Gerät, Seite und letzte Meldung unten links auf der Bühne."""
        self._sub_text = text
        try:
            self.canvas.itemconfigure("subtext", text=text or _("Drag actions onto keys · right-click for more"))
        except (AttributeError, tk.TclError):
            pass

    # ------------------------------------------------------------------ Mitte
    def _build_center(self, main):
        insp = tk.Frame(main, bg=COL["panel"], height=250, padx=22, pady=14)
        insp.pack(side="bottom", fill="x")
        insp.pack_propagate(False)
        tk.Frame(main, bg=COL["border"], height=1).pack(side="bottom", fill="x")
        foot = tk.Frame(main, bg=COL["canvas"], padx=20, pady=10)
        foot.pack(side="bottom", fill="x")
        self.pagebar = tk.Frame(foot, bg=COL["canvas"], height=36)
        self.pagebar.pack(side="left", fill="x", expand=True)
        self.canvas = tk.Canvas(main, bg=COL["canvas"], highlightthickness=0, height=300)
        self.canvas.pack(side="top", fill="both", expand=True, padx=12, pady=(12, 0))
        self.canvas.bind("<Configure>", lambda e: self.redraw())
        self.canvas.bind("<ButtonPress-1>", self._canvas_press)
        self.canvas.bind("<B1-Motion>", self._canvas_motion)
        self.canvas.bind("<ButtonRelease-1>", self._canvas_release)
        self.canvas.bind("<Double-Button-1>", self._canvas_double)
        self.canvas.bind("<Button-3>", self._canvas_menu)
        self.insp_head = tk.Frame(insp, bg=COL["panel"])
        self.insp_head.pack(side="top", fill="x")
        self.inspector = ScrollFrame(insp, bg=COL["panel"])
        self.inspector.pack(side="top", fill="both", expand=True, pady=(10, 0))

    # ------------------------------------------------------------ Bibliothek
    def _build_library(self, lib):
        box = tk.Frame(lib, bg=COL["side"], padx=12, pady=12)
        box.pack(fill="both", expand=True)
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self.fill_catalog())
        sb = RoundBox(box, height=36, radius=10, pad=4, padx=10)
        sb.pack(fill="x")
        tk.Label(sb.body, image=self.icons.get("search", 15, COL["muted"]), bg=COL["panel"]).pack(side="left")
        ent = tk.Entry(sb.body, textvariable=self.search_var, bg=COL["panel"], fg=COL["fg"],
                       insertbackground=COL["fg"], relief="flat", highlightthickness=0, bd=0,
                       font=theme.font(self.root, 10))
        ent.pack(side="left", fill="both", expand=True, padx=(8, 0))
        ph = tk.Label(sb.body, text=_("Search actions"), bg=COL["panel"], fg=COL["faint"],
                      font=theme.font(self.root, 10))

        def placeholder(*_a):
            if self.search_var.get() or self.root.focus_get() is ent:
                ph.place_forget()
            else:
                ph.place(in_=ent, x=0, rely=0.5, anchor="w")
        ent.bind("<FocusIn>", placeholder, add="+")
        ent.bind("<FocusOut>", placeholder, add="+")
        self.search_var.trace_add("write", placeholder)
        ph.bind("<Button-1>", lambda e: ent.focus_set())
        self.root.after_idle(placeholder)
        self.tree_hint = tk.StringVar(value=_("Drag onto a key or double-click"))
        tk.Label(box, textvariable=self.tree_hint, bg=COL["side"], fg=COL["muted"], wraplength=274, justify="left",
                 anchor="w", font=theme.font(self.root, 9)).pack(side="bottom", fill="x", pady=(8, 0))
        self.library = ScrollFrame(box, bg=COL["side"])
        self.library.pack(fill="both", expand=True, pady=(10, 0))
        self._lib_open = None  # aufgeklappte Kategorien (None = Vorgabe: die ersten beiden)

    def _bind_keys(self):
        r = self.root
        r.bind_all("<Control-z>", lambda e: self._shortcut(e, self.undo))
        r.bind_all("<Control-y>", lambda e: self._shortcut(e, self.redo))
        r.bind_all("<Control-Z>", lambda e: self._shortcut(e, self.redo))
        r.bind_all("<Control-c>", lambda e: self._shortcut(e, self.copy_selected))
        r.bind_all("<Control-x>", lambda e: self._shortcut(e, self.cut_selected))
        r.bind_all("<Control-v>", lambda e: self._shortcut(e, self.paste_selected))
        r.bind_all("<Delete>", lambda e: self._shortcut(e, self.delete_selected))
        r.bind_all("<Control-s>", lambda e: self.save_now())
        for key, d in (("<Left>", (-1, 0)), ("<Right>", (1, 0)), ("<Up>", (0, -1)), ("<Down>", (0, 1))):
            self.canvas.bind(key, lambda e, d=d: (self._move_selection(*d), "break")[1])
        self.canvas.bind("<Return>", self._canvas_double_selected)

    def _shortcut(self, e, fn):
        try:
            if e.widget.winfo_class() in TEXT_WIDGETS or getattr(e.widget, "_hotkey", False):
                return None
        except (AttributeError, tk.TclError):
            return None
        fn()
        return "break"

    def status(self, text):
        self.status_var.set(text)
        self._update_header()

    # ============================================================== Profile
    def refresh_profiles(self):
        self.store.reload()
        profs = sorted(self.store.profiles.values(), key=lambda p: (p.name.lower(), p.id))
        self._profile_ids = [p.id for p in profs]
        self.profile_list.clear()
        f = self.profile_list.inner
        self.profile_items = {}
        cur = self.doc.id if self.doc else None
        shapes = self._connected_shapes()
        for p in profs:
            n = len(p.page_ids)
            on = p.id == cur
            other = bool(shapes) and _shape(p.model) not in shapes  # Profil für ein nicht angeschlossenes Gerät
            count = ngettext("{n} page", "{n} pages", n).format(n=n) if on else n
            if other and not on:  # kurzer Modellname statt Seitenzahl („Mini MK.2“)
                count = (model_name(p.model) if p.model else "").replace("FreeDeck ", "")
            it = theme.NavItem(f, p.name, profile_glyph(p.name), count, active=on, dim=other,
                               command=lambda i=p.id: self._on_profile_selected(i))
            it.bind("<Button-3>", lambda e, i=p.id: self._profile_context(i, e.x_root, e.y_root))
            it.pack(fill="x", pady=(0, 2))
            self.profile_items[p.id] = it
        theme.NavItem(f, _("New profile"), "plus", accent=True, command=self.new_profile).pack(fill="x")
        self._update_header()

    def _on_profile_selected(self, pid):
        self._hide_drops()
        if pid in self._profile_ids and (not self.doc or self.doc.id != pid):
            self.open_profile(pid)

    def close_doc(self):
        if self.doc:
            if self._save_job:
                self.root.after_cancel(self._save_job)
                self._save_job = None
            try:
                self.doc.cleanup()
            except OSError as e:
                log.warning(_("Cleanup failed: %s"), e)
        self.doc = None

    def open_profile(self, pid, from_device=False):
        p = self.store.get(pid)
        if not p:
            self.store.reload()
            p = self.store.get(pid)
        if not p:
            return
        self.close_doc()
        try:
            self.doc = E.ProfileDoc(p.root)
        except (E.EditError, OSError, ValueError) as e:
            messagebox.showerror(_("Open profile"), str(e), parent=self.root)
            self.show_empty()
            return
        self.doc.on_change = self._on_doc_change
        self.nav = [self.doc.page_ids[0]] if self.doc.page_ids else []
        self.selected = None
        self._img_cache.clear()
        self._tk_cache.clear()
        self._live_keys.clear()
        self.refresh_profiles()
        self.root.title(f"FreeDeck – {self.doc.name}")
        if self.daemon is None:
            self.build_devices()
        self.fill_catalog()
        self.refresh_all()
        self.status(_("{name} opened – changes are saved automatically").format(name=self.doc.name))
        if not from_device:
            self._device_follow(0)

    def show_empty(self):
        self.doc = None
        self.redraw()
        for c in self.pagebar.winfo_children():
            c.destroy()
        self.inspector.clear()
        for c in self.insp_head.winfo_children():
            c.destroy()
        for k in ("exp", "undo", "redo"):
            self.btn[k].configure_text(enabled=False)
        if self.daemon is None:
            self.build_devices()
        self._update_header()
        self.fill_catalog()

    def _connected_shapes(self):
        """Tastenraster der angeschlossenen Stream Decks (leer = keins angeschlossen/bekannt)."""
        models = [c.model for c in self._controllers() if getattr(c, "model", None)]
        if not models and self.daemon is None:
            models = [d["model"] for d in connected_models()]
        return {_shape(m) for m in models}

    def auto_model(self):
        """Modell für ein neues Profil: das gewählte/angeschlossene Gerät, sonst das zuletzt benutzte.

        Gibt (Modellcode, angeschlossen) zurück."""
        ctrl = self._ctrl() or next(iter(self._controllers()), None)
        if ctrl is not None and getattr(ctrl, "model", None):
            return ctrl.model, True
        plugged = connected_models()
        if plugged:
            return plugged[0]["model"], True
        if self.doc and self.doc.model:
            return self.doc.model, False
        models = [p.model for p in self.store.profiles.values() if p.model]
        return (max(set(models), key=models.count) if models else "20GBA9901"), False

    def new_profile(self):
        self._hide_drops()  # „Neues Profil“ steht in der Liste: sie läge sonst über dem Dialog
        code, plugged = self.auto_model()
        dlg = NewProfileDialog(self.root, code, plugged)
        if not dlg.result:
            return
        name = dlg.result
        root = E.create_profile(name, code, self.store_root)
        self.refresh_profiles()
        self.open_profile(root.name[: -len(".sdProfile")])

    def duplicate_profile(self):
        if not self.doc:
            return
        name = simpledialog.askstring(_("Duplicate profile"), _("Name of the copy:"),
                                      initialvalue=_("{name} (copy)").format(name=self.doc.name),
                                      parent=self.root)
        if not name:
            return
        self.doc.save()
        dst = E.duplicate_profile(self.doc.root, name)
        self.refresh_profiles()
        self.open_profile(dst.name[: -len(".sdProfile")])

    def rename_profile(self):
        if not self.doc:
            return
        name = simpledialog.askstring(_("Rename profile"), _("New name:"), initialvalue=self.doc.name,
                                      parent=self.root)
        if name and name.strip():
            self.doc.rename(name)
            self.save_now()
            self.refresh_profiles()
            self.root.title(f"FreeDeck – {self.doc.name}")

    def delete_profile(self):
        if not self.doc:
            return
        if not messagebox.askyesno(_("Delete profile"),
                                   _("Permanently delete profile “{name}”?").format(name=self.doc.name),
                                   parent=self.root, icon="warning"):
            return
        pid = self.doc.id
        if self._save_job:
            self.root.after_cancel(self._save_job)
            self._save_job = None
        self.doc = None
        delete_profile(pid, self.store_root)
        self.refresh_profiles()
        if self._profile_ids:
            self.open_profile(self._profile_ids[0])
        else:
            self.show_empty()

    def import_profiles(self):
        files = filedialog.askopenfilenames(
            parent=self.root, title=_("Import profiles"),
            filetypes=[(_("Profiles (Elgato format)"), "*.streamDeckProfile *.streamDeckProfilesBackup *.zip"),
                       (_("All files"), "*")])
        if not files:
            return
        ids = []
        for f in files:
            try:
                ids += import_path(f, self.store_root)
            except Exception as e:  # noqa: BLE001
                messagebox.showerror(_("Import"), f"{f}:\n{e}", parent=self.root)
        self.refresh_profiles()
        self.status(ngettext("{n} profile imported", "{n} profiles imported", len(ids)).format(n=len(ids)))
        if ids:
            self.open_profile(ids[0])

    def export_profile(self):
        if not self.doc:
            return
        safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in self.doc.name).strip() or _("Profile")
        f = filedialog.asksaveasfilename(parent=self.root, title=_("Export profile"),
                                         initialfile=f"{safe}.streamDeckProfile",
                                         defaultextension=".streamDeckProfile",
                                         filetypes=[(_("Profile (Elgato format)"), "*.streamDeckProfile")])
        if f:
            out = self.doc.export(f)
            self.status(_("Exported: {path}").format(path=out))

    # ============================================================ Speichern
    def _on_doc_change(self):
        self._img_cache_trim()
        self.redraw()
        self._update_buttons()
        if self._save_job:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(400, self.save_now)
        self.status(_("Saving…"))

    def save_now(self):
        self._save_job = None
        if not self.doc:
            return
        try:
            if self.doc.save():
                self.status(_("saved"))
        except OSError as e:
            self.status(_("Saving failed: {error}").format(error=e))
            messagebox.showerror(_("Save"), str(e), parent=self.root)

    def close(self):
        if self.daemon is not None:
            self.hide()  # wie bei Elgato: Fenster zu, Stream Deck läuft weiter
            return
        self.close_doc()
        self.root.destroy()

    def undo(self):
        if self.doc and self.doc.undo():
            self._after_structural_change()

    def redo(self):
        if self.doc and self.doc.redo():
            self._after_structural_change()

    def _after_structural_change(self):
        reach = self.doc.reachable()
        if not self.nav or any(u.lower() not in reach for u in self.nav):
            self.nav = [self.doc.page_ids[0]]
        self.refresh_profiles()
        self.refresh_all()

    def _update_buttons(self):
        if not self.doc:
            return
        self.btn["exp"].configure_text(enabled=True)
        self.btn["undo"].configure_text(enabled=bool(self.doc.undo_stack))
        self.btn["redo"].configure_text(enabled=bool(self.doc.redo_stack))

    # ============================================== Plugin-Einstellungsseite
    def open_property_inspector(self, page_uuid, slot):
        """Einstellungsseite (Property Inspector) eines Plugins über den laufenden Dienst öffnen."""
        import os
        import shutil
        import subprocess
        import urllib.parse
        import webbrowser
        self.save_now()
        try:
            info = json.loads((paths.STATE_DIR / "plugin-host.json").read_text(encoding="utf-8"))
            os.kill(int(info["pid"]), 0)
        except (OSError, ValueError, KeyError):
            messagebox.showinfo(_("Settings page"), _("FreeDeck must be running for this (menu: “FreeDeck” "
                                "or `freedeck`), because it runs the plugins."), parent=self.root)
            return
        pos = f"key:{slot[1]},{slot[2]}" if slot[0] == "key" else f"dial:{slot[1]}"
        q = urllib.parse.urlencode({"profile": self.doc.id, "page": page_uuid, "slot": pos})
        url = f"http://127.0.0.1:{info['port']}/pi/{info['token']}?{q}"
        for exe in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "brave-browser",
                    "microsoft-edge"):
            if shutil.which(exe):
                subprocess.Popen([exe, f"--app={url}", "--window-size=460,720", "--new-window"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                break
        else:
            webbrowser.open(url)
        self.status(_("Settings page opened – changes are applied automatically"))
        self._watch_external()

    def _watch_external(self):
        if getattr(self, "_watch_job", None) or not self.doc:
            return

        def poll():
            self._watch_job = None
            if not self.doc:
                return
            if not self._save_job and self.doc.reload_external():
                self._img_cache.clear()
                self._tk_cache.clear()
                self._live_keys.clear()
                self.redraw()
                focus = self.root.focus_get()
                if not (focus and focus.winfo_class() in TEXT_WIDGETS):
                    self.build_inspector()
            self._watch_job = self.root.after(1500, poll)
        self._watch_job = self.root.after(1500, poll)

    # ================================================================ Seiten
    @property
    def page_uuid(self):
        return self.nav[-1] if self.nav else None

    def refresh_all(self):
        self.build_pagebar()
        self.redraw()
        self.build_inspector()
        self._update_buttons()

    def build_pagebar(self):
        for c in self.pagebar.winfo_children():
            c.destroy()
        self._update_header()
        if not self.doc:
            return
        bar = self.pagebar
        n = len(self.doc.page_ids)
        it = getattr(self, "profile_items", {}).get(self.doc.id)
        if it is not None:
            it.set(count=ngettext("{n} page", "{n} pages", n).format(n=n))
        if len(self.nav) > 1:
            self.back_btn = theme.Button(bar, _("↩ Back"), command=self.folder_back, height=30)
            self.back_btn.pack(side="left", padx=(0, 10))
        has_bg = self.doc.background(self.page_uuid)[0] is not None
        theme.Button(bar, _("Background"), icon="image", kind="primary" if has_bg else "default",
                     command=self.open_background).pack(side="right")
        # Seiten wie in der Deck-Software: ‹ ● ○ ○ › mittig unter den Tasten, daneben + für eine neue
        mid = tk.Frame(bar, bg=_bg(bar))
        mid.place(relx=0.5, rely=0.5, anchor="center")
        self.page_dots = PageDots(mid, self, self.doc.page_ids, self.nav[0] if self.nav else None)
        self.page_dots.pack(side="left")
        theme.PillButton(mid, text="+", height=26, padx=9, command=self.add_page).pack(side="left", padx=(10, 0))

    def _page_menu(self, e, pid):
        """Kontextmenü einer Seitenschaltfläche: verschieben, löschen, hinzufügen."""
        if self.nav and self.nav[0] != pid:
            self.goto_page(pid)
        ids = self.doc.page_ids
        idx = ids.index(pid) if pid in ids else 0
        m = theme.PopupMenu(self.root, width=210)
        m.add(_("Move left"), (lambda: self.move_page(-1)) if idx > 0 else None, icon="chevron")
        m.add(_("Move right"), (lambda: self.move_page(1)) if idx < len(ids) - 1 else None, icon="arrow")
        m.separator()
        m.add(_("Add page"), self.add_page, icon="plus")
        m.add(_("Delete page"), self.remove_page if len(ids) > 1 else None, icon="trash", danger=True)
        return m.popup(e.x_root, e.y_root)

    def goto_page(self, pid):
        self.nav = [pid]
        self.selected = None
        self.refresh_all()
        if self.doc and pid in self.doc.page_ids:
            self._device_follow(self.doc.page_ids.index(pid))

    def add_page(self):
        pid = self.doc.add_page(after=self.nav[0] if self.nav else None)
        self.goto_page(pid)

    def remove_page(self):
        pid = self.nav[0]
        pg = self.doc.page(pid)
        if pg and pg.items() and not messagebox.askyesno(
                _("Delete page"), _("This page contains actions. Delete it anyway?"), parent=self.root):
            return
        idx = self.doc.page_ids.index(pid)
        try:
            self.doc.remove_page(pid)
        except E.EditError as e:
            self.status(str(e))
            return
        ids = self.doc.page_ids
        self.goto_page(ids[min(idx, len(ids) - 1)])

    def move_page(self, delta):
        self.doc.move_page(self.nav[0], delta)
        self.refresh_all()

    def open_folder(self, uuid):
        if not uuid or not self.doc.page(uuid):
            self.status(_("Folder not found"))
            return
        self.nav.append(uuid)
        self.selected = None
        self.refresh_all()

    def folder_back(self):
        if len(self.nav) > 1:
            self.nav.pop()
            self.selected = None
            self.refresh_all()

    # ================================================================ Gerät
    def _slots(self):
        g = self.doc.geometry
        keys = [("key", c, r) for r in range(g.rows) for c in range(g.cols)]
        dials = [("dial", i) for i in range(g.dials)]
        return g, keys, dials

    def _img_cache_trim(self):
        if len(self._img_cache) > 600:
            self._img_cache.clear()
            self._tk_cache.clear()
            self._live_keys.clear()

    def _tk_image(self, img):
        """Tk-Bild zu einem (zwischengespeicherten) PIL-Bild – die Umwandlung kostet bei jedem Neuzeichnen
        sonst spürbar Zeit. Schlüssel ist das PIL-Objekt selbst; es bleibt im Eintrag, damit die id gültig bleibt."""
        hit = self._tk_cache.get(id(img))
        if hit is None or hit[0] is not img:
            if len(self._tk_cache) > 600:
                self._tk_cache.clear()
            hit = self._tk_cache[id(img)] = (img, ImageTk.PhotoImage(img, master=self.root))
        return hit[1]

    def _tk_forget(self, img):
        """Tk-Bild eines PIL-Bildes verwerfen, das nicht mehr gezeichnet wird."""
        hit = self._tk_cache.get(id(img))
        if hit is not None and hit[0] is img:
            del self._tk_cache[id(img)]

    def _device_page(self):
        """Gerät, das gerade genau die bearbeitete Seite zeigt (sonst None)."""
        ctrl = self._ctrl()
        if ctrl is None or not self.doc or not getattr(ctrl, "profile", None) or ctrl.profile.id != self.doc.id \
                or not hasattr(ctrl, "live_image"):
            return None
        pg = ctrl.current_page()
        if pg is None or not self.page_uuid or pg.uuid.lower() != self.page_uuid.lower():
            return None
        return ctrl

    def _live(self, slot, size):
        """Tastenbild so, wie das Deck es gerade zeigt (Lautstärke, Stumm, Zustand) – oder None."""
        if slot[0] != "key" or self._save_job:
            return None
        ctrl = self._device_page()
        if ctrl is None or slot[1] >= ctrl.geometry.cols:
            return None
        img = ctrl.live_image(slot[2] * ctrl.geometry.cols + slot[1])
        if img is None:
            return None
        key = ("live", id(img), size)
        hit = self._img_cache.get(key)
        if hit is None or hit[0] is not img:
            self._img_cache_trim()
            hit = self._img_cache[key] = (img, _rounded(img.convert("RGBA").resize(size, Image.LANCZOS),
                                                        max(6, size[0] // 8)))
        # das vorige Bild dieser Taste kommt nie wieder: aus beiden Speichern nehmen – sonst sammeln Live-Tasten
        # (Pegel, Uhr) alle 300 ms neue Einträge, bis _img_cache_trim auch alle festen Tastenbilder verwirft
        old = self._live_keys.get(tuple(slot))
        self._live_keys[tuple(slot)] = key
        if old is not None and old != key and old not in self._live_keys.values():
            gone = self._img_cache.pop(old, None)
            if gone is not None:
                self._tk_forget(gone[1])
        return hit[1]

    def _render(self, slot, data, size):
        live = self._live(slot, size)
        if live is not None:
            return live
        tile = self._tiles.get((slot[1], slot[2])) if slot[0] == "key" and getattr(self, "_tiles", None) else None
        key = (self.page_uuid, slot[0], json.dumps(data, sort_keys=True), size,
               (slot[1:], self._tiles_id) if tile is not None else None,
               self.doc.page_ids.index(self.nav[0]) if self.nav and self.nav[0] in self.doc.page_ids else 0)
        img = self._img_cache.get(key)
        if img is None:
            if slot[0] == "dial":
                # in Originalgröße des Touchstreifen-Segments zeichnen, dann skalieren
                img = E.preview_dial(self.doc, self.page_uuid, data, (max(100, 100 * size[0] // size[1]), 100))
                img = img.resize(size, Image.LANCZOS)
            else:
                img = E.preview_action(self.doc, self.page_uuid, data, size, page_index=key[-1], base=tile)
            img = _rounded(img.convert("RGBA"), max(6, size[0] // 8))
            self._img_cache[key] = img
        return img

    def _draw_stage(self, footer_right=None):
        """Bühne: Karte mit Rand, Fußzeile mit Hinweis und Helligkeit."""
        cv = self.canvas
        w, h = cv.winfo_width(), cv.winfo_height()
        f = theme.font(self.root, 9)
        cv.create_text(18, h - FOOTER_H / 2 - 2, anchor="w", fill=COL["muted"], font=f, tags="subtext",
                       text=getattr(self, "_sub_text", "") or _("Drag actions onto keys · right-click for more"))
        ctrl = self._ctrl()
        bright = getattr(ctrl, "brightness", None) if ctrl is not None else None
        if footer_right is None and isinstance(bright, (int, float)):
            footer_right = _("Brightness {n} %").format(n=int(bright))
        if footer_right:
            cv.create_text(w - 18, h - FOOTER_H / 2 - 2, anchor="e", fill=COL["muted"], font=f, text=footer_right)

    def redraw(self):
        cv = self.canvas
        cv.delete("all")
        self._photos = []
        self._hit = []
        self._rings = {}
        self._tiles, self._tiles_id = {}, None  # vor jedem frühen return: das Pedal hat keinen Hintergrund
        if not self.doc:
            self._draw_stage()
            cv.create_text(cv.winfo_width() / 2, (cv.winfo_height() - FOOTER_H) / 2, anchor="center",
                           justify="center", fill=COL["muted"], font=theme.font(self.root, 11),
                           text=_("No profiles yet.\n\n“New…” creates an empty profile,\n"
                                  "“Import…” takes over profiles from the Elgato app."))
            return
        g, keys, dials = self._slots()
        kind = _stage_kind(self.doc.model, g)
        self._draw_stage(_("No display") if kind == "pedal" else None)
        if not self.page_uuid or not self.doc.page(self.page_uuid):
            return
        pad = 18
        aw, ah = cv.winfo_width() - 24, cv.winfo_height() - FOOTER_H - 12
        L = _layout(g, aw, ah, pad, kind)
        ts, gap = L["ts"], L["gap"]
        cw, ch = max(aw, L["w"] + 2 * pad), max(ah, L["h"] + 2 * pad)
        bx = 12 + (cw - L["w"]) // 2
        y0 = 12 + max(pad, (ch - L["h"]) // 2)
        x0 = bx + L["side"]
        page = self.doc.page(self.page_uuid)
        if kind == "pedal":
            for slot in keys:
                x = x0 + slot[1] * (L["pw"] + 2 * gap)
                self._draw_pedal(slot, page.get(slot), x, y0, L["pw"], L["ph"])
            return
        bg_path, bg_dim = self.doc.background(self.page_uuid)
        self._tiles = render.background_tiles(bg_path, g.cols, g.rows, (ts, ts), bg_dim, gap_ratio=gap / ts) \
            if bg_path else {}
        self._tiles_id = (str(bg_path), bg_dim) if bg_path else None
        for slot in keys:
            c, r = slot[1], slot[2]
            x, y = x0 + c * (ts + gap), y0 + r * (ts + gap)
            self._draw_slot(slot, page.get(slot), x, y, ts, ts)
        below = y0 + L["grid_h"]
        if kind == "plus":
            self._draw_strip(L, x0, below + 16, page, dials)
        elif kind == "neo":
            self._draw_infobar(L, x0, below + gap)
        elif kind == "studio":
            for slot in dials:
                cx = bx + L["ring"] + gap if slot[1] == 0 else bx + L["w"] - L["ring"] - gap
                self._draw_ring_dial(slot, page.get(slot), cx, y0 + L["grid_h"] / 2 - 8, L["ring"],
                                     label=2 * L["ring"] + 2 * gap)

    def _dial_info(self, data):
        key = ("dialinfo", self.page_uuid, json.dumps(data, sort_keys=True))
        hit = self._img_cache.get(key)
        import time
        if hit is None or time.monotonic() - hit[0] > 3:
            try:
                hit = (time.monotonic(), E.dial_info(self.doc, self.page_uuid, data))
            except Exception:  # noqa: BLE001
                log.debug("dial_info", exc_info=True)
                hit = (time.monotonic(), {"icon": None, "title": C.display_name(data), "value": "",
                                          "indicator": None})
            self._img_cache[key] = hit
        return hit[1]

    def _photo(self, img):
        ph = ImageTk.PhotoImage(img, master=self.root)
        self._photos.append(ph)
        return ph

    def _sel_ring(self, slot, shape, box, r=0):
        """Auswahlrahmen eines Feldes merken und – wenn es gewählt ist – zeichnen (Tag „sel“).
        So kann select() nur den Rahmen versetzen, statt das ganze Gerät neu zu zeichnen."""
        self._rings[tuple(slot)] = (shape, box, r)
        if self.selected is not None and tuple(self.selected) == tuple(slot):
            self._draw_ring(shape, box, r)

    def _draw_ring(self, shape, box, r):
        if shape == "oval":
            self.canvas.create_oval(*box, outline=COL["accent"], width=3, tags="sel")
        else:
            _round_rect(self.canvas, *box, r, fill="", outline=COL["accent"], width=3, tags="sel")

    def _show_selection(self):
        self.canvas.delete("sel")
        spec = self._rings.get(tuple(self.selected)) if self.selected is not None else None
        if spec:
            self._draw_ring(*spec)

    def _draw_strip(self, L, x0, y, page, dials):
        """Touchstreifen als Felder (Symbol, Name, Wert), darunter je Regler ein Werte-Ring."""
        cv = self.canvas
        seg, ch = L["seg_w"], L["card_h"]
        sp = self.doc.strip_background(self.page_uuid)
        if sp:
            fr = render.load_frames(sp, int(L["grid_w"]), ch)
            if fr:
                strip = fr[0][0].convert("RGBA").resize((int(L["grid_w"]), ch))
                cv.create_image(x0, y, image=self._photo(_rounded(strip, 10)), anchor="nw")
        for slot in dials:
            i = slot[1]
            x = x0 + i * (seg + 6)
            data = page.get(slot)
            if data is None:
                _round_rect(cv, x, y, x + seg, y + ch, 10, fill="", outline=COL["line"], width=2, dash=(5, 4))
                cv.create_text(x + seg / 2, y + ch / 2, text="+", fill=COL["faint"], font=(self._fam, 14))
                info = None
            else:
                info = self._dial_info(data)
                _round_rect(cv, x, y, x + seg, y + ch, 10, fill="" if sp else COL["card"], outline=COL["card_edge"],
                            width=1)
                tx = x + 10
                if info["icon"] is not None:
                    ic = render._fit(info["icon"].convert("RGBA"), 18, 18)
                    cv.create_image(x + 10, y + 8, image=self._photo(ic), anchor="nw")
                    tx = x + 34
                name = _clip(info["title"], self._small_bold, int(x + seg - 8 - tx))
                cv.create_text(tx, y + 17, text=name, anchor="w", fill=COL["fg"], font=self._small_bold)
                ind = info["indicator"]
                if ind is not None or info["value"]:
                    by = y + ch - 13
                    vx = x + seg - 9
                    cv.create_text(vx, by + 2, text=info["value"], anchor="e", fill=COL["muted"],
                                   font=self._small)
                    bw = max(10, seg - 20 - (self._small.measure(info["value"]) + 8 if info["value"] else 0))
                    if ind is not None:
                        _round_rect(cv, x + 10, by, x + 10 + bw, by + 5, 2, fill=COL["raised"], outline="")
                        if ind > 0:
                            _round_rect(cv, x + 10, by, x + 10 + bw * min(1, ind / 100), by + 5, 2,
                                        fill=COL["accent"], outline="")
            self._sel_ring(slot, "rect", (x - 3, y - 3, x + seg + 3, y + ch + 3), 12)
            self._hit.append((x, y, x + seg, y + ch, slot))
            self._draw_ring_dial(slot, data, x + seg / 2, y + ch + 12 + L["ring"], L["ring"], info=info,
                                 select=False)

    def _draw_ring_dial(self, slot, data, cx, cy, r, label=0, info=None, select=True):
        """Regler als Ring: grauer Bogen, darin der Wert (falls bekannt); label: Breite des Namens darunter."""
        cv = self.canvas
        if data is not None and info is None:
            info = self._dial_info(data)
        w = max(3, r // 5)
        box = (cx - r, cy - r, cx + r, cy + r)
        cv.create_arc(*box, start=225, extent=-270, style="arc", outline=COL["raised"], width=w)
        ind = info["indicator"] if info else None
        if ind is not None and ind > 0:
            cv.create_arc(*box, start=225, extent=-270 * min(1, ind / 100), style="arc", outline=COL["accent"],
                          width=w)
        if ind is not None:
            cv.create_text(cx, cy, text=str(int(ind)), fill=COL["fg"], font=self._small_bold)
        elif info and info["icon"] is not None and r >= 16:
            ic = render._fit(info["icon"].convert("RGBA"), int(r), int(r))
            cv.create_image(cx, cy, image=self._photo(ic))
        elif data is None:
            cv.create_text(cx, cy, text="+", fill=COL["faint"], font=(self._fam, 12))
        y1 = cy + r
        if label:
            text = info["title"] if info else _("Dial")
            cv.create_text(cx, cy + r + 10, text=_clip(text, self._small_bold, int(label)), fill=COL["muted"],
                           font=self._small_bold)
            y1 = cy + r + 18
        if select:
            self._sel_ring(slot, "oval", (cx - r - 5, cy - r - 5, cx + r + 5, cy + r + 5))
            self._hit.append((cx - r - 4, cy - r - 4, cx + r + 4, y1, slot))
        else:
            self._hit.append((cx - r, cy - r, cx + r, cy + r, slot))

    def _draw_infobar(self, L, x0, y):
        """Neo: Infoleiste mit Uhrzeit und Seite, links und rechts die Touch-Tasten (Seite wechseln)."""
        import time
        cv = self.canvas
        h, tw = L["bar_h"], L["bar_h"]
        x1 = x0 + L["grid_w"]
        n = len(self.doc.page_ids)
        idx = self.doc.page_ids.index(self.nav[0]) + 1 if self.nav and self.nav[0] in self.doc.page_ids else 1
        # Touch-Tasten blättern wie am Gerät durch die Seiten (in der Elgato-App nicht belegbar)
        for x, sign, step in ((x0, "‹", -1), (x1 - tw, "›", 1)):
            tag = f"neo{step}"
            on = n > 1 and len(self.nav) == 1
            _round_rect(cv, x, y, x + tw, y + h, 10, fill=COL["card"], outline=COL["card_edge"], width=1, tags=tag)
            cv.create_text(x + tw / 2, y + h / 2 - 1, text=sign, fill=COL["fg"] if on else COL["faint"],
                           font=(self._fam, 15), tags=tag)
            if on:
                target = self.doc.page_ids[(idx - 1 + step) % n]
                cv.tag_bind(tag, "<Button-1>", lambda e, p=target: self.root.after_idle(self.goto_page, p))
                cv.tag_bind(tag, "<Enter>", lambda e: cv.configure(cursor="hand2"))
                cv.tag_bind(tag, "<Leave>", lambda e: cv.configure(cursor=""))
        ix0, ix1 = x0 + tw + 10, x1 - tw - 10
        _round_rect(cv, ix0, y, ix1, y + h, 10, fill=COL["device"], outline=COL["card_edge"], width=1)
        big = theme.font(self.root, 10, "bold")
        if h >= 40:
            cv.create_text((ix0 + ix1) / 2, y + h * 0.36, text=time.strftime("%H:%M"), fill="#e9ecf2", font=big)
            cv.create_text((ix0 + ix1) / 2, y + h * 0.72, text=_("Page {n} / {m}").format(n=idx, m=n),
                           fill="#8b93a3", font=self._small)
        else:
            cv.create_text((ix0 + ix1) / 2, y + h / 2, fill="#e9ecf2", font=self._small,
                           text=time.strftime("%H:%M") + "  ·  " + _("Page {n} / {m}").format(n=idx, m=n))

    def _draw_pedal(self, slot, data, x, y, w, h):
        """Pedal: kein Display – Riffeln, Symbol und Name der Aktion."""
        cv = self.canvas
        if data is None:
            _round_rect(cv, x, y, x + w, y + h, 16, fill="", outline=COL["line"], width=2, dash=(5, 4))
            cv.create_text(x + w / 2, y + h / 2, text="+", fill=COL["faint"], font=(self._fam, 18))
        else:
            _round_rect(cv, x, y, x + w, y + h, 16, fill=COL["card"], outline=COL["card_edge"], width=1)
            for k in range(5):
                yy = y + h * 0.15 + k * h * 0.065
                cv.create_line(x + w * 0.2, yy, x + w * 0.8, yy, fill=COL["card_edge"], width=2)
            s = int(min(w, h) * 0.42)
            img = self._render(slot, data, (s, s))
            cv.create_image(x + w / 2, y + h * 0.62, image=self._photo(img))
            title = ((data.get("States") or [{}])[0] or {}).get("Title") if isinstance(data.get("States"), list) \
                else ""
            name = _clip(title or C.display_name(data), self._small_bold, int(w - 12))
            cv.create_text(x + w / 2, y + h * 0.88, text=name, fill=COL["fg"], font=self._small_bold)
        self._sel_ring(slot, "rect", (x - 3, y - 3, x + w + 3, y + h + 3), 18)
        self._hit.append((x, y, x + w, y + h, slot))

    def _draw_slot(self, slot, data, x, y, w, h):
        cv = self.canvas
        tile = self._tiles.get((slot[1], slot[2])) if slot[0] == "key" and getattr(self, "_tiles", None) else None
        if data is None and tile is not None:
            # leere Taste auf dem Seiten-Hintergrund: gerundete Kachel merken statt bei jedem Neuzeichnen neu –
            # ein Eintrag je Taste (die Kachel wechselt mit Größe und Bild), sonst sammelt das Ziehen am Fenster
            # für jede Größe neue Einträge
            key = ("tile", tuple(slot[1:]))
            hit = self._img_cache.get(key)
            if hit is None or hit[0] is not tile:
                if hit is not None:
                    self._tk_forget(hit[1])
                hit = self._img_cache[key] = (tile, _rounded(tile, max(6, w // 8)))
            cv.create_image(x, y, image=self._tk_image(hit[1]), anchor="nw")
        elif data is None:
            _round_rect(cv, x, y, x + w, y + h, max(6, w // 8), fill="", outline=COL["line"], width=2, dash=(5, 4))
            cv.create_text(x + w / 2, y + h / 2, text="+", fill=COL["faint"], font=(self._fam, max(10, w // 5)))
        else:
            cv.create_image(x, y, image=self._tk_image(self._render(slot, data, (w, h))), anchor="nw")
            if not A.is_supported(data.get("UUID", "")):
                cv.create_oval(x + w - 14, y + 4, x + w - 4, y + 14, fill="#f5a623", outline="")
        self._sel_ring(slot, "rect", (x - 3, y - 3, x + w + 3, y + h + 3), max(8, w // 7))
        self._hit.append((x, y, x + w, y + h, slot))

    def slot_at(self, x, y):
        for x0, y0, x1, y1, slot in self._hit:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return slot
        return None

    def select(self, slot, redraw=False):
        """Feld wählen. redraw: das Gerät neu zeichnen (nach Änderungen am Inhalt); sonst wandert nur der Rahmen."""
        self.selected = slot
        if not redraw and slot is not None and tuple(slot) in getattr(self, "_rings", {}):
            self._show_selection()  # nur der Rahmen wandert
        else:
            self.redraw()
        self.build_inspector()

    def _move_selection(self, dx, dy):
        if not self.doc:
            return
        g = self.doc.geometry
        s = self.selected or ("key", 0, 0)
        if s[0] == "key":
            c, r = max(0, min(g.cols - 1, s[1] + dx)), max(0, min(g.rows - 1, s[2] + dy))
            if dy > 0 and s[2] == g.rows - 1 and g.dials:
                self.select(("dial", min(g.dials - 1, s[1] * g.dials // g.cols)))
                return
            self.select(("key", c, r))
        else:
            if dy < 0:
                self.select(("key", min(g.cols - 1, s[1] * g.cols // max(1, g.dials)), g.rows - 1))
            else:
                self.select(("dial", max(0, min(g.dials - 1, s[1] + dx))))

    # -------------------------------------------------- Maus auf dem Gerät
    def _canvas_press(self, e):
        self.canvas.focus_set()
        slot = self.slot_at(e.x, e.y)
        self._drag = {"slot": slot, "x": e.x, "y": e.y, "active": False, "ghost": None}
        if slot is not None:
            self.select(slot)

    def _canvas_motion(self, e):
        d = self._drag
        if not d or d["slot"] is None or not self.doc.get(self.page_uuid, d["slot"]):
            return
        if not d["active"] and abs(e.x - d["x"]) + abs(e.y - d["y"]) > 6:
            d["active"] = True
            self.canvas.configure(cursor="fleur")
        if d["active"]:
            self._show_drop_target(self.slot_at(e.x, e.y))

    def _canvas_release(self, e):
        d, self._drag = self._drag, None
        self.canvas.configure(cursor="")
        self.canvas.delete("droptarget")
        if not d or not d["active"]:
            return
        target = self.slot_at(e.x, e.y)
        if target is None:
            # auf die Seitenpunkte gezogen? (‹ › = Nachbarseite)
            w = self.root.winfo_containing(e.x_root, e.y_root)
            pid = getattr(w, "_page", None)
            if isinstance(w, PageDots):
                hit = w._index_at(e.x_root - w.winfo_rootx())
                cur = w.ids.index(w.current) if w.current in w.ids else 0
                hit = cur - 1 if hit == "prev" else cur + 1 if hit == "next" else hit
                pid = w.ids[hit] if isinstance(hit, int) and 0 <= hit < len(w.ids) else None
            if pid and pid != self.nav[0]:
                self._move_to_page(d["slot"], pid)
            return
        if not self._fits(self.doc.get(self.page_uuid, d["slot"]), target):
            return
        self.doc.move(self.page_uuid, d["slot"], self.page_uuid, target)
        self.select(target, redraw=True)

    def _move_to_page(self, slot, pid):
        g, keys, dials = self._slots()
        page = self.doc.page(pid)
        free = next((s for s in (keys if slot[0] == "key" else dials) if page.get(s) is None), None)
        if free is None:
            self.status(_("There is no free space on the target page"))
            return
        self.doc.move(self.page_uuid, slot, pid, free)
        self.selected = None
        self.refresh_all()
        self.status(_("Action moved to page {n}").format(n=self.doc.page_ids.index(pid) + 1))

    def _show_drop_target(self, slot):
        self.canvas.delete("droptarget")
        if slot is None:
            return
        for x0, y0, x1, y1, s in self._hit:
            if s == slot:
                _round_rect(self.canvas, x0 - 3, y0 - 3, x1 + 3, y1 + 3, 10, fill="", outline=COL["drop"],
                            width=3, tags="droptarget")

    def _canvas_double(self, e):
        slot = self.slot_at(e.x, e.y)
        data = self.doc.get(self.page_uuid, slot) if slot and self.doc else None
        if not data:
            return
        if data.get("UUID") == C.FOLDER_UUID:
            self.open_folder((data.get("Settings") or {}).get("ProfileUUID"))
        elif data.get("UUID") == C.BACK_UUID:
            self.folder_back()

    def _canvas_double_selected(self, _e=None):
        for x0, y0, x1, y1, s in self._hit:
            if self.selected is not None and s == tuple(self.selected):
                self._canvas_double(type("Ev", (), {"x": x0 + 1, "y": y0 + 1})())
        return "break"

    def _canvas_menu(self, e):
        if not self.doc:
            return
        slot = self.slot_at(e.x, e.y)
        if slot is None:
            return
        self.select(slot)
        data = self.doc.get(self.page_uuid, slot)
        m = theme.PopupMenu(self.root, width=250)
        if data and data.get("UUID") == C.FOLDER_UUID:
            m.add(_("Open folder"), lambda: self.open_folder((data.get("Settings") or {}).get("ProfileUUID")),
                  icon="folder")
            m.separator()
        m.add(_("Cut"), self.cut_selected if data else None, icon="close", hint=_("Ctrl+X"))
        m.add(_("Copy"), self.copy_selected if data else None, icon="layers", hint=_("Ctrl+C"))
        m.add(_("Paste"), self.paste_selected if self.clipboard else None, icon="import", hint=_("Ctrl+V"))
        m.separator()
        if len(self.doc.page_ids) > 1 and data:
            for i, pid in enumerate(self.doc.page_ids):
                if pid != self.nav[0] or len(self.nav) > 1:
                    m.add(_("Move to page {n}").format(n=i + 1), lambda p=pid: self._move_to_page(slot, p),
                          icon="arrow")
            m.separator()
        m.add(_("Delete"), self.delete_selected if data else None, icon="trash", danger=True, hint=_("Del"))
        return m.popup(e.x_root, e.y_root)

    # ------------------------------------------------------ Zwischenablage
    def copy_selected(self):
        if self.doc and self.selected:
            clip = self.doc.copy(self.page_uuid, self.selected)
            if clip:
                self.clipboard = clip
                self.status(_("Copied"))

    def cut_selected(self):
        self.copy_selected()
        if self.clipboard:
            self.delete_selected()

    def paste_selected(self):
        if not (self.doc and self.selected and self.clipboard):
            return
        if not self._fits(self.clipboard["data"], self.selected):
            return
        self.doc.paste(self.page_uuid, self.selected, self.clipboard)
        self.build_inspector()

    def delete_selected(self):
        if self.doc and self.selected and self.doc.get(self.page_uuid, self.selected):
            self.doc.remove(self.page_uuid, self.selected)
            self.build_inspector()

    # ============================================================ Katalog
    def _catalog(self):
        try:
            extra = C.plugin_types()
        except OSError:
            extra = []
        catalog = C.CATALOG + extra
        self._types = {a.uuid: a for a in catalog}
        dials = self.doc.geometry.dials if self.doc else 0
        return [a for a in catalog if (C.KEY in a.controllers or dials)
                and a.uuid != "com.elgato.streamdeck.multiactions.delay"]

    def fill_catalog(self, keep_scroll=False):
        """Aktionsliste: Kategorien zum Auf-/Zuklappen, darin je Aktion eine Zeile (wie die Deck-Software)."""
        q = self.search_var.get().strip().lower()
        avail = self._catalog()
        cats = list(dict.fromkeys(a.category for a in avail))
        if self._lib_open is None:
            self._lib_open = set(cats[:2])
        self.library.clear(keep_scroll)
        f = self.library.inner
        self._tiles_by_uuid = {}
        shown = False
        for cat in cats:
            items = [a for a in avail if a.category == cat
                     and (not q or q in a.name.lower() or q in a.description.lower() or q in cat.lower())]
            if not items:
                continue
            shown = True
            is_open = bool(q) or cat in self._lib_open  # beim Suchen alles Gefundene zeigen
            CategoryRow(f, self, cat, is_open, len(items)).pack(fill="x", pady=(2, 0))
            if not is_open:
                continue
            for a in items:
                row = ActionRow(f, self, a)
                row.pack(fill="x")
                if a.uuid == self._lib_selected:
                    row.selected = True
                self._tiles_by_uuid[a.uuid] = row
        if not shown:
            tk.Label(f, text=_("No matching actions"), bg=COL["side"], fg=COL["muted"],
                     font=theme.font(self.root, 9)).pack(anchor="w", padx=8, pady=6)

    def toggle_category(self, cat):
        if self._lib_open is None:
            self._lib_open = set()
        self._lib_open ^= {cat}
        self.fill_catalog(keep_scroll=True)  # an der Stelle bleiben – sonst springt die Liste nach oben

    def _lib_select(self, uuid):
        old = self._tiles_by_uuid.get(self._lib_selected)
        if old is not None and old.winfo_exists():
            old.set_selected(False)
        self._lib_selected = uuid
        t = self._tiles_by_uuid.get(uuid)
        if t is not None:
            t.set_selected(True)
        a = self._types.get(uuid)
        if a:
            where = _("dials only") if a.controllers == (C.DIAL,) else _("keys and dials") \
                if C.DIAL in a.controllers else _("keys")
            self.tree_hint.set(_("{name}: {description} ({where}).\nDrag onto a key or double-click.").format(
                name=a.name, description=a.description or a.category, where=where))

    def _lib_press(self, e, uuid):
        self._lib_select(uuid)
        self._tdrag = {"uuid": uuid, "x": e.x_root, "y": e.y_root, "win": None}

    def _lib_motion(self, e):
        d = getattr(self, "_tdrag", None)
        if not d or not d["uuid"] or not self.doc:
            return
        if d["win"] is None and abs(e.x_root - d["x"]) + abs(e.y_root - d["y"]) > 6:
            w = tk.Toplevel(self.root)
            w.overrideredirect(True)
            w.attributes("-topmost", True)
            tk.Label(w, text=self._types[d["uuid"]].name, bg=COL["accent"], fg=COL["on_accent"], padx=10,
                     pady=4).pack()
            d["win"] = w
        if d["win"] is not None:
            d["win"].geometry(f"+{e.x_root + 12}+{e.y_root + 8}")
            cx, cy = e.x_root - self.canvas.winfo_rootx(), e.y_root - self.canvas.winfo_rooty()
            self._show_drop_target(self.slot_at(cx, cy))

    def _lib_release(self, e):
        d, self._tdrag = getattr(self, "_tdrag", None), None
        if not d or d["win"] is None:
            return
        d["win"].destroy()
        self.canvas.delete("droptarget")
        cx, cy = e.x_root - self.canvas.winfo_rootx(), e.y_root - self.canvas.winfo_rooty()
        if self.root.winfo_containing(e.x_root, e.y_root) is not self.canvas:
            return
        slot = self.slot_at(cx, cy)
        if slot is not None:
            self.place_action(d["uuid"], slot)

    def _lib_double(self, uuid):
        """Doppelklick auf eine Aktion: auf die gewählte bzw. die nächste freie Taste legen."""
        if not uuid or not self.doc or uuid not in self._types:
            return "break"
        a = self._types[uuid]
        slot = self.selected
        if slot is None or not self._fits({"UUID": uuid}, slot, quiet=True):
            _g, keys, dials = self._slots()
            page = self.doc.page(self.page_uuid)
            pool = [s for s in keys + dials if s[0] in a.controllers]
            slot = next((s for s in pool if page.get(s) is None), None)
        if slot is None:
            self.status(_("No free space – select a key to replace it"))
            return "break"
        self.place_action(uuid, slot)
        return "break"

    def _fits(self, data, slot, quiet=False):
        t = self._types.get((data or {}).get("UUID", ""))
        if t and slot[0] not in t.controllers:
            if not quiet:
                msg = _("“{name}” only works on dials") if C.DIAL in t.controllers \
                    else _("“{name}” only works on keys")
                self.status(msg.format(name=t.name))
            return False
        return True

    def place_action(self, uuid, slot):
        if not self._fits({"UUID": uuid}, slot):
            return
        old = self.doc.get(self.page_uuid, slot)
        if old and old.get("UUID") == C.FOLDER_UUID and self.doc.page((old.get("Settings") or {}).get("ProfileUUID", "")):
            if not messagebox.askyesno(_("Replace folder"), _("The folder and its contents will be replaced. Continue?"),
                                       parent=self.root):
                return
        self.doc.place(self.page_uuid, slot, uuid)
        self.select(slot, redraw=True)
        self.status(_("{name} added").format(name=self._types[uuid].name))

    # ========================================================= Eigenschaften
    INSPECTOR_TABS = ("appearance", "action", "advanced")

    def set_inspector_tab(self, key):
        if key in self.INSPECTOR_TABS:
            self._insp_tab = key
            self.build_inspector()

    def build_inspector(self):
        self.inspector.clear()
        head = self.insp_head
        for c in head.winfo_children():
            c.destroy()
        f = self.inspector.inner
        if not self.doc:
            return
        pc = COL["panel"]
        if self.selected is None:
            self._insp_title(head, theme.icon_tile(self.root, "grid", 38, COL["raised"], fg=COL["muted"]),
                             _("Select a key to edit it."), self.doc.name)
            tk.Label(f, text=_("Tips: drag actions from the right onto keys · move keys by drag & drop "
                               "(also onto another page) · right-click to copy/paste · double-click "
                               "opens folders · Ctrl+Z undoes."), bg=pc, fg=COL["muted"], wraplength=620,
                     justify="left", anchor="w", font=theme.font(self.root, 9)).pack(anchor="w", fill="x")
            _autowrap(f.winfo_children()[-1])
            return
        data = self.doc.get(self.page_uuid, self.selected)
        where = _("Key {col},{row}").format(col=self.selected[1] + 1, row=self.selected[2] + 1) \
            if self.selected[0] == "key" else _("Dial {n}").format(n=self.selected[1] + 1)
        if data is None:
            self._insp_title(head, theme.icon_tile(self.root, "plus", 38, COL["raised"], fg=COL["muted"]),
                             _("{where} – empty").format(where=where), self.doc.name)
            tk.Label(f, text=_("Drag an action here from the list on the right, or double-click it there."),
                     bg=pc, fg=COL["muted"], anchor="w", font=theme.font(self.root, 9)).pack(anchor="w")
            if self.clipboard:
                theme.Button(f, _("Paste"), command=self.paste_selected).pack(anchor="w", pady=8)
            return
        form = ActionForm(self, f, data, self.page_uuid, self.selected, header=where)
        form.build_header(head)
        form.build_tab(self._insp_tab)

    def _insp_title(self, head, image, title, subtitle):
        pc = COL["panel"]
        lb = tk.Label(head, image=image, bg=pc)
        lb.image = image
        lb.pack(side="left", padx=(0, 12))
        box = tk.Frame(head, bg=pc)
        box.pack(side="left", fill="y")
        theme.label(box, title, 12, "bold", bg=pc, anchor="w").pack(anchor="w")
        theme.label(box, subtitle, 9, color=COL["muted"], bg=pc, anchor="w").pack(anchor="w")
        return box


class ActionForm:
    """Eigenschaften einer Aktion (auch für verschachtelte Aktionen im Dialog)."""

    def __init__(self, app: EditorApp, parent, data, page_uuid, slot, header="", appearance=True):
        self.app, self.parent, self.data = app, parent, data
        self.page_uuid, self.slot = page_uuid, slot
        self.header = header
        self.appearance = appearance
        self.doc = app.doc
        self.type = C.BY_UUID.get(data.get("UUID", ""))
        self.state_idx = int(data.get("State") or 0)
        self._icon_photo = None

    def _look(self):
        t = self.type or self.app._types.get(self.data.get("UUID", ""))
        return action_look(self.data.get("UUID", ""), t.category if t else "")

    # ------------------------------------------------------------ Aufbau
    def build_header(self, head):
        """Kopf der Eigenschaften im Hauptfenster: Symbol, Titel, Reiter."""
        glyph, color = self._look()
        st = (self.data.get("States") or [{}])[min(self.state_idx, len(self.data.get("States") or [{}]) - 1)]
        title = ((st or {}).get("Title") or "").strip().splitlines()
        name = C.display_name(self.data)
        self.app._insp_title(head, theme.icon_tile(self.app.root, glyph, 38, color), title[0] if title else name,
                             f"{name} · {self.header}")
        theme.Button(head, icon="trash", kind="ghost", command=self.app.delete_selected).pack(side="right",
                                                                                             padx=(8, 0))
        theme.SoftSegmented(head, [("appearance", _("Appearance"), None), ("action", _("Action"), None),
                                   ("advanced", _("Advanced"), None)], command=self.app.set_inspector_tab,
                            value=self.app._insp_tab).pack(side="right")

    def build_tab(self, tab):
        f = self.parent
        if tab == "appearance":
            self.app_frame = tk.Frame(f, bg=COL["panel"])
            self.app_frame.pack(fill="x")
            self.build_appearance()
        elif tab == "action":
            self._unsupported_note(f)
            shown = False
            if self.data.get("UUID") == C.FOLDER_UUID:
                theme.Button(f, _("Open folder"), icon="folder", command=lambda: self.app.open_folder(
                    (self.data.get("Settings") or {}).get("ProfileUUID"))).pack(anchor="w", pady=(0, 8))
                shown = True
            if self.data.get("UUID", "").startswith("com.lindeck.homeassistant."):
                from . import hassui
                hassui.connection_row(f, self.app)
            fields = self.type.fields_for(self.slot[0]) if self.type else []
            if fields:
                self.build_fields(f, fields)
                shown = True
            if self.type and self.type.children:
                self.build_children(f)
                shown = True
            if not shown and self._has_pi():
                theme.Button(f, _("Settings page…"), kind="primary", command=lambda: self.app.open_property_inspector(
                    self.page_uuid, self.slot)).pack(anchor="w")
                shown = True
            if not shown:
                tk.Label(f, text=_("This action has no further settings."), bg=COL["panel"], fg=COL["muted"],
                         font=theme.font(self.app.root, 9)).pack(anchor="w")
        else:
            self._unsupported_note(f)
            grid = tk.Frame(f, bg=COL["panel"])
            grid.pack(fill="x")
            grid.columnconfigure(1, weight=1)
            ttk.Label(grid, text=_("Action"), style="Panel.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 16),
                                                                         pady=4)
            ttk.Label(grid, text=self.data.get("UUID", ""), style="Muted.TLabel").grid(row=0, column=1, sticky="w")
            row = tk.Frame(f, bg=COL["panel"])
            row.pack(fill="x", pady=(8, 0))
            if self._has_pi():
                theme.Button(row, _("Settings page…"), kind="primary", command=lambda: self.app.open_property_inspector(
                    self.page_uuid, self.slot)).pack(side="left", padx=(0, 8))
            theme.Button(row, _("Settings (JSON)…"), command=self.edit_json).pack(side="left", padx=(0, 8))
            theme.Button(row, _("Delete"), icon="trash", kind="danger", command=self.app.delete_selected).pack(
                side="left")

    def _unsupported_note(self, f):
        if not A.is_supported(self.data.get("UUID", "")):
            plugin = (self.data.get("Plugin") or {}).get("Name") or self.data.get("UUID", "").rsplit(".", 1)[0]
            ttk.Label(f, text=_("Action of the plugin “{plugin}” – cannot run on Linux (plugin not installed "
                                "or only for Windows/macOS). Title, icon and settings are kept.").format(
                plugin=plugin),
                      style="Muted.TLabel", wraplength=620, justify="left").pack(anchor="w", fill="x", pady=(0, 8))
            _autowrap(f.winfo_children()[-1])

    def build(self):
        """Alles auf einmal (Dialog für verschachtelte Aktionen)."""
        f = self.parent
        head = ttk.Frame(f, style="Panel.TFrame")
        head.pack(fill="x")
        glyph, color = self._look()
        img = theme.icon_tile(self.app.root, glyph, 34, color)
        tk.Label(head, image=img, bg=COL["panel"]).pack(side="left", padx=(0, 10))
        ttk.Label(head, text=C.display_name(self.data), style="Head.TLabel").pack(side="left")
        if self.header:
            ttk.Label(head, text="  " + self.header, style="Muted.TLabel").pack(side="left")
        theme.Button(head, _("Settings (JSON)…"), command=self.edit_json).pack(side="right")
        ttk.Label(f, text=self.data.get("UUID", ""), style="Muted.TLabel").pack(anchor="w", pady=(4, 0))
        self._unsupported_note(f)
        fields = self.type.fields_for(self.slot[0]) if self.type else []
        if fields:
            self._section(f, _("SETTINGS"))
            self.build_fields(f, fields)
        if self.type and self.type.children:
            self.build_children(f)

    def build_fields(self, f, fields):
        """Felder zweispaltig: kurze Felder nebeneinander, breite über die ganze Zeile."""
        grid = tk.Frame(f, bg=COL["panel"])
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)
        row = col = 0
        for fld in fields:
            wide = fld.kind in ("multiline", "hotkey", "file", "profile", "entity") or (
                fld.kind == "text" and fld.hint)
            if wide and col:
                row, col = row + 1, 0
            self.build_field(grid, row, fld, col=col, span=3 if wide else 1)
            if wide or col:
                row, col = row + 1, 0
            else:
                col = 2
    def _has_pi(self):
        from . import plugins
        info = plugins.plugin_for_action(self.data.get("UUID", ""))
        if not info or not info.runnable:
            return False
        meta = info.actions.get(self.data.get("UUID", "")) or {}
        return bool(meta.get("PropertyInspectorPath") or info.manifest.get("PropertyInspectorPath"))

    def _section(self, f, text):
        ttk.Label(f, text=text, style="Section.TLabel").pack(anchor="w", pady=(14, 4))

    def _changed_visual(self):
        self.app.redraw()

    # ------------------------------------------------------- Darstellung
    def build_appearance(self):
        """Titel, Schrift, Farbe, Position und Symbol – zweispaltig."""
        from .dialogkit import trace
        f = self.app_frame
        for c in f.winfo_children():
            c.destroy()
        states = self.data.get("States") or [{}]
        self.state_idx = min(self.state_idx, len(states) - 1)
        st = states[self.state_idx] if states else {}
        f.columnconfigure(1, weight=1, uniform="v")
        f.columnconfigure(3, weight=1, uniform="v")

        def lab(text, r, c, sticky="w"):
            ttk.Label(f, text=text, style="Muted.TLabel").grid(row=r, column=c, sticky=sticky,
                                                               padx=(0 if c == 0 else 24, 14), pady=4)
        # links: Titel, Schrift, Symbol
        lab(_("Title"), 0, 0, "nw")
        title = st.get("Title") or ""
        txt = tk.Text(f, height=2 if "\n" in title else 1, width=24, bg=COL["panel2"], fg=COL["fg"], insertbackground=COL["fg"],
                      relief="flat", font=theme.font(self.app.root, 10), wrap="none", highlightthickness=1,
                      highlightbackground=COL["border"], highlightcolor=COL["accent"], padx=6, pady=4)
        txt.insert("1.0", title)
        txt.grid(row=0, column=1, sticky="ew", pady=4)
        txt.bind("<KeyRelease>", lambda e: self._set_state("Title", txt.get("1.0", "end-1c")))
        lab(_("Font"), 1, 0)
        frow = tk.Frame(f, bg=COL["panel"])
        frow.grid(row=1, column=1, sticky="ew", pady=4)
        sty = ttk.Combobox(frow, state="readonly", width=11, values=[n for _v, n in STYLES])
        cur = (st.get("FontStyle") or "").replace("Regular", "")
        sty.current(next((i for i, (v, _n) in enumerate(STYLES) if v.lower() == cur.lower()), 0))
        sty.bind("<<ComboboxSelected>>", lambda e: self._set_state("FontStyle", STYLES[sty.current()][0]))
        sty.pack(side="left", fill="x", expand=True)
        size = tk.StringVar(value=str(st.get("FontSize") or 12))
        ttk.Spinbox(frow, from_=6, to=40, width=4, textvariable=size).pack(side="left", padx=(8, 0))
        trace(frow, size, lambda: self._set_int_state("FontSize", size.get()))
        lab(_("Icon"), 2, 0)
        irow = tk.Frame(f, bg=COL["panel"])
        irow.grid(row=2, column=1, sticky="w", pady=4)
        prev = E.preview_action(self.doc, self.page_uuid, self.data, (36, 36), state=self.state_idx) \
            if self.slot[0] == "key" else E.preview_dial(self.doc, self.page_uuid, self.data, (72, 36))
        self._icon_photo = ImageTk.PhotoImage(_rounded(prev.convert("RGBA"), 7), master=self.app.root)
        pv = tk.Label(irow, image=self._icon_photo, bg=COL["panel"], cursor="hand2")
        pv.pack(side="left")
        pv.bind("<Button-1>", lambda e: self._icon_library())
        # Bibliothek mit vordefinierten Icons; „Eigenes Bild …“ steckt darin
        theme.Button(irow, _("Icons…"), "grid", command=self._icon_library).pack(side="left", padx=(8, 0))
        b = theme.Button(irow, _("Default"), kind="ghost", command=lambda: self._set_state("Image", "", rebuild=True))
        b.pack(side="left", padx=(4, 0))
        if not st.get("Image"):
            b.configure_text(enabled=False)
        # Deckkraft: das Hintergrundbild der Seite scheint durch das Symbol (FreeWave-Tasten: ihr Hintergrund)
        lab(_("Opacity"), 3, 0)
        orow = tk.Frame(f, bg=COL["panel"])
        orow.grid(row=3, column=1, sticky="ew", pady=4)
        try:
            op = max(0, min(100, int(st.get("IconOpacity", 100))))
        except (TypeError, ValueError):
            op = 100
        op_val = tk.StringVar(f, value=f"{op} %")
        fd = theme.Fader(orow, length=150, thickness=18, orient="horizontal", value=op,
                         command=lambda v: self._set_opacity(v, op_val))
        fd.pack(side="left")
        ttk.Label(orow, textvariable=op_val, style="Panel.TLabel", width=6).pack(side="left", padx=(8, 0))
        # rechts: Zustand, Titel zeigen + Position, Farbe
        r = 0
        if len(states) > 1:
            lab(_("State"), r, 2)
            sv = ttk.Combobox(f, state="readonly", width=12,
                              values=[_("State {n}").format(n=i + 1) for i in range(len(states))])
            sv.current(self.state_idx)
            sv.grid(row=r, column=3, sticky="w", pady=4)
            sv.bind("<<ComboboxSelected>>", lambda e: (setattr(self, "state_idx", sv.current()),
                                                       self.build_appearance()))
            r += 1
        lab(_("Show title"), r, 2)
        srow = tk.Frame(f, bg=COL["panel"])
        srow.grid(row=r, column=3, sticky="w", pady=4)
        show = theme.Switch(srow, command=lambda: self._set_state("ShowTitle", show.get()))
        show.set(st.get("ShowTitle", True) is not False)
        show.pack(side="left")
        al = ttk.Combobox(srow, state="readonly", width=8, values=[n for _v, n in ALIGN])
        cur = (st.get("TitleAlignment") or "bottom").lower()
        al.current(next((i for i, (v, _n) in enumerate(ALIGN) if v == cur), 2))
        al.bind("<<ComboboxSelected>>", lambda e: self._set_state("TitleAlignment", ALIGN[al.current()][0]))
        al.pack(side="left", padx=(10, 0))
        r += 1
        lab(_("Title color"), r, 2)
        crow = tk.Frame(f, bg=COL["panel"])
        crow.grid(row=r, column=3, sticky="w", pady=4)
        color = st.get("TitleColor") or "#ffffff"
        cb = tk.Button(crow, text="  ", bg=color, activebackground=color, relief="flat", width=3, bd=0,
                       highlightthickness=1, highlightbackground=COL["border"], cursor="hand2",
                       command=lambda: self._pick_color(cb))
        cb.pack(side="left", ipady=2)
        und = theme.Switch(crow, command=lambda: self._set_state("FontUnderline", und.get()))
        und.set(bool(st.get("FontUnderline")))
        und.pack(side="left", padx=(14, 8))
        ttk.Label(crow, text=_("underline"), style="Panel.TLabel").pack(side="left")

    def _set_state(self, key, value, rebuild=False):
        self.doc.update_state(self.page_uuid, self.slot, self.state_idx, key, value, action=self.data)
        if rebuild:
            self.build_appearance()

    def _set_opacity(self, value, var):
        """Deckkraft für alle Zustände der Taste (gesammelt beim Ziehen, damit nicht jeder Pixel speichert)."""
        v = max(0, min(100, int(round(float(value)))))
        var.set(f"{v} %")
        job = getattr(self, "_opacity_job", None)
        if job:
            self.app.root.after_cancel(job)

        def apply():
            # ein Rückgängig-Schritt und ein Neuzeichnen für alle Zustände (update_state je Zustand gäbe
            # mehrere, und Strg+Z ließe die Zustände halb geändert zurück)
            self._opacity_job = None
            self.doc.checkpoint(("state-all", self.page_uuid, tuple(self.slot), id(self.data), "IconOpacity"))
            states = self.data.setdefault("States", [])
            if not states:
                states.append(C.default_state())
            for st in states:
                st["IconOpacity"] = v
            self.doc.changed()
        self._opacity_job = self.app.root.after(60, apply)

    def _set_int_state(self, key, text):
        try:
            v = int(text)
        except ValueError:
            return
        if 4 <= v <= 80:
            self._set_state(key, v)

    def _pick_color(self, btn):
        states = self.data.get("States") or [{}]
        cur = states[self.state_idx].get("TitleColor") or "#ffffff"
        res = colorchooser.askcolor(color=cur, parent=self.app.root, title=_("Title color"))
        if res and res[1]:
            btn.configure(bg=res[1], activebackground=res[1])
            self._set_state("TitleColor", res[1])

    def _icon_library(self):
        from .iconlibui import IconLibrary
        IconLibrary(self.app.root, on_pick=self._set_library_icon, on_file=self._choose_icon)

    def _set_library_icon(self, name, style):
        """Symbol aus der Icon-Bibliothek als Tastenbild übernehmen."""
        import tempfile
        from . import iconlib
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/{name}.png"
            iconlib.render(name, style).save(path)
            try:
                self.doc.set_icon(self.page_uuid, self.slot, self.state_idx, path, action=self.data)
            except E.EditError as e:
                messagebox.showerror(_("Icon"), str(e), parent=self.app.root)
                return
        self.build_appearance()

    def _choose_icon(self):
        f = filedialog.askopenfilename(parent=self.app.root, title=_("Choose icon"),
                                       filetypes=[(_("Images"), "*.png *.jpg *.jpeg *.gif *.svg *.webp *.bmp"),
                                                  (_("All files"), "*")])
        if not f:
            return
        try:
            self.doc.set_icon(self.page_uuid, self.slot, self.state_idx, f, action=self.data)
        except E.EditError as e:
            messagebox.showerror(_("Icon"), str(e), parent=self.app.root)
            return
        self.build_appearance()

    # ------------------------------------------------------------ Felder
    def _set(self, key, value):
        self.doc.update_settings(self.page_uuid, self.slot, key, value, action=self.data)

    def build_field(self, grid, row, fld: C.Field, col=0, span=1):
        # Beobachtung der Felder endet mit ihrer Zelle – sonst hielte Tcl jedes ersetzte Formular fest
        from .dialogkit import trace
        s = self.data.setdefault("Settings", {})
        ttk.Label(grid, text=fld.label, style="Muted.TLabel").grid(row=row, column=col, sticky="nw",
                                                                   padx=(0 if col == 0 else 24, 14), pady=4)
        cell = ttk.Frame(grid, style="Panel.TFrame")
        cell.grid(row=row, column=col + 1, columnspan=span, sticky="ew", pady=4)
        k = fld.key
        if fld.kind in ("text", "file"):
            var = tk.StringVar(value=str(s.get(k) or ""))
            ent = ttk.Entry(cell, textvariable=var)
            ent.pack(side="left", fill="x", expand=True)
            trace(cell, var, lambda: self._set(k, var.get()))
            if fld.kind == "file":
                ttk.Button(cell, text="…", width=3, command=lambda: self._browse(var)).pack(side="left", padx=(4, 0))
            if fld.hint:
                ttk.Label(cell, text=fld.hint, style="Muted.TLabel").pack(side="left", padx=(8, 0))
        elif fld.kind == "multiline":
            txt = tk.Text(cell, height=4, bg=COL["panel2"], fg=COL["fg"], insertbackground=COL["fg"], relief="flat",
                          font=theme.font(self.app.root, 10), highlightthickness=1, highlightbackground=COL["border"],
                          highlightcolor=COL["accent"], padx=6, pady=4)
            txt.insert("1.0", str(s.get(k) or ""))
            txt.pack(fill="x", expand=True)
            txt.bind("<KeyRelease>", lambda e: self._set(k, txt.get("1.0", "end-1c")))
        elif fld.kind == "int":
            var = tk.StringVar(value=str(s.get(k) if s.get(k) is not None else fld.min))
            ttk.Spinbox(cell, from_=fld.min, to=fld.max, textvariable=var, width=10).pack(side="left")

            def on_int(var=var):
                try:
                    v = int(var.get())
                except ValueError:
                    return
                if fld.min <= v <= fld.max:
                    self._set(k, v)
            trace(cell, var, on_int)
        elif fld.kind == "bool":
            var = tk.BooleanVar(value=bool(s.get(k)) if k in s else True if k == "bring_to_front" else False)
            ttk.Checkbutton(cell, variable=var, command=lambda: self._set(k, var.get())).pack(side="left")
        elif fld.kind == "choice":
            vals = list(fld.options() if callable(fld.options) else fld.options)
            if s.get(k) not in (None, "") and all(str(v) != str(s.get(k)) for v, _n in vals):
                vals = [(s.get(k), str(s.get(k)))] + [(v, n) for v, n in vals if v != ""]  # z. B. gelöschte Szene
            box = ttk.Combobox(cell, state="readonly", width=28, values=[n for _v, n in vals])
            cur = s.get(k)
            idx = next((i for i, (v, _n) in enumerate(vals) if str(v) == str(cur)), 0)
            box.current(idx)
            box.bind("<<ComboboxSelected>>", lambda e: self._set(k, vals[box.current()][0]))
            box.pack(side="left")
        elif fld.kind == "page":
            n = len(self.doc.page_ids)
            box = ttk.Combobox(cell, state="readonly", width=12, values=[_("Page {n}").format(n=i + 1) for i in range(n)])
            try:
                box.current(max(0, min(n - 1, int(s.get(k) or 1) - 1)))
            except ValueError:
                box.current(0)
            box.bind("<<ComboboxSelected>>", lambda e: self._set(k, box.current() + 1))
            box.pack(side="left")
        elif fld.kind == "profile":
            profs = sorted(self.app.store.profiles.values(), key=lambda p: p.name.lower())
            ids = [""] + [p.id for p in profs]
            box = ttk.Combobox(cell, state="readonly", width=34,
                               values=[_("(next profile)")] + [f"{p.name}  ·  {p.model_name}" for p in profs])
            cur = (s.get(k) or "").lower()
            box.current(next((i for i, p in enumerate(ids) if p.lower() == cur), 0))
            box.bind("<<ComboboxSelected>>", lambda e: self._set(k, ids[box.current()]))
            box.pack(side="left")
        elif fld.kind == "hotkey":
            HotkeyField(cell, self, int(k)).pack(side="left", fill="x", expand=True)
        elif fld.kind == "entity":
            from . import hassui
            hassui.entity_field(cell, self, k, fld)

    def _browse(self, var):
        f = filedialog.askopenfilename(parent=self.app.root, title=_("Choose file"))
        if f:
            var.set(f)

    # ----------------------------------------------------- Kindaktionen
    def build_children(self, f):
        kind, labels = self.type.children
        entries = C.child_lists(self.data)
        self._section(f, _("INCLUDED ACTIONS"))
        box = ttk.Frame(f, style="Panel.TFrame")
        box.pack(fill="x")
        if kind == "slots":
            for i, label in enumerate(labels):
                row = ttk.Frame(box, style="Panel.TFrame")
                row.pack(fill="x", pady=2)
                ttk.Label(row, text=label, style="Panel.TLabel", width=16).pack(side="left")
                child = entries[i] if isinstance(entries[i], dict) and entries[i].get("UUID") else None
                ttk.Label(row, text=C.display_name(child) if child else "–", style="Panel.TLabel",
                          width=24).pack(side="left")
                mb = ttk.Menubutton(row, text=_("Choose"))
                mb["menu"] = self._catalog_menu(mb, lambda u, i=i: self._set_slot_child(i, u))
                mb.pack(side="left", padx=2)
                b1 = ttk.Button(row, text=_("Edit…"), command=lambda c=child: self._edit_child(c))
                b1.pack(side="left", padx=2)
                b2 = ttk.Button(row, text=_("Clear"), command=lambda i=i: self._set_slot_child(i, None))
                b2.pack(side="left", padx=2)
                if child is None:
                    b1.state(["disabled"])
                    b2.state(["disabled"])
            return
        for g, label in enumerate(labels):
            group = entries[g]["Actions"]
            ttk.Label(box, text=label, style="Muted.TLabel").pack(anchor="w", pady=(6, 2))
            row = ttk.Frame(box, style="Panel.TFrame")
            row.pack(fill="x")
            lb = tk.Listbox(row, height=max(3, min(8, len(group))), bg=COL["panel2"], fg=COL["fg"],
                            selectbackground=COL["accent"], relief="flat", highlightthickness=0, activestyle="none",
                            exportselection=False)
            for a in group:
                lb.insert("end", self._child_label(a))
            lb.pack(side="left", fill="x", expand=True, anchor="n")
            col = ttk.Frame(row, style="Panel.TFrame")
            col.pack(side="left", padx=(6, 0), anchor="n")
            mb = ttk.Menubutton(col, text=_("Add"))
            mb["menu"] = self._catalog_menu(mb, lambda u, grp=group: self._add_child(grp, u),
                                            with_delay=self.data.get("UUID", "").startswith(
                                                "com.elgato.streamdeck.multiactions"))
            mb.pack(fill="x", pady=1)

            def sel(lb=lb):
                s = lb.curselection()
                return s[0] if s else None
            ttk.Button(col, text=_("Edit…"), command=lambda grp=group, sel=sel: self._edit_child(
                grp[sel()] if sel() is not None else None)).pack(fill="x", pady=1)
            ttk.Button(col, text=_("Remove"), command=lambda grp=group, sel=sel: self._remove_child(grp, sel())
                       ).pack(fill="x", pady=1)
            ttk.Button(col, text="▲", command=lambda grp=group, sel=sel: self._move_child(grp, sel(), -1)
                       ).pack(fill="x", pady=1)
            ttk.Button(col, text="▼", command=lambda grp=group, sel=sel: self._move_child(grp, sel(), 1)
                       ).pack(fill="x", pady=1)
            lb.bind("<Double-Button-1>", lambda e, grp=group, sel=sel: self._edit_child(
                grp[sel()] if sel() is not None else None))

    def _child_label(self, a):
        name = C.display_name(a)
        st = (a.get("States") or [{}])[0]
        if a.get("UUID") == "com.elgato.streamdeck.multiactions.delay":
            return f"⏱ {name}: {(a.get('Settings') or {}).get('delay', 0)} ms"
        if a.get("UUID") == "com.elgato.streamdeck.system.hotkey":
            hk = ((a.get("Settings") or {}).get("Hotkeys") or [None])[0]
            return f"{name}: {C.format_hotkey(hk) or '–'}"
        return f"{name}{(': ' + st.get('Title')) if st.get('Title') else ''}"

    def _catalog_menu(self, master, cb, with_delay=False):
        m = tk.Menu(master, tearoff=0)
        kind = C.DIAL if self.data.get("UUID") in ("com.elgato.streamdeck.dial.stack",) else C.KEY
        for cat in C.CATEGORIES:
            sub = tk.Menu(m, tearoff=0)
            n = 0
            for a in C.CATALOG:
                if a.category != cat or kind not in a.controllers or a.uuid == C.FOLDER_UUID or a.children:
                    continue
                if a.uuid == "com.elgato.streamdeck.multiactions.delay" and not with_delay:
                    continue
                sub.add_command(label=a.name, command=lambda u=a.uuid: cb(u))
                n += 1
            if n:
                m.add_cascade(label=cat, menu=sub)
        return m

    def _rebuild(self):
        self.app.build_inspector() if self.appearance else self._dialog_rebuild()

    def _dialog_rebuild(self):
        for c in self.parent.winfo_children():
            c.destroy()
        self.build()

    def _add_child(self, group, uuid):
        self.doc.checkpoint()
        group.append(C.new_action(uuid))
        self.doc.changed()
        self._rebuild()

    def _set_slot_child(self, i, uuid):
        self.doc.checkpoint()
        C.child_lists(self.data)[i] = C.new_action(uuid) if uuid else {}
        self.doc.changed()
        self._rebuild()

    def _remove_child(self, group, idx):
        if idx is None:
            return
        self.doc.checkpoint()
        group.pop(idx)
        self.doc.changed()
        self._rebuild()

    def _move_child(self, group, idx, delta):
        if idx is None or not (0 <= idx + delta < len(group)):
            return
        self.doc.checkpoint()
        group.insert(idx + delta, group.pop(idx))
        self.doc.changed()
        self._rebuild()

    def _edit_child(self, child):
        if not child:
            return
        kind = C.DIAL if self.data.get("UUID") == "com.elgato.streamdeck.dial.stack" else C.KEY
        ChildDialog(self.app, child, self.page_uuid, (kind, *self.slot[1:]), on_close=self._rebuild)

    # -------------------------------------------------------------- JSON
    def edit_json(self):
        JsonDialog(self.app, self.data, self.page_uuid, self.slot, on_close=self._rebuild)


class HotkeyField(ttk.Frame):
    """Feld zum Aufnehmen einer Tastenkombination."""

    def __init__(self, master, form: ActionForm, idx: int):
        super().__init__(master, style="Panel.TFrame")
        self.form, self.idx = form, idx
        self.var = tk.StringVar(value=self._current() or "")
        self.entry = ttk.Entry(self, textvariable=self.var, width=14)
        self.entry._hotkey = True
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<FocusIn>", lambda e: self.var.set(_("Press keys…")))
        self.entry.bind("<FocusOut>", lambda e: self.var.set(self._current() or ""))
        self.entry.bind("<KeyPress>", self._key)
        ttk.Button(self, text="✕", width=3, command=self.clear).pack(side="left", padx=(4, 0))
        ttk.Button(self, text=_("Type in…"), command=self.type_in).pack(side="left", padx=(4, 0))

    def _current(self):
        hks = (self.form.data.get("Settings") or {}).get("Hotkeys") or []
        return C.format_hotkey(hks[self.idx]) if self.idx < len(hks) else ""

    def _key(self, e):
        hk = C.hotkey_from_tk(e.keysym, e.char, e.state, e.keycode)
        if hk is None:
            return "break"
        self._store(hk)
        return "break"

    def _store(self, hk):
        f = self.form
        f.doc.set_hotkey(f.page_uuid, f.slot, self.idx, hk, action=f.data)
        self.var.set(C.format_hotkey(hk))
        self.form.app.root.focus_set()

    def clear(self):
        self._store(C.empty_hotkey())

    def type_in(self):
        text = simpledialog.askstring(_("Key combination"), _("e.g. Ctrl+Shift+T, Alt+F4, Super+E, F13:"),
                                      initialvalue=self._current(), parent=self.form.app.root)
        if text is None:
            return
        try:
            self._store(C.parse_hotkey(text))
        except ValueError as e:
            messagebox.showerror(_("Key combination"), str(e), parent=self.form.app.root)


class _Dialog(tk.Toplevel):
    def __init__(self, app, title, size="640x520"):
        super().__init__(app.root)
        self.app = app
        self.title(title)
        self.configure(bg=COL["panel"])
        self.geometry(size)
        self.transient(app.root)
        self.bind("<Escape>", lambda e: self.done())
        self.protocol("WM_DELETE_WINDOW", self.done)

    def done(self):
        self.destroy()


class ChildDialog(_Dialog):
    def __init__(self, app, data, page_uuid, slot, on_close=None):
        super().__init__(app, _("Edit {name}").format(name=C.display_name(data)))
        self.on_close = on_close
        body = ScrollFrame(self, bg=COL["panel"], padding=12)
        body.pack(fill="both", expand=True)
        ActionForm(app, body.inner, data, page_uuid, slot, appearance=False).build()
        ttk.Button(self, text=_("Done"), style="Accent.TButton", command=self.done).pack(anchor="e", padx=12,
                                                                                       pady=(0, 12))

    def done(self):
        super().done()
        if self.on_close:
            self.on_close()


class JsonDialog(_Dialog):
    def __init__(self, app, data, page_uuid, slot, on_close=None):
        super().__init__(app, _("Settings (JSON)"))
        self.data, self.page_uuid, self.slot, self.on_close = data, page_uuid, slot, on_close
        ttk.Label(self, text=_("Raw data of the action (“Settings”) – for plugin actions and special cases."),
                  style="Muted.TLabel").pack(anchor="w", padx=12, pady=(12, 4))
        self.txt = tk.Text(self, bg=COL["panel2"], fg=COL["fg"], insertbackground=COL["fg"], relief="flat",
                           font=("monospace", 10), highlightthickness=1, highlightbackground=COL["border"],
                           highlightcolor=COL["accent"])
        self.txt.insert("1.0", json.dumps(data.get("Settings") or {}, indent=2, ensure_ascii=False))
        self.txt.pack(fill="both", expand=True, padx=12)
        row = ttk.Frame(self, style="Panel.TFrame")
        row.pack(fill="x", padx=12, pady=12)
        ttk.Button(row, text=_("Apply"), style="Accent.TButton", command=self.apply).pack(side="right")
        ttk.Button(row, text=_("Cancel"), command=self.done).pack(side="right", padx=6)

    def apply(self):
        try:
            val = json.loads(self.txt.get("1.0", "end-1c") or "{}")
            if not isinstance(val, dict):
                raise ValueError(_("A JSON object { … } is expected"))
        except ValueError as e:
            messagebox.showerror("JSON", str(e), parent=self)
            return
        self.app.doc.set_settings_json(self.page_uuid, self.slot, val, action=self.data)
        self.done()
        if self.on_close:
            self.on_close()


class NewProfileDialog(theme.Dialog):
    """Neues Profil: nur der Name – das Gerät erkennt FreeDeck selbst (angeschlossen oder zuletzt benutzt)."""

    def __init__(self, app_root, model, plugged=True, wait=True):
        from .dialogkit import entry, field, footer, header
        super().__init__(app_root, _("New profile"), width=520)
        self.result = None
        b = self.body
        header(b, _("New profile"), _("For the connected device") if plugged else
               _("No device connected – the profile is for the model used last"), icon="plus")
        self.name = tk.StringVar(self, value=_("New profile"))
        field(b, _("Name"))
        self.entry = entry(b, self.name)
        self.entry.box.pack(fill="x", pady=(0, 14))
        field(b, _("DEVICE – DETECTED AUTOMATICALLY") if plugged else _("DEVICE"))
        geo = model_geometry(model)
        sub = ((_("connected") + " · ") if plugged else "") + (
            _("{cols} × {rows} keys").format(cols=geo.cols, rows=geo.rows) if geo else "")
        if geo and geo.dials:
            sub += " · " + ngettext("{n} dial", "{n} dials", geo.dials).format(n=geo.dials)
        theme.InfoCard(b, model_name(model), sub,
                       image=theme.icon_tile(self, "deck", 34, COL["device"], radius=0.24, fg="#c9cfda"),
                       dot=COL["ok"] if plugged else None).pack(fill="x")
        footer(self)
        theme.Button(self.buttons, _("Create"), icon="plus", kind="primary", command=self.ok).pack(side="right")
        theme.Button(self.buttons, _("Cancel"), command=self.destroy).pack(side="right", padx=8)
        self.bind("<Return>", lambda ev: self.ok())
        self.entry.focus_set()
        self.entry.select_range(0, "end")
        self.show()
        if wait:
            self.wait_visibility()
            self.grab_set()
            self.wait_window()

    def ok(self):
        self.result = self.name.get().strip() or _("New profile")
        self.destroy()


def _shape(model):
    """Tastenraster eines Modells – gleiche Raster gelten als dasselbe Gerät (z. B. XL und XL V2)."""
    g = model_geometry(model) if model else None
    return (g.cols, g.rows, g.dials) if g else None


# --------------------------------------------------------------------------
# Zeichenhilfen
# --------------------------------------------------------------------------
def _round_rect(cv, x0, y0, x1, y1, r, **kw):
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
           x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return cv.create_polygon(pts, smooth=True, **kw)


def _rounded(img: Image.Image, r: int) -> Image.Image:
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, img.size[0] - 1, img.size[1] - 1), radius=r, fill=255)
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def main(profile=None, store_root=None):
    try:
        root = tk.Tk(className="freedeck-editor")
    except tk.TclError as e:
        print(_("No graphical display available: {error}").format(error=e))
        return 1
    EditorApp(root, store_root=store_root, profile=profile)
    root.mainloop()
    return 0
