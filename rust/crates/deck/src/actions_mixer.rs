//! FreeWave-Tasten und -Drehregler (wie `MixerAction`, `MixSelectAction`, `MixerPagesAction`,
//! `MixerSceneAction` und `WaveMicAction` in `freestream/native.py`, Bilder aus [`crate::mixkeys`]):
//!
//!   * Mixer-Kanal: Taste stumm/lauter/leiser/Lautstärke setzen (Halten wiederholt), Drehregler ändert die
//!     Lautstärke, Drücken schaltet stumm; Darstellung als Text, Kachel oder Fader mit Live-Pegel
//!   * Ausgabe des Persönlichen Mixes umschalten
//!   * Mix-Wahl (Kanäle mit mix „selected“ zeigen nur die Kanäle des gewählten Mixes) und Kanal-Seiten
//!   * Mixer-Szene anwenden (leuchtet, solange der Mixer ihr entspricht)
//!   * Wave-Mikrofon: Gain, stumm, Low Cut, Clipguard, LEDs
//!
//! Auch Aktionen des Elgato-Plugins „Wave Link“ aus importierten Profilen landen hier.
//! Gezeichnet wird nur aus dem gemeinsamen Stand ([`crate::wavehub`]) – nie mit einer pactl-Abfrage.

use std::cell::RefCell;
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use freestream_core::{pgettext, tr};
use freestream_wave::levels::post_fader;
use freestream_wave::Mute;
use image::{Rgba, RgbaImage};
use serde_json::{Map, Value};

use crate::actions::{ActionData, Behavior, Command, Env, Feedback, Outbox};
use crate::mixkeys::{self, Look};
use crate::native::{self, first, first_title, has_image, state_of};
use crate::wavehub::{self, Hub, Need, Snap, WORKER};

/// Gedrückt halten: nach 0,6 s alle 0,15 s wiederholen (wie `REPEAT_DELAY`, `REPEAT_EVERY`)
const REPEAT_DELAY: Duration = Duration::from_millis(600);
const REPEAT_EVERY: Duration = Duration::from_millis(150);

fn setting_int(s: &Map<String, Value>, key: &str, default: i64) -> i64 {
    // wie `int(settings.get(key) or default)`: 0 und leer zählen als nicht gesetzt
    s.get(key).and_then(|v| v.as_i64().or_else(|| v.as_f64().map(|f| f as i64))
        .or_else(|| v.as_str().and_then(|t| t.trim().parse().ok())))
        .filter(|v| *v != 0).unwrap_or(default)
}

fn lower_setting(s: &Map<String, Value>, keys: &[&str]) -> String {
    first(s, keys).unwrap_or_default().to_lowercase()
}

/// Gesehener Stand, um zu erkennen, ob eine Taste neu gezeichnet werden muss.
#[derive(Debug, Clone, PartialEq)]
enum View {
    Look(Look),
    /// leerer Platz (im gewählten Mix gibt es so viele Kanäle nicht)
    Empty,
    Text { main: String, status: String, active: Option<bool> },
    Dial(Feedback),
}

// ===================================================================================== Mixer-Kanal
/// Was die Taste tut: Kanal oder Ausgabe des Persönlichen Mixes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Func {
    Channel,
    Output,
}

/// Mixer-Kanal (`com.lindeck.mixer.channel`, Wave-Link-Kanäle) und Ausgabe (`com.lindeck.mixer.output`).
pub struct MixerKey {
    uuid: String,
    last: RefCell<Option<View>>,
    seen: u64,
    repeat: Option<Arc<AtomicBool>>,
}

/// Was eine Kanal-Taste gerade zeigt (aus Einstellungen und Stand).
struct Target {
    func: Func,
    channel: String,
    mix: String,
    empty: bool,
}

impl MixerKey {
    pub fn new(uuid: &str) -> Self {
        Self { uuid: uuid.to_lowercase(), last: RefCell::new(None), seen: 0, repeat: None }
    }

    fn func(&self, a: &ActionData) -> Func {
        let f = lower_setting(&a.settings(), &["function"]);
        if !f.is_empty() {
            return if f == "output" { Func::Output } else { Func::Channel };
        }
        let u = &self.uuid;
        if u.contains("output") || u.contains("monitormix") || u.contains("switch") { Func::Output } else { Func::Channel }
    }

