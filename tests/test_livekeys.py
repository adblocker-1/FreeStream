"""Live-Tasten: Uhr, Countdown, Stoppuhr, Systemmonitor (ohne Gerät, mit falscher Uhr und falschem /proc, /sys)."""
from collections import namedtuple
from datetime import datetime

import pytest

from freestream import actions as A
from freestream import catalog as C
from freestream import desktop, livekeys, native, sysinfo
from freestream.config import DEFAULTS
from freestream.i18n import _, pgettext

SIZES = [(72, 72), (80, 80), (96, 96), (120, 120), (144, 144)]


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class RecSound:
    def __init__(self):
        self.played, self.stopped = [], []

    def play(self, key, path, volume=100, output="", loop=False):
        self.played.append((key, path, volume, loop))

    def is_playing(self, key):
        return False

    def stop(self, key=None):
        self.stopped.append(key)


class Ctrl:
    def __init__(self, preview=False):
        self.preview = preview
        self.redraws = 0

    def redraw_action(self, a):
        self.redraws += 1

    def show_overlay(self, *a, **kw):
        pass


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


STAT1 = "cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 1 2 3 4\n"
STAT2 = "cpu  150 0 150 750 150 0 0 0 0 0\n"   # +100 belegt, +100 Leerlauf -> 50 %
NETDEV = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 5000 10 0 0 0 0 0 0 5000 10 0 0 0 0 0 0
  eth0: 1000 10 0 0 0 0 0 0 2000 10 0 0 0 0 0 0
 wlan0: 3000 10 0 0 0 0 0 0 500 10 0 0 0 0 0 0
