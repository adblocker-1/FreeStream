"""Globale Tastenkürzel über die Einstellungen des Desktops – funktioniert auch unter Wayland.

Programme dürfen unter Wayland keine Tasten global abfangen. Deshalb trägt
Streamlin eigene Tastenkürzel dort ein, wo sie der Nutzer auch selbst anlegen
würde („Eigene Tastenkürzel“); der Desktop führt dann einen Befehl aus.

  GNOME/Ubuntu/Pop/Budgie  gsettings org.gnome.settings-daemon.plugins.media-keys custom-keybindings
  Cinnamon                 gsettings org.cinnamon.desktop.keybindings custom-list
  MATE                     gsettings org.mate.control-center.keybinding (/org/mate/desktop/keybindings/…)
  XFCE                     xfconf-query -c xfce4-keyboard-shortcuts /commands/custom/<Taste>
  KDE Plasma 5/6           .desktop-Datei in ~/.local/share/applications + Eintrag in kglobalshortcutsrc
                           (wirkt nach dem nächsten Anmelden)

Eigene Einträge heißen „streamlin-<id>“; was Streamlin eingetragen hat, steht
zusätzlich in CONFIG_DIR/shortcuts.json. Anlegen und Entfernen sind
wiederholbar und fassen nur diese Einträge an. Auf unbekannten Desktops gibt
register() Unsupported mit dem Befehl zurück, den der Nutzer selbst eintragen kann.

Tastenkürzel werden einheitlich als Text gespeichert: "Ctrl+Alt+M",
"Super+F9" (Modifier Ctrl, Alt, Shift, Super; Taste wie unten in KEYS).
"""
import ast
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import paths
from .i18n import _

log = logging.getLogger("streamlin.shortcuts")

PREFIX = "streamlin-"
REGISTRY = "shortcuts.json"
MODS = ("Ctrl", "Alt", "Shift", "Super")
# Tasten: Name in Streamlin -> (GTK/XFCE, KDE)
KEYS = {"Space": ("space", "Space"), "Pause": ("Pause", "Pause"), "Print": ("Print", "Print"),
        "Home": ("Home", "Home"), "End": ("End", "End"), "Insert": ("Insert", "Ins"), "Delete": ("Delete", "Del"),
        "PageUp": ("Page_Up", "PgUp"), "PageDown": ("Page_Down", "PgDown"), "Up": ("Up", "Up"),
        "Down": ("Down", "Down"), "Left": ("Left", "Left"), "Right": ("Right", "Right"),
        "ScrollLock": ("Scroll_Lock", "ScrollLock")}
_TK_KEYS = {"space": "Space", "Pause": "Pause", "Print": "Print", "Home": "Home", "End": "End", "Insert": "Insert",
            "Delete": "Delete", "Prior": "PageUp", "Next": "PageDown", "Page_Up": "PageUp", "Page_Down": "PageDown",
            "Up": "Up", "Down": "Down", "Left": "Left", "Right": "Right", "Scroll_Lock": "ScrollLock"}
_ALIASES = {"control": "Ctrl", "ctrl": "Ctrl", "primary": "Ctrl", "strg": "Ctrl", "alt": "Alt", "mod1": "Alt",
            "shift": "Shift", "super": "Super", "meta": "Super", "win": "Super", "mod4": "Super"}

GNOME_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
GNOME_BASE = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
CINNAMON_SCHEMA = "org.cinnamon.desktop.keybindings"
CINNAMON_BASE = "/org/cinnamon/desktop/keybindings/custom-keybindings/"
MATE_SCHEMA = "org.mate.control-center.keybinding"
MATE_BASE = "/org/mate/desktop/keybindings/"
XFCE_CHANNEL = "xfce4-keyboard-shortcuts"

NAMES = {"gnome": "GNOME", "cinnamon": "Cinnamon", "mate": "MATE", "xfce": "XFCE", "kde": "KDE Plasma"}


class Unsupported(RuntimeError):
    """Desktop unbekannt oder Werkzeug fehlt – .command selbst im Desktop eintragen."""

    def __init__(self, message, command=""):
        super().__init__(message)
        self.command = command


