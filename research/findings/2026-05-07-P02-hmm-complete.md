# Finding: P-02 — HMM Regime-Detector (3 States) komplett

**Datum:** 2026-05-07
**Roadmap-ID:** P-02
**Status:** DONE

## Zusammenfassung

3-State Gaussian HMM ersetzt den binären EMA50-Slope-Regime-Filter.
Erkennt TREND / SIDEWAYS / HIGH_VOL und blockiert strategie-inkompatible
Signale in der Governance. Wöchentliches Re-Training via systemd-Timer.

## Training-Ergebnis SOL (lookback=180 Tage, 1h-Candles)

```
Konvergiert:        True  (covariance_type='full', bester aus 10 Seeds)
State-Labels:       {0: 'SIDEWAYS', 1: 'HIGH_VOL', 2: 'TREND'}
Means atr_ratio:    [-0.8130,  0.8599,  0.2963]  (skaliert)
                     SIDEWAYS  HIGH_VOL  TREND
```

**Transitions-Matrix:**
```
             → SIDEWAYS  → HIGH_VOL  → TREND
SIDEWAYS       0.947       0.053       0.000
HIGH_VOL       0.056       0.807       0.138
TREND          0.020       0.063       0.917
```

Alle Diagonalen > 0.85 → Regime-Persistenz erfüllt ✓
Aktuelles Regime SOL: **SIDEWAYS**

## Implementierte Dateien

| Datei | Änderung |
|-------|----------|
| `research/train_hmm.py` | Neu — load_features, train_hmm, label_states, save_model, get_current_regime |
| `config/settings.py` | STRATEGY_ALLOWED_REGIMES dict ergänzt |
| `governance/checks.py` | HMMRegimeCheck Klasse ergänzt (fail-open bei fehlendem Modell) |
| `scripts/run_governance.py` | HMMRegimeCheck nach DailyDrawdownCheck eingebunden |
| `scripts/run_hmm_retrain.py` | Neu — wöchentliches Re-Training aller aktiven Assets |
| `/etc/systemd/system/apex-hmm-retrain.service` | Neu — oneshot |
| `/etc/systemd/system/apex-hmm-retrain.timer` | Neu — Sun 05:00:00, Persistent=true |
| `data/hmm_models/SOL_hmm_py312.pkl` | Trainiertes Modell (Pickle, Python 3.12) |

## STRATEGY_ALLOWED_REGIMES

```python
{
    "donchian_breakout":   ["TREND", "HIGH_VOL"],
    "dual_donchian":       ["TREND", "HIGH_VOL"],
    "mean_reversion":      ["SIDEWAYS"],
    "squeeze":             ["SIDEWAYS", "TREND"],
    "bb_kc_squeeze":       ["SIDEWAYS", "TREND"],
    "ema_pullback":        ["TREND"],
    "inside_bar_breakout": ["TREND"],
    "supertrend":          ["TREND", "HIGH_VOL"],
    # vaa, kdt, weekend_momo, asian_fade, vwap_bounce → kein Filter (alle 3)
}
```

## Feature-Architektur (4 Dimensionen)

| Index | Feature | Berechnung |
|-------|---------|------------|
| 0 | log_return_1h | log(close / close_prev) |
| 1 | log_return_4h | log(close_4h / close_4h_prev), jede 4. Bar |
| 2 | atr_ratio | ATR(14) / close |
| 3 | volume_ratio | volume / volume.rolling(20).mean() |

Normalisierung: StandardScaler (fit auf Training, transform auf Inference).

## Technische Entscheidungen

- **n_init via Multi-Start**: hmmlearn 0.3.3 hat kein `n_init`-Argument →
  10 Seeds manuell, bestes Score (log-likelihood) gewinnt
- **Fail-open bei fehlendem Modell**: HMMRegimeCheck gibt True zurück wenn
  `data/hmm_models/{asset}_hmm_py312.pkl` nicht existiert — kein Blocking
  bei neuen Assets vor erstem Re-Training
- **label_states() immer verwenden**: State-Index ist run-abhängig, nie direkt nutzen
- **Pickle + Python-Version im Dateinamen**: `{asset}_hmm_py312.pkl` verhindert
  stille Kompatibilitätsfehler bei Python-Upgrades

## Verifikation

- `python3 -m py_compile` aller geänderten Dateien: OK
- `python3 tests/parity_test.py`: **12 PASS | 0 FAIL | 1 SKIP**
- `systemctl list-timers apex-hmm-retrain.timer`: nächste Ausführung So 2026-05-10 05:00
- `get_current_regime('SOL', conn)`: SIDEWAYS ✓

## Nächste Schritte (Backlog)

- Nach 4 Wochen Live-Daten: Analyse wie oft HMMRegimeCheck blockiert hat
  (governance_log WHERE checks_json LIKE '%hmm_regime%')
- Ggf. STRATEGY_ALLOWED_REGIMES anpassen wenn false positives auftreten
- B-04: DSR auf tägliche Returns umstellen (bessere Diskriminierung)
