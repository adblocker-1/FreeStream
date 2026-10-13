"""Elgato-Wave-Mikrofone (Wave:1, Wave:3, Wave Neo, Wave XLR) unter Linux.

  * Gain und Stummschaltung: über die Standard-USB-Audio-Steuerung (pactl).
  * Low Cut (80/120 Hz) und Clipguard: in Wave Link sind das Software-Filter
    bzw. eine Pegelbegrenzung. LinDeck bildet sie mit einer PipeWire-
    Filterkette nach (Hochpass + Limiter) und stellt das Ergebnis als
    Mikrofon „LinDeck Wave“ bereit – dieses in Discord/OBS auswählen.
    Clipguard braucht den LADSPA-Limiter aus dem Paket swh-plugins.
  * Wave:3: Gain, Stumm, Low Cut, Clipguard, LEDs aus/umgekehrt, Kopfhörer und
    Mithören werden direkt im Mikrofon geschaltet (wave3hw.py) – dann ist keine
    Software-Filterkette nötig. Eine LED-Farbe hat das Wave:3 nicht.
  * Andere Wave-Modelle (Wave:1, Neo, XLR): Low Cut/Clipguard per Software,
    LED-Steuerung nicht möglich (Protokoll nicht dokumentiert).

Konfiguration (optional, config.json):
  "wave_mic": {"lowcut": false, "lowcut_freq": 80, "clipguard": false, "set_default": true}
"""
import json
import logging
import os
import shutil
import signal
import subprocess
from pathlib import Path

from . import paths, system

log = logging.getLogger("lindeck.wavemic")

# True im Programm „LinDeck Wave“: es besitzt die Software-Filterkette. Andere
# Prozesse (Stream-Deck-App, CLI) bitten es per SIGHUP, die Einstellungen neu zu laden.
OWNER = False
WAVE_APP_FILE = "lindeck-wave.json"

PROCESSED = "lindeck_wave"
LIMITER_PLUGIN = "fast_lookahead_limiter_1913"
LADSPA_DIRS = ["/usr/lib/ladspa", "/usr/lib/x86_64-linux-gnu/ladspa", "/usr/lib/aarch64-linux-gnu/ladspa",
               "/usr/local/lib/ladspa", "/usr/lib64/ladspa"]


def find_source():
    """pactl-Name des angeschlossenen Wave-Mikrofons (oder None)."""
    for b in system._pactl_blocks("sources"):
        name, desc = b.get("name", ""), b.get("description", "")
        p = b.get("props", {})
        text = " ".join([name, desc, p.get("device.vendor.name", ""), p.get("device.product.name", "")]).lower()
        if name.endswith(".monitor") or name.startswith("lindeck_"):
            continue
        if "wave" in text and ("elgato" in text or "0fd9" in text or "wave:" in text or "wave_" in text):
            return name
    return None


def limiter_available():
    return any((Path(d) / f"{LIMITER_PLUGIN}.so").exists() for d in LADSPA_DIRS)


def filter_conf(source: str, lowcut: bool, freq: int, clipguard: bool) -> str:
    """PipeWire-Konfiguration für `pipewire -c` (Filterkette Hochpass → Limiter)."""
    nodes, chain = [], []
    if lowcut:
        nodes.append(f'{{ type = builtin name = lowcut label = bq_highpass control = {{ "Freq" = {float(freq):.1f} '
                     '"Q" = 0.707 }} }}')
        chain.append(("lowcut:In", "lowcut:Out"))
    if clipguard:
        nodes.append(f'{{ type = ladspa name = limiter plugin = {LIMITER_PLUGIN} label = fastLookaheadLimiter '
                     'control = { "Input gain (dB)" = 0.0 "Limit (dB)" = -1.0 "Release time (s)" = 0.08 } }')
        chain.append(("limiter:Input 1", "limiter:Output 1"))
    if not nodes:
        raise ValueError("Weder Low Cut noch Clipguard aktiv")
    links = [f'{{ output = "{chain[i][1]}" input = "{chain[i + 1][0]}" }}' for i in range(len(chain) - 1)]
    return f"""# Von LinDeck erzeugt – Wave-Mikrofon mit Low Cut/Clipguard
context.properties = {{ log.level = 0 }}
context.spa-libs = {{
    audio.convert.* = audioconvert/libspa-audioconvert
    support.*       = support/libspa-support
}}
context.modules = [
    {{ name = libpipewire-module-rt args = {{ nice.level = -11 }} flags = [ ifexists nofail ] }}
    {{ name = libpipewire-module-protocol-native }}
    {{ name = libpipewire-module-client-node }}
    {{ name = libpipewire-module-adapter }}
    {{ name = libpipewire-module-filter-chain
        args = {{
            node.description = "LinDeck Wave"
            media.name       = "LinDeck Wave"
            filter.graph = {{
                nodes = [
                    {chr(10).join('                    ' + n for n in nodes).strip()}
                ]
                links = [ {' '.join(links)} ]
                inputs  = [ "{chain[0][0]}" ]
                outputs = [ "{chain[-1][1]}" ]
            }}
            audio.channels = 1
            audio.position = [ MONO ]
            capture.props = {{
                node.name      = "lindeck_wave_capture"
                node.passive   = true
                target.object  = "{source}"
                stream.dont-remix = true
            }}
            playback.props = {{
                node.name        = "{PROCESSED}"
                node.description = "LinDeck Wave"
                media.class      = Audio/Source
            }}
        }}
    }}
]
"""


