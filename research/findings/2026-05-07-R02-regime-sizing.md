# Finding: R-02 — Regime-Sizing (SIDEWAYS/HIGH_VOL → 0.5×)

**Datum:** 2026-05-07
**Roadmap-ID:** R-02
**Status:** DONE

---

## Zusammenfassung

Regime-abhängiges Position-Sizing implementiert. Analoge Umsetzung zum bestehenden
`HALF_SIZE`-Pattern aus S-03 (DailyDrawdownCheck). Keine Architektur-Änderung —
nur neues Flag im governance_log-Reason-String, das der Executor liest.

---

## Implementierung

### governance/checks.py — HMMRegimeCheck.evaluate()

Neue Logik nach der `regime not in allowed`-Prüfung:

```python
if regime == "SIDEWAYS" and signal.strategy in STRATEGY_ALLOWED_REGIMES:
    return True, f"HMM_WARN: regime=SIDEWAYS — REGIME_HALF"
if regime == "HIGH_VOL":
    return True, f"hmm_regime=HIGH_VOL — REGIME_HALF"
return True, f"hmm_regime={regime} OK"
```

**Rationale:**
- `SIDEWAYS + strategy in ALLOWED_REGIMES`: Strategie ist für Sideways konfiguriert,
  aber die Unsicherheit bleibt höher als bei TREND → halbe Größe als Sicherheitspuffer.
- `HIGH_VOL`: Volatile Märkte erhöhen Slippage und Stop-Loss-Durchbruch-Risiko →
  halbe Größe unabhängig von Strategie-Mapping.
- `TREND + in allowed`: Volle Größe, kein Flag.
- `regime not in allowed`: `HMM_WARN` ohne `REGIME_HALF` (kein Sizing-Effekt,
  da Soft-Warning-Modus — später Hard-Block via B-05).

### execution/executor.py — REGIME_HALF-Check

```python
if _row and _row[0] and "REGIME_HALF" in _row[0]:
    s_dec = SIZE_DECIMALS.get(signal.asset, 2)
    size  = round(size * 0.5, s_dec)
    regime_tag = "SIDEWAYS" if "SIDEWAYS" in _row[0] else "HIGH_VOL"
    log(f"[EXECUTOR] Regime-Half-Size: {size} (Regime: {regime_tag})")
```

Liest `governance_log.reason` für `signal_id` — identisches Pattern wie HALF_SIZE (S-03).

---

## Sizing-Matrix (Vollständig)

| Situation | Flag | Größe |
|-----------|------|-------|
| TREND, in allowed | `hmm_regime=TREND OK` | 100% |
| SIDEWAYS, in allowed | `HMM_WARN: regime=SIDEWAYS — REGIME_HALF` | 50% |
| HIGH_VOL, in allowed | `hmm_regime=HIGH_VOL — REGIME_HALF` | 50% |
| Regime not in allowed | `HMM_WARN: regime=X not in [...]` | 100%* |
| Daily-DD -1.5R | `daily_dd_half: ... — HALF_SIZE` | 50% |
| Beide aktiv (DD + Regime) | beide Flags | 25%** |

*Soft-Warning-Modus: kein Sizing-Effekt. Nach B-05-Aktivierung: Trade geblockt.
**Kumulierung möglich — HALF_SIZE und REGIME_HALF werden sequenziell angewandt.

---

## Verifikation

- `python3 -m py_compile governance/checks.py` → OK
- `python3 -m py_compile execution/executor.py` → OK
- `python3 tests/parity_test.py` → **12 PASS | 0 FAIL | 1 SKIP**

Parity-Test nicht betroffen: er testet entry_price/SL/TP-Konsistenz, nicht Sizing.

---

## Aktueller Effekt (2026-05-07)

| Asset | Regime | Sizing-Effekt |
|-------|--------|---------------|
| SOL | SIDEWAYS | → 50% bei donchian_breakout |
| XRP | TREND | → 100% |
| AVAX | SIDEWAYS | → 50% bei donchian_breakout |
| LINK | TREND | → 100% |
| ADA | TREND | → 100% |

SOL und AVAX sind aktuell im SIDEWAYS-Regime → nächste Signale erhalten halbierte Größe.

---

## Nächste Schritte

- B-05: Nach 30 Live-Trades mit HMM-Daten → Hard-Block für `regime not in allowed`
- Monitoring: governance_log auf `REGIME_HALF` prüfen um Häufigkeit zu validieren
