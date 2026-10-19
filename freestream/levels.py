"""Pegelanzeige für FreeWave: echte Audiopegel über `parec`.

Für jede beobachtete Quelle (Monitor eines Kanal-Sinks, Mikrofon, die beiden
Mixe) läuft ein kleiner `parec`-Prozess mit niedriger Rate (16 kHz, s16 mono,
25 Spitzenwerte pro Sekunde). Ein Lese-Thread je Prozess merkt sich den
letzten Spitzenwert; das Fenster fragt die Werte mit `read()` ab und bekommt
sie mit sanftem Abfall (wie ein Peak-Meter).

Läuft nur, solange das Fenster sichtbar ist – `stop()` beendet alle
Prozesse. Ohne `parec` (pulseaudio-utils) bleiben alle Pegel bei 0.

`parec` hängt fest an seiner Quelle: verschwindet sie (Kanal neu angelegt,
PipeWire neu gestartet), beendet er sich, statt zur Standardquelle zu
wechseln – `watch()` startet ihn dann neu (höchstens alle RESTART_S Sekunden).

Zum Testen lässt sich statt `parec` eine eigene Quelle einsetzen:
`LevelMonitor(factory=lambda device: obj)` – obj braucht `.level` (0..1)
und `.stop()`, optional `.ended` (True = neu starten).
"""
import array
import logging
import math
import shutil
import subprocess
import sys
import threading
import time

log = logging.getLogger("freestream.levels")

RATE = 16000
HZ = 25
CHUNK = RATE // HZ * 2  # Bytes je Messung (s16 mono)
FLOOR_DB = -60.0
FALL_PER_S = 1.6  # so viel (von 0..1) fällt die Anzeige pro Sekunde höchstens
RESTART_S = 2.0  # beendetes parec frühestens so lange nach dem Start neu starten (Quelle fehlt noch)


