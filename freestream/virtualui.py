"""Fenster des virtuellen Decks (virtualdeck.py) – im Prozess der FreeDeck-App.

Zwei Darstellungen, Einstellungen › Geräte › Virtuelles Deck:
  window  schwebendes Fenster: Titelleiste zum Verschieben, Tasten, unten Profil und Seiten
  edge    Leiste am rechten oder linken Bildschirmrand (Tasten gedreht: Zeilen werden Spalten);
          klappt ein, wenn die Maus sie verlässt, und kommt zurück, sobald die Maus an den Rand fährt

Das Fenster nimmt keinen Fokus (overrideredirect): Tastenkürzel einer Taste landen so im
Programm, in dem du gerade arbeitest – wie bei einem echten Deck.
"""
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
COLLAPSE_MS = 900     # Randleiste klappt so lange nach dem Verlassen ein
# Gehäuse wie ein echtes Deck – dunkel, auch im hellen Design
CHROME = {"bg": "#16181e", "edge": "#2a2f3a", "fg": "#e9ecf2", "muted": "#8b93a3", "slot": "#0b0c10",
          "dot": "#3a404d", "pill": "#222630"}


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
        self._collapse_job = None
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
        shape = (id(ctrl), s["mode"], s["edge"], s["topmost"])
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
        g = ctrl.geometry
        self.cols, self.rows = (g.rows, g.cols) if self.mode == "edge" else (g.cols, g.rows)
        if self.mode == "edge":
            avail = self.root.winfo_screenheight() - 140
            self.tile = max(40, min(64, (avail - 8 * (self.rows - 1)) // self.rows))
            self.gap, self.pad, self.title_h, self.foot_h = 8, 10, 0, 26
        else:
            self.tile, self.gap, self.pad, self.title_h, self.foot_h = 72, 10, 12, 34, 44
        self.width = 2 * self.pad + self.cols * self.tile + (self.cols - 1) * self.gap
        if self.mode == "window":
            self.width = max(self.width, 300)
        self.height = self.title_h + self.pad + self.rows * self.tile + (self.rows - 1) * self.gap + self.foot_h
        self.win = tk.Toplevel(self.root, bg=CHROME["bg"])
        self.win.withdraw()
        self.win.title(_("Virtual deck"))
        self.win.overrideredirect(True)
        try:
            self.win.attributes("-topmost", bool(s["topmost"] or self.mode == "edge"))
        except tk.TclError:
            pass
        self.cv = tk.Canvas(self.win, width=self.width, height=self.height, bg=CHROME["bg"], highlightthickness=1,
                            highlightbackground=CHROME["edge"], bd=0)
        self.cv.pack()
        self.f_title = font(self.root, 10, "bold")
        self.f_small = font(self.root, 9)
        self.cv.bind("<ButtonPress-1>", self._press)
        self.cv.bind("<ButtonRelease-1>", self._release)
        self.cv.bind("<B1-Motion>", self._motion)
        if self.mode == "edge":
            self.win.bind("<Enter>", lambda e: self._expand())
            self.win.bind("<Leave>", lambda e: self._collapse_later())
        self.collapsed = False

    def _place(self, s):
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        if self.mode == "edge":
            # eingeklappt: schmaler Streifen über die ganze Höhe – die Maus findet ihn am Rand
            w, h = (8 if self.collapsed else self.width + 2), self.height + 2
            x = 0 if self.edge == "left" else sw - w
            self.win.geometry(f"{w}x{h}+{x}+{max(0, (sh - h) // 2)}")
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
        if self.mode == "edge":
            r, c = c, r
        x0 = (self.width - (self.cols * self.tile + (self.cols - 1) * self.gap)) // 2
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
        if self.mode == "edge" and self.collapsed:
            cv.configure(width=6, height=self.height)
            mid = self.height // 2
            rounded_rect(cv, 1, mid - 40, 6, mid + 40, 3, fill=CHROME["muted"], outline="")
            return
        cv.configure(width=self.width, height=self.height)
        ctrl = self.ctrl
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

    def _draw_footer(self):
        cv, ctrl = self.cv, self.ctrl
        y = self.height - self.foot_h / 2
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
                cx0 = (self.width - span) // 2
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

    def _press(self, e):
        if self.mode == "edge" and self.collapsed:
            self._expand()
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
    def _collapse_later(self):
        if self._collapse_job:
            self.root.after_cancel(self._collapse_job)
        if self._down is None:
            self._collapse_job = self.root.after(COLLAPSE_MS, lambda: self._set_collapsed(True))

    def _expand(self):
        if self._collapse_job:
            self.root.after_cancel(self._collapse_job)
            self._collapse_job = None
        if self.collapsed:
            self._set_collapsed(False)

    def _set_collapsed(self, on):
        self._collapse_job = None
        if self.win is None or self.collapsed == on:
            return
        self.collapsed = on
        self._place(virtualdeck.settings(self.cfg))
        self.draw()
