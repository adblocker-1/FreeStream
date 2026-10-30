//! Tests: Sichern und Wiederherstellen in Testordnern, Prüfungen, Austausch mit der Python-Version.

use super::*;
use crate::updater::tests::{python, repo_root};
use std::collections::BTreeMap;
use std::process::Command;

/// Ordner wie unter XDG: <base>/config/freestream usw.
fn roots_in(base: &Path) -> Roots {
    let r = |s: &str| {
        let p = base.join(s).join("freestream");
        std::fs::create_dir_all(&p).unwrap();
        p
    };
    Roots::new(&r("config"), &r("data"), &r("state"))
}

fn write(p: &Path, data: &[u8]) {
    std::fs::create_dir_all(p.parent().unwrap()).unwrap();
    std::fs::write(p, data).unwrap();
}

/// Beispieldaten: Einstellungen, Profile mit Bildern, Plugin, Links, Dinge, die nie gesichert werden.
fn fill(roots: &Roots, outside: &Path) {
    write(&roots.config.join("config.json"), br#"{"brightness": 55, "language": "de"}"#);
    let prof = roots.data.join("profiles/ABC.sdProfile");
    write(&prof.join("manifest.json"), "{\"Name\": \"Spiele – ü\"}".as_bytes());
    write(&prof.join("Profiles/P1/Images/key.png"), &(0..=255u8).cycle().take(70_000).collect::<Vec<_>>());
    write(&roots.data.join("profiles/Ohne Name.sdProfile/manifest.json"), b"{}");
    write(&roots.data.join("resources/sound.wav"), b"RIFF....");
    write(&roots.data.join("plugins/com.example.sdPlugin/manifest.json"), br#"{"Name": "Beispiel"}"#);
    write(&roots.data.join("plugin-data/com.example/settings.json"), b"{}");
    std::fs::set_permissions(roots.data.join("resources/sound.wav"), std::fs::Permissions::from_mode(0o640)).unwrap();
    // Links im Bereich: relativ und absolut (wird relativ gespeichert)
    std::os::unix::fs::symlink("sound.wav", roots.data.join("resources/alias.wav")).unwrap();
    std::os::unix::fs::symlink(roots.data.join("profiles"), roots.data.join("resources/profiles-link")).unwrap();
    // nie gesichert
    write(&outside.join("secret.txt"), b"x");
    std::os::unix::fs::symlink(outside.join("secret.txt"), roots.data.join("resources/outside.txt")).unwrap();
    write(&roots.data.join("cache/thumb.png"), b"c");
    write(&roots.data.join("resources/half.tmp"), b"t");
    write(&roots.data.join("profiles/__pycache__/x.pyc"), b"p");
    write(&roots.state.join("state.json"), br#"{"SERIAL": {"profile": "ABC"}}"#);
    write(&roots.state.join("wavemic.json"), br#"{"lowcut": true}"#);
    write(&roots.state.join("freestream.log"), b"log");
}

/// Was nach dem Wiederherstellen da sein muss (ohne die nie gesicherten Dinge).
fn skipped(rel: &str) -> bool {
    rel.contains("cache/") || rel.ends_with(".tmp") || rel.contains("__pycache__") || rel.ends_with("outside.txt")
        || rel.ends_with(".log") || rel.contains("/backups/") || rel.contains(STAGE)
}

/// Gleicher Inhalt? Bei Unterschieden nur die Namen zeigen.
fn assert_same(a: &BTreeMap<String, Entry>, b: &BTreeMap<String, Entry>) {
    let diff: Vec<_> = a.keys().chain(b.keys()).filter(|k| a.get(*k) != b.get(*k))
        .map(|k| format!("{k}: {:?} / {:?}", a.get(k).map(short), b.get(k).map(short))).collect();
    assert!(diff.is_empty(), "{diff:#?}");
}

fn short(e: &Entry) -> String {
    match e {
        Entry::File(d, m) => format!("{} Bytes, {m:o}", d.len()),
        Entry::Link(t) => format!("-> {t}"),
    }
}

#[derive(Debug, PartialEq, Eq)]
enum Entry {
    File(Vec<u8>, u32),
    Link(String),
}

fn snapshot(roots: &Roots) -> BTreeMap<String, Entry> {
    // Links: Ziel im Bereich (absolute Links werden als relative gesichert)
    fn walk(root: &Path, dir: &Path, prefix: &str, out: &mut BTreeMap<String, Entry>) {
        let Ok(rd) = std::fs::read_dir(dir) else { return };
        for e in rd.flatten() {
            let rel = format!("{prefix}/{}", e.file_name().to_string_lossy());
            let t = e.file_type().unwrap();
            if t.is_symlink() {
                let target = realpath(&e.path());
                let t = target.strip_prefix(realpath(root)).map(posix).unwrap_or_else(|_| target.display().to_string());
                out.insert(rel, Entry::Link(t));
            } else if t.is_dir() {
                walk(root, &e.path(), &rel, out);
            } else {
                let mode = e.metadata().unwrap().permissions().mode() & 0o777;
                out.insert(rel, Entry::File(std::fs::read(e.path()).unwrap(), mode));
            }
        }
    }
    let mut out = BTreeMap::new();
    walk(&roots.config, &roots.config, "config", &mut out);
    walk(&roots.data, &roots.data, "data", &mut out);
    for n in STATE_FILES {
        if let Ok(b) = std::fs::read(roots.state.join(n)) {
            out.insert(format!("state/{n}"), Entry::File(b, 0o644));
        }
    }
    out.retain(|k, _| !skipped(k));
    out
}

/// Ordner leeren und etwas Fremdes hineinlegen (muss nach dem Wiederherstellen weg sein).
fn wipe(roots: &Roots) {
    for d in [&roots.config, &roots.data] {
        std::fs::remove_dir_all(d).unwrap();
        std::fs::create_dir_all(d).unwrap();
    }
    for n in STATE_FILES {
        let _ = std::fs::remove_file(roots.state.join(n));
    }
    write(&roots.data.join("profiles/NEU.sdProfile/manifest.json"), b"{}");
    write(&roots.config.join("config.json"), b"{}");
}

#[test]
fn round_trip() {
    let tmp = tempfile::tempdir().unwrap();
    let roots = roots_in(&tmp.path().join("home"));
    fill(&roots, &tmp.path().join("outside"));
    let before = snapshot(&roots);
    assert!(before.contains_key("data/resources/alias.wav"));

    let dest = tmp.path().join("out/meine-sicherung");
    let mut steps = Vec::new();
    let m = create_from(&roots, &dest, true, &mut |d, t| steps.push((d, t))).unwrap();
    let path = PathBuf::from(m["path"].as_str().unwrap());
    assert!(path.to_string_lossy().ends_with("meine-sicherung.freestream-backup"));
    assert!(!tmp.path().join("out/.meine-sicherung.freestream-backup.part").exists());
    assert_eq!(steps.last().map(|s| s.0), Some(m["contents"]["bytes"].as_u64().unwrap()));
    assert_eq!(m["contents"]["profiles"], json!(["Spiele – ü", "Ohne Name"]));
    assert_eq!(m["contents"]["plugins"], json!(["Beispiel"]));
    assert_eq!(m["contents"]["config"], json!(true));
    assert_eq!(m["links"]["data/resources/alias.wav"], json!("sound.wav"));
    assert_eq!(m["links"]["data/resources/profiles-link"], json!("../profiles"));
    let files: Vec<&str> = m["files"].as_array().unwrap().iter().map(|v| v.as_str().unwrap()).collect();
    assert!(files.contains(&"state/state.json") && files.contains(&"config/config.json"));
    assert!(!files.iter().any(|f| skipped(f)), "{files:?}");

    let checked = inspect(&path).unwrap();
    assert_eq!(describe(&checked).len(), 6);

    wipe(&roots);
    let (_, safety) = restore_to(&roots, &path, &mut |_, _| {}, true).unwrap();
    assert_same(&snapshot(&roots), &before);
    // Sicherheitskopie mit dem Stand vor dem Wiederherstellen
    let safety = safety.unwrap();
    assert!(safety.starts_with(roots.safety_dir()));
    let sm = inspect(&safety).unwrap();
    assert_eq!(sm["contents"]["profiles"], json!(["NEU"]));
    // keine Zwischenordner übrig
    for d in [&roots.config, &roots.data] {
        assert!(!std::fs::read_dir(d).unwrap().flatten().any(|e| e.file_name().to_string_lossy().starts_with(STAGE)));
    }
}

#[test]
fn without_plugins_keeps_installed_plugins() {
    let tmp = tempfile::tempdir().unwrap();
    let roots = roots_in(tmp.path());
    fill(&roots, &tmp.path().join("outside"));
    let m = create_from(&roots, &tmp.path().join("b"), false, &mut |_, _| {}).unwrap();
    assert_eq!(m["plugins_included"], json!(false));
    assert!(!m["files"].as_array().unwrap().iter().any(|f| f.as_str().unwrap().starts_with("data/plugins/")));
    write(&roots.data.join("plugins/other.sdPlugin/manifest.json"), b"{}");
    restore_to(&roots, Path::new(m["path"].as_str().unwrap()), &mut |_, _| {}, false).unwrap();
    assert!(roots.data.join("plugins/other.sdPlugin/manifest.json").exists());
    assert!(!roots.safety_dir().exists());
}

#[test]
fn keeps_only_three_safety_copies() {
    let tmp = tempfile::tempdir().unwrap();
    let roots = roots_in(tmp.path());
    write(&roots.config.join("config.json"), b"{}");
    for _ in 0..5 {
        safety_backup(&roots, true).unwrap();
    }
    assert_eq!(std::fs::read_dir(roots.safety_dir()).unwrap().count(), KEEP_SAFETY);
}

/// Zip mit eigenem Inhalt (für die Prüfungen).
fn zip_with(path: &Path, manifest: &Value, entries: &[(&str, &[u8])]) {
    use zip::write::SimpleFileOptions;
    let mut z = zip::ZipWriter::new(File::create(path).unwrap());
    z.start_file(MANIFEST, SimpleFileOptions::default()).unwrap();
    z.write_all(manifest.to_string().as_bytes()).unwrap();
    for (n, data) in entries {
        z.start_file(*n, SimpleFileOptions::default()).unwrap();
        z.write_all(data).unwrap();
    }
    z.finish().unwrap();
}

#[test]
fn rejects_bad_backups() {
    let tmp = tempfile::tempdir().unwrap();
    let f = tmp.path().join("x.freestream-backup");
    let not_backup = not_backup().0;
    std::fs::write(&f, b"kein zip").unwrap();
    assert_eq!(inspect(&f).unwrap_err().0, not_backup);

    // neuere Version
    zip_with(&f, &json!({"format": FORMAT, "format_version": 2, "files": []}), &[]);
    let e = inspect(&f).unwrap_err().0;
    assert_ne!(e, not_backup);
    assert!(e.contains("FreeStream"));
    // alte Sicherung (Streamlin) wird angenommen
    zip_with(&f, &json!({"format": "streamlin-backup", "format_version": 1, "files": ["data/a"]}), &[("data/a", b"1")]);
    assert!(inspect(&f).unwrap()["total_bytes"].as_u64().unwrap() > 1, "mit manifest.json");
    // fremdes Format
    zip_with(&f, &json!({"format": "other", "format_version": 1, "files": []}), &[]);
    assert_eq!(inspect(&f).unwrap_err().0, not_backup);
    // unsichere Pfade
    for bad in ["../evil", "data/../../evil", "/etc/passwd", "state/other.json", "other/x", "data\\x", "c:/x"] {
        zip_with(&f, &json!({"format": FORMAT, "format_version": 1, "files": [bad]}), &[(bad, b"1")]);
        assert_eq!(inspect(&f).unwrap_err(), unsafe_path(bad), "{bad}");
    }
    // Liste passt nicht
    zip_with(&f, &json!({"format": FORMAT, "format_version": 1, "files": []}), &[("data/a", b"1")]);
    assert_eq!(inspect(&f).unwrap_err(), incomplete());
    // Link nach außerhalb
    let m = json!({"format": FORMAT, "format_version": 1, "files": [], "links": {"data/a/l": "../../x"}});
    zip_with(&f, &m, &[]);
    assert_eq!(inspect(&f).unwrap_err(), unsafe_path("data/a/l"));
    let m = json!({"format": FORMAT, "format_version": 1, "files": [], "links": {"data/a/l": "../b"}});
    zip_with(&f, &m, &[]);
    assert!(inspect(&f).is_ok());
}

#[test]
fn rejects_link_chains_leaving_the_area() {
    // jeder Link für sich bleibt im Bereich, zusammen zeigen sie hinaus
    let tmp = tempfile::tempdir().unwrap();
    let roots = roots_in(tmp.path());
    let f = tmp.path().join("chain.freestream-backup");
    let m = json!({"format": FORMAT, "format_version": 1, "files": [],
                   "links": {"data/sub/d": "..", "data/sub/e": "d/../.."}});
    zip_with(&f, &m, &[]);
    assert!(inspect(&f).is_ok());
    let e = restore_to(&roots, &f, &mut |_, _| {}, false).unwrap_err();
    assert_eq!(e, unsafe_path("data/sub/e"));
}

#[test]
fn helpers() {
    assert_eq!(human_size(512), "512 B");
    assert_eq!(human_size(1536), "1.5 KB");
    assert_eq!(human_size(5 << 30), "5.0 GB");
    assert_eq!(human_size(3 << 40), "3072.0 GB");
    assert_eq!(format_created("2026-10-30T16:45:12+01:00").as_deref(), Some("2026-10-30 16:45"));
    assert_eq!(format_created("kaputt"), None);
    assert!(safe_name("data/a/./b") && safe_name("manifest.json") && !safe_name("data/") && !safe_name("data"));
    assert!(link_ok("data/a/b/l", "../x") && link_ok("data/a/l", "..") && !link_ok("data/l", ".."));
    assert!(default_name().starts_with("freestream-") && default_name().ends_with(EXT));
    assert_eq!(relpath(Path::new("/a/b/c"), Path::new("/a/d")), "../b/c");
    assert_eq!(relpath(Path::new("/a"), Path::new("/a")), ".");
}

// ------------------------------------------------------------------ Austausch mit Python
fn run_python(py: &str, base: &Path, code: &str) {
    let script = format!("import sys; sys.path.insert(0, {:?}); from freestream import backup\n{code}",
                         repo_root().display().to_string());
    let out = Command::new(py)
        .args(["-I", "-c", &script])
        .env("HOME", base)
        .env("XDG_CONFIG_HOME", base.join("config"))
        .env("XDG_DATA_HOME", base.join("data"))
        .env("XDG_STATE_HOME", base.join("state"))
        .env("FREESTREAM_LANG", "en")
        .output()
        .unwrap();
    assert!(out.status.success(), "{}\n{}", String::from_utf8_lossy(&out.stdout), String::from_utf8_lossy(&out.stderr));
}

#[test]
fn python_backup_restores_in_rust() {
    let Some(py) = python() else { return };
    let tmp = tempfile::tempdir().unwrap();
    let a = tmp.path().join("a");
    let roots_a = roots_in(&a);
    fill(&roots_a, &tmp.path().join("outside"));
    let dest = tmp.path().join("py.freestream-backup");
    run_python(py, &a, &format!("backup.create({:?})", dest.display().to_string()));

    let m = inspect(&dest).unwrap();
    // dasselbe Manifest wie von Rust (bis auf Zeit, Version und Reihenfolge der Dateien)
    let ours = create_from(&roots_a, &tmp.path().join("rs"), true, &mut |_, _| {}).unwrap();
    for key in ["format", "format_version", "plugins_included", "contents", "links", "host"] {
        assert_eq!(m[key], ours[key], "{key}");
    }
    let set = |v: &Value| v.as_array().unwrap().iter().map(|x| x.as_str().unwrap().to_string()).collect::<BTreeSet<_>>();
    assert_eq!(set(&m["files"]), set(&ours["files"]));
    let roots_b = roots_in(&tmp.path().join("b"));
    restore_to(&roots_b, &dest, &mut |_, _| {}, false).unwrap();
    assert_same(&snapshot(&roots_b), &snapshot(&roots_a));
}

#[test]
fn rust_backup_restores_in_python() {
    let Some(py) = python() else { return };
    let tmp = tempfile::tempdir().unwrap();
    let roots_a = roots_in(&tmp.path().join("a"));
    fill(&roots_a, &tmp.path().join("outside"));
    let m = create_from(&roots_a, &tmp.path().join("rs"), true, &mut |_, _| {}).unwrap();
    let path = m["path"].as_str().unwrap().to_string();

    let b = tmp.path().join("b");
    let roots_b = roots_in(&b);
    wipe(&roots_b);
    run_python(py, &b, &format!(
        "m = backup.inspect({path:?})\nassert m['freestream_version'] == {VERSION:?}, m\n\
         print('\\n'.join(backup.describe(m)))\nbackup.restore({path:?}, safety=False)"));
    assert_same(&snapshot(&roots_b), &snapshot(&roots_a));
}
