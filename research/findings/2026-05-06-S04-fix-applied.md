# S-04 — Fix Applied: Live-vs-Backtest-Drift Auto-Pause

**Datum:** 2026-05-06  
**Roadmap-ID:** S-04 → DONE  
**py_compile run_drift_check.py:** ✅  
**py_compile telegram_bot.py:** ✅  
**Migration:** ✅ Tabelle live_vs_backtest_drift angelegt  
**Erster Drift-Check-Lauf:** ✅ 5 Deployments, alle ok

---

## Geänderte Dateien

### 1. config/settings.py — 3 neue Konstanten

```python
DRIFT_WARNING_PCT  = -30.0   # drift < -30% → Warning
DRIFT_CRITICAL_PCT = -50.0   # drift < -50% + n >= 30 → Auto-Pause
DRIFT_MIN_TRADES   = 30      # Mindest-Trade-Anzahl
```

### 2. core/db.py — DDL + Migration

Neue Tabelle im DDL-Block und idempotenter Migration-Check in `run_migrations()`:

```sql
CREATE TABLE IF NOT EXISTS live_vs_backtest_drift (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at    TEXT    NOT NULL,
    deployment_id INTEGER NOT NULL REFERENCES active_deployments(id),
    strategy_key  TEXT    NOT NULL,
    asset         TEXT    NOT NULL,
    mode          TEXT    NOT NULL,
    n_live        INTEGER NOT NULL,
    pf_live       REAL,
    pf_oos        REAL    NOT NULL,
    drift_pct     REAL,
    status        TEXT    NOT NULL DEFAULT 'ok',
    action_taken  TEXT
);
```

Migration-Ergebnis:
```
python3 -c "from core.db import run_migrations; run_migrations()"
→ Migration OK
Spalten: ['id','checked_at','deployment_id','strategy_key','asset','mode',
          'n_live','pf_live','pf_oos','drift_pct','status','action_taken']
```

### 3. scripts/run_drift_check.py — neu

Vollständige Implementierung mit:
- JOIN active_deployments + lab_discoveries für pf_oos
- Live-PF-Berechnung aus trades (gross_win / gross_loss)
- Drift-Klassifikation: ok / warning / critical
- Auto-Pause: `UPDATE active_deployments SET mode='shadow'` bei critical
- Telegram-Push bei warning/critical
- Heartbeat-Eintrag (component='drift_check')

### 4. monitor/telegram_bot.py — Drift-Sektion in build_status_text()

Neue Sektion `*══ DRIFT MONITOR ═══════════*` vor dem Return:
- Zeigt alle Deployments mit status=warning oder critical
- Zeigt PF-live, PF-oos, Drift%, n, action_taken
- Fehler werden geschluckt (kein Absturz wenn Tabelle leer)

### 5. Crontab — neuer Eintrag

```cron
0 6 * * *   python3 scripts/run_drift_check.py >> logs/drift_check.log 2>&1
```

---

## Erster Drift-Check-Output (2026-05-06 18:57 UTC)

```
[DRIFT] Drift-Check gestartet
[DRIFT] 5 aktive Deployments geladen
[DRIFT] donchian_breakout_551  SOL  | n=1 | pf_live=n/a | pf_oos=2.37 | drift=n/a | status=ok
[DRIFT] inside_bar_breakout_334 XRP | n=3 | pf_live=n/a | pf_oos=2.40 | drift=n/a | status=ok
[DRIFT] donchian_breakout_571  AVAX | n=0 | pf_live=n/a | pf_oos=2.60 | drift=n/a | status=ok
[DRIFT] donchian_breakout_1157 LINK | n=2 | pf_live=n/a | pf_oos=2.51 | drift=n/a | status=ok
[DRIFT] donchian_breakout_916  ADA  | n=1 | pf_live=n/a | pf_oos=2.85 | drift=n/a | status=ok
[DRIFT] Fertig: 5 Deployments geprüft | 10ms
```

`pf_live=n/a` und `drift=n/a` sind korrekt: alle Deployments haben ausschließlich
Gewinn-Trades (kein Verlust-Trade → PF = ∞, Drift nicht berechenbar).
Der Check greift automatisch sobald der erste Verlust-Trade gebucht und n ≥ 30 ist.

---

## Drift-Logik zusammengefasst

| drift_pct | n_live | status | Aktion |
|-----------|--------|--------|--------|
| > -30% | beliebig | ok | keine |
| < -30% | beliebig | warning | Log + Telegram-Push |
| < -50% | < 30 | warning | Log + Push (noch kein Auto-Pause) |
| < -50% | ≥ 30 | critical | `mode='shadow'` + Telegram-Push |
