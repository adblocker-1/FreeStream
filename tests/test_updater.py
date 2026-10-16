"""Automatische Updates: Release prüfen, herunterladen, installieren, abschalten."""
import hashlib
import io
import subprocess
import sys

import pytest

from streamlin import paths, updater

PREFIX = updater.DOWNLOAD_PREFIX


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    for name, sub in (("CONFIG_DIR", "cfg"), ("DATA_DIR", "data"), ("STATE_DIR", "state"),
                      ("PROFILES_DIR", "data/profiles"), ("RESOURCES_DIR", "data/res")):
        monkeypatch.setattr(paths, name, tmp_path / sub)
    monkeypatch.setattr(paths, "CONFIG_FILE", tmp_path / "cfg" / "config.json")
    monkeypatch.delenv("STREAMLIN_NO_UPDATE_CHECK", raising=False)
    return tmp_path


def release(version="9.1.0", pkgs=("streamlin", "lindeck", "linewave"), data=b"deb"):
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    return {"tag_name": f"v{version}", "html_url": "https://github.com/x/releases/tag/v" + version,
            "body": "## New\n\n**English**\n\n- Better things\n\n---\n\n**Deutsch**\n\n- Bessere Dinge\n",
            "assets": [{"name": f"{p}_{version}_all.deb", "size": len(data), "digest": digest,
                        "browser_download_url": f"{PREFIX}v{version}/{p}_{version}_all.deb"} for p in pkgs]}


def test_versions():
    assert updater.parse_version("v0.16.0") == (0, 16, 0)
    assert updater.is_newer("0.17.0", "0.16.0")
    assert updater.is_newer("0.16.10", "0.16.9")
    assert not updater.is_newer("0.16.0", "0.16.0")
    assert not updater.is_newer("0.9.0", "0.16.0")
    assert not updater.is_newer("", "0.16.0")


def test_release_picks_installed_packages_only():
    up = updater.release_update(release(), {"lindeck": "0.16.0", "linewave": "0.16.0"}, current="0.16.0")
    assert up.version == "9.1.0"
    assert [f["name"] for f in up.files] == ["lindeck_9.1.0_all.deb", "linewave_9.1.0_all.deb"]
    assert up.notes == "## New\n\n- Better things"


def test_no_update_when_not_newer_missing_or_unsafe():
    pk = {"streamlin": "0.16.0"}
    assert updater.release_update(release("0.16.0"), pk, current="0.16.0") is None
    assert updater.release_update(release(pkgs=("lindeck",)), pk, current="0.16.0") is None
    assert updater.release_update(release(), {}, current="0.16.0") is None
    r = release()
    r["prerelease"] = True
    assert updater.release_update(r, pk, current="0.16.0") is None
    r = release()
    for a in r["assets"]:
        a["browser_download_url"] = "https://example.com/" + a["name"]
    assert updater.release_update(r, pk, current="0.16.0") is None
    r = release()
    for a in r["assets"]:
        a.pop("digest")
    assert updater.release_update(r, pk, current="0.16.0") is None


def test_source_checkout_gets_no_updates():
    assert updater.installed_packages() == {}
    assert updater.check() is None


def test_switch_off_and_on(home):
    assert updater.enabled()
    paths.CONFIG_FILE.parent.mkdir(parents=True)
    paths.CONFIG_FILE.write_text('{"language": "fr"}')
    updater.set_enabled(False)
    assert not updater.enabled()
    assert '"language": "fr"' in paths.CONFIG_FILE.read_text()  # andere Einstellungen bleiben
    updater.set_enabled(True)
    assert updater.enabled()


def test_env_switches_off(monkeypatch):
    monkeypatch.setenv("STREAMLIN_NO_UPDATE_CHECK", "1")
    assert not updater.enabled()
    assert updater.configured()  # die gespeicherte Einstellung bleibt


def test_skip_and_later():
    assert not updater.suppressed("9.1.0")
    updater.remind_later("9.1.0", now=1000)
    assert updater.suppressed("9.1.0", now=1001)
    assert not updater.suppressed("9.1.0", now=1000 + updater.LATER_SECONDS + 1)
    assert not updater.suppressed("9.2.0", now=1001)
    updater.skip_version("9.1.0")
    assert updater.suppressed("9.1.0", now=10 ** 10)


def test_just_updated_once(monkeypatch):
    updater.save_state(installing=updater.__version__)
    assert updater.just_updated() == updater.__version__
    assert updater.just_updated() is None


def _serve(monkeypatch, data):
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda req, timeout=0: io.BytesIO(data))


