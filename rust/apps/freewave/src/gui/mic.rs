//! Ansicht „Mikrofon“ (waveui.py: _build_mic, _build_noise, _build_ducking): Gain und Stumm, Low Cut und
//! Clipguard, beim Wave:3 Kopfhörer, Mithören und LEDs; Rauschunterdrückung und Auto-Ducking.

use std::cell::{Cell, RefCell};
use std::rc::Rc;
use std::sync::Arc;
use std::time::{Duration, Instant};

use gtk::prelude::*;
use gtk::Align;
use gtk4 as gtk;

use freestream_core::tr;
use freestream_wave::control::Snapshot;
use freestream_wave::ducking::{self, AMOUNTS};
use freestream_wave::levels::level_db;
use freestream_wave::noise::{self, MAX_STRENGTH};
use freestream_wave::wavemic;
use serde_json::{json, Value};

use super::theme;
use super::widgets::{self, hbox, label, vbox, Fader, Switch, Toggle, ToggleKind};
use super::{bg, Win};
use crate::cli::obj;

const DUCK_MS: u64 = 80;

/// „40 % mic“ (Mithören des Wave:3) – Python-Formatangabe im Text.
fn mic_pct(v: f64) -> String {
    freestream_core::i18n::gettext("{value:.0f} % mic").replace("{value:.0f}", &format!("{v:.0}"))
}

pub struct MicView {
    pub root: gtk::ScrolledWindow,
    name: gtk::Label,
    hint: gtk::Label,
    gain: Fader,
    gain_val: gtk::Label,
    mute: Toggle,
    lowcut: Switch,
    clipguard: Switch,
    freq_box: gtk::Box,
    freq: RefCell<Vec<gtk::ToggleButton>>,
    hw_card: gtk::Box,
    no_hw: gtk::Box,
    hp: Fader,
    hp_val: gtk::Label,
    hp_mute: Toggle,
    dm: Fader,
    dm_val: gtk::Label,
    leds_off: Switch,
    leds_flip: Switch,
    noise: Switch,
    noise_status: gtk::Label,
    noise_strength: gtk::Box,
    noise_fader: Fader,
    noise_val: gtk::Label,
    duck_on: Switch,
    duck_chips: gtk::FlowBox,
    duck_chip_sig: RefCell<String>,
    duck_amount: RefCell<Vec<(i64, gtk::ToggleButton)>>,
    duck_thr: Fader,
    duck_thr_val: gtk::Label,
    duck_meter: gtk::DrawingArea,
    duck_state: Rc<RefCell<(f64, f64, bool)>>,
    duck_text: gtk::Label,
    /// Gain ohne Wave:3 (pactl, im Hintergrund gelesen)
    soft_gain: RefCell<Option<(i64, bool)>>,
    gain_asked: Cell<Option<Instant>>,
    settings: RefCell<(bool, bool, i64)>,
}

fn row(parent: &gtk::Box, title: &str, sub: &str) -> gtk::Box {
    let r = hbox(8);
    widgets::margins(&r, 8, 0, 8, 0);
    let t = vbox(1);
    t.set_hexpand(true);
    t.append(&label(title, &["bold"]));
    if !sub.is_empty() {
        t.append(&widgets::wrap(sub, &["muted", "small"], 45));
    }
    r.append(&t);
    parent.append(&r);
    r
}

