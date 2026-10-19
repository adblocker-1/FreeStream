"""Home Assistant im FreeDeck-Fenster: Verbindungsdialog, Entitätsauswahl, Verbindungszeile.

Netzwerkzugriffe laufen in Threads; Ergebnisse gehen über eine Warteschlange
zurück in den Tk-Thread (after-Schleife), damit das Fenster nie hängt.
"""
import queue
import threading
import tkinter as tk
from tkinter import ttk

from . import hass
from .dialogkit import entry, field, footer, header, trace
from .i18n import _
from .theme import COL, Button, Dialog, Switch, font, label

HA_COLOR = "#0ea5e9"


def _cfg(app):
    """Konfiguration des laufenden Dienstes (gleicher Prozess) oder frisch von der Platte."""
    cfg = getattr(getattr(app, "daemon", None), "cfg", None)
    if cfg is None:
        from .config import Config
        cfg = Config()
    return cfg


def _root(app):
    return getattr(app, "root", app)


class _Async:
    """Funktion im Hintergrund ausführen, Ergebnis im Tk-Thread an done(ok, wert) geben."""

    def __init__(self, widget, fn, done):
        self.widget, self.done, self.q = widget, done, queue.Queue()
        q = self.q  # der Thread hält nur die Warteschlange, nie das Fenster (Tk-Objekte nur im Tk-Thread freigeben)

        def run():
            try:
                q.put((True, fn()))
            except Exception as e:  # noqa: BLE001 - Fehler im Dialog anzeigen
                q.put((False, e))
        threading.Thread(target=run, daemon=True).start()
        self._poll()

    def _poll(self):
        try:
            ok, val = self.q.get_nowait()
        except queue.Empty:
            try:
                self.widget.after(60, self._poll)
            except tk.TclError:
                pass
            return
        try:
            if self.widget.winfo_exists():
                self.done(ok, val)
        except tk.TclError:
            pass


# ==========================================================================
# Verbindung
# ==========================================================================
class ConnectionDialog(Dialog):
    """Adresse und Zugangstoken eingeben, testen und speichern."""

    def __init__(self, master, app=None, on_saved=None):
        super().__init__(master, "Home Assistant", width=520)
        self.app, self.on_saved = app, on_saved
        self.cfg = _cfg(app)
        cur = hass.settings(self.cfg)
        b = self.body
        header(b, _("Connect Home Assistant"),
               _("Enter the address of your Home Assistant and a long-lived access token. Create the token in "
                 "Home Assistant under your profile → Security → Long-lived access tokens."),
               icon="home", color=HA_COLOR, wrap=420)
        field(b, _("Address"))
        self.url = tk.StringVar(self, value=cur.get("url") or "http://homeassistant.local:8123")
        e = entry(b, self.url)
        e.box.pack(fill="x", pady=(0, 12))
        self.url_entry = e
        field(b, _("Access token"))
        self.token = tk.StringVar(self, value=cur.get("token") or "")
        t = entry(b, self.token)
        t.configure(show="•")
        t.box.pack(fill="x", pady=(0, 12))
        self.token_entry = t
        row = tk.Frame(b, bg=COL["bg"])
        row.pack(fill="x", pady=(0, 12))
        self.insecure = Switch(row)
        self.insecure.set(cur.get("verify_ssl", True) is False)
        self.insecure.pack(side="left")
        label(row, _("Accept self-signed certificates (HTTPS)"), 10, bg=COL["bg"]).pack(side="left", padx=(10, 0))
        self.status = label(b, "", 10, bg=COL["bg"], wraplength=460, justify="left")
        self.status.pack(anchor="w", fill="x", pady=(4, 0))
        footer(self)
        Button(self.buttons, _("Save"), kind="primary", command=self.save).pack(side="right")
        Button(self.buttons, _("Cancel"), kind="ghost", command=self.destroy).pack(side="right", padx=(0, 8))
        self.test_btn = Button(self.buttons, _("Test connection"), icon="check", command=self.test)
        self.test_btn.pack(side="left")
        self.bind("<Return>", lambda e: self.save())
        self.result = None
        self.show()

    def _values(self):
        return self.url.get().strip(), self.token.get().strip(), not self.insecure.get()

    def _set_status(self, text, kind="muted"):
        self.status.configure(text=text, fg={"ok": COL["ok"], "error": COL["danger"]}.get(kind, COL["muted"]))

    def test(self):
        url, token, verify = self._values()
        if not url or not token:
            self._set_status(_("Please enter the address and the access token."), "error")
            return
        self._set_status(_("Connecting …"))
        self.test_btn.configure_text(enabled=False)
        _Async(self, lambda: hass.check_connection(url, token, verify), self._tested)

    def _tested(self, ok, val):
        self.test_btn.configure_text(enabled=True)
        self.result = (ok, val)
        if ok:
            self._set_status(_("Connected to “{name}” (Home Assistant {version}).").format(
                name=val.get("location_name") or "Home", version=val.get("version") or "?"), "ok")
        else:
            self._set_status(str(val), "error")

    def save(self):
        url, token, verify = self._values()
        if not url or not token:
            self._set_status(_("Please enter the address and the access token."), "error")
            return
        hass.save_settings(self.cfg, url, token, verify)
        self.destroy()
        if self.on_saved:
            self.on_saved()


