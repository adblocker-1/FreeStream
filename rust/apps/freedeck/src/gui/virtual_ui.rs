//! Fenster des virtuellen Decks: die Tasten des Speicher-Decks „VIRTUAL“ zum Anklicken, darunter das
//! Profil mit ‹ › zum Wechseln (wie `freestream/virtualui.py`, schwebendes Fenster).

use gtk4 as gtk;
use gtk::prelude::*;
use gtk::{glib, Orientation};

use freestream_core::config::Config;
use freestream_core::models::model_geometry;
use freestream_core::tr;
use freestream_deck::daemon::Daemon;
use freestream_deck::device::{Event, MemoryDeck};
use freestream_deck::virtual_deck::{self, SERIAL};
use std::cell::RefCell;
use std::rc::Rc;
use std::sync::{Arc, Mutex};

use super::util::{label, texture};

const TILE: i32 = 72;

thread_local!(static WINDOW: RefCell<Option<gtk::Window>> = const { RefCell::new(None) });

/// Beim Start: virtuelles Deck zeigen, wenn es eingeschaltet ist.
pub fn start_if_enabled(app: &gtk::Application, daemon: &Arc<Mutex<Daemon>>) {
    let s = virtual_deck::settings(&Config::load().data);
    if s.enabled {
        show(app, daemon);
    }
}

/// Virtuelles Deck starten (falls nötig) und sein Fenster zeigen.
pub fn show(app: &gtk::Application, daemon: &Arc<Mutex<Daemon>>) {
    if let Some(w) = WINDOW.with(|w| w.borrow().clone()) {
        w.present();
        return;
    }
    let s = virtual_deck::settings(&Config::load().data);
    let dev = virtual_deck::start(&mut daemon.lock().unwrap(), &s);
    let g = model_geometry(&dev.model_code).unwrap_or(freestream_core::models::Geometry::new(4, 2));

    let win = gtk::Window::builder().application(app).title(tr!("Virtual deck")).resizable(false).build();
    win.add_css_class("virtual-deck");
    let outer = gtk::Box::new(Orientation::Vertical, 10);
    for m in [outer.upcast_ref::<gtk::Widget>()] {
        m.set_margin_start(12);
        m.set_margin_end(12);
        m.set_margin_top(12);
        m.set_margin_bottom(10);
    }
    win.set_child(Some(&outer));
    let grid = gtk::Grid::new();
    grid.set_row_spacing(10);
    grid.set_column_spacing(10);
    outer.append(&grid);
    let mut pics = Vec::new();
    for k in 0..g.keys() as u8 {
        let pic = gtk::Picture::new();
        pic.set_size_request(TILE, TILE);
        pic.set_can_shrink(true);
        pic.add_css_class("vkey");
        let click = gtk::GestureClick::new();
        let d = dev.clone();
        click.connect_pressed(move |_, _, _, _| d.push(Event::KeyDown(k)));
        let d = dev.clone();
        click.connect_released(move |_, _, _, _| d.push(Event::KeyUp(k)));
        // Maustaste außerhalb losgelassen: trotzdem loslassen
        let d = dev.clone();
        click.connect_cancel(move |_, _| d.push(Event::KeyUp(k)));
        pic.add_controller(click);
        grid.attach(&pic, (k % g.cols) as i32, (k / g.cols) as i32, 1, 1);
        pics.push(pic);
    }
    // unten: Profil mit ‹ ›
    let foot = gtk::Box::new(Orientation::Horizontal, 6);
    let prev = gtk::Button::with_label("‹");
    let name = label("", &["muted"]);
    name.set_hexpand(true);
    name.set_xalign(0.5);
    name.set_ellipsize(gtk::pango::EllipsizeMode::End);
    let next = gtk::Button::with_label("›");
    for b in [&prev, &next] {
        b.add_css_class("flat");
    }
    foot.append(&prev);
    foot.append(&name);
    foot.append(&next);
    outer.append(&foot);
    for (b, dir) in [(&prev, -1), (&next, 1)] {
        let d = daemon.clone();
        b.connect_clicked(move |_| {
            let c = d.lock().unwrap().controllers.lock().unwrap().get(SERIAL).cloned();
            if let Some(c) = c {
                c.lock().unwrap().rotate_profile(dir);
            }
        });
    }

    // Bilder abholen (das Speicher-Deck bekommt sie vom Dienst)
    let shown: Rc<RefCell<Vec<Option<image::RgbaImage>>>> = Rc::new(RefCell::new(vec![None; pics.len()]));
    let (d2, daemon2, win2) = (dev.clone(), daemon.clone(), win.clone());
    let tick = glib::timeout_add_local(std::time::Duration::from_millis(80), move || {
        poll(&d2, &pics, &shown);
        let pname = daemon2.lock().unwrap().controllers.lock().unwrap().get(SERIAL).cloned()
            .and_then(|c| c.lock().unwrap().profile.as_ref().map(|p| p.name.clone()))
            .unwrap_or_else(|| tr!("No profile"));
        if name.text() != pname {
            name.set_text(&pname);
        }
        if !win2.is_visible() {
            return glib::ControlFlow::Break;
        }
        glib::ControlFlow::Continue
    });
    let tick = RefCell::new(Some(tick));
    win.connect_close_request(move |_| {
        if let Some(t) = tick.borrow_mut().take() {
            t.remove();
        }
        WINDOW.with(|w| *w.borrow_mut() = None);
        glib::Propagation::Proceed
    });
    WINDOW.with(|w| *w.borrow_mut() = Some(win.clone()));
    win.present();
}

fn poll(dev: &MemoryDeck, pics: &[gtk::Picture], shown: &RefCell<Vec<Option<image::RgbaImage>>>) {
    let mut shown = shown.borrow_mut();
    for (k, pic) in pics.iter().enumerate() {
        let img = dev.key_image(k as u8);
        if img != shown[k] {
            match &img {
                Some(i) => pic.set_paintable(Some(&texture(i))),
                None => pic.set_paintable(None::<&gtk::gdk::Paintable>),
            }
            shown[k] = img;
        }
    }
}

/// Einstellungen übernehmen: ein-/ausschalten, bei anderer Größe neu aufbauen.
pub fn apply(daemon: &Arc<Mutex<Daemon>>) {
    let s = virtual_deck::settings(&Config::load().data);
    let Some(app) = gtk::gio::Application::default().and_then(|a| a.downcast::<gtk::Application>().ok()) else { return };
    let running = daemon.lock().unwrap().virtual_device().map(|d| d.model_code.clone());
    let want = s.enabled.then(|| s.device_model());
    if running == want {
        return;
    }
    if let Some(w) = WINDOW.with(|w| w.borrow_mut().take()) {
        w.close();
    }
    virtual_deck::stop(&mut daemon.lock().unwrap());
    if s.enabled {
        show(&app, daemon);
    }
}
