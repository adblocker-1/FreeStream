//! Fenster von FreeWave (GTK4) wie `freestream/waveui.py`: links die Seitenleiste mit Mikrofon, den
//! Ansichten (Mixer, Audio Flow, Soundboard, Mikrofon, Effekte, Apps) und den Mixen; rechts die gewählte Ansicht. Im Mixer hat
//! jeder Kanal eine Zeile, jeder Mix eine Spalte mit eigenem Regler – samt Pegel, Stumm-Knöpfen,
//! gekoppelten Reglern und Effekten.
//!
//! Alle Zugriffe auf PipeWire laufen über den [`Controller`] (Arbeits-Thread); das Fenster liest nur
//! dessen fertigen [`Snapshot`] und zeichnet neu, wenn sich dessen Version ändert. Regler, die gerade
//! bewegt wurden, überschreibt die Aktualisierung 2,5 s lang nicht (wie `_held` in Python).

mod apps;
mod deck_ui;
mod dialogs;
mod effects_ui;
mod flow;
mod mic;
mod mixer;
mod settings;
mod setup;
mod soundboard;
mod theme;
mod views;
mod widgets;

use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use gtk::prelude::*;
use gtk::Align;
use gtk4 as gtk;

use freestream_core::i18n::ngettext;
use freestream_core::{paths, tr};
use freestream_wave::control::{MixView, Snapshot};
use freestream_wave::host::{clear_app_info, write_app_info};
use freestream_wave::wavemic::WAVE_APP_FILE;
use freestream_wave::{Controller, WaveHost};
use serde_json::{Map, Value};

use widgets::{button, hbox, label, set_button_text, vbox, InfoCard, NavItem};

pub const APP_ID: &str = "io.github.freestream.FreeWave";
/// so lange überschreibt eine Aktualisierung einen gerade bewegten Regler nicht
const TOUCH_HOLD: Duration = Duration::from_millis(2500);
const POLL_MS: u64 = 50;
const LEVEL_MS: u64 = 50; // 20 Hz

/// Gibt es eine grafische Oberfläche?
pub fn display_available() -> bool {
    ["DISPLAY", "WAYLAND_DISPLAY"].iter().any(|v| std::env::var(v).is_ok_and(|s| !s.is_empty()))
}

/// Ist FreeDeck installiert (Knopf „Export to FreeDeck“)?
pub fn has_freedeck() -> bool {
    let path = std::env::var("PATH").unwrap_or_default();
    ["freedeck", "freedeck-rs"].iter().any(|n| path.split(':').any(|d| std::path::Path::new(d).join(n).is_file()))
}

/// Fenster starten; der Hintergrundteil (Mixer einrichten, Programme verteilen …) läuft im selben Prozess.
pub fn run() {
    let app = gtk::Application::builder().application_id(APP_ID).flags(gtk::gio::ApplicationFlags::NON_UNIQUE).build();
    gtk::glib::set_application_name("FreeWave");
    gtk::glib::set_prgname(Some("freewave"));
    let host = Arc::new(WaveHost::system());
    let dir = paths::state_dir();
    let _ = std::fs::create_dir_all(&dir);
    write_app_info(&dir, true, WAVE_APP_FILE);
    let stop = Arc::new(AtomicBool::new(false));
    {
        let (host, stop) = (host.clone(), stop.clone());
        let _ = std::thread::Builder::new().name("freewave".into()).spawn(move || {
            host.start();
            host.run_loop(&stop);
        });
    }
    app.connect_startup(|_| theme::install_css());
    let h = host.clone();
    let s = stop.clone();
    app.connect_activate(move |app| {
        if app.active_window().is_some() {
            return;
        }
        let w = Win::build(app, h.clone(), s.clone());
        w.win.present();
        // FreeWave läuft weiter, auch wenn das Fenster zu ist (wie mit dem Symbol im Systembereich)
        std::mem::forget(app.hold());
    });
    app.run_with_args::<&str>(&[]);
    stop.store(true, Ordering::SeqCst);
    host.shutdown();
    host.cleanup();
    clear_app_info(&dir, WAVE_APP_FILE);
}

