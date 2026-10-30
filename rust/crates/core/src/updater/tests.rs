//! Tests ohne Netz: Versionen, Auswahl der Pakete aus einer Beispiel-Antwort der GitHub-API, Zustand.

use super::*;

const RELEASES: &str = include_str!("releases.json");

fn releases() -> Vec<Value> {
    serde_json::from_str(RELEASES).unwrap()
}

fn ubuntu() -> Target {
    Target { arch: "amd64".into(), distro: Some("ubuntu-24.04".into()) }
}

fn debian() -> Target {
    Target { arch: "amd64".into(), distro: Some("debian-12".into()) }
}

fn versions(ups: &[Update]) -> Vec<&str> {
    ups.iter().map(|u| u.version.as_str()).collect()
}

fn names(up: &Update) -> Vec<&str> {
    up.files.iter().map(|f| f.name.as_str()).collect()
}

#[test]
fn version_parsing_and_order() {
    assert_eq!(parse_version("0.20.1-beta.2"), vec![0, 20, 1]);
    assert_eq!(parse_version("1.2.3.4.5"), vec![1, 2, 3, 4]);
    assert_eq!(parse_version(""), Vec::<u64>::new());
    // aufsteigend sortiert
    let ordered = ["0.9", "0.19.9", "0.20.0-alpha", "0.20.0-beta", "0.20.0-beta.1", "0.20.0~beta.2",
                   "0.20.0-beta.10", "0.20.0-rc1", "v0.20.0", "0.20.0.1", "0.21.0", "1.0.0"];
    for w in ordered.windows(2) {
        assert!(version_key(w[0]) < version_key(w[1]), "{} < {}", w[0], w[1]);
        assert!(is_newer(w[1], w[0]) && is_older(w[0], w[1]));
    }
    assert_eq!(version_key("V0.20.0+build5"), version_key("0.20.0"));
    assert_eq!(version_key("0.20.0-BETA.1"), version_key("0.20.0-beta.1"));
    assert!(!is_newer("garbage", "0.1.0"));
    assert!(!is_older("", "0.1.0"));
    assert!(!is_newer("0.39.0", "0.39.0"));
}

#[test]
fn version_order_matches_python() {
    let Some(py) = python() else { return };
    let samples = ["0.9", "0.20.0", "0.20.0-beta.1", "0.20.0~beta.2", "0.20.0-rc.1", "0.20.0-1a", "v1.2",
                   "1.2.0", "1.2", "0.20.0-beta-1", "0.20.0-b2c", "0.20", "2.0.0+meta", "x", "0.20.0-", "1.2.3.4.5",
                   "0.20.0-Beta.1", "0.020.0", "3-alpha", "0.39.0"];
    let script = format!(
        "import sys; sys.path.insert(0, {repo:?}); from freestream import updater as u; s = {samples:?}; \
         print('\\n'.join(str(sorted(range(len(s)), key=lambda i: (u.version_key(s[i]), i)))[1:-1].split(', ')))",
        repo = repo_root().display().to_string(),
        samples = samples.to_vec()
    );
    let out = Command::new(py).args(["-I", "-c", &script]).output().unwrap();
    assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
    let py_order: Vec<usize> = String::from_utf8_lossy(&out.stdout).lines().map(|l| l.trim().parse().unwrap()).collect();
    let mut rs_order: Vec<usize> = (0..samples.len()).collect();
    rs_order.sort_by(|a, b| version_key(samples[*a]).cmp(&version_key(samples[*b])).then(a.cmp(b)));
    assert_eq!(rs_order, py_order);
}

pub(crate) fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..").canonicalize().unwrap()
}

/// python3.12 (oder python3), wenn vorhanden.
pub(crate) fn python() -> Option<&'static str> {
    ["python3.12", "python3"].into_iter().find(|p| {
        Command::new(p).arg("-c").arg("import sys; assert sys.version_info >= (3, 10)").output()
            .map(|o| o.status.success()).unwrap_or(false)
    })
}

