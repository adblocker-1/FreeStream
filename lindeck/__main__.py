"""Kommandozeile: lindeck <befehl>"""
import argparse
import collections
import logging
import os
import signal
import sys
import time
from pathlib import Path

from . import __version__, paths


def _setup_logging(verbose):
    paths.ensure_dirs()
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(paths.LOG_FILE, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S",
                        handlers=handlers)
    logging.getLogger("StreamDeck").setLevel(logging.WARNING)


def cmd_app(args):
    """LinDeck öffnen (Fenster) – oder das Fenster der laufenden Instanz nach vorne holen."""
    from .app import main as app_main
    return app_main(hidden=False, profile=getattr(args, "profile", None), transport=getattr(args, "transport", None))


def cmd_run(args):
    """Autostart: LinDeck unsichtbar starten (Fenster über das Menü). --no-gui: nur Dienst."""
    if args.no_gui:
        from .daemon import Daemon
        Daemon(transport=args.transport).run()
        return 0
    from .app import main as app_main
    return app_main(hidden=True, transport=args.transport)


def cmd_edit(args):
    return cmd_app(args)


def cmd_quit(args):
    from .app import quit_running
    if not quit_running():
        print("LinDeck läuft nicht")
        return 1
    print("LinDeck wird beendet")
    return 0


def cmd_import(args):
    from .profiles import import_path
    total = []
    for src in args.files:
        ids = import_path(src, replace=not args.keep)
        total += ids
        print(f"{src}: {len(ids)} Profil(e) importiert")
    _signal_daemon()
    if not total:
        print("Keine Profile gefunden. Erwartet: .streamDeckProfile, Backup-Datei oder ProfilesV2/V3-Ordner.")
        return 1
    return 0


def cmd_profiles(args):
    from .profiles import ProfileStore
    store = ProfileStore()
    if not store.profiles:
        print("Noch keine Profile importiert. 'lindeck import <datei>' verwenden.")
        return
    for p in sorted(store.profiles.values(), key=lambda p: (p.model_name, p.name.lower())):
        smart = f"  [Smart: {p.app_identifier}]" if p.app_identifier else ""
        print(f"{p.id}  {p.name:<30} {p.model_name:<26} {len(p.page_ids)} Seite(n){smart}")


def _decks(transport=None):
    from StreamDeck.DeviceManager import DeviceManager
    return DeviceManager(transport=transport).enumerate() if transport else DeviceManager().enumerate()


def cmd_devices(args):
    from .models import geometry_for_deck, model_for_deck, model_name
    try:
        decks = _decks(args.transport)
    except Exception as e:  # noqa: BLE001
        print(f"USB-Zugriff fehlgeschlagen: {e}")
        return 1
    if not decks:
        print("Kein Stream Deck gefunden. Angeschlossen? udev-Regel installiert (install.sh)?")
    for d in decks:
        try:
            d.open()
            serial = d.get_serial_number()
            fw = d.get_firmware_version()
            d.close()
        except Exception as e:  # noqa: BLE001
            serial, fw = f"(kein Zugriff: {e})", ""
        print(f"{model_name(model_for_deck(d)):<26} {geometry_for_deck(d)}  Seriennr. {serial}  FW {fw}")


def cmd_assign(args):
    from .config import Config
    from .profiles import ProfileStore
    p = ProfileStore().get(args.profile)
    if not p:
        print(f"Profil '{args.profile}' nicht gefunden")
        return 1
    cfg = Config()
    cfg.device(args.serial)["profile"] = p.id
    cfg.save()
    _signal_daemon()
    print(f"Gerät {args.serial} -> {p.name}")


def cmd_remove(args):
    from .profiles import ProfileStore, delete_profile
    p = ProfileStore().get(args.profile)
    if p and delete_profile(p.id):
        print(f"Profil '{p.name}' gelöscht")
        _signal_daemon()
        return 0
    print("Profil nicht gefunden")
    return 1


def _dummy_for(geometry):
    from .models import geometry_for_deck
    decks = _decks("dummy")
    for d in decks:
        if geometry_for_deck(d) == geometry or (geometry_for_deck(d).cols, geometry_for_deck(d).rows,
                                                 geometry_for_deck(d).dials) == (geometry.cols, geometry.rows,
                                                                                 geometry.dials):
            return d
    return None


