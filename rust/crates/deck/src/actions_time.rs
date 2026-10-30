//! Zeit-Aktionen: Elgato-Uhr (digitaltime) und Timer sowie die FreeDeck-Tasten Uhr, Countdown und Stoppuhr
//! (`freestream/actions.py` DigitalTime/Timer, `freestream/livekeys.py` Clock/Countdown/Stopwatch).
//!
//! Countdown und Stoppuhr behalten ihren Zustand pro ActionID, auch wenn die Seite gewechselt wird; der
//! Countdown-Alarm läuft über einen eigenen Thread und klingelt deshalb auch auf anderen Seiten.

use freestream_core::i18n::pgettext;
use freestream_core::tr;
use image::RgbaImage;
use serde_json::{Map, Value};
use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use crate::actions::{ActionData, Behavior, Command, Env, Feedback};
use crate::livedraw::{self as d, BG, FG, MUTED};
use crate::{render, sound, system};

const DEFAULT_ALARM: &str = "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga";

// ------------------------------------------------------------------------------------ Formate
/// Sekunden -> „4:05“ bzw. „1:02:03“.
pub fn fmt_duration(sec: f64) -> String {
    let sec = sec.max(0.0) as i64;
    let (h, rem) = (sec / 3600, sec % 3600);
    let (m, s) = (rem / 60, rem % 60);
    if h > 0 { format!("{h}:{m:02}:{s:02}") } else { format!("{m}:{s:02}") }
}

/// „5:00“, „1:30:00“, „90“, „90s“, „5m“, „1h30m“ -> Sekunden.
pub fn parse_duration(v: Option<&Value>, default: f64) -> f64 {
    let s = match v {
        Some(Value::Number(n)) => {
            let f = n.as_f64().unwrap_or(0.0);
            return if f > 0.0 { f } else { default };
        }
        Some(Value::String(s)) => s.trim().to_lowercase().replace(' ', ""),
        _ => return default,
    };
    if s.is_empty() {
        return default;
    }
    let parsed = (|| -> Option<f64> {
        if s.contains(':') {
            let mut sec = 0i64;
            for p in s.split(':') {
                sec = sec * 60 + if p.is_empty() { 0 } else { p.parse::<i64>().ok()? };
            }
            return Some(sec as f64);
        }
        if s.contains(['h', 'm', 's']) {
            let (mut total, mut num) = (0.0, String::new());
            for ch in s.chars() {
                if ch.is_ascii_digit() || ch == '.' {
                    num.push(ch);
                } else if "hms".contains(ch) && !num.is_empty() {
                    let mult = match ch { 'h' => 3600.0, 'm' => 60.0, _ => 1.0 };
                    total += num.parse::<f64>().ok()? * mult;
                    num.clear();
                } else {
                    return None;
                }
            }
            if !num.is_empty() {
                total += num.parse::<f64>().ok()?;
            }
            return Some(total);
        }
        s.parse::<f64>().ok()
    })();
    match parsed {
        Some(v) if v > 0.0 => v,
        _ => default,
    }
}

/// Dauer des Elgato-Timers (Settings.duration/time: Sekunden, Millisekunden oder „m:s“).
pub fn timer_duration(s: &Map<String, Value>) -> f64 {
    let v = s.get("duration").filter(|v| truthy(v)).or_else(|| s.get("time").filter(|v| truthy(v)));
    match v {
        None => 300.0,
        Some(Value::String(t)) if t.contains(':') => {
            let mut sec = 0i64;
            for p in t.split(':') {
                match p.trim().parse::<i64>() {
                    Ok(n) => sec = sec * 60 + n,
                    Err(_) => return 300.0,
                }
            }
            sec as f64
        }
        Some(v) => match v.as_f64().or_else(|| v.as_str()?.trim().parse().ok()) {
            Some(d) if d > 86400.0 => d / 1000.0,
            Some(d) => d,
            None => 300.0,
        },
    }
}

fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        _ => true,
    }
}

/// Anzeige des Elgato-Timers: „04:59“ bzw. „1:04:59“.
pub fn timer_text(remaining: f64) -> String {
    let r = remaining.max(0.0).round() as i64;
    let (h, rem) = (r / 3600, r % 3600);
    let (m, s) = (rem / 60, rem % 60);
    if h > 0 { format!("{h}:{m:02}:{s:02}") } else { format!("{m:02}:{s:02}") }
}

/// Uhrzeit und Datum der Elgato-Uhr: („21:05“ oder „9:05 PM“, „30.10.2026“).
pub fn digital_time(hour: i8, minute: i8, day: i8, month: i8, year: i16, twelve: bool) -> (String, String) {
    let t = if twelve {
        let h12 = if hour % 12 == 0 { 12 } else { hour % 12 };
        format!("{h12}:{minute:02} {}", if hour < 12 { "AM" } else { "PM" })
    } else {
        format!("{hour:02}:{minute:02}")
    };
    (t, format!("{day:02}.{month:02}.{year}"))
}

