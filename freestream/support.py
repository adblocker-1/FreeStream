"""Support-Bericht: eine Textdatei mit allem, was für ein GitHub-Issue nützlich ist.

Version und Pakete, System und Desktop, Stream-Deck-Geräte und Rechte, Audio
(PipeWire/PulseAudio), Zustand von FreeDeck/FreeWave, config.json (Geheimnisse
entfernt) und die letzten Zeilen des Protokolls. Jeder Sammler ist robust:
fehlende Programme, Fehler und Zeitüberschreitungen (höchstens 3 s) landen als
Hinweis im Bericht, nie als Absturz. Der Bericht selbst ist Englisch.

Aufruf: `freedeck support-report [DATEI]`, `freewave support-report [DATEI]`
oder Einstellungen → Hilfe.
"""
import concurrent.futures
import datetime
import glob
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__, parts, paths

ISSUE_URL = "https://github.com/adblocker-1/Streamlin/issues/new"
TIMEOUT = 3  # Sekunden je Befehl
LOG_LINES = 300
VENDOR = "0fd9"  # Elgato
REDACTED = "<redacted>"
STATE_FILES = ("state.json", "mixer.json", "update.json")  # Zustand, der beim Helfen nützt

# Schlüsselnamen, deren Werte nie im Bericht stehen
SECRET_KEY = re.compile(r"token|passw|secret|api_?key|apikey|auth(?!or)|credential|cookie|webhook|private"
                        r"|session_?id|(^|[_-])key$|^key$|stream_?key", re.I)
JWT = re.compile(r"^eyJ[\w-]+\.[\w-]+\.[\w-]+$")
# „token=…“, "password": "…" usw. in Protokollzeilen
SECRET_TEXT = re.compile(r"""((?:token|passw\w*|secret|api_?key|authorization)["']?\s*[:=]\s*(?:bearer\s+)?["']?)"""
                         r"""[^\s"',;}]+""", re.I)


# ------------------------------------------------------------------ Geheimnisse entfernen
def is_secret_key(key):
    return isinstance(key, str) and bool(SECRET_KEY.search(key))


def redact(data):
    """Kopie von data ohne Geheimnisse (rekursiv; nach Schlüsselnamen und JWT-ähnlichen Werten)."""
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if is_secret_key(k) and v not in (None, "", [], {}) and not isinstance(v, bool):
                out[k] = REDACTED
            else:
                out[k] = redact(v)
        return out
    if isinstance(data, list):
        return [redact(v) for v in data]
    if isinstance(data, str) and JWT.match(data.strip()):
        return REDACTED
    return data


def redact_text(text):
    """Geheimnisse in freiem Text (Protokoll) ersetzen, Home-Ordner kürzen."""
    text = SECRET_TEXT.sub(lambda m: m.group(1) + REDACTED, text)
    text = re.sub(r"\beyJ[\w-]{8,}\.[\w-]+\.[\w-]+", REDACTED, text)
    home = os.path.expanduser("~")
    if len(home) > 1:
        text = text.replace(home, "~")
    return text


# ------------------------------------------------------------------ Hilfen
def run(cmd, timeout=TIMEOUT, max_lines=None):
    """Ausgabe eines Befehls; bei fehlendem Programm, Fehler oder Zeitüberschreitung ein Hinweis."""
    if not shutil.which(cmd[0]):
        return f"({cmd[0]} not available)"
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return f"({cmd[0]}: timed out after {timeout} s)"
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return f"({cmd[0]}: {e})"
    out = (p.stdout or "").rstrip()
    err = (p.stderr or "").strip()
    if p.returncode and err:
        out = (out + "\n" if out else "") + f"(exit {p.returncode}: {err.splitlines()[-1]})"
    elif not out:
        out = f"(no output, exit {p.returncode})"
    lines = out.splitlines()
    if max_lines and len(lines) > max_lines:
        out = "\n".join(lines[:max_lines] + [f"… ({len(lines) - max_lines} more lines)"])
    return out


def read(path, default="(missing)"):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError as e:
        return default if isinstance(e, FileNotFoundError) else f"({e.strerror or e})"


def _json_file(path):
    p = Path(path)
    if not p.exists():
        return "(missing)"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return f"(unreadable: {e})"
    return json.dumps(redact(data), indent=2, ensure_ascii=False)


def tail(path, lines=LOG_LINES):
    p = Path(path)
    try:
        with open(p, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 512 * 1024))
            data = fh.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return "(no log file)"
    except OSError as e:
        return f"({e.strerror or e})"
    return "\n".join(data.splitlines()[-lines:]) or "(empty)"


def _access(path):
    if not os.path.exists(path):
        return "missing"
    try:
        st = os.stat(path)
        mode = oct(st.st_mode & 0o777)
    except OSError:
        mode = "?"
    rw = ("r" if os.access(path, os.R_OK) else "-") + ("w" if os.access(path, os.W_OK) else "-")
    return f"{mode}, this user: {rw}"


