"""Nachbau aller eingebauten Elgato-Aktionen.

Jede Aktion entspricht einer UUID aus den Elgato-Profilen. Einstellungen
werden 1:1 aus "Settings" gelesen, Zustände/Titel/Bilder aus "States".
Aktionen von Drittanbieter-Plugins werden mit ihrem gespeicherten Bild
angezeigt (PluginAction) und später über den Plugin-Host ausgeführt.
"""
import logging
import random
import threading
import time
from datetime import datetime

from . import render, system
from .i18n import _, pgettext

log = logging.getLogger("freestream.actions")

# Plugin-Ordner der eingebauten Aktionen (für Standard-Icons aus den Elgato-Assets)
BUILTIN_PLUGIN_DIR = {
    "com.elgato.streamdeck.dial.stack": "com.elgato.streamdeck.dial.sdPlugin",
    "com.elgato.streamdeck.keys.stack": "com.elgato.streamdeck.keys.sdPlugin",
    "com.elgato.streamdeck.keys.adaptor": "com.elgato.streamdeck.keys.sdPlugin",
    "com.elgato.streamdeck.keys.logic": "com.elgato.streamdeck.keys.sdPlugin",
    "com.elgato.streamdeck.multiactions.routine": "com.elgato.streamdeck.multiactions.sdPlugin",
    "com.elgato.streamdeck.multiactions.routine2": "com.elgato.streamdeck.multiactions.sdPlugin",
    "com.elgato.streamdeck.multiactions.random": "com.elgato.streamdeck.multiactions.sdPlugin",
    "com.elgato.streamdeck.multiactions.delay": "com.elgato.streamdeck.multiactions.sdPlugin",
    "com.elgato.streamdeck.page.previous": "com.elgato.streamdeck.page.sdPlugin",
    "com.elgato.streamdeck.page.next": "com.elgato.streamdeck.page.sdPlugin",
    "com.elgato.streamdeck.page.goto": "com.elgato.streamdeck.page.sdPlugin",
    "com.elgato.streamdeck.page.indicator": "com.elgato.streamdeck.page.sdPlugin",
    "com.elgato.streamdeck.profile.backtoparent": "com.elgato.streamdeck.profile.backtoparent.sdPlugin",
    "com.elgato.streamdeck.profile.openchild": "com.elgato.streamdeck.profile.openchild.sdPlugin",
    "com.elgato.streamdeck.profile.rotate": "com.elgato.streamdeck.profile.rotate.sdPlugin",
    "com.elgato.streamdeck.soundboard.playaudio": "com.elgato.streamdeck.soundboard.sdPlugin",
    "com.elgato.streamdeck.soundboard.stopaudioplay": "com.elgato.streamdeck.soundboard.sdPlugin",
    "com.elgato.streamdeck.system.mouse-wheel": "com.elgato.streamdeck.system.mouse.sdPlugin",
    "com.elgato.streamdeck.system.timer": "com.elgato.streamdeck.timer.sdPlugin",
}

# Standard-Icons (manifest "States[].Image") – Name ohne Endung
DEFAULT_ICONS = {
    "com.elgato.streamdeck.dial.stack": ["Images/btn_dialStack"],
    "com.elgato.streamdeck.keys.stack": ["Images/btn_keyStack"],
    "com.elgato.streamdeck.keys.adaptor": ["Images/btn_keyAdaptor"],
    "com.elgato.streamdeck.keys.logic": ["Images/btn_keyLogic"],
    "com.elgato.streamdeck.multiactions.routine": ["Images/btn_multiAction"],
    "com.elgato.streamdeck.multiactions.routine2": ["Images/btn_toggleMultiActionOff", "Images/btn_toggleMultiActionOn"],
    "com.elgato.streamdeck.multiactions.random": ["Images/btn_randomAction"],
    "com.elgato.streamdeck.multiactions.delay": ["Images/btn_duration"],
    "com.elgato.streamdeck.page.previous": ["Images/btn_previousPage"],
    "com.elgato.streamdeck.page.next": ["Images/btn_nextPage"],
    "com.elgato.streamdeck.page.goto": ["Images/btn_goToPage"],
    "com.elgato.streamdeck.page.indicator": ["Images/btn_pageIndicator"],
    "com.elgato.streamdeck.profile.backtoparent": ["Images/btn_backToParent"],
    "com.elgato.streamdeck.profile.openchild": ["Images/btn_folder"],
    "com.elgato.streamdeck.profile.rotate": ["Images/btn_switchProfile"],
    "com.elgato.streamdeck.soundboard.playaudio": ["Images/btn_playAudio"],
    "com.elgato.streamdeck.soundboard.stopaudioplay": ["Images/btn_stopAudio"],
    "com.elgato.streamdeck.system.close": ["Images/btn_close"],
    "com.elgato.streamdeck.system.digitaltime": ["Images/btn_digitalTime"],
    "com.elgato.streamdeck.system.hotkey": ["Images/btn_hotkey"],
    "com.elgato.streamdeck.system.hotkeyswitch": ["Images/btn_toggleHotkeyOn", "Images/btn_toggleHotkeyOff"],
    "com.elgato.streamdeck.system.keybrightness": ["Images/btn_keybrightness_max"],
    "com.elgato.streamdeck.system.mouse-wheel": ["Images/btn_custom_trigger_wheel"],
    "com.elgato.streamdeck.system.multimedia": ["Images/btn_media_play_pause"],
    "com.elgato.streamdeck.system.open": ["Images/btn_open"],
    "com.elgato.streamdeck.system.openapp": ["Images/btn_open"],
    "com.elgato.streamdeck.system.pagination": ["Images/btn_pagination"],
    "com.elgato.streamdeck.system.sleep": ["Images/btn_sleep"],
    "com.elgato.streamdeck.system.text": ["Images/btn_text"],
    "com.elgato.streamdeck.system.website": ["Images/btn_website"],
    "com.elgato.streamdeck.system.timer": ["Images/btn_timer"],
    "com.elgato.streamdeck.system.vsdtoggle": ["Images/btn_vsdtoggle"],
}

