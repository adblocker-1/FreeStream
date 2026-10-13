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

from PIL import Image, ImageDraw, ImageTk

from . import actions as A
from . import catalog as C
from . import editdoc as E
from . import paths
from .models import MODELS, model_geometry, model_name
from .profiles import ProfileStore, delete_profile, import_path

log = logging.getLogger("lindeck.editor")

COL = {
    "bg": "#1b1c1f", "panel": "#25262b", "panel2": "#2e3036", "fg": "#e8e8ea", "muted": "#9a9ca5",
    "accent": "#0a84ff", "device": "#111214", "slot": "#2a2b30", "danger": "#e5484d", "drop": "#30d158",
}
ALIGN = [("top", "oben"), ("middle", "Mitte"), ("bottom", "unten")]
STYLES = [("", "Normal"), ("Bold", "Fett"), ("Italic", "Kursiv"), ("Bold Italic", "Fett kursiv")]
TEXT_WIDGETS = ("Entry", "TEntry", "Text", "Spinbox", "TSpinbox", "TCombobox")


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


class ScrollFrame(ttk.Frame):
    """Senkrecht scrollbarer Bereich."""

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, highlightthickness=0, bg=COL["panel"], bd=0)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas, style="Panel.TFrame")
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.bar.pack(side="right", fill="y")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        for seq in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
            self.canvas.bind_all(seq, self._wheel, add="+")

    def _wheel(self, e):
        w = e.widget
        while w is not None:
            if w is self.canvas:
                break
            w = getattr(w, "master", None)
        else:
            return
        step = -1 if (e.num == 4 or getattr(e, "delta", 0) > 0) else 1
        self.canvas.yview_scroll(step * 2, "units")

    def clear(self):
        for c in self.inner.winfo_children():
            c.destroy()
        self.canvas.yview_moveto(0)


