"""Fenster von Linewave: Mixer (Monitor-/Stream-Mix je Kanal) und Mikrofon.

Aufbau wie in Elgato Wave Link: oben die Umschaltung Mixer/Mikrofon, darunter
je Kanal eine Karte mit zwei Reglern – Cyan für den Monitor-Mix (was du
hörst), Violett für den Stream-Mix (was dein Publikum hört) – und runden
Stumm-Knöpfen.

Alle Zugriffe auf PipeWire (pactl) und das Mikrofon (USB) laufen in einem
eigenen Arbeits-Thread, damit das Fenster nie hängt. Ergebnisse werden im
Tk-Thread übernommen; Regler, die gerade bewegt werden, überschreibt die
Aktualisierung nicht.
"""
import logging
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from . import theme, wavemic
from .i18n import _
from .theme import COL, Card, Fader, HScroll, PillButton, RoundToggle, Segmented, Switch, label

log = logging.getLogger("streamlin.waveui")
REFRESH_MS = 1500
TOUCH_HOLD = 2.5
MIX_LABEL = {"monitor": "Monitor", "stream": "Stream"}
MIX_COLOR = {"monitor": COL["monitor"], "stream": COL["stream"]}
MUTE_ICONS = {"monitor": ("headphones", "headphones_off"), "stream": ("stream", "stream_off")}
NOT_IN_MIXER = _("– not in mixer –")
CHANNEL_LOOK = {  # Kanal -> (Symbol, Farbe)
    "system": ("system", "#8e9bb0"), "music": ("music", "#30d158"), "browser": ("browser", "#2f7bff"),
    "voice": ("chat", "#6b74ff"), "game": ("game", "#ffb020"), "sfx": ("sfx", "#ff6bd5"),
    "mic": ("mic", "#ff4d5a"),
}