MEDIA_ICONS = {0: "btn_media_prev_track", 1: "btn_media_play_pause", 2: "btn_media_next_track",
               3: "btn_media_stop", 4: "btn_media_mute", 5: "btn_media_volume_up", 6: "btn_media_volume_down",
               13: "btn_media_fast_forward", 14: "btn_media_rewind", 18: "btn_media_volume"}
BRIGHTNESS_ICONS = {0: "btn_keybrightness_increase", 1: "btn_keybrightness_decrease",
                    2: "btn_keybrightness_min", 3: "btn_keybrightness_low", 4: "btn_keybrightness_medium",
                    5: "btn_keybrightness_high", 6: "btn_keybrightness_max"}


def plugin_dir_for(uuid: str) -> str:
    if uuid in BUILTIN_PLUGIN_DIR:
        return BUILTIN_PLUGIN_DIR[uuid]
    return uuid + ".sdPlugin"


class Context:
    """Was Aktionen von außen brauchen."""

    def __init__(self, controller, cfg, vin, sound, swap_cmd_ctrl=False):
        self.ctrl = controller
        self.cfg = cfg
        self.vin = vin
        self.sound = sound
        self.swap_cmd_ctrl = swap_cmd_ctrl


# ==========================================================================
class Action:
    supported = True
    needs_tick = False   # tick() jede Sekunde aufrufen
    executes_on_up = False
    inline = False

    def __init__(self, data: dict, page, ctx: Context):
        self.data = data or {}
        self.uuid = self.data.get("UUID", "")
        self.page = page
        self.ctx = ctx
        self.settings = self.data.get("Settings") or {}
        self.states = self.data.get("States") or [{}]
        if not self.states:
            self.states = [{}]
        self.state = int(self.data.get("State") or 0)
        if self.state >= len(self.states):
            self.state = 0
        self.title_override = None
        self.image_override = None
        self.slot = None  # wird vom Controller gesetzt: ("key", idx) / ("dial", idx)

    # ---------------------------------------------------------------- Info
    @property
    def name(self):
        return self.data.get("Name") or self.uuid.split(".")[-1]

    def state_dict(self):
        return self.states[self.state] if self.state < len(self.states) else {}

    def title(self):
        if self.title_override is not None:
            return self.title_override
        return self.state_dict().get("Title") or ""

    def default_icon(self):
        icons = DEFAULT_ICONS.get(self.uuid)
        if icons:
            return render.builtin_icon_path(icons[min(self.state, len(icons) - 1)], plugin_dir_for(self.uuid))
        return None

    def image_path(self):
        img = self.state_dict().get("Image")
        if img and self.page is not None:
            p = self.page.image_path(img)
            if p:
                return p
        return self.default_icon()

    def frames(self, size):
        """Liste (PIL-Bild, Dauer ms). Leere Liste = kein Bild."""
        if self.image_override is not None:
            return [(self.image_override, 0)]
        fr = render.load_frames(self.image_path(), *size)
        if fr:
            return fr
        return [(render.fallback_icon(size[0], size[1], self.fallback_label()), 0)]

    def fallback_label(self):
        return self.name

    # Dial-Darstellung (Touchstreifen)
    def encoder_icon_path(self):
        enc = self.data.get("Encoder") or {}
        if enc.get("IconVisible") is False:
            return None
        if enc.get("Icon") and self.page is not None:
            p = self.page.image_path(enc["Icon"])
            if p:
                return p
        return self.image_path()

    def encoder_background_path(self):
        enc = self.data.get("Encoder") or {}
        bg = enc.get("background") or enc.get("Background")
        return self.page.image_path(bg) if bg and self.page is not None else None

    def feedback(self):
        """Werte für den Touchstreifen: dict(title, value, indicator)."""
        return {"title": self.title() or self.name, "value": "", "indicator": None}

    # ------------------------------------------------------------- Events
    def key_down(self):
        pass

    def key_up(self, held_ms=0):
        pass

    def execute(self):
        """Einmal auslösen (z.B. innerhalb einer Multi-Action)."""
        self.key_down()
        self.key_up(0)

    def dial_rotate(self, ticks):
        pass

    def dial_down(self):
        self.key_down()

    def dial_up(self, held_ms=0):
        self.key_up(held_ms)

    def touch(self, long=False):
        self.dial_down()
        self.dial_up(600 if long else 0)

    def tick(self) -> bool:
        return False

    def will_appear(self):
        pass

    def will_disappear(self):
        pass

    # -------------------------------------------------------------- Helfer
    def redraw(self):
        if self.ctx and self.ctx.ctrl:
            self.ctx.ctrl.redraw_action(self)

    def alert(self):
        if self.ctx and self.ctx.ctrl:
            self.ctx.ctrl.show_overlay(self, "alert")

    def ok(self):
        if self.ctx and self.ctx.ctrl:
            self.ctx.ctrl.show_overlay(self, "ok")

    def set_state(self, s):
        self.state = s % max(1, len(self.states))
        self.redraw()


