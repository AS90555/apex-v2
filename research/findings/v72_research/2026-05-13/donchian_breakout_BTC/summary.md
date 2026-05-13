# v7.2 Research Report — 2026-05-13

study_hash: `758b234966333da396d73f47c9f73f79`  objective_version: `v72.0`
Trials: 10 | Pass: 0 | Fail/PBO-pruned: 10

---

## 1. Laufzeit

| Metrik | Wert |
|--------|------|
| Gesamtlaufzeit | 24m 16s |
| Ø Trial-Zeit | 2m 25s (145s) |
| Folds pro Trial | 18 |
| Zeitfenster | 730 Tage (2024-05-13 → 2026-05-13) |

---

## 2. Bestes Trial

| Feld | Wert |
|------|------|
| strategy | donchian_breakout |
| asset | BTC |
| trial_number | 2 |
| params_hash | `cb89ae67ace9a4a94c104c7525cb78b7` |
| study_hash | `758b234966333da396d73f47c9f73f79` |
| objective_version | `v72.0` |
| params | DC_PERIOD=10, VOL_FACTOR=2.946, ATR_MIN_MULT=1.799, SL_ATR_MULT=0.712, TP_R=1.527 |
| composite_score | 0.526 |
| DSR | 0.000 |
| PBO | 0.207 |
| MaxDD | 2.643R |
| Stability | 0.000 |
| n_oos | 27 |

---

## 3. Alle Trials — Top-10 nach Composite

| Trial | Composite | DSR | PBO | MaxDD | Stability | n_oos | Pass |
|-------|-----------|-----|-----|-------|-----------|-------|------|
| 2 | 0.526 | 0.000 | 0.207 | 2.643 | 0.000 | 27 | ❌ |
| 1 | 0.187 | 0.000 | 0.126 | 3.772 | 0.546 | 35 | ❌ |
| 6 | -0.012 | 0.000 | 0.434 | 2.539 | 0.546 | 22 | ❌ |
| 9 | -0.033 | 0.000 | 0.486 | 3.631 | 0.590 | 33 | ❌ |
| 3 | -0.072 | 0.000 | 0.308 | 6.149 | 0.548 | 77 | ❌ |
| 7 | -0.086 | 0.000 | 0.581 | 5.800 | 0.590 | 51 | ❌ |
| 0 | -0.118 | 0.000 | 0.692 | 2.754 | 0.040 | 32 | ❌ |
| 5 | -0.152 | 0.000 | 0.627 | 17.043 | 0.735 | 186 | ❌ |
| 8 | -0.166 | 0.000 | 0.432 | 11.203 | 0.518 | 133 | ❌ |
| 4 | -0.195 | 0.000 | 0.824 | 12.494 | 0.581 | 183 | ❌ |

---

## 4. Pass-Kandidaten

**0 / 10**

---

## 5. Häufigste Fail-Reasons

| Grund | Häufigkeit | Bedeutung |
|-------|-----------|-----------|
| DSR=0.000 < 0.50 | 10/10 | OOS-Returns durchgängig negativ; bootstrap_dsr gibt 0 zurück |
| PBO > 0.30 (Hard-Filter → score=0) | 8/10 | Overfit-Signal; Optuna bewertet diese Trials als 0 |
| n_oos < 100 | 4/10 | Zu kurze OOS-Sequenzen (n=22–35) bei engen Parametern |
| Stability < 0.50 | 2/10 | Instabile Faltenkurve (Trials 0, 2) |

**Wurzelursache:** donchian_breakout/BTC erzeugt im aktuellen 730-Tage-Fenster (Mai 2024 – Mai 2026)
keine positiv orientierten OOS-Sharpes. Das Signal existiert nicht robust genug, um DSR ≥ 0.50
bei n_tested=10 zu passieren. Dies ist kein Framework-Fehler — es ist die korrekte Ablehnung
einer OOS-schwachen Kombination.

---

## 6. Technische Verifikation

| Check | Status | Detail |
|-------|--------|--------|
| Staging-Writes | ✅ | 10/10 eingefügt, 0 ignoriert (idempotent) |
| study_hash gesetzt | ✅ | Alle 10 Einträge: `758b234966333da396d73f47c9f73f79` |
| objective_version gesetzt | ✅ | Alle 10 Einträge: `v72.0` |
| sync_status | ✅ | Alle `pending` (warten auf run_staging_sync) |
| Reproduzierbarkeit | ✅ | Zweiter Lauf → identische Param-Sequenz (Seed=42) |
| DB-Lock-Probleme | ✅ | Keine |
| staging_schema Bug | ⚠️ GEFIXT | `get_staging_connection()` ergänzt additive ALTERs für bestehende DBs |

**Bug-Fix während Mini-Run:** Trial 9 des ersten Laufs schlug mit `OperationalError: table lab_discoveries
has no column named study_hash` fehl, weil `executescript(STAGING_DDL)` keine neuen Spalten zu
bestehenden Tabellen hinzufügt. Fix: additive ALTER TABLE Statements in `get_staging_connection()`
für `source_discovery_id`, `study_hash`, `objective_version`. 306/306 Tests grün.

---

## 7. Entscheidung

### ✅ TECHNICAL GO — STRATEGISCHER HINWEIS

**Pipeline:** Vollständig funktional. Timing, Staging-Writes, Reproduzierbarkeit, Report-Generierung — alles korrekt.

**Strategie/Asset:** donchian_breakout/BTC hat in diesem Fenster keinen OOS-Edge. Korrekte Ablehnung durch das Framework.

**Nächste Schritte für sinnvollen Vollrun:**

1. **Mehr Trials:** 50–100 Trials geben TPE genug Raum zur Konvergenz. n_tested=10 ist DSR-streng.
2. **Andere Strategien/Assets:** `squeeze/ETH`, `inside_bar_breakout/ETH`, `dual_donchian/AVAX` — diese hatten in v7.1 die höchsten Composite-Werte trotz Fail.
3. **Kein Gate-Tuning nötig:** Die Range-Definition ist korrekt. Problem liegt im Signal.

**Empfehlung:** Vollrun mit `--n-trials 50` über 3–4 Kombinationen. Erwartete Gesamtlaufzeit: ~6–8h.