/// Aufgabe im Hintergrund, Ergebnis im GTK-Thread (für Abfragen, die pactl brauchen).
pub fn bg<T: Send + 'static>(work: impl FnOnce() -> T + Send + 'static, done: impl FnOnce(T) + 'static) {
    gtk::glib::MainContext::default().spawn_local(async move {
        if let Ok(v) = gtk::gio::spawn_blocking(work).await {
            done(v);
        }
    });
}

/// Neustart von FreeWave (Sprache geändert): das neue startet, sobald dieses beendet ist.
pub fn restart(app: &gtk::Application) {
    if let Ok(exe) = std::env::current_exe() {
        let pid = std::process::id();
        let script = format!("while kill -0 {pid} 2>/dev/null; do sleep 0.2; done; exec \"$0\"");
        let _ = std::process::Command::new("sh").arg("-c").arg(script).arg(exe).spawn();
    }
    app.quit();
}

/// Desktop-Benachrichtigung (notify-send), falls vorhanden.
fn notify(title: &str, body: &str) {
    let _ = std::process::Command::new("notify-send").args(["-a", "FreeWave", "-i", "audio-volume-high", title, body])
        .stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null()).spawn();
}

static SIG_SHOW: AtomicBool = AtomicBool::new(false);
static SIG_QUIT: AtomicBool = AtomicBool::new(false);
static SIG_RELOAD: AtomicBool = AtomicBool::new(false);

extern "C" fn on_signal(sig: libc::c_int) {
    match sig {
        libc::SIGUSR1 => SIG_SHOW.store(true, Ordering::SeqCst),
        libc::SIGHUP => SIG_RELOAD.store(true, Ordering::SeqCst),
        _ => SIG_QUIT.store(true, Ordering::SeqCst),
    }
}

type Job = Box<dyn FnOnce(&WaveHost) -> freestream_wave::Result<()> + Send>;

