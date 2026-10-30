//! Übersetzungen aus denselben `.po`-Dateien wie die Python-Version (`freestream/locale`). Sie werden beim
//! Bauen eingebettet – das Programm braucht keine Dateien daneben.
//!
//! Sprache: `FREESTREAM_LANG` > `"language"` in config.json > Systemsprache > Englisch.

use std::collections::HashMap;
use std::sync::RwLock;

/// Ausgangssprache im Code.
pub const SOURCE: &str = "en";

/// Eingebettete Kataloge (Sprachcode, Inhalt der .po-Datei).
const CATALOGS: &[(&str, &str)] = &[
    ("de", include_str!("../../../../freestream/locale/de.po")),
    ("es", include_str!("../../../../freestream/locale/es.po")),
    ("fr", include_str!("../../../../freestream/locale/fr.po")),
    ("pt_BR", include_str!("../../../../freestream/locale/pt_BR.po")),
];

/// Namen in den Sprachmenüs (immer in der Sprache selbst).
pub fn name(lang: &str) -> &str {
    match lang {
        "en" => "English",
        "de" => "Deutsch",
        "es" => "Español",
        "fr" => "Français",
        "pt_BR" => "Português (Brasil)",
        other => other,
    }
}

struct State {
    lang: String,
    catalog: HashMap<String, Vec<String>>,
}

static STATE: RwLock<Option<State>> = RwLock::new(None);

/// Eintrag einer .po-Datei.
#[derive(Debug, Default, Clone)]
pub struct PoEntry {
    pub msgctxt: Option<String>,
    pub msgid: String,
    pub msgid_plural: Option<String>,
    pub msgstr: Vec<String>,
    pub fuzzy: bool,
}

fn unquote(s: &str) -> Option<String> {
    let s = s.trim();
    if s.len() < 2 || !s.starts_with('"') || !s.ends_with('"') {
        return None;
    }
    let mut out = String::new();
    let mut it = s[1..s.len() - 1].chars();
    while let Some(c) = it.next() {
        if c == '\\' {
            match it.next() {
                Some('n') => out.push('\n'),
                Some('t') => out.push('\t'),
                Some('r') => out.push('\r'),
                Some(o) => out.push(o),
                None => {}
            }
        } else {
            out.push(c);
        }
    }
    Some(out)
}

/// Einträge einer .po-Datei lesen (wie `i18n.parse_po`).
pub fn parse_po(text: &str) -> Vec<PoEntry> {
    #[derive(PartialEq, Clone, Copy)]
    enum Field {
        None,
        Ctx,
        Id,
        Plural,
        Str(usize),
    }
    let mut out = Vec::new();
    let mut cur: Option<PoEntry> = None;
    let mut has_id = false;
    let mut field = Field::None;
    let start_new = |cur: &mut Option<PoEntry>, has_id: &mut bool, out: &mut Vec<PoEntry>| {
        let begin = match cur {
            None => true,
            Some(e) => !e.msgstr.is_empty(),
        };
        if begin {
            if let Some(e) = cur.take() {
                if *has_id {
                    out.push(e);
                }
            }
            *cur = Some(PoEntry::default());
            *has_id = false;
        }
    };
    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() {
            continue;
        }
        if line.starts_with('#') {
            start_new(&mut cur, &mut has_id, &mut out);
            if let Some(flags) = line.strip_prefix("#,") {
                if flags.split(',').any(|f| f.trim() == "fuzzy") {
                    cur.as_mut().unwrap().fuzzy = true;
                }
            }
            field = Field::None;
            continue;
        }
        let (key, rest) = match line.find(char::is_whitespace) {
            Some(i) => (&line[..i], line[i..].trim()),
            None => (line, ""),
        };
        let keyed = key.starts_with("msgctxt") || key.starts_with("msgid") || key.starts_with("msgstr");
        if keyed && rest.starts_with('"') {
            let Some(val) = unquote(rest) else { continue };
            if key == "msgctxt" || key == "msgid" {
                start_new(&mut cur, &mut has_id, &mut out);
            }
            let Some(e) = cur.as_mut() else { continue };
            match key {
                "msgctxt" => {
                    e.msgctxt = Some(val);
                    field = Field::Ctx;
                }
                "msgid" => {
                    e.msgid = val;
                    has_id = true;
                    field = Field::Id;
                }
                "msgid_plural" => {
                    e.msgid_plural = Some(val);
                    field = Field::Plural;
                }
                _ => {
                    let i = key
                        .strip_prefix("msgstr[")
                        .and_then(|r| r.strip_suffix(']'))
                        .and_then(|n| n.parse().ok())
                        .unwrap_or(0);
                    while e.msgstr.len() <= i {
                        e.msgstr.push(String::new());
                    }
                    e.msgstr[i] = val;
                    field = Field::Str(i);
                }
            }
            continue;
        }
        if line.starts_with('"') {
            if let (Some(e), Some(val)) = (cur.as_mut(), unquote(line)) {
                match field {
                    Field::Ctx => e.msgctxt.get_or_insert_with(String::new).push_str(&val),
                    Field::Id => e.msgid.push_str(&val),
                    Field::Plural => e.msgid_plural.get_or_insert_with(String::new).push_str(&val),
                    Field::Str(i) => e.msgstr[i].push_str(&val),
                    Field::None => {}
                }
            }
        }
    }
    if let Some(e) = cur {
        if has_id {
            out.push(e);
        }
    }
    out
}

fn key(msgid: &str, ctx: Option<&str>) -> String {
    match ctx {
        Some(c) => format!("{c}\u{4}{msgid}"),
        None => msgid.to_string(),
    }
}

