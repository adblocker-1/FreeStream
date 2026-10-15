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
from .i18n import _

log = logging.getLogger("streamlin.render")

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
        img.alpha_composite(frame if frame.size == (w, h) else _fit(frame, w, h))
    show = state.get("ShowTitle")
    if show is None:
        show = True
    if show and title:
        draw_title(img, title, state, w / 72.0)
    if overlay == "alert":
        _alert(img)
    elif overlay == "ok":
        _ok(img)
    return img


def _alert(img):
    w, h = img.size
    d = ImageDraw.Draw(img)
    s = int(w * 0.45)
    cx, cy = w // 2, h // 2
    d.polygon([(cx, cy - s // 2), (cx - s // 2, cy + s // 2), (cx + s // 2, cy + s // 2)], fill=(250, 190, 0),
              outline=(0, 0, 0))
    f = font(int(s * 0.6), "bold")
    d.text((cx, cy + s // 8), "!", font=f, fill=(0, 0, 0), anchor="mm")


def _ok(img):
    w, h = img.size
    d = ImageDraw.Draw(img)
    r = int(w * 0.25)
    cx, cy = w // 2, h // 2
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(40, 180, 90))
    d.line([(cx - r // 2, cy), (cx - r // 8, cy + r // 2), (cx + r // 2, cy - r // 2)], fill=(255, 255, 255),
           width=max(2, r // 4))


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
