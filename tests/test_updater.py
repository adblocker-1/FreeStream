"""Automatische Updates: Release prüfen, herunterladen, installieren, abschalten."""
import hashlib
import io
import subprocess
import sys

import pytest

from freestream import paths, updater

PREFIX = updater.DOWNLOAD_PREFIX


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    for name, sub in (("CONFIG_DIR", "cfg"), ("DATA_DIR", "data"), ("STATE_DIR", "state"),
                      ("PROFILES_DIR", "data/profiles"), ("RESOURCES_DIR", "data/res")):
        monkeypatch.setattr(paths, name, tmp_path / sub)
    monkeypatch.setattr(paths, "CONFIG_FILE", tmp_path / "cfg" / "config.json")
    monkeypatch.delenv("FREESTREAM_NO_UPDATE_CHECK", raising=False)
    return tmp_path


def _asset(p, version):
    return f"{p}_{version}_all.deb"


def release(version="9.1.0", pkgs=("freestream", "freedeck", "freewave"), data=b"deb", **extra):
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    rel = {"tag_name": f"v{version}", "html_url": "https://github.com/x/releases/tag/v" + version,
           "body": "## New\n\n**English**\n\n- Better things\n\n---\n\n**Deutsch**\n\n- Bessere Dinge\n",
           "assets": [{"name": _asset(p, version), "size": len(data), "digest": digest,
                       "browser_download_url": f"{PREFIX}v{version}/{_asset(p, version)}"}
                      for p in pkgs]}
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
    up = updater.release_update(release(), {"freedeck": "0.16.0", "freewave": "0.16.0"}, current="0.16.0")
    assert up.version == "9.1.0"
    assert [f["name"] for f in up.files] == ["freedeck_9.1.0_all.deb", "freewave_9.1.0_all.deb"]
    assert up.notes == "## New\n\n- Better things"


def test_no_update_when_not_newer_missing_or_unsafe():
    pk = {"freestream": "0.16.0"}
    assert updater.release_update(release("0.16.0"), pk, current="0.16.0") is None
    assert updater.release_update(release(pkgs=("freedeck",)), pk, current="0.16.0") is None
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
    monkeypatch.setenv("FREESTREAM_NO_UPDATE_CHECK", "1")
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
    up = updater.release_update(release(data=b"good"), {"freestream": "0"}, current="0.16.0")
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


# ------------------------------------------------------------------ Beta, Zurückstufen
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
            release("0.16.0", pkgs=("freedeck",)),
            {"tag_name": "", "draft": False}, "garbage"]


def test_release_list_parsing():
    assert [r["tag_name"] for r in updater.published(_releases())] == \
        ["v0.19.0-beta.1", "v0.18.0", "v0.17.0", "v0.16.0"]
    assert [r["tag_name"] for r in updater.published(_releases(), beta=False)] == ["v0.18.0", "v0.17.0", "v0.16.0"]


def test_beta_channel_takes_newest_prerelease_never_draft(home):
    pk = {"freestream": "0.18.0"}
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
    assert updater.check({"freestream": "0"}).version == "9.1.0"
    updater.set_channel("beta")
    assert updater.check({"freestream": "0"}).version == "9.2.0-beta.1"
    assert urls == [updater.API_URL, updater.RELEASES_URL]


def test_rollback_lists_older_versions_newest_first():
    ups = updater.earlier_versions(_releases(), {"freestream": "0.19.0", "freedeck": "0.19.0"}, current="0.19.0")
    assert [u.version for u in ups] == ["0.18.0", "0.17.0"]  # 0.16.0 hat kein freestream-Paket
    assert ups[0].date == "2026-09-01"
    assert [f["name"] for f in ups[0].files] == ["freedeck_0.18.0_all.deb", "freestream_0.18.0_all.deb"]
    assert ups[1].downgrade  # 0.17.0 ist älter als die laufende Version
    with_beta = updater.earlier_versions(_releases(), {"freedeck": "0"}, current="0.19.0", beta=True)
    assert [u.version for u in with_beta] == ["0.19.0-beta.1", "0.18.0", "0.17.0", "0.16.0"]


def test_prerelease_assets_with_tilde():
    beta = release("9.2.0-beta.1", prerelease=True)
    for a in beta["assets"]:  # dpkg-Version auch mit „~“: 9.2.0~beta.1
        a["name"] = a["name"].replace("9.2.0-beta.1_", "9.2.0~beta.1_")
    up = updater.release_update(beta, {"freewave": "0"}, current="0.18.0", beta=True)
    assert up.files[0]["name"] == "freewave_9.2.0~beta.1_all.deb"


