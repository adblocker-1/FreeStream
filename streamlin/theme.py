"""Gemeinsames Aussehen von LinDeck und Linewave (Streamlin-Design).

Seitenleiste links, Karten mit runden Ecken und feinem Rand, Schrift Inter,
blauer Akzent, Monitor-Mix in Cyan und Stream-Mix in Violett – als heller und
dunkler Modus. Der Modus („appearance“ in config.json: auto, dark, light)
wird beim Start einmal festgelegt; „auto“ folgt der Einstellung des Desktops.
Alle Symbole zeichnet dieses Modul selbst (keine Grafiken von Elgato).

Bausteine:
  apply(root)          ttk-Stile und Schriften setzen
  glyph(name, size)    Symbol als PIL-Bild (kantengeglättet)
  Card                 Fläche mit runden Ecken, Inhalt in .body
  Fader                Schieberegler (senkrecht oder waagerecht)
  RoundToggle          runder Knopf mit Symbol (z. B. stumm)
  Switch               Schalter an/aus
  Segmented            Umschalter für Ansichten (Reiter)
"""
import json
import math
import os
import subprocess
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

from PIL import Image, ImageDraw, ImageTk

from . import paths

# bg: Hauptfläche, side: Seitenleiste, panel: Karten, panel2: Eingabefelder/Chips,
# raised: Spuren/gedrückt, border: feine Ränder, accent_soft: gewählte Einträge
PALETTES = {
    "dark": {
        "bg": "#0e1015", "side": "#12151b", "panel": "#171a21", "panel2": "#1e222b", "raised": "#272c37",
        "border": "#262b35", "fg": "#e9ecf2", "muted": "#8b93a3", "faint": "#5b6372",
        "accent": "#5b7cff", "accent_hi": "#7590ff", "accent_soft": "#1f2745", "on_accent": "#ffffff",
        "monitor": "#22c3ee", "stream": "#c084fc", "danger": "#f43f5e", "danger_soft": "#3a1a24",
        "ok": "#22c55e", "warn": "#f5b13d",
        "device": "#07080b", "slot": "#1a1d24", "drop": "#22c55e", "shadow": "#05060a",
    },
    "light": {
        "bg": "#f3f5f9", "side": "#eceff5", "panel": "#ffffff", "panel2": "#f2f4f8", "raised": "#e6e9f0",
        "border": "#e0e4eb", "fg": "#151821", "muted": "#5d6676", "faint": "#9aa2b1",
        "accent": "#3a5ff0", "accent_hi": "#5175f3", "accent_soft": "#e4e9fd", "on_accent": "#ffffff",
        "monitor": "#0a9cc4", "stream": "#9341e6", "danger": "#e11d48", "danger_soft": "#fde4ea",
        "ok": "#16a34a", "warn": "#d48a06",
        "device": "#16181e", "slot": "#262a33", "drop": "#16a34a", "shadow": "#c9cfda",
    },
}
MODES = ("auto", "light", "dark")


def configured_appearance():
    """„appearance“ aus config.json (auto, light, dark)."""
    try:
        v = json.loads(paths.CONFIG_FILE.read_text(encoding="utf-8")).get("appearance")
    except (OSError, ValueError, AttributeError):
        v = None
    return v if v in MODES else "auto"


def save_appearance(mode):
    try:
        data = json.loads(paths.CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data["appearance"] = mode
    paths.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = paths.CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(paths.CONFIG_FILE)


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=1.5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def system_prefers_dark():
    """Hell oder dunkel laut Desktop (GNOME/GTK, KDE); None, wenn unbekannt."""
    gtk = os.environ.get("GTK_THEME", "")
    if gtk:
        return gtk.lower().endswith(":dark") or "dark" in gtk.lower()
    scheme = _run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"]).strip("'")
    if scheme in ("prefer-dark", "prefer-light"):
        return scheme == "prefer-dark"
    kde = os.path.expanduser("~/.config/kdeglobals")
    try:
        text = open(kde, encoding="utf-8").read()
        for line in text.splitlines():
            if line.startswith("ColorScheme="):
                return "dark" in line.lower()
    except OSError:
        pass
    theme = _run(["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"]).strip("'").lower()
    if theme:
        return "dark" in theme
    return None


def resolve_mode(mode=None):
    """Tatsächlicher Modus: STREAMLIN_APPEARANCE > config.json > Desktop > dunkel."""
    mode = os.environ.get("STREAMLIN_APPEARANCE") or mode or configured_appearance()
    if mode in ("light", "dark"):
        return mode
    dark = system_prefers_dark()
    return "light" if dark is False else "dark"


MODE = resolve_mode()
COL = dict(PALETTES[MODE])
FONT_FAMILIES = ("Inter", "Noto Sans", "Cantarell", "Ubuntu", "Segoe UI", "DejaVu Sans")


def rgb(hex_color):
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def mix(a, b, t):
    """Farbe zwischen a und b (0 … 1) als #rrggbb."""
    ca, cb = rgb(a), rgb(b)
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(ca, cb))


# ================================================================ Schriften/ttk
def _family(root):
    have = set(tkfont.families(root))
    return next((f for f in FONT_FAMILIES if f in have), "TkDefaultFont")


