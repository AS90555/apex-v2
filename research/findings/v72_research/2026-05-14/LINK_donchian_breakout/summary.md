# v7.2 Research Report — 2026-05-14

**Kombination:** `donchian_breakout` / `LINK`
**study_hash:** `c8fcd1407760c17a7424bcbad33cfc78`
**objective_version:** `v72.0`
**Trials:** 50 | **Pass:** 0 | **Fail:** 50
**Laufzeit:** 112m 25s | **Ø Trial:** ~2m

## Bestes Trial

| Feld | Wert |
|------|------|
| params_hash | `cd57cf99adfb8293b2d9e0f5eea7331d` |
| composite | 0.873 |
| DSR | 1.000 |
| PBO | 0.008 |
| MaxDD | 1.118R |
| Stability | 0.307 |
| oos_folds_n | 18 |

## Top-10 Trials

| # | Composite | DSR | PBO | MaxDD | Stability | Gate-Fails |
|---|-----------|-----|-----|-------|-----------|------------|
| 1 | 0.873 | 1.000 | 0.008 | 1.118 | 0.307 | ❌ Stab |
| 2 | 0.834 | 1.000 | 0.387 | 3.215 | 0.582 | ❌ PBO |
| 3 | 0.825 | 0.969 | 0.563 | 2.423 | 0.582 | ❌ PBO |
| 4 | 0.791 | 1.000 | 0.228 | 2.485 | 0.090 | ❌ Stab |
| 5 | 0.766 | 0.497 | 0.000 | 1.097 | 0.424 | ❌ DSR+PBO+Stab |
| 6 | 0.733 | 0.348 | 0.000 | 1.238 | 0.471 | ❌ DSR+PBO+Stab |
| 7 | 0.729 | 0.776 | 0.598 | 3.565 | 0.440 | ❌ PBO+Stab |
| 8 | 0.684 | 0.404 | 0.214 | 1.256 | 0.199 | ❌ DSR+Stab |
| 9 | 0.609 | 0.811 | 0.000 | 1.236 | 0.000 | ❌ PBO+Stab |
| 10 | 0.573 | 0.000 | 0.000 | 1.353 | 0.000 | ❌ DSR+PBO+Stab |

## Häufigste Fail-Reasons

- **48/50** Stability < 0.50
- **43/50** DSR < 0.50
- **40/50** PBO > 0.30
- **3/50** MaxDD > 5.0R

## Near-Miss Analyse

- Trials mit nur **1 Gate-Fail**: **4**
- Bester Composite: **0.873**
- DSR-Maximum erreicht: **1.000**
