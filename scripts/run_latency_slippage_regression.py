"""
Latenz-Slippage-Regression pro Asset (v7 Phase 4).

Berechnet lineare Regression: slippage_bps ~ signal_to_fill_ms
Output: research/findings/latency_slippage/{date}.json
Schreibt empfohlene Toleranz in asset_execution_calibration.

Cron: 1x/Woche (z.B. Sonntag 03:00 UTC).
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_connection
from core.utils import log

MIN_SAMPLES = 20
OUTLIER_P95_MS = 30_000  # 30s — Ausreißer-Cap für Latenz


def _linear_regression(x: list[float], y: list[float]) -> tuple[float, float, float, float]:
    """OLS: y = slope * x + intercept. Gibt (slope, intercept, r_squared, stderr_slope) zurück."""
    n = len(x)
    if n < 2:
        return 0.0, 0.0, 0.0, 0.0

    mx = sum(x) / n
    my = sum(y) / n
    sxx = sum((xi - mx) ** 2 for xi in x)
    sxy = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    syy = sum((yi - my) ** 2 for yi in y)

    if sxx == 0:
        return 0.0, my, 0.0, 0.0

    slope = sxy / sxx
    intercept = my - slope * mx
    ss_res = sum((yi - (slope * xi + intercept)) ** 2 for xi, yi in zip(x, y))
    r_squared = 1.0 - ss_res / syy if syy > 0 else 0.0

    # Standard Error des Slope
    if n > 2 and ss_res >= 0 and sxx > 0:
        s2 = ss_res / (n - 2)
        stderr_slope = math.sqrt(s2 / sxx)
    else:
        stderr_slope = 0.0

    return slope, intercept, max(0.0, r_squared), stderr_slope


def _confidence_interval_95(slope: float, stderr: float, n: int) -> tuple[float, float]:
    """Annäherung 95%-CI via t-Quantil (t ≈ 2 für n>30, sonst konservativ)."""
    t = 2.0 if n >= 30 else 2.5
    return slope - t * stderr, slope + t * stderr


def _recommended_tolerance(slope: float, intercept: float, p95_latency_ms: float) -> float:
    """
    Empfehle Slippage-Toleranz als erwartete Slippage bei P95-Latenz + 10 bps Puffer.
    Minimum 5 bps, Maximum 50 bps.
    """
    expected_bps = max(0.0, slope * p95_latency_ms + intercept)
    tolerance = expected_bps + 10.0
    return max(5.0, min(50.0, tolerance))


def run_regression(asset: str, conn) -> dict | None:
    """Führt Regression für ein Asset durch. Gibt None zurück bei unzureichenden Daten."""
    rows = conn.execute(
        """SELECT signal_to_fill_ms, slippage_bps
           FROM trades
           WHERE asset=?
             AND signal_to_fill_ms IS NOT NULL
             AND slippage_bps IS NOT NULL
             AND signal_to_fill_ms > 0
             AND signal_to_fill_ms <= ?
           ORDER BY entry_ts DESC
           LIMIT 500""",
        (asset, OUTLIER_P95_MS),
    ).fetchall()

    if len(rows) < MIN_SAMPLES:
        log(f"[LatencyRegr] {asset}: nur {len(rows)} Samples (min={MIN_SAMPLES}) — überspringe")
        return None

    x = [r["signal_to_fill_ms"] for r in rows]
    y = [r["slippage_bps"] for r in rows]

    slope, intercept, r2, stderr = _linear_regression(x, y)
    ci_lo, ci_hi = _confidence_interval_95(slope, stderr, len(rows))

    # P95 Latenz aus Stichprobe
    x_sorted = sorted(x)
    p95_idx = int(0.95 * len(x_sorted))
    p95_latency = x_sorted[min(p95_idx, len(x_sorted) - 1)]

    tolerance = _recommended_tolerance(slope, intercept, p95_latency)

    return {
        "asset": asset,
        "n_samples": len(rows),
        "slope_bps_per_ms": round(slope, 6),
        "intercept_bps": round(intercept, 4),
        "r_squared": round(r2, 4),
        "ci_95_lo": round(ci_lo, 6),
        "ci_95_hi": round(ci_hi, 6),
        "p95_latency_ms": round(p95_latency, 1),
        "recommended_tolerance_bps": round(tolerance, 2),
    }


def save_calibration(result: dict, conn) -> None:
    conn.execute(
        """INSERT INTO asset_execution_calibration
               (asset, slippage_slope_bps_per_ms, r_squared,
                recommended_tolerance_bps, n_samples, updated_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(asset) DO UPDATE SET
               slippage_slope_bps_per_ms=excluded.slippage_slope_bps_per_ms,
               r_squared=excluded.r_squared,
               recommended_tolerance_bps=excluded.recommended_tolerance_bps,
               n_samples=excluded.n_samples,
               updated_at=excluded.updated_at""",
        (
            result["asset"],
            result["slope_bps_per_ms"],
            result["r_squared"],
            result["recommended_tolerance_bps"],
            result["n_samples"],
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def main() -> None:
    conn = get_connection()

    assets = [
        r["asset"]
        for r in conn.execute(
            "SELECT DISTINCT asset FROM trades WHERE signal_to_fill_ms IS NOT NULL"
        ).fetchall()
    ]

    if not assets:
        log("[LatencyRegr] Keine Trades mit signal_to_fill_ms — nichts zu tun")
        conn.close()
        return

    results = []
    for asset in assets:
        res = run_regression(asset, conn)
        if res:
            save_calibration(res, conn)
            results.append(res)
            log(
                f"[LatencyRegr] {asset}: slope={res['slope_bps_per_ms']:.4f} bps/ms "
                f"R²={res['r_squared']:.3f} n={res['n_samples']} "
                f"tolerance={res['recommended_tolerance_bps']:.1f} bps"
            )

    conn.close()

    if not results:
        log("[LatencyRegr] Keine auswertbaren Assets")
        return

    out_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "research", "findings", "latency_slippage",
    )
    os.makedirs(out_dir, exist_ok=True)

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = os.path.join(out_dir, f"{date_str}.json")

    with open(out_path, "w") as f:
        json.dump({"generated_at": datetime.now(timezone.utc).isoformat(), "results": results}, f, indent=2)

    log(f"[LatencyRegr] Report geschrieben: {out_path} ({len(results)} Assets)")


if __name__ == "__main__":
    main()
