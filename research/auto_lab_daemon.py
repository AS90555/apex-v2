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

# ── Multi-Window OOS Validation ───────────────────────────────────────────────
# Ersetzt den früheren 70/30-Single-Split.
# Ein Setup muss ALLE 3 Fenster bestehen — kein Fenster kompensiert das andere.
# Offsets in Tagen relativ zu now_ms (negativ = Vergangenheit).
WF_WINDOWS = [
    # Fenster 1 (alt): 120 Tage OOS
    {"train_end": -480, "test_start": -480, "test_end": -360,
     "min_n": 30, "min_pf": 1.20, "min_avg_r": 0.06, "min_wr": 46.0,
     "ruin_filter": False, "weight": 1.0},
    # Fenster 2 (mittel): 120 Tage OOS
    {"train_end": -240, "test_start": -240, "test_end": -120,
     "min_n": 30, "min_pf": 1.20, "min_avg_r": 0.06, "min_wr": 46.0,
     "ruin_filter": False, "weight": 1.5},
    # Fenster 3 (aktuell): 60 Tage OOS — deployment-relevant, Ruin-Filter aktiv
    {"train_end": -60,  "test_start": -60,  "test_end": 0,
     "min_n": 20, "min_pf": 1.20, "min_avg_r": 0.06, "min_wr": 46.0,
     "ruin_filter": True, "weight": 2.0},
]
TOTAL_WEIGHT = sum(w["weight"] for w in WF_WINDOWS)

# Schwellen für Alpha-Library-Aufnahme (alle Fenster müssen bestehen)
MIN_PF_TEST         = 1.30   # Autopilot-Deploy-Filter (strenger als OOS-Gate)
MIN_PF_TRAIN        = 1.10
MAX_TRAIN_TEST_DROP = 0.40

# Monte-Carlo vs. Grid: 80% random sampling, 20% Grid-Abdeckung
MONTE_CARLO_FRAC = 0.80
BATCH_SIZE       = 20
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
    "pf_test":       "pf_zu_niedrig",
    "wr_test":       "wr_zu_niedrig",
    "avg_r_test":    "avg_r_zu_niedrig",
    "pf_train":      "train_pf_zu_niedrig",
    "overfit_drop":  "ueberfit",
    "ruin_filter":   "ruin_drawdown",
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
]

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
    for col, definition in [("max_dd_r", "REAL"), ("micro_score", "REAL")]:
        if col not in cols:
            conn.execute(f"ALTER TABLE lab_discoveries ADD COLUMN {col} {definition}")
            log(f"[LAB-DAEMON] DB migriert: Spalte {col} hinzugefügt")
    # Index jetzt anlegen (Spalte garantiert vorhanden)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_disc_asset_regime ON lab_discoveries(asset, market_regime)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_disc_micro_score ON lab_discoveries(micro_score DESC)")
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


