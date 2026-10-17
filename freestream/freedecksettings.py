"""Reiter der FreeDeck-Einstellungen: Geräte, Profile, Plugins, Stil (wie in der Elgato-App).

Jeder Reiter ist ein tk.Frame, den settingsui.SettingsDialog beim ersten Öffnen baut.
Alles wird in config.json gespeichert; der laufende Dienst übernimmt Änderungen sofort.
"""
import threading
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk

from . import msgbox as messagebox
from . import plugins, system, virtualdeck
from .dialogkit import ScrollArea, ShortcutField, entry
from .i18n import _, ngettext
from .models import VIRTUAL_MODELS, VIRTUAL_SIZES, VIRTUAL_UPRIGHT, model_name
from .theme import COL, Button, Card, Fader, Icons, Segmented, Switch, font, icon_tile, label, rounded_rect, section

SLEEP_CHOICES = [0, 1, 5, 10, 15, 30, 60]
KEY_STYLE_DEFAULT = {"ShowTitle": True, "TitleAlignment": "bottom", "FontFamily": "", "FontSize": 12,
                     "FontStyle": "", "FontUnderline": False, "TitleColor": "#ffffff"}
DIAL_STYLE_DEFAULT = {"ShowTitle": True, "TitleColor": "#ffffff"}


def config_of(freedeck):
    """Die Konfiguration des laufenden Dienstes (gleiches Programm) oder eine eigene."""
    daemon = getattr(freedeck, "daemon", None)
    if daemon is not None and hasattr(getattr(daemon, "cfg", None), "device"):
        return daemon.cfg
    cfg = getattr(freedeck, "_settings_cfg", None)
    if cfg is None:
        from .config import Config
        cfg = freedeck._settings_cfg = Config()
    return cfg


def device_title(cfg, ctrl):
    """Name eines Geräts: selbst vergeben (Einstellungen › Geräte) oder der Modellname."""
    return (cfg.device(ctrl.serial).get("name") or "").strip() or model_name(ctrl.model)


def _fill(card):
    """Inhalt einer Karte auf die ganze Kartenfläche ziehen (Knöpfe unten bleiben unten)."""
    def fit(e):
        card.itemconfigure(card._win, width=max(1, e.width - 2 * card.pad), height=max(1, e.height - 2 * card.pad))
    card.bind("<Configure>", fit, add="+")
    return card


def sleep_text(minutes):
    return _("Never") if not minutes else ngettext("{n} minute", "{n} minutes", minutes).format(n=minutes)


class _Page(tk.Frame):
    def __init__(self, master, dialog, freedeck):
        super().__init__(master, bg=COL["bg"])
        self.dialog, self.freedeck = dialog, freedeck
        self.cfg = config_of(freedeck)
        self.icons = Icons(self)
        self._save_job = None

    def save_later(self, delay=500):
        """Speichern gesammelt (z. B. beim Ziehen am Helligkeitsregler)."""
        if self._save_job:
            self.after_cancel(self._save_job)
        self._save_job = self.after(delay, self.save_now)

    def save_now(self):
        self._save_job = None
        try:
            self.cfg.save()
        except OSError as e:
            messagebox.showerror(_("Settings"), str(e), parent=self)

    def destroy(self):
        if self._save_job:
            self.after_cancel(self._save_job)
            self.save_now()
        super().destroy()

    def panel(self, master, **pack):
        card = Card(master, pad=14, stretch=True)
        card.pack(**pack)
        if pack.get("expand"):
            _fill(card)
        return card.body

    def row(self, master, text, pady=8, width=18, top=False):
        r = tk.Frame(master, bg=COL["panel"])
        r.pack(fill="x", pady=(0, pady))
        label(r, text, 10, color=COL["muted"], width=width, anchor="ne" if top else "e").pack(
            side="left", padx=(0, 14), anchor="n" if top else "center", pady=(12, 0) if top else 0)
        return r


