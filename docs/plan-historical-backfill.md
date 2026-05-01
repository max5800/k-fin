# Plan — Historischer Backfill für Konto-Transaktionen

**Status:** Entwurf zur Review
**Datum:** 2026-05-01

---

## 1. Hintergrund

Der heutige Sync (`/sync/start` → TAN → `/sync/confirm`) holt nur das aktuelle Default-Fenster der Comdirect-API. Wir wollen die History bis zu **3 Jahre** zurück abholbar machen, ohne den schnellen Inkrement-Sync zu kompromittieren.

### Constraints aus der Probe (`scripts/probe_account_paging.py`, 2026-05-01)

| Frage | Ergebnis |
|---|---|
| `paging-first` (Offset-Paging) am Konto-Endpoint? | **HTTP 422** — funktioniert nicht |
| `max-bookingDate` am Konto-Endpoint (undokumentiert)? | **✓ Akzeptiert + filtert** — matches sinkt korrekt |
| `min` + `max` als echtes Fenster? | **✓ Funktioniert** |
| `min-bookingDate=-1095d` (3y) mit `paging-count=500`? | HTTP 422 — entweder Lookback-Cap oder Param-Kombi-Problem |

**Kernkonsequenz:** Backfill geht **nur** über Datums-Stückelung (Window-Walk rückwärts). Offset-Paging ist tot.

---

## 2. Design-Prinzipien

