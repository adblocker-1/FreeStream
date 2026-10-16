"""Einstellungen von LinDeck und Linewave (Zahnrad in der Seitenleiste).

Design (Hell/Dunkel/Auto), Sprache und Updates an einer Stelle. Beide Programme
teilen sich die Einstellungen (config.json).
"""
import tkinter as tk
from tkinter import ttk

from . import __version__, i18n, updater
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
        row = tk.Frame(card, bg=COL["panel"])
        row.pack(fill="x")
        self.auto_update = Switch(row, command=self._toggle_updates)
        self.auto_update.set(updater.configured())
        self.auto_update.pack(side="left")
        label(row, _("Check for updates every time {app} starts").format(app=app), 10).pack(side="left", padx=10)
        row = tk.Frame(card, bg=COL["panel"])
        row.pack(fill="x", pady=(12, 0))
        label(row, _("Installed version: {version}").format(version=__version__), 9, color=COL["muted"]).pack(
            side="left")
        Button(row, _("Check now"), icon="spark", command=self._check, height=30).pack(side="right")

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

    def _check(self):
        from . import updateui
        updateui.check_now(self, self.app, self.restart)
