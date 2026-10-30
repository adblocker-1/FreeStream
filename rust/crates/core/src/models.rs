//! Elgato-Gerätecodes: Name, Tastenraster, USB-Product-IDs (wie `freestream/models.py`).

use crate::i18n::gettext;

/// Tastenraster eines Geräts.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct Geometry {
    pub cols: u8,
    pub rows: u8,
    pub dials: u8,
    pub touch_keys: u8,
}

impl Geometry {
    pub const fn new(cols: u8, rows: u8) -> Self {
        Self { cols, rows, dials: 0, touch_keys: 0 }
    }
    pub const fn with_dials(cols: u8, rows: u8, dials: u8) -> Self {
        Self { cols, rows, dials, touch_keys: 0 }
    }
    pub fn keys(&self) -> usize {
        self.cols as usize * self.rows as usize
    }
}

/// Elgato-Modellcode -> (Name, Raster).
pub const MODELS: &[(&str, &str, Geometry)] = &[
    ("20GAA9901", "Stream Deck (Original)", Geometry::new(5, 3)),
    ("20GAA9902", "Stream Deck (Original V2)", Geometry::new(5, 3)),
    ("20GBA9901", "Stream Deck MK.2", Geometry::new(5, 3)),
    ("20GAI9901", "Stream Deck Mini", Geometry::new(3, 2)),
    ("20GAI9902", "Stream Deck Mini MK.2", Geometry::new(3, 2)),
    ("20GAT9901", "Stream Deck XL", Geometry::new(8, 4)),
    ("20GAT9902", "Stream Deck XL V2", Geometry::new(8, 4)),
    ("20GBD9901", "Stream Deck +", Geometry::with_dials(4, 2, 4)),
    ("20GBX9901", "Stream Deck + XL", Geometry::with_dials(9, 4, 6)),
    ("20GBJ9901", "Stream Deck Neo", Geometry { cols: 4, rows: 2, dials: 0, touch_keys: 2 }),
    ("20GBF9901", "Stream Deck Pedal", Geometry::new(3, 1)),
    ("20GBO9901", "Stream Deck Studio", Geometry::with_dials(16, 2, 2)),
];

/// Virtuelles Deck (Fenster): eigener Code je Größe.
pub const VIRTUAL_MODELS: &[(&str, Geometry)] = &[
    ("FDV3X2", Geometry::new(3, 2)),
    ("FDV4X2", Geometry::new(4, 2)),
    ("FDV5X3", Geometry::new(5, 3)),
    ("FDV8X4", Geometry::new(8, 4)),
    ("FDV2X3", Geometry::new(2, 3)),
    ("FDV2X4", Geometry::new(2, 4)),
    ("FDV3X5", Geometry::new(3, 5)),
    ("FDV4X8", Geometry::new(4, 8)),
];

/// USB-Product-ID (Vendor 0x0fd9) -> Modellcode.
pub const PID_TO_MODEL: &[(u16, &str)] = &[
    (0x0060, "20GAA9901"),
    (0x006D, "20GAA9902"),
    (0x0080, "20GBA9901"),
    (0x00A5, "20GBA9901"),
    (0x00B9, "20GBA9901"),
    (0x0063, "20GAI9901"),
    (0x00B3, "20GAI9901"),
    (0x0090, "20GAI9902"),
    (0x00B8, "20GAI9902"),
    (0x006C, "20GAT9901"),
    (0x008F, "20GAT9902"),
    (0x00BA, "20GAT9902"),
    (0x0084, "20GBD9901"),
    (0x00C6, "20GBX9901"),
    (0x009A, "20GBJ9901"),
    (0x0086, "20GBF9901"),
    (0x00AA, "20GBO9901"),
];

pub const ELGATO_VENDOR: u16 = 0x0fd9;

fn base(code: &str) -> &str {
    let b = code.split('-').next().unwrap_or("");
    b.strip_suffix("LL").unwrap_or(b)
}

pub fn is_virtual(code: &str) -> bool {
    VIRTUAL_MODELS.iter().any(|(c, _)| *c == code)
}

pub fn model_for_pid(pid: u16) -> Option<&'static str> {
    PID_TO_MODEL.iter().find(|(p, _)| *p == pid).map(|(_, c)| *c)
}

/// Modellname wie bei Elgato („Stream Deck XL“) – für Plugins.
pub fn elgato_name(code: &str) -> String {
    if is_virtual(code) {
        return "Virtual Deck".into();
    }
    MODELS
        .iter()
        .find(|(c, ..)| *c == base(code))
        .map(|(_, n, _)| (*n).to_string())
        .unwrap_or_else(|| if code.is_empty() { gettext("unknown") } else { code.to_string() })
}

/// Modellname in FreeDeck und FreeWave: „FreeDeck XL“, „FreeDeck +“ …
pub fn model_name(code: &str) -> String {
    if let Some(g) = model_geometry(code).filter(|_| is_virtual(code)) {
        return crate::tr!("Virtual deck {cols} × {rows}", cols = g.cols, rows = g.rows);
    }
    elgato_name(code).replace("Stream Deck", "FreeDeck")
}

pub fn model_geometry(code: &str) -> Option<Geometry> {
    if let Some((_, g)) = VIRTUAL_MODELS.iter().find(|(c, _)| *c == code) {
        return Some(*g);
    }
    MODELS.iter().find(|(c, ..)| *c == base(code)).map(|(.., g)| *g)
}

/// Angeschlossenes Gerät laut USB (sysfs), ohne es zu öffnen.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Connected {
    pub model: String,
    pub name: String,
    pub serial: String,
}

/// Angeschlossene Stream Decks laut `/sys/bus/usb/devices`.
pub fn connected_models() -> Vec<Connected> {
    connected_in(std::path::Path::new("/sys/bus/usb/devices"))
}

pub fn connected_in(root: &std::path::Path) -> Vec<Connected> {
    let read = |d: &std::path::Path, n: &str| std::fs::read_to_string(d.join(n)).map(|s| s.trim().to_string()).unwrap_or_default();
    let mut out = Vec::new();
    let Ok(rd) = std::fs::read_dir(root) else { return out };
    let mut dirs: Vec<_> = rd.flatten().map(|e| e.path()).collect();
    dirs.sort();
    for d in dirs {
        if read(&d, "idVendor").to_lowercase() != "0fd9" {
            continue;
        }
        let Ok(pid) = u16::from_str_radix(&read(&d, "idProduct"), 16) else { continue };
        if let Some(code) = model_for_pid(pid) {
            out.push(Connected { model: code.into(), name: model_name(code), serial: read(&d, "serial") });
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_and_geometry() {
        crate::i18n::set_language("en");
        assert_eq!(model_name("20GAT9902"), "FreeDeck XL V2");
        assert_eq!(model_name("20GBD9901"), "FreeDeck +");
        assert_eq!(model_geometry("20GBD9901-LL").unwrap().dials, 4);
        assert_eq!(model_name("FDV4X2"), "Virtual deck 4 × 2");
        assert_eq!(model_for_pid(0x0080), Some("20GBA9901"));
    }
}
