"""Einstellungen › Verbindungen: OBS, Discord, Home Assistant und Windows-Profile (Programme, Pfade, Layout).

Links die Liste der Dienste mit Status, rechts die Felder. Änderungen werden gesammelt
gespeichert; Passwörter und Tokens landen dabei im Schlüsselbund (config.Config.save).
Verbindungstests laufen im Hintergrund (hassui._Async), das Fenster bleibt bedienbar.
"""
import tkinter as tk
from tkinter import ttk

from . import hass
from .dialogkit import ScrollArea, entry
from .hassui import _Async
from .i18n import _, ngettext
from .freedecksettings import _fill, _Page
from .theme import COL, Button, Card, InfoCard, Switch, icon_tile, label, section

OBS_COLOR, DISCORD_COLOR, HA_COLOR, WIN_COLOR = "#7c3aed", "#5865f2", "#0ea5e9", "#64748b"
DISCORD_PORTAL = "https://discord.com/developers/applications"
LAYOUTS = (("auto", None), ("de", "German"), ("us", "English (US)"))


def _section(cfg, key):
    c = cfg.data.get(key)
    if not isinstance(c, dict):
        c = cfg.data[key] = {}
    return c


def obs_test(host, port, password, timeout=4.0):
    """Mit OBS verbinden und gleich wieder trennen. Ergebnis: None oder Fehlertext."""
    from . import obs
    cl = obs.OBSClient(host, port, password)
    try:
        if obs.wait_connected(cl, timeout):
            return None
        return cl.error or _("OBS is not answering")
    finally:
        cl.close()


