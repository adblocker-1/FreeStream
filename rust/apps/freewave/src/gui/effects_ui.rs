//! Effektkette eines Kanals bearbeiten (wavedialogs.EffectsDialog) und Effekte hinzufügen
//! (wavedialogs.PluginPicker) – wie die Plugins in Wave Link: hinzufügen, an/aus, Reihenfolge, Regler.

use std::cell::RefCell;
use std::rc::Rc;

use gtk::prelude::*;
use gtk::Align;
use gtk4 as gtk;

use freestream_core::tr;
use freestream_wave::effects::{self, Control, Effect, Plugin};
use freestream_wave::mixer::truthy;
use freestream_wave::Channel;
use serde_json::{Map, Value};

use super::theme;
use super::widgets::{self, hbox, label, vbox, Fader, Switch};
use super::{bg, Win};

/// Effektkette eines Kanals aus dem Abschnitt "mixer" der Einstellungen.
pub fn chain(conf: &Map<String, Value>, cid: &str) -> Vec<Effect> {
    conf.get("effects").and_then(|e| e.get(cid)).and_then(Value::as_array)
        .map(|a| a.iter().filter_map(Effect::from_value).collect()).unwrap_or_default()
}

/// Namen der eingeschalteten Effekte (beim Mikrofon die Rauschunterdrückung zuerst).
pub fn fx_names(conf: &Map<String, Value>, cid: &str) -> Vec<String> {
    let mut names: Vec<String> = chain(conf, cid).into_iter().filter(|f| f.enabled)
        .map(|f| if f.name.is_empty() { "FX".to_string() } else { f.name }).collect();
    let noise_on = conf.get("noise").and_then(|n| n.get("enabled")).map(truthy).unwrap_or(false);
    if cid == "mic" && noise_on {
        names.insert(0, tr!("Noise suppression"));
    }
    names
}

/// Wert eines Reglers als Text (wavedialogs._fmt).
pub fn fmt_value(v: f64, c: &Control) -> String {
    if c.toggle {
        return if v >= 0.5 { tr!("on") } else { tr!("off") };
    }
    let unit = c.unit.as_ref().filter(|u| !u.is_empty()).map(|u| format!(" {u}")).unwrap_or_default();
    if c.int || v.abs() >= 100.0 {
        format!("{v:.0}{unit}")
    } else if v.abs() < 10.0 {
        format!("{v:.2}{unit}")
    } else {
        format!("{v:.1}{unit}")
    }
}

/// Reglerstellung 0 … 100 ↔ Wert (logarithmisch bei Frequenzen u. Ä.).
pub fn to_pos(v: f64, c: &Control) -> f64 {
    let (lo, hi) = (c.min, c.max);
    let v = v.clamp(lo.min(hi), hi.max(lo));
    if c.log && lo > 0.0 && hi > 0.0 {
        (v / lo).ln() / (hi / lo).ln() * 100.0
    } else {
        (v - lo) / if hi - lo == 0.0 { 1.0 } else { hi - lo } * 100.0
    }
}

pub fn from_pos(t: f64, c: &Control) -> f64 {
    let (lo, hi) = (c.min, c.max);
    let v = if c.log && lo > 0.0 && hi > 0.0 { lo * (hi / lo).powf(t / 100.0) } else { lo + (hi - lo) * t / 100.0 };
    if c.int {
        v.round()
    } else {
        v
    }
}

/// Effekte eines Kanals öffnen (Katalog wird im Hintergrund gelesen).
pub fn open(w: &Rc<Win>, ch: &Channel) {
    w.status(&tr!("Loading effects …"));
    let dir = w.ctl.host().mixer.state_dir().to_path_buf();
    let (w2, ch) = (w.clone(), ch.clone());
    bg(move || effects::catalog(&dir, false), move |cat| {
        w2.status("");
        EffectsDialog::open(&w2, ch, cat);
    });
}

struct EffectsDialog {
    w: Rc<Win>,
    channel: Channel,
    chain: RefCell<Vec<Effect>>,
    catalog: Vec<Plugin>,
    list: gtk::Box,
    win: gtk::Window,
}

