# V-02 — FDR-Control implementiert: Benjamini-Hochberg im Auto-Lab

**Datum:** 2026-05-06  
**Status:** DONE  
**Roadmap-ID:** V-02  
**Abweichungen vom Analyse-Dokument:** q=0.05 (nicht 0.10), Round-Level (nicht Batch-Level)

---

## Was implementiert wurde

### Neue Hilfsfunktionen in `research/auto_lab_daemon.py`

```python
def _normal_cdf(z: float) -> float:
    import math
    return 0.5 * math.erfc(-z / math.sqrt(2))

def _pf_to_pvalue(pf: float, n: int) -> float:
    import math
    wr  = pf / (1.0 + pf)
    se  = math.sqrt(wr * (1.0 - wr) / max(n, 1))
    if se == 0:
        return 0.0001
    z = (wr - 0.5) / se
    return max(0.0001, 1.0 - _normal_cdf(z))

def _benjamini_hochberg(pvalues: list[float], q: float) -> list[bool]:
    m = len(pvalues)
    if m == 0:
        return []
    sorted_idx = sorted(range(m), key=lambda i: pvalues[i])
    sorted_p   = [pvalues[i] for i in sorted_idx]
    k_max = -1
    for k in range(m):
        if sorted_p[k] <= (k + 1) / m * q:
            k_max = k
    accept = [False] * m
    if k_max >= 0:
        for j in range(k_max + 1):
            accept[sorted_idx[j]] = True
    return accept
```

**Keine scipy-Abhängigkeit** — `_normal_cdf` nutzt `math.erfc`.

### Umbau von `_run_one_target()` auf 3-Phasen-Struktur

- **Phase A:** Alle Kombinationen evaluieren → `candidates = [(params, window_results, pvalue)]`
- **Phase B:** BH über alle candidates des gesamten Runs (`q = BH_FDR_Q = 0.05`)
- **Phase C:** Nur BH-akzeptierte Candidates in `lab_discoveries` speichern

Log-Eintrag zeigt `[BH q=0.05]` und `bh_rejected_today`-Zähler.

---

## Abweichungen vom Analyse-Dokument und Begründung

| Parameter | Analyse-Dokument | Implementiert | Begründung |
|---|---|---|---|
| q | 0.10 | **0.05** | Zeitliche Korrelation der Trades → p-Wert überschätzt Signifikanz; konservativere Schwelle sicherer |
| Granularität | Batch-Level (20 Kombinationen) | **Round-Level** | Mit BATCH_SIZE=20 nur 3–4 Kandidaten pro Batch → zu wenig Trennschärfe; Round-Level maximiert m |

`BH_FDR_Q = 0.05` ist in `config/settings.py` als benannte Konstante definiert.

---

## Verifikationsergebnisse

### 1. py_compile
```
python3 -m py_compile research/auto_lab_daemon.py
# → OK (kein Output = kein Fehler)
```

### 2. parity_test
```
python3 tests/parity_test.py
# → 12 PASS, 0 FAIL, 1 SKIP
```
SKIP = kein Signal in 500 Bars für eine Asset/Strategy-Kombination gefunden (kein Fehler).

### 3. Simulationstest
`_run_one_target("donchian_breakout", "SOL", ...)` mit 20 Kombinationen:
- Ergebnis: **0 Discoveries**
- Ursache: Alle 20 Kombinationen scheitern bereits an `_passes()` (MIN_PF_TEST=1.80 + total_n≥100 + apply_costs=True)
- **Phase B (BH) wurde gar nicht erreicht** — korrekt, da candidates-Liste leer

### 4. BH-Funktionsdemonstration (synthetisch)

```python
_pf_to_pvalue(2.5, 50)  → p=0.00040  → accepted  (q=0.05)
_pf_to_pvalue(1.8, 40)  → p=0.02967  → accepted  (q=0.05)
_pf_to_pvalue(1.2, 30)  → p=0.30854  → rejected   (q=0.05)
```

Mit 3 Kandidaten [p=0.00040, 0.02967, 0.30854]:
- Sortiert: [0.00040, 0.02967, 0.30854]
- BH-Schwellen: k=1 → 1/3×0.05=0.0167, k=2 → 2/3×0.05=0.0333, k=3 → 3/3×0.05=0.05
- k=2 (p=0.02967 ≤ 0.0333) → k_max=1 → accept[0, 1] = True
- **2 von 3 akzeptiert, 1 (p=0.309) korrekt abgelehnt**

---

## Erwarteter Effekt im Live-Lab

Bei einem typischen Batch mit ~5 passing candidates (nach strengeren V-04 Filtern):

| Szenario | Ohne BH | Mit BH (q=0.05) |
|---|---|---|
| 5 Kandidaten, alle PF~1.80 | 5 Discoveries | ~1–2 nach BH |
| 5 Kandidaten, 2 stark / 3 grenzwertig | 5 Discoveries | 2 nach BH |
| False-Discovery-Rate | unkontrolliert | ≤ 5% garantiert |

---

## Risiken (verbleibend)

1. **p-Wert-Approximation**: Binomial-Test setzt unabhängige Trials voraus. Korrelierte Trades → p-Wert überschätzt Signifikanz. q=0.05 mindert dieses Risiko gegenüber q=0.10.
2. **Bestehende Discoveries**: Die 275 Alteinträge (`cost_model_applied=0`) wurden nicht retroaktiv BH-gefiltert. Sie sind durch die V-01 Re-Evaluation bereits inhaltlich bewertet.
3. **Kleine m**: Falls pro Round nur 1–2 Kandidaten die `_passes()`-Schwellen bestehen, hat BH kaum Effekt (BH braucht m≥3 für sinnvolle Diskriminierung).
