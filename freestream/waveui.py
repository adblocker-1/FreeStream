"""Fenster von FreeWave: Mixer (Monitor-/Stream-Mix je Kanal), Mikrofon, Effekte und Programme.

Aufbau (FreeStream-Design): links die Seitenleiste mit Mikrofon, den Ansichten
(Mixer, Mikrofon, Effekte, Programme, Einrichtung) und den beiden Ausgängen;
rechts die gewählte Ansicht. Im Mixer hat jeder Kanal eine Karte mit zwei
Reglern – Cyan für den Monitor-Mix (was du hörst), Violett für den Stream-Mix
(was dein Publikum hört) – samt Pegelanzeige, Stumm-Knöpfen und Effekten.

Alle Zugriffe auf PipeWire (pactl) und das Mikrofon (USB) laufen in einem
eigenen Arbeits-Thread, damit das Fenster nie hängt. Ergebnisse werden im
Tk-Thread übernommen; Regler, die gerade bewegt werden, überschreibt die
Aktualisierung nicht. Die Pegel kommen aus levels.py (parec) und laufen nur,
solange der Mixer sichtbar ist.

Mixer-Szenen (Knopf „Szenen“ im Kopf des Mixers), Auto-Ducking und globale
Tastenkürzel (Ansicht Mikrofon) kommen aus mixer.py, ducking.py und shortcuts.py.
"""
import logging
import queue
import threading
import time
import tkinter as tk

from PIL import Image, ImageDraw, ImageTk

from . import ducking, levels, parts, shortcuts, soundboard, system, theme, wavemic
from .dialogkit import ScrollArea
from .i18n import _, ngettext
from .parts import has_freedeck
from .theme import (COL, Button, Card, Chip, Fader, HScroll, InfoCard, LevelMeter, NavItem, PopupMenu, RoundToggle,
                    Segmented, Sidebar, SoftSegmented, Switch, icon_tile, label, rounded_rect)

log = logging.getLogger("freestream.waveui")
REFRESH_MS = 1500
REFRESH_IDLE_MS = 5000  # mit PipeWire-Ereignissen: ohne Änderung nur alle 5 s (Mikrofon, Ducking, Geräte)
LEVEL_MS = 40  # 25 Hz
TOUCH_HOLD = 2.5
MUTE_ICONS = {"monitor": ("headphones", "headphones_off"), "stream": ("stream", "stream_off")}
NOT_IN_MIXER = _("– not in mixer –")
CHANNEL_LOOK = theme.CHANNEL_LOOK
CH_W, MIX_W, ROW_H, HEAD_H = 360, 194, 64, 72
METER_INSET = 16  # Pegel unter einem Regler: so viel schmaler wie dessen Spur (Knopf 18 px, Spur 6 px)
MIX_W_MIN, MIX_W_MAX = 176, 300  # Mix-Spalten füllen die Fensterbreite in diesen Grenzen
CLIP_DB, CLIP_HOLD = -1.0, 1.5  # Mix-Pegel ab −1 dB: „CLIP“ für 1,5 s  # Mixer-Tabelle: Kanalspalte, Mix-Spalten, Zeilenhöhe
MIC_SHORTCUT = "linewave-mic-mute"
SCENE_SHORTCUT = "linewave-scene-"
DUCK_MS = 80
DRAG_STALE = 10  # s ohne Mausbewegung: Ziehen gilt als abgebrochen (Loslassen ging verloren)


def mix_color(mix):
    return COL["monitor"] if mix == "monitor" else COL["stream"]


channel_look = theme.channel_look


post_fader = levels.post_fader


def _fit_text(widget, fnt, text, width):
    """Text auf width Pixel kürzen; fnt wie theme.font() – gemessen wird mit der geladenen Schrift."""
    return theme.elide(widget, text, fnt, width)


# ============================================================ kleine Bausteine
def _sliders_image(size, color):
    """Symbol „Regler“ (drei Schieber) für den Eintrag Mixer."""
    k = 4
    S = size * k
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    c = theme.rgb(color) + (255,)
    lw = max(1, int(S * 0.09))
    for x, y in ((0.22, 0.62), (0.5, 0.34), (0.78, 0.56)):
        X = S * x
        d.line((X, S * 0.1, X, S * 0.9), fill=c, width=lw)
        d.rounded_rectangle((X - S * 0.12, S * y - S * 0.07, X + S * 0.12, S * y + S * 0.07), radius=S * 0.05,
                            fill=c)
    return im.resize((size, size), Image.LANCZOS)


class _NavIcons(theme.Icons):
    """Wie theme.Icons, zusätzlich mit dem Symbol „sliders“."""

    def get(self, name, size=20, color=None, circle=None):
        if name != "sliders":
            return super().get(name, size, color, circle)
        color = color or COL["fg"]
        key = (name, size, color)
        if key not in self._cache:
            self._cache[key] = ImageTk.PhotoImage(_sliders_image(size, color), master=self.master)
        return self._cache[key]


class MuteToggle(RoundToggle):
    """Stumm-Knopf der Kanal-Karten: schlichtes Symbol (beim Überfahren hinterlegt); stumm = rot."""

    W, H = 34, 30

    def __init__(self, master, icon_off, icon_on, command=None, width=None, height=None):
        if width:
            self.W, self.H = width, height or width
        super().__init__(master, icon_off, icon_on, size=self.H, command=command)
        self.configure(width=self.W, height=self.H)
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.W, self.H
        if self.hover:
            rounded_rect(self, 0, 0, w - 1, h - 1, 6, fill=COL["raised"], outline="")
        fg = COL["danger"] if self.state_on else COL["fg"]
        icon = self.icon_on if self.state_on and self.icon_on != self.icon_off else self.icon_off
        self.create_image(w / 2, h / 2, image=self.icons.get(icon, 18, fg))


class HearToggle(MuteToggle):
    """Mithören: an = eigene Stimme auf dem Kopfhörer (Akzentfarbe statt Rot)."""

    def _draw(self):
        self.delete("all")
        w, h = self.W, self.H
        if self.state_on or self.hover:
            rounded_rect(self, 0, 0, w - 1, h - 1, 6, fill=COL["accent"] if self.state_on else COL["raised"],
                         outline="")
        fg = "#ffffff" if self.state_on else COL["fg"]
        self.create_image(w / 2, h / 2, image=self.icons.get(self.icon_off, 17, fg))


class DuckMeter(tk.Canvas):
    """Mikrofonpegel für das Auto-Ducking mit Markierung der Schwelle (gleiche Skala wie der Regler)."""

    LO, HI = -60.0, -5.0

    def __init__(self, master, width, height=10):
        super().__init__(master, width=width, height=height + 6, bg=theme._bg_of(master), highlightthickness=0,
                         bd=0)
        self.w, self.h = width, height
        self.level, self.threshold, self.ducking = 0.0, -30.0, False
        self._draw()

    def _x(self, db):
        return max(0.0, min(1.0, (db - self.LO) / (self.HI - self.LO))) * self.w

    def set(self, level=None, threshold=None, ducking=None):
        changed = False
        for name, v in (("level", level), ("threshold", threshold), ("ducking", ducking)):
            if v is not None and v != getattr(self, name):
                setattr(self, name, v)
                changed = True
        if changed:
            self._draw()

    def _draw(self):
        self.delete("all")
        w, h, y = self.w, self.h, 3
        rounded_rect(self, 0, y, w, y + h, h / 2, fill=COL["raised"], outline="")
        db = levels.level_db(self.level)
        if db is not None and db > self.LO:
            rounded_rect(self, 0, y, max(h, self._x(db)), y + h, h / 2,
                         fill=COL["warn"] if self.ducking else COL["ok"], outline="")
        x = self._x(self.threshold)
        self.create_line(x, 0, x, h + 6, fill=COL["fg"], width=2)


class FxPill(tk.Canvas):
    """Effekte eines Kanals: „Gate · Comp   2“ (aktiv) oder „Effects   +“."""

    def __init__(self, master, width, command=None, height=26):
        super().__init__(master, width=width, height=height, bg=theme._bg_of(master), highlightthickness=0, bd=0,
                         cursor="hand2")
        self.w, self.h, self.command = width, height, command
        self.text, self.count, self.active, self.hover = "", 0, False, False
        self.icons = theme.Icons(self)
        self.f = theme.font(self, 8, "normal")
        self.bind("<Button-1>", lambda e: self.command and self.command())
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self._draw()

    def _set_hover(self, v):
        self.hover = v
        self._draw()

    def set(self, names):
        """names: Namen der eingeschalteten Effekte."""
        text = " · ".join(names) if names else _("Effects")
        if (len(names), bool(names), text) == (self.count, self.active, self.text):
            return  # bei jeder Aktualisierung gesetzt: nur bei einer Änderung neu zeichnen
        self.count = len(names)
        self.active = bool(names)
        self.text = text
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.w, self.h
        if self.active:
            fill, out, fg = COL["accent_soft"], "", COL["accent"]
        else:
            fill, out, fg = (COL["raised"] if self.hover else COL["panel2"]), COL["border"], COL["muted"]
        rounded_rect(self, 0, 0, w - 1, h - 1, 9, fill=fill, outline=out)
        self.create_image(13, h / 2, image=self.icons.get("spark", 12, fg))
        right = str(self.count) if self.active else "+"
        self.create_text(w - 9, h / 2, text=right, anchor="e", font=self.f, fill=fg)
        avail = w - 24 - 10 - theme.measure(self, self.f, right) - 8
        self.create_text(23, h / 2, text=_fit_text(self, self.f, self.text, avail), anchor="w", font=self.f, fill=fg)


class AddCard(tk.Canvas):
    """Gestrichelte Karte „Kanal hinzufügen“ am Ende der Kanäle."""

    def __init__(self, master, command, width=110, height=380):
        super().__init__(master, width=width, height=height, bg=theme._bg_of(master), highlightthickness=0, bd=0,
                         cursor="hand2")
        self.command, self.hover = command, False
        self.icons = theme.Icons(self)
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", lambda e: self.command())
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self._draw()

    def _set_hover(self, v):
        self.hover = v
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = int(self["width"]), int(self["height"])
        rounded_rect(self, 1, 1, w - 2, h - 2, 16, fill=COL["panel"] if self.hover else "",
                     outline=COL["faint"] if self.hover else COL["border"], dash=(5, 4), width=2)
        cy = h / 2 - 18
        rounded_rect(self, w / 2 - 20, cy - 20, w / 2 + 20, cy + 20, 12, fill=COL["panel2"], outline="")
        self.create_image(w / 2, cy, image=self.icons.get("plus", 18, COL["accent"]))
        self.create_text(w / 2, cy + 42, text=_("Add channel"), width=w - 20, justify="center",
                         font=theme.font(self, 10, "bold"), fill=COL["muted"])


class FxButton(FxPill):
    """Kleiner Effekte-Knopf in der Mixer-Tabelle; aktiv = Akzent, daneben die Anzahl."""

    def __init__(self, master, command=None):
        super().__init__(master, 28, command=command, height=28)

    def _draw(self):
        self.delete("all")
        w, h = self.w, self.h
        if self.hover:
            rounded_rect(self, 0, 0, w - 1, h - 1, 6, fill=COL["raised"], outline="")
        fg = COL["ok"] if self.active else COL["fg"]  # Effekte an: grün wie in Wave Link
        cache = theme._img_cache(self)  # je Farbe einmal gebaut, nicht bei jedem Überfahren/Aktualisieren
        key = ("sliders", 16, fg)
        if key not in cache:
            cache[key] = ImageTk.PhotoImage(_sliders_image(16, fg), master=self)
        self.create_image(w / 2, h / 2, image=cache[key])


class AddRowCard(AddCard):
    """Flache Zeile „+ Kanal anlegen“ unter der Mixer-Tabelle (wie Wave Link)."""

    def __init__(self, master, text, command, width, height=48):
        self.text = text
        super().__init__(master, command, width=width, height=height)

    def _draw(self):
        self.delete("all")
        w, h = int(self["width"]), int(self["height"])
        rounded_rect(self, 0, 0, w - 1, h - 1, 6, fill=COL["panel"] if self.hover else COL["panel2"], outline="")
        f = theme.font(self, 10)
        tw = theme.measure(self, f, self.text)
        x = (w - tw - 24) / 2
        self.create_image(x + 8, h / 2, image=self.icons.get("plus", 16, COL["fg"]))
        self.create_text(x + 24, h / 2, text=self.text, anchor="w", font=f, fill=COL["fg"])


def _plus_circle(master, size, color):
    """⊕ als Bild (glatt gezeichnet)."""
    k = 4
    S = size * k
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    w = int(1.5 * k)
    d.ellipse((w / 2, w / 2, S - w / 2, S - w / 2), outline=color, width=w)
    a = S * 0.24
    d.line((S / 2 - a, S / 2, S / 2 + a, S / 2), fill=color, width=w)
    d.line((S / 2, S / 2 - a, S / 2, S / 2 + a), fill=color, width=w)
    return ImageTk.PhotoImage(im.resize((size, size), Image.LANCZOS), master=master)


class Tooltip:
    """Kleiner Hinweis über einem Widget (eigenes Fenster ohne Rahmen)."""

    def __init__(self, widget, text):
        self.widget, self.text, self.win = widget, text, None

    def show(self):
        if self.win is not None:
            return
        w = self.widget
        win = tk.Toplevel(w)
        win.overrideredirect(True)
        win.configure(bg=COL["border"])
        tk.Label(win, text=self.text, bg=COL["raised"], fg=COL["fg"], font=theme.font(w, 9),
                 padx=10, pady=5).pack(padx=1, pady=1)
        win.update_idletasks()
        x = w.winfo_rootx() + (w.winfo_width() - win.winfo_reqwidth()) // 2
        y = w.winfo_rooty() + w.winfo_height() // 2 - win.winfo_reqheight() - 14
        win.geometry(f"+{x}+{y}")
        self.win = win

    def hide(self):
        if self.win is not None:
            try:
                self.win.destroy()
            except tk.TclError:
                pass
            self.win = None


class EmptyCell(tk.Canvas):
    """Zelle der Mixer-Tabelle, wenn der Kanal nicht im Mix ist: beim Überfahren heller mit ⊕ und Hinweis."""

    def __init__(self, master, width, height, command=None):
        super().__init__(master, width=width, height=height, bg=COL["bg"], highlightthickness=0, bd=0,
                         cursor="hand2")
        self.w, self.h, self.command, self.hover = width, height, command, False
        self.plus = _plus_circle(self, 18, COL["fg"])
        self.tip = Tooltip(self, _("Add channel to mix"))
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<Button-1>", lambda e: self._click())
        self.bind("<Destroy>", lambda e: self.tip.hide(), add="+")
        self._draw()

    def _click(self):
        self.tip.hide()
        if self.command:
            self.command()

    def _set_hover(self, v):
        self.hover = v
        self._draw()
        self.tip.show() if v else self.tip.hide()

    def _draw(self):
        self.delete("all")
        w, h = self.w, self.h
        rounded_rect(self, 0, 0, w - 1, h - 1, 6, fill=COL["raised"] if self.hover else COL["panel2"], outline="")
        if self.hover:
            self.create_image(w / 2, h / 2, image=self.plus)


