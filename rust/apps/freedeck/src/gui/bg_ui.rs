//! Dialog „Hintergrund“: ein Bild über alle Tasten der Seite (wie ein Desktop-Hintergrund), abdunkeln,
//! auf alle Seiten übernehmen; bei Geräten mit Drehreglern zusätzlich ein Bild für den Touchstreifen.

use gtk4 as gtk;
use gtk::prelude::*;
use gtk::{glib, Align, Orientation};

use freestream_core::i18n::ngettext;
use freestream_core::tr;
use freestream_deck::render;
use image::RgbaImage;
use std::cell::RefCell;
use std::rc::Rc;

use super::editor::{info, Editor};
use super::util::{label, texture};

/// Vorschau der Seite: kleine Tasten mit dem Bild.
fn page_preview(ed: &Rc<Editor>, page: &str) -> RgbaImage {
    let g = ed.geometry();
    let (path, dim) = ed.state.borrow().doc.as_ref().map(|d| d.background(page)).unwrap_or((None, 0));
    let ks = 34u32;
    let gap = (ks as f32 * render::BG_GAP) as u32;
    let (w, h) = (g.cols as u32 * ks + (g.cols as u32 + 1) * gap, g.rows as u32 * ks + (g.rows as u32 + 1) * gap);
    let mut img = RgbaImage::from_pixel(w, h, image::Rgba([13, 14, 16, 255]));
    let tiles = path.map(|p| render::background_tiles(&p, g.cols, g.rows, (ks, ks), dim.clamp(0, 100) as u8)).unwrap_or_default();
    for r in 0..g.rows {
        for c in 0..g.cols {
            let (x, y) = (gap + c as u32 * (ks + gap), gap + r as u32 * (ks + gap));
            let t = tiles.get(&(c, r)).cloned().unwrap_or_else(|| RgbaImage::from_pixel(ks, ks, image::Rgba([40, 41, 46, 255])));
            image::imageops::overlay(&mut img, &t, x as i64, y as i64);
        }
    }
    img
}

fn strip_preview(ed: &Rc<Editor>, page: &str) -> RgbaImage {
    let path = ed.state.borrow().doc.as_ref().and_then(|d| d.strip_background(page));
    let mut img = RgbaImage::from_pixel(240, 40, image::Rgba([0, 0, 0, 255]));
    if let Some(p) = path.and_then(|p| render::load_image(&p, 240, 40)) {
        image::imageops::overlay(&mut img, &render::fit(&p, 240, 40), 0, 0);
    }
    img
}

fn image_filter() -> gtk::FileFilter {
    let f = gtk::FileFilter::new();
    f.set_name(Some(&tr!("Images")));
    for ext in ["png", "jpg", "jpeg", "webp", "gif", "bmp"] {
        f.add_suffix(ext);
    }
    f
}

fn choose_image(win: &impl IsA<gtk::Window>, title: &str, on: impl Fn(std::path::PathBuf) + 'static) {
    let dlg = gtk::FileChooserNative::new(Some(title), Some(win), gtk::FileChooserAction::Open, Some(&tr!("Open")), Some(&tr!("Cancel")));
    dlg.add_filter(&image_filter());
    dlg.connect_response(move |d, resp| {
        if resp == gtk::ResponseType::Accept {
            if let Some(p) = d.file().and_then(|f| f.path()) {
                on(p);
            }
        }
    });
    dlg.show();
}

