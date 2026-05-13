"""
GBM-Intrabar-Simulation für den Backtest-Exit.

Wenn keine 1m-Kerzen für einen Bar vorhanden sind, simuliert diese Funktion
N_PATHS geometrische Brownsche Bewegungen (GBM) durch den Bar und ermittelt
die wahrscheinlichste Exit-Reihenfolge (SL vs TP1 vs TP2).

Rückgabe:
  - exit_reason: 'sl' | 'tp1' | 'tp2' | None (kein Hit)
  - exit_price:  float

Kalibrierung: μ und σ werden aus den letzten N_CALIBRATION_CANDLES 1h-Bar-Returns
geschätzt (log-returns, annualisiert auf Bar-Länge skaliert).
"""

from __future__ import annotations

import math
import random
from typing import Optional


N_PATHS               = 500
N_CALIBRATION_CANDLES = 200
_SEED                 = 42   # Reproduzierbarkeit


def _calibrate(candles: list[dict], n: int = N_CALIBRATION_CANDLES) -> tuple[float, float]:
    """Schätzt (mu_per_bar, sigma_per_bar) aus log-returns der letzten n Candles."""
    subset = candles[-n:] if len(candles) >= n else candles
    if len(subset) < 2:
        return 0.0, 0.001
    log_returns = [
        math.log(subset[i]["close"] / subset[i - 1]["close"])
        for i in range(1, len(subset))
        if subset[i - 1]["close"] > 0 and subset[i]["close"] > 0
    ]
    if not log_returns:
        return 0.0, 0.001
    mu    = sum(log_returns) / len(log_returns)
    var   = sum((r - mu) ** 2 for r in log_returns) / max(len(log_returns) - 1, 1)
    sigma = math.sqrt(var)
    return mu, max(sigma, 1e-6)


def simulate_intrabar(
    entry_price: float,
    stop_loss:   float,
    take_profit_1: float,
    take_profit_2: float,
    direction:   str,
    bar_open:    float,
    bar_high:    float,
    bar_low:     float,
    candles_history: list[dict],
    n_steps:     int = 60,   # Schritte innerhalb des Bars (z.B. 60 = 1m-äquivalent)
    seed:        Optional[int] = _SEED,
) -> tuple[Optional[str], float]:
    """
    Simuliert N_PATHS GBM-Pfade durch einen Bar und gibt (exit_reason, exit_price) zurück.

    exit_reason ist None wenn kein Level getroffen wird (kein Exit in diesem Bar).
    Trifft ein Pfad mehrere Level, gewinnt das zuerst getroffene (zufällige Step-Reihenfolge).

    Die Entscheidung basiert auf der Mehrheit der Pfade: welcher Ausgang die meisten
    Treffer erzielt. Bei Gleichstand gewinnt die konservativere Schätzung (None > sl > tp1 > tp2).
    """
    rng = random.Random(seed)

    mu, sigma = _calibrate(candles_history)
    dt = 1.0 / n_steps

    # Skewness-Korrektur: Itô-Lemma Drift-Term
    drift = (mu - 0.5 * sigma ** 2) * dt
    vol   = sigma * math.sqrt(dt)

    counts: dict[Optional[str], int] = {None: 0, "sl": 0, "tp1": 0, "tp2": 0}

    for _ in range(N_PATHS):
        price = bar_open
        hit: Optional[str] = None

        for _ in range(n_steps):
            z     = rng.gauss(0, 1)
            price = price * math.exp(drift + vol * z)

            # Clamp auf bar_high/bar_low (GBM kann Bar-Grenzen nicht überschreiten)
            price = min(price, bar_high)
            price = max(price, bar_low)

            if direction == "long":
                if price <= stop_loss:
                    hit = "sl"
                    break
                if take_profit_2 and take_profit_2 > 0 and price >= take_profit_2:
                    hit = "tp2"
                    break
                if price >= take_profit_1:
                    hit = "tp1"
                    break
            else:  # short
                if price >= stop_loss:
                    hit = "sl"
                    break
                if take_profit_2 and take_profit_2 > 0 and price <= take_profit_2:
                    hit = "tp2"
                    break
                if price <= take_profit_1:
                    hit = "tp1"
                    break

        counts[hit] = counts.get(hit, 0) + 1

    # Majority-Vote mit Tie-Breaking (konservativ)
    best_reason = max(counts, key=lambda k: (counts[k], _priority(k)))

    exit_price_map = {
        "sl":  stop_loss,
        "tp1": take_profit_1,
        "tp2": take_profit_2 if take_profit_2 and take_profit_2 > 0 else take_profit_1,
        None:  0.0,
    }
    return best_reason, exit_price_map.get(best_reason, 0.0)


def _priority(reason: Optional[str]) -> int:
    """Konservativste Auflösung bei Gleichstand: None > sl > tp1 > tp2."""
    return {None: 4, "sl": 3, "tp1": 2, "tp2": 1}.get(reason, 0)
