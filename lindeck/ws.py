"""Minimales WebSocket (RFC 6455) für Server und Client – nur Standardbibliothek.

Genutzt vom Plugin-Host (Server für Elgato-Plugins) und von der OBS-Anbindung
(Client für obs-websocket). Nur Textnachrichten, keine Erweiterungen.
"""
import base64
import hashlib
import os
import socket
import struct
import threading

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class Closed(Exception):
    pass


def _accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()


def _read_headers(sock):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise Closed("Verbindung beim Handshake geschlossen")
        data += chunk
        if len(data) > 65536:
            raise Closed("Handshake zu groß")
    head, rest = data.split(b"\r\n\r\n", 1)
    lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return lines[0], headers, rest


class Connection:
    """Eine WebSocket-Verbindung (thread-sicheres Senden)."""

    def __init__(self, sock, client_side, buffered=b"", path="/", headers=None):
        self.sock = sock
        self.client_side = client_side
        self.path = path
        self.headers = headers or {}
        self._buf = buffered
        self._lock = threading.Lock()
        self.closed = False

    # ------------------------------------------------------------ Senden
    def _frame(self, opcode, payload: bytes) -> bytes:
        head = bytes([0x80 | opcode])
        n = len(payload)
        mask_bit = 0x80 if self.client_side else 0
        if n < 126:
            head += bytes([mask_bit | n])
        elif n < 65536:
            head += bytes([mask_bit | 126]) + struct.pack("!H", n)
        else:
            head += bytes([mask_bit | 127]) + struct.pack("!Q", n)
        if self.client_side:
            mask = os.urandom(4)
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            head += mask
        return head + payload

    def send(self, text: str):
        if self.closed:
            raise Closed()
        data = self._frame(0x1, text.encode("utf-8"))
        with self._lock:
            try:
                self.sock.sendall(data)
            except OSError as e:
                self.closed = True
                raise Closed(str(e)) from e

    def close(self):
        if self.closed:
            return
        try:
            with self._lock:
                self.sock.sendall(self._frame(0x8, b""))
        except OSError:
            pass
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()

    # ---------------------------------------------------------- Empfangen
    def _read(self, n):
        while len(self._buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except OSError as e:
                self.closed = True
                raise Closed(str(e)) from e
            if not chunk:
                self.closed = True
                raise Closed("Verbindung geschlossen")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self) -> str:
        """Nächste Textnachricht (Ping/Pong werden intern beantwortet)."""
        parts, op0 = [], None
        while True:
            b1, b2 = self._read(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            payload = self._read(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                self.close()
                raise Closed("vom Gegenüber geschlossen")
            if opcode == 0x9:
                with self._lock:
                    self.sock.sendall(self._frame(0xA, payload))
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                op0, parts = opcode, [payload]
            elif opcode == 0x0:
                parts.append(payload)
            if fin and op0 is not None:
                return b"".join(parts).decode("utf-8", errors="replace")


class Server:
    """WebSocket-Server auf 127.0.0.1; on_connect(conn) läuft je Verbindung in einem Thread.

    Normale HTTP-GET-Anfragen beantwortet on_http(path, headers) -> (Status, Content-Type, Bytes, Extra-Header).
    """

    def __init__(self, on_connect, host="127.0.0.1", port=0, on_http=None):
        self.on_connect = on_connect
        self.on_http = on_http
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.running = True
        threading.Thread(target=self._accept_loop, name="ws-server", daemon=True).start()

    def _accept_loop(self):
        while self.running:
            try:
                s, _ = self.sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handshake, args=(s,), daemon=True).start()

    def _handshake(self, s):
        try:
            s.settimeout(10)
            line, h, rest = _read_headers(s)
            parts = line.split(" ")
            method, path = (parts[0], parts[1]) if len(parts) >= 2 else ("", "/")
            key = h.get("sec-websocket-key")
            if not key or "websocket" not in h.get("upgrade", "").lower():
                self._http(s, method, path, h)
                return
            s.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       f"Sec-WebSocket-Accept: {_accept_key(key)}\r\n\r\n").encode())
            s.settimeout(None)
        except (OSError, Closed):
            s.close()
            return
        self.on_connect(Connection(s, client_side=False, buffered=rest, path=path, headers=h))

    def _http(self, s, method, path, headers):
        status, ctype, body, extra = 404, "text/plain; charset=utf-8", b"Nicht gefunden", {}
        if method == "GET" and self.on_http:
            try:
                status, ctype, body, extra = self.on_http(path, headers)
            except Exception as e:  # noqa: BLE001
                status, ctype, body, extra = 500, "text/plain; charset=utf-8", str(e).encode(), {}
        reason = {200: "OK", 302: "Found", 403: "Forbidden", 404: "Not Found"}.get(status, "Error")
        head = (f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                "Cache-Control: no-store\r\nConnection: close\r\n")
        head += "".join(f"{k}: {v}\r\n" for k, v in (extra or {}).items())
        try:
            s.sendall(head.encode() + b"\r\n" + body)
        except OSError:
            pass
        s.close()

    def close(self):
        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass


def connect(host: str, port: int, path="/", timeout=5.0, headers=None) -> Connection:
    s = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    extra = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
    s.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n{extra}\r\n").encode())
    status, h, rest = _read_headers(s)
    if " 101 " not in status + " " or h.get("sec-websocket-accept") != _accept_key(key):
        s.close()
        raise Closed(f"Handshake abgelehnt: {status}")
    s.settimeout(None)
    return Connection(s, client_side=True, buffered=rest)
