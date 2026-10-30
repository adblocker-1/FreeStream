"""Ansicht „Soundboard“ von FreeWave: Klang-Kacheln, Einstellungen rechts, Ziel und Lautstärke unten.

Ein Klick auf eine Kachel spielt den Klang (nochmals: stoppen) und wählt ihn aus; rechts stehen dann
Name, Datei, Farbe, Lautstärke, Schleife und Löschen. Unten: „Abspielen in“ (welcher Mix – oder alle),
„Mithören in“ (selbst hören: ein Mix oder ein Ausgabegerät) und die Gesamtlautstärke.
Gespeichert wird in config.json (soundboard.py), immer im Arbeits-Thread des Fensters.
"""
import tkinter as tk
from pathlib import Path

from . import soundboard, theme
from .dialogkit import ScrollArea, entry, field
from .i18n import _
from .theme import COL, Button, Fader, Switch, label, rounded_rect

PAD_W, PAD_H = 128, 96
SIDE_W = 280
POLL_MS = 300


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
        rounded_rect(self, 0, 0, w - 1, self.h - 1, 8, fill=COL["raised"] if self.hover else COL["panel2"],
                     outline=COL["border"])
        x = 12
        self.create_text(x, self.h / 2, text=self.prefix, anchor="w", fill=COL["muted"], font=self.f)
        x += pw + 6
        if vw:
            self.create_text(x, self.h / 2, text=self.value, anchor="w", fill=COL["fg"], font=self.fv)
            x += vw
        self.create_image(w - 16, self.h / 2, image=self.icons.get("down", 10, COL["muted"]))


class SoundPad(tk.Canvas):
    """Kachel eines Klangs: Farbe, Name; spielt = farbiger Rand und Stopp-Symbol."""

    def __init__(self, master, sound, command=None, select=None):
        super().__init__(master, width=PAD_W, height=PAD_H, bg=theme._bg_of(master), highlightthickness=0, bd=0,
                         cursor="hand2")
        self.sound, self.command, self.select = sound, command, select
        self.playing = self.selected = self.hover = False
        self.icons = theme.Icons(self)
        self.f, self.fs = theme.font(self, 10, "bold"), theme.font(self, 8)
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

    def set(self, sound=None, playing=None, selected=None):
        if sound is not None and sound != self.sound:
            self.sound = sound
            self._draw()
        kw = {k: v for k, v in (("playing", playing), ("selected", selected)) if v is not None}
        if kw:
            self._set(**kw)

    def _draw(self):
        self.delete("all")
        s, w, h = self.sound, PAD_W, PAD_H
        color = s["color"]
        base = theme.mix(COL["panel"], color, 0.22 if self.playing else 0.12)
        fill = theme.mix(base, COL["fg"], 0.05) if self.hover else base
        out = color if self.playing else (COL["fg"] if self.selected else COL["border"])
        rounded_rect(self, 1, 1, w - 2, h - 2, 12, fill=fill, outline=out, width=2 if (self.playing or self.selected)
                     else 1)
        # runder Knopf oben links: Abspielen bzw. Stoppen
        self.create_oval(12, 12, 40, 40, fill=color, outline="")
        self.create_image(26, 26, image=self.icons.get("stop" if self.playing else "play", 12, "#ffffff"))
        if s.get("loop"):
            self.create_image(w - 22, 26, image=self.icons.get("undo", 14, COL["muted"]))
        self.create_text(12, h - 32, text=theme.elide(self, s["name"], self.f, w - 24), anchor="w", fill=COL["fg"],
                         font=self.f)
        sub = _("Playing") if self.playing else f"{s['volume']} %"
        self.create_text(12, h - 15, text=sub, anchor="w", fill=color if self.playing else COL["muted"],
                         font=self.fs)


class AddPad(tk.Canvas):
    """Kachel „+ Sound hinzufügen“."""

    def __init__(self, master, command):
        super().__init__(master, width=PAD_W, height=PAD_H, bg=theme._bg_of(master), highlightthickness=0, bd=0,
                         cursor="hand2")
        self.command, self.hover = command, False
        self.icons = theme.Icons(self)
        self.f = theme.font(self, 9)
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
                     outline=COL["border"])
        fg = COL["fg"] if self.hover else COL["muted"]
        self.create_image(w / 2, h / 2 - 10, image=self.icons.get("plus", 14, fg))
        self.create_text(w / 2, h / 2 + 14, text=_("Add sound"), fill=fg, font=self.f)


