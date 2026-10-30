"""Soundboard von FreeWave: Klänge in die Mixe spielen (wie das Soundboard in Wave Link).

In config.json:

    "soundboard":         [{"id", "name", "file", "color", "volume", "loop"}, ...]
    "soundboard_play":    ""  (nicht ausgewählt) | "all" (alle Mixe) | Mix-id ("stream", "monitor", ...)
    "soundboard_monitor": ""  | Mix-id | "out:<Sink>" – selbst mithören (leerer Sink = Standardausgabe)
    "soundboard_volume":  80  (Gesamtlautstärke in Prozent)

Hinzugefügte Dateien werden nach ~/.local/share/freestream/sounds/ kopiert, damit das Soundboard
auch dann noch spielt, wenn die Originaldatei verschoben wird.

Abgespielt wird mit pw-play (bzw. paplay, mpv oder ffplay) direkt in den Sink des gewählten Mixes –
derselbe Sink, in den auch die Kanäle des Mixes spielen (bei einem Mix ohne eigenen Sink das Gerät).
Je Ziel läuft ein eigener Prozess; Schleifen ohne eigene Option des Players wiederholt eine kleine
Shell-Schleife, die beim Stoppen samt Player beendet wird (eigene Prozessgruppe). Ist kein Ziel gewählt,
spielt der Klang auf der Standardausgabe (wie in FreeWave in Rust).

Die Streams tragen die Property linewave.loop=linewave_soundboard: FreeWave zählt sie damit zu seinen
eigenen Streams (mixer._is_ours) – sie erscheinen nicht als Programme und werden nicht in Kanäle
verschoben. Dieselben Schlüssel und dieselbe Kennung nutzt FreeWave in Rust.
"""
import logging
import os
import shutil
import signal
import subprocess
import threading
import uuid
from pathlib import Path

from . import paths, system
from .i18n import _

log = logging.getLogger("freestream.soundboard")

EXTENSIONS = (".wav", ".mp3", ".ogg", ".flac")
COLORS = ["#2fc3ff", "#30d158", "#ffb020", "#ff8a3d", "#ff4d5a", "#ff6bd5", "#a66bff", "#6b74ff"]
DEFAULT_VOLUME = 80
KEYS = ("soundboard", "soundboard_play", "soundboard_monitor", "soundboard_volume")
STREAM_ID = "linewave_soundboard"  # Wert der Property linewave.loop (mixer.LOOP_PROP)
LOOP_PROP = "linewave.loop"


def sounds_dir():
    return Path(paths.DATA_DIR) / "sounds"


# ------------------------------------------------------------ Konfiguration
def _clamp(v, default=100):
    try:
        return max(0, min(100, int(v)))
    except (TypeError, ValueError):
        return default


def normalize(s, i=0):
    """Ein Eintrag mit allen Feldern (fehlende ergänzt, Werte in Grenzen)."""
    f = str(s.get("file") or "")
    return {"id": str(s.get("id") or uuid.uuid4().hex[:8]),
            "name": str(s.get("name") or Path(f).stem or _("Sound")),
            "file": f,
            "color": str(s.get("color") or COLORS[i % len(COLORS)]),
            "volume": _clamp(s.get("volume"), 100),
            "loop": bool(s.get("loop"))}


def sounds(data):
    """Alle Klänge aus den Konfigurationsdaten (dict wie Config.data)."""
    lst = data.get("soundboard") if isinstance(data, dict) else None
    return [normalize(s, i) for i, s in enumerate(lst or []) if isinstance(s, dict)]


def options(data):
    """(Abspielen in, Mithören in, Gesamtlautstärke)."""
    data = data if isinstance(data, dict) else {}
    vol = data.get("soundboard_volume")
    return (str(data.get("soundboard_play") or ""), str(data.get("soundboard_monitor") or ""),
            _clamp(vol, DEFAULT_VOLUME) if vol is not None else DEFAULT_VOLUME)


def import_file(src):
    """Datei in den Soundboard-Ordner kopieren (Name eindeutig); gibt den neuen Pfad zurück."""
    src = Path(src)
    if src.suffix.lower() not in EXTENSIONS:
        raise RuntimeError(_("Unsupported audio file: {name}").format(name=src.name))
    d = sounds_dir()
    if src.parent.resolve() == d.resolve():
        return src
    d.mkdir(parents=True, exist_ok=True)
    dst, n = d / src.name, 2
    while dst.exists():
        dst, n = d / f"{src.stem}-{n}{src.suffix}", n + 1
    shutil.copy2(src, dst)
    return dst