def _fake_tools(monkeypatch, tools, outputs):
    monkeypatch.setattr(updater.shutil, "which", lambda n: f"/usr/bin/{n}" if n in tools else None)
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=outputs.get(cmd[0], ""), stderr="")

    monkeypatch.setattr(updater.subprocess, "run", run)
    monkeypatch.setattr(updater, "_runs_from_opt", lambda: True)
    return calls


def test_dpkg_detection(monkeypatch):
    calls = _fake_tools(monkeypatch, {"dpkg-query"}, {"dpkg-query": "freestream\tinstall ok installed\t0.18.0\n"
                                                                    "freedeck\tdeinstall ok config-files\t0.17.0\n"})
    assert updater.installed_packages() == {"freestream": "0.18.0"}
    assert calls[0][:2] == ["dpkg-query", "-W"]
    _fake_tools(monkeypatch, set(), {})
    assert updater.installed_packages() == {}
    # nicht aus /opt/<Paket>: dpkg wird gar nicht gefragt
    calls = _fake_tools(monkeypatch, {"dpkg-query"}, {"dpkg-query": "freestream\tinstall ok installed\t0.18.0\n"})
    monkeypatch.setattr(updater, "_runs_from_opt", lambda: False)
    assert updater.installed_packages() == {} and not calls


def test_install_command_allows_downgrades(monkeypatch, tmp_path):
    monkeypatch.setattr(updater.shutil, "which", lambda n: f"/usr/bin/{n}")
    cmd = updater.install_command([tmp_path / "a.deb"])
    assert "--allow-downgrades" in cmd
    assert cmd[cmd.index("Dpkg::Options::=--force-confdef") - 1] == "-o"
    assert "Dpkg::Options::=--force-confold" in cmd


def test_start_install_without_pkexec(monkeypatch, tmp_path, home):
    seen = []
    monkeypatch.setattr(updater, "install_command", lambda files: seen.append(files))
    assert updater.start_install([tmp_path / "x.deb"], "0.17.0") is None
    assert seen == [[tmp_path / "x.deb"]] and "installing" not in updater.load_state()


def test_download_removes_old_debs(home, monkeypatch):
    (home / "dl").mkdir()
    (home / "dl" / "old_1_all.deb").write_bytes(b"x")
    up = updater.release_update(release(data=b"good"), {"freestream": "0"}, current="0.16.0")
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
    from freestream import updateui
    up = updater.release_update(release(), {"freestream": "0"}, current="0.16.0")
    monkeypatch.setattr(updater, "download", lambda u, progress=None: (progress(1, 1), [home / "x.deb"])[1])
    monkeypatch.setattr(updater, "start_install", lambda files, v, **kw: subprocess.Popen(
        ["true"], stdout=subprocess.PIPE, text=True))
    restarted = []
    dlg = updateui.UpdateDialog(root, up, "FreeDeck", lambda: restarted.append(1), automatic=True)
    assert dlg.wm_transient() == ""  # unsichtbar gestartet: eigenes Fenster
    dlg._install()
    assert _pump(root, lambda: restarted)


def test_dialog_install_cancelled(root, monkeypatch, home):
    from freestream import updateui
    up = updater.release_update(release(), {"freestream": "0"}, current="0.16.0")
    monkeypatch.setattr(updater, "download", lambda u, progress=None: [home / "x.deb"])
    monkeypatch.setattr(updater, "start_install", lambda files, v, **kw: subprocess.Popen(
        ["sh", "-c", "exit 126"], stdout=subprocess.PIPE, text=True))
    dlg = updateui.UpdateDialog(root, up, "FreeDeck", lambda: None)
    dlg._install()
    assert _pump(root, lambda: not dlg.busy)
    assert "abgebrochen" in dlg.status.cget("text")
    assert dlg.install_btn.winfo_manager()


def test_dialog_later_and_skip(root, home):
    from freestream import updateui
    up = updater.release_update(release(), {"freestream": "0"}, current="0.16.0")
    lock = updater.PromptLock()
    assert lock.acquire()
    dlg = updateui.UpdateDialog(root, up, "FreeWave", lambda: None, lock=lock, automatic=True)
    dlg.auto.set(False)
    dlg._toggle_auto()
    assert not updater.enabled()
    dlg._later()
    assert updater.suppressed("9.1.0")
    assert updater.PromptLock().acquire()  # Sperre wieder frei
    dlg = updateui.UpdateDialog(root, up, "FreeWave", lambda: None)
    dlg._skip()
    assert updater.load_state()["skipped"] == "9.1.0"


