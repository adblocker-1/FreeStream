//! Aktionsleiste rechts: Suche, Reiter „Tasten | Plugins“, Kategorien zum Auf- und Zuklappen.
//! Die Aktionen kommen aus dem Katalog (`freestream_core::catalog`); jede Zeile lässt sich auf eine
//! Taste ziehen oder per Doppelklick auf die gewählte (sonst erste freie) Taste legen.

use gtk4 as gtk;
use gtk::prelude::*;
use gtk::{gdk, Orientation};

use freestream_core::catalog::{self, ActionType, Text};
use freestream_core::tr;
use std::cell::RefCell;
use std::collections::HashSet;
use std::rc::Rc;

use super::editor::State;
use super::util::{glyph_tile, label};

/// Text im Drag & Drop für eine Aktion aus der Liste.
pub const DRAG_PREFIX: &str = "freedeck-action:";

/// FreeWave-Aktionen nur zeigen, wenn FreeWave installiert ist.
fn with_freewave() -> bool {
    freestream_deck::system::have("freewave")
}

fn action_row(t: &'static ActionType, state: &Rc<RefCell<State>>) -> gtk::Box {
    let r = gtk::Box::new(Orientation::Horizontal, 10);
    r.add_css_class("action-row");
    let (glyph, color) = catalog::action_look(&t.uuid, t.category.id());
    r.append(&glyph_tile(glyph, color, 26));
    let l = label(&t.name(), &[]);
    l.set_ellipsize(gtk::pango::EllipsizeMode::End);
    r.append(&l);
    let desc = t.description();
    if !desc.is_empty() {
        r.set_tooltip_text(Some(&desc));
    }
    // ziehen
    let drag = gtk::DragSource::new();
    drag.set_actions(gdk::DragAction::COPY);
    let uuid = t.uuid.clone();
    drag.connect_prepare(move |_, _, _| Some(gdk::ContentProvider::for_value(&format!("{DRAG_PREFIX}{uuid}").to_value())));
    let (gl, co) = (glyph, color);
    drag.connect_drag_begin(move |src, _| {
        let tile = glyph_tile(gl, co, 40);
        let paintable = gtk::WidgetPaintable::new(Some(&tile));
        src.set_icon(Some(&paintable), 20, 20);
    });
    r.add_controller(drag);
    // Doppelklick: auf die Taste legen
    let click = gtk::GestureClick::new();
    let (st, uuid) = (state.clone(), t.uuid.clone());
    click.connect_pressed(move |_, n, _, _| {
        if n == 2 {
            let cb = st.borrow().on_add.clone();
            if let Some(cb) = cb {
                cb(uuid.clone());
            }
        }
    });
    r.add_controller(click);
    r
}

pub fn build(state: &Rc<RefCell<State>>) -> gtk::Box {
    let side = gtk::Box::new(Orientation::Vertical, 10);
    side.add_css_class("side");
    side.set_size_request(310, -1);
    side.set_hexpand(false); // die Suche dehnt sich – die Leiste selbst bleibt 310 px breit
    let pad = gtk::Box::new(Orientation::Vertical, 10);
    pad.set_margin_start(12);
    pad.set_margin_end(12);
    pad.set_margin_top(12);
    pad.set_margin_bottom(12);
    pad.set_vexpand(true);
    side.append(&pad);

    let srow = gtk::Box::new(Orientation::Horizontal, 8);
    let search = gtk::SearchEntry::new();
    search.set_placeholder_text(Some(&tr!("Search")));
    search.add_css_class("search");
    search.set_hexpand(true);
    srow.append(&search);
    let all = gtk::Button::from_icon_name("view-list-symbolic");
    all.add_css_class("icon-btn");
    srow.append(&all);
    pad.append(&srow);

    let tabs = gtk::Box::new(Orientation::Horizontal, 0);
    tabs.add_css_class("tabs");
    tabs.set_homogeneous(true);
    let keys = gtk::ToggleButton::with_label(&tr!("Keys"));
    let plugins = gtk::ToggleButton::with_label(&tr!("Plugins"));
    plugins.set_group(Some(&keys));
    keys.set_active(true);
    for t in [&keys, &plugins] {
        t.add_css_class("tab");
        tabs.append(t);
    }
    pad.append(&tabs);

    let list = gtk::Box::new(Orientation::Vertical, 0);
    let sc = gtk::ScrolledWindow::new();
    sc.set_child(Some(&list));
    sc.set_vexpand(true);
    sc.set_hscrollbar_policy(gtk::PolicyType::Never);
    pad.append(&sc);
    let hint = label(&tr!("Drag onto a key or double-click"), &["muted"]);
    hint.set_wrap(true);
    pad.append(&hint);

    let freewave = with_freewave();
    let open: Rc<RefCell<HashSet<String>>> = Rc::new(RefCell::new(HashSet::new()));
    let fill: Rc<RefCell<Option<Box<dyn Fn()>>>> = Rc::new(RefCell::new(None));
    {
        let (list, search, open, fill2, plugins, state) = (list.clone(), search.clone(), open.clone(), fill.clone(), plugins.clone(), state.clone());
        *fill.borrow_mut() = Some(Box::new(move || {
            while let Some(c) = list.first_child() {
                list.remove(&c);
            }
            if plugins.is_active() {
                list.append(&label(&tr!("No plugins installed"), &["muted"]));
                return;
            }
            let q = search.text().to_lowercase();
            let types = catalog::catalog_for(freewave);
            for cat in catalog::categories(freewave) {
                let cat_label = cat.get();
                let items: Vec<&'static ActionType> = types
                    .iter()
                    .copied()
                    .filter(|t| t.category.id() == cat.id())
                    .filter(|t| q.is_empty() || t.name().to_lowercase().contains(&q) || cat_label.to_lowercase().contains(&q))
                    .collect();
                if items.is_empty() {
                    continue;
                }
                let key = cat.id().to_string();
                let is_open = !q.is_empty() || open.borrow().contains(&key);
                let head = gtk::Button::new();
                head.add_css_class("title-drop");
                let row = gtk::Box::new(Orientation::Horizontal, 12);
                row.add_css_class("cat-row");
                let chev = gtk::Image::from_icon_name(if is_open { "pan-down-symbolic" } else { "pan-end-symbolic" });
                chev.add_css_class("muted");
                row.append(&chev);
                let (glyph, color) = catalog::category_look(cat.id());
                row.append(&glyph_tile(glyph, color, 24));
                row.append(&label(&cat_label, &["cat-name"]));
                head.set_child(Some(&row));
                let (open, fill3) = (open.clone(), fill2.clone());
                head.connect_clicked(move |_| {
                    {
                        let mut o = open.borrow_mut();
                        if !o.remove(&key) {
                            o.insert(key.clone());
                        }
                    }
                    if let Some(f) = fill3.borrow().as_ref() {
                        f();
                    }
                });
                list.append(&head);
                if is_open {
                    for t in items {
                        list.append(&action_row(t, &state));
                    }
                }
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
    {
        let run = run.clone();
        keys.connect_toggled(move |_| run());
    }
    {
        let (open, run) = (open.clone(), run.clone());
        all.connect_clicked(move |_| {
            {
                let mut o = open.borrow_mut();
                if o.is_empty() {
                    o.extend(catalog::categories(freewave).iter().map(|c: &Text| c.id().to_string()));
                } else {
                    o.clear();
                }
            }
            run();
        });
    }
    side
}
