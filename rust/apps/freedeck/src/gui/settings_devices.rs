//! Einstellungen › Geräte (wie `DevicesPage` in `freestream/freedecksettings.py`): links die
//! angeschlossenen Geräte und das virtuelle Deck, rechts Name, aktiv, Ausrichtung, Ruhezustand,
//! Helligkeit, Bild „läuft nicht“, Modell, Seriennummer, Firmware und Profil.
//!
//! Alles steht in config.json unter `devices.<Seriennummer>` bzw. `virtual_deck`.

use gtk4 as gtk;
use gtk::prelude::*;
use gtk::{Align, Orientation};

use freestream_core::config::Config;
use freestream_core::i18n::ngettext;
use freestream_core::models::{model_geometry, model_name, VIRTUAL_MODELS};
use freestream_core::profiles::ProfileStore;
use freestream_core::{paths, tr};
use freestream_deck::engine::Controller;
use serde_json::{json, Map, Value};
use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;
use std::sync::{Arc, Mutex};

use super::settings::{button, cfg_edit, cfg_read, choose_file, clear, dropdown, form, form_hint, form_row, message, panel,
                      section, split, Ctx, Debounce, InfoCard};
use super::util::label;

const SLEEP_CHOICES: [i64; 7] = [0, 1, 5, 10, 15, 30, 60];
/// Seriennummer des virtuellen Decks (wie `virtualdeck.SERIAL`).
pub const VIRTUAL_SERIAL: &str = "VIRTUAL";
const VIRTUAL_SIZES: [&str; 4] = ["FDV3X2", "FDV4X2", "FDV5X3", "FDV8X4"];
const VIRTUAL_DEFAULT: &str = "FDV4X2";
const DEVICE_COLOR: &str = "#3a4250";
const ACCENT: &str = "#3d9bff";

fn upright(code: &str) -> &'static str {
    match code {
        "FDV3X2" => "FDV2X3",
        "FDV5X3" => "FDV3X5",
        "FDV8X4" => "FDV4X8",
        _ => "FDV2X4",
    }
}

/// Text für „Ruhezustand nach“.
pub fn sleep_text(minutes: i64) -> String {
    if minutes == 0 {
        tr!("Never")
    } else {
        ngettext("{n} minute", "{n} minutes", minutes).replace("{n}", &minutes.to_string())
    }
}

/// Einstellungen des virtuellen Decks mit Standardwerten (wie `virtualdeck.settings`).
pub fn virtual_settings(cfg: &Config) -> Map<String, Value> {
    let mut s = json!({"enabled": false, "mode": "window", "model": VIRTUAL_DEFAULT, "topmost": true, "edge": "right",
                       "shortcut": "", "visible": true, "pos": null}).as_object().cloned().unwrap_or_default();
    if let Some(raw) = cfg.get("virtual_deck").and_then(Value::as_object) {
        for (k, v) in raw {
            if s.contains_key(k) {
                s.insert(k.clone(), v.clone());
            }
        }
    }
    let get = |s: &Map<String, Value>, k: &str| s.get(k).and_then(Value::as_str).unwrap_or("").to_string();
    if !VIRTUAL_SIZES.contains(&get(&s, "model").as_str()) {
        s.insert("model".into(), Value::from(VIRTUAL_DEFAULT));
    }
    if !["window", "edge"].contains(&get(&s, "mode").as_str()) {
        s.insert("mode".into(), Value::from("window"));
    }
    if !["right", "left"].contains(&get(&s, "edge").as_str()) {
        s.insert("edge".into(), Value::from("right"));
    }
    s
}

fn vstr(s: &Map<String, Value>, k: &str) -> String {
    s.get(k).and_then(Value::as_str).unwrap_or("").to_string()
}

fn vbool(s: &Map<String, Value>, k: &str) -> bool {
    s.get(k).and_then(Value::as_bool).unwrap_or(false)
}

fn virtual_status(s: &Map<String, Value>) -> String {
    if !vbool(s, "enabled") {
        tr!("Off")
    } else if vstr(s, "mode") == "window" {
        tr!("On · Window")
    } else {
        tr!("On · Screen edge")
    }
}

