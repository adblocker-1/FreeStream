//! Einstellungen › Verbindungen (wie `freestream/connectionsui.py`): OBS, Discord, Home Assistant und
//! Windows-Profile (Tastaturlayout, Programme, Pfade). Links die Dienste mit Status, rechts die Felder.
//!
//! Passwörter und Tokens kommen in den Schlüsselbund, wenn es einen gibt (sonst in config.json);
//! Verbindungstests, Anmeldung und Schlüsselbund laufen in Threads.

use gtk4 as gtk;
use gtk::prelude::*;
use gtk::{Align, Orientation};

use freestream_core::config::Config;
use freestream_core::i18n::ngettext;
use freestream_core::{secrets, tr};
use freestream_deck::{discord, hass, obs};
use serde_json::{Map, Value};
use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::rc::Rc;
use std::time::Duration;

use super::settings::{button, cfg_read, cfg_section, clear, form, form_row, panel, run_bg, save_secret, section,
                      set_section_field, set_status, split, status_label, Ctx, Debounce, InfoCard};
use super::util::label;

const OBS_COLOR: &str = "#7c3aed";
const DISCORD_COLOR: &str = "#5865f2";
const HA_COLOR: &str = "#0ea5e9";
const WIN_COLOR: &str = "#64748b";
const DISCORD_PORTAL: &str = "https://discord.com/developers/applications";

struct Page {
    ctx: Rc<Ctx>,
    selected: RefCell<&'static str>,
    list: gtk::Box,
    right: gtk::Box,
    cards: RefCell<HashMap<&'static str, InfoCard>>,
    /// gesammeltes Speichern je Feld
    save: RefCell<HashMap<&'static str, Debounce>>,
}

pub fn build(ctx: &Rc<Ctx>) -> gtk::Widget {
    let (page, left, right) = split(250);
    left.append(&section(&tr!("Services")));
    let list = gtk::Box::new(Orientation::Vertical, 6);
    list.set_margin_top(8);
    left.append(&list);
    let p = Rc::new(Page { ctx: ctx.clone(), selected: RefCell::new("obs"), list, right, cards: Default::default(),
                           save: Default::default() });
    p.render();
    page.upcast()
}