# ==========================================================================
# System
# ==========================================================================
class Hotkey(Action):
    # Hotkeys[0]=Taste/Linksdrehung, [1]=Rechtsdrehung, [2]=Drücken, [3]=Touch (Dials)
    def _hk(self, i):
        hks = self.settings.get("Hotkeys") or []
        if i < len(hks) and not self.ctx.vin.hotkey_empty(hks[i]):
            return hks[i]
        return None

    def _send(self, i):
        hk = self._hk(i)
        if hk is None:
            return False
        codes = self.ctx.vin.hotkey_codes(hk, self.ctx.swap_cmd_ctrl)
        if not codes:
            self.alert()
            return False
        self.ctx.vin.tap(codes)
        return True

    def key_down(self):
        if self.slot and self.slot[0] == "dial":
            return
        if not self._send(0):
            if self._hk(0) is None:
                self.alert()

    def dial_rotate(self, ticks):
        idx = 1 if ticks > 0 else 0
        for _i in range(min(abs(ticks), 20)):
            self._send(idx)

    def dial_down(self):
        self._send(2)

    def dial_up(self, held_ms=0):
        pass

    def touch(self, long=False):
        if not self._send(3):
            self._send(2)


class HotkeySwitch(Action):
    def key_down(self):
        hks = self.settings.get("Hotkeys") or []
        hk = hks[self.state] if self.state < len(hks) else None
        if hk and not self.ctx.vin.hotkey_empty(hk):
            self.ctx.vin.tap(self.ctx.vin.hotkey_codes(hk, self.ctx.swap_cmd_ctrl))
        self.set_state(self.state + 1)


class Text(Action):
    def key_down(self):
        text = self.settings.get("pastedText") or ""
        enter = bool(self.settings.get("isSendingEnter"))
        vin = self.ctx.vin
        if text:
            if self.settings.get("isTypingMode") and vin.can_type(text):
                vin.type_text(text)
            else:
                old = system.clipboard_get()
                if system.clipboard_set(text):
                    time.sleep(0.08)
                    vin.tap([vin.key_by_name("KEY_LEFTCTRL"), vin.key_by_name("KEY_V")])
                    if old is not None:
                        threading.Timer(0.6, system.clipboard_set, args=(old,)).start()
                elif vin.can_type(text):
                    vin.type_text(text)
                else:
                    self.alert()
                    return
        if enter:
            time.sleep(0.05)
            vin.tap([vin.key_by_name("KEY_ENTER")])


class Multimedia(Action):
    KEYS = {0: "KEY_PREVIOUSSONG", 1: "KEY_PLAYPAUSE", 2: "KEY_NEXTSONG", 3: "KEY_STOPCD", 4: "KEY_MUTE",
            5: "KEY_VOLUMEUP", 6: "KEY_VOLUMEDOWN", 7: "KEY_BRIGHTNESSDOWN", 8: "KEY_BRIGHTNESSUP",
            9: "KEY_EJECTCD", 10: "KEY_KBDILLUMDOWN", 11: "KEY_KBDILLUMUP", 12: "KEY_KBDILLUMTOGGLE",
            13: "KEY_FASTFORWARD", 14: "KEY_REWIND"}
    PLAYERCTL = {0: "previous", 1: "play-pause", 2: "next", 3: "stop", 13: "position 10+", 14: "position 10-"}

    @property
    def idx(self):
        v = self.settings.get("actionIdx")
        if v is None:
            return 18 if self.slot and self.slot[0] == "dial" else 1
        return int(v)

    def default_icon(self):
        name = MEDIA_ICONS.get(self.idx)
        if name:
            p = render.builtin_icon_path("Images/" + name, "com.elgato.streamdeck.system.multimedia.sdPlugin")
            if p:
                return p
        return super().default_icon()

    def fallback_label(self):
        return {0: "⏮", 1: "⏯", 2: "⏭", 3: "⏹", 4: "Mute", 5: "Vol +", 6: "Vol −", 13: "⏩", 14: "⏪",
                18: "Volume"}.get(self.idx, "Media")

    def key_down(self):
        i = self.idx
        if i == 18:
            system.toggle_mute()
            self.redraw()
            return
        if i in (13, 14) and system.media(self.PLAYERCTL[i]):
            return
        if self.ctx.vin.available and i in self.KEYS:
            self.ctx.vin.tap([self.ctx.vin.key_by_name(self.KEYS[i])])
        elif i in self.PLAYERCTL:
            system.media(self.PLAYERCTL[i])
        elif i == 4:
            system.toggle_mute()
        elif i in (5, 6):
            system.change_volume(5 if i == 5 else -5)
        else:
            self.alert()

    def dial_rotate(self, ticks):
        if self.idx in (18, 4, 5, 6):
            system.change_volume(2 * ticks)
        elif self.idx in (0, 1, 2, 13, 14):
            system.media(f"position {abs(ticks) * 5}{'+' if ticks > 0 else '-'}")
        self.redraw()

    def dial_down(self):
        system.toggle_mute() if self.idx in (18, 4, 5, 6) else self.key_down()
        self.redraw()

    def dial_up(self, held_ms=0):
        pass

    needs_tick = True

    def tick(self):
        return bool(self.slot and self.slot[0] == "dial")

    def feedback(self):
        fb = super().feedback()
        if self.idx in (18, 4, 5, 6):
            vol, muted = system.get_volume()
            fb["value"] = _("Muted") if muted else f"{vol}%"
            fb["indicator"] = 0 if muted else vol
        return fb