/// Das Fenster mit allen Ansichten.
pub struct Win {
    pub app: gtk::Application,
    pub win: gtk::ApplicationWindow,
    pub ctl: Arc<Controller>,
    pub snap: RefCell<Arc<Snapshot>>,
    /// Abschnitt "mixer" aus config.json (Kopie, bei jeder neuen Snapshot-Version gelesen)
    pub conf: RefCell<Map<String, Value>>,
    touched: RefCell<HashMap<String, Instant>>,
    pending: RefCell<HashMap<String, Job>>,
    pub view: RefCell<String>,
    // Kopf
    title: gtk::Label,
    sub: gtk::Label,
    head_buttons: gtk::Box,
    listen_btn: gtk::Button,
    scene_btn: gtk::Button,
    deck_btn: gtk::Button,
    setup_btn: gtk::Button,
    add_btn: gtk::Button,
    clear_btn: gtk::Button,
    head_shown: RefCell<Vec<&'static str>>,
    status: gtk::Label,
    // Seitenleiste
    nav: Vec<(&'static str, NavItem)>,
    mic_card: InfoCard,
    mix_list: gtk::Box,
    mix_cards: RefCell<HashMap<String, InfoCard>>,
    mix_card_sig: RefCell<Option<String>>,
    // Ansichten
    stack: gtk::Stack,
    pub mixer: mixer::MixerView,
    pub mic: mic::MicView,
    pub views: views::Views,
    pub flow: flow::FlowView,
    pub sb: soundboard::SoundView,
    hidden_once: Cell<bool>,
    pub wizard_open: Cell<bool>,
    deck_sig: RefCell<Option<String>>,
    pub listen_now: RefCell<Option<String>>,
}

impl Win {
    fn build(app: &gtk::Application, host: Arc<WaveHost>, stop: Arc<AtomicBool>) -> Rc<Self> {
        let win = gtk::ApplicationWindow::builder().application(app).title("FreeWave").default_width(1280).default_height(800).build();
        win.set_size_request(1100, 720);
        // Symbol aus dem Paket (freewave-rs.svg), sonst das der Python-Version
        let icon = ["freewave-rs", "freewave"].into_iter()
            .find(|n| gtk::gdk::Display::default().is_some_and(|d| gtk::IconTheme::for_display(&d).has_icon(n)))
            .unwrap_or("freewave-rs");
        gtk::Window::set_default_icon_name(icon);
        let ctl = Arc::new(Controller::new(host));
        let root = hbox(0);
        win.set_child(Some(&root));

        // ---------------------------------------------------------- Seitenleiste
        let side = vbox(0);
        side.add_css_class("side");
        side.set_size_request(236, -1);
        side.set_hexpand(false); // Inhalt dehnt sich, die Seitenleiste nicht
        root.append(&side);
        let top = vbox(0);
        widgets::margins(&top, 16, 14, 16, 14);
        top.set_vexpand(true);
        side.append(&top);
        let brand = hbox(8);
        widgets::margins(&brand, 0, 4, 14, 4);
        brand.append(&theme::mark(24));
        brand.append(&label("FreeWave", &["brand"]));
        top.append(&brand);
        let section = |t: &str| {
            let l = label(t, &["side-section"]);
            widgets::margins(&l, 14, 8, 4, 8);
            l
        };
        top.append(&section(&tr!("Wave devices")));
        let mic_card = InfoCard::new(&tr!("Microphone"), &tr!("Looking for microphone …"),
                                     theme::tile("mic", 30, theme::FG, 0.2, theme::SIDE));
        mic_card.button.set_margin_bottom(6);
        top.append(&mic_card.button);
        let line = widgets::sep();
        widgets::margins(&line, 8, 6, 4, 6);
        top.append(&line);
        let mut nav = Vec::new();
        for (key, text, icon) in [("mixer", tr!("Mixer"), "sliders"), ("flow", tr!("Audio Flow"), "flow"),
                                  ("soundboard", tr!("Soundboard"), "wave"), ("mic", tr!("Microphone"), "mic"),
                                  ("effects", tr!("Effects"), "spark"), ("apps", tr!("Apps"), "grid")] {
            let item = NavItem::new(&text, icon, false);
            item.button.set_margin_bottom(2);
            top.append(&item.button);
            nav.push((key, item));
        }
        top.append(&section(&tr!("Mixes")));
        let mix_list = vbox(2);
        top.append(&mix_list);
        let bottom = vbox(0);
        widgets::margins(&bottom, 14, 14, 14, 14);
        let settings_item = NavItem::new(&tr!("Settings"), "gear", false);
        bottom.append(&settings_item.button);
        side.append(&bottom);

        // ---------------------------------------------------------- Kopf und Inhalt
        let main = vbox(0);
        main.add_css_class("main");
        main.set_hexpand(true);
        root.append(&main);
        let head = hbox(8);
        widgets::margins(&head, 16, 24, 16, 24);
        let left = vbox(0);
        left.set_hexpand(true);
        let title = label(&tr!("Mixer"), &["title"]);
        let sub = label("", &["sub", "small"]);
        left.append(&title);
        left.append(&sub);
        head.append(&left);
        let head_buttons = hbox(8);
        head_buttons.set_valign(Align::Center);
        head.append(&head_buttons);
        main.append(&head);
        let listen_btn = button("–", Some("headphones"), "");
        listen_btn.add_css_class("tall");
        let scene_btn = button(&tr!("Scenes"), Some("layers"), "");
        let deck_btn = button(&tr!("Export to FreeDeck"), Some("export"), "");
        let setup_btn = button(&tr!("Set up mixer"), Some("wave"), "primary");
        let add_btn = button(&tr!("Add channel"), Some("plus"), "primary");
        let clear_btn = button(&tr!("Clear selection"), Some("close"), "");
        let stack = gtk::Stack::new();
        stack.set_vexpand(true);
        stack.set_transition_type(gtk::StackTransitionType::None);
        main.append(&stack);
        let status = label("", &["status"]);
        widgets::margins(&status, 0, 22, 6, 22);
        status.set_ellipsize(gtk::pango::EllipsizeMode::End);
        main.append(&status);

        let w = Rc::new(Self {
            app: app.clone(),
            win,
            ctl,
            snap: RefCell::new(Arc::new(Snapshot::default())),
            conf: RefCell::new(Map::new()),
            touched: RefCell::new(HashMap::new()),
            pending: RefCell::new(HashMap::new()),
            view: RefCell::new("mixer".into()),
            title,
            sub,
            head_buttons,
            listen_btn,
            scene_btn,
            deck_btn,
            setup_btn,
            add_btn,
            clear_btn,
            head_shown: RefCell::new(vec!["-"]),
            status,
            nav,
            mic_card,
            mix_list,
            mix_cards: RefCell::new(HashMap::new()),
            mix_card_sig: RefCell::new(None),
            stack,
            mixer: mixer::MixerView::new(),
            mic: mic::MicView::new(),
            views: views::Views::new(),
            flow: flow::FlowView::new(),
            sb: soundboard::SoundView::new(),
            hidden_once: Cell::new(false),
            wizard_open: Cell::new(false),
            deck_sig: RefCell::new(None),
            listen_now: RefCell::new(None),
        });
        w.stack.add_named(&w.mixer.root, Some("mixer"));
        w.stack.add_named(&w.flow.root, Some("flow"));
        w.stack.add_named(&w.sb.root, Some("soundboard"));
        w.stack.add_named(&w.mic.root, Some("mic"));
        w.stack.add_named(&w.views.fx_root, Some("effects"));
        w.stack.add_named(&w.views.apps_root, Some("apps"));
        mixer::init(&w);
        mic::init(&w);
        flow::init(&w);
        soundboard::init(&w);
        w.fill_mix_cards(&[]);
        w.connect(settings_item, stop);
        w.show_view("mixer");
        w
    }

