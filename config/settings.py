import os

# ── Verzeichnisse ────────────────────────────────────────────────────────────
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR    = os.path.join(PROJECT_DIR, "data")
CONFIG_DIR  = os.path.join(PROJECT_DIR, "config")
LOGS_DIR    = os.path.join(PROJECT_DIR, "logs")

# ── API ──────────────────────────────────────────────────────────────────────
BITGET_API_KEY    = os.getenv("BITGET_API_KEY", "")
BITGET_SECRET_KEY = os.getenv("BITGET_SECRET_KEY", "")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Dashboard API ────────────────────────────────────────────────────────────
API_PORT         = 8890
API_BEARER_TOKEN = os.getenv("APEX_V2_API_TOKEN", "")

# ── Kapital & Risiko ─────────────────────────────────────────────────────────
CAPITAL         = 68.33
MAX_RISK_PCT    = 0.02
MIN_RR_RATIO    = 2.0
DRAWDOWN_KILL_PCT = 0.50
MIN_BALANCE_USD   = 10.0
MAX_SL_DISTANCE_PCT = 0.10

# ── Micro-Account Position Sizing ────────────────────────────────────────────
RISK_USDT          = 1.50    # Festes Risiko pro Trade in USDT
MAX_OPEN_RISK_USDT = 4.50    # Max. gebundenes Risiko aller offenen Positionen (= 3R)
MIN_NOTIONAL       = 5.0     # Bitget Mindest-Order-Volumen in USDT
TARGET_NOTIONAL    = 6.0     # Zielbetrag wenn unter MIN_NOTIONAL (etwas Puffer)
MAX_LEVERAGE       = 20      # Hartes Leverage-Limit — darüber: Trade abgelehnt

# ── Tages-DD Circuit Breaker ─────────────────────────────────────────────────
DAILY_DD_HALF_R = -1.5
DAILY_DD_KILL_R = -2.0

# ── Live-vs-Backtest Drift Monitor ───────────────────────────────────────────
DRIFT_WARNING_PCT  = -15.0   # drift < -15% → Warning (Log + Push) | Kalibriert gegen Brutto-OOS-PF (Netto ca. 15-30% niedriger)
DRIFT_CRITICAL_PCT = -30.0   # drift < -30% + n >= MIN_TRADES → Auto-Pause (shadow) | Kalibriert gegen Brutto-OOS-PF (Netto ca. 15-30% niedriger)
DRIFT_MIN_TRADES   = 30      # Mindest-Trade-Anzahl bevor Drift-Check greift

# ── FDR-Control (Benjamini-Hochberg) ─────────────────────────────────────────
BH_FDR_Q = 0.05   # FDR-Niveau q=0.05 (konservativ wegen zeitl. Korrelation der Trades)
MIN_DSR   = 0.95  # Deflated Sharpe Ratio Mindestkonfidenz (Bailey & López de Prado 2014)

# ── HMM Regime-Filter (P-02) ─────────────────────────────────────────────────
# Erlaubte Regimes pro Strategie. Fehlt ein Eintrag → alle 3 Regimes erlaubt.
# HMM_MODE = "warn"  → "block" nach 30 validierten Live-Trades (B-05)
STRATEGY_ALLOWED_REGIMES: dict[str, list[str]] = {
    "donchian_breakout":  ["TREND", "HIGH_VOL"],
    "dual_donchian":      ["TREND", "HIGH_VOL"],
    "mean_reversion":     ["SIDEWAYS"],
    "squeeze":            ["SIDEWAYS", "TREND"],
    "bb_kc_squeeze":      ["SIDEWAYS", "TREND"],
    "ema_pullback":       ["TREND"],
    "inside_bar_breakout":["TREND"],
    "supertrend":         ["TREND", "HIGH_VOL"],
    "orb":                ["TREND", "HIGH_VOL"],
    # Alle anderen (vaa, kdt, weekend_momo, asian_fade, vwap_bounce) → kein Filter
}

# ── Backtest-Kostenmodell (Bitget Richtwerte) ────────────────────────────────
TAKER_FEE    = 0.0006   # 0.06% pro Side (Taker-Order)
MAKER_FEE    = 0.0002   # 0.02% pro Side (Maker-Order)
SLIPPAGE_EST = 0.0003   # 0.03% geschätzte Markt-Slippage
FUNDING_8H   = 0.0001   # 0.01% Funding-Rate per 8h-Periode
ROUND_TRIP   = (TAKER_FEE + SLIPPAGE_EST) * 2  # 0.18% gesamt Ein+Ausstieg

