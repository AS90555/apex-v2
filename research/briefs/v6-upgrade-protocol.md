# APEX-V2 — Quant-Upgrade-Protokoll v6
**Zweck:** Vollständige Arbeitsgrundlage für Claude Code Plan-Modus.
**Regel:** Nicht sofort implementieren — erst vollständigen Plan erstellen.

---

## BEREITS ERLEDIGT (nicht nochmal anfassen)
- systemd Units (7 aktiv)
- Telegram fail-CLOSED
- DSR als Ranking-Metrik
- Gaussian HMM 3-State
- Optuna TPE (20/50 Trials)
- restic Backups
- GitHub Actions CI
- Governance: DailyDD-Half, Regime-Half, OpenRisk
- parity_test.py (13 PASS)
- SQLite WAL-Mode (basic)

---

## TEIL A — Kritische Code- und Logikfehler

### A.1 Backtest-Exit-Priorität + GBM Intrabar-Pfadsimulation
**Problem:** _simulate_exit() in backtest/engine.py verarbeitet nur
high/low/close der Einzelkerze. Statische if/elif/else-Kette erzwingt
SL vor TP2 vor TP1 — unabhängig vom tatsächlichen Preisverlauf.
**Wirkung:** Verzerrte Winrate, verzerrter Total-R, unzuverlässige
Drift-Vergleiche.

Zweistufige Lösung:
Stufe 1 (1m-Zoom): Wenn 1m-Daten vorhanden → deterministisch in
1m-Kerzen hineinzoomen.
Stufe 2 (GBM-Fallback): GBM mit Asset-spezifischen μ/σ aus letzten
N_CALIBRATION_CANDLES. Skewness-Korrektur historischer Intrabar-Schiefe.
n_paths=500 (GBM_N_PATHS). P(SL) und P(TP) als Pfad-Anteile.

Neue Felder: intrabar_model TEXT in lab_discoveries.
Config: GBM_N_PATHS, N_CALIBRATION_CANDLES in settings.py.
Tests: GBM vs 1m-Zoom vs statisch auf identischen Testkerzen.

### A.2 Partielle TP-Logik fehlt im Backtest
**Problem:** Bei TP1-Treffer bucht engine.py gesamten Trade aus.
Kein 50%-Partial-Exit, kein BE-Stop-Nachzug, keine Restposition.
BtTrade erweitern um: tp1_hit, remaining_size, realized_pnl_tp1,
be_sl_active.
Partial-Exit engine-seitig simulieren inkl. BE-Stop nach TP1.

### A.3 Strategien — inkonsistente Zieldefinitionen
**Problem:** _vaa_signal() und _kdt_signal() hart auf direction=short.
_squeeze_signal() und _asian_fade_signal() setzen TP1=TP2.
Fix: Signal-Parität Live-Adapter vs Backtest-Adapter erzwingen.
Paritäts-Test: identische Candle-Daten → identische Richtung/Levels/Size.

### A.4 Indikator-Implementierung — fehlende Bessel-Korrektur
**Problem:** stdev() in features/indicators.py nutzt Populationsformel
(/ period statt / (period-1)). Systematisch zu schmale Bollinger-Bänder.
Fix: / (period - 1) in stdev(). Alle Backtests danach neu rechnen.

### A.5 Promotion-Gates statistisch zu schwach
**Problem:** Kein DSR-Mindestgate, kein MaxDD-Gate.
Fix:
- DSR-Gate: dry_run >= 0.50, live >= 0.65
- MaxDD-Gate: MaxDD <= 30%
- Calmar-Ratio und Stabilitätsscore als Pflichtfelder
- Promotion nur nach mindestens einem echten OOS-Fold

### A.6 Governance taktgebunden + Stale-Data Watchdog
**Problem:** Keine Frische-Prüfung der Kerzendaten vor Signalfreigabe.
Fix: Stale-Data Watchdog im Governance Gate.
Letztes Kerzen-Update > STALE_CANDLE_TOLERANCE_SECONDS →
Signal mit reject_reason=stale_market_data blockieren.
Neues Feld reject_reason TEXT in signals-Tabelle.

---

## TEIL B — Architekturbedingte Schwachstellen

