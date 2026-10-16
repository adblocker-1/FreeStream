"""Kommandozeile: lindeck <befehl>"""
import argparse
import collections
import logging
import os
import signal
import sys
import time
from pathlib import Path

from . import __version__, parts, paths
from .i18n import _, ngettext, pgettext


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
        print(_("LinDeck is not running"))
        return 1
    print(_("Quitting LinDeck"))
    return 0


def cmd_wave_app(args):
    """Linewave öffnen (Mixer + Mikrofon) – eigenes Programm wie Elgato Wave Link."""
    from . import wavehost
    if args.quit:
        if not wavehost.quit_running():
            print(_("Linewave is not running"))
            return 1
        print(_("Quitting Linewave"))
        return 0
    return wavehost.main(hidden=args.hidden, tray=not args.no_tray)


def cmd_import(args):
    from .profiles import import_path
    total = []
    for src in args.files:
        ids = import_path(src, replace=not args.keep)
        total += ids
        print(ngettext("{src}: {n} profile imported", "{src}: {n} profiles imported", len(ids)).format(
            src=src, n=len(ids)))
    _signal_daemon()
    if not total:
        print(_("No profiles found. Expected: .streamDeckProfile, backup file or ProfilesV2/V3 folder."))
        return 1
    return 0


def cmd_profiles(args):
    from .profiles import ProfileStore
    store = ProfileStore()
    if not store.profiles:
        print(_("No profiles imported yet. Use 'lindeck import <file>'."))
        return
    for p in sorted(store.profiles.values(), key=lambda p: (p.model_name, p.name.lower())):
        smart = f"  [Smart: {p.app_identifier}]" if p.app_identifier else ""
        pages = ngettext("{n} page", "{n} pages", len(p.page_ids)).format(n=len(p.page_ids))
        print(f"{p.id}  {p.name:<30} {p.model_name:<26} {pages}{smart}")


def _decks(transport=None):
    from StreamDeck.DeviceManager import DeviceManager
    return DeviceManager(transport=transport).enumerate() if transport else DeviceManager().enumerate()


def cmd_devices(args):
    from .models import geometry_for_deck, model_for_deck, model_name
    try:
        decks = _decks(args.transport)
    except Exception as e:  # noqa: BLE001
        print(_("USB access failed: {error}").format(error=e))
        return 1
    if not decks:
        print(_("No Stream Deck found. Is it connected? Is the udev rule installed (install.sh)?"))
    for d in decks:
        try:
            d.open()
            serial = d.get_serial_number()
            fw = d.get_firmware_version()
            d.close()
        except Exception as e:  # noqa: BLE001
            serial, fw = _("(no access: {error})").format(error=e), ""
        print(f"{model_name(model_for_deck(d)):<26} {geometry_for_deck(d)}  "
              + _("Serial {serial}  FW {firmware}").format(serial=serial, firmware=fw))


def cmd_assign(args):
    from .config import Config
    from .profiles import ProfileStore
    p = ProfileStore().get(args.profile)
    if not p:
        print(_("Profile '{profile}' not found").format(profile=args.profile))
        return 1
    cfg = Config()
    cfg.device(args.serial)["profile"] = p.id
    cfg.save()
    _signal_daemon()
    print(_("Device {serial} -> {profile}").format(serial=args.serial, profile=p.name))


def cmd_remove(args):
    from .profiles import ProfileStore, delete_profile
    p = ProfileStore().get(args.profile)
    if p and delete_profile(p.id):
        print(_("Profile '{profile}' deleted").format(profile=p.name))
        _signal_daemon()
        return 0
    print(_("Profile not found"))
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
        print(_("Profile '{profile}' not found").format(profile=args.profile))
        return 1
    deck = _dummy_for(p.geometry)
    if deck is None:
        print(_("No matching device for geometry {geometry}").format(geometry=p.geometry))
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
        print(_("Profile not found"))
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
                        hints.append(_("{where}: '{app}' -> no Linux mapping (config.json app_map/path_map)")
                                     .format(where=where, app=s.get('app_name') or target))
        if uuid.endswith("soundboard.playaudio") and not (a.get("Resources") or {}).get("file"):
            pth = map_path(s.get("path", ""), cfg.get("path_map"))
            if not os.path.exists(pth):
                hints.append(_("{where}: audio file missing: {path}").format(where=where, path=s.get('path')))
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
    print(_("Supported actions (built-in, Linux implementation or installed plugin): {count}").format(
        count=sum(builtin.values())))
    for u, n in builtin.most_common():
        print(f"   {n:4d}  {u}")
    print("\n" + _("Unsupported plugin actions: {count}").format(count=sum(plugins.values())))
    for u, n in plugins.most_common():
        print(f"   {n:4d}  {u}")
    if hints:
        print("\n" + _("Notes:"))
        for h in hints:
            print("   " + h)