    fn connect(self: &Rc<Self>, settings_item: NavItem, stop: Arc<AtomicBool>) {
        for (key, item) in &self.nav {
            let (w, k) = (self.clone(), key.to_string());
            item.button.connect_clicked(move |_| w.show_view(&k));
        }
        let w = self.clone();
        self.mic_card.button.connect_clicked(move |_| w.show_view("mic"));
        let w = self.clone();
        settings_item.button.connect_clicked(move |_| settings::open(&w));
        let w = self.clone();
        self.listen_btn.connect_clicked(move |b| w.output_menu(b));
        let w = self.clone();
        self.scene_btn.connect_clicked(move |b| dialogs::scene_menu(&w, b));
        let w = self.clone();
        self.deck_btn.connect_clicked(move |_| deck_ui::open(&w));
        let w = self.clone();
        self.setup_btn.connect_clicked(move |_| w.mixer_enable(true));
        let w = self.clone();
        self.add_btn.connect_clicked(move |_| dialogs::channel_dialog(&w, None));
        let w = self.clone();
        self.clear_btn.connect_clicked(move |_| flow::select(&w, None));
        // Schließen versteckt nur: Mixer und Mikrofon bleiben aktiv
        let w = self.clone();
        self.win.connect_close_request(move |win| {
            win.set_visible(false);
            w.ctl.set_meters(false);
            if !w.hidden_once.replace(true) {
                notify(&tr!("FreeWave is still running"),
                       &tr!("Mixer and microphone stay active. Open it again from the “FreeWave” menu."));
            }
            gtk::glib::Propagation::Stop
        });
        // Signale: SIGUSR1 = Fenster zeigen (zweiter Start), SIGTERM/SIGINT = beenden, SIGHUP = neu laden –
        // der Handler setzt nur Merker, der Takt unten wertet sie aus
        // SAFETY: die Handler setzen nur Atomics (async-signal-safe)
        unsafe {
            for sig in [libc::SIGUSR1, libc::SIGTERM, libc::SIGINT, libc::SIGHUP] {
                libc::signal(sig, on_signal as extern "C" fn(libc::c_int) as libc::sighandler_t);
            }
        }
        let (w, stop) = (self.clone(), stop.clone());
        gtk::glib::timeout_add_local(Duration::from_millis(100), move || {
            if SIG_SHOW.swap(false, Ordering::SeqCst) {
                w.show();
            }
            if SIG_RELOAD.swap(false, Ordering::SeqCst) {
                w.ctl.host().request_reload();
            }
            if SIG_QUIT.load(Ordering::SeqCst) {
                stop.store(true, Ordering::SeqCst);
                w.app.quit();
                return gtk::glib::ControlFlow::Break;
            }
            gtk::glib::ControlFlow::Continue
        });
        // Takt: neuer Zustand, Fehler, Breite der Mix-Spalten
        let w = self.clone();
        gtk::glib::timeout_add_local(Duration::from_millis(POLL_MS), move || {
            w.poll();
            gtk::glib::ControlFlow::Continue
        });
        let w = self.clone();
        gtk::glib::timeout_add_local(Duration::from_millis(LEVEL_MS), move || {
            w.tick_levels();
            gtk::glib::ControlFlow::Continue
        });
        mic::start_ticks(self);
        // erster Start: Einrichtungsassistent
        let w = self.clone();
        gtk::glib::timeout_add_local_once(Duration::from_millis(500), move || w.maybe_setup());
    }

