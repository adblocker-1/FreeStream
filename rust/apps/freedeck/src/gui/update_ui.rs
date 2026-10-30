//! Updates (Glocke oben rechts, und beim Start, wenn eingeschaltet): neue Version suchen, Notizen zeigen,
//! herunterladen und installieren – wie `freestream/updateui.py`. Netz und Installation laufen in Threads.

use gtk4 as gtk;
use gtk::prelude::*;
use gtk::{glib, Align, Orientation};

use freestream_core::background::Event;
use freestream_core::tr;
use freestream_core::updater::{self, InstallOutcome, Update};
use std::sync::mpsc::{Receiver, TryRecvError};
use std::time::Duration;

use super::editor::info;
use super::util::label;

/// Empfänger in der GTK-Hauptschleife abfragen; `on` bekommt das Ergebnis einmal.
fn poll<T: 'static>(rx: Receiver<T>, on: impl Fn(T) + 'static) {
    glib::timeout_add_local(Duration::from_millis(150), move || match rx.try_recv() {
        Ok(v) => {
            on(v);
            glib::ControlFlow::Break
        }
        Err(TryRecvError::Empty) => glib::ControlFlow::Continue,
        Err(TryRecvError::Disconnected) => glib::ControlFlow::Break,
    });
}

/// Beim Start: still nachsehen und nur bei einer neuen Version fragen.
pub fn check_at_start(win: &gtk::ApplicationWindow) {
    if !updater::enabled() || updater::running_from_source() {
        return;
    }
    let w = win.clone();
    poll(updater::check_in_background(), move |res| {
        if let Ok(Some(u)) = res {
            if !updater::suppressed(&u.version) {
                offer(&w, u, true);
            }
        }
    });
}

/// Glocke: nachsehen und das Ergebnis immer zeigen.
pub fn check_now(win: &gtk::ApplicationWindow) {
    if updater::running_from_source() {
        info(win, &tr!("Updates"), &updater::source_message("FreeDeck"));
        return;
    }
    let w = win.clone();
    poll(updater::check_in_background(), move |res| match res {
        Ok(Some(u)) => offer(&w, u, false),
        Ok(None) => info(&w, &tr!("Updates"), &updater::latest_message()),
        Err(e) => info(&w, &tr!("Updates"), &e.check_message()),
    });
}

/// Dialog „Update verfügbar“: Notizen, „Diese Version überspringen“, „Später“, „Update installieren“.
fn offer(parent: &gtk::ApplicationWindow, u: Update, automatic: bool) {
    let mut lock = updater::PromptLock::new();
    if automatic && !lock.acquire() {
        return; // ein anderes Programm fragt gerade
    }
    let win = gtk::Window::builder().title(tr!("Update available")).transient_for(parent).modal(true).default_width(520).build();
    let b = gtk::Box::new(Orientation::Vertical, 10);
    for m in [b.upcast_ref::<gtk::Widget>()] {
        m.set_margin_start(20);
        m.set_margin_end(20);
        m.set_margin_top(18);
        m.set_margin_bottom(16);
    }
    b.append(&label(&tr!("Update available"), &["insp-name"]));
    b.append(&label(&format!("FreeDeck {} · {}", u.version, u.date), &["muted"]));
    let notes = gtk::TextView::new();
    notes.set_editable(false);
    notes.set_wrap_mode(gtk::WrapMode::WordChar);
    notes.buffer().set_text(&u.notes);
    let sc = gtk::ScrolledWindow::new();
    sc.set_child(Some(&notes));
    sc.set_min_content_height(220);
    sc.set_vexpand(true);
    b.append(&sc);
    let bar = gtk::ProgressBar::new();
    bar.set_visible(false);
    b.append(&bar);
    let row = gtk::Box::new(Orientation::Horizontal, 8);
    let skip = gtk::Button::with_label(&tr!("Skip this version"));
    skip.add_css_class("flat");
    row.append(&skip);
    let sp = gtk::Box::new(Orientation::Horizontal, 0);
    sp.set_hexpand(true);
    row.append(&sp);
    let later = gtk::Button::with_label(&tr!("Later"));
    let install = gtk::Button::with_label(&tr!("Install update"));
    install.add_css_class("suggested-action");
    row.append(&later);
    row.append(&install);
    row.set_halign(Align::Fill);
    b.append(&row);
    win.set_child(Some(&b));
    let lock = std::rc::Rc::new(std::cell::RefCell::new(lock));
    {
        let (w, v, l) = (win.clone(), u.version.clone(), lock.clone());
        skip.connect_clicked(move |_| {
            let _ = updater::skip_version(&v);
            l.borrow_mut().release();
            w.close();
        });
    }
    {
        let (w, v, l) = (win.clone(), u.version.clone(), lock.clone());
        later.connect_clicked(move |_| {
            let _ = updater::remind_later(&v);
            l.borrow_mut().release();
            w.close();
        });
    }
    {
        let (w, p, l) = (win.clone(), parent.clone(), lock.clone());
        install.connect_clicked(move |btn| {
            btn.set_sensitive(false);
            bar.set_visible(true);
            let rx = updater::download_in_background(u.clone());
            let (w, p, l, bar, version) = (w.clone(), p.clone(), l.clone(), bar.clone(), u.version.clone());
            glib::timeout_add_local(Duration::from_millis(100), move || loop {
                match rx.try_recv() {
                    Ok(Event::Progress(done, total)) => {
                        if total > 0 {
                            bar.set_fraction(done as f64 / total as f64);
                        } else {
                            bar.pulse();
                        }
                    }
                    Ok(Event::Done(res)) => {
                        w.close();
                        l.borrow_mut().release();
                        match res {
                            Ok(files) => run_install(&p, files, version.clone()),
                            Err(e) => info(&p, &tr!("Updates"), &e.download_message()),
                        }
                        return glib::ControlFlow::Break;
                    }
                    Err(TryRecvError::Empty) => return glib::ControlFlow::Continue,
                    Err(TryRecvError::Disconnected) => return glib::ControlFlow::Break,
                }
            });
        });
    }
    win.present();
}

fn run_install(parent: &gtk::ApplicationWindow, files: Vec<std::path::PathBuf>, version: String) {
    let p = parent.clone();
    let v = version.clone();
    poll(updater::install_in_background(files, version), move |outcome| {
        let text = outcome.message("FreeDeck", &v, false);
        match outcome {
            InstallOutcome::Installed => {
                info(&p, &tr!("Updates"), &text);
                restart();
            }
            InstallOutcome::NoPkexec(folder) => {
                info(&p, &tr!("Updates"), &text);
                let _ = std::process::Command::new("xdg-open").arg(folder).spawn();
            }
            _ => info(&p, &tr!("Updates"), &text),
        }
    });
}

/// Neu starten, sobald dieses Programm beendet ist (die neue Version liegt dann auf der Platte).
fn restart() {
    if let Ok(exe) = std::env::current_exe() {
        let pid = std::process::id();
        let script = format!("while kill -0 {pid} 2>/dev/null; do sleep 0.2; done; exec \"$0\" gui");
        let _ = std::process::Command::new("sh").arg("-c").arg(script).arg(exe).spawn();
    }
    glib::timeout_add_local_once(Duration::from_secs(2), || {
        if let Some(app) = gtk::gio::Application::default() {
            app.quit();
        }
    });
}
