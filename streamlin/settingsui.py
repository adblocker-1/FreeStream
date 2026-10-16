"""Einstellungen von LinDeck und Linewave (Zahnrad in der Seitenleiste).

Design (Hell/Dunkel/Auto), Sprache, Updates und Hilfe (Support-Bericht) an einer
Stelle. Beide Programme teilen sich die Einstellungen (config.json).
"""
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__, i18n, support, system, updater, updateui
from .dialogkit import field, footer, header
from .i18n import _
from .theme import COL, Button, Card, Dialog, Switch, choose_language, label, mode_switch


def open_settings(master, app, restart):
    """Fenster öffnen; ist es schon offen, nach vorne holen."""
    top = master.winfo_toplevel()
    dlg = getattr(top, "_settings_dialog", None)
    if dlg is not None and dlg.winfo_exists():
        dlg.deiconify()
        dlg.lift()
        dlg.focus_force()
        return dlg
    top._settings_dialog = SettingsDialog(master, app, restart)
    return top._settings_dialog


class SettingsDialog(Dialog):
    def __init__(self, master, app, restart):
        super().__init__(master, _("Settings"), width=500)
        self.app, self.restart = app, restart
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

        # ---------------------------------------------------------- Updates
        card = self._card(_("Updates"))
        self.auto_update = self.beta = None
        if updater.in_flatpak():
            # Flatpak: nie selbst aktualisieren
            label(card, updateui.flatpak_text(), 10, wraplength=420, justify="left").pack(anchor="w")
            label(card, _("Installed version: {version}").format(version=__version__), 9, color=COL["muted"]).pack(
                anchor="w", pady=(12, 0))
        else:
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

        footer(self)
        Button(self.buttons, _("Close"), kind="primary", command=self.destroy).pack(side="right")
        self.update_idletasks()
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
