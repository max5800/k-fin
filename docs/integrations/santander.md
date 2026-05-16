# Santander Credit-Card Integration — Anbindungs-Spike (M16-P2c)

> **Status:** Spike abgeschlossen — Strategie-Entscheidung getroffen, Connector
> gegen einen *dokumentierten, angenommenen* API-Contract implementiert.
> Die konkrete Endpoint-Verifikation gegen die Live-Umgebung steht noch aus
> (siehe §4 Capture-Checkliste) und ist als bewusste Maintenance-Aufgabe für
> einen späteren AI-Agenten/Maintainer ausgewiesen.

Dieser Spike ist das harte Gate für P2c: *„Ohne diesen Spike kein weiterer
P2c-TODO."* Er hält die Anbindungsstrategie, das TAN-Verfahren und die
ToS-Risiko-Bewertung fest, bevor Connector-Code geschrieben wird.

---

## 1. Entität & Karten-Identifikation

| Frage | Ergebnis |
|---|---|
| Karte | **Santander 1plus Visa Card** (kostenlose Reise-Kreditkarte) |
| Emittent | **Santander Consumer Bank AG** (Deutschland) |
| Verwaltungsoberfläche | **„MySantander" Online-Banking** (Web) + **Santander Mobile-Banking-App** |
| Login-Identität | Online-Banking-Nutzername + Passwort (separat von der Kartennummer) |

**Drift-Risiko — Corporate-Reorg:** Die Santander Consumer Bank AG firmiert
derzeit zur **Openbank Deutschland AG** um. Branding, Hostnamen und
Endpoint-Pfade können sich dadurch verschieben. Der Connector ist deshalb
bewusst defensiv gebaut (klare Fehler bei Struktur-Drift, §9) — eine
Pfad-/Host-Änderung muss als sauber lokalisierter Fehler auflaufen, nicht als
stiller Garbage-Parse.

Die „1plus Visa Card" wurde im Neugeschäft durch die **„BestCard"**-Reihe
abgelöst; Bestandskarten laufen weiter. Der Connector ist kartentyp-agnostisch
gebaut — er liest die Kreditkarten-Liste und iteriert, statt einen Kartentyp
hart zu kodieren.

## 2. Kandidaten-Anbindungsstrategien

Drei mögliche Anbindungsflächen wurden bewertet:

### (a) PSD2 / XS2A — sanktioniert, **verworfen**
Santander stellt eine PSD2-AIS-API bereit. Der Zugriff verlangt jedoch ein
**QWAC-Zertifikat eines qualifizierten Vertrauensdiensteanbieters** — d.h. man
muss ein bei der BaFin registrierter/lizenzierter TPP (Third-Party-Provider)
sein. Für ein privates Open-Source-Tool ist das nicht praktikabel. Zudem ist
die PSD2-AIS in einigen Märkten an Avida ausgelagert; der deutsche Status ist
unklar. → **Dokumentiert, aber nicht der Weg.**

### (b) MySantander-API reverse-engineered — fragil, **nicht Default**
Die MySantander-Web-/Mobile-Oberfläche spricht intern eine JSON-API. Diese
ließe sich per Traffic-Capture rekonstruieren. Nachteile: hohe Drift-Anfälligkeit
(undokumentiert, versioniert ohne Ankündigung), das 2FA-Gate (§3) bricht
automatisierte Abrufe regelmäßig, und es ist die ToS-riskanteste Variante.

### (c) Authentifizierter CSV-Export innerhalb der Session — **empfohlener Default**
MySantander bietet im Web-Banking einen nativen **Transaktions-Export als
`transactions.csv`**. Der Connector loggt sich mit den Credentials des
Maintainers ein, navigiert zur Kreditkarten-Umsatzansicht und triggert den
**offiziellen Export-Button** statt DOM-/JSON-Scraping. Vorteile: drift-resistent
(der Export ist ein stabiles, vom Institut gepflegtes Feature), niedrigeres
ToS-Risiko (kein Eingriff in private APIs), und das Ergebnis ist ein
wohldefiniertes Tabellenformat.

> **Entscheidung:** Strategie **(c)** als Default. Der Connector
> (`src/external/santander_client.py`) ist gegen einen *abstrahierten*
> Umsatz-Abruf gebaut: er kapselt Login → Session → Kreditkarten-Umsatz-Abruf
> hinter einer Methode `get_credit_card_transactions()`. Ob diese intern den
> CSV-Export oder den JSON-Endpoint anspricht, ist ein Implementierungsdetail
> hinter dem defensiven Parsing-Layer — austauschbar ohne Provider-Änderung.

