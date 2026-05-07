# Brief: V-03 — Deflated Sharpe Ratio als Lab-Filter

**Datum:** 2026-05-07
**Priorität:** P1
**Adressat:** backtest-validator

## Kontext
BH (V-02) kontrolliert FDR über p-Werte aus PF-Schätzungen.
DSR ergänzt das auf einer zweiten Ebene: je mehr Strategien
getestet wurden (N), desto höher muss der Sharpe sein um
als signifikant zu gelten. Quelle: Bailey & López de Prado (2014).

## Kernformel

Erwarteter Max-Sharpe unter Null-Hypothese (N Tests):
  SR_benchmark = (1 - γ_E) × Φ⁻¹(1 - 1/N) + γ_E × Φ⁻¹(1 - 1/(N×e))
  γ_E = 0.5772156649  (Euler-Mascheroni-Konstante)

DSR (Wahrscheinlichkeit dass SR echt ist):
  z = (SR_hat - SR_benchmark) × √(T - 1)
      / √(1 - γ3×SR_hat + γ4×SR_hat²/4)
  DSR = Φ(z)

Wobei:
  SR_hat = annualisierter Sharpe der Strategie (aus Trades)
  T      = Anzahl Trades (Beobachtungen)
  γ3     = Schiefe der Trade-Returns (pnl_r)
  γ4     = Excess-Kurtosis der Trade-Returns
  N      = Anzahl getesteter Kombinationen in dieser Lab-Runde
  Φ      = Standard-Normalverteilung CDF (via math.erfc)

## Aufgabe

1. research/auto_lab_daemon.py:
   Neue Hilfsfunktion _calc_dsr(pnl_rs, n_tested) → float (0-1)
   - SR_hat: mean(pnl_r) / std(pnl_r) × √252 (annualisiert)
   - γ3, γ4 aus pnl_r Liste berechnen
   - SR_benchmark aus Formel oben
   - DSR = Φ(z), Rückgabe zwischen 0 und 1

2. Neue Konstante in config/settings.py:
   MIN_DSR = 0.95  (DSR muss > 95% Konfidenz haben)

3. _passes() erweitern:
   Nach bestehendem PF/WR/n-Check:
   pnl_rs = alle pnl_r Werte aus Fenster 3 (aktuellstes)
   dsr = _calc_dsr(pnl_rs, n_tested=len(candidates_so_far))
   if dsr < MIN_DSR: return False, f"dsr={dsr:.3f}<{MIN_DSR}"

4. lab_discoveries: neue Spalte dsr REAL ergänzen (Migration)
   Beim INSERT: dsr-Wert mitschreiben

## Akzeptanzkriterien
- [ ] _calc_dsr() gibt sinnvolle Werte (0.90-0.99 bei guten Strategien)
- [ ] Synthetischer Test: SR=2.0, T=100, N=70 → DSR > 0.95?
- [ ] Synthetischer Test: SR=1.2, T=40, N=70 → DSR < 0.95?
- [ ] py_compile + parity_test 12 PASS
- [ ] lab_discoveries hat Spalte dsr

## Risiken
- Bei T < 10 Trades ist DSR numerisch instabil → Guard einbauen:
  if len(pnl_rs) < 10: return False, "dsr: n<10 instabil"
- γ3/γ4 bei kleinem n sehr rauschig → akzeptabel, konservativ

## Abhängigkeiten
V-02 (DONE), V-04 (DONE), auto_lab_daemon.py, core/db.py