    fn needs(&self, a: &ActionData) -> &'static [Need] {
        match self.func(a) {
            Func::Output => &[Need::Output],
            Func::Channel => &[Need::Levels],
        }
    }

    /// Taste mit Platz-Nummer (FreeWave-Profil mit Mix-Wahl am Deck)?
    fn placed(s: &Map<String, Value>) -> bool {
        s.get("slot").map(Value::is_i64).unwrap_or(false) && s.get("channels").map(Value::is_array).unwrap_or(false)
            && lower_setting(s, &["mix"]) == "selected"
    }

    /// Mix dieser Taste: „both“ (alle), ein Mix aus FreeWave oder der an der Mix-Wahl gewählte.
    fn mix(s: &Map<String, Value>, snap: &Snap) -> String {
        let m = first(s, &["mix", "mixType"]).unwrap_or_else(|| "both".into()).to_lowercase();
        let ids = snap.mix_ids();
        if m == "selected" {
            let m = wavehub::deck_mix();
            return if ids.contains(&m) { m } else { "both".into() };
        }
        if ids.contains(&m) {
            return m; // auch eigene Mixe („chat“ …)
        }
        if m.contains("monitor") || m.contains("local") {
            "monitor".into()
        } else if m.contains("stream") {
            "stream".into()
        } else {
            "both".into()
        }
    }

    /// FreeWave-Profil mit Mix-Wahl am Deck: die exportierten Kanäle, die im gewählten Mix sind („Alle“:
    /// die in irgendeinem Mix), in Mixer-Reihenfolge – Taste Nr. n zeigt den n-ten davon.
    fn shown_channels(s: &Map<String, Value>, snap: &Snap, mix: &str) -> Vec<String> {
        let mut out = Vec::new();
        for c in s.get("channels").and_then(Value::as_array).into_iter().flatten() {
            let c = match c {
                Value::String(t) => t.clone(),
                other => other.to_string(),
            };
            if snap.channel(&c).is_none() {
                continue; // inzwischen gelöscht
            }
            let mixes = snap.channel_mixes(&c);
            if !mixes.is_empty() && (mix == "both" || mixes.iter().any(|m| m == mix)) {
                out.push(c);
            }
        }
        out
    }

    fn target(&self, a: &ActionData, snap: &Snap) -> Target {
        let s = a.settings();
        let func = self.func(a);
        let mix = Self::mix(&s, snap);
        let channel = if Self::placed(&s) {
            let shown = Self::shown_channels(&s, snap, &mix);
            let place = s.get("slot").and_then(Value::as_i64).unwrap_or(0);
            usize::try_from(place).ok().and_then(|p| shown.get(p).cloned()).unwrap_or_default()
        } else {
            let t = first_title(a);
            let c = first(&s, &["channel", "mixerId", "mixerName", "identifier", "channelName"])
                .unwrap_or_else(|| if t.is_empty() { "system".into() } else { t });
            snap.channel(&c).map(|m| m.id.clone()).unwrap_or_else(|| c.to_lowercase())
        };
        let empty = Self::placed(&s) && func != Func::Output && channel.is_empty();
        Target { func, channel, mix, empty }
    }

    /// Darstellung: "" (Text), "tile", "fader_top", "fader_bottom" (FreeWave-Profil).
    fn display(&self, a: &ActionData) -> String {
        let d = lower_setting(&a.settings(), &["display"]);
        if matches!(d.as_str(), "tile" | "fader_top" | "fader_bottom") && self.func(a) != Func::Output { d } else { String::new() }
    }

    fn key_action(a: &ActionData) -> String {
        let k = lower_setting(&a.settings(), &["action"]);
        if k.is_empty() { "mute".into() } else { k }
    }

    /// Kurzname des Mixes für die Tasten: „Alle“ oder der Name des Mixes.
    fn mix_label(snap: &Snap, mix: &str) -> String {
        if mix == "both" {
            return pgettext("mix", "All");
        }
        snap.mix(mix).map(|m| m.name.clone()).unwrap_or_else(|| mix.to_string())
    }

    fn main_text(t: &Target, snap: &Snap) -> String {
        match t.func {
            Func::Output => pgettext("key label", "Output"),
            Func::Channel if t.empty => String::new(),
            Func::Channel => snap.channel(&t.channel).map(|c| c.name.clone()).unwrap_or_else(|| t.channel.clone()),
        }
    }

    fn status_text(a: &ActionData, t: &Target, snap: &Snap, (vol, mute): (Option<i64>, bool)) -> String {
        if t.func == Func::Output {
            return crate::net::fit(&snap.output_name, 14);
        }
        if t.empty {
            return String::new();
        }
        let Some(vol) = vol else { return tr!("Mixer off") };
        let mixname = if t.mix == "both" { String::new() } else { Self::mix_label(snap, &t.mix) };
        let sign = if a.dial {
            ""
        } else {
            match Self::key_action(a).as_str() {
                "up" => "+ ",
                "down" => "− ",
                _ => "",
            }
        };
        let val = if mute { pgettext("state", "muted") } else { format!("{vol}%") };
        if mixname.is_empty() { format!("{sign}{val}") } else { format!("{sign}{val} · {mixname}") }
    }

    fn level(hub: &Hub, snap: &Snap, t: &Target) -> (Option<i64>, bool) {
        if t.func == Func::Output || t.empty {
            return (None, false);
        }
        hub.level(snap, &t.channel, &t.mix)
    }

    fn active(t: &Target, lv: (Option<i64>, bool)) -> Option<bool> {
        if t.func == Func::Output {
            return None;
        }
        lv.0.map(|_| !lv.1)
    }

    fn view(&self, a: &ActionData, hub: &Hub, snap: &Snap) -> View {
        let t = self.target(a, snap);
        let lv = Self::level(hub, snap, &t);
        if a.dial {
            let mut fb = Feedback::of(a);
            fb.title = if t.empty {
                String::new()
            } else if a.title().is_empty() {
                Self::main_text(&t, snap)
            } else {
                a.title()
            };
            fb.value = Self::status_text(a, &t, snap, lv);
            fb.indicator = lv.0.map(|v| if lv.1 { 0.0 } else { v.min(100) as f32 });
            return View::Dial(fb);
        }
        if t.empty {
            return View::Empty;
        }
        let display = self.display(a);
        if !display.is_empty() && !has_image(a) {
            let (icon, color) = match snap.channel(&t.channel) {
                Some(c) => mixkeys::channel_look(&c.id, Some(&c.kind), &c.icon, &c.color),
                None => mixkeys::channel_look(&t.channel, None, "", ""),
            };
            let (vol, muted) = lv;
            let label = Self::mix_label(snap, &t.mix);
            let alpha = mixkeys::bg_alpha(&a.state_dict());
            let color = mixkeys::rgb(&color);
            if display == "tile" {
                return View::Look(Look::Tile { icon, color, vol, muted, label, alpha });
            }
            let top = display == "fader_top";
            let live = hub.meter(&t.channel).map(|raw| post_fader(raw, vol.map(|v| v as f64), muted));
            return View::Look(Look::Fader { top, icon, color, vol, muted, label, alpha, live: mixkeys::fader_live(top, live) });
        }
        View::Text { main: Self::main_text(&t, snap), status: Self::status_text(a, &t, snap, lv), active: Self::active(&t, lv) }
    }

    /// Neuen Stand sofort zeigen und im Arbeits-Thread schreiben (wie `_change`).
    fn change(hub: &Arc<Hub>, cid: &str, mix: &str, delta: i64, outbox: &Outbox) {
        Self::show_change(hub, cid, mix, delta);
        let (hub, cid, mix) = (hub.clone(), cid.to_string(), mix.to_string());
        let started = Instant::now();
        WORKER.run(outbox, cid.clone(), move || Self::write_change(&hub, &cid, &mix, delta, started).map(|_| Command::None));
    }

    fn show_change(hub: &Hub, cid: &str, mix: &str, delta: i64) {
        if let Some((vol, _)) = hub.fresh_level(cid, mix) {
            hub.show(cid, mix, Some((vol + delta).clamp(0, 100)), None);
        }
    }

    fn write_change(hub: &Hub, cid: &str, mix: &str, delta: i64, started: Instant) -> Result<(), String> {
        match hub.mixer().change_volume(cid, mix, delta, true) {
            // gekoppelter Kanal: alle Mixe geändert – gezeigt wird der Mix dieser Taste
            Ok(vols) => {
                let v = vols.get(mix).copied().or_else(|| vols.values().copied().max());
                hub.confirm(cid, mix, v, None, started);
                Ok(())
            }
            Err(e) => {
                hub.forget(cid, mix); // gezeigter Stand stimmt nicht: neu lesen
                Err(e.0)
            }
        }
    }

    fn toggle_mute(hub: &Arc<Hub>, cid: &str, mix: &str, outbox: &Outbox) {
        let lv = hub.fresh_level(cid, mix);
        let target = match lv {
            Some((_, m)) => Mute::Set(!m),
            None => Mute::Toggle,
        };
        if let Mute::Set(m) = target {
            hub.show(cid, mix, None, Some(m));
        }
        let (hub, cid, mix) = (hub.clone(), cid.to_string(), mix.to_string());
        let started = Instant::now();
        WORKER.run(outbox, cid.clone(), move || match hub.mixer().set_mute(&cid, &mix, target, true) {
            Ok(m) => {
                hub.confirm(&cid, &mix, None, Some(m), started);
                Ok(Command::None)
            }
            Err(e) => {
                hub.forget(&cid, &mix);
                Err(e.0)
            }
        });
    }

    fn stop_repeat(&mut self) {
        if let Some(stop) = self.repeat.take() {
            stop.store(true, Ordering::SeqCst);
        }
    }

    /// Gedrückt halten: weiter lauter/leiser, bis die Taste losgelassen wird.
    fn start_repeat(&mut self, hub: Arc<Hub>, cid: String, mix: String, delta: i64, outbox: Outbox) {
        self.stop_repeat();
        let stop = Arc::new(AtomicBool::new(false));
        self.repeat = Some(stop.clone());
        let _ = std::thread::Builder::new().name("key-repeat".into()).spawn(move || {
            std::thread::sleep(REPEAT_DELAY);
            while !stop.load(Ordering::SeqCst) {
                Self::show_change(&hub, &cid, &mix, delta);
                if let Err(e) = Self::write_change(&hub, &cid, &mix, delta, Instant::now()) {
                    log::warn!("{cid}: {e}");
                    outbox.send(Command::Alert);
                    return;
                }
                std::thread::sleep(REPEAT_EVERY);
            }
        });
    }

    fn changed(&mut self, a: &ActionData, hub: &Hub, snap: &Snap) -> bool {
        let v = self.view(a, hub, snap);
        if !a.dial && self.display(a).starts_with("fader") {
            if let View::Look(_) = v {
                hub_want_meter(&self.target(a, snap).channel);
            }
        }
        self.last.borrow().as_ref() != Some(&v)
    }
}

