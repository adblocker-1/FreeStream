"""FreeStream – Stream Deck und Wave Link für Linux.

Zwei Programme: FreeDeck (Stream Deck, liest Elgato-Profile direkt ein) und
FreeWave (Audio-Mixer und Wave-Mikrofon, Ersatz für Wave Link).
"""

__version__ = "0.40.0"
APP_NAME = "freedeck"


# Einzeln installierte Programme (Pakete freedeck und freewave) liegen in eigenen
# Ordnern; sind beide da, finden sie gegenseitig ihre Module.
def _join_installed_parts():
    import os
    if os.environ.get("FREESTREAM_NO_JOIN"):  # Tests: nur die eigenen Dateien
        return
    bases = os.environ.get("FREESTREAM_JOIN_DIRS")
    bases = bases.split(os.pathsep) if bases else ("/opt/freestream", "/opt/freedeck", "/opt/freewave")
    for base in bases:
        d = os.path.join(base, "freestream")
        if os.path.isfile(os.path.join(d, "__init__.py")) and not any(os.path.realpath(d) == os.path.realpath(p) for p in __path__):
            __path__.append(d)


_join_installed_parts()
