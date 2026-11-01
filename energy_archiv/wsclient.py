"""Minimálny synchrónny WebSocket klient (RFC 6455) len so štandardnou knižnicou."""
from __future__ import annotations

import base64
import os
import socket
import ssl
import struct
from urllib.parse import urlparse


class WSError(RuntimeError):
    pass


class WebSocket:
    def __init__(self, url: str, timeout: float = 60, verify_ssl: bool = True):
        u = urlparse(url)
        secure = u.scheme in ("wss", "https")
        port = u.port or (443 if secure else 80)
        sock = socket.create_connection((u.hostname, port), timeout=timeout)
        if secure:
            ctx = ssl.create_default_context()
            if not verify_ssl:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=u.hostname)
        self.sock = sock
        self._buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        req = (f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{port}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        sock.sendall(req.encode())
        head = self._read_until(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status + b" ":
            raise WSError(f"WebSocket handshake zlyhal: {status.decode(errors='replace')}")

    # -- nízka úroveň
    def _fill(self) -> None:
        chunk = self.sock.recv(65536)
        if not chunk:
            raise WSError("spojenie ukončené")
        self._buf += chunk

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buf:
            self._fill()
        head, self._buf = self._buf.split(marker, 1)
        return head

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._fill()
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        head = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(head + mask + masked)

    # -- verejné
    def send(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv(self) -> str:
        parts: list[bytes] = []
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
            if opcode == 0x9:  # ping
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                raise WSError("server ukončil spojenie")
            parts.append(payload)
            if fin:
                return b"".join(parts).decode("utf-8")

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        self.sock.close()