1. **TAN-in-the-loop ist Feature.** Jeder Backfill-Run startet aus der UI mit pushTAN. Kein Cron, kein Hintergrund-Refresh.
2. **Backfill bleibt separat vom Sync.** `/sync/confirm` zieht weiter nur das Inkrement (z. B. `min=-30d`); `/backfill/confirm` macht den Window-Walk. Beide teilen sich den Auth-Flow, aber unterschiedliche Stop-Bedingungen.
3. **Idempotent.** Re-Runs sind gefahrlos — der `content_hash` in [src/normalization/ingest.py:72-79](../src/normalization/ingest.py#L72-L79) dedupliziert vollständig.
4. **Sichtbarer Fortschritt.** Long-running (mehrere Minuten) → BackgroundTask + Polling-Endpoint, UI zeigt Progress-Bar. Pattern analog zu Agent-Runs ([src/api/routers/runs.py](../src/api/routers/runs.py)).
5. **Keine Schema-Brüche.** Ingest-Pipeline und `raw_transactions` bleiben unverändert.

---

## 3. Architektur

### 3.1 Connector — neue Methode

**Datei:** [src/connector/comdirect_client.py](../src/connector/comdirect_client.py)

```python
async def get_transactions_window(
    self,
    account_id: str,
    from_date: date,
    to_date: date,
    *,
    paging_count: int = 500,
    transaction_state: str = "BOOKED",
) -> tuple[list[dict], int]:
    """Fetch one date-window of account transactions.
    
    Returns (values, matches). Caller decides if matches==paging_count
    means truncation and a smaller window is needed.
    """
```

- Setzt `min-bookingDate`, `max-bookingDate`, `paging-count`, `transactionState=BOOKED`.
- Bestehende `get_transactions(...)` bleibt für den Inkrement-Sync **unverändert**.
- HTTP 422 wird nicht hart geraised, sondern als `(values=[], matches=0)` mit WARN-Log zurückgegeben — das macht den Window-Walk robust gegen Boundary-Reached-Fälle.

### 3.2 Backfill-Service

**Neue Datei:** `src/scheduler/backfill.py`

Pure Logik, kein FastAPI:

```python
async def run_backfill(
    client: ComdirectClient,           # bereits authentifiziert
    pipeline: NormalizationPipeline,
    *,
    target_start_date: date,           # z. B. heute - 3y
    initial_window_days: int = 30,
    progress_cb: Callable[[BackfillProgress], None] | None = None,
) -> BackfillResult:
    """Walk backwards in monthly windows, ingest each, until target or boundary."""
```

**Algorithmus:**

```
fenster_ende = heute
fenster_anfang = heute - initial_window_days

WHILE fenster_anfang >= target_start_date:
    values, matches = get_transactions_window(account_id, fenster_anfang, fenster_ende)
    
    IF matches >= paging_count:        # Cap getroffen → Fenster halbieren
        WARN; window_days /= 2; retry same fenster_ende
        IF window_days < 1: ERROR (mehr als 500 tx/Tag — sollte nie passieren)
    
    IF values: ingest_transactions(pipeline, values, batch_id=f"backfill-{...}")
    
    fenster_ende = fenster_anfang - 1 day
    fenster_anfang = fenster_ende - window_days
    
    progress_cb(BackfillProgress(...))   # für UI-Polling

LAST: pipeline.process_and_normalize()    # einmal am Ende, nicht per Fenster
```

**Stop-Bedingungen:**
- `fenster_anfang < target_start_date` (Hard-Stop)
- HTTP 422 / Hard-Error
- Heuristik *vorerst draußen* — bei langen Konto-Inaktivitäten würden echte Lücken abgeschnitten. Kommt rein wenn sich's in der Praxis als Problem zeigt.

### 3.3 DB-Modell

**Neu:** `BackfillRun` in [src/core/db/models.py](../src/core/db/models.py)

| Feld | Typ |
|---|---|
| `id` | UUID, pk |
| `account_id` | str (Comdirect accountId) — NULLable für "alle Konten" |
| `target_start_date` | date |
| `current_window_start` | date — fortschreibend, fürs Resume |
| `windows_done` | int |
| `rows_inserted` | int |
| `status` | enum (running, succeeded, failed, cancelled) |
| `error` | str nullable |
| `started_at`, `finished_at` | timestamps |

**Migration:** neue Alembic-Revision in [alembic/versions/](../alembic/versions/).

**Resume-Verhalten:** Für M1 nicht implementiert — eine neue Backfill-Run startet immer bei `target_start_date`. Das ist ok weil `content_hash` dedupliziert. Resume kann später nachgezogen werden, wenn Sec-Token-Lifetime sich als Problem erweist.

### 3.4 Worker-Endpoints

**Datei:** [main.py](../main.py) — analog zu `/internal/sync/start` und `/internal/sync/confirm`.

```
POST /internal/backfill/start
    Body: { months: int }   # default 24, max 24 (3y-API-Cap nicht voll ausgeschöpft, da Probe T5 fehlschlug)
    → checkt: kein BackfillRun mit status=running existiert (sonst 409 Conflict)
    → ruft begin_auth auf, parkt config in _pending_sessions
    → 200 { status: "pending_tan", session_id }

POST /internal/backfill/confirm?session_id=...
    → complete_auth
    → erzeugt BackfillRun-Row (status=running)
    → BackgroundTasks → run_backfill(...)
    → 202 { run_id }

GET /internal/backfill/runs/{run_id}
    → liest BackfillRun-Row
    → 200 { status, windows_done, rows_inserted, current_window_start, error }
```

**Wichtig:** Der `/confirm`-Endpoint kehrt sofort zurück, sobald der BackgroundTask läuft. Status-Polling läuft über den separaten `/runs/{id}`-Endpoint. So überlebt der Backfill auch wenn die HTTP-Connection des Browsers timeoutet.

### 3.5 API-Proxy

**Datei:** [src/api/routers/sync.py](../src/api/routers/sync.py) — drei neue Routes:

```
POST /api/v1/sync/backfill/start       → proxy zu /internal/backfill/start
POST /api/v1/sync/backfill/confirm     → proxy zu /internal/backfill/confirm
GET  /api/v1/sync/backfill/runs/{id}   → proxy zu /internal/backfill/runs/{id}
```

Auth über bestehenden `Auth`-Dependency. Bleibt im selben Router weil's logisch zum Sync gehört.

### 3.6 UI

**Datei:** [k-fin-ui/src/components/Settings.tsx](../../k-fin-ui/src/components/Settings.tsx) — neuer Abschnitt unter "Sync (Daten holen)".

**Komponenten:**
- Button "Historie nachladen" mit Datum-Picker (Default: 24 Monate zurück, max 36).
- TAN-Modal — wiederverwendet das bestehende Pattern aus dem Sync-Flow (Lines 322–390).
- Nach TAN-Confirm: **neuer** Progress-Modal (statt einfach zu schließen):
  - Zeigt: "Fenster X/Y verarbeitet", "Z Transaktionen importiert", aktuelles Datum.
  - Polled `GET /api/v1/sync/backfill/runs/{run_id}` alle 2 s.
  - Schließt automatisch bei `status=succeeded`/`failed`.
  - Cancellable: das Modal kann zugeklappt werden, der Job läuft im Worker weiter; UI zeigt das beim Wieder-Öffnen oder als Banner.
- Neuer API-Hook in [k-fin-ui/src/api/sync.ts](../../k-fin-ui/src/api/sync.ts): `useStartBackfill`, `useConfirmBackfill`, `useBackfillRun(id)` (Query mit `refetchInterval: 2000`).

---

## 4. Etappen / PRs

Jede Etappe ist für sich mergeable, hat Tests, läuft grün durch CI.

### Etappe 1 — Connector + Probe-Skript-Cleanup
- [ ] `get_transactions_window` in `comdirect_client.py`
- [ ] Unit-Tests mit mocked httpx (Cap-Detection, 422-Handling)
- [ ] Probe-Skript löschen

### Etappe 2 — Backfill-Logik + DB-Modell
- [ ] `src/scheduler/backfill.py` mit `run_backfill()`
- [ ] `BackfillRun`-Modell + Alembic-Migration
- [ ] Tests: synthetisches Fenster-Walking, Cap-Halbierung, leere Fenster, Boundary-422

### Etappe 3 — Worker-Endpoints
- [ ] `/internal/backfill/start`, `/confirm`, `/runs/{id}` in `main.py`
- [ ] Reuse `_pending_sessions` für TAN-State
- [ ] BackgroundTask schreibt Status fortlaufend in `BackfillRun`
- [ ] Tests: TAN-Flow, Status-Updates, Failure-Recovery

### Etappe 4 — API-Proxy
- [ ] Routes in `src/api/routers/sync.py`
- [ ] Tests: Auth-Required, Worker-Unreachable-Fallback

### Etappe 5 — UI
- [ ] Hook + Komponente in `k-fin-ui`
- [ ] TAN-Modal-Reuse, Progress-Modal-Neu
- [ ] Lokal getestet im Browser (golden path + cancel + error)

**Reihenfolge:** 1→2→3 können nacheinander direkt in Hauptbranch (Backend), 4+5 zusammen als sichtbares Feature.

---

## 5. Risiken & offene Punkte

| Risiko | Mitigation |
|---|---|
| Sec-Token läuft während Backfill ab | Erst empirisch messen. Notfall: Token-Lifetime in BackgroundTask überwachen, vorzeitig stoppen mit Resume-fähigem State (für M1 OOS). |
| API-422 bei Lookback > N (siehe T5 in Probe) | Window-Walk macht das transparent — wir lernen die Boundary beim ersten Run und stoppen dort. Keine zusätzliche Logik nötig. |
| User triggert Backfill mehrfach parallel | DB-Constraint: max 1 BackfillRun mit `status=running` pro User. Worker-Endpoint checkt das. |
| 500-Cap auf busy Giro nicht testbar | Adaptive Halbierung im Window-Walk fängt das ab. WARN-Log macht's sichtbar. |
| Lange Operation crasht Worker-Pod (OOM, etc.) | Status bleibt `running` in DB. Beim nächsten Sync sieht man verwaisten Run; UI zeigt "abgebrochen" wenn `started_at` älter als X. (Cleanup-Logik in Etappe 3.) |

### Bewusst aus dem Scope draußen

- **Resume eines abgebrochenen Backfills.** Erst wenn's nachweislich nötig wird (Sec-Token-Lifetime zu kurz).
- **Depot-Backfill.** Depot hat schon `min-bookingDate=-730d`, der Bedarf ist kleiner. Kommt als Folge-Feature wenn Account-Backfill steht.
- **Cron-getriggert.** Per Design ausgeschlossen (TAN-in-the-loop).
- **Backfill nur für ausgewählte Konten.** Erstmal alle Konten. Per-Account-Opt-In wäre UI-Aufwand ohne klaren Mehrwert.

---

## 6. Akzeptanzkriterien

- [ ] User kann in Settings "Historie nachladen" klicken, Zeitraum wählen, TAN bestätigen.
- [ ] Während des Backfills sieht der User Progress (Fenster X/Y, N Transaktionen importiert).
- [ ] Nach Abschluss erscheint ein Summary: gesamt importierte Rows + Zeitraum + Dauer.
- [ ] Re-Run mit gleichen Parametern fügt **0 neue Rows** ein (Idempotenz).
- [ ] Inkrementeller Sync (`/sync/start` → TAN → `/sync/confirm`) bleibt unverändert in Verhalten und Performance.
- [ ] Test-Coverage: Connector + Backfill-Service + Worker-Endpoints jeweils mit Unit-Tests.
- [ ] CI grün, Conventional Commits.
