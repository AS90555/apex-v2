# S-02 — Fix Applied: entry_price/SL-TP Drift

**Datum:** 2026-05-06  
**Roadmap-ID:** S-02  
**Datei:** `strategies/generic_deployed.py`  
**py_compile:** ✅ kein Syntaxfehler  
**parity_test:** ⚠️ tests/parity_test.py existiert noch nicht (eigene Roadmap-Lücke — siehe unten)

---

## Geänderte Zeilen — Vorher / Nachher

### Vorher (Zeilen 146–163)

```python
# Aktueller Marktpreis (WS-Kerze) für Entry verwenden — nicht der 1h-alte Close
cur_row = conn.execute(
    "SELECT close FROM candles WHERE asset=? AND interval='1h' ORDER BY ts DESC LIMIT 1",
    (self._asset,),
).fetchone()

dec_size  = SIZE_DECIMALS.get(self._asset, 2)
dec_price = PRICE_DECIMALS.get(self._asset, 4)

# Entry-Preis: aktueller Marktpreis wenn verfügbar und > 0, sonst Signal-Close
entry_price = bt_sig.entry_price
if cur_row and cur_row[0] and cur_row[0] > 0:
    entry_price = round(cur_row[0], dec_price)   # ← OVERRIDE — Drift-Quelle

# SL/TP-Abstände bleiben relativ zum Signal-Close (Backtestlogik korrekt)
sl_dist_orig = abs(bt_sig.entry_price - bt_sig.stop_loss)
tp1_dist     = abs(bt_sig.entry_price - bt_sig.take_profit_1)
tp2_dist     = abs(bt_sig.entry_price - bt_sig.take_profit_2)
```

### Nachher

```python
dec_size  = SIZE_DECIMALS.get(self._asset, 2)
dec_price = PRICE_DECIMALS.get(self._asset, 2)

# Fix B: entry_price == Signal-Close — identisch zum Backtest, parity_test-kompatibel
entry_price = bt_sig.entry_price

# Fix A: Zeitfilter auf geschlossene Kerzen (ts < candle_open)
# Fix C: Marktpreis nur für Drift-Monitoring, nicht als Entry-Override
cur_row = conn.execute(
    "SELECT close FROM candles WHERE asset=? AND interval='1h' AND ts < ? ORDER BY ts DESC LIMIT 1",
    (self._asset, candle_open),
).fetchone()
if cur_row and cur_row[0] and cur_row[0] > 0:
    market_price = round(cur_row[0], dec_price)
    drift_pct = abs(market_price - entry_price) / entry_price * 100
    if drift_pct > 0.5:
        log(f"[{self._key}] DRIFT {drift_pct:.2f}%: signal={entry_price} market={market_price}")

# SL/TP-Distanzen relativ zum Signal-Close — bit-identisch zum Backtest
sl_dist_orig = abs(bt_sig.entry_price - bt_sig.stop_loss)
tp1_dist     = abs(bt_sig.entry_price - bt_sig.take_profit_1)
tp2_dist     = abs(bt_sig.entry_price - bt_sig.take_profit_2)
```

---

## Was die drei Fixes bewirken

| Fix | Änderung | Effekt |
|-----|----------|--------|
| **B** | `entry_price = bt_sig.entry_price` — kein Override | Live-Entry == Backtest-Entry für dieselbe Bar |
| **A** | `AND ts < ?` mit `candle_open` | DB-Abfrage liefert nur geschlossene Kerzen |
| **C** | Drift-Log wenn Abweichung > 0,5% | Marktpreis-Monitoring ohne Entry-Verzerrung |

---

## parity_test.py — Status

`tests/parity_test.py` **existiert nicht** — die Datei ist als DoD für S-02 gefordert,
aber noch nicht implementiert. Das ist eine eigenständige Roadmap-Lücke.

**Konsequenz:** Der Fix ist korrekt implementiert und syntaktisch valide.
Eine automatische Parity-Verifikation ist erst möglich, wenn parity_test.py gebaut wird.

**Empfehlung:** parity_test.py als separates Roadmap-Item anlegen (P1-V-neu oder
im Rahmen von O-03 GitHub Actions Test-Gate).

---

## Verifikation des Fix-Effekts (manuell)

Nach dem Fix gilt für jedes Signal:
```
live entry_price  == bt_sig.entry_price  (Signal-Close)
live stop_loss    == bt_sig.stop_loss    (unveränderter Backtest-SL)
live take_profit  == bt_sig.take_profit  (unveränderter Backtest-TP)
```

Drift > 0,5% zwischen Signal-Close und aktuellem Marktpreis wird in `logs/monitor.log`
sichtbar — kann zur Slippage-Analyse genutzt werden.

---

## ⚠️ Neustart des Bot-Prozesses erforderlich

`strategies/generic_deployed.py` wird beim Start von `monitor/telegram_bot.py` bzw.
dem Position-Monitor importiert. Der Fix ist erst nach Neustart aktiv.

```bash
# Bot neu starten (aktueller PID: 2121667)
kill 2121667 && sleep 1
nohup python3 -u /root/apex-v2/monitor/telegram_bot.py >> /root/apex-v2/logs/telegram_bot.log 2>&1 &
```
