"""Einrichtungsassistent von FreeWave – erscheint beim ersten Start.

Schritt für Schritt wie bei Elgato Wave Link:
  1. Willkommen
  2. Mikrofon wählen – mit Pegelanzeige zum Testen
  3. Kopfhörer/Lautsprecher wählen – mit Testton
  4. Mikrofon einstellen: Gain, Low Cut, Clipguard und empfohlene Effekte
  5. Kanäle wählen
  6. Fertig – was in OBS und Discord einzustellen ist

Später jederzeit über „Einrichtung“ oben im Fenster erneut aufrufbar.
"""
import array
import math
import shutil
import subprocess
import threading
import tkinter as tk
import wave

from . import effects, noise, paths, wavemic
from .dialogkit import draw_steps, field
from .i18n import _
from .mixer import DEFAULT_CHANNELS
from .theme import COL, Button, Card, Fader, Icons, Switch, gradient_mark, icon_tile, label, rounded_rect

HIGHPASS = "builtin:highpass"  # Low Cut ohne Wave:3: Effekt am Mikrofon-Kanal
STEPS = [_("Welcome"), _("Microphone"), _("Headphones"), _("Sound"), _("Channels"), _("Done")]
CHANNEL_ICONS = {"system": ("system", "#8e9bb0"), "music": ("music", "#30d158"), "browser": ("browser", "#2f7bff"),
                 "voice": ("chat", "#6b74ff"), "game": ("game", "#ffb020"), "sfx": ("sfx", "#ff6bd5")}


def is_wave(name, desc=""):
    t = f"{name} {desc}".lower()
    return "wave" in t and ("elgato" in t or "0fd9" in t or "wave:" in t or "wave_" in t)


