"""Welche Dateien zu welchem Paket gehören.

  streamlin  – beide Programme (LinDeck + Linewave)
  lindeck    – nur LinDeck (Stream Deck)
  linewave   – nur Linewave (Mixer, Wave-Mikrofon)

Jedes Paket enthält den gemeinsamen Teil (COMMON) und ist für sich lauffähig.
Sind lindeck und linewave beide installiert, arbeiten sie zusammen wie streamlin.
"""
COMMON = ["__init__.py", "__main__.py", "paths.py", "config.py", "i18n.py", "theme.py", "dialogkit.py", "msgbox.py",
          "system.py", "desktop.py", "parts.py", "updater.py", "updateui.py", "settingsui.py", "backup.py",
          "support.py", "shortcuts.py", "locale"]
LINDECK = ["app.py", "daemon.py", "engine.py", "actions.py", "catalog.py", "editor.py", "editdoc.py",
           "profiles.py", "render.py", "keyboard.py", "models.py", "plugins.py", "pluginui.py", "ws.py", "obs.py",
           "discord.py", "native.py", "hass.py", "hasskeys.py", "hassui.py", "autoimport.py", "assets.py", "bgui.py",
           "selftest.py", "livekeys.py", "sysinfo.py", "deckexport.py", "mixkeys.py", "iconlib.py", "iconlibui.py",
           "lindecksettings.py"]
LINEWAVE = ["linewave.py", "wavehost.py", "waveui.py", "wavedialogs.py", "wavesetup.py", "mixer.py", "effects.py",
            "levels.py", "wavemic.py", "wave3hw.py", "noise.py", "ducking.py"]

PACKAGES = {
    "streamlin": COMMON + LINDECK + LINEWAVE,
    "lindeck": COMMON + LINDECK,
    "linewave": COMMON + LINEWAVE,
}

if __name__ == "__main__":
    import sys
    print(" ".join(PACKAGES[sys.argv[1]]))