impl MicView {
    pub fn new() -> Self {
        let outer = hbox(14);
        widgets::margins(&outer, 18, 22, 18, 22);
        // --- Karte Mikrofon
        let card = widgets::card(&["big"]);
        card.set_valign(Align::Start);
        card.set_size_request(430, -1);
        let top = hbox(14);
        top.append(&theme::tile("mic", 44, "#e5484d", 0.3, "#ffffff"));
        let names = vbox(2);
        let name = label("", &["mic-name"]);
        let hint = widgets::wrap("", &["muted", "small"], 45);
        names.append(&name);
        names.append(&hint);
        top.append(&names);
        card.append(&top);
        let s = widgets::section("Gain");
        widgets::margins(&s, 22, 0, 6, 0);
        card.append(&s);
        let g = hbox(8);
        let gain = Fader::new(380, 24, 0.0, 100.0);
        gain.area.set_hexpand(true);
        g.append(&gain.area);
        let mute = Toggle::new(ToggleKind::Round, 40, 40, ("mic", "mic_off"), theme::DANGER);
        g.append(&mute.area);
        card.append(&g);
        let gain_val = label("–", &["bold", "muted"]);
        widgets::margins(&gain_val, 4, 0, 10, 0);
        card.append(&gain_val);
        card.append(&widgets::sep());
        let s = widgets::section(&tr!("Filters"));
        widgets::margins(&s, 12, 0, 0, 0);
        card.append(&s);
        let r = row(&card, "Low Cut", &tr!("Filters out low hum and footfall noise"));
        let freq_box = hbox(0);
        r.append(&freq_box);
        let lowcut = Switch::new();
        r.append(&lowcut.area);
        let r = row(&card, "Clipguard", &tr!("Prevents clipping when you suddenly get loud"));
        let clipguard = Switch::new();
        r.append(&clipguard.area);
        outer.append(&card);

        // --- rechte Spalte: Wave:3, Rauschunterdrückung, Auto-Ducking
        let right = vbox(14);
        right.set_size_request(440, -1);
        right.set_valign(Align::Start);
        let hw_card = widgets::card(&["big"]);
        hw_card.append(&widgets::section("Wave:3"));
        let r = row(&hw_card, &tr!("Headphones"), "");
        let hp_val = label("–", &["muted"]);
        hp_val.set_width_chars(8);
        r.append(&hp_val);
        let hp_mute = Toggle::new(ToggleKind::Round, 36, 36, ("headphones", "headphones_off"), theme::DANGER);
        r.append(&hp_mute.area);
        let hp = Fader::new(360, 24, -60.0, 0.0);
        hp.area.set_halign(Align::Start);
        hw_card.append(&hp.area);
        let r = row(&hw_card, &tr!("Monitoring"), &tr!("Balance of mic ↔ PC audio in your headphones"));
        let dm_val = label("–", &["muted"]);
        dm_val.set_width_chars(12);
        r.append(&dm_val);
        let mixrow = hbox(6);
        mixrow.append(&label("PC", &["faint", "small"]));
        let dm = Fader::new(320, 24, 0.0, 100.0);
        mixrow.append(&dm.area);
        mixrow.append(&label(&tr!("Mic"), &["faint", "small"]));
        hw_card.append(&mixrow);
        let line = widgets::sep();
        widgets::margins(&line, 12, 0, 12, 0);
        hw_card.append(&line);
        let r = row(&hw_card, &tr!("LEDs off"), "");
        let leds_off = Switch::new();
        r.append(&leds_off.area);
        let r = row(&hw_card, &tr!("Flip LEDs"), &tr!("If the microphone is mounted upside down on a boom arm"));
        let leds_flip = Switch::new();
        r.append(&leds_flip.area);
        right.append(&hw_card);
        let no_hw = widgets::card(&["big"]);
        no_hw.append(&label(&tr!("More settings with the Elgato Wave:3"), &["bold"]));
        let t = widgets::wrap(&tr!("Headphone volume, monitoring and the LEDs can be adjusted directly on the Wave:3. For Wave:1, Wave Neo and Wave XLR the protocol for this is not documented."), &["muted", "small"], 50);
        t.set_margin_top(6);
        no_hw.append(&t);
        right.append(&no_hw);
        // Rauschunterdrückung
        let ncard = widgets::card(&["big"]);
        let r = row(&ncard, &tr!("Noise suppression"), &tr!("Filters out keyboard, fans and background noise"));
        let noise_sw = Switch::new();
        r.append(&noise_sw.area);
        let noise_status = widgets::wrap("", &["muted", "small"], 50);
        ncard.append(&noise_status);
        let noise_strength = vbox(0);
        noise_strength.set_margin_top(4);
        let r = row(&noise_strength, &tr!("Strength"), &tr!("Higher values mute everything that does not clearly sound like a voice"));
        let noise_val = label("", &["muted"]);
        noise_val.set_width_chars(6);
        r.append(&noise_val);
        let noise_fader = Fader::new(360, 24, 0.0, MAX_STRENGTH as f64);
        noise_fader.area.set_halign(Align::Start);
        noise_strength.append(&noise_fader.area);
        noise_strength.set_visible(false);
        ncard.append(&noise_strength);
        right.append(&ncard);
        // Auto-Ducking
        let dcard = widgets::card(&["big"]);
        let top = hbox(0);
        top.append(&widgets::section(&tr!("Auto-ducking")));
        top.append(&widgets::spacer());
        let duck_on = Switch::new();
        top.append(&duck_on.area);
        dcard.append(&top);
        let t = widgets::wrap(&tr!("Turns the selected channels down in both mixes while you speak and brings them back exactly afterwards."), &["muted", "small"], 50);
        t.set_margin_top(6);
        dcard.append(&t);
        let s = widgets::section(&tr!("Channels"));
        widgets::margins(&s, 16, 0, 6, 0);
        dcard.append(&s);
        let duck_chips = gtk::FlowBox::new();
        duck_chips.set_selection_mode(gtk::SelectionMode::None);
        duck_chips.set_max_children_per_line(8);
        duck_chips.set_column_spacing(6);
        duck_chips.set_row_spacing(6);
        dcard.append(&duck_chips);
        let s = widgets::section(&tr!("Lower by"));
        widgets::margins(&s, 16, 0, 6, 0);
        dcard.append(&s);
        let amount_box = hbox(0);
        dcard.append(&amount_box);
        let r = hbox(0);
        widgets::margins(&r, 16, 0, 6, 0);
        r.append(&widgets::section(&tr!("Sensitivity")));
        r.append(&widgets::spacer());
        let duck_thr_val = label("", &["muted", "small"]);
        r.append(&duck_thr_val);
        dcard.append(&r);
        let duck_thr = Fader::new(360, 22, -60.0, -5.0);
        duck_thr.area.set_halign(Align::Start);
        dcard.append(&duck_thr.area);
        let duck_state = Rc::new(RefCell::new((0.0, -30.0, false)));
        let duck_meter = gtk::DrawingArea::new();
        duck_meter.set_content_width(360);
        duck_meter.set_content_height(16);
        duck_meter.set_halign(Align::Start);
        duck_meter.set_margin_top(8);
        let st = duck_state.clone();
        duck_meter.set_draw_func(move |_, cr, w, _| {
            // Mikrofonpegel mit Markierung der Schwelle (gleiche Skala wie der Regler)
            let (level, thr, ducking) = *st.borrow();
            let (w, h, y) = (w as f64, 10.0, 3.0);
            let x_of = |db: f64| ((db + 60.0) / 55.0).clamp(0.0, 1.0) * w;
            theme::fill_rounded(cr, 0.0, y, w, h, h / 2.0, theme::RAISED);
            if let Some(db) = level_db(level).filter(|d| *d > -60.0) {
                theme::fill_rounded(cr, 0.0, y, x_of(db).max(h), h, h / 2.0, if ducking { theme::WARN } else { theme::OK });
            }
            let x = x_of(thr);
            theme::set_source(cr, theme::FG);
            cr.set_line_width(2.0);
            cr.move_to(x, 0.0);
            cr.line_to(x, h + 6.0);
            let _ = cr.stroke();
        });
        dcard.append(&duck_meter);
        let duck_text = widgets::wrap("", &["muted", "small"], 50);
        duck_text.set_margin_top(6);
        dcard.append(&duck_text);
        right.append(&dcard);
        outer.append(&right);
        let root = widgets::scroll(&outer);
        root.set_hscrollbar_policy(gtk::PolicyType::Automatic);
        let me = Self {
            root, name, hint, gain, gain_val, mute, lowcut, clipguard, freq_box, freq: RefCell::new(Vec::new()), hw_card, no_hw,
            hp, hp_val, hp_mute, dm, dm_val, leds_off, leds_flip, noise: noise_sw, noise_status, noise_strength, noise_fader,
            noise_val, duck_on, duck_chips, duck_chip_sig: RefCell::new(String::new()), duck_amount: RefCell::new(Vec::new()),
            duck_thr, duck_thr_val, duck_meter, duck_state, duck_text, soft_gain: RefCell::new(None), gain_asked: Cell::new(None),
            settings: RefCell::new((false, false, 80)),
        };
        me.amount_box_fill(&amount_box);
        me
    }

