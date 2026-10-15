"""Linux-Umsetzungen häufiger Stream-Deck-Plugins.

  * OBS Studio      – über obs-websocket (obs.py)
  * Spotify/Medien  – über MPRIS (playerctl); funktioniert mit jedem Player
  * Discord         – über die lokale RPC-Schnittstelle (discord.py)
  * Wave Link       – über den PipeWire-Mixer (mixer.py) und wavemic.py

Eigene Aktionen haben UUIDs unter "com.lindeck.". Aktionen der Elgato-/
Drittanbieter-Plugins aus importierten Profilen werden anhand ihrer UUID
zugeordnet (z. B. com.elgato.obsstudio.record → Aufnahme). Deren
Einstellungen sind nicht dokumentiert; LinDeck liest die üblichen Felder
und nutzt sonst den Tastentitel (z. B. als Szenenname).
"""
import logging
import time

from . import actions as A
from . import render, system
from .i18n import _, pgettext

log = logging.getLogger("lindeck.native")

ON_BG, OFF_BG, IDLE_BG, ERR_BG = (22, 120, 64), (40, 40, 46), (40, 40, 46), (110, 30, 30)


def _first(settings, *keys, default=""):
    for k in keys:
        v = settings.get(k)
        if isinstance(v, dict):
            v = v.get("name") or v.get("value") or v.get("id")
        if v not in (None, ""):
            return v
    return default


class NativeAction(A.Action):
    """Gemeinsame Darstellung: eigenes Icon, sonst Text mit Status; zweiter Zustand = aktiv."""
    needs_tick = True
    label = ""

    def active(self):
        return None

    def status_text(self):
        return ""

    def main_text(self):
        return self.label or self.name

    def _has_image(self):
        return any(st.get("Image") for st in self.states if isinstance(st, dict))

    def preview_text(self):
        return self.main_text()

    def frames(self, size):
        if self.image_override is not None or self._has_image():
            return super().frames(size)
        if getattr(self.ctx.ctrl, "preview", False):
            # Editor-Vorschau: keine Verbindungen zu OBS/Discord/PipeWire aufbauen
            return [(render.text_key(size, _fit(self.preview_text()), "", bg=OFF_BG), 0)]
        act = self.active()
        bg = ON_BG if act else OFF_BG
        sub = "" if self.title() else self.status_text()
        return [(render.text_key(size, _fit(self.main_text()), sub, bg=bg), 0)]

    def _sync_state(self):
        act = self.active()
        if act is not None and len(self.states) > 1:
            self.state = 1 if act else 0

    def tick(self):
        try:
            self._sync_state()
            snap = (self.active(), self.status_text(), self.state, self.main_text())
        except Exception:  # noqa: BLE001
            snap = None
        changed = snap != getattr(self, "_snap", None)
        self._snap = snap
        return changed

    def run(self, fn, *args):
        try:
            fn(*args)
        except Exception as e:  # noqa: BLE001 - Fehler anzeigen statt abstürzen
            log.warning("%s: %s", self.name, e)
            self.alert()
            return False
        self.tick()
        self.redraw()
        return True

    def feedback(self):
        fb = super().feedback()
        fb["value"] = self.status_text()
        return fb


def _fit(text, n=9):
    text = str(text or "")
    return text if len(text) <= n else text[: n - 1] + "…"