# ── Backtest Intrabar-Modell (v6) ────────────────────────────────────────────
# 'static'   — High/Low-Check pro Bar (konservativ, kein Intrabar-Pfad)
# '1m_zoom'  — 1m-Kerzen wenn vorhanden, sonst GBM-Fallback
INTRABAR_MODEL         = os.getenv("INTRABAR_MODEL", "static")
GBM_N_PATHS            = 500
N_CALIBRATION_CANDLES  = 200

# ── Walk-Forward & Statistische Härtung (v6 Phase 4) ─────────────────────────
DSR_MIN_DRY_RUN   = 0.50   # Hard-Gate: DSR ≥ 0.50 für dry_run
DSR_MIN_LIVE      = 0.65   # Hard-Gate: DSR ≥ 0.65 für live
PBO_MAX           = 0.30   # Hard-Gate: PBO ≤ 0.30
STABILITY_MIN     = 0.50   # Hard-Gate: stability_score ≥ 0.50
MAX_DD_GATE       = 5.0    # Hard-Gate: |MaxDD| ≤ 5R kumulativer Drawdown im schlechtesten OOS-Fold.
                           # max_drawdown() gibt R-Einheiten (kumulativ); 5R ≈ 30% Kapital bei ~2% Sizing/Trade.
V6_STATS_ENFORCED   = os.getenv("V6_STATS_ENFORCED",   "false").lower() == "true"
V6_GATES_ENFORCED   = os.getenv("V6_GATES_ENFORCED",   "true").lower()  == "true"
# v7 Phase 2: DSR aus Block-Bootstrap statt direkter Schätzung
V7_MC_DSR_ENFORCED  = os.getenv("V7_MC_DSR_ENFORCED",  "false").lower() == "true"

# ── v7 Phase 4 — Funding-Sizing + Latenz-Slippage ───────────────────────────
FUNDING_SIZE_K  = 1.0   # Skalierungsfaktor für Funding-Drag-Adjustment in sizing.py
V7_FUNDING_SIZING = os.getenv("V7_FUNDING_SIZING", "true").lower() == "true"

# ── v7 Phase 7 — Maker-Taker-Kostendifferenzierung ──────────────────────────
# False: bisheriges ROUND_TRIP-Modell (alle Orders = Taker). True: IOC-Limit
# → Maker-Fee wenn Fill; Market-Order / Timeout → Taker-Fee.
V7_MAKER_TAKER_SPLIT = os.getenv("V7_MAKER_TAKER_SPLIT", "false").lower() == "true"

# ── v7 Phase 6 — Re-Evaluation Gate ─────────────────────────────────────────
# Initial False: bestehende Discoveries nicht automatisch demoten.
# Manuell auf True setzen (via Telegram-Workflow) nach erfolgreicher Re-Eval.
V7_REEVAL_REQUIRED  = os.getenv("V7_REEVAL_REQUIRED", "false").lower() == "true"
OOS_FOLDS_MIN_V7    = 3   # Minimum OOS-Folds für v7-Re-Eval

# v7.0 (2026-05-13): initiale Versionierung; Gewichte unverändert zu v6.
# Änderungen: PR + Changelog in backtest/composite_score.py + Re-Backtest erforderlich.
COMPOSITE_WEIGHTS_VERSION = "v7.0"
COMPOSITE_WEIGHTS = {
    "sharpe":    0.30,
    "dsr":       0.25,
    "max_dd":    0.20,
    "stability": 0.15,
    "pbo":       0.10,
}