    fn amount_box_fill(&self, b: &gtk::Box) {
        let items: Vec<(String, String, Option<&str>)> = AMOUNTS.iter().map(|a| (a.to_string(), format!("−{a} dB"), None)).collect();
        let refs: Vec<(&str, String, Option<&str>)> = items.iter().map(|(k, t, i)| (k.as_str(), t.clone(), *i)).collect();
        let (seg, btns) = widgets::segmented(&refs, "12", |_| {});
        seg.set_halign(Align::Start);
        b.append(&seg);
        *self.duck_amount.borrow_mut() = AMOUNTS.iter().copied().zip(btns).collect();
    }

    /// Name des Mikrofons (Unterzeile im Kopf).
    pub fn name(&self) -> String {
        self.name.text().to_string()
    }
}

fn save_section(w: &Rc<Win>, section: &'static str, key: &str, value: Value) {
    let key = key.to_string();
    w.ctl.act_host(move |h| {
        h.mixer.with_settings_mut(|s| {
            obj(obj(s.data_mut(), "mixer"), section).insert(key, value);
            if let Err(e) = s.save() {
                log::warn!("config.json: {e}");
            }
        });
        Ok(())
    });
}

/// Regler und Schalter verbinden.
pub fn init(w: &Rc<Win>) {
    let v = &w.mic;
    let w2 = w.clone();
    v.gain.on_change(move |val| {
        let pct = val.round();
        w2.mic.gain_val.set_text(&format!("{pct} %"));
        w2.debounced("gain", 120, move |h| h.mic.lock().unwrap().set_gain(pct));
    });
    let w2 = w.clone();
    v.mute.on_toggle(move |on| {
        w2.touch("mute");
        w2.mic.gain.set_dimmed(on);
        w2.ctl.act_host(move |h| h.mic.lock().unwrap().set_mute(None).map(|_| ()));
    });
    for (key, sw) in [("lowcut", &v.lowcut), ("clipguard", &v.clipguard)] {
        let w2 = w.clone();
        sw.on_toggle(move |on| {
            w2.touch(key);
            w2.job(move |h| {
                let mut mic = h.mic.lock().unwrap();
                if mic.hw().is_some() {
                    mic.hw_set(key, if on { 1.0 } else { 0.0 })?;
                    mic.settings.insert(key.into(), json!(on));
                    Ok(())
                } else {
                    mic.set(&[(key, json!(on))]).map(|_| ())
                }
            }, move |w, _| {
                let name = if key == "lowcut" { "Low Cut" } else { "Clipguard" };
                w.status(&if on { tr!("{name} on", name = name) } else { tr!("{name} off", name = name) });
            });
        });
    }
    let (seg, btns) = widgets::segmented(&[("80", "80 Hz".into(), None), ("120", "120 Hz".into(), None)], "80", {
        let w2 = w.clone();
        move |f| {
            w2.touch("lowcut_freq");
            let f: i64 = f.parse().unwrap_or(80);
            w2.ctl.act_host(move |h| h.mic.lock().unwrap().set(&[("lowcut_freq", json!(f))]).map(|_| ()));
        }
    });
    seg.set_margin_end(12);
    v.freq_box.append(&seg);
    *v.freq.borrow_mut() = btns;
    for (key, sw) in [("leds_off", &v.leds_off), ("leds_flip", &v.leds_flip)] {
        let w2 = w.clone();
        sw.on_toggle(move |on| {
            w2.touch(key);
            w2.ctl.act_host(move |h| h.mic.lock().unwrap().hw_set(key, if on { 1.0 } else { 0.0 }).map(|_| ()));
        });
    }
    let w2 = w.clone();
    v.hp_mute.on_toggle(move |on| {
        w2.touch("headphone_mute");
        w2.mic.hp.set_dimmed(on);
        w2.ctl.act_host(move |h| h.mic.lock().unwrap().hw_set("headphone_mute", if on { 1.0 } else { 0.0 }).map(|_| ()));
    });
    let w2 = w.clone();
    v.hp.on_change(move |val| {
        w2.mic.hp_val.set_text(&format!("{val:.0} dB"));
        w2.debounced("headphone_volume", 120, move |h| h.mic.lock().unwrap().hw_set("headphone_volume", val).map(|_| ()));
    });
    let w2 = w.clone();
    v.dm.on_change(move |val| {
        w2.mic.dm_val.set_text(&mic_pct(val));
        w2.debounced("direct_monitor", 120, move |h| h.mic.lock().unwrap().hw_set("direct_monitor", val).map(|_| ()));
    });
    let w2 = w.clone();
    v.noise.on_toggle(move |on| {
        w2.touch("noise");
        w2.job(move |h| {
            let ns = h.mixer.noise();
            ns.set(Some(on), None, None)?;
            ns.apply().map(|_| ())
        }, move |w, _| w.status(&if on { tr!("Noise suppression on") } else { tr!("Noise suppression off") }));
    });
    let w2 = w.clone();
    v.noise_fader.on_change(move |val| {
        let v = val.round();
        w2.mic.noise_val.set_text(&format!("{v} %"));
        w2.debounced("noise_strength", 300, move |h| h.mixer.noise().set_strength(v).map(|_| ()));
    });
    let w2 = w.clone();
    v.duck_on.on_toggle(move |on| {
        w2.touch("duck:enabled");
        save_section(&w2, "ducking", "enabled", json!(on));
    });
    for (a, b) in v.duck_amount.borrow().iter() {
        let (w2, a) = (w.clone(), *a);
        b.connect_toggled(move |b| {
            if b.is_active() && !w2.held("duck:set_amount") {
                w2.touch("duck:amount_db");
                save_section(&w2, "ducking", "amount_db", json!(a));
            }
        });
    }
    let w2 = w.clone();
    v.duck_thr.on_change(move |val| {
        let db = val.round() as i64;
        w2.mic.duck_thr_val.set_text(&format!("{db} dB"));
        w2.mic.duck_state.borrow_mut().1 = db as f64;
        w2.mic.duck_meter.queue_draw();
        w2.touch("duck:threshold_db");
        // nach dem Loslassen speichern (entprellt)
        w2.debounced("duck:threshold_db", 300, move |h| {
            h.mixer.with_settings_mut(|s| {
                obj(obj(s.data_mut(), "mixer"), "ducking").insert("threshold_db".into(), json!(db));
                s.save()
            }).map_err(|e| freestream_wave::Error(e.to_string()))
        });
    });
}

