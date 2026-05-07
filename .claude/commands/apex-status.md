Führe folgende Analyse als apex-lead aus:

1. Lies research/state/master-roadmap.md
2. Lies research/state/improvement-backlog.md  
3. Führe aus: python3 scripts/run_drift_check.py
4. Führe aus: python3 -c "
from core.db import get_connection
from research.train_hmm import get_current_regime
conn = get_connection()
assets = [r[0] for r in conn.execute(
    'SELECT DISTINCT asset FROM active_deployments WHERE active=1'
).fetchall()]
for asset in assets:
    try:
        print(f'{asset}: {get_current_regime(asset, conn)}')
    except Exception as e:
        print(f'{asset}: kein Modell ({e})')
"
5. Lies logs/master.log letzte 50 Zeilen

Gib aus:
## System-Status [DATUM]
- Aktive Deployments + Modus + n_live_trades
- Drift-Status aller Deployments
- Aktuelles HMM-Regime pro Asset
- Letzte 3 Fehler aus master.log
- Top-3 offene Roadmap-Items
- Top-3 Backlog-Items