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
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    return tmp_path


def _asset(p, version, kind):
    return f"{p}_{version}_all.deb" if kind == "deb" else f"{p}-{version}-1.noarch.rpm"


def release(version="9.1.0", pkgs=("streamlin", "lindeck", "linewave"), data=b"deb", kinds=("deb",), **extra):
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    rel = {"tag_name": f"v{version}", "html_url": "https://github.com/x/releases/tag/v" + version,
           "body": "## New\n\n**English**\n\n- Better things\n\n---\n\n**Deutsch**\n\n- Bessere Dinge\n",
           "assets": [{"name": _asset(p, version, k), "size": len(data), "digest": digest,
                       "browser_download_url": f"{PREFIX}v{version}/{_asset(p, version, k)}"}
                      for p in pkgs for k in kinds]}
    rel.update(extra)
    return rel


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


# ------------------------------------------------------------------ Beta, Zurückstufen, rpm, Flatpak
def test_prerelease_versions_sort_before_final():
    order = ["0.18.0", "0.19.0-beta.1", "0.19.0-beta.2", "0.19.0-rc.1", "0.19.0", "0.19.1"]
    assert sorted(reversed(order), key=updater.version_key) == order
    assert updater.is_newer("0.19.0", "0.19.0-beta.2")
    assert updater.is_newer("0.19.0-beta.1", "0.18.0")
    assert updater.is_older("0.18.0", "0.19.0-beta.1")
    assert not updater.is_older("", "0.18.0")


def _releases():
    return [release("0.18.0", published_at="2026-09-01T10:00:00Z"),
            release("0.20.0", draft=True),
            release("0.19.0-beta.1", prerelease=True, published_at="2026-10-02T10:00:00Z"),
            release("0.17.0", published_at="2026-08-01T10:00:00Z"),
            release("0.16.0", pkgs=("lindeck",)),
            {"tag_name": "", "draft": False}, "garbage"]


def test_release_list_parsing():
    assert [r["tag_name"] for r in updater.published(_releases())] == \
        ["v0.19.0-beta.1", "v0.18.0", "v0.17.0", "v0.16.0"]
    assert [r["tag_name"] for r in updater.published(_releases(), beta=False)] == ["v0.18.0", "v0.17.0", "v0.16.0"]


def test_beta_channel_takes_newest_prerelease_never_draft(home):
    pk = {"streamlin": "0.18.0"}
    up = updater.newest_update(_releases(), pk, current="0.18.0")
    assert up.version == "0.19.0-beta.1" and up.prerelease
    assert updater.newest_update(_releases(), pk, current="0.19.0-beta.1") is None
    assert updater.newest_update(_releases(), pk, current="0.18.0", beta=False) is None
    # stabiler Kanal ignoriert Vorabversionen weiterhin
    assert updater.release_update(release("0.19.0-beta.1", prerelease=True), pk, current="0.18.0") is None
    # Einstellung
    assert updater.channel() == "stable"
    updater.set_channel("beta")
    assert updater.channel() == "beta"
    updater.set_channel("nonsense")
    assert updater.channel() == "stable"


def test_check_uses_channel(home, monkeypatch):
    urls = []

    listing = [release("9.1.0"), release("9.2.0-beta.1", prerelease=True), release("9.3.0", draft=True)]

    def fake(url, timeout):
        urls.append(url)
        return listing if url == updater.RELEASES_URL else release("9.1.0")

    monkeypatch.setattr(updater, "_get_json", fake)
    assert updater.check({"streamlin": "0"}, kind="deb").version == "9.1.0"
    updater.set_channel("beta")
    assert updater.check({"streamlin": "0"}, kind="deb").version == "9.2.0-beta.1"
    assert urls == [updater.API_URL, updater.RELEASES_URL]


def test_rollback_lists_older_versions_newest_first():
    ups = updater.earlier_versions(_releases(), {"streamlin": "0.19.0", "lindeck": "0.19.0"}, current="0.19.0")
    assert [u.version for u in ups] == ["0.18.0", "0.17.0"]  # 0.16.0 hat kein streamlin-Paket
    assert ups[0].date == "2026-09-01"
    assert [f["name"] for f in ups[0].files] == ["lindeck_0.18.0_all.deb", "streamlin_0.18.0_all.deb"]
    assert ups[1].downgrade  # 0.17.0 ist älter als die laufende Version
    with_beta = updater.earlier_versions(_releases(), {"lindeck": "0"}, current="0.19.0", beta=True)
    assert [u.version for u in with_beta] == ["0.19.0-beta.1", "0.18.0", "0.17.0", "0.16.0"]