/// Mikrofonpegel und Zustand des Auto-Duckings anzeigen (nur in der Ansicht Mikrofon).
pub fn start_ticks(w: &Rc<Win>) {
    let w2 = w.clone();
    gtk::glib::timeout_add_local(Duration::from_millis(DUCK_MS), move || {
        if w2.win.is_visible() && *w2.view.borrow() == "mic" {
            let d = &w2.ctl.host().ducker;
            let conf = ducking::settings(&w2.conf.borrow());
            let s = w2.snap.borrow().clone();
            let level = if conf.enabled { d.level() } else { 0.0 };
            let ducking = d.ducking();
            {
                let mut st = w2.mic.duck_state.borrow_mut();
                if (st.0 - level).abs() > 0.004 || st.2 != ducking {
                    st.0 = level;
                    st.2 = ducking;
                    w2.mic.duck_meter.queue_draw();
                }
            }
            let text = if !conf.enabled {
                tr!("Switched off")
            } else if !s.active {
                tr!("Works as soon as the mixer is set up.")
            } else if ducking {
                tr!("You are speaking – channels are lowered.")
            } else {
                tr!("Listening – speak to see the level reach the marker.")
            };
            if w2.mic.duck_text.text() != text {
                w2.mic.duck_text.set_text(&text);
            }
        }
        gtk::glib::ControlFlow::Continue
    });
}

