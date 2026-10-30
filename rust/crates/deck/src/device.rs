//! Geräte: echte Stream Decks per USB (hidapi) und ein Deck im Speicher (virtuelles Deck, Tests,
//! Vorschau). Der Rest von FreeDeck kennt nur [`Device`].

use elgato_streamdeck::{info::Kind, DeviceStateReader, DeviceStateUpdate, StreamDeck};
use freestream_core::models::{model_for_pid, model_geometry, Geometry};
use image::{DynamicImage, RgbaImage};
use std::sync::{Arc, Mutex};
use std::time::Duration;

/// Eingabe vom Gerät.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Event {
    KeyDown(u8),
    KeyUp(u8),
    DialDown(u8),
    DialUp(u8),
    DialTurn(u8, i8),
    /// Touchstreifen (Stream Deck +): kurzer oder langer Druck bei x
    Touch { x: u16, long: bool },
    /// Wischen über den Touchstreifen (von x nach x)
    Swipe { from: u16, to: u16 },
    /// Touch-Tasten des Neo
    TouchKeyDown(u8),
    TouchKeyUp(u8),
}

/// Ein Deck, wie FreeDeck es sieht.
pub trait Device: Send + Sync {
    /// Elgato-Modellcode (z. B. „20GBA9901“).
    fn model(&self) -> String;
    fn serial(&self) -> String;
    fn geometry(&self) -> Geometry;
    /// Größe eines Tastenbilds in Pixeln.
    fn key_size(&self) -> (u32, u32);
    /// Größe des Touchstreifens (Stream Deck +) oder der Infoleiste (Neo).
    fn lcd_size(&self) -> Option<(u32, u32)> {
        None
    }
    fn has_display(&self) -> bool {
        true
    }
    fn set_key(&self, key: u8, img: &RgbaImage) -> anyhow::Result<()>;
    fn set_lcd(&self, _img: &RgbaImage) -> anyhow::Result<()> {
        Ok(())
    }
    fn set_brightness(&self, percent: u8) -> anyhow::Result<()>;
    /// Gesammelte Bilder ans Gerät schicken.
    fn flush(&self) -> anyhow::Result<()> {
        Ok(())
    }
    /// Auf Eingaben warten (höchstens `timeout`).
    fn read(&self, timeout: Duration) -> anyhow::Result<Vec<Event>>;
    fn firmware(&self) -> String {
        String::new()
    }
}

/// Echtes Stream Deck per USB.
pub struct HidDeck {
    deck: Arc<StreamDeck>,
    reader: Arc<DeviceStateReader>,
    kind: Kind,
    model: String,
    serial: String,
    /// Schreiben und Lesen nicht gleichzeitig (hidapi ist nicht für beides zugleich gedacht)
    io: Mutex<()>,
}

fn kind_model(kind: Kind) -> String {
    model_for_pid(kind.product_id()).map(str::to_string).unwrap_or_else(|| match kind {
        Kind::Mini | Kind::MiniDiscord | Kind::MiniMk2Module => "20GAI9902".into(),
        Kind::Xl | Kind::XlV2 | Kind::XlV2Module => "20GAT9902".into(),
        _ => "20GBA9901".into(),
    })
}

impl HidDeck {
    pub fn open(hid: &hidapi::HidApi, kind: Kind, serial: &str) -> anyhow::Result<Self> {
        let deck = Arc::new(StreamDeck::connect(hid, kind, serial)?);
        let reader = deck.get_reader();
        Ok(Self { deck, reader, kind, model: kind_model(kind), serial: serial.to_string(), io: Mutex::new(()) })
    }
}

// hidapi-Geräte sind nicht für gleichzeitige Zugriffe gebaut: jeder Zugriff auf `deck` und `reader` läuft hier
// unter `io` – damit ist das Teilen zwischen Threads sicher.
unsafe impl Send for HidDeck {}
unsafe impl Sync for HidDeck {}

