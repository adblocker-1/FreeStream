"""Discord-Steuerung über die lokale RPC-Schnittstelle (IPC-Socket der Desktop-App).

Discord erlaubt Stummschalten/Taubschalten per RPC nur angemeldeten Apps.
Dafür einmalig eine eigene App anlegen (kostenlos, ohne Freigabe nutzbar
für den eigenen Account):

  1. https://discord.com/developers/applications → „New Application“
  2. Unter OAuth2 die Redirect-URL  http://localhost  eintragen
  3. Client-ID und Client-Secret in config.json eintragen:
       "discord": {"client_id": "…", "client_secret": "…"}

Beim ersten Tastendruck fragt Discord einmal nach der Erlaubnis; der Zugang
wird danach gespeichert (~/.local/state/freestream/discord-token.json).
"""
import json
import logging
import os
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid as uuidlib
from pathlib import Path

from . import paths
from .i18n import _

log = logging.getLogger("freestream.discord")

OP_HANDSHAKE, OP_FRAME, OP_CLOSE, OP_PING, OP_PONG = 0, 1, 2, 3, 4
SCOPES = ["rpc", "rpc.voice.read", "rpc.voice.write"]
TOKEN_URL = "https://discord.com/api/oauth2/token"


class DiscordError(Exception):
    pass


class DiscordAuthError(DiscordError):
    """Discord lehnt die Zugangsdaten ab (Zugang widerrufen, abgelaufen, andere App) – nicht: offline."""


def socket_paths():
    base = [os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}", os.environ.get("TMPDIR") or "/tmp"]
    subs = ["", "app/com.discordapp.Discord", "snap.discord", ".flatpak/com.discordapp.Discord/xdg-run",
            "app/dev.vencord.Vesktop"]
    out = []
    for b in base:
        for s in subs:
            for i in range(10):
                p = Path(b) / s / f"discord-ipc-{i}"
                if p.exists():
                    out.append(p)
    return out


