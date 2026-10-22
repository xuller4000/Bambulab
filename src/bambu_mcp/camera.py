"""Single JPEG frame from the A1/P1 camera stream (TLS on port 6000).

After an 80 byte auth packet the printer sends frames, each prefixed by a
16 byte header whose first 4 bytes are the little-endian payload length.
"""

from __future__ import annotations

import socket
import struct

from .printer import PrinterError, tls_context

JPEG_START = b"\xff\xd8"
JPEG_END = b"\xff\xd9"


def auth_packet(access_code: str, username: str = "bblp") -> bytes:
    header = struct.pack("<IIII", 0x40, 0x3000, 0, 0)
    return header + username.encode().ljust(32, b"\0") + access_code.encode().ljust(32, b"\0")


def _read_exact(sock, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise PrinterError("Camera stream closed unexpectedly")
        buf += chunk
    return bytes(buf)


def read_frame(sock, max_frames: int = 5) -> bytes:
    for _ in range(max_frames):
        header = _read_exact(sock, 16)
        size = struct.unpack_from("<I", header)[0]
        if not 0 < size < 10_000_000:
            raise PrinterError(f"Implausible camera frame size {size}")
        frame = _read_exact(sock, size)
        if frame.startswith(JPEG_START) and frame.endswith(JPEG_END):
            return frame
    raise PrinterError("No valid JPEG frame received from the camera")


def snapshot(host: str, access_code: str, port: int = 6000, timeout: float = 10) -> bytes:
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with tls_context().wrap_socket(raw, server_hostname=host) as sock:
                sock.sendall(auth_packet(access_code))
                return read_frame(sock)
    except OSError as exc:
        raise PrinterError(f"Camera not reachable at {host}:{port}: {exc}") from exc
