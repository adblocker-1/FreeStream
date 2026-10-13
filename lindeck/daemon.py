"""Hintergrunddienst: erkennt Geräte (inkl. Hotplug), lädt Profile neu, Smart Profiles."""
import logging
import os
import signal
import threading
import time

from . import autoimport, paths, system
from .config import Config, State
from .engine import DeckController
from .keyboard import VirtualInput
from .profiles import ProfileStore

log = logging.getLogger("lindeck.daemon")


class Daemon:
    def __init__(self, transport=None):
        paths.ensure_dirs()
        autoimport.adopt_session_env()
        self.cfg = Config()
        self.state = State()
        self.store = ProfileStore()
        self.vin = VirtualInput(self.cfg.get("keyboard_layout", "auto"))
        self.sound = system.SoundPlayer()
        self.transport = transport
        self.controllers = {}
        self.running = True
        self._last_window = ""
        self._smart_prev = {}

    def _manager(self):
        from StreamDeck.DeviceManager import DeviceManager
        return DeviceManager(transport=self.transport) if self.transport else DeviceManager()

    def scan(self):
        try:
            decks = self._manager().enumerate()
        except Exception as e:  # noqa: BLE001
            log.error("USB-Zugriff fehlgeschlagen: %s (ist libhidapi installiert?)", e)
            return
        seen = set()
        for deck in decks:
            did = deck.id()
            seen.add(did)
            if did in self.controllers:
                continue
            try:
                deck.open()
                ctrl = DeckController(deck, self.store, self.cfg, self.state, self.vin, self.sound)
                ctrl.plugin_host = getattr(self, "plugin_host", None)
                ctrl.start()
                self.controllers[did] = ctrl
                if ctrl.plugin_host:
                    ctrl.plugin_host.device_connected(ctrl)
            except Exception as e:  # noqa: BLE001
                log.error("Gerät %s nicht nutzbar: %s (udev-Regel installiert?)", did, e)
                try:
                    deck.close()
                except Exception:  # noqa: BLE001
                    pass
        for did in list(self.controllers):
            ctrl = self.controllers[did]
            alive = did in seen
            try:
                alive = alive and ctrl.deck.connected()
            except Exception:  # noqa: BLE001
                alive = False
            if not alive:
                log.info("Gerät %s getrennt", ctrl.serial)
                if getattr(self, "plugin_host", None):
                    self.plugin_host.device_disconnected(ctrl)
                ctrl.running = False
                ctrl.events.put(("stop",))
                del self.controllers[did]

    def reload(self):
        log.info("Lade Profile und Konfiguration neu")
        self.cfg.load()
        self.store.reload()
        for ctrl in self.controllers.values():
            pid = ctrl.profile.id if ctrl.profile else None
            if not pid or not self.store.get(pid):
                pid = ctrl._initial_profile_id()
            ctrl.load_profile(pid, ctrl.page_index)

    def smart_profiles(self):
        """Wechselt Profile je nach aktivem Fenster (Elgato 'Smart Profiles')."""
        mapping = {k.lower(): v for k, v in (self.cfg.get("smart_profiles") or {}).items()}
        auto = {}
        for p in self.store.profiles.values():
            if p.app_identifier:
                stem = os.path.basename(p.app_identifier.replace("\\", "/")).lower()
                stem = stem.rsplit(".", 1)[0] if stem.endswith((".exe", ".app")) else stem
                auto[stem] = p.id
                auto[stem.rstrip("0123456789")] = p.id
        if not mapping and not auto:
            return
        win = system.active_window_class()
        if win == self._last_window:
            return
        self._last_window = win
        target = mapping.get(win) or auto.get(win) or next((v for k, v in auto.items() if k and k in win), None)
        for ctrl in self.controllers.values():
            if target and ctrl.profile and ctrl.profile.id != target:
                p = self.store.get(target)
                if p and (p in ctrl.candidate_profiles()):
                    self._smart_prev.setdefault(ctrl.serial, ctrl.profile.id)
                    ctrl.load_profile(target)
            elif not target and ctrl.serial in self._smart_prev:
                ctrl.load_profile(self._smart_prev.pop(ctrl.serial))

    @staticmethod
    def already_running():
        try:
            pid = int(paths.PID_FILE.read_text())
            if pid != os.getpid() and "lindeck" in open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="ignore"):
                return pid
        except (OSError, ValueError):
            pass
        return None

    def _auto_import(self):
        try:
            autoimport.run_once(self.state)
        except Exception as e:  # noqa: BLE001
            log.debug("Auto-Import: %s", e)

    def run(self):
        """Dienst im Vordergrund (ohne Fenster) bis SIGTERM/SIGINT."""
        if self.already_running():
            log.info("LinDeck läuft bereits")
            autoimport.notify("LinDeck", "LinDeck läuft bereits")
            return
        signal.signal(signal.SIGHUP, lambda *a: threading.Thread(target=self.reload, daemon=True).start())
        signal.signal(signal.SIGTERM, lambda *a: self.shutdown())
        signal.signal(signal.SIGINT, lambda *a: self.shutdown())
        self.start()
        self.loop()
        self.cleanup()

    def start(self):
        """Pid-Datei, Auto-Import, Plugin-Host und Hintergrunddienste (ohne Signale, ohne Schleife)."""
        paths.PID_FILE.write_text(str(os.getpid()))
        self._auto_import()
        self.store.reload()
        try:
            from .plugins import PluginHost
            self.plugin_host = PluginHost(self)
        except Exception as e:  # noqa: BLE001
            log.warning("Plugin-Host nicht verfügbar: %s", e)
            self.plugin_host = None
        # Mixer und Wave-Mikrofon laufen im eigenen Programm „Linewave“ (wavehost.py)
        self.services = []
        log.info("LinDeck läuft – %d Profile geladen", len(self.store.profiles))

    def loop(self):
        last_scan = last_import = 0
        while self.running:
            now = time.time()
            if now - last_import > 15:
                last_import = now
                threading.Thread(target=self._auto_import, daemon=True).start()
            if now - last_scan > 2:
                last_scan = now
                self.scan()
                for c in self.controllers.values():
                    c.plugin_host = self.plugin_host
                if self.plugin_host:
                    self.plugin_host.sync()
                if self.store.changed_on_disk() or self.cfg.changed_on_disk():
                    self.reload()
                for svc in self.services:
                    try:
                        svc.tick()
                    except Exception as e:  # noqa: BLE001
                        log.debug("Dienst %s: %s", type(svc).__name__, e)
            try:
                self.smart_profiles()
            except Exception as e:  # noqa: BLE001
                log.debug("Smart Profiles: %s", e)
            time.sleep(0.5)

    def cleanup(self):
        for c in list(self.controllers.values()):
            c.stop()
        if getattr(self, "plugin_host", None):
            self.plugin_host.stop()
        for svc in getattr(self, "services", []):
            try:
                svc.stop()
            except Exception:  # noqa: BLE001
                pass
        from . import wavemic
        if "obj" in wavemic._instance:
            wavemic._instance["obj"].stop()  # Filterkette, falls „Linewave“ nicht lief
        self.vin.close()
        self.sound.stop()
        try:
            paths.PID_FILE.unlink()
        except OSError:
            pass

    def shutdown(self):
        self.running = False
