"""Dialoge von Linewave: eigene Kanäle anlegen/bearbeiten und Effekte (Plugins) je Kanal."""
import math
import shutil
import tkinter as tk
from tkinter import ttk

from . import effects
from .i18n import _
from .theme import COL, Card, Dialog, Fader, Icons, PillButton, Segmented, Switch, label

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
        self.icons = Icons(self)
        self.cells = []
        for i, v in enumerate(values):
            cv = tk.Canvas(self, width=36, height=36, bg=self.cget("bg"), highlightthickness=0, cursor="hand2")
            cv.grid(row=i // 8, column=i % 8, padx=3, pady=3)
            cv.bind("<Button-1>", lambda e, v=v: self.set(v, notify=True))
            self.cells.append((v, cv))
        self._draw()

    def set(self, v, notify=False):
        self.value = v
        self._draw()
        if notify and self.command:
            self.command(v)

    def set_color(self, color):
        self.color = color
        self._draw()

    def _draw(self):
        for v, cv in self.cells:
            cv.delete("all")
            sel = v == self.value
            if sel:
                cv.create_oval(1, 1, 35, 35, outline="#ffffff", width=2)
            if self.kind == "color":
                cv.create_oval(6, 6, 30, 30, fill=v, outline="")
            else:
                cv.create_oval(5, 5, 31, 31, fill=self.color if sel else COL["raised"], outline="")
                cv.create_image(18, 18, image=self.icons.get(v, 16, "#ffffff"))


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
        label(b, _("Edit channel") if edit else _("Create new channel"), 15, "bold").pack(anchor="w")
        label(b, (_("Afterwards, put apps into the channel in the mixer under “Apps”.")
                  if not edit else _("Changes are applied to the mixer right away.")),
              9, color=COL["muted"]).pack(anchor="w", pady=(2, 14))

        self.name = tk.StringVar(self, value=c.get("name", ""))
        label(b, "NAME", 9, "bold", color=COL["muted"]).pack(anchor="w")
        self.entry = tk.Entry(b, textvariable=self.name, bg=COL["panel2"], fg=COL["fg"], relief="flat",
                              insertbackground=COL["fg"], highlightthickness=1, highlightbackground=COL["border"],
                              highlightcolor=COL["accent"], font=("TkDefaultFont", 11))
        self.entry.pack(fill="x", ipady=6, pady=(4, 14))
        if is_mic:
            self.entry.configure(state="disabled")

        self.kind = c.get("kind", "apps") if edit else "apps"
        if not edit:
            label(b, _("TYPE"), 9, "bold", color=COL["muted"]).pack(anchor="w")
            self.kind_seg = Segmented(b, [("apps", _("Apps")), ("input", _("Input device"))], command=self._kind)
            self.kind_seg.pack(anchor="w", pady=(4, 4))
            self.kind_hint = label(b, "", 9, color=COL["muted"], wraplength=460, justify="left")
            self.kind_hint.pack(anchor="w", pady=(0, 10))

        self.dev_frame = tk.Frame(b, bg=COL["bg"])
        label(self.dev_frame, _("INPUT DEVICE"), 9, "bold", color=COL["muted"]).pack(anchor="w")
        self.dev = tk.StringVar(self)
        names = [d for _n, d in inputs] or [_("No input device found")]
        self.dev_box = ttk.Combobox(self.dev_frame, textvariable=self.dev, values=names, state="readonly")
        self.dev_box.pack(fill="x", pady=(4, 14))
        cur = c.get("source") or ""
        self.dev.set(next((d for n, d in inputs if n == cur), names[0] if inputs else names[0]))

        icon, color = c.get("icon") or default_look[0], c.get("color") or default_look[1]
        label(b, _("ICON"), 9, "bold", color=COL["muted"]).pack(anchor="w")
        self.icon = _Picker(b, ICONS, icon, "icon", color=color)
        self.icon.pack(anchor="w", pady=(4, 12))
        label(b, _("COLOR"), 9, "bold", color=COL["muted"]).pack(anchor="w")
        self.color = _Picker(b, COLORS, color if color in COLORS else COLORS[5], "color",
                             command=self.icon.set_color)
        self.color.pack(anchor="w", pady=(4, 6))
        self.error = label(b, "", 9, color=COL["danger"])
        self.error.pack(anchor="w", pady=(8, 0))

        ttk.Button(self.buttons, text=_("Save") if edit else _("Create channel"), style="Accent.TButton",
                   command=self._save).pack(side="right")
        ttk.Button(self.buttons, text=_("Cancel"), style="Ghost.TButton", command=self.destroy).pack(side="right",
                                                                                                padx=8)
        if edit and on_delete and not is_mic:
            ttk.Button(self.buttons, text=_("Delete channel"), style="Danger.TButton", command=self._delete).pack(
                side="left")
        self._kind(self.kind)
        self.entry.focus_set()
        self.bind("<Return>", lambda e: self._save())
        self.show()

    def _kind(self, kind):
        self.kind = kind
        if hasattr(self, "kind_hint"):
            self.kind_hint.configure(text=(
                _("A separate channel for apps – e.g. “Podcast” or “Second game”.")
                if kind == "apps" else
                _("Bring a device straight into the mix – e.g. a second microphone, line-in or a capture card.")))
        if kind == "input":
            self.dev_frame.pack(fill="x", after=self.kind_hint if hasattr(self, "kind_hint") else self.entry)
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


class _ScrollArea(tk.Frame):
    def __init__(self, master):
        super().__init__(master, bg=COL["bg"])
        self.cv = tk.Canvas(self, bg=COL["bg"], highlightthickness=0, bd=0)
        bar = ttk.Scrollbar(self, orient="vertical", command=self.cv.yview)
        self.cv.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.cv.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.cv, bg=COL["bg"])
        self._win = self.cv.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.cv.configure(scrollregion=self.cv.bbox("all")))
        self.cv.bind("<Configure>", lambda e: self.cv.itemconfigure(self._win, width=e.width))
        for seq, d in (("<Button-4>", -2), ("<Button-5>", 2)):
            self.bind_all(seq, lambda e, d=d: self._wheel(e, d), add="+")
        self.bind_all("<MouseWheel>", lambda e: self._wheel(e, -1 if e.delta > 0 else 1), add="+")

    def _wheel(self, e, d):
        w = e.widget
        while w is not None:
            if w is self:
                self.cv.yview_scroll(d, "units")
                return
            w = getattr(w, "master", None)


