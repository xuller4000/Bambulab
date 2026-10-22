"""Finding a printer's serial number (and IP) on the local network.

Two sources, both available in LAN-only mode:
- The TLS certificate on the MQTT port carries the serial as its common name.
- The printer broadcasts SSDP-style NOTIFY messages on UDP port 2021 with
  IP, serial and model, which is how Bambu Studio lists LAN printers.
"""

from __future__ import annotations

import socket
import ssl
import time

from .printer import PrinterError

CN_OID = b"\x06\x03\x55\x04\x03"  # DER encoding of OID 2.5.4.3 (commonName)
SSDP_PORT = 2021
MODELS = {"N1": "A1 mini", "N2S": "A1", "C11": "P1P", "C12": "P1S", "BL-P001": "X1 Carbon", "C13": "X1E"}


def common_name(der: bytes) -> str | None:
    """Subject CN of a DER certificate. The subject follows the issuer, so the last CN wins."""
    pos = der.rfind(CN_OID)
    if pos < 0:
        return None
    start = pos + len(CN_OID)
    if start + 2 > len(der):
        return None
    length = der[start + 1]
    value = der[start + 2 : start + 2 + length]
    return value.decode("utf-8", errors="replace") or None


def serial_from_certificate(host: str, port: int = 8883, timeout: float = 5) -> str:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
    except OSError as exc:
        raise PrinterError(f"Printer not reachable at {host}:{port}: {exc}") from exc
    serial = common_name(der or b"")
    if not serial:
        raise PrinterError("The printer's certificate has no serial number in it")
    return serial


def parse_notify(data: bytes, sender_ip: str) -> dict | None:
    text = data.decode("utf-8", errors="replace")
    headers = {}
    for line in text.splitlines()[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    if "bambulab" not in headers.get("nt", "").lower() or not headers.get("usn"):
        return None
    model = headers.get("devmodel.bambu.com", "")
    return {
        "ip": headers.get("location") or sender_ip,
        "serial": headers["usn"],
        "model": MODELS.get(model, model) or None,
        "name": headers.get("devname.bambu.com") or None,
        "connection": headers.get("devconnect.bambu.com") or None,
    }


def listen(timeout: float = 12, port: int = SSDP_PORT) -> list[dict]:
    """Collect printer announcements for a few seconds (they arrive about every 5 s)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", port))
    found: dict[str, dict] = {}
    deadline = time.time() + timeout
    try:
        while (remaining := deadline - time.time()) > 0:
            sock.settimeout(remaining)
            try:
                data, (ip, _) = sock.recvfrom(4096)
            except socket.timeout:
                break
            if info := parse_notify(data, ip):
                found[info["serial"]] = info
    finally:
        sock.close()
    return list(found.values())


def find_serial(host: str, port: int = 8883) -> str:
    try:
        return serial_from_certificate(host, port)
    except PrinterError as cert_error:
        for printer in listen(timeout=12):
            if printer["ip"] == host:
                return printer["serial"]
        raise PrinterError(
            f"Could not read the serial number from {host} ({cert_error}). "
            "Check the IP address, or set BAMBU_SERIAL from the sticker on the printer."
        ) from cert_error


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Find Bambu Lab printers and their serial numbers")
    parser.add_argument("ip", nargs="?", help="Printer IP; read the serial from its certificate")
    args = parser.parse_args()
    if args.ip:
        print(f"Seriennummer: {find_serial(args.ip)}")
        return
    print("Suche Drucker im Netzwerk (ca. 12 Sekunden) ...")
    printers = listen()
    if not printers:
        print("Kein Drucker gefunden. Gleiches WLAN? Firewall-Abfrage zugelassen? Sonst: bambu-find <IP-Adresse>")
    for p in printers:
        print(f"{p.get('model') or 'Drucker'} {p.get('name') or ''}: IP {p['ip']}, Seriennummer {p['serial']}")


if __name__ == "__main__":
    main()