class RemoveBadge(tk.Canvas):
    """Kleines X im Kreis auf der Ecke einer Zelle: Kanal aus dem Mix nehmen. Rot beim Überfahren."""

    SIZE = 14

    def __init__(self, master, command=None):
        s = self.SIZE
        super().__init__(master, width=s, height=s, bg=COL["bg"], highlightthickness=0, bd=0, cursor="hand2")
        self.command = command
        self.img = {k: self._image(k) for k in (False, True)}
        self.item = self.create_image(s / 2, s / 2, image=self.img[False])
        self.bind("<Enter>", lambda e: self.itemconfigure(self.item, image=self.img[True]))
        self.bind("<Leave>", lambda e: self.itemconfigure(self.item, image=self.img[False]))
        self.bind("<ButtonRelease-1>", lambda e: self.command and self.command())

    def _image(self, hover):
        k, s = 4, self.SIZE
        S = s * k
        fill, ring, x = ((COL["danger"], COL["danger"], "#ffffff") if hover
                         else (COL["panel2"], COL["border"], COL["muted"]))
        im = Image.new("RGBA", (S, S), theme.rgb(COL["bg"]) + (255,))
        d = ImageDraw.Draw(im)
        d.ellipse((k / 2, k / 2, S - k / 2, S - k / 2), fill=fill, outline=ring, width=k)
        a = S * 0.17
        for sgn in (1, -1):
            d.line((S / 2 - a, S / 2 - sgn * a, S / 2 + a, S / 2 + sgn * a), fill=x, width=int(1.6 * k))
        return ImageTk.PhotoImage(im.resize((s, s), Image.LANCZOS), master=self)


LINK_GAP = 32  # Lücke hinter dem ersten Mix eines Kanals, darin die Kette (LinkToggle.SIZE + Rand)


class LinkToggle(tk.Canvas):
    """Kettensymbol zwischen zwei Mix-Reglern (wie das Schloss in Wave Link): gekoppelt = farbig."""

    SIZE = 24

    def __init__(self, master, command=None):
        s = self.SIZE
        super().__init__(master, width=s, height=s, bg=theme._bg_of(master), highlightthickness=0, bd=0,
                         cursor="hand2")
        self.command, self.on, self.hover = command, False, False
        self.icons = theme.Icons(self)
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", lambda e: self.command and self.command(not self.on))
        self._draw()

    def set(self, on):
        if bool(on) != self.on:
            self.on = bool(on)
            self._draw()

    def _set_hover(self, v):
        self.hover = v
        self._draw()

    def _draw(self):
        self.delete("all")
        s = self.SIZE
        # gekoppelt: farbiger Punkt mit verhakter Kette; getrennt: nur die offene Kette (Rand beim Überfahren)
        if self.on:
            self.create_oval(1, 1, s - 2, s - 2, fill=COL["accent"], outline="")
        elif self.hover:
            self.create_oval(1, 1, s - 2, s - 2, fill=COL["raised"], outline=COL["border"])
        self.create_image(s / 2, s / 2, image=self.icons.get("link" if self.on else "link_off", 15,
                                                             "#ffffff" if self.on else COL["muted"]))


class AppChip(tk.Canvas):
    """Programm mit Kanal: [Symbol] Spotify │ → Musik. Klick öffnet die Kanalauswahl."""

    DRAG_PX = 6  # ab so viel Bewegung wird aus dem Klick ein Ziehen

    def __init__(self, master, app, target, tile, command=None, height=36, drag=None):
        super().__init__(master, height=height, bg=theme._bg_of(master), highlightthickness=0, bd=0,
                         cursor="hand2")
        self.app, self.target, self.tile, self.command, self.h = app, target, tile, command, height
        self.drag = drag  # drag(chip, phase, x_root, y_root): Programm auf einen Kanal ziehen
        self.hover = False
        self._press = None
        self._dragging = False
        self.icons = theme.Icons(self)
        self.f = theme.font(self, 10, "normal")
        self.fs = theme.font(self, 9, "normal")
        self.bind("<ButtonPress-1>", self._down)
        self.bind("<B1-Motion>", self._move)
        self.bind("<ButtonRelease-1>", self._up)
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self._draw()

    def _down(self, e):
        self._press, self._dragging = (e.x_root, e.y_root), False

    def _move(self, e):
        if self._press is None or self.drag is None:
            return
        if not self._dragging and max(abs(e.x_root - self._press[0]), abs(e.y_root - self._press[1])) >= self.DRAG_PX:
            self._dragging = True
            self.drag(self, "start", e.x_root, e.y_root)
        if self._dragging:
            self.drag(self, "move", e.x_root, e.y_root)

    def _up(self, e):
        if self._dragging:
            self._dragging = False
            self.drag(self, "drop", e.x_root, e.y_root)
        elif self._press is not None and self.command:
            self.command(self)  # einfacher Klick: Kanalauswahl
        self._press = None

    def _set_hover(self, v):
        self.hover = v
        self._draw()

    def _draw(self):
        self.delete("all")
        app = _fit_text(self, self.f, self.app, 150)
        aw, tw = theme.measure(self, self.f, app), theme.measure(self, self.fs, self.target)
        w = 6 + 24 + 8 + aw + 10 + 1 + 8 + 12 + 5 + tw + 10
        self.configure(width=w)
        h = self.h
        rounded_rect(self, 0, 0, w - 1, h - 1, 10, fill=COL["raised"] if self.hover else COL["panel2"],
                     outline=COL["border"])
        self.create_image(6, h / 2, image=self.tile, anchor="w")
        x = 38
        self.create_text(x, h / 2, text=app, anchor="w", font=self.f, fill=COL["fg"])
        x += aw + 10
        self.create_line(x, 9, x, h - 9, fill=COL["border"])
        x += 9
        self.create_image(x + 6, h / 2, image=self.icons.get("arrow", 11, COL["muted"]))
        self.create_text(x + 17, h / 2, text=self.target, anchor="w", font=self.fs, fill=COL["muted"])


def _alive(widget):
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:
        return False


def _flow(container, widgets, gap=8):
    """Widgets nebeneinander mit Zeilenumbruch (wie flex-wrap) per place() anordnen."""
    width = max(container.winfo_width(), 200)
    x = y = row_h = 0
    for w in widgets:
        ww, wh = w.winfo_reqwidth(), w.winfo_reqheight()
        if x and x + ww > width:
            x, y = 0, y + row_h + gap
            row_h = 0
        w.place(x=x, y=y)
        x += ww + gap
        row_h = max(row_h, wh)
    container.configure(height=max(1, y + row_h))


class Worker:
    """Führt Aufträge nacheinander in einem Hintergrund-Thread aus."""

    def __init__(self):
        self.jobs = queue.Queue()
        self.results = queue.Queue()
        threading.Thread(target=self._run, name="wave-ui", daemon=True).start()

    def submit(self, fn, done=None):
        self.jobs.put((fn, done))

    def _run(self):
        while True:
            fn, done = self.jobs.get()
            try:
                res, err = fn(), None
            except Exception as e:  # noqa: BLE001
                res, err = None, e
            self.results.put((done, res, err))