/// Jetzt in der Zone `tz` (leer = Ortszeit); dazu der Ortsname („Berlin“, „?“ bei unbekannter Zone).
fn now_in(tz: &str) -> (jiff::Zoned, String) {
    let now = jiff::Zoned::now();
    let tz = tz.trim();
    if tz.is_empty() {
        return (now, String::new());
    }
    match jiff::tz::TimeZone::get(tz) {
        Ok(zone) => (now.with_time_zone(zone), tz.rsplit('/').next().unwrap_or(tz).replace('_', " ")),
        Err(_) => (now, "?".into()),
    }
}

fn s_str(s: &Map<String, Value>, k: &str) -> String {
    s.get(k).and_then(Value::as_str).unwrap_or("").trim().to_string()
}

fn has_image(a: &ActionData) -> bool {
    a.state_dict().get("Image").and_then(Value::as_str).map(|s| !s.is_empty()).unwrap_or(false)
}

// ------------------------------------------------------------------------------ Elgato-Uhr
/// com.elgato.streamdeck.system.digitaltime
#[derive(Default)]
pub struct DigitalTime {
    last: Option<(String, String)>,
}

impl DigitalTime {
    fn fmt(a: &ActionData) -> (String, String) {
        let s = a.settings();
        let twelve = s.get("standardTime").map(truthy).unwrap_or(false) || s.get("24HourFormat") == Some(&Value::Bool(false));
        let (now, _) = now_in(&s_str(&s, "location"));
        digital_time(now.hour(), now.minute(), now.day(), now.month(), now.year(), twelve)
    }
}

impl Behavior for DigitalTime {
    fn image(&self, a: &ActionData, _env: &Env, size: (u32, u32)) -> Option<RgbaImage> {
        let (t, date) = Self::fmt(a);
        let light = a.settings().get("theme").and_then(Value::as_str) == Some("light");
        let (bg, fg) = if light { ([235, 235, 235], [0, 0, 0]) } else { ([0, 0, 0], [255, 255, 255]) };
        Some(render::text_key(size, &t, &date, bg, fg))
    }
    fn needs_tick(&self) -> bool {
        true
    }
    fn tick(&mut self, a: &ActionData) -> bool {
        let t = Some(Self::fmt(a));
        let changed = t != self.last;
        self.last = t;
        changed
    }
}

// ------------------------------------------------------------------------------- Elgato-Timer
/// com.elgato.streamdeck.system.timer: Drücken startet/pausiert, langer Druck setzt zurück.
pub struct Timer {
    total: Option<f64>,
    remaining: f64,
    running: bool,
    end_at: Instant,
    finished: bool,
    sound_key: String,
    alarm: String,
}

impl Default for Timer {
    fn default() -> Self {
        Self { total: None, remaining: 0.0, running: false, end_at: Instant::now(), finished: false,
               sound_key: sound::new_key("timer"), alarm: String::new() }
    }
}

impl Timer {
    fn init(&mut self, a: &ActionData) -> f64 {
        *self.total.get_or_insert_with(|| {
            let t = timer_duration(&a.settings());
            self.remaining = t;
            t
        })
    }
    fn left(&self) -> f64 {
        if self.running { self.end_at.saturating_duration_since(Instant::now()).as_secs_f64() } else { self.remaining }
    }
}

impl Behavior for Timer {
    fn key_up(&mut self, a: &ActionData, env: &Env, held_ms: u64) -> Command {
        let total = self.init(a);
        // Alarmdatei jetzt auflösen (beim Ablauf gibt es keine Einstellungen mehr)
        let s = a.settings();
        let alarm = [s_str(&s, "alarm"), s_str(&s, "lastUserFile")].into_iter().find(|p| !p.is_empty()).unwrap_or_default();
        self.alarm = if alarm.is_empty() { alarm } else { system::map_path(&alarm, env.cfg.get("path_map").and_then(Value::as_object)) };
        if env.is_long(held_ms) || self.finished {
            self.running = false;
            self.finished = false;
            self.remaining = total;
            sound::stop(Some(&self.sound_key));
        } else if self.running {
            self.remaining = self.left();
            self.running = false;
        } else {
            self.end_at = Instant::now() + Duration::from_secs_f64(self.remaining.max(0.0));
            self.running = true;
        }
        Command::Redraw
    }
    fn needs_tick(&self) -> bool {
        true
    }
    fn tick(&mut self, a: &ActionData) -> bool {
        self.init(a);
        if !self.running {
            return self.finished;
        }
        self.remaining = self.left();
        if self.remaining <= 0.0 && !self.finished {
            self.running = false;
            self.finished = true;
            if !self.alarm.is_empty() && std::path::Path::new(&self.alarm).exists() {
                sound::play(&self.sound_key, &self.alarm, 100, "", false);
            } else {
                sound::bell();
            }
        }
        true
    }
    fn image(&self, a: &ActionData, _env: &Env, size: (u32, u32)) -> Option<RgbaImage> {
        if has_image(a) {
            return None;
        }
        if self.finished {
            let bg = if !blink() { [200, 30, 30] } else { [0, 0, 0] };
            return Some(render::text_key(size, "00:00", &pgettext("timer", "done"), bg, [255, 255, 255]));
        }
        let total = self.total.unwrap_or_else(|| timer_duration(&a.settings()));
        let left = if self.total.is_some() { self.left() } else { total };
        let sub = if self.running {
            pgettext("timer", "running")
        } else if left.round() < total {
            pgettext("timer", "paused")
        } else {
            "Timer".to_string()
        };
        Some(render::text_key(size, &timer_text(left), &sub, [0, 0, 0], [255, 255, 255]))
    }
}

