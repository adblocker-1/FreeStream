"""Fenster von Linewave: Mixer (Monitor-/Stream-Mix je Kanal), Mikrofon, Effekte und Programme.

Aufbau (Streamlin-Design): links die Seitenleiste mit Mikrofon, den Ansichten
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
import math
import queue
import threading
import time
import tkinter as tk
from tkinter import font as tkfont

from PIL import Image, ImageDraw, ImageTk

from . import ducking, levels, parts, shortcuts, theme, wavemic
from .dialogkit import ScrollArea
from .i18n import _
from .theme import (COL, Button, Card, Chip, Fader, HScroll, InfoCard, LevelMeter, NavItem, RoundToggle, Segmented,
                    Sidebar, SoftSegmented, Switch, icon_tile, label, rounded_rect)

log = logging.getLogger("streamlin.waveui")
REFRESH_MS = 1500
LEVEL_MS = 40  # 25 Hz
TOUCH_HOLD = 2.5
MIX_LABEL = {"monitor": "Monitor", "stream": "Stream"}
MUTE_ICONS = {"monitor": ("headphones", "headphones_off"), "stream": ("stream", "stream_off")}
NOT_IN_MIXER = _("– not in mixer –")
CHANNEL_LOOK = {  # Kanal -> (Symbol, Farbe)
    "system": ("system", "#64748b"), "music": ("music", "#1fa463"), "browser": ("globe", "#ea580c"),
    "voice": ("chat", "#5865f2"), "game": ("game", "#f59e0b"), "sfx": ("spark", "#ec4899"),
    "mic": ("mic", "#e5484d"),
}
FADER_LEN = 190
FADER_LEN_SMALL = 140
MIC_SHORTCUT = "linewave-mic-mute"
SCENE_SHORTCUT = "linewave-scene-"
DUCK_MS = 80


def mix_color(mix):
    return COL["monitor"] if mix == "monitor" else COL["stream"]


def channel_look(c):
    """(Symbol, Farbe) eines Kanals – eigene Angaben gehen vor."""
    if isinstance(c, dict):
        base = CHANNEL_LOOK.get(c["id"], ("input" if c.get("kind") == "input" else "wave", "#64748b"))
        return c.get("icon") or base[0], c.get("color") or base[1]
    return CHANNEL_LOOK.get(c, ("wave", "#64748b"))


def post_fader(level, volume, muted):
    """Pegel nach dem Regler: Lautstärke in dB auf die Anzeige (−60…0 dB) umgerechnet."""
    if muted or volume <= 0 or level <= 0:
        return 0.0
    return max(0.0, min(1.0, level + 20 * math.log10(min(volume, 150) / 100) / -levels.FLOOR_DB))


def _fit_text(fnt, text, width):
    if fnt.measure(text) <= width:
        return text
    while text and fnt.measure(text + "…") > width:
        text = text[:-1]
    return text + "…"


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
    """Stumm-Knopf der Kanal-Karten: abgerundetes Feld; stumm = rot hinterlegt."""

    W, H = 34, 30

    def __init__(self, master, icon_off, icon_on, command=None):
        super().__init__(master, icon_off, icon_on, size=self.H, command=command)
        self.configure(width=self.W, height=self.H)
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.W, self.H
        if self.state_on:
            fill, out, fg = COL["danger_soft"], "", COL["danger"]
        else:
            fill, out, fg = (COL["raised"] if self.hover else COL["panel2"]), COL["border"], COL["muted"]
        rounded_rect(self, 0, 0, w - 1, h - 1, 9, fill=fill, outline=out)
        self.create_image(w / 2, h / 2, image=self.icons.get(self.icon_off, 15, fg))


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
        self.count = len(names)
        self.active = bool(names)
        self.text = " · ".join(names) if names else _("Effects")
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
        fnt = tkfont.Font(self, font=self.f)
        self.create_text(w - 9, h / 2, text=right, anchor="e", font=self.f, fill=fg)
        avail = w - 24 - 10 - fnt.measure(right) - 8
        self.create_text(23, h / 2, text=_fit_text(fnt, self.text, avail), anchor="w", font=self.f, fill=fg)


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


class AppChip(tk.Canvas):
    """Programm mit Kanal: [Symbol] Spotify │ → Musik. Klick öffnet die Kanalauswahl."""

    def __init__(self, master, app, target, tile, command=None, height=36):
        super().__init__(master, height=height, bg=theme._bg_of(master), highlightthickness=0, bd=0,
                         cursor="hand2")
        self.app, self.target, self.tile, self.command, self.h = app, target, tile, command, height
        self.hover = False
        self.icons = theme.Icons(self)
        self.f = theme.font(self, 10, "normal")
        self.fs = theme.font(self, 9, "normal")
        self.bind("<Button-1>", lambda e: self.command and self.command(self))
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self._draw()

    def _set_hover(self, v):
        self.hover = v
        self._draw()

    def _draw(self):
        self.delete("all")
        f, fs = tkfont.Font(self, font=self.f), tkfont.Font(self, font=self.fs)
        app = _fit_text(f, self.app, 150)
        aw, tw = f.measure(app), fs.measure(self.target)
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
        self._last_refresh = 0.0
        self.state = {}
        self.strips = {}
        self._strip_ids = None
        self._stream_keys = None
        self._fx_ids = None
        self._apps_ids = None
        self.view = "mixer"
        self.fader_len = FADER_LEN
        root.title("Linewave")
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
            self.tray = Tray(self.request_show, self.request_quit, icon=make_icon(64), title="Linewave",
                             ident="linewave", open_label=_("Open Linewave"))
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
            notify(_("Linewave is still running"),
                   _("Mixer and microphone stay active. Open it again from the “Linewave” menu."),
                   app="Linewave", icon="audio-volume-high")

    def open_settings(self):
        """Design, Sprache und Updates."""
        from .settingsui import open_settings
        return open_settings(self.root, "Linewave", self._restart)

    def _restart(self):
        theme.restart_later("streamlin.linewave")
        self.quit_app()

    def quit_app(self):
        self.request_quit()

    def open_lindeck(self):
        """Das Schwesterprogramm LinDeck (Stream Deck) öffnen."""
        import shutil
        import subprocess
        import sys
        exe = shutil.which("lindeck")
        cmd = [exe] if exe else [sys.executable, "-m", "streamlin"]
        try:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError as e:
            self.status(_("Error: {error}").format(error=e))

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
        if self.visible() and not self._refreshing and time.time() - self._last_refresh > REFRESH_MS / 1000:
            self.refresh()
        self.root.after(100, self._poll)

    def _quit(self):
        if self.quitting:
            return
        self.quitting = True
        self.levels.stop()
        if self.tray:
            self.tray.stop()
        self.root.quit()

    def status(self, text):
        self.status_var.set(text)

    # ============================================================== Aufbau
    def _build(self):
        r = self.root
        self._build_sidebar()
        tk.Frame(r, bg=COL["border"], width=1).pack(side="left", fill="y")
        main = tk.Frame(r, bg=COL["bg"])
        main.pack(side="left", fill="both", expand=True)
        self.main = main
        self._build_header(main)
        self.status_var = tk.StringVar(self.root, value="")
        tk.Label(main, textvariable=self.status_var, bg=COL["bg"], fg=COL["muted"], anchor="w",
                 font=theme.font(r, 9)).pack(fill="x", side="bottom", padx=22, pady=(0, 6))
        self.content = tk.Frame(main, bg=COL["bg"])
        self.content.pack(fill="both", expand=True)
        self.view_mixer = tk.Frame(self.content, bg=COL["bg"])
        self.view_mic = tk.Frame(self.content, bg=COL["bg"])
        self.view_fx = tk.Frame(self.content, bg=COL["bg"])
        self.view_apps = tk.Frame(self.content, bg=COL["bg"])
        self._build_mixer()
        self._build_mic()
        self._build_fx_view()
        self._build_apps_view()
        self.show_view("mixer")

    # ------------------------------------------------------- Seitenleiste
    def _build_sidebar(self):
        sb = Sidebar(self.root)
        sb.pack(side="left", fill="y")
        self.sidebar = sb
        sb.brand("Linewave", "wave")
        self.mic_head = tk.StringVar(self.root, value=_("Looking for microphone …"))
        self.mic_card = InfoCard(sb.top, _("Microphone"), _("Looking for microphone …"),
                                 image=icon_tile(self.root, "mic", 34, COL["device"]), height=58,
                                 command=lambda: self.show_view("mic"))
        self.mic_card.pack(fill="x", pady=(0, 6))
        sb.section(_("Mix"))
        self.nav = {}
        for key, text, icon in (("mixer", _("Mixer"), "sliders"), ("mic", _("Microphone"), "mic"),
                                ("effects", _("Effects"), "spark"), ("apps", _("Apps"), "grid")):
            item = NavItem(sb.top, text, icon, "", command=lambda k=key: self.show_view(k))
            item.icons = _NavIcons(item)
            item._draw()
            item.pack(fill="x", pady=1)
            self.nav[key] = item
        NavItem(sb.top, _("Setup wizard"), "gear", command=self.open_setup).pack(fill="x", pady=1)
        sb.section(_("Outputs"))
        self.monitor_card = InfoCard(sb.top, _("Monitor Mix"), "–",
                                     image=icon_tile(self.root, "headphones", 34, COL["monitor"]),
                                     command=self._output_menu)
        self.monitor_card.pack(fill="x", pady=(0, 8))
        self.stream_card = InfoCard(sb.top, _("Stream Mix"), _("OBS · Discord"),
                                    image=icon_tile(self.root, "stream", 34, COL["stream"]))
        self.stream_card.pack(fill="x")

        sb.bottom.configure(padx=8)
        row = tk.Frame(sb.bottom, bg=COL["side"])
        row.pack(fill="x")
        if parts.has_lindeck():  # Link zum Schwesterprogramm, falls installiert
            link = tk.Label(row, text=" LinDeck", image=self.icons.get("grid", 15, COL["muted"]), compound="left",
                            bg=COL["side"], fg=COL["fg"], font=theme.font(self.root, 10), cursor="hand2", padx=6,
                            pady=4)
            link.pack(side="right")
            link.bind("<Button-1>", lambda e: self.open_lindeck())
            link.bind("<Enter>", lambda e: link.configure(bg=COL["raised"]))
            link.bind("<Leave>", lambda e: link.configure(bg=COL["side"]))
        self.settings_item = theme.NavItem(row, _("Settings"), "gear", command=self.open_settings)
        self.settings_item.configure(width=1)  # Breite kommt von der Seitenleiste
        self.settings_item.pack(side="left", fill="x", expand=True)

    def _output_menu(self):
        """Ausgabegerät des Monitor-Mixes wählen (Kopfhörer, Lautsprecher …)."""
        outs = self.state.get("outputs") or []
        menu = tk.Menu(self.root, tearoff=False, bg=COL["panel2"], fg=COL["fg"], activebackground=COL["accent"],
                       activeforeground=COL["on_accent"], selectcolor=COL["accent"], bd=0)
        if not outs:
            menu.add_command(label=_("No output device found"), state="disabled")
        for name, desc in outs:
            menu.add_radiobutton(label=desc, value=desc, variable=self.out_var,
                                 command=self._on_output)
        try:
            menu.tk_popup(self.monitor_card.winfo_rootx() + 12,
                          self.monitor_card.winfo_rooty() + self.monitor_card.winfo_height())
        finally:
            menu.grab_release()

    # -------------------------------------------------------------- Kopf
    def _build_header(self, parent):
        head = tk.Frame(parent, bg=COL["bg"], padx=22, pady=9)
        head.pack(fill="x")
        left = tk.Frame(head, bg=COL["bg"])
        left.pack(side="left")
        self.title_var = tk.StringVar(self.root, value=_("Mixer"))
        self.sub_var = tk.StringVar(self.root, value="")
        label(left, textvariable=self.title_var, size=16, weight="bold").pack(anchor="w")
        label(left, textvariable=self.sub_var, size=9, color=COL["muted"]).pack(anchor="w")
        more = Button(head, icon="more", kind="ghost", command=self._more_menu)
        more.pack(side="right", padx=(8, 0))
        self.more_btn = more
        self.add_btn = Button(head, _("Add channel"), "plus", kind="primary", command=self._new_channel)
        self.scene_btn = Button(head, _("Scenes"), "layers", command=self._scene_menu)
        self.setup_btn = Button(head, _("Set up mixer"), "wave", kind="primary",
                                command=lambda: self._mixer_enable(True))
        self.legend = tk.Frame(head, bg=COL["bg"])
        for mix, text in (("monitor", _("Monitor – what you hear")), ("stream", _("Stream – what your audience hears"))):
            dot = tk.Canvas(self.legend, width=10, height=10, bg=COL["bg"], highlightthickness=0)
            rounded_rect(dot, 0, 0, 10, 10, 3, fill=mix_color(mix), outline="")
            dot.pack(side="left", padx=(14, 6))
            label(self.legend, text, 9, color=COL["muted"]).pack(side="left")
        tk.Frame(parent, bg=COL["border"], height=1).pack(fill="x")

    def _more_menu(self):
        menu = tk.Menu(self.root, tearoff=False, bg=COL["panel2"], fg=COL["fg"], activebackground=COL["accent"],
                       activeforeground=COL["on_accent"], bd=0)
        menu.add_command(label=_("Setup wizard"), command=self.open_setup)
        if self.state.get("mixer_active"):
            menu.add_command(label=_("Remove mixer"), command=lambda: self._mixer_enable(False))
        else:
            menu.add_command(label=_("Set up mixer"), command=lambda: self._mixer_enable(True))
        menu.add_separator()
        menu.add_command(label=_("Settings"), command=self.open_settings)
        menu.add_separator()
        if parts.has_lindeck():
            menu.add_command(label=_("Open LinDeck"), command=self.open_lindeck)
        menu.add_command(label=_("Close window"), command=self.hide)
        menu.add_command(label=_("Quit"), command=self.quit_app)
        b = self.more_btn
        try:
            menu.tk_popup(b.winfo_rootx() + b.winfo_width() - 180, b.winfo_rooty() + b.winfo_height() + 4)
        finally:
            menu.grab_release()

    def _update_header(self):
        st = self.state
        active = bool(st.get("mixer_active"))
        for w in (self.add_btn, self.scene_btn, self.setup_btn, self.legend):
            w.pack_forget()
        if self.view == "mixer":
            self.title_var.set(_("Mixer"))
            if active:
                n = len(st.get("channels") or [])
                self.sub_var.set(_("{count} channels · everything is live").format(count=n))
                self.add_btn.pack(side="right")
                self.scene_btn.pack(side="right", padx=(0, 8))
                self.legend.pack(side="right", padx=(0, 18))
            else:
                self.sub_var.set(_("The mixer is off – set it up to give every app its own channel."))
                self.setup_btn.pack(side="right")
        elif self.view == "mic":
            self.title_var.set(_("Microphone"))
            self.sub_var.set(self.mic_name.get())
        elif self.view == "effects":
            self.title_var.set(_("Effects"))
            self.sub_var.set(_("Effect chains per channel – changes are live."))
        else:
            self.title_var.set(_("Apps"))
            self.sub_var.set(_("Click an app to move it to another channel"))
            if active:
                self.add_btn.pack(side="right")

    def show_view(self, key):
        self.view = key
        for k, item in self.nav.items():
            item.set(active=k == key)
        for k, f in (("mixer", self.view_mixer), ("mic", self.view_mic), ("effects", self.view_fx),
                     ("apps", self.view_apps)):
            if k == key:
                f.pack(fill="both", expand=True)
            else:
                f.pack_forget()
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
                   "for your headphones (Monitor Mix) and for your stream (Stream Mix)."),
              color=COL["muted"], wraplength=460, justify="center").pack()
        Button(b, _("Set up mixer"), "wave", kind="primary", command=lambda: self._mixer_enable(True)).pack(
            pady=(18, 0))

        # Mixer aktiv
        self.mix_on = tk.Frame(t, bg=COL["bg"])
        self.mix_on.bind("<Configure>", self._on_mixer_size)
        self.out_var = tk.StringVar(self.root)
        self.strip_scroll = HScroll(self.mix_on)
        self.strip_scroll.pack(fill="x", padx=22, pady=(18, 0))
        self.strip_row = self.strip_scroll.inner

        bottom = tk.Frame(self.mix_on, bg=COL["bg"])
        bottom.pack(fill="both", expand=True, padx=22, pady=(16, 10))
        bottom.grid_columnconfigure(0, weight=13)
        bottom.grid_columnconfigure(1, weight=10)
        bottom.grid_rowconfigure(0, weight=1)

        self.apps_card = Card(bottom, pad=16, radius=16, stretch=True)
        self.apps_card.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        hb = tk.Frame(self.apps_card.body, bg=COL["panel"])
        hb.pack(fill="x")
        theme.section(hb, _("Apps")).pack(side="left")
        label(hb, _("Click an app to move it to another channel"), 9, color=COL["muted"]).pack(side="left", padx=8)
        self.streams_frame = tk.Frame(self.apps_card.body, bg=COL["panel"], height=1)
        self.streams_frame.pack(fill="x", pady=(10, 0))
        self.streams_frame.bind("<Configure>", lambda e: self._reflow_chips())
        self._chips = []

        out = Card(bottom, pad=16, radius=16, stretch=True)
        out.grid(row=0, column=1, sticky="nsew")
        ob = out.body
        theme.section(ob, _("Outputs")).pack(fill="x")
        rows = tk.Frame(ob, bg=COL["panel"])
        rows.pack(fill="x", pady=(10, 0))
        rows.grid_columnconfigure(1, weight=1)
        self.mix_meters = {}
        for i, (mix, text) in enumerate((("monitor", _("Monitor Mix")), ("stream", _("Stream Mix")))):
            label(rows, text, 9).grid(row=i, column=0, sticky="w", pady=4)
            m = LevelMeter(rows, length=120, thickness=8, orient="horizontal", color=mix_color(mix))
            m.grid(row=i, column=1, sticky="ew", padx=(14, 10), pady=4)
            db = tk.StringVar(self.root, value="")
            label(rows, textvariable=db, size=9, color=COL["muted"], width=7, anchor="e").grid(row=i, column=2,
                                                                                                sticky="e")
            self.mix_meters[mix] = {"meter": m, "db": db}
        hint = Card(ob, color=COL["panel2"], outline="", radius=10, pad=10, stretch=True)
        hint.pack(fill="x", pady=(14, 0))
        hb = hint.body
        tk.Label(hb, image=self.icons.get("stream", 15, COL["muted"]), bg=COL["panel2"]).pack(side="left",
                                                                                              padx=(2, 8))
        before, sep, after = _("In OBS add {source}").partition("{source}")
        if before.strip():
            label(hb, before.strip(), 9, color=COL["muted"]).pack(side="left")
        label(hb, _("Monitor of {device}").format(device=_("Linewave Stream Mix")), 9, "bold").pack(
            side="left", padx=6)
        if after.strip():
            label(hb, after.strip(), 9, color=COL["muted"]).pack(side="left")

    def _on_mixer_size(self, e):
        """Kleines Fenster: kürzere Regler, damit unten Platz für Programme und Ausgänge bleibt."""
        length = FADER_LEN if e.height >= 640 else FADER_LEN_SMALL
        if length != self.fader_len:
            self.fader_len = length
            if self.strips:
                self._build_strips(self.state.get("channels") or [])
                self.apply_state(self.state)

    def _mixer_enable(self, on):
        self.status(_("Setting up mixer …") if on else _("Removing mixer …"))
        self._strip_ids = None

        def done(_res, err):
            if err is None:
                self.status(_("Mixer set up") if on else _("Mixer removed"))
            self.refresh()
        self.worker.submit(lambda: self.host.enable_mixer(on), done)

    def _build_strips(self, channels):
        for w in self.strip_row.winfo_children():
            w.destroy()
        self.strips = {}
        # Mikrofon zuerst – wie in Wave Link
        ordered = sorted(channels, key=lambda c: c["id"] != "mic")
        first = None
        for ch in ordered:
            cid, name = ch["id"], ch["name"]
            glyph, color = channel_look(ch)
            card = Card(self.strip_row, pad=12, radius=16, stripe=color, width=136)
            card.pack(side="left", padx=(0, 12), anchor="n")
            first = first or card
            b = card.body
            tk.Label(b, image=icon_tile(self.root, glyph, 40, color, radius=0.3), bg=COL["panel"]).pack(pady=(4, 0))
            label(b, name, 10, "bold").pack(pady=(6, 0))
            apps = tk.StringVar(self.root, value=" ")
            label(b, textvariable=apps, size=8, color=COL["muted"]).pack()
            edit = tk.Label(b, image=self.icons.get("more", 14, COL["faint"]), bg=COL["panel"], cursor="hand2")
            edit.place(relx=1.0, x=2, y=-4, anchor="ne")
            edit.bind("<Button-1>", lambda e, c=ch: self._edit_channel(c))
            row = tk.Frame(b, bg=COL["panel"])
            row.pack(pady=(10, 0))
            strip = {"apps": apps, "card": card, "channel": ch, "edit": edit}
            for col, mix in enumerate(("monitor", "stream")):
                track = tk.Frame(row, bg=COL["panel"])
                track.grid(row=0, column=col, padx=8)
                meter = LevelMeter(track, length=self.fader_len - 16, thickness=4)
                meter.pack(side="left", padx=(0, 2), pady=8)
                fader = Fader(track, length=self.fader_len, thickness=22, color=mix_color(mix),
                              command=lambda v, c=cid, m=mix: self._on_level(c, m, v))
                fader.pack(side="left")
                val = tk.StringVar(self.root, value="–")
                label(row, textvariable=val, size=9, weight="bold", color=COL["muted"], width=5).grid(
                    row=1, column=col, pady=(4, 6))
                off, on = MUTE_ICONS[mix]
                mute = MuteToggle(row, off, on, command=lambda c=cid, m=mix: self._on_mute(c, m))
                mute.grid(row=2, column=col)
                strip[mix] = {"fader": fader, "val": val, "mute": mute, "meter": meter}
            strip["fx"] = FxPill(b, 112, command=lambda c=ch: self._open_effects(c))
            strip["fx"].set(ch.get("fx_names") or [])
            strip["fx"].pack(pady=(12, 0))
            self.strips[cid] = strip
        add = AddCard(self.strip_row, self._new_channel)
        add.pack(side="left", anchor="n")
        if first is not None:
            self.root.update_idletasks()
            add.configure(height=max(200, first.winfo_reqheight()))
            add._draw()

    def _on_level(self, cid, mix, value):
        pct = int(round(float(value)))
        self.strips[cid][mix]["val"].set(f"{pct} %")
        self._debounced(f"lvl:{cid}:{mix}", lambda v: self.mixer.set_volume(cid, mix, v), pct)

    def _on_mute(self, cid, mix):
        s = self.strips[cid][mix]
        on = s["mute"].get()
        s["fader"].set_dimmed(on)
        self._touch(f"mute:{cid}:{mix}")
        self.worker.submit(lambda: self.mixer.set_mute(cid, mix, on))

    def _on_output(self, _e=None):
        lbl = self.out_var.get()
        name = next((n for n, d in self.state.get("outputs", []) if d == lbl), None)
        if not name:
            return
        self._touch("output")
        self.monitor_card.set(subtitle=lbl)

        def done(res, err):
            if err is None:
                self.status(_("Monitor Mix → {output}").format(output=lbl))
        self.worker.submit(lambda: self.mixer.set_monitor_output(name), done)

    # ------------------------------------------------------- Programme
    def _app_channels(self, channels):
        return [c for c in channels if c["id"] != "mic" and c.get("kind", "apps") == "apps"]

    def _stream_menu(self, s, chip):
        """Kanal für ein Programm wählen (wie früher die Auswahlliste)."""
        channels = self.state.get("channels") or []
        menu = tk.Menu(self.root, tearoff=False, bg=COL["panel2"], fg=COL["fg"], activebackground=COL["accent"],
                       activeforeground=COL["on_accent"], selectcolor=COL["accent"], bd=0)
        var = tk.StringVar(menu, value=s["channel"] or "")
        menu._var = var
        for c in self._app_channels(channels):
            menu.add_radiobutton(label=c["name"], value=c["id"], variable=var,
                                 command=lambda cid=c["id"]: self._on_stream(s, cid))
        try:
            menu.tk_popup(chip.winfo_rootx(), chip.winfo_rooty() + chip.winfo_height() + 2)
        finally:
            menu.grab_release()

    def _chip_for(self, parent, s, by_id):
        ch = by_id.get(s["channel"]) if s["channel"] else None
        glyph, color = channel_look(ch or s["channel"]) if s["channel"] else ("speaker", COL["faint"])
        target = ch["name"] if ch else NOT_IN_MIXER
        return AppChip(parent, s["app"], target, icon_tile(self.root, glyph, 24, color, radius=0.3),
                       command=lambda chip, s=s: self._stream_menu(s, chip))

    def _build_streams(self, streams, channels):
        for w in self.streams_frame.winfo_children():
            w.destroy()
        self._chips = []
        by_id = {c["id"]: c for c in channels}
        if not streams:
            lb = label(self.streams_frame, _("No app is playing audio right now."), color=COL["muted"])
            self._chips = [lb]
        else:
            self._chips = [self._chip_for(self.streams_frame, s, by_id) for s in streams]
        self._reflow_chips()

    def _reflow_chips(self):
        if self._chips:
            _flow(self.streams_frame, self._chips)

    def _build_apps_view(self):
        t = self.view_apps
        self.apps_list = tk.Frame(t, bg=COL["bg"])
        self.apps_list.pack(fill="both", expand=True, padx=22, pady=18)

    def _fill_apps_view(self, streams, channels):
        for w in self.apps_list.winfo_children():
            w.destroy()
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

    def _channel_dialog(self, ch):
        from .wavedialogs import ChannelDialog

        def opened(inputs, err):
            if err is not None:
                return
            ChannelDialog(self.root, inputs or [], channel=ch, default_look=channel_look(ch or {"id": ""}),
                          on_save=lambda v: self._save_channel(ch, v),
                          on_delete=(lambda: self._delete_channel(ch)) if ch else None)
        self.worker.submit(self.mixer.inputs, opened)

    def _save_channel(self, ch, v):
        self._strip_ids = None

        def work():
            if ch is None:
                return self.mixer.add_channel(v["name"], v["kind"], v["source"], v["icon"], v["color"])
            changes = {"icon": v["icon"], "color": v["color"]}
            if ch["id"] != "mic":
                changes["name"] = v["name"]
            if ch.get("kind") == "input" or ch["id"] == "mic":
                changes["source"] = v["source"]
            return self.mixer.update_channel(ch["id"], **changes)

        def done(res, err):
            if err is None:
                self.status((_("Channel “{name}” created") if ch is None else _("Channel “{name}” saved"))
                            .format(name=res["name"]))
            self.refresh()
        self.worker.submit(work, done)

    def _delete_channel(self, ch):
        from tkinter import messagebox
        if not messagebox.askyesno(_("Delete channel"),
                                   _("Really delete channel “{name}”?").format(name=ch["name"]),
                                   parent=self.root):
            return
        self._strip_ids = None

        def done(res, err):
            if err is None:
                self.status(_("Channel “{name}” deleted").format(name=ch["name"]))
            self.refresh()
        self.worker.submit(lambda: self.mixer.remove_channel(ch["id"]), done)

    # ------------------------------------------------------- Szenen
    def _scene_menu(self):
        """Szenen-Menü am Knopf im Kopf: anwenden, aktuelle Einstellung speichern, verwalten."""
        scenes = self.state.get("scenes") or []
        menu = tk.Menu(self.root, tearoff=False, bg=COL["panel2"], fg=COL["fg"], activebackground=COL["accent"],
                       activeforeground=COL["on_accent"], selectcolor=COL["accent"], bd=0)
        var = tk.StringVar(menu, value=self.state.get("active_scene") or "")
        menu._var = var
        for sc in scenes:
            menu.add_radiobutton(label=sc.get("name") or sc["id"], value=sc["id"], variable=var,
                                 command=lambda sc=sc: self._apply_scene(sc))
        if scenes:
            menu.add_separator()
        menu.add_command(label=_("Save current mix as scene …"), command=self._new_scene)
        menu.add_command(label=_("Manage scenes …"), command=self.open_scenes)
        b = self.scene_btn
        try:
            menu.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height() + 4)
        finally:
            menu.grab_release()

    def _scene_keys(self, st=None):
        keys = (st or self.state).get("shortcuts") or {}
        return {k[len(SCENE_SHORTCUT):]: v.get("binding", "") for k, v in keys.items()
                if k.startswith(SCENE_SHORTCUT)}

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
        from tkinter import messagebox
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
        return _("Linewave: apply scene “{name}”").format(name=sc.get("name") or sc["id"])

    def _scene_shortcut(self, sc, binding):
        self._set_shortcut(SCENE_SHORTCUT + sc["id"], self._scene_shortcut_name(sc), self._scene_command(sc),
                           binding)

    # --------------------------------------------------- Tastenkürzel
    def _set_shortcut(self, ident, name, command, binding):
        """Globales Tastenkürzel eintragen (binding) oder entfernen (None) – im Arbeits-Thread."""
        def work():
            if not binding:
                shortcuts.unregister(ident)
                return None
            return shortcuts.register(ident, name, command, binding)

        def done(res, err):
            if isinstance(err, shortcuts.Unsupported):
                self.status("")
                from .wavedialogs import ManualShortcutDialog
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
        self._set_shortcut(MIC_SHORTCUT, _("Linewave: mute microphone"),
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
        from .wavedialogs import ShortcutField
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
        chans = list(self._duck_conf().get("channels") or [])
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
            raw = lv.get(cid, 0.0)
            for mix in ("monitor", "stream"):
                s = strip[mix]
                vol = s["fader"].get()
                muted = s["mute"].get()
                if f"{cid}_{mix}" not in vols:
                    vol = 0
                s["meter"].set(post_fader(raw, vol, muted))
        for mix, m in self.mix_meters.items():
            v = lv.get(f"mix:{mix}", 0.0)
            m["meter"].set(v)
            db = levels.level_db(v)
            m["db"].set(f"{db:.0f} dB" if db is not None else ("−∞ dB" if self.levels.running else ""))

    # ------------------------------------------------------ Aktualisieren
    def collect(self):
        """Zustand von Mixer und Mikrofon einlesen (läuft im Arbeits-Thread)."""
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
                chans.append(dict(c, fx=len(on), fx_names=on, fx_total=len(chain)))
            st["channels"] = chans
            st["levels"] = self.mixer.levels()
            st["outputs"] = [(o["name"], o.get("description") or o["name"]) for o in self.mixer.outputs()]
            st["monitor_output"] = self.mixer._state().get("monitor_output", "")
            st["streams"] = self.mixer.streams()
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
        return st

    def refresh(self):
        if self._refreshing:
            return
        self._refreshing = True

        def done(st, err):
            self._refreshing = False
            self._last_refresh = time.time()
            if err is None and st is not None:
                self.apply_state(st)
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
            ids = tuple((c["id"], c["name"], c.get("icon"), c.get("color"), c.get("kind")) for c in st["channels"])
            if ids != self._strip_ids:
                self._strip_ids = ids
                self._build_strips(st["channels"])
            for key, (vol, mute) in (st.get("levels") or {}).items():
                cid, mix = key.rsplit("_", 1)
                s = self.strips.get(cid, {}).get(mix)
                if not s:
                    continue
                if not self._held(f"lvl:{cid}:{mix}"):
                    s["fader"].set(min(100, vol))
                    s["val"].set(f"{vol} %")
                if not self._held(f"mute:{cid}:{mix}"):
                    s["mute"].set(bool(mute))
                    s["fader"].set_dimmed(bool(mute))
            apps = {}
            for s in st.get("streams") or []:
                if s["channel"]:
                    apps.setdefault(s["channel"], []).append(s["app"])
            by_id = {c["id"]: c for c in st["channels"]}
            sub_font = tkfont.Font(self.root, font=theme.font(self.root, 8))
            for cid, strip in self.strips.items():
                ch = by_id.get(cid, strip["channel"])
                strip["apps"].set(_fit_text(sub_font, self._channel_sub(ch, apps.get(cid, []), st), 110))
                strip["fx"].set(ch.get("fx_names") or [])
            outs = st.get("outputs") or []
            cur = next((d for n, d in outs if n == st.get("monitor_output")), st.get("monitor_output", ""))
            if not self._held("output"):
                self.out_var.set(cur)
                self.monitor_card.set(subtitle=cur or "–", dim=False)
            keys = tuple((s["index"], s["app"], s["channel"]) for s in st.get("streams") or [])
            if keys != self._stream_keys and not self._held("streams"):
                self._stream_keys = keys
                self._build_streams(st.get("streams") or [], st["channels"])
            fx_ids = tuple((c["id"], c["name"], c.get("icon"), c.get("color"), tuple(c.get("fx_names") or []),
                            c.get("fx_total", 0)) for c in st["channels"])
            if fx_ids != self._fx_ids:
                self._fx_ids = fx_ids
                self._fill_fx_view(st["channels"])
            apps_ids = (keys, ids)
            if apps_ids != self._apps_ids and not self._held("streams"):
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
            self.monitor_card.set(subtitle=_("Mixer is off"), dim=True)
        if st.get("error"):
            self.status(st["error"])

        # --- Szenen und Tastenkürzel
        scenes = st.get("scenes") or []
        active = next((x for x in scenes if x["id"] == st.get("active_scene")), None)
        self.scene_btn.configure_text(text=active.get("name") if active else _("Scenes"))
        dlg = getattr(self, "_scenes_dlg", None)
        if dlg is not None and dlg.winfo_exists():
            dlg.update_scenes(scenes, st.get("active_scene"), self._scene_keys(st))
        mic_key = ((st.get("shortcuts") or {}).get(MIC_SHORTCUT) or {}).get("binding", "")
        if not self.mic_shortcut.capturing and mic_key != self.mic_shortcut.binding:
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
            fnt = tkfont.Font(self.root, font=theme.font(self.root, 9))
            self.mic_card.set(title=name, subtitle=_fit_text(fnt, " · ".join(parts), 150) or " ",
                              dot=COL["danger"] if muted else COL["ok"], dim=False)
        else:
            self.mic_card.set(title=_("Microphone"), subtitle=_("No Wave microphone"), dot="", dim=True)
        if hw:
            hint = _("All settings are stored directly in the microphone.") + (
                " " + _("Firmware API {api}.").format(api=mic["api"]) if mic.get("api") else "")
        elif present:
            hint = (_("Low Cut and Clipguard run as PipeWire filters. In Discord/OBS, select the microphone "
                      "“Linewave Microphone”.") + ("" if st.get("limiter") else
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
        self._update_header()

    def _build_fx_placeholder(self):
        for w in self.fx_list.winfo_children():
            w.destroy()
        label(self.fx_list, _("Set up the mixer to add effects to channels."), color=COL["muted"]).pack(anchor="w")
