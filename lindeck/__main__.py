"""Kommandozeile: lindeck <befehl>"""
import argparse
import collections
import logging
import os
import signal
import sys
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


def cmd_run(args):
    from .daemon import Daemon
    Daemon(transport=args.transport).run()


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
        if A.is_builtin(uuid):
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
    print(f"Eingebaute Aktionen (unterstützt): {sum(builtin.values())}")
    for u, n in builtin.most_common():
        print(f"   {n:4d}  {u}")
    print(f"\nPlugin-Aktionen (benötigen Plugin-Umbau): {sum(plugins.values())}")
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

    s = sub.add_parser("run", help="Dienst starten (Standard)")
    s.add_argument("--transport", default=None, help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_run)

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

    s = sub.add_parser("reload", help="laufenden Dienst neu laden")
    s.set_defaults(fn=cmd_reload)

    args = ap.parse_args(argv)
    _setup_logging(args.verbose)
    if not getattr(args, "fn", None):
        args = ap.parse_args((argv or sys.argv[1:]) + ["run"])
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
