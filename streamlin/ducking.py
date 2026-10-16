"""Auto-Ducking für Linewave: Kanäle leiser, solange du ins Mikrofon sprichst.

Ein kleiner Mess-Prozess (levels.ParecSource) beobachtet das Mikrofon. Liegt
der Pegel über der Schwelle, senkt Linewave die gewählten Kanäle (Standard:
Musik) in beiden Mixen um den eingestellten Betrag ab – mit weichem Einsatz
(Attack), Haltezeit und weichem Ausklingen (Release). Danach steht die
Lautstärke wieder exakt auf dem Wert des Nutzers.

Mit Reglern kämpft das Ducking nicht: es merkt sich je Kanal/Mix den Wert
des Nutzers („base“) und senkt relativ dazu ab. Regler im Fenster, Tasten in
LinDeck und die Kommandozeile setzen während des Absenkens den Nutzerwert
(mixer.Mixer.set_volume → user_set); fremde Programme (pavucontrol) erkennt
der Abgleich mit den tatsächlichen Lautstärken.

Konfiguration (config.json, "mixer"):
  "ducking": {"enabled": false, "channels": ["music"], "amount_db": 12,
              "threshold_db": -30, "attack_ms": 150, "hold_ms": 800, "release_ms": 900}

Die Logik (Envelope, Ducker.step) läuft ohne PipeWire testbar: Pegelquelle
und Uhr lassen sich ersetzen.
"""
import json
import logging
import threading
import time

from . import levels, paths
from .mixer import DUCK_FILE, DUCK_USER_FILE, MAX_VOLUME, MIXES, duck_volume

log = logging.getLogger("streamlin.ducking")

DEFAULTS = {"enabled": False, "channels": ["music"], "amount_db": 12, "threshold_db": -30,
            "attack_ms": 150, "hold_ms": 800, "release_ms": 900}
AMOUNTS = (6, 12, 18, 24)
STEP_S = 0.04          # 25 Schritte pro Sekunde
CHECK_S = 0.5          # Abgleich mit den tatsächlichen Lautstärken
MIC_S = 5.0            # Mikrofonquelle neu bestimmen
ACTIVE_S = 2.0         # prüfen, ob der Mixer läuft


def settings(cfg):
    """Ducking-Einstellungen mit Standardwerten."""
    mx = (cfg.get("mixer") if cfg is not None else None) or {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (mx.get("ducking") or {}).items() if v is not None})
    return out


def factor(amount_db, depth):
    """Faktor auf die PipeWire-Lautstärke (Prozent, kubische Kurve) für depth 0..1 der Absenkung."""
    return 10 ** (-float(amount_db) * max(0.0, min(1.0, depth)) / 60.0)


class Envelope:
    """Hüllkurve: depth 0 (nicht abgesenkt) … 1 (voll abgesenkt).

    step(level_db, dt): level_db ist der Mikrofonpegel (None = Stille).
    Über der Schwelle steigt depth in attack Sekunden auf 1, hält danach hold
    Sekunden und fällt in release Sekunden auf 0.
    """

    def __init__(self, threshold_db=-30.0, attack=0.15, hold=0.8, release=0.9):
        self.threshold_db, self.attack, self.hold, self.release = threshold_db, attack, hold, release
        self.depth = 0.0
        self.hold_left = 0.0

    def speaking(self, level_db):
        return level_db is not None and level_db >= self.threshold_db

    def step(self, level_db, dt):
        dt = max(0.0, dt)
        if self.speaking(level_db):
            self.hold_left = self.hold
            self.depth = 1.0 if self.attack <= 0 else min(1.0, self.depth + dt / self.attack)
        elif self.hold_left >= dt:
            self.hold_left -= dt  # Pause zwischen Wörtern: abgesenkt bleiben
        else:
            rest, self.hold_left = dt - self.hold_left, 0.0
            self.depth = 0.0 if self.release <= 0 else max(0.0, self.depth - rest / self.release)
        return self.depth


