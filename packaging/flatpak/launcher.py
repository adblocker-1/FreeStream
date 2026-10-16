#!/usr/bin/env python3
"""Starter für LinDeck und Linewave im Flatpak (/app/bin/streamlin-launch).

Jedes „flatpak run“ bekommt einen eigenen Prozess-Namensraum. LinDeck und Linewave erkennen
laufende Instanzen aber über Pids (Statusdateien) und schicken sich Signale. Deshalb laufen
alle Programme im Namensraum eines kleinen „Hub“-Prozesses:

  1. Gibt es keinen Hub, startet der Starter einen (``flatpak run … --hub`` auf dem System).
  2. Dann startet er das gewünschte Programm mit ``flatpak run --parent-pid=<Hub>
     --parent-share-pids`` – es sieht damit alle anderen Streamlin-Prozesse.
  3. Der Hub beendet sich, wenn in seinem Namensraum eine Weile nichts mehr läuft.

Geht das nicht (kein flatpak-spawn, keine Sitzung, Aufruf außerhalb des Flatpaks), startet
das Programm einfach direkt. Nur Standardbibliothek.
"""
import configparser
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

APP_ID = os.environ.get("FLATPAK_ID") or "io.github.adblocker1.Streamlin"
FLATPAK_INFO = "/.flatpak-info"
APP_DIR = "/app/lib/streamlin"
MODULES = {"lindeck": "streamlin", "linewave": "streamlin.linewave"}
DIRECT = "STREAMLIN_FLATPAK_DIRECT"   # gesetzt: Programm hier direkt starten
PASS_ENV = ("STREAMLIN_", "LINDECK_")  # werden in den geteilten Namensraum mitgenommen
HUB_GRACE = 30    # s: so lange nach dem Start bzw. einem Beitritt bleibt der Hub sicher
HUB_IDLE = 10     # s: ohne andere Prozesse -> Hub beendet sich
AUTOSTART = "{id}.desktop"


def in_flatpak():
    return os.path.exists(FLATPAK_INFO)


def flatpak_info():
    """[Instance] aus /.flatpak-info: instance-id, branch, arch."""
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read(FLATPAK_INFO, encoding="utf-8")
        return dict(cp["Instance"]) if cp.has_section("Instance") else {}
    except (OSError, configparser.Error):
        return {}


def runtime_dir():
    """Von allen Instanzen der App geteilter Ordner ($XDG_RUNTIME_DIR/app/<id>)."""
    base = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "app" / APP_ID
    if base.is_dir():
        return base
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "streamlin-flatpak"
    base.mkdir(parents=True, exist_ok=True)
    return base


def host(cmd):
    return ["flatpak-spawn", "--host"] + list(cmd)


def flatpak_run(info, args):
    """Befehl, der auf dem System dieselbe App (gleicher Zweig, gleiche Architektur) startet."""
    cmd = ["flatpak", "run"]
    for key in ("branch", "arch"):
        if info.get(key):
            cmd.append(f"--{key}={info[key]}")
    return cmd + list(args)


def instances(run=subprocess.run):
    """Laufende Flatpak-Instanzen auf dem System: {instance-id: child-pid}."""
    r = run(host(["flatpak", "ps", "--columns=instance,application,child-pid"]),
            capture_output=True, text=True, timeout=10)
    out = {}
    for line in (r.stdout or "").splitlines():
        cols = line.split()
        if len(cols) >= 3 and cols[1] == APP_ID and cols[2].isdigit():
            out[cols[0]] = int(cols[2])
    return out


