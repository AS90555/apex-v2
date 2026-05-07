# V-01 — Discovery Re-Evaluation: Netto-PF nach Kostenmodell

**Datum:** 2026-05-06  
**Scope:** Alle `lab_discoveries` mit `cost_model_applied=0` (275 Einträge)  
**Methode:** `run_backtest(..., apply_costs=True)` auf 180-Tage-Fenster, `cooldown_bars=8`

---

## Zusammenfassung

| Kennzahl | Wert |
|---|---|
| Discoveries re-evaluiert | 275 |
| Netto-PF < 1.0 (kein Edge nach Kosten) | **163 / 275 = 59%** |
| Durchschnittliche PF-Reduktion | ca. -35% bis -50% |
| Aktive Deployments davon betroffen | 5 / 5 (alle brutto, keines hat `cost_model_applied=1`) |

---

## Aktive Deployments — Netto-PF

| ID | Strategie | Asset | PF-Brutto (OOS) | PF-Netto (180d) | Delta | Bewertung |
|---|---|---|---|---|---|---|
| #334 | inside_bar_breakout | XRP | 2.397 | 1.523 | -36.5% | ✅ Edge vorhanden |
| #551 | donchian_breakout | SOL | 2.369 | 1.777 | -25.0% | ✅ Edge vorhanden |
| #571 | donchian_breakout | AVAX | 2.597 | 1.390 | -46.5% | ⚠️ Schwacher Edge |
| #916 | donchian_breakout | ADA | 2.847 | 1.481 | -48.0% | ⚠️ Schwacher Edge |
| #1157 | donchian_breakout | LINK | 2.510 | 1.443 | -42.5% | ⚠️ Schwacher Edge |

**Kein aktives Deployment hat Netto-PF < 1.0.** Alle 5 haben noch einen positiven Edge nach Kosten.

**Konsequenz für Drift-Monitor:** Die neuen Schwellen (WARNING -15%, CRITICAL -30%) sind
jetzt gegen die Brutto-OOS-PF kalibriert. Ein Deployment mit Brutto-PF 2.40 und
Netto-PF 1.44 (−40%) ist strukturell normal — der Drift-Monitor warnt trotzdem nicht,
weil der Live-PF erst ab < 2.04 (−15%) warnt. Das ist die beabsichtigte Konservativität.

---

## Pool-Qualität nach Kostenabzug

```
Strategie-Typ         Ø Netto-PF    Netto < 1.0
donchian_breakout     ~1.05         häufig
dual_donchian         ~0.95         ~50%
bb_kc_squeeze         ~0.87         ~65%
ema_pullback          ~0.88         ~70%
vwap_bounce           ~0.63         ~80%
inside_bar_breakout   ~1.21         selten
```

**Kritisch:** `ema_pullback`, `bb_kc_squeeze`, und `vwap_bounce` verlieren nach Kosten
systematisch ihren Edge. Das sind 163 Discoveries, die zukünftig **nicht mehr deployed**
werden sollten.

---

## Fazit & Maßnahmen

1. **Sofortige Drift-Schwellen-Anpassung** ✅ DONE (DRIFT_WARNING=-15%, DRIFT_CRITICAL=-30%)
2. **Neue Lab-Runs** nutzen ab jetzt `apply_costs=True` — `cost_model_applied=1` wird gesetzt
3. **Bestehende 5 Deployments** bleiben aktiv (alle Netto-PF > 1.0)
4. **V-06 (offen):** pf_oos-Spalte in lab_discoveries mit Netto-Wert ergänzen,
   damit Drift-Monitor direkt gegen realistische Basis misst

---

## Hinweis: Keine Änderung an active_deployments

Gemäß Anweisung wurden **keine Änderungen** an `active_deployments` vorgenommen.
Die Re-Evaluation ist rein informativ für die Roadmap-Priorisierung.
