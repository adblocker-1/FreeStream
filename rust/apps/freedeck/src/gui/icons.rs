//! Icon-Bibliothek („+“ unter der Taste): vordefinierte, selbst gezeichnete Tastenbilder suchen und
//! zuweisen. Aufbau wie in der Python-Version: Suche oben, darunter die Pakete (aufklappbar, mit Anzahl),
//! unten links „Eigenes Bild …“, unten rechts Zoom.

use gtk4 as gtk;
use gtk::prelude::*;
use gtk::{Align, Orientation};

use freestream_core::editdoc::Slot;
use freestream_core::tr;
use freestream_deck::iconlib;
use std::cell::RefCell;
use std::collections::HashSet;
use std::rc::Rc;

use super::editor::Editor;
use super::util::{label, texture};

const SIZES: [u32; 3] = [48, 64, 84];

fn pick(ed: &Rc<Editor>, slot: Slot, idx: usize, name: &str, style: &str) {
    let img = iconlib::render(name, style);
    let tmp = std::env::temp_dir().join(format!("freedeck-icon-{}-{name}.png", std::process::id()));
    if img.save(&tmp).is_ok() {
        super::inspector::set_icon_file(ed, slot, idx, &tmp);
        let _ = std::fs::remove_file(&tmp);
    }
}

pub fn open_library(ed: &Rc<Editor>, slot: Slot, idx: usize) {
    let win = gtk::Window::builder().title(tr!("Icon library")).transient_for(&ed.win).modal(true).default_width(460).default_height(600).build();
    let outer = gtk::Box::new(Orientation::Vertical, 0);
    win.set_child(Some(&outer));
    let search = gtk::SearchEntry::new();
    search.set_placeholder_text(Some(&tr!("Search icons …")));
    search.set_margin_start(16);
    search.set_margin_end(16);
    search.set_margin_top(12);
    search.set_margin_bottom(12);
    outer.append(&search);
    let list = gtk::Box::new(Orientation::Vertical, 0);
    let sc = gtk::ScrolledWindow::new();
    sc.set_child(Some(&list));
    sc.set_vexpand(true);
    sc.set_hscrollbar_policy(gtk::PolicyType::Never);
    outer.append(&sc);
    let foot = gtk::Box::new(Orientation::Horizontal, 6);
    for m in [foot.upcast_ref::<gtk::Widget>()] {
        m.set_margin_start(12);
        m.set_margin_end(12);
        m.set_margin_top(10);
        m.set_margin_bottom(10);
    }
    let own = gtk::Button::with_label(&tr!("Own image …"));
    own.add_css_class("flat");
    foot.append(&own);
    let spacer = gtk::Box::new(Orientation::Horizontal, 0);
    spacer.set_hexpand(true);
    foot.append(&spacer);
    let zout = gtk::Button::from_icon_name("zoom-out-symbolic");
    let zin = gtk::Button::from_icon_name("zoom-in-symbolic");
    foot.append(&zout);
    foot.append(&zin);
    outer.append(&foot);

    let size_idx = Rc::new(RefCell::new(1usize));
    let open: Rc<RefCell<HashSet<&'static str>>> = Rc::new(RefCell::new(HashSet::from(["color"])));
    let fill: Rc<RefCell<Option<Box<dyn Fn()>>>> = Rc::new(RefCell::new(None));
    {
        let (list, search, size_idx, open, fill2, ed, win2, zin, zout) =
            (list.clone(), search.clone(), size_idx.clone(), open.clone(), fill.clone(), ed.clone(), win.clone(), zin.clone(), zout.clone());
        *fill.borrow_mut() = Some(Box::new(move || {
            while let Some(c) = list.first_child() {
                list.remove(&c);
            }
            let q = search.text().to_string();
            let searching = !q.trim().is_empty();
            let found = iconlib::search(&q);
            let t = SIZES[*size_idx.borrow()];
            zin.set_sensitive(*size_idx.borrow() + 1 < SIZES.len());
            zout.set_sensitive(*size_idx.borrow() > 0);
            for style in iconlib::STYLES {
                let expanded = searching || open.borrow().contains(style);
                let head = gtk::Button::new();
                head.add_css_class("title-drop");
                let row = gtk::Box::new(Orientation::Horizontal, 10);
                row.add_css_class("cat-row");
                row.append(&gtk::Image::from_icon_name(if expanded { "pan-down-symbolic" } else { "pan-end-symbolic" }));
                row.append(&label(&iconlib::style_title(style), &["cat-name"]));
                row.append(&label(&format!("({})", found.len()), &["faint"]));
                head.set_child(Some(&row));
                let (open, fill3, s2) = (open.clone(), fill2.clone(), search.clone());
                head.connect_clicked(move |_| {
                    if !s2.text().trim().is_empty() {
                        return;
                    }
                    {
                        let mut o = open.borrow_mut();
                        if !o.remove(style) {
                            o.insert(style);
                        }
                    }
                    if let Some(f) = fill3.borrow().as_ref() {
                        f();
                    }
                });
                list.append(&head);
                if expanded && !found.is_empty() {
                    let flow = gtk::FlowBox::new();
                    flow.set_selection_mode(gtk::SelectionMode::None);
                    flow.set_max_children_per_line(30);
                    flow.set_row_spacing(6);
                    flow.set_column_spacing(6);
                    flow.set_margin_start(12);
                    flow.set_margin_end(12);
                    flow.set_margin_top(10);
                    flow.set_margin_bottom(10);
                    flow.set_homogeneous(true);
                    for name in &found {
                        let img = iconlib::rounded_tile(&iconlib::render_sized(name, style, t), t as f32 * 0.18, (style == "black").then_some([70, 70, 70]));
                        let pic = gtk::Picture::for_paintable(&texture(&img));
                        pic.set_size_request(t as i32, t as i32);
                        pic.set_can_shrink(false);
                        let b = gtk::Button::new();
                        b.add_css_class("icon-cell");
                        b.set_child(Some(&pic));
                        b.set_tooltip_text(Some(&iconlib::label(name)));
                        b.set_halign(Align::Center);
                        let (ed, w, n) = (ed.clone(), win2.clone(), name.to_string());
                        b.connect_clicked(move |_| {
                            w.close();
                            pick(&ed, slot, idx, &n, style);
                        });
                        flow.insert(&b, -1);
                    }
                    list.append(&flow);
                }
            }
            if searching && found.is_empty() {
                let l = label(&tr!("No icons found"), &["muted"]);
                l.set_halign(Align::Center);
                l.set_margin_top(24);
                list.append(&l);
            }
        }));
    }
    let run = {
        let fill = fill.clone();
        move || {
            if let Some(f) = fill.borrow().as_ref() {
                f();
            }
        }
    };
    run();
    {
        let run = run.clone();
        search.connect_search_changed(move |_| run());
    }
    for (b, d) in [(&zin, 1i32), (&zout, -1)] {
        let (run, size_idx) = (run.clone(), size_idx.clone());
        b.connect_clicked(move |_| {
            let v = (*size_idx.borrow() as i32 + d).clamp(0, SIZES.len() as i32 - 1) as usize;
            *size_idx.borrow_mut() = v;
            run();
        });
    }
    {
        let (ed, w) = (ed.clone(), win.clone());
        own.connect_clicked(move |_| {
            w.close();
            let e = ed.clone();
            super::inspector::choose_file(&ed.win, &tr!("Choose image file…"), move |p| super::inspector::set_icon_file(&e, slot, idx, &p));
        });
    }
    win.present();
    search.grab_focus();
}