### B.2 SQLite Connection Hardening + Research-Daemon-Isolation + Atomares Staging

**Connection-Template (überall in core/db.py):**
isolation_level="IMMEDIATE", busy_timeout=5000, WAL, synchronous=NORMAL

**Research-Daemon-Isolation:**
- Research-Daemons lesen Candles via Read-Only-URI:
  sqlite3.connect(f"file:{DB_PATH}?mode=ro&immutable=1", uri=True)
- Research-Writes gehen in research_staging.db (nie direkt in Haupt-DB)
- Batch-Writes am Ende jedes Trial-Blocks (nicht pro Trial)

**Atomares Staging-Protokoll (research_staging.db → Haupt-DB):**
1. Alle pending Discoveries aus research_staging.db lesen
2. Integritätsprüfung pro Discovery:
   a. DSR-Wert vorhanden und >= DSR_MIN_DRY_RUN?
   b. PBO-Wert vorhanden und <= PBO_MAX?
   c. oos_folds_n >= 1?
   d. backtest_funding_model = dynamic?
   e. intrabar_model != static?
3. Nur bei vollständig bestandener Prüfung:
   → Haupt-DB öffnen via isolation_level=IMMEDIATE
   → INSERT OR IGNORE (Idempotenz)
   → Commit
4. Fehlgeschlagen → status=rejected_integrity + Alert

**Idempotenz-Constraint:**
CREATE UNIQUE INDEX idx_lab_disc_idempotent
ON lab_discoveries(strategy_id, framework_version, re_evaluated_at);

### B.3 Walk-Forward und Overfitting
- Walk-Forward-Engine mit IS/OOS-Folds, Purge/Embargo
- PBO als Pflichtgate
- Monte-Carlo-Resampling
- Mindestdauer Shadow/Paper vor Live

### B.4 Governance Gate
- Pre-Trade-Risikocheck: Stop × Size × Leverage vs Risikobudget
- Korrelations-Exposure-Limits
- Soft-Kill / Hard-Kill formal trennen
- Stale-Data Watchdog (A.6) integrieren
- Funding-Rate-Check (C.17) integrieren

### B.5 Executor — fehlertolerante Execution + deterministische clOrdId
**Problem:** Bei API-Fehler bleibt Signal in processing.
Kein deterministisches clOrdId vor API-Call.

**clOrdId-Schema:**
Entry:  APEX-V2-SIG-{signal.id}-E1
TP1:    APEX-V2-SIG-{signal.id}-TP1
TP2:    APEX-V2-SIG-{signal.id}-TP2
SL:     APEX-V2-SIG-{signal.id}-SL
Retry:  APEX-V2-SIG-{signal.id}-E1-R1

Retry mit exponentiellem Backoff (max 3, dann reconcile_required).
Circuit-Breaker: 3 API-Fehler → Soft Kill für Asset.

### B.6 Positionsmanagement — Constant Volatility Targeting
Size = (Kapital × Ziel-Risiko-%) / ATR(n)
Regime-Multiplikator:
  TREND:    1.0
  SIDEWAYS: 0.75
  HIGH_VOL: 0.50
  Undefined: 0.25
Config: REGIME_SIZE_MULTIPLIERS in settings.py.

### B.7 CI/CD
- Dockerisierte Sandbox + Bitget Testnet
- PR-basierte Konfigurationsänderungen
- Chaos Engineering automatisiert in CI (C.18)

---

## TEIL C — Quant-Grade-Erweiterungen

### C.1 Purged/Embargoed Cross-Validation + Zero-Leakage Feature-Puffer
**Problem:** Normale Cross-Validation erzeugt Leakage.
Langlaufende EMAs kontaminieren OOS-Fenster-Ränder.

**Walk-Forward-Engine (backtest/walk_forward.py):**
- Purge-Gap: mindestens längste Lookback-Periode der Features
- Embargo-Gap: mindestens durchschnittliche Trade-Haltedauer

**Zero-Leakage Feature-Puffer in features/feature_agent.py:**
def _load_candles(self, asset, start, end, embargo_mode=False):
    if embargo_mode:
        max_lookback = self._compute_max_lookback()
        # Lade max_lookback zusätzliche Kerzen VOR start
        # Schneide Ausgabe am exakten start-Timestamp ab
        # Indikatorwerte korrekt berechnet, kein Look-Ahead