class KeyBrightness(Action):
    LEVELS = {2: 10, 3: 25, 4: 50, 5: 75, 6: 100}

    @property
    def idx(self):
        return int(self.settings.get("actionIdx") or 0)

    def default_icon(self):
        name = BRIGHTNESS_ICONS.get(self.idx)
        p = render.builtin_icon_path("Images/" + name,
                                     "com.elgato.streamdeck.system.keybrightness.sdPlugin") if name else None
        return p or super().default_icon()

    def fallback_label(self):
        return _("Bright +") if self.idx == 0 else _("Bright −") if self.idx == 1 else _("Brightness")

    def key_down(self):
        c = self.ctx.ctrl
        if self.idx == 0:
            c.change_brightness(+10)
        elif self.idx == 1:
            c.change_brightness(-10)
        else:
            c.set_brightness(self.LEVELS.get(self.idx, 100))

    def dial_rotate(self, ticks):
        self.ctx.ctrl.change_brightness(5 * ticks)
        self.redraw()

    def dial_down(self):
        c = self.ctx.ctrl
        c.set_brightness(100 if c.brightness < 50 else 30)
        self.redraw()

    def dial_up(self, held_ms=0):
        pass

    def feedback(self):
        fb = super().feedback()
        fb["value"] = f"{self.ctx.ctrl.brightness}%"
        fb["indicator"] = self.ctx.ctrl.brightness
        return fb


class MouseWheel(Action):
    def dial_rotate(self, ticks):
        horizontal = bool(self.settings.get("horizontal") or self.settings.get("direction") == "horizontal")
        inv = -1 if self.settings.get("invert") else 1
        self.ctx.vin.scroll(-ticks * inv, horizontal)


class Open(Action):
    def key_down(self):
        path = self.settings.get("path") or self.settings.get("openInBrowser") or ""
        if not system.open_target(path, self.ctx.cfg):
            self.alert()


class OpenApp(Action):
    executes_on_up = True

    def _prog(self):
        s = self.settings
        target = s.get("exec") or s.get("bundle_path") or s.get("bundle_id") or ""
        if target and not system.is_windows_path(target) and system.have(target.split()[0]):
            return ("cmd", target + ((" " + s["args"]) if s.get("args") else ""))
        prog = system.resolve_program(target, self.ctx.cfg.get("app_map"), s.get("app_name", ""))
        if prog and prog[0] == "cmd" and s.get("args"):
            prog = ("cmd", prog[1] + " " + s["args"])
        return prog

    def key_down(self):
        pass

    def key_up(self, held_ms=0):
        s = self.settings
        prog = self._prog()
        names = system.process_names_for(prog, s.get("exec") or s.get("bundle_path") or "")
        long_ms = self.ctx.cfg.get("long_press_ms", 500)
        if held_ms >= long_ms and s.get("long_press") in ("quit", "close", "kill", "force_quit"):
            if not system.kill_processes(names):
                self.alert()
            return
        if not prog:
            log.warning(_("Program '{name}' not found – add it under Settings → Connections → Windows profiles").format(
                name=s.get("app_name") or s.get("exec")))
            self.alert()
            return
        if s.get("bring_to_front", True) and system.is_running(names) and system.focus_window(names):
            return
        system.launch(prog)


class CloseApp(OpenApp):
    def key_up(self, held_ms=0):
        s = self.settings
        prog = self._prog()
        names = system.process_names_for(prog, s.get("exec") or s.get("path") or s.get("bundle_path") or "")
        if s.get("app_name"):
            names.add(s["app_name"].lower().replace(" ", ""))
        if not system.kill_processes(names):
            self.alert()


class Website(Action):
    def key_down(self):
        url = (self.settings.get("path") or "").strip()
        if not url:
            self.alert()
            return
        if "://" not in url:
            url = "https://" + url
        browser = self.settings.get("browser") or ""
        if browser:
            prog = system.resolve_program(browser, self.ctx.cfg.get("app_map"))
            if prog and prog[0] == "cmd":
                system.run_bg(prog[1] + " " + url)
                return
        system.run_bg(["xdg-open", url])


class Sleep(Action):
    def key_down(self):
        self.ctx.ctrl.sleep_device()


# ==========================================================================
# Navigation
# ==========================================================================
class NextPage(Action):
    inline = True  # Navigation sofort und in Reihenfolge ausführen

    def key_down(self):
        self.ctx.ctrl.next_page()


class PreviousPage(Action):
    inline = True  # Navigation sofort und in Reihenfolge ausführen

    def key_down(self):
        self.ctx.ctrl.previous_page()


class GotoPage(Action):
    inline = True  # Navigation sofort und in Reihenfolge ausführen

    def key_down(self):
        self.ctx.ctrl.goto_page(int(self.settings.get("PageIndex") or 1) - 1)


class PageIndicator(Action):
    def title(self):
        if self.title_override is not None:
            return self.title_override
        t = self.state_dict().get("Title")
        return t if t else str(self.ctx.ctrl.page_index + 1)

    def frames(self, size):
        if self.state_dict().get("Image"):
            return super().frames(size)
        n = len(self.ctx.ctrl.profile.page_ids) if self.ctx.ctrl.profile else 1
        self.title_override = ""
        return [(render.text_key(size, str(self.ctx.ctrl.page_index + 1), _("of {n}").format(n=n)), 0)]