def cmd_assets(args):
    from .assets import extract
    dst = extract(args.source)
    print(_("Elgato icons copied to {path}").format(path=dst))
    _signal_daemon()


def cmd_plugin(args):
    from . import plugins
    if args.action == "install":
        if not args.target:
            print(_("Specify a file: lindeck plugin install <file.streamDeckPlugin>"))
            return 1
        ids = []
        for t in args.target:
            ids += plugins.install(t)
        for uid in ids:
            info = plugins.installed()[uid]
            if info.runnable:
                print(_("Installed: {name} ({uuid})").format(name=info.name, uuid=uid))
            else:
                print(_("Installed: {name} ({uuid}) – does not start: {reason}").format(
                    name=info.name, uuid=uid, reason=info.reason))
        _signal_daemon()
        return 0 if ids else 1
    if args.action == "remove":
        ok = all(plugins.remove(t) for t in args.target)
        _signal_daemon()
        print(_("Removed") if ok else _("Plugin not found"))
        return 0 if ok else 1
    found = plugins.installed()
    if not found:
        print(_("No plugins installed. 'lindeck plugin install <file.streamDeckPlugin>'"))
    for info in found.values():
        state = f"{info.runtime}" if info.runnable else _("cannot start: {reason}").format(reason=info.reason)
        n_actions = ngettext("{n} action", "{n} actions", len(info.actions)).format(n=len(info.actions))
        print(f"{info.uuid:<40} {info.name:<28} {info.version:<10} {n_actions}  [{state}]")
    return 0


def _signal_wave_app():
    """Linewave die geänderte Konfiguration neu laden lassen (falls es läuft)."""
    from . import desktop, wavemic
    pid = desktop.running_instance(wavemic.WAVE_APP_FILE)
    if pid:
        os.kill(pid, signal.SIGHUP)
    return bool(pid)


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
            _signal_wave_app()
            print(_("Mixer set up and enabled at startup. In OBS, choose "
                    "“Monitor of Linewave Stream Mix” as the audio source."))
        elif args.action == "remove":
            mx.teardown()
            cfg.data.setdefault("mixer", {})["enabled"] = False
            cfg.save()
            print(_("Mixer removed"))
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
            print(_("{app} → {channel} ({count} running streams moved)").format(
                app=app, channel=ch, count=mx.assign(app, ch)))
        elif args.action == "output":
            print(_("Monitor output: {output}").format(
                output=mx.set_monitor_output(args.args[0] if args.args else "next")))
        elif args.action == "add":
            if not args.args:
                print(_("Specify a name: linewave mixer add <name> [input device]"))
                return 1
            name, dev = args.args[0], (args.args[1] if len(args.args) > 1 else "")
            if dev:
                src = next((n for n, d in mx.inputs() if dev == n or dev.lower() in d.lower()), None)
                if not src:
                    print(_("Input device “{device}” not found. Available: {available}").format(
                        device=dev, available=", ".join(d for _n, d in mx.inputs())))
                    return 1
                c = mx.add_channel(name, "input", source=src)
            else:
                c = mx.add_channel(name)
            _signal_wave_app()
            print(_("Channel created: {name} ({id})").format(name=c['name'], id=c['id']))
        elif args.action == "delete":
            mx.remove_channel(args.args[0])
            _signal_wave_app()
            print(_("Channel deleted"))
        else:
            if not mx.is_active():
                print(_("Mixer not set up – 'linewave mixer setup'"))
                return 1
            print(_("Monitor output: {output}").format(output=mx.monitor_output_name()))
            for c in mx.all_channels():
                cells = []
                for m in ("monitor", "stream"):
                    vol, mute = mx.get(c["id"], m)
                    shown = "–" if vol is None else (pgettext("state", "muted") if mute else f"{vol}%")
                    cells.append(f"{m}: {shown}")
                print(f"  {c['id']:<8} {c['name']:<12} " + "   ".join(cells))
    except (RuntimeError, ValueError) as e:
        print(_("Error: {error}").format(error=e))
        return 1
    return 0


