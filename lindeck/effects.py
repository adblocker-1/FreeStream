"""Effekt-Plugins für Linewave-Kanäle (wie die VST-Effekte in Elgato Wave Link).

Jeder Kanal – auch das Mikrofon und eigene Eingänge – kann eine Kette von
Effekten bekommen. Sie läuft als PipeWire-Filterkette in einem eigenen
`pipewire -c`-Prozess:

    Kanal-Quelle ─► Effekt 1 ─► Effekt 2 … ─► „Linewave <Kanal> (Effekte)“ ─► Monitor-/Stream-Mix

Verfügbare Effekte:
  * eingebaute PipeWire-Filter: Hochpass, Tiefpass, Bass/Höhen (Shelf), EQ-Band
  * LADSPA-Plugins aus den üblichen Ordnern, z. B. Rauschunterdrückung
    (noise-suppression-for-voice), Kompressor, Noise Gate und Limiter
    (swh-plugins), Equalizer und Hall (tap-plugins)

Plugins werden in einem eigenen Prozess ausgelesen (`python3 -m lindeck.effects
--scan`), damit ein fehlerhaftes Plugin Linewave nicht mitreißt.

Konfiguration (config.json, wird von Linewave geschrieben):
  "mixer": {"effects": {"mic": [{"plugin": "builtin:highpass", "enabled": true,
                                 "controls": {"Freq": 90}}, …], …}}
"""
import ctypes
import json
import logging
import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import paths, system

log = logging.getLogger("lindeck.effects")

LADSPA_DIRS = ["/usr/lib/ladspa", "/usr/lib/x86_64-linux-gnu/ladspa", "/usr/lib/aarch64-linux-gnu/ladspa",
               "/usr/local/lib/ladspa", "/usr/lib64/ladspa", "~/.ladspa"]
STATE = "effects.json"
LV2_DIRS = ["/usr/lib/lv2", "/usr/lib/x86_64-linux-gnu/lv2", "/usr/lib/aarch64-linux-gnu/lv2", "/usr/local/lib/lv2",
            "/usr/lib64/lv2", "~/.lv2"]
CACHE = "plugin-cache.json"

# Eingebaute Filter von PipeWire (Typ builtin, Ports „In“/„Out“)
BUILTIN = [
    {"id": "builtin:highpass", "label": "bq_highpass", "name": "Hochpass (Low Cut)",
     "info": "Entfernt Brummen und Trittschall unterhalb der Frequenz",
     "controls": [{"name": "Freq", "min": 20, "max": 400, "default": 90, "unit": "Hz", "log": True},
                  {"name": "Q", "min": 0.3, "max": 2.0, "default": 0.707}]},
    {"id": "builtin:lowpass", "label": "bq_lowpass", "name": "Tiefpass",
     "info": "Dämpft hohe Frequenzen, z. B. gegen Zischen",
     "controls": [{"name": "Freq", "min": 2000, "max": 20000, "default": 12000, "unit": "Hz", "log": True},
                  {"name": "Q", "min": 0.3, "max": 2.0, "default": 0.707}]},
    {"id": "builtin:lowshelf", "label": "bq_lowshelf", "name": "Bass",
     "info": "Hebt oder senkt die Tiefen",
     "controls": [{"name": "Freq", "min": 40, "max": 500, "default": 150, "unit": "Hz", "log": True},
                  {"name": "Q", "min": 0.3, "max": 2.0, "default": 0.707},
                  {"name": "Gain", "min": -15, "max": 15, "default": 3, "unit": "dB"}]},
    {"id": "builtin:highshelf", "label": "bq_highshelf", "name": "Höhen",
     "info": "Hebt oder senkt die Höhen (Präsenz, Brillanz)",
     "controls": [{"name": "Freq", "min": 2000, "max": 16000, "default": 6000, "unit": "Hz", "log": True},
                  {"name": "Q", "min": 0.3, "max": 2.0, "default": 0.707},
                  {"name": "Gain", "min": -15, "max": 15, "default": 3, "unit": "dB"}]},
    {"id": "builtin:peaking", "label": "bq_peaking", "name": "EQ-Band",
     "info": "Ein einzelnes Equalizer-Band",
     "controls": [{"name": "Freq", "min": 60, "max": 16000, "default": 1000, "unit": "Hz", "log": True},
                  {"name": "Q", "min": 0.3, "max": 6.0, "default": 1.0},
                  {"name": "Gain", "min": -15, "max": 15, "default": 0, "unit": "dB"}]},
]
BUILTIN_BY_ID = {b["id"]: b for b in BUILTIN}

