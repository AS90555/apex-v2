# v7.2 Screening Ranking — 2026-05-14

**6 Kombinationen × 50 Trials | Gesamt-Laufzeit: ~11h | Pass-Kandidaten gesamt: 0**

---

## Ranking

| Rang | Strategie | Asset | Pass | Bester Composite | DSR | PBO | Stability | 1-Gate-Fails |
|------|-----------|-------|------|-----------------|-----|-----|-----------|--------------|
| 1 | donchian_breakout | **LINK** | 0/50 | **0.873** | 1.000 | 0.008 | 0.307 | **4** |
| 2 | donchian_breakout | **XRP** | 0/50 | **0.822** | 0.995 | 0.032 | 0.000 | **2** |
| 3 | donchian_breakout | AVAX | 0/50 | 0.810 | 1.000 | 0.543 | 0.248 | 1 |
| 4 | inside_bar_breakout | LINK | 0/50 | 0.511 | 0.000 | 0.698 | — | 0 |
| 5 | inside_bar_breakout | XRP | 0/50 | 0.451 | 0.000 | 0.481 | — | 0 |
| 6 | inside_bar_breakout | AVAX | 0/50 | 0.126 | 0.000 | 0.661 | — | 0 |

---

## Near-Miss Analyse — donchian_breakout Top-3

### #1: donchian_breakout / LINK (4 × 1-Gate-Fail)

**Bestes Trial:** Composite=0.873 | DSR=1.000 | PBO=0.008 | MaxDD=1.118R | Stability=**0.307** ❌

Die Blockade: **Stability < 0.50** in 48/50 Trials. DSR und PBO sind lösbar — DSR=1.0 und PBO=0.008
wurden beide erreicht. Die Sharpe-Kurve ist über die 18 Folds zu ungleichmäßig verteilt.

| Bestes Trial | Composite | DSR | PBO | MaxDD | Stability | Fehlende Gate(s) |
|-------------|-----------|-----|-----|-------|-----------|------------------|
| Trial 1 | 0.873 | 1.000 | 0.008 | 1.118 | 0.307 | Stab (+0.193) |
| Trial 4 | 0.791 | 1.000 | 0.228 | 2.485 | 0.090 | Stab (+0.410) |
| Trial 5 | 0.766 | 0.497 | 0.000 | 1.097 | 0.424 | DSR (+0.003), Stab (+0.076) |

**Interpretation:** Der Donchian-Channel auf LINK erzeugt in manchen Folds sehr starke Signale,
in anderen ist er flach. Das deutet auf Regime-Abhängigkeit hin — LINK hat 2024–2026 zwei
klar verschiedene Volatilitätsphasen. Stability-Gate korrekt: kein robuster Dauertrend.

---

### #2: donchian_breakout / XRP (2 × 1-Gate-Fail)

**Bestes Trial:** Composite=0.822 | DSR=0.995 | PBO=0.032 | MaxDD=1.181R | Stability=**0.000** ❌

Die Blockade: **Stability=0.000** bei sonst fast perfekten Metriken. DSR=0.995 ist praktisch 1.0.
Nur ein einziger Fold trägt alle positiven Returns — Rest flat.

| Bestes Trial | Composite | DSR | PBO | MaxDD | Stability | Fehlende Gate(s) |
|-------------|-----------|-----|-----|-------|-----------|------------------|
| Trial 1 | 0.822 | 0.995 | 0.032 | 1.181 | 0.000 | Stab (+0.500) |
| Trial 3 | 0.624 | 0.000 | 0.091 | 2.376 | 0.536 | DSR (+0.500) |

**Interpretation:** XRP/Donchian ist bi-modal. Entweder starker Trend in einem Fold mit DSR=1.0 aber
Stability=0 (alles in einem Block), oder verteilte Folds aber DSR kippt. Kein "smooth" Regime gefunden.

---

### #3: donchian_breakout / AVAX (1 × 1-Gate-Fail)

**Bestes Trial:** Composite=0.810 | DSR=1.000 | PBO=0.543 | MaxDD=1.156R | Stability=0.248 ❌