    // ============================================================ Fenster
    pub fn show(self: &Rc<Self>) {
        let first = !self.win.is_visible();
        self.win.set_visible(true);
        self.win.present();
        if first {
            let w = self.clone();
            gtk::glib::timeout_add_local_once(Duration::from_millis(400), move || w.maybe_setup());
        }
        self.ctl.refresh();
    }

    pub fn set_nav_count(&self, key: &str, n: usize) {
        if let Some((_, item)) = self.nav.iter().find(|(k, _)| *k == key) {
            item.set_count(n);
        }
    }

    pub fn status(&self, text: &str) {
        self.status.set_text(text);
    }

    /// Erster Start: noch nie eingerichtet und Mixer noch nicht in Betrieb.
    pub fn needs_setup(&self) -> bool {
        let done = freestream_core::config::read_file().ok()
            .and_then(|c| c.get("linewave").and_then(|l| l.get("setup_done")).map(freestream_wave::mixer::truthy))
            .unwrap_or(false);
        !done && !self.ctl.host().mixer_enabled()
    }

    fn maybe_setup(self: &Rc<Self>) {
        if self.win.is_visible() && self.needs_setup() {
            setup::open(self);
        }
    }

    pub fn show_view(self: &Rc<Self>, key: &str) {
        *self.view.borrow_mut() = key.to_string();
        for (k, item) in &self.nav {
            item.set_active(*k == key);
        }
        self.stack.set_visible_child_name(key);
        self.update_header();
        self.update_meter_state();
    }

    fn update_meter_state(&self) {
        let snap = self.snap.borrow().clone();
        let on = self.win.is_visible() && *self.view.borrow() == "mixer" && snap.active;
        self.ctl.set_meters(on);
    }

    // ============================================================ Regler entprellen
    pub fn touch(&self, key: &str) {
        self.touched.borrow_mut().insert(key.to_string(), Instant::now());
    }

    pub fn held(&self, key: &str) -> bool {
        self.touched.borrow().get(key).is_some_and(|t| t.elapsed() < TOUCH_HOLD)
    }

    pub fn touched_remove(&self, key: &str) {
        self.touched.borrow_mut().remove(key);
    }

    /// Mikrofon-Karte in der Seitenleiste.
    pub fn mic_card_set(&self, title: &str, sub: &str, dot: &str, dim: bool) {
        self.mic_card.set(title, sub, dot, dim);
    }

    pub fn release_all(&self) {
        self.touched.borrow_mut().clear();
    }

    /// Aktion verzögert ausführen; schnelle Folgen fassen sich zusammen (es gilt die letzte).
    pub fn debounced(self: &Rc<Self>, key: &str, delay_ms: u64, job: impl FnOnce(&WaveHost) -> freestream_wave::Result<()> + Send + 'static) {
        self.touch(key);
        let first = self.pending.borrow_mut().insert(key.to_string(), Box::new(job)).is_none();
        if first {
            let (w, k) = (self.clone(), key.to_string());
            gtk::glib::timeout_add_local_once(Duration::from_millis(delay_ms), move || {
                if let Some(job) = w.pending.borrow_mut().remove(&k) {
                    w.ctl.act_host(job);
                }
            });
        }
    }

