# 🏦 comdirect-firefly-sync

**Automatischer Read-Only Import von Comdirect-Kontodaten in Firefly III.**

> Konten immer aktuell — ohne manuelle Exporte.

## Was macht das?

- Holt Konten und Umsätze aus der **Comdirect REST API** (read-only)
- Importiert sie periodisch in **Firefly III** via API
- Verhindert Duplikate
- Läuft als eigenständiger Service (Docker / K8s)

## Tech Stack

| Komponente | Technologie |
|-----------|-------------|
| Connector | Python, httpx, comdirect REST API |
| Importer | Python, Firefly III JSON API |
| Scheduler | APScheduler |
| Config | pydantic-settings, .env |

## Quickstart

```bash
cp .env.example .env
# Füll .env mit deinen Zugangsdaten
./scripts/run.sh
```

## Datenquelle

Comdirect REST API — offizielle API der Comdirect (Commerzbank AG).
Nur Read-Only Zugriff auf eigene Konten und Umsätze.

## Sicherheitshinweis

- Kein Write-Zugriff auf Comdirect
- Keine Zahlungen, keine Orders
- Credentials lokal in `.env` (niemals in Git)

---

*Projekt von [max5800](https://github.com/max5800) — gebaut mit KI-Agenten und viel Pragmatismus. 🐠*
