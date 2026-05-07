# Finding: P-01 — Optuna TPE komplett (alle 13 SIGNAL_FNS)

**Datum:** 2026-05-07  
**Roadmap-ID:** P-01  
**Status:** DONE  

## Zusammenfassung

Optuna TPE (Tree-structured Parzen Estimator) ersetzt Grid+MC im Auto-Lab für alle 13 SIGNAL_FNS.  
Phase 1 (donchian_breakout) wurde als Pilot implementiert, Phase 2 auf alle Strategien ausgeweitet.

## Technische Umsetzung

### Architektur
- `OPTUNA_SPACES`: dict `strategy → {param: (low, high, is_int)}`
- `_optuna_objective(trial, strategy, asset, now_ms, start_ms, conn)`: führt 3-Fenster-WF durch, pruned nach Fenster 1 wenn PF < MIN_PF_TEST × 0.85
- `_run_optuna_target(strategy, asset, now_ms, start_ms, conn)`: erstellt Study, optimiert n_trials=50, gibt beste passed=True-Trials zurück
- `_run_one_target()`: leitet zu Optuna-Pfad wenn `strategy in OPTUNA_SPACES` (alle 13)
- BH-Filter läuft über Optuna-Candidates (passed=True), nicht über alle Trials

### Pruning-Logik
```python
# Nach Fenster 1: prune wenn PF deutlich unter Schwelle
if pf1 < MIN_PF_TEST * 0.85:  # 1.80 × 0.85 = 1.53
    trial.report(0.0, step=0)
    raise optuna.exceptions.TrialPruned()
```
`MedianPruner(n_startup_trials=5)` — nach 5 Warmup-Trials aktiv.

## Parameter-Spaces (alle 13 Strategien)

