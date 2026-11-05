"""Ansicht „Soundboard“ von FreeWave: Klang-Kacheln, Einstellungen rechts, Ziel und Lautstärke unten.

Jede Kachel zeigt die Wellenform ihres Klangs (beim Abspielen füllt sie sich als Fortschritt), Dauer und
Lautstärke bzw. die verstrichene Zeit, und unten kleine Marken: auf welcher FreeDeck-Taste der Klang liegt
und ob er in der Schleife spielt. Ein Klick auf eine Kachel spielt den Klang (nochmals: stoppen) und wählt
ihn aus; rechts stehen dann Name, Datei, der Zuschnitt (Anfang und Ende in der Wellenform ziehen),
Farbe, Lautstärke, Schleife, die Taste auf dem Deck und Löschen. „Stop all“ oben rechts (oder Esc) stoppt
alles. Unten: „Abspielen in“ (welcher Mix – oder alle), „Mithören in“ (selbst hören: ein Mix oder ein
Ausgabegerät), wie viele Klänge gerade spielen und die Gesamtlautstärke.
Gespeichert wird in config.json (soundboard.py), immer im Arbeits-Thread des Fensters.
"""
import tkinter as tk
from pathlib import Path

from PIL import Image, ImageDraw

from . import soundboard, theme
from .dialogkit import ScrollArea, entry, field
from .i18n import _, ngettext
from .theme import COL, Button, Card, Fader, Switch, label, rounded_rect

PAD_W, PAD_H = 160, 150
PAD_GAP = 12
SIDE_W = 300
POLL_MS = 300
POLL_PLAY_MS = 100  # solange etwas spielt: Fortschritt flüssiger
KEYS_EVERY = 5000  # Tasten auf dem Deck so oft neu lesen (ms), solange die Ansicht sichtbar ist
TRIM_H = 74


def _scissors(size, color):
    """Symbol „Schere“ (Zuschnitt) – zwei Ringe, zwei Klingen."""
    k = 4
    S = size * k
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    lw = max(2, round(S * 0.09))
    r = S * 0.16
    for cy in (S * 0.26, S * 0.74):
        d.ellipse((S * 0.1, cy - r, S * 0.1 + 2 * r, cy + r), outline=color, width=lw)
    d.line((S * 0.38, S * 0.34, S * 0.94, S * 0.78), fill=color, width=lw)
    d.line((S * 0.38, S * 0.66, S * 0.94, S * 0.22), fill=color, width=lw)
    return im.resize((size, size), Image.LANCZOS)


def _repeat(size, color):
    """Symbol „Schleife“ – zwei Bögen mit Pfeilspitzen (FreeWave hat iconlib nicht immer dabei)."""
    k = 4
    S = size * k
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    lw = max(2, round(S * 0.1))
    d.line((S * 0.22, S * 0.62, S * 0.22, S * 0.42, S * 0.3, S * 0.3, S * 0.8, S * 0.3), fill=color, width=lw,
           joint="curve")
    d.line((S * 0.78, S * 0.38, S * 0.78, S * 0.58, S * 0.7, S * 0.7, S * 0.2, S * 0.7), fill=color, width=lw,
           joint="curve")
    d.polygon([(S * 0.66, S * 0.12), (S * 0.92, S * 0.3), (S * 0.66, S * 0.48)], fill=color)
    d.polygon([(S * 0.34, S * 0.52), (S * 0.08, S * 0.7), (S * 0.34, S * 0.88)], fill=color)
    return im.resize((size, size), Image.LANCZOS)


theme.EXTRA_GLYPHS.setdefault("repeat", _repeat)
theme.EXTRA_GLYPHS.setdefault("scissors", _scissors)


