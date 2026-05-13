#!/usr/bin/env python3
"""
APEX Auto-Lab Daemon — Autonomer Quant-Researcher (v2)

Läuft als 24/7 Hintergrundprozess. Durchsucht den Parameter-Raum
mittels Monte-Carlo-Sampling (zufällig innerhalb definierter Ranges),
ergänzt durch systematische Grid-Abdeckung.

Neu in v2:
  • Market-Regime-Tagging (TREND_UP / TREND_DOWN / SIDEWAYS) pro Testfenster
  • Highscore-Modus: Push NUR wenn neuer PF-Rekord pro (asset, regime)
  • Alpha-Library: bis zu 5.000 Funde pro (asset, regime)
  • Log-Rotation: RotatingFileHandler (10 MB, 3 Backups)

Start (empfohlen):
  python3 research/auto_lab_daemon.py &
  echo $! > /tmp/apex_lab_daemon.pid

Stop:
  kill $(cat /tmp/apex_lab_daemon.pid)
"""

import sys
import os
import json
import math
import random
import hashlib
import time
import itertools
import logging
import logging.handlers
import requests
from datetime import datetime, timezone

# .env vor allen eigenen Imports laden
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", ".env"))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_connection, run_migrations
from backtest.engine import run_backtest
from features.indicators import (
    ema, atr_wilder, sma, detect_regime as _detect_regime_fn,
    REGIME_EMA_PERIOD, REGIME_SLOPE_PCT,
)
from research.lab_search_config import LAB_SEARCH_CFG


# ── Logging mit Rotation ─────────────────────────────────────────────────────

_LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs", "lab_daemon.log",
)

_handler = logging.handlers.RotatingFileHandler(
    _LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))

_stderr = logging.StreamHandler(sys.stderr)
_stderr.setFormatter(logging.Formatter("%(asctime)s %(message)s"))

_logger = logging.getLogger("lab_daemon")
_logger.setLevel(logging.INFO)
_logger.addHandler(_handler)
_logger.addHandler(_stderr)


def log(msg: str):
    _logger.info(msg)


# ── Konfiguration ────────────────────────────────────────────────────────────

DAYS = 730  # Gesamtzeitraum für Backtest-Daten in Tagen
COOLDOWN_BARS = 4  # Bars Pause nach jedem Exit (4h = halbe Session → mehr Signalchancen)

# Mindest-Signalfrequenz: ≥2.5 Trades/Woche im OOS-Fenster.
# Sichert, dass deployed Setups im Live-Betrieb mind. 4x/Woche über 6 Assets feuern.
MIN_SIGNALS_PER_WEEK = 2.5

# ── Multi-Window OOS Validation ───────────────────────────────────────────────
# Ersetzt den früheren 70/30-Single-Split.
# Ein Setup muss ALLE 3 Fenster bestehen — kein Fenster kompensiert das andere.
# Offsets in Tagen relativ zu now_ms (negativ = Vergangenheit).
WF_WINDOWS = [
    # Fenster 1 (alt): 120 Tage OOS — min_n=35 → ≥2/Woche
    {"train_end": -480, "test_start": -480, "test_end": -360,
     "min_n": 35, "min_pf": 1.20, "min_avg_r": 0.06, "min_wr": 46.0,
     "ruin_filter": True, "weight": 1.0, "days": 120},
    # Fenster 2 (mittel): 120 Tage OOS — min_n=35 → ≥2/Woche
    {"train_end": -240, "test_start": -240, "test_end": -120,
     "min_n": 35, "min_pf": 1.20, "min_avg_r": 0.06, "min_wr": 46.0,
     "ruin_filter": True, "weight": 1.5, "days": 120},
    # Fenster 3 (aktuell): 60 Tage OOS — deployment-relevant, Ruin-Filter aktiv
    {"train_end": -60,  "test_start": -60,  "test_end": 0,
     "min_n": 22, "min_pf": 1.20, "min_avg_r": 0.06, "min_wr": 46.0,
     "ruin_filter": True, "weight": 2.0, "days": 60},
]
TOTAL_WEIGHT = sum(w["weight"] for w in WF_WINDOWS)

# Schwellen für Alpha-Library-Aufnahme (alle Fenster müssen bestehen)
MIN_PF_TEST         = 1.80   # Brutto-Minimum für Netto-PF ≥ 1.3 nach Kosten (ca. 25-35% Abzug)
MIN_N_TEST_TOTAL    = 100    # Mindest-Gesamtanzahl OOS-Trades über alle 3 Fenster (V-04)
MIN_PF_TRAIN        = 1.10
MAX_TRAIN_TEST_DROP = 0.40

# Monte-Carlo vs. Grid: 80% random sampling, 20% Grid-Abdeckung
MONTE_CARLO_FRAC = 0.80
BATCH_SIZE       = 20
N_TRIALS_DAEMON  = 20   # Daemon-Modus (--single-pass): schnellere Rotation
N_TRIALS_FULL    = 50   # Manueller Aufruf: volle Suchtiefe
SLEEP_BETWEEN    = 30
SLEEP_ON_ERROR   = 120

# Maximale Funde pro (asset, regime) — Alpha-Library Limit
MAX_DISCOVERIES_PER_BUCKET = 5_000

# ── Budget-Parameter (Micro-Account Schutz) ───────────────────────────────────
STARTING_CAPITAL     = 56.0   # USDT
RISK_PER_TRADE       = 1.50   # USDT pro Trade (fest)
MAX_DRAWDOWN_PERCENT = 0.25   # 25 % → max 14 USDT Ruin-Schwelle

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# Regime-Parameter importiert aus features/indicators.py (Single Source of Truth)

# ── Rejection-Kategorien (für Lab-Stats) ─────────────────────────────────────
# Jeder Rejection-Reason wird auf eine der folgenden Kategorien gemappt,
# damit /lab_stats eine lesbare Top-Liste ausgeben kann.
_REJECTION_CATEGORY = {
    "n_test":        "zu_wenig_trades",
    "total_n":       "zu_wenig_trades_gesamt",
    "freq=":         "zu_selten",
    "pf_test":       "pf_zu_niedrig",
    "wr_test":       "wr_zu_niedrig",
    "avg_r_test":    "avg_r_zu_niedrig",
    "pf_train":      "train_pf_zu_niedrig",
    "overfit_drop":    "ueberfit",
    "pf_overfit_drop": "ueberfit_pf",
    "ruin_filter":     "ruin_drawdown",
}

def _rejection_category(reason: str) -> str:
    """Mappt den rohen _passes()-Reason-String auf eine zählbare Kategorie."""
    for key, cat in _REJECTION_CATEGORY.items():
        if key in reason:
            return cat
    return "sonstige"


# ── Parameter-Räume ──────────────────────────────────────────────────────────

RANGES = {
    "squeeze": {
        "SQUEEZE_PERIOD": [10,  30,  True],
        "EMA_PERIOD":     [10,  35,  True],
        "SL_ATR_MULT":    [0.3, 2.5, False],
        "TP_R":           [1.0, 6.0, False],
    },
    "vaa": {
        "VOL_MULT":   [1.5, 5.0, False],
        "BODY_MULT":  [0.3, 0.8, False],
        "ATR_EXPAND": [0.8, 2.5, False],
        "TP_R":       [1.5, 6.0, False],
    },
    "mean_reversion": {
        "BB_PERIOD":    [10,  30,  True],
        "BB_MULT":      [1.5, 3.0, False],
        "RSI_PERIOD":   [10,  21,  True],
        "RSI_OS":       [25,  42,  False],
        "SL_ATR_MULT":  [0.5, 2.0, False],
        "TP_R":         [1.0, 4.0, False],
    },
    "vwap_bounce": {
        "VWAP_PERIOD":  [12,  48,  True],
        "VWAP_BAND":    [0.1, 0.5, False],
        "EMA_PERIOD":   [20, 100,  True],
        "RSI_MIN":      [45,  60,  False],
        "SL_ATR_MULT":  [0.5, 2.0, False],
        "TP_R":         [1.5, 5.0, False],
    },
    "ema_pullback": {
        "EMA_SLOW":    [100, 200, True],
        "EMA_FAST":    [20,   75, True],
        "BODY_FACTOR": [0.1,  0.6, False],
        "SL_ATR_MULT": [0.5,  2.0, False],
        "TP_R":        [1.5,  5.0, False],
    },
    "donchian_breakout": {
        "DC_PERIOD":    [10,  50, True],
        "VOL_FACTOR":   [1.2, 3.0, False],
        "ATR_MIN_MULT": [0.8, 2.0, False],
        "SL_ATR_MULT":  [0.5, 1.5, False],   # eng halten → WR-freundlich
        "TP_R":         [1.2, 3.0, False],   # eng halten → WR-freundlich
    },
    "inside_bar_breakout": {
        "EMA_PERIOD":    [20, 100, True],
        "MOTHER_ATR_MIN":[0.3, 1.5, False],
        "SL_ATR_MULT":  [0.5, 2.0, False],
        "TP_R":         [1.5, 4.0, False],
    },
    "dual_donchian": {
        "ENTRY_PERIOD": [15,  60, True],
        "EXIT_PERIOD":  [5,   20, True],
        "VOL_FACTOR":   [1.2, 3.0, False],
        "ATR_MIN_MULT": [0.8, 2.0, False],
        "SL_ATR_MULT":  [0.5, 1.5, False],
        "TP_R":         [1.2, 3.0, False],
    },
    "bb_kc_squeeze": {
        "BB_PERIOD":  [10,  30, True],
        "BB_MULT":    [1.5, 3.0, False],
        "KC_MULT":    [1.0, 2.5, False],
        "SL_ATR_MULT":[0.5, 2.0, False],
        "TP_R":       [1.5, 5.0, False],
    },
    "supertrend": {
        "ST1_PERIOD": [7,   14, True],
        "ST1_MULT":   [0.5, 2.0, False],
        "ST2_PERIOD": [10,  20, True],
        "ST2_MULT":   [1.5, 3.5, False],
        "ST3_PERIOD": [12,  25, True],
        "ST3_MULT":   [2.5, 5.0, False],
        "SL_ATR_MULT":[0.5, 2.0, False],
        "TP_R":       [1.5, 5.0, False],
    },
    "orb": {
        "breakout_threshold_pct":  [0.0005, 0.005,  False],
        "min_box_range_pct":       [0.002,  0.015,  False],
        "max_box_age_bars":        [2,      12,     True],
        "volume_ratio_min":        [1.0,    3.0,    False],
        "max_breakout_dist_ratio": [1.0,    3.0,    False],
        "sl_buffer_pct":           [0.0005, 0.003,  False],
    },
}