def test_rpm_assets_are_picked():
    rel = release("9.1.0", kinds=("deb", "rpm"))
    up = updater.release_update(rel, {"linewave": "0.18.0"}, current="0.18.0", kind="rpm")
    assert [f["name"] for f in up.files] == ["linewave-9.1.0-1.noarch.rpm"] and up.kind == "rpm"
    assert updater.release_update(release("9.1.0"), {"linewave": "0"}, current="0.18.0", kind="rpm") is None
    beta = release("9.2.0-beta.1", kinds=("rpm",), prerelease=True)
    for a in beta["assets"]:  # rpm kennt kein „-“ in der Version: 9.2.0~beta.1
        a["name"] = a["name"].replace("9.2.0-beta.1-1", "9.2.0~beta.1-1")
    up = updater.release_update(beta, {"linewave": "0"}, current="0.18.0", kind="rpm", beta=True)
    assert up.files[0]["name"] == "linewave-9.2.0~beta.1-1.noarch.rpm"


def _fake_tools(monkeypatch, tools, outputs):
    monkeypatch.setattr(updater.shutil, "which", lambda n: f"/usr/bin/{n}" if n in tools else None)
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=outputs.get(cmd[0], ""), stderr="")

    monkeypatch.setattr(updater.subprocess, "run", run)
    monkeypatch.setattr(updater, "_runs_from_opt", lambda: True)
    return calls


def test_rpm_detection(monkeypatch):
    calls = _fake_tools(monkeypatch, {"rpm"}, {"rpm": "lindeck\t0.18.0\npackage linewave is not installed\n"
                                                      "package streamlin is not installed\n"})
    assert updater.detect_installed() == ("rpm", {"lindeck": "0.18.0"})
    assert calls[0][:3] == ["rpm", "-q", "--qf"]
    # dpkg kennt die Pakete: dpkg hat Vorrang
    _fake_tools(monkeypatch, {"rpm", "dpkg-query"}, {"dpkg-query": "streamlin\tinstall ok installed\t0.18.0\n",
                                                     "rpm": "streamlin\t0.1\n"})
    assert updater.detect_installed() == ("deb", {"streamlin": "0.18.0"})
    # dpkg vorhanden, kennt sie aber nicht (Fedora mit dpkg): rpm
    _fake_tools(monkeypatch, {"rpm", "dpkg-query"}, {"dpkg-query": "", "rpm": "streamlin\t0.18.0\n"})
    assert updater.detect_installed() == ("rpm", {"streamlin": "0.18.0"})
    _fake_tools(monkeypatch, set(), {})
    assert updater.detect_installed() == (None, {})


def test_rpm_install_command(monkeypatch, tmp_path):
    f = tmp_path / "streamlin-9.1.0-1.noarch.rpm"
    monkeypatch.setattr(updater.shutil, "which", lambda n: f"/usr/bin/{n}")
    assert updater.install_command([f], "rpm") == ["/usr/bin/pkexec", "/usr/bin/dnf", "install", "-y", str(f)]
    assert updater.install_command([f], "rpm", downgrade=True) == \
        ["/usr/bin/pkexec", "/usr/bin/dnf", "downgrade", "-y", "--allowerasing", str(f)]
    monkeypatch.setattr(updater.shutil, "which", lambda n: None if n == "dnf" else f"/usr/bin/{n}")
    assert updater.install_command([f], "rpm") == ["/usr/bin/pkexec", "/usr/bin/rpm", "-U", str(f)]
    assert updater.install_command([f], "rpm", downgrade=True) == \
        ["/usr/bin/pkexec", "/usr/bin/rpm", "-U", "--oldpackage", str(f)]
    deb = updater.install_command([tmp_path / "a.deb"], "deb", downgrade=True)
    assert "--allow-downgrades" in deb