max_lookback dynamisch aus Feature-Konfiguration (nie fix).
Walk-Forward setzt embargo_mode=True für alle OOS-Folds automatisch.
Unit-Test: OOS-Fold mit 200-Perioden-EMA → erster Wert korrekt.

### C.2 CPCV / CSCV
- CPCV optional als strengeres Validierungsprotokoll
- OOS-Performance-Verteilung (Median, 5th/95th Percentile)
- Breite OOS-Verteilung → instabil flaggen

### C.3 Deflated Sharpe Ratio (DSR) — BEREITS PARTIAL DONE
- DSR/PSR in backtest/metrics.py (vorhanden)
- Gate: dry_run >= 0.50, live >= 0.65 (noch nicht als Hard-Gate)
- Alle DSR-Werte nach Framework-Upgrade neu kalkulieren

### C.4 PBO — Probability of Backtest Overfitting
- Gate: PBO <= 0.30
- Pflichtfeld in lab_discoveries
- CSCV über alle Walk-Forward-Folds

### C.5 Monte-Carlo-Robustheit
- 1000 Permutationen pro Promotion
- Ausgabe: Median-Equity, 5th/95th-Percentile, MaxDD-Verteilung,
  Ruin-Wahrscheinlichkeit
- Dauerhaft negatives 5th-Percentile → nicht promoten

### C.6 Parameter-Stabilitäts-Checks
- Variation aller Parameter ±10%, ±20%, ±50%
- Stabilitätsscore als Pflichtfeld in lab_discoveries

### C.7 Mehrdimensionaler Composite Score
Gewichteter Score:
  OOS Sharpe, DSR, MaxDD (neg.), Stabilitätsscore,
  PBO (neg.), Live-Konsistenz, Slippage-Drift (neg.),
  Funding-Cost-Drift (neg.)
Gewichte konfigurierbar. Altes Ranking archivieren.

### C.8 Portfolio-Exposure-Engine
governance/portfolio_risk.py:
- Max Exposure pro Asset/Cluster
- Max Delta, Max Total Open Risk
- VaR-Budget inkl. Regime-Multiplikator
- Als PortfolioExposureCheck in GovernanceGate

### C.9 Kill-Switch-Hierarchie
4 Stufen: Soft Kill / Hard Kill / Volatility Kill / Manual Override
Jede Stufe in system_state. Telegram-Alarm bei jedem Übergang.

### C.10 Execution State Machine
Zustände:
created → sent → acked → partially_filled → filled
→ cancel_pending → canceled
→ error → reconcile_required

State-Transitions in execution_audit_log.
clOrdId MUSS vor API-Call gesetzt sein.

### C.11 Order-Reconciliation nach jedem Executor-Lauf
Exchange-State vs DB-State nach jedem Lauf.
Differenzen → Alert + reconcile_required.

### C.12 Research/Live-Datentrennung (Zielarchitektur)
- Market Data Store (read-only für Live)
- Research DB (nur Research)
- Live Execution Ledger (nur Live)
- System State (gemeinsam)
Übergang: Read-Only-Verbindungen + research_staging.db
→ physische Trennung → PostgreSQL (langfristig)

### C.13 Autonomer Reconciliation Daemon
scripts/run_reconciliation.py — minütlich, unabhängig vom Executor:
- Exchange: alle offenen Positionen/Orders
- DB: alle Trades mit status=executed
- Reale Position ohne DB → Hard Kill + Alert + reconcile_required
- DB-Position ohne Exchange → Alert + reconcile_required
- Größenabweichung > Toleranz → Alert + reconcile_required
- Sauber → Heartbeat reconciliation_ok
WICHTIG: Sendet NIEMALS selbst Orders.

### C.14 Implementation Shortfall Tracking
Neue Spalten in trades:
  signal_price REAL
  fill_price REAL
  slippage_bps REAL
  slippage_measured_at TEXT

run_slippage_monitor.py:
Median-Slippage letzte 20 Trades → Vergleich mit Annahme.
Bei Drift > SLIPPAGE_ALERT_THRESHOLD_BPS →
  deployment_status=shadow + Alarm.