#[test]
fn published_and_channels() {
    let r = releases();
    let tags: Vec<&str> = published(&r, true).iter().map(|x| str_field(x, "tag_name")).collect();
    assert_eq!(tags, ["v0.41.0-beta.1", "v0.40.0", "v0.39.1", "v0.38.0", "v0.37.0", "v0.36.0", "v0.35.0", "v0.27.0"]);
    let stable: Vec<&str> = published(&r, false).iter().map(|x| str_field(x, "tag_name")).collect();
    assert_eq!(stable[0], "v0.40.0");
    assert!(!stable.contains(&"v0.42.0"), "Entwürfe nie");
}

#[test]
fn python_packages_like_python() {
    // dieselben Ergebnisse wie freestream.updater mit diesen Paketen
    let r = releases();
    let p = ["freestream", "lindeck"];
    assert_eq!(versions(&earlier_versions(&r, &p, "0.39.0", false, &ubuntu())), ["0.38.0", "0.36.0", "0.35.0", "0.27.0"]);
    let n = newest_update(&r, &p, "0.39.0", true, &ubuntu()).unwrap();
    assert_eq!(n.version, "0.41.0-beta.1");
    assert_eq!(names(&n), ["freedeck_0.41.0~beta.1_all.deb", "freestream_0.41.0~beta.1_all.deb"]);
    assert_eq!(n.date, "2026-10-20");
    assert_eq!(n.notes, "New things.\n\nMore.");
    assert!(n.prerelease);
    assert_eq!(release_update(&r[3], &p, "0.39.0", false, &ubuntu()).unwrap().version, "0.40.0");
    assert!(release_update(&r[1], &p, "0.39.0", false, &ubuntu()).is_none(), "Vorabversion nur im Beta-Kanal");
    // alte Paketnamen im alten Repository
    let old = make_update(&r[8], &p, &ubuntu()).unwrap();
    assert_eq!(names(&old), ["lindeck_0.27.0_all.deb", "streamlin_0.27.0_all.deb"]);
    assert!(old.files[0].url.starts_with("https://github.com/adblocker-1/Streamlin/"));
}

#[test]
fn rust_asset_selection() {
    let r = releases();
    let p = ["freedeck-rs", "freedeck"];
    // passendes System zuerst
    let up = release_update(&r[3], &p, "0.39.0", false, &ubuntu()).unwrap();
    assert_eq!(names(&up), ["freedeck_0.40.0_all.deb", "freedeck-rs_0.40.0_amd64_ubuntu-24.04.deb"]);
    assert_eq!(up.files[1].sha256, "cd".repeat(32));
    assert_eq!(up.files[0].sha256, "ab".repeat(32), "Prüfsumme klein geschrieben");
    assert_eq!(up.files[1].size, 6000);
    assert_eq!(up.page, "https://github.com/adblocker-1/FreeStream/releases/tag/v0.40.0");
    // anderes System: Paket ohne Systemnamen
    let up = release_update(&r[3], &p, "0.39.0", false, &debian()).unwrap();
    assert_eq!(names(&up)[1], "freedeck-rs_0.40.0_amd64.deb");
    // andere Architektur: kein Paket, kein Update
    let arm = Target { arch: "arm64".into(), distro: None };
    assert!(release_update(&r[3], &p, "0.39.0", false, &arm).is_none());
    // Vorabversion mit ~ im Dateinamen
    let beta = newest_update(&r, &["freedeck-rs"], "0.40.0", true, &ubuntu()).unwrap();
    assert_eq!(names(&beta), ["freedeck-rs_0.41.0~beta.1_amd64_ubuntu-24.04.deb"]);
    // ältere Versionen: ohne Rust-Paket (0.38, 0.27), ohne Prüfsumme (0.37), fremde Adresse (0.36) fallen weg
    assert_eq!(versions(&earlier_versions(&r, &["freedeck-rs"], "0.40.0", false, &ubuntu())), ["0.39.1", "0.35.0"]);
    assert!(newest_update(&r, &["freedeck-rs"], "0.41.0", true, &ubuntu()).is_none(), "nichts Neueres");
    assert!(release_update(&r[3], &Vec::<String>::new(), "0.39.0", false, &ubuntu()).is_none());
}

