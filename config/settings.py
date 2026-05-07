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
    # Alle anderen (vaa, kdt, weekend_momo, asian_fade, vwap_bounce) → kein Filter
}

# ── Backtest-Kostenmodell (Bitget Richtwerte) ────────────────────────────────
TAKER_FEE    = 0.0006   # 0.06% pro Side (Taker-Order)
MAKER_FEE    = 0.0002   # 0.02% pro Side (Maker-Order)
SLIPPAGE_EST = 0.0003   # 0.03% geschätzte Markt-Slippage
FUNDING_8H   = 0.0001   # 0.01% Funding-Rate per 8h-Periode
ROUND_TRIP   = (TAKER_FEE + SLIPPAGE_EST) * 2  # 0.18% gesamt Ein+Ausstieg

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

# ── Telegram Shadow-Prefix ────────────────────────────────────────────────────
TELEGRAM_V2_PREFIX = "[V2·SHADOW]"
