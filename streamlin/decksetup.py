"""Einrichtung von LinDeck beim ersten Start: Gerät → Profile → fertig.

Erscheint, wenn es noch keine Profile gibt (und die Einrichtung nicht übersprungen wurde).
  1. Willkommen
  2. Gerät: angeschlossene Stream Decks (wird alle 2 s aktualisiert) oder Modell wählen
  3. Profile: aus der Elgato-App übernehmen (gefundene Dateien oder selbst wählen) oder leer anfangen
  4. Fertig – Hinweise zum Fenster, zu Verbindungen und Icons
"""
import os
import threading
import tkinter as tk
from tkinter import ttk

from . import editdoc as E
from . import msgbox as filedialog
from .dialogkit import draw_steps, entry
from .i18n import _, ngettext
from .models import MODELS, connected_models, model_name
from .profiles import import_path
from .theme import COL, Button, Card, Icons, gradient_mark, icon_tile, label

STEPS = [_("Welcome"), _("Device"), _("Profiles"), _("Done")]


def needed(app):
    """Assistent zeigen? Nur ohne Profile und wenn er nicht schon gelaufen ist."""
    from .lindecksettings import config_of
    if app.store.profiles or os.environ.get("STREAMLIN_NO_SETUP"):
        return False
    return not (config_of(app).get("lindeck") or {}).get("setup_done")


def find_profiles():
    """Profile in Downloads, auf dem Schreibtisch und auf USB-Sticks (wie der automatische Import)."""
    from . import autoimport
    found = []
    for base in autoimport.search_dirs():
        found += [p for kind, p in autoimport.candidates(base) if kind == "profile"]
    return found


def _search(result):
    try:
        found = find_profiles()
    except Exception:  # noqa: BLE001
        found = []
    result.append(found)


