# APEX V2 — Betriebs-Runbook

Stand: 2026-05-18 (geprüft) | Gültig für: Produktionsbetrieb nach P1–P3-Abschluss

---

## 1. Systemüberblick

### Laufende Prozesse

| Prozess | Cron (UTC) | Beschreibung |
|---|---|---|
| `scripts/master_run.py` | `*/5 * * * *` | Live-Pipeline: Signale → Governance → Execution |
| `scripts/master_watchdog.py` | `*/5 * * * *` | Stille-Erkennung > 15 min → Telegram-Alert |
| `scripts/dead_mans_switch.sh` | `*/5 * * * *` ¹ | Notfall-Abschaltung bei längerem Daemon-Stillstand |
| `scripts/run_reconciliation.py` | `* * * * *` ¹ | Ghost/Phantom/Size-Mismatch-Check, 1× pro Minute |
| `scripts/db_backup.py` | `50 1 * * *` | DB-Backup, Retention 7 daily + 4 weekly |
| `scripts/lab_regime_daily_check.py` | `0 6 * * *` | Regime-Erkennung pro Asset (täglich) |
| `scripts/lab_controller.py asset-profile-update` | `0 2 * * 1` | Asset-Profiler (Mo) |
| `scripts/lab_controller.py build-queue` | `0 3 * * 1` | Lab-Queue befüllen (Mo) |
| `scripts/lab_controller.py run-cycle` | `0 4 * * 1` | Research-Lab-Cycle (Mo) |
| `scripts/lab_controller.py generate-report` | `0 20 * * 5` | Weekly-Report (Fr) |
| `scripts/lab_controller.py health-check` | `10 6 * * *` | Daily Health-Check |
| `tests/test_v72_gates_immutable.py` | `5 6 * * *` | Gate-Immutabilitäts-Watchdog |

¹ Nicht in `setup_lab_cron.sh` enthalten — muss separat eingerichtet werden (eigene Crontab-Zeile).

### Schutzschichten (P1–P3)

| Schicht | Mechanismus | Auslöser |
|---|---|---|
| **Drawdown-Kill** | `DAILY_DD_KILL_R = -2.0 R` — Hard-Kill per Session | Governance-Gate bei jedem Signal |
| **Daily-Trade-Limit** | `MAX_DAILY_TRADES = 3` — blockiert neues Signal | Governance-Gate, gezählt per `entry_ts` |
| **Kill-Switch (4 Stufen)** | `none / soft / hard / manual` — manuell oder auto | `/panic`, DD-Trigger, Phantom-Erkennung |
| **Reconciler** | Ghost/Phantom/Size-Mismatch pro Cycle | `run_reconciliation.py`, Phantom → Hard-Kill |
| **clOrdId-Recovery** | Netzwerk-Timeout → Query + einmaliger R1-Retry | `executor.py` Fehlerpfad |
| **TP2-Fallback** | TP2-Fail → Hard-Kill + `close_position` | `executor.py` TP2-Pfad |
| **Dead-Man-Switch** | `master.hb` > Schwelle → `emergency_close_all.py` | `dead_mans_switch.sh` |
| **Master-Watchdog** | Stille > 15 min → Telegram | `master_watchdog.py`, Cron */5 min |
| **DB-Backup** | Täglich 01:50 UTC, 7 daily + 4 weekly | `db_backup.py` |
| **Dispatcher CB** | > 50 Msgs / 10 min → Circuit-Breaker offen | `telegram_dispatcher.py` |
| **Promotion-Gate** | Re-Eval timeout 600s → kein Discovery-Eintrag | `lab_promotion_gate.py` |
| **PBO/DSR-Gates** | `DSR ≥ 0.50 (dry) / 0.65 (live)`, `PBO ≤ 0.30` | `v7_eval.py`, immutable |

---

## 2. Tägliche Checks

### `/board` — 9 KPIs, täglich prüfen

```
/board
```

