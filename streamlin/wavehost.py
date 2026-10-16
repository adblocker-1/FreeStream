"""Linewave – eigenes Programm für Audio-Mixer und Wave-Mikrofon.

Wie bei Elgato gibt es zwei Programme:

  * LinDeck       – Stream Deck (Tasten, Profile, Plugins)        `lindeck`
  * Linewave  – Wave-Link-Ersatz: Mixer und Mikrofon           `linewave`

Linewave startet beim Anmelden unsichtbar (`linewave --hidden`),
richtet den PipeWire-Mixer ein, verteilt neue Programme auf die Kanäle und
betreibt die Software-Filterkette (Low Cut/Clipguard) für Wave-Mikrofone ohne
Hardware-Filter. Ein zweiter Start holt nur das Fenster nach vorne.

Tasten auf dem Stream Deck (Mixer, Wave-Mikrofon) funktionieren auch ohne
Linewave – sie stellen PipeWire bzw. das Mikrofon direkt. Ändert die
Stream-Deck-App Low Cut/Clipguard, lädt Linewave die Einstellungen neu
(Signal SIGHUP).
"""
import logging
import os
import signal
import threading
import time

from . import desktop, wavemic
from .i18n import _
from .mixer import Mixer

log = logging.getLogger("streamlin.wave")
WAVE_APP_FILE = wavemic.WAVE_APP_FILE


