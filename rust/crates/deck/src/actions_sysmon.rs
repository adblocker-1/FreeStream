//! Systemmonitor-Tasten com.lindeck.sysmon.cpu/memory/temperature/network/battery/disk (wie `SysMon` in
//! `freestream/livekeys.py`): großer Messwert, Balken oder kleiner Verlaufsgraph, Zusatzzeile.

use freestream_core::i18n::pgettext;
use freestream_core::tr;
use image::RgbaImage;
use serde_json::{Map, Value};
use std::collections::VecDeque;

use crate::actions::{ActionData, Behavior, Env};
use crate::actions_time::fmt_duration;
use crate::livedraw::{self as d, Rgb, BG, FG, MUTED};
use crate::sysinfo::{self, human_bytes, CpuMeter, RateMeter, SysInfo};

const HISTORY: usize = 30;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Metric {
    Cpu,
    Memory,
    Temperature,
    Network,
    Battery,
    Disk,
}

impl Metric {
    pub fn from_uuid(uuid: &str) -> Option<Metric> {
        Some(match uuid.to_lowercase().strip_prefix("com.lindeck.sysmon.")? {
            "cpu" => Metric::Cpu,
            "memory" => Metric::Memory,
            "temperature" => Metric::Temperature,
            "network" => Metric::Network,
            "battery" => Metric::Battery,
            "disk" => Metric::Disk,
            _ => return None,
        })
    }

    fn default_color(self) -> &'static str {
        match self {
            Metric::Memory => "purple",
            Metric::Temperature => "orange",
            Metric::Network => "teal",
            Metric::Battery => "green",
            Metric::Cpu | Metric::Disk => "blue",
        }
    }
}

/// Ein Messwert für die Anzeige.
#[derive(Debug, Clone, PartialEq)]
pub struct Sample {
    pub text: String,
    pub sub: String,
    pub frac: Option<f64>,
    pub value: Option<f64>,
    pub value2: Option<f64>,
    pub color: Rgb,
    pub charging: bool,
}

impl Sample {
    fn none() -> Self {
        Sample { text: "–".into(), sub: tr!("not available"), frac: None, value: None, value2: None, color: MUTED, charging: false }
    }
}

fn s_str(s: &Map<String, Value>, k: &str) -> String {
    s.get(k).and_then(Value::as_str).unwrap_or("").trim().to_string()
}

/// Zusatzzeile zum Akku: lädt / voll / Restzeit / am Netz.
pub fn battery_sub(b: &sysinfo::Battery) -> String {
    if b.charging {
        pgettext("battery", "charging")
    } else if b.status == "Full" || (b.plugged && b.percent >= 95) {
        pgettext("battery", "full")
    } else if let Some(h) = b.hours.filter(|h| *h > 0.0) {
        let t = if h >= 1.0 {
            let f = fmt_duration(h * 3600.0);
            f[..f.len() - 3].to_string()
        } else {
            format!("{} min", (h * 60.0) as i64)
        };
        tr!("{time} left", time = t)
    } else if b.plugged {
        pgettext("battery", "plugged in")
    } else {
        String::new()
    }
}

/// com.lindeck.sysmon.*
pub struct SysMon {
    metric: Metric,
    info: SysInfo,
    cpu: CpuMeter,
    net: RateMeter,
    hist: VecDeque<Option<f64>>,
    hist2: VecDeque<Option<f64>>,
    cur: Option<Sample>,
    last: Option<(String, String)>,
}

impl SysMon {
    pub fn new(metric: Metric, info: SysInfo) -> Self {
        Self { metric, info, cpu: CpuMeter::default(), net: RateMeter::default(), hist: VecDeque::new(), hist2: VecDeque::new(),
               cur: None, last: None }
    }

    fn graph(&self, s: &Map<String, Value>) -> bool {
        match s.get("graph").and_then(Value::as_bool) {
            Some(g) => g,
            None => matches!(self.metric, Metric::Cpu | Metric::Memory | Metric::Temperature | Metric::Network),
        }
    }

    fn label(&self, s: &Map<String, Value>) -> String {
        match self.metric {
            Metric::Network => Some(s_str(s, "interface")).filter(|i| !i.is_empty()).unwrap_or_else(|| pgettext("short", "Network")),
            Metric::Disk => {
                let p = s_str(s, "path");
                let p = if p.is_empty() { "/".to_string() } else { p };
                let p = p.trim_end_matches('/');
                p.rsplit('/').next().filter(|b| !b.is_empty()).unwrap_or("/").to_string()
            }
            Metric::Cpu => "CPU".into(),
            Metric::Memory => "RAM".into(),
            Metric::Temperature => pgettext("short", "Temp"),
            Metric::Battery => pgettext("short", "Battery"),
        }
    }

