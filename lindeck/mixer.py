"""Wave-Link-Ersatz auf Basis von PipeWire (über pipewire-pulse/pactl).

Wie Elgato Wave Link: Programme landen in virtuellen Kanälen (System, Musik,
Browser, Voice Chat, Spiel, Effekte, Mikrofon). Jeder Kanal hat zwei
unabhängige Lautstärken/Stummschaltungen:

  * Monitor-Mix – was du über Kopfhörer/Lautsprecher hörst
  * Stream-Mix  – was OBS/Discord als Quelle „LinDeck Stream-Mix“ bekommt

Aufbau:
    Programm ─► Kanal (Null-Sink „LinDeck <Kanal>“) ─┬─► Loopback ─► Kopfhörer      (Monitor-Mix)
                                                      └─► Loopback ─► Stream-Mix-Sink (Stream-Mix)
    Mikrofon ─────────────────────────────────────────┬─► Loopback ─► Stream-Mix-Sink
                                                      └─► Loopback ─► Kopfhörer (standardmäßig stumm)

Der Stream-Mix ist ein Null-Sink; seine Monitor-Quelle wird in OBS als
Audio-Eingang gewählt („Monitor of LinDeck Stream-Mix“).

Konfiguration (config.json):
  "mixer": {
    "enabled": true,
    "channels": [{"id": "music", "name": "Musik"}, …],    (optional, Standard siehe unten)
    "monitor_output": "",                                  (leer = aktuelles Standardgerät)
    "mic": "",                                             (leer = Standardmikrofon)
    "routes": {"spotify": "music", "firefox": "browser"}   (Programm -> Kanal)
  }
"""
import json
import logging
import re
import subprocess
import time

from . import paths, system

log = logging.getLogger("lindeck.mixer")

PREFIX = "lindeck_"
STREAM_SINK = PREFIX + "streammix"
DEFAULT_CHANNELS = [
    {"id": "system", "name": "System"},
    {"id": "music", "name": "Musik"},
    {"id": "browser", "name": "Browser"},
    {"id": "voice", "name": "Voice Chat"},
    {"id": "game", "name": "Spiel"},
    {"id": "sfx", "name": "Effekte"},
]
MIC = {"id": "mic", "name": "Mikrofon"}
MIXES = ("monitor", "stream")
DEFAULT_ROUTES = {
    "spotify": "music", "rhythmbox": "music", "lollypop": "music", "elisa": "music", "amarok": "music",
    "firefox": "browser", "chromium": "browser", "chrome": "browser", "brave": "browser", "vivaldi": "browser",
    "opera": "browser", "librewolf": "browser",
    "discord": "voice", "vesktop": "voice", "teamspeak": "voice", "ts3client": "voice", "mumble": "voice",
    "zoom": "voice", "teams": "voice", "signal": "voice", "element": "voice",
    "steam": "game", "wine": "game", "proton": "game", "lutris": "game", "heroic": "game", "minecraft": "game",
    "java": "game",
}
STATE_FILE = "mixer.json"


def _run(args):
    r = subprocess.run(["pactl"] + args, capture_output=True, text=True, timeout=5)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "pactl-Fehler").strip())
    return r.stdout.strip()


def _q(text):
    """Wert für eine Property-Liste in pactl-Modulargumenten."""
    return "'" + text.replace("'", "") + "'"


