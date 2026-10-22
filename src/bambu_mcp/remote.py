"""Remote access: the HTTP server behind a tunnel, and the setup wizard.

The connector listens on 127.0.0.1 only; a Cloudflare tunnel makes it reachable
over HTTPS, either a fixed tunnel on the user's own domain or a quick tunnel
with a random trycloudflare.com address. The MCP endpoint lives under a long random token,
``/<token>/mcp``, so only someone who knows the full URL can use it.
"""

from __future__ import annotations

import atexit
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import ConfigError, config_path, read_config_file, write_config_file

MIN_TOKEN_LENGTH = 24


def build_app(mcp, token: str):
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    if len(token) < MIN_TOKEN_LENGTH:
        raise ConfigError("BAMBU_HTTP_TOKEN is too short; run 'bambu-mcp setup' to create one")
    # Requests arrive through the tunnel with its public host name, so the localhost-only
    # host check is switched off; the secret path is what protects the endpoint.
    # Plain JSON responses instead of SSE streams: Cloudflare quick tunnels do not pass SSE.
    app = mcp.streamable_http_app(
        streamable_http_path=f"/{token}/mcp",
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    async def health(request):
        return PlainTextResponse("bambu-mcp läuft\n")

    app.router.routes.append(Route("/", health))
    return app


def serve(mcp, token: str, port: int, *, public_url: str | None = None, quick_tunnel: bool = False) -> None:
    import uvicorn

    app = build_app(mcp, token)
    print(f"bambu-mcp läuft auf http://127.0.0.1:{port}/  (Fenster offen lassen)", flush=True)
    if quick_tunnel:
        base = start_quick_tunnel(port)
        url = f"{base}/{token}/mcp"
        print("\nSchnelltest-Tunnel aktiv. Diese Adresse in claude.ai als Connector eintragen:")
        print(f"  {url}")
        print("Sie gilt nur, solange dieses Fenster offen ist.\n", flush=True)
        _save_url(url)
    elif public_url:
        print(f"Connector-Adresse: {public_url}/{token}/mcp", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


# -- cloudflare ------------------------------------------------------------------

QUICK_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def find_cloudflared() -> str | None:
    found = shutil.which("cloudflared")
    if found:
        return found
    for candidate in (r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
                      r"C:\Program Files\cloudflared\cloudflared.exe"):
        if Path(candidate).exists():
            return candidate
    return None


def start_quick_tunnel(port: int, timeout: float = 45) -> str:
    exe = find_cloudflared()
    if not exe:
        raise SystemExit(
            "cloudflared nicht gefunden. Installieren mit: winget install --id Cloudflare.cloudflared "
            "und danach ein neues PowerShell-Fenster öffnen."
        )
    proc = subprocess.Popen(
        [exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
    )
    atexit.register(proc.terminate)
    found: list[str] = []
    ready = threading.Event()

    def drain() -> None:
        # Keep reading so cloudflared never blocks on a full pipe.
        for line in proc.stderr:
            if not found and (m := QUICK_URL.search(line)):
                found.append(m.group(0))
                ready.set()

    threading.Thread(target=drain, daemon=True).start()
    deadline = time.time() + timeout
    while not ready.wait(0.5):
        if proc.poll() is not None or time.time() > deadline:
            proc.terminate()
            raise SystemExit("Der Cloudflare-Tunnel ist nicht gestartet. Internetverbindung prüfen und erneut versuchen.")
    return found[0]


def _save_url(url: str) -> None:
    try:
        target = config_path().parent / "connector-adresse.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(url + "\n", encoding="utf-8")
    except OSError:
        pass


def connector_url(token: str, port: int, public_url: str | None) -> str:
    if public_url:
        return f"{public_url}/{token}/mcp"
    saved = config_path().parent / "connector-adresse.txt"
    if saved.is_file():
        return saved.read_text(encoding="utf-8").strip() + "   (Schnelltest-Adresse vom letzten Start)"
    return f"Noch keine öffentliche Adresse. Lokal: http://127.0.0.1:{port}/{token}/mcp"


# -- setup wizard ----------------------------------------------------------------

def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or (default or "")


def _yes(prompt: str) -> bool:
    return _ask(f"{prompt} (j/n)", "j").lower().startswith(("j", "y"))


def _default_model_dir() -> Path:
    onedrive = os.environ.get("OneDrive")
    base = Path(onedrive) if onedrive and Path(onedrive).is_dir() else Path.home()
    return base / "3D-Drucker"


def _startup_folder() -> Path | None:
    if os.name != "nt":
        return None
    appdata = os.environ.get("APPDATA")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" if appdata else None


def _own_executable() -> Path:
    name = "bambu-mcp.exe" if os.name == "nt" else "bambu-mcp"
    return Path(sys.executable).with_name(name)


def setup() -> None:
    from .discovery import listen, serial_from_certificate
    from .printer import PrinterError

    path = config_path()
    current = read_config_file(path)
    print("Einrichtung bambu-mcp\n")

    host = current.get("BAMBU_HOST")
    if not host:
        print("Suche den Drucker im WLAN (ca. 12 Sekunden) ...")
        try:
            found = listen()
        except OSError:
            found = []
        if found:
            p = found[0]
            print(f"Gefunden: {p.get('model') or 'Drucker'} {p.get('name') or ''} unter {p['ip']}")
            host = p["ip"]
        else:
            print("Kein Drucker gefunden (Firewall-Abfrage zugelassen?). Bitte IP selbst eingeben.")
    host = _ask("IP-Adresse des Druckers", host)
    code = _ask("Zugangscode (LAN-Menü am Drucker)", current.get("BAMBU_ACCESS_CODE"))

    serial = current.get("BAMBU_SERIAL", "")
    try:
        serial = serial_from_certificate(host)
        print(f"Drucker erreicht. Seriennummer: {serial}")
    except PrinterError as exc:
        print(f"Drucker gerade nicht erreichbar ({exc}). Die Seriennummer wird später automatisch gelesen.")

    model_default = current.get("BAMBU_MODEL_DIR") or str(_default_model_dir())
    model_folder = Path(_ask("Ordner für Modelle (z. B. in OneDrive, damit das Handy Dateien ablegen kann)",
                             model_default)).expanduser()
    model_folder.mkdir(parents=True, exist_ok=True)

    print("\nFester Cloudflare-Tunnel mit eigener Domain? Dann die Adresse eingeben,")
    print("z. B. https://drucker.deinedomain.de. Leer lassen = Schnelltest-Tunnel ohne Konto.")
    public_url = _ask("Öffentliche Adresse", current.get("BAMBU_PUBLIC_URL")).rstrip("/")
    if public_url and not public_url.startswith("https://"):
        public_url = "https://" + public_url.removeprefix("http://")

    token = current.get("BAMBU_HTTP_TOKEN") or secrets.token_urlsafe(24)
    port = current.get("BAMBU_HTTP_PORT", "8765")
    values = dict(current) | {
        "BAMBU_HOST": host, "BAMBU_ACCESS_CODE": code, "BAMBU_SERIAL": serial,
        "BAMBU_MODEL_DIR": str(model_folder), "BAMBU_HTTP_TOKEN": token, "BAMBU_HTTP_PORT": port,
        "BAMBU_PUBLIC_URL": public_url,
    }
    write_config_file(path, values)
    print(f"\nGespeichert in {path}")

    startup = _startup_folder()
    if startup and startup.is_dir() and _yes("Connector beim Windows-Start automatisch starten?"):
        bat = startup / "bambu-mcp.bat"
        mode = "serve" if public_url else "serve --quick-tunnel"
        bat.write_text(f'@echo off\r\nstart "bambu-mcp" /min "{_own_executable()}" {mode}\r\n', encoding="utf-8")
        print(f"Autostart angelegt: {bat}")

    print("\nNächste Schritte:")
    if public_url:
        print(f"  1. Im Cloudflare-Tunnel als Ziel eintragen: http://localhost:{port}")
        print("  2. Connector starten:   bambu-mcp serve")
        print(f"  3. In claude.ai unter Einstellungen > Connectors eintragen: {public_url}/{token}/mcp")
    else:
        print("  1. Connector mit Tunnel starten:   bambu-mcp serve --quick-tunnel")
        print("  2. Die angezeigte Adresse in claude.ai unter Einstellungen > Connectors eintragen.")
        print("     Sie ändert sich bei jedem Start.")
