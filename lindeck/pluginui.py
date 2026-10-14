"""Plugin-Verwaltung im LinDeck-Fenster (wie „Plugins“ in der Stream-Deck-Software).

Zeigt alle installierten Elgato-Plugins mit Status, installiert neue
(.streamDeckPlugin), startet sie neu oder entfernt sie. Liegt eine
.streamDeckPlugin-Datei im Download-Ordner, installiert LinDeck sie ohnehin
automatisch.
"""
import shutil
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from . import paths, plugins
from .theme import COL, Card, Dialog, Icons, PillButton, label

MARKET = "https://marketplace.elgato.com/stream-deck/plugins"


def node_version(node):
    if not node:
        return ""
    try:
        return subprocess.run([node, "--version"], capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def status(info, host):
    """(Text, Farbe) für ein Plugin."""
    if not info.runnable:
        return info.reason, COL["danger"]
    if host is None:
        return "bereit", COL["muted"]
    if info.runtime == "node" and not host.node:
        return "Node.js fehlt (Paket nodejs)", COL["warn"]
    proc = host.procs.get(info.uuid)
    if proc is None or proc.proc is None:
        return "wird gestartet …", COL["warn"]
    if proc.proc.poll() is not None:
        return "beendet – siehe Log", COL["danger"]
    if proc.conn is None:
        return "startet …", COL["warn"]
    return "läuft", COL["ok"]


def _plugin_icon(info, size=48):
    rel = info.manifest.get("Icon")
    path = plugins._image_file(info.dir / rel) if rel else None
    if path and path.suffix.lower() != ".svg":
        try:
            return Image.open(path).convert("RGBA").resize((size, size), Image.LANCZOS)
        except OSError:
            return None
    if path:
        try:
            import io

            import cairosvg
            png = cairosvg.svg2png(url=str(path), output_width=size, output_height=size)
            return Image.open(io.BytesIO(png)).convert("RGBA")
        except Exception:  # noqa: BLE001
            return None
    return None


class PluginManager(Dialog):
    def __init__(self, master, host=None, on_change=None):
        super().__init__(master, "Plugins", width=720, height=640)
        self.host = host
        self.on_change = on_change
        self.icons = Icons(self)
        self._photos = []
        self._rows = {}
        b = self.body
        label(b, "Plugins", 16, "bold").pack(anchor="w")
        label(b, "Elgato-Plugins aus dem Marketplace laufen auch unter Linux, wenn sie mit Node.js gebaut sind "
                 "(die meisten neuen Plugins). Lade die Datei „….streamDeckPlugin“ herunter und installiere sie "
                 "hier – oder lege sie einfach in den Download-Ordner, LinDeck übernimmt sie dann von selbst.",
              9, color=COL["muted"], wraplength=660, justify="left").pack(anchor="w", pady=(4, 10))
        bar = tk.Frame(b, bg=COL["bg"])
        bar.pack(fill="x", pady=(0, 12))
        ttk.Button(bar, text="Plugin installieren …", style="Accent.TButton", command=self.install).pack(side="left")
        ttk.Button(bar, text="Marketplace öffnen", command=lambda: self._open(MARKET)).pack(side="left", padx=8)
        ttk.Button(bar, text="Plugin-Ordner", style="Ghost.TButton",
                   command=lambda: self._open(str(paths.PLUGINS_DIR))).pack(side="left")
        node = host.node if host else (shutil.which("node") or shutil.which("nodejs"))
        ver = node_version(node)
        text = (f"● Node.js {ver} – Plugins können laufen" if ver else
                "● Node.js gefunden – Plugins können laufen" if node else
                "● Node.js fehlt – bitte das Paket „nodejs“ installieren, sonst starten die meisten Plugins nicht")
        label(b, text, 9, color=COL["ok"] if node else COL["warn"], bg=COL["bg"]).pack(anchor="w", pady=(0, 10),
                                                                                      before=bar)

        wrap = tk.Frame(b, bg=COL["bg"])
        wrap.pack(fill="both", expand=True)
        self.cv = tk.Canvas(wrap, bg=COL["bg"], highlightthickness=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.cv.yview)
        self.cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.cv.pack(side="left", fill="both", expand=True)
        self.list = tk.Frame(self.cv, bg=COL["bg"])
        self._win = self.cv.create_window(0, 0, window=self.list, anchor="nw")
        self.list.bind("<Configure>", lambda e: self.cv.configure(scrollregion=self.cv.bbox("all")))
        self.cv.bind("<Configure>", lambda e: self.cv.itemconfigure(self._win, width=e.width))
        ttk.Button(self.buttons, text="Schließen", style="Ghost.TButton", command=self.destroy).pack(side="right")
        self.render()
        self._tick()
        self.show()

    # ------------------------------------------------------------ Anzeige
    def render(self):
        for w in self.list.winfo_children():
            w.destroy()
        self._photos, self._rows = [], {}
        found = plugins.installed()
        if not found:
            card = Card(self.list, pad=24)
            card.pack(fill="x", pady=4)
            tk.Label(card.body, image=self.icons.get("puzzle", 44, "#ffffff", circle=COL["raised"]),
                     bg=COL["panel"]).pack(side="left")
            txt = tk.Frame(card.body, bg=COL["panel"])
            txt.pack(side="left", padx=14)
            label(txt, "Noch keine Plugins installiert", 12, "bold").pack(anchor="w")
            label(txt, "OBS, Spotify, Discord und Wave Link brauchen kein Plugin – das kann LinDeck schon selbst.",
                  9, color=COL["muted"], wraplength=480, justify="left").pack(anchor="w")
            return
        for info in sorted(found.values(), key=lambda i: i.name.lower()):
            self._row(info)

    def _row(self, info):
        card = Card(self.list, pad=14, stretch=True)
        card.pack(fill="x", pady=4)
        b = card.body
        img = _plugin_icon(info)
        if img is not None:
            ph = ImageTk.PhotoImage(img, master=self)
            self._photos.append(ph)
            tk.Label(b, image=ph, bg=COL["panel"]).pack(side="left", anchor="n")
        else:
            tk.Label(b, image=self.icons.get("puzzle", 48, "#ffffff", circle=COL["accent"]), bg=COL["panel"]).pack(
                side="left", anchor="n")
        txt = tk.Frame(b, bg=COL["panel"])
        txt.pack(side="left", padx=14, fill="x", expand=True, anchor="n")
        m = info.manifest
        label(txt, info.name, 12, "bold").pack(anchor="w")
        meta = " · ".join(x for x in (f"Version {info.version}" if info.version else "", m.get("Author") or "",
                                      f"{len(info.actions)} Aktionen") if x)
        label(txt, meta, 9, color=COL["muted"]).pack(anchor="w")
        if m.get("Description"):
            label(txt, m["Description"], 9, color=COL["muted"], wraplength=300, justify="left").pack(anchor="w",
                                                                                                  pady=(2, 0))
        st = tk.StringVar(self, value="")
        stl = label(txt, textvariable=st, size=9, weight="bold")
        stl.pack(anchor="w", pady=(6, 0))
        btns = tk.Frame(b, bg=COL["panel"])
        btns.pack(side="right", anchor="n")
        PillButton(btns, "Entfernen", command=lambda i=info: self.remove(i)).pack(side="right", padx=(6, 0))
        log = paths.STATE_DIR / "plugins" / f"{info.uuid}.log"
        PillButton(btns, "Log", command=lambda p=log: self._open(str(p))).pack(side="right", padx=(6, 0))
        if info.runnable and self.host is not None:
            PillButton(btns, "Neu starten", command=lambda i=info: self.restart(i)).pack(side="right")
        self._rows[info.uuid] = (info, st, stl)
        self._update_status()

    def _update_status(self):
        for info, st, stl in self._rows.values():
            text, color = status(info, self.host)
            st.set("● " + text)
            stl.configure(fg=color)

    def _tick(self):
        self._update_status()
        self._tick_job = self.after(1500, self._tick)

    def destroy(self):
        try:
            self.after_cancel(self._tick_job)
        except (AttributeError, tk.TclError):
            pass
        super().destroy()

    # -------------------------------------------------------------- Aktionen
    def _sync(self):
        if self.host is not None:
            threading.Thread(target=self.host.sync, daemon=True).start()
        if self.on_change:
            self.on_change()

    def install(self):
        files = filedialog.askopenfilenames(
            parent=self, title="Plugin installieren",
            filetypes=[("Stream-Deck-Plugin", "*.streamDeckPlugin *.zip"), ("Alle Dateien", "*")])
        if not files:
            return
        msgs = []
        for f in files:
            try:
                for uid in plugins.install(f):
                    info = plugins.installed().get(uid)
                    msgs.append(f"„{info.name if info else uid}“ installiert" + (
                        "" if not info or info.runnable else f" – läuft nicht unter Linux ({info.reason})"))
            except Exception as e:  # noqa: BLE001
                msgs.append(f"{f}: {e}")
        self._sync()
        self.render()
        if msgs:
            messagebox.showinfo("Plugins", "\n".join(msgs), parent=self)

    def restart(self, info):
        host = self.host

        def work():
            proc = host.procs.pop(info.uuid, None)
            if proc:
                proc.stop()
            host.sync()
        threading.Thread(target=work, daemon=True).start()

    def remove(self, info):
        if not messagebox.askyesno("Plugin entfernen", f"„{info.name}“ entfernen? Tasten mit seinen Aktionen bleiben "
                                   "im Profil erhalten, tun aber nichts mehr.", parent=self):
            return
        plugins.remove(info.uuid)
        self._sync()
        self.render()

    @staticmethod
    def _open(target):
        try:
            subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