# ------------------------------------------------------------------ Sammler
def freestream_info(app=""):
    from . import updater
    pk = updater.installed_packages()
    lines = [
        f"FreeStream version: {__version__}",
        f"Report created by: {app or 'command line'}",
        f"Running from: {Path(__file__).resolve().parent}",
        f"FreeDeck available: {parts.has_freedeck()}, FreeWave available: {parts.has_freewave()}",
        f"Installed packages: {pk or '-'}",
        f"Update channel: {updater.channel()}, check at start: {updater.configured()}",
        "",
        "dpkg:",
        run(["dpkg-query", "-W", "-f=${Package} ${Version} ${Status}\n", *updater.PACKAGES]),
    ]
    return "\n".join(lines)


def system_info():
    try:
        import tkinter
        tk_version = f"{tkinter.TkVersion} (Tcl {tkinter.Tcl().eval('info patchlevel')})"
    except Exception as e:  # noqa: BLE001 - kein tkinter, kein Tcl
        tk_version = f"(not available: {e})"
    osr = {}
    for line in read("/etc/os-release", "").splitlines():
        k, sep, v = line.partition("=")
        if sep:
            osr[k] = v.strip().strip('"')
    env = os.environ
    lines = [
        f"Python: {sys.version.split()[0]} ({sys.executable})",
        f"Tk: {tk_version}",
        f"Distribution: {osr.get('PRETTY_NAME') or '?'} (ID={osr.get('ID', '?')}, "
        f"VERSION_ID={osr.get('VERSION_ID', '?')}, ID_LIKE={osr.get('ID_LIKE', '-')})",
        f"Kernel: {platform.release()} {platform.machine()}",
        f"Desktop: XDG_CURRENT_DESKTOP={env.get('XDG_CURRENT_DESKTOP', '')} "
        f"XDG_SESSION_DESKTOP={env.get('XDG_SESSION_DESKTOP', '')} DESKTOP_SESSION={env.get('DESKTOP_SESSION', '')}",
        f"Session: XDG_SESSION_TYPE={env.get('XDG_SESSION_TYPE', '')} "
        f"WAYLAND_DISPLAY={'set' if env.get('WAYLAND_DISPLAY') else '-'} DISPLAY={env.get('DISPLAY', '-')}",
        f"Language: LANG={env.get('LANG', '')} FREESTREAM_LANG={env.get('FREESTREAM_LANG', '')}",
        f"Groups: {run(['id', '-Gn'])}",
    ]
    return "\n".join(lines)


def usb_devices(sysfs="/sys/bus/usb/devices"):
    """Elgato-Geräte laut sysfs: [„0fd9:0080 Stream Deck MK.2 (Elgato) bus 1-2“]."""
    out = []
    for d in sorted(glob.glob(os.path.join(sysfs, "*"))):
        if read(os.path.join(d, "idVendor"), "").lower() != VENDOR:
            continue
        pid = read(os.path.join(d, "idProduct"), "?")
        name = read(os.path.join(d, "product"), "?")
        maker = read(os.path.join(d, "manufacturer"), "?")
        out.append(f"{VENDOR}:{pid} {name} ({maker}) at {os.path.basename(d)}")
    return out


def hidraw_devices(sysfs="/sys/class/hidraw"):
    """hidraw-Knoten von Elgato-Geräten mit Rechten."""
    out = []
    for d in sorted(glob.glob(os.path.join(sysfs, "hidraw*"))):
        uevent = read(os.path.join(d, "device", "uevent"), "")
        if f":0000{VENDOR.upper()}:" not in uevent.upper():
            continue
        name = next((ln.split("=", 1)[1] for ln in uevent.splitlines() if ln.startswith("HID_NAME=")), "?")
        node = "/dev/" + os.path.basename(d)
        out.append(f"{node} {name}: {_access(node)}")
    return out


def udev_rules():
    """Regeldateien, die Elgato-Geräte freigeben (/lib ist oft ein Verweis auf /usr/lib: nur einmal)."""
    found = {}
    for folder in ("/etc/udev/rules.d", "/lib/udev/rules.d", "/usr/lib/udev/rules.d"):
        for f in sorted(glob.glob(os.path.join(folder, "*.rules"))):
            try:
                text = Path(f).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if VENDOR in text.lower():
                found.setdefault(os.path.realpath(f), f + (" (with uinput)" if "uinput" in text else ""))
    return list(found.values())


def device_info():
    usb = usb_devices()
    hid = hidraw_devices()
    rules = udev_rules()
    lines = ["USB (sysfs): " + ("\n  ".join([""] + usb) if usb else "no Elgato device found"),
             "hidraw: " + ("\n  ".join([""] + hid) if hid else "no Elgato hidraw node"),
             f"/dev/uinput: {_access('/dev/uinput')}",
             "udev rules with 0fd9: " + ("\n  ".join([""] + rules) if rules else "none found"),
             "lsusb:", run(["lsusb", "-d", f"{VENDOR}:"])]
    return "\n".join(lines)