GRIDS = {
    "squeeze": {
        "SQUEEZE_PERIOD": [10, 15, 20, 25, 30],
        "EMA_PERIOD":     [10, 15, 20, 25, 30],
        "SL_ATR_MULT":    [0.3, 0.5, 0.75, 1.0, 1.5, 2.0],
        "TP_R":           [1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0],
    },
    "vaa": {
        "VOL_MULT":   [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0],
        "BODY_MULT":  [0.3, 0.4, 0.5, 0.6, 0.7, 0.8],
        "ATR_EXPAND": [0.8, 1.0, 1.2, 1.5, 1.8, 2.5],
        "TP_R":       [1.5, 2.0, 3.0, 4.0, 5.0, 6.0],
    },
    "mean_reversion": {
        "BB_PERIOD":   [10, 15, 20, 25, 30],
        "BB_MULT":     [1.5, 1.8, 2.0, 2.5, 3.0],
        "RSI_PERIOD":  [10, 14, 21],
        "RSI_OS":      [25, 30, 35, 40, 42],
        "SL_ATR_MULT": [0.5, 0.75, 1.0, 1.5, 2.0],
        "TP_R":        [1.0, 1.5, 2.0, 3.0, 4.0],
    },
    "vwap_bounce": {
        "VWAP_PERIOD":  [12, 18, 24, 36, 48],
        "VWAP_BAND":    [0.1, 0.2, 0.25, 0.35, 0.5],
        "EMA_PERIOD":   [20, 30, 50, 75, 100],
        "RSI_MIN":      [45, 48, 50, 52, 55, 60],
        "SL_ATR_MULT":  [0.5, 0.75, 1.0, 1.5, 2.0],
        "TP_R":         [1.5, 2.0, 2.5, 3.0, 4.0, 5.0],
    },
    "ema_pullback": {
        "EMA_SLOW":    [100, 150, 200],
        "EMA_FAST":    [20, 30, 50, 75],
        "BODY_FACTOR": [0.1, 0.2, 0.3, 0.5],
        "SL_ATR_MULT": [0.5, 0.75, 1.0, 1.5, 2.0],
        "TP_R":        [1.5, 2.0, 2.5, 3.0, 4.0, 5.0],
    },
    "donchian_breakout": {
        "DC_PERIOD":    [10, 15, 20, 30, 50],
        "VOL_FACTOR":   [1.2, 1.5, 2.0, 2.5, 3.0],
        "ATR_MIN_MULT": [0.8, 1.0, 1.2, 1.5, 2.0],
        "SL_ATR_MULT":  [0.5, 0.75, 1.0, 1.5],
        "TP_R":         [1.2, 1.5, 2.0, 2.5, 3.0],
    },
    "inside_bar_breakout": {
        "EMA_PERIOD":     [20, 30, 50, 75, 100],
        "MOTHER_ATR_MIN": [0.3, 0.5, 0.75, 1.0, 1.5],
        "SL_ATR_MULT":    [0.5, 0.75, 1.0, 1.5, 2.0],
        "TP_R":           [1.5, 2.0, 2.5, 3.0, 4.0],
    },
    "dual_donchian": {
        "ENTRY_PERIOD": [15, 20, 30, 40, 50, 60],
        "EXIT_PERIOD":  [5, 8, 10, 15, 20],
        "VOL_FACTOR":   [1.2, 1.5, 2.0, 2.5, 3.0],
        "ATR_MIN_MULT": [0.8, 1.0, 1.2, 1.5, 2.0],
        "SL_ATR_MULT":  [0.5, 0.75, 1.0, 1.5],
        "TP_R":         [1.2, 1.5, 2.0, 2.5, 3.0],
    },
    "bb_kc_squeeze": {
        "BB_PERIOD":  [10, 15, 20, 25, 30],
        "BB_MULT":    [1.5, 1.8, 2.0, 2.5, 3.0],
        "KC_MULT":    [1.0, 1.25, 1.5, 1.75, 2.0, 2.5],
        "SL_ATR_MULT":[0.5, 0.75, 1.0, 1.5, 2.0],
        "TP_R":       [1.5, 2.0, 2.5, 3.0, 4.0, 5.0],
    },
    "supertrend": {
        "ST1_PERIOD": [7, 9, 10, 12, 14],
        "ST1_MULT":   [0.5, 1.0, 1.5, 2.0],
        "ST2_PERIOD": [10, 11, 13, 16, 20],
        "ST2_MULT":   [1.5, 2.0, 2.5, 3.0, 3.5],
        "ST3_PERIOD": [12, 14, 18, 21, 25],
        "ST3_MULT":   [2.5, 3.0, 3.5, 4.0, 5.0],
        "SL_ATR_MULT":[0.5, 0.75, 1.0, 1.5, 2.0],
        "TP_R":       [1.5, 2.0, 2.5, 3.0, 4.0, 5.0],
    },
}

SEARCH_SPACE = [
    ("squeeze",        "BTC"),
    ("squeeze",        "ETH"),
    ("squeeze",        "SOL"),
    ("squeeze",        "XRP"),
    ("squeeze",        "ADA"),
    ("squeeze",        "LINK"),
    ("squeeze",        "AVAX"),
    ("vaa",            "BTC"),
    ("vaa",            "ETH"),
    ("vaa",            "SOL"),
    ("vaa",            "XRP"),
    ("vaa",            "ADA"),
    ("vaa",            "LINK"),
    ("vaa",            "AVAX"),
    ("mean_reversion", "BTC"),
    ("mean_reversion", "ETH"),
    ("mean_reversion", "SOL"),
    ("mean_reversion", "XRP"),
    ("mean_reversion", "ADA"),
    ("mean_reversion", "LINK"),
    ("mean_reversion", "AVAX"),
    ("vwap_bounce",         "BTC"),
    ("vwap_bounce",         "ETH"),
    ("vwap_bounce",         "SOL"),
    ("vwap_bounce",         "XRP"),
    ("vwap_bounce",         "ADA"),
    ("vwap_bounce",         "LINK"),
    ("vwap_bounce",         "AVAX"),
    ("ema_pullback",        "BTC"),
    ("ema_pullback",        "ETH"),
    ("ema_pullback",        "SOL"),
    ("ema_pullback",        "XRP"),
    ("ema_pullback",        "ADA"),
    ("ema_pullback",        "LINK"),
    ("ema_pullback",        "AVAX"),
    ("donchian_breakout",   "BTC"),
    ("donchian_breakout",   "ETH"),
    ("donchian_breakout",   "SOL"),
    ("donchian_breakout",   "XRP"),
    ("donchian_breakout",   "ADA"),
    ("donchian_breakout",   "LINK"),
    ("donchian_breakout",   "AVAX"),
    ("inside_bar_breakout", "BTC"),
    ("inside_bar_breakout", "ETH"),
    ("inside_bar_breakout", "SOL"),
    ("inside_bar_breakout", "XRP"),
    ("inside_bar_breakout", "ADA"),
    ("inside_bar_breakout", "LINK"),
    ("inside_bar_breakout", "AVAX"),
    ("dual_donchian",       "BTC"),
    ("dual_donchian",       "ETH"),
    ("dual_donchian",       "SOL"),
    ("dual_donchian",       "XRP"),
    ("dual_donchian",       "ADA"),
    ("dual_donchian",       "LINK"),
    ("dual_donchian",       "AVAX"),
    ("bb_kc_squeeze",       "BTC"),
    ("bb_kc_squeeze",       "ETH"),
    ("bb_kc_squeeze",       "SOL"),
    ("bb_kc_squeeze",       "XRP"),
    ("bb_kc_squeeze",       "ADA"),
    ("bb_kc_squeeze",       "LINK"),
    ("bb_kc_squeeze",       "AVAX"),
    ("supertrend",          "BTC"),
    ("supertrend",          "ETH"),
    ("supertrend",          "SOL"),
    ("supertrend",          "XRP"),
    ("supertrend",          "ADA"),
    ("supertrend",          "LINK"),
    ("supertrend",          "AVAX"),
    ("orb",                 "BTC"),
    ("orb",                 "ETH"),
    ("orb",                 "SOL"),
    ("orb",                 "XRP"),
    ("orb",                 "ADA"),
    ("orb",                 "LINK"),
    ("orb",                 "AVAX"),
]

