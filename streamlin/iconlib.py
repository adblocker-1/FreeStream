"""Icon-Bibliothek für LinDeck: vordefinierte Tastenbilder, alle selbst gezeichnet (keine Elgato-Grafiken).

Jedes Symbol gibt es in drei Paketen:
  * Farbe   – weißes Symbol auf farbiger Fläche (Farbe je Thema)
  * Weiß    – weißes Symbol auf Schwarz
  * Schwarz – schwarzes Symbol auf Weiß

render(name, style) liefert das fertige Tastenbild (144 × 144), glyph(name, size, color) nur das Symbol.
"""
import math

from PIL import Image, ImageDraw

from . import theme
from .i18n import _, pgettext

KEY_SIZE = 144
STYLES = ("color", "white", "black")

# Thema -> (Titel, Farbe im Paket „Farbe“, Symbole)
TOPICS = [
    ("audio", "#3b82f6", ["mic", "mic_off", "headphones", "headphones_off", "speaker", "speaker_off", "volume_up",
                          "volume_down", "music", "wave", "sfx", "fx"]),
    ("media", "#ef4444", ["play", "pause", "stop", "prev", "next", "shuffle", "repeat", "record", "video",
                          "camera", "image", "film"]),
    ("stream", "#a855f7", ["stream", "stream_off", "live", "scene", "screen", "eye", "eye_off", "chat", "users",
                           "heart", "star", "bell"]),
    ("system", "#64748b", ["system", "gear", "power", "lock", "unlock", "refresh", "folder", "search", "keyboard",
                           "terminal", "download", "upload", "link", "copy", "save", "trash", "edit", "wifi",
                           "bluetooth", "cpu", "memory", "disk", "network", "battery"]),
    ("time", "#f59e0b", ["clock", "hourglass", "stopwatch", "calendar", "sun", "moon"]),
    ("home", "#10b981", ["home", "lightbulb", "plug", "thermometer", "fan", "door"]),
    ("nav", "#0ea5e9", ["up", "down", "left", "right", "layers", "deck", "plus", "minus",
                        "close", "check", "more", "undo", "redo", "import", "export"]),
    ("misc", "#ec4899", ["game", "globe", "mail", "phone", "coffee", "rocket", "trophy", "gift", "flag",
                         "pin", "smile", "thumbs_up", "spark", "puzzle", "text"]),
]


def topic_title(key):
    return {"audio": _("Audio"), "media": _("Media"), "stream": _("Streaming"), "system": _("System"),
            "time": _("Time"), "home": _("Smart home"), "nav": _("Navigation"), "misc": _("Miscellaneous")}[key]


def style_title(style):
    return {"color": _("LinDeck Icons (Color)"), "white": _("LinDeck Icons (White)"),
            "black": _("LinDeck Icons (Black)")}[style]


