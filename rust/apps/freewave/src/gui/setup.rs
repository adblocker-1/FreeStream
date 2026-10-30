//! Einrichtungsassistent von FreeWave – erscheint beim ersten Start (wavesetup.py). Schritt für Schritt
//! wie bei Wave Link: Willkommen, Mikrofon (mit Pegel), Kopfhörer (mit Testton), Klang (Gain, Low Cut,
//! Clipguard, empfohlene Effekte), Kanäle, Fertig.

use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::io::Read;
use std::path::PathBuf;
use std::rc::Rc;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use gtk::prelude::*;
use gtk::Align;
use gtk4 as gtk;

use freestream_core::tr;
use freestream_wave::effects::{self, Effect, Plugin};
use freestream_wave::mixer::{default_channels, truthy, Device};
use freestream_wave::{noise, wavemic, WaveHost};
use serde_json::{json, Map, Value};

use super::theme;
use super::widgets::{self, hbox, label, vbox, Fader, Switch};
use super::{bg, Win};
use crate::cli::obj;

/// Low Cut ohne Wave:3: Effekt am Mikrofon-Kanal
pub const HIGHPASS: &str = "builtin:highpass";

fn steps() -> [String; 6] {
    [tr!("Welcome"), tr!("Microphone"), tr!("Headphones"), tr!("Sound"), tr!("Channels"), tr!("Done")]
}

/// Symbol und Farbe der Standardkanäle im Assistenten.
fn channel_icon(id: &str) -> (&'static str, &'static str) {
    match id {
        "system" => ("system", "#8e9bb0"),
        "music" => ("music", "#30d158"),
        "browser" => ("browser", "#2f7bff"),
        "voice" => ("chat", "#6b74ff"),
        "game" => ("game", "#ffb020"),
        "sfx" => ("sfx", "#ff6bd5"),
        _ => ("wave", "#8e9bb0"),
    }
}

/// Ist das Gerät ein Elgato-Wave-Mikrofon?
pub fn is_wave(name: &str, desc: &str) -> bool {
    let t = format!("{name} {desc}").to_lowercase();
    t.contains("wave") && (t.contains("elgato") || t.contains("0fd9") || t.contains("wave:") || t.contains("wave_"))
}

// ================================================================== Daten
/// Im Hintergrund gesammelt (wavesetup.collect).
#[derive(Clone, Default)]
pub struct SetupData {
    pub inputs: Vec<Device>,
    pub outputs: Vec<Device>,
    pub catalog: Vec<Plugin>,
    pub wave3: bool,
    pub noise: Vec<String>,
    pub default_source: String,
    pub default_sink: String,
    pub monitor_output: String,
    pub gain_pct: Option<i64>,
    /// Einstellungen des Mikrofons (lowcut, clipguard) – für erneutes Aufrufen
    pub mic_settings: Map<String, Value>,
}

pub fn collect(host: &WaveHost) -> SetupData {
    let mx = &host.mixer;
    let mut out = SetupData { inputs: mx.inputs(), outputs: mx.outputs(), catalog: effects::catalog(mx.state_dir(), false),
                              noise: noise::available(), ..Default::default() };
    let clean = |v: freestream_wave::Result<String>| v.ok().filter(|v| !v.starts_with("linewave_") && !v.starts_with("lindeck_")).unwrap_or_default();
    out.default_source = clean(mx.default_source());
    out.default_sink = clean(mx.default_sink());
    // Gerät des laufenden Persönlichen Mix (der Standard-Sink ist bei laufendem Mixer der eigene)
    let cur = if mx.is_active() { mx.state().get("monitor_output").and_then(Value::as_str).unwrap_or("").to_string() } else { String::new() };
    if out.outputs.iter().any(|o| o.name == cur) {
        out.monitor_output = cur;
    }
    if let Ok(mut mic) = host.mic.lock() {
        out.wave3 = mic.hw().is_some();
        if out.wave3 {
            out.gain_pct = mic.gain().ok().map(|g| g.0);
        }
        out.mic_settings = mic.settings.clone();
    }
    if let Some(src) = wavemic::find_source(mx.pactl()) {
        if !out.inputs.iter().any(|d| d.name == src) {
            out.inputs.insert(0, Device { name: src, description: "Elgato Wave".into() });
        }
    }
    out
}

/// Auswahl des Assistenten.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Choices {
    pub mic: String,
    pub output: String,
    pub wave3: bool,
    pub lowcut: bool,
    pub clipguard: bool,
    pub gain: Option<f64>,
    pub channels: Vec<String>,
    pub mic_fx: Vec<Effect>,
    /// Plugin-id -> an/aus, nur für umgeschaltete
    pub mic_fx_set: HashMap<String, bool>,
    pub noise: Option<bool>,
}

/// Effekte des Assistenten in die Kette des Mikrofons übernehmen (wavesetup.merge_chain): abgeschaltete
/// gehen, eingeschaltete bleiben samt ihren Reglern (und sind an) oder kommen vor den ersten Effekt, der
/// beim Assistenten danach kommt. Alles andere bleibt, wie und wo es ist.
pub fn merge_chain(old: &[Effect], new: &[Effect], changes: &HashMap<String, bool>) -> Vec<Effect> {
    let mut out: Vec<Effect> = Vec::new();
    for fx in old {
        match changes.get(&fx.plugin) {
            Some(false) => continue,
            Some(true) => out.push(Effect { enabled: true, ..fx.clone() }),
            None => out.push(fx.clone()),
        }
    }
    for (i, fx) in new.iter().enumerate() {
        if changes.get(&fx.plugin) != Some(&true) || out.iter().any(|o| o.plugin == fx.plugin) {
            continue;
        }
        let later: Vec<&str> = new[i + 1..].iter().map(|f| f.plugin.as_str()).collect();
        let pos = out.iter().position(|f| later.contains(&f.plugin.as_str())).unwrap_or(out.len());
        out.insert(pos, fx.clone());
    }
    out
}

