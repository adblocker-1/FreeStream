"""Wave-Link-Ersatz auf Basis von PipeWire (über pipewire-pulse/pactl).

Wie Elgato Wave Link: Programme landen in virtuellen Kanälen (System, Musik,
Browser, Voice Chat, Spiel, Effekte, Mikrofon). Jeder Kanal hat zwei
unabhängige Lautstärken/Stummschaltungen:

  * Monitor-Mix – was du über Kopfhörer/Lautsprecher hörst
  * Stream-Mix  – was OBS/Discord als Quelle „Linewave Stream-Mix“ bekommt

Aufbau:
    Programm ─► Kanal (Null-Sink „Linewave <Kanal>“) ┬─► Loopback ─► Kopfhörer      (Monitor-Mix)
                                                      └─► Loopback ─► Stream-Mix-Sink (Stream-Mix)
    Mikrofon ─────────────────────────────────────────┬─► Loopback ─► Stream-Mix-Sink
                                                      └─► Loopback ─► Kopfhörer (standardmäßig stumm)

Der Stream-Mix ist ein Null-Sink; seine Monitor-Quelle wird in OBS als
Audio-Eingang gewählt („Monitor of Linewave Stream-Mix“).

Konfiguration (config.json):
  "mixer": {
    "enabled": true,
    "channels": [{"id": "music", "name": "Musik"}, …],    (optional, Standard siehe unten)
        eigene Kanäle: {"id", "name", "icon", "color", "kind": "apps"|"input", "source": "<pactl-Quelle>"}
        „apps“  = Kanal für Programme (Null-Sink), „input“ = Eingangsgerät (z. B. 2. Mikrofon, Capture-Karte)
    "monitor_output": "",                                  (leer = aktuelles Standardgerät)
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
import re
import subprocess
import time

from . import paths, system
from .i18n import _

log = logging.getLogger("streamlin.mixer")

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
RESERVED_IDS = {"mic", "streammix", "micfilter", "stream", "monitor"}
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
DUCK_FILE = "ducking.json"
DUCK_USER_FILE = "ducking-user.json"


def _run(args):
    r = subprocess.run(["pactl"] + args, capture_output=True, text=True, timeout=5)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or _("pactl error")).strip())
    return r.stdout.strip()


def _props(**props):
    """Property-Liste als ein Modulargument, z. B. sink_properties=_props(...).

    Werte mit Leerzeichen („Linewave Musik“) müssen doppelt gequotet sein:
    außen für das Modulargument, innen für die Property-Liste – sonst schneiden
    PulseAudio und pipewire-pulse beim ersten Leerzeichen ab und jeder Kanal
    hieße nur „Linewave“.
    """
    inner = " ".join(f"{k.replace('_', '.')}='{str(v).replace(chr(39), '').replace(chr(34), '')}'"
                     for k, v in props.items())
    return f'"{inner}"'


def _ours(name):
    return name.startswith(PREFIX) or name.startswith(LEGACY_PREFIXES)


LOOP_PROP = "linewave.loop"


def _loop_id(props):
    """Kennung eines Linewave-Loopbacks („linewave_music_monitor“) oder ''.

    Steht in der eigenen Property linewave.loop; media.name trägt den lesbaren
    Namen („Linewave Musik → Monitor-Mix“), den Lautstärke-Regler anzeigen.
    Ältere Versionen hatten die Kennung direkt in media.name.
    """
    lid = props.get(LOOP_PROP) or props.get("media.name", "")
    return lid if _ours(lid) else ""


def _is_ours(props):
    return bool(_loop_id(props)) or _ours(props.get("media.name", ""))


class DuckFile:
    """Auto-Ducking des Linewave-Hosts aus Sicht anderer Programme (LinDeck, Kommandozeile).

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
    return max(0, min(150, int(round(base * factor))))