class OpenChild(Action):
    inline = True  # Navigation sofort und in Reihenfolge ausführen

    def key_down(self):
        self.ctx.ctrl.open_folder(self.settings.get("ProfileUUID"))

    def fallback_label(self):
        return "📁 " + (self.title() or _("Folder"))


class BackToParent(Action):
    inline = True  # Navigation sofort und in Reihenfolge ausführen

    def key_down(self):
        self.ctx.ctrl.back_to_parent()

    def fallback_label(self):
        return "↩ " + _("Back")


class SwitchProfile(Action):
    inline = True  # Navigation sofort und in Reihenfolge ausführen

    def key_down(self):
        pid = self.settings.get("ProfileUUID") or self.settings.get("profileUUID") or ""
        if pid:
            if not self.ctx.ctrl.switch_profile(pid):
                self.alert()
        else:
            direction = -1 if str(self.settings.get("direction", "")).lower() in ("previous", "prev", "-1") else 1
            self.ctx.ctrl.rotate_profile(direction)


class Pagination(Action):
    inline = True  # Navigation sofort und in Reihenfolge ausführen

    """Neo-Infobar: Seitenanzeige. Auf einer Taste: Seite weiter."""

    def key_down(self):
        self.ctx.ctrl.next_page()


class DigitalTime(Action):
    needs_tick = True

    def _fmt(self):
        twelve = bool(self.settings.get("standardTime")) or self.settings.get("24HourFormat") is False
        now = datetime.now()
        loc = self.settings.get("location") or ""
        if loc:
            try:
                from zoneinfo import ZoneInfo
                now = datetime.now(ZoneInfo(loc))
            except Exception:  # noqa: BLE001
                pass
        t = now.strftime("%I:%M %p").lstrip("0") if twelve else now.strftime("%H:%M")
        return t, now.strftime("%d.%m.%Y")

    def frames(self, size):
        t, d = self._fmt()
        light = self.settings.get("theme") == "light"
        return [(render.text_key(size, t, d, bg=(235, 235, 235) if light else (0, 0, 0),
                                 fg=(0, 0, 0) if light else (255, 255, 255)), 0)]

    def tick(self):
        t = self._fmt()
        changed = t != getattr(self, "_last", None)
        self._last = t
        return changed


class Timer(Action):
    needs_tick = True
    executes_on_up = True

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.total = self._duration()
        self.remaining = self.total
        self.running = False
        self.end_at = 0.0
        self.finished = False

    def _duration(self):
        d = self.settings.get("duration") or self.settings.get("time") or 300
        try:
            if isinstance(d, str) and ":" in d:
                parts = [int(p) for p in d.split(":")]
                sec = 0
                for p in parts:
                    sec = sec * 60 + p
                return float(sec)
            d = float(d)
            return d / 1000.0 if d > 86400 else d
        except (TypeError, ValueError):
            return 300.0

    def key_up(self, held_ms=0):
        if held_ms >= self.ctx.cfg.get("long_press_ms", 500) or self.finished:
            self.running, self.finished, self.remaining = False, False, self.total
            self.ctx.sound.stop(("timer", id(self)))
        elif self.running:
            self.remaining = max(0.0, self.end_at - time.time())
            self.running = False
        else:
            self.end_at = time.time() + self.remaining
            self.running = True
        self.redraw()

    def tick(self):
        if not self.running:
            return self.finished
        self.remaining = max(0.0, self.end_at - time.time())
        if self.remaining <= 0 and not self.finished:
            self.running, self.finished = False, True
            alarm = self.settings.get("alarm") or self.settings.get("lastUserFile") or ""
            path = system.map_path(alarm, self.ctx.cfg.get("path_map")) if alarm else ""
            import os
            if path and os.path.exists(path):
                self.ctx.sound.play(("timer", id(self)), path)
            else:
                for _i in range(3):
                    print("\a", end="", flush=True)
        return True

    def frames(self, size):
        if self.finished:
            blink = int(time.time() * 2) % 2
            return [(render.text_key(size, "00:00", pgettext("timer", "done"), bg=(200, 30, 30) if blink else (0, 0, 0)), 0)]
        r = int(round(self.remaining))
        h, rem = divmod(r, 3600)
        m, s = divmod(rem, 60)
        txt = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        sub = pgettext("timer", "running") if self.running else \
            pgettext("timer", "paused") if r < self.total else "Timer"
        return [(render.text_key(size, txt, sub), 0)]


# ==========================================================================
# Soundboard
# ==========================================================================
class PlayAudio(Action):
    # actionType: 0 Abspielen/Überlappen, 1 Abspielen/Stopp, 2 Abspielen/Neustart, 3 Schleife/Stopp
    def _path(self):
        res = (self.data.get("Resources") or {}).get("file")
        prof = self.ctx.ctrl.profile if self.ctx.ctrl else None
        if res and prof:
            p = prof.resource_path(res)
            if p:
                return str(p)
        import os
        p = system.map_path(self.settings.get("path") or "", self.ctx.cfg.get("path_map"))
        return p if p and os.path.exists(p) else None

    def key_down(self):
        path = self._path()
        if not path:
            log.warning(_("Audio file not found: {path}").format(path=self.settings.get("path")))
            self.alert()
            return
        key = ("sb", id(self))
        mode = int(self.settings.get("actionType") or 0)
        playing = self.ctx.sound.is_playing(key)
        if playing and mode in (1, 3):
            self.ctx.sound.stop(key)
            return
        if playing and mode == 2:
            self.ctx.sound.stop(key)
        out = self.settings.get("outputType") or self.ctx.cfg.get("soundboard_output") or ""
        if out and ("{" in out or "\\" in out):  # Windows-Geräte-ID -> Standardausgabe
            out = self.ctx.cfg.get("soundboard_output") or ""
        self.ctx.sound.play(key, path, self.settings.get("volume", 100), out, loop=(mode == 3))