def audio_info():
    lines = ["pactl info:", run(["pactl", "info"]),
             "", "Sinks:", run(["pactl", "list", "short", "sinks"], max_lines=40),
             "", "Sources:", run(["pactl", "list", "short", "sources"], max_lines=40),
             "", "Modules:", run(["pactl", "list", "short", "modules"], max_lines=60),
             "", "PipeWire:", run(["pipewire", "--version"])]
    return "\n".join(lines)


def state_info():
    lines = []
    for name in ("freedeck", "freewave", "freestream"):
        pid = read(paths.STATE_DIR / f"{name}.pid", "")
        if pid:
            lines.append(f"PID file {name}.pid: {pid}")
    lines.append("Processes: " + ("\n  ".join([""] + [redact_text(p) for p in processes()]) or "none running"))
    try:
        profiles = len(list(paths.PROFILES_DIR.iterdir())) if paths.PROFILES_DIR.is_dir() else 0
    except OSError:
        profiles = "?"
    try:
        plugins = sorted(p.name for p in paths.PLUGINS_DIR.iterdir()) if paths.PLUGINS_DIR.is_dir() else []
    except OSError:
        plugins = []
    lines.append(f"Profiles: {profiles}")
    lines.append(f"Plugins: {', '.join(plugins) or '-'}")
    lines.append(f"Discord authorized: {(paths.STATE_DIR / 'discord-token.json').exists()}")
    others = sorted(f.name for f in paths.STATE_DIR.glob("*.json") if f.name not in STATE_FILES) \
        if paths.STATE_DIR.is_dir() else []
    lines.append(f"Other state files: {', '.join(others) or '-'}")
    for name in STATE_FILES:
        text = _json_file(paths.STATE_DIR / name)
        rows = text.splitlines()
        if len(rows) > 150:
            text = "\n".join(rows[:150] + [f"… ({len(rows) - 150} more lines)"])
        lines += ["", f"{name}:", text]
    return "\n".join(lines)


def processes(proc="/proc"):
    """Laufende FreeDeck-/FreeWave-Prozesse (ohne diesen): [„1234 freedeck run“]."""
    out = []
    for d in sorted(glob.glob(os.path.join(proc, "[0-9]*")), key=lambda p: int(os.path.basename(p))):
        pid = os.path.basename(d)
        if pid == str(os.getpid()):
            continue
        try:
            args = Path(d, "cmdline").read_bytes().decode("utf-8", errors="replace").split("\0")
        except OSError:
            continue
        args = [a for a in args if a]
        if not args:
            continue
        names = [os.path.basename(a) for a in args[:3]]
        python = names[0].startswith("python")
        if any(n in ("freedeck", "freewave", "lindeck", "linewave") for n in names) or \
                (python and any(n in args[1:3] for n in ("freestream", "streamlin"))):
            out.append(f"{pid} {' '.join(args)[:200]}")
    return out[:20]


def config_info():
    return _json_file(paths.CONFIG_FILE)


def log_info():
    lines = [redact_text(f"{paths.LOG_FILE} (last {LOG_LINES} lines):"), redact_text(tail(paths.LOG_FILE))]
    plugin_logs = sorted((paths.STATE_DIR / "plugins").glob("*.log")) if (paths.STATE_DIR / "plugins").is_dir() \
        else []
    for f in plugin_logs[:10]:
        lines += ["", f"Plugin log {f.name} (last 40 lines):", redact_text(tail(f, 40))]
    return "\n".join(lines)


def sections(app=""):
    return [("FreeStream", lambda: freestream_info(app)), ("System", system_info), ("FreeDeck devices", device_info),
            ("Audio", audio_info), ("FreeDeck / FreeWave state", state_info),
            ("config.json (secrets redacted)", config_info), ("Log", log_info)]


# ------------------------------------------------------------------ Bericht
def build_report(app=""):
    """Bericht als Text. Die Sammler laufen parallel, jeder fängt seine Fehler selbst ab."""
    items = sections(app)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(items)) as pool:
        futures = [pool.submit(fn) for _name, fn in items]
        results = []
        for (name, _fn), fut in zip(items, futures):
            try:
                text = fut.result(timeout=30)
            except Exception as e:  # noqa: BLE001 - ein kaputter Sammler darf den Bericht nicht verhindern
                text = f"(failed: {type(e).__name__}: {e})"
            results.append((name, text))
    now = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    out = [f"FreeStream support report – {now}",
           f"Attach this file to a GitHub issue: {ISSUE_URL}",
           "Passwords, tokens and keys are replaced by " + REDACTED + "."]
    for name, text in results:
        out += ["", "=" * 72, f"## {name}", "=" * 72, text.rstrip()]
    return "\n".join(out) + "\n"


def default_name(now=None):
    return (now or datetime.datetime.now()).strftime("freestream-support-%Y%m%d-%H%M%S.txt")


def write_report(target=None, app=""):
    """Bericht schreiben. target: Datei, Ordner oder None (Home-Ordner). Gibt den Pfad zurück."""
    target = Path(os.path.expanduser(str(target))) if target else Path.home()
    if target.is_dir():
        target = target / default_name()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_report(app), encoding="utf-8")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target
