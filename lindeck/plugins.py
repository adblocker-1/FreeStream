"""Plugin-Host für das Elgato-Stream-Deck-SDK (WebSocket-Protokoll).

Elgato-Plugins sind eigenständige Programme, die sich per WebSocket mit der
Stream-Deck-App verbinden. LinDeck übernimmt diese Rolle:

  * Plugins werden aus ~/.local/share/lindeck/plugins/<UUID>.sdPlugin geladen
    (Installation: `lindeck plugin install X.streamDeckPlugin` oder einfach in
    den Download-Ordner legen).
  * Node.js-Plugins (CodePath *.js, alle Plugins mit dem offiziellen Elgato-SDK
    ab Version 6) laufen unter Linux mit dem System-Node.js. Plugins, die nur
    als Windows-/macOS-Programm vorliegen, können nicht gestartet werden.
    Linux-Plugins können zusätzlich "CodePathLin" im Manifest angeben.
  * Gestartet wird wie von der Elgato-App:
      <Programm> -port P -pluginUUID U -registerEvent registerPlugin -info JSON
  * Ereignisse an das Plugin: keyDown/keyUp, willAppear/willDisappear,
    dialRotate/dialDown/dialUp, touchTap, deviceDidConnect/-Disconnect,
    didReceiveSettings, didReceiveGlobalSettings
  * Befehle vom Plugin: setTitle, setImage, setState, showAlert, showOk,
    setSettings, getSettings, setGlobalSettings, getGlobalSettings, openUrl,
    logMessage, switchToProfile, setFeedback, setFeedbackLayout

Property Inspectors (HTML-Einstellungsseiten) werden nicht angezeigt; die
Einstellungen lassen sich im Editor als JSON bearbeiten.
"""
import base64
import io
import json
import logging
import os
import platform
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import uuid as uuidlib
import zipfile
from pathlib import Path

from . import __version__, paths, system, ws
from .models import model_name

log = logging.getLogger("lindeck.plugins")

SDK_APP_VERSION = "6.7.0"
# Elgato-Gerätetypen im SDK (DeviceType)
DEVICE_TYPES = {"20GAA": 0, "20GBA": 0, "20GAI": 1, "20GAT": 2, "20GBF": 5, "20GBD": 7, "20GBX": 7,
                "20GBJ": 9, "20GBO": 10}
NODE_EXT = (".js", ".mjs", ".cjs")


# ==========================================================================
# Installierte Plugins
# ==========================================================================
class PluginInfo:
    def __init__(self, d: Path):
        self.dir = Path(d)
        self.manifest = json.loads((self.dir / "manifest.json").read_text(encoding="utf-8-sig"))
        m = self.manifest
        self.uuid = m.get("UUID") or self.dir.name[: -len(".sdPlugin")]
        self.name = m.get("Name") or self.uuid
        self.version = str(m.get("Version") or "")
        self.actions = {a.get("UUID"): a for a in m.get("Actions") or [] if a.get("UUID")}
        self.code_path, self.runtime, self.reason = self._runtime()

    def _runtime(self):
        m = self.manifest
        for key in ("CodePathLin", "CodePathLinux"):
            if m.get(key):
                return m[key], ("node" if m[key].endswith(NODE_EXT) else "exec"), ""
        code = m.get("CodePath") or ""
        if code.endswith(NODE_EXT) or (m.get("Nodejs") and code and not code.endswith((".exe", ".html", ".htm"))):
            return code, "node", ""
        if code.endswith((".html", ".htm")):
            return code, None, "HTML-Plugin (läuft nur in der Elgato-App)"
        if code and (self.dir / code).is_file() and _is_elf(self.dir / code):
            return code, "exec", ""
        return code, None, "nur für Windows/macOS gebaut"

    @property
    def runnable(self):
        return self.runtime is not None

    def action_icon(self, action_uuid, state=0):
        a = self.actions.get(action_uuid) or {}
        states = a.get("States") or []
        rel = (states[min(state, len(states) - 1)].get("Image") if states else None) or a.get("Icon")
        return _image_file(self.dir / rel) if rel else None