/// Blinken im Halbsekundentakt (abgelaufener Timer).
fn blink() -> bool {
    let ms = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis()).unwrap_or(0);
    ((ms / 500) & 1) == 0
}

// ----------------------------------------------------------------------------- FreeDeck-Uhr
/// Teile der Uhranzeige.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClockParts {
    pub hm: String,
    pub ampm: String,
    pub sec: String,
    pub date: String,
    pub weekday: String,
    pub city: String,
    pub day: i8,
    pub month: i8,
    pub year: i16,
}

fn weekday_name(i: i8) -> String {
    const W: [&str; 7] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
    pgettext("weekday", W[i.clamp(0, 6) as usize])
}

/// Uhranzeige nach den Einstellungen (hour24, seconds, date_format). `weekday`: 0 = Montag.
pub fn clock_parts(s: &Map<String, Value>, (hour, minute, second): (i8, i8, i8), (day, month, year): (i8, i8, i16), weekday: i8, city: &str) -> ClockParts {
    let h24 = s.get("hour24") != Some(&Value::Bool(false));
    let (hm, ampm) = if h24 {
        (format!("{hour:02}:{minute:02}"), String::new())
    } else {
        let h12 = if hour % 12 == 0 { 12 } else { hour % 12 };
        (format!("{h12}:{minute:02}"), if hour < 12 { "AM" } else { "PM" }.to_string())
    };
    let sec = if s.get("seconds").map(truthy).unwrap_or(false) { format!("{second:02}") } else { String::new() };
    let wd = weekday_name(weekday);
    let date = match s.get("date_format").and_then(Value::as_str).unwrap_or("weekday") {
        "dmy" => format!("{day:02}.{month:02}.{year}"),
        "mdy" => format!("{month:02}/{day:02}/{year}"),
        "iso" => format!("{year}-{month:02}-{day:02}"),
        _ => format!("{wd} {day:02}.{month:02}."),
    };
    ClockParts { hm, ampm, sec, date, weekday: wd, city: city.to_string(), day, month, year }
}

/// com.lindeck.clock.clock
#[derive(Default)]
pub struct Clock {
    last: Option<ClockParts>,
}

impl Clock {
    fn parts(a: &ActionData) -> ClockParts {
        let s = a.settings();
        let (now, city) = now_in(&s_str(&s, "timezone"));
        clock_parts(&s, (now.hour(), now.minute(), now.second()), (now.day(), now.month(), now.year()),
                    now.weekday().to_monday_zero_offset(), &city)
    }