class StopAudio(Action):
    def key_down(self):
        self.ctx.sound.stop()


# ==========================================================================
# Zusammengesetzte Aktionen
# ==========================================================================
def _children(data):
    """Liste der Gruppen (Listen von Aktions-Dicts) einer Multi-Action."""
    groups = []
    for entry in data.get("Actions") or []:
        if isinstance(entry, dict) and "UUID" in entry:
            groups.append([entry])
        elif isinstance(entry, dict):
            groups.append([a for a in entry.get("Actions") or [] if isinstance(a, dict) and a.get("UUID")])
    return groups


class MultiAction(Action):
    executes_on_up = False

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.groups = [[create_action(d, self.page, self.ctx) for d in g] for g in _children(self.data)]
        self._running = False

    def _run(self, steps):
        if self._running:
            return
        self._running = True
        # Fortschritt auf der Taste (Einstellungen › Stil › Multi-Aktion), nur bei mehreren Schritten
        ctrl = getattr(self.ctx, "ctrl", None)
        show = len(steps) > 1 and ctrl is not None and self.slot is not None and \
            (self.ctx.cfg.get("multi_progress", True) if self.ctx.cfg is not None else True)

        def progress(done):
            if show:
                try:
                    ctrl.show_overlay(self, ("progress", done / len(steps), min(done + 1, len(steps)), len(steps)),
                                      seconds=600)
                except Exception:  # noqa: BLE001
                    pass

        def worker():
            try:
                progress(0)
                for i, st in enumerate(steps, start=1):
                    if isinstance(st, Delay):
                        time.sleep(st.ms / 1000.0)
                        progress(i)
                        continue
                    try:
                        st.execute()
                    except Exception as e:  # noqa: BLE001
                        log.warning(_("Multi Action step {action} failed: {error}").format(action=st.uuid, error=e))
                    progress(i)
                    time.sleep(0.03)
            finally:
                self._running = False
                if show:
                    ctrl.overlays.pop(id(self), None)
                    try:
                        ctrl.redraw_action(self)
                    except Exception:  # noqa: BLE001
                        pass
        threading.Thread(target=worker, daemon=True).start()

    def key_down(self):
        steps = [a for g in self.groups for a in g] if self.uuid.endswith("routine") else (self.groups[0] if self.groups else [])
        self._run(steps)

    def execute(self):
        self.key_down()


class MultiActionSwitch(MultiAction):
    def key_down(self):
        if self.groups:
            self._run(self.groups[min(self.state, len(self.groups) - 1)])
        self.set_state(self.state + 1)


class RandomAction(MultiAction):
    def key_down(self):
        pool = [a for g in self.groups for a in g if not isinstance(a, Delay)]
        if pool:
            self._run([random.choice(pool)])


class Delay(Action):
    @property
    def ms(self):
        try:
            return float(self.settings.get("delay") or 0)
        except (TypeError, ValueError):
            return 0.0


class KeyLogic(Action):
    """Key Logic: [Tippen, Doppeltippen, Halten] -> je eine Aktion."""
    executes_on_up = True

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        slots = []
        for entry in self.data.get("Actions") or []:
            if isinstance(entry, dict) and entry.get("UUID"):
                slots.append(create_action(entry, self.page, self.ctx))
            else:
                slots.append(None)
        while len(slots) < 3:
            slots.append(None)
        self.single, self.double, self.hold = slots[:3]
        self._pending = None

    def _fire(self, act):
        if act is not None:
            threading.Thread(target=act.execute, daemon=True).start()

    def key_up(self, held_ms=0):
        cfg = self.ctx.cfg
        if held_ms >= cfg.get("long_press_ms", 500) and self.hold is not None:
            self._fire(self.hold)
            return
        if self.double is None:
            self._fire(self.single)
            return
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None
            self._fire(self.double)
            return
        self._pending = threading.Timer(cfg.get("double_press_ms", 300) / 1000.0, self._single)
        self._pending.start()

    def _single(self):
        self._pending = None
        self._fire(self.single)