class Mixer:
    ducker = None  # Auto-Ducking im selben Prozess (Linewave-Host), sonst über DuckFile

    def __init__(self, cfg=None, run=None):
        self.cfg = cfg
        self.run = run or _run
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

    def add_channel(self, name, kind="apps", source="", icon="", color=""):
        """Eigenen Kanal anlegen; läuft der Mixer, wird er sofort eingerichtet."""
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
        """Kanäle und Mixe anlegen (vorhandene Linewave-Module werden vorher entfernt)."""
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
        modules, per_channel = [], {}
        ctx = {"monitor": monitor_out, "mic": mic}
        stream_desc = _("Linewave Stream Mix")
        modules.append(self.run(["load-module", "module-null-sink", f"sink_name={STREAM_SINK}",
                                 "sink_properties=" + _props(device_description=stream_desc,
                                                             node_description=stream_desc)]))
        for c in self.all_channels():
            mods = self._load_channel(c, ctx)
            per_channel[c["id"]] = mods
            modules += mods
        first = self.app_channels()[0] if self.app_channels() else None
        if first:
            try:
                self.run(["set-default-sink", self.sink_name(first["id"])])
            except RuntimeError as e:  # z. B. ohne Session-Manager (WirePlumber)
                log.warning(_("Default output not set to Linewave {name}: {error}").format(name=first["name"], error=e))
        self._save_state({"modules": modules, "channels": per_channel, "previous_default": prev_default,
                          "monitor_output": monitor_out, "mic": mic, "created": time.time()})
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
        log.info(_("Audio mixer set up: {count} channels, monitor → {output}").format(
            count=len(self.channels()), output=monitor_out))
        return modules

    def _loop_props(self, cid, name, mix):
        label = (_("Linewave {name} → Monitor Mix") if mix == "monitor" else
                 _("Linewave {name} → Stream Mix")).format(name=name)
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

    def _load_channel(self, c, ctx):
        """Null-Sink (bei Programm-Kanälen) und die beiden Loopbacks eines Kanals laden."""
        mods = []
        cid = c["id"]
        if c.get("kind", "apps") == "apps" and cid != "mic":
            desc = "Linewave " + c["name"]
            mods.append(self.run(["load-module", "module-null-sink", f"sink_name={self.sink_name(cid)}",
                                  "sink_properties=" + _props(device_description=desc, node_description=desc)]))
        src = self._source_of(c, ctx)
        if src:
            for mix, target in (("monitor", ctx["monitor"]), ("stream", STREAM_SINK)):
                args = ["load-module", "module-loopback", f"source={src}", f"sink={target}", "latency_msec=20"]
                if src.endswith(".monitor"):
                    args.append("source_dont_move=true")
                mods.append(self.run(args + ["sink_input_properties=" + self._loop_props(cid, c["name"], mix)]))
        return mods

    def _ctx(self, st=None):
        st = st or self._state()
        sources = {b.get("name") for b in system._pactl_blocks("sources")}
        mic = self.conf.get("mic") or st.get("mic") or (
            "linewave_micfilter" if "linewave_micfilter" in sources else self.default_source())
        return {"monitor": st.get("monitor_output") or self.default_sink(), "mic": mic}

    def _add_live(self, c, levels=None):
        st = self._state()
        mods = self._load_channel(c, self._ctx(st))
        st.setdefault("channels", {})[c["id"]] = mods
        st["modules"] = (st.get("modules") or []) + mods
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
        levels = {mix: v for key, v in self.levels().items() for mix in MIXES if key == f"{cid}_{mix}"}
        st = self._state()
        mods = (st.get("channels") or {}).pop(cid, None)
        if mods is None:  # ältere Zustandsdatei ohne Zuordnung: Module über ihre Namen suchen
            mods = []
            for b in system._pactl_blocks("modules"):
                arg = b.get("argument", "")
                if (f"sink_name={self.sink_name(cid)} " in arg + " " or self.loop_name(cid, "monitor") in arg
                        or self.loop_name(cid, "stream") in arg) and b.get("index"):
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
        levels = self._unload_channel(cid)
        self.fx.stop(cid)
        self._add_live(c, levels)

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
        self._save_state(st)
        self.fx.stop_all()
        self.noise.stop()

    # ------------------------------------------------------ Lautstärke/Mute
    def _loop_input(self, cid, mix):
        name = self.loop_name(cid, mix)
        for b in system._pactl_blocks("sink-inputs"):
            if _loop_id(b["props"]) == name:
                return b
        return None

    def _duck(self):
        """Laufendes Auto-Ducking (im Linewave-Host oder über dessen Zustandsdatei) oder None."""
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

    def set_volume(self, cid, mix, pct):
        duck = self._duck()
        for m in (MIXES if mix == "both" else (mix,)):
            b = self._loop_input(cid, m)
            if not b:
                raise RuntimeError(_("Channel {id}/{mix} not found – is the mixer set up? (linewave mixer setup)").format(
                    id=cid, mix=m))
            self._write_volume(b["index"], f"{cid}_{m}", pct, duck)

    def _write_volume(self, index, key, pct, duck=None):
        pct = max(0, min(150, int(pct)))
        if duck is not None and duck.base(key) is not None:
            pct = duck.user_set(key, pct)  # abgesenkt schreiben, Nutzerwert merken
        self.run(["set-sink-input-volume", index, f"{pct}%"])

    def change_volume(self, cid, mix, delta):
        for m in (MIXES if mix == "both" else (mix,)):
            vol, _m = self.get(cid, m)
            if vol is None:
                raise RuntimeError(_("Channel {id}/{mix} not found").format(id=cid, mix=m))
            self.set_volume(cid, m, vol + delta)

    def set_mute(self, cid, mix, mute):
        for m in (MIXES if mix == "both" else (mix,)):
            b = self._loop_input(cid, m)
            if not b:
                raise RuntimeError(_("Channel {id}/{mix} not found").format(id=cid, mix=m))
            self.run(["set-sink-input-mute", b["index"], "toggle" if mute == "toggle" else ("1" if mute else "0")])

    def raw_levels(self, with_index=False):
        """Tatsächliche Lautstärken der Loopbacks: {"music_monitor": (Prozent, stumm[, Index])}."""
        out = {}
        for b in system._pactl_blocks("sink-inputs"):
            name = _loop_id(b["props"])
            if name.startswith(PREFIX) and name.rsplit("_", 1)[-1] in MIXES:
                v = (b.get("volume", 0), bool(b.get("mute")))
                out[name[len(PREFIX):]] = v + (b["index"],) if with_index else v
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
            raise RuntimeError(_("Mixer not set up – 'linewave mixer setup'"))
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
            raise RuntimeError(_("Mixer not set up – 'linewave mixer setup'"))
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
        """Echte Ausgabegeräte (ohne Linewave-Kanäle)."""
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
        for b in system._pactl_blocks("sink-inputs"):
            name = _loop_id(b["props"])
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
        pass  # Kanäle bleiben bestehen, damit der Ton beim Neustart von Linewave nicht abreißt