/// Angeschlossenes Gerät.
#[derive(Clone)]
struct Dev {
    serial: String,
    model: String,
    firmware: String,
    brightness: u8,
    ctrl: Option<Arc<Mutex<Controller>>>,
    /// Profil, das gerade auf dem Gerät läuft
    profile: Option<String>,
}

/// Name eines Geräts: selbst vergeben oder der Modellname.
fn device_title(cfg: &Config, serial: &str, model: &str) -> String {
    let n = cfg.device(serial).get("name").and_then(Value::as_str).unwrap_or("").trim().to_string();
    if n.is_empty() { model_name(model) } else { n }
}

/// Wert eines Geräts in config.json setzen (None: entfernen).
fn set_device(cfg: &mut Config, serial: &str, key: &str, value: Option<Value>) {
    match value {
        Some(v) => cfg.set_device(serial, key, v),
        None => {
            if let Some(d) = cfg.section("devices").get_mut(serial).and_then(Value::as_object_mut) {
                d.remove(key);
            }
        }
    }
}

struct Page {
    ctx: Rc<Ctx>,
    serial: RefCell<String>,
    listbox: gtk::Box,
    vbox: gtk::Box,
    right: gtk::Box,
    cards: RefCell<HashMap<String, InfoCard>>,
    vcard: RefCell<Option<InfoCard>>,
}

pub fn build(ctx: &Rc<Ctx>) -> gtk::Widget {
    let (page, left, right) = split(240);
    left.append(&section(&tr!("Connected")));
    let listbox = gtk::Box::new(Orientation::Vertical, 6);
    listbox.set_margin_top(8);
    left.append(&listbox);
    let s = section(&tr!("Virtual"));
    s.set_margin_top(16);
    left.append(&s);
    let vbox = gtk::Box::new(Orientation::Vertical, 6);
    vbox.set_margin_top(8);
    left.append(&vbox);
    let serial = ctx.state.borrow().serial.clone().unwrap_or_default();
    let p = Rc::new(Page { ctx: ctx.clone(), serial: RefCell::new(serial), listbox, vbox, right, cards: Default::default(),
                           vcard: Default::default() });
    p.render();
    page.upcast()
}

impl Page {
    fn devices(&self) -> Vec<Dev> {
        let cfg = cfg_read();
        let mut out: Vec<Dev> = if self.ctx.state.borrow().daemon.is_some() {
            self.ctx.controllers().into_iter().map(|c| {
                let g = c.lock().unwrap();
                Dev { serial: g.serial(), model: g.dev.model(), firmware: g.dev.firmware(), brightness: g.brightness,
                      profile: g.profile.as_ref().map(|p| p.id.clone()), ctrl: Some(c.clone()) }
            }).collect()
        } else {
            // der Dienst läuft in einem anderen Prozess: nur die Einstellungen ändern
            freestream_core::models::connected_models().into_iter().map(|c| {
                let d = cfg.device(&c.serial);
                let b = d.get("brightness").and_then(Value::as_u64).unwrap_or(cfg.get_i64("brightness", 70) as u64).min(100) as u8;
                Dev { profile: d.get("profile").and_then(Value::as_str).map(str::to_string), serial: c.serial, model: c.model,
                      firmware: String::new(), brightness: b, ctrl: None }
            }).collect()
        };
        out.sort_by_key(|d| (model_name(&d.model), d.serial.clone()));
        out
    }