fn load_catalog(lang: &str) -> HashMap<String, Vec<String>> {
    let mut out = HashMap::new();
    let Some((_, text)) = CATALOGS.iter().find(|(l, _)| *l == lang) else { return out };
    for e in parse_po(text) {
        if e.msgid.is_empty() || e.fuzzy || e.msgstr.is_empty() || e.msgstr.iter().any(|s| s.is_empty()) {
            continue;
        }
        out.insert(key(&e.msgid, e.msgctxt.as_deref()), e.msgstr);
    }
    out
}

/// Sprachen mit Übersetzung (und Englisch), Englisch zuerst.
pub fn available() -> Vec<&'static str> {
    let mut v: Vec<&str> = CATALOGS.iter().map(|(l, _)| *l).collect();
    v.sort_by_key(|l| name(l).to_lowercase());
    v.insert(0, SOURCE);
    v
}

/// „pt_BR.UTF-8“ oder „de-AT“ auf eine vorhandene Sprache abbilden.
pub fn match_lang(code: &str) -> Option<&'static str> {
    let code = code.split('.').next()?.split('@').next()?.replace('-', "_");
    if code.is_empty() || code == "C" || code == "POSIX" {
        return None;
    }
    let have = available();
    if let Some(l) = have.iter().find(|l| **l == code) {
        return Some(l);
    }
    let base = code.split('_').next().unwrap_or("");
    have.iter().find(|l| l.split('_').next() == Some(base)).copied()
}

/// Sprache wie die Python-Version bestimmen.
pub fn detect() -> &'static str {
    for var in ["FREESTREAM_LANG", "FREEDECK_LANG"] {
        if let Ok(v) = std::env::var(var) {
            if !v.is_empty() {
                return match_lang(&v).unwrap_or(SOURCE);
            }
        }
    }
    let cfg = crate::config::read_file()
        .ok()
        .and_then(|d| d.get("language").and_then(|v| v.as_str().map(str::to_string)))
        .unwrap_or_else(|| "auto".into());
    if cfg != "auto" {
        if let Some(l) = match_lang(&cfg) {
            return l;
        }
    }
    for var in ["LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"] {
        let Ok(val) = std::env::var(var) else { continue };
        if val.is_empty() {
            continue;
        }
        for part in val.split(':') {
            if let Some(l) = match_lang(part) {
                return l;
            }
        }
        if var != "LANGUAGE" {
            break;
        }
    }
    SOURCE
}

/// Sprache umschalten (für neue Texte); gibt sie zurück.
pub fn set_language(lang: &str) -> String {
    let lang = match_lang(lang).unwrap_or(SOURCE).to_string();
    let catalog = load_catalog(&lang);
    *STATE.write().unwrap() = Some(State { lang: lang.clone(), catalog });
    lang
}

/// Aktive Sprache.
pub fn current() -> String {
    ensure();
    STATE.read().unwrap().as_ref().map(|s| s.lang.clone()).unwrap_or_else(|| SOURCE.into())
}

fn ensure() {
    if STATE.read().unwrap().is_none() {
        set_language(detect());
    }
}

fn lookup(k: &str) -> Option<Vec<String>> {
    ensure();
    STATE.read().unwrap().as_ref().and_then(|s| s.catalog.get(k).cloned())
}

/// Text übersetzen (`_()` der Python-Version).
pub fn gettext(msgid: &str) -> String {
    lookup(msgid).map(|t| t[0].clone()).unwrap_or_else(|| msgid.to_string())
}

/// Wie [`gettext`] mit Kontext (Wörter, die sich je nach Stelle anders übersetzen).
pub fn pgettext(ctx: &str, msgid: &str) -> String {
    lookup(&key(msgid, Some(ctx))).map(|t| t[0].clone()).unwrap_or_else(|| msgid.to_string())
}

/// Einzahl/Mehrzahl.
pub fn ngettext(singular: &str, plural: &str, n: i64) -> String {
    if let Some(t) = lookup(singular) {
        if t.len() > 1 {
            let lang = current();
            let i: usize = match lang.as_str() {
                "fr" | "pt_BR" => usize::from(!(n == 0 || n == 1)),
                _ => usize::from(n != 1),
            };
            return t[i.min(t.len() - 1)].clone();
        }
    }
    if n == 1 { singular } else { plural }.to_string()
}

/// Platzhalter `{name}` füllen (wie `str.format` in Python).
pub fn fmt(text: &str, args: &[(&str, &dyn std::fmt::Display)]) -> String {
    let mut out = text.to_string();
    for (k, v) in args {
        // `r#where` (Rust-Schlüsselwort als Name) steht im Text als {where}
        let k = k.trim_start_matches("r#");
        out = out.replace(&format!("{{{k}}}"), &v.to_string());
    }
    out
}

/// `tr!("Text {n}", n = 3)`: übersetzen und Platzhalter füllen.
#[macro_export]
macro_rules! tr {
    ($msg:expr) => { $crate::i18n::gettext($msg) };
    ($msg:expr, $($k:ident = $v:expr),+ $(,)?) => {
        $crate::i18n::fmt(&$crate::i18n::gettext($msg), &[$((stringify!($k), &$v as &dyn std::fmt::Display)),+])
    };
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_catalogs_and_translates() {
        let entries = parse_po(CATALOGS[0].1);
        assert!(entries.len() > 1000);
        set_language("de");
        assert_eq!(gettext("Profiles"), "Profile");
        assert_eq!(pgettext("state", "on"), "an");
        set_language("en");
        assert_eq!(gettext("Profiles"), "Profiles");
    }

    #[test]
    fn matches_locales() {
        assert_eq!(match_lang("de_AT.UTF-8"), Some("de"));
        assert_eq!(match_lang("pt"), Some("pt_BR"));
        assert_eq!(match_lang("C"), None);
    }
}