def apply(root):
    """Schriften und ttk-Stile für das ganze Fenster setzen."""
    fam = _family(root)
    for name, size, weight in (("TkDefaultFont", 10, "normal"), ("TkTextFont", 10, "normal"),
                               ("TkMenuFont", 10, "normal"), ("TkHeadingFont", 10, "bold")):
        try:
            tkfont.nametofont(name, root).configure(family=fam, size=size, weight=weight)
        except tk.TclError:
            pass
    root.configure(background=COL["bg"])
    s = ttk.Style(root)
    s.theme_use("clam")
    bg, panel, panel2, fg = COL["bg"], COL["panel"], COL["panel2"], COL["fg"]
    s.configure(".", background=bg, foreground=fg, fieldbackground=panel2, bordercolor=COL["border"],
                lightcolor=panel2, darkcolor=panel2, troughcolor=bg, insertcolor=fg, focuscolor=COL["accent"],
                font=(fam, 10))
    s.configure("TFrame", background=bg)
    s.configure("Panel.TFrame", background=panel)
    s.configure("Bar.TFrame", background=panel)
    s.configure("TLabel", background=bg, foreground=fg)
    s.configure("Panel.TLabel", background=panel)
    s.configure("Bar.TLabel", background=panel)
    s.configure("Muted.TLabel", background=panel, foreground=COL["muted"])
    s.configure("MutedBg.TLabel", background=bg, foreground=COL["muted"])
    s.configure("Head.TLabel", background=panel, font=(fam, 13, "bold"))
    s.configure("Title.TLabel", background=panel, font=(fam, 14, "bold"))
    s.configure("Section.TLabel", background=panel, foreground=COL["muted"], font=(fam, 9, "bold"))
    s.configure("Status.TLabel", background=panel, foreground=COL["muted"], padding=(12, 5))
    s.configure("Ok.TLabel", background=panel, foreground=COL["ok"])
    s.configure("TButton", background=panel2, foreground=fg, padding=(12, 6), borderwidth=1, relief="flat",
                width=-4, lightcolor=panel2, darkcolor=panel2, bordercolor=COL["border"], focusthickness=0)
    s.map("TButton", background=[("pressed", COL["raised"]), ("active", COL["raised"]), ("disabled", panel)],
          foreground=[("disabled", COL["faint"])],
          lightcolor=[("active", COL["raised"])], darkcolor=[("active", COL["raised"])],
          bordercolor=[("active", COL["raised"])])
    s.configure("Accent.TButton", background=COL["accent"], lightcolor=COL["accent"], darkcolor=COL["accent"],
                bordercolor=COL["accent"])
    s.map("Accent.TButton", background=[("active", COL["accent_hi"]), ("pressed", COL["accent_hi"])],
          lightcolor=[("active", COL["accent_hi"])], darkcolor=[("active", COL["accent_hi"])],
          bordercolor=[("active", COL["accent_hi"])])
    s.configure("Ghost.TButton", background=panel, lightcolor=panel, darkcolor=panel, bordercolor=panel)
    s.map("Ghost.TButton", background=[("active", panel2)], lightcolor=[("active", panel2)],
          darkcolor=[("active", panel2)], bordercolor=[("active", panel2)])
    side = COL["side"]
    s.configure("Side.TFrame", background=side)
    s.configure("Side.TLabel", background=side, foreground=fg)
    s.configure("SideMuted.TLabel", background=side, foreground=COL["muted"])
    s.configure("Side.TMenubutton", background=side, lightcolor=side, darkcolor=side, bordercolor=side,
                foreground=COL["muted"], arrowcolor=COL["faint"], padding=(6, 4))
    s.map("Side.TMenubutton", background=[("active", COL["raised"])], lightcolor=[("active", COL["raised"])],
          darkcolor=[("active", COL["raised"])], bordercolor=[("active", COL["raised"])])
    s.configure("Bg.TButton", background=bg, lightcolor=bg, darkcolor=bg, bordercolor=COL["border"])
    s.configure("Ghost.TMenubutton", background=panel, lightcolor=panel, darkcolor=panel, bordercolor=panel,
                foreground=COL["fg"], arrowcolor=COL["muted"], padding=(8, 4))
    s.map("Ghost.TMenubutton", background=[("active", panel2)], lightcolor=[("active", panel2)],
          darkcolor=[("active", panel2)], bordercolor=[("active", panel2)])
    s.configure("Danger.TButton", background=panel2, foreground=COL["danger"])
    s.configure("Page.TButton", padding=(10, 4))
    s.configure("PageOn.TButton", padding=(10, 4), background=COL["accent"], lightcolor=COL["accent"],
                darkcolor=COL["accent"], bordercolor=COL["accent"])
    s.map("PageOn.TButton", background=[("active", COL["accent_hi"])])
    s.configure("TCheckbutton", background=panel, foreground=fg)
    s.map("TCheckbutton", background=[("active", panel)], indicatorcolor=[("selected", COL["accent"])])
    s.configure("TRadiobutton", background=panel, foreground=fg)
    s.map("TRadiobutton", background=[("active", panel)], indicatorcolor=[("selected", COL["accent"])])
    s.configure("TEntry", foreground=fg, fieldbackground=panel2, padding=(6, 4), lightcolor=panel2,
                darkcolor=panel2, bordercolor=COL["border"])
    s.map("TEntry", bordercolor=[("focus", COL["accent"])], lightcolor=[("focus", COL["accent"])])
    s.configure("TCombobox", foreground=fg, fieldbackground=panel2, background=panel2, arrowcolor=fg,
                padding=(6, 4), lightcolor=panel2, darkcolor=panel2, bordercolor=COL["border"])
    s.map("TCombobox", fieldbackground=[("readonly", panel2)], foreground=[("readonly", fg)],
          selectbackground=[("readonly", panel2)], selectforeground=[("readonly", fg)],
          background=[("active", COL["raised"])])
    s.configure("TSpinbox", foreground=fg, fieldbackground=panel2, arrowcolor=fg, background=panel2,
                lightcolor=panel2, darkcolor=panel2, bordercolor=COL["border"])
    s.configure("Treeview", background=panel, fieldbackground=panel, foreground=fg, rowheight=30, borderwidth=0,
                font=(fam, 10))
    s.map("Treeview", background=[("selected", COL["accent"])], foreground=[("selected", "#ffffff")])
    s.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    s.configure("TLabelframe", background=panel)
    s.configure("TPanedwindow", background=bg)
    s.configure("Sash", sashthickness=6, gripcount=0, background=bg)
    s.configure("TSeparator", background=COL["border"])
    s.configure("TScrollbar", background=panel2, troughcolor=panel, bordercolor=panel, arrowcolor=COL["muted"],
                lightcolor=panel2, darkcolor=panel2, gripcount=0)
    s.map("TScrollbar", background=[("active", COL["raised"])])
    s.configure("TScale", background=panel, troughcolor=bg)
    s.configure("TNotebook", background=bg, borderwidth=0)
    s.configure("TNotebook.Tab", background=panel2, foreground=fg, padding=(16, 6))
    s.map("TNotebook.Tab", background=[("selected", COL["accent"])])
    root.option_add("*TCombobox*Listbox.background", panel2)
    root.option_add("*TCombobox*Listbox.foreground", fg)
    root.option_add("*TCombobox*Listbox.selectBackground", COL["accent"])
    root.option_add("*TCombobox*Listbox.borderWidth", 0)
    root.option_add("*Menu.background", panel2)
    root.option_add("*Menu.foreground", fg)
    root.option_add("*Menu.activeBackground", COL["accent"])
    root.option_add("*Menu.activeForeground", COL["on_accent"])
    root.option_add("*Text.background", panel2)
    root.option_add("*Text.foreground", fg)
    root.option_add("*Listbox.background", panel2)
    root.option_add("*Listbox.foreground", fg)
    root.option_add("*Menu.borderWidth", 0)
    return fam


def font(root, size=10, weight="normal"):
    return (_family(root), size, weight)


# ===================================================================== Symbole
EXTRA_GLYPHS = {}  # Name -> fn(size, color) -> RGBA-Bild (von Programmteilen eingetragen)


