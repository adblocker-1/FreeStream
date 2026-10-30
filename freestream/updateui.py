"""Update-Dialog für FreeDeck und FreeWave (Logik in updater.py).

start(): beim Programmstart im Hintergrund nach Updates suchen (wenn eingeschaltet)
und bei einer neuen Version fragen. check_now(): Menüpunkt „Check for updates…“.
choose_earlier(): ältere Version auswählen und installieren (Zurückstufen).
"""
import logging
import subprocess
import threading
import tkinter as tk
import webbrowser
from tkinter import ttk

from . import __version__, desktop, updater
from . import msgbox as messagebox
from .dialogkit import ScrollArea, footer, header, slim_scrollbar
from .i18n import _
from .theme import COL, Button, Card, Dialog, InfoCard, Segmented, Switch, font, label

log = logging.getLogger("freestream.updateui")


def _in_background(root, work, done, interval=200):
    """work() in einem Thread, done(result, error) danach im Tk-Thread."""
    box = {}

    def run():
        try:
            box["result"] = work()
        except Exception as e:  # noqa: BLE001 - Netzfehler, kaputte Antwort …
            box["error"] = e
        box["finished"] = True

    threading.Thread(target=run, name="freestream-update", daemon=True).start()

    def poll():
        if not box.get("finished"):
            root.after(interval, poll)
            return
        done(box.get("result"), box.get("error"))

    root.after(interval, poll)


def start(root, app, restart):
    """Beim Start: Hinweis nach einem Update, dann (falls eingeschaltet) nach Updates suchen."""
    version = updater.just_updated()
    if version:
        desktop.notify(_("FreeStream was updated"), _("Version {version} is installed.").format(version=version),
                       app=app, icon="system-software-update")
    if not updater.installed_packages():
        return
    if version and "edition" not in updater._read_config():
        # erstes Update mit der Wahl Python/Rust: einmal fragen, welche Programme laufen sollen
        _ask_edition(root, app, restart)
        return
    if not updater.enabled():
        return

    def done(update, error):
        if error is not None:
            log.info("Update check failed: %s", error)
            return
        if update is None or updater.suppressed(update.version) or not updater.enabled():
            return
        lock = updater.PromptLock()
        if not lock.acquire():
            return  # das andere Programm fragt schon
        try:
            UpdateDialog(root, update, app, restart, lock=lock, automatic=True)
        except tk.TclError:
            lock.release()

    _in_background(root, updater.check, done)


def _ask_edition(root, app, restart):
    """Nach einem Update einmal fragen: weiter mit Python oder Rust (Beta)? Holt das laufende Release (mit den
    Rust-Paketen für dieses System); ohne Rust-Pakete wird nicht gefragt."""
    def work():
        release = updater._get_first(updater.API_URLS, 10)
        if updater.parse_version(release.get("tag_name") or "") != updater.parse_version(__version__):
            return None
        return updater.make_update(release, updater.packages_for("python"))

    def done(update, error):
        if error is not None or update is None or updater.update_for_edition(update, "rust") is None:
            return
        lock = updater.PromptLock()
        if not lock.acquire():
            return
        try:
            UpdateDialog(root, update, app, restart, lock=lock, choose=True)
        except tk.TclError:
            lock.release()

    _in_background(root, work, done)


def _no_packages(parent, app):
    """Hinweis, wenn sich diese Kopie nicht selbst aktualisieren kann. True: Hinweis gezeigt."""
    if not updater.installed_packages():
        messagebox.showinfo(_("Updates"), _("Automatic updates work for the installed .deb packages only. "
                                            "This copy of {app} was started from the source code.").format(app=app),
                            parent=parent)
        return True
    return False


def check_now(root, app, restart):
    """Menüpunkt: sofort suchen und das Ergebnis immer anzeigen."""
    parent = root.winfo_toplevel()
    if _no_packages(parent, app):
        return

    def done(update, error):
        if error is not None:
            messagebox.showerror(_("Updates"), _("Could not check for updates: {error}").format(error=error),
                                 parent=parent)
        elif update is None:
            messagebox.showinfo(_("Updates"), _("You are using the latest version ({version}).")
                                .format(version=__version__), parent=parent)
        else:
            UpdateDialog(root, update, app, restart)

    _in_background(root, updater.check, done)