impl Device for HidDeck {
    fn model(&self) -> String {
        self.model.clone()
    }
    fn serial(&self) -> String {
        self.serial.clone()
    }
    fn geometry(&self) -> Geometry {
        let mut g = model_geometry(&self.model).unwrap_or(Geometry::new(self.kind.column_count(), self.kind.row_count()));
        g.cols = self.kind.column_count();
        g.rows = self.kind.row_count();
        g.dials = self.kind.encoder_count();
        g
    }
    fn key_size(&self) -> (u32, u32) {
        let (w, h) = self.kind.key_image_format().size;
        (w as u32, h as u32)
    }
    fn lcd_size(&self) -> Option<(u32, u32)> {
        self.kind.lcd_strip_size().map(|(w, h)| (w as u32, h as u32))
    }
    fn has_display(&self) -> bool {
        self.kind.is_visual()
    }
    fn set_key(&self, key: u8, img: &RgbaImage) -> anyhow::Result<()> {
        let (w, h) = self.key_size();
        let img = crate::render::fit(img, w, h);
        let _io = self.io.lock().unwrap();
        self.deck.set_button_image(key, DynamicImage::ImageRgba8(img))?;
        Ok(())
    }
    fn set_lcd(&self, img: &RgbaImage) -> anyhow::Result<()> {
        let Some(fmt) = self.kind.lcd_image_format() else { return Ok(()) };
        let data = elgato_streamdeck::images::convert_image_with_format(fmt, DynamicImage::ImageRgba8(img.clone()))?;
        let _io = self.io.lock().unwrap();
        self.deck.write_lcd_fill(&data)?;
        Ok(())
    }
    fn set_brightness(&self, percent: u8) -> anyhow::Result<()> {
        let _io = self.io.lock().unwrap();
        self.deck.set_brightness(percent.min(100))?;
        Ok(())
    }
    fn flush(&self) -> anyhow::Result<()> {
        let _io = self.io.lock().unwrap();
        self.deck.flush()?;
        Ok(())
    }
    fn read(&self, timeout: Duration) -> anyhow::Result<Vec<Event>> {
        let updates = {
            let _io = self.io.lock().unwrap();
            self.reader.read(Some(timeout))?
        };
        Ok(updates
            .into_iter()
            .map(|u| match u {
                DeviceStateUpdate::ButtonDown(k) => Event::KeyDown(k),
                DeviceStateUpdate::ButtonUp(k) => Event::KeyUp(k),
                DeviceStateUpdate::EncoderDown(i) => Event::DialDown(i),
                DeviceStateUpdate::EncoderUp(i) => Event::DialUp(i),
                DeviceStateUpdate::EncoderTwist(i, t) => Event::DialTurn(i, t),
                DeviceStateUpdate::TouchPointDown(i) => Event::TouchKeyDown(i),
                DeviceStateUpdate::TouchPointUp(i) => Event::TouchKeyUp(i),
                DeviceStateUpdate::TouchScreenPress(x, _) => Event::Touch { x, long: false },
                DeviceStateUpdate::TouchScreenLongPress(x, _) => Event::Touch { x, long: true },
                DeviceStateUpdate::TouchScreenSwipe((a, _), (b, _)) => Event::Swipe { from: a, to: b },
            })
            .collect())
    }
    fn firmware(&self) -> String {
        let _io = self.io.lock().unwrap();
        self.deck.firmware_version().unwrap_or_default()
    }
}

/// Angeschlossene Stream Decks öffnen (Gerätedateien /dev/hidraw* brauchen die udev-Regel aus dem Paket).
pub fn discover(hid: &mut hidapi::HidApi) -> Vec<(Kind, String)> {
    let _ = elgato_streamdeck::refresh_device_list(hid);
    elgato_streamdeck::list_devices(hid)
}

/// Deck im Speicher: zeigt Bilder nicht an, sondern merkt sie sich (virtuelles Deck, Tests, Vorschau).
pub struct MemoryDeck {
    pub model_code: String,
    pub serial_no: String,
    pub size: (u32, u32),
    pub keys: Mutex<Vec<Option<RgbaImage>>>,
    pub brightness: Mutex<u8>,
    pending: Mutex<Vec<Event>>,
    wake: std::sync::Condvar,
}

impl MemoryDeck {
    pub fn new(model: &str, serial: &str, size: (u32, u32)) -> Self {
        let n = model_geometry(model).map(|g| g.keys()).unwrap_or(15);
        Self {
            model_code: model.into(),
            serial_no: serial.into(),
            size,
            keys: Mutex::new(vec![None; n]),
            brightness: Mutex::new(70),
            pending: Mutex::new(Vec::new()),
            wake: std::sync::Condvar::new(),
        }
    }

    /// Eingabe einspeisen (Klick im virtuellen Deck, Tests).
    pub fn push(&self, ev: Event) {
        self.pending.lock().unwrap().push(ev);
        self.wake.notify_all();
    }

    pub fn key_image(&self, key: u8) -> Option<RgbaImage> {
        self.keys.lock().unwrap().get(key as usize).cloned().flatten()
    }
}

impl Device for MemoryDeck {
    fn model(&self) -> String {
        self.model_code.clone()
    }
    fn serial(&self) -> String {
        self.serial_no.clone()
    }
    fn geometry(&self) -> Geometry {
        model_geometry(&self.model_code).unwrap_or(Geometry::new(5, 3))
    }
    fn key_size(&self) -> (u32, u32) {
        self.size
    }
    fn set_key(&self, key: u8, img: &RgbaImage) -> anyhow::Result<()> {
        let mut keys = self.keys.lock().unwrap();
        if let Some(slot) = keys.get_mut(key as usize) {
            *slot = Some(img.clone());
        }
        Ok(())
    }
    fn set_brightness(&self, percent: u8) -> anyhow::Result<()> {
        *self.brightness.lock().unwrap() = percent;
        Ok(())
    }
    fn read(&self, timeout: Duration) -> anyhow::Result<Vec<Event>> {
        let mut p = self.pending.lock().unwrap();
        if p.is_empty() {
            p = self.wake.wait_timeout(p, timeout).unwrap().0;
        }
        Ok(std::mem::take(&mut *p))
    }
}