class DiscordClient:
    def __init__(self, client_id, client_secret, token_file=None, sock_paths=None):
        self.client_id, self.client_secret = str(client_id), client_secret or ""
        self.token_file = Path(token_file or paths.STATE_DIR / "discord-token.json")
        self.sock_paths = sock_paths
        self.sock = None
        self.ready = False
        self.voice = {"mute": False, "deaf": False}
        self.version = 0
        self._pending = {}
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._connecting = threading.Lock()

    # ---------------------------------------------------------- Transport
    def _send(self, op, payload):
        data = json.dumps(payload).encode()
        with self._send_lock:
            self.sock.sendall(struct.pack("<II", op, len(data)) + data)

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise DiscordError(_("Discord closed the connection"))
            buf += chunk
        return buf

    def _recv(self):
        op, n = struct.unpack("<II", self._recv_exact(8))
        return op, json.loads(self._recv_exact(n) or b"{}")

    def _reader(self, sock):
        try:
            while self.sock is sock:
                op, msg = self._recv()
                if op == OP_PING:
                    self._send(OP_PONG, msg)
                    continue
                if op == OP_CLOSE:
                    break
                nonce = msg.get("nonce")
                if nonce:
                    with self._lock:
                        item = self._pending.pop(nonce, None)
                    if item:
                        item[1]["msg"] = msg
                        item[0].set()
                elif msg.get("evt") == "VOICE_SETTINGS_UPDATE":
                    self._voice(msg.get("data") or {})
        except (OSError, DiscordError, ValueError, struct.error):
            pass
        self._disconnect(sock)

    def _disconnect(self, sock=None):
        if sock is not None and sock is not self.sock:
            return
        self.ready = False
        s, self.sock = self.sock, None
        if s:
            try:
                s.shutdown(socket.SHUT_RDWR)  # weckt den Lese-Thread in recv (close allein tut das nicht)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass
        with self._lock:
            for ev, box in self._pending.values():
                box["error"] = _("Connection to Discord lost")
                ev.set()
            self._pending.clear()
        self.version += 1

    def command(self, cmd, args=None, timeout=5.0, evt=None):
        nonce = uuidlib.uuid4().hex
        ev, box = threading.Event(), {}
        with self._lock:
            self._pending[nonce] = (ev, box)
        payload = {"cmd": cmd, "args": args or {}, "nonce": nonce}
        if evt:
            payload["evt"] = evt
        self._send(OP_FRAME, payload)
        if not ev.wait(timeout):
            with self._lock:
                self._pending.pop(nonce, None)
            raise DiscordError(_("No response from Discord ({command})").format(command=cmd))
        if "error" in box:
            raise DiscordError(box["error"])
        msg = box["msg"]
        if msg.get("evt") == "ERROR":
            raise DiscordError((msg.get("data") or {}).get("message") or _("Discord error"))
        return msg.get("data") or {}

    # ---------------------------------------------------------- Anmeldung
    def connect(self):
        with self._connecting:
            if self.ready:
                return
            if not self.client_id:
                raise DiscordError(_("Discord app missing: enter Client ID and Client Secret under "
                                     "Settings → Connections → Discord"))
            cands = self.sock_paths if self.sock_paths is not None else socket_paths()
            if not cands:
                raise DiscordError(_("Discord is not running (no IPC socket found)"))
            last = None
            for p in cands:
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.settimeout(None)
                    s.connect(str(p))
                    break
                except OSError as e:
                    last = e
            else:
                raise DiscordError(_("Discord not reachable: {error}").format(error=last))
            self.sock = s
            try:
                self._send(OP_HANDSHAKE, {"v": 1, "client_id": self.client_id})
                op, msg = self._recv()
                if op == OP_CLOSE or msg.get("evt") != "READY":
                    raise DiscordError((msg or {}).get("message") or _("Discord refused the connection"))
                threading.Thread(target=self._reader, args=(s,), name="discord", daemon=True).start()
                self._authenticate()
            except BaseException:
                self._disconnect(s)  # Socket zu, der Lese-Thread endet damit (sonst je Versuch einer mehr)
                raise
            self.ready = True
            try:
                self._voice(self.command("GET_VOICE_SETTINGS"))
                self.command("SUBSCRIBE", evt="VOICE_SETTINGS_UPDATE")
            except DiscordError as e:
                log.debug(_("Voice status: {error}").format(error=e))
            log.info(_("Connected to Discord"))

    def _authenticate(self):
        tok = self._load_token()
        if tok and tok.get("expires_at", 0) < time.time() + 60 and tok.get("refresh_token"):
            try:
                tok = self._token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
            except DiscordAuthError as e:  # Zugang widerrufen: neu anmelden statt bei jedem Druck zu scheitern
                log.info(_("Saved Discord access is invalid ({error}) – logging in again").format(error=e))
                self.token_file.unlink(missing_ok=True)
                tok = None
        if tok:
            try:
                self.command("AUTHENTICATE", {"access_token": tok["access_token"]})
                return
            except DiscordError as e:
                log.info(_("Saved Discord access is invalid ({error}) – logging in again").format(error=e))
        data = self.command("AUTHORIZE", {"client_id": self.client_id, "scopes": SCOPES}, timeout=120)
        tok = self._token_request({"grant_type": "authorization_code", "code": data["code"],
                                   "redirect_uri": "http://localhost"})
        self.command("AUTHENTICATE", {"access_token": tok["access_token"]})

    def _load_token(self):
        try:
            return json.loads(self.token_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _token_request(self, fields):
        if not self.client_secret:
            raise DiscordError(_("Client Secret missing – enter it under Settings → Connections → Discord"))
        body = urllib.parse.urlencode(dict(fields, client_id=self.client_id,
                                           client_secret=self.client_secret)).encode()
        req = urllib.request.Request(TOKEN_URL, data=body,
                                     headers={"Content-Type": "application/x-www-form-urlencoded",
                                              "User-Agent": "FreeDeck"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                tok = json.loads(r.read())
        except urllib.error.HTTPError as e:
            err = DiscordAuthError if e.code in (400, 401) else DiscordError  # invalid_grant, invalid_client
            raise err(_("Discord login failed: {error}").format(error=e)) from e
        except Exception as e:  # noqa: BLE001
            raise DiscordError(_("Discord login failed: {error}").format(error=e)) from e
        tok["expires_at"] = time.time() + int(tok.get("expires_in", 0))
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps(tok), encoding="utf-8")
        os.chmod(self.token_file, 0o600)
        return tok

    # ------------------------------------------------------------ Befehle
    def _voice(self, data):
        changed = False
        for k in ("mute", "deaf"):
            if k in data and self.voice.get(k) != bool(data[k]):
                self.voice[k] = bool(data[k])
                changed = True
        if changed:
            self.version += 1

    def set_voice(self, **kw):
        self.connect()
        self._voice(self.command("SET_VOICE_SETTINGS", kw))

    def toggle(self, key):
        self.connect()
        self.set_voice(**{key: not self.voice.get(key)})

    def select_voice_channel(self, channel_id):
        self.connect()
        self.command("SELECT_VOICE_CHANNEL", {"channel_id": channel_id or None, "force": True}, timeout=10)

    def select_text_channel(self, channel_id):
        self.connect()
        self.command("SELECT_TEXT_CHANNEL", {"channel_id": channel_id or None}, timeout=10)


_client = {"key": None, "obj": None}


def client(cfg) -> DiscordClient:
    c = (cfg.get("discord") if cfg else None) or {}
    key = (str(c.get("client_id") or ""), c.get("client_secret") or "")
    if _client["key"] != key:
        if _client["obj"] is not None:
            _client["obj"]._disconnect()  # alte Verbindung (und ihren Lese-Thread) nicht liegen lassen
        _client.update(key=key, obj=DiscordClient(*key))
    return _client["obj"]