### C.15 Dynamic Funding Reconciliation
Neue Tabelle funding_rates:
  asset, funding_rate, funding_time, created_at

Backtest: Point-in-Time Funding statt FUNDING_8H.
funding_cost_actual in trades-Tabelle.

### C.16 Pre-Trade Market Impact Guard + dynamische IOC-Slippage-Matrix
Neue Tabelle asset_liquidity_metrics:
  asset, avg_spread_bps, avg_depth_level1_usd,
  avg_depth_level3_usd, liquidity_score, measured_at

Stufe 0: Liquiditätsmatrix täglich aus Bitget-Orderbuch.
Stufe 1: Orderbuch-Snapshot vor Order.
Stufe 2: Adaptive IOC-Toleranz:
  IOC_tolerance = f(static_base, liquidity_score_24h, regime)
  Bei Liquiditätsdegradierung < THRESHOLD:
    IOC_tolerance *= LIQUIDITY_STRESS_MULTIPLIER
  Bei Stale-Daten → WORST_CASE_TOLERANCE (nie liberaler)
Stufe 3: Order-Ausführung:
  Größe <= MARKET_IMPACT_THRESHOLD × Level1-Volumen → Market
  Größe > Threshold → IOC-Limit mit adaptiver Toleranz

Neue Spalten in trades:
  market_impact_check, spread_at_execution_bps,
  order_type_used, ioc_fill_ratio,
  ioc_tolerance_used_bps, liquidity_score_at_execution

### C.17 Funding Rate als Live-Governance-Signal
FUNDING_RATE_WARN_THRESHOLD (z.B. 0.05% per 8h):
  → Long-Signale: Warnung oder Blockierung
FUNDING_RATE_BLOCK_THRESHOLD (z.B. 0.2% per 8h):
  → Alle Signale in Richtung: blockieren
Abhängigkeit: C.15 zuerst.

### C.18 Chaos Engineering in CI/CD
5 Pflichtszenarien — kein Merge ohne bestandene Tests:
1. Netzwerk-Timeout nach place_market_order
   → Signal reconcile_required, Daemon Alert
2. Prozessabsturz nach Order-Send, vor DB-Update
   → Daemon findet Phantomposition, Hard Kill
3. Doppelter Cron-Trigger
   → Process-Lock greift, kein Doppeltrade
4. Identische clOrdId bei Retry
   → Bitget-Ablehnung korrekt verarbeitet
5. Stale Candle-Data
   → Governance blockiert, reject_reason=stale_market_data

### C.19 Dead Man's Switch — zweistufiger False-Positive-Schutz
scripts/dead_mans_switch.sh — via eigenem Cron alle 2 Minuten:

Stufe 1: Heartbeat-Datei aktuell? → OK (exit 0)
Stufe 2a: Börse erreichbar? → Bei Nein: Netzwerk-Alert, kein Kill
Stufe 2b: Prozesse noch aktiv (I/O-Stau)?
  → Wenn ja: Eskalations-Alert, Retry nach DEAD_MANS_RETRY_WAIT_SECONDS
Stufe 3: Echter Ausfall → emergency_close_all.py

Designprinzipien:
- Heartbeat-Datei auf Dateisystem (NICHT SQLite)
- emergency_close_all.py minimal, KEINE core/db.py-Abhängigkeit
- Kein automatischer Neustart ohne Admin-Eingriff
- Aktivierungstest im Testnet Pflicht vor Live-Einsatz

### C.20 Atomares Staging-Protokoll
Referenz: vollständige Spezifikation in B.2.
Abhängigkeiten:
- research_staging.db existiert und wird vom Lab befüllt
- Sync-Prozess als eigenständiger Cron/Daemon
- Integritätsprüfung vor jedem Write in Haupt-DB
- Idempotenz-Constraint verhindert Doppel-Inserts
- Sync-Ergebnis in Heartbeat überwachbar

---

## TEIL D — Legacy-Strategie-Reset

### D.1 Framework-Diskontinuität
Alle bestehenden Discoveries entstammen altem Framework:
- Statisches Intrabar-Modell
- Fehlende Partial-TP-Simulation
- Populationsformel statt Bessel-Korrektur
- Statischer Funding-Abzug
- Kein PBO, kein DSR-Mindestgate
- Kein Zero-Leakage Feature-Puffer