def channel_look(c):
    """(Symbol, Farbe) eines Kanals – eigene Angaben gehen vor."""
    if isinstance(c, dict):
        base = CHANNEL_LOOK.get(c["id"], ("input" if c.get("kind") == "input" else "wave", "#8e9bb0"))
        return c.get("icon") or base[0], c.get("color") or base[1]
    return CHANNEL_LOOK.get(c, ("wave", "#8e9bb0"))


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
    def __init__(self, root, host, hidden=False, tray=True):
        self.root, self.host = root, host
        self.mixer, self.mic = host.mixer, host.mic
        self.worker = Worker()
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
        self.view = "mixer"
        root.title("Linewave")
        root.geometry("1240x800")
        root.minsize(980, 620)
        root.protocol("WM_DELETE_WINDOW", self.hide)
        theme.apply(root)
        self.icons = theme.Icons(root)
        self._build()
        try:
            from PIL import ImageTk
            from .wavehost import make_icon
            self._icon = ImageTk.PhotoImage(make_icon(128), master=root)
            root.iconphoto(True, self._icon)
        except Exception:  # noqa: BLE001
            pass
        self.tray = None
        if tray:
            from .app import Tray
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
        if not self._hidden_once:
            self._hidden_once = True
            from .autoimport import notify
            notify(_("Linewave is still running"),
                   _("Mixer and microphone stay active. Open it again from the “Linewave” menu."))

    def _restart(self):
        theme.restart_later("streamlin.linewave")
        self.quit_app()

    def quit_app(self):
        self.request_quit()

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
        visible = self.root.state() != "withdrawn"
        if visible and not self._refreshing and time.time() - self._last_refresh > REFRESH_MS / 1000:
            self.refresh()
        self.root.after(100, self._poll)

    def _quit(self):
        if self.quitting:
            return
        self.quitting = True
        if self.tray:
            self.tray.stop()
        self.root.quit()

    def status(self, text):
        self.status_var.set(text)

    # ============================================================== Aufbau
    def _build(self):
        r = self.root
        head = tk.Frame(r, bg=COL["panel"], height=64)
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(head, image=self.icons.get("wave", 34, "#ffffff", circle=COL["accent"]),
                 bg=COL["panel"]).pack(side="left", padx=(16, 10))
        label(head, "Linewave", 15, "bold").pack(side="left")
        self.mic_head = tk.StringVar(self.root, value=_("Looking for microphone …"))
        self.mic_pill = label(head, textvariable=self.mic_head, size=9, color=COL["muted"], padx=10, pady=3,
                              bg=COL["panel2"])
        self.mic_pill.pack(side="left", padx=16)
        ttk.Button(head, text=_("Quit"), command=self.quit_app).pack(side="right", padx=(6, 16))
        ttk.Button(head, text=_("Close window"), style="Ghost.TButton", command=self.hide).pack(side="right")
        ttk.Button(head, text=_("Setup"), style="Ghost.TButton", command=self.open_setup).pack(side="right")
        theme.language_button(head, self.icons, "Linewave", self._restart).pack(side="right", padx=(0, 4))
        self.tabs = Segmented(head, [("mixer", _("Mixer")), ("mic", _("Microphone"))], command=self.show_view)
        self.tabs.place(relx=0.5, rely=0.5, anchor="center")

        self.status_var = tk.StringVar(self.root, value="")
        ttk.Label(r, textvariable=self.status_var, style="Status.TLabel", anchor="w").pack(fill="x", side="bottom")

        self.content = tk.Frame(r, bg=COL["bg"])
        self.content.pack(fill="both", expand=True, padx=18, pady=16)
        self.view_mixer = tk.Frame(self.content, bg=COL["bg"])
        self.view_mic = tk.Frame(self.content, bg=COL["bg"])
        self._build_mixer()
        self._build_mic()
        self.show_view("mixer")

    def show_view(self, key):
        self.view = key
        self.tabs.set(key)
        for k, f in (("mixer", self.view_mixer), ("mic", self.view_mic)):
            if k == key:
                f.pack(fill="both", expand=True)
            else:
                f.pack_forget()

    # -------------------------------------------------------------- Mixer
    def _build_mixer(self):
        t = self.view_mixer
        # Mixer noch nicht eingerichtet
        self.mix_off = tk.Frame(t, bg=COL["bg"])
        card = Card(self.mix_off, pad=28)
        card.pack(pady=60)
        b = card.body
        tk.Label(b, image=self.icons.get("wave", 64, "#ffffff", circle=COL["accent"]), bg=COL["panel"]).pack()
        label(b, _("Set up mixer"), 16, "bold").pack(pady=(14, 6))
        label(b, _("Just like in Elgato Wave Link, every app gets a channel – with its own volume "
                   "for your headphones (Monitor Mix) and for your stream (Stream Mix)."),
              color=COL["muted"], wraplength=460, justify="center").pack()
        ttk.Button(b, text=_("Set up mixer"), style="Accent.TButton",
                   command=lambda: self._mixer_enable(True)).pack(pady=(18, 0))

        # Mixer aktiv
        self.mix_on = tk.Frame(t, bg=COL["bg"])
        bar = tk.Frame(self.mix_on, bg=COL["bg"])
        bar.pack(fill="x", pady=(0, 12))
        for mix, text in (("monitor", _("Monitor Mix – what you hear")),
                          ("stream", _("Stream Mix – what your audience hears"))):
            dot = tk.Canvas(bar, width=12, height=12, bg=COL["bg"], highlightthickness=0)
            dot.create_oval(1, 1, 11, 11, fill=MIX_COLOR[mix], outline="")
            dot.pack(side="left", padx=(0, 6))
            label(bar, text, color=COL["muted"]).pack(side="left", padx=(0, 18))
        ttk.Button(bar, text=_("Remove mixer"), style="Ghost.TButton",
                   command=lambda: self._mixer_enable(False)).pack(side="right")
        self.out_var = tk.StringVar(self.root)
        self.out_box = ttk.Combobox(bar, textvariable=self.out_var, state="readonly", width=34)
        self.out_box.pack(side="right", padx=(6, 12))
        self.out_box.bind("<<ComboboxSelected>>", self._on_output)
        tk.Label(bar, image=self.icons.get("headphones", 18, COL["monitor"]), bg=COL["bg"]).pack(side="right")

        self.strip_scroll = HScroll(self.mix_on)
        self.strip_scroll.pack(fill="x")
        self.strip_row = self.strip_scroll.inner

        self.apps_card = Card(self.mix_on, pad=16)
        self.apps_card.pack(fill="both", expand=True, pady=(14, 0))
        hb = tk.Frame(self.apps_card.body, bg=COL["panel"])
        hb.pack(fill="x")
        label(hb, _("APPS"), 9, "bold", color=COL["muted"]).pack(side="left")
        label(hb, _("Choose a channel – Linewave remembers the assignment"), 9, color=COL["faint"]).pack(side="left",
                                                                                                padx=10)
        self.streams_frame = tk.Frame(self.apps_card.body, bg=COL["panel"])
        self.streams_frame.pack(fill="x", pady=(10, 0))

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
        for ch in ordered:
            cid, name = ch["id"], ch["name"]
            glyph, color = channel_look(ch)
            card = Card(self.strip_row, pad=12, radius=16)
            card.pack(side="left", padx=(0, 10), anchor="n")
            b = card.body
            tk.Label(b, image=self.icons.get(glyph, 40, "#ffffff", circle=color), bg=COL["panel"]).pack()
            label(b, name, 11, "bold", width=12).pack(pady=(8, 0))
            apps = tk.StringVar(self.root, value=" ")
            label(b, textvariable=apps, size=8, color=COL["muted"], wraplength=112, height=2,
                  justify="center").pack()
            row = tk.Frame(b, bg=COL["panel"])
            row.pack(pady=(6, 0))
            strip = {"apps": apps, "card": card, "channel": ch}
            for col, mix in enumerate(("monitor", "stream")):
                fader = Fader(row, length=160, thickness=40, color=MIX_COLOR[mix],
                              command=lambda v, c=cid, m=mix: self._on_level(c, m, v))
                fader.grid(row=0, column=col, padx=6)
                val = tk.StringVar(self.root, value="–")
                label(row, textvariable=val, size=9, color=COL["muted"], width=5).grid(row=1, column=col, pady=(4, 6))
                off, on = MUTE_ICONS[mix]
                mute = RoundToggle(row, off, on, size=36, command=lambda c=cid, m=mix: self._on_mute(c, m))
                mute.grid(row=2, column=col)
                label(row, MIX_LABEL[mix], 8, color=COL["faint"]).grid(row=3, column=col, pady=(4, 0))
                strip[mix] = {"fader": fader, "val": val, "mute": mute}
            tools = tk.Frame(b, bg=COL["panel"])
            tools.pack(pady=(10, 0))
            n = ch.get("fx", 0)
            strip["fx"] = PillButton(tools, f"FX {n}" if n else "FX", icon="fx", active=bool(n),
                                     command=lambda c=ch: self._open_effects(c))
            strip["fx"].pack(side="left", padx=(0, 6))
            PillButton(tools, icon="more", padx=8, command=lambda c=ch: self._edit_channel(c)).pack(side="left")
            self.strips[cid] = strip
        # „Kanal hinzufügen“ wie in Wave Link
        add = Card(self.strip_row, pad=12, radius=16, color=COL["panel"])
        add.pack(side="left", anchor="n", fill="y")
        inner = tk.Frame(add.body, bg=COL["panel"], cursor="hand2", width=112, height=200)
        inner.pack()
        btn = tk.Label(add.body, image=self.icons.get("plus", 44, "#ffffff", circle=COL["raised"]), bg=COL["panel"],
                       cursor="hand2")
        btn.place(in_=inner, relx=0.5, rely=0.42, anchor="center")
        txt = label(add.body, _("Add\nchannel"), 10, "bold", color=COL["muted"], justify="center", cursor="hand2")
        txt.place(in_=inner, relx=0.5, rely=0.68, anchor="center")
        for w in (inner, btn, txt):
            w.bind("<Button-1>", lambda e: self._new_channel())

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

        def done(res, err):
            if err is None:
                self.status(_("Monitor Mix → {output}").format(output=lbl))
        self.worker.submit(lambda: self.mixer.set_monitor_output(name), done)

    def _build_streams(self, streams, channels):
        for w in self.streams_frame.winfo_children():
            w.destroy()
        apps_ch = [c for c in channels if c["id"] != "mic" and c.get("kind", "apps") == "apps"]
        names = [c["name"] for c in apps_ch]
        ids = {c["name"]: c["id"] for c in apps_ch}
        by_id = {c["id"]: c for c in channels}
        if not streams:
            label(self.streams_frame, _("No app is playing audio right now."), color=COL["muted"]).pack(anchor="w")
            return
        cols = 3
        for i, s in enumerate(streams):
            r, c = divmod(i, cols)
            cell = tk.Frame(self.streams_frame, bg=COL["panel2"], padx=10, pady=6)
            cell.grid(row=r, column=c, sticky="we", padx=(0, 10), pady=4)
            glyph, color = channel_look(by_id.get(s["channel"]) or s["channel"]) if s["channel"] else \
                ("speaker", COL["faint"])
            tk.Label(cell, image=self.icons.get(glyph, 22, "#ffffff", circle=color), bg=COL["panel2"]).pack(
                side="left")
            label(cell, s["app"], 10, "bold", width=16, anchor="w").pack(side="left", padx=8)
            var = tk.StringVar(self.root, value=(by_id.get(s["channel"]) or {}).get("name", NOT_IN_MIXER))
            box = ttk.Combobox(cell, textvariable=var, values=names, state="readonly", width=14)
            box.pack(side="left")
            box.bind("<<ComboboxSelected>>", lambda e, s=s, v=var: self._on_stream(s, ids.get(v.get())))
        for c in range(cols):
            self.streams_frame.grid_columnconfigure(c, weight=1)

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
            self.refresh()
        self.worker.submit(lambda: self.mixer.move_stream(s["index"], cid, remember=s.get("key", "")), done)

    # ------------------------------------------------------------ Mikrofon
    def _row(self, parent, title, sub=""):
        row = tk.Frame(parent, bg=COL["panel"])
        row.pack(fill="x", pady=7)
        txt = tk.Frame(row, bg=COL["panel"])
        txt.pack(side="left", fill="x", expand=True)
        label(txt, title, 11, "bold").pack(anchor="w")
        if sub:
            label(txt, sub, 9, color=COL["muted"], wraplength=330, justify="left").pack(anchor="w")
        return row

    def _build_mic(self):
        t = self.view_mic
        self.v = {}
        # --- Karte Mikrofon
        card = Card(t, pad=22)
        card.pack(side="left", anchor="n", padx=(0, 16))
        b = card.body
        top = tk.Frame(b, bg=COL["panel"])
        top.pack(fill="x")
        tk.Label(top, image=self.icons.get("mic", 48, "#ffffff", circle=CHANNEL_LOOK["mic"][1]),
                 bg=COL["panel"]).pack(side="left")
        names = tk.Frame(top, bg=COL["panel"])
        names.pack(side="left", padx=14)
        self.mic_name = tk.StringVar(self.root, value="")
        label(names, textvariable=self.mic_name, size=14, weight="bold").pack(anchor="w")
        self.mic_hint = tk.StringVar(self.root, value="")
        label(names, textvariable=self.mic_hint, size=9, color=COL["muted"], wraplength=380,
              justify="left").pack(anchor="w")

        label(b, "GAIN", 9, "bold", color=COL["muted"]).pack(anchor="w", pady=(22, 6))
        g = tk.Frame(b, bg=COL["panel"])
        g.pack(fill="x")
        self.gain = Fader(g, length=380, thickness=36, orient="horizontal", color=COL["accent"],
                          command=self._on_gain)
        self.gain.pack(side="left")
        self.v["mute"] = RoundToggle(g, "mic", "mic_off", size=44, command=self._on_mic_mute)
        self.v["mute"].pack(side="right")
        self.gain_val = tk.StringVar(self.root, value="–")
        label(b, textvariable=self.gain_val, size=10, color=COL["fg"]).pack(anchor="w", pady=(4, 10))

        ttk.Separator(b).pack(fill="x", pady=8)
        row = self._row(b, "Low Cut", _("Filters out low hum and footfall noise"))
        self.v["lowcut"] = Switch(row, command=lambda: self._on_filter("lowcut"))
        self.v["lowcut"].pack(side="right")
        self.freq_seg = Segmented(row, [("80", "80 Hz"), ("120", "120 Hz")], command=self._on_freq)
        self.freq_seg.pack(side="right", padx=12)
        self.freq = tk.StringVar(self.root, value="80")
        row = self._row(b, "Clipguard", _("Prevents clipping when you suddenly get loud"))
        self.v["clipguard"] = Switch(row, command=lambda: self._on_filter("clipguard"))
        self.v["clipguard"].pack(side="right")

        # --- Karte Wave:3 (nur mit Hardware)
        right = tk.Frame(t, bg=COL["bg"])
        right.pack(side="left", anchor="n", fill="x", expand=True)
        self.hw_frame = Card(right, pad=22)
        h = self.hw_frame.body
        label(h, "WAVE:3", 9, "bold", color=COL["muted"]).pack(anchor="w")
        row = self._row(h, _("Headphones"))
        self.v["headphone_mute"] = RoundToggle(row, "headphones", "headphones_off", size=36,
                                               command=lambda: self._on_hw_bool("headphone_mute"))
        self.v["headphone_mute"].pack(side="right")
        self.hp_val = tk.StringVar(self.root, value="–")
        label(row, textvariable=self.hp_val, color=COL["muted"], width=8).pack(side="right", padx=8)
        self.hp = Fader(h, length=360, thickness=32, orient="horizontal", from_=-60, to=0, color=COL["monitor"],
                        command=lambda v: self._on_hw_level("headphone_volume", v, self.hp_val, "{value:.0f} dB"))
        self.hp.pack(anchor="w")
        row = self._row(h, _("Monitoring"), _("Balance of mic ↔ PC audio in your headphones"))
        self.dm_val = tk.StringVar(self.root, value="–")
        label(row, textvariable=self.dm_val, color=COL["muted"], width=12).pack(side="right")
        mixrow = tk.Frame(h, bg=COL["panel"])
        mixrow.pack(anchor="w")
        label(mixrow, "PC", 9, color=COL["faint"]).pack(side="left", padx=(0, 6))
        self.dm = Fader(mixrow, length=320, thickness=32, orient="horizontal", from_=0, to=100,
                        color=CHANNEL_LOOK["mic"][1],
                        command=lambda v: self._on_hw_level("direct_monitor", v, self.dm_val,
                                                          _("{value:.0f} % mic")))
        self.dm.pack(side="left")
        label(mixrow, _("Mic"), 9, color=COL["faint"]).pack(side="left", padx=(6, 0))
        ttk.Separator(h).pack(fill="x", pady=12)
        row = self._row(h, _("LEDs off"))
        self.v["leds_off"] = Switch(row, command=lambda: self._on_hw_bool("leds_off"))
        self.v["leds_off"].pack(side="right")
        row = self._row(h, _("Flip LEDs"), _("If the microphone is mounted upside down on a boom arm"))
        self.v["leds_flip"] = Switch(row, command=lambda: self._on_hw_bool("leds_flip"))
        self.v["leds_flip"].pack(side="right")

        self.no_hw = Card(right, pad=22)
        label(self.no_hw.body, _("More settings with the Elgato Wave:3"), 11, "bold").pack(anchor="w")
        label(self.no_hw.body, _("Headphone volume, monitoring and the LEDs can be adjusted directly on the "
                                 "Wave:3. For Wave:1, Wave Neo and Wave XLR the protocol for this is not "
                                 "documented."), 9, color=COL["muted"], wraplength=360, justify="left").pack(
            anchor="w", pady=(6, 0))
        self.no_hw.pack(anchor="nw")

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

    # ------------------------------------------------------ Aktualisieren
    def collect(self):
        """Zustand von Mixer und Mikrofon einlesen (läuft im Arbeits-Thread)."""
        st = {"mixer_enabled": self.host.mixer_enabled, "error": self.host.error, "mixer_active": False}
        try:
            st["mixer_active"] = self.mixer.is_active()
        except Exception as e:  # noqa: BLE001
            st["error"] = str(e)
        if st["mixer_active"]:
            st["channels"] = [dict(c, fx=sum(1 for fx in self.mixer.fx.chain(c["id"]) if fx.get("enabled", True)))
                              for c in self.mixer.all_channels()]
            st["levels"] = self.mixer.levels()
            st["outputs"] = [(o["name"], o.get("description") or o["name"]) for o in self.mixer.outputs()]
            st["monitor_output"] = self.mixer._state().get("monitor_output", "")
            st["streams"] = self.mixer.streams()
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

    def apply_state(self, st):
        self.state = st
        # --- Mixer
        if st.get("mixer_active"):
            self.mix_off.pack_forget()
            self.mix_on.pack(fill="both", expand=True)
            ids = tuple((c["id"], c["name"], c.get("icon"), c.get("color"), c.get("kind"), c["fx"])
                        for c in st["channels"])
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
            for cid, strip in self.strips.items():
                strip["apps"].set(", ".join(apps.get(cid, [])) or " ")
            outs = st.get("outputs") or []
            self.out_box.configure(values=[d for _n, d in outs])
            if not self._held("output"):
                self.out_var.set(next((d for n, d in outs if n == st.get("monitor_output")),
                                      st.get("monitor_output", "")))
            keys = tuple((s["index"], s["app"], s["channel"]) for s in st.get("streams") or [])
            if keys != self._stream_keys and not self._held("streams"):
                self._stream_keys = keys
                self._build_streams(st.get("streams") or [], st["channels"])
        else:
            self.mix_on.pack_forget()
            self.mix_off.pack(fill="both", expand=True)
            self._strip_ids = None
        if st.get("error"):
            self.status(st["error"])

        # --- Mikrofon
        mic, cfg = st.get("mic") or {}, st.get("settings") or {}
        hw = bool(mic.get("hardware"))
        present = hw or bool(mic.get("source"))
        name = "Elgato Wave:3" if hw else ("Elgato Wave" if present else _("No Wave microphone"))
        self.mic_name.set(name)
        muted = bool(mic.get("mute"))
        self.mic_head.set(("●  " + (_("{name} · muted").format(name=name) if muted else name)) if present
                          else "○  " + _("No Wave microphone connected"))
        self.mic_pill.configure(fg=COL["danger"] if muted else (COL["ok"] if present else COL["muted"]))
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
                self.hw_frame.pack(anchor="nw")
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
                self.no_hw.pack(anchor="nw")
