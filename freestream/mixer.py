"""Wave-Link-Ersatz auf Basis von PipeWire (über pipewire-pulse/pactl).

Wie Elgato Wave Link: Programme landen in virtuellen Kanälen (System, Musik,
Browser, Voice Chat, Spiel, Effekte, Mikrofon). Jeder Kanal hat je Mix eine
eigene Lautstärke/Stummschaltung. Mixe:

  * Persönlicher Mix („monitor“) – was du über Kopfhörer/Lautsprecher hörst
  * Stream-Mix („stream“)       – was OBS/Discord als Quelle „FreeWave Stream-Mix“ bekommt
  * eigene Mixe (z. B. „Chat-Mix“ für Discord) – auf Geräte und/oder als virtuelle Quelle

Aufbau:
    Programm ─► Kanal (Null-Sink „FreeWave <Kanal>“) ┬─► Loopback ─► Kopfhörer      (Persönlicher Mix)
                                                      ├─► Loopback ─► Stream-Mix-Sink (Stream-Mix)
                                                      └─► Loopback ─► Mix-Sink ─► Ausgänge (eigene Mixe)
    Mikrofon ─────────────────────────────────────────┬─► Loopback ─► Stream-Mix-Sink
                                                      └─► Loopback ─► Kopfhörer (standardmäßig stumm)

Ein Mix mit genau einem Gerät und ohne virtuelle Quelle geht direkt auf das
Gerät (wie bisher der Monitor-Mix); sonst ist er ein Null-Sink, dessen
Monitor-Quelle Programme aufnehmen (OBS: „Monitor of FreeWave Stream-Mix“) und
der über eigene Loopbacks auf seine Geräte geht. Die Kanal-Sinks werden mit
monitor.channel-volumes angelegt: ihre Lautstärke ist der Hauptregler des
Kanals und wirkt auf alle Mixe.

Konfiguration (config.json):
  "mixer": {
    "enabled": true,
    "channels": [{"id": "music", "name": "Musik"}, …],    (optional, Standard siehe unten)
        eigene Kanäle: {"id", "name", "icon", "color", "kind": "apps"|"input", "source": "<pactl-Quelle>"}
        „apps“  = Kanal für Programme (Null-Sink), „input“ = Eingangsgerät (z. B. 2. Mikrofon, Capture-Karte)
    "monitor_output": "",                                  (leer = aktuelles Standardgerät)
    "mixes": [{"id": "chat", "name": "Chat-Mix", "icon": "chat", "color": "#22c55e",
               "devices": ["alsa_output…"], "virtual": true, "exclude": ["music"]}, …]
               (feste Mixe "monitor"/"stream" dürfen hier Name, Symbol, Geräte und Ausschlüsse ändern)
    "mic": "",                                             (leer = Standardmikrofon)
    "routes": {"spotify": "music", "firefox": "browser"},  (Programm -> Kanal)
    "scenes": [{"id": "gaming", "name": "Gaming",           (Mixer-Szenen: Lautstärke/stumm je Kanal und Mix)
                "levels": {"music": {"monitor": [40, false], "stream": [30, false]}, …}}],
    "ducking": {"enabled": false, "channels": ["music"], …} (Auto-Ducking, siehe ducking.py)
    "noise": {"enabled": true, "backend": "auto", "strength": 50}   (Rauschunterdrückung, siehe noise.py)
  }
"""
import json
import logging
import os
import re
import subprocess
import threading
import time

from . import paths, system
from .i18n import _

log = logging.getLogger("freestream.mixer")

PREFIX = "linewave_"
LEGACY_PREFIXES = ("lindeck_",)  # Namen bis Version 0.6
STREAM_SINK = PREFIX + "streammix"
DEFAULT_CHANNELS = [
    {"id": "system", "name": _("System")},
    {"id": "music", "name": _("Music")},
    {"id": "browser", "name": _("Browser")},
    {"id": "voice", "name": _("Voice Chat")},
    {"id": "game", "name": _("Game")},
    {"id": "sfx", "name": _("SFX")},
]
MIC = {"id": "mic", "name": _("Microphone"), "kind": "input"}
RESERVED_IDS = {"mic", "streammix", "micfilter", "stream", "monitor", "mix", "park", "listen", "both", "selected",
                "cycle"}
MIXES = ("monitor", "stream")  # feste Mixe; dazu kommen eigene (Mixer.mix_ids())
MIX_SINK_PREFIX = PREFIX + "mix_"
PARK_SINK = PREFIX + "park"      # stiller Sink: hier wartet der Persönliche Mix, während ein anderer abgehört wird
LISTEN_LOOP = PREFIX + "listen"
BUILTIN_MIXES = {
    "monitor": {"id": "monitor", "name": _("Personal Mix"), "icon": "headphones", "color": "#22c3ee",
                "virtual": False},
    "stream": {"id": "stream", "name": _("Stream Mix"), "icon": "stream", "color": "#c084fc", "virtual": True},
}
MAX_VOLUME = 100  # wie Wave Link: nie über 100 % (keine Übersteuerung)
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
DUCK_FILE = "ducking.json"
DUCK_USER_FILE = "ducking-user.json"


READ_ONLY = ("get-default-sink", "get-default-source", "info", "list")


def _run(args):
    def run():
        r = subprocess.run(["pactl"] + args, capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or _("pactl error")).strip())
        return r.stdout.strip()
    # lesende Abfragen innerhalb von system.pactl_snapshot() nur einmal, schreibende leeren den Speicher
    return system.pactl_cached(("run", *args), run, write=not args or args[0] not in READ_ONLY)


def _props(raw=None, **props):
    """Property-Liste als ein Modulargument, z. B. sink_properties=_props(...).

    Werte mit Leerzeichen („FreeWave Musik“) müssen doppelt gequotet sein:
    außen für das Modulargument, innen für die Property-Liste – sonst schneiden
    PulseAudio und pipewire-pulse beim ersten Leerzeichen ab und jeder Kanal
    hieße nur „FreeWave“.
    """
    items = [(k.replace("_", "."), v) for k, v in props.items()] + list((raw or {}).items())
    inner = " ".join(f"{k}='{str(v).replace(chr(39), '').replace(chr(34), '')}'" for k, v in items)
    return f'"{inner}"'


def _ours(name):
    return name.startswith(PREFIX) or name.startswith(LEGACY_PREFIXES)


LOOP_PROP = "linewave.loop"


def _loop_id(props):
    """Kennung eines FreeWave-Loopbacks („linewave_music_monitor“) oder ''.

    Steht in der eigenen Property linewave.loop; media.name trägt den lesbaren
    Namen („FreeWave Musik → Monitor-Mix“), den Lautstärke-Regler anzeigen.
    Ältere Versionen hatten die Kennung direkt in media.name.
    """
    lid = props.get(LOOP_PROP) or props.get("media.name", "")
    return lid if _ours(lid) else ""


# Zuletzt gelesene Loopbacks (Name -> Block): Tasten am Deck schreiben damit sofort, ohne erst
# „pactl list“ abzuwarten. Nur kurz gültig; jede neue Abfrage frischt sie auf.
RECENT_S = 2.0
KEEP_RECENT = ("set-sink-input-volume", "set-sink-input-mute")  # ändern nur Werte, nicht die Indizes
_RECENT = {"at": 0.0, "loops": {}}


