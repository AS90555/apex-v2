# S-03 — Fix Applied: DAILY_DD_HALF_R implementiert

**Datum:** 2026-05-06  
**Roadmap-ID:** S-03 → DONE  
**Dateien:** `governance/checks.py`, `execution/executor.py`  
**py_compile checks.py:** ✅ kein Syntaxfehler  
**py_compile executor.py:** ✅ kein Syntaxfehler

---

## Patch 1 — governance/checks.py

### Import (Zeile 25)

**Vorher:**
```python
from config.settings import (
    DRAWDOWN_KILL_PCT, MIN_BALANCE_USD,
    DAILY_DD_KILL_R,
    SIGNAL_EXPIRY_MINUTES,
)
```

**Nachher:**
```python
from config.settings import (
    DRAWDOWN_KILL_PCT, MIN_BALANCE_USD,
    DAILY_DD_HALF_R, DAILY_DD_KILL_R,
    SIGNAL_EXPIRY_MINUTES,
)
```

### DailyDrawdownCheck.evaluate() — Zeilen 68–83

**Vorher:**
```python
class DailyDrawdownCheck(BaseGovernanceCheck):
    """Tages-PnL-Breaker: wenn pnl_r <= DAILY_DD_KILL_R → Stop."""

    def evaluate(self, signal: Signal) -> Tuple[bool, str]:
        daily = get_daily_pnl()
        pnl_r = daily.get("pnl_r", 0.0)
        if pnl_r <= DAILY_DD_KILL_R:
            return False, f"daily_dd_kill: pnl_r={pnl_r:.2f} <= {DAILY_DD_KILL_R}"
        return True, f"daily_pnl_r={pnl_r:.2f}"
```

**Nachher:**
```python
class DailyDrawdownCheck(BaseGovernanceCheck):
    """Tages-PnL-Breaker: zweistufig.
    pnl_r <= DAILY_DD_KILL_R (-2.0R)  → kein Trade.
    pnl_r <= DAILY_DD_HALF_R (-1.5R)  → Trade erlaubt, aber halbe Größe (HALF_SIZE-Flag).
    """

    def evaluate(self, signal: Signal) -> Tuple[bool, str]:
        daily = get_daily_pnl()
        pnl_r = daily.get("pnl_r", 0.0)
        if pnl_r <= DAILY_DD_KILL_R:
            return False, f"daily_dd_kill: pnl_r={pnl_r:.2f} <= {DAILY_DD_KILL_R}"
        if pnl_r <= DAILY_DD_HALF_R:
            return True, f"daily_dd_half: pnl_r={pnl_r:.2f} — HALF_SIZE"
        return True, f"daily_pnl_r={pnl_r:.2f}"
```

---

## Patch 2 — execution/executor.py

### Schritt 2b nach _calc_sizing() in _execute_live() (~Zeile 276)

**Vorher:**
```python
size     = sizing["size"]
leverage = sizing["leverage"]

# Schritt 3: Hebel setzen ...
```

**Nachher:**
```python
size     = sizing["size"]
leverage = sizing["leverage"]

# Schritt 2b: Daily-DD Half-Size — Governance-Flag aus governance_log lesen
_conn = get_connection()
_row = _conn.execute(
    "SELECT reason FROM governance_log WHERE signal_id=? ORDER BY ts DESC LIMIT 1",
    (signal.id,),
).fetchone()
_conn.close()
if _row and _row[0] and "HALF_SIZE" in _row[0]:
    s_dec = SIZE_DECIMALS.get(signal.asset, 2)
    size  = round(size * 0.5, s_dec)
    log(f"[EXECUTOR] Half-size wegen Daily-DD -1.5R: neue Größe {size}")

# Schritt 3: Hebel setzen ...
```

---

## Architektur-Entscheidung: Warum governance_log statt Signal-Feld?

Der Governance-Reason wird für approved Signale in `governance_log` gespeichert,
aber **nicht** im `signals`-Table (`reject_reason` bleibt `NULL` bei approved).
Die `Signal`-Dataclass enthält den Reason daher nicht zur Laufzeit im Executor.

→ Lösung: Executor liest `governance_log` per neuer Read-Connection (einmalig,
read-only, kein WAL-Konflikt). Die Connection wird sofort geschlossen.

---

## Verhalten nach Fix

| Tages-PnL | governance_log reason | Executor-Verhalten |
|-----------|----------------------|-------------------|
| > -1.5R | `daily_pnl_r=X.XX` | Volle Größe |
| -1.5R bis -1.99R | `daily_dd_half: ... — HALF_SIZE` | **Halbe Größe** |
| ≤ -2.0R | `daily_dd_kill: ...` | Kein Trade (rejected) |

---

## ⚠️ Neustart erforderlich

Bot-Prozess (PID 2135428) und run_governance.py-Cron laden die geänderten
Module beim nächsten Lauf neu. Bot-Neustart empfohlen.
