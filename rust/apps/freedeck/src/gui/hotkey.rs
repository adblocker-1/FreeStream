//! Hotkey-Feld: Knopf zeigt die Tastenkombination; Klick nimmt die nächste gedrückte Kombination auf.

use gtk4 as gtk;
use gtk::prelude::*;
use gtk::{glib, Orientation};

use freestream_core::catalog;
use freestream_core::editdoc::Slot;
use freestream_core::tr;
use serde_json::{Map, Value};
use std::rc::Rc;

use super::editor::Editor;

fn current(data: &Map<String, Value>, idx: usize) -> Value {
    data.get("Settings").and_then(|s| s.get("Hotkeys")).and_then(|h| h.get(idx)).cloned().unwrap_or_else(catalog::empty_hotkey)
}

fn text_of(hk: &Value) -> String {
    if catalog::hotkey_is_empty(hk) {
        tr!("Press keys…")
    } else {
        catalog::format_hotkey(hk)
    }
}

pub fn field(ed: &Rc<Editor>, slot: Slot, idx: usize, data: &Map<String, Value>) -> gtk::Widget {
    let row = gtk::Box::new(Orientation::Horizontal, 6);
    let btn = gtk::ToggleButton::with_label(&text_of(&current(data, idx)));
    btn.set_hexpand(true);
    row.append(&btn);
    let clear = super::util::icon_button("edit-clear-symbolic", &tr!("Clear"));
    row.append(&clear);

    let keys = gtk::EventControllerKey::new();
    let (e, b) = (ed.clone(), btn.clone());
    keys.connect_key_pressed(move |_, key, code, state| {
        if !b.is_active() {
            return glib::Propagation::Proceed;
        }
        let name = key.name().map(|n| n.to_string()).unwrap_or_default();
        if catalog::MODIFIER_KEYSYMS.contains(&name.as_str()) {
            return glib::Propagation::Stop; // nur Umschalt/Strg/…: auf die eigentliche Taste warten
        }
        let ch = key.to_unicode().map(|c| c.to_string()).unwrap_or_default();
        let mods = catalog::mods_from_gdk_state(state.bits());
        if let Some(hk) = catalog::hotkey_from_keysym(&name, &ch, mods, code) {
            b.set_label(&text_of(&hk));
            b.set_active(false);
            e.edit_soon(|doc, page| doc.set_hotkey(page, slot, &[], idx, hk));
        }
        glib::Propagation::Stop
    });
    btn.add_controller(keys);
    btn.connect_toggled(|b| {
        if b.is_active() {
            b.set_label(&tr!("Press keys …"));
            b.grab_focus();
        }
    });
    let (e, b) = (ed.clone(), btn.clone());
    clear.connect_clicked(move |_| {
        b.set_active(false);
        b.set_label(&text_of(&catalog::empty_hotkey()));
        e.edit_soon(|doc, page| doc.set_hotkey(page, slot, &[], idx, catalog::empty_hotkey()));
    });
    row.upcast()
}
