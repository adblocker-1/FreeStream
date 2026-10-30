//! „Export to FreeDeck“ (wavedialogs.DeckExportDialog): Profil „FreeWave“ mit Lautstärkereglern für die
//! gewählten Kanäle auf einem Deck-Modell anlegen – es wird von selbst aktuell gehalten.

use std::cell::{Cell, RefCell};
use std::path::PathBuf;
use std::rc::Rc;

use gtk::prelude::*;
use gtk::Align;
use gtk4 as gtk;

use freestream_core::tr;

use super::dialogs::check_row;
use super::widgets::{self, hbox, label, Switch};
use super::Win;
use crate::deckexport::{self as dx, Background, DeviceChoice, ExportOptions};

struct State {
    devices: Vec<DeviceChoice>,
    sel: Cell<usize>,
    channels: Vec<(String, String)>,
    chosen: RefCell<Vec<bool>>,
    mix: RefCell<String>,
    background: RefCell<Background>,
    shown_bg: RefCell<Option<PathBuf>>,
    transparent: Switch,
    bg_label: gtk::Label,
    bg_remove: gtk::Button,
    hint: gtk::Label,
    result: gtk::Label,
    create: gtk::Button,
    ok: Cell<bool>,
}

impl State {
    fn device(&self) -> &DeviceChoice {
        &self.devices[self.sel.get().min(self.devices.len() - 1)]
    }

    fn selected(&self) -> Vec<String> {
        self.channels.iter().zip(self.chosen.borrow().iter()).filter(|(_, on)| **on).map(|((id, _), _)| id.clone()).collect()
    }

    /// Aussehen des schon vorhandenen Profils dieses Geräts übernehmen.
    fn load_look(&self) {
        let lk = dx::look(&self.device().model, None);
        self.transparent.set(lk.opacity < 100);
        *self.background.borrow_mut() = Background::Keep;
        *self.shown_bg.borrow_mut() = lk.background;
        self.show_background();
    }

    fn show_background(&self) {
        let bg = self.shown_bg.borrow().clone();
        self.bg_label.set_text(&bg.as_ref().and_then(|p| p.file_name()).map(|n| n.to_string_lossy().into_owned()).unwrap_or_else(|| tr!("none")));
        self.bg_remove.set_sensitive(bg.is_some());
    }

    fn update_hint(&self) {
        let d = self.device();
        let r = dx::geometry_of(&d.model).and_then(|g| {
            let mut text = dx::describe(g);
            let n = dx::plan(g, &self.selected(), *self.mix.borrow() == "selected", &dx::linewave_mixes())?.len();
            if n > 1 {
                text += &format!(" {}", tr!("The channels need {count} pages; keys to turn the page are added.", count = n));
            }
            if !d.profile.is_empty() {
                text += &format!(" {}", tr!("The existing profile “{name}” is replaced.", name = d.profile));
            }
            Ok(text)
        });
        let ok = r.is_ok();
        self.hint.set_text(&r.unwrap_or_else(|e| e.0));
        widgets::set_class(&self.hint, "danger", !ok);
        widgets::set_class(&self.hint, "muted", ok);
        widgets::set_button_text(&self.create, &if d.profile.is_empty() { tr!("Create profile") } else { tr!("Update profile") });
        self.create.set_sensitive(ok);
        self.ok.set(ok);
    }
}

