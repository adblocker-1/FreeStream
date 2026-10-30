//! FreeDeck – Software für Elgato Stream Decks unter Linux (Rust-Version).
//!
//! `freedeck run`        Dienst: Geräte betreiben (ohne Fenster)
//! `freedeck devices`    angeschlossene Geräte zeigen
//! `freedeck profiles`   Profile auflisten
//! `freedeck import F`   Profil, Backup oder Ordner der Elgato-Software übernehmen
//! `freedeck preview P [Seite] [-o Datei.png]`  Seite eines Profils als Bild (ohne Gerät)

use freestream_core::models::{connected_models, model_geometry, model_name};
use freestream_core::profiles::{import_path, ProfileStore};
use freestream_core::tr;
use freestream_deck::daemon::{already_running, Daemon};
use freestream_deck::device::MemoryDeck;
use freestream_deck::engine::Controller;
use std::sync::{Arc, Mutex};

mod gui;

fn usage() {
    println!("FreeDeck {}\n", freestream_core::VERSION);
    println!("  freedeck run                     {}", tr!("start only the service, without a window (e.g. via SSH)"));
    println!("  freedeck devices                 {}", tr!("show connected devices"));
    println!("  freedeck profiles                {}", tr!("Profiles"));
    println!("  freedeck import <file|folder>    {}", tr!("Import profiles"));
    println!("  freedeck quit                    {}", tr!("quit the running FreeDeck"));
    println!("  freedeck assign <serial> <profile> {}", tr!("permanently assign a profile to a device"));
    println!("  freedeck remove <profile>        {}", tr!("delete a profile"));
    println!("  freedeck preview <profile> [page] [-o file.png]  {}", tr!("render profile pages as PNG (no device needed)"));
}

fn main() -> anyhow::Result<()> {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        None | Some("gui") | Some("open") | Some("edit") => {
            // Fenster mit dem Dienst im selben Prozess (Geräte laufen in eigenen Threads)
            let daemon = if already_running().is_some() {
                None
            } else {
                let d = Arc::new(Mutex::new(Daemon::new()));
                let d2 = d.clone();
                std::thread::spawn(move || service_loop(d2));
                Some(d)
            };
            gui::run(daemon);
            Ok(())
        }
        Some("run") | Some("--hidden") => {
            if let Some(pid) = already_running() {
                println!("{}", format!("{} (pid {pid})", tr!("FreeDeck is already running")));
                return Ok(());
            }
            let mut d = Daemon::new();
            let stop = d.stop.clone();
            ctrlc_stop(stop);
            d.run()
        }
        Some("devices") => {
            let devs = connected_models();
            if devs.is_empty() {
                println!("{}", tr!("No device connected"));
            }
            for d in devs {
                println!("{}  {}  {}", d.name, d.model, d.serial);
            }
            Ok(())
        }
        Some("profiles") => {
            let store = ProfileStore::new(None);
            for p in store.profiles.values() {
                println!("{:<38} {:<24} {:<22} {}", p.id, p.name, p.model_name(), freestream_core::i18n::ngettext("{n} page", "{n} pages", p.page_ids.len() as i64).replace("{n}", &p.page_ids.len().to_string()));
            }
            Ok(())
        }
        Some("import") => {
            let Some(src) = args.get(1) else {
                usage();
                return Ok(());
            };
            let ids = import_path(std::path::Path::new(src), None, None, true)?;
            println!("{}", freestream_core::i18n::ngettext("{n} profile imported", "{n} profiles imported", ids.len() as i64).replace("{n}", &ids.len().to_string()));
            Ok(())
        }
        Some("preview") => preview(&args[1..]),
        Some("quit") => {
            match already_running() {
                Some(pid) => {
                    // wie die Python-Version: SIGTERM, der Dienst beendet sich sauber
                    let _ = std::process::Command::new("kill").arg(pid.to_string()).status();
                    println!("{}", tr!("Quitting FreeDeck"));
                }
                None => println!("{}", tr!("FreeDeck is not running")),
            }
            Ok(())
        }
        Some("assign") => {
            let (Some(serial), Some(prof)) = (args.get(1), args.get(2)) else {
                usage();
                return Ok(());
            };
            let store = ProfileStore::new(None);
            let Some(p) = store.get(prof) else {
                println!("{}", tr!("Profile '{profile}' not found", profile = prof));
                std::process::exit(1);
            };
            let mut cfg = freestream_core::config::Config::load();
            cfg.set_device(serial, "profile", serde_json::Value::from(p.id.clone()));
            cfg.save()?;
            println!("{}", tr!("Device {serial} -> {profile}", serial = serial, profile = p.name));
            Ok(())
        }
        Some("remove") => {
            let Some(prof) = args.get(1) else {
                usage();
                return Ok(());
            };
            let store = ProfileStore::new(None);
            match store.get(prof).cloned() {
                Some(p) if freestream_core::profiles::delete_profile(&p.id, None) => {
                    println!("{}", tr!("Profile '{profile}' deleted", profile = p.name));
                    Ok(())
                }
                _ => {
                    println!("{}", tr!("Profile not found"));
                    std::process::exit(1);
                }
            }
        }
        Some("--version") | Some("version") => {
            println!("FreeDeck {}", freestream_core::VERSION);
            Ok(())
        }
        _ => {
            usage();
            Ok(())
        }
    }
}

/// Seite eines Profils als PNG – mit einem Deck im Speicher, ganz ohne Gerät.
fn preview(args: &[String]) -> anyhow::Result<()> {
    let store = Arc::new(Mutex::new(ProfileStore::new(None)));
    let Some(pid) = args.first() else { anyhow::bail!("freedeck preview <profile> [page] [-o file.png]") };
    let p = store.lock().unwrap().get(pid).cloned().ok_or_else(|| anyhow::anyhow!(tr!("Profile not found")))?;
    let page: usize = args.get(1).and_then(|a| a.parse().ok()).unwrap_or(1);
    let out = args.iter().position(|a| a == "-o").and_then(|i| args.get(i + 1)).cloned().unwrap_or_else(|| "preview.png".into());
    let model = if model_geometry(&p.model).is_some() { p.model.clone() } else { "20GBA9901".into() };
    let dev = Arc::new(MemoryDeck::new(&model, "PREVIEW", (96, 96)));
    let mut c = Controller::new(dev, store);
    c.load_profile(Some(&p.id), Some(page.saturating_sub(1)));
    c.preview(12).save(&out)?;
    println!("{} – {} → {out}", p.name, model_name(&model));
    Ok(())
}

/// Geräte suchen und Profile neu lesen, solange das Fenster offen ist.
fn service_loop(d: Arc<Mutex<Daemon>>) {
    let Ok(mut hid) = elgato_streamdeck::new_hidapi() else { return };
    freestream_deck::daemon::write_pid_file();
    loop {
        {
            let mut d = d.lock().unwrap();
            d.scan(&mut hid);
            d.reload_if_changed();
        }
        std::thread::sleep(std::time::Duration::from_secs(2));
    }
}

/// Strg+C / SIGTERM beenden den Dienst sauber (Tasten bleiben nicht halb gezeichnet).
fn ctrlc_stop(stop: Arc<std::sync::atomic::AtomicBool>) {
    let _ = ctrlc::set_handler(move || stop.store(true, std::sync::atomic::Ordering::Relaxed));
}
