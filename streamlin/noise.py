"""Rauschunterdrückung für das Mikrofon in Linewave.

Die Rauschunterdrückung sitzt als eigene Stufe vor den Effekten des
Mikrofon-Kanals:

    Mikrofon ─► Rauschunterdrückung („linewave_mic_ns“) ─► Effekte des Kanals ─► Monitor-/Stream-Mix

Zwei Verfahren, automatisch gewählt (oder fest über "backend"):
  * rnnoise – LADSPA-Plugin librnnoise_ladspa.so (noise-suppression-for-voice),
    läuft in einer PipeWire-Filterkette. Die Stärke ist die VAD-Schwelle des
    Plugins: darunter gilt ein Block als „keine Stimme“ und wird stummgeschaltet.
  * webrtc – das Echo-Cancel-Modul von PipeWire mit der WebRTC-Verarbeitung
    (Pakete libpipewire-0.3-modules und libspa-0.2-modules, bei PipeWire dabei).
    Ohne Referenzsignal arbeitet nur die Rauschunterdrückung; eine Stärke gibt es nicht.

Beides läuft wie die Effekte in einem eigenen `pipewire -c`-Prozess (PID in
~/.local/state/streamlin/noise.json), damit Fenster, Befehl und Hintergrund-
programm denselben Prozess verwalten können.

Konfiguration (config.json):
  "mixer": {"noise": {"enabled": true, "backend": "auto"|"rnnoise"|"webrtc", "strength": 50}}
"""
import glob
import json
import logging
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from . import paths, system
from .i18n import _

log = logging.getLogger("streamlin.noise")

NODE = "linewave_mic_ns"
STATE = "noise.json"
RNNOISE_LIB = "librnnoise_ladspa.so"
RNNOISE_LABEL = "noise_suppressor_mono"
RNNOISE_VAD = "VAD Threshold (%)"
BACKENDS = ("rnnoise", "webrtc")
DEFAULTS = {"enabled": False, "backend": "auto", "strength": 50}
MAX_STRENGTH = 95  # VAD-Schwelle in %; darüber schneidet RNNoise auch Sprache weg
WEBRTC_GLOBS = {
    "module": ["/usr/lib*/pipewire-0.3/libpipewire-module-echo-cancel.so",
               "/usr/lib*/*/pipewire-0.3/libpipewire-module-echo-cancel.so",
               "/usr/local/lib*/pipewire-0.3/libpipewire-module-echo-cancel.so",
               "/usr/local/lib*/*/pipewire-0.3/libpipewire-module-echo-cancel.so"],
    "aec": ["/usr/lib*/spa-0.2/aec/libspa-aec-webrtc.so", "/usr/lib*/*/spa-0.2/aec/libspa-aec-webrtc.so",
            "/usr/local/lib*/spa-0.2/aec/libspa-aec-webrtc.so", "/usr/local/lib*/*/spa-0.2/aec/libspa-aec-webrtc.so"],
}


def backend_name(backend):
    return {"rnnoise": "RNNoise", "webrtc": _("PipeWire WebRTC")}.get(backend, backend or "–")


# ============================================================ Verfügbarkeit
def rnnoise_plugin():
    """Pfad zu librnnoise_ladspa.so oder None."""
    from .effects import LADSPA_DIRS
    dirs = [os.path.expanduser(d) for d in LADSPA_DIRS] + os.environ.get("LADSPA_PATH", "").split(":")
    for d in dirs:
        if d and (Path(d) / RNNOISE_LIB).is_file():
            return str(Path(d) / RNNOISE_LIB)
    return None


def webrtc_available():
    """Echo-Cancel-Modul und WebRTC-Plugin von PipeWire vorhanden?"""
    return all(any(glob.glob(p) for p in pats) for pats in WEBRTC_GLOBS.values())


def available():
    """Nutzbare Verfahren in der Reihenfolge der Bevorzugung."""
    out = []
    if rnnoise_plugin():
        out.append("rnnoise")
    if webrtc_available():
        out.append("webrtc")
    return out


def pick(pref="auto"):
    """Verfahren für die Einstellung "backend" (auto = RNNoise, sonst WebRTC) oder None."""
    have = available()
    if pref in BACKENDS and pref in have:
        return pref
    return have[0] if have else None


def install_hint():
    return _("No noise suppression available. Install the package noise-suppression-for-voice (RNNoise) or "
             "libspa-0.2-modules and libpipewire-0.3-modules (PipeWire WebRTC).")


# ============================================================ Konfiguration
def _q(s):
    return '"' + str(s).replace('"', "'") + '"'