| KPI | Kritischer Wert | Aktion |
|---|---|---|
| **1. Live-DD heute** | Nahe `-2.0 R` | Prüfen ob Kill-Switch ausgelöst; falls nicht: manuell `/panic` erwägen |
| **2. Offene Positionen** | > 3 ungewöhnlich | Mit Exchange-Dashboard abgleichen; bei Diskrepanz: Reconciler-Log prüfen |
| **3. Letzter Cycle** | `failed` oder > 24h alt | `logs/lab_controller*.log` prüfen |
| **4. Aktive NCs** | Starker Anstieg | Neue Signal-Quellen haben strukturelle Probleme → kein Handlungsbedarf, nur Beobachtung |
| **5. Promotion-Kandidaten** | `n/a` | `research_staging.db` nicht erreichbar → DB-Backup prüfen |
| **6. Watchdog** | `STALE` | `master_run.py` läuft nicht → sofort prüfen (Abschnitt 3) |
| **7. Trades heute** | `3/3 heute ⚠️` | Limit erreicht, neue Signale werden blockiert — normal wenn viel Bewegung. Zählt per `entry_ts` (=Gate-Logik), optional mit Mode-Suffix `(N live, M dry_run)` |
| **8. DD heute** | Negativer R-Wert nahe Kill-Schwelle | Kill-Switch-Entscheidung vorbereiten |
| **9. Funding** | `⚠️` neben Asset | Funding > 0.05% / 8h — informativ; bei extremen Werten Position-Review |

**Wöchentlich zusätzlich:**
- `data/backups/` — mind. 7 Files mit tagesaktuellen Timestamps
- `logs/master_watchdog.log` — keine False-Positives (Alerts ohne echten Stillstand)
- `python3 tests/governance_invariants.py` — muss 0 Findings liefern

---

## 3. Störfälle / Sofortmaßnahmen

### Telegram down / kein /board erreichbar

1. SSH auf Server, `logs/api.log` prüfen
2. `python3 scripts/master_watchdog.py` manuell starten — gibt Status direkt aus
3. `python3 -c "from core.db import get_connection; c=get_connection(); print(c.execute('SELECT COUNT(*) FROM trades WHERE exit_ts IS NULL').fetchone())"` — offene Positionen zählen
4. Telegram-Bot neu starten wenn Token/Chat-ID korrekt in `config/.env`

### Exchange-API-Probleme (Bitget)

- `executor.py` hat automatischen Retry mit `-R1`-Suffix bei Netzwerk-Timeout
- Wird als `attempt.error` in `execution_audit_log` geschrieben
- Prüfen: `SELECT * FROM execution_audit_log WHERE state_to='retry_r1' ORDER BY id DESC LIMIT 10`
- Bei anhaltenden Problemen: `/panic` um neue Orders zu blockieren
- Positionen bleiben offen bis manuell oder per `/panic_clear` + Reconciler bereinigt

### Kill-Switch aktiv (`/board` Watchdog zeigt STALE oder kill_mode != none)

```sql
-- Aktuellen Mode prüfen:
SELECT value FROM system_state WHERE key='kill_mode';

-- Audit-Trail einsehen:
SELECT * FROM kill_switch_events ORDER BY id DESC LIMIT 20;
```

- **soft**: Neue Signale werden blockiert, bestehende Positionen laufen
- **hard**: Alle neuen Signale blockiert, `close_position` wurde versucht
- **manual**: Operator hat manuell gesetzt — nur `clear_kill_mode()` mit Begründung löst

Vor `/panic_clear` immer:
1. Exchange-Dashboard prüfen — alle offenen Positionen bekannt?
2. Reconciler laufen lassen
3. Begründung dokumentieren (wird in `kill_switch_events.reason` geschrieben)

### Reconciler findet Ghost-Trade (DB hat, Exchange nicht)

**Standard (RECONCILER_AUTO_HEAL_GHOST=False):**
- Alert kommt via Telegram, `reconcile_required=1` gesetzt
- Status bleibt `executed` — manuelles Review nötig
- Prüfen: `SELECT * FROM trades WHERE reconcile_required=1`
- Danach manuell `status='ghost_closed'` setzen oder Trade archivieren

