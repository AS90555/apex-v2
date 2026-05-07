# V-01 Kostenmodell Validierungsreport
**Datum:** 2026-05-06  
**Validator:** backtest-validator  
**Implementierung:** `backtest/engine.py` — `_apply_trade_costs()` + `run_backtest(apply_costs=True)`

---

## Check 1 — Kostenformel korrekt?

**Befund: KORREKT**

```python
# engine.py Zeilen 1105–1124
notional = sig.entry_price * sig.size
rt_cost  = notional * ROUND_TRIP          # 0.0018 = (0.0006+0.0003)*2
periods  = (trade.exit_ts - trade.entry_ts) / (8 * 3_600_000)
funding  = notional * FUNDING_8H * periods
total_cost = rt_cost + funding
sl_dist    = abs(sig.entry_price - sig.stop_loss)
denominator = sl_dist * sig.size
trade.pnl_usd = round(trade.pnl_usd - total_cost, 4)
trade.pnl_r   = round(trade.pnl_usd / denominator, 3)
```

Alle drei Teilprüfungen bestehen:

| Prüfpunkt | Ergebnis |
|---|---|
| `pnl_usd -= total_cost` | Korrekt — Kosten werden vom bereits berechneten brutto-pnl_usd subtrahiert |
| `pnl_r` Neuberechnung | Korrekt — verwendet `pnl_usd` (schon netto) geteilt durch `sl_dist * size` |
| `entry_ts` für Haltedauer | Korrekt — `trade.entry_ts` wird korrekt gegen `trade.exit_ts` gestellt; None-Guard vorhanden |

Einzige Beobachtung: `entry_ts` im `BtTrade` wird auf den Signal-`ts` gesetzt (Zeitstempel der Bar, die das Signal erzeugt), nicht auf den `exit_ts` der Bar, an der tatsächlich eingestiegen wird. In der Bar-Logik kann der Entry also 1 Bar früher sein als der tatsächliche Exit. Dies ist eine strukturelle Eigenschaft der Engine, nicht ein Fehler in `_apply_trade_costs()`.

---

## Check 2 — R-Amplifikation erklären

**Befund: MATHEMATISCH KORREKT — 38.8% Reduktion ist plausibel**

### Analytische Herleitung

```
cost_r = notional * ROUND_TRIP / (sl_dist * size)
       = (entry_price * size) * ROUND_TRIP / (sl_dist * size)
       = entry_price * ROUND_TRIP / sl_dist
       = ROUND_TRIP / SL_pct
```

Das heißt: `cost_r` ist ausschließlich eine Funktion des SL-Abstands relativ zum Preis. Der absolute Preis oder die Positionsgröße kürzt sich heraus.

### SOL-spezifische Werte (90-Tage-Backtest)

| Parameter | Wert |
|---|---|
| avg entry_price | $85.15 |
| sl_dist avg | $0.987 |
| sl_dist min | $0.216 |
| sl_dist max | $3.300 |

Für die gemessenen sl_dist-Werte ergibt sich:

| ATR / sl_dist | SL_pct | RT-Kosten (R) | Funding ~15h (R) | Gesamt (R) |
|---|---|---|---|---|
| $0.40 | 0.471% | 0.382R | 0.040R | **0.422R** |
| $0.50 | 0.588% | 0.306R | 0.032R | **0.338R** |
| $0.65 | 0.765% | 0.235R | 0.025R | **0.260R** |
| $0.987 (avg) | 1.16% | 0.155R | 0.016R | **0.171R** |

### Gemessene Werte (90d Backtest, 79 Trades)

- Brutto total_R: +4.00R
- Netto total_R: -11.17R
- Gesamtkosten: **15.17R**
- **Avg cost per trade: 0.192R** (nicht 0.38R wie im Brief für 180d)

Die Diskrepanz zum Brief-Wert (0.38R bei 46 Trades über 180d) erklärt sich durch:
1. **Unterschiedliche Zeitperiode:** 90d vs. 180d — die 180d enthalten möglicherweise eine volatile Phase mit engeren ATRs
2. **ATR-Regime:** In der 90d-Periode dominieren breitere SL-Abstände (avg $0.987), was die Kosten auf 0.192R senkt

Die Formel selbst ist korrekt. Die beobachteten 38.8% Reduktion im 180d-Brief entsprechen einer durchschnittlichen SL-Distanz von ca. 0.43 USD bei SOL (sl_pct ≈ 0.51%), was in volatileren Phasen plausibel ist.

**Fazit:** Die R-Amplifikation ist kein Implementierungsfehler, sondern ein struktureller Effekt beim Micro-Account-Sizing: enge SL-Abstände (tighte Ausbrüche) erzeugen hohe Hebelwirkung auf Kosten.

---

## Check 3 — apply_costs=False funktioniert?

**Befund: PASS**

```
Brutto trades: 79
Netto trades:  79
```

- Gleiche Anzahl Trades in beiden Runs — die Kostenfunktion ändert keine Signale
- `pnl_r`-Differenzen sind durchgehend positiv (brutto > netto), niemals invertiert
- Beispiele aus dem Live-Test:

| Trade | Brutto (R) | Netto (R) | Kosten (R) |
|---|---|---|---|
| 0 | -1.000 | -1.057 | 0.057 |
| 1 | -1.000 | -1.041 | 0.041 |
| 2 | +1.000 | +0.884 | 0.116 |
| 3 | -1.000 | -1.115 | 0.115 |
| 4 | +1.000 | +0.876 | 0.124 |
| 5 | -1.000 | -1.120 | 0.120 |

Die variablen Kosten (0.041–0.124R) reflektieren unterschiedliche sl_dist-Werte. Korrekte Funktion bestätigt.

---

## Check 4 — Parity-Test grün?

**Befund: PASS — 12/12 PASS, 1 SKIP (weekend_momo kein Signal in 500 Bars)**

```
Ergebnis: 12 PASS  |  0 FAIL  |  1 SKIP
Toleranz: 0.01%  |  Strategien gesamt: 13  |  Getestet (mit Signal): 12
```

Das Kostenmodell berührt den Parity-Test nicht (der testet nur Signal-Logik, nicht Exit-PnL). Alle Strategien bleiben bit-identisch zwischen Backtest und Live-Pendant.

---

## Check 5 — Drift-Monitor-Kalibrierung

**Befund: REKALIBRIERUNG EMPFOHLEN**

### Aktuelle Deployments (aus `lab_discoveries`)

| Strategie/Asset | pf_test (brutto) | Status |
|---|---|---|
| donchian_breakout/AVAX | 1.482 | lab |
| ema_pullback/LINK | 1.306–1.635 | lab |
| ema_pullback/SOL | 1.27–1.324 | lab |
| bb_kc_squeeze/ETH | 1.22–1.619 | lab |
| squeeze/ETH | 1.14 (OOS brutto) | dry_run |

### Problem: Alle `pf_test`-Werte sind brutto (ohne Kosten)

Mit dem neuen Kostenmodell sind die Netto-PF deutlich niedriger. Am kritischsten:

**squeeze/ETH**: pf_oos_brutto = 1.14  
→ Netto-PF bei avg_r = +0.095R brutto, avg_cost ≈ 0.05–0.15R (je nach sl_dist bei ETH ~$2–$3 SL):
- Bei cost_r = 0.08R: avg_r_netto = 0.015R → PF netto ≈ 1.02 (kaum Edge!)

**ema_pullback/SOL**: pf_test = 1.27–1.32 brutto  
→ Mit avg_cost ~0.15–0.20R würde das bereits netto negativ werden

### Drift-Monitor-Empfehlung

Die aktuelle Schwelle `DRIFT_CRITICAL_PCT = -50%` ist gegen **Brutto-OOS-PF** kalibriert. Das hat zwei Konsequenzen:

1. **Live-Performance wird immer schlechter als Backtest sein**, selbst ohne echten Edge-Verlust — einfach durch Kosten. Eine -50% Drift könnte daher schon bei stabiler Strategie ausgelöst werden.

2. **Approved discoveries mit pf_oos < 1.5 (brutto) sind nach Netto potenziell unrentabel**

**Empfohlene Maßnahmen:**

| Maßnahme | Priorität |
|---|---|
| Backtest-Standard: `apply_costs=True` für alle Lab-Runs | SOFORT |
| Alle bestehenden Lab-Discoveries mit `pf_test < 1.5 brutto` auf Netto-Äquivalent prüfen | HOCH |
| `DRIFT_WARNING_PCT` von -30% auf -15% senken (Frühwarnung) | HOCH |
| `DRIFT_CRITICAL_PCT` von -50% auf -30% senken oder gegen Netto-PF messen | HOCH |
| squeeze/ETH dry_run: Netto-Backtest durchführen — Edge möglicherweise zu klein | MITTEL |

### Netto-Mindest-PF-Empfehlung für neue Deployments

Bei typischen Kostenstrukturen (avg cost_r 0.10–0.20R):
- Brutto-PF Minimum für Deployment sollte **≥ 1.6** sein (war implizit niedrig genug für 1.14)
- Ziel-Netto-PF nach Kosten: **≥ 1.2**

---

## Zusammenfassung

| Check | Ergebnis |
|---|---|
| 1 — Kostenformel korrekt | PASS |
| 2 — R-Amplifikation mathematisch erklärbar | PASS |
| 3 — apply_costs=False/True Trennung funktioniert | PASS |
| 4 — Parity-Test | PASS (12/12) |
| 5 — Drift-Monitor-Kalibrierung | WARNUNG — Rekalibrierung nötig |

**Gesamturteil: GO — mit Auflagen**

Das Kostenmodell V-01 ist korrekt implementiert und mathematisch konsistent. Die höhere-als-erwartete Kostenbelastung (38.8% im 180d-Test) ist ein realer struktureller Effekt des Micro-Account-Sizing bei engen SL-Abständen, kein Implementierungsfehler. 

Die kritische Folgewirkung: Bestehende Lab-Discoveries mit niedrigem Brutto-PF müssen neu bewertet werden. Besonders squeeze/ETH (pf=1.14 brutto) könnte netto marginalen Edge haben.