docker0: 99999 10 0 0 0 0 0 0 99999 10 0 0 0 0 0 0
"""


@pytest.fixture
def fake(tmp_path, monkeypatch):
    proc, sys_ = tmp_path / "proc", tmp_path / "sys"
    write(proc / "stat", STAT1)
    write(proc / "meminfo", "MemTotal:       16000000 kB\nMemFree:  1000000 kB\nMemAvailable:    4000000 kB\n")
    write(proc / "net" / "dev", NETDEV)
    hw = sys_ / "class" / "hwmon"
    write(hw / "hwmon0" / "name", "acpitz\n")
    write(hw / "hwmon0" / "temp1_input", "30000\n")
    write(hw / "hwmon1" / "name", "coretemp\n")
    write(hw / "hwmon1" / "temp1_input", "54000\n")
    write(hw / "hwmon1" / "temp1_label", "Package id 0\n")
    write(hw / "hwmon1" / "temp2_input", "47000\n")
    write(hw / "hwmon1" / "temp2_label", "Core 0\n")
    write(sys_ / "class" / "thermal" / "thermal_zone0" / "type", "x86_pkg_temp\n")
    write(sys_ / "class" / "thermal" / "thermal_zone0" / "temp", "55000\n")
    ps = sys_ / "class" / "power_supply"
    write(ps / "AC" / "type", "Mains\n")
    write(ps / "AC" / "online", "0\n")
    write(ps / "hidpp_battery_0" / "type", "Battery\n")
    write(ps / "hidpp_battery_0" / "scope", "Device\n")
    write(ps / "hidpp_battery_0" / "capacity", "12\n")
    write(ps / "BAT0" / "type", "Battery\n")
    write(ps / "BAT0" / "capacity", "87\n")
    write(ps / "BAT0" / "status", "Discharging\n")
    write(ps / "BAT0" / "energy_now", "30000000\n")
    write(ps / "BAT0" / "energy_full", "50000000\n")
    write(ps / "BAT0" / "power_now", "10000000\n")
    usage = namedtuple("usage", "total used free")
    info = sysinfo.SysInfo(proc, sys_, disk_usage=lambda p: usage(1000 * 10 ** 9, 250 * 10 ** 9, 750 * 10 ** 9))
    monkeypatch.setattr(livekeys, "INFO", info)
    clk = FakeClock()
    monkeypatch.setattr(livekeys, "clock", clk)
    monkeypatch.setattr(livekeys.Countdown, "use_thread", False)
    livekeys._RUNS.clear()
    info.root = tmp_path
    info.clk = clk
    return info


def make(uuid, ctrl=None, snd=None, **settings):
    data = C.new_action(uuid)
    data["Settings"].update(settings)
    ctx = A.Context(ctrl or Ctrl(), dict(DEFAULTS), None, snd or RecSound())
    a = A.create_action(data, None, ctx)
    a.slot = ("key", 0)
    return a


# ---------------------------------------------------------------- Leser
def test_sysinfo_readers(fake):
    assert fake.cpu_times() == (800, 1000)
    pct, used, total = fake.memory()
    assert round(pct) == 75 and total == 16000000 * 1024 and used == 12000000 * 1024
    assert fake.temperature() == 54.0                     # coretemp, Package id 0 vor acpitz
    assert fake.temperature("acpitz") == 30.0
    assert fake.temperature("x86_pkg") == 55.0            # Teilname eines thermal_zone-Typs
    assert fake.temperature("gibtsnicht") is None
    assert fake.net_bytes() == (4000, 2500)                # ohne lo und docker0
    assert fake.net_bytes("wlan0") == (3000, 500)
    assert fake.net_bytes("eth9") is None
    assert fake.interfaces() == ["eth0", "wlan0"]
    b = fake.battery()
    assert b["percent"] == 87 and b["status"] == "Discharging" and not b["charging"] and not b["plugged"]
    assert b["hours"] == pytest.approx(3.0)
    assert fake.battery("hidpp_battery_0")["percent"] == 12
    assert fake.disk("/") == (25.0, 750 * 10 ** 9, 1000 * 10 ** 9)


def test_sysinfo_missing_files(tmp_path):
    info = sysinfo.SysInfo(tmp_path / "proc", tmp_path / "sys")
    assert info.cpu_times() is None and info.memory() is None and info.temperature() is None
    assert info.net_bytes() is None and info.battery() is None
    assert sysinfo.CpuMeter(info).percent() is None


def test_cpu_and_rate_meters(fake):
    cpu = sysinfo.CpuMeter(fake)
    assert cpu.percent() == pytest.approx(20.0)            # Mittel seit dem Start
    write(fake.root / "proc" / "stat", STAT2)
    assert cpu.percent() == pytest.approx(50.0)
    clk = FakeClock(10.0)
    rm = sysinfo.RateMeter(clock=clk)
    assert rm.update((1000, 0)) is None
    clk.t = 12.0
    assert rm.update((5000, 1000)) == (2000.0, 500.0)
    clk.t = 13.0
    assert rm.update((100, 1000)) == (0.0, 0.0)            # Zähler zurückgesetzt
    assert sysinfo.human_bytes(1234567, True) == "1.2 MB/s"
    assert sysinfo.human_bytes(512) == "512 B"


# ------------------------------------------------------------- Systemmonitor
def test_sysmon_actions(fake):
    for m in livekeys.METRICS:
        uuid = "com.lindeck.sysmon." + m
        assert native.lookup(uuid) is not None and A.is_builtin(uuid)
        a = make(uuid)
        for size in SIZES:
            img = a.frames(size)[0][0]
            assert img.size == size
        a.tick()
    cpu = make("com.lindeck.sysmon.cpu")
    assert cpu.sample()["text"] == "20%"
    write(fake.root / "proc" / "stat", STAT2)
    assert cpu.tick() is True
    fb = cpu.feedback()
    assert fb["value"] == "50%" and fb["indicator"] == pytest.approx(50.0) and fb["title"] == "CPU"
    assert list(cpu.hist) == pytest.approx([20.0, 50.0])
    assert make("com.lindeck.sysmon.memory").sample()["text"] == "75%"
    assert make("com.lindeck.sysmon.temperature").sample()["text"] == "54°C"
    assert make("com.lindeck.sysmon.temperature", unit="f").sample()["text"] == "129°F"
    bat = make("com.lindeck.sysmon.battery").sample()
    assert bat["text"] == "87%" and "3:00" in bat["sub"]
    write(fake.root / "sys" / "class" / "power_supply" / "BAT0" / "status", "Charging\n")
    assert make("com.lindeck.sysmon.battery").sample().get("charging") is True
    disk = make("com.lindeck.sysmon.disk", path="/").sample()
    assert disk["text"] == "25%" and "750" in disk["sub"]
    net = make("com.lindeck.sysmon.network", interface="wlan0")
    assert net.sample()["value"] is None                   # erste Messung
    write(fake.root / "proc" / "net" / "dev", NETDEV.replace(" wlan0: 3000", " wlan0: 5000"))
    fake.clk.t += 2
    r = net.sample()
    assert r["value"] == 1000.0 and r["text"] == "1.0 kB/s"
    assert net.feedback()["value"] == "↓1.0 kB/s"
    net.frames((72, 72))
    # nicht vorhandene Werte: Strich statt Absturz
    gone = make("com.lindeck.sysmon.temperature", sensor="nix")
    assert gone.sample()["text"] == "–"
    gone.frames((72, 72))


# --------------------------------------------------------------------- Uhr
def test_clock(fake, monkeypatch):
    fixed = datetime(2026, 10, 16, 21, 5, 7)
    monkeypatch.setattr(livekeys, "wallclock", lambda tz=None: fixed if tz is None else fixed.replace(tzinfo=tz))
    a = make("com.lindeck.clock.clock")
    assert a.time_text() == "21:05"
    assert a.feedback()["title"].endswith("16.10.")
    assert make("com.lindeck.clock.clock", hour24=False, seconds=True).time_text() == "9:05:07 PM"
    assert make("com.lindeck.clock.clock", date_format="iso")._parts()["date"] == "2026-10-16"
    assert make("com.lindeck.clock.clock", timezone="Europe/Berlin")._parts()["city"] == "Berlin"
    assert make("com.lindeck.clock.clock", timezone="Mars/Olympus")._parts()["city"] == "?"
    for show in ("both", "time", "date"):
        for extra in ({}, {"seconds": True}, {"hour24": False, "seconds": True, "timezone": "Asia/Tokyo"}):
            c = make("com.lindeck.clock.clock", show=show, **extra)
            for size in SIZES:
                assert c.frames(size)[0][0].size == size
    assert a.tick() is True and a.tick() is False          # gleiche Minute: nichts neu zeichnen


# --------------------------------------------------------- Countdown
def test_parse_duration():
    assert livekeys.parse_duration("5:00") == 300
    assert livekeys.parse_duration("1:30:00") == 5400
    assert livekeys.parse_duration("90") == 90
    assert livekeys.parse_duration("90s") == 90
    assert livekeys.parse_duration("1h30m") == 5400
    assert livekeys.parse_duration("2m 5s") == 125
    assert livekeys.parse_duration("quatsch") == 300
    assert livekeys.parse_duration(0) == 300
    assert livekeys.fmt_duration(3725) == "1:02:05" and livekeys.fmt_duration(65) == "1:05"


def test_countdown_state_machine(fake, monkeypatch, tmp_path):
    notes = []
    monkeypatch.setattr(desktop, "notify", lambda title, body, **kw: notes.append((title, body)))
    alarm = tmp_path / "alarm.wav"
    alarm.write_bytes(b"RIFF")
    snd = RecSound()
    clk = fake.clk
    a = make("com.lindeck.clock.countdown", snd=snd, duration="1:00", label="Tea", sound=str(alarm))
    assert a.feedback()["value"] == "1:00" and a.state_text() == "Tea"
    a.key_up(0)                                            # Start
    assert a.st.running
    clk.t += 20.5
    assert a.feedback()["value"] == "0:40"
    a.key_up(0)                                            # Pause
    assert not a.st.running and a.state_text() == pgettext("timer", "paused")
    clk.t += 100
    assert a.feedback()["value"] == "0:40"                 # steht still
    a.key_up(0)                                            # weiter
    clk.t += 39
    a.tick()
    assert not a.st.finished and a.feedback()["value"] == "0:01"
    clk.t += 1
    a.tick()
    assert a.st.finished and not a.st.running
    assert snd.played and snd.played[0][1] == str(alarm)
    assert notes and notes[0][0] == "Tea"
    fr = a.frames((72, 72))
    assert len(fr) == 2 and fr[0][1] == 500                # blinkt
    a.tick()
    assert len(snd.played) == 1                            # Alarm nur einmal
    a.key_up(0)                                            # Druck nach Ablauf: zurücksetzen
    assert not a.st.finished and a.remaining() == 60 and snd.stopped
    assert len(a.frames((72, 72))) == 1
    # langer Druck setzt mitten im Lauf zurück
    a.key_up(0)
    clk.t += 10
    a.key_up(DEFAULTS["long_press_ms"] + 1)
    assert not a.st.running and a.remaining() == 60
    # Dial: Dauer ändern, Tippen startet
    a.dial_rotate(2)
    assert a.remaining() == 120 and a.st.total == 120
    a.dial_rotate(-10)
    assert a.remaining() == 30                             # nicht unter einen Schritt
    a.touch(False)
    assert a.st.running
    a.touch(True)
    assert not a.st.running and a.remaining() == 60        # zurück auf die eingestellte Dauer


def test_countdown_without_sound_and_notification(fake, monkeypatch):
    notes = []
    monkeypatch.setattr(desktop, "notify", lambda *a, **kw: notes.append(a))
    snd = RecSound()
    a = make("com.lindeck.clock.countdown", snd=snd, duration="10s", play_sound=False, notify=False)
    a.key_up(0)
    fake.clk.t += 11
    a.tick()
    assert a.st.finished and not snd.played and not notes


def test_countdown_survives_page_switch_and_timer_thread(fake, monkeypatch):
    monkeypatch.setattr(desktop, "notify", lambda *a, **kw: None)
    data = C.new_action("com.lindeck.clock.countdown")
    ctx = A.Context(Ctrl(), dict(DEFAULTS), None, RecSound())
    a = A.create_action(data, None, ctx)
    a.will_appear()
    a.key_up(0)
    fake.clk.t += 100
    a.will_disappear()
    b = A.create_action(data, None, ctx)                   # Seite wieder geöffnet
    b.will_appear()
    assert b.st.running and b.feedback()["value"] == "3:20"
    # Alarm-Thread: feuert finish() auch ohne Tick
    fake.clk.t += 200
    b._timer_fired()
    assert b.st.finished and ctx.ctrl.redraws
    # Vorschau im Editor legt keinen Zustand an
    livekeys._RUNS.clear()
    p = make("com.lindeck.clock.countdown", ctrl=Ctrl(preview=True))
    p.frames((72, 72))
    assert not livekeys._RUNS


def test_countdown_real_timer_thread(fake, monkeypatch):
    monkeypatch.setattr(livekeys.Countdown, "use_thread", True)
    monkeypatch.setattr(desktop, "notify", lambda *a, **kw: None)
    a = make("com.lindeck.clock.countdown", duration=1)
    a.key_up(0)
    t = a.st.timer
    assert t is not None
    a.key_up(0)                                            # Pause bricht den Timer ab
    t.join(2)
    assert a.st.timer is None and not t.is_alive() and not a.st.finished


# ---------------------------------------------------------- Stoppuhr
def test_stopwatch(fake):
    clk = fake.clk
    a = make("com.lindeck.clock.stopwatch")
    assert a.feedback()["value"] == "0:00" and a.state_text() == _("Stopwatch")
    a.key_up(0)
    clk.t += 65.4
    assert a.tick() is True
    assert a.feedback()["value"] == "1:05" and a.state_text() == pgettext("timer", "running")
    a.key_up(0)                                            # Stopp
    clk.t += 30
    assert a.feedback()["value"] == "1:05" and a.state_text() == pgettext("timer", "paused")
    a.execute()                                            # in einer Multi-Action: weiter
    clk.t += 3600
    assert a.feedback()["value"] == "1:01:05"
    for size in SIZES:
        assert a.frames(size)[0][0].size == size
    a.key_up(DEFAULTS["long_press_ms"])                    # lang: zurücksetzen
    assert a.elapsed() == 0 and not a.st.running


# ----------------------------------------------------------- Katalog
def test_catalog_entries():
    uuids = ["com.lindeck.clock.clock", "com.lindeck.clock.countdown", "com.lindeck.clock.stopwatch"] + \
        ["com.lindeck.sysmon." + m for m in livekeys.METRICS]
    for u in uuids:
        t = C.BY_UUID[u]
        assert t.category in (C.CLOCK_CAT, C.SYSMON_CAT) and C.DIAL in t.controllers
        for f in t.fields:
            if f.kind == "choice":
                assert any(str(v) == str(t.settings.get(f.key)) for v, _n in f.options), (u, f.key)
    assert C.CLOCK_CAT in C.CATEGORIES and C.SYSMON_CAT in C.CATEGORIES
    for name in livekeys.GLYPHS:
        assert livekeys.icon(name, 24, "#ffffff").size == (24, 24)


def test_dial_feedback_and_icon(fake):
    for u in ("com.lindeck.clock.clock", "com.lindeck.clock.countdown", "com.lindeck.clock.stopwatch",
              "com.lindeck.sysmon.cpu", "com.lindeck.sysmon.battery"):
        a = make(u)
        a.slot = ("dial", 0)
        fb = a.feedback()
        assert fb["title"] and fb["value"]
        assert a.encoder_icon_image.size == (48, 48)


# ------------------------------------------------- Engine (simuliertes Stream Deck +)
def test_engine_stream_deck_plus(fake, tmp_path, monkeypatch):
    pytest.importorskip("StreamDeck")
    from StreamDeck.DeviceManager import DeviceManager

    from freestream import editdoc as E
    from freestream.config import Config, State
    from freestream.engine import DeckController
    from freestream.profiles import ProfileStore
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(desktop, "notify", lambda *a, **kw: None)
    store_dir = tmp_path / "profiles"
    store_dir.mkdir()
    doc = E.ProfileDoc(E.create_profile("Live", "20GBD9901", store_dir))
    page = doc.page_ids[0]
    doc.place(page, ("key", 0, 0), "com.lindeck.clock.countdown")
    doc.place(page, ("key", 1, 0), "com.lindeck.sysmon.cpu")
    doc.place(page, ("dial", 0), "com.lindeck.clock.countdown")
    doc.place(page, ("dial", 1), "com.lindeck.sysmon.memory")
    doc.save()
    deck = next(d for d in DeviceManager(transport="dummy").enumerate() if d.deck_type() == "Stream Deck +")
    deck.open()
    st = State()
    st.save = lambda: None
    snd = RecSound()
    ctrl = DeckController(deck, ProfileStore(store_dir), Config(), st, None, snd)
    ctrl.load_profile(doc.id, 0)
    key = ctrl.keys[0]
    assert isinstance(key, livekeys.Countdown) and isinstance(ctrl.keys[1], livekeys.SysMon)
    ctrl._run = lambda fn, *a: fn(*a)                       # synchron statt Thread
    ctrl.on_key(0, True)
    ctrl.on_key(0, False)
    assert key.st.running
    fake.clk.t += 301
    ctrl._second_tick(__import__("time").time())
    assert key.st.finished and 0 in ctrl.anim               # Taste blinkt
    ctrl.on_key(0, True)
    ctrl.on_key(0, False)
    assert not key.st.finished and 0 not in ctrl.anim
    # Dial: Drehen ändert die Dauer, Touchstreifen zeigt Symbol und Restzeit
    dial = ctrl.dials[0]
    ctrl.on_dial_rotate(0, 2)
    assert dial.remaining() == 360
    img = ctrl.touchscreen_image()
    assert img.size == tuple(ctrl.ts_size)
    ctrl.on_dial_push(0, True)
    ctrl.on_dial_push(0, False)
    assert dial.st.running
    ctrl.stop()