def _save(cfg, lst):
    cfg.data["soundboard"] = lst
    if hasattr(cfg, "save"):
        cfg.save()
    return lst


def add(cfg, src, name=None):
    """Klang hinzufügen (Datei wird kopiert); gibt den neuen Eintrag zurück."""
    lst = sounds(cfg.data)
    f = import_file(src)
    s = normalize({"name": name or Path(src).stem, "file": str(f)}, len(lst))
    lst.append(s)
    _save(cfg, lst)
    return s


def update(cfg, sid, **changes):
    lst = sounds(cfg.data)
    for i, s in enumerate(lst):
        if s["id"] == sid:
            s.update({k: v for k, v in changes.items() if k in ("name", "file", "color", "volume", "loop")})
            lst[i] = normalize(s, i)
            _save(cfg, lst)
            return lst[i]
    raise RuntimeError(_("Sound not found"))


def remove(cfg, sid):
    """Klang entfernen; die kopierte Datei wird gelöscht, wenn sie kein anderer Klang nutzt."""
    lst = sounds(cfg.data)
    gone = [s for s in lst if s["id"] == sid]
    rest = [s for s in lst if s["id"] != sid]
    _save(cfg, rest)
    for s in gone:
        f = Path(s["file"])
        try:
            if f.parent.resolve() == sounds_dir().resolve() and all(x["file"] != s["file"] for x in rest):
                f.unlink(missing_ok=True)
        except OSError:
            pass
    return rest


def set_options(cfg, play=None, monitor=None, volume=None):
    if play is not None:
        cfg.data["soundboard_play"] = play
    if monitor is not None:
        cfg.data["soundboard_monitor"] = monitor
    if volume is not None:
        cfg.data["soundboard_volume"] = _clamp(volume, DEFAULT_VOLUME)
    if hasattr(cfg, "save"):
        cfg.save()


# --------------------------------------------------------------- Ziele
def mix_targets(mixer):
    """{Mix-id: Sink oder None}: wohin ein Klang für diesen Mix spielt (wie die Kanäle des Mixes).

    Ohne laufenden Mixer {} – dann spielt „Mithören“ auf die Standardausgabe."""
    try:
        if not mixer.is_active():
            return {}
    except Exception:  # noqa: BLE001
        return {}
    st = mixer._state()
    out = {}
    for m in mixer.mixes():
        devices = mixer.mix_devices(m, st)
        if mixer._direct(m, devices):
            out[m["id"]] = devices[0]
        elif devices or m["virtual"]:
            out[m["id"]] = mixer.mix_sink(m)
        else:
            out[m["id"]] = None
    return out


def targets(play, monitor, mixes):
    """Sinks, in die ein Klang gespielt wird ("" = Standardausgabe), ohne Doppelte.

    play: "" | "all" | Mix-id; monitor: "" | Mix-id | "out:<Sink>"; mixes: mix_targets().
    Ohne jedes Ziel: [""] – der Klang spielt auf der Standardausgabe."""
    out = []
    if play == "all":
        out += [t for t in mixes.values() if t]
    elif play and mixes.get(play):
        out.append(mixes[play])
    if monitor.startswith("out:"):
        out.append(monitor[4:])
    elif monitor:
        mid = monitor[4:] if monitor.startswith("mix:") else monitor  # „mix:“ von frühen Testversionen
        if mixes.get(mid):
            out.append(mixes[mid])
    if not out:
        out.append("")
    seen, res = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            res.append(t)
    return res


# ------------------------------------------------------------- Abspielen
def pick_player(path, have=None):
    """Player für eine Datei: pw-play/paplay für wav, ogg, flac; für mp3 lieber mpv/ffplay."""
    have = have or system.have
    order = ("mpv", "ffplay", "pw-play", "paplay") if str(path).lower().endswith(".mp3") else \
        ("pw-play", "paplay", "mpv", "ffplay")
    return next((p for p in order if have(p)), None)