# ================================================================ Tasten
def _key_name(key):
    key = str(key or "").strip()
    if not key:
        return ""
    if len(key) == 1 and key.isalnum():
        return key.upper()
    m = re.fullmatch(r"[fF](\d{1,2})", key)
    if m and 1 <= int(m.group(1)) <= 24:
        return f"F{int(m.group(1))}"
    for name, (gtk, kde) in KEYS.items():
        if key.lower() in (name.lower(), gtk.lower(), kde.lower()):
            return name
    return _TK_KEYS.get(key, "")


def normalize(text):
    """'ctrl+alt+m' / '<Control><Alt>m' -> 'Ctrl+Alt+M'; ungültig -> ''."""
    text = str(text or "").strip()
    if not text:
        return ""
    mods = set()
    if "<" in text:
        for m in re.findall(r"<([^>]+)>", text):
            mod = _ALIASES.get(m.lower())
            if not mod:
                return ""
            mods.add(mod)
        key = re.sub(r"<[^>]+>", "", text)
    else:
        bits = [b for b in re.split(r"\s*\+\s*", text) if b] if text != "+" else []
        if not bits:
            return ""
        *mod_bits, key = bits
        for m in mod_bits:
            mod = _ALIASES.get(m.lower())
            if not mod:
                return ""
            mods.add(mod)
    key = _key_name(key)
    if not key:
        return ""
    if not mods and not (key.startswith("F") and key[1:].isdigit()) and key not in ("Pause", "Print", "ScrollLock"):
        return ""  # einzelne Buchstaben würden das Tippen stören
    return "+".join([m for m in MODS if m in mods] + [key])


def from_tk(keysym, state):
    """Tastendruck aus Tk (event.keysym, event.state) -> 'Ctrl+Alt+M' oder '' (nur Modifier/ungültig)."""
    if keysym in ("Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Super_L", "Super_R",
                  "Meta_L", "Meta_R", "ISO_Level3_Shift", "Caps_Lock", "Num_Lock"):
        return ""
    mods = [m for m, bit in (("Ctrl", 0x4), ("Alt", 0x8), ("Shift", 0x1), ("Super", 0x40)) if state & bit]
    key = _key_name(keysym)
    return normalize("+".join(mods + [key])) if key else ""


def _split(binding):
    b = normalize(binding)
    if not b:
        raise RuntimeError(_("Invalid key combination: {keys}").format(keys=binding))
    *mods, key = b.split("+")
    return mods, key


def to_gtk(binding, primary=False):
    """'Ctrl+Alt+M' -> '<Control><Alt>m' (XFCE: primary=True -> '<Primary>')."""
    mods, key = _split(binding)
    names = {"Ctrl": "Primary" if primary else "Control", "Alt": "Alt", "Shift": "Shift", "Super": "Super"}
    k = KEYS[key][0] if key in KEYS else (key.lower() if len(key) == 1 else key)
    return "".join(f"<{names[m]}>" for m in mods) + k


def to_kde(binding):
    """'Super+Ctrl+M' -> 'Meta+Ctrl+M'."""
    mods, key = _split(binding)
    names = {"Ctrl": "Ctrl", "Alt": "Alt", "Shift": "Shift", "Super": "Meta"}
    order = ("Super", "Ctrl", "Alt", "Shift")
    return "+".join([names[m] for m in order if m in mods] + [KEYS[key][1] if key in KEYS else key])


def label(binding):
    """Anzeige für den Nutzer, z. B. 'Ctrl + Alt + M'."""
    b = normalize(binding)
    return " + ".join(b.split("+")) if b else ""


# ================================================================ Befehle
def linewave_command(*args):
    """Befehlszeile für Linewave (installiertes `linewave` oder python -m streamlin.linewave)."""
    exe = shutil.which("linewave")
    if exe:
        argv = [exe]
    else:  # aus dem Quellcode gestartet: Paketordner mitgeben, der Desktop startet im Home-Verzeichnis
        argv = ["env", f"PYTHONPATH={Path(__file__).resolve().parent.parent}", sys.executable, "-m",
                "streamlin.linewave"]
    argv += [str(a) for a in args]
    return " ".join(shlex.quote(a) for a in argv)


def _desktop_exec(command):
    """Shell-Befehlszeile -> Exec= einer .desktop-Datei (doppelte Anführungszeichen nach der Spezifikation)."""
    out = []
    for a in shlex.split(command):
        if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", a):
            out.append(a.replace("%", "%%"))
        else:
            out.append('"' + re.sub(r'([\\"`$])', r"\\\1", a).replace("%", "%%") + '"')
    return " ".join(out)


