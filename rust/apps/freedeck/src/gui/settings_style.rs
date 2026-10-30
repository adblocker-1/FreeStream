//! Einstellungen › Stil (wie `StylePage` in `freestream/freedecksettings.py`): Fortschritt der
//! Multi-Aktion (`multi_progress`) und die Schrift aller Tasten (`title_style`, `dial_title_style`):
//! neue Tasten bekommen sie über `catalog::set_title_defaults`, „Auf alle Tasten anwenden“ ändert alle
//! Profile mit `editdoc::apply_title_style`.

use gtk4 as gtk;
use gtk::prelude::*;
use gtk::{Align, Orientation};

use freestream_core::i18n::ngettext;
use freestream_core::{catalog, editdoc, paths, tr};
use serde_json::{json, Map, Value};
use std::cell::{Cell, RefCell};
use std::rc::Rc;

use super::settings::{button, card, confirm, form, form_row, hint, message, run_bg, section, switch_row, Ctx, Debounce};
use super::util::label;

fn key_default() -> Map<String, Value> {
    json!({"ShowTitle": true, "TitleAlignment": "bottom", "FontFamily": "", "FontSize": 12, "FontStyle": "",
           "FontUnderline": false, "TitleColor": "#ffffff"}).as_object().cloned().unwrap_or_default()
}

fn dial_default() -> Map<String, Value> {
    json!({"ShowTitle": true, "TitleColor": "#ffffff"}).as_object().cloned().unwrap_or_default()
}

/// Standardwerte, darüber die gespeicherten.
fn merged(mut base: Map<String, Value>, saved: Option<&Value>) -> Map<String, Value> {
    if let Some(m) = saved.and_then(Value::as_object) {
        for (k, v) in m {
            base.insert(k.clone(), v.clone());
        }
    }
    base
}

/// Schriftfamilien des Systems (fc-list), höchstens 300.
fn font_families() -> Vec<String> {
    let out = freestream_deck::system::run_out(&["fc-list", ":", "family"]);
    let mut fams: Vec<String> = out.lines().filter(|l| !l.trim().is_empty() && !l.starts_with('.'))
        .map(|l| l.split(',').next().unwrap_or("").trim().to_string()).filter(|f| !f.is_empty()).collect();
    fams.sort_by_key(|f| f.to_lowercase());
    fams.dedup();
    fams.truncate(300);
    fams
}

fn rgba(hex: &str) -> gtk::gdk::RGBA {
    let c = freestream_deck::render::parse_color(Some(hex), [255, 255, 255]);
    gtk::gdk::RGBA::new(c[0] as f32 / 255.0, c[1] as f32 / 255.0, c[2] as f32 / 255.0, 1.0)
}

fn hex(c: &gtk::gdk::RGBA) -> String {
    let b = |v: f32| (v.clamp(0.0, 1.0) * 255.0).round() as u8;
    format!("#{:02x}{:02x}{:02x}", b(c.red()), b(c.green()), b(c.blue()))
}

/// Vorschau einer Taste mit Symbol und Titel (runde Ecken).
fn preview_image(style: &Map<String, Value>) -> image::RgbaImage {
    let size = 96u32;
    let mut base = image::RgbaImage::from_pixel(size, size, image::Rgba([0, 0, 0, 255]));
    let g = freestream_deck::iconlib::glyph("deck", 40, [255, 255, 255, 255]);
    image::imageops::overlay(&mut base, &g, ((size - 40) / 2) as i64, 18);
    let mut img = freestream_deck::render::compose_key((size, size), None, &tr!("Title"), style, None, Some(&base));
    // runde Ecken
    let r = 16.0f32;
    for (x, y, p) in img.enumerate_pixels_mut() {
        let (fx, fy) = (x as f32 + 0.5, y as f32 + 0.5);
        let cx = if fx < r { r } else if fx > size as f32 - r { size as f32 - r } else { fx };
        let cy = if fy < r { r } else if fy > size as f32 - r { size as f32 - r } else { fy };
        let d = ((fx - cx).powi(2) + (fy - cy).powi(2)).sqrt();
        if d > r {
            p[3] = 0;
        } else if d > r - 1.0 {
            p[3] = ((r - d) * p[3] as f32) as u8;
        }
    }
    img
}