    fn draw(a: &ActionData, size: (u32, u32)) -> RgbaImage {
        let s = a.settings();
        let p = Self::parts(a);
        let (w, h) = (size.0 as f32, size.1 as f32);
        let mut img = d::canvas(size, BG);
        let acc = d::accent(&s_str(&s, "color"), "blue");
        if !p.city.is_empty() {
            d::text(&mut img, w / 2.0, h * 0.13, &p.city, w * 0.88, h * 0.13, acc, true);
        }
        let show = s.get("show").and_then(Value::as_str).unwrap_or("both");
        if show == "date" {
            if p.city.is_empty() {
                d::text(&mut img, w / 2.0, h * 0.15, &p.weekday, w * 0.86, h * 0.16, acc, true);
            }
            d::text(&mut img, w / 2.0, h * 0.5, &p.day.to_string(), w * 0.8, h * 0.46, FG, true);
            let rest = match s.get("date_format").and_then(Value::as_str).unwrap_or("weekday") {
                "mdy" => format!("{:02}/{}", p.month, p.year),
                "iso" => format!("{}-{:02}", p.year, p.month),
                _ => format!("{:02}.{}", p.month, p.year),
            };
            d::text(&mut img, w / 2.0, h * 0.84, &rest, w * 0.86, h * 0.15, MUTED, false);
            return img;
        }
        let with_date = show == "both";
        let cy = h * if with_date { 0.44 } else { 0.52 } + if p.city.is_empty() { 0.0 } else { h * 0.04 };
        let big_h = h * if with_date { 0.36 } else { 0.44 };
        // Sekunden bzw. AM/PM klein hinter der Uhrzeit
        let side = [p.ampm.as_str(), p.sec.as_str()].into_iter().filter(|t| !t.is_empty()).collect::<Vec<_>>().join(" ");
        if side.is_empty() {
            d::text(&mut img, w / 2.0, cy, &p.hm, w * 0.92, big_h, FG, true);
        } else if let Some(f) = render::font(render::Style::Bold) {
            let ratio = if p.ampm.is_empty() { 0.5 } else { 0.42 };
            let gap = w * 0.02;
            let mut size_big = big_h;
            let total = |sz: f32| render::text_width(&f, sz, &p.hm) + gap + render::text_width(&f, sz * ratio, &side);
            if total(size_big) > w * 0.92 {
                size_big *= w * 0.92 / total(size_big);
            }
            let x = (w - total(size_big)) / 2.0;
            let tw = render::text_width(&f, size_big, &p.hm);
            render::draw_text(&mut img, &f, size_big, x, cy - size_big * 0.55, &p.hm, FG);
            let color = if p.sec.is_empty() { MUTED } else { acc };
            render::draw_text(&mut img, &f, size_big * ratio, x + tw + gap, cy - size_big * 0.55 + size_big * (1.0 - ratio) * 0.75, &side, color);
        }
        if with_date {
            d::text(&mut img, w / 2.0, h * 0.82, &p.date, w * 0.88, h * 0.15, MUTED, false);
        } else if !p.sec.is_empty() && p.ampm.is_empty() {
            d::bar(&mut img, p.sec.parse::<f32>().unwrap_or(0.0) / 59.0, acc, 0.88);
        }
        img
    }
}

impl Behavior for Clock {
    fn image(&self, a: &ActionData, _env: &Env, size: (u32, u32)) -> Option<RgbaImage> {
        (!has_image(a)).then(|| Self::draw(a, size))
    }
    fn feedback(&self, a: &ActionData, _env: &Env) -> Feedback {
        let p = Self::parts(a);
        let t = a.title();
        let title = if !t.is_empty() { t } else if !p.city.is_empty() { p.city.clone() } else { p.date.clone() };
        let mut value = p.hm.clone();
        if !p.sec.is_empty() {
            value += &format!(":{}", p.sec);
        }
        if !p.ampm.is_empty() {
            value += &format!(" {}", p.ampm);
        }
        Feedback { title, value, indicator: None }
    }
    fn needs_tick(&self) -> bool {
        true
    }
    fn tick(&mut self, a: &ActionData) -> bool {
        let p = Some(Self::parts(a));
        let changed = p != self.last;
        self.last = p;
        changed
    }
}

// ---------------------------------------------------------------- Countdown und Stoppuhr
/// Zustand eines Countdowns bzw. einer Stoppuhr (pro ActionID, übersteht Seitenwechsel).
#[derive(Debug, Clone)]
struct Run {
    total: f64,
    remaining: f64,
    running: bool,
    end_at: Instant,
    finished: bool,
    /// erhöht sich bei jeder Planung – ältere Alarm-Threads tun dann nichts
    gen: u64,
    dialed: bool,
    start_at: Instant,
    elapsed: f64,
    /// Einstellungen und Pfadzuordnung für den Alarm
    alarm: Option<(Map<String, Value>, Map<String, Value>)>,
}

impl Run {
    fn new(total: f64) -> Self {
        let now = Instant::now();
        Run { total, remaining: total, running: false, end_at: now, finished: false, gen: 0, dialed: false,
              start_at: now, elapsed: 0.0, alarm: None }
    }
    fn left(&self) -> f64 {
        if self.running { self.end_at.saturating_duration_since(Instant::now()).as_secs_f64() } else { self.remaining }
    }
    fn stopwatch(&self) -> f64 {
        self.elapsed + if self.running { self.start_at.elapsed().as_secs_f64() } else { 0.0 }
    }
}

fn runs() -> &'static Mutex<HashMap<String, Run>> {
    static R: OnceLock<Mutex<HashMap<String, Run>>> = OnceLock::new();
    R.get_or_init(Default::default)
}