# ================================================================ Desktop
def desktop(env=None):
    """'gnome', 'cinnamon', 'mate', 'xfce', 'kde' oder '' (unbekannt)."""
    env = os.environ if env is None else env
    raw = ":".join(filter(None, (env.get("XDG_CURRENT_DESKTOP"), env.get("XDG_SESSION_DESKTOP"),
                                 env.get("DESKTOP_SESSION")))).lower()
    parts = set(re.split(r"[:;,\s]+", raw))
    if any("cinnamon" in p for p in parts):
        return "cinnamon"
    if "kde" in parts or any(p.startswith("plasma") for p in parts):
        return "kde"
    if any(p.startswith("xfce") for p in parts):
        return "xfce"
    if "mate" in parts:
        return "mate"
    if parts & {"gnome", "ubuntu", "pop", "unity", "budgie", "budgie-desktop", "pantheon", "zorin", "gnome-classic",
                "gnome-flashback"}:
        return "gnome"
    return ""


def desktop_name(env=None):
    return NAMES.get(desktop(env), "")


def _run(args, check=True):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(str(e)) from e
    if check and r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or f"{args[0]} failed").strip())
    return r


def _gv_str(s):
    """Text als GVariant-String."""
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _gv_list(items):
    return "[" + ", ".join(_gv_str(i) for i in items) + "]"


def _parse_strv(text):
    text = (text or "").strip()
    if text.startswith("@as"):
        text = text[3:].strip()
    try:
        v = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return []
    return [str(x) for x in v] if isinstance(v, (list, tuple)) else []


def _parse_str(text):
    text = (text or "").strip()
    try:
        v = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return text
    if isinstance(v, (list, tuple)):
        return str(v[0]) if v else ""
    return str(v)


def _gsettings(*args, check=True):
    return _run(["gsettings", *args], check=check)


# ------------------------------------------------------------ Registrierung
def _registry_file():
    return paths.CONFIG_DIR / REGISTRY