def cmd_preview(args):
    from .config import Config, State
    from .engine import DeckController
    from .profiles import ProfileStore
    from .system import SoundPlayer

    class _NoInput:
        available = False

        def __getattr__(self, n):
            return lambda *a, **k: []

    logging.getLogger("StreamDeck").setLevel(logging.WARNING)
    store = ProfileStore()
    p = store.get(args.profile)
    if not p:
        print(f"Profil '{args.profile}' nicht gefunden")
        return 1
    deck = _dummy_for(p.geometry)
    if deck is None:
        print(f"Kein passendes Gerät für Geometrie {p.geometry}")
        return 1
    deck.open()
    cfg, state = Config(), State()
    state.save = lambda: None
    ctrl = DeckController(deck, store, cfg, state, _NoInput(), SoundPlayer())
    ctrl.serial = "preview"
    ctrl.load_profile(p.id, 0)
    out = Path(args.out or ".").expanduser()
    out.mkdir(parents=True, exist_ok=True)
    files = []
    pages = p.all_pages() if args.all else [p.page(pid) for pid in p.page_ids]
    for i, pg in enumerate([x for x in pages if x]):
        ctrl.folder_stack = [pg]
        ctrl.show_current()
        f = out / f"{_safe(p.name)}-{i + 1:02d}-{_safe(pg.name or pg.uuid[:8])}.png"
        ctrl.preview_image().save(f)
        files.append(f)
    for f in files:
        print(f)


def _safe(s):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)[:40]


def cmd_report(args):
    from . import actions as A
    from .config import Config
    from .profiles import ProfileStore
    from .system import is_windows_path, map_path, resolve_program
    cfg = Config()
    store = ProfileStore()
    profs = [store.get(args.profile)] if args.profile else list(store.profiles.values())
    if not profs or profs[0] is None:
        print("Profil nicht gefunden")
        return 1
    plugins = collections.Counter()
    builtin = collections.Counter()
    hints = []

    def walk(a, where):
        uuid = a.get("UUID", "")
        if A.is_supported(uuid):
            builtin[uuid] += 1
        else:
            plug = (a.get("Plugin") or {}).get("Name") or uuid.rsplit(".", 1)[0]
            plugins[f"{plug} ({uuid.rsplit('.', 1)[0]})"] += 1
        s = a.get("Settings") or {}
        if uuid.endswith(("system.open", "system.openapp", "system.close")):
            target = s.get("path") or s.get("exec") or s.get("bundle_path") or ""
            if target and is_windows_path(target):
                mapped = map_path(target, cfg.get("path_map"))
                if is_windows_path(mapped) or not os.path.exists(mapped):
                    prog = resolve_program(target, cfg.get("app_map"), s.get("app_name", ""))
                    if not prog:
                        hints.append(f"{where}: '{s.get('app_name') or target}' -> keine Linux-Zuordnung "
                                     f"(config.json app_map/path_map)")
        if uuid.endswith("soundboard.playaudio") and not (a.get("Resources") or {}).get("file"):
            pth = map_path(s.get("path", ""), cfg.get("path_map"))
            if not os.path.exists(pth):
                hints.append(f"{where}: Audiodatei fehlt: {s.get('path')}")
        for e in a.get("Actions") or []:
            if isinstance(e, dict) and e.get("UUID"):
                walk(e, where + " > " + (e.get("Name") or ""))
            elif isinstance(e, dict):
                for x in e.get("Actions") or []:
                    if isinstance(x, dict) and x.get("UUID"):
                        walk(x, where + " > " + (x.get("Name") or ""))

    for p in profs:
        for pg in p.all_pages():
            for pos, a in list(pg.keypad.items()) + [((i, "dial"), a) for i, a in pg.encoders.items()] + \
                    [((i, "info"), a) for i, a in pg.infobar.items()]:
                walk(a, f"{p.name} / {pg.name or pg.uuid[:8]} / {pos}")
    print(f"Unterstützte Aktionen (eingebaut, Linux-Umsetzung oder installiertes Plugin): {sum(builtin.values())}")
    for u, n in builtin.most_common():
        print(f"   {n:4d}  {u}")
    print(f"\nNicht unterstützte Plugin-Aktionen: {sum(plugins.values())}")
    for u, n in plugins.most_common():
        print(f"   {n:4d}  {u}")
    if hints:
        print("\nHinweise:")
        for h in hints:
            print("   " + h)


def cmd_assets(args):
    from .assets import extract
    dst = extract(args.source)
    print(f"Elgato-Icons übernommen nach {dst}")
    _signal_daemon()