pub fn apply(w: &Rc<Win>, s: &Arc<Snapshot>) {
    let v = &w.mic;
    let mic = &s.mic;
    let hw = mic.hardware;
    let present = hw || mic.source.is_some();
    let f = |k: &str| mic.fields.get(k).map(|x| x.as_f64());
    let b = |k: &str| mic.fields.get(k).map(|x| x.as_bool());
    // Einstellungen des Mikrofons (ohne zu warten, falls es gerade beschäftigt ist)
    if let Ok(m) = w.ctl.host().mic.try_lock() {
        let st = m.mic_settings();
        *v.settings.borrow_mut() = (st.lowcut, st.clipguard, st.lowcut_freq);
    }
    let (set_lowcut, set_clip, freq) = *v.settings.borrow();
    // Gain ohne Wave:3 über pactl – im Hintergrund, höchstens alle 2 s
    if !hw && mic.source.is_some() && w.win.is_visible() && v.gain_asked.get().is_none_or(|t| t.elapsed() > Duration::from_secs(2)) {
        v.gain_asked.set(Some(Instant::now()));
        let (host, w2) = (w.ctl.host().clone(), w.clone());
        bg(move || host.mic.lock().map(|mut m| m.gain().ok()).ok().flatten(), move |g| {
            if g.is_some() {
                *w2.mic.soft_gain.borrow_mut() = g;
                let s = w2.snap.borrow().clone();
                apply(&w2, &s);
            }
        });
    }
    let gain_pct: Option<i64> = if hw { f("gain").map(|g| (g * 2.5).round() as i64) } else if present { v.soft_gain.borrow().map(|g| g.0) } else { None };
    let muted = if hw { b("mute").unwrap_or(false) } else { v.soft_gain.borrow().map(|g| g.1).unwrap_or(false) };
    let name = if hw { "Elgato Wave:3".to_string() } else if present { "Elgato Wave".to_string() } else { tr!("No Wave microphone") };
    v.name.set_text(&name);
    if present {
        let mut parts = Vec::new();
        if muted {
            parts.push(tr!("Muted"));
        }
        if let Some(g) = gain_pct {
            parts.push(tr!("Gain {value} %", value = g));
        }
        let lowcut = if hw { b("lowcut").unwrap_or(set_lowcut) } else { set_lowcut };
        let clip = if hw { b("clipguard").unwrap_or(set_clip) } else { set_clip };
        if lowcut {
            parts.push("Low Cut".into());
        }
        if clip {
            parts.push("Clipguard".into());
        }
        let sub = parts.join(" · ");
        w.mic_card_set(&name, if sub.is_empty() { " " } else { &sub }, if muted { "danger" } else { "ok" }, false);
    } else {
        w.mic_card_set(&tr!("Microphone"), &tr!("No Wave microphone"), "", true);
    }
    let hint = if hw {
        let mut t = tr!("All settings are stored directly in the microphone.");
        if !mic.api.is_empty() {
            t += &format!(" {}", tr!("Firmware API {api}.", api = mic.api));
        }
        t
    } else if present {
        let mut t = tr!("Low Cut and Clipguard run as PipeWire filters. In Discord/OBS, select the microphone “FreeWave Microphone”.");
        if !wavemic::limiter_available() {
            t += &format!(" {}", tr!("Clipguard needs the package swh-plugins."));
        }
        t
    } else {
        tr!("Connect a microphone – the settings are applied as soon as it is detected.")
    };
    v.hint.set_text(&hint);
    v.gain.set_enabled(gain_pct.is_some()); // ohne Mikrofon nichts zu regeln
    if let Some(g) = gain_pct.filter(|_| !w.held("gain")) {
        v.gain.set(g as f64);
        let db = if hw { f("gain").map(|x| format!("  ·  {x:.1} dB")).unwrap_or_default() } else { String::new() };
        v.gain_val.set_text(&format!("{g} %{db}"));
    }
    if !w.held("mute") {
        v.mute.set(muted);
        v.gain.set_dimmed(muted || gain_pct.is_none());
    }
    if !w.held("lowcut") {
        v.lowcut.set(if hw { b("lowcut").unwrap_or(set_lowcut) } else { set_lowcut });
    }
    if !w.held("clipguard") {
        v.clipguard.set(if hw { b("clipguard").unwrap_or(set_clip) } else { set_clip });
    }
    if !w.held("lowcut_freq") {
        let f = if freq == 120 { 1 } else { 0 };
        if let Some(bt) = v.freq.borrow().get(f) {
            if !bt.is_active() {
                bt.set_active(true);
            }
        }
    }
    v.freq_box.set_visible(!hw); // das Wave:3 filtert mit fester Frequenz selbst
    v.hw_card.set_visible(hw);
    v.no_hw.set_visible(!hw);
    if hw {
        for (k, sw) in [("leds_off", &v.leds_off), ("leds_flip", &v.leds_flip)] {
            if let Some(x) = b(k).filter(|_| !w.held(k)) {
                sw.set(x);
            }
        }
        v.leds_flip.set_enabled(mic.fields.contains_key("leds_flip"));
        if !w.held("headphone_mute") {
            v.hp_mute.set(b("headphone_mute").unwrap_or(false));
        }
        if let Some(x) = f("headphone_volume").filter(|_| !w.held("headphone_volume")) {
            v.hp.set(x);
            v.hp_val.set_text(&format!("{x:.0} dB"));
            v.hp.set_dimmed(b("headphone_mute").unwrap_or(false));
        }
        if let Some(x) = f("direct_monitor").filter(|_| !w.held("direct_monitor")) {
            v.dm.set(x);
            v.dm_val.set_text(&mic_pct(x));
        }
    }
    apply_noise(w, s);
    apply_duck(w, s);
}

