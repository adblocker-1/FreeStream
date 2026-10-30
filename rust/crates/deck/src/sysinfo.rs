//! Systemwerte direkt aus /proc und /sys (wie `freestream/sysinfo.py`). Die Pfade sind austauschbar
//! ([`SysInfo::with_roots`]), damit die Tests mit nachgebauten Dateien arbeiten; das Zerlegen der Dateien
//! geschieht in reinen Funktionen (`parse_*`).

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::time::Instant;

/// Sensoren, die die CPU-Temperatur liefern (in dieser Reihenfolge bevorzugt)
const CPU_HWMON: &[&str] = &["coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal", "soc_thermal", "acpitz", "thinkpad"];
const CPU_ZONES: &[&str] = &["x86_pkg_temp", "cpu-thermal", "cpu_thermal", "soc-thermal", "tcpu", "acpitz"];
const CPU_LABELS: &[&str] = &["package id 0", "tctl", "tdie", "cpu"];
/// virtuelle Schnittstellen, die bei „alle“ nicht mitgezählt werden
const VIRTUAL_IF: &[&str] = &["lo", "veth", "docker", "br-", "virbr", "vnet", "vmnet", "podman", "cni", "flannel"];

fn read(p: &Path) -> String {
    std::fs::read_to_string(p).map(|s| s.trim().to_string()).unwrap_or_default()
}

fn int(p: &Path) -> Option<i64> {
    read(p).parse().ok()
}

/// /proc/stat -> (Leerlauf, Gesamt) in Jiffies.
pub fn parse_cpu_times(stat: &str) -> Option<(u64, u64)> {
    let line = stat.lines().find(|l| l.starts_with("cpu "))?;
    let vals: Vec<u64> = line.split_whitespace().skip(1).filter_map(|v| v.parse().ok()).collect();
    if vals.len() < 4 {
        return None;
    }
    let idle = vals[3] + vals.get(4).copied().unwrap_or(0);
    Some((idle, vals.iter().take(8).sum()))
}

/// /proc/meminfo -> (belegt in %, belegt Bytes, gesamt Bytes).
pub fn parse_meminfo(text: &str) -> Option<(f64, u64, u64)> {
    let mut info: BTreeMap<&str, u64> = BTreeMap::new();
    for line in text.lines() {
        if let Some((k, v)) = line.split_once(':') {
            if let Some(n) = v.split_whitespace().next().and_then(|n| n.parse::<u64>().ok()) {
                info.insert(k.trim(), n * 1024);
            }
        }
    }
    let total = *info.get("MemTotal").filter(|t| **t > 0)?;
    let avail = info.get("MemAvailable").copied()
        .unwrap_or_else(|| info.get("MemFree").unwrap_or(&0) + info.get("Buffers").unwrap_or(&0) + info.get("Cached").unwrap_or(&0));
    let used = total.saturating_sub(avail);
    Some((used as f64 * 100.0 / total as f64, used, total))
}

/// /proc/net/dev -> {Schnittstelle: (empfangen, gesendet)}.
pub fn parse_net_dev(text: &str) -> BTreeMap<String, (u64, u64)> {
    let mut out = BTreeMap::new();
    for line in text.lines().skip(2) {
        let Some((name, rest)) = line.split_once(':') else { continue };
        let f: Vec<&str> = rest.split_whitespace().collect();
        if let (Some(rx), Some(tx)) = (f.first().and_then(|v| v.parse().ok()), f.get(8).and_then(|v| v.parse().ok())) {
            out.insert(name.trim().to_string(), (rx, tx));
        }
    }
    out
}

pub fn is_virtual(iface: &str) -> bool {
    VIRTUAL_IF.iter().any(|p| iface.starts_with(p))
}

/// Akkustand.
#[derive(Debug, Clone, PartialEq)]
pub struct Battery {
    pub percent: u8,
    pub status: String,
    pub charging: bool,
    pub plugged: bool,
    pub hours: Option<f64>,
}

