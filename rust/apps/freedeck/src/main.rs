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

fn usage() {
    println!("FreeDeck {}\n", freestream_core::VERSION);
    println!("  freedeck run                     {}", tr!("Run the service (keys on the device, no window)"));
    println!("  freedeck devices                 {}", tr!("Connected devices"));
    println!("  freedeck profiles                {}", tr!("Profiles"));
    println!("  freedeck import <file|folder>    {}", tr!("Import profiles"));
    println!("  freedeck preview <profile> [page] [-o file.png]");
}

fn main() -> anyhow::Result<()> {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        None | Some("run") | Some("--hidden") => {
            if let Some(pid) = already_running() {
                println!("{}", tr!("FreeDeck is already running (pid {pid})", pid = pid));
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
                println!("{:<38} {:<24} {:<22} {} {}", p.id, p.name, p.model_name(), p.page_ids.len(), tr!("pages"));
            }
            Ok(())
        }
        Some("import") => {
            let Some(src) = args.get(1) else {
                usage();
                return Ok(());
            };
            let ids = import_path(std::path::Path::new(src), None, None, true)?;
            println!("{}", tr!("{n} profiles imported", n = ids.len()));
            Ok(())
        }
        Some("preview") => preview(&args[1..]),
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

/// Strg+C / SIGTERM beenden den Dienst sauber (Tasten bleiben nicht halb gezeichnet).
fn ctrlc_stop(stop: Arc<std::sync::atomic::AtomicBool>) {
    let _ = ctrlc::set_handler(move || stop.store(true, std::sync::atomic::Ordering::Relaxed));
}