fn apply_noise(w: &Rc<Win>, s: &Snapshot) {
    let v = &w.mic;
    let ns = &s.noise;
    let have = !ns.available.is_empty();
    if !w.held("noise") {
        v.noise.set(ns.enabled);
    }
    v.noise.set_enabled(have || ns.enabled);
    let backend = noise::backend_name(ns.backend.as_deref().unwrap_or(""));
    let text = if !have {
        ns.hint.clone()
    } else if !ns.enabled {
        tr!("Off – method: {backend}", backend = backend)
    } else if ns.running {
        tr!("Active: {backend}. The microphone channel in the mixer uses the filtered signal; in Discord/OBS you can also select “FreeWave Microphone (noise suppression)”.", backend = backend)
    } else if !s.active {
        tr!("Starts as soon as the mixer is set up ({backend}).", backend = backend)
    } else {
        tr!("Could not start ({backend}).", backend = backend)
    };
    v.noise_status.set_text(&text);
    let rn = ns.backend.as_deref() == Some("rnnoise") && have;
    v.noise_strength.set_visible(rn);
    if rn && !w.held("noise_strength") {
        v.noise_fader.set(ns.strength as f64);
        v.noise_val.set_text(&format!("{} %", ns.strength));
    }
}

fn apply_duck(w: &Rc<Win>, s: &Snapshot) {
    let v = &w.mic;
    let conf = ducking::settings(&w.conf.borrow());
    if !w.held("duck:enabled") {
        v.duck_on.set(conf.enabled);
    }
    if !w.held("duck:amount_db") {
        w.touch("duck:set_amount");
        for (a, b) in v.duck_amount.borrow().iter() {
            if *a == conf.amount_db as i64 && !b.is_active() {
                b.set_active(true);
            }
        }
        w.touched_remove("duck:set_amount");
    }
    if !w.held("duck:threshold_db") {
        let thr = conf.threshold_db.round();
        v.duck_thr.set(thr);
        v.duck_thr_val.set_text(&format!("{thr} dB"));
        v.duck_state.borrow_mut().1 = thr;
        v.duck_meter.queue_draw();
    }
    if !w.held("duck:channels") {
        fill_duck_chips(w, s, &conf.channels);
    }
}

