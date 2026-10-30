//! FreeWave – Audio-Mixer und Wave-Mikrofon unter Linux (Rust-Version von `freestream/wavehost.py`,
//! `waveui.py`, `wavedialogs.py` und `wavesetup.py`).
//!
//! `freewave`                 Fenster öffnen; läuft FreeWave schon mit Fenster, wird es nach vorne geholt,
//!                            läuft es ohne Fenster (Autostart), wird es mit Fenster neu gestartet
//! `freewave --hidden`        Dienst ohne Fenster (Autostart): Mixer, Programme verteilen, Mikrofon-Filter
//! `freewave --quit`          laufendes FreeWave beenden
//! `freewave mixer …`, `freewave mic …`   Kommandozeile wie in der Python-Version

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use freestream_core::{paths, tr};
use freestream_wave::host::{app_info, running_instance};
use freestream_wave::procs;
use freestream_wave::wavemic::WAVE_APP_FILE;

mod cli;
mod deckexport;
mod gui;

fn main() {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let code = match cli::parse(&argv) {
        cli::Cmd::Version => {
            println!("FreeWave {}", freestream_core::VERSION);
            0
        }
        cli::Cmd::Help => {
            print!("{}", cli::usage());
            0
        }
        cli::Cmd::Bad(what) => {
            eprintln!("{}", tr!("Unknown command: {name}", name = what));
            eprint!("{}", cli::usage());
            2
        }
        cli::Cmd::Mixer { action, args } => cli::cmd_mixer(&action, &args),
        cli::Cmd::Wave { action, args } => cli::cmd_wave(&action, &args),
        cli::Cmd::App { quit: true, .. } => quit_running(),
        cli::Cmd::App { hidden, .. } => start(hidden),
    };
    std::process::exit(code);
}

/// `freewave --quit`
fn quit_running() -> i32 {
    match running_instance(&paths::state_dir(), WAVE_APP_FILE) {
        Some(pid) => {
            // SAFETY: kill mit gültiger Pid und Signal
            unsafe { libc::kill(pid as libc::pid_t, libc::SIGTERM) };
            println!("{}", tr!("Quitting FreeWave"));
            0
        }
        None => {
            println!("{}", tr!("FreeWave is not running"));
            1
        }
    }
}

/// Startet FreeWave oder holt das Fenster der laufenden Instanz nach vorne (wie `wavehost.main`).
fn start(hidden: bool) -> i32 {
    let dir = paths::state_dir();
    if let Some(pid) = running_instance(&dir, WAVE_APP_FILE) {
        if hidden {
            return 0;
        }
        let gui = app_info(&dir, WAVE_APP_FILE).get("gui").and_then(serde_json::Value::as_bool).unwrap_or(false);
        if gui {
            // SAFETY: kill mit gültiger Pid und Signal
            unsafe { libc::kill(pid as libc::pid_t, libc::SIGUSR1) };
            return 0;
        }
        log::info!("{}", tr!("FreeWave is running without a window – restarting with a window"));
        // SAFETY: wie oben
        unsafe { libc::kill(pid as libc::pid_t, libc::SIGTERM) };
        for _ in 0..50 {
            if !procs::alive(pid) {
                break;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
    }
    if hidden || !gui::display_available() {
        if !hidden {
            log::info!("{}", tr!("No graphical interface ({error}) – FreeWave is running as a service", error = "DISPLAY"));
        }
        let stop = Arc::new(AtomicBool::new(false));
        let s = stop.clone();
        let _ = ctrlc::set_handler(move || s.store(true, Ordering::SeqCst));
        freestream_wave::run_service(stop);
        return 0;
    }
    gui::run();
    0
}
