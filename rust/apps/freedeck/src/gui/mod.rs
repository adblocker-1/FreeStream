//! FreeDeck-Fenster (GTK4) im Aufbau der Deck-Software von Elgato: links oben Gerät und Profil, rechts
//! Glocke, Plugin-Store und Einstellungen; in der Mitte die Tasten mit den Seitenzahlen darunter; unten die
//! Eigenschaften der gewählten Taste; rechts über die ganze Höhe die Aktionen.

mod bg_ui;
mod editor;
mod hotkey;
mod icons;
mod inspector;
mod library;
mod profiles_ui;
mod settings;
mod update_ui;
mod settings_connections;
mod settings_devices;
mod settings_plugins;
mod settings_profiles;
mod settings_style;
mod util;
pub mod virtual_ui;

use gtk4 as gtk;
use gtk::prelude::*;

use freestream_deck::daemon::Daemon;
use std::sync::{Arc, Mutex};

pub const APP_ID: &str = "io.github.freestream.FreeDeck";

/// Fenster starten; der Dienst (Geräte) läuft im selben Prozess in eigenen Threads.
pub fn run(daemon: Option<Arc<Mutex<Daemon>>>) -> gtk::glib::ExitCode {
    let app = gtk::Application::builder().application_id(APP_ID).build();
    app.connect_startup(|_| {
        let css = gtk::CssProvider::new();
        #[allow(deprecated)]
        css.load_from_data(include_str!("style.css"));
        if let Some(display) = gtk::gdk::Display::default() {
            gtk::style_context_add_provider_for_display(&display, &css, gtk::STYLE_PROVIDER_PRIORITY_APPLICATION);
        }
        // Schrift für neue Tasten (Einstellungen › Stil)
        if let Some(style) = freestream_core::config::read_file().ok().and_then(|c| c.get("title_style").and_then(|v| v.as_object()).cloned()) {
            freestream_core::catalog::set_title_defaults(&style);
        }
        // dunkles Systemthema für Menüs und Eingabefelder
        if let Some(s) = gtk::Settings::default() {
            s.set_gtk_application_prefer_dark_theme(true);
        }
    });
    app.connect_activate(move |app| {
        let win = editor::Editor::build(app, daemon.clone());
        win.present();
        if let Some(d) = &daemon {
            virtual_ui::start_if_enabled(app, d);
        }
        update_ui::check_at_start(&win);
    });
    app.run_with_args::<&str>(&[])
}