def hub_pid(state_dir, run=subprocess.run):
    """System-Pid des laufenden Hubs oder None (Datei veraltet oder Hub beendet)."""
    try:
        hub = json.loads((state_dir / "hub.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = instances(run).get(hub.get("instance"))
    return pid if pid and pid == hub.get("pid") else None


def ensure_hub(info, state_dir, run=subprocess.run, popen=subprocess.Popen, sleep=time.sleep, wait=10.0):
    """Hub suchen oder starten; gibt seine System-Pid zurück (None: geht nicht)."""
    with open(state_dir / "hub.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pid = hub_pid(state_dir, run)
        if pid:
            return pid
        popen(host(flatpak_run(info, ["--command=streamlin-launch", APP_ID, "--hub"])),
              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              start_new_session=True)
        for _i in range(int(wait / 0.2)):
            sleep(0.2)
            pid = hub_pid(state_dir, run)
            if pid:
                return pid
    return None


def join_cmd(info, pid, prog, args, env=None):
    """Programm im Namensraum des Hubs starten (auf dem System, über flatpak-spawn)."""
    env = os.environ if env is None else env
    extra = [f"--env={k}={v}" for k, v in sorted(env.items()) if k.startswith(PASS_ENV) and k != DIRECT]
    return host(flatpak_run(info, [f"--parent-pid={pid}", "--parent-share-pids", f"--env={DIRECT}=1", *extra,
                                   f"--command={prog}", APP_ID, *args]))


def _others(proc="/proc"):
    """Andere Prozesse im eigenen Namensraum (ohne Pid 1 = bwrap und ohne uns)."""
    me = os.getpid()
    return [int(p) for p in os.listdir(proc) if p.isdigit() and int(p) not in (1, me)]


def hub(info, state_dir, run=subprocess.run, sleep=time.sleep, now=time.monotonic, proc="/proc"):
    """Hub: hält den Namensraum offen, solange darin Streamlin läuft."""
    me = None
    for _i in range(25):
        me = instances(run).get(info.get("instance-id"))
        if me:
            break
        sleep(0.2)
    if not me:
        return 1
    hub_file = state_dir / "hub.json"
    hub_file.write_text(json.dumps({"instance": info.get("instance-id"), "pid": me}), encoding="utf-8")
    idle_since, started = None, now()
    try:
        while True:
            sleep(2)
            t = now()
            try:
                joined = (state_dir / "join").stat().st_mtime
            except OSError:
                joined = 0
            busy = _others(proc) or t - started < HUB_GRACE or time.time() - joined < HUB_GRACE
            if busy:
                idle_since = None
            elif idle_since is None:
                idle_since = t
            elif t - idle_since >= HUB_IDLE:
                return 0
    finally:
        try:
            if json.loads(hub_file.read_text(encoding="utf-8")).get("pid") == me:
                hub_file.unlink()
        except (OSError, ValueError):
            pass


def autostart_entry(config_home):
    """Einmalig einen Autostart-Eintrag anlegen (startet LinDeck und Linewave im Hintergrund)."""
    marker = config_home / "streamlin" / "flatpak-autostart"
    target = Path.home() / ".config" / "autostart" / AUTOSTART.format(id=APP_ID)
    if marker.exists() or target.exists():
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("[Desktop Entry]\nType=Application\nName=Streamlin\n"
                          "Comment=Start LinDeck and Linewave in the background\n"
                          f"Exec=flatpak run --command=streamlin-launch {APP_ID} --autostart\n"
                          f"Icon={APP_ID}\nNoDisplay=true\nX-GNOME-Autostart-enabled=true\n"
                          f"X-Flatpak={APP_ID}\n", encoding="utf-8")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("1\n", encoding="utf-8")
        return True
    except OSError:
        return False


def direct(prog, args):
    """Programm hier im eigenen Prozess starten."""
    env = dict(os.environ)
    if os.path.isdir(APP_DIR):
        env["PYTHONPATH"] = APP_DIR + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.pop(DIRECT, None)
    os.execve(sys.executable, [sys.executable, "-m", MODULES[prog], *args], env)


def main(argv):
    if not argv:
        argv = ["lindeck"]
    if argv[0] == "--hub":
        return hub(flatpak_info(), runtime_dir())
    progs = [("lindeck", ["run"]), ("linewave", ["--hidden"])] if argv[0] == "--autostart" else [(argv[0], argv[1:])]
    for prog, _args in progs:
        if prog not in MODULES:
            print(f"streamlin-launch: unknown program {prog}", file=sys.stderr)
            return 2
    simple = any(a in ("--version", "-h", "--help") for _p, args in progs for a in args)
    pid = None
    if in_flatpak() and not os.environ.get(DIRECT) and not simple and shutil.which("flatpak-spawn"):
        info = flatpak_info()
        if progs[0][1] == []:  # Fenster geöffnet: einmalig Autostart einrichten
            autostart_entry(Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"))
        try:
            state = runtime_dir()
            pid = ensure_hub(info, state)
        except (OSError, subprocess.SubprocessError):
            pid = None
    if not pid:
        if len(progs) == 1:
            direct(*progs[0])
        procs = [subprocess.Popen([sys.executable, os.path.abspath(__file__), p, *a],
                                  env=dict(os.environ, **{DIRECT: "1"})) for p, a in progs]
    else:
        (state / "join").touch()
        if len(progs) == 1:
            cmd = join_cmd(info, pid, *progs[0])
            os.execvp(cmd[0], cmd)
        procs = [subprocess.Popen(join_cmd(info, pid, p, a)) for p, a in progs]
    return max(p.wait() for p in procs)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