def cmd_plugin(args):
    from . import plugins
    if args.action == "install":
        if not args.target:
            print("Datei angeben: lindeck plugin install <Datei.streamDeckPlugin>")
            return 1
        ids = []
        for t in args.target:
            ids += plugins.install(t)
        for uid in ids:
            info = plugins.installed()[uid]
            print(f"Installiert: {info.name} ({uid})" + ("" if info.runnable else f" – startet nicht: {info.reason}"))
        _signal_daemon()
        return 0 if ids else 1
    if args.action == "remove":
        ok = all(plugins.remove(t) for t in args.target)
        _signal_daemon()
        print("Entfernt" if ok else "Plugin nicht gefunden")
        return 0 if ok else 1
    found = plugins.installed()
    if not found:
        print("Keine Plugins installiert. 'lindeck plugin install <Datei.streamDeckPlugin>'")
    for info in found.values():
        state = f"{info.runtime}" if info.runnable else f"nicht startbar: {info.reason}"
        print(f"{info.uuid:<40} {info.name:<28} {info.version:<10} {len(info.actions)} Aktionen  [{state}]")
    return 0


def cmd_mixer(args):
    from .config import Config
    from .mixer import Mixer
    cfg = Config()
    mx = Mixer(cfg)
    try:
        if args.action == "setup":
            mx.setup()
            cfg.data.setdefault("mixer", {})["enabled"] = True
            cfg.save()
            print("Mixer eingerichtet und beim Start aktiviert. In OBS als Audioquelle "
                  "„Monitor of LinDeck Stream-Mix“ wählen.")
        elif args.action == "remove":
            mx.teardown()
            cfg.data.setdefault("mixer", {})["enabled"] = False
            cfg.save()
            print("Mixer entfernt")
        elif args.action == "set":
            ch, mix, vol = args.args
            mx.set_volume(ch, mix, int(vol))
        elif args.action == "mute":
            ch, mix = args.args
            mx.set_mute(ch, mix, "toggle")
        elif args.action == "assign":
            app, ch = args.args
            cfg.data.setdefault("mixer", {}).setdefault("routes", {})[app.lower()] = ch
            cfg.save()
            print(f"{app} → {ch} ({mx.assign(app, ch)} laufende Streams verschoben)")
        elif args.action == "output":
            print("Monitor-Ausgang:", mx.set_monitor_output(args.args[0] if args.args else "next"))
        else:
            if not mx.is_active():
                print("Mixer nicht eingerichtet – 'lindeck mixer setup'")
                return 1
            print(f"Monitor-Ausgang: {mx.monitor_output_name()}")
            for c in mx.all_channels():
                cells = []
                for m in ("monitor", "stream"):
                    vol, mute = mx.get(c["id"], m)
                    cells.append(f"{m}: " + ("–" if vol is None else ("stumm" if mute else f"{vol}%")))
                print(f"  {c['id']:<8} {c['name']:<12} " + "   ".join(cells))
    except (RuntimeError, ValueError) as e:
        print(f"Fehler: {e}")
        return 1
    return 0