def _load_requested_targets() -> list[tuple[str, str]]:
    """
    Liest asset_requests mit status='pending' aus der DB und erzeugt
    (strategy, asset)-Tupel für alle bekannten Strategien.
    Gibt leere Liste zurück wenn keine Anfragen vorhanden oder DB-Fehler.
    """
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT asset FROM asset_requests WHERE status='pending'"
        ).fetchall()
        conn.close()
    except Exception:
        return []

    strategies = list(RANGES.keys())
    result = []
    for row in rows:
        asset = row["asset"]
        for strategy in strategies:
            result.append((strategy, asset))
    return result


# Assets ohne bisherige qualifizierte Discovery — werden bevorzugt behandelt
_PRIORITY_ASSETS = {"XRP", "ADA", "LINK", "AVAX"}


# ── Datenbank-Schema ─────────────────────────────────────────────────────────

DDL_DISCOVERIES = """
CREATE TABLE IF NOT EXISTS lab_discoveries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    discovered_at TEXT NOT NULL,
    params_hash   TEXT NOT NULL UNIQUE,
    strategy      TEXT NOT NULL,
    asset         TEXT NOT NULL,
    params_json   TEXT NOT NULL,
    n_train       INTEGER, pf_train REAL, avg_r_train REAL,
    n_test        INTEGER, pf_test  REAL, avg_r_test  REAL, wr_test REAL,
    fitness_score REAL,
    max_dd_r      REAL,
    micro_score   REAL,
    notified      INTEGER NOT NULL DEFAULT 0
);
"""

# Highscore-Tabelle: bester PF pro (strategy, asset, regime)
DDL_HIGHSCORES = """
CREATE TABLE IF NOT EXISTS lab_highscores (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy      TEXT NOT NULL,
    asset         TEXT NOT NULL,
    market_regime TEXT NOT NULL,
    best_pf       REAL NOT NULL,
    best_fitness  REAL NOT NULL,
    discovery_id  INTEGER,
    updated_at    TEXT NOT NULL,
    UNIQUE(strategy, asset, market_regime)
);
"""