def _remember_loops(blocks):
    loops = {}
    for b in blocks:
        lid = _loop_id(b["props"])
        if lid:
            loops[lid] = b
    _RECENT["loops"], _RECENT["at"] = loops, time.monotonic()


def _recent_loops():
    return _RECENT["loops"] if time.monotonic() - _RECENT["at"] < RECENT_S else None


def _is_ours(props):
    return bool(_loop_id(props)) or _ours(props.get("media.name", ""))


class DuckFile:
    """Auto-Ducking des FreeWave-Hosts aus Sicht anderer Programme (FreeDeck, Kommandozeile).

    Der Host schreibt während des Absenkens STATE_DIR/ducking.json: {"time", "factor", "bases"}.
    Lautstärken, die hier gesetzt werden, gelten als Nutzerwert und landen in ducking-user.json;
    der Host übernimmt sie und senkt relativ dazu ab.
    """
    MAX_AGE = 3.0

    def __init__(self, data):
        self.data = data

    @classmethod
    def load(cls):
        try:
            data = json.loads((paths.STATE_DIR / DUCK_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or abs(time.time() - float(data.get("time") or 0)) > cls.MAX_AGE:
            return None
        return cls(data)

    def base(self, key):
        v = (self.data.get("bases") or {}).get(key)
        return None if v is None else int(v)

    def user_set(self, key, pct):
        self.data.setdefault("bases", {})[key] = pct
        f = paths.STATE_DIR / DUCK_USER_FILE
        try:
            req = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            req = {}
        req[key] = pct
        try:
            f.write_text(json.dumps(req), encoding="utf-8")
        except OSError:
            pass
        return duck_volume(pct, float(self.data.get("factor") or 1.0))


def duck_volume(base, factor):
    """Abgesenkte Lautstärke (Prozent) zu einem Nutzerwert."""
    return max(0, min(MAX_VOLUME, int(round(base * factor))))


class Mixer:
    ducker = None  # Auto-Ducking im selben Prozess (FreeWave-Host), sonst über DuckFile

    def __init__(self, cfg=None, run=None):
        self.cfg = cfg
        raw = run or _run

        def tracked(args):
            if args and args[0] not in READ_ONLY and args[0] not in KEEP_RECENT:
                _RECENT["at"] = 0.0  # Loopbacks neu angelegt/verschoben: gemerkte Indizes ungültig
            return raw(args)
        self.run = tracked
        self.state_file = paths.STATE_DIR / STATE_FILE
        self._moved = set()
        from .effects import Effects
        from .noise import NoiseSuppression
        self.fx = Effects(self)
        self.noise = NoiseSuppression(self)

    # ----------------------------------------------------------- Konfiguration
    @property
    def conf(self):
        return (self.cfg.get("mixer") if self.cfg else None) or {}

    def channels(self):
        """Alle Kanäle außer dem Mikrofon (Programme und eigene Eingänge)."""
        chans = self.conf.get("channels") or DEFAULT_CHANNELS
        return [c for c in chans if c.get("id") and c["id"] != "mic"]

    def app_channels(self):
        """Kanäle, in die Programme gelegt werden können (Null-Sinks)."""
        return [c for c in self.channels() if c.get("kind", "apps") == "apps"]

    def all_channels(self):
        mic = dict(MIC)
        mic.update((self.conf.get("mic_channel") or {}))
        return self.channels() + [mic]

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

    def _cfg_channels(self):
        mx = self.cfg.data.setdefault("mixer", {})
        if not mx.get("channels"):
            mx["channels"] = [dict(c) for c in DEFAULT_CHANNELS]
        return mx["channels"]

    def _save_cfg(self):
        if hasattr(self.cfg, "save"):
            self.cfg.save()

    def new_id(self, name):
        base = re.sub(r"[^a-z0-9]", "", (name or "").lower().replace("ä", "ae").replace("ö", "oe")
                      .replace("ü", "ue").replace("ß", "ss"))[:16] or "kanal"
        taken = {c["id"] for c in self.all_channels()} | RESERVED_IDS
        cid, n = base, 2
        while cid in taken:
            cid, n = f"{base}{n}", n + 1
        return cid

    def inputs(self):
        """Echte Eingangsgeräte (Mikrofone, Line-In, Capture-Karten) als [(Name, Beschreibung)]."""
        out = []
        for b in system._pactl_blocks("sources"):
            name = b.get("name", "")
            if not name or name.endswith(".monitor") or _ours(name):
                continue
            out.append((name, b.get("description") or name))
        return out

    def add_channel(self, name, kind="apps", source="", icon="", color="", in_mixes=True):
        """Eigenen Kanal anlegen; läuft der Mixer, wird er sofort eingerichtet.

        in_mixes=False: der Kanal ist zunächst in keinem Mix (Zuordnung wählt man selbst).
        """
        name = (name or "").strip()
        if not name:
            raise RuntimeError(_("Please enter a name"))
        if kind not in ("apps", "input"):
            raise RuntimeError(_("Unknown kind: {kind}").format(kind=kind))
        if kind == "input" and not source:
            raise RuntimeError(_("Please choose an input device"))
        c = {"id": self.new_id(name), "name": name, "kind": kind}
        if kind == "input":
            c["source"] = source
        if icon:
            c["icon"] = icon
        if color:
            c["color"] = color
        self._cfg_channels().append(c)
        if not in_mixes:
            for m in self.mixes():
                cm = self._cfg_mix(m["id"])
                cm["exclude"] = [x for x in m["exclude"] if x != c["id"]] + [c["id"]]
        self._save_cfg()
        if self.is_active():
            self._add_live(c)
        return c

    def update_channel(self, cid, **changes):
        """Name, Symbol, Farbe oder Eingangsgerät eines Kanals ändern."""
        if cid == "mic":
            mc = self.cfg.data.setdefault("mixer", {})
            if "source" in changes:
                mc["mic"] = changes.pop("source") or ""
            mc.setdefault("mic_channel", {}).update({k: v for k, v in changes.items() if k in ("icon", "color")})
            self._save_cfg()
            if self.is_active():
                self.rebuild_channel("mic")
            return self.channel("mic")
        c = next((c for c in self._cfg_channels() if c["id"] == cid), None)
        if c is None:
            raise RuntimeError(_("Channel {id} not found").format(id=cid))
        if "name" in changes and not (changes["name"] or "").strip():
            raise RuntimeError(_("Please enter a name"))
        rebuild = any(k in changes and changes[k] != c.get(k) for k in ("name", "source"))
        for k in ("name", "icon", "color", "source"):
            if k in changes and changes[k] is not None:
                c[k] = changes[k].strip() if k == "name" else changes[k]
        self._save_cfg()
        if rebuild and self.is_active():
            self.rebuild_channel(cid)
        return c

    def remove_channel(self, cid):
        """Kanal löschen. Programme darin landen wieder beim Standardgerät."""
        chans = self._cfg_channels()
        c = next((c for c in chans if c["id"] == cid), None)
        if c is None:
            raise RuntimeError(_("Channel {id} not found").format(id=cid))
        if c.get("kind", "apps") == "apps" and len(self.app_channels()) <= 1:
            raise RuntimeError(_("At least one channel for apps must remain"))
        chans.remove(c)
        mx = self.cfg.data["mixer"]
        mx["routes"] = {k: v for k, v in (mx.get("routes") or {}).items() if v != cid}
        (mx.get("effects") or {}).pop(cid, None)
        self._save_cfg()
        if self.is_active():
            self._unload_channel(cid)
        self.fx.stop(cid)

    def routes(self):
        r = dict(DEFAULT_ROUTES)
        r.update({k.lower(): v for k, v in (self.conf.get("routes") or {}).items()})
        return r

    # ------------------------------------------------------------------ Mixe
    def mixes(self):
        """Alle Mixe in Reihenfolge: Persönlicher Mix, Stream-Mix, eigene.

        Jeder Mix: {"id", "name", "icon", "color", "virtual", "devices": [...], "exclude": [...], "builtin"}.
        devices leer beim Persönlichen Mix = aktuelles Ausgabegerät (monitor_output)."""
        conf = {m.get("id"): m for m in (self.conf.get("mixes") or []) if isinstance(m, dict) and m.get("id")}
        out = []
        for mid, base in BUILTIN_MIXES.items():
            m = dict(base, devices=[], exclude=[], builtin=True)
            m.update({k: v for k, v in (conf.get(mid) or {}).items() if k in ("name", "icon", "color", "devices",
                                                                              "exclude") and v not in (None, "")})
            m["virtual"] = mid == "stream" or bool((conf.get(mid) or {}).get("virtual", base["virtual"]))
            out.append(m)
        for mid, m in conf.items():
            if mid in BUILTIN_MIXES:
                continue
            out.append({"id": mid, "name": m.get("name") or mid, "icon": m.get("icon") or "wave",
                        "color": m.get("color") or "#22c55e", "virtual": bool(m.get("virtual")),
                        "devices": list(m.get("devices") or []), "exclude": list(m.get("exclude") or []),
                        "builtin": False})
        return out

    def mix(self, mid):
        mid = (mid or "").lower()
        return next((m for m in self.mixes() if m["id"] == mid or m["name"].lower() == mid), None)

    def mix_ids(self):
        return [m["id"] for m in self.mixes()]

    def in_mix(self, cid, mid):
        m = self.mix(mid)
        return bool(m) and cid not in m["exclude"]

    def channel_mixes(self, cid):
        """Mixe, in die ein Kanal geht."""
        return [m["id"] for m in self.mixes() if cid not in m["exclude"]]

    def _cfg_mix(self, mid, create=True):
        mx = self.cfg.data.setdefault("mixer", {})
        lst = mx.setdefault("mixes", [])
        m = next((x for x in lst if x.get("id") == mid), None)
        if m is None and create:
            m = {"id": mid}
            lst.append(m)
        return m

    def new_mix_id(self, name):
        base = re.sub(r"[^a-z]", "", (name or "").lower().replace("ä", "ae").replace("ö", "oe")
                      .replace("ü", "ue").replace("ß", "ss"))[:16] or "mix"
        taken = set(self.mix_ids()) | RESERVED_IDS
        mid, n = base, 2
        while mid in taken:
            mid, n = f"{base}{chr(96 + n)}", n + 1  # nur Buchstaben: „chatb“, „chatc“ …
        return mid

    @staticmethod
    def _check_outputs(devices, virtual):
        if not devices and not virtual:
            raise RuntimeError(_("Choose at least one output"))

    def add_mix(self, name, icon="wave", color="#22c55e", devices=(), virtual=True, exclude=()):
        """Eigenen Mix anlegen; läuft der Mixer, wird er sofort eingerichtet."""
        name = (name or "").strip()
        if not name:
            raise RuntimeError(_("Please enter a name"))
        devices = [d for d in devices if d]
        self._check_outputs(devices, virtual)
        m = self._cfg_mix(self.new_mix_id(name))
        m.update({"name": name, "icon": icon or "wave", "color": color or "#22c55e", "devices": devices,
                  "virtual": bool(virtual), "exclude": list(exclude)})
        self._save_cfg()
        if self.is_active():
            self._add_mix_live(self.mix(m["id"]))
        return self.mix(m["id"])

    def update_mix(self, mid, **changes):
        """Name, Symbol, Farbe, Geräte (devices) oder virtuelle Quelle eines Mixes ändern."""
        cur = self.mix(mid)
        if cur is None:
            raise RuntimeError(_("Mix {id} not found").format(id=mid))
        if "name" in changes and not (changes["name"] or "").strip():
            raise RuntimeError(_("Please enter a name"))
        devices = [d for d in changes.get("devices", cur["devices"]) if d]
        virtual = True if cur["id"] == "stream" else bool(changes.get("virtual", cur["virtual"]))
        if cur["id"] != "monitor":  # der Persönliche Mix nimmt ohne Gerät das Standardgerät
            self._check_outputs(devices, virtual)
        rebuild = devices != cur["devices"] or virtual != cur["virtual"]
        m = self._cfg_mix(cur["id"])
        for k in ("name", "icon", "color"):
            if changes.get(k):
                m[k] = changes[k].strip() if k == "name" else changes[k]
        m["devices"], m["virtual"] = devices, virtual
        self._save_cfg()
        if cur["id"] == "monitor" and devices:  # erstes Gerät = Abhörgerät
            st = self._state()
            if st.get("monitor_output") != devices[0]:
                st["monitor_output"] = devices[0]
                self._save_state(st)
                rebuild = True
        if rebuild and self.is_active():
            self.rebuild_mixes()
        return self.mix(cur["id"])

    def remove_mix(self, mid):
        m = self.mix(mid)
        if m is None:
            raise RuntimeError(_("Mix {id} not found").format(id=mid))
        if m["builtin"]:
            raise RuntimeError(_("The Personal Mix and the Stream Mix cannot be deleted"))
        mx = self.cfg.data.setdefault("mixer", {})
        mx["mixes"] = [x for x in mx.get("mixes") or [] if x.get("id") != m["id"]]
        self._save_cfg()
        if self.is_active():
            self._remove_mix_live(m["id"])

    def set_member(self, cid, mid, on):
        """Kanal in einen Mix aufnehmen (on=True) oder herausnehmen – sofort, ohne Neuaufbau."""
        m, c = self.mix(mid), self.channel(cid)
        if m is None or c is None:
            raise RuntimeError(_("Channel {id}/{mix} not found").format(id=cid, mix=mid))
        cm = self._cfg_mix(m["id"])
        excl = [x for x in m["exclude"] if x != c["id"]]
        if not on:
            excl.append(c["id"])
        cm["exclude"] = excl
        self._save_cfg()
        if not self.is_active():
            return
        st = self._state()
        loops = st.setdefault("loops", {}).setdefault(c["id"], {})
        if on and m["id"] not in loops:
            src = (st.get("sources") or {}).get(c["id"])
            target = self._mix_target(m, self._ctx(st))
            if src and target:
                mod = self._load_loop(c, m, src, target)
                loops[m["id"]] = mod
                st.setdefault("channels", {}).setdefault(c["id"], []).append(mod)
                st["modules"] = (st.get("modules") or []) + [mod]
                self._save_state(st)
                if c["id"] == "mic" and m["id"] == "monitor":
                    time.sleep(0.2)
                    self.set_mute("mic", "monitor", True)  # eigene Stimme nicht ungewollt auf dem Kopfhörer
        elif not on and m["id"] in loops:
            mod = loops.pop(m["id"])
            try:
                self.run(["unload-module", str(mod)])
            except RuntimeError:
                pass
            st["channels"][c["id"]] = [x for x in st["channels"].get(c["id"], []) if x != mod]
            st["modules"] = [x for x in st.get("modules") or [] if x != mod]
            self._save_state(st)

    # ----------------------------------------------------------------- Status
    def _state(self):
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, st):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        # erst in eine Nachbardatei, dann umbenennen: wer gleichzeitig liest, sieht nie eine halbe Datei
        tmp = self.state_file.with_name(self.state_file.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(st, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def is_active(self):
        names = {b.get("name") for b in system._pactl_blocks("sinks")}
        return STREAM_SINK in names

    def default_sink(self):
        return self.run(["get-default-sink"])

    def default_source(self):
        return self.run(["get-default-source"])

    # ------------------------------------------------------- Auf- und Abbau
    def setup(self):
        """Kanäle und Mixe anlegen (vorhandene FreeWave-Module werden vorher entfernt)."""
        if not system.have("pactl"):
            raise RuntimeError(_("pactl is missing (package pulseaudio-utils) – PipeWire with pipewire-pulse is required"))
        old = self._state()
        self.teardown(restore_default=False)
        prev_default = old.get("previous_default") or self.default_sink()
        if _ours(prev_default):
            prev_default = self.default_sink()
        monitor_out = self.conf.get("monitor_output") or prev_default
        sources = {b.get("name") for b in system._pactl_blocks("sources")}
        mic = self.conf.get("mic") or ("linewave_micfilter" if "linewave_micfilter" in sources else self.default_source())
        ctx = {"monitor": monitor_out, "mic": mic}
        st = {"modules": [], "channels": {}, "loops": {}, "sources": {}, "mixes": {}, "previous_default": prev_default,
              "monitor_output": monitor_out, "mic": mic, "created": time.time(), "listen": "monitor"}
        for m in self.mixes():
            mods = self._load_mix(m, ctx)
            st["mixes"][m["id"]] = mods
            st["modules"] += mods
        for c in self.all_channels():
            self._record_channel(st, c, self._load_channel(c, ctx))
        first = self.app_channels()[0] if self.app_channels() else None
        if first:
            try:
                self.run(["set-default-sink", self.sink_name(first["id"])])
            except RuntimeError as e:  # z. B. ohne Session-Manager (WirePlumber)
                log.warning(_("Default output not set to FreeWave {name}: {error}").format(name=first["name"], error=e))
        self._save_state(st)
        time.sleep(0.3)
        # eigenes Mikrofon standardmäßig nicht auf den Kopfhörer
        try:
            self.set_mute("mic", "monitor", True)
        except RuntimeError:
            pass
        self._restore_levels(old.get("levels") or {})
        self._restore_masters(old.get("masters") or {})
        self._moved.clear()
        log.info(_("Audio mixer set up: {count} channels, monitor → {output}").format(
            count=len(self.channels()), output=monitor_out))
        return st["modules"]

    def _restore_levels(self, levels):
        for key, (vol, mute) in levels.items():
            if "_" not in key:
                continue
            cid, mix = key.rsplit("_", 1)
            try:
                self.set_volume(cid, mix, vol)
                self.set_mute(cid, mix, mute)
            except RuntimeError:
                pass

    def _restore_masters(self, masters):
        for cid, (vol, mute) in masters.items():
            try:
                self.set_master(cid, vol)
                self.set_master_mute(cid, mute)
            except RuntimeError:
                pass

    def _loop_props(self, cid, name, mix):
        m = self.mix(mix)
        label = _("FreeWave {name} → {mix}").format(name=name, mix=m["name"] if m else mix)
        return _props(linewave_loop=self.loop_name(cid, mix), media_name=label, node_description=label)

    def _source_of(self, c, ctx):
        """Quelle, aus der ein Kanal in die Mixe geht (ggf. über seine Effekte)."""
        if c["id"] == "mic":
            src = ctx.get("mic")
        elif c.get("kind", "apps") == "input":
            src = c.get("source")
        else:
            src = self.sink_name(c["id"]) + ".monitor"
        if not src or (c["id"] == "mic" and src.endswith(".monitor")):
            if c["id"] == "mic":
                self.noise.stop()
            return None
        if c["id"] == "mic":  # Rauschunterdrückung vor den Effekten des Mikrofons
            src = self.noise.source_for(src)
        return self.fx.source_for(c, src)

    # ------------------------------------------------------- Mix-Aufbau
    def mix_devices(self, m, st=None):
        """Geräte, auf die ein Mix geht (Persönlicher Mix ohne eigene Angabe: das Monitor-Ausgabegerät)."""
        if m["devices"]:
            return list(m["devices"])
        if m["id"] == "monitor":
            st = self._state() if st is None else st
            dev = st.get("monitor_output") or self.conf.get("monitor_output")
            if not dev:
                try:
                    dev = self.default_sink()
                except RuntimeError:
                    dev = ""
            return [dev] if dev else []
        return []

    @staticmethod
    def mix_sink(m):
        """Null-Sink eines Mixes (auch wenn er gerade direkt auf ein Gerät geht)."""
        return STREAM_SINK if m["id"] == "stream" else MIX_SINK_PREFIX + m["id"]

    def _direct(self, m, devices):
        return not m["virtual"] and len(devices) == 1

    def _mix_target(self, m, ctx):
        """Wohin die Kanäle eines Mixes spielen: direkt auf das Gerät oder in den Mix-Sink."""
        devices = ctx.get("devices", {}).get(m["id"])
        if devices is None:
            devices = [ctx["monitor"]] if m["id"] == "monitor" and not m["devices"] else list(m["devices"])
        if self._direct(m, devices):
            return devices[0]
        if not devices and not m["virtual"]:
            return None
        return self.mix_sink(m)

    def _load_mix(self, m, ctx):
        """Null-Sink eines Mixes und je Ausgabegerät ein Loopback (entfällt, wenn der Mix direkt aufs Gerät geht)."""
        devices = [ctx["monitor"]] if m["id"] == "monitor" and not m["devices"] else list(m["devices"])
        ctx.setdefault("devices", {})[m["id"]] = devices
        if self._direct(m, devices) or (not devices and not m["virtual"]):
            return []
        sink = self.mix_sink(m)
        desc = _("FreeWave Stream Mix") if m["id"] == "stream" else "FreeWave " + m["name"]
        mods = [self.run(["load-module", "module-null-sink", f"sink_name={sink}",
                          "sink_properties=" + _props(device_description=desc, node_description=desc)])]
        for i, dev in enumerate(devices):
            lid = f"{PREFIX}mixout-{m['id']}-{i}"
            label = f"{desc} → {dev}"
            mods.append(self.run(["load-module", "module-loopback", f"source={sink}.monitor", f"sink={dev}",
                                  "latency_msec=20", "source_dont_move=true",
                                  "sink_input_properties=" + _props(linewave_loop=lid, media_name=label,
                                                                    node_description=label)]))
        return mods

    def _load_loop(self, c, m, src, target):
        args = ["load-module", "module-loopback", f"source={src}", f"sink={target}", "latency_msec=20"]
        if src.endswith(".monitor"):
            args.append("source_dont_move=true")
        return self.run(args + ["sink_input_properties=" + self._loop_props(c["id"], c["name"], m["id"])])

    def _load_channel(self, c, ctx):
        """Null-Sink (bei Programm-Kanälen) und je Mix ein Loopback.

        Gibt {"sink": Modul oder None, "loops": {Mix: Modul}, "src": Quelle} zurück."""
        out = {"sink": None, "loops": {}, "src": None}
        cid = c["id"]
        if c.get("kind", "apps") == "apps" and cid != "mic":
            desc = "FreeWave " + c["name"]
            # monitor.channel-volumes: die Lautstärke des Kanals (Hauptregler) wirkt auch auf die Mixe
            out["sink"] = self.run(["load-module", "module-null-sink", f"sink_name={self.sink_name(cid)}",
                                    "sink_properties=" + _props({"monitor.channel-volumes": "true"},
                                                                device_description=desc, node_description=desc)])
        src = self._source_of(c, ctx)
        out["src"] = src
        if src:
            for m in self.mixes():
                if cid in m["exclude"]:
                    continue
                target = self._mix_target(m, ctx)
                if target:
                    out["loops"][m["id"]] = self._load_loop(c, m, src, target)
        return out

    @staticmethod
    def _record_channel(st, c, res):
        mods = ([res["sink"]] if res["sink"] else []) + list(res["loops"].values())
        st.setdefault("channels", {})[c["id"]] = mods
        st.setdefault("loops", {})[c["id"]] = dict(res["loops"])
        st.setdefault("sources", {})[c["id"]] = res["src"]
        st["modules"] = (st.get("modules") or []) + mods

    def _ctx(self, st=None):
        st = st or self._state()
        sources = {b.get("name") for b in system._pactl_blocks("sources")}
        mic = self.conf.get("mic") or st.get("mic") or (
            "linewave_micfilter" if "linewave_micfilter" in sources else self.default_source())
        return {"monitor": st.get("monitor_output") or self.default_sink(), "mic": mic}

    def _add_live(self, c, levels=None):
        st = self._state()
        if "loops" not in st:  # Zustand einer älteren Version: alles neu aufbauen
            self.setup()
            return
        self._record_channel(st, c, self._load_channel(c, self._ctx(st)))
        self._save_state(st)
        time.sleep(0.2)
        for mix, (vol, mute) in (levels or {}).items():
            try:
                self.set_volume(c["id"], mix, vol)
                self.set_mute(c["id"], mix, mute)
            except RuntimeError:
                pass
        if c["id"] == "mic" and not levels:
            try:
                self.set_mute("mic", "monitor", True)
            except RuntimeError:
                pass

    def _unload_channel(self, cid):
        """Module eines Kanals entladen; gibt seine Lautstärken zurück."""
        mix_ids = self.mix_ids()
        levels = {mix: v for key, v in self.levels().items() for mix in mix_ids if key == f"{cid}_{mix}"}
        st = self._state()
        mods = (st.get("channels") or {}).pop(cid, None)
        (st.get("loops") or {}).pop(cid, None)
        (st.get("sources") or {}).pop(cid, None)
        if mods is None:  # ältere Zustandsdatei ohne Zuordnung: Module über ihre Namen suchen
            mods = []
            for b in system._pactl_blocks("modules"):
                arg = b.get("argument", "")
                if (f"sink_name={self.sink_name(cid)} " in arg + " "
                        or any(self.loop_name(cid, m) in arg for m in mix_ids)) and b.get("index"):
                    mods.append(b["index"])
        for mid in reversed(mods):
            try:
                self.run(["unload-module", str(mid)])
            except RuntimeError:
                pass
        st["modules"] = [m for m in (st.get("modules") or []) if m not in mods]
        self._save_state(st)
        return levels

    def rebuild_channel(self, cid):
        """Kanal neu einrichten (z. B. nach Umbenennen oder geänderten Effekten); Lautstärken bleiben."""
        c = self.channel(cid)
        if c is None:
            raise RuntimeError(_("Channel {id} not found").format(id=cid))
        masters = {k: v for k, v in self.masters().items() if k == cid}
        levels = self._unload_channel(cid)
        self.fx.stop(cid)
        self._add_live(c, levels)
        self._restore_masters(masters)

    def _add_mix_live(self, m):
        """Nur den neuen Mix einrichten; alle anderen Verbindungen laufen ungestört weiter."""
        st = self._state()
        if "loops" not in st:
            self.rebuild_mixes()
            return
        ctx = self._ctx(st)
        mods = self._load_mix(m, ctx)
        st.setdefault("mixes", {})[m["id"]] = mods
        st["modules"] = (st.get("modules") or []) + mods
        target = self._mix_target(m, ctx)
        for c in self.all_channels():
            src = (st.get("sources") or {}).get(c["id"])
            if not src or not target or c["id"] in m["exclude"]:
                continue
            mod = self._load_loop(c, m, src, target)
            st["loops"].setdefault(c["id"], {})[m["id"]] = mod
            st.setdefault("channels", {}).setdefault(c["id"], []).append(mod)
            st["modules"].append(mod)
        self._save_state(st)

    def _remove_mix_live(self, mid):
        """Nur die Verbindungen des gelöschten Mixes entfernen."""
        st = self._state()
        if "loops" not in st:
            self.rebuild_mixes()
            return
        if st.get("listen") == mid:
            self._stop_listen(st)
        gone = []
        for cid, loops in (st.get("loops") or {}).items():
            mod = loops.pop(mid, None)
            if mod is not None:
                gone.append(mod)
                st["channels"][cid] = [x for x in st["channels"].get(cid, []) if x != mod]
        gone += list(reversed((st.get("mixes") or {}).pop(mid, None) or []))
        for mod in gone:
            try:
                self.run(["unload-module", str(mod)])
            except RuntimeError:
                pass
        st["modules"] = [x for x in st.get("modules") or [] if x not in gone]
        self._save_state(st)

    def rebuild_mixes(self):
        """Mixe neu aufbauen (neuer/gelöschter Mix, andere Geräte). Kanal-Sinks und Programme bleiben,
        Lautstärken werden übernommen."""
        st = self._state()
        if "loops" not in st:  # Zustand einer älteren Version
            self.setup()
            return
        levels = self.levels()
        self._stop_listen(st)
        gone = []
        for cid, loops in (st.get("loops") or {}).items():
            for mod in loops.values():
                gone.append(mod)
            st["channels"][cid] = [x for x in st["channels"].get(cid, []) if x not in loops.values()]
            st["loops"][cid] = {}
        for mods in (st.get("mixes") or {}).values():
            gone += list(reversed(mods))
        for mod in gone:
            try:
                self.run(["unload-module", str(mod)])
            except RuntimeError:
                pass
        st["modules"] = [x for x in st.get("modules") or [] if x not in gone]
        st["mixes"] = {}
        ctx = self._ctx(st)
        for m in self.mixes():
            mods = self._load_mix(m, ctx)
            st["mixes"][m["id"]] = mods
            st["modules"] += mods
        for c in self.all_channels():
            src = (st.get("sources") or {}).get(c["id"])
            if not src:
                continue
            for m in self.mixes():
                target = self._mix_target(m, ctx)
                if c["id"] in m["exclude"] or not target:
                    continue
                mod = self._load_loop(c, m, src, target)
                st["loops"].setdefault(c["id"], {})[m["id"]] = mod
                st["channels"].setdefault(c["id"], []).append(mod)
                st["modules"].append(mod)
        self._save_state(st)
        time.sleep(0.2)
        if "mic_monitor" not in levels:
            try:
                self.set_mute("mic", "monitor", True)
            except RuntimeError:
                pass
        self._restore_levels(levels)

    def teardown(self, restore_default=True):
        st = self._state()
        if st.get("modules"):
            st["levels"] = self.levels()
            try:
                st["masters"] = self.masters()
            except RuntimeError:
                pass
        for mid in reversed(st.get("modules") or []):
            try:
                self.run(["unload-module", str(mid)])
            except RuntimeError:
                pass
        # Reste früherer Läufe (z. B. nach Absturz) und Abhören/Parken
        for b in system._pactl_blocks("modules"):
            args = " ".join(b.get("props", {}).values()) + " " + b.get("argument", "")
            if any(p in args for p in (PREFIX,) + LEGACY_PREFIXES) and b.get("index"):
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
        st["channels"] = {}
        for k in ("loops", "sources", "mixes", "listen_mods", "parked"):
            st.pop(k, None)
        st["listen"] = "monitor"
        self._save_state(st)
        self.fx.stop_all()
        self.noise.stop()

    # ------------------------------------------------------ Lautstärke/Mute
    def _loop_input(self, cid, mix):
        name = self.loop_name(cid, mix)
        blocks = system._pactl_blocks("sink-inputs")
        _remember_loops(blocks)
        for b in blocks:
            if _loop_id(b["props"]) == name:
                return b
        return None

    def _duck(self):
        """Laufendes Auto-Ducking (im FreeWave-Host oder über dessen Zustandsdatei) oder None."""
        if self.ducker is not None:
            return self.ducker
        return DuckFile.load()

    def get(self, cid, mix):
        """(Lautstärke, stumm). Während Auto-Ducking gilt die Lautstärke des Nutzers, nicht die abgesenkte."""
        b = self._loop_input(cid, mix)
        if not b:
            return (None, False)
        vol = b.get("volume", 0)
        duck = self._duck()
        base = duck.base(f"{cid}_{mix}") if duck else None
        return (vol if base is None else base), bool(b.get("mute"))

    def _mixes_of(self, cid, mix):
        """Die Mixe hinter einer Angabe: "both"/"all" = alle Mixe, in denen der Kanal ist."""
        if mix in ("both", "all"):
            ids = self.channel_mixes(cid)
            if not ids:
                raise RuntimeError(_("Channel {id} is in no mix").format(id=cid))
            return ids
        return (mix,)

    def _loops(self, cid, mix, hint=None, recent=False):
        """[(Mix, Loopback-Block)] aller Mixe hinter mix – aus einer einzigen pactl-Abfrage.

        recent: eine höchstens RECENT_S alte Abfrage genügt (Tasten am Deck: sofort schreiben,
        statt erst PipeWire auszulesen – der Sekundentakt der Tasten hält sie frisch)."""
        mixes = self._mixes_of(cid, mix)
        known = _recent_loops() if recent else None
        if known is not None and all(self.loop_name(cid, m) in known for m in mixes):
            return [(m, known[self.loop_name(cid, m)]) for m in mixes]
        with system.pactl_snapshot():
            out = [(m, self._loop_input(cid, m)) for m in mixes]
        for m, b in out:
            if not b:
                raise RuntimeError(hint.format(id=cid, mix=m) if hint else
                                   _("Channel {id}/{mix} not found").format(id=cid, mix=m))
        return out

    def set_volume(self, cid, mix, pct):
        duck = self._duck()
        for m, b in self._loops(cid, mix, _("Channel {id}/{mix} not found – is the mixer set up? (freewave mixer setup)")):
            self._write_volume(b["index"], f"{cid}_{m}", pct, duck)

    def _write_volume(self, index, key, pct, duck=None):
        pct = max(0, min(MAX_VOLUME, int(pct)))
        if duck is not None and duck.base(key) is not None:
            pct = duck.user_set(key, pct)  # abgesenkt schreiben, Nutzerwert merken
        self.run(["set-sink-input-volume", index, f"{pct}%"])

    def _write_all(self, cid, mix, write, recent=False, loops=None):
        """write(Mix, Block) für alle Mixe hinter mix – gleichzeitig (je ein pactl-Aufruf). Stimmt ein
        gemerkter Index nicht mehr (Loopback neu angelegt), einmal frisch auslesen und wiederholen."""
        loops = loops if loops is not None else self._loops(cid, mix, recent=recent)
        errors = []

        def one(m, b):
            try:
                write(m, b)
            except RuntimeError as e:
                errors.append(e)
        if len(loops) == 1:
            one(*loops[0])
        else:
            threads = [threading.Thread(target=one, args=lb, daemon=True) for lb in loops]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        if errors:
            if recent:
                _RECENT["at"] = 0.0
                return self._write_all(cid, mix, write, recent=False)
            raise errors[0]
        return loops

    def change_volume(self, cid, mix, delta, recent=False):
        """Lautstärke in allen Mixen um delta ändern; gibt die neuen Werte zurück ({Mix: Prozent}).
        recent: zuletzt gelesene Loopbacks genügen (Tasten am Deck – sofort schreiben)."""
        duck = self._duck()
        out = {}

        def write(m, b):
            key = f"{cid}_{m}"
            base = duck.base(key) if duck else None
            pct = max(0, min(MAX_VOLUME, int((b.get("volume", 0) if base is None else base) + delta)))
            self._write_volume(b["index"], key, pct, duck)
            b["volume"] = pct if base is None else b.get("volume", 0)  # gemerkten Stand nachführen
            out[m] = pct
        self._write_all(cid, mix, write, recent=recent)
        return out

    def set_mute(self, cid, mix, mute, recent=False):
        """Stumm schalten; "toggle" schaltet alle Mixe gemeinsam um. Gibt den neuen Zustand zurück."""
        loops = self._loops(cid, mix, recent=recent)
        if mute == "toggle":
            mute = not all(b.get("mute") for _m, b in loops)
        mute = bool(mute)

        def write(_m, b):
            self.run(["set-sink-input-mute", b["index"], "1" if mute else "0"])
            b["mute"] = mute
        self._write_all(cid, mix, write, recent=recent, loops=loops)
        return mute

    def raw_levels(self, with_index=False):
        """Tatsächliche Lautstärken der Loopbacks: {"music_monitor": (Prozent, stumm[, Index])}."""
        out = {}
        mixes = set(self.mix_ids())
        for b in system._pactl_blocks("sink-inputs"):
            name = _loop_id(b["props"])
            key = name[len(PREFIX):] if name.startswith(PREFIX) else ""
            if "_" in key and key.rsplit("_", 1)[1] in mixes:
                v = (b.get("volume", 0), bool(b.get("mute")))
                out[key] = v + (b["index"],) if with_index else v
        return out

    # ------------------------------------------------------- Hauptregler
    def _master_target(self, cid, st=None):
        """("sink"|"source", Name) – wo der Hauptregler eines Kanals sitzt.

        Programm-Kanäle: ihr Null-Sink (wirkt dank monitor.channel-volumes auf alle Mixe);
        Eingänge und Mikrofon: die Quelle, aus der der Kanal in die Mixe geht."""
        c = self.channel(cid)
        if c is None:
            raise RuntimeError(_("Channel {id} not found").format(id=cid))
        if c.get("kind", "apps") == "apps" and c["id"] != "mic":
            return "sink", self.sink_name(c["id"])
        st = self._state() if st is None else st
        src = (st.get("sources") or {}).get(c["id"])
        if not src:
            raise RuntimeError(_("Channel {id} has no input").format(id=cid))
        return "source", src

    def masters(self):
        """Hauptregler je Kanal: {Kanal: (Prozent, stumm)}."""
        st = self._state()
        sinks = {b.get("name"): b for b in system._pactl_blocks("sinks")}
        sources = {b.get("name"): b for b in system._pactl_blocks("sources")}
        out = {}
        for c in self.all_channels():
            try:
                kind, name = self._master_target(c["id"], st)
            except RuntimeError:
                continue
            b = (sinks if kind == "sink" else sources).get(name)
            if b is not None:
                out[c["id"]] = (int(b.get("volume", 100)), bool(b.get("mute")))
        return out

    def set_master(self, cid, pct):
        kind, name = self._master_target(cid)
        pct = max(0, min(MAX_VOLUME, int(pct)))
        self.run([f"set-{kind}-volume", name, f"{pct}%"])
        return pct

    def set_master_mute(self, cid, mute):
        kind, name = self._master_target(cid)
        self.run([f"set-{kind}-mute", name, "toggle" if mute == "toggle" else ("1" if mute else "0")])

    # ------------------------------------------------------------ Abhören
    def listening(self):
        """Mix, der gerade auf dem Abhörgerät (Gerät des Persönlichen Mixes) läuft."""
        return self._state().get("listen") or "monitor"

    def can_listen(self, mid):
        """Abhören geht bei Mixen mit eigenem Sink (virtuell oder mehrere Geräte)."""
        m = self.mix(mid)
        if m is None:
            return False
        if m["id"] == "monitor":
            return True
        return not self._direct(m, self.mix_devices(m)) and bool(m["virtual"] or m["devices"])

    def _stop_listen(self, st):
        for mod in st.pop("listen_mods", None) or []:
            try:
                self.run(["unload-module", str(mod)])
            except RuntimeError:
                pass
            st["modules"] = [x for x in st.get("modules") or [] if x != mod]
        parked = st.pop("parked", None) or {}
        if parked:
            for b in system._pactl_blocks("sink-inputs"):
                lid = _loop_id(b["props"])
                if lid in parked:
                    try:
                        self.run(["move-sink-input", b["index"], parked[lid]])
                    except RuntimeError:
                        pass
        st["listen"] = "monitor"

    def listen(self, mid):
        """Einen Mix auf dem Gerät des Persönlichen Mixes abhören ("monitor" = wieder normal).

        Der Persönliche Mix wartet so lange auf einem stillen Sink."""
        m = self.mix(mid)
        if m is None:
            raise RuntimeError(_("Mix {id} not found").format(id=mid))
        st = self._state()
        self._stop_listen(st)
        if m["id"] != "monitor":
            if not self.can_listen(m["id"]):
                raise RuntimeError(_("“{name}” plays directly on a device – listen to it there").format(
                    name=m["name"]))
            device = st.get("monitor_output") or self.default_sink()
            mods = [self.run(["load-module", "module-null-sink", f"sink_name={PARK_SINK}",
                              "sink_properties=" + _props(device_description="FreeWave (parked)",
                                                          node_description="FreeWave (parked)")])]
            parked = {}
            mon = self.mix("monitor")
            direct = self._direct(mon, self.mix_devices(mon, st))
            sinks = {b["index"]: b.get("name", "") for b in system._pactl_blocks("sinks")}
            for b in system._pactl_blocks("sink-inputs"):
                lid = _loop_id(b["props"])
                ours = (direct and lid.endswith("_monitor")) or lid.startswith(f"{PREFIX}mixout-monitor-")
                if ours and sinks.get(b.get("sink"), device) == device:
                    parked[lid] = device
                    self.run(["move-sink-input", b["index"], PARK_SINK])
            label = _("FreeWave listen: {mix}").format(mix=m["name"])
            mods.append(self.run(["load-module", "module-loopback", f"source={self.mix_sink(m)}.monitor",
                                  f"sink={device}", "latency_msec=20", "source_dont_move=true",
                                  "sink_input_properties=" + _props(linewave_loop=LISTEN_LOOP, media_name=label,
                                                                    node_description=label)]))
            st["listen_mods"], st["parked"], st["listen"] = mods, parked, m["id"]
            st["modules"] = (st.get("modules") or []) + mods
        self._save_state(st)
        return st["listen"]

    def level_sources(self):
        """Pegel je Mix: {"mix:<id>": Quelle} (Mix-Sink oder bei direkten Mixen das Gerät)."""
        st = self._state()
        out = {}
        for m in self.mixes():
            devices = self.mix_devices(m, st)
            if self._direct(m, devices):
                out["mix:" + m["id"]] = devices[0] + ".monitor"
            elif devices or m["virtual"]:
                out["mix:" + m["id"]] = self.mix_sink(m) + ".monitor"
        return out

    def levels(self):
        """Lautstärken und Stummschaltung je Kanal und Mix (beim Auto-Ducking die Werte des Nutzers)."""
        out = self.raw_levels()
        duck = self._duck()
        if duck:
            for key, (vol, mute) in out.items():
                base = duck.base(key)
                if base is not None:
                    out[key] = (base, mute)
        return out

    # ------------------------------------------------------------- Szenen
    def scenes(self):
        """Gespeicherte Mixer-Szenen: [{"id", "name", "levels": {Kanal: {Mix: [Prozent, stumm]}}}]."""
        return [s for s in (self.conf.get("scenes") or []) if isinstance(s, dict) and s.get("id")]

    def scene(self, ref):
        """Szene nach id oder Name (ohne Groß-/Kleinschreibung) oder None."""
        ref = str(ref or "").strip().lower()
        if not ref:
            return None
        for s in self.scenes():
            if s["id"].lower() == ref:
                return s
        return next((s for s in self.scenes() if str(s.get("name", "")).lower() == ref), None)

    def _need_scene(self, ref):
        s = self.scene(ref)
        if s is None:
            raise RuntimeError(_("Scene “{name}” not found").format(name=ref))
        return s

    def _cfg_scenes(self):
        mx = self.cfg.data.setdefault("mixer", {})
        mx["scenes"] = self.scenes()
        return mx["scenes"]

    def snapshot(self):
        """Aktueller Mixer-Zustand für eine Szene: {Kanal: {Mix: [Prozent, stumm]}}."""
        out = {}
        for key, (vol, mute) in self.levels().items():
            cid, mix = key.rsplit("_", 1)
            out.setdefault(cid, {})[mix] = [int(vol), bool(mute)]
        return out

    def save_scene(self, name):
        """Aktuellen Zustand unter einem Namen speichern (gleicher Name = überschreiben)."""
        name = (name or "").strip()
        if not name:
            raise RuntimeError(_("Please enter a name"))
        snap = self.snapshot()
        if not snap:
            raise RuntimeError(_("Mixer not set up – 'freewave mixer setup'"))
        scenes = self._cfg_scenes()
        s = next((x for x in scenes if x.get("name", "").lower() == name.lower()), None)
        if s is None:
            base = re.sub(r"[^a-z0-9]+", "-", name.lower().replace("ä", "ae").replace("ö", "oe")
                          .replace("ü", "ue").replace("ß", "ss")).strip("-")[:24] or "scene"
            taken = {x["id"] for x in scenes}
            sid, n = base, 2
            while sid in taken:
                sid, n = f"{base}-{n}", n + 1
            s = {"id": sid, "name": name}
            scenes.append(s)
        s["name"] = name
        s["levels"] = snap
        self._save_cfg()
        return s

    def apply_scene(self, ref):
        """Szene anwenden: Lautstärken und Stummschaltungen aller gespeicherten Kanäle setzen."""
        s = self._need_scene(ref)
        live = self.raw_levels(with_index=True)
        if not live:
            raise RuntimeError(_("Mixer not set up – 'freewave mixer setup'"))
        duck = self._duck()
        for cid, mixes in (s.get("levels") or {}).items():
            for mix, val in (mixes or {}).items():
                cur = live.get(f"{cid}_{mix}")
                if cur is None or not isinstance(val, (list, tuple)) or len(val) < 2:
                    continue  # Kanal gibt es nicht mehr
                self._write_volume(cur[2], f"{cid}_{mix}", val[0], duck)
                if bool(val[1]) != cur[1]:
                    self.run(["set-sink-input-mute", cur[2], "1" if val[1] else "0"])
        return s

    def rename_scene(self, ref, name):
        name = (name or "").strip()
        if not name:
            raise RuntimeError(_("Please enter a name"))
        s = self._need_scene(ref)
        if any(x["id"] != s["id"] and x.get("name", "").lower() == name.lower() for x in self.scenes()):
            raise RuntimeError(_("A scene named “{name}” already exists").format(name=name))
        for x in self._cfg_scenes():
            if x["id"] == s["id"]:
                x["name"] = name
                s = x
        self._save_cfg()
        return s

    def delete_scene(self, ref):
        s = self._need_scene(ref)
        self.cfg.data.setdefault("mixer", {})["scenes"] = [x for x in self.scenes() if x["id"] != s["id"]]
        self._save_cfg()
        return s

    def active_scene(self, levels=None):
        """Die Szene, die dem aktuellen Zustand entspricht (±1 %), oder None."""
        levels = self.levels() if levels is None else levels
        if not levels:
            return None
        for s in self.scenes():
            items = [(f"{cid}_{mix}", v) for cid, mixes in (s.get("levels") or {}).items()
                     for mix, v in (mixes or {}).items()]
            items = [(k, v) for k, v in items if k in levels]
            if items and all(abs(levels[k][0] - v[0]) <= 1 and bool(levels[k][1]) == bool(v[1]) for k, v in items):
                return s
        return None

    # ---------------------------------------------------------- Ausgänge
    def outputs(self):
        """Echte Ausgabegeräte (ohne FreeWave-Kanäle)."""
        return [b for b in system._pactl_blocks("sinks") if not _ours(b.get("name", ""))]

    def set_monitor_output(self, device):
        """Monitor-Mix auf ein anderes Ausgabegerät legen (Name, Teil der Beschreibung oder 'next')."""
        outs = self.outputs()
        if not outs:
            raise RuntimeError(_("No output device found"))
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
                raise RuntimeError(_("Output device “{device}” not found").format(device=device))
        mon = self.mix("monitor")
        if mon["devices"]:  # Persönlicher Mix mit eigener Geräteliste: erstes Gerät tauschen
            devices = [target] + [d for d in mon["devices"][1:] if d != target]
            st["monitor_output"] = target
            self._save_state(st)
            self.update_mix("monitor", devices=devices)
            return target
        self._stop_listen(st)
        for b in system._pactl_blocks("sink-inputs"):
            name = _loop_id(b["props"])
            if name.startswith(PREFIX) and (name.endswith("_monitor") or name.startswith(f"{PREFIX}mixout-monitor-")):
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
        chans = {c["id"] for c in self.app_channels()}
        moved = 0
        for b in system._pactl_blocks("sink-inputs"):
            idx = b["index"]
            p = b["props"]
            if idx in self._moved or _is_ours(p):
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
                        log.debug(_("Moving {stream} failed: {error}").format(stream=idx, error=e))
                    break
        return moved

    def streams(self):
        """Wiedergabe-Streams der Programme mit ihrem Kanal (Kanal-id oder None = nicht im Mixer)."""
        sinks = {b["index"]: b.get("name", "") for b in system._pactl_blocks("sinks")}
        out = []
        for b in system._pactl_blocks("sink-inputs"):
            p = b["props"]
            if _is_ours(p):
                continue
            sink = sinks.get(b.get("sink", ""), "")
            cid = sink[len(PREFIX):] if sink.startswith(PREFIX) and sink != STREAM_SINK else None
            if cid and (sink.startswith(MIX_SINK_PREFIX) or sink == PARK_SINK):
                cid = None
            out.append({"index": b["index"], "channel": cid, "sink": sink,
                        "app": (p.get("application.name") or p.get("application.process.binary")
                                or p.get("node.name") or _("Stream {index}").format(index=b["index"])),
                        "key": (p.get("application.process.binary") or p.get("application.name") or "").lower()})
        return out

    def move_stream(self, index, cid, remember=""):
        """Einen Stream in einen Kanal verschieben; mit remember (Programmname) dauerhaft merken."""
        if cid not in {c["id"] for c in self.app_channels()}:
            raise RuntimeError(_("Unknown channel: {id}").format(id=cid))
        self.run(["move-sink-input", str(index), self.sink_name(cid)])
        if remember and self.cfg is not None and hasattr(self.cfg, "save"):
            self.cfg.data.setdefault("mixer", {}).setdefault("routes", {})[remember.lower()] = cid
            self.cfg.save()

    def assign(self, app, cid):
        """Programm dauerhaft einem Kanal zuordnen (laufende Streams werden sofort verschoben)."""
        c = self.channel(cid)
        if not c or c["id"] not in {a["id"] for a in self.app_channels()}:
            raise RuntimeError(_("Unknown channel: {id}").format(id=cid))
        cid = c["id"]
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
        pass  # Kanäle bleiben bestehen, damit der Ton beim Neustart von FreeWave nicht abreißt