class LevelMeter:
    """Pegel einer Eingangsquelle über `parec` (läuft in einem eigenen Thread)."""

    def __init__(self, source):
        self.level = 0.0
        self.peak = 0.0
        self.proc = None
        exe = shutil.which("parec")
        if not exe or not source:
            return
        try:
            self.proc = subprocess.Popen([exe, f"--device={source}", "--raw", "--format=s16le", "--rate=16000",
                                          "--channels=1", "--latency-msec=40"],
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            self.proc = None
            return
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        proc = self.proc
        while proc and proc.poll() is None:
            data = proc.stdout.read(1280)  # 40 ms
            if not data:
                break
            a = array.array("h", data[: len(data) // 2 * 2])
            peak = max((abs(x) for x in a), default=0) / 32768.0
            db = 20 * math.log10(peak) if peak > 0 else -90
            self.level = max(0.0, min(1.0, (db + 60) / 60))  # −60 dB … 0 dB
            self.peak = max(self.level, self.peak * 0.92)

    @property
    def available(self):
        return self.proc is not None

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.proc = None


def test_tone_file():
    """Kurzer Testton (440 Hz, sanft ein- und ausgeblendet) als WAV."""
    path = paths.STATE_DIR / "freewave-testton.wav"
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    rate, secs = 48000, 0.8
    n = int(rate * secs)
    frames = array.array("h")
    for i in range(n):
        env = min(1.0, i / 2400, (n - i) / 4800)
        v = int(9000 * env * math.sin(2 * math.pi * 440 * i / rate))
        frames.extend((v, v))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames.tobytes())
    return path


class SetupWizard(tk.Toplevel):
    def __init__(self, win, data, on_done=None):
        """data: {"inputs": [(name, desc)], "outputs": [(name, desc)], "default_source", "default_sink",
        "wave3": bool, "catalog": [...]} – im Arbeits-Thread gesammelt."""
        super().__init__(win.root)
        self.win, self.data, self.on_done = win, data, on_done
        self.title(_("Set up FreeWave"))
        self.configure(bg=COL["bg"])
        self.geometry("780x680")
        self.minsize(720, 640)
        self.transient(win.root)
        self.icons = Icons(self)
        self.step = 0
        self.meter = None
        self._meter_job = None
        cfg = win.host.cfg
        mx = cfg.get("mixer") or {}
        ins = data.get("inputs") or []
        wave_src = next((n for n, d in ins if is_wave(n, d)), None)
        self.mic = tk.StringVar(self, value=mx.get("mic") or wave_src or data.get("default_source") or
                                (ins[0][0] if ins else ""))
        outs = data.get("outputs") or []
        self.out = tk.StringVar(self, value=mx.get("monitor_output") or data.get("default_sink") or
                                (outs[0][0] if outs else ""))
        have = {c.get("id") for c in (mx.get("channels") or DEFAULT_CHANNELS)}
        self.channels = {c["id"]: tk.BooleanVar(self, value=c["id"] in have) for c in DEFAULT_CHANNELS}
        sound = self._current_sound(cfg, mx)
        self.lowcut = tk.BooleanVar(self, value=sound["lowcut"])
        self.clipguard = tk.BooleanVar(self, value=sound["clipguard"])
        self.fx_choice = {k: tk.BooleanVar(self, value=sound[k]) for k in ("noise", "gate", "sc4m")}

        self.head = tk.Frame(self, bg=COL["bg"], padx=22, pady=16)
        self.head.pack(fill="x")
        self.dots = tk.Canvas(self.head, height=26, bg=COL["bg"], highlightthickness=0)
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
        self.dots.bind("<Configure>", lambda e: self._dots())
        self.protocol("WM_DELETE_WINDOW", self.skip)
        self.render()
        try:
            self.grab_set()
        except tk.TclError:
            pass

    # -------------------------------------------------------------- Rahmen
    def _dots(self):
        draw_steps(self.dots, STEPS, self.step, self.icons)

    def render(self):
        self._stop_meter()
        for w in self.body.winfo_children():
            w.destroy()
        self._dots()
        getattr(self, f"_step_{self.step}")()
        self.back_btn.configure_text(enabled=0 < self.step < len(STEPS) - 1)
        last = self.step == len(STEPS) - 1
        self.next_btn.configure_text(text=_("Finish") if last else (_("Let's go") if self.step == 0 else _("Next")),
                                     icon="check" if last else "")
        if last:
            self.skip_btn.pack_forget()

    def next(self):
        if self.step == len(STEPS) - 2:
            self.step += 1
            self.render()
            self.apply()
            return
        if self.step == len(STEPS) - 1:
            self.destroy()
            return
        self.step += 1
        self.render()

    def back(self):
        if self.step > 0:
            self.step -= 1
            self.render()

    def skip(self):
        self._stop_meter()
        self.win.host.cfg.data.setdefault("linewave", {})["setup_done"] = True
        self.win.worker.submit(self.win.host.cfg.save)
        self.destroy()

    def destroy(self):
        self._stop_meter()
        super().destroy()

    # --------------------------------------------------------------- Teile
    def _title(self, title, text):
        label(self.body, title, 17, "bold").pack(anchor="w")
        label(self.body, text, 10, color=COL["muted"], wraplength=680, justify="left").pack(anchor="w",
                                                                                           pady=(4, 16))

    def _device_list(self, items, var, icon, on_select=None, badge=None):
        box = tk.Frame(self.body, bg=COL["bg"])
        box.pack(fill="x")
        if not items:
            label(box, _("No device found – is PipeWire (pipewire-pulse) running?"), 10, color=COL["warn"],
                  bg=COL["bg"]).pack(anchor="w")
            return
        rows = []

        def paint():
            for name, row, dot, tile in rows:
                sel = var.get() == name
                bg = COL["accent_soft"] if sel else COL["panel"]
                row.configure(bg=bg, highlightbackground=COL["accent"] if sel else COL["border"])
                for ch in row.winfo_children():
                    ch.configure(bg=bg)
                tile.configure(image=icon_tile(tile, icon, 30, COL["accent"] if sel else COL["raised"],
                                               fg=COL["on_accent"] if sel else COL["muted"]))
                dot.delete("all")
                dot.create_oval(2, 2, 18, 18, outline=COL["accent"] if sel else COL["faint"], width=2)
                if sel:
                    dot.create_oval(6, 6, 14, 14, fill=COL["accent"], outline="")

        def pick(name):
            var.set(name)
            paint()
            if on_select:
                on_select(name)
        for name, desc in items[:8]:
            row = tk.Frame(box, bg=COL["panel"], padx=14, pady=10, cursor="hand2", highlightthickness=1,
                           highlightbackground=COL["border"])
            row.pack(fill="x", pady=(0, 8))
            tile = tk.Label(row, bg=COL["panel"])
            tile.pack(side="left")
            label(row, desc, 10, "bold").pack(side="left", padx=12)
            if badge and badge(name, desc):
                label(row, "Elgato Wave", 9, "bold", color=COL["ok"]).pack(side="left")
            dot = tk.Canvas(row, width=20, height=20, highlightthickness=0, bg=COL["panel"])
            dot.pack(side="right")
            for w in (row, *row.winfo_children()):
                w.bind("<Button-1>", lambda e, n=name: pick(n))
            rows.append((name, row, dot, tile))
        paint()

    # ------------------------------------------------------------- Schritte
    def _step_0(self):
        b = self.body
        tk.Label(b, image=gradient_mark(b, 84, "wave"), bg=COL["bg"]).pack(pady=(40, 20))
        label(b, _("Welcome to FreeWave"), 20, "bold").pack()
        label(b, _("In a few steps you set up your microphone, your headphones and the mixer – just like "
                   "the first start of Elgato Wave Link. You can change everything later at any time."),
              10, color=COL["muted"], wraplength=520, justify="center").pack(pady=(8, 0))

    def _step_1(self):
        self._title(_("Which microphone do you use?"),
                    _("The microphone goes into your Stream Mix. Say something – the bar below shows whether "
                      "it is coming through."))
        self._device_list(self.data.get("inputs") or [], self.mic, "mic", on_select=self._start_meter,
                          badge=is_wave)
        card = Card(self.body, pad=16, stretch=True)
        card.pack(fill="x", pady=(10, 0))
        lv = card.body
        field(lv, _("LEVEL"))
        self.meter_cv = tk.Canvas(lv, height=16, bg=COL["panel"], highlightthickness=0)
        self.meter_cv.pack(fill="x", pady=(2, 0))
        self.meter_hint = label(lv, "", 9, color=COL["muted"])
        self.meter_hint.pack(anchor="w", pady=(4, 0))
        self._start_meter(self.mic.get())

    def _step_2(self):
        self._title(_("What do you listen on?"),
                    _("This is where your Personal Mix goes – everything you want to hear yourself. Use the "
                      "test tone to check that it is the right device."))
        self._device_list(self.data.get("outputs") or [], self.out, "headphones")
        Button(self.body, _("Play test tone"), icon="play", command=self._tone).pack(anchor="w", pady=(10, 0))

    def _step_3(self):
        wave3 = self.data.get("wave3") and is_wave(self.mic.get())
        self._title(_("Your microphone's sound"),
                    _("Your Wave:3 applies Low Cut and Clipguard directly in the microphone. You can also turn "
                      "on recommended effects – just like the plugins in Wave Link.")
                    if wave3 else
                    _("Low Cut and Clipguard run as filters in FreeWave. You can also turn on recommended "
                      "effects – just like the plugins in Wave Link."))
        card = Card(self.body, pad=14, stretch=True)
        card.pack(fill="x")

        def row(parent, title, text, var=None, enabled=True, sw_cb=None):
            if parent.winfo_children():
                tk.Frame(parent, bg=COL["border"], height=1).pack(fill="x", pady=6)
            r = tk.Frame(parent, bg=COL["panel"])
            r.pack(fill="x")
            t = tk.Frame(r, bg=COL["panel"])
            t.pack(side="left", fill="x", expand=True)
            label(t, title, 11, "bold", color=COL["fg"] if enabled else COL["faint"]).pack(anchor="w")
            label(t, text, 9, color=COL["muted"], wraplength=500, justify="left").pack(anchor="w")
            sw = Switch(r)
            sw.pack(side="right")
            if var is not None:
                sw.set(var.get())
                sw.command = lambda: var.set(sw.get())
            sw.set_enabled(enabled)
            return sw
        row(card.body, "Low Cut", _("Removes hum and footfall noise"), self.lowcut)
        row(card.body, "Clipguard", _("Prevents clipping when you suddenly get loud"), self.clipguard,
            enabled=wave3 or bool(self._plugin("fastLookaheadLimiter")))
        if wave3:
            tk.Frame(card.body, bg=COL["border"], height=1).pack(fill="x", pady=6)
            g = tk.Frame(card.body, bg=COL["panel"])
            g.pack(fill="x")
            label(g, "Gain", 11, "bold", width=10, anchor="w").pack(side="left")
            self.gain = Fader(g, length=380, thickness=28, orient="horizontal", color=COL["accent"])
            self.gain.set(self.data.get("gain_pct", 70))
            self.gain.pack(side="left")
        field(self.body, _("RECOMMENDED EFFECTS"), pady=(14, 6))
        fx = Card(self.body, pad=14, stretch=True)
        fx.pack(fill="x")
        # Rauschunterdrückung: eigene Stufe vor den Effekten (noise.py, RNNoise oder PipeWire WebRTC)
        have = self.data.get("noise") or []
        var = self.fx_choice["noise"]
        if have:
            text = _("Filters out keyboard, fans and background noise ({backend})").format(
                backend=noise.backend_name(have[0]))
        else:
            var.set(False)
            text = noise.install_hint()
        row(fx.body, _("Noise suppression"), text, var, enabled=bool(have))
        for key, label_, title, text in (
                ("gate", "gate", _("Noise Gate"), _("Mutes the microphone while you pause speaking")),
                ("sc4m", "comp", _("Compressor"), _("Evens out loud and quiet passages"))):
            p = self._plugin(key)
            var = self.fx_choice[key]
            if not p:
                var.set(False)
                text = _("{text} – package “{package}” is missing").format(text=text, package="swh-plugins")
            row(fx.body, title, text, var, enabled=bool(p))

    def _step_4(self):
        self._title(_("Which channels do you want?"),
                    _("Every app goes into a channel with its own controls for headphones and stream. You can "
                      "create your own channels (e.g. for a capture card) later with “Add channel”."))
        grid = tk.Frame(self.body, bg=COL["bg"])
        grid.pack(fill="x")
        for i, c in enumerate(DEFAULT_CHANNELS):
            glyph, color = CHANNEL_ICONS.get(c["id"], ("wave", "#8e9bb0"))
            cell = Card(grid, pad=14, stretch=True)
            cell.grid(row=i // 2, column=i % 2, sticky="we", padx=(0 if i % 2 == 0 else 12, 0), pady=6)
            tk.Label(cell.body, image=icon_tile(cell.body, glyph, 34, color), bg=COL["panel"]).pack(side="left")
            label(cell.body, c["name"], 11, "bold").pack(side="left", padx=12)
            sw = Switch(cell.body)
            sw.set(self.channels[c["id"]].get())
            sw.command = lambda sw=sw, cid=c["id"]: self.channels[cid].set(sw.get())
            sw.pack(side="right")
        grid.grid_columnconfigure(0, weight=1)
        grid.grid_columnconfigure(1, weight=1)

    def _step_5(self):
        b = self.body
        tk.Label(b, image=icon_tile(b, "check", 56, COL["ok"], radius=0.5), bg=COL["bg"]).pack(pady=(6, 12))
        label(b, _("All set!"), 18, "bold").pack()
        self.result = label(b, _("Setting up mixer …"), 10, color=COL["muted"])
        self.result.pack(pady=(4, 16))
        card = Card(b, pad=18, stretch=True)
        card.pack(fill="x")
        fx_on = any(v.get() for v in self.fx_choice.values()) or (self.lowcut.get() and not self.data.get("wave3"))
        tips = [("stream", "OBS Studio",
                 _("Add the audio input “Monitor of FreeWave Stream Mix” – it contains everything "
                   "your audience should hear.")),
                ("chat", "Discord, Zoom & Co.",
                 _("Select “FreeWave Microphone (Effects)” as the microphone.") if fx_on
                 else _("Select your own microphone – or the Stream Mix if others should hear everything.")),
                ("grid", _("Apps"), _("New apps automatically go into the matching channel; you can move them to "
                                      "another channel in the mixer under “Apps”."))]
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
            label(t, text, 9, color=COL["muted"], wraplength=580, justify="left").pack(anchor="w", pady=(2, 0))

    # -------------------------------------------------------------- Helfer
    def _current_sound(self, cfg, mx):
        """Anfangswerte des Schritts „Klang“: beim ersten Mal die Empfehlung. Wird der Assistent erneut
        aufgerufen, der jetzige Stand – „Fertig“ ändert dann nur, was man umschaltet (self._fx_on)."""
        self._fx_on = None  # None: erste Einrichtung – alle Effekte des Assistenten setzen
        self._noise_on = False
        if not ((cfg.get("linewave") or {}).get("setup_done") or mx.get("enabled")):
            return {"lowcut": True, "clipguard": True, "noise": bool(self.data.get("noise")),
                    "gate": bool(self._plugin("gate")), "sc4m": False}
        chain = (mx.get("effects") or {}).get("mic") or []
        on = self._fx_on = {fx.get("plugin") for fx in chain if fx.get("enabled", True)} - {None}
        self._noise_on = bool((mx.get("noise") or {}).get("enabled"))
        wave3 = bool(self.data.get("wave3")) and is_wave(self.mic.get())
        # Low Cut/Clipguard im Wave:3 oder als alter Mikrofonfilter (ohne Wave:3) – sonst als Effekt der Kette
        filters = self.win.host.mic.settings if wave3 or not self.data.get("wave3") else {}
        lim, gate, comp = ((self._plugin(k) or {}).get("id") for k in ("fastLookaheadLimiter", "gate", "sc4m"))
        return {"lowcut": bool(filters.get("lowcut")) or (not wave3 and HIGHPASS in on),
                "clipguard": bool(filters.get("clipguard")) or (not wave3 and lim in on),
                "noise": self._noise_on, "gate": gate in on, "sc4m": comp in on}

    def _plugin(self, label_):
        return next((p for p in self.data.get("catalog") or [] if p.get("label") == label_), None)

    def _start_meter(self, source):
        self._stop_meter()
        self.meter = LevelMeter(source)
        if hasattr(self, "meter_hint") and self.meter_hint.winfo_exists():
            self.meter_hint.configure(text="" if self.meter.available else
                                      _("Level meter not available (parec from pulseaudio-utils is missing)"))
        self._tick_meter()

    def _tick_meter(self):
        cv = getattr(self, "meter_cv", None)
        if cv is None or not cv.winfo_exists() or self.meter is None:
            return
        cv.delete("all")
        w = cv.winfo_width() or 600
        rounded_rect(cv, 0, 4, w, 12, 4, fill=COL["raised"], outline="")
        lv, pk = self.meter.level, self.meter.peak
        col = COL["ok"] if lv < 0.8 else (COL["warn"] if lv < 0.95 else COL["danger"])
        if lv > 0:
            rounded_rect(cv, 0, 4, max(8, w * lv), 12, 4, fill=col, outline="")
        if pk > 0:
            cv.create_rectangle(w * pk - 2, 1, w * pk, 15, fill=COL["fg"], outline="")
        self._meter_job = self.after(60, self._tick_meter)

    def _stop_meter(self):
        if self._meter_job:
            try:
                self.after_cancel(self._meter_job)
            except tk.TclError:
                pass
            self._meter_job = None
        if self.meter:
            self.meter.stop()
            self.meter = None

    def _tone(self):
        sink, exe = self.out.get(), shutil.which("paplay") or shutil.which("pw-play")
        if not exe:
            return
        args = [exe, f"--device={sink}" if "paplay" in exe else f"--target={sink}", str(test_tone_file())]
        try:
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    # ----------------------------------------------------------- Übernehmen
    def choices(self):
        wave3 = bool(self.data.get("wave3")) and is_wave(self.mic.get())
        # Effekte des Assistenten in seiner Reihenfolge, je (Plugin, gewünscht); beim Wave:3 filtert das
        # Mikrofon selbst (Low Cut, Clipguard)
        offer = [] if wave3 else [(effects.lookup(HIGHPASS), self.lowcut.get())]
        offer += [(self._plugin(key), self.fx_choice[key].get()) for key in ("gate", "sc4m")]
        if not wave3:
            offer.append((self._plugin("fastLookaheadLimiter"), self.clipguard.get()))
        offer = [(p, bool(on)) for p, on in offer if p]
        # nur Umgeschaltetes ändern (erneut aufgerufen): eigene Effekte und eingestellte Regler bleiben
        fx_set = {p["id"]: on for p, on in offer if self._fx_on is None or on != (p["id"] in self._fx_on)}
        out = {"mic": self.mic.get(), "output": self.out.get(), "wave3": wave3,
               "lowcut": self.lowcut.get(), "clipguard": self.clipguard.get(),
               "gain": self.gain.get() if wave3 and hasattr(self, "gain") else None,
               "channels": [cid for cid, v in self.channels.items() if v.get()],
               "mic_fx": [effects.new_effect(p) for p, on in offer if on], "mic_fx_set": fx_set}
        noise = bool(self.data.get("noise")) and bool(self.fx_choice["noise"].get())
        if self._fx_on is None or (self.data.get("noise") and noise != self._noise_on):
            out["noise"] = noise
        return out

    def apply(self):
        ch = self.choices()
        host = self.win.host

        def work():
            return apply_choices(host, ch)

        def done(res, err):
            if not self.winfo_exists():
                return
            if err is not None:
                self.result.configure(text=_("Error: {error}").format(error=err), fg=COL["danger"])
            else:
                self.result.configure(text=_("Mixer and microphone are ready."), fg=COL["ok"])
            if self.on_done:
                self.on_done()
        self.win.worker.submit(work, done)


def apply_choices(host, ch):
    """Auswahl des Assistenten speichern und den Mixer einrichten (läuft im Arbeits-Thread)."""
    cfg = host.cfg
    mx = cfg.data.setdefault("mixer", {})
    if ch.get("mic"):
        mx["mic"] = ch["mic"]
    if ch.get("output"):
        mx["monitor_output"] = ch["output"]
    # bestehende Kanäle bleiben, wie sie sind (Name, Symbol, Farbe, Reihenfolge); abgewählte Standardkanäle
    # gehen, neu gewählte kommen hinten dazu
    defaults = {d["id"] for d in DEFAULT_CHANNELS}
    want = set(ch.get("channels", []))
    chans = [c for c in (mx.get("channels") or []) if c.get("id") not in defaults or c.get("id") in want]
    have = {c.get("id") for c in chans}
    chans += [dict(c) for c in DEFAULT_CHANNELS if c["id"] in want and c["id"] not in have]
    if not any(c.get("kind", "apps") == "apps" for c in chans):
        chans.insert(0, dict(DEFAULT_CHANNELS[0]))  # mindestens ein Kanal für Programme
    mx["channels"] = chans
    fx = mx.setdefault("effects", {})
    chain = ch.get("mic_fx") or []
    if ch.get("mic_fx_set") is not None:
        chain = merge_chain(fx.get("mic") or [], chain, ch["mic_fx_set"])
    if chain:
        fx["mic"] = chain
    else:
        fx.pop("mic", None)
    if "noise" in ch:
        mx.setdefault("noise", {})["enabled"] = bool(ch["noise"])
    cfg.data.setdefault("linewave", {})["setup_done"] = True
    cfg.save()
    mic = host.mic
    if ch.get("wave3") and mic.hw():
        for key in ("lowcut", "clipguard"):
            try:
                mic.hw_set(key, bool(ch.get(key)))
            except RuntimeError:
                pass
        if ch.get("gain") is not None:
            try:
                mic.set_gain(int(ch["gain"]))
            except RuntimeError:
                pass
        mic.settings.update(lowcut=bool(ch.get("lowcut")), clipguard=bool(ch.get("clipguard")))
        mic._save()
    elif mic.settings.get("lowcut") or mic.settings.get("clipguard"):
        # Low Cut/Clipguard laufen jetzt als Effekte des Mikrofon-Kanals
        mic.set(lowcut=False, clipguard=False)
    host.enable_mixer(True)
    return True


def merge_chain(old, new, changes):
    """Effekte des Assistenten in die Kette des Mikrofons übernehmen.

    new: die gewünschten Effekte des Assistenten in seiner Reihenfolge; changes: {Plugin-id: an/aus} nur für
    umgeschaltete. Abgeschaltete gehen, eingeschaltete bleiben samt ihren Reglern (und sind an) oder kommen vor
    den ersten Effekt, der beim Assistenten danach kommt. Alles andere bleibt, wie und wo es ist."""
    out = []
    for fx in old:
        on = changes.get(fx.get("plugin"))
        if on is False:
            continue
        out.append(dict(fx, enabled=True) if on else fx)
    have = {fx.get("plugin") for fx in out}
    for i, fx in enumerate(new):
        pid = fx.get("plugin")
        if not changes.get(pid) or pid in have:
            continue
        later = {f.get("plugin") for f in new[i + 1:]}
        out.insert(next((k for k, f in enumerate(out) if f.get("plugin") in later), len(out)), fx)
        have.add(pid)
    return out


def collect(host):
    """Daten für den Assistenten (Geräte, Standardgeräte, Plugins) – im Arbeits-Thread."""
    mx = host.mixer
    out = {"inputs": mx.inputs(), "outputs": [(o["name"], o.get("description") or o["name"]) for o in mx.outputs()],
           "catalog": effects.catalog(), "wave3": bool(host.mic.hw()), "noise": noise.available()}
    for key, fn in (("default_source", mx.default_source), ("default_sink", mx.default_sink)):
        try:
            v = fn()
            out[key] = "" if v.startswith(("linewave_", "lindeck_")) else v
        except Exception:  # noqa: BLE001
            out[key] = ""
    if out["wave3"]:
        try:
            out["gain_pct"] = host.mic.gain()[0]
        except Exception:  # noqa: BLE001
            pass
    src = wavemic.find_source()
    if src and src not in {n for n, _d in out["inputs"]}:
        out["inputs"].insert(0, (src, "Elgato Wave"))
    return out