### D.2 Kontrollierter Schnitt (3 Stufen)
Stufe 1: Alle aktiven Strategien auf deployment_status=frozen
Stufe 2: Framework-Upgrade vollständig, alle Tests grün
Stufe 3: Re-Evaluation mit neuem Framework
  (DSR, PBO, Stabilitätsscore, Composite Score,
   GBM-Intrabar, Zero-Leakage)

### D.3 Schema-Migration v6 (vollständig)
-- lab_discoveries
ALTER TABLE lab_discoveries ADD COLUMN framework_version TEXT DEFAULT v1;
ALTER TABLE lab_discoveries ADD COLUMN dsr_value REAL;
ALTER TABLE lab_discoveries ADD COLUMN pbo_value REAL;
ALTER TABLE lab_discoveries ADD COLUMN max_drawdown REAL;
ALTER TABLE lab_discoveries ADD COLUMN calmar_ratio REAL;
ALTER TABLE lab_discoveries ADD COLUMN stability_score REAL;
ALTER TABLE lab_discoveries ADD COLUMN composite_score REAL;
ALTER TABLE lab_discoveries ADD COLUMN oos_folds_n INTEGER;
ALTER TABLE lab_discoveries ADD COLUMN re_evaluated_at TEXT;
ALTER TABLE lab_discoveries ADD COLUMN backtest_slippage_assumption REAL;
ALTER TABLE lab_discoveries ADD COLUMN backtest_funding_model TEXT DEFAULT static;
ALTER TABLE lab_discoveries ADD COLUMN intrabar_model TEXT DEFAULT static;

CREATE UNIQUE INDEX IF NOT EXISTS idx_lab_disc_idempotent
ON lab_discoveries(strategy_id, framework_version, re_evaluated_at);

-- trades
ALTER TABLE trades ADD COLUMN signal_price REAL;
ALTER TABLE trades ADD COLUMN fill_price REAL;
ALTER TABLE trades ADD COLUMN slippage_bps REAL;
ALTER TABLE trades ADD COLUMN slippage_measured_at TEXT;
ALTER TABLE trades ADD COLUMN funding_cost_actual REAL;
ALTER TABLE trades ADD COLUMN market_impact_check TEXT;
ALTER TABLE trades ADD COLUMN spread_at_execution_bps REAL;
ALTER TABLE trades ADD COLUMN order_type_used TEXT;
ALTER TABLE trades ADD COLUMN ioc_fill_ratio REAL;
ALTER TABLE trades ADD COLUMN ioc_tolerance_used_bps REAL;
ALTER TABLE trades ADD COLUMN liquidity_score_at_execution REAL;

-- signals
ALTER TABLE signals ADD COLUMN reject_reason TEXT;

-- Neue Tabellen
CREATE TABLE IF NOT EXISTS funding_rates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT NOT NULL,
    funding_rate REAL NOT NULL,
    funding_time TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime(now))
);

CREATE TABLE IF NOT EXISTS asset_liquidity_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT NOT NULL,
    avg_spread_bps REAL NOT NULL,
    avg_depth_level1_usd REAL NOT NULL,
    avg_depth_level3_usd REAL NOT NULL,
    liquidity_score REAL NOT NULL,
    measured_at TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime(now))
);

---

## TEIL E — Repository-spezifische Korrekturen

### E.1 lab_safety_bridge.py
Alle impliziten Schuldpositionen in konkrete Tests und Fixes überführen.

### E.2 lab_safety_bridge.py — Rohe SQLite-Verbindungen
Vollständig auf core.db.get_connection() umstellen.

### E.3 features/indicators.py — Performance-Bottleneck
NumPy-Fast-Path mit Referenztests. Feature-Caching prüfen.

### E.4 scripts/ — Starke Kopplung
Services mit klaren Verträgen, zentrale Statuscodes, dünne Aufrufer.