# Bekannte LADSPA-Plugins mit verständlichem Namen (Label -> Name, Beschreibung)
FRIENDLY = {
    "noise_suppressor_mono": ("Rauschunterdrückung (RNNoise)", "Filtert Tastatur, Lüfter und Hintergrund heraus"),
    "noise_suppressor_stereo": ("Rauschunterdrückung Stereo (RNNoise)", "Filtert Hintergrundgeräusche heraus"),
    "deep_filter_mono": ("Rauschunterdrückung (DeepFilterNet)", "KI-Rauschfilter für Stimmen"),
    "deep_filter_stereo": ("Rauschunterdrückung Stereo (DeepFilterNet)", "KI-Rauschfilter für Stimmen"),
    "sc4": ("Kompressor (SC4)", "Gleicht laute und leise Stellen aus"),
    "sc4m": ("Kompressor mono (SC4)", "Gleicht laute und leise Stellen aus"),
    "gate": ("Noise Gate", "Schaltet das Mikrofon in Sprechpausen stumm"),
    "fastLookaheadLimiter": ("Limiter", "Verhindert Übersteuern"),
    "tap_equalizer": ("Equalizer (8 Bänder)", "Klangregelung"),
    "tap_reverb": ("Hall", "Raumklang"),
    "tap_deesser": ("De-Esser", "Dämpft scharfe S-Laute"),
}
# Bessere Startwerte für den Einsatz am Mikrofon
PRESETS = {
    "gate": {"Threshold (dB)": -45.0, "Attack (ms)": 5.0, "Hold (ms)": 250.0, "Decay (ms)": 250.0,
             "Range (dB)": -60.0},
    "sc4m": {"Threshold level (dB)": -20.0, "Ratio (1:n)": 3.0, "Makeup gain (dB)": 4.0},
    "sc4": {"Threshold level (dB)": -20.0, "Ratio (1:n)": 3.0, "Makeup gain (dB)": 4.0},
    "fastLookaheadLimiter": {"Limit (dB)": -1.0},
}
FAVORITES = ["noise_suppressor_mono", "deep_filter_mono", "gate", "sc4m", "sc4", "fastLookaheadLimiter",
             "tap_deesser", "tap_equalizer"]


# =================================================================== LADSPA
class _Hint(ctypes.Structure):
    _fields_ = [("hint", ctypes.c_int), ("lower", ctypes.c_float), ("upper", ctypes.c_float)]


class _Desc(ctypes.Structure):
    _fields_ = [("unique_id", ctypes.c_ulong), ("label", ctypes.c_char_p), ("properties", ctypes.c_int),
                ("name", ctypes.c_char_p), ("maker", ctypes.c_char_p), ("copyright", ctypes.c_char_p),
                ("port_count", ctypes.c_ulong), ("port_descriptors", ctypes.POINTER(ctypes.c_int)),
                ("port_names", ctypes.POINTER(ctypes.c_char_p)), ("port_hints", ctypes.POINTER(_Hint))]


P_INPUT, P_OUTPUT, P_CONTROL, P_AUDIO = 1, 2, 4, 8
H_BELOW, H_ABOVE, H_TOGGLED, H_SRATE, H_LOG, H_INT = 0x1, 0x2, 0x4, 0x8, 0x10, 0x20
H_DEFAULT = 0x3C0
RATE = 48000.0


def _default(hint, lo, hi):
    d = hint & H_DEFAULT
    log_ = hint & H_LOG and lo > 0 and hi > 0

    def between(t):
        if log_:
            import math
            return math.exp(math.log(lo) * (1 - t) + math.log(hi) * t)
        return lo * (1 - t) + hi * t
    return {0x40: lo, 0x80: between(0.25), 0xC0: between(0.5), 0x100: between(0.75), 0x140: hi,
            0x200: 0.0, 0x240: 1.0, 0x280: 100.0, 0x2C0: 440.0}.get(d, lo if hint & H_BELOW else 0.0)


def read_ladspa(path):
    """Alle Plugins einer LADSPA-Bibliothek (läuft im Scan-Prozess)."""
    lib = ctypes.CDLL(str(path))
    fn = lib.ladspa_descriptor
    fn.restype = ctypes.POINTER(_Desc)
    fn.argtypes = [ctypes.c_ulong]
    out = []
    for i in range(256):
        p = fn(i)
        if not p:
            break
        d = p.contents
        ins, outs, controls = [], [], []
        for k in range(d.port_count):
            pd = d.port_descriptors[k]
            name = d.port_names[k].decode("utf-8", "replace")
            if pd & P_AUDIO:
                (ins if pd & P_INPUT else outs).append(name)
            elif pd & P_CONTROL and pd & P_INPUT:
                h = d.port_hints[k]
                lo = h.lower * (RATE if h.hint & H_SRATE else 1) if h.hint & H_BELOW else 0.0
                hi = h.upper * (RATE if h.hint & H_SRATE else 1) if h.hint & H_ABOVE else max(1.0, lo + 1)
                if h.hint & H_TOGGLED:
                    lo, hi = 0.0, 1.0
                c = {"name": name, "min": lo, "max": hi, "default": _default(h.hint, lo, hi)}
                if h.hint & H_TOGGLED:
                    c["toggle"] = True
                if h.hint & H_INT:
                    c["int"] = True
                if h.hint & H_LOG:
                    c["log"] = True
                controls.append(c)
        if len(ins) not in (1, 2) or len(outs) != len(ins):
            continue  # nur Filter mit gleich vielen Ein- und Ausgängen (mono/stereo)
        label = d.label.decode("utf-8", "replace")
        friendly = FRIENDLY.get(label)
        out.append({"id": f"ladspa:{Path(path).name}:{label}", "type": "ladspa", "plugin": str(path),
                    "label": label, "name": friendly[0] if friendly else d.name.decode("utf-8", "replace"),
                    "info": friendly[1] if friendly else (d.maker or b"").decode("utf-8", "replace"),
                    "inputs": ins, "outputs": outs, "controls": controls})
    return out