    /// Neuen Messwert lesen.
    pub fn sample(&mut self, s: &Map<String, Value>) -> Sample {
        let acc = d::accent(&s_str(s, "color"), self.metric.default_color());
        let mut r = Sample::none();
        match self.metric {
            Metric::Cpu => {
                if let Some(v) = self.cpu.update(self.info.cpu_times()) {
                    r = Sample { text: format!("{v:.0}%"), sub: String::new(), frac: Some(v / 100.0), value: Some(v),
                                 color: d::level(v as f32 / 100.0, acc, 0.75, 0.9, false), ..r };
                }
            }
            Metric::Memory => {
                if let Some((pct, used, total)) = self.info.memory() {
                    r = Sample { text: format!("{pct:.0}%"), sub: format!("{} / {}", human_bytes(used as f64, false), human_bytes(total as f64, false)),
                                 frac: Some(pct / 100.0), value: Some(pct), color: d::level(pct as f32 / 100.0, acc, 0.8, 0.92, false), ..r };
                }
            }
            Metric::Temperature => {
                if let Some(c) = self.info.temperature(&s_str(s, "sensor")) {
                    let fahr = s_str(s, "unit") == "f";
                    let shown = if fahr { c * 9.0 / 5.0 + 32.0 } else { c };
                    r = Sample { text: format!("{shown:.0}°{}", if fahr { 'F' } else { 'C' }), sub: String::new(), frac: Some((c / 100.0).min(1.0)),
                                 value: Some(c), color: d::level(c as f32 / 100.0, acc, 0.7, 0.85, false), ..r };
                }
            }
            Metric::Network => {
                let bytes = self.info.net_bytes(&s_str(s, "interface"));
                match self.net.update(bytes) {
                    Some((down, up)) => {
                        r = Sample { text: human_bytes(down, true), sub: human_bytes(up, true), frac: None, value: Some(down), value2: Some(up),
                                     color: acc, charging: false };
                    }
                    None => {
                        r.sub = String::new();
                        r.text = if bytes.is_some() { "…".into() } else { "–".into() };
                    }
                }
            }
            Metric::Battery => {
                if let Some(b) = self.info.battery(&s_str(s, "battery")) {
                    let pct = b.percent as f64;
                    let color = if b.charging { acc } else { d::level(pct as f32 / 100.0, acc, 0.3, 0.15, true) };
                    r = Sample { text: format!("{}%", b.percent), sub: battery_sub(&b), frac: Some(pct / 100.0), value: Some(pct), value2: None,
                                 color, charging: b.charging };
                }
            }
            Metric::Disk => {
                let p = s_str(s, "path");
                if let Some((pct, free, _)) = sysinfo::disk(if p.is_empty() { "/" } else { &p }) {
                    r = Sample { text: format!("{pct:.0}%"), sub: tr!("{size} free", size = human_bytes(free as f64, false)),
                                 frac: Some(pct / 100.0), value: Some(pct), color: d::level(pct as f32 / 100.0, acc, 0.85, 0.95, false), ..r };
                }
            }
        }
        for (h, v) in [(&mut self.hist, r.value), (&mut self.hist2, r.value2)] {
            h.push_back(v);
            while h.len() > HISTORY {
                h.pop_front();
            }
        }
        self.cur = Some(r.clone());
        r
    }

    fn draw(&self, s: &Map<String, Value>, size: (u32, u32)) -> RgbaImage {
        let (w, h) = (size.0 as f32, size.1 as f32);
        let mut img = d::canvas(size, BG);
        let r = self.cur.clone().unwrap_or_else(Sample::none);
        let acc = d::accent(&s_str(s, "color"), self.metric.default_color());
        let mut lab = self.label(s);
        if r.charging {
            lab = format!("⚡ {lab}");
        }
        d::text(&mut img, w / 2.0, h * 0.15, &lab, w * 0.86, h * 0.14, MUTED, true);
        let graph = self.graph(s);
        let hist: Vec<Option<f64>> = self.hist.iter().copied().collect();
        if self.metric == Metric::Network {
            let fs = h * if graph { 0.2 } else { 0.22 };
            let ys = if graph { (0.36, 0.54) } else { (0.42, 0.68) };
            for (y, arrow, val, col) in [(ys.0, "↓", &r.text, FG), (ys.1, "↑", &r.sub, MUTED)] {
                if val.is_empty() {
                    continue;
                }
                let t = if r.value.is_some() { format!("{arrow} {val}") } else { val.clone() };
                d::text(&mut img, w / 2.0, h * y, &t, w * 0.92, fs, col, true);
            }
            if graph {
                let hist2: Vec<Option<f64>> = self.hist2.iter().copied().collect();
                let top = hist.iter().chain(&hist2).flatten().copied().fold(1.0, f64::max);
                let bx = (w * 0.08, h * 0.68, w * 0.92, h * 0.93);
                d::spark(&mut img, bx, &hist2, top, d::mix(BG, MUTED, 0.7));
                d::spark(&mut img, bx, &hist, top, acc);
            }
            return img;
        }
        if graph && r.value.is_some() {
            d::text(&mut img, w / 2.0, h * 0.43, &r.text, w * 0.9, h * 0.3, FG, true);
            let color = if r.color == MUTED { acc } else { r.color };
            d::spark(&mut img, (w * 0.08, h * 0.64, w * 0.92, h * 0.93), &hist, 100.0, color);
        } else {
            d::text(&mut img, w / 2.0, h * 0.46, &r.text, w * 0.9, h * 0.34, FG, true);
            if let Some(f) = r.frac {
                d::bar(&mut img, f as f32, r.color, 0.69);
            }
            d::text(&mut img, w / 2.0, h * 0.86, &r.sub, w * 0.9, h * 0.13, MUTED, false);
        }
        img
    }
}

