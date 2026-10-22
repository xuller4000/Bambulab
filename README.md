# bambu-mcp

MCP-Server für den Bambu Lab A1 mini. Er spricht den Drucker direkt im lokalen Netz an (MQTT für Befehle und Status, FTPS für Dateien, Port 6000 für die Kamera), ohne Bambu-Cloud. Claude Desktop, Claude Code oder jeder andere MCP-Client kann damit Drucke starten, laufende Drucke nachjustieren und Modelle mit eigenen Einstellungen slicen.

## Tools

| Tool | Zweck |
|---|---|
| `printer_status` | Zustand, Fortschritt, Restzeit, Schicht, Temperaturen, Geschwindigkeit, Filament, Fehler |
| `camera_snapshot` | Ein JPEG von der eingebauten Kamera |
| `list_sd_files`, `upload_file`, `delete_sd_file` | Dateien auf der SD-Karte |
| `start_print` | Datei von der SD-Karte drucken (Platte, Bed Leveling, Flow- und Vibrationskalibrierung, Zeitraffer, AMS-Zuordnung) |
| `upload_and_print` | Fertig geslicte `.gcode.3mf` hochladen und starten |
| `pause_print`, `resume_print`, `stop_print` | Drucksteuerung |
| `set_print_speed` | silent / standard / sport / ludicrous |
| `set_temperature`, `set_part_fan`, `set_light` | Live-Anpassungen |
| `set_filament` | Filamenttyp, Farbe und Temperaturbereich für die externe Spule oder einen AMS-lite-Slot setzen |
| `send_gcode` | Rohes G-Code (z. B. `G28`) |
| `slice_model_file` | STL/3MF/OBJ/STEP slicen, mit Schichthöhe, Infill, Wänden, Support, Brim, Temperaturen und beliebigen weiteren Slicer-Keys |
| `slice_and_print` | Slicen, hochladen und starten in einem Schritt |

## Am Drucker vorbereiten

1. Am Display unter Einstellungen > Netzwerk den **LAN-Modus** einschalten. Dort stehen auch IP-Adresse und **Zugangscode**.
2. Die **Seriennummer** steht unter Einstellungen > Gerät (oder auf dem Aufkleber hinten).
3. Neuere Firmware hat die sogenannte Autorisierungssteuerung. Dann nimmt der Drucker Befehle von Drittsoftware nur im **Entwicklermodus** an (gleiches Menü wie der LAN-Modus). Der Statusabruf geht oft auch ohne, Start und Steuerung nicht. Im Entwicklermodus sind Bambu Handy und Cloud-Druck aus.
4. Am besten dem Drucker im Router eine feste IP geben, sonst stimmt `BAMBU_HOST` irgendwann nicht mehr.

## Installation

Python 3.10 oder neuer. Mit [uv](https://docs.astral.sh/uv/):

```bash
git clone <dieses Repo> bambu-mcp
cd bambu-mcp
uv venv && uv pip install -e .
```

Oder mit pip: `python -m venv .venv && .venv/bin/pip install -e .`

### Claude Desktop

In `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`):

```json
{
  "mcpServers": {
    "bambu": {
      "command": "/pfad/zu/bambu-mcp/.venv/bin/bambu-mcp",
      "env": {
        "BAMBU_HOST": "192.168.178.50",
        "BAMBU_SERIAL": "0309DA123456789",
        "BAMBU_ACCESS_CODE": "12345678"
      }
    }
  }
}
```

Unter Windows heißt der Pfad `...\.venv\Scripts\bambu-mcp.exe`. Danach Claude Desktop neu starten.

### Claude Code

```bash
claude mcp add bambu \
  -e BAMBU_HOST=192.168.178.50 -e BAMBU_SERIAL=0309DA123456789 -e BAMBU_ACCESS_CODE=12345678 \
  -- /pfad/zu/bambu-mcp/.venv/bin/bambu-mcp
```

### Als HTTP-Server