/// Kanäle nach der Auswahl: bestehende bleiben, abgewählte Standardkanäle gehen, neue kommen hinten dazu.
pub fn merged_channels(current: &[Value], want: &[String]) -> Vec<Value> {
    let defaults = default_channels();
    let is_default = |id: &str| defaults.iter().any(|d| d["id"] == id);
    let mut chans: Vec<Value> = current.iter().filter(|c| {
        let id = c.get("id").and_then(Value::as_str).unwrap_or("");
        !is_default(id) || want.iter().any(|w| w == id)
    }).cloned().collect();
    for d in &defaults {
        let id = d["id"].as_str().unwrap_or("");
        if want.iter().any(|w| w == id) && !chans.iter().any(|c| c.get("id").and_then(Value::as_str) == Some(id)) {
            chans.push(d.clone());
        }
    }
    if !chans.iter().any(|c| c.get("kind").and_then(Value::as_str).unwrap_or("apps") == "apps") {
        chans.insert(0, defaults[0].clone()); // mindestens ein Kanal für Programme
    }
    chans
}

/// Auswahl speichern und den Mixer einrichten (läuft im Hintergrund).
pub fn apply_choices(host: &WaveHost, ch: &Choices) -> freestream_wave::Result<()> {
    host.mixer.with_settings_mut(|s| {
        let data = s.data_mut();
        let mx = obj(data, "mixer");
        if !ch.mic.is_empty() {
            mx.insert("mic".into(), json!(ch.mic));
        }
        if !ch.output.is_empty() {
            mx.insert("monitor_output".into(), json!(ch.output));
        }
        let cur: Vec<Value> = mx.get("channels").and_then(Value::as_array).cloned().unwrap_or_default();
        mx.insert("channels".into(), Value::Array(merged_channels(&cur, &ch.channels)));
        let fx = obj(mx, "effects");
        let old: Vec<Effect> = fx.get("mic").and_then(Value::as_array).map(|a| a.iter().filter_map(Effect::from_value).collect()).unwrap_or_default();
        let chain = merge_chain(&old, &ch.mic_fx, &ch.mic_fx_set);
        if chain.is_empty() {
            fx.shift_remove("mic");
        } else {
            fx.insert("mic".into(), Value::Array(chain.iter().map(Effect::to_value).collect()));
        }
        if let Some(n) = ch.noise {
            obj(mx, "noise").insert("enabled".into(), json!(n));
        }
        obj(data, "linewave").insert("setup_done".into(), json!(true));
        if let Err(e) = s.save() {
            log::warn!("config.json: {e}");
        }
    });
    {
        let mut mic = host.mic.lock().unwrap();
        if ch.wave3 && mic.hw().is_some() {
            for (key, on) in [("lowcut", ch.lowcut), ("clipguard", ch.clipguard)] {
                let _ = mic.hw_set(key, if on { 1.0 } else { 0.0 });
            }
            if let Some(g) = ch.gain {
                let _ = mic.set_gain(g);
            }
            mic.settings.insert("lowcut".into(), json!(ch.lowcut));
            mic.settings.insert("clipguard".into(), json!(ch.clipguard));
            let _ = mic.set(&[]);
        } else if mic.settings.get("lowcut").map(truthy).unwrap_or(false) || mic.settings.get("clipguard").map(truthy).unwrap_or(false) {
            // Low Cut/Clipguard laufen jetzt als Effekte des Mikrofon-Kanals
            let _ = mic.set(&[("lowcut", json!(false)), ("clipguard", json!(false))]);
        }
    }
    host.enable_mixer(true)
}

// ================================================================== Pegel und Testton
/// Pegel einer Eingangsquelle über `parec` (eigener Thread).
struct LevelMeter {
    child: Option<std::process::Child>,
    state: Arc<Mutex<(f64, f64)>>,
}

impl LevelMeter {
    fn new(source: &str) -> Self {
        let state = Arc::new(Mutex::new((0.0, 0.0)));
        let child = freestream_wave::pactl::which("parec").filter(|_| !source.is_empty()).and_then(|exe| {
            std::process::Command::new(exe).args([&format!("--device={source}"), "--raw", "--format=s16le", "--rate=16000",
                                                  "--channels=1", "--latency-msec=40"])
                .stdout(std::process::Stdio::piped()).stderr(std::process::Stdio::null()).spawn().ok()
        });
        let mut me = Self { child, state };
        if let Some(out) = me.child.as_mut().and_then(|c| c.stdout.take()) {
            let st = me.state.clone();
            std::thread::spawn(move || {
                let mut out = out;
                let mut buf = [0u8; 1280]; // 40 ms
                while let Ok(n) = out.read(&mut buf) {
                    if n == 0 {
                        break;
                    }
                    let peak = buf[..n / 2 * 2].chunks(2).map(|c| i16::from_le_bytes([c[0], c[1]]).unsigned_abs()).max().unwrap_or(0) as f64 / 32768.0;
                    let db = if peak > 0.0 { 20.0 * peak.log10() } else { -90.0 };
                    let level = ((db + 60.0) / 60.0).clamp(0.0, 1.0); // −60 dB … 0 dB
                    let mut s = st.lock().unwrap();
                    s.0 = level;
                    s.1 = level.max(s.1 * 0.92);
                }
            });
        }
        me
    }

