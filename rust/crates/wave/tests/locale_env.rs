//! pactl-Abfragen laufen immer englisch (LC_ALL=C, ohne LANGUAGE) – sonst hieße es „Stumm: nein“ und
//! nichts würde erkannt. Schreibende Befehle behalten die Sprache des Nutzers (ihre Fehlermeldungen).
//! Eigene Testdatei: ändert PATH für den ganzen Prozess.

use std::os::unix::fs::PermissionsExt;

use freestream_wave::pactl::{Pactl, SystemPactl};

#[test]
fn queries_run_in_c_locale_writes_keep_language() {
    let dir = tempfile::tempdir().unwrap();
    let script = dir.path().join("pactl");
    std::fs::write(&script, "#!/bin/sh\n\
        if [ \"$1\" = fail ]; then echo 'Fehler: Keine solche Entität' >&2; exit 1; fi\n\
        echo \"ARGS=$*\"; echo \"LC_ALL=${LC_ALL-unset}\"; echo \"LANGUAGE=${LANGUAGE-unset}\"\n").unwrap();
    std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
    let path = format!("{}:{}", dir.path().display(), std::env::var("PATH").unwrap_or_default());
    std::env::set_var("PATH", path);
    std::env::set_var("LANGUAGE", "de");
    std::env::set_var("LC_ALL", "de_DE.UTF-8");
    let p = SystemPactl;
    assert!(p.available());
    let out = p.list("sink-inputs");
    assert!(out.contains("ARGS=list sink-inputs") && out.contains("LC_ALL=C") && out.contains("LANGUAGE=unset"), "{out}");
    let out = p.run(&["get-default-sink".into()]).unwrap();
    assert!(out.contains("LC_ALL=C") && out.contains("LANGUAGE=unset"), "{out}");
    let out = p.run(&["set-sink-mute".into(), "linewave_music".into(), "1".into()]).unwrap();
    assert!(out.contains("LC_ALL=de_DE.UTF-8") && out.contains("LANGUAGE=de"), "{out}");
    let e = p.run(&["fail".into()]).unwrap_err();
    assert_eq!(e.0, "Fehler: Keine solche Entität");
    // die eigene Umgebung bleibt unverändert
    assert_eq!(std::env::var("LANGUAGE").unwrap(), "de");
}