# ==========================================================================
# OBS
# ==========================================================================
class OBSAction(NativeAction):
    kind = ""

    @property
    def obs(self):
        from . import obs
        return obs.client(self.ctx.cfg)

    def will_appear(self):
        self.obs  # Verbindung aufbauen

    def _scene(self):
        return _first(self.settings, "scene", "sceneName", "scene_name", "sceneId", "SceneName",
                      default=(self.states[0].get("Title") if self.states else "") or "")

    def _source(self):
        return _first(self.settings, "source", "sourceName", "source_name", "sceneItem", "item", "input",
                      "inputName", default=(self.states[0].get("Title") if self.states else "") or "")

    def _mode(self):
        return str(_first(self.settings, "mode", "action", default="toggle")).lower()

    def active(self):
        o = self.obs
        if not o.connected:
            return None
        k, s = self.kind, o.state
        if k == "scene":
            return s["scene"] == self._scene() or (s["studio"] and s["preview"] == self._scene())
        if k == "record":
            return s["recording"]
        if k == "stream":
            return s["streaming"]
        if k == "replay":
            return s["replay"]
        if k == "virtualcam":
            return s["virtualcam"]
        if k == "studiomode":
            return s["studio"]
        if k == "audio":
            m = o.is_muted(self._source())
            return None if m is None else not m
        if k == "source":
            try:
                return o.item(self.settings.get("scene") or "", self._source())[1][1]
            except Exception:  # noqa: BLE001
                return None
        return None

    def status_text(self):
        o = self.obs
        if not o.connected:
            return "OBS offline"
        k, s = self.kind, o.state
        if k == "record":
            return pgettext("state", "paused") if s["rec_paused"] else (
                pgettext("state", "running") if s["recording"] else pgettext("state", "off"))
        if k == "stream":
            return "LIVE" if s["streaming"] else pgettext("state", "off")
        if k == "audio":
            db = o.volume_db.get(self._source())
            m = o.muted.get(self._source())
            return pgettext("state", "muted") if m else (f"{db:.0f} dB" if isinstance(db, (int, float)) else "")
        if k in ("replay", "virtualcam", "studiomode", "source"):
            a = self.active()
            return "" if a is None else (pgettext("state", "on") if a else pgettext("state", "off"))
        return ""

    def main_text(self):
        return {"scene": self._scene() or _("Scene"), "record": "REC", "stream": "Stream", "source": self._source()
                or _("Source"), "audio": self._source() or "Audio", "replay": "Replay", "virtualcam": "VirtCam",
                "studiomode": "Studio", "screenshot": _("Snapshot"), "transition": _("Transition")
                }.get(self.kind, "OBS")

    def tick(self):
        v = self.obs.version
        changed = v != getattr(self, "_v", None)
        self._v = v
        return super().tick() or changed

    def key_down(self):
        self.run(self._execute)

    def _execute(self):
        o, k, mode = self.obs, self.kind, self._mode()
        from . import obs
        if not obs.wait_connected(o):
            raise RuntimeError(_("OBS not connected – enable the WebSocket server in OBS"))
        if k == "scene":
            o.set_scene(self._scene())
        elif k in ("record", "stream"):
            base = "Record" if k == "record" else "Stream"
            if mode in ("start", "stop"):
                o.request(f"{mode.capitalize()}{base}")
            elif mode == "pause" and k == "record":
                o.request("ToggleRecordPause")
            else:
                o.request(f"Toggle{base}")
        elif k == "replay":
            o.request("SaveReplayBuffer" if mode == "save" or "save" in self.uuid.lower() else "ToggleReplayBuffer")
        elif k == "virtualcam":
            o.request("ToggleVirtualCam")
        elif k == "studiomode":
            o.request("SetStudioModeEnabled", {"studioModeEnabled": not o.state["studio"]})
        elif k == "transition":
            o.request("TriggerStudioModeTransition")
        elif k == "source":
            o.toggle_source(self.settings.get("scene") or "", self._source())
        elif k == "audio":
            o.request("ToggleInputMute", {"inputName": self._source()})
        elif k == "screenshot":
            from .autoimport import _xdg_dir
            folder = _xdg_dir("PICTURES", "~/Pictures") / "OBS"
            folder.mkdir(parents=True, exist_ok=True)
            src = self._source() if self.settings.get("source") else o.state["scene"]
            o.request("SaveSourceScreenshot", {"sourceName": src, "imageFormat": "png",
                                               "imageFilePath": str(folder / time.strftime("OBS %Y-%m-%d %H-%M-%S.png"))})
            self.ok()

    def dial_rotate(self, ticks):
        if self.kind == "audio":
            self.run(self.obs.change_volume_db, self._source(), float(self.settings.get("step_db") or 1.0) * ticks)
        elif self.kind == "scene":
            self.run(self._cycle_scene, ticks)

    def _cycle_scene(self, ticks):
        scenes = [s["sceneName"] for s in reversed(self.obs.request("GetSceneList").get("scenes", []))]
        if scenes:
            cur = self.obs.state["scene"]
            i = (scenes.index(cur) + (1 if ticks > 0 else -1)) % len(scenes) if cur in scenes else 0
            self.obs.set_scene(scenes[i])

    def dial_down(self):
        self.key_down()

    def dial_up(self, held_ms=0):
        pass

    def feedback(self):
        fb = super().feedback()
        fb["title"] = self.title() or self.main_text()
        if self.kind == "audio":
            db = self.obs.volume_db.get(self._source())
            if isinstance(db, (int, float)):
                fb["indicator"] = max(0, min(100, (db + 60) * 100 / 60))
        return fb


