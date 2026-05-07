# APEX V2 — Operating Manual for Claude Code

## Mission
Quant-Trading-System härten, optimieren, erweitern — OHNE Live-Risiko zu erhöhen.

## Hard Rules (NIE verletzen)
1. Niemals `execution/` direkt anfassen ohne explizite User-Freigabe — Live-Geld
2. Niemals Bitget-API-Keys in Code, Logs, oder Tests
3. Niemals `lab_discoveries`-Einträge mit `status='approved'` oder `'live'` löschen
4. Niemals Migrations auf `data/apex_v2.db` ohne Backup nach `data/backups/`
5. Niemals `RISK_USDT`, `MAX_LEVERAGE`, `DRAWDOWN_KILL_PCT` in einer PR ändern
6. Live-Mode-Wechsel (`shadow → dry_run → live`) NUR durch User über Telegram

## Architecture Invariants
- Pipeline-Order: Intake → Features → Strategy → Governance → Execution → Monitor
- Einziger Order-Sender: `execution/executor.py` (kein Bypass)
- Feature-Cache: jede Berechnung über `features/registry.py`, niemals inline
- Backtest-Engine MUSS `cooldown_bars=8` respektieren — sonst sind Scores ungültig
- `GenericDeployedStrategy` ist das Live-Pendant zu `SIGNAL_FNS` — beide müssen
  bit-identisch funktionieren (parity_test.py existiert dafür)

## Code Standards
- Python 3.12+, Type Hints überall, `from __future__ import annotations`
- Neue Strategie → MUSS in `SIGNAL_FNS` UND parity_test bestehen
- DB-Schreibvorgänge: nur über `core/db.py` Connections (WAL-Mode garantiert)
- Logging: `core/utils.py` Logger, niemals `print()`

## Test Gates (vor jedem Merge)
1. `pytest tests/` — alle grün
2. `python tests/parity_test.py` — Backtest=Live für alle SIGNAL_FNS
3. `python tests/governance_invariants.py` — keine approved-ohne-Check
4. `python scripts/dry_run_smoke.py` — 24h Replay auf Test-DB

## Daily Health Check (von Claude Code automatisiert)
- Heartbeats aller Komponenten in `heartbeats`-Tabelle prüfen
- Letzte 24h Trades vs. Backtest-Erwartung in `/research/findings/drift/`
- Lab-Discovery-Rate (Funde/Tag) — Alarm wenn < 1

## Wichtige Dateipfade
- Live-DB: /root/apex-v2/data/apex_v2.db (NUR LESEN ohne Freigabe)
- Backups: /root/apex-v2/data/backups/
- Research-Briefs: /root/apex-v2/research/briefs/  (von Claude Chat)
- Research-Findings: /root/apex-v2/research/findings/  (von Claude Code)
- Logs: /root/apex-v2/logs/

## Agent-Architektur
- apex-lead: Orchestrator, Roadmap-Pfleger, kein Production-Code
- quant-researcher: Markt-/Strategie-Recherche, Hypothesen-Generierung
- backtest-validator: Statistische Validierung, Anti-Overfitting
- governance-auditor: DB-Integrität, Rule-Compliance-Checks
- executor-hardener: Execution-Layer-Sicherheit, Race-Condition-Analyse
- lab-tuner: Auto-Lab-Parameter-Optimierung