pub fn open(ed: &Rc<Editor>) {
    let Some(page) = ed.page_uuid() else { return };
    let dials = ed.geometry().dials > 0;
    let win = gtk::Window::builder().title(tr!("Background")).transient_for(&ed.win).modal(true).default_width(560).resizable(false).build();
    let outer = gtk::Box::new(Orientation::Vertical, 12);
    for m in [outer.upcast_ref::<gtk::Widget>()] {
        m.set_margin_start(20);
        m.set_margin_end(20);
        m.set_margin_top(18);
        m.set_margin_bottom(16);
    }
    win.set_child(Some(&outer));
    outer.append(&label(&tr!("Background image"), &["insp-name"]));
    let intro = label(&tr!("The image is spread across all keys of this page – like a desktop wallpaper. Keys with their own icon sit on top; empty keys show the image."), &["muted"]);
    intro.set_wrap(true);
    intro.set_max_width_chars(60);
    outer.append(&intro);

    // ------------------------------------------------------- Tasten
    let card = gtk::Box::new(Orientation::Horizontal, 18);
    card.add_css_class("card");
    outer.append(&card);
    let pic = gtk::Picture::new();
    pic.set_can_shrink(false);
    pic.set_valign(Align::Start);
    card.append(&pic);
    let side = gtk::Box::new(Orientation::Vertical, 8);
    side.set_hexpand(true);
    card.append(&side);
    let row = gtk::Box::new(Orientation::Horizontal, 8);
    let choose = gtk::Button::with_label(&tr!("Choose image …"));
    choose.add_css_class("suggested-action");
    let remove = gtk::Button::with_label(&tr!("Remove"));
    row.append(&choose);
    row.append(&remove);
    side.append(&row);
    let all = gtk::Button::with_label(&tr!("Apply to all pages"));
    all.set_halign(Align::Start);
    all.add_css_class("flat");
    side.append(&all);
    side.append(&label(&tr!("DIM"), &["section"]));
    let drow = gtk::Box::new(Orientation::Horizontal, 6);
    let dim = gtk::Scale::with_range(Orientation::Horizontal, 0.0, 80.0, 1.0);
    dim.set_size_request(190, -1);
    let dim_val = label("", &[]);
    drow.append(&dim);
    drow.append(&dim_val);
    side.append(&drow);
    let hint = label(&tr!("Darkens the image so titles stay easy to read."), &["muted"]);
    hint.set_wrap(true);
    side.append(&hint);

    // ------------------------------------------------- Touchstreifen
    let strip = if dials {
        let sc = gtk::Box::new(Orientation::Vertical, 10);
        sc.add_css_class("card");
        outer.append(&sc);
        sc.append(&label(&tr!("Touch strip"), &["cat-name"]));
        let t = label(&tr!("Custom image for the whole touch strip (as in the Elgato app)."), &["muted"]);
        t.set_wrap(true);
        sc.append(&t);
        let body = gtk::Box::new(Orientation::Horizontal, 18);
        sc.append(&body);
        let sp = gtk::Picture::new();
        sp.set_can_shrink(false);
        body.append(&sp);
        let sb = gtk::Box::new(Orientation::Vertical, 8);
        let sch = gtk::Button::with_label(&tr!("Choose image …"));
        let srm = gtk::Button::with_label(&tr!("Remove"));
        sb.append(&sch);
        sb.append(&srm);
        body.append(&sb);
        Some((sp, sch, srm))
    } else {
        None
    };

    let foot = gtk::Box::new(Orientation::Horizontal, 8);
    foot.set_halign(Align::End);
    let done = gtk::Button::with_label(&tr!("Done"));
    done.add_css_class("suggested-action");
    foot.append(&done);
    outer.append(&foot);
    let w = win.clone();
    done.connect_clicked(move |_| w.close());

    // ------------------------------------------------------ Anzeige
    let updating = Rc::new(RefCell::new(false));
    let refresh: Rc<dyn Fn()> = {
        let (ed, page, pic, remove, all, dim, dim_val, strip, updating) =
            (ed.clone(), page.clone(), pic.clone(), remove.clone(), all.clone(), dim.clone(), dim_val.clone(), strip.clone(), updating.clone());
        Rc::new(move || {
            pic.set_paintable(Some(&texture(&page_preview(&ed, &page))));
            let (path, d) = ed.state.borrow().doc.as_ref().map(|doc| doc.background(&page)).unwrap_or((None, 30));
            let has = path.is_some();
            remove.set_sensitive(has);
            all.set_sensitive(has);
            dim.set_sensitive(has);
            *updating.borrow_mut() = true;
            dim.set_value(if has { d as f64 } else { 30.0 });
            *updating.borrow_mut() = false;
            dim_val.set_text(&format!("{} %", if has { d } else { 30 }));
            if let Some((sp, _, srm)) = &strip {
                sp.set_paintable(Some(&texture(&strip_preview(&ed, &page))));
                srm.set_sensitive(ed.state.borrow().doc.as_ref().and_then(|doc| doc.strip_background(&page)).is_some());
            }
        })
    };
    refresh();

    // ------------------------------------------------------- Aktionen
    let apply = {
        let (ed, refresh, win) = (ed.clone(), refresh.clone(), win.clone());
        Rc::new(move |f: &dyn Fn(&mut freestream_core::editdoc::ProfileDoc) -> Result<(), String>| {
            let res = ed.state.borrow_mut().doc.as_mut().map(f);
            if let Some(Err(e)) = res {
                info(&ed.win, &tr!("Background"), &e);
            }
            ed.commit();
            refresh();
            win.present();
        })
    };
    {
        let (apply, page, win2) = (apply.clone(), page.clone(), win.clone());
        choose.connect_clicked(move |_| {
            let (apply, page) = (apply.clone(), page.clone());
            choose_image(&win2, &tr!("Choose background image"), move |p| {
                apply(&|d| d.set_background(&page, Some(&p), None, false).map_err(|e| e.to_string()));
            });
        });
    }
    {
        let (apply, page) = (apply.clone(), page.clone());
        remove.connect_clicked(move |_| apply(&|d| d.set_background(&page, None, None, false).map_err(|e| e.to_string())));
    }
    {
        let (apply, page, ed2) = (apply.clone(), page.clone(), ed.clone());
        all.connect_clicked(move |_| {
            let n = std::cell::Cell::new(0usize);
            apply(&|d| {
                let (path, dimv) = d.background(&page);
                let Some(path) = path else { return Ok(()) };
                for pid in d.page_ids() {
                    if !pid.eq_ignore_ascii_case(&page) {
                        d.set_background(&pid, Some(&path), Some(dimv), false).map_err(|e| e.to_string())?;
                        n.set(n.get() + 1);
                    }
                }
                Ok(())
            });
            ed2.state.borrow_mut().status = ngettext("Background applied to {n} more page", "Background applied to {n} more pages", n.get() as i64).replace("{n}", &n.get().to_string());
            ed2.refresh();
        });
    }
    {
        let (apply, page, dim_val) = (apply.clone(), page.clone(), dim_val.clone());
        let job: Rc<RefCell<Option<glib::SourceId>>> = Rc::new(RefCell::new(None));
        dim.connect_value_changed(move |s| {
            if *updating.borrow() {
                return;
            }
            let v = s.value().round() as i64;
            dim_val.set_text(&format!("{v} %"));
            if let Some(j) = job.borrow_mut().take() {
                j.remove();
            }
            let (apply, page, job2) = (apply.clone(), page.clone(), job.clone());
            *job.borrow_mut() = Some(glib::timeout_add_local_once(std::time::Duration::from_millis(250), move || {
                job2.borrow_mut().take();
                apply(&|d| d.set_background(&page, None, Some(v), true).map_err(|e| e.to_string()));
            }));
        });
    }
    if let Some((_, sch, srm)) = &strip {
        let (apply2, page2, win2) = (apply.clone(), page.clone(), win.clone());
        sch.connect_clicked(move |_| {
            let (apply, page) = (apply2.clone(), page2.clone());
            choose_image(&win2, &tr!("Image for the touch strip"), move |p| {
                apply(&|d| d.set_strip_background(&page, Some(&p)).map_err(|e| e.to_string()));
            });
        });
        let (apply2, page2) = (apply.clone(), page.clone());
        srm.connect_clicked(move |_| apply2(&|d| d.set_strip_background(&page2, None).map_err(|e| e.to_string())));
    }
    win.present();
}