Die Blockade: **PBO=0.543 >> 0.30** UND **Stability=0.248**. Zwei Gates gleichzeitig.
AVAX zeigt hohe IS/OOS-Divergenz — Overfit-Signal dominiert.

---

### inside_bar_breakout — alle Kombis: strukturelle Schwäche

DSR=0.000 in 100% der Trials für alle 3 Assets. Das ist kein Parameter-Problem,
sondern ein Signal-Problem: Inside-Bar-Breakouts erzeugen in diesem Marktregime
(2024–2026: volatile, aber trendlose Altcoins) systematisch negative OOS-Sharpes.

**Empfehlung:** `inside_bar_breakout` für dieses Fenster ausschließen.

---

## Gate-Häufigkeitsanalyse (donchian_breakout alle 3 Assets, 150 Trials)

| Gate | Fehlschlag-Rate | Bedeutung |
|------|----------------|-----------|
| Stability < 0.50 | **~95%** | Haupt-Blocker: Fold-Inkonsistenz |
| DSR < 0.50 | ~80% | Sekundär: OOS-Bootstrap-Signal schwach |
| PBO > 0.30 | ~55% | Overfit-Schutz funktioniert korrekt |
| MaxDD > 5.0R | ~10% | Selten, nur bei extremen Param-Kombinationen |

**Kernaussage:** Der Stability-Gate ist der echte Engpass. Ein Trial kann DSR=1.0 und PBO=0.008
erreichen — aber wenn die Sharpe-Kurve über 18 Folds zu ungleichmäßig ist, schlägt Stability an.
Das ist korrekt: instabile Returns über Folds → keine Strategie für Deployment.

---

## Entscheidung

### **GO-SCREEN**

**Begründung:**

0 Pass-Kandidaten bei 300 Trials total (10 BTC + 50×6). Das Framework funktioniert korrekt —
die getesteten Kombis liefern keinen *konsistenten* OOS-Edge über alle 18 Walk-Forward-Folds.

ABER: `donchian_breakout/LINK` und `donchian_breakout/XRP` zeigen, dass DSR und PBO lösbar sind.
Der Stability-Gate ist die echte Hürde, und der ist prinzipiell überwindbar — es braucht Parameter,
die auf alle Regimephasen gleichmäßig wirken, nicht nur auf einzelne Trend-Episoden.

**Was als nächstes sinnvoll wäre:**

| Option | Beschreibung | Erwartung |
|--------|-------------|-----------|
| **A) Mehr Trials LINK** | 150–200 Trials `donchian_breakout/LINK` | TPE konvergiert auf Stability-stabile Params |
| **B) Andere Strategien** | `squeeze`, `dual_donchian` auf LINK/XRP testen | Andere Signal-Typen — anderes Stabilitätsprofil |
| **C) Kürzeres Fenster** | 365 statt 730 Tage — nur neuestes Regime | Weniger Folds, aber Stability leichter erreichbar |
| **D) Akzeptanz** | 2024–2026 ist kein Trend-Regime für Breakouts | Warten auf neues Marktregime |

**Empfohlene Reihenfolge:** Option B zuerst (squeeze/LINK, squeeze/XRP, dual_donchian/LINK).
Option A parallel als Konvergenzcheck für LINK/Donchian.

---

## Top-Kombinationen für nächsten Run

| Priorität | Kombination | Begründung |
|-----------|------------|------------|
| **1** | `donchian_breakout` / `LINK` | 4 × 1-Gate-Fail, DSR=1.0 und PBO=0.008 bereits erreicht |
| **2** | `donchian_breakout` / `XRP` | 2 × 1-Gate-Fail, DSR=0.995, niedrige MaxDD |
| **3** | `squeeze` / `LINK` | Anderer Signal-Typ, ähnliches Asset — Stabilitätsprofil unklar |
| **4** | `squeeze` / `XRP` | Momentum-Signal könnte bei XRP-Regime besser passen |
| *Skip* | `inside_bar_breakout` alle | DSR=0.000 strukturell — kein Edge in diesem Regime |
| *Skip* | `donchian_breakout` / `AVAX` | PBO systemisch > 0.30 — Overfit dominiert |