/// Schlüssel des Zustands: ActionID aus dem Profil, sonst eine eigene Nummer der Aktion.
fn run_key(a: &ActionData, own: &mut Option<String>) -> String {
    if let Some(id) = a.data.get("ActionID").and_then(Value::as_str).filter(|s| !s.is_empty()) {
        return id.to_string();
    }
    own.get_or_insert_with(|| sound::new_key("run")).clone()
}

fn label(s: &Map<String, Value>) -> String {
    s_str(s, "label")
}

/// com.lindeck.clock.countdown: Drücken startet/pausiert, langer Druck setzt zurück, Drehen ändert die Dauer.
#[derive(Default)]
pub struct Countdown {
    own: Option<String>,
    last: Option<(i64, bool, bool, i64)>,
}

impl Countdown {
    fn duration(s: &Map<String, Value>) -> f64 {
        parse_duration(s.get("duration"), 300.0)
    }

    /// Zustand holen (und eine geänderte Dauer übernehmen, solange er nicht läuft).
    fn with<R>(&mut self, a: &ActionData, f: impl FnOnce(&mut Run, &str) -> R) -> R {
        let key = run_key(a, &mut self.own);
        let dur = Self::duration(&a.settings());
        let mut map = runs().lock().unwrap();
        let st = map.entry(key.clone()).or_insert_with(|| Run::new(dur));
        if !st.running && !st.finished && st.remaining == st.total && st.total != dur && !st.dialed {
            st.total = dur;
            st.remaining = dur;
        }
        f(st, &key)
    }

    fn peek(&self, a: &ActionData) -> Run {
        let dur = Self::duration(&a.settings());
        let key = a.data.get("ActionID").and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_string).or_else(|| self.own.clone());
        key.and_then(|k| runs().lock().unwrap().get(&k).cloned()).unwrap_or_else(|| Run::new(dur))
    }

    fn reset(st: &mut Run, key: &str, dur: f64) {
        st.gen += 1;
        st.dialed = false;
        st.total = dur;
        st.remaining = dur;
        st.running = false;
        st.finished = false;
        sound::stop(Some(&format!("countdown-{key}")));
    }

    fn start(st: &mut Run, key: &str) {
        if st.remaining <= 0.0 {
            st.remaining = st.total;
        }
        st.end_at = Instant::now() + Duration::from_secs_f64(st.remaining);
        st.running = true;
        schedule(st, key);
    }

    fn state_text(st: &Run, s: &Map<String, Value>) -> String {
        if st.finished {
            pgettext("timer", "done")
        } else if st.running {
            pgettext("timer", "running")
        } else if st.remaining < st.total {
            pgettext("timer", "paused")
        } else {
            Some(label(s)).filter(|l| !l.is_empty()).unwrap_or_else(|| tr!("Countdown"))
        }
    }

    fn draw(st: &Run, s: &Map<String, Value>, size: (u32, u32), flash: bool) -> RgbaImage {
        let (w, h) = (size.0 as f32, size.1 as f32);
        let mut img = d::canvas(size, if flash { d::ALARM_BG } else { BG });
        let acc = d::accent(&s_str(s, "color"), "orange");
        let l = label(s);
        let top = if !l.is_empty() && !st.finished { l } else { Self::state_text(st, s) };
        let top_color = if flash { FG } else if st.running { acc } else { MUTED };
        d::text(&mut img, w / 2.0, h * 0.15, &top, w * 0.88, h * 0.15, top_color, st.running || flash);
        let rem = shown(st.left());
        let txt = fmt_duration(rem as f64);
        let color = if st.running || st.finished { FG } else if st.remaining < st.total { d::mix(FG, MUTED, 0.5) } else { FG };
        d::text(&mut img, w / 2.0, h * 0.52, &txt, w * 0.9, h * if txt.len() <= 5 { 0.4 } else { 0.3 }, color, true);
        let frac = if st.total > 0.0 { rem as f32 / st.total as f32 } else { 0.0 };
        d::bar(&mut img, frac, if flash { FG } else if st.running { acc } else { MUTED }, 0.87);
        img
    }
}

/// Angezeigte Sekunden (aufgerundet, wie ein Küchenwecker).
fn shown(left: f64) -> i64 {
    (left - 1e-6).ceil().max(0.0) as i64
}

/// Alarm-Thread für den laufenden Countdown planen.
fn schedule(st: &mut Run, key: &str) {
    st.gen += 1;
    let (gen, key) = (st.gen, key.to_string());
    let wait = st.left() + 0.05;
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_secs_f64(wait.max(0.0)));
        let due = {
            let map = runs().lock().unwrap();
            map.get(&key).map(|st| st.gen == gen && st.running && st.left() <= 0.2).unwrap_or(false)
        };
        if due {
            finish(&key);
        }
    });
}

