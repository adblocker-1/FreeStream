//! Einstellungen › Allgemein › Sicherung: alle Profile, Bilder, Plugins und Einstellungen in eine Datei
//! sichern oder daraus wiederherstellen (wie `_build_backup` in `freestream/settingsui.py`).

use gtk4 as gtk;
use gtk::prelude::*;
use gtk::{glib, Orientation};

use freestream_core::background::Event;
use freestream_core::backup;
use freestream_core::tr;
use std::path::PathBuf;
use std::rc::Rc;
use std::sync::mpsc::{Receiver, TryRecvError};
use std::time::Duration;

use super::settings::{card, card_title, switch_row};
use super::util::label;

/// Arbeit im Hintergrund mit Fortschritt in der Statuszeile; `done` bekommt das Ergebnis.
fn run<T: 'static>(rx: Receiver<Event<T>>, status: gtk::Label, buttons: Vec<gtk::Button>, text: String, done: impl Fn(T) + 'static) {
    for b in &buttons {
        b.set_sensitive(false);
    }
    status.set_text(&text);
    glib::timeout_add_local(Duration::from_millis(150), move || loop {
        match rx.try_recv() {
            Ok(Event::Progress(n, total)) => {
                let pct = if total > 0 { 100 * n / total } else { 100 };
                status.set_text(&format!("{text} {pct} %"));
            }
            Ok(Event::Done(v)) => {
                for b in &buttons {
                    b.set_sensitive(true);
                }
                done(v);
                return glib::ControlFlow::Break;
            }
            Err(TryRecvError::Empty) => return glib::ControlFlow::Continue,
            Err(TryRecvError::Disconnected) => return glib::ControlFlow::Break,
        }
    });
}

fn chooser(parent: Option<gtk::Window>, title: &str, save: bool) -> gtk::FileChooserNative {
    let action = if save { gtk::FileChooserAction::Save } else { gtk::FileChooserAction::Open };
    let ok = if save { tr!("Save") } else { tr!("Open") };
    let d = gtk::FileChooserNative::new(Some(title), parent.as_ref(), action, Some(&ok), Some(&tr!("Cancel")));
    let f = gtk::FileFilter::new();
    f.set_name(Some(&tr!("FreeStream backup")));
    f.add_suffix(backup::EXT.trim_start_matches('.'));
    d.add_filter(&f);
    d
}

