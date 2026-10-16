"""Einstellungen von LinDeck und Linewave (Zahnrad in der Seitenleiste).

Design (Hell/Dunkel/Auto), Sprache, Updates, Hilfe (Support-Bericht) und Sicherung an
einer Stelle. Beide Programme teilen sich die Einstellungen (config.json).
"""
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__, i18n, support, system, updater, updateui
from .dialogkit import ScrollArea, field, footer, header
from .i18n import _
from .theme import COL, Button, Card, Dialog, Switch, choose_language, label, mode_switch


def open_settings(master, app, restart, mixer=None):
    """Fenster öffnen; ist es schon offen, nach vorne holen."""
    top = master.winfo_toplevel()
    dlg = getattr(top, "_settings_dialog", None)
    if dlg is not None and dlg.winfo_exists():
        dlg.deiconify()
        dlg.lift()
        dlg.focus_force()
        return dlg
    top._settings_dialog = SettingsDialog(master, app, restart, mixer=mixer)
    return top._settings_dialog


class SettingsDialog(Dialog):
    def __init__(self, master, app, restart, mixer=None):
        """mixer: (eingerichtet, enable(bool)) – nur in Linewave: Mixer einrichten/entfernen."""
        super().__init__(master, _("Settings"), width=500)
        self.app, self.restart = app, restart
        self._outer = self.body
        self._scroll = ScrollArea(self._outer)
        self._scroll.pack(fill="both", expand=True)
        self.body = self._scroll.inner
        b = self.body
        header(b, _("Settings"), _("These settings apply to LinDeck and Linewave."), icon="gear")

        # ---------------------------------------------------------- Design
        card = self._card(_("Appearance"))
        self.appearance = mode_switch(card, app, restart)
        self.appearance.pack(fill="x")
        label(card, _("Auto follows the light or dark setting of your desktop."), 9, color=COL["muted"],
              wraplength=420, justify="left").pack(anchor="w", pady=(8, 0))

        # ---------------------------------------------------------- Sprache
        card = self._card(_("Language"))
        self.codes = ["auto"] + i18n.available()
        names = [_("System language")] + [i18n.name(c) for c in i18n.available()]
        cur = i18n.configured()
        self.language = tk.StringVar(self, value=names[self.codes.index(cur)] if cur in self.codes else names[0])
        self.language_box = ttk.Combobox(card, textvariable=self.language, values=names, state="readonly")
        self.language_box.pack(fill="x")
        self.language_box.bind("<<ComboboxSelected>>", self._language)

        # ---------------------------------------------------------- Mixer (Linewave)
        if mixer is not None:
            active, enable = mixer
            card = self._card(_("Mixer"))
            label(card, (_("Remove mixer: the Linewave channels and mixes disappear, apps play directly on your "
                           "device again. Your settings are kept.") if active else
                         _("The mixer is off. Set it up to give every app its own channel.")), 9,
                  color=COL["muted"], wraplength=420, justify="left").pack(anchor="w")
            Button(card, _("Remove mixer") if active else _("Set up mixer"), icon="close" if active else "wave",
                   kind="danger" if active else "primary", height=30,
                   command=lambda: (self.destroy(), enable(not active))).pack(anchor="w", pady=(10, 0))

        # ---------------------------------------------------------- Updates
        card = self._card(_("Updates"))
        self.auto_update = self._switch_row(card, self._toggle_updates, updater.configured(),
                                            _("Check for updates every time {app} starts").format(app=app))
        self.beta = self._switch_row(card, self._toggle_beta, updater.channel() == "beta",
                                     _("Include beta versions"), pady=(10, 0),
                                     hint=_("Beta versions bring new features earlier but may contain more bugs."))
        row = tk.Frame(card, bg=COL["panel"])
        row.pack(fill="x", pady=(12, 0))
        label(row, _("Installed version: {version}").format(version=__version__), 9, color=COL["muted"]).pack(
            side="left")
        Button(row, _("Check now"), icon="spark", command=self._check, height=30).pack(side="right")
        Button(card, _("Install an earlier version…"), icon="undo", kind="ghost", height=28,
               command=self._rollback).pack(anchor="w", pady=(8, 0))

        # ---------------------------------------------------------- Hilfe
        card = self._card(_("Help"))
        label(card, _("Something not working? Create a support report and attach it to a GitHub issue. "
                      "Passwords and tokens are removed from it."), 9, color=COL["muted"], wraplength=420,
              justify="left").pack(anchor="w")
        row = tk.Frame(card, bg=COL["panel"])
        row.pack(fill="x", pady=(10, 0))
        self.report_btn = Button(row, _("Create support report…"), icon="export", command=self._support_report,
                                 height=30)
        self.report_btn.pack(side="left")
        Button(row, _("Report a problem on GitHub"), icon="globe", kind="ghost", height=30,
               command=lambda: webbrowser.open(support.ISSUE_URL)).pack(side="left", padx=(8, 0))
        self.report_status = label(card, "", 9, color=COL["muted"], wraplength=420, justify="left")

        self._build_backup()

        self.body = self._outer
        footer(self)
        Button(self.buttons, _("Close"), kind="primary", command=self.destroy).pack(side="right")
        self._fit()
        self.focus_set()

    def _card(self, title):
        field(self.body, title)
        card = Card(self.body, pad=16, stretch=True)
        card.pack(fill="x", pady=(0, 16))
        return card.body

    def _language(self, _e=None):
        code = self.codes[self.language_box.current()]
        choose_language(code, self.app, self.restart, self)

    def _toggle_updates(self):
        updater.set_enabled(self.auto_update.get())

    def _switch_row(self, card, command, value, text, pady=(0, 0), hint=""):
        """Schalter mit Beschriftung (und grauem Hinweis darunter)."""
        row = tk.Frame(card, bg=COL["panel"])
        row.pack(fill="x", pady=pady)
        switch = Switch(row, command=command)
        switch.set(value)
        switch.pack(side="left", anchor="n")
        txt = tk.Frame(row, bg=COL["panel"])
        txt.pack(side="left", fill="x", expand=True, padx=10)
        label(txt, text, 10).pack(anchor="w", pady=(2, 0))
        if hint:
            label(txt, hint, 9, color=COL["muted"], wraplength=360, justify="left").pack(anchor="w", pady=(2, 0))
        return switch

    def _toggle_beta(self):
        updater.set_channel("beta" if self.beta.get() else "stable")

    def _check(self):
        updateui.check_now(self, self.app, self.restart)

    def _fit(self):
        """Höhe an den Inhalt anpassen, höchstens so hoch wie der Bildschirm (sonst Bildlauf)."""
        self.update_idletasks()
        inner = self._scroll.inner
        room = self.winfo_screenheight() - 90 - self.buttons.winfo_reqheight() - 2 * 18
        self._scroll.cv.configure(height=max(200, min(inner.winfo_reqheight(), room)),
                                  width=inner.winfo_reqwidth())
        self.update_idletasks()

    # ---------------------------------------------------------- Sicherung
    def _build_backup(self):
        card = self._card(_("Backup"))
        label(card, _("Save all profiles, images, plugins and settings of LinDeck and Linewave to one file, "
                      "or restore them from such a file."), 9, color=COL["muted"], wraplength=420,
              justify="left").pack(anchor="w")
        row = tk.Frame(card, bg=COL["panel"])
        row.pack(fill="x", pady=(12, 0))
        self.backup_plugins = Switch(row)
        self.backup_plugins.set(True)
        self.backup_plugins.pack(side="left")
        label(row, _("Include installed plugins"), 10).pack(side="left", padx=10)
        row = tk.Frame(card, bg=COL["panel"])
        row.pack(fill="x", pady=(12, 0))
        self.backup_btn = Button(row, _("Create backup…"), icon="export", command=self._backup, height=30)
        self.backup_btn.pack(side="left")
        self.restore_btn = Button(row, _("Restore…"), icon="import", command=self._restore, height=30)
        self.restore_btn.pack(side="left", padx=(8, 0))
        self.backup_status = label(card, "", 9, color=COL["muted"], wraplength=420, justify="left")
        self.backup_status.pack(anchor="w", pady=(10, 0))
        self._busy = False

    def _status(self, text, color=None):
        try:
            self.backup_status.configure(text=text, fg=color or COL["muted"])
            self._fit()
        except tk.TclError:
            pass

    def _run(self, work, done, text):
        """work(progress) im Hintergrund; done(ergebnis, fehler) danach im Tk-Thread."""
        from . import backup
        self._busy = True
        for b in (self.backup_btn, self.restore_btn):
            b.configure_text(enabled=False)
        state = {"p": None, "res": None, "err": None, "end": False}

        def progress(n, total):
            state["p"] = int(100 * n / total) if total else 100

        def worker():
            try:
                state["res"] = work(progress)
            except backup.BackupError as e:
                state["err"] = str(e)
            except Exception as e:  # noqa: BLE001
                state["err"] = _("Error: {error}").format(error=e)
            state["end"] = True

        def poll():
            if not self.winfo_exists():
                return
            if not state["end"]:
                pct = f" {state['p']} %" if state["p"] is not None else ""
                self._status(text + pct)
                self.after(150, poll)
                return
            self._busy = False
            for b in (self.backup_btn, self.restore_btn):
                b.configure_text(enabled=True)
            done(state["res"], state["err"])

        self._status(text)
        threading.Thread(target=worker, name="backup", daemon=True).start()
        self.after(150, poll)

    def _backup(self):
        from . import backup
        if self._busy:
            return
        path = filedialog.asksaveasfilename(
            parent=self, title=_("Create backup"), initialdir=str(Path.home()), initialfile=backup.default_name(),
            defaultextension=backup.EXT, filetypes=[(_("Streamlin backup"), "*" + backup.EXT)])
        if not path:
            return
        plugins = self.backup_plugins.get()

        def done(m, err):
            if err:
                self._status(err, COL["danger"])
            else:
                f = Path(m["path"])  # nur der Dateiname: lange Pfade lassen sich nicht umbrechen
                self._status(_("Backup saved: {path}").format(path=f.name) + "\n" + _("Size: {size}").format(
                    size=backup.human_size(f.stat().st_size)), COL["ok"])
        self._run(lambda progress: backup.create(path, plugins=plugins, progress=progress), done,
                  _("Creating backup…"))

    def _restore(self):
        from . import backup
        if self._busy:
            return
        path = filedialog.askopenfilename(parent=self, title=_("Restore backup"), initialdir=str(Path.home()),
                                          filetypes=[(_("Streamlin backup"), "*" + backup.EXT),
                                                     (_("All files"), "*")])
        if not path:
            return
        try:
            m = backup.inspect(path)
        except backup.BackupError as e:
            self._status(str(e), COL["danger"])
            messagebox.showerror(_("Restore backup"), str(e), parent=self)
            return
        msg = "\n".join(backup.describe(m)) + "\n\n" + _(
            "Your current profiles and settings will be replaced. A safety copy of them is saved first. "
            "LinDeck and Linewave restart afterwards. Continue?")
        if not messagebox.askyesno(_("Restore backup"), msg, parent=self):
            return
        # Offenes Profil im Editor jetzt speichern und schließen, damit es die Sicherung nicht überschreibt
        editor = getattr(self.restart, "__self__", None)
        doc = getattr(editor, "doc", None)
        reopen = getattr(doc, "id", None)
        if doc is not None and callable(getattr(editor, "close_doc", None)):
            editor.close_doc()

        def work(progress):
            progs = backup.stop_programs()
            try:
                return backup.restore(path, progress=progress), progs
            finally:
                backup.start_programs(progs)

        def done(res, err):
            if err:
                if reopen:
                    editor.open_profile(reopen)  # nichts ersetzt: Profil wieder öffnen
                self._status(err, COL["danger"])
                messagebox.showerror(_("Restore backup"), err, parent=self)
                return
            (__, safety), __p = res
            self._status(_("Backup restored."), COL["ok"])
            messagebox.showinfo(_("Restore backup"), _("Backup restored. {app} restarts now.").format(app=self.app) +
                                "\n\n" + _("Your previous data was saved to {path}").format(path=safety),
                                parent=self)
            self.destroy()
            self.restart()
        self._run(work, done, _("Restoring backup…"))

    def _rollback(self):
        return updateui.choose_earlier(self, self.app, self.restart)

    # -------------------------------------------------------------- Support-Bericht
    def _support_report(self, target=None):
        """Bericht speichern (ohne target: Speicherort erfragen, Vorschlag im Home-Ordner)."""
        if target is None:
            target = filedialog.asksaveasfilename(
                parent=self, title=_("Save support report"), initialdir=str(Path.home()),
                initialfile=support.default_name(), defaultextension=".txt",
                filetypes=[(_("Text file"), "*.txt"), (_("All files"), "*")])
            if not target:
                return
        self.report_btn.configure_text(enabled=False)
        self._report_status(_("Creating the support report…"))
        updateui._in_background(self, lambda: support.write_report(target, app=self.app), self._report_done)

    def _report_status(self, text, color=None):
        self.report_status.configure(text=text, fg=color or COL["muted"])
        if not self.report_status.winfo_manager():
            self.report_status.pack(anchor="w", pady=(10, 0))

    def _report_done(self, path, error, ask=None):
        if not self.winfo_exists():
            return
        self.report_btn.configure_text(enabled=True)
        if error is not None:
            self._report_status(_("Could not create the support report: {error}").format(error=error),
                                COL["danger"])
            return
        self._report_status(_("Support report saved: {path}").format(path=path), COL["ok"])
        ask = ask or messagebox.askyesno
        if ask(_("Support report"), _("The support report was saved to {path}.\n\nAttach this file to a GitHub "
                                      "issue. Open the folder now?").format(path=path), parent=self):
            system.run_bg(["xdg-open", str(Path(path).parent)])
