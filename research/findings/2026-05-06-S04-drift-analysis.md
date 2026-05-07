# S-04 — Live-vs-Backtest-Diskrepanz Auto-Pause

**Datum:** 2026-05-06  
**Analyst:** apex-lead  
**Roadmap-ID:** S-04  
**Severity:** P0 — Live-Sicherheit  
**DoD:** Live-PF ≤ 50% von OOS-PF nach 30 Trades → Deployment-Modus auf `shadow`

---

## Check 1 — Tabelle live_vs_backtest_drift

```
SELECT name FROM sqlite_master WHERE name='live_vs_backtest_drift'
→ []   ← Tabelle existiert NICHT
```

Alle vorhandenen Tabellen: `active_deployments`, `candles`, `features`,
`governance_log`, `heartbeats`, `lab_discoveries`, `lab_window_results`,
`opening_ranges`, `research_runs`, `signals`, `system_state`, `trades`.

→ Tabelle muss neu angelegt werden.

---

## Check 2 — Migration-Pattern in core/db.py

**Schema:** Neue Tabellen werden in der `DDL`-Konstante (ab Zeile 6) als
`CREATE TABLE IF NOT EXISTS` definiert → idempotent, kein Risiko bei mehrfachem Ausführen.

**Additive Columns:** Werden in `run_migrations()` per `ALTER TABLE ... ADD COLUMN`
mit `PRAGMA table_info`-Check nachgezogen (Zeilen 194–256).

**Neues Tabellen-Pattern:** Analog zu `lab_window_results` (Zeilen 235–256) —
`CREATE TABLE IF NOT EXISTS` im DDL-Block, dann in `run_migrations()` per
`PRAGMA table_info`-Check auf Existenz prüfen und ggf. anlegen.

---

## Check 3 — Wo wird Live-PnL pro Deployment getrackt?

`monitor/position_monitor.py` trackt PnL pro Trade (`pnl_r`, `pnl_usd`),
schreibt tägl. Summe in `system_state["daily_pnl"]`.

**Aber:** Es gibt keine Aggregation nach `strategy` (Deployment) über alle Trades.
Der Live-PF pro Deployment muss aus der `trades`-Tabelle berechnet werden:

```sql
SELECT strategy, asset, mode,
       COUNT(*)                                                    AS n_live,
       SUM(CASE WHEN pnl_r > 0 THEN pnl_r ELSE 0 END)            AS gross_win,
       SUM(CASE WHEN pnl_r < 0 THEN ABS(pnl_r) ELSE 0 END)       AS gross_loss
FROM trades
WHERE exit_ts IS NOT NULL
  AND mode IN ('live', 'dry_run')
  AND strategy = ?
```

`pf_live = gross_win / gross_loss` (oder `∞` wenn gross_loss = 0).

**Aktueller Datenstand** (aus DB):

| strategy | asset | mode | n | total_r | PF |
|----------|-------|------|---|---------|-----|
| inside_bar_breakout_334 | XRP | dry_run | 3 | +3.0R | ∞ (kein Verlust) |
| donchian_breakout_1157 | LINK | dry_run | 2 | +2.0R | ∞ |
| donchian_breakout_551 | SOL | live | 1 | +0.64R | ∞ |
| donchian_breakout_334 | XRP | live | 1 | +0.32R | ∞ |
| donchian_breakout_916 | ADA | dry_run | 1 | +1.0R | ∞ |

→ Alle Deployments haben noch **n < 30** → Drift-Check noch nicht relevant,
aber die Infrastruktur muss jetzt her, damit sie greift wenn n=30 erreicht wird.

---

## Check 4 — OOS-PF pro Deployment in lab_discoveries

`lab_discoveries` enthält `pf_test REAL` (der OOS-PF aus dem Walk-Forward-Backtest).
Über `active_deployments.discovery_id` → `lab_discoveries.id` erreichbar:

```sql
SELECT ad.strategy_key, ad.asset, ad.mode,
       ld.pf_test AS oos_pf, ld.n_test AS oos_n
FROM active_deployments ad
JOIN lab_discoveries ld ON ld.id = ad.discovery_id
WHERE ad.active = 1
```

**Aktuelle Werte:**

| strategy_key | asset | mode | oos_pf | oos_n |
|---|---|---|---|---|
| donchian_breakout_551 | SOL | live | 2.369 | 50 |
| inside_bar_breakout_334 | XRP | dry_run | 2.397 | 41 |
| donchian_breakout_571 | AVAX | dry_run | 2.597 | 56 |
| donchian_breakout_1157 | LINK | dry_run | n/a | n/a |
| donchian_breakout_916 | ADA | dry_run | n/a | n/a |