## 3. TAN-Verfahren-Entscheidung

Santander Consumer Bank hat das Online-Banking auf **2FA** umgestellt. Belege
aus der OSS-Sichtung (§7): Screen-Scraping-Tools melden seit der 2FA-Umstellung
gebrochene Umsatzabrufe — automatisierter Zugriff ist **TAN-pflichtig**.

Verfügbare Verfahren: **mobileTAN (SMS)** und **App-Bestätigung** (Push in die
Santander-Mobile-Banking-App).

**Entscheidung:** `tan_kind = TanKind.SCRAPING_SESSION`.

- `start_sync()` führt den Username/Passwort-Login aus und löst die
  Stark-Authentifizierung aus; es liefert eine `SyncChallenge` zurück.
- Der Maintainer bestätigt **out-of-band** (App-Push bzw. mobileTAN).
- `complete_sync(None)` prüft, ob die Session elevated ist, und ruft die
  Umsätze ab. **Es wird kein OTP in k-fin getippt** — das hält die bestehende
  Worker-Plumbing (`/internal/sync/{source}/complete` ohne Body) unverändert.
- Wenn die Session bei `complete_sync` noch nicht bestätigt ist, wirft der
  Connector einen klaren Fehler (`SantanderAuthError`).

**TAN-in-the-loop-Regel:** `SCRAPING_SESSION` ist — wie Comdirect — **strikt
user-getriggert**. Kein silent background sync. (Nur `NONE_OAUTH_M2M`/PayPal
darf unbeaufsichtigt laufen. Siehe `00_PROJEKTPLAN_KANONISCH.md` und die
Provider-Contract-Doku in `src/external/provider.py`.)

## 4. Endpoint-Liste (angenommener Contract + Capture-Checkliste)

Der Connector implementiert gegen den folgenden **angenommenen** Contract.
Pfade/Hosts sind Platzhalter und **müssen** per Live-Capture verifiziert werden.

| Schritt | Methode | Pfad (angenommen) | Zweck |
|---|---|---|---|
| Login | `POST` | `/api/auth/login` | Username/Passwort → Session-Cookie + ggf. 2FA-Challenge |
| 2FA-Status | `GET` | `/api/auth/session` | Pollt, ob die Session elevated/bestätigt ist |
| Karten-Liste | `GET` | `/api/credit-cards` | Liefert die Kreditkarten des Nutzers (`card_id`, `last4`) |
| CC-Umsätze | `GET` | `/api/credit-cards/{card_id}/transactions` | Umsatzliste je Karte |

**Capture-Checkliste (Maintainer, mit eigenem Login):**

- [ ] MySantander-Web im Browser einloggen, DevTools → Network-Tab öffnen.
- [ ] Login-Request mitschneiden: Host, Pfad, Payload-Form, Cookie-Namen.
- [ ] 2FA-Schritt beobachten: separater Request? Polling? Redirect?
- [ ] Kreditkarten-Umsatzansicht öffnen — Request für die Umsatzliste
      mitschneiden (JSON-Endpoint **oder** CSV-Export-URL).
- [ ] Feldnamen der Umsatz-Antwort notieren und mit `santander_models.py`
      abgleichen — Abweichungen dort als Aliase ergänzen.
- [ ] Host/Pfade in `santander_client.py` (`_BASE_URL`, `_PATHS`) eintragen.

## 5. Session-Lifetime & Re-Auth-Bedingungen

Aus der Desk-Research nicht abschließend belegbar — als Annahmen im
Modulkopf von `santander_client.py` dokumentiert und per Capture zu schärfen:

- **Session-Lifetime:** vermutlich kurz (10–15 min Inaktivität), Cookie-basiert.
- **Re-Auth:** jeder Sync ist ein voller `start_sync` → 2FA → `complete_sync`
  Zyklus. Es wird **keine** Session über Sync-Läufe hinweg persistiert.
