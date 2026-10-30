//! Einstellungen › Profile (wie `ProfilesPage` in `freestream/freedecksettings.py`): links alle Profile
//! mit ihren Seiten, rechts „Automatisch wechseln“ (Programm → Profil, `smart_profiles`) und das
//! Standardprofil (`default_profile`).

use gtk4 as gtk;
use gtk::glib;
use gtk::prelude::*;
use gtk::{Align, Orientation};

use freestream_core::config::Config;
use freestream_core::models::model_name;
use freestream_core::profiles::{Profile, ProfileStore};
use freestream_core::{editdoc, tr};
use freestream_deck::system::{have, run_out};
use serde_json::Value;
use std::cell::RefCell;
use std::rc::Rc;

use super::settings::{ask_text, button, card, cfg_read, clear, confirm, hint, panel, split, Ctx};
use super::util::label;

/// Programme aus den .desktop-Dateien: (Name, Fensterklasse) – wie `desktop.installed_apps`.
pub fn installed_apps() -> Vec<(String, String)> {
    let home = freestream_core::paths::home();
    let mut dirs: Vec<std::path::PathBuf> = std::env::var("XDG_DATA_DIRS").ok().filter(|s| !s.is_empty())
        .unwrap_or_else(|| "/usr/local/share:/usr/share".into())
        .split(':').map(|p| std::path::Path::new(p).join("applications")).collect();
    dirs.push(std::env::var("XDG_DATA_HOME").map(std::path::PathBuf::from).unwrap_or_else(|_| home.join(".local/share")).join("applications"));
    dirs.push("/var/lib/flatpak/exports/share/applications".into());
    dirs.push(home.join(".local/share/flatpak/exports/share/applications"));
    let mut found: Vec<(String, String)> = Vec::new();
    for d in dirs {
        let Ok(rd) = std::fs::read_dir(&d) else { continue };
        let mut files: Vec<_> = rd.flatten().map(|e| e.path()).filter(|p| p.extension().map(|e| e == "desktop").unwrap_or(false)).collect();
        files.sort();
        for f in files {
            let Ok(text) = std::fs::read_to_string(&f) else { continue };
            let entry = text.split("[Desktop Action").next().unwrap_or("");
            let mut vals: std::collections::HashMap<String, String> = Default::default();
            for line in entry.lines() {
                if line.starts_with('#') {
                    continue;
                }
                if let Some((k, v)) = line.split_once('=') {
                    vals.entry(k.trim().to_string()).or_insert_with(|| v.trim().to_string());
                }
            }
            let get = |k: &str| vals.get(k).cloned().unwrap_or_default();
            if get("NoDisplay").eq_ignore_ascii_case("true") || vals.get("Type").map(|t| t != "Application").unwrap_or(false) {
                continue;
            }
            let name = get("Name");
            if name.is_empty() || found.iter().any(|(n, _)| *n == name) {
                continue;
            }
            let stem = f.file_stem().and_then(|s| s.to_str()).unwrap_or("").to_string();
            let exec = Some(get("Exec")).filter(|e| !e.is_empty()).unwrap_or_else(|| stem.clone());
            let first = exec.split_whitespace().next().unwrap_or("");
            let base = first.rsplit('/').next().unwrap_or(first);
            let exe = base.rsplit_once('.').map(|(a, _)| a).unwrap_or(base).to_string();
            let mut cls = Some(get("StartupWMClass")).filter(|c| !c.is_empty()).unwrap_or(exe);
            if cls == "env" || cls == "flatpak" {
                cls = if stem.matches('.').count() >= 2 { stem.rsplit('.').next().unwrap_or(&stem).to_string() } else { stem.clone() };
            }
            found.push((name, cls.to_lowercase()));
        }
    }
    found.sort_by_key(|(n, _)| n.to_lowercase());
    found
}

