//! Einstellungen von FreeWave (settingsui.SettingsDialog ohne die FreeDeck-Reiter): Programm (Version,
//! Autostart), Sprache, Mixer einrichten/entfernen und den Einrichtungsassistenten erneut öffnen.

use std::rc::Rc;

use gtk::prelude::*;
use gtk::Align;
use gtk4 as gtk;

use freestream_core::config::save_keys;
use freestream_core::{desktop, i18n, tr};
use serde_json::Value;

use super::widgets::{self, hbox, label, vbox, Switch};
use super::{setup, Win};

fn card(body: &gtk::Box, title: &str) -> gtk::Box {
    widgets::field(body, title);
    let c = widgets::card(&[]);
    c.set_margin_bottom(16);
    body.append(&c);
    c
}

pub fn open(w: &Rc<Win>) {
    let d = widgets::dialog(w.win.upcast_ref(), &tr!("Settings"), 500, 640);
    let b = vbox(0);
    d.body.append(&widgets::scroll(&b));
    widgets::header(&b, &tr!("Settings"), &tr!("These settings apply to FreeDeck and FreeWave."), Some(("gear", super::theme::ACCENT)), 45);

    // Programm
    let c = card(&b, &tr!("Program"));
    c.append(&label(&tr!("Installed version: {version}", version = freestream_core::VERSION), &[]));
    let row = hbox(10);
    row.set_margin_top(12);
    let sw = Switch::new();
    sw.area.set_valign(Align::Start);
    sw.set(desktop::autostart_enabled("freewave"));
    let w2 = w.clone();
    sw.on_toggle(move |on| {
        if let Err(e) = desktop::set_autostart("freewave", on) {
            w2.status(&e.to_string());
        }
    });
    row.append(&sw.area);
    let txt = vbox(2);
    txt.append(&label(&tr!("Start automatically when you log in"), &[]));
    txt.append(&widgets::wrap(&tr!("{app} then runs in the background and is ready as soon as your desktop appears.", app = "FreeWave"), &["muted", "small"], 45));
    row.append(&txt);
    c.append(&row);

    // Sprache
    let c = card(&b, &tr!("Appearance"));
    c.append(&label(&tr!("Language"), &[]));
    let langs: Vec<&str> = i18n::available();
    let mut names = vec![tr!("System language")];
    names.extend(langs.iter().map(|l| i18n::name(l).to_string()));
    let dd = gtk::DropDown::from_strings(&names.iter().map(String::as_str).collect::<Vec<_>>());
    dd.set_margin_top(6);
    let cur = freestream_core::config::read_file().ok().and_then(|c| c.get("language").and_then(Value::as_str).map(str::to_string))
        .unwrap_or_else(|| "auto".into());
    dd.set_selected(langs.iter().position(|l| *l == cur).map(|i| i + 1).unwrap_or(0) as u32);
    let w2 = w.clone();
    dd.connect_selected_notify(move |d| {
        let i = d.selected() as usize;
        let key = if i == 0 { "auto".to_string() } else { langs[i - 1].to_string() };
        save_keys(&[("language", Value::from(key))]);
        // wie in der Python-Version: Neustart anbieten
        ask_restart(&w2, &tr!("{app} will be shown in {language} after a restart. Restart now?", app = "FreeWave", language = names[i]));
    });
    c.append(&dd);

    // Mixer
    let active = w.snap.borrow().active;
    let c = card(&b, &tr!("Mixer"));
    c.append(&widgets::wrap(&if active {
        tr!("Remove mixer: the FreeWave channels and mixes disappear, apps play directly on your device again. Your settings are kept.")
    } else {
        tr!("The mixer is off. Set it up to give every app its own channel.")
    }, &["muted", "small"], 50));
    let mb = if active { widgets::button(&tr!("Remove mixer"), Some("close"), "danger-btn") } else { widgets::button(&tr!("Set up mixer"), Some("wave"), "primary") };
    mb.set_halign(Align::Start);
    mb.set_margin_top(10);
    let (w2, win) = (w.clone(), d.win.clone());
    mb.connect_clicked(move |_| {
        win.close();
        w2.mixer_enable(!active);
    });
    c.append(&mb);

    // Einrichtung
    let c = card(&b, &tr!("Setup wizard"));
    c.append(&widgets::wrap(&tr!("Goes through the first-start setup again – your current settings are shown and kept until you change them."), &["muted", "small"], 50));
    let sb = widgets::button(&tr!("Open setup wizard"), Some("check"), "");
    sb.set_halign(Align::Start);
    sb.set_margin_top(10);
    let (w2, win) = (w.clone(), d.win.clone());
    sb.connect_clicked(move |_| {
        win.close();
        setup::open(&w2);
    });
    c.append(&sb);

    let close = widgets::button(&tr!("Close"), None, "primary");
    d.buttons.append(&close);
    let win = d.win.clone();
    close.connect_clicked(move |_| win.close());
    d.win.present();
}

/// Neustart anbieten („Neu starten“ / „Später“) – wie in FreeDeck.
fn ask_restart(w: &Rc<Win>, text: &str) {
    let d = widgets::dialog(w.win.upcast_ref(), "FreeWave", 420, -1);
    d.body.append(&widgets::wrap(text, &[], 50));
    let later = widgets::button(&tr!("Later"), None, "");
    let now = widgets::button(&tr!("Restart"), None, "primary");
    d.buttons.append(&later);
    d.buttons.append(&now);
    let win = d.win.clone();
    later.connect_clicked(move |_| win.close());
    let w2 = w.clone();
    now.connect_clicked(move |_| super::restart(&w2.app));
    d.win.present();
}
