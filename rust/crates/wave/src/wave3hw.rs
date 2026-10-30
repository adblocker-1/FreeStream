//! Hardware-Einstellungen des Elgato Wave:3 (USB 0fd9:0070) direkt am Mikrofon – wie
//! `freestream/wave3hw.py`.
//!
//! Protokoll nach der öffentlichen Analyse von Kailas Mahavarkar
//! (github.com/KailasMahavarkar/elgato-wave3-ubuntu, research/dump/PROTOCOL.md, MIT-Lizenz):
//! USB-Class-Control-Transfers auf Endpoint 0, wIndex 0x3303 (Interface 3 wird vom Audiotreiber nicht
//! belegt – der Ton läuft weiter).
//!
//! Das sind Audio-Class-Anfragen (GET_MEM 0x85 / SET_MEM 0x05), keine HID-Reports – hidapi kann sie
//! nicht senden. Wie die Python-Version wird libusb-1.0 zur Laufzeit geladen (dlopen); ohne libusb
//! meldet [`Wave3::open`] einen verständlichen Fehler.
//!
//! ```text
//!   /config  (wValue 0x0000, 16 Byte, lesen 0x85 / schreiben 0x05)
//!      0  int16 Q8.8  Gain in dB (0 … 40, Schritt 0,5)
//!      4  bool        Mikrofon stumm
//!      5  bool        Clipguard
//!      6  bool        Low Cut
//!      7  int16 Q8.8  Kopfhörer in dB (−60 … 0)
//!      9  bool        Kopfhörer stumm
//!     10  int16 Q8.8  Mithören Mikro/PC in % (0 … 100, Schritt 5)
//!     12  u8          Drehregler: 1 Mikro, 2 Kopfhörer, 3 Mix
//!     13  bool        alle LEDs aus
//!     14  bool        LEDs umgekehrt (Mikro hängend am Arm)         [ab API 5.3]
//!     15  bool        Gain-Sperre (Lautstärke vom System ignorieren) [ab API 5.3]
//! ```
//!
//! Jede Änderung ist eine Transaktion: Block lesen, ein Feld ändern, ganzen Block schreiben, zurücklesen.
//! Hat sich ein anderes Byte verändert, wird der alte Block zurückgeschrieben und ein Fehler gemeldet.

use std::sync::Mutex;

use freestream_core::tr;
use indexmap::IndexMap;

pub const VID: u16 = 0x0FD9;
pub const PID: u16 = 0x0070;
pub const RT_IN: u8 = 0xA1;
pub const RT_OUT: u8 = 0x21;
pub const REQ_READ: u8 = 0x85;
pub const REQ_WRITE: u8 = 0x05;
pub const WINDEX: u16 = 0x3303;
pub const SEL_CONFIG: u16 = 0x0000;
pub const SEL_VERSION: u16 = 0x000A;
pub const CONFIG_LEN: usize = 16;

/// Art eines Feldes.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Kind {
    Q88,
    Bool,
    U8,
}

/// Feld: (Name, Offset, Größe, Art, min, max, Schritt)
pub const FIELDS: [(&str, usize, usize, Kind, f64, f64, f64); 11] = [
    ("gain", 0, 2, Kind::Q88, 0.0, 40.0, 0.5),
    ("mute", 4, 1, Kind::Bool, 0.0, 1.0, 1.0),
    ("clipguard", 5, 1, Kind::Bool, 0.0, 1.0, 1.0),
    ("lowcut", 6, 1, Kind::Bool, 0.0, 1.0, 1.0),
    ("headphone_volume", 7, 2, Kind::Q88, -60.0, 0.0, 0.5),
    ("headphone_mute", 9, 1, Kind::Bool, 0.0, 1.0, 1.0),
    ("direct_monitor", 10, 2, Kind::Q88, 0.0, 100.0, 5.0),
    ("dial_mode", 12, 1, Kind::U8, 1.0, 3.0, 1.0),
    ("leds_off", 13, 1, Kind::Bool, 0.0, 1.0, 1.0),
    ("leds_flip", 14, 1, Kind::Bool, 0.0, 1.0, 1.0),
    ("gain_lock", 15, 1, Kind::Bool, 0.0, 1.0, 1.0),
];
pub const NEEDS_53: [&str; 2] = ["leds_flip", "gain_lock"];