fn fill_duck_chips(w: &Rc<Win>, s: &Snapshot, selected: &[String]) {
    let v = &w.mic;
    let chans: Vec<(String, String)> = s.channels.iter().filter(|c| c.channel.id != "mic")
        .map(|c| (c.channel.id.clone(), c.channel.name.clone())).collect();
    let sig = format!("{chans:?}{selected:?}");
    if *v.duck_chip_sig.borrow() == sig {
        return;
    }
    *v.duck_chip_sig.borrow_mut() = sig;
    while let Some(c) = v.duck_chips.first_child() {
        v.duck_chips.remove(&c);
    }
    for (id, name) in chans {
        let on = selected.contains(&id);
        let chip = widgets::chip(&name, on);
        let (w2, sel) = (w.clone(), selected.to_vec());
        chip.connect_clicked(move |_| {
            // vom Gezeigten ausgehen: die Einstellung schreibt erst der Arbeits-Thread
            let mut chans = sel.clone();
            if chans.contains(&id) {
                chans.retain(|c| *c != id);
            } else {
                chans.push(id.clone());
            }
            w2.touch("duck:channels");
            save_section(&w2, "ducking", "channels", json!(chans));
            let s = w2.snap.borrow().clone();
            fill_duck_chips(&w2, &s, &chans);
        });
        v.duck_chips.insert(&chip, -1);
    }
}
