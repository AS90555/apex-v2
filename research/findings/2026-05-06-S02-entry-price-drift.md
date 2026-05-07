# S-02 — entry_price/SL-TP Drift Bug

**Datum:** 2026-05-06  
**Analyst:** executor-hardener  
**Roadmap-ID:** S-02  
**Severity:** P0 — Live-Sicherheit  
**Datei:** `strategies/generic_deployed.py`

---

## Befund

### Wo wird entry_price gesetzt?

**Backtest-Engine** (`backtest/engine.py`): Alle SIGNAL_FNS setzen `entry_price`
auf den **Close der letzten geschlossenen 1h-Kerze** zum Zeitpunkt `as_of_ts`:

```python
# engine.py — repräsentativ für alle Signal-Funktionen:
entry = c["close"]           # Zeile 216 — letzter 1h-Close
entry = current_close        # Zeile 357, 424
entry = round(cur["close"])  # Zeile 642, 661, 716, 730, ...
```

**GenericDeployedStrategy** (`generic_deployed.py`, Zeilen 146–158):
Das Signal kommt aus `signal_fn()` mit `entry_price = Signal-Close`.
Dann wird es **überschrieben** durch den aktuellsten DB-Close:

```python
# Zeile 146–158
# Aktueller Marktpreis (WS-Kerze) für Entry verwenden — nicht der 1h-alte Close
cur_row = conn.execute(
    "SELECT close FROM candles WHERE asset=? AND interval='1h' ORDER BY ts DESC LIMIT 1",
    (self._asset,),
).fetchone()

entry_price = bt_sig.entry_price           # Signal-Close (Backtest-Basis)
if cur_row and cur_row[0] and cur_row[0] > 0:
    entry_price = round(cur_row[0], dec_price)   # ← ÜBERSCHRIEBEN mit DB-Latest
```

Die Abfrage holt die **neueste** Zeile aus `candles` ohne `ts < candle_open`-Filter —
das kann die **aktuell noch formende, unvollständige Kerze** sein.

---

### Werden SL/TP-Distanzen korrekt neu berechnet?

**Ja — die Distanzen werden neu verankert.** Zeilen 161–177:

```python
# Zeile 161–177
sl_dist_orig = abs(bt_sig.entry_price - bt_sig.stop_loss)   # Distanz vom Signal-Close
tp1_dist     = abs(bt_sig.entry_price - bt_sig.take_profit_1)
tp2_dist     = abs(bt_sig.entry_price - bt_sig.take_profit_2)

# SL/TP werden relativ zum neuen (überschriebenen) entry_price berechnet:
if direction == "long":
    stop_loss     = round(entry_price - sl_dist_orig, dec_price)
    take_profit_1 = round(entry_price + tp1_dist, dec_price)
    take_profit_2 = round(entry_price + tp2_dist, dec_price)
```

SL/TP folgen dem neuen `entry_price` mit unveränderter Distanz.
Das klingt korrekt — ist es aber **nur dann**, wenn `entry_price` den
tatsächlichen Fill-Preis widerspiegelt.

---

## Das eigentliche Problem: Drei Divergenz-Quellen

### Problem 1 — DB-Latest kann formende Kerze sein (Zeile 147–150)

```python
"SELECT close FROM candles WHERE asset=? AND interval='1h' ORDER BY ts DESC LIMIT 1"
```

Kein `WHERE ts < candle_open`-Filter. Wenn der WS-Ingest eine neue (noch laufende)
Kerze in die DB geschrieben hat, bevor `as_of_ts` greift, liefert diese Abfrage
den **Close der laufenden, unvollständigen Kerze** — also einen volatilen Intra-Stunden-Preis.

Der `signal_fn`-Aufruf verwendet `as_of_ts = candle_open - 1` (Zeile 132) korrekt.
Die nachgelagerte Marktpreis-Abfrage verwendet **keinen Zeitfilter** — Inkonsistenz.

### Problem 2 — Entry-Preis-Drift erzeugt systematisch falsches R

Beispiel BTC Long, Signal-Bar-Close = 60.000 $:
- Backtest: entry=60.000, SL=59.400 → sl_dist=600, TP1=60.600
- Live (15 Min später, Preis bei 60.500):
  - entry_price = 60.500 (Marktpreis)
  - SL = 60.500 - 600 = 59.900 ← höher als Backtest-SL
  - TP1 = 60.500 + 600 = 61.100 ← höher als Backtest-TP1