/// WM_CLASS / App-ID des aktiven Fensters (Hyprland, Sway, KDE, X11) – wie `system.active_window_class`.
pub fn active_window_class() -> String {
    let env = |k: &str| std::env::var(k).unwrap_or_default();
    if !env("HYPRLAND_INSTANCE_SIGNATURE").is_empty() && have("hyprctl") {
        let out = run_out(&["hyprctl", "activewindow"]);
        return out.lines().find_map(|l| l.trim().strip_prefix("class:")).map(|c| c.trim().to_lowercase()).unwrap_or_default();
    }
    if !env("SWAYSOCK").is_empty() && have("swaymsg") {
        let out = run_out(&["swaymsg", "-t", "get_tree"]);
        if let Ok(tree) = serde_json::from_str::<Value>(&out) {
            fn focused(n: &Value) -> Option<String> {
                if n.get("focused").and_then(Value::as_bool) == Some(true) {
                    return n.get("app_id").and_then(Value::as_str).map(str::to_lowercase);
                }
                for k in ["nodes", "floating_nodes"] {
                    for c in n.get(k).and_then(Value::as_array).into_iter().flatten() {
                        if let Some(r) = focused(c) {
                            return Some(r);
                        }
                    }
                }
                None
            }
            return focused(&tree).unwrap_or_default();
        }
        return String::new();
    }
    if have("kdotool") && env("XDG_CURRENT_DESKTOP").contains("KDE") {
        let wid = run_out(&["kdotool", "getactivewindow"]).trim().to_string();
        if !wid.is_empty() {
            return run_out(&["kdotool", "getwindowclassname", &wid]).trim().to_lowercase();
        }
    }
    if !env("DISPLAY").is_empty() && have("xprop") {
        let out = run_out(&["xprop", "-root", "_NET_ACTIVE_WINDOW"]);
        if let Some(id) = out.split("window id # ").nth(1).and_then(|s| s.split([',', ' ', '\n']).next()) {
            if id.starts_with("0x") && id != "0x0" {
                let cls = run_out(&["xprop", "-id", id, "WM_CLASS"]);
                let parts: Vec<&str> = cls.split('"').skip(1).step_by(2).collect();
                return parts.last().map(|p| p.to_lowercase()).unwrap_or_default();
            }
        }
    }
    String::new()
}

struct Page {
    ctx: Rc<Ctx>,
    selected: RefCell<Option<String>>,
    tree: gtk::Box,
    right: gtk::Box,
    apps: RefCell<Option<Vec<(String, String)>>>,
}

pub fn build(ctx: &Rc<Ctx>) -> gtk::Widget {
    let (page, left, right) = split(270);
    let sc = gtk::ScrolledWindow::new();
    sc.set_hscrollbar_policy(gtk::PolicyType::Never);
    sc.set_vexpand(true);
    let tree = gtk::Box::new(Orientation::Vertical, 2);
    sc.set_child(Some(&tree));
    left.append(&sc);
    let bar = gtk::Box::new(Orientation::Horizontal, 4);
    bar.set_margin_top(8);
    let add = super::util::icon_button("list-add-symbolic", &tr!("New profile"));
    let del = super::util::icon_button("user-trash-symbolic", &tr!("Delete profile"));
    bar.append(&add);
    bar.append(&del);
    left.append(&bar);
    let selected = ctx.state.borrow().profile.clone();
    let p = Rc::new(Page { ctx: ctx.clone(), selected: RefCell::new(selected), tree, right, apps: RefCell::new(None) });
    let me = p.clone();
    add.connect_clicked(move |_| me.new_profile());
    let me = p.clone();
    del.connect_clicked(move |_| me.delete());
    p.render();
    page.upcast()
}

fn smart_map(cfg: &Config) -> serde_json::Map<String, Value> {
    cfg.get("smart_profiles").and_then(Value::as_object).cloned().unwrap_or_default()
}

impl Page {
    fn profiles() -> Vec<Profile> {
        let store = ProfileStore::new(None);
        let mut v: Vec<Profile> = store.profiles.values().cloned().collect();
        v.sort_by_key(|p| p.name.to_lowercase());
        v
    }

