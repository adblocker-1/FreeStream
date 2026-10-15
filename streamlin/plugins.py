"""Plugin-Host für das Elgato-Stream-Deck-SDK (WebSocket-Protokoll).

Elgato-Plugins sind eigenständige Programme, die sich per WebSocket mit der
Stream-Deck-App verbinden. LinDeck übernimmt diese Rolle:

  * Plugins werden aus ~/.local/share/streamlin/plugins/<UUID>.sdPlugin geladen
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

Property Inspector (HTML-Einstellungsseite eines Plugins): Der Host liefert
die Seite über HTTP auf demselben Port aus (geschützt durch einen zufälligen
Schlüssel), ruft wie die Elgato-App `connectElgatoStreamDeckSocket(...)` auf
und vermittelt setSettings/getSettings/sendToPlugin/sendToPropertyInspector
und die globalen Einstellungen. Der Editor öffnet die Seite in einem
Browserfenster. Port und Schlüssel stehen in ~/.local/state/streamlin/plugin-host.json.
"""
import base64
import io
import json
import logging
import os
import mimetypes
import platform
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import uuid as uuidlib
import zipfile
from pathlib import Path

from . import __version__, i18n, paths, system, ws
from .i18n import _
from .models import model_name

log = logging.getLogger("streamlin.plugins")

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
            return code, None, _("HTML plugin (runs only in the Elgato app)")
        if code and (self.dir / code).is_file() and _is_elf(self.dir / code):
            return code, "exec", ""
        return code, None, _("built for Windows/macOS only")

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
                log.warning(_("Plugin {name} not readable: {error}").format(name=d.name, error=e))
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
            tmp = Path(tempfile.mkdtemp(prefix="streamlin-plugin-"))
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
        header, __, body = data.partition(",")
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
            log.debug(_("SVG cannot be rendered: {error}").format(error=e))
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
                raise RuntimeError(_("Node.js not found (install the “nodejs” package or set node_path)"))
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
            log.warning(_("Plugin {name} cannot be started: {error}").format(name=self.info.name, error=e))
            return False
        finally:
            logf.close()
        log.info(_("Plugin started: {name} {version}").format(name=self.info.name, version=self.info.version))
        threading.Thread(target=self._watch, daemon=True).start()
        return True

    def _watch(self):
        proc = self.proc
        code = proc.wait()
        if self.stopping or proc is not self.proc:
            return
        self.conn = None
        log.warning(_("Plugin {name} exited (code {code})").format(name=self.info.name, code=code))
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

    def __init__(self, daemon=None, plugins_dir=None, node=None, autostart=True, publish=None):
        self.daemon = daemon
        self.plugins_dir = Path(plugins_dir or paths.PLUGINS_DIR)
        cfg_node = daemon.cfg.get("node_path") if daemon and getattr(daemon, "cfg", None) else None
        self.node = node or cfg_node or shutil.which("node") or shutil.which("nodejs")
        self.procs = {}           # Plugin-UUID -> PluginProcess
        self.contexts = {}        # Kontext -> Aktion
        self.visible = set()      # Kontexte, die gerade angezeigt werden
        self.pis = {}             # Kontext -> Verbindung des Property Inspectors
        self.token = secrets.token_urlsafe(18)
        self._lock = threading.RLock()
        self.server = ws.Server(self._on_connect, on_http=self._on_http)
        self.port = self.server.port
        self.publish = daemon is not None if publish is None else publish
        if self.publish:
            paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
            f = paths.STATE_DIR / "plugin-host.json"
            f.write_text(json.dumps({"port": self.port, "token": self.token, "pid": os.getpid()}), encoding="utf-8")
            os.chmod(f, 0o600)
        if autostart:
            self.sync()

    # ------------------------------------------------------------ Plugins
    def sync(self):
        """Startet neu installierte Plugins und beendet entfernte."""
        plugins = installed(self.plugins_dir)
        for uid, info in plugins.items():
            if uid in self.procs or not info.runnable:
                if not info.runnable and uid not in self.procs:
                    log.info(_("Plugin {name} will not be started: {reason}").format(name=info.name, reason=info.reason))
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
            "application": {"font": "", "language": i18n.current().split("_")[0], "platform": "linux",
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
        for conn in list(self.pis.values()):
            conn.close()
        self.server.close()
        if self.publish:
            try:
                (paths.STATE_DIR / "plugin-host.json").unlink()
            except OSError:
                pass

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
        if getattr(action, "sdk_coords", None):
            return action.sdk_coords
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
    def _origin_ok(self, headers):
        """Nur Plugins (ohne Origin) und die eigenen Einstellungsseiten dürfen sich verbinden."""
        origin = (headers or {}).get("origin")
        return not origin or origin in (f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}")

    def _on_connect(self, conn):
        if not self._origin_ok(conn.headers):
            log.warning(_("WebSocket connection from foreign origin rejected: {origin}").format(
                origin=conn.headers.get("origin")))
            conn.close()
            return
        try:
            first = json.loads(conn.recv())
        except (ws.Closed, ValueError):
            conn.close()
            return
        if first.get("event") == "registerPropertyInspector":
            self._pi_session(conn, first.get("uuid"))
            return
        if first.get("event") != "registerPlugin":
            conn.close()
            return
        proc = self.procs.get(first.get("uuid"))
        if proc is None:
            log.warning(_("Unknown plugin tried to register: {uuid}").format(uuid=first.get("uuid")))
            conn.close()
            return
        log.info(_("Plugin connected: {name}").format(name=proc.info.name))
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
                log.warning(_("Plugin {name}: command {command} failed: {error}").format(
                    name=proc.info.name, command=msg.get("event"), error=e))
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
            if ctx in self.pis:
                self._pi_send(self.pis[ctx], self._settings_msg(a))
        elif ev == "getSettings" and a is not None:
            proc.send({"event": "didReceiveSettings", "action": a.uuid, "context": ctx,
                       "device": getattr(self._ctrl(a), "serial", ""),
                       "payload": {"settings": a.settings, "coordinates": self._coords(a),
                                   "isInMultiAction": a.slot is None, "state": a.state}})
        elif ev == "setGlobalSettings":
            self._set_global(proc, payload)
            for c, pi in list(self.pis.items()):
                pa = self.contexts.get(c)
                if pa is not None and self._proc_for(pa) is proc:
                    self._pi_send(pi, {"event": "didReceiveGlobalSettings", "payload": {"settings": payload}})
        elif ev == "getGlobalSettings":
            proc.send({"event": "didReceiveGlobalSettings", "payload": {"settings": self._get_global(proc)}})
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
        elif ev == "sendToPropertyInspector":
            pi = self.pis.get(ctx)
            if pi is not None and a is not None:
                self._pi_send(pi, {"event": "sendToPropertyInspector", "action": a.uuid, "context": ctx,
                                   "payload": payload})
        elif ev in ("setFeedbackLayout", "setTriggerDescription"):
            pass
        else:
            log.debug(_("Plugin {name}: unknown command {command}").format(name=proc.info.name, command=ev))

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
                log.warning(_("Settings not saved: {error}").format(error=e))

    def _switch_profile(self, device, profile):
        for ctrl in self._controllers():
            if (not device or ctrl.serial == device) and profile:
                ctrl.switch_profile(profile)

    # ------------------------------------------------ globale Einstellungen
    @staticmethod
    def _global_file(proc):
        return paths.PLUGIN_DATA_DIR / f"{proc.info.uuid}.json"

    def _set_global(self, proc, data):
        f = self._global_file(proc)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data), encoding="utf-8")

    def _get_global(self, proc):
        f = self._global_file(proc)
        try:
            return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        except ValueError:
            return {}

    def _settings_msg(self, a):
        return {"event": "didReceiveSettings", "action": a.uuid, "context": self.context(a),
                "device": getattr(self._ctrl(a), "serial", ""),
                "payload": {"settings": a.settings, "coordinates": self._coords(a),
                            "isInMultiAction": a.slot is None and not getattr(a, "sdk_coords", None),
                            "state": a.state}}

    # ---------------------------------------------- Property Inspector
    @staticmethod
    def _pi_send(conn, msg):
        try:
            conn.send(json.dumps(msg))
        except ws.Closed:
            pass

    def pi_path(self, action):
        info = plugin_for_action(action.uuid, self.plugins_dir)
        if not info:
            return None, None
        meta = info.actions.get(action.uuid) or {}
        rel = meta.get("PropertyInspectorPath") or info.manifest.get("PropertyInspectorPath")
        return info, rel

    def open_context(self, profile_id, page_uuid, slot):
        """Aktion aus einem Profil für die Einstellungsseite bereitstellen (auch wenn sie nicht sichtbar ist)."""
        from . import actions as A
        from .profiles import ProfileStore
        store = getattr(self.daemon, "store", None) or ProfileStore()
        prof = store.get(profile_id)
        if prof is None:
            store.reload()
            prof = store.get(profile_id)
        if prof is None:
            raise LookupError(_("Profile not found"))
        prof._page_cache.clear()
        page = prof.page(page_uuid)
        if page is None:
            raise LookupError(_("Page not found"))
        kind, __, pos = slot.partition(":")
        if kind == "dial":
            data, coords = page.encoders.get(int(pos)), {"column": int(pos), "row": 0}
        else:
            c, r = (int(x) for x in pos.split(","))
            data, coords = page.keypad.get((c, r)), {"column": c, "row": r}
        if not data:
            raise LookupError(_("No action in this slot"))
        # Läuft die Aktion schon auf einem Gerät? Dann denselben Kontext nehmen
        with self._lock:
            for ctx, a in self.contexts.items():
                pg = getattr(a, "page", None)
                if pg is not None and Path(pg.dir) == Path(page.dir) and a.uuid == data.get("UUID") and (
                        (data.get("ActionID") and a.data.get("ActionID") == data.get("ActionID")) or a.data == data):
                    return a
        a = A.create_action(data, page, A.Context(None, getattr(self.daemon, "cfg", None) or {}, None, None))
        a.slot = ("dial", int(pos)) if kind == "dial" else None
        a.sdk_coords = coords
        a.sdk_detached = True
        self.context(a)
        return a

    def _on_http(self, path, headers):
        url = urllib.parse.urlsplit(path)
        parts = [urllib.parse.unquote(x) for x in url.path.split("/") if x]
        q = dict(urllib.parse.parse_qsl(url.query))
        if len(parts) < 2 or not secrets.compare_digest(parts[1], self.token):
            return 403, "text/plain; charset=utf-8", _("Access denied").encode(), {}
        if parts[0] == "pi":
            try:
                a = self.open_context(q.get("profile", ""), q.get("page", ""), q.get("slot", ""))
            except (LookupError, ValueError) as e:
                return 404, "text/plain; charset=utf-8", str(e).encode(), {}
            info, rel = self.pi_path(a)
            if not rel:
                return 404, "text/plain; charset=utf-8", _("This plugin has no settings page").encode(), {}
            loc = f"/plugin/{self.token}/{urllib.parse.quote(info.uuid)}/{urllib.parse.quote(rel)}" \
                  f"?ctx={self.context(a)}"
            return 302, "text/plain", b"", {"Location": loc}
        if parts[0] == "plugin" and len(parts) >= 4:
            info = installed(self.plugins_dir).get(parts[2])
            if not info:
                return 404, "text/plain; charset=utf-8", _("Plugin not installed").encode(), {}
            base = info.dir.resolve()
            f = (base / "/".join(parts[3:])).resolve()
            if base not in f.parents or not f.is_file():
                return 404, "text/plain; charset=utf-8", _("File not found").encode(), {}
            body = f.read_bytes()
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            if f.suffix.lower() in (".html", ".htm") and q.get("ctx") in self.contexts:
                body = self._inject(body, info, self.contexts[q["ctx"]])
                ctype = "text/html; charset=utf-8"
            elif ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8"
            return 200, ctype, body, {}
        return 404, "text/plain; charset=utf-8", _("Not found").encode(), {}

    def _inject(self, html: bytes, info, a) -> bytes:
        ctx = self.context(a)
        action_info = {"action": a.uuid, "context": ctx, "device": getattr(self._ctrl(a), "serial", ""),
                       "payload": {"settings": a.settings, "coordinates": self._coords(a),
                                   "controller": "Encoder" if a.slot and a.slot[0] == "dial" else "Keypad",
                                   "state": a.state}}
        def js(value):
            # JSON-Text als JS-String; "</" maskieren, damit nichts aus dem <script> ausbrechen kann
            return json.dumps(json.dumps(value)).replace("</", "<\\/")
        boot = ("<script>/* LinDeck: Start wie in der Elgato-App */(function(){"
                f"var port={self.port},uuid={json.dumps(ctx)},info={js(self.info_for(info))},ai={js(action_info)};"
                "function go(){if(typeof window.connectElgatoStreamDeckSocket==='function'){"
                "window.connectElgatoStreamDeckSocket(port,uuid,'registerPropertyInspector',info,ai);}"
                "else{console.error('connectElgatoStreamDeckSocket missing');}}"
                "if(document.readyState==='complete'){setTimeout(go,0);}else{window.addEventListener('load',go);}"
                "})();</script>")
        text = html.decode("utf-8", errors="replace")
        title = f"<title>{info.name} – LinDeck</title>" if "<title>" not in text.lower() else ""
        idx = text.lower().rfind("</body>")
        text = (text[:idx] + boot + text[idx:]) if idx >= 0 else text + boot
        if title:
            h = text.lower().find("<head>")
            text = text[:h + 6] + title + text[h + 6:] if h >= 0 else title + text
        return text.encode("utf-8")

    def _pi_session(self, conn, ctx):
        a = self.contexts.get(ctx)
        proc = self._proc_for(a) if a is not None else None
        if a is None or proc is None:
            conn.close()
            return
        old = self.pis.get(ctx)
        if old is not None:
            old.close()
        self.pis[ctx] = conn
        detached = getattr(a, "sdk_detached", False)
        if detached and ctx not in self.visible:
            self._event("willAppear", a)
        proc.send({"event": "propertyInspectorDidAppear", "action": a.uuid, "context": ctx,
                   "device": getattr(self._ctrl(a), "serial", "")})
        try:
            while True:
                try:
                    msg = json.loads(conn.recv())
                except ValueError:
                    continue
                try:
                    self._pi_handle(proc, a, ctx, conn, msg)
                except Exception as e:  # noqa: BLE001
                    log.warning(_("Settings page {name}: {error}").format(name=proc.info.name, error=e))
        except ws.Closed:
            pass
        if self.pis.get(ctx) is conn:
            del self.pis[ctx]
        proc.send({"event": "propertyInspectorDidDisappear", "action": a.uuid, "context": ctx,
                   "device": getattr(self._ctrl(a), "serial", "")})
        if detached and ctx not in self.visible:
            self._event("willDisappear", a)

    def _pi_handle(self, proc, a, ctx, conn, msg):
        ev = msg.get("event")
        payload = msg.get("payload")
        if ev == "setSettings":
            self._store_settings(a, payload if isinstance(payload, dict) else {})
            proc.send(self._settings_msg(a))
        elif ev == "getSettings":
            self._pi_send(conn, self._settings_msg(a))
        elif ev == "sendToPlugin":
            proc.send({"event": "sendToPlugin", "action": a.uuid, "context": ctx, "payload": payload or {}})
        elif ev == "setGlobalSettings":
            self._set_global(proc, payload or {})
            proc.send({"event": "didReceiveGlobalSettings", "payload": {"settings": payload or {}}})
        elif ev == "getGlobalSettings":
            self._pi_send(conn, {"event": "didReceiveGlobalSettings", "payload": {"settings": self._get_global(proc)}})
        elif ev == "openUrl":
            url = (payload or {}).get("url") or ""
            if url.startswith(("http://", "https://", "mailto:")):
                system.run_bg(["xdg-open", url])
        elif ev == "logMessage":
            log.info(_("[{name} settings] {message}").format(name=proc.info.name,
                                                         message=(payload or {}).get("message")))