# ── Governance v2 (v6 Phase 6) ───────────────────────────────────────────────
STALE_CANDLE_TOLERANCE_SECONDS = 900          # 15 min — Kerze älter → stale_market_data
FUNDING_RATE_WARN_THRESHOLD    = 0.0005       # 0.05% per 8h → Warning (beide Richtungen)
FUNDING_RATE_BLOCK_THRESHOLD   = 0.002        # 0.20% per 8h → Block wenn gegen Signal-Richtung
# Bitget zahlt Funding alle 8h. Nach 2h ohne Update gilt Rate als stale (fail-open: warn, kein Block).
FUNDING_RATE_STALE_MIN         = 120          # Funding-Daten älter als N Minuten → stale
ATR_SIZING_PERIOD              = 14           # ATR-Periode für Vol-Targeting
TARGET_VOLATILITY_PCT          = 0.02         # 2% Tages-Vol-Ziel
V6_VOL_TARGETING               = os.getenv("V6_VOL_TARGETING", "false").lower() == "true"
REGIME_SIZE_MULTIPLIERS: dict[str, float] = {
    "TREND":    1.0,
    "SIDEWAYS": 0.75,
    "HIGH_VOL": 0.50,
    "UNDEFINED": 0.25,
}
# Portfolio-Exposure-Grenzen
PORTFOLIO_MAX_EXPOSURE_USDT  = float(os.getenv("PORTFOLIO_MAX_EXPOSURE_USDT", "500"))
PORTFOLIO_MAX_CLUSTER_USDT   = float(os.getenv("PORTFOLIO_MAX_CLUSTER_USDT",  "800"))
PORTFOLIO_CORR_LIMIT         = float(os.getenv("PORTFOLIO_CORR_LIMIT",        "600"))
# Asset-Cluster-Mapping für Korrelations-Budget
CLUSTER_MAP: dict[str, str] = {
    "BTC": "l1_btc", "ETH": "l1_eth",
    "SOL": "l1_alt", "AVAX": "l1_alt",
    "XRP": "l1_xrp", "ADA": "l1_xrp",
    "LINK": "defi",  "AAVE": "defi",
    "DOGE": "meme",  "SUI":  "l1_alt",
}

# ── Market Impact + Liquidität (v6 Phase 7) ──────────────────────────────────
MARKET_IMPACT_THRESHOLD       = 0.10   # Order ≤ 10% von L1-Depth → Market Order
IOC_SLIPPAGE_TOLERANCE_BASE   = 5.0    # Basis-IOC-Toleranz in bps
WORST_CASE_TOLERANCE          = 25.0   # Max IOC-Toleranz bei Stale-Daten / Degradation
LIQUIDITY_DEGRADATION_THRESHOLD = 0.50 # Score < 0.5 → Stress-Modus
LIQUIDITY_STRESS_MULTIPLIER   = 2.0    # Toleranz × 2 bei Degradation
V6_MARKET_IMPACT_GUARD        = os.getenv("V6_MARKET_IMPACT_GUARD", "false").lower() == "true"

# ── Execution-Härtung (v6 Phase 5) ──────────────────────────────────────────
SLIPPAGE_ALERT_THRESHOLD_BPS  = 8      # Median-Slippage > 8bps → shadow + Alert
DEAD_MANS_TIMEOUT_SECONDS     = 300    # Heartbeat-Alter > 5min → DMS-Aktivierung
DEAD_MANS_RETRY_WAIT_SECONDS  = 120    # Wartezeit vor zweitem DMS-Check
RECONCILE_SIZE_TOLERANCE      = 0.001  # Toleranz für Positions-Größenvergleich

# ── Bitget Handelsparameter ──────────────────────────────────────────────────
LEVERAGE     = 5
MARGIN_MODE  = "isolated"

SIZE_DECIMALS = {"BTC": 4, "ETH": 2, "SOL": 1, "XRP": 0, "ADA": 0,
                 "LINK": 2, "AVAX": 1, "DOGE": 0, "SUI": 1, "AAVE": 2}
PRICE_DECIMALS = {"BTC": 1, "ETH": 2, "SOL": 3, "XRP": 4, "ADA": 4,
                  "LINK": 3, "AVAX": 3, "DOGE": 5, "SUI": 4, "AAVE": 2}

# ── Asset-Universum (Live-Trading) ───────────────────────────────────────────
# Liquide Assets mit ausreichend Volumen für Micro-Account (enge Spreads, echte Fills)
LIVE_ASSETS = ["BTC", "ETH", "SOL", "XRP", "ADA", "LINK", "AVAX"]