impl EffectsDialog {
    fn open(w: &Rc<Win>, channel: Channel, catalog: Vec<Plugin>) {
        let conf = w.ctl.host().mixer.conf();
        let ch = chain(&conf, &channel.id);
        let d = widgets::dialog(w.win.upcast_ref(), &tr!("Effects – {name}", name = channel.name), 600, 640);
        let color = if channel.color.is_empty() { theme::channel_look(&channel).1 } else { channel.color.clone() };
        widgets::header(&d.body, &tr!("Effects for “{name}”", name = channel.name),
                        &tr!("Audio flows through the effects from top to bottom. Just like the plugins in Wave Link."),
                        Some(("fx", &color)), 60);
        let list = vbox(10);
        d.body.append(&widgets::scroll(&list));
        let add = widgets::button(&tr!("Add effect"), Some("plus"), "");
        d.left.append(&add);
        let done = widgets::button(&tr!("Done"), None, "primary");
        d.buttons.append(&done);
        let win = d.win.clone();
        done.connect_clicked(move |_| win.close());
        let me = Rc::new(Self { w: w.clone(), channel, chain: RefCell::new(ch), catalog, list, win: d.win.clone() });
        let m = me.clone();
        add.connect_clicked(move |_| {
            let m2 = m.clone();
            plugin_picker(&m.win, &m.catalog, move |p| {
                m2.chain.borrow_mut().push(effects::new_effect(&p));
                m2.changed();
                m2.render();
            });
        });
        me.render();
        d.win.present();
    }

    fn lookup(&self, id: &str) -> Option<Plugin> {
        effects::lookup(id, Some(&self.catalog), self.w.ctl.host().mixer.state_dir())
    }

    fn render(self: &Rc<Self>) {
        widgets::clear(&self.list);
        let chain = self.chain.borrow().clone();
        if chain.is_empty() {
            let card = hbox(14);
            card.add_css_class("card");
            card.add_css_class("big");
            let t = theme::tile("fx", 40, theme::RAISED, 0.28, theme::MUTED);
            t.set_valign(Align::Start);
            card.append(&t);
            let txt = vbox(4);
            txt.append(&label(&tr!("No effects yet"), &["bold"]));
            txt.append(&widgets::wrap(&tr!("Use “Add effect” to put e.g. noise suppression, a noise gate, a compressor or an equalizer on this channel."), &["muted", "small"], 50));
            card.append(&txt);
            self.list.append(&card);
            return;
        }
        let n = chain.len();
        for (i, fx) in chain.iter().enumerate() {
            self.card(i, n, fx);
        }
    }

    fn card(self: &Rc<Self>, i: usize, n: usize, fx: &Effect) {
        let p = self.lookup(&fx.plugin);
        let on = fx.enabled;
        let card = vbox(0);
        card.add_css_class("card");
        card.add_css_class("big");
        let top = hbox(12);
        let ok = on && p.is_some();
        let t = theme::tile("fx", 34, if ok { theme::ACCENT } else { theme::RAISED }, 0.28, if ok { "#ffffff" } else { theme::MUTED });
        t.set_valign(Align::Start);
        top.append(&t);
        let names = vbox(2);
        names.set_hexpand(true);
        let name = if !fx.name.is_empty() { fx.name.clone() } else { p.as_ref().map(|p| p.name.clone()).unwrap_or_else(|| fx.plugin.clone()) };
        names.append(&label(&name, if on { &["bold"] } else { &["bold", "muted"] }));
        let info = match &p {
            Some(p) => widgets::wrap(&p.info, &["muted", "small"], 34),
            None => widgets::wrap(&tr!("Plugin not found – is it installed?"), &["danger", "small"], 34),
        };
        names.append(&info);
        top.append(&names);
        for (icon, ok, d) in [("up", i > 0, -1i32), ("down", i + 1 < n, 1), ("trash", true, 0)] {
            let b = widgets::button("", Some(icon), "ghost");
            b.set_sensitive(ok);
            let me = self.clone();
            b.connect_clicked(move |_| {
                {
                    let mut c = me.chain.borrow_mut();
                    if d == 0 {
                        c.remove(i);
                    } else {
                        let j = (i as i32 + d) as usize;
                        c.swap(i, j);
                    }
                }
                me.changed();
                me.render();
            });
            top.append(&b);
        }
        let sw = Switch::new();
        sw.set(on);
        sw.area.set_margin_start(8);
        let me = self.clone();
        sw.on_toggle(move |v| {
            me.chain.borrow_mut()[i].enabled = v;
            me.changed();
            me.render();
        });
        top.append(&sw.area);
        card.append(&top);
        if let Some(p) = p.filter(|p| !p.controls.is_empty()) {
            let line = widgets::sep();
            widgets::margins(&line, 14, 0, 10, 0);
            card.append(&line);
            let grid = gtk::Grid::new();
            grid.set_row_spacing(3);
            grid.set_column_spacing(8);
            for (r, c) in p.controls.iter().enumerate() {
                let value = fx.controls.get(&c.name).copied().unwrap_or(c.default);
                let l = label(c.label.as_deref().unwrap_or(&c.name), &["muted", "small"]);
                l.set_width_chars(20);
                l.set_max_width_chars(20);
                l.set_ellipsize(gtk::pango::EllipsizeMode::End);
                grid.attach(&l, 0, r as i32, 1, 1);
                let val = label(&fmt_value(value, c), &["small"]);
                val.set_width_chars(10);
                val.set_xalign(1.0);
                val.set_hexpand(true);
                if c.toggle {
                    let s = Switch::new();
                    s.set(value >= 0.5);
                    s.area.set_halign(Align::Start);
                    let (me, c2, v2) = (self.clone(), c.clone(), val.clone());
                    s.on_toggle(move |on| me.control(i, &c2, if on { 1.0 } else { 0.0 }, &v2));
                    grid.attach(&s.area, 1, r as i32, 1, 1);
                } else {
                    let f = Fader::new(230, 26, 0.0, 100.0);
                    f.set(to_pos(value, c).clamp(0.0, 100.0));
                    let (me, c2, v2) = (self.clone(), c.clone(), val.clone());
                    f.on_change(move |t| me.control(i, &c2, from_pos(t, &c2), &v2));
                    grid.attach(&f.area, 1, r as i32, 1, 1);
                }
                grid.attach(&val, 2, r as i32, 1, 1);
            }
            card.append(&grid);
        }
        self.list.append(&card);
    }

