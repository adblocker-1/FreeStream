"""Hintergrunddienste des Daemons (je nach Konfiguration)."""
import logging

log = logging.getLogger("lindeck.services")


def create(daemon):
    out = []
    cfg = daemon.cfg
    if (cfg.get("mixer") or {}).get("enabled"):
        from .mixer import MixerService
        out.append(MixerService(daemon))
    from . import wavemic
    out.append(wavemic.WaveMicService(daemon))
    return out
