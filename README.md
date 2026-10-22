# bambu-mcp

MCP-Server für den Bambu Lab A1 mini. Er spricht den Drucker direkt im lokalen Netz an (MQTT für Befehle und Status, FTPS für Dateien, Port 6000 für die Kamera), ohne Bambu-Cloud. Claude Desktop, Claude Code oder jeder andere MCP-Client kann damit Modelle slicen, Drucke starten und laufende Drucke nachjustieren. Die Druckparameter bestimmt der Server beim Slicen selbst, aus der Geometrie des Modells, dem eingelegten Filament und dem Zweck des Teils.

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
| `analyze_model` | Geometrie prüfen: Maße, Volumen, Auflagefläche, Überhänge, beste Ausrichtung, passt es auf den A1 mini |
| `recommend_print_settings` | Vorschau der automatisch gewählten Einstellungen mit Begründung, ohne zu slicen |
| `list_filament_presets` | Filament-Presets des installierten Slicers für den A1 mini |
| `slice_model_file` | STL/3MF/OBJ slicen, Parameter automatisch bestimmt; eigene Angaben überschreiben die Empfehlung |
| `slice_and_print` | Slicen, hochladen und starten in einem Schritt |

## Am Drucker vorbereiten

1. Am Display unter Einstellungen > Netzwerk den **LAN-Modus** einschalten. Dort stehen auch IP-Adresse und **Zugangscode**.
2. Die **Seriennummer** muss nicht eingetragen werden. Fehlt `BAMBU_SERIAL`, liest der Server sie aus dem Zertifikat des Druckers. Zum Nachsehen: `bambu-find` listet alle Bambu-Drucker im Netz mit IP und Seriennummer, `bambu-find <IP>` fragt einen bestimmten Drucker.
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
  -e BAMBU_HOST=192.168.178.50 -e BAMBU_ACCESS_CODE=12345678 \
  -- /pfad/zu/bambu-mcp/.venv/bin/bambu-mcp
```

### Als HTTP-Server

`bambu-mcp --transport streamable-http --port 8765` startet den Server unter `http://127.0.0.1:8765/mcp`. Ein Custom Connector auf claude.ai braucht allerdings eine öffentlich erreichbare HTTPS-Adresse, und der Drucker hängt im Heimnetz. Ohne Tunnel und eigene Authentifizierung davor ist die lokale Variante (Desktop oder Claude Code) die richtige.

## Slicen und automatische Druckparameter

Gesliced wird mit der Kommandozeile von Bambu Studio oder OrcaSlicer. Der Server sucht beides an den üblichen Orten (macOS `/Applications`, Windows `Program Files`, Linux im `PATH`) und nimmt die Bambu-Profile direkt aus der Installation, dazu deine eigenen Presets aus dem Benutzerordner. Exportieren musst du nichts. Die Profile speichern nur Abweichungen zu einem Elternprofil (`inherits`); der Server löst diese Ketten auf und übergibt dem Slicer vollständige Profile.

Bei jedem Slice-Auftrag läuft vorher eine Analyse:

1. **Geometrie.** Größe, Volumen, Auflagefläche auf dem Bett, Flächen flacher als 30° zur Horizontalen (die ohne Stützen durchhängen), flache Oberseiten, Verhältnis Höhe zu Breite. Passt das Teil nicht in 180 × 180 × 180 mm, kommt eine Warnung mit dem nötigen Skalierfaktor.
2. **Ausrichtung.** Die sechs Lagen „Seite nach unten“ werden durchgerechnet. Gedreht wird nur, wenn das deutlich Stützmaterial spart oder das Teil sonst nicht passt. Allein wegen geringerer Höhe dreht der Server nichts, weil die Lage bestimmt, wie die Schichtlinien verlaufen.
3. **Filament.** Ohne Angabe fragt der Server den Drucker, was eingelegt ist (externe Spule oder AMS-lite-Slot). Bei Bambu-Filament mit RFID kommt die genaue Filament-ID zurück, damit passt auch das Preset genau („Bambu PLA Matte @BBL A1M“ statt „Generic PLA“). Die Düsengröße liest er ebenfalls vom Drucker.
4. **Regeln.** Daraus folgen Preset und Anpassungen, jeweils mit Begründung:
   - Schichthöhe nach Zweck und Teilegröße (Qualität 0,12 bis 0,16 mm, ausgewogen 0,20, schnell 0,24 bis 0,28), für andere Düsen skaliert. Das passende Bambu-Preset wird danach gewählt, bei Qualität die „High Quality“-Variante mit langsamen Außenwänden, bei Festigkeit „Strength“.
   - 3 statt 2 Wände ab 30 mm Teilegröße, bei Festigkeit 6 Wände und 35 % Gyroid.
   - Tree-Support nur, wenn nennenswerte Flächen ihn brauchen.
   - Brim bei schlanken Teilen und kleiner Auflagefläche (der A1 mini bewegt das Bett, hohe Teile kippen leichter) und bei großen PETG-Teilen.
   - Bügeln bei Qualität und einer flachen Oberseite bis 6000 mm², nie bei TPU.
   - Warnungen für Material, das auf dem offenen A1 mini nicht funktioniert (ABS, ASA, PC, PA), für abrasives Material (CF, GF, Glow) und für TPU.

Temperaturen, Kühlung und maximaler Volumenstrom kommen aus dem Bambu-Filamentprofil. Die sind für den A1 mini abgestimmt, und die Regeln überschreiben sie nicht.

Der Zweck (`purpose`) ist `auto`, `balanced`, `quality`, `strength` oder `speed`. Bei `auto` werden kleine Teile unter 40 mm mit Qualitätseinstellungen gedruckt, alles andere ausgewogen. Was du ausdrücklich angibst (Schichthöhe, Infill, Wände, beliebige Slicer-Keys über `extra_process_settings`), hat immer Vorrang. Mit `auto_tune=false` bleibt das Preset unverändert.

Nach dem Slicen liest der Server Druckzeit und Filamentverbrauch aus der fertigen Datei und gibt sie mit zurück.

Optionale Variablen dafür:

| Variable | Inhalt |
|---|---|
| `BAMBU_SLICER_PATH` | Slicer-Programm, falls es nicht automatisch gefunden wird |
| `BAMBU_PROFILE_DIR` | Profilordner (der mit `machine/`, `process/`, `filament/`), mehrere mit `:` bzw. `;` getrennt |
| `BAMBU_PLATE_TYPE` | `textured_pei` (Standard beim A1 mini), `smooth_pei`, `cool_plate`, `engineering` |
| `BAMBU_NOZZLE` | Düsendurchmesser, falls der Drucker ihn nicht meldet |
| `BAMBU_PROFILE_MACHINE`, `_PROCESS`, `_FILAMENT` | Feste Profildateien statt automatischer Wahl |
| `BAMBU_SLICE_OUTPUT_DIR` | Zielordner, Standard `~/.cache/bambu-mcp` |

Geprüft ist die Auswahl gegen die echten A1-mini-Profile aus dem Bambu-Studio-Repository (Vererbung, Preset-Wahl für alle vier Düsengrößen, Filament nach Typ und ID). Den Slicer selbst hat in den Tests ein Ersatzprogramm gespielt, das die übergebenen Profile mitschreibt. Mit einer echten Bambu-Studio-Installation lief der Aufruf noch nicht. Falls die Kommandozeile etwas anmeckert, steht der Schluss ihrer Ausgabe in der Fehlermeldung.

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

Die Tests prüfen die MQTT-Befehle gegen einen simulierten Drucker, das Kamera-Protokoll gegen einen lokalen TLS-Server, die Geometrie-Analyse (STL, OBJ, 3MF mit Komponenten), die Profilauflösung und die ganze Slice-Kette mit einem Ersatz-Slicer. Upload, Liste und Löschen per FTPS wurden zusätzlich gegen einen lokalen FTPS-Server mit implizitem TLS geprüft, der MQTT-Weg über TLS gegen einen lokalen Broker. An einem echten A1 mini lief der Code noch nicht.