fn services() -> [(&'static str, String, &'static str, &'static str); 4] {
    [("obs", "OBS Studio".to_string(), "record", OBS_COLOR), ("discord", "Discord".to_string(), "chat", DISCORD_COLOR),
     ("homeassistant", "Home Assistant".to_string(), "home", HA_COLOR), ("windows", tr!("Windows profiles"), "keyboard", WIN_COLOR)]
}

/// Unterzeile und Punkt eines Dienstes in der Liste.
fn summary(cfg: &Config, key: &str) -> (String, Option<&'static str>) {
    let ok = match key {
        "obs" => !cfg_section(cfg, "obs").is_empty(),
        "discord" => secrets::configured(&cfg.data, "discord", "client_id") && secrets::configured(&cfg.data, "discord", "client_secret"),
        "homeassistant" => secrets::configured(&cfg.data, hass::CONFIG_KEY, "url") && secrets::configured(&cfg.data, hass::CONFIG_KEY, "token"),
        _ => {
            let n = cfg_section(cfg, "app_map").len() + cfg_section(cfg, "path_map").len();
            let text = if n > 0 { ngettext("{n} mapping", "{n} mappings", n as i64).replace("{n}", &n.to_string()) } else { tr!("Automatic") };
            return (text, Some("muted"));
        }
    };
    if ok { (tr!("Set up"), Some("ok")) } else { (tr!("Not set up"), Some("faint")) }
}

impl Page {
    fn render(self: &Rc<Self>) {
        clear(&self.list);
        let cfg = cfg_read();
        let sel = *self.selected.borrow();
        let mut cards = HashMap::new();
        for (key, title, glyph, color) in services() {
            let (text, dot) = summary(&cfg, key);
            let card = InfoCard::new(&title, &text, glyph, color);
            card.set_dot(dot);
            card.set_selected(key == sel);
            let me = self.clone();
            card.button.connect_clicked(move |_| {
                *me.selected.borrow_mut() = key;
                me.render();
            });
            self.list.append(&card.button);
            cards.insert(key, card);
        }
        *self.cards.borrow_mut() = cards;
        let body = panel(&self.right);
        match sel {
            "obs" => self.build_obs(&body),
            "discord" => self.build_discord(&body),
            "homeassistant" => self.build_ha(&body),
            _ => self.build_windows(&body),
        }
    }

    /// Unterzeile des gewählten Dienstes nachziehen.
    fn refresh_card(&self) {
        let sel = *self.selected.borrow();
        if let Some(card) = self.cards.borrow().get(sel) {
            let (text, dot) = summary(&cfg_read(), sel);
            card.sub.set_text(&text);
            card.set_dot(dot);
        }
    }

    /// Feld eines Abschnitts speichern (gesammelt beim Tippen).
    fn set_field(self: &Rc<Self>, section: &'static str, field: &'static str, value: Option<Value>) {
        let me = self.clone();
        let d = self.save.borrow_mut().entry(field).or_default().clone();
        d.run(400, move || {
            me.ctx.edit(|cfg| set_section_field(cfg, section, field, value));
            me.refresh_card();
        });
    }

    // ---------------------------------------------------------------------------------- Bausteine
    fn head(body: &gtk::Box, title: &str, text: &str, glyph: &str, color: &str) {
        let h = gtk::Box::new(Orientation::Horizontal, 12);
        h.set_margin_bottom(16);
        let tile = super::util::glyph_tile(glyph, color, 40);
        tile.set_valign(Align::Start);
        h.append(&tile);
        let t = gtk::Box::new(Orientation::Vertical, 2);
        t.set_hexpand(true);
        t.append(&label(title, &["plugin-name"]));
        let l = super::settings::hint(text);
        l.set_max_width_chars(60);
        t.append(&l);
        h.append(&t);
        body.append(&h);
    }

    fn entry(text: &str, width: i32) -> gtk::Entry {
        let e = gtk::Entry::new();
        e.set_text(text);
        e.set_width_chars(width);
        e
    }

    /// Feld für Passwort/Token: Wert aus der Datei oder (im Hintergrund) aus dem Schlüsselbund; Änderungen
    /// gehen gesammelt in den Schlüsselbund.
    fn secret_entry(self: &Rc<Self>, section: &'static str, field: &'static str) -> gtk::PasswordEntry {
        let e = gtk::PasswordEntry::new();
        e.set_show_peek_icon(true);
        e.set_width_chars(28);
        let cfg = cfg_read();
        let loading = Rc::new(Cell::new(false));
        if let Some(v) = secrets::in_file(&cfg.data, section, field) {
            e.set_text(&v);
        } else if secrets::listed(&cfg.data, section, field) {
            loading.set(true);
            e.set_sensitive(false);
            let (e2, l2) = (e.clone(), loading.clone());
            run_bg(move || secrets::lookup(&format!("{section}.{field}")), move |v| {
                e2.set_text(&v.unwrap_or_default());
                e2.set_sensitive(true);
                l2.set(false);
            });
        }
        let save = Debounce::default();
        let me = self.clone();
        e.connect_changed(move |e| {
            if loading.get() {
                return;
            }
            let v = e.text().to_string();
            let me2 = me.clone();
            save.run(800, move || {
                let me3 = me2.clone();
                run_bg(move || save_secret(section, field, &v), move |_| {
                    me3.ctx.sync();
                    me3.refresh_card();
                });
            });
        });
        e
    }

    fn buttons(body: &gtk::Box) -> gtk::Box {
        let b = gtk::Box::new(Orientation::Horizontal, 8);
        b.set_margin_top(14);
        body.append(&b);
        b
    }

    fn status(body: &gtk::Box) -> gtk::Label {
        let s = status_label();
        s.set_margin_top(10);
        body.append(&s);
        s
    }

    // ---------------------------------------------------------------------------------- OBS
    fn build_obs(self: &Rc<Self>, body: &gtk::Box) {
        Self::head(body, "OBS Studio", &tr!("Turn on the WebSocket server in OBS: Tools → WebSocket Server Settings → Enable WebSocket server. Copy the password from “Show Connect Info”."),
                   "record", OBS_COLOR);
        let c = cfg_section(&cfg_read(), "obs");
        let g = form();
        body.append(&g);
        let host = Self::entry(c.get("host").and_then(Value::as_str).filter(|h| !h.is_empty()).unwrap_or("127.0.0.1"), 28);
        form_row(&g, 0, &tr!("Address"), &host);
        let port_text = match c.get("port") {
            Some(Value::Number(n)) => n.to_string(),
            Some(Value::String(s)) if !s.is_empty() => s.clone(),
            _ => obs::DEFAULT_PORT.to_string(),
        };
        let port = Self::entry(&port_text, 8);
        form_row(&g, 1, &tr!("Port"), &port);
        let pw = self.secret_entry("obs", "password");
        form_row(&g, 2, &tr!("Password"), &pw);
        let me = self.clone();
        host.connect_changed(move |e| {
            let v = e.text().trim().to_string();
            me.set_field("obs", "host", (!v.is_empty()).then(|| Value::from(v)));
        });
        let me = self.clone();
        port.connect_changed(move |e| {
            let v = e.text().trim().to_string();
            if v.is_empty() {
                me.set_field("obs", "port", None);
            } else if let Ok(n) = v.parse::<u16>() {
                me.set_field("obs", "port", Some(Value::from(n)));
            }
        });
        let btns = Self::buttons(body);
        let test = button(&tr!("Test connection"), Some("emblem-ok-symbolic"), "");
        btns.append(&test);
        let status = Self::status(body);
        test.connect_clicked(move |b| {
            let h = host.text().trim().to_string();
            let p = port.text().trim().to_string();
            let p: u16 = if p.is_empty() { obs::DEFAULT_PORT } else {
                match p.parse() {
                    Ok(n) => n,
                    Err(_) => {
                        set_status(&status, &tr!("The port must be a number."), "error");
                        return;
                    }
                }
            };
            let password = pw.text().to_string();
            set_status(&status, &tr!("Connecting …"), "muted");
            b.set_sensitive(false);
            let (b, status) = (b.clone(), status.clone());
            run_bg(move || obs::test_connection(&h, p, &password, Duration::from_secs(4)), move |res| {
                b.set_sensitive(true);
                match res {
                    Ok(()) => set_status(&status, &tr!("Connected to OBS."), "ok"),
                    Err(e) => set_status(&status, &e, "error"),
                }
            });
        });
    }

    // ---------------------------------------------------------------------------------- Discord
    fn build_discord(self: &Rc<Self>, body: &gtk::Box) {
        Self::head(body, "Discord", &tr!("Discord only lets apps control the microphone and sound with your own Discord app. Create one in the Discord Developer Portal, add the redirect “http://localhost” under OAuth2 and copy its Client ID and Client Secret."),
                   "chat", DISCORD_COLOR);
        let cfg = cfg_read();
        let g = form();
        body.append(&g);
        let id = Self::entry(&secrets::in_file(&cfg.data, "discord", "client_id").unwrap_or_default(), 28);
        form_row(&g, 0, &tr!("Client ID"), &id);
        let secret = self.secret_entry("discord", "client_secret");
        form_row(&g, 1, &tr!("Client Secret"), &secret);
        let me = self.clone();
        id.connect_changed(move |e| {
            let v = e.text().trim().to_string();
            me.set_field("discord", "client_id", (!v.is_empty()).then(|| Value::from(v)));
        });
        let btns = Self::buttons(body);
        let connect = button(&tr!("Connect …"), Some("emblem-ok-symbolic"), "");
        btns.append(&connect);
        let portal = button(&tr!("Developer Portal"), Some("web-browser-symbolic"), "ghost");
        portal.connect_clicked(|_| super::settings::open_uri(DISCORD_PORTAL));
        btns.append(&portal);
        let logout = button(&tr!("Log out"), Some("system-log-out-symbolic"), "ghost");
        logout.set_visible(discord::logged_in());
        btns.append(&logout);
        let status = Self::status(body);
        let (st2, lo2) = (status.clone(), logout.clone());
        logout.connect_clicked(move |b| {
            b.set_sensitive(false);
            let (b, st, lo) = (b.clone(), st2.clone(), lo2.clone());
            run_bg(discord::logout, move |_| {
                b.set_sensitive(true);
                lo.set_visible(discord::logged_in());
                set_status(&st, "", "muted");
            });
        });
        let me = self.clone();
        connect.connect_clicked(move |b| {
            let cid = id.text().trim().to_string();
            let sec = secret.text().trim().to_string();
            if cid.is_empty() || sec.is_empty() {
                set_status(&status, &tr!("Please enter the Client ID and the Client Secret."), "error");
                return;
            }
            set_status(&status, &tr!("Connecting – please confirm the request in Discord …"), "muted");
            b.set_sensitive(false);
            let (b, status, logout, me) = (b.clone(), status.clone(), logout.clone(), me.clone());
            run_bg(move || {
                // erst speichern (Schlüsselbund), dann mit den gespeicherten Daten anmelden
                super::settings::cfg_edit(|cfg| set_section_field(cfg, "discord", "client_id", Some(Value::from(cid))));
                save_secret("discord", "client_secret", &sec);
                let cfg = Config::load();
                discord::client(&cfg.data).connect().map_err(|e| e.to_string())
            }, move |res| {
                b.set_sensitive(true);
                me.ctx.sync();
                me.refresh_card();
                logout.set_visible(discord::logged_in());
                match res {
                    Ok(()) => set_status(&status, &tr!("Connected to Discord."), "ok"),
                    Err(e) => set_status(&status, &e, "error"),
                }
            });
        });
    }

    // ---------------------------------------------------------------------------------- Home Assistant
    fn build_ha(self: &Rc<Self>, body: &gtk::Box) {
        Self::head(body, "Home Assistant", &tr!("Enter the address of your Home Assistant and a long-lived access token. Create the token in Home Assistant under your profile → Security → Long-lived access tokens."),
                   "home", HA_COLOR);
        let c = cfg_section(&cfg_read(), hass::CONFIG_KEY);
        let g = form();
        body.append(&g);
        let url = Self::entry(c.get("url").and_then(Value::as_str).unwrap_or(""), 28);
        url.set_placeholder_text(Some("http://homeassistant.local:8123"));
        form_row(&g, 0, &tr!("Address"), &url);
        let token = self.secret_entry(hass::CONFIG_KEY, "token");
        form_row(&g, 1, &tr!("Access token"), &token);
        let row = gtk::Box::new(Orientation::Horizontal, 10);
        let insecure = gtk::Switch::new();
        insecure.set_active(c.get("verify_ssl") == Some(&Value::Bool(false)));
        insecure.set_valign(Align::Center);
        row.append(&insecure);
        row.append(&label(&tr!("Accept self-signed certificates (HTTPS)"), &[]));
        form_row(&g, 2, "", &row);
        let me = self.clone();
        url.connect_changed(move |e| {
            let v = hass::normalize_url(&e.text());
            me.set_field(hass::CONFIG_KEY, "url", (!v.is_empty()).then(|| Value::from(v)));
        });
        let me = self.clone();
        insecure.connect_active_notify(move |s| {
            let v = s.is_active().then_some(Value::Bool(false));
            me.ctx.edit(|cfg| set_section_field(cfg, hass::CONFIG_KEY, "verify_ssl", v));
            me.refresh_card();
        });
        let btns = Self::buttons(body);
        let test = button(&tr!("Test connection"), Some("emblem-ok-symbolic"), "");
        btns.append(&test);
        let status = Self::status(body);
        test.connect_clicked(move |b| {
            let (u, t) = (hass::normalize_url(&url.text()), token.text().trim().to_string());
            if u.is_empty() || t.is_empty() {
                set_status(&status, &tr!("Please enter the address and the access token."), "error");
                return;
            }
            let verify = !insecure.is_active();
            set_status(&status, &tr!("Connecting …"), "muted");
            b.set_sensitive(false);
            let (b, status) = (b.clone(), status.clone());
            run_bg(move || hass::check_connection(&u, &t, verify).map_err(|e| e.to_string()), move |res| {
                b.set_sensitive(true);
                match res {
                    Ok((version, name)) => set_status(&status, &tr!("Connected to “{name}” (Home Assistant {version}).",
                        name = if name.is_empty() { "Home".to_string() } else { name },
                        version = if version.is_empty() { "?".to_string() } else { version }), "ok"),
                    Err(e) => set_status(&status, &e, "error"),
                }
            });
        });
    }

    // ---------------------------------------------------------------------------------- Windows-Profile
    fn build_windows(self: &Rc<Self>, body: &gtk::Box) {
        Self::head(body, &tr!("Windows profiles"), &tr!("Profiles from Windows start Windows programs and use Windows paths. Tell FreeDeck what to use on Linux instead."),
                   "keyboard", WIN_COLOR);
        let g = form();
        body.append(&g);
        let detected = match freestream_deck::input::detect_layout() {
            freestream_deck::input::Layout::De => tr!("German"),
            freestream_deck::input::Layout::Us => tr!("English (US)"),
        };
        let keys = ["auto", "de", "us"];
        let layout = super::settings::dropdown(&[tr!("Automatic ({layout})", layout = detected), tr!("German"), tr!("English (US)")]);
        let cur = cfg_read().get_str("keyboard_layout");
        layout.set_selected(keys.iter().position(|k| *k == cur).unwrap_or(0) as u32);
        let me = self.clone();
        layout.connect_selected_notify(move |d| {
            let k = keys[d.selected() as usize % 3];
            me.ctx.edit(|cfg| cfg.set("keyboard_layout", Value::from(k)));
        });
        form_row(&g, 0, &tr!("Keyboard layout"), &layout);
        self.map_table(body, "app_map", &tr!("Programs"), &tr!("Windows program (e.g. obs64.exe)"), &tr!("Linux command"));
        self.map_table(body, "path_map", &tr!("Folders"), &tr!("Windows path (e.g. C:/Users/Alex/Music)"), &tr!("Linux path"));
    }

    /// Tabelle Windows → Linux (`app_map` bzw. `path_map`).
    fn map_table(self: &Rc<Self>, body: &gtk::Box, key: &'static str, title: &str, left: &str, right: &str) {
        let s = section(title);
        s.set_margin_top(18);
        s.set_margin_bottom(6);
        body.append(&s);
        let rows = gtk::Box::new(Orientation::Vertical, 6);
        body.append(&rows);
        type Pairs = Rc<RefCell<Vec<(gtk::Entry, gtk::Entry)>>>;
        let pairs: Pairs = Rc::default();
        let save = Debounce::default();
        let store = {
            let (me, pairs, save) = (self.clone(), pairs.clone(), save.clone());
            Rc::new(move || {
                let mut out = Map::new();
                for (a, b) in pairs.borrow().iter() {
                    let (k, v) = (a.text().trim().to_string(), b.text().trim().to_string());
                    if !k.is_empty() && !v.is_empty() {
                        out.insert(k, Value::from(v));
                    }
                }
                let me2 = me.clone();
                save.run(500, move || {
                    me2.ctx.edit(|cfg| cfg.set(key, Value::Object(out)));
                    me2.refresh_card();
                });
            })
        };
        let add: Rc<dyn Fn(&str, &str)> = {
            let (rows, pairs, store) = (rows.clone(), pairs.clone(), store.clone());
            Rc::new(move |k: &str, v: &str| {
                let r = gtk::Box::new(Orientation::Horizontal, 6);
                let a = Self::entry(k, 24);
                let b = Self::entry(v, 24);
                r.append(&a);
                r.append(&label("→", &["muted"]));
                r.append(&b);
                let del = super::util::icon_button("user-trash-symbolic", &tr!("Remove"));
                r.append(&del);
                rows.append(&r);
                pairs.borrow_mut().push((a.clone(), b.clone()));
                for e in [&a, &b] {
                    let st = store.clone();
                    e.connect_changed(move |_| st());
                }
                let (rows2, pairs2, st, a2) = (rows.clone(), pairs.clone(), store.clone(), a.clone());
                del.connect_clicked(move |_| {
                    pairs2.borrow_mut().retain(|(x, _)| x != &a2);
                    rows2.remove(&r);
                    st();
                });
                if k.is_empty() {
                    a.grab_focus();
                }
            })
        };
        for (k, v) in cfg_section(&cfg_read(), key) {
            add(&k, v.as_str().unwrap_or(""));
        }
        let h = label(&format!("{left}  →  {right}"), &["faint", "small"]);
        h.set_margin_top(4);
        body.append(&h);
        let b = button(&tr!("Add"), Some("list-add-symbolic"), "ghost");
        b.set_margin_top(4);
        b.connect_clicked(move |_| add("", ""));
        body.append(&b);
    }
}
