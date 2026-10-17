"""Bilder für Tasten, Touchstreifen (Stream Deck +) und Infobar (Neo) erzeugen.

Titel werden wie in der Elgato-App gerendert: FontSize/FontStyle/
TitleAlignment/TitleColor/OutlineThickness/ShowTitle aus dem Zustand.
Animierte GIFs werden als Bildfolge zurückgegeben.
"""
import io
import logging
import os
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageSequence

from . import paths
from .i18n import _, pgettext

log = logging.getLogger("freestream.render")

try:
    import cairosvg
except (ImportError, OSError):  # pragma: no cover
    cairosvg = None

FONT_CANDIDATES = {
    "regular": ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
                "/usr/share/fonts/TTF/DejaVuSans.ttf"],
    "bold": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
             "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
             "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"],
    "italic": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
               "/usr/share/fonts/truetype/noto/NotoSans-Italic.ttf"],
    "bolditalic": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf",
                   "/usr/share/fonts/truetype/noto/NotoSans-BoldItalic.ttf"],
}
EMOJI_FONTS = ["/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"]


def _font_file(style: str, family: str = ""):
    if family:
        try:
            import subprocess
            out = subprocess.run(["fc-match", "-f", "%{file}", f"{family}:{style or 'regular'}"],
                                 capture_output=True, text=True, timeout=2).stdout
            if out and os.path.exists(out):
                return out
        except (OSError, Exception):
            pass
    for f in FONT_CANDIDATES.get(style, FONT_CANDIDATES["regular"]):
        if os.path.exists(f):
            return f
    return None


@lru_cache(maxsize=128)
def font(size: int, style: str = "regular", family: str = ""):
    f = _font_file(style, family)
    try:
        return ImageFont.truetype(f, max(6, int(size))) if f else ImageFont.load_default()
    except OSError:
        return ImageFont.load_default()


def _style_of(state: dict) -> str:
    s = (state.get("FontStyle") or "").lower()
    bold, italic = "bold" in s, "italic" in s
    return "bolditalic" if bold and italic else "bold" if bold else "italic" if italic else "regular"


def parse_color(c, default=(255, 255, 255)):
    if not c:
        return default
    c = c.strip().lstrip("#")
    try:
        if len(c) == 3:
            return tuple(int(x * 2, 16) for x in c)
        if len(c) >= 6:
            return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        pass
    return default


# --------------------------------------------------------------------------
# Bilder laden
# --------------------------------------------------------------------------
@lru_cache(maxsize=512)
def _load_frames(path: str, w: int, h: int, mtime: float):
    p = Path(path)
    ext = p.suffix.lower()
    try:
        if ext == ".svg":
            if not cairosvg:
                return None
            png = cairosvg.svg2png(url=str(p), output_width=w, output_height=h)
            return [(Image.open(io.BytesIO(png)).convert("RGBA"), 0)]
        img = Image.open(p)
        if getattr(img, "is_animated", False):
            frames = []
            for fr in ImageSequence.Iterator(img):
                dur = fr.info.get("duration", img.info.get("duration", 100)) or 100
                frames.append((_fit(fr.convert("RGBA"), w, h), dur))
            return frames
        return [(_fit(img.convert("RGBA"), w, h), 0)]
    except (OSError, ValueError, Exception) as e:  # noqa: BLE001 - beschädigte Bilder ignorieren
        log.debug(_("Cannot load image {path}: {error}").format(path=path, error=e))
        return None


def _fit(img: Image.Image, w: int, h: int) -> Image.Image:
    if img.size == (w, h):
        return img
    return img.resize((w, h), Image.LANCZOS)


def load_frames(path, w, h):
    if not path:
        return None
    path = str(path)
    if not os.path.exists(path):
        # Elgato speichert oft ohne Endung (Images/btn_hotkey) -> svg/png suchen
        for ext in (".png", ".svg", "@2x.png", ".gif", ".jpg"):
            if os.path.exists(path + ext):
                path = path + ext
                break
        else:
            return None
    return _load_frames(path, w, h, os.path.getmtime(path))


