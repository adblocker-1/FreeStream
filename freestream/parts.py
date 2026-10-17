"""Welche Programme installiert sind.

FreeDeck (Stream Deck) und FreeWave (Mixer, Wave-Mikrofon) lassen sich einzeln
oder zusammen installieren (Pakete freedeck, freewave oder freestream). Was das
andere Programm braucht, wird nur benutzt, wenn es da ist.
"""
import importlib.util
import shutil


def _has(module):
    try:
        return importlib.util.find_spec(f"freestream.{module}") is not None
    except (ImportError, ValueError):
        return False


def has_freedeck():
    """FreeDeck (Stream Deck) ist installiert."""
    return _has("daemon")


def has_freewave():
    """FreeWave (Mixer und Wave-Mikrofon) ist installiert."""
    return _has("mixer")


def command(name):
    """Pfad des Befehls `freedeck`/`freewave` oder None."""
    return shutil.which(name)