# ==========================================================================
# Medien (Spotify u. a.) über MPRIS
# ==========================================================================
class MediaAction(NativeAction):
    FUNCS = {"playpause": "play-pause", "play": "play", "pause": "pause", "next": "next", "previous": "previous",
             "stop": "stop"}

    @property
    def player(self):
        p = self.settings.get("player")
        if p is None:
            p = "spotify" if "spotify" in self.uuid.lower() else ""
        return p

    @property
    def func(self):
        f = str(_first(self.settings, "function", "action", default="")).lower()
        if f:
            return f
        u = self.uuid.lower().rsplit(".", 1)[-1]
        for key in ("playpause", "play", "pause", "next", "previous", "prev", "shuffle", "repeat", "loop",
                    "volumeup", "volumedown", "volume", "nowplaying", "stop"):
            if key in u:
                return {"prev": "previous", "loop": "repeat"}.get(key, key)
        return "playpause"

    def _pc(self, *args, timeout=2):
        cmd = ["playerctl"] + (["-p", self.player] if self.player else []) + list(args)
        return system.run_out(cmd, timeout=timeout).strip()

    def _q(self, *args):
        """Abfrage mit kurzem Zwischenspeicher (Anzeige wird jede Sekunde aktualisiert)."""
        cache = self.__dict__.setdefault("_qcache", {})
        hit = cache.get(args)
        if hit and time.time() - hit[0] < 1.5:
            return hit[1]
        val = self._pc(*args) if system.have("playerctl") else ""
        cache[args] = (time.time(), val)
        return val

    def _status(self):
        return self._q("status")

    def active(self):
        f = self.func
        if f in ("playpause", "play", "pause", "nowplaying"):
            st = self._status()
            return None if not st else st == "Playing"
        if f == "shuffle":
            return self._q("shuffle") == "On"
        if f == "repeat":
            return self._q("loop") not in ("", "None")
        return None

    def preview_text(self):
        return _("Title") if self.func == "nowplaying" else self.main_text()

    def main_text(self):
        f = self.func
        if f == "nowplaying":
            return self._q("metadata", "--format", "{{title}}") or "–"
        return {"playpause": "▶ ‖", "play": "▶", "pause": "‖", "next": "»", "previous": "«", "stop": "■",
                "shuffle": "Shuffle", "repeat": "Repeat", "volumeup": "Vol +", "volumedown": "Vol −",
                "volume": "Vol"}.get(f, f)

    def status_text(self):
        if self.func == "nowplaying":
            return _fit(self._q("metadata", "--format", "{{artist}}"), 14)
        if self.func in ("volume", "volumeup", "volumedown"):
            v = self._q("volume")
            try:
                return f"{round(float(v) * 100)}%"
            except ValueError:
                return ""
        return (self.player or "").capitalize()

    def key_down(self):
        if not system.have("playerctl"):
            log.warning(_("playerctl is missing (package playerctl)"))
            self.alert()
            return
        f = self.func
        if f in self.FUNCS:
            self._pc(self.FUNCS[f])
        elif f == "nowplaying":
            self._pc("play-pause")
        elif f == "shuffle":
            self._pc("shuffle", "Toggle")
        elif f == "repeat":
            nxt = {"None": "Playlist", "Playlist": "Track"}.get(self._pc("loop"), "None")
            self._pc("loop", nxt)
        elif f in ("volumeup", "volume"):
            self._pc("volume", "0.05+")
        elif f == "volumedown":
            self._pc("volume", "0.05-")
        self.__dict__.pop("_qcache", None)
        self.redraw()

    def dial_rotate(self, ticks):
        self._pc("volume", f"{abs(ticks) * 0.03:.2f}{'+' if ticks > 0 else '-'}")
        self.__dict__.pop("_qcache", None)
        self.redraw()

    def dial_down(self):
        self._pc("play-pause")
        self.__dict__.pop("_qcache", None)

    def dial_up(self, held_ms=0):
        pass


