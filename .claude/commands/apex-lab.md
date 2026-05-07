Führe folgende Aktion als lab-tuner aus:

1. Lies research/state/master-roadmap.md
2. Prüfe wann der letzte Lab-Run war:
   python3 -c "
from core.db import get_connection
conn = get_connection()
row = conn.execute('SELECT MAX(created_at) FROM research_runs').fetchone()
print('Letzter Lab-Run:', row[0])
count = conn.execute('SELECT COUNT(*) FROM lab_discoveries WHERE deployment_status=\"lab\"').fetchone()
print('Aktuelle Lab-Discoveries:', count[0])
"
3. Starte einen Lab-Run für alle aktiven Assets:
   python3 research/auto_lab_daemon.py --single-pass

Berichte: welche neuen Discoveries wurden gefunden,
welche bestanden BH-Filter, welche empfehlen sich für dry_run.