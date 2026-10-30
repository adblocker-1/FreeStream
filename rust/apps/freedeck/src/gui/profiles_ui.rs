//! Profile verwalten (Liste in der Kopfzeile): neu, umbenennen, duplizieren, exportieren, löschen.

use gtk4 as gtk;
use gtk::prelude::*;
use gtk::{Align, Orientation};

use freestream_core::editdoc;
use freestream_core::i18n::ngettext;
use freestream_core::models::{model_geometry, model_name};
use freestream_core::tr;
use std::rc::Rc;

use super::editor::{info, Editor};
use super::util::label;

/// Fenster mit Inhalt und Knöpfen unten rechts.
fn dialog(parent: &gtk::ApplicationWindow, title: &str) -> (gtk::Window, gtk::Box, gtk::Box) {
    let win = gtk::Window::builder().title(title).transient_for(parent).modal(true).default_width(460).resizable(false).build();
    let outer = gtk::Box::new(Orientation::Vertical, 14);
    outer.add_css_class("dialog-body");
    for m in [outer.upcast_ref::<gtk::Widget>()] {
        m.set_margin_start(20);
        m.set_margin_end(20);
        m.set_margin_top(18);
        m.set_margin_bottom(16);
    }
    let body = gtk::Box::new(Orientation::Vertical, 8);
    outer.append(&body);
    let buttons = gtk::Box::new(Orientation::Horizontal, 8);
    buttons.set_halign(Align::End);
    outer.append(&buttons);
    win.set_child(Some(&outer));
    (win, body, buttons)
}

/// Frage nach einem Text (Name). `on_ok` bekommt den eingegebenen Text.
pub fn ask_text(parent: &gtk::ApplicationWindow, title: &str, prompt: &str, initial: &str, ok: &str, on_ok: impl Fn(String) + 'static) {
    let (win, body, buttons) = dialog(parent, title);
    body.append(&label(prompt, &["muted"]));
    let e = gtk::Entry::new();
    e.set_text(initial);
    e.set_activates_default(true);
    body.append(&e);
    let cancel = gtk::Button::with_label(&tr!("Cancel"));
    let okb = gtk::Button::with_label(ok);
    okb.add_css_class("suggested-action");
    buttons.append(&cancel);
    buttons.append(&okb);
    win.set_default_widget(Some(&okb));
    let w = win.clone();
    cancel.connect_clicked(move |_| w.close());
    let (w, e2) = (win.clone(), e.clone());
    let on_ok = Rc::new(on_ok);
    okb.connect_clicked(move |_| {
        let t = e2.text().trim().to_string();
        if !t.is_empty() {
            w.close();
            on_ok(t);
        }
    });
    win.present();
    e.grab_focus();
    e.select_region(0, -1);
}

/// Sicherheitsfrage mit rotem Knopf.
pub fn confirm(parent: &gtk::ApplicationWindow, title: &str, text: &str, yes: &str, on_yes: impl Fn() + 'static) {
    let (win, body, buttons) = dialog(parent, title);
    let l = label(text, &[]);
    l.set_wrap(true);
    body.append(&l);
    let cancel = gtk::Button::with_label(&tr!("Cancel"));
    let y = gtk::Button::with_label(yes);
    y.add_css_class("destructive-action");
    buttons.append(&cancel);
    buttons.append(&y);
    let w = win.clone();
    cancel.connect_clicked(move |_| w.close());
    let w = win.clone();
    y.connect_clicked(move |_| {
        w.close();
        on_yes();
    });
    win.present();
}

/// Modell für ein neues Profil: angeschlossenes Gerät, sonst das häufigste Modell der Profile.
fn auto_model(ed: &Rc<Editor>) -> (String, bool) {
    let st = ed.state.borrow();
    if let (Some(serial), Some(d)) = (&st.serial, &st.daemon) {
        if let Some(c) = d.lock().unwrap().controllers.lock().unwrap().get(serial) {
            return (c.lock().unwrap().dev.model(), true);
        }
    }
    let store = st.store.lock().unwrap();
    let mut counts: std::collections::HashMap<String, usize> = std::collections::HashMap::new();
    for p in store.profiles.values().filter(|p| !p.model.is_empty()) {
        *counts.entry(p.model.clone()).or_default() += 1;
    }
    (counts.into_iter().max_by_key(|(_, n)| *n).map(|(m, _)| m).unwrap_or_else(|| "20GBA9901".into()), false)
}

fn after_change(ed: &Rc<Editor>, open: Option<String>) {
    ed.state.borrow().store.lock().unwrap().reload();
    ed.open_profile(open.as_deref());
}