def label(name):
    """Anzeigename (auch für die Suche)."""
    return {
        "mic": _("Microphone"), "mic_off": _("Microphone off"), "headphones": _("Headphones"),
        "headphones_off": _("Headphones off"), "speaker": _("Speaker"), "speaker_off": _("Speaker off"),
        "volume_up": _("Volume up"), "volume_down": _("Volume down"), "music": _("Music"), "wave": _("Wave"),
        "sfx": _("Sound effects"), "fx": _("Effects"), "play": _("Play"), "pause": _("Pause"), "stop": _("Stop"),
        "prev": _("Previous"), "next": _("Next"), "shuffle": _("Shuffle"), "repeat": _("Repeat"),
        "record": _("Record"), "video": _("Video"), "camera": _("Camera"), "image": _("Image"), "film": _("Film"),
        "stream": _("Stream"), "stream_off": _("Stream off"), "live": _("Live"), "scene": _("Scene"),
        "screen": _("Screen"), "eye": _("Visible"), "eye_off": _("Hidden"), "chat": _("Chat"), "users": _("People"),
        "heart": _("Heart"), "star": _("Star"), "bell": _("Notification"), "system": _("Computer"),
        "gear": _("Settings"), "power": _("Power"), "lock": _("Lock"), "unlock": _("Unlock"),
        "refresh": _("Refresh"), "folder": _("Folder"), "search": _("Search"), "keyboard": _("Keyboard"),
        "terminal": _("Terminal"), "download": _("Download"), "upload": _("Upload"), "link": _("Link"),
        "copy": _("Copy"), "save": _("Save"), "trash": _("Delete"), "edit": _("Edit"), "wifi": _("Wi-Fi"),
        "bluetooth": _("Bluetooth"), "cpu": _("Processor"), "memory": _("Memory"), "disk": _("Disk"),
        "network": _("Network"), "battery": _("Battery"), "clock": _("Clock"), "hourglass": _("Hourglass"),
        "stopwatch": _("Stopwatch"), "calendar": _("Calendar"), "sun": _("Day"), "moon": _("Night"),
        "home": _("Home"), "lightbulb": pgettext("icon", "Light"), "plug": _("Socket"), "thermometer": _("Temperature"),
        "fan": _("Fan"), "door": _("Door"), "up": _("Up"), "down": _("Down"), "left": _("Left"),
        "right": _("Right"), "navigate": _("Open"), "grid": _("Grid"), "layers": _("Layers"), "deck": _("Deck"),
        "plus": _("Plus"), "minus": _("Minus"), "close": _("Close"), "check": _("Done"), "more": _("More"),
        "undo": _("Undo"), "redo": _("Redo"), "import": _("Import"), "export": _("Export"), "game": _("Game"),
        "browser": _("Browser"), "globe": _("Web"), "mail": _("Mail"), "phone": _("Phone"),
        "coffee": _("Coffee"), "rocket": _("Rocket"), "trophy": _("Trophy"), "gift": _("Gift"),
        "flag": _("Flag"), "pin": _("Location"), "smile": _("Smiley"), "thumbs_up": _("Thumbs up"),
        "spark": _("Magic"), "puzzle": _("Plugin"), "text": _("Text"),
    }.get(name, name)


def names():
    return [n for _k, _c, lst in TOPICS for n in lst]


def topic_of(name):
    return next((k for k, _c, lst in TOPICS if name in lst), "misc")


def topic_color(name):
    return next((c for _k, c, lst in TOPICS if name in lst), "#64748b")


def search(query, items=None):
    """Symbole, deren Name oder Anzeigename den Suchtext enthält."""
    q = (query or "").strip().lower()
    items = names() if items is None else items
    if not q:
        return list(items)
    return [n for n in items if q in n.lower() or q in label(n).lower() or q in topic_title(topic_of(n)).lower()]


# ============================================================ Zeichnen
_LIVE = ("clock", "hourglass", "stopwatch", "cpu", "memory", "thermometer", "network", "battery", "disk")


def glyph(name, size, color):
    """Nur das Symbol (RGBA, transparent)."""
    if name in _LIVE:
        from . import livekeys
        return livekeys.icon(name, size, color)
    if name in NEW:
        return _draw_new(name, size, color)
    return theme.glyph(name, size, color)


