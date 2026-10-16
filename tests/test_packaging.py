"""Pakete: RPM wie .deb, Flatpak-Manifest, Starter im Flatpak und Programme des Systems."""
import configparser
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from streamlin import system

ROOT = Path(__file__).resolve().parent.parent
FLATPAK = ROOT / "packaging" / "flatpak"
APP_ID = "io.github.adblocker1.Streamlin"


def _launcher():
    spec = importlib.util.spec_from_file_location("streamlin_launcher", FLATPAK / "launcher.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def flatpak(tmp_path, monkeypatch):
    """Tut so, als liefe Streamlin im Flatpak."""
    info = tmp_path / "flatpak-info"
    info.write_text("[Application]\nname=%s\n\n[Instance]\ninstance-id=111\nbranch=master\narch=x86_64\n" % APP_ID)
    monkeypatch.setattr(system, "FLATPAK_INFO", str(info))
    return info


# ===================================================================== system.py
def test_host_cmd_only_in_flatpak(flatpak, monkeypatch):
    assert system.host_cmd(["pactl", "info"]) == ["flatpak-spawn", "--host", "pactl", "info"]
    assert system.host_cmd("obs --startreplaybuffer", shell=True) == [
        "flatpak-spawn", "--host", "sh", "-c", "obs --startreplaybuffer"]
    # eigene Programme bleiben in der Sandbox
    assert system.host_cmd(["linewave", "--hidden"]) == ["linewave", "--hidden"]
    assert system.host_cmd(["/app/bin/lindeck", "run"]) == ["/app/bin/lindeck", "run"]
    monkeypatch.setattr(system, "FLATPAK_INFO", str(flatpak) + ".fehlt")
    assert not system.in_flatpak()
    assert system.host_cmd(["pactl", "info"]) == ["pactl", "info"]


def test_run_bg_and_desktop_launch_use_host(flatpak, monkeypatch):
    started = []
    monkeypatch.setattr(system.subprocess, "Popen", lambda cmd, **kw: started.append((cmd, kw.get("shell"))))
    assert system.run_bg("flatpak run com.obsproject.Studio")
    assert system.run_bg("echo hi > /tmp/x", shell=True)
    monkeypatch.setattr(system, "have", lambda c: True)  # gtk-launch der Sandbox nicht benutzen
    assert system.launch_desktop({"id": "firefox", "file": "/run/host/usr/share/applications/firefox.desktop",
                                  "exec": "/usr/lib/firefox/firefox %u"})
    assert started == [
        (["flatpak-spawn", "--host", "flatpak", "run", "com.obsproject.Studio"], False),
        (["flatpak-spawn", "--host", "sh", "-c", "echo hi > /tmp/x"], False),
        (["flatpak-spawn", "--host", "/usr/lib/firefox/firefox"], False),
    ]


def test_known_apps_found_on_host(flatpak, monkeypatch):
    seen = []
    monkeypatch.setattr(system, "have", lambda c: False)  # in der Sandbox gibt es nichts
    monkeypatch.setattr(system.os.path, "exists",
                        lambda p: seen.append(p) or p in (str(flatpak), "/run/host/usr/bin/chromium"))
    assert system.first_available(["google-chrome", "chromium"]) == "chromium"
    assert "/run/host/usr/bin/google-chrome" in seen
    assert system.first_available(["nix", "flatpak run com.spotify.Client"]) == "flatpak run com.spotify.Client"


def test_run_bg_unchanged_outside_flatpak(monkeypatch):
    monkeypatch.setattr(system, "FLATPAK_INFO", "/nicht/da")
    started = []
    monkeypatch.setattr(system.subprocess, "Popen", lambda cmd, **kw: started.append((cmd, kw.get("shell"))))
    system.run_bg("obs")
    system.run_bg("a | b", shell=True)
    assert started == [(["obs"], False), ("a | b", True)]


# ===================================================================== Starter im Flatpak
class FakeHost:
    """flatpak-spawn --host flatpak ps / flatpak run … --hub"""

    def __init__(self, state, ps=""):
        self.state, self.ps, self.calls, self.started = state, ps, [], []

    def run(self, cmd, **kw):
        self.calls.append(cmd)
        assert cmd[:4] == ["flatpak-spawn", "--host", "flatpak", "ps"]
        return types.SimpleNamespace(stdout=self.ps, returncode=0)

    def popen(self, cmd, **kw):
        self.started.append(cmd)
        # der Hub meldet sich: Instanz 222 mit Pid 4242
        self.ps = f"222\t{APP_ID}\t4242\n999\torg.other.App\t5\n"
        (self.state / "hub.json").write_text(json.dumps({"instance": "222", "pid": 4242}))


def test_instances_and_stale_hub(tmp_path):
    L = _launcher()
    fake = FakeHost(tmp_path, f"111\t{APP_ID}\t100\n222\t{APP_ID}\tx\n333\torg.other.App\t7\n")
    assert L.instances(fake.run) == {"111": 100}
    assert L.hub_pid(tmp_path, fake.run) is None  # keine hub.json
    (tmp_path / "hub.json").write_text(json.dumps({"instance": "111", "pid": 99}))
    assert L.hub_pid(tmp_path, fake.run) is None  # Pid passt nicht mehr (veraltete Datei)
    (tmp_path / "hub.json").write_text(json.dumps({"instance": "111", "pid": 100}))
    assert L.hub_pid(tmp_path, fake.run) == 100


def test_ensure_hub_starts_one_hub(tmp_path):
    L = _launcher()
    fake = FakeHost(tmp_path)
    info = {"instance-id": "111", "branch": "master", "arch": "x86_64"}
    assert L.ensure_hub(info, tmp_path, fake.run, fake.popen, sleep=lambda s: None) == 4242
    assert fake.started == [["flatpak-spawn", "--host", "flatpak", "run", "--branch=master", "--arch=x86_64",
                             "--command=streamlin-launch", APP_ID, "--hub"]]
    # läuft schon: kein zweiter Hub
    assert L.ensure_hub(info, tmp_path, fake.run, fake.popen, sleep=lambda s: None) == 4242
    assert len(fake.started) == 1


def test_ensure_hub_gives_up(tmp_path):
    L = _launcher()
    fake = FakeHost(tmp_path)
    fake.popen = lambda cmd, **kw: None  # Hub startet nicht (z. B. altes flatpak)
    assert L.ensure_hub({}, tmp_path, fake.run, fake.popen, sleep=lambda s: None, wait=1) is None


def test_join_cmd_shares_pids_and_env():
    L = _launcher()
    cmd = L.join_cmd({"branch": "stable", "arch": "x86_64"}, 4242, "linewave", ["--hidden"],
                     env={"STREAMLIN_LANG": "de", "HOME": "/h", L.DIRECT: "1", "LINDECK_X": "1"})
    assert cmd == ["flatpak-spawn", "--host", "flatpak", "run", "--branch=stable", "--arch=x86_64",
                   "--parent-pid=4242", "--parent-share-pids", f"--env={L.DIRECT}=1", "--env=LINDECK_X=1",
                   "--env=STREAMLIN_LANG=de", "--command=linewave", APP_ID, "--hidden"]


def test_hub_waits_while_programs_run(tmp_path):
    L = _launcher()
    proc = tmp_path / "proc"
    for p in ("1", "7", "self"):
        (proc / p).mkdir(parents=True)
    clock = {"t": 0.0}

    def sleep(s):
        clock["t"] += s
        if clock["t"] >= 100 and (proc / "7").exists():
            (proc / "7").rmdir()  # das letzte Programm endet

    fake = FakeHost(tmp_path, f"222\t{APP_ID}\t4242\n")
    rc = L.hub({"instance-id": "222"}, tmp_path, fake.run, sleep=sleep, now=lambda: clock["t"], proc=str(proc))
    assert rc == 0
    assert 100 + L.HUB_IDLE <= clock["t"] <= 100 + L.HUB_IDLE + 4
    assert not (tmp_path / "hub.json").exists()


def test_autostart_entry_once(tmp_path, monkeypatch):
    L = _launcher()
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / "var-config"
    assert L.autostart_entry(cfg)
    entry = tmp_path / ".config" / "autostart" / f"{APP_ID}.desktop"
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(entry)
    assert cp["Desktop Entry"]["Exec"] == f"flatpak run --command=streamlin-launch {APP_ID} --autostart"
    entry.unlink()  # vom Benutzer entfernt: bleibt weg
    assert not L.autostart_entry(cfg)
    assert not entry.exists()


def test_main_direct_and_join(tmp_path, monkeypatch):
    L = _launcher()
    calls = []

    class Exec(Exception):
        pass

    def execve(path, args, env):
        calls.append(("direct", args[1:], env.get("PYTHONPATH", "")))
        raise Exec

    def execvp(path, args):
        calls.append(("join", args))
        raise Exec

    monkeypatch.setattr(L.os, "execve", execve)
    monkeypatch.setattr(L.os, "execvp", execvp)
    monkeypatch.setattr(L, "FLATPAK_INFO", str(tmp_path / "keins"))
    with pytest.raises(Exec):
        L.main(["linewave", "mixer", "status"])
    assert calls[-1][:2] == ("direct", ["-m", "streamlin.linewave", "mixer", "status"])
    assert L.main(["unbekannt"]) == 2

    # im Flatpak: über den Hub (außer --version/--help)
    info = tmp_path / "flatpak-info"
    info.write_text("[Instance]\ninstance-id=111\nbranch=stable\narch=x86_64\n")
    monkeypatch.setattr(L, "FLATPAK_INFO", str(info))
    monkeypatch.setattr(L.shutil, "which", lambda c: "/usr/bin/" + c)
    monkeypatch.setattr(L, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(L, "autostart_entry", lambda cfg: calls.append(("autostart",)))
    monkeypatch.setattr(L, "ensure_hub", lambda info, state: 4242)
    monkeypatch.delenv(L.DIRECT, raising=False)
    with pytest.raises(Exec):
        L.main(["lindeck", "--version"])
    assert calls[-1][0] == "direct"
    with pytest.raises(Exec):
        L.main(["lindeck"])
    assert calls[-2] == ("autostart",)
    kind, cmd = calls[-1]
    assert kind == "join" and "--parent-pid=4242" in cmd and cmd[-2:] == ["--command=lindeck", APP_ID]
    assert (tmp_path / "join").exists()
    # ohne Hub (z. B. keine Sitzung): direkt
    monkeypatch.setattr(L, "ensure_hub", lambda info, state: None)
    with pytest.raises(Exec):
        L.main(["lindeck", "profiles"])
    assert calls[-1][:2] == ("direct", ["-m", "streamlin", "profiles"])


# ===================================================================== Flatpak-Bau
def test_flatpak_manifest():
    yaml = pytest.importorskip("yaml")
    m = yaml.safe_load((FLATPAK / f"{APP_ID}.yml").read_text())
    assert m["id"] == APP_ID and m["command"] == "lindeck"
    args = m["finish-args"]
    for need in ("--device=all", "--socket=pulseaudio", "--socket=x11", "--talk-name=org.freedesktop.Flatpak",
                 "--talk-name=org.freedesktop.Notifications"):
        assert need in args

    def walk(mods):
        for mod in mods:
            yield mod
            yield from walk(mod.get("modules", []))

    names = []
    for mod in walk(m["modules"]):
        names.append(mod["name"])
        for src in mod.get("sources", []):
            if src["type"] in ("archive", "file"):
                assert len(src.get("sha256", "")) == 64, src["url"]
            elif src["type"] == "git":
                assert len(src.get("commit", "")) == 40, src["url"]
    assert names[-1] == "streamlin" and {"tkinter", "tcl", "tk", "hidapi", "python3-deps"} <= set(names)


def test_flatpak_install_script(tmp_path):
    dest = tmp_path / "app"
    r = subprocess.run([str(FLATPAK / "install.sh")], env=dict(os.environ, FLATPAK_DEST=str(dest)),
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    assert (dest / "bin/streamlin-launch").read_text() == (FLATPAK / "launcher.py").read_text()
    assert "streamlin-launch linewave" in (dest / "bin/linewave").read_text()
    assert (dest / "bin/pactl").read_text() == '#!/bin/sh\nexec flatpak-spawn --host --watch-bus pactl "$@"\n'
    assert os.access(dest / "bin/pw-dump", os.X_OK)
    for desk, exe, icon in ((APP_ID, "lindeck", APP_ID), (APP_ID + ".Linewave", "linewave", APP_ID + ".Linewave")):
        cp = configparser.ConfigParser(interpolation=None)
        cp.optionxform = str
        cp.read(dest / "share/applications" / f"{desk}.desktop", encoding="utf-8")
        assert cp["Desktop Entry"]["Exec"] == exe and cp["Desktop Entry"]["Icon"] == icon
        assert (dest / f"share/icons/hicolor/256x256/apps/{icon}.png").exists()
    assert "uinput" in (dest / "share/streamlin/70-streamlin.rules").read_text()
    assert (dest / "lib/streamlin/streamlin/waveui.py").exists() and (dest / "lib/streamlin/streamlin/editor.py").exists()
    # Weiterleitung: Argumente kommen unverändert bei flatpak-spawn an
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "flatpak-spawn").write_text('#!/bin/sh\nprintf "%s|" "$@"\n')
    (fake / "flatpak-spawn").chmod(0o755)
    r = subprocess.run([str(dest / "bin/pactl"), "load-module", "module-null-sink", "sink_name=a b"],
                       env=dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}"), capture_output=True, text=True)
    assert r.stdout == "--host|--watch-bus|pactl|load-module|module-null-sink|sink_name=a b|"


def test_launcher_runs_program_outside_flatpak():
    from streamlin import __version__
    r = subprocess.run([sys.executable, str(FLATPAK / "launcher.py"), "lindeck", "--version"],
                       env=dict(os.environ, PYTHONPATH=str(ROOT)), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == __version__


# ===================================================================== RPM
@pytest.mark.skipif(not (shutil.which("rpmbuild") and shutil.which("dpkg-deb") and shutil.which("rpm2cpio")
                         and shutil.which("cpio")), reason="rpmbuild/dpkg-deb fehlt")
def test_rpm_has_same_files_as_deb(tmp_path):
    from streamlin import __version__ as v
    for script in ("build-deb.sh", "build-rpm.sh"):
        r = subprocess.run([str(ROOT / "packaging" / script), str(tmp_path), "lindeck"], capture_output=True,
                           text=True, timeout=300)
        assert r.returncode == 0, r.stderr
    rpm = tmp_path / f"lindeck-{v}-1.noarch.rpm"
    deb_root, rpm_root = tmp_path / "deb", tmp_path / "rpm"
    subprocess.run(["dpkg-deb", "-x", str(tmp_path / f"lindeck_{v}_all.deb"), str(deb_root)], check=True)
    rpm_root.mkdir()
    subprocess.run(f"rpm2cpio '{rpm}' | cpio -idm --quiet", shell=True, cwd=rpm_root, check=True)

    def files(base, udev):
        out = {}
        for p in base.rglob("*"):
            if p.is_file():
                rel = str(p.relative_to(base)).replace(udev, "usr/lib/udev/")
                out[rel] = (p.read_bytes(), p.stat().st_mode & 0o777)
        return out

    assert files(deb_root, "lib/udev/") == files(rpm_root, "usr/lib/udev/")
    q = subprocess.run(["rpm", "-qp", "--qf", "%{NAME} %{VERSION} %{RELEASE} %{ARCH}\n", "--requires", "--obsoletes",
                        "--scripts", str(rpm)], capture_output=True, text=True, check=True).stdout
    assert q.startswith(f"lindeck {v} 1 noarch")
    assert "(python3-tkinter or python3-tk)" in q and "python3-evdev" in q
    assert "streamlin <= " in q  # ersetzt das Paket streamlin
    assert "posttrans scriptlet" in q and "setsid -f /usr/bin/lindeck run" in q
    assert 'rm -rf "/opt/lindeck"' in q