    fn render(self: &Rc<Self>) {
        clear(&self.listbox);
        clear(&self.vbox);
        let devs = self.devices();
        let cfg = cfg_read();
        {
            let mut sel = self.serial.borrow_mut();
            if *sel != VIRTUAL_SERIAL && !devs.iter().any(|d| d.serial == *sel) {
                *sel = devs.first().map(|d| d.serial.clone()).unwrap_or_else(|| VIRTUAL_SERIAL.to_string());
            }
        }
        let sel = self.serial.borrow().clone();
        let mut cards = HashMap::new();
        for d in &devs {
            let card = InfoCard::new(&device_title(&cfg, &d.serial, &d.model), &tr!("Connected"), "deck", DEVICE_COLOR);
            card.set_dot(Some("ok"));
            card.set_selected(d.serial == sel);
            let (me, s) = (self.clone(), d.serial.clone());
            card.button.connect_clicked(move |_| me.select(&s));
            self.listbox.append(&card.button);
            cards.insert(d.serial.clone(), card);
        }
        if devs.is_empty() {
            let card = InfoCard::new(&tr!("No device connected"), &tr!("Plug in your deck – its settings appear here."), "deck", "#2c2c2c");
            card.set_dot(None);
            card.button.set_sensitive(false);
            card.sub.set_wrap(true);
            card.sub.set_ellipsize(gtk::pango::EllipsizeMode::None);
            card.sub.set_max_width_chars(22);
            self.listbox.append(&card.button);
        }
        *self.cards.borrow_mut() = cards;
        let vs = virtual_settings(&cfg);
        let v = InfoCard::new(&tr!("Virtual deck"), &virtual_status(&vs), "grid", ACCENT);
        v.set_dot(vbool(&vs, "enabled").then_some("ok"));
        v.set_selected(sel == VIRTUAL_SERIAL);
        let me = self.clone();
        v.button.connect_clicked(move |_| me.select(VIRTUAL_SERIAL));
        self.vbox.append(&v.button);
        *self.vcard.borrow_mut() = Some(v);
        if sel == VIRTUAL_SERIAL {
            self.build_virtual();
        } else if let Some(d) = devs.into_iter().find(|d| d.serial == sel) {
            self.build_form(d);
        }
    }

    fn select(self: &Rc<Self>, serial: &str) {
        *self.serial.borrow_mut() = serial.to_string();
        self.render();
    }

    /// Profile, die zum Gerät passen (sonst alle): (ID, Name).
    fn candidate_profiles(model: &str) -> Vec<(String, String)> {
        let store = ProfileStore::new(None);
        let geo = model_geometry(model).unwrap_or(freestream_core::models::Geometry::new(5, 3));
        let mut c: Vec<(String, String)> = store.for_model(model, geo).iter().map(|p| (p.id.clone(), p.name.clone())).collect();
        if c.is_empty() {
            c = store.profiles.values().map(|p| (p.id.clone(), p.name.clone())).collect();
            c.sort_by_key(|(_, n)| n.to_lowercase());
        }
        c
    }

