# 🏦 comdirect-firefly-sync

**Read-Only Finanzdaten-Export aus der Comdirect REST API — als CSV, REST API oder (geplant) Firefly III Import.**

> Automatisierter Zugriff auf Konten, Umsätze und Depots — sicher und ohne Schreibzugriff.

## Features

- **Comdirect API Connector** — OAuth2 + pushTAN Authentifizierung, read-only
- **CSV-Export** — Vollständiger Finanzreport (Konten, Umsätze, Depot-Positionen, Depot-Umsätze, Finanzübersicht)
- **Read-Only REST API** — Stellt exportierte CSVs über HTTP bereit (für KI-Agenten o.ä.)
- **Docker** — Zwei-Container-Architektur: Export-Job + API-Server mit geteiltem Volume
- **Firefly III Import** — (geplant) Periodischer Import in Firefly III mit Deduplizierung

## Architektur

```
┌─────────────────┐     ┌──────────────┐     ┌────────────────┐
│  Comdirect API  │────▶│  Export Job   │────▶│  CSV Volume    │
│  (read-only)    │     │  (Container) │     │  /data/exports │
└─────────────────┘     └──────────────┘     └───────┬────────┘
                                                     │ ro
                                              ┌──────▼────────┐
                                              │  REST API     │
                                              │  (Container)  │
                                              │  :8420        │
                                              └───────────────┘
```

| Modul | Beschreibung |
|-------|-------------|
| `src/connector/` | Comdirect API Client (Auth, Konten, Umsätze, Depot) |
| `src/api/` | Read-only FastAPI zum Bereitstellen der CSV-Exporte |
| `src/importer/` | Firefly III Client + Transaction Mapper (geplant) |
| `src/exporter/` | Finance Agent Mapper |
| `src/scheduler/` | Sync-Job Orchestrierung |
| `src/core/` | Config (pydantic-settings), Logging |
| `scripts/` | Export-Skript, Auth-Test, Debug-Skripte |

## Tech Stack

| Komponente | Technologie |
|-----------|-------------|
| Sprache | Python 3.13 |
| Paketmanager | uv |
| HTTP Client | httpx (async) |
| API | FastAPI + uvicorn |
| Config | pydantic-settings, `.env` |
| Container | Docker, docker-compose |
| Scheduler | APScheduler |

## Quickstart

### Voraussetzungen

- Python 3.13+ und [uv](https://docs.astral.sh/uv/)
- Comdirect API-Zugangsdaten (Client ID, Client Secret, Zugangsnummer, PIN)
- Docker (optional, für Container-Betrieb)

### Lokaler Export

```bash
cp .env.example .env
# .env mit Zugangsdaten füllen

# Vollständiger Export aller Finanzdaten
uv run python scripts/export_csv.py --output-dir exports

# Export mit Zeitfilter (nur Umsätze seit Datum)
uv run python scripts/export_csv.py --output-dir exports --since 2025-01-01
uv run python scripts/export_csv.py --output-dir exports --since 90d
```

### Docker

```bash
# Export-Job ausführen (interaktiv wegen TAN-Bestätigung)
docker compose run export

# API-Server starten (stellt CSVs bereit auf :8420)
docker compose up api -d
```

### API-Endpoints

| Endpoint | Beschreibung |
|----------|-------------|
| `GET /exports` | Liste aller CSV-Dateien |
| `GET /exports/latest` | Neueste Datei pro Typ |
| `GET /exports/{filename}` | CSV-Download |

## Sicherheit

- **Kein Schreibzugriff** auf Comdirect — ausschließlich Read-Only
- **Credentials** nur in `.env` — niemals im Code oder Git
- **API-Container** hat keinen Zugriff auf Credentials — nur auf das CSV-Volume (read-only)
- **Optionaler API-Token** zur Absicherung der REST API

## Datenquelle

Comdirect REST API — offizielle API der Comdirect (Commerzbank AG).

**Hinweis:** Die Authentifizierung erfordert eine TAN-Bestätigung (pushTAN). Für vollautomatisierten Betrieb ist daher eine Lösung für die TAN-Freigabe nötig.

---

*Projekt von [max5800](https://github.com/max5800) — gebaut mit KI-Agenten und viel Pragmatismus. 🐠*