    // ============================================================ Takt
    fn poll(self: &Rc<Self>) {
        for e in self.ctl.take_errors() {
            self.status(&tr!("Error: {error}", error = e));
        }
        let s = self.ctl.snapshot();
        if s.version != self.snap.borrow().version {
            *self.snap.borrow_mut() = s.clone();
            *self.conf.borrow_mut() = self.ctl.host().mixer.conf();
            self.apply_state(&s);
        }
        mixer::check_width(self);
        flow::check_width(self);
    }

    fn tick_levels(self: &Rc<Self>) {
        let show = self.win.is_visible() && *self.view.borrow() == "mixer" && self.snap.borrow().active;
        if !show {
            return;
        }
        let lv = self.ctl.levels();
        mixer::set_meters(self, &lv);
    }

    fn apply_state(self: &Rc<Self>, s: &Arc<Snapshot>) {
        if s.active {
            mixer::apply(self, s);
            let sig = s.mixes.iter().map(|m| format!("{}|{}|{}|{}", m.mix.id, m.mix.name, m.mix.icon, m.mix.color)).collect::<Vec<_>>().join(";");
            if self.mix_card_sig.borrow().as_deref() != Some(&sig) {
                *self.mix_card_sig.borrow_mut() = Some(sig);
                self.fill_mix_cards(&s.mixes);
            }
            let listen = if self.held("listen") { self.listen_now.borrow().clone().unwrap_or_else(|| s.listening.clone()) } else { s.listening.clone() };
            for m in &s.mixes {
                if let Some(card) = self.mix_cards.borrow().get(&m.mix.id) {
                    let mut sub = self.mix_sub(m);
                    if m.mix.id == listen && listen != "monitor" {
                        sub = format!("{} · {sub}", tr!("listening"));
                    }
                    card.set(&m.mix.name, &sub, "", false);
                }
            }
            if !self.held("output") {
                let cur = s.outputs.iter().find(|d| d.name == s.monitor_output).map(|d| d.description.clone())
                    .unwrap_or_else(|| s.monitor_output.clone());
                set_button_text(&self.listen_btn, if cur.is_empty() { "–" } else { &cur });
            }
            views::apply(self, s);
            flow::apply(self, s);
            soundboard::apply(self, s);
            self.sync_decks(s);
        } else {
            mixer::apply_off(self);
            views::apply_off(self);
            flow::apply_off(self);
            soundboard::apply(self, s);
            for card in self.mix_cards.borrow().values() {
                card.set_sub(&tr!("Mixer is off"), true);
            }
        }
        if !s.error.is_empty() {
            self.status(&s.error);
        }
        let active = s.active_scene.as_ref().and_then(|id| s.scenes.iter().find(|x| &x.id == id)).map(|x| x.name.clone());
        set_button_text(&self.scene_btn, &active.unwrap_or_else(|| tr!("Scenes")));
        dialogs::update_scenes(self);
        mic::apply(self, s);
        self.update_header();
        self.update_meter_state();
    }

    /// FreeWave-Profile auf dem Deck an die Kanäle anpassen (wie apply_state in Python).
    fn sync_decks(self: &Rc<Self>, s: &Snapshot) {
        if !has_freedeck() || s.channels.is_empty() {
            return;
        }
        let ids: Vec<String> = s.channels.iter().map(|c| c.channel.id.clone()).collect();
        let mix_ids: Vec<String> = s.mixes.iter().map(|m| m.mix.id.clone()).collect();
        let members: Vec<String> = s.mixes.iter().map(|m| {
            let mut ex = m.mix.exclude.clone();
            ex.sort();
            format!("{}:{}", m.mix.id, ex.join(","))
        }).collect();
        let sig = format!("{}|{}|{}", ids.join(","), mix_ids.join(","), members.join(";"));
        let last = self.deck_sig.borrow().clone();
        if last.as_deref() == Some(&sig) {
            return;
        }
        let force = last.as_ref().is_some_and(|l| l.rsplit('|').next() != sig.rsplit('|').next());
        let first = last.is_none();
        *self.deck_sig.borrow_mut() = Some(sig);
        let w = self.clone();
        bg(move || {
            let mut changed = crate::deckexport::sync(&ids, None, Some(mix_ids), force);
            if first {
                // Gerät kann angesteckt worden sein, als FreeWave zu war
                changed.extend(crate::deckexport::adopt_new_devices(None, None));
            }
            changed
        }, move |changed| {
            if !changed.is_empty() {
                w.status(&tr!("Profile “{name}” updated.", name = changed.join(", ")));
            }
        });
    }

