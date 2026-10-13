"""Fenster von Linewave: Mixer (Monitor-/Stream-Mix je Kanal) und Mikrofon.

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

from . import wavemic
from .editor import COL

log = logging.getLogger("lindeck.waveui")
REFRESH_MS = 1500
TOUCH_HOLD = 2.5
MIX_LABEL = {"monitor": "Monitor", "stream": "Stream"}
NOT_IN_MIXER = "– nicht im Mixer –"


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
        root.title("Linewave")
        root.geometry("1140x660")
        root.minsize(900, 560)
        root.configure(background=COL["bg"])
        root.protocol("WM_DELETE_WINDOW", self.hide)
        self._style()
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
                             ident="linewave", open_label="Linewave öffnen")
        if hidden:
            root.withdraw()
        self.refresh()
        self._poll()

    # ============================================================ Fenster
    def request_show(self):
        self._want_show.set()

    def request_quit(self):
        self._want_quit.set()

    def show(self):
        self.root.deiconify()
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
            notify("Linewave läuft weiter", "Mixer und Mikrofon bleiben aktiv. Öffnen über das Menü „Linewave“.")

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
                self.status(f"Fehler: {err}")
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
        s.configure("Strip.TLabel", background=panel, font=("TkDefaultFont", 10, "bold"))
        s.configure("Section.TLabel", background=panel, foreground=COL["muted"], font=("TkDefaultFont", 9, "bold"))
        s.configure("Status.TLabel", background=COL["panel2"], foreground=COL["muted"], padding=(10, 4))
        s.configure("TButton", background=COL["panel2"], foreground=fg, padding=(8, 3), borderwidth=1, width=-4)
        s.map("TButton", background=[("active", "#3a3d45"), ("disabled", bg)],
              foreground=[("disabled", "#5c5e66")])
        s.configure("Accent.TButton", background=COL["accent"])
        s.map("Accent.TButton", background=[("active", "#3b9bff")])
        s.configure("TCheckbutton", background=panel, foreground=fg)
        s.map("TCheckbutton", background=[("active", panel)], indicatorcolor=[("selected", COL["accent"])])
        s.configure("Mute.TCheckbutton", background=panel, foreground=fg)
        s.map("Mute.TCheckbutton", indicatorcolor=[("selected", COL["danger"])], background=[("active", panel)])
        s.configure("TCombobox", foreground=fg, fieldbackground=COL["panel2"], background=COL["panel2"],
                    arrowcolor=fg)
        s.map("TCombobox", fieldbackground=[("readonly", COL["panel2"])], foreground=[("readonly", fg)],
              selectbackground=[("readonly", COL["panel2"])], selectforeground=[("readonly", fg)])
        s.configure("TScale", background=panel, troughcolor=COL["bg"])
        s.configure("TNotebook", background=bg, borderwidth=0)
        s.configure("TNotebook.Tab", background=COL["panel2"], foreground=fg, padding=(16, 6))
        s.map("TNotebook.Tab", background=[("selected", COL["accent"])])
        self.root.option_add("*TCombobox*Listbox.background", COL["panel2"])
        self.root.option_add("*TCombobox*Listbox.foreground", fg)

    def _build(self):
        r = self.root
        head = ttk.Frame(r, style="Panel.TFrame", padding=(14, 10))
        head.pack(fill="x")
        ttk.Label(head, text="Linewave", style="Head.TLabel").pack(side="left")
        self.mic_head = tk.StringVar(value="Mikrofon wird gesucht …")
        ttk.Label(head, textvariable=self.mic_head, style="Muted.TLabel").pack(side="left", padx=16)
        ttk.Button(head, text="Beenden", command=self.quit_app).pack(side="right")
        ttk.Button(head, text="Fenster schließen", command=self.hide).pack(side="right", padx=6)

        self.nb = ttk.Notebook(r)
        self.nb.pack(fill="both", expand=True, padx=10, pady=10)
        self.tab_mixer = ttk.Frame(self.nb, style="Panel.TFrame", padding=12)
        self.tab_mic = ttk.Frame(self.nb, style="Panel.TFrame", padding=16)
        self.nb.add(self.tab_mixer, text="Mixer")
        self.nb.add(self.tab_mic, text="Mikrofon")
        self._build_mixer()
        self._build_mic()

        self.status_var = tk.StringVar(value="")
        ttk.Label(r, textvariable=self.status_var, style="Status.TLabel", anchor="w").pack(fill="x", side="bottom")

    # -------------------------------------------------------------- Mixer
    def _build_mixer(self):
        t = self.tab_mixer
        self.mix_off = ttk.Frame(t, style="Panel.TFrame")
        ttk.Label(self.mix_off, style="Panel.TLabel", justify="left", wraplength=640,
                  text="Der Mixer ist nicht eingerichtet.\n\nWie in Elgato Wave Link bekommt jedes Programm einen "
                       "Kanal (System, Musik, Browser, Voice Chat, Spiel, Effekte) mit eigener Lautstärke für den "
                       "Monitor-Mix (Kopfhörer) und den Stream-Mix (OBS/Discord: „Monitor of Linewave Stream-Mix“)."
                  ).pack(anchor="w")
        ttk.Button(self.mix_off, text="Mixer einrichten", style="Accent.TButton",
                   command=lambda: self._mixer_enable(True)).pack(anchor="w", pady=14)

        self.mix_on = ttk.Frame(t, style="Panel.TFrame")
        self.strip_row = ttk.Frame(self.mix_on, style="Panel.TFrame")
        self.strip_row.pack(fill="x")
        bar = ttk.Frame(self.mix_on, style="Panel.TFrame")
        bar.pack(fill="x", pady=(12, 4))
        ttk.Label(bar, text="Monitor-Ausgang (Kopfhörer):", style="Panel.TLabel").pack(side="left")
        self.out_var = tk.StringVar()
        self.out_box = ttk.Combobox(bar, textvariable=self.out_var, state="readonly", width=42)
        self.out_box.pack(side="left", padx=8)
        self.out_box.bind("<<ComboboxSelected>>", self._on_output)
        ttk.Button(bar, text="Mixer entfernen", command=lambda: self._mixer_enable(False)).pack(side="right")
        ttk.Label(self.mix_on, text="PROGRAMME", style="Section.TLabel").pack(anchor="w", pady=(10, 4))
        self.streams_frame = ttk.Frame(self.mix_on, style="Panel.TFrame")
        self.streams_frame.pack(fill="both", expand=True)

    def _mixer_enable(self, on):
        self.status("Mixer wird eingerichtet …" if on else "Mixer wird entfernt …")
        self._strip_ids = None

        def done(_res, err):
            if err is None:
                self.status("Mixer eingerichtet" if on else "Mixer entfernt")
            self.refresh()
        self.worker.submit(lambda: self.host.enable_mixer(on), done)

    def _build_strips(self, channels):
        for w in self.strip_row.winfo_children():
            w.destroy()
        self.strips = {}
        for cid, name in channels:
            if cid == "mic":
                ttk.Separator(self.strip_row, orient="vertical").pack(side="left", fill="y", padx=6)
            f = ttk.Frame(self.strip_row, style="Panel.TFrame", padding=(2, 4))
            f.pack(side="left", fill="y", padx=2)
            ttk.Label(f, text=name, style="Strip.TLabel").grid(row=0, column=0, columnspan=2, pady=(0, 6))
            strip = {}
            for col, mix in enumerate(("monitor", "stream")):
                var = tk.DoubleVar(value=0)
                val = tk.StringVar(value="–")
                mute = tk.BooleanVar(value=False)
                ttk.Label(f, text=MIX_LABEL[mix], style="Muted.TLabel").grid(row=1, column=col)
                sc = ttk.Scale(f, from_=100, to=0, orient="vertical", length=170, variable=var,
                               command=lambda v, c=cid, m=mix: self._on_level(c, m, v))
                sc.grid(row=2, column=col, padx=3, pady=2)
                ttk.Label(f, textvariable=val, style="Panel.TLabel", width=5, anchor="center").grid(row=3, column=col)
                ttk.Checkbutton(f, text="Stumm", variable=mute, style="Mute.TCheckbutton",
                                command=lambda c=cid, m=mix: self._on_mute(c, m)).grid(row=4, column=col)
                strip[mix] = {"var": var, "val": val, "mute": mute, "scale": sc}
            self.strips[cid] = strip

    def _on_level(self, cid, mix, value):
        pct = int(round(float(value)))
        self.strips[cid][mix]["val"].set(f"{pct} %")
        self._debounced(f"lvl:{cid}:{mix}", lambda v: self.mixer.set_volume(cid, mix, v), pct)

    def _on_mute(self, cid, mix):
        on = self.strips[cid][mix]["mute"].get()
        self._touch(f"mute:{cid}:{mix}")
        self.worker.submit(lambda: self.mixer.set_mute(cid, mix, on))

    def _on_output(self, _e=None):
        label = self.out_var.get()
        name = next((n for n, d in self.state.get("outputs", []) if d == label), None)
        if not name:
            return
        self._touch("output")

        def done(res, err):
            if err is None:
                self.status(f"Monitor-Mix → {label}")
        self.worker.submit(lambda: self.mixer.set_monitor_output(name), done)

    def _build_streams(self, streams, channels):
        for w in self.streams_frame.winfo_children():
            w.destroy()
        names = [n for cid, n in channels if cid != "mic"]
        ids = {n: cid for cid, n in channels}
        if not streams:
            ttk.Label(self.streams_frame, text="Gerade spielt kein Programm Ton ab.", style="Muted.TLabel").pack(
                anchor="w")
            return
        for i, s in enumerate(streams):
            ttk.Label(self.streams_frame, text=s["app"], style="Panel.TLabel", width=32, anchor="w").grid(
                row=i, column=0, sticky="w", pady=2)
            var = tk.StringVar(value=next((n for cid, n in channels if cid == s["channel"]), NOT_IN_MIXER))
            box = ttk.Combobox(self.streams_frame, textvariable=var, values=names, state="readonly", width=20)
            box.grid(row=i, column=1, padx=8, pady=2)
            box.bind("<<ComboboxSelected>>",
                     lambda e, s=s, v=var: self._on_stream(s, ids.get(v.get())))

    def _on_stream(self, s, cid):
        if not cid:
            return
        self._touch("streams")

        def done(res, err):
            if err is None:
                self.status(f"{s['app']} → Kanal „{cid}“ (wird gemerkt)")
            self.refresh()
        self.worker.submit(lambda: self.mixer.move_stream(s["index"], cid, remember=s.get("key", "")), done)

    # ------------------------------------------------------------ Mikrofon
    def _build_mic(self):
        t = self.tab_mic
        self.mic_name = tk.StringVar(value="")
        ttk.Label(t, textvariable=self.mic_name, style="Head.TLabel").grid(row=0, column=0, columnspan=4, sticky="w")
        self.mic_hint = tk.StringVar(value="")
        ttk.Label(t, textvariable=self.mic_hint, style="Muted.TLabel", wraplength=640, justify="left").grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(2, 12))

        self.v = {k: tk.BooleanVar(value=False) for k in ("mute", "lowcut", "clipguard", "leds_off", "leds_flip",
                                                            "headphone_mute")}
        self.gain = tk.DoubleVar(value=0)
        self.gain_val = tk.StringVar(value="–")
        ttk.Label(t, text="Gain", style="Panel.TLabel").grid(row=2, column=0, sticky="w")
        self.gain_scale = ttk.Scale(t, from_=0, to=100, orient="horizontal", length=320, variable=self.gain,
                                    command=self._on_gain)
        self.gain_scale.grid(row=2, column=1, sticky="w", padx=8)
        ttk.Label(t, textvariable=self.gain_val, style="Panel.TLabel", width=18).grid(row=2, column=2, sticky="w")
        ttk.Checkbutton(t, text="Stumm", variable=self.v["mute"], style="Mute.TCheckbutton",
                        command=self._on_mic_mute).grid(row=2, column=3, sticky="w")

        ttk.Checkbutton(t, text="Low Cut", variable=self.v["lowcut"],
                        command=lambda: self._on_filter("lowcut")).grid(row=3, column=0, sticky="w", pady=(12, 2))
        self.freq = tk.StringVar(value="80")
        self.freq_box = ttk.Combobox(t, textvariable=self.freq, values=["80", "120"], state="readonly", width=6)
        self.freq_box.grid(row=3, column=1, sticky="w", padx=8, pady=(12, 2))
        self.freq_box.bind("<<ComboboxSelected>>", self._on_freq)
        self.freq_unit = ttk.Label(t, text="Hz (Software-Filter)", style="Muted.TLabel")
        self.freq_unit.grid(row=3, column=1, sticky="w", padx=(80, 0), pady=(12, 2))
        ttk.Checkbutton(t, text="Clipguard", variable=self.v["clipguard"],
                        command=lambda: self._on_filter("clipguard")).grid(row=4, column=0, sticky="w", pady=2)

        self.hw_frame = ttk.Frame(t, style="Panel.TFrame")
        self.hw_frame.grid(row=5, column=0, columnspan=4, sticky="w", pady=(16, 0))
        h = self.hw_frame
        ttk.Label(h, text="WAVE:3", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 6))
        ttk.Checkbutton(h, text="LEDs aus", variable=self.v["leds_off"],
                        command=lambda: self._on_hw_bool("leds_off")).grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(h, text="LEDs umkehren (Mikrofon hängend)", variable=self.v["leds_flip"],
                        command=lambda: self._on_hw_bool("leds_flip")).grid(row=1, column=1, sticky="w", padx=8)
        self.hp = tk.DoubleVar(value=-20)
        self.hp_val = tk.StringVar(value="–")
        ttk.Label(h, text="Kopfhörer", style="Panel.TLabel").grid(row=2, column=0, sticky="w", pady=(10, 2))
        ttk.Scale(h, from_=-60, to=0, orient="horizontal", length=320, variable=self.hp,
                  command=lambda v: self._on_hw_level("headphone_volume", v, self.hp_val, "{:.0f} dB")).grid(
            row=2, column=1, sticky="w", padx=8, pady=(10, 2))
        ttk.Label(h, textvariable=self.hp_val, style="Panel.TLabel", width=9).grid(row=2, column=2, sticky="w",
                                                                                  pady=(10, 2))
        ttk.Checkbutton(h, text="Stumm", variable=self.v["headphone_mute"], style="Mute.TCheckbutton",
                        command=lambda: self._on_hw_bool("headphone_mute")).grid(row=2, column=3, sticky="w",
                                                                                pady=(10, 2))
        self.dm = tk.DoubleVar(value=50)
        self.dm_val = tk.StringVar(value="–")
        ttk.Label(h, text="Mithören", style="Panel.TLabel").grid(row=3, column=0, sticky="w", pady=2)
        ttk.Scale(h, from_=0, to=100, orient="horizontal", length=320, variable=self.dm,
                  command=lambda v: self._on_hw_level("direct_monitor", v, self.dm_val, "{:.0f} % Mikro")).grid(
            row=3, column=1, sticky="w", padx=8, pady=2)
        ttk.Label(h, textvariable=self.dm_val, style="Panel.TLabel", width=12).grid(row=3, column=2, sticky="w")
        self.hw_frame.grid_remove()

    def _on_gain(self, value):
        pct = int(round(float(value)))
        self.gain_val.set(f"{pct} %")
        self._debounced("gain", self.mic.set_gain, pct)

    def _on_mic_mute(self):
        self._touch("mute")
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
                self.status(f"{'Low Cut' if key == 'lowcut' else 'Clipguard'} {'an' if on else 'aus'}")
        self.worker.submit(work, done)

    def _on_freq(self, _e=None):
        f = int(self.freq.get())
        self._touch("lowcut_freq")
        self.worker.submit(lambda: self.mic.set(lowcut_freq=f))

    def _on_hw_bool(self, key):
        on = self.v[key].get()
        self._touch(key)
        self.worker.submit(lambda: self.mic.hw_set(key, on))

    def _on_hw_level(self, key, value, label, fmt):
        v = float(value)
        label.set(fmt.format(v))
        self._debounced(key, lambda x: self.mic.hw_set(key, x), v)

    # ------------------------------------------------- Regler entprellen
    def _touch(self, key):
        self._touched[key] = time.time()

    def _held(self, key):
        return time.time() - self._touched.get(key, 0) < TOUCH_HOLD

    def _debounced(self, key, fn, value):
        self._touch(key)
        self._pending[key] = (fn, value)
        if key not in self._timers:
            self._timers[key] = self.root.after(120, lambda: self._fire(key))

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
            st["channels"] = [(c["id"], c["name"]) for c in self.mixer.all_channels()]
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
            ids = tuple(st["channels"])
            if ids != self._strip_ids:
                self._strip_ids = ids
                self._build_strips(st["channels"])
            for key, (vol, mute) in (st.get("levels") or {}).items():
                cid, mix = key.rsplit("_", 1)
                s = self.strips.get(cid, {}).get(mix)
                if not s:
                    continue
                if not self._held(f"lvl:{cid}:{mix}"):
                    s["var"].set(min(100, vol))
                    s["val"].set(f"{vol} %")
                if not self._held(f"mute:{cid}:{mix}"):
                    s["mute"].set(bool(mute))
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
        if hw:
            name = "Elgato Wave:3" + (f" (Firmware-API {mic['api']})" if mic.get("api") else "")
        elif mic.get("source"):
            name = "Elgato Wave"
        else:
            name = "Kein Wave-Mikrofon angeschlossen"
        self.mic_name.set(name)
        self.mic_head.set(name + (" – stumm" if mic.get("mute") else ""))
        if hw:
            hint = "Alle Einstellungen werden direkt im Mikrofon gespeichert (wie mit Wave Link)."
        elif mic.get("source"):
            hint = ("Low Cut und Clipguard laufen als PipeWire-Filter. In Discord/OBS das Mikrofon "
                    "„Linewave“ wählen." + ("" if st.get("limiter") else
                                                 " Clipguard braucht das Paket swh-plugins.")
                    + " LED-Steuerung gibt es nur beim Wave:3.")
        else:
            hint = "Mikrofon anschließen – die Einstellungen werden übernommen, sobald es erkannt wird."
        self.mic_hint.set(hint)
        if "gain_pct" in mic and not self._held("gain"):
            self.gain.set(mic["gain_pct"])
            self.gain_val.set(f"{mic['gain_pct']} %" + (f" ({mic['gain']:.1f} dB)" if hw else ""))
        if not self._held("mute"):
            self.v["mute"].set(bool(mic.get("mute")))
        for key in ("lowcut", "clipguard"):
            if not self._held(key):
                self.v[key].set(bool(mic[key]) if hw and key in mic else bool(cfg.get(key)))
        if not self._held("lowcut_freq"):
            self.freq.set(str(cfg.get("lowcut_freq") or 80))
        for w in (self.freq_box, self.freq_unit):  # das Wave:3 filtert mit fester Frequenz selbst
            if hw:
                w.grid_remove()
            else:
                w.grid()
        if hw:
            self.hw_frame.grid()
            for key in ("leds_off", "leds_flip", "headphone_mute"):
                if key in mic and not self._held(key):
                    self.v[key].set(bool(mic[key]))
            if "headphone_volume" in mic and not self._held("headphone_volume"):
                self.hp.set(mic["headphone_volume"])
                self.hp_val.set(f"{mic['headphone_volume']:.0f} dB")
            if "direct_monitor" in mic and not self._held("direct_monitor"):
                self.dm.set(mic["direct_monitor"])
                self.dm_val.set(f"{mic['direct_monitor']:.0f} % Mikro")
        else:
            self.hw_frame.grid_remove()