/// Namen der Drehregler-Modi.
pub fn dial_mode_name(mode: u8) -> String {
    match mode {
        1 => tr!("Microphone"),
        2 => tr!("Headphones"),
        3 => tr!("Mix"),
        _ => mode.to_string(),
    }
}

/// Fehler beim Zugriff auf das Wave:3.
#[derive(Debug, Clone, PartialEq)]
pub struct Wave3Error(pub String);

impl std::fmt::Display for Wave3Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for Wave3Error {}

/// Wert eines Feldes (Kommazahl, Schalter oder Zahl).
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum FieldValue {
    Num(f64),
    Bool(bool),
    Int(u8),
}

impl FieldValue {
    pub fn as_f64(self) -> f64 {
        match self {
            FieldValue::Num(f) => f,
            FieldValue::Bool(b) => f64::from(u8::from(b)),
            FieldValue::Int(i) => f64::from(i),
        }
    }
    pub fn as_bool(self) -> bool {
        self.as_f64() != 0.0
    }
}

fn field(name: &str) -> Option<(usize, usize, Kind, f64, f64, f64)> {
    FIELDS.iter().find(|f| f.0 == name).map(|f| (f.1, f.2, f.3, f.4, f.5, f.6))
}

/// Block → Felder (nur die, die ganz im Block liegen).
pub fn decode(block: &[u8]) -> IndexMap<String, FieldValue> {
    let mut out = IndexMap::new();
    for (name, off, size, kind, ..) in FIELDS {
        if off + size > block.len() {
            continue;
        }
        let v = match kind {
            Kind::Q88 => FieldValue::Num(f64::from(i16::from_le_bytes([block[off], block[off + 1]])) / 256.0),
            Kind::Bool => FieldValue::Bool(block[off] & 1 != 0),
            Kind::U8 => FieldValue::Int(block[off]),
        };
        out.insert(name.to_string(), v);
    }
    out
}

/// Ein Feld im Block setzen (begrenzt und auf die Schrittweite gerundet).
pub fn encode(block: &[u8], name: &str, value: f64) -> Vec<u8> {
    let mut b = block.to_vec();
    let Some((off, _size, kind, lo, hi, step)) = field(name) else { return b };
    match kind {
        Kind::Q88 => {
            let v = value.clamp(lo, hi);
            let v = crate::mixer::py_round(v / step) as f64 * step;
            let raw = crate::mixer::py_round(v * 256.0) as i16;
            b[off..off + 2].copy_from_slice(&raw.to_le_bytes());
        }
        Kind::Bool => b[off] = u8::from(value != 0.0),
        Kind::U8 => b[off] = (value as i64).clamp(lo as i64, hi as i64) as u8,
    }
    b
}

/// Übertragungsweg (austauschbar für Tests).
pub trait Transport: Send {
    /// Control-Transfer: `Ok(Daten)`; bei OUT mit `data`, bei IN mit `len`.
    fn control(&mut self, req_type: u8, req: u8, value: u16, data: Option<&[u8]>, len: usize)
               -> Result<Vec<u8>, Wave3Error>;
    fn close(&mut self) {}
}

// ------------------------------------------------------------------ libusb über dlopen
type InitFn = unsafe extern "C" fn(*mut *mut libc::c_void) -> libc::c_int;
type OpenFn = unsafe extern "C" fn(*mut libc::c_void, u16, u16) -> *mut libc::c_void;
type CloseFn = unsafe extern "C" fn(*mut libc::c_void);
type ControlFn =
    unsafe extern "C" fn(*mut libc::c_void, u8, u8, u16, u16, *mut u8, u16, libc::c_uint) -> libc::c_int;

struct LibUsb {
    ctx: *mut libc::c_void,
    open: OpenFn,
    close: CloseFn,
    control: ControlFn,
}

// SAFETY: der libusb-Kontext ist threadsicher; Zugriffe laufen ohnehin über einen Mutex
unsafe impl Send for LibUsb {}

static USB: Mutex<Option<Result<LibUsbPtr, Wave3Error>>> = Mutex::new(None);

#[derive(Clone, Copy)]
struct LibUsbPtr(*const LibUsb);
unsafe impl Send for LibUsbPtr {}

fn libusb() -> Result<&'static LibUsb, Wave3Error> {
    let mut g = USB.lock().unwrap();
    if g.is_none() {
        *g = Some(load_libusb().map(|l| LibUsbPtr(Box::leak(Box::new(l)) as *const LibUsb)));
    }
    match g.as_ref().unwrap() {
        // SAFETY: einmal angelegt und nie freigegeben
        Ok(p) => Ok(unsafe { &*p.0 }),
        Err(e) => Err(e.clone()),
    }
}