# ===================================================================== Geräte
class DevicesPage(_Page):
    def __init__(self, master, dialog, freedeck):
        super().__init__(master, dialog, freedeck)
        self.serial = getattr(freedeck, "device", None)
        left = Card(self, pad=12, width=230)
        left.pack(side="left", fill="y", padx=(0, 12))
        _fill(left)
        section(left.body, _("Connected")).pack(anchor="w", pady=(0, 8))
        self.listbox = tk.Frame(left.body, bg=COL["panel"])
        self.listbox.pack(fill="x")
        section(left.body, _("Virtual")).pack(anchor="w", pady=(14, 8))
        self.vbox = tk.Frame(left.body, bg=COL["panel"])
        self.vbox.pack(fill="x")
        self.right = tk.Frame(self, bg=COL["bg"])
        self.right.pack(side="left", fill="both", expand=True)
        self.render()

    def controllers(self):
        """Angeschlossene Geräte (ohne das virtuelle Deck)."""
        return sorted((c for c in self.freedeck._controllers() if not getattr(c, "virtual", False)),
                      key=lambda c: (model_name(c.model), c.serial))

    def virtual_controller(self):
        return next((c for c in self.freedeck._controllers() if getattr(c, "virtual", False)), None)

    def render(self):
        for box in (self.listbox, self.vbox, self.right):
            for w in box.winfo_children():
                w.destroy()
        ctrls = self.controllers()
        if self.serial != virtualdeck.SERIAL and self.serial not in [c.serial for c in ctrls]:
            self.serial = ctrls[0].serial if ctrls else virtualdeck.SERIAL
        img = icon_tile(self, "deck", 32, COL["device"], radius=0.24, fg="#c9cfda")
        from .theme import InfoCard
        self.cards = {}
        for c in ctrls:
            card = InfoCard(self.listbox, device_title(self.cfg, c), _("Connected"), image=img, dot=COL["ok"],
                            command=lambda s=c.serial: self.select(s))
            card.selected = c.serial == self.serial
            card.pack(fill="x", pady=(0, 6))
            self.cards[c.serial] = card
        if not ctrls:
            InfoCard(self.listbox, _("No device connected"), _("Plug in your deck – its settings appear here."),
                     image=icon_tile(self, "deck", 32, COL["raised"], radius=0.24, fg=COL["muted"]),
                     dim=True).pack(fill="x", pady=(0, 6))
        s = virtualdeck.settings(self.cfg)
        self.vcard = InfoCard(self.vbox, _("Virtual deck"), self._virtual_status(s),
                              image=icon_tile(self, "grid", 32, COL["accent"], radius=0.24),
                              dot=COL["ok"] if s["enabled"] else None,
                              command=lambda: self.select(virtualdeck.SERIAL))
        self.vcard.selected = self.serial == virtualdeck.SERIAL
        self.vcard.pack(fill="x", pady=(0, 6))
        if self.serial == virtualdeck.SERIAL:
            self.build_virtual_form()
        else:
            self.build_form(next(c for c in ctrls if c.serial == self.serial))

    @staticmethod
    def _virtual_status(s):
        if not s["enabled"]:
            return _("Off")
        return _("On · Window") if s["mode"] == "window" else _("On · Screen edge")

    def select(self, serial):
        self.serial = serial
        self.render()

    def build_form(self, ctrl):
        dcfg = self.cfg.device(ctrl.serial)
        body = self.panel(self.right, fill="both", expand=True)
        self.ctrl = ctrl
        # Name und aktiv
        r = self.row(body, _("Device name"))
        self.name = tk.StringVar(self, value=dcfg.get("name") or model_name(ctrl.model))
        ent = entry(r, self.name)
        ent.configure(width=16)
        ent.box.pack(side="left")
        self.name.trace_add("write", lambda *a: self._set_name())
        self.enabled = Switch(r, command=self._set_enabled)
        self.enabled.set(dcfg.get("enabled", True) is not False)
        self.enabled.pack(side="left", padx=(14, 6))
        label(r, _("Active"), 10).pack(side="left")
        # Ausrichtung
        r = self.row(body, _("Orientation"))
        self.rotation = ttk.Combobox(r, state="readonly", width=24,
                                     values=[_("Standard"), _("Upside down (180°)")])
        self.rotation.current(1 if dcfg.get("rotation") == 180 else 0)
        self.rotation.bind("<<ComboboxSelected>>", lambda e: self._set_rotation())
        self.rotation.pack(side="left")
        # Ruhezustand
        r = self.row(body, _("Sleep after"))
        cur = int(dcfg.get("sleep_after_minutes", self.cfg.get("sleep_after_minutes", 0)) or 0)
        choices = SLEEP_CHOICES if cur in SLEEP_CHOICES else sorted(SLEEP_CHOICES + [cur])
        self.sleep_values = choices
        self.sleep = ttk.Combobox(r, state="readonly", width=24, values=[sleep_text(m) for m in choices])
        self.sleep.current(choices.index(cur))
        self.sleep.bind("<<ComboboxSelected>>", lambda e: self._set_sleep())
        self.sleep.pack(side="left")
        # Helligkeit
        r = self.row(body, _("Brightness"), pady=14)
        tk.Label(r, image=self.icons.get("sun", 16, COL["muted"]), bg=COL["panel"]).pack(side="left", padx=(0, 8))
        self.bright_val = tk.StringVar(self, value=f"{ctrl.brightness} %")
        self.bright = Fader(r, length=260, thickness=22, orient="horizontal", value=ctrl.brightness,
                            command=self._set_brightness)
        self.bright.pack(side="left")
        label(r, textvariable=self.bright_val, size=9, color=COL["muted"], width=6, anchor="e").pack(side="left")
        # Bild, solange FreeDeck nicht läuft (statt des Logos des Geräts)
        r = self.row(body, _("When not running"))
        self.idle_preview = tk.Label(r, bg=COL["panel"])
        self.idle_preview.pack(side="left", padx=(0, 10))
        Button(r, _("Image…"), icon="image", command=self._pick_idle).pack(side="left")
        self.idle_remove = Button(r, _("Remove"), kind="ghost", command=lambda: self._set_idle(None))
        self.idle_remove.pack(side="left", padx=(4, 0))
        self._show_idle()
        # Info
        r = self.row(body, _("Model"))
        label(r, model_name(ctrl.model), 10).pack(side="left")
        r = self.row(body, _("Serial number"))
        label(r, ctrl.serial, 10).pack(side="left")
        r = self.row(body, _("Firmware"))
        label(r, getattr(ctrl, "firmware", "") or "–", 10).pack(side="left")
        # Profil
        r = self.row(body, _("Profile"), pady=0)
        cands = ctrl.candidate_profiles()
        self.profile_ids = [p.id for p in cands]
        self.profile = ttk.Combobox(r, state="readonly", width=24, values=[p.name for p in cands])
        if ctrl.profile and ctrl.profile.id in self.profile_ids:
            self.profile.current(self.profile_ids.index(ctrl.profile.id))
        self.profile.bind("<<ComboboxSelected>>", lambda e: self._set_profile())
        self.profile.pack(side="left")

    # ----------------------------------------------------------- virtuelles Deck
    def build_virtual_form(self):
        """Virtuelles Deck: an/aus, Darstellung (Fenster oder Randleiste), Größe, Kürzel, Profil."""
        s = virtualdeck.settings(self.cfg)
        area = ScrollArea(self.panel(self.right, fill="both", expand=True), bg=COL["panel"])
        area.pack(fill="both", expand=True)
        body = area.inner
        w = 14
        r = self.row(body, _("Virtual deck"), width=w)
        self.v_on = Switch(r, command=lambda: self._v_set(enabled=self.v_on.get(), visible=True))
        self.v_on.set(s["enabled"])
        self.v_on.pack(side="left")
        label(r, _("On – usable without a connected device"), 9).pack(side="left", padx=(10, 0))
        r = self.row(body, _("Display"), pady=14, width=w, top=True)
        self.v_modes = {}
        for mode, title, sub in (("window", _("Floating window"), _("move it anywhere, stays on top")),
                                 ("edge", _("Edge bar"), _("slides in, mouse to the edge brings it"))):
            card = ModeCard(r, mode, title, sub, command=lambda m=mode: self._v_set(mode=m))
            card.pack(side="left", padx=(0, 8))
            self.v_modes[mode] = card
        r = self.row(body, _("Size"), width=w)
        self.v_codes = list(VIRTUAL_SIZES)
        self.v_size = ttk.Combobox(r, state="readonly", width=24)
        self.v_size.bind("<<ComboboxSelected>>", lambda e: self._v_set(model=self.v_codes[self.v_size.current()]))
        self.v_size.pack(side="left")
        r = self.row(body, _("Always on top"), width=w)
        self.v_top = Switch(r, command=lambda: self._v_set(topmost=self.v_top.get()))
        self.v_top.set(s["topmost"])
        self.v_top.pack(side="left")
        r = self.row(body, _("Screen edge"), width=w)
        self.v_edge = ttk.Combobox(r, state="readonly", width=24, values=[_("Right"), _("Left")])
        self.v_edge.current(1 if s["edge"] == "left" else 0)
        self.v_edge.bind("<<ComboboxSelected>>",
                         lambda e: self._v_set(edge="left" if self.v_edge.current() == 1 else "right"))
        self.v_edge.pack(side="left")
        self._hint(body, _("only for the edge bar"), w)
        r = self.row(body, _("Show/hide"), width=w)
        self.v_key = ShortcutField(r, s["shortcut"], on_set=self._v_shortcut,
                                   on_remove=lambda: self._v_shortcut(""), width=140)
        self.v_key.pack(side="left")
        self._hint(body, _("shortcut, works everywhere"), w)
        r = self.row(body, _("Profile"), width=w)
        self.v_profile = ttk.Combobox(r, state="readonly", width=24)
        self.v_profile.bind("<<ComboboxSelected>>", lambda e: self._v_profile())
        self.v_profile.pack(side="left")
        self._hint(body, _("You assign the keys like on a real deck: choose “Virtual deck” on the left of the "
                           "FreeDeck window and drag actions onto it."), w, color=COL["muted"], pady=(10, 0))
        self._v_refresh()

    @staticmethod
    def _hint(body, text, width, color=None, pady=(0, 8)):
        """Kleine graue Zeile unter einem Feld (eingerückt wie die Felder)."""
        f = tk.Frame(body, bg=COL["panel"])
        f.pack(fill="x", pady=pady)
        label(f, "", 10, width=width).pack(side="left", padx=(0, 14))
        label(f, text, 8, color=color or COL["faint"], wraplength=330, justify="left").pack(side="left")

    def _v_refresh(self):
        """Felder ein-/ausgrauen und die Profilliste des laufenden virtuellen Decks zeigen."""
        s = virtualdeck.settings(self.cfg)
        on = s["enabled"]
        for mode, card in self.v_modes.items():
            card.set(selected=mode == s["mode"], enabled=on)
        # Randleiste: hochkant (2 × 4 statt 4 × 2) – so steht sie auch im Editor
        geos = [VIRTUAL_MODELS[VIRTUAL_UPRIGHT[c] if s["mode"] == "edge" else c] for c in self.v_codes]
        self.v_size.configure(values=[_("{cols} × {rows} keys").format(cols=g.cols, rows=g.rows) for g in geos],
                              state="readonly" if on else "disabled")
        self.v_size.current(self.v_codes.index(s["model"]))
        self.v_top.set_enabled(on and s["mode"] == "window")
        self.v_edge.configure(state="readonly" if on and s["mode"] == "edge" else "disabled")
        ctrl = self.virtual_controller()
        cands = ctrl.candidate_profiles() if ctrl is not None else []
        self.v_profile_ids = [p.id for p in cands]
        self.v_profile.configure(values=[p.name for p in cands], state="readonly" if ctrl is not None else "disabled")
        if ctrl is not None and ctrl.profile and ctrl.profile.id in self.v_profile_ids:
            self.v_profile.current(self.v_profile_ids.index(ctrl.profile.id))
        else:
            self.v_profile.set("")
        if getattr(self, "vcard", None) is not None:
            self.vcard.set(subtitle=self._virtual_status(s), dot=COL["ok"] if on else "")

    def _v_set(self, **changes):
        virtualdeck.update(self.cfg, **changes)
        self.save_now()
        daemon = getattr(self.freedeck, "daemon", None)
        if hasattr(daemon, "sync_virtual") and {"enabled", "model", "mode"} & set(changes):
            try:
                daemon.sync_virtual()  # gleich anlegen/ändern statt erst im nächsten Takt
            except Exception as e:  # noqa: BLE001
                messagebox.showerror(_("Virtual deck"), str(e), parent=self)
            self.freedeck._device_key = None
            try:
                self.freedeck.refresh_devices()
            except Exception:  # noqa: BLE001
                pass
        self._v_refresh()

    def _v_profile(self):
        ctrl = self.virtual_controller()
        if ctrl is None or not self.v_profile_ids:
            return
        pid = self.v_profile_ids[self.v_profile.current()]
        self.cfg.device(ctrl.serial)["profile"] = pid
        self.save_now()
        threading.Thread(target=ctrl.load_profile, args=(pid,), daemon=True).start()

    def _v_shortcut(self, binding):
        """Globales Tastenkürzel zum Ein-/Ausblenden beim Desktop eintragen (wie die Szenen in FreeWave)."""
        from . import shortcuts
        virtualdeck.update(self.cfg, shortcut=binding)
        self.save_now()
        result = {}

        def work():
            try:
                if binding:
                    shortcuts.register(virtualdeck.SHORTCUT_ID, _("FreeDeck: show/hide virtual deck"),
                                       shortcuts.freedeck_command("virtual-deck"), binding)
                else:
                    shortcuts.unregister(virtualdeck.SHORTCUT_ID)
            except Exception as e:  # noqa: BLE001
                result["error"] = e
            result["done"] = True

        def check():
            if not result.get("done"):
                self.after(100, check)
                return
            err = result.get("error")
            if isinstance(err, shortcuts.Unsupported):
                from .dialogkit import ManualShortcutDialog
                ManualShortcutDialog(self, str(err), err.command)
            elif err is not None:
                messagebox.showerror(_("Keyboard shortcut"), str(err), parent=self)
        threading.Thread(target=work, daemon=True).start()
        self.after(100, check)

    # ----------------------------------------------------------- Änderungen
    def _dcfg(self):
        return self.cfg.device(self.ctrl.serial)

    def _set_name(self):
        name = self.name.get().strip()
        d = self._dcfg()
        if name and name != model_name(self.ctrl.model):
            d["name"] = name
        else:
            d.pop("name", None)
        card = self.cards.get(self.ctrl.serial)
        if card is not None:
            card.set(title=device_title(self.cfg, self.ctrl))
        self.freedeck._device_key = None
        self.freedeck.build_devices()
        self.save_later()

    def _set_enabled(self):
        on = self.enabled.get()
        self._dcfg()["enabled"] = on
        self.ctrl.set_enabled(on)
        self.save_now()

    def _set_rotation(self):
        deg = 180 if self.rotation.current() == 1 else 0
        self._dcfg()["rotation"] = deg
        threading.Thread(target=self.ctrl.set_rotation, args=(deg,), daemon=True).start()
        self.save_now()

    def _set_sleep(self):
        self._dcfg()["sleep_after_minutes"] = self.sleep_values[self.sleep.current()]
        self.save_now()

    def _set_brightness(self, value):
        v = int(round(float(value)))
        self.bright_val.set(f"{v} %")
        self._dcfg()["brightness"] = v
        self.ctrl.set_brightness(v)
        self.save_later()

    def _show_idle(self):
        """Kleine Vorschau des Bilds für „FreeDeck läuft nicht“ (sonst ein Hinweis)."""
        path = self.ctrl.idle_image_path() if hasattr(self.ctrl, "idle_image_path") else None
        if path is None:
            self._idle_photo = None
            self.idle_preview.configure(image="", text=_("Device logo"), fg=COL["muted"], font=font(self, 9))
        else:
            from PIL import ImageOps
            try:
                with Image.open(path) as im:
                    thumb = ImageOps.fit(im.convert("RGB"), (48, 28), Image.LANCZOS)
                self._idle_photo = ImageTk.PhotoImage(thumb, master=self)
                self.idle_preview.configure(image=self._idle_photo, text="")
            except OSError:
                self.idle_preview.configure(image="", text=path.name)
        self.idle_remove.configure_text(enabled=path is not None)

    def _pick_idle(self):
        f = messagebox.askopenfilename(parent=self, title=_("Image when FreeDeck is not running"), filetypes=[
            (_("Images"), "*.png *.jpg *.jpeg *.webp *.gif *.bmp"), (_("All files"), "*")])
        if f:
            self._set_idle(f)

    def _set_idle(self, src):
        """Bild in den Konfigurationsordner kopieren (die Quelle darf danach verschwinden)."""
        from PIL import ImageOps
        from . import paths
        d = self._dcfg()
        dst = paths.CONFIG_DIR / "idle" / f"{self.ctrl.serial}.png"
        if src is None:
            d.pop("idle_image", None)
            dst.unlink(missing_ok=True)
        else:
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                with Image.open(src) as im:
                    im = ImageOps.exif_transpose(im).convert("RGB")
                    im.thumbnail((1600, 1600), Image.LANCZOS)
                    im.save(dst)
            except (OSError, ValueError) as e:
                messagebox.showerror(_("Settings"), _("Image not readable: {error}").format(error=e), parent=self)
                return
            d["idle_image"] = str(dst)
        self.save_now()
        self._show_idle()

    def _set_profile(self):
        pid = self.profile_ids[self.profile.current()]
        self._dcfg()["profile"] = pid
        self.save_now()
        threading.Thread(target=self.ctrl.load_profile, args=(pid,), daemon=True).start()