def test_settings_dialog(root, monkeypatch, home):
    from freestream import i18n, settingsui
    restarts, asked = [], []
    monkeypatch.setattr(settingsui, "choose_language", lambda code, app, restart, parent: asked.append(code))
    dlg = settingsui.open_settings(root, "FreeDeck", lambda: restarts.append(1))
    assert settingsui.open_settings(root, "FreeDeck", lambda: None) is dlg  # nur ein Fenster
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
    from freestream import settingsui, support
    monkeypatch.setattr(support, "build_report", lambda app="": f"report from {app}\n")
    asked, opened = [], []
    monkeypatch.setattr(settingsui.messagebox, "askyesno", lambda *a, **k: asked.append(a) or True)
    monkeypatch.setattr(settingsui.system, "run_bg", lambda cmd: opened.append(cmd))
    dlg = settingsui.open_settings(root, "FreeWave", lambda: None)
    (home / "out").mkdir()
    dlg._support_report(home / "out")  # Ordner: Dateiname wird vorgeschlagen
    assert _pump(root, lambda: asked)
    [report] = (home / "out").glob("freestream-support-*.txt")
    assert report.read_text() == "report from FreeWave\n"
    assert opened == [["xdg-open", str(home / "out")]]
    assert str(report) in dlg.report_status.cget("text")
    assert dlg.report_btn.enabled
    dlg.destroy()


def test_source_checkout_cannot_roll_back(root, monkeypatch, home):
    from freestream import settingsui, updateui
    shown = []
    monkeypatch.setattr(updateui.messagebox, "showinfo", lambda title, text, parent=None: shown.append(text))
    dlg = settingsui.open_settings(root, "FreeDeck", lambda: None)
    assert updateui.choose_earlier(dlg, "FreeDeck", lambda: None) is None
    assert ".deb" in shown[0] and "FreeDeck" in shown[0]
    dlg.destroy()


def test_rollback_dialog(root, monkeypatch, home):
    from freestream import updateui
    ups = updater.earlier_versions(_releases(), {"freestream": "0"}, current="0.19.0")
    installs, restarted = [], []

    def start_install(files, version):
        installs.append(version)
        return subprocess.Popen(["true"], stdout=subprocess.PIPE, text=True)

    monkeypatch.setattr(updater, "download", lambda u, progress=None: [home / "x.deb"])
    monkeypatch.setattr(updater, "start_install", start_install)
    dlg = updateui.RollbackDialog(root, "FreeDeck", lambda: restarted.append(1), load=lambda: ups)
    assert _pump(root, lambda: dlg.cards)
    assert [c.title for c in dlg.cards] == ["Version 0.18.0", "Version 0.17.0"]
    assert dlg.selected is ups[0]
    dlg.select(ups[1])
    up = dlg._continue()
    assert not dlg.winfo_exists() and up.rollback
    assert not up.skip_btn.winfo_manager() and not up.auto.master.winfo_manager()
    up._install()
    assert _pump(root, lambda: restarted)
    assert installs == ["0.17.0"]


def test_rollback_dialog_error_and_empty(root, home):
    from freestream import updateui

    def fail():
        raise OSError("offline")

    dlg = updateui.RollbackDialog(root, "FreeDeck", lambda: None, load=fail)
    assert _pump(root, lambda: "offline" in dlg.status.cget("text"))
    assert dlg._continue() is None
    dlg.destroy()
    dlg = updateui.RollbackDialog(root, "FreeDeck", lambda: None, load=lambda: [])
    assert _pump(root, lambda: dlg.status.cget("text") != updateui._("Loading the list of versions…"))
    assert not dlg.cards
    dlg.destroy()


def test_legacy_packages_get_new_names():
    # Installierte Pakete mit altem Namen (lindeck/linewave) wechseln auf die neuen
    up = updater.release_update(release(), {"lindeck": "0.16.0", "linewave": "0.16.0"}, current="0.16.0")
    assert [f["name"] for f in up.files] == ["freedeck_9.1.0_all.deb", "freewave_9.1.0_all.deb"]


def test_old_releases_with_old_asset_names_and_repo():
    old = updater.DOWNLOAD_PREFIXES[1]
    r = release("0.28.0", pkgs=("streamlin", "lindeck"))
    for a in r["assets"]:
        a["browser_download_url"] = a["browser_download_url"].replace(PREFIX, old)
    up = updater.make_update(r, {"freestream": "0", "freedeck": "0"})
    assert [f["name"] for f in up.files] == ["lindeck_0.28.0_all.deb", "streamlin_0.28.0_all.deb"]
    assert all(f["url"].startswith(old) for f in up.files)