    fn control(self: &Rc<Self>, i: usize, c: &Control, value: f64, val: &gtk::Label) {
        if let Some(fx) = self.chain.borrow_mut().get_mut(i) {
            fx.controls.insert(c.name.clone(), value);
        }
        val.set_text(&fmt_value(value, c));
        // Regler bewegt: live setzen (pw-cli), sonst Kette neu starten
        let (chain, cid, name) = (self.chain.borrow().clone(), self.channel.id.clone(), c.name.clone());
        self.w.debounced(&format!("fxc:{cid}"), 300, move |h| {
            h.mixer.fx().set_chain(&cid, &chain);
            if h.mixer.is_active() && !h.mixer.fx().set_live(&cid, i, &name, value) {
                h.mixer.rebuild_channel(&cid)?;
            }
            Ok(())
        });
    }

    /// Kette geändert (hinzugefügt, entfernt, an/aus, verschoben): speichern und Kanal neu verbinden.
    fn changed(&self) {
        let (chain, cid) = (self.chain.borrow().clone(), self.channel.id.clone());
        self.w.debounced(&format!("fx:{cid}"), 350, move |h| {
            h.mixer.fx().set_chain(&cid, &chain);
            if h.mixer.is_active() {
                h.mixer.rebuild_channel(&cid)?;
            }
            Ok(())
        });
    }
}

