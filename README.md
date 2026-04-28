# APEX V2 — Modulares Quant-Trading-System

Ein vollständig automatisiertes Algorithmic-Trading-System für Bitget Futures (Micro-Account, USDT-M Perpetuals). Gebaut auf einer sauberen Daten-Pipeline, zentralem Feature-Layer, separatem Governance-Gate und einem selbstlernenden Research-Lab.

> **Status:** Aktiver Forschungs- und Shadow-Betrieb. Kein Live-Trading ohne explizite Freigabe per Telegram-Bot.

---

## Architektur

```
Cron (alle 5 Min.)
    │
    ▼
[Data Intake]  ←── Bitget WebSocket (Live) + Binance REST (History)
    │                7 Assets × 3–4 Intervalle → SQLite candles-Tabelle
    ▼
[Feature Registry]  ←── EMA, ATR, Bollinger, Volume-SMA, Regime-Detektor
    │                    Ergebnisse gecacht in features-Tabelle
    ▼
[Strategy Layer]  ←── 7 Strategien generieren Signale (kein Order-Code!)
    │                  Signale → signals-Tabelle mit status='pending'
    ▼
[Governance Gate]  ←── Risiko-Checks: DD-Kill-Switch, Regime, Position offen,
    │                   Session-Limit, Signal-Expiry, Balance-Sanity
    │                   → status='approved' oder 'rejected' + Audit-Log
    ▼
[Executor]  ←── Einzige Komponente, die Orders an Bitget sendet
    │           Dynamisches Position Sizing: RISK_USDT / SL-Distanz
    │           Automatische Hebel-Berechnung (Min-Notional $5 Bypass)
    ▼
[Monitor]  ←── Break-Even SL, Exit-Tracking, Heartbeats
    │
    ▼
[Telegram Bot]  ←── Vollständiges Dashboard, CIO-Portfolio-Empfehlung,
                     Alpha-Library, Deploy-Buttons, API-Diagnose
```

Parallel dazu läuft dauerhaft:

```
[Auto-Lab Daemon]  ←── Walk-Forward-Optimierung über 730 Tage History
    │                   49 Kombinationen (7 Strategien × 7 Assets)
    │                   Monte-Carlo + Grid-Search, Ruin-Filter, WR-Filter
    ▼
[lab_discoveries]  ←── Validierte Setups (OOS-PF ≥ 1.30, WR ≥ 48%)
    ▼
[Autopilot]        ←── Regime-Wechsel → automatisches Deploy des besten Setups
```

---

## Asset-Universum & Intervalle

| Asset | Intervalle | Bemerkung |
|-------|-----------|-----------|
| BTC | 5m, 15m, 1h | Basisasset, liquidester Markt |
| ETH | 5m, 15m, 1h, 4h | Champion: Squeeze PF=1.14 OOS |
| SOL | 5m, 15m, 1h | Hohe Volatilität, gute Signaldichte |
| XRP | 5m, 15m, 1h | — |
| ADA | 5m, 15m, 1h | — |
| LINK | 5m, 15m, 1h | — |
| AVAX | 5m, 15m, 1h | — |

Historische Daten: 730 Tage via Binance (ccxt), Live-Feed via Bitget WebSocket.

---

## Strategie-Arsenal (Lab-Suchraum)

| Strategie | Logik | Richtung |
|-----------|-------|---------|
| `squeeze` | TTM Squeeze Release + EMA-Filter | Long & Short |
| `vaa` | Volume-/ATR-Expansion Reversal | Short |
| `mean_reversion` | Bollinger-Band-Extrempunkt + RSI-Überkauft/-verkauft | Long & Short |
| `vwap_bounce` | Rollender VWAP-Touch im Trendkontext + RSI-Bestätigung | Long & Short |
| `ema_pullback` | EMA200-Trend + EMA50-Pullback + Bestätigungskerze | Long & Short |
| `donchian_breakout` | N-Bar-Hoch/-Tief-Ausbruch + Volumen- und ATR-Filter | Long & Short |
| `inside_bar_breakout` | Kompressions-Setup (Inside Bar) + Trendfilter | Long & Short |

Jedes Backtest-Setup durchläuft:
1. **Train-Phase** (70% des 2J-Fensters) — Parameter-Optimierung
2. **Test-Phase / OOS** (30%) — Validierung, nur dieser Zeitraum zählt

---

## Risk-Management

| Parameter | Wert | Bedeutung |
|-----------|------|-----------|
| `RISK_USDT` | $1.50 | Festes Risiko pro Trade |
| `MIN_NOTIONAL` | $5.00 | Bitget Mindest-Ordervolumen |
| `MAX_LEVERAGE` | 20× | Hartes Hebel-Limit |
| `DRAWDOWN_KILL_PCT` | 50% | Kill-Switch ab 50% Drawdown vom HWM |
| `DAILY_DD_KILL_R` | −2R | Tages-DD-Stopp |
| `MIN_WR_TEST` (Lab) | 48% | Mindest-Win-Rate im OOS-Fenster |
| `MIN_PF_TEST` (Lab) | 1.30 | Mindest-Profit-Factor OOS |
| `MAX_DRAWDOWN_PERCENT` (Lab) | 25% | Ruin-Filter: Max. Kontoeinbruch |

