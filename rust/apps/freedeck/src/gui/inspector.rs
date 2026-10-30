//! Eigenschaften der gewählten Taste (unten im Fenster), wie in der Deck-Software: links die Taste mit
//! „+“ (Icon-Bibliothek) und ⌄ (Symbol, Deckkraft, JSON, Löschen), rechts der Titel mit T⌄ und die Felder
//! der Aktion aus dem Katalog; bei Multi-Aktionen darunter die enthaltenen Aktionen.

use gtk4 as gtk;
use gtk::prelude::*;
use gtk::{glib, Align, Orientation};

use freestream_core::catalog::{self, ActionType, ChildKind, Field, FieldKind};
use freestream_core::editdoc::Slot;
use freestream_core::tr;
use serde_json::{json, Map, Value};
use std::rc::Rc;

use super::editor::Editor;
use super::util::{glyph_tile, label};

const ALIGN: [(&str, &str); 3] = [("top", "top"), ("middle", "middle"), ("bottom", "bottom")];
const STYLES: [(&str, &str); 4] = [("", "Regular"), ("Bold", "Bold"), ("Italic", "Italic"), ("Bold Italic", "Bold italic")];

fn clear(b: &gtk::Box) {
    while let Some(c) = b.first_child() {
        b.remove(&c);
    }
}

/// Aktion auf der gewählten Taste bzw. dem Drehregler (Kopie) und ihr Platz.
fn selected(ed: &Rc<Editor>) -> Option<(u8, Slot, Map<String, Value>)> {
    let slot = ed.selected_slot()?;
    let k = ed.state.borrow().selected.unwrap_or(0);
    let page = ed.page_uuid()?;
    let st = ed.state.borrow();
    let data = st.doc.as_ref()?.get(&page, slot)?.clone();
    Some((k, slot, data))
}

fn state_of(data: &Map<String, Value>, idx: usize) -> Map<String, Value> {
    data.get("States")
        .and_then(Value::as_array)
        .and_then(|s| s.get(idx.min(s.len().saturating_sub(1))))
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default()
}

fn menu_row(text: &str, icon: Option<&str>, classes: &[&str]) -> gtk::Button {
    let b = gtk::Button::new();
    b.add_css_class("title-drop");
    let row = gtk::Box::new(Orientation::Horizontal, 10);
    row.add_css_class("menu-row");
    if let Some(i) = icon {
        row.append(&gtk::Image::from_icon_name(i));
    }
    row.append(&label(text, classes));
    b.set_child(Some(&row));
    b
}

fn popover_box(pop: &gtk::Popover) -> gtk::Box {
    let list = gtk::Box::new(Orientation::Vertical, 2);
    list.set_margin_start(6);
    list.set_margin_end(6);
    list.set_margin_top(6);
    list.set_margin_bottom(6);
    pop.set_child(Some(&list));
    list
}

fn close_popover(w: &impl IsA<gtk::Widget>) {
    if let Some(p) = w.ancestor(gtk::Popover::static_type()) {
        p.downcast::<gtk::Popover>().unwrap().popdown();
    }
}

