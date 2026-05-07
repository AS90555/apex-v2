# APEX V2 — Modulares Quant-Trading-System

Ein vollständig automatisiertes Algorithmic-Trading-System für Bitget Futures (Micro-Account, USDT-M Perpetuals). Gebaut auf einer sauberen Daten-Pipeline, zentralem Feature-Layer, separatem Governance-Gate und einem selbstlernenden Research-Lab.

> **Status:** Aktiver Live-Betrieb. SOL läuft live, BTC/XRP im Paper-Trading (Dry-Run), weitere Assets im Shadow-Monitoring.

---

## Aktueller Betriebsstatus (Stand: Mai 2026)

### OOS-Backtest-Ergebnisse (Walk-Forward, Brutto ohne Kosten)

| Asset | Modus | Strategie | MS | OOS-PF (Brutto) | Netto-PF (nach Kosten) | WR | Regime |
|-------|-------|-----------|-----|-----------------|------------------------|----|--------|
| SOL | 💰 **LIVE** | donchian_breakout | 22.8 | 2.37 | 1.78 | 64% | SIDEWAYS |
| XRP | ⚙️ Dry-Run | inside_bar_breakout | 21.7 | 2.40 | 1.52 | 66% | SIDEWAYS |
| BTC | ⚙️ Dry-Run | donchian_breakout | 12.6 | 1.96 | — | 57% | SIDEWAYS |
| LINK | 👁️ Shadow | donchian_breakout | 26.7 | 2.51 | 1.44 | 63% | SIDEWAYS |
| AVAX | 👁️ Shadow | donchian_breakout | 22.6 | 2.60 | 1.39 | 68% | SIDEWAYS |
| ADA | 👁️ Shadow | donchian_breakout | 31.7 | 2.85 | 1.48 | 70% | SIDEWAYS |
| ETH | ⏸️ Pausiert | — | — | — | — | — | — |

> **OOS-PF (Brutto):** Backtest-Ergebnis ohne Slippage, Fees und Funding.
> **Netto-PF (nach Kosten):** Backtest mit ROUND_TRIP=0.18% + Funding=0.01%/8h (Schätzwert, 180-Tage-Fenster).
> BTC-Netto-PF noch nicht berechnet (Deployment zu neu).

### Live Track Record

> **Live seit 2026-05-06** — Track Record noch < 30 Trades. OOS-Zahlen sind Backtest-Prognosen, kein Live-Ergebnis.
> Drift-Monitor (tägl. 06:00 UTC) vergleicht Live-PF mit OOS-PF — Auto-Pause bei Einbruch > 30%.

Alle Deployments basieren auf Lab-Discoveries mit `cooldown_bars=8` (8h Mindestabstand zwischen Trades) und bestandenem Walk-Forward-OOS-Test (3 Fenster, gesamt n ≥ 100).

---

## Architektur

```
Cron (alle 5 Min.) → master_run.py (sequenziell, kein Subprocess-Overhead)
    │
    ▼
[Data Intake]  ←── Bitget WebSocket (Live) + Binance REST (History)
    │                7 Assets × 3–4 Intervalle → SQLite candles-Tabelle
    ▼
[Feature Registry]  ←── EMA, ATR, Bollinger, Volume-SMA, Regime-Detektor
    │                    Ergebnisse gecacht in features-Tabelle
    ▼
[Strategy Layer]  ←── 10 Strategien generieren Signale via GenericDeployedStrategy
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
[Telegram Bot]  ←── Vollständiges Dashboard, Portfolio Manager,
                     Lab-Screens, Deploy-Buttons, API-Diagnose
```

Parallel dazu laufen dauerhaft:

```
[intake_ws.py]     ←── Bitget WebSocket-Verbindung (Candle-Stream, 24/7)

[Auto-Lab Daemon]  ←── Walk-Forward-Optimierung über 730 Tage History
    │                   70 Kombinationen (10 Strategien × 7 Assets)
    │                   Monte-Carlo + Grid-Search, Ruin-Filter, WR-Filter
    │                   cooldown_bars=8 in jedem Backtest
    ▼
[lab_discoveries]  ←── Validierte Setups (OOS-PF ≥ 1.30, WR ≥ 48%, n ≥ 40)
    ▼
[GenericDeployedStrategy]  ←── Universeller Live-Signal-Generator für alle
                                Lab-Discoveries ohne separate Strategy-Datei
    ▼
[Autopilot]        ←── Regime-Wechsel → automatisches Deploy des besten Setups
```

---

## Asset-Universum & Intervalle