Position Sizing:
```
size = RISK_USDT / |entry - stop_loss|          # Coins bei Hebel = 1
wenn notional < $5 → leverage = ceil($6 / notional)
wenn leverage > 20 → Trade abgelehnt
```

---

## Betriebsmodi

Jede Strategie/Asset-Kombination kann unabhängig konfiguriert werden:

| Modus | Verhalten |
|-------|-----------|
| `shadow` | Signal wird geloggt, nie ausgeführt |
| `dry_run` | Execution simuliert Order lokal, kein API-Call |
| `live` | Echter Order an Bitget |

Konfiguration in `config/settings.py` → `STRATEGY_MODES`.

---

## Verzeichnisstruktur

```
apex-v2/
├── config/
│   ├── settings.py          # Alle Parameter zentral
│   └── .env                 # API-Keys (gitignored)
├── core/
│   ├── db.py                # SQLite-Verbindung, WAL-Mode, Migrationen
│   ├── models.py            # Dataclasses: Signal, Trade, Candle
│   └── utils.py             # Logging, Zeitzone
├── intake/
│   └── intake_ws.py         # Bitget WebSocket → SQLite (Live-Feed)
├── features/
│   ├── indicators.py        # EMA, ATR, RSI, Bollinger, VWAP, Regime
│   └── registry.py          # Feature-Cache-Layer
├── strategies/              # Signal-Generatoren (kein Order-Code)
├── governance/
│   ├── checks.py            # Einzelne Risk-Checks als Funktionen
│   └── gate.py              # Orchestrierung → approved/rejected
├── execution/
│   ├── executor.py          # Einziger Order-Sender
│   └── bitget_client.py     # Bitget REST API Client
├── backtest/
│   └── engine.py            # Alle 7 Strategien als Backtest-Signalgeneratoren
├── research/
│   └── auto_lab_daemon.py   # Walk-Forward-Lab (läuft dauerhaft)
├── monitor/
│   ├── position_monitor.py  # Break-Even, Exit-Tracking
│   ├── heartbeat.py         # Systemgesundheit
│   └── telegram_bot.py      # Vollständiger Bot (Dashboard, Deploy, CIO)
├── api/
│   └── server.py            # Read-only REST API (Port 8890)
├── scripts/
│   ├── run_intake.py        # Cron: Kerzen holen
│   ├── run_features.py      # Cron: Features + Regime berechnen
│   ├── run_strategies.py    # Cron: Signale generieren
│   ├── run_governance.py    # Cron: Signale prüfen
│   ├── run_execution.py     # Cron: Approved Signale ausführen
│   ├── run_monitor.py       # Cron: Positionen überwachen
│   └── master_run.py        # Optionaler All-in-One-Runner
└── data/
    └── apex_v2.db           # SQLite (WAL-Mode)
```

---

## SQLite-Schema (Kurzübersicht)

| Tabelle | Inhalt |
|---------|--------|
| `candles` | Rohe OHLCV-Kerzen (30-Tage-TTL, Binance-History ohne TTL) |
| `features` | Berechnete Indikatoren, gecacht |
| `signals` | Strategie-Output: pending → approved/rejected → executed |
| `governance_log` | Vollständiger Audit-Trail jeder Risk-Entscheidung |
| `trades` | Ausgeführte Orders mit P&L und R-Wert |
| `lab_discoveries` | Validierte Strategie-Parameter aus dem Lab |
| `lab_highscores` | Bester Micro-Score je (strategy, asset, regime) |
| `active_deployments` | Aktuell laufende Lab-Setups mit Trade-Zähler |
| `system_state` | Key-Value-State: Regime, HWM, Daily-PnL |
| `heartbeats` | Systemgesundheit aller Komponenten |
| `opening_ranges` | ORB-Boxen historisiert |

---

## Setup

### Voraussetzungen

- Python 3.12+
- Bitget-Account mit Futures-API (API Key, Secret, Passphrase)
- Telegram Bot Token + Chat ID

### Installation

```bash
git clone <repo> apex-v2
cd apex-v2
pip install -r requirements.txt
```

### Umgebungsvariablen

```bash
# config/.env
BITGET_API_KEY=...
BITGET_SECRET_KEY=...
BITGET_PASSPHRASE=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
APEX_V2_API_TOKEN=...    # Für die Read-only REST API
```

```bash
source config/.env   # oder in Shell-Profil eintragen
```

### Datenbank initialisieren

```bash
python3 -c "from core.db import run_migrations; run_migrations()"
```

### Historische Daten laden (einmalig)