_HEAD = """context.properties = { log.level = 0 }
context.spa-libs = {
    audio.convert.* = audioconvert/libspa-audioconvert
    support.*       = support/libspa-support
}
context.modules = [
    { name = libpipewire-module-rt args = { nice.level = -11 } flags = [ ifexists nofail ] }
    { name = libpipewire-module-protocol-native }
    { name = libpipewire-module-client-node }
    { name = libpipewire-module-adapter }
"""


def _vad_control(plugin):
    """Name des Reglers für die VAD-Schwelle (aus dem Plugin, sonst der übliche Name)."""
    from . import effects
    try:  # über den Effekt-Katalog: das Plugin wird dort in einem eigenen Prozess gelesen
        for p in effects.catalog():
            if p.get("label") == RNNOISE_LABEL and Path(p.get("plugin", "")).name == Path(plugin).name:
                return next((c["name"] for c in p["controls"] if c["name"].lower().startswith("vad threshold")),
                            RNNOISE_VAD)
    except (OSError, ValueError, KeyError):
        pass
    return RNNOISE_VAD


def rnnoise_conf(source, plugin, strength, control=RNNOISE_VAD):
    desc = _q(_("Linewave Microphone (noise suppression)"))
    return f"""# Von Linewave erzeugt – Rauschunterdrückung (RNNoise)
{_HEAD}    {{ name = libpipewire-module-filter-chain
        args = {{
            node.description = {desc}
            media.name       = {desc}
            filter.graph = {{
                nodes = [
                    {{ type = ladspa name = ns plugin = {_q(plugin)} label = {RNNOISE_LABEL}
                      control = {{ {_q(control)} = {float(strength):g} }} }}
                ]
            }}
            audio.channels = 1
            audio.position = [ MONO ]
            capture.props = {{
                node.name      = "{NODE}_in"
                node.passive   = true
                target.object  = {_q(source)}
                stream.dont-remix = true
            }}
            playback.props = {{
                node.name        = "{NODE}"
                node.description = {desc}
                media.class      = Audio/Source
            }}
        }}
    }}
]
"""


def webrtc_conf(source):
    """Echo-Cancel-Modul nur zur Rauschunterdrückung: die Referenz bleibt unverbunden (Stille)."""
    desc = _q(_("Linewave Microphone (noise suppression)"))
    ref = _q(_("Linewave noise suppression (reference)"))
    return f"""# Von Linewave erzeugt – Rauschunterdrückung (PipeWire WebRTC)
{_HEAD}    {{ name = libpipewire-module-echo-cancel
        args = {{
            library.name = aec/libspa-aec-webrtc
            monitor.mode = true
            node.latency = 480/48000
            audio.rate     = 48000
            audio.channels = 1
            audio.position = [ MONO ]
            aec.args = {{
                webrtc.noise_suppression = true
                webrtc.high_pass_filter  = true
                webrtc.gain_control      = false
                webrtc.extended_filter   = false
                webrtc.delay_agnostic    = false
                webrtc.voice_detection   = false
            }}
            capture.props = {{
                node.name      = "{NODE}_in"
                node.passive   = true
                target.object  = {_q(source)}
            }}
            source.props = {{
                node.name        = "{NODE}"
                node.description = {desc}
            }}
            sink.props = {{
                node.name        = "{NODE}_ref"
                node.description = {ref}
                node.autoconnect = false
                node.passive     = true
                priority.session = 0
                priority.driver  = 0
            }}
            playback.props = {{
                node.name        = "{NODE}_ref_out"
                node.autoconnect = false
                node.passive     = true
            }}
        }}
    }}
]
"""


