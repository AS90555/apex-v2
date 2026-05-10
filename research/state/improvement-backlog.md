# APEX V2 — Improvement Backlog
> Gepflegt von apex-lead. Ideen, Hinweise, aufgeschobene Items.
> Quellen: Claude Code Hinweise, User-Ideen, externe Reviews.
> Sortierung: OPEN oben, DONE unten.

---

## OPEN — Strategie-Erweiterungen
| ID | Idee | Quelle | Aufwand | Abhängigkeit |
|----|------|--------|---------|--------------|
| B-01 | ORB-Strategie: Backtest-Adapter für engine.py schreiben → Lab-Validierung | User | Mittel | strategies/orb.py existiert, SIGNAL_FNS-Interface fehlt |

## OPEN — R-Layer
| ID | Idee | Quelle | Aufwand | Abhängigkeit |
|----|------|--------|---------|--------------|
| B-08 | Macro-Regime via BTC.D + Total Market Cap als übergeordneter Filter — externe Datenquelle (CoinGecko) als neuer Intake-Job | R-Layer | Mittel | neuer Intake-Daemon |

## OPEN — Infrastruktur
| ID | Idee | Quelle | Aufwand | Abhängigkeit |
|----|------|--------|---------|--------------|
| B-06 | apex-status HMM-Alert verbessern: unterscheide zwischen 'Modell nie trainiert' vs 'Modell veraltet >7 Tage' | Claude Chat Empfehlung | Klein | HMM-Modelle vorhanden |

## OPEN — Governance
| ID | Idee | Quelle | Aufwand | Abhängigkeit |
|----|------|--------|---------|--------------|
| B-05 | HMM von Soft-Warning auf Hard-Block umstellen nach 30 Live-Trades mit HMM-Regime-Daten. Trigger: governance_log auf HMM_WARN-Rate analysieren, dann HMMRegimeCheck auf return False umstellen | Claude Code (P-02) | Klein | 30 Live-Trades mit governance_log.checks_json LIKE '%HMM_WARN%' |

## OPEN — Lab & Validierung
| ID | Idee | Quelle | Aufwand | Abhängigkeit |
|----|------|--------|---------|--------------|
| B-04 | DSR auf tägliche Returns umstellen statt Trade-Level für bessere Diskriminierung | Claude Code (V-03 Hinweis) | Mittel | V-03 DONE |
| B-07 | --max-targets N Flag für schnelle Smoke-Tests ohne vollen 65-Target-Run | Claude Code (Mess-Run Befund) | Klein | N_TRIALS_DAEMON implementiert |
| B-10 | DSR-Verteilung im Lab-Pool analysieren: AVG/MIN/MAX nach deployment_status — sicherstellen dass deployten Strategies DSR-top sind | weekly-report 2026-05-07 | Klein | — |

---

## DONE — Infrastruktur
| ID | Idee | Abgeschlossen | Ergebnis |
|----|------|---------------|---------|
| B-13 | Executor-Dedup: WS-Intake erzeugte Phantom-Signals (signal_key=None) → 34 failed ETH/squeeze in 24h | DONE 2026-05-09 | Dedup-Check in executor.py vor Execution: wenn (strategy, asset, mode, DATE) bereits 'executed', → rejected mit reason 'dedup_executor: already_executed_today' |
| B-09 | ETH/4h Gap-Warnung: fetched=0 Lücke diagnostizieren | DONE 2026-05-08 | Kein Bug — historische Binance-Datenlücke 07./08.04. (36h); Alert-Schwellwert false-positive; keine Aktion erforderlich |
| B-02 | Backup-Staleness-Alert: Telegram-Warnung wenn letzter Snapshot > 25h | DONE 2026-05-07 | Staleness-Check am Ende von run_backup.sh; curl an Telegram-API wenn AGE_MIN > 1500 |
| B-03 | governance_invariants.py erstellen + in GitHub Actions einbinden | DONE 2026-05-07 | tests/governance_invariants.py; 2 Checks (approved-Signals + live-Deployments); in test-gate.yml als dritter Step |

---

## Wie neue Items hinzukommen
- Claude Code schreibt Hinweis → apex-lead trägt ein
- User-Idee → direkt hier dokumentieren
- Externes Review → Findings → Backlog
