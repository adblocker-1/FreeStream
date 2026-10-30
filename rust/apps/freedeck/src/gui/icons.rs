//! Icon-Bibliothek („+“ unter der Taste): vordefinierte, selbst gezeichnete Tastenbilder.

use freestream_core::editdoc::Slot;
use freestream_core::tr;
use std::rc::Rc;

use super::editor::Editor;

pub fn open_library(ed: &Rc<Editor>, slot: Slot, idx: usize) {
    let e = ed.clone();
    super::inspector::choose_file(&ed.win, &tr!("Choose image file…"), move |p| super::inspector::set_icon_file(&e, slot, idx, &p));
}
