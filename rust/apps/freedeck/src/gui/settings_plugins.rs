//! Einstellungen › Plugins (wie `PluginsPage` in `freestream/freedecksettings.py`): installierte Plugins
//! (Ordner `plugins/<UUID>.sdPlugin`) mit Symbol, Version, Autor, Aktionen und Beschreibung;
//! installieren aus einer `.streamDeckPlugin`-Datei, entfernen, Protokoll öffnen.

use gtk4 as gtk;
use gtk::prelude::*;
use gtk::{Align, Orientation};

use freestream_core::i18n::ngettext;
use freestream_core::plugins::{self, PluginInfo};
use freestream_core::tr;
use serde_json::Value;
use std::cell::RefCell;
use std::rc::Rc;

use super::settings::{button, choose_file, clear, confirm, message, open_uri, panel, run_bg, section, split, Ctx};
use super::util::label;

const ACCENT: &str = "#3d9bff";

struct Page {
    ctx: Rc<Ctx>,
    selected: RefCell<Option<String>>,
    list: gtk::Box,
    right: gtk::Box,
}

pub fn build(ctx: &Rc<Ctx>) -> gtk::Widget {
    let (page, left, right) = split(270);
    left.append(&section(&tr!("Installed")));
    let sc = gtk::ScrolledWindow::new();
    sc.set_hscrollbar_policy(gtk::PolicyType::Never);
    sc.set_vexpand(true);
    sc.set_margin_top(8);
    let list = gtk::Box::new(Orientation::Vertical, 4);
    sc.set_child(Some(&list));
    left.append(&sc);
    let install = button(&tr!("Install plugin …"), Some("list-add-symbolic"), "");
    install.set_margin_top(8);
    left.append(&install);
    let p = Rc::new(Page { ctx: ctx.clone(), selected: RefCell::new(None), list, right });
    let me = p.clone();
    install.connect_clicked(move |_| me.install());
    p.render();
    page.upcast()
}

/// Symbol eines Plugins (Datei aus dem Manifest), sonst eine farbige Kachel.
fn plugin_icon(info: &PluginInfo, size: i32) -> gtk::Widget {
    if let Some(p) = info.icon_path() {
        let img = if p.extension().map(|e| e == "svg").unwrap_or(false) {
            None
        } else {
            image::open(&p).ok().map(|i| i.resize_exact(size as u32 * 2, size as u32 * 2, image::imageops::FilterType::Triangle).to_rgba8())
        };
        let pic = match img {
            Some(i) => gtk::Picture::for_paintable(&super::util::texture(&i)),
            None => gtk::Picture::for_filename(&p),
        };
        pic.set_size_request(size, size);
        pic.set_can_shrink(true);
        pic.set_valign(Align::Center);
        pic.add_css_class("plugin-icon");
        return pic.upcast();
    }
    super::util::glyph_tile("puzzle", ACCENT, size).upcast()
}

/// (Text, CSS-Klasse) für den Zustand eines Plugins (ohne Plugin-Host: bereit oder läuft nicht).
fn status(info: &PluginInfo) -> (String, &'static str) {
    if !info.runnable() {
        return (info.reason.clone(), "danger");
    }
    if info.runtime == Some("node") && !freestream_deck::system::have("node") && !freestream_deck::system::have("nodejs") {
        return (tr!("Node.js missing (package nodejs)"), "warn");
    }
    (tr!("ready"), "muted")
}

impl Page {
    fn render(self: &Rc<Self>) {
        clear(&self.list);
        let found = plugins::installed(None);
        {
            let mut sel = self.selected.borrow_mut();
            if !found.iter().any(|i| Some(&i.uuid) == sel.as_ref()) {
                *sel = found.first().map(|i| i.uuid.clone());
            }
        }
        let sel = self.selected.borrow().clone();
        for info in &found {
            let b = gtk::Button::new();
            b.add_css_class("info-card");
            if Some(&info.uuid) == sel.as_ref() {
                b.add_css_class("selected");
            }
            let row = gtk::Box::new(Orientation::Horizontal, 10);
            row.append(&plugin_icon(info, 30));
            let col = gtk::Box::new(Orientation::Vertical, 1);
            col.set_valign(Align::Center);
            let n = label(&info.name, &["info-title"]);
            n.set_ellipsize(gtk::pango::EllipsizeMode::End);
            col.append(&n);
            let (text, kind) = status(info);
            let st = label(&[text.as_str(), info.version.as_str()].iter().filter(|s| !s.is_empty()).cloned().collect::<Vec<_>>().join(" · "), &["small", kind]);
            st.set_ellipsize(gtk::pango::EllipsizeMode::End);
            col.append(&st);
            row.append(&col);
            b.set_child(Some(&row));
            let (me, u) = (self.clone(), info.uuid.clone());
            b.connect_clicked(move |_| {
                *me.selected.borrow_mut() = Some(u.clone());
                me.render();
            });
            self.list.append(&b);
        }
        let body = panel(&self.right);
        match found.into_iter().find(|i| Some(&i.uuid) == sel.as_ref()) {
            Some(info) => self.build_detail(&body, &info),
            None => {
                body.set_valign(Align::Center);
                let tile = super::util::glyph_tile("puzzle", "#2c2c2c", 44);
                tile.set_halign(Align::Center);
                body.append(&tile);
                let t = label(&tr!("No plugins installed yet"), &["set-card-title"]);
                t.set_halign(Align::Center);
                t.set_margin_top(10);
                body.append(&t);
                let h = label(&tr!("Elgato plugins from the Marketplace also run on Linux if they are built with Node.js (most new plugins)."), &["muted"]);
                h.set_wrap(true);
                h.set_max_width_chars(50);
                h.set_justify(gtk::Justification::Center);
                h.set_halign(Align::Center);
                h.set_margin_top(4);
                body.append(&h);
            }
        }
    }