def _ladspa_files():
    seen = set()
    for d in [os.path.expanduser(x) for x in LADSPA_DIRS] + os.environ.get("LADSPA_PATH", "").split(":"):
        if d and os.path.isdir(d):
            for f in sorted(Path(d).glob("*.so")):
                real = f.resolve()
                if real not in seen:
                    seen.add(real)
                    yield f


# ===================================================================== LV2
LV2 = "http://lv2plug.in/ns/"
# Features, die die Filterkette von PipeWire bereitstellt; Plugins mit anderen Pflicht-Features fehlen
LV2_SUPPORTED = {LV2 + "ext/urid#map", LV2 + "ext/urid#unmap", LV2 + "ext/options#options",
                 LV2 + "ext/buf-size#boundedBlockLength", LV2 + "ext/worker#schedule", LV2 + "ext/log#log",
                 LV2 + "lv2core#isLive", LV2 + "lv2core#hardRTCapable"}
FRIENDLY_LV2 = {
    "http://gareus.org/oss/lv2/darc#mono": ("Kompressor (x42 darc)", "Gleicht laute und leise Stellen aus"),
    "http://gareus.org/oss/lv2/darc#stereo": ("Kompressor Stereo (x42 darc)", "Gleicht laute und leise Stellen aus"),
    "http://gareus.org/oss/lv2/fil4#mono": ("Equalizer (x42 fil4)", "Parametrischer EQ mit Hoch- und Tiefpass"),
    "http://gareus.org/oss/lv2/fil4#stereo": ("Equalizer Stereo (x42 fil4)", "Parametrischer EQ"),
    "http://gareus.org/oss/lv2/dpl#mono": ("Limiter (x42 dpl)", "Verhindert Übersteuern"),
    "http://gareus.org/oss/lv2/dpl#stereo": ("Limiter Stereo (x42 dpl)", "Verhindert Übersteuern"),
    "http://calf.sourceforge.net/plugins/Gate": ("Noise Gate (Calf)", "Schaltet in Sprechpausen stumm"),
    "http://calf.sourceforge.net/plugins/Compressor": ("Kompressor (Calf)", "Gleicht laute und leise Stellen aus"),
    "http://calf.sourceforge.net/plugins/Deesser": ("De-Esser (Calf)", "Dämpft scharfe S-Laute"),
    "http://calf.sourceforge.net/plugins/Equalizer5Band": ("Equalizer 5 Bänder (Calf)", "Klangregelung"),
    "urn:jeremy.salwen:plugins:noise-repellent-mono": ("Rauschunterdrückung (Noise Repellent)",
                                                       "Filtert gleichmäßiges Rauschen heraus"),
}
LV2_FAVORITES = list(FRIENDLY_LV2)


def _lv2_dirs():
    dirs = [os.path.expanduser(d) for d in LV2_DIRS] + os.environ.get("LV2_PATH", "").split(":")
    seen, out = set(), []
    for d in dirs:
        if d and os.path.isdir(d) and os.path.realpath(d) not in seen:
            seen.add(os.path.realpath(d))
            out.append(d)
    return out


