# V-05 — Ruin-Filter Analyse: Aktueller Stand und Lücke

**Datum:** 2026-05-06  
**Status:** Analyse only — noch nicht implementiert  
**Roadmap-ID:** V-05 (OPEN)

---

## Befund: Ruin-Filter nur auf Fenster 3 (aktuellstes)

### WF_WINDOWS Definition (`auto_lab_daemon.py`, Zeilen 88–101)

```python
WF_WINDOWS = [
    # Fenster 1 (alt): test_start=-480, test_end=-360, 120d OOS
    {"train_end": -480, "test_start": -480, "test_end": -360,
     "min_n": 35, "min_pf": 1.20, "min_avg_r": 0.06, "min_wr": 46.0,
     "ruin_filter": False,   # ← KEIN Ruin-Filter
     "weight": 1.0, "days": 120},

    # Fenster 2 (mittel): test_start=-240, test_end=-120, 120d OOS
    {"train_end": -240, "test_start": -240, "test_end": -120,
     "min_n": 35, "min_pf": 1.20, "min_avg_r": 0.06, "min_wr": 46.0,
     "ruin_filter": False,   # ← KEIN Ruin-Filter
     "weight": 1.5, "days": 120},

    # Fenster 3 (aktuell): test_start=-60, test_end=0, 60d OOS
    {"train_end": -60,  "test_start": -60,  "test_end": 0,
     "min_n": 22, "min_pf": 1.20, "min_avg_r": 0.06, "min_wr": 46.0,
     "ruin_filter": True,    # ← Ruin-Filter aktiv
     "weight": 2.0, "days": 60},
]
```

### Ruin-Filter Implementierung (`_passes_window()`, Zeilen 697–701)

```python
if wcfg["ruin_filter"]:
    max_dd_usdt = max_dd_r * RISK_PER_TRADE
    ruin_limit  = STARTING_CAPITAL * MAX_DRAWDOWN_PERCENT   # = 68.33 * 0.25 = ~17.08 $
    if max_dd_usdt > ruin_limit:
        return False, f"ruin_filter: dd={max_dd_usdt:.1f}$>{ruin_limit:.1f}$ ({max_dd_r:.1f}R)"
```

**Konstanten:**
- `STARTING_CAPITAL = 68.33`
- `MAX_DRAWDOWN_PERCENT = 0.25`  (Zeile 122)
- `RISK_PER_TRADE` = `RISK_USDT = 1.50` aus settings
- `ruin_limit = 68.33 × 0.25 = 17.08 $` → entspricht `17.08 / 1.50 = 11.4R` Max-DD

Der Filter prüft: *"Hat die Strategie im OOS-Fenster eine Equity-Kurven-Delle > 11.4R erlitten?"*

---

## Das Problem: Fenster 1 und 2 sind blind für Ruin-Drawdowns

Eine Strategie kann in Fenster 1 (vor 13–16 Monaten) oder Fenster 2 (vor 4–8 Monaten)
einen Max-DD von 20R erlitten haben und wird **trotzdem approved**, weil der Ruin-Filter
dort auf `False` steht.

### Konkretes Ruin-Szenario das durchschlüpft

| Fenster | Zeitraum | Max-DD | Ruin-Filter | Ergebnis |
|---|---|---|---|---|
| W1 (alt) | -480 bis -360 | 25R → 37.50 $ | **False** | PASS (kein Check) |
| W2 (mittel) | -240 bis -120 | 18R → 27.00 $ | **False** | PASS (kein Check) |
| W3 (aktuell) | -60 bis 0 | 8R → 12.00 $ | True | PASS (≤ 17.08 $) |
| → Gesamt | | | | **APPROVED** — trotz 2× Ruin-Events |

Die Strategie wäre in W1 und W2 mit dem echten Account faktisch ruin-trächtig gewesen,
wurde aber deployed weil W3 zufällig ruhig war.

---

## Warum wurde Fenster 3 priorisiert? (Ursprüngliche Intention)

Der Kommentar in Zeile 97 lautet:
> `"deployment-relevant, Ruin-Filter aktiv"`

Die ursprüngliche Logik: Das aktuellste Fenster ist das relevanteste für Deployment-Entscheidungen.
Das stimmt — aber es schützt nicht vor Strategien mit historisch periodischen Ruin-Events.

---

## Implementierungsplan für V-05

### Option A: `ruin_filter: True` in allen 3 Fenstern (einfachste Lösung)

```python
WF_WINDOWS = [
    {"train_end": -480, ..., "ruin_filter": True, ...},   # war False
    {"train_end": -240, ..., "ruin_filter": True, ...},   # war False
    {"train_end": -60,  ..., "ruin_filter": True, ...},   # unverändert
]
```

**Effekt:** Eine Kombination wird abgelehnt, sobald Max-DD in *irgendeinem* der 3 Fenster > 11.4R.

**Risiko:** Fenster 1 (120d OOS) hat min_n=35 → max nur ~35 Trades.
Ein unglücklicher Drawdown in 35 Trades kann statistisch zufällig sein.
Konservativ, aber möglicherweise zu viele False Negatives.

### Option B: Fenster-spezifische Ruin-Schwellen

```python
# Fenster 1 + 2: großzügigere Schwelle (mehr statistische Streuung bei n=35)
WF_WINDOWS = [
    {"ruin_filter": True, "max_dd_r_limit": 20.0, ...},  # 20R = 30$ Schwelle
    {"ruin_filter": True, "max_dd_r_limit": 15.0, ...},  # 15R = 22.5$ Schwelle
    {"ruin_filter": True, "max_dd_r_limit": 11.4, ...},  # unverändert
]
```

Erfordert Erweiterung von `_passes_window()` um fensterspezifische `max_dd_r_limit`.

### Option C: Strenge "any window fails all" Logik (DoD V-05 Interpretation)

Die DoD lautet: *"Lab-Code prüft MaxDD in JEDEM Fenster"*.
Das entspricht Option A — gleiche Schwelle (11.4R) in allen Fenstern.

---

## Empfehlung

**Option A** — `ruin_filter: True` in allen Fenstern, gleiche Schwelle — als ersten Schritt.

Begründung:
- Entspricht exakt dem DoD-Wortlaut
- Minimaler Code-Eingriff (2 Zeilen in WF_WINDOWS)
- Kein Architektur-Umbau nötig
- Kann in einem zweiten Schritt zu Option B verfeinert werden falls False-Negative-Rate zu hoch

**Geschätzter Aufwand:** 5 Minuten Implementierung + py_compile + parity_test.

---

## Betroffene Zeilen (exakt)

| Zeile | Inhalt | Änderung |
|---|---|---|
| 92 | `"ruin_filter": False,` (W1) | → `True` |
| 96 | `"ruin_filter": False,` (W2) | → `True` |
| 100 | `"ruin_filter": True,` (W3) | unverändert |

Keine Änderung an `_passes_window()` nötig — die Logik ist bereits korrekt.