class ModeCard(tk.Canvas):
    """Darstellung des virtuellen Decks zur Wahl: kleine Vorschau, Auswahlpunkt, Titel, Unterzeile."""

    W, H = 166, 138

    def __init__(self, master, mode, title, sub, command=None):
        super().__init__(master, width=self.W, height=self.H, bg=COL["panel"], highlightthickness=0, bd=0,
                         cursor="hand2")
        self.mode, self.title, self.sub, self.command = mode, title, sub, command
        self.selected, self.enabled = False, True
        self.f_title, self.f_sub = font(self, 9, "bold"), font(self, 8)
        self.bind("<Button-1>", lambda e: self.enabled and self.command and self.command())
        self._draw()

    def set(self, selected=None, enabled=None):
        if selected is not None:
            self.selected = selected
        if enabled is not None:
            self.enabled = enabled
            self.configure(cursor="hand2" if enabled else "")
        self._draw()

    def _draw(self):
        self.delete("all")
        W, H = self.W, self.H
        sel = self.selected and self.enabled
        rounded_rect(self, 1, 1, W - 2, H - 2, 12, fill=COL["accent_soft"] if sel else COL["panel2"],
                     outline=COL["accent"] if sel else COL["border"], width=2 if sel else 1)
        # Vorschau: Bildschirm mit Deck
        x0, y0, x1, y1 = 10, 10, W - 10, 62
        rounded_rect(self, x0, y0, x1, y1, 6, fill=COL["raised"], outline="")
        colors = ["#3b82f6", "#ef4444", "#a855f7", "#ef4444", "#3b82f6", "#ec4899", "#a855f7", "#64748b"]
        if not self.enabled:
            colors = [COL["faint"]] * 8
        t, g = 11, 3
        if self.mode == "window":
            dx, dy = x1 - 4 * t - 3 * g - 18, y1 - 2 * t - g - 10
            self.create_rectangle(dx - 6, dy - 6, dx + 4 * t + 3 * g + 6, dy + 2 * t + g + 6, fill="#16181e",
                                  outline="")
            for i, c in enumerate(colors):
                cx, cy = dx + (i % 4) * (t + g), dy + (i // 4) * (t + g)
                rounded_rect(self, cx, cy, cx + t, cy + t, 3, fill=c, outline="")
        else:
            t, g = 9, 2
            dx, dy = x1 - 2 * t - g - 5, y0 + 4
            self.create_rectangle(dx - 5, dy - 3, x1, dy + 4 * t + 3 * g + 3, fill="#16181e", outline="")
            for i, c in enumerate(colors):
                cx, cy = dx + (i % 2) * (t + g), dy + (i // 2) * (t + g)
                rounded_rect(self, cx, cy, cx + t, cy + t, 3, fill=c, outline="")
        # Auswahlpunkt und Text
        cy = 80
        self.create_oval(10, cy - 6, 22, cy + 6, outline=COL["accent"] if sel else COL["muted"], width=2)
        if sel:
            self.create_oval(13, cy - 3, 19, cy + 3, fill=COL["accent"], outline="")
        fg = COL["fg"] if self.enabled else COL["faint"]
        self.create_text(28, cy, anchor="w", text=self.title, font=self.f_title, fill=fg)
        self.create_text(12, cy + 14, anchor="nw", text=self.sub, font=self.f_sub, width=W - 22,
                         fill=COL["muted"] if self.enabled else COL["faint"])


# ===================================================================== Profile
class ProfilesPage(_Page):
    def __init__(self, master, dialog, freedeck):
        super().__init__(master, dialog, freedeck)
        self.selected = freedeck.doc.id if getattr(freedeck, "doc", None) else None
        self.apps = None
        left = Card(self, pad=10, width=270)
        left.pack(side="left", fill="y", padx=(0, 12))
        _fill(left)
        self.tree_area = ScrollArea(left.body, bg=COL["panel"])
        self.tree_area.pack(fill="both", expand=True)
        bar = tk.Frame(left.body, bg=COL["panel"])
        bar.pack(fill="x", pady=(8, 0))
        Button(bar, "", "plus", kind="ghost", command=self._new).pack(side="left")
        Button(bar, "", "trash", kind="ghost", command=self._delete).pack(side="left", padx=(4, 0))
        self.right = tk.Frame(self, bg=COL["bg"])
        self.right.pack(side="left", fill="both", expand=True)
        self.render()

    def profiles(self):
        store = self.freedeck.store
        store.reload()
        return sorted(store.profiles.values(), key=lambda p: p.name.lower())

    def mapping(self):
        return self.cfg.data.setdefault("smart_profiles", {})

    def render(self):
        tree = self.tree_area.inner
        for w in tree.winfo_children():
            w.destroy()
        profs = self.profiles()
        if profs and self.selected not in [p.id for p in profs]:
            self.selected = profs[0].id
        default = self.cfg.get("default_profile") or ""
        for p in profs:
            sel = p.id == self.selected
            bg = COL["accent"] if sel else COL["panel"]
            row = tk.Frame(tree, bg=bg, cursor="hand2")
            row.pack(fill="x", pady=(0, 2))
            tk.Label(row, image=self.icons.get("chevron", 11, "#ffffff" if sel else COL["muted"]), bg=bg).pack(
                side="left", padx=(8, 6), pady=6)
            name = tk.Label(row, text=p.name, bg=bg, fg="#ffffff" if sel else COL["fg"], anchor="w",
                            font=_font(self, 10, "bold" if sel else "normal"))
            name.pack(side="left", fill="x", expand=True)
            if p.id == default:
                tk.Label(row, text="★", bg=bg, fg="#ffd166", font=_font(self, 10)).pack(side="right", padx=8)
            for w in (row, name):
                w.bind("<Button-1>", lambda e, pid=p.id: self.select(pid))
            for i, pg in enumerate(p.page_ids, start=1):
                sub = tk.Frame(tree, bg=COL["panel"])
                sub.pack(fill="x")
                box = tk.Label(sub, text=str(i), bg=COL["panel"], fg=COL["muted"], font=_font(self, 8),
                               width=2, highlightthickness=1, highlightbackground=COL["muted"])
                box.pack(side="left", padx=(30, 8), pady=3)
                label(sub, _("Page {n}").format(n=i), 9, color=COL["muted"]).pack(side="left")
        for w in self.right.winfo_children():
            w.destroy()
        p = self.freedeck.store.get(self.selected) if self.selected else None
        if p is not None:
            self.build_detail(p)

    def select(self, pid):
        self.selected = pid
        self.render()

    def build_detail(self, p):
        body = self.panel(self.right, fill="both", expand=True)
        label(body, _("Switch automatically"), 11, "bold").pack(anchor="w")
        label(body, _("FreeDeck switches to this profile as soon as the chosen program is in the foreground."),
              9, color=COL["muted"], wraplength=420, justify="left").pack(anchor="w", pady=(4, 14))
        r = tk.Frame(body, bg=COL["panel"])
        r.pack(fill="x")
        label(r, _("Program"), 10, color=COL["muted"]).pack(side="left", padx=(0, 12))
        self.apps = self.apps if self.apps is not None else _installed_apps()
        names = [_("None")] + [n for n, _c in self.apps]
        self.program = ttk.Combobox(r, values=names, width=34)
        cur = next((k for k, v in self.mapping().items() if v == p.id), "")
        shown = next((n for n, c in self.apps if c == cur), cur) if cur else names[0]
        self.program.set(shown)
        self.program.bind("<<ComboboxSelected>>", lambda e: self._set_program())
        self.program.bind("<Return>", lambda e: self._set_program())
        self.program.bind("<FocusOut>", lambda e: self._set_program())
        self.program.pack(side="left", fill="x", expand=True)
        if not cur and p.app_identifier:
            label(body, _("From the imported profile: {app}").format(app=p.app_identifier), 9,
                  color=COL["faint"]).pack(anchor="w", pady=(6, 0))
        self.pick_btn = Button(body, _("Use active window"), "search", command=self._pick_window)
        self.pick_btn.pack(anchor="w", pady=(10, 0))
        self.pick_status = label(body, "", 9, color=COL["muted"])
        self.pick_status.pack(anchor="w", pady=(4, 0))
        tk.Frame(body, bg=COL["border"], height=1).pack(fill="x", pady=16)
        r = tk.Frame(body, bg=COL["panel"])
        r.pack(fill="x")
        self.default = Switch(r, command=self._set_default)
        self.default.set(self.cfg.get("default_profile") == p.id)
        self.default.pack(side="left")
        label(r, _("This is my default profile"), 10).pack(side="left", padx=10)
        label(body, _("It appears whenever the program in the foreground has no profile of its own."), 9,
              color=COL["muted"], wraplength=420, justify="left").pack(anchor="w", pady=(8, 0))

    # ----------------------------------------------------------- Änderungen
    def set_program_class(self, cls):
        m = self.mapping()
        for k in [k for k, v in m.items() if v == self.selected]:
            m.pop(k)
        if cls:
            m[cls.lower()] = self.selected
        self.save_now()

    def _set_program(self):
        text = self.program.get().strip()
        if not text or text == _("None"):
            self.set_program_class("")
            return
        cls = next((c for n, c in self.apps if n == text), text)
        self.set_program_class(cls)

    def _pick_window(self):
        """Nach 3 s das Fenster im Vordergrund übernehmen (so lange zum gewünschten Programm wechseln)."""
        self.pick_btn.configure_text(enabled=False)

        def count(n):
            if not self.winfo_exists():
                return
            if n:
                self.pick_status.configure(text=_("Switch to the program now … {n}").format(n=n))
                self.after(1000, count, n - 1)
                return
            cls = system.active_window_class()
            self.pick_btn.configure_text(enabled=True)
            if not cls or cls in ("freedeck", "freestream", "lindeck", "streamlin"):
                self.pick_status.configure(text=_("No other program was in the foreground."))
                return
            self.pick_status.configure(text=_("Program: {app}").format(app=cls))
            self.program.set(next((n for n, c in self.apps or [] if c == cls), cls))
            self.set_program_class(cls)
        count(3)

    def _set_default(self):
        if self.default.get():
            self.cfg.data["default_profile"] = self.selected
        elif self.cfg.get("default_profile") == self.selected:
            self.cfg.data.pop("default_profile", None)
        self.save_now()
        self.render()

    def _new(self):
        self.freedeck.new_profile()
        if self.freedeck.doc:
            self.selected = self.freedeck.doc.id
        self.render()

    def _delete(self):
        if not self.selected:
            return
        if not (self.freedeck.doc and self.freedeck.doc.id == self.selected):
            self.freedeck.open_profile(self.selected)
        self.freedeck.delete_profile()
        if self.freedeck.store.get(self.selected) is None:
            m = self.mapping()
            for k in [k for k, v in m.items() if v == self.selected]:
                m.pop(k)
            if self.cfg.get("default_profile") == self.selected:
                self.cfg.data.pop("default_profile", None)
            self.save_now()
            self.selected = None
        self.render()


def _installed_apps():
    from . import desktop
    try:
        return desktop.installed_apps()
    except Exception:  # noqa: BLE001
        return []


def _font(master, size, weight="normal"):
    from .theme import font
    return font(master, size, weight)


# ===================================================================== Plugins
class PluginsPage(_Page):
    def __init__(self, master, dialog, freedeck):
        super().__init__(master, dialog, freedeck)
        self.host = getattr(getattr(freedeck, "daemon", None), "plugin_host", None)
        self.selected = None
        self._photos = []
        left = Card(self, pad=12, width=270)
        left.pack(side="left", fill="y", padx=(0, 12))
        _fill(left)
        section(left.body, _("Installed")).pack(anchor="w", pady=(0, 8))
        self.listbox = ScrollArea(left.body, bg=COL["panel"])
        self.listbox.pack(fill="both", expand=True)
        Button(left.body, _("Install plugin …"), "plus", command=self._install).pack(anchor="w", pady=(8, 0))
        self.right = tk.Frame(self, bg=COL["bg"])
        self.right.pack(side="left", fill="both", expand=True)
        self.render()
        self._tick()

    def render(self):
        from .pluginui import _plugin_icon, _rounded
        for w in self.listbox.inner.winfo_children():
            w.destroy()
        for w in self.right.winfo_children():
            w.destroy()
        self._photos = []
        found = sorted(plugins.installed().values(), key=lambda i: i.name.lower())
        if found and self.selected not in [i.uuid for i in found]:
            self.selected = found[0].uuid
        self.rows = {}
        for info in found:
            sel = info.uuid == self.selected
            bg = COL["raised"] if sel else COL["panel"]
            row = tk.Frame(self.listbox.inner, bg=bg, cursor="hand2")
            row.pack(fill="x", pady=(0, 4))
            img = _plugin_icon(info, 30)
            if img is not None:
                ph = ImageTk.PhotoImage(_rounded(img, 7), master=self)
                self._photos.append(ph)
                ic = tk.Label(row, image=ph, bg=bg)
            else:
                ic = tk.Label(row, image=icon_tile(row, "puzzle", 30, COL["accent"]), bg=bg)
            ic.pack(side="left", padx=8, pady=6)
            txt = tk.Frame(row, bg=bg)
            txt.pack(side="left", fill="x", expand=True)
            n = tk.Label(txt, text=info.name, bg=bg, fg=COL["fg"], anchor="w", font=_font(self, 10, "bold"))
            n.pack(anchor="w")
            st = tk.Label(txt, text="", bg=bg, fg=COL["muted"], anchor="w", font=_font(self, 8))
            st.pack(anchor="w")
            self.rows[info.uuid] = (info, st)
            for w in (row, ic, txt, n, st):
                w.bind("<Button-1>", lambda e, u=info.uuid: self.select(u))
        if not found:
            body = self.panel(self.right, fill="both", expand=True)
            tk.Label(body, image=icon_tile(body, "puzzle", 44, COL["raised"], fg=COL["muted"]),
                     bg=COL["panel"]).pack(pady=(40, 10))
            label(body, _("No plugins installed yet"), 12, "bold").pack()
            label(body, _("Elgato plugins from the Marketplace also run on Linux if they are built with Node.js "
                          "(most new plugins)."), 9, color=COL["muted"], wraplength=380, justify="center").pack(
                pady=(4, 0))
            return
        self.build_detail(plugins.installed()[self.selected])
        self._update_status()

    def select(self, uuid):
        self.selected = uuid
        self.render()

    def build_detail(self, info):
        from .pluginui import _plugin_icon, _rounded
        body = self.panel(self.right, fill="both", expand=True)
        head = tk.Frame(body, bg=COL["panel"])
        head.pack(fill="x")
        img = _plugin_icon(info, 48)
        if img is not None:
            ph = ImageTk.PhotoImage(_rounded(img, 11), master=self)
            self._photos.append(ph)
            tk.Label(head, image=ph, bg=COL["panel"]).pack(side="left")
        else:
            tk.Label(head, image=icon_tile(head, "puzzle", 48, COL["accent"]), bg=COL["panel"]).pack(side="left")
        txt = tk.Frame(head, bg=COL["panel"])
        txt.pack(side="left", padx=12)
        label(txt, info.name, 13, "bold").pack(anchor="w")
        m = info.manifest
        n = len(info.actions)
        meta = " · ".join(x for x in (_("Version {version}").format(version=info.version) if info.version else "",
                                      m.get("Author") or "",
                                      ngettext("{n} action", "{n} actions", n).format(n=n)) if x)
        label(txt, meta, 9, color=COL["muted"]).pack(anchor="w")
        srow = tk.Frame(body, bg=COL["panel"])
        srow.pack(anchor="w", pady=(16, 0))
        self.dot = tk.Canvas(srow, width=9, height=9, bg=COL["panel"], highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 8))
        self.status = label(srow, "", 10)
        self.status.pack(side="left")
        if m.get("Description"):
            label(body, m["Description"], 9, color=COL["muted"], wraplength=400, justify="left").pack(
                anchor="w", pady=(12, 0))
        btns = tk.Frame(body, bg=COL["panel"])
        btns.pack(side="bottom", anchor="w")
        from .pluginui import PluginManager
        if info.runnable and self.host is not None:
            Button(btns, _("Restart"), "undo", command=self._restart).pack(side="left", padx=(0, 8))
        log = plugins.paths.STATE_DIR / "plugins" / f"{info.uuid}.log"
        Button(btns, _("Log"), "text", command=lambda: PluginManager._open(str(log))).pack(side="left", padx=(0, 8))
        Button(btns, _("Remove"), "trash", kind="danger", command=self._remove).pack(side="left")

    def _update_status(self):
        from .pluginui import status
        for uuid, (info, st) in self.rows.items():
            text, color = status(info, self.host)
            st.configure(text=" · ".join(x for x in (text, info.version) if x), fg=color)
            if uuid == self.selected and getattr(self, "status", None) is not None:
                try:
                    self.status.configure(text=text, fg=color)
                    self.dot.delete("all")
                    self.dot.create_oval(0, 0, 8, 8, fill=color, outline="")
                except tk.TclError:
                    pass

    def _tick(self):
        if self.winfo_exists():
            self._update_status()
            self._tick_job = self.after(1500, self._tick)

    def destroy(self):
        try:
            self.after_cancel(self._tick_job)
        except (AttributeError, tk.TclError):
            pass
        super().destroy()

    def _changed(self):
        if self.host is not None:
            threading.Thread(target=self.host.sync, daemon=True).start()
        if hasattr(self.freedeck, "_plugins_changed"):
            self.freedeck._plugins_changed()
        self.render()

    def _install(self):
        from .pluginui import install_files
        if install_files(self):
            self._changed()

    def _restart(self):
        info = plugins.installed().get(self.selected)
        host = self.host
        if info is None or host is None:
            return

        def work():
            proc = host.procs.pop(info.uuid, None)
            if proc:
                proc.stop()
            host.sync()
        threading.Thread(target=work, daemon=True).start()

    def _remove(self):
        info = plugins.installed().get(self.selected)
        if info is None or not messagebox.askyesno(_("Remove plugin"), _(
                "Remove “{name}”? Keys with its actions stay in the profile but no longer do anything.").format(
                name=info.name), parent=self):
            return
        plugins.remove(info.uuid)
        self.selected = None
        self._changed()


# ===================================================================== Stil
class StylePage(_Page):
    def __init__(self, master, dialog, freedeck):
        super().__init__(master, dialog, freedeck)
        self.key = dict(KEY_STYLE_DEFAULT, **(self.cfg.get("title_style") or {}))
        self.dial = dict(DIAL_STYLE_DEFAULT, **(self.cfg.get("dial_title_style") or {}))
        area = ScrollArea(self)
        area.pack(fill="both", expand=True)
        b = area.inner
        # Multi-Aktion
        card = self.panel(b, fill="x", pady=(0, 14))
        section(card, _("Multi Action")).pack(anchor="w", pady=(0, 10))
        r = tk.Frame(card, bg=COL["panel"])
        r.pack(fill="x")
        self.progress = Switch(r, command=self._set_progress)
        self.progress.set(self.cfg.get("multi_progress", True))
        self.progress.pack(side="left")
        label(r, _("Show progress on the key while it runs"), 10).pack(side="left", padx=10)
        # Schrift
        card = self.panel(b, fill="x")
        section(card, _("Font for all keys")).pack(anchor="w", pady=(0, 10))
        grid = tk.Frame(card, bg=COL["panel"])
        grid.pack(side="left", fill="x", expand=True, anchor="n")
        prev = tk.Frame(card, bg=COL["panel"])
        prev.pack(side="right", anchor="n", padx=(16, 0))
        self.preview = tk.Label(prev, bg=COL["panel"])
        self.preview.pack()
        label(prev, _("Preview"), 9, color=COL["muted"]).pack(pady=(6, 0))

        r = self.row(grid, _("Keys"))
        self.show = Switch(r, command=lambda: self._set("ShowTitle", self.show.get()))
        self.show.set(self.key["ShowTitle"])
        self.show.pack(side="left")
        label(r, _("Show title"), 10).pack(side="left", padx=(10, 14))
        self.align = Segmented(r, [("top", _("top")), ("middle", _("middle")), ("bottom", _("bottom"))],
                               command=lambda v: self._set("TitleAlignment", v), value=self.key["TitleAlignment"])
        self.align.pack(side="left")

        r = self.row(grid, _("Font"))
        fams = [_("Standard")] + _font_families()
        self.family = ttk.Combobox(r, state="readonly", width=20, values=fams)
        self.family.set(self.key["FontFamily"] or fams[0])
        self.family.bind("<<ComboboxSelected>>", lambda e: self._set(
            "FontFamily", "" if self.family.current() == 0 else self.family.get()))
        self.family.pack(side="left")
        self.size = tk.StringVar(self, value=str(self.key["FontSize"]))
        ttk.Spinbox(r, from_=6, to=40, width=4, textvariable=self.size).pack(side="left", padx=(8, 0))
        self.size.trace_add("write", lambda *a: self._set_size())
        label(r, "px", 9, color=COL["muted"]).pack(side="left", padx=(4, 0))

        r = self.row(grid, _("Style"))
        self.toggles = {}
        for key, text in (("bold", "B"), ("italic", "I"), ("underline", "U")):
            t = _Toggle(r, text, command=lambda k=key: self._toggle_style(k))
            t.pack(side="left", padx=(0, 6))
            self.toggles[key] = t
        style = (self.key["FontStyle"] or "").lower()
        self.toggles["bold"].set("bold" in style)
        self.toggles["italic"].set("italic" in style)
        self.toggles["underline"].set(bool(self.key["FontUnderline"]))
        label(r, _("Colour"), 10, color=COL["muted"]).pack(side="left", padx=(14, 8))
        self.color_btn = _Swatch(r, self.key["TitleColor"], command=lambda: self._pick_color("key"))
        self.color_btn.pack(side="left")

        r = self.row(grid, _("Dials"))
        self.dial_show = Switch(r, command=lambda: self._set_dial("ShowTitle", self.dial_show.get()))
        self.dial_show.set(self.dial["ShowTitle"])
        self.dial_show.pack(side="left")
        label(r, _("Show title"), 10).pack(side="left", padx=(10, 14))
        self.dial_color = _Swatch(r, self.dial["TitleColor"], command=lambda: self._pick_color("dial"))
        self.dial_color.pack(side="left")

        r = tk.Frame(grid, bg=COL["panel"])
        r.pack(fill="x", pady=(10, 0))
        tk.Frame(r, bg=COL["panel"], width=1).pack(side="left", padx=(0, 140))
        Button(r, _("Apply to all keys"), kind="primary", command=self.apply_all).pack(side="left")
        Button(r, _("Reset"), command=self.reset).pack(side="left", padx=(8, 0))
        label(grid, _("New keys get this font automatically. “Apply to all keys” also changes the keys in "
                      "all your profiles."), 9, color=COL["muted"], wraplength=440, justify="left").pack(
            anchor="w", pady=(10, 0))
        self.draw_preview()

    # ---------------------------------------------------------- Änderungen
    def _store(self):
        self.cfg.data["title_style"] = dict(self.key)
        self.cfg.data["dial_title_style"] = dict(self.dial)
        from . import catalog
        catalog.set_title_defaults(self.key)
        self.save_later(300)
        self.draw_preview()

    def _set(self, k, v):
        self.key[k] = v
        self._store()

    def _set_dial(self, k, v):
        self.dial[k] = v
        self._store()

    def _set_size(self):
        try:
            v = int(self.size.get())
        except ValueError:
            return
        if 6 <= v <= 40:
            self._set("FontSize", v)

    def _toggle_style(self, key):
        if key == "underline":
            self._set("FontUnderline", self.toggles["underline"].get())
            return
        b, i = self.toggles["bold"].get(), self.toggles["italic"].get()
        self._set("FontStyle", "Bold Italic" if b and i else "Bold" if b else "Italic" if i else "")

    def _pick_color(self, which):
        cur = (self.key if which == "key" else self.dial)["TitleColor"]
        res = messagebox.askcolor(color=cur, parent=self, title=_("Title color"))
        if res and res[1]:
            if which == "key":
                self.color_btn.set(res[1])
                self._set("TitleColor", res[1])
            else:
                self.dial_color.set(res[1])
                self._set_dial("TitleColor", res[1])

    def _set_progress(self):
        self.cfg.data["multi_progress"] = self.progress.get()
        self.save_now()

    def draw_preview(self):
        from . import iconlib, render
        size = 96
        base = Image.new("RGBA", (size, size), (0, 0, 0, 255))
        g = iconlib.glyph("deck", 40, "#ffffff")
        base.alpha_composite(g, ((size - 40) // 2, 18))
        img = render.compose_key((size, size), None, _("Title"), self.key, base=base)
        mask = Image.new("L", (size * 4, size * 4), 0)
        from PIL import ImageDraw
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, size * 4 - 1, size * 4 - 1), radius=64, fill=255)
        out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        out.paste(img, (0, 0), mask.resize((size, size), Image.LANCZOS))
        self._prev_photo = ImageTk.PhotoImage(out, master=self)
        self.preview.configure(image=self._prev_photo)

    def reset(self):
        self.key, self.dial = dict(KEY_STYLE_DEFAULT), dict(DIAL_STYLE_DEFAULT)
        self.show.set(True)
        self.align.set("bottom")
        self.family.current(0)
        self.size.set("12")
        for t in self.toggles.values():
            t.set(False)
        self.color_btn.set("#ffffff")
        self.dial_show.set(True)
        self.dial_color.set("#ffffff")
        self._store()

    def apply_all(self):
        if not messagebox.askyesno(_("Apply to all keys"),
                                   _("Change title font, position and colour of all keys in all profiles?"),
                                   parent=self):
            return
        from . import editdoc
        lin = self.freedeck
        if getattr(lin, "doc", None) is not None:
            lin.save_now()
        n = editdoc.apply_title_style(lin.store_root, self.key, self.dial)
        if getattr(lin, "doc", None) is not None:
            lin.open_profile(lin.doc.id, from_device=True)
        messagebox.showinfo(_("Apply to all keys"),
                            ngettext("{n} key changed.", "{n} keys changed.", n).format(n=n), parent=self)


def _font_families():
    out = system.run_out(["fc-list", ":", "family"], timeout=3)
    fams = sorted({f.split(",")[0].strip() for f in out.splitlines() if f.strip() and not f.startswith(".")},
                  key=str.lower)
    return fams[:300]


class _Toggle(tk.Canvas):
    """Kleiner Umschaltknopf (B / I / U)."""

    def __init__(self, master, text, command=None):
        super().__init__(master, width=30, height=28, bg=COL["panel"], highlightthickness=0, cursor="hand2")
        self.text, self.command, self.on = text, command, False
        self.bind("<Button-1>", lambda e: self._click())
        self._draw()

    def get(self):
        return self.on

    def set(self, on):
        self.on = bool(on)
        self._draw()

    def _click(self):
        self.set(not self.on)
        if self.command:
            self.command()

    def _draw(self):
        from .theme import rounded_rect
        self.delete("all")
        rounded_rect(self, 1, 1, 29, 27, 7, fill=COL["accent"] if self.on else COL["panel2"], outline="")
        extra = {"B": ("bold",), "I": ("italic",), "U": ("underline",)}.get(self.text, ())
        f = (_font(self, 10)[0], 10) + extra
        self.create_text(15, 14, text=self.text, fill="#ffffff" if self.on else COL["fg"], font=f)


class _Swatch(tk.Canvas):
    """Farbfeld; Klick öffnet den Farbwähler."""

    def __init__(self, master, color, command=None):
        super().__init__(master, width=40, height=26, bg=COL["panel"], highlightthickness=0, cursor="hand2")
        self.bind("<Button-1>", lambda e: command and command())
        self.set(color)

    def set(self, color):
        from .theme import rounded_rect
        self.delete("all")
        rounded_rect(self, 1, 1, 39, 25, 6, fill=color or "#ffffff", outline=COL["border"])
