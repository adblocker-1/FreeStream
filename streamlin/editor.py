"""Grafischer Profil-Editor (Tk).

Aufbau wie in der Elgato-App: links das Gerät mit Tasten, Dials und Seiten,
rechts die Aktionsliste, darunter die Eigenschaften der gewählten Taste.
Aktionen werden per Drag & Drop (oder Doppelklick) auf Tasten gelegt,
Tasten per Drag & Drop verschoben. Jede Änderung wird sofort gespeichert;
der laufende LinDeck-Dienst zeigt sie wenige Sekunden später auf dem Gerät.

Start: lindeck edit [Profil]
"""
import json
import logging
import tkinter as tk
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk
from tkinter import font as tkfont

from PIL import Image, ImageDraw, ImageFilter, ImageTk

from . import actions as A
from . import catalog as C
from . import editdoc as E
from . import livekeys, parts, paths, render, theme
from .i18n import _, ngettext
from .models import MODELS, model_geometry, model_name
from .profiles import ProfileStore, delete_profile, import_path

log = logging.getLogger("streamlin.editor")

COL = theme.COL
# Kategorie der Aktionsliste -> (Symbol, Farbe)
CATEGORY_LOOK = {
    _("System"): ("keyboard", "#64748b"), _("Navigation"): ("navigate", "#3b82f6"), "Stream Deck": ("grid", "#3b82f6"),
    "Multi Action": ("layers", "#8b5cf6"), "Soundboard": ("sfx", "#ec4899"), _("Audio"): ("speaker", "#22c55e"),
    "OBS Studio": ("record", "#ef4444"), _("Spotify & Media"): ("play", "#16a34a"), "Discord": ("chat", "#6366f1"),
    "Linewave": ("wave", "#06b6d4"), C.CLOCK_CAT: ("clock", "#f97316"), C.SYSMON_CAT: ("gauge", "#14b8a6"),
}
# eigenes Symbol für einzelne Aktionen (sonst das der Kategorie)
ACTION_GLYPH = {
    "system.hotkey": "keyboard", "system.hotkeyswitch": "keyboard", "system.text": "text", "system.open": "folder",
    "system.openapp": "play", "system.close": "close", "system.website": "globe", "system.multimedia": "play",
    "system.timer": "auto", "system.mouse-wheel": "navigate", "profile.openchild": "folder",
    "profile.backtoparent": "up", "page.next": "arrow", "page.previous": "arrow", "page.goto": "navigate",
    "page.indicator": "grid", "profile.rotate": "deck", "system.keybrightness": "sun", "system.sleep": "moon",
    "system.digitaltime": "auto", "soundboard.playaudio": "sfx", "soundboard.stopaudioplay": "close",
    "volume-controller.output-device-control": "speaker", "volume-controller.input-device-control": "mic",
    "obs.scene": "camera", "obs.record": "record", "obs.stream": "stream", "obs.source": "image",
    "obs.audio": "mic", "obs.replay": "undo", "obs.virtualcam": "camera", "obs.studiomode": "grid",
    "obs.transition": "arrow", "obs.screenshot": "image", "media.player": "music", "discord.voice": "chat",
    "mixer.channel": "fx", "mixer.output": "headphones", "wavemic.control": "mic",
    "clock.clock": "clock", "clock.countdown": "hourglass", "clock.stopwatch": "stopwatch", "sysmon.cpu": "cpu",
    "sysmon.memory": "memory", "sysmon.temperature": "thermometer", "sysmon.network": "network",
    "sysmon.battery": "battery", "sysmon.disk": "disk",
}
theme.EXTRA_GLYPHS.update(livekeys.GLYPHS)  # selbst gezeichnete Symbole der Live-Tasten
PLUGINS_CHIP = "plugins"


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


def chip_label(cat):
    return {"OBS Studio": "OBS", _("Spotify & Media"): _("Media")}.get(cat, cat)


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


