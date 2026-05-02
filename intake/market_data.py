"""
Zentraler Data Intake Agent.

Einziger Ort, der Marktdaten von Bitget abruft und in die `candles`-Tabelle schreibt.
Keine Strategie darf direkt die API aufrufen — alle lesen aus SQLite.

Gap-Erkennung:
  1. Trailing-Gap: MAX(ts) → wie viele aktuelle Kerzen fehlen?
  2. Interior-Gap: alle Timestamps prüfen → Sprünge > 1.5 × interval_ms füllen
  Beide Typen werden in einem Lauf erkannt und geschlossen.
"""

import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from core.db import get_connection
from core.utils import log

INTERVAL_MS = {
    "1m":  60_000,
    "3m":  180_000,
    "5m":  300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h":  3_600_000,
    "2h":  7_200_000,
    "4h":  14_400_000,
    "6h":  21_600_000,
    "12h": 43_200_000,
    "1d":  86_400_000,
}

BITGET_MAX_LIMIT = 200
INITIAL_LIMIT    = 210


def _now_ms() -> int:
    return int(time.time() * 1000)


def _get_all_timestamps(conn, asset: str, interval: str) -> list[int]:
    """Alle bekannten Timestamps für (asset, interval), aufsteigend sortiert."""
    rows = conn.execute(
        "SELECT ts FROM candles WHERE asset=? AND interval=? ORDER BY ts",
        (asset, interval),
    ).fetchall()
    return [r[0] for r in rows]


