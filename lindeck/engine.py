"""Steuerung eines einzelnen Stream Decks: Seiten, Ordner, Rendering, Events."""
import logging
import queue
import threading
import time

from PIL import Image

from . import actions as A
from . import render
from .models import geometry_for_deck, model_for_deck, model_name

log = logging.getLogger("lindeck.engine")

try:
    from StreamDeck.Devices.StreamDeck import DialEventType, TouchscreenEventType
    from StreamDeck.ImageHelpers import PILHelper
except ImportError:  # pragma: no cover
    DialEventType = TouchscreenEventType = PILHelper = None


class DeckController:
    def __init__(self, deck, store, cfg, state, vin, sound):
        self.deck = deck
        self.store = store
        self.cfg = cfg
        self.state_store = state
        self.vin = vin
        self.sound = sound
        self.lock = threading.RLock()
        self.events = queue.Queue()
        self.running = True
        self.plugin_host = None

        self.serial = deck.get_serial_number() or deck.id()
        self.model = model_for_deck(deck)
        self.geometry = geometry_for_deck(deck)
        self.visual = deck.is_visual()
        self.touch = deck.is_touch()
        self.key_count = deck.key_count()
        self.touch_key_count = getattr(deck, "TOUCH_KEY_COUNT", 0)
        self.dial_count = deck.dial_count()
        self.key_size = deck.key_image_format()["size"] if self.visual else (0, 0)
        self.ts_size = deck.touchscreen_image_format()["size"] if self.touch else (0, 0)
        self.screen_size = deck.screen_image_format()["size"] if getattr(deck, "SCREEN_PIXEL_WIDTH", 0) else (0, 0)

        dstate = self.state_store.device(self.serial)
        dcfg = self.cfg.device(self.serial)
        self.brightness = int(dcfg.get("brightness") or dstate.get("brightness") or cfg.get("brightness", 70))
        self.sleeping = False
        self.last_input = time.time()

        self.profile = None
        self.page_index = 0
        self.folder_stack = []   # Liste von Page-Objekten (Ordner)
        self.keys = {}           # Tastenindex -> Action
        self.dials = {}          # Dialindex -> Action
        self.infobar = {}        # Neo-Infobar
        self.key_down_at = {}
        self.dial_down_at = {}
        self.overlays = {}       # id(action) -> (art, bis)
        self.anim = {}           # Tastenindex -> [frames, idx, next_time]
        self.ctx = None

        self._worker = threading.Thread(target=self._loop, name=f"deck-{self.serial}", daemon=True)
        self._ticker = threading.Thread(target=self._tick_loop, name=f"tick-{self.serial}", daemon=True)

    # ================================================================ Start
    def start(self, profile_id=None):
        d = self.deck
        d.reset()
        d.set_brightness(self.brightness)
        d.set_key_callback(lambda deck, k, s: self.events.put(("key", k, s)))
        if self.dial_count:
            d.set_dial_callback(lambda deck, i, ev, v: self.events.put(("dial", i, ev, v)))
        if self.touch:
            d.set_touchscreen_callback(lambda deck, ev, v: self.events.put(("touch", ev, v)))
        self._worker.start()
        self._ticker.start()
        self.load_profile(profile_id or self._initial_profile_id())
        log.info("%s (%s, Seriennr. %s) bereit – Profil: %s", model_name(self.model), self.geometry,
                 self.serial, self.profile.name if self.profile else "keins")

    def stop(self):
        self.running = False
        self.events.put(("stop",))
        self._unload_actions()
        try:
            self.deck.reset()
            self.deck.close()
        except Exception:  # noqa: BLE001
            pass

    def _initial_profile_id(self):
        dcfg = self.cfg.device(self.serial)
        dstate = self.state_store.device(self.serial)
        for pid in (dcfg.get("profile"), dstate.get("profile")):
            if pid and self.store.get(pid):
                return self.store.get(pid).id
        cands = self.store.for_model(self.model, self.geometry)
        if cands:
            return cands[0].id
        return None  # kein passendes Profil -> Hinweis auf dem Gerät

    def candidate_profiles(self):
        c = self.store.for_model(self.model, self.geometry)
        return c or list(self.store.profiles.values())

    # ============================================================== Profile
    def load_profile(self, pid, page_index=None):
        with self.lock:
            self.profile = self.store.get(pid) if pid else None
            self.folder_stack = []
            if self.profile:
                swap = self.profile.os_type.lower().startswith("mac")
                self.ctx = A.Context(self, self.cfg, self.vin, self.sound, swap_cmd_ctrl=swap)
                dstate = self.state_store.device(self.serial)
                if page_index is None:
                    page_index = 0
                    if dstate.get("profile") == self.profile.id:
                        page_index = int(dstate.get("page", 0))
                    elif self.profile.current_page in self.profile.page_ids:
                        page_index = self.profile.page_ids.index(self.profile.current_page)
                self.page_index = max(0, min(page_index, len(self.profile.page_ids) - 1))
            else:
                self.ctx = A.Context(self, self.cfg, self.vin, self.sound)
                self.page_index = 0
            self._remember()
            self.show_current()

    def switch_profile(self, pid):
        p = self.store.get(pid)
        if not p:
            return False
        self.load_profile(p.id)
        return True

    def rotate_profile(self, direction=1):
        cands = self.candidate_profiles()
        if not cands:
            return
        ids = [p.id for p in cands]
        cur = ids.index(self.profile.id) if self.profile and self.profile.id in ids else -1
        self.load_profile(ids[(cur + direction) % len(ids)])

    def _remember(self):
        d = self.state_store.device(self.serial)
        d["profile"] = self.profile.id if self.profile else None
        d["page"] = self.page_index
        d["brightness"] = self.brightness
        try:
            self.state_store.save()
        except OSError:
            pass

    # ================================================================ Seiten
    def current_page(self):
        if self.folder_stack:
            return self.folder_stack[-1]
        if not self.profile or not self.profile.page_ids:
            return None
        return self.profile.page(self.profile.page_ids[self.page_index])

    @property
    def page_count(self):
        return len(self.profile.page_ids) if self.profile else 0

    def goto_page(self, idx):
        if not self.profile or not self.profile.page_ids:
            return
        idx = max(0, min(idx, len(self.profile.page_ids) - 1))
        with self.lock:
            self.folder_stack = []
            self.page_index = idx
            self._remember()
            self.show_current()

    def next_page(self):
        if self.page_count:
            self.goto_page((self.page_index + 1) % self.page_count)

    def previous_page(self):
        if self.page_count:
            self.goto_page((self.page_index - 1) % self.page_count)

    def open_folder(self, page_uuid):
        if not self.profile or not page_uuid:
            return
        pg = self.profile.page(page_uuid)
        if not pg:
            log.warning("Ordner %s nicht gefunden", page_uuid)
            return
        with self.lock:
            self.folder_stack.append(pg)
            self.show_current()

    def back_to_parent(self):
        with self.lock:
            if self.folder_stack:
                self.folder_stack.pop()
            self.show_current()

    # ============================================================ Darstellung
    def _unload_actions(self):
        for a in list(self.keys.values()) + list(self.dials.values()) + list(self.infobar.values()):
            try:
                a.will_disappear()
                if self.plugin_host:
                    self.plugin_host.will_disappear(self, a)
            except Exception:  # noqa: BLE001
                pass
        self.keys, self.dials, self.infobar, self.anim = {}, {}, {}, {}

    def show_current(self):
        with self.lock:
            self._unload_actions()
            pg = self.current_page()
            cols = self.geometry.cols
            if pg:
                for (c, r), data in pg.keypad.items():
                    if c >= cols or r >= self.geometry.rows:
                        continue
                    idx = r * cols + c
                    if idx >= self.key_count:
                        continue
                    a = A.create_action(data, pg, self.ctx)
                    a.slot = ("key", idx)
                    self.keys[idx] = a
                for i, data in pg.encoders.items():
                    if i < self.dial_count:
                        a = A.create_action(data, pg, self.ctx)
                        a.slot = ("dial", i)
                        self.dials[i] = a
                for i, data in pg.infobar.items():
                    a = A.create_action(data, pg, self.ctx)
                    a.slot = ("info", i)
                    self.infobar[i] = a
            for a in list(self.keys.values()) + list(self.dials.values()) + list(self.infobar.values()):
                try:
                    a.will_appear()
                    if self.plugin_host:
                        self.plugin_host.will_appear(self, a)
                except Exception as e:  # noqa: BLE001
                    log.debug("will_appear %s: %s", a.uuid, e)
            self.render_all()

    def render_all(self):
        if self.sleeping:
            return
        if self.visual and not self.profile:
            # Noch kein Profil: Hinweis anzeigen
            msgs = [("LinDeck", "bereit"), ("Profil", "fehlt"), ("Download-", "ordner")]
            for k in range(self.key_count):
                img = render.text_key(self.key_size, *msgs[k]) if k < len(msgs) else \
                    Image.new("RGBA", self.key_size, (0, 0, 0, 255))
                try:
                    self._set_key(k, img)
                except Exception:  # noqa: BLE001
                    pass
            return
        if self.visual:
            for k in range(self.key_count):
                self.render_key(k)
        if self.touch:
            self.render_touchscreen()
        if self.screen_size[0]:
            self.render_infobar()
        self.update_touch_keys()

    def redraw_action(self, action):
        if self.sleeping:
            return
        slot = action.slot
        if not slot:
            # verschachtelte Aktion: Eltern neu zeichnen
            self.render_all()
            return
        kind, idx = slot
        if kind == "key":
            self.render_key(idx)
        elif kind == "dial":
            self.render_touchscreen()
        elif kind == "info":
            self.render_infobar()

    def show_overlay(self, action, kind, seconds=1.2):
        self.overlays[id(action)] = (kind, time.time() + seconds)
        self.redraw_action(action)

    def _overlay_for(self, action):
        ov = self.overlays.get(id(action))
        if ov and ov[1] > time.time():
            return ov[0]
        if ov:
            self.overlays.pop(id(action), None)
        return None

    def _set_key(self, k, img):
        native = PILHelper.to_native_key_format(self.deck, img.convert("RGB"))
        with self.deck:
            self.deck.set_key_image(k, native)

    def key_image(self, k, frame_idx=0):
        """PIL-Bild für Taste k (ohne Gerät – auch für Vorschau benutzt)."""
        a = self.keys.get(k)
        if a is None:
            return Image.new("RGBA", self.key_size, (0, 0, 0, 255)), None
        try:
            frames = a.frames(self.key_size)
        except Exception as e:  # noqa: BLE001
            log.debug("Bildfehler %s: %s", a.uuid, e)
            frames = [(render.fallback_icon(*self.key_size, a.name), 0)]
        frame = frames[frame_idx % len(frames)][0] if frames else None
        img = render.compose_key(self.key_size, frame, a.title(), a.state_dict(), self._overlay_for(a))
        return img, (frames if len(frames) > 1 else None)

    def render_key(self, k, frame_idx=0):
        if not self.visual or k >= self.key_count:
            return
        with self.lock:
            img, frames = self.key_image(k, frame_idx)
            if frames and k not in self.anim:
                self.anim[k] = [frames, 0, time.time() + frames[0][1] / 1000.0]
            elif not frames:
                self.anim.pop(k, None)
        try:
            self._set_key(k, img)
        except Exception as e:  # noqa: BLE001
            log.debug("set_key_image %s: %s", k, e)

    def touchscreen_image(self):
        w, h = self.ts_size
        n = max(1, self.dial_count)
        seg_w = w // n
        pg = self.current_page()
        bg = None
        if pg and pg.encoder_background:
            fr = render.load_frames(pg.image_path(pg.encoder_background), w, h)
            bg = fr[0][0] if fr else None
        segs = []
        for i in range(n):
            a = self.dials.get(i)
            if a is None:
                segs.append(Image.new("RGBA", (seg_w, h), (0, 0, 0, 0)))
                continue
            try:
                fb = a.feedback()
                icon_fr = render.load_frames(a.encoder_icon_path(), 48, 48) if a.encoder_icon_path() else None
                sbg_path = a.encoder_background_path()
                sbg = render.load_frames(sbg_path, seg_w, h) if sbg_path else None
                seg = render.compose_dial_segment((seg_w, h), icon_fr[0][0] if icon_fr else None,
                                                  fb.get("title"), fb.get("value"), fb.get("indicator"),
                                                  sbg[0][0] if sbg else None)
                if self._overlay_for(a) == "alert":
                    render._alert(seg)
            except Exception as e:  # noqa: BLE001
                log.debug("Dial-Segment %s: %s", i, e)
                seg = render.fallback_icon(seg_w, h, a.name)
            segs.append(seg)
        img = render.compose_strip((w, h), segs, bg)
        return img

    def render_touchscreen(self):
        if not self.touch:
            return
        with self.lock:
            img = self.touchscreen_image()
        native = PILHelper.to_native_touchscreen_format(self.deck, img.convert("RGB"))
        try:
            with self.deck:
                self.deck.set_touchscreen_image(native, 0, 0, self.ts_size[0], self.ts_size[1])
        except Exception as e:  # noqa: BLE001
            log.debug("Touchscreen: %s", e)

    def infobar_image(self):
        w, h = self.screen_size
        clock = next((a for a in self.infobar.values() if isinstance(a, A.DigitalTime)), None)
        if clock:
            fr = clock.frames((w, h))
            return fr[0][0]
        n = max(1, self.page_count)
        return render.compose_infobar((w, h), time.strftime("%H:%M"), dots=(n, self.page_index) if n > 1 else None)

    def render_infobar(self):
        if not self.screen_size[0]:
            return
        with self.lock:
            img = self.infobar_image()
        try:
            native = PILHelper.to_native_screen_format(self.deck, img.convert("RGB"))
            with self.deck:
                self.deck.set_screen_image(native)
        except Exception as e:  # noqa: BLE001
            log.debug("Infobar: %s", e)

    def update_touch_keys(self):
        """Neo: die beiden Touch-Tasten leuchten, wenn es weitere Seiten gibt."""
        for i in range(self.touch_key_count):
            on = self.page_count > 1 and not self.folder_stack
            try:
                with self.deck:
                    self.deck.set_key_color(self.key_count + i, *((255, 255, 255) if on else (0, 0, 0)))
            except Exception:  # noqa: BLE001
                pass

    # ============================================================ Helligkeit
    def set_brightness(self, value):
        self.brightness = max(0, min(100, int(value)))
        if not self.sleeping:
            with self.deck:
                self.deck.set_brightness(self.brightness)
        self._remember()

    def change_brightness(self, delta):
        self.set_brightness(self.brightness + delta)

    def sleep_device(self):
        self.sleeping = True
        try:
            with self.deck:
                self.deck.set_brightness(0)
        except Exception:  # noqa: BLE001
            pass

    def wake(self):
        if not self.sleeping:
            return
        self.sleeping = False
        with self.deck:
            self.deck.set_brightness(self.brightness)
        self.render_all()

    # ================================================================ Events
    def _loop(self):
        while self.running:
            ev = self.events.get()
            if ev[0] == "stop":
                break
            try:
                self._handle(ev)
            except Exception as e:  # noqa: BLE001
                log.exception("Fehler bei Ereignis %s: %s", ev, e)

    def _handle(self, ev):
        kind = ev[0]
        self.last_input = time.time()
        if self.sleeping:
            # Erster Tastendruck weckt nur auf (wie bei Elgato)
            pressed = (kind == "key" and ev[2]) or (kind == "dial" and ev[2] == DialEventType.PUSH and ev[3]) \
                or kind == "touch"
            if pressed:
                self.wake()
                self._swallow = True
            return
        if getattr(self, "_swallow", False):
            if kind in ("key", "dial"):
                self._swallow = False
                return
        if kind == "key":
            self.on_key(ev[1], ev[2])
        elif kind == "dial":
            _, i, etype, value = ev
            if etype == DialEventType.TURN:
                self.on_dial_rotate(i, value)
            else:
                self.on_dial_push(i, value)
        elif kind == "touch":
            self.on_touch(ev[1], ev[2])

    def on_key(self, k, pressed):
        if k >= self.key_count:
            # Neo-Touchtasten: Seite zurück / weiter
            if pressed:
                (self.previous_page if k == self.key_count else self.next_page)()
            return
        a = self.keys.get(k)
        if pressed:
            self.key_down_at[k] = time.time()
            if a is not None:
                if self.plugin_host and not a.supported:
                    self.plugin_host.key_down(a)
                    return
                if not a.executes_on_up:
                    self._run(a.key_down)
        else:
            held = (time.time() - self.key_down_at.pop(k, time.time())) * 1000
            if a is not None and self.keys.get(k) is a:
                if self.plugin_host and not a.supported:
                    self.plugin_host.key_up(a)
                    return
                self._run(a.key_up, held)

    def on_dial_rotate(self, i, ticks):
        a = self.dials.get(i)
        if a:
            if self.plugin_host and not a.supported:
                self.plugin_host.dial_rotate(a, ticks)
                return
            self._run(a.dial_rotate, ticks)

    def on_dial_push(self, i, pressed):
        a = self.dials.get(i)
        if not a:
            return
        if self.plugin_host and not a.supported:
            (self.plugin_host.dial_down if pressed else self.plugin_host.dial_up)(a)
            return
        if pressed:
            self.dial_down_at[i] = time.time()
            self._run(a.dial_down)
        else:
            held = (time.time() - self.dial_down_at.pop(i, time.time())) * 1000
            self._run(a.dial_up, held)

    def on_touch(self, etype, value):
        if etype == TouchscreenEventType.DRAG:
            dx = value.get("x_out", 0) - value.get("x", 0)
            if abs(dx) > 40:
                (self.previous_page if dx > 0 else self.next_page)()
            return
        seg_w = self.ts_size[0] // max(1, self.dial_count)
        i = min(self.dial_count - 1, value.get("x", 0) // max(1, seg_w))
        a = self.dials.get(i)
        if a:
            if self.plugin_host and not a.supported:
                self.plugin_host.touch_tap(a, value, etype == TouchscreenEventType.LONG)
                return
            self._run(a.touch, etype == TouchscreenEventType.LONG)

    def _run(self, fn, *args):
        if getattr(getattr(fn, "__self__", None), "inline", False):
            try:
                fn(*args)
            except Exception as e:  # noqa: BLE001
                log.exception("Aktion fehlgeschlagen: %s", e)
            return

        def wrapped():
            try:
                fn(*args)
            except Exception as e:  # noqa: BLE001
                log.exception("Aktion fehlgeschlagen: %s", e)
        threading.Thread(target=wrapped, daemon=True).start()

    # ================================================================ Ticker
    def _tick_loop(self):
        last_sec = 0
        while self.running:
            time.sleep(0.04)
            now = time.time()
            if self.sleeping:
                continue
            # GIF-Animationen
            for k, st in list(self.anim.items()):
                frames, idx, nxt = st
                if now >= nxt:
                    idx = (idx + 1) % len(frames)
                    st[1], st[2] = idx, now + max(20, frames[idx][1]) / 1000.0
                    try:
                        img, _ = self.key_image(k, idx)
                        self._set_key(k, img)
                    except Exception:  # noqa: BLE001
                        pass
            if now - last_sec >= 1.0:
                last_sec = now
                self._second_tick(now)

    def _second_tick(self, now):
        mins = self.cfg.get("sleep_after_minutes", 0) or 0
        if mins and now - self.last_input > mins * 60:
            self.sleep_device()
            return
        # abgelaufene Overlays entfernen
        for aid, (kind, until) in list(self.overlays.items()):
            if until <= now:
                self.overlays.pop(aid, None)
                for a in list(self.keys.values()) + list(self.dials.values()):
                    if id(a) == aid:
                        self.redraw_action(a)
        dial_dirty = False
        for k, a in list(self.keys.items()):
            if a.needs_tick:
                try:
                    if a.tick():
                        self.render_key(k)
                except Exception:  # noqa: BLE001
                    pass
        for a in list(self.dials.values()):
            if a.needs_tick:
                try:
                    dial_dirty |= bool(a.tick())
                except Exception:  # noqa: BLE001
                    pass
        if dial_dirty:
            self.render_touchscreen()
        if self.screen_size[0] and int(now) % 10 == 0:
            self.render_infobar()

    # ============================================================== Vorschau
    def preview_image(self, gap=8):
        """Gesamtbild des Geräts (Tasten + Touchstreifen + Infobar) für die Vorschau."""
        cols, rows = self.geometry.cols, self.geometry.rows
        kw, kh = self.key_size if self.visual else (72, 72)
        width = cols * kw + (cols + 1) * gap
        height = rows * kh + (rows + 1) * gap
        extra = []
        if self.touch:
            extra.append(("ts", self.touchscreen_image()))
        if self.screen_size[0]:
            extra.append(("info", self.infobar_image()))
        for _, im in extra:
            width = max(width, im.size[0] + 2 * gap)
            height += im.size[1] + gap
        canvas = Image.new("RGB", (width, height), (28, 28, 32))
        for k in range(self.key_count):
            r, c = divmod(k, cols)
            if self.visual:
                img, _ = self.key_image(k)
            else:
                a = self.keys.get(k)
                img = render.fallback_icon(kw, kh, a.title() or a.name if a else "–")
            canvas.paste(img.convert("RGB"), (gap + c * (kw + gap), gap + r * (kh + gap)))
        y = rows * kh + (rows + 1) * gap
        for _, im in extra:
            canvas.paste(im.convert("RGB"), ((width - im.size[0]) // 2, y))
            y += im.size[1] + gap
        return canvas