```bash
python3 scripts/fetch_binance_history.py   # 730 Tage × 7 Assets × 1h
```

### Live-Feed starten

```bash
# WebSocket Intake (im Hintergrund)
nohup python3 intake/intake_ws.py >> logs/intake_ws.log 2>&1 &

# Telegram Bot
nohup python3 monitor/telegram_bot.py >> logs/telegram_bot.log 2>&1 &

# Auto-Lab (dauerhaft recherchieren)
nohup python3 research/auto_lab_daemon.py >> logs/lab_daemon.log 2>&1 &
```

### Crontab (empfohlen)

```cron
# APEX V2 — alle 5 Minuten, versetzt
*/5 * * * *  python3 /root/apex-v2/scripts/run_intake.py    >> /root/apex-v2/logs/intake.log 2>&1
*/5 * * * *  sleep 20 && python3 /root/apex-v2/scripts/run_features.py  >> /root/apex-v2/logs/features.log 2>&1
*/5 * * * *  sleep 40 && python3 /root/apex-v2/scripts/run_strategies.py >> /root/apex-v2/logs/strategies.log 2>&1
*/5 * * * *  sleep 60 && python3 /root/apex-v2/scripts/run_governance.py  >> /root/apex-v2/logs/governance.log 2>&1
*/5 * * * *  sleep 80 && python3 /root/apex-v2/scripts/run_execution.py   >> /root/apex-v2/logs/execution.log 2>&1
*/5 * * * *  sleep 100 && python3 /root/apex-v2/scripts/run_monitor.py   >> /root/apex-v2/logs/monitor.log 2>&1
```

---

## Telegram-Bot Befehle

| Befehl | Funktion |
|--------|---------|
| Dashboard | Systemstatus, offene Positionen, heutige P&L |
| Alpha Setups | Lab-Discoveries sortiert nach Micro-Score |
| 💼 Portfolio Empfehlung | CIO-Modus: bestes Setup je Asset/Regime, Deploy-Buttons |
| ⚙️ Status | Heartbeats aller Komponenten |
| 🔌 API Test | Bitget-Verbindung testen, Balance + Contract Limits |
| `/deploy <ID>` | Setup aus der Alpha-Library deployen |
| `/portfolio` | CIO-Portfolio-Übersicht |

---

## Auto-Lab — Wie es funktioniert

```
Für jede (strategie, asset)-Kombination alle ~60s:
  1. Regime im OOS-Fenster bestimmen (EMA50-Slope + ATR-Volatilität)
  2. 20 Parameter-Sets samplen (50% Monte-Carlo, 50% Grid)
  3. Walk-Forward-Backtest: Train (70%) → Test (30%)
  4. Filter-Gauntlet:
       n_test ≥ 40        (statistische Signifikanz)
       PF_test ≥ 1.30     (positiver Edge)
       WR_test ≥ 48%      (psychologisch tradebar)
       AvgR_test ≥ 0.08R  (ausreichende Effizienz)
       Ruin-Filter: Max-DD ≤ $14 (25% von $56 Startkapital)
       Overfit-Check: |AvgR_train - AvgR_test| ≤ 0.15R
  5. Micro-Score = PF / (MaxDD_USDT / Startkapital)
     → Belohnt hohen Edge bei minimalem Kontorisiko
  6. Neuer Highscore → Telegram-Notification + DB-Eintrag
```

Validierte Setups landen in `lab_discoveries` und können per Bot oder Autopilot deployed werden.

---

## Autopilot (Regime-Switching)

Erkennt automatisch Regime-Wechsel (TREND_UP / TREND_DOWN / SIDEWAYS) und deployed das beste bekannte Setup für das neue Regime — mit Cooldown (6h) und Duplikat-Schutz.

```
Regime-Wechsel erkannt
    → best_setup_for(asset, new_regime)  [PF ≥ 1.30, n ≥ 40]
    → deploy_discovery(id, mode='dry_run')
    → Telegram-Push
```

---

## Sicherheitsdesign

- **Kein Schreiben über API**: Die REST API (Port 8890) ist vollständig read-only
- **Idempotente Execution**: `UPDATE ... WHERE status='approved'` atomar — kein Doppel-Trade möglich
- **Shadow-First**: Jede neue Strategie startet im `shadow`-Modus, manueller Schritt zu `dry_run` / `live`
- **Ruin-Filter im Lab**: Kein Setup mit theoretischem Kontoeinbruch > 25% wird deployed
- **Kill-Switches**: Tages-DD (−2R) und Gesamt-DD (−50% HWM) stoppen automatisch alle Trades
- **WAL-Mode**: SQLite mit Write-Ahead-Logging — parallele Reads (Dashboard, Monitor) ohne Locking

---

## Lizenz

Privates Repository. Nicht für den produktiven Einsatz ohne eigene Due-Diligence geeignet. Krypto-Futures-Trading beinhaltet das Risiko des Totalverlusts.