fn load_libusb() -> Result<LibUsb, Wave3Error> {
    let missing = || Wave3Error(tr!("libusb-1.0 is missing (package libusb-1.0-0)"));
    unsafe {
        let mut lib = std::ptr::null_mut();
        for name in [c"libusb-1.0.so.0", c"libusb-1.0.so"] {
            lib = libc::dlopen(name.as_ptr(), libc::RTLD_NOW);
            if !lib.is_null() {
                break;
            }
        }
        if lib.is_null() {
            return Err(missing());
        }
        let sym = |n: &std::ffi::CStr| libc::dlsym(lib, n.as_ptr());
        let (i, o, c, t) = (sym(c"libusb_init"), sym(c"libusb_open_device_with_vid_pid"), sym(c"libusb_close"),
                            sym(c"libusb_control_transfer"));
        if i.is_null() || o.is_null() || c.is_null() || t.is_null() {
            return Err(missing());
        }
        let init = std::mem::transmute::<*mut libc::c_void, InitFn>(i);
        let mut ctx = std::ptr::null_mut();
        if init(&mut ctx) != 0 {
            return Err(Wave3Error(tr!("libusb could not be initialized")));
        }
        Ok(LibUsb {
            ctx,
            open: std::mem::transmute::<*mut libc::c_void, OpenFn>(o),
            close: std::mem::transmute::<*mut libc::c_void, CloseFn>(c),
            control: std::mem::transmute::<*mut libc::c_void, ControlFn>(t),
        })
    }
}

/// Control-Transfers über libusb (ohne ein Interface zu beanspruchen).
pub struct UsbTransport {
    handle: *mut libc::c_void,
}

// SAFETY: das Handle wird nur über &mut self benutzt
unsafe impl Send for UsbTransport {}

impl UsbTransport {
    pub fn open() -> Result<Self, Wave3Error> {
        let u = libusb()?;
        let handle = unsafe { (u.open)(u.ctx, VID, PID) };
        if handle.is_null() {
            return Err(Wave3Error(tr!("Wave:3 not found or no access (udev rule, log in again)")));
        }
        Ok(Self { handle })
    }
}

impl Transport for UsbTransport {
    fn control(&mut self, req_type: u8, req: u8, value: u16, data: Option<&[u8]>, len: usize)
               -> Result<Vec<u8>, Wave3Error> {
        let u = libusb()?;
        let mut buf = match data {
            Some(d) => d.to_vec(),
            None => vec![0u8; len],
        };
        let n = buf.len() as u16;
        let r = unsafe { (u.control)(self.handle, req_type, req, value, WINDEX, buf.as_mut_ptr(), n, 1000) };
        if r < 0 {
            return Err(Wave3Error(match r {
                -3 => tr!("no access to the Wave:3 (udev rule, log in again)"),
                -4 => tr!("Wave:3 disconnected"),
                _ => tr!("USB error {code}", code = r),
            }));
        }
        buf.truncate(r as usize);
        Ok(buf)
    }

    fn close(&mut self) {
        if !self.handle.is_null() {
            if let Ok(u) = libusb() {
                unsafe { (u.close)(self.handle) };
            }
            self.handle = std::ptr::null_mut();
        }
    }
}

impl Drop for UsbTransport {
    fn drop(&mut self) {
        Transport::close(self);
    }
}

/// Ein angeschlossenes Wave:3.
pub struct Wave3 {
    t: Mutex<Box<dyn Transport>>,
    /// Firmware-API (Haupt-, Nebenversion)
    pub api: (u8, u8),
}

impl Wave3 {
    /// Über USB öffnen.
    pub fn open() -> Result<Self, Wave3Error> {
        Ok(Self::with(Box::new(UsbTransport::open()?)))
    }

    /// Mit eigenem Übertragungsweg (Tests).
    pub fn with(mut t: Box<dyn Transport>) -> Self {
        let api = match t.control(RT_IN, REQ_READ, SEL_VERSION, None, 2) {
            Ok(v) if v.len() >= 2 => (v[0], v[1]),
            _ => (5, 2),
        };
        Self { t: Mutex::new(t), api }
    }

    pub fn config_len(&self) -> usize {
        if self.api >= (5, 3) {
            CONFIG_LEN
        } else {
            14
        }
    }

