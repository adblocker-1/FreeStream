"""Hardware-Selbsttest: prüft ein angeschlossenes Stream Deck Schritt für Schritt.

Aufruf: freedeck selftest   (der FreeDeck-Dienst wird dafür kurz angehalten)

  1. Display: jede Taste zeigt ihre Nummer in eigener Farbe, Helligkeit läuft durch
  2. Tasten: jede Taste einmal drücken (wird grün)
  3. Dials: jeden Dial links und rechts drehen und drücken
  4. Touchstreifen: jedes Segment antippen und einmal wischen
  5. Neo: beide Touch-Tasten antippen

Das Ergebnis landet als Textdatei in ~/.local/state/freestream/ – bitte bei
Problemen an ein Issue anhängen.
"""
import json
import platform
import threading
import time

from . import __version__, paths, render
from .i18n import _, pgettext
from .models import geometry_for_deck, model_for_deck, model_name

try:
    from StreamDeck.Devices.StreamDeck import DialEventType, TouchscreenEventType
    from StreamDeck.ImageHelpers import PILHelper
except ImportError:  # pragma: no cover
    DialEventType = TouchscreenEventType = PILHelper = None

COLORS = [(200, 60, 60), (60, 160, 60), (60, 90, 200), (200, 160, 40), (160, 60, 180), (40, 170, 170),
          (220, 110, 40), (120, 120, 120)]


def _dial_label(what):
    """Anzeigename für 'left' / 'right' / 'press'."""
    return {"left": _("left"), "right": _("right"), "press": _("press")}[what]