**Falls Auto-Heal aktiviert (RECONCILER_AUTO_HEAL_GHOST=True):**
- Trade bekommt `status='ghost_closed'` automatisch
- Audit in `execution_audit_log` mit `cl_ord_id='RECONCILE-HEAL-{asset}'`

### Reconciler findet Phantom-Position (Exchange hat, DB nicht)

- **Immer Hard-Kill**, unabhängig von Auto-Heal-Flag
- `kill_switch_events` enthält Eintrag
- Exchange manuell prüfen: Ist die Position real oder ein API-Artefakt?
- Erst nach Klärung: `/panic_clear` mit Begründung

### DB-Problem / fehlendes Backup

```bash
# Letztes Backup prüfen:
ls -lht data/backups/ | head -10

# Manuelles Backup anlegen:
python3 scripts/db_backup.py

# Integrität prüfen:
sqlite3 data/trading.db "PRAGMA integrity_check;"
sqlite3 data/lab_state.db "PRAGMA integrity_check;"
sqlite3 data/research_staging.db "PRAGMA integrity_check;"
```

Bei Korruption: letztes Backup aus `data/backups/` einspielen, danach `run_migrations()` via `python3 -c "from core.db import run_migrations; run_migrations()"`.

---

## 4. Recovery-Abläufe

### Nur beobachten (kein Eingriff nötig)

- DD heute negativ, aber > -2.0 R → Gate läuft automatisch
- Funding ⚠️ ohne extremen Wert → informativ
- Watchdog STALE < 30 min → evtl. kurzer Deploy oder Netz-Hickup
- NC-Anstieg im Lab → strukturelle Muster, keine Live-Auswirkung

### Wann `/panic` (Hard-Kill manuell)

- Phantom erkannt und Reconciler-Auto-Kill zweifelhaft
- Unerwartete Positionen auf Exchange die nicht in DB stehen
- Telegram zeigt extremen Funding-Spike + DD bereits negativ
- Watchdog STALE > 60 min und SSH nicht erreichbar (DMS übernimmt, sonst manuell)

Syntax: `/panic` → Inline-Bestätigung anklicken (verhindert versehentliche Aktivierung)

### Wann `/panic_clear`

Erst wenn **alle** zutreffen:
1. Offene Positionen auf Exchange bekannt und in DB korrekt
2. Ursache des Kill-Switch dokumentiert und behoben
3. Reconciler sauber gelaufen (`hard_kills=0`, `alerts=0`)
4. Begründung formuliert (mind. 1 Satz — wird auditiert)

Syntax: `/panic_clear Netz stabil, Positionen geprüft, Reconciler OK`

### Reconciler / Watchdog manuell starten

```bash
python3 scripts/run_reconciliation.py
python3 scripts/master_watchdog.py
```

Outputs landen in `logs/` und via Telegram-Alert wenn Threshold überschritten.

---

## 5. Audit / Nachvollziehbarkeit

### Relevante Tabellen

| Tabelle | DB | Was steht drin |
|---|---|---|
| `kill_switch_events` | `trading.db` | Jede Kill/Clear-Mutation: ts, action, mode_from, mode_to, reason, cleared_by |
| `execution_audit_log` | `trading.db` | SL/TP-Änderungen, clOrdId-Recovery, Reconciler-Heals |
| `governance_log` | `trading.db` | Jeder Gate-Check: pass/fail, Grund |
| `lab_cycles` | `lab_state.db` | Cycle-ID, Status, Timestamp |
| `negative_controls` | `lab_state.db` | Blockierte Strategie/Asset-Paare |
| `evolution_events` | `lab_state.db` | Promotion-Versuche, Fitness-Events |
| `kill_switch_events` | `trading.db` | Vollständiger Audit-Trail inkl. Telegram-User-ID |

### Wichtige Abfragen