# Lauf-Statistik: Rejection-Counter und Gesamt-Testzähler
DDL_LAB_STATS = """
CREATE TABLE IF NOT EXISTS lab_stats (
    key        TEXT PRIMARY KEY,
    value      INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""


def _ensure_schema():
    conn = get_connection()
    # Tabellen anlegen (idempotent, ohne market_regime im DDL → backward-compat)
    conn.executescript(DDL_DISCOVERIES)
    conn.executescript(DDL_HIGHSCORES)
    conn.executescript(DDL_LAB_STATS)
    # market_regime nachrüsten falls Tabelle bereits ohne sie existiert
    cols = [r[1] for r in conn.execute("PRAGMA table_info(lab_discoveries)").fetchall()]
    if "market_regime" not in cols:
        conn.execute("ALTER TABLE lab_discoveries ADD COLUMN market_regime TEXT NOT NULL DEFAULT 'UNKNOWN'")
        log("[LAB-DAEMON] DB migriert: Spalte market_regime hinzugefügt")
    # Neue Spalten nachrüsten falls Tabelle älter ist
    for col, definition in [("max_dd_r", "REAL"), ("micro_score", "REAL"), ("signals_per_week", "REAL")]:
        if col not in cols:
            conn.execute(f"ALTER TABLE lab_discoveries ADD COLUMN {col} {definition}")
            log(f"[LAB-DAEMON] DB migriert: Spalte {col} hinzugefügt")
    # Deployment-Tracking-Spalten nachrüsten
    for col, definition in [
        ("deployment_status", "TEXT NOT NULL DEFAULT 'lab'"),
        ("deployed_at",       "TEXT"),
        ("deployed_by",       "TEXT"),
        ("deploy_notes",      "TEXT"),
    ]:
        if col not in cols:
            conn.execute(f"ALTER TABLE lab_discoveries ADD COLUMN {col} {definition}")
            log(f"[LAB-DAEMON] DB migriert: Spalte {col} hinzugefügt")
    # cost_model_applied nachrüsten (V-01)
    if "cost_model_applied" not in cols:
        conn.execute("ALTER TABLE lab_discoveries ADD COLUMN cost_model_applied INTEGER NOT NULL DEFAULT 0")
        log("[LAB-DAEMON] DB migriert: Spalte cost_model_applied hinzugefügt")
    # dsr nachrüsten (V-03)
    if "dsr" not in cols:
        conn.execute("ALTER TABLE lab_discoveries ADD COLUMN dsr REAL")
        log("[LAB-DAEMON] DB migriert: Spalte dsr hinzugefügt")
    # Indizes anlegen (Spalten garantiert vorhanden)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_disc_asset_regime ON lab_discoveries(asset, market_regime)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_disc_micro_score ON lab_discoveries(micro_score DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_disc_deployment ON lab_discoveries(deployment_status)")
    conn.commit()
    conn.close()


# ── Market-Regime-Erkennung ──────────────────────────────────────────────────
# Zentrale Logik in features/indicators.py::detect_regime() —
# hier nur der DB-Wrapper für historische Zeitfenster.

def _detect_regime(asset: str, start_ms: int, end_ms: int) -> str:
    """Ermittelt das dominante Markt-Regime im historischen Testzeitraum."""
    try:
        conn = get_connection()
        rows = conn.execute(
            """SELECT open, high, low, close, volume FROM candles
               WHERE asset=? AND interval='1h' AND ts >= ? AND ts <= ?
               ORDER BY ts ASC""",
            (asset, start_ms, end_ms),
        ).fetchall()
        conn.close()
    except Exception:
        return "UNKNOWN"

    candles = [{"open": r[0], "high": r[1], "low": r[2], "close": r[3], "volume": r[4]}
               for r in rows]
    return _detect_regime_fn(candles)


# ── Highscore-Logik ──────────────────────────────────────────────────────────

def _get_highscore(conn, strategy: str, asset: str, regime: str) -> tuple[float, float]:
    """Gibt (best_pf, best_fitness) für diesen Bucket zurück."""
    row = conn.execute(
        "SELECT best_pf, best_fitness FROM lab_highscores WHERE strategy=? AND asset=? AND market_regime=?",
        (strategy, asset, regime),
    ).fetchone()
    return (row[0], row[1]) if row else (0.0, 0.0)


def _get_best_micro_score(conn, strategy: str, asset: str, regime: str) -> float:
    """Gibt den besten bisher gespeicherten Micro-Score zurück."""
    row = conn.execute(
        """SELECT MAX(micro_score) FROM lab_discoveries
           WHERE strategy=? AND asset=? AND market_regime=? AND micro_score IS NOT NULL""",
        (strategy, asset, regime),
    ).fetchone()
    return row[0] if row and row[0] is not None else 0.0


def _update_highscore(conn, strategy: str, asset: str, regime: str,
                      pf: float, fitness: float, disc_id: int):
    conn.execute(
        """INSERT INTO lab_highscores (strategy, asset, market_regime, best_pf, best_fitness, discovery_id, updated_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(strategy, asset, market_regime) DO UPDATE SET
               best_pf=excluded.best_pf,
               best_fitness=excluded.best_fitness,
               discovery_id=excluded.discovery_id,
               updated_at=excluded.updated_at""",
        (strategy, asset, regime, pf, fitness, disc_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def _bucket_count(conn, asset: str, regime: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM lab_discoveries WHERE asset=? AND market_regime=?",
        (asset, regime),
    ).fetchone()[0]


# ── Parameter-Sampling ───────────────────────────────────────────────────────

def _round_params(params: dict) -> dict:
    return {k: (int(v) if isinstance(v, float) and v == int(v) else round(v, 2))
            for k, v in params.items()}


def _param_hash(strategy: str, asset: str, params: dict) -> str:
    rounded = _round_params(params)
    raw = f"{strategy}:{asset}:{json.dumps(rounded, sort_keys=True)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _sample_random(strategy: str) -> dict:
    ranges = RANGES[strategy]
    out = {}
    for key, (lo, hi, is_int) in ranges.items():
        val = random.uniform(lo, hi)
        out[key] = int(round(val)) if is_int else round(val, 2)
    return out


def _grid_iter(strategy: str):
    grid = GRIDS[strategy]
    keys = list(grid.keys())
    for combo in itertools.product(*[grid[k] for k in keys]):
        yield dict(zip(keys, combo))


def _batch(strategy: str, size: int) -> list[dict]:
    n_random = int(size * MONTE_CARLO_FRAC)
    n_grid   = size - n_random
    batch    = [_sample_random(strategy) for _ in range(n_random)]
    grid_pool = list(_grid_iter(strategy))
    if grid_pool:
        batch += random.sample(grid_pool, min(n_grid, len(grid_pool)))
    return batch


# ── Metriken & Fitness ───────────────────────────────────────────────────────

def _max_r_drawdown(result) -> float:
    """
    Berechnet den maximalen kumulativen R-Drawdown aus der Trade-Sequenz.
    Gibt einen positiven Wert zurück (z.B. 8.5 bedeutet -8.5R maximaler Einbruch).
    Schlüsselfunktion für den Ruin-Filter.
    """
    trades = getattr(result, "trades", [])
    if not trades:
        return 0.0
    peak      = 0.0
    equity    = 0.0
    max_dd    = 0.0
    for t in trades:
        equity += t.pnl_r
        if equity > peak:
            peak = equity
        dd = peak - equity          # aktueller Drawdown in R (immer ≥ 0)
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, 4)


def _metrics(result, window_days: int = 0) -> dict:
    s = result.summary()
    n = s["trades"]
    if n == 0:
        return {"n": 0, "avg_r": 0.0, "pf": 0.0, "wr": 0.0, "spw": 0.0}
    wins       = [t.pnl_r for t in result.trades if t.pnl_r > 0]
    losses     = [t.pnl_r for t in result.trades if t.pnl_r < 0]
    gross_win  = sum(wins)
    gross_loss = abs(sum(losses))
    pf  = gross_win / gross_loss if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)
    spw = round(n / window_days * 7, 2) if window_days > 0 else 0.0
    return {
        "n":     n,
        "avg_r": round(s["avg_r"], 4),
        "pf":    round(pf, 3),
        "wr":    round(s["winrate"], 2),
        "spw":   spw,  # signals per week
    }


def _fitness_single(te: dict) -> float:
    if te["n"] <= 0 or te["pf"] <= 0:
        return 0.0
    return te["pf"] * min(te["avg_r"], 1.0) * math.log(max(te["n"], 2))


def _fitness(window_results: list[dict]) -> float:
    """Gewichteter Fitness-Durchschnitt über alle OOS-Fenster (Fenster 3 trägt doppelt)."""
    total = 0.0
    for wr, wcfg in zip(window_results, WF_WINDOWS):
        total += _fitness_single(wr["te"]) * wcfg["weight"]
    return round(total / TOTAL_WEIGHT, 4)


def _calc_micro_score(pf: float, avg_r: float, wr: float, n: int, max_dd_r: float) -> float:
    """
    6-dimensionaler Composite-Score (institutioneller Standard):
      √PF × AvgR × (WR/50) × ln(n) × DD-Penalty × Calmar-Factor

    Calmar-Factor = min(avg_r / max_dd_r / 0.05, 2.0)
      → Normiert auf 0.05 als neutralen Calmar (AvgR = 5% des MaxDD)
      → Cap bei 2.0× um Explosion bei sehr kleinem MaxDD zu verhindern
    Skala: ~0–35 für realistische Setups.
    """
    if n < 20 or pf <= 0 or avg_r <= 0 or max_dd_r <= 0:
        return 0.0
    dd_penalty    = 1.0 / (1.0 + max_dd_r / 3.0)
    calmar        = avg_r / max_dd_r
    calmar_factor = min(calmar / 0.05, 2.0)
    return round(
        math.sqrt(pf) * avg_r * (wr / 50.0) * math.log(max(n, 2))
        * dd_penalty * calmar_factor * 10,
        2,
    )


def _passes_window(tr: dict, te: dict, max_dd_r: float, wcfg: dict) -> tuple[bool, str]:
    """Prüft ein einzelnes OOS-Fenster gegen die Fenster-spezifischen Schwellen."""
    if te["n"] < wcfg["min_n"]:
        return False, f"n_test={te['n']}<{wcfg['min_n']}"
    # Frequenzfilter: Strategie muss mindestens MIN_SIGNALS_PER_WEEK im OOS-Fenster feuern
    spw = te["n"] / wcfg["days"] * 7
    if spw < MIN_SIGNALS_PER_WEEK:
        return False, f"freq={spw:.2f}/w<{MIN_SIGNALS_PER_WEEK}/w"
    if te["pf"] < wcfg["min_pf"]:
        return False, f"pf_test={te['pf']:.2f}<{wcfg['min_pf']}"
    if te["wr"] < wcfg["min_wr"]:
        return False, f"wr_test={te['wr']:.1f}%<{wcfg['min_wr']}%"
    if te["avg_r"] < wcfg["min_avg_r"]:
        return False, f"avg_r_test={te['avg_r']:.3f}<{wcfg['min_avg_r']}"
    if tr["pf"] < MIN_PF_TRAIN:
        return False, f"pf_train={tr['pf']:.2f}<{MIN_PF_TRAIN}"
    drop = abs(tr["avg_r"] - te["avg_r"])
    if drop > MAX_TRAIN_TEST_DROP:
        return False, f"overfit_drop={drop:.3f}>{MAX_TRAIN_TEST_DROP}"
    pf_drop_ratio = (tr["pf"] - te["pf"]) / max(tr["pf"], 0.01)
    if pf_drop_ratio > 0.35:
        return False, f"pf_overfit_drop={pf_drop_ratio:.2f}"
    if wcfg["ruin_filter"]:
        max_dd_usdt = max_dd_r * RISK_PER_TRADE
        ruin_limit  = STARTING_CAPITAL * MAX_DRAWDOWN_PERCENT
        if max_dd_usdt > ruin_limit:
            return False, f"ruin_filter: dd={max_dd_usdt:.1f}$>{ruin_limit:.1f}$ ({max_dd_r:.1f}R)"
    return True, ""


def _passes(window_results: list[dict],
            pnl_rs_w3: list[float] | None = None,
            n_tested: int = 1) -> tuple[bool, str | tuple[str, float]]:
    """Multi-Window OOS Validation: alle Fenster müssen bestehen."""
    for i, wr in enumerate(window_results):
        if not wr["passed"]:
            return False, f"w{i+1}_{wr['reason']}"
    total_n = sum(wr["te"]["n"] for wr in window_results)
    if total_n < MIN_N_TEST_TOTAL:
        return False, f"total_n={total_n}<{MIN_N_TEST_TOTAL}"
    # DSR (V-03): berechnen und als Metrik mitgeben — kein Pass/Fail-Filter,
    # da DSR bei großem N (70+) für alle realen Strategien gegen 0 geht.
    # Gespeichert in lab_discoveries.dsr für Ranking und spätere Analyse.
    dsr = _calc_dsr(pnl_rs_w3, n_tested) if pnl_rs_w3 is not None else 0.0
    return True, ("", dsr)


# ── Telegram ─────────────────────────────────────────────────────────────────

def _escape_md(text: str) -> str:
    """Escaped Sonderzeichen für Telegram MarkdownV2."""
    for ch in r'_*[]()~`>#+-=|{}.!':
        text = text.replace(ch, f'\\{ch}')
    return text


def _send_telegram(text: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "MarkdownV2"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        log(f"[LAB-DAEMON] Telegram-Fehler: {e}")
        return False


_REGIME_ICON = {"TREND_UP": "📈", "TREND_DOWN": "📉", "SIDEWAYS": "↔️", "UNKNOWN": "❓"}


def _notify_highscore(strategy: str, asset: str, regime: str, params: dict,
                      tr: dict, te: dict, fitness: float, prev_pf: float,
                      disc_n: int, max_dd_r: float, micro_score: float,
                      prev_micro: float, disc_id: int = 0):
    icon       = _REGIME_ICON.get(regime, "❓")
    dd_usdt    = max_dd_r * RISK_PER_TRADE
    ruin_limit = STARTING_CAPITAL * MAX_DRAWDOWN_PERCENT
    spw        = te.get("spw", 0.0)
    param_lines = "\n".join(f"  `{k}` \\= `{v}`" for k, v in sorted(params.items()))
    msg = (
        f"🏆 *Neuer Micro\\-Score\\-Rekord\\!* \\(Discovery \\#{disc_n}\\)\n"
        f"🆔 Deploy\\-ID: `{disc_id}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 `{strategy}/{asset}`  {icon} `{regime}`\n"
        f"🎯 Score: `{prev_micro:.2f}` → *`{micro_score:.2f}`*\n\n"
        f"*Out\\-of\\-Sample \\(OOS\\):*\n"
        f"  📊 Trades:   *{te['n']}*  \\({spw:.1f}/Woche\\)\n"
        f"  💰 PF:       *{te['pf']:.2f}*\n"
        f"  🎰 Win\\-Rate: *{te['wr']:.1f}%*\n"
        f"  📈 Avg R:    *{te['avg_r']:+.3f}R*\n"
        f"  📉 Max DD:   *\\-${dd_usdt:.2f}* \\({max_dd_r:.1f}R\\)\n"
        f"  🏋️ Fitness:  `{fitness:.4f}`\n\n"
        f"*Train \\(Robustheit\\):*\n"
        f"  PF: {tr['pf']:.2f}  ·  Avg R: {tr['avg_r']:+.3f}R\n\n"
        f"*Parameter:*\n{param_lines}"
    )

    # Glossar-Buttons als Inline-Keyboard (JSON direkt für requests.post)
    keyboard = {"inline_keyboard": [[
        {"text": "❓ Score",    "callback_data": "info_score"},
        {"text": "❓ PF",       "callback_data": "info_pf"},
        {"text": "❓ Win-Rate", "callback_data": "info_wr"},
    ], [
        {"text": "❓ Avg R",   "callback_data": "info_avgr"},
        {"text": "❓ Fitness", "callback_data": "info_fitness"},
        {"text": "❓ Max DD",  "callback_data": "info_maxdd"},
    ]]}

    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID, "text": msg,
                "parse_mode": "MarkdownV2",
                "reply_markup": keyboard,
            },
            timeout=10,
        )
    except Exception as e:
        log(f"[LAB-DAEMON] Telegram-Fehler: {e}")


# ── Discovery-Persistenz ─────────────────────────────────────────────────────

def _already_known(conn, h: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM lab_discoveries WHERE params_hash=?", (h,)
    ).fetchone() is not None


def _save_discovery(conn, h: str, strategy: str, asset: str, regime: str,
                    params: dict, tr: dict, te: dict, fitness: float,
                    max_dd_r: float, micro_score: float,
                    window_results: list[dict] | None = None,
                    now_ms: int = 0, dsr: float | None = None) -> int:
    cur = conn.execute(
        """INSERT OR IGNORE INTO lab_discoveries
           (discovered_at, params_hash, strategy, asset, market_regime, params_json,
            n_train, pf_train, avg_r_train,
            n_test,  pf_test,  avg_r_test,  wr_test,
            fitness_score, max_dd_r, micro_score, signals_per_week, notified, cooldown_bars,
            cost_model_applied, dsr, framework_version, lab_config_hash)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,1,?,?,?)""",
        (
            datetime.now(timezone.utc).isoformat(), h, strategy, asset, regime,
            json.dumps(_round_params(params), sort_keys=True),
            tr["n"], tr["pf"], tr["avg_r"],
            te["n"], te["pf"], te["avg_r"], te["wr"],
            fitness, max_dd_r, micro_score, te.get("spw", 0.0), COOLDOWN_BARS,
            dsr, "v7", LAB_SEARCH_CFG.hash(),
        ),
    )
    disc_id = cur.lastrowid

    # Fenster-Ergebnisse speichern (Weg B)
    if disc_id and window_results and now_ms:
        for idx, (wr_data, wcfg) in enumerate(zip(window_results, WF_WINDOWS)):
            period_start = now_ms + wcfg["test_start"] * 86_400_000
            period_end   = now_ms + wcfg["test_end"]   * 86_400_000 if wcfg["test_end"] != 0 else now_ms
            conn.execute(
                """INSERT OR IGNORE INTO lab_window_results
                   (discovery_id, window_idx, period_start, period_end,
                    n_train, pf_train, avg_r_train,
                    n_test,  pf_test,  avg_r_test,  wr_test,
                    max_dd_r, passed)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    disc_id, idx, period_start, period_end,
                    wr_data["tr"]["n"], wr_data["tr"]["pf"], wr_data["tr"]["avg_r"],
                    wr_data["te"]["n"], wr_data["te"]["pf"], wr_data["te"]["avg_r"], wr_data["te"]["wr"],
                    wr_data["max_dd_r"], 1 if wr_data["passed"] else 0,
                ),
            )

    conn.commit()
    return disc_id


