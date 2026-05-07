# APEX V2 — Master Roadmap

> Gepflegt von: apex-lead | Letzte Aktualisierung: 2026-05-07 | Stand: P0+P1+P2+P3+Phase0 vollständig
> Improvement Backlog (aufgeschobene Ideen): [research/state/improvement-backlog.md](improvement-backlog.md)

---

## P0 — Live-Sicherheit

| ID | Titel | Quelle | Status | Owner | DoD |
|----|-------|--------|--------|-------|-----|
| S-01 | Telegram-Auth fail-CLOSED bei leerer CHAT_ID | external-review | DONE 2026-05-06 | executor-hardener | Test: leeres .env → Bot lehnt ALLE Commands ab |
| S-02 | generic_deployed.py entry_price/SL-TP Konsistenz | external-review | DONE 2026-05-06 | executor-hardener | parity_test: live entry == backtest entry bei selber Bar |
| S-03 | DAILY_DD_HALF_R implementieren oder removen | external-review | DONE 2026-05-06 | executor-hardener | DailyDrawdownCheck halbiert Position bei -1.5R, oder Setting weg |
| S-04 | Live-vs-Backtest-Diskrepanz-Auto-Pause | external-review | DONE 2026-05-06 | apex-lead | Live-PF ≤ 50% von OOS-PF nach 30 Trades → Modus auf shadow |
| S-05 | parity_test.py erstellen — Backtest/Live-Verifikation fehlt | executor-hardener (S-02 Befund) | DONE 2026-05-06 | executor-hardener | parity_test.py läuft grün für alle SIGNAL_FNS, entry_price/SL/TP Live == Backtest für gleiche Bar |

---

## P1 — Statistische Validität

| ID | Titel | Quelle | Status | Owner | DoD |
|----|-------|--------|--------|-------|-----|
| V-01 | Slippage+Fees+Funding ins Backtest | external-review | DONE 2026-05-06 | backtest-validator | Backtest-PF sinkt um realistische 15-30%, parity_test bleibt grün |
| V-02 | FDR-Control im Auto-Lab (Benjamini-Hochberg) | external-review | DONE 2026-05-06 | lab-tuner | BH q=0.05 auf Round-Level; py_compile OK, parity_test 12 PASS |
| V-03 | Deflated Sharpe Ratio statt nakter PF-Schwelle | claude-chat | DONE 2026-05-07 | lab-tuner | DSR implementiert als Ranking-Metrik in lab_discoveries.dsr. Kein Hard-Gate (MIN_DSR=0.95 inaktiv) — SR_benchmark bei N=70 trade-basiert numerisch zu hoch. BH (V-02) bleibt operativer Multiple-Testing-Filter. DSR für Portfolio-Ranking in P3. |
| V-04 | n_test ≥ 100 statt 40 als Minimum | external-review | DONE 2026-05-06 | lab-tuner | Lab-Filter aktualisiert, alte Discoveries re-validiert |
| V-05 | Ruin-Filter über alle WF-Fenster, nicht nur jüngstes | external-review | DONE 2026-05-06 | backtest-validator | ruin_filter: True in allen 3 WF_WINDOWS; parity_test 12 PASS |
| V-06 | Drift-Monitor auf Netto-PF-Basis umstellen | V-01 Befund | DONE 2026-05-07 | backtest-validator | pf_test_netto Spalte migriert; 5 Deployments befüllt; Fallback brutto×0.75 für NULL |

---

## P2 — Operational Resilience

| ID | Titel | Quelle | Status | Owner | DoD |
|----|-------|--------|--------|-------|-----|
| O-01 | Cron → systemd Units | claude-chat | DONE 2026-05-07 | apex-lead | apex-master.timer (5min), apex-drift-check.timer (06:00), apex-telegram.service (Restart=always); journalctl bestätigt 2× apex-master.service grün |
| O-02 | DB-Backup-Rotation (restic, hourly+daily+weekly) | claude-chat | DONE 2026-05-07 | apex-lead | restic 0.16.4; apex-backup.timer (stündlich); 2 Snapshots verifiziert; keep 24h/7d/4w — Staleness-Alert → B-02 im Backlog |
| O-03 | GitHub Actions Test-Gate auf PRs | claude-chat | DONE 2026-05-07 | apex-lead | .github/workflows/test-gate.yml: pytest + parity_test auf push/PR nach main |

---

## P3 — Performance

| ID | Titel | Quelle | Status | Owner | DoD |
|----|-------|--------|--------|-------|-----|
| P-01 | Optuna TPE statt Grid+MC im Lab | claude-chat | DONE 2026-05-07 | lab-tuner | Phase 1 (donchian_breakout): Pruning aktiv, 10/10 PRUNED. Phase 2: alle 13 SIGNAL_FNS mit OPTUNA_SPACES; py_compile OK; parity_test 12 PASS; Smoke-Test squeeze/mean_reversion/supertrend: Pruning je aktiv |
| P-02 | HMM-Regime-Detector (3 States) | claude-chat | DONE 2026-05-07 | quant-researcher | GaussianHMM(3) konvergiert für SOL; Diagonale [0.947, 0.807, 0.917] > 0.85; HMMRegimeCheck in Governance nach DailyDrawdownCheck; STRATEGY_ALLOWED_REGIMES in settings.py; wöchentliches Re-Training via apex-hmm-retrain.timer (So 05:00); parity_test 12 PASS |

---

## P4 — Code-Hygiene