    fn available(&self) -> bool {
        self.child.is_some()
    }

    fn read(&self) -> (f64, f64) {
        *self.state.lock().unwrap()
    }
}

impl Drop for LevelMeter {
    fn drop(&mut self) {
        if let Some(mut c) = self.child.take() {
            let _ = c.kill();
            let _ = c.wait();
        }
    }
}

/// Kurzer Testton (440 Hz, sanft ein- und ausgeblendet) als WAV.
pub fn test_tone_file() -> PathBuf {
    let path = freestream_core::paths::state_dir().join("freewave-testton.wav");
    if path.exists() {
        return path;
    }
    let _ = std::fs::create_dir_all(path.parent().unwrap_or(std::path::Path::new(".")));
    let (rate, n) = (48000u32, 38400usize);
    let mut data: Vec<u8> = Vec::with_capacity(n * 4 + 44);
    let le32 = |v: u32| v.to_le_bytes();
    data.extend(b"RIFF");
    data.extend(le32(36 + n as u32 * 4));
    data.extend(b"WAVEfmt ");
    data.extend(le32(16));
    data.extend(1u16.to_le_bytes());
    data.extend(2u16.to_le_bytes());
    data.extend(le32(rate));
    data.extend(le32(rate * 4));
    data.extend(4u16.to_le_bytes());
    data.extend(16u16.to_le_bytes());
    data.extend(b"data");
    data.extend(le32(n as u32 * 4));
    for i in 0..n {
        let env = (i as f64 / 2400.0).min((n - i) as f64 / 4800.0).min(1.0);
        let v = (9000.0 * env * (std::f64::consts::TAU * 440.0 * i as f64 / rate as f64).sin()) as i16;
        data.extend(v.to_le_bytes());
        data.extend(v.to_le_bytes());
    }
    let _ = std::fs::write(&path, data);
    path
}

fn play_tone(sink: &str) {
    let exe = freestream_wave::pactl::which("paplay").or_else(|| freestream_wave::pactl::which("pw-play"));
    let Some(exe) = exe else { return };
    let arg = if exe.to_string_lossy().contains("paplay") { format!("--device={sink}") } else { format!("--target={sink}") };
    let _ = std::process::Command::new(exe).arg(arg).arg(test_tone_file()).stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null()).spawn();
}

// ================================================================== Fenster
struct Wizard {
    w: Rc<Win>,
    win: gtk::Window,
    data: SetupData,
    step: Cell<usize>,
    dots: gtk::Box,
    body: gtk::Box,
    next: gtk::Button,
    back: gtk::Button,
    skip: gtk::Button,
    mic: RefCell<String>,
    out: RefCell<String>,
    channels: RefCell<Vec<(String, bool)>>,
    lowcut: Cell<bool>,
    clipguard: Cell<bool>,
    fx: RefCell<HashMap<&'static str, bool>>,
    gain: Cell<Option<f64>>,
    /// beim erneuten Aufrufen: bisher eingeschaltete Effekte (None: erste Einrichtung)
    fx_on: RefCell<Option<Vec<String>>>,
    noise_on: Cell<bool>,
    wave3_0: Cell<bool>,
    meter: RefCell<Option<LevelMeter>>,
    result: RefCell<Option<gtk::Label>>,
}

/// Assistent öffnen (Geräte und Plugins werden vorher im Hintergrund gesammelt).
pub fn open(w: &Rc<Win>) {
    if w.wizard_open.replace(true) {
        return;
    }
    let host = w.ctl.host().clone();
    let w2 = w.clone();
    bg(move || collect(&host), move |data| Wizard::open(&w2, data));
}

