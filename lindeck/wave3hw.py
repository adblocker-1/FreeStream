"""Hardware-Einstellungen des Elgato Wave:3 (USB 0fd9:0070) direkt am Mikrofon.

Protokoll nach der öffentlichen Analyse von Kailas Mahavarkar
(github.com/KailasMahavarkar/elgato-wave3-ubuntu, research/dump/PROTOCOL.md,
MIT-Lizenz): USB-Class-Control-Transfers auf Endpoint 0, wIndex 0x3303
(Interface 3 wird vom Audiotreiber nicht belegt – der Ton läuft weiter).

  /config  (wValue 0x0000, 16 Byte, lesen 0x85 / schreiben 0x05)
     0  int16 Q8.8  Gain in dB (0 … 40, Schritt 0,5)
     4  bool        Mikrofon stumm
     5  bool        Clipguard
     6  bool        Low Cut
     7  int16 Q8.8  Kopfhörer in dB (−60 … 0)
     9  bool        Kopfhörer stumm
    10  int16 Q8.8  Mithören Mikro/PC in % (0 … 100, Schritt 5)
    12  u8          Drehregler: 1 Mikro, 2 Kopfhörer, 3 Mix
    13  bool        alle LEDs aus
    14  bool        LEDs umgekehrt (Mikro hängend am Arm)       [ab API 5.3]
    15  bool        Gain-Sperre (Lautstärke vom System ignorieren) [ab API 5.3]

Jede Änderung ist eine Transaktion: Block lesen, ein Feld ändern, ganzen
Block schreiben, zurücklesen. Hat sich ein anderes Byte verändert, wird der
alte Block zurückgeschrieben und ein Fehler gemeldet.
"""
import ctypes
import ctypes.util
import struct
import threading

VID, PID = 0x0FD9, 0x0070
RT_IN, RT_OUT = 0xA1, 0x21
REQ_READ, REQ_WRITE = 0x85, 0x05
WINDEX = 0x3303
SEL_CONFIG, SEL_VERSION = 0x0000, 0x000A
CONFIG_LEN = 16

# Feld: (Offset, Größe, Art, min, max, Schritt)
FIELDS = {
    "gain": (0, 2, "q88", 0.0, 40.0, 0.5),
    "mute": (4, 1, "bool", 0, 1, 1),
    "clipguard": (5, 1, "bool", 0, 1, 1),
    "lowcut": (6, 1, "bool", 0, 1, 1),
    "headphone_volume": (7, 2, "q88", -60.0, 0.0, 0.5),
    "headphone_mute": (9, 1, "bool", 0, 1, 1),
    "direct_monitor": (10, 2, "q88", 0.0, 100.0, 5.0),
    "dial_mode": (12, 1, "u8", 1, 3, 1),
    "leds_off": (13, 1, "bool", 0, 1, 1),
    "leds_flip": (14, 1, "bool", 0, 1, 1),
    "gain_lock": (15, 1, "bool", 0, 1, 1),
}
NEEDS_53 = {"leds_flip", "gain_lock"}
DIAL_MODES = {1: "Mikrofon", 2: "Kopfhörer", 3: "Mix"}


class Wave3Error(RuntimeError):
    pass


def decode(block: bytes) -> dict:
    out = {}
    for name, (off, size, kind, *_r) in FIELDS.items():
        if off + size > len(block):
            continue
        if kind == "q88":
            out[name] = struct.unpack_from("<h", block, off)[0] / 256.0
        elif kind == "bool":
            out[name] = bool(block[off] & 1)
        else:
            out[name] = block[off]
    return out


def encode(block: bytes, name: str, value) -> bytearray:
    off, size, kind, lo, hi, step = FIELDS[name]
    b = bytearray(block)
    if kind == "q88":
        v = max(lo, min(hi, float(value)))
        v = round(v / step) * step
        struct.pack_into("<h", b, off, int(round(v * 256)))
    elif kind == "bool":
        b[off] = 1 if value else 0
    else:
        b[off] = int(max(lo, min(hi, int(value))))
    return b