/// Live-Pegel für eine sichtbare Fader-Taste messen.
fn hub_want_meter(cid: &str) {
    wavehub::hub().want_meter(cid);
}

impl Drop for MixerKey {
    fn drop(&mut self) {
        self.stop_repeat();
    }
}

impl Behavior for MixerKey {
    fn key_down(&mut self, a: &ActionData, env: &Env) -> Command {
        let hub = wavehub::hub();
        let snap = hub.snap(self.needs(a));
        let t = self.target(a, &snap);
        if t.empty {
            return Command::None;
        }
        let s = a.settings();
        if t.func == Func::Output {
            let dev = first(&s, &["device"]).unwrap_or_else(|| "next".into());
            let h = hub.clone();
            WORKER.run(&env.outbox, a.name(), move || {
                let r = h.mixer().set_monitor_output(&dev).map(|_| Command::None).map_err(|e| e.0);
                h.request_refresh();
                r
            });
            return Command::None;
        }
        match Self::key_action(a).as_str() {
            k @ ("up" | "down") => {
                let step = setting_int(&s, "step", 5);
                let delta = if k == "up" { step } else { -step };
                Self::change(&hub, &t.channel, &t.mix, delta, &env.outbox);
                self.start_repeat(hub, t.channel, t.mix, delta, env.outbox.clone());
            }
            "set" => {
                let vol = setting_int(&s, "volume", 100);
                let h = hub.clone();
                WORKER.run(&env.outbox, a.name(), move || {
                    let r = h.mixer().set_volume(&t.channel, &t.mix, vol).map(|_| Command::None).map_err(|e| e.0);
                    h.request_refresh();
                    r
                });
            }
            _ => Self::toggle_mute(&hub, &t.channel, &t.mix, &env.outbox),
        }
        Command::Redraw
    }

