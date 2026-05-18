# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Mission
Quant-Trading-System härten, optimieren, erweitern — OHNE Live-Risiko zu erhöhen.

## Hard Rules (NIE verletzen)
1. Niemals `execution/` direkt anfassen ohne explizite User-Freigabe — Live-Geld
2. Niemals Bitget-API-Keys in Code, Logs, oder Tests
3. Niemals `lab_discoveries`-Einträge mit `status='approved'` oder `'live'` löschen
4. Niemals Migrationen auf die Live-DB ohne Backup nach `data/backups/`
5. Niemals `RISK_USDT`, `MAX_LEVERAGE`, `DRAWDOWN_KILL_PCT` in einer PR ändern
6. Live-Mode-Wechsel (`shadow → dry_run → live`) NUR durch User über Telegram
7. Gates (`DSR_MIN_*`, `PBO_MAX`, `STABILITY_MIN`, `MAX_DD_GATE`) sind immutable — kein Lab-Code darf sie importieren oder verändern

## Befehle

```bash
# Tests
pytest tests/                                        # alle Tests
pytest tests/test_lab_families.py -v                 # einzelner Test-File
pytest tests/ -k "evolution"                         # Tests per Keyword

# Pflicht-Gates vor jedem Merge
python3 tests/parity_test.py                         # Backtest=Live für alle SIGNAL_FNS
python3 tests/governance_invariants.py               # keine approved-ohne-Check

# Research-Lab (V72_RESEARCH_ENABLED=true in config/.env erforderlich)
python3 scripts/run_v72_research.py --strategy donchian_breakout --asset BTC --n-trials 50
python3 scripts/lab_controller.py --mode run-cycle --db-path data/lab_state.db
python3 scripts/lab_controller.py --mode status
python3 scripts/lab_controller.py --mode health-check

# Regime-Detector (täglich per Cron, läuft auch als: scripts/lab_regime_daily_check.py)
python3 scripts/lab_regime_daily_check.py --assets BTC ETH SOL XRP LINK

# Reports
python3 scripts/lab_report_generator.py --mode weekly --send
python3 scripts/lab_report_generator.py --mode evolution --send

# Pre-Scan einzeln
python3 scripts/lab_pre_scan.py --strategy dual_donchian --asset BTC
```

## Architektur: Live-Trading-Pipeline

```
Cron (*/5 Min) → scripts/master_run.py  (sequenziell, kein Subprocess)
    │
    ├─ intake/intake_ws.py          WebSocket Candle-Stream (24/7)
    ├─ features/registry.py         Feature-Cache (EMA, ATR, BB, Regime) — nie inline
    ├─ strategies/generic_deployed.py  Live-Signale via SIGNAL_FNS aus backtest/engine.py
    ├─ governance/gate.py           DD-Kill, Regime-Check, Session-Limit, Signal-Expiry
    ├─ execution/executor.py        Einziger Order-Sender — KEIN Bypass
    └─ monitor/position_monitor.py  Break-Even SL, Heartbeats
```

**Kritische Invarianten:**
- `GenericDeployedStrategy` und `SIGNAL_FNS` (in `backtest/engine.py`) müssen bit-identisch sein → `tests/parity_test.py`
- `cooldown_bars=8` in **jedem** Backtest-Aufruf — sonst sind Scores ungültig
- Feature-Berechnungen: ausschließlich über `features/registry.py`, nie inline
- DB-Connections: nur über `core/db.py` (WAL-Mode) bzw. `core/lab_state_db.py`

## Architektur: Research-Lab (autonomes System)

Das Research-Lab läuft unabhängig von der Live-Pipeline und arbeitet mit zwei separaten Datenbanken:

- **`data/lab_state.db`** — Governance: Cycles, Queue, Negative Controls, Borderline-Kandidaten, Familien, Variants, Lineage, Fitness, Regime-History. Einziger Schreiber pro Modul — **kein direktes `sqlite3.connect()` außerhalb `core/lab_state_db.py`**.
- **`data/research_staging.db`** — Optuna-Trial-Outputs (`lab_discoveries`-Tabelle). Schreiber: `research/v72_staging_writer.py`.

### Lab-Cycle-Ablauf

```
lab_controller.py --mode run-cycle
    │
    ├─ lab_families.sync_to_db()          Familien-Ontologie aus YAML → DB
    ├─ lab_evolution_engine.propose_variants()  Neue Variants (70% Random-Seed, 30% Mutation)
    ├─ mode_build_queue()                 Variants → lab_queue (mit variant_id-Link)
    │
    └─ pro Queue-Entry:
        ├─ lab_pre_scan.py (10 Trials)    Signal vorhanden?
        │   ├─ signal_absent/freq_incompatible → classify_and_archive() → Negative Control
        │   ├─ inconclusive → paused_inconclusive + Telegram-Eskalation
        │   └─ signal_present → voller Run
        │
        ├─ run_v72_research.py (50 Trials)  Optuna-Optimierung → research_staging.db
        └─ _post_run_governance()
            ├─ classify_and_archive()     NO-GO → Negative Control
            ├─ classify_and_register()    Borderline → User-Review
            ├─ compute_fitness() + variant_evaluated-Event
            └─ promote_if_eligible() → run_v7_reeval (E8)  wenn fitness ≥ 0.60
```

### Versionierungs-Hashes (deterministisch)

`study_hash` = SHA256(`LAB_SEARCH_CFG.hash` + `RANGES_V72_VERSION` + `ranges_v72_hash` + `OBJECTIVE_V72_VERSION` + `strategy` + `asset`)[:32]