def parse_lv2info(text, uri):
    """Ausgabe von `lv2info <URI>` -> Katalogeintrag (oder None, wenn kein passender Filter)."""
    head, ports, cur, key = {}, [], None, None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("Port ") and line.endswith(":"):
            cur, key = {}, None
            ports.append(cur)
            continue
        target = cur if cur is not None else head
        if ":" in line and not line.startswith("http") and not line.startswith("file:") \
                and line.split(":", 1)[0].replace(" ", "").isalpha():
            key, val = (x.strip() for x in line.split(":", 1))
            target.setdefault(key, [])
            if val:
                target[key].append(val)
        elif key is not None:
            target.setdefault(key, []).append(line)
    req = {f for f in head.get("Required Features", []) if f}
    if req - LV2_SUPPORTED:
        return None
    name0 = (head.get("Name") or [""])[0]
    if any(x in " ".join(head.get("Class", [])) for x in ("Analy", "Instrument", "Generator")) or \
            any(x in name0 for x in ("Meter", "Display", "Scope", "Tuner", "Analy")):
        return None  # Pegelanzeigen u. Ä. brauchen ihre eigene Oberfläche
    ins, outs, controls = [], [], []
    for pt in ports:
        types = " ".join(pt.get("Type", []))
        sym = (pt.get("Symbol") or [""])[0]
        props = " ".join(pt.get("Properties", []))
        if "AudioPort" in types:
            (ins if "InputPort" in types else outs).append(sym)
        elif "ControlPort" in types and "InputPort" in types:
            def num(k, d):
                try:
                    return float((pt.get(k) or [d])[0])
                except ValueError:
                    return float(d)
            lo, hi = num("Minimum", 0), num("Maximum", 1)
            if hi <= lo:
                hi = lo + 1
            c = {"name": sym, "label": (pt.get("Name") or [sym])[0], "min": lo, "max": hi,
                 "default": max(lo, min(hi, num("Default", lo)))}
            if "toggled" in props:
                c["toggle"] = True
            if "integer" in props or "enumeration" in props:
                c["int"] = True
            if "logarithmic" in props:
                c["log"] = True
            if "enabled" in " ".join(pt.get("Designation", [])):
                continue  # „Enable“-Schalter des Plugins: immer an
            controls.append(c)
        elif "ControlPort" in types:
            continue  # Ausgabewerte (Pegel, Gain-Reduktion)
        else:
            return None  # Atom/CV/MIDI – nicht als Effekt nutzbar
    if len(ins) not in (1, 2) or len(outs) != len(ins):
        return None
    name = (head.get("Name") or [uri])[0]
    friendly = FRIENDLY_LV2.get(uri)
    author = (head.get("Author") or [""])[0]
    return {"id": f"lv2:{uri}", "type": "lv2", "plugin": uri, "label": uri.rsplit("/", 1)[-1].split("#")[0],
            "name": friendly[0] if friendly else name, "info": friendly[1] if friendly else author,
            "inputs": ins, "outputs": outs, "controls": controls}


def read_lv2():
    """Alle LV2-Effekte über `lv2ls`/`lv2info` (Paket lilv-utils)."""
    if not shutil.which("lv2ls") or not shutil.which("lv2info"):
        return []
    try:
        uris = subprocess.run(["lv2ls"], capture_output=True, text=True, timeout=20).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        return []
    from concurrent.futures import ThreadPoolExecutor

    def one(uri):
        try:
            out = subprocess.run(["lv2info", uri], capture_output=True, text=True, timeout=10).stdout
            return parse_lv2info(out, uri)
        except (OSError, subprocess.TimeoutExpired):
            return None
    with ThreadPoolExecutor(8) as ex:
        return [p for p in ex.map(one, uris) if p]


def _scan_all():
    found = []
    for f in _ladspa_files():
        try:
            found += read_ladspa(f)
        except (OSError, AttributeError) as e:
            print(f"übersprungen {f}: {e}", file=sys.stderr)
    return found + read_lv2()


def catalog(refresh=False):
    """Alle verfügbaren Effekte: eingebaute zuerst, dann bekannte und übrige LADSPA- und LV2-Plugins."""
    files = [str(f) for f in _ladspa_files()] + _lv2_dirs()
    stamp = [[f, os.path.getmtime(f)] for f in files] + [bool(shutil.which("lv2info"))]
    cache = paths.STATE_DIR / CACHE
    plugins = None
    if not refresh:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("stamp") == stamp:
                plugins = data["plugins"]
        except (OSError, ValueError, KeyError):
            pass
    if plugins is None:
        plugins = []
        if files:
            try:
                r = subprocess.run([sys.executable, "-m", "lindeck.effects", "--scan"], capture_output=True,
                                   text=True, timeout=30,
                                   env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)))
                plugins = json.loads(r.stdout or "[]")
            except (OSError, ValueError, subprocess.TimeoutExpired) as e:
                log.warning("Plugins nicht lesbar: %s", e)
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"stamp": stamp, "plugins": plugins}), encoding="utf-8")
        except OSError:
            pass
    rank = {lab: i for i, lab in enumerate(FAVORITES)}
    rank.update({f"lv2:{u}": 100 + i for i, u in enumerate(LV2_FAVORITES)})
    plugins.sort(key=lambda p: (rank.get(p["id"], rank.get(p["label"], 999) if p["type"] == "ladspa" else 999),
                                p["name"].lower()))
    return [dict(b, type="builtin", inputs=["In"], outputs=["Out"]) for b in BUILTIN] + plugins