    fn key_up(&mut self, _a: &ActionData, _env: &Env, _held_ms: u64) -> Command {
        self.stop_repeat();
        Command::None
    }

    fn dial_turn(&mut self, a: &ActionData, env: &Env, ticks: i8) -> Command {
        let hub = wavehub::hub();
        let t = self.target(a, &hub.snap(self.needs(a)));
        if t.empty || t.func == Func::Output {
            return Command::None;
        }
        Self::change(&hub, &t.channel, &t.mix, setting_int(&a.settings(), "step", 2) * ticks as i64, &env.outbox);
        Command::Redraw
    }

    fn dial_down(&mut self, a: &ActionData, env: &Env) -> Command {
        let hub = wavehub::hub();
        let t = self.target(a, &hub.snap(self.needs(a)));
        if t.empty {
            return Command::None;
        }
        if t.func == Func::Output {
            return self.key_down(a, env);
        }
        Self::toggle_mute(&hub, &t.channel, &t.mix, &env.outbox);
        Command::Redraw
    }

    fn dial_up(&mut self, _a: &ActionData, _env: &Env, _held_ms: u64) -> Command {
        Command::None
    }

    fn image(&self, a: &ActionData, _env: &Env, size: (u32, u32)) -> Option<RgbaImage> {
        let hub = wavehub::hub();
        let snap = hub.snap(self.needs(a));
        let v = self.view(a, &hub, &snap);
        *self.last.borrow_mut() = Some(v.clone());
        match v {
            View::Empty => Some(RgbaImage::from_pixel(size.0, size.1, Rgba([0, 0, 0, 255]))), // leerer Platz: dunkle Taste
            View::Look(l) => Some(mixkeys::draw(&l, size)),
            View::Text { main, status, active } => native::text_key(a, size, &main, &status, active),
            View::Dial(_) => None,
        }
    }

    fn title(&self, a: &ActionData, _env: &Env) -> Option<String> {
        let hub = wavehub::hub();
        match self.view(a, &hub, &hub.current()) {
            View::Empty | View::Look(_) => Some(String::new()),
            _ => None,
        }
    }

    fn own_opacity(&self, a: &ActionData) -> bool {
        !self.display(a).is_empty() && !has_image(a)
    }

    fn needs_tick(&self) -> bool {
        true
    }

    fn tick(&mut self, a: &ActionData) -> bool {
        let hub = wavehub::hub();
        let snap = hub.snap(self.needs(a));
        self.seen = hub.version();
        self.changed(a, &hub, &snap)
    }

