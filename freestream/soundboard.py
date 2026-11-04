"""Soundboard von FreeWave: Klänge in die Mixe spielen (wie das Soundboard in Wave Link).

In config.json:

    "soundboard":         [{"id", "name", "file", "color", "volume", "loop", "start", "end"}, ...]
    "soundboard_play":    ""  (nicht ausgewählt) | "all" (alle Mixe) | Mix-id ("stream", "monitor", ...)
    "soundboard_monitor": ""  | Mix-id | "out:<Sink>" – selbst mithören (leerer Sink = Standardausgabe)
    "soundboard_volume":  80  (Gesamtlautstärke in Prozent)

"start"/"end" (Sekunden, optional) schneiden den Klang zu (Trimmen). Da pw-play und paplay nicht
springen können, wird für einen zugeschnittenen WAV-Klang eine gekürzte Kopie geschrieben
(STATE_DIR/soundboard-trim/) und diese gespielt; andere Formate spielen ungekürzt.

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
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
import wave
from array import array
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
ACTION_UUID = "com.lindeck.freewave.soundboard"  # FreeDeck-Taste „Soundboard: Klang abspielen“
PEAKS = 120  # Balken der Wellenform-Vorschau
MIN_LENGTH = 0.05  # kürzester zugeschnittener Klang (Sekunden)


def sounds_dir():
    return Path(paths.DATA_DIR) / "sounds"


def trim_dir():
    """Ordner für die zugeschnittenen Kopien (werden bei Bedarf neu geschrieben)."""
    return Path(paths.STATE_DIR) / "soundboard-trim"


# ------------------------------------------------------------ Konfiguration
def _clamp(v, default=100):
    try:
        return max(0, min(100, int(v)))
    except (TypeError, ValueError):
        return default


def _secs(v):
    """Zeitpunkt in Sekunden (≥ 0) oder None."""
    if isinstance(v, bool):
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return round(v, 3) if v == v and 0 <= v < 1e7 else None


def normalize(s, i=0):
    """Ein Eintrag mit allen Feldern (fehlende ergänzt, Werte in Grenzen).

    "start"/"end" stehen nur drin, wenn der Klang zugeschnitten ist (Anfang > 0, Ende nach dem Anfang)."""
    f = str(s.get("file") or "")
    out = {"id": str(s.get("id") or uuid.uuid4().hex[:8]),
           "name": str(s.get("name") or Path(f).stem or _("Sound")),
           "file": f,
           "color": str(s.get("color") or COLORS[i % len(COLORS)]),
           "volume": _clamp(s.get("volume"), 100),
           "loop": bool(s.get("loop"))}
    start, end = _secs(s.get("start")), _secs(s.get("end"))
    if start:
        out["start"] = start
    if end is not None and end - (start or 0) >= MIN_LENGTH:
        out["end"] = end
    return out


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
            s.update({k: v for k, v in changes.items()
                      if k in ("name", "file", "color", "volume", "loop", "start", "end")})
            if "file" in changes and "start" not in changes and "end" not in changes:
                s.pop("start", None)  # neue Datei: der Zuschnitt der alten passt nicht mehr
                s.pop("end", None)
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
        _drop_trimmed(s["id"])
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


# ------------------------------------------------- Wellenform und Zuschnitt
_INFO = {}  # (Pfad, mtime, Größe, n) -> {"duration", "peaks"} oder None
_INFO_LOCK = threading.Lock()


def _peak(data, width):
    """Größter Ausschlag (0 … 1) in einem Block PCM-Daten (little endian, width Bytes je Wert)."""
    if not data:
        return 0.0
    if width == 1:  # 8 Bit ohne Vorzeichen
        return max(abs(b - 128) for b in data) / 128
    if width == 3:  # 24 Bit: das oberste Byte reicht für die Vorschau
        a, full = array("b", data[2::3]), 128
    elif width in (2, 4):
        a = array("h" if width == 2 else "i")
        a.frombytes(data[:len(data) - len(data) % width])
        if sys.byteorder == "big":
            a.byteswap()
        full = 1 << (8 * width - 1)
    else:
        return 0.0
    if not a:
        return 0.0
    return min(1.0, max(max(a), -min(a)) / full)


def _analyze(path, n):
    with wave.open(str(path), "rb") as r:
        rate, frames, width = r.getframerate(), r.getnframes(), r.getsampwidth()
        if rate <= 0:
            return None
        frame_bytes = width * r.getnchannels()
        # je Balken höchstens ~64 KB lesen: große Dateien nur stichprobenweise
        take = max(1, min(frames // n or 1, (64 << 10) // max(1, frame_bytes)))
        peaks = []
        for i in range(n if frames else 0):
            r.setpos(min(frames - 1, i * frames // n))
            peaks.append(_peak(r.readframes(take), width))
    top = max(peaks) if peaks else 0
    return {"duration": frames / rate, "peaks": [round(p / top, 3) if top else 0.0 for p in peaks]}


def analyze(path, n=PEAKS):
    """Länge (Sekunden) und Wellenform (n Werte 0 … 1) einer WAV-Datei; None für andere Formate oder bei
    Fehlern. Zwischengespeichert je Pfad und Änderungszeit."""
    if not path or not str(path).lower().endswith(".wav"):
        return None
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    key = (str(path), st.st_mtime_ns, st.st_size, n)
    with _INFO_LOCK:
        if key in _INFO:
            return _INFO[key]
    try:
        info = _analyze(path, n)
    except (wave.Error, EOFError, OSError, ValueError) as e:
        log.debug("Wellenform %s: %s", path, e)
        info = None
    with _INFO_LOCK:
        if len(_INFO) > 256:
            _INFO.clear()
        _INFO[key] = info
    return info


def trim_range(sound, duration):
    """(Anfang, Ende) in Sekunden, auf die Länge duration begrenzt."""
    start = min(max(0.0, float(sound.get("start") or 0)), duration)
    end = sound.get("end")
    end = duration if end is None else min(max(0.0, float(end)), duration)
    if end - start < MIN_LENGTH:
        return 0.0, duration
    return start, end


def play_length(sound, info=None):
    """Wie lange der Klang spielt (zugeschnitten) in Sekunden, oder None, wenn unbekannt."""
    info = info if info is not None else analyze(sound.get("file") or "")
    if not info:
        return None
    start, end = trim_range(sound, info["duration"])
    return end - start


def write_trimmed(src, dst, start, end):
    """Ausschnitt start … end (Sekunden) einer WAV-Datei nach dst schreiben; gibt die Zahl der Frames zurück."""
    with wave.open(str(src), "rb") as r:
        rate, frames = r.getframerate(), r.getnframes()
        ch, width = r.getnchannels(), r.getsampwidth()
        a = max(0, min(frames, int(round(start * rate))))
        b = max(a, min(frames, int(round(end * rate))))
        if a < frames:
            r.setpos(a)
        data = r.readframes(b - a) if b > a else b""
    dst = Path(dst)
    tmp = dst.with_name(dst.name + ".tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(data)
    os.replace(tmp, dst)
    return len(data) // max(1, ch * width)


def _fnv(text):
    """FNV-1a (64 Bit) – derselbe Dateiname der Kopie wie in FreeWave in Rust."""
    h = 0xcbf29ce484222325
    for b in text.encode("utf-8"):
        h = ((h ^ b) * 0x100000001b3) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


def _safe_id(sid):
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(sid or "")) or "sound"


def _drop_trimmed(sid, keep=None):
    """Zugeschnittene Kopien eines Klangs löschen (außer keep)."""
    d = trim_dir()
    if not d.is_dir():
        return
    for f in d.glob(f"{_safe_id(sid)}-*.wav"):
        if f.name != keep:
            try:
                f.unlink()
            except OSError:
                pass


def playable(sound):
    """Datei, die für den Klang gespielt wird: bei einem zugeschnittenen WAV die gekürzte Kopie."""
    f = sound.get("file") or ""
    if sound.get("start") is None and sound.get("end") is None:
        return f
    info = analyze(f)
    if not info:
        return f  # Trimmen geht nur mit WAV: ungekürzt spielen
    start, end = trim_range(sound, info["duration"])
    if start <= 0 and end >= info["duration"]:
        return f
    st = os.stat(f)
    name = f"{_safe_id(sound.get('id'))}-{_fnv(f'{f}|{st.st_mtime_ns}|{st.st_size}|{start:.3f}|{end:.3f}')}.wav"
    dst = trim_dir() / name
    if not dst.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        _drop_trimmed(sound.get("id"), keep=name)  # ältere Zuschnitte desselben Klangs
        write_trimmed(f, dst, start, end)
    return str(dst)


def fmt_time(sec):
    """„0:07“ (gerundet; ein kurzer Klang zeigt mindestens 0:01)."""
    s = int(round(sec or 0))
    if sec and sec > 0:
        s = max(1, s)
    return f"{s // 60}:{s % 60:02d}"


def fmt_precise(sec, digits=2):
    """„0:03.65“ – Felder des Zuschnitts (digits Nachkommastellen)."""
    cs = int(round(max(0.0, float(sec or 0)) * 10 ** digits))
    m, rest = divmod(cs, 60 * 10 ** digits)
    s, frac = divmod(rest, 10 ** digits)
    return f"{m}:{s:02d}.{frac:0{digits}d}" if digits else f"{m}:{s:02d}"


def parse_time(text):
    """„1:02.5“, „62,5“ oder „0:00.25“ -> Sekunden; None, wenn nicht lesbar."""
    t = str(text or "").strip().replace(",", ".")
    if not t:
        return None
    try:
        if ":" in t:
            m, s = t.rsplit(":", 1)
            v = int(m or 0) * 60 + float(s)
        else:
            v = float(t)
    except ValueError:
        return None
    return v if v >= 0 else None


# ------------------------------------------------ Tasten auf dem Deck
def deck_keys(store_root=None):
    """{Klang-id: (Seite, Taste)}: erste FreeDeck-Taste „Soundboard: Klang abspielen“ je Klang.

    Seite und Taste zählen ab 1 (Tasten zeilenweise wie auf dem Gerät), Profile in der Reihenfolge des
    Profilordners."""
    from .profiles import ProfileStore
    out = {}
    try:
        store = ProfileStore(store_root)
    except OSError:
        return out
    for p in store.profiles.values():
        try:
            cols = p.geometry.cols or 5
            pages = p.pages()
        except (OSError, ValueError, KeyError, TypeError):
            continue
        for n, pg in enumerate(pages, 1):
            for (c, r), a in sorted(pg.keypad.items(), key=lambda kv: (kv[0][1], kv[0][0])):
                if a.get("UUID") != ACTION_UUID:
                    continue
                st = a.get("Settings") if isinstance(a.get("Settings"), dict) else {}
                sid = str(st.get("sound") or "")
                if sid and sid not in out:
                    out[sid] = (n, r * cols + c + 1)
    return out


def key_text(pos, short=True):
    """„Key 2“ (kurz, Seite 1) bzw. „Page 2 · Key 3“."""
    page, key = pos
    if short and page == 1:
        return _("Key {n}").format(n=key)
    return _("Page {page} · Key {key}").format(page=page, key=key)


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
        self._started = {}  # Klang-id -> Startzeit (time.monotonic) für den Fortschritt
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
            self._started[sid] = time.monotonic()
        return len(procs)

    def elapsed(self, sid):
        """Sekunden seit dem Start des Klangs (None, wenn er nicht spielt)."""
        with self._lock:
            t = self._started.get(sid) if self._procs.get(sid) else None
        return None if t is None else time.monotonic() - t

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
            for k in keys:
                self._started.pop(k, None)
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
        path = sound["file"]
        try:
            path = playable(sound)  # zugeschnitten: die gekürzte Kopie
        except (OSError, wave.Error, EOFError, ValueError) as e:
            log.warning(_("Audio playback failed: {error}").format(error=e))
        return self.play(sound["id"], path, vol, sinks, sound["loop"]) > 0


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
