"""
Feature Agent — Phase 5

Liest Candles aus SQLite, berechnet Indikatoren und schreibt sie in die `features`-Tabelle.

Regeln:
  - Point-in-Time-Korrektheit: Jeder Feature-Wert ist verknüpft mit dem as_of_ts der Kerze.
    Die Berechnung nutzt ausschließlich Daten, die ZUM ZEITPUNKT dieser Kerze bekannt waren
    (d.h. alle Kerzen mit ts <= as_of_ts). Kein Look-Ahead-Bias.
  - Idempotenz: INSERT OR IGNORE — kein Überschreiben existierender Werte bei Neustart.
  - Zentralisierung: Alle Indikator-Logiken laufen hier, nicht in den Strategie-Skripten.

Feature-Namensschema: "{indicator}_{param}_{interval}"
  Beispiele: ema_200_15m, atr_14_1h, vol_sma_20_5m, is_squeeze_15m
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from core.db import get_connection
from core.utils import log
from features.indicators import (
    ema, sma, atr_wilder, atr_sma, vol_sma, body_sma, is_squeeze,
)

# ── Feature-Definitionen ──────────────────────────────────────────────────────
# Jeder Eintrag: (feature_name, interval, min_candles, compute_fn)
# compute_fn(candles: list[dict]) -> float | None

def _ema_200(candles):
    return ema([c["close"] for c in candles], 200)

def _ema_50(candles):
    return ema([c["close"] for c in candles], 50)

def _ema_20(candles):
    return ema([c["close"] for c in candles], 20)

def _atr_14(candles):
    return atr_wilder(candles, 14)

def _atr_sma_20(candles):
    return atr_sma(candles, atr_period=14, sma_period=20)

def _vol_sma_20(candles):
    return vol_sma(candles, 20)

def _vol_sma_50(candles):
    return vol_sma(candles, 50)

def _body_sma_50(candles):
    return body_sma(candles, 50)

def _squeeze(candles):
    return 1.0 if is_squeeze(candles, 20) else 0.0


# (feature_name_template, interval, min_candles_required, fn)
FEATURE_DEFS: list[tuple[str, str, int, callable]] = [
    # 15m-Features (für ORB)
    ("ema_200_15m",    "15m", 210, _ema_200),
    ("ema_50_15m",     "15m", 60,  _ema_50),
    ("atr_14_15m",     "15m", 15,  _atr_14),
    ("vol_sma_20_15m", "15m", 21,  _vol_sma_20),
    ("is_squeeze_15m", "15m", 22,  _squeeze),

    # 1h-Features (für VAA + KDT)
    ("ema_200_1h",     "1h",  210, _ema_200),
    ("ema_50_1h",      "1h",  60,  _ema_50),
    ("ema_20_1h",      "1h",  25,  _ema_20),
    ("atr_14_1h",      "1h",  15,  _atr_14),
    ("atr_sma_20_1h",  "1h",  36,  _atr_sma_20),
    ("vol_sma_20_1h",  "1h",  21,  _vol_sma_20),
    ("vol_sma_50_1h",  "1h",  51,  _vol_sma_50),
    ("body_sma_50_1h", "1h",  51,  _body_sma_50),

    # 4h-Features (für ORB H-006 H4-Alignment)
    ("ema_50_4h",      "4h",  60,  _ema_50),
    ("atr_14_4h",      "4h",  15,  _atr_14),

    # 5m-Features (für ORB Volume-Filter H-014)
    ("vol_sma_20_5m",  "5m",  21,  _vol_sma_20),
]


def _load_candles(conn, asset: str, interval: str, up_to_ts: int, limit: int) -> list[dict]:
    """
    Lädt bis zu `limit` Candles für (asset, interval) mit ts <= up_to_ts.
    Point-in-Time: ausschließlich Daten die zu diesem Zeitpunkt bekannt waren.
    """
    rows = conn.execute(
        """SELECT ts, open, high, low, close, volume
           FROM candles
           WHERE asset=? AND interval=? AND ts<=?
           ORDER BY ts DESC
           LIMIT ?""",
        (asset, interval, up_to_ts, limit),
    ).fetchall()
    # Umkehren: älteste zuerst (wie Indikatoren es erwarten)
    return [
        {"time": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}
        for r in reversed(rows)
    ]


def _upsert_feature(conn, asset: str, interval: str, ts: int, name: str, value: float):
    """INSERT OR IGNORE — idempotent, kein Überschreiben bei Neustart."""
    conn.execute(
        """INSERT OR IGNORE INTO features(asset, interval, ts, feature_name, value)
           VALUES (?,?,?,?,?)""",
        (asset, interval, ts, name, round(value, 8) if value is not None else None),
    )


def compute_features_for_asset(asset: str, intervals: list[str] | None = None) -> dict:
    """
    Berechnet alle ausstehenden Features für ein Asset.

    Für jede (asset, interval)-Kombination aus FEATURE_DEFS:
      1. Alle Candle-Timestamps laden die noch kein Feature dieser Art haben
      2. Für jeden Timestamp: Candles bis zu diesem Punkt laden + Indikator berechnen
      3. Ergebnis idempotent in `features` schreiben

    Gibt Summary-Dict zurück: {feature_name: computed_count}
    """
    conn        = get_connection()
    summary     = {}
    now_iso     = datetime.now(timezone.utc).isoformat()

    # Gruppiere FEATURE_DEFS nach Interval
    by_interval: dict[str, list] = {}
    for feat_name, interval, min_candles, fn in FEATURE_DEFS:
        if intervals and interval not in intervals:
            continue
        by_interval.setdefault(interval, []).append((feat_name, min_candles, fn))

    for interval, feats in by_interval.items():
        # Alle Timestamps für dieses Asset/Interval aus candles
        all_ts_rows = conn.execute(
            "SELECT ts FROM candles WHERE asset=? AND interval=? ORDER BY ts",
            (asset, interval),
        ).fetchall()
        all_ts = [r[0] for r in all_ts_rows]

        if not all_ts:
            continue

        # Pro Feature: welche Timestamps fehlen noch?
        for feat_name, min_candles, fn in feats:
            # Bereits berechnete Timestamps für dieses Feature
            existing = {r[0] for r in conn.execute(
                "SELECT ts FROM features WHERE asset=? AND interval=? AND feature_name=?",
                (asset, interval, feat_name),
            ).fetchall()}

            missing_ts = [ts for ts in all_ts if ts not in existing]
            if not missing_ts:
                continue

            computed = 0
            for ts in missing_ts:
                # Point-in-Time: nur Candles bis einschließlich dieses Timestamps
                # min_candles + 50 Puffer für saubere Indikator-Konvergenz
                candles = _load_candles(conn, asset, interval, ts, min_candles + 50)

                if len(candles) < min_candles:
                    # Zu wenig Warmup-Daten → None speichern (als Sentinel)
                    _upsert_feature(conn, asset, interval, ts, feat_name, None)
                    continue

                try:
                    value = fn(candles)
                    _upsert_feature(conn, asset, interval, ts, feat_name, value)
                    computed += 1
                except Exception as e:
                    log(f"[FEATURES] FEHLER {feat_name} {asset}/{interval} ts={ts}: {e}")
                    _upsert_feature(conn, asset, interval, ts, feat_name, None)

            if computed > 0:
                log(f"[FEATURES] {asset}/{interval}/{feat_name}: {computed} neu berechnet")
            summary[f"{asset}/{interval}/{feat_name}"] = computed

            conn.commit()  # pro Feature-Typ committen (kein riesiger Rollback)

    conn.close()
    return summary


def get_features(asset: str, interval: str, as_of_ts: int) -> dict[str, float | None]:
    """
    Liest alle berechneten Features für (asset, interval) zum Zeitpunkt as_of_ts.
    Gibt dict {feature_name: value} zurück.
    Für Strategien: rufen Sie dies statt direkt Candles zu laden.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT feature_name, value FROM features WHERE asset=? AND interval=? AND ts=?",
        (asset, interval, as_of_ts),
    ).fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


def run_all_features(intake_matrix: dict) -> dict:
    """
    Berechnet Features für alle Assets aus der INTAKE_MATRIX.
    Entry-Point für run_features.py (Cron).
    """
    total = {}
    for asset, intervals in intake_matrix.items():
        result = compute_features_for_asset(asset, intervals)
        total.update(result)
    return total