class EditorApp:
    def __init__(self, root: tk.Tk, store_root=None, profile=None, daemon=None, on_quit=None):
        self.root = root
        self.daemon = daemon        # laufender Dienst im selben Programm (App-Modus)
        self.on_quit = on_quit
        self.device = None          # Seriennummer des gewählten Stream Decks
        self._device_ids = []
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

        root.title("LinDeck")
        root.geometry("1280x820")
        root.minsize(980, 640)
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

    def refresh_devices(self, initial=False):
        """Liste der angeschlossenen Stream Decks aktualisieren (alle 2 s)."""
        if self.daemon is None:
            return
        ctrls = sorted(self._controllers(), key=lambda c: (model_name(c.model), c.serial))
        ids = [c.serial for c in ctrls]
        if ids != self._device_ids:
            self._device_ids = ids
            self.device_box["values"] = [f"{model_name(c.model)}  ·  {c.serial}" for c in ctrls] or \
                ["Kein Stream Deck verbunden"]
            if self.device not in ids:
                self.device = ids[0] if ids else None
                if self.device and not initial:
                    self._show_device_profile()
            if self.device:
                self.device_box.current(ids.index(self.device))
            else:
                self.device_box.current(0)
            n = len(ids)
            self.device_status.set("● verbunden" if n == 1 else f"● {n} Geräte verbunden" if n else "○ kein Gerät")
            if initial and self.device:
                self._show_device_profile()
        try:
            self.root.after_cancel(self._dev_job)
        except (AttributeError, tk.TclError):
            pass
        self._dev_job = self.root.after(2000, self.refresh_devices)

    def _on_device_selected(self, _e=None):
        i = self.device_box.current()
        if 0 <= i < len(self._device_ids):
            self.device = self._device_ids[i]
            self._show_device_profile()

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
                log.debug("Gerät umschalten: %s", e)
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
            from .autoimport import notify
            notify("LinDeck läuft weiter", "Das Stream Deck bleibt aktiv. Öffnen über das Menü „LinDeck“.")

    def quit_app(self):
        self.close_doc()
        if self.on_quit:
            self.on_quit()
        else:
            self.root.destroy()

    # ================================================================ Aufbau
    def _style(self):
        s = ttk.Style(self.root)
        s.theme_use("clam")
        bg, panel, fg = COL["bg"], COL["panel"], COL["fg"]
        s.configure(".", background=bg, foreground=fg, fieldbackground=COL["panel2"], bordercolor="#3a3c42",
                    lightcolor=panel, darkcolor=panel, troughcolor=bg, insertcolor=fg, focuscolor=COL["accent"])
        s.configure("TFrame", background=bg)
        s.configure("Panel.TFrame", background=panel)
        s.configure("TLabel", background=bg, foreground=fg)
        s.configure("Panel.TLabel", background=panel)
        s.configure("Muted.TLabel", background=panel, foreground=COL["muted"])
        s.configure("Head.TLabel", background=panel, font=("TkDefaultFont", 13, "bold"))
        s.configure("Section.TLabel", background=panel, foreground=COL["muted"], font=("TkDefaultFont", 9, "bold"))
        s.configure("Status.TLabel", background=COL["panel2"], foreground=COL["muted"], padding=(10, 4))
        s.configure("TButton", background=COL["panel2"], foreground=fg, padding=(8, 3), borderwidth=1, width=-4)
        s.map("TButton", background=[("active", "#3a3d45"), ("disabled", bg)],
              foreground=[("disabled", "#5c5e66")])
        s.configure("Accent.TButton", background=COL["accent"])
        s.map("Accent.TButton", background=[("active", "#3b9bff")])
        s.configure("Page.TButton", padding=(10, 3))
        s.configure("PageOn.TButton", padding=(10, 3), background=COL["accent"])
        s.map("PageOn.TButton", background=[("active", "#3b9bff")])
        s.configure("TCheckbutton", background=panel, foreground=fg)
        s.map("TCheckbutton", background=[("active", panel)], indicatorcolor=[("selected", COL["accent"])])
        s.configure("TEntry", foreground=fg, fieldbackground=COL["panel2"])
        s.configure("TCombobox", foreground=fg, fieldbackground=COL["panel2"], background=COL["panel2"],
                    arrowcolor=fg)
        s.map("TCombobox", fieldbackground=[("readonly", COL["panel2"])], foreground=[("readonly", fg)],
              selectbackground=[("readonly", COL["panel2"])], selectforeground=[("readonly", fg)])
        s.configure("TSpinbox", foreground=fg, fieldbackground=COL["panel2"], arrowcolor=fg,
                    background=COL["panel2"])
        s.configure("Treeview", background=panel, fieldbackground=panel, foreground=fg, rowheight=26,
                    borderwidth=0)
        s.map("Treeview", background=[("selected", COL["accent"])])
        s.configure("TLabelframe", background=panel)
        s.configure("TPanedwindow", background=bg)
        self.root.option_add("*TCombobox*Listbox.background", COL["panel2"])
        self.root.option_add("*TCombobox*Listbox.foreground", fg)
        self.root.option_add("*Menu.background", COL["panel2"])
        self.root.option_add("*Menu.foreground", fg)
        self.root.option_add("*Menu.activeBackground", COL["accent"])

    def _build(self):
        r = self.root
        # --- Kopfzeile mit Gerät (nur App-Modus)
        if self.daemon is not None:
            head = ttk.Frame(r, padding=(10, 8, 10, 0))
            head.pack(side="top", fill="x")
            ttk.Label(head, text="LinDeck", font=("TkDefaultFont", 13, "bold")).pack(side="left", padx=(0, 14))
            self.device_box = ttk.Combobox(head, state="readonly", width=34)
            self.device_box.pack(side="left")
            self.device_box.bind("<<ComboboxSelected>>", self._on_device_selected)
            self.device_status = tk.StringVar(value="")
            ttk.Label(head, textvariable=self.device_status, foreground="#30d158").pack(side="left", padx=10)
            ttk.Button(head, text="Beenden", command=self.quit_app).pack(side="right")
            ttk.Button(head, text="Fenster schließen", command=self.hide).pack(side="right", padx=4)
        # --- Werkzeugleiste
        top = ttk.Frame(r, padding=(10, 8))
        top.pack(side="top", fill="x")
        ttk.Label(top, text="Profil").pack(side="left")
        self.profile_var = tk.StringVar()
        self.profile_box = ttk.Combobox(top, textvariable=self.profile_var, state="readonly", width=28)
        self.profile_box.pack(side="left", padx=(6, 6))
        self.profile_box.bind("<<ComboboxSelected>>", self._on_profile_selected)
        self.btn = {}
        for key, text, cmd in (("new", "Neu…", self.new_profile), ("dup", "Duplizieren", self.duplicate_profile),
                               ("ren", "Umbenennen…", self.rename_profile), ("del", "Löschen", self.delete_profile)):
            self.btn[key] = ttk.Button(top, text=text, command=cmd)
            self.btn[key].pack(side="left", padx=2)
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8)
        for key, text, cmd in (("imp", "Importieren…", self.import_profiles), ("exp", "Exportieren…", self.export_profile)):
            self.btn[key] = ttk.Button(top, text=text, command=cmd)
            self.btn[key].pack(side="left", padx=2)
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8)
        self.btn["undo"] = ttk.Button(top, text="↶", width=3, command=self.undo)
        self.btn["undo"].pack(side="left", padx=2)
        self.btn["redo"] = ttk.Button(top, text="↷", width=3, command=self.redo)
        self.btn["redo"].pack(side="left", padx=2)
        self.model_var = tk.StringVar()
        self._model_codes = list(MODELS)
        self.model_box = ttk.Combobox(top, textvariable=self.model_var, state="readonly", width=22,
                                      values=[MODELS[c][0] for c in self._model_codes])
        self.model_box.pack(side="right")
        self.model_box.bind("<<ComboboxSelected>>", self._on_model_selected)
        ttk.Label(top, text="Modell").pack(side="right", padx=6)

        # --- Statusleiste
        self.status_var = tk.StringVar(value="")
        ttk.Label(r, textvariable=self.status_var, style="Status.TLabel", anchor="w").pack(side="bottom", fill="x")

        body = ttk.PanedWindow(r, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        # --- links: Gerät + Eigenschaften
        left = ttk.PanedWindow(body, orient="vertical")
        body.add(left, weight=4)
        dev = ttk.Frame(left, style="Panel.TFrame", padding=10)
        left.add(dev, weight=1)
        nav = ttk.Frame(dev, style="Panel.TFrame")
        nav.pack(side="top", fill="x")
        self.back_btn = ttk.Button(nav, text="↩ Zurück", command=self.folder_back)
        self.back_btn.pack(side="left")
        self.crumb_var = tk.StringVar()
        ttk.Label(nav, textvariable=self.crumb_var, style="Panel.TLabel").pack(side="left", padx=10)
        self.pagebar = ttk.Frame(dev, style="Panel.TFrame")
        self.pagebar.pack(side="bottom", fill="x")
        self.canvas = tk.Canvas(dev, bg=COL["panel"], highlightthickness=0, height=300)
        self.canvas.pack(side="top", fill="both", expand=True, pady=6)
        self.canvas.bind("<Configure>", lambda e: self.redraw())
        self.canvas.bind("<ButtonPress-1>", self._canvas_press)
        self.canvas.bind("<B1-Motion>", self._canvas_motion)
        self.canvas.bind("<ButtonRelease-1>", self._canvas_release)
        self.canvas.bind("<Double-Button-1>", self._canvas_double)
        self.canvas.bind("<Button-3>", self._canvas_menu)

        insp = ttk.Frame(left, style="Panel.TFrame", padding=(12, 10))
        left.add(insp, weight=1)
        self.inspector = ScrollFrame(insp, style="Panel.TFrame")
        self.inspector.pack(fill="both", expand=True)

        # --- rechts: Aktionen
        right = ttk.Frame(body, style="Panel.TFrame", padding=10)
        body.add(right, weight=1)
        ttk.Label(right, text="AKTIONEN", style="Section.TLabel").pack(anchor="w")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self.fill_catalog())
        ttk.Entry(right, textvariable=self.search_var).pack(fill="x", pady=(6, 6))
        self.tree = ttk.Treeview(right, show="tree", selectmode="browse")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<ButtonPress-1>", self._tree_press)
        self.tree.bind("<B1-Motion>", self._tree_motion)
        self.tree.bind("<ButtonRelease-1>", self._tree_release)
        self.tree.bind("<Double-Button-1>", self._tree_double)
        self.tree.bind("<<TreeviewSelect>>", self._tree_select)
        self.tree_hint = tk.StringVar(value="Auf eine Taste ziehen oder doppelklicken")
        ttk.Label(right, textvariable=self.tree_hint, style="Muted.TLabel", wraplength=240,
                  justify="left").pack(fill="x", pady=(6, 0))

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

    # ============================================================== Profile
    def refresh_profiles(self):
        self.store.reload()
        profs = sorted(self.store.profiles.values(), key=lambda p: (p.name.lower(), p.id))
        self._profile_ids = [p.id for p in profs]
        self.profile_box["values"] = [f"{p.name}  ·  {p.model_name}" for p in profs]
        if self.doc and self.doc.id in self._profile_ids:
            self.profile_box.current(self._profile_ids.index(self.doc.id))

    def _on_profile_selected(self, _e=None):
        i = self.profile_box.current()
        if 0 <= i < len(self._profile_ids):
            self.open_profile(self._profile_ids[i])

    def close_doc(self):
        if self.doc:
            if self._save_job:
                self.root.after_cancel(self._save_job)
                self._save_job = None
            try:
                self.doc.cleanup()
            except OSError as e:
                log.warning("Aufräumen fehlgeschlagen: %s", e)
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
            messagebox.showerror("Profil öffnen", str(e), parent=self.root)
            self.show_empty()
            return
        self.doc.on_change = self._on_doc_change
        self.nav = [self.doc.page_ids[0]] if self.doc.page_ids else []
        self.selected = None
        self._img_cache.clear()
        self.refresh_profiles()
        self.root.title(f"LinDeck – {self.doc.name}")
        if self.doc.model in self._model_codes:
            self.model_box.current(self._model_codes.index(self.doc.model))
        else:
            self.model_var.set(model_name(self.doc.model) if self.doc.model else "unbekannt")
        self.fill_catalog()
        self.refresh_all()
        self.status(f"{self.doc.name} geöffnet – Änderungen werden automatisch gespeichert")
        if not from_device:
            self._device_follow(0)

    def show_empty(self):
        self.doc = None
        self.canvas.delete("all")
        self.canvas.create_text(20, 30, anchor="nw", fill=COL["muted"], font=("TkDefaultFont", 12),
                                text="Noch keine Profile vorhanden.\n\n„Neu…“ legt ein leeres Profil an,\n"
                                     "„Importieren…“ übernimmt Profile der Elgato-App.")
        for c in self.pagebar.winfo_children():
            c.destroy()
        self.inspector.clear()
        for k in ("dup", "ren", "del", "exp", "undo", "redo"):
            self.btn[k].state(["disabled"])
        self.back_btn.state(["disabled"])
        self.crumb_var.set("")
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
        name = simpledialog.askstring("Profil duplizieren", "Name der Kopie:",
                                      initialvalue=f"{self.doc.name} (Kopie)", parent=self.root)
        if not name:
            return
        self.doc.save()
        dst = E.duplicate_profile(self.doc.root, name)
        self.refresh_profiles()
        self.open_profile(dst.name[: -len(".sdProfile")])

    def rename_profile(self):
        if not self.doc:
            return
        name = simpledialog.askstring("Profil umbenennen", "Neuer Name:", initialvalue=self.doc.name,
                                      parent=self.root)
        if name and name.strip():
            self.doc.rename(name)
            self.save_now()
            self.refresh_profiles()
            self.root.title(f"LinDeck – {self.doc.name}")

    def delete_profile(self):
        if not self.doc:
            return
        if not messagebox.askyesno("Profil löschen", f"Profil „{self.doc.name}“ endgültig löschen?",
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
            self.profile_var.set("")
            self.show_empty()

    def import_profiles(self):
        files = filedialog.askopenfilenames(
            parent=self.root, title="Profile importieren",
            filetypes=[("Stream-Deck-Profile", "*.streamDeckProfile *.streamDeckProfilesBackup *.zip"),
                       ("Alle Dateien", "*")])
        if not files:
            return
        ids = []
        for f in files:
            try:
                ids += import_path(f, self.store_root)
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("Import", f"{f}:\n{e}", parent=self.root)
        self.refresh_profiles()
        self.status(f"{len(ids)} Profil(e) importiert")
        if ids:
            self.open_profile(ids[0])

    def export_profile(self):
        if not self.doc:
            return
        safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in self.doc.name).strip() or "Profil"
        f = filedialog.asksaveasfilename(parent=self.root, title="Profil exportieren",
                                         initialfile=f"{safe}.streamDeckProfile",
                                         defaultextension=".streamDeckProfile",
                                         filetypes=[("Stream-Deck-Profil", "*.streamDeckProfile")])
        if f:
            out = self.doc.export(f)
            self.status(f"Exportiert: {out}")

    def _on_model_selected(self, _e=None):
        if not self.doc:
            return
        code = self._model_codes[self.model_box.current()]
        if code == self.doc.model:
            return
        old, new = self.doc.geometry, model_geometry(code)
        if new and (new.cols < old.cols or new.rows < old.rows or new.dials < old.dials):
            if not messagebox.askyesno("Gerät ändern", "Das neue Gerät hat weniger Tasten oder Dials. "
                                       "Aktionen außerhalb bleiben gespeichert, werden aber nicht angezeigt.\n\n"
                                       "Fortfahren?", parent=self.root):
                if self.doc.model in self._model_codes:
                    self.model_box.current(self._model_codes.index(self.doc.model))
                return
        self.doc.set_model(code)
        self.selected = None
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
        self.status("Ändere …")

    def save_now(self):
        self._save_job = None
        if not self.doc:
            return
        try:
            if self.doc.save():
                self.status("Gespeichert ✓  –  das Stream Deck wird automatisch aktualisiert")
        except OSError as e:
            self.status(f"Speichern fehlgeschlagen: {e}")
            messagebox.showerror("Speichern", str(e), parent=self.root)

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
        for k in ("dup", "ren", "del", "exp"):
            self.btn[k].state(["!disabled"])
        self.btn["undo"].state(["!disabled" if self.doc.undo_stack else "disabled"])
        self.btn["redo"].state(["!disabled" if self.doc.redo_stack else "disabled"])

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
            messagebox.showinfo("Einstellungsseite", "Dafür muss LinDeck laufen (Menü: „LinDeck“ "
                                "oder `lindeck`), weil es die Plugins ausführt.", parent=self.root)
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
        self.status("Einstellungsseite geöffnet – Änderungen werden automatisch übernommen")
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
        if not self.doc:
            return
        in_folder = len(self.nav) > 1
        self.back_btn.state(["!disabled" if in_folder else "disabled"])
        crumbs = [self.doc.name]
        ids = self.doc.page_ids
        if self.nav and self.nav[0] in ids:
            crumbs.append(f"Seite {ids.index(self.nav[0]) + 1}")
        crumbs += [self.doc.folder_title(u) for u in self.nav[1:]]
        self.crumb_var.set("  ›  ".join(crumbs))
        ttk.Label(self.pagebar, text="Seiten", style="Muted.TLabel").pack(side="left", padx=(0, 8))
        for i, pid in enumerate(ids):
            on = self.nav and self.nav[0] == pid
            b = ttk.Button(self.pagebar, text=str(i + 1), width=3, style="PageOn.TButton" if on else "Page.TButton",
                           command=lambda p=pid: self.goto_page(p))
            b.pack(side="left", padx=1)
            b._page = pid
        ttk.Button(self.pagebar, text="+", width=3, command=self.add_page).pack(side="left", padx=(8, 1))
        rm = ttk.Button(self.pagebar, text="−", width=3, command=self.remove_page)
        rm.pack(side="left", padx=1)
        left = ttk.Button(self.pagebar, text="◀", width=3, command=lambda: self.move_page(-1))
        left.pack(side="left", padx=(8, 1))
        right = ttk.Button(self.pagebar, text="▶", width=3, command=lambda: self.move_page(1))
        right.pack(side="left", padx=1)
        if len(ids) <= 1:
            rm.state(["disabled"])
            left.state(["disabled"])
            right.state(["disabled"])

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
                "Seite löschen", "Die Seite enthält Aktionen. Trotzdem löschen?", parent=self.root):
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
            self.status("Ordner nicht gefunden")
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
        key = (self.page_uuid, slot[0], json.dumps(data, sort_keys=True), size,
               self.doc.page_ids.index(self.nav[0]) if self.nav and self.nav[0] in self.doc.page_ids else 0)
        img = self._img_cache.get(key)
        if img is None:
            if slot[0] == "dial":
                # in Originalgröße des Touchstreifen-Segments zeichnen, dann skalieren
                img = E.preview_dial(self.doc, self.page_uuid, data, (max(100, 100 * size[0] // size[1]), 100))
                img = img.resize(size, Image.LANCZOS)
            else:
                img = E.preview_action(self.doc, self.page_uuid, data, size, page_index=key[-1])
            img = _rounded(img.convert("RGBA"), max(6, size[0] // 8))
            self._img_cache[key] = img
        return img

    def redraw(self):
        cv = self.canvas
        cv.delete("all")
        self._photos = []
        self._hit = []
        if not self.doc or not self.page_uuid or not self.doc.page(self.page_uuid):
            return
        g, keys, dials = self._slots()
        pad = 26
        ts, gap, grid_w, seg_w, dial_h, knob, total_h = _layout(g, cv.winfo_width(), cv.winfo_height(), pad)
        cw, ch = max(cv.winfo_width(), grid_w + 2 * pad), max(cv.winfo_height(), total_h + 2 * pad)
        x0 = (cw - grid_w) // 2
        y0 = max(pad, (ch - total_h) // 2)
        _round_rect(cv, x0 - pad, y0 - pad, x0 + grid_w + pad, y0 + total_h + pad, 26, fill=COL["device"],
                    outline="#000000")
        page = self.doc.page(self.page_uuid)
        for slot in keys:
            c, r = slot[1], slot[2]
            x, y = x0 + c * (ts + gap), y0 + r * (ts + gap)
            self._draw_slot(slot, page.get(slot), x, y, ts, ts)
        if g.dials:
            y = y0 + g.rows * ts + (g.rows - 1) * gap + gap * 2
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
        if data is None:
            _round_rect(cv, x, y, x + w, y + h, max(6, w // 8), fill=COL["slot"], outline="")
            cv.create_text(x + w / 2, y + h / 2, text="+", fill="#4a4c53", font=("TkDefaultFont", max(10, w // 5)))
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
            self.status("Auf der Zielseite ist kein Platz frei")
            return
        self.doc.move(self.page_uuid, slot, pid, free)
        self.selected = None
        self.refresh_all()
        self.status(f"Aktion auf Seite {self.doc.page_ids.index(pid) + 1} verschoben")

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
            m.add_command(label="Ordner öffnen", command=lambda: self.open_folder(
                (data.get("Settings") or {}).get("ProfileUUID")))
            m.add_separator()
        m.add_command(label="Ausschneiden", accelerator="Strg+X", command=self.cut_selected,
                      state="normal" if data else "disabled")
        m.add_command(label="Kopieren", accelerator="Strg+C", command=self.copy_selected,
                      state="normal" if data else "disabled")
        m.add_command(label="Einfügen", accelerator="Strg+V", command=self.paste_selected,
                      state="normal" if self.clipboard else "disabled")
        m.add_separator()
        if len(self.doc.page_ids) > 1 and data:
            sub = tk.Menu(m, tearoff=0)
            for i, pid in enumerate(self.doc.page_ids):
                if pid != self.nav[0] or len(self.nav) > 1:
                    sub.add_command(label=f"Seite {i + 1}", command=lambda p=pid: self._move_to_page(slot, p))
            m.add_cascade(label="Verschieben auf", menu=sub)
        m.add_command(label="Löschen", accelerator="Entf", command=self.delete_selected,
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
                self.status("Kopiert")

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
    def fill_catalog(self):
        t = self.tree
        t.delete(*t.get_children())
        q = self.search_var.get().strip().lower()
        dials = self.doc.geometry.dials if self.doc else 0
        try:
            extra = C.plugin_types()
        except OSError:
            extra = []
        catalog = C.CATALOG + extra
        self._types = {a.uuid: a for a in catalog}
        for cat in list(dict.fromkeys(a.category for a in catalog)):
            items = [a for a in catalog if a.category == cat and (C.KEY in a.controllers or dials)
                     and a.uuid != "com.elgato.streamdeck.multiactions.delay"
                     and (not q or q in a.name.lower() or q in a.description.lower())]
            if not items:
                continue
            node = t.insert("", "end", iid=f"cat:{cat}", text=cat, open=True)
            for a in items:
                suffix = "  ◎" if C.DIAL in a.controllers and C.KEY not in a.controllers else ""
                t.insert(node, "end", iid=a.uuid, text=f"  {a.name}{suffix}")

    def _tree_item(self, e):
        it = self.tree.identify_row(e.y)
        return it if it in self._types else None

    def _tree_select(self, _e=None):
        sel = self.tree.selection()
        a = self._types.get(sel[0]) if sel else None
        if a:
            where = "nur Dials" if a.controllers == (C.DIAL,) else "Tasten und Dials" if C.DIAL in a.controllers \
                else "Tasten"
            self.tree_hint.set(f"{a.name}: {a.description or a.category} ({where}).\n"
                               "Auf eine Taste ziehen oder doppelklicken.")

    def _tree_press(self, e):
        self._tdrag = {"uuid": self._tree_item(e), "x": e.x_root, "y": e.y_root, "win": None}

    def _tree_motion(self, e):
        d = getattr(self, "_tdrag", None)
        if not d or not d["uuid"] or not self.doc:
            return
        if d["win"] is None and abs(e.x_root - d["x"]) + abs(e.y_root - d["y"]) > 6:
            w = tk.Toplevel(self.root)
            w.overrideredirect(True)
            w.attributes("-topmost", True)
            tk.Label(w, text=self._types[d["uuid"]].name, bg=COL["accent"], fg="white", padx=10, pady=4).pack()
            d["win"] = w
        if d["win"] is not None:
            d["win"].geometry(f"+{e.x_root + 12}+{e.y_root + 8}")
            cx, cy = e.x_root - self.canvas.winfo_rootx(), e.y_root - self.canvas.winfo_rooty()
            self._show_drop_target(self.slot_at(cx, cy))

    def _tree_release(self, e):
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

    def _tree_double(self, e):
        uuid = self._tree_item(e)
        if not uuid or not self.doc:
            return "break"
        a = self._types[uuid]
        slot = self.selected
        if slot is None or not self._fits({"UUID": uuid}, slot, quiet=True):
            _, keys, dials = self._slots()
            page = self.doc.page(self.page_uuid)
            pool = [s for s in keys + dials if s[0] in a.controllers]
            slot = next((s for s in pool if page.get(s) is None), None)
        if slot is None:
            self.status("Kein freier Platz – Taste auswählen, um sie zu ersetzen")
            return "break"
        self.place_action(uuid, slot)
        return "break"

    def _fits(self, data, slot, quiet=False):
        t = self._types.get((data or {}).get("UUID", ""))
        if t and slot[0] not in t.controllers:
            if not quiet:
                self.status(f"„{t.name}“ ist nur für {'Dials' if C.DIAL in t.controllers else 'Tasten'} geeignet")
            return False
        return True

    def place_action(self, uuid, slot):
        if not self._fits({"UUID": uuid}, slot):
            return
        old = self.doc.get(self.page_uuid, slot)
        if old and old.get("UUID") == C.FOLDER_UUID and self.doc.page((old.get("Settings") or {}).get("ProfileUUID", "")):
            if not messagebox.askyesno("Ordner ersetzen", "Der Ordner und sein Inhalt werden ersetzt. Fortfahren?",
                                       parent=self.root):
                return
        self.doc.place(self.page_uuid, slot, uuid)
        self.select(slot)
        self.status(f"{self._types[uuid].name} hinzugefügt")

    # ========================================================= Eigenschaften
    def build_inspector(self):
        self.inspector.clear()
        f = self.inspector.inner
        if not self.doc:
            return
        if self.selected is None:
            ttk.Label(f, text="Taste auswählen, um sie zu bearbeiten.", style="Muted.TLabel").pack(anchor="w")
            ttk.Label(f, text="Tipps: Aktionen von rechts auf Tasten ziehen · Tasten per Drag & Drop verschieben "
                              "(auch auf eine andere Seite) · Rechtsklick für Kopieren/Einfügen · Doppelklick "
                              "öffnet Ordner · Strg+Z macht rückgängig.",
                      style="Muted.TLabel", wraplength=640, justify="left").pack(anchor="w", pady=(6, 0))
            return
        data = self.doc.get(self.page_uuid, self.selected)
        where = f"Taste {self.selected[1] + 1},{self.selected[2] + 1}" if self.selected[0] == "key" \
            else f"Dial {self.selected[1] + 1}"
        if data is None:
            ttk.Label(f, text=f"{where} – leer", style="Head.TLabel").pack(anchor="w")
            ttk.Label(f, text="Aktion aus der Liste rechts hierher ziehen oder dort doppelklicken.",
                      style="Muted.TLabel").pack(anchor="w", pady=(4, 0))
            if self.clipboard:
                ttk.Button(f, text="Einfügen", command=self.paste_selected).pack(anchor="w", pady=8)
            return
        ActionForm(self, f, data, self.page_uuid, self.selected, header=where).build()


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

    # ------------------------------------------------------------ Aufbau
    def build(self):
        f = self.parent
        head = ttk.Frame(f, style="Panel.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text=C.display_name(self.data), style="Head.TLabel").pack(side="left")
        if self.header:
            ttk.Label(head, text="  " + self.header, style="Muted.TLabel").pack(side="left")
        btns = ttk.Frame(head, style="Panel.TFrame")
        btns.pack(side="right")
        if self.data.get("UUID") == C.FOLDER_UUID and self.appearance:
            ttk.Button(btns, text="Ordner öffnen", command=lambda: self.app.open_folder(
                (self.data.get("Settings") or {}).get("ProfileUUID"))).pack(side="left", padx=2)
        if self.appearance and self._has_pi():
            ttk.Button(btns, text="Einstellungsseite…", style="Accent.TButton",
                       command=lambda: self.app.open_property_inspector(self.page_uuid, self.slot)
                       ).pack(side="left", padx=2)
        ttk.Button(btns, text="Einstellungen (JSON)…", command=self.edit_json).pack(side="left", padx=2)
        if self.appearance:
            ttk.Button(btns, text="Löschen", command=self.app.delete_selected).pack(side="left", padx=2)
        ttk.Label(f, text=self.data.get("UUID", ""), style="Muted.TLabel").pack(anchor="w")
        if not A.is_supported(self.data.get("UUID", "")):
            plugin = (self.data.get("Plugin") or {}).get("Name") or self.data.get("UUID", "").rsplit(".", 1)[0]
            ttk.Label(f, text=f"Aktion des Plugins „{plugin}“ – unter Linux nicht ausführbar (Plugin nicht installiert "
                              "oder nur für Windows/macOS). Titel, Icon und Einstellungen bleiben erhalten.",
                      style="Muted.TLabel", wraplength=640, justify="left").pack(anchor="w", pady=(4, 0))
        if self.appearance:
            self.app_frame = ttk.Frame(f, style="Panel.TFrame")
            self.app_frame.pack(fill="x", pady=(10, 0))
            self.build_appearance()
        fields = self.type.fields_for(self.slot[0]) if self.type else []
        if fields:
            self._section(f, "EINSTELLUNGEN")
            grid = ttk.Frame(f, style="Panel.TFrame")
            grid.pack(fill="x")
            grid.columnconfigure(1, weight=1)
            for row, fld in enumerate(fields):
                self.build_field(grid, row, fld)
        if self.type and self.type.children:
            self.build_children(f)

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
        f = self.app_frame
        for c in f.winfo_children():
            c.destroy()
        ttk.Label(f, text="DARSTELLUNG", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
        states = self.data.get("States") or [{}]
        self.state_idx = min(self.state_idx, len(states) - 1)
        st = states[self.state_idx] if states else {}
        f.columnconfigure(1, weight=1)
        r = 1
        if len(states) > 1:
            ttk.Label(f, text="Zustand", style="Panel.TLabel").grid(row=r, column=0, sticky="w", padx=(0, 10))
            sv = ttk.Combobox(f, state="readonly", width=12, values=[f"Zustand {i + 1}" for i in range(len(states))])
            sv.current(self.state_idx)
            sv.grid(row=r, column=1, sticky="w", pady=2)
            sv.bind("<<ComboboxSelected>>", lambda e: (setattr(self, "state_idx", sv.current()),
                                                       self.build_appearance()))
            r += 1
        # Titel
        ttk.Label(f, text="Titel", style="Panel.TLabel").grid(row=r, column=0, sticky="nw", padx=(0, 10), pady=2)
        txt = tk.Text(f, height=2, width=30, bg=COL["panel2"], fg=COL["fg"], insertbackground=COL["fg"],
                      relief="flat", font=("TkDefaultFont", 10), wrap="none", highlightthickness=1,
                      highlightbackground="#3a3c42", highlightcolor=COL["accent"])
        txt.insert("1.0", st.get("Title") or "")
        txt.grid(row=r, column=1, sticky="ew", pady=2)
        txt.bind("<KeyRelease>", lambda e: self._set_state("Title", txt.get("1.0", "end-1c")))
        r += 1
        row = ttk.Frame(f, style="Panel.TFrame")
        row.grid(row=r, column=1, sticky="w", pady=2)
        show = tk.BooleanVar(value=st.get("ShowTitle", True) is not False)
        ttk.Checkbutton(row, text="anzeigen", variable=show,
                        command=lambda: self._set_state("ShowTitle", show.get())).pack(side="left")
        al = ttk.Combobox(row, state="readonly", width=7, values=[n for _, n in ALIGN])
        cur = (st.get("TitleAlignment") or "bottom").lower()
        al.current(next((i for i, (v, _) in enumerate(ALIGN) if v == cur), 2))
        al.bind("<<ComboboxSelected>>", lambda e: self._set_state("TitleAlignment", ALIGN[al.current()][0]))
        al.pack(side="left", padx=(10, 0))
        size = tk.StringVar(value=str(st.get("FontSize") or 12))
        sp = ttk.Spinbox(row, from_=6, to=40, width=4, textvariable=size)
        sp.pack(side="left", padx=(8, 0))
        size.trace_add("write", lambda *a: self._set_int_state("FontSize", size.get()))
        sty = ttk.Combobox(row, state="readonly", width=10, values=[n for _, n in STYLES])
        cur = (st.get("FontStyle") or "").replace("Regular", "")
        sty.current(next((i for i, (v, _) in enumerate(STYLES) if v.lower() == cur.lower()), 0))
        sty.bind("<<ComboboxSelected>>", lambda e: self._set_state("FontStyle", STYLES[sty.current()][0]))
        sty.pack(side="left", padx=(8, 0))
        color = st.get("TitleColor") or "#ffffff"
        cb = tk.Button(row, text="  ", bg=color, activebackground=color, relief="flat", width=3,
                       command=lambda: self._pick_color(cb))
        cb.pack(side="left", padx=(8, 0))
        und = tk.BooleanVar(value=bool(st.get("FontUnderline")))
        ttk.Checkbutton(row, text="unterstrichen", variable=und,
                        command=lambda: self._set_state("FontUnderline", und.get())).pack(side="left", padx=(8, 0))
        r += 1
        # Icon
        ttk.Label(f, text="Icon", style="Panel.TLabel").grid(row=r, column=0, sticky="w", padx=(0, 10), pady=(6, 2))
        irow = ttk.Frame(f, style="Panel.TFrame")
        irow.grid(row=r, column=1, sticky="w", pady=(6, 2))
        prev = E.preview_action(self.doc, self.page_uuid, self.data, (56, 56), state=self.state_idx) \
            if self.slot[0] == "key" else E.preview_dial(self.doc, self.page_uuid, self.data, (112, 56))
        self._icon_photo = ImageTk.PhotoImage(_rounded(prev.convert("RGBA"), 7), master=self.app.root)
        tk.Label(irow, image=self._icon_photo, bg=COL["panel"]).pack(side="left")
        ttk.Button(irow, text="Bild wählen…", command=self._choose_icon).pack(side="left", padx=(10, 2))
        b = ttk.Button(irow, text="Standard", command=lambda: self._set_state("Image", "", rebuild=True))
        b.pack(side="left", padx=2)
        if not st.get("Image"):
            b.state(["disabled"])

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
        res = colorchooser.askcolor(color=cur, parent=self.app.root, title="Titelfarbe")
        if res and res[1]:
            btn.configure(bg=res[1], activebackground=res[1])
            self._set_state("TitleColor", res[1])

    def _choose_icon(self):
        f = filedialog.askopenfilename(parent=self.app.root, title="Icon wählen",
                                       filetypes=[("Bilder", "*.png *.jpg *.jpeg *.gif *.svg *.webp *.bmp"),
                                                  ("Alle Dateien", "*")])
        if not f:
            return
        try:
            self.doc.set_icon(self.page_uuid, self.slot, self.state_idx, f, action=self.data)
        except E.EditError as e:
            messagebox.showerror("Icon", str(e), parent=self.app.root)
            return
        self.build_appearance()

    # ------------------------------------------------------------ Felder
    def _set(self, key, value):
        self.doc.update_settings(self.page_uuid, self.slot, key, value, action=self.data)

    def build_field(self, grid, row, fld: C.Field):
        s = self.data.setdefault("Settings", {})
        ttk.Label(grid, text=fld.label, style="Panel.TLabel").grid(row=row, column=0, sticky="nw", padx=(0, 10),
                                                                   pady=3)
        cell = ttk.Frame(grid, style="Panel.TFrame")
        cell.grid(row=row, column=1, sticky="ew", pady=3)
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
                          font=("TkDefaultFont", 10), highlightthickness=1, highlightbackground="#3a3c42",
                          highlightcolor=COL["accent"])
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
            box = ttk.Combobox(cell, state="readonly", width=28, values=[n for _, n in vals])
            cur = s.get(k)
            idx = next((i for i, (v, _) in enumerate(vals) if str(v) == str(cur)), 0)
            box.current(idx)
            box.bind("<<ComboboxSelected>>", lambda e: self._set(k, vals[box.current()][0]))
            box.pack(side="left")
        elif fld.kind == "page":
            n = len(self.doc.page_ids)
            box = ttk.Combobox(cell, state="readonly", width=12, values=[f"Seite {i + 1}" for i in range(n)])
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
                               values=["(nächstes Profil)"] + [f"{p.name}  ·  {p.model_name}" for p in profs])
            cur = (s.get(k) or "").lower()
            box.current(next((i for i, p in enumerate(ids) if p.lower() == cur), 0))
            box.bind("<<ComboboxSelected>>", lambda e: self._set(k, ids[box.current()]))
            box.pack(side="left")
        elif fld.kind == "hotkey":
            HotkeyField(cell, self, int(k)).pack(side="left", fill="x", expand=True)

    def _browse(self, var):
        f = filedialog.askopenfilename(parent=self.app.root, title="Datei wählen")
        if f:
            var.set(f)

    # ----------------------------------------------------- Kindaktionen
    def build_children(self, f):
        kind, labels = self.type.children
        entries = C.child_lists(self.data)
        self._section(f, "ENTHALTENE AKTIONEN")
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
                mb = ttk.Menubutton(row, text="Wählen")
                mb["menu"] = self._catalog_menu(mb, lambda u, i=i: self._set_slot_child(i, u))
                mb.pack(side="left", padx=2)
                b1 = ttk.Button(row, text="Bearbeiten…", command=lambda c=child: self._edit_child(c))
                b1.pack(side="left", padx=2)
                b2 = ttk.Button(row, text="Leeren", command=lambda i=i: self._set_slot_child(i, None))
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
            mb = ttk.Menubutton(col, text="Hinzufügen")
            mb["menu"] = self._catalog_menu(mb, lambda u, grp=group: self._add_child(grp, u),
                                            with_delay=self.data.get("UUID", "").startswith(
                                                "com.elgato.streamdeck.multiactions"))
            mb.pack(fill="x", pady=1)

            def sel(lb=lb):
                s = lb.curselection()
                return s[0] if s else None
            ttk.Button(col, text="Bearbeiten…", command=lambda grp=group, sel=sel: self._edit_child(
                grp[sel()] if sel() is not None else None)).pack(fill="x", pady=1)
            ttk.Button(col, text="Entfernen", command=lambda grp=group, sel=sel: self._remove_child(grp, sel())
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
        self.entry = ttk.Entry(self, textvariable=self.var, width=26)
        self.entry._hotkey = True
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<FocusIn>", lambda e: self.var.set("Tasten drücken …"))
        self.entry.bind("<FocusOut>", lambda e: self.var.set(self._current() or ""))
        self.entry.bind("<KeyPress>", self._key)
        ttk.Button(self, text="✕", width=3, command=self.clear).pack(side="left", padx=(4, 0))
        ttk.Button(self, text="Eingeben…", command=self.type_in).pack(side="left", padx=(4, 0))

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
        text = simpledialog.askstring("Tastenkombination", "z. B. Strg+Umschalt+T, Alt+F4, Super+E, F13:",
                                      initialvalue=self._current(), parent=self.form.app.root)
        if text is None:
            return
        try:
            self._store(C.parse_hotkey(text))
        except ValueError as e:
            messagebox.showerror("Tastenkombination", str(e), parent=self.form.app.root)


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
        super().__init__(app, f"{C.display_name(data)} bearbeiten")
        self.on_close = on_close
        body = ScrollFrame(self, style="Panel.TFrame", padding=12)
        body.pack(fill="both", expand=True)
        ActionForm(app, body.inner, data, page_uuid, slot, appearance=False).build()
        ttk.Button(self, text="Fertig", style="Accent.TButton", command=self.done).pack(anchor="e", padx=12,
                                                                                       pady=(0, 12))

    def done(self):
        super().done()
        if self.on_close:
            self.on_close()


class JsonDialog(_Dialog):
    def __init__(self, app, data, page_uuid, slot, on_close=None):
        super().__init__(app, "Einstellungen (JSON)")
        self.data, self.page_uuid, self.slot, self.on_close = data, page_uuid, slot, on_close
        ttk.Label(self, text="Rohdaten der Aktion („Settings“) – für Plugin-Aktionen und Sonderfälle.",
                  style="Muted.TLabel").pack(anchor="w", padx=12, pady=(12, 4))
        self.txt = tk.Text(self, bg=COL["panel2"], fg=COL["fg"], insertbackground=COL["fg"], relief="flat",
                           font=("monospace", 10))
        self.txt.insert("1.0", json.dumps(data.get("Settings") or {}, indent=2, ensure_ascii=False))
        self.txt.pack(fill="both", expand=True, padx=12)
        row = ttk.Frame(self, style="Panel.TFrame")
        row.pack(fill="x", padx=12, pady=12)
        ttk.Button(row, text="Übernehmen", style="Accent.TButton", command=self.apply).pack(side="right")
        ttk.Button(row, text="Abbrechen", command=self.done).pack(side="right", padx=6)

    def apply(self):
        try:
            val = json.loads(self.txt.get("1.0", "end-1c") or "{}")
            if not isinstance(val, dict):
                raise ValueError("Ein JSON-Objekt { … } wird erwartet")
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
        self.title("Neues Profil")
        self.configure(bg=COL["panel"])
        self.transient(app_root)
        self.result = None
        f = ttk.Frame(self, style="Panel.TFrame", padding=16)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Name", style="Panel.TLabel").grid(row=0, column=0, sticky="w", pady=4)
        self.name = tk.StringVar(value="Neues Profil")
        e = ttk.Entry(f, textvariable=self.name, width=34)
        e.grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(f, text="Gerät", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=4, padx=(0, 10))
        self.codes = list(MODELS)
        self.box = ttk.Combobox(f, state="readonly", width=32, values=[MODELS[c][0] for c in self.codes])
        self.box.current(self.codes.index("20GBA9901"))
        self.box.grid(row=1, column=1, sticky="ew", pady=4)
        row = ttk.Frame(f, style="Panel.TFrame")
        row.grid(row=2, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(row, text="Abbrechen", command=self.destroy).pack(side="right")
        ttk.Button(row, text="Anlegen", style="Accent.TButton", command=self.ok).pack(side="right", padx=6)
        self.bind("<Return>", lambda ev: self.ok())
        self.bind("<Escape>", lambda ev: self.destroy())
        e.focus_set()
        e.select_range(0, "end")
        self.wait_visibility()
        self.grab_set()
        self.wait_window()

    def ok(self):
        self.result = (self.name.get().strip() or "Neues Profil", self.codes[self.box.current()])
        self.destroy()


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
        root = tk.Tk(className="lindeck-editor")
    except tk.TclError as e:
        print(f"Keine grafische Oberfläche verfügbar: {e}")
        return 1
    EditorApp(root, store_root=store_root, profile=profile)
    root.mainloop()
    return 0
