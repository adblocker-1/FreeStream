"""Fenster des virtuellen Decks (virtualdeck.py) – im Prozess der FreeDeck-App.

Zwei Darstellungen, Einstellungen › Geräte › Virtuelles Deck:
  window  schwebendes Fenster: Titelleiste zum Verschieben, Tasten, unten Profil und Seiten
  edge    Leiste am rechten oder linken Bildschirmrand (hochkant – im Editor genauso), zum Rand hin
          gerade, nach innen rund. Ein Klick auf den Griff am inneren Rand lässt sie hinausgleiten
          (nur der Griff bleibt stehen), ein zweiter holt sie zurück

Das Fenster nimmt keinen Fokus (overrideredirect): Tastenkürzel einer Taste landen so im
Programm, in dem du gerade arbeitest – wie bei einem echten Deck.
"""
import ctypes
import ctypes.util
import logging
import threading
import tkinter as tk

from PIL import Image, ImageDraw, ImageEnhance, ImageTk

from . import virtualdeck
from .i18n import _
from .theme import COL, Icons, PopupMenu, elide, font, rounded_rect

log = logging.getLogger("freestream.virtualui")
TICK_MS = 40          # neue Tastenbilder abholen
IDLE_MS = 300         # ausgeblendet: seltener nachsehen
GRIP = 20             # Randleiste: innerer Rand mit dem Griff darin – bleibt eingeklappt sichtbar
SLIDE_MS, SLIDE_STEPS = 220, 13  # Hinaus-/Hereingleiten: Dauer und Bilder (≈ 60 pro Sekunde)
GRIP_REACH = 36       # Griff: klickbar 36 px über und unter der Mitte (nicht die ganze Kante)
RADIUS = 16           # runde Ecken der Randleiste (nach innen)
# Gehäuse wie ein echtes Deck – dunkel, auch im hellen Design
CHROME = {"bg": "#1c1c1c", "edge": "#333333", "fg": "#f2f2f2", "muted": "#a8a8a8", "slot": "#0d0d0d",
          "dot": "#4a4a4a", "pill": "#2a2a2a"}


def _ease_out(t):
    """Schnell los, weich ankommen."""
    return 1 - (1 - t) ** 3


_X = {}  # libX11/libXext und eine eigene Verbindung zum X-Server (für die Fensterform)


def _toplevel_xid(wid):
    """Tk legt um jedes Toplevel ein Hüllfenster – geformt werden muss das oberste (Kind des Wurzelfensters)."""
    x11, dpy = _X["x11"], _X["dpy"]
    x11.XQueryTree.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
                               ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_void_p),
                               ctypes.POINTER(ctypes.c_uint)]
    x11.XFree.argtypes = [ctypes.c_void_p]
    for _i in range(4):
        root, parent, kids, n = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_void_p(), ctypes.c_uint()
        if not x11.XQueryTree(dpy, wid, ctypes.byref(root), ctypes.byref(parent), ctypes.byref(kids),
                              ctypes.byref(n)):
            break
        if kids:
            x11.XFree(kids)
        if not parent.value or parent.value == root.value:
            break
        wid = parent.value
    return wid


