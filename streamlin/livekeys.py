"""Live-Tasten: Uhr, Countdown, Stoppuhr und Systemmonitor.

Die Tasten zeichnen ihr Bild selbst (große Ziffern, Fortschrittsbalken,
kleiner Verlaufsgraph) und werden vom Engine-Ticker jede Sekunde
aktualisiert. Countdown und Stoppuhr behalten ihren Zustand pro Aktion
(ActionID), auch wenn die Seite gewechselt wird; der Countdown-Alarm läuft
über einen eigenen Timer-Thread und klingelt deshalb auch auf anderen Seiten.

Langer Druck (long_press_ms) setzt Countdown und Stoppuhr zurück. Am
Stream Deck + : Drehen ändert die Countdown-Dauer, Drücken/Tippen
startet/pausiert, langes Drücken setzt zurück.
"""
import logging
import math
import os
import threading
import time
from collections import deque
from datetime import datetime

from PIL import Image, ImageDraw

from . import actions as A
from . import render, sysinfo, system
from .i18n import _, pgettext

log = logging.getLogger("streamlin.livekeys")

# austauschbar für Tests
clock = time.monotonic
wallclock = datetime.now
INFO = sysinfo.SysInfo()

BG = (14, 15, 19)
FG = (255, 255, 255)
MUTED = (150, 152, 166)
TRACK = (52, 54, 64)
ALARM_BG = (210, 36, 36)
ACCENTS = {"blue": (10, 132, 255), "green": (48, 209, 88), "orange": (255, 159, 10), "red": (255, 69, 58),
           "purple": (191, 90, 242), "teal": (64, 200, 224), "white": (235, 235, 240)}
WARN, CRIT = (255, 159, 10), (255, 69, 58)
DEFAULT_ALARM = "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga"


# ==========================================================================
# Zeichenhelfer
# ==========================================================================
def _canvas(size, bg=BG):
    img = Image.new("RGBA", tuple(size), tuple(bg) + (255,))
    return img, ImageDraw.Draw(img)


def _font_fit(d, text, max_w, max_h, style="bold"):
    s = max(6, int(max_h))
    f = render.font(s, style)
    tw = d.textlength(text, font=f)
    if tw > max_w > 0:
        f = render.font(max(6, int(s * max_w / tw)), style)
    return f


def _text(d, xy, text, max_w, max_h, fill=FG, style="bold", anchor="mm"):
    if text:
        d.text(xy, text, font=_font_fit(d, text, max_w, max_h, style), fill=fill, anchor=anchor)


def _bar(d, w, h, frac, color, y=0.87):
    x0, x1 = w * 0.14, w * 0.86
    t = max(2, h * 0.055)
    yc = h * y
    d.rounded_rectangle((x0, yc - t / 2, x1, yc + t / 2), radius=t / 2, fill=TRACK)
    frac = max(0.0, min(1.0, frac or 0.0))
    if frac > 0:
        d.rounded_rectangle((x0, yc - t / 2, max(x0 + t, x0 + (x1 - x0) * frac), yc + t / 2), radius=t / 2,
                            fill=color)


