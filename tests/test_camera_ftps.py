import socket
import ssl
import struct
import subprocess
import threading

import pytest

from bambu_mcp import camera
from bambu_mcp.ftps import parse_list_line

JPEG = b"\xff\xd8" + b"x" * 100 + b"\xff\xd9"


@pytest.fixture
def tls_server(tmp_path):
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=printer",
                    "-keyout", str(key), "-out", str(cert), "-days", "1"], check=True, capture_output=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    received = {}

    def serve():
        conn, _ = srv.accept()
        with ctx.wrap_socket(conn, server_side=True) as tls:
            received["auth"] = tls.recv(80)
            for frame in (b"garbage!", JPEG):
                tls.sendall(struct.pack("<IIII", len(frame), 0, 1, 0) + frame)

    threading.Thread(target=serve, daemon=True).start()
    yield srv.getsockname()[1], received
    srv.close()


def test_camera_snapshot(tls_server):
    port, received = tls_server
    assert camera.snapshot("127.0.0.1", "12345678", port=port) == JPEG
    auth = received["auth"]
    assert struct.unpack_from("<II", auth) == (0x40, 0x3000)
    assert auth[16:20] == b"bblp" and auth[48:56] == b"12345678"


def test_parse_list_line():
    entry = parse_list_line("-rw-rw-rw-   1 user group   1048576 Oct 22 14:31 benchy plate.gcode.3mf")
    assert entry == {"name": "benchy plate.gcode.3mf", "type": "file", "size": 1048576, "modified": "Oct 22 14:31"}
    assert parse_list_line("drwxr-xr-x 1 u g 0 Jan 01 2026 timelapse")["type"] == "dir"
    assert parse_list_line("total 4") is None