def peak_level(data):
    """Spitzenwert eines s16le-Blocks als Anzeigewert 0..1 (−60 dB … 0 dB)."""
    a = array.array("h", bytes(data[: len(data) // 2 * 2]))
    if sys.byteorder == "big":
        a.byteswap()
    if not a:
        return 0.0
    peak = max(max(a), -min(a)) / 32768.0  # max/min laufen in C – keine Python-Schleife je Messwert
    if peak <= 0:
        return 0.0
    db = 20 * math.log10(peak)
    return max(0.0, min(1.0, (db - FLOOR_DB) / -FLOOR_DB))


def post_fader(level, volume, muted):
    """Pegel nach dem Regler: Lautstärke in dB auf die Anzeige (−60…0 dB) umgerechnet.

    PipeWire/PulseAudio-Prozent sind kubisch (Amplitude = (pct/100)³): 50 % = −18 dB, 10 % = −60 dB."""
    if muted or not volume or volume <= 0 or level <= 0:
        return 0.0
    return max(0.0, min(1.0, level + 60 * math.log10(min(volume, 150) / 100) / -FLOOR_DB))


def level_db(level):
    """Anzeigewert 0..1 zurück in dB (None bei Stille)."""
    if level <= 0:
        return None
    return FLOOR_DB + level * -FLOOR_DB


class ParecSource:
    """Ein `parec`-Prozess für eine Quelle; .level ist der letzte Spitzenwert."""

    def __init__(self, device, exe=None, popen=subprocess.Popen):
        self.device = device
        self.level = 0.0
        self.proc = None
        exe = exe or shutil.which("parec")
        if not exe or not device:
            return
        try:
            # dont-reconnect: wird die Quelle entfernt, endet parec, statt still die Standardquelle zu messen
            self.proc = popen([exe, f"--device={device}", "--raw", "--format=s16le", f"--rate={RATE}",
                               "--channels=1", f"--latency-msec={1000 // HZ}",
                               "--property=node.dont-reconnect=true"],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        except OSError as e:
            log.debug("parec %s: %s", device, e)
            self.proc = None
            return
        threading.Thread(target=self._read, name="freewave-level", daemon=True).start()

    def _read(self):
        proc = self.proc
        try:
            while proc is not None and proc.poll() is None:
                data = proc.stdout.read(CHUNK)
                if not data:
                    break
                self.level = peak_level(data)
        except (OSError, ValueError):
            pass
        self.level = 0.0

    @property
    def ended(self):
        """parec hat sich beendet (Quelle entfernt oder noch nicht da, PipeWire neu gestartet)."""
        proc = self.proc
        return proc is not None and proc.poll() is not None

    def stop(self):
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        self.level = 0.0


def parec_available():
    return shutil.which("parec") is not None


class LevelMonitor:
    """Verwaltet die Pegel-Quellen: watch() legt fest, was gemessen wird, read() liefert die Werte."""

    def __init__(self, factory=None, clock=time.monotonic, fall_per_s=FALL_PER_S):
        self.factory = factory or ParecSource
        self.available = factory is not None or parec_available()
        self.clock = clock
        self.fall = fall_per_s
        self.sources = {}  # key -> (device, source)
        self.started = {}  # key -> Startzeit der Quelle (clock)
        self.shown = {}
        self._last = None

    @property
    def running(self):
        return bool(self.sources)

    def watch(self, targets):
        """targets: {key: device}. Startet neue Quellen und beendet nicht mehr benötigte."""
        if not self.available:
            return
        targets = {k: d for k, d in (targets or {}).items() if d}
        now = self.clock()
        for key in list(self.sources):
            dev, src = self.sources[key]
            # beendetes parec neu starten – fehlt die Quelle noch, nur alle RESTART_S Sekunden
            if targets.get(key) != dev or (now - self.started.get(key, now) >= RESTART_S
                                           and getattr(src, "ended", False)):
                src.stop()
                del self.sources[key]
                self.started.pop(key, None)
                self.shown.pop(key, None)
        for key, dev in targets.items():
            if key not in self.sources:
                try:
                    src = self.factory(dev)
                except Exception as e:  # noqa: BLE001
                    log.debug("level source %s: %s", dev, e)
                    continue
                if src is not None:
                    self.sources[key] = (dev, src)
                    self.started[key] = now

    def read(self):
        """{key: level 0..1} – steigt sofort, fällt höchstens um fall_per_s pro Sekunde."""
        now = self.clock()
        dt = 0.0 if self._last is None else max(0.0, now - self._last)
        self._last = now
        out = {}
        for key, (_dev, src) in self.sources.items():
            raw = max(0.0, min(1.0, float(getattr(src, "level", 0.0) or 0.0)))
            prev = self.shown.get(key, 0.0)
            val = raw if raw >= prev else max(raw, prev - self.fall * dt)
            self.shown[key] = val
            out[key] = val
        return out

    def stop(self):
        """Alle Messungen beenden (Fenster versteckt oder Programm beendet)."""
        for _dev, src in self.sources.values():
            try:
                src.stop()
            except Exception:  # noqa: BLE001
                pass
        self.sources.clear()
        self.started.clear()
        self.shown.clear()
        self._last = None


def channel_sources(mixer, channels, monitor_output=""):
    """Welche PipeWire-Quellen gemessen werden: je Kanal und je Mix.

    Schlüssel: Kanal-id und "mix:<Mix-id>" (z. B. "mix:monitor", "mix:stream", "mix:chat").
    """
    out = {}
    mic = ""
    try:
        mic = mixer._ctx().get("mic") or ""
    except Exception:  # noqa: BLE001
        pass
    for c in channels:
        cid = c["id"]
        if cid == "mic":
            src = mic if not mic.endswith(".monitor") else ""
        elif c.get("kind", "apps") == "input":
            src = c.get("source") or ""
        else:
            src = mixer.sink_name(cid) + ".monitor"
        if src:
            out[cid] = src
    try:
        out.update(mixer.level_sources())
    except Exception:  # noqa: BLE001
        from .mixer import STREAM_SINK
        if monitor_output:
            out["mix:monitor"] = monitor_output + ".monitor"
        out["mix:stream"] = STREAM_SINK + ".monitor"
    return out
