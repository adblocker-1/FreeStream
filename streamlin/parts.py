"""Welche Programme installiert sind.

LinDeck (Stream Deck) und Linewave (Mixer, Wave-Mikrofon) lassen sich einzeln
oder zusammen installieren (Pakete lindeck, linewave oder streamlin). Was das
andere Programm braucht, wird nur benutzt, wenn es da ist.
"""
import importlib.util
import shutil


def _has(module):
    try:
        return importlib.util.find_spec(f"streamlin.{module}") is not None
    except (ImportError, ValueError):
        return False


def has_lindeck():
    """LinDeck (Stream Deck) ist installiert."""
    return _has("daemon")


def has_linewave():
    """Linewave (Mixer und Wave-Mikrofon) ist installiert."""
    return _has("mixer")


def command(name):
    """Pfad des Befehls `lindeck`/`linewave` oder None."""
    return shutil.which(name)