→ `pf_test` ist die richtige Vergleichsbasis für den Drift-Check.

---

## Schema-Vorschlag für live_vs_backtest_drift

```sql
CREATE TABLE IF NOT EXISTS live_vs_backtest_drift (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at      TEXT NOT NULL,                     -- ISO-Timestamp des Checks
    deployment_id   INTEGER NOT NULL                   -- FK → active_deployments.id
                    REFERENCES active_deployments(id),
    strategy_key    TEXT NOT NULL,
    asset           TEXT NOT NULL,
    mode            TEXT NOT NULL,                     -- live | dry_run
    n_live          INTEGER NOT NULL,                  -- Trades seit Deployment
    pf_live         REAL,                              -- gross_win / gross_loss (NULL wenn n<2)
    pf_oos          REAL NOT NULL,                     -- aus lab_discoveries.pf_test
    drift_pct       REAL,                              -- (pf_live - pf_oos) / pf_oos * 100
    status          TEXT NOT NULL DEFAULT 'ok',        -- ok | warning | critical | paused
    action_taken    TEXT                               -- NULL | shadow_downgrade | ...
);

CREATE INDEX IF NOT EXISTS idx_drift_deployment ON live_vs_backtest_drift(deployment_id, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_drift_status     ON live_vs_backtest_drift(status, checked_at DESC);
```

**Drift-Formel:**
```
drift_pct = (pf_live - pf_oos) / pf_oos * 100
```

| drift_pct | status | Aktion |
|-----------|--------|--------|
| > -30% | ok | keine |
| -30% bis -50% | warning | Log + Telegram-Push |
| < -50% UND n_live ≥ 30 | critical | `active_deployments.mode = 'shadow'` |

---

## Welche Module müssen angepasst werden?

### 1. core/db.py — Tabellen-Definition + Migration

- `DDL`-Block: `CREATE TABLE IF NOT EXISTS live_vs_backtest_drift` ergänzen
- `run_migrations()`: idempotenter Check analog `lab_window_results`

### 2. Neues Script: scripts/run_drift_check.py (cron: täglich)

Aufgabe:
1. Alle aktiven Deployments laden (JOIN mit lab_discoveries für pf_oos)
2. Live-PF pro Deployment aus trades berechnen (nur n ≥ 30)
3. Drift berechnen, in `live_vs_backtest_drift` schreiben
4. Bei critical: `UPDATE active_deployments SET mode='shadow'` + Telegram-Push
5. Heartbeat schreiben

### 3. monitor/telegram_bot.py — Drift-Anzeige

- `/status`-Command: Drift-Warnung anzeigen wenn status = warning/critical
- Tägl. Push wenn critical (via `push_daily_status`)

---

## Wo wird die Auto-Pause ausgelöst?

**Empfohlen: `scripts/run_drift_check.py`** als eigenständiger Cron-Job (täglich 06:00).

```python
# Pseudo-Code für die Kern-Logik:
if drift_pct < -50 and n_live >= 30:
    conn.execute(
        "UPDATE active_deployments SET mode='shadow', note=? WHERE id=?",
        (f"Auto-pause: Live-PF={pf_live:.2f} < 50% OOS-PF={pf_oos:.2f}", dep_id)
    )
    # Telegram-Push senden
    status = "critical"
    action = "shadow_downgrade"
```

**Nicht** in `position_monitor.py` — der Monitor läuft alle 2 Min,
der Drift-Check braucht keinen so kurzen Zyklus und wäre dort fehl am Platz.

---

## Migrations-Risiko

**Backup-Pflicht** vor `run_migrations()` wegen Hard Rule 4:
```bash
cp data/apex_v2.db data/backups/apex_v2_$(date +%Y%m%d_%H%M%S).db
```

Die neue Tabelle ist additiv (`CREATE TABLE IF NOT EXISTS`) —
kein Risiko für bestehende Daten.

---

## Implementierungsumfang (3 Dateien + 1 neues Script)

| Datei | Änderung | Aufwand |
|-------|----------|---------|
| `core/db.py` | DDL + Migration für neue Tabelle | Klein |
| `scripts/run_drift_check.py` | Neu — komplette Drift-Logik | Mittel |
| `monitor/telegram_bot.py` | Drift-Warning in /status + push | Klein |
| `config/settings.py` | `DRIFT_WARNING_PCT=-30`, `DRIFT_CRITICAL_PCT=-50`, `DRIFT_MIN_TRADES=30` | Klein |

**Freigabe erforderlich für:** `core/db.py` (Migration auf Live-DB),
`monitor/telegram_bot.py`, `execution/` nicht betroffen.