pub fn new_profile(ed: &Rc<Editor>) {
    let (model, plugged) = auto_model(ed);
    let (win, body, buttons) = dialog(&ed.win, &tr!("New profile"));
    body.append(&label(&tr!("New profile"), &["insp-name"]));
    body.append(&label(&if plugged { tr!("For the connected device") } else { tr!("No device connected – the profile is for the model used last") }, &["muted"]));
    body.append(&label(&tr!("Name"), &["section"]));
    let e = gtk::Entry::new();
    e.set_text(&tr!("New profile"));
    e.set_activates_default(true);
    body.append(&e);
    body.append(&label(&if plugged { tr!("DEVICE – DETECTED AUTOMATICALLY") } else { tr!("DEVICE") }, &["section"]));
    let mut sub = if plugged { format!("{} · ", tr!("connected")) } else { String::new() };
    if let Some(g) = model_geometry(&model) {
        sub += &tr!("{cols} × {rows} keys", cols = g.cols, rows = g.rows);
        if g.dials > 0 {
            sub += &format!(" · {}", ngettext("{n} dial", "{n} dials", g.dials as i64).replace("{n}", &g.dials.to_string()));
        }
    }
    let card = gtk::Box::new(Orientation::Horizontal, 12);
    card.add_css_class("card");
    card.append(&super::util::glyph_tile("deck", "#3a4250", 34));
    let col = gtk::Box::new(Orientation::Vertical, 2);
    col.append(&label(&model_name(&model), &[]));
    col.append(&label(&sub, &["muted"]));
    card.append(&col);
    body.append(&card);
    let cancel = gtk::Button::with_label(&tr!("Cancel"));
    let ok = gtk::Button::with_label(&tr!("Create"));
    ok.add_css_class("suggested-action");
    buttons.append(&cancel);
    buttons.append(&ok);
    win.set_default_widget(Some(&ok));
    let w = win.clone();
    cancel.connect_clicked(move |_| w.close());
    let (w, e2, ed2) = (win.clone(), e.clone(), ed.clone());
    ok.connect_clicked(move |_| {
        let name = Some(e2.text().trim().to_string()).filter(|n| !n.is_empty()).unwrap_or_else(|| tr!("New profile"));
        w.close();
        match editdoc::create_profile(&name, &model, None) {
            Ok(root) => after_change(&ed2, profile_id(&root)),
            Err(err) => info(&ed2.win, &tr!("New profile"), &err.to_string()),
        }
    });
    win.present();
    e.grab_focus();
    e.select_region(0, -1);
}

fn profile_id(root: &std::path::Path) -> Option<String> {
    root.file_name()?.to_str()?.strip_suffix(".sdProfile").map(str::to_string)
}

/// Profil öffnen, falls ein anderes offen ist, dann `f` damit ausführen.
fn with_profile(ed: &Rc<Editor>, pid: &str, f: impl FnOnce(&Rc<Editor>)) {
    if ed.state.borrow().profile.as_deref() != Some(pid) {
        ed.open_profile(Some(pid));
    }
    f(ed);
}

pub fn rename(ed: &Rc<Editor>, pid: &str) {
    with_profile(ed, pid, |ed| {
        let Some(name) = ed.state.borrow().doc.as_ref().map(|d| d.name()) else { return };
        let e = ed.clone();
        ask_text(&ed.win, &tr!("Rename profile"), &tr!("New name:"), &name, &tr!("Rename…").trim_end_matches('…').to_string(), move |n| {
            if let Some(d) = e.state.borrow_mut().doc.as_mut() {
                d.rename(&n);
            }
            e.commit();
        });
    });
}

pub fn duplicate(ed: &Rc<Editor>, pid: &str) {
    with_profile(ed, pid, |ed| {
        let Some((name, root)) = ed.state.borrow().doc.as_ref().map(|d| (d.name(), d.root.clone())) else { return };
        let e = ed.clone();
        ask_text(&ed.win, &tr!("Duplicate profile"), &tr!("Name of the copy:"), &tr!("{name} (copy)", name = name), &tr!("Duplicate"), move |n| {
            let _ = e.state.borrow_mut().doc.as_mut().map(|d| d.save());
            match editdoc::duplicate_profile(&root, &n) {
                Ok(dst) => after_change(&e, profile_id(&dst)),
                Err(err) => info(&e.win, &tr!("Duplicate profile"), &err.to_string()),
            }
        });
    });
}

