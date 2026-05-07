# V-02 — FDR-Control Analyse: Wo Benjamini-Hochberg ansetzen würde

**Datum:** 2026-05-06  
**Status:** Analyse only — noch nicht implementiert  
**Roadmap-ID:** V-02 (OPEN)

---

## Problem: Multiple-Testing-Inflation im Auto-Lab

Das Auto-Lab testet pro Batch 20 Parameter-Kombinationen (`BATCH_SIZE = 20`) pro
(strategy, asset)-Paar. Über alle 13 Strategien × 7 Assets × N Batches werden
viele tausend Kombinationen getestet.

**Aktuelle Entscheidungslogik:**
Jede Kombination wird **unabhängig** entschieden:
- `_passes_window()` → deterministische Schwellen (PF, WR, avg_r, n)
- `_passes()` → alle 3 Fenster + `total_n ≥ 100` (neu ab V-04)
- Ergebnis: `True/False` — Discovery wird gespeichert oder verworfen

**Das Multiple-Testing-Problem:**
Wenn 1.000 Kombinationen getestet werden und 5% zufällig die Schwellen bestehen
(False-Discovery-Rate ohne Korrektur), entstehen ~50 Schein-Discoveries.
Ohne FDR-Control ist die tatsächliche False-Discovery-Rate unbekannt und unkontrolliert.

---

## Wo BH ansetzen würde: Konkrete Code-Stelle

### Haupt-Loop: `_run_one_target()` — Zeilen 940–1016

```python
# AKTUELL (Zeilen 940–978):
for params in batch:                          # 20 Kombinationen pro Batch
    h = _param_hash(strategy, asset, params)
    if _already_known(conn, h): continue

    # Backtests für 3 Fenster ...
    ok, reason = _passes(window_results)      # ← Binäre Ja/Nein-Entscheidung
    if not ok:
        continue                              # ← Sofortiges Verwerfen
    # ... Discovery speichern                 # ← Sofortiges Speichern
```

**Problem:** Jede Kombination wird sofort und unabhängig entschieden.
Kein Batch-weites Scoring, keine Rangfolge, keine Fehlerkorrektur.

---

## BH-Implementierungsplan (wenn V-02 umgesetzt wird)

### Schritt 1: Batch vollständig evaluieren

Statt jede Kombination sofort zu entscheiden: Alle `BATCH_SIZE` Kombinationen
zunächst **durchrechnen** und als (score, params, window_results)-Tripel sammeln.

### Schritt 2: p-Werte approximieren aus PF-Überlegenheit

Da der Backtest keine echten p-Werte liefert, wird ein Surrogate-Score verwendet:
```python
def _pf_to_pvalue(pf: float, n: int) -> float:
    """
    Approximiert einen p-Wert aus Profit-Factor und Sample-Size.
    Nutzt die Beziehung: WR ~ Binomial(n, p) → einseitiger Binomial-Test.
    Vereinfacht: pf > 1.0 entspricht WR > 50% → p-Wert aus Normal-Approximation.
    """
    import math
    wr_estimate = pf / (1 + pf)          # WR aus PF abgeleitet
    se = math.sqrt(wr_estimate * (1 - wr_estimate) / n)
    z  = (wr_estimate - 0.5) / se        # einseitiger z-Test gegen 50% WR
    return max(0.0001, 1 - _normal_cdf(z))
```

### Schritt 3: Benjamini-Hochberg über den Batch anwenden

```python
def _benjamini_hochberg(pvalues: list[float], q: float = 0.10) -> list[bool]:
    """
    BH-Prozedur: kontrolliert FDR auf Niveau q.
    Gibt Boolean-Maske zurück: True = Hypothese verwerfen (Discovery akzeptieren).
    """
    m = len(pvalues)
    sorted_idx = sorted(range(m), key=lambda i: pvalues[i])
    sorted_p   = [pvalues[i] for i in sorted_idx]
    
    accept = [False] * m
    for k in range(m - 1, -1, -1):
        if sorted_p[k] <= (k + 1) / m * q:
            for j in range(k + 1):
                accept[sorted_idx[j]] = True
            break
    return accept
```

### Schritt 4: Integration in `_run_one_target()`

```python
# NEU (nach Sammlung aller Batch-Ergebnisse):
candidates = []   # (params, window_results, pvalue) für alle passed

for params in batch:
    # ... Backtests ...
    ok, reason = _passes(window_results)
    if ok:
        te3 = window_results[-1]["te"]
        pvalue = _pf_to_pvalue(te3["pf"], te3["n"])
        candidates.append((params, window_results, pvalue))

# BH-Korrektur über alle candidates des Batches:
if candidates:
    pvalues = [c[2] for c in candidates]
    accepted = _benjamini_hochberg(pvalues, q=0.10)
    
    for (params, window_results, _), is_accepted in zip(candidates, accepted):
        if is_accepted:
            _save_discovery(...)       # Nur BH-korrigierte Discoveries speichern
```

---

## Erwarteter Effekt (bei q=0.10)

| Szenario | Ohne BH | Mit BH (q=0.10) |
|---|---|---|
| 20 Kombinationen getestet | ~3–4 bestehen Schwellen | ~2–3 nach BH-Korrektur |
| 70 Kombinationen (wie im Report) | ~20 Discoveries | ~7 echte Discoveries |
| False-Discovery-Rate | unkontrolliert (evtl. 30–50%) | ≤ 10% garantiert |

Die ~67% Reduktion (20 → 7) aus dem Weekly-Report deckt sich mit dem erwarteten
BH-Effekt bei q=0.10 und einer angenommenen echten Discovery-Rate von ~5%.

---

## Risiken der Implementierung

1. **p-Wert-Approximation ist grob** — Binomial-Test setzt unabhängige Binär-Trials voraus.
   Trades sind zeitlich korreliert → p-Wert überschätzt Signifikanz. Konservativere q=0.05
   wäre sicherer.

2. **Batch-Größe zu klein für BH** — Mit `BATCH_SIZE=20` sind nur 3–4 Kandidaten pro
   Batch nach `_passes()`. BH über 3 Werte hat wenig Trennschärfe. Empfehlung:
   BH über einen **Fenster-Level** (alle Kombinationen einer Runde pro Strategy/Asset)
   statt pro Batch.

3. **Bestehende Discoveries nicht betroffen** — BH würde nur auf neue Lab-Runs wirken.
   Die 275 bestehenden Discoveries (alle `cost_model_applied=0`) wären nicht retroaktiv
   neu bewertet.

---

## Empfehlung für Implementierung (DoD V-02)

```
1. _pf_to_pvalue() implementieren (Normal-Approximation des Binomial-Tests)
2. _benjamini_hochberg() implementieren (Standard-BH-Prozedur)
3. _run_one_target() umstrukturieren:
   - Phase A: alle BATCH_SIZE Kombinationen evaluieren → candidates-Liste
   - Phase B: BH über candidates mit q=0.10
   - Phase C: nur akzeptierte Candidates speichern
4. Neues lab_stats Feld: "bh_rejected" Zähler
5. py_compile + parity_test grün
```

**Geschätzter Aufwand:** 60–90 Minuten Implementierung + Test.
**Abhängigkeit:** Kann unabhängig von V-03 (DSR) implementiert werden.
