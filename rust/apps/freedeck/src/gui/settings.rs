//! Einstellungen von FreeDeck: Allgemein (Version, Updates, Autostart, Aussehen, Sprache) und Geräte
//! (Name, aktiv, Helligkeit, Ruhezustand, Profil, Modell, Seriennummer, Firmware).

use gtk4 as gtk;
use gtk::prelude::*;
use gtk::Orientation;

use freestream_core::config::save_keys;
use freestream_core::models::model_name;
use freestream_core::{desktop, i18n, tr};
use serde_json::Value;
use std::cell::RefCell;
use std::rc::Rc;

use super::editor::State;
use super::util::label;

fn row(grid: &gtk::Grid, r: i32, text: &str, w: &impl IsA<gtk::Widget>) {
    let l = label(text, &["field-label"]);
    l.set_xalign(1.0);
    l.set_width_chars(16);
    grid.attach(&l, 0, r, 1, 1);
    grid.attach(w, 1, r, 1, 1);
}

fn grid() -> gtk::Grid {
    let g = gtk::Grid::new();
    g.set_row_spacing(10);
    g.set_column_spacing(14);
    g.set_margin_start(22);
    g.set_margin_end(22);
    g.set_margin_top(18);
    g.set_margin_bottom(18);
    g
}

fn section(text: &str) -> gtk::Label {
    let l = label(&text.to_uppercase(), &["section"]);
    l.set_margin_start(22);
    l.set_margin_top(14);
    l
}