def glyph(name, size=20, color=None, bg=None, circle=None):
    """Symbol als RGBA-Bild. circle: Hintergrundkreis in dieser Farbe."""
    color = color or COL["fg"]
    if name in EXTRA_GLYPHS and not bg and not circle:
        return EXTRA_GLYPHS[name](size, color)
    k = 4
    S = size * k
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if bg:
        d.rectangle((0, 0, S, S), fill=bg)
    if circle:
        d.ellipse((0, 0, S - 1, S - 1), fill=circle)
        pad = S * 0.22
    else:
        pad = S * 0.06
    c = color
    box = (pad, pad, S - pad, S - pad)
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    lw = max(2, round(S * 0.075))

    def P(px, py):
        return (x0 + px * w, y0 + py * h)

    def line(*pts, width=lw):
        d.line([P(*p) for p in pts], fill=c, width=width, joint="curve")
        for p in (pts[0], pts[-1]):
            q = P(*p)
            d.ellipse((q[0] - width / 2, q[1] - width / 2, q[0] + width / 2, q[1] + width / 2), fill=c)

    def rect(a, b, cc, dd, r=0.1, fill=True):
        d.rounded_rectangle((*P(a, b), *P(cc, dd)), radius=r * w, fill=c if fill else None,
                            outline=None if fill else c, width=lw)

    def ell(a, b, cc, dd, fill=True):
        d.ellipse((*P(a, b), *P(cc, dd)), fill=c if fill else None, outline=None if fill else c, width=lw)

    def arc(a, b, cc, dd, s, e):
        d.arc((*P(a, b), *P(cc, dd)), s, e, fill=c, width=lw)

    def slash():
        d.line([P(0.08, 0.92), P(0.92, 0.08)], fill=COL["danger"] if not circle else c, width=lw + 2)

    n = name
    if n in ("mic", "mic_off"):
        rect(0.34, 0.02, 0.66, 0.62, r=0.16)
        arc(0.18, 0.28, 0.82, 0.78, 0, 180)
        line((0.5, 0.78), (0.5, 0.94))
        line((0.32, 0.95), (0.68, 0.95))
        if n == "mic_off":
            slash()
    elif n in ("headphones", "headphones_off"):
        arc(0.08, 0.06, 0.92, 0.9, 180, 360)
        line((0.08, 0.48), (0.08, 0.62))
        line((0.92, 0.48), (0.92, 0.62))
        rect(0.02, 0.56, 0.28, 0.96, r=0.08)
        rect(0.72, 0.56, 0.98, 0.96, r=0.08)
        if n == "headphones_off":
            slash()
    elif n in ("speaker", "speaker_off"):
        d.polygon([P(0.04, 0.36), P(0.24, 0.36), P(0.5, 0.1), P(0.5, 0.9), P(0.24, 0.64), P(0.04, 0.64)], fill=c)
        if n == "speaker":
            arc(0.36, 0.28, 0.72, 0.72, -50, 50)
            arc(0.3, 0.1, 0.96, 0.9, -50, 50)
        else:
            line((0.64, 0.34), (0.94, 0.66))
            line((0.94, 0.34), (0.64, 0.66))
    elif n in ("stream", "stream_off"):
        ell(0.4, 0.4, 0.6, 0.6)
        arc(0.24, 0.24, 0.76, 0.76, 135, 225)
        arc(0.24, 0.24, 0.76, 0.76, -45, 45)
        arc(0.04, 0.04, 0.96, 0.96, 135, 225)
        arc(0.04, 0.04, 0.96, 0.96, -45, 45)
        if n == "stream_off":
            slash()
    elif n == "music":
        ell(0.04, 0.68, 0.36, 0.96)
        ell(0.6, 0.58, 0.92, 0.86)
        line((0.34, 0.82), (0.34, 0.12))
        line((0.9, 0.72), (0.9, 0.04))
        d.polygon([P(0.34, 0.1), P(0.92, 0.0), P(0.92, 0.2), P(0.34, 0.3)], fill=c)
    elif n == "browser":
        ell(0.04, 0.04, 0.96, 0.96, fill=False)
        ell(0.3, 0.04, 0.7, 0.96, fill=False)
        line((0.06, 0.5), (0.94, 0.5))
    elif n == "chat":
        rect(0.02, 0.08, 0.98, 0.72, r=0.18)
        d.polygon([P(0.2, 0.68), P(0.16, 0.96), P(0.46, 0.7)], fill=c)
    elif n == "game":
        rect(0.02, 0.24, 0.98, 0.8, r=0.26)
        ink = (0, 0, 0, 0) if not circle else circle
        d.rectangle((*P(0.2, 0.47), *P(0.42, 0.57)), fill=ink)
        d.rectangle((*P(0.26, 0.4), *P(0.36, 0.64)), fill=ink)
        d.ellipse((*P(0.62, 0.38), *P(0.72, 0.48)), fill=ink)
        d.ellipse((*P(0.72, 0.52), *P(0.82, 0.62)), fill=ink)
    elif n == "sfx":
        pts = []
        for i in range(10):
            r = 0.48 if i % 2 == 0 else 0.2
            a = -math.pi / 2 + i * math.pi / 5
            pts.append(P(0.5 + r * math.cos(a), 0.52 + r * math.sin(a)))
        d.polygon(pts, fill=c)
    elif n == "system":
        rect(0.02, 0.06, 0.98, 0.7, r=0.08, fill=False)
        line((0.5, 0.72), (0.5, 0.88))
        line((0.26, 0.92), (0.74, 0.92))
    elif n == "wave":
        for i, hh in enumerate((0.3, 0.6, 0.95, 0.6, 0.3)):
            x = 0.1 + i * 0.2
            line((x, 0.5 - hh / 2), (x, 0.5 + hh / 2))
    elif n == "keyboard":
        rect(0.0, 0.2, 1.0, 0.8, r=0.1, fill=False)
        for ry in (0.38, 0.52):
            for i in range(5):
                xx = 0.14 + i * 0.18
                d.rectangle((*P(xx, ry), *P(xx + 0.08, ry + 0.07)), fill=c)
        line((0.3, 0.68), (0.7, 0.68))
    elif n == "navigate":
        line((0.12, 0.5), (0.88, 0.5))
        line((0.56, 0.18), (0.88, 0.5), (0.56, 0.82))
    elif n == "grid":
        for gy in (0.04, 0.54):
            for gx in (0.04, 0.54):
                rect(gx, gy, gx + 0.42, gy + 0.42, r=0.1)
    elif n == "layers":
        d.polygon([P(0.5, 0.06), P(0.96, 0.3), P(0.5, 0.54), P(0.04, 0.3)], fill=c)
        line((0.06, 0.52), (0.5, 0.74), (0.94, 0.52))
        line((0.06, 0.74), (0.5, 0.96), (0.94, 0.74))
    elif n == "record":
        ell(0.04, 0.04, 0.96, 0.96, fill=False)
        ell(0.26, 0.26, 0.74, 0.74)
    elif n == "play":
        d.polygon([P(0.18, 0.06), P(0.18, 0.94), P(0.92, 0.5)], fill=c)
    elif n == "puzzle":
        rect(0.06, 0.24, 0.8, 0.94, r=0.08)
        ell(0.3, 0.02, 0.56, 0.32)
        ell(0.68, 0.46, 0.98, 0.72)
    elif n == "search":
        ell(0.04, 0.04, 0.7, 0.7, fill=False)
        line((0.62, 0.62), (0.94, 0.94))
    elif n == "image":
        rect(0.02, 0.1, 0.98, 0.9, r=0.1, fill=False)
        ell(0.62, 0.22, 0.82, 0.42)
        d.polygon([P(0.1, 0.82), P(0.38, 0.42), P(0.58, 0.68), P(0.7, 0.56), P(0.9, 0.82)], fill=c)
    elif n == "more":
        for x in (0.15, 0.5, 0.85):
            ell(x - 0.1, 0.4, x + 0.1, 0.6)
    elif n == "fx":
        for i, (lo, hi) in enumerate(((0.1, 0.9), (0.1, 0.9), (0.1, 0.9))):
            x = 0.18 + i * 0.32
            line((x, lo), (x, hi))
        for i, y in enumerate((0.35, 0.7, 0.45)):
            x = 0.18 + i * 0.32
            rect(x - 0.13, y - 0.08, x + 0.13, y + 0.08, r=0.04)
    elif n == "close":
        line((0.15, 0.15), (0.85, 0.85))
        line((0.85, 0.15), (0.15, 0.85))
    elif n == "up":
        line((0.15, 0.68), (0.5, 0.3), (0.85, 0.68))
    elif n == "down":
        line((0.15, 0.32), (0.5, 0.7), (0.85, 0.32))
    elif n == "input":
        d.polygon([P(0.04, 0.34), P(0.46, 0.34), P(0.46, 0.12), P(0.86, 0.5), P(0.46, 0.88), P(0.46, 0.66),
                   P(0.04, 0.66)], fill=c)
    elif n == "plus":
        line((0.5, 0.1), (0.5, 0.9))
        line((0.1, 0.5), (0.9, 0.5))
    elif n == "globe":
        ell(0.04, 0.04, 0.96, 0.96, fill=False)
        ell(0.3, 0.04, 0.7, 0.96, fill=False)
        line((0.06, 0.5), (0.94, 0.5))
        arc(0.12, -0.3, 0.88, 0.36, 40, 140)
        arc(0.12, 0.64, 0.88, 1.3, 220, 320)
    elif n == "sun":
        ell(0.3, 0.3, 0.7, 0.7, fill=False)
        for i in range(8):
            a = i * math.pi / 4
            line((0.5 + 0.34 * math.cos(a), 0.5 + 0.34 * math.sin(a)),
                 (0.5 + 0.46 * math.cos(a), 0.5 + 0.46 * math.sin(a)))
    elif n == "moon":
        ell(0.06, 0.06, 0.94, 0.94)
        ink = circle or (0, 0, 0, 0)
        d.ellipse((*P(0.34, -0.12), *P(1.12, 0.66)), fill=ink)
    elif n == "auto":
        ell(0.08, 0.08, 0.92, 0.92, fill=False)
        d.pieslice((*P(0.08, 0.08), *P(0.92, 0.92)), 90, 270, fill=c)
    elif n == "gear":
        ell(0.3, 0.3, 0.7, 0.7, fill=False)
        for i in range(8):
            a = i * math.pi / 4
            line((0.5 + 0.3 * math.cos(a), 0.5 + 0.3 * math.sin(a)),
                 (0.5 + 0.46 * math.cos(a), 0.5 + 0.46 * math.sin(a)), width=lw + 2)
    elif n in ("undo", "redo"):
        sx = (lambda x: x) if n == "undo" else (lambda x: 1 - x)
        line((sx(0.36), 0.6), (sx(0.1), 0.36), (sx(0.36), 0.12))
        line((sx(0.12), 0.36), (sx(0.62), 0.36))
        if n == "undo":
            d.arc((*P(0.42, 0.36), *P(0.94, 0.86)), -90, 90, fill=c, width=lw)
        else:
            d.arc((*P(0.06, 0.36), *P(0.58, 0.86)), 90, 270, fill=c, width=lw)
        line((sx(0.66), 0.86), (sx(0.48), 0.86))
    elif n == "spark":
        d.polygon([P(0.5, 0.02), P(0.62, 0.38), P(0.98, 0.5), P(0.62, 0.62), P(0.5, 0.98), P(0.38, 0.62),
                   P(0.02, 0.5), P(0.38, 0.38)], fill=c)
    elif n == "arrow":
        line((0.1, 0.5), (0.9, 0.5))
        line((0.58, 0.2), (0.9, 0.5), (0.58, 0.8))
    elif n == "chevron":
        line((0.2, 0.36), (0.5, 0.66), (0.8, 0.36))
    elif n == "folder":
        d.polygon([P(0.04, 0.16), P(0.38, 0.16), P(0.48, 0.28), P(0.96, 0.28), P(0.96, 0.86), P(0.04, 0.86)],
                  fill=c)
    elif n == "text":
        line((0.14, 0.16), (0.86, 0.16))
        line((0.5, 0.16), (0.5, 0.9))
    elif n == "camera":
        rect(0.02, 0.22, 0.68, 0.78, r=0.1)
        d.polygon([P(0.7, 0.42), P(0.98, 0.24), P(0.98, 0.76), P(0.7, 0.58)], fill=c)
    elif n == "check":
        line((0.12, 0.52), (0.4, 0.8), (0.9, 0.2))
    elif n == "trash":
        line((0.1, 0.22), (0.9, 0.22))
        line((0.38, 0.1), (0.62, 0.1))
        d.polygon([P(0.18, 0.28), P(0.82, 0.28), P(0.74, 0.94), P(0.26, 0.94)], fill=c)
    elif n == "edit":
        d.polygon([P(0.66, 0.08), P(0.92, 0.34), P(0.36, 0.9), P(0.1, 0.9), P(0.1, 0.64)], fill=c)
    elif n in ("import", "export"):
        rect(0.06, 0.56, 0.94, 0.94, r=0.08, fill=False)
        if n == "import":
            line((0.5, 0.06), (0.5, 0.62))
            line((0.28, 0.42), (0.5, 0.64), (0.72, 0.42))
        else:
            line((0.5, 0.64), (0.5, 0.08))
            line((0.28, 0.28), (0.5, 0.06), (0.72, 0.28))
    elif n == "deck":
        for gy in (0.08, 0.54):
            for gx in (0.08, 0.54):
                rect(gx, gy, gx + 0.38, gy + 0.38, r=0.12)
    else:  # Punkt als Ersatz
        ell(0.3, 0.3, 0.7, 0.7)
    return im.resize((size, size), Image.LANCZOS)