    // ============================================================ Kopf
    /// Kopf neu ordnen (Audio Flow: „Auswahl aufheben“ ein-/ausblenden).
    pub fn refresh_header(self: &Rc<Self>) {
        self.update_header();
    }

    fn update_header(self: &Rc<Self>) {
        let s = self.snap.borrow().clone();
        let view = self.view.borrow().clone();
        let mut shown: Vec<&'static str> = Vec::new();
        match view.as_str() {
            "mixer" => {
                self.title.set_text(&tr!("Mixer"));
                if s.active {
                    self.sub.set_text(&tr!("{count} channels · {mixes} mixes · everything is live", count = s.channels.len(),
                                           mixes = s.mixes.len()));
                    shown = vec!["scene", "deck", "listen"];
                } else {
                    self.sub.set_text(&tr!("The mixer is off – set it up to give every app its own channel."));
                    shown = vec!["setup"];
                }
            }
            "flow" => {
                self.title.set_text(&tr!("Audio Flow"));
                self.sub.set_text(&tr!("Click any element to follow its signal path"));
                if !s.active {
                    shown = vec!["setup"];
                } else if self.flow.has_selection() {
                    shown = vec!["clear"];
                }
            }
            "soundboard" => {
                self.title.set_text(&tr!("Soundboard"));
                self.sub.set_text("");
            }
            "mic" => {
                self.title.set_text(&tr!("Microphone"));
                self.sub.set_text(&self.mic.name());
            }
            "effects" => {
                self.title.set_text(&tr!("Effects"));
                self.sub.set_text(&tr!("Effect chains per channel – changes are live."));
            }
            _ => {
                self.title.set_text(&tr!("Apps"));
                self.sub.set_text(&tr!("Click an app to move it to another channel"));
                if s.active {
                    shown = vec!["add"];
                }
            }
        }
        if !has_freedeck() {
            shown.retain(|k| *k != "deck");
        }
        if *self.head_shown.borrow() == shown {
            return; // nur bei einer Änderung neu anordnen (kein Flackern)
        }
        widgets::clear(&self.head_buttons);
        for k in &shown {
            let b = match *k {
                "scene" => &self.scene_btn,
                "deck" => &self.deck_btn,
                "listen" => &self.listen_btn,
                "setup" => &self.setup_btn,
                "clear" => &self.clear_btn,
                _ => &self.add_btn,
            };
            self.head_buttons.append(b);
        }
        *self.head_shown.borrow_mut() = shown;
    }

    /// Gerät zum Abhören wählen (= Gerät des Persönlichen Mixes).
    fn output_menu(self: &Rc<Self>, anchor: &gtk::Button) {
        let s = self.snap.borrow().clone();
        let cur = s.outputs.iter().find(|d| d.name == s.monitor_output).map(|d| d.description.clone()).unwrap_or_default();
        let mut items = Vec::new();
        if s.outputs.is_empty() {
            items.push(Some(widgets::MenuItem { label: tr!("No output device found"), icon: None, danger: false, action: None }));
        }
        for d in &s.outputs {
            let (w, name, desc) = (self.clone(), d.name.clone(), d.description.clone());
            items.push(widgets::item(&d.description, if d.description == cur { "check" } else { "speaker" }, move || {
                w.touch("output");
                set_button_text(&w.listen_btn, &desc);
                let (n, w2, desc2) = (name.clone(), w.clone(), desc.clone());
                w.ctl.act(move |m| m.set_monitor_output(&n).map(|_| ()));
                w2.status(&tr!("Personal Mix → {output}", output = desc2));
            }));
        }
        widgets::popup(anchor, None, items, 300);
    }