def _round_shape(win, w, h, r, side):
    """Fensterform mit runden Ecken auf einer Seite (X11-Shape): Fenster ohne Rahmen sind sonst
    rechteckig. side: "left" oder "right" = die runde Seite. Ohne X11/Xext bleibt es eckig."""
    try:
        if "dpy" not in _X:
            x11 = ctypes.cdll.LoadLibrary(ctypes.util.find_library("X11") or "libX11.so.6")
            xext = ctypes.cdll.LoadLibrary(ctypes.util.find_library("Xext") or "libXext.so.6")
            x11.XOpenDisplay.restype = ctypes.c_void_p
            x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
            x11.XFlush.argtypes = [ctypes.c_void_p]
            _X.update(x11=x11, xext=xext, dpy=x11.XOpenDisplay(None))
        if not _X["dpy"]:
            return False

        class Rect(ctypes.Structure):
            _fields_ = [("x", ctypes.c_short), ("y", ctypes.c_short), ("width", ctypes.c_ushort),
                        ("height", ctypes.c_ushort)]
        rows = []
        for y in range(h):
            dy = max(r - y - 0.5, y + 0.5 - (h - r), 0)
            inset = round(r - (r * r - dy * dy) ** 0.5) if dy else 0
            rows.append(inset)
        rects, y = [], 0
        while y < h:  # gleiche Zeilen zu einem Rechteck zusammenfassen
            y1 = y
            while y1 + 1 < h and rows[y1 + 1] == rows[y]:
                y1 += 1
            x0 = rows[y] if side == "left" else 0
            rects.append(Rect(x0, y, w - rows[y], y1 - y + 1))
            y = y1 + 1
        arr = (Rect * len(rects))(*rects)
        wid = _toplevel_xid(int(win.wm_frame(), 16))
        xext = _X["xext"]
        xext.XShapeCombineRectangles.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                                                 ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                                 ctypes.c_int]
        xext.XShapeCombineRectangles(_X["dpy"], wid, 0, 0, 0, arr, len(rects), 0, 0)  # Bounding, Set
        _X["x11"].XFlush(_X["dpy"])
        return True
    except Exception as e:  # noqa: BLE001 - nur Aussehen
        log.debug("Fensterform: %s", e)
        return False


def _rounded(img, size, radius, dim=False):
    img = img.convert("RGB").resize((size, size), Image.LANCZOS)
    if dim:
        img = ImageEnhance.Brightness(img).enhance(0.7)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