def builtin_icon_path(rel_image: str, plugin_dir_name: str):
    """Pfad zu einem Standard-Icon aus den extrahierten Elgato-Assets."""
    if not rel_image:
        return None
    base = paths.ASSETS_DIR / "Plugins" / plugin_dir_name / rel_image
    for ext in ("", ".png", "@2x.png", ".svg"):
        p = Path(str(base) + ext)
        if p.exists():
            return p
    return None


# --------------------------------------------------------------------------
# Tasten
# --------------------------------------------------------------------------
def draw_title(img: Image.Image, title: str, state: dict, scale: float):
    if not title:
        return
    key = (title, img.size, round(scale, 3), state.get("FontSize"), state.get("FontStyle"), state.get("FontFamily"),
           str(state.get("TitleColor")), state.get("OutlineThickness"), state.get("TitleAlignment"),
           bool(state.get("FontUnderline")))
    layer = _title_layer(key, title, dict(state), scale, img.size)
    img.alpha_composite(layer)


_TITLE_CACHE = {}


def _title_layer(key, title, state, scale, size_px):
    """Titel als durchsichtige Ebene – zwischengespeichert, denn Schrift mit Umriss zu zeichnen ist der
    teuerste Teil einer Taste und dieselben Titel kommen bei jedem Seitenwechsel wieder."""
    layer = _TITLE_CACHE.get(key)
    if layer is not None:
        return layer
    layer = Image.new("RGBA", size_px, (0, 0, 0, 0))
    _draw_title(layer, title, state, scale)
    if len(_TITLE_CACHE) > 512:
        _TITLE_CACHE.clear()
    _TITLE_CACHE[key] = layer
    return layer


