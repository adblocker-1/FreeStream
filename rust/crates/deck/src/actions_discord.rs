//! Discord-Aktionen (`DiscordAction` in `freestream/native.py`): Mikrofon stumm, taub, Push-to-Talk,
//! Push-to-Mute, Sprachkanal betreten/verlassen, Textkanal öffnen – für `com.lindeck.discord.voice` und
//! die UUIDs der Discord-Plugins aus importierten Profilen.
//!
//! Befehle laufen im Arbeits-Thread des Clients (in der Reihenfolge der Tastendrücke, wichtig für
//! Push-to-Talk); die Taste zeigt stumm/taub live (Zustand 1 = stumm bzw. taub).

use freestream_core::{pgettext, tr};
use image::RgbaImage;
use serde_json::json;
use std::sync::Arc;

use crate::actions::{ActionData, Behavior, Command, Env};
use crate::discord::{self, Client, DiscordError};
use crate::native;

/// Ist das eine Discord-Aktion?
pub fn is_discord(uuid: &str) -> bool {
    let u = uuid.to_lowercase();
    let plugin = u.rsplit_once('.').map(|(p, _)| p).unwrap_or("");
    u.starts_with("com.lindeck.discord.") || plugin.contains("discord")
}

/// Funktion aus Settings.function, sonst aus der UUID.
pub fn function(a: &ActionData) -> String {
    if let Some(f) = native::first(&a.settings(), &["function"]) {
        return f.to_lowercase();
    }
    let u = a.uuid.to_lowercase();
    let last = u.rsplit('.').next().unwrap_or("");
    if last.contains("deaf") {
        "deafen"
    } else if last.contains("pushtotalk") || last.contains("ptt") {
        "pushtotalk"
    } else if last.contains("pushtomute") {
        "pushtomute"
    } else if last.contains("voicechannel") || (last.contains("channel") && !last.contains("text")) {
        "voicechannel"
    } else if last.contains("textchannel") {
        "textchannel"
    } else if last.contains("leave") || last.contains("disconnect") {
        "leave"
    } else {
        "mute"
    }
    .to_string()
}

#[derive(Default)]
pub struct DiscordAction {
    snap: Option<(u64, bool, Option<bool>)>,
}

impl DiscordAction {
    fn active(c: &Client, func: &str) -> Option<bool> {
        if !c.ready() {
            return None;
        }
        let (mute, deaf) = c.voice();
        match func {
            "mute" => Some(mute),
            "deafen" => Some(deaf),
            _ => None,
        }
    }

    fn main_text(func: &str) -> String {
        match func {
            "mute" => pgettext("key label", "Mic"),
            "deafen" => pgettext("key label", "Sound"),
            "pushtotalk" => "PTT".into(),
            "pushtomute" => "PTM".into(),
            "voicechannel" => tr!("Channel"),
            "textchannel" => "Chat".into(),
            "leave" => tr!("Hang up"),
            _ => "Discord".into(),
        }
    }

    fn status(c: &Client, func: &str) -> String {
        if !c.ready() {
            return "Discord".into();
        }
        match Self::active(c, func) {
            None => String::new(),
            Some(true) => pgettext("state", "muted"),
            Some(false) => pgettext("state", "on"),
        }
    }

    fn channel(a: &ActionData) -> String {
        native::first(&a.settings(), &["channel_id", "channelId", "channel"]).unwrap_or_default()
    }

    fn run(env: &Env, a: &ActionData, c: Arc<Client>, job: impl FnOnce(&Arc<Client>) -> Result<(), DiscordError> + Send + 'static) {
        let me = c.clone();
        c.worker.run(&env.outbox, a.name(), move || job(&me).map(|_| Command::Redraw).map_err(|e| e.to_string()));
    }
}

impl Behavior for DiscordAction {
    fn key_down(&mut self, a: &ActionData, env: &Env) -> Command {
        let c = discord::client(env.cfg);
        let ch = Self::channel(a);
        match function(a).as_str() {
            "mute" => Self::run(env, a, c, |c| c.toggle("mute")),
            "deafen" => Self::run(env, a, c, |c| c.toggle("deaf")),
            "pushtotalk" => Self::run(env, a, c, |c| c.set_voice(json!({"mute": false}))),
            "pushtomute" => Self::run(env, a, c, |c| c.set_voice(json!({"mute": true}))),
            "voicechannel" => Self::run(env, a, c, move |c| c.select_voice_channel(&ch)),
            "textchannel" => Self::run(env, a, c, move |c| c.select_text_channel(&ch)),
            "leave" => Self::run(env, a, c, |c| c.select_voice_channel("")),
            _ => {}
        }
        Command::None
    }

    fn key_up(&mut self, a: &ActionData, env: &Env, _held_ms: u64) -> Command {
        let c = discord::client(env.cfg);
        match function(a).as_str() {
            "pushtotalk" => Self::run(env, a, c, |c| c.set_voice(json!({"mute": true}))),
            "pushtomute" => Self::run(env, a, c, |c| c.set_voice(json!({"mute": false}))),
            _ => {}
        }
        Command::None
    }

    fn image(&self, a: &ActionData, env: &Env, size: (u32, u32)) -> Option<RgbaImage> {
        let c = discord::client(env.cfg);
        let f = function(a);
        native::text_key(a, size, &Self::main_text(&f), &Self::status(&c, &f), Self::active(&c, &f))
    }

    fn needs_tick(&self) -> bool {
        true
    }

    fn tick(&mut self, a: &ActionData) -> bool {
        let Some(c) = discord::current() else { return false };
        let snap = Some((c.version(), c.ready(), Self::active(&c, &function(a))));
        let changed = snap != self.snap;
        self.snap = snap;
        changed
    }

    fn live_state(&self, a: &ActionData) -> Option<usize> {
        let c = discord::current()?;
        native::state_of(a, Self::active(&c, &function(a)))
    }
}

pub fn behavior_for(uuid: &str) -> Option<Box<dyn Behavior>> {
    is_discord(uuid).then(|| Box::<DiscordAction>::default() as Box<dyn Behavior>)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn action(uuid: &str, settings: serde_json::Value) -> ActionData {
        let d = json!({"UUID": uuid, "Settings": settings, "States": [{}, {}]});
        ActionData::new(d.as_object().cloned().unwrap(), PathBuf::new())
    }

    #[test]
    fn functions_from_settings_and_uuid() {
        assert_eq!(function(&action("com.lindeck.discord.voice", json!({"function": "deafen"}))), "deafen");
        assert_eq!(function(&action("com.lindeck.discord.voice", json!({}))), "mute");
        assert_eq!(function(&action("com.elgato.discord.deafen", json!({}))), "deafen");
        assert_eq!(function(&action("com.elgato.discord.pushtotalk", json!({}))), "pushtotalk");
        assert_eq!(function(&action("com.elgato.discord.voicechannel", json!({}))), "voicechannel");
        assert_eq!(function(&action("com.elgato.discord.textchannel", json!({}))), "textchannel");
        assert_eq!(function(&action("com.elgato.discord.disconnect", json!({}))), "leave");
        assert!(is_discord("com.elgato.discord.mute"));
        assert!(is_discord("com.lindeck.discord.voice"));
        assert!(!is_discord("com.lindeck.obs.scene"));
    }
}