def _is_elf(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def _image_file(base: Path):
    for ext in ("", ".png", "@2x.png", ".svg", ".gif", ".jpg"):
        p = Path(str(base) + ext)
        if p.is_file():
            return p
    return None


_cache = {"mtime": None, "plugins": {}}


def installed(plugins_dir=None) -> dict:
    """UUID -> PluginInfo aller installierten Plugins (zwischengespeichert)."""
    root = Path(plugins_dir or paths.PLUGINS_DIR)
    try:
        mtime = (root, root.stat().st_mtime, tuple(sorted(d.stat().st_mtime for d in root.iterdir())))
    except OSError:
        return {}
    if _cache["mtime"] == mtime:
        return _cache["plugins"]
    found = {}
    for d in sorted(root.iterdir()):
        if d.is_dir() and (d / "manifest.json").exists():
            try:
                info = PluginInfo(d)
                found[info.uuid] = info
            except (OSError, ValueError) as e:
                log.warning("Plugin %s nicht lesbar: %s", d.name, e)
    _cache.update(mtime=mtime, plugins=found)
    return found


def plugin_for_action(action_uuid: str, plugins_dir=None):
    for info in installed(plugins_dir).values():
        if action_uuid in info.actions:
            return info
    for info in installed(plugins_dir).values():
        if action_uuid.startswith(info.uuid + "."):
            return info
    return None


def install(src, plugins_dir=None) -> list:
    """Installiert .streamDeckPlugin (ZIP) oder einen *.sdPlugin-Ordner. Gibt die Plugin-UUIDs zurück."""
    src = Path(src).expanduser()
    root = Path(plugins_dir or paths.PLUGINS_DIR)
    root.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        if src.is_file():
            tmp = Path(tempfile.mkdtemp(prefix="lindeck-plugin-"))
            from .profiles import _safe_extract
            with zipfile.ZipFile(src) as z:
                _safe_extract(z, tmp)
            base = tmp
        else:
            base = src
        dirs = [base] if base.name.endswith(".sdPlugin") else \
            [d for d in base.rglob("*.sdPlugin") if (d / "manifest.json").exists()]
        out = []
        for d in dirs:
            info = PluginInfo(d)
            dst = root / f"{info.uuid}.sdPlugin"
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(d, dst)
            for f in dst.rglob("*"):
                if f.is_file() and (f.suffix == "" or _is_elf(f)):
                    f.chmod(f.stat().st_mode | 0o111)
            out.append(info.uuid)
        os.utime(root)
        return out
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def remove(plugin_uuid, plugins_dir=None) -> bool:
    d = Path(plugins_dir or paths.PLUGINS_DIR) / f"{plugin_uuid}.sdPlugin"
    if d.exists():
        shutil.rmtree(d)
        return True
    return False


def decode_image(data: str, plugin_dir: Path = None, size=144):
    """setImage-Daten (data-URL mit PNG/JPG/SVG, Base64 oder Dateipfad) -> PIL-Bild oder None."""
    from PIL import Image
    if not data:
        return None
    raw, is_svg = None, False
    if data.startswith("data:"):
        header, _, body = data.partition(",")
        is_svg = "svg" in header
        raw = base64.b64decode(body) if ";base64" in header else urllib.parse.unquote(body).encode()
    elif data.lstrip().startswith("<svg") or data.lstrip().startswith("<?xml"):
        raw, is_svg = data.encode(), True
    elif plugin_dir is not None and _image_file(plugin_dir / data):
        p = _image_file(plugin_dir / data)
        raw, is_svg = p.read_bytes(), p.suffix == ".svg"
    else:
        try:
            raw = base64.b64decode(data, validate=True)
        except ValueError:
            return None
    if is_svg:
        try:
            import cairosvg
            raw = cairosvg.svg2png(bytestring=raw, output_width=size, output_height=size)
        except Exception as e:  # noqa: BLE001
            log.debug("SVG nicht darstellbar: %s", e)
            return None
    try:
        return Image.open(io.BytesIO(raw)).convert("RGBA")
    except (OSError, ValueError):
        return None


# ==========================================================================
# Laufende Plugins
# ==========================================================================
class PluginProcess:
    MAX_RESTARTS = 5

    def __init__(self, host, info: PluginInfo):
        self.host, self.info = host, info
        self.proc = None
        self.conn = None
        self.restarts = 0
        self.stopping = False
        self._lock = threading.Lock()

    def command(self):
        exe = self.info.dir / self.info.code_path
        if self.info.runtime == "node":
            node = self.host.node
            if not node:
                raise RuntimeError("Node.js nicht gefunden (Paket „nodejs“ installieren oder node_path setzen)")
            return [node, str(exe)]
        return [str(exe)]

    def start(self):
        try:
            cmd = self.command() + ["-port", str(self.host.port), "-pluginUUID", self.info.uuid,
                                    "-registerEvent", "registerPlugin", "-info", json.dumps(self.host.info_for(self.info))]
        except RuntimeError as e:
            log.warning("Plugin %s: %s", self.info.name, e)
            return False
        logdir = paths.STATE_DIR / "plugins"
        logdir.mkdir(parents=True, exist_ok=True)
        logf = open(logdir / f"{self.info.uuid}.log", "ab")
        try:
            self.proc = subprocess.Popen(cmd, cwd=str(self.info.dir), stdin=subprocess.DEVNULL, stdout=logf,
                                         stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e:
            log.warning("Plugin %s nicht startbar: %s", self.info.name, e)
            return False
        finally:
            logf.close()
        log.info("Plugin gestartet: %s %s", self.info.name, self.info.version)
        threading.Thread(target=self._watch, daemon=True).start()
        return True

    def _watch(self):
        proc = self.proc
        code = proc.wait()
        if self.stopping or proc is not self.proc:
            return
        self.conn = None
        log.warning("Plugin %s beendet (Code %s)", self.info.name, code)
        if self.restarts < self.MAX_RESTARTS:
            self.restarts += 1
            time.sleep(min(30, 2 ** self.restarts))
            if not self.stopping:
                self.start()

    def attach(self, conn):
        with self._lock:
            self.conn = conn

    def send(self, msg: dict):
        """Ohne Verbindung wird verworfen – nach dem Anmelden gibt es ohnehin willAppear für alles Sichtbare."""
        with self._lock:
            conn = self.conn
            if conn is None:
                return
        try:
            conn.send(json.dumps(msg))
        except ws.Closed:
            with self._lock:
                self.conn = None

    def stop(self):
        self.stopping = True
        if self.conn:
            self.conn.close()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class PluginHost:
    """Schnittstelle zwischen Geräten (DeckController) und Elgato-Plugins."""

    def __init__(self, daemon=None, plugins_dir=None, node=None, autostart=True):
        self.daemon = daemon
        self.plugins_dir = Path(plugins_dir or paths.PLUGINS_DIR)
        cfg_node = daemon.cfg.get("node_path") if daemon and getattr(daemon, "cfg", None) else None
        self.node = node or cfg_node or shutil.which("node") or shutil.which("nodejs")
        self.procs = {}           # Plugin-UUID -> PluginProcess
        self.contexts = {}        # Kontext -> Aktion
        self.visible = set()      # Kontexte, die gerade angezeigt werden
        self._lock = threading.RLock()
        self.server = ws.Server(self._on_connect)
        self.port = self.server.port
        if autostart:
            self.sync()

    # ------------------------------------------------------------ Plugins
    def sync(self):
        """Startet neu installierte Plugins und beendet entfernte."""
        plugins = installed(self.plugins_dir)
        for uid, info in plugins.items():
            if uid in self.procs or not info.runnable:
                if not info.runnable and uid not in self.procs:
                    log.info("Plugin %s wird nicht gestartet: %s", info.name, info.reason)
                    self.procs[uid] = None
                continue
            p = PluginProcess(self, info)
            self.procs[uid] = p
            p.start()
        for uid in list(self.procs):
            if uid not in plugins:
                p = self.procs.pop(uid)
                if p:
                    p.stop()

    def info_for(self, info: PluginInfo):
        devices = []
        for ctrl in self._controllers():
            devices.append(self._device_info(ctrl))
        return {
            "application": {"font": "", "language": (os.environ.get("LANG") or "de")[:2], "platform": "linux",
                            "platformVersion": platform.release(), "version": SDK_APP_VERSION},
            "plugin": {"uuid": info.uuid, "version": info.version},
            "devicePixelRatio": 2,
            "colors": {"buttonPressedBackgroundColor": "#303030FF", "buttonPressedBorderColor": "#646464FF",
                       "buttonPressedTextColor": "#969696FF", "disabledColor": "#007AFF59",
                       "highlightColor": "#007AFFFF", "mouseDownColor": "#2EA8FFFF"},
            "devices": devices,
            "lindeck": __version__,
        }

    def _controllers(self):
        d = self.daemon
        return list(getattr(d, "controllers", {}).values()) if d else []

    @staticmethod
    def _device_info(ctrl):
        code = (ctrl.model or "")[:5]
        return {"id": ctrl.serial, "name": model_name(ctrl.model),
                "size": {"columns": ctrl.geometry.cols, "rows": ctrl.geometry.rows},
                "type": DEVICE_TYPES.get(code, 0)}

    def stop(self):
        for p in self.procs.values():
            if p:
                p.stop()
        self.server.close()

    # ---------------------------------------------------------- Kontexte
    def _proc_for(self, action):
        info = plugin_for_action(action.uuid, self.plugins_dir)
        if not info:
            return None
        return self.procs.get(info.uuid)

    def handles(self, action) -> bool:
        return self._proc_for(action) is not None

    def context(self, action) -> str:
        ctx = getattr(action, "sdk_context", None)
        if ctx is None:
            ctx = action.sdk_context = uuidlib.uuid4().hex.upper()
        with self._lock:
            self.contexts[ctx] = action
        return ctx

    @staticmethod
    def _ctrl(action):
        return action.ctx.ctrl if getattr(action, "ctx", None) else None

    def _coords(self, action):
        ctrl, slot = self._ctrl(action), action.slot
        if not slot or ctrl is None:
            return {"column": 0, "row": 0}
        if slot[0] == "key":
            return {"column": slot[1] % ctrl.geometry.cols, "row": slot[1] // ctrl.geometry.cols}
        return {"column": slot[1], "row": 0}

    def _event(self, event, action, payload=None):
        p = self._proc_for(action)
        if p is None:
            return False
        ctrl = self._ctrl(action)
        base = {"settings": action.settings, "coordinates": self._coords(action), "state": action.state,
                "isInMultiAction": action.slot is None}
        if action.slot and action.slot[0] == "dial":
            base["controller"] = "Encoder"
        else:
            base["controller"] = "Keypad"
        base.update(payload or {})
        p.send({"event": event, "action": action.uuid, "context": self.context(action),
                "device": getattr(ctrl, "serial", ""), "payload": base})
        return True

    # ---------------------------------------- Aufrufe aus der Engine
    def will_appear(self, ctrl, action):
        if not self.handles(action):
            return False
        self.visible.add(self.context(action))
        return self._event("willAppear", action)

    def will_disappear(self, ctrl, action):
        ctx = getattr(action, "sdk_context", None)
        if ctx:
            self.visible.discard(ctx)
        return self._event("willDisappear", action) if self.handles(action) else False

    def key_down(self, action):
        if not self._event("keyDown", action):
            action.alert()
            return False
        return True

    def key_up(self, action):
        if not self._event("keyUp", action):
            return False
        info = plugin_for_action(action.uuid, self.plugins_dir)
        meta = info.actions.get(action.uuid, {}) if info else {}
        if len(action.states) > 1 and not meta.get("DisableAutomaticStates"):
            action.set_state(action.state + 1)
        return True

    def dial_rotate(self, action, ticks):
        return self._event("dialRotate", action, {"ticks": ticks, "pressed": False})

    def dial_down(self, action):
        if not self._event("dialDown", action):
            action.alert()
            return False
        return True

    def dial_up(self, action):
        return self._event("dialUp", action)

    def touch_tap(self, action, pos, long):
        pos = pos or {}
        return self._event("touchTap", action, {"tapPos": [pos.get("x", 0), pos.get("y", 0)], "hold": bool(long)})

    def device_connected(self, ctrl):
        for p in self.procs.values():
            if p:
                p.send({"event": "deviceDidConnect", "device": ctrl.serial, "deviceInfo": self._device_info(ctrl)})

    def device_disconnected(self, ctrl):
        for p in self.procs.values():
            if p:
                p.send({"event": "deviceDidDisconnect", "device": ctrl.serial})

    # ------------------------------------------------- Plugin-Verbindung
    def _on_connect(self, conn):
        try:
            first = json.loads(conn.recv())
        except (ws.Closed, ValueError):
            conn.close()
            return
        if first.get("event") != "registerPlugin":
            conn.close()  # Property Inspector o. Ä. – nicht unterstützt
            return
        proc = self.procs.get(first.get("uuid"))
        if proc is None:
            log.warning("Unbekanntes Plugin meldet sich an: %s", first.get("uuid"))
            conn.close()
            return
        log.info("Plugin verbunden: %s", proc.info.name)
        proc.attach(conn)
        for ctrl in self._controllers():
            proc.send({"event": "deviceDidConnect", "device": ctrl.serial, "deviceInfo": self._device_info(ctrl)})
        with self._lock:
            for ctx in list(self.visible):
                a = self.contexts.get(ctx)
                if a is not None and self._proc_for(a) is proc:
                    self._event("willAppear", a)
        while True:
            try:
                msg = json.loads(conn.recv())
            except ws.Closed:
                break
            except ValueError:
                continue
            try:
                self.handle(proc, msg)
            except Exception as e:  # noqa: BLE001 - ein fehlerhaftes Plugin darf den Dienst nicht stören
                log.warning("Plugin %s: Befehl %s fehlgeschlagen: %s", proc.info.name, msg.get("event"), e)
        if proc.conn is conn:
            proc.conn = None

    def handle(self, proc, msg):
        ev = msg.get("event")
        ctx = msg.get("context")
        payload = msg.get("payload") or {}
        a = self.contexts.get(ctx) if ctx else None
        if ev == "setTitle" and a is not None:
            t = payload.get("title")
            a.title_override = None if t is None else str(t)
            a.redraw()
        elif ev == "setImage" and a is not None:
            a.image_override = decode_image(payload.get("image") or "", proc.info.dir)
            a.redraw()
        elif ev == "setState" and a is not None:
            a.set_state(int(payload.get("state") or 0))
        elif ev == "showAlert" and a is not None:
            a.alert()
        elif ev == "showOk" and a is not None:
            a.ok()
        elif ev == "setSettings" and a is not None:
            self._store_settings(a, payload if isinstance(payload, dict) else {})
        elif ev == "getSettings" and a is not None:
            proc.send({"event": "didReceiveSettings", "action": a.uuid, "context": ctx,
                       "device": getattr(self._ctrl(a), "serial", ""),
                       "payload": {"settings": a.settings, "coordinates": self._coords(a),
                                   "isInMultiAction": a.slot is None, "state": a.state}})
        elif ev == "setGlobalSettings":
            f = paths.PLUGIN_DATA_DIR / f"{proc.info.uuid}.json"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(payload), encoding="utf-8")
        elif ev == "getGlobalSettings":
            f = paths.PLUGIN_DATA_DIR / f"{proc.info.uuid}.json"
            data = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
            proc.send({"event": "didReceiveGlobalSettings", "payload": {"settings": data}})
        elif ev == "openUrl":
            url = payload.get("url") or ""
            if url.startswith(("http://", "https://", "mailto:")):
                system.run_bg(["xdg-open", url])
        elif ev == "logMessage":
            log.info("[%s] %s", proc.info.name, payload.get("message"))
        elif ev == "switchToProfile":
            self._switch_profile(msg.get("device"), payload.get("profile"))
        elif ev == "setFeedback" and a is not None:
            fb = dict(getattr(a, "sdk_feedback", None) or {})
            for k, v in payload.items():
                if k == "icon":
                    a.encoder_icon_image = decode_image(v if isinstance(v, str) else (v or {}).get("value", ""),
                                                        proc.info.dir, 96)
                else:
                    fb[k] = v
            a.sdk_feedback = fb
            a.redraw()
        elif ev in ("setFeedbackLayout", "setTriggerDescription", "sendToPropertyInspector"):
            pass
        else:
            log.debug("Plugin %s: unbekannter Befehl %s", proc.info.name, ev)

    def _store_settings(self, action, settings):
        old = json.dumps(action.data, sort_keys=True)
        action.settings = settings
        action.data["Settings"] = settings
        page = getattr(action, "page", None)
        if page is not None and getattr(page, "dir", None):
            from .profiles import save_action_settings
            try:
                save_action_settings(page.dir, old, settings)
            except (OSError, ValueError) as e:
                log.warning("Einstellungen nicht gespeichert: %s", e)

    def _switch_profile(self, device, profile):
        for ctrl in self._controllers():
            if (not device or ctrl.serial == device) and profile:
                ctrl.switch_profile(profile)
