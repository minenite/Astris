#!/usr/bin/env python3
"""Minimal WebSocket ↔ TCP proxy for Astris → local Horizon (stdlib only).

Browsers cannot open Minecraft TCP. The WASM client dials:
  ws://127.0.0.1:8768/?target=127.0.0.1:25565
and this process bridges binary WebSocket frames to a TCP socket.
Only loopback / private targets are allowed.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import selectors
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DEFAULT_TARGET = ("127.0.0.1", 25565)


def allowed_host(host: str) -> bool:
    h = host.lower().strip("[]")
    if h in ("localhost", "127.0.0.1", "::1"):
        return True
    if h.startswith("192.168.") or h.startswith("10."):
        return True
    if h.startswith("172."):
        try:
            second = int(h.split(".")[1])
            return 16 <= second <= 31
        except (IndexError, ValueError):
            return False
    return False


def parse_target(raw: str | None) -> tuple[str, int]:
    if not raw:
        return DEFAULT_TARGET
    raw = raw.strip()
    if raw.startswith("[") and "]" in raw:
        host, _, rest = raw[1:].partition("]")
        port = int(rest[1:]) if rest.startswith(":") and rest[1:] else 25565
        return host, port
    if raw.count(":") == 1:
        host, port_s = raw.split(":")
        return host, int(port_s)
    return raw, 25565


def accept_key(key: str) -> str:
    digest = hashlib.sha1((key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf.extend(chunk)
    return bytes(buf)


def read_ws_frame(sock: socket.socket) -> tuple[int, bytes]:
    b1, b2 = recv_exact(sock, 2)
    opcode = b1 & 0x0F
    masked = (b2 & 0x80) != 0
    length = b2 & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", recv_exact(sock, 2))
    elif length == 127:
        (length,) = struct.unpack("!Q", recv_exact(sock, 8))
    mask = recv_exact(sock, 4) if masked else b""
    payload = recv_exact(sock, length) if length else b""
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def send_ws_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    header = bytearray()
    header.append(0x80 | (opcode & 0x0F))
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header.extend(struct.pack("!H", n))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", n))
    sock.sendall(header + payload)


def bridge(client: socket.socket, target: tuple[str, int]) -> None:
    host, port = target
    if not allowed_host(host):
        send_ws_frame(client, 0x8, b"\x03\xe8denied")
        client.close()
        return
    try:
        remote = socket.create_connection((host, port), timeout=10)
    except OSError as exc:
        send_ws_frame(client, 0x8, f"tcp:{exc}".encode()[:120])
        client.close()
        return
    remote.setblocking(False)
    client.setblocking(False)
    sel = selectors.DefaultSelector()
    sel.register(client, selectors.EVENT_READ)
    sel.register(remote, selectors.EVENT_READ)
    try:
        while True:
            for key, _ in sel.select(timeout=60):
                if key.fileobj is client:
                    try:
                        opcode, payload = read_ws_frame(client)
                    except (ConnectionError, OSError, struct.error):
                        return
                    if opcode == 0x8:
                        return
                    if opcode in (0x1, 0x2) and payload:
                        remote.sendall(payload)
                    elif opcode == 0x9:
                        send_ws_frame(client, 0xA, payload)
                else:
                    try:
                        data = remote.recv(65536)
                    except BlockingIOError:
                        continue
                    except OSError:
                        return
                    if not data:
                        return
                    send_ws_frame(client, 0x2, data)
    finally:
        try:
            remote.close()
        except OSError:
            pass
        try:
            client.close()
        except OSError:
            pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[minecraft-ws] {self.address_string()} {fmt % args}", flush=True)

    def do_GET(self) -> None:
        if self.headers.get("Upgrade", "").lower() != "websocket":
            self.send_error(426, "Upgrade Required")
            return
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self.send_error(400, "Missing Sec-WebSocket-Key")
            return
        query = parse_qs(urlsplit(self.path).query)
        target = parse_target((query.get("target") or [None])[0])
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept_key(key))
        self.end_headers()
        bridge(self.connection, target)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8768)
    args = ap.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Astris Minecraft WS↔TCP: ws://{args.host}:{args.port}/?target=127.0.0.1:25565", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