def _layout(g, width, height, pad=26):
    """Größte Kachelgröße, bei der Tasten und Touchstreifen in den Bereich passen."""
    for ts in range(96, 35, -2):
        gap = max(6, ts // 7)
        grid_w = g.cols * ts + (g.cols - 1) * gap
        seg_w = (grid_w - (g.dials - 1) * 4) / g.dials if g.dials else 0
        dial_h = int(min(seg_w / 2, ts)) if g.dials else 0
        knob = int(ts * 0.45) if g.dials else 0
        total_h = g.rows * ts + (g.rows - 1) * gap + ((gap * 2 + dial_h + gap + knob) if g.dials else 0)
        if grid_w + 2 * pad <= width and total_h + 2 * pad <= height:
            break
    return ts, gap, grid_w, seg_w, dial_h, knob, total_h


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
        for seq in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
            self.canvas.bind_all(seq, self._wheel, add="+")

    def _on_scroll(self, lo, hi):
        if float(lo) <= 0 and float(hi) >= 1:
            if self.bar.winfo_manager():
                self.bar.pack_forget()
        elif not self.bar.winfo_manager():
            self.bar.pack(side="right", fill="y", before=self.canvas)
        self.bar.set(lo, hi)

    def _wheel(self, e):
        w = e.widget
        while w is not None:
            if w is self.canvas:
                break
            w = getattr(w, "master", None)
        else:
            return
        try:
            if not self.bar.winfo_manager():
                return
            step = -1 if (e.num == 4 or getattr(e, "delta", 0) > 0) else 1
            self.canvas.yview_scroll(step * 2, "units")
        except tk.TclError:
            pass

    def clear(self):
        for c in self.inner.winfo_children():
            c.destroy()
        self.canvas.yview_moveto(0)


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


class ActionTile(tk.Canvas):
    """Kachel der Aktionsbibliothek: Symbol, Name, kurze Beschreibung."""

    H = 104

    def __init__(self, master, app, atype):
        super().__init__(master, bg=_bg(master), highlightthickness=0, bd=0, height=self.H, cursor="hand2")
        self.app, self.type = app, atype
        self.selected = self.hover = False
        glyph, color = action_look(atype.uuid, atype.category)
        self.img = theme.icon_tile(app.root, glyph, 30, color, radius=0.27)
        self.fb = theme.font(app.root, 10, "bold")
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

    def _description(self):
        a = self.type
        if a.description:
            return a.description
        if a.controllers == (C.DIAL,):
            return _("dials only")
        return a.category.split(":", 1)[-1].strip() if a.category.startswith("Plugin:") else a.category

    def _draw(self):
        self.delete("all")
        w, h = max(self.winfo_width(), 60), self.H
        out = COL["accent"] if self.selected else (COL["faint"] if self.hover else COL["border"])
        theme.rounded_rect(self, 0, 0, w - 1, h - 1, 12, fill=COL["panel"], outline=out)
        self.create_image(11, 11, image=self.img, anchor="nw")
        if C.DIAL in self.type.controllers and C.KEY not in self.type.controllers:
            self.create_text(w - 10, 12, text="◎", anchor="ne", fill=COL["faint"], font=self.fs)
        fnt = tkfont.Font(self, font=self.fb)
        name = self.type.name
        while fnt.measure(name) > w - 22 and len(name) > 3:
            name = name.rstrip("…")[:-1] + "…"
        self.create_text(11, 56, text=name, anchor="w", fill=COL["fg"], font=self.fb)
        desc = _clip_lines(self, self._description(), self.fs, w - 22, 2)
        self.create_text(11, 68, text=desc, anchor="nw", fill=COL["muted"], font=self.fs, width=w - 20)


def _clip_lines(widget, text, fnt, width, lines):
    """Text auf höchstens `lines` Zeilen der Breite `width` kürzen (mit …)."""
    f = tkfont.Font(widget, font=fnt)
    words, out, cur = text.split(), [], ""
    for wd in words:
        t = (cur + " " + wd).strip()
        if f.measure(t) <= width or not cur:
            cur = t
        else:
            out.append(cur)
            cur = wd
    if cur:
        out.append(cur)
    cut = len(out) > lines
    out = out[:lines]
    for i, line in enumerate(out):
        if f.measure(line) > width or (cut and i == len(out) - 1):
            while line and f.measure(line + "…") > width:
                line = line[:-1]
            out[i] = line.rstrip() + "…"
    return "\n".join(out)


def _autowrap(label):
    """Zeilenumbruch eines Labels an seine Breite anpassen."""
    label.bind("<Configure>", lambda e: label.configure(wraplength=max(100, e.width - 4)), add="+")


def _flow(frame, widgets, width, gap=6, vgap=6):
    """Widgets (z. B. Chips) zeilenweise umbrechend in `frame` setzen."""
    x = y = 0
    rowh = 0
    for w in widgets:
        ww, wh = w.winfo_reqwidth(), w.winfo_reqheight()
        if x and x + ww > width:
            x, y = 0, y + rowh + vgap
            rowh = 0
        w.place(x=x, y=y)
        x += ww + gap
        rowh = max(rowh, wh)
    frame.configure(height=max(1, y + rowh))


class EditorApp:
    def __init__(self, root: tk.Tk, store_root=None, profile=None, daemon=None, on_quit=None):
        self.root = root
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
        self._hit = []
        self._drag = None
        self._save_job = None
        self._profile_ids = []
        self._types = dict(C.BY_UUID)
        self._insp_tab = "appearance"
        self._lib_filter = None
        self._lib_selected = None
        self._tiles_by_uuid = {}

        root.title("LinDeck")
        root.geometry("1280x800")
        root.minsize(1100, 720)
        root.configure(bg=COL["bg"])
        self._style()
        self._build()
        self._bind_keys()
        root.protocol("WM_DELETE_WINDOW", self.close)

        self.refresh_profiles()
        target = self.store.get(profile) if profile else None
        if target is None and self.store.profiles:
            target = sorted(self.store.profiles.values(), key=lambda p: p.name.lower())[0]
        if target:
            self.open_profile(target.id)
        else:
            self.show_empty()
        if self.daemon is not None:
            self.refresh_devices(initial=True)

    # ================================================================ Geräte
    def _controllers(self):
        return list(getattr(self.daemon, "controllers", {}).values()) if self.daemon is not None else []

    def _ctrl(self):
        return next((c for c in self._controllers() if c.serial == self.device), None)

    def _known_devices(self, connected):
        """Bekannte, aber nicht angeschlossene Geräte aus der Konfiguration: [(Seriennummer, Modellname)]."""
        try:
            devs = (getattr(self.daemon, "cfg", None) or {}).get("devices") or {}
        except AttributeError:
            return []
        out = []
        for serial, d in sorted(devs.items()):
            if serial in connected or not isinstance(d, dict):
                continue
            prof = self.store.get(d.get("profile") or "")
            out.append((serial, prof.model_name if prof else serial))
        return out

    def refresh_devices(self, initial=False):
        """Liste der angeschlossenen Stream Decks aktualisieren (alle 2 s)."""
        if self.daemon is None:
            return
        ctrls = sorted(self._controllers(), key=lambda c: (model_name(c.model), c.serial))
        ids = [c.serial for c in ctrls]
        if ids != self._device_ids:
            self._device_ids = ids
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
            ctrls = sorted(self._controllers(), key=lambda c: (model_name(c.model), c.serial))
            known = self._known_devices({c.serial for c in ctrls})
            key = ("app", tuple(c.serial for c in ctrls), tuple(known), self.device)
        else:
            ctrls, known = [], []
            key = ("edit", self.doc.model if self.doc else None)
        if key == self._device_key:
            return
        self._device_key = key
        for c in self.devbox.winfo_children():
            c.destroy()
        self.device_cards = {}
        on_img = theme.icon_tile(self.root, "deck", 34, COL["device"], radius=0.24, fg="#c9cfda")
        off_img = theme.icon_tile(self.root, "deck", 34, theme.mix(COL["device"], COL["muted"], 0.45),
                                  radius=0.24, fg="#c9cfda")
        if self.daemon is None:
            if self.doc:
                card = theme.InfoCard(self.devbox, model_name(self.doc.model) if self.doc.model else _("unknown"),
                                      _("Layout of this profile"), image=on_img, command=self._model_menu_at_card)
                card.pack(fill="x", pady=(0, 8))
                self.device_cards[""] = card
            return
        for c in ctrls:
            card = theme.InfoCard(self.devbox, model_name(c.model), _("Connected"), image=on_img, dot=COL["ok"],
                                  command=lambda s=c.serial: self.select_device(s))
            card.selected = c.serial == self.device
            card.pack(fill="x", pady=(0, 8))
            self.device_cards[c.serial] = card
        for serial, name in known:
            card = theme.InfoCard(self.devbox, name, _("Not connected"), image=off_img, dim=True)
            card.pack(fill="x", pady=(0, 8))
            self.device_cards[serial] = card
        if not ctrls and not known:
            theme.InfoCard(self.devbox, _("No Stream Deck connected"), "", image=off_img, dim=True).pack(
                fill="x", pady=(0, 8))

    def select_device(self, serial):
        """Gerät in der Seitenleiste gewählt: dessen laufendes Profil öffnen."""
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
        self.root.withdraw()
        if not self._hidden_once:
            self._hidden_once = True
            from .desktop import notify
            notify(_("LinDeck keeps running"), _("The Stream Deck stays active. Open it via the “LinDeck” menu."))

    def open_background(self):
        """Hintergrundbild der aktuellen Seite (und des Touchstreifens) einstellen."""
        if not self.doc or not self.nav:
            return
        from .bgui import BackgroundDialog
        BackgroundDialog(self.root, self, self.nav[-1])

    def open_plugins(self):
        """Plugin-Verwaltung öffnen."""
        from .pluginui import PluginManager
        PluginManager(self.root, host=getattr(self.daemon, "plugin_host", None), on_change=self._plugins_changed)

    def _plugins_changed(self):
        self.fill_catalog()
        self._update_plugin_count()

    def _update_plugin_count(self):
        try:
            from . import plugins
            n = len(plugins.installed())
        except Exception:  # noqa: BLE001
            n = ""
        self.plugins_item.set(count=n if n else "")

    def open_wave(self):
        """Das zweite Programm „Linewave“ (Mixer + Mikrofon) öffnen."""
        import shutil
        import subprocess
        import sys
        cmd = [shutil.which("linewave")] if shutil.which("linewave") else [sys.executable, "-m", "streamlin",
                                                                                   "linewave"]
        try:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError as e:
            messagebox.showerror("Linewave", _("Could not start Linewave: {error}").format(error=e), parent=self.root)

    def _restart(self):
        theme.restart_later("streamlin")
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

    def _build(self):
        r = self.root
        self.status_var = tk.StringVar(value="")
        self.model_var = tk.StringVar()
        self._model_codes = list(MODELS)
        self.btn = {}
        self._build_sidebar(r)
        tk.Frame(r, bg=COL["border"], width=1).pack(side="left", fill="y")
        right = tk.Frame(r, bg=COL["bg"])
        right.pack(side="left", fill="both", expand=True)
        self._build_header(right)
        tk.Frame(right, bg=COL["border"], height=1).pack(side="top", fill="x")
        lib = tk.Frame(right, bg=COL["side"], width=300)
        lib.pack(side="right", fill="y")
        lib.pack_propagate(False)
        tk.Frame(right, bg=COL["border"], width=1).pack(side="right", fill="y")
        self._build_library(lib)
        main = tk.Frame(right, bg=COL["bg"], padx=20, pady=16)
        main.pack(side="left", fill="both", expand=True)
        self._build_center(main)

    # ---------------------------------------------------------- Seitenleiste
    def _build_sidebar(self, r):
        sb = self.sidebar = theme.Sidebar(r, width=240)
        sb.bottom.configure(padx=5)  # Platz für den Hell/Dunkel/Auto-Umschalter
        sb.pack(side="left", fill="y")
        sb.brand("LinDeck", "deck")
        sb.section(_("Devices"))
        self.devbox = tk.Frame(sb.top, bg=COL["side"])
        self.devbox.pack(fill="x")
        sb.section(_("Profiles"))
        self.profile_list = ScrollFrame(sb.top, bg=COL["side"])
        self.profile_list.pack(fill="both", expand=True)
        b = tk.Frame(sb.bottom, bg=COL["side"], padx=9)
        b.pack(fill="x")
        self.plugins_item = theme.NavItem(b, _("Plugins"), "puzzle", command=self.open_plugins)
        self.plugins_item.pack(fill="x")
        if parts.has_linewave():
            theme.NavItem(b, _("Open Linewave"), "wave", command=self.open_wave).pack(fill="x", pady=(2, 0))
        self.settings_item = theme.NavItem(b, _("Settings"), "gear", command=self.open_settings)
        self.settings_item.pack(fill="x", pady=(2, 4))
        self._update_plugin_count()

    # ------------------------------------------------------------- Kopfzeile
    def _build_header(self, parent):
        head = tk.Frame(parent, bg=COL["bg"], height=64, padx=22)
        head.pack(side="top", fill="x")
        head.pack_propagate(False)
        txt = tk.Frame(head, bg=COL["bg"])
        txt.pack(side="left", fill="y")
        self.title_lbl = theme.label(txt, "", 15, "bold", anchor="w")
        self.title_lbl.pack(anchor="w", pady=(10, 0))
        self.sub_lbl = theme.label(txt, "", 9, color=COL["muted"], anchor="w")
        self.sub_lbl.pack(anchor="w")
        more = theme.Button(head, icon="more")
        more.command = lambda: self._popup(self._more_menu(), more)
        more.pack(side="right", padx=(8, 0))
        self.btn["exp"] = theme.Button(head, _("Export"), command=self.export_profile)
        self.btn["exp"].pack(side="right", padx=(8, 0))
        self.btn["imp"] = theme.Button(head, _("Import"), command=self.import_profiles)
        self.btn["imp"].pack(side="right", padx=(8, 0))
        self.btn["redo"] = theme.Button(head, icon="redo", command=self.redo)
        self.btn["redo"].pack(side="right", padx=(8, 0))
        self.btn["undo"] = theme.Button(head, icon="undo", command=self.undo)
        self.btn["undo"].pack(side="right", padx=(8, 0))
        for b in head.winfo_children():
            if isinstance(b, theme.Button):
                b.pack_configure(pady=16)

    def _popup(self, menu, widget):
        try:
            menu.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height() + 2)
        finally:
            menu.grab_release()

    def _profile_menu(self, m=None):
        m = m or tk.Menu(self.root, tearoff=0)
        on = "normal" if self.doc else "disabled"
        m.add_command(label=_("New…"), command=self.new_profile)
        m.add_command(label=_("Duplicate"), command=self.duplicate_profile, state=on)
        m.add_command(label=_("Rename…"), command=self.rename_profile, state=on)
        m.add_command(label=_("Delete"), command=self.delete_profile, state=on)
        m.add_separator()
        m.add_command(label=_("Import…"), command=self.import_profiles)
        m.add_command(label=_("Export…"), command=self.export_profile, state=on)
        return m

    def _models_menu(self, master):
        sub = tk.Menu(master, tearoff=0)
        self.model_var.set(self.doc.model if self.doc else "")
        for code in self._model_codes:
            sub.add_radiobutton(label=MODELS[code][0], value=code, variable=self.model_var,
                                command=lambda c=code: self._set_model(c))
        return sub

    def _more_menu(self):
        m = self._profile_menu()
        on = "normal" if self.doc else "disabled"
        m.add_separator()
        m.add_cascade(label=_("Model"), menu=self._models_menu(m), state=on)
        m.add_command(label=_("Background"), command=self.open_background, state=on)
        m.add_command(label=_("Plugins"), command=self.open_plugins)
        if parts.has_linewave():
            m.add_command(label=_("Open Linewave"), command=self.open_wave)
        m.add_command(label=_("Settings"), command=self.open_settings)
        m.add_separator()
        if self.daemon is not None:
            m.add_command(label=_("Close window"), command=self.hide)
        m.add_command(label=_("Quit"), command=self.quit_app)
        return m

    def open_settings(self):
        """Design, Sprache und Updates."""
        from .settingsui import open_settings
        return open_settings(self.root, "LinDeck", self._restart)

    def _model_menu_at_card(self):
        if self.doc:
            card = self.device_cards.get("")
            self._popup(self._models_menu(self.root), card or self.root)

    def _update_header(self):
        st = self.status_var.get()
        if not self.doc:
            self.title_lbl.configure(text="LinDeck")
            self.sub_lbl.configure(text=st)
            return
        self.title_lbl.configure(text=self.doc.name)
        ids = self.doc.page_ids
        parts = [model_name(self.doc.model) if self.doc.model else _("unknown")]
        if self.nav and self.nav[0] in ids:
            parts.append(_("Page {n} of {total}").format(n=ids.index(self.nav[0]) + 1, total=len(ids)))
        parts += [self.doc.folder_title(u) for u in self.nav[1:]]
        sub = "  ›  ".join(parts[1:])
        text = parts[0] + (" · " + sub if sub else "") + (" · " + st if st else "")
        self.sub_lbl.configure(text=text)

    # ------------------------------------------------------------------ Mitte
    def _build_center(self, main):
        self.pagebar = tk.Frame(main, bg=COL["bg"], height=34)
        self.pagebar.pack(side="top", fill="x")
        self.canvas = tk.Canvas(main, bg=COL["bg"], highlightthickness=0, height=300)
        self.canvas.pack(side="top", fill="both", expand=True, pady=(14, 0))
        self.canvas.bind("<Configure>", lambda e: self.redraw())
        self.canvas.bind("<ButtonPress-1>", self._canvas_press)
        self.canvas.bind("<B1-Motion>", self._canvas_motion)
        self.canvas.bind("<ButtonRelease-1>", self._canvas_release)
        self.canvas.bind("<Double-Button-1>", self._canvas_double)
        self.canvas.bind("<Button-3>", self._canvas_menu)
        card = RoundBox(main, height=232, pad=14, padx=18)
        card.pack(side="top", fill="x", pady=(14, 0))
        self.insp_head = tk.Frame(card.body, bg=COL["panel"])
        self.insp_head.pack(side="top", fill="x")
        self.inspector = ScrollFrame(card.body, bg=COL["panel"])
        self.inspector.pack(side="top", fill="both", expand=True, pady=(10, 0))

    # ------------------------------------------------------------ Bibliothek
    def _build_library(self, lib):
        box = tk.Frame(lib, bg=COL["side"], padx=16, pady=16)
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
        self.chipbox = tk.Frame(box, bg=COL["side"], height=30)
        self.chipbox.pack(fill="x", pady=(12, 4))
        self.chipbox.bind("<Configure>", lambda e: self._flow_chips())
        self.tree_hint = tk.StringVar(value=_("Drag onto a key or double-click"))
        tk.Label(box, textvariable=self.tree_hint, bg=COL["side"], fg=COL["muted"], wraplength=264, justify="left",
                 anchor="w", font=theme.font(self.root, 9)).pack(side="bottom", fill="x", pady=(8, 0))
        self.library = ScrollFrame(box, bg=COL["side"])
        self.library.pack(fill="both", expand=True, pady=(4, 0))
        self._chips = []

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
        for p in profs:
            n = len(p.page_ids)
            on = p.id == cur
            count = ngettext("{n} page", "{n} pages", n).format(n=n) if on else n
            it = theme.NavItem(f, p.name, profile_glyph(p.name), count, active=on,
                               command=lambda i=p.id: self._on_profile_selected(i))
            it.pack(fill="x", pady=(0, 2))
            self.profile_items[p.id] = it
        theme.NavItem(f, _("New profile"), "plus", accent=True, command=self.new_profile).pack(fill="x")
        self._update_header()

    def _on_profile_selected(self, pid):
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
        self.refresh_profiles()
        self.root.title(f"LinDeck – {self.doc.name}")
        self.model_var.set(self.doc.model or "")
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

    def new_profile(self):
        dlg = NewProfileDialog(self.root)
        if not dlg.result:
            return
        name, code = dlg.result
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
            self.root.title(f"LinDeck – {self.doc.name}")

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
            filetypes=[(_("Stream Deck profiles"), "*.streamDeckProfile *.streamDeckProfilesBackup *.zip"),
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
                                         filetypes=[(_("Stream Deck profile"), "*.streamDeckProfile")])
        if f:
            out = self.doc.export(f)
            self.status(_("Exported: {path}").format(path=out))

    def _set_model(self, code):
        """Gerätemodell des Profils ändern (Tastenraster, Dials)."""
        if not self.doc or code == self.doc.model:
            return
        old, new = self.doc.geometry, model_geometry(code)
        if new and (new.cols < old.cols or new.rows < old.rows or new.dials < old.dials):
            if not messagebox.askyesno(_("Change device"), _("The new device has fewer keys or dials. "
                                       "Actions outside of it stay saved but are not shown.\n\n"
                                       "Continue?"), parent=self.root):
                self.model_var.set(self.doc.model or "")
                return
        self.doc.set_model(code)
        self.selected = None
        if self.daemon is None:
            self.build_devices()
        self.fill_catalog()
        self.refresh_all()

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
            messagebox.showinfo(_("Settings page"), _("LinDeck must be running for this (menu: “LinDeck” "
                                "or `lindeck`), because it runs the plugins."), parent=self.root)
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
        ids = self.doc.page_ids
        for i, pid in enumerate(ids):
            on = bool(self.nav) and self.nav[0] == pid
            b = theme.PillButton(bar, text=str(i + 1), active=on, height=30, padx=11,
                                 command=lambda p=pid: self.goto_page(p))
            b.pack(side="left", padx=(0, 6))
            b._page = pid
            b.bind("<Button-3>", lambda e, p=pid: self._page_menu(e, p))
        add = theme.PillButton(bar, text="+", height=30, padx=11, command=self.add_page)
        add.pack(side="left")
        has_bg = self.doc.background(self.page_uuid)[0] is not None
        theme.Button(bar, _("Background"), icon="image", kind="primary" if has_bg else "default",
                     command=self.open_background).pack(side="right")

    def _page_menu(self, e, pid):
        """Kontextmenü einer Seitenschaltfläche: verschieben, löschen, hinzufügen."""
        if self.nav and self.nav[0] != pid:
            self.goto_page(pid)
        ids = self.doc.page_ids
        idx = ids.index(pid) if pid in ids else 0
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label=_("Move left"), command=lambda: self.move_page(-1),
                      state="normal" if idx > 0 else "disabled")
        m.add_command(label=_("Move right"), command=lambda: self.move_page(1),
                      state="normal" if idx < len(ids) - 1 else "disabled")
        m.add_separator()
        m.add_command(label=_("Add page"), command=self.add_page)
        m.add_command(label=_("Delete page"), command=self.remove_page,
                      state="normal" if len(ids) > 1 else "disabled")
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

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

    def _render(self, slot, data, size):
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

    def _draw_stage(self):
        """Bühne: Karte mit Rand, Fußzeile mit Hinweis und Helligkeit."""
        cv = self.canvas
        w, h = cv.winfo_width(), cv.winfo_height()
        theme.rounded_rect(cv, 0, 0, w - 1, h - 1, 16, fill=COL["panel"], outline=COL["border"])
        f = theme.font(self.root, 9)
        cv.create_text(18, h - FOOTER_H / 2 - 2, anchor="w", fill=COL["muted"], font=f,
                       text=_("Drag actions onto keys · right-click for more"))
        ctrl = self._ctrl()
        bright = getattr(ctrl, "brightness", None) if ctrl is not None else None
        if isinstance(bright, (int, float)):
            cv.create_text(w - 18, h - FOOTER_H / 2 - 2, anchor="e", fill=COL["muted"], font=f,
                           text=_("Brightness {n} %").format(n=int(bright)))

    def redraw(self):
        cv = self.canvas
        cv.delete("all")
        self._photos = []
        self._hit = []
        self._draw_stage()
        if not self.doc:
            cv.create_text(cv.winfo_width() / 2, (cv.winfo_height() - FOOTER_H) / 2, anchor="center",
                           justify="center", fill=COL["muted"], font=theme.font(self.root, 11),
                           text=_("No profiles yet.\n\n“New…” creates an empty profile,\n"
                                  "“Import…” takes over profiles from the Elgato app."))
            return
        if not self.page_uuid or not self.doc.page(self.page_uuid):
            return
        g, keys, dials = self._slots()
        pad = 26
        aw, ah = cv.winfo_width() - 24, cv.winfo_height() - FOOTER_H - 12
        ts, gap, grid_w, seg_w, dial_h, knob, total_h = _layout(g, aw, ah, pad)
        cw, ch = max(aw, grid_w + 2 * pad), max(ah, total_h + 2 * pad)
        x0 = 12 + (cw - grid_w) // 2
        y0 = 12 + max(pad, (ch - total_h) // 2)
        body = _device_body(grid_w + 2 * pad, total_h + 2 * pad, 28)
        self._body_photo = ImageTk.PhotoImage(body, master=self.root)
        cv.create_image(x0 - pad - SHADOW, y0 - pad - SHADOW, image=self._body_photo, anchor="nw")
        page = self.doc.page(self.page_uuid)
        bg_path, bg_dim = self.doc.background(self.page_uuid)
        self._tiles = render.background_tiles(bg_path, g.cols, g.rows, (ts, ts), bg_dim, gap_ratio=gap / ts) \
            if bg_path else {}
        self._tiles_id = (str(bg_path), bg_dim) if bg_path else None
        for slot in keys:
            c, r = slot[1], slot[2]
            x, y = x0 + c * (ts + gap), y0 + r * (ts + gap)
            self._draw_slot(slot, page.get(slot), x, y, ts, ts)
        if g.dials:
            y = y0 + g.rows * ts + (g.rows - 1) * gap + gap * 2
            strip_w = int(g.dials * (seg_w + 4) - 4)
            strip = Image.new("RGBA", (strip_w, dial_h), (0, 0, 0, 255))
            sp = self.doc.strip_background(self.page_uuid)
            if sp:
                fr = render.load_frames(sp, strip_w, dial_h)
                if fr:
                    strip.alpha_composite(fr[0][0].convert("RGBA").resize((strip_w, dial_h)))
            self._strip_photo = ImageTk.PhotoImage(_rounded(strip, max(6, dial_h // 8)), master=self.root)
            cv.create_image(x0, y, image=self._strip_photo, anchor="nw")
            for slot in dials:
                i = slot[1]
                x = x0 + i * (seg_w + 4)
                self._draw_slot(slot, page.get(slot), int(x), y, int(seg_w), dial_h)
                kx, ky = x + seg_w / 2, y + dial_h + gap + knob / 2
                cv.create_oval(kx - knob / 2, ky - knob / 2, kx + knob / 2, ky + knob / 2, fill="#2b2c31",
                               outline="#45474e", width=2)
                cv.create_line(kx, ky - knob / 2 + 4, kx, ky - knob / 4, fill="#8a8c94", width=2)

    def _draw_slot(self, slot, data, x, y, w, h):
        cv = self.canvas
        sel = self.selected is not None and tuple(self.selected) == tuple(slot)
        tile = self._tiles.get((slot[1], slot[2])) if slot[0] == "key" and getattr(self, "_tiles", None) else None
        if data is None and tile is not None:
            ph = ImageTk.PhotoImage(_rounded(tile, max(6, w // 8)), master=self.root)
            self._photos.append(ph)
            cv.create_image(x, y, image=ph, anchor="nw")
        elif data is None and slot[0] == "dial":
            pass  # leeres Feld auf dem Touchstreifen: der Streifen ist schon gezeichnet
        elif data is None:
            _round_rect(cv, x, y, x + w, y + h, max(6, w // 8), fill=COL["slot"], outline="")
            cv.create_text(x + w / 2, y + h / 2, text="+", fill="#4a4c53", font=(self._fam, max(10, w // 5)))
        else:
            img = self._render(slot, data, (w, h))
            ph = ImageTk.PhotoImage(img, master=self.root)
            self._photos.append(ph)
            cv.create_image(x, y, image=ph, anchor="nw")
            if not A.is_supported(data.get("UUID", "")):
                cv.create_oval(x + w - 14, y + 4, x + w - 4, y + 14, fill="#f5a623", outline="")
        if sel:
            _round_rect(cv, x - 3, y - 3, x + w + 3, y + h + 3, max(8, w // 7), fill="", outline=COL["accent"],
                        width=3)
        self._hit.append((x, y, x + w, y + h, slot))

    def slot_at(self, x, y):
        for x0, y0, x1, y1, slot in self._hit:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return slot
        return None

    def select(self, slot):
        self.selected = slot
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
            # auf eine Seitenschaltfläche gezogen?
            w = self.root.winfo_containing(e.x_root, e.y_root)
            pid = getattr(w, "_page", None)
            if pid and pid != self.nav[0]:
                self._move_to_page(d["slot"], pid)
            return
        if not self._fits(self.doc.get(self.page_uuid, d["slot"]), target):
            return
        self.doc.move(self.page_uuid, d["slot"], self.page_uuid, target)
        self.select(target)

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
        m = tk.Menu(self.root, tearoff=0)
        if data and data.get("UUID") == C.FOLDER_UUID:
            m.add_command(label=_("Open folder"), command=lambda: self.open_folder(
                (data.get("Settings") or {}).get("ProfileUUID")))
            m.add_separator()
        m.add_command(label=_("Cut"), accelerator=_("Ctrl+X"), command=self.cut_selected,
                      state="normal" if data else "disabled")
        m.add_command(label=_("Copy"), accelerator=_("Ctrl+C"), command=self.copy_selected,
                      state="normal" if data else "disabled")
        m.add_command(label=_("Paste"), accelerator=_("Ctrl+V"), command=self.paste_selected,
                      state="normal" if self.clipboard else "disabled")
        m.add_separator()
        if len(self.doc.page_ids) > 1 and data:
            sub = tk.Menu(m, tearoff=0)
            for i, pid in enumerate(self.doc.page_ids):
                if pid != self.nav[0] or len(self.nav) > 1:
                    sub.add_command(label=_("Page {n}").format(n=i + 1), command=lambda p=pid: self._move_to_page(slot, p))
            m.add_cascade(label=_("Move to"), menu=sub)
        m.add_command(label=_("Delete"), accelerator=_("Del"), command=self.delete_selected,
                      state="normal" if data else "disabled")
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

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

    @staticmethod
    def _chip_key(cat):
        return PLUGINS_CHIP if cat.startswith("Plugin:") else cat

    def fill_catalog(self):
        """Aktionsbibliothek: Filter-Chips und Kacheln nach Kategorien."""
        q = self.search_var.get().strip().lower()
        avail = self._catalog()
        cats = list(dict.fromkeys(a.category for a in avail))
        keys = list(dict.fromkeys(self._chip_key(c) for c in cats))
        if self._lib_filter not in keys:
            self._lib_filter = None
        for c in self._chips:
            c.destroy()
        self._chips = []
        for key in [None] + keys:
            text = _("All") if key is None else _("Plugins") if key == PLUGINS_CHIP else chip_label(key)
            chip = theme.Chip(self.chipbox, text, active=key == self._lib_filter,
                              command=lambda k=key: self._set_lib_filter(k))
            self._chips.append(chip)
        self._flow_chips()
        self.library.clear()
        f = self.library.inner
        self._tiles_by_uuid = {}
        first = True
        for cat in cats:
            if self._lib_filter is not None and self._chip_key(cat) != self._lib_filter:
                continue
            items = [a for a in avail if a.category == cat
                     and (not q or q in a.name.lower() or q in a.description.lower() or q in cat.lower())]
            if not items:
                continue
            theme.section(f, cat, bg=COL["side"]).pack(fill="x", pady=(0 if first else 14, 8))
            first = False
            grid = tk.Frame(f, bg=COL["side"])
            grid.pack(fill="x")
            grid.columnconfigure(0, weight=1, uniform="t")
            grid.columnconfigure(1, weight=1, uniform="t")
            for i, a in enumerate(items):
                t = ActionTile(grid, self, a)
                t.grid(row=i // 2, column=i % 2, sticky="ew", padx=(0, 4) if i % 2 == 0 else (4, 0), pady=(0, 8))
                if a.uuid == self._lib_selected:
                    t.selected = True
                self._tiles_by_uuid[a.uuid] = t
        if first:
            tk.Label(f, text=_("No matching actions"), bg=COL["side"], fg=COL["muted"],
                     font=theme.font(self.root, 9)).pack(anchor="w")

    def _flow_chips(self):
        if self._chips:
            _flow(self.chipbox, self._chips, max(self.chipbox.winfo_width(), 200))

    def _set_lib_filter(self, key):
        self._lib_filter = key
        self.fill_catalog()

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
        self.select(slot)
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
            wide = fld.kind in ("multiline", "hotkey", "file", "profile") or (fld.kind == "text" and fld.hint)
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
        size.trace_add("write", lambda *a: self._set_int_state("FontSize", size.get()))
        lab(_("Icon"), 2, 0)
        irow = tk.Frame(f, bg=COL["panel"])
        irow.grid(row=2, column=1, sticky="w", pady=4)
        prev = E.preview_action(self.doc, self.page_uuid, self.data, (36, 36), state=self.state_idx) \
            if self.slot[0] == "key" else E.preview_dial(self.doc, self.page_uuid, self.data, (72, 36))
        self._icon_photo = ImageTk.PhotoImage(_rounded(prev.convert("RGBA"), 7), master=self.app.root)
        tk.Label(irow, image=self._icon_photo, bg=COL["panel"]).pack(side="left")
        theme.Button(irow, _("Choose image…"), command=self._choose_icon).pack(side="left", padx=(8, 0))
        b = theme.Button(irow, _("Default"), kind="ghost", command=lambda: self._set_state("Image", "", rebuild=True))
        b.pack(side="left", padx=(4, 0))
        if not st.get("Image"):
            b.configure_text(enabled=False)
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
            var.trace_add("write", lambda *a: self._set(k, var.get()))
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

            def on_int(*a, var=var):
                try:
                    v = int(var.get())
                except ValueError:
                    return
                if fld.min <= v <= fld.max:
                    self._set(k, v)
            var.trace_add("write", on_int)
        elif fld.kind == "bool":
            var = tk.BooleanVar(value=bool(s.get(k)) if k in s else True if k == "bring_to_front" else False)
            ttk.Checkbutton(cell, variable=var, command=lambda: self._set(k, var.get())).pack(side="left")
        elif fld.kind == "choice":
            vals = fld.options
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


class NewProfileDialog(_Dialog):
    def __init__(self, app_root):
        tk.Toplevel.__init__(self, app_root)
        self.title(_("New profile"))
        self.configure(bg=COL["panel"])
        self.transient(app_root)
        self.result = None
        f = ttk.Frame(self, style="Panel.TFrame", padding=16)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=_("Name"), style="Panel.TLabel").grid(row=0, column=0, sticky="w", pady=4)
        self.name = tk.StringVar(value=_("New profile"))
        e = ttk.Entry(f, textvariable=self.name, width=34)
        e.grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(f, text=_("Device"), style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=4, padx=(0, 10))
        self.codes = list(MODELS)
        self.box = ttk.Combobox(f, state="readonly", width=32, values=[MODELS[c][0] for c in self.codes])
        self.box.current(self.codes.index("20GBA9901"))
        self.box.grid(row=1, column=1, sticky="ew", pady=4)
        row = ttk.Frame(f, style="Panel.TFrame")
        row.grid(row=2, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(row, text=_("Cancel"), command=self.destroy).pack(side="right")
        ttk.Button(row, text=_("Create"), style="Accent.TButton", command=self.ok).pack(side="right", padx=6)
        self.bind("<Return>", lambda ev: self.ok())
        self.bind("<Escape>", lambda ev: self.destroy())
        e.focus_set()
        e.select_range(0, "end")
        self.wait_visibility()
        self.grab_set()
        self.wait_window()

    def ok(self):
        self.result = (self.name.get().strip() or _("New profile"), self.codes[self.box.current()])
        self.destroy()


# --------------------------------------------------------------------------
# Zeichenhilfen
# --------------------------------------------------------------------------
SHADOW = 36
_BODY_CACHE = {}


def _device_body(w, h, r):
    """Gehäuse des Stream Decks: dunkler Verlauf, heller Rand oben, weicher Schatten."""
    key = (w, h, r)
    if key in _BODY_CACHE:
        return _BODY_CACHE[key]
    W, H = w + 2 * SHADOW, h + 2 * SHADOW
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sh = Image.new("L", (W, H), 0)
    ImageDraw.Draw(sh).rounded_rectangle((SHADOW, SHADOW + 8, SHADOW + w, SHADOW + h + 8), r, fill=170)
    sh = sh.filter(ImageFilter.GaussianBlur(10))
    out.paste(theme.rgb(COL["shadow"]) + (255,), (0, 0), sh)
    grad = Image.new("RGBA", (w, h))
    top, bot = theme.rgb("#26282d"), theme.rgb("#0c0d0f")
    gd = ImageDraw.Draw(grad)
    for y in range(h):
        t = y / max(1, h - 1)
        gd.line([(0, y), (w, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bot)) + (255,))
    k = 3
    mask = Image.new("L", (w * k, h * k), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w * k - 1, h * k - 1), r * k, fill=255)
    mask = mask.resize((w, h), Image.LANCZOS)
    out.paste(grad, (SHADOW, SHADOW), mask)
    rim = Image.new("RGBA", (w * k, h * k), (0, 0, 0, 0))
    ImageDraw.Draw(rim).rounded_rectangle((0, 0, w * k - 1, h * k - 1), r * k, outline=(70, 73, 80, 255),
                                          width=2 * k)
    out.alpha_composite(rim.resize((w, h), Image.LANCZOS), (SHADOW, SHADOW))
    if len(_BODY_CACHE) > 20:
        _BODY_CACHE.clear()
    _BODY_CACHE[key] = out
    return out


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
        root = tk.Tk(className="lindeck-editor")
    except tk.TclError as e:
        print(_("No graphical display available: {error}").format(error=e))
        return 1
    EditorApp(root, store_root=store_root, profile=profile)
    root.mainloop()
    return 0