| Asset | Intervalle | Bemerkung |
|-------|-----------|-----------|
| BTC | 5m, 15m, 1h | Basisasset, liquidester Markt |
| ETH | 5m, 15m, 1h, 4h | Pausiert nach Cooldown-Rebacktest |
| SOL | 5m, 15m, 1h | Erstes Live-Asset |
| XRP | 5m, 15m, 1h | Dry-Run: inside_bar_breakout |
| ADA | 5m, 15m, 1h | Shadow-Monitoring |
| LINK | 5m, 15m, 1h | Shadow-Monitoring |
| AVAX | 5m, 15m, 1h | Shadow-Monitoring |

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
| `dual_donchian` | Langer Kanal für Entry, kurzer Kanal für Exit + ATR-Filter | Long & Short |
| `bb_kc_squeeze` | Squeeze wenn BB-Breite < KC-Breite, Signal bei Release + Momentum | Long & Short |
| `supertrend` | 3× Supertrend mit verschiedenen Parametern, Signal nur bei Richtungswechsel aller 3 | Long & Short |

Jedes Backtest-Setup durchläuft:
1. **Train-Phase** (70% des 2J-Fensters) — Parameter-Optimierung
2. **Test-Phase / OOS** (30%) — Validierung, nur dieser Zeitraum zählt
3. **Cooldown-Simulation** (`cooldown_bars=8`) — 8h Mindestabstand zwischen Trades im Backtest

---

## Risk-Management

| Parameter | Wert | Bedeutung |
|-----------|------|-----------|
| `RISK_USDT` | $1.50 | Festes Risiko pro Trade |
| `MIN_NOTIONAL` | $5.00 | Bitget Mindest-Ordervolumen |
| `MAX_LEVERAGE` | 20× | Hartes Hebel-Limit |
| `DRAWDOWN_KILL_PCT` | 50% | Kill-Switch ab 50% Drawdown vom HWM |
| `DAILY_DD_HALF_R` | −1.5R | Tages-DD: halbe Position ab hier |
| `DAILY_DD_KILL_R` | −2.0R | Tages-DD-Stopp: kein weiterer Trade |
| `MIN_WR_TEST` (Lab) | 48% | Mindest-Win-Rate im OOS-Fenster |
| `MIN_PF_TEST` (Lab) | 1.30 | Mindest-Profit-Factor OOS |
| `MAX_DRAWDOWN_PERCENT` (Lab) | 25% | Ruin-Filter: Max. Kontoeinbruch |
| `cooldown_bars` (Lab) | 8 | Mindest-Bars zwischen Trades im Backtest |

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
| `shadow` | Signal wird geloggt, nie ausgeführt — reines Monitoring |
| `dry_run` | Paper-Trade: Signal wird simuliert und getrackt, kein API-Call |
| `live` | Echter Order an Bitget |

Upgrade-Pfad: Shadow → Dry-Run (manuell per Telegram) → Live (manuell nach Dry-Run-Bestätigung).

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
│   ├── autopilot.py         # Regime-Switching, Deploy-Logik
│   └── utils.py             # Logging, Zeitzone
├── intake/
│   └── intake_ws.py         # Bitget WebSocket → SQLite (Live-Feed, dauerhaft)
├── features/
│   ├── indicators.py        # EMA, ATR, RSI, Bollinger, VWAP, Regime
│   └── registry.py          # Feature-Cache-Layer
├── strategies/
│   ├── base.py              # BaseStrategy ABC
│   └── generic_deployed.py  # Universeller Live-Signal-Generator für Lab-Discoveries
├── governance/
│   ├── checks.py            # Einzelne Risk-Checks als Funktionen
│   └── gate.py              # Orchestrierung → approved/rejected
├── execution/
│   ├── executor.py          # Einziger Order-Sender
│   └── bitget_client.py     # Bitget REST API Client
├── backtest/
│   └── engine.py            # Alle 10 Strategien als Backtest-Signalgeneratoren (SIGNAL_FNS)
├── research/
│   └── auto_lab_daemon.py   # Walk-Forward-Lab (läuft dauerhaft, 70 Kombinationen)
├── monitor/
│   ├── position_monitor.py  # Break-Even, Exit-Tracking
│   ├── heartbeat.py         # Systemgesundheit
│   └── telegram_bot.py      # Vollständiger Bot (Dashboard, Lab, Portfolio Manager)
├── api/
│   └── server.py            # Read-only REST API (Port 8890)
├── scripts/
│   ├── run_features.py      # Pipeline-Schritt: Features + Regime berechnen
│   ├── run_strategies.py    # Pipeline-Schritt: Signale generieren
│   ├── run_governance.py    # Pipeline-Schritt: Signale prüfen
│   ├── run_execution.py     # Pipeline-Schritt: Approved Signale ausführen
│   ├── run_monitor.py       # Pipeline-Schritt: Positionen überwachen
│   └── master_run.py        # Haupt-Runner: alle Schritte sequenziell in einem Prozess
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
| `asset_requests` | Vom Nutzer angefragte neue Assets für das Lab |

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

### Datenbank initialisieren

```bash
python3 -c "from core.db import run_migrations; run_migrations()"
```

### Historische Daten laden (einmalig)