/// Countdown abgelaufen: Zustand setzen, Ton und Benachrichtigung.
fn finish(key: &str) {
    let (alarm, total) = {
        let mut map = runs().lock().unwrap();
        let Some(st) = map.get_mut(key) else { return };
        if st.finished {
            return;
        }
        st.running = false;
        st.finished = true;
        st.remaining = 0.0;
        st.gen += 1;
        (st.alarm.clone(), st.total)
    };
    let (s, path_map) = alarm.unwrap_or_default();
    if s.get("play_sound") != Some(&Value::Bool(false)) {
        let sound_file = s_str(&s, "sound");
        let path = if !sound_file.is_empty() {
            system::map_path(&sound_file, Some(&path_map))
        } else if std::path::Path::new(DEFAULT_ALARM).exists() {
            DEFAULT_ALARM.to_string()
        } else {
            String::new()
        };
        if !path.is_empty() && std::path::Path::new(&path).exists() {
            let vol = s.get("volume").and_then(Value::as_u64).unwrap_or(100) as u32;
            sound::play(&format!("countdown-{key}"), &path, vol, "", s.get("repeat_sound").map(truthy).unwrap_or(false));
        } else {
            sound::bell();
        }
    }
    if s.get("notify") != Some(&Value::Bool(false)) {
        let l = label(&s);
        system::notify(if l.is_empty() { tr!("Countdown") } else { l }.as_str(),
                       &tr!("The countdown of {duration} has finished.", duration = fmt_duration(total)), "alarm-clock");
    }
}

impl Behavior for Countdown {
    fn key_up(&mut self, a: &ActionData, env: &Env, held_ms: u64) -> Command {
        let s = a.settings();
        let dur = Self::duration(&s);
        let long = env.is_long(held_ms);
        let path_map = env.cfg.get("path_map").and_then(Value::as_object).cloned().unwrap_or_default();
        self.with(a, |st, key| {
            st.alarm = Some((s.clone(), path_map));
            if long || st.finished {
                Self::reset(st, key, dur);
            } else if st.running {
                st.remaining = st.left();
                st.running = false;
                st.gen += 1;
            } else {
                Self::start(st, key);
            }
        });
        Command::Redraw
    }
    fn dial_turn(&mut self, a: &ActionData, _env: &Env, ticks: i8) -> Command {
        let step = parse_duration(a.settings().get("dial_step"), 30.0);
        let t = ticks as f64;
        self.with(a, |st, key| {
            if st.finished {
                return;
            }
            if st.running {
                let now = Instant::now();
                let left = (st.left() + t * step).max(1.0);
                st.end_at = now + Duration::from_secs_f64(left);
                st.total = st.total.max(left);
                schedule(st, key);
            } else if st.remaining == st.total {
                st.total = (st.total + t * step).clamp(step, 99.0 * 3600.0);
                st.remaining = st.total;
                st.dialed = true;
            } else {
                st.remaining = (st.remaining + t * step).clamp(1.0, 99.0 * 3600.0);
                st.total = st.total.max(st.remaining);
            }
        });
        Command::Redraw
    }
    fn dial_down(&mut self, _a: &ActionData, _env: &Env) -> Command {
        Command::None
    }
    fn dial_up(&mut self, a: &ActionData, env: &Env, held_ms: u64) -> Command {
        self.key_up(a, env, held_ms)
    }
    fn touch(&mut self, a: &ActionData, env: &Env, long: bool) -> Command {
        self.key_up(a, env, if long { u64::MAX / 2 } else { 0 })
    }
    fn needs_tick(&self) -> bool {
        true
    }
    fn tick(&mut self, a: &ActionData) -> bool {
        let (due, key) = self.with(a, |st, key| (st.running && st.left() <= 0.0, key.to_string()));
        if due {
            finish(&key);
        }
        let st = self.peek(a);
        let snap = Some((shown(st.left()), st.running, st.finished, st.total as i64));
        let changed = snap != self.last;
        self.last = snap;
        changed || st.finished
    }
    fn image(&self, a: &ActionData, _env: &Env, size: (u32, u32)) -> Option<RgbaImage> {
        if has_image(a) {
            return None;
        }
        let st = self.peek(a);
        let flash = st.finished && blink();
        Some(Self::draw(&st, &a.settings(), size, flash))
    }
    fn feedback(&self, a: &ActionData, _env: &Env) -> Feedback {
        let st = self.peek(a);
        let t = a.title();
        let left = st.left();
        Feedback {
            title: if t.is_empty() { Self::state_text(&st, &a.settings()) } else { t },
            value: fmt_duration(shown(left) as f64),
            indicator: Some(if st.total > 0.0 { (100.0 * left / st.total) as f32 } else { 0.0 }),
        }
    }
}

/// com.lindeck.clock.stopwatch: Drücken startet/stoppt, langer Druck setzt zurück.
#[derive(Default)]
pub struct Stopwatch {
    own: Option<String>,
    last: Option<(i64, bool)>,
}