def lookup(plugin_id, cat=None):
    if plugin_id in BUILTIN_BY_ID:
        b = BUILTIN_BY_ID[plugin_id]
        return dict(b, type="builtin", inputs=["In"], outputs=["Out"])
    return next((p for p in (cat if cat is not None else catalog()) if p["id"] == plugin_id), None)


def new_effect(plugin):
    """Eintrag für die Effektkette eines Kanals mit den Standardwerten des Plugins."""
    preset = PRESETS.get(plugin.get("label"), {})
    return {"plugin": plugin["id"], "name": plugin["name"], "enabled": True,
            "controls": {c["name"]: preset.get(c["name"], c["default"]) for c in plugin.get("controls", [])}}


# ================================================================ Filterkette
def _q(s):
    return '"' + str(s).replace('"', "'") + '"'


def graph(chain, cat=None):
    """filter.graph für eine Effektkette – immer stereo (Mono-Plugins laufen je Seite einmal)."""
    nodes, links = [], []
    lanes = [None, None]  # letzter Ausgang je Seite
    first = [None, None]
    n = 0
    for fx in chain:
        if not fx.get("enabled", True):
            continue
        p = lookup(fx["plugin"], cat)
        if not p:
            log.warning("Effekt %s nicht gefunden – ausgelassen", fx["plugin"])
            continue
        ctl = " ".join(f"{_q(k)} = {float(v):g}" for k, v in (fx.get("controls") or {}).items()
                       if any(c["name"] == k for c in p.get("controls", [])))
        stereo = len(p["inputs"]) == 2
        names = [f"fx{n}"] if stereo else [f"fx{n}l", f"fx{n}r"]
        n += 1
        for nm in names:
            if p["type"] == "builtin":
                nodes.append(f"{{ type = builtin name = {nm} label = {p['label']} control = {{ {ctl} }} }}")
            elif p["type"] == "lv2":
                nodes.append(f"{{ type = lv2 name = {nm} plugin = {_q(p['plugin'])} control = {{ {ctl} }} }}")
            else:
                nodes.append(f"{{ type = ladspa name = {nm} plugin = {_q(p['plugin'])} label = {p['label']} "
                             f"control = {{ {ctl} }} }}")
        for lane in (0, 1):
            nm = names[0] if stereo else names[lane]
            port_in = f"{nm}:{p['inputs'][lane if stereo else 0]}"
            port_out = f"{nm}:{p['outputs'][lane if stereo else 0]}"
            if lanes[lane]:
                links.append(f"{{ output = {_q(lanes[lane])} input = {_q(port_in)} }}")
            else:
                first[lane] = port_in
            lanes[lane] = port_out
    if not nodes:
        return None
    return {"nodes": nodes, "links": links, "inputs": first, "outputs": lanes}


def conf(cid, label, source, chain, cat=None, stage=None):
    """PipeWire-Konfiguration für `pipewire -c`.

    stage=i: nur eine Teilkette (Brücken-Modus) – Ein- und Ausgang werden nicht
    automatisch verbunden, sondern von Linewave per pw-link verkabelt.
    """
    g = graph(chain, cat)
    if g is None:
        raise ValueError("Keine aktiven Effekte")
    node = f"linewave_{cid}_fx"
    if stage is not None:
        return _stage_conf(node, stage, label, g)
    if source.endswith(".monitor"):
        target = f'target.object = {_q(source[:-len(".monitor")])}\n                stream.capture.sink = true'
    else:
        target = f"target.object = {_q(source)}"
    nl = "\n                    "
    return f"""# Von Linewave erzeugt – Effekte für {label}
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
            node.description = {_q(f"Linewave {label} (Effekte)")}
            media.name       = {_q(f"Linewave {label} (Effekte)")}
            filter.graph = {{
                nodes = [
                    {nl.join(g["nodes"])}
                ]
                links = [ {" ".join(g["links"])} ]
                inputs  = [ {" ".join(_q(x) for x in g["inputs"])} ]
                outputs = [ {" ".join(_q(x) for x in g["outputs"])} ]
            }}
            audio.channels = 2
            audio.position = [ FL FR ]
            capture.props = {{
                node.name    = "{node}_in"
                node.passive = true
                {target}
            }}
            playback.props = {{
                node.name        = "{node}"
                node.description = {_q(f"Linewave {label} (Effekte)")}
                media.class      = Audio/Source
            }}
        }}
    }}
]
"""


def _stage_conf(node, stage, label, g):
    nl = "\n                    "
    return f"""# Von Linewave erzeugt – Teilkette {stage} der Effekte für {label}
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
            node.description = {_q(f"Linewave {label} (Effekte {stage + 1})")}
            filter.graph = {{
                nodes = [
                    {nl.join(g["nodes"])}
                ]
                links = [ {" ".join(g["links"])} ]
                inputs  = [ {" ".join(_q(x) for x in g["inputs"])} ]
                outputs = [ {" ".join(_q(x) for x in g["outputs"])} ]
            }}
            audio.channels = 2
            audio.position = [ FL FR ]
            capture.props = {{
                node.name        = "{node}_s{stage}_in"
                node.autoconnect = false
                node.passive     = true
            }}
            playback.props = {{
                node.name        = "{node}_s{stage}_out"
                node.autoconnect = false
            }}
        }}
    }}
]
"""