    fn needs_fast_tick(&self, _a: &ActionData) -> bool {
        true
    }

    fn fast_tick(&mut self, a: &ActionData) -> bool {
        let hub = wavehub::hub();
        let v = hub.version();
        if v == self.seen {
            return false;
        }
        self.seen = v;
        self.changed(a, &hub, &hub.current())
    }

    fn live_state(&self, a: &ActionData) -> Option<usize> {
        let hub = wavehub::hub();
        let snap = hub.current();
        let t = self.target(a, &snap);
        state_of(a, Self::active(&t, Self::level(&hub, &snap, &t)))
    }

    fn feedback(&self, a: &ActionData, _env: &Env) -> Feedback {
        let hub = wavehub::hub();
        let snap = hub.snap(self.needs(a));
        let v = self.view(a, &hub, &snap);
        *self.last.borrow_mut() = Some(v.clone());
        match v {
            View::Dial(fb) => fb,
            _ => Feedback::of(a),
        }
    }
}

// ===================================================================================== Mix-Wahl
/// Mix-Wahl im FreeWave-Profil: Alle / jeder Mix; „cycle“ schaltet der Reihe nach weiter.
pub struct MixSelect {
    last: RefCell<Option<View>>,
    seen: u64,
}

impl MixSelect {
    pub fn new() -> Self {
        Self { last: RefCell::new(None), seen: 0 }
    }

    /// [(id, Name, Symbol)] – „Alle“ zuerst, dann die Mixe aus FreeWave.
    fn mixes(snap: &Snap) -> Vec<(String, String, String)> {
        let mut out = vec![("both".to_string(), pgettext("mix", "All"), "wave".to_string())];
        out.extend(snap.mixes.iter().map(|m| {
            (m.id.clone(), m.name.clone(), if m.icon.is_empty() { "wave".into() } else { m.icon.clone() })
        }));
        out
    }

    fn target(a: &ActionData, mixes: &[(String, String, String)]) -> String {
        let m = lower_setting(&a.settings(), &["mix"]);
        let m = if m.is_empty() { "both".to_string() } else { m };
        if m == "cycle" || mixes.iter().any(|x| x.0 == m) { m } else { "both".into() }
    }

    fn current(mixes: &[(String, String, String)]) -> String {
        let m = wavehub::deck_mix();
        if mixes.iter().any(|x| x.0 == m) { m } else { "both".into() }
    }

    fn info(mixes: &[(String, String, String)], mid: &str) -> (String, String, String) {
        mixes.iter().find(|x| x.0 == mid).cloned().unwrap_or_else(|| (mid.into(), mid.into(), "wave".into()))
    }

    fn view(&self, a: &ActionData, snap: &Snap) -> View {
        let mixes = Self::mixes(snap);
        let (target, current) = (Self::target(a, &mixes), Self::current(&mixes));
        let alpha = mixkeys::bg_alpha(&a.state_dict());
        let shown = if target == "cycle" { current.clone() } else { target.clone() };
        let (mid, name, icon) = Self::info(&mixes, &shown);
        if a.dial {
            return View::Dial(Feedback { value: wavehub::deck_mix(), ..Feedback::of(a) });
        }
        if has_image(a) {
            return View::Text { main: name, status: wavehub::deck_mix(), active: Some(target == "cycle" || current == target) };
        }
        let (selected, sub) = if target == "cycle" { (true, "Mix".to_string()) } else { (current == target, String::new()) };
        View::Look(Look::Mix { mix: mid, selected, sub, name, icon, alpha })
    }
}

impl Default for MixSelect {
    fn default() -> Self {
        Self::new()
    }
}

impl Behavior for MixSelect {
    fn key_down(&mut self, a: &ActionData, _env: &Env) -> Command {
        let mixes = Self::mixes(&wavehub::hub().snap(&[]));
        let target = Self::target(a, &mixes);
        if target == "cycle" {
            let cur = Self::current(&mixes);
            let i = mixes.iter().position(|x| x.0 == cur).unwrap_or(0);
            wavehub::set_deck_mix(&mixes[(i + 1) % mixes.len()].0);
        } else {
            wavehub::set_deck_mix(&target);
        }
        // die anderen Mix- und Kanaltasten ziehen im schnellen Takt nach (aus dem gemerkten Stand)
        Command::Redraw
    }

    fn image(&self, a: &ActionData, _env: &Env, size: (u32, u32)) -> Option<RgbaImage> {
        let v = self.view(a, &wavehub::hub().snap(&[]));
        *self.last.borrow_mut() = Some(v.clone());
        match v {
            View::Look(l) => Some(mixkeys::draw(&l, size)),
            _ => None,
        }
    }

    fn title(&self, a: &ActionData, _env: &Env) -> Option<String> {
        (!has_image(a)).then(String::new)
    }