def _on_off(on):
    return pgettext("state", "on") if on else pgettext("state", "off")


def _muted_suffix(mute):
    return pgettext("state", " (muted)") if mute else ""


ON_WORDS = ("on", "an", "1", "true", "ein")


def cmd_noise(args):
    """`linewave mic noise on|off|status|strength <0…95>|backend auto|rnnoise|webrtc`"""
    from . import noise
    from .config import Config
    from .mixer import Mixer
    ns = Mixer(Config()).noise
    sub = (args.args[0] if args.args else "status").lower()
    try:
        if sub in ON_WORDS + ("off", "aus", "0", "false"):
            on = sub in ON_WORDS
            if on and not noise.available():
                print(noise.install_hint())
                return 1
            ns.set(enabled=on)
            ns.apply()
        elif sub == "strength":
            ns.set_strength(int(args.args[1]))
        elif sub == "backend":
            ns.set(backend=args.args[1].lower())
            if ns.enabled:
                ns.apply()
        elif sub != "status":
            print(_("Unknown command: {name}").format(name=sub))
            return 1
        st = ns.status()
        if not st["enabled"]:
            state = _on_off(False)
        elif st["running"]:
            state = _("on ({backend}, active)").format(backend=noise.backend_name(st["backend"]))
        elif not st["available"]:
            state = _("on – not available")
        else:
            state = _("on – starts as soon as the mixer is set up (linewave mixer setup)")
        print(_("Noise suppression: {state}").format(state=state))
        if st["available"]:
            print(_("Available: {names}").format(names=", ".join(noise.backend_name(b) for b in st["available"])))
            if (st["backend"] or "") == "rnnoise":
                print(_("Strength: {value} % (VAD threshold)").format(value=st["strength"]))
        else:
            print(st["hint"])
    except (RuntimeError, ValueError, IndexError) as e:
        print(_("Error: {error}").format(error=e))
        return 1
    return 0


def cmd_wave(args):
    from . import wavemic
    from .config import Config
    if args.action == "noise":
        return cmd_noise(args)
    mic = wavemic.WaveMic(Config())
    on = (args.args[0] if args.args else "on").lower() in ON_WORDS
    try:
        if args.action in ("lowcut", "clipguard"):
            if mic.hw():
                mic.hw_set(args.action, on)
                print(_("{setting}: {state} (directly on the Wave:3)").format(setting=args.action, state=_on_off(on)))
                return 0
            kw = {args.action: on}
            if args.action == "lowcut" and len(args.args) > 1:
                kw["lowcut_freq"] = int(args.args[1])
            mic.settings.update(kw)
            mic._save()
            if mic._delegate():
                print(_("{setting}: {state} (software filter in Linewave)").format(
                    setting=args.action, state=_on_off(on)))
            else:
                print(_("{setting}: {state} – saved; the software filter runs as soon as Linewave "
                        "is started (linewave)").format(setting=args.action, state=_on_off(on)))
            if args.action == "clipguard" and on and not wavemic.limiter_available():
                print(_("Note: Clipguard needs the swh-plugins package"))
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
                print(_("No Elgato Wave microphone found"))
                return 1
            print(_("Microphone: {source}").format(source=st.get('source') or '–'))
            if st.get("hardware"):
                print(_("Wave:3 (firmware API {api}) – settings stored directly in the microphone").format(
                    api=st['api']))
                print(f"Gain:       {st['gain']:.1f} dB{_muted_suffix(st['mute'])}")
                print(_("Low cut:    {state}").format(state=_on_off(st['lowcut'])))
                print(f"Clipguard:  {_on_off(st['clipguard'])}")
                print(_("Headphones: {volume} dB{muted}").format(volume=f"{st['headphone_volume']:.1f}",
                                                                 muted=_muted_suffix(st['headphone_mute'])))
                print(_("Monitoring: {percent} % microphone").format(percent=f"{st['direct_monitor']:.0f}"))
                flip = ""
                if 'leds_flip' in st:
                    flip = _(", flipped: {answer}").format(
                        answer=pgettext("answer", "yes") if st.get('leds_flip') else pgettext("answer", "no"))
                print(f"LEDs:       {_on_off(not st['leds_off'])}" + flip)
            else:
                if st.get("error"):
                    print(_("Cannot control the Wave:3: {error}").format(error=st['error']))
                vol, mute = mic.gain()
                s = mic.settings
                print(f"Gain:       {vol}%{_muted_suffix(mute)}")
                lowcut = _("{freq} Hz (software)").format(freq=s.get('lowcut_freq')) if s.get('lowcut') \
                    else _on_off(False)
                print(_("Low cut:    {state}").format(state=lowcut))
                print(f"Clipguard:  {_('on (software)') if s.get('clipguard') else _on_off(False)}")
                print(_("LEDs:       only controllable on the Wave:3"))
    except (RuntimeError, ValueError, IndexError) as e:
        print(_("Error: {error}").format(error=e))
        return 1
    return 0