def _store_candles(conn, asset: str, interval: str, candles: list, fetched_at: str) -> int:
    inserted = 0
    for c in candles:
        cur = conn.execute(
            """INSERT OR IGNORE INTO candles(asset, interval, ts, open, high, low, close, volume, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (asset, interval, c["time"],
             c["open"], c["high"], c["low"], c["close"], c["volume"], fetched_at),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted


def _find_gaps(timestamps: list[int], interval_ms: int) -> list[tuple[int, int]]:
    """
    Sucht Lücken in einer aufsteigend sortierten Timestamp-Liste.
    Eine Lücke liegt vor wenn next_ts - prev_ts > 1.5 × interval_ms.
    Gibt Liste von (gap_start_ms, gap_end_ms) zurück:
      gap_start = erste fehlende Kerze (prev_ts + interval_ms)
      gap_end   = letzte fehlende Kerze (next_ts - interval_ms)
    """
    gaps = []
    threshold = int(interval_ms * 1.5)
    for i in range(len(timestamps) - 1):
        diff = timestamps[i + 1] - timestamps[i]
        if diff > threshold:
            gap_start = timestamps[i] + interval_ms
            gap_end   = timestamps[i + 1] - interval_ms
            gaps.append((gap_start, gap_end))
    return gaps


def _fill_gap(client, asset: str, interval: str, interval_ms: int,
              gap_start: int, gap_end: int, fetched_at: str) -> int:
    """Füllt eine konkrete Lücke: API-Call OHNE offene Connection, dann kurze Write-Connection."""
    missing = max(1, int((gap_end - gap_start) / interval_ms) + 1)
    limit   = min(missing + 2, BITGET_MAX_LIMIT)
    log(f"[INTAKE] {asset}/{interval}: Lücke {gap_start}→{gap_end} ({missing} Kerzen) → fetch {limit}")
    try:
        candles = client.get_candles(
            coin=asset, interval=interval, limit=limit,
            start_time=gap_start - 1, end_time=gap_end + interval_ms,
        )
        conn = get_connection()
        inserted = _store_candles(conn, asset, interval, candles, fetched_at)
        conn.close()
        return inserted
    except Exception as e:
        log(f"[INTAKE] FEHLER beim Gap-Füllen {asset}/{interval} {gap_start}→{gap_end}: {e}")
        return 0


def fetch_and_store(client, asset: str, interval: str) -> dict:
    """
    Holt fehlende Kerzen für (asset, interval) und speichert sie in SQLite.

    Connection-Strategie: Connection wird nur für DB-Operationen geöffnet,
    vor jedem API-Call geschlossen → kein langer Schreiblock während HTTP-Wartezeit.
    """
    interval_ms = INTERVAL_MS.get(interval)
    if not interval_ms:
        return {"error": f"Unbekanntes Intervall: {interval}"}

    now_ms     = _now_ms()
    fetched_at = datetime.now(timezone.utc).isoformat()

    total_fetched  = 0
    total_inserted = 0
    gaps_found     = 0

    # ── Timestamps lesen (kurze Read-Connection) ──────────────────────────────
    conn = get_connection()
    timestamps = _get_all_timestamps(conn, asset, interval)
    conn.close()

    # ── Initialload ───────────────────────────────────────────────────────────
    if not timestamps:
        log(f"[INTAKE] {asset}/{interval}: Kein Datensatz → Initialload {INITIAL_LIMIT} Kerzen")
        try:
            candles = client.get_candles(coin=asset, interval=interval, limit=INITIAL_LIMIT)
        except Exception as e:
            log(f"[INTAKE] FEHLER Initialload {asset}/{interval}: {e}")
            return {"asset": asset, "interval": interval, "error": str(e)}
        conn = get_connection()
        ins = _store_candles(conn, asset, interval, candles, fetched_at)
        timestamps = _get_all_timestamps(conn, asset, interval)
        conn.close()
        total_fetched  += len(candles)
        total_inserted += ins

    # ── Interior-Gaps finden und füllen ───────────────────────────────────────
    gaps = _find_gaps(timestamps, interval_ms)
    for gap_start, gap_end in gaps:
        gaps_found += 1
        ins = _fill_gap(client, asset, interval, interval_ms, gap_start, gap_end, fetched_at)
        total_inserted += ins

    # ── Timestamps nach Gap-Fixes refreshen (kurze Read-Connection) ──────────
    conn = get_connection()
    timestamps = _get_all_timestamps(conn, asset, interval)
    conn.close()
    last_ts = timestamps[-1] if timestamps else None

    # ── Trailing-Gap (fehlende aktuelle Kerzen nach MAX(ts)) ──────────────────
    if last_ts is not None:
        trailing_gap = int((now_ms - last_ts) / interval_ms)
        if trailing_gap > 1:
            start_time = last_ts + interval_ms
            limit      = min(trailing_gap + 2, BITGET_MAX_LIMIT)
            log(f"[INTAKE] {asset}/{interval}: Trailing-Gap {trailing_gap} Kerzen → fetch {limit} ab {start_time}")
            try:
                # API-Call OHNE offene DB-Connection
                candles = client.get_candles(
                    coin=asset, interval=interval,
                    limit=limit, start_time=start_time,
                )
                # Dann kurze Write-Connection
                conn = get_connection()
                ins = _store_candles(conn, asset, interval, candles, fetched_at)
                conn.close()
                total_fetched  += len(candles)
                total_inserted += ins
            except Exception as e:
                log(f"[INTAKE] FEHLER Trailing-Gap {asset}/{interval}: {e}")

    # ── Letzten Timestamp + letzten Close für Rückgabe lesen ─────────────────
    conn = get_connection()
    new_ts_list = _get_all_timestamps(conn, asset, interval)
    last_row = conn.execute(
        "SELECT ts, close FROM candles WHERE asset=? AND interval=? ORDER BY ts DESC LIMIT 1",
        (asset, interval),
    ).fetchone()
    conn.close()
    new_last_ts  = new_ts_list[-1] if new_ts_list else None
    last_close   = last_row[1] if last_row else None
    last_close_ts = last_row[0] if last_row else None

    # Warnung wenn letzter Candle älter als 3h (Feature-Berechnungen werden unzuverlässig)
    if last_close_ts:
        age_min = (_now_ms() - last_close_ts) / 60_000
        if age_min > 180:
            log(f"[INTAKE] ⚠️ {asset}/{interval}: letzter Candle {age_min:.0f} Min alt — Features unzuverlässig")

    if total_inserted > 0 or gaps_found > 0:
        log(f"[INTAKE] {asset}/{interval}: fetched={total_fetched} inserted={total_inserted} gaps={gaps_found}")

    return {
        "asset":       asset,
        "interval":    interval,
        "fetched":     total_fetched,
        "inserted":    total_inserted,
        "gaps_found":  gaps_found,
        "last_ts":     new_last_ts,
        "last_close":  last_close,       # Fallback-Preis für Feature-Agent
        "last_close_age_min": round((_now_ms() - last_close_ts) / 60_000, 1) if last_close_ts else None,
    }


def run_intake(client, intake_matrix: dict) -> list:
    results = []
    for asset, intervals in intake_matrix.items():
        for interval in intervals:
            result = fetch_and_store(client, asset, interval)
            results.append(result)
    return results


def cleanup_old_candles(ttl_days: int = 30) -> int:
    cutoff_ms = _now_ms() - ttl_days * 86_400_000
    conn = get_connection()
    cur  = conn.execute("DELETE FROM candles WHERE ts < ? AND source='bitget'", (cutoff_ms,))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    if deleted:
        log(f"[INTAKE] Cleanup: {deleted} alte Kerzen gelöscht (>{ttl_days}d)")
    return deleted