```bash
python3 scripts/fetch_binance_history.py   # 730 Tage × 7 Assets × 1h
```

### Dauerhaft laufende Prozesse starten

```bash
# WebSocket Intake (Candle-Stream, 24/7)
nohup python3 intake/intake_ws.py >> logs/intake_ws.log 2>&1 &

# Telegram Bot
nohup python3 monitor/telegram_bot.py >> logs/telegram_bot.log 2>&1 &

# Auto-Lab (dauerhaft neue Setups suchen)
nohup python3 research/auto_lab_daemon.py >> logs/lab_daemon.log 2>&1 &
```

### Crontab

```cron
# APEX V2 — Master-Pipeline alle 5 Minuten (sequenziell, kein sleep-Hack)
*/5 * * * *  cd /root/apex-v2 && python3 scripts/master_run.py >> logs/master.log 2>&1

# API-Server (nur starten wenn nicht läuft)
5 4 * * *    pgrep -f "api/server.py" || (cd /root/apex-v2 && nohup python3 api/server.py >> logs/api.log 2>&1 &)
```

---

## Telegram-Bot

Der Bot bietet ein vollständiges Interface über Inline-Buttons ohne manuelle Befehle eingeben zu müssen.

| Screen | Inhalt |
|--------|---------|
| 📊 Überblick | Performance, aktive Strategien, Fortschrittsbalken, Markt-Wetter |
| 💰 Strategien | Verwaltung aktiver Deployments, Modus-Wechsel |
| 🏆 Top Setups | Beste Lab-Discoveries nach Micro-Score, Deploy-Buttons |
| 📂 Offene Trades | Laufende Positionen mit Live-PnL |
| 🔬 Labor | Lab-Status, Suchraum, Lernkurve, Heatmap, Funde |
| ⚙️ System | Heartbeats aller Komponenten, Server-Ressourcen, letzte Trades |
| 📊 Portfolio Manager | Nach Asset/Strategie/Regime filtern, Regime-Fit-Check |

---

## Auto-Lab — Wie es funktioniert

```
Für jede (strategie, asset)-Kombination (70 total), ~alle 60s eine Iteration:
  1. Regime im OOS-Fenster bestimmen (EMA50-Slope + ATR-Volatilität)
  2. 20 Parameter-Sets samplen (50% Monte-Carlo, 50% Grid)
  3. Walk-Forward-Backtest mit cooldown_bars=8:
       Train (70% von 730 Tagen) → Parameter-Optimierung
       Test  (30% von 730 Tagen) → OOS-Validierung
  4. Filter-Gauntlet:
       n_test ≥ 40        (statistische Signifikanz)
       PF_test ≥ 1.30     (positiver Edge)
       WR_test ≥ 48%      (psychologisch tradebar)
       AvgR_test ≥ 0.08R  (ausreichende Effizienz)
       Ruin-Filter: Max-DD ≤ 25% des Startkapitals
       Overfit-Check: |AvgR_train - AvgR_test| ≤ 0.15R
  5. Micro-Score = PF_factor × Calmar_factor × n_factor
       PF_factor   = min(PF_test / 1.3, 3.0)
       Calmar_factor = min(AvgR / MaxDD_R / 0.20, 2.0)   ← Normierung bei 0.20
       n_factor    = min(n_test / 40, 2.0)
  6. Neuer Highscore → Telegram-Notification + DB-Eintrag
```

Validierte Setups landen in `lab_discoveries` und können per Bot oder Autopilot deployed werden.

---

## GenericDeployedStrategy

Alle Lab-Discoveries nutzen denselben universellen Signal-Generator statt separater Strategy-Dateien:

```python
# Lädt alle aktiven Deployments und nutzt SIGNAL_FNS[base_strategy] direkt
strategies = load_deployed_strategies()
# → Eine Instanz pro aktivem Deployment, dieselbe Backtest-Logik live
```

Vorteile: Jede neue Strategie im Lab wird automatisch live-fähig sobald sie in `SIGNAL_FNS` registriert ist. Keine separate Strategy-Datei nötig.

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
- **Cooldown im Backtest**: `cooldown_bars=8` stellt sicher dass Lab-Scores nicht durch Overtrading aufgebläht werden
- **Ruin-Filter im Lab**: Kein Setup mit theoretischem Kontoeinbruch > 25% wird deployed
- **Kill-Switches**: Tages-DD (−2R) und Gesamt-DD (−50% HWM) stoppen automatisch alle Trades
- **WAL-Mode**: SQLite mit Write-Ahead-Logging — parallele Reads ohne Locking
- **Signal-Deduplication**: `signal_key` (strategy + asset + session + datum) verhindert Doppel-Signale

---

## Lizenz

Privates Repository. Nicht für den produktiven Einsatz ohne eigene Due-Diligence geeignet. Krypto-Futures-Trading beinhaltet das Risiko des Totalverlusts.
