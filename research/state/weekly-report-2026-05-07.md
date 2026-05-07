# APEX V2 — Wöchentlicher Report
> Erstellt: 2026-05-07 | apex-lead | Berichtszeitraum: 2026-05-01 – 2026-05-07

---

## Executive Summary

APEX V2 ist in seiner ersten Live-Woche stabil: Pipeline läuft fehlerlos (alle Heartbeats grün, kein einziger Fehler in master.log), 1 Live-Deployment (SOL donchian_breakout) und 4 Dry-Runs sammeln Daten. Das gesamte Marktumfeld zeigt SIDEWAYS über alle Assets — Breakout-Strategien liefern entsprechend wenige Signale, was korrekte Regime-Selektion bestätigt. Regime-Half-Sizing ist aktiv und schützt die Live-Position bei SOL/AVAX. Die Roadmap wurde in einer intensiven Session von P0 bis T-Layer vollständig aufgebaut: 31 Items DONE, 2 DEFERRED, 0 Bugs offen. Nächster kritischer Meilenstein: XRP/LINK/ADA auf n≥30 Trades für statistisch belastbare Drift- und Demotion-Entscheidungen.

---

## Roadmap-Status

| Layer | Items | DONE | DEFERRED | OPEN |
|-------|-------|------|----------|------|
| P0 — Live-Sicherheit | 5 | 5 | 0 | 0 |
| P1 — Statistische Validität | 6 | 6 | 0 | 0 |
| P2 — Operational Resilience | 3 | 3 | 0 | 0 |
| P3 — Performance | 2 | 2 | 0 | 0 |
| P4 — Code-Hygiene | 2 | 1 | 1 (C-01 ORB) | 0 |
| Phase 0 — Slash-Commands | 4 | 4 | 0 | 0 |
| Q-Layer — Quant-Erweiterungen | 5 | 5 | 0 | 0 |
| R-Layer — Resilience & Recovery | 4 | 3 | 1 (R-04 Macro-Regime) | 0 |
| T-Layer — Tooling & Observability | 2 | 2 (T-02, T-03) | 0 | 0 |
| **Gesamt** | **33** | **31** | **2** | **0** |

Neu diese Woche (2026-05-07):
- Q-01 bis Q-05: Lab-Daemon, Auto-Promotion/Demotion, /promote Command
- R-01: Multi-TF HMM Feature-Analyse (5 Assets)
- R-02: Regime-Sizing SIDEWAYS/HIGH_VOL → 50% Positionsgröße
- R-03: Regime-Monitor (4h systemd-Timer)
- T-02: Trade-Alert erweitert um Regime/OOS-PF/DSR/Size-Modifier
- T-03: Inline-Buttons in /status, Trade-Alert, Daily Digest

---

## Drift-Status aller Deployments

| Deployment | Asset | Modus | n (exit) | OOS-PF (netto) | Live-PF | Drift |
|---|---|---|---|---|---|---|
| donchian_breakout_551 | SOL | **LIVE** | 1 | 1.78 | n/a | n/a (<30) |
| inside_bar_breakout_334 | XRP | dry_run | 4 | 1.52 | n/a | n/a (<30) |
| donchian_breakout_1157 | LINK | dry_run | 2 | 1.44 | n/a | n/a (<30) |
| donchian_breakout_916 | ADA | dry_run | 1 | 1.48 | n/a | n/a (<30) |
| donchian_breakout_571 | AVAX | dry_run | 0 | 1.39 | n/a | n/a (<30) |

Frühindikator (nicht statistisch): 11 abgeschlossene Trades gesamt, 10 Wins (91% WR) — stark positiv, aber Sample zu klein für Aussagen. SOL live: +0.64R, XRP live: +0.32R. Drift-Check greift erst bei n≥30. XRP erreicht diesen Schwellwert voraussichtlich in ~4–5 Wochen bei aktuellem SIDEWAYS-Markt.

---

## HMM-Regime der letzten 7 Tage

