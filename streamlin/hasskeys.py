"""Home-Assistant-Aktionen für LinDeck (Schalten, Dienst, Szene/Skript, Zustand).

Die Tasten zeichnen ihre Symbole selbst (PIL) und zeigen den Zustand farbig.
Alle Anfragen laufen über den gemeinsamen Client in hass.py: Zustände kommen
aus dem Zwischenspeicher, Dienstaufrufe aus dem Arbeits-Thread – Tastendruck
und Zeichnen blockieren nie. Fehler aus dem Arbeits-Thread werden im
nächsten tick() angezeigt (Gerät nur aus dem Geräte-Thread ansprechen).
"""
import logging
import math

from PIL import Image, ImageDraw

from . import hass, render
from .i18n import _, pgettext
from .native import NativeAction

log = logging.getLogger("streamlin.hasskeys")

PREFIX = "com.lindeck.homeassistant."
OFF_BG, ERR_BG, IDLE_BG = (40, 40, 46), (110, 30, 30), (16, 78, 122)
ON_COLORS = {"light": (196, 138, 22), "cover": (28, 104, 176), "climate": (196, 84, 32), "fan": (16, 128, 140),
             "media_player": (98, 70, 180), "lock": (22, 120, 64)}
ON_BG = (22, 120, 64)
MUTED = (200, 200, 210)

GLYPHS = {"light": "bulb", "switch": "power", "input_boolean": "power", "automation": "power", "fan": "fan",
          "cover": "cover", "climate": "thermo", "sensor": "gauge", "binary_sensor": "gauge", "lock": "lock",
          "scene": "spark", "script": "play", "media_player": "play", "weather": "thermo",
          "input_number": "gauge", "number": "gauge", "button": "power", "input_button": "power"}


