"""Tests check the German texts (they were written against the German UI)."""
import os

os.environ.setdefault("STREAMLIN_LANG", "de")

# Tests never contact GitHub for updates
os.environ.setdefault("STREAMLIN_NO_UPDATE_CHECK", "1")
os.environ.setdefault("STREAMLIN_NO_SETUP", "1")  # Einrichtungsassistent nur in test_decksetup
os.environ.setdefault("STREAMLIN_SECRETS", "file")  # Schlüsselbund nur in test_vault (mit Attrappe)

import gc  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _collect_tk_images():
    """Nach jedem Test im Haupt-Thread aufräumen.

    Sonst räumt der Garbage Collector Tk-Bilder eines schon geschlossenen Fensters irgendwann
    in einem Hintergrund-Thread des nächsten Tests auf – der Tk-Aufruf aus dem falschen Thread
    blockiert dann diesen Thread (z. B. den Arbeits-Thread von Linewave) für immer.
    """
    yield
    gc.collect()