    fn own_opacity(&self, a: &ActionData) -> bool {
        !has_image(a)
    }

    fn needs_tick(&self) -> bool {
        true
    }

    fn tick(&mut self, a: &ActionData) -> bool {
        let hub = wavehub::hub();
        self.seen = hub.version();
        let v = self.view(a, &hub.snap(&[]));
        self.last.borrow().as_ref() != Some(&v)
    }

    fn needs_fast_tick(&self, _a: &ActionData) -> bool {
        true
    }

    fn fast_tick(&mut self, a: &ActionData) -> bool {
        let hub = wavehub::hub();
        let ver = hub.version();
        if ver == self.seen {
            return false;
        }
        self.seen = ver;
        let v = self.view(a, &hub.current());
        self.last.borrow().as_ref() != Some(&v)
    }

    fn live_state(&self, a: &ActionData) -> Option<usize> {
        let mixes = Self::mixes(&wavehub::hub().current());
        let target = Self::target(a, &mixes);
        state_of(a, Some(target == "cycle" || Self::current(&mixes) == target))
    }

    fn feedback(&self, a: &ActionData, _env: &Env) -> Feedback {
        let v = self.view(a, &wavehub::hub().snap(&[]));
        *self.last.borrow_mut() = Some(v.clone());
        match v {
            View::Dial(fb) => fb,
            _ => Feedback::of(a),
        }
    }
}

// ===================================================================================== Kanal-Seiten
/// „Kanäle 1–7“: zeigt die Kanäle dieser Seite, Drücken blättert zur nächsten Seite.
pub struct MixerPages;

impl MixerPages {
    fn look(a: &ActionData) -> Look {
        let s = a.settings();
        let first = setting_int(&s, "first", 1);
        Look::Pages { first, last: setting_int(&s, "last", first), alpha: mixkeys::bg_alpha(&a.state_dict()) }
    }
}

impl Behavior for MixerPages {
    fn key_down(&mut self, _a: &ActionData, _env: &Env) -> Command {
        Command::NextPage
    }

    fn image(&self, a: &ActionData, _env: &Env, size: (u32, u32)) -> Option<RgbaImage> {
        (!has_image(a)).then(|| mixkeys::draw(&Self::look(a), size))
    }

    fn title(&self, a: &ActionData, _env: &Env) -> Option<String> {
        (!has_image(a)).then(String::new)
    }

    fn own_opacity(&self, a: &ActionData) -> bool {
        !has_image(a)
    }
}

// ===================================================================================== Mixer-Szene
/// Mixer-Szene aus FreeWave anwenden; leuchtet, solange der Mixer dieser Szene entspricht.
pub struct MixerScene {
    last: Option<(Option<bool>, String, String)>,
}

impl MixerScene {
    fn reference(a: &ActionData) -> String {
        first(&a.settings(), &["scene"]).unwrap_or_else(|| first_title(a))
    }

    /// (Name der Szene oder None, aktiv: Some(true/false), None = Mixer aus)
    fn info(a: &ActionData, snap: &Snap) -> (Option<String>, Option<bool>) {
        let r = Self::reference(a).trim().to_lowercase();
        if r.is_empty() {
            return (None, None);
        }
        let sc = snap.scenes.iter().find(|s| s.id.to_lowercase() == r)
            .or_else(|| snap.scenes.iter().find(|s| s.name.to_lowercase() == r));
        let Some(sc) = sc else { return (None, None) };
        let act = match &snap.levels {
            Some(l) if !l.is_empty() => Some(snap.active_scene.as_deref() == Some(sc.id.as_str())),
            _ => None,
        };
        (Some(sc.name.clone()), act)
    }

    fn texts(a: &ActionData, snap: &Snap) -> (Option<bool>, String, String) {
        let (name, act) = Self::info(a, snap);
        let main = name.clone().unwrap_or_else(|| pgettext("key label", "Scene"));
        let status = match (&name, act) {
            (None, _) => tr!("not found"),
            (_, None) => tr!("Mixer off"),
            (_, Some(true)) => pgettext("state", "active"),
            _ => String::new(),
        };
        (act, main, status)
    }

    fn snap() -> Arc<Snap> {
        wavehub::hub().snap(&[Need::Scenes])
    }
}

impl Behavior for MixerScene {
    fn key_down(&mut self, a: &ActionData, env: &Env) -> Command {
        let hub = wavehub::hub();
        let r = Self::reference(a);
        WORKER.run(&env.outbox, a.name(), move || {
            let res = hub.mixer().apply_scene(&r).map(|_| Command::None).map_err(|e| e.0);
            hub.request_refresh();
            res
        });
        Command::None
    }

    fn image(&self, a: &ActionData, _env: &Env, size: (u32, u32)) -> Option<RgbaImage> {
        let (act, main, status) = Self::texts(a, &Self::snap());
        native::text_key(a, size, &main, &status, act)
    }

    fn needs_tick(&self) -> bool {
        true
    }