def cmd_obs(args):
    from . import obs
    from .config import Config
    cl = obs.client(Config())
    if not obs.wait_connected(cl, 5):
        print(_("No connection to OBS: {error}").format(error=cl.error or _("timeout")))
        print(_("In OBS: Tools → WebSocket Server Settings → enable; password in config.json (obs.password)"))
        return 1
    time.sleep(0.5)
    v = cl.request("GetVersion")
    print(_("Connected to OBS {version} (obs-websocket {ws_version})").format(
        version=v.get('obsVersion'), ws_version=v.get('obsWebSocketVersion')))
    for k, val in cl.state.items():
        print(f"  {k:<11} {val}")
    cl.close()
    return 0


def cmd_discord(args):
    from . import discord
    from .config import Config
    try:
        cl = discord.client(Config())
        print(_("Connecting to Discord – please confirm the request in Discord …"))
        cl.connect()
        print(_("Connected. Microphone {mic}, sound {sound}").format(
            mic=pgettext("state", "muted") if cl.voice.get('mute') else _on_off(True),
            sound=_on_off(not cl.voice.get('deaf'))))
    except discord.DiscordError as e:
        print(_("Error: {error}").format(error=e))
        return 1
    return 0


def cmd_selftest(args):
    from . import selftest
    from .daemon import Daemon
    pid = Daemon.already_running()
    if pid:
        if not args.stop_daemon:
            print(_("The LinDeck service is running and is using the device. With --stop-daemon it is "
                    "stopped for the test and restarted afterwards."))
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
        print(_("LinDeck is not running"))
        return 1
    print(_("Reloaded"))


def _signal_daemon():
    try:
        pid = int(paths.PID_FILE.read_text())
        os.kill(pid, signal.SIGHUP)
        return True
    except (OSError, ValueError):
        return False


def cmd_backup(args):
    """Alle Benutzerdaten (Profile, Einstellungen, Plugins) in eine Datei sichern."""
    from . import backup
    dest = Path(args.file).expanduser() if args.file else Path.cwd() / backup.default_name()
    if dest.is_dir():
        dest = dest / backup.default_name()
    try:
        m = backup.create(dest, plugins=not args.no_plugins)
    except backup.BackupError as e:
        print(_("Error: {error}").format(error=e), file=sys.stderr)
        return 1
    for line in backup.describe(m):
        print("  " + line)
    print(_("Backup saved: {path}").format(path=m["path"]))
    return 0


