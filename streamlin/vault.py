"""Zugangsdaten im Schlüsselbund des Systems (Secret Service: GNOME-Schlüsselbund, KWallet).

Über das Programm `secret-tool` (Paket libsecret-tools) – ohne zusätzliche Python-Module.
Ist kein Schlüsselbund erreichbar, bleiben die Daten wie bisher in config.json (Rechte 0600).
STREAMLIN_SECRETS=file schaltet den Schlüsselbund ab (Tests, Fehlersuche).
"""
import logging
import os
import shutil
import subprocess

log = logging.getLogger("streamlin.secrets")

SERVICE = "streamlin"
# (Abschnitt in config.json, Feld) – diese Felder kommen in den Schlüsselbund
FIELDS = (("obs", "password"), ("discord", "client_secret"), ("homeassistant", "token"))
TIMEOUT = 5


def _tool():
    if os.environ.get("STREAMLIN_SECRETS", "").lower() == "file":
        return None
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS") and not os.path.exists(f"/run/user/{os.getuid()}/bus"):
        return None
    return shutil.which("secret-tool")


def available() -> bool:
    return _tool() is not None


def _run(args, data=None):
    tool = _tool()
    if tool is None:
        return None
    try:
        r = subprocess.run([tool, *args], input=data, capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("secret-tool: %s", e)
        return None
    return r


def lookup(key: str):
    """Gespeicherten Wert lesen; None, wenn es ihn nicht gibt oder der Schlüsselbund fehlt."""
    r = _run(["lookup", "service", SERVICE, "key", key])
    if r is None or r.returncode != 0:
        return None
    return r.stdout.rstrip("\n") or None


def store(key: str, value: str) -> bool:
    r = _run(["store", "--label", f"Streamlin ({key})", "service", SERVICE, "key", key], data=value)
    ok = r is not None and r.returncode == 0
    if not ok and r is not None:
        log.info("Schlüsselbund: %s", (r.stderr or "").strip())
    return ok


def clear(key: str) -> bool:
    r = _run(["clear", "service", SERVICE, "key", key])
    return r is not None and r.returncode == 0