def pipewire_lv2():
    """Kann die Filterkette dieses PipeWire LV2-Plugins laden? (nicht jede Distribution baut das mit)"""
    import glob
    pats = ["/usr/lib*/pipewire-0.3/libpipewire-module-filter-chain-lv2.so",
            "/usr/lib*/*/pipewire-0.3/libpipewire-module-filter-chain-lv2.so",
            "/usr/lib*/spa-0.2/filter-graph/libspa-filter-graph-plugin-lv2.so",
            "/usr/lib*/*/spa-0.2/filter-graph/libspa-filter-graph-plugin-lv2.so",
            "/usr/local/lib*/pipewire-0.3/libpipewire-module-filter-chain-lv2.so"]
    return any(glob.glob(p) for p in pats)


def lv2_bridge_available():
    """LV2 über jalv (pipewire-jack), wenn die Filterkette selbst kein LV2 kann."""
    return all(shutil.which(x) for x in ("jalv", "pw-jack", "pw-link"))


def lv2_status():
    """(verfügbar, Hinweis) für LV2-Effekte auf diesem System."""
    if pipewire_lv2():
        return True, ""
    if lv2_bridge_available():
        return True, "über jalv"
    return False, "LV2 braucht die Pakete jalv und pipewire-jack (oder ein PipeWire mit LV2-Unterstützung)"


def stages(chain, cat=None):
    """Kette für den Brücken-Modus aufteilen: [("fc", [Effekte]), ("lv2", Effekt), …] mit Ketten-Index."""
    out = []
    for i, fx in enumerate(chain):
        if not fx.get("enabled", True):
            continue
        p = lookup(fx["plugin"], cat)
        if not p:
            continue
        if p["type"] == "lv2":
            out.append(("lv2", [(i, fx, p)]))
        elif out and out[-1][0] == "fc":
            out[-1][1].append((i, fx, p))
        else:
            out.append(("fc", [(i, fx, p)]))
    return out


_PROCS = {}  # Kanal -> {Name: Popen} – für Live-Regler über die Standardeingabe von jalv