# ==========================================================================
# Entitätsauswahl
# ==========================================================================
class EntityPicker(Dialog):
    """Durchsuchbare Liste der Entitäten aus /api/states."""

    def __init__(self, master, app=None, domains=None, on_pick=None, current=""):
        super().__init__(master, _("Choose entity"), width=560, height=520)
        self.app, self.domains, self.on_pick = app, list(domains or []), on_pick
        self.cfg = _cfg(app)
        self.items, self.shown = [], []
        b = self.body
        header(b, _("Choose entity"), _("All entities of your Home Assistant. Type to search by name or ID."),
               icon="home", color=HA_COLOR, wrap=440)
        self.query = tk.StringVar(self, value="")
        e = entry(b, self.query)
        e.box.pack(fill="x")
        e.focus_set()
        self.search_entry = e
        trace(self, self.query, self._fill)
        self.only = tk.BooleanVar(self, value=bool(self.domains))
        if self.domains:
            row = tk.Frame(b, bg=COL["bg"])
            row.pack(fill="x", pady=(8, 0))
            sw = Switch(row, command=lambda: (self.only.set(sw.get()), self._fill()))
            sw.set(True)
            sw.pack(side="left")
            label(row, _("Only suitable entities"), 10, bg=COL["bg"]).pack(side="left", padx=(10, 0))
        box = tk.Frame(b, bg=COL["panel2"], highlightthickness=1, highlightbackground=COL["border"])
        box.pack(fill="both", expand=True, pady=(10, 0))
        self.lb = tk.Listbox(box, bg=COL["panel2"], fg=COL["fg"], selectbackground=COL["accent_soft"],
                             selectforeground=COL["fg"], highlightthickness=0, relief="flat", activestyle="none",
                             font=font(self, 10), exportselection=False)
        bar = ttk.Scrollbar(box, orient="vertical", command=self.lb.yview)
        self.lb.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.lb.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        self.lb.bind("<Double-Button-1>", lambda e: self.choose())
        self.bind("<Return>", lambda e: self.choose())
        self.status = label(b, _("Loading entities …"), 9, color=COL["muted"], bg=COL["bg"], wraplength=500,
                            justify="left")
        self.status.pack(anchor="w", pady=(8, 0))
        footer(self)
        Button(self.buttons, _("Choose"), kind="primary", command=self.choose).pack(side="right")
        Button(self.buttons, _("Cancel"), kind="ghost", command=self.destroy).pack(side="right", padx=(0, 8))
        Button(self.buttons, _("Connect …"), icon="gear", command=self.connect).pack(side="left")
        self.current = current
        self.load()
        self.show()

    def connect(self):
        ConnectionDialog(self, self.app, on_saved=self.load)

    def load(self):
        self.cfg = _cfg(self.app)
        cl = hass.client(self.cfg)
        if not cl.configured:
            self.status.configure(text=_("Home Assistant is not set up yet – click “Connect …”."), fg=COL["danger"])
            return
        self.status.configure(text=_("Loading entities …"), fg=COL["muted"])
        _Async(self, lambda: cl.states(max_age=20), self._loaded)

    def _loaded(self, ok, val):
        if not ok:
            self.status.configure(text=str(val), fg=COL["danger"])
            return
        self.items = sorted(((s.get("entity_id", ""), hass.friendly_name(s, s.get("entity_id", "")),
                              hass.format_state(s)) for s in val if isinstance(s, dict) and s.get("entity_id")),
                            key=lambda x: (x[1].lower(), x[0]))
        self._fill()

    def _fill(self):
        q = self.query.get().strip().lower()
        only = self.only.get() and self.domains
        self.shown = [it for it in self.items
                      if (not only or hass.domain_of(it[0]) in self.domains)
                      and (not q or q in it[0].lower() or q in it[1].lower())]
        self.lb.delete(0, "end")
        for eid, name, state in self.shown:
            self.lb.insert("end", f"{name}  ·  {eid}" + (f"  ({state})" if state else ""))
        for i, it in enumerate(self.shown):
            if it[0] == self.current:
                self.lb.selection_set(i)
                self.lb.see(i)
                break
        if self.items:
            self.status.configure(text=_("{shown} of {total} entities").format(
                shown=len(self.shown), total=len(self.items)), fg=COL["muted"])

    def choose(self):
        sel = self.lb.curselection()
        if not sel and len(self.shown) == 1:
            sel = (0,)
        if not sel:
            return
        eid = self.shown[sel[0]][0]
        self.destroy()
        if self.on_pick:
            self.on_pick(eid)