def _mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _spark(img, box, values, vmax, color):
    """Kleiner Verlaufsgraph (Fläche + Linie) in box=(x0, y0, x1, y1)."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return
    x0, y0, x1, y1 = box
    top = max(vmax or 0, max(vals), 1e-9)
    n = len(vals)
    step = (x1 - x0) / max(1, n - 1)
    pts = [(x0 + i * step, y1 - (y1 - y0) * max(0.0, min(1.0, v / top))) for i, v in enumerate(vals)]
    d = ImageDraw.Draw(img)
    d.polygon([(pts[0][0], y1)] + pts + [(pts[-1][0], y1)], fill=_mix(BG, color, 0.32))
    d.line(pts, fill=color, width=max(1, int((y1 - y0) / 12)), joint="curve")


def _level(frac, accent, warn=0.75, crit=0.9, reverse=False):
    """Farbe nach Füllstand (reverse: wenig = kritisch, z. B. Akku)."""
    if frac is None:
        return MUTED
    if reverse:
        return CRIT if frac <= crit else WARN if frac <= warn else accent
    return CRIT if frac >= crit else WARN if frac >= warn else accent


def _rgb(color):
    return render.parse_color(color) if isinstance(color, str) else tuple(color[:3])


# ==========================================================================
# Symbole (Touchstreifen und Aktionsbibliothek im Editor)
# ==========================================================================
def icon(name, size=48, color=FG):
    """Selbst gezeichnetes Symbol als RGBA-Bild (4-fach überabgetastet)."""
    c = _rgb(color) + (255,)
    k = 4
    S = size * k
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    lw = max(2, round(S * 0.075))

    def P(x, y):
        return (x * S, y * S)

    def box(a, b, cc, dd):
        return (*P(a, b), *P(cc, dd))

    if name == "clock":
        d.ellipse(box(0.06, 0.06, 0.94, 0.94), outline=c, width=lw)
        d.line([P(0.5, 0.5), P(0.5, 0.24)], fill=c, width=lw)
        d.line([P(0.5, 0.5), P(0.7, 0.6)], fill=c, width=lw)
    elif name == "hourglass":
        d.line([P(0.2, 0.06), P(0.8, 0.06)], fill=c, width=lw)
        d.line([P(0.2, 0.94), P(0.8, 0.94)], fill=c, width=lw)
        d.line([P(0.26, 0.08), P(0.26, 0.2), P(0.74, 0.8), P(0.74, 0.92)], fill=c, width=lw, joint="curve")
        d.line([P(0.74, 0.08), P(0.74, 0.2), P(0.26, 0.8), P(0.26, 0.92)], fill=c, width=lw, joint="curve")
        d.polygon([P(0.32, 0.92), P(0.5, 0.7), P(0.68, 0.92)], fill=c)
    elif name == "stopwatch":
        d.ellipse(box(0.1, 0.18, 0.9, 0.98), outline=c, width=lw)
        d.line([P(0.38, 0.05), P(0.62, 0.05)], fill=c, width=lw)
        d.line([P(0.5, 0.05), P(0.5, 0.18)], fill=c, width=lw)
        d.line([P(0.5, 0.58), P(0.66, 0.4)], fill=c, width=lw)
        d.line([P(0.82, 0.2), P(0.9, 0.28)], fill=c, width=lw)
    elif name == "cpu":
        d.rounded_rectangle(box(0.2, 0.2, 0.8, 0.8), radius=S * 0.08, outline=c, width=lw)
        d.rectangle(box(0.38, 0.38, 0.62, 0.62), fill=c)
        for t in (0.34, 0.5, 0.66):
            d.line([P(t, 0.02), P(t, 0.18)], fill=c, width=lw)
            d.line([P(t, 0.82), P(t, 0.98)], fill=c, width=lw)
            d.line([P(0.02, t), P(0.18, t)], fill=c, width=lw)
            d.line([P(0.82, t), P(0.98, t)], fill=c, width=lw)
    elif name == "memory":
        d.rounded_rectangle(box(0.04, 0.26, 0.96, 0.66), radius=S * 0.05, outline=c, width=lw)
        for i in range(4):
            x = 0.16 + i * 0.19
            d.rectangle(box(x, 0.36, x + 0.11, 0.56), fill=c)
        for i in range(6):
            x = 0.12 + i * 0.152
            d.line([P(x, 0.68), P(x, 0.82)], fill=c, width=lw)
    elif name == "thermometer":
        d.ellipse(box(0.3, 0.6, 0.7, 0.98), fill=c)
        d.rounded_rectangle(box(0.38, 0.04, 0.62, 0.72), radius=S * 0.12, outline=c, width=lw)
        d.line([P(0.5, 0.36), P(0.5, 0.75)], fill=c, width=lw)
        for y in (0.18, 0.3, 0.42):
            d.line([P(0.7, y), P(0.86, y)], fill=c, width=max(1, lw // 2))
    elif name == "network":
        d.line([P(0.32, 0.08), P(0.32, 0.9)], fill=c, width=lw)
        d.line([P(0.12, 0.7), P(0.32, 0.92), P(0.52, 0.7)], fill=c, width=lw, joint="curve")
        d.line([P(0.68, 0.92), P(0.68, 0.1)], fill=c, width=lw)
        d.line([P(0.48, 0.3), P(0.68, 0.08), P(0.88, 0.3)], fill=c, width=lw, joint="curve")
    elif name in ("battery", "battery_charging"):
        d.rounded_rectangle(box(0.04, 0.26, 0.86, 0.74), radius=S * 0.08, outline=c, width=lw)
        d.rectangle(box(0.88, 0.4, 0.97, 0.6), fill=c)
        if name == "battery_charging":
            d.polygon([P(0.5, 0.3), P(0.3, 0.54), P(0.46, 0.54), P(0.4, 0.72), P(0.62, 0.46), P(0.46, 0.46)],
                      fill=c)
        else:
            d.rectangle(box(0.14, 0.36, 0.56, 0.64), fill=c)
    elif name == "disk":
        d.rounded_rectangle(box(0.06, 0.2, 0.94, 0.8), radius=S * 0.1, outline=c, width=lw)
        d.line([P(0.08, 0.56), P(0.92, 0.56)], fill=c, width=lw)
        d.ellipse(box(0.7, 0.62, 0.82, 0.74), fill=c)
    elif name == "gauge":
        d.arc(box(0.04, 0.14, 0.96, 1.06), 180, 360, fill=c, width=lw)
        d.line([P(0.5, 0.6), P(0.74, 0.32)], fill=c, width=lw)
        d.ellipse(box(0.42, 0.52, 0.58, 0.68), fill=c)
    else:
        d.ellipse(box(0.3, 0.3, 0.7, 0.7), fill=c)
    return im.resize((size, size), Image.LANCZOS)


GLYPHS = {n: (lambda size, color, n=n: icon(n, size, color))
          for n in ("clock", "hourglass", "stopwatch", "cpu", "memory", "thermometer", "network", "battery",
                    "disk", "gauge")}


# ==========================================================================
# Gemeinsame Basis
# ==========================================================================
class LiveAction(A.Action):
    """Selbst gezeichnete Taste, die sich jede Sekunde aktualisiert."""
    needs_tick = True
    icon_name = "clock"
    default_color = "blue"

    @property
    def accent(self):
        return ACCENTS.get(str(self.settings.get("color") or self.default_color), ACCENTS[self.default_color])

    @property
    def preview(self):
        return bool(getattr(getattr(self.ctx, "ctrl", None), "preview", False))

    def _custom_image(self):
        return self.image_override is not None or bool(self.state_dict().get("Image"))

    def frames(self, size):
        if self._custom_image():
            return super().frames(size)
        return [(self.draw(tuple(size)), 0)]

    def draw(self, size):
        return _canvas(size)[0]

    def snapshot(self):
        return None

    def tick(self):
        snap = self.snapshot()
        changed = snap != getattr(self, "_snap", None)
        self._snap = snap
        return changed

    @property
    def encoder_icon_image(self):
        enc = self.data.get("Encoder") or {}
        if enc.get("Icon") or enc.get("IconVisible") is False:
            return None  # eigenes Symbol des Profils (encoder_icon_path) bzw. keins
        return icon(self.icon_name, 48, self.accent)

    def _long(self, held_ms):
        cfg = self.ctx.cfg if self.ctx else None
        return held_ms >= (cfg.get("long_press_ms", 500) if cfg is not None else 500)

    # Dials: Drücken wie eine Taste (Auswertung beim Loslassen), Tippen = kurz, langes Tippen = lang
    def dial_down(self):
        pass

    def dial_up(self, held_ms=0):
        self.key_up(held_ms)

    def touch(self, long=False):
        self.key_up(10 ** 6 if long else 0)


def fmt_duration(sec):
    """Sekunden -> "4:05" bzw. "1:02:03"."""
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_duration(v, default=300.0):
    """"5:00", "1:30:00", "90", "90s", "5m", "1h30m" -> Sekunden."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v) if v > 0 else default
    s = str(v or "").strip().lower().replace(" ", "")
    if not s:
        return default
    try:
        if ":" in s:
            sec = 0
            for p in s.split(":"):
                sec = sec * 60 + int(p or 0)
            return float(sec) if sec > 0 else default
        if s[-1] in "hms" or "h" in s or "m" in s:
            total, num = 0.0, ""
            for ch in s:
                if ch.isdigit() or ch == ".":
                    num += ch
                elif ch in "hms" and num:
                    total += float(num) * {"h": 3600, "m": 60, "s": 1}[ch]
                    num = ""
                else:
                    raise ValueError(s)
            if num:
                total += float(num)
            return total if total > 0 else default
        val = float(s)
        return val if val > 0 else default
    except ValueError:
        return default