impl Behavior for SysMon {
    fn image(&self, a: &ActionData, _env: &Env, size: (u32, u32)) -> Option<RgbaImage> {
        if a.state_dict().get("Image").and_then(Value::as_str).map(|s| !s.is_empty()).unwrap_or(false) {
            return None;
        }
        Some(self.draw(&a.settings(), size))
    }
    fn needs_tick(&self) -> bool {
        true
    }
    fn tick(&mut self, a: &ActionData) -> bool {
        let s = a.settings();
        let r = self.sample(&s);
        let snap = Some((r.text, r.sub));
        let changed = snap != self.last;
        self.last = snap;
        changed || self.graph(&s)
    }
}

pub fn behavior_for(uuid: &str) -> Option<Box<dyn Behavior>> {
    Metric::from_uuid(uuid).map(|m| Box::new(SysMon::new(m, SysInfo::default())) as Box<dyn Behavior>)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn metrics_from_uuid() {
        assert_eq!(Metric::from_uuid("com.lindeck.sysmon.cpu"), Some(Metric::Cpu));
        assert_eq!(Metric::from_uuid("com.lindeck.sysmon.Disk"), Some(Metric::Disk));
        assert_eq!(Metric::from_uuid("com.lindeck.sysmon.gpu"), None);
        assert!(behavior_for("com.lindeck.sysmon.battery").is_some());
    }

    #[test]
    fn samples_from_fixtures() {
        freestream_core::i18n::set_language("en");
        let dir = tempfile::tempdir().unwrap();
        let w = |p: &str, v: &str| {
            let f = dir.path().join(p);
            std::fs::create_dir_all(f.parent().unwrap()).unwrap();
            std::fs::write(f, v).unwrap();
        };
        w("stat", "cpu  100 0 0 100 0 0 0 0\n");
        w("meminfo", "MemTotal: 2000000 kB\nMemAvailable: 500000 kB\n");
        w("class/power_supply/BAT0/type", "Battery\n");
        w("class/power_supply/BAT0/capacity", "42\n");
        w("class/power_supply/BAT0/status", "Charging\n");
        let s = Map::new();
        let info = SysInfo::with_roots(dir.path(), dir.path());
        let mut cpu = SysMon::new(Metric::Cpu, info.clone());
        assert_eq!(cpu.sample(&s).text, "50%");
        w("stat", "cpu  200 0 0 300 0 0 0 0\n");
        assert_eq!(cpu.sample(&s).text, "33%");
        let mut mem = SysMon::new(Metric::Memory, info.clone());
        let r = mem.sample(&s);
        assert_eq!((r.text.as_str(), r.sub.as_str()), ("75%", "1.5 GB / 2.0 GB"));
        let mut bat = SysMon::new(Metric::Battery, info.clone());
        let r = bat.sample(&s);
        assert_eq!((r.text.as_str(), r.sub.as_str(), r.charging), ("42%", "charging", true));
        let mut temp = SysMon::new(Metric::Temperature, info.clone());
        assert_eq!(temp.sample(&s).text, "–");
        let mut net = SysMon::new(Metric::Network, info);
        assert_eq!(net.sample(&s).text, "–");
        let img = mem.draw(&json!({"graph": false}).as_object().cloned().unwrap(), (72, 72));
        assert_eq!(img.dimensions(), (72, 72));
    }

    #[test]
    fn battery_texts() {
        freestream_core::i18n::set_language("en");
        let b = |status: &str, plugged: bool, percent: u8, hours: Option<f64>| sysinfo::Battery {
            percent, status: status.into(), charging: status == "Charging", plugged, hours };
        assert_eq!(battery_sub(&b("Discharging", false, 50, Some(2.5))), "2:30 left");
        assert_eq!(battery_sub(&b("Discharging", false, 50, Some(0.5))), "30 min left");
        assert_eq!(battery_sub(&b("Full", true, 100, None)), "full");
        assert_eq!(battery_sub(&b("Not charging", true, 80, None)), "plugged in");
        assert_eq!(battery_sub(&b("Discharging", false, 50, None)), "");
    }
}