### E.5 config/settings.py — Magic Numbers
Alle neuen Parameter als Env-Variablen:
  ATR_SIZING_PERIOD, TARGET_VOLATILITY_PCT
  REGIME_SIZE_MULTIPLIERS, SLIPPAGE_ALERT_THRESHOLD_BPS
  DSR_MIN_DRY_RUN, DSR_MIN_LIVE, PBO_MAX
  FUNDING_RATE_WARN_THRESHOLD, FUNDING_RATE_BLOCK_THRESHOLD
  MARKET_IMPACT_THRESHOLD, IOC_SLIPPAGE_TOLERANCE
  WORST_CASE_TOLERANCE, LIQUIDITY_DEGRADATION_THRESHOLD
  LIQUIDITY_STRESS_MULTIPLIER, DEAD_MANS_TIMEOUT_SECONDS
  DEAD_MANS_RETRY_WAIT_SECONDS, STALE_CANDLE_TOLERANCE_SECONDS
  GBM_N_PATHS, N_CALIBRATION_CANDLES

---

## TEIL F — Test- und Qualitätssicherungsplan

### F.1 Paritätstests
Live vs Backtest: identische Inputs → identische Outputs.

### F.2 Deterministische Replays
End-to-End-Replays für feste historische Zeitfenster als Snapshot-Tests.

### F.3 Datenbanktests
- Alle Migrationen aus D.3
- isolation_level=IMMEDIATE unter simulierter Parallelität
- Read-Only-Verbindungen für Research-Daemons
- Idempotenz-Constraint: Doppel-Insert via Staging-Sync → ignoriert
- Staging-Sync mit fehlgeschlagener Prüfung → rejected_integrity

### F.4 Statistik- und Metrik-Tests
- Unit-Tests: Sharpe, Sortino, Calmar, MaxDD, DSR, PSR
- GBM-Modell: Vergleich GBM vs 1m-Zoom vs statisch
- Zero-Leakage: OOS-Fold mit 200-Perioden-EMA korrekt
- Funding-Kostenmodell gegen historische Extremphasen
- Dynamische Slippage-Matrix Stress-Szenario

### F.5 Chaos-Tests (alle 5 Szenarien aus C.18)
- Exchange-Timeout → reconcile_required
- SIGKILL nach Order → Hard Kill
- Doppelter Cron → Process-Lock
- Stale Candle → stale_market_data
- Liquiditäts-Stress → WORST_CASE_TOLERANCE

### F.6 DMS-Tests
- False-Positive (I/O-Stau) → kein Kill, Eskalations-Alert
- Echter Ausfall → Positionen geschlossen nach Timeout

---

## TEIL G — Empfohlene Umsetzungsreihenfolge (25 Phasen)

| Prio | Paket | Warum zuerst |
|---|---|---|
| 1 | DB-Layer: WAL, isolation_level=IMMEDIATE, busy_timeout, get_connection() überall | Fundament |
| 2 | Research-Daemon-Isolation: Read-Only + research_staging.db | Optuna-Write-Konkurrenz |
| 3 | Atomares Staging-Protokoll + Idempotenz-Constraint | Saubere Promotion-Pipeline |
| 4 | Process-Lock, Watchdog, Heartbeat-Datei | Basis für DMS |
| 5 | Legacy-Strategien einfrieren (frozen) | Sauberer Schnitt |
| 6 | Schema-Migration v6 (alle Felder + neue Tabellen D.3) | Pflicht vor allem anderen |
| 7 | Funding-Rate-Intake + Liquiditäts-Metrik-Intake | Datengrundlage |
| 8 | Backtest: Exit-Prio, GBM Intrabar, Partial-TP, Point-in-Time Funding | Ohne korrekten Backtest alles wertlos |
| 9 | Zero-Leakage Feature-Puffer in _load_candles() | Look-Ahead-Bias-Prävention |
| 10 | Bessel-Korrektur → alle Backtests neu | Indikator-Korrektheit |
| 11 | Walk-Forward mit Purge/Embargo | Pflicht vor DSR/PBO |
| 12 | DSR, PSR, PBO, Monte Carlo, Stabilitäts-Checks | Statistische Härtung |
| 13 | Composite Score, Promotion-Gates v2 | Belastbare Promotion |
| 14 | Legacy-Strategien re-evaluieren | Weiter oder archivieren |
| 15 | Deterministische clOrdId + Execution State Machine | Idempotente Execution |
| 16 | Autonomer Reconciliation Daemon | Unabhängige Überwachung |
| 17 | Dead Man's Switch mit False-Positive-Schutz | Letzte Verteidigungslinie |
| 18 | Slippage Tracking + Monitor | Performance-Degradierung |
| 19 | Stale-Data Watchdog im Governance Gate | Einstiege nach Verbindungsabbrüchen |
| 20 | Funding Rate als Live-Governance-Signal | Echtzeit-Sentiment |
| 21 | Constant Volatility Targeting, Portfolio-Exposure, Kill-Switch | Portfolio-Risk |
| 22 | Market Impact Guard: dynamische IOC-Slippage-Matrix | Adaptiver Liquiditätsschutz |
| 23 | Monitoring-Ausbau inkl. Staging-Sync-Überwachung | Betriebssicherheit |
| 24 | CI/CD + Chaos Engineering (alle 5 Szenarien) | Beweis dass alles funktioniert |
| 25 | NumPy-Fast-Path für Features | Erst nach vollständiger Korrektheit |