class Effects:
    """Startet und beendet die Effektketten der Kanäle (ein pipewire-Prozess je Kanal).

    Die Prozess-IDs stehen in ~/.local/state/lindeck/effects.json, damit jeder
    Linewave-Prozess (Fenster, Stream-Deck-Taste, Befehl) sie verwalten kann.
    """

    def __init__(self, mixer):
        self.mixer = mixer
        self.state_file = paths.STATE_DIR / STATE

    # ------------------------------------------------------- Konfiguration
    def _all(self):
        return (self.mixer.conf.get("effects") or {}) if self.mixer.cfg is not None else {}

    def chain(self, cid):
        return list(self._all().get(cid) or [])

    def active(self, cid):
        return any(fx.get("enabled", True) for fx in self.chain(cid))

    def set_chain(self, cid, chain):
        mx = self.mixer.cfg.data.setdefault("mixer", {})
        fx = mx.setdefault("effects", {})
        if chain:
            fx[cid] = chain
        else:
            fx.pop(cid, None)
        if hasattr(self.mixer.cfg, "save"):
            self.mixer.cfg.save()

    @staticmethod
    def node_name(cid):
        return f"linewave_{cid}_fx"

    # ----------------------------------------------------------- Prozesse
    def _state(self):
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, st):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(st), encoding="utf-8")

    @staticmethod
    def _ours(pid, conf_path):
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            return conf_path.encode() in cmd
        except OSError:
            return False

    def running(self, cid):
        e = self._state().get(cid)
        if not e:
            return False
        if e.get("mode") == "bridge":
            return bool(e["procs"]) and all(self._ours(x["pid"], x["tag"]) for x in e["procs"])
        return self._ours(e["pid"], e["conf"])

    def _needs_bridge(self, cid, cat=None):
        if pipewire_lv2():
            return False
        return any(kind == "lv2" for kind, _ in stages(self.chain(cid), cat))

    def start(self, c, source):
        """Effektkette eines Kanals starten; gibt den Namen der bearbeiteten Quelle zurück."""
        cid = c["id"]
        self.stop(cid)
        cat = catalog() if any(fx["plugin"].startswith("lv2:") for fx in self.chain(cid)) else None
        if self._needs_bridge(cid, cat):
            return self._start_bridge(c, source, cat)
        exe = shutil.which("pipewire")
        if not exe:
            raise RuntimeError("pipewire nicht gefunden")
        path = paths.STATE_DIR / f"effects-{cid}.conf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(conf(cid, c["name"], source, self.chain(cid), cat), encoding="utf-8")
        proc = subprocess.Popen([exe, "-c", str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        st = self._state()
        st[cid] = {"pid": proc.pid, "conf": str(path)}
        self._save(st)
        node = self.node_name(cid)
        for _ in range(30):  # bis die neue Quelle bei PipeWire angemeldet ist
            if any(b.get("name") == node for b in system._pactl_blocks("sources")):
                break
            if proc.poll() is not None:
                raise RuntimeError("Effektkette konnte nicht starten (Plugin fehlt oder Einstellung ungültig)")
            time.sleep(0.1)
        log.info("Effekte für %s aktiv (%d)", c["name"], sum(1 for fx in self.chain(cid) if fx.get("enabled", True)))
        return node

    # ------------------------------------------------------ Brücken-Modus
    @staticmethod
    def _ports(direction):
        try:
            out = subprocess.run(["pw-link", "-o" if direction == "out" else "-i"], capture_output=True, text=True,
                                 timeout=3).stdout
        except (OSError, subprocess.TimeoutExpired):
            return []
        return [x.strip() for x in out.splitlines() if x.strip()]

    @staticmethod
    def _pair(ports):
        """Zwei Ports (links, rechts) aus einer Liste; Mono wird auf beide Seiten gelegt."""
        def order(p):
            n = p.rsplit(":", 1)[-1].upper()
            return (0 if n.endswith(("FL", "_L", "LEFT", "_1")) else 1 if n.endswith(("FR", "_R", "RIGHT", "_2"))
                    else 2, p)
        ports = sorted(ports, key=order)
        if not ports:
            return None
        return [ports[0], ports[1] if len(ports) > 1 else ports[0]]

    def _source_ports(self, source):
        outs = self._ports("out")
        if source.endswith(".monitor"):
            node = source[:-len(".monitor")]
            return self._pair([p for p in outs if p.startswith(node + ":") and ":monitor_" in p])
        return self._pair([p for p in outs if p.startswith(source + ":") and ":monitor_" not in p])

    def _start_bridge(self, c, source, cat):
        """Kette mit LV2-Effekten ohne LV2 in PipeWire: Teilketten + jalv, per pw-link verbunden."""
        cid = c["id"]
        if not lv2_bridge_available():
            raise RuntimeError("LV2-Effekte brauchen die Pakete jalv und pipewire-jack")
        node = self.node_name(cid)
        desc = f"Linewave {c['name']} (Effekte)"
        from .mixer import _props
        mid = self.mixer.run(["load-module", "module-null-sink", "media.class=Audio/Source/Virtual",
                              f"sink_name={node}", "channel_map=front-left,front-right",
                              "sink_properties=" + _props(device_description=desc, node_description=desc)])
        entry = {"mode": "bridge", "module": mid, "procs": [], "stages": []}
        st = self._state()
        st[cid] = entry
        self._save(st)
        procs = _PROCS.setdefault(cid, {})
        chain_stages = stages(self.chain(cid), cat)
        wanted = []
        for i, (kind, items) in enumerate(chain_stages):
            if kind == "fc":
                path = paths.STATE_DIR / f"effects-{cid}-s{i}.conf"
                path.write_text(conf(cid, c["name"], source, [fx for _, fx, _ in items], cat, stage=i),
                                encoding="utf-8")
                pr = subprocess.Popen([shutil.which("pipewire") or "pipewire", "-c", str(path)],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      start_new_session=True)
                entry["procs"].append({"pid": pr.pid, "tag": str(path)})
                ins = [f"{node}_s{i}_in:input_FL", f"{node}_s{i}_in:input_FR"]
                outs = [f"{node}_s{i}_out:output_FL", f"{node}_s{i}_out:output_FR"]
                entry["stages"].append({"kind": "fc", "index": [ix for ix, _, _ in items], "node": f"{node}_s{i}_in"})
            else:
                ix, fx, p = items[0]
                stereo = len(p["inputs"]) == 2
                names = [f"{node}_s{i}"] if stereo else [f"{node}_s{i}l", f"{node}_s{i}r"]
                ctl = [f"{k}={float(v):g}" for k, v in (fx.get("controls") or {}).items()
                       if any(cc["name"] == k for cc in p.get("controls", []))]
                for nm in names:
                    args = ["pw-jack", "jalv", "-x", "-n", nm]
                    for x in ctl:
                        args += ["-c", x]
                    pr = subprocess.Popen(args + [p["plugin"]], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, start_new_session=True, text=True)
                    procs[nm] = pr
                    entry["procs"].append({"pid": pr.pid, "tag": nm})
                if stereo:
                    ins = [f"{names[0]}:{p['inputs'][0]}", f"{names[0]}:{p['inputs'][1]}"]
                    outs = [f"{names[0]}:{p['outputs'][0]}", f"{names[0]}:{p['outputs'][1]}"]
                else:
                    ins = [f"{names[0]}:{p['inputs'][0]}", f"{names[1]}:{p['inputs'][0]}"]
                    outs = [f"{names[0]}:{p['outputs'][0]}", f"{names[1]}:{p['outputs'][0]}"]
                entry["stages"].append({"kind": "lv2", "index": [ix], "jalv": names})
            wanted.append((ins, outs))
        self._save(st)
        final = [f"{node}:input_FL", f"{node}:input_FR"]
        src = None
        for _ in range(60):  # bis alle Ports da sind (jalv braucht einen Moment)
            have_in, src = set(self._ports("in")), self._source_ports(source)
            if src and all(p in have_in for ins, _ in wanted for p in ins) and all(p in have_in for p in final):
                have_out = set(self._ports("out"))
                if all(p in have_out for _, outs in wanted for p in outs):
                    break
            time.sleep(0.1)
        else:
            self.stop(cid)
            raise RuntimeError("Effektkette (LV2 über jalv) konnte nicht starten")
        prev = src
        for ins, outs in wanted + [(final, None)]:
            for a, b in zip(prev, ins):
                subprocess.run(["pw-link", a, b], capture_output=True, timeout=3)
            prev = outs
        log.info("Effekte für %s aktiv (LV2 über jalv, %d Stufen)", c["name"], len(wanted))
        return node

    def source_for(self, c, source):
        """Quelle für die Mixe: mit aktiven Effekten die Effektkette, sonst die Kanal-Quelle selbst."""
        if not self.active(c["id"]):
            self.stop(c["id"])
            return source
        try:
            return self.start(c, source)
        except (RuntimeError, ValueError, OSError) as e:
            log.warning("Effekte für %s nicht gestartet: %s", c["name"], e)
            return source

    def stop(self, cid):
        st = self._state()
        e = st.pop(cid, None)
        procs = [(x["pid"], x["tag"]) for x in e.get("procs", [])] if e and e.get("mode") == "bridge" else \
            [(e["pid"], e["conf"])] if e else []
        for pid, tag in procs:
            if self._ours(pid, tag):
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
        if e and e.get("module"):
            try:
                self.mixer.run(["unload-module", str(e["module"])])
            except RuntimeError:
                pass
        for pr in _PROCS.pop(cid, {}).values():
            try:
                pr.stdin.close()
            except (OSError, AttributeError):
                pass
        if e is not None:
            self._save(st)

    def stop_all(self):
        for cid in list(self._state()):
            self.stop(cid)

    def set_live(self, cid, chain_index, control, value):
        """Reglerwert ohne Neustart setzen (pw-cli bzw. jalv); False, wenn das nicht geht."""
        e = self._state().get(cid) or {}
        want, offset = f"{self.node_name(cid)}_in", 0
        if e.get("mode") == "bridge":
            stage = next((s for s in e.get("stages", []) if chain_index in s["index"]), None)
            if stage is None:
                return False
            if stage["kind"] == "lv2":
                ok = False
                for nm in stage["jalv"]:
                    pr = _PROCS.get(cid, {}).get(nm)
                    if pr is None or pr.poll() is not None:
                        return False
                    try:
                        pr.stdin.write(f"{control} = {float(value):g}\n")
                        pr.stdin.flush()
                        ok = True
                    except (OSError, ValueError):
                        return False
                return ok
            want = stage["node"]
            offset = min(stage["index"])
        if not shutil.which("pw-cli") or not shutil.which("pw-dump"):
            return False
        try:
            nodes = json.loads(subprocess.run(["pw-dump", "Node"], capture_output=True, text=True,
                                              timeout=3).stdout or "[]")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return False
        nid = next((n["id"] for n in nodes if ((n.get("info") or {}).get("props") or {}).get("node.name") == want),
                   None)
        if nid is None:
            return False
        # Name des Knotens im Graphen (nur aktive Effekte zählen, ab Beginn der Teilkette)
        k = -1
        for i, fx in enumerate(self.chain(cid)):
            if i >= offset and fx.get("enabled", True):
                k += 1
            if i == chain_index:
                break
        p = lookup(self.chain(cid)[chain_index]["plugin"])
        names = [f"fx{k}"] if p and len(p["inputs"]) == 2 else [f"fx{k}l", f"fx{k}r"]
        params = " ".join(f'"{nm}:{control}" {float(value):g}' for nm in names)
        r = subprocess.run(["pw-cli", "set-param", str(nid), "Props", f"{{ params = [ {params} ] }}"],
                           capture_output=True, text=True, timeout=3)
        return r.returncode == 0


if __name__ == "__main__":
    if "--scan" in sys.argv:
        print(json.dumps(_scan_all()))
