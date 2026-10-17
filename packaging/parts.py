"""Welche Dateien zu welchem Paket gehören.

  freestream  – beide Programme (FreeDeck + FreeWave)
  freedeck    – nur FreeDeck (Stream Deck)
  freewave   – nur FreeWave (Mixer, Wave-Mikrofon)

Jedes Paket enthält den gemeinsamen Teil (COMMON) und ist für sich lauffähig.
Sind freedeck und freewave beide installiert, arbeiten sie zusammen wie freestream.
"""
COMMON = ["__init__.py", "__main__.py", "paths.py", "config.py", "vault.py", "i18n.py", "theme.py", "dialogkit.py",
          "msgbox.py", "system.py", "desktop.py", "parts.py", "updater.py", "updateui.py", "settingsui.py", "backup.py",
          "support.py", "shortcuts.py", "locale"]
FREEDECK = ["app.py", "daemon.py", "engine.py", "actions.py", "catalog.py", "editor.py", "editdoc.py",
           "profiles.py", "render.py", "keyboard.py", "models.py", "plugins.py", "pluginui.py", "ws.py", "obs.py",
           "discord.py", "native.py", "hass.py", "hasskeys.py", "hassui.py", "autoimport.py", "assets.py", "bgui.py",
           "selftest.py", "livekeys.py", "sysinfo.py", "deckexport.py", "mixkeys.py", "iconlib.py", "iconlibui.py",
           "freedecksettings.py", "connectionsui.py", "decksetup.py"]
FREEWAVE = ["freewave.py", "wavehost.py", "waveui.py", "wavedialogs.py", "wavesetup.py", "mixer.py", "effects.py",
            "levels.py", "wavemic.py", "wave3hw.py", "noise.py", "ducking.py"]

PACKAGES = {
    "freestream": COMMON + FREEDECK + FREEWAVE,
    "freedeck": COMMON + FREEDECK,
    "freewave": COMMON + FREEWAVE,
}

if __name__ == "__main__":
    import sys
    print(" ".join(PACKAGES[sys.argv[1]]))