class DeckSetup(tk.Toplevel):
    def __init__(self, app, on_done=None):
        super().__init__(app.root)
        self.app, self.on_done = app, on_done
        self.title(_("Set up LinDeck"))
        self.configure(bg=COL["bg"])
        self.geometry("760x600")
        self.minsize(700, 560)
        self.transient(app.root)
        self.icons = Icons(self)
        self.step = 0
        self.model = tk.StringVar(self, value="")
        self.choice = tk.StringVar(self, value="import")
        self.name = tk.StringVar(self, value=_("My profile"))
        self.found = None          # gefundene Profil-Dateien (Suche im Hintergrund)
        self.picked = []           # selbst gewählte Dateien
        self.imported = []
        self._job = None
        result = self._search_result = []  # der Such-Thread bekommt nur diese Liste, nicht das Fenster
        threading.Thread(target=_search, args=(result,), daemon=True).start()

        head = tk.Frame(self, bg=COL["bg"], padx=22, pady=16)
        head.pack(fill="x")
        self.dots = tk.Canvas(head, height=26, bg=COL["bg"], highlightthickness=0)
        self.dots.pack(fill="x")
        tk.Frame(self, bg=COL["border"], height=1).pack(fill="x")
        foot = tk.Frame(self, bg=COL["bg"], padx=22, pady=14)
        foot.pack(fill="x", side="bottom")
        tk.Frame(self, bg=COL["border"], height=1).pack(fill="x", side="bottom")
        self.body = tk.Frame(self, bg=COL["bg"], padx=22, pady=22)
        self.body.pack(fill="both", expand=True)
        self.next_btn = Button(foot, _("Next"), kind="primary", command=self.next)
        self.next_btn.pack(side="right")
        self.back_btn = Button(foot, _("Back"), command=self.back)
        self.back_btn.pack(side="right", padx=8)
        self.skip_btn = Button(foot, _("Set up later"), kind="ghost", command=self.skip)
        self.skip_btn.pack(side="left")
        self.dots.bind("<Configure>", lambda e: draw_steps(self.dots, STEPS, self.step, self.icons))
        self.protocol("WM_DELETE_WINDOW", self.skip)
        self.render()

    # ------------------------------------------------------------ Rahmen
    def render(self):
        if self._job:
            self.after_cancel(self._job)
            self._job = None
        for w in self.body.winfo_children():
            w.destroy()
        draw_steps(self.dots, STEPS, self.step, self.icons)
        getattr(self, f"_step_{self.step}")()
        last = self.step == len(STEPS) - 1
        self.back_btn.configure_text(enabled=0 < self.step < len(STEPS) - 1)
        self.next_btn.configure_text(text=_("Finish") if last else (_("Let's go") if self.step == 0 else _("Next")),
                                     icon="check" if last else "")
        if last:
            self.skip_btn.pack_forget()

    def next(self):
        if self.step == 2:
            if not self.apply():
                return
        if self.step == len(STEPS) - 1:
            self.finish()
            return
        self.step += 1
        self.render()

    def back(self):
        if self.step > 0:
            self.step -= 1
            self.render()

    def _mark_done(self):
        from .lindecksettings import config_of
        cfg = config_of(self.app)
        cfg.data.setdefault("lindeck", {})["setup_done"] = True
        try:
            cfg.save()
        except OSError:
            pass

    def skip(self):
        self._mark_done()
        self.destroy()

    def finish(self):
        self._mark_done()
        self.destroy()
        if self.on_done:
            self.on_done(self.imported)

    def destroy(self):
        if self._job:
            try:
                self.after_cancel(self._job)
            except tk.TclError:
                pass
        super().destroy()

    def _title(self, title, text):
        label(self.body, title, 17, "bold").pack(anchor="w")
        label(self.body, text, 10, color=COL["muted"], wraplength=680, justify="left").pack(anchor="w",
                                                                                           pady=(4, 16))

    # ------------------------------------------------------------ Schritte
    def _step_0(self):
        b = self.body
        tk.Label(b, image=gradient_mark(b, 84, "deck"), bg=COL["bg"]).pack(pady=(40, 20))
        label(b, _("Welcome to LinDeck"), 20, "bold").pack()
        label(b, _("In two steps your deck is ready: LinDeck finds your device and takes over your profiles "
                   "from the Elgato app – or you start with an empty profile."),
              10, color=COL["muted"], wraplength=520, justify="center").pack(pady=(8, 0))

    def devices(self):
        """Angeschlossene Geräte: [(Modellcode, Titel, Untertitel)]."""
        out = []
        for c in self.app._controllers():
            out.append((c.model, model_name(c.model), _("Connected · serial no. {serial}").format(serial=c.serial)))
        if not out:
            for d in connected_models():
                out.append((d["model"], model_name(d["model"]), _("Connected")))
        return out

    def _step_1(self):
        self._title(_("Your device"), _("Plug in your deck. LinDeck detects it by itself; without a device you "
                                        "can choose the model for your first profile."))
        self.dev_box = tk.Frame(self.body, bg=COL["bg"])
        self.dev_box.pack(fill="x")
        self._devices_key = None
        self._refresh_devices()

    def _refresh_devices(self):
        devs = self.devices()
        key = tuple(d[0] for d in devs)
        if key != self._devices_key:
            self._devices_key = key
            for w in self.dev_box.winfo_children():
                w.destroy()
            if devs:
                if self.model.get() not in key:
                    self.model.set(devs[0][0])
                for code, title, sub in devs:
                    self._card(self.dev_box, title, sub, "deck", COL["ok"])
            else:
                card = Card(self.dev_box, pad=16, stretch=True)
                card.pack(fill="x")
                r = tk.Frame(card.body, bg=COL["panel"])
                r.pack(fill="x")
                tk.Label(r, image=icon_tile(r, "deck", 36, COL["raised"], fg=COL["muted"]),
                         bg=COL["panel"]).pack(side="left")
                t = tk.Frame(r, bg=COL["panel"])
                t.pack(side="left", padx=12)
                label(t, _("No device found yet"), 11, "bold").pack(anchor="w")
                label(t, _("Waiting for your deck … or choose the model:"), 9, color=COL["muted"]).pack(anchor="w")
                codes = list(dict.fromkeys(code for code, (_n, _g) in MODELS.items()))
                names = [model_name(c) for c in codes]
                if self.model.get() not in codes:
                    self.model.set("20GBA9901")
                combo = ttk.Combobox(card.body, state="readonly", width=32, values=names)
                combo.current(codes.index(self.model.get()))
                combo.bind("<<ComboboxSelected>>", lambda e: self.model.set(codes[combo.current()]))
                combo.pack(anchor="w", pady=(12, 0))
        self._job = self.after(2000, self._refresh_devices)

    def _card(self, master, title, sub, glyph, dot):
        card = Card(master, pad=14, stretch=True)
        card.pack(fill="x", pady=(0, 8))
        r = tk.Frame(card.body, bg=COL["panel"])
        r.pack(fill="x")
        tk.Label(r, image=icon_tile(r, glyph, 36, COL["device"], fg="#c9cfda"), bg=COL["panel"]).pack(side="left")
        t = tk.Frame(r, bg=COL["panel"])
        t.pack(side="left", padx=12)
        label(t, title, 11, "bold").pack(anchor="w")
        row = tk.Frame(t, bg=COL["panel"])
        row.pack(anchor="w")
        cv = tk.Canvas(row, width=8, height=8, bg=COL["panel"], highlightthickness=0)
        cv.create_oval(0, 0, 7, 7, fill=dot, outline="")
        cv.pack(side="left", padx=(0, 6))
        label(row, sub, 9, color=COL["muted"]).pack(side="left")

    def _step_2(self):
        self._title(_("Your profiles"), _("On Windows, copy the folder %APPDATA%\\Elgato\\StreamDeck\\ProfilesV3 "
                                          "to a USB stick – or export single profiles in the Elgato app."))
        self.opt_box = tk.Frame(self.body, bg=COL["bg"])
        self.opt_box.pack(fill="x")
        self._paint_options()

    def _option(self, key, title, text, glyph, extra=None):
        sel = self.choice.get() == key
        bg = COL["accent_soft"] if sel else COL["panel"]
        row = tk.Frame(self.opt_box, bg=bg, padx=14, pady=12, cursor="hand2", highlightthickness=1,
                       highlightbackground=COL["accent"] if sel else COL["border"])
        row.pack(fill="x", pady=(0, 10))
        tk.Label(row, image=icon_tile(row, glyph, 34, COL["accent"] if sel else COL["raised"],
                                      fg=COL["on_accent"] if sel else COL["muted"]), bg=bg).pack(side="left",
                                                                                             anchor="n")
        t = tk.Frame(row, bg=bg)
        t.pack(side="left", fill="x", expand=True, padx=12)
        label(t, title, 11, "bold", bg=bg).pack(anchor="w")
        label(t, text, 9, color=COL["muted"], bg=bg, wraplength=520, justify="left").pack(anchor="w", pady=(2, 0))
        if extra and sel:
            extra(t, bg)
        for w in (row, t, *row.winfo_children(), *t.winfo_children()):
            if not isinstance(w, (tk.Entry, Button)):
                w.bind("<Button-1>", lambda e, k=key: self._choose(k))

    def _choose(self, key):
        if self.choice.get() != key:
            self.choice.set(key)
            self._paint_options()

    def _paint_options(self):
        if self._job:
            self.after_cancel(self._job)
            self._job = None
        for w in self.opt_box.winfo_children():
            w.destroy()
        if self.found is None and self._search_result:
            self.found = self._search_result[0]
        found = self.found
        if found is None:
            text = _("Searching Downloads, desktop and USB sticks …")
        elif found:
            text = ngettext("Found {n} profile file – it will be imported.",
                            "Found {n} profile files – they will be imported.", len(found)).format(n=len(found))
        else:
            text = _("Nothing found in Downloads, on the desktop or on USB sticks. Choose the files yourself.")
        if self.picked:
            text = ngettext("{n} file chosen.", "{n} files chosen.", len(self.picked)).format(n=len(self.picked))

        def pick_btn(parent, bg):
            Button(parent, _("Choose files …"), "folder", command=self._pick).pack(anchor="w", pady=(8, 0))
        self._option("import", _("Take over from the Elgato app"), text, "import", pick_btn)

        def name_field(parent, bg):
            e = entry(parent, self.name, size=10)
            e.configure(width=30)
            e.box.pack(anchor="w", pady=(8, 0))
        self._option("new", _("Start with an empty profile"),
                     _("For the {model} – you fill it by dragging actions onto the keys.").format(
                         model=model_name(self.model.get() or "20GBA9901")), "plus", name_field)
        self.error = label(self.opt_box, "", 10, color=COL["danger"], bg=COL["bg"])
        self.error.pack(anchor="w")
        if self.found is None:
            self._job = self.after(300, self._paint_options)

    def _pick(self):
        files = filedialog.askopenfilenames(
            parent=self, title=_("Import profiles"),
            filetypes=[(_("Profiles (Elgato format)"), "*.streamDeckProfile *.streamDeckProfilesBackup *.zip"),
                       (_("All files"), "*")])
        if files:
            self.picked = list(files)
            self._paint_options()

    def apply(self):
        """Profile anlegen bzw. übernehmen. False: auf dieser Seite bleiben (Fehlermeldung steht da)."""
        store = self.app.store_root
        self.imported = []
        if self.choice.get() == "import":
            sources = self.picked or self.found or []
            if not sources:
                self.error.configure(text=_("Choose the profile files first – or start with an empty profile."))
                return False
            errors = []
            for f in sources:
                try:
                    self.imported += import_path(f, store)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{f}: {e}")
            if not self.imported:
                self.error.configure(text=errors[0] if errors else _("No profiles found in these files."))
                return False
        else:
            name = self.name.get().strip() or _("My profile")
            root = E.create_profile(name, self.model.get() or "20GBA9901", store)
            self.imported = [root.name[: -len(".sdProfile")]]
        return True

    def _step_3(self):
        b = self.body
        tk.Label(b, image=icon_tile(b, "check", 56, COL["ok"], radius=0.5), bg=COL["bg"]).pack(pady=(6, 12))
        label(b, _("All set!"), 18, "bold").pack()
        n = len(self.imported)
        label(b, ngettext("{n} profile is ready.", "{n} profiles are ready.", n).format(n=n), 10,
              color=COL["muted"]).pack(pady=(4, 16))
        card = Card(b, pad=18, stretch=True)
        card.pack(fill="x")
        tips = [("grid", _("Keys"), _("Drag an action from the list on the right onto a key – or double-click it. "
                                      "Right-click a key for copy, paste and delete.")),
                ("image", _("Icons"), _("“Icons …” below the key preview opens over 100 icons drawn for LinDeck.")),
                ("gear", _("Connections"), _("OBS, Discord and Home Assistant are set up under Settings → "
                                             "Connections."))]
        for i, (glyph, title, text) in enumerate(tips):
            if i:
                tk.Frame(card.body, bg=COL["border"], height=1).pack(fill="x", pady=10)
            r = tk.Frame(card.body, bg=COL["panel"])
            r.pack(fill="x")
            tk.Label(r, image=icon_tile(r, glyph, 32, COL["accent_soft"], fg=COL["accent"]),
                     bg=COL["panel"]).pack(side="left", anchor="n")
            t = tk.Frame(r, bg=COL["panel"])
            t.pack(side="left", padx=12, fill="x", expand=True)
            label(t, title, 11, "bold").pack(anchor="w")
            label(t, text, 9, color=COL["muted"], wraplength=560, justify="left").pack(anchor="w", pady=(2, 0))