- **Optionaler `santander_device_id`:** falls Santander ein bestätigtes Gerät
  („Gerät merken") anbietet, kann eine `device_id` 2FA-Friktion reduzieren.
  Als optionales Setting vorgesehen (`settings.santander_device_id`), per
  Default leer → voller 2FA-Flow.

## 6. Rate-Limit-Verhalten

Nicht öffentlich dokumentiert. Konservative Default-Annahme: der Connector
macht **sequentielle** Requests, kein paralleler Fan-out, kein aggressives
Retry. Ein HTTP 429 wird als harter Fehler durchgereicht — der Sync wird vom
Nutzer später erneut getriggert (gleiches Muster wie der PayPal-Client).

## 7. OSS-Vorlagen-Sichtung (Studienobjekte — kein Code-Übernahme)

| Projekt | Erkenntnis |
|---|---|
| `python-fints` / `libfintx` / `fints4k` | FinTS/HBCI-Clients — **N/A**: Santander unterstützt FinTS/HBCI **nicht**. |
| Hibiscus-Scripting-Community | Screen-Scraping-Skripte für Santander; Berichte über gebrochene Abrufe seit der 2FA-Umstellung — bestätigt §3. |
| StarMoney-Foren | Bestätigen: Santander = Screen-Scraping, kein Standard-Interface. |
| KontoCSV / SmartKontoauszug | Kommerzielle PDF/CSV-Konverter für Santander-Auszüge — bestätigen, dass ein CSV-Export existiert (§2c). |

Es wurde **kein Code übernommen** — die Projekte dienten ausschließlich der
Strategie-Validierung.

## 8. ToS-Risiko-Bewertung

- Automatisierter Zugriff auf MySantander ist in den Online-Banking-AGB
  mit hoher Wahrscheinlichkeit nicht ausdrücklich gestattet (AGB-Graubereich).
- **Strategie (c)** — eigener Login, eigener Export-Button, eigene Daten — ist
  **risikoärmer** als Strategie (b) (Reverse-Engineering privater APIs).
- Es findet **kein Schreibzugriff** statt: der Connector ist strikt read-only,
  es existiert kein einziger Mutation-Pfad.
- Empfehlung: rein privater Gebrauch, niedrige Abruffrequenz (user-getriggert),
  keine Weitergabe von Credentials. Der Maintainer trägt das Restrisiko bewusst.

## 9. Provider-Capabilities (für die P2a-Contract-Implementation)

| Capability | Wert | Begründung |
|---|---|---|
| `supports_depot` | `DepotSupport.NONE` | Kreditkarte hat kein Depot. |
| `supports_watchlist` | `False` | — |
| `supports_orders` | `False` | — |
| `supports_pending_bookings` | `True` | Kreditkarten haben regelmäßig vorgemerkte (noch nicht abgerechnete) Autorisierungen; MySantander zeigt „vorgemerkt". Der Connector liefert sie mit; das Feld kann später für eine UI-Trennung genutzt werden. |
| `tan_kind` | `TanKind.SCRAPING_SESSION` | Siehe §3. |

**Defensive Endpoint-/Selector-Logik:** `santander_client.py` und
`santander_models.py` parsen defensiv — fehlende Pflichtfelder oder eine
unerwartete Antwortstruktur lösen einen klaren `SantanderParseError` /
`SantanderAuthError` aus, statt stillschweigend leere/falsche Daten zu liefern.
Das gibt einem späteren AI-Agenten bei Struktur-Drift einen eindeutigen
Ankerpunkt.

## 10. Go / No-Go für den restlichen P2c-Code

**GO** — mit folgender Maßgabe:

- Connector, Models, Canonicalize-Adapter, Provider, Tests werden gegen den
  in §4 dokumentierten **angenommenen** Contract gebaut. Tests laufen gegen
  **synthetische Fixtures** (Dummy-Kartennummer `4111 1111 1111 1111`,
  Dummy-Merchants, offensichtliche Test-Beträge) — kein Live-Zugriff in CI.
- Vor dem **ersten echten Live-Sync** muss die Capture-Checkliste (§4)
  abgearbeitet und Host/Pfade/Feldnamen verifiziert werden. Bis dahin ist der
  Santander-Provider registriert, aber ein Live-Sync schlägt mit klarem
  Fehler fehl (Platzhalter-Host).
- FX wird durchgängig erhalten: `original_amount` + `original_currency` werden
  persistiert und in der UI angezeigt — Auslandskäufe sind bei Reise-Kreditkarten
  der Normalfall, kein blindes EUR-Cast.