def _draw_title(img: Image.Image, title: str, state: dict, scale: float):
    size = int(round((state.get("FontSize") or 12) * 1.05 * scale))
    style, family = _style_of(state), state.get("FontFamily") or ""
    f = font(size, style, family)
    d0 = ImageDraw.Draw(img)
    widest = max((d0.textlength(l, font=f) for l in title.split("\n")), default=0)
    if widest > img.size[0] - 4 and widest > 0:
        size = max(7, int(size * (img.size[0] - 4) / widest))
        f = font(size, style, family)
    color = parse_color(state.get("TitleColor"))
    outline = state.get("OutlineThickness")
    outline = 2 if outline is None else outline
    stroke = max(0, int(round(outline * scale * 0.6)))
    d = ImageDraw.Draw(img)
    lines = title.split("\n")
    line_h = int(size * 1.15)
    total = line_h * len(lines)
    w, h = img.size
    align = (state.get("TitleAlignment") or "bottom").lower()
    pad = int(4 * scale)
    if align == "top":
        y = pad
    elif align in ("middle", "center"):
        y = (h - total) // 2
    else:
        y = h - total - pad
    for line in lines:
        if line:
            tw = d.textlength(line, font=f)
            d.text(((w - tw) / 2, y), line, font=f, fill=color, stroke_width=stroke, stroke_fill=(0, 0, 0))
            if state.get("FontUnderline"):
                d.line(((w - tw) / 2, y + size + 1, (w + tw) / 2, y + size + 1), fill=color, width=max(1, size // 12))
        y += line_h


def fallback_icon(w, h, label: str, color=(40, 40, 46)):
    img = Image.new("RGBA", (w, h), color + (255,))
    if label == "":
        return img
    d = ImageDraw.Draw(img)
    words = (label or "?").split()
    lines, cur = [], ""
    f = font(max(9, w // 7), "bold")
    for word in words:
        t = (cur + " " + word).strip()
        if d.textlength(t, font=f) > w - 8 and cur:
            lines.append(cur)
            cur = word
        else:
            cur = t
    lines.append(cur)
    lh = int(f.size * 1.15)
    y = (h - lh * len(lines)) // 2
    for line in lines[:4]:
        tw = d.textlength(line, font=f)
        d.text(((w - tw) / 2, y), line, font=f, fill=(200, 200, 210))
        y += lh
    return img


BG_GAP = 0.22  # Abstand zwischen den Tasten im Verhältnis zur Tastengröße (wie am Gerät)
_bg_cache = {}


def background_tiles(path, cols, rows, size, dim=0, gap_ratio=BG_GAP):
    """Hintergrundbild einer Seite in Kacheln je Taste zerlegen: {(Spalte, Zeile): RGBA-Bild}.

    Das Bild füllt die ganze Tastenfläche (zugeschnitten, nicht verzerrt). Die
    Lücken zwischen den Tasten werden mitgerechnet, damit es über die Tasten
    hinweg durchgehend wirkt. dim (0–90) dunkelt es ab, damit Titel lesbar bleiben.
    """
    import os
    from PIL import ImageOps
    try:
        key = (str(path), os.path.getmtime(path), cols, rows, tuple(size), int(dim))
    except OSError:
        return {}
    if key in _bg_cache:
        return _bg_cache[key]
    w, h = size
    gap = int(round(w * gap_ratio))
    W, H = cols * w + (cols - 1) * gap, rows * h + (rows - 1) * gap
    try:
        with Image.open(path) as src:
            img = ImageOps.fit(ImageOps.exif_transpose(src).convert("RGB"), (W, H), Image.LANCZOS)
    except (OSError, ValueError):
        return {}
    if dim:
        img = Image.blend(img, Image.new("RGB", img.size, (0, 0, 0)), max(0, min(90, dim)) / 100)
    tiles = {(c, r): img.crop((c * (w + gap), r * (h + gap), c * (w + gap) + w, r * (h + gap) + h)).convert("RGBA")
             for r in range(rows) for c in range(cols)}
    if len(_bg_cache) > 16:
        _bg_cache.clear()
    _bg_cache[key] = tiles
    return tiles


def compose_key(size, frame, title, state, overlay=None, background=(0, 0, 0), base=None):
    """Erzeugt ein RGBA-Bild einer Taste (base: Kachel des Seiten-Hintergrunds)."""
    w, h = size
    img = base.copy() if base is not None else Image.new("RGBA", (w, h), background + (255,))
    if frame is not None:
        own = frame.info.get("own_opacity")  # FreeWave-Tasten: nur ihr Hintergrund wird durchsichtig
        frame = frame if frame.size == (w, h) else _fit(frame, w, h)
        img.alpha_composite(frame if own else with_opacity(frame, state.get("IconOpacity", 100)))
    show = state.get("ShowTitle")
    if show is None:
        show = True
    if show and title and not overlay:  # während einer Anzeige steht der Text der Anzeige auf der Taste
        draw_title(img, title, state, w / 72.0)
    if overlay == "alert":
        _alert(img)
    elif overlay == "ok":
        _ok(img)
    elif isinstance(overlay, tuple) and overlay[0] == "progress":
        _progress(img, *overlay[1:])
    return img


def with_opacity(img, pct):
    """Bild mit pct % Deckkraft (Einstellung „Deckkraft“ einer Taste: das Hintergrundbild der Seite
    scheint durch). 100 % = unverändert."""
    try:
        pct = max(0, min(100, int(pct)))
    except (TypeError, ValueError):
        return img
    if pct >= 100:
        return img
    img = img.convert("RGBA") if img.mode != "RGBA" else img.copy()
    img.putalpha(img.getchannel("A").point(lambda a: a * pct // 100))
    return img


OK_COLOR, ALERT_COLOR, ACCENT = (34, 197, 94), (244, 63, 94), (91, 124, 255)


def _dim(img, alpha=175):
    img.alpha_composite(Image.new("RGBA", img.size, (8, 9, 13, alpha)))


def _mark(d, kind, box, color=(255, 255, 255)):
    """Kleines Zeichen in box: check, close, layers, deck."""
    x0, y0, x1, y1 = box
    s = x1 - x0
    lw = max(2, round(s * 0.11))
    if kind == "check":
        d.line([(x0 + s * 0.12, y0 + s * 0.52), (x0 + s * 0.4, y0 + s * 0.8), (x0 + s * 0.88, y0 + s * 0.22)],
               fill=color, width=lw, joint="curve")
    elif kind == "close":
        d.line([(x0 + s * 0.2, y0 + s * 0.2), (x1 - s * 0.2, y1 - s * 0.2)], fill=color, width=lw)
        d.line([(x1 - s * 0.2, y0 + s * 0.2), (x0 + s * 0.2, y1 - s * 0.2)], fill=color, width=lw)
    elif kind == "layers":
        for a in (0.0, 0.18, 0.36):
            cy = y0 + s * (0.3 + a)
            d.polygon([(x0 + s * 0.5, cy - s * 0.2), (x1 - s * 0.05, cy), (x0 + s * 0.5, cy + s * 0.2),
                       (x0 + s * 0.05, cy)], outline=color, fill=None, width=max(1, lw // 2 + 1))
    elif kind == "deck":
        q, g = s * 0.36, s * 0.12
        for i in range(2):
            for j in range(2):
                xx, yy = x0 + s * 0.08 + i * (q + g), y0 + s * 0.08 + j * (q + g)
                d.rounded_rectangle((xx, yy, xx + q, yy + q), radius=q * 0.25, fill=color)


def _badge(img, color, kind, text, sub=""):
    """Anzeige auf der Taste: farbige Kachel mit Zeichen, darunter ein kurzer Text."""
    w, h = img.size
    m = min(w, h)
    d = ImageDraw.Draw(img)
    s = m * 0.36
    x0, y0 = (w - s) / 2, h * 0.2 if not sub else h * 0.12
    d.rounded_rectangle((x0, y0, x0 + s, y0 + s), radius=s * 0.28, fill=color)
    p = s * 0.22
    _mark(d, kind, (x0 + p, y0 + p, x0 + s - p, y0 + s - p))
    if text:
        f = _fit_font(text, w * 0.92, int(m * 0.15), "bold")
        d.text((w / 2, h * (0.72 if sub else 0.76)), text, font=f, fill=(255, 255, 255), anchor="mm")
    if sub:
        f = _fit_font(sub, w * 0.92, int(m * 0.11), "regular")
        d.text((w / 2, h * 0.87), sub, font=f, fill=(150, 156, 170), anchor="mm")


def _fit_font(text, width, size, style):
    f = font(size, style)
    while size > 7 and f.getlength(text) > width:
        size -= 1
        f = font(size, style)
    return f


def _progress(img, frac, step=None, steps=None):
    """Fortschritt einer Multi-Aktion: „Schritt n / m“ und unten ein Balken."""
    w, h = img.size
    _dim(img)
    text = _("Step {n} / {m}").format(n=step, m=steps) if step and steps else f"{int(round(frac * 100))} %"
    _badge(img, ACCENT, "layers", text)
    pad, bh = w * 0.14, max(3, h * 0.055)
    y = h * 0.88
    track = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(track).rounded_rectangle((pad, y, w - pad, y + bh), radius=bh / 2, fill=(255, 255, 255, 45))
    img.alpha_composite(track)
    frac = max(0.0, min(1.0, float(frac)))
    if frac > 0:
        ImageDraw.Draw(img).rounded_rectangle((pad, y, pad + (w - 2 * pad) * frac, y + bh), radius=bh / 2,
                                              fill=ACCENT)


def _alert(img):
    _dim(img)
    _badge(img, ALERT_COLOR, "close", pgettext("overlay", "Error"))


def _ok(img):
    _dim(img)
    _badge(img, OK_COLOR, "check", pgettext("overlay", "Done"))


def no_profile_key(size):
    """Taste auf einem Gerät ohne Profil: „Kein Profil – in FreeDeck wählen“."""
    img = Image.new("RGBA", size, (14, 16, 21, 255))
    _badge(img, ACCENT, "deck", pgettext("no profile", "No profile"), _("choose in FreeDeck"))
    return img


def text_key(size, text, sub="", bg=(0, 0, 0), fg=(255, 255, 255)):
    w, h = size
    img = Image.new("RGBA", (w, h), bg + (255,))
    d = ImageDraw.Draw(img)
    f = font(int(h * 0.28), "bold")
    d.text((w / 2, h * (0.42 if sub else 0.5)), text, font=f, fill=fg, anchor="mm")
    if sub:
        d.text((w / 2, h * 0.78), sub, font=font(int(h * 0.14)), fill=(170, 170, 180), anchor="mm")
    return img


# --------------------------------------------------------------------------
# Touchstreifen (Stream Deck + / + XL / Studio)
# --------------------------------------------------------------------------
def compose_dial_segment(size, icon, title, value_text, indicator, background=None, active=False):
    """Ein Segment des Touchstreifens im Stil des Elgato-Layouts $B1/$A1."""
    w, h = size
    # durchsichtig, damit der Hintergrund des ganzen Touchstreifens zu sehen ist
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if background is not None:
        img.alpha_composite(_fit(background, w, h))
    d = ImageDraw.Draw(img)
    if title:
        d.text((w / 2, 14), title, font=font(16, "bold"), fill=(255, 255, 255), anchor="mm",
               stroke_width=1, stroke_fill=(0, 0, 0))
    ic = int(h * 0.48)
    if icon is not None:
        img.alpha_composite(_fit(icon, ic, ic), (12, 26))
    if value_text:
        d.text((w - 14, 50), str(value_text), font=font(20, "bold"), fill=(255, 255, 255), anchor="rm",
               stroke_width=1, stroke_fill=(0, 0, 0))
    if indicator is not None:
        x0, x1, y = 14, w - 14, h - 14
        d.rounded_rectangle((x0, y - 4, x1, y + 4), radius=4, fill=(60, 60, 66))
        frac = max(0.0, min(1.0, indicator / 100.0))
        if frac > 0:
            d.rounded_rectangle((x0, y - 4, x0 + (x1 - x0) * frac, y + 4), radius=4, fill=(10, 132, 255))
    if active:
        d.rectangle((0, 0, w - 1, h - 1), outline=(10, 132, 255), width=2)
    return img


def compose_strip(size, segments, background=None):
    w, h = size
    img = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    if background is not None:
        img.alpha_composite(_fit(background, w, h))
    x = 0
    for seg in segments:
        if seg is not None:
            img.alpha_composite(seg, (x, 0))
        x += seg.size[0] if seg is not None else w // max(1, len(segments))
    return img


# --------------------------------------------------------------------------
# Neo-Infobar
# --------------------------------------------------------------------------
def compose_infobar(size, big, small="", dots=None):
    w, h = size
    img = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    d = ImageDraw.Draw(img)
    d.text((w / 2, h * 0.42), big, font=font(int(h * 0.5), "bold"), fill=(255, 255, 255), anchor="mm")
    if dots:
        n, cur = dots
        r, gap = 3, 12
        x0 = w / 2 - (n - 1) * gap / 2
        for i in range(n):
            col = (255, 255, 255) if i == cur else (90, 90, 90)
            d.ellipse((x0 + i * gap - r, h - 10 - r, x0 + i * gap + r, h - 10 + r), fill=col)
    elif small:
        d.text((w / 2, h * 0.85), small, font=font(int(h * 0.2)), fill=(170, 170, 180), anchor="mm")
    return img