    fn build_detail(self: &Rc<Self>, body: &gtk::Box, info: &PluginInfo) {
        let head = gtk::Box::new(Orientation::Horizontal, 12);
        head.append(&plugin_icon(info, 48));
        let txt = gtk::Box::new(Orientation::Vertical, 2);
        txt.set_valign(Align::Center);
        txt.append(&label(&info.name, &["plugin-name"]));
        let author = info.manifest.get("Author").and_then(Value::as_str).unwrap_or("").to_string();
        let n = info.actions.len() as i64;
        let meta: Vec<String> = [
            if info.version.is_empty() { String::new() } else { tr!("Version {version}", version = info.version) },
            author,
            ngettext("{n} action", "{n} actions", n).replace("{n}", &n.to_string()),
        ].into_iter().filter(|s| !s.is_empty()).collect();
        txt.append(&label(&meta.join(" · "), &["muted", "small"]));
        head.append(&txt);
        body.append(&head);
        let srow = gtk::Box::new(Orientation::Horizontal, 8);
        srow.set_margin_top(16);
        let (text, kind) = status(info);
        let dot = gtk::Box::new(Orientation::Horizontal, 0);
        dot.add_css_class("dot");
        dot.add_css_class(&format!("dot-{}", if kind == "muted" { "muted" } else { kind }));
        dot.set_size_request(8, 8);
        dot.set_valign(Align::Center);
        srow.append(&dot);
        srow.append(&label(&text, &[kind]));
        body.append(&srow);
        if let Some(d) = info.manifest.get("Description").and_then(Value::as_str).filter(|d| !d.is_empty()) {
            let l = super::settings::hint(d);
            l.set_margin_top(12);
            l.set_max_width_chars(60);
            body.append(&l);
        }
        let spacer = gtk::Box::new(Orientation::Vertical, 0);
        spacer.set_vexpand(true);
        body.append(&spacer);
        let btns = gtk::Box::new(Orientation::Horizontal, 8);
        let log = button(&tr!("Log"), Some("text-x-generic-symbolic"), "");
        let lf = plugins::log_file(&info.uuid);
        log.set_sensitive(lf.is_file());
        log.connect_clicked(move |_| open_uri(&lf.to_string_lossy()));
        btns.append(&log);
        let rm = button(&tr!("Remove"), Some("user-trash-symbolic"), "danger-btn");
        let (me, uuid, name) = (self.clone(), info.uuid.clone(), info.name.clone());
        rm.connect_clicked(move |_| me.remove(&uuid, &name));
        btns.append(&rm);
        body.append(&btns);
    }

    fn install(self: &Rc<Self>) {
        let me = self.clone();
        choose_file(&self.ctx.win, &tr!("Install plugin"),
                    &[(tr!("Plugin (Elgato format)"), &["*.streamDeckPlugin", "*.zip"]), (tr!("All files"), &["*"])],
                    move |f| {
            let me2 = me.clone();
            // Entpacken kann dauern: im Hintergrund
            run_bg(move || plugins::install(&f, None).map_err(|e| format!("{}: {e}", f.display())), move |res| {
                let all = plugins::installed(None);
                let msgs: Vec<String> = match &res {
                    Ok(ids) => ids.iter().map(|uid| match all.iter().find(|i| i.uuid == *uid) {
                        Some(i) if !i.runnable() => tr!("“{name}” installed – does not run on Linux ({reason})", name = i.name, reason = i.reason),
                        Some(i) => tr!("“{name}” installed", name = i.name),
                        None => tr!("“{name}” installed", name = uid),
                    }).collect(),
                    Err(e) => vec![e.clone()],
                };
                if let Ok(ids) = &res {
                    if let Some(first) = ids.first() {
                        *me2.selected.borrow_mut() = Some(first.clone());
                    }
                }
                me2.render();
                if !msgs.is_empty() {
                    message(&me2.ctx.win, &tr!("Plugins"), &msgs.join("\n"));
                }
            });
        });
    }

    fn remove(self: &Rc<Self>, uuid: &str, name: &str) {
        let (me, uuid) = (self.clone(), uuid.to_string());
        confirm(&self.ctx.win, &tr!("Remove plugin"),
                &tr!("Remove “{name}”? Keys with its actions stay in the profile but no longer do anything.", name = name),
                &tr!("Remove"), true, move || {
            plugins::remove(&uuid, None);
            *me.selected.borrow_mut() = None;
            me.render();
        });
    }
}