| Strategie | Parameter | Low | High | Typ |
|-----------|-----------|-----|------|-----|
| vaa | VOL_MULT | 1.5 | 4.0 | float |
| vaa | BODY_MULT | 0.3 | 0.8 | float |
| vaa | ATR_EXPAND | 0.8 | 2.0 | float |
| vaa | TP_R | 1.5 | 5.0 | float |
| vaa | SL_ATR_MULT | 0.5 | 2.0 | float |
| kdt | SL_ATR_MULT | 0.5 | 2.0 | float |
| kdt | TP_R | 1.5 | 5.0 | float |
| weekend_momo | MOMENTUM_THRESHOLD | 0.01 | 0.06 | float |
| weekend_momo | ATR_SL_MULT | 0.5 | 2.5 | float |
| weekend_momo | ATR_TP_MULT | 1.5 | 5.0 | float |
| asian_fade | PUMP_THRESHOLD | 0.008 | 0.03 | float |
| asian_fade | RSI_OB | 60 | 80 | int |
| asian_fade | RSI_OS | 20 | 40 | int |
| asian_fade | SL_ATR_MULT | 0.5 | 2.0 | float |
| asian_fade | TP_MULT | 1.0 | 3.0 | float |
| squeeze | SQUEEZE_PERIOD | 10 | 30 | int |
| squeeze | EMA_PERIOD | 10 | 40 | int |
| squeeze | SL_ATR_MULT | 0.5 | 2.0 | float |
| squeeze | TP_R | 1.5 | 5.0 | float |
| mean_reversion | BB_PERIOD | 10 | 30 | int |
| mean_reversion | BB_MULT | 1.5 | 3.0 | float |
| mean_reversion | RSI_PERIOD | 7 | 21 | int |
| mean_reversion | RSI_OS | 25 | 45 | float |
| mean_reversion | SL_ATR_MULT | 0.5 | 2.0 | float |
| mean_reversion | TP_R | 1.5 | 4.0 | float |
| vwap_bounce | VWAP_PERIOD | 12 | 48 | int |
| vwap_bounce | VWAP_BAND | 0.1 | 0.5 | float |
| vwap_bounce | EMA_PERIOD | 20 | 80 | int |
| vwap_bounce | RSI_MIN | 40 | 60 | float |
| vwap_bounce | SL_ATR_MULT | 0.5 | 2.0 | float |
| vwap_bounce | TP_R | 1.5 | 4.0 | float |
| ema_pullback | EMA_SLOW | 100 | 200 | int |
| ema_pullback | EMA_FAST | 20 | 75 | int |
| ema_pullback | BODY_FACTOR | 0.1 | 0.6 | float |
| ema_pullback | SL_ATR_MULT | 0.5 | 2.0 | float |
| ema_pullback | TP_R | 1.5 | 5.0 | float |
| donchian_breakout | DC_PERIOD | 10 | 50 | int |
| donchian_breakout | VOL_FACTOR | 1.2 | 3.0 | float |
| donchian_breakout | ATR_MIN_MULT | 0.8 | 2.0 | float |
| donchian_breakout | SL_ATR_MULT | 0.5 | 1.5 | float |
| donchian_breakout | TP_R | 1.2 | 3.0 | float |
| inside_bar_breakout | EMA_PERIOD | 20 | 100 | int |
| inside_bar_breakout | MOTHER_ATR_MIN | 0.3 | 1.5 | float |
| inside_bar_breakout | SL_ATR_MULT | 0.5 | 2.0 | float |
| inside_bar_breakout | TP_R | 1.5 | 4.0 | float |
| dual_donchian | ENTRY_PERIOD | 15 | 60 | int |
| dual_donchian | EXIT_PERIOD | 5 | 20 | int |
| dual_donchian | VOL_FACTOR | 1.2 | 3.0 | float |
| dual_donchian | ATR_MIN_MULT | 0.8 | 2.0 | float |
| dual_donchian | SL_ATR_MULT | 0.5 | 1.5 | float |
| dual_donchian | TP_R | 1.2 | 3.0 | float |
| bb_kc_squeeze | BB_PERIOD | 10 | 30 | int |
| bb_kc_squeeze | BB_MULT | 1.5 | 3.0 | float |
| bb_kc_squeeze | KC_MULT | 1.0 | 2.5 | float |
| bb_kc_squeeze | SL_ATR_MULT | 0.5 | 2.0 | float |
| bb_kc_squeeze | TP_R | 1.5 | 5.0 | float |
| supertrend | ST1_PERIOD | 7 | 14 | int |
| supertrend | ST1_MULT | 0.5 | 2.0 | float |
| supertrend | ST2_PERIOD | 10 | 20 | int |
| supertrend | ST2_MULT | 1.5 | 3.5 | float |
| supertrend | ST3_PERIOD | 12 | 25 | int |
| supertrend | ST3_MULT | 2.5 | 5.0 | float |
| supertrend | SL_ATR_MULT | 0.5 | 2.0 | float |
| supertrend | TP_R | 1.5 | 5.0 | float |

## Smoke-Test-Ergebnisse (n_trials=5, SOL, 2026-05-07)

| Strategie | Pruned | Complete | Best Fitness |
|-----------|--------|----------|--------------|
| squeeze | 5 | 0 | 0.0000 |
| mean_reversion | 5 | 0 | 0.0000 |
| supertrend | 4 | 1 | 0.0000 |

Alle Strategien zeigen aktives Pruning. Fitness=0.0 bedeutet, dass kein Trial MIN_PF_TEST=1.80 (mit apply_costs=True) überschritten hat — erwartet bei n=5 mit strikten Filtern.

## Verifikation

- `python3 -m py_compile research/auto_lab_daemon.py` → OK
- `python3 tests/parity_test.py` → **12 PASS | 0 FAIL | 1 SKIP**
- Phase 1 Smoke-Test (donchian_breakout, n=10): 10/10 PRUNED ✓
- Phase 2 Smoke-Test (3 Strategien, n=5 je): Pruning aktiv ✓

## Nächste Schritte (P3-Backlog)

- **Phase 3** (optional, B-Backlog): SQLite-Persistenz für Warm-Start zwischen Runs  
  `storage = "sqlite:///data/optuna_studies.db"`
- **P-02**: HMM-Regime-Detector (3 States)
