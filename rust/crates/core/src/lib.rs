//! Gemeinsamer Kern von FreeDeck und FreeWave: Pfade (XDG), `config.json`, Übersetzungen,
//! Gerätemodelle und das Profilformat der Elgato-Software. Dateien und Formate sind dieselben wie in
//! der Python-Version – beide lesen und schreiben dieselben Ordner.

pub mod catalog;
pub mod config;
pub mod editdoc;
pub mod i18n;
pub mod models;
pub mod paths;
pub mod profiles;

pub use i18n::{gettext as tr, pgettext};

/// Version des Programms (gleich für FreeDeck und FreeWave).
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
