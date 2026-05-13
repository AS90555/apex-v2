"""
Composite-Score für Discovery-Promotion (Phase 4).

Gewichtete Summe aus OOS-Sharpe, DSR, MaxDD (neg.), Stability, PBO (neg.).
Gewichte in config/settings.py::COMPOSITE_WEIGHTS.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CompositeInput:
    sharpe_oos:      float
    dsr:             float
    max_drawdown:    float   # negative Zahl (z.B. -3.5)
    stability_score: float   # [0,1]
    pbo:             float   # [0,1] — niedriger = besser
    n_oos:           int


def composite_score(inp: CompositeInput, weights: dict | None = None) -> float:
    """
    Berechnet den Composite-Score.

    Default-Gewichte aus config/settings.py.
    Score ist normalisiert auf [-1, +1]-ähnlichen Bereich.
    """
    if weights is None:
        from config.settings import COMPOSITE_WEIGHTS
        weights = COMPOSITE_WEIGHTS

    if inp.n_oos < 10:
        return 0.0

    # Normierte Komponenten
    sharpe_norm  = _clamp(inp.sharpe_oos / 3.0, -1.0, 1.0)
    dsr_norm     = _clamp(inp.dsr, 0.0, 1.0)
    dd_norm      = _clamp(1.0 + inp.max_drawdown / 10.0, 0.0, 1.0)  # MaxDD -10R = 0
    stab_norm    = _clamp(inp.stability_score, 0.0, 1.0)
    pbo_penalty  = _clamp(inp.pbo, 0.0, 1.0)   # niedrig = gut → invertiert

    score = (
        weights.get("sharpe",    0.30) * sharpe_norm
      + weights.get("dsr",       0.25) * dsr_norm
      + weights.get("max_dd",    0.20) * dd_norm
      + weights.get("stability", 0.15) * stab_norm
      + weights.get("pbo",       0.10) * (1.0 - pbo_penalty)
    )
    return round(score, 4)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))
