"""Dialoge von FreeWave: eigene Kanäle anlegen/bearbeiten und Effekte (Plugins) je Kanal."""
import math
import shutil
import tkinter as tk
from tkinter import ttk

from . import effects
from .dialogkit import ScrollArea, ShortcutField, entry, field, footer, header
from .i18n import _, ngettext
from .theme import COL, Button, Card, Chip, Dialog, Fader, Icons, SoftSegmented, Switch, font, icon_tile, \
    label, rounded_rect

ICONS = ["music", "browser", "chat", "game", "sfx", "system", "mic", "headphones", "speaker", "wave", "record",
         "play", "input", "keyboard", "stream", "grid"]
COLORS = ["#ff4d5a", "#ff8a3d", "#ffb020", "#30d158", "#2fc3ff", "#2f7bff", "#6b74ff", "#a66bff", "#ff6bd5",
          "#8e9bb0"]


class _Picker(tk.Frame):
    """Auswahl als Reihe runder Felder (Symbole oder Farben)."""

    def __init__(self, master, values, value, kind, color="#2f7bff", command=None):
        super().__init__(master, bg=master.cget("bg"))
        self.values, self.value, self.kind, self.color = values, value, kind, color
        self.command = command
        self.watch = None  # wird nach jeder Änderung aufgerufen (Vorschau im Dialogkopf)
        self.icons = Icons(self)
        self.cells = []
        for i, v in enumerate(values):
            cv = tk.Canvas(self, width=38, height=38, bg=self.cget("bg"), highlightthickness=0, cursor="hand2")
            per_row = 10 if kind == "color" else 8
            cv.grid(row=i // per_row, column=i % per_row, padx=2, pady=2)
            cv.bind("<Button-1>", lambda e, v=v: self.set(v, notify=True))
            self.cells.append((v, cv))
        self._draw()

    def set(self, v, notify=False):
        self.value = v
        self._draw()
        if self.watch:
            self.watch()
        if notify and self.command:
            self.command(v)

    def set_color(self, color):
        self.color = color
        self._draw()
        if self.watch:
            self.watch()

    def _draw(self):
        for v, cv in self.cells:
            cv.delete("all")
            sel = v == self.value
            if self.kind == "color":
                if sel:
                    cv.create_oval(2, 2, 36, 36, outline=COL["fg"], width=2)
                cv.create_oval(8, 8, 30, 30, fill=v, outline="")
            else:
                if sel:
                    cv.create_image(19, 19, image=icon_tile(cv, v, 34, self.color))
                else:
                    rounded_rect(cv, 2, 2, 36, 36, 9, fill=COL["panel2"], outline=COL["border"])
                    cv.create_image(19, 19, image=self.icons.get(v, 16, COL["muted"]))


class ChannelDialog(Dialog):
    """Kanal anlegen oder bearbeiten: Name, Art (Programme/Eingang), Gerät, Symbol, Farbe."""

    def __init__(self, master, inputs, channel=None, on_save=None, on_delete=None, default_look=("wave", COLORS[5])):
        edit = channel is not None
        c = channel or {}
        is_mic = c.get("id") == "mic"
        super().__init__(master, (_("Edit channel") if edit else _("New channel")), width=520)
        self.inputs = inputs
        self.on_save, self.on_delete = on_save, on_delete
        b = self.body
        icon, color = c.get("icon") or default_look[0], c.get("color") or default_look[1]
        color = color if color in COLORS else COLORS[5]
        self.head = header(b, _("Edit channel") if edit else _("Create new channel"),
                           (_("Afterwards, put apps into the channel in the mixer under “Apps”.")
                            if not edit else _("Changes are applied to the mixer right away.")), icon=icon, color=color)

        card = Card(b, pad=18, stretch=True)
        card.pack(fill="x")
        cb = card.body
        self.name = tk.StringVar(self, value=c.get("name", ""))
        field(cb, _("Name"))
        self.entry = entry(cb, self.name)
        self.entry.box.pack(fill="x", pady=(0, 14))
        if is_mic:
            self.entry.configure(state="disabled")

        self.kind = c.get("kind", "apps") if edit else "apps"
        if not edit:
            field(cb, _("TYPE"))
            self.kind_seg = SoftSegmented(cb, [("apps", _("Apps"), "grid"), ("input", _("Input device"), "input")],
                                          command=self._kind, fill=True, height=34)
            self.kind_seg.pack(fill="x", pady=(0, 6))
            self.kind_hint = label(cb, "", 9, color=COL["muted"], wraplength=440, justify="left")
            self.kind_hint.pack(anchor="w", pady=(0, 14))

        self.dev_frame = tk.Frame(cb, bg=COL["panel"])
        field(self.dev_frame, _("INPUT DEVICE"))
        self.dev = tk.StringVar(self)
        names = [d for _n, d in inputs] or [_("No input device found")]
        self.dev_box = ttk.Combobox(self.dev_frame, textvariable=self.dev, values=names, state="readonly")
        self.dev_box.pack(fill="x", pady=(0, 14))
        cur = c.get("source") or ""
        self.dev.set(next((d for n, d in inputs if n == cur), names[0] if inputs else names[0]))

        field(cb, _("ICON"))
        self.icon = _Picker(cb, ICONS, icon, "icon", color=color)
        self.icon.pack(anchor="w", pady=(0, 14))
        field(cb, _("COLOR"))
        self.color = _Picker(cb, COLORS, color, "color", command=self.icon.set_color)
        self.color.pack(anchor="w")
        self.icon.watch = self.color.watch = self._look
        self.error = label(b, "", 9, color=COL["danger"])
        self.error.pack(anchor="w", pady=(10, 0))

        footer(self)
        Button(self.buttons, _("Save") if edit else _("Create channel"), icon=None if edit else "plus",
               kind="primary", command=self._save).pack(side="right")
        Button(self.buttons, _("Cancel"), command=self.destroy).pack(side="right", padx=8)
        if edit and on_delete and not is_mic:
            Button(self.buttons, _("Delete channel"), icon="trash", kind="danger", command=self._delete).pack(
                side="left")
        self._kind(self.kind)
        self.entry.focus_set()
        self.bind("<Return>", lambda e: self._save())
        self.show()

    def _look(self):
        tile = getattr(self.head, "tile", None)
        if tile is not None:
            tile.configure(image=icon_tile(tile, self.icon.value, 40, self.icon.color))

    def _kind(self, kind):
        self.kind = kind
        if hasattr(self, "kind_hint"):
            self.kind_hint.configure(text=(
                _("A separate channel for apps – e.g. “Podcast” or “Second game”.")
                if kind == "apps" else
                _("Bring a device straight into the mix – e.g. a second microphone, line-in or a capture card.")))
        if kind == "input":
            self.dev_frame.pack(fill="x", after=self.kind_hint if hasattr(self, "kind_hint") else self.entry.box)
        else:
            self.dev_frame.pack_forget()

    def values(self):
        src = next((n for n, d in self.inputs if d == self.dev.get()), "")
        return {"name": self.name.get().strip(), "kind": self.kind, "source": src, "icon": self.icon.value,
                "color": self.color.value}

    def _save(self):
        v = self.values()
        if not v["name"] and self.entry.cget("state") != "disabled":
            self.error.configure(text=_("Please enter a name"))
            return
        if v["kind"] == "input" and not v["source"]:
            self.error.configure(text=_("Please choose an input device"))
            return
        self.destroy()
        if self.on_save:
            self.on_save(v)

    def _delete(self):
        self.destroy()
        if self.on_delete:
            self.on_delete()


MIX_ICONS = ["headphones", "stream", "chat", "speaker", "record", "game", "music", "mic", "wave", "grid"]
MIX_COLORS = ["#22c3ee", "#c084fc", "#22c55e", "#f5b13d", "#ff4d5a", "#5b7cff", "#ff6bd5", "#8e9bb0"]


class _CheckRow(tk.Canvas):
    """Zeile zum Anhaken: Kästchen, Symbol, Titel und Unterzeile (Ausgänge eines Mixes)."""

    def __init__(self, master, title, sub, icon, checked=False, enabled=True, command=None, width=456, height=50):
        super().__init__(master, width=width, height=height, bg=master.cget("bg"), highlightthickness=0, bd=0,
                         cursor="hand2" if enabled else "")
        self.title, self.sub, self.icon = title, sub, icon
        self.checked, self.enabled, self.command = checked, enabled, command
        self.icons = Icons(self)
        self.f1, self.f2 = font(self, 10, "bold"), font(self, 9)
        self.bind("<Configure>", lambda e: self._draw())
        if enabled:
            self.bind("<Button-1>", lambda e: self.toggle())
        self._draw()

    def toggle(self):
        self.checked = not self.checked
        self._draw()
        if self.command:
            self.command(self.checked)

    def _draw(self):
        self.delete("all")
        w, h = max(self.winfo_width(), int(self["width"])), int(self["height"])
        on = self.checked
        rounded_rect(self, 0, 0, w - 1, h - 1, 9, fill=COL["panel2"], outline=COL["accent"] if on else COL["border"])
        y = h / 2
        rounded_rect(self, 14, y - 9, 32, y + 9, 4, fill=COL["accent"] if on else "",
                     outline=COL["accent"] if on else COL["faint"], width=2)
        if on:
            self.create_image(23, y, image=self.icons.get("check", 13, "#ffffff"))
        self.create_image(56, y, image=self.icons.get(self.icon, 17, COL["muted"]))
        self.create_text(78, y - 8, text=self.title, anchor="w", fill=COL["fg"] if self.enabled else COL["muted"],
                         font=self.f1)
        self.create_text(78, y + 9, text=self.sub, anchor="w", fill=COL["muted"], font=self.f2)


class MixDialog(Dialog):
    """Mix anlegen oder bearbeiten: Name, Symbol, Farbe und Ausgänge (Geräte und/oder virtuelle Quelle)."""

    def __init__(self, master, outputs, mix=None, current_devices=(), on_save=None, on_delete=None):
        edit = mix is not None
        m = mix or {}
        super().__init__(master, _("Edit mix") if edit else _("New mix"), width=520)
        self.outputs, self.on_save, self.on_delete = outputs, on_save, on_delete
        self.mid = m.get("id", "")
        b = self.body
        icon = m.get("icon") or "chat"
        color = m.get("color") or MIX_COLORS[2]
        color = color if color in MIX_COLORS else MIX_COLORS[2]
        self.head = header(b, _("Edit mix") if edit else _("Create new mix"),
                           _("Where this mix goes and what it looks like"), icon=icon, color=color)
        area = ScrollArea(b, bg=COL["bg"])
        area.pack(fill="both", expand=True)
        cb = area.inner
        self.name = tk.StringVar(self, value=m.get("name", ""))
        field(cb, _("Name"))
        self.entry = entry(cb, self.name)
        self.entry.box.pack(fill="x", pady=(0, 14))
        field(cb, _("ICON AND COLOR"))
        self.icon = _Picker(cb, MIX_ICONS, icon, "icon", color=color)
        self.icon.pack(anchor="w", pady=(0, 6))
        self.color = _Picker(cb, MIX_COLORS, color, "color", command=self.icon.set_color)
        self.color.pack(anchor="w", pady=(0, 14))
        self.icon.watch = self.color.watch = self._look
        field(cb, _("OUTPUTS – THE MIX GOES TO ALL TICKED ONES"))
        self.devices = list(current_devices)
        self.current = list(current_devices)
        rows = tk.Frame(cb, bg=COL["bg"])
        rows.pack(fill="x")
        if not outputs:
            label(rows, _("No output device found"), 9, color=COL["muted"]).pack(anchor="w", pady=(0, 6))
        for name, desc in outputs:
            _CheckRow(rows, desc, _("Device"), "speaker", checked=name in self.devices,
                      command=lambda on, n=name: self._device(n, on)).pack(fill="x", pady=(0, 6))
        is_stream = self.mid == "stream"
        self.virtual = bool(m.get("virtual", True)) if edit else True
        vname = _("FreeWave Stream Mix") if is_stream else "FreeWave " + (m.get("name") or _("Mix"))
        self.virtual_row = _CheckRow(rows, _("Virtual source “{name}”").format(name=vname),
                                     _("always on – OBS records the Stream Mix from here") if is_stream else
                                     _("for apps like Discord or OBS"), "stream", checked=self.virtual or is_stream,
                                     enabled=not is_stream, command=self._virtual)
        self.virtual_row.pack(fill="x", pady=(0, 6))
        hint = (_("Without a ticked device the Personal Mix plays on the device chosen under “Listen”.")
                if self.mid == "monitor" else
                _("Tip: in Discord choose “Monitor of FreeWave {name}” as microphone.").format(
                    name=m.get("name") or _("Mix")))
        label(cb, hint, 9, color=COL["muted"], wraplength=440, justify="left").pack(anchor="w", pady=(4, 0))
        self.error = label(b, "", 9, color=COL["danger"])
        self.error.pack(anchor="w", pady=(10, 0))

        footer(self)
        Button(self.buttons, _("Save") if edit else _("Create mix"), icon=None if edit else "plus",
               kind="primary", command=self._save).pack(side="right")
        Button(self.buttons, _("Cancel"), command=self.destroy).pack(side="right", padx=8)
        if edit and on_delete and not m.get("builtin"):
            Button(self.buttons, _("Delete mix"), icon="trash", kind="danger", command=self._delete).pack(side="left")
        self.entry.focus_set()
        self.bind("<Return>", lambda e: self._save())
        self.geometry("560x720")
        self.show()

    def _look(self):
        tile = getattr(self.head, "tile", None)
        if tile is not None:
            tile.configure(image=icon_tile(tile, self.icon.value, 40, self.icon.color))

    def _device(self, name, on):
        self.devices = [d for d in self.devices if d != name] + ([name] if on else [])

    def _virtual(self, on):
        self.virtual = on

    def values(self):
        # bisherige Geräte behalten ihre Reihenfolge (das erste ist beim Persönlichen Mix das Abhörgerät)
        order = [d for d in self.current if d in self.devices] + [n for n, _d in self.outputs]
        devices = sorted(self.devices, key=lambda d: order.index(d) if d in order else len(order))
        return {"name": self.name.get().strip(), "icon": self.icon.value, "color": self.color.value,
                "devices": devices, "virtual": self.virtual or self.mid == "stream"}

    def _save(self):
        v = self.values()
        if not v["name"]:
            self.error.configure(text=_("Please enter a name"))
            return
        if not v["devices"] and not v["virtual"] and self.mid != "monitor":
            self.error.configure(text=_("Choose at least one output"))
            return
        self.destroy()
        if self.on_save:
            self.on_save(v)

    def _delete(self):
        self.destroy()
        if self.on_delete:
            self.on_delete()


def _fmt(value, c):
    if c.get("toggle"):
        return _("on") if value >= 0.5 else _("off")
    unit = f" {c['unit']}" if c.get("unit") else ""
    if c.get("int") or abs(value) >= 100:
        return f"{value:.0f}{unit}"
    return f"{value:.2f}{unit}" if abs(value) < 10 else f"{value:.1f}{unit}"


class EffectsDialog(Dialog):
    """Effektkette eines Kanals: Effekte hinzufügen, an/aus, Reihenfolge, Regler."""

    def __init__(self, master, channel, chain, catalog, on_change, on_control):
        super().__init__(master, _("Effects – {name}").format(name=channel["name"]), width=600, height=640)
        self.channel = channel
        self.chain = [dict(fx, controls=dict(fx.get("controls") or {})) for fx in chain]
        self.catalog = catalog
        self.on_change, self.on_control = on_change, on_control
        self.icons = Icons(self)
        header(self.body, _("Effects for “{name}”").format(name=channel["name"]),
               _("Audio flows through the effects from top to bottom. Just like the plugins in Wave Link."),
               icon="fx", color=channel.get("color") or COL["accent"], wrap=500)
        self.area = ScrollArea(self.body)
        self.area.pack(fill="both", expand=True)
        footer(self)
        Button(self.buttons, _("Done"), kind="primary", command=self.destroy).pack(side="right")
        Button(self.buttons, _("Add effect"), icon="plus", command=self._add).pack(side="left")
        self._render()
        self.show()

    # ------------------------------------------------------------ Anzeige
    def _render(self):
        for w in self.area.inner.winfo_children():
            w.destroy()
        if not self.chain:
            card = Card(self.area.inner, pad=22, stretch=True)
            card.pack(fill="x", pady=4)
            tk.Label(card.body, image=icon_tile(card.body, "fx", 40, COL["raised"], fg=COL["muted"]),
                     bg=COL["panel"]).pack(side="left", anchor="n")
            txt = tk.Frame(card.body, bg=COL["panel"])
            txt.pack(side="left", padx=14, fill="x", expand=True)
            label(txt, _("No effects yet"), 11, "bold").pack(anchor="w")
            label(txt, _("Use “Add effect” to put e.g. noise suppression, a noise gate, a compressor or "
                         "an equalizer on this channel."), 9, color=COL["muted"], wraplength=420,
                  justify="left").pack(anchor="w", pady=(4, 0))
            return
        for i, fx in enumerate(self.chain):
            self._card(i, fx)

    def _card(self, i, fx):
        p = effects.lookup(fx["plugin"], self.catalog)
        on = fx.get("enabled", True)
        card = Card(self.area.inner, pad=16, stretch=True)
        card.pack(fill="x", pady=(0, 10))
        b = card.body
        top = tk.Frame(b, bg=COL["panel"])
        top.pack(fill="x")
        tk.Label(top, image=icon_tile(top, "fx", 34, COL["accent"] if on and p else COL["raised"],
                                      fg="#ffffff" if on and p else COL["muted"]),
                 bg=COL["panel"]).pack(side="left", anchor="n")
        names = tk.Frame(top, bg=COL["panel"])
        names.pack(side="left", padx=12, fill="x", expand=True)
        label(names, fx.get("name") or (p or {}).get("name") or fx["plugin"], 11, "bold",
              color=COL["fg"] if on else COL["muted"]).pack(anchor="w")
        label(names, (p or {}).get("info") or (_("Plugin not found – is it installed?") if not p else ""),
              9, color=COL["muted"] if p else COL["danger"], wraplength=250, justify="left").pack(anchor="w")
        sw = Switch(top, command=lambda: self._toggle(i, sw.get()))
        sw.set(on)
        sw.pack(side="right", padx=(8, 0))
        for icon, cmd, ok in (("trash", lambda: self._remove(i), True),
                              ("down", lambda: self._move(i, 1), i < len(self.chain) - 1),
                              ("up", lambda: self._move(i, -1), i > 0)):
            bt = Button(top, icon=icon, kind="ghost", command=cmd, height=30)
            bt.configure_text(enabled=ok)
            bt.pack(side="right", padx=(2, 0))
        if not p or not p.get("controls"):
            return
        tk.Frame(b, bg=COL["border"], height=1).pack(fill="x", pady=(14, 10))
        grid = tk.Frame(b, bg=COL["panel"])
        grid.pack(fill="x")
        grid.grid_columnconfigure(1, weight=1)
        for r, c in enumerate(p.get("controls", [])):
            value = float(fx["controls"].get(c["name"], c["default"]))
            label(grid, c.get("label") or c["name"], 9, color=COL["muted"], anchor="w", width=20).grid(
                row=r, column=0, sticky="w")
            val = tk.StringVar(self, value=_fmt(value, c))
            if c.get("toggle"):
                w = Switch(grid)
                w.command = lambda c=c, i=i, w=w, val=val: self._control(i, c, 1.0 if w.get() else 0.0, val)
                w.set(value >= 0.5)
                w.grid(row=r, column=1, sticky="w", pady=3)
            else:
                lo, hi = float(c["min"]), float(c["max"])
                use_log = c.get("log") and lo > 0 and hi > 0
                to_pos = (lambda v, lo=lo, hi=hi: (math.log(v / lo) / math.log(hi / lo) * 100)) if use_log else \
                    (lambda v, lo=lo, hi=hi: (v - lo) / ((hi - lo) or 1) * 100)
                from_pos = (lambda t, lo=lo, hi=hi: lo * (hi / lo) ** (t / 100)) if use_log else \
                    (lambda t, lo=lo, hi=hi: lo + (hi - lo) * t / 100)
                f = Fader(grid, length=230, thickness=26, orient="horizontal", from_=0, to=100,
                          color=COL["accent"] if on else COL["faint"],
                          command=lambda t, c=c, i=i, val=val, fp=from_pos: self._control(
                              i, c, round(fp(t)) if c.get("int") else fp(t), val))
                f.set(max(0.0, min(100.0, to_pos(min(max(value, lo), hi)))))
                f.grid(row=r, column=1, sticky="w", pady=2)
            label(grid, textvariable=val, size=9, width=10, anchor="e").grid(row=r, column=2, sticky="e")

    # ---------------------------------------------------------- Änderungen
    def _changed(self, rebuild=True):
        if self.on_change:
            self.on_change(self.chain, rebuild)

    def _toggle(self, i, on):
        self.chain[i]["enabled"] = bool(on)
        self._changed()
        self._render()

    def _remove(self, i):
        del self.chain[i]
        self._changed()
        self._render()

    def _move(self, i, d):
        j = i + d
        if 0 <= j < len(self.chain):
            self.chain[i], self.chain[j] = self.chain[j], self.chain[i]
            self._changed()
            self._render()

    def _control(self, i, c, value, var):
        self.chain[i]["controls"][c["name"]] = value
        if var is not None:
            var.set(_fmt(value, c))
        if self.on_control:
            self.on_control(self.chain, i, c["name"], value)

    def _add(self):
        PluginPicker(self, self.catalog, self._added)

    def _added(self, plugin):
        self.chain.append(effects.new_effect(plugin))
        self._changed()
        self._render()
        self.after(50, lambda: self.area.cv.yview_moveto(1.0))


class PluginPicker(Dialog):
    """Liste aller Effekte mit Suche – wie der Plugin-Browser in Wave Link."""

    KINDS = ("builtin", "ladspa", "lv2")

    def __init__(self, master, catalog, on_pick):
        super().__init__(master, _("Add effect"), width=580, height=640)
        self.catalog, self.on_pick = catalog, on_pick
        self.icons = Icons(self)
        n = {t: sum(1 for p in catalog if p["type"] == t) for t in self.KINDS}
        self.lv2_ok, self.lv2_hint = effects.lv2_status()
        info = _("Found {builtin} built-in filters, {ladspa} LADSPA and {lv2} LV2 plugins. More plugins: packages "
                 "swh-plugins, tap-plugins, x42-plugins, lsp-plugins-lv2, calf-plugins.").format(**n)
        if n["lv2"] == 0 and not shutil.which("lv2info"):
            info += " " + _("For LV2 plugins, install the package lilv-utils.")
        elif not self.lv2_ok:
            info += " " + self.lv2_hint + "."
        header(self.body, _("Add effect"), info, icon="fx", wrap=470)

        sb = tk.Frame(self.body, bg=COL["panel"], highlightthickness=1, highlightbackground=COL["border"],
                      highlightcolor=COL["accent"])
        sb.pack(fill="x", pady=(0, 10))
        tk.Label(sb, image=self.icons.get("search", 16, COL["muted"]), bg=COL["panel"]).pack(side="left",
                                                                                         padx=(10, 6))
        self.q = tk.StringVar(self)
        e = tk.Entry(sb, textvariable=self.q, bg=COL["panel"], fg=COL["fg"], insertbackground=COL["fg"],
                     relief="flat", highlightthickness=0, font=font(sb, 10))
        e.pack(side="left", fill="x", expand=True, ipady=7)
        e.focus_set()
        self.q.trace_add("write", lambda *a: self._fill())

        chips = tk.Frame(self.body, bg=COL["bg"])
        chips.pack(fill="x", pady=(0, 12))
        self.kind = "all"
        self.chips = {}
        for key, text in (("all", _("All")), ("builtin", _("Built-in")), ("ladspa", "LADSPA"), ("lv2", "LV2")):
            ch = Chip(chips, text, command=lambda k=key: self._kind(k), active=key == "all")
            ch.pack(side="left", padx=(0, 6))
            self.chips[key] = ch
        self.count = label(chips, "", 9, color=COL["faint"])
        self.count.pack(side="right")
        self.area = ScrollArea(self.body)
        self.area.pack(fill="both", expand=True)
        footer(self)
        Button(self.buttons, _("Close"), command=self.destroy).pack(side="right")
        self._fill()
        self.show()

    def _kind(self, kind):
        self.kind = kind
        for k, ch in self.chips.items():
            ch.set(active=k == kind)
        self._fill()

    def _fill(self):
        for w in self.area.inner.winfo_children():
            w.destroy()
        q = self.q.get().strip().lower()
        shown = 0
        for p in self.catalog:
            if self.kind != "all" and p["type"] != self.kind:
                continue
            if q and q not in p["name"].lower() and q not in (p.get("info") or "").lower() \
                    and q not in p.get("label", "").lower():
                continue
            shown += 1
            if shown > 150:
                label(self.area.inner, _("… more – narrow down your search"), 9, color=COL["muted"]).pack(
                    anchor="w", pady=(6, 0))
                break
            self._row(p)
        self.count.configure(text=str(min(shown, 150)) if shown else "")
        if not shown:
            label(self.area.inner, _("No effect matches your search."), 10, color=COL["muted"]).pack(
                anchor="w", pady=12)
        self.area.cv.yview_moveto(0)

    def _row(self, p):
        row = tk.Frame(self.area.inner, bg=COL["panel"], padx=12, pady=10, cursor="hand2",
                       highlightthickness=1, highlightbackground=COL["border"])
        row.pack(fill="x", pady=(0, 6))
        tag = {"builtin": "PipeWire", "ladspa": "LADSPA", "lv2": "LV2"}.get(p["type"], p["type"])
        ok = p["type"] != "lv2" or self.lv2_ok
        tint = {"builtin": COL["accent"], "ladspa": COL["stream"], "lv2": COL["monitor"]}.get(p["type"],
                                                                                          COL["raised"])
        tk.Label(row, image=icon_tile(row, "fx", 32, tint if ok else COL["raised"],
                                      fg="#ffffff" if ok else COL["muted"]), bg=COL["panel"]).pack(side="left")
        txt = tk.Frame(row, bg=COL["panel"])
        txt.pack(side="left", padx=12, fill="x", expand=True)
        label(txt, p["name"], 10, "bold").pack(anchor="w")
        label(txt, f"{tag} · {p.get('info') or p.get('label', '')}" if ok else f"{tag} · {self.lv2_hint}",
              9, color=COL["muted"] if ok else COL["warn"], wraplength=340, justify="left").pack(anchor="w")
        if not ok:
            return
        Button(row, _("Add"), icon="plus", command=lambda p=p: self._pick(p), height=30).pack(side="right")
        for w in (row, txt, *txt.winfo_children()):
            w.bind("<Double-Button-1>", lambda e, p=p: self._pick(p))

    def _pick(self, p):
        self.destroy()
        self.on_pick(p)


# ================================================================ Mixer-Szenen
class SceneNameDialog(Dialog):
    """Szene speichern oder umbenennen: nur ein Name."""

    def __init__(self, master, name="", on_save=None, rename=False):
        title = _("Rename scene") if rename else _("Save scene")
        super().__init__(master, title, width=460)
        self.on_save = on_save
        header(self.body, title, _("Saves the volume and mute state of every channel in both mixes.")
               if not rename else "", icon="layers")
        self.name = tk.StringVar(self, value=name)
        field(self.body, _("Name"))
        self.entry = entry(self.body, self.name)
        self.entry.box.pack(fill="x")
        self.error = label(self.body, "", 9, color=COL["danger"])
        self.error.pack(anchor="w", pady=(8, 0))
        footer(self)
        Button(self.buttons, _("Save"), kind="primary", command=self._save).pack(side="right")
        Button(self.buttons, _("Cancel"), command=self.destroy).pack(side="right", padx=8)
        self.entry.focus_set()
        self.entry.select_range(0, "end")
        self.bind("<Return>", lambda e: self._save())
        self.show()

    def _save(self):
        name = self.name.get().strip()
        if not name:
            self.error.configure(text=_("Please enter a name"))
            return
        self.destroy()
        if self.on_save:
            self.on_save(name)


class ScenesDialog(Dialog):
    """Mixer-Szenen verwalten: anwenden, Tastenkürzel, umbenennen, löschen, neue speichern.

    actions: {"apply", "rename", "delete", "new", "shortcut"} -> Funktionen des Fensters.
    """

    def __init__(self, master, scenes, active, keys, actions):
        super().__init__(master, _("Mixer scenes"), width=640, height=520)
        self.actions = actions
        self.icons = Icons(self)
        header(self.body, _("Mixer scenes"), _("A scene remembers the volume and mute state of every channel. "
                                               "Apply it with one click, a keyboard shortcut or a FreeDeck key."),
               icon="layers", wrap=520)
        self.area = ScrollArea(self.body)
        self.area.pack(fill="both", expand=True)
        footer(self)
        Button(self.buttons, _("Done"), kind="primary", command=self.destroy).pack(side="right")
        Button(self.buttons, _("Save current mix"), icon="plus", command=actions["new"]).pack(side="left")
        self.update_scenes(scenes, active, keys)
        self.show()

    def update_scenes(self, scenes, active, keys):
        """Liste neu aufbauen. keys: {Szenen-id: Tastenkürzel}."""
        sig = (tuple((s["id"], s.get("name")) for s in scenes), active, tuple(sorted(keys.items())))
        if sig == getattr(self, "_sig", None):
            return
        self._sig = sig
        box = self.area.inner
        for w in box.winfo_children():
            w.destroy()
        if not scenes:
            label(box, _("No scenes yet. Set up the mixer the way you like it and save it as a scene."),
                  color=COL["muted"], wraplength=520, justify="left").pack(anchor="w", pady=8)
            return
        for s in scenes:
            on = s["id"] == active
            card = Card(box, pad=12, radius=12, stretch=True, outline=COL["accent"] if on else None)
            card.pack(fill="x", pady=(0, 8))
            b = card.body
            top = tk.Frame(b, bg=COL["panel"])
            top.pack(fill="x")
            tk.Label(top, image=icon_tile(top, "layers", 32, COL["accent"] if on else COL["faint"], radius=0.3),
                     bg=COL["panel"]).pack(side="left")
            txt = tk.Frame(top, bg=COL["panel"])
            txt.pack(side="left", padx=12, fill="x", expand=True)
            label(txt, s.get("name") or s["id"], 10, "bold").pack(anchor="w")
            n = len(s.get("levels") or {})
            label(txt, _("Active") if on else ngettext("{count} channel", "{count} channels", n).format(count=n), 9,
                  color=COL["accent"] if on else COL["muted"]).pack(anchor="w")
            Button(top, icon="trash", kind="ghost", command=lambda s=s: self.actions["delete"](s)).pack(side="right")
            Button(top, icon="edit", kind="ghost", command=lambda s=s: self.actions["rename"](s)).pack(side="right")
            Button(top, _("Apply"), command=lambda s=s: self.actions["apply"](s)).pack(side="right", padx=(0, 6))
            row = tk.Frame(b, bg=COL["panel"])
            row.pack(fill="x", pady=(10, 0))
            label(row, _("Keyboard shortcut"), 9, color=COL["muted"]).pack(side="left", padx=(44, 12))
            ShortcutField(row, keys.get(s["id"], ""), on_set=lambda bnd, s=s: self.actions["shortcut"](s, bnd),
                          on_remove=lambda s=s: self.actions["shortcut"](s, None)).pack(side="left")


class DeckExportDialog(Dialog):
    """Profil „FreeWave“ für ein Stream-Deck-Modell anlegen (Lautstärke direkt am Gerät)."""

    def __init__(self, master, channels, on_done=None):
        from . import deckexport
        super().__init__(master, _("Export to FreeDeck"), width=560)
        self.dx, self.on_done = deckexport, on_done
        self.channels = channels  # [(id, Name)]
        b = self.body
        header(b, _("Export to FreeDeck"),
               _("Creates a FreeDeck profile “FreeWave” with volume controls for the chosen channels. "
                 "It is updated by itself when you add or delete channels."), icon="export", wrap=460)
        self.devices = deckexport.devices()
        footer(self)
        self.create_btn = Button(self.buttons, _("Create profile"), icon="plus", kind="primary", command=self._create)
        self.create_btn.pack(side="right")
        Button(self.buttons, _("Cancel"), command=self.destroy).pack(side="right", padx=8)

        plugged = [d for d in self.devices if d.get("connected")]
        self.cards = []
        self.device_box = None
        if plugged:  # angeschlossene Stream Decks: als Karten, das erste ist gewählt
            field(b, _("DEVICE – DETECTED AUTOMATICALLY"))
            row = tk.Frame(b, bg=COL["bg"])
            row.pack(fill="x", pady=(0, 6 if len(plugged) > 1 else 14))
            self.sel = plugged[0]
            for i, d in enumerate(plugged[:3]):
                card = _CheckRow(row, d["name"], _("connected") + (" · " + _("profile exists") if d["profile"] else ""),
                                 "deck", checked=i == 0, width=170 if len(plugged) > 1 else 456, height=54,
                                 command=lambda on, d=d: self._pick(d))
                card.pack(side="left", fill="x", expand=True, padx=(0, 6))
                self.cards.append((d, card))
            if len(plugged) > 1:
                label(b, _("Several are connected: click the one the profile is for."), 9,
                      color=COL["muted"]).pack(anchor="w", pady=(0, 14))
        else:  # nichts angeschlossen: Modell wählen
            field(b, _("DEVICE"))
            label(b, _("No device connected – choose the model:"), 9, color=COL["muted"]).pack(anchor="w",
                                                                                                 pady=(0, 6))
            names = [self._device_label(d) for d in self.devices]
            self.device = tk.StringVar(self, value=names[0])
            self.device_box = ttk.Combobox(b, textvariable=self.device, values=names, state="readonly")
            self.device_box.pack(fill="x", pady=(0, 14))
            self.device_box.bind("<<ComboboxSelected>>", lambda e: self._update_hint())

        field(b, _("CHANNELS"))
        box = tk.Frame(b, bg=COL["bg"])
        box.pack(fill="x", pady=(0, 14))
        self.chosen = {cid: True for cid, _n in channels}
        self.chips = {}
        for i, (cid, name) in enumerate(channels):
            chip = Chip(box, name, active=True, command=lambda c=cid: self._toggle(c))
            chip.grid(row=i // 4, column=i % 4, sticky="w", padx=(0, 6), pady=(0, 6))
            self.chips[cid] = chip

        field(b, "MIX")
        self.mix = "selected"
        SoftSegmented(b, [("selected", _("Choose on deck"), "deck"), ("both", _("All mixes"), None),
                          ("monitor", _("Personal"), "headphones"), ("stream", "Stream", "stream")],
                      command=self._set_mix, value="selected",
                      fill=True).pack(fill="x", pady=(0, 14))

        self.hint = label(b, "", 9, color=COL["muted"], wraplength=500, justify="left")
        self.hint.pack(anchor="w")
        self.result = label(b, "", 9, color=COL["muted"], wraplength=500, justify="left")
        self.result.pack(anchor="w", pady=(8, 0))
        self._update_hint()
        self.show()

    @staticmethod
    def _device_label(d):
        return d["name"] + (" · " + _("in use") if d["in_use"] else "")

    def _selected_device(self):
        if self.device_box is None:
            return self.sel
        return self.devices[max(0, self.device_box.current())]

    def _pick(self, d):
        """Eine der Gerätekarten gewählt: nur diese bleibt angehakt."""
        self.sel = d
        for dd, card in self.cards:
            card.checked = dd is d
            card._draw()
        self._update_hint()

    def _selected_channels(self):
        return [cid for cid, _name in self.channels if self.chosen.get(cid)]

    def _toggle(self, cid):
        self.chosen[cid] = not self.chosen[cid]
        self.chips[cid].set(active=self.chosen[cid])
        self._update_hint()

    def _set_mix(self, value):
        self.mix = value
        self._update_hint()

    def _update_hint(self):
        d = self._selected_device()
        try:
            geo = self.dx.geometry_of(d["model"])
            text = self.dx.describe(geo)
            n = len(self.dx.plan(geo, self._selected_channels(), selectable=self.mix == "selected"))
            if n > 1:
                text += " " + _("The channels need {count} pages; keys to turn the page are added.").format(count=n)
            if d["profile"]:
                text += " " + _("The existing profile “{name}” is replaced.").format(name=d["profile"])
            ok = True
        except self.dx.ExportError as e:
            text, ok = str(e), False
        self.hint.configure(text=text, fg=COL["muted"] if ok else COL["danger"])
        self.create_btn.configure_text(text=_("Update profile") if d["profile"] else _("Create profile"), enabled=ok)

    def _create(self):
        if not self.create_btn.enabled:
            return
        d = self._selected_device()
        chans = self._selected_channels()
        try:
            name, pages, created = self.dx.export(d["model"], chans, mix=self.mix,
                                                  all_channels=len(chans) == len(self.channels))
        except (self.dx.ExportError, OSError) as e:
            self.result.configure(text=_("Error: {error}").format(error=e), fg=COL["danger"])
            return
        self.destroy()
        if self.on_done:
            self.on_done(name, created)