class Icons:
    """Hält PhotoImages am Leben (Tk vergisst sie sonst)."""

    def __init__(self, master):
        self.master = master
        self._cache = {}

    def get(self, name, size=20, color=None, circle=None):
        color = color or COL["fg"]
        key = (name, size, color, circle)
        if key not in self._cache:
            self._cache[key] = ImageTk.PhotoImage(glyph(name, size, color, circle=circle), master=self.master)
        return self._cache[key]


# ===================================================================== Widgets
def language_button(parent, icons, app, restart, bg=None, style="Ghost.TMenubutton"):
    """Knopf mit Globus und Sprachname; Auswahl speichert die Sprache und bietet einen Neustart an."""
    from . import i18n
    from .i18n import _

    cur = i18n.configured()
    btn = ttk.Menubutton(parent, text=" " + i18n.name(i18n.current()), image=icons.get("globe", 16, COL["muted"]),
                         compound="left", style=style)
    menu = tk.Menu(btn, tearoff=False, bg=COL["panel2"], fg=COL["fg"], activebackground=COL["accent"],
                   activeforeground="#ffffff", bd=0)
    var = tk.StringVar(btn, value=cur if cur == "auto" or cur in i18n.available() else "auto")
    btn._var = var  # am Leben halten

    def pick():
        choose_language(var.get(), app, restart, parent)

    menu.add_radiobutton(label=_("System language"), value="auto", variable=var, command=pick)
    menu.add_separator()
    for code in i18n.available():
        menu.add_radiobutton(label=i18n.name(code), value=code, variable=var, command=pick)
    btn["menu"] = menu
    return btn


def choose_language(code, app, restart, parent):
    """Sprache speichern ("auto" = Systemsprache) und einen Neustart anbieten, wenn sie sich ändert."""
    from tkinter import messagebox

    from . import i18n
    from .i18n import _

    i18n.save_language(code)
    new = i18n.detect()
    if new == i18n.current():
        return
    msg = _("{app} will be shown in {language} after a restart. Restart now?").format(
        app=app, language=i18n.name(new))
    if messagebox.askyesno(_("Language"), msg, parent=parent.winfo_toplevel()):
        restart()