def cmd_wave(args):
    from . import wavemic
    from .config import Config
    mic = wavemic.WaveMic(Config())
    on = (args.args[0] if args.args else "on").lower() in ("on", "an", "1", "true", "ein")
    try:
        if args.action in ("lowcut", "clipguard"):
            if mic.hw():
                mic.hw_set(args.action, on)
                print(f"{args.action}: {'an' if on else 'aus'} (direkt im Wave:3)")
                return 0
            kw = {args.action: on}
            if args.action == "lowcut" and len(args.args) > 1:
                kw["lowcut_freq"] = int(args.args[1])
            mic.settings.update(kw)
            mic._save()
            _signal_daemon()
            print(f"{args.action}: {'an' if on else 'aus'} (Software-Filter, wird vom LinDeck-Dienst angewendet)")
            if args.action == "clipguard" and on and not wavemic.limiter_available():
                print("Hinweis: Clipguard braucht das Paket swh-plugins")
        elif args.action == "leds":
            mic.hw_set("leds_off", not on)
        elif args.action == "flip":
            mic.hw_set("leds_flip", on)
        elif args.action == "headphone":
            mic.hw_set("headphone_volume", float(args.args[0]))
        elif args.action == "monitor":
            mic.hw_set("direct_monitor", float(args.args[0]))
        elif args.action == "gain":
            mic.set_gain(int(args.args[0]))
        elif args.action == "mute":
            mic.toggle_mute()
        else:
            st = mic.status()
            if not st.get("source") and not st.get("hardware"):
                print("Kein Elgato-Wave-Mikrofon gefunden")
                return 1
            print(f"Mikrofon:   {st.get('source') or '–'}")
            if st.get("hardware"):
                print(f"Wave:3 (Firmware-API {st['api']}) – Einstellungen direkt im Mikrofon")
                print(f"Gain:       {st['gain']:.1f} dB{' (stumm)' if st['mute'] else ''}")
                print(f"Low Cut:    {'an' if st['lowcut'] else 'aus'}")
                print(f"Clipguard:  {'an' if st['clipguard'] else 'aus'}")
                print(f"Kopfhörer:  {st['headphone_volume']:.1f} dB{' (stumm)' if st['headphone_mute'] else ''}")
                print(f"Mithören:   {st['direct_monitor']:.0f} % Mikrofon")
                print(f"LEDs:       {'aus' if st['leds_off'] else 'an'}"
                      + (f", umgekehrt: {'ja' if st.get('leds_flip') else 'nein'}" if 'leds_flip' in st else ""))
            else:
                if st.get("error"):
                    print(f"Wave:3-Steuerung nicht möglich: {st['error']}")
                vol, mute = mic.gain()
                s = mic.settings
                print(f"Gain:       {vol}%{' (stumm)' if mute else ''}")
                print(f"Low Cut:    {str(s.get('lowcut_freq')) + ' Hz (Software)' if s.get('lowcut') else 'aus'}")
                print(f"Clipguard:  {'an (Software)' if s.get('clipguard') else 'aus'}")
                print("LEDs:       nur beim Wave:3 steuerbar")
    except (RuntimeError, ValueError, IndexError) as e:
        print(f"Fehler: {e}")
        return 1
    return 0


def cmd_obs(args):
    from . import obs
    from .config import Config
    cl = obs.client(Config())
    if not obs.wait_connected(cl, 5):
        print(f"Keine Verbindung zu OBS: {cl.error or 'Zeitüberschreitung'}")
        print("In OBS: Werkzeuge → WebSocket-Server-Einstellungen → aktivieren; Passwort in config.json (obs.password)")
        return 1
    time.sleep(0.5)
    v = cl.request("GetVersion")
    print(f"Verbunden mit OBS {v.get('obsVersion')} (obs-websocket {v.get('obsWebSocketVersion')})")
    for k, val in cl.state.items():
        print(f"  {k:<11} {val}")
    cl.close()
    return 0


def cmd_discord(args):
    from . import discord
    from .config import Config
    try:
        cl = discord.client(Config())
        print("Verbinde mit Discord – bitte die Anfrage in Discord bestätigen …")
        cl.connect()
        print(f"Verbunden. Mikrofon {'stumm' if cl.voice.get('mute') else 'an'}, "
              f"Ton {'aus' if cl.voice.get('deaf') else 'an'}")
    except discord.DiscordError as e:
        print(f"Fehler: {e}")
        return 1
    return 0


def cmd_selftest(args):
    from . import selftest
    from .daemon import Daemon
    pid = Daemon.already_running()
    if pid:
        if not args.stop_daemon:
            print("Der LinDeck-Dienst läuft und belegt das Gerät. Mit --stop-daemon wird er für den Test "
                  "angehalten und danach neu gestartet.")
            return 1
        os.kill(pid, signal.SIGTERM)
        time.sleep(2)
    try:
        files = selftest.run(args.transport, args.timeout)
    finally:
        if pid:
            from .system import run_bg
            run_bg(["lindeck", "run"])
    return 0 if files else 1


def cmd_reload(args):
    if not _signal_daemon():
        print("LinDeck läuft nicht")
        return 1
    print("Neu geladen")