# ==========================================================================
# Uhr
# ==========================================================================
WEEKDAYS = (pgettext("weekday", "Mon"), pgettext("weekday", "Tue"), pgettext("weekday", "Wed"),
            pgettext("weekday", "Thu"), pgettext("weekday", "Fri"), pgettext("weekday", "Sat"),
            pgettext("weekday", "Sun"))


class Clock(LiveAction):
    icon_name = "clock"

    def _tz(self):
        name = str(self.settings.get("timezone") or "").strip()
        if not name:
            return None, ""
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name), name.rsplit("/", 1)[-1].replace("_", " ")
        except Exception:  # noqa: BLE001 - unbekannte Zone: Ortszeit
            return None, "?"

    def _now(self):
        tz, city = self._tz()
        return (wallclock(tz) if tz else wallclock()), city

    def _parts(self):
        now, city = self._now()
        h24 = self.settings.get("hour24", True) is not False
        if h24:
            hm, ampm = now.strftime("%H:%M"), ""
        else:
            hm, ampm = f"{now.hour % 12 or 12}:{now.minute:02d}", ("AM" if now.hour < 12 else "PM")
        sec = f"{now.second:02d}" if self.settings.get("seconds") else ""
        fmt = self.settings.get("date_format") or "weekday"
        wd = WEEKDAYS[now.weekday()]
        date = {"dmy": f"{now.day:02d}.{now.month:02d}.{now.year}", "mdy": f"{now.month:02d}/{now.day:02d}/{now.year}",
                "iso": f"{now.year}-{now.month:02d}-{now.day:02d}"}.get(fmt, f"{wd} {now.day:02d}.{now.month:02d}.")
        return {"hm": hm, "ampm": ampm, "sec": sec, "date": date, "city": city, "now": now, "weekday": wd}

    def time_text(self):
        p = self._parts()
        return p["hm"] + (f":{p['sec']}" if p["sec"] else "") + (f" {p['ampm']}" if p["ampm"] else "")

    def snapshot(self):
        p = self._parts()
        return p["hm"], p["sec"], p["date"], p["ampm"]

    def draw(self, size):
        w, h = size
        img, d = _canvas(size)
        p = self._parts()
        show = self.settings.get("show") or "both"
        acc = self.accent
        if p["city"]:
            _text(d, (w / 2, h * 0.13), p["city"], w * 0.88, h * 0.13, fill=acc)
        if show == "date":
            now = p["now"]
            if not p["city"]:
                _text(d, (w / 2, h * 0.15), p["weekday"], w * 0.86, h * 0.16, fill=acc)
            _text(d, (w / 2, h * 0.5), str(now.day), w * 0.8, h * 0.46)
            fmt = self.settings.get("date_format") or "weekday"
            rest = f"{now.month:02d}/{now.year}" if fmt == "mdy" else f"{now.year}-{now.month:02d}" \
                if fmt == "iso" else f"{now.month:02d}.{now.year}"
            _text(d, (w / 2, h * 0.84), rest, w * 0.86, h * 0.15, fill=MUTED, style="regular")
            return img
        side = p["sec"] or p["ampm"]
        with_date = show == "both"
        cy = h * (0.44 if with_date else 0.52) + (h * 0.04 if p["city"] else 0)
        big_h = h * (0.36 if with_date else 0.44)
        ratio = 0.5 if p["sec"] and not p["ampm"] else 0.42
        gap = w * 0.02 if side else 0

        def widths(size):
            big = render.font(max(6, int(size)), "bold")
            sm = render.font(max(6, int(size * ratio)), "bold")
            side_w = max(d.textlength(p["sec"], font=sm) if p["sec"] else 0,
                         d.textlength(p["ampm"], font=sm) if p["ampm"] else 0)
            return big, sm, d.textlength(p["hm"], font=big), side_w
        f, small, tw, sw = widths(big_h)
        if tw + sw + gap > w * 0.92:  # Uhrzeit samt Sekunden/AM-PM auf die Breite bringen
            f, small, tw, sw = widths(big_h * (w * 0.92 - gap) / (tw + sw))
        x = (w - tw - sw - gap) / 2
        d.text((x, cy), p["hm"], font=f, fill=FG, anchor="lm")
        if side:
            top, bottom = cy - f.size * 0.36, cy + f.size * 0.36
            sx = x + tw + gap
            if p["ampm"]:
                d.text((sx, top), p["ampm"], font=small, fill=MUTED, anchor="lt")
            if p["sec"]:
                d.text((sx, bottom), p["sec"], font=small, fill=acc, anchor="lb")
        if with_date:
            _text(d, (w / 2, h * 0.82), p["date"], w * 0.88, h * 0.15, fill=MUTED, style="regular")
        elif p["sec"] and not p["ampm"]:
            _bar(d, w, h, int(p["sec"]) / 59.0, acc, y=0.88)
        return img

    def feedback(self):
        fb = super().feedback()
        p = self._parts()
        fb["title"] = self.title() or p["city"] or p["date"]
        fb["value"] = self.time_text()
        return fb


