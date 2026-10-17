"""Hintergrunddienst: erkennt Geräte (inkl. Hotplug), lädt Profile neu, Smart Profiles."""
import logging
import os
import signal
import threading
import time

from . import autoimport, desktop, parts, paths, system
from .config import Config, State
from .engine import DeckController
from .i18n import _, ngettext
from .keyboard import VirtualInput
from .models import model_name
from .profiles import ProfileStore

log = logging.getLogger("freestream.daemon")


class Daemon:
    _virtual_lock = threading.RLock()  # Einstellungen-Fenster und Dienst-Takt legen das virtuelle Deck an

    def __init__(self, transport=None):
        paths.ensure_dirs()
        desktop.adopt_session_env()
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
            log.error(_("USB access failed: {error} (is libhidapi installed?)").format(error=e))
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
                log.error(_("Device {device} not usable: {error} (is the udev rule installed?)").format(
                    device=did, error=e))
                try:
                    deck.close()
                except Exception:  # noqa: BLE001
                    pass
        self.sync_virtual()
        for did in list(self.controllers):
            ctrl = self.controllers.get(did)
            if ctrl is None or ctrl.virtual:
                continue
            alive = did in seen
            try:
                alive = alive and ctrl.deck.connected()
            except Exception:  # noqa: BLE001
                alive = False
            if not alive:
                log.info(_("Device {serial} disconnected").format(serial=ctrl.serial))
                del self.controllers[did]
                if getattr(self, "plugin_host", None):
                    self.plugin_host.device_disconnected(ctrl)
                ctrl.detach()

    def sync_virtual(self):
        """Virtuelles Deck nach den Einstellungen anlegen, in der Größe ändern oder entfernen.

        Läuft im Dienst-Takt (scan) und sofort nach einer Änderung in den Einstellungen."""
        from . import virtualdeck
        with self._virtual_lock:
            s = virtualdeck.settings(self.cfg)
            did = "virtual:" + s["model"] if s["enabled"] else None
            for old in [k for k, c in list(self.controllers.items()) if c.virtual and k != did]:
                ctrl = self.controllers.pop(old)
                if getattr(self, "plugin_host", None):
                    self.plugin_host.device_disconnected(ctrl)
                ctrl.stop()
            if did is None or did in self.controllers:
                return did
            deck = virtualdeck.VirtualDeck(s["model"])
            deck.open()
            ctrl = DeckController(deck, self.store, self.cfg, self.state, self.vin, self.sound)
            ctrl.plugin_host = getattr(self, "plugin_host", None)
            if not self.store.for_model(ctrl.model, ctrl.geometry):
                self._virtual_profile(ctrl)
            ctrl.start()
            self.controllers[did] = ctrl
            if ctrl.plugin_host:
                ctrl.plugin_host.device_connected(ctrl)
            return did

    def _virtual_profile(self, ctrl):
        """Leeres Profil „Virtuelles Deck“ für eine Größe, zu der es noch keins gibt."""
        from .editdoc import create_profile
        try:
            create_profile(model_name(ctrl.model), ctrl.model, self.store.root)
            self.store.reload()
        except Exception as e:  # noqa: BLE001
            log.warning(_("Could not create a profile for the virtual deck: {error}").format(error=e))

    @property
    def virtual_controller(self):
        return next((c for c in list(self.controllers.values()) if c.virtual), None)

    def reload(self):
        log.info(_("Reloading profiles and configuration"))
        self.cfg.load()
        self.store.reload()
        for ctrl in list(self.controllers.values()):  # das virtuelle Deck kommt auch aus dem Fenster-Thread
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
        default = self.cfg.get("default_profile") or ""
        if not mapping and not auto:
            return
        win = system.active_window_class()
        if win == self._last_window:
            return
        self._last_window = win
        target = mapping.get(win) or auto.get(win) or next((v for k, v in auto.items() if k and k in win), None)
        for ctrl in list(self.controllers.values()):
            if target and ctrl.profile and ctrl.profile.id != target:
                p = self.store.get(target)
                if p and (p in ctrl.candidate_profiles()):
                    self._smart_prev.setdefault(ctrl.serial, ctrl.profile.id)
                    ctrl.load_profile(target)
            elif not target and ctrl.serial in self._smart_prev:
                back = self._smart_prev.pop(ctrl.serial)
                p = self.store.get(default) if default else None
                # Standardprofil (Einstellungen › Profile), sonst das Profil von vorher
                ctrl.load_profile(p.id if p and p in ctrl.candidate_profiles() else back)

    @staticmethod
    def already_running():
        try:
            pid = int(paths.PID_FILE.read_text())
            if pid != os.getpid() and any(n in open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="ignore")
                                            for n in ("freestream", "freedeck", "streamlin", "lindeck")):
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
            log.info(_("FreeDeck is already running"))
            desktop.notify("FreeDeck", _("FreeDeck is already running"))
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
            log.warning(_("Plugin host not available: {error}").format(error=e))
            self.plugin_host = None
        # Mixer und Wave-Mikrofon laufen im eigenen Programm „FreeWave“ (wavehost.py)
        self.services = []
        n = len(self.store.profiles)
        log.info(ngettext("FreeDeck is running – {count} profile loaded", "FreeDeck is running – {count} profiles loaded",
                          n).format(count=n))

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
                        log.debug(_("Service {name}: {error}").format(name=type(svc).__name__, error=e))
            try:
                self.smart_profiles()  # fragt das aktive Fenster ab (bei X11 zwei xprop-Aufrufe)
            except Exception as e:  # noqa: BLE001
                log.debug("Smart Profiles: %s", e)
            time.sleep(1.0)

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
        if parts.has_freewave():
            from . import wavemic
            if "obj" in wavemic._instance:
                wavemic._instance["obj"].stop()  # Filterkette, falls „FreeWave“ nicht lief
        self.vin.close()
        self.sound.stop()
        try:
            paths.PID_FILE.unlink()
        except OSError:
            pass

    def shutdown(self):
        self.running = False
