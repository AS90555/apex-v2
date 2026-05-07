# S-03 — DAILY_DD_HALF_R: Setting vorhanden, Logik fehlt

**Datum:** 2026-05-06  
**Analyst:** executor-hardener  
**Roadmap-ID:** S-03  
**Severity:** P0 — Live-Sicherheit  
**Dateien:** `governance/checks.py`, `config/settings.py`

---

## Befund

### config/settings.py — zwei DD-Settings definiert

```python
# settings.py, Zeilen 37–38
DAILY_DD_HALF_R = -1.5   # ← DEFINIERT, wird NIRGENDWO genutzt
DAILY_DD_KILL_R = -2.0   # ← DEFINIERT, wird in checks.py genutzt
```

### governance/checks.py — Import: nur KILL_R, kein HALF_R

```python
# checks.py, Zeilen 23–27
from config.settings import (
    DRAWDOWN_KILL_PCT, MIN_BALANCE_USD,
    DAILY_DD_KILL_R,        # ← importiert
    SIGNAL_EXPIRY_MINUTES,
)
# DAILY_DD_HALF_R wird NICHT importiert
```

### DailyDrawdownCheck (Zeilen 68–80) — binäre Logik, kein Zwischenschritt

```python
class DailyDrawdownCheck(BaseGovernanceCheck):
    """Tages-PnL-Breaker: wenn pnl_r <= DAILY_DD_KILL_R → Stop."""

    def evaluate(self, signal: Signal) -> Tuple[bool, str]:
        daily = get_daily_pnl()
        pnl_r = daily.get("pnl_r", 0.0)
        if pnl_r <= DAILY_DD_KILL_R:                           # -2.0R
            return False, f"daily_dd_kill: pnl_r={pnl_r:.2f} <= {DAILY_DD_KILL_R}"
        return True, f"daily_pnl_r={pnl_r:.2f}"
```

**Es gibt nur zwei Zustände:**
- `pnl_r > -2.0R` → `True` (Trade erlaubt, volle Größe)
- `pnl_r <= -2.0R` → `False` (kein Trade)

**Der Bereich -1.5R bis -2.0R** existiert in der Logik nicht.
`DAILY_DD_HALF_R = -1.5` hat keinen Effekt auf irgendetwas im System.

---

## Systemweite Suche: Wird DAILY_DD_HALF_R irgendwo genutzt?

```bash
grep -rn "DAILY_DD_HALF_R" /root/apex-v2 --include="*.py"
# Ergebnis: nur config/settings.py (Definition) — keine weiteren Treffer
```

**Befund:** `DAILY_DD_HALF_R` ist **totes Setting** — definiert, nie importiert, nie ausgewertet.

---

## Was passiert aktuell bei welchem DD-Level?

| Tages-PnL | Aktuelles Verhalten | Erwartetes Verhalten (DoD) |
|-----------|--------------------|-----------------------------|
| 0R bis -1.49R | Trade erlaubt, volle Größe | ✅ korrekt |
| -1.5R bis -1.99R | Trade erlaubt, **volle Größe** | ❌ sollte: halbe Größe |
| -2.0R und schlechter | Kein Trade | ✅ korrekt |

**Die Lücke:** Zwischen -1.5R und -2.0R handelt das System mit voller Positionsgröße,
obwohl das Setting eine Halbierung vorsieht. Das erhöht das Verlustrisiko in genau dem
Moment, wo der Tag bereits schlecht läuft.

---

## Wo würde die Half-Size-Logik implementiert werden müssen?

Die Half-Size-Anpassung kann an zwei Stellen greifen:

### Option A — In DailyDrawdownCheck (Governance-Ebene)
`evaluate()` gibt `True` zurück, aber verändert `signal.size` direkt.

**Problem:** `BaseGovernanceCheck.evaluate()` gibt `Tuple[bool, str]` zurück —
kein Mechanismus vorgesehen, ein modifiziertes Signal zurückzugeben.
Würde einen neuen Return-Typ oder Side-Effect auf dem Signal-Objekt erfordern.

### Option B — Als separater Check: DailyDDHalfSizeCheck
Neuer Check, der nach `DailyDrawdownCheck` läuft. Gibt immer `True` zurück,
setzt aber `signal.size *= 0.5` wenn `DAILY_DD_HALF_R <= pnl_r < DAILY_DD_KILL_R`.

**Problem:** Signal ist ein Dataclass — muss mutable sein.
Seiteneffekte in Governance-Checks sind Architektur-Abweichung.

### Option C — In generic_deployed.py / Signal-Generierung
Vor der Signal-Erzeugung den Tages-PnL prüfen und `size` halbieren.

**Problem:** Bypass der Governance-Schicht — schlechteste Trennung.

### Option D — In execution/executor.py (bevorzugt, sauberste Trennung)
Der Executor kennt den finalen `signal.size` und kann ihn vor dem Order-Submit
anpassen. Governance prüft die Regel und setzt ein Flag, Executor handelt darauf.

**Problem:** Erfordert Erweiterung des Signal-Modells oder des Governance-Outputs
um ein `size_modifier`-Feld.

---

## Vergleich: position_monitor.py hat bereits Half-Size-Logik

In `monitor/position_monitor.py` (Zeilen 112–154) gibt es bereits
eine funktionierende `half_size`-Implementierung für TP1-Teilschließungen:

```python
half_size = trade["size"] * 0.5
# → place_partial_close(asset, half_size, ...)
# → modify_sl(asset, new_sl, half_size, ...)
```

Diese Logik könnte als Vorlage für den Daily-DD-Halbierungs-Executor-Code dienen.

---

## Entscheidung: Implementieren oder Setting entfernen?

Laut DoD (S-03): *"DailyDrawdownCheck halbiert Position bei -1.5R, oder Setting weg"*

**Zwei valide Wege:**

| Weg | Aufwand | Risiko | Empfehlung |
|-----|---------|--------|------------|
| **Implementieren** (Option D via Executor) | Mittel — Signal-Modell + Executor + Check anpassen | Signal-Flow-Komplexität steigt | Wenn DD-Schutz gewünscht |
| **Setting entfernen** | Klein — 1 Zeile in settings.py löschen | Kein Schutz bei -1.5R, aber sauberer Zustand | Wenn Feature nicht gewollt |

---

## Vorgeschlagener Fix (Option D — Executor-basiert, falls Implementierung gewünscht)

**Schritt 1:** `DailyDrawdownCheck` erweitern — neuer Return-Reason-Code:
```python
if DAILY_DD_HALF_R <= pnl_r < 0:  # nur wenn negativer Tag
    if pnl_r <= DAILY_DD_HALF_R:
        return True, f"daily_dd_half: pnl_r={pnl_r:.2f} — HALF_SIZE"
```

**Schritt 2:** `execution/executor.py` vor Order-Submit prüfen:
```python
# Im governance_result reason nach "HALF_SIZE" suchen:
if "HALF_SIZE" in governance_reason:
    signal.size = round(signal.size * 0.5, dec_size)
    log(f"[EXECUTOR] Half-size wegen Daily-DD: {signal.size}")
```

**Freigabe durch User erforderlich** — Entscheidung: implementieren oder entfernen?
