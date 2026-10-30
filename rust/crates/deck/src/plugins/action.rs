//! Aktion eines Elgato-Plugins auf einer Taste oder einem Drehregler (wie `PluginAction` und die
//! Aufrufe des Plugin-Hosts in `freestream/engine.py`).
//!
//! Die Aktion meldet sich beim ersten Zeichnen beim Plugin an (willAppear) und beim Verwerfen ab
//! (willDisappear, wenn die Steuerung die Seite wechselt). Was das Plugin setzt (Titel, Bild, Zustand,
//! Werte im Touchstreifen), steht im gemeinsamen [`CtxState`]; `tick` zeichnet dann neu, Zustand und
//! „Fertig“/„Fehler“ gehen über die [`crate::actions::Outbox`] an die Steuerung.

use image::RgbaImage;
use serde_json::Value;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::OnceLock;

use super::host::{new_ctx, Ctx, CtxState, PluginHost};
use super::plugin_for_action;
use crate::actions::{ActionData, Behavior, Command, Env, Feedback};
use crate::render;

/// Verhalten einer Plugin-Aktion.
pub struct PluginBehavior {
    uuid: String,
    /// fester Host (Tests); sonst der Host des Prozesses ([`super::host()`])
    fixed_host: Option<PluginHost>,
    ctx: OnceLock<Ctx>,
    appeared: AtomicBool,
}

impl PluginBehavior {
    pub fn new(uuid: &str) -> Self {
        Self { uuid: uuid.to_string(), fixed_host: None, ctx: OnceLock::new(), appeared: AtomicBool::new(false) }
    }

    /// Mit einem bestimmten Host (statt dem des Prozesses).
    pub fn with_host(uuid: &str, host: PluginHost) -> Self {
        let mut b = Self::new(uuid);
        b.fixed_host = Some(host);
        b
    }

    fn host(&self) -> Option<PluginHost> {
        self.fixed_host.clone().or_else(super::host)
    }

    /// Kontext dieser Aktion (beim ersten Aufruf angelegt: Position auf der Seite, Gerät).
    pub fn context(&self, a: &ActionData) -> Ctx {
        self.ctx.get_or_init(|| {
            let coords = find_coords(a);
            let ctx = new_ctx(&self.uuid, a.data.clone(), a.page_dir.clone(), a.dial, coords);
            if let Some(h) = self.host() {
                ctx.lock().unwrap().device = h.device_for_page(&a.page_dir);
            }
            ctx
        }).clone()
    }

    /// Kontext anlegen, Zustand aus der Steuerung übernehmen, Ausgang für Befehle merken und beim ersten
    /// Mal willAppear schicken (`visible` = die Aktion wird gerade angezeigt).
    fn sync(&self, a: &ActionData, env: Option<&Env>, visible: bool) -> Ctx {
        let ctx = self.context(a);
        {
            let mut c = ctx.lock().unwrap();
            c.state = a.state;
            if let Some(env) = env {
                c.outbox = Some(env.outbox.clone());
            }
        }
        if visible && !self.appeared.swap(true, Ordering::SeqCst) {
            if let Some(h) = self.host() {
                h.will_appear(&ctx);
            }
        }
        ctx
    }

    fn not_available(&self) -> Command {
        log::info!("{}", freestream_core::tr!("Plugin action {uuid} not available yet", uuid = self.uuid));
        Command::Alert
    }

    fn with<T>(&self, f: impl FnOnce(&CtxState) -> T) -> Option<T> {
        self.ctx.get().map(|c| f(&c.lock().unwrap()))
    }
}

/// Position der Aktion auf ihrer Seite (Taste: Spalte/Zeile, Drehregler: Nummer); None = nicht direkt auf
/// der Seite, also Schritt einer Multi-Aktion.
fn find_coords(a: &ActionData) -> Option<(u8, u8)> {
    let pg = freestream_core::profiles::Page::load("", &a.page_dir).ok()?;
    if a.dial {
        return pg.encoders.iter().find(|(_, d)| **d == a.data).map(|(i, _)| (*i, 0));
    }
    pg.keypad.iter().find(|(_, d)| **d == a.data).map(|(p, _)| *p)
}

impl Drop for PluginBehavior {
    fn drop(&mut self) {
        if self.appeared.load(Ordering::SeqCst) {
            if let (Some(h), Some(ctx)) = (self.host(), self.ctx.get()) {
                h.will_disappear(ctx);
            }
        }
    }
}

impl Behavior for PluginBehavior {
    fn key_down(&mut self, a: &ActionData, env: &Env) -> Command {
        let ctx = self.sync(a, Some(env), false);
        match self.host() {
            Some(h) if h.key_down(&ctx) => Command::None,
            _ => self.not_available(),
        }
    }

    fn key_up(&mut self, a: &ActionData, env: &Env, _held_ms: u64) -> Command {
        let ctx = self.sync(a, Some(env), false);
        let Some(h) = self.host() else { return Command::None };
        if !h.key_up(&ctx) {
            return Command::None;
        }
        // Zustand wechselt von selbst, außer das Plugin will das nicht (DisableAutomaticStates)
        let auto = plugin_for_action(&self.uuid, None)
            .and_then(|i| i.action_meta(&self.uuid).map(|m| super::truthy(m.get("DisableAutomaticStates"))))
            .map(|disabled| !disabled)
            .unwrap_or(true);
        if a.state_count() > 1 && auto {
            let s = (a.state + 1) % a.state_count();
            ctx.lock().unwrap().state = s;
            return Command::SetState(s);
        }
        Command::None
    }