# ── Data Intake Matrix ───────────────────────────────────────────────────────
# Welche (asset, interval)-Kombinationen der Intake-Agent vorhalten soll.
INTAKE_MATRIX = {
    "BTC":  ["5m", "15m", "1h"],
    "ETH":  ["5m", "15m", "1h", "4h"],
    "SOL":  ["5m", "15m", "1h"],
    "XRP":  ["5m", "15m", "1h"],
    "ADA":  ["5m", "15m", "1h"],
    "LINK": ["5m", "15m", "1h"],
    "AVAX": ["5m", "15m", "1h"],
}

# Kerzen-TTL: nach N Tagen aus DB löschen (täglich bereinigt)
CANDLE_TTL_DAYS   = 30
HEARTBEAT_TTL_DAYS = 7

# ── Governance ───────────────────────────────────────────────────────────────
# Signal älter als N Minuten → direkt auf 'expired' setzen
SIGNAL_EXPIRY_MINUTES = 60

# ── Strategie-Modi ───────────────────────────────────────────────────────────
# 'shadow'  → Signal wird geloggt, aber nie an Execution übergeben
# 'dry_run' → Execution simuliert Order lokal, kein API-Call
# 'live'    → Execution sendet echte Order an Bitget
STRATEGY_MODES = {
    "squeeze": {
        "ETH": "dry_run",   # Champion: OOS n=1924 PF=1.14 AvgR=+0.095
        "BTC": "dry_run",   # Lab: n=418 PF=1.10 AvgR=+0.068
        "SOL": "dry_run",   # Lab: n=1527 PF=1.07 AvgR=+0.053
    },
    "orb": {
        "ETH": "shadow", "SOL": "shadow", "AVAX": "shadow", "XRP": "shadow",
    },
    "vaa": {
        "SOL": "shadow", "AVAX": "shadow", "DOGE": "shadow",
        "ADA": "shadow", "SUI": "shadow", "AAVE": "shadow",
    },
    "kdt": {
        "ETH": "shadow",
    },
    "weekend_momo": {
        "AVAX": "shadow",
    },
}

# ── ORB Strategie ────────────────────────────────────────────────────────────
ORB_ASSETS        = ["ETH", "SOL", "AVAX", "XRP"]
ORB_ASSET_PRIORITY = ["ETH", "SOL", "AVAX", "XRP"]
MAX_SPREAD_PCT    = 0.1
BREAKOUT_THRESHOLD = {"ETH": 3.0, "SOL": 0.10, "AVAX": 0.03, "XRP": 0.001}
MIN_BOX_RANGE     = {"ETH": 1.0, "SOL": 0.10, "AVAX": 0.04, "XRP": 0.003}
MAX_BOX_AGE_MIN   = 120
MAX_BREAKOUT_DISTANCE_RATIO = 2.0

H006_EMA_FILTER_ENABLED = True
H006_REQUIRE_H4_ALIGN   = True
H014_VOLUME_FILTER_ENABLED = True
H014_VOLUME_RATIO_MIN      = 2.0
H015_REGIME_RISK_MODIFIER_ENABLED = True

# ── VAA Strategie ────────────────────────────────────────────────────────────
VAA_ENABLED      = True
VAA_ASSETS       = ["ETH"]   # Backtest: nur ETH hat positiven Edge (PF 2.17)
VAA_VOL_MULT     = 2.5
VAA_BODY_MULT    = 0.6
VAA_ATR_EXPAND   = 1.2
VAA_TP_R         = 3.0
VAA_ENTRY_WINDOW = 3
VAA_VOL_SMA_PERIOD  = 50
VAA_BODY_SMA_PERIOD = 50
VAA_EMA_PERIOD      = 20
VAA_ATR_PERIOD      = 14
VAA_CANDLE_LIMIT    = 120
VAA_MAX_RISK_PCT    = 0.02

# ── KDT Strategie ────────────────────────────────────────────────────────────
KDT_ENABLED      = False  # Deaktiviert: 2J-Backtest -34R, PF 0.39, kein Edge
KDT_ASSET        = "ETH"
KDT_EMA_PERIOD   = 50
KDT_ENTRY_WINDOW = 2
KDT_TP_R         = 3.0
KDT_SL_ATR_MULT  = 1.0
KDT_CANDLE_LIMIT = 120
KDT_MAX_RISK_PCT = 0.02

