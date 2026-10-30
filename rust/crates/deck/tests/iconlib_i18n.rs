//! Icon-Bibliothek auf Deutsch (eigene Testdatei: die Sprache gilt für den ganzen Prozess).

use freestream_core::i18n::set_language;
use freestream_deck::iconlib;

#[test]
fn labels_titles_and_search_in_german() {
    set_language("de");
    assert_eq!(iconlib::label("mic"), "Mikrofon");
    assert_ne!(iconlib::label("mic_off"), "Microphone off");
    assert_ne!(iconlib::label("lightbulb"), "Light");
    assert_ne!(iconlib::topic_title("home"), "Smart home");
    assert_ne!(iconlib::style_title("color"), "FreeDeck Icons (Color)");
    assert_eq!(iconlib::label("unbekannt"), "unbekannt");
    // Suche über den deutschen Anzeigenamen
    assert!(iconlib::search("mikro").contains(&"mic_off"));
    set_language("en");
    assert_eq!(iconlib::label("mic"), "Microphone");
}