def test_download_checks_sha256(home, monkeypatch):
    up = updater.release_update(release(data=b"good"), {"streamlin": "0"}, current="0.16.0")
    _serve(monkeypatch, b"good")
    seen = []
    files = updater.download(up, home / "dl", progress=lambda d, t: seen.append((d, t)))
    assert [f.read_bytes() for f in files] == [b"good"]
    assert seen[-1] == (4, 4)
    _serve(monkeypatch, b"evil")
    with pytest.raises(updater.UpdateError):
        updater.download(up, home / "dl")
    assert not list((home / "dl").glob("*.deb"))


def test_install_command(monkeypatch, tmp_path):
    monkeypatch.setattr(updater.shutil, "which", lambda n: f"/usr/bin/{n}")
    cmd = updater.install_command([tmp_path / "a.deb"])
    assert cmd[:4] == ["/usr/bin/pkexec", "/usr/bin/apt-get", "install", "-y"]
    assert cmd[-1] == str(tmp_path / "a.deb")
    monkeypatch.setattr(updater.shutil, "which", lambda n: None)
    assert updater.install_command([tmp_path / "a.deb"]) is None


def test_only_one_program_asks():
    a, b = updater.PromptLock(), updater.PromptLock()
    assert a.acquire()
    code = ("import fcntl,sys;f=open(sys.argv[1],'w')\n"
            "try:\n fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);print('free')\nexcept OSError:\n print('busy')")
    lockfile = str(paths.STATE_DIR / "update.lock")
    assert subprocess.run([sys.executable, "-c", code, lockfile], capture_output=True, text=True).stdout.strip() \
        == "busy"
    a.release()
    assert b.acquire()
    b.release()


# ------------------------------------------------------------------ Dialog
tk = pytest.importorskip("tkinter")


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("kein Display")
    r.withdraw()
    yield r
    r.destroy()


def _pump(root, cond, steps=200):
    import time
    for _i in range(steps):
        root.update()
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_dialog_install_flow(root, monkeypatch, home):
    from streamlin import updateui
    up = updater.release_update(release(), {"streamlin": "0"}, current="0.16.0")
    monkeypatch.setattr(updater, "download", lambda u, progress=None: (progress(1, 1), [home / "x.deb"])[1])
    monkeypatch.setattr(updater, "start_install", lambda files, v: subprocess.Popen(
        ["true"], stdout=subprocess.PIPE, text=True))
    restarted = []
    dlg = updateui.UpdateDialog(root, up, "LinDeck", lambda: restarted.append(1), automatic=True)
    assert dlg.wm_transient() == ""  # unsichtbar gestartet: eigenes Fenster
    dlg._install()
    assert _pump(root, lambda: restarted)


def test_dialog_install_cancelled(root, monkeypatch, home):
    from streamlin import updateui
    up = updater.release_update(release(), {"streamlin": "0"}, current="0.16.0")
    monkeypatch.setattr(updater, "download", lambda u, progress=None: [home / "x.deb"])
    monkeypatch.setattr(updater, "start_install", lambda files, v: subprocess.Popen(
        ["sh", "-c", "exit 126"], stdout=subprocess.PIPE, text=True))
    dlg = updateui.UpdateDialog(root, up, "LinDeck", lambda: None)
    dlg._install()
    assert _pump(root, lambda: not dlg.busy)
    assert "abgebrochen" in dlg.status.cget("text")
    assert dlg.install_btn.winfo_manager()


def test_dialog_later_and_skip(root, home):
    from streamlin import updateui
    up = updater.release_update(release(), {"streamlin": "0"}, current="0.16.0")
    lock = updater.PromptLock()
    assert lock.acquire()
    dlg = updateui.UpdateDialog(root, up, "Linewave", lambda: None, lock=lock, automatic=True)
    dlg.auto.set(False)
    dlg._toggle_auto()
    assert not updater.enabled()
    dlg._later()
    assert updater.suppressed("9.1.0")
    assert updater.PromptLock().acquire()  # Sperre wieder frei
    dlg = updateui.UpdateDialog(root, up, "Linewave", lambda: None)
    dlg._skip()
    assert updater.load_state()["skipped"] == "9.1.0"


def test_settings_dialog(root, monkeypatch, home):
    from streamlin import i18n, settingsui
    restarts, asked = [], []
    monkeypatch.setattr(settingsui, "choose_language", lambda code, app, restart, parent: asked.append(code))
    dlg = settingsui.open_settings(root, "LinDeck", lambda: restarts.append(1))
    assert settingsui.open_settings(root, "LinDeck", lambda: None) is dlg  # nur ein Fenster
    # Updates ausschalten
    assert dlg.auto_update.get()
    dlg.auto_update.set(False)
    dlg._toggle_updates()
    assert not updater.enabled() and not updater.configured()
    # Sprache wählen
    idx = 1 + i18n.available().index("fr")
    dlg.language_box.current(idx)
    dlg._language()
    assert asked == ["fr"]
    dlg.destroy()