# ==========================================================================
# Bausteine für die Eigenschaften im Editor
# ==========================================================================
def entity_field(cell, form, key, fld):
    """Textfeld für die Entitäts-ID mit Knopf zur Auswahl aus Home Assistant."""
    s = form.data.setdefault("Settings", {})
    var = tk.StringVar(cell, value=str(s.get(key) or ""))
    ent = ttk.Entry(cell, textvariable=var)
    ent.pack(side="left", fill="x", expand=True)
    trace(cell, var, lambda: form._set(key, var.get().strip()))

    def pick():
        EntityPicker(_root(form.app), form.app, fld.options, on_pick=var.set, current=var.get().strip())
    btn = ttk.Button(cell, text=_("Choose …"), command=pick)
    btn.pack(side="left", padx=(4, 0))
    cell.entity_var, cell.pick_button = var, btn
    return var


def connection_row(parent, app):
    """Zeile „Home Assistant: Adresse“ mit Knopf „Verbinden …“ (ohne Netzwerkzugriff)."""
    bg = COL["panel"]
    row = tk.Frame(parent, bg=bg)
    row.pack(fill="x", pady=(0, 10))
    txt = label(row, "", 9, color=COL["muted"], bg=bg)
    txt.pack(side="left")

    def refresh():
        c = hass.settings(_cfg(app))
        if c.get("url") and c.get("token"):
            txt.configure(text=_("Home Assistant: {url}").format(url=c["url"]), fg=COL["muted"])
        else:
            txt.configure(text=_("Home Assistant is not set up yet."), fg=COL["warn"])
    refresh()
    Button(row, _("Connect …"), icon="gear", kind="ghost",
           command=lambda: ConnectionDialog(_root(app), app, on_saved=refresh)).pack(side="right")
    return row