    fn tick(&mut self, a: &ActionData) -> bool {
        let now = Some(Self::texts(a, &Self::snap()));
        let changed = now != self.last;
        self.last = now;
        changed
    }

    fn live_state(&self, a: &ActionData) -> Option<usize> {
        state_of(a, Self::info(a, &wavehub::hub().current()).1)
    }

    fn feedback(&self, a: &ActionData, _env: &Env) -> Feedback {
        let (_, main, status) = Self::texts(a, &Self::snap());
        let t = a.title();
        Feedback { title: if t.is_empty() { main } else { t }, value: status, indicator: None }
    }
}

// ===================================================================================== Wave-Mikrofon
/// Gelesener Zustand des Mikrofons (Gain höchstens einmal pro Sekunde, Schalter alle zwei Sekunden).
#[derive(Default)]
struct MicCache {
    version: Option<u64>,
    gain: Option<(Option<(i64, bool)>, Instant)>,
    switches: HashMap<String, (Option<bool>, Instant)>,
    hw: bool,
    lowcut_freq: i64,
}

/// Wave-Mikrofon (`com.lindeck.wavemic.control`, Wave-Link-Mikrofon-Aktionen).
pub struct WaveMicKey {
    uuid: String,
    cache: RefCell<MicCache>,
    last: Option<(Option<bool>, String, String)>,
}

impl WaveMicKey {
    pub fn new(uuid: &str) -> Self {
        Self { uuid: uuid.to_lowercase(), cache: RefCell::new(MicCache::default()), last: None }
    }

    fn func(&self, a: &ActionData) -> String {
        let f = lower_setting(&a.settings(), &["function"]);
        if !f.is_empty() {
            return f;
        }
        let u = &self.uuid;
        if u.contains("lowcut") {
            "lowcut"
        } else if u.contains("clip") {
            "clipguard"
        } else if u.contains("led") {
            "leds"
        } else if u.contains("gain") {
            "gain"
        } else {
            "mute"
        }.into()
    }

    fn is_switch(f: &str) -> bool {
        matches!(f, "lowcut" | "clipguard" | "leds" | "ledsflip")
    }

    /// Zustand nachlesen, wenn er zu alt ist oder sich das Mikrofon geändert hat. Ist das Mikrofon gerade
    /// beschäftigt (Tastendruck im Arbeits-Thread), bleibt der alte Stand.
    fn read(&self, f: &str) {
        let Ok(mut mic) = wavehub::wave_mic().try_lock() else { return };
        let mut c = self.cache.borrow_mut();
        if c.version != Some(mic.version) {
            c.gain = None;
            c.switches.clear();
            c.version = Some(mic.version);
        }
        if Self::is_switch(f) {
            if c.switches.get(f).map(|(_, t)| t.elapsed() > Duration::from_secs(2)).unwrap_or(true) {
                let key = match f {
                    "leds" => "leds_off",
                    "ledsflip" => "leds_flip",
                    k => k,
                };
                let val = mic.is_on(key);
                let val = if f == "leds" { val.map(|v| !v) } else { val };
                c.switches.insert(f.to_string(), (val, Instant::now()));
                c.hw = mic.hw().is_some();
                c.lowcut_freq = mic.mic_settings().lowcut_freq;
            }
        } else if c.gain.map(|(_, t)| t.elapsed() > Duration::from_secs(1)).unwrap_or(true) {
            c.gain = Some((mic.gain().ok(), Instant::now()));
        }
        c.version = Some(mic.version);
    }

    fn gain(&self) -> (Option<i64>, bool) {
        match self.cache.borrow().gain.and_then(|g| g.0) {
            Some((v, m)) => (Some(v), m),
            None => (None, false),
        }
    }

    fn switch(&self, f: &str) -> Option<bool> {
        self.cache.borrow().switches.get(f).and_then(|s| s.0)
    }

    fn texts(&self, a: &ActionData) -> (Option<bool>, String, String) {
        let f = self.func(a);
        self.read(&f);
        let main = match f.as_str() {
            "mute" => pgettext("key label", "Mic"),
            "gain" => "Gain".into(),
            "lowcut" => "Low Cut".into(),
            "clipguard" => "Clipguard".into(),
            "leds" => "LEDs".into(),
            "ledsflip" => "LED ↕".into(),
            _ => "Wave".into(),
        };
        if Self::is_switch(&f) {
            let on = self.switch(&f);
            let status = match on {
                None if f == "leds" || f == "ledsflip" => tr!("Wave:3 only"),
                None => tr!("no Wave"),
                Some(true) if f == "lowcut" && !self.cache.borrow().hw => format!("{} Hz", self.cache.borrow().lowcut_freq),
                Some(true) => pgettext("state", "on"),
                Some(false) => pgettext("state", "off"),
            };
            return (on, main, status);
        }
        let (vol, mute) = self.gain();
        let status = match vol {
            None => tr!("no Wave"),
            Some(_) if mute => pgettext("state", "muted"),
            Some(v) => format!("{v}%"),
        };
        (vol.map(|_| !mute), main, status)
    }

