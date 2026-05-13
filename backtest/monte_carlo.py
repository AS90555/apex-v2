"""
Monte-Carlo-Permutationen für Backtest-Validierung (Phase 4).

Block-Bootstrap der Returns → 1000 Equity-Kurven → Perzentile + Ruin-Wahrscheinlichkeit.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass
class MCResult:
    n_paths:          int
    median_total_r:   float
    p5_total_r:       float
    p95_total_r:      float
    ruin_probability: float   # P(MaxDD ≤ RUIN_THRESHOLD)
    median_sharpe:    float


_RUIN_THRESHOLD = -5.0   # MaxDD < -5R gilt als Ruin


def _max_dd(rs: list[float]) -> float:
    cum = peak = 0.0
    worst = 0.0
    for r in rs:
        cum  += r
        peak  = max(peak, cum)
        worst = min(worst, cum - peak)
    return worst


def _sharpe(rs: list[float]) -> float:
    if len(rs) < 2:
        return 0.0
    m = sum(rs) / len(rs)
    v = sum((r - m) ** 2 for r in rs) / (len(rs) - 1)
    s = math.sqrt(v) if v > 0 else 0.0
    return m / s * math.sqrt(104) if s > 0 else 0.0


def run_monte_carlo(
    pnl_rs:     list[float],
    n_paths:    int = 1000,
    block_size: int = 10,
    seed:       int = 42,
) -> MCResult:
    """
    Block-Bootstrap: zieht `block_size`-große Blöcke aus pnl_rs mit Zurücklegen,
    bis Sequenz gleich lang wie Original ist. Wiederholt n_paths mal.

    Gibt MCResult mit Perzentilen (5/50/95) und Ruin-Wahrscheinlichkeit zurück.
    """
    rng = random.Random(seed)
    T   = len(pnl_rs)
    if T < 2:
        return MCResult(n_paths=0, median_total_r=0.0, p5_total_r=0.0,
                        p95_total_r=0.0, ruin_probability=1.0, median_sharpe=0.0)

    total_rs: list[float] = []
    sharpes:  list[float] = []
    ruin_count = 0

    for _ in range(n_paths):
        path: list[float] = []
        while len(path) < T:
            start = rng.randint(0, T - 1)
            block = pnl_rs[start:start + block_size]
            path.extend(block)
        path = path[:T]

        total = sum(path)
        total_rs.append(total)
        sharpes.append(_sharpe(path))
        if _max_dd(path) <= _RUIN_THRESHOLD:
            ruin_count += 1

    total_rs_s = sorted(total_rs)
    n          = len(total_rs_s)
    sharpes_s  = sorted(sharpes)

    return MCResult(
        n_paths          = n_paths,
        median_total_r   = round(total_rs_s[n // 2], 3),
        p5_total_r       = round(total_rs_s[max(0, int(n * 0.05))], 3),
        p95_total_r      = round(total_rs_s[min(n - 1, int(n * 0.95))], 3),
        ruin_probability = round(ruin_count / n_paths, 4),
        median_sharpe    = round(sharpes_s[n // 2], 4),
    )
