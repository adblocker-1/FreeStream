//! FreeWave: Audio-Mixer auf PipeWire – Rust-Version von `freestream/mixer.py`, `levels.py`,
//! `ducking.py`, `noise.py`, `effects.py`, `wavemic.py`, `wave3hw.py` und dem Hintergrundteil von
//! `wavehost.py`.
//!
//! Gleiche Einstellungen (`config.json`, Abschnitte `"mixer"` und `"wave_mic"`), gleiche Zustandsdateien
//! (`mixer.json`, `effects.json`, `noise.json`, `ducking.json`, `wavemic.json`, `freewave.json`) und
//! gleiche PipeWire-Objekte (Null-Sinks, Loopbacks, Filterketten mit denselben Namen und Properties) wie
//! die Python-Version – beide können abwechselnd laufen.
//!
//! Aufbau:
//!   * [`mixer::Mixer`] – Kanäle, Mixe, Lautstärken, Abhören, Szenen, Programme (blockiert: pactl)
//!   * [`control::Controller`] – für das Fenster: [`control::Snapshot`] zum Zeichnen, Aktionen im
//!     Arbeits-Thread, Pegel
//!   * [`host::WaveHost`] / [`host::run_service`] – Hintergrunddienst (`freewave --hidden`)
//!   * [`levels`], [`ducking`], [`effects`], [`noise`], [`wavemic`], [`wave3hw`]
//!
//! Nicht übernommen: alles, was Daten aus Elgatos Wave-Link-Software ausliest (Python hat in diesen
//! Modulen nichts davon; Werkzeuge dafür werden bewusst nicht portiert).

pub mod control;
pub mod ducking;
pub mod effects;
pub mod flow;
pub mod fmt;
pub mod host;
pub mod levels;
pub mod mixer;
pub mod noise;
pub mod pactl;
pub mod procs;
pub mod settings;
pub mod soundboard;
pub mod wave3hw;
pub mod wavemic;

pub use control::{Controller, Snapshot};
pub use host::{run_service, WaveHost};
pub use mixer::{Channel, Level, Mix, Mixer, Mute};
pub use settings::{FileSettings, Settings};

/// Fehler mit einer Meldung für den Nutzer (schon übersetzt).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Error(pub String);

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for Error {}

impl From<String> for Error {
    fn from(s: String) -> Self {
        Self(s)
    }
}

pub type Result<T> = std::result::Result<T, Error>;