class _LibUSB:
    def __init__(self):
        path = ctypes.util.find_library("usb-1.0") or "libusb-1.0.so.0"
        try:
            lib = ctypes.CDLL(path)
        except OSError as e:
            raise Wave3Error("libusb-1.0 fehlt (Paket libusb-1.0-0)") from e
        lib.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        lib.libusb_open_device_with_vid_pid.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16]
        lib.libusb_open_device_with_vid_pid.restype = ctypes.c_void_p
        lib.libusb_close.argtypes = [ctypes.c_void_p]
        lib.libusb_close.restype = None
        lib.libusb_control_transfer.argtypes = [ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint16,
                                                ctypes.c_uint16, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint16,
                                                ctypes.c_uint]
        lib.libusb_control_transfer.restype = ctypes.c_int
        self.ctx = ctypes.c_void_p()
        if lib.libusb_init(ctypes.byref(self.ctx)) != 0:
            raise Wave3Error("libusb konnte nicht initialisiert werden")
        self.lib = lib


_usb = {}


def _lib():
    if "obj" not in _usb:
        _usb["obj"] = _LibUSB()
    return _usb["obj"]


class USBTransport:
    """Control-Transfers über libusb (ohne Interface zu beanspruchen)."""

    def __init__(self):
        u = _lib()
        self.lib, self.handle = u.lib, u.lib.libusb_open_device_with_vid_pid(u.ctx, VID, PID)
        if not self.handle:
            raise Wave3Error("Wave:3 nicht gefunden oder kein Zugriff (udev-Regel, neu anmelden)")

    def control(self, req_type, req, value, length_or_data):
        if isinstance(length_or_data, int):
            buf = (ctypes.c_ubyte * length_or_data)()
            n = length_or_data
        else:
            data = bytes(length_or_data)
            buf = (ctypes.c_ubyte * len(data))(*data)
            n = len(data)
        r = self.lib.libusb_control_transfer(self.handle, req_type, req, value, WINDEX, buf, n, 1000)
        if r < 0:
            raise Wave3Error({-3: "kein Zugriff auf das Wave:3 (udev-Regel, neu anmelden)",
                              -4: "Wave:3 getrennt"}.get(r, f"USB-Fehler {r}"))
        return bytes(buf[:r])

    def close(self):
        if self.handle:
            self.lib.libusb_close(self.handle)
            self.handle = None


class Wave3:
    def __init__(self, transport=None):
        self.t = transport or USBTransport()
        self._lock = threading.RLock()
        self.api = self._api_version()

    def _api_version(self):
        try:
            v = self.t.control(RT_IN, REQ_READ, SEL_VERSION, 2)
            return (v[0], v[1]) if len(v) >= 2 else (5, 2)
        except Wave3Error:
            return (5, 2)

    def config_len(self):
        return CONFIG_LEN if self.api >= (5, 3) else 14

    def read(self) -> bytes:
        n = self.config_len()
        b = self.t.control(RT_IN, REQ_READ, SEL_CONFIG, n)
        if len(b) != n:
            raise Wave3Error(f"unerwartete Blocklänge {len(b)}")
        return b

    def get(self) -> dict:
        return decode(self.read())

    def set(self, name, value):
        """Ein Feld setzen (Transaktion mit Rückprüfung). Gibt den neuen Wert zurück."""
        if name not in FIELDS:
            raise Wave3Error(f"Unbekannte Einstellung: {name}")
        if name in NEEDS_53 and self.api < (5, 3):
            raise Wave3Error(f"{name} braucht Firmware-API 5.3 (Gerät: {self.api[0]}.{self.api[1]})")
        off, size = FIELDS[name][0], FIELDS[name][1]
        with self._lock:
            before = self.read()
            want = bytes(encode(before, name, value))
            if want == before:
                return decode(before)[name]
            self.t.control(RT_OUT, REQ_WRITE, SEL_CONFIG, want)
            after = self.read()
            own = set(range(off, off + size))
            bad = [i for i in range(len(want)) if after[i] != want[i] and i not in own]
            if bad:
                self.t.control(RT_OUT, REQ_WRITE, SEL_CONFIG, before)
                raise Wave3Error(f"Gerät hat unerwartet Byte {bad} verändert – Änderung zurückgenommen")
            return decode(after)[name]

    def toggle(self, name):
        return self.set(name, not self.get()[name])

    def change(self, name, delta):
        return self.set(name, self.get()[name] + delta)

    def close(self):
        self.t.close()


def present() -> bool:
    """Ist ein Wave:3 angeschlossen? (über sysfs, ohne USB-Zugriff)"""
    import glob
    for d in glob.glob("/sys/bus/usb/devices/*"):
        try:
            with open(d + "/idVendor") as v, open(d + "/idProduct") as p:
                if v.read().strip() == f"{VID:04x}" and p.read().strip() == f"{PID:04x}":
                    return True
        except OSError:
            continue
    return False