struct Page {
    ctx: Rc<Ctx>,
    key: RefCell<Map<String, Value>>,
    dial: RefCell<Map<String, Value>>,
    preview: gtk::Picture,
    save: Debounce,
    /// Felder werden gerade gesetzt (Zurücksetzen): nicht speichern
    busy: Cell<bool>,
}

impl Page {
    fn store(&self) {
        if self.busy.get() {
            return;
        }
        let (key, dial) = (self.key.borrow().clone(), self.dial.borrow().clone());
        catalog::set_title_defaults(&key);
        let ctx = self.ctx.clone();
        self.save.run(300, move || {
            ctx.edit(|cfg| {
                cfg.set("title_style", Value::Object(key));
                cfg.set("dial_title_style", Value::Object(dial));
            })
        });
        self.draw_preview();
    }

    fn set(&self, k: &str, v: Value) {
        self.key.borrow_mut().insert(k.to_string(), v);
        self.store();
    }

    fn set_dial(&self, k: &str, v: Value) {
        self.dial.borrow_mut().insert(k.to_string(), v);
        self.store();
    }

    fn draw_preview(&self) {
        self.preview.set_paintable(Some(&super::util::texture(&preview_image(&self.key.borrow()))));
    }
}

pub fn build(ctx: &Rc<Ctx>) -> gtk::Widget {
    let cfg = super::settings::cfg_read();
    let sc = gtk::ScrolledWindow::new();
    sc.set_hscrollbar_policy(gtk::PolicyType::Never);
    let b = gtk::Box::new(Orientation::Vertical, 0);
    b.set_margin_end(4);
    sc.set_child(Some(&b));

    // Multi-Aktion
    let c = card();
    c.set_margin_bottom(14);
    c.append(&section(&tr!("Multi Action")));
    let (row, progress) = switch_row(&tr!("Show progress on the key while it runs"), "",
                                     cfg.get("multi_progress").and_then(Value::as_bool).unwrap_or(true));
    row.set_margin_top(10);
    c.append(&row);
    let ctx2 = ctx.clone();
    progress.connect_active_notify(move |s| {
        let on = s.is_active();
        ctx2.edit(|cfg| cfg.set("multi_progress", Value::Bool(on)));
    });
    b.append(&c);

    // Schrift
    let c = card();
    c.append(&section(&tr!("Font for all keys")));
    let cols = gtk::Box::new(Orientation::Horizontal, 16);
    cols.set_margin_top(10);
    c.append(&cols);
    let grid = gtk::Box::new(Orientation::Vertical, 0);
    grid.set_hexpand(true);
    cols.append(&grid);
    let prev = gtk::Box::new(Orientation::Vertical, 6);
    prev.set_valign(Align::Start);
    let picture = gtk::Picture::new();
    picture.set_size_request(96, 96);
    picture.set_can_shrink(false);
    prev.append(&picture);
    let pl = label(&tr!("Preview"), &["muted", "small"]);
    pl.set_halign(Align::Center);
    prev.append(&pl);
    cols.append(&prev);
    b.append(&c);

    let page = Rc::new(Page {
        ctx: ctx.clone(),
        key: RefCell::new(merged(key_default(), cfg.get("title_style"))),
        dial: RefCell::new(merged(dial_default(), cfg.get("dial_title_style"))),
        preview: picture,
        save: Debounce::default(),
        busy: Cell::new(false),
    });
    let key = page.key.borrow().clone();
    let dial = page.dial.borrow().clone();
    let g = form();
    grid.append(&g);

    // Tasten: Titel zeigen, Position
    let r = gtk::Box::new(Orientation::Horizontal, 10);
    let show = gtk::Switch::new();
    show.set_active(key.get("ShowTitle").and_then(Value::as_bool).unwrap_or(true));
    show.set_valign(Align::Center);
    r.append(&show);
    r.append(&label(&tr!("Show title"), &[]));
    let align = gtk::Box::new(Orientation::Horizontal, 0);
    align.add_css_class("tabs");
    align.set_margin_start(8);
    let cur_align = key.get("TitleAlignment").and_then(Value::as_str).unwrap_or("bottom").to_string();
    let mut align_btns: Vec<(&'static str, gtk::ToggleButton)> = Vec::new();
    for (k, t) in [("top", tr!("top")), ("middle", tr!("middle")), ("bottom", tr!("bottom"))] {
        let tb = gtk::ToggleButton::with_label(&t);
        tb.add_css_class("tab");
        tb.add_css_class("seg");
        if let Some((_, f)) = align_btns.first() {
            tb.set_group(Some(f));
        }
        tb.set_active(k == cur_align);
        let p = page.clone();
        tb.connect_toggled(move |tb| {
            if tb.is_active() {
                p.set("TitleAlignment", Value::from(k));
            }
        });
        align.append(&tb);
        align_btns.push((k, tb));
    }
    r.append(&align);
    form_row(&g, 0, &tr!("Keys"), &r);
    let p = page.clone();
    show.connect_active_notify(move |s| p.set("ShowTitle", Value::Bool(s.is_active())));

    // Schrift und Größe
    let r = gtk::Box::new(Orientation::Horizontal, 8);
    let fam_cur = key.get("FontFamily").and_then(Value::as_str).unwrap_or("").to_string();
    let fams: Rc<RefCell<Vec<String>>> = Rc::new(RefCell::new(if fam_cur.is_empty() { vec![] } else { vec![fam_cur.clone()] }));
    let fam_names = |f: &[String]| -> Vec<String> { std::iter::once(tr!("Standard")).chain(f.iter().cloned()).collect() };
    let family = super::settings::dropdown(&fam_names(&fams.borrow()));
    family.set_enable_search(true);
    family.set_selected(if fam_cur.is_empty() { 0 } else { 1 });
    r.append(&family);
    let size = gtk::SpinButton::with_range(6.0, 40.0, 1.0);
    size.set_value(key.get("FontSize").and_then(Value::as_f64).unwrap_or(12.0));
    r.append(&size);
    r.append(&label("px", &["muted", "small"]));
    form_row(&g, 1, &tr!("Font"), &r);
    let (p, f2) = (page.clone(), fams.clone());
    family.connect_selected_notify(move |d| {
        let i = d.selected() as usize;
        let v = if i == 0 { String::new() } else { f2.borrow().get(i - 1).cloned().unwrap_or_default() };
        p.set("FontFamily", Value::from(v));
    });
    let p = page.clone();
    size.connect_value_changed(move |s| p.set("FontSize", Value::from(s.value_as_int())));
    // Schriftliste im Hintergrund (fc-list kann dauern)
    {
        let (p, family, fams) = (page.clone(), family.clone(), fams.clone());
        run_bg(font_families, move |list| {
            let cur = p.key.borrow().get("FontFamily").and_then(Value::as_str).unwrap_or("").to_string();
            p.busy.set(true);
            *fams.borrow_mut() = list.clone();
            let names: Vec<String> = std::iter::once(tr!("Standard")).chain(list.iter().cloned()).collect();
            family.set_model(Some(&gtk::StringList::new(&names.iter().map(String::as_str).collect::<Vec<_>>())));
            family.set_selected(list.iter().position(|f| *f == cur).map(|i| i as u32 + 1).unwrap_or(0));
            p.busy.set(false);
        });
    }

    // Stil (B / I / U) und Farbe
    let r = gtk::Box::new(Orientation::Horizontal, 6);
    let style = key.get("FontStyle").and_then(Value::as_str).unwrap_or("").to_lowercase();
    let toggles: Vec<(&'static str, gtk::ToggleButton)> = [("bold", "B"), ("italic", "I"), ("underline", "U")].into_iter().map(|(k, t)| {
        let tb = gtk::ToggleButton::new();
        let l = gtk::Label::new(None);
        l.set_markup(&match k {
            "bold" => format!("<b>{t}</b>"),
            "italic" => format!("<i>{t}</i>"),
            _ => format!("<u>{t}</u>"),
        });
        tb.set_child(Some(&l));
        tb.add_css_class("style-toggle");
        tb.set_active(match k {
            "bold" => style.contains("bold"),
            "italic" => style.contains("italic"),
            _ => key.get("FontUnderline").and_then(Value::as_bool).unwrap_or(false),
        });
        r.append(&tb);
        (k, tb)
    }).collect();
    for (k, tb) in &toggles {
        let (p, k, all) = (page.clone(), *k, toggles.clone());
        tb.connect_toggled(move |tb| {
            if k == "underline" {
                p.set("FontUnderline", Value::Bool(tb.is_active()));
                return;
            }
            let on = |name: &str| all.iter().any(|(n, t)| *n == name && t.is_active());
            let v = match (on("bold"), on("italic")) {
                (true, true) => "Bold Italic",
                (true, false) => "Bold",
                (false, true) => "Italic",
                _ => "",
            };
            p.set("FontStyle", Value::from(v));
        });
    }
    let cl = label(&tr!("Colour"), &["field-label"]);
    cl.set_margin_start(14);
    r.append(&cl);
    let color = gtk::ColorButton::with_rgba(&rgba(key.get("TitleColor").and_then(Value::as_str).unwrap_or("#ffffff")));
    color.set_title(&tr!("Title color"));
    color.set_use_alpha(false);
    r.append(&color);
    let p = page.clone();
    color.connect_color_set(move |c| p.set("TitleColor", Value::from(hex(&c.rgba()))));
    form_row(&g, 2, &tr!("Style"), &r);

    // Drehregler
    let r = gtk::Box::new(Orientation::Horizontal, 10);
    let dial_show = gtk::Switch::new();
    dial_show.set_active(dial.get("ShowTitle").and_then(Value::as_bool).unwrap_or(true));
    dial_show.set_valign(Align::Center);
    r.append(&dial_show);
    r.append(&label(&tr!("Show title"), &[]));
    let dial_color = gtk::ColorButton::with_rgba(&rgba(dial.get("TitleColor").and_then(Value::as_str).unwrap_or("#ffffff")));
    dial_color.set_title(&tr!("Title color"));
    dial_color.set_use_alpha(false);
    dial_color.set_margin_start(8);
    r.append(&dial_color);
    form_row(&g, 3, &tr!("Dials"), &r);
    let p = page.clone();
    dial_show.connect_active_notify(move |s| p.set_dial("ShowTitle", Value::Bool(s.is_active())));
    let p = page.clone();
    dial_color.connect_color_set(move |c| p.set_dial("TitleColor", Value::from(hex(&c.rgba()))));

    // Anwenden / Zurücksetzen
    let r = gtk::Box::new(Orientation::Horizontal, 8);
    let apply = button(&tr!("Apply to all keys"), None, "primary");
    let reset = button(&tr!("Reset"), None, "");
    r.append(&apply);
    r.append(&reset);
    r.set_margin_top(6);
    form_row(&g, 4, "", &r);
    let h = hint(&tr!("New keys get this font automatically. “Apply to all keys” also changes the keys in all your profiles."));
    h.set_max_width_chars(60);
    g.attach(&h, 1, 5, 1, 1);

    let p = page.clone();
    reset.connect_clicked(move |_| {
        p.busy.set(true);
        *p.key.borrow_mut() = key_default();
        *p.dial.borrow_mut() = dial_default();
        show.set_active(true);
        for (k, tb) in &align_btns {
            tb.set_active(*k == "bottom");
        }
        family.set_selected(0);
        size.set_value(12.0);
        for (_, tb) in &toggles {
            tb.set_active(false);
        }
        color.set_rgba(&rgba("#ffffff"));
        dial_show.set_active(true);
        dial_color.set_rgba(&rgba("#ffffff"));
        p.busy.set(false);
        p.store();
    });
    let p = page.clone();
    apply.connect_clicked(move |_| {
        let p2 = p.clone();
        confirm(&p.ctx.win, &tr!("Apply to all keys"), &tr!("Change title font, position and colour of all keys in all profiles?"),
                &tr!("Apply to all keys"), false, move || {
            // offenes Profil zuerst speichern; der Editor liest es danach neu
            if let Some(doc) = p2.ctx.state.borrow_mut().doc.as_mut() {
                if let Err(e) = doc.save() {
                    log::warn!("{e}");
                }
            }
            let (key, dial) = (p2.key.borrow().clone(), p2.dial.borrow().clone());
            let p3 = p2.clone();
            run_bg(move || editdoc::apply_title_style(&paths::profiles_dir(), &key, &dial), move |n| {
                message(&p3.ctx.win, &tr!("Apply to all keys"),
                        &ngettext("{n} key changed.", "{n} keys changed.", n as i64).replace("{n}", &n.to_string()));
            });
        });
    });
    page.draw_preview();
    // Standardwerte für neue Tasten schon jetzt (auch ohne Änderung)
    catalog::set_title_defaults(&page.key.borrow());
    sc.upcast()
}