def test_api_falls_back_to_old_repo_name(monkeypatch):
    import urllib.error
    urls = []

    def fake(url, timeout):
        urls.append(url)
        if url == updater.API_URLS[0]:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return release("9.1.0")

    monkeypatch.setattr(updater, "_get_json", fake)
    assert updater.check({"freestream": "0"}).version == "9.1.0"
    assert urls == list(updater.API_URLS)


# ------------------------------------------------------------------ Ausgabe: Python oder Rust (Beta)
def _rust_release(version="9.1.0", distros=("ubuntu-24.04", "debian-12"), data=b"deb"):
    rel = release(version, data=data)
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    for d in distros:
        name = f"freedeck-rs_{version}_amd64_{d}.deb"
        rel["assets"].append({"name": name, "size": len(data), "digest": digest,
                              "browser_download_url": f"{PREFIX}v{version}/{name}"})
    return rel


def test_edition_setting(home, monkeypatch):
    monkeypatch.delenv("FREESTREAM_EDITION", raising=False)
    assert updater.edition() == "python"
    updater.set_edition("rust")
    assert updater.edition() == "rust"
    updater.set_edition("nonsense")
    assert updater.edition() == "python"
    monkeypatch.setenv("FREESTREAM_EDITION", "rust")
    assert updater.edition() == "rust"


def test_rust_asset_names_prefer_own_system():
    names = updater.asset_names("freedeck-rs", "1.2.0", arch="amd64", distro="debian-12")
    assert names[0] == "freedeck-rs_1.2.0_amd64_debian-12.deb"
    assert "freedeck-rs_1.2.0_amd64.deb" in names
    assert updater.asset_names("freedeck", "1.2.0") == ["freedeck_1.2.0_all.deb"]


def test_update_for_other_edition(monkeypatch):
    monkeypatch.setattr(updater, "_distro", lambda: "ubuntu-24.04")
    monkeypatch.setattr(updater, "_deb_arch", lambda: "amd64")
    monkeypatch.setattr(updater, "dpkg_packages", lambda: {"freestream": "0.16.0"})
    up = updater.release_update(_rust_release(), {"freestream": "0.16.0"}, current="0.16.0")
    assert [f["name"] for f in up.files] == ["freestream_9.1.0_all.deb"]
    rust = updater.update_for_edition(up, "rust")
    assert [f["name"] for f in rust.files] == ["freedeck-rs_9.1.0_amd64_ubuntu-24.04.deb"]
    assert updater.update_for_edition(up, "python").files == up.files
    # Release ohne Rust-Paket für dieses System: keine Rust-Auswahl
    monkeypatch.setattr(updater, "_distro", lambda: "fedora-40")
    assert updater.update_for_edition(up, "rust") is None


def test_packages_for_edition():
    assert updater.packages_for("rust", {"freestream": "1"}) == ["freedeck-rs"]
    assert updater.packages_for("python", {"freedeck": "1", "freedeck-rs": "1"}) == ["freedeck"]
    assert updater.packages_for("python", {"freedeck-rs": "1"}) == ["freestream"]


def test_redirect_to_rust(home, monkeypatch):
    calls = []
    monkeypatch.setattr(updater.os, "execv", lambda exe, argv: calls.append(argv))
    monkeypatch.setattr(updater, "rust_program", lambda name: f"/usr/bin/{name}-rs")
    monkeypatch.delenv("FREESTREAM_PYTHON", raising=False)
    monkeypatch.delenv("FREESTREAM_EDITION", raising=False)
    updater.redirect_to_rust("freedeck", [])
    assert calls == []  # Python gewählt: nichts umleiten
    updater.set_edition("rust")
    updater.redirect_to_rust("freedeck", [])
    updater.redirect_to_rust("freedeck", ["run"])
    updater.redirect_to_rust("freedeck", ["profiles"])  # Kommandozeile bleibt Python
    updater.redirect_to_rust("freewave", ["--hidden"])
    updater.redirect_to_rust("freewave", ["mixer", "status"])
    assert calls == [["/usr/bin/freedeck-rs"], ["/usr/bin/freedeck-rs", "run"], ["/usr/bin/freewave-rs", "--hidden"]]
    monkeypatch.setenv("FREESTREAM_PYTHON", "1")
    updater.redirect_to_rust("freedeck", [])
    assert len(calls) == 3
