"""Step-by-step check of the setup, with an optional small test print.

Started by double-clicking ``Drucker-Test.bat``. Reads the printer data from
Claude Desktop's config, so nothing has to be typed in twice.
"""

from __future__ import annotations

import glob
import json
import os
import re
import socket
import sys
import tempfile
import time
from pathlib import Path

OK, FAIL, INFO = "[OK]  ", "[FEHLER]", "[INFO]"


def _say(tag: str, text: str) -> None:
    print(f"{tag} {text}", flush=True)


def claude_config_files() -> list[Path]:
    candidates = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates += glob.glob(os.path.join(local, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude",
                                             "claude_desktop_config.json"))
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(os.path.join(appdata, "Claude", "claude_desktop_config.json"))
    home = Path.home()
    candidates.append(str(home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"))
    return [Path(c) for c in candidates if Path(c).is_file()]


def load_bambu_entry() -> tuple[dict, Path | None]:
    for path in claude_config_files():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            _say(FAIL, f"{path} ist kein gültiges JSON ({exc}). Meist fehlt ein Komma oder Anführungszeichen.")
            continue
        entry = (data.get("mcpServers") or {}).get("bambu")
        if entry:
            return entry, path
    return {}, None


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=4):
            return True
    except OSError:
        return False


def log_tail(config_file: Path | None, secret: str, lines: int = 15) -> list[str]:
    if not config_file:
        return []
    log = config_file.parent / "logs" / "mcp-server-bambu.log"
    if not log.is_file():
        return []
    text = log.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    return [line.replace(secret, "XXX") if secret else line for line in text]


def tiny_test_model(folder: Path) -> Path:
    """A 20 x 20 x 1 mm square: a few minutes, shows bed adhesion and first layer."""
    from .mesh import write_stl
    import numpy as np

    x0, y0, z0, x1, y1, z1 = 0, 0, 0, 20, 20, 1
    v = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
                  [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]], float)
    f = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
         (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    path = folder / "testquadrat.stl"
    write_stl(path, v[np.array(f)])
    return path


def main() -> int:
    print("Bambu-Connector: Prüfung\n")

    # 1. Claude Desktop config
    entry, config_file = load_bambu_entry()
    env = dict(entry.get("env") or {})
    if config_file:
        _say(OK, f"Claude-Konfiguration gefunden: {config_file}")
    else:
        _say(FAIL, "Kein Eintrag 'bambu' in einer claude_desktop_config.json gefunden.")
    command = entry.get("command")
    if command:
        if Path(command).is_file():
            _say(OK, f"Programm vorhanden: {command}")
        else:
            _say(FAIL, f"Programm aus der Konfiguration fehlt: {command}  -> pip install -e . erneut ausführen")

    host = env.get("BAMBU_HOST") or input("IP-Adresse des Druckers: ").strip()
    code = env.get("BAMBU_ACCESS_CODE") or input("Zugangscode: ").strip()
    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host or ""):
        _say(FAIL, f"'{host}' sieht nicht wie eine IP-Adresse aus (Beispiel: 192.168.178.50).")
        return 1
    if code in ("ZUGANGSCODE", "") or host == "IP-ADRESSE":
        _say(FAIL, "In der Konfiguration stehen noch die Platzhalter statt deiner Werte.")
        return 1

    # 2. Network
    if not port_open(host, 8883):
        _say(FAIL, f"Drucker unter {host} nicht erreichbar. Ist er an, im selben WLAN, und stimmt die IP "
                   "(Display: Einstellungen > Netzwerk)?")
        return 1
    _say(OK, f"Drucker unter {host} erreichbar")
    if not port_open(host, 990):
        _say(INFO, "Dateiübertragung (Port 990) nicht erreichbar. Ist der LAN-Modus an?")

    # 3. Serial
    from .discovery import serial_from_certificate
    from .printer import BambuPrinter, PrinterError, summarize

    serial = env.get("BAMBU_SERIAL")
    try:
        serial = serial or serial_from_certificate(host)
        _say(OK, f"Seriennummer: {serial}")
    except PrinterError as exc:
        _say(FAIL, f"Seriennummer nicht lesbar: {exc}")
        return 1

    # 4. Login and status
    printer = BambuPrinter(host, serial, code)
    try:
        printer.connect(timeout=10)
        state = printer.refresh(timeout=8)
    except PrinterError as exc:
        msg = str(exc).lower()
        if "not authori" in msg or "bad user" in msg or "password" in msg:
            _say(FAIL, "Anmeldung abgelehnt: Der Zugangscode stimmt nicht. Er ändert sich, wenn der LAN-Modus "
                       "neu eingeschaltet wird; den aktuellen vom Display abschreiben.")
        else:
            _say(FAIL, f"Keine Verbindung zur Druckersteuerung: {exc}")
        return 1
    s = summarize(state)
    _say(OK, "Anmeldung am Drucker erfolgreich")
    details = [f"Zustand: {s.get('state') or 'unbekannt'}"]
    if s.get("nozzle_temp") is not None:
        details.append(f"Düse {s['nozzle_temp']} °C")
    if s.get("bed_temp") is not None:
        details.append(f"Bett {s['bed_temp']} °C")
    if (s.get("external_spool") or {}).get("type"):
        details.append(f"Filament: {s['external_spool']['type']}")
    _say(INFO, ", ".join(details))

    # 5. Control rights (Developer Mode)
    try:
        ack = printer.set_light(True)
        if ack.get("acknowledged"):
            _say(OK, "Drucker nimmt Befehle an (Licht eingeschaltet)")
        else:
            _say(INFO, "Lichtbefehl gesendet, aber keine Bestätigung. Falls das Licht nicht angeht: "
                       "Entwicklermodus am Drucker einschalten.")
    except PrinterError as exc:
        _say(FAIL, f"Drucker lehnt Befehle ab ({exc}). Entwicklermodus am Drucker einschalten.")

    # 6. Claude Desktop log
    tail = log_tail(config_file, code)
    if tail:
        errors = [l for l in tail if "error" in l.lower() or "traceback" in l.lower()]
        if errors:
            _say(INFO, "Letzte Fehler im Claude-Log des Connectors:")
            for line in tail:
                print("        " + line)

    # 7. Slicer
    from .config import Settings
    from .workflow import SliceRequest, library_for, slice_request

    settings = Settings(host=host, serial=serial, access_code=code)
    try:
        lib, slicer = library_for(settings)
    except Exception as exc:  # noqa: BLE001 - report anything to the user
        lib, slicer = None, None
        _say(INFO, f"Slicer-Suche fehlgeschlagen: {exc}")
    if slicer and lib:
        _say(OK, f"Slicer gefunden: {slicer.executable}")
    else:
        _say(FAIL, "Bambu Studio nicht gefunden. Für das Slicen bambulab.com/de/download/studio installieren.")
        print("\nDie Steuerung funktioniert. Testdruck braucht Bambu Studio.")
        return 0

    # 8. Optional test print
    if s.get("state") not in ("idle", "finished", "failed", None):
        _say(INFO, f"Der Drucker ist gerade beschäftigt ({s.get('state')}), kein Testdruck.")
        return 0
    print("\nTestdruck: Quadrat 20 x 20 x 1 mm, dauert nur wenige Minuten plus Aufheizen und Bettvermessung.")
    print("Vorher prüfen: Druckplatte leer und sauber, Filament eingelegt.")
    if not input("Testdruck jetzt starten? (j/n): ").strip().lower().startswith("j"):
        print("Kein Testdruck. Alles andere ist geprüft.")
        return 0

    info = (state.get("print") or {}).get("vt_tray") or {}
    with tempfile.TemporaryDirectory() as tmp:
        model = tiny_test_model(Path(tmp))
        req = SliceRequest(model_path=str(model), purpose="balanced",
                           filament_type=info.get("tray_type") or None, filament_id=info.get("tray_info_idx") or None)
        try:
            result = slice_request(req, settings)
        except Exception as exc:  # noqa: BLE001
            _say(FAIL, f"Slicen fehlgeschlagen: {exc}")
            return 1
    est = result.get("estimate") or {}
    _say(OK, f"Gesliced mit {result['presets']['process']} / {result['presets']['filament']}"
             + (f", ca. {est['print_time']}" if est.get("print_time") else ""))

    from .ftps import PrinterFiles

    try:
        PrinterFiles(host, code).upload(result["sliced_file"], "testquadrat.gcode.3mf")
        _say(OK, "Datei auf den Drucker übertragen")
        printer.start_print("testquadrat.gcode.3mf", bed_leveling=True, flow_calibration=False,
                            vibration_calibration=False)
    except (PrinterError, OSError) as exc:
        _say(FAIL, f"Start fehlgeschlagen: {exc}")
        return 1
    time.sleep(8)
    now = summarize(printer.refresh(timeout=8))
    if now.get("state") in ("idle", None):
        _say(OK, "Startbefehl angenommen. Der Drucker beginnt gleich mit Aufheizen und Bettvermessung; "
                 "am Display nachsehen.")
    else:
        _say(OK, f"Druck gestartet. Zustand jetzt: {now.get('state')}")
    if now.get("print_error"):
        _say(INFO, f"Der Drucker meldet Fehlercode {now['print_error']}.")
    print("\nAlles läuft. In Claude Desktop sollte der Connector jetzt ebenfalls funktionieren.")
    return 0


def run() -> None:
    try:
        code = main()
    except KeyboardInterrupt:
        code = 1
    except Exception as exc:  # noqa: BLE001 - show anything, the window must stay readable
        _say(FAIL, f"Unerwarteter Fehler: {type(exc).__name__}: {exc}")
        code = 1
    if os.name == "nt":
        input("\nEnter drücken zum Schließen ...")
    sys.exit(code)


if __name__ == "__main__":
    run()
