"""Dialoge von Linewave: eigene Kanäle anlegen/bearbeiten und Effekte (Plugins) je Kanal."""
import math
import shutil
import tkinter as tk
from tkinter import ttk

from . import effects
from .dialogkit import ScrollArea, entry, field, footer, header
from .i18n import _
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
