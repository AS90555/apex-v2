# Brief: V-06 — Drift-Monitor auf Netto-PF-Basis

**Datum:** 2026-05-07
**Priorität:** P1
**Adressat:** backtest-validator

## Kontext
Der Drift-Monitor (S-04) vergleicht Live-PF gegen pf_oos aus lab_discoveries.
Diese Werte sind Brutto (ohne Kosten). Nach V-01 wissen wir: Netto-PF ist
25–48% niedriger. Ein Deployment mit pf_oos_brutto=2.37 hat netto ~1.78.
Der Monitor schlägt bei -30% Critical an — gegen Brutto bedeutet das
Live-PF < 1.66, gegen Netto wäre die Schwelle Live-PF < 1.25.
Das ist ein erheblicher Unterschied für die Auto-Pause-Logik.

## Aufgabe
1. core/db.py: neue Spalte pf_test_netto REAL in lab_discoveries
   (Migration: ALTER TABLE, idempotent)
2. Fülle pf_test_netto für alle 5 aktiven Deployments:
   Netto-Werte aus research/findings/2026-05-06-V01-discovery-reeval.md
   SOL=1.777, XRP=1.523, AVAX=1.390, ADA=1.481, LINK=1.443
3. scripts/run_drift_check.py: lese pf_oos aus pf_test_netto statt pf_test
   Fallback: wenn pf_test_netto NULL → pf_test * 0.75 als Schätzung
4. Einmalig run_drift_check.py ausführen — neuen Baseline-Output zeigen

## Akzeptanzkriterien
- [ ] Spalte pf_test_netto in lab_discoveries vorhanden
- [ ] Drift-Check läuft ohne Fehler mit Netto-Werten
- [ ] Drift-Prozentsätze in live_vs_backtest_drift basieren auf Netto-PF
- [ ] Fallback für NULL-Werte funktioniert

## Risiken
DB-Migration auf Live-DB → Backup-Pflicht vor ALTER TABLE.
Additive Änderung (neue Spalte), kein Datenverlust möglich.

## Abhängigkeiten
V-01 (DONE), core/db.py, scripts/run_drift_check.py