# ==========================================================================
# Countdown und Stoppuhr (Zustand pro ActionID, übersteht Seitenwechsel)
# ==========================================================================
class _Run:
    def __init__(self, total):
        self.lock = threading.RLock()
        self.total = total
        self.remaining = total
        self.running = False
        self.end_at = 0.0
        self.finished = False
        self.timer = None
        self.owner = None
        # Stoppuhr
        self.start_at = 0.0
        self.elapsed = 0.0
        self.dialed = False   # Dauer am Dial geändert


_RUNS = {}


def _run_key(data):
    return (data or {}).get("ActionID") or id(data)


class Countdown(LiveAction):
    icon_name = "hourglass"
    default_color = "orange"
    executes_on_up = True
    use_thread = True   # Alarm über threading.Timer (in Tests aus)

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._key = _run_key(self.data)

    def duration(self):
        return parse_duration(self.settings.get("duration"), 300.0)

    @property
    def st(self):
        st = _RUNS.get(self._key)
        if st is None:
            st = _Run(self.duration())
            if not self.preview:
                _RUNS[self._key] = st
        # geänderte Dauer übernehmen, solange der Countdown nicht läuft
        with st.lock:
            if not st.running and not st.finished and st.remaining == st.total and st.total != self.duration() \
                    and not st.dialed:
                st.total = st.remaining = self.duration()
        return st

    def remaining(self):
        st = self.st
        with st.lock:
            if st.running:
                return max(0.0, st.end_at - clock())
            return st.remaining

    # --------------------------------------------------------- Zustände
    def start(self):
        st = self.st
        with st.lock:
            if st.finished:
                return self.reset()
            if st.remaining <= 0:
                st.remaining = st.total
            st.end_at = clock() + st.remaining
            st.running = True
            self._schedule(st.remaining)

    def pause(self):
        st = self.st
        with st.lock:
            st.remaining = self.remaining()
            st.running = False
            self._cancel()

    def reset(self):
        st = self.st
        with st.lock:
            self._cancel()
            st.dialed = False
            st.total = st.remaining = self.duration()
            st.running = st.finished = False
        if self.ctx and self.ctx.sound:
            self.ctx.sound.stop(("countdown", self._key))

    def _schedule(self, sec):
        self._cancel()
        if self.use_thread:
            t = threading.Timer(sec + 0.05, self._timer_fired)
            t.daemon = True
            self.st.timer = t
            t.start()

    def _cancel(self):
        st = _RUNS.get(self._key)
        if st is not None and st.timer is not None:
            st.timer.cancel()
            st.timer = None

    def _timer_fired(self):
        st = self.st
        with st.lock:
            st.timer = None
            if not st.running or self.remaining() > 0.2:
                return
        self.finish()

    def finish(self):
        st = self.st
        with st.lock:
            if st.finished:
                return
            st.running, st.finished, st.remaining = False, True, 0.0
            self._cancel()
        self._alarm()
        owner = st.owner or self
        try:
            owner.redraw()
        except Exception:  # noqa: BLE001
            pass

    def _alarm(self):
        s = self.settings
        if s.get("play_sound", True) is not False and self.ctx and self.ctx.sound:
            path = str(s.get("sound") or "")
            if path:
                path = system.map_path(path, self.ctx.cfg.get("path_map")) or path
            elif os.path.exists(DEFAULT_ALARM):
                path = DEFAULT_ALARM
            if path and os.path.exists(path):
                self.ctx.sound.play(("countdown", self._key), path, s.get("volume", 100), "",
                                    loop=bool(s.get("repeat_sound")))
            else:
                print("\a", end="", flush=True)
        if s.get("notify", True) is not False:
            from . import desktop
            label = str(s.get("label") or "").strip()
            desktop.notify(label or _("Countdown"),
                           _("The countdown of {duration} has finished.").format(
                               duration=fmt_duration(self.st.total)), icon="alarm-clock")

    # ----------------------------------------------------------- Events
    def key_up(self, held_ms=0):
        st = self.st
        if self._long(held_ms) or st.finished:
            self.reset()
        elif st.running:
            self.pause()
        else:
            self.start()
        self.redraw()

    def dial_rotate(self, ticks):
        step = parse_duration(self.settings.get("dial_step") or 30, 30.0)
        st = self.st
        with st.lock:
            if st.finished:
                return
            if st.running:
                st.end_at = max(clock() + 1, st.end_at + ticks * step)
                st.total = max(st.total, st.end_at - clock())
                self._schedule(st.end_at - clock())
            elif st.remaining == st.total:
                st.total = st.remaining = max(step, min(99 * 3600, st.total + ticks * step))
                st.dialed = True
            else:
                st.remaining = max(1.0, min(99 * 3600, st.remaining + ticks * step))
                st.total = max(st.total, st.remaining)
        self.redraw()

    def will_appear(self):
        self.st.owner = self

    def will_disappear(self):
        st = _RUNS.get(self._key)
        if st is not None and st.owner is self:
            st.owner = None

    def tick(self):
        st = self.st
        if st.running and self.remaining() <= 0:
            self.finish()
        return super().tick()

    # -------------------------------------------------------- Anzeige
    def _shown(self):
        return int(math.ceil(self.remaining() - 1e-6))

    def snapshot(self):
        st = self.st
        return self._shown(), st.running, st.finished, st.total

    def state_text(self):
        st = self.st
        if st.finished:
            return pgettext("timer", "done")
        if st.running:
            return pgettext("timer", "running")
        if st.remaining < st.total:
            return pgettext("timer", "paused")
        return str(self.settings.get("label") or "").strip() or _("Countdown")

    def _draw_one(self, size, flash=False):
        w, h = size
        st = self.st
        img, d = _canvas(size, ALARM_BG if flash else BG)
        acc = self.accent
        label = str(self.settings.get("label") or "").strip()
        top = label if label and not st.finished else self.state_text()
        _text(d, (w / 2, h * 0.15), top, w * 0.88, h * 0.15, fill=FG if flash else
              (acc if st.running else MUTED), style="bold" if st.running or flash else "regular")
        rem = self._shown()
        txt = fmt_duration(rem)
        _text(d, (w / 2, h * 0.52), txt, w * 0.9, h * (0.4 if len(txt) <= 5 else 0.3),
              fill=FG if st.running or st.finished else _mix(FG, MUTED, 0.5) if st.remaining < st.total else FG)
        frac = rem / st.total if st.total else 0
        _bar(d, w, h, frac, FG if flash else acc if st.running else MUTED)
        return img

    def frames(self, size):
        if self._custom_image():
            return super().frames(size)
        size = tuple(size)
        if self.st.finished and not self.preview:
            return [(self._draw_one(size, True), 500), (self._draw_one(size, False), 500)]
        return [(self._draw_one(size), 0)]

    def draw(self, size):
        return self._draw_one(size)

    def feedback(self):
        fb = super().feedback()
        st = self.st
        fb["title"] = self.title() or self.state_text()
        fb["value"] = fmt_duration(self._shown())
        fb["indicator"] = 100.0 * self.remaining() / st.total if st.total else 0
        return fb


