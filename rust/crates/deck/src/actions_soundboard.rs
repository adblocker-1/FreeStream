//! Taste „Soundboard: Klang abspielen“: spielt einen Klang aus dem Soundboard von FreeWave in den dort
//! gewählten Mixen ab bzw. stoppt ihn (wie `SoundboardAction` der Python-Version).

use freestream_core::config;
use freestream_wave::soundboard::{self, Board, Player};
use image::RgbaImage;
use serde_json::Value;
use std::sync::Mutex;

use crate::actions::{ActionData, Behavior, Command, Env, Feedback};
use crate::native::{self, first_title};
use crate::wavehub;

/// Ein Abspieler für alle Soundboard-Tasten des Prozesses.
static PLAYER: Mutex<Option<Player>> = Mutex::new(None);

fn with_player<R>(f: impl FnOnce(&mut Player) -> R) -> R {
    let mut p = PLAYER.lock().unwrap();
    f(p.get_or_insert_with(Player::new))
}

pub struct SoundboardKey {
    last: Option<(bool, String)>,
}

impl SoundboardKey {
    pub fn new() -> Self {
        Self { last: None }
    }

    fn sound_id(a: &ActionData) -> String {
        a.settings().get("sound").and_then(Value::as_str).unwrap_or("").to_string()
    }

    fn name(a: &ActionData) -> String {
        let id = Self::sound_id(a);
        let conf = config::read_file().unwrap_or_default();
        Board::from_conf(&conf).sound(&id).map(|s| s.name.clone()).unwrap_or_else(|| first_title(a))
    }

    fn playing(a: &ActionData) -> bool {
        let id = Self::sound_id(a);
        !id.is_empty() && with_player(|p| p.is_playing(&id))
    }
}

impl Default for SoundboardKey {
    fn default() -> Self {
        Self::new()
    }
}

impl Behavior for SoundboardKey {
    fn key_down(&mut self, a: &ActionData, _env: &Env) -> Command {
        let id = Self::sound_id(a);
        if id.is_empty() {
            return Command::Alert;
        }
        if with_player(|p| p.is_playing(&id)) {
            with_player(|p| p.stop(&id));
            return Command::Redraw;
        }
        let conf = config::read_file().unwrap_or_default();
        let board = Board::from_conf(&conf);
        let Some(sound) = board.sound(&id).cloned() else { return Command::Alert };
        // Ziele: die Mixe aus „Abspielen in“/„Abhören in“ (Gerät bzw. Null-Sink des Mixes)
        let mixes: Vec<(String, Option<String>)> = wavehub::hub()
            .mixer()
            .mixes()
            .iter()
            .map(|m| (m.id.clone(), soundboard::mix_target(m, &m.devices)))
            .collect();
        let play = conf.get("soundboard_play").and_then(Value::as_str).unwrap_or("");
        let monitor = conf.get("soundboard_monitor").and_then(Value::as_str).unwrap_or("");
        let vol = conf.get("soundboard_volume").and_then(Value::as_i64).unwrap_or(80);
        let targets = soundboard::targets(play, monitor, &mixes);
        let cmds = soundboard::commands(&sound, vol, &targets, crate::system::have);
        if cmds.is_empty() {
            return Command::Alert;
        }
        let ok = with_player(|p| p.play(&id, cmds, sound.looping));
        if ok { Command::Redraw } else { Command::Alert }
    }

    fn image(&self, a: &ActionData, _env: &Env, size: (u32, u32)) -> Option<RgbaImage> {
        let playing = Self::playing(a);
        let status = if playing { freestream_core::pgettext("state", "playing") } else { String::new() };
        native::text_key(a, size, &Self::name(a), &status, Some(playing))
    }

    fn needs_tick(&self) -> bool {
        true
    }

    fn tick(&mut self, a: &ActionData) -> bool {
        with_player(|p| p.tick());
        let now = Some((Self::playing(a), Self::name(a)));
        let changed = now != self.last;
        self.last = now;
        changed
    }

    fn live_state(&self, a: &ActionData) -> Option<usize> {
        Some(usize::from(Self::playing(a)))
    }

    fn feedback(&self, a: &ActionData, _env: &Env) -> Feedback {
        Feedback { title: Self::name(a), value: String::new(), indicator: None }
    }
}
