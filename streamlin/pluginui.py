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
from tkinter import filedialog, messagebox

from PIL import Image, ImageTk

from . import paths, plugins
from .i18n import _, ngettext
from .dialogkit import ScrollArea, footer, header
from .theme import COL, Button, Card, Dialog, Icons, icon_tile, label, section

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
        return _("ready"), COL["muted"]
    if info.runtime == "node" and not host.node:
        return _("Node.js missing (package nodejs)"), COL["warn"]
    proc = host.procs.get(info.uuid)
    if proc is None or proc.proc is None:
        return _("launching …"), COL["warn"]
    if proc.proc.poll() is not None:
        return _("exited – see log"), COL["danger"]
    if proc.conn is None:
        return _("starting …"), COL["warn"]
    return _("running"), COL["ok"]


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


def _rounded(img, r):
    from PIL import ImageDraw
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, img.size[0] - 1, img.size[1] - 1), radius=r, fill=255)
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


class PluginManager(Dialog):
    def __init__(self, master, host=None, on_change=None):
        super().__init__(master, _("Plugins"), width=720, height=640)
        self.host = host
        self.on_change = on_change
        self.icons = Icons(self)
        self._photos = []
        self._rows = {}
        b = self.body
        node = host.node if host else (shutil.which("node") or shutil.which("nodejs"))
        ver = node_version(node)
        head = header(b, _("Plugins"),
                      _("Elgato plugins from the Marketplace also run on Linux if they are built with Node.js "
                        "(most new plugins). Download the “….streamDeckPlugin” file and install it here – "
                        "or just put it in your Downloads folder, LinDeck picks it up by itself."),
                      icon="puzzle", wrap=600)
        bar = tk.Frame(b, bg=COL["bg"])
        bar.pack(fill="x", pady=(0, 14))
        Button(bar, _("Install plugin …"), icon="import", kind="primary", command=self.install).pack(side="left")
        Button(bar, _("Open Marketplace"), icon="globe", command=lambda: self._open(MARKET)).pack(side="left",
                                                                                                  padx=8)
        Button(bar, _("Plugin folder"), icon="folder", kind="ghost",
               command=lambda: self._open(str(paths.PLUGINS_DIR))).pack(side="left")
        text = (_("Node.js {version} – plugins can run").format(version=ver) if ver else
                _("Node.js found – plugins can run") if node else
                _("Node.js missing – please install the “nodejs” package, "
                  "otherwise most plugins will not start"))
        st = tk.Frame(head.subtitle.master, bg=COL["bg"])
        st.pack(fill="x", pady=(8, 0))
        dot = tk.Canvas(st, width=8, height=8, bg=COL["bg"], highlightthickness=0)
        dot.create_oval(0, 0, 7, 7, fill=COL["ok"] if node else COL["warn"], outline="")
        dot.pack(side="left", padx=(0, 8))
        label(st, text, 9, color=COL["muted"] if node else COL["warn"], wraplength=580, justify="left").pack(
            side="left")

        section(b, _("Installed")).pack(fill="x", pady=(0, 8))
        self.area = ScrollArea(b)
        self.area.pack(fill="both", expand=True)
        self.cv, self.list = self.area.cv, self.area.inner
        footer(self)
        Button(self.buttons, _("Close"), command=self.destroy).pack(side="right")
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
            card = Card(self.list, pad=22, stretch=True)
            card.pack(fill="x", pady=(0, 8))
            tk.Label(card.body, image=icon_tile(card.body, "puzzle", 44, COL["raised"], fg=COL["muted"]),
                     bg=COL["panel"]).pack(side="left")
            txt = tk.Frame(card.body, bg=COL["panel"])
            txt.pack(side="left", padx=14, fill="x", expand=True)
            label(txt, _("No plugins installed yet"), 12, "bold").pack(anchor="w")
            label(txt, _("OBS, Spotify, Discord and Wave Link don't need a plugin – LinDeck can do that by itself."),
                  9, color=COL["muted"], wraplength=480, justify="left").pack(anchor="w", pady=(2, 0))
            return
        for info in sorted(found.values(), key=lambda i: i.name.lower()):
            self._row(info)

    def _row(self, info):
        card = Card(self.list, pad=16, stretch=True)
        card.pack(fill="x", pady=(0, 10))
        b = card.body
        img = _plugin_icon(info, 44)
        if img is not None:
            ph = ImageTk.PhotoImage(_rounded(img, 10), master=self)
            self._photos.append(ph)
            tk.Label(b, image=ph, bg=COL["panel"]).pack(side="left", anchor="n")
        else:
            tk.Label(b, image=icon_tile(b, "puzzle", 44, COL["accent"]), bg=COL["panel"]).pack(side="left",
                                                                                              anchor="n")
        txt = tk.Frame(b, bg=COL["panel"])
        txt.pack(side="left", padx=14, fill="x", expand=True, anchor="n")
        m = info.manifest
        label(txt, info.name, 11, "bold").pack(anchor="w")
        n = len(info.actions)
        meta = " · ".join(x for x in (_("Version {version}").format(version=info.version) if info.version else "",
                                      m.get("Author") or "",
                                      ngettext("{n} action", "{n} actions", n).format(n=n)) if x)
        label(txt, meta, 9, color=COL["faint"]).pack(anchor="w", pady=(1, 0))
        if m.get("Description"):
            label(txt, m["Description"], 9, color=COL["muted"], wraplength=300, justify="left").pack(anchor="w",
                                                                                                  pady=(4, 0))
        srow = tk.Frame(txt, bg=COL["panel"])
        srow.pack(anchor="w", pady=(8, 0))
        dot = tk.Canvas(srow, width=8, height=8, bg=COL["panel"], highlightthickness=0)
        dot.pack(side="left", padx=(0, 6))
        st = tk.StringVar(self, value="")
        stl = label(srow, textvariable=st, size=9, weight="bold")
        stl.pack(side="left")
        btns = tk.Frame(b, bg=COL["panel"])
        btns.pack(side="right", anchor="n")
        Button(btns, _("Remove"), icon="trash", kind="danger", command=lambda i=info: self.remove(i)).pack(
            side="right", padx=(6, 0))
        log = paths.STATE_DIR / "plugins" / f"{info.uuid}.log"
        Button(btns, _("Log"), icon="text", command=lambda p=log: self._open(str(p))).pack(side="right",
                                                                                         padx=(6, 0))
        if info.runnable and self.host is not None:
            Button(btns, _("Restart"), icon="undo", command=lambda i=info: self.restart(i)).pack(side="right")
        self._rows[info.uuid] = (info, st, stl, dot)
        self._update_status()

    def _update_status(self):
        for info, st, stl, dot in self._rows.values():
            text, color = status(info, self.host)
            st.set(text)
            stl.configure(fg=color)
            dot.delete("all")
            dot.create_oval(0, 0, 7, 7, fill=color, outline="")

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
            parent=self, title=_("Install plugin"),
            filetypes=[(_("Stream Deck plugin"), "*.streamDeckPlugin *.zip"), (_("All files"), "*")])
        if not files:
            return
        msgs = []
        for f in files:
            try:
                for uid in plugins.install(f):
                    info = plugins.installed().get(uid)
                    name = info.name if info else uid
                    msgs.append(_("“{name}” installed").format(name=name) if not info or info.runnable else
                                _("“{name}” installed – does not run on Linux ({reason})").format(
                                    name=name, reason=info.reason))
            except Exception as e:  # noqa: BLE001
                msgs.append(f"{f}: {e}")
        self._sync()
        self.render()
        if msgs:
            messagebox.showinfo(_("Plugins"), "\n".join(msgs), parent=self)

    def restart(self, info):
        host = self.host

        def work():
            proc = host.procs.pop(info.uuid, None)
            if proc:
                proc.stop()
            host.sync()
        threading.Thread(target=work, daemon=True).start()

    def remove(self, info):
        if not messagebox.askyesno(_("Remove plugin"), _(
                "Remove “{name}”? Keys with its actions stay in the profile but no longer do anything.").format(
                name=info.name), parent=self):
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
