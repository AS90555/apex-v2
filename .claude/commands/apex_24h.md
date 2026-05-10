Führe eine vollständige 24h-System-Analyse durch:

BLOCK 1 — Trade-Qualität:
```python
python3 -c "
from core.db import get_connection
from datetime import datetime, timedelta
conn = get_connection()
cutoff = int((datetime.utcnow() - timedelta(hours=24)).timestamp() * 1000)

# Offene Positionen + TP1-Status
open_trades = conn.execute('''
    SELECT asset, strategy, direction, entry_price, stop_loss,
           tp1_partial_done, size
    FROM trades WHERE exit_ts IS NULL
''').fetchall()

print(f'=== OFFENE POSITIONEN ({len(open_trades)}) ===')
for t in open_trades:
    status = 'BREAKEVEN' if t[5] else 'RISIKO'
    sl_ok = 'SL-BUG' if (t[2]=='long' and t[4]>t[3] and not t[5]) else ''
    print(f'{t[0]} {t[2]} entry={t[3]:.4f} sl={t[4]:.4f} {status} {sl_ok}')

# Geschlossene Trades letzte 24h
closed = conn.execute('''
    SELECT asset, strategy, direction, pnl_r, exit_reason
    FROM trades WHERE exit_ts > ? AND exit_ts IS NOT NULL
    ORDER BY exit_ts DESC
''', (cutoff,)).fetchall()

print(f'\n=== GESCHLOSSENE TRADES LETZTE 24H ({len(closed)}) ===')
total_r = 0
for t in closed:
    total_r += t[3] or 0
    print(f'{t[0]} {t[2]} {t[4]}: {t[3]:+.2f}R')
print(f'Gesamt: {total_r:+.2f}R')
"
```

BLOCK 2 — Signal-Qualität:
```python
python3 -c "
from core.db import get_connection
from datetime import datetime, timedelta
conn = get_connection()
cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()

rows = conn.execute('''
    SELECT status, strategy, asset, COUNT(*) as n
    FROM signals WHERE created_at > ?
    GROUP BY status, strategy, asset
    ORDER BY n DESC
''', (cutoff,)).fetchall()

print('=== SIGNAL-STATUS LETZTE 24H ===')
for r in rows:
    label = 'OK' if r[0]=='executed' else 'FAIL' if r[0]=='failed' else 'REJ'
    print(f'{label} {r[0]:12} {r[1]:25} {r[2]:6} n={r[3]}')

failed = [r for r in rows if r[0]=='failed']
if failed:
    print(f'\nFAILED-SIGNALS: {sum(r[3] for r in failed)} total — Execution-Fehler untersuchen')
"
```

BLOCK 3 — Pipeline-Health:
```python
python3 -c "
from core.db import get_connection
from datetime import datetime, timezone
conn = get_connection()
now = datetime.now(timezone.utc)

components = conn.execute('''
    SELECT component, status, ts, latency_ms
    FROM heartbeats ORDER BY ts DESC
''').fetchall()

seen = {}
print('=== HEARTBEATS ===')
for c in components:
    if c[0] not in seen:
        seen[c[0]] = c
        try:
            lb = datetime.fromisoformat(c[2].replace('Z','+00:00'))
            age_min = int((now - lb).total_seconds() / 60)
        except Exception:
            age_min = -1
        status = 'OK' if age_min < 10 else 'WARN' if age_min < 30 else 'STALE'
        print(f'{status:5} {c[0]:25} {age_min:3}min alt | latency={c[3]}ms')
"
```

BLOCK 4 — Lab-Status:
```python
python3 -c "
from core.db import get_connection
conn = get_connection()

new = conn.execute('''
    SELECT COUNT(*) FROM lab_discoveries
    WHERE cost_model_applied=1
''').fetchone()[0]

total = conn.execute('SELECT COUNT(*) FROM lab_discoveries').fetchone()[0]

print('=== LAB-STATUS ===')
print(f'Discoveries mit Kostensimulation: {new}')
print(f'Gesamt Pool: {total}')

deps = conn.execute('''
    SELECT strategy, asset, mode FROM active_deployments
    WHERE active=1
''').fetchall()
print(f'Deployments aktiv: {len(deps)}')
for d in deps:
    print(f'  {d[0]} {d[1]} [{d[2]}]')
"
```

BLOCK 5 — Drift-Check:
```bash
python3 scripts/run_drift_check.py 2>&1 | grep -E "DRIFT|status|pf_live" | head -20
```

Erstelle danach eine Zusammenfassung in diesem Format:

## 24h System Report [DATUM]
- **Trade-Bilanz:** X geschlossen, Y offen (Z im Breakeven nach TP1)
- **Signal-Qualität:** A executed, B rejected, C failed
- **Pipeline:** alle grün / Warnung bei [Komponente]
- **Lab:** X Discoveries gesamt, Y mit Kostensimulation
- **Kritische Findings:** [falls vorhanden, sonst "keine"]
- **Empfehlung:** [nächste sinnvolle Aktion]

Hinweis zu SL > Entry bei offenen Positionen: Das ist KEIN Bug wenn tp1_partial_done=1 —
der Monitor verschiebt den SL nach TP1-Hit auf Entry+ATR als Trailing-Stop (Gewinnsicherung).
Nur wenn tp1_partial_done=0 und SL auf der falschen Seite liegt, ist es ein echter Bug.
