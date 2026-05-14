# v7.2 Research Report — 2026-05-14

**Kombination:** `inside_bar_breakout` / `XRP`
**study_hash:** `22a0c1a5a3515b3852612d5d8f25b521`
**objective_version:** `v72.0`
**Trials:** 50 | **Pass:** 0 | **Fail:** 50
**Laufzeit:** 59m 20s | **Ø Trial:** ~1m

## Bestes Trial

| Feld | Wert |
|------|------|
| params_hash | `da307a2d991e5833608900a81c844d73` |
| composite | 0.451 |
| DSR | 0.000 |
| PBO | 0.481 |
| MaxDD | 5.054R |
| Stability | 0.000 |
| oos_folds_n | 18 |

## Top-10 Trials

| # | Composite | DSR | PBO | MaxDD | Stability | Gate-Fails |
|---|-----------|-----|-----|-------|-----------|------------|
| 1 | 0.451 | 0.000 | 0.481 | 5.054 | 0.000 | ❌ DSR+PBO+Stab+MaxDD |
| 2 | 0.448 | 0.000 | 0.856 | 3.321 | 0.000 | ❌ DSR+PBO+Stab |
| 3 | 0.333 | 0.000 | 0.407 | 8.393 | 0.343 | ❌ DSR+PBO+Stab+MaxDD |
| 4 | 0.296 | 0.000 | 0.729 | 5.775 | 0.000 | ❌ DSR+PBO+Stab+MaxDD |
| 5 | 0.263 | 0.000 | 0.613 | 4.596 | 0.095 | ❌ DSR+PBO+Stab |
| 6 | 0.177 | 0.000 | 0.902 | 4.655 | 0.450 | ❌ DSR+PBO+Stab |
| 7 | 0.171 | 0.000 | 0.794 | 5.314 | 0.003 | ❌ DSR+PBO+Stab+MaxDD |
| 8 | 0.123 | 0.000 | 0.675 | 5.786 | 0.579 | ❌ DSR+PBO+MaxDD |
| 9 | 0.044 | 0.000 | 0.910 | 4.635 | 0.382 | ❌ DSR+PBO+Stab |
| 10 | 0.035 | 0.000 | 0.788 | 8.249 | 0.446 | ❌ DSR+PBO+Stab+MaxDD |

## Häufigste Fail-Reasons

- **50/50** DSR < 0.50
- **49/50** PBO > 0.30
- **40/50** MaxDD > 5.0R
- **25/50** Stability < 0.50

## Near-Miss Analyse

- Trials mit nur **1 Gate-Fail**: **0**
- Bester Composite: **0.451**
- DSR-Maximum erreicht: **0.000**