```sql
-- Letzter Kill-Switch-Eintrag:
SELECT ts, action, mode_to, reason, cleared_by
FROM kill_switch_events ORDER BY id DESC LIMIT 5;

-- Reconciler-Heals der letzten 24h:
SELECT cl_ord_id, state_from, payload_json, created_at
FROM execution_audit_log WHERE state_to='healed'
ORDER BY id DESC LIMIT 20;

-- Offene Trades mit Auffälligkeiten:
SELECT id, asset, entry_ts, exit_ts, status, reconcile_required
FROM trades WHERE reconcile_required=1 OR status='ghost_closed';

-- clOrdId-Recovery-Ereignisse:
SELECT signal_id, cl_ord_id, state_from, state_to, payload_json
FROM execution_audit_log WHERE state_to IN ('recovered','retry_r1')
ORDER BY id DESC LIMIT 10;
```

---

## 6. Deployment- / Änderungsregeln

**Niemals ohne Freigabe:**
- `execution/` — Live-Geld, clOrdId-Logik, SL/TP-Pfade
- `RISK_USDT`, `MAX_LEVERAGE`, `DRAWDOWN_KILL_PCT` in einer PR ändern
- `DSR_MIN_*`, `PBO_MAX`, `STABILITY_MIN`, `MAX_DD_GATE` (immutable Gates)
- `lab_discoveries` mit `status='approved'` oder `'live'` löschen
- Live-Mode-Wechsel (`shadow → dry_run → live`) ohne Telegram-Bestätigung

**Pflicht-Workflow vor jedem Merge:**
```bash
pytest tests/                          # alle Tests grün
python3 tests/parity_test.py           # Backtest == Live für alle SIGNAL_FNS
python3 tests/governance_invariants.py # keine approved-Discovery ohne Check
```

**Bei DB-Migration:**
```bash
python3 scripts/db_backup.py           # Backup vor Migration
# Migration durchführen
sqlite3 data/trading.db "PRAGMA integrity_check;"
```

**Neue Strategie:**
- Muss in `SIGNAL_FNS` (`backtest/engine.py`) UND `tests/parity_test.py` bestehen
- `cooldown_bars=8` in jedem Backtest-Aufruf — sonst sind Scores ungültig
- Feature-Berechnungen nur über `features/registry.py`, nie inline

---

## 7. Go-Live- / Post-Change-Checkliste

Nach jedem Deploy oder Konfigurationsänderung:

```
[ ] pytest tests/ — 0 FAILED
[ ] python3 tests/parity_test.py — 0 FAIL (SKIP bei kein Signal ist OK)
[ ] python3 tests/governance_invariants.py — 0 Findings
[ ] /board — alle 9 KPIs plausibel, Watchdog OK
[ ] data/backups/ — aktuelles Backup vorhanden (< 24h)
[ ] kill_switch_events — kein offener Kill-Switch
[ ] trades WHERE reconcile_required=1 — leer oder bekannte Einträge
[ ] Cron-Jobs aktiv: crontab -l | grep APEX-LAB
```

**Nach execution/-Änderung zusätzlich:**
```
[ ] test_executor_recovery.py — alle grün
[ ] test_executor_tp2.py — alle grün
[ ] test_chaos_smoke.py C-1/C-2/C-3 — alle grün
[ ] Shadow-Mode mindestens 1 Cycle beobachten vor dry_run
```

---

## 8. Optionale Verbesserungen (nicht umgesetzt, kein Blocker)

*Klar als optional markiert — kein Einfluss auf aktuelle Produktionsreife.*

| Thema | Aufwand | Nutzen |
|---|---|---|
| `/board` Wochenvergleich (Trend +/- gegenüber Vorwoche) | S | Operator-UX |
| Hot-Live-Mode-Switch via Telegram (`shadow → dry_run → live`) | M | Deployment-Sicherheit |
| Connection-Pooling in `core/db.py` | S | Performance bei hoher Lab-Last |
| R6 Race-Fix: expliziter Lock für `lab_queue`/`lab_cycles` concurrent Writer | M | Lab-Stabilität unter Last |
| `/board` Chart-Links (Grafana/externe Seite) | S | Schnellzugriff |
| Trade-Notification mit Funding-Feldern in Execution-Alerts | XS | Kontext bei Alerts |
| Dispatcher Last-Test unter echter Netzwerk-Last (nicht nur gemockt) | S | Vollständige Prod-Verifikation |