/// Zugriff auf /proc und /sys.
#[derive(Debug, Clone)]
pub struct SysInfo {
    proc: PathBuf,
    sys: PathBuf,
}

impl Default for SysInfo {
    fn default() -> Self {
        Self::with_roots("/proc", "/sys")
    }
}

impl SysInfo {
    pub fn with_roots(proc: impl Into<PathBuf>, sys: impl Into<PathBuf>) -> Self {
        Self { proc: proc.into(), sys: sys.into() }
    }

    pub fn cpu_times(&self) -> Option<(u64, u64)> {
        parse_cpu_times(&read(&self.proc.join("stat")))
    }

    pub fn memory(&self) -> Option<(f64, u64, u64)> {
        parse_meminfo(&read(&self.proc.join("meminfo")))
    }

    pub fn net_counters(&self) -> BTreeMap<String, (u64, u64)> {
        parse_net_dev(&std::fs::read_to_string(self.proc.join("net/dev")).unwrap_or_default())
    }

    /// (empfangen, gesendet) einer Schnittstelle oder aller echten zusammen.
    pub fn net_bytes(&self, iface: &str) -> Option<(u64, u64)> {
        let c = self.net_counters();
        if !iface.is_empty() {
            return c.get(iface).copied();
        }
        let sel: Vec<(u64, u64)> = c.iter().filter(|(n, _)| !is_virtual(n)).map(|(_, v)| *v).collect();
        if sel.is_empty() {
            return None;
        }
        Some((sel.iter().map(|v| v.0).sum(), sel.iter().map(|v| v.1).sum()))
    }

    fn sorted(dir: &Path, prefix: &str) -> Vec<PathBuf> {
        let mut v: Vec<PathBuf> = std::fs::read_dir(dir).into_iter().flatten().flatten()
            .map(|e| e.path())
            .filter(|p| p.file_name().and_then(|n| n.to_str()).map(|n| n.starts_with(prefix)).unwrap_or(false))
            .collect();
        v.sort();
        v
    }

    /// Alle Sensoren als (Name, °C) – hwmon zuerst, dann thermal_zone.
    pub fn temperatures(&self) -> Vec<(String, f64)> {
        let mut out = Vec::new();
        for d in Self::sorted(&self.sys.join("class/hwmon"), "hwmon") {
            let name = Some(read(&d.join("name"))).filter(|n| !n.is_empty())
                .unwrap_or_else(|| d.file_name().unwrap_or_default().to_string_lossy().into_owned());
            let mut best = None;
            for t in Self::sorted(&d, "temp").into_iter().filter(|p| p.to_string_lossy().ends_with("_input")) {
                let label = read(Path::new(&t.to_string_lossy().replace("_input", "_label"))).to_lowercase();
                let Some(v) = int(&t) else { continue };
                let cpu = CPU_LABELS.contains(&label.as_str());
                if best.is_none() || cpu {
                    best = Some(v);
                    if cpu {
                        break;
                    }
                }
            }
            if let Some(b) = best {
                out.push((name, b as f64 / 1000.0));
            }
        }
        for z in Self::sorted(&self.sys.join("class/thermal"), "thermal_zone") {
            if let Some(v) = int(&z.join("temp")) {
                let name = Some(read(&z.join("type"))).filter(|n| !n.is_empty())
                    .unwrap_or_else(|| z.file_name().unwrap_or_default().to_string_lossy().into_owned());
                out.push((name, v as f64 / 1000.0));
            }
        }
        out
    }

    /// CPU-Temperatur (oder die eines bestimmten Sensors) in °C.
    pub fn temperature(&self, sensor: &str) -> Option<f64> {
        let temps = self.temperatures();
        if temps.is_empty() {
            return None;
        }
        if !sensor.is_empty() {
            let s = sensor.to_lowercase();
            return temps.iter().find(|(n, _)| n.to_lowercase() == s)
                .or_else(|| temps.iter().find(|(n, _)| n.to_lowercase().contains(&s)))
                .map(|(_, v)| *v);
        }
        for pref in CPU_HWMON.iter().chain(CPU_ZONES) {
            if let Some((_, v)) = temps.iter().find(|(n, _)| n.to_lowercase() == *pref) {
                return Some(*v);
            }
        }
        Some(temps[0].1)
    }