pub fn build(ed: &Rc<Editor>) {
    clear(&ed.insp_head);
    clear(&ed.insp_body);
    *ed.insp_pic.borrow_mut() = None;
    let g = ed.geometry();
    let Some(sel) = ed.selected_slot() else {
        ed.insp_head.append(&label(&tr!("Select a key to edit it."), &["insp-name"]));
        ed.insp_body.append(&label(&tr!("Drag an action here from the list on the right, or double-click it there."), &["muted"]));
        return;
    };
    let where_ = match sel {
        Slot::Dial(i) => tr!("Dial {n}", n = i + 1),
        _ => {
            let k = ed.state.borrow().selected.unwrap_or(0);
            tr!("Key {col},{row}", col = k % g.cols + 1, row = k / g.cols + 1)
        }
    };
    let Some((k, slot, data)) = selected(ed) else {
        ed.insp_head.append(&label(&tr!("{where} – empty", r#where = where_), &["insp-name"]));
        ed.insp_body.append(&label(&tr!("Drag an action here from the list on the right, or double-click it there."), &["muted"]));
        return;
    };
    let uuid = data.get("UUID").and_then(Value::as_str).unwrap_or("").to_string();
    let typ = catalog::lookup(&uuid);
    let cat = typ.map(ActionType::category_label).unwrap_or_default();
    let (glyph, color) = catalog::action_look(&uuid, typ.map(|t| t.category.id()).unwrap_or(""));

    // ------------------------------------------------------------------ Kopf
    ed.insp_head.append(&glyph_tile(glyph, color, 28));
    if !cat.is_empty() {
        let c = label(&format!("{cat}:"), &["insp-cat"]);
        c.set_margin_start(6);
        ed.insp_head.append(&c);
    }
    ed.insp_head.append(&label(&catalog::display_name(&data), &["insp-name"]));
    let w = label(&where_, &["muted"]);
    w.set_margin_start(10);
    ed.insp_head.append(&w);
    let n_states = data.get("States").and_then(Value::as_array).map(Vec::len).unwrap_or(1).max(1);
    let state_idx = ed.state.borrow().state_idx.min(n_states - 1);
    if n_states > 1 {
        // Zustand wählen (z. B. Stummschalten an/aus)
        let spacer = gtk::Box::new(Orientation::Horizontal, 0);
        spacer.set_hexpand(true);
        ed.insp_head.append(&spacer);
        let names: Vec<String> = (0..n_states).map(|i| match i {
            0 => tr!("State 1"),
            1 => tr!("State 2"),
            _ => format!("{} {}", tr!("State 1").trim_end_matches('1').trim_end(), i + 1),
        }).collect();
        let refs: Vec<&str> = names.iter().map(String::as_str).collect();
        let dd = gtk::DropDown::from_strings(&refs);
        dd.set_selected(state_idx as u32);
        let e = ed.clone();
        dd.connect_selected_notify(move |d| {
            e.state.borrow_mut().state_idx = d.selected() as usize;
            build(&e);
        });
        ed.insp_head.append(&dd);
    }

    // ------------------------------------------------- links: Taste mit + und ⌄
    let left = gtk::Box::new(Orientation::Vertical, 6);
    left.set_valign(Align::Start);
    let preview = match slot {
        // Drehregler: sein Feld auf dem Touchstreifen
        Slot::Dial(i) => ed.state.borrow().preview.as_ref().and_then(|c| c.lcd_frame()).map(|img| {
            let n = g.dials.max(1) as u32;
            let w = img.width() / n;
            image::imageops::crop_imm(&img, w * i as u32, 0, w, img.height()).to_image()
        }),
        _ => ed.state.borrow().keys.get(&k).cloned(),
    };
    if let Some(img) = preview.as_ref() {
        let pic = gtk::Picture::for_paintable(&super::util::texture(img));
        pic.set_size_request(96, 96);
        pic.set_can_shrink(true);
        pic.add_css_class("key-preview");
        left.append(&pic);
        *ed.insp_pic.borrow_mut() = Some(pic);
    }
    let tools = gtk::Box::new(Orientation::Horizontal, 4);
    tools.set_halign(Align::Center);
    let plus = super::util::icon_button("list-add-symbolic", &tr!("Open icon collection…"));
    let e = ed.clone();
    plus.connect_clicked(move |_| super::icons::open_library(&e, slot, state_idx));
    tools.append(&plus);
    let more = gtk::MenuButton::new();
    more.set_icon_name("pan-down-symbolic");
    more.add_css_class("icon-btn");
    let pop = gtk::Popover::new();
    pop.set_has_arrow(false);
    fill_key_menu(ed, &pop, slot, state_idx, &state_of(&data, state_idx));
    more.set_popover(Some(&pop));
    tools.append(&more);
    left.append(&tools);
    ed.insp_body.append(&left);

    // ------------------------------------------------ rechts: Titel und Felder
    let right = gtk::Box::new(Orientation::Vertical, 6);
    right.set_hexpand(true);
    let sc = gtk::ScrolledWindow::new();
    sc.set_hscrollbar_policy(gtk::PolicyType::Never);
    sc.set_propagate_natural_height(true);
    sc.set_max_content_height(220);
    sc.set_child(Some(&right));
    sc.set_hexpand(true);
    ed.insp_body.append(&sc);
    let grid = gtk::Grid::new();
    grid.set_row_spacing(8);
    grid.set_column_spacing(14);
    right.append(&grid);
    let mut row = 0;

    // Titel mit T⌄
    let st = state_of(&data, state_idx);
    attach_label(&grid, row, &tr!("Title"));
    let trow = gtk::Box::new(Orientation::Horizontal, 6);
    let tv = gtk::TextView::new();
    tv.add_css_class("title-text");
    tv.set_hexpand(true);
    tv.set_wrap_mode(gtk::WrapMode::WordChar);
    tv.buffer().set_text(st.get("Title").and_then(Value::as_str).unwrap_or(""));
    let e = ed.clone();
    tv.buffer().connect_changed(move |b| {
        let text = b.text(&b.start_iter(), &b.end_iter(), false).to_string();
        e.edit_soon(|doc, page| doc.update_state(page, slot, &[], state_idx, "Title", Value::String(text)));
    });
    trow.append(&tv);
    let tbtn = gtk::MenuButton::new();
    tbtn.set_label("T");
    tbtn.add_css_class("icon-btn");
    tbtn.set_tooltip_text(Some(&tr!("Title")));
    let tpop = gtk::Popover::new();
    tpop.set_has_arrow(false);
    title_settings(ed, &tpop, slot, state_idx, &st);
    tbtn.set_popover(Some(&tpop));
    tbtn.set_valign(Align::Start);
    trow.append(&tbtn);
    grid.attach(&trow, 1, row, 1, 1);
    row += 1;

    let plugin_ok = typ.is_none() && freestream_deck::plugins::action_supported(&uuid);
    if plugin_ok && freestream_deck::plugins::has_property_inspector(&uuid) {
        // Einstellungsseite des Plugins im Browser (läuft über den Plugin-Host des Dienstes)
        let b = gtk::Button::with_label(&tr!("Settings page…"));
        b.add_css_class("suggested-action");
        b.set_halign(Align::Start);
        let e = ed.clone();
        b.connect_clicked(move |_| open_property_inspector(&e, slot));
        grid.attach(&b, 1, row, 1, 1);
        row += 1;
    }
    if typ.is_none() && !plugin_ok {
        let note = label(&tr!("Action of the plugin “{plugin}” – cannot run on Linux (plugin not installed or only for Windows/macOS). Title, icon and settings are kept.",
                              plugin = data.get("Plugin").and_then(|p| p.get("Name")).and_then(Value::as_str).unwrap_or(uuid.rsplit_once('.').map(|x| x.0).unwrap_or(&uuid))), &["muted"]);
        note.set_wrap(true);
        grid.attach(&note, 1, row, 1, 1);
    }
    if let Some(t) = typ {
        let settings = data.get("Settings").and_then(Value::as_object).cloned().unwrap_or_default();
        if uuid == catalog::FOLDER_UUID {
            let b = gtk::Button::with_label(&tr!("Open folder"));
            b.set_halign(Align::Start);
            let (e, f) = (ed.clone(), settings.get("ProfileUUID").and_then(Value::as_str).map(str::to_string));
            b.connect_clicked(move |_| {
                if let Some(f) = &f {
                    if let Some(c) = e.state.borrow_mut().preview.as_mut() {
                        c.open_folder(f);
                    }
                    e.state.borrow_mut().selected = None;
                    e.refresh();
                }
            });
            grid.attach(&b, 1, row, 1, 1);
            row += 1;
        }
        for f in t.fields_for(slot.controller()) {
            attach_label(&grid, row, &f.label());
            grid.attach(&field_widget(ed, slot, f, &settings, &data), 1, row, 1, 1);
            row += 1;
        }
        if let Some(ch) = &t.children {
            children(ed, &right, slot, ch.kind, &ch.labels.iter().map(|l| l.get()).collect::<Vec<_>>(), &data);
        }
    }
}

fn attach_label(grid: &gtk::Grid, row: i32, text: &str) {
    let l = label(&format!("{text}:"), &["field-label"]);
    l.set_xalign(1.0);
    l.set_valign(Align::Start);
    l.set_margin_top(6);
    l.set_width_chars(14);
    grid.attach(&l, 0, row, 1, 1);
}

// ============================================================================ Felder
fn field_widget(ed: &Rc<Editor>, slot: Slot, f: &Field, s: &Map<String, Value>, data: &Map<String, Value>) -> gtk::Widget {
    let key = f.key.clone();
    let set = {
        let ed = ed.clone();
        move |key: String, v: Value| ed.edit_soon(|doc, page| doc.update_settings(page, slot, &[], &key, v))
    };
    match f.kind {
        FieldKind::Entity => entity_field(ed, f, s, set),
        FieldKind::Text | FieldKind::File => {
            let b = gtk::Box::new(Orientation::Horizontal, 4);
            let e = gtk::Entry::new();
            e.set_hexpand(true);
            e.set_text(&s.get(&key).map(value_text).unwrap_or_default());
            let (set2, k2) = (set.clone(), key.clone());
            e.connect_changed(move |e| set2(k2.clone(), Value::String(e.text().to_string())));
            b.append(&e);
            if f.kind == FieldKind::File {
                let btn = gtk::Button::with_label("…");
                let (ed2, e2) = (ed.clone(), e.clone());
                btn.connect_clicked(move |_| {
                    let e3 = e2.clone();
                    choose_file(&ed2.win, &tr!("Choose file"), move |p| e3.set_text(&p.to_string_lossy()));
                });
                b.append(&btn);
            }
            let hint = f.hint();
            if !hint.is_empty() {
                b.append(&label(&hint, &["muted"]));
            }
            b.upcast()
        }
        FieldKind::Multiline => {
            let tv = gtk::TextView::new();
            tv.add_css_class("title-text");
            tv.set_size_request(-1, 70);
            tv.set_wrap_mode(gtk::WrapMode::WordChar);
            tv.buffer().set_text(&s.get(&key).map(value_text).unwrap_or_default());
            tv.buffer().connect_changed(move |b| set(key.clone(), Value::String(b.text(&b.start_iter(), &b.end_iter(), false).to_string())));
            tv.upcast()
        }
        FieldKind::Int => {
            let sp = gtk::SpinButton::with_range(f.min as f64, f.max.max(f.min) as f64, 1.0);
            sp.set_value(s.get(&key).and_then(|v| v.as_i64().or_else(|| v.as_str()?.parse().ok())).unwrap_or(f.min) as f64);
            sp.set_halign(Align::Start);
            sp.connect_value_changed(move |sp| set(key.clone(), json!(sp.value() as i64)));
            sp.upcast()
        }
        FieldKind::Bool => {
            let sw = gtk::Switch::new();
            sw.set_halign(Align::Start);
            sw.set_active(s.get(&key).map(|v| v.as_bool().unwrap_or(false)).unwrap_or(key == "bring_to_front"));
            sw.connect_active_notify(move |sw| set(key.clone(), Value::Bool(sw.is_active())));
            sw.upcast()
        }
        FieldKind::Choice => {
            let mut vals = f.resolved_choices();
            let cur = s.get(&key).cloned().unwrap_or(Value::Null);
            let same = |a: &Value, b: &Value| value_text(a) == value_text(b);
            if !matches!(&cur, Value::Null) && value_text(&cur) != "" && !vals.iter().any(|(v, _)| same(v, &cur)) {
                // z. B. gelöschte Szene: alten Wert behalten
                vals.retain(|(v, _)| value_text(v) != "");
                vals.insert(0, (cur.clone(), value_text(&cur)));
            }
            dropdown(vals.iter().map(|(_, n)| n.clone()).collect(), vals.iter().position(|(v, _)| same(v, &cur)).unwrap_or(0), move |i| {
                if let Some((v, _)) = vals.get(i) {
                    set(key.clone(), v.clone());
                }
            })
        }
        FieldKind::Page => {
            let n = ed.state.borrow().doc.as_ref().map(|d| d.page_ids().len()).unwrap_or(1).max(1);
            let cur = s.get(&key).and_then(|v| v.as_i64().or_else(|| v.as_str()?.parse().ok())).unwrap_or(1);
            dropdown((1..=n).map(|i| tr!("Page {n}", n = i)).collect(), (cur.max(1) as usize - 1).min(n - 1), move |i| set(key.clone(), json!(i + 1)))
        }
        FieldKind::Profile => {
            let profs: Vec<(String, String)> = {
                let st = ed.state.borrow();
                let store = st.store.lock().unwrap();
                let mut v: Vec<_> = store.profiles.values().map(|p| (p.id.clone(), format!("{}  ·  {}", p.name, p.model_name()))).collect();
                v.sort_by_key(|p| p.1.to_lowercase());
                v
            };
            let cur = s.get(&key).and_then(Value::as_str).unwrap_or("").to_lowercase();
            let mut names = vec![tr!("(next profile)")];
            names.extend(profs.iter().map(|p| p.1.clone()));
            let idx = profs.iter().position(|p| p.0.to_lowercase() == cur).map(|i| i + 1).unwrap_or(0);
            dropdown(names, idx, move |i| set(key.clone(), Value::String(if i == 0 { String::new() } else { profs[i - 1].0.clone() })))
        }
        FieldKind::Hotkey => super::hotkey::field(ed, slot, f.hotkey_index().unwrap_or(0), data),
    }
}

fn value_text(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

fn dropdown(names: Vec<String>, cur: usize, on: impl Fn(usize) + 'static) -> gtk::Widget {
    let refs: Vec<&str> = names.iter().map(String::as_str).collect();
    let dd = gtk::DropDown::from_strings(&refs);
    dd.set_selected(cur.min(names.len().saturating_sub(1)) as u32);
    dd.set_halign(Align::Start);
    dd.connect_selected_notify(move |d| on(d.selected() as usize));
    dd.upcast()
}

pub fn choose_file(win: &gtk::ApplicationWindow, title: &str, on: impl Fn(std::path::PathBuf) + 'static) {
    let dlg = gtk::FileChooserNative::new(Some(title), Some(win), gtk::FileChooserAction::Open, Some(&tr!("Open")), Some(&tr!("Cancel")));
    dlg.connect_response(move |d, resp| {
        if resp == gtk::ResponseType::Accept {
            if let Some(p) = d.file().and_then(|f| f.path()) {
                on(p);
            }
        }
    });
    dlg.show();
}

// ======================================================================= T⌄ und ⌄
fn title_settings(ed: &Rc<Editor>, pop: &gtk::Popover, slot: Slot, idx: usize, st: &Map<String, Value>) {
    let grid = gtk::Grid::new();
    grid.set_row_spacing(8);
    grid.set_column_spacing(12);
    for m in [grid.upcast_ref::<gtk::Widget>()] {
        m.set_margin_start(10);
        m.set_margin_end(10);
        m.set_margin_top(10);
        m.set_margin_bottom(10);
    }
    pop.set_child(Some(&grid));
    let set = {
        let ed = ed.clone();
        move |key: &'static str, v: Value| ed.edit_soon(|doc, page| doc.update_state(page, slot, &[], idx, key, v))
    };
    let lab = |text: String, r: i32| {
        let l = label(&text, &["muted"]);
        grid.attach(&l, 0, r, 1, 1);
    };
    lab(tr!("Show title"), 0);
    let r0 = gtk::Box::new(Orientation::Horizontal, 10);
    let show = gtk::Switch::new();
    show.set_active(st.get("ShowTitle").and_then(Value::as_bool) != Some(false));
    let s2 = set.clone();
    show.connect_active_notify(move |w| s2("ShowTitle", Value::Bool(w.is_active())));
    r0.append(&show);
    let cur = st.get("TitleAlignment").and_then(Value::as_str).unwrap_or("bottom").to_lowercase();
    let s2 = set.clone();
    r0.append(&dropdown(ALIGN.iter().map(|(_, n)| tr!(*n)).collect(), ALIGN.iter().position(|(v, _)| *v == cur).unwrap_or(2), move |i| {
        s2("TitleAlignment", Value::String(ALIGN[i].0.into()))
    }));
    grid.attach(&r0, 1, 0, 1, 1);

    lab(tr!("Font"), 1);
    let r1 = gtk::Box::new(Orientation::Horizontal, 8);
    let cur = st.get("FontStyle").and_then(Value::as_str).unwrap_or("").replace("Regular", "");
    let s2 = set.clone();
    r1.append(&dropdown(STYLES.iter().map(|(_, n)| tr!(*n)).collect(), STYLES.iter().position(|(v, _)| v.eq_ignore_ascii_case(cur.trim())).unwrap_or(0), move |i| {
        s2("FontStyle", Value::String(STYLES[i].0.into()))
    }));
    let size = gtk::SpinButton::with_range(6.0, 40.0, 1.0);
    size.set_value(st.get("FontSize").and_then(Value::as_i64).unwrap_or(12) as f64);
    let s2 = set.clone();
    size.connect_value_changed(move |w| s2("FontSize", json!(w.value() as i64)));
    r1.append(&size);
    grid.attach(&r1, 1, 1, 1, 1);

    lab(tr!("Title color"), 2);
    let r2 = gtk::Box::new(Orientation::Horizontal, 10);
    let rgba = gtk::gdk::RGBA::parse(st.get("TitleColor").and_then(Value::as_str).unwrap_or("#ffffff")).unwrap_or(gtk::gdk::RGBA::WHITE);
    #[allow(deprecated)]
    let color = gtk::ColorButton::with_rgba(&rgba);
    let s2 = set.clone();
    #[allow(deprecated)]
    color.connect_color_set(move |b| {
        let c = b.rgba();
        let hex = format!("#{:02x}{:02x}{:02x}", (c.red() * 255.0).round() as u8, (c.green() * 255.0).round() as u8, (c.blue() * 255.0).round() as u8);
        s2("TitleColor", Value::String(hex));
    });
    r2.append(&color);
    let und = gtk::Switch::new();
    und.set_active(st.get("FontUnderline").and_then(Value::as_bool).unwrap_or(false));
    und.connect_active_notify(move |w| set("FontUnderline", Value::Bool(w.is_active())));
    r2.append(&und);
    r2.append(&label(&tr!("underline"), &[]));
    grid.attach(&r2, 1, 2, 1, 1);
}

/// ⌄ unter der Taste: Symbol (Datei, Zwischenablage, Bibliothek, Standard), Deckkraft, JSON, Löschen.
fn fill_key_menu(ed: &Rc<Editor>, pop: &gtk::Popover, slot: Slot, idx: usize, st: &Map<String, Value>) {
    let list = popover_box(pop);
    list.set_size_request(250, -1);
    let head = label(&tr!("Icon"), &["section"]);
    head.set_margin_start(10);
    list.append(&head);

    let b = menu_row(&tr!("Choose image file…"), Some("image-x-generic-symbolic"), &[]);
    let e = ed.clone();
    b.connect_clicked(move |b| {
        close_popover(b);
        let e2 = e.clone();
        choose_file(&e.win, &tr!("Choose image file…"), move |p| set_icon_file(&e2, slot, idx, &p));
    });
    list.append(&b);

    let b = menu_row(&tr!("Paste from clipboard"), Some("edit-paste-symbolic"), &[]);
    let e = ed.clone();
    b.connect_clicked(move |b| {
        close_popover(b);
        let e2 = e.clone();
        e.win.clipboard().read_texture_async(None::<&gtk::gio::Cancellable>, move |res| {
            if let Ok(Some(tex)) = res {
                let tmp = std::env::temp_dir().join(format!("freedeck-clip-{}.png", std::process::id()));
                if tex.save_to_png(&tmp).is_ok() {
                    set_icon_file(&e2, slot, idx, &tmp);
                    let _ = std::fs::remove_file(&tmp);
                }
            }
        });
    });
    list.append(&b);

    let b = menu_row(&tr!("Open icon collection…"), Some("view-grid-symbolic"), &[]);
    let e = ed.clone();
    b.connect_clicked(move |b| {
        close_popover(b);
        super::icons::open_library(&e, slot, idx);
    });
    list.append(&b);

    let b = menu_row(&tr!("Default"), Some("edit-undo-symbolic"), &[]);
    b.set_sensitive(st.get("Image").and_then(Value::as_str).map(|s| !s.is_empty()).unwrap_or(false));
    let e = ed.clone();
    b.connect_clicked(move |b| {
        close_popover(b);
        e.edit(|doc, page| doc.update_state(page, slot, &[], idx, "Image", Value::String(String::new())));
    });
    list.append(&b);

    // Deckkraft (das Hintergrundbild der Seite scheint durch)
    let orow = gtk::Box::new(Orientation::Horizontal, 8);
    orow.add_css_class("menu-row");
    orow.append(&gtk::Image::from_icon_name("display-brightness-symbolic"));
    orow.append(&label(&tr!("Opacity"), &[]));
    let sc = gtk::Scale::with_range(Orientation::Horizontal, 0.0, 100.0, 1.0);
    sc.set_hexpand(true);
    sc.set_value(st.get("IconOpacity").and_then(Value::as_i64).unwrap_or(100) as f64);
    let e = ed.clone();
    sc.connect_value_changed(move |s| {
        let v = s.value().round() as i64;
        e.edit_soon(|doc, page| {
            // ein Rückgängig-Schritt für alle Zustände
            doc.checkpoint(Some(&format!("opacity|{page}|{slot:?}")));
            if let Some(a) = doc.action_mut(page, slot, &[]) {
                if let Some(states) = a.get_mut("States").and_then(Value::as_array_mut) {
                    for st in states.iter_mut().filter_map(Value::as_object_mut) {
                        st.insert("IconOpacity".into(), json!(v));
                    }
                }
            }
            doc.changed();
        });
    });
    orow.append(&sc);
    list.append(&orow);

    list.append(&gtk::Separator::new(Orientation::Horizontal));
    let b = menu_row(&tr!("Settings (JSON)…"), Some("document-edit-symbolic"), &[]);
    let e = ed.clone();
    b.connect_clicked(move |b| {
        close_popover(b);
        edit_json(&e, slot);
    });
    list.append(&b);
    list.append(&gtk::Separator::new(Orientation::Horizontal));
    let b = menu_row(&tr!("Delete"), Some("user-trash-symbolic"), &["danger"]);
    let e = ed.clone();
    b.connect_clicked(move |b| {
        close_popover(b);
        if let Some(k) = e.state.borrow().selected {
            let e = e.clone();
            glib::idle_add_local_once(move || e.delete_key(k));
        }
    });
    list.append(&b);
}

pub fn set_icon_file(ed: &Rc<Editor>, slot: Slot, idx: usize, path: &std::path::Path) {
    let mut err = None;
    ed.edit(|doc, page| {
        if let Err(e) = doc.set_icon(page, slot, &[], idx, path) {
            err = Some(e.to_string());
        }
    });
    if let Some(e) = err {
        super::editor::info(&ed.win, &tr!("Icon"), &e);
    }
}

fn edit_json(ed: &Rc<Editor>, slot: Slot) {
    let Some(page) = ed.page_uuid() else { return };
    let settings = ed.state.borrow().doc.as_ref().and_then(|d| d.get(&page, slot)).and_then(|a| a.get("Settings").cloned()).unwrap_or(json!({}));
    let win = gtk::Window::builder().title(tr!("Settings (JSON)…").trim_end_matches('…')).transient_for(&ed.win).modal(true).default_width(560).default_height(420).build();
    let b = gtk::Box::new(Orientation::Vertical, 10);
    for m in [b.upcast_ref::<gtk::Widget>()] {
        m.set_margin_start(14);
        m.set_margin_end(14);
        m.set_margin_top(14);
        m.set_margin_bottom(14);
    }
    let tv = gtk::TextView::new();
    tv.set_monospace(true);
    tv.buffer().set_text(&serde_json::to_string_pretty(&settings).unwrap_or_default());
    let sc = gtk::ScrolledWindow::new();
    sc.set_child(Some(&tv));
    sc.set_vexpand(true);
    b.append(&sc);
    let err = label("", &["danger"]);
    b.append(&err);
    let row = gtk::Box::new(Orientation::Horizontal, 8);
    row.set_halign(Align::End);
    let cancel = gtk::Button::with_label(&tr!("Cancel"));
    let ok = gtk::Button::with_label(&tr!("Save"));
    ok.add_css_class("suggested-action");
    row.append(&cancel);
    row.append(&ok);
    b.append(&row);
    win.set_child(Some(&b));
    let w = win.clone();
    cancel.connect_clicked(move |_| w.close());
    let (w, e) = (win.clone(), ed.clone());
    ok.connect_clicked(move |_| {
        let buf = tv.buffer();
        let text = buf.text(&buf.start_iter(), &buf.end_iter(), false);
        match serde_json::from_str::<Value>(&text) {
            Ok(Value::Object(m)) => {
                e.edit(|doc, page| doc.set_settings_json(page, slot, &[], m));
                w.close();
            }
            Ok(_) => err.set_text(&tr!("A JSON object { … } is expected")),
            Err(x) => err.set_text(&format!("{}\n{x}", tr!("A JSON object { … } is expected"))),
        }
    });
    win.present();
}

// ===================================================================== Kindaktionen
fn children(ed: &Rc<Editor>, parent: &gtk::Box, slot: Slot, kind: ChildKind, labels: &[String], data: &Map<String, Value>) {
    let head = label(&tr!("INCLUDED ACTIONS"), &["section"]);
    head.set_margin_top(10);
    parent.append(&head);
    let mut data = data.clone();
    let entries = catalog::child_lists(&mut data).cloned().unwrap_or_default();
    let with_delay = data.get("UUID").and_then(Value::as_str).map(|u| u.starts_with("com.elgato.streamdeck.multiactions")).unwrap_or(false);
    for (g, lab) in labels.iter().enumerate() {
        let row = gtk::Box::new(Orientation::Horizontal, 8);
        row.append(&label(lab, &["muted"]));
        parent.append(&row);
        match kind {
            ChildKind::Slots => {
                let child = entries.get(g).filter(|c| c.get("UUID").is_some()).and_then(Value::as_object);
                row.append(&label(&child.map(catalog::display_name).unwrap_or_else(|| "–".into()), &[]));
                let e = ed.clone();
                row.append(&catalog_button(&tr!("Choose"), false, move |uuid| {
                    set_children(&e, slot, |list| {
                        if let Some(x) = list.get_mut(g) {
                            *x = Value::Object(catalog::new_action(&uuid));
                        }
                    })
                }));
                let clear_b = gtk::Button::with_label(&tr!("Clear"));
                clear_b.set_sensitive(child.is_some());
                let e = ed.clone();
                clear_b.connect_clicked(move |_| set_children(&e, slot, |list| {
                    if let Some(x) = list.get_mut(g) {
                        *x = json!({});
                    }
                }));
                row.append(&clear_b);
            }
            ChildKind::Groups => {
                let group: Vec<Value> = entries.get(g).and_then(|e| e.get("Actions")).and_then(Value::as_array).cloned().unwrap_or_default();
                let e = ed.clone();
                row.append(&catalog_button(&tr!("Add"), with_delay, move |uuid| {
                    set_children(&e, slot, |list| {
                        if let Some(arr) = list.get_mut(g).and_then(|x| x.get_mut("Actions")).and_then(Value::as_array_mut) {
                            arr.push(Value::Object(catalog::new_action(&uuid)));
                        }
                    })
                }));
                let lb = gtk::Box::new(Orientation::Vertical, 2);
                lb.set_margin_start(20);
                for (i, a) in group.iter().enumerate() {
                    let r = gtk::Box::new(Orientation::Horizontal, 8);
                    let name = a.as_object().map(child_label).unwrap_or_default();
                    let l = label(&format!("{}. {name}", i + 1), &[]);
                    l.set_hexpand(true);
                    r.append(&l);
                    for (icon, what) in [("go-up-symbolic", 0), ("go-down-symbolic", 1), ("user-trash-symbolic", 2)] {
                        let b = super::util::icon_button(icon, &match what {
                            0 => tr!("Up"),
                            1 => tr!("Down"),
                            _ => tr!("Remove"),
                        });
                        b.set_sensitive(match what {
                            0 => i > 0,
                            1 => i + 1 < group.len(),
                            _ => true,
                        });
                        let e = ed.clone();
                        b.connect_clicked(move |_| set_children(&e, slot, |list| {
                            if let Some(arr) = list.get_mut(g).and_then(|x| x.get_mut("Actions")).and_then(Value::as_array_mut) {
                                match what {
                                    0 if i > 0 => arr.swap(i, i - 1),
                                    1 if i + 1 < arr.len() => arr.swap(i, i + 1),
                                    2 if i < arr.len() => {
                                        arr.remove(i);
                                    }
                                    _ => {}
                                }
                            }
                        }));
                        r.append(&b);
                    }
                    lb.append(&r);
                }
                parent.append(&lb);
            }
        }
    }
}

fn child_label(a: &Map<String, Value>) -> String {
    if a.get("UUID").and_then(Value::as_str) == Some("com.elgato.streamdeck.multiactions.delay") {
        let ms = a.get("Settings").and_then(|s| s.get("delay")).and_then(Value::as_i64).unwrap_or(0);
        return format!("{} ({ms} ms)", catalog::display_name(a));
    }
    catalog::display_name(a)
}

/// Kindaktionen ändern (ein Rückgängig-Schritt) und neu aufbauen.
fn set_children(ed: &Rc<Editor>, slot: Slot, f: impl FnOnce(&mut Vec<Value>)) {
    ed.edit(|doc, page| {
        doc.checkpoint(None);
        if let Some(a) = doc.action_mut(page, slot, &[]) {
            if let Some(list) = catalog::child_lists(a) {
                f(list);
            }
        }
        doc.changed();
    });
}

/// Knopf mit Menü aller Aktionen nach Kategorie.
fn catalog_button(text: &str, with_delay: bool, on: impl Fn(String) + Clone + 'static) -> gtk::MenuButton {
    let mb = gtk::MenuButton::new();
    mb.set_label(text);
    let pop = gtk::Popover::new();
    pop.set_has_arrow(false);
    let list = popover_box(&pop);
    let sc = gtk::ScrolledWindow::new();
    sc.set_hscrollbar_policy(gtk::PolicyType::Never);
    sc.set_propagate_natural_height(true);
    sc.set_max_content_height(380);
    sc.set_min_content_width(260);
    pop.set_child(Some(&sc));
    sc.set_child(Some(&list));
    let freewave = freestream_deck::system::have("freewave");
    for cat in catalog::categories(freewave) {
        let items: Vec<&ActionType> = catalog::catalog_for(freewave)
            .into_iter()
            .filter(|t| t.category.id() == cat.id() && t.supports(catalog::KEY))
            .filter(|t| with_delay || t.uuid != "com.elgato.streamdeck.multiactions.delay")
            .filter(|t| t.children.is_none())
            .collect();
        if items.is_empty() {
            continue;
        }
        let h = label(&cat.get().to_uppercase(), &["section"]);
        h.set_margin_start(8);
        h.set_margin_top(6);
        list.append(&h);
        for t in items {
            let (glyph, color) = catalog::action_look(&t.uuid, t.category.id());
            let b = gtk::Button::new();
            b.add_css_class("title-drop");
            let row = gtk::Box::new(Orientation::Horizontal, 8);
            row.add_css_class("menu-row");
            row.append(&glyph_tile(glyph, color, 20));
            row.append(&label(&t.name(), &[]));
            b.set_child(Some(&row));
            let (on, uuid) = (on.clone(), t.uuid.clone());
            b.connect_clicked(move |b| {
                close_popover(b);
                on(uuid.clone());
            });
            list.append(&b);
        }
    }
    mb.set_popover(Some(&pop));
    mb
}

fn open_property_inspector(ed: &Rc<Editor>, slot: Slot) {
    use freestream_deck::plugins::{browser_command, property_inspector_url, PiSlot};
    let (Some(page), Some(pid)) = (ed.page_uuid(), ed.state.borrow().profile.clone()) else { return };
    let pi = match slot {
        Slot::Dial(i) => PiSlot::Dial(i),
        Slot::Key(c, r) => PiSlot::Key(c, r),
        _ => return,
    };
    match property_inspector_url(&pid, &page, pi) {
        Some(url) => {
            let cmd = browser_command(&url);
            if let Some((exe, args)) = cmd.split_first() {
                let _ = std::process::Command::new(exe).args(args).spawn();
            }
        }
        None => super::editor::info(&ed.win, &tr!("Settings page…"), &tr!("FreeDeck must be running for this (menu: “FreeDeck” or `freedeck`), because it runs the plugins.")),
    }
}

/// Home-Assistant-Entität: Eingabefeld und „Auswählen …“ mit der Liste aus Home Assistant (im Hintergrund
/// geladen, nach den erlaubten Domains gefiltert), wie `hassui.entity_field`.
fn entity_field(ed: &Rc<Editor>, f: &Field, s: &Map<String, Value>, set: impl Fn(String, Value) + Clone + 'static) -> gtk::Widget {
    let key = f.key.clone();
    let row = gtk::Box::new(Orientation::Horizontal, 4);
    let e = gtk::Entry::new();
    e.set_hexpand(true);
    e.set_text(&s.get(&key).map(value_text).unwrap_or_default());
    let (set2, k2) = (set.clone(), key.clone());
    e.connect_changed(move |e| set2(k2.clone(), Value::String(e.text().to_string())));
    row.append(&e);
    let mb = gtk::MenuButton::new();
    mb.set_label(&tr!("Choose …"));
    let pop = gtk::Popover::new();
    pop.set_has_arrow(false);
    let list = popover_box(&pop);
    let search = gtk::SearchEntry::new();
    list.append(&search);
    let items = gtk::Box::new(Orientation::Vertical, 0);
    let sc = gtk::ScrolledWindow::new();
    sc.set_child(Some(&items));
    sc.set_min_content_width(320);
    sc.set_min_content_height(300);
    sc.set_hscrollbar_policy(gtk::PolicyType::Never);
    list.append(&sc);
    mb.set_popover(Some(&pop));
    row.append(&mb);
    let domains = f.domains.clone();
    let all: Rc<std::cell::RefCell<Vec<(String, String)>>> = Rc::new(std::cell::RefCell::new(Vec::new()));
    let fill = {
        let (items, all, search, e) = (items.clone(), all.clone(), search.clone(), e.clone());
        Rc::new(move || {
            while let Some(c) = items.first_child() {
                items.remove(&c);
            }
            let q = search.text().to_lowercase();
            for (id, name) in all.borrow().iter().filter(|(id, n)| q.is_empty() || id.to_lowercase().contains(&q) || n.to_lowercase().contains(&q)).take(300) {
                let b = menu_row(&format!("{name}  ·  {id}"), None, &[]);
                let (e, id) = (e.clone(), id.clone());
                b.connect_clicked(move |b| {
                    e.set_text(&id);
                    close_popover(b);
                });
                items.append(&b);
            }
        })
    };
    {
        let fill = fill.clone();
        search.connect_search_changed(move |_| fill());
    }
    let cfg = ed.state.borrow().preview.as_ref().map(|c| c.cfg.data.clone()).unwrap_or_default();
    pop.connect_show(move |_| {
        // Liste im Hintergrund holen (Netz), dann hier füllen
        let (tx, rx) = std::sync::mpsc::channel();
        let cfg = cfg.clone();
        std::thread::spawn(move || {
            let _ = tx.send(freestream_deck::hass::client(&cfg).states_all().map_err(|e| e.to_string()));
        });
        let (all, fill, items, domains) = (all.clone(), fill.clone(), items.clone(), domains.clone());
        while let Some(c) = items.first_child() {
            items.remove(&c);
        }
        items.append(&label("…", &["muted"]));
        glib::timeout_add_local(std::time::Duration::from_millis(150), move || match rx.try_recv() {
            Ok(Ok(states)) => {
                let mut v: Vec<(String, String)> = states.iter().filter_map(|st| {
                    let id = st.get("entity_id")?.as_str()?.to_string();
                    let dom = id.split('.').next().unwrap_or("").to_string();
                    if let Some(d) = &domains {
                        if !d.iter().any(|x| *x == dom) {
                            return None;
                        }
                    }
                    let name = st.get("attributes").and_then(|a| a.get("friendly_name")).and_then(Value::as_str).unwrap_or(&id).to_string();
                    Some((id, name))
                }).collect();
                v.sort_by_key(|x| x.1.to_lowercase());
                *all.borrow_mut() = v;
                fill();
                glib::ControlFlow::Break
            }
            Ok(Err(err)) => {
                while let Some(c) = items.first_child() {
                    items.remove(&c);
                }
                let l = label(&err, &["muted"]);
                l.set_wrap(true);
                items.append(&l);
                glib::ControlFlow::Break
            }
            Err(std::sync::mpsc::TryRecvError::Empty) => glib::ControlFlow::Continue,
            Err(_) => glib::ControlFlow::Break,
        });
    });
    row.upcast()
}