def cmd_restore(args):
    """Sicherung prüfen, Sicherheitskopie anlegen, Daten ersetzen, Programme neu starten."""
    from . import backup
    try:
        m = backup.inspect(args.file)
    except backup.BackupError as e:
        print(_("Error: {error}").format(error=e), file=sys.stderr)
        return 1
    for line in backup.describe(m):
        print("  " + line)
    if not args.yes:
        try:
            ok = input(_("Replace your current profiles and settings with this backup? [y/N] ")).strip().lower()
        except EOFError:
            ok = ""
        if not ok or ok[0] not in "yjos":  # yes, ja, oui, sí/sim
            print(_("Cancelled. Nothing was changed."))
            return 1
    progs = backup.stop_programs()
    try:
        __, safety = backup.restore(args.file)
    except backup.BackupError as e:
        print(_("Error: {error}").format(error=e), file=sys.stderr)
        backup.start_programs(progs)
        return 1
    backup.start_programs(progs)
    if safety:
        print(_("Your previous data was saved to {path}").format(path=safety))
    print(_("Backup restored."))
    return 0


LINEWAVE_CMDS = ("mixer", "wave", "linewave", "wave-app")
COMMON_CMDS = ("backup", "restore")


def _installed(cmd):
    """Gehört der Befehl zu einem Programm, das nicht installiert ist: Hinweis ausgeben."""
    if cmd in COMMON_CMDS:
        ok, program, package = parts.has_lindeck() or parts.has_linewave(), "Streamlin", "streamlin"
    elif cmd in LINEWAVE_CMDS:
        ok, program, package = parts.has_linewave(), "Linewave", "linewave"
    else:
        ok, program, package = parts.has_lindeck(), "LinDeck", "lindeck"
    if not ok:
        print(_("{program} is not installed. Install the package “{package}” (or “streamlin” for both programs).")
              .format(program=program, package=package), file=sys.stderr)
    return ok