    fn dial_turn(&mut self, a: &ActionData, env: &Env, ticks: i8) -> Command {
        let ctx = self.sync(a, Some(env), false);
        if let Some(h) = self.host() {
            h.dial_rotate(&ctx, ticks as i32);
        }
        Command::None
    }

    fn dial_down(&mut self, a: &ActionData, env: &Env) -> Command {
        let ctx = self.sync(a, Some(env), false);
        match self.host() {
            Some(h) if h.dial_down(&ctx) => Command::None,
            _ => self.not_available(),
        }
    }

    fn dial_up(&mut self, a: &ActionData, env: &Env, _held_ms: u64) -> Command {
        let ctx = self.sync(a, Some(env), false);
        if let Some(h) = self.host() {
            h.dial_up(&ctx);
        }
        Command::None
    }

    fn touch(&mut self, a: &ActionData, env: &Env, long: bool) -> Command {
        let ctx = self.sync(a, Some(env), false);
        // die Steuerung gibt die Stelle im Feld nicht weiter: Mitte des Felds nicht bekannt, (0, 0)
        if let Some(h) = self.host() {
            h.touch_tap(&ctx, (0, 0), long);
        }
        Command::None
    }

    fn image(&self, a: &ActionData, env: &Env, size: (u32, u32)) -> Option<RgbaImage> {
        let ctx = self.sync(a, Some(env), true);
        let (img, title) = {
            let c = ctx.lock().unwrap();
            (c.image.clone(), c.title.clone())
        };
        let has_profile_image = a.state_dict().get("Image").and_then(Value::as_str).map(|s| !s.is_empty()).unwrap_or(false);
        let mut out = match img {
            Some(i) => render::fit(&i, size.0, size.1),
            None if has_profile_image => return None,
            None => {
                // Symbol aus dem Plugin, sonst Ersatzbild „Plugin: Aktion“
                let icon = plugin_for_action(&self.uuid, None)
                    .and_then(|i| i.action_icon(&self.uuid, a.state))
                    .and_then(|p| render::load_image(&p, size.0, size.1));
                match icon {
                    Some(i) => i,
                    None => {
                        let label = if title.is_some() || !a.title().is_empty() {
                            String::new()
                        } else {
                            let plugin = a.data.get("Plugin").and_then(|p| p.get("Name")).and_then(Value::as_str)
                                .filter(|s| !s.is_empty()).map(str::to_string)
                                .unwrap_or_else(|| {
                                    let base = self.uuid.rsplit_once('.').map(|x| x.0).unwrap_or("");
                                    base.rsplit('.').next().unwrap_or("").to_string()
                                });
                            format!("{plugin}: {}", a.name())
                        };
                        render::fallback_icon(size.0, size.1, &label)
                    }
                }
            }
        };
        // Mit eigenem Bild zeichnet die Steuerung nur den Titel aus dem Profil: einen Titel des Plugins
        // über einer Taste ohne Profil-Titel hier selbst zeichnen
        if a.title().is_empty() {
            if let Some(t) = title.filter(|t| !t.is_empty()) {
                let st = a.state_dict();
                if st.get("ShowTitle").and_then(Value::as_bool).unwrap_or(true) {
                    render::draw_title(&mut out, &t, &st, size.0 as f32 / 72.0);
                }
            }
        }
        Some(out)
    }

    fn title(&self, _a: &ActionData, _env: &Env) -> Option<String> {
        self.with(|c| c.title.clone()).flatten()
    }

    fn needs_tick(&self) -> bool {
        true
    }

    fn tick(&mut self, a: &ActionData) -> bool {
        // Drehregler werden nicht als Taste gezeichnet: hier anmelden
        let ctx = self.sync(a, None, true);
        let mut c = ctx.lock().unwrap();
        std::mem::take(&mut c.dirty)
    }

    fn feedback(&self, a: &ActionData, env: &Env) -> Feedback {
        let ctx = self.sync(a, Some(env), true);
        let c = ctx.lock().unwrap();
        let mut fb = Feedback::of(a);
        if let Some(t) = c.title.clone().filter(|t| !t.is_empty()) {
            fb.title = t;
        }
        for (k, v) in &c.feedback {
            let v = match v {
                Value::Object(o) => o.get("value").cloned().unwrap_or(Value::Null),
                v => v.clone(),
            };
            match k.as_str() {
                "indicator" => {
                    if let Some(f) = v.as_f64().or_else(|| v.as_str().and_then(|s| s.trim().parse().ok())) {
                        fb.indicator = Some(f as f32);
                    }
                }
                "title" => fb.title = super::value_text(&v),
                "value" => fb.value = super::value_text(&v),
                _ => {}
            }
        }
        fb
    }

    fn encoder_icon(&self, _a: &ActionData, _env: &Env, size: (u32, u32)) -> Option<RgbaImage> {
        self.with(|c| c.encoder_icon.as_ref().map(|i| render::fit(i, size.0, size.1))).flatten()
    }
}
