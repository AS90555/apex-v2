Führe folgende Analyse als apex-lead + quant-researcher aus:

1. Lies research/state/improvement-backlog.md
2. Lies die letzten 3 Weekly-Reports in research/state/
3. Lies alle research/findings/ der letzten 14 Tage
4. Prüfe aktuelle Lab-Discovery-Rate:
   python3 -c "
from core.db import get_connection
conn = get_connection()
rows = conn.execute('''
    SELECT DATE(created_at) as day, COUNT(*) as n
    FROM lab_discoveries
    WHERE created_at > DATE('now','-14 days')
    GROUP BY day ORDER BY day DESC
''').fetchall()
for r in rows: print(r)
"

Gib aus:
## Verbesserungs-Analyse [DATUM]
- Discovery-Rate der letzten 14 Tage (Trend: steigend/fallend?)
- Top-3 Backlog-Items nach Impact×Aufwand
- Neue Verbesserungsideen aus Findings
- Konkrete Empfehlung: was als nächstes angehen?