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

# ── Tages-DD Circuit Breaker ─────────────────────────────────────────────────
DAILY_DD_HALF_R = -1.5
DAILY_DD_KILL_R = -2.0

# ── Bitget Handelsparameter ──────────────────────────────────────────────────
LEVERAGE     = 5
MARGIN_MODE  = "isolated"

SIZE_DECIMALS = {"BTC": 4, "ETH": 2, "SOL": 1, "AVAX": 1, "XRP": 0,
                 "DOGE": 0, "ADA": 0, "SUI": 1, "AAVE": 2}
PRICE_DECIMALS = {"BTC": 1, "ETH": 2, "SOL": 3, "AVAX": 3, "XRP": 4,
                  "DOGE": 5, "ADA": 4, "SUI": 4, "AAVE": 2}

# ── Data Intake Matrix ───────────────────────────────────────────────────────
# Welche (asset, interval)-Kombinationen der Intake-Agent vorhalten soll.
INTAKE_MATRIX = {
    "ETH":  ["5m", "15m", "1h", "4h"],
    "SOL":  ["5m", "15m", "1h"],
    "AVAX": ["5m", "15m", "1h"],
    "XRP":  ["5m", "15m"],
    "DOGE": ["1h"],
    "ADA":  ["1h"],
    "SUI":  ["1h"],
    "AAVE": ["1h"],
}

# Kerzen-TTL: nach N Tagen aus DB löschen (täglich bereinigt)
CANDLE_TTL_DAYS   = 30
HEARTBEAT_TTL_DAYS = 7

# ── Governance ───────────────────────────────────────────────────────────────
# Signal älter als N Minuten → direkt auf 'expired' setzen
SIGNAL_EXPIRY_MINUTES = 30

# ── Strategie-Modi ───────────────────────────────────────────────────────────
# 'shadow'  → Signal wird geloggt, aber nie an Execution übergeben
# 'dry_run' → Execution simuliert Order lokal, kein API-Call
# 'live'    → Execution sendet echte Order an Bitget
STRATEGY_MODES = {
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
VAA_ASSETS       = ["SOL", "AVAX", "DOGE", "ADA", "SUI", "AAVE"]
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
KDT_ENABLED      = True
KDT_ASSET        = "ETH"
KDT_EMA_PERIOD   = 50
KDT_ENTRY_WINDOW = 2
KDT_TP_R         = 3.0
KDT_SL_ATR_MULT  = 1.0
KDT_CANDLE_LIMIT = 120
KDT_MAX_RISK_PCT = 0.02

# ── Weekend Momentum ─────────────────────────────────────────────────────────
WEEKEND_ASSET         = "AVAX"
MOMENTUM_THRESHOLD    = 0.03
ATR_SL_MULTIPLIER     = 1.5
ATR_TP_MULTIPLIER     = 3.0

# ── Telegram Shadow-Prefix ────────────────────────────────────────────────────
TELEGRAM_V2_PREFIX = "[V2·SHADOW]"
