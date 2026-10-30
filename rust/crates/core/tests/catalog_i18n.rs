//! Katalog und Hotkeys in verschiedenen Sprachen. Eigene Testdatei = eigener Prozess, damit der
//! Sprachwechsel keine anderen Tests stört; alles in einem Test, weil die Sprache global ist.

use freestream_core::catalog as C;
use freestream_core::i18n::set_language;
use serde_json::json;

#[test]
fn hotkeys_and_names_follow_language() {
    set_language("de");
    for text in ["Strg+Umschalt+T", "Alt+F4", "Super+E", "F13", "Strg+Num +", "Strg+Entf", "Umschalt+1", "Strg+#", "Bild↑"] {
        assert_eq!(C::format_hotkey(&C::parse_hotkey(text).unwrap()), text);
    }
    assert_eq!(C::parse_hotkey("ctrl+shift+t").unwrap(), C::parse_hotkey("Strg+Umschalt+T").unwrap());
    let hk = C::hotkey_from_tk("t", "\x14", 0x4 | 0x1, 0).unwrap();
    assert_eq!(C::hotkey_label(&hk), "Strg+Umschalt+T");
    assert_eq!(C::format_hotkey(&C::hotkey_from_tk("exclam", "!", 0x1, 10).unwrap()), "Umschalt+1");
    let err = C::parse_hotkey("Strg+Gibtsnicht").unwrap_err();
    assert!(err.contains("Gibtsnicht") && err != "Unknown key: Gibtsnicht", "{err}");
    let text = C::by_uuid("com.elgato.streamdeck.system.text").unwrap();
    assert_eq!(text.name(), "Text");
    assert_eq!(C::by_uuid("com.elgato.streamdeck.page.next").unwrap().name(), "Nächste Seite");
    // feste Namen bleiben unübersetzt (wie in Python)
    assert_eq!(C::by_uuid("com.elgato.streamdeck.multiactions.routine").unwrap().category_label(), "Multi Action");
    let new = C::new_action("com.elgato.streamdeck.page.next");
    assert_eq!(new["Name"], "Nächste Seite");
    // Kategorie auch als übersetzte Anzeige erkannt
    let nav = C::by_uuid("com.elgato.streamdeck.page.next").unwrap().category_label();
    assert_eq!(C::action_look("com.elgato.streamdeck.page.next", &nav), ("arrow", "#3b82f6"));
    let mixes = C::resolve_choices_in(C::DYN_MIXER_MIXES, json!({}).as_object().unwrap());
    assert_eq!(mixes[0].1, "Alle Mixe");

    set_language("en");
    for text in ["Ctrl+Shift+T", "Alt+F4", "Super+E", "Ctrl+Num +", "Ctrl+Del", "Shift+1", "Play/Pause", "PgUp"] {
        assert_eq!(C::format_hotkey(&C::parse_hotkey(text).unwrap()), text);
    }
    assert_eq!(C::format_hotkey(&C::parse_hotkey("Strg+C").unwrap()), "Ctrl+C");
    assert_eq!(C::parse_hotkey("Strg+Gibtsnicht").unwrap_err(), "Unknown key: Gibtsnicht");
    assert_eq!(C::parse_hotkey("Foo+T").unwrap_err(), "Unknown modifier key: Foo");
    assert_eq!(C::by_uuid("com.elgato.streamdeck.page.next").unwrap().name(), "Next Page");
    catalog_matches_python("en");
    set_language("de");
    catalog_matches_python("de");
}

/// Der ganze Katalog stimmt mit `freestream/catalog.py` überein (übersprungen ohne python3.12).
/// Wird vom Sprachtest aufgerufen; `lang` muss die aktive Sprache sein.
fn catalog_matches_python(lang: &str) {
    use serde_json::Value;
    let repo = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..").canonicalize().unwrap();
    let script = r#"
import json, sys
sys.path.insert(0, sys.argv[1])
from freestream import catalog as C
def fields(fs):
    if fs is None:
        return None
    out = []
    for f in fs:
        if f.kind == "entity":
            opts = f.options
        elif callable(f.options):
            opts = "dyn"
        elif f.options is None:
            opts = None
        else:
            opts = [[v, l] for v, l in f.options]
        out.append([f.kind, str(f.key), f.label, f.min, f.max, f.hint, opts])
    return out
print(json.dumps([[a.uuid, a.name, a.category, a.settings, a.states, list(a.controllers), fields(a.fields),
                   fields(a.dial_fields), [a.children[0], a.children[1]] if a.children else None, a.description,
                   a.plugin] for a in C.CATALOG]))
"#;
    let tmp = tempfile::tempdir().unwrap();
    let out = match std::process::Command::new("python3.12")
        .args(["-I", "-c", script])
        .arg(&repo)
        .env("FREESTREAM_LANG", lang)
        .env("XDG_CONFIG_HOME", tmp.path().join("c"))
        .env("XDG_DATA_HOME", tmp.path().join("d"))
        .env("XDG_STATE_HOME", tmp.path().join("s"))
        .output()
    {
        Ok(o) if o.status.success() => o,
        Ok(o) => panic!("Python: {}", String::from_utf8_lossy(&o.stderr)),
        Err(e) => {
            eprintln!("python3.12 nicht verfügbar ({e}) – übersprungen");
            return;
        }
    };
    let py: Value = serde_json::from_slice(&out.stdout).unwrap();
    let kind = |k: C::FieldKind| {
        use C::FieldKind::*;
        match k {
            Text => "text",
            Multiline => "multiline",
            Int => "int",
            Bool => "bool",
            Choice => "choice",
            File => "file",
            Hotkey => "hotkey",
            Page => "page",
            Profile => "profile",
            Entity => "entity",
        }
    };
    let fields = |fs: Option<&Vec<C::Field>>| -> Value {
        let Some(fs) = fs else { return Value::Null };
        Value::Array(
            fs.iter()
                .map(|f| {
                    let opts = match (&f.kind, &f.choices) {
                        (C::FieldKind::Entity, _) => json!(f.domains),
                        (_, C::Choices::Dynamic(_)) => json!("dyn"),
                        (_, C::Choices::None) => Value::Null,
                        (_, C::Choices::Static(l)) => Value::Array(l.iter().map(|c| json!([c.value, c.label.get()])).collect()),
                    };
                    json!([kind(f.kind), f.key, f.label(), f.min, f.max, f.hint(), opts])
                })
                .collect(),
        )
    };
    let rust: Vec<Value> = C::catalog()
        .iter()
        .map(|a| {
            let ctrls: Vec<&str> = a.controllers.iter().map(|c| if *c == C::KEY { "key" } else { "dial" }).collect();
            let children = a.children.as_ref().map(|c| {
                let kind = if c.kind == C::ChildKind::Groups { "groups" } else { "slots" };
                json!([kind, c.labels.iter().map(|l| l.get()).collect::<Vec<_>>()])
            });
            json!([a.uuid, a.name(), a.category_label(), a.settings, a.states, ctrls, fields(Some(&a.fields)),
                   fields(a.dial_fields.as_ref()), children, a.description(), a.plugin])
        })
        .collect();
    let py = py.as_array().unwrap();
    assert!(py.len() >= 50, "{}", py.len());
    // ohne FreeWave fehlen in Python dessen Aktionen
    let rust: Vec<&Value> = rust.iter().filter(|r| py.iter().any(|p| p[0] == r[0])).collect();
    assert_eq!(rust.len(), py.len());
    for (r, p) in rust.iter().zip(py) {
        assert_eq!(*r, p, "{}", p[0]);
    }
}