def main(argv=None, prog="lindeck"):
    ap = argparse.ArgumentParser(prog=prog, description=_("Stream Deck software for Linux (Elgato compatible)"))
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("open", help=_("open the LinDeck window (default without a command)"))
    s.add_argument("profile", nargs="?", help=argparse.SUPPRESS)
    s.add_argument("--transport", default=None, help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_app)

    s = sub.add_parser("run", help=_("start LinDeck hidden (autostart)"))
    s.add_argument("--no-gui", action="store_true", help=_("start only the service, without a window (e.g. via SSH)"))
    s.add_argument("--transport", default=None, help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("edit", help=_("open the LinDeck window, optionally with a specific profile"))
    s.add_argument("profile", nargs="?", help=_("profile (ID or name) to open"))
    s.set_defaults(fn=cmd_edit)

    s = sub.add_parser("quit", help=_("quit the running LinDeck"))
    s.set_defaults(fn=cmd_quit)

    s = sub.add_parser("import", help=_("import Elgato profiles (.streamDeckProfile, backup, ProfilesV2/V3 folder)"))
    s.add_argument("files", nargs="+")
    s.add_argument("--keep", action="store_true", help=_("do not overwrite existing profiles"))
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("profiles", help=_("show imported profiles"))
    s.set_defaults(fn=cmd_profiles)

    s = sub.add_parser("devices", help=_("show connected Stream Decks"))
    s.add_argument("--transport", default=None, help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_devices)

    s = sub.add_parser("assign", help=_("permanently assign a profile to a device"))
    s.add_argument("serial")
    s.add_argument("profile")
    s.set_defaults(fn=cmd_assign)

    s = sub.add_parser("remove", help=_("delete a profile"))
    s.add_argument("profile")
    s.set_defaults(fn=cmd_remove)

    s = sub.add_parser("preview", help=_("render profile pages as PNG (no device needed)"))
    s.add_argument("profile")
    s.add_argument("-o", "--out", default=".")
    s.add_argument("--all", action="store_true", help=_("also render folders"))
    s.set_defaults(fn=cmd_preview)

    s = sub.add_parser("report", help=_("compatibility report for profiles"))
    s.add_argument("profile", nargs="?")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("extract-assets", help=_("copy the default icons from the Elgato installer"))
    s.add_argument("source", help=_("Stream_Deck_*.msi or a copied program folder"))
    s.set_defaults(fn=cmd_assets)

    s = sub.add_parser("plugin", help=_("manage Elgato plugins (list, install, remove)"))
    s.add_argument("action", choices=["list", "install", "remove"], nargs="?", default="list")
    s.add_argument("target", nargs="*", help=_("file (.streamDeckPlugin) or plugin UUID"))
    s.set_defaults(fn=cmd_plugin)

    s = sub.add_parser("mixer", help=_("audio mixer (Wave Link replacement): status, setup, remove, set, mute, "
                                       "assign, output"))
    s.add_argument("action", nargs="?", default="status",
                   choices=["status", "setup", "remove", "set", "mute", "assign", "output", "add", "delete"])
    s.add_argument("args", nargs="*", help=_("set <channel> <monitor|stream|both> <percent> · mute <channel> <mix> · "
                                             "assign <program> <channel> · output [device] · add <name> [input device] · "
                                             "delete <channel>"))
    s.set_defaults(fn=cmd_mixer)

    s = sub.add_parser("wave", help=_("microphone: status, gain, mute, lowcut, clipguard, leds, flip, "
                                      "headphone, monitor, noise"))
    s.add_argument("action", nargs="?", default="status",
                   choices=["status", "gain", "mute", "lowcut", "clipguard", "leds", "flip", "headphone", "monitor",
                            "noise"])
    s.add_argument("args", nargs="*", help=_("gain <percent> · lowcut on|off [80|120] · clipguard on|off · "
                                             "leds on|off · flip on|off · headphone <dB −60…0> · monitor <0…100> · "
                                             "noise on|off|status|strength <0…95>|backend auto|rnnoise|webrtc"))
    s.set_defaults(fn=cmd_wave)

    s = sub.add_parser("linewave", aliases=["wave-app"], help=_("open Linewave: mixer and microphone (separate program)"))
    s.add_argument("--hidden", action="store_true", help=_("start hidden (autostart)"))
    s.add_argument("--no-tray", action="store_true", help=_("no icon in the system tray"))
    s.add_argument("--quit", action="store_true", help=_("quit the running Linewave"))
    s.set_defaults(fn=cmd_wave_app)

    s = sub.add_parser("obs", help=_("test the connection to OBS"))
    s.set_defaults(fn=cmd_obs)

    s = sub.add_parser("discord", help=_("connect to Discord (one-time authorization)"))
    s.set_defaults(fn=cmd_discord)

    s = sub.add_parser("selftest", help=_("test connected Stream Decks step by step"))
    s.add_argument("--timeout", type=int, default=90, help=_("seconds per step"))
    s.add_argument("--stop-daemon", action="store_true", help=_("stop the running service for the test"))
    s.add_argument("--transport", default=None, help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_selftest)

    s = sub.add_parser("reload", help=_("reload the running service"))
    s.set_defaults(fn=cmd_reload)

    s = sub.add_parser("backup", help=_("save all profiles, settings and plugins to one file"))
    s.add_argument("file", nargs="?", help=_("target file or folder (default: streamlin-DATE.streamlin-backup)"))
    s.add_argument("--no-plugins", action="store_true", help=_("do not include installed plugins"))
    s.set_defaults(fn=cmd_backup)

    s = sub.add_parser("restore", help=_("restore a backup (replaces profiles and settings)"))
    s.add_argument("file", help=_("backup file (.streamlin-backup)"))
    s.add_argument("--yes", action="store_true", help=_("do not ask for confirmation"))
    s.set_defaults(fn=cmd_restore)

    args = ap.parse_args(argv)
    _setup_logging(args.verbose)
    if not getattr(args, "fn", None):
        args = ap.parse_args((argv or sys.argv[1:]) + ["open"])
    if not _installed(args.cmd or "open"):
        return 2
    return args.fn(args) or 0


def wave_main(argv=None):
    """Einstieg für `linewave`: ohne Befehl Fenster öffnen; `linewave mixer …`, `linewave mic …`."""
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = next((a for a in argv if not a.startswith("-")), "")
    if cmd in ("mixer", "mic", "wave", "backup", "restore"):
        i = argv.index(cmd)
        argv[i] = "wave" if cmd == "mic" else cmd
        return main(argv, prog="linewave")
    return main(["linewave"] + argv, prog="linewave")


if __name__ == "__main__":
    sys.exit(main())