def _count_discoveries(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM lab_discoveries").fetchone()[0]


# ── Lab-Stats Counter ─────────────────────────────────────────────────────────

def _stat_inc(_conn_unused, key: str, delta: int = 1) -> None:
    """Atomar einen Stats-Counter erhöhen — eigene kurzlebige Verbindung,
    damit die Backtest-Schleife keine lange Schreibsperre hält."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        c = get_connection()
        c.execute(
            """INSERT INTO lab_stats (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = value + ?, updated_at = ?""",
            (key, delta, now, delta, now),
        )
        c.commit()
        c.close()
    except Exception:
        pass  # Stats-Fehler sind nicht kritisch


def get_lab_stats() -> dict:
    """Liest alle Lab-Stats aus der DB — wird vom Telegram-Bot genutzt."""
    conn = get_connection()
    rows = conn.execute("SELECT key, value FROM lab_stats").fetchall()
    total_disc = conn.execute("SELECT COUNT(*) FROM lab_discoveries").fetchone()[0]

    # Blind Spots: (asset, regime)-Kombinationen ohne gültiges Setup
    # Alle LIVE_ASSETS × bekannte Regimes prüfen
    live_assets = ["BTC", "ETH", "SOL", "XRP", "ADA", "LINK", "AVAX"]
    regimes     = ["TREND_UP", "TREND_DOWN", "SIDEWAYS"]
    blind_spots = []
    for asset in live_assets:
        for regime in regimes:
            has = conn.execute(
                """SELECT 1 FROM lab_discoveries
                   WHERE asset=? AND market_regime=?
                     AND micro_score > 0 AND wr_test >= 48.0 AND n_test >= 30
                   LIMIT 1""",
                (asset, regime),
            ).fetchone()
            if not has:
                blind_spots.append(f"{asset}/{regime}")
    conn.close()

    stats = {r[0]: r[1] for r in rows}
    total_tests = stats.get("total_tests", 0)
    total_pass  = stats.get("total_pass",  0)
    hit_rate    = round(total_pass / total_tests * 100, 2) if total_tests > 0 else 0.0

    # Rejection-Kategorien sortiert nach Häufigkeit
    rejections = {
        k.replace("reject_", ""): v
        for k, v in stats.items() if k.startswith("reject_")
    }
    top_rejection = sorted(rejections.items(), key=lambda x: x[1], reverse=True)

    return {
        "total_tests":   total_tests,
        "total_pass":    total_pass,
        "total_disc":    total_disc,
        "hit_rate":      hit_rate,
        "top_rejection": top_rejection,
        "blind_spots":   blind_spots[:5],
        "updated_at":    stats.get("updated_at_iso", "—"),
    }


# ── FDR-Control: Benjamini-Hochberg ──────────────────────────────────────────

def _normal_cdf(z: float) -> float:
    """Standard-Normal-CDF via math.erfc (keine scipy-Abhängigkeit)."""
    import math
    return 0.5 * math.erfc(-z / math.sqrt(2))


def _pf_to_pvalue(pf: float, n: int) -> float:
    """
    Approximiert p-Wert aus Profit-Factor und Sample-Size.
    Einseitiger Binomial-Test (WR > 50%) mit Normal-Approximation.
    Trades sind zeitlich korreliert → p-Werte sind konservativ zu interpretieren.
    """
    import math
    wr  = pf / (1.0 + pf)
    se  = math.sqrt(wr * (1.0 - wr) / max(n, 1))
    if se == 0:
        return 0.0001
    z = (wr - 0.5) / se
    return max(0.0001, 1.0 - _normal_cdf(z))


def _benjamini_hochberg(pvalues: list[float], q: float) -> list[bool]:
    """
    Standard BH-Prozedur: kontrolliert FDR auf Niveau q.
    Gibt Boolean-Maske zurück: True = Discovery akzeptiert.
    """
    m = len(pvalues)
    if m == 0:
        return []
    sorted_idx = sorted(range(m), key=lambda i: pvalues[i])
    sorted_p   = [pvalues[i] for i in sorted_idx]

    k_max = -1
    for k in range(m):
        if sorted_p[k] <= (k + 1) / m * q:
            k_max = k

    accept = [False] * m
    if k_max >= 0:
        for j in range(k_max + 1):
            accept[sorted_idx[j]] = True
    return accept


def _calc_dsr(pnl_rs: list[float], n_tested: int) -> float:
    """
    Deflated Sharpe Ratio (Bailey & López de Prado 2014).
    Gibt P(SR_hat > SR_benchmark | N Tests) zurück — Wert zwischen 0 und 1.
    Je mehr Kombinationen getestet wurden (n_tested), desto höher muss SR_hat sein.
    """
    import math

    T = len(pnl_rs)
    if T < 10:
        return 0.0   # numerisch instabil bei zu kleinem n

    mean_r = sum(pnl_rs) / T
    var_r  = sum((r - mean_r) ** 2 for r in pnl_rs) / max(T - 1, 1)
    std_r  = math.sqrt(var_r) if var_r > 0 else 1e-9

    # SR_hat annualisiert: skaliert auf Jahresbasis via Trades pro Jahr.
    # Annahme: ~2 Signale/Woche → ~104 Trades/Jahr (konservativ für Micro-Account).
    # Annualisierungsfaktor √(trades_per_year) analog zu √252 bei täglichen Returns.
    trades_per_year = 104
    sr_hat = mean_r / std_r * math.sqrt(trades_per_year)

    # Schiefe (γ3) und Excess-Kurtosis (γ4) der Returns
    if std_r > 0 and T >= 3:
        gamma3 = sum((r - mean_r) ** 3 for r in pnl_rs) / (T * std_r ** 3)
        gamma4 = sum((r - mean_r) ** 4 for r in pnl_rs) / (T * std_r ** 4) - 3.0
        gamma4 = max(gamma4, 0.0)   # Clipping: neg. Excess-Kurtosis stabilisiert Formel
    else:
        gamma3, gamma4 = 0.0, 0.0

    # SR-Benchmark unter Null-Hypothese (erwarteter Max-Sharpe bei N Tests)
    N = max(n_tested, 1)
    gamma_e = 0.5772156649   # Euler-Mascheroni-Konstante

    def phi_inv(p: float) -> float:
        """Inverse Normalverteilung via Newton-Raphson auf erfc."""
        p = max(1e-9, min(1 - 1e-9, p))
        # Abramowitz & Stegun Näherung
        if p < 0.5:
            t = math.sqrt(-2 * math.log(p))
            c0, c1, c2 = 2.515517, 0.802853, 0.010328
            d1, d2, d3 = 1.432788, 0.189269, 0.001308
            return -(t - (c0 + c1*t + c2*t**2) / (1 + d1*t + d2*t**2 + d3*t**3))
        else:
            t = math.sqrt(-2 * math.log(1 - p))
            c0, c1, c2 = 2.515517, 0.802853, 0.010328
            d1, d2, d3 = 1.432788, 0.189269, 0.001308
            return t - (c0 + c1*t + c2*t**2) / (1 + d1*t + d2*t**2 + d3*t**3)

    sr_benchmark = (
        (1 - gamma_e) * phi_inv(1 - 1 / N)
        + gamma_e     * phi_inv(1 - 1 / (N * math.e))
    )

    # DSR-Z-Score
    denom_sq = 1 - gamma3 * sr_hat + gamma4 * sr_hat ** 2 / 4
    if denom_sq <= 0 or T <= 1:
        return 0.0

    z = (sr_hat - sr_benchmark) * math.sqrt(T - 1) / math.sqrt(denom_sq)
    return round(_normal_cdf(z), 6)


# ── Optuna TPE (Phase 2: alle SIGNAL_FNS) ────────────────────────────────────

# Parameter-Spaces für Optuna: (min, max, is_int)
# Abgeleitet aus cfg.get()-Calls in backtest/engine.py.
OPTUNA_SPACES: dict[str, dict[str, tuple]] = {
    "vaa": {
        "VOL_MULT":   (1.5, 4.0, False),
        "BODY_MULT":  (0.3, 0.8, False),
        "ATR_EXPAND": (0.8, 2.0, False),
        "TP_R":       (1.5, 5.0, False),
        "SL_ATR_MULT":(0.5, 2.0, False),
    },
    "kdt": {
        # KDT hat minimale eigene Parameter — SL/TP tunable
        "SL_ATR_MULT": (0.5, 2.0, False),
        "TP_R":        (1.5, 5.0, False),
    },
    "weekend_momo": {
        "MOMENTUM_THRESHOLD": (0.01, 0.06, False),
        "ATR_SL_MULT":        (0.5,  2.5, False),
        "ATR_TP_MULT":        (1.5,  5.0, False),
    },
    "asian_fade": {
        "PUMP_THRESHOLD": (0.008, 0.03,  False),
        "RSI_OB":         (60,    80,    True),
        "RSI_OS":         (20,    40,    True),
        "SL_ATR_MULT":    (0.5,   2.0,  False),
        "TP_MULT":        (1.0,   3.0,  False),
    },
    "squeeze": {
        "SQUEEZE_PERIOD": (10,  30, True),
        "EMA_PERIOD":     (10,  40, True),
        "SL_ATR_MULT":    (0.5, 2.0, False),
        "TP_R":           (1.5, 5.0, False),
    },
    "mean_reversion": {
        "BB_PERIOD":  (10,  30, True),
        "BB_MULT":    (1.5, 3.0, False),
        "RSI_PERIOD": (7,   21, True),
        "RSI_OS":     (25,  45, False),
        "SL_ATR_MULT":(0.5, 2.0, False),
        "TP_R":       (1.5, 4.0, False),
    },
    "vwap_bounce": {
        "VWAP_PERIOD": (12, 48, True),
        "VWAP_BAND":   (0.1, 0.5, False),
        "EMA_PERIOD":  (20,  80, True),
        "RSI_MIN":     (40,  60, False),
        "SL_ATR_MULT": (0.5, 2.0, False),
        "TP_R":        (1.5, 4.0, False),
    },
    "ema_pullback": {
        "EMA_SLOW":    (100, 200, True),
        "EMA_FAST":    (20,   75, True),
        "BODY_FACTOR": (0.1,  0.6, False),
        "SL_ATR_MULT": (0.5,  2.0, False),
        "TP_R":        (1.5,  5.0, False),
    },
    "donchian_breakout": {
        "DC_PERIOD":    (10,  50, True),
        "VOL_FACTOR":   (1.2, 3.0, False),
        "ATR_MIN_MULT": (0.8, 2.0, False),
        "SL_ATR_MULT":  (0.5, 1.5, False),
        "TP_R":         (1.2, 3.0, False),
    },
    "inside_bar_breakout": {
        "EMA_PERIOD":     (20, 100, True),
        "MOTHER_ATR_MIN": (0.3, 1.5, False),
        "SL_ATR_MULT":    (0.5, 2.0, False),
        "TP_R":           (1.5, 4.0, False),
    },
    "dual_donchian": {
        "ENTRY_PERIOD": (15,  60, True),
        "EXIT_PERIOD":  (5,   20, True),
        "VOL_FACTOR":   (1.2, 3.0, False),
        "ATR_MIN_MULT": (0.8, 2.0, False),
        "SL_ATR_MULT":  (0.5, 1.5, False),
        "TP_R":         (1.2, 3.0, False),
    },
    "bb_kc_squeeze": {
        "BB_PERIOD":  (10,  30, True),
        "BB_MULT":    (1.5, 3.0, False),
        "KC_MULT":    (1.0, 2.5, False),
        "SL_ATR_MULT":(0.5, 2.0, False),
        "TP_R":       (1.5, 5.0, False),
    },
    "supertrend": {
        "ST1_PERIOD": (7,   14, True),
        "ST1_MULT":   (0.5, 2.0, False),
        "ST2_PERIOD": (10,  20, True),
        "ST2_MULT":   (1.5, 3.5, False),
        "ST3_PERIOD": (12,  25, True),
        "ST3_MULT":   (2.5, 5.0, False),
        "SL_ATR_MULT":(0.5, 2.0, False),
        "TP_R":       (1.5, 5.0, False),
    },
    "orb": {
        "breakout_threshold_pct":  (0.0005, 0.005,  False),
        "min_box_range_pct":       (0.002,  0.015,  False),
        "max_box_age_bars":        (2,      12,     True),
        "volume_ratio_min":        (1.0,    3.0,    False),
        "max_breakout_dist_ratio": (1.0,    3.0,    False),
        "sl_buffer_pct":           (0.0005, 0.003,  False),
    },
}


def _optuna_objective(trial, strategy: str, asset: str,
                      now_ms: int, start_ms: int, conn) -> float:
    """
    Optuna-Objective für eine (strategy, asset)-Kombination.
    Gibt fitness_score zurück (0.0 bei Nicht-Bestehen).
    Pruned nach Fenster 1 wenn PF < MIN_PF_TEST.
    """
    import optuna

    space = OPTUNA_SPACES[strategy]
    params: dict = {}
    for key, (lo, hi, is_int) in space.items():
        if is_int:
            params[key] = trial.suggest_int(key, int(lo), int(hi))
        else:
            params[key] = round(trial.suggest_float(key, lo, hi), 3)

    h = _param_hash(strategy, asset, params)
    if _already_known(conn, h):
        raise optuna.exceptions.TrialPruned()

    window_results = []
    te_res_w3      = None
    for w_idx, wcfg in enumerate(WF_WINDOWS):
        train_end_ms  = now_ms + wcfg["train_end"]  * 86_400_000
        test_start_ms = now_ms + wcfg["test_start"] * 86_400_000
        test_end_ms   = now_ms + wcfg["test_end"]   * 86_400_000 if wcfg["test_end"] != 0 else now_ms
        try:
            tr_res = run_backtest(strategy, asset, start_ms,      train_end_ms,
                                  cfg=params, cooldown_bars=COOLDOWN_BARS, apply_costs=True)
            te_res = run_backtest(strategy, asset, test_start_ms, test_end_ms,
                                  cfg=params, cooldown_bars=COOLDOWN_BARS, apply_costs=True)
        except Exception:
            return 0.0

        tr       = _metrics(tr_res)
        te       = _metrics(te_res, window_days=wcfg["days"])
        max_dd_r = _max_r_drawdown(te_res)
        passed, reason = _passes_window(tr, te, max_dd_r, wcfg)
        window_results.append({"tr": tr, "te": te, "max_dd_r": max_dd_r,
                               "passed": passed, "reason": reason})
        if wcfg is WF_WINDOWS[-1]:
            te_res_w3 = te_res

        # Frühzeitiges Pruning nach Fenster 1 bei schwachem PF
        if w_idx == 0:
            trial.report(te["pf"], step=0)
            if te["pf"] < MIN_PF_TEST * 0.85:   # 15% Toleranz für spätere Fenster
                raise optuna.exceptions.TrialPruned()

    pnl_rs_w3 = [t.pnl_r for t in te_res_w3.trades] if te_res_w3 else []
    ok, result = _passes(window_results, pnl_rs_w3=pnl_rs_w3,
                         n_tested=trial.number + 1)
    if not ok:
        return 0.0

    dsr = result[1] if isinstance(result, tuple) else 0.0
    fitness = _fitness(window_results)

    # Candidate für spätere BH-Auswertung im trial user_attrs speichern
    trial.set_user_attr("params",         params)
    trial.set_user_attr("h",              h)
    trial.set_user_attr("window_results", window_results)
    trial.set_user_attr("dsr",            dsr)
    trial.set_user_attr("passed",         True)

    return fitness


def _run_optuna_target(strategy: str, asset: str,
                       now_ms: int, conn, n_trials: int = 50) -> list[dict]:
    """
    Führt eine Optuna-Study für (strategy, asset) durch.
    Gibt Liste von Candidates zurück (analog zum Batch-Pfad).
    """
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    start_ms = now_ms - DAYS * 86_400_000

    study = optuna.create_study(
        study_name=f"{strategy}_{asset}_{LAB_SEARCH_CFG.short_hash()}",
        direction="maximize",
        sampler=LAB_SEARCH_CFG.build_sampler(),
        pruner=LAB_SEARCH_CFG.build_pruner(),
    )
    study.optimize(
        lambda trial: _optuna_objective(trial, strategy, asset, now_ms, start_ms, conn),
        n_trials=n_trials,
        catch=(Exception,),
    )

    # Nur bestandene Trials als Candidates
    candidates = []
    for t in study.trials:
        if not t.user_attrs.get("passed"):
            continue
        pvalue = _pf_to_pvalue(
            t.user_attrs["window_results"][-1]["te"]["pf"],
            t.user_attrs["window_results"][-1]["te"]["n"],
        )
        candidates.append({
            "h":              t.user_attrs["h"],
            "params":         t.user_attrs["params"],
            "window_results": t.user_attrs["window_results"],
            "pvalue":         pvalue,
            "dsr":            t.user_attrs.get("dsr", 0.0),
        })

    pruned  = sum(1 for t in study.trials if t.state.name == "PRUNED")
    ok_cnt  = sum(1 for t in study.trials if t.state.name == "COMPLETE")
    log(f"[OPTUNA] {strategy}/{asset}: {n_trials} Trials — "
        f"{ok_cnt} complete, {pruned} pruned, {len(candidates)} passed")

    return candidates


# ── Haupt-Loop ───────────────────────────────────────────────────────────────

def _run_one_target(strategy: str, asset: str, now_ms: int, conn,
                    n_trials: int = N_TRIALS_FULL) -> int:
    """Multi-Window OOS Validation: alle 3 Fenster müssen bestehen."""
    # Regime anhand des aktuellsten Fensters bestimmen (Fenster 3)
    w3 = WF_WINDOWS[-1]
    w3_test_start = now_ms + w3["test_start"] * 86_400_000
    regime = _detect_regime(asset, w3_test_start, now_ms)

    # Bucket-Limit prüfen
    if _bucket_count(conn, asset, regime) >= MAX_DISCOVERIES_PER_BUCKET:
        log(f"[LAB-DAEMON] Bucket {asset}/{regime} voll ({MAX_DISCOVERIES_PER_BUCKET}) — überspringe")
        return 0

    from config.settings import BH_FDR_Q

    # ── Optuna-Pfad (Phase 1: donchian_breakout) ──────────────────────────────
    if strategy in OPTUNA_SPACES:
        candidates = _run_optuna_target(strategy, asset, now_ms, conn,
                                        n_trials=n_trials)
        # BH + Discovery-Speicherung (identisch zum Batch-Pfad unten)
        if not candidates:
            return 0
        pvalues  = [c["pvalue"] for c in candidates]
        accepted = _benjamini_hochberg(pvalues, q=BH_FDR_Q)
        bh_rejected = sum(1 for a in accepted if not a)
        if bh_rejected:
            _stat_inc(None, "bh_rejected_today", bh_rejected)
        found = 0
        for cand, is_accepted in zip(candidates, accepted):
            if not is_accepted:
                continue
            window_results = cand["window_results"]
            te3    = window_results[-1]["te"]
            max_dd_r = window_results[-1]["max_dd_r"]
            tr3    = window_results[-1]["tr"]
            dsr    = cand.get("dsr")
            micro_score = _calc_micro_score(te3["pf"], te3["avg_r"], te3["wr"], te3["n"], max_dd_r)
            fitness     = _fitness(window_results)
            prev_pf, prev_fit = _get_highscore(conn, strategy, asset, regime)
            prev_micro        = _get_best_micro_score(conn, strategy, asset, regime)
            is_micro_highscore = micro_score > prev_micro
            disc_id = _save_discovery(conn, cand["h"], strategy, asset, regime, cand["params"],
                                      tr3, te3, fitness, max_dd_r, micro_score,
                                      window_results=window_results, now_ms=now_ms, dsr=dsr)
            disc_n  = _count_discoveries(conn)
            dd_usdt = max_dd_r * RISK_PER_TRADE
            log(f"[OPTUNA] {'🏆' if is_micro_highscore else '✅'} Discovery #{disc_n}: "
                f"{strategy}/{asset} PF={te3['pf']:.2f} AvgR={te3['avg_r']:+.3f} "
                f"n={te3['n']} MaxDD=${dd_usdt:.1f} Score={micro_score:.2f} DSR={dsr:.3f}")
            if is_micro_highscore:
                _update_highscore(conn, strategy, asset, regime, te3["pf"], fitness, disc_id)
                _notify_highscore(strategy, asset, regime, _round_params(cand["params"]),
                                  tr3, te3, fitness, prev_pf, disc_n,
                                  max_dd_r, micro_score, prev_micro, disc_id=disc_id)
            found += 1
        return found

    # ── Legacy Batch-Pfad (nicht mehr erreichbar — alle Strategien in OPTUNA_SPACES) ──
    start_ms = now_ms - DAYS * 86_400_000
    batch    = _batch(strategy, BATCH_SIZE)

    # ── Phase A: Alle Kombinationen der Runde evaluieren ─────────────────────
    # Kandidaten werden gesammelt statt sofort gespeichert, damit BH über die
    # gesamte Runde (Round-Level) angewendet werden kann, nicht nur per Batch.
    candidates = []   # [{h, params, window_results, pvalue}]

    for params in batch:
        h = _param_hash(strategy, asset, params)
        if _already_known(conn, h):
            continue

        window_results = []
        backtest_error = False
        te_res_w3      = None   # Fenster-3-Ergebnis für DSR-Berechnung
        for wcfg in WF_WINDOWS:
            train_end_ms  = now_ms + wcfg["train_end"]  * 86_400_000
            test_start_ms = now_ms + wcfg["test_start"] * 86_400_000
            test_end_ms   = now_ms + wcfg["test_end"]   * 86_400_000 if wcfg["test_end"] != 0 else now_ms
            try:
                tr_res = run_backtest(strategy, asset, start_ms,      train_end_ms, cfg=params, cooldown_bars=COOLDOWN_BARS, apply_costs=True)
                te_res = run_backtest(strategy, asset, test_start_ms, test_end_ms,  cfg=params, cooldown_bars=COOLDOWN_BARS, apply_costs=True)
            except Exception as e:
                log(f"[LAB-DAEMON] Backtest-Fehler {strategy}/{asset}: {e}")
                backtest_error = True
                break
            tr       = _metrics(tr_res)
            te       = _metrics(te_res, window_days=wcfg["days"])
            max_dd_r = _max_r_drawdown(te_res)
            passed, reason = _passes_window(tr, te, max_dd_r, wcfg)
            window_results.append({"tr": tr, "te": te, "max_dd_r": max_dd_r,
                                   "passed": passed, "reason": reason})
            if wcfg is WF_WINDOWS[-1]:
                te_res_w3 = te_res   # aktuellstes Fenster merken

        if backtest_error:
            continue

        _stat_inc(None, "total_tests")

        pnl_rs_w3 = [t.pnl_r for t in te_res_w3.trades] if te_res_w3 else []
        ok, result = _passes(window_results, pnl_rs_w3=pnl_rs_w3,
                             n_tested=len(candidates) + 1)
        if not ok:
            reason = result
            cat = _rejection_category(reason.split("_", 1)[-1] if "_" in reason else reason)
            _stat_inc(None, f"reject_{cat}")
            if "ruin_filter" in reason:
                log(f"[LAB-DAEMON] ☠️  Ruin-Filter: {strategy}/{asset} {reason}")
            continue

        dsr = result[1] if isinstance(result, tuple) else 0.0
        _stat_inc(None, "total_pass")

        # p-Wert aus Fenster-3-Metriken (deployment-relevantes Fenster)
        te3    = window_results[-1]["te"]
        pvalue = _pf_to_pvalue(te3["pf"], te3["n"])
        candidates.append({"h": h, "params": params,
                            "window_results": window_results, "pvalue": pvalue,
                            "dsr": dsr})

    if not candidates:
        return 0

    # ── Phase B: Benjamini-Hochberg über alle Kandidaten der Runde ───────────
    pvalues  = [c["pvalue"] for c in candidates]
    accepted = _benjamini_hochberg(pvalues, q=BH_FDR_Q)

    bh_rejected = sum(1 for a in accepted if not a)
    if bh_rejected:
        _stat_inc(None, "bh_rejected_today", bh_rejected)
        log(f"[LAB-DAEMON] BH-FDR (q={BH_FDR_Q}): {len(candidates)} Kandidaten → "
            f"{sum(accepted)} akzeptiert, {bh_rejected} verworfen")

    # ── Phase C: Nur BH-akzeptierte Kandidaten speichern ────────────────────
    found = 0
    for cand, is_accepted in zip(candidates, accepted):
        if not is_accepted:
            continue

        h              = cand["h"]
        params         = cand["params"]
        window_results = cand["window_results"]
        te3            = window_results[-1]["te"]
        max_dd_r       = window_results[-1]["max_dd_r"]
        tr3            = window_results[-1]["tr"]
        dsr            = cand.get("dsr")

        micro_score = _calc_micro_score(te3["pf"], te3["avg_r"], te3["wr"], te3["n"], max_dd_r)
        fitness     = _fitness(window_results)

        prev_pf, prev_fit  = _get_highscore(conn, strategy, asset, regime)
        prev_micro         = _get_best_micro_score(conn, strategy, asset, regime)
        is_micro_highscore = micro_score > prev_micro

        disc_id = _save_discovery(conn, h, strategy, asset, regime, params,
                                  tr3, te3, fitness, max_dd_r, micro_score,
                                  window_results=window_results, now_ms=now_ms,
                                  dsr=dsr)
        disc_n  = _count_discoveries(conn)

        dd_usdt     = max_dd_r * RISK_PER_TRADE
        regime_icon = _REGIME_ICON.get(regime, "?")
        log(
            f"[LAB-DAEMON] {'🏆' if is_micro_highscore else '✅'} Discovery #{disc_n}: "
            f"{strategy}/{asset} [{regime_icon}{regime}] "
            f"PF={te3['pf']:.2f} AvgR={te3['avg_r']:+.3f} n={te3['n']} "
            f"MaxDD=${dd_usdt:.1f}({max_dd_r:.1f}R) Score={micro_score:.2f} "
            f"DSR={dsr:.3f} [BH q={BH_FDR_Q}]"
            + (f" ← NEUER SCORE (war {prev_micro:.2f})" if is_micro_highscore else "")
        )

        if is_micro_highscore:
            _update_highscore(conn, strategy, asset, regime, te3["pf"], fitness, disc_id)
            _notify_highscore(strategy, asset, regime, _round_params(params),
                              tr3, te3, fitness, prev_pf, disc_n,
                              max_dd_r, micro_score, prev_micro, disc_id=disc_id)

        found += 1

    return found


def main(single_pass: bool = False):
    _ensure_schema()
    log("[LAB-DAEMON] ════════════════════════════════════════════════")
    n_trials = N_TRIALS_DAEMON if single_pass else N_TRIALS_FULL
    log("[LAB-DAEMON] APEX Auto-Lab Daemon v2 gestartet")
    log(f"[LAB-DAEMON] Suchraum: {[f'{s}/{a}' for s,a in SEARCH_SPACE]}")
    log(f"[LAB-DAEMON] Multi-Window OOS: {len(WF_WINDOWS)} Fenster — alle müssen bestehen")
    log(f"[LAB-DAEMON] Deploy-Filter: PF≥{MIN_PF_TEST} (Autopilot)")
    ruin_limit = STARTING_CAPITAL * MAX_DRAWDOWN_PERCENT
    log(f"[LAB-DAEMON] Ruin-Filter: MaxDD≤${ruin_limit:.0f} ({MAX_DRAWDOWN_PERCENT*100:.0f}% von ${STARTING_CAPITAL:.0f}) | Risiko=${RISK_PER_TRADE}/Trade")
    log(f"[LAB-DAEMON] Regime: EMA({REGIME_EMA_PERIOD}) Slope±{REGIME_SLOPE_PCT*100:.1f}%")
    log(f"[LAB-DAEMON] Log-Rotation: {_LOG_PATH} (max 10MB × 3)")
    log("[LAB-DAEMON] ════════════════════════════════════════════════")

    _send_telegram(
        f"🤖 *Auto\\-Lab Daemon v2 gestartet*\n"
        f"Suchraum: `{len(SEARCH_SPACE)}` Kombinationen\n"
        f"Regime: EMA\\({REGIME_EMA_PERIOD}\\) Slope±{REGIME_SLOPE_PCT*100:.1f}%\n"
        f"3\\-Fenster OOS: alle Fenster müssen bestehen\n"
        f"Deploy\\-Filter: PF≥{MIN_PF_TEST}\n"
        f"Ruin\\-Filter: MaxDD≤${STARTING_CAPITAL * MAX_DRAWDOWN_PERCENT:.0f} "
        f"\\(${RISK_PER_TRADE}/Trade\\)\n"
        f"Push: nur bei neuem Micro\\-Score\\-Rekord"
    )

    iteration = 0
    _last_heartbeat = time.time()
    _HEARTBEAT_INTERVAL = 6 * 3600  # 6 Stunden

    while True:
        iteration += 1
        now_ms = int(time.time() * 1000)

        try:
            # Sortierung mit kurzlebiger Verbindung — sofort schließen
            def _sort_key(target):
                _, asset = target
                c = get_connection()
                has_disc = c.execute(
                    "SELECT 1 FROM lab_discoveries WHERE asset=? LIMIT 1", (asset,)
                ).fetchone() is not None
                c.close()
                if asset in _PRIORITY_ASSETS and not has_disc:
                    return (0, random.random())
                if not has_disc:
                    return (1, random.random())
                return (2, random.random())

            requested = _load_requested_targets()
            combined  = list(dict.fromkeys(SEARCH_SPACE + requested))  # dedupliziert, Reihenfolge erhalten
            targets   = sorted(combined, key=_sort_key)

            requested_assets = {asset for _, asset in requested}

            found_this_round = 0
            for strategy, asset in targets:
                if strategy not in RANGES:
                    continue
                # Jedes Target bekommt eine eigene kurzlebige Verbindung
                conn = get_connection()
                try:
                    found = _run_one_target(strategy, asset, now_ms, conn,
                                           n_trials=n_trials)
                    found_this_round += found
                finally:
                    conn.close()
                # Kurze Pause zwischen Targets — gibt anderen Prozessen Luft
                time.sleep(0.05)

            # Angeforderte Assets die vollständig getestet wurden → status='done'
            if requested_assets:
                try:
                    c_req = get_connection()
                    for req_asset in requested_assets:
                        has_run = c_req.execute(
                            "SELECT 1 FROM research_runs WHERE asset=? LIMIT 1", (req_asset,)
                        ).fetchone()
                        if has_run:
                            c_req.execute(
                                "UPDATE asset_requests SET status='done' WHERE asset=? AND status='pending'",
                                (req_asset,),
                            )
                    c_req.commit()
                    c_req.close()
                except Exception as e:
                    log(f"[LAB-DAEMON] asset_requests update fehler: {e}")

            c2 = get_connection()
            disc_total = _count_discoveries(c2)
            c2.close()

            log(
                f"[LAB-DAEMON] Iteration #{iteration} | "
                f"Discoveries gesamt: {disc_total} | "
                f"Neue Funde: {found_this_round} | "
                f"Schlafe {SLEEP_BETWEEN}s"
            )

            # 6h-Heartbeat
            if time.time() - _last_heartbeat >= _HEARTBEAT_INTERVAL:
                c_hb = get_connection()
                hb_disc = _count_discoveries(c_hb)
                c_hb.close()
                _send_telegram(
                    f"✅ Lab alive \\| Iteration \\#{iteration} "
                    f"\\| Discoveries: {hb_disc}"
                )
                _last_heartbeat = time.time()

        except Exception as e:
            log(f"[LAB-DAEMON] Kritischer Fehler in Iteration #{iteration}: {e}")
            time.sleep(SLEEP_ON_ERROR)
            continue

        if single_pass:
            log("[LAB-DAEMON] --single-pass abgeschlossen")
            break
        time.sleep(SLEEP_BETWEEN)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--single-pass", action="store_true",
                        help="Einmalig alle Targets durchlaufen, dann beenden")
    args = parser.parse_args()
    main(single_pass=args.single_pass)