# ==========================================================================
# Discord
# ==========================================================================
class DiscordAction(NativeAction):
    @property
    def dc(self):
        from . import discord
        return discord.client(self.ctx.cfg)

    @property
    def func(self):
        f = str(_first(self.settings, "function", default="")).lower()
        if f:
            return f
        u = self.uuid.lower().rsplit(".", 1)[-1]
        if "deaf" in u:
            return "deafen"
        if "pushtotalk" in u or "ptt" in u:
            return "pushtotalk"
        if "pushtomute" in u:
            return "pushtomute"
        if "voicechannel" in u or "channel" in u and "text" not in u:
            return "voicechannel"
        if "textchannel" in u:
            return "textchannel"
        if "leave" in u or "disconnect" in u:
            return "leave"
        return "mute"

    def active(self):
        if not self.dc.ready:
            return None
        if self.func == "mute":
            return self.dc.voice.get("mute")
        if self.func == "deafen":
            return self.dc.voice.get("deaf")
        return None

    def main_text(self):
        return {"mute": pgettext("key label", "Mic"), "deafen": _("Sound"), "pushtotalk": "PTT", "pushtomute": "PTM",
                "voicechannel": _("Channel"), "textchannel": "Chat", "leave": _("Hang up")}.get(self.func, "Discord")

    def status_text(self):
        if not self.dc.ready:
            return "Discord"
        a = self.active()
        return "" if a is None else (pgettext("state", "muted") if a else pgettext("state", "on"))

    def tick(self):
        v = self.dc.version
        changed = v != getattr(self, "_v", None)
        self._v = v
        return super().tick() or changed

    def _channel(self):
        return str(_first(self.settings, "channel_id", "channelId", "channel", default=""))

    def key_down(self):
        f = self.func
        if f == "mute":
            self.run(self.dc.toggle, "mute")
        elif f == "deafen":
            self.run(self.dc.toggle, "deaf")
        elif f == "pushtotalk":
            self.run(lambda: self.dc.set_voice(mute=False))
        elif f == "pushtomute":
            self.run(lambda: self.dc.set_voice(mute=True))
        elif f == "voicechannel":
            self.run(self.dc.select_voice_channel, self._channel())
        elif f == "textchannel":
            self.run(self.dc.select_text_channel, self._channel())
        elif f == "leave":
            self.run(self.dc.select_voice_channel, None)

    def key_up(self, held_ms=0):
        if self.func == "pushtotalk":
            self.run(lambda: self.dc.set_voice(mute=True))
        elif self.func == "pushtomute":
            self.run(lambda: self.dc.set_voice(mute=False))