    /// Akku (name: bestimmter Akku, sonst der erste des Systems); None ohne Akku.
    pub fn battery(&self, name: &str) -> Option<Battery> {
        let mut bats = Vec::new();
        let mut plugged: Option<bool> = None;
        for d in Self::sorted(&self.sys.join("class/power_supply"), "") {
            let kind = read(&d.join("type"));
            if kind == "Mains" || (kind == "USB" && d.join("online").exists()) {
                let on = int(&d.join("online")).unwrap_or(0) != 0;
                plugged = Some(on || plugged.unwrap_or(false));
            } else if kind == "Battery" {
                if read(&d.join("scope")) == "Device" && name.is_empty() {
                    continue; // Akku von Maus oder Kopfhörer
                }
                bats.push(d);
            }
        }
        if !name.is_empty() {
            bats.retain(|d| d.file_name().map(|n| n == name).unwrap_or(false));
        }
        let d = bats.first()?;
        let now = int(&d.join("energy_now")).filter(|v| *v != 0).or_else(|| int(&d.join("charge_now")));
        let full = int(&d.join("energy_full")).filter(|v| *v != 0).or_else(|| int(&d.join("charge_full")));
        let pct = int(&d.join("capacity")).or_else(|| match (now, full) {
            (Some(n), Some(f)) if n != 0 && f != 0 => Some((n as f64 * 100.0 / f as f64).round() as i64),
            _ => None,
        })?;
        let status = Some(read(&d.join("status"))).filter(|s| !s.is_empty()).unwrap_or_else(|| "Unknown".into());
        let rate = int(&d.join("power_now")).filter(|v| *v != 0).or_else(|| int(&d.join("current_now"))).unwrap_or(0).abs();
        let mut hours = None;
        if rate != 0 {
            if let Some(n) = now {
                if status == "Discharging" {
                    hours = Some(n as f64 / rate as f64);
                } else if status == "Charging" {
                    if let Some(f) = full {
                        hours = Some((f - n).max(0) as f64 / rate as f64);
                    }
                }
            }
        }
        let plugged = plugged.unwrap_or(matches!(status.as_str(), "Charging" | "Full" | "Not charging"));
        Some(Battery { percent: pct.clamp(0, 100) as u8, charging: status == "Charging", status, plugged, hours })
    }
}

/// Belegter Platz eines Dateisystems: (belegt in %, frei Bytes, gesamt Bytes).
pub fn disk(path: &str) -> Option<(f64, u64, u64)> {
    let p = crate::system::expand_user(if path.is_empty() { "/" } else { path });
    let c = std::ffi::CString::new(p).ok()?;
    let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
    // SAFETY: c ist ein gültiger C-String, st ein beschreibbarer Puffer der richtigen Größe
    if unsafe { libc::statvfs(c.as_ptr(), &mut st) } != 0 {
        return None;
    }
    let frsize = st.f_frsize as u64;
    let total = st.f_blocks as u64 * frsize;
    if total == 0 {
        return None;
    }
    let free = st.f_bavail as u64 * frsize;
    let used = total - st.f_bfree as u64 * frsize;
    Some((used as f64 * 100.0 / total as f64, free, total))
}

/// CPU-Last zwischen zwei Messungen (erste Messung: Mittel seit dem Start).
#[derive(Debug, Default, Clone)]
pub struct CpuMeter {
    last: (u64, u64),
    val: Option<f64>,
}

impl CpuMeter {
    pub fn update(&mut self, t: Option<(u64, u64)>) -> Option<f64> {
        let t = t?;
        let (di, dt) = (t.0.saturating_sub(self.last.0), t.1.saturating_sub(self.last.1));
        if dt == 0 {
            return self.val;
        }
        self.last = t;
        self.val = Some((100.0 * (1.0 - di as f64 / dt as f64)).clamp(0.0, 100.0));
        self.val
    }
}