#[test]
fn asset_name_variants() {
    assert_eq!(asset_names("freedeck", "0.41.0-beta.1", &ubuntu()),
               ["freedeck_0.41.0-beta.1_all.deb", "freedeck_0.41.0~beta.1_all.deb"]);
    assert_eq!(asset_names("freedeck-rs", "0.40.0", &Target { arch: "amd64".into(), distro: None }),
               ["freedeck-rs_0.40.0_amd64.deb"]);
}

#[test]
fn notes_dpkg_and_os_release() {
    assert_eq!(english_notes("**English**\n\nA\n\n\n\nB\n---\nDeutsch"), "A\n\nB");
    assert_eq!(english_notes(""), "");
    let out = "freedeck-rs\tinstall ok installed\t0.39.0\nfreedeck\tdeinstall ok config-files\t0.30.0\n\
               lindeck\tinstall ok installed\t0.12.0\nother\tinstall ok installed\t1\n";
    let found = parse_dpkg(out);
    assert_eq!(found.into_iter().collect::<Vec<_>>(),
               [("freedeck-rs".to_string(), "0.39.0".to_string()), ("lindeck".into(), "0.12.0".into())]);
    assert_eq!(distro_from_os_release("NAME=\"Ubuntu\"\nVERSION_ID=\"24.04\"\nID=ubuntu\n").as_deref(),
               Some("ubuntu-24.04"));
    assert_eq!(distro_from_os_release("ID=arch\n"), None);
}

#[test]
fn skip_and_later() {
    let dir = tempfile::tempdir().unwrap();
    let f = dir.path().join(STATE_FILE);
    assert!(!is_suppressed(&load_state_from(&f), "0.40.0", 1000.0));
    save_state_to(&f, &[("skipped", Some(Value::String("0.40.0".into())))]).unwrap();
    let st = load_state_from(&f);
    assert!(is_suppressed(&st, "0.40.0", 1000.0));
    assert!(!is_suppressed(&st, "0.41.0", 1000.0), "nur die übersprungene Version");
    save_state_to(&f, &[("later", Some(later_entry("0.41.0", 1000.0))), ("skipped", None)]).unwrap();
    let st = load_state_from(&f);
    assert!(st.get("skipped").is_none());
    assert!(is_suppressed(&st, "0.41.0", 1000.0 + LATER_SECONDS - 1.0));
    assert!(!is_suppressed(&st, "0.41.0", 1000.0 + LATER_SECONDS));
    assert!(!is_suppressed(&st, "0.42.0", 1000.0));
    // von Python geschriebene Datei (until als Zahl mit Nachkommastellen)
    std::fs::write(&f, "{\"later\": {\"version\": \"0.5\", \"until\": 2000.5}, \"installing\": \"0.5\"}").unwrap();
    assert!(is_suppressed(&load_state_from(&f), "0.5", 2000.0));
    std::fs::write(&f, "kaputt").unwrap();
    assert!(load_state_from(&f).is_empty());
}

#[test]
fn prompt_lock_only_once() {
    let dir = tempfile::tempdir().unwrap();
    let f = dir.path().join("update.lock");
    let mut a = PromptLock::new();
    let mut b = PromptLock::new();
    assert!(a.acquire_at(&f));
    assert!(!b.acquire_at(&f));
    a.release();
    assert!(b.acquire_at(&f));
}

#[test]
fn install_messages() {
    let m = InstallOutcome::NoPkexec(PathBuf::from("/x/updates")).message("FreeDeck", "0.40.0", false);
    assert!(m.contains("/x/updates"));
    // Sprache kann in parallelen Tests wechseln: nur die Platzhalter prüfen
    let m = InstallOutcome::Installed.message("FreeDeck", "0.38.0", true);
    assert!(m.contains("0.38.0") && m.contains("FreeDeck"));
    assert!(UpdateError::Damaged("a.deb".into()).download_message().contains("(a.deb)"));
}