    fn render(self: &Rc<Self>) {
        clear(&self.tree);
        let profs = Self::profiles();
        {
            let mut sel = self.selected.borrow_mut();
            if !profs.iter().any(|p| Some(&p.id) == sel.as_ref()) {
                *sel = profs.first().map(|p| p.id.clone());
            }
        }
        let sel = self.selected.borrow().clone();
        let default = cfg_read().get_str("default_profile");
        for p in &profs {
            let is_sel = Some(&p.id) == sel.as_ref();
            let b = gtk::Button::new();
            b.add_css_class("prof-row");
            if is_sel {
                b.add_css_class("selected");
            }
            let row = gtk::Box::new(Orientation::Horizontal, 6);
            row.append(&gtk::Image::from_icon_name(if is_sel { "pan-down-symbolic" } else { "pan-end-symbolic" }));
            let n = label(&p.name, if is_sel { &["info-title"] } else { &[] });
            n.set_hexpand(true);
            n.set_ellipsize(gtk::pango::EllipsizeMode::End);
            row.append(&n);
            if p.id == default {
                row.append(&label("★", &["star"]));
            }
            b.set_child(Some(&row));
            let (me, pid) = (self.clone(), p.id.clone());
            b.connect_clicked(move |_| {
                *me.selected.borrow_mut() = Some(pid.clone());
                me.render();
            });
            self.tree.append(&b);
            for (i, _) in p.page_ids.iter().enumerate() {
                let sub = gtk::Box::new(Orientation::Horizontal, 8);
                sub.set_margin_start(30);
                sub.set_margin_top(2);
                sub.set_margin_bottom(2);
                let n = label(&(i + 1).to_string(), &["page-num"]);
                n.set_xalign(0.5);
                sub.append(&n);
                sub.append(&label(&tr!("Page {n}", n = i + 1), &["muted", "small"]));
                self.tree.append(&sub);
            }
        }
        clear(&self.right);
        if let Some(p) = sel.and_then(|id| profs.into_iter().find(|p| p.id == id)) {
            self.build_detail(&p);
        }
    }