class ActionWheel(Action):
    """Action Wheel (keys.stack): Drehen wählt, Drücken führt aus."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.items = [create_action(d, self.page, self.ctx) for g in _children(self.data) for d in g]
        self.idx = int(self.settings.get("CurrentIdx") or 0)
        if self.items:
            self.idx %= len(self.items)
        if self.settings.get("InvertRotation"):
            self._inv = -1
        else:
            self._inv = 1

    def current(self):
        return self.items[self.idx] if self.items else None

    def dial_rotate(self, ticks):
        if self.items:
            self.idx = (self.idx + ticks * self._inv) % len(self.items)
            self.redraw()

    def dial_down(self):
        c = self.current()
        if c:
            threading.Thread(target=c.execute, daemon=True).start()

    def dial_up(self, held_ms=0):
        pass

    def encoder_icon_path(self):
        c = self.current()
        return c.image_path() if c else super().encoder_icon_path()

    def feedback(self):
        c = self.current()
        fb = super().feedback()
        if c:
            fb["title"] = c.title() or c.name
            fb["value"] = f"{self.idx + 1}/{len(self.items)}"
        return fb


class DialStack(Action):
    """Dial Stack: mehrere Dial-Aktionen; Drücken wechselt zur nächsten."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.items = [create_action(d, self.page, self.ctx) for g in _children(self.data) for d in g]
        self.idx = int(self.settings.get("CurrentIdx") or 0)
        if self.items:
            self.idx %= len(self.items)

    def _set_slots(self):
        for it in self.items:
            it.slot = self.slot

    def current(self):
        self._set_slots()
        return self.items[self.idx] if self.items else None

    def dial_rotate(self, ticks):
        c = self.current()
        if c:
            c.dial_rotate(ticks)
            self.redraw()

    def dial_down(self):
        if self.items:
            self.idx = (self.idx + 1) % len(self.items)
            self.redraw()

    def dial_up(self, held_ms=0):
        pass

    def touch(self, long=False):
        c = self.current()
        if c:
            c.touch(long)

    needs_tick = True

    def tick(self):
        c = self.current()
        return bool(c and c.needs_tick and c.tick())

    def encoder_icon_path(self):
        c = self.current()
        return c.encoder_icon_path() if c else None

    def feedback(self):
        c = self.current()
        if not c:
            return super().feedback()
        fb = c.feedback()
        if self.settings.get("ShowStackIcon", True) and len(self.items) > 1:
            fb["title"] = f"{fb.get('title', '')}  {self.idx + 1}/{len(self.items)}"
        return fb