pub fn open(w: &Rc<Win>) {
    let s = w.snap.borrow().clone();
    let channels: Vec<(String, String)> = s.channels.iter().map(|c| (c.channel.id.clone(), if c.channel.name.is_empty() { c.channel.id.clone() } else { c.channel.name.clone() })).collect();
    let devices = dx::devices(None, None);
    if devices.is_empty() {
        return;
    }
    let d = widgets::dialog(w.win.upcast_ref(), &tr!("Export to FreeDeck"), 560, -1);
    widgets::header(&d.body, &tr!("Export to FreeDeck"),
                    &tr!("Creates a FreeDeck profile “FreeWave” with volume controls for the chosen channels. It is updated by itself when you add or delete channels."),
                    Some(("export", super::theme::ACCENT)), 50);
    let create = widgets::button(&tr!("Create profile"), Some("plus"), "primary");
    let cancel = widgets::button(&tr!("Cancel"), None, "");
    d.buttons.append(&cancel);
    d.buttons.append(&create);
    let win = d.win.clone();
    cancel.connect_clicked(move |_| win.close());
    let st = Rc::new(State {
        devices: devices.clone(), sel: Cell::new(0), channels: channels.clone(), chosen: RefCell::new(vec![true; channels.len()]),
        mix: RefCell::new("selected".into()), background: RefCell::new(Background::Keep), shown_bg: RefCell::new(None),
        transparent: Switch::new(), bg_label: label("", &["muted", "small"]), bg_remove: widgets::button(&tr!("Remove"), None, "ghost"),
        hint: widgets::wrap("", &["muted", "small"], 60), result: widgets::wrap("", &["muted", "small"], 60), create: create.clone(),
        ok: Cell::new(false),
    });
    let plugged: Vec<usize> = devices.iter().enumerate().filter(|(_, d)| d.connected).map(|(i, _)| i).collect();
    if !plugged.is_empty() {
        // angeschlossene Decks: als Karten, das erste ist gewählt
        widgets::field(&d.body, &tr!("DEVICE – DETECTED AUTOMATICALLY"));
        let row = hbox(6);
        row.set_homogeneous(true);
        row.set_margin_bottom(if plugged.len() > 1 { 6 } else { 14 });
        st.sel.set(plugged[0]);
        let cards: Rc<RefCell<Vec<(usize, gtk::Button)>>> = Rc::new(RefCell::new(Vec::new()));
        for &i in plugged.iter().take(3) {
            let dev = &devices[i];
            let mut sub = tr!("connected");
            if !dev.profile.is_empty() {
                sub += &format!(" · {}", tr!("profile exists"));
            }
            let (st2, cards2) = (st.clone(), cards.clone());
            let card = check_row(&dev.name, &sub, "deck", i == plugged[0], true, move |_| {
                st2.sel.set(i);
                for (j, c) in cards2.borrow().iter() {
                    widgets::set_class(c, "selected", *j == i);
                }
                st2.load_look();
                st2.update_hint();
            });
            row.append(&card);
            cards.borrow_mut().push((i, card));
        }
        d.body.append(&row);
        if plugged.len() > 1 {
            let l = label(&tr!("Several are connected: click the one the profile is for."), &["muted", "small"]);
            l.set_margin_bottom(14);
            d.body.append(&l);
        }
    } else {
        // nichts angeschlossen: Modell wählen
        widgets::field(&d.body, &tr!("DEVICE"));
        let l = label(&tr!("No device connected – choose the model:"), &["muted", "small"]);
        l.set_margin_bottom(6);
        d.body.append(&l);
        let names: Vec<String> = devices.iter().map(|d| if d.in_use { format!("{} · {}", d.name, tr!("in use")) } else { d.name.clone() }).collect();
        let dd = gtk::DropDown::from_strings(&names.iter().map(String::as_str).collect::<Vec<_>>());
        dd.set_margin_bottom(14);
        let st2 = st.clone();
        dd.connect_selected_notify(move |d| {
            st2.sel.set(d.selected() as usize);
            st2.load_look();
            st2.update_hint();
        });
        d.body.append(&dd);
    }
    widgets::field(&d.body, &tr!("CHANNELS"));
    let grid = gtk::Grid::new();
    grid.set_column_spacing(6);
    grid.set_row_spacing(6);
    grid.set_margin_bottom(14);
    for (i, (_, name)) in channels.iter().enumerate() {
        let chip = widgets::chip(name, true);
        chip.set_halign(Align::Start);
        let st2 = st.clone();
        chip.connect_clicked(move |c| {
            let on = {
                let mut ch = st2.chosen.borrow_mut();
                ch[i] = !ch[i];
                ch[i]
            };
            widgets::set_class(c, "active", on);
            st2.update_hint();
        });
        grid.attach(&chip, (i % 4) as i32, (i / 4) as i32, 1, 1);
    }
    d.body.append(&grid);
    widgets::field(&d.body, "MIX");
    let st2 = st.clone();
    let (seg, _) = widgets::segmented(&[("selected", tr!("Choose on deck"), Some("deck")), ("both", tr!("All mixes"), None),
                                        ("monitor", tr!("Personal"), Some("headphones")), ("stream", "Stream".into(), Some("stream"))],
                                      "selected", move |v| {
        *st2.mix.borrow_mut() = v;
        st2.update_hint();
    });
    seg.set_margin_bottom(14);
    d.body.append(&seg);
    // Aussehen: durchsichtige Tasten über einem Hintergrundbild (bleibt beim nächsten Export erhalten)
    widgets::field(&d.body, &tr!("LOOK"));
    let r = hbox(10);
    r.set_margin_bottom(8);
    st.transparent.area.set_halign(Align::Start);
    r.append(&st.transparent.area);
    r.append(&label(&tr!("Transparent keys – the background image shows through"), &[]));
    d.body.append(&r);
    let r = hbox(10);
    r.set_margin_bottom(14);
    let pick = widgets::button(&tr!("Background image…"), Some("image"), "");
    r.append(&pick);
    r.append(&st.bg_label);
    r.append(&st.bg_remove);
    d.body.append(&r);
    let (st2, win) = (st.clone(), d.win.clone());
    pick.connect_clicked(move |_| {
        #[allow(deprecated)]
        let dlg = gtk::FileChooserNative::new(Some(&tr!("Choose background image")), Some(&win), gtk::FileChooserAction::Open,
                                              Some(&tr!("Open")), Some(&tr!("Cancel")));
        let filter = gtk::FileFilter::new();
        filter.set_name(Some(&tr!("Images")));
        for p in ["*.png", "*.jpg", "*.jpeg", "*.webp", "*.gif", "*.bmp"] {
            filter.add_pattern(p);
        }
        #[allow(deprecated)]
        dlg.add_filter(&filter);
        let st3 = st2.clone();
        #[allow(deprecated)]
        dlg.connect_response(move |d, resp| {
            if resp == gtk::ResponseType::Accept {
                if let Some(p) = d.file().and_then(|f| f.path()) {
                    *st3.background.borrow_mut() = Background::File(p.clone());
                    *st3.shown_bg.borrow_mut() = Some(p);
                    st3.show_background();
                    if !st3.transparent.get() {
                        st3.transparent.set(true); // sonst sieht man das Bild nur zwischen den Tasten
                    }
                }
            }
        });
        #[allow(deprecated)]
        dlg.show();
    });
    let st2 = st.clone();
    st.bg_remove.connect_clicked(move |_| {
        *st2.background.borrow_mut() = Background::None;
        *st2.shown_bg.borrow_mut() = None;
        st2.show_background();
    });
    d.body.append(&st.hint);
    st.result.set_margin_top(8);
    d.body.append(&st.result);
    st.load_look();
    st.update_hint();
    let (st2, win, w2) = (st.clone(), d.win.clone(), w.clone());
    create.connect_clicked(move |_| {
        if !st2.ok.get() {
            return;
        }
        let chans = st2.selected();
        let opts = ExportOptions {
            mix: st2.mix.borrow().clone(),
            all_channels: chans.len() == st2.channels.len(),
            opacity: Some(if st2.transparent.get() { 0 } else { 100 }),
            background: st2.background.borrow().clone(),
            ..Default::default()
        };
        match dx::export(&st2.device().model, &chans, &opts) {
            Ok((name, _, created)) => {
                win.close();
                w2.status(&if created {
                    tr!("Profile “{name}” created – choose it in FreeDeck for your device.", name = name)
                } else {
                    tr!("Profile “{name}” updated.", name = name)
                });
            }
            Err(e) => {
                st2.result.set_text(&tr!("Error: {error}", error = e));
                st2.result.add_css_class("danger");
            }
        }
    });
    d.win.present();
}
