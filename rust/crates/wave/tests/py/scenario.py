"""Gleicher Ablauf wie im Rust-Test `python_compat.rs`, hier mit der Python-Version von FreeWave.

Ein simuliertes pactl (wie FakePactl in tests/test_waveapp.py) zeichnet alle Befehle auf; ausgegeben
werden Befehle, Einstellungen, Zustand und Ergebnisse als JSON – der Rust-Test vergleicht sie mit seinen.

Aufruf: python3 scenario.py <Arbeitsordner> <Teil>   (Umgebung: FREESTREAM_LANG=en, XDG_* auf den Ordner)
"""
import json
import re
import sys

from freestream import mixer, system, paths, effects, noise, wavemic


class FakePactl:
    def __init__(self):
        self.cmds, self.next_id = [], 100
        self.sinks = [{"index": "1", "name": "alsa_output.headphones", "description": "Kopfhörer"},
                      {"index": "2", "name": "alsa_output.speakers", "description": "Lautsprecher"}]
        self.inputs = {"7": {"index": "7", "sink": "1", "volume": 80, "mute": False,
                             "props": {"application.name": "Spotify", "application.process.binary": "spotify"}},
                       "8": {"index": "8", "sink": "1", "volume": 80, "mute": False,
                             "props": {"application.name": "Mein Spiel", "application.process.binary": "game.exe"}},
                       "9": {"index": "9", "sink": "1", "volume": 80, "mute": False,
                             "props": {"application.name": "Firefox", "application.process.binary": "firefox-esr"}}}

    def _sink(self, name):
        return next((s["index"] for s in self.sinks if s["name"] == name), "0")

    def __call__(self, args):
        self.cmds.append(list(args))
        if args[0] == "load-module":
            self.next_id += 1
            kv = dict(a.split("=", 1) for a in args[2:] if "=" in a)
            if args[1] == "module-null-sink":
                self.sinks.append({"index": str(self.next_id), "name": kv["sink_name"], "description": ""})
            name = next((re.search(r"linewave\.loop='([^']+)'", a).group(1) for a in args
                         if a.startswith("sink_input_properties=")), None)
            if name:
                self.inputs[str(self.next_id)] = {"index": str(self.next_id), "volume": 100, "mute": False,
                                                  "sink": self._sink(kv.get("sink", "")),
                                                  "props": {"linewave.loop": name, "media.name": "lesbar"}}
            return str(self.next_id)
        if args[0] == "get-default-sink":
            return "alsa_output.headphones"
        if args[0] == "get-default-source":
            return "alsa_input.mic"
        if args[0] == "set-sink-input-volume":
            self.inputs[args[1]]["volume"] = int(args[2].rstrip("%"))
        if args[0] == "set-sink-input-mute":
            b = self.inputs[args[1]]
            b["mute"] = (not b["mute"]) if args[2] == "toggle" else args[2] == "1"
        if args[0] == "move-sink-input":
            self.inputs[args[1]]["sink"] = self._sink(args[2])
        if args[0] == "unload-module":
            gone = [x["index"] for x in self.sinks if x["index"] == args[1]]
            self.sinks = [x for x in self.sinks if x["index"] != args[1]]
            self.inputs.pop(args[1], None)
            for b in self.inputs.values():  # wie PipeWire: Streams eines verschwundenen Sinks aufs Standardgerät
                if b["sink"] in gone:
                    b["sink"] = "1"
        return ""

    def blocks(self, what):
        if what == "sink-inputs":
            return [json.loads(json.dumps(b)) for b in self.inputs.values()]
        if what == "sinks":
            return [dict(s) for s in self.sinks]
        if what == "sources":
            return [{"index": "50", "name": "alsa_input.usb-capture", "description": "USB Capture", "props": {}},
                    {"index": "51", "name": "alsa_output.headphones.monitor", "description": "Monitor", "props": {}}]
        return []