impl Wizard {
    fn open(w: &Rc<Win>, data: SetupData) {
        let win = gtk::Window::builder().title(tr!("Set up FreeWave")).transient_for(&w.win).modal(true).default_width(780)
            .default_height(680).build();
        let outer = vbox(0);
        outer.add_css_class("main");
        let dots = hbox(0);
        widgets::margins(&dots, 16, 22, 16, 22);
        outer.append(&dots);
        outer.append(&widgets::sep());
        let body = vbox(0);
        widgets::margins(&body, 22, 22, 22, 22);
        body.set_vexpand(true);
        outer.append(&widgets::scroll(&body));
        outer.append(&widgets::sep());
        let foot = hbox(8);
        widgets::margins(&foot, 14, 22, 14, 22);
        let skip = widgets::button(&tr!("Set up later"), None, "ghost");
        foot.append(&skip);
        foot.append(&widgets::spacer());
        let back = widgets::button(&tr!("Back"), None, "");
        let next = widgets::button(&tr!("Next"), None, "primary");
        foot.append(&back);
        foot.append(&next);
        outer.append(&foot);
        win.set_child(Some(&outer));
        let conf = w.ctl.host().mixer.conf();
        let wave_src = data.inputs.iter().find(|d| is_wave(&d.name, &d.description)).map(|d| d.name.clone());
        let mic = conf.get("mic").and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_string)
            .or(wave_src).or_else(|| (!data.default_source.is_empty()).then(|| data.default_source.clone()))
            .or_else(|| data.inputs.first().map(|d| d.name.clone())).unwrap_or_default();
        // zuerst das Gerät, auf dem der Persönliche Mix gerade läuft – sonst verlegt ihn „Fertig“
        let out = [data.monitor_output.clone(), conf.get("monitor_output").and_then(Value::as_str).unwrap_or("").to_string(),
                   data.default_sink.clone(), data.outputs.first().map(|d| d.name.clone()).unwrap_or_default()]
            .into_iter().find(|s| !s.is_empty()).unwrap_or_default();
        let have: Vec<String> = conf.get("channels").and_then(Value::as_array).map(|a| a.iter().filter_map(|c| c.get("id").and_then(Value::as_str).map(str::to_string)).collect())
            .unwrap_or_else(|| default_channels().iter().filter_map(|c| c["id"].as_str().map(str::to_string)).collect());
        let channels = default_channels().iter().filter_map(|c| c["id"].as_str().map(|id| (id.to_string(), have.iter().any(|h| h == id)))).collect();
        let me = Rc::new(Self {
            w: w.clone(), win: win.clone(), data, step: Cell::new(0), dots, body, next, back, skip, mic: RefCell::new(mic),
            out: RefCell::new(out), channels: RefCell::new(channels), lowcut: Cell::new(true), clipguard: Cell::new(true),
            fx: RefCell::new(HashMap::new()), gain: Cell::new(None), fx_on: RefCell::new(None), noise_on: Cell::new(false),
            wave3_0: Cell::new(false), meter: RefCell::new(None), result: RefCell::new(None),
        });
        me.current_sound(&conf);
        let m = me.clone();
        me.next.connect_clicked(move |_| m.go_next());
        let m = me.clone();
        me.back.connect_clicked(move |_| {
            if m.step.get() > 0 {
                m.step.set(m.step.get() - 1);
                m.render();
            }
        });
        let m = me.clone();
        me.skip.connect_clicked(move |_| m.skip());
        let m = me.clone();
        win.connect_close_request(move |_| {
            m.meter.borrow_mut().take();
            m.w.wizard_open.set(false);
            if m.step.get() < 5 {
                m.mark_done();
            }
            gtk::glib::Propagation::Proceed
        });
        me.render();
        win.present();
    }

    fn mark_done(&self) {
        self.w.ctl.act_host(|h| {
            h.mixer.with_settings_mut(|s| {
                obj(s.data_mut(), "linewave").insert("setup_done".into(), json!(true));
                let _ = s.save();
            });
            Ok(())
        });
    }

    fn skip(&self) {
        self.mark_done();
        self.step.set(5);
        self.meter.borrow_mut().take();
        self.win.close();
    }

    fn go_next(self: &Rc<Self>) {
        let n = self.step.get();
        if n == 4 {
            self.step.set(5);
            self.render();
            self.apply();
        } else if n == 5 {
            self.win.close();
        } else {
            self.step.set(n + 1);
            self.render();
        }
    }

    fn plugin(&self, label_: &str) -> Option<Plugin> {
        self.data.catalog.iter().find(|p| p.label == label_).cloned()
    }

    /// Empfehlung beim ersten Mal; erneut aufgerufen der jetzige Stand (wavesetup._current_sound).
    fn current_sound(&self, mx: &Map<String, Value>) {
        let mic_fx: Vec<Effect> = mx.get("effects").and_then(|e| e.get("mic")).and_then(Value::as_array)
            .map(|a| a.iter().filter_map(Effect::from_value).collect()).unwrap_or_default();
        let noise_enabled = mx.get("noise").and_then(|n| n.get("enabled")).map(truthy).unwrap_or(false);
        let set_up = mx.get("enabled").map(truthy).unwrap_or(false) || mx.get("channels").map(truthy).unwrap_or(false)
            || !mic_fx.is_empty() || noise_enabled;
        let mut fx = self.fx.borrow_mut();
        if !set_up {
            self.lowcut.set(true);
            self.clipguard.set(true);
            fx.insert("noise", !self.data.noise.is_empty());
            fx.insert("gate", self.plugin("gate").is_some());
            fx.insert("sc4m", false);
            return;
        }
        let on: Vec<String> = mic_fx.iter().filter(|f| f.enabled).map(|f| f.plugin.clone()).collect();
        self.noise_on.set(noise_enabled);
        let wave3 = self.data.wave3 && is_wave(&self.mic.borrow(), "");
        self.wave3_0.set(wave3);
        let filters = if wave3 || !self.data.wave3 { self.data.mic_settings.clone() } else { Map::new() };
        let id = |l: &str| self.plugin(l).map(|p| p.id);
        let has = |p: Option<String>| p.is_some_and(|p| on.contains(&p));
        self.lowcut.set(filters.get("lowcut").map(truthy).unwrap_or(false) || (!wave3 && on.iter().any(|p| p == HIGHPASS)));
        self.clipguard.set(filters.get("clipguard").map(truthy).unwrap_or(false) || (!wave3 && has(id("fastLookaheadLimiter"))));
        fx.insert("noise", noise_enabled);
        fx.insert("gate", has(id("gate")));
        fx.insert("sc4m", has(id("sc4m")));
        *self.fx_on.borrow_mut() = Some(on);
    }

    fn choices(&self) -> Choices {
        let wave3 = self.data.wave3 && is_wave(&self.mic.borrow(), "");
        let fx = self.fx.borrow();
        let mut offer: Vec<(Option<Plugin>, bool)> = Vec::new();
        if !wave3 {
            offer.push((effects::lookup(HIGHPASS, Some(&self.data.catalog), self.w.ctl.host().mixer.state_dir()), self.lowcut.get()));
        }
        offer.push((self.plugin("gate"), fx.get("gate").copied().unwrap_or(false)));
        offer.push((self.plugin("sc4m"), fx.get("sc4m").copied().unwrap_or(false)));
        if !wave3 {
            offer.push((self.plugin("fastLookaheadLimiter"), self.clipguard.get()));
        }
        let offer: Vec<(Plugin, bool)> = offer.into_iter().filter_map(|(p, on)| p.map(|p| (p, on))).collect();
        let fx_on = self.fx_on.borrow().clone();
        // nur Umgeschaltetes ändern (erneut aufgerufen): eigene Effekte und eingestellte Regler bleiben
        let mut set: HashMap<String, bool> = offer.iter()
            .filter(|(p, on)| fx_on.as_ref().map(|o| *on != o.contains(&p.id)).unwrap_or(true))
            .map(|(p, on)| (p.id.clone(), *on)).collect();
        if let Some(o) = &fx_on {
            if wave3 && !self.wave3_0.get() {
                // jetzt das Wave:3 gewählt: dessen Low Cut/Clipguard ersetzen die Effekte
                for pid in [Some(HIGHPASS.to_string()), self.plugin("fastLookaheadLimiter").map(|p| p.id)].into_iter().flatten() {
                    if o.contains(&pid) {
                        set.insert(pid, false);
                    }
                }
            }
        }
        let noise_want = !self.data.noise.is_empty() && fx.get("noise").copied().unwrap_or(false);
        let noise = (fx_on.is_none() || (!self.data.noise.is_empty() && noise_want != self.noise_on.get())).then_some(noise_want);
        Choices {
            mic: self.mic.borrow().clone(),
            output: self.out.borrow().clone(),
            wave3,
            lowcut: self.lowcut.get(),
            clipguard: self.clipguard.get(),
            gain: if wave3 { self.gain.get() } else { None },
            channels: self.channels.borrow().iter().filter(|(_, on)| *on).map(|(id, _)| id.clone()).collect(),
            mic_fx: offer.iter().filter(|(_, on)| *on).map(|(p, _)| effects::new_effect(p)).collect(),
            mic_fx_set: set,
            noise,
        }
    }

    fn apply(self: &Rc<Self>) {
        let ch = self.choices();
        let host = self.w.ctl.host().clone();
        let me = self.clone();
        bg(move || apply_choices(&host, &ch), move |r| {
            if let Some(l) = me.result.borrow().as_ref() {
                match r {
                    Err(e) => {
                        l.set_text(&tr!("Error: {error}", error = e));
                        l.add_css_class("danger");
                    }
                    Ok(()) => {
                        l.set_text(&tr!("Mixer and microphone are ready."));
                        l.add_css_class("ok");
                    }
                }
            }
            me.w.ctl.refresh();
        });
    }

    // -------------------------------------------------------------- Aufbau
    fn draw_steps(&self) {
        widgets::clear(&self.dots);
        let names = steps();
        let n = names.len();
        for (i, name) in names.iter().enumerate() {
            let (done, cur) = (i < self.step.get(), i == self.step.get());
            let dot = gtk::Box::new(gtk::Orientation::Horizontal, 0);
            dot.add_css_class("step-dot");
            if !(done || cur) {
                dot.add_css_class("todo");
            }
            dot.set_valign(Align::Center);
            if done {
                let g = theme::glyph("check", 12, "#ffffff");
                g.set_halign(Align::Center);
                g.set_size_request(22, 22);
                dot.append(&g);
            } else {
                let l = gtk::Label::new(Some(&(i + 1).to_string()));
                l.set_size_request(22, 22);
                dot.append(&l);
            }
            self.dots.append(&dot);
            let l = label(name, &["small", if cur { "bold" } else { "muted" }]);
            widgets::margins(&l, 0, 0, 0, 8);
            self.dots.append(&l);
            if i + 1 < n {
                let line = hbox(0);
                line.add_css_class("step-line");
                if done {
                    line.add_css_class("done");
                }
                line.set_hexpand(true);
                line.set_valign(Align::Center);
                widgets::margins(&line, 0, 10, 0, 10);
                self.dots.append(&line);
            }
        }
    }

    fn render(self: &Rc<Self>) {
        self.meter.borrow_mut().take();
        widgets::clear(&self.body);
        self.draw_steps();
        match self.step.get() {
            0 => self.step_welcome(),
            1 => self.step_mic(),
            2 => self.step_out(),
            3 => self.step_sound(),
            4 => self.step_channels(),
            _ => self.step_done(),
        }
        let n = self.step.get();
        self.back.set_sensitive(n > 0 && n < 5);
        let last = n == 5;
        widgets::set_button_text(&self.next, &if last { tr!("Finish") } else if n == 0 { tr!("Let's go") } else { tr!("Next") });
        self.skip.set_visible(!last);
    }

    fn title(&self, title: &str, text: &str) {
        self.body.append(&label(title, &["big-title"]));
        let t = widgets::wrap(text, &["muted"], 80);
        widgets::margins(&t, 4, 0, 16, 0);
        self.body.append(&t);
    }

    fn device_list(self: &Rc<Self>, items: &[Device], which: &'static str, icon: &'static str, badge: bool) {
        let b = vbox(8);
        self.body.append(&b);
        if items.is_empty() {
            b.append(&label(&tr!("No device found – is PipeWire (pipewire-pulse) running?"), &["warn"]));
            return;
        }
        let rows: Rc<RefCell<Vec<(String, gtk::Button, gtk::Image, gtk::DrawingArea)>>> = Rc::new(RefCell::new(Vec::new()));
        let paint = {
            let (rows, me) = (rows.clone(), self.clone());
            move || {
                let cur = if which == "mic" { me.mic.borrow().clone() } else { me.out.borrow().clone() };
                for (name, row, tile, dot) in rows.borrow().iter() {
                    let sel = *name == cur;
                    widgets::set_class(row, "selected", sel);
                    theme::set_tile(tile, icon, 30, if sel { theme::ACCENT } else { theme::RAISED }, 0.28, if sel { "#ffffff" } else { theme::MUTED });
                    dot.queue_draw();
                }
            }
        };
        let paint = Rc::new(paint);
        for d in items.iter().take(8) {
            let row = gtk::Button::new();
            row.add_css_class("row-pick");
            let r = hbox(12);
            let tile = theme::tile(icon, 30, theme::RAISED, 0.28, theme::MUTED);
            r.append(&tile);
            r.append(&label(&d.description, &["bold"]));
            if badge && is_wave(&d.name, &d.description) {
                r.append(&label("Elgato Wave", &["small", "bold", "ok"]));
            }
            r.append(&widgets::spacer());
            let dot = gtk::DrawingArea::new();
            dot.set_content_width(20);
            dot.set_content_height(20);
            let (me, name) = (self.clone(), d.name.clone());
            dot.set_draw_func(move |_, cr, _, _| {
                let cur = if which == "mic" { me.mic.borrow().clone() } else { me.out.borrow().clone() };
                let sel = cur == name;
                theme::set_source(cr, if sel { theme::ACCENT } else { theme::FAINT });
                cr.set_line_width(2.0);
                cr.arc(10.0, 10.0, 8.0, 0.0, std::f64::consts::TAU);
                let _ = cr.stroke();
                if sel {
                    cr.arc(10.0, 10.0, 4.0, 0.0, std::f64::consts::TAU);
                    let _ = cr.fill();
                }
            });
            r.append(&dot);
            row.set_child(Some(&r));
            let (me, name, p) = (self.clone(), d.name.clone(), paint.clone());
            row.connect_clicked(move |_| {
                if which == "mic" {
                    *me.mic.borrow_mut() = name.clone();
                    me.start_meter();
                } else {
                    *me.out.borrow_mut() = name.clone();
                }
                p();
            });
            b.append(&row);
            rows.borrow_mut().push((d.name.clone(), row, tile, dot));
        }
        paint();
    }

    fn step_welcome(&self) {
        let m = theme::mark(84);
        m.set_halign(Align::Center);
        widgets::margins(&m, 40, 0, 20, 0);
        self.body.append(&m);
        let t = label(&tr!("Welcome to FreeWave"), &["welcome"]);
        t.set_halign(Align::Center);
        self.body.append(&t);
        let txt = widgets::wrap(&tr!("In a few steps you set up your microphone, your headphones and the mixer – just like the first start of Elgato Wave Link. You can change everything later at any time."), &["muted"], 60);
        txt.set_justify(gtk::Justification::Center);
        txt.set_halign(Align::Center);
        txt.set_xalign(0.5);
        txt.set_margin_top(8);
        self.body.append(&txt);
    }

    fn start_meter(&self) {
        let m = LevelMeter::new(&self.mic.borrow());
        let ok = m.available();
        *self.meter.borrow_mut() = Some(m);
        if let Some(h) = self.body.last_child().and_then(|c| c.last_child()).and_then(|l| l.downcast::<gtk::Label>().ok()) {
            h.set_text(&if ok { String::new() } else { tr!("Level meter not available (parec from pulseaudio-utils is missing)") });
        }
    }

    fn step_mic(self: &Rc<Self>) {
        self.title(&tr!("Which microphone do you use?"),
                   &tr!("The microphone goes into your Stream Mix. Say something – the bar below shows whether it is coming through."));
        let inputs = self.data.inputs.clone();
        self.device_list(&inputs, "mic", "mic", true);
        let card = widgets::card(&[]);
        card.set_margin_top(10);
        widgets::field(&card, &tr!("LEVEL"));
        let cv = gtk::DrawingArea::new();
        cv.set_content_height(16);
        cv.set_hexpand(true);
        let me = Rc::downgrade(self);
        cv.set_draw_func(move |_, cr, w, _| {
            let w = w as f64;
            theme::fill_rounded(cr, 0.0, 4.0, w, 8.0, 4.0, theme::RAISED);
            let Some(me) = me.upgrade() else { return };
            let (lv, pk) = me.meter.borrow().as_ref().map(|m| m.read()).unwrap_or((0.0, 0.0));
            let col = if lv < 0.8 { theme::OK } else if lv < 0.95 { theme::WARN } else { theme::DANGER };
            if lv > 0.0 {
                theme::fill_rounded(cr, 0.0, 4.0, (w * lv).max(8.0), 8.0, 4.0, col);
            }
            if pk > 0.0 {
                theme::set_source(cr, theme::FG);
                cr.rectangle(w * pk - 2.0, 1.0, 2.0, 14.0);
                let _ = cr.fill();
            }
        });
        let c2 = cv.clone();
        gtk::glib::timeout_add_local(Duration::from_millis(60), move || {
            if c2.root().is_none() {
                return gtk::glib::ControlFlow::Break;
            }
            c2.queue_draw();
            gtk::glib::ControlFlow::Continue
        });
        card.append(&cv);
        let hint = label("", &["muted", "small"]);
        hint.set_margin_top(4);
        card.append(&hint);
        self.body.append(&card);
        self.start_meter();
    }

    fn step_out(self: &Rc<Self>) {
        self.title(&tr!("What do you listen on?"),
                   &tr!("This is where your Personal Mix goes – everything you want to hear yourself. Use the test tone to check that it is the right device."));
        let outputs = self.data.outputs.clone();
        self.device_list(&outputs, "out", "headphones", false);
        let b = widgets::button(&tr!("Play test tone"), Some("play"), "");
        b.set_halign(Align::Start);
        b.set_margin_top(10);
        let me = self.clone();
        b.connect_clicked(move |_| play_tone(&me.out.borrow()));
        self.body.append(&b);
    }

    fn switch_row(&self, parent: &gtk::Box, title: &str, text: &str, value: bool, enabled: bool, on: impl Fn(bool) + 'static) {
        if parent.first_child().is_some() {
            let s = widgets::sep();
            widgets::margins(&s, 6, 0, 6, 0);
            parent.append(&s);
        }
        let r = hbox(8);
        let t = vbox(1);
        t.set_hexpand(true);
        t.append(&label(title, if enabled { &["bold"] } else { &["bold", "faint"] }));
        t.append(&widgets::wrap(text, &["muted", "small"], 60));
        r.append(&t);
        let sw = Switch::new();
        sw.set(value && enabled);
        sw.set_enabled(enabled);
        sw.on_toggle(on);
        r.append(&sw.area);
        parent.append(&r);
    }

    fn step_sound(self: &Rc<Self>) {
        let wave3 = self.data.wave3 && is_wave(&self.mic.borrow(), "");
        self.title(&tr!("Your microphone's sound"), &if wave3 {
            tr!("Your Wave:3 applies Low Cut and Clipguard directly in the microphone. You can also turn on recommended effects – just like the plugins in Wave Link.")
        } else {
            tr!("Low Cut and Clipguard run as filters in FreeWave. You can also turn on recommended effects – just like the plugins in Wave Link.")
        });
        let card = widgets::card(&[]);
        let rows = vbox(0);
        card.append(&rows);
        let me = self.clone();
        self.switch_row(&rows, "Low Cut", &tr!("Removes hum and footfall noise"), self.lowcut.get(), true, move |v| me.lowcut.set(v));
        let me = self.clone();
        let clip_ok = wave3 || self.plugin("fastLookaheadLimiter").is_some();
        self.switch_row(&rows, "Clipguard", &tr!("Prevents clipping when you suddenly get loud"), self.clipguard.get(), clip_ok, move |v| me.clipguard.set(v));
        if wave3 {
            let s = widgets::sep();
            widgets::margins(&s, 6, 0, 6, 0);
            rows.append(&s);
            let g = hbox(8);
            let l = label("Gain", &["bold"]);
            l.set_width_chars(10);
            g.append(&l);
            let f = Fader::new(380, 28, 0.0, 100.0);
            f.set(self.gain.get().or(self.data.gain_pct.map(|g| g as f64)).unwrap_or(70.0));
            let me = self.clone();
            f.on_change(move |v| me.gain.set(Some(v)));
            g.append(&f.area);
            rows.append(&g);
        }
        self.body.append(&card);
        let s = widgets::section(&tr!("RECOMMENDED EFFECTS"));
        widgets::margins(&s, 14, 0, 6, 0);
        self.body.append(&s);
        let fxc = widgets::card(&[]);
        let rows = vbox(0);
        fxc.append(&rows);
        let have = !self.data.noise.is_empty();
        let text = if have {
            tr!("Filters out keyboard, fans and background noise ({backend})", backend = noise::backend_name(&self.data.noise[0]))
        } else {
            self.fx.borrow_mut().insert("noise", false);
            noise::install_hint()
        };
        let me = self.clone();
        let cur = self.fx.borrow().get("noise").copied().unwrap_or(false);
        self.switch_row(&rows, &tr!("Noise suppression"), &text, cur, have, move |v| {
            me.fx.borrow_mut().insert("noise", v);
        });
        for (key, title, text) in [("gate", tr!("Noise Gate"), tr!("Mutes the microphone while you pause speaking")),
                                   ("sc4m", tr!("Compressor"), tr!("Evens out loud and quiet passages"))] {
            let p = self.plugin(key);
            let text = if p.is_some() { text } else {
                self.fx.borrow_mut().insert(key, false);
                tr!("{text} – package “{package}” is missing", text = text, package = "swh-plugins")
            };
            let me = self.clone();
            let cur = self.fx.borrow().get(key).copied().unwrap_or(false);
            self.switch_row(&rows, &title, &text, cur, p.is_some(), move |v| {
                me.fx.borrow_mut().insert(key, v);
            });
        }
        self.body.append(&fxc);
    }

    fn step_channels(self: &Rc<Self>) {
        self.title(&tr!("Which channels do you want?"),
                   &tr!("Every app goes into a channel with its own controls for headphones and stream. You can create your own channels (e.g. for a capture card) later with “Add channel”."));
        let grid = gtk::Grid::new();
        grid.set_column_homogeneous(true);
        grid.set_column_spacing(12);
        grid.set_row_spacing(12);
        let names: HashMap<String, String> = default_channels().iter()
            .filter_map(|c| Some((c["id"].as_str()?.to_string(), c["name"].as_str()?.to_string()))).collect();
        for (i, (id, on)) in self.channels.borrow().iter().enumerate() {
            let (glyph, color) = channel_icon(id);
            let cell = hbox(12);
            cell.add_css_class("card");
            cell.append(&theme::tile(glyph, 34, color, 0.28, "#ffffff"));
            let l = label(names.get(id).map(String::as_str).unwrap_or(id), &["bold"]);
            l.set_hexpand(true);
            cell.append(&l);
            let sw = Switch::new();
            sw.set(*on);
            let (me, i2) = (self.clone(), i);
            sw.on_toggle(move |v| me.channels.borrow_mut()[i2].1 = v);
            cell.append(&sw.area);
            grid.attach(&cell, (i % 2) as i32, (i / 2) as i32, 1, 1);
        }
        self.body.append(&grid);
    }

    fn step_done(self: &Rc<Self>) {
        let t = theme::tile("check", 56, theme::OK, 0.5, "#ffffff");
        t.set_halign(Align::Center);
        widgets::margins(&t, 6, 0, 12, 0);
        self.body.append(&t);
        let l = label(&tr!("All set!"), &["big-title"]);
        l.set_halign(Align::Center);
        self.body.append(&l);
        let result = label(&tr!("Setting up mixer …"), &["muted"]);
        result.set_halign(Align::Center);
        widgets::margins(&result, 4, 0, 16, 0);
        self.body.append(&result);
        *self.result.borrow_mut() = Some(result);
        let card = widgets::card(&["big"]);
        let fx_on = self.fx.borrow().values().any(|v| *v) || (self.lowcut.get() && !self.data.wave3);
        let tips = [("stream", "OBS Studio".to_string(), tr!("Add the audio input “Monitor of FreeWave Stream Mix” – it contains everything your audience should hear.")),
                    ("chat", "Discord, Zoom & Co.".to_string(), if fx_on { tr!("Select “FreeWave Microphone (Effects)” as the microphone.") } else { tr!("Select your own microphone – or the Stream Mix if others should hear everything.") }),
                    ("grid", tr!("Apps"), tr!("New apps automatically go into the matching channel; you can move them to another channel in the mixer under “Apps”."))];
        for (i, (glyph, title, text)) in tips.iter().enumerate() {
            if i > 0 {
                let s = widgets::sep();
                widgets::margins(&s, 10, 0, 10, 0);
                card.append(&s);
            }
            let r = hbox(12);
            let tl = theme::tile(glyph, 32, theme::ACCENT_SOFT, 0.28, theme::ACCENT);
            tl.set_valign(Align::Start);
            r.append(&tl);
            let t = vbox(2);
            t.set_hexpand(true);
            t.append(&label(title, &["bold"]));
            t.append(&widgets::wrap(text, &["muted", "small"], 70));
            r.append(&t);
            card.append(&r);
        }
        self.body.append(&card);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fx(p: &str, on: bool) -> Effect {
        Effect { plugin: p.into(), name: p.into(), enabled: on, ..Default::default() }
    }

    #[test]
    fn merge_keeps_own_effects() {
        let old = vec![fx("eq", true), fx("gate", false), fx("comp", true)];
        let new = vec![fx("hp", true), fx("gate", true), fx("comp", true), fx("lim", true)];
        let changes: HashMap<String, bool> = [("hp".to_string(), true), ("gate".to_string(), true), ("comp".to_string(), false), ("lim".to_string(), true)].into();
        let out = merge_chain(&old, &new, &changes);
        let ids: Vec<&str> = out.iter().map(|f| f.plugin.as_str()).collect();
        // „hp“ kommt vor den ersten späteren Effekt des Assistenten („gate“), „lim“ ans Ende
        assert_eq!(ids, ["eq", "hp", "gate", "lim"]);
        assert!(out[2].enabled);
        // erste Einrichtung ohne alte Kette
        let out = merge_chain(&[], &new, &changes);
        assert_eq!(out.iter().map(|f| f.plugin.as_str()).collect::<Vec<_>>(), ["hp", "gate", "lim"]);
    }

    #[test]
    fn channels_follow_choice() {
        let cur = vec![json!({"id": "music", "name": "Musik"}), json!({"id": "cap", "name": "Capture", "kind": "input"}),
                       json!({"id": "sfx", "name": "SFX"})];
        let out = merged_channels(&cur, &["music".to_string(), "game".to_string()]);
        let ids: Vec<&str> = out.iter().map(|c| c["id"].as_str().unwrap()).collect();
        assert_eq!(ids, ["music", "cap", "game"]);
        assert_eq!(out[0]["name"], "Musik"); // bestehende bleiben, wie sie sind
        // nichts für Programme gewählt: „System“ bleibt
        let out = merged_channels(&[], &[]);
        assert_eq!(out[0]["id"], "system");
    }

    #[test]
    fn wave_detection_and_tone() {
        assert!(is_wave("alsa_input.usb-Elgato_Systems_Elgato_Wave_3", ""));
        assert!(is_wave("x", "Elgato Wave:3 Mono"));
        assert!(!is_wave("alsa_input.pci", "Built-in Audio"));
        assert!(!is_wave("microwave", ""));
    }
}