impl Stopwatch {
    fn peek(&self, a: &ActionData) -> Run {
        let key = a.data.get("ActionID").and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_string).or_else(|| self.own.clone());
        key.and_then(|k| runs().lock().unwrap().get(&k).cloned()).unwrap_or_else(|| Run::new(0.0))
    }

    fn draw(st: &Run, s: &Map<String, Value>, size: (u32, u32)) -> RgbaImage {
        let (w, h) = (size.0 as f32, size.1 as f32);
        let mut img = d::canvas(size, BG);
        let acc = d::accent(&s_str(s, "color"), "green");
        let el = st.stopwatch();
        let state = if st.running {
            pgettext("timer", "running")
        } else if st.elapsed > 0.0 {
            pgettext("timer", "paused")
        } else {
            Some(label(s)).filter(|l| !l.is_empty()).unwrap_or_else(|| tr!("Stopwatch"))
        };
        let l = label(s);
        let top = if l.is_empty() { state } else { l };
        d::text(&mut img, w / 2.0, h * 0.15, &top, w * 0.88, h * 0.15, if st.running { acc } else { MUTED }, st.running);
        let txt = fmt_duration(el);
        let color = if st.running || el == 0.0 { FG } else { d::mix(FG, MUTED, 0.5) };
        d::text(&mut img, w / 2.0, h * 0.52, &txt, w * 0.9, h * if txt.len() <= 5 { 0.4 } else { 0.3 }, color, true);
        let frac = if el > 0.0 { (el as i64 % 60) as f32 / 59.0 } else { 0.0 };
        d::bar(&mut img, frac, if st.running { acc } else { MUTED }, 0.87);
        img
    }
}

impl Behavior for Stopwatch {
    fn key_up(&mut self, a: &ActionData, env: &Env, held_ms: u64) -> Command {
        let key = run_key(a, &mut self.own);
        let long = env.is_long(held_ms);
        let mut map = runs().lock().unwrap();
        let st = map.entry(key).or_insert_with(|| Run::new(0.0));
        if long {
            st.running = false;
            st.elapsed = 0.0;
        } else if st.running {
            st.elapsed = st.stopwatch();
            st.running = false;
        } else {
            st.start_at = Instant::now();
            st.running = true;
        }
        Command::Redraw
    }
    fn dial_down(&mut self, _a: &ActionData, _env: &Env) -> Command {
        Command::None
    }
    fn dial_up(&mut self, a: &ActionData, env: &Env, held_ms: u64) -> Command {
        self.key_up(a, env, held_ms)
    }
    fn touch(&mut self, a: &ActionData, env: &Env, long: bool) -> Command {
        self.key_up(a, env, if long { u64::MAX / 2 } else { 0 })
    }
    fn needs_tick(&self) -> bool {
        true
    }
    fn tick(&mut self, a: &ActionData) -> bool {
        let st = self.peek(a);
        let snap = Some((st.stopwatch() as i64, st.running));
        let changed = snap != self.last;
        self.last = snap;
        changed
    }
    fn image(&self, a: &ActionData, _env: &Env, size: (u32, u32)) -> Option<RgbaImage> {
        (!has_image(a)).then(|| Self::draw(&self.peek(a), &a.settings(), size))
    }
    fn feedback(&self, a: &ActionData, _env: &Env) -> Feedback {
        let st = self.peek(a);
        let el = st.stopwatch();
        let t = a.title();
        let title = if !t.is_empty() {
            t
        } else if st.running {
            pgettext("timer", "running")
        } else if st.elapsed > 0.0 {
            pgettext("timer", "paused")
        } else {
            Some(label(&a.settings())).filter(|l| !l.is_empty()).unwrap_or_else(|| tr!("Stopwatch"))
        };
        Feedback { title, value: fmt_duration(el), indicator: Some((el as i64 % 60) as f32 * 100.0 / 59.0) }
    }
}