pub fn card_widget() -> gtk::Widget {
    let outer = gtk::Box::new(Orientation::Vertical, 0);
    outer.append(&card_title(&tr!("Backup")));
    let c = card();
    c.set_spacing(12);
    let intro = label(&tr!("Save all profiles, images, plugins and settings of FreeDeck and FreeWave to one file, or restore them from such a file."), &["muted"]);
    intro.set_wrap(true);
    c.append(&intro);
    let (row, plugins) = switch_row(&tr!("Include installed plugins"), "", true);
    c.append(&row);
    let btns = gtk::Box::new(Orientation::Horizontal, 8);
    let create = gtk::Button::with_label(&tr!("Create backup…"));
    let restore = gtk::Button::with_label(&tr!("Restore…"));
    for b in [&create, &restore] {
        b.add_css_class("btn");
        btns.append(b);
    }
    c.append(&btns);
    let status = label("", &["muted"]);
    status.set_wrap(true);
    c.append(&status);
    outer.append(&c);

    let buttons = vec![create.clone(), restore.clone()];
    {
        let (status, buttons, plugins) = (status.clone(), buttons.clone(), plugins.clone());
        create.connect_clicked(move |b| {
            let parent = b.root().and_then(|r| r.downcast::<gtk::Window>().ok());
            let d = chooser(parent, &tr!("Create backup"), true);
            d.set_current_name(&backup::default_name());
            let (status, buttons, plugins) = (status.clone(), buttons.clone(), plugins.clone());
            d.connect_response(move |d, resp| {
                if resp != gtk::ResponseType::Accept {
                    return;
                }
                let Some(mut path) = d.file().and_then(|f| f.path()) else { return };
                if !path.to_string_lossy().ends_with(backup::EXT) {
                    path = PathBuf::from(format!("{}{}", path.display(), backup::EXT));
                }
                let st = status.clone();
                let p2 = path.clone();
                run(backup::create_in_background(path, plugins.is_active()), status.clone(), buttons.clone(), tr!("Creating backup…"), move |res| match res {
                    Ok(_) => {
                        let size = std::fs::metadata(&p2).map(|m| backup::human_size(m.len())).unwrap_or_default();
                        let name = p2.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
                        st.set_text(&format!("{}\n{}", tr!("Backup saved: {path}", path = name), tr!("Size: {size}", size = size)));
                    }
                    Err(e) => st.set_text(&e.to_string()),
                });
            });
            d.show();
        });
    }
    {
        let (status, buttons) = (status.clone(), buttons.clone());
        restore.connect_clicked(move |b| {
            let parent = b.root().and_then(|r| r.downcast::<gtk::Window>().ok());
            let d = chooser(parent.clone(), &tr!("Restore backup"), false);
            let (status, buttons) = (status.clone(), buttons.clone());
            d.connect_response(move |d, resp| {
                if resp != gtk::ResponseType::Accept {
                    return;
                }
                let Some(path) = d.file().and_then(|f| f.path()) else { return };
                let m = match backup::inspect(&path) {
                    Ok(m) => m,
                    Err(e) => {
                        status.set_text(&e.to_string());
                        return;
                    }
                };
                let text = format!("{}\n\n{}", backup::describe(&m).join("\n"),
                    tr!("Your current profiles and settings will be replaced. A safety copy of them is saved first. FreeDeck and FreeWave restart afterwards. Continue?"));
                let (status, buttons, path) = (status.clone(), buttons.clone(), path.clone());
                let Some(win) = parent.clone() else { return };
                confirm(&win, &tr!("Restore backup"), &text, Rc::new(move || {
                    let st = status.clone();
                    run(backup::restore_in_background(path.clone(), true), status.clone(), buttons.clone(), tr!("Restoring backup…"), move |res| match res {
                        Ok(_) => {
                            st.set_text(&tr!("Backup restored. {app} restarts now.", app = "FreeDeck"));
                            restart();
                        }
                        Err(e) => st.set_text(&e.to_string()),
                    });
                }));
            });
            d.show();
        });
    }
    outer.upcast()
}

fn confirm(parent: &gtk::Window, title: &str, text: &str, on_yes: Rc<dyn Fn()>) {
    let win = gtk::Window::builder().title(title).transient_for(parent).modal(true).resizable(false).build();
    let b = gtk::Box::new(Orientation::Vertical, 14);
    for m in [b.upcast_ref::<gtk::Widget>()] {
        m.set_margin_start(20);
        m.set_margin_end(20);
        m.set_margin_top(18);
        m.set_margin_bottom(16);
    }
    let l = label(text, &[]);
    l.set_wrap(true);
    l.set_max_width_chars(60);
    b.append(&l);
    let row = gtk::Box::new(Orientation::Horizontal, 8);
    row.set_halign(gtk::Align::End);
    let no = gtk::Button::with_label(&tr!("Cancel"));
    let yes = gtk::Button::with_label(&tr!("Restore…").trim_end_matches('…').to_string());
    yes.add_css_class("destructive-action");
    row.append(&no);
    row.append(&yes);
    b.append(&row);
    win.set_child(Some(&b));
    let w = win.clone();
    no.connect_clicked(move |_| w.close());
    let w = win.clone();
    yes.connect_clicked(move |_| {
        w.close();
        on_yes();
    });
    win.present();
}

/// Nach dem Wiederherstellen neu starten (die Profile und Einstellungen sind ausgetauscht).
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