**Das Risk-Reward-Verhältnis bleibt gleich** — aber der absolute SL ist verschoben.
Wenn der Markt nach Signal-Close kurzfristig auf 59.800 fiel und dann auf 60.500
zurückkam, würde der Backtest-SL (59.400) halten — der Live-SL (59.900) **nicht**.

→ **Live ist systematisch SL-empfindlicher als der Backtest.**

### Problem 3 — parity_test kann diesen Drift nicht erkennen

`parity_test.py` vergleicht `signal_fn()` Output mit `GenericDeployedStrategy` Output.
Aber der Drift entsteht erst **nach** `signal_fn()` — im Überschreibungs-Block Zeile 157–158.
Der parity_test sieht nur das fertige Signal, nicht den Ursprung des entry_price.

---

## Kommentar im Code vs. tatsächliches Verhalten

```python
# Zeile 160 (Kommentar im Code):
# SL/TP-Abstände bleiben relativ zum Signal-Close (Backtestlogik korrekt)
```

**Dieser Kommentar ist irreführend.** Die SL/TP-Distanzen werden zwar
vom Signal-Close *berechnet*, aber an einem *anderen Ankerpunkt* (Marktpreis)
angewendet. Das ist **nicht** dasselbe wie "Backtestlogik korrekt".

---

## Bewertung: Bug oder Design-Entscheidung?

Die Intention (Kommentar Zeile 146) ist explizit: "Aktueller Marktpreis für Entry
verwenden, nicht der 1h-alte Close." Das ist eine bewusste Entscheidung — der
tatsächliche Fill-Preis soll näher am Markt sein.

**Das zugrundeliegende Konzept ist vertretbar.** Der Bug liegt in der Umsetzung:

1. Die DB-Abfrage filtert nicht auf geschlossene Kerzen → kann formende Kerze liefern
2. Die SL/TP-Verschiebung ist im Backtest nicht reproduzierbar →
   Live-Performance und Backtest-Performance sind nicht vergleichbar

---

## Vorgeschlagene Fixes (noch nicht implementiert)

### Fix A — Minimal: DB-Abfrage auf geschlossene Kerze beschränken

```python
# Zeile 147–150 — ERSETZEN durch:
cur_row = conn.execute(
    "SELECT close FROM candles WHERE asset=? AND interval='1h' AND ts < ? ORDER BY ts DESC LIMIT 1",
    (self._asset, candle_open),   # candle_open bereits auf Zeile 131 berechnet
).fetchone()
```

Stellt sicher: nur vollständig geschlossene Kerzen als Marktpreis-Basis.
**Behebt Problem 1.** Probleme 2 und 3 bleiben bestehen.

### Fix B — Sauber: entry_price == Signal-Close (parity_test-kompatibel)

```python
# Zeile 155–158 — ERSETZEN durch:
entry_price = bt_sig.entry_price  # Signal-Close — identisch zum Backtest
# Kein Überschreiben. SL/TP bleiben Backtest-identisch.
```

Konsequenz: `parity_test.py` kann jetzt echte Parity prüfen.
Nachteil: Entry-Preis kann beim Order-Fill abweichen (Slippage schon in Kosten modelliert).

### Fix C — Hybrid: Marktpreis nur als Warnung, nicht als Override

```python
# Marktpreis zur Drift-Überwachung loggen, NICHT als entry_price setzen:
if cur_row and cur_row[0] and cur_row[0] > 0:
    market_price = round(cur_row[0], dec_price)
    drift_pct = abs(market_price - bt_sig.entry_price) / bt_sig.entry_price * 100
    if drift_pct > 0.5:
        log(f"[{self._key}] DRIFT WARNING: Signal-Entry={bt_sig.entry_price}, "
            f"Market={market_price}, Drift={drift_pct:.2f}%")
entry_price = bt_sig.entry_price  # Signal-Close bleibt Basis
```

---

## DoD (aus Roadmap S-02)

```bash
python tests/parity_test.py
# Muss grün sein: live entry_price == backtest entry_price für gleiche Bar
```

Aktuell würde parity_test bei aktivem Marktpreis-Override **rot** werden,
da der Override nur bei Live-Zeitpunkt angewendet wird, nicht im Test.

---

## Empfehlung

**Fix A als Sofortmaßnahme** (verhindert formende Kerze als Entry-Basis).  
**Fix B oder C als Hauptfix** — Entscheidung durch User:

- Fix B: Maximale Backtest-Parity, einfacher Code, Slippage ist bereits in Kosten
- Fix C: Markt-Monitoring ohne Entry-Drift, mehr Transparenz, kein Parity-Bruch

**Freigabe durch User erforderlich** (strategies/ berührt Live-Signal-Generierung).