def make_icon(size=256):
    """Symbol: Mikrofon mit Pegelbalken."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 256
    d.rounded_rectangle((8 * s, 8 * s, 248 * s, 248 * s), radius=48 * s, fill=(28, 29, 33, 255))
    d.rounded_rectangle((88 * s, 36 * s, 168 * s, 156 * s), radius=40 * s, fill=(230, 232, 236, 255))
    d.arc((64 * s, 92 * s, 192 * s, 196 * s), 0, 180, fill=(230, 232, 236, 255), width=max(1, int(12 * s)))
    d.rectangle((122 * s, 196 * s, 134 * s, 222 * s), fill=(230, 232, 236, 255))
    for i, (h, col) in enumerate(((36, (48, 209, 88)), (64, (48, 209, 88)), (92, (255, 159, 10)))):
        x = (30 + i * 10) * s
        d.rectangle((x, (150 - h) * s, x + 6 * s, 150 * s), fill=col + (255,))
        x = (220 - i * 10) * s
        d.rectangle((x, (150 - h) * s, x + 6 * s, 150 * s), fill=col + (255,))
    return img


class WaveHost:
    """Hintergrundteil von Linewave: Mixer einrichten, Streams verteilen, Mikrofon-Filter."""

    def __init__(self, cfg=None):
        if cfg is None:
            from .config import Config
            cfg = Config()
        self.cfg = cfg
        wavemic.OWNER = True
        self.mic = wavemic.instance(cfg)
        self.mic.cfg = cfg
        self.mixer = Mixer(cfg)
        self.mic_service = wavemic.WaveMicService(self)
        self.running = True
        self.error = ""
        self._reload = threading.Event()

    @property
    def mixer_enabled(self):
        return bool((self.cfg.get("mixer") or {}).get("enabled"))

    def start(self):
        if self.mixer_enabled:
            try:
                if not self.mixer.is_active():
                    self.mixer.setup()
            except Exception as e:  # noqa: BLE001
                self.error = _("Mixer: {error}").format(error=e)
                log.warning(_("Mixer not set up: {error}").format(error=e))
        try:
            self.mic_service.start()
        except Exception as e:  # noqa: BLE001
            log.warning(_("Wave microphone: {error}").format(error=e))

    def request_reload(self):
        self._reload.set()

    def tick(self):
        if self._reload.is_set():
            self._reload.clear()
            self.cfg.load()
            try:
                self.mic.reload()
            except Exception as e:  # noqa: BLE001
                log.warning(_("Wave microphone: {error}").format(error=e))
        elif self.cfg.changed_on_disk():
            self.cfg.load()
        if self.mixer_enabled and self.mixer.conf.get("auto_route", True):
            try:
                self.mixer.route_streams()
            except Exception as e:  # noqa: BLE001
                log.debug("Routing: %s", e)
        try:
            self.mic_service.tick()
        except Exception as e:  # noqa: BLE001
            log.debug(_("Wave microphone: {error}").format(error=e))
        self._watch_effects()

    def _watch_effects(self):
        """Abgestürzte Effektketten neu starten (alle 10 s)."""
        now = time.time()
        if now - getattr(self, "_fx_check", 0) < 10 or not self.mixer_enabled:
            return
        self._fx_check = now
        try:
            if not self.mixer.is_active():
                return
            for c in self.mixer.all_channels():
                if self.mixer.fx.active(c["id"]) and not self.mixer.fx.running(c["id"]):
                    log.info(_("Restarting effects for {name}").format(name=c["name"]))
                    self.mixer.rebuild_channel(c["id"])
        except Exception as e:  # noqa: BLE001
            log.debug(_("Effects: {error}").format(error=e))

    def loop(self):
        while self.running:
            self.tick()
            for _i in range(10):
                if not self.running or self._reload.is_set():
                    break
                time.sleep(0.1)

    def shutdown(self):
        self.running = False

    def cleanup(self):
        try:
            self.mic_service.stop()
        except Exception:  # noqa: BLE001
            pass

    # -------------------------------------------------- Mixer ein/aus
    def enable_mixer(self, on=True):
        self.cfg.data.setdefault("mixer", {})["enabled"] = bool(on)
        self.cfg.save()
        if on:
            self.mixer.setup()
        else:
            self.mixer.teardown()
        self.error = ""


def main(hidden=False, tray=True, cfg=None):
    """Startet Linewave oder holt das Fenster der laufenden Instanz nach vorne."""
    from .desktop import adopt_session_env
    adopt_session_env()
    pid = desktop.running_instance(WAVE_APP_FILE)
    if pid:
        if hidden:
            return 0
        if desktop._app_info(WAVE_APP_FILE).get("gui"):
            os.kill(pid, signal.SIGUSR1)
            return 0
        log.info(_("Linewave is running without a window – restarting with a window"))
        os.kill(pid, signal.SIGTERM)
        for _i in range(50):
            if not desktop._alive(pid):
                break
            time.sleep(0.1)

    root = None
    try:
        import tkinter as tk
        root = tk.Tk(className="linewave")
    except Exception as e:  # noqa: BLE001
        log.info(_("No graphical interface ({error}) – Linewave is running as a service").format(error=e))

    host = WaveHost(cfg)
    desktop._write_app_info(root is not None, WAVE_APP_FILE)
    signal.signal(signal.SIGHUP, lambda *a: host.request_reload())
    try:
        host.start()
        if root is None:
            signal.signal(signal.SIGUSR1, lambda *a: None)
            signal.signal(signal.SIGTERM, lambda *a: host.shutdown())
            signal.signal(signal.SIGINT, lambda *a: host.shutdown())
            host.loop()
            return 0
        loop = threading.Thread(target=host.loop, name="linewave", daemon=True)
        loop.start()
        from .waveui import WaveWindow
        win = WaveWindow(root, host, hidden=hidden, tray=tray)
        from . import updateui
        root.after(3000, lambda: updateui.start(root, "Linewave", win._restart))
        signal.signal(signal.SIGUSR1, lambda *a: win.request_show())
        signal.signal(signal.SIGTERM, lambda *a: win.request_quit())
        signal.signal(signal.SIGINT, lambda *a: win.request_quit())
        try:
            root.mainloop()
        finally:
            host.shutdown()
            loop.join(timeout=5)
            try:
                root.destroy()
            except Exception:  # noqa: BLE001
                pass
    finally:
        host.cleanup()
        desktop._clear_app_info(WAVE_APP_FILE)
    return 0


def quit_running():
    pid = desktop.running_instance(WAVE_APP_FILE)
    if not pid:
        return False
    os.kill(pid, signal.SIGTERM)
    return True