    /// Auftrag am Mikrofon im Arbeits-Thread (Wave:3 über USB kann dauern).
    fn run(&mut self, a: &ActionData, outbox: &Outbox, job: impl FnOnce(&mut freestream_wave::wavemic::WaveMic)
        -> freestream_wave::Result<()> + Send + 'static) -> Command {
        *self.cache.borrow_mut() = MicCache::default();
        WORKER.run(outbox, a.name(), move || {
            let mut mic = wavehub::wave_mic().lock().unwrap_or_else(|e| e.into_inner());
            job(&mut mic).map(|_| Command::None).map_err(|e| e.0)
        });
        Command::None
    }

    fn toggle_mute(mic: &mut freestream_wave::wavemic::WaveMic) -> freestream_wave::Result<()> {
        // wie `toggle_mute` in Python: ohne Wave-Mikrofon ein Fehler (nicht das Standardmikrofon)
        if mic.hw().is_none() {
            mic.source()?;
        }
        mic.set_mute(None).map(|_| ())
    }
}

impl Behavior for WaveMicKey {
    fn key_down(&mut self, a: &ActionData, env: &Env) -> Command {
        let f = self.func(a);
        let step = setting_int(&a.settings(), "step", 5) as f64;
        match f.as_str() {
            "lowcut" | "clipguard" => self.run(a, &env.outbox, move |m| m.toggle(&f).map(|_| ())),
            "leds" => self.run(a, &env.outbox, |m| m.toggle("leds_off").map(|_| ())),
            "ledsflip" => self.run(a, &env.outbox, |m| m.toggle("leds_flip").map(|_| ())),
            "gain" => self.run(a, &env.outbox, move |m| m.change_gain(step)),
            _ => self.run(a, &env.outbox, Self::toggle_mute),
        }
    }

    fn dial_turn(&mut self, a: &ActionData, env: &Env, ticks: i8) -> Command {
        let delta = (setting_int(&a.settings(), "step", 2) * ticks as i64) as f64;
        self.run(a, &env.outbox, move |m| m.change_gain(delta))
    }

    fn dial_down(&mut self, a: &ActionData, env: &Env) -> Command {
        self.run(a, &env.outbox, Self::toggle_mute)
    }

    fn dial_up(&mut self, _a: &ActionData, _env: &Env, _held_ms: u64) -> Command {
        Command::None
    }

    fn image(&self, a: &ActionData, _env: &Env, size: (u32, u32)) -> Option<RgbaImage> {
        let (act, main, status) = self.texts(a);
        native::text_key(a, size, &main, &status, act)
    }

    fn needs_tick(&self) -> bool {
        true
    }

    fn tick(&mut self, a: &ActionData) -> bool {
        let now = Some(self.texts(a));
        let changed = now != self.last;
        self.last = now;
        changed
    }

    fn live_state(&self, a: &ActionData) -> Option<usize> {
        state_of(a, self.last.as_ref().and_then(|l| l.0))
    }

    fn feedback(&self, a: &ActionData, _env: &Env) -> Feedback {
        let (_, main, status) = self.texts(a);
        let t = a.title();
        let (vol, mute) = self.gain();
        let f = self.func(a);
        let indicator = if f == "mute" || f == "gain" { vol.map(|v| if mute { 0.0 } else { v as f32 }) } else { None };
        Feedback { title: if t.is_empty() { main } else { t }, value: status, indicator }
    }
}

// ===================================================================================== Zuordnung
/// Verhalten für FreeWave-Aktionen (eigene und die des Elgato-Plugins „Wave Link“).
pub fn behavior_for(uuid: &str) -> Option<Box<dyn Behavior>> {
    let u = uuid.to_lowercase();
    if u.is_empty() {
        return None;
    }
    let (plugin, last) = u.rsplit_once('.').unwrap_or(("", u.as_str()));
    let wavelink = plugin.contains("wavelink");
    if u.starts_with("com.lindeck.wavemic.")
        || (wavelink && ["mic", "lowcut", "clip", "gain"].iter().any(|k| last.contains(k)))
    {
        return Some(Box::new(WaveMicKey::new(&u)));
    }
    match u.as_str() {
        "com.lindeck.mixer.scene" => return Some(Box::new(MixerScene { last: None })),
        "com.lindeck.mixer.mixselect" => return Some(Box::new(MixSelect::new())),
        "com.lindeck.mixer.pages" => return Some(Box::new(MixerPages)),
        "com.lindeck.freewave.soundboard" => return Some(Box::new(crate::actions_soundboard::SoundboardKey::new())),
        _ => {}
    }
    if u.starts_with("com.lindeck.mixer.") || wavelink || plugin.contains("wave-link") {
        return Some(Box::new(MixerKey::new(&u)));
    }
    None
}
