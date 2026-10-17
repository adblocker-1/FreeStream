"""Streamlin – Stream Deck und Wave Link für Linux.

Zwei Programme: LinDeck (Stream Deck, liest Elgato-Profile direkt ein) und
Linewave (Audio-Mixer und Wave-Mikrofon, Ersatz für Wave Link).
"""

__version__ = "0.27.0"
APP_NAME = "lindeck"


# Einzeln installierte Programme (Pakete lindeck und linewave) liegen in eigenen
# Ordnern; sind beide da, finden sie gegenseitig ihre Module.
def _join_installed_parts():
    import os
    if os.environ.get("STREAMLIN_NO_JOIN"):  # Tests: nur die eigenen Dateien
        return
    bases = os.environ.get("STREAMLIN_JOIN_DIRS")
    bases = bases.split(os.pathsep) if bases else ("/opt/streamlin", "/opt/lindeck", "/opt/linewave")
    for base in bases:
        d = os.path.join(base, "streamlin")
        if os.path.isfile(os.path.join(d, "__init__.py")) and not any(os.path.realpath(d) == os.path.realpath(p) for p in __path__):
            __path__.append(d)


_join_installed_parts()