/// Fenster „Einstellungen“ öffnen.
pub fn open(parent: &gtk::ApplicationWindow, state: &Rc<RefCell<State>>) {
    let win = gtk::Window::builder().title(tr!("Settings")).transient_for(parent).modal(true).default_width(820).default_height(620).build();
    let outer = gtk::Box::new(Orientation::Vertical, 0);
    outer.add_css_class("main");
    win.set_child(Some(&outer));
    let stack = gtk::Stack::new();
    let switcher = gtk::StackSwitcher::new();
    switcher.set_stack(Some(&stack));
    switcher.set_margin_top(14);
    switcher.set_margin_start(22);
    switcher.set_halign(gtk::Align::Start);
    outer.append(&switcher);
    stack.set_vexpand(true);
    outer.append(&stack);

    // ------------------------------------------------------------ Allgemein
    let general = gtk::Box::new(Orientation::Vertical, 0);
    general.append(&section(&tr!("Program")));
    let g = grid();
    g.attach(&label(&tr!("Installed version: {version}", version = freestream_core::VERSION), &[]), 0, 0, 2, 1);
    let cfg = freestream_core::config::read_file().unwrap_or_default();
    let updates = gtk::Switch::new();
    updates.set_active(cfg.get("check_updates").and_then(Value::as_bool).unwrap_or(true));
    updates.set_halign(gtk::Align::Start);
    updates.connect_state_set(|_, on| {
        save_keys(&[("check_updates", Value::from(on))]);
        gtk::glib::Propagation::Proceed
    });
    row(&g, 1, &tr!("Check for updates every time FreeDeck starts"), &updates);
    let auto = gtk::Switch::new();
    auto.set_active(desktop::autostart_enabled("freedeck"));
    auto.set_halign(gtk::Align::Start);
    auto.connect_state_set(|_, on| {
        if let Err(e) = desktop::set_autostart("freedeck", on) {
            log::warn!("Autostart: {e}");
        }
        gtk::glib::Propagation::Proceed
    });
    row(&g, 2, &tr!("Start automatically when you log in"), &auto);
    general.append(&g);
    general.append(&section(&tr!("Appearance")));
    let g = grid();
    let modes = [("auto", tr!("Auto")), ("light", tr!("Light")), ("dark", tr!("Dark"))];
    let mode = gtk::DropDown::from_strings(&modes.iter().map(|(_, n)| n.as_str()).collect::<Vec<_>>());
    let cur = cfg.get("appearance").and_then(Value::as_str).unwrap_or("auto").to_string();
    mode.set_selected(modes.iter().position(|(k, _)| *k == cur).unwrap_or(0) as u32);
    mode.connect_selected_notify(move |d| {
        let key = ["auto", "light", "dark"][d.selected() as usize % 3];
        save_keys(&[("appearance", Value::from(key))]);
        if let Some(s) = gtk::Settings::default() {
            s.set_gtk_application_prefer_dark_theme(key != "light");
        }
    });
    row(&g, 0, &tr!("Appearance"), &mode);
    let langs: Vec<&str> = i18n::available();
    let mut names = vec![tr!("System language")];
    names.extend(langs.iter().map(|l| i18n::name(l).to_string()));
    let lang = gtk::DropDown::from_strings(&names.iter().map(String::as_str).collect::<Vec<_>>());
    let cur = cfg.get("language").and_then(Value::as_str).unwrap_or("auto").to_string();
    lang.set_selected(langs.iter().position(|l| *l == cur).map(|i| i + 1).unwrap_or(0) as u32);
    let langs2 = langs.clone();
    lang.connect_selected_notify(move |d| {
        let i = d.selected() as usize;
        let key = if i == 0 { "auto".to_string() } else { langs2[i - 1].to_string() };
        save_keys(&[("language", Value::from(key))]);
    });
    row(&g, 1, &tr!("Language"), &lang);
    let hint = label(&tr!("The new language is used after a restart."), &["muted"]);
    g.attach(&hint, 1, 2, 1, 1);
    general.append(&g);
    stack.add_titled(&general, Some("general"), &tr!("General"));

    // --------------------------------------------------------------- Geräte
    let devices = gtk::Box::new(Orientation::Vertical, 0);
    let ctrls: Vec<_> = state.borrow().daemon.as_ref()
        .map(|d| d.lock().unwrap().controllers.lock().unwrap().iter().map(|(s, c)| (s.clone(), c.clone())).collect())
        .unwrap_or_default();
    if ctrls.is_empty() {
        let l = label(&tr!("No device connected"), &["muted"]);
        l.set_margin_start(22);
        l.set_margin_top(18);
        devices.append(&l);
    }
    for (serial, ctrl) in ctrls {
        let (model, fw, brightness, name, sleep, prof) = {
            let c = ctrl.lock().unwrap();
            let d = c.cfg.device(&serial);
            (c.dev.model(), c.dev.firmware(), c.brightness,
             d.get("name").and_then(Value::as_str).unwrap_or("").to_string(),
             d.get("sleep_after_minutes").and_then(Value::as_i64).unwrap_or_else(|| c.cfg.get_i64("sleep_after_minutes", 0)),
             c.profile.as_ref().map(|p| p.id.clone()))
        };
        devices.append(&section(&model_name(&model)));
        let g = grid();
        let name_e = gtk::Entry::new();
        name_e.set_text(&name);
        name_e.set_placeholder_text(Some(&model_name(&model)));
        let (c2, s2) = (ctrl.clone(), serial.clone());
        name_e.connect_changed(move |e| {
            let mut c = c2.lock().unwrap();
            c.cfg.set_device(&s2, "name", Value::from(e.text().to_string()));
            let _ = c.cfg.save();
        });
        row(&g, 0, &tr!("Device name"), &name_e);
        let scale = gtk::Scale::with_range(Orientation::Horizontal, 0.0, 100.0, 5.0);
        scale.set_value(brightness as f64);
        scale.set_hexpand(true);
        let c2 = ctrl.clone();
        let s2 = serial.clone();
        scale.connect_value_changed(move |s| {
            let mut c = c2.lock().unwrap();
            c.set_brightness(s.value() as u8);
            c.cfg.set_device(&s2, "brightness", Value::from(s.value() as u8));
            let _ = c.cfg.save();
        });
        row(&g, 1, &tr!("Brightness"), &scale);
        let opts = [0i64, 1, 5, 10, 15, 30, 60];
        let labels: Vec<String> = opts.iter().map(|m| if *m == 0 { tr!("Never") } else { tr!("{n} minutes", n = m) }).collect();
        let sl = gtk::DropDown::from_strings(&labels.iter().map(String::as_str).collect::<Vec<_>>());
        sl.set_selected(opts.iter().position(|m| *m == sleep).unwrap_or(0) as u32);
        let (c2, s2) = (ctrl.clone(), serial.clone());
        sl.connect_selected_notify(move |d| {
            let mut c = c2.lock().unwrap();
            c.cfg.set_device(&s2, "sleep_after_minutes", Value::from(opts[d.selected() as usize]));
            let _ = c.cfg.save();
        });
        row(&g, 2, &tr!("Sleep after"), &sl);
        let profiles: Vec<(String, String)> = {
            let st = state.borrow();
            let store = st.store.lock().unwrap();
            store.profiles.values().map(|p| (p.id.clone(), p.name.clone())).collect()
        };
        let pd = gtk::DropDown::from_strings(&profiles.iter().map(|(_, n)| n.as_str()).collect::<Vec<_>>());
        pd.set_selected(prof.and_then(|p| profiles.iter().position(|(i, _)| *i == p)).unwrap_or(0) as u32);
        let (c2, s2) = (ctrl.clone(), serial.clone());
        pd.connect_selected_notify(move |d| {
            if let Some((id, _)) = profiles.get(d.selected() as usize) {
                let mut c = c2.lock().unwrap();
                c.cfg.set_device(&s2, "profile", Value::from(id.clone()));
                let _ = c.cfg.save();
                c.load_profile(Some(id), None);
            }
        });
        row(&g, 3, &tr!("Profile"), &pd);
        row(&g, 4, &tr!("Model"), &label(&model_name(&model), &[]));
        row(&g, 5, &tr!("Serial number"), &label(&serial, &[]));
        row(&g, 6, &tr!("Firmware"), &label(if fw.is_empty() { "–" } else { &fw }, &[]));
        devices.append(&g);
    }
    let sc = gtk::ScrolledWindow::new();
    sc.set_child(Some(&devices));
    stack.add_titled(&sc, Some("devices"), &tr!("Devices"));

    let close = gtk::Button::with_label(&tr!("Close"));
    close.add_css_class("btn");
    close.add_css_class("primary");
    close.set_halign(gtk::Align::End);
    close.set_margin_end(22);
    close.set_margin_bottom(16);
    let w2 = win.clone();
    close.connect_clicked(move |_| w2.close());
    outer.append(&close);
    win.present();
}