class WaveMic:
    def __init__(self, cfg=None):
        self.cfg = cfg
        self.proc = None
        self.state_file = paths.STATE_DIR / "wavemic.json"
        self.conf_file = paths.STATE_DIR / "wave-filter.conf"
        self.settings = self._load()
        self.version = 0
        self._hw = None
        self._hw_error = ""

    def _load(self):
        base = {"lowcut": False, "lowcut_freq": 80, "clipguard": False, "set_default": True}
        base.update((self.cfg.get("wave_mic") if self.cfg else None) or {})
        try:
            base.update(json.loads(self.state_file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        return base

    def _save(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        keep = {k: self.settings[k] for k in ("lowcut", "lowcut_freq", "clipguard")}
        self.state_file.write_text(json.dumps(keep), encoding="utf-8")

    # ------------------------------------------------ Wave:3-Hardware
    def hw(self):
        """Wave3-Objekt, wenn ein Wave:3 angeschlossen und erreichbar ist, sonst None."""
        from . import wave3hw
        if self._hw is None and wave3hw.present():
            try:
                self._hw = wave3hw.Wave3()
                self._hw_error = ""
            except wave3hw.Wave3Error as e:
                if str(e) != self._hw_error:
                    log.warning("Wave:3: %s", e)
                self._hw_error = str(e)
        return self._hw

    def _hw_do(self, fn):
        from . import wave3hw
        try:
            return fn(self._hw)
        except wave3hw.Wave3Error as e:
            if self._hw:
                self._hw.close()
            self._hw = None
            raise RuntimeError(str(e)) from e
        finally:
            self.version += 1

    def is_on(self, key):
        """Low Cut, Clipguard oder LED-Schalter: aktueller Zustand."""
        if self.hw():
            try:
                return bool(self._hw.get()[key])
            except Exception:  # noqa: BLE001
                self._hw = None
                return None
        if key in ("leds_off", "leds_flip"):
            return None
        return bool(self.settings.get(key))

    def hw_set(self, key, value):
        if not self.hw():
            raise RuntimeError("Nur mit Elgato Wave:3 möglich" + (f" ({self._hw_error})" if self._hw_error else ""))
        return self._hw_do(lambda h: h.set(key, value))

    def status(self):
        st = {"source": find_source(), "hardware": False}
        if self.hw():
            try:
                st.update(self._hw.get())
                st["hardware"] = True
                st["api"] = "%d.%d" % self._hw.api
            except Exception as e:  # noqa: BLE001
                st["error"] = str(e)
        return st

    # ------------------------------------------------------- Gain / Mute
    def source(self):
        src = find_source()
        if not src:
            raise RuntimeError("Kein Elgato-Wave-Mikrofon gefunden")
        return src

    def gain(self):
        """(Prozent, stumm). Beim Wave:3 ist 100 % = 40 dB Hardware-Gain."""
        if self.hw():
            try:
                c = self._hw.get()
                return int(round(c["gain"] * 2.5)), c["mute"]
            except Exception:  # noqa: BLE001
                self._hw = None
        return system.device_volume("source", self.source())

    def change_gain(self, delta):
        if self.hw():
            return self._hw_do(lambda h: h.change("gain", delta * 0.4))
        src = self.source()
        cur, _ = system.device_volume("source", src)
        system.device_set("source", src, cur + delta)
        self.version += 1

    def set_gain(self, pct):
        if self.hw():
            return self._hw_do(lambda h: h.set("gain", pct * 0.4))
        system.device_set("source", self.source(), pct)
        self.version += 1

    def toggle_mute(self):
        if self.hw():
            return self._hw_do(lambda h: h.toggle("mute"))
        system.device_mute("source", self.source(), "toggle")
        self.version += 1

    # ------------------------------------------------ Low Cut / Clipguard
    def set(self, **kw):
        self.settings.update(kw)
        self._save()
        self.apply()
        self.version += 1

    def toggle(self, key):
        if self.hw():
            new = self._hw_do(lambda h: h.toggle(key))
            if key in ("lowcut", "clipguard"):
                self.settings[key] = bool(new)
                self._save()
            return new
        if key in ("leds_off", "leds_flip"):
            raise RuntimeError("LED-Steuerung gibt es nur für das Elgato Wave:3")
        self.set(**{key: not self.settings.get(key)})
        return self.settings[key]

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def apply(self):
        """Filterkette passend zu den Einstellungen starten, neu starten oder beenden."""
        self.stop()
        if self.hw():
            # Wave:3: Low Cut und Clipguard schaltet das Mikrofon selbst
            for key in ("lowcut", "clipguard"):
                try:
                    if bool(self._hw.get()[key]) != bool(self.settings.get(key)):
                        self._hw.set(key, bool(self.settings.get(key)))
                except Exception as e:  # noqa: BLE001
                    log.warning("Wave:3 %s: %s", key, e)
            return False
        if not OWNER and self._delegate():
            return False
        lowcut, clip = bool(self.settings.get("lowcut")), bool(self.settings.get("clipguard"))
        if clip and not limiter_available():
            log.warning("Clipguard braucht den LADSPA-Limiter (Paket swh-plugins) – wird ausgelassen")
            clip = False
        if not (lowcut or clip):
            return False
        src = find_source()
        if not src:
            log.info("Wave-Mikrofon nicht angeschlossen – Filter startet beim Einstecken")
            return False
        exe = shutil.which("pipewire")
        if not exe:
            raise RuntimeError("pipewire nicht gefunden")
        self.conf_file.parent.mkdir(parents=True, exist_ok=True)
        self.conf_file.write_text(filter_conf(src, lowcut, int(self.settings.get("lowcut_freq") or 80), clip),
                                  encoding="utf-8")
        self.proc = subprocess.Popen([exe, "-c", str(self.conf_file)], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        log.info("Wave-Filter aktiv (Low Cut: %s, Clipguard: %s)", lowcut, clip)
        if self.settings.get("set_default", True):
            import time
            time.sleep(0.5)
            subprocess.run(["pactl", "set-default-source", PROCESSED], capture_output=True)
        return True

    def _delegate(self):
        """Läuft „LinDeck Wave“, übernimmt es die Filterkette (Einstellungen liegen in wavemic.json)."""
        from . import app
        pid = app.running_instance(WAVE_APP_FILE)
        if not pid:
            return False
        try:
            os.kill(pid, signal.SIGHUP)
            return True
        except OSError:
            return False

    def reload(self):
        """Einstellungen neu lesen und anwenden (z. B. nach Änderung durch die Stream-Deck-App)."""
        self.settings = self._load()
        self.version += 1
        return self.apply()

    def stop(self):
        if self.running():
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


_instance = {}


def instance(cfg=None) -> WaveMic:
    if "obj" not in _instance:
        _instance["obj"] = WaveMic(cfg)
    return _instance["obj"]


class WaveMicService:
    def __init__(self, daemon):
        self.mic = instance(daemon.cfg)
        self._present = None

    def start(self):
        if not (self.mic.settings.get("lowcut") or self.mic.settings.get("clipguard")):
            return
        self._present = bool(find_source())
        if self._present:
            self.mic.apply()

    def tick(self):
        import time
        if time.time() - getattr(self, "_last", 0) < 10:
            return
        self._last = time.time()
        wants = self.mic.settings.get("lowcut") or self.mic.settings.get("clipguard")
        if not wants and not self.mic.running():
            return
        from . import wave3hw
        if wave3hw.present():
            if self.mic.running():
                self.mic.stop()  # Wave:3 filtert selbst
            return
        present = bool(find_source())
        if present and wants and (not self.mic.running() or not self._present):
            self.mic.apply()
        elif not present and self._present:
            self.mic.stop()
        self._present = present

    def stop(self):
        self.mic.stop()