def show_notes(txt, notes):
    """Release-Notizen (Markdown) lesbar ins Textfeld: Überschriften fett, Aufzählungspunkte, ohne ** und `."""
    txt.tag_configure("h", font=font(txt, 11, "bold"), spacing1=2, spacing3=6)
    txt.tag_configure("b", font=font(txt, 10, "bold"))
    txt.tag_configure("li", lmargin1=4, lmargin2=18)
    txt.tag_configure("li2", lmargin1=20, lmargin2=34)
    after_heading = False
    for line in notes.splitlines():
        stripped = line.lstrip()
        if after_heading and not stripped:
            continue
        after_heading = stripped.startswith("#")
        indent = len(line) - len(stripped)
        if stripped.startswith("#"):
            txt.insert("end", stripped.lstrip("#").strip().replace("**", "") + "\n", "h")
            continue
        tags = ()
        if stripped.startswith(("- ", "* ")):
            tags = ("li2",) if indent >= 2 else ("li",)
            stripped = ("◦ " if indent >= 2 else "• ") + stripped[2:]
        for i, part in enumerate(stripped.replace("`", "").split("**")):
            txt.insert("end", part, tags + (("b",) if i % 2 else ()))
        txt.insert("end", "\n", tags)


def choose_earlier(root, app, restart):
    """Einstellungen → „Install an earlier version…“: Liste der älteren Releases zeigen."""
    parent = root.winfo_toplevel()
    if _no_packages(parent, app):
        return None
    return RollbackDialog(root, app, restart)


class RollbackDialog(Dialog):
    """Ältere Versionen (neueste zuerst) mit Datum; die gewählte geht an den UpdateDialog."""

    def __init__(self, master, app, restart, load=None):
        super().__init__(master, _("Install an earlier version"), width=500, height=540)
        self.master_widget, self.app, self.restart = master, app, restart
        self.choices, self.cards, self.selected = [], [], None
        b = self.body
        header(b, _("Install an earlier version"),
               _("Go back to an older release if the current version causes problems."), icon="undo")
        warn = Card(b, pad=12, stretch=True)
        warn.pack(fill="x", pady=(0, 12))
        label(warn.body, _("Before you go back"), 10, "bold", color=COL["warn"], bg=COL["panel"]).pack(anchor="w")
        label(warn.body, _("Older versions may not read settings or profiles that a newer version saved. "
                           "Automatic update checks will offer the newest version again – choose “Skip this "
                           "version” to stay on the older one."), 9, color=COL["fg"], wraplength=420,
              justify="left", bg=COL["panel"]).pack(anchor="w", pady=(4, 0))
        self.status = label(b, _("Loading the list of versions…"), 10, color=COL["muted"], wraplength=440,
                            justify="left")
        self.status.pack(anchor="w", pady=(4, 8))
        self.area = ScrollArea(b)
        self.area.pack(fill="both", expand=True)

        footer(self)
        self.next_btn = Button(self.buttons, _("Continue"), icon="arrow", kind="primary", command=self._continue)
        self.next_btn.pack(side="right")
        Button(self.buttons, _("Cancel"), command=self.destroy).pack(side="right", padx=8)
        self.update_idletasks()
        self.focus_set()
        _in_background(self, load or updater.rollback_choices, self._loaded)

    def _loaded(self, choices, error):
        if not self.winfo_exists():
            return
        if error is not None:
            log.info("Release list failed: %s", error)
            self.status.configure(text=_("Could not load the list of versions: {error}").format(error=error),
                                  fg=COL["danger"])
            return
        self.choices = list(choices or [])
        if not self.choices:
            self.status.configure(text=_("No earlier versions are available."))
            return
        self.status.configure(text=_("Choose the version to install:"))
        for up in self.choices:
            when = _("Published {date}").format(date=up.date) if up.date else ""
            if up.prerelease:
                when = (when + " · " if when else "") + _("Beta")
            card = InfoCard(self.area.inner, _("Version {version}").format(version=up.version), when, height=50,
                            command=lambda u=up: self.select(u))
            card.pack(fill="x", pady=(0, 6))
            self.cards.append(card)
        self.select(self.choices[0])

    def select(self, update):
        self.selected = update
        for up, card in zip(self.choices, self.cards):
            card.set(selected=up is update)

    def _continue(self):
        if self.selected is None:
            return None
        up, master = self.selected, self.master_widget
        self.destroy()
        return UpdateDialog(master, up, self.app, self.restart, rollback=True)


