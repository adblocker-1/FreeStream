"""Pegelanzeige für Linewave: echte Audiopegel über `parec`.

Für jede beobachtete Quelle (Monitor eines Kanal-Sinks, Mikrofon, die beiden
Mixe) läuft ein kleiner `parec`-Prozess mit niedriger Rate (16 kHz, s16 mono,
25 Spitzenwerte pro Sekunde). Ein Lese-Thread je Prozess merkt sich den
letzten Spitzenwert; das Fenster fragt die Werte mit `read()` ab und bekommt
sie mit sanftem Abfall (wie ein Peak-Meter).

Läuft nur, solange das Fenster sichtbar ist – `stop()` beendet alle
Prozesse. Ohne `parec` (pulseaudio-utils) bleiben alle Pegel bei 0.

Zum Testen lässt sich statt `parec` eine eigene Quelle einsetzen:
`LevelMonitor(factory=lambda device: obj)` – obj braucht `.level` (0..1)
und `.stop()`.
"""
import array
import logging
import math
import shutil
import subprocess
import sys
import threading
import time

log = logging.getLogger("streamlin.levels")

RATE = 16000
HZ = 25
CHUNK = RATE // HZ * 2  # Bytes je Messung (s16 mono)
FLOOR_DB = -60.0
FALL_PER_S = 1.6  # so viel (von 0..1) fällt die Anzeige pro Sekunde höchstens


def peak_level(data):
    """Spitzenwert eines s16le-Blocks als Anzeigewert 0..1 (−60 dB … 0 dB)."""
    a = array.array("h", bytes(data[: len(data) // 2 * 2]))
    if sys.byteorder == "big":
        a.byteswap()
    peak = max((abs(x) for x in a), default=0) / 32768.0
    if peak <= 0:
        return 0.0
    db = 20 * math.log10(peak)
    return max(0.0, min(1.0, (db - FLOOR_DB) / -FLOOR_DB))


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
            self.proc = popen([exe, f"--device={device}", "--raw", "--format=s16le", f"--rate={RATE}",
                               "--channels=1", f"--latency-msec={1000 // HZ}"],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        except OSError as e:
            log.debug("parec %s: %s", device, e)
            self.proc = None
            return
        threading.Thread(target=self._read, name="linewave-level", daemon=True).start()

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
        for key in list(self.sources):
            dev, src = self.sources[key]
            if targets.get(key) != dev:
                src.stop()
                del self.sources[key]
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
        self.shown.clear()
        self._last = None


def channel_sources(mixer, channels, monitor_output=""):
    """Welche PipeWire-Quellen gemessen werden: je Kanal und die beiden Mixe.

    Schlüssel: Kanal-id, "mix:monitor" und "mix:stream".
    """
    from .mixer import STREAM_SINK
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
    if monitor_output:
        out["mix:monitor"] = monitor_output + ".monitor"
    out["mix:stream"] = STREAM_SINK + ".monitor"
    return out