---

## TEIL H — Direkte Arbeitsanweisung an Claude Code

### Auftrag (PLAN-MODUS)
Sichte das gesamte Repository. Bestätige oder widerlege jeden
Punkt dieses Protokolls anhand des aktuellen Codes.

Erstelle ZUERST vollständigen Plan-Output:
1. Betroffene Dateien pro Punkt
2. Abhängigkeiten (Dependency Graph)
3. Risikostufe (Hoch/Mittel/Niedrig)
4. Tests pro Änderung inkl. Chaos-Szenarien
5. Implementierungsreihenfolge mit Phasengrenzen
6. Alle Schema-Migrationen (D.3)
7. Rollback-Strategie pro Phase
8. Liste bereits DONE vs OPEN Items

### Pflichtregeln
- NICHT sofort implementieren — vollständigen Plan zuerst
- Phase für Phase mit Tests und Re-Review
- Bei Änderungen an Indikatoren/Exit-Logik/Kostenmodell: alle Backtests neu
- Legacy-Strategien erst nach vollständigem Framework-Upgrade re-evaluieren
- clOrdId IMMER vor API-Call, nie danach
- Reconciliation Daemon und DMS dürfen NIEMALS selbst Orders senden
- DMS minimal halten — keine DB-Abhängigkeit
- Staging-Sync muss idempotent und transaktional sein
- Zero-Leakage: max_lookback dynamisch, nie als fixer Wert

### Fokusfragen für Plan-Session
- Wo weichen Live-, Governance- und Backtest-Logik voneinander ab?
- Wo erzeugen auto_lab_daemon.py und telegram_bot.py Write-Konkurrenzen?
- Wie groß ist der maximale Feature-Lookback über alle Indikatoren?
- Welche Assets zeigen stärkste Liquiditätsdegradierung in Risk-Off?
- Wo bleibt der Executor in processing hängen?
- Sind bestehende lab_discoveries mit strategy_id für Idempotenz-Constraint?

### Was bereits DONE ist (nicht nochmal implementieren)
- systemd 7 Units aktiv
- Telegram fail-CLOSED (11/11 Handler)
- SQLite WAL-Mode (basic)
- HMM 3-State Regime Detection
- Optuna TPE (20/50 Trials)
- restic stündlich
- GitHub Actions CI (pytest + parity_test)
- Governance: DailyDD-Half, Regime-Half, OpenRisk
- parity_test.py (13 PASS)
- Drift-Monitor (live_vs_backtest_drift)
- Auto-Promotion/Demotion Scripts
- Daily Digest 08:00
- Inline-Buttons Telegram

### Ausgabe-Format des Plans
Schreibe nach: research/state/v6-implementation-plan.md

Struktur:
## Status-Übersicht (DONE/PARTIAL/OPEN pro Item)
## Dependency-Graph (Mermaid-Diagramm)
## Phasen-Plan (Phase 1-8 mit Dateien + Tests + Rollback)
## Schema-Migrationen (SQL-Statements aus D.3)
## Risiko-Register (Hoch/Mittel/Niedrig pro Änderung)
## Geschätzter Aufwand (Sessions pro Phase)