class VirtualDeckWindow:
    """Zeigt das virtuelle Deck des laufenden Dienstes, solange es eingeschaltet ist."""

    def __init__(self, root, daemon, editor=None):
        self.root, self.daemon, self.editor = root, daemon, editor
        self.icons = Icons(root)
        self.win = self.cv = None
        self.shape = None          # (Gerät, Darstellung, Rand) des gebauten Fensters
        self.collapsed = False
        self._seq = -1
        self._footer = None
        self._photos = {}          # Taste -> (Bild vom Deck, gedrückt, PhotoImage)
        self._down = None          # gedrückte Taste
        self._drag = None          # Fenster verschieben: (x, y) des Mausklicks im Fenster
        self._hits = []            # (x0, y0, x1, y1, Ziel)
        self.grip_hover = False
        self._slide = None         # laufende Animation: [Start-x, Ziel-x, Schritt, after-Job]
        self._ticks = 0
        self._want_toggle = threading.Event()
        self._job = root.after(TICK_MS, self._tick)

    # ------------------------------------------------------------ Zustand
    @property
    def cfg(self):
        return getattr(self.daemon, "cfg", None)

    def controller(self):
        return getattr(self.daemon, "virtual_controller", None) if self.daemon is not None else None

    def request_toggle(self):
        """Aus einem Signal (Tastenkürzel): ein-/ausblenden im Tk-Thread."""
        self._want_toggle.set()

    def toggle(self):
        if self.cfg is None:
            return
        s = virtualdeck.settings(self.cfg)
        if not s["enabled"]:
            return
        virtualdeck.update(self.cfg, visible=not s["visible"])
        self._save()
        self._tick(reschedule=False)

    def _save(self):
        try:
            self.cfg.save()
        except OSError:
            pass

    def _tick(self, reschedule=True):
        try:
            if self._want_toggle.is_set():
                self._want_toggle.clear()
                self.toggle()
            self.update()
            if self.grip_hover:
                self.check_hover()
        except Exception as e:  # noqa: BLE001 - der Takt muss weiterlaufen
            log.debug("Virtuelles Deck: %s", e)
        if reschedule:
            self._job = self.root.after(TICK_MS if self.win is not None and self.win.winfo_ismapped() else IDLE_MS,
                                        self._tick)

    def update(self):
        s = virtualdeck.settings(self.cfg) if self.cfg is not None else dict(virtualdeck.DEFAULTS)
        ctrl = self.controller()
        if not s["enabled"] or ctrl is None:
            self.destroy_window()
            return
        multi = ctrl.page_count > 1 if s["mode"] == "edge" else None  # Randleiste: Seitenpunkte nur bei Bedarf
        shape = (id(ctrl), s["mode"], s["edge"], s["topmost"], multi)
        if self.win is None or shape != self.shape:
            self.destroy_window()
            self._build(ctrl, s)
            self.shape = shape
        if not s["visible"]:
            if self.win.winfo_ismapped():
                self.win.withdraw()
            return
        if not self.win.winfo_ismapped():
            self.win.deiconify()
            self._place(s)
            self._seq = -1
        self._ticks += 1
        if (s["topmost"] or s["mode"] == "edge") and self._ticks % 25 == 0:
            self.win.lift()  # Fenster ohne Rahmen: der Fenstermanager hält sie nicht oben
        footer = self._footer_state(ctrl)
        seq = ctrl.deck.seq
        if seq != self._seq or footer != self._footer:
            self._seq, self._footer = seq, footer
            self.draw()

    @staticmethod
    def _footer_state(ctrl):
        p = ctrl.profile
        return (p.id if p else None, p.name if p else "", ctrl.page_index, ctrl.page_count, len(ctrl.folder_stack))

    def destroy_window(self):
        self._cancel_slide()
        if self.win is not None:
            try:
                self.win.destroy()
            except tk.TclError:
                pass
        self.win = self.cv = self.shape = None
        self._photos = {}
        self._down = None
        self._seq = -1

    def close(self):
        try:
            self.root.after_cancel(self._job)
        except tk.TclError:
            pass
        self.destroy_window()

    # ------------------------------------------------------------ Aufbau
    def _build(self, ctrl, s):
        self.ctrl, self.mode, self.edge = ctrl, s["mode"], s["edge"]
        g = ctrl.geometry  # als Randleiste schon hochkant (virtualdeck.device_model)
        self.cols, self.rows = g.cols, g.rows
        if self.mode == "edge":
            self.gap, self.pad, self.title_h = 10, 12, 0
            self.foot_h = 22 if ctrl.page_count > 1 else 0
            avail = self.root.winfo_screenheight() - 120 - self.foot_h
            self.tile = max(40, min(64, (avail - self.gap * (self.rows - 1)) // self.rows))
            grid_w = self.cols * self.tile + (self.cols - 1) * self.gap
            # innen 20 px (der Griff sitzt im Rand), sonst überall 12 px
            self.width = GRIP + grid_w + self.pad
            self.grid_x = GRIP if self.edge == "right" else self.pad
            self.height = self.pad + self.rows * self.tile + (self.rows - 1) * self.gap + self.pad + self.foot_h
        else:
            self.tile, self.gap, self.pad, self.title_h, self.foot_h = 72, 10, 12, 34, 44
            self.width = max(300, 2 * self.pad + self.cols * self.tile + (self.cols - 1) * self.gap)
            self.grid_x = None
            self.height = self.title_h + self.pad + self.rows * self.tile + (self.rows - 1) * self.gap + self.foot_h
        self.win = tk.Toplevel(self.root, bg=CHROME["bg"])
        self.win.withdraw()
        self.win.title(_("Virtual deck"))
        self.win.overrideredirect(True)
        try:
            self.win.attributes("-topmost", bool(s["topmost"] or self.mode == "edge"))
        except tk.TclError:
            pass
        edge = self.mode == "edge"
        self.cv = tk.Canvas(self.win, width=self.width, height=self.height, bg=CHROME["bg"],
                            highlightthickness=0 if edge else 1, highlightbackground=CHROME["edge"], bd=0)
        self.cv.pack()
        self.f_title = font(self.root, 10, "bold")
        self.f_small = font(self.root, 9)
        self.cv.bind("<ButtonPress-1>", self._press)
        self.cv.bind("<ButtonRelease-1>", self._release)
        self.cv.bind("<B1-Motion>", self._motion)
        if edge:
            self.cv.bind("<Motion>", self._hover)
            self.cv.bind("<Leave>", lambda e: self._set_grip_hover(False))
        self._shaped = False

    def _place(self, s):
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        if self.mode == "edge":
            self._cancel_slide()
            self.win.geometry(f"{self.width}x{self.height}+{self._edge_x(self.collapsed)}"
                              f"+{max(0, (sh - self.height) // 2)}")
            if not self._shaped:
                self.win.update_idletasks()
                self._shaped = _round_shape(self.win, self.width, self.height, RADIUS,
                                            "left" if self.edge == "right" else "right")
            return
        pos = s.get("pos")
        if isinstance(pos, (list, tuple)) and len(pos) == 2:
            x = max(0, min(int(pos[0]), sw - 80))
            y = max(0, min(int(pos[1]), sh - 60))
        else:
            x, y = sw - self.width - 48, sh - self.height - 96
        self.win.geometry(f"+{max(0, x)}+{max(0, y)}")

    # ------------------------------------------------------------ Zeichnen
    def _key_xy(self, k):
        r, c = divmod(k, self.ctrl.geometry.cols)
        x0 = self.grid_x if self.grid_x is not None else \
            (self.width - (self.cols * self.tile + (self.cols - 1) * self.gap)) // 2
        return x0 + c * (self.tile + self.gap), self.title_h + self.pad + r * (self.tile + self.gap)

    def _photo(self, k, img):
        pressed = self._down == k
        cached = self._photos.get(k)
        if cached and cached[0] is img and cached[1] == pressed:
            return cached[2]
        size = self.tile - (4 if pressed else 0)
        ph = ImageTk.PhotoImage(_rounded(img, size, max(6, size // 7), dim=pressed), master=self.cv)
        self._photos[k] = (img, pressed, ph)
        return ph

    def draw(self):
        cv = self.cv
        if cv is None:
            return
        cv.delete("all")
        self._hits = []
        ctrl = self.ctrl
        if self.mode == "edge":  # Gehäuse: zum Bildschirmrand gerade (Rand außerhalb), innen rund
            w, h = self.width, self.height
            box = (0, 0, w + RADIUS * 2, h - 1) if self.edge == "right" else (-RADIUS * 2, 0, w - 1, h - 1)
            rounded_rect(cv, *box, RADIUS, fill=CHROME["bg"], outline=CHROME["edge"])
            self._draw_grip()
        if self.title_h:
            self._draw_title()
        __, images, __ = ctrl.deck.snapshot()
        for k in range(ctrl.key_count):
            x, y = self._key_xy(k)
            img = images.get(k)
            if img is None:
                rounded_rect(cv, x, y, x + self.tile, y + self.tile, max(6, self.tile // 7), fill=CHROME["slot"],
                             outline="")
            else:
                off = 2 if self._down == k else 0
                cv.create_image(x + off, y + off, image=self._photo(k, img), anchor="nw")
            self._hits.append((x, y, x + self.tile, y + self.tile, ("key", k)))
        self._draw_footer()

    def _draw_title(self):
        cv, h = self.cv, self.title_h
        from .freedecksettings import device_title
        cv.create_image(self.pad + 10, h / 2 + 2, image=self.icons.get("deck", 18, COL["accent"]))
        title = device_title(self.cfg, self.ctrl) if self.cfg is not None else _("Virtual deck")
        cv.create_text(self.pad + 26, h / 2 + 2, anchor="w", text=elide(cv, title, self.f_title, self.width - 120),
                       font=self.f_title, fill=CHROME["fg"])
        x = self.width - self.pad - 10
        cv.create_image(x, h / 2 + 2, image=self.icons.get("close", 14, CHROME["muted"]))
        self._hits.append((x - 12, 0, x + 12, h, ("hide",)))
        x -= 28
        cv.create_image(x, h / 2 + 2, image=self.icons.get("grid", 15, CHROME["muted"]))
        self._hits.append((x - 12, 0, x + 12, h, ("edit",)))
        self._hits.append((0, 0, x - 14, h, ("drag",)))

    def _draw_grip(self):
        """Griff im inneren Rand: dezenter Strich; unter der Maus hell auf einer Fläche (ohne Pfeil)."""
        cv, h = self.cv, self.height
        cv.delete("grip")
        gx = GRIP // 2 if self.edge == "right" else self.width - GRIP // 2
        cy = h // 2
        if self.grip_hover:
            rounded_rect(cv, gx - 5, cy - 30, gx + 5, cy + 30, 5, fill="#2e2e2e", outline="", tags="grip")
        col = CHROME["fg"] if self.grip_hover else "#555555"
        rounded_rect(cv, gx - 2, cy - 20, gx + 2, cy + 20, 2, fill=col, outline="", tags="grip")
        x0, x1 = (0, GRIP) if self.edge == "right" else (self.width - GRIP, self.width)
        self._grip_box = (x0, cy - GRIP_REACH, x1, cy + GRIP_REACH)

    def _draw_footer(self):
        cv, ctrl = self.cv, self.ctrl
        # Randleiste: Punkte zwischen Tasten und unterem Rand (gleicher Abstand wie überall)
        y = self.height - self.foot_h / 2 - (self.pad - 4 if self.mode == "edge" else 0)
        n, cur = ctrl.page_count, ctrl.page_index
        if self.mode == "window":
            # Profil: Klick öffnet die Auswahl
            name = ctrl.profile.name if ctrl.profile else _("No profile")
            w = min(170, self.width - 150)
            x0 = self.pad
            rounded_rect(cv, x0, y - 13, x0 + w, y + 13, 8, fill=CHROME["pill"], outline=CHROME["edge"])
            cv.create_text(x0 + 10, y, anchor="w", text=elide(cv, name, self.f_title, w - 30), font=self.f_title,
                           fill=CHROME["fg"])
            cv.create_text(x0 + w - 12, y, text="⌄", font=self.f_small, fill=CHROME["muted"])
            self._hits.append((x0, y - 13, x0 + w, y + 13, ("profiles",)))
        if n > 1:
            dots = min(n, 8)
            span = (dots - 1) * 14
            if self.mode == "window":
                right = self.width - self.pad - 14
                cx0 = right - 20 - span
                cv.create_text(right, y, text="›", font=self.f_title, fill=CHROME["fg"])
                self._hits.append((right - 10, y - 14, right + 12, y + 14, ("page", 1)))
                cv.create_text(cx0 - 20, y, text="‹", font=self.f_title, fill=CHROME["fg"])
                self._hits.append((cx0 - 32, y - 14, cx0 - 10, y + 14, ("page", -1)))
            else:
                gw = self.cols * self.tile + (self.cols - 1) * self.gap
                cx0 = self.grid_x + (gw - span) // 2
            for i in range(dots):
                cx = cx0 + i * 14
                on = i == min(cur, dots - 1)
                cv.create_oval(cx - 4, y - 4, cx + 4, y + 4, fill=COL["accent"] if on else CHROME["dot"], outline="")
                self._hits.append((cx - 7, y - 10, cx + 7, y + 10, ("goto", i)))

    # ------------------------------------------------------------ Maus
    def _hit(self, x, y):
        for x0, y0, x1, y1, what in self._hits:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return what
        return None

    def _in_grip(self, x, y):
        b = getattr(self, "_grip_box", None)
        return self.mode == "edge" and b is not None and b[0] <= x <= b[2] and b[1] <= y <= b[3]

    def _press(self, e):
        if self._in_grip(e.x, e.y):
            self.set_collapsed(not self.collapsed)
            return
        if self.mode == "edge" and self.collapsed:
            return
        what = self._hit(e.x, e.y)
        if not what:
            return
        if what[0] == "key":
            self._down = what[1]
            self.ctrl.deck.press(what[1], True)
            self.draw()
        elif what[0] == "drag":
            self._drag = (e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y())
        elif what[0] == "hide":
            self.toggle()
        elif what[0] == "edit":
            self.open_in_freedeck()
        elif what[0] == "profiles":
            self._profiles_menu(e)
        elif what[0] == "page":
            self._bg(self.ctrl.next_page if what[1] > 0 else self.ctrl.previous_page)
        elif what[0] == "goto":
            self._bg(self.ctrl.goto_page, what[1])

    def _release(self, e):
        if self._down is not None:
            k, self._down = self._down, None
            self.ctrl.deck.press(k, False)
            self.draw()
        if self._drag is not None:
            self._drag = None
            virtualdeck.update(self.cfg, pos=[self.win.winfo_x(), self.win.winfo_y()])
            self._save()

    def _motion(self, e):
        if self._drag is not None:
            dx, dy = self._drag
            self.win.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

    @staticmethod
    def _bg(fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    def _profiles_menu(self, e):
        ctrl = self.ctrl
        m = PopupMenu(self.root)
        for p in ctrl.candidate_profiles():
            m.add(p.name, lambda pid=p.id: self._bg(ctrl.load_profile, pid),
                  icon="check" if ctrl.profile and p.id == ctrl.profile.id else None)
        m.popup(e.x_root, e.y_root)

    def open_in_freedeck(self):
        """FreeDeck-Fenster öffnen, das virtuelle Deck gewählt – dort werden die Tasten belegt."""
        ed = self.editor
        if ed is None:
            return
        ed.show()
        try:
            ed.refresh_devices()
            ed.select_device(self.ctrl.serial)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ Randleiste
    def _hover(self, e):
        self._set_grip_hover(self._in_grip(e.x, e.y))

    def check_hover(self):
        """Hervorhebung des Griffs nach der Mausposition richten. Gleitet die Leiste unter der stehenden
        Maus weg, meldet X kein <Leave> – der Griff bliebe sonst hell."""
        if self.cv is None or not self.win.winfo_ismapped():
            self._set_grip_hover(False)
            return
        px, py = self.win.winfo_pointerxy()
        x, y = px - self.cv.winfo_rootx(), py - self.cv.winfo_rooty()
        inside = 0 <= x < self.width and 0 <= y < self.height
        self._set_grip_hover(inside and self._in_grip(x, y))

    def _set_grip_hover(self, on):
        if on != self.grip_hover and self.cv is not None:
            self.grip_hover = on
            self.cv.configure(cursor="hand2" if on else "")
            self._draw_grip()

    def _edge_x(self, collapsed):
        """x-Position der Randleiste: ganz sichtbar oder bis auf den Griff hinausgeschoben."""
        sw = self.root.winfo_screenwidth()
        if self.edge == "right":
            return sw - GRIP if collapsed else sw - self.width
        return -(self.width - GRIP) if collapsed else 0

    def set_collapsed(self, on, animate=True):
        """Randleiste hinaus- oder hereingleiten lassen (Klick auf den Griff)."""
        if self.win is None or self.mode != "edge":
            return
        self.collapsed = bool(on)
        self._draw_grip()
        target = self._edge_x(self.collapsed)
        start = self.win.winfo_x()
        self._cancel_slide()
        if not animate or start == target or not self.win.winfo_ismapped():
            self.win.geometry(f"+{target}+{self.win.winfo_y()}")
            return
        self._slide = [start, target, 0, None]
        self._slide_step()

    def _slide_step(self):
        sl = self._slide
        if sl is None or self.win is None:
            return
        start, target, i, _job = sl
        i += 1
        x = round(start + (target - start) * _ease_out(i / SLIDE_STEPS))
        try:
            self.win.geometry(f"+{x}+{self.win.winfo_y()}")
        except tk.TclError:
            self._slide = None
            return
        if i >= SLIDE_STEPS:
            self._slide = None
            self.root.after(30, self.check_hover)  # Maus steht still, die Leiste ist weg
            return
        sl[2] = i
        sl[3] = self.root.after(max(1, SLIDE_MS // SLIDE_STEPS), self._slide_step)

    def _cancel_slide(self):
        if self._slide is not None and self._slide[3] is not None:
            try:
                self.root.after_cancel(self._slide[3])
            except tk.TclError:
                pass
        self._slide = None
