import socket
import ssl
import subprocess
import threading

import pytest

from bambu_mcp.discovery import common_name, listen, parse_notify, serial_from_certificate
from bambu_mcp.config import Settings

NOTIFY = (b"NOTIFY * HTTP/1.1\r\nHOST: 239.255.255.250:1990\r\nServer: UPnP/1.0\r\nLocation: 192.168.178.50\r\n"
          b"NT: urn:bambulab-com:device:3dprinter:1\r\nUSN: 0309DA4B0100123\r\nDevModel.bambu.com: N1\r\n"
          b"DevName.bambu.com: Werkstatt\r\nDevConnect.bambu.com: lan\r\n\r\n")


def test_parse_notify():
    info = parse_notify(NOTIFY, "10.0.0.9")
    assert info == {"ip": "192.168.178.50", "serial": "0309DA4B0100123", "model": "A1 mini",
                    "name": "Werkstatt", "connection": "lan"}
    assert parse_notify(b"NOTIFY * HTTP/1.1\r\nNT: upnp:rootdevice\r\nUSN: x\r\n", "1.2.3.4") is None


def test_listen_receives_broadcast():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    def send():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            for _ in range(20):
                s.sendto(NOTIFY, ("127.0.0.1", port))
                threading.Event().wait(0.05)

    threading.Timer(0.2, send).start()
    found = listen(timeout=1.5, port=port)
    assert [p["serial"] for p in found] == ["0309DA4B0100123"]


def test_serial_from_certificate(tmp_path):
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/C=CN/O=BBL Technologies Co., Ltd/CN=0309DA4B0100123",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        try:
            with ctx.wrap_socket(conn, server_side=True) as tls:
                tls.recv(1)
        except OSError:
            pass

    threading.Thread(target=serve, daemon=True).start()
    assert serial_from_certificate("127.0.0.1", srv.getsockname()[1]) == "0309DA4B0100123"
    srv.close()


def test_common_name_picks_subject_not_issuer():
    issuer = b"\x06\x03\x55\x04\x03\x0c\x07BBL CA1"
    subject = b"\x06\x03\x55\x04\x03\x13\x0f0309DA4B0100123"
    assert common_name(b"\x30" + issuer + b"\x30" + subject) == "0309DA4B0100123"
    assert common_name(b"nothing") is None


def test_serial_is_optional():
    s = Settings.from_env({"BAMBU_HOST": "192.168.178.50", "BAMBU_ACCESS_CODE": "12345678"})
    assert s.serial == ""