def render(name, style="color", size=KEY_SIZE):
    """Fertiges Tastenbild: Fläche + Symbol in der Mitte."""
    if style == "color":
        bg, fg = theme.rgb(topic_color(name)), (255, 255, 255)
    elif style == "black":
        bg, fg = (244, 244, 246), (17, 17, 20)
    else:
        bg, fg = (0, 0, 0), (255, 255, 255)
    im = Image.new("RGBA", (size, size), bg + (255,))
    if style == "color":  # leichter Verlauf nach unten
        shade = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shade)
        for y in range(size):
            sd.line((0, y, size, y), fill=(0, 0, 0, int(46 * y / size)))
        im.alpha_composite(shade)
    g = glyph(name, int(size * 0.5), "#%02x%02x%02x" % fg)
    im.alpha_composite(g, ((size - g.width) // 2, (size - g.height) // 2))
    return im


NEW = {"volume_up", "volume_down", "pause", "stop", "prev", "next", "shuffle", "repeat", "video", "film", "live",
       "scene", "screen", "eye", "eye_off", "users", "heart", "star", "bell", "power", "lock", "unlock", "refresh",
       "terminal", "download", "upload", "link", "copy", "save", "wifi", "bluetooth", "calendar", "lightbulb",
       "plug", "fan", "door", "left", "right", "minus", "mail", "phone", "coffee", "rocket", "trophy", "gift",
       "flag", "pin", "smile", "thumbs_up"}


def _draw_new(n, size, color):
    k = 4
    S = size * k
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    c = theme.rgb(color) + (255,) if isinstance(color, str) else color
    pad = S * 0.06
    w = S - 2 * pad
    lw = max(2, round(S * 0.075))

    def P(px, py):
        return (pad + px * w, pad + py * w)

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

    def poly(*pts):
        d.polygon([P(*p) for p in pts], fill=c)

    def cut(*pts):  # transparente Fläche ausschneiden
        d.polygon([P(*p) for p in pts], fill=(0, 0, 0, 0))

    speaker = [(0.04, 0.36), (0.24, 0.36), (0.5, 0.1), (0.5, 0.9), (0.24, 0.64), (0.04, 0.64)]
    if n == "volume_up":
        poly(*speaker)
        line((0.62, 0.5), (0.94, 0.5))
        line((0.78, 0.34), (0.78, 0.66))
    elif n == "volume_down":
        poly(*speaker)
        line((0.62, 0.5), (0.94, 0.5))
    elif n == "pause":
        rect(0.18, 0.1, 0.4, 0.9, r=0.06)
        rect(0.6, 0.1, 0.82, 0.9, r=0.06)
    elif n == "stop":
        rect(0.14, 0.14, 0.86, 0.86, r=0.1)
    elif n in ("prev", "next"):
        pts = [(0.14, 0.12), (0.7, 0.5), (0.14, 0.88)]
        bar = (0.74, 0.12, 0.88, 0.88)
        if n == "prev":
            pts = [(1 - x, y) for x, y in pts]
            bar = (0.12, 0.12, 0.26, 0.88)
        poly(*pts)
        rect(*bar, r=0.04)
    elif n == "shuffle":
        line((0.06, 0.26), (0.3, 0.26), (0.66, 0.74), (0.88, 0.74))
        line((0.06, 0.74), (0.3, 0.74), (0.66, 0.26), (0.88, 0.26))
        poly((0.82, 0.12), (0.98, 0.26), (0.82, 0.4))
        poly((0.82, 0.6), (0.98, 0.74), (0.82, 0.88))
    elif n == "repeat":
        line((0.12, 0.6), (0.12, 0.3), (0.82, 0.3))
        line((0.88, 0.4), (0.88, 0.7), (0.18, 0.7))
        poly((0.76, 0.14), (0.96, 0.3), (0.76, 0.46))
        poly((0.24, 0.54), (0.04, 0.7), (0.24, 0.86))
    elif n == "video":
        rect(0.02, 0.22, 0.68, 0.78, r=0.12)
        poly((0.72, 0.4), (0.98, 0.24), (0.98, 0.76), (0.72, 0.6))
    elif n == "film":
        rect(0.12, 0.04, 0.88, 0.96, r=0.08)
        for y in (0.12, 0.3, 0.48, 0.66, 0.84):
            for x in (0.18, 0.74):
                d.rectangle((*P(x, y), *P(x + 0.08, y + 0.08)), fill=(0, 0, 0, 0))
        d.rectangle((*P(0.32, 0.12), *P(0.68, 0.46)), fill=(0, 0, 0, 0))
        d.rectangle((*P(0.32, 0.54), *P(0.68, 0.88)), fill=(0, 0, 0, 0))
    elif n == "live":
        from . import render as R
        rect(0.0, 0.26, 1.0, 0.74, r=0.12, fill=False)
        ell(0.1, 0.43, 0.24, 0.57)
        d.text(P(0.62, 0.51), "LIVE", font=R.font(int(w * 0.24), "bold"), fill=c, anchor="mm")
    elif n == "scene":  # Filmklappe
        rect(0.06, 0.42, 0.94, 0.94, r=0.06)
        poly((0.04, 0.24), (0.86, 0.04), (0.92, 0.24), (0.1, 0.44))
        for x in (0.28, 0.52, 0.76):
            cut((x, 0.2 - (x - 0.28) * 0.25 + 0.02), (x + 0.1, 0.18 - (x - 0.28) * 0.25 + 0.02),
                (x - 0.02, 0.4 - (x - 0.28) * 0.25), (x - 0.12, 0.42 - (x - 0.28) * 0.25))
    elif n == "screen":
        rect(0.04, 0.1, 0.96, 0.7, r=0.06, fill=False)
        line((0.5, 0.72), (0.5, 0.86))
        line((0.28, 0.9), (0.72, 0.9))
    elif n in ("eye", "eye_off"):
        d.chord((*P(0.02, 0.18), *P(0.98, 0.82)), 0, 360, fill=None, outline=c, width=lw)
        ell(0.36, 0.36, 0.64, 0.64)
        if n == "eye_off":
            line((0.1, 0.9), (0.9, 0.1))
    elif n == "users":
        ell(0.12, 0.08, 0.46, 0.42)
        d.pieslice((*P(0.0, 0.48), *P(0.58, 1.1)), 180, 360, fill=c)
        ell(0.56, 0.14, 0.86, 0.44)
        d.pieslice((*P(0.5, 0.52), *P(1.0, 1.06)), 180, 360, fill=c)
    elif n == "heart":
        ell(0.04, 0.12, 0.52, 0.58)
        ell(0.48, 0.12, 0.96, 0.58)
        poly((0.07, 0.44), (0.93, 0.44), (0.5, 0.92))
    elif n == "star":
        pts = []
        for i in range(10):
            r = 0.48 if i % 2 == 0 else 0.2
            a = -math.pi / 2 + i * math.pi / 5
            pts.append((0.5 + r * math.cos(a), 0.53 + r * math.sin(a)))
        poly(*pts)
    elif n == "bell":
        d.chord((*P(0.18, 0.1), *P(0.82, 0.74)), 180, 360, fill=c)
        rect(0.18, 0.4, 0.82, 0.72, r=0.0)
        poly((0.18, 0.66), (0.82, 0.66), (0.94, 0.8), (0.06, 0.8))
        ell(0.4, 0.8, 0.6, 0.98)
        ell(0.44, 0.02, 0.56, 0.14)
    elif n == "power":
        arc(0.1, 0.12, 0.9, 0.92, -55, 235)
        line((0.5, 0.04), (0.5, 0.46))
    elif n in ("lock", "unlock"):
        rect(0.14, 0.44, 0.86, 0.96, r=0.08)
        if n == "lock":
            arc(0.26, 0.04, 0.74, 0.62, 180, 360)
            line((0.26, 0.33), (0.26, 0.46))
            line((0.74, 0.33), (0.74, 0.46))
        else:
            arc(0.46, 0.04, 0.94, 0.62, 180, 360)
            line((0.46, 0.33), (0.46, 0.46))
        ell(0.43, 0.6, 0.57, 0.74, fill=False)
    elif n == "refresh":
        arc(0.1, 0.1, 0.9, 0.9, 30, 320)
        poly((0.64, 0.02), (0.92, 0.2), (0.62, 0.34))
    elif n == "terminal":
        rect(0.02, 0.1, 0.98, 0.9, r=0.08, fill=False)
        line((0.2, 0.34), (0.38, 0.5), (0.2, 0.66))
        line((0.48, 0.68), (0.76, 0.68))
    elif n in ("download", "upload"):
        line((0.1, 0.7), (0.1, 0.92), (0.9, 0.92), (0.9, 0.7))
        if n == "download":
            line((0.5, 0.06), (0.5, 0.66))
            line((0.26, 0.44), (0.5, 0.68), (0.74, 0.44))
        else:
            line((0.5, 0.7), (0.5, 0.08))
            line((0.26, 0.3), (0.5, 0.06), (0.74, 0.3))
    elif n == "link":
        for (a, b) in ((0.04, 0.3), (0.42, 0.3)):
            rect(a, b, a + 0.54, b + 0.4, r=0.2, fill=False)
        d.rectangle((*P(0.42, 0.36), *P(0.58, 0.64)), fill=(0, 0, 0, 0))
        line((0.34, 0.5), (0.66, 0.5))
    elif n == "copy":
        rect(0.3, 0.04, 0.96, 0.7, r=0.08, fill=False)
        rect(0.04, 0.3, 0.7, 0.96, r=0.08)
    elif n == "save":
        poly((0.06, 0.06), (0.76, 0.06), (0.94, 0.24), (0.94, 0.94), (0.06, 0.94))
        d.rectangle((*P(0.22, 0.06), *P(0.66, 0.32)), fill=(0, 0, 0, 0))
        ell(0.34, 0.5, 0.66, 0.82, fill=False)
    elif n == "wifi":
        ell(0.42, 0.78, 0.58, 0.94)
        arc(0.26, 0.5, 0.74, 0.98, 225, 315)
        arc(0.12, 0.3, 0.88, 1.06, 225, 315)
        arc(-0.02, 0.1, 1.02, 1.14, 225, 315)
    elif n == "bluetooth":
        line((0.26, 0.28), (0.74, 0.7), (0.5, 0.92), (0.5, 0.08), (0.74, 0.3), (0.26, 0.72))
    elif n == "calendar":
        rect(0.06, 0.14, 0.94, 0.94, r=0.08, fill=False)
        d.rectangle((*P(0.06, 0.14), *P(0.94, 0.34)), fill=c)
        line((0.3, 0.04), (0.3, 0.2))
        line((0.7, 0.04), (0.7, 0.2))
        for y in (0.48, 0.7):
            for x in (0.22, 0.44, 0.66):
                rect(x, y, x + 0.12, y + 0.12, r=0.03)
    elif n == "lightbulb":
        ell(0.18, 0.02, 0.82, 0.66)
        poly((0.3, 0.52), (0.7, 0.52), (0.64, 0.76), (0.36, 0.76))
        line((0.38, 0.84), (0.62, 0.84))
        line((0.42, 0.95), (0.58, 0.95))
    elif n == "plug":
        rect(0.18, 0.3, 0.82, 0.7, r=0.12)
        line((0.36, 0.06), (0.36, 0.3))
        line((0.64, 0.06), (0.64, 0.3))
        line((0.5, 0.7), (0.5, 0.96))
    elif n == "fan":  # drei Flügel um die Nabe
        for ang in (-90, 30, 150):
            a = math.radians(ang)
            pts = []
            for i in range(24):
                t = 2 * math.pi * i / 24
                ex, ey = 0.25 + 0.22 * math.cos(t), 0.11 * math.sin(t)
                ex, ey = ex * math.cos(a) - ey * math.sin(a), ex * math.sin(a) + ey * math.cos(a)
                pts.append((0.5 + ex, 0.5 + ey))
            poly(*pts)
        ell(0.38, 0.38, 0.62, 0.62)
        d.ellipse((*P(0.44, 0.44), *P(0.56, 0.56)), fill=(0, 0, 0, 0))
    elif n == "door":
        rect(0.18, 0.04, 0.82, 0.96, r=0.04, fill=False)
        ell(0.62, 0.48, 0.72, 0.58)
    elif n in ("left", "right"):
        pts = [(0.86, 0.5), (0.14, 0.5)]
        head = [(0.44, 0.2), (0.14, 0.5), (0.44, 0.8)]
        if n == "right":
            pts = [(1 - x, y) for x, y in pts]
            head = [(1 - x, y) for x, y in head]
        line(*pts)
        line(*head)
    elif n == "minus":
        line((0.14, 0.5), (0.86, 0.5))
    elif n == "mail":
        rect(0.02, 0.16, 0.98, 0.84, r=0.08, fill=False)
        line((0.06, 0.22), (0.5, 0.56), (0.94, 0.22))
    elif n == "phone":
        rect(0.24, 0.02, 0.76, 0.98, r=0.12, fill=False)
        line((0.42, 0.86), (0.58, 0.86))
    elif n == "coffee":
        rect(0.08, 0.34, 0.7, 0.92, r=0.1)
        arc(0.56, 0.42, 0.94, 0.76, -90, 90)
        line((0.26, 0.06), (0.26, 0.22))
        line((0.42, 0.06), (0.42, 0.22))
        line((0.58, 0.06), (0.58, 0.22))
    elif n == "rocket":
        poly((0.5, 0.02), (0.66, 0.2), (0.66, 0.68), (0.34, 0.68), (0.34, 0.2))
        poly((0.34, 0.42), (0.14, 0.72), (0.14, 0.8), (0.34, 0.68))
        poly((0.66, 0.42), (0.86, 0.72), (0.86, 0.8), (0.66, 0.68))
        poly((0.4, 0.74), (0.6, 0.74), (0.5, 0.98))
        d.ellipse((*P(0.42, 0.26), *P(0.58, 0.42)), fill=(0, 0, 0, 0))
    elif n == "trophy":
        poly((0.22, 0.06), (0.78, 0.06), (0.72, 0.46), (0.5, 0.6), (0.28, 0.46))
        arc(0.02, 0.1, 0.32, 0.46, 90, 270)
        arc(0.68, 0.1, 0.98, 0.46, -90, 90)
        line((0.5, 0.6), (0.5, 0.8))
        rect(0.28, 0.8, 0.72, 0.94, r=0.03)
    elif n == "gift":
        rect(0.06, 0.32, 0.94, 0.52, r=0.04)
        rect(0.12, 0.52, 0.88, 0.96, r=0.04)
        d.rectangle((*P(0.45, 0.32), *P(0.55, 0.96)), fill=(0, 0, 0, 0))
        arc(0.18, 0.06, 0.5, 0.36, 180, 360)
        arc(0.5, 0.06, 0.82, 0.36, 180, 360)
    elif n == "flag":
        line((0.16, 0.04), (0.16, 0.96))
        poly((0.2, 0.06), (0.9, 0.06), (0.74, 0.3), (0.9, 0.54), (0.2, 0.54))
    elif n == "pin":
        ell(0.16, 0.02, 0.84, 0.7)
        poly((0.2, 0.5), (0.8, 0.5), (0.5, 0.98))
        d.ellipse((*P(0.37, 0.23), *P(0.63, 0.49)), fill=(0, 0, 0, 0))
    elif n == "smile":
        ell(0.04, 0.04, 0.96, 0.96, fill=False)
        ell(0.3, 0.3, 0.4, 0.42)
        ell(0.6, 0.3, 0.7, 0.42)
        arc(0.26, 0.3, 0.74, 0.76, 20, 160)
    elif n == "thumbs_up":
        rect(0.04, 0.44, 0.24, 0.94, r=0.04)
        poly((0.3, 0.44), (0.5, 0.06), (0.62, 0.1), (0.58, 0.38), (0.9, 0.38), (0.96, 0.5), (0.86, 0.94),
             (0.3, 0.94))
    return im.resize((size, size), Image.LANCZOS)
