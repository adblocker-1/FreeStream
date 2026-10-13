"""LinDeck als eine App – wie die Elgato-Stream-Deck-Software.

Ein Prozess für das Stream Deck: Gerätesteuerung, Plugins und das Fenster zum
Bearbeiten der Profile. Mixer und Wave-Mikrofon sind – wie Elgato Wave Link –
ein eigenes Programm: „Linewave“ (wavehost.py).

  * Beim Anmelden startet LinDeck unsichtbar (`lindeck --hidden`).
  * Der Menüeintrag „LinDeck“ öffnet das Fenster. Läuft LinDeck schon, holt
    ein zweiter Start nur das Fenster nach vorne (Signal SIGUSR1).
  * Fenster schließen = ausblenden; das Stream Deck läuft weiter.
    Beenden über „Beenden“ im Fenster, das Symbol in der Taskleiste oder
    `lindeck quit`.
  * Symbol in der Taskleiste über pystray, falls installiert
    (python3-pystray, gir1.2-ayatanaappindicator3-0.1).
  * Ohne grafische Oberfläche (z. B. Server, SSH) läuft nur der Dienst.
"""
import json
import logging
import os
import signal
import threading
import time

from . import paths

log = logging.getLogger("lindeck.app")
APP_FILE = "app.json"


def _app_info(name=APP_FILE):
    try:
        return json.loads((paths.STATE_DIR / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_app_info(gui, name=APP_FILE):
    paths.ensure_dirs()
    (paths.STATE_DIR / name).write_text(json.dumps({"pid": os.getpid(), "gui": gui}), encoding="utf-8")


def _clear_app_info(name=APP_FILE):
    try:
        if _app_info(name).get("pid") == os.getpid():
            (paths.STATE_DIR / name).unlink()
    except OSError:
        pass


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def running_instance(name=APP_FILE):
    """Pid einer laufenden Instanz (LinDeck: Dienst oder App; sonst nach Statusdatei) oder None."""
    if name == APP_FILE:
        from .daemon import Daemon
        pid = Daemon.already_running()
        if pid:
            return pid
    pid = _app_info(name).get("pid")
    if pid and pid != os.getpid() and _alive(pid):
        return pid
    return None


def make_icon(size=256):
    """App-Symbol: dunkles Stream Deck mit bunten Tasten."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = size // 8
    d.rounded_rectangle((size * 0.04, size * 0.16, size * 0.96, size * 0.84), radius=r, fill=(28, 29, 33, 255))
    colors = [(10, 132, 255), (48, 209, 88), (255, 159, 10), (191, 90, 242), (255, 69, 58), (100, 210, 255)]
    kw = size * 0.22
    gap = size * 0.06
    x0 = (size - (3 * kw + 2 * gap)) / 2
    y0 = (size - (2 * kw + gap)) / 2
    for i, col in enumerate(colors):
        cx, cy = x0 + (i % 3) * (kw + gap), y0 + (i // 3) * (kw + gap)
        d.rounded_rectangle((cx, cy, cx + kw, cy + kw), radius=kw * 0.2, fill=col + (255,))
    return img


class Tray:
    """Symbol in der Taskleiste (optional, über pystray)."""

    def __init__(self, on_show, on_quit, icon=None, title="LinDeck (Stream Deck)", ident="lindeck",
                 open_label="LinDeck öffnen"):
        self.icon = None
        try:
            import pystray
        except Exception as e:  # noqa: BLE001 - pystray fehlt oder kein Backend
            log.info("Kein Taskleisten-Symbol (pystray nicht verfügbar: %s)", e)
            return
        menu = pystray.Menu(pystray.MenuItem(open_label, lambda *a: on_show(), default=True),
                            pystray.MenuItem("Beenden", lambda *a: on_quit()))
        try:
            self.icon = pystray.Icon(ident, icon or make_icon(64), title, menu)
            threading.Thread(target=self.icon.run, name="tray", daemon=True).start()
        except Exception as e:  # noqa: BLE001
            log.info("Taskleisten-Symbol nicht möglich: %s", e)
            self.icon = None

    def stop(self):
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:  # noqa: BLE001
                pass


class App:
    """Dienst + Fenster in einem Prozess. Tk läuft im Hauptthread, der Dienst in einem eigenen Thread."""

    def __init__(self, root, daemon, profile=None, hidden=False, tray=True):
        from .editor import EditorApp
        self.root, self.daemon = root, daemon
        self._want_show = threading.Event()
        self._want_quit = threading.Event()
        self.quitting = False
        self.editor = EditorApp(root, profile=profile, daemon=daemon, on_quit=self.request_quit)
        try:
            from PIL import ImageTk
            self._icon = ImageTk.PhotoImage(make_icon(128), master=root)
            root.iconphoto(True, self._icon)
        except Exception:  # noqa: BLE001
            pass
        self.tray = Tray(self.request_show, self.request_quit) if tray else None
        if hidden:
            root.withdraw()
        self._poll()

    # Aufrufe aus anderen Threads/Signalen setzen nur Flags; ausgeführt wird im Tk-Thread
    def request_show(self):
        self._want_show.set()

    def request_quit(self):
        self._want_quit.set()

    def _poll(self):
        if self._want_quit.is_set():
            self._quit()
            return
        if self._want_show.is_set():
            self._want_show.clear()
            self.editor.show()
        self.root.after(250, self._poll)

    def _quit(self):
        if self.quitting:
            return
        self.quitting = True
        try:
            self.editor.close_doc()
        except Exception:  # noqa: BLE001
            pass
        if self.tray:
            self.tray.stop()
        self.root.quit()


def main(hidden=False, profile=None, transport=None, tray=True):
    """Startet LinDeck oder holt das Fenster einer laufenden Instanz nach vorne."""
    from .autoimport import adopt_session_env
    from .daemon import Daemon
    adopt_session_env()
    pid = running_instance()
    if pid:
        info = _app_info()
        if hidden:
            return 0  # läuft schon (z. B. Autostart doppelt)
        if info.get("pid") == pid and info.get("gui"):
            os.kill(pid, signal.SIGUSR1)
            return 0
        # läuft nur als Dienst ohne Fenster -> ablösen
        log.info("LinDeck läuft ohne Fenster – starte neu mit Fenster")
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            if not _alive(pid):
                break
            time.sleep(0.1)

    root = None
    try:
        import tkinter as tk
        root = tk.Tk(className="lindeck")
    except Exception as e:  # noqa: BLE001 - kein Display oder kein Tk
        log.info("Keine grafische Oberfläche (%s) – LinDeck läuft als Dienst", e)

    daemon = Daemon(transport=transport)
    if root is None:
        _write_app_info(False)
        signal.signal(signal.SIGUSR1, lambda *a: None)
        daemon.run()
        return 0

    _write_app_info(True)
    daemon.start()
    loop = threading.Thread(target=daemon.loop, name="lindeck-dienst", daemon=True)
    loop.start()
    app = App(root, daemon, profile=profile, hidden=hidden, tray=tray)
    signal.signal(signal.SIGUSR1, lambda *a: app.request_show())
    signal.signal(signal.SIGTERM, lambda *a: app.request_quit())
    signal.signal(signal.SIGINT, lambda *a: app.request_quit())
    signal.signal(signal.SIGHUP, lambda *a: threading.Thread(target=daemon.reload, daemon=True).start())
    try:
        root.mainloop()
    finally:
        daemon.shutdown()
        loop.join(timeout=5)
        daemon.cleanup()
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass
        _clear_app_info()
    return 0


def quit_running():
    pid = running_instance()
    if not pid:
        return False
    os.kill(pid, signal.SIGTERM)
    return True
