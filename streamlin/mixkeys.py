"""Tastenbilder für das Linewave-Profil – im Stil des Wave-Link-Profils.

  * Kachel: farbiges Kanal-Symbol, darunter Mix und Lautstärke („Alle 80%“)
  * Fader: ein senkrechter Regler über zwei Tasten (obere und untere Hälfte)
  * Mix-Wahl: Symbol und Name des Mixes, der gewählte ist grün umrandet
  * Seiten: „Kanäle 1–7“

Alles wird mit PIL gezeichnet (keine Elgato-Grafiken).
"""
from PIL import Image, ImageDraw

from . import render, theme
from .i18n import _, pgettext

BG = (16, 16, 20)
TRACK = (52, 52, 60)
GREEN = (46, 204, 113)
RED = (229, 72, 77)
GREY = (150, 150, 160)
SS = 3  # Überabtastung für glatte Kanten
MIX_ICON = {"both": "wave", "monitor": "headphones", "stream": "stream"}


def mix_name(mix):
    return {"both": pgettext("mix", "All"), "monitor": "Monitor", "stream": "Stream"}.get(mix, mix)


def _rgb(color):
    if isinstance(color, tuple):
        return color[:3]
    try:
        return theme.rgb(color)
    except (ValueError, AttributeError):
        return (100, 116, 139)


def _fitting(d, text, f, width, size):
    """Schrift für text, verkleinert, bis er in width passt (lange Mix-Namen)."""
    s = size
    while d.textlength(text, font=f) > width and s > size * 0.55:
        s *= 0.9
        f = render.font(int(s), "bold")
    return f


def _canvas(w, h):
    im = Image.new("RGBA", (w * SS, h * SS), BG + (255,))
    return im, ImageDraw.Draw(im)


def _done(im, w, h):
    return im.resize((w, h), Image.LANCZOS)


def _paste_glyph(im, name, size, color, center):
    g = theme.glyph(name, int(size), color)
    im.alpha_composite(g, (int(center[0] - g.width / 2), int(center[1] - g.height / 2)))


def _dim(c, t=0.3):
    """Farbe c, nur zu Anteil t über dem Hintergrund (unbeleuchtete Segmente)."""
    return tuple(round(b + (x - b) * t) for b, x in zip(BG, c))


def tile(size, icon, color, vol, muted, mix, label=None):
    """Kanal-Kachel: abgerundetes Quadrat mit Symbol, darunter „Alle 80%“ bzw. „stumm“."""
    w, h = size
    im, d = _canvas(w, h)
    W, H = w * SS, h * SS
    side = H * 0.5
    x0, y0 = (W - side) / 2, H * 0.1
    fill = (70, 70, 78) if muted else _rgb(color)
    d.rounded_rectangle((x0, y0, x0 + side, y0 + side), radius=side * 0.24, fill=fill)
    _paste_glyph(im, icon + ("_off" if muted and icon in ("mic", "headphones", "speaker", "stream") else ""),
                 side * 0.56, (255, 255, 255) if not muted else (200, 200, 205), (W / 2, y0 + side / 2))
    if muted and icon not in ("mic", "headphones", "speaker", "stream"):
        d.line((x0 + side * 0.12, y0 + side * 0.88, x0 + side * 0.88, y0 + side * 0.12), fill=RED, width=int(SS * 3))
    f = render.font(int(H * 0.15), "bold")
    if vol is None:
        d.text((W / 2, H * 0.8), _("Mixer off"), font=f, fill=GREY, anchor="mm")
    elif muted:
        d.text((W / 2, H * 0.8), pgettext("state", "muted"), font=f, fill=RED, anchor="mm")
    else:
        name, value = (label or mix_name(mix)) + " ", f"{vol}%"
        f = _fitting(d, name + value, f, W * 0.94, H * 0.15)
        lw, vw = d.textlength(name, font=f), d.textlength(value, font=f)
        x = (W - lw - vw) / 2
        d.text((x, H * 0.8), name, font=f, fill=GREY, anchor="lm")
        d.text((x + lw, H * 0.8), value, font=f, fill=(255, 255, 255), anchor="lm")
    return _done(im, w, h)