class ActionTrigger(Action):
    """Action Trigger (keys.adaptor): [Links drehen, Drücken, Rechts drehen]."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        slots = []
        for entry in self.data.get("Actions") or []:
            slots.append(create_action(entry, self.page, self.ctx) if isinstance(entry, dict) and entry.get("UUID") else None)
        while len(slots) < 3:
            slots.append(None)
        self.left, self.push, self.right = slots[:3]

    def dial_rotate(self, ticks):
        act = self.right if ticks > 0 else self.left
        if act:
            for _i in range(min(abs(ticks), 10)):
                act.execute()

    def dial_down(self):
        if self.push:
            self.push.execute()

    def dial_up(self, held_ms=0):
        pass


# ==========================================================================
# Elgato Volume Controller (wird mit der Stream-Deck-Software installiert)
# ==========================================================================
class VolumeDevice(Action):
    """com.elgato.volume-controller.output-/input-device-control und App-Lautstärke."""
    needs_tick = True

    @property
    def kind(self):
        return "source" if "input-device" in self.uuid else "sink"

    @property
    def app(self):
        s = self.settings
        return s.get("appName") or s.get("app") or s.get("processName") or s.get("process") or \
            s.get("executable") or ""

    def _target(self):
        return system.find_audio_device(self.kind, self.settings.get("friendlyName", ""),
                                        self.settings.get("deviceId", "default"))

    def _step(self):
        try:
            return int(float(self.settings.get("volumeStep") or 5))
        except ValueError:
            return 5

    def _vol(self):
        if self.app:
            return system.app_volume(self.app)
        return system.device_volume(self.kind, self._target())

    def _change(self, delta):
        if self.app:
            system.app_change(self.app, delta=delta)
        else:
            system.device_change(self.kind, self._target(), delta)

    def _mute(self, mode="toggle"):
        if self.app:
            system.app_change(self.app, mute=mode)
        else:
            system.device_mute(self.kind, self._target(), mode)

    def key_down(self):
        if self.slot and self.slot[0] == "dial":
            return
        act = str(self.settings.get("action") or "mute").lower()
        if act in ("mute", "togglemute", "toggle"):
            self._mute()
        elif act in ("unmute",):
            self._mute("0")
        elif act in ("up", "increase", "volumeup"):
            self._change(self._step())
        elif act in ("down", "decrease", "volumedown"):
            self._change(-self._step())
        elif act in ("set", "setvolume", "volume"):
            v = int(float(self.settings.get("volume") or 50))
            if self.app:
                system.app_change(self.app, set_to=v)
            else:
                system.device_set(self.kind, self._target(), v)
        else:
            self._mute()
        self.redraw()

    def dial_rotate(self, ticks):
        self._change(self._step() * ticks)
        self.redraw()

    def dial_down(self):
        self._mute()
        self.redraw()

    def dial_up(self, held_ms=0):
        pass

    def touch(self, long=False):
        self.dial_down()

    def tick(self):
        if not self.slot or self.slot[0] != "dial":
            return False
        v = self._vol()
        changed = v != getattr(self, "_last", None)
        self._last = v
        return changed

    def fallback_label(self):
        return _("Microphone") if self.kind == "source" else _("Volume")

    def feedback(self):
        vol, muted = self._vol()
        name = self.title() or self.settings.get("friendlyName") or self.app or \
            (_("Microphone") if self.kind == "source" else _("Output"))
        return {"title": name, "value": "–" if vol is None else (_("Muted") if muted else f"{vol}%"),
                "indicator": 0 if muted or vol is None else vol}


class NoOp(Action):
    """Aktionen ohne Linux-Entsprechung (Virtual Stream Deck, Action Bar)."""

    def key_down(self):
        log.info(_("Action {uuid} does nothing on Linux").format(uuid=self.uuid))


class PluginAction(Action):
    """Aktion eines Drittanbieter-Plugins – Anzeige des gespeicherten Bildes, Ausführung im Plugin-Host."""
    supported = False

    def default_icon(self):
        from .plugins import plugin_for_action
        info = plugin_for_action(self.uuid)
        return info.action_icon(self.uuid, self.state) if info else None

    def feedback(self):
        fb = super().feedback()
        for k, v in (getattr(self, "sdk_feedback", None) or {}).items():
            if isinstance(v, dict):
                v = v.get("value")
            if k == "indicator":
                try:
                    fb["indicator"] = float(v)
                except (TypeError, ValueError):
                    pass
            elif k in ("title", "value"):
                fb[k] = "" if v is None else str(v)
        return fb

    def _host(self):
        return getattr(self.ctx.ctrl, "plugin_host", None) if self.ctx and self.ctx.ctrl else None

    def key_up(self, held_ms=0):
        host = self._host()
        if host:
            host.key_up(self)

    def fallback_label(self):
        if self.title():
            return ""
        plugin = (self.data.get("Plugin") or {}).get("Name") or self.uuid.rsplit(".", 1)[0].split(".")[-1]
        return f"{plugin}: {self.name}"

    def key_down(self):
        handled = False
        host = self._host()
        if host:
            handled = host.key_down(self)
        if not handled:
            log.info(_("Plugin action {uuid} not available yet").format(uuid=self.uuid))
            self.alert()


REGISTRY = {
    "com.elgato.streamdeck.system.hotkey": Hotkey,
    "com.elgato.streamdeck.system.hotkeyswitch": HotkeySwitch,
    "com.elgato.streamdeck.system.text": Text,
    "com.elgato.streamdeck.system.multimedia": Multimedia,
    "com.elgato.streamdeck.system.keybrightness": KeyBrightness,
    "com.elgato.streamdeck.system.mouse-wheel": MouseWheel,
    "com.elgato.streamdeck.system.open": Open,
    "com.elgato.streamdeck.system.openapp": OpenApp,
    "com.elgato.streamdeck.system.close": CloseApp,
    "com.elgato.streamdeck.system.website": Website,
    "com.elgato.streamdeck.system.sleep": Sleep,
    "com.elgato.streamdeck.system.timer": Timer,
    "com.elgato.streamdeck.timer": Timer,
    "com.elgato.streamdeck.system.digitaltime": DigitalTime,
    "com.elgato.streamdeck.system.pagination": Pagination,
    "com.elgato.streamdeck.system.vsdtoggle": NoOp,
    "com.elgato.streamdeck.vsdtoggle": NoOp,
    "com.elgato.streamdeck.system.actionbar": NoOp,
    "com.elgato.streamdeck.system.assert": NoOp,
    "com.elgato.streamdeck.system.debug": NoOp,
    "com.elgato.streamdeck.page.next": NextPage,
    "com.elgato.streamdeck.page.previous": PreviousPage,
    "com.elgato.streamdeck.page.goto": GotoPage,
    "com.elgato.streamdeck.page.indicator": PageIndicator,
    "com.elgato.streamdeck.profile.openchild": OpenChild,
    "com.elgato.streamdeck.profile.backtoparent": BackToParent,
    "com.elgato.streamdeck.profile.rotate": SwitchProfile,
    "com.elgato.streamdeck.soundboard.playaudio": PlayAudio,
    "com.elgato.streamdeck.soundboard.stopaudioplay": StopAudio,
    "com.elgato.streamdeck.multiactions.routine": MultiAction,
    "com.elgato.streamdeck.multiactions.routine2": MultiActionSwitch,
    "com.elgato.streamdeck.multiactions.random": RandomAction,
    "com.elgato.streamdeck.multiactions.delay": Delay,
    "com.elgato.streamdeck.keys.logic": KeyLogic,
    "com.elgato.streamdeck.keys.stack": ActionWheel,
    "com.elgato.streamdeck.keys.adaptor": ActionTrigger,
    "com.elgato.streamdeck.dial.stack": DialStack,
}


def create_action(data, page, ctx) -> Action:
    uuid = (data or {}).get("UUID", "")
    cls = REGISTRY.get(uuid)
    if cls is None:
        cls = _plugin_class(uuid)
    try:
        return cls(data, page, ctx)
    except Exception as e:  # noqa: BLE001 - kaputte Einstellungen dürfen das Gerät nicht blockieren
        log.warning(_("Cannot load action {uuid}: {error}").format(uuid=(data or {}).get("UUID"), error=e))
        return PluginAction(data, page, ctx)


def _plugin_class(uuid: str):
    """Aktionen außerhalb der Elgato-Grundausstattung: installiertes SDK-Plugin vor Linux-Umsetzung."""
    if uuid.startswith("com.elgato.volume-controller."):
        return VolumeDevice
    from . import native, plugins
    try:
        if plugins.plugin_for_action(uuid):
            return PluginAction
    except OSError:
        pass
    return native.lookup(uuid) or PluginAction


def is_supported(uuid: str) -> bool:
    """True, wenn die Aktion unter Linux ausgeführt werden kann (eingebaut, Linux-Umsetzung oder SDK-Plugin)."""
    if is_builtin(uuid):
        return True
    from . import plugins
    info = plugins.plugin_for_action(uuid)
    return bool(info and info.runnable)


def is_builtin(uuid: str) -> bool:
    """True, wenn FreeDeck die Aktion selbst ausführt (eingebaut oder Linux-Umsetzung eines Plugins)."""
    if uuid in REGISTRY or uuid.startswith("com.elgato.volume-controller."):
        return True
    from . import native
    return native.lookup(uuid) is not None
