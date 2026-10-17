"""Sicherung und Wiederherstellung: Rundreise, Zip-Slip, Sicherheitskopie, Verweise."""
import json
import os
import zipfile

import pytest

from freestream import backup, paths


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Eigenes Zuhause mit Profil, Plugin, Einstellungen, Logs und Caches."""
    share = tmp_path / "share"
    cfg, data, state = tmp_path / "cfg" / "freestream", share / "freestream", tmp_path / "state" / "freestream"
    for name, val in (("CONFIG_DIR", cfg), ("DATA_DIR", data), ("STATE_DIR", state),
                      ("CONFIG_FILE", cfg / "config.json"), ("PID_FILE", state / "freestream.pid")):
        monkeypatch.setattr(paths, name, val)
    monkeypatch.setattr(backup, "running_programs", lambda: [])
    prof = data / "profiles" / "P1.sdProfile"
    (prof / "Profiles" / "PAGE" / "Images").mkdir(parents=True)
    (prof / "manifest.json").write_text(json.dumps({"Name": "Gaming"}))
    (prof / "Profiles" / "PAGE" / "Images" / "bg.png").write_bytes(b"\x89PNG" + b"x" * 100)
    (data / "resources").mkdir()
    (data / "resources" / "icon.png").write_bytes(b"icon")
    plug = data / "plugins" / "com.x.sdPlugin"
    (plug / "__pycache__").mkdir(parents=True)
    (plug / "manifest.json").write_text(json.dumps({"Name": "X Plugin"}))
    (plug / "bin").write_text("#!/bin/sh\n")
    (plug / "bin").chmod(0o755)
    (plug / "__pycache__" / "m.cpython-312.pyc").write_bytes(b"c")
    (data / "plugin-data").mkdir()
    (data / "plugin-data" / "com.x.json").write_text("{}")
    cfg.mkdir(parents=True)
    (cfg / "config.json").write_text(json.dumps({"language": "en", "mixer": {"enabled": True,
                                                  "channels": [{"id": "game", "name": "Game"}]}}))
    state.mkdir(parents=True)
    (state / "state.json").write_text('{"ABC": {"profile": "P1"}}')
    (state / "wavemic.json").write_text('{"lowcut": true}')
    (state / "freestream.log").write_text("log")
    (state / "mixer.json").write_text("{}")
    (state / "updates").mkdir()
    (state / "updates" / "freestream_9.deb").write_bytes(b"deb")
    # alter Ordnername als Verweis (wie nach dem Umzug 0.13) und Verweise im Datenordner
    (share / "lindeck").symlink_to("freestream")
    (data / "old-profiles").symlink_to("profiles")
    (data / "outside").symlink_to(tmp_path)
    return tmp_path


def _names(path):
    with zipfile.ZipFile(path) as z:
        return set(z.namelist())


def test_create_contents(home):
    m = backup.create(home / "b")
    path = home / ("b" + backup.EXT)
    assert m["path"] == str(path)
    names = _names(path)
    assert {"manifest.json", "config/config.json", "state/state.json", "state/wavemic.json",
            "data/profiles/P1.sdProfile/manifest.json", "data/profiles/P1.sdProfile/Profiles/PAGE/Images/bg.png",
            "data/resources/icon.png", "data/plugins/com.x.sdPlugin/bin", "data/plugin-data/com.x.json"} <= names
    # keine Logs, Caches, Laufzeitdaten, Updates
    assert not any("__pycache__" in n or n.endswith(".log") or "updates" in n or "mixer.json" in n for n in names)
    # Verweis im Datenordner: nicht doppelt, nur als Link; Verweis nach außen gar nicht
    assert not any(n.startswith("data/old-profiles") or n.startswith("data/outside") for n in names)
    assert m["links"] == {"data/old-profiles": "profiles"}
    assert m["contents"]["profiles"] == ["Gaming"] and m["contents"]["plugins"] == ["X Plugin"]
    info = backup.inspect(path)
    assert info["plugins_included"] and any("Gaming" in line for line in backup.describe(info))


def test_without_plugins_keeps_installed(home):
    path = backup.create(home / "np.freestream-backup", plugins=False)["path"]
    assert not any(n.startswith("data/plugins/") for n in _names(path))
    plug = paths.DATA_DIR / "plugins" / "com.x.sdPlugin"
    backup.restore(path)
    assert (plug / "manifest.json").exists()  # nicht in der Sicherung -> bleibt


def test_round_trip_and_safety_backup(home):
    path = backup.create(home / "full.freestream-backup")["path"]
    # danach ändert sich alles
    (paths.CONFIG_DIR / "config.json").write_text('{"language": "de"}')
    prof = paths.DATA_DIR / "profiles" / "P1.sdProfile"
    (prof / "manifest.json").write_text(json.dumps({"Name": "Changed"}))
    (paths.DATA_DIR / "profiles" / "NEW.sdProfile").mkdir()
    (paths.STATE_DIR / "wavemic.json").unlink()
    import shutil
    shutil.rmtree(paths.DATA_DIR / "plugins")

    m, safety = backup.restore(path)
    assert json.loads((paths.CONFIG_DIR / "config.json").read_text())["mixer"]["channels"][0]["id"] == "game"
    assert json.loads((prof / "manifest.json").read_text())["Name"] == "Gaming"
    assert not (paths.DATA_DIR / "profiles" / "NEW.sdProfile").exists()
    assert (paths.STATE_DIR / "wavemic.json").read_text() == '{"lowcut": true}'
    exe = paths.DATA_DIR / "plugins" / "com.x.sdPlugin" / "bin"
    assert exe.exists() and os.access(exe, os.X_OK)
    assert (paths.DATA_DIR / "old-profiles").is_symlink() and os.readlink(paths.DATA_DIR / "old-profiles") == "profiles"
    assert (paths.DATA_DIR / "outside").is_symlink()  # eigener Verweis nach außen bleibt
    assert (paths.STATE_DIR / "freestream.log").exists()  # Laufzeitdaten unberührt
    assert not any(p.name.startswith(backup.STAGE) for p in paths.DATA_DIR.iterdir())
    # Sicherheitskopie enthält den Stand vor dem Einspielen
    assert safety.parent == paths.STATE_DIR / "backups"
    with zipfile.ZipFile(safety) as z:
        assert json.loads(z.read("config/config.json")) == {"language": "de"}
        assert "data/profiles/NEW.sdProfile/manifest.json" not in z.namelist()
    # nur die letzten Sicherheitskopien bleiben
    for _i in range(backup.KEEP_SAFETY + 1):
        backup.restore(path)
    assert len(list((paths.STATE_DIR / "backups").glob("*" + backup.EXT))) == backup.KEEP_SAFETY


def _evil(home, entries, links=None, files=None):
    path = home / "evil.freestream-backup"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("manifest.json", json.dumps({
            "format": backup.FORMAT, "format_version": 1, "files": files if files is not None else list(entries),
            "links": links or {}}))
        for n, data in entries.items():
            z.writestr(n, data)
    return path


@pytest.mark.parametrize("name", ["data/../../evil.txt", "/etc/evil", "config/../x", "data\\..\\x",
                                  "other/x", "state/freestream.log", "state/../../x", "C:/x"])
def test_zip_slip_rejected(home, name):
    path = _evil(home, {name: "boom"})
    with pytest.raises(backup.BackupError):
        backup.inspect(path)
    with pytest.raises(backup.BackupError):
        backup.restore(path)
    assert not (home / "evil.txt").exists()
    assert json.loads((paths.CONFIG_DIR / "config.json").read_text())["language"] == "en"


@pytest.mark.parametrize("target", ["../..", "../../../etc", "/etc", "a/../../.."])
def test_link_escape_rejected(home, target):
    path = _evil(home, {"data/x.txt": "x"}, links={"data/sub/link": target})
    with pytest.raises(backup.BackupError):
        backup.inspect(path)


def test_link_chain_escape_rejected(home):
    # self -> "." ist harmlos, "self/.." zeigt aber physisch nach außen
    path = _evil(home, {"data/x.txt": "x"}, links={"data/self": ".", "data/up": "self/.."})
    with pytest.raises(backup.BackupError):
        backup.restore(path)
    assert (paths.DATA_DIR / "profiles" / "P1.sdProfile").exists()  # nichts ersetzt


def test_invalid_archives(home):
    junk = home / "junk.freestream-backup"
    junk.write_text("no zip")
    with pytest.raises(backup.BackupError):
        backup.inspect(junk)
    with pytest.raises(backup.BackupError):  # Datei nicht im Manifest
        backup.inspect(_evil(home, {"data/a": "1"}, files=[]))
    with zipfile.ZipFile(home / "new.freestream-backup", "w") as z:
        z.writestr("manifest.json", json.dumps({"format": backup.FORMAT, "format_version": 99, "files": []}))
    with pytest.raises(backup.BackupError):
        backup.inspect(home / "new.freestream-backup")


def test_zip_bomb_rejected(home, monkeypatch):
    monkeypatch.setattr(backup, "MAX_BYTES", 1000)
    path = _evil(home, {"data/big": "0" * 5000})
    with pytest.raises(backup.BackupError):
        backup.inspect(path)


def test_programs_restarted(home, monkeypatch):
    stopped, started = [], []
    monkeypatch.setattr(backup, "running_programs", lambda: [("freewave", 4242, True)])
    monkeypatch.setattr(backup.os, "kill", lambda pid, sig: stopped.append(pid))
    monkeypatch.setattr(backup.desktop, "_alive", lambda pid: False)
    monkeypatch.setattr(backup.subprocess, "Popen", lambda cmd, **kw: started.append(cmd))
    progs = backup.stop_programs()
    backup.start_programs(progs)
    assert stopped == [4242] and started and started[0][-1] == "--hidden"


def test_cli(home, monkeypatch, capsys):
    from freestream.__main__ import main, wave_main
    out = home / "cli.freestream-backup"
    assert main(["backup", str(out), "--no-plugins"]) == 0
    assert out.exists() and not any(n.startswith("data/plugins/") for n in _names(out))
    (paths.CONFIG_DIR / "config.json").write_text("{}")
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert main(["restore", str(out)]) == 1
    assert (paths.CONFIG_DIR / "config.json").read_text() == "{}"
    assert wave_main(["restore", str(out), "--yes"]) == 0
    assert json.loads((paths.CONFIG_DIR / "config.json").read_text())["language"] == "en"
    assert wave_main(["restore", str(home / "missing.freestream-backup"), "--yes"]) == 1


def test_settings_backup_card(home, monkeypatch):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("kein Display")
    root.withdraw()
    from freestream import settingsui
    restarts = []
    monkeypatch.setattr(settingsui.SettingsDialog, "winfo_screenheight", lambda self: 720)  # kleiner Bildschirm
    try:
        dlg = settingsui.SettingsDialog(root, "FreeWave", lambda: restarts.append(1))
        assert dlg.winfo_reqheight() <= 720 - 60  # Platz für Titelleiste und Panel
        out = home / "ui.freestream-backup"
        monkeypatch.setattr(settingsui.filedialog, "asksaveasfilename", lambda **kw: str(out))
        dlg._backup()
        _pump(root, lambda: not dlg._busy)
        assert out.exists() and out.name in dlg.backup_status.cget("text")
        (paths.CONFIG_DIR / "config.json").write_text("{}")
        monkeypatch.setattr(settingsui.filedialog, "askopenfilename", lambda **kw: str(out))
        monkeypatch.setattr(settingsui.messagebox, "askyesno", lambda *a, **kw: True)
        monkeypatch.setattr(settingsui.messagebox, "showinfo", lambda *a, **kw: None)
        dlg._restore()
        _pump(root, lambda: restarts)
        assert restarts == [1]
        assert json.loads((paths.CONFIG_DIR / "config.json").read_text())["language"] == "en"
    finally:
        root.destroy()


def _pump(root, cond, steps=300):
    import time
    for _i in range(steps):
        root.update()
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("Zeitüberschreitung")
