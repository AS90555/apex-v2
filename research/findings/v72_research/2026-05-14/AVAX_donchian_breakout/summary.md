# v7.2 Research Report — 2026-05-14

**Kombination:** `donchian_breakout` / `AVAX`
**study_hash:** `1d528507ce14ec1104d3349ecc465cac`
**objective_version:** `v72.0`
**Trials:** 50 | **Pass:** 0 | **Fail:** 50
**Laufzeit:** 131m 43s | **Ø Trial:** ~2m

## Bestes Trial

| Feld | Wert |
|------|------|
| params_hash | `5e834eef50744242e7006477c7e6836a` |
| composite | 0.810 |
| DSR | 1.000 |
| PBO | 0.543 |
| MaxDD | 1.156R |
| Stability | 0.248 |
| oos_folds_n | 18 |

## Top-10 Trials

| # | Composite | DSR | PBO | MaxDD | Stability | Gate-Fails |
|---|-----------|-----|-----|-------|-----------|------------|
| 1 | 0.810 | 1.000 | 0.543 | 1.156 | 0.248 | ❌ PBO+Stab |
| 2 | 0.795 | 1.000 | 0.680 | 2.841 | 0.467 | ❌ PBO+Stab |
| 3 | 0.646 | 0.233 | 0.485 | 1.166 | 0.399 | ❌ DSR+PBO+Stab |
| 4 | 0.632 | 0.000 | 0.149 | 2.268 | 0.619 | ❌ DSR |
| 5 | 0.602 | 0.004 | 0.233 | 2.268 | 0.462 | ❌ DSR+Stab |
| 6 | 0.591 | 0.000 | 0.275 | 2.252 | 0.423 | ❌ DSR+Stab |
| 7 | 0.584 | 0.000 | 0.372 | 2.257 | 0.439 | ❌ DSR+PBO+Stab |
| 8 | 0.575 | 0.000 | 0.600 | 2.263 | 0.534 | ❌ DSR+PBO |
| 9 | 0.575 | 0.000 | 0.583 | 2.180 | 0.512 | ❌ DSR+PBO |
| 10 | 0.557 | 0.937 | 0.000 | 1.138 | 0.275 | ❌ PBO+Stab |

## Häufigste Fail-Reasons

- **47/50** Stability < 0.50
- **46/50** DSR < 0.50
- **41/50** PBO > 0.30
- **5/50** MaxDD > 5.0R

## Near-Miss Analyse

- Trials mit nur **1 Gate-Fail**: **1**
- Bester Composite: **0.810**
- DSR-Maximum erreicht: **1.000**
