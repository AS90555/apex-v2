# v7.2 Research Report — 2026-05-14

**Kombination:** `donchian_breakout` / `XRP`
**study_hash:** `3865037b3a58e684cfaa6f7c74b446ce`
**objective_version:** `v72.0`
**Trials:** 50 | **Pass:** 0 | **Fail:** 50
**Laufzeit:** 112m 13s | **Ø Trial:** ~2m

## Bestes Trial

| Feld | Wert |
|------|------|
| params_hash | `d2c8b4c4001e35866ef41289ec49f18e` |
| composite | 0.822 |
| DSR | 0.995 |
| PBO | 0.032 |
| MaxDD | 1.181R |
| Stability | 0.000 |
| oos_folds_n | 18 |

## Top-10 Trials

| # | Composite | DSR | PBO | MaxDD | Stability | Gate-Fails |
|---|-----------|-----|-----|-------|-----------|------------|
| 1 | 0.822 | 0.995 | 0.032 | 1.181 | 0.000 | ❌ Stab |
| 2 | 0.639 | 0.356 | 0.254 | 1.210 | 0.000 | ❌ DSR+Stab |
| 3 | 0.624 | 0.000 | 0.091 | 2.376 | 0.536 | ❌ DSR |
| 4 | 0.612 | 0.000 | 0.096 | 2.389 | 0.462 | ❌ DSR+Stab |
| 5 | 0.610 | 0.000 | 0.046 | 1.392 | 0.283 | ❌ DSR+Stab |
| 6 | 0.599 | 0.000 | 0.000 | 2.378 | 0.312 | ❌ DSR+PBO+Stab |
| 7 | 0.594 | 0.000 | 0.006 | 2.454 | 0.290 | ❌ DSR+Stab |
| 8 | 0.587 | 0.000 | 0.000 | 1.193 | 0.072 | ❌ DSR+PBO+Stab |
| 9 | 0.587 | 0.000 | 0.091 | 1.226 | 0.136 | ❌ DSR+Stab |
| 10 | 0.576 | 0.000 | 0.000 | 1.190 | 0.000 | ❌ DSR+PBO+Stab |

## Häufigste Fail-Reasons

- **49/50** DSR < 0.50
- **44/50** Stability < 0.50
- **36/50** PBO > 0.30
- **7/50** MaxDD > 5.0R

## Near-Miss Analyse

- Trials mit nur **1 Gate-Fail**: **2**
- Bester Composite: **0.822**
- DSR-Maximum erreicht: **0.995**