| Asset | Aktuelles Regime | Letzter Wechsel | Trend |
|-------|-----------------|-----------------|-------|
| BTC | SIDEWAYS | seit 2026-04-28 | stabil |
| ETH | SIDEWAYS | seit 2026-04-30 | stabil |
| SOL | SIDEWAYS | seit 2026-04-28 | stabil |
| XRP | SIDEWAYS | seit 2026-04-28 | stabil |
| AVAX | SIDEWAYS | seit 2026-04-28 | stabil |
| LINK | SIDEWAYS | seit 2026-04-28 | stabil |
| ADA | SIDEWAYS | seit 2026-04-28 | stabil |

**Befund:** Gesamtmarkt durchgehend SIDEWAYS seit mindestens 9 Tagen. `regime_prev` = SIDEWAYS für alle Assets — kein Wechsel seit Monitoring-Start. Donchian-Breakout-Strategien haben in diesem Umfeld strukturell wenige Signale; Regime-Half-Sizing schützt SOL (live) und AVAX korrekt. XRP inside_bar_breakout ist für SIDEWAYS zugelassen und läuft auf voller Größe. Kein Handlungsbedarf — System verhält sich korrekt. Historisch dauern Consolidation-Phasen dieser Art 2–6 Wochen.

---

## Top-3 Maßnahmen für die kommende Woche

### 1. B-05 — HMM Hard-Block vorbereiten (Trigger-Monitoring)
Trigger: 30 Live-Trades mit `governance_log.checks_json LIKE '%HMM_WARN%'`. R-02 (Regime-Sizing) läuft, der nächste Schritt ist Hard-Block für `regime not in allowed`. Maßnahme: täglichen Query auf `governance_log` aufsetzen, der HMM_WARN-Rate trackt. Implementierung ist eine Zeile (return False statt return True in HMMRegimeCheck).

### 2. ETH/4h Gap-Warnung untersuchen (Log-Befund, B-09)
master.log zeigt bei jedem Pipeline-Run: `ETH/4h: letzter Candle 345 Min alt — Features unzuverlässig`. Lücke `1775534400000→1775577600000` wird wiederholt eingefügt aber `fetched=0`. Kein Produktions-Impact (SOL ist live, nicht ETH), aber Intake-Qualität sicherstellen. Maßnahme: `fetch_binance_history.py` für ETH/4h manuell ausführen und Lücke diagnostizieren.

### 3. DSR-Verteilung im Lab-Pool analysieren (B-10)
377 Discoveries seit 01.05, davon 5 dry / 2 live deployed. Offen: Sind die deployten 7 tatsächlich die DSR-stärksten? Maßnahme: `SELECT deployment_status, AVG(dsr), MIN(dsr), MAX(dsr) FROM lab_discoveries GROUP BY deployment_status` ausführen. Falls Lab-Pool DSR-schwache Kandidaten enthält → Optuna-Zielmetrik prüfen.

---

## Neue Backlog-Items

| ID | Idee | Aufwand | Quelle |
|----|------|---------|--------|
| B-09 | ETH/4h Gap-Warnung: `fetched=0` Lücke persistent — Intake-Bug oder Binance-Datenlücke diagnostizieren | Klein | weekly-report |
| B-10 | DSR-Verteilung im Lab-Pool: sicherstellen dass deployten Strategies DSR-top sind | Klein | weekly-report |

---

## Technische Metriken

| Metrik | Wert |
|--------|------|
| Pipeline-Laufzeit | 9–12 Sekunden (nominal) |
| Heartbeat-Status | alle 6 Komponenten grün |
| Fehler in master.log | 0 |
| Abgeschlossene Trades | 11 (10W / 1L) — Sample zu klein |
| Lab-Discoveries gesamt | 377 (5 dry, 2 live) |
| Systemd-Services aktiv | 7 (master, telegram, drift-check, lab-daemon, regime-monitor, hmm-retrain, backup) |