    fn read_with(&self, t: &mut Box<dyn Transport>) -> Result<Vec<u8>, Wave3Error> {
        let n = self.config_len();
        let b = t.control(RT_IN, REQ_READ, SEL_CONFIG, None, n)?;
        if b.len() != n {
            return Err(Wave3Error(tr!("unexpected block length {length}", length = b.len())));
        }
        Ok(b)
    }

    pub fn read(&self) -> Result<Vec<u8>, Wave3Error> {
        let mut t = self.t.lock().unwrap();
        self.read_with(&mut t)
    }

    pub fn get(&self) -> Result<IndexMap<String, FieldValue>, Wave3Error> {
        Ok(decode(&self.read()?))
    }

    /// Ein Feld setzen (Transaktion mit Rückprüfung). Gibt den neuen Wert zurück.
    pub fn set(&self, name: &str, value: f64) -> Result<FieldValue, Wave3Error> {
        let Some((off, size, ..)) = field(name) else {
            return Err(Wave3Error(tr!("Unknown setting: {name}", name = name)));
        };
        if NEEDS_53.contains(&name) && self.api < (5, 3) {
            return Err(Wave3Error(tr!("{name} needs firmware API 5.3 (device: {version})", name = name,
                                      version = format!("{}.{}", self.api.0, self.api.1))));
        }
        let mut t = self.t.lock().unwrap();
        let before = self.read_with(&mut t)?;
        let want = encode(&before, name, value);
        if want == before {
            return Ok(decode(&before)[name]);
        }
        t.control(RT_OUT, REQ_WRITE, SEL_CONFIG, Some(&want), want.len())?;
        let after = self.read_with(&mut t)?;
        let bad: Vec<usize> = (0..want.len()).filter(|i| after[*i] != want[*i] && !(off..off + size).contains(i)).collect();
        if !bad.is_empty() {
            t.control(RT_OUT, REQ_WRITE, SEL_CONFIG, Some(&before), before.len())?;
            return Err(Wave3Error(tr!("Device unexpectedly changed byte {bytes} – change reverted",
                                      bytes = format!("{bad:?}"))));
        }
        Ok(decode(&after)[name])
    }

    pub fn toggle(&self, name: &str) -> Result<FieldValue, Wave3Error> {
        let cur = self.get()?.get(name).copied().map(FieldValue::as_bool).unwrap_or(false);
        self.set(name, if cur { 0.0 } else { 1.0 })
    }

    pub fn change(&self, name: &str, delta: f64) -> Result<FieldValue, Wave3Error> {
        let cur = self.get()?.get(name).copied().map(FieldValue::as_f64).unwrap_or(0.0);
        self.set(name, cur + delta)
    }

    pub fn close(&self) {
        self.t.lock().unwrap().close();
    }
}

/// Ist ein Wave:3 angeschlossen? (über sysfs, ohne USB-Zugriff)
pub fn present() -> bool {
    let Ok(rd) = std::fs::read_dir("/sys/bus/usb/devices") else { return false };
    rd.flatten().any(|d| {
        let rd = |f: &str| std::fs::read_to_string(d.path().join(f)).map(|s| s.trim().to_string()).unwrap_or_default();
        rd("idVendor") == format!("{VID:04x}") && rd("idProduct") == format!("{PID:04x}")
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decode_encode() {
        let mut block = vec![0u8; 16];
        block[0..2].copy_from_slice(&((20.5 * 256.0) as i16).to_le_bytes());
        block[4] = 1;
        block[7..9].copy_from_slice(&((-12.0 * 256.0) as i16).to_le_bytes());
        block[12] = 2;
        let d = decode(&block);
        assert_eq!(d["gain"], FieldValue::Num(20.5));
        assert_eq!(d["mute"], FieldValue::Bool(true));
        assert_eq!(d["headphone_volume"], FieldValue::Num(-12.0));
        assert_eq!(d["dial_mode"], FieldValue::Int(2));
        let b = encode(&block, "gain", 33.3);
        assert_eq!(decode(&b)["gain"], FieldValue::Num(33.5));
        let b = encode(&block, "gain", 99.0);
        assert_eq!(decode(&b)["gain"], FieldValue::Num(40.0));
        let b = encode(&block, "direct_monitor", 47.0);
        assert_eq!(decode(&b)["direct_monitor"], FieldValue::Num(45.0));
        assert_eq!(decode(&block[..14]).len(), 9);
    }
}
