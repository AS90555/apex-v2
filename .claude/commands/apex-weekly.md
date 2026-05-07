Führe den wöchentlichen Report als apex-lead aus:

1. Lies alle Findings der letzten 7 Tage in research/findings/
2. Lies research/state/master-roadmap.md
3. Führe python3 scripts/run_drift_check.py aus
4. Lies logs/master.log letzte 200 Zeilen auf Fehler

Erstelle research/state/weekly-report-$(date +%Y-%m-%d).md mit:
## Executive Summary (5 Zeilen)
## Roadmap-Status (Tabelle)
## Drift-Status aller Deployments
## HMM-Regime der letzten 7 Tage (Trend erkennbar?)
## Top-3 Maßnahmen für die kommende Woche
## Neue Backlog-Items falls gefunden

Zeige den Report nach dem Schreiben.