def command(player, path, volume=100, sink="", loop=False):
    """(argv, zusätzliche Umgebung) zum Abspielen von path mit volume (0–100) in sink ("" = Standard)."""
    vol = _clamp(volume)
    env = {}
    path = str(path)
    if player == "pw-play":
        argv = ["pw-play", "-P", f'{{ {LOOP_PROP} = "{STREAM_ID}" }}'] + (["--target", sink] if sink else []) + \
            [f"--volume={vol / 100:.2f}", path]
    elif player == "paplay":
        argv = ["paplay", f"--property={LOOP_PROP}={STREAM_ID}"] + ([f"--device={sink}"] if sink else []) + \
            [f"--volume={int(vol / 100 * 65536)}", path]
    elif player in ("mpv", "ffplay"):
        env["PULSE_PROP"] = f"{LOOP_PROP}={STREAM_ID}"  # Property für libpulse (nicht jede Ausgabe reicht sie weiter)
        if player == "mpv":
            argv = ["mpv", "--no-video", "--really-quiet", f"--volume={vol}"] + \
                ([f"--audio-device=pulse/{sink}"] if sink else []) + (["--loop-file=inf"] if loop else []) + [path]
        else:
            argv = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-volume", str(vol)] + \
                (["-loop", "0"] if loop else []) + [path]
            if sink:
                env["PULSE_SINK"] = sink
        return argv, env
    else:
        raise RuntimeError(_("No audio player found (install mpv, ffplay or pipewire-bin)"))
    if loop:  # pw-play/paplay können keine Schleife: so lange wiederholen, bis gestoppt wird
        argv = ["sh", "-c", 'while "$@"; do :; done', "sh"] + argv
    return argv, env


class Player:
    """Laufende Klänge je Klang-id (ein Prozess je Ziel)."""

    def __init__(self, popen=None, have=None):
        self._popen = popen or subprocess.Popen
        self._have = have
        self._procs = {}
        self._lock = threading.Lock()

    def play(self, sid, path, volume, sinks, loop=False):
        """Klang starten (ein bereits laufender derselben id wird vorher beendet). Gibt die Prozesszahl zurück."""
        self.stop(sid)
        if not path or not Path(path).is_file():
            raise RuntimeError(_("Audio file not found: {path}").format(path=path))
        player = pick_player(path, self._have)
        if player is None:
            raise RuntimeError(_("No audio player found (install mpv, ffplay or pipewire-bin)"))
        procs = []
        for sink in sinks:
            argv, extra = command(player, path, volume, sink, loop)
            env = dict(os.environ, **extra) if extra else None
            try:
                procs.append(self._popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, env=env, start_new_session=True))
            except OSError as e:
                log.warning(_("Audio playback failed: {error}").format(error=e))
        with self._lock:
            self._procs[sid] = procs
        return len(procs)

    def is_playing(self, sid):
        with self._lock:
            procs = [p for p in self._procs.get(sid, []) if p.poll() is None]
            if procs:
                self._procs[sid] = procs
            else:
                self._procs.pop(sid, None)
            return bool(procs)

    def playing(self):
        return {sid for sid in list(self._procs) if self.is_playing(sid)}

    def stop(self, sid=None):
        with self._lock:
            keys = [sid] if sid is not None else list(self._procs)
            procs = [p for k in keys for p in self._procs.pop(k, [])]
        for p in procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)  # samt Shell-Schleife und Player
                except (OSError, AttributeError):
                    try:
                        p.terminate()
                    except OSError:
                        pass

    def toggle(self, sound, volume, sinks):
        """Spielt der Klang, wird er gestoppt, sonst gestartet. True = spielt jetzt."""
        if self.is_playing(sound["id"]):
            self.stop(sound["id"])
            return False
        vol = sound["volume"] * volume / 100
        return self.play(sound["id"], sound["file"], vol, sinks, sound["loop"]) > 0


# ------------------------------------------------ Tasten (FreeDeck-Aktion)
_DECK_PLAYER = Player()


def sound_choices():
    """[(id, Name)] für das Auswahlfeld der Tasten-Aktion (liest config.json direkt)."""
    from .config import _read_file
    try:
        data = _read_file()
    except (OSError, ValueError):
        data = {}
    lst = [(s["id"], s["name"]) for s in sounds(data)]
    return lst or [("", _("(no sounds in the FreeWave soundboard yet)"))]


def play_from_config(sid, cfg=None, player=None):
    """Klang sid mit den Soundboard-Einstellungen aus config.json abspielen bzw. stoppen (Tasten-Aktion)."""
    from .config import _read_file
    from .mixer import Mixer
    data = _read_file()
    s = next((x for x in sounds(data) if x["id"] == sid), None)
    if s is None:
        raise RuntimeError(_("Sound not found"))
    play, monitor, volume = options(data)
    sinks = targets(play, monitor, mix_targets(Mixer(cfg)))
    return (player or _DECK_PLAYER).toggle(s, volume, sinks)
