"""Erzeugt die Bilder für die README (docs/images/*.png, englische Oberfläche).

Aufruf:  xvfb-run -a -s "-screen 0 1400x1000x24" python3 docs/screenshots.py

Alles läuft mit einem Demo-Profil, simuliertem PipeWire und simuliertem
Wave:3 – es werden keine echten Geräte gebraucht und keine Elgato-Grafiken
verwendet (die Symbole zeichnet dieses Skript selbst).
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("LINDECK_LANG", "en")
OUT = ROOT / "docs" / "images"
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def font(size, bold=True):
    try:
        return ImageFont.truetype(FONT if bold else FONT_REG, size)
    except OSError:
        return ImageFont.load_default()


def pump(root, seconds):
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.02)


def grab(root, name):
    """Fensterinhalt als PNG speichern (ImageMagick `import`)."""
    root.update()
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    subprocess.run(["import", "-window", str(root.winfo_id()), str(path)], check=True)
    Image.open(path).convert("RGB").save(path, optimize=True)
    print("geschrieben:", path.relative_to(ROOT))


# ---------------------------------------------------------------- Symbole
def icon(path, color, glyph):
    """Einfaches eigenes Tastensymbol: farbige Fläche mit Zeichen."""
    im = Image.new("RGBA", (144, 144), color + (255,))
    d = ImageDraw.Draw(im)
    w = (255, 255, 255, 255)
    if glyph == "mic":
        d.rounded_rectangle((56, 24, 88, 84), radius=16, fill=w)
        d.arc((40, 52, 104, 104), 0, 180, fill=w, width=8)
        d.rectangle((68, 104, 76, 120), fill=w)
        d.line((30, 120, 114, 24), fill=(255, 69, 58, 255), width=10)
    elif glyph == "rec":
        d.ellipse((40, 40, 104, 104), fill=(255, 69, 58, 255), outline=w, width=6)
    elif glyph == "live":
        d.rounded_rectangle((22, 50, 122, 94), radius=12, outline=w, width=6)
        d.text((72, 72), "LIVE", font=font(26), fill=w, anchor="mm")
    elif glyph == "scene":
        d.rectangle((26, 36, 118, 100), outline=w, width=6)
        d.polygon([(40, 92), (64, 60), (82, 80), (94, 68), (108, 92)], fill=w)
    elif glyph == "play":
        d.polygon([(52, 36), (52, 108), (110, 72)], fill=w)
    elif glyph == "music":
        d.ellipse((34, 86, 62, 110), fill=w)
        d.ellipse((82, 74, 110, 98), fill=w)
        d.rectangle((54, 32, 62, 98), fill=w)
        d.rectangle((102, 22, 110, 86), fill=w)
        d.polygon([(54, 32), (110, 20), (110, 38), (54, 50)], fill=w)
    elif glyph == "chat":
        d.rounded_rectangle((24, 30, 120, 96), radius=18, fill=w)
        d.polygon([(46, 92), (40, 120), (70, 94)], fill=w)
    elif glyph == "web":
        d.ellipse((30, 30, 114, 114), outline=w, width=6)
        d.ellipse((54, 30, 90, 114), outline=w, width=5)
        d.line((30, 72, 114, 72), fill=w, width=5)
    elif glyph == "folder":
        d.polygon([(24, 44), (60, 44), (70, 54), (120, 54), (120, 110), (24, 110)], fill=w)
    elif glyph == "light":
        d.ellipse((44, 24, 100, 80), fill=w)
        d.rectangle((58, 80, 86, 104), fill=w)
    elif glyph == "copy":
        d.rectangle((40, 30, 96, 96), outline=w, width=6)
        d.rectangle((56, 48, 112, 114), fill=color + (255,), outline=w, width=6)
    elif glyph == "lock":
        d.arc((46, 26, 98, 82), 180, 360, fill=w, width=9)
        d.rounded_rectangle((36, 60, 108, 116), radius=10, fill=w)
    out = Image.new("RGBA", (144, 144), color + (255,))
    out.alpha_composite(im.resize((100, 100), Image.LANCZOS), (22, 6))
    out.save(path)
    return path


# ------------------------------------------------------------- LinDeck
def shot_lindeck(tmp):
    import tkinter as tk
    from streamlin import catalog as C
    from streamlin import editdoc as E
    from streamlin import editor
    store = tmp / "profiles"
    store.mkdir()
    doc = E.ProfileDoc(E.create_profile("Streaming", "20GBD9901", store))  # Stream Deck +
    page = doc.page_ids[0]
    icons = tmp / "icons"
    icons.mkdir()
    keys = [
        (("key", 0, 0), "com.lindeck.wavemic.control", "Mute", (229, 72, 77), "mic", {"function": "mute"}),
        (("key", 1, 0), "com.lindeck.obs.record", "Record", (60, 60, 68), "rec", {"mode": "toggle"}),
        (("key", 2, 0), "com.lindeck.obs.stream", "Live", (191, 90, 242), "live", {"mode": "toggle"}),
        (("key", 3, 0), "com.lindeck.obs.scene", "Camera", (10, 132, 255), "scene", {"scene": "Camera"}),
        (("key", 0, 1), "com.lindeck.media.player", "Spotify", (48, 209, 88), "music", {}),
        (("key", 1, 1), "com.elgato.streamdeck.system.website", "Twitch", (100, 65, 165), "web",
         {"path": "https://twitch.tv", "openInBrowser": True}),
        (("key", 2, 1), "com.lindeck.discord.voice", "Discord", (88, 101, 242), "chat", {"function": "mute"}),
        (("key", 3, 1), "com.elgato.streamdeck.system.hotkey", "Copy", (255, 159, 10), "copy", None),
    ]
    for slot, uuid, title, color, glyph, settings in keys:
        a = doc.place(page, slot, uuid)
        if settings:
            a.setdefault("Settings", {}).update(settings)
        if uuid.endswith("hotkey"):
            doc.set_hotkey(page, slot, 0, C.parse_hotkey("Ctrl+C"))
        path = icon(icons / f"{glyph}.png", color, glyph)
        doc.set_icon(page, slot, 0, path)
        doc.update_state(page, slot, 0, "Title", title)
        doc.update_state(page, slot, 0, "TitleAlignment", "bottom")
    for i, (uuid, title, settings) in enumerate((
            ("com.lindeck.mixer.channel", "Music", {"channel": "music", "mix": "stream"}),
            ("com.lindeck.mixer.channel", "Game", {"channel": "game", "mix": "monitor"}),
            ("com.lindeck.wavemic.control", "Gain", {"function": "gain"}),
            ("com.elgato.volume-controller.output-device-control", "Volume", {}))):
        a = doc.place(page, ("dial", i), uuid)
        a.setdefault("Settings", {}).update(settings)
        doc.update_state(page, ("dial", i), 0, "Title", title)
    doc.add_page()
    doc.add_page()
    doc.save()
    others = {}
    for name, code, pages in (("Gaming", "20GBD9901", 2), ("Video editing", "20GBD9901", 1),
                              ("Meetings", "20GAI9902", 1)):
        d = E.ProfileDoc(E.create_profile(name, code, store))
        for _i in range(pages - 1):
            d.add_page()
        d.save()
        others[name] = d.id

    from streamlin.models import Geometry
    from streamlin.profiles import ProfileStore
    from test_app import FakeCtrl
    ctrl = FakeCtrl("DL41K1A04321", "20GBD9901", Geometry(4, 2, 4), doc.id)
    ctrl.brightness = 70

    profiles = ProfileStore(store)

    class Daemon:  # angeschlossenes Stream Deck + (simuliert)
        controllers = {"a": ctrl}
        store = profiles
        cfg = {"devices": {"A00SA4212345": {"profile": others["Meetings"]}}}  # bekannt, nicht angeschlossen
    root = tk.Tk(className="lindeck")
    root.geometry("1280x800")
    app = editor.EditorApp(root, store_root=store, daemon=Daemon())
    pump(root, 1.5)
    app.select(("key", 0, 0))
    pump(root, 1.5)
    grab(root, "lindeck-window.png")
    shot_background(tmp, root, app, doc)
    shot_plugins(tmp, root, Daemon)
    dlg = app.open_settings()
    pump(root, 1.0)
    grab(dlg, "settings.png")
    dlg.destroy()
    app.close()
    try:
        root.after_cancel(app._dev_job)
        root.destroy()
    except tk.TclError:
        pass


def wallpaper(w, h):
    """Eigenes Beispiel-Hintergrundbild: Abendhimmel mit Sonne und Bergen."""
    im = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(im)
    top, mid, bot = (38, 20, 84), (196, 66, 120), (255, 158, 76)
    for y in range(h):
        t = y / (h - 1)
        a, b, u = (top, mid, t / 0.6) if t < 0.6 else (mid, bot, (t - 0.6) / 0.4)
        d.line([(0, y), (w, y)], fill=tuple(round(x + (z - x) * u) for x, z in zip(a, b)))
    cx, cy, r = w * 0.62, h * 0.62, h * 0.26
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 214, 120))
    for i in range(6):
        y = cy + r * (0.15 + i * 0.16)
        d.rectangle((cx - r, y, cx + r, y + r * 0.05 * (i + 1)), fill=(196, 66, 120))
    d.polygon([(0, h), (0, h * 0.72), (w * 0.18, h * 0.55), (w * 0.38, h * 0.74), (w * 0.55, h * 0.6),
               (w * 0.8, h * 0.78), (w, h * 0.66), (w, h)], fill=(30, 16, 56))
    return im


def shot_background(tmp, root, app, _doc):
    """Zweite Seite mit Hintergrundbild über alle Tasten und eigenem Touchstreifen."""
    doc = app.doc
    from streamlin.bgui import BackgroundDialog
    from streamlin.theme import glyph
    wp = tmp / "abendhimmel.jpg"
    wallpaper(1600, 900).save(wp, quality=92)
    strip = tmp / "streifen.png"
    wallpaper(1600, 900).crop((0, 600, 1600, 800)).save(strip)
    pid = doc.add_page()
    for (c, r), name, title in (((0, 0), "music", "Music"), ((1, 0), "chat", "Discord"), ((3, 1), "record", "Rec")):
        f = tmp / f"t_{name}.png"
        g = glyph(name, 120, "#ffffff")
        im = Image.new("RGBA", (144, 144), (0, 0, 0, 0))
        im.alpha_composite(g.resize((80, 80), Image.LANCZOS), (32, 18))
        im.save(f)
        doc.place(pid, ("key", c, r), "com.elgato.streamdeck.system.hotkey")
        doc.set_icon(pid, ("key", c, r), 0, f)
        doc.update_state(pid, ("key", c, r), 0, "Title", title)
        doc.update_state(pid, ("key", c, r), 0, "TitleAlignment", "bottom")
    doc.set_background(pid, wp, dim=15)
    doc.set_strip_background(pid, strip)
    doc.save()
    app.goto_page(pid)
    app.select(("key", 2, 0))
    pump(root, 1.5)
    grab(root, "lindeck-background.png")
    dlg = BackgroundDialog(root, app, pid)
    pump(root, 1)
    grab(dlg, "lindeck-background-dialog.png")
    dlg.destroy()
    app.goto_page(doc.page_ids[0])
    pump(root, 0.5)


def shot_plugins(tmp, root, daemon):
    """Plugin-Verwaltung mit zwei Demo-Plugins (selbst erzeugt)."""
    from streamlin import paths
    from streamlin.pluginui import PluginManager
    paths.PLUGINS_DIR = tmp / "plugins"
    demos = [("de.example.timer", "Pomodoro-Timer", "Shows a timer on the key and rings when it is up.",
              "bin/plugin.js", (255, 107, 90), "rec"),
             ("de.example.homeassist", "Smart Home", "Switches lights and scenes via the local hub.",
              "bin/plugin.js", (48, 209, 88), "light"),
             ("de.example.winonly", "Windows tool",
              "A plugin built for Windows only.",
              "Tool.exe", (142, 155, 176), "keyboard")]
    for uid, name, desc, code, color, glyph in demos:
        d = paths.PLUGINS_DIR / f"{uid}.sdPlugin"
        (d / "bin").mkdir(parents=True)
        icon(d / "icon.png", color, glyph)
        (d / "manifest.json").write_text(json.dumps({
            "UUID": uid, "Name": name, "Description": desc, "Author": "Example", "Version": "1.2.0",
            "CodePath": code, "Icon": "icon", "Nodejs": {"Version": "20"},
            "Actions": [{"UUID": f"{uid}.a{i}", "Name": "Action {i}".format(i=i), "States": [{}]} for i in range(3)]}))

    class Proc:
        def __init__(self, ok):
            self.proc = type("P", (), {"poll": lambda s: None})()
            self.conn = object() if ok else None

    class Host:
        node = "/usr/bin/node"
        procs = {"de.example.timer": Proc(True), "de.example.homeassist": Proc(False)}

        def sync(self):
            pass
    dlg = PluginManager(root, host=Host())
    dlg.geometry("740x600")
    pump(root, 1.5)
    grab(dlg, "lindeck-plugins.png")
    dlg.destroy()


# ------------------------------------------------------------ Linewave
def shot_linewave(tmp):
    import tkinter as tk
    import test_waveapp as TW
    from streamlin import effects, mixer, paths, system, wave3hw
    for n, s in (("STATE_DIR", "state"), ("CONFIG_DIR", "config"), ("CONFIG_FILE", "config/config.json"),
                 ("DATA_DIR", "data")):
        setattr(paths, n, tmp / s)
    (tmp / "state").mkdir(exist_ok=True)
    (tmp / "config").mkdir(exist_ok=True)
    channels = [dict(c) for c in mixer.DEFAULT_CHANNELS[:4]] + [
        {"id": "podcast", "name": "Podcast guest", "icon": "chat", "color": "#ff6bd5"},
        {"id": "capture", "name": "Console", "kind": "input", "source": "alsa_input.usb-capture", "icon": "game",
         "color": "#ff8a3d"}]
    hp, gate = effects.lookup("builtin:highpass"), effects.lookup("builtin:highshelf")
    fx = {"mic": [effects.new_effect(hp), effects.new_effect(gate)], "music": [effects.new_effect(gate)]}
    (tmp / "config" / "config.json").write_text(json.dumps({"mixer": {"enabled": True, "channels": channels,
                                                                       "effects": fx}}))
    fake = TW.FakePactl()
    fake.sinks[0]["description"] = "Headphones"  # Beispieldaten der Tests sind deutsch
    for inp in fake.inputs.values():
        if inp["props"].get("application.name") == "Mein Spiel":
            inp["props"]["application.name"] = "My Game"
    fake.inputs["9"] = {"index": "9", "sink": "1", "volume": 80, "mute": False,
                        "props": {"application.name": "Firefox", "application.process.binary": "firefox"}}
    fake.inputs["10"] = {"index": "10", "sink": "1", "volume": 80, "mute": False,
                         "props": {"application.name": "Discord", "application.process.binary": "discord"}}
    blocks = fake.blocks

    def blocks_with_sources(what):
        if what == "sources":
            return [{"name": "alsa_input.usb-Elgato_Wave_3", "description": "Elgato Wave:3 Mono"},
                    {"name": "alsa_input.usb-capture", "description": "USB Capture HDMI – Digital Stereo"},
                    {"name": "alsa_input.pci-analog", "description": "Built-in Audio Analog Stereo"}]
        return blocks(what)
    system._pactl_blocks = blocks_with_sources
    system.have = lambda c: True
    mixer._run = fake
    mixer.time.sleep = lambda s: None
    effects.Effects.start = lambda self, c, src: self.node_name(c["id"])  # keine echten Filterprozesse
    wave3hw.present = lambda: True
    wave3hw.USBTransport = lambda: TW.FakeWave3()
    from streamlin.wavehost import WaveHost
    from streamlin.waveui import WaveWindow
    host = WaveHost()
    host.start()
    host.tick()
    for cid, mix, vol in (("music", "stream", 35), ("music", "monitor", 70), ("podcast", "stream", 85),
                          ("voice", "stream", 0), ("browser", "monitor", 55), ("capture", "monitor", 40)):
        host.mixer.set_volume(cid, mix, vol)
    host.mixer.set_mute("voice", "stream", True)
    import types
    demo = {"music": .78, "browser": .55, "voice": .72, "game": .8, "system": .5, "podcast": .62, "capture": .66,
            "mic": .82, "streammix": .74, "headphones": .8}

    def fake_level(device):  # feste Pegel statt parec, damit das Bild immer gleich aussieht
        key = next((k for k in demo if k in device), "")
        return types.SimpleNamespace(level=demo.get(key, .5) if "Wave" in device or key else .6, stop=lambda: None)
    root = tk.Tk(className="linewave")
    win = WaveWindow(root, host, tray=False, level_factory=fake_level)
    pump(root, 3)
    grab(root, "linewave-mixer.png")
    win.show_view("mic")
    pump(root, 1)
    grab(root, "linewave-microphone.png")
    win.show_view("mixer")

    from streamlin import wavesetup
    data = wavesetup.collect(host)
    data["catalog"] = effects.catalog()
    wiz = wavesetup.SetupWizard(win, data)
    for step, name in ((1, "linewave-setup-microphone.png"), (3, "linewave-setup-sound.png")):
        wiz.step = step
        wiz.render()
        if step == 1 and wiz.meter is not None:
            wiz.meter.level, wiz.meter.peak = 0.58, 0.71
            wiz.meter_hint.configure(text="")
        pump(root, 1)
        grab(wiz, name)
    wiz.destroy()

    from streamlin.wavedialogs import ChannelDialog, EffectsDialog
    dlg = ChannelDialog(root, host.mixer.inputs(), on_save=lambda v: None)
    dlg.name.set("Console")
    dlg.kind_seg.set("input")
    dlg._kind("input")
    dlg.dev.set("USB Capture HDMI – Digital Stereo")
    dlg.icon.set("game")
    dlg.color.set("#ff8a3d")
    dlg.icon.set_color("#ff8a3d")
    pump(root, 1)
    grab(dlg, "linewave-channel.png")
    dlg.destroy()

    cat = effects.catalog()
    chain = [effects.new_effect(p) for p in (effects.lookup("builtin:highpass", cat),
                                            next((p for p in cat if p["label"] == "gate"), None),
                                            next((p for p in cat if p["label"] == "sc4m"), None)) if p]
    mic = host.mixer.channel("mic")
    dlg = EffectsDialog(root, mic, chain, cat, on_change=None, on_control=None)
    dlg.geometry("620x760")
    pump(root, 1.5)
    grab(dlg, "linewave-effects.png")
    dlg.destroy()

    root.destroy()


# ------------------------------------------------------------ Schaubild
def diagram():
    """Schaubild: Programme → Kanäle → Monitor-Mix / Stream-Mix."""
    import math
    W, H = 1200, 600
    bg, fg, muted = (27, 28, 31), (232, 232, 234), (154, 156, 165)
    blue, purple = (64, 140, 230), (175, 95, 220)
    im = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(im)
    f_head, f_box, f_small = font(26), font(19), font(15, bold=False)
    d.text((W // 2, 34), "How Linewave works", font=f_head, fill=fg, anchor="mm")

    def box(x, y, w, h, title, sub="", color=(46, 48, 54)):
        d.rounded_rectangle((x, y, x + w, y + h), radius=12, fill=color)
        d.text((x + w / 2, y + (h / 2 if not sub else h / 2 - 10)), title, font=f_box, fill=fg, anchor="mm")
        if sub:
            d.text((x + w / 2, y + h / 2 + 14), sub, font=f_small, fill=(210, 210, 220), anchor="mm")

    def arrow(a, b, color, width=3):
        d.line((a, b), fill=color, width=width)
        ang = math.atan2(b[1] - a[1], b[0] - a[0])
        d.polygon([(b[0] - 13 * math.cos(ang - 0.45), b[1] - 13 * math.sin(ang - 0.45)),
                   (b[0] - 13 * math.cos(ang + 0.45), b[1] - 13 * math.sin(ang + 0.45)), b], fill=color)

    def slider(x, y, level, color):
        d.rounded_rectangle((x, y, x + 10, y + 44), radius=4, fill=(20, 20, 24))
        top = y + 44 - int(44 * level)
        d.rounded_rectangle((x, top, x + 10, y + 44), radius=4, fill=color)

    d.text((120, 84), "Programs", font=f_small, fill=muted, anchor="mm")
    d.text((470, 84), "Channels in Linewave (2 sliders each)", font=f_small, fill=muted, anchor="mm")
    d.text((990, 84), "Outputs", font=f_small, fill=muted, anchor="mm")
    apps = [("Spotify", "Music", .7, .35), ("Firefox", "Browser", .55, .0),
            ("Discord", "Voice Chat", .9, .0), ("Game", "Game", .8, .6),
            ("Wave mic", "Microphone", .0, .9)]
    colors = [(48, 209, 88), (10, 132, 255), (88, 101, 242), (255, 159, 10), (229, 72, 77)]
    bus_m, bus_s = 690, 740
    ys = []
    for i, ((app, ch, m, st), col) in enumerate(zip(apps, colors)):
        y = 108 + i * 90
        ys.append(y)
        box(30, y, 180, 64, app)
        arrow((210, y + 32), (318, y + 32), (120, 122, 130))
        d.rounded_rectangle((320, y, 640, y + 64), radius=12, fill=tuple(int(c * 0.42) for c in col))
        d.rounded_rectangle((320, y, 328, y + 64), radius=4, fill=col)
        d.text((345, y + 32), f"Linewave {ch}", font=f_box, fill=fg, anchor="lm")
        slider(586, y + 10, m, blue)
        slider(608, y + 10, st, purple)
        d.line((640, y + 22, bus_m, y + 22), fill=blue, width=3)
        d.line((640, y + 42, bus_s, y + 42), fill=purple, width=3)
    d.line((bus_m, ys[0] + 22, bus_m, ys[-1] + 22), fill=blue, width=4)
    d.line((bus_s, ys[0] + 42, bus_s, ys[-1] + 42), fill=purple, width=4)
    box(860, 150, 260, 110, "Monitor Mix", "Headphones / speakers", (34, 84, 150))
    box(860, 360, 260, 110, "Stream Mix", "OBS / Discord", (110, 50, 140))
    arrow((bus_m, 205), (858, 205), blue, 4)
    arrow((bus_s, 415), (858, 415), purple, 4)
    d.text((598, 560), "blue = Monitor slider (what you hear)      purple = Stream slider (what your audience hears)",
           font=f_small, fill=muted, anchor="mm")
    OUT.mkdir(parents=True, exist_ok=True)
    im.save(OUT / "linewave-flow.png", optimize=True)
    print("geschrieben:", (OUT / "linewave-flow.png").relative_to(ROOT))


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp())
    only = sys.argv[1:]
    if not only or "diagram" in only:
        diagram()
    if not only or "lindeck" in only:
        shot_lindeck(tmp)
    if not only or "linewave" in only:
        shot_linewave(tmp)