class Stopwatch(LiveAction):
    icon_name = "stopwatch"
    default_color = "green"
    executes_on_up = True

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._key = _run_key(self.data)

    @property
    def st(self):
        st = _RUNS.get(self._key)
        if st is None:
            st = _Run(0.0)
            if not self.preview:
                _RUNS[self._key] = st
        return st

    def elapsed(self):
        st = self.st
        with st.lock:
            return st.elapsed + (clock() - st.start_at if st.running else 0.0)

    def start(self):
        st = self.st
        with st.lock:
            if not st.running:
                st.start_at = clock()
                st.running = True

    def stop(self):
        st = self.st
        with st.lock:
            if st.running:
                st.elapsed = self.elapsed()
                st.running = False

    def reset(self):
        st = self.st
        with st.lock:
            st.running = False
            st.elapsed = 0.0

    def key_up(self, held_ms=0):
        if self._long(held_ms):
            self.reset()
        elif self.st.running:
            self.stop()
        else:
            self.start()
        self.redraw()

    def state_text(self):
        st = self.st
        if st.running:
            return pgettext("timer", "running")
        if st.elapsed > 0:
            return pgettext("timer", "paused")
        return str(self.settings.get("label") or "").strip() or _("Stopwatch")

    def snapshot(self):
        return int(self.elapsed()), self.st.running

    def draw(self, size):
        w, h = size
        img, d = _canvas(size)
        st = self.st
        acc = self.accent
        el = self.elapsed()
        label = str(self.settings.get("label") or "").strip()
        _text(d, (w / 2, h * 0.15), label or self.state_text(), w * 0.88, h * 0.15,
              fill=acc if st.running else MUTED, style="bold" if st.running else "regular")
        txt = fmt_duration(el)
        _text(d, (w / 2, h * 0.52), txt, w * 0.9, h * (0.4 if len(txt) <= 5 else 0.3),
              fill=FG if st.running or not el else _mix(FG, MUTED, 0.5))
        _bar(d, w, h, (int(el) % 60) / 59.0 if el else 0, acc if st.running else MUTED)
        return img

    def feedback(self):
        fb = super().feedback()
        fb["title"] = self.title() or self.state_text()
        fb["value"] = fmt_duration(self.elapsed())
        fb["indicator"] = (int(self.elapsed()) % 60) * 100 / 59.0
        return fb


