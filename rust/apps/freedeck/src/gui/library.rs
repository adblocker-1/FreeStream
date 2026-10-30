//! Aktionsleiste rechts: Suche, Reiter „Tasten | Plugins“, Kategorien zum Auf- und Zuklappen.
//! (Bis der Aktionskatalog aus der Python-Version übernommen ist, steht hier eine kurze Liste.)

use gtk4 as gtk;
use gtk::prelude::*;
use gtk::Orientation;

use freestream_core::tr;
use std::cell::RefCell;
use std::collections::HashSet;
use std::rc::Rc;

use super::editor::State;
use super::util::label;

/// (Kategorie, Symbol, [(UUID, Name)])
fn entries() -> Vec<(String, &'static str, Vec<(&'static str, String)>)> {
    vec![
        (tr!("System"), "input-keyboard-symbolic", vec![
            ("com.elgato.streamdeck.system.hotkey", tr!("Hotkey")),
            ("com.elgato.streamdeck.system.hotkeyswitch", tr!("Hotkey Switch")),
            ("com.elgato.streamdeck.system.text", tr!("Text")),
            ("com.elgato.streamdeck.system.open", tr!("Open")),
            ("com.elgato.streamdeck.system.openapp", tr!("Open Application")),
            ("com.elgato.streamdeck.system.close", tr!("Close Application")),
            ("com.elgato.streamdeck.system.website", tr!("Website")),
            ("com.elgato.streamdeck.system.multimedia", tr!("Multimedia")),
            ("com.elgato.streamdeck.system.timer", tr!("Timer")),
        ]),
        (tr!("Navigation"), "folder-symbolic", vec![
            ("com.elgato.streamdeck.profile.openchild", tr!("Create Folder")),
            ("com.elgato.streamdeck.profile.backtoparent", tr!("Parent Folder")),
            ("com.elgato.streamdeck.page.next", tr!("Next Page")),
            ("com.elgato.streamdeck.page.previous", tr!("Previous Page")),
            ("com.elgato.streamdeck.page.goto", tr!("Go to Page")),
            ("com.elgato.streamdeck.page.indicator", tr!("Page Indicator")),
        ]),
        ("FreeDeck".into(), "emblem-system-symbolic", vec![
            ("com.elgato.streamdeck.profile.rotate", tr!("Switch Profile")),
            ("com.elgato.streamdeck.system.keybrightness", tr!("Brightness")),
            ("com.elgato.streamdeck.system.sleep", tr!("Sleep")),
        ]),
        (tr!("Multi Action"), "view-dual-symbolic", vec![
            ("com.elgato.streamdeck.multiactions.routine", tr!("Multi Action")),
            ("com.elgato.streamdeck.multiactions.routine2", tr!("Multi Action Switch")),
        ]),
        (tr!("Soundboard"), "audio-x-generic-symbolic", vec![
            ("com.elgato.streamdeck.soundboard.playaudio", tr!("Play Audio")),
            ("com.elgato.streamdeck.soundboard.stopaudioplay", tr!("Stop Audio")),
        ]),
        (tr!("Clock & timer"), "alarm-symbolic", vec![
            ("com.lindeck.clock.clock", tr!("Clock")),
            ("com.lindeck.clock.countdown", tr!("Countdown")),
            ("com.lindeck.clock.stopwatch", tr!("Stopwatch")),
        ]),
        (tr!("System monitor"), "utilities-system-monitor-symbolic", vec![
            ("com.lindeck.sysmon.cpu", tr!("CPU")),
            ("com.lindeck.sysmon.memory", tr!("Memory")),
        ]),
    ]
}

/// Kategorie einer Aktion (für die Überschrift der Eigenschaften).
pub fn category_of(uuid: &str) -> String {
    entries().into_iter().find(|(_, _, items)| items.iter().any(|(u, _)| *u == uuid)).map(|(c, ..)| c).unwrap_or_default()
}

pub fn build(_state: &Rc<RefCell<State>>) -> gtk::Box {
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
    let all = super::util::icon_button("view-list-symbolic", &tr!("Open or close all categories"));
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

    let open: Rc<RefCell<HashSet<String>>> = Rc::new(RefCell::new(HashSet::new()));
    let fill: Rc<RefCell<Option<Box<dyn Fn()>>>> = Rc::new(RefCell::new(None));
    {
        let (list, search, open, fill2, plugins) = (list.clone(), search.clone(), open.clone(), fill.clone(), plugins.clone());
        *fill.borrow_mut() = Some(Box::new(move || {
            while let Some(c) = list.first_child() {
                list.remove(&c);
            }
            if plugins.is_active() {
                list.append(&label(&tr!("No plugins installed"), &["muted"]));
                return;
            }
            let q = search.text().to_lowercase();
            for (cat, icon, items) in entries() {
                let items: Vec<_> = items.into_iter().filter(|(_, n)| q.is_empty() || n.to_lowercase().contains(&q) || cat.to_lowercase().contains(&q)).collect();
                if items.is_empty() {
                    continue;
                }
                let is_open = !q.is_empty() || open.borrow().contains(&cat);
                let head = gtk::Button::new();
                head.add_css_class("title-drop");
                let row = gtk::Box::new(Orientation::Horizontal, 12);
                row.add_css_class("cat-row");
                let chev = gtk::Image::from_icon_name(if is_open { "pan-down-symbolic" } else { "pan-end-symbolic" });
                chev.add_css_class("muted");
                row.append(&chev);
                let img = gtk::Image::from_icon_name(icon);
                img.set_pixel_size(20);
                row.append(&img);
                row.append(&label(&cat, &["cat-name"]));
                head.set_child(Some(&row));
                let (open, fill3, cat2) = (open.clone(), fill2.clone(), cat.clone());
                head.connect_clicked(move |_| {
                    {
                        let mut o = open.borrow_mut();
                        if !o.remove(&cat2) {
                            o.insert(cat2.clone());
                        }
                    }
                    if let Some(f) = fill3.borrow().as_ref() {
                        f();
                    }
                });
                list.append(&head);
                if is_open {
                    for (_uuid, name) in items {
                        let r = gtk::Box::new(Orientation::Horizontal, 10);
                        r.add_css_class("action-row");
                        let ic = gtk::Image::from_icon_name(icon);
                        ic.set_pixel_size(16);
                        r.append(&ic);
                        r.append(&label(&name, &[]));
                        list.append(&r);
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
                    o.extend(entries().into_iter().map(|(c, ..)| c));
                } else {
                    o.clear();
                }
            }
            run();
        });
    }
    side
}