/// Liste aller Effekte mit Suche – wie der Plugin-Browser in Wave Link.
fn plugin_picker(parent: &gtk::Window, catalog: &[Plugin], on_pick: impl Fn(Plugin) + 'static) {
    let count = |t: &str| catalog.iter().filter(|p| p.kind == t).count();
    let (lv2_ok, lv2_hint) = effects::lv2_status();
    let mut info = tr!("Found {builtin} built-in filters, {ladspa} LADSPA and {lv2} LV2 plugins. More plugins: packages swh-plugins, tap-plugins, x42-plugins, lsp-plugins-lv2, calf-plugins.",
                       builtin = count("builtin"), ladspa = count("ladspa"), lv2 = count("lv2"));
    if count("lv2") == 0 && freestream_wave::pactl::which("lv2info").is_none() {
        info += &format!(" {}", tr!("For LV2 plugins, install the package lilv-utils."));
    } else if !lv2_ok {
        info += &format!(" {lv2_hint}.");
    }
    let d = widgets::dialog(parent, &tr!("Add effect"), 580, 640);
    widgets::header(&d.body, &tr!("Add effect"), &info, Some(("fx", theme::ACCENT)), 60);
    let search = gtk::SearchEntry::new();
    search.set_margin_bottom(10);
    d.body.append(&search);
    let chips = hbox(6);
    chips.set_margin_bottom(12);
    let kind = Rc::new(RefCell::new("all".to_string()));
    let mut chip_btns = Vec::new();
    for (key, text) in [("all", tr!("All")), ("builtin", tr!("Built-in")), ("ladspa", "LADSPA".into()), ("lv2", "LV2".into())] {
        let c = widgets::chip(&text, key == "all");
        chips.append(&c);
        chip_btns.push((key, c));
    }
    chips.append(&widgets::spacer());
    let count_l = label("", &["faint", "small"]);
    chips.append(&count_l);
    d.body.append(&chips);
    let list = vbox(6);
    d.body.append(&widgets::scroll(&list));
    let close = widgets::button(&tr!("Close"), None, "");
    d.buttons.append(&close);
    let win = d.win.clone();
    close.connect_clicked(move |_| win.close());
    let catalog: Rc<Vec<Plugin>> = Rc::new(catalog.to_vec());
    let on_pick = Rc::new(on_pick);
    let fill: Rc<dyn Fn()> = {
        let (list, search, kind, count_l, catalog, win, on_pick, lv2_hint) =
            (list.clone(), search.clone(), kind.clone(), count_l.clone(), catalog.clone(), d.win.clone(), on_pick.clone(), lv2_hint.clone());
        Rc::new(move || {
            widgets::clear(&list);
            let q = search.text().trim().to_lowercase();
            let k = kind.borrow().clone();
            let mut shown = 0;
            for p in catalog.iter() {
                if k != "all" && p.kind != k {
                    continue;
                }
                if !q.is_empty() && !p.name.to_lowercase().contains(&q) && !p.info.to_lowercase().contains(&q) && !p.label.to_lowercase().contains(&q) {
                    continue;
                }
                shown += 1;
                if shown > 150 {
                    list.append(&label(&tr!("… more – narrow down your search"), &["muted", "small"]));
                    break;
                }
                let ok = p.kind != "lv2" || lv2_ok;
                let tag = match p.kind.as_str() { "builtin" => "PipeWire", "ladspa" => "LADSPA", "lv2" => "LV2", o => o };
                let tint = match p.kind.as_str() { "builtin" => theme::ACCENT, "ladspa" => theme::STREAM, "lv2" => theme::MONITOR, _ => theme::RAISED };
                let row = hbox(12);
                row.add_css_class("card");
                row.add_css_class("r12");
                row.append(&theme::tile("fx", 32, if ok { tint } else { theme::RAISED }, 0.28, if ok { "#ffffff" } else { theme::MUTED }));
                let txt = vbox(2);
                txt.set_hexpand(true);
                txt.append(&label(&p.name, &["bold"]));
                let sub = if ok { format!("{tag} · {}", if p.info.is_empty() { &p.label } else { &p.info }) } else { format!("{tag} · {lv2_hint}") };
                txt.append(&widgets::wrap(&sub, &["small", if ok { "muted" } else { "warn" }], 44));
                row.append(&txt);
                if ok {
                    let b = widgets::button(&tr!("Add"), Some("plus"), "");
                    let (w, f, p2) = (win.clone(), on_pick.clone(), p.clone());
                    b.connect_clicked(move |_| {
                        w.close();
                        f(p2.clone());
                    });
                    row.append(&b);
                    let g = gtk::GestureClick::new();
                    let (w, f, p2) = (win.clone(), on_pick.clone(), p.clone());
                    g.connect_pressed(move |_, n, _, _| {
                        if n == 2 {
                            w.close();
                            f(p2.clone());
                        }
                    });
                    row.add_controller(g);
                }
                list.append(&row);
            }
            count_l.set_text(&if shown > 0 { shown.min(150).to_string() } else { String::new() });
            if shown == 0 {
                list.append(&label(&tr!("No effect matches your search."), &["muted"]));
            }
        })
    };
    for (key, c) in &chip_btns {
        let (kind, fill, all, key) = (kind.clone(), fill.clone(), chip_btns.clone(), key.to_string());
        c.connect_clicked(move |_| {
            *kind.borrow_mut() = key.clone();
            for (k, b) in &all {
                widgets::set_class(b, "active", *k == key);
            }
            fill();
        });
    }
    let f = fill.clone();
    search.connect_search_changed(move |_| f());
    fill();
    d.win.present();
    search.grab_focus();
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn ctl(min: f64, max: f64, log: bool) -> Control {
        Control { name: "x".into(), min, max, log, ..Default::default() }
    }

    #[test]
    fn values_and_positions() {
        let c = Control { unit: Some("Hz".into()), ..ctl(20.0, 20000.0, true) };
        assert_eq!(fmt_value(90.0, &c), "90.0 Hz");
        assert_eq!(fmt_value(250.0, &c), "250 Hz");
        assert_eq!(fmt_value(0.71, &ctl(0.0, 1.0, false)), "0.71");
        assert!((from_pos(to_pos(90.0, &c), &c) - 90.0).abs() < 1e-6);
        assert!((to_pos(5.0, &ctl(0.0, 10.0, false)) - 50.0).abs() < 1e-9);
        let i = Control { int: true, ..ctl(0.0, 10.0, false) };
        assert_eq!(from_pos(33.0, &i), 3.0);
    }

    #[test]
    fn names_from_config() {
        let conf = json!({"effects": {"mic": [{"plugin": "builtin:highpass", "name": "Low Cut", "enabled": true},
                                              {"plugin": "x", "name": "", "enabled": true},
                                              {"plugin": "y", "name": "Off", "enabled": false}]},
                          "noise": {"enabled": true}});
        let conf = conf.as_object().unwrap();
        assert_eq!(chain(conf, "mic").len(), 3);
        assert_eq!(fx_names(conf, "music"), Vec::<String>::new());
        let names = fx_names(conf, "mic");
        assert_eq!(names.len(), 3);
        assert_eq!(&names[1..], ["Low Cut".to_string(), "FX".to_string()]);
    }
}