class Ducker:
    """Senkt Kanäle ab, solange das Mikrofon spricht (läuft als Thread im Linewave-Host).

    mixer: mixer.Mixer · level: Funktion () -> Pegel 0..1 (levels.py-Skala) oder None für
    die eingebaute parec-Messung · clock: Zeitquelle (Tests).
    """

    def __init__(self, mixer, cfg, level=None, level_factory=None, clock=time.monotonic):
        self.mixer, self.cfg, self.clock = mixer, cfg, clock
        self._level_fn = level
        self.meter = levels.LevelMonitor(factory=level_factory, clock=clock, fall_per_s=1000.0)
        self.env = Envelope()
        self.bases = {}     # "music_monitor" -> Lautstärke des Nutzers
        self.applied = {}   # "music_monitor" -> zuletzt geschriebene Lautstärke
        self.index = {}     # "music_monitor" -> Sink-Input-Index
        self.level = 0.0
        self.factor = 1.0
        self._lock = threading.RLock()
        self._running = False
        self._wake = threading.Event()
        self._thread = None
        self._mic = ""
        self._mic_at = -1e9
        self._check_at = -1e9
        self._active_at = -1e9
        self._active = False
        self._file_at = -1e9
        self._last = None
        self._known = set()
        mixer.ducker = self

    # ------------------------------------------------ Schnittstelle Mixer
    def base(self, key):
        with self._lock:
            return self.bases.get(key)

    def user_set(self, key, pct):
        """Nutzer stellt während des Absenkens eine Lautstärke ein: merken, abgesenkt zurückgeben."""
        with self._lock:
            self.bases[key] = int(pct)
            vol = duck_volume(pct, self.factor)
            self.applied[key] = vol
            self._write_file(force=True)
            return vol

    @property
    def depth(self):
        return self.env.depth

    @property
    def ducking(self):
        return self.env.depth > 0

    # ---------------------------------------------------------- Ablauf
    def start(self):
        if self._thread is not None:
            return
        self._running = True
        self._wake.clear()
        self._recover()
        self._thread = threading.Thread(target=self._loop, name="linewave-ducking", daemon=True)
        self._thread.start()

    def stop(self):
        """Thread beenden und abgesenkte Kanäle sofort auf den Nutzerwert zurückstellen."""
        self._running = False
        self._wake.set()
        t, self._thread = self._thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2)
        self.meter.stop()
        try:
            self.restore()
        except Exception as e:  # noqa: BLE001
            log.debug("ducking restore: %s", e)

    def _loop(self):
        while self._running:
            try:
                self.step()
            except Exception as e:  # noqa: BLE001
                log.debug("ducking: %s", e)
            self._wake.wait(STEP_S)  # Event statt time.sleep: endet sofort bei stop()

    def _settings(self):
        st = settings(self.cfg)
        env = self.env
        env.threshold_db = float(st["threshold_db"])
        env.attack = max(0.0, float(st["attack_ms"]) / 1000)
        env.hold = max(0.0, float(st["hold_ms"]) / 1000)
        env.release = max(0.0, float(st["release_ms"]) / 1000)
        return st

    def _enabled(self):
        return bool((self.cfg.get("mixer") or {}).get("enabled"))

    def _read_level(self, now):
        if self._level_fn is not None:
            lv = self._level_fn()
        else:
            if now - self._mic_at > MIC_S:
                self._mic_at = now
                try:
                    mic = self.mixer._ctx().get("mic") or ""
                except Exception:  # noqa: BLE001
                    mic = ""
                self._mic = "" if mic.endswith(".monitor") else mic
            self.meter.watch({"mic": self._mic})
            lv = self.meter.read().get("mic", 0.0)
        return float(lv or 0.0)

    def step(self, dt=None):
        """Ein Schritt: Pegel messen, Hüllkurve, Lautstärken schreiben. Gibt depth zurück."""
        now = self.clock()
        if dt is None:
            dt = 0.0 if self._last is None else max(0.0, min(0.5, now - self._last))
        self._last = now
        st = self._settings()
        on = bool(st["enabled"]) and self._enabled()
        if on and now - self._active_at > ACTIVE_S:
            self._active_at = now
            try:
                self._active = self.mixer.is_active()
            except Exception:  # noqa: BLE001
                self._active = False
        if not on or not self._active:
            if self._level_fn is None and self.meter.running:
                self.meter.stop()
            self.level = 0.0
            if self.bases:
                self.restore()  # ausgeschaltet oder Mixer weg: sofort zurück
            self.env.depth, self.env.hold_left = 0.0, 0.0
            return 0.0
        self.level = self._read_level(now)
        depth = self.env.step(levels.level_db(self.level), dt)
        keys = {f"{c}_{m}" for c in st["channels"] or [] if c != "mic" for m in MIXES}
        with self._lock:
            self._take_user_requests()
            for key in [k for k in self.bases if k not in keys]:  # Kanal abgewählt: zurückstellen
                self._restore_key(key)
            if depth <= 0:
                if self.bases:
                    self.restore()
                self.factor = 1.0
                return 0.0
            self.factor = factor(st["amount_db"], depth)
            if not keys <= self._known or now - self._check_at > CHECK_S:
                self._check_at = now
                self._known = set(keys)
                self._sync(keys)
            for key in keys:
                if key in self.bases:
                    self._apply(key, duck_volume(self.bases[key], self.factor))
            self._write_file()
        return depth

    def _sync(self, keys):
        """Tatsächliche Lautstärken lesen: neue Kanäle übernehmen, fremde Änderungen erkennen."""
        try:
            raw = self.mixer.raw_levels(with_index=True)
        except Exception as e:  # noqa: BLE001
            log.debug("ducking levels: %s", e)
            return
        for key in keys:
            cur = raw.get(key)
            if cur is None:
                self.bases.pop(key, None)
                self.applied.pop(key, None)
                continue
            vol, _mute, idx = cur
            self.index[key] = idx
            if key not in self.bases:
                self.bases[key] = int(vol)
                self.applied[key] = int(vol)
            elif abs(int(vol) - self.applied.get(key, vol)) > 1:
                # jemand anderes hat den Regler bewegt: das ist der neue (abgesenkte) Wert
                f = self.factor if self.factor > 0 else 1.0
                self.bases[key] = max(0, min(MAX_VOLUME, int(round(vol / f))))
                self.applied[key] = int(vol)

    def _apply(self, key, vol):
        if self.applied.get(key) == vol or key not in self.index:
            return
        try:
            self.mixer.run(["set-sink-input-volume", str(self.index[key]), f"{vol}%"])
            self.applied[key] = vol
        except RuntimeError:
            self.index.pop(key, None)
            self._known = set()  # Index neu bestimmen (Kanal neu aufgebaut)

    def _restore_key(self, key):
        base = self.bases.pop(key, None)
        applied = self.applied.pop(key, None)
        idx = self.index.get(key)
        if base is None or idx is None or applied == base:
            return
        try:
            self.mixer.run(["set-sink-input-volume", str(idx), f"{base}%"])
        except RuntimeError:
            # Index veraltet: über den Namen setzen (ohne Ducking-Umweg)
            try:
                b = self.mixer._loop_input(*key.rsplit("_", 1))
                if b:
                    self.mixer.run(["set-sink-input-volume", b["index"], f"{base}%"])
            except Exception:  # noqa: BLE001
                pass

    def restore(self):
        """Alle abgesenkten Kanäle exakt auf den Wert des Nutzers zurückstellen."""
        with self._lock:
            self._take_user_requests()
            for key in list(self.bases):
                self._restore_key(key)
            self.bases.clear()
            self.applied.clear()
            self._known = set()
            self.factor = 1.0
            self._remove_file()

    # ------------------------------------------- Zustand für andere Programme
    def _write_file(self, force=False):
        now = time.time()
        if not force and now - self._file_at < 1.0:
            return
        self._file_at = now
        try:
            paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = paths.STATE_DIR / (DUCK_FILE + ".tmp")
            tmp.write_text(json.dumps({"time": now, "factor": self.factor, "bases": self.bases,
                                       "applied": self.applied}), encoding="utf-8")
            tmp.replace(paths.STATE_DIR / DUCK_FILE)
        except OSError as e:
            log.debug("ducking state: %s", e)

    def _remove_file(self):
        for name in (DUCK_FILE, DUCK_USER_FILE):
            try:
                (paths.STATE_DIR / name).unlink()
            except OSError:
                pass
        self._file_at = -1e9

    def _take_user_requests(self):
        """Lautstärken, die LinDeck/Kommandozeile während des Absenkens gesetzt haben."""
        f = paths.STATE_DIR / DUCK_USER_FILE
        try:
            req = json.loads(f.read_text(encoding="utf-8"))
            f.unlink()
        except (OSError, ValueError):
            return
        for key, pct in (req or {}).items():
            if key in self.bases:
                self.bases[key] = max(0, min(MAX_VOLUME, int(pct)))
                self.applied[key] = duck_volume(self.bases[key], self.factor)

    def _recover(self):
        """Nach einem Absturz noch abgesenkte Kanäle zurückstellen (alte ducking.json)."""
        try:
            data = json.loads((paths.STATE_DIR / DUCK_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        try:
            raw = self.mixer.raw_levels(with_index=True)
        except Exception:  # noqa: BLE001
            raw = {}
        applied = data.get("applied") or {}
        with self._lock:
            for key, base in (data.get("bases") or {}).items():
                cur = raw.get(key)
                if cur is not None and abs(cur[0] - int(applied.get(key, cur[0]))) <= 1:
                    self.index[key] = cur[2]
                    self.bases[key] = int(base)
                    self.applied[key] = cur[0]
            self.restore()