`bambu-mcp --transport streamable-http --port 8765` startet den Server unter `http://127.0.0.1:8765/mcp`. Ein Custom Connector auf claude.ai braucht allerdings eine öffentlich erreichbare HTTPS-Adresse, und der Drucker hängt im Heimnetz. Ohne Tunnel und eigene Authentifizierung davor ist die lokale Variante (Desktop oder Claude Code) die richtige.

## Slicen mit eigenen Einstellungen

Für `slice_model_file` und `slice_and_print` ruft der Server die Kommandozeile von Bambu Studio oder OrcaSlicer auf. Zusätzliche Umgebungsvariablen:

| Variable | Inhalt |
|---|---|
| `BAMBU_SLICER_PATH` | Programmdatei, z. B. `/Applications/BambuStudio.app/Contents/MacOS/BambuStudio` oder `C:\Program Files\Bambu Studio\bambu-studio.exe` |
| `BAMBU_PROFILE_MACHINE` | Drucker-Profil (JSON), z. B. „Bambu Lab A1 mini 0.4 nozzle“ |
| `BAMBU_PROFILE_PROCESS` | Prozess-Profil (JSON), z. B. „0.20mm Standard @BBL A1M“ |
| `BAMBU_PROFILE_FILAMENT` | Filament-Profil (JSON) |
| `BAMBU_SLICE_OUTPUT_DIR` | Zielordner, Standard `~/.cache/bambu-mcp` |

Die Profile liegen bei Bambu Studio im Installationsordner unter `resources/profiles/BBL/` (machine, process, filament), eigene Presets im Benutzerordner unter `user/<id>/`. Die Kommandozeile erwartet vollständige Profile. Ein Preset, das per `inherits` nur Abweichungen speichert, vorher in Bambu Studio vollständig exportieren.

Die Overrides landen in einer temporären Kopie, die Originaldateien bleiben unverändert. Neben den benannten Parametern lassen sich über `extra_process_settings` und `extra_filament_settings` beliebige Slicer-Keys setzen, etwa `{"top_shell_layers": 5, "seam_position": "back"}`. Eine bereits fertig eingerichtete Projekt-3MF kann auch ohne Profile geslict werden, dann aber ohne Overrides.

Ehrlicher Hinweis: Der Slicer-Aufruf ist mit einem simulierten Slicer getestet, nicht mit einer echten Bambu-Studio-Installation. Die CLI-Optionen ändern sich zwischen Versionen gelegentlich; wenn ein Lauf scheitert, zeigt die Fehlermeldung die letzten Zeilen der Slicer-Ausgabe.

## Optionale Variablen

`BAMBU_MQTT_PORT` (8883), `BAMBU_FTP_PORT` (990), `BAMBU_CAMERA_PORT` (6000) und `BAMBU_SDCARD_ROOT` (`/sdcard`, Pfad der SD-Karte in Druckbefehlen). Eine Vorlage steht in `.env.example`.

## Sicherheit

Der Drucker nutzt ein Zertifikat von Bambus eigener CA mit der Seriennummer als Namen. Der Server prüft das Zertifikat deshalb nicht, die Verbindung ist verschlüsselt, aber nicht gegen einen Angreifer im selben Netz abgesichert. Für das Heimnetz ist das vertretbar, über offene WLANs oder das Internet sollte der Zugangscode nicht laufen.

Temperaturen sind auf die Grenzen des A1 mini begrenzt (Düse 300 °C, Bett 80 °C). `stop_print`, `delete_sd_file` und `send_gcode` sind als destruktiv markiert, MCP-Clients fragen dafür normalerweise nach.

## Tests

```bash
uv pip install -e ".[test]"
.venv/bin/python -m pytest
```

Die Tests prüfen die MQTT-Befehle gegen einen simulierten Drucker, das Kamera-Protokoll gegen einen lokalen TLS-Server und das Slicen mit einem Fake-Slicer. Upload, Liste und Löschen per FTPS wurden zusätzlich gegen einen lokalen FTPS-Server mit implizitem TLS geprüft, der MQTT-Weg über TLS gegen einen lokalen Broker. An einem echten A1 mini lief der Code noch nicht.