class Mixer:
    def __init__(self, cfg=None, run=None):
        self.cfg = cfg
        self.run = run or _run
        self.state_file = paths.STATE_DIR / STATE_FILE
        self._moved = set()

    # ----------------------------------------------------------- Konfiguration
    @property
    def conf(self):
        return (self.cfg.get("mixer") if self.cfg else None) or {}

    def channels(self):
        chans = self.conf.get("channels") or DEFAULT_CHANNELS
        return [c for c in chans if c.get("id") and c["id"] != "mic"]

    def all_channels(self):
        return self.channels() + [MIC]

    def channel(self, cid):
        cid = (cid or "").lower()
        for c in self.all_channels():
            if c["id"].lower() == cid or c["name"].lower() == cid:
                return c
        return None

    @staticmethod
    def sink_name(cid):
        return PREFIX + cid

    @staticmethod
    def loop_name(cid, mix):
        return f"{PREFIX}{cid}_{mix}"

    def routes(self):
        r = dict(DEFAULT_ROUTES)
        r.update({k.lower(): v for k, v in (self.conf.get("routes") or {}).items()})
        return r

    # ----------------------------------------------------------------- Status
    def _state(self):
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, st):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(st, indent=2), encoding="utf-8")

    def is_active(self):
        names = {b.get("name") for b in system._pactl_blocks("sinks")}
        return STREAM_SINK in names

    def default_sink(self):
        return self.run(["get-default-sink"])

    def default_source(self):
        return self.run(["get-default-source"])

    # ------------------------------------------------------- Auf- und Abbau
    def setup(self):
        """Kanäle und Mixe anlegen (vorhandene LinDeck-Module werden vorher entfernt)."""
        if not system.have("pactl"):
            raise RuntimeError("pactl fehlt (Paket pulseaudio-utils) – PipeWire mit pipewire-pulse wird benötigt")
        old = self._state()
        self.teardown(restore_default=False)
        prev_default = old.get("previous_default") or self.default_sink()
        if prev_default.startswith(PREFIX):
            prev_default = self.default_sink()
        monitor_out = self.conf.get("monitor_output") or prev_default
        sources = {b.get("name") for b in system._pactl_blocks("sources")}
        mic = self.conf.get("mic") or ("lindeck_wave" if "lindeck_wave" in sources else self.default_source())
        modules = []

        def load(*args):
            mid = self.run(["load-module"] + list(args))
            modules.append(mid)
            return mid
        load("module-null-sink", f"sink_name={STREAM_SINK}",
             f"sink_properties=device.description={_q('LinDeck Stream-Mix')}")
        for c in self.channels():
            sink = self.sink_name(c["id"])
            load("module-null-sink", f"sink_name={sink}",
                 f"sink_properties=device.description={_q('LinDeck ' + c['name'])}")
            for mix, target in (("monitor", monitor_out), ("stream", STREAM_SINK)):
                load("module-loopback", f"source={sink}.monitor", f"sink={target}", "latency_msec=20",
                     "source_dont_move=true", f"sink_input_properties=media.name={self.loop_name(c['id'], mix)}")
        if mic and not mic.endswith(".monitor"):
            for mix, target in (("stream", STREAM_SINK), ("monitor", monitor_out)):
                load("module-loopback", f"source={mic}", f"sink={target}", "latency_msec=20",
                     f"sink_input_properties=media.name={self.loop_name('mic', mix)}")
        self.run(["set-default-sink", self.sink_name(self.channels()[0]["id"])])
        self._save_state({"modules": modules, "previous_default": prev_default, "monitor_output": monitor_out,
                          "mic": mic, "created": time.time()})
        time.sleep(0.3)
        # eigenes Mikrofon standardmäßig nicht auf den Kopfhörer
        try:
            self.set_mute("mic", "monitor", True)
        except RuntimeError:
            pass
        restored = old.get("levels") or {}
        for key, (vol, mute) in restored.items():
            cid, mix = key.rsplit("_", 1)
            try:
                self.set_volume(cid, mix, vol)
                self.set_mute(cid, mix, mute)
            except RuntimeError:
                pass
        self._moved.clear()
        log.info("Audio-Mixer eingerichtet: %d Kanäle, Monitor → %s", len(self.channels()), monitor_out)
        return modules

    def teardown(self, restore_default=True):
        st = self._state()
        if st.get("modules"):
            st["levels"] = self.levels()
        for mid in reversed(st.get("modules") or []):
            try:
                self.run(["unload-module", str(mid)])
            except RuntimeError:
                pass
        # Reste früherer Läufe (z. B. nach Absturz)
        for b in system._pactl_blocks("modules"):
            args = " ".join(b.get("props", {}).values()) + " " + b.get("argument", "")
            if PREFIX in args and b.get("index"):
                try:
                    self.run(["unload-module", b["index"]])
                except RuntimeError:
                    pass
        if restore_default and st.get("previous_default"):
            try:
                self.run(["set-default-sink", st["previous_default"]])
            except RuntimeError:
                pass
        st["modules"] = []
        self._save_state(st)

    # ------------------------------------------------------ Lautstärke/Mute
    def _loop_input(self, cid, mix):
        name = self.loop_name(cid, mix)
        for b in system._pactl_blocks("sink-inputs"):
            if b["props"].get("media.name") == name:
                return b
        return None

    def get(self, cid, mix):
        b = self._loop_input(cid, mix)
        return (b.get("volume", 0), bool(b.get("mute"))) if b else (None, False)

    def set_volume(self, cid, mix, pct):
        for m in (MIXES if mix == "both" else (mix,)):
            b = self._loop_input(cid, m)
            if not b:
                raise RuntimeError(f"Kanal {cid}/{m} nicht gefunden – Mixer eingerichtet? (lindeck mixer setup)")
            self.run(["set-sink-input-volume", b["index"], f"{max(0, min(150, int(pct)))}%"])

    def change_volume(self, cid, mix, delta):
        for m in (MIXES if mix == "both" else (mix,)):
            vol, _ = self.get(cid, m)
            if vol is None:
                raise RuntimeError(f"Kanal {cid}/{m} nicht gefunden")
            self.set_volume(cid, m, vol + delta)

    def set_mute(self, cid, mix, mute):
        for m in (MIXES if mix == "both" else (mix,)):
            b = self._loop_input(cid, m)
            if not b:
                raise RuntimeError(f"Kanal {cid}/{m} nicht gefunden")
            self.run(["set-sink-input-mute", b["index"], "toggle" if mute == "toggle" else ("1" if mute else "0")])

    def levels(self):
        out = {}
        for b in system._pactl_blocks("sink-inputs"):
            name = b["props"].get("media.name", "")
            if name.startswith(PREFIX) and name.rsplit("_", 1)[-1] in MIXES:
                out[name[len(PREFIX):]] = (b.get("volume", 0), bool(b.get("mute")))
        return out

    # ---------------------------------------------------------- Ausgänge
    def outputs(self):
        """Echte Ausgabegeräte (ohne LinDeck-Kanäle)."""
        return [b for b in system._pactl_blocks("sinks") if not b.get("name", "").startswith(PREFIX)]

    def set_monitor_output(self, device):
        """Monitor-Mix auf ein anderes Ausgabegerät legen (Name, Teil der Beschreibung oder 'next')."""
        outs = self.outputs()
        if not outs:
            raise RuntimeError("Kein Ausgabegerät gefunden")
        st = self._state()
        cur = st.get("monitor_output", "")
        if device in ("", "next", None):
            names = [o["name"] for o in outs]
            target = names[(names.index(cur) + 1) % len(names)] if cur in names else names[0]
        else:
            d = device.lower()
            target = next((o["name"] for o in outs if o["name"] == device or d in (o.get("description") or "").lower()),
                          None)
            if not target:
                raise RuntimeError(f"Ausgabegerät „{device}“ nicht gefunden")
        for b in system._pactl_blocks("sink-inputs"):
            name = b["props"].get("media.name", "")
            if name.startswith(PREFIX) and name.endswith("_monitor"):
                self.run(["move-sink-input", b["index"], target])
        st["monitor_output"] = target
        self._save_state(st)
        return target

    def monitor_output_name(self):
        cur = self._state().get("monitor_output", "")
        for o in self.outputs():
            if o["name"] == cur:
                return o.get("description") or cur
        return cur

    # ------------------------------------------------------------- Routing
    def route_streams(self):
        """Neue Wiedergabe-Streams anhand der Regeln in Kanäle verschieben."""
        routes = self.routes()
        chans = {c["id"] for c in self.channels()}
        moved = 0
        for b in system._pactl_blocks("sink-inputs"):
            idx = b["index"]
            p = b["props"]
            if idx in self._moved or p.get("media.name", "").startswith(PREFIX):
                continue
            self._moved.add(idx)
            names = " ".join(filter(None, (p.get("application.name"), p.get("application.process.binary"),
                                           p.get("application.id"), p.get("node.name")))).lower()
            for key, cid in routes.items():
                if cid in chans and re.search(r"\b" + re.escape(key), names):
                    try:
                        self.run(["move-sink-input", idx, self.sink_name(cid)])
                        moved += 1
                    except RuntimeError as e:
                        log.debug("Verschieben von %s fehlgeschlagen: %s", idx, e)
                    break
        return moved

    def assign(self, app, cid):
        """Programm dauerhaft einem Kanal zuordnen (laufende Streams werden sofort verschoben)."""
        if not self.channel(cid):
            raise RuntimeError(f"Unbekannter Kanal: {cid}")
        n = 0
        for b in system.app_streams(app):
            self.run(["move-sink-input", b["index"], self.sink_name(cid)])
            n += 1
        return n


class MixerService:
    """Richtet den Mixer beim Start ein und verteilt neue Streams auf die Kanäle."""

    def __init__(self, daemon):
        self.mixer = Mixer(daemon.cfg)

    def start(self):
        if not self.mixer.is_active():
            self.mixer.setup()

    def tick(self):
        if self.mixer.conf.get("auto_route", True):
            self.mixer.route_streams()

    def stop(self):
        pass  # Kanäle bleiben bestehen, damit der Ton beim Neustart von LinDeck nicht abreißt
