"""Minimal RFC6455 WebSocket server endpoint, plus the handshake accept-key
helper and the inbound-frame size cap.

The PTY terminal stream (GET /api/ptys/<id>/io) is the only WebSocket endpoint;
it upgrades through ``WebSocket.accept()``. The handshake enforces the same
loopback Origin allowlist as the rest of the API via
``server.runtime._origin_allowed``, so a cross-origin page can't drive the
upgrade. Kept out of serve.py so the framing has no dependency on the rest of
the server beyond that one allowlist check.
"""
from __future__ import annotations

import base64
import hashlib
import struct
import threading

from server.runtime import _origin_allowed

# Cap on a single inbound WebSocket frame payload. The WS framing format
# allows a 64-bit extended length, so without an explicit cap a client
# can declare a multi-GB payload and pin the reader thread on
# ``self._rfile.read(length)`` while attempting allocation. PTY input is
# keystrokes (tens of bytes); chat composer frames are JSON capped
# client-side. 1 MiB matches ``MAX_JSON_BODY`` and is well above any
# legitimate WS traffic the dashboard sends.
MAX_WS_PAYLOAD = 1024 * 1024  # 1 MiB

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_accept_key(client_key: str) -> str:
    raw = (client_key + WS_GUID).encode("ascii")
    return base64.b64encode(hashlib.sha1(raw).digest()).decode("ascii")


class _WsClosed(Exception):
    """Raised by WebSocket recv/send when the peer has disconnected."""


class WebSocket:
    """Minimal RFC6455 server endpoint.

    Frames are sent unfragmented (FIN=1 always), payload up to 2^63
    bytes. Receives single-frame messages and pings; replies to pings
    automatically. Control flow:

        ws = WebSocket.accept(handler, expected_path)
        try:
            while True:
                opcode, data = ws.recv()
                # opcode 0x1 = text, 0x2 = binary, 0x8 = close
                ...
        except _WsClosed:
            ...
        finally:
            ws.close()
    """

    OPCODE_CONT  = 0x0
    OPCODE_TEXT  = 0x1
    OPCODE_BIN   = 0x2
    OPCODE_CLOSE = 0x8
    OPCODE_PING  = 0x9
    OPCODE_PONG  = 0xA

    def __init__(self, handler):
        self._handler = handler
        self._rfile = handler.rfile
        self._wfile = handler.wfile
        # ``self.connection`` is the raw socket; we don't read from it
        # directly but tracking it lets us shutdown on close.
        self._sock = getattr(handler, "connection", None)
        self._write_lock = threading.Lock()
        self.closed = False

    @classmethod
    def accept(cls, handler) -> "WebSocket | None":
        """Complete the RFC6455 handshake. Returns a WebSocket on success,
        or sends an HTTP error and returns ``None`` on failure."""
        h = handler.headers
        if h.get("Upgrade", "").lower() != "websocket":
            handler.send_error(400, "Expected WebSocket upgrade")
            return None
        if "upgrade" not in h.get("Connection", "").lower():
            handler.send_error(400, "Expected Connection: Upgrade")
            return None
        if not _origin_allowed(h):
            handler.send_error(403, "Origin not allowed")
            return None
        key = h.get("Sec-WebSocket-Key", "").strip()
        if not key:
            handler.send_error(400, "Missing Sec-WebSocket-Key")
            return None
        accept = _ws_accept_key(key)
        # Write the 101 response manually so we don't pick up "Server"
        # / "Date" headers from the base handler.
        try:
            handler.wfile.write(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + accept.encode("ascii") + b"\r\n"
                b"\r\n"
            )
            handler.wfile.flush()
        except OSError:
            return None
        return cls(handler)

    def recv(self) -> tuple[int, bytes]:
        b0 = self._rfile.read(1)
        if not b0:
            raise _WsClosed()
        b0 = b0[0]
        opcode = b0 & 0x0F
        b1 = self._rfile.read(1)
        if not b1:
            raise _WsClosed()
        b1 = b1[0]
        masked = bool(b1 & 0x80)
        # RFC 6455 §5.1: a server MUST fail the connection on any unmasked
        # frame from a client. Reject rather than silently process it.
        if not masked:
            raise _WsClosed()
        length = b1 & 0x7F
        if length == 126:
            ext = self._rfile.read(2)
            if len(ext) < 2:
                raise _WsClosed()
            length = struct.unpack(">H", ext)[0]
        elif length == 127:
            ext = self._rfile.read(8)
            if len(ext) < 8:
                raise _WsClosed()
            length = struct.unpack(">Q", ext)[0]
        if length > MAX_WS_PAYLOAD:
            raise _WsClosed()
        mask = self._rfile.read(4) if masked else None
        if masked and (mask is None or len(mask) < 4):
            raise _WsClosed()
        payload = b""
        if length:
            payload = self._rfile.read(length)
            if len(payload) < length:
                raise _WsClosed()
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        # Auto-handle pings (reply with pong) and close frames so callers
        # only deal with text/binary data.
        if opcode == self.OPCODE_PING:
            self._send_frame(self.OPCODE_PONG, payload)
            return self.recv()
        if opcode == self.OPCODE_CLOSE:
            self.closed = True
            raise _WsClosed()
        return opcode, payload

    def send_binary(self, data: bytes) -> None:
        self._send_frame(self.OPCODE_BIN, data)

    def send_text(self, text: str) -> None:
        self._send_frame(self.OPCODE_TEXT, text.encode("utf-8", errors="replace"))

    def _send_frame(self, opcode: int, data: bytes) -> None:
        if self.closed:
            raise _WsClosed()
        header = bytearray([0x80 | (opcode & 0x0F)])
        length = len(data)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header += struct.pack(">H", length)
        else:
            header.append(127)
            header += struct.pack(">Q", length)
        with self._write_lock:
            try:
                self._wfile.write(bytes(header))
                if data:
                    self._wfile.write(data)
                self._wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.closed = True
                raise _WsClosed()

    def close(self, code: int = 1000) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            payload = struct.pack(">H", code)
            self._send_frame(self.OPCODE_CLOSE, payload)
        except (_WsClosed, OSError):
            pass
        try:
            if self._sock is not None:
                self._sock.shutdown(2)  # SHUT_RDWR
        except OSError:
            pass