    fn build_detail(self: &Rc<Self>, p: &Profile) {
        let body = panel(&self.right);
        body.set_spacing(0);
        let h = label(&tr!("Switch automatically"), &["set-card-title"]);
        body.append(&h);
        let t = hint(&tr!("FreeDeck switches to this profile as soon as the chosen program is in the foreground."));
        t.set_margin_top(4);
        t.set_margin_bottom(14);
        body.append(&t);
        let r = gtk::Box::new(Orientation::Horizontal, 12);
        r.append(&label(&tr!("Program"), &["field-label"]));
        if self.apps.borrow().is_none() {
            *self.apps.borrow_mut() = Some(installed_apps());
        }
        let apps = self.apps.borrow().clone().unwrap_or_default();
        let combo = gtk::ComboBoxText::with_entry();
        combo.append_text(&tr!("None"));
        for (n, _) in &apps {
            combo.append_text(n);
        }
        combo.set_hexpand(true);
        let cfg = cfg_read();
        let cur = smart_map(&cfg).iter().find(|(_, v)| v.as_str() == Some(p.id.as_str())).map(|(k, _)| k.clone()).unwrap_or_default();
        let shown = if cur.is_empty() { tr!("None") } else { apps.iter().find(|(_, c)| *c == cur).map(|(n, _)| n.clone()).unwrap_or(cur.clone()) };
        let entry = combo.child().and_then(|c| c.downcast::<gtk::Entry>().ok());
        if let Some(e) = &entry {
            e.set_text(&shown);
        }
        r.append(&combo);
        body.append(&r);
        let app_id = p.app_identifier.clone();
        if cur.is_empty() && !app_id.is_empty() {
            let l = label(&tr!("From the imported profile: {app}", app = app_id), &["faint", "small"]);
            l.set_margin_top(6);
            body.append(&l);
        }
        // Programm übernehmen: Auswahl, Enter, Fokus weg
        let apply = {
            let (me, apps, pid, entry) = (self.clone(), apps.clone(), p.id.clone(), entry.clone());
            let last = Rc::new(RefCell::new(shown.clone()));
            Rc::new(move || {
                let Some(e) = &entry else { return };
                let text = e.text().trim().to_string();
                if *last.borrow() == text {
                    return;
                }
                *last.borrow_mut() = text.clone();
                let cls = if text.is_empty() || text == tr!("None") {
                    String::new()
                } else {
                    apps.iter().find(|(n, _)| *n == text).map(|(_, c)| c.clone()).unwrap_or(text)
                };
                me.set_program_class(&pid, &cls);
            })
        };
        let a = apply.clone();
        combo.connect_changed(move |c| {
            if c.active().is_some() {
                a();
            }
        });
        if let Some(e) = &entry {
            let a = apply.clone();
            e.connect_activate(move |_| a());
            let focus = gtk::EventControllerFocus::new();
            let a = apply.clone();
            focus.connect_leave(move |_| a());
            e.add_controller(focus);
        }
        let pick = button(&tr!("Use active window"), Some("edit-find-symbolic"), "");
        pick.set_margin_top(10);
        body.append(&pick);
        let status = label("", &["muted", "small"]);
        status.set_margin_top(4);
        body.append(&status);
        let (me, pid, apps2, entry2, status2) = (self.clone(), p.id.clone(), apps.clone(), entry.clone(), status.clone());
        pick.connect_clicked(move |b| {
            // nach 3 s das Fenster im Vordergrund übernehmen (so lange zum gewünschten Programm wechseln)
            b.set_sensitive(false);
            let n = Rc::new(std::cell::Cell::new(3));
            status2.set_text(&tr!("Switch to the program now … {n}", n = 3));
            let (b, me, pid, apps, entry, status) = (b.clone(), me.clone(), pid.clone(), apps2.clone(), entry2.clone(), status2.clone());
            glib::timeout_add_seconds_local(1, move || {
                n.set(n.get() - 1);
                if n.get() > 0 {
                    status.set_text(&tr!("Switch to the program now … {n}", n = n.get()));
                    return glib::ControlFlow::Continue;
                }
                b.set_sensitive(true);
                let cls = active_window_class();
                if cls.is_empty() || ["freedeck", "freestream", "lindeck", "streamlin"].contains(&cls.as_str())
                    || cls.starts_with("io.github.freestream") {
                    status.set_text(&tr!("No other program was in the foreground."));
                    return glib::ControlFlow::Break;
                }
                status.set_text(&tr!("Program: {app}", app = cls));
                if let Some(e) = &entry {
                    e.set_text(&apps.iter().find(|(_, c)| *c == cls).map(|(n, _)| n.clone()).unwrap_or(cls.clone()));
                }
                me.set_program_class(&pid, &cls);
                glib::ControlFlow::Break
            });
        });
        let sep = gtk::Separator::new(Orientation::Horizontal);
        sep.add_css_class("sep");
        sep.set_margin_top(16);
        sep.set_margin_bottom(16);
        body.append(&sep);
        let (row, sw) = super::settings::switch_row(&tr!("This is my default profile"),
                                                     &tr!("It appears whenever the program in the foreground has no profile of its own."),
                                                     cfg.get_str("default_profile") == p.id);
        body.append(&row);
        let (me, pid) = (self.clone(), p.id.clone());
        sw.connect_active_notify(move |sw| {
            let on = sw.is_active();
            me.ctx.edit(|cfg| {
                if on {
                    cfg.set("default_profile", Value::from(pid.clone()));
                } else if cfg.get_str("default_profile") == pid {
                    cfg.data.remove("default_profile");
                }
            });
            let me2 = me.clone();
            glib::idle_add_local_once(move || me2.render());
        });
        let info = card();
        info.add_css_class("inner-card");
        info.set_margin_top(18);
        info.set_spacing(2);
        info.append(&label(&p.name, &["info-title"]));
        let pages = freestream_core::i18n::ngettext("{n} page", "{n} pages", p.page_ids.len() as i64).replace("{n}", &p.page_ids.len().to_string());
        info.append(&label(&format!("{} · {}", model_name(&p.model), pages), &["muted", "small"]));
        info.set_valign(Align::End);
        info.set_vexpand(true);
        body.append(&info);
    }