/// Rate eines Zählers pro Sekunde (z. B. Bytes der Netzwerkkarte).
#[derive(Debug, Default, Clone)]
pub struct RateMeter {
    last: Option<(f64, (u64, u64))>,
    rate: Option<(f64, f64)>,
}

impl RateMeter {
    /// `now` in Sekunden; None bei der ersten Messung.
    pub fn update_at(&mut self, now: f64, values: Option<(u64, u64)>) -> Option<(f64, f64)> {
        let Some(values) = values else {
            self.last = None;
            return None;
        };
        let last = self.last.replace((now, values))?;
        let dt = now - last.0;
        if dt <= 0.0 {
            return self.rate;
        }
        let r = |v: u64, o: u64| (v as f64 - o as f64).max(0.0) / dt;
        self.rate = Some((r(values.0, last.1 .0), r(values.1, last.1 .1)));
        self.rate
    }

    pub fn update(&mut self, values: Option<(u64, u64)>) -> Option<(f64, f64)> {
        static START: std::sync::OnceLock<Instant> = std::sync::OnceLock::new();
        let now = START.get_or_init(Instant::now).elapsed().as_secs_f64();
        self.update_at(now, values)
    }
}

/// 1234567 -> „1.2 MB“ (bzw. „1.2 MB/s“).
pub fn human_bytes(n: f64, per_second: bool) -> String {
    let suffix = if per_second { "/s" } else { "" };
    let mut n = n;
    for unit in ["B", "kB", "MB", "GB", "TB"] {
        if n.abs() < 1000.0 || unit == "TB" {
            return if unit == "B" {
                format!("{n:.0} {unit}{suffix}")
            } else if n.abs() < 100.0 {
                format!("{n:.1} {unit}{suffix}")
            } else {
                format!("{n:.0} {unit}{suffix}")
            };
        }
        n /= 1000.0;
    }
    unreachable!()
}

#[cfg(test)]
mod tests {
    use super::*;

    const STAT: &str = "cpu  4705 356 584 3699 23 23 0 0 0 0\ncpu0 1393280 32966 572056 13343292 6130 0 17875 0 23933 0\nintr 1\n";
    const MEMINFO: &str = "MemTotal:       16000000 kB\nMemFree:         2000000 kB\nMemAvailable:    4000000 kB\nBuffers:          100000 kB\nCached:          3000000 kB\n";
    const NETDEV: &str = "Inter-|   Receive                                                |  Transmit\n face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n    lo: 1000 10 0 0 0 0 0 0 1000 10 0 0 0 0 0 0\n  eth0: 5000 50 0 0 0 0 0 0 7000 60 0 0 0 0 0 0\nwlan0: 300 3 0 0 0 0 0 0 400 4 0 0 0 0 0 0\ndocker0: 99 1 0 0 0 0 0 0 99 1 0 0 0 0 0 0\n";

    #[test]
    fn cpu_and_meter() {
        assert_eq!(parse_cpu_times(STAT), Some((3699 + 23, 4705 + 356 + 584 + 3699 + 23 + 23)));
        assert_eq!(parse_cpu_times("cpu  1 2\n"), None);
        let mut m = CpuMeter::default();
        m.update(Some((100, 200)));
        assert_eq!(m.update(Some((150, 300))), Some(50.0));
        assert_eq!(m.update(Some((150, 300))), Some(50.0));
    }

    #[test]
    fn memory() {
        let (pct, used, total) = parse_meminfo(MEMINFO).unwrap();
        assert_eq!(total, 16_000_000 * 1024);
        assert_eq!(used, 12_000_000 * 1024);
        assert!((pct - 75.0).abs() < 1e-9);
        // ohne MemAvailable: frei + Puffer + Cache
        let (pct, ..) = parse_meminfo("MemTotal: 1000 kB\nMemFree: 100 kB\nBuffers: 100 kB\nCached: 300 kB\n").unwrap();
        assert!((pct - 50.0).abs() < 1e-9);
        assert!(parse_meminfo("").is_none());
    }