# ── Asian Fade ───────────────────────────────────────────────────────────────
# Hypothese: Asien-Pump (00:00–08:00 UTC) wird von London abverkauft.
ASIAN_FADE_ENABLED        = False  # 2026-04-27 archiviert: Grid-Test (4 Varianten) → kein Edge
ASIAN_FADE_ASSET          = "ETH"
ASIAN_FADE_PUMP_THRESHOLD = 0.015   # +1.5% overnight pump required
ASIAN_FADE_RSI_OB         = 70      # RSI(14) > 70 = overbought
ASIAN_FADE_SL_ATR_MULT    = 1.0     # SL = 1× ATR(14)
ASIAN_FADE_TP_MULT        = 1.5     # TP = 1.5R (relative zum SL)
ASIAN_FADE_MAX_RISK_PCT   = 0.02

# ── Squeeze Breakout ────────────────────────────────────────────────────────
# Champion-Parameter (Auto-Lab 2026-04-27): OOS n=1924, PF=1.14, AvgR=+0.095
SQUEEZE_ENABLED      = True
SQUEEZE_ASSETS       = ["ETH"]  # BTC/SOL entfernt: SL-Distanz > $56 Balance → execution_aborted
SQUEEZE_PERIOD       = 20      # TTM Squeeze BB/KC Periode
SQUEEZE_EMA_PERIOD   = 25      # EMA Richtungs-Filter (Champion: EMA_PERIOD=25)
SQUEEZE_SL_ATR_MULT  = 1.5     # SL = entry ± ATR(14) × 1.5 (Champion)
SQUEEZE_TP_R         = 3.0     # TP = SL_dist × 3.0R (Champion)
SQUEEZE_MAX_RISK_PCT = 0.02

# ── Weekend Momentum ─────────────────────────────────────────────────────────
WEEKEND_ASSET         = "AVAX"
MOMENTUM_THRESHOLD    = 0.03
ATR_SL_MULTIPLIER     = 1.5
ATR_TP_MULTIPLIER     = 3.0

# ── Session-Limit (C.2) ──────────────────────────────────────────────────────
MAX_DAILY_TRADES = 3  # max. abgeschlossene Trades pro 24h-Fenster (alle Assets, alle Strategien)

# ── Heartbeat-Schwellen (D.2) ─────────────────────────────────────────────────
# Maximales Alter des letzten Heartbeats pro Komponente (Minuten).
# Intake/Features sind zeitkritisch (10 min); Governance/Executor toleranter (30 min).
HEARTBEAT_THRESHOLDS_MIN: dict[str, int] = {
    "intake":          10,
    "features":        10,
    "strategies":      30,
    "governance":      30,
    "executor":        30,
    "monitor":         30,
    "master":          15,   # master_run.py — Watchdog-Schwelle (P1.3)
    "drift_check":   1500,   # Daily-Cron (06:00 UTC) — 25h Puffer
    "hmm_retrain":  10080,   # Wöchentlicher Job — 7-Tage-Puffer
    "regime_monitor":  250,  # 4h-Timer — 10 Min Puffer über 4h
}

# ── Telegram Shadow-Prefix ────────────────────────────────────────────────────
TELEGRAM_V2_PREFIX = "[V2·SHADOW]"

# ── Telegram Spam-Schutz (A.5) ───────────────────────────────────────────────
TG_DEDUPE_WINDOW_MIN  = 15   # gleiche Nachricht innerhalb N Minuten unterdrücken
TG_RATE_LIMIT_PER_MIN = 30   # max Nachrichten pro 60 Sekunden
TG_RATE_LIMIT_BURST   = 10   # Token-Bucket Burst-Kapazität
TG_CB_THRESHOLD       = 50   # ab dieser Anzahl Nachrichten in CB-Fenster → CB öffnet
TG_CB_WINDOW_MIN      = 10   # Beobachtungsfenster für CB-Threshold (Minuten)
TG_CB_RESET_MIN       = 60   # CB schließt nach N Minuten ohne neue Last

# ── v7.2 Research (2026-05-13) ───────────────────────────────────────────────
RANDOM_SEED           = 42       # globaler Determinismus für Optuna/MC/Bootstrap
OBJECTIVE_V72_VERSION = "v72.0"  # Bump bei Änderung der Objective-Funktion
V72_RESEARCH_ENABLED  = os.getenv("V72_RESEARCH_ENABLED", "false").lower() == "true"