# ================================================================ Prozess
class NoiseSuppression:
    """Startet/beendet die Rauschunterdrückung für den Mikrofon-Kanal des Mixers."""

    def __init__(self, mixer):
        self.mixer = mixer
        self.state_file = paths.STATE_DIR / STATE

    # ------------------------------------------------------ Einstellungen
    def settings(self):
        s = dict(DEFAULTS)
        s.update(self.mixer.conf.get("noise") or {})
        return s

    @property
    def enabled(self):
        return bool(self.settings().get("enabled"))

    def set(self, **kw):
        """Einstellungen ändern und mit der Mixer-Konfiguration speichern."""
        if "backend" in kw and kw["backend"] not in BACKENDS + ("auto",):
            raise RuntimeError(_("Unknown method: {name}").format(name=kw["backend"]))
        if "strength" in kw:
            kw["strength"] = max(0, min(MAX_STRENGTH, int(round(float(kw["strength"])))))
        ns = self.mixer.cfg.data.setdefault("mixer", {}).setdefault("noise", {})
        ns.update(kw)
        if hasattr(self.mixer.cfg, "save"):
            self.mixer.cfg.save()
        return self.settings()

    def apply(self):
        """Mikrofon-Kanal neu verbinden, damit die Einstellung wirkt (nur bei eingerichtetem Mixer)."""
        if self.mixer.is_active():
            self.mixer.rebuild_channel("mic")
            return True
        return False

    def set_strength(self, value):
        """Stärke speichern; läuft RNNoise, live setzen, sonst den Kanal neu verbinden."""
        s = self.set(strength=value)
        if self.enabled and self.backend() == "rnnoise" and not self.set_live(s["strength"]):
            self.apply()
        return s["strength"]

    def backend(self):
        return pick(self.settings().get("backend") or "auto")

    # -------------------------------------------------------------- Zustand
    def _state(self):
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, st):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(st), encoding="utf-8")

    def running(self):
        st = self._state()
        if not st.get("pid"):
            return False
        try:
            cmd = Path(f"/proc/{st['pid']}/cmdline").read_bytes().split(b"\0")
            return st.get("conf", "").encode() in cmd
        except OSError:
            return False

    def status(self):
        s = self.settings()
        st = self._state()
        run = self.running()
        have = available()
        return {"enabled": bool(s.get("enabled")), "strength": int(s.get("strength", 50)),
                "backend_pref": s.get("backend") or "auto", "backend": st.get("backend") if run else self.backend(),
                "running": run, "available": have, "hint": "" if have else install_hint()}

    # ---------------------------------------------------------- Start/Stopp
    def start(self, source):
        """Rauschunterdrückung für eine Quelle starten; gibt den Namen der bearbeiteten Quelle zurück."""
        self.stop()
        backend = self.backend()
        if not backend:
            raise RuntimeError(install_hint())
        exe = shutil.which("pipewire")
        if not exe:
            raise RuntimeError(_("pipewire not found"))
        path = paths.STATE_DIR / "noise.conf"
        path.parent.mkdir(parents=True, exist_ok=True)
        control = ""
        if backend == "rnnoise":
            plugin = rnnoise_plugin()
            control = _vad_control(plugin)
            text = rnnoise_conf(source, plugin, self.settings().get("strength", 50), control)
        else:
            text = webrtc_conf(source)
        path.write_text(text, encoding="utf-8")
        proc = subprocess.Popen([exe, "-c", str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        self._save({"pid": proc.pid, "conf": str(path), "backend": backend, "source": source, "control": control})
        for _i in range(30):  # bis die neue Quelle bei PipeWire angemeldet ist
            if any(b.get("name") == NODE for b in system._pactl_blocks("sources")):
                break
            if proc.poll() is not None:
                self._save({})
                raise RuntimeError(_("Noise suppression could not start ({backend})").format(
                    backend=backend_name(backend)))
            time.sleep(0.1)
        log.info(_("Noise suppression active ({backend})").format(backend=backend_name(backend)))
        return NODE

    def source_for(self, source):
        """Quelle für den Mikrofon-Kanal: mit Rauschunterdrückung deren Ausgang, sonst die Quelle selbst."""
        if not self.enabled or not source:
            self.stop()
            return source
        try:
            return self.start(source)
        except (RuntimeError, OSError) as e:
            log.warning(_("Noise suppression not started: {error}").format(error=e))
            return source

    def stop(self):
        st = self._state()
        if not st:
            return
        if self.running():
            try:
                os.kill(st["pid"], signal.SIGTERM)
            except OSError:
                pass
        self._save({})

    def set_live(self, strength):
        """Stärke ohne Neustart setzen (nur RNNoise, über pw-cli); False, wenn das nicht geht."""
        st = self._state()
        if st.get("backend") != "rnnoise" or not self.running():
            return False
        if not shutil.which("pw-cli") or not shutil.which("pw-dump"):
            return False
        try:
            nodes = json.loads(subprocess.run(["pw-dump", "Node"], capture_output=True, text=True,
                                              timeout=3).stdout or "[]")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return False
        nid = next((n["id"] for n in nodes
                    if ((n.get("info") or {}).get("props") or {}).get("node.name") == f"{NODE}_in"), None)
        if nid is None:
            return False
        control = st.get("control") or RNNOISE_VAD
        r = subprocess.run(["pw-cli", "set-param", str(nid), "Props",
                            f'{{ params = [ "ns:{control}" {float(strength):g} ] }}'],
                           capture_output=True, text=True, timeout=3)
        return r.returncode == 0