def _bars(peaks, n):
    """peaks (0 … 1) auf n Balken bringen (Höchstwert je Abschnitt)."""
    if not peaks or n <= 0:
        return []
    m = len(peaks)
    return [max(peaks[i * m // n:max(i * m // n + 1, (i + 1) * m // n)]) for i in range(n)]


class DropButton(tk.Canvas):
    """Auswahlknopf der Fußleiste: „Abspielen in  Stream-Mix ⌄“ – grauer Vorsatz, heller Wert."""

    def __init__(self, master, prefix, command=None, height=32):
        super().__init__(master, height=height, bg=theme._bg_of(master), highlightthickness=0, bd=0, cursor="hand2")
        self.prefix, self.value, self.command, self.h = prefix, "", command, height
        self.hover = False
        self.icons = theme.Icons(self)
        self.f, self.fv = theme.font(self, 9), theme.font(self, 10)
        self.bind("<ButtonRelease-1>", lambda e: self.command and self.command(self))
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self._draw()

    def _set_hover(self, v):
        self.hover = v
        self._draw()

    def set(self, value):
        if value != self.value:
            self.value = value
            self._draw()

    def _draw(self):
        self.delete("all")
        pw = theme.measure(self, self.f, self.prefix)
        vw = theme.measure(self, self.fv, self.value) if self.value else 0
        w = 12 + pw + (6 + vw if vw else 0) + 10 + 12 + 10
        self.configure(width=w)
        rounded_rect(self, 0, 0, w - 1, self.h - 1, 8, fill=COL["raised"] if self.hover else COL["bg"],
                     outline=COL["border"])
        x = 12
        self.create_text(x, self.h / 2, text=self.prefix, anchor="w", fill=COL["muted"], font=self.f)
        x += pw + 6
        if vw:
            self.create_text(x, self.h / 2, text=self.value, anchor="w", fill=COL["fg"], font=self.fv)
            x += vw
        self.create_image(w - 16, self.h / 2, image=self.icons.get("down", 10, COL["muted"]))


class StopAllButton(tk.Canvas):
    """Roter Knopf „Stop all“ mit dem Kürzel Esc (in der Kopfzeile); grau, solange nichts spielt."""

    def __init__(self, master, command=None, height=36):
        super().__init__(master, height=height, bg=theme._bg_of(master), highlightthickness=0, bd=0,
                         cursor="hand2")
        self.command, self.h = command, height
        self.active = self.hover = False
        self.icons = theme.Icons(self)
        self.f, self.fk = theme.font(self, 10), theme.font(self, 8, "bold")
        self.bind("<ButtonRelease-1>", lambda e: self.command and self.command())
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False))
        self._draw()

    def _set(self, **kw):
        if any(getattr(self, k) != v for k, v in kw.items()):
            self.__dict__.update(kw)
            self._draw()

    def set_active(self, on):
        self._set(active=bool(on))

    def _draw(self):
        self.delete("all")
        text, key = _("Stop all"), "Esc"
        tw, kw = theme.measure(self, self.f, text), theme.measure(self, self.fk, key) + 12
        w = 14 + 13 + 8 + tw + 10 + kw + 10
        self.configure(width=w)
        if self.active:
            fill = theme.mix(COL["danger_soft"], COL["danger"], 0.15 if self.hover else 0.0)
            out, fg = theme.mix(COL["danger_soft"], COL["danger"], 0.55), COL["danger"]
            kfill, kfg = theme.mix(COL["danger_soft"], COL["danger"], 0.22), theme.mix(COL["danger"], COL["fg"], 0.35)
        else:
            fill, out, fg = COL["raised"] if self.hover else COL["panel2"], COL["border"], COL["muted"]
            kfill, kfg = COL["raised"], COL["muted"]
        rounded_rect(self, 0, 0, w - 1, self.h - 1, 8, fill=fill, outline=out)
        x = 14
        self.create_image(x + 6, self.h / 2, image=self.icons.get("stop", 13, fg))
        x += 13 + 8
        self.create_text(x, self.h / 2, text=text, anchor="w", font=self.f, fill=fg)
        x += tw + 10
        rounded_rect(self, x, self.h / 2 - 9, x + kw, self.h / 2 + 9, 4, fill=kfill, outline="")
        self.create_text(x + kw / 2, self.h / 2, text=key, font=self.fk, fill=kfg)


class SoundPad(tk.Canvas):
    """Kachel eines Klangs: Knopf, Name, Dauer/Fortschritt, Wellenform und Marken (Deck-Taste, Schleife)."""

    def __init__(self, master, sound, command=None, select=None):
        super().__init__(master, width=PAD_W, height=PAD_H, bg=theme._bg_of(master), highlightthickness=0, bd=0,
                         cursor="hand2")
        self.sound, self.command, self.select = sound, command, select
        self.playing = self.selected = self.hover = False
        self.info = None      # soundboard.analyze() der Datei
        self.key = None       # (Seite, Taste) auf dem Deck
        self.elapsed = None   # Sekunden seit dem Start (spielt)
        self.icons = theme.Icons(self)
        self.f, self.fs, self.fb = theme.font(self, 10, "bold"), theme.font(self, 9), theme.font(self, 8, "bold")
        self.bind("<ButtonRelease-1>", lambda e: self.command and self.command(self.sound))
        self.bind("<Button-3>", lambda e: self.select and self.select(self.sound))
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False))
        self._draw()

    def _set(self, **kw):
        changed = any(getattr(self, k) != v for k, v in kw.items())
        for k, v in kw.items():
            setattr(self, k, v)
        if changed:
            self._draw()

    def set(self, sound=None, playing=None, selected=None, **kw):
        """Neue Werte (nur angegebene); info, key und elapsed dürfen auch None werden."""
        if sound is not None:
            kw["sound"] = sound
        kw.update({k: v for k, v in (("playing", playing), ("selected", selected)) if v is not None})
        if kw:
            self._set(**kw)

    def progress(self):
        """Wiedergabeposition als Anteil der ganzen Datei (für die Wellenform) oder None."""
        if not self.playing or self.elapsed is None or not self.info or self.info["duration"] <= 0:
            return None
        dur = self.info["duration"]
        start, end = soundboard.trim_range(self.sound, dur)
        length = max(0.01, end - start)
        t = self.elapsed % length if self.sound.get("loop") else min(self.elapsed, length)
        return (start + t) / dur

    def _draw(self):
        self.delete("all")
        s, w, h = self.sound, PAD_W, PAD_H
        color = s["color"]
        fill = theme.mix(COL["bg"], color, 0.27 if self.playing else 0.2)
        if self.hover:
            fill = theme.mix(fill, COL["fg"], 0.05)
        if self.playing:
            out, ow = COL["ok"], 2
        elif self.selected:
            out, ow = COL["fg"], 2
        else:
            out, ow = theme.mix(fill, COL["fg"], 0.07), 1
        rounded_rect(self, 1, 1, w - 2, h - 2, 12, fill=fill, outline=out, width=ow)
        # runder Knopf oben links: Abspielen bzw. Stoppen
        self.create_oval(14, 14, 48, 48, fill=color, outline="")
        self.create_image(31, 31, image=self.icons.get("stop" if self.playing else "play", 13, "#ffffff"))
        self.create_text(58, 23, text=theme.elide(self, s["name"], self.f, w - 70), anchor="w", fill=COL["fg"],
                         font=self.f)
        length = soundboard.play_length(s, self.info) if self.info else None
        if self.playing and self.elapsed is not None and length:
            t = self.elapsed % length if s.get("loop") else min(self.elapsed, length)
            sub, sub_fg = f"{soundboard.fmt_time(t)} / {soundboard.fmt_time(length)}", \
                theme.mix(COL["ok"], COL["fg"], 0.4)
        else:
            sub = f"{soundboard.fmt_time(length)} · {s['volume']} %" if length else f"{s['volume']} %"
            sub_fg = theme.mix(fill, COL["fg"], 0.7)
        self.create_text(58, 40, text=sub, anchor="w", fill=sub_fg, font=self.fs)
        self._wave(fill)
        self._badges(fill)

    def _wave(self, fill):
        x0, y0, x1, y1 = 14, 62, PAD_W - 14, 102
        box = theme.mix(fill, "#000000", 0.18)
        rounded_rect(self, x0, y0, x1, y1, 6, fill=box, outline="")
        cy, half = (y0 + y1) / 2, (y1 - y0) / 2 - 4
        info = self.info
        if not info or not info["peaks"]:  # kein WAV: neutraler Platzhalter
            rounded_rect(self, x0 + 8, cy - 2, x1 - 8, cy + 2, 2, fill=theme.mix(box, COL["fg"], 0.2), outline="")
            return
        step = 3
        n = int((x1 - x0 - 12) // step)
        dur = info["duration"] or 1
        start, end = soundboard.trim_range(self.sound, dur)
        prog = self.progress()
        on, off, cut = "#ffffff", theme.mix(box, COL["fg"], 0.36), theme.mix(box, COL["fg"], 0.12)
        for i, p in enumerate(_bars(info["peaks"], n)):
            pos = (i + 0.5) / n
            if not start / dur <= pos <= end / dur:
                col = cut
            elif prog is not None and pos <= prog:
                col = on
            else:
                col = off
            x = x0 + 6 + i * step
            hh = max(1.0, p * half)
            self.create_rectangle(x, cy - hh, x + 2, cy + hh, fill=col, outline="")

    def _badges(self, fill):
        y1 = PAD_H - 13
        y0 = y1 - 21
        bfill, fg = theme.mix(fill, "#000000", 0.3), "#e6e6e6"
        x = 14
        if self.key:
            text = soundboard.key_text(self.key)
            tw = theme.measure(self, self.fb, text)
            rounded_rect(self, x, y0, x + 7 + 12 + 5 + tw + 7, y1, 5, fill=bfill, outline="")
            self.create_image(x + 7 + 6, (y0 + y1) / 2, image=self.icons.get("deck", 12, fg))
            self.create_text(x + 7 + 12 + 5, (y0 + y1) / 2, text=text, anchor="w", fill=fg, font=self.fb)
        if self.sound.get("loop"):
            x1 = PAD_W - 14
            rounded_rect(self, x1 - 25, y0, x1, y1, 5, fill=bfill, outline="")
            self.create_image(x1 - 12.5, (y0 + y1) / 2, image=self.icons.get("repeat", 12, fg))


class AddPad(tk.Canvas):
    """Kachel „+ Sound hinzufügen“ (gestrichelter Rand)."""

    def __init__(self, master, command):
        super().__init__(master, width=PAD_W, height=PAD_H, bg=theme._bg_of(master), highlightthickness=0, bd=0,
                         cursor="hand2")
        self.command, self.hover = command, False
        self.icons = theme.Icons(self)
        self.f = theme.font(self, 10)
        self.bind("<ButtonRelease-1>", lambda e: self.command())
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self._draw()

    def _set_hover(self, v):
        self.hover = v
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = PAD_W, PAD_H
        rounded_rect(self, 1, 1, w - 2, h - 2, 12, fill=COL["raised"] if self.hover else COL["bg"],
                     outline=theme.mix(COL["bg"], COL["fg"], 0.2), dash=(5, 4), width=1)
        fg = COL["fg"] if self.hover else COL["muted"]
        self.create_image(w / 2, h / 2 - 12, image=self.icons.get("plus", 22, fg))
        self.create_text(w / 2, h / 2 + 16, text=_("Add sound"), fill=fg, font=self.f)


class PlayingCount(tk.Canvas):
    """„● 1 playing“ in der Fußleiste (leer, solange nichts spielt)."""

    def __init__(self, master):
        super().__init__(master, width=96, height=24, bg=theme._bg_of(master), highlightthickness=0, bd=0)
        self.f = theme.font(self, 9, "bold")
        self.n = -1
        self.set(0)

    def set(self, n):
        if n == self.n:
            return
        self.n = n
        self.delete("all")
        if not n:
            return
        text = ngettext("{count} playing", "{count} playing", n).format(count=n)
        tw = theme.measure(self, self.f, text)
        self.configure(width=tw + 16)
        self.create_oval(1, 8.5, 8, 15.5, fill=COL["ok"], outline="")
        self.create_text(14, 12, text=text, anchor="w", fill=theme.mix(COL["ok"], COL["fg"], 0.4), font=self.f)


class TrimEditor(tk.Canvas):
    """Wellenform mit zwei Griffen (Anfang, Ende) zum Ziehen und dem Abspielpunkt.

    on_drag(start, end) während des Ziehens, on_change(start, end) beim Loslassen (Sekunden)."""

    def __init__(self, master, width, color, info, start, end, on_drag=None, on_change=None):
        super().__init__(master, width=width, height=TRIM_H, bg=theme._bg_of(master), highlightthickness=0, bd=0)
        self.w, self.color, self.info = width, color, info
        self.dur = info["duration"] if info else 0
        self.start, self.end = start, end
        self.playhead = None
        self.on_drag, self.on_change = on_drag, on_change
        self._grab = None
        if info:
            self.configure(cursor="sb_h_double_arrow")
            self.bind("<Button-1>", self._press)
            self.bind("<B1-Motion>", self._move)
            self.bind("<ButtonRelease-1>", self._release)
        self._draw()

    def _x(self, t):
        x0, x1 = 10, self.w - 10
        return x0 + (x1 - x0) * (t / self.dur if self.dur else 0)

    def _t(self, x):
        x0, x1 = 10, self.w - 10
        return max(0.0, min(1.0, (x - x0) / max(1, x1 - x0))) * self.dur

    def set(self, start=None, end=None, playhead=False):
        """Neue Griffe bzw. Abspielpunkt (playhead=None: keiner); False = unverändert."""
        if start is not None:
            self.start = start
        if end is not None:
            self.end = end
        if playhead is not False:
            self.playhead = playhead
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.w, TRIM_H
        bg = COL["panel2"]
        rounded_rect(self, 0, 0, w - 1, h - 1, 8, fill=bg, outline=COL["border"])
        cy = h / 2
        if not self.info or not self.info["peaks"]:
            rounded_rect(self, 12, cy - 3, w - 12, cy + 3, 3, fill=theme.mix(bg, COL["fg"], 0.16), outline="")
            return
        xs, xe = self._x(self.start), self._x(self.end)
        dark = theme.mix(bg, "#000000", 0.35)
        if xs > 2:  # abgeschnittene Teile: dunkler
            rounded_rect(self, 1, 1, xs + 8, h - 2, 8, fill=dark, outline="")
            self.create_rectangle(xs, 1, xs + 9, h - 2, fill=bg, outline="")
        if xe < w - 3:
            rounded_rect(self, xe - 8, 1, w - 2, h - 2, 8, fill=dark, outline="")
            self.create_rectangle(xe - 9, 1, xe, h - 2, fill=bg, outline="")
        step, half = 5, h / 2 - 10
        n = int((w - 20) // step)
        on, off = self.color, theme.mix(dark, self.color, 0.35)
        for i, p in enumerate(_bars(self.info["peaks"], n)):
            x = 10 + i * step + 1
            col = on if xs <= x + 1.5 <= xe else off
            hh = max(1.0, p * half)
            self.create_rectangle(x, cy - hh, x + 3, cy + hh, fill=col, outline="")
        if self.playhead is not None:
            xp = self._x(self.playhead)
            self.create_rectangle(xp - 1, 4, xp + 1, h - 4, fill="#ffffff", outline="")
        for x in (xs, xe):  # Griffe
            self.create_rectangle(x - 2, 2, x + 2, h - 2, fill=COL["warn"], outline="")
            rounded_rect(self, x - 6, cy - 9, x + 6, cy + 9, 3, fill=COL["warn"], outline="")

    def _press(self, e):
        t = self._t(e.x)
        self._grab = "start" if abs(t - self.start) <= abs(t - self.end) else "end"
        self._move(e)

    def _move(self, e):
        if self._grab is None:
            return
        t = self._t(e.x)
        gap = soundboard.MIN_LENGTH
        if self._grab == "start":
            self.start = min(t, self.end - gap)
        else:
            self.end = max(t, self.start + gap)
        self._draw()
        if self.on_drag:
            self.on_drag(self.start, self.end)

    def _release(self, _e):
        if self._grab is None:
            return
        self._grab = None
        if self.on_change:
            self.on_change(self.start, self.end)


class SidePanel(tk.Canvas):
    """Fläche rechts mit runden Ecken; Inhalt in .body – leer mittig, sonst oben über die ganze Höhe."""

    def __init__(self, master):
        super().__init__(master, width=SIDE_W, bg=theme._bg_of(master), highlightthickness=0, bd=0)
        self.body = tk.Frame(self, bg=COL["panel"])
        self.centered = True
        self._win = self.create_window(0, 0, window=self.body, anchor="n")
        self.bind("<Configure>", lambda e: self._draw())
        self.body.bind("<Configure>", lambda e: self._place())

    def _draw(self):
        self.delete("bg")
        w, h = self.winfo_width(), self.winfo_height()
        rounded_rect(self, 0, 0, w - 1, h - 1, 14, fill=COL["panel"], outline=COL["border"], tags="bg")
        self.tag_lower("bg")
        self._place()

    def _place(self):
        w, h = self.winfo_width(), self.winfo_height()
        self.itemconfigure(self._win, width=max(1, w - 40))
        if self.centered:
            self.itemconfigure(self._win, height=0)
            y = max(20, (h - self.body.winfo_reqheight()) / 2)
        else:
            self.itemconfigure(self._win, height=max(self.body.winfo_reqheight(), h - 40))
            y = 20
        self.coords(self._win, w / 2, y)

    def clear(self, centered):
        for c in self.body.winfo_children():
            c.destroy()
        self.centered = centered
        self._place()


class SoundboardView:
    """Soundboard im Rahmen frame; win ist das FreeWave-Fenster (Arbeits-Thread, Zustand, Konfiguration)."""

    def __init__(self, frame, win):
        self.win, self.root = win, win.root
        self.cfg = win.host.cfg
        self.player = soundboard.Player()
        self.sounds = soundboard.sounds(self.cfg.data)
        self.play_in, self.monitor_in, self.volume = soundboard.options(self.cfg.data)
        self.selected = None
        self.pads = {}
        self.info = {}   # Datei -> soundboard.analyze() (im Arbeits-Thread gelesen)
        self.keys = {}   # Klang-id -> (Seite, Taste) auf dem Deck
        self.visible = False
        self._ids = None
        self._poll_job = None
        self._keys_at = 0
        self._side = {}  # Widgets der Einstellungen, die sich ohne Neuaufbau ändern

        # „Stop all“ steht in der Kopfzeile des Fensters (waveui zeigt ihn nur im Soundboard)
        head = getattr(win, "head", None)
        self.stop_btn = StopAllButton(head if head is not None else frame, command=self.stop_all)
        self.root.bind("<Escape>", self._escape, add="+")

        self.side = SidePanel(frame)
        self.side.pack(side="right", fill="y", padx=(0, 22), pady=(0, 14))
        left = tk.Frame(frame, bg=COL["bg"])
        left.pack(side="left", fill="both", expand=True)
        bar_card = Card(left, color=COL["panel2"], radius=10, pad=10, stretch=True)
        bar_card.pack(side="bottom", fill="x", padx=(22, 16), pady=(0, 14))
        bar = bar_card.body
        # Lautstärke zuerst (rechts): bei schmalem Fenster bleibt sie ganz sichtbar
        self.icons = theme.Icons(self.root)
        self.vol_val = tk.StringVar(self.root, value=f"{self.volume} %")
        label(bar, textvariable=self.vol_val, size=10, width=5, anchor="e").pack(side="right")
        self.vol = Fader(bar, length=110, thickness=24, orient="horizontal", value=self.volume,
                         command=self._on_volume)
        self.vol.pack(side="right", padx=(6, 6))
        self._spk = tk.Label(bar, image=self.icons.get("speaker", 16, COL["muted"]), bg=COL["panel2"])
        self._spk.pack(side="right")
        self.count = PlayingCount(bar)
        self.count.pack(side="right", padx=(0, 12))
        self.play_btn = DropButton(bar, _("Play in"), command=self._play_menu)
        self.play_btn.pack(side="left")
        self.mon_btn = DropButton(bar, _("Monitor in"), command=self._monitor_menu)
        self.mon_btn.pack(side="left", padx=(10, 8))
        self.area = ScrollArea(left, bg=COL["bg"])
        self.area.pack(fill="both", expand=True, padx=(22, 16), pady=(0, 12))
        self.grid = tk.Frame(self.area.inner, bg=COL["bg"])
        self.grid.pack(fill="x", anchor="nw")
        self.grid.bind("<Configure>", lambda e: self._layout())
        self.add_pad = AddPad(self.grid, self.add_sound)
        self._fill()
        self._show_settings()
        self._update_bar()
        self._load_info()

    # ------------------------------------------------------- Anzeige
    def show(self):
        self.visible = True
        self._update_bar()
        self._load_info()
        self._load_keys()
        self._poll()

    def hide(self):
        self.visible = False

    def _escape(self, _e=None):
        if self.visible and self.player.playing():
            self.stop_all()

    def _poll(self):
        """Spielt noch? Solange die Ansicht sichtbar ist (oder etwas spielt), regelmäßig prüfen."""
        if self._poll_job is not None:
            try:
                self.root.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None
        playing = self._refresh_playing()
        if self.visible:
            now = self.root.tk.call("clock", "milliseconds")
            if now - self._keys_at > KEYS_EVERY:
                self._load_keys()
        if self.visible or playing:
            self._poll_job = self.root.after(POLL_PLAY_MS if playing else POLL_MS, self._poll)

    def _refresh_playing(self):
        """Kacheln, Zähler, „Stop all“ und Abspielpunkt an die laufenden Klänge anpassen."""
        playing = self.player.playing()
        for sid, pad in self.pads.items():
            on = sid in playing
            pad.set(playing=on, elapsed=self.player.elapsed(sid) if on else None)
        self.count.set(len(playing))
        self.stop_btn.set_active(bool(playing))
        side = self._side.get("play")
        if side is not None and self.selected:
            try:
                side.configure_text(text=_("Stop") if self.selected in playing else _("Play"),
                                    icon="stop" if self.selected in playing else "play")
            except tk.TclError:
                pass
        trim = self._side.get("trim")
        if trim is not None and self.selected:
            pad = self.pads.get(self.selected)
            prog = pad.progress() if pad is not None else None
            try:
                if prog is not None or trim.playhead is not None:
                    trim.set(playhead=None if prog is None else prog * trim.dur)
            except tk.TclError:
                pass
        return playing

    def _load_info(self):
        """Wellenformen und Längen im Arbeits-Thread lesen (zwischengespeichert je Datei)."""
        files = [s["file"] for s in self.sounds]

        def done(res, err):
            if err is not None or res is None or res == {f: self.info.get(f) for f in res}:
                return
            self.info.update(res)
            for s in self.sounds:
                pad = self.pads.get(s["id"])
                if pad is not None:
                    pad.set(info=self.info.get(s["file"]))
            if self.selected:
                self._show_settings()
        self.win.worker.submit(lambda: {f: soundboard.analyze(f) for f in files}, done)

    def _load_keys(self):
        """Welche FreeDeck-Taste welchen Klang spielt (im Arbeits-Thread)."""
        self._keys_at = self.root.tk.call("clock", "milliseconds")

        def done(res, err):
            if err is not None or res is None or res == self.keys:
                return
            self.keys = res
            for sid, pad in self.pads.items():
                pad.set(key=self.keys.get(sid))
            lb = self._side.get("trigger")
            if lb is not None:
                self._set_trigger(lb)
        self.win.worker.submit(soundboard.deck_keys, done)

    def _fill(self):
        ids = tuple((s["id"], s["name"], s["color"], s["volume"], s["loop"], s.get("start"), s.get("end"), s["file"])
                    for s in self.sounds)
        if ids == self._ids:
            return
        self._ids = ids
        old = self.pads
        self.pads = {}
        for s in self.sounds:
            pad = old.pop(s["id"], None)
            if pad is None:
                pad = SoundPad(self.grid, s, command=self.toggle, select=self.select)
            pad.set(sound=s, selected=s["id"] == self.selected, info=self.info.get(s["file"]),
                    key=self.keys.get(s["id"]))
            self.pads[s["id"]] = pad
        for pad in old.values():
            pad.destroy()
        self._layout()

    def _layout(self):
        widgets = [self.pads[s["id"]] for s in self.sounds if s["id"] in self.pads] + [self.add_pad]
        width = max(self.grid.winfo_width(), PAD_W + 10)
        x = y = 0
        for w in widgets:
            if x and x + PAD_W > width:
                x, y = 0, y + PAD_H + PAD_GAP
            w.place(x=x, y=y)
            x += PAD_W + PAD_GAP
        h = y + PAD_H + 2
        if int(self.grid.cget("height")) != h:
            self.grid.configure(height=h)

    def update(self, st):
        """Neuer Fensterzustand: Namen der Mixe/Geräte in der Fußleiste auffrischen."""
        self._update_bar()

    # ------------------------------------------------------- Fußleiste
    def _mixes(self):
        st = self.win.state
        return [(m["id"], m["name"]) for m in (st.get("mixes") or [])]

    def _outputs(self):
        return list(self.win.state.get("outputs") or [])

    def _play_name(self, v):
        if not v:
            return _("Not selected")
        if v == "all":
            return _("All mixes")
        return dict(self._mixes()).get(v, v)

    def _monitor_name(self, v):
        if not v:
            return _("Not selected")
        if v.startswith("out:"):
            dev = v[4:]
            return dict(self._outputs()).get(dev) or dev or _("Default output")
        mid = v[4:] if v.startswith("mix:") else v
        return dict(self._mixes()).get(mid) or (_("Personal Mix") if mid == "monitor" else mid)

    def _update_bar(self):
        self.play_btn.set(self._play_name(self.play_in))
        self.mon_btn.set(theme.elide(self.mon_btn, self._monitor_name(self.monitor_in), self.mon_btn.fv, 200))

    def _menu(self, btn, items, cur, pick):
        menu = theme.PopupMenu(self.root, width=280)
        for val, text, icon in items:
            if val is None:
                menu.separator()
                continue
            menu.add(text, lambda v=val: pick(v), icon="check" if val == cur else icon)
        menu.below(btn)
        return menu

    def _play_menu(self, btn):
        items = [("", _("Not selected"), "close"), ("all", _("All mixes"), "layers"), (None, "", None)]
        items += [(mid, name, "wave") for mid, name in self._mixes()]
        return self._menu(btn, items, self.play_in, lambda v: self._set_option(play=v))

    def _monitor_menu(self, btn):
        items = [("", _("Not selected"), "close"), (None, "", None)]
        mixes = self._mixes() or [("monitor", _("Personal Mix"))]
        items += [(mid, name, "headphones") for mid, name in mixes]
        outs = self._outputs()
        if outs:
            items.append((None, "", None))
            items += [("out:" + name, desc, "speaker") for name, desc in outs]
        else:
            items += [(None, "", None), ("out:", _("Default output"), "speaker")]
        return self._menu(btn, items, self.monitor_in, lambda v: self._set_option(monitor=v))

    def _set_option(self, play=None, monitor=None):
        if play is not None:
            self.play_in = play
        if monitor is not None:
            self.monitor_in = monitor
        self._update_bar()
        cfg = self.cfg
        self.win.worker.submit(lambda: soundboard.set_options(cfg, play=play, monitor=monitor))

    def _on_volume(self, value):
        self.volume = int(round(value))
        self.vol_val.set(f"{self.volume} %")
        cfg = self.cfg
        self.win._debounced("sb_volume", lambda v: soundboard.set_options(cfg, volume=v), self.volume, delay=400)

    def _later(self, key, fn, delay):
        """fn nach delay ms im Tk-Thread ausführen; ein neuerer Aufruf mit demselben key ersetzt ihn."""
        timers = self.__dict__.setdefault("_timers", {})
        job = timers.pop(key, None)
        if job is not None:
            self.root.after_cancel(job)

        def run():
            timers.pop(key, None)
            fn()
        timers[key] = self.root.after(delay, run)

    # ------------------------------------------------------- Abspielen
    def sinks(self):
        return soundboard.targets(self.play_in, self.monitor_in, self.win.state.get("mix_targets") or {})

    def toggle(self, sound):
        """Klick auf eine Kachel: auswählen und abspielen bzw. stoppen."""
        self.select(sound)
        if self.player.is_playing(sound["id"]):
            self.player.stop(sound["id"])
        else:
            try:
                self.player.toggle(sound, self.volume, self.sinks())
                self.win.status("")
            except RuntimeError as e:
                self.win.status(str(e))
        self._poll_once()

    def _poll_once(self):
        playing = self._refresh_playing()
        if playing and self._poll_job is None:
            self._poll_job = self.root.after(POLL_PLAY_MS, self._poll)

    def stop_all(self):
        self.player.stop()
        try:
            self._refresh_playing()
        except tk.TclError:  # Fenster wird gerade geschlossen
            pass

    # ------------------------------------------------------- Bearbeiten
    def _current(self):
        return next((s for s in self.sounds if s["id"] == self.selected), None)

    def select(self, sound):
        sid = sound["id"] if sound else None
        if sid == self.selected:
            return
        self.selected = sid
        for k, pad in self.pads.items():
            pad.set(selected=k == sid)
        self._show_settings()

    def _job(self, work, select=None):
        """Konfiguration im Arbeits-Thread ändern, danach die Liste neu lesen."""
        def done(_res, err):
            self.sounds = soundboard.sounds(self.cfg.data)
            if select is not None:
                self.selected = select() if callable(select) else select
            self._fill()
            for k, pad in self.pads.items():
                pad.set(selected=k == self.selected)
            if err is None or select is not None:
                self._show_settings()
            self._load_info()
        self.win.worker.submit(work, done)

    def add_sound(self):
        from . import msgbox
        f = msgbox.askopenfilename(parent=self.root, title=_("Add sound"), filetypes=[
            (_("Audio files"), "*.wav *.mp3 *.ogg *.flac"), (_("All files"), "*")])
        if not f:
            return
        cfg = self.cfg
        box = {}

        def work():
            box["s"] = soundboard.add(cfg, f)
        self._job(work, select=lambda: (box.get("s") or {}).get("id", self.selected))

    def _change(self, sid, **changes):
        for s in self.sounds:  # sofort anzeigen, gespeichert wird im Arbeits-Thread
            if s["id"] == sid:
                s.update(changes)
        pad = self.pads.get(sid)
        if pad is not None:
            pad.set(sound=dict(pad.sound, **changes))
        cfg = self.cfg
        self.win.worker.submit(lambda: soundboard.update(cfg, sid, **changes))

    def _replace_file(self, sid):
        from . import msgbox
        f = msgbox.askopenfilename(parent=self.root, title=_("Choose audio file"), filetypes=[
            (_("Audio files"), "*.wav *.mp3 *.ogg *.flac"), (_("All files"), "*")])
        if not f:
            return
        cfg = self.cfg

        def work():
            soundboard.update(cfg, sid, file=str(soundboard.import_file(f)))
        self._job(work, select=sid)

    def _delete(self, sid):
        self.player.stop(sid)
        cfg = self.cfg
        self._job(lambda: soundboard.remove(cfg, sid), select="")

    def _set_trim(self, sid, start, end, dur):
        """Zuschnitt übernehmen: ganz vorn bzw. ganz hinten = nicht zugeschnitten (None)."""
        start = None if start <= 0.005 else round(start, 3)
        end = None if end >= dur - 0.005 else round(end, 3)
        s = self._current() or {}
        if (s.get("start"), s.get("end")) != (start, end):
            self._change(sid, start=start, end=end)
        self._trim_labels(start or 0.0, dur if end is None else end, dur)

    def _trim_labels(self, start, end, dur):
        v = self._side
        if "start_var" in v:
            v["start_var"].set(soundboard.fmt_precise(start))
            v["end_var"].set(soundboard.fmt_precise(end))
        if "plays_var" in v:
            v["plays_var"].set(_("plays {time}").format(time=soundboard.fmt_precise(end - start, 1)))

    def _set_trigger(self, lb):
        pos = self.keys.get(self.selected)
        try:
            if pos:
                lb["name"].configure(text=_("Deck key"), fg=COL["fg"])
                lb["value"].configure(text=soundboard.key_text(pos, short=False))
            else:
                lb["name"].configure(text=_("Not on a deck key"), fg=COL["muted"])
                lb["value"].configure(text="")
        except tk.TclError:
            pass

    def _show_settings(self):
        side = self.side
        s = self._current()
        side.clear(centered=s is None)
        self._side = {}
        b = side.body
        if s is None:
            tk.Label(b, image=self.icons.get("music", 22, COL["muted"]), bg=COL["panel"]).pack()
            label(b, _("Select a sound to edit its settings"), 10, color=COL["muted"], wraplength=SIDE_W - 60,
                  justify="center").pack(pady=(10, 0))
            return
        sid = s["id"]
        inner_w = SIDE_W - 40
        head = tk.Frame(b, bg=COL["panel"])
        head.pack(fill="x", pady=(0, 14))
        tk.Label(head, image=theme.icon_tile(self.root, "waveform", 36, s["color"]), bg=COL["panel"]).pack(
            side="left")
        label(head, _("Sound settings"), 13, "bold").pack(side="left", padx=12)

        # Buttons unten (zuerst packen: bleiben auch bei wenig Platz sichtbar)
        btns = tk.Frame(b, bg=COL["panel"])
        btns.pack(side="bottom", fill="x")
        playing = self.player.is_playing(sid)
        self._side["play"] = Button(btns, _("Stop") if playing else _("Play"), "stop" if playing else "play",
                                    kind="primary", command=lambda: self.toggle(self._current() or s))
        self._side["play"].pack(side="left")
        Button(btns, _("Delete sound"), "trash", kind="danger", command=lambda: self._delete(sid)).pack(side="right")

        field(b, _("Name"))
        name = tk.StringVar(b, value=s["name"])
        e = entry(b, name, size=10)
        e.box.pack(fill="x", pady=(0, 12))

        def save_name(*_a):
            v = name.get().strip()
            if v and v != (self._current() or {}).get("name"):
                self._later(f"name:{sid}", lambda: self._change(sid, name=v), 500)
        name.trace_add("write", save_name)

        # Datei: Überschrift mit „Ersetzen …“, darunter Name und Länge
        frow = tk.Frame(b, bg=COL["panel"])
        frow.pack(fill="x")
        theme.section(frow, _("Audio file")).pack(side="left")
        Button(frow, _("Replace …"), "folder", command=lambda: self._replace_file(sid), height=26,
               padx=9).pack(side="right")
        info = self.info.get(s["file"])
        fname = Path(s["file"]).name or "–"
        exists = bool(s["file"]) and Path(s["file"]).is_file()
        nrow = tk.Frame(b, bg=COL["panel"])
        nrow.pack(fill="x", pady=(4, 10))
        dur_txt = f" · {soundboard.fmt_precise(info['duration'], 1)}" if info else ""
        label(nrow, theme.elide(self.root, fname, theme.font(self.root, 10), inner_w - 70), 10,
              color=COL["fg"] if exists else COL["danger"], anchor="w").pack(side="left")
        if dur_txt:
            label(nrow, dur_txt, 9, color=COL["faint"]).pack(side="left")

        # Zuschnitt
        trow = tk.Frame(b, bg=COL["panel"])
        trow.pack(fill="x", pady=(0, 6))
        theme.section(trow, _("Trim")).pack(side="left")
        dur = info["duration"] if info else 0.0
        start, end = soundboard.trim_range(s, dur) if info else (0.0, 0.0)
        if info:
            self._side["plays_var"] = tk.StringVar(b)
            label(trow, textvariable=self._side["plays_var"], size=9, color=COL["muted"]).pack(side="right")
            tk.Label(trow, image=self.icons.get("scissors", 12, COL["muted"]), bg=COL["panel"]).pack(
                side="right", padx=(0, 4))
        trim = TrimEditor(b, inner_w, s["color"], info, start, end,
                          on_drag=lambda a, z: self._trim_labels(a, z, dur),
                          on_change=lambda a, z: self._set_trim(sid, a, z, dur))
        trim.pack(anchor="w")
        self._side["trim"] = trim
        if info:
            tf = tk.Frame(b, bg=COL["panel"])
            tf.pack(fill="x", pady=(8, 0))
            for i, (key, cap) in enumerate((("start", _("Start")), ("end", _("End")))):
                var = tk.StringVar(b)
                self._side[f"{key}_var"] = var
                box = tk.Frame(tf, bg=COL["panel2"], highlightthickness=1, highlightbackground=COL["border"],
                               highlightcolor=COL["accent"], padx=10)
                box.pack(side="left", fill="x", expand=True, padx=(0, 4) if i == 0 else (4, 0))
                tk.Label(box, text=cap.upper(), bg=COL["panel2"], fg=COL["faint"],
                         font=theme.font(self.root, 8, "bold")).pack(side="left")
                ent = tk.Entry(box, textvariable=var, bg=COL["panel2"], fg=COL["fg"], relief="flat", width=7,
                               justify="right", insertbackground=COL["fg"], highlightthickness=0,
                               font=theme.font(self.root, 10))
                ent.pack(side="right", ipady=5)

                def commit(_e=None, key=key, var=var):
                    t = soundboard.parse_time(var.get())
                    a, z = trim.start, trim.end
                    if t is not None:
                        if key == "start":
                            a = max(0.0, min(t, z - soundboard.MIN_LENGTH))
                        else:
                            z = min(dur, max(t, a + soundboard.MIN_LENGTH))
                    trim.set(start=a, end=z)
                    self._set_trim(sid, a, z, dur)
                ent.bind("<Return>", commit)
                ent.bind("<FocusOut>", commit)
            self._trim_labels(start, end, dur)
        else:
            label(b, _("Trimming works with WAV files only"), 8, color=COL["muted"], anchor="w").pack(
                fill="x", pady=(6, 0))

        # Farbe
        field(b, _("Colour"), pady=(14, 6))
        sw = tk.Canvas(b, height=26, width=inner_w, bg=COL["panel"], highlightthickness=0, bd=0)
        sw.pack(fill="x", pady=(0, 12))
        gap = (inner_w - 6 - 22) / (len(soundboard.COLORS) - 1)
        for i, c in enumerate(soundboard.COLORS):
            x = 3 + i * gap
            sw.create_oval(x, 2, x + 22, 24, fill=c, outline=COL["fg"] if c == s["color"] else "", width=2,
                           tags=(f"c{i}",))
            sw.tag_bind(f"c{i}", "<Button-1>", lambda ev, c=c: (self._change(sid, color=c), self._show_settings()))
        sw.configure(cursor="hand2")

        # Lautstärke: Überschrift und Wert in einer Zeile, Regler darunter
        vhead = tk.Frame(b, bg=COL["panel"])
        vhead.pack(fill="x")
        theme.section(vhead, _("Volume")).pack(side="left")
        val = tk.StringVar(b, value=f"{s['volume']} %")
        label(vhead, textvariable=val, size=10).pack(side="right")
        fd = Fader(b, length=inner_w, thickness=24, orient="horizontal", value=s["volume"], color=s["color"])
        fd.pack(fill="x", pady=(4, 10))

        def on_vol(v):
            v = int(round(v))
            val.set(f"{v} %")
            self._later(f"vol:{sid}", lambda: self._change(sid, volume=v), 300)
        fd.command = on_vol

        lrow = tk.Frame(b, bg=COL["panel"])
        lrow.pack(fill="x", pady=(0, 14))
        txt = tk.Frame(lrow, bg=COL["panel"])
        txt.pack(side="left", fill="x", expand=True)
        label(txt, _("Loop"), 10).pack(anchor="w")
        label(txt, _("Repeat until stopped"), 8, color=COL["muted"]).pack(anchor="w")
        sw_loop = Switch(lrow)
        sw_loop.set(s["loop"])
        sw_loop.command = lambda: self._change(sid, loop=sw_loop.get())
        sw_loop.pack(side="right")

        # Auslöser: die Taste auf dem Deck
        field(b, _("Triggers"))
        trg = tk.Frame(b, bg=COL["panel2"], highlightthickness=1, highlightbackground=COL["border"], padx=10)
        trg.pack(fill="x", pady=(0, 14))
        tk.Label(trg, image=self.icons.get("deck", 14, COL["muted"]), bg=COL["panel2"]).pack(side="left")
        lb = {"name": label(trg, "", 10, bg=COL["panel2"], anchor="w"),
              "value": label(trg, "", 9, color=COL["muted"], bg=COL["panel2"])}
        lb["name"].pack(side="left", padx=(10, 0), ipady=8)
        lb["value"].pack(side="right")
        self._side["trigger"] = lb
        self._set_trigger(lb)