def restart_later(module):
    """Startet `python -m <module>` neu, sobald dieser Prozess beendet ist."""
    import os
    import subprocess
    import sys
    env = dict(os.environ)
    env.pop("STREAMLIN_LANG", None)
    env.pop("LINDECK_LANG", None)
    subprocess.Popen(["sh", "-c", 'sleep 2; exec "$@"', "sh", sys.executable, "-m", module],
                     env=env, start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def rounded_rect(cv, x0, y0, x1, y1, r, **kw):
    r = max(0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
           x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return cv.create_polygon(pts, smooth=True, **kw)


class Card(tk.Canvas):
    """Fläche mit runden Ecken; Widgets kommen in card.body (tk.Frame)."""

    def __init__(self, master, color=None, radius=14, pad=14, outline=None, parent_bg=None, stretch=False,
                 stripe=None, **kw):
        color = color or COL["panel"]
        outline = COL["border"] if outline is None else outline
        self.stripe = stripe  # farbiger Streifen oben (Kanal-Karten)
        bg = parent_bg or _bg_of(master)
        super().__init__(master, bg=bg, highlightthickness=0, bd=0, **kw)
        self.color, self.radius, self.pad, self.outline = color, radius, pad, outline
        self.stretch = stretch  # Inhalt auf die ganze Breite ziehen (pack(fill="x"))
        self.body = tk.Frame(self, bg=color)
        self._win = self.create_window(pad, pad, window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._fit)
        self.bind("<Configure>", self._draw)
        self._fixed_w = kw.get("width")
        self._fixed_h = kw.get("height")

    def _fit(self, _e=None):
        w = self.body.winfo_reqwidth() + 2 * self.pad
        h = self.body.winfo_reqheight() + 2 * self.pad
        self.configure(width=self._fixed_w or w, height=self._fixed_h or h)
        self._draw()

    def _draw(self, _e=None):
        self.delete("bg")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4 or h < 4:
            w, h = int(self["width"]), int(self["height"])
        rounded_rect(self, 0, 0, w - 1, h - 1, self.radius, fill=self.color, outline=self.outline, tags="bg")
        if self.stripe:
            rounded_rect(self, 12, -3, w - 13, 3, 3, fill=self.stripe, outline="", tags="bg")
        self.tag_lower("bg")
        if self._fixed_w or self._fixed_h or (self.stretch and w - 2 * self.pad >= self.body.winfo_reqwidth()):
            self.itemconfigure(self._win, width=max(1, w - 2 * self.pad))

    def set_color(self, color):
        self.color = color
        self.body.configure(bg=color)
        _recolor(self.body, color)
        self._draw()


def _bg_of(w):
    try:
        return w.cget("bg")
    except tk.TclError:
        try:
            st = w.cget("style") or w.winfo_class()
            return ttk.Style(w).lookup(st, "background") or COL["bg"]
        except tk.TclError:
            return COL["bg"]


def _recolor(frame, color):
    for ch in frame.winfo_children():
        if isinstance(ch, (tk.Frame, tk.Label)) and getattr(ch, "_follow_bg", True):
            ch.configure(bg=color)
        if isinstance(ch, tk.Canvas) and getattr(ch, "_follow_bg", True):
            ch.configure(bg=color)
        if isinstance(ch, tk.Frame):
            _recolor(ch, color)


def label(master, text="", size=10, weight="normal", color=None, textvariable=None, **kw):
    bg = kw.pop("bg", None) or _bg_of(master)
    lb = tk.Label(master, text=text, textvariable=textvariable, bg=bg, fg=color or COL["fg"],
                  font=font(master, size, weight), **kw)
    return lb


class Fader(tk.Canvas):
    """Schieberegler wie in Wave Link: Spur, farbige Füllung, runder Knopf.

    command(value) wird bei jeder Bewegung aufgerufen; set() ruft ihn nicht auf.
    """

    def __init__(self, master, length=200, thickness=34, orient="vertical", from_=0, to=100, value=0,
                 color=None, command=None, **kw):
        color = color or COL["accent"]
        self.orient = orient
        w, h = (thickness, length) if orient == "vertical" else (length, thickness)
        super().__init__(master, width=w, height=h, bg=_bg_of(master), highlightthickness=0, bd=0, **kw)
        self.lo, self.hi = from_, to
        self.value = value
        self.color = color
        self.command = command
        self.enabled = True
        self.dimmed = False
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._press)
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Button-4>", lambda e: self._step(+2))
        self.bind("<Button-5>", lambda e: self._step(-2))
        self._draw()

    def get(self):
        return self.value

    def set(self, value):
        self.value = max(self.lo, min(self.hi, value))
        self._draw()

    def set_dimmed(self, dimmed):
        if dimmed != self.dimmed:
            self.dimmed = dimmed
            self._draw()

    def _geom(self):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4:
            w, h = int(self["width"]), int(self["height"])
        return w, h

    def _frac(self):
        return (self.value - self.lo) / ((self.hi - self.lo) or 1)

    def _draw(self):
        self.delete("all")
        w, h = self._geom()
        f = self._frac()
        col = mix(self.color, COL["panel2"], 0.65) if self.dimmed else self.color
        if self.orient == "vertical":
            tw = max(6, w // 4)
            x0, x1 = (w - tw) / 2, (w + tw) / 2
            knob = min(w - 4, 26)
            top, bot = knob / 2 + 2, h - knob / 2 - 2
            y = bot - f * (bot - top)
            rounded_rect(self, x0, top - tw / 2, x1, bot + tw / 2, tw / 2, fill=COL["raised"], outline="")
            if f > 0:
                rounded_rect(self, x0, y, x1, bot + tw / 2, tw / 2, fill=col, outline="")
            cx, cy = w / 2, y
        else:
            th = max(6, h // 4)
            y0, y1 = (h - th) / 2, (h + th) / 2
            knob = min(h - 4, 22)
            left, right = knob / 2 + 2, w - knob / 2 - 2
            x = left + f * (right - left)
            rounded_rect(self, left - th / 2, y0, right + th / 2, y1, th / 2, fill=COL["raised"], outline="")
            if f > 0:
                rounded_rect(self, left - th / 2, y0, x, y1, th / 2, fill=col, outline="")
            cx, cy = x, h / 2
        self._knob = _knob_image(self, int(knob), col, self.enabled)
        self.create_image(cx, cy + 1, image=self._knob)

    def _to_value(self, e):
        w, h = self._geom()
        if self.orient == "vertical":
            knob = min(w - 4, 26)
            top, bot = knob / 2 + 2, h - knob / 2 - 2
            f = (bot - e.y) / ((bot - top) or 1)
        else:
            knob = min(h - 4, 22)
            left, right = knob / 2 + 2, w - knob / 2 - 2
            f = (e.x - left) / ((right - left) or 1)
        return self.lo + max(0.0, min(1.0, f)) * (self.hi - self.lo)

    def _changed(self, v):
        v = max(self.lo, min(self.hi, v))
        if v != self.value:
            self.value = v
            self._draw()
            if self.command:
                self.command(v)

    def _press(self, e):
        if self.enabled:
            self._changed(self._to_value(e))

    def _step(self, delta):
        if self.enabled:
            self._changed(self.value + delta * (self.hi - self.lo) / 100)

    def _wheel(self, e):
        self._step(2 if e.delta > 0 else -2)


_KNOBS = {}


def _knob_image(master, d, color, enabled=True):
    """Kantengeglätteter Reglerknopf (weiß mit farbigem Punkt und Schatten)."""
    cache = _KNOBS.setdefault(master.tk, {})  # Bilder gehören zu genau einem Tk-Interpreter
    key = (d, color, enabled)
    if key not in cache:
        k = 4
        size = (d + 4) * k
        im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        dr = ImageDraw.Draw(im)
        o = 2 * k
        dr.ellipse((o, o + 2 * k, o + d * k, o + d * k + 2 * k - 1), fill=(0, 0, 0, 110))
        # weißer Knopf mit farbigem Ring (Streamlin-Design)
        dr.ellipse((o, o, o + d * k, o + d * k), fill=rgb(color if enabled else COL["faint"]) + (255,))
        r = d * k * 0.3
        c = o + d * k / 2
        dr.ellipse((c - r, c - r, c + r, c + r), fill=rgb("#ffffff") + (255,))
        cache[key] = ImageTk.PhotoImage(im.resize((d + 4, d + 4), Image.LANCZOS), master=master)
    return cache[key]


class RoundToggle(tk.Canvas):
    """Runder Knopf mit Symbol. on = aktiv (z. B. „stumm“ → rot)."""

    def __init__(self, master, icon_off, icon_on, size=34, on_color=None, off_color=None,
                 command=None, tooltip=""):
        on_color = on_color or COL["danger"]
        off_color = off_color or COL["raised"]
        super().__init__(master, width=size, height=size, bg=_bg_of(master), highlightthickness=0, bd=0,
                         cursor="hand2")
        self.size = size
        self.state_on = False
        self.on_color, self.off_color = on_color, off_color
        self.command = command
        self.icons = Icons(self)
        self.icon_off, self.icon_on = icon_off, icon_on
        self.hover = False
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self.tooltip = tooltip
        self._draw()

    def get(self):
        return self.state_on

    def set(self, on):
        on = bool(on)
        if on != self.state_on:
            self.state_on = on
            self._draw()

    def _hover(self, v):
        self.hover = v
        self._draw()

    def _draw(self):
        self.delete("all")
        s = self.size
        fill = self.on_color if self.state_on else (mix(self.off_color, "#ffffff", 0.08) if self.hover
                                                     else self.off_color)
        self._disc = _disc_image(self, s, fill)
        self.create_image(s / 2, s / 2, image=self._disc)
        ic = self.icons.get(self.icon_on if self.state_on else self.icon_off, int(s * 0.5),
                            "#ffffff" if self.state_on else COL["fg"])
        self.create_image(s / 2, s / 2, image=ic)

    def _click(self, _e):
        self.state_on = not self.state_on
        self._draw()
        if self.command:
            self.command()


def _disc_image(master, d, color):
    cache = _KNOBS.setdefault(master.tk, {})
    key = ("disc", d, color)
    if key not in cache:
        k = 4
        im = Image.new("RGBA", (d * k, d * k), (0, 0, 0, 0))
        ImageDraw.Draw(im).ellipse((k, k, d * k - k, d * k - k), fill=rgb(color) + (255,))
        cache[key] = ImageTk.PhotoImage(im.resize((d, d), Image.LANCZOS), master=master)
    return cache[key]


class Switch(tk.Canvas):
    """Schalter an/aus (wie in den Einstellungen von Wave Link)."""

    def __init__(self, master, command=None, width=42, height=24, color=None):
        color = color or COL["accent"]
        super().__init__(master, width=width, height=height, bg=_bg_of(master), highlightthickness=0, bd=0,
                         cursor="hand2")
        self.w, self.h = width, height
        self.on = False
        self.enabled = True
        self.color = color
        self.command = command
        self.bind("<Button-1>", self._click)
        self._draw()

    def get(self):
        return self.on

    def set(self, on):
        on = bool(on)
        if on != self.on:
            self.on = on
            self._draw()

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.configure(cursor="hand2" if enabled else "")
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.w, self.h
        track = (self.color if self.on else COL["raised"]) if self.enabled else COL["panel2"]
        rounded_rect(self, 1, 1, w - 1, h - 1, h / 2, fill=track, outline="")
        r = h / 2 - 3
        cx = w - h / 2 if self.on else h / 2
        self._dot = _disc_image(self, int(2 * r), "#ffffff" if self.enabled else COL["faint"])
        self.create_image(cx, h / 2, image=self._dot)

    def _click(self, _e):
        if not self.enabled:
            return
        self.on = not self.on
        self._draw()
        if self.command:
            self.command()


class PillButton(tk.Canvas):
    """Kleiner, runder Knopf mit Text und/oder Symbol (z. B. „FX 2“)."""

    def __init__(self, master, text="", icon=None, command=None, active=False, height=26, padx=12):
        super().__init__(master, height=height, bg=_bg_of(master), highlightthickness=0, bd=0, cursor="hand2")
        self.text, self.icon, self.command, self.active = text, icon, command, active
        self.h, self.padx = height, padx
        self.hover = False
        self.icons = Icons(self)
        self.f = font(self, 9, "bold")
        self.bind("<Button-1>", lambda e: self.command and self.command())
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self._draw()

    def _hover(self, v):
        self.hover = v
        self._draw()

    def set(self, text=None, active=None):
        if text is not None:
            self.text = text
        if active is not None:
            self.active = active
        self._draw()

    def _draw(self):
        self.delete("all")
        fnt = tkfont.Font(self, font=self.f)
        tw = fnt.measure(self.text) if self.text else 0
        iw = 14 if self.icon else 0
        w = self.padx * 2 + tw + iw + (6 if tw and iw else 0)
        self.configure(width=w)
        base = COL["accent"] if self.active else COL["raised"]
        fill = mix(base, "#ffffff", 0.1) if self.hover else base
        rounded_rect(self, 0, 0, w, self.h, self.h / 2, fill=fill, outline="")
        x = self.padx
        fg = "#ffffff" if self.active else COL["fg"]
        if self.icon:
            self.create_image(x + 7, self.h / 2, image=self.icons.get(self.icon, 14, fg))
            x += iw + 6
        if self.text:
            self.create_text(x, self.h / 2, text=self.text, font=self.f, fill=fg, anchor="w")


class HScroll(tk.Frame):
    """Waagerecht scrollbarer Bereich; Inhalt in .inner."""

    def __init__(self, master, height=None):
        bg = _bg_of(master)
        super().__init__(master, bg=bg)
        self.cv = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0, height=height or 10)
        self.bar = ttk.Scrollbar(self, orient="horizontal", command=self.cv.xview)
        self.cv.configure(xscrollcommand=self._on_scroll)
        self.cv.pack(fill="x")
        self.inner = tk.Frame(self.cv, bg=bg)
        self.cv.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._fit)
        self.cv.bind("<Configure>", self._fit)
        for w in (self.cv, self.inner):
            w.bind("<Shift-MouseWheel>", lambda e: self.cv.xview_scroll(-1 if e.delta > 0 else 1, "units"))
            w.bind("<Shift-Button-4>", lambda e: self.cv.xview_scroll(-3, "units"))
            w.bind("<Shift-Button-5>", lambda e: self.cv.xview_scroll(3, "units"))

    def _fit(self, _e=None):
        self.cv.configure(height=self.inner.winfo_reqheight(), scrollregion=(0, 0, self.inner.winfo_reqwidth(),
                                                                               self.inner.winfo_reqheight()))

    def _on_scroll(self, lo, hi):
        if float(lo) <= 0 and float(hi) >= 1:
            self.bar.pack_forget()
        elif not self.bar.winfo_manager():
            self.bar.pack(fill="x", pady=(6, 0))
        self.bar.set(lo, hi)


class Dialog(tk.Toplevel):
    """Dialogfenster im Linewave/LinDeck-Stil."""

    def __init__(self, master, title, width=520, height=None):
        super().__init__(master)
        self.title(title)
        self.configure(bg=COL["bg"])
        self.transient(master.winfo_toplevel())
        self.resizable(True, True)
        if height:
            self.geometry(f"{width}x{height}")
        self.minsize(width, 200)
        self.body = tk.Frame(self, bg=COL["bg"], padx=22, pady=18)
        self.body.pack(fill="both", expand=True)
        self.buttons = tk.Frame(self, bg=COL["panel"], padx=16, pady=12)
        self.buttons.pack(fill="x", side="bottom")
        self.bind("<Escape>", lambda e: self.destroy())

    def show(self):
        self.update_idletasks()
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.focus_set()


class Segmented(tk.Frame):
    """Umschalter mit mehreren Feldern; command(key) beim Wechsel."""

    def __init__(self, master, items, command=None, value=None):
        bg = _bg_of(master)
        super().__init__(master, bg=bg)
        self.command = command
        self.items = items
        self.value = value or items[0][0]
        self.cv = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0, height=34, cursor="hand2")
        self.cv.pack()
        self.f = font(self, 10, "bold")
        self.cv.bind("<Button-1>", self._click)
        self._draw()

    def _draw(self):
        cv = self.cv
        cv.delete("all")
        fnt = tkfont.Font(self, font=self.f)
        widths = [fnt.measure(t) + 36 for _k, t in self.items]
        total = sum(widths) + 8
        cv.configure(width=total)
        rounded_rect(cv, 0, 0, total, 34, 17, fill=COL["panel2"], outline="")
        x = 4
        self._hits = []
        for (k, t), w in zip(self.items, widths):
            if k == self.value:
                rounded_rect(cv, x, 4, x + w, 30, 13, fill=COL["accent"], outline="")
            cv.create_text(x + w / 2, 17, text=t, font=self.f, fill="#ffffff" if k == self.value else COL["muted"])
            self._hits.append((x, x + w, k))
            x += w

    def set(self, key):
        self.value = key
        self._draw()

    def _click(self, e):
        for a, b, k in self._hits:
            if a <= e.x <= b and k != self.value:
                self.set(k)
                if self.command:
                    self.command(k)
                return