class SidePanel(tk.Canvas):
    """Fläche rechts mit runden Ecken; Inhalt in .body – leer mittig, sonst oben."""

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
        y = max(20, (h - self.body.winfo_reqheight()) / 2) if self.centered else 20
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
        self.visible = False
        self._ids = None

        self.side = SidePanel(frame)
        self.side.pack(side="right", fill="y", padx=(0, 22), pady=(0, 14))
        left = tk.Frame(frame, bg=COL["bg"])
        left.pack(side="left", fill="both", expand=True)
        bar = tk.Frame(left, bg=COL["bg"])
        bar.pack(side="bottom", fill="x", padx=(22, 16), pady=(0, 12))
        tk.Frame(left, bg=COL["border"], height=1).pack(side="bottom", fill="x", padx=(22, 16), pady=(0, 10))
        # Lautstärke zuerst (rechts): bei schmalem Fenster bleibt sie ganz sichtbar
        self.icons = theme.Icons(self.root)
        self.vol_val = tk.StringVar(self.root, value=f"{self.volume} %")
        label(bar, textvariable=self.vol_val, size=9, color=COL["muted"], width=5, anchor="w").pack(side="right")
        self.vol = Fader(bar, length=110, thickness=24, orient="horizontal", value=self.volume,
                         command=self._on_volume)
        self.vol.pack(side="right", padx=(6, 8))
        self._spk = tk.Label(bar, image=self.icons.get("speaker", 16, COL["muted"]), bg=COL["bg"])
        self._spk.pack(side="right")
        self.play_btn = DropButton(bar, _("Play in"), command=self._play_menu)
        self.play_btn.pack(side="left")
        self.mon_btn = DropButton(bar, _("Monitor in"), command=self._monitor_menu)
        self.mon_btn.pack(side="left", padx=(8, 8))
        self.area = ScrollArea(left, bg=COL["bg"])
        self.area.pack(fill="both", expand=True, padx=(22, 16), pady=(0, 8))
        self.grid = tk.Frame(self.area.inner, bg=COL["bg"])
        self.grid.pack(fill="x", anchor="nw")
        self.grid.bind("<Configure>", lambda e: self._layout())
        self.add_pad = AddPad(self.grid, self.add_sound)
        self._fill()
        self._show_settings()
        self._update_bar()

    # ------------------------------------------------------- Anzeige
    def show(self):
        self.visible = True
        self._update_bar()
        self._poll()

    def hide(self):
        self.visible = False

    def _poll(self):
        """Spielt noch? Solange die Ansicht sichtbar ist (oder etwas spielt), alle POLL_MS prüfen."""
        playing = self.player.playing()
        for sid, pad in self.pads.items():
            pad.set(playing=sid in playing)
        side = getattr(self, "_side_play", None)
        if side is not None and self.selected:
            try:
                side.configure_text(text=_("Stop") if self.selected in playing else _("Play"),
                                    icon="stop" if self.selected in playing else "play")
            except tk.TclError:
                pass
        if self.visible or playing:
            self.root.after(POLL_MS, self._poll)

    def _fill(self):
        ids = tuple((s["id"], s["name"], s["color"], s["volume"], s["loop"]) for s in self.sounds)
        if ids == self._ids:
            return
        self._ids = ids
        old = self.pads
        self.pads = {}
        for s in self.sounds:
            pad = old.pop(s["id"], None)
            if pad is None:
                pad = SoundPad(self.grid, s, command=self.toggle, select=self.select)
            else:
                pad.set(sound=s)
            pad.set(selected=s["id"] == self.selected)
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
                x, y = 0, y + PAD_H + 12
            w.place(x=x, y=y)
            x += PAD_W + 12
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
        self.mon_btn.set(theme.elide(self.mon_btn, self._monitor_name(self.monitor_in), self.mon_btn.fv, 220))

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
        playing = self.player.playing()
        for sid, pad in self.pads.items():
            pad.set(playing=sid in playing)
        if playing and not self.visible:
            self.root.after(POLL_MS, self._poll)

    def stop_all(self):
        self.player.stop()

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

    def _show_settings(self):
        side = self.side
        s = self._current()
        side.clear(centered=s is None)
        self._side_play = None
        b = side.body
        if s is None:
            tk.Label(b, image=self.icons.get("music", 22, COL["muted"]), bg=COL["panel"]).pack()
            label(b, _("Select a sound to edit its settings"), 10, color=COL["muted"], wraplength=SIDE_W - 60,
                  justify="center").pack(pady=(10, 0))
            return
        sid = s["id"]
        head = tk.Frame(b, bg=COL["panel"])
        head.pack(fill="x", pady=(0, 16))
        tk.Label(head, image=theme.icon_tile(self.root, "waveform", 36, s["color"]), bg=COL["panel"]).pack(
            side="left")
        label(head, _("Sound settings"), 12, "bold").pack(side="left", padx=10)

        field(b, _("Name"))
        name = tk.StringVar(b, value=s["name"])
        e = entry(b, name, size=10)
        e.box.pack(fill="x", pady=(0, 14))

        def save_name(*_a):
            v = name.get().strip()
            if v and v != (self._current() or {}).get("name"):
                self._later(f"name:{sid}", lambda: self._change(sid, name=v), 500)
        name.trace_add("write", save_name)

        field(b, _("Audio file"))
        row = tk.Frame(b, bg=COL["panel"])
        row.pack(fill="x", pady=(0, 14))
        fname = Path(s["file"]).name or "–"
        exists = bool(s["file"]) and Path(s["file"]).is_file()
        label(row, theme.elide(self.root, fname, theme.font(self.root, 9), SIDE_W - 150), 9,
              color=COL["fg"] if exists else COL["danger"], anchor="w").pack(side="left", fill="x", expand=True)
        Button(row, _("Replace …"), "folder", command=lambda: self._replace_file(sid), height=28).pack(side="right")

        field(b, _("Colour"))
        sw = tk.Canvas(b, height=26, bg=COL["panel"], highlightthickness=0, bd=0)
        sw.pack(fill="x", pady=(0, 14))
        for i, c in enumerate(soundboard.COLORS):
            x = 2 + i * 30
            sw.create_oval(x, 2, x + 22, 24, fill=c, outline=COL["fg"] if c == s["color"] else "", width=2,
                           tags=(f"c{i}",))
            sw.tag_bind(f"c{i}", "<Button-1>", lambda ev, c=c: (self._change(sid, color=c), self._show_settings()))
        sw.configure(cursor="hand2")

        field(b, _("Volume"))
        vrow = tk.Frame(b, bg=COL["panel"])
        vrow.pack(fill="x", pady=(0, 14))
        val = tk.StringVar(b, value=f"{s['volume']} %")
        fd = Fader(vrow, length=SIDE_W - 110, thickness=24, orient="horizontal", value=s["volume"],
                   color=s["color"])
        fd.pack(side="left")
        label(vrow, textvariable=val, size=9, color=COL["muted"]).pack(side="left", padx=8)

        def on_vol(v):
            v = int(round(v))
            val.set(f"{v} %")
            self._later(f"vol:{sid}", lambda: self._change(sid, volume=v), 300)
        fd.command = on_vol

        lrow = tk.Frame(b, bg=COL["panel"])
        lrow.pack(fill="x", pady=(0, 20))
        txt = tk.Frame(lrow, bg=COL["panel"])
        txt.pack(side="left", fill="x", expand=True)
        label(txt, _("Loop"), 10).pack(anchor="w")
        label(txt, _("Repeat until stopped"), 8, color=COL["muted"]).pack(anchor="w")
        sw_loop = Switch(lrow)
        sw_loop.set(s["loop"])
        sw_loop.command = lambda: self._change(sid, loop=sw_loop.get())
        sw_loop.pack(side="right")

        btns = tk.Frame(b, bg=COL["panel"])
        btns.pack(fill="x")
        playing = self.player.is_playing(sid)
        self._side_play = Button(btns, _("Stop") if playing else _("Play"), "stop" if playing else "play",
                                 kind="primary", command=lambda: self.toggle(self._current() or s))
        self._side_play.pack(side="left")
        Button(btns, _("Delete sound"), "trash", kind="danger", command=lambda: self._delete(sid)).pack(side="right")
