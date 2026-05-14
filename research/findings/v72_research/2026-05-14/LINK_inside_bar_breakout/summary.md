# v7.2 Research Report — 2026-05-14

**Kombination:** `inside_bar_breakout` / `LINK`
**study_hash:** `c2049f0ed5d8356ed3a4c8ae0be38a33`
**objective_version:** `v72.0`
**Trials:** 50 | **Pass:** 0 | **Fail:** 50
**Laufzeit:** 60m 14s | **Ø Trial:** ~1m

## Bestes Trial

| Feld | Wert |
|------|------|
| params_hash | `fc3dc97bc56bd78a9da9564cd4be2a14` |
| composite | 0.511 |
| DSR | 0.000 |
| PBO | 0.698 |
| MaxDD | 5.424R |
| Stability | 0.596 |
| oos_folds_n | 18 |

## Top-10 Trials

| # | Composite | DSR | PBO | MaxDD | Stability | Gate-Fails |
|---|-----------|-----|-----|-------|-----------|------------|
| 1 | 0.511 | 0.000 | 0.698 | 5.424 | 0.596 | ❌ DSR+PBO+MaxDD |
| 2 | 0.146 | 0.000 | 0.555 | 5.457 | 0.000 | ❌ DSR+PBO+Stab+MaxDD |
| 3 | 0.012 | 0.000 | 0.115 | 13.476 | 0.395 | ❌ DSR+Stab+MaxDD |
| 4 | 0.012 | 0.000 | 0.104 | 11.388 | 0.416 | ❌ DSR+Stab+MaxDD |
| 5 | 0.011 | 0.000 | 0.116 | 11.384 | 0.391 | ❌ DSR+Stab+MaxDD |
| 6 | 0.011 | 0.000 | 0.130 | 11.391 | 0.418 | ❌ DSR+Stab+MaxDD |
| 7 | 0.006 | 0.000 | 0.556 | 13.735 | 0.216 | ❌ DSR+PBO+Stab+MaxDD |
| 8 | 0.005 | 0.000 | 0.326 | 14.686 | 0.635 | ❌ DSR+PBO+MaxDD |
| 9 | 0.004 | 0.000 | 0.113 | 11.394 | 0.462 | ❌ DSR+Stab+MaxDD |
| 10 | -0.002 | 0.000 | 0.352 | 14.063 | 0.414 | ❌ DSR+PBO+Stab+MaxDD |

## Häufigste Fail-Reasons

- **50/50** DSR < 0.50
- **50/50** MaxDD > 5.0R
- **40/50** PBO > 0.30
- **20/50** Stability < 0.50

## Near-Miss Analyse

- Trials mit nur **1 Gate-Fail**: **0**
- Bester Composite: **0.511**
- DSR-Maximum erreicht: **0.000**
