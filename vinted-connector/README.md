# Vinted Connector

MCP-Server als Cloudflare Worker, über den Claude (claude.ai, Desktop, Mobile) auf Vinted suchen und Artikel lesen kann. Kein Login, kein eigenes Konto. Nur lesend: Kaufen, Favorisieren oder Nachrichten an Verkäufer gehen damit nicht.

Aufgebaut wie der Kleinanzeigen-Connector (`xuller4000/kleinanzeigen`), mit eigenem Worker `vinted-connector` und eigenem Deploy.

## Tools

| Tool | Zweck |
|---|---|
| `search_items` | Suche mit Begriff, Preisgrenzen, Zustand, Sortierung, Seite und Länderseite (Standard vinted.de) |
| `get_item` | Details eines Artikels: Preis mit Käuferschutz, Verkäufer und Bewertung, Ort, Marke, Größe, Zustand, Material, Beschreibung und die ersten vier Fotos |

## So funktioniert der Zugriff

Vinted hat keine offizielle API. Der Worker ruft einmal die Startseite auf, nimmt die anonymen Session-Cookies (`access_token_web` usw.) mit und fragt damit dieselbe JSON-API ab, die auch die Website nutzt (`/api/v2/catalog/items`, `/api/v2/items/<id>`). Die Session wird pro Worker-Instanz 20 Minuten gehalten; bei HTTP 401 holt er einmal eine neue. Liefert die Artikel-API nichts, liest `get_item` die Artikelseite (JSON-LD und Open-Graph-Tags) als Ersatz.

## Einrichtung

### 1. GitHub Secrets

Unter *Settings → Secrets and variables → Actions → New repository secret* drei Secrets anlegen. Die Cloudflare-Werte sind dieselben wie beim Kleinanzeigen-Connector.

| Name | Wert |
|---|---|
| `CLOUDFLARE_API_TOKEN` | Cloudflare-Token mit der Vorlage *Edit Cloudflare Workers* |
| `CLOUDFLARE_ACCOUNT_ID` | Account-ID aus dem Cloudflare-Dashboard |
| `CONNECTOR_SECRET` | zufällige Zeichenkette, mindestens 24 Zeichen, z. B. `openssl rand -hex 24` |

Für `CONNECTOR_SECRET` ein eigenes nehmen, nicht das vom Kleinanzeigen-Connector.

### 2. Deploy

*Actions → Deploy to Cloudflare → Run workflow*. Danach deployt jeder Push auf `main` automatisch.

### 3. In Claude einbinden

claude.ai → *Einstellungen → Connectors → Custom Connector hinzufügen*:

- Name: `Vinted`
- URL: `https://vinted-connector.xuller4000.workers.dev/<CONNECTOR_SECRET>/mcp`

OAuth-Felder leer lassen.

## Wenn der Deploy scheitert

Der Workflow prüft das Token vor dem Deploy und schreibt ins Log, was nicht stimmt (gleiche Meldungen wie beim Kleinanzeigen-Connector).

## Grenzen

Vinted sitzt hinter einem Bot-Schutz (Datadome) und sperrt Rechenzentrums-IPs teils hart. Ob Anfragen aus Cloudflare durchkommen, schwankt; bei einer Sperre meldet der Connector „Vinted blockiert die Anfrage“ oder „keine Sitzung ausgegeben“ statt leerer Ergebnisse. Die Nutzungsbedingungen von Vinted untersagen automatisierte Abfragen, also nur für gelegentliche private Suchen nutzen.

Die API ist inoffiziell. Ändert Vinted Feldnamen, fehlen zuerst einzelne Angaben (Bewertung, Ort), die Suche selbst ist am stabilsten.

## Lokal entwickeln

```sh
npm install
echo 'CONNECTOR_SECRET=local-dev-secret-0123456789abcdef' > .dev.vars
npm run dev
```

MCP-Endpunkt dann unter `http://localhost:8787/local-dev-secret-0123456789abcdef/mcp`.