def _metrics(result) -> dict:
    s = result.summary()
    n = s["trades"]
    if n == 0:
        return {"n": 0, "avg_r": 0.0, "pf": 0.0, "wr": 0.0}
    wins       = [t.pnl_r for t in result.trades if t.pnl_r > 0]
    losses     = [t.pnl_r for t in result.trades if t.pnl_r < 0]
    gross_win  = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_win / gross_loss if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)
    return {
        "n":     n,
        "avg_r": round(s["avg_r"], 4),
        "pf":    round(pf, 3),
        "wr":    round(s["winrate"], 2),
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


def _calc_micro_score(pf: float, max_dd_r: float) -> float:
    """
    Micro_Score = PF / (Max_Drawdown_USDT / STARTING_CAPITAL)
    Belohnt hohen Profit Factor bei minimalem Kontoeinbruch.
    Wenn max_dd_r = 0 (perfekte Strategie): Score = PF * 100 als Obergrenze.
    """
    max_dd_usdt = max_dd_r * RISK_PER_TRADE
    dd_ratio    = max_dd_usdt / STARTING_CAPITAL
    if dd_ratio <= 0:
        return round(pf * 100.0, 4)   # kein Drawdown → maximaler Bonus
    return round(pf / dd_ratio, 4)


def _passes_window(tr: dict, te: dict, max_dd_r: float, wcfg: dict) -> tuple[bool, str]:
    """Prüft ein einzelnes OOS-Fenster gegen die Fenster-spezifischen Schwellen."""
    if te["n"] < wcfg["min_n"]:
        return False, f"n_test={te['n']}<{wcfg['min_n']}"
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
    if wcfg["ruin_filter"]:
        max_dd_usdt = max_dd_r * RISK_PER_TRADE
        ruin_limit  = STARTING_CAPITAL * MAX_DRAWDOWN_PERCENT
        if max_dd_usdt > ruin_limit:
            return False, f"ruin_filter: dd={max_dd_usdt:.1f}$>{ruin_limit:.1f}$ ({max_dd_r:.1f}R)"
    return True, ""


def _passes(window_results: list[dict]) -> tuple[bool, str]:
    """Multi-Window OOS Validation: alle Fenster müssen bestehen."""
    for i, wr in enumerate(window_results):
        if not wr["passed"]:
            return False, f"w{i+1}_{wr['reason']}"
    return True, ""


# ── Telegram ─────────────────────────────────────────────────────────────────

def _send_telegram(text: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
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
                      prev_micro: float):
    icon       = _REGIME_ICON.get(regime, "❓")
    dd_usdt    = max_dd_r * RISK_PER_TRADE
    ruin_limit = STARTING_CAPITAL * MAX_DRAWDOWN_PERCENT
    param_lines = "\n".join(f"  `{k}` \\= `{v}`" for k, v in sorted(params.items()))
    msg = (
        f"🏆 *Neuer Micro\\-Score\\-Rekord\\!* \\(Discovery \\#{disc_n}\\)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 `{strategy}/{asset}`  {icon} `{regime}`\n"
        f"🎯 Score: `{prev_micro:.2f}` → *`{micro_score:.2f}`*\n\n"
        f"*Out\\-of\\-Sample \\(OOS\\):*\n"
        f"  📊 Trades:   *{te['n']}*\n"
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
                    max_dd_r: float, micro_score: float) -> int:
    cur = conn.execute(
        """INSERT OR IGNORE INTO lab_discoveries
           (discovered_at, params_hash, strategy, asset, market_regime, params_json,
            n_train, pf_train, avg_r_train,
            n_test,  pf_test,  avg_r_test,  wr_test,
            fitness_score, max_dd_r, micro_score, notified)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
        (
            datetime.now(timezone.utc).isoformat(), h, strategy, asset, regime,
            json.dumps(_round_params(params), sort_keys=True),
            tr["n"], tr["pf"], tr["avg_r"],
            te["n"], te["pf"], te["avg_r"], te["wr"],
            fitness, max_dd_r, micro_score,
        ),
    )
    conn.commit()
    return cur.lastrowid


def _count_discoveries(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM lab_discoveries").fetchone()[0]


# ── Lab-Stats Counter ─────────────────────────────────────────────────────────

def _stat_inc(conn, key: str, delta: int = 1) -> None:
    """Atomar einen Stats-Counter erhöhen (INSERT OR REPLACE)."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO lab_stats (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value = value + ?, updated_at = ?""",
        (key, delta, now, delta, now),
    )


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
                     AND micro_score > 0 AND wr_test >= 48.0 AND n_test >= 40
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


# ── Haupt-Loop ───────────────────────────────────────────────────────────────

def _run_one_target(strategy: str, asset: str, now_ms: int, conn) -> int:
    """Multi-Window OOS Validation: alle 3 Fenster müssen bestehen."""
    # Regime anhand des aktuellsten Fensters bestimmen (Fenster 3)
    w3 = WF_WINDOWS[-1]
    w3_test_start = now_ms + w3["test_start"] * 86_400_000
    regime = _detect_regime(asset, w3_test_start, now_ms)

    # Bucket-Limit prüfen
    if _bucket_count(conn, asset, regime) >= MAX_DISCOVERIES_PER_BUCKET:
        log(f"[LAB-DAEMON] Bucket {asset}/{regime} voll ({MAX_DISCOVERIES_PER_BUCKET}) — überspringe")
        return 0

    start_ms = now_ms - DAYS * 86_400_000
    batch = _batch(strategy, BATCH_SIZE)
    found = 0

    for params in batch:
        h = _param_hash(strategy, asset, params)
        if _already_known(conn, h):
            continue

        # ── 3 Fenster-Backtests ───────────────────────────────────────────────
        window_results = []
        backtest_error = False
        for wcfg in WF_WINDOWS:
            train_end_ms   = now_ms + wcfg["train_end"]   * 86_400_000
            test_start_ms  = now_ms + wcfg["test_start"]  * 86_400_000
            test_end_ms    = now_ms + wcfg["test_end"]    * 86_400_000 if wcfg["test_end"] != 0 else now_ms
            try:
                tr_res = run_backtest(strategy, asset, start_ms,     train_end_ms, cfg=params)
                te_res = run_backtest(strategy, asset, test_start_ms, test_end_ms,  cfg=params)
            except Exception as e:
                log(f"[LAB-DAEMON] Backtest-Fehler {strategy}/{asset}: {e}")
                backtest_error = True
                break
            tr = _metrics(tr_res)
            te = _metrics(te_res)
            max_dd_r = _max_r_drawdown(te_res)
            passed, reason = _passes_window(tr, te, max_dd_r, wcfg)
            window_results.append({"tr": tr, "te": te, "max_dd_r": max_dd_r,
                                   "passed": passed, "reason": reason})

        if backtest_error:
            continue

        # Stats: Gesamt-Testzähler erhöhen
        _stat_inc(conn, "total_tests")

        ok, reason = _passes(window_results)
        if not ok:
            cat = _rejection_category(reason.split("_", 1)[-1] if "_" in reason else reason)
            _stat_inc(conn, f"reject_{cat}")
            if "ruin_filter" in reason:
                log(f"[LAB-DAEMON] ☠️  Ruin-Filter: {strategy}/{asset} {reason}")
            continue

        # Stats: bestandener Test
        _stat_inc(conn, "total_pass")
        conn.commit()

        # Metriken aus Fenster 3 für Discovery-Eintrag und Deployment
        te3      = window_results[-1]["te"]
        max_dd_r = window_results[-1]["max_dd_r"]
        tr3      = window_results[-1]["tr"]

        micro_score = _calc_micro_score(te3["pf"], max_dd_r)
        fitness     = _fitness(window_results)

        prev_pf, prev_fit  = _get_highscore(conn, strategy, asset, regime)
        prev_micro         = _get_best_micro_score(conn, strategy, asset, regime)
        is_micro_highscore = micro_score > prev_micro

        disc_id = _save_discovery(conn, h, strategy, asset, regime, params,
                                  tr3, te3, fitness, max_dd_r, micro_score)
        disc_n  = _count_discoveries(conn)

        dd_usdt     = max_dd_r * RISK_PER_TRADE
        regime_icon = _REGIME_ICON.get(regime, "?")
        log(
            f"[LAB-DAEMON] {'🏆' if is_micro_highscore else '✅'} Discovery #{disc_n}: "
            f"{strategy}/{asset} [{regime_icon}{regime}] "
            f"PF={te3['pf']:.2f} AvgR={te3['avg_r']:+.3f} n={te3['n']} "
            f"MaxDD=${dd_usdt:.1f}({max_dd_r:.1f}R) Score={micro_score:.2f} [3-Window]"
            + (f" ← NEUER SCORE (war {prev_micro:.2f})" if is_micro_highscore else "")
        )

        if is_micro_highscore:
            _update_highscore(conn, strategy, asset, regime, te3["pf"], fitness, disc_id)
            _notify_highscore(strategy, asset, regime, _round_params(params),
                              tr3, te3, fitness, prev_pf, disc_n,
                              max_dd_r, micro_score, prev_micro)

        found += 1

    return found


def main():
    _ensure_schema()
    log("[LAB-DAEMON] ════════════════════════════════════════════════")
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
        f"Filter: PF≥{MIN_PF_TEST} | AvgR≥{MIN_AVG_R_TEST} | n≥{MIN_TRADES_TEST}\n"
        f"Ruin\\-Filter: MaxDD≤${STARTING_CAPITAL * MAX_DRAWDOWN_PERCENT:.0f} \\(${RISK_PER_TRADE}/Trade\\)\n"
        f"Push: nur bei neuem Micro\\-Score\\-Rekord"
    )

    iteration = 0

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

            targets = sorted(SEARCH_SPACE.copy(), key=_sort_key)

            found_this_round = 0
            for strategy, asset in targets:
                if strategy not in RANGES:
                    continue
                # Jedes Target bekommt eine eigene kurzlebige Verbindung
                conn = get_connection()
                try:
                    found = _run_one_target(strategy, asset, now_ms, conn)
                    found_this_round += found
                finally:
                    conn.close()
                # Kurze Pause zwischen Targets — gibt anderen Prozessen Luft
                time.sleep(0.05)

            c2 = get_connection()
            disc_total = _count_discoveries(c2)
            c2.close()

            log(
                f"[LAB-DAEMON] Iteration #{iteration} | "
                f"Discoveries gesamt: {disc_total} | "
                f"Neue Funde: {found_this_round} | "
                f"Schlafe {SLEEP_BETWEEN}s"
            )

        except Exception as e:
            log(f"[LAB-DAEMON] Kritischer Fehler in Iteration #{iteration}: {e}")
            time.sleep(SLEEP_ON_ERROR)
            continue

        time.sleep(SLEEP_BETWEEN)


if __name__ == "__main__":
    main()