# ================================================================ Streamlin 2
_IMG = {}


def _img_cache(master):
    return _IMG.setdefault(master.tk, {})


def gradient_mark(master, size, kind="deck"):
    """App-Zeichen: abgerundetes Quadrat mit Farbverlauf und weißem Symbol."""
    cache = _img_cache(master)
    key = ("mark", size, kind)
    if key not in cache:
        a, b = (("#5b7cff", "#8a5cff") if kind == "deck" else ("#22c3ee", "#c084fc"))
        k = 4
        S = size * k
        grad = Image.new("RGBA", (S, S))
        ca, cb = rgb(a), rgb(b)
        px = grad.load()
        for y in range(S):
            for x in range(S):
                t = (x + y) / (2 * S - 2)
                px[x, y] = tuple(round(u + (v - u) * t) for u, v in zip(ca, cb)) + (255,)
        mask = Image.new("L", (S, S), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, S - 1, S - 1), radius=S * 0.3, fill=255)
        im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        im.paste(grad, (0, 0), mask)
        g = glyph("deck" if kind == "deck" else "wave", int(S * 0.56), "#ffffff")
        im.alpha_composite(g, (int(S * 0.22), int(S * 0.22)))
        cache[key] = ImageTk.PhotoImage(im.resize((size, size), Image.LANCZOS), master=master)
    return cache[key]