| ID | Titel | Quelle | Status | Owner | DoD |
|----|-------|--------|--------|-------|-----|
| C-01 | Alten ORB-Code aus settings.py entfernen | external-review | DEFERRED | apex-lead | ORB-Konstanten aktiv von strategies/orb.py genutzt — Entfernung würde orb.py brechen. Entfällt bis orb.py archiviert wird. |
| C-02 | README: OOS-Zahlen klar von Live-Zahlen trennen | external-review | DONE 2026-05-07 | apex-lead | Tabelle: Spalte OOS-PF (Brutto) + Netto-PF (nach Kosten); Hinweis "Live seit 2026-05-06, < 30 Trades" |

---

## Phase 0 — Slash-Commands & Tooling

| ID | Titel | Quelle | Status | Owner | DoD |
|----|-------|--------|--------|-------|-----|
| SC-01 | /apex-status Slash-Command | Claude Chat | DONE 2026-05-07 | apex-lead | Dynamischer HMM-Query aus active_deployments; kein hardcodierter Asset-Block |
| SC-02 | /apex-weekly Slash-Command | Claude Chat | DONE 2026-05-07 | apex-lead | Wöchentlicher Report mit Trade-Stats, Drift, Lab-Rate, Roadmap-Delta |
| SC-03 | /apex-lab Slash-Command | Claude Chat | DONE 2026-05-07 | lab-tuner | Lab-Single-Pass auslösbar, Discovery-Rate sichtbar |
| SC-04 | /apex-audit Slash-Command | Claude Chat | DONE 2026-05-07 | governance-auditor | DB-Integrität + Governance-Invarianten on-demand prüfbar |

---

## Q-Layer — Quant-Erweiterungen

| ID | Titel | Quelle | Status | Owner | DoD |
|----|-------|--------|--------|-------|-----|
| Q-01 | apex-lab-daemon.service (continuous, nice -n 19) | Claude Chat | DONE 2026-05-07 | lab-tuner | systemd-Service aktiv; N_TRIALS_DAEMON=20; 30min Pause zwischen Runs |
| Q-02 | N_TRIALS_DAEMON/N_TRIALS_FULL Trennung | Claude Chat | DONE 2026-05-07 | lab-tuner | --single-pass nutzt 20 Trials; manuell 50; py_compile OK |
| Q-03 | run_auto_promotion.py — Lab→dry_run Gate | Claude Chat | DONE 2026-05-07 | apex-lead | 6 Gates; Crontab */30min; Telegram-Push; Heartbeat |
| Q-04 | run_auto_demotion.py — dry_run Archivierung + Go-Live-Alert | Claude Chat | DONE 2026-05-07 | apex-lead | Demotion PF<1.20@n≥30; Go-Live-Check PF≥1.40+Drift+Regime; stündlich |
| Q-05 | /promote Telegram-Command + apex_promote Slash-Command | Claude Chat | DONE 2026-05-07 | apex-lead | cmd_promote + promote_confirm_ Callback; py_compile OK |

---

## R-Layer — Resilience & Recovery

| ID | Titel | Quelle | Status | Owner | DoD |
|----|-------|--------|--------|-------|-----|
| R-01 | Multi-TF HMM Feature-Analyse + Re-Training alle 5 Assets | Claude Chat | DONE 2026-05-07 | quant-researcher | log_return_4h bereits als Feature [1] bestätigt; alle 5 Modelle konvergiert; AVAX-Regime-Wechsel TREND→SIDEWAYS erkannt; Findings R01 |
| R-02 | Regime-Sizing: SIDEWAYS/HIGH_VOL → 0.5× Positionsgröße | Claude Chat | DONE 2026-05-07 | executor-hardener | HMMRegimeCheck REGIME_HALF-Flag; Executor analog HALF_SIZE; parity_test 12 PASS |
| R-03 | run_regime_monitor.py — 4h Regime-Wechsel-Detektion | Claude Chat | DONE 2026-05-07 | apex-lead | systemd-Timer alle 4h; Telegram-Push bei Wechsel; 5 Assets initialisiert; Heartbeat |
| R-04 | Macro-Regime via BTC.D + Total Market Cap (CoinGecko) | R-Layer | DEFERRED | apex-lead | → B-08 im Backlog; Abhängigkeit: neuer Intake-Daemon für externe Datenquelle |

---

## T-Layer — Tooling & Observability

| ID | Titel | Quelle | Status | Owner | DoD |
|----|-------|--------|--------|-------|-----|
| T-02 | Trade-Alert um Regime/OOS-PF/Size-Modifier/DSR erweitern | Claude Chat | DONE 2026-05-07 | apex-lead | push_new_trades: 📍 Regime (live HMM), 📊 OOS-PF (netto), ⚖️ Size-Modifier (HALF/FULL via governance_log), 🎯 Edge-Confidence (DSR); py_compile OK; Bot neugestartet |
| T-03 | Inline-Buttons in /status, Trade-Alert und Daily Digest | Claude Chat | DONE 2026-05-07 | apex-lead | /status: [🔬 Lab starten][📊 Audit][📈 Regime]; Trade-Alert: [⏸ Pause {asset}][📊 Details]; Daily Digest: [📋 Portfolio][⚙️ Status]; Callbacks: lab_run_now, audit_now, regime_now, portfolio_overview, trade_pause_confirm_{id}; py_compile OK; Bot neugestartet |

---

## Legende
- **OPEN**: Noch nicht begonnen
- **IN-PROGRESS**: Aktiv in Arbeit
- **REVIEW**: Fertig, wartet auf Prüfung
- **DONE**: Abgeschlossen, DoD erfüllt
- **BLOCKED**: Blockiert (Grund im Kommentar)
- **DEFERRED**: Verschoben (Begründung im Kommentar)