class EffectsDialog(Dialog):
    """Effektkette eines Kanals: Effekte hinzufügen, an/aus, Reihenfolge, Regler."""

    def __init__(self, master, channel, chain, catalog, on_change, on_control):
        super().__init__(master, _("Effects – {name}").format(name=channel["name"]), width=600, height=640)
        self.channel = channel
        self.chain = [dict(fx, controls=dict(fx.get("controls") or {})) for fx in chain]
        self.catalog = catalog
        self.on_change, self.on_control = on_change, on_control
        self.icons = Icons(self)
        head = tk.Frame(self.body, bg=COL["bg"])
        head.pack(fill="x")
        label(head, _("Effects for “{name}”").format(name=channel["name"]), 15, "bold").pack(anchor="w")
        label(head, _("Audio flows through the effects from top to bottom. Just like the plugins in Wave Link."),
              9, color=COL["muted"]).pack(anchor="w", pady=(2, 12))
        self.area = _ScrollArea(self.body)
        self.area.pack(fill="both", expand=True)
        ttk.Button(self.buttons, text=_("Done"), style="Accent.TButton", command=self.destroy).pack(side="right")
        PillButton(self.buttons, _("Add effect"), icon="plus", command=self._add, height=30).pack(side="left")
        self._render()
        self.show()

    # ------------------------------------------------------------ Anzeige
    def _render(self):
        for w in self.area.inner.winfo_children():
            w.destroy()
        if not self.chain:
            card = Card(self.area.inner, pad=22)
            card.pack(fill="x", pady=4)
            label(card.body, _("No effects yet"), 11, "bold").pack(anchor="w")
            label(card.body, _("Use “Add effect” to put e.g. noise suppression, a noise gate, a compressor or "
                               "an equalizer on this channel."), 9, color=COL["muted"], wraplength=480,
                  justify="left").pack(anchor="w", pady=(4, 0))
            return
        for i, fx in enumerate(self.chain):
            self._card(i, fx)

    def _card(self, i, fx):
        p = effects.lookup(fx["plugin"], self.catalog)
        card = Card(self.area.inner, pad=16)
        card.pack(fill="x", pady=5)
        b = card.body
        top = tk.Frame(b, bg=COL["panel"])
        top.pack(fill="x")
        tk.Label(top, image=self.icons.get("fx", 26, "#ffffff", circle=COL["accent"] if fx.get("enabled", True)
                                           else COL["raised"]), bg=COL["panel"]).pack(side="left")
        names = tk.Frame(top, bg=COL["panel"])
        names.pack(side="left", padx=10, fill="x", expand=True)
        label(names, fx.get("name") or (p or {}).get("name") or fx["plugin"], 11, "bold").pack(anchor="w")
        label(names, (p or {}).get("info") or (_("Plugin not found – is it installed?") if not p else ""),
              8, color=COL["muted"] if p else COL["danger"], wraplength=250, justify="left").pack(anchor="w")
        sw = Switch(top, command=lambda: self._toggle(i, sw.get()))
        sw.set(fx.get("enabled", True))
        sw.pack(side="right")
        for icon, cmd in (("close", lambda: self._remove(i)), ("down", lambda: self._move(i, 1)),
                          ("up", lambda: self._move(i, -1))):
            PillButton(top, icon=icon, command=cmd, padx=6).pack(side="right", padx=(0, 6))
        if not p:
            return
        grid = tk.Frame(b, bg=COL["panel"])
        grid.pack(fill="x", pady=(10, 0))
        for r, c in enumerate(p.get("controls", [])):
            value = float(fx["controls"].get(c["name"], c["default"]))
            label(grid, c.get("label") or c["name"], 9, color=COL["muted"], anchor="w", width=20).grid(row=r, column=0, sticky="w")
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
                          color=COL["accent"],
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

    def __init__(self, master, catalog, on_pick):
        super().__init__(master, _("Add effect"), width=560, height=600)
        self.catalog, self.on_pick = catalog, on_pick
        self.icons = Icons(self)
        label(self.body, _("Add effect"), 15, "bold").pack(anchor="w")
        n = {t: sum(1 for p in catalog if p["type"] == t) for t in ("builtin", "ladspa", "lv2")}
        self.lv2_ok, self.lv2_hint = effects.lv2_status()
        info = _("Found {builtin} built-in filters, {ladspa} LADSPA and {lv2} LV2 plugins. More plugins: packages "
                 "swh-plugins, tap-plugins, x42-plugins, lsp-plugins-lv2, calf-plugins.").format(**n)
        if n["lv2"] == 0 and not shutil.which("lv2info"):
            info += " " + _("For LV2 plugins, install the package lilv-utils.")
        elif not self.lv2_ok:
            info += " " + self.lv2_hint + "."
        label(self.body, info, 9, color=COL["muted"], wraplength=500, justify="left").pack(anchor="w", pady=(2, 8))
        self.kind = "all"
        self.seg = Segmented(self.body, [("all", _("All")), ("builtin", _("Built-in")), ("ladspa", "LADSPA"),
                                         ("lv2", "LV2")], command=self._kind)
        self.seg.pack(anchor="w", pady=(0, 8))
        sb = tk.Frame(self.body, bg=COL["panel2"])
        sb.pack(fill="x", pady=(0, 10))
        tk.Label(sb, image=self.icons.get("search", 16, COL["muted"]), bg=COL["panel2"]).pack(side="left", padx=8)
        self.q = tk.StringVar(self)
        e = tk.Entry(sb, textvariable=self.q, bg=COL["panel2"], fg=COL["fg"], insertbackground=COL["fg"],
                     relief="flat", highlightthickness=0)
        e.pack(side="left", fill="x", expand=True, ipady=6)
        e.focus_set()
        self.q.trace_add("write", lambda *a: self._fill())
        self.area = _ScrollArea(self.body)
        self.area.pack(fill="both", expand=True)
        ttk.Button(self.buttons, text=_("Close"), style="Ghost.TButton", command=self.destroy).pack(side="right")
        self._fill()
        self.show()

    def _kind(self, kind):
        self.kind = kind
        self.seg.set(kind)
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
                label(self.area.inner, _("… more – narrow down your search"), 9, color=COL["muted"]).pack(anchor="w")
                break
            row = tk.Frame(self.area.inner, bg=COL["panel"], padx=12, pady=8, cursor="hand2")
            row.pack(fill="x", pady=2)
            tag = {"builtin": "PipeWire", "ladspa": "LADSPA", "lv2": "LV2"}.get(p["type"], p["type"])
            ok = p["type"] != "lv2" or self.lv2_ok
            circle = {"builtin": COL["accent"], "ladspa": COL["stream"], "lv2": COL["monitor"]}.get(p["type"],
                                                                                                COL["raised"])
            tk.Label(row, image=self.icons.get("fx", 24, "#ffffff", circle=circle if ok else COL["raised"]),
                     bg=COL["panel"]).pack(side="left")
            txt = tk.Frame(row, bg=COL["panel"])
            txt.pack(side="left", padx=10, fill="x", expand=True)
            label(txt, p["name"], 10, "bold").pack(anchor="w")
            label(txt, f"{tag} · {p.get('info') or p.get('label', '')}" if ok else f"{tag} · {self.lv2_hint}",
                  8, color=COL["muted"] if ok else COL["warn"], wraplength=330, justify="left").pack(anchor="w")
            if not ok:
                continue
            add = PillButton(row, _("Add"), command=lambda p=p: self._pick(p))
            add.pack(side="right")
            for w in (row, txt):
                w.bind("<Double-Button-1>", lambda e, p=p: self._pick(p))

    def _pick(self, p):
        self.destroy()
        self.on_pick(p)

