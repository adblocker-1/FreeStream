"""Systemfunktionen: Programme starten, Zwischenablage, Lautstärke, Fenster, Audio."""
import configparser
import glob
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
from pathlib import Path, PureWindowsPath

log = logging.getLogger("lindeck.system")

# Bekannte Windows-Programme -> Linux-Befehle (Kandidaten in Reihenfolge)
KNOWN_APPS = {
    "chrome.exe": ["google-chrome", "chromium", "chromium-browser", "flatpak run com.google.Chrome"],
    "msedge.exe": ["microsoft-edge", "microsoft-edge-stable", "flatpak run com.microsoft.Edge"],
    "firefox.exe": ["firefox", "firefox-esr", "flatpak run org.mozilla.firefox"],
    "brave.exe": ["brave-browser", "brave", "flatpak run com.brave.Browser"],
    "opera.exe": ["opera"],
    "discord.exe": ["discord", "flatpak run com.discordapp.Discord"],
    "update.exe": ["discord", "flatpak run com.discordapp.Discord"],
    "spotify.exe": ["spotify", "flatpak run com.spotify.Client"],
    "steam.exe": ["steam", "flatpak run com.valvesoftware.Steam"],
    "obs64.exe": ["obs", "flatpak run com.obsproject.Studio"],
    "obs.exe": ["obs", "flatpak run com.obsproject.Studio"],
    "code.exe": ["code", "codium", "flatpak run com.visualstudio.code"],
    "teams.exe": ["teams-for-linux", "flatpak run com.github.IsmaelMartinez.teams_for_linux"],
    "ms-teams.exe": ["teams-for-linux", "flatpak run com.github.IsmaelMartinez.teams_for_linux"],
    "slack.exe": ["slack", "flatpak run com.slack.Slack"],
    "thunderbird.exe": ["thunderbird", "flatpak run org.mozilla.Thunderbird"],
    "vlc.exe": ["vlc", "flatpak run org.videolan.VLC"],
    "gimp.exe": ["gimp", "flatpak run org.gimp.GIMP"],
    "keepassxc.exe": ["keepassxc"],
    "telegram.exe": ["telegram-desktop", "flatpak run org.telegram.desktop"],
    "signal.exe": ["signal-desktop", "flatpak run org.signal.Signal"],
    "zoom.exe": ["zoom", "flatpak run us.zoom.Zoom"],
    "resolve.exe": ["/opt/resolve/bin/resolve", "davinci-resolve"],
    "notepad.exe": ["gnome-text-editor", "gedit", "kate", "mousepad", "xed"],
    "notepad++.exe": ["notepad-plus-plus", "gnome-text-editor", "kate"],
    "calc.exe": ["gnome-calculator", "kcalc", "galculator"],
    "explorer.exe": ["xdg-open ~"],
    "cmd.exe": ["x-terminal-emulator", "gnome-terminal", "konsole", "xterm"],
    "powershell.exe": ["x-terminal-emulator", "gnome-terminal", "konsole"],
    "wt.exe": ["x-terminal-emulator", "gnome-terminal", "konsole"],
    "taskmgr.exe": ["gnome-system-monitor", "plasma-systemmonitor", "htop"],
    "mstsc.exe": ["remmina", "xfreerdp"],
    "rdm.exe": ["remotedesktopmanager", "flatpak run net.devolutions.RDM"],
    "remotedesktopmanager.exe": ["remotedesktopmanager", "flatpak run net.devolutions.RDM"],
    "outlook.exe": ["thunderbird", "evolution"],
    "winword.exe": ["libreoffice --writer", "onlyoffice-desktopeditors"],
    "excel.exe": ["libreoffice --calc", "onlyoffice-desktopeditors"],
    "powerpnt.exe": ["libreoffice --impress", "onlyoffice-desktopeditors"],
    "wavelink.exe": ["lindeck-wavelink"],
}