    // -------------------------------------------------------------------------------- echtes Gerät
    fn build_form(self: &Rc<Self>, d: Dev) {
        let body = panel(&self.right);
        let cfg = cfg_read();
        let dcfg = cfg.device(&d.serial);
        let g = form();
        body.append(&g);
        let serial = d.serial.clone();

        // Name und aktiv
        let row = gtk::Box::new(Orientation::Horizontal, 8);
        let name = gtk::Entry::new();
        name.set_text(&device_title(&cfg, &d.serial, &d.model));
        name.set_width_chars(18);
        row.append(&name);
        let enabled = gtk::Switch::new();
        enabled.set_active(dcfg.get("enabled") != Some(&Value::Bool(false)));
        enabled.set_valign(Align::Center);
        enabled.set_margin_start(8);
        row.append(&enabled);
        row.append(&label(&tr!("Active"), &[]));
        form_row(&g, 0, &tr!("Device name"), &row);
        let (me, s, model, save) = (self.clone(), serial.clone(), d.model.clone(), Debounce::default());
        name.connect_changed(move |e| {
            let n = e.text().trim().to_string();
            let v = (!n.is_empty() && n != model_name(&model)).then(|| Value::from(n.clone()));
            let (me2, s2, model2) = (me.clone(), s.clone(), model.clone());
            save.run(500, move || {
                me2.ctx.edit(|cfg| set_device(cfg, &s2, "name", v));
                if let Some(card) = me2.cards.borrow().get(&s2) {
                    card.title.set_text(&device_title(&cfg_read(), &s2, &model2));
                }
            });
        });
        let (me, s) = (self.clone(), serial.clone());
        enabled.connect_active_notify(move |sw| {
            let on = sw.is_active();
            me.ctx.edit(|cfg| set_device(cfg, &s, "enabled", Some(Value::Bool(on))));
        });

        // Ausrichtung
        let rot = dropdown(&[tr!("Standard"), tr!("Upside down (180°)")]);
        rot.set_selected(u32::from(dcfg.get("rotation").and_then(Value::as_i64) == Some(180)));
        let (me, s) = (self.clone(), serial.clone());
        rot.connect_selected_notify(move |dd| {
            let deg = if dd.selected() == 1 { 180 } else { 0 };
            me.ctx.edit(|cfg| set_device(cfg, &s, "rotation", Some(Value::from(deg))));
        });
        form_row(&g, 1, &tr!("Orientation"), &rot);

        // Ruhezustand
        let cur = dcfg.get("sleep_after_minutes").and_then(Value::as_i64).unwrap_or_else(|| cfg.get_i64("sleep_after_minutes", 0));
        let mut choices: Vec<i64> = SLEEP_CHOICES.to_vec();
        if !choices.contains(&cur) {
            choices.push(cur);
            choices.sort();
        }
        let sl = dropdown(&choices.iter().map(|m| sleep_text(*m)).collect::<Vec<_>>());
        sl.set_selected(choices.iter().position(|m| *m == cur).unwrap_or(0) as u32);
        let (me, s) = (self.clone(), serial.clone());
        sl.connect_selected_notify(move |dd| {
            let m = choices[dd.selected() as usize];
            me.ctx.edit(|cfg| set_device(cfg, &s, "sleep_after_minutes", Some(Value::from(m))));
        });
        form_row(&g, 2, &tr!("Sleep after"), &sl);

        // Helligkeit
        let row = gtk::Box::new(Orientation::Horizontal, 8);
        let sun = gtk::Image::from_icon_name("display-brightness-symbolic");
        sun.add_css_class("muted");
        row.append(&sun);
        let scale = gtk::Scale::with_range(Orientation::Horizontal, 0.0, 100.0, 1.0);
        scale.set_value(d.brightness as f64);
        scale.set_size_request(260, -1);
        scale.set_draw_value(false);
        row.append(&scale);
        let val = label(&format!("{} %", d.brightness), &["muted"]);
        val.set_width_chars(6);
        val.set_xalign(1.0);
        row.append(&val);
        let (me, s, ctrl, save) = (self.clone(), serial.clone(), d.ctrl.clone(), Debounce::default());
        scale.connect_value_changed(move |sc| {
            let v = sc.value().round() as u8;
            val.set_text(&format!("{v} %"));
            if let Some(c) = &ctrl {
                c.lock().unwrap().set_brightness(v);
            }
            let (me2, s2) = (me.clone(), s.clone());
            save.run(500, move || me2.ctx.edit(|cfg| set_device(cfg, &s2, "brightness", Some(Value::from(v)))));
        });
        form_row(&g, 3, &tr!("Brightness"), &row);

        // Bild, solange FreeDeck nicht läuft
        let row = gtk::Box::new(Orientation::Horizontal, 8);
        let preview = gtk::Box::new(Orientation::Horizontal, 0);
        preview.set_margin_end(4);
        row.append(&preview);
        let pick = button(&tr!("Image…"), Some("image-x-generic-symbolic"), "");
        row.append(&pick);
        let remove = button(&tr!("Remove"), None, "ghost");
        row.append(&remove);
        form_row(&g, 4, &tr!("When not running"), &row);
        let show = {
            let (preview, remove, s) = (preview.clone(), remove.clone(), serial.clone());
            Rc::new(move || {
                clear(&preview);
                let path = cfg_read().device(&s).get("idle_image").and_then(Value::as_str).map(std::path::PathBuf::from)
                    .filter(|p| p.is_file());
                remove.set_sensitive(path.is_some());
                let thumb = path.as_ref().and_then(|p| image::open(p).ok())
                    .map(|im| im.resize_to_fill(48, 28, image::imageops::FilterType::Triangle).to_rgba8());
                match (path, thumb) {
                    (_, Some(t)) => {
                        let pic = gtk::Picture::for_paintable(&super::util::texture(&t));
                        pic.set_size_request(48, 28);
                        pic.set_can_shrink(false);
                        preview.append(&pic);
                    }
                    (Some(p), None) => preview.append(&label(&p.file_name().and_then(|n| n.to_str()).unwrap_or("").to_string(), &["muted"])),
                    (None, None) => preview.append(&label(&tr!("Device logo"), &["muted", "small"])),
                }
            })
        };
        show();
        let (me, s, show2) = (self.clone(), serial.clone(), show.clone());
        pick.connect_clicked(move |_| {
            let (me2, s2, show3) = (me.clone(), s.clone(), show2.clone());
            choose_file(&me.ctx.win, &tr!("Image when FreeDeck is not running"),
                        &[(tr!("Images"), &["*.png", "*.jpg", "*.jpeg", "*.webp", "*.gif", "*.bmp"]), (tr!("All files"), &["*"])],
                        move |src| {
                            match set_idle(&s2, Some(&src)) {
                                Ok(()) => me2.ctx.sync(),
                                Err(e) => message(&me2.ctx.win, &tr!("Settings"), &tr!("Image not readable: {error}", error = e)),
                            }
                            show3();
                        });
        });
        let (me, s) = (self.clone(), serial.clone());
        remove.connect_clicked(move |_| {
            let _ = set_idle(&s, None);
            me.ctx.sync();
            show();
        });

        // Angaben zum Gerät
        form_row(&g, 5, &tr!("Model"), &label(&model_name(&d.model), &[]));
        let sl = label(&d.serial, &[]);
        sl.set_selectable(true);
        form_row(&g, 6, &tr!("Serial number"), &sl);
        form_row(&g, 7, &tr!("Firmware"), &label(if d.firmware.is_empty() { "–" } else { &d.firmware }, &[]));

        // Profil
        let cands = Self::candidate_profiles(&d.model);
        let pd = dropdown(&cands.iter().map(|(_, n)| n.clone()).collect::<Vec<_>>());
        let cur = d.profile.clone().or_else(|| dcfg.get("profile").and_then(Value::as_str).map(str::to_string));
        pd.set_selected(cur.and_then(|p| cands.iter().position(|(i, _)| *i == p)).map(|i| i as u32).unwrap_or(gtk::INVALID_LIST_POSITION));
        let (me, s, ctrl) = (self.clone(), serial.clone(), d.ctrl.clone());
        pd.connect_selected_notify(move |dd| {
            let Some((pid, _)) = cands.get(dd.selected() as usize).cloned() else { return };
            me.ctx.edit(|cfg| set_device(cfg, &s, "profile", Some(Value::from(pid.clone()))));
            if let Some(c) = ctrl.clone() {
                std::thread::spawn(move || c.lock().unwrap().load_profile(Some(&pid), None));
            }
        });
        form_row(&g, 8, &tr!("Profile"), &pd);
    }

