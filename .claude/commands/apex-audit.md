Führe folgende Analyse als governance-auditor aus:

1. python3 tests/governance_invariants.py
2. python3 tests/parity_test.py
3. Lies die letzten 100 Einträge in governance_log:
   python3 -c "
from core.db import get_connection
conn = get_connection()
rows = conn.execute('''
    SELECT gl.ts, s.strategy, s.asset, gl.decision, gl.checks_json
    FROM governance_log gl JOIN signals s ON s.id = gl.signal_id
    ORDER BY gl.id DESC LIMIT 100
''').fetchall()
warn = [r for r in rows if 'HMM_WARN' in str(r[4])]
fail = [r for r in rows if r[3] == 'rejected']
print(f'HMM_WARN Events: {len(warn)}')
print(f'Geblockte Signale: {len(fail)}')
for r in fail[:5]: print(r)
"

Gib aus:
## Governance-Audit [DATUM]
- parity_test: PASS/FAIL
- governance_invariants: PASS/FAIL
- HMM_WARN Rate (letzte 100 Signale)
- Geblockte Signale mit Grund
- Empfehlung: HMM Hard-Block aktivieren? (ja wenn WARN-Rate > 30%)