# ==========================================================================
# Systemmonitor
# ==========================================================================
class SysMon(LiveAction):
    metric = "cpu"
    HISTORY = 30

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.hist = deque(maxlen=self.HISTORY)
        self.hist2 = deque(maxlen=self.HISTORY)
        self._cpu = None
        self._net = sysinfo.RateMeter(clock=lambda: clock())
        self._cur = None

    @property
    def icon_name(self):
        return {"cpu": "cpu", "memory": "memory", "temperature": "thermometer", "network": "network",
                "battery": "battery", "disk": "disk"}.get(self.metric, "gauge")

    @property
    def default_color(self):
        return {"memory": "purple", "temperature": "orange", "network": "teal", "battery": "green",
                "disk": "blue"}.get(self.metric, "blue")

    @property
    def graph(self):
        g = self.settings.get("graph")
        return (self.metric in ("cpu", "memory", "temperature", "network")) if g is None else bool(g)

    def label(self):
        m = self.metric
        if m == "network":
            return str(self.settings.get("interface") or "").strip() or pgettext("short", "Network")
        if m == "disk":
            p = str(self.settings.get("path") or "/").rstrip("/")
            return (os.path.basename(p) or "/") if p else "/"
        return {"cpu": "CPU", "memory": "RAM", "temperature": pgettext("short", "Temp"),
                "battery": pgettext("short", "Battery")}.get(m, m)

    # ------------------------------------------------------- Messung
    def sample(self):
        """Neuen Messwert lesen -> dict(text, sub, frac, value, color)."""
        m, s, acc = self.metric, self.settings, self.accent
        r = {"text": "–", "sub": _("not available"), "frac": None, "value": None, "color": MUTED}
        try:
            if m == "cpu":
                if self._cpu is None:
                    self._cpu = sysinfo.CpuMeter(INFO)
                v = self._cpu.percent()
                if v is not None:
                    r.update(text=f"{v:.0f}%", sub="", frac=v / 100, value=v, color=_level(v / 100, acc))
            elif m == "memory":
                mem = INFO.memory()
                if mem:
                    r.update(text=f"{mem[0]:.0f}%", sub=f"{sysinfo.human_bytes(mem[1])} / "
                             f"{sysinfo.human_bytes(mem[2])}", frac=mem[0] / 100, value=mem[0],
                             color=_level(mem[0] / 100, acc, 0.8, 0.92))
            elif m == "temperature":
                c = INFO.temperature(str(s.get("sensor") or "").strip())
                if c is not None:
                    fahr = s.get("unit") == "f"
                    shown = c * 9 / 5 + 32 if fahr else c
                    r.update(text=f"{shown:.0f}°{'F' if fahr else 'C'}", sub="", frac=min(1.0, c / 100),
                             value=c, color=_level(c / 100, acc, 0.7, 0.85))
            elif m == "network":
                rates = self._net.update(INFO.net_bytes(str(s.get("interface") or "").strip()))
                if rates is not None:
                    down, up = rates
                    r.update(text=sysinfo.human_bytes(down, True), sub=sysinfo.human_bytes(up, True),
                             value=down, value2=up, color=acc, frac=None)
                else:
                    r.update(sub="", text="…" if INFO.net_bytes(str(s.get("interface") or "").strip())
                             else "–")
            elif m == "battery":
                b = INFO.battery(str(s.get("battery") or "").strip())
                if b:
                    pct = b["percent"]
                    if b["charging"]:
                        sub = pgettext("battery", "charging")
                    elif b["status"] == "Full" or (b["plugged"] and pct >= 95):
                        sub = pgettext("battery", "full")
                    elif b["hours"]:
                        sub = _("{time} left").format(time=fmt_duration(b["hours"] * 3600)[:-3]
                                                      if b["hours"] >= 1 else f"{int(b['hours'] * 60)} min")
                    elif b["plugged"]:
                        sub = pgettext("battery", "plugged in")
                    else:
                        sub = ""
                    r.update(text=f"{pct}%", sub=sub, frac=pct / 100, value=pct, charging=b["charging"],
                             color=acc if b["charging"] else _level(pct / 100, acc, 0.3, 0.15, reverse=True))
            elif m == "disk":
                dk = INFO.disk(str(s.get("path") or "/"))
                if dk:
                    r.update(text=f"{dk[0]:.0f}%", sub=_("{size} free").format(size=sysinfo.human_bytes(dk[1])),
                             frac=dk[0] / 100, value=dk[0], color=_level(dk[0] / 100, acc, 0.85, 0.95))
        except Exception as e:  # noqa: BLE001 - Messfehler nur anzeigen
            log.debug("sysmon %s: %s", m, e)
        self.hist.append(r["value"])
        self.hist2.append(r.get("value2"))
        self._cur = r
        return r

    def current(self):
        return self._cur if self._cur is not None else self.sample()

    def tick(self):
        self.sample()
        return super().tick() or self.graph

    def snapshot(self):
        r = self._cur or {}
        return r.get("text"), r.get("sub")

    # -------------------------------------------------------- Anzeige
    def draw(self, size):
        w, h = size
        img, d = _canvas(size)
        r = self.current()
        acc = self.accent
        ic = max(8, int(h * 0.17))
        name = "battery_charging" if r.get("charging") else self.icon_name
        lab = self.label()
        f = _font_fit(d, lab, w * 0.86 - ic - 3, h * 0.14, "bold")
        lw = d.textlength(lab, font=f)
        x0 = (w - lw - ic - 3) / 2
        img.alpha_composite(icon(name, ic, MUTED), (int(x0), int(h * 0.15 - ic / 2)))
        d.text((x0 + ic + 3, h * 0.15), lab, font=f, fill=MUTED, anchor="lm")
        graph = self.graph
        if self.metric == "network":
            fs = h * (0.2 if graph else 0.22)
            ys = (0.36, 0.54) if graph else (0.42, 0.68)
            for y, arrow, val, col in ((ys[0], "↓", r["text"], acc), (ys[1], "↑", r["sub"], MUTED)):
                if not val:
                    continue
                t = f"{arrow} {val}" if r["value"] is not None else val
                _text(d, (w / 2, h * y), t, w * 0.92, fs, fill=FG if col is acc else MUTED)
            if graph:
                vals = list(self.hist)
                top = max([v for v in list(self.hist) + list(self.hist2) if v is not None] or [1])
                _spark(img, (w * 0.08, h * 0.68, w * 0.92, h * 0.93), list(self.hist2), top, _mix(BG, MUTED, 0.7))
                _spark(img, (w * 0.08, h * 0.68, w * 0.92, h * 0.93), vals, top, acc)
            return img
        if graph and r["value"] is not None:
            _text(d, (w / 2, h * 0.43), r["text"], w * 0.9, h * 0.3, fill=FG)
            vmax = 100.0
            _spark(img, (w * 0.08, h * 0.64, w * 0.92, h * 0.93), list(self.hist), vmax, r["color"]
                   if r["color"] is not MUTED else acc)
        else:
            _text(d, (w / 2, h * 0.46), r["text"], w * 0.9, h * 0.34, fill=FG)
            if r["frac"] is not None:
                _bar(d, w, h, r["frac"], r["color"], y=0.69)
            _text(d, (w / 2, h * 0.86), r["sub"], w * 0.9, h * 0.13, fill=MUTED, style="regular")
        return img

    def feedback(self):
        fb = super().feedback()
        r = self.current()
        fb["title"] = self.title() or self.label()
        if self.metric == "network" and r["value"] is not None:
            fb["value"] = f"↓{r['text']}"
        else:
            fb["value"] = r["text"]
        fb["indicator"] = None if r["frac"] is None else r["frac"] * 100
        return fb


METRICS = ("cpu", "memory", "temperature", "network", "battery", "disk")


def lookup(uuid):
    """Aktionsklasse für com.lindeck.clock.* / com.lindeck.sysmon.* oder None."""
    u = (uuid or "").lower()
    if u == "com.lindeck.clock.clock":
        return Clock
    if u == "com.lindeck.clock.countdown":
        return Countdown
    if u == "com.lindeck.clock.stopwatch":
        return Stopwatch
    if u.startswith("com.lindeck.sysmon."):
        m = u.rsplit(".", 1)[-1]
        if m in METRICS:
            return type(f"SysMon_{m}", (SysMon,), {"metric": m})
    return None