# ==========================================================================
# Audio-Mixer (Wave-Link-Ersatz)
# ==========================================================================
class MixerAction(NativeAction):
    @property
    def mixer(self):
        from .mixer import Mixer
        return Mixer(self.ctx.cfg)

    @property
    def func(self):
        f = str(_first(self.settings, "function", default="")).lower()
        if f:
            return f
        u = self.uuid.lower()
        if "output" in u or "monitormix" in u or "switch" in u:
            return "output"
        return "channel"

    @property
    def channel(self):
        c = _first(self.settings, "channel", "mixerId", "mixerName", "identifier", "channelName",
                   default=(self.states[0].get("Title") if self.states else "") or "system")
        m = self.mixer.channel(str(c))
        return m["id"] if m else str(c).lower()

    @property
    def mix(self):
        m = str(_first(self.settings, "mix", "mixType", default="both")).lower()
        return "monitor" if "monitor" in m or "local" in m else "stream" if "stream" in m else "both"

    @property
    def key_action(self):
        return str(_first(self.settings, "action", default="mute")).lower()

    def _level(self):
        now = time.time()
        if now - getattr(self, "_lv_at", 0) > 1.0:
            mx = self.mixer
            mixes = ("monitor", "stream") if self.mix == "both" else (self.mix,)
            vals = [mx.get(self.channel, m) for m in mixes]
            vols = [v for v, _mu in vals if v is not None]
            self._lv = (max(vols) if vols else None, all(mu for _v, mu in vals) if vols else False)
            self._lv_at = now
        return self._lv

    def active(self):
        if self.func == "output":
            return None
        vol, mute = self._level()
        return None if vol is None else not mute

    def main_text(self):
        if self.func == "output":
            return _("Output")
        ch = self.mixer.channel(self.channel)
        return ch["name"] if ch else self.channel

    def status_text(self):
        if self.func == "output":
            return _fit(self.mixer.monitor_output_name(), 14)
        vol, mute = self._level()
        if vol is None:
            return _("Mixer off")
        mixname = {"monitor": "Monitor", "stream": "Stream", "both": ""}[self.mix]
        return (pgettext("state", "muted") if mute else f"{vol}%") + (f" · {mixname}" if mixname else "")

    def key_down(self):
        mx = self.mixer
        if self.func == "output":
            self.run(mx.set_monitor_output, self.settings.get("device") or "next")
        elif self.key_action in ("up", "down"):
            step = int(self.settings.get("step") or 5)
            self.run(mx.change_volume, self.channel, self.mix, step if self.key_action == "up" else -step)
        elif self.key_action == "set":
            self.run(mx.set_volume, self.channel, self.mix, int(self.settings.get("volume") or 100))
        else:
            self.run(mx.set_mute, self.channel, self.mix, "toggle")
        self._lv_at = 0

    def dial_rotate(self, ticks):
        self._lv_at = 0
        self.run(self.mixer.change_volume, self.channel, self.mix, int(self.settings.get("step") or 2) * ticks)

    def dial_down(self):
        self._lv_at = 0
        self.run(self.mixer.set_mute, self.channel, self.mix, "toggle")

    def dial_up(self, held_ms=0):
        pass

    def feedback(self):
        fb = super().feedback()
        fb["title"] = self.title() or self.main_text()
        vol, mute = self._level() if self.func != "output" else (None, False)
        fb["indicator"] = None if vol is None else (0 if mute else min(100, vol))
        return fb