pub fn behavior_for(uuid: &str) -> Option<Box<dyn Behavior>> {
    Some(match uuid {
        "com.elgato.streamdeck.system.digitaltime" => Box::<DigitalTime>::default(),
        "com.elgato.streamdeck.system.timer" | "com.elgato.streamdeck.timer" => Box::<Timer>::default(),
        "com.lindeck.clock.clock" => Box::<Clock>::default(),
        "com.lindeck.clock.countdown" => Box::<Countdown>::default(),
        "com.lindeck.clock.stopwatch" => Box::<Stopwatch>::default(),
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::path::PathBuf;

    fn obj(v: Value) -> Map<String, Value> {
        v.as_object().cloned().unwrap()
    }

    #[test]
    fn durations() {
        assert_eq!(fmt_duration(245.0), "4:05");
        assert_eq!(fmt_duration(3723.0), "1:02:03");
        assert_eq!(fmt_duration(-3.0), "0:00");
        assert_eq!(parse_duration(Some(&json!("5:00")), 1.0), 300.0);
        assert_eq!(parse_duration(Some(&json!("1:30:00")), 1.0), 5400.0);
        assert_eq!(parse_duration(Some(&json!("90")), 1.0), 90.0);
        assert_eq!(parse_duration(Some(&json!("90s")), 1.0), 90.0);
        assert_eq!(parse_duration(Some(&json!("5m")), 1.0), 300.0);
        assert_eq!(parse_duration(Some(&json!("1h 30m")), 1.0), 5400.0);
        assert_eq!(parse_duration(Some(&json!(45)), 1.0), 45.0);
        assert_eq!(parse_duration(Some(&json!("abc")), 7.0), 7.0);
        assert_eq!(parse_duration(Some(&json!(0)), 7.0), 7.0);
        assert_eq!(parse_duration(None, 7.0), 7.0);
    }

    #[test]
    fn elgato_timer_duration_and_text() {
        assert_eq!(timer_duration(&obj(json!({}))), 300.0);
        assert_eq!(timer_duration(&obj(json!({"duration": 90}))), 90.0);
        assert_eq!(timer_duration(&obj(json!({"duration": 120000}))), 120.0);
        assert_eq!(timer_duration(&obj(json!({"time": "1:30"}))), 90.0);
        assert_eq!(timer_duration(&obj(json!({"duration": "x"}))), 300.0);
        assert_eq!(timer_text(299.4), "04:59");
        assert_eq!(timer_text(3725.0), "1:02:05");
    }

    #[test]
    fn digital_time_formats() {
        assert_eq!(digital_time(21, 5, 30, 10, 2026, false), ("21:05".into(), "30.10.2026".into()));
        assert_eq!(digital_time(21, 5, 1, 2, 2026, true).0, "9:05 PM");
        assert_eq!(digital_time(0, 7, 1, 2, 2026, true).0, "12:07 AM");
        assert_eq!(digital_time(7, 7, 1, 2, 2026, false).0, "07:07");
    }

    #[test]
    fn clock_formats() {
        freestream_core::i18n::set_language("en");
        let p = clock_parts(&obj(json!({})), (9, 5, 7), (30, 10, 2026), 4, "");
        assert_eq!((p.hm.as_str(), p.ampm.as_str(), p.sec.as_str(), p.date.as_str()), ("09:05", "", "", "Fri 30.10."));
        let p = clock_parts(&obj(json!({"hour24": false, "seconds": true, "date_format": "iso"})), (13, 5, 7), (3, 1, 2026), 0, "Berlin");
        assert_eq!((p.hm.as_str(), p.ampm.as_str(), p.sec.as_str(), p.date.as_str()), ("1:05", "PM", "07", "2026-01-03"));
        let p = clock_parts(&obj(json!({"date_format": "mdy"})), (0, 0, 0), (3, 1, 2026), 0, "");
        assert_eq!(p.date, "01/03/2026");
    }

    #[test]
    fn countdown_and_stopwatch_states() {
        let cfg = Map::new();
        let env = Env::new(&cfg, 0, 1);
        let a = ActionData::new(obj(json!({"UUID": "com.lindeck.clock.countdown", "ActionID": "test-countdown",
            "Settings": {"duration": "2:00", "notify": false, "play_sound": false}})), PathBuf::new());
        let mut c = Countdown::default();
        assert_eq!(c.peek(&a).total, 120.0);
        c.key_up(&a, &env, 0); // Start
        assert!(c.peek(&a).running);
        c.key_up(&a, &env, 0); // Pause
        let st = c.peek(&a);
        assert!(!st.running && st.remaining <= 120.0 && st.remaining > 119.0);
        c.dial_turn(&a, &env, 2); // pausiert: Restzeit + 60 s
        assert!(c.peek(&a).remaining > 179.0);
        c.key_up(&a, &env, 5000); // langer Druck: zurücksetzen
        let st = c.peek(&a);
        assert_eq!((st.total, st.remaining, st.running), (120.0, 120.0, false));
        assert!(c.image(&a, &env, (72, 72)).is_some());

        let a = ActionData::new(obj(json!({"UUID": "com.lindeck.clock.stopwatch", "ActionID": "test-sw"})), PathBuf::new());
        let mut s = Stopwatch::default();
        s.key_up(&a, &env, 0);
        assert!(s.peek(&a).running);
        s.key_up(&a, &env, 0);
        assert!(!s.peek(&a).running);
        s.key_up(&a, &env, 600);
        assert_eq!(s.peek(&a).stopwatch(), 0.0);
    }
}