class ConnectionsPage(_Page):
    def __init__(self, master, dialog, freedeck):
        super().__init__(master, dialog, freedeck)
        self.selected = "obs"
        left = Card(self, pad=12, width=250)
        left.pack(side="left", fill="y", padx=(0, 12))
        _fill(left)
        section(left.body, _("Services")).pack(anchor="w", pady=(0, 8))
        self.listbox = tk.Frame(left.body, bg=COL["panel"])
        self.listbox.pack(fill="both", expand=True)
        self.right = tk.Frame(self, bg=COL["bg"])
        self.right.pack(side="left", fill="both", expand=True)
        self.render()

    # ------------------------------------------------------------ Liste
    def services(self):
        return [("obs", "OBS Studio", "record", OBS_COLOR), ("discord", "Discord", "chat", DISCORD_COLOR),
                ("homeassistant", "Home Assistant", "home", HA_COLOR),
                ("windows", _("Windows profiles"), "keyboard", WIN_COLOR)]

    def summary(self, key):
        c = self.cfg.data.get(key) if key != "windows" else None
        if key == "obs":
            ok = bool(c)
        elif key == "discord":
            ok = bool(c and c.get("client_id") and c.get("client_secret"))
        elif key == "homeassistant":
            ok = bool(c and c.get("url") and c.get("token"))
        else:
            n = len(self.cfg.get("app_map") or {}) + len(self.cfg.get("path_map") or {})
            return (ngettext("{n} mapping", "{n} mappings", n).format(n=n) if n else _("Automatic")), COL["muted"]
        return (_("Set up"), COL["ok"]) if ok else (_("Not set up"), COL["faint"])

    def render(self):
        for w in self.listbox.winfo_children():
            w.destroy()
        for w in self.right.winfo_children():
            w.destroy()
        self.cards = {}
        for key, title, glyph, color in self.services():
            text, dot = self.summary(key)
            card = InfoCard(self.listbox, title, text, image=icon_tile(self, glyph, 32, color), dot=dot,
                            command=lambda k=key: self.select(k))
            card.selected = key == self.selected
            card.pack(fill="x", pady=(0, 6))
            self.cards[key] = card
        body = self.panel(self.right, fill="both", expand=True)
        getattr(self, "build_" + self.selected)(body)

    def select(self, key):
        self.save_now()
        self.selected = key
        self.render()

    def refresh_card(self):
        card = self.cards.get(self.selected)
        if card is not None:
            text, dot = self.summary(self.selected)
            card.set(subtitle=text, dot=dot)

    # ------------------------------------------------------------ Bausteine
    def head(self, body, title, text, glyph, color):
        h = tk.Frame(body, bg=COL["panel"])
        h.pack(fill="x", pady=(0, 14))
        tk.Label(h, image=icon_tile(h, glyph, 40, color), bg=COL["panel"]).pack(side="left", anchor="n")
        t = tk.Frame(h, bg=COL["panel"])
        t.pack(side="left", fill="x", expand=True, padx=(12, 0))
        label(t, title, 13, "bold").pack(anchor="w")
        label(t, text, 9, color=COL["muted"], wraplength=420, justify="left").pack(anchor="w", pady=(2, 0))

    def field(self, body, text, value, on_change, secret=False, width=28):
        r = self.row(body, text)
        var = tk.StringVar(self, value=str(value or ""))
        ent = entry(r, var, size=10)
        ent.configure(width=width)
        if secret:
            ent.configure(show="•")
        ent.box.pack(side="left")
        var.trace_add("write", lambda *a: on_change(var.get()))
        if secret:
            eye = Button(r, "", "search", kind="ghost",
                         command=lambda: ent.configure(show="" if ent.cget("show") else "•"))
            eye.pack(side="left", padx=(6, 0))
        return var

    def status_row(self, body):
        r = tk.Frame(body, bg=COL["panel"])
        r.pack(fill="x", pady=(6, 0))
        lab = label(r, "", 10, wraplength=440, justify="left")
        lab.pack(side="left", anchor="w")
        return lab

    def set_status(self, lab, text, kind="muted"):
        try:
            lab.configure(text=text, fg={"ok": COL["ok"], "error": COL["danger"]}.get(kind, COL["muted"]))
        except tk.TclError:
            pass

    def changed(self):
        self.refresh_card()
        self.save_later(800)

    # ------------------------------------------------------------ OBS
    def build_obs(self, body):
        self.head(body, "OBS Studio", _("Turn on the WebSocket server in OBS: Tools → WebSocket Server Settings → "
                                        "Enable WebSocket server. Copy the password from “Show Connect Info”."),
                  "record", OBS_COLOR)
        c = self.cfg.data.get("obs") if isinstance(self.cfg.data.get("obs"), dict) else {}

        def setter(field, conv=str):
            def set_(v):
                v = v.strip()
                sec = _section(self.cfg, "obs")
                try:
                    v = conv(v) if v else ""
                except ValueError:
                    return
                if v in ("", None):
                    sec.pop(field, None)
                else:
                    sec[field] = v
                if not sec:
                    self.cfg.data.pop("obs", None)
                self.changed()
            return set_
        self.obs_host = self.field(body, _("Address"), c.get("host") or "127.0.0.1", setter("host"))
        self.obs_port = self.field(body, _("Port"), c.get("port") or 4455, setter("port", int), width=8)
        self.obs_pw = self.field(body, _("Password"), c.get("password"), setter("password"), secret=True)
        btns = tk.Frame(body, bg=COL["panel"])
        btns.pack(fill="x", pady=(6, 0))
        self.obs_btn = Button(btns, _("Test connection"), "check", command=self.test_obs)
        self.obs_btn.pack(side="left")
        self.obs_status = self.status_row(body)

    def test_obs(self):
        host = self.obs_host.get().strip() or "127.0.0.1"
        try:
            port = int(self.obs_port.get().strip() or 4455)
        except ValueError:
            self.set_status(self.obs_status, _("The port must be a number."), "error")
            return
        self.set_status(self.obs_status, _("Connecting …"))
        self.obs_btn.configure_text(enabled=False)

        def done(ok, err):
            self.obs_btn.configure_text(enabled=True)
            if ok and err is None:
                self.set_status(self.obs_status, _("Connected to OBS."), "ok")
            else:
                self.set_status(self.obs_status, str(err), "error")
        password = self.obs_pw.get()  # Tk-Variablen nur im Tk-Thread lesen
        _Async(self, lambda: obs_test(host, port, password), done)

    # ------------------------------------------------------------ Discord
    def build_discord(self, body):
        self.head(body, "Discord", _("Discord only lets apps control the microphone and sound with your own "
                                     "Discord app. Create one in the Discord Developer Portal, add the redirect "
                                     "“http://localhost” under OAuth2 and copy its Client ID and Client Secret."),
                  "chat", DISCORD_COLOR)
        c = self.cfg.data.get("discord") if isinstance(self.cfg.data.get("discord"), dict) else {}

        def setter(field):
            def set_(v):
                sec = _section(self.cfg, "discord")
                v = v.strip()
                if v:
                    sec[field] = v
                else:
                    sec.pop(field, None)
                if not sec:
                    self.cfg.data.pop("discord", None)
                self.changed()
            return set_
        self.dc_id = self.field(body, _("Client ID"), c.get("client_id"), setter("client_id"))
        self.dc_secret = self.field(body, _("Client Secret"), c.get("client_secret"), setter("client_secret"),
                                    secret=True)
        btns = tk.Frame(body, bg=COL["panel"])
        btns.pack(fill="x", pady=(6, 0))
        self.dc_btn = Button(btns, _("Connect …"), "check", command=self.connect_discord)
        self.dc_btn.pack(side="left")
        Button(btns, _("Developer Portal"), "globe", kind="ghost",
               command=lambda: _open_url(DISCORD_PORTAL)).pack(side="left", padx=(8, 0))
        self.dc_status = self.status_row(body)

    def connect_discord(self):
        from . import discord
        if not self.dc_id.get().strip() or not self.dc_secret.get().strip():
            self.set_status(self.dc_status, _("Please enter the Client ID and the Client Secret."), "error")
            return
        self.save_now()
        self.set_status(self.dc_status, _("Connecting – please confirm the request in Discord …"))
        self.dc_btn.configure_text(enabled=False)
        cfg = self.cfg

        def work():
            cl = discord.client(cfg)
            cl.connect()
            return cl

        def done(ok, val):
            self.dc_btn.configure_text(enabled=True)
            if ok:
                self.set_status(self.dc_status, _("Connected to Discord."), "ok")
            else:
                self.set_status(self.dc_status, str(val), "error")
        _Async(self, work, done)

    # ------------------------------------------------------------ Home Assistant
    def build_homeassistant(self, body):
        self.head(body, "Home Assistant", _("Enter the address of your Home Assistant and a long-lived access "
                                            "token. Create the token in Home Assistant under your profile → "
                                            "Security → Long-lived access tokens."), "home", HA_COLOR)
        c = hass.settings(self.cfg)

        def setter(field, conv=lambda v: v.strip()):
            def set_(v):
                sec = _section(self.cfg, hass.CONFIG_KEY)
                v = conv(v)
                if v:
                    sec[field] = v
                else:
                    sec.pop(field, None)
                if not sec:
                    self.cfg.data.pop(hass.CONFIG_KEY, None)
                self.changed()
            return set_
        self.ha_url = self.field(body, _("Address"), c.get("url") or "", setter("url", hass.normalize_url))
        self.ha_token = self.field(body, _("Access token"), c.get("token"), setter("token"), secret=True)
        r = self.row(body, "")
        self.ha_insecure = Switch(r, command=self._ha_ssl)
        self.ha_insecure.set(c.get("verify_ssl", True) is False)
        self.ha_insecure.pack(side="left")
        label(r, _("Accept self-signed certificates (HTTPS)"), 10).pack(side="left", padx=(10, 0))
        btns = tk.Frame(body, bg=COL["panel"])
        btns.pack(fill="x", pady=(6, 0))
        self.ha_btn = Button(btns, _("Test connection"), "check", command=self.test_ha)
        self.ha_btn.pack(side="left")
        self.ha_status = self.status_row(body)

    def _ha_ssl(self):
        sec = _section(self.cfg, hass.CONFIG_KEY)
        if self.ha_insecure.get():
            sec["verify_ssl"] = False
        else:
            sec.pop("verify_ssl", None)
        self.changed()

    def test_ha(self):
        url, token = hass.normalize_url(self.ha_url.get()), self.ha_token.get().strip()
        if not url or not token:
            self.set_status(self.ha_status, _("Please enter the address and the access token."), "error")
            return
        verify = not self.ha_insecure.get()
        self.set_status(self.ha_status, _("Connecting …"))
        self.ha_btn.configure_text(enabled=False)

        def done(ok, val):
            self.ha_btn.configure_text(enabled=True)
            if ok:
                self.set_status(self.ha_status, _("Connected to “{name}” (Home Assistant {version}).").format(
                    name=val.get("location_name") or "Home", version=val.get("version") or "?"), "ok")
            else:
                self.set_status(self.ha_status, str(val), "error")
        _Async(self, lambda: hass.check_connection(url, token, verify), done)

    # ------------------------------------------------------------ Windows-Profile
    def build_windows(self, body):
        self.head(body, _("Windows profiles"), _("Profiles from Windows start Windows programs and use Windows "
                                                 "paths. Tell FreeDeck what to use on Linux instead."),
                  "keyboard", WIN_COLOR)
        r = self.row(body, _("Keyboard layout"))
        from .keyboard import detect_layout
        names = {"de": _("German"), "us": _("English (US)")}
        values = [_("Automatic ({layout})").format(layout=names.get(detect_layout(), detect_layout()))] + \
            [names[k] for k, _n in LAYOUTS[1:]]
        self.layout = ttk.Combobox(r, state="readonly", width=26, values=values)
        cur = self.cfg.get("keyboard_layout") or "auto"
        keys = [k for k, _n in LAYOUTS]
        self.layout.current(keys.index(cur) if cur in keys else 0)
        self.layout.bind("<<ComboboxSelected>>", lambda e: self._set_layout(keys[self.layout.current()]))
        self.layout.pack(side="left")
        area = ScrollArea(body, bg=COL["panel"])
        area.pack(fill="both", expand=True, pady=(8, 0))
        self.map_table(area.inner, "app_map", _("Programs"), _("Windows program (e.g. obs64.exe)"),
                       _("Linux command"))
        self.map_table(area.inner, "path_map", _("Folders"), _("Windows path (e.g. C:/Users/Alex/Music)"),
                       _("Linux path"))

    def _set_layout(self, key):
        self.cfg.data["keyboard_layout"] = key
        vin = getattr(getattr(self.freedeck, "daemon", None), "vin", None)
        if vin is not None:
            from .keyboard import detect_layout
            vin.layout = detect_layout() if key == "auto" else key
        self.changed()

    def map_table(self, master, key, title, left, right):
        box = tk.Frame(master, bg=COL["panel"])
        box.pack(fill="x", pady=(10, 0))
        section(box, title).pack(anchor="w", pady=(0, 6))
        rows = tk.Frame(box, bg=COL["panel"])
        rows.pack(fill="x")
        data = dict(self.cfg.get(key) or {})
        entries = []

        def store():
            out = {}
            for a, b in entries:
                k, v = a.get().strip(), b.get().strip()
                if k and v:
                    out[k] = v
            if out:
                self.cfg.data[key] = out
            else:
                self.cfg.data[key] = {}
            self.changed()

        def add(k="", v=""):
            r = tk.Frame(rows, bg=COL["panel"])
            r.pack(fill="x", pady=(0, 6))
            a, b = tk.StringVar(self, value=k), tk.StringVar(self, value=v)
            ea = entry(r, a, size=10)
            ea.configure(width=24)
            ea.box.pack(side="left")
            label(r, "→", 10, color=COL["muted"]).pack(side="left", padx=6)
            eb = entry(r, b, size=10)
            eb.configure(width=24)
            eb.box.pack(side="left")
            pair = (a, b)
            entries.append(pair)

            def remove():
                entries.remove(pair)
                r.destroy()
                store()
            Button(r, "", "trash", kind="ghost", command=remove).pack(side="left", padx=(6, 0))
            for var in pair:
                var.trace_add("write", lambda *x: store())
            if not k:
                ea.focus_set()
        for k, v in data.items():
            add(k, v)
        hint = label(box, f"{left}  →  {right}", 8, color=COL["faint"])
        hint.pack(anchor="w")
        Button(box, _("Add"), "plus", kind="ghost", command=add).pack(anchor="w", pady=(4, 0))


def _open_url(url):
    from . import system
    system.run_bg(["xdg-open", url])