def run_bg(cmd, shell=False):
    """Startet einen Prozess losgelöst im Hintergrund."""
    try:
        if isinstance(cmd, str) and not shell:
            cmd = shlex.split(os.path.expanduser(cmd))
        subprocess.Popen(cmd, shell=shell, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except (OSError, ValueError) as e:
        log.warning("Start fehlgeschlagen %s: %s", cmd, e)
        return False


def run_out(cmd, timeout=2):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def have(cmd):
    return shutil.which(cmd) is not None


def first_available(cands):
    for c in cands:
        exe = shlex.split(os.path.expanduser(c))[0]
        if exe == "xdg-open" or have(exe) or Path(exe).exists():
            return c
    return None


# --------------------------------------------------------------------------
# .desktop-Dateien durchsuchen
# --------------------------------------------------------------------------
_desktop_cache = None


def desktop_entries():
    global _desktop_cache
    if _desktop_cache is not None:
        return _desktop_cache
    dirs = [os.path.expanduser("~/.local/share/applications"), "/usr/share/applications",
            "/usr/local/share/applications", "/var/lib/flatpak/exports/share/applications",
            os.path.expanduser("~/.local/share/flatpak/exports/share/applications"),
            "/var/lib/snapd/desktop/applications"]
    entries = []
    for d in dirs:
        for f in glob.glob(os.path.join(d, "*.desktop")):
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                cp.read(f, encoding="utf-8")
                sec = cp["Desktop Entry"]
            except (configparser.Error, KeyError, UnicodeDecodeError):
                continue
            if sec.get("NoDisplay", "false").lower() == "true" or sec.get("Type", "Application") != "Application":
                continue
            entries.append({"file": f, "id": Path(f).stem, "name": sec.get("Name", ""),
                            "exec": sec.get("Exec", ""), "wmclass": sec.get("StartupWMClass", "")})
    _desktop_cache = entries
    return entries


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def find_desktop(name):
    n = _norm(name)
    if not n:
        return None
    best = None
    for e in desktop_entries():
        if _norm(e["name"]) == n or _norm(e["id"].split(".")[-1]) == n:
            return e
        if best is None and (n in _norm(e["name"]) or n in _norm(e["id"])):
            best = e
    return best


def launch_desktop(entry):
    if have("gtk-launch"):
        return run_bg(["gtk-launch", entry["id"]])
    if have("gio"):
        return run_bg(["gio", "launch", entry["file"]])
    cmd = re.sub(r"%[fFuUdDnNickvm]", "", entry["exec"]).strip()
    return run_bg(cmd)


# --------------------------------------------------------------------------
# Windows-Pfade abbilden
# --------------------------------------------------------------------------
def map_path(path: str, path_map: dict) -> str:
    """Wendet path_map (Präfix-Ersetzung) an; Schrägstriche werden normalisiert."""
    if not path:
        return path
    p = path.replace("\\", "/")
    for src, dst in sorted((path_map or {}).items(), key=lambda kv: -len(kv[0])):
        s = src.replace("\\", "/")
        if p.lower().startswith(s.lower()):
            return os.path.expanduser(dst + p[len(s):])
    return os.path.expanduser(p)


def is_windows_path(p: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", p or "")) or (p or "").startswith("\\\\")


def resolve_program(name_or_path: str, app_map: dict, app_name: str = ""):
    """Ermittelt einen Linux-Befehl für einen Windows-Programmpfad / App-Namen."""
    app_map = {k.lower(): v for k, v in (app_map or {}).items()}
    exe = PureWindowsPath(name_or_path).name.lower() if name_or_path else ""
    for key in (exe, (app_name or "").lower(), (name_or_path or "").lower()):
        if key and key in app_map:
            return ("cmd", app_map[key])
    if exe in KNOWN_APPS:
        c = first_available(KNOWN_APPS[exe])
        if c:
            return ("cmd", c)
    for cand in (app_name, Path(exe).stem if exe else ""):
        e = find_desktop(cand)
        if e:
            return ("desktop", e)
    stem = Path(exe).stem if exe else _norm(app_name)
    for c in (stem, stem.rstrip("0123456789"), _norm(app_name)):
        if c and have(c):
            return ("cmd", c)
    return None


def open_target(path: str, cfg) -> bool:
    """Öffnet Datei, Ordner, URL oder Programm (com.elgato.streamdeck.system.open)."""
    if not path:
        return False
    path = path.strip().strip('"')
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", path) or path.startswith("mailto:"):
        return run_bg(["xdg-open", path])
    if is_windows_path(path):
        mapped = map_path(path, cfg.get("path_map"))
        if not is_windows_path(mapped) and os.path.exists(mapped):
            path = mapped
        else:
            prog = resolve_program(path, cfg.get("app_map"))
            if prog:
                return launch(prog)
            log.warning("Windows-Pfad ohne Zuordnung: %s (in config.json unter path_map/app_map eintragen)", path)
            return False
    else:
        path = map_path(path, cfg.get("path_map"))
    if os.path.isfile(path) and os.access(path, os.X_OK) and not path.endswith((".sh", ".desktop")):
        return run_bg([path])
    if path.endswith(".desktop"):
        return run_bg(["gio", "launch", path]) if have("gio") else run_bg(["xdg-open", path])
    if os.path.exists(path):
        return run_bg(["xdg-open", path])
    if have(path.split()[0]):
        return run_bg(path)
    log.warning("Ziel nicht gefunden: %s", path)
    return False


def launch(prog):
    kind, val = prog
    if kind == "desktop":
        return launch_desktop(val)
    return run_bg(val)


def process_names_for(prog, exe_hint=""):
    names = set()
    if prog:
        kind, val = prog
        if kind == "desktop":
            cmd = shlex.split(re.sub(r"%[a-zA-Z]", "", val["exec"]))
            if cmd:
                if cmd[0] == "flatpak" and len(cmd) > 2:
                    names.add(cmd[-1].split(".")[-1].lower())
                else:
                    names.add(Path(cmd[0]).name)
            if val.get("wmclass"):
                names.add(val["wmclass"].lower())
        else:
            parts = shlex.split(val)
            if parts and parts[0] == "flatpak":
                names.add(parts[-1].split(".")[-1].lower())
            elif parts:
                names.add(Path(parts[0]).name)
    if exe_hint:
        names.add(Path(PureWindowsPath(exe_hint).name).stem.lower())
    return {n for n in names if n}


def kill_processes(names):
    ok = False
    for n in names:
        if subprocess.run(["pkill", "-i", "-x", n[:15]], capture_output=True).returncode == 0:
            ok = True
        elif subprocess.run(["pkill", "-i", "-f", f"(^|/){re.escape(n)}( |$)"], capture_output=True).returncode == 0:
            ok = True
    return ok


def is_running(names):
    for n in names:
        if subprocess.run(["pgrep", "-i", "-x", n[:15]], capture_output=True).returncode == 0:
            return True
    return False


def focus_window(names):
    """Bringt ein Fenster nach vorne (X11 mit wmctrl, KDE mit kdotool)."""
    for n in names:
        if have("wmctrl") and os.environ.get("DISPLAY") and \
                subprocess.run(["wmctrl", "-x", "-a", n], capture_output=True).returncode == 0:
            return True
        if have("kdotool"):
            wid = run_out(["kdotool", "search", "--class", n]).split()
            if wid:
                subprocess.run(["kdotool", "windowactivate", wid[0]], capture_output=True)
                return True
    return False


# --------------------------------------------------------------------------
# Zwischenablage
# --------------------------------------------------------------------------
def wayland():
    return bool(os.environ.get("WAYLAND_DISPLAY"))


def clipboard_get():
    if wayland() and have("wl-paste"):
        return run_out(["wl-paste", "--no-newline"])
    if have("xclip"):
        return run_out(["xclip", "-selection", "clipboard", "-o"])
    if have("xsel"):
        return run_out(["xsel", "--clipboard", "--output"])
    return None


def clipboard_set(text: str) -> bool:
    if wayland() and have("wl-copy"):
        cmd = ["wl-copy"]
    elif have("xclip"):
        cmd = ["xclip", "-selection", "clipboard"]
    elif have("xsel"):
        cmd = ["xsel", "--clipboard", "--input"]
    else:
        log.warning("Keine Zwischenablage verfügbar (wl-clipboard oder xclip installieren)")
        return False
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        p.stdin.write(text.encode("utf-8"))
        p.stdin.close()
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# Audio (PipeWire/PulseAudio)
# --------------------------------------------------------------------------
def _wpctl_target(source=False):
    return "@DEFAULT_AUDIO_SOURCE@" if source else "@DEFAULT_AUDIO_SINK@"


def get_volume(source=False):
    """Gibt (Prozent, stumm) zurück."""
    if have("wpctl"):
        out = run_out(["wpctl", "get-volume", _wpctl_target(source)])
        m = re.search(r"Volume:\s*([\d.]+)", out)
        if m:
            return round(float(m.group(1)) * 100), "MUTED" in out
    if have("pactl"):
        what = "source" if source else "sink"
        out = run_out(["pactl", f"get-{what}-volume", f"@DEFAULT_{what.upper()}@"])
        m = re.search(r"(\d+)%", out)
        mute = run_out(["pactl", f"get-{what}-mute", f"@DEFAULT_{what.upper()}@"])
        if m:
            return int(m.group(1)), "yes" in mute
    return 0, False


def change_volume(delta_pct: int, source=False):
    if have("wpctl"):
        sign = "+" if delta_pct >= 0 else "-"
        subprocess.run(["wpctl", "set-volume", "-l", "1.0", _wpctl_target(source), f"{abs(delta_pct)}%{sign}"],
                       capture_output=True)
    elif have("pactl"):
        what = "source" if source else "sink"
        subprocess.run(["pactl", f"set-{what}-volume", f"@DEFAULT_{what.upper()}@", f"{delta_pct:+d}%"],
                       capture_output=True)


def set_volume(pct: int, source=False):
    pct = max(0, min(100, int(pct)))
    if have("wpctl"):
        subprocess.run(["wpctl", "set-volume", _wpctl_target(source), f"{pct}%"], capture_output=True)
    elif have("pactl"):
        what = "source" if source else "sink"
        subprocess.run(["pactl", f"set-{what}-volume", f"@DEFAULT_{what.upper()}@", f"{pct}%"], capture_output=True)


def toggle_mute(source=False):
    if have("wpctl"):
        subprocess.run(["wpctl", "set-mute", _wpctl_target(source), "toggle"], capture_output=True)
    elif have("pactl"):
        what = "source" if source else "sink"
        subprocess.run(["pactl", f"set-{what}-mute", f"@DEFAULT_{what.upper()}@", "toggle"], capture_output=True)


def media(cmd: str) -> bool:
    """playerctl-Steuerung (play-pause, next, previous, stop, position)."""
    if have("playerctl"):
        subprocess.run(["playerctl"] + cmd.split(), capture_output=True)
        return True
    return False


# --------------------------------------------------------------------------
# Soundboard
# --------------------------------------------------------------------------
class SoundPlayer:
    def __init__(self):
        self._procs = {}
        self._lock = threading.Lock()

    def _cmd(self, path, volume, output, loop):
        vol = max(0, min(100, int(volume if volume is not None else 100)))
        if have("mpv"):
            c = ["mpv", "--no-video", "--really-quiet", f"--volume={vol}"]
            if output:
                c.append(f"--audio-device=pipewire/{output}")
            if loop:
                c.append("--loop-file=inf")
            return c + [path]
        if have("ffplay"):
            return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-volume", str(vol)] + \
                   (["-loop", "0"] if loop else []) + [path]
        if have("pw-play"):
            c = ["pw-play", f"--volume={vol / 100:.2f}"]
            if output:
                c += ["--target", output]
            return c + [path]
        if have("paplay"):
            return ["paplay", f"--volume={int(vol / 100 * 65536)}", path]
        return None

    def play(self, key, path, volume=100, output="", loop=False):
        cmd = self._cmd(path, volume, output, loop)
        if not cmd:
            log.warning("Kein Audioplayer gefunden (mpv, ffplay oder pipewire-bin installieren)")
            return None
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            log.warning("Audio-Wiedergabe fehlgeschlagen: %s", e)
            return None
        with self._lock:
            self._procs.setdefault(key, []).append(p)
        return p

    def is_playing(self, key):
        with self._lock:
            procs = [p for p in self._procs.get(key, []) if p.poll() is None]
            self._procs[key] = procs
            return bool(procs)

    def stop(self, key=None):
        with self._lock:
            keys = [key] if key is not None else list(self._procs)
            for k in keys:
                for p in self._procs.pop(k, []):
                    if p.poll() is None:
                        p.terminate()


# --------------------------------------------------------------------------
# Aktives Fenster (Smart Profiles)
# --------------------------------------------------------------------------
def active_window_class():
    """WM_CLASS / App-ID des aktiven Fensters (X11, KDE, Hyprland, Sway)."""
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and have("hyprctl"):
        m = re.search(r"class:\s*(\S+)", run_out(["hyprctl", "activewindow"]))
        return m.group(1).lower() if m else ""
    if os.environ.get("SWAYSOCK") and have("swaymsg"):
        out = run_out(["swaymsg", "-t", "get_tree"])
        m = re.findall(r'"focused":\s*true.*?"app_id":\s*"([^"]+)"', out, re.S)
        return m[0].lower() if m else ""
    if have("kdotool") and "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", ""):
        wid = run_out(["kdotool", "getactivewindow"]).strip()
        if wid:
            return run_out(["kdotool", "getwindowclassname", wid]).strip().lower()
    if os.environ.get("DISPLAY") and have("xprop"):
        out = run_out(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
        m = re.search(r"window id # (0x[0-9a-f]+)", out)
        if m and m.group(1) != "0x0":
            cls = run_out(["xprop", "-id", m.group(1), "WM_CLASS"])
            parts = re.findall(r'"([^"]*)"', cls)
            return parts[-1].lower() if parts else ""
    return ""


# --------------------------------------------------------------------------
# Audiogeräte und App-Lautstärken (Elgato Volume Controller)
# --------------------------------------------------------------------------
def _pactl_blocks(what):
    """pactl list <what> -> Liste von dicts mit index, name, description, sink, props, volume, mute."""
    out = run_out(["pactl", "list", what], timeout=3)
    blocks, cur = [], None
    for line in out.splitlines():
        if re.match(r"^\S.*#\d+", line):
            cur = {"index": line.split("#")[-1].strip(), "props": {}}
            blocks.append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("Name:"):
            cur["name"] = s.split(":", 1)[1].strip()
        elif s.startswith("Description:"):
            cur["description"] = s.split(":", 1)[1].strip()
        elif s.startswith("Sink:"):
            cur["sink"] = s.split(":", 1)[1].strip()
        elif s.startswith("Argument:"):
            cur["argument"] = s.split(":", 1)[1].strip()
        elif s.startswith("Mute:"):
            cur["mute"] = s.endswith("yes")
        elif s.startswith("Volume:") and "volume" not in cur:
            m = re.search(r"(\d+)%", s)
            cur["volume"] = int(m.group(1)) if m else 0
        elif "=" in s and s.split("=", 1)[0].strip().count(".") >= 1:
            k, v = s.split("=", 1)
            cur["props"][k.strip()] = v.strip().strip('"')
    return blocks


def find_audio_device(kind: str, friendly: str = "", device_id: str = ""):
    """kind 'sink'/'source' -> pactl-Name oder Standardgerät."""
    default = "@DEFAULT_SINK@" if kind == "sink" else "@DEFAULT_SOURCE@"
    if not friendly or (device_id or "").lower() == "default" or not have("pactl"):
        return default
    n = _norm(friendly)
    for b in _pactl_blocks(kind + "s"):
        if kind == "source" and b.get("name", "").endswith(".monitor"):
            continue
        if n and (n in _norm(b.get("description")) or _norm(b.get("description")) in n):
            return b.get("name", default)
    return default


def device_volume(kind, name):
    if have("pactl"):
        out = run_out(["pactl", f"get-{kind}-volume", name])
        m = re.search(r"(\d+)%", out)
        mute = "yes" in run_out(["pactl", f"get-{kind}-mute", name])
        if m:
            return int(m.group(1)), mute
    return get_volume(source=(kind == "source"))


def device_change(kind, name, delta):
    if have("pactl"):
        cur, _ = device_volume(kind, name)
        device_set(kind, name, cur + delta)
    else:
        change_volume(delta, source=(kind == "source"))


def device_set(kind, name, pct):
    pct = max(0, min(100, int(pct)))
    if have("pactl"):
        subprocess.run(["pactl", f"set-{kind}-volume", name, f"{pct}%"], capture_output=True)
    else:
        set_volume(pct, source=(kind == "source"))


def device_mute(kind, name, mode="toggle"):
    if have("pactl"):
        subprocess.run(["pactl", f"set-{kind}-mute", name, mode], capture_output=True)
    else:
        toggle_mute(source=(kind == "source"))


def app_streams(app: str):
    """Sink-Inputs (Wiedergabe-Streams) eines Programms."""
    n = _norm(Path(PureWindowsPath(app).name).stem if app else "")
    if not n or not have("pactl"):
        return []
    res = []
    for b in _pactl_blocks("sink-inputs"):
        p = b["props"]
        names = [p.get("application.name"), p.get("application.process.binary"), p.get("application.id"),
                 p.get("node.name")]
        if any(n and x and (n in _norm(x) or _norm(x) in n) for x in names if x):
            res.append(b)
    return res


def app_volume(app):
    s = app_streams(app)
    return (s[0].get("volume", 0), s[0].get("mute", False)) if s else (None, False)


def app_change(app, delta=None, set_to=None, mute=None):
    for b in app_streams(app):
        idx = b["index"]
        if set_to is not None:
            subprocess.run(["pactl", "set-sink-input-volume", idx, f"{max(0, min(100, int(set_to)))}%"],
                           capture_output=True)
        elif delta is not None:
            new = max(0, min(100, b.get("volume", 0) + delta))
            subprocess.run(["pactl", "set-sink-input-volume", idx, f"{new}%"], capture_output=True)
        if mute is not None:
            subprocess.run(["pactl", "set-sink-input-mute", idx, mute], capture_output=True)