def test_start_install_passes_kind(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(updater, "install_command", lambda files, kind, downgrade: seen.append((kind, downgrade)))
    assert updater.start_install([tmp_path / "x.rpm"], "0.17.0", kind="rpm", downgrade=True) is None
    assert seen == [("rpm", True)]


def test_flatpak_never_updates(monkeypatch):
    calls = _fake_tools(monkeypatch, {"dpkg-query"}, {"dpkg-query": "streamlin\tinstall ok installed\t0.18.0\n"})
    assert updater.detect_installed()[0] == "deb"
    monkeypatch.setenv("FLATPAK_ID", "io.github.Streamlin")
    assert updater.in_flatpak()
    calls.clear()
    assert updater.detect_installed() == (None, {}) and not calls
    assert updater.check() is None
    monkeypatch.delenv("FLATPAK_ID")
    monkeypatch.setattr(updater.os.path, "exists", lambda p: p == "/.flatpak-info")
    assert updater.in_flatpak()


def test_download_removes_old_rpms(home, monkeypatch):
    (home / "dl").mkdir()
    (home / "dl" / "old-1.noarch.rpm").write_bytes(b"x")
    up = updater.release_update(release(data=b"good", kinds=("rpm",)), {"streamlin": "0"}, current="0.16.0",
                                kind="rpm")
    _serve(monkeypatch, b"good")
    files = updater.download(up, home / "dl")
    assert [f.name for f in (home / "dl").iterdir()] == [files[0].name]


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
    monkeypatch.setattr(updater, "start_install", lambda files, v, **kw: subprocess.Popen(
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
    monkeypatch.setattr(updater, "start_install", lambda files, v, **kw: subprocess.Popen(
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
    # Beta-Kanal
    assert not dlg.beta.get()
    dlg.beta.set(True)
    dlg._toggle_beta()
    assert updater.channel() == "beta"
    dlg.destroy()


def test_settings_support_report(root, monkeypatch, home):
    from streamlin import settingsui, support
    monkeypatch.setattr(support, "build_report", lambda app="": f"report from {app}\n")
    asked, opened = [], []
    monkeypatch.setattr(settingsui.messagebox, "askyesno", lambda *a, **k: asked.append(a) or True)
    monkeypatch.setattr(settingsui.system, "run_bg", lambda cmd: opened.append(cmd))
    dlg = settingsui.open_settings(root, "Linewave", lambda: None)
    (home / "out").mkdir()
    dlg._support_report(home / "out")  # Ordner: Dateiname wird vorgeschlagen
    assert _pump(root, lambda: asked)
    [report] = (home / "out").glob("streamlin-support-*.txt")
    assert report.read_text() == "report from Linewave\n"
    assert opened == [["xdg-open", str(home / "out")]]
    assert str(report) in dlg.report_status.cget("text")
    assert dlg.report_btn.enabled
    dlg.destroy()


def test_settings_flatpak(root, monkeypatch, home):
    from streamlin import settingsui, updateui
    monkeypatch.setenv("FLATPAK_ID", "io.github.Streamlin")
    shown = []
    monkeypatch.setattr(updateui.messagebox, "showinfo", lambda title, text, parent=None: shown.append(text))
    dlg = settingsui.open_settings(root, "LinDeck", lambda: None)
    assert dlg.auto_update is None and dlg.beta is None  # keine Update-Schalter in Flatpak
    assert updateui.choose_earlier(dlg, "LinDeck", lambda: None) is None
    assert "Flatpak" in shown[0]
    dlg.destroy()


def test_rollback_dialog(root, monkeypatch, home):
    from streamlin import updateui
    ups = updater.earlier_versions(_releases(), {"streamlin": "0"}, current="0.19.0")
    installs, restarted = [], []

    def start_install(files, version, kind="deb", downgrade=False):
        installs.append((version, kind, downgrade))
        return subprocess.Popen(["true"], stdout=subprocess.PIPE, text=True)

    monkeypatch.setattr(updater, "download", lambda u, progress=None: [home / "x.deb"])
    monkeypatch.setattr(updater, "start_install", start_install)
    dlg = updateui.RollbackDialog(root, "LinDeck", lambda: restarted.append(1), load=lambda: ups)
    assert _pump(root, lambda: dlg.cards)
    assert [c.title for c in dlg.cards] == ["Version 0.18.0", "Version 0.17.0"]
    assert dlg.selected is ups[0]
    dlg.select(ups[1])
    up = dlg._continue()
    assert not dlg.winfo_exists() and up.rollback
    assert not up.skip_btn.winfo_manager() and not up.auto.master.winfo_manager()
    up._install()
    assert _pump(root, lambda: restarted)
    assert installs == [("0.17.0", "deb", True)]


def test_rollback_dialog_error_and_empty(root, home):
    from streamlin import updateui

    def fail():
        raise OSError("offline")

    dlg = updateui.RollbackDialog(root, "LinDeck", lambda: None, load=fail)
    assert _pump(root, lambda: "offline" in dlg.status.cget("text"))
    assert dlg._continue() is None
    dlg.destroy()
    dlg = updateui.RollbackDialog(root, "LinDeck", lambda: None, load=lambda: [])
    assert _pump(root, lambda: dlg.status.cget("text") != updateui._("Loading the list of versions…"))
    assert not dlg.cards
    dlg.destroy()