def _signal_daemon():
    try:
        pid = int(paths.PID_FILE.read_text())
        os.kill(pid, signal.SIGHUP)
        return True
    except (OSError, ValueError):
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lindeck", description="Stream-Deck-Software für Linux (Elgato-kompatibel)")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("open", help="LinDeck-Fenster öffnen (Standard ohne Befehl)")
    s.add_argument("profile", nargs="?", help=argparse.SUPPRESS)
    s.add_argument("--transport", default=None, help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_app)

    s = sub.add_parser("run", help="LinDeck unsichtbar starten (Autostart)")
    s.add_argument("--no-gui", action="store_true", help="nur den Dienst ohne Fenster starten (z. B. per SSH)")
    s.add_argument("--transport", default=None, help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("edit", help="LinDeck-Fenster öffnen, optional mit einem bestimmten Profil")
    s.add_argument("profile", nargs="?", help="Profil (ID oder Name), das geöffnet werden soll")
    s.set_defaults(fn=cmd_edit)

    s = sub.add_parser("quit", help="laufendes LinDeck beenden")
    s.set_defaults(fn=cmd_quit)

    s = sub.add_parser("import", help="Elgato-Profile importieren (.streamDeckProfile, Backup, ProfilesV2/V3-Ordner)")
    s.add_argument("files", nargs="+")
    s.add_argument("--keep", action="store_true", help="vorhandene Profile nicht überschreiben")
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("profiles", help="importierte Profile anzeigen")
    s.set_defaults(fn=cmd_profiles)

    s = sub.add_parser("devices", help="angeschlossene Stream Decks anzeigen")
    s.add_argument("--transport", default=None, help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_devices)

    s = sub.add_parser("assign", help="Profil fest einem Gerät zuordnen")
    s.add_argument("serial")
    s.add_argument("profile")
    s.set_defaults(fn=cmd_assign)

    s = sub.add_parser("remove", help="Profil löschen")
    s.add_argument("profile")
    s.set_defaults(fn=cmd_remove)

    s = sub.add_parser("preview", help="Profilseiten als PNG rendern (ohne Gerät)")
    s.add_argument("profile")
    s.add_argument("-o", "--out", default=".")
    s.add_argument("--all", action="store_true", help="auch Ordner rendern")
    s.set_defaults(fn=cmd_preview)

    s = sub.add_parser("report", help="Kompatibilitätsbericht für Profile")
    s.add_argument("profile", nargs="?")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("extract-assets", help="Standard-Icons aus dem Elgato-Installer übernehmen")
    s.add_argument("source", help="Stream_Deck_*.msi oder kopierter Programmordner")
    s.set_defaults(fn=cmd_assets)

    s = sub.add_parser("plugin", help="Elgato-Plugins verwalten (list, install, remove)")
    s.add_argument("action", choices=["list", "install", "remove"], nargs="?", default="list")
    s.add_argument("target", nargs="*", help="Datei (.streamDeckPlugin) bzw. Plugin-UUID")
    s.set_defaults(fn=cmd_plugin)

    s = sub.add_parser("mixer", help="Audio-Mixer (Wave-Link-Ersatz): status, setup, remove, set, mute, "
                                     "assign, output")
    s.add_argument("action", nargs="?", default="status",
                   choices=["status", "setup", "remove", "set", "mute", "assign", "output"])
    s.add_argument("args", nargs="*", help="set <Kanal> <monitor|stream|both> <Prozent> · mute <Kanal> <Mix> · "
                                           "assign <Programm> <Kanal> · output [Gerät]")
    s.set_defaults(fn=cmd_mixer)

    s = sub.add_parser("wave", help="Elgato-Wave-Mikrofon: status, gain, mute, lowcut, clipguard, leds, flip, "
                                    "headphone, monitor")
    s.add_argument("action", nargs="?", default="status",
                   choices=["status", "gain", "mute", "lowcut", "clipguard", "leds", "flip", "headphone", "monitor"])
    s.add_argument("args", nargs="*", help="gain <Prozent> · lowcut on|off [80|120] · clipguard on|off · "
                                           "leds on|off · flip on|off · headphone <dB −60…0> · monitor <0…100>")
    s.set_defaults(fn=cmd_wave)

    s = sub.add_parser("obs", help="Verbindung zu OBS testen")
    s.set_defaults(fn=cmd_obs)

    s = sub.add_parser("discord", help="mit Discord verbinden (einmalige Freigabe)")
    s.set_defaults(fn=cmd_discord)

    s = sub.add_parser("selftest", help="angeschlossene Stream Decks Schritt für Schritt testen")
    s.add_argument("--timeout", type=int, default=90, help="Sekunden je Schritt")
    s.add_argument("--stop-daemon", action="store_true", help="laufenden Dienst für den Test anhalten")
    s.add_argument("--transport", default=None, help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_selftest)

    s = sub.add_parser("reload", help="laufenden Dienst neu laden")
    s.set_defaults(fn=cmd_reload)

    args = ap.parse_args(argv)
    _setup_logging(args.verbose)
    if not getattr(args, "fn", None):
        args = ap.parse_args((argv or sys.argv[1:]) + ["open"])
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