class SelfTest:
    def __init__(self, deck, out=print):
        self.deck, self.out = deck, out
        self.model = model_for_deck(deck)
        self.geo = geometry_for_deck(deck)
        self.keys = deck.key_count()
        self.visual = deck.is_visual()
        self.dials = deck.dial_count()
        self.touch = deck.is_touch()
        self.touch_keys = getattr(deck, "TOUCH_KEY_COUNT", 0)
        self.pressed, self.released = set(), set()
        self.dial_seen = {i: set() for i in range(self.dials)}
        self.segments, self.swiped = set(), False
        self.touch_keys_seen = set()
        self.changed = threading.Event()
        self.results = {}

    # -------------------------------------------------------------- Bilder
    def _key(self, k, img):
        if not self.visual:
            return
        native = PILHelper.to_native_key_format(self.deck, img.convert("RGB"))
        with self.deck:
            self.deck.set_key_image(k, native)

    def _size(self):
        return self.deck.key_image_format()["size"] if self.visual else (72, 72)

    def _touchscreen(self, text_per_segment):
        if not self.touch:
            return
        w, h = self.deck.touchscreen_image_format()["size"]
        n = max(1, self.dials)
        segs = [render.text_key((w // n, h), t, s, bg=(0, 120, 60) if ok else (30, 30, 34))
                for t, s, ok in text_per_segment]
        img = render.compose_strip((w, h), segs)
        native = PILHelper.to_native_touchscreen_format(self.deck, img.convert("RGB"))
        with self.deck:
            self.deck.set_touchscreen_image(native, 0, 0, w, h)

    # ----------------------------------------------------------- Ereignisse
    def on_key(self, k, state):
        if k >= self.keys:
            if state:
                self.touch_keys_seen.add(k - self.keys)
        elif state:
            self.pressed.add(k)
        else:
            self.released.add(k)
            self._key(k, render.text_key(self._size(), "✓", str(k + 1), bg=(0, 140, 70)))
        self.changed.set()

    def on_dial(self, i, ev, value):
        if DialEventType and ev == DialEventType.TURN:
            self.dial_seen[i].add("right" if value > 0 else "left")
        elif value:
            self.dial_seen[i].add("press")
        self.changed.set()

    def on_touch(self, ev, value):
        if TouchscreenEventType and ev == TouchscreenEventType.DRAG:
            self.swiped = True
        else:
            w = self.deck.touchscreen_image_format()["size"][0]
            self.segments.add(min(self.dials - 1, value.get("x", 0) * self.dials // max(1, w)))
        self.changed.set()

    # ---------------------------------------------------------------- Ablauf
    def _wait(self, done, timeout, progress):
        end = time.time() + timeout
        last = None
        while time.time() < end:
            if done():
                return True
            p = progress()
            if p != last:
                self.out("   " + p)
                last = p
            self.changed.wait(0.5)
            self.changed.clear()
        return done()

    def run(self, timeout=60, step_timeout=None):
        d = self.deck
        st = step_timeout or timeout
        info = {"Modell": model_name(self.model), "Modellcode": self.model, "Layout": str(self.geo),
                "USB-PID": f"0x{d.product_id():04x}" if hasattr(d, "product_id") else "?",
                "Seriennummer": d.get_serial_number(), "Firmware": d.get_firmware_version(),
                "Tasten": self.keys, "Dials": self.dials, "Touchstreifen": self.touch,
                "Infobar": bool(getattr(d, "SCREEN_PIXEL_WIDTH", 0)), "Touch-Tasten": self.touch_keys,
                "Bildformat": d.key_image_format() if self.visual else None,
                "FreeDeck": __version__, "System": f"{platform.system()} {platform.release()}"}
        self.results["info"] = info
        self.out(f"\n== {info['Modell']} ({info['Seriennummer']}, FW {info['Firmware']}) ==")
        d.reset()
        d.set_key_callback(lambda deck, k, s: self.on_key(k, s))
        if self.dials:
            d.set_dial_callback(lambda deck, i, ev, v: self.on_dial(i, ev, v))
        if self.touch:
            d.set_touchscreen_callback(lambda deck, ev, v: self.on_touch(ev, v))

        # 1. Display und Helligkeit
        if self.visual:
            for k in range(self.keys):
                self._key(k, render.text_key(self._size(), str(k + 1), _("press"), bg=COLORS[k % len(COLORS)]))
            for b in (10, 40, 70, 100, 70):
                d.set_brightness(b)
                time.sleep(0.15)
            if getattr(d, "SCREEN_PIXEL_WIDTH", 0):
                w, h = d.screen_image_format()["size"]
                img = render.compose_infobar((w, h), _("Self-test"), _("Tap the touch keys"))
                with d:
                    d.set_screen_image(PILHelper.to_native_screen_format(d, img.convert("RGB")))
            self.results["display"] = _("shown (please check: numbers 1…{count} in reading order)").format(
                count=self.keys)
        else:
            self.results["display"] = _("no display")

        # 2. Tasten
        self.out(" " + _("Step 1: press all {count} keys one after another …").format(count=self.keys))
        ok = self._wait(lambda: len(self.released) >= self.keys, st,
                        lambda: _("{done}/{total} keys detected").format(done=len(self.released), total=self.keys))
        missing = sorted(set(range(self.keys)) - self.released)
        self.results["tasten"] = "ok" if ok else _("missing: {keys}").format(
            keys=", ".join(str(m + 1) for m in missing))

        # 3. Dials
        if self.dials:
            self.out(" " + _("Step 2: turn each of the {count} dials left and right and press it …").format(
                count=self.dials))
            need = {"left", "right", "press"}
            ok = self._wait(lambda: all(need <= s for s in self.dial_seen.values()), st,
                            lambda: " | ".join(f"Dial {i + 1}: {len(s & need)}/3" for i, s in self.dial_seen.items()))
            self.results["dials"] = "ok" if ok else "; ".join(
                _("Dial {number} missing {actions}").format(
                    number=i + 1, actions=", ".join(sorted(_dial_label(x) for x in need - s)))
                for i, s in self.dial_seen.items() if need - s)
        # 4. Touchstreifen
        if self.touch:
            self.out(" " + _("Step 3: tap each field of the touch strip and swipe once …"))

            def segs():
                return [(_("Field {number}").format(number=i + 1), "ok" if i in self.segments else _("tap"),
                         i in self.segments) for i in range(self.dials)]
            self._touchscreen(segs())

            def progress():
                self._touchscreen(segs())
                return _("{done}/{total} fields, swipe: {answer}").format(
                    done=len(self.segments), total=self.dials,
                    answer=pgettext("answer", "yes") if self.swiped else pgettext("answer", "no"))
            ok = self._wait(lambda: len(self.segments) >= self.dials and self.swiped, st, progress)
            self.results["touchstreifen"] = "ok" if ok else \
                _("{done}/{total} fields, swipe {status}").format(
                    done=len(self.segments), total=self.dials, status="ok" if self.swiped else _("missing"))
        # 5. Neo-Touch-Tasten
        if self.touch_keys:
            self.out(" " + _("Step 4: tap both touch keys (left/right below the display) …"))
            ok = self._wait(lambda: len(self.touch_keys_seen) >= self.touch_keys, st,
                            lambda: _("{done}/{total} touch keys").format(done=len(self.touch_keys_seen),
                                                                         total=self.touch_keys))
            self.results["touch_tasten"] = "ok" if ok else f"{len(self.touch_keys_seen)}/{self.touch_keys}"
        d.reset()
        return self.results


def report_text(results) -> str:
    lines = [_("FreeDeck hardware self-test"), time.strftime("%Y-%m-%d %H:%M"), ""]
    for k, v in results.get("info", {}).items():
        lines.append(f"{k:14} {v}")
    lines.append("")
    for k, v in results.items():
        if k != "info":
            lines.append(f"{k:14} {v}")
    return "\n".join(lines) + "\n"


def passed(results) -> bool:
    return all(v == "ok" for k, v in results.items() if k not in ("info", "display"))


def run(transport=None, timeout=60, out=print):
    from StreamDeck.DeviceManager import DeviceManager
    decks = DeviceManager(transport=transport).enumerate() if transport else DeviceManager().enumerate()
    if not decks:
        out(_("No device found."))
        return []
    files = []
    for deck in decks:
        deck.open()
        try:
            res = SelfTest(deck, out).run(timeout)
        finally:
            try:
                deck.reset()
                deck.close()
            except Exception:  # noqa: BLE001
                pass
        paths.ensure_dirs()
        info = res["info"]
        name = f"selftest-{info['Modellcode'] or 'unbekannt'}-{info['Seriennummer'] or 'x'}-{time.strftime('%Y%m%d-%H%M')}"
        f = paths.STATE_DIR / f"{name}.txt"
        f.write_text(report_text(res), encoding="utf-8")
        (paths.STATE_DIR / f"{name}.json").write_text(json.dumps(res, indent=2, ensure_ascii=False, default=str),
                                                     encoding="utf-8")
        out("\n" + (_("✔ PASSED – report: {path}") if passed(res) else _("✘ INCOMPLETE – report: {path}"))
            .format(path=f))
        files.append(f)
    return files
