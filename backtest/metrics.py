"""
Quant-Grade-Metriken für Walk-Forward-Validation (Phase 4).

Alle Funktionen sind pure Python, kein numpy/pandas.
DSR und phi_inv sind aus auto_lab_daemon.py extrahiert und zentralisiert.
"""

from __future__ import annotations

import math
from typing import Optional


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float], ddof: int = 1) -> float:
    if len(xs) <= ddof:
        return 0.0
    m   = _mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - ddof)
    return math.sqrt(var)


def _normal_cdf(z: float) -> float:
    """Cumulative standard normal via erfc approximation."""
    return 0.5 * math.erfc(-z / math.sqrt(2))


def phi_inv(p: float) -> float:
    """Inverse standard normal (Abramowitz & Stegun)."""
    p = max(1e-9, min(1 - 1e-9, p))
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


# ── Core-Metriken ─────────────────────────────────────────────────────────────

def sharpe(pnl_rs: list[float], trades_per_year: int = 104) -> float:
    """Annualisierter Sharpe Ratio (ddof=1)."""
    if len(pnl_rs) < 2:
        return 0.0
    m = _mean(pnl_rs)
    s = _std(pnl_rs, ddof=1)
    if s <= 0:
        return 0.0
    return round(m / s * math.sqrt(trades_per_year), 4)


def sortino(pnl_rs: list[float], trades_per_year: int = 104) -> float:
    """Sortino Ratio: nur negative Returns im Nenner."""
    if len(pnl_rs) < 2:
        return 0.0
    m = _mean(pnl_rs)
    neg = [r for r in pnl_rs if r < 0]
    if not neg:
        return float("inf")
    downside = math.sqrt(sum(r ** 2 for r in neg) / len(neg))
    if downside <= 0:
        return 0.0
    return round(m / downside * math.sqrt(trades_per_year), 4)


def max_drawdown(pnl_rs: list[float]) -> float:
    """Max Drawdown als negative Zahl (in R-Einheiten, kumulativ)."""
    if not pnl_rs:
        return 0.0
    peak  = 0.0
    cum   = 0.0
    worst = 0.0
    for r in pnl_rs:
        cum  += r
        peak  = max(peak, cum)
        dd    = cum - peak
        worst = min(worst, dd)
    return round(worst, 4)


def calmar(pnl_rs: list[float], trades_per_year: int = 104) -> float:
    """Calmar = Ann. Return / |MaxDD|. Gibt 0 wenn MaxDD = 0."""
    if not pnl_rs:
        return 0.0
    ann_return = _mean(pnl_rs) * trades_per_year
    mdd = abs(max_drawdown(pnl_rs))
    if mdd <= 0:
        return 0.0
    return round(ann_return / mdd, 4)


def dsr(pnl_rs: list[float], n_tested: int = 1) -> float:
    """
    Deflated Sharpe Ratio (Bailey & López de Prado 2014).
    Zentralisiert aus auto_lab_daemon._calc_dsr().
    Gibt P(SR_hat > SR_benchmark) in [0, 1].
    """
    T = len(pnl_rs)
    if T < 10:
        return 0.0

    m    = _mean(pnl_rs)
    std  = _std(pnl_rs, ddof=1)
    if std <= 0:
        return 0.0

    trades_per_year = 104
    sr_hat = m / std * math.sqrt(trades_per_year)

    gamma3 = gamma4 = 0.0
    if T >= 3:
        gamma3 = sum((r - m) ** 3 for r in pnl_rs) / (T * std ** 3)
        gamma4 = sum((r - m) ** 4 for r in pnl_rs) / (T * std ** 4) - 3.0
        gamma4 = max(gamma4, 0.0)

    N = max(n_tested, 1)
    gamma_e = 0.5772156649
    sr_benchmark = (
        (1 - gamma_e) * phi_inv(1 - 1 / N)
        + gamma_e     * phi_inv(1 - 1 / (N * math.e))
    )

    denom_sq = 1 - gamma3 * sr_hat + gamma4 * sr_hat ** 2 / 4
    if denom_sq <= 0 or T <= 1:
        return 0.0

    z = (sr_hat - sr_benchmark) * math.sqrt(T - 1) / math.sqrt(denom_sq)
    return round(_normal_cdf(z), 6)


def pbo(is_returns: list[list[float]], oos_returns: list[list[float]]) -> float:
    """
    Probability of Backtest Overfitting via CSCV (Bailey et al. 2017, v7 Phase 2).

    Combinatorial Symmetric Cross-Validation:
      - N Fold-Paare (IS, OOS) aus Walk-Forward oder Optuna-Trials.
      - Bildet alle Kombinationen von N//2 Folds als "IS-Subsets".
      - Pro Kombination: identifiziere IS-bestes Fold, prüfe ob dessen OOS-
        Performance ≤ Median aller OOS-Sharpes (= Overfitting).
      - PBO = P(IS-beste Auswahl ist im OOS unterdurchschnittlich).

    Gibt Wert in [0, 1] zurück; niedrig = kein Overfitting-Signal.
    Bei < 4 Folds: Rückgabe 0.5 (neutral — zu wenig Daten).
    """
    n = len(is_returns)
    if n < 4:
        return 0.5

    is_sharpes  = [sharpe(r) for r in is_returns]
    oos_sharpes = [sharpe(r) for r in oos_returns]

    # Median aller OOS-Sharpes (Gesamt-Benchmark)
    oos_sorted = sorted(oos_sharpes)
    oos_median = oos_sorted[n // 2]

    # Alle Kombinationen von n//2 Fold-Indizes als "IS-Subset"
    # Kombinatorisch: C(n, n//2) — für n≤16 praktikabel (max C(16,8)=12870)
    from itertools import combinations
    half = n // 2
    dominated_count = 0
    total_combos    = 0

    for is_idx in combinations(range(n), half):
        oos_idx = [i for i in range(n) if i not in is_idx]

        # IS-bestes Fold in diesem Subset
        best_local_is = max(is_idx, key=lambda i: is_sharpes[i])

        # OOS-Sharpe des IS-besten Folds
        oos_of_best = oos_sharpes[best_local_is]

        # OOS-Median dieses Subsets
        local_oos_sharpes = sorted(oos_sharpes[i] for i in oos_idx)
        local_median = local_oos_sharpes[len(local_oos_sharpes) // 2]

        if oos_of_best < local_median:
            dominated_count += 1
        total_combos += 1

    if total_combos == 0:
        return 0.5

    return round(dominated_count / total_combos, 4)


def stability_score(
    pnl_rs: list[float],
    base_params: dict,
    param_variations: list[dict],
    sharpe_fn=None,
) -> float:
    """
    Parameter-Stabilitäts-Score: 1 - (std(Sharpes über Variationen) / mean(|Sharpes|)).
    Höher = stabiler. Erwartet vorberechnete Sharpe-Werte pro Variation.
    sharpe_fn: Funktion die (pnl_rs) → sharpe liefert; default: metrics.sharpe
    """
    if sharpe_fn is None:
        sharpe_fn = sharpe
    if not param_variations:
        return 1.0
    sharpes = [sharpe_fn(r) for r in param_variations]
    if not sharpes:
        return 1.0
    mean_abs = _mean([abs(s) for s in sharpes])
    if mean_abs <= 0:
        return 0.0
    std_s = _std(sharpes, ddof=1)
    return round(max(0.0, 1.0 - std_s / mean_abs), 4)
