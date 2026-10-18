"""Tests check the German texts (they were written against the German UI)."""
import os

os.environ.setdefault("FREESTREAM_LANG", "de")

# Tests never contact GitHub for updates
os.environ.setdefault("FREESTREAM_NO_UPDATE_CHECK", "1")
os.environ.setdefault("FREESTREAM_NO_SETUP", "1")  # Einrichtungsassistent nur in test_decksetup
os.environ.setdefault("FREESTREAM_SECRETS", "file")  # Schlüsselbund nur in test_vault (mit Attrappe)

import gc  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _collect_tk_images():
    """Nach jedem Test im Haupt-Thread aufräumen.

    Sonst räumt der Garbage Collector Tk-Bilder eines schon geschlossenen Fensters irgendwann
    in einem Hintergrund-Thread des nächsten Tests auf – der Tk-Aufruf aus dem falschen Thread
    blockiert dann diesen Thread (z. B. den Arbeits-Thread von FreeWave) für immer.
    """
    yield
    from freestream import system  # Ereignis-Threads von FreeWave (PulseWatch) nicht in den nächsten Test mitnehmen
    for w in list(system.PulseWatch.instances):
        w.stop()
    gc.collect()
    gc.enable()  # theme.gc_in_main_thread schaltet sie ab, solange ein Fenster lebt


@pytest.fixture(autouse=True)
def _forget_recent_loops():
    """Gemerkte Loopbacks (mixer._RECENT) gelten nur für den laufenden Test."""
    from freestream import mixer
    mixer._RECENT["at"] = 0.0
    mixer._WRITTEN.clear()
    mixer.WATCH = False  # kein pactl subscribe im Hintergrund (eigener Test in test_deckgui)
    yield
    mixer._RECENT["at"] = 0.0
    mixer._WRITTEN.clear()