class UpdateDialog(Dialog):
    """Neue (mit rollback: ältere) Version: Notizen zeigen, nach Bestätigung herunterladen und installieren."""

    def __init__(self, master, update, app, restart, lock=None, automatic=False, rollback=False, choose=False):
        # choose: nur die Wahl Python/Rust (nach einem Update), nichts Neues zu installieren
        self.choose = choose
        title = _("Install an earlier version") if rollback else _("Program") if choose else _("Update available")
        super().__init__(master, title, width=540)
        self.update_info, self.app, self.restart = update, app, restart
        self.lock, self.automatic, self.rollback = lock, automatic, rollback
        self.proc = None
        self.busy = False
        if master.winfo_toplevel().state() == "withdrawn":
            self.wm_transient("")  # Programm läuft unsichtbar: Dialog als eigenes Fenster zeigen
        b = self.body
        if rollback:
            header(b, title, _("FreeStream {version} will replace version {current}. Your settings and profiles "
                               "stay, but an older version may not understand everything a newer one saved.")
                   .format(version=update.version, current=__version__), icon="undo", color=COL["warn"])
        elif choose:
            header(b, title, _("FreeStream {version} is installed. Choose which programs you want to use.")
                   .format(version=update.version), icon="spark")
        else:
            header(b, title, _("FreeStream {version} is ready to install. You are using version {current}.")
                   .format(version=update.version, current=__version__), icon="spark")

        if update.notes and not choose:
            card = Card(b, pad=14, stretch=True)
            card.pack(fill="both", expand=True)
            txt = tk.Text(card.body, height=10, width=62, wrap="word", bg=COL["panel"], fg=COL["fg"], relief="flat",
                          highlightthickness=0, bd=0, font=font(b, 10), padx=2, pady=2)
            show_notes(txt, update.notes)
            txt.configure(state="disabled")
            bar = ttk.Scrollbar(card.body, orient="vertical", command=txt.yview, style=slim_scrollbar(card.body))
            txt.configure(yscrollcommand=bar.set)
            bar.pack(side="right", fill="y", padx=(6, 0))
            txt.pack(side="left", fill="both", expand=True)
        if update.page:
            Button(b, _("Show release on GitHub"), icon="arrow", kind="ghost", height=28,
                   command=lambda: webbrowser.open(update.page)).pack(anchor="w", pady=(8, 0))

        row = tk.Frame(b, bg=COL["bg"])
        if not rollback and not choose:
            row.pack(fill="x", pady=(14, 0))
        self.auto = Switch(row, command=self._toggle_auto)
        self.auto.set(updater.configured())
        self.auto.pack(side="left")
        label(row, _("Check for updates every time {app} starts").format(app=app), 10).pack(side="left", padx=10)

        # Welche Programme: Python oder Rust (Beta) – gilt für FreeDeck und FreeWave
        self.edition = "python"
        if not rollback:
            erow = tk.Frame(b, bg=COL["bg"])
            erow.pack(fill="x", pady=(14, 0))
            label(erow, _("Program"), 10).pack(side="left", padx=(0, 12))
            self.edition_switch = Segmented(erow, [("python", "Python"), ("rust", _("Rust (Beta)"))],
                                            command=self._choose_edition, value=updater.edition())
            self.edition_switch.pack(side="left")
            label(b, _("Rust (Beta) runs without Python and starts faster. It uses the same profiles and settings – "
                       "you can switch back with every update."), 9, color=COL["muted"], wraplength=520,
                  justify="left").pack(anchor="w", pady=(6, 0))
            self.edition = updater.edition()

        self.status = label(b, "", 9, color=COL["muted"], wraplength=560, justify="left")
        st = ttk.Style(self)
        st.configure("Update.Horizontal.TProgressbar", background=COL["accent"], troughcolor=COL["panel2"],
                     bordercolor=COL["border"], lightcolor=COL["accent"], darkcolor=COL["accent"], thickness=6)
        self.bar = ttk.Progressbar(b, mode="determinate", maximum=1000, style="Update.Horizontal.TProgressbar")

        footer(self)
        install_text = _("Install version {version}").format(version=update.version) if rollback \
            else _("Continue") if choose else _("Install update")
        self.install_btn = Button(self.buttons, install_text, icon="check", kind="primary", command=self._install)
        self.install_btn.pack(side="right")
        self.later_btn = Button(self.buttons, _("Cancel") if rollback else _("Later"), command=self._later)
        self.later_btn.pack(side="right", padx=8)
        self.skip_btn = Button(self.buttons, _("Skip this version"), kind="ghost", command=self._skip)
        if not rollback and not choose:
            self.skip_btn.pack(side="left")
        self.protocol("WM_DELETE_WINDOW", self._later)
        self.bind("<Escape>", lambda e: self._later())
        self.bind("<Destroy>", self._on_destroy, add="+")
        self.update_idletasks()
        self.deiconify()
        self.lift()
        self.focus_force()

    # -------------------------------------------------------------- Knöpfe
    def _choose_edition(self, ed):
        if self.busy:
            return
        if ed == "rust" and updater.update_for_edition(self.update_info, "rust") is None:
            self.edition_switch.value = self.edition
            self.edition_switch._draw()
            self._show_status(_("This release has no Rust packages for your system."), COL["warn"], bar=False)
            return
        self.edition = ed
        self._show_status("", bar=False)

    def _toggle_auto(self):
        updater.set_enabled(self.auto.get())

    def _later(self):
        if self.busy:
            return
        if self.choose:
            updater.set_edition(updater.edition())  # nur einmal fragen
        if self.automatic:
            updater.remind_later(self.update_info.version)
        self.destroy()

    def _skip(self):
        if self.busy:
            return
        updater.skip_version(self.update_info.version)
        self.destroy()

    def _on_destroy(self, e):
        if e.widget is self and self.lock is not None:
            self.lock.release()
            self.lock = None

    def _set_busy(self, busy):
        self.busy = busy
        for w in (self.install_btn, self.later_btn) + (() if self.rollback or self.choose else (self.skip_btn,)):
            if busy:
                w.pack_forget()
            elif not w.winfo_manager():
                w.pack(side="left" if w is self.skip_btn else "right", padx=8 if w is self.later_btn else 0)

    def _show_status(self, text, color=None, bar=True):
        self.status.configure(text=text, fg=color or COL["muted"])
        if not self.status.winfo_manager():
            self.status.pack(anchor="w", pady=(12, 4))
        if bar and not self.bar.winfo_manager():
            self.bar.pack(fill="x", pady=(2, 0))
        elif not bar:
            self.bar.pack_forget()

    # -------------------------------------------------------------- Installation
    def _install(self):
        if self.choose and self.edition == "python":
            updater.set_edition("python")  # bleibt wie es ist, nicht wieder fragen
            self.destroy()
            return
        self._set_busy(True)
        self.protocol("WM_DELETE_WINDOW", lambda: None)
        self._show_status(_("Downloading FreeStream {version}…").format(version=self.update_info.version))
        progress = {"value": 0}

        # gewählte Ausgabe: dasselbe Release mit den Paketen von Python bzw. Rust
        up = updater.update_for_edition(self.update_info, self.edition) if not self.rollback else None
        self.install_info = up or self.update_info

        def work():
            return updater.download(self.install_info, progress=lambda d, t: progress.update(value=d * 1000 // t))

        def tick():
            if self.busy and self.proc is None and self.winfo_exists():
                self.bar.configure(value=progress["value"])
                self.after(150, tick)

        tick()
        _in_background(self, work, self._downloaded)

    def _downloaded(self, files, error):
        if error is not None:
            log.warning("Update download failed: %s", error)
            msg = (_("The download is damaged ({file}). Nothing was installed.").format(file=error)
                   if isinstance(error, updater.UpdateError)
                   else _("Download failed: {error}").format(error=error))
            return self._failed(msg)
        self.bar.configure(mode="indeterminate")
        self.bar.start(12)
        self._show_status(_("Installing… Enter your password when asked. {app} restarts by itself afterwards.")
                          .format(app=self.app))
        try:
            self.proc = updater.start_install(files, self.update_info.version)
        except OSError as e:
            return self._failed(_("Could not start the installation: {error}").format(error=e))
        if self.proc is None:
            # kein pkexec: Ordner öffnen, Pakete per Doppelklick installieren
            folder = str(files[0].parent)
            try:
                subprocess.Popen(["xdg-open", folder], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass
            return self._failed(_("Automatic installation is not possible here (pkexec is missing). "
                                  "The packages are in {folder} – install them with a double-click.")
                                .format(folder=folder))
        self._wait_install()

    def _wait_install(self):
        rc = self.proc.poll()
        if rc is None:
            self.after(300, self._wait_install)
            return
        output = (self.proc.stdout.read() if self.proc.stdout else "") or ""
        if rc == 0:
            self.bar.stop()
            if not self.rollback:
                # ab jetzt startet „FreeDeck“/„FreeWave“ die gewählte Ausgabe (auch beim Neustart gleich)
                updater.set_edition(self.edition)
            done = (_("Version {version} installed. {app} restarts now.")
                    .format(version=self.update_info.version, app=self.app) if self.rollback
                    else _("Update installed. {app} restarts now.").format(app=self.app))
            self._show_status(done, COL["ok"], bar=False)
            self.after(2500, self._restart)
            return
        updater.save_state(installing=None)
        if rc in (126, 127):  # pkexec: abgebrochen oder nicht berechtigt
            return self._failed(_("The installation was cancelled. Nothing was changed."))
        log.warning("Update installation failed (%s): %s", rc, output[-2000:])
        last = output.strip().splitlines()[-1:] or [str(rc)]
        self._failed(_("The installation failed: {error}").format(error=last[0]))

    def _restart(self):
        try:
            self.destroy()
        finally:
            self.restart()

    def _failed(self, message):
        try:
            self.bar.stop()
        except tk.TclError:
            pass
        self.proc = None
        self._show_status(message, COL["danger"], bar=False)
        self._set_busy(False)
        self.protocol("WM_DELETE_WINDOW", self._later)