# ==========================================================================
# Zeichnen
# ==========================================================================
def draw_glyph(img, kind, box, color=(255, 255, 255)):
    """Eigenes Symbol in box (x0, y0, x1, y1) zeichnen."""
    k = 4
    x0, y0, x1, y1 = box
    S = int(max(1, (x1 - x0)) * k)
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    c = tuple(color) + (255,)
    lw = max(2, round(S * 0.08))

    def P(px, py):
        return (px * S, py * S)

    if kind == "bulb":
        d.ellipse((*P(0.2, 0.04), *P(0.8, 0.64)), outline=c, width=lw)
        d.rectangle((*P(0.36, 0.6), *P(0.64, 0.66)), fill=(0, 0, 0, 0))
        d.line([P(0.36, 0.58), P(0.36, 0.76)], fill=c, width=lw)
        d.line([P(0.64, 0.58), P(0.64, 0.76)], fill=c, width=lw)
        d.line([P(0.34, 0.78), P(0.66, 0.78)], fill=c, width=lw)
        d.line([P(0.38, 0.9), P(0.62, 0.9)], fill=c, width=lw)
    elif kind == "power":
        d.arc((*P(0.12, 0.14), *P(0.88, 0.9)), -60, 240, fill=c, width=lw)
        d.line([P(0.5, 0.04), P(0.5, 0.48)], fill=c, width=lw)
    elif kind == "fan":
        cx, cy = 0.5, 0.5
        for i in range(3):
            a = i * 2 * math.pi / 3
            bx, by = cx + 0.26 * math.cos(a), cy + 0.26 * math.sin(a)
            d.ellipse((*P(bx - 0.2, by - 0.2), *P(bx + 0.2, by + 0.2)), fill=c)
        d.ellipse((*P(0.42, 0.42), *P(0.58, 0.58)), fill=(0, 0, 0, 160))
    elif kind == "cover":
        d.rectangle((*P(0.1, 0.06), *P(0.9, 0.94)), outline=c, width=lw)
        for y in (0.28, 0.46, 0.64):
            d.line([P(0.18, y), P(0.82, y)], fill=c, width=max(2, lw // 2))
    elif kind == "thermo":
        d.rounded_rectangle((*P(0.4, 0.04), *P(0.6, 0.66)), radius=0.1 * S, outline=c, width=lw)
        d.ellipse((*P(0.3, 0.6), *P(0.7, 0.96)), fill=c)
        d.line([P(0.5, 0.3), P(0.5, 0.7)], fill=c, width=lw)
    elif kind == "gauge":
        d.arc((*P(0.06, 0.16), *P(0.94, 1.04)), 180, 360, fill=c, width=lw)
        d.line([P(0.5, 0.6), P(0.76, 0.32)], fill=c, width=lw)
        d.ellipse((*P(0.42, 0.52), *P(0.58, 0.68)), fill=c)
    elif kind == "lock":
        d.arc((*P(0.26, 0.04), *P(0.74, 0.56)), 180, 360, fill=c, width=lw)
        d.line([P(0.26, 0.3), P(0.26, 0.46)], fill=c, width=lw)
        d.line([P(0.74, 0.3), P(0.74, 0.46)], fill=c, width=lw)
        d.rounded_rectangle((*P(0.14, 0.44), *P(0.86, 0.96)), radius=0.08 * S, fill=c)
    elif kind == "spark":
        d.polygon([P(0.42, 0.06), P(0.54, 0.4), P(0.88, 0.52), P(0.54, 0.64), P(0.42, 0.98), P(0.3, 0.64),
                   P(0.0, 0.52), P(0.3, 0.4)], fill=c)
        d.polygon([P(0.82, 0.02), P(0.86, 0.14), P(0.98, 0.18), P(0.86, 0.22), P(0.82, 0.34), P(0.78, 0.22),
                   P(0.66, 0.18), P(0.78, 0.14)], fill=c)
    elif kind == "play":
        d.polygon([P(0.22, 0.08), P(0.22, 0.92), P(0.88, 0.5)], fill=c)
    elif kind == "bolt":
        d.polygon([P(0.58, 0.02), P(0.16, 0.56), P(0.46, 0.56), P(0.38, 0.98), P(0.84, 0.4), P(0.54, 0.4)], fill=c)
    elif kind == "alert":
        d.polygon([P(0.5, 0.04), P(0.02, 0.92), P(0.98, 0.92)], outline=c, width=lw)
        d.line([P(0.5, 0.34), P(0.5, 0.62)], fill=c, width=lw)
        d.ellipse((*P(0.45, 0.72), *P(0.55, 0.82)), fill=c)
    else:  # Haus
        d.polygon([P(0.5, 0.04), P(0.96, 0.46), P(0.84, 0.46), P(0.84, 0.94), P(0.16, 0.94), P(0.16, 0.46),
                   P(0.04, 0.46)], fill=c)
        d.rectangle((*P(0.42, 0.62), *P(0.58, 0.94)), fill=(0, 0, 0, 0))
    size = max(1, int(x1 - x0))
    img.alpha_composite(im.resize((size, size), Image.LANCZOS), (int(x0), int(y0)))
    return img


def glyph_image(kind, size=48, color=(255, 255, 255)):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    return draw_glyph(img, kind, (0, 0, size, size), color)


def _fit_font(d, text, max_w, size, style="bold", min_size=8):
    while size > min_size and d.textlength(text, font=render.font(size, style)) > max_w:
        size -= 1
    f = render.font(size, style)
    if d.textlength(text, font=f) > max_w:
        while len(text) > 1 and d.textlength(text + "…", font=f) > max_w:
            text = text[:-1]
        text += "…"
    return text, f


def draw_key(size, glyph, bg, name="", value="", big=False):
    """Taste: Symbol oben, Name und Wert darunter (big: Wert groß in der Mitte)."""
    w, h = size
    img = Image.new("RGBA", (w, h), tuple(bg) + (255,))
    d = ImageDraw.Draw(img)
    if big:
        g = int(h * 0.22)
        draw_glyph(img, glyph, (w * 0.08, h * 0.07, w * 0.08 + g, h * 0.07 + g), MUTED)
        if value:
            txt, f = _fit_font(d, value, w - 6, int(h * 0.3))
            d.text((w / 2, h * 0.52), txt, font=f, fill=(255, 255, 255), anchor="mm")
        if name:
            txt, f = _fit_font(d, name, w - 6, int(h * 0.14), "regular")
            d.text((w / 2, h * 0.84), txt, font=f, fill=MUTED, anchor="mm")
        return img
    g = int(h * (0.36 if (name or value) else 0.5))
    top = h * (0.1 if (name or value) else 0.25)
    draw_glyph(img, glyph, ((w - g) / 2, top, (w + g) / 2, top + g))
    y = top + g + h * 0.12
    if name:
        txt, f = _fit_font(d, name, w - 6, int(h * 0.15))
        d.text((w / 2, y), txt, font=f, fill=(255, 255, 255), anchor="mm")
        y += h * 0.17
    if value:
        txt, f = _fit_font(d, value, w - 6, int(h * 0.13), "regular")
        d.text((w / 2, min(y, h * 0.9)), txt, font=f, fill=MUTED, anchor="mm")
    return img


# ==========================================================================
# Aktionen
# ==========================================================================
class HAAction(NativeAction):
    """Gemeinsame Basis: Client, Entität, Fehlerzustand, Zeichnen."""
    glyph = "home"

    @property
    def ha(self):
        return hass.client(self.ctx.cfg)

    @property
    def preview(self):
        return bool(getattr(self.ctx.ctrl, "preview", False))

    @property
    def entity(self):
        return str(self.settings.get("entity_id") or "").strip()

    def st(self):
        if self.preview or not self.entity:
            return None
        return self.ha.state(self.entity)

    def will_appear(self):
        if not self.preview and self.entity:
            self.ha.watch(self.entity)

    def problem(self):
        """Kurzer Fehlertext für die Taste oder ""."""
        if self.preview:
            return ""
        ha = self.ha
        if not ha.configured:
            return _("Set up")
        if ha.error_kind == hass.AUTH:
            return _("Token?")
        if ha.error_kind:
            return _("Offline")
        if self.entity and not ha.known(self.entity):
            return _("Unknown")
        return ""

    def entity_glyph(self):
        return GLYPHS.get(hass.domain_of(self.entity), self.glyph)

    def display_name(self):
        if self.entity:
            return hass.friendly_name(self.st(), self.entity)
        return ""

    def main_text(self):
        return self.display_name() or self.name

    def preview_text(self):
        return self.main_text()

    def colors(self):
        on = self.active()
        if on:
            return ON_COLORS.get(hass.domain_of(self.entity), ON_BG)
        return OFF_BG

    def frames(self, size):
        if self.image_override is not None or self._has_image():
            return super(NativeAction, self).frames(size)
        name = "" if self.title() else self.display_name()
        prob = self.problem()
        if prob:
            return [(draw_key(size, self.entity_glyph() if self.entity else "alert", ERR_BG,
                              name, prob), 0)]
        return [(self.draw(size, name), 0)]

    def draw(self, size, name):
        return draw_key(size, self.entity_glyph(), self.colors(), name, self.status_text())

    @property
    def encoder_icon_image(self):
        if self.image_override is not None or self._has_image():
            return None
        return glyph_image(self.entity_glyph(), 48)

    # ------------------------------------------------------- Ablauf
    def _failed(self, e):
        """Aus dem Arbeits-Thread: Fehler merken, tick() zeigt ihn an."""
        self._fail = str(e)
        self.ha.refresh()

    def tick(self):
        if self.preview:
            return False
        if getattr(self, "_fail", None):
            self._fail = None
            self.alert()
        self._sync_state()
        snap = (self.ha.version, self.ha.error_kind, self.state)
        changed = snap != getattr(self, "_hsnap", None)
        self._hsnap = snap
        return changed

    def _ready(self, need_entity=True):
        """Vor einem Aufruf prüfen; False (mit Warnsymbol), wenn es nicht geht."""
        ha = self.ha
        if not ha.configured:
            log.warning(_("Home Assistant is not set up yet"))
            self.alert()
            return False
        if need_entity and not self.entity:
            log.warning(_("{action}: no entity chosen").format(action=self.name))
            self.alert()
            return False
        return True

    def feedback(self):
        fb = super().feedback()
        fb["title"] = self.title() or self.display_name() or self.name
        fb["value"] = self.problem() or self.status_text()
        return fb

    def dial_up(self, held_ms=0):
        pass


class ToggleAction(HAAction):
    """Entität schalten; zeigt An/Aus farbig, bei Lampen die Helligkeit. Dial: Helligkeit/Temperatur …"""
    glyph = "power"

    def active(self):
        return hass.is_on(self.st())

    def status_text(self):
        st = self.st()
        if not st:
            return ""
        attrs = st.get("attributes") or {}
        on = hass.is_on(st)
        dom = hass.domain_of(self.entity)
        if self.settings.get("show_value", True) is not False and on:
            if dom == "light" and attrs.get("brightness") is not None:
                return f"{round(int(attrs['brightness']) * 100 / 255)}%"
            if dom == "cover" and attrs.get("current_position") is not None:
                return f"{attrs['current_position']}%"
            if dom == "fan" and attrs.get("percentage") is not None:
                return f"{attrs['percentage']}%"
        if dom == "climate" and attrs.get("temperature") is not None and self.settings.get("show_value", True):
            return f"{attrs['temperature']}°"
        if on is None:
            return hass.format_state(st)
        if dom == "cover":
            return _("open") if on else _("closed")
        if dom == "lock":
            return _("unlocked") if on else _("locked")
        return pgettext("state", "on") if on else pgettext("state", "off")

    def key_down(self):
        if not self._ready():
            return
        ha, eid = self.ha, self.entity
        on = hass.is_on(ha.state(eid))
        dom = hass.domain_of(eid)
        if on is not None and dom in ("light", "switch", "fan", "input_boolean", "automation", "siren",
                                      "humidifier"):
            ha.set_local(eid, "off" if on else "on")  # sofort anzeigen, Abfrage korrigiert
        ha.submit(ha.toggle, eid, on_error=self._failed)
        self.tick()
        self.redraw()

    def _step(self):
        try:
            return max(1, int(self.settings.get("step") or 5))
        except (TypeError, ValueError):
            return 5

    def dial_rotate(self, ticks):
        if not self._ready():
            return
        ha, eid = self.ha, self.entity
        st = ha.state(eid) or {}
        attrs = st.get("attributes") or {}
        dom, step = hass.domain_of(eid), self._step()
        on = hass.is_on(st)
        if dom == "light":
            cur = round(int(attrs.get("brightness") or 0) * 100 / 255) if on else 0
            new = max(0, min(100, cur + step * ticks))
            ha.set_local(eid, "on" if new else "off", brightness=round(new * 255 / 100))
            call = ("light", "turn_on", {"brightness_pct": new}) if new else ("light", "turn_off", {})
        elif dom == "climate":
            cur = float(attrs.get("temperature") or attrs.get("current_temperature") or 20)
            tstep = float(attrs.get("target_temp_step") or 0.5)
            new = round(cur + tstep * ticks, 1)
            new = max(float(attrs.get("min_temp", -50)), min(float(attrs.get("max_temp", 100)), new))
            ha.set_local(eid, temperature=new)
            call = ("climate", "set_temperature", {"temperature": new})
        elif dom == "fan":
            new = max(0, min(100, int(attrs.get("percentage") or 0) + step * ticks))
            ha.set_local(eid, "on" if new else "off", percentage=new)
            call = ("fan", "set_percentage", {"percentage": new})
        elif dom == "cover":
            new = max(0, min(100, int(attrs.get("current_position") or 0) + step * ticks))
            ha.set_local(eid, "open" if new else "closed", current_position=new)
            call = ("cover", "set_cover_position", {"position": new})
        elif dom == "media_player":
            new = round(max(0.0, min(1.0, float(attrs.get("volume_level") or 0) + step / 100 * ticks)), 2)
            ha.set_local(eid, volume_level=new)
            call = ("media_player", "volume_set", {"volume_level": new})
        elif dom in ("input_number", "number"):
            nstep = float(attrs.get("step") or 1)
            try:
                cur = float(st.get("state"))
            except (TypeError, ValueError):
                cur = float(attrs.get("min") or 0)
            new = max(float(attrs.get("min", cur - 1e9)), min(float(attrs.get("max", cur + 1e9)), cur + nstep * ticks))
            ha.set_local(eid, str(new))
            call = (dom, "set_value", {"value": new})
        else:
            return
        self._dial_call = call
        if not getattr(self, "_dial_queued", False):  # schnelle Drehungen zu einem Aufruf zusammenfassen
            self._dial_queued = True
            ha.submit(self._send_dial, on_error=self._failed)
        self.redraw()

    def _send_dial(self):
        self._dial_queued = False
        dom, svc, data = self._dial_call
        return self.ha.call_service(dom, svc, dict(data, entity_id=self.entity))

    def dial_down(self):
        self.key_down()

    def feedback(self):
        fb = super().feedback()
        st = self.st()
        if st and not self.problem():
            attrs = st.get("attributes") or {}
            dom = hass.domain_of(self.entity)
            on = hass.is_on(st)
            if dom == "light":
                fb["indicator"] = round(int(attrs.get("brightness") or 0) * 100 / 255) if on else 0
            elif dom == "fan" and attrs.get("percentage") is not None:
                fb["indicator"] = attrs["percentage"]
            elif dom == "cover" and attrs.get("current_position") is not None:
                fb["indicator"] = attrs["current_position"]
            elif dom == "media_player" and attrs.get("volume_level") is not None:
                fb["indicator"] = round(float(attrs["volume_level"]) * 100)
            elif dom == "climate" and attrs.get("temperature") is not None:
                lo, hi = float(attrs.get("min_temp", 7)), float(attrs.get("max_temp", 35))
                fb["indicator"] = max(0, min(100, (float(attrs["temperature"]) - lo) * 100 / max(1, hi - lo)))
        return fb


class ServiceAction(HAAction):
    """Beliebigen Dienst aufrufen (domain.service, Entität, JSON-Daten)."""
    glyph = "bolt"

    def entity_glyph(self):
        return "bolt"

    def active(self):
        return hass.is_on(self.st()) if self.entity else None

    def colors(self):
        on = self.active()
        return IDLE_BG if on is None else (ON_COLORS.get(hass.domain_of(self.entity), ON_BG) if on else OFF_BG)

    def display_name(self):
        return super().display_name() or str(self.settings.get("service") or "")

    def status_text(self):
        return str(self.settings.get("service") or "") if self.entity else ""

    def key_down(self):
        if not self._ready(need_entity=False):
            return
        try:
            dom, svc = hass.parse_service(self.settings.get("service"))
            data = hass.parse_data(self.settings.get("data"))
        except hass.HAError as e:
            log.warning("%s: %s", self.name, e)
            self.alert()
            return
        if self.entity:
            data.setdefault("entity_id", self.entity)
        self._done = False
        self.ha.submit(self.ha.call_service, dom, svc, data, on_error=self._failed,
                       on_done=lambda r: setattr(self, "_done", True))

    def tick(self):
        if getattr(self, "_done", False):
            self._done = False
            self.ok()
        return super().tick()


class SceneAction(ServiceAction):
    """Szene aktivieren oder Skript starten."""

    def entity_glyph(self):
        return "play" if hass.domain_of(self.entity) == "script" else "spark"

    def display_name(self):
        return HAAction.display_name(self)

    def active(self):
        # Skripte laufen („on“); Szenen haben keinen An-Zustand
        return hass.is_on(self.st()) if hass.domain_of(self.entity) == "script" else None

    def status_text(self):
        return _("Script") if hass.domain_of(self.entity) == "script" else _("Scene")

    def key_down(self):
        if not self._ready():
            return
        self._done = False
        self.ha.submit(self.ha.toggle, self.entity, on_error=self._failed,
                       on_done=lambda r: setattr(self, "_done", True))


class StateAction(HAAction):
    """Wert einer Entität anzeigen (z. B. Temperatur mit Einheit), live aktualisiert."""
    glyph = "gauge"

    def active(self):
        return None

    def _decimals(self):
        try:
            d = int(self.settings.get("decimals", -1))
        except (TypeError, ValueError):
            d = -1
        return d if d >= 0 else None

    def status_text(self):
        return hass.format_state(self.st(), str(self.settings.get("attribute") or "").strip(), self._decimals())

    def draw(self, size, name):
        return draw_key(size, self.entity_glyph(), OFF_BG, name, self.status_text() or "–", big=True)

    def key_down(self):
        if not self._ready():
            return
        self.ha.submit(self.ha.fetch, self.entity, on_error=self._failed)

    def dial_down(self):
        self.key_down()


CLASSES = {"toggle": ToggleAction, "service": ServiceAction, "scene": SceneAction, "state": StateAction}


def lookup(uuid):
    if not uuid.startswith(PREFIX):
        return None
    return CLASSES.get(uuid[len(PREFIX):])