def fader(size, half, icon, color, vol, muted, mix, label=None):
    """Hälfte („top“ oder „bottom“) eines Faders, der über zwei übereinanderliegende Tasten geht."""
    w, h = size
    W, H = w * SS, h * SS
    im = Image.new("RGBA", (W, 2 * H), BG + (255,))
    d = ImageDraw.Draw(im)
    col = _rgb(color)
    top, bottom = H * 0.14, 2 * H - H * 0.4
    tx, tw = W * 0.38, W * 0.13
    level = 0 if vol is None else max(0, min(100, vol))
    y = bottom - (bottom - top) * level / 100
    d.rounded_rectangle((tx - tw / 2, top, tx + tw / 2, bottom), radius=tw / 2, fill=TRACK)
    if level:
        d.rounded_rectangle((tx - tw / 2, y, tx + tw / 2, bottom), radius=tw / 2,
                            fill=(95, 95, 104) if muted else col)
    kw, kh = W * 0.4, H * 0.13
    d.rounded_rectangle((tx - kw / 2, y - kh / 2, tx + kw / 2, y + kh / 2), radius=kh / 2,
                        fill=RED if muted else (245, 245, 248))
    # Pegel-Leiste rechts: Segmente bis zur Lautstärke leuchten
    mx, mw, step = W * 0.66, W * 0.1, H * 0.085
    n = int((bottom - top) / step)
    for i in range(n):
        sy = bottom - (i + 1) * step
        lit = not muted and vol is not None and (i + 0.5) / n * 100 <= level
        d.rounded_rectangle((mx, sy + step * 0.22, mx + mw, sy + step), radius=step * 0.2,
                            fill=col if lit else _dim(col) if not muted else TRACK)
    # unten: kleines Kanal-Symbol und Mix
    ly = 2 * H - H * 0.18
    f = render.font(int(H * 0.15), "bold")
    text = pgettext("state", "muted") if muted else (label or mix_name(mix))
    f = _fitting(d, text, f, W * 0.7, H * 0.15)
    tw_ = d.textlength(text, font=f)
    gs = H * 0.17
    x = (W - gs - W * 0.05 - tw_) / 2
    _paste_glyph(im, icon, gs, RED if muted else col, (x + gs / 2, ly))
    d.text((x + gs + W * 0.05, ly), text, font=f, fill=RED if muted else GREY, anchor="lm")
    part = im.crop((0, 0, W, H)) if half == "top" else im.crop((0, H, W, 2 * H))
    return _done(part, w, h)


def mix_key(size, mix, selected, sub="", name=None, icon=None):
    """Mix-Wahl: Symbol und Name; der gewählte Mix ist grün umrandet."""
    w, h = size
    im, d = _canvas(w, h)
    W, H = w * SS, h * SS
    if selected:
        b = W * 0.05
        d.rounded_rectangle((b / 2, b / 2, W - b / 2, H - b / 2), radius=W * 0.16, outline=GREEN, width=int(b))
    _paste_glyph(im, icon or MIX_ICON.get(mix, "wave"), H * 0.34, (255, 255, 255) if selected else GREY,
                 (W / 2, H * 0.38))
    name = name or mix_name(mix)
    f = _fitting(d, name, render.font(int(H * 0.16), "bold"), W * 0.88, H * 0.16)
    d.text((W / 2, H * (0.7 if sub else 0.74)), name, font=f,
           fill=GREEN if selected else (220, 220, 226), anchor="mm")
    if sub:
        d.text((W / 2, H * 0.85), sub, font=render.font(int(H * 0.11)), fill=GREY, anchor="mm")
    return _done(im, w, h)


def pages_key(size, first, last):
    """„Kanäle 1–7“ – zeigt, welche Kanäle diese Seite steuert; Drücken blättert weiter."""
    w, h = size
    im, d = _canvas(w, h)
    W, H = w * SS, h * SS
    d.text((W / 2, H * 0.4), _("Channels"), font=render.font(int(H * 0.17), "bold"), fill=GREEN, anchor="mm")
    span = f"{first}–{last}" if last != first else str(first)
    d.text((W / 2, H * 0.64), span, font=render.font(int(H * 0.2), "bold"), fill=GREEN, anchor="mm")
    return _done(im, w, h)