class WaveWindow:
    def __init__(self, root, host, hidden=False, tray=True, level_factory=None):
        self.root, self.host = root, host
        theme.gc_in_main_thread(root)  # Tk-Objekte nie in Arbeits-Threads aufräumen (sonst hängen sie)
        self.mixer, self.mic = host.mixer, host.mic
        self.worker = Worker()
        self.levels = levels.LevelMonitor(factory=level_factory)
        self._want_show = threading.Event()
        self._want_quit = threading.Event()
        self.quitting = False
        self._hidden_once = False
        self._touched = {}
        self._pending = {}
        self._timers = {}
        self._refreshing = False
        self._refresh_again = False
        self._last_refresh = 0.0
        self.state = {}
        self.strips = {}
        self.mix_heads = {}
        self.mix_cards = {}
        self._strip_ids = None
        self._mix_card_ids = None
        self._stream_keys = None
        self._fx_ids = None
        self._apps_ids = None
        self.view = "mixer"
        root.title("FreeWave")
        root.geometry("1280x800")
        root.minsize(1100, 720)
        root.protocol("WM_DELETE_WINDOW", self.hide)
        theme.apply(root)
        root.configure(bg=COL["bg"])
        self.icons = theme.Icons(root)
        self._build()
        try:
            from .wavehost import make_icon
            self._icon = ImageTk.PhotoImage(make_icon(128), master=root)
            root.iconphoto(True, self._icon)
        except Exception:  # noqa: BLE001
            pass
        self.tray = None
        if tray:
            from .desktop import Tray
            from .wavehost import make_icon
            self.tray = Tray(self.request_show, self.request_quit, icon=make_icon(64), title="FreeWave",
                             ident="freewave", open_label=_("Open FreeWave"))
        self._wizard = None
        if hidden:
            root.withdraw()
        else:
            root.after(500, self._maybe_setup)
        self.refresh()
        self._poll()
        self._tick_levels()

    # ======================================================= Einrichtung
    def needs_setup(self):
        """Erster Start: noch nie eingerichtet und Mixer noch nicht in Betrieb."""
        return not (self.host.cfg.get("linewave") or {}).get("setup_done") and not self.host.mixer_enabled

    def _maybe_setup(self):
        if self.needs_setup():
            self.open_setup()

    def open_setup(self):
        """Einrichtungsassistent öffnen (beim ersten Start automatisch)."""
        if self._wizard is not None and self._wizard.winfo_exists():
            self._wizard.lift()
            return
        from . import wavesetup

        def opened(data, err):
            if err is not None:
                return
            self._wizard = wavesetup.SetupWizard(self, data, on_done=self._after_setup)
        self.worker.submit(lambda: wavesetup.collect(self.host), opened)

    def _after_setup(self):
        self._strip_ids = None
        self.refresh()

    # ============================================================ Fenster
    def request_show(self):
        self._want_show.set()

    def request_quit(self):
        self._want_quit.set()

    def visible(self):
        try:
            return self.root.state() in ("normal", "zoomed")
        except tk.TclError:
            return False

    def show(self):
        first = self.root.state() == "withdrawn"
        self.root.deiconify()
        if first and self.needs_setup():
            self.root.after(400, self._maybe_setup)
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass
        self.refresh()

    def hide(self):
        self.root.withdraw()
        self.levels.stop()
        if not self._hidden_once:
            self._hidden_once = True
            from .desktop import notify
            notify(_("FreeWave is still running"),
                   _("Mixer and microphone stay active. Open it again from the “FreeWave” menu."),
                   app="FreeWave", icon="audio-volume-high")

    def open_settings(self):
        """Design, Sprache und Updates."""
        from .settingsui import open_settings
        return open_settings(self.root, "FreeWave", self._restart,
                             mixer=(self.state.get("mixer_active"), self._mixer_enable), setup=self.open_setup)

    def _restart(self):
        theme.restart_later("freestream.freewave")
        self.quit_app()

    def quit_app(self):
        self.request_quit()

    def _poll(self):
        if self._want_quit.is_set():
            self._quit()
            return
        if self._want_show.is_set():
            self._want_show.clear()
            self.show()
        while True:
            try:
                done, res, err = self.worker.results.get_nowait()
            except queue.Empty:
                break
            if err is not None:
                self.status(_("Error: {error}").format(error=err))
            if done is not None:
                try:
                    done(res, err)
                except Exception as e:  # noqa: BLE001
                    log.exception("UI: %s", e)
        if self.visible() and not self._refreshing and self._refresh_due():
            self.refresh()
        self.root.after(100, self._poll)

    def _quit(self):
        if self.quitting:
            return
        self.quitting = True
        self.levels.stop()
        self.soundview.stop_all()
        if self.tray:
            self.tray.stop()
        self.root.quit()

    def status(self, text):
        self.status_var.set(text)

    # ============================================================== Aufbau
    def _build(self):
        r = self.root
        self._build_sidebar()
        main = tk.Frame(r, bg=COL["bg"])
        main.pack(side="left", fill="both", expand=True)
        self.main = main
        self._build_header(main)
        self.status_var = tk.StringVar(self.root, value="")
        tk.Label(main, textvariable=self.status_var, bg=COL["bg"], fg=COL["muted"], anchor="w",
                 font=theme.font(r, 9)).pack(fill="x", side="bottom", padx=22, pady=(0, 6))
        self.content = tk.Frame(main, bg=COL["bg"])
        self.content.pack(fill="both", expand=True)
        self.content.bind("<Configure>", self._content_resized, add="+")
        self.view_mixer = tk.Frame(self.content, bg=COL["bg"])
        self.view_mic = tk.Frame(self.content, bg=COL["bg"])
        self.view_fx = tk.Frame(self.content, bg=COL["bg"])
        self.view_apps = tk.Frame(self.content, bg=COL["bg"])
        self.view_flow = tk.Frame(self.content, bg=COL["bg"])
        self.view_sound = tk.Frame(self.content, bg=COL["bg"])
        self._build_mixer()
        from .flowview import FlowView
        from .soundview import SoundboardView
        self.flow = FlowView(self.view_flow, self.root, on_select=lambda _nid: self._update_header())
        self.soundview = SoundboardView(self.view_sound, self)
        self._build_mic()
        self._build_fx_view()
        self._build_apps_view()
        self.show_view("mixer")

    # ------------------------------------------------------- Seitenleiste
    def _build_sidebar(self):
        sb = Sidebar(self.root)
        sb.pack(side="left", fill="y")
        self.sidebar = sb
        sb.brand("FreeWave", "wave")
        self.mic_head = tk.StringVar(self.root, value=_("Looking for microphone …"))
        sb.section(_("Wave devices"))
        self.mic_card = InfoCard(sb.top, _("Microphone"), _("Looking for microphone …"),
                                 image=icon_tile(self.root, "mic", 30, COL["fg"], radius=0.2, fg=COL["side"]),
                                 height=50,
                                 command=lambda: self.show_view("mic"))
        self.mic_card.pack(fill="x", pady=(0, 6))
        tk.Frame(sb.top, bg=COL["border"], height=1).pack(fill="x", padx=6, pady=(8, 4))
        self.nav = {}
        for key, text, icon in (("mixer", _("Mixer"), "sliders"), ("flow", _("Audio Flow"), "flow"),
                                ("soundboard", _("Soundboard"), "waveform"), ("mic", _("Microphone"), "mic"),
                                ("effects", _("Effects"), "spark"), ("apps", _("Apps"), "grid")):
            item = NavItem(sb.top, text, icon, "", command=lambda k=key: self.show_view(k))
            item.icons = _NavIcons(item)
            item._draw()
            item.pack(fill="x", pady=1)
            self.nav[key] = item
        sb.section(_("Mixes"))
        self.mix_list = tk.Frame(sb.top, bg=COL["side"])
        self.mix_list.pack(fill="x")
        self._fill_mix_cards([{"id": "monitor", "name": _("Personal Mix"), "icon": "headphones",
                               "color": COL["monitor"]},
                              {"id": "stream", "name": _("Stream Mix"), "icon": "stream", "color": COL["stream"]}])

        b = tk.Frame(sb.bottom, bg=COL["side"])  # wie in FreeDeck
        b.pack(fill="x")
        self.settings_item = theme.NavItem(b, _("Settings"), "gear", command=self.open_settings)
        self.settings_item.pack(fill="x", pady=(2, 4))

    def _fill_mix_cards(self, mixes):
        """Seitenleiste: je Mix eine Karte (Klick = bearbeiten)."""
        for w in self.mix_list.winfo_children():
            w.destroy()
        self.mix_cards = {}
        for m in mixes:
            card = InfoCard(self.mix_list, m["name"], self._mix_sub(m) if m.get("devices_eff") is not None else "–",
                            image=icon_tile(self.root, m.get("icon") or "wave", 30, m.get("color") or COL["accent"],
                                            radius=0.2), height=50,
                            command=lambda mid=m["id"]: self._edit_mix_id(mid))
            card.pack(fill="x", pady=(0, 2))
            card.bind("<Button-3>", lambda e, mid=m["id"]: self._mix_menu(mid, e.x_root, e.y_root))
            self.mix_cards[m["id"]] = card
        self.addmix_item = NavItem(self.mix_list, _("Create mix"), "plus", accent=True, command=self._new_mix)
        self.addmix_item.pack(fill="x")
        self.monitor_card = self.mix_cards.get("monitor")

    def _mix_menu(self, mid, x, y):
        """Rechtsklick auf einen Mix (Seitenleiste oder Spaltenkopf): bearbeiten, abhören, löschen."""
        if not self.state.get("mixer_active"):
            return None
        m = next((x for x in self.state.get("mixes") or [] if x["id"] == mid), None)
        if m is None:
            return None
        listening = (getattr(self, "_listen_now", None) or self.state.get("listen") or "monitor") == mid
        menu = PopupMenu(self.root)
        menu.add(_("Edit …"), lambda: self._edit_mix(m), icon="edit")
        if m.get("can_listen") and mid != "monitor":
            menu.add(_("Stop listening") if listening else _("Listen"), lambda: self._on_listen(mid),
                     icon="headphones")
        menu.separator()
        menu.add(_("Delete mix"), (lambda: self._delete_mix(m)) if not m.get("builtin") else None, icon="trash",
                 danger=True)
        return menu.popup(x, y)

    def _output_menu(self):
        """Gerät zum Abhören wählen (= Gerät des Persönlichen Mixes: Kopfhörer, Lautsprecher …)."""
        outs = self.state.get("outputs") or []
        menu = PopupMenu(self.root, width=300)
        if not outs:
            menu.add(_("No output device found"))
        cur = self.out_var.get()
        for name, desc in outs:
            menu.add(desc, lambda d=desc: (self.out_var.set(d), self._on_output()),
                     icon="check" if desc == cur else "speaker")
        anchor = getattr(self, "listen_btn", None) or self.monitor_card
        menu.below(anchor)
        return menu

    # -------------------------------------------------------------- Kopf
    def _build_header(self, parent):
        head = tk.Frame(parent, bg=COL["bg"], padx=24, pady=16)
        head.pack(fill="x")
        left = tk.Frame(head, bg=COL["bg"])
        left.pack(side="left")
        self.title_var = tk.StringVar(self.root, value=_("Mixer"))
        self.sub_var = tk.StringVar(self.root, value="")
        label(left, textvariable=self.title_var, size=24, weight="bold").pack(anchor="w")
        label(left, textvariable=self.sub_var, size=9, color=COL["muted"]).pack(anchor="w")
        self.listen_btn = Button(head, "–", "headphones", command=self._output_menu, height=36)
        self.add_btn = Button(head, _("Add channel"), "plus", kind="primary", command=self._new_channel)
        self.scene_btn = Button(head, _("Scenes"), "layers", command=self._scene_menu)
        self.deck_btn = Button(head, _("Export to FreeDeck"), "export", command=self.open_deck_export)
        self.setup_btn = Button(head, _("Set up mixer"), "wave", kind="primary",
                                command=lambda: self._mixer_enable(True))
        self.clear_btn = Button(head, _("Clear selection"), "close", command=lambda: self.flow.select(None))
        self.head, self.head_left = head, left

    def _update_header(self):
        st = self.state
        active = bool(st.get("mixer_active"))
        shown = ()
        if self.view == "mixer":
            self.title_var.set(_("Mixer"))
            if active:
                n = len(st.get("channels") or [])
                k = len(st.get("mixes") or [])
                self.sub_var.set(_("{count} channels · {mixes} mixes · everything is live").format(count=n, mixes=k))
                shown = ("listen", "deck", "scene")
            else:
                self.sub_var.set(_("The mixer is off – set it up to give every app its own channel."))
                shown = ("setup",)
        elif self.view == "mic":
            self.title_var.set(_("Microphone"))
            self.sub_var.set(self.mic_name.get())
        elif self.view == "flow":
            self.title_var.set(_("Audio Flow"))
            self.sub_var.set(_("Click any element to follow its signal path"))
            if self.flow.selected:
                shown = ("clear",)
        elif self.view == "soundboard":
            self.title_var.set(_("Soundboard"))
            self.sub_var.set(_("Click a sound to play it – click again to stop. Right-click to edit it."))
        elif self.view == "effects":
            self.title_var.set(_("Effects"))
            self.sub_var.set(_("Effect chains per channel – changes are live."))
        else:
            self.title_var.set(_("Apps"))
            self.sub_var.set(_("Click an app to move it to another channel"))
            if active:
                shown = ("add",)
        if not parts.has_freedeck():
            shown = tuple(k for k in shown if k != "deck")
        # nur bei einer Änderung neu anordnen – sonst flackert die Kopfzeile bei jeder Aktualisierung
        if shown == getattr(self, "_header_shown", None):
            return
        self._header_shown = shown
        self._pack_header()

    def _pack_header(self):
        widgets = {"add": (self.add_btn, {}), "deck": (self.deck_btn, {"padx": (0, 8)}),
                   "listen": (self.listen_btn, {}),
                   "scene": (self.scene_btn, {"padx": (0, 8)}),
                   "setup": (self.setup_btn, {}), "clear": (self.clear_btn, {})}
        for w in (self.add_btn, self.deck_btn, self.scene_btn, self.setup_btn, self.listen_btn, self.clear_btn):
            w.pack_forget()
        for key in self._header_shown:
            w, kw = widgets[key]
            w.pack(side="right", **kw)

    def show_view(self, key):
        self.view = key
        for k, item in self.nav.items():
            item.set(active=k == key)
        for k, f in (("mixer", self.view_mixer), ("mic", self.view_mic), ("effects", self.view_fx),
                     ("apps", self.view_apps), ("flow", self.view_flow), ("soundboard", self.view_sound)):
            if k == key:
                f.pack(fill="both", expand=True)
            else:
                f.pack_forget()
        if key == "flow":
            self.flow.show()
        else:
            self.flow.hide()
        if key == "soundboard":
            self.soundview.show()
        else:
            self.soundview.hide()
        self._update_header()

    # -------------------------------------------------------------- Mixer
    def _build_mixer(self):
        t = self.view_mixer
        # Mixer noch nicht eingerichtet
        self.mix_off = tk.Frame(t, bg=COL["bg"])
        card = Card(self.mix_off, pad=28, radius=16)
        card.pack(pady=60)
        b = card.body
        tk.Label(b, image=theme.gradient_mark(self.root, 64, "wave"), bg=COL["panel"]).pack()
        label(b, _("Set up mixer"), 16, "bold").pack(pady=(14, 6))
        label(b, _("Just like in Elgato Wave Link, every app gets a channel – with its own volume "
                   "for your headphones (Personal Mix), your stream (Stream Mix) and any mix you create."),
              color=COL["muted"], wraplength=460, justify="center").pack()
        Button(b, _("Set up mixer"), "wave", kind="primary", command=lambda: self._mixer_enable(True)).pack(
            pady=(18, 0))

        # Mixer aktiv: Tabelle – Kanäle links, je Mix eine Spalte (wie „Mixes“ in Wave Link)
        self.mix_on = tk.Frame(t, bg=COL["bg"])
        self.out_var = tk.StringVar(self.root)
        area = ScrollArea(self.mix_on, bg=COL["bg"])
        area.pack(fill="both", expand=True, padx=(22, 10), pady=(16, 0))
        self.table_scroll = HScroll(area.inner)
        self.table_scroll.pack(fill="x", anchor="nw")
        self.table = self.table_scroll.inner
        self.table_hint = label(area.inner, "", 9, color=COL["muted"], anchor="w", justify="left", wraplength=980)
        self.table_hint.pack(fill="x", pady=(12, 10))
        self.mix_meters = {}

    def _mixer_enable(self, on):
        self.status(_("Setting up mixer …") if on else _("Removing mixer …"))
        self._strip_ids = None

        def done(_res, err):
            if err is None:
                self.status(_("Mixer set up") if on else _("Mixer removed"))
            self.refresh()
        self.worker.submit(lambda: self.host.enable_mixer(on), done)

    # ------------------------------------------------------- Tabelle
    def _mix_sub(self, m):
        """Unterzeile eines Mixes: „2 Ausgänge“, „Kopfhörer“, „virtuell“ …"""
        outs = dict(self.state.get("outputs") or [])
        devs = m.get("devices_eff") or []
        bits = []
        if len(devs) > 1:
            bits.append(ngettext("{n} output", "{n} outputs", len(devs)).format(n=len(devs)))
        elif devs:
            bits.append(outs.get(devs[0], devs[0]))
        if m.get("virtual"):
            bits.append("OBS" if m["id"] == "stream" else _("virtual"))
        return " · ".join(bits) or _("no output")

    def _fit_mix_w(self, n):
        """Breite der Mix-Spalten: so, dass alle Mixe ins Fenster passen (wie Wave Link), in Grenzen."""
        cw = self.content.winfo_width()
        if cw < 100:
            cw = max(self.root.winfo_width(), 1280) - 236
        avail = cw - 22 - 10 - CH_W - 12 - 30 - LINK_GAP
        w = int(avail / max(1, n)) - 10
        return max(MIX_W_MIN, min(MIX_W_MAX, w))

    def _content_resized(self, _e=None):
        """Fenster breiter/schmaler: Mix-Spalten neu verteilen (verzögert, nur bei echter Änderung)."""
        job = getattr(self, "_resize_job", None)
        if job:
            self.root.after_cancel(job)

        def check():
            self._resize_job = None
            n = len(self.mix_heads)
            w = self._fit_mix_w(n) if n else None
            if w and w != getattr(self, "mix_w", MIX_W):
                self._resize_columns(w)
                self.root.after_idle(self.table_scroll._fit)
        self._resize_job = self.root.after(300, check)

    def _resize_columns(self, w):
        """Alle Mix-Spalten auf die Breite w bringen, ohne Zellen neu anzulegen."""
        self.mix_w = w
        f = theme.font(self.root, 10, "bold")
        for mid, head in self.mix_heads.items():
            card = head["card"]
            card._fixed_w = w
            card.configure(width=w)
            head["name"].configure(text=_fit_text(self.root, f, head["mix"]["name"], w - 90))
            meter = self.mix_meters.get(mid, {}).get("meter")
            if meter is not None:
                meter.configure(width=w - 76)
        for (cid, mid), (widget, _r, _c, _m) in self.cells.items():
            if isinstance(widget, EmptyCell):
                widget.w = w
                widget.configure(width=w)
                widget._draw()
                continue
            widget._fixed_w = w
            widget.configure(width=w)
            part = self.strips.get(cid, {}).get(mid)
            if part:
                part["fader"].configure(width=w - 100)
                part["meter"].configure(width=w - 100 - METER_INSET)

    def _build_table(self, channels, mixes):
        self.mix_w = self._fit_mix_w(len(mixes))
        for w in self.table.winfo_children():
            w.destroy()
        self.strips, self.mix_meters, self.mix_heads, self.cells = {}, {}, {}, {}
        tb = self.table
        # oben links: Abhören (welches Gerät, welcher Mix)
        lb = tk.Frame(tb, bg=COL["bg"], width=CH_W, height=HEAD_H)
        lb.grid(row=0, column=0, sticky="nw", padx=(0, 12), pady=(0, 10))
        for col, m in enumerate(mixes, start=1):
            self.mix_heads[m["id"]] = self._mix_head(tb, m, col)
        for row, ch in enumerate(sorted(channels, key=lambda c: c["id"] != "mic"), start=1):
            self._channel_row(tb, ch, mixes, row)
        self.add_row = AddRowCard(tb, _("Create channel"), self._new_channel, width=CH_W, height=48)
        self.add_row.grid(row=len(channels) + 1, column=0, sticky="nw", pady=(2, 0))
        self.root.after_idle(self.table_scroll._fit)

    def _drop_cell(self, cid, mid):
        cell = self.cells.pop((cid, mid), None)
        part = self.strips.get(cid, {}).pop(mid, None)
        if part and part.get("remove") is not None:
            part["remove"].destroy()
        if cell:
            cell[0].destroy()

    def _sync_table(self, channels, mixes, old, new):
        """Nur das Geänderte umbauen: neue/gelöschte/geänderte Mixe (Spalten) und Kanäle (Zeilen);
        der Rest der Tabelle bleibt stehen. False, wenn ein kompletter Neuaufbau nötig ist."""
        if not hasattr(self, "add_row") or not self.add_row.winfo_exists():
            return False
        new_w = self._fit_mix_w(len(mixes))
        if new_w != getattr(self, "mix_w", MIX_W):
            self._resize_columns(new_w)  # Spalten an Ort und Stelle breiter/schmaler, Regler bleiben
        def no_excl(x):
            return x[:4] + x[5:]
        old_ch, new_ch = {c[0]: c for c in old[0]}, {c[0]: c for c in new[0]}
        old_mx, new_mx = {x[0]: no_excl(x) for x in old[1]}, {x[0]: no_excl(x) for x in new[1]}
        tb = self.table
        # 1. gelöschte oder geänderte Spalten und Zeilen entfernen
        for mid in [m for m in old_mx if new_mx.get(m) != old_mx[m]]:
            head = self.mix_heads.pop(mid, None)
            if head:
                head["card"].destroy()
            self.mix_meters.pop(mid, None)
            for cid in list(self.strips):
                self._drop_cell(cid, mid)
        for cid in [c for c in old_ch if new_ch.get(c) != old_ch[c]]:
            for mid in list(self.mix_heads):
                self._drop_cell(cid, mid)
            strip = self.strips.pop(cid, None)
            if strip:
                strip["card"].destroy()
        # 2. Reihenfolge: Spalten = Mixe, Zeilen = Kanäle (Mikrofon zuerst)
        order = sorted(channels, key=lambda c: c["id"] != "mic")
        rows = {c["id"]: r for r, c in enumerate(order, start=1)}
        cols = {m["id"]: c for c, m in enumerate(mixes, start=1)}
        by_id = {m["id"]: m for m in mixes}
        for (cid, mid), (w, _r, _c, _m) in list(self.cells.items()):
            w.grid_configure(row=rows[cid], column=cols[mid])
            self.cells[(cid, mid)] = (w, rows[cid], cols[mid], by_id[mid])
        for cid, strip in self.strips.items():
            strip["card"].grid_configure(row=rows[cid])
        # 3. neue Spalten (für die bestehenden Zeilen) und neue Zeilen anlegen
        for m in mixes:
            mid = m["id"]
            if mid in self.mix_heads:
                self.mix_heads[mid]["card"].grid_configure(column=cols[mid])
                self.mix_heads[mid]["mix"] = m
                continue
            self.mix_heads[mid] = self._mix_head(tb, m, cols[mid])
            for cid in self.strips:
                self._place_cell(tb, cid, m, rows[cid], cols[mid], cid not in (m.get("exclude") or []))
        for ch in order:
            if ch["id"] not in self.strips:
                self._channel_row(tb, ch, mixes, rows[ch["id"]])
        # 4. Zuordnungen abgleichen (z. B. neuer Kanal, der in keinem Mix ist)
        for (cid, mid), (w, r, c, m) in list(self.cells.items()):
            member = cid not in (m.get("exclude") or [])
            if member == isinstance(w, EmptyCell):
                self._drop_cell(cid, mid)
                self._place_cell(tb, cid, m, r, c, member)
        self.add_row.grid_configure(row=len(channels) + 1)
        self.root.after_idle(self.table_scroll._fit)
        return True

    def _mix_head(self, tb, m, col):
        color = m.get("color") or COL["accent"]
        mw = self.mix_w
        card = Card(tb, pad=10, radius=6, outline="", width=mw, height=HEAD_H)
        card.grid(row=0, column=col, sticky="nw", padx=(0, 10), pady=(0, 10))
        body = card.body
        body.grid_columnconfigure(1, weight=1)
        tile = tk.Label(body, image=icon_tile(self.root, m.get("icon") or "wave", 36, COL["fg"], radius=0.18,
                                              fg=COL["panel"]),
                        bg=COL["panel"], cursor="hand2")
        tile.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 8))
        name = label(body, _fit_text(self.root, theme.font(self.root, 10, "bold"), m["name"], mw - 90), 10, "bold",
                     cursor="hand2", anchor="w")
        name.grid(row=0, column=1, sticky="w")
        sub = tk.StringVar(self.root, value=self._mix_sub(m))
        label(body, textvariable=sub, size=8, color=COL["muted"], anchor="w").grid(row=1, column=1, sticky="w")
        ear = RoundToggle(body, "headphones", "headphones", size=26, on_color=color,
                          command=lambda mid=m["id"]: self._on_listen(mid))
        ear.grid(row=0, column=2, rowspan=2, sticky="e")
        if not m.get("can_listen"):
            ear.grid_remove()
        mrow = tk.Frame(body, bg=COL["panel"])  # Pegel des Mixes mit dB-Zahl (rot „CLIP“ bei Übersteuerung)
        mrow.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        meter = LevelMeter(mrow, length=mw - 76, thickness=5, orient="horizontal")
        meter.pack(side="left")
        db = tk.StringVar(self.root, value="")
        db_lb = label(mrow, textvariable=db, size=8, color=COL["muted"], anchor="e", width=7)
        db_lb.pack(side="right")
        for w in (tile, name):
            w.bind("<Button-1>", lambda e, mid=m["id"]: self._edit_mix_id(mid))
        for w in (card, body, tile, name):
            w.bind("<Button-3>", lambda e, mid=m["id"]: self._mix_menu(mid, e.x_root, e.y_root))
        self.mix_meters[m["id"]] = {"meter": meter, "db": db, "label": db_lb}
        return {"card": card, "sub": sub, "ear": ear, "mix": m, "name": name}

    def _channel_row(self, tb, ch, mixes, row):
        cid = ch["id"]
        glyph, color = channel_look(ch)
        card = Card(tb, pad=10, radius=6, outline="", width=CH_W, height=ROW_H)
        card.grid(row=row, column=0, sticky="nw", padx=(0, 12), pady=(0, 8))
        body = card.body
        body.grid_columnconfigure(1, weight=1)
        tile = tk.Label(body, image=icon_tile(self.root, glyph, 40, color, radius=0.18), bg=COL["panel"],
                        cursor="hand2")
        tile.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 10))
        name = label(body, _fit_text(self.root, theme.font(self.root, 10, "bold"), ch["name"], 112), 10, "bold",
                     cursor="hand2", anchor="w")
        name.grid(row=0, column=1, sticky="sw")
        apps = tk.StringVar(self.root, value=" ")
        label(body, textvariable=apps, size=8, color=COL["muted"], anchor="w", width=1).grid(row=1, column=1,
                                                                                             sticky="new")
        name.configure(width=1)
        name.grid_configure(sticky="sew")
        for w in (tile, name):
            w.bind("<Button-1>", lambda e: self._edit_channel_id(cid))
        mute = MuteToggle(body, "speaker", "speaker_off", command=lambda: self._on_master_mute(cid), width=28)
        mute.grid(row=0, column=2, rowspan=2, padx=(4, 6))
        fcol = tk.Frame(body, bg=COL["panel"])
        fcol.grid(row=0, column=3, rowspan=2)
        flen = 74
        fader = Fader(fcol, length=flen, thickness=22, orient="horizontal", color=color,
                      command=lambda v: self._on_master(cid, v))
        fader.pack()
        meter = LevelMeter(fcol, length=flen - METER_INSET, thickness=5, orient="horizontal")
        meter.pack(pady=(3, 0))  # Live-Pegel unter dem Regler, genau so breit wie dessen Spur
        val = tk.StringVar(self.root, value="–")
        if cid != "mic":  # Mikrofon: dort sitzt der Mithören-Knopf (Prozentwert steht unter „Mikrofon“)
            label(body, textvariable=val, size=9, color=COL["muted"], width=5, anchor="e").grid(row=0, column=4,
                                                                                                 rowspan=2)
        fx = FxButton(body, command=lambda c=ch: self._open_effects(c))
        fx.grid(row=0, column=5, rowspan=2, padx=(6, 0))
        hear = None
        if cid == "mic":  # sich selbst hören: Mikrofon im Persönlichen Mix laut/stumm
            hear = HearToggle(body, "headphones", "headphones", command=self._on_hear_mic, width=28)
            hear.grid(row=0, column=6, rowspan=2, padx=(6, 0))
        fx.set(ch.get("fx_names") or [])
        if cid != "mic" and ch.get("kind", "apps") == "apps":
            self.__dict__.setdefault("_drop_targets", {})[card] = cid
        strip = {"channel": ch, "apps": apps, "card": card, "fx": fx, "hear": hear,
                 "master": {"fader": fader, "val": val, "mute": mute, "meter": meter}}
        self.strips[cid] = strip
        for col, m in enumerate(mixes, start=1):
            self._place_cell(tb, cid, m, row, col, cid not in (m.get("exclude") or []))

    def _place_cell(self, tb, cid, m, row, col, member):
        """Zelle Kanal × Mix: Regler (im Mix) oder leer mit „+ hinzufügen“."""
        mid = m["id"]
        if member:
            part = self._mix_cell(tb, cid, m, row, col)
            self.strips[cid][mid] = part
            widget = part["card"]
            badge = RemoveBadge(tb, command=lambda: self._toggle_member(cid, mid, False))
            part["remove"] = badge
            self._hover_badge(widget, badge)
        else:
            self.strips[cid].pop(mid, None)
            widget = EmptyCell(tb, self.mix_w, ROW_H, command=lambda: self._toggle_member(cid, mid, True))
            widget.grid(row=row, column=col, sticky="nw", padx=(0, 10), pady=(0, 8))
        self.cells[(cid, mid)] = (widget, row, col, m)

    def _mix_cell(self, tb, cid, m, row, col):
        mid = m["id"]
        card = Card(tb, pad=10, radius=6, outline="", width=self.mix_w, height=ROW_H)
        card.grid(row=row, column=col, sticky="nw", padx=(0, 10), pady=(0, 8))
        body = card.body
        body.grid_columnconfigure(1, weight=1)
        mute = MuteToggle(body, "speaker", "speaker_off", command=lambda: self._on_mute(cid, mid), width=28)
        mute.grid(row=0, column=0, rowspan=2, padx=(0, 8))
        fcol = tk.Frame(body, bg=COL["panel"])
        fcol.grid(row=0, column=1, rowspan=2, sticky="w")
        fader = Fader(fcol, length=self.mix_w - 100, thickness=22, orient="horizontal", color=m.get("color"),
                      command=lambda v: self._on_level(cid, mid, v))
        fader.pack()
        meter = LevelMeter(fcol, length=self.mix_w - 100 - METER_INSET, thickness=5, orient="horizontal")
        meter.pack(pady=(3, 0))
        val = tk.StringVar(self.root, value="–")
        lb = label(body, textvariable=val, size=9, color=COL["muted"], width=5, anchor="e")
        lb.grid(row=0, column=2, rowspan=2)
        for w in (card, body, lb):
            w.bind("<Button-3>", lambda e: self._cell_menu(e, cid, mid))
        return {"fader": fader, "val": val, "mute": mute, "meter": meter, "label": lb, "card": card}

    def _hover_badge(self, card, badge):
        """X zum Entfernen nur zeigen, solange die Maus über der Zelle (oder dem X) ist."""
        def show(_e=None):
            if badge.winfo_exists() and not badge.winfo_ismapped():
                badge.place(in_=card, relx=1.0, x=-3, y=3, anchor="center")  # halb auf der Ecke
                tk.Misc.lift(badge)  # Canvas.lift wäre tag_raise

        def check():
            if not card.winfo_exists():
                return
            px, py = card.winfo_pointerxy()
            x, y = card.winfo_rootx(), card.winfo_rooty()
            r = RemoveBadge.SIZE
            if not (x - 2 <= px <= x + card.winfo_width() + r and y - r <= py <= y + card.winfo_height() + 2):
                badge.place_forget()

        def leave(_e=None):
            card.after(60, check)  # kurz warten: Wechsel zwischen Zelle, Regler und X
        stack = [card, badge]
        while stack:
            w = stack.pop()
            w.bind("<Enter>", show, add="+")
            w.bind("<Leave>", leave, add="+")
            stack.extend(w.winfo_children())

    def _cell_menu(self, e, cid, mid):
        mname = next((m["name"] for m in self.state.get("mixes") or [] if m["id"] == mid), mid)
        menu = PopupMenu(self.root)
        menu.add(_("Remove from “{mix}”").format(mix=mname), lambda: self._toggle_member(cid, mid, False),
                 icon="close")
        return menu.popup(e.x_root, e.y_root)

    def _on_level(self, cid, mix, value):
        pct = int(round(float(value)))
        strip = self.strips[cid]
        s = strip[mix]
        s["val"].set(f"{pct} %")
        if cid in getattr(self, "_linked", set()):
            # gekoppelt: die anderen Regler des Kanals gleich mitziehen (der Mixer macht es genauso)
            delta = pct - s.get("pct", pct)
            for mid, o in strip.items():
                if mid in ("master", mix) or not isinstance(o, dict) or "fader" not in o or "pct" not in o:
                    continue
                o["pct"] = max(0, min(100, o["pct"] + delta))
                o["fader"].set(o["pct"])
                o["val"].set(f"{o['pct']} %")
                self._touch(f"lvl:{cid}:{mid}")
        s["pct"] = pct
        self._debounced(f"lvl:{cid}:{mix}", lambda v: self.mixer.set_volume(cid, mix, v), pct)

    def _on_hear_mic(self):
        """Eigene Stimme mithören: Mikrofon im Persönlichen Mix laut (an) oder stumm (aus)."""
        on = self.strips["mic"]["hear"].get()
        self._touch("hear")
        self.status(_("You now hear yourself on your headphones (Personal Mix).") if on
                    else _("You no longer hear yourself."))

        def work():
            if "monitor" not in self.mixer.channel_mixes("mic"):
                self.mixer.set_member("mic", "monitor", True)
            self.mixer.set_mute("mic", "monitor", not on)
        self.worker.submit(work, lambda _r, _e: self.refresh())

    def _place_links(self, mixes, linked):
        """Kettensymbol je Kanal zwischen seinen ersten beiden Mix-Reglern (ab zwei Mixen)."""
        self._linked = linked
        btns = self.__dict__.setdefault("link_btns", {})
        order = [m["id"] for m in mixes]
        for cid, strip in self.strips.items():
            members = [mid for mid in order if isinstance(strip.get(mid), dict) and "fader" in strip[mid]]
            anchor = strip[members[0]]["card"] if len(members) > 1 else None
            btn = btns.get(cid)
            if btn is not None and (anchor is None or btn.anchor is not anchor or not btn.winfo_exists()):
                self._drop_link(btns.pop(cid))
                btn = None
            if anchor is None:
                continue
            if btn is None:
                btn = LinkToggle(self.table, command=lambda on, c=cid: self._on_link(c, on))
                btn.anchor = anchor
                # eigene, breitere Lücke hinter dem ersten Mix: die Kette sitzt ganz darin (sonst ragt ihr
                # Quadrat in beide Zellen und zeigt dort die falsche Farbe)
                anchor.grid_configure(padx=(0, LINK_GAP))
                btn.configure(bg=theme._bg_of(anchor.master))
                btn.place(in_=anchor, relx=1.0, x=LINK_GAP // 2, rely=0.5, anchor="center")
                tk.Misc.lift(btn)
                btns[cid] = btn
            if not self._held(f"link:{cid}"):
                btn.set(cid in linked)
        for cid in [c for c in btns if c not in self.strips]:
            self._drop_link(btns.pop(cid))

    @staticmethod
    def _drop_link(btn):
        """Kette entfernen und die breite Lücke hinter ihrer Zelle zurücknehmen – sonst bleibt die ganze
        Mix-Spalte breiter (die Spaltenbreite richtet sich nach der breitesten Zelle)."""
        if _alive(btn):
            btn.destroy()
        if _alive(btn.anchor):
            btn.anchor.grid_configure(padx=(0, 10))

    def _on_link(self, cid, on):
        self._touch(f"link:{cid}")
        self.link_btns[cid].set(on)
        self._linked = (self._linked | {cid}) if on else (self._linked - {cid})
        name = self.strips[cid]["channel"].get("name", cid)
        self.status((_("“{channel}”: sliders linked – all mixes move together") if on
                     else _("“{channel}”: sliders unlinked")).format(channel=name))
        self.worker.submit(lambda: self.mixer.set_linked(cid, on))

    def _on_mute(self, cid, mix):
        s = self.strips[cid][mix]
        on = s["mute"].get()
        s["fader"].set_dimmed(on)
        self._touch(f"mute:{cid}:{mix}")
        self.worker.submit(lambda: self.mixer.set_mute(cid, mix, on))

    def _on_master(self, cid, value):
        pct = int(round(float(value)))
        self.strips[cid]["master"]["val"].set(f"{pct} %")
        self._debounced(f"master:{cid}", lambda v: self.mixer.set_master(cid, v), pct)

    def _on_master_mute(self, cid):
        s = self.strips[cid]["master"]
        on = s["mute"].get()
        s["fader"].set_dimmed(on)
        self._touch(f"mmute:{cid}")
        self.worker.submit(lambda: self.mixer.set_master_mute(cid, on))

    def _toggle_member(self, cid, mid, on):
        """Nur die eine Zelle tauschen – sofort, ohne die Tabelle neu aufzubauen."""
        self._touch("member")
        cell = getattr(self, "cells", {}).get((cid, mid))
        if cell:
            widget, row, col, m = cell
            badge = self.strips.get(cid, {}).get(mid, {}).get("remove")
            if badge is not None:
                badge.destroy()
            widget.destroy()
            cur = next((list(x[4]) for x in (self._strip_ids or ((), ()))[1] if x[0] == mid), m.get("exclude") or [])
            excl = [x for x in cur if x != cid] + ([] if on else [cid])
            m = dict(m, exclude=excl)
            self._place_cell(self.table, cid, m, row, col, on)
            if on:
                self.strips[cid][mid]["fader"].set_dimmed(True)  # bis der Ausgang steht
            self._strip_ids = self._ids_with(mid, excl)

        def done(_res, err):
            if err is None:
                self.status(_("Channel added to the mix") if on else _("Channel removed from the mix"))
            else:
                self._strip_ids = None  # fehlgeschlagen: Tabelle aus dem echten Zustand neu aufbauen
            self.refresh()
        self.worker.submit(lambda: self.mixer.set_member(cid, mid, on), done)

    def _ids_with(self, mid, excl):
        """Kennung der Tabelle (siehe apply_state) mit geänderter Zuordnung eines Mixes."""
        if not self._strip_ids:
            return None
        chans, mixes = self._strip_ids
        return chans, tuple(x[:4] + (tuple(excl),) + x[5:] if x[0] == mid else x for x in mixes)

    def _on_listen(self, mid):
        cur = getattr(self, "_listen_now", None) or self.state.get("listen") or "monitor"
        target = "monitor" if mid == cur else mid
        self._listen_now = target  # gilt sofort, auch vor der nächsten Aktualisierung
        self._touch("listen")
        for k, h in getattr(self, "mix_heads", {}).items():
            h["ear"].set(k == target)

        def done(_res, err):
            if err is None:
                name = next((m["name"] for m in self.state.get("mixes") or [] if m["id"] == target), target)
                self.status(_("Listening to “{mix}”").format(mix=name))
            self.refresh()
        self.worker.submit(lambda: self.mixer.listen(target), done)

    def _on_output(self, _e=None):
        lbl = self.out_var.get()
        name = next((n for n, d in self.state.get("outputs", []) if d == lbl), None)
        if not name:
            return
        self._touch("output")
        if getattr(self, "listen_btn", None) is not None:
            self.listen_btn.configure_text(text=lbl)

        def done(res, err):
            if err is None:
                self.status(_("Personal Mix → {output}").format(output=lbl))
            self.refresh()
        self.worker.submit(lambda: self.mixer.set_monitor_output(name), done)

    # ------------------------------------------------------- Mixe anlegen/bearbeiten
    def _new_mix(self):
        self._mix_dialog(None)

    def _edit_mix(self, m):
        self._mix_dialog(m)

    def _current(self, key, xid):
        """Mix oder Kanal (key "mixes"/"channels") im aktuellen Zustand. Klicks merken sich nur die id: die
        Tabelle wird bei geänderten Ausgängen oder Geräten nicht neu gebaut, ein beim Aufbau gemerkter Stand
        wäre veraltet – und Speichern nähme spätere Änderungen wieder zurück."""
        return next((x for x in self.state.get(key) or [] if x["id"] == xid), None)

    def _edit_mix_id(self, mid):
        m = self._current("mixes", mid) if self.state.get("mixer_active") else None
        if m is not None:  # Mixer aus oder Mix gerade gelöscht: nichts zu bearbeiten
            self._edit_mix(m)

    def _mix_dialog(self, m):
        from .wavedialogs import MixDialog
        outs = self.state.get("outputs") or []
        cur = list((m or {}).get("devices") or ((m or {}).get("devices_eff") if (m or {}).get("id") == "monitor"
                                                  else []) or [])
        MixDialog(self.root, outs, mix=m, current_devices=cur, on_save=lambda v: self._save_mix(m, v),
                  on_delete=(lambda: self._delete_mix(m)) if m else None)

    def _save_mix(self, m, v):
        self._force_table()  # gleich abgleichen, auch während einer Zuordnung

        def work():
            if m is None:
                # neuer Mix startet leer: welche Kanäle hinein, wählt man selbst in der Tabelle
                return self.mixer.add_mix(v["name"], v["icon"], v["color"], v["devices"], v["virtual"],
                                          exclude=[c["id"] for c in self.mixer.all_channels()])
            return self.mixer.update_mix(m["id"], **v)

        def done(res, err):
            if err is None:
                self.status((_("Mix “{name}” created") if m is None else _("Mix “{name}” saved"))
                            .format(name=res["name"]))
            self._force_table()  # jetzt ist die Änderung da: die nächste Abfrage muss abgleichen
            self.refresh()
        self.worker.submit(work, done)

    def _delete_mix(self, m):
        from . import msgbox as messagebox
        if not messagebox.askyesno(_("Delete mix"), _("Really delete mix “{name}”?").format(name=m["name"]),
                                   parent=self.root):
            return
        self._force_table()

        def done(_res, err):
            if err is None:
                self.status(_("Mix “{name}” deleted").format(name=m["name"]))
            self._force_table()  # jetzt ist die Änderung da: die nächste Abfrage muss abgleichen
            self.refresh()
        self.worker.submit(lambda: self.mixer.remove_mix(m["id"]), done)

    # ------------------------------------------------------- Programme
    def _app_channels(self, channels):
        return [c for c in channels if c["id"] != "mic" and c.get("kind", "apps") == "apps"]

    def _stream_menu(self, s, chip):
        """Kanal für ein Programm wählen."""
        channels = self.state.get("channels") or []
        menu = PopupMenu(self.root)
        for c in self._app_channels(channels):
            glyph, _color = channel_look(c)
            menu.add(c["name"], lambda cid=c["id"]: self._on_stream(s, cid),
                     icon="check" if c["id"] == s["channel"] else glyph)
        menu.below(chip)
        return menu

    def _chip_for(self, parent, s, by_id):
        ch = by_id.get(s["channel"]) if s["channel"] else None
        glyph, color = channel_look(ch or s["channel"]) if s["channel"] else ("speaker", COL["faint"])
        target = ch["name"] if ch else NOT_IN_MIXER
        return AppChip(parent, s["app"], target, icon_tile(self.root, glyph, 24, color, radius=0.3),
                       command=lambda chip, s=s: self._stream_menu(s, chip),
                       drag=lambda chip, phase, x, y, s=s: self._drag_app(s, chip, phase, x, y))

    # Programme per Ziehen in einen Kanal legen (wie in Wave Link)
    def _drop_target(self, x, y):
        """Kanal-id unter dem Mauszeiger (Kanalkarte in „Apps“ oder Kanalzeile im Mixer) oder None."""
        w = self.root.winfo_containing(x, y)
        targets = getattr(self, "_drop_targets", {})
        while w is not None:
            cid = targets.get(w)
            if cid is not None:
                return cid, w
            w = w.master
        return None, None

    def _drag_app(self, s, chip, phase, x, y):
        if phase == "start":
            self._drag_reset()  # ein übrig gebliebenes Geisterbild nicht überschreiben (bliebe für immer stehen)
            ghost = self._drag_ghost = tk.Toplevel(self.root)
            ghost.overrideredirect(True)
            ghost.attributes("-topmost", True)
            tk.Label(ghost, text=s["app"], bg=COL["accent"], fg="#ffffff", padx=10, pady=4,
                     font=theme.font(self.root, 10, "bold")).pack()
        ghost = getattr(self, "_drag_ghost", None)
        if ghost is not None and phase in ("start", "move"):
            self._touch("drag")
            ghost.geometry(f"+{x + 12}+{y + 8}")
        cid, card = self._drop_target(x, y) if ghost is not None else (None, None)
        old = getattr(self, "_drag_over", None)
        if old is not None and old is not card and old.winfo_exists():
            old.outline = COL["border"]
            old._draw()
        self._drag_over = card if phase != "drop" else None
        if card is not None and phase != "drop" and isinstance(card, Card):
            card.outline = COL["accent"]
            card._draw()
        if phase == "drop":
            if ghost is not None:
                ghost.destroy()
            self._drag_ghost = None
            if cid is not None and card is not None and isinstance(card, Card):
                card.outline = COL["border"]
                card._draw()
            if cid and cid != s.get("channel"):
                self._on_stream(s, cid)

    def _dragging(self):
        """Wird gerade ein Programm gezogen? Dann die Ansicht „Apps“ nicht neu bauen – das gezogene Feld
        verschwände samt seinem Loslassen. Ohne Bewegung seit DRAG_STALE s gilt das Ziehen als abgebrochen."""
        return (getattr(self, "_drag_ghost", None) is not None
                and time.time() - self._touched.get("drag", 0) < DRAG_STALE)

    def _drag_reset(self):
        """Ziehen beenden: Geisterbild schließen, hervorgehobene Karte zurücksetzen."""
        ghost, self._drag_ghost = getattr(self, "_drag_ghost", None), None
        if ghost is not None and _alive(ghost):
            ghost.destroy()
        old, self._drag_over = getattr(self, "_drag_over", None), None
        if old is not None and _alive(old):
            old.outline = COL["border"]
            old._draw()

    def _build_apps_view(self):
        t = self.view_apps
        self.apps_list = tk.Frame(t, bg=COL["bg"])
        self.apps_list.pack(fill="both", expand=True, padx=22, pady=18)

    def _fill_apps_view(self, streams, channels):
        self._drag_reset()  # die Felder werden neu gebaut: ein angefangenes Ziehen ist vorbei
        for w in self.apps_list.winfo_children():
            w.destroy()
        self._drop_targets = {w: c for w, c in getattr(self, "_drop_targets", {}).items() if _alive(w)}
        by_id = {c["id"]: c for c in channels}
        groups = [(c, [s for s in streams if s["channel"] == c["id"]]) for c in self._app_channels(channels)]
        loose = [s for s in streams if not s["channel"] or s["channel"] not in by_id]
        if loose:
            groups.append((None, loose))
        cols = 3
        for i, (c, items) in enumerate(groups):
            r, k = divmod(i, cols)
            card = Card(self.apps_list, pad=14, radius=14, stretch=True)
            card.grid(row=r, column=k, sticky="nsew", padx=(0, 12), pady=(0, 12))
            if c is not None:
                self.__dict__.setdefault("_drop_targets", {})[card] = c["id"]
            b = card.body
            top = tk.Frame(b, bg=COL["panel"])
            top.pack(fill="x")
            if c is not None:
                glyph, color = channel_look(c)
                tk.Label(top, image=icon_tile(self.root, glyph, 30, color, radius=0.3),
                         bg=COL["panel"]).pack(side="left")
                label(top, c["name"], 10, "bold").pack(side="left", padx=10)
            else:
                tk.Label(top, image=icon_tile(self.root, "speaker", 30, COL["faint"], radius=0.3),
                         bg=COL["panel"]).pack(side="left")
                label(top, NOT_IN_MIXER, 10, "bold").pack(side="left", padx=10)
            label(top, str(len(items)), 9, color=COL["faint"]).pack(side="right")
            box = tk.Frame(b, bg=COL["panel"])
            box.pack(fill="x", pady=(10, 0))
            if not items:
                label(box, _("No app is playing audio right now."), 9, color=COL["faint"]).pack(anchor="w")
            for s in items:
                self._chip_for(box, s, by_id).pack(anchor="w", pady=3)
        for k in range(cols):
            self.apps_list.grid_columnconfigure(k, weight=1, uniform="apps")

    # ------------------------------------------------------- Effekte (Ansicht)
    def _build_fx_view(self):
        t = self.view_fx
        self.fx_list = tk.Frame(t, bg=COL["bg"])
        self.fx_list.pack(fill="both", expand=True, padx=22, pady=18)
        label(self.fx_list, _("Set up the mixer to add effects to channels."), color=COL["muted"]).pack(anchor="w")

    def _fill_fx_view(self, channels):
        for w in self.fx_list.winfo_children():
            w.destroy()
        ordered = sorted(channels, key=lambda c: c["id"] != "mic")
        cols = 2
        for i, ch in enumerate(ordered):
            r, k = divmod(i, cols)
            card = Card(self.fx_list, pad=12, radius=14, stretch=True)
            card.grid(row=r, column=k, sticky="nsew", padx=(0, 12), pady=(0, 10))
            b = card.body
            glyph, color = channel_look(ch)
            tk.Label(b, image=icon_tile(self.root, glyph, 34, color, radius=0.3), bg=COL["panel"]).pack(side="left")
            txt = tk.Frame(b, bg=COL["panel"])
            txt.pack(side="left", padx=12, fill="x", expand=True)
            label(txt, ch["name"], 10, "bold").pack(anchor="w")
            names = ch.get("fx_names") or []
            total = ch.get("fx_total", len(names))
            if names:
                summary = " · ".join(names)
            elif total:
                summary = _("All effects are switched off.")
            else:
                summary = _("No effects yet")
            label(txt, summary, 9, color=COL["accent"] if names else COL["muted"], anchor="w").pack(anchor="w")
            Button(b, _("Edit"), "fx", command=lambda c=ch: self._open_effects(c)).pack(side="right")
            if names:
                label(b, str(len(names)), 9, "bold", color=COL["accent"]).pack(side="right", padx=10)
        for k in range(cols):
            self.fx_list.grid_columnconfigure(k, weight=1, uniform="fx")

    # ------------------------------------------------- eigene Kanäle
    def _new_channel(self):
        self._channel_dialog(None)

    def _edit_channel(self, ch):
        self._channel_dialog(ch)

    def _edit_channel_id(self, cid):
        ch = self._current("channels", cid)
        if ch is not None:
            self._edit_channel(ch)

    def _channel_dialog(self, ch):
        from .wavedialogs import ChannelDialog
        with_apps = ch is None or (ch.get("kind", "apps") == "apps" and ch["id"] != "mic")

        def gather():
            c = ch
            if ch is not None and ch["id"] == "mic":  # das Gerät des Mikrofon-Kanals steht in mixer.mic
                c = dict(ch, source=self.mixer.conf.get("mic") or "")
            return c, self.mixer.inputs(), (self._app_choices(ch) if with_apps else None)

        def opened(res, err):
            if err is not None:
                return
            c, inputs, apps = res
            ChannelDialog(self.root, inputs or [], channel=c, default_look=channel_look(ch or {"id": ""}),
                          on_save=lambda v: self._save_channel(ch, v),
                          on_delete=(lambda: self._delete_channel(ch)) if ch else None, apps=apps)
        self.worker.submit(gather, opened)

    def _app_choices(self, ch):
        """Für den Reiter „Apps“: zugeordnete, gerade spielende und installierte Programme (im Worker)."""
        from . import desktop
        if getattr(self, "_app_entries", None) is None:
            try:
                entries = desktop.app_entries()
            except Exception:  # noqa: BLE001
                entries = []
            for e in entries:
                e["icon_path"] = desktop.app_icon(e["icon"])
            self._app_entries = entries
        installed = self._app_entries
        try:
            running = self.mixer.streams()
        except RuntimeError:
            running = []
        known = [s["names"] for s in running] + [" ".join((e["key"], e["cls"], e["name"])) for e in installed]
        routes = self.mixer.channel_routes(ch["id"], known) if ch else []
        names = {c["id"]: c["name"] for c in self.mixer.app_channels()}
        cid = ch["id"] if ch else None
        elsewhere = {k: names[v] for k, v in self.mixer.routes().items() if v in names and v != cid}
        return {"routes": routes, "running": running, "installed": installed, "elsewhere": elsewhere}

    def _save_channel(self, ch, v):
        self._force_table()  # gleich abgleichen, auch während einer Zuordnung

        def work():
            if ch is None:
                # neuer Kanal startet in keinem Mix: Zuordnung wählt man selbst in der Tabelle – außer er bekommt
                # gleich Programme; deren laufende Streams wandern sofort hinein und wären sonst stumm
                res = self.mixer.add_channel(v["name"], v["kind"], v["source"], v["icon"], v["color"],
                                             in_mixes=bool(v.get("apps_add")))
            else:
                changes = {"icon": v["icon"], "color": v["color"]}
                if ch["id"] != "mic":
                    changes["name"] = v["name"]
                # Gerät nur, wenn man ein anderes gewählt hat – sonst würde z. B. „automatisch“ beim Mikrofon
                # auf das erste Gerät der Liste festgelegt
                if (ch.get("kind") == "input" or ch["id"] == "mic") and v.get("source_changed"):
                    changes["source"] = v["source"]
                res = self.mixer.update_channel(ch["id"], **changes)
            if v.get("apps_add") or v.get("apps_remove"):
                self.mixer.set_channel_routes(res["id"], add=v.get("apps_add", ()), remove=v.get("apps_remove", ()))
            return res

        def done(res, err):
            if err is None:
                self.status((_("Channel “{name}” created") if ch is None else _("Channel “{name}” saved"))
                            .format(name=res["name"]))
            self._force_table()  # jetzt ist die Änderung da: die nächste Abfrage muss abgleichen
            self._stream_keys = None
            self._apps_ids = None
            self.refresh()
        self.worker.submit(work, done)

    def _delete_channel(self, ch):
        from . import msgbox as messagebox
        if not messagebox.askyesno(_("Delete channel"),
                                   _("Really delete channel “{name}”?").format(name=ch["name"]),
                                   parent=self.root):
            return
        self._force_table()

        def done(res, err):
            if err is None:
                self.status(_("Channel “{name}” deleted").format(name=ch["name"]))
            self._force_table()  # jetzt ist die Änderung da: die nächste Abfrage muss abgleichen
            self.refresh()
        self.worker.submit(lambda: self.mixer.remove_channel(ch["id"]), done)

    # ------------------------------------------------------- Szenen
    def _scene_menu(self):
        """Szenen-Menü am Knopf im Kopf: anwenden, aktuelle Einstellung speichern, verwalten."""
        scenes = self.state.get("scenes") or []
        active = self.state.get("active_scene") or ""
        menu = PopupMenu(self.root, width=260)
        for sc in scenes:
            menu.add(sc.get("name") or sc["id"], lambda sc=sc: self._apply_scene(sc),
                     icon="check" if sc["id"] == active else "layers")
        menu.separator()
        menu.add(_("Save current mix as scene …"), self._new_scene, icon="plus")
        menu.add(_("Manage scenes …"), self.open_scenes, icon="edit")
        menu.below(self.scene_btn)
        return menu

    def _scene_keys(self, st=None):
        keys = (st or self.state).get("shortcuts") or {}
        return {k[len(SCENE_SHORTCUT):]: v.get("binding", "") for k, v in keys.items()
                if k.startswith(SCENE_SHORTCUT)}

    def open_deck_export(self):
        """Kanäle als neue Seite in ein FreeDeck-Profil legen."""
        from .wavedialogs import DeckExportDialog
        chans = [(c["id"], c.get("name") or c["id"]) for c in self.state.get("channels") or []]

        def done(profile, created):
            self.status((_("Profile “{name}” created – choose it in FreeDeck for your device.") if created
                         else _("Profile “{name}” updated.")).format(name=profile))
        return DeckExportDialog(self.root, chans, on_done=done)

    @staticmethod
    def _sync_deck(ids, mixes=None, force=False):
        from . import deckexport
        return deckexport.sync(list(ids), mixes=list(mixes) if mixes is not None else None, force=force)

    @staticmethod
    def _adopt_decks():
        from . import deckexport
        return deckexport.adopt_new_devices()

    def _deck_synced(self, changed, err):
        if err is None and changed:
            self.status(_("Profile “{name}” updated.").format(name=", ".join(changed)))

    def open_scenes(self):
        from .wavedialogs import ScenesDialog
        dlg = getattr(self, "_scenes_dlg", None)
        if dlg is not None and dlg.winfo_exists():
            dlg.lift()
            return dlg
        self._scenes_dlg = ScenesDialog(
            self.root, self.state.get("scenes") or [], self.state.get("active_scene"), self._scene_keys(),
            {"apply": self._apply_scene, "rename": self._rename_scene, "delete": self._delete_scene,
             "new": self._new_scene, "shortcut": self._scene_shortcut})
        return self._scenes_dlg

    def _scene_job(self, work, message):
        def done(res, err):
            if err is None and res is not None:
                self.status(message.format(name=res.get("name", "")))
            self.refresh()
        self.worker.submit(work, done)

    def _apply_scene(self, sc):
        self._touched.clear()  # Regler sofort auf die Szene springen lassen
        self._scene_job(lambda: self.mixer.apply_scene(sc["id"]), _("Scene “{name}” applied"))

    def _new_scene(self):
        from .wavedialogs import SceneNameDialog
        active = next((x for x in self.state.get("scenes") or [] if x["id"] == self.state.get("active_scene")), None)
        SceneNameDialog(self.root, name=(active or {}).get("name", ""),
                        on_save=lambda name: self._scene_job(lambda: self.mixer.save_scene(name),
                                                             _("Scene “{name}” saved")))

    def _rename_scene(self, sc):
        from .wavedialogs import SceneNameDialog

        def work(name):
            res = self.mixer.rename_scene(sc["id"], name)
            shortcuts.update_command(SCENE_SHORTCUT + res["id"], self._scene_command(res),
                                     name=self._scene_shortcut_name(res))
            return res
        SceneNameDialog(self.root, name=sc.get("name", ""), rename=True,
                        on_save=lambda name: self._scene_job(lambda: work(name), _("Scene renamed to “{name}”")))

    def _delete_scene(self, sc):
        from . import msgbox as messagebox
        if not messagebox.askyesno(_("Delete scene"),
                                   _("Really delete scene “{name}”?").format(name=sc.get("name")), parent=self.root):
            return

        def work():
            res = self.mixer.delete_scene(sc["id"])
            shortcuts.unregister(SCENE_SHORTCUT + res["id"])
            return res
        self._scene_job(work, _("Scene “{name}” deleted"))

    @staticmethod
    def _scene_command(sc):
        return shortcuts.linewave_command("mixer", "scene", "apply", sc["id"])

    @staticmethod
    def _scene_shortcut_name(sc):
        return _("FreeWave: apply scene “{name}”").format(name=sc.get("name") or sc["id"])

    def _scene_shortcut(self, sc, binding):
        self._set_shortcut(SCENE_SHORTCUT + sc["id"], self._scene_shortcut_name(sc), self._scene_command(sc),
                           binding)

    # --------------------------------------------------- Tastenkürzel
    def _set_shortcut(self, ident, name, command, binding):
        """Globales Tastenkürzel eintragen (binding) oder entfernen (None) – im Arbeits-Thread."""
        self._touch(f"shortcut:{ident}")  # ältere Zustände überschreiben das Feld solange nicht

        def work():
            if not binding:
                shortcuts.unregister(ident)
                return None
            return shortcuts.register(ident, name, command, binding)

        def done(res, err):
            if isinstance(err, shortcuts.Unsupported):
                self.status("")
                from .dialogkit import ManualShortcutDialog
                ManualShortcutDialog(self.root, str(err), err.command)
            elif err is None:
                if binding:
                    msg = _("Shortcut {keys} set").format(keys=shortcuts.label(binding))
                    if res and res.get("note"):
                        msg += " – " + res["note"]
                    self.status(msg)
                else:
                    self.status(_("Shortcut removed"))
            self.refresh()
        self.worker.submit(work, done)

    def _on_mic_shortcut(self, binding):
        self._set_shortcut(MIC_SHORTCUT, _("FreeWave: mute microphone"),
                           shortcuts.linewave_command("mic", "mute", "toggle"), binding)

    # ------------------------------------------------------- Effekte
    def _open_effects(self, ch):
        from . import effects
        from .wavedialogs import EffectsDialog
        self.status(_("Loading effects …"))

        def opened(cat, err):
            if err is not None:
                return
            self.status("")
            EffectsDialog(self.root, ch, self.mixer.fx.chain(ch["id"]), cat,
                          on_change=lambda chain, rebuild: self._fx_changed(ch, chain),
                          on_control=lambda chain, i, name, value: self._fx_control(ch, chain, i, name, value))
        self.worker.submit(effects.catalog, opened)

    def _fx_changed(self, ch, chain):
        """Kette geändert (hinzugefügt, entfernt, an/aus, verschoben): speichern und Kanal neu verbinden."""
        chain = [dict(fx, controls=dict(fx["controls"])) for fx in chain]
        self._strip_ids = None

        def work():
            self.mixer.fx.set_chain(ch["id"], chain)
            if self.mixer.is_active():
                self.mixer.rebuild_channel(ch["id"])
        self._debounced(f"fx:{ch['id']}", lambda _v: work(), None, delay=350)

    def _fx_control(self, ch, chain, i, name, value):
        """Regler bewegt: live setzen (pw-cli), sonst Kette neu starten."""
        chain = [dict(fx, controls=dict(fx["controls"])) for fx in chain]

        def work():
            self.mixer.fx.set_chain(ch["id"], chain)
            if self.mixer.is_active() and not self.mixer.fx.set_live(ch["id"], i, name, value):
                self.mixer.rebuild_channel(ch["id"])
        self._debounced(f"fxc:{ch['id']}", lambda _v: work(), None, delay=300)

    def _on_stream(self, s, cid):
        if not cid:
            return
        self._touch("streams")

        def done(res, err):
            if err is None:
                self.status(_("{app} → channel “{channel}” (remembered)").format(app=s["app"], channel=cid))
            self._stream_keys = None
            self._apps_ids = None
            self.refresh()
        self.worker.submit(lambda: self.mixer.move_stream(s["index"], cid, remember=s.get("key", "")), done)

    # ------------------------------------------------------------ Mikrofon
    def _row(self, parent, title, sub=""):
        row = tk.Frame(parent, bg=COL["panel"])
        row.pack(fill="x", pady=8)
        txt = tk.Frame(row, bg=COL["panel"])
        txt.pack(side="left", fill="x", expand=True)
        label(txt, title, 10, "bold").pack(anchor="w")
        if sub:
            label(txt, sub, 9, color=COL["muted"], wraplength=330, justify="left").pack(anchor="w")
        return row

    def _build_mic(self):
        area = ScrollArea(self.view_mic, bg=COL["bg"])
        area.pack(fill="both", expand=True)
        t = tk.Frame(area.inner, bg=COL["bg"])
        t.pack(fill="both", expand=True, padx=22, pady=18)
        self.v = {}
        # --- Karte Mikrofon
        card = Card(t, pad=20, radius=16)
        card.pack(side="left", anchor="n", padx=(0, 14))
        b = card.body
        top = tk.Frame(b, bg=COL["panel"])
        top.pack(fill="x")
        tk.Label(top, image=icon_tile(self.root, "mic", 44, CHANNEL_LOOK["mic"][1], radius=0.3),
                 bg=COL["panel"]).pack(side="left")
        names = tk.Frame(top, bg=COL["panel"])
        names.pack(side="left", padx=14)
        self.mic_name = tk.StringVar(self.root, value="")
        label(names, textvariable=self.mic_name, size=13, weight="bold").pack(anchor="w")
        self.mic_hint = tk.StringVar(self.root, value="")
        label(names, textvariable=self.mic_hint, size=9, color=COL["muted"], wraplength=360,
              justify="left").pack(anchor="w")

        theme.section(b, "Gain").pack(fill="x", pady=(22, 6))
        g = tk.Frame(b, bg=COL["panel"])
        g.pack(fill="x")
        self.gain = Fader(g, length=380, thickness=24, orient="horizontal", color=COL["accent"],
                          command=self._on_gain)
        self.gain.pack(side="left")
        self.v["mute"] = RoundToggle(g, "mic", "mic_off", size=40, command=self._on_mic_mute)
        self.v["mute"].pack(side="right")
        self.gain_val = tk.StringVar(self.root, value="–")
        label(b, textvariable=self.gain_val, size=10, weight="bold", color=COL["muted"]).pack(anchor="w",
                                                                                             pady=(4, 10))

        tk.Frame(b, bg=COL["border"], height=1).pack(fill="x", pady=8)
        theme.section(b, _("Filters")).pack(fill="x", pady=(4, 0))
        row = self._row(b, "Low Cut", _("Filters out low hum and footfall noise"))
        self.v["lowcut"] = Switch(row, command=lambda: self._on_filter("lowcut"))
        self.v["lowcut"].pack(side="right")
        self.freq_seg = Segmented(row, [("80", "80 Hz"), ("120", "120 Hz")], command=self._on_freq)
        self.freq_seg.pack(side="right", padx=12)
        self.freq = tk.StringVar(self.root, value="80")
        row = self._row(b, "Clipguard", _("Prevents clipping when you suddenly get loud"))
        self.v["clipguard"] = Switch(row, command=lambda: self._on_filter("clipguard"))
        self.v["clipguard"].pack(side="right")

        # --- globales Tastenkürzel zum Stummschalten
        from .dialogkit import ShortcutField
        tk.Frame(b, bg=COL["border"], height=1).pack(fill="x", pady=8)
        theme.section(b, _("Keyboard shortcut")).pack(fill="x", pady=(4, 0))
        row = self._row(b, _("Mute microphone"), _("Works in every app, also on Wayland – it is added to your "
                                                   "desktop's keyboard shortcuts."))
        self.mic_shortcut = ShortcutField(b, on_set=self._on_mic_shortcut,
                                          on_remove=lambda: self._on_mic_shortcut(None))
        self.mic_shortcut.pack(anchor="w", pady=(0, 4))

        # --- Karte Wave:3 (nur mit Hardware)
        outer = tk.Frame(t, bg=COL["bg"])
        outer.pack(side="left", anchor="n", fill="x", expand=True)
        right = tk.Frame(outer, bg=COL["bg"])  # Spalte: Wave:3-Karte und Auto-Ducking gleich breit
        right.pack(anchor="nw")
        hw_slot = tk.Frame(right, bg=COL["bg"])
        hw_slot.pack(fill="x")
        self.hw_frame = Card(hw_slot, pad=20, radius=16)
        h = self.hw_frame.body
        theme.section(h, "Wave:3").pack(fill="x")
        row = self._row(h, _("Headphones"))
        self.v["headphone_mute"] = RoundToggle(row, "headphones", "headphones_off", size=36,
                                               command=lambda: self._on_hw_bool("headphone_mute"))
        self.v["headphone_mute"].pack(side="right")
        self.hp_val = tk.StringVar(self.root, value="–")
        label(row, textvariable=self.hp_val, color=COL["muted"], width=8).pack(side="right", padx=8)
        self.hp = Fader(h, length=360, thickness=24, orient="horizontal", from_=-60, to=0, color=COL["monitor"],
                        command=lambda v: self._on_hw_level("headphone_volume", v, self.hp_val, "{value:.0f} dB"))
        self.hp.pack(anchor="w")
        row = self._row(h, _("Monitoring"), _("Balance of mic ↔ PC audio in your headphones"))
        self.dm_val = tk.StringVar(self.root, value="–")
        label(row, textvariable=self.dm_val, color=COL["muted"], width=12).pack(side="right")
        mixrow = tk.Frame(h, bg=COL["panel"])
        mixrow.pack(anchor="w")
        label(mixrow, "PC", 9, color=COL["faint"]).pack(side="left", padx=(0, 6))
        self.dm = Fader(mixrow, length=320, thickness=24, orient="horizontal", from_=0, to=100,
                        color=CHANNEL_LOOK["mic"][1],
                        command=lambda v: self._on_hw_level("direct_monitor", v, self.dm_val,
                                                          _("{value:.0f} % mic")))
        self.dm.pack(side="left")
        label(mixrow, _("Mic"), 9, color=COL["faint"]).pack(side="left", padx=(6, 0))
        tk.Frame(h, bg=COL["border"], height=1).pack(fill="x", pady=12)
        row = self._row(h, _("LEDs off"))
        self.v["leds_off"] = Switch(row, command=lambda: self._on_hw_bool("leds_off"))
        self.v["leds_off"].pack(side="right")
        row = self._row(h, _("Flip LEDs"), _("If the microphone is mounted upside down on a boom arm"))
        self.v["leds_flip"] = Switch(row, command=lambda: self._on_hw_bool("leds_flip"))
        self.v["leds_flip"].pack(side="right")

        self.no_hw = Card(hw_slot, pad=20, radius=16)
        label(self.no_hw.body, _("More settings with the Elgato Wave:3"), 10, "bold").pack(anchor="w")
        label(self.no_hw.body, _("Headphone volume, monitoring and the LEDs can be adjusted directly on the "
                                 "Wave:3. For Wave:1, Wave Neo and Wave XLR the protocol for this is not "
                                 "documented."), 9, color=COL["muted"], wraplength=360, justify="left").pack(
            anchor="w", pady=(6, 0))
        self.no_hw.pack(anchor="nw", fill="x")
        self._build_noise(right)
        self._build_ducking(right)

    # ------------------------------------------------------- Auto-Ducking
    def _build_ducking(self, parent):
        """Karte „Auto-Ducking“: Kanäle leiser, solange du sprichst."""
        card = Card(parent, pad=20, radius=16, stretch=True)
        card.pack(fill="x", pady=(14, 0))
        b = card.body
        self.duck = {}
        top = tk.Frame(b, bg=COL["panel"])
        top.pack(fill="x")
        theme.section(top, _("Auto-ducking")).pack(side="left")
        self.duck["enabled"] = Switch(top, command=lambda: self._on_duck("enabled", self.duck["enabled"].get()))
        self.duck["enabled"].pack(side="right")
        label(b, _("Turns the selected channels down in both mixes while you speak and brings them back "
                   "exactly afterwards."), 9, color=COL["muted"], wraplength=380, justify="left").pack(
            anchor="w", pady=(6, 0))
        theme.section(b, _("Channels")).pack(fill="x", pady=(16, 6))
        self.duck_chips = tk.Frame(b, bg=COL["panel"], width=380, height=1)
        self.duck_chips.pack(fill="x")
        self._duck_chip_ids = None
        theme.section(b, _("Lower by")).pack(fill="x", pady=(16, 6))
        self.duck["amount"] = SoftSegmented(b, [(str(a), f"−{a} dB", None) for a in ducking.AMOUNTS],
                                            command=lambda v: self._on_duck("amount_db", int(v)), value="12")
        self.duck["amount"].pack(anchor="w")
        row = tk.Frame(b, bg=COL["panel"])
        row.pack(fill="x", pady=(16, 6))
        theme.section(row, _("Sensitivity")).pack(side="left")
        self.duck_thr = tk.StringVar(self.root, value="")
        label(row, textvariable=self.duck_thr, size=9, color=COL["muted"]).pack(side="right")
        self.duck["threshold"] = Fader(b, length=360, thickness=22, orient="horizontal", from_=-60, to=-5,
                                       color=COL["accent"], command=self._on_duck_threshold)
        self.duck["threshold"].pack(anchor="w")
        self.duck_meter = DuckMeter(b, 360)
        self.duck_meter.pack(anchor="w", pady=(8, 0))
        self.duck_state = tk.StringVar(self.root, value="")
        label(b, textvariable=self.duck_state, size=9, color=COL["muted"], wraplength=380, justify="left").pack(
            anchor="w", pady=(6, 0))
        self.root.after(DUCK_MS, self._tick_duck)

    def _duck_conf(self):
        return ducking.settings(self.host.cfg)

    def _on_duck(self, key, value):
        self._touch(f"duck:{key}")

        def work():
            mx = self.host.cfg.data.setdefault("mixer", {})
            mx.setdefault("ducking", {})[key] = value
            self.host.cfg.save()
        self.worker.submit(work)

    def _on_duck_threshold(self, value):
        db = int(round(float(value)))
        self.duck_thr.set(f"{db} dB")
        self.duck_meter.set(threshold=db)
        self._debounced("duck:threshold_db", lambda v: self._on_duck("threshold_db", v), db, delay=300)

    def _toggle_duck_channel(self, cid):
        # vom Gezeigten ausgehen: die Einstellung schreibt erst der Arbeits-Thread – ist er noch beschäftigt,
        # stünde dort die Auswahl vor dem letzten Klick, und der ginge verloren
        chans = (list(self._duck_chip_ids[1]) if self._duck_chip_ids
                 else list(self._duck_conf().get("channels") or []))
        chans = [c for c in chans if c != cid] if cid in chans else chans + [cid]
        self._on_duck("channels", chans)
        self._fill_duck_chips(self.state.get("channels") or self._duck_channels_fallback(), chans)

    def _duck_channels_fallback(self):
        try:
            return self.mixer.channels()
        except Exception:  # noqa: BLE001
            return []

    def _fill_duck_chips(self, channels, selected):
        chans = [c for c in channels if c["id"] != "mic"]
        ids = (tuple((c["id"], c["name"]) for c in chans), tuple(selected))
        if ids == self._duck_chip_ids:
            return
        self._duck_chip_ids = ids
        for w in self.duck_chips.winfo_children():
            w.destroy()
        chips = [Chip(self.duck_chips, c["name"], active=c["id"] in selected,
                      command=lambda cid=c["id"]: self._toggle_duck_channel(cid)) for c in chans]
        if chips:
            self.root.update_idletasks()
            _flow(self.duck_chips, chips, gap=6)

    def _apply_duck(self, st):
        conf = self._duck_conf()
        if not self._held("duck:enabled"):
            self.duck["enabled"].set(bool(conf.get("enabled")))
        if not self._held("duck:amount_db"):
            self.duck["amount"].set(str(conf.get("amount_db")))
        if not self._held("duck:threshold_db"):
            thr = int(conf.get("threshold_db") or -30)
            self.duck["threshold"].set(thr)
            self.duck_thr.set(f"{thr} dB")
            self.duck_meter.set(threshold=thr)
        if not self._held("duck:channels"):
            self._fill_duck_chips(st.get("channels") or self._duck_channels_fallback(), conf.get("channels") or [])

    def _tick_duck(self):
        """Mikrofonpegel und Zustand des Auto-Duckings anzeigen (nur in der Ansicht Mikrofon)."""
        if self.quitting:
            return
        try:
            show = self.visible() and self.view == "mic"
        except tk.TclError:
            return
        if show:
            d = self.host.ducker
            conf = self._duck_conf()
            on = bool(conf.get("enabled"))
            self.duck_meter.set(level=d.level if on else 0.0, ducking=d.ducking)
            if not on:
                text = _("Switched off")
            elif not self.state.get("mixer_active"):
                text = _("Works as soon as the mixer is set up.")
            elif d.ducking:
                text = _("You are speaking – channels are lowered.")
            else:
                text = _("Listening – speak to see the level reach the marker.")
            self.duck_state.set(text)
        self.root.after(DUCK_MS, self._tick_duck)


    def _build_noise(self, parent):
        """Karte Rauschunterdrückung (für das Mikrofon im Mixer, jedes Mikrofon)."""
        card = Card(parent, pad=20, radius=16, stretch=True)
        card.pack(fill="x", pady=(14, 0))
        n = card.body
        row = self._row(n, _("Noise suppression"), _("Filters out keyboard, fans and background noise"))
        self.v["noise"] = Switch(row, command=self._on_noise)
        self.v["noise"].pack(side="right")
        self.noise_status = tk.StringVar(self.root, value="")
        label(n, textvariable=self.noise_status, size=9, color=COL["muted"], wraplength=360,
              justify="left").pack(anchor="w")
        self.noise_strength = tk.Frame(n, bg=COL["panel"])
        row = self._row(self.noise_strength, _("Strength"),
                        _("Higher values mute everything that does not clearly sound like a voice"))
        self.noise_val = tk.StringVar(self.root, value="")
        label(row, textvariable=self.noise_val, color=COL["muted"], width=6).pack(side="right")
        from .noise import MAX_STRENGTH
        self.noise_fader = Fader(self.noise_strength, length=360, thickness=24, orient="horizontal", from_=0,
                                 to=MAX_STRENGTH, color=CHANNEL_LOOK["mic"][1], command=self._on_noise_strength)
        self.noise_fader.pack(anchor="w")

    def _on_noise(self):
        on = self.v["noise"].get()
        self._touch("noise")
        ns = self.mixer.noise

        def work():
            ns.set(enabled=on)
            ns.apply()

        def done(_res, err):
            if err is None:
                self.status(_("Noise suppression on") if on else _("Noise suppression off"))
            self.refresh()
        self.worker.submit(work, done)

    def _on_noise_strength(self, value):
        v = int(round(float(value)))
        self.noise_val.set(f"{v} %")
        self._debounced("noise_strength", self.mixer.noise.set_strength, v, delay=300)

    def _apply_noise(self, ns):
        """Schalter, Statuszeile und Stärke-Regler der Rauschunterdrückung."""
        from . import noise
        have = ns.get("available") or []
        if not self._held("noise"):
            self.v["noise"].set(bool(ns.get("enabled")))
        self.v["noise"].set_enabled(bool(have) or bool(ns.get("enabled")))
        backend = noise.backend_name(ns.get("backend"))
        if not have:
            text = ns.get("hint") or ""
        elif not ns.get("enabled"):
            text = _("Off – method: {backend}").format(backend=backend)
        elif ns.get("running"):
            text = _("Active: {backend}. The microphone channel in the mixer uses the filtered signal; in "
                     "Discord/OBS you can also select “FreeWave Microphone (noise suppression)”.").format(
                backend=backend)
        elif not self.state.get("mixer_active"):
            text = _("Starts as soon as the mixer is set up ({backend}).").format(backend=backend)
        else:
            text = _("Could not start ({backend}).").format(backend=backend)
        self.noise_status.set(text)
        if ns.get("backend") == "rnnoise" and have:
            if not self.noise_strength.winfo_manager():
                self.noise_strength.pack(fill="x", pady=(4, 0))
            if not self._held("noise_strength"):
                self.noise_fader.set(ns.get("strength", 50))
                self.noise_val.set(f"{ns.get('strength', 50)} %")
        else:
            self.noise_strength.pack_forget()

    def _on_gain(self, value):
        pct = int(round(float(value)))
        self.gain_val.set(f"{pct} %")
        self._debounced("gain", self.mic.set_gain, pct)

    def _on_mic_mute(self):
        self._touch("mute")
        self.gain.set_dimmed(self.v["mute"].get())
        self.worker.submit(self.mic.toggle_mute)

    def _on_filter(self, key):
        on = self.v[key].get()
        self._touch(key)

        def work():
            if self.mic.hw():
                self.mic.hw_set(key, on)
                self.mic.settings[key] = on
                self.mic._save()
            else:
                self.mic.set(**{key: on})

        def done(res, err):
            if err is None:
                name = "Low Cut" if key == "lowcut" else "Clipguard"
                self.status((_("{name} on") if on else _("{name} off")).format(name=name))
        self.worker.submit(work, done)

    def _on_freq(self, value=None):
        f = int(value or self.freq.get())
        self.freq.set(str(f))
        self._touch("lowcut_freq")
        self.worker.submit(lambda: self.mic.set(lowcut_freq=f))

    def _on_hw_bool(self, key):
        on = self.v[key].get()
        self._touch(key)
        self.worker.submit(lambda: self.mic.hw_set(key, on))

    def _on_hw_level(self, key, value, lbl, fmt):
        v = float(value)
        lbl.set(fmt.format(value=v))
        self._debounced(key, lambda x: self.mic.hw_set(key, x), v)

    # ------------------------------------------------- Regler entprellen
    def _touch(self, key):
        self._touched[key] = time.time()

    def _held(self, key):
        return time.time() - self._touched.get(key, 0) < TOUCH_HOLD

    def _debounced(self, key, fn, value, delay=120):
        self._touch(key)
        self._pending[key] = (fn, value)
        if key not in self._timers:
            self._timers[key] = self.root.after(delay, lambda: self._fire(key))

    def _fire(self, key):
        self._timers.pop(key, None)
        fn, value = self._pending.pop(key, (None, None))
        if fn is not None:
            self.worker.submit(lambda: fn(value))

    # ------------------------------------------------------------- Pegel
    def _tick_levels(self):
        """25-mal pro Sekunde: Pegel lesen und anzeigen – nur bei sichtbarem Mixer."""
        if self.quitting:
            return
        try:
            show = self.visible() and self.view == "mixer" and bool(self.state.get("mixer_active"))
        except tk.TclError:
            return
        if not show:
            if self.levels.running:
                self.levels.stop()
            self._set_meters({})
            self.root.after(250, self._tick_levels)
            return
        self.levels.watch(self.state.get("level_sources") or {})
        self._set_meters(self.levels.read())
        self.root.after(LEVEL_MS, self._tick_levels)

    def _set_meters(self, lv):
        vols = self.state.get("levels") or {}
        for cid, strip in self.strips.items():
            # gemessen wird nach dem Hauptregler (Kanal-Sink bzw. Quelle)
            raw = lv.get(cid, 0.0)
            strip["master"]["meter"].set(raw)
            for mid, s in strip.items():
                if not isinstance(s, dict) or mid == "master" or "fader" not in s:
                    continue
                vol = s["fader"].get() if f"{cid}_{mid}" in vols else 0
                s["meter"].set(post_fader(raw, vol, s["mute"].get()))
        now = time.monotonic()
        for mid, m in self.mix_meters.items():
            v = lv.get(f"mix:{mid}", 0.0)
            m["meter"].set(v)
            db = levels.level_db(v)
            if db is not None and db >= CLIP_DB:
                m["clip_until"] = now + CLIP_HOLD  # übersteuert: kurz rot „CLIP“ halten
            clip = m.get("clip_until", 0) > now
            text = "CLIP" if clip else (f"{db:.0f} dB" if db is not None else ("−∞ dB" if self.levels.running else ""))
            if m.get("db_text") != text:  # Tk nur ansprechen, wenn sich die Anzeige ändert (25×/s)
                m["db_text"] = text
                m["db"].set(text)
                lb = m.get("label")
                if lb is not None:
                    lb.configure(fg=COL["danger"] if clip else COL["muted"])

    # ------------------------------------------------------ Aktualisieren
    def collect(self):
        """Zustand von Mixer und Mikrofon einlesen (läuft im Arbeits-Thread).

        Gleiche pactl-Abfragen laufen dabei nur einmal (system.pactl_snapshot)."""
        with system.pactl_snapshot():
            return self._collect()

    def _collect(self):
        st = {"mixer_enabled": self.host.mixer_enabled, "error": self.host.error, "mixer_active": False}
        try:
            st["mixer_active"] = self.mixer.is_active()
        except Exception as e:  # noqa: BLE001
            st["error"] = str(e)
        if st["mixer_active"]:
            chans = []
            for c in self.mixer.all_channels():
                chain = self.mixer.fx.chain(c["id"])
                on = [fx.get("name") or "FX" for fx in chain if fx.get("enabled", True)]
                names = on
                if c["id"] == "mic" and self.mixer.noise.enabled:  # Rauschunterdrückung vor den Effekten
                    names = [_("Noise suppression")] + on
                chans.append(dict(c, fx=len(on), fx_names=names, fx_total=len(chain)))
            st["channels"] = chans
            st["levels"] = self.mixer.levels()
            st["masters"] = self.mixer.masters()
            st["linked"] = [c["id"] for c in chans if self.mixer.is_linked(c["id"])]
            st["mixes"] = [dict(m, devices_eff=self.mixer.mix_devices(m), can_listen=self.mixer.can_listen(m["id"]))
                           for m in self.mixer.mixes()]
            st["listen"] = self.mixer.listening()
            st["outputs"] = [(o["name"], o.get("description") or o["name"]) for o in self.mixer.outputs()]
            st["monitor_output"] = self.mixer._state().get("monitor_output", "")
            st["streams"] = self.mixer.streams()
            # für Audio Flow und Soundboard: Lautstärken der Sinks (Mixe, Geräte), Namen der Quellen,
            # Ziele der Mixe und Programmsymbole – alles aus derselben pactl-Abfrage
            st["sink_levels"] = {b.get("name"): (int(b.get("volume", 100)), bool(b.get("mute")))
                                 for b in system._pactl_blocks("sinks") if b.get("name")}
            st["source_names"] = {b.get("name"): b.get("description") or b.get("name")
                                  for b in system._pactl_blocks("sources") if b.get("name")}
            st["mix_sinks"] = {m["id"]: self.mixer.mix_sink(m) for m in st["mixes"]}
            st["mix_targets"] = soundboard.mix_targets(self.mixer)
            st["app_icons"] = self._app_icons(st["streams"])
            if any(c.get("kind") == "input" for c in chans):
                try:
                    st["inputs"] = dict(self.mixer.inputs())
                except Exception:  # noqa: BLE001
                    st["inputs"] = {}
            if self.levels.available:
                st["level_sources"] = levels.channel_sources(
                    self.mixer, chans, st["monitor_output"] or self.mixer.default_sink())
        try:
            st["scenes"] = self.mixer.scenes()
            act = self.mixer.active_scene(st.get("levels")) if st["mixer_active"] else None
            st["active_scene"] = act["id"] if act else None
        except Exception:  # noqa: BLE001
            st["scenes"], st["active_scene"] = [], None
        st["shortcuts"] = shortcuts.registered()
        if has_freedeck():  # angeschlossene Stream Decks (USB) – für das FreeWave-Profil
            try:
                from .models import connected_models
                st["decks"] = tuple(sorted(d["model"] for d in connected_models()))
            except Exception:  # noqa: BLE001
                st["decks"] = ()
        mic = self.mic.status()
        if mic.get("hardware"):
            mic["gain_pct"] = int(round(mic.get("gain", 0) * 2.5))
        elif mic.get("source"):
            try:
                mic["gain_pct"], mic["mute"] = self.mic.gain()
            except Exception:  # noqa: BLE001
                pass
        st["mic"] = mic
        st["settings"] = dict(self.mic.settings)
        st["filter_running"] = self.mic.running()
        st["limiter"] = wavemic.limiter_available()
        try:
            st["noise"] = self.mixer.noise.status()
        except Exception:  # noqa: BLE001
            st["noise"] = {}
        return st

    def _app_icons(self, streams):
        """{Programm-Schlüssel: Pfad zum Symbol} für die spielenden Programme (im Arbeits-Thread, gemerkt)."""
        cache = self.__dict__.setdefault("_icon_paths", {})
        todo = [s for s in streams if (s.get("key") or "") not in cache]
        if todo:
            from . import desktop
            if getattr(self, "_app_entries", None) is None:
                try:
                    entries = desktop.app_entries()
                except Exception:  # noqa: BLE001
                    entries = []
                for e in entries:
                    e["icon_path"] = desktop.app_icon(e["icon"])
                self._app_entries = entries
            for s in todo:
                key, app = (s.get("key") or "").lower(), (s.get("app") or "").lower()
                hit = next((e for e in self._app_entries if e.get("icon_path") and key and (
                    (e.get("key") or "").lower() == key or (e.get("cls") or "").lower() == key)), None) or \
                    next((e for e in self._app_entries if e.get("icon_path") and app
                          and (e.get("name") or "").lower() == app), None)
                cache[s.get("key") or ""] = str(hit["icon_path"]) if hit else None
        return {s.get("key") or "": cache.get(s.get("key") or "") for s in streams}

    def _refresh_due(self):
        """Neu einlesen, wenn PipeWire eine Änderung meldet (gebündelt), sonst nur alle REFRESH_IDLE_MS –
        ohne Ereignisse (pactl subscribe fehlt) wie bisher alle REFRESH_MS."""
        age = time.time() - self._last_refresh
        pulse = getattr(self.host, "pulse", None)
        if pulse is None or not pulse.available:
            return age > REFRESH_MS / 1000
        if pulse.version != getattr(self, "_pulse_seen", None) and age > 0.25:
            self._pulse_seen = pulse.version
            return True
        return age > REFRESH_IDLE_MS / 1000

    def _force_table(self):
        """Die nächste Aktualisierung soll die Mixer-Tabelle abgleichen, auch während einer Zuordnung."""
        self._table_force = True
        self._force_seq = getattr(self, "_force_seq", 0) + 1

    def refresh(self):
        if self._refreshing:
            self._refresh_again = True  # läuft schon: danach gleich noch einmal (Zustand könnte veraltet sein)
            return
        self._refreshing = True
        self._refresh_again = False
        seq = getattr(self, "_force_seq", 0)  # welche Abgleich-Anforderungen diese Abfrage schon sieht

        def done(st, err):
            self._refreshing = False
            self._last_refresh = time.time()
            if err is None and st is not None:
                self._collect_seq = seq
                self.apply_state(st)
            if self._refresh_again:
                self.refresh()
        self.worker.submit(self.collect, done)

    def _channel_sub(self, ch, apps, st):
        if apps:
            return ", ".join(apps)
        if ch["id"] == "mic":
            mic = st.get("mic") or {}
            return "Wave:3" if mic.get("hardware") else ("Wave" if mic.get("source") else " ")
        if ch.get("kind") == "input":
            src = ch.get("source") or ""
            return (st.get("inputs") or {}).get(src, src) or " "
        return " "

    def apply_state(self, st):
        self.state = st
        # --- Mixer
        if st.get("mixer_active"):
            self.mix_off.pack_forget()
            self.mix_on.pack(fill="both", expand=True)
            mixes = st.get("mixes") or []
            ids = (tuple((c["id"], c["name"], c.get("icon"), c.get("color"), c.get("kind")) for c in st["channels"]),
                   tuple((m["id"], m["name"], m.get("icon"), m.get("color"), tuple(m.get("exclude") or []),
                          bool(m.get("can_listen"))) for m in mixes))
            # _strip_ids None = Neuaufbau verlangt (z. B. neuer Mix); sonst nicht, solange eine Zuordnung läuft
            # _strip_ids None = ganz neu aufbauen; sonst nur Geändertes abgleichen (nicht während einer
            # Zuordnung, außer ein neuer/geänderter Mix oder Kanal verlangt es)
            # Abgleich verlangt (neuer/gelöschter Mix oder Kanal) – zählt nur, wenn diese Abfrage nach der
            # Anforderung begann; eine ältere (mit dem Stand von vorher) darf ihn nicht verbrauchen
            force = getattr(self, "_table_force", False)
            fresh = getattr(self, "_collect_seq", 0) >= getattr(self, "_force_seq", 0)
            if ids != self._strip_ids and (self._strip_ids is None or force or not self._held("member")):
                old = self._strip_ids
                if fresh:
                    self._table_force = False
                try:
                    synced = old is not None and self._sync_table(st["channels"], mixes, old, ids)
                except Exception:  # noqa: BLE001 – lieber ganz neu aufbauen als halb stehen bleiben
                    log.exception("mixer table sync failed")
                    synced = False
                if not synced:
                    self._build_table(st["channels"], mixes)
                self._strip_ids = ids
            if ids[1] != self._mix_card_ids:
                self._mix_card_ids = ids[1]
                self._fill_mix_cards(mixes)
            self._place_links(mixes, set(st.get("linked") or []))
            levels_now = st.get("levels") or {}
            for cid, strip in self.strips.items():
                for mid, s in strip.items():
                    if mid == "master" or not isinstance(s, dict) or "fader" not in s:
                        continue
                    if f"{cid}_{mid}" not in levels_now:  # Mix ohne Ausgang: kein Regler
                        s["val"].set("–")
                        s["fader"].set_dimmed(True)
            for key, (vol, mute) in levels_now.items():
                cid, mix = key.rsplit("_", 1)
                s = self.strips.get(cid, {}).get(mix)
                if not s:
                    continue
                if not self._held(f"lvl:{cid}:{mix}"):
                    s["fader"].set(min(100, vol))
                    s["val"].set(f"{vol} %")
                    s["pct"] = vol
                if not self._held(f"mute:{cid}:{mix}"):
                    s["mute"].set(bool(mute))
                    s["fader"].set_dimmed(bool(mute))
            hear = (self.strips.get("mic") or {}).get("hear")
            if hear is not None and not self._held("hear"):
                lv = levels_now.get("mic_monitor")
                hear.set(lv is not None and not lv[1])
            for cid, (vol, mute) in (st.get("masters") or {}).items():
                s = self.strips.get(cid, {}).get("master")
                if not s:
                    continue
                if not self._held(f"master:{cid}"):
                    s["fader"].set(min(100, vol))
                    s["val"].set(f"{vol} %")
                if not self._held(f"mmute:{cid}"):
                    s["mute"].set(bool(mute))
                    s["fader"].set_dimmed(bool(mute))
            apps = {}
            for s in st.get("streams") or []:
                if s["channel"]:
                    apps.setdefault(s["channel"], []).append(s["app"])
            by_id = {c["id"]: c for c in st["channels"]}
            sub_font = theme.font(self.root, 8)
            for cid, strip in self.strips.items():
                ch = by_id.get(cid, strip["channel"])
                strip["apps"].set(_fit_text(self.root, sub_font, self._channel_sub(ch, apps.get(cid, []), st), 112))
                strip["fx"].set(ch.get("fx_names") or [])
            listen = st.get("listen") or "monitor"
            if not self._held("listen"):
                self._listen_now = listen
            for m in mixes:
                h = self.mix_heads.get(m["id"])
                if h is not None:
                    h["sub"].set(self._mix_sub(m))
                    if not self._held("listen"):
                        h["ear"].set(m["id"] == listen)
                card = self.mix_cards.get(m["id"])
                if card is not None:
                    sub = self._mix_sub(m)
                    if m["id"] == listen and listen != "monitor":
                        sub = _("listening") + " · " + sub
                    card.set(subtitle=sub, dim=False)
            outs = st.get("outputs") or []
            cur = next((d for n, d in outs if n == st.get("monitor_output")), st.get("monitor_output", ""))
            if not self._held("output"):
                self.out_var.set(cur)
                if getattr(self, "listen_btn", None) is not None:
                    self.listen_btn.configure_text(text=cur or "–")
            hint = _("In OBS add {source}").format(source=_("Monitor of {device}").format(
                device=_("FreeWave Stream Mix")))
            if any(m.get("virtual") and m["id"] != "stream" for m in mixes):
                hint += "  ·  " + _("Other virtual mixes: “Monitor of FreeWave <name>”")
            self.table_hint.configure(
                text=hint + "\n" + _("Click an empty cell to add the channel to the mix; the ✕ on the corner of a "
                                     "cell removes it."))
            keys = tuple((s["index"], s["app"], s["channel"]) for s in st.get("streams") or [])
            self._stream_keys = keys
            fx_ids = tuple((c["id"], c["name"], c.get("icon"), c.get("color"), tuple(c.get("fx_names") or []),
                            c.get("fx_total", 0)) for c in st["channels"])
            if fx_ids != self._fx_ids:
                self._fx_ids = fx_ids
                self._fill_fx_view(st["channels"])
            apps_ids = (keys, ids)
            if apps_ids != self._apps_ids and not self._held("streams") and not self._dragging():
                self._apps_ids = apps_ids
                self._fill_apps_view(st.get("streams") or [], st["channels"])
            self.nav["effects"].set(count=sum(c.get("fx", 0) for c in st["channels"]) or "")
            self.nav["apps"].set(count=len(st.get("streams") or []) or "")
        else:
            self.mix_on.pack_forget()
            self.mix_off.pack(fill="both", expand=True)
            self._strip_ids = None
            if self._fx_ids is not None:
                self._fx_ids = None
                self._build_fx_placeholder()
            self.nav["effects"].set(count="")
            self.nav["apps"].set(count="")
            for card in self.mix_cards.values():
                card.set(subtitle=_("Mixer is off"), dim=True)
        if st.get("error"):
            self.status(st["error"])

        # --- FreeWave-Profile auf dem Stream Deck an die Kanäle anpassen
        ids = tuple(c["id"] for c in st.get("channels") or [])
        mix_ids = tuple(m["id"] for m in st.get("mixes") or [])
        # welcher Kanal in welchem Mix ist: ändert sich das, zeigt das Deck bei diesem Mix andere Kanäle
        members = tuple((m["id"], tuple(sorted(m.get("exclude") or []))) for m in st.get("mixes") or [])
        last = getattr(self, "_deck_ids", None)
        if st.get("mixer_active") and ids and (ids, mix_ids, members) != last and has_freedeck():
            force = last is not None and last[2] != members
            self._deck_ids = (ids, mix_ids, members)
            self.worker.submit(lambda: self._sync_deck(ids, mix_ids, force), self._deck_synced)

        # --- neu angeschlossenes Stream Deck: FreeWave-Profil dafür anlegen (wenn schon einmal exportiert)
        decks = st.get("decks")
        if decks is not None and decks != getattr(self, "_decks", None):
            self._decks = decks  # auch beim Start: Gerät kann angesteckt worden sein, als FreeWave zu war
            if decks:
                self.worker.submit(self._adopt_decks, self._deck_synced)

        # --- Szenen und Tastenkürzel
        scenes = st.get("scenes") or []
        active = next((x for x in scenes if x["id"] == st.get("active_scene")), None)
        self.scene_btn.configure_text(text=active.get("name") if active else _("Scenes"))
        dlg = getattr(self, "_scenes_dlg", None)
        if dlg is not None and dlg.winfo_exists():
            dlg.update_scenes(scenes, st.get("active_scene"), self._scene_keys(st))
        mic_key = ((st.get("shortcuts") or {}).get(MIC_SHORTCUT) or {}).get("binding", "")
        if not self.mic_shortcut.capturing and not self._held(f"shortcut:{MIC_SHORTCUT}") \
                and mic_key != self.mic_shortcut.binding:
            self.mic_shortcut.set(mic_key)
        self._apply_duck(st)

        # --- Mikrofon
        mic, cfg = st.get("mic") or {}, st.get("settings") or {}
        hw = bool(mic.get("hardware"))
        present = hw or bool(mic.get("source"))
        name = "Elgato Wave:3" if hw else ("Elgato Wave" if present else _("No Wave microphone"))
        self.mic_name.set(name)
        muted = bool(mic.get("mute"))
        self.mic_head.set(("●  " + (_("{name} · muted").format(name=name) if muted else name)) if present
                          else "○  " + _("No Wave microphone connected"))
        if present:
            parts = []
            if muted:
                parts.append(_("Muted"))
            if "gain_pct" in mic:
                parts.append(_("Gain {value} %").format(value=mic["gain_pct"]))
            lowcut = bool(mic["lowcut"]) if hw and "lowcut" in mic else bool(cfg.get("lowcut"))
            clip = bool(mic["clipguard"]) if hw and "clipguard" in mic else bool(cfg.get("clipguard"))
            parts += (["Low Cut"] if lowcut else []) + (["Clipguard"] if clip else [])
            fnt = theme.font(self.root, 9)
            self.mic_card.set(title=name, subtitle=_fit_text(self.root, fnt, " · ".join(parts), 150) or " ",
                              dot=COL["danger"] if muted else COL["ok"], dim=False)
        else:
            self.mic_card.set(title=_("Microphone"), subtitle=_("No Wave microphone"), dot="", dim=True)
        if hw:
            hint = _("All settings are stored directly in the microphone.") + (
                " " + _("Firmware API {api}.").format(api=mic["api"]) if mic.get("api") else "")
        elif present:
            hint = (_("Low Cut and Clipguard run as PipeWire filters. In Discord/OBS, select the microphone "
                      "“FreeWave Microphone”.") + ("" if st.get("limiter") else
                                                   " " + _("Clipguard needs the package swh-plugins.")))
        else:
            hint = _("Connect a microphone – the settings are applied as soon as it is detected.")
        self.mic_hint.set(hint)
        if self.gain.enabled != ("gain_pct" in mic):  # ohne Mikrofon nichts zu regeln
            self.gain.enabled = "gain_pct" in mic
            self.gain.set_dimmed(not self.gain.enabled)
        if "gain_pct" in mic and not self._held("gain"):
            self.gain.set(mic["gain_pct"])
            self.gain_val.set(f"{mic['gain_pct']} %" + (f"  ·  {mic['gain']:.1f} dB" if hw else ""))
        if not self._held("mute"):
            self.v["mute"].set(muted)
            self.gain.set_dimmed(muted)
        for key in ("lowcut", "clipguard"):
            if not self._held(key):
                self.v[key].set(bool(mic[key]) if hw and key in mic else bool(cfg.get(key)))
        if not self._held("lowcut_freq"):
            self.freq.set(str(cfg.get("lowcut_freq") or 80))
            self.freq_seg.set(self.freq.get() if self.freq.get() in ("80", "120") else "80")
        if hw:  # das Wave:3 filtert mit fester Frequenz selbst
            self.freq_seg.pack_forget()
        elif not self.freq_seg.winfo_manager():
            self.freq_seg.pack(side="right", padx=12)
        if hw:
            self.no_hw.pack_forget()
            if not self.hw_frame.winfo_manager():
                self.hw_frame.pack(anchor="nw", fill="x")
            for key in ("leds_off", "leds_flip", "headphone_mute"):
                if key in mic and not self._held(key):
                    self.v[key].set(bool(mic[key]))
            if "headphone_volume" in mic and not self._held("headphone_volume"):
                self.hp.set(mic["headphone_volume"])
                self.hp_val.set(f"{mic['headphone_volume']:.0f} dB")
                self.hp.set_dimmed(bool(mic.get("headphone_mute")))
            if "direct_monitor" in mic and not self._held("direct_monitor"):
                self.dm.set(mic["direct_monitor"])
                self.dm_val.set(_("{value:.0f} % mic").format(value=mic["direct_monitor"]))
            self.v["leds_flip"].set_enabled("leds_flip" in mic)
        else:
            self.hw_frame.pack_forget()
            if not self.no_hw.winfo_manager():
                self.no_hw.pack(anchor="nw", fill="x")
        self._apply_noise(st.get("noise") or {})
        self.flow.update(st)
        self.soundview.update(st)
        self._update_header()

    def _build_fx_placeholder(self):
        for w in self.fx_list.winfo_children():
            w.destroy()
        label(self.fx_list, _("Set up the mixer to add effects to channels."), color=COL["muted"]).pack(anchor="w")