    // ============================================================ Seitenleiste: Mixe
    fn fill_mix_cards(self: &Rc<Self>, mixes: &[MixView]) {
        widgets::clear(&self.mix_list);
        self.mix_cards.borrow_mut().clear();
        let defaults;
        let list: Vec<(String, String, String, String)> = if mixes.is_empty() {
            defaults = vec![("monitor".to_string(), tr!("Personal Mix"), "headphones".to_string(), theme::MONITOR.to_string()),
                            ("stream".to_string(), tr!("Stream Mix"), "stream".to_string(), theme::STREAM.to_string())];
            defaults
        } else {
            mixes.iter().map(|m| (m.mix.id.clone(), m.mix.name.clone(),
                                  if m.mix.icon.is_empty() { "wave".into() } else { m.mix.icon.clone() },
                                  if m.mix.color.is_empty() { theme::ACCENT.into() } else { m.mix.color.clone() })).collect()
        };
        for (id, name, icon, color) in list {
            let sub = mixes.iter().find(|m| m.mix.id == id).map(|m| self.mix_sub(m)).unwrap_or_else(|| "–".into());
            let card = InfoCard::new(&name, &sub, theme::tile(&icon, 30, &color, 0.2, "#ffffff"));
            let (w, mid) = (self.clone(), id.clone());
            card.button.connect_clicked(move |_| dialogs::edit_mix_id(&w, &mid));
            let (w, mid, b) = (self.clone(), id.clone(), card.button.clone());
            widgets::on_right_click(&card.button, move |x, y| dialogs::mix_menu(&w, &mid, &b, x, y));
            self.mix_list.append(&card.button);
            self.mix_cards.borrow_mut().insert(id, card);
        }
        let add = NavItem::new(&tr!("Create mix"), "plus", true);
        let w = self.clone();
        add.button.connect_clicked(move |_| dialogs::mix_dialog(&w, None));
        self.mix_list.append(&add.button);
    }

    /// Unterzeile eines Mixes: „2 Ausgänge“, „Kopfhörer“, „virtuell“ …
    pub fn mix_sub(&self, m: &MixView) -> String {
        let s = self.snap.borrow();
        let mut bits = Vec::new();
        if m.devices.len() > 1 {
            bits.push(ngettext("{n} output", "{n} outputs", m.devices.len() as i64).replace("{n}", &m.devices.len().to_string()));
        } else if let Some(d) = m.devices.first() {
            bits.push(s.outputs.iter().find(|o| &o.name == d).map(|o| o.description.clone()).unwrap_or_else(|| d.clone()));
        }
        if m.mix.virtual_ {
            bits.push(if m.mix.id == "stream" { "OBS".to_string() } else { tr!("virtual") });
        }
        if bits.is_empty() {
            tr!("no output")
        } else {
            bits.join(" · ")
        }
    }

    /// Aufgabe mit dem Host im Hintergrund; danach `done` im GTK-Thread (nur bei Erfolg), Fehler in der
    /// Statuszeile, und der Zustand wird neu gelesen.
    pub fn job<T: Send + 'static>(self: &Rc<Self>, work: impl FnOnce(&WaveHost) -> freestream_wave::Result<T> + Send + 'static,
                                  done: impl FnOnce(&Rc<Win>, T) + 'static) {
        let host = self.ctl.host().clone();
        let w = self.clone();
        bg(move || work(&host), move |r| {
            match r {
                Ok(v) => done(&w, v),
                Err(e) => w.status(&tr!("Error: {error}", error = e)),
            }
            w.ctl.refresh();
        });
    }

    pub fn mixer_enable(self: &Rc<Self>, on: bool) {
        self.status(&if on { tr!("Setting up mixer …") } else { tr!("Removing mixer …") });
        let w = self.clone();
        let host = self.ctl.host().clone();
        bg(move || host.enable_mixer(on), move |r| {
            if r.is_ok() {
                w.status(&if on { tr!("Mixer set up") } else { tr!("Mixer removed") });
            } else if let Err(e) = r {
                w.status(&tr!("Error: {error}", error = e));
            }
            w.ctl.refresh();
        });
    }
}

/// Leere Ansicht mit Hinweis (Effekte ohne Mixer usw.).
pub fn placeholder(text: &str) -> gtk::Label {
    let l = label(text, &["muted"]);
    l.set_halign(Align::Start);
    l.set_valign(Align::Start);
    // Hinweise brechen um, statt das Fenster zu verbreitern
    l.set_wrap(true);
    l.set_max_width_chars(80);
    l
}