def icon_tile(master, name, size, color, radius=0.28, fg="#ffffff"):
    """Farbiges, abgerundetes Quadrat mit Symbol (Aktionen, Kanäle, Taste)."""
    cache = _img_cache(master)
    key = ("tile", name, size, color, radius, fg)
    if key not in cache:
        k = 4
        S = size * k
        im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        ImageDraw.Draw(im).rounded_rectangle((0, 0, S - 1, S - 1), radius=S * radius, fill=rgb(color) + (255,))
        g = glyph(name, int(S * 0.5), fg)
        im.alpha_composite(g, (int(S * 0.25), int(S * 0.25)))
        cache[key] = ImageTk.PhotoImage(im.resize((size, size), Image.LANCZOS), master=master)
    return cache[key]


def section(master, text, bg=None):
    """Kleine Abschnittsüberschrift in Großbuchstaben (DEVICES, PROFILES …)."""
    return tk.Label(master, text=text.upper(), bg=bg or _bg_of(master), fg=COL["faint"],
                    font=font(master, 8, "bold"), anchor="w")


class Sidebar(tk.Frame):
    """Seitenleiste links; Inhalt in .top (oben) und .bottom (unten)."""

    def __init__(self, master, width=236):
        super().__init__(master, bg=COL["side"], width=width)
        self.pack_propagate(False)
        self.top = tk.Frame(self, bg=COL["side"], padx=14, pady=16)
        self.top.pack(side="top", fill="both", expand=True)
        self.bottom = tk.Frame(self, bg=COL["side"], padx=14, pady=14)
        self.bottom.pack(side="bottom", fill="x")

    def brand(self, name, kind):
        row = tk.Frame(self.top, bg=COL["side"])
        row.pack(fill="x", padx=4, pady=(0, 14))
        self._mark = gradient_mark(self, 32, kind)
        tk.Label(row, image=self._mark, bg=COL["side"]).pack(side="left")
        tk.Label(row, text=name, bg=COL["side"], fg=COL["fg"], font=font(self, 13, "bold")).pack(side="left", padx=10)
        return row

    def section(self, text, parent=None):
        lb = section(parent or self.top, text, bg=COL["side"])
        lb.pack(fill="x", padx=6, pady=(12, 6))
        return lb