pub fn export(ed: &Rc<Editor>, pid: &str) {
    with_profile(ed, pid, |ed| {
        let Some(name) = ed.state.borrow().doc.as_ref().map(|d| d.name()) else { return };
        let safe: String = name.chars().map(|c| if c.is_alphanumeric() || " -_".contains(c) { c } else { '_' }).collect();
        let safe = if safe.trim().is_empty() { tr!("Profile") } else { safe.trim().to_string() };
        let dlg = gtk::FileChooserNative::new(Some(&tr!("Export profile")), Some(&ed.win), gtk::FileChooserAction::Save, Some(&tr!("Export")), Some(&tr!("Cancel")));
        dlg.set_current_name(&format!("{safe}.streamDeckProfile"));
        let e = ed.clone();
        dlg.connect_response(move |d, resp| {
            if resp != gtk::ResponseType::Accept {
                return;
            }
            let Some(mut path) = d.file().and_then(|f| f.path()) else { return };
            if path.extension().is_none() {
                path.set_extension("streamDeckProfile");
            }
            let res = e.state.borrow_mut().doc.as_mut().map(|doc| doc.export(&path));
            match res {
                Some(Ok(out)) => {
                    e.state.borrow_mut().status = tr!("Exported: {path}", path = out.display());
                    e.refresh();
                }
                Some(Err(err)) => info(&e.win, &tr!("Export profile"), &err.to_string()),
                None => {}
            }
        });
        dlg.show();
    });
}

pub fn delete(ed: &Rc<Editor>, pid: &str) {
    let name = ed.state.borrow().store.lock().unwrap().get(pid).map(|p| p.name.clone()).unwrap_or_default();
    let (e, pid) = (ed.clone(), pid.to_string());
    confirm(&ed.win, &tr!("Delete profile"), &tr!("Permanently delete profile “{name}”?", name = name), &tr!("Delete profile"), move || {
        if e.state.borrow().profile.as_deref() == Some(pid.as_str()) {
            e.state.borrow_mut().doc = None;
        }
        freestream_core::profiles::delete_profile(&pid, None);
        let first = {
            let st = e.state.borrow();
            let mut store = st.store.lock().unwrap();
            store.reload();
            let mut v: Vec<_> = store.profiles.values().map(|p| (p.name.to_lowercase(), p.id.clone())).collect();
            v.sort();
            v.first().map(|x| x.1.clone())
        };
        e.open_profile(first.as_deref());
    });
}

/// Rechtsklick auf ein Profil in der Liste.
pub fn context_menu(ed: &Rc<Editor>, anchor: &gtk::Widget, pid: &str) {
    let pop = gtk::Popover::new();
    pop.set_has_arrow(false);
    pop.set_parent(anchor);
    let list = gtk::Box::new(Orientation::Vertical, 2);
    for m in [list.upcast_ref::<gtk::Widget>()] {
        m.set_margin_start(4);
        m.set_margin_end(4);
        m.set_margin_top(4);
        m.set_margin_bottom(4);
    }
    type Act = fn(&Rc<Editor>, &str);
    let open: Act = |ed, pid| ed.open_profile(Some(pid));
    let items: [(String, &str, Act, bool); 5] = [
        (tr!("Open"), "folder-open-symbolic", open, false),
        (tr!("Rename…"), "document-edit-symbolic", rename, false),
        (tr!("Duplicate"), "edit-copy-symbolic", duplicate, false),
        (tr!("Export…"), "document-save-as-symbolic", export, false),
        (tr!("Delete profile"), "user-trash-symbolic", delete, true),
    ];
    for (text, icon, act, danger) in items {
        if danger {
            list.append(&gtk::Separator::new(Orientation::Horizontal));
        }
        let b = gtk::Button::new();
        b.add_css_class("title-drop");
        let row = gtk::Box::new(Orientation::Horizontal, 10);
        row.add_css_class("menu-row");
        row.append(&gtk::Image::from_icon_name(icon));
        row.append(&label(&text, if danger { &["danger"] } else { &[] }));
        b.set_child(Some(&row));
        let (e, p, pid, anchor) = (ed.clone(), pop.clone(), pid.to_string(), anchor.clone());
        b.connect_clicked(move |_| {
            p.popdown();
            // die Profilliste schließen, bevor ein Dialog kommt
            if let Some(outer) = anchor.ancestor(gtk::Popover::static_type()) {
                outer.downcast::<gtk::Popover>().unwrap().popdown();
            }
            let (e, pid) = (e.clone(), pid.clone());
            gtk::glib::idle_add_local_once(move || act(&e, &pid));
        });
        list.append(&b);
    }
    pop.set_child(Some(&list));
    pop.connect_closed(|p| p.unparent());
    pop.popup();
}