    #[test]
    fn network() {
        let c = parse_net_dev(NETDEV);
        assert_eq!(c.get("eth0"), Some(&(5000, 7000)));
        assert_eq!(c.len(), 4);
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(dir.path().join("net")).unwrap();
        std::fs::write(dir.path().join("net/dev"), NETDEV).unwrap();
        let si = SysInfo::with_roots(dir.path(), dir.path());
        assert_eq!(si.net_bytes(""), Some((5300, 7400)));
        assert_eq!(si.net_bytes("lo"), Some((1000, 1000)));
        let mut r = RateMeter::default();
        assert_eq!(r.update_at(10.0, Some((1000, 0))), None);
        assert_eq!(r.update_at(12.0, Some((3000, 500))), Some((1000.0, 250.0)));
    }

    #[test]
    fn temperatures_and_battery() {
        let dir = tempfile::tempdir().unwrap();
        let w = |p: &str, v: &str| {
            let f = dir.path().join(p);
            std::fs::create_dir_all(f.parent().unwrap()).unwrap();
            std::fs::write(f, v).unwrap();
        };
        w("class/hwmon/hwmon0/name", "nvme\n");
        w("class/hwmon/hwmon0/temp1_input", "38850\n");
        w("class/hwmon/hwmon1/name", "coretemp\n");
        w("class/hwmon/hwmon1/temp1_input", "61000\n");
        w("class/hwmon/hwmon1/temp1_label", "Core 0\n");
        w("class/hwmon/hwmon1/temp2_input", "55000\n");
        w("class/hwmon/hwmon1/temp2_label", "Package id 0\n");
        w("class/thermal/thermal_zone0/type", "acpitz\n");
        w("class/thermal/thermal_zone0/temp", "27800\n");
        w("class/power_supply/AC/type", "Mains\n");
        w("class/power_supply/AC/online", "0\n");
        w("class/power_supply/BAT0/type", "Battery\n");
        w("class/power_supply/BAT0/capacity", "81\n");
        w("class/power_supply/BAT0/status", "Discharging\n");
        w("class/power_supply/BAT0/energy_now", "40000000\n");
        w("class/power_supply/BAT0/energy_full", "50000000\n");
        w("class/power_supply/BAT0/power_now", "10000000\n");
        w("class/power_supply/hidpp_battery_0/type", "Battery\n");
        w("class/power_supply/hidpp_battery_0/scope", "Device\n");
        w("class/power_supply/hidpp_battery_0/capacity", "10\n");
        let si = SysInfo::with_roots(dir.path(), dir.path());
        let t = si.temperatures();
        assert_eq!(t, vec![("nvme".to_string(), 38.85), ("coretemp".to_string(), 55.0), ("acpitz".to_string(), 27.8)]);
        assert_eq!(si.temperature(""), Some(55.0));
        assert_eq!(si.temperature("nvm"), Some(38.85));
        let b = si.battery("").unwrap();
        assert_eq!(b, Battery { percent: 81, status: "Discharging".into(), charging: false, plugged: false, hours: Some(4.0) });
        assert_eq!(si.battery("hidpp_battery_0").unwrap().percent, 10);
        assert!(SysInfo::with_roots("/nonexistent", "/nonexistent").battery("").is_none());
    }

    #[test]
    fn bytes_and_disk() {
        assert_eq!(human_bytes(512.0, false), "512 B");
        assert_eq!(human_bytes(1_234_567.0, false), "1.2 MB");
        assert_eq!(human_bytes(123_456_789.0, true), "123 MB/s");
        assert_eq!(human_bytes(5e15, false), "5000 TB");
        let (pct, free, total) = disk("/").unwrap();
        assert!(total > 0 && free <= total && (0.0..=100.0).contains(&pct));
    }
}