def registered():
    """{id: {"desktop", "binding", "command", "name"}} – was Streamlin eingetragen hat."""
    try:
        data = json.loads(_registry_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_registry(data):
    f = _registry_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def get(ident):
    """Eingetragenes Tastenkürzel ('Ctrl+Alt+M') oder ''."""
    return (registered().get(ident) or {}).get("binding", "")


def _entry(ident):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", ident or ""):
        raise ValueError(f"invalid shortcut id: {ident!r}")
    return PREFIX + ident


def register(ident, name, command, binding, env=None):
    """Tastenkürzel eintragen (oder ändern). Gibt {"desktop", "binding", "note"} zurück.

    Unsupported: Desktop unbekannt oder Werkzeug fehlt (Befehl steht in .command).
    """
    entry = _entry(ident)
    raw, binding = binding, normalize(binding)
    if not binding:
        raise RuntimeError(_("Invalid key combination: {keys}").format(keys=raw))
    de = desktop(env)
    tool = {"gnome": "gsettings", "cinnamon": "gsettings", "mate": "gsettings", "xfce": "xfconf-query"}.get(de)
    if not de or (tool and not shutil.which(tool)):
        raise Unsupported(_("Your desktop does not support setting shortcuts automatically. Add this command as a "
                            "custom shortcut in your desktop's keyboard settings:"), command)
    reg = registered()
    old = reg.get(ident) or {}
    if old.get("desktop") and old.get("desktop") != de:
        old = {}
    note = ""
    if de == "gnome":
        _gnome_set(entry, name, command, binding)
    elif de == "cinnamon":
        _cinnamon_set(entry, name, command, binding)
    elif de == "mate":
        _mate_set(entry, name, command, binding)
    elif de == "xfce":
        _xfce_set(old, command, binding)
    elif de == "kde":
        _kde_set(entry, name, command, binding, env)
        note = _("KDE Plasma applies the shortcut after you log in again.")
    reg[ident] = {"desktop": de, "binding": binding, "command": command, "name": name}
    _save_registry(reg)
    return {"desktop": de, "binding": binding, "note": note}


def unregister(ident, env=None):
    """Eigenes Tastenkürzel entfernen. True, wenn eines eingetragen war."""
    entry = _entry(ident)
    reg = registered()
    old = reg.pop(ident, None)
    de = (old or {}).get("desktop") or desktop(env)
    try:
        if de == "gnome" and shutil.which("gsettings"):
            _gnome_remove(entry)
        elif de == "cinnamon" and shutil.which("gsettings"):
            _cinnamon_remove(entry)
        elif de == "mate" and shutil.which("gsettings"):
            _mate_remove(entry)
        elif de == "xfce" and old and shutil.which("xfconf-query"):
            _xfce_remove(old)
        elif de == "kde":
            _kde_remove(entry, env)
    finally:
        if old is not None:
            _save_registry(reg)
    return old is not None


def update_command(ident, command, name=None, env=None):
    """Befehl eines eingetragenen Kürzels ändern (z. B. nach dem Umbenennen einer Szene)."""
    old = registered().get(ident)
    if old and (old.get("command") != command or (name and name != old.get("name"))):
        return register(ident, name or old.get("name") or ident, command, old["binding"], env)
    return None


# ----------------------------------------------------------------- GNOME
def _gnome_path(entry):
    return f"{GNOME_BASE}{entry}/"


def _gnome_set(entry, name, command, binding):
    path = _gnome_path(entry)
    rel = f"{GNOME_SCHEMA}.custom-keybinding:{path}"
    _gsettings("set", rel, "name", _gv_str(name))
    _gsettings("set", rel, "command", _gv_str(command))
    _gsettings("set", rel, "binding", _gv_str(to_gtk(binding)))
    cur = _parse_strv(_gsettings("get", GNOME_SCHEMA, "custom-keybindings").stdout)
    if path not in cur:
        _gsettings("set", GNOME_SCHEMA, "custom-keybindings", _gv_list(cur + [path]))


def _gnome_remove(entry):
    path = _gnome_path(entry)
    cur = _parse_strv(_gsettings("get", GNOME_SCHEMA, "custom-keybindings").stdout)
    if path in cur:
        _gsettings("set", GNOME_SCHEMA, "custom-keybindings", _gv_list([p for p in cur if p != path]))
    rel = f"{GNOME_SCHEMA}.custom-keybinding:{path}"
    for key in ("name", "command", "binding"):
        _gsettings("reset", rel, key, check=False)


# -------------------------------------------------------------- Cinnamon
def _cinnamon_set(entry, name, command, binding):
    rel = f"{CINNAMON_SCHEMA}.custom-keybinding:{CINNAMON_BASE}{entry}/"
    _gsettings("set", rel, "name", _gv_str(name))
    _gsettings("set", rel, "command", _gv_str(command))
    _gsettings("set", rel, "binding", _gv_list([to_gtk(binding)]))
    cur = _parse_strv(_gsettings("get", CINNAMON_SCHEMA, "custom-list").stdout)
    if entry not in cur:
        _gsettings("set", CINNAMON_SCHEMA, "custom-list", _gv_list(cur + [entry]))


def _cinnamon_remove(entry):
    cur = _parse_strv(_gsettings("get", CINNAMON_SCHEMA, "custom-list").stdout)
    if entry in cur:
        _gsettings("set", CINNAMON_SCHEMA, "custom-list", _gv_list([p for p in cur if p != entry]))
    rel = f"{CINNAMON_SCHEMA}.custom-keybinding:{CINNAMON_BASE}{entry}/"
    for key in ("name", "command", "binding"):
        _gsettings("reset", rel, key, check=False)


# ------------------------------------------------------------------ MATE
def _mate_set(entry, name, command, binding):
    rel = f"{MATE_SCHEMA}:{MATE_BASE}{entry}/"
    _gsettings("set", rel, "name", _gv_str(name))
    _gsettings("set", rel, "action", _gv_str(command))
    _gsettings("set", rel, "binding", _gv_str(to_gtk(binding)))


def _mate_remove(entry):
    rel = f"{MATE_SCHEMA}:{MATE_BASE}{entry}/"
    for key in ("name", "action", "binding"):
        _gsettings("reset", rel, key, check=False)


# ------------------------------------------------------------------ XFCE
def _xfce_prop(binding):
    return "/commands/custom/" + to_gtk(binding, primary=True)


def _xfce_get(prop):
    r = _run(["xfconf-query", "-c", XFCE_CHANNEL, "-p", prop], check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def _xfce_set(old, command, binding):
    prop = _xfce_prop(binding)
    cur = _xfce_get(prop)
    ours = {old.get("command")} | {command}
    if cur is not None and cur not in ours:
        raise RuntimeError(_("The key combination {keys} is already used by “{command}”.").format(
            keys=label(binding), command=cur))
    if old.get("binding") and normalize(old["binding"]) != binding:
        _xfce_remove(old)
    if cur is None:
        _run(["xfconf-query", "-c", XFCE_CHANNEL, "-p", prop, "-n", "-t", "string", "-s", command])
    else:
        _run(["xfconf-query", "-c", XFCE_CHANNEL, "-p", prop, "-s", command])


def _xfce_remove(old):
    """Nur entfernen, wenn dort noch unser Befehl steht."""
    try:
        prop = _xfce_prop(old.get("binding"))
    except RuntimeError:
        return
    if _xfce_get(prop) == old.get("command"):
        _run(["xfconf-query", "-c", XFCE_CHANNEL, "-p", prop, "-r"], check=False)


# ------------------------------------------------------------------- KDE
def _home(env):
    env = os.environ if env is None else env
    return Path(env.get("HOME") or Path.home())


def _kde_files(entry, env=None):
    env = os.environ if env is None else env
    home = _home(env)
    data = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share")
    conf = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    return data / "applications" / f"{entry}.desktop", conf / "kglobalshortcutsrc"


def _kde_version(env=None):
    env = os.environ if env is None else env
    try:
        return int(env.get("KDE_SESSION_VERSION") or 6)
    except ValueError:
        return 6


def _ini_set_group(text, group, values):
    """Gruppe [group] einer KDE-INI-Datei ersetzen (values None = entfernen); alles andere bleibt."""
    lines = text.splitlines()
    out, i, found = [], 0, False
    header = f"[{group}]"
    while i < len(lines):
        if lines[i].strip() == header:
            found = True
            i += 1
            while i < len(lines) and not lines[i].startswith("["):
                i += 1
            while out and not out[-1].strip():
                out.pop()
            if values is not None:
                if out:
                    out.append("")
                out += [header] + [f"{k}={v}" for k, v in values.items()]
            if i < len(lines):
                out.append("")
            continue
        out.append(lines[i])
        i += 1
    if not found and values is not None:
        while out and not out[-1].strip():
            out.pop()
        if out:
            out.append("")
        out += [header] + [f"{k}={v}" for k, v in values.items()]
    return "\n".join(out) + ("\n" if out else "")


def _kde_groups(entry, name, binding, version):
    """(Gruppe, Werte) für kglobalshortcutsrc: Plasma 6 unter [services], Plasma 5 eigene Gruppe."""
    desk = f"{entry}.desktop"
    if version >= 6:
        return [(f"services][{desk}", {"_launch": to_kde(binding)} if binding else None)]
    vals = {"_k_friendly_name": name, "_launch": f"{to_kde(binding)},none,{name}"} if binding else None
    return [(desk, vals)]


def _kde_set(entry, name, command, binding, env=None):
    desk, rc = _kde_files(entry, env)
    desk.parent.mkdir(parents=True, exist_ok=True)
    desk.write_text("[Desktop Entry]\nType=Application\n"
                    f"Name={name}\nExec={_desktop_exec(command)}\nIcon=audio-volume-high\n"
                    "NoDisplay=true\nStartupNotify=false\nX-KDE-GlobalAccel-CommandShortcut=true\n"
                    f"X-KDE-Shortcuts={to_kde(binding)}\n", encoding="utf-8")
    rc.parent.mkdir(parents=True, exist_ok=True)
    text = rc.read_text(encoding="utf-8") if rc.exists() else ""
    for group, vals in _kde_groups(entry, name, binding, _kde_version(env)):
        text = _ini_set_group(text, group, vals)
    rc.write_text(text, encoding="utf-8")


def _kde_remove(entry, env=None):
    desk, rc = _kde_files(entry, env)
    try:
        desk.unlink()
    except OSError:
        pass
    if rc.exists():
        text = rc.read_text(encoding="utf-8")
        for version in (5, 6):
            for group, _vals in _kde_groups(entry, "", "", version):
                text = _ini_set_group(text, group, None)
        rc.write_text(text, encoding="utf-8")
