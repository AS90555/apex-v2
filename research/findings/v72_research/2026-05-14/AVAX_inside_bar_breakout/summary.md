# v7.2 Research Report — 2026-05-14

**Kombination:** `inside_bar_breakout` / `AVAX`
**study_hash:** `b1ff0d6ba8ebd576f6ce66e92e61e0db`
**objective_version:** `v72.0`
**Trials:** 50 | **Pass:** 0 | **Fail:** 50
**Laufzeit:** 63m 38s | **Ø Trial:** ~1m

## Bestes Trial

| Feld | Wert |
|------|------|
| params_hash | `b738eff9cc3f2139074962ec73f0a09f` |
| composite | 0.126 |
| DSR | 0.000 |
| PBO | 0.661 |
| MaxDD | 7.137R |
| Stability | 0.000 |
| oos_folds_n | 18 |

## Top-10 Trials

| # | Composite | DSR | PBO | MaxDD | Stability | Gate-Fails |
|---|-----------|-----|-----|-------|-----------|------------|
| 1 | 0.126 | 0.000 | 0.661 | 7.137 | 0.000 | ❌ DSR+PBO+Stab+MaxDD |
| 2 | 0.083 | 0.000 | 0.611 | 7.289 | 0.349 | ❌ DSR+PBO+Stab+MaxDD |
| 3 | 0.082 | 0.000 | 0.842 | 7.244 | 0.000 | ❌ DSR+PBO+Stab+MaxDD |
| 4 | 0.043 | 0.000 | 0.645 | 8.746 | 0.455 | ❌ DSR+PBO+Stab+MaxDD |
| 5 | 0.027 | 0.000 | 0.372 | 11.588 | 0.736 | ❌ DSR+PBO+MaxDD |
| 6 | 0.027 | 0.000 | 0.779 | 7.589 | 0.483 | ❌ DSR+PBO+Stab+MaxDD |
| 7 | 0.020 | 0.000 | 0.351 | 20.506 | 0.583 | ❌ DSR+PBO+MaxDD |
| 8 | 0.020 | 0.000 | 0.384 | 11.763 | 0.647 | ❌ DSR+PBO+MaxDD |
| 9 | 0.019 | 0.000 | 0.820 | 8.559 | 0.711 | ❌ DSR+PBO+MaxDD |
| 10 | 0.013 | 0.000 | 0.652 | 7.400 | 0.399 | ❌ DSR+PBO+Stab+MaxDD |

## Häufigste Fail-Reasons

- **50/50** DSR < 0.50
- **50/50** MaxDD > 5.0R
- **48/50** PBO > 0.30
- **17/50** Stability < 0.50

## Near-Miss Analyse

- Trials mit nur **1 Gate-Fail**: **0**
- Bester Composite: **0.126**
- DSR-Maximum erreicht: **0.000**