def main():
    part = sys.argv[2] if len(sys.argv) > 2 else "main"
    fake = FakePactl()
    system._pactl_blocks = fake.blocks
    system.have = lambda c: True
    mixer._run = fake
    mixer.time.sleep = lambda s: None
    mixer.WATCH = False
    effects.catalog = lambda refresh=False: [dict(b, type="builtin", inputs=["In"], outputs=["Out"])
                                             for b in effects.BUILTIN]
    noise.available = lambda: []
    from freestream.config import Config
    cfg = Config()
    mx = mixer.Mixer(cfg)
    out = {}
    if part == "main":
        mx.setup()
        out["after_setup"] = len(fake.cmds)
        mx.set_volume("music", "monitor", 70)
        mx.set_mute("mic", "stream", True)
        out["chat"] = mx.add_mix("Chat-Mix", icon="chat", color="#22c55e", virtual=True)
        mx.set_member("music", "chatmix", False)
        mx.set_member("music", "chatmix", True)
        mx.update_mix("chatmix", devices=["alsa_output.speakers"])
        out["listen"] = [mx.listen("chatmix"), mx.listening(), mx.listen("monitor")]
        out["capture"] = mx.add_channel("Capture", kind="input", source="alsa_input.usb-capture")
        mx.set_master("music", 140)
        mx.set_master_mute("music", True)
        mx.set_master("mic", 70)
        out["routed"] = mx.route_streams()
        out["streams"] = mx.streams()
        out["monitor"] = mx.set_monitor_output("next")
        out["scene"] = mx.save_scene("Gaming")
        mx.set_volume("music", "monitor", 20)
        mx.apply_scene("gaming")
        out["active_scene"] = (mx.active_scene() or {}).get("id")
        mx.set_linked("game", True)
        mx.set_volume("game", "monitor", 50)
        out["routes"] = mx.channel_routes("browser", known=["firefox-esr"])
        out["moved"] = mx.set_channel_routes("sfx", add=[("Spotify", "Spotify")], remove=["firefox"])
        mx.remove_mix("chatmix")
        mx.update_channel("music", name="Musik 2")
        mx.remove_channel("capture")
        out["levels"] = mx.levels()
        out["masters"] = mx.masters()
        out["level_sources"] = mx.level_sources()
        out["mixes"] = mx.mixes()
        out["channels"] = mx.all_channels()
        mx.teardown()
    elif part == "noise":
        out["status"] = mx.noise.set(enabled=True, backend="webrtc", strength=73.6)
        out["rnnoise"] = noise.rnnoise_conf("alsa_input.usb-Elgato_Wave_3", "/usr/lib/ladspa/librnnoise_ladspa.so",
                                            42, "VAD Threshold (%)")
        out["webrtc"] = noise.webrtc_conf("alsa_input.usb-Elgato_Wave_3")
        out["wavemic"] = [wavemic.filter_conf("alsa_input.wave", True, 120, True),
                          wavemic.filter_conf("alsa_input.wave", True, 80, False),
                          wavemic.filter_conf("alsa_input.wave", False, 80, True)]
        chain = [effects.new_effect(effects.lookup("builtin:highpass")),
                 {"plugin": "builtin:peaking", "name": "EQ", "enabled": True,
                  "controls": {"Freq": 2500.5, "Q": 1.25, "Gain": -3, "Unknown": 1}},
                 {"plugin": "builtin:lowpass", "name": "off", "enabled": False, "controls": {}},
                 {"plugin": "ladspa:sc4m_1916.so:sc4m", "name": "Comp", "enabled": True,
                  "controls": {"Ratio (1:n)": 3.0, "Threshold level (dB)": -20}}]
        cat = effects.catalog() + [{"id": "ladspa:sc4m_1916.so:sc4m", "type": "ladspa",
                                    "plugin": "/usr/lib/ladspa/sc4m_1916.so", "label": "sc4m", "name": "SC4",
                                    "info": "", "inputs": ["Input"], "outputs": ["Output"],
                                    "controls": [{"name": "Ratio (1:n)", "min": 1, "max": 20, "default": 1},
                                                 {"name": "Threshold level (dB)", "min": -30, "max": 0,
                                                  "default": 0}]},
                                   {"id": "lv2:urn:x", "type": "lv2", "plugin": "urn:x", "label": "x", "name": "X",
                                    "info": "", "inputs": ["in_l", "in_r"], "outputs": ["out_l", "out_r"],
                                    "controls": [{"name": "gain", "min": 0, "max": 1, "default": 0.5}]}]
        out["fx_monitor"] = effects.conf("music", "Music", "linewave_music.monitor", chain, cat)
        out["fx_mic"] = effects.conf("mic", "Mic \"1\"", "alsa_input.mic", chain[:2], cat)
        chain2 = chain + [{"plugin": "lv2:urn:x", "name": "X", "enabled": True, "controls": {"gain": 0.25}}]
        out["fx_stage"] = effects.conf("mic", "Mic", "alsa_input.mic", chain2, cat, stage=2)
        out["chain"] = chain
        mx.fx.set_chain("mic", chain)
        mx.set_linked("music", True)
        mx.save_scene("Leer") if False else None
    print(json.dumps({"cmds": fake.cmds, "out": out, "config": cfg.data.get("mixer"),
                      "state": json.loads((paths.STATE_DIR / "mixer.json").read_text()) if (paths.STATE_DIR / "mixer.json").exists() else None},
                     default=str))


main()
