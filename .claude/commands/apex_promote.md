Führe folgende Analyse durch:

1. Führe aus: python3 -c "
from core.db import get_connection
from research.train_hmm import get_current_regime

conn = get_connection()
deps = conn.execute(
    \"SELECT id, strategy_key, base_strategy, asset, mode FROM active_deployments WHERE mode='dry_run' AND active=1\"
).fetchall()

for dep in deps:
    dep = dict(dep)
    rows = conn.execute(
        'SELECT pnl_r FROM trades WHERE strategy=? AND asset=? AND exit_ts IS NOT NULL',
        (dep['strategy_key'], dep['asset'])
    ).fetchall()
    n = len(rows)
    gross_win  = sum(r[0] for r in rows if r[0] and r[0] > 0)
    gross_loss = abs(sum(r[0] for r in rows if r[0] and r[0] < 0))
    pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else None

    drift_row = conn.execute(
        'SELECT status FROM live_vs_backtest_drift WHERE strategy_key=? ORDER BY checked_at DESC LIMIT 1',
        (dep['strategy_key'],)
    ).fetchone()
    drift = drift_row[0] if drift_row else 'n/a'

    try:
        regime = get_current_regime(dep['asset'], conn)
    except:
        regime = 'UNKNOWN'

    golive = n >= 30 and pf is not None and pf >= 1.40 and drift == 'ok'
    print(f\"[{'GO-LIVE' if golive else 'warten'}] ID={dep['id']} {dep['strategy_key']}/{dep['asset']} n={n} pf={pf} drift={drift} regime={regime}\")
"

2. Zeige eine Tabelle mit allen dry_run Deployments:
   - deployment_id | strategy | asset | n_trades | live_pf | drift | regime | go_live_ready

3. Erkläre: Um ein Deployment zu promoten, sende im Telegram-Bot:
   /promote {deployment_id}
   Der Bot zeigt nochmals alle Metriken und fragt per Inline-Button um Bestätigung.
   Nur du (autorisierter Chat) kannst diesen Command ausführen.

4. Weise auf Bedingungen hin:
   - Mindestens 30 abgeschlossene Trades erforderlich
   - Live-PF >= 1.40 (Empfehlung, kein Hard-Gate im /promote Command)
   - Drift-Status = ok
   - HMM-Regime in STRATEGY_ALLOWED_REGIMES