Ändert sich bei Änderungen an Sampler-Config, Search-Space-Ranges oder Objective-Funktion — verhindert Hash-Kollisionen zwischen Runs. **Beim Bumpen muss auch `RANGES_V72_VERSION`** (in `research/v72_search_space.py`) **erhöht werden.**

### Governance-Schreibrechte pro Modul

| Modul | Darf schreiben |
|---|---|
| `core/lab_negative_controls.py` | `negative_controls` |
| `core/lab_borderline_registry.py` | `borderline_candidates` |
| `core/lab_evolution_engine.py` | `strategy_variants`, `evolution_events` |
| `core/lab_lineage_tracker.py` | `variant_lineage` |
| `core/lab_fitness_metric.py` | `fitness_records` |
| `core/lab_regime_detector.py` | `regime_history`, `evolution_events` |
| `core/lab_promotion_gate.py` | `evolution_events` |
| `scripts/lab_controller.py` | `lab_cycles`, `lab_queue` |

### Negative-Control-NO-GO-Kriterien

- `signal_absent`: `dsr_rate == 0` nach ≥ 8 Trials
- `frequency_incompatible`: `n_oos_median < 30` nach ≥ 10 Trials
- `structural`: 4-Gate-Pass-Count == 0 nach ≥ 50 Trials

**Regime-Wahrheitsquelle:** `core/lab_state_db.py::get_current_regime(conn, asset, fallback="MIXED")` ist die **einzige** API für das aktuelle Regime — liest aus `regime_history`. Kein direktes Lesen aus `asset_profiles`.

### Evolution-Layer (E1–E8, implementiert)

- **Familien-Ontologie**: `config/lab_strategy_families.yaml` — 7 Familien (donchian, squeeze, mean_reversion, momentum, pattern, volume_action, takeprofit_only). Nur via YAML ändern, nie zur Laufzeit.
- **Variant-Statusmaschine**: `proposed → queued → pre_scanning → running → evaluated` (terminal). Übergänge nur via `update_variant_status()` — ungültige Übergänge werfen `ValueError`.
- **Lineage-Tiefe-Limit**: 5 Ebenen (`MAX_LINEAGE_DEPTH = 5` in `core/lab_lineage_tracker.py`). Bei Überschreitung: Random-Seed-Reset.
- **Fitness-Version**: `fitness_v1.0` — bei Algorithmen-Änderung neue Version, kein stilles Überschreiben.
- **Mutation-Strategien**: Gaussian (σ=0.10 der Range), Crossover (50/50 zweier Top-Trials), Random-Seed (30%-Floor Exploration).

## Promotions-Gates (v7.2, immutable)

```python
DSR_MIN_DRY_RUN = 0.50   # Mindest-DSR für dry_run-Deployment
PBO_MAX         = 0.30   # Max. Probability of Backtest Overfitting
STABILITY_MIN   = 0.50   # Min. Stability-Score über OOS-Folds
MAX_DD_GATE     = 5.0    # Max. kumulativer Drawdown im schlechtesten OOS-Fold (R)
OOS_FOLDS_MIN_V7 = 3     # Mindest-Anzahl OOS-Folds
# n_oos = 100 in backtest/v7_eval.py — NICHT ändern
```

`test_v72_gates_immutable.py` prüft dass diese Werte nicht verändert wurden.

## Wichtige Dateipfade

| Pfad | Beschreibung |
|---|---|
| `config/.env` | Secrets + `V72_RESEARCH_ENABLED=true` (für Lab-Runs) |
| `config/settings.py` | Alle Trading-Konstanten, Gates, `STRATEGY_MODES` (shadow/dry_run/live) |
| `config/lab_strategy_matrix.yaml` | Asset-Universum + kompatible Strategien pro Regime |
| `config/lab_strategy_families.yaml` | 7 Evolution-Familien (v1.0) |
| `data/lab_state.db` | Governance-DB — nur via `core/lab_state_db.py` |
| `data/research_staging.db` | Trial-Outputs — nur via `research/v72_staging_writer.py` |
| `data/backups/` | Tägliche Backups (vor jeder Live-DB-Migration anlegen) |
| `research/findings/` | Backtest-Reports, Drift-Analyse |
| `docs/OPERATIONS_RUNBOOK.md` | Betriebshandbuch: Cron-Jobs, KPIs, Störfall-Recovery |

## Code-Standards

- Python 3.12+, `from __future__ import annotations`, Type Hints überall
- Logging: `core/utils.py` → `log()` — niemals `print()`
- Neue Strategie: MUSS in `SIGNAL_FNS` (`backtest/engine.py`) UND `tests/parity_test.py` bestehen
- Neue Lab-Module dürfen `config.settings` Gate-Konstanten weder importieren noch referenzieren — `tests/test_evolution_no_gate_writes.py` prüft das automatisch
- Chaos-Smoke-Tests (`tests/test_chaos_smoke.py` C-1/C-2/C-3) sind Teil der Pflicht-Suite — müssen grün sein nach Änderungen an `execution/` oder `core/telegram_dispatcher.py`

## Agent-Rollen

| Agent | Kann | Kann nicht |
|---|---|---|
| `apex-lead` | Roadmap, Briefs, Berichte | Production-Code schreiben |
| `quant-researcher` | Strategie-Recherche, Hypothesen | Code, DB-Writes |
| `backtest-validator` | Statistische Validierung, GO/NO-GO | Deployments |
| `governance-auditor` | DB-Integrität prüfen | Code ändern |
| `executor-hardener` | Execution-Layer analysieren | ohne Freigabe implementieren |
| `lab-tuner` | Auto-Lab-Parameter optimieren | Live-Code deployen |