class NavItem(tk.Canvas):
    """Eintrag der Seitenleiste: Symbol, Text, rechts eine Zahl; gewählt = hinterlegt."""

    def __init__(self, master, text, icon=None, count="", command=None, active=False, accent=False, height=34):
        bg = _bg_of(master)
        super().__init__(master, height=height, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self.text, self.icon, self.count, self.command = text, icon, count, command
        self.active, self.accent, self.hover, self.h = active, accent, False, height
        self.icons = Icons(self)
        self.f = font(self, 10, "normal")
        self.fb = font(self, 10, "bold")
        self.fs = font(self, 9, "normal")
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", lambda e: self.command and self.command())
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self._draw()

    def _set_hover(self, v):
        self.hover = v
        self._draw()

    def set(self, active=None, text=None, count=None):
        if active is not None:
            self.active = active
        if text is not None:
            self.text = text
        if count is not None:
            self.count = count
        self._draw()

    def _draw(self):
        self.delete("all")
        w = max(self.winfo_width(), 40)
        h = self.h
        if self.active or self.hover:
            rounded_rect(self, 0, 1, w - 1, h - 1, 9, outline="",
                         fill=COL["accent_soft"] if self.active else COL["raised"])
        fg = COL["accent"] if self.accent else (COL["fg"] if self.active else COL["muted"])
        x = 10
        if self.icon:
            ic = COL["accent"] if (self.active or self.accent) else COL["muted"]
            self.create_image(x + 8, h / 2, image=self.icons.get(self.icon, 16, ic))
            x += 26
        self.create_text(x, h / 2, text=self.text, anchor="w", fill=fg,
                         font=self.fb if self.active else self.f)
        if self.count != "":
            self.create_text(w - 10, h / 2, text=str(self.count), anchor="e", fill=COL["faint"], font=self.fs)


def elide(widget, text, fnt, width):
    """Text auf width Pixel kürzen („…“ am Ende)."""
    f = tkfont.Font(widget, font=fnt)
    if not text or f.measure(text) <= width:
        return text
    while text and f.measure(text + "…") > width:
        text = text[:-1]
    return text.rstrip() + "…"


class InfoCard(tk.Canvas):
    """Karte mit Bild links, Titel und Untertitel (Geräte, Mikrofon, Ausgänge)."""

    def __init__(self, master, title, subtitle="", image=None, dot=None, dim=False, command=None, height=56):
        bg = _bg_of(master)
        super().__init__(master, height=height, bg=bg, highlightthickness=0, bd=0,
                         cursor="hand2" if command else "")
        self.title, self.subtitle, self.image, self.dot, self.dim = title, subtitle, image, dot, dim
        self.selected = False
        self.h = height
        self.command = command
        self.f = font(self, 10, "bold")
        self.fs = font(self, 9, "normal")
        self.bind("<Configure>", lambda e: self._draw())
        if command:
            self.bind("<Button-1>", lambda e: command())
        self._draw()

    def set(self, title=None, subtitle=None, dot=None, dim=None, image=None, selected=None):
        for k, v in (("title", title), ("subtitle", subtitle), ("dot", dot), ("dim", dim), ("image", image),
                     ("selected", selected)):
            if v is not None:
                setattr(self, k, v)
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = max(self.winfo_width(), 60), self.h
        if not self.dim:
            rounded_rect(self, 0, 0, w - 1, h - 1, 12, fill=COL["panel"],
                         outline=COL["accent"] if self.selected else COL["border"])
        else:
            rounded_rect(self, 0, 0, w - 1, h - 1, 12, fill="", outline=COL["border"])
        x = 12
        if self.image is not None:
            self.create_image(x, h / 2, image=self.image, anchor="w")
            x += self.image.width() + 10
        tcol = COL["muted"] if self.dim else COL["fg"]
        room = w - x - 10
        self.create_text(x, h / 2 - 8, text=elide(self, self.title, self.f, room), anchor="w", fill=tcol,
                         font=self.f)
        sx = x
        if self.dot:
            self.create_oval(x, h / 2 + 5, x + 7, h / 2 + 12, fill=self.dot, outline="")
            sx += 12
        self.create_text(sx, h / 2 + 9, text=elide(self, self.subtitle, self.fs, room - (sx - x)), anchor="w",
                         fill=COL["muted"] if not self.dim else COL["faint"], font=self.fs)


class SoftSegmented(tk.Canvas):
    """Umschalter im Stil der Seitenleiste/Reiter: gewähltes Feld hell hinterlegt.

    items: [(key, text, icon or None)]; command(key) beim Wechsel.
    """

    def __init__(self, master, items, command=None, value=None, fill=False, height=32):
        bg = _bg_of(master)
        super().__init__(master, height=height, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self.items, self.command, self.fill, self.h = items, command, fill, height
        self.value = value or items[0][0]
        self.icons = Icons(self)
        self.f = font(self, 9, "bold")
        self.bind("<Button-1>", self._click)
        if fill:
            self.bind("<Configure>", lambda e: self._draw())
        self._draw()

    def set(self, key):
        self.value = key
        self._draw()

    def _draw(self):
        self.delete("all")
        fnt = tkfont.Font(self, font=self.f)
        nat = [fnt.measure(t) + (22 if ic else 0) + 22 for _k, t, ic in self.items]
        show_icons = True
        if self.fill:
            total = max(self.winfo_width(), 60)
            widths = [(total - 8) / len(self.items)] * len(self.items)
            # zu schmal für Symbol + Text: nur Text zeigen
            show_icons = all(w >= n for w, n in zip(widths, nat))
        else:
            widths = nat
            total = sum(widths) + 8
            self.configure(width=total)
        h = self.h
        rounded_rect(self, 0, 0, total - 1, h - 1, 10, fill=COL["panel2"], outline=COL["border"])
        x = 4
        self._hits = []
        for (k, t, ic), w in zip(self.items, widths):
            on = k == self.value
            if on:
                rounded_rect(self, x, 3, x + w, h - 4, 7, fill=COL["panel"] if MODE == "light" else COL["raised"],
                             outline="")
            fg = COL["fg"] if on else COL["muted"]
            ic = ic if show_icons else None
            tw = fnt.measure(t) + (20 if ic else 0)
            cx = x + (w - tw) / 2
            if ic:
                self.create_image(cx + 7, h / 2, image=self.icons.get(ic, 13, fg))
                cx += 20
            self.create_text(cx, h / 2, text=t, anchor="w", font=self.f, fill=fg)
            self._hits.append((x, x + w, k))
            x += w

    def _click(self, e):
        for a, b, k in self._hits:
            if a <= e.x <= b and k != self.value:
                self.set(k)
                if self.command:
                    self.command(k)
                return


def mode_switch(parent, app, restart):
    """Umschalter Hell / Dunkel / Auto; speichert und bietet einen Neustart an."""
    from tkinter import messagebox

    from .i18n import _

    def pick(mode):
        save_appearance(mode)
        if resolve_mode(mode) == MODE:
            return
        if messagebox.askyesno(_("Appearance"), _("{app} switches to the new look after a restart. Restart now?")
                               .format(app=app), parent=parent.winfo_toplevel()):
            restart()

    return SoftSegmented(parent, [("light", _("Light"), "sun"), ("dark", _("Dark"), "moon"),
                                  ("auto", _("Auto"), "auto")], command=pick, value=configured_appearance(),
                         fill=True)


class Button(tk.Canvas):
    """Knopf mit runden Ecken und feinem Rand. kind: default, primary, ghost, danger."""

    def __init__(self, master, text="", icon=None, command=None, kind="default", height=32, padx=12):
        bg = _bg_of(master)
        super().__init__(master, height=height, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self.text, self.icon, self.command, self.kind = text, icon, command, kind
        self.h, self.padx, self.hover, self.enabled = height, padx, False, True
        self.icons = Icons(self)
        self.f = font(self, 10, "normal" if kind != "primary" else "bold")
        self.bind("<ButtonRelease-1>", self._click)
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self._draw()

    def _click(self, e):
        if self.enabled and self.command and 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height():
            self.command()

    def _set_hover(self, v):
        self.hover = v
        self._draw()

    def configure_text(self, text=None, icon=None, enabled=None):
        if text is not None:
            self.text = text
        if icon is not None:
            self.icon = icon
        if enabled is not None:
            self.enabled = enabled
            self.configure(cursor="hand2" if enabled else "")
        self._draw()

    def _draw(self):
        self.delete("all")
        fnt = tkfont.Font(self, font=self.f)
        tw = fnt.measure(self.text) if self.text else 0
        iw = 16 if self.icon else 0
        pad = self.padx if self.text else (self.h - iw) / 2
        w = int(pad * 2 + tw + iw + (7 if tw and iw else 0))
        self.configure(width=w)
        k = self.kind
        if k == "primary":
            fill, out, fg = (COL["accent_hi"] if self.hover else COL["accent"]), "", COL["on_accent"]
        elif k == "ghost":
            fill, out, fg = (COL["raised"] if self.hover else ""), "", COL["muted"]
        else:
            fill = COL["raised"] if self.hover else COL["panel2"]
            out, fg = COL["border"], COL["danger"] if k == "danger" else COL["fg"]
        if not self.enabled:
            fg = COL["faint"]
        rounded_rect(self, 0, 0, w - 1, self.h - 1, 9, fill=fill, outline=out)
        x = pad
        if self.icon:
            self.create_image(x + 8, self.h / 2, image=self.icons.get(self.icon, 16, fg))
            x += iw + 7
        if self.text:
            self.create_text(x, self.h / 2, text=self.text, anchor="w", font=self.f, fill=fg)


class Chip(tk.Canvas):
    """Filter-Chip (rund, mit Rand); aktiv = Akzent hinterlegt."""

    def __init__(self, master, text, command=None, active=False, icon=None, height=26):
        super().__init__(master, height=height, bg=_bg_of(master), highlightthickness=0, bd=0, cursor="hand2")
        self.text, self.command, self.active, self.icon, self.h = text, command, active, icon, height
        self.icons = Icons(self)
        self.f = font(self, 9, "normal")
        self.bind("<Button-1>", lambda e: self.command and self.command())
        self._draw()

    def set(self, active=None, text=None):
        if active is not None:
            self.active = active
        if text is not None:
            self.text = text
        self._draw()

    def _draw(self):
        self.delete("all")
        fnt = tkfont.Font(self, font=self.f)
        iw = 18 if self.icon else 0
        w = fnt.measure(self.text) + 22 + iw
        self.configure(width=w)
        h = self.h
        if self.active:
            rounded_rect(self, 0, 0, w - 1, h - 1, h / 2, fill=COL["accent_soft"], outline="")
            fg = COL["accent"]
        else:
            rounded_rect(self, 0, 0, w - 1, h - 1, h / 2, fill=COL["panel"], outline=COL["border"])
            fg = COL["muted"]
        x = 11
        if self.icon:
            self.create_image(x + 6, h / 2, image=self.icons.get(self.icon, 12, fg))
            x += iw
        self.create_text(x, h / 2, text=self.text, anchor="w", font=self.f, fill=fg)


class LevelMeter(tk.Canvas):
    """Pegelanzeige: senkrecht als schmaler Streifen (grün → gelb → rot) oder waagerecht in einer Farbe."""

    def __init__(self, master, length=200, thickness=4, orient="vertical", color=None):
        w, h = (thickness, length) if orient == "vertical" else (length, thickness)
        super().__init__(master, width=w, height=h, bg=_bg_of(master), highlightthickness=0, bd=0)
        self.orient, self.color, self.level = orient, color, 0.0
        self.bind("<Configure>", lambda e: self._draw())
        self._draw()

    def set(self, level):
        level = max(0.0, min(1.0, float(level or 0)))
        if abs(level - self.level) > 0.004:
            self.level = level
            self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 2:
            w, h = int(self["width"]), int(self["height"])
        if self.orient == "vertical":
            rounded_rect(self, 0, 0, w, h, w / 2, fill=COL["raised"], outline="")
            top = h - self.level * h
            if self.level > 0:
                for y0, y1, col in ((h * 0.4, h, COL["ok"]), (h * 0.2, h * 0.4, COL["warn"]),
                                    (0, h * 0.2, COL["danger"])):
                    a, b = max(top, y0), y1
                    if b > a:
                        self.create_rectangle(0, a, w, b, fill=col, outline="")
        else:
            rounded_rect(self, 0, 0, w, h, h / 2, fill=COL["raised"], outline="")
            if self.level > 0:
                rounded_rect(self, 0, 0, max(h, self.level * w), h, h / 2, fill=self.color or COL["accent"],
                             outline="")