    /// Programm (Fensterklasse) diesem Profil zuordnen; leer: Zuordnung entfernen.
    fn set_program_class(&self, pid: &str, cls: &str) {
        self.ctx.edit(|cfg| {
            let m = cfg.section("smart_profiles");
            m.retain(|_, v| v.as_str() != Some(pid));
            if !cls.is_empty() {
                m.insert(cls.to_lowercase(), Value::from(pid));
            }
        });
    }

    /// Modell für ein neues Profil: angeschlossenes Gerät, sonst das häufigste Modell der Profile.
    fn auto_model(&self) -> String {
        let serial = self.ctx.state.borrow().serial.clone();
        for c in self.ctx.controllers() {
            let c = c.lock().unwrap();
            if serial.is_none() || serial.as_deref() == Some(c.serial().as_str()) {
                return c.dev.model();
            }
        }
        let mut counts: std::collections::HashMap<String, usize> = Default::default();
        for p in Self::profiles().iter().filter(|p| !p.model.is_empty()) {
            *counts.entry(p.model.clone()).or_default() += 1;
        }
        counts.into_iter().max_by_key(|(_, n)| *n).map(|(m, _)| m).unwrap_or_else(|| "20GBA9901".into())
    }

    fn new_profile(self: &Rc<Self>) {
        let model = self.auto_model();
        let me = self.clone();
        ask_text(&self.ctx.win, &tr!("New profile"), &tr!("Name"), &tr!("New profile"), &tr!("Create"), move |name| {
            match editdoc::create_profile(&name, &model, None) {
                Ok(root) => {
                    let pid = root.file_name().and_then(|n| n.to_str()).and_then(|n| n.strip_suffix(".sdProfile")).map(str::to_string);
                    // der Editor öffnet das neue Profil, sobald er die geänderte Profilliste bemerkt
                    me.ctx.state.borrow_mut().profile = pid.clone();
                    *me.selected.borrow_mut() = pid;
                    me.render();
                }
                Err(e) => super::settings::message(&me.ctx.win, &tr!("New profile"), &e.to_string()),
            }
        });
    }

    fn delete(self: &Rc<Self>) {
        let Some(pid) = self.selected.borrow().clone() else { return };
        let name = Self::profiles().into_iter().find(|p| p.id == pid).map(|p| p.name).unwrap_or_default();
        let me = self.clone();
        confirm(&self.ctx.win, &tr!("Delete profile"), &tr!("Permanently delete profile “{name}”?", name = name),
                &tr!("Delete profile"), true, move || {
            {
                let mut st = me.ctx.state.borrow_mut();
                if st.profile.as_deref() == Some(pid.as_str()) {
                    st.doc = None;
                }
            }
            if !freestream_core::profiles::delete_profile(&pid, None) {
                return;
            }
            me.ctx.edit(|cfg| {
                cfg.section("smart_profiles").retain(|_, v| v.as_str() != Some(pid.as_str()));
                if cfg.get_str("default_profile") == pid {
                    cfg.data.remove("default_profile");
                }
            });
            let first = Self::profiles().first().map(|p| p.id.clone());
            {
                let mut st = me.ctx.state.borrow_mut();
                if st.profile.as_deref() == Some(pid.as_str()) {
                    st.profile = first;
                }
            }
            *me.selected.borrow_mut() = None;
            me.render();
        });
    }
}