# ==========================================================================
# Wave-Mikrofon
# ==========================================================================
class WaveMicAction(NativeAction):
    @property
    def mic(self):
        from . import wavemic
        return wavemic.instance(self.ctx.cfg)

    @property
    def func(self):
        f = str(_first(self.settings, "function", default="")).lower()
        if f:
            return f
        u = self.uuid.lower()
        if "lowcut" in u:
            return "lowcut"
        if "clip" in u:
            return "clipguard"
        if "led" in u:
            return "leds"
        if "gain" in u:
            return "gain"
        return "mute"

    def _gain(self):
        now = time.time()
        if now - getattr(self, "_g_at", 0) > 1.0:
            try:
                self._g = self.mic.gain()
            except RuntimeError:
                self._g = (None, False)
            self._g_at = now
        return self._g

    def _switch(self, f):
        now = time.time()
        cache = self.__dict__.setdefault("_sw", {})
        if f not in cache or now - cache[f][0] > 2.0:
            key = {"leds": "leds_off", "ledsflip": "leds_flip"}.get(f, f)
            val = self.mic.is_on(key)
            cache[f] = (now, (not val if val is not None else None) if f == "leds" else val)
        return cache[f][1]

    def active(self):
        f = self.func
        if f in ("lowcut", "clipguard", "leds", "ledsflip"):
            return self._switch(f)
        vol, mute = self._gain()
        return None if vol is None else not mute

    def main_text(self):
        return {"mute": pgettext("key label", "Mic"), "gain": "Gain", "lowcut": "Low Cut", "clipguard": "Clipguard", "leds": "LEDs",
                "ledsflip": "LED ↕"}.get(self.func, "Wave")

    def status_text(self):
        f = self.func
        if f in ("lowcut", "clipguard", "leds", "ledsflip"):
            on = self._switch(f)
            if on is None:
                return _("Wave:3 only") if f in ("leds", "ledsflip") else _("no Wave")
            if f == "lowcut" and on and not self.mic.hw():
                return f"{self.mic.settings.get('lowcut_freq', 80)} Hz"
            return pgettext("state", "on") if on else pgettext("state", "off")
        vol, mute = self._gain()
        if vol is None:
            return _("no Wave")
        return pgettext("state", "muted") if mute else f"{vol}%"

    def key_down(self):
        f = self.func
        self._g_at = 0
        self.__dict__.pop("_sw", None)
        if f in ("lowcut", "clipguard"):
            self.run(self.mic.toggle, f)
        elif f == "leds":
            self.run(self.mic.toggle, "leds_off")
        elif f == "ledsflip":
            self.run(self.mic.toggle, "leds_flip")
        elif f == "gain":
            self.run(self.mic.change_gain, int(self.settings.get("step") or 5))
        else:
            self.run(self.mic.toggle_mute)

    def dial_rotate(self, ticks):
        self._g_at = 0
        self.run(self.mic.change_gain, int(self.settings.get("step") or 2) * ticks)

    def dial_down(self):
        self._g_at = 0
        self.run(self.mic.toggle_mute)

    def dial_up(self, held_ms=0):
        pass

    def feedback(self):
        fb = super().feedback()
        fb["title"] = self.title() or self.main_text()
        vol, mute = self._gain()
        if self.func in ("mute", "gain") and vol is not None:
            fb["indicator"] = 0 if mute else vol
        return fb


# ==========================================================================
# Zuordnung UUID -> Aktion
# ==========================================================================
_OBS_KINDS = [("screenshot", "screenshot"), ("virtualcam", "virtualcam"), ("virtual", "virtualcam"),
              ("replay", "replay"), ("studio", "studiomode"), ("transition", "transition"),
              ("record", "record"), ("stream", "stream"), ("mixer", "audio"), ("audio", "audio"),
              ("mute", "audio"), ("volume", "audio"), ("input", "audio"), ("source", "source"),
              ("sceneitem", "source"), ("item", "source"), ("scene", "scene")]


def lookup(uuid: str):
    """Aktionsklasse für eine Plugin-UUID oder None."""
    u = (uuid or "").lower()
    if not u:
        return None
    plugin, _sep, last = u.rpartition(".")
    if u.startswith("com.lindeck.obs.") or "obs" in plugin.split(".") or "obsstudio" in plugin:
        kind = next((k for key, k in _OBS_KINDS if key in last), None)
        if kind:
            return type(f"OBS_{kind}", (OBSAction,), {"kind": kind})
        return None
    if u.startswith("com.lindeck.media.") or "spotify" in plugin:
        return MediaAction
    if u.startswith("com.lindeck.discord.") or "discord" in plugin:
        return DiscordAction
    if u.startswith("com.lindeck.wavemic.") or ("wavelink" in plugin and ("mic" in last or "lowcut" in last
                                                                         or "clip" in last or "gain" in last)):
        return WaveMicAction
    if u.startswith("com.lindeck.mixer.") or "wavelink" in plugin or "wave-link" in plugin:
        return MixerAction
    return None