    // -------------------------------------------------------------------------------- virtuelles Deck
    fn build_virtual(self: &Rc<Self>) {
        let body = panel(&self.right);
        let s = virtual_settings(&cfg_read());
        let g = form();
        body.append(&g);
        let on_row = gtk::Box::new(Orientation::Horizontal, 10);
        let on = gtk::Switch::new();
        on.set_active(vbool(&s, "enabled"));
        on.set_valign(Align::Center);
        on_row.append(&on);
        on_row.append(&label(&tr!("On – usable without a connected device"), &[]));
        form_row(&g, 0, &tr!("Virtual deck"), &on_row);

        // Darstellung: zwei Karten zur Wahl
        let modes = gtk::Box::new(Orientation::Horizontal, 8);
        let mut mode_btns: Vec<(&'static str, gtk::ToggleButton)> = Vec::new();
        for (mode, title, sub) in [("window", tr!("Floating window"), tr!("move it anywhere, stays on top")),
                                   ("edge", tr!("Edge bar"), tr!("click its grip to slide it away and back"))] {
            let t = gtk::ToggleButton::new();
            t.add_css_class("mode-card");
            let col = gtk::Box::new(Orientation::Vertical, 4);
            col.append(&mode_sketch(mode));
            col.append(&label(&title, &["info-title"]));
            let sl = label(&sub, &["muted", "small"]);
            sl.set_wrap(true);
            sl.set_max_width_chars(18);
            col.append(&sl);
            t.set_child(Some(&col));
            t.set_size_request(170, -1);
            if let Some((_, f)) = mode_btns.first() {
                t.set_group(Some(f));
            }
            modes.append(&t);
            mode_btns.push((mode, t));
        }
        let l = form_row(&g, 1, &tr!("Display"), &modes);
        l.set_valign(Align::Start);
        l.set_margin_top(12);

        let size = dropdown(&[]);
        form_row(&g, 2, &tr!("Size"), &size);
        let top = gtk::Switch::new();
        top.set_active(vbool(&s, "topmost"));
        form_row(&g, 3, &tr!("Always on top"), &top);
        let edge = dropdown(&[tr!("Right"), tr!("Left")]);
        edge.set_selected(u32::from(vstr(&s, "edge") == "left"));
        form_row(&g, 4, &tr!("Screen edge"), &edge);
        form_hint(&g, 5, &tr!("only for the edge bar"));
        let profs = {
            let store = ProfileStore::new(None);
            let mut v: Vec<(String, String)> = store.profiles.values().map(|p| (p.id.clone(), p.name.clone())).collect();
            v.sort_by_key(|(_, n)| n.to_lowercase());
            v
        };
        let prof = dropdown(&profs.iter().map(|(_, n)| n.clone()).collect::<Vec<_>>());
        let cur = cfg_read().device(VIRTUAL_SERIAL).get("profile").and_then(Value::as_str).map(str::to_string);
        prof.set_selected(cur.and_then(|c| profs.iter().position(|(i, _)| *i == c)).map(|i| i as u32).unwrap_or(gtk::INVALID_LIST_POSITION));
        form_row(&g, 6, &tr!("Profile"), &prof);
        let h = super::settings::hint(&tr!("You assign the keys like on a real deck: choose “Virtual deck” on the left of the FreeDeck window and drag actions onto it."));
        h.set_margin_top(10);
        h.set_max_width_chars(56);
        g.attach(&h, 1, 7, 1, 1);

        // Felder ein-/ausgrauen, Größen je nach Darstellung (Randleiste hochkant)
        let busy = Rc::new(std::cell::Cell::new(false));
        let refresh = {
            let (size, top, edge, me, busy) = (size.clone(), top.clone(), edge.clone(), self.clone(), busy.clone());
            let mode_btns = mode_btns.clone();
            Rc::new(move || {
                busy.set(true);
                let s = virtual_settings(&cfg_read());
                let on = vbool(&s, "enabled");
                let mode = vstr(&s, "mode");
                for (m, b) in &mode_btns {
                    b.set_active(*m == mode);
                    b.set_sensitive(on);
                }
                let names: Vec<String> = VIRTUAL_SIZES.iter().map(|c| {
                    let code = if mode == "edge" { upright(c) } else { c };
                    let g = VIRTUAL_MODELS.iter().find(|(m, _)| *m == code).map(|(_, g)| *g).unwrap_or(freestream_core::models::Geometry::new(4, 2));
                    tr!("{cols} × {rows} keys", cols = g.cols, rows = g.rows)
                }).collect();
                size.set_model(Some(&gtk::StringList::new(&names.iter().map(String::as_str).collect::<Vec<_>>())));
                size.set_selected(VIRTUAL_SIZES.iter().position(|c| *c == vstr(&s, "model")).unwrap_or(1) as u32);
                size.set_sensitive(on);
                top.set_sensitive(on && mode == "window");
                edge.set_sensitive(on && mode == "edge");
                if let Some(v) = me.vcard.borrow().as_ref() {
                    v.sub.set_text(&virtual_status(&s));
                    v.set_dot(on.then_some("ok"));
                }
                busy.set(false);
            })
        };
        refresh();
        let set = {
            let (me, refresh, busy) = (self.clone(), refresh.clone(), busy.clone());
            Rc::new(move |key: &str, v: Value| {
                if busy.get() {
                    return;
                }
                me.ctx.edit(|cfg| {
                    let mut s = virtual_settings(cfg);
                    s.insert(key.to_string(), v);
                    cfg.set("virtual_deck", Value::Object(s));
                });
                refresh();
            })
        };
        let set2 = set.clone();
        on.connect_active_notify(move |sw| {
            set2("visible", Value::Bool(true));
            set2("enabled", Value::Bool(sw.is_active()));
        });
        for (m, b) in &mode_btns {
            let (set2, m) = (set.clone(), *m);
            b.connect_toggled(move |b| {
                if b.is_active() {
                    set2("mode", Value::from(m));
                }
            });
        }
        let set2 = set.clone();
        size.connect_selected_notify(move |dd| {
            if let Some(code) = VIRTUAL_SIZES.get(dd.selected() as usize) {
                set2("model", Value::from(*code));
            }
        });
        let set2 = set.clone();
        top.connect_active_notify(move |sw| set2("topmost", Value::Bool(sw.is_active())));
        let set2 = set.clone();
        edge.connect_selected_notify(move |dd| set2("edge", Value::from(if dd.selected() == 1 { "left" } else { "right" })));
        let me = self.clone();
        prof.connect_selected_notify(move |dd| {
            if let Some((pid, _)) = profs.get(dd.selected() as usize) {
                me.ctx.edit(|cfg| cfg.set_device(VIRTUAL_SERIAL, "profile", Value::from(pid.clone())));
            }
        });
    }
}

/// Kleine Skizze der Darstellung: schwebendes Fenster bzw. Leiste am Rand.
fn mode_sketch(mode: &'static str) -> gtk::DrawingArea {
    let da = gtk::DrawingArea::new();
    da.set_content_width(140);
    da.set_content_height(64);
    da.set_draw_func(move |_, cr, w, h| {
        let (w, h) = (w as f64, h as f64);
        cr.set_source_rgb(0.12, 0.12, 0.12);
        super::util::rounded(cr, 0.0, 0.0, w, h, 6.0);
        let _ = cr.fill();
        let (kx, ky, cols, rows, bx, by) = if mode == "window" {
            (14.0, 10.0, 4, 2, w / 2.0 - 34.0, h / 2.0 - 18.0)
        } else {
            (12.0, 12.0, 2, 4, w - 34.0, 4.0)
        };
        cr.set_source_rgb(0.24, 0.24, 0.24);
        let (bw, bh) = (cols as f64 * (kx + 3.0) + 5.0, rows as f64 * (ky + 3.0) + 5.0);
        super::util::rounded(cr, bx, by, bw, bh.min(h - 8.0), 4.0);
        let _ = cr.fill();
        cr.set_source_rgb(0.24, 0.61, 1.0);
        for r in 0..rows {
            for c in 0..cols {
                let (x, y) = (bx + 4.0 + c as f64 * (kx + 3.0), by + 4.0 + r as f64 * (ky + 3.0));
                if y + ky < h - 2.0 {
                    super::util::rounded(cr, x, y, kx, ky * if mode == "window" { 1.0 } else { 0.8 }, 2.0);
                    let _ = cr.fill();
                }
            }
        }
    });
    da
}

/// Bild für „FreeDeck läuft nicht“ in den Konfigurationsordner kopieren (die Quelle darf danach verschwinden).
fn set_idle(serial: &str, src: Option<&std::path::Path>) -> Result<(), String> {
    let dst = paths::config_dir().join("idle").join(format!("{serial}.png"));
    match src {
        None => {
            let _ = std::fs::remove_file(&dst);
            cfg_edit(|cfg| set_device(cfg, serial, "idle_image", None));
        }
        Some(src) => {
            let im = image::open(src).map_err(|e| e.to_string())?;
            let im = if im.width() > 1600 || im.height() > 1600 { im.thumbnail(1600, 1600) } else { im };
            if let Some(p) = dst.parent() {
                std::fs::create_dir_all(p).map_err(|e| e.to_string())?;
            }
            im.to_rgb8().save(&dst).map_err(|e| e.to_string())?;
            cfg_edit(|cfg| set_device(cfg, serial, "idle_image", Some(Value::from(dst.to_string_lossy().to_string()))));
        }
    }
    Ok(())
}
