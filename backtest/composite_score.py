"""
Composite-Score für Discovery-Promotion (Phase 4 / v7 Phase 3).

Gewichtete Summe aus OOS-Sharpe, DSR, MaxDD (neg.), Stability, PBO (neg.).
Gewichte in config/settings.py::COMPOSITE_WEIGHTS.

v7: composite_score() gibt zusätzlich weights_hash zurück (SHA256 über
Gewichte + Version). Wird in lab_discoveries.composite_weights_hash gespeichert.

Changelog:
  v7.0 (2026-05-13): initiale Versionierung; Gewichte unverändert zu v6.
  Künftige Änderungen hier eintragen: Datum, was geändert, warum.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass
class CompositeInput:
    sharpe_oos:      float
    dsr:             float
    max_drawdown:    float   # negative Zahl (z.B. -3.5)
    stability_score: float   # [0,1]
    pbo:             float   # [0,1] — niedriger = besser
    n_oos:           int


def weights_hash(weights: dict, version: str) -> str:
    """SHA256 über Gewichte + Version-String."""
    payload = json.dumps({"version": version, "weights": weights},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def composite_score(
    inp: CompositeInput,
    weights: dict | None = None,
) -> float:
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


def composite_score_with_hash(
    inp: CompositeInput,
    weights: dict | None = None,
) -> tuple[float, str]:
    """
    Wie composite_score(), gibt aber (score, weights_hash) zurück.

    weights_hash wird in lab_discoveries.composite_weights_hash gespeichert
    und ermöglicht nachträgliche Audit-Trails bei Gewichts-Änderungen.
    """
    if weights is None:
        from config.settings import COMPOSITE_WEIGHTS, COMPOSITE_WEIGHTS_VERSION
        weights = COMPOSITE_WEIGHTS
        version = COMPOSITE_WEIGHTS_VERSION
    else:
        version = "custom"

    score = composite_score(inp, weights)
    w_hash = weights_hash(weights, version)
    return score, w_hash


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))
