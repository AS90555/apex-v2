"""
Walk-Forward-Engine mit Purge/Embargo (Phase 4).

Splits die Zeitreihe in IS/OOS-Fenster und verhindert Look-Ahead-Bias durch:
  - Purge-Gap  = max Feature-Lookback (Standard: 200 Bars)
  - Embargo-Gap = durchschnittliche Trade-Haltedauer (Standard: 8 Bars)

Liefert List[FoldResult] mit IS/OOS-Metriken pro Fold.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from backtest.engine import run_backtest, BtResult
from backtest.metrics import sharpe, sortino, max_drawdown, calmar, dsr
from core.utils import log


@dataclass
class FoldResult:
    fold_idx:    int
    is_start:    int   # ms
    is_end:      int
    oos_start:   int
    oos_end:     int
    n_is:        int   = 0
    n_oos:       int   = 0
    sharpe_is:   float = 0.0
    sharpe_oos:  float = 0.0
    sortino_oos: float = 0.0
    max_dd_oos:  float = 0.0
    calmar_oos:  float = 0.0
    dsr_oos:     float = 0.0
    total_r_is:  float = 0.0
    total_r_oos: float = 0.0
    pf_is:       float = 0.0
    pf_oos:      float = 0.0
    wr_oos:      float = 0.0
    passed:      bool  = False


@dataclass
class WalkForwardResult:
    strategy:   str
    asset:      str
    folds:      list[FoldResult] = field(default_factory=list)

    @property
    def n_folds(self) -> int:
        return len(self.folds)

    @property
    def mean_sharpe_oos(self) -> float:
        vals = [f.sharpe_oos for f in self.folds if f.n_oos > 0]
        return sum(vals) / len(vals) if vals else 0.0

    @property
    def mean_dsr_oos(self) -> float:
        vals = [f.dsr_oos for f in self.folds if f.n_oos > 0]
        return sum(vals) / len(vals) if vals else 0.0

    @property
    def worst_max_dd(self) -> float:
        vals = [f.max_dd_oos for f in self.folds if f.n_oos > 0]
        return min(vals) if vals else 0.0

    @property
    def all_oos_pnl_rs(self) -> list[float]:
        rs = []
        for f in self.folds:
            rs.extend(getattr(f, "_oos_pnl_rs", []))
        return rs


def _pf(pnl_rs: list[float]) -> float:
    wins  = sum(r for r in pnl_rs if r > 0)
    losses = abs(sum(r for r in pnl_rs if r < 0))
    return round(wins / losses, 4) if losses > 0 else (float("inf") if wins > 0 else 1.0)


def _wr(pnl_rs: list[float]) -> float:
    if not pnl_rs:
        return 0.0
    return sum(1 for r in pnl_rs if r > 0) / len(pnl_rs)


def run_walk_forward(
    strategy:     str,
    asset:        str,
    start_ts:     int,
    end_ts:       int,
    cfg:          dict,
    is_bars:      int   = 4320,   # ~6 Monate bei 1h
    oos_bars:     int   = 720,    # ~1 Monat bei 1h
    step_bars:    int   = 720,    # Walk-Forward-Step
    purge_bars:   int   = 200,    # Feature-Lookback-Puffer
    embargo_bars: int   = 8,      # Ø Trade-Haltedauer
    interval_ms:  int   = 3_600_000,
    cooldown_bars: int  = 8,
    apply_costs:  bool  = True,
) -> WalkForwardResult:
    """
    Führt einen Walk-Forward-Backtest mit Purge und Embargo durch.

    Fenster-Schema:
      IS:  [fold_start, is_end)
      Gap: [is_end, is_end + purge_ms + embargo_ms)   ← nie verwendet
      OOS: [oos_start, oos_end)

    Jeder Fold schreitet um step_bars vor.
    """
    result = WalkForwardResult(strategy=strategy, asset=asset)

    purge_ms   = purge_bars   * interval_ms
    embargo_ms = embargo_bars * interval_ms
    is_ms      = is_bars      * interval_ms
    oos_ms     = oos_bars     * interval_ms
    step_ms    = step_bars    * interval_ms

    fold_start = start_ts
    fold_idx   = 0

    while True:
        is_end    = fold_start + is_ms
        oos_start = is_end + purge_ms + embargo_ms
        oos_end   = oos_start + oos_ms

        if oos_end > end_ts:
            break

        log(f"[WF] Fold {fold_idx}: IS [{_ts(fold_start)}→{_ts(is_end)}] "
            f"OOS [{_ts(oos_start)}→{_ts(oos_end)}] (purge+embargo={purge_bars+embargo_bars} bars)")

        # IS-Backtest
        bt_is  = run_backtest(strategy, asset, fold_start, is_end,
                              cfg=cfg, cooldown_bars=cooldown_bars, apply_costs=apply_costs)
        # OOS-Backtest
        bt_oos = run_backtest(strategy, asset, oos_start, oos_end,
                              cfg=cfg, cooldown_bars=cooldown_bars, apply_costs=apply_costs)

        rs_is  = [t.pnl_r for t in bt_is.trades]
        rs_oos = [t.pnl_r for t in bt_oos.trades]

        fold = FoldResult(
            fold_idx   = fold_idx,
            is_start   = fold_start,
            is_end     = is_end,
            oos_start  = oos_start,
            oos_end    = oos_end,
            n_is       = len(rs_is),
            n_oos      = len(rs_oos),
            sharpe_is  = sharpe(rs_is),
            sharpe_oos = sharpe(rs_oos),
            sortino_oos= sortino(rs_oos),
            max_dd_oos = max_drawdown(rs_oos),
            calmar_oos = calmar(rs_oos),
            dsr_oos    = dsr(rs_oos, n_tested=1),
            total_r_is = round(sum(rs_is), 4),
            total_r_oos= round(sum(rs_oos), 4),
            pf_is      = _pf(rs_is),
            pf_oos     = _pf(rs_oos),
            wr_oos     = round(_wr(rs_oos), 4),
        )
        # Versteckte Liste für all_oos_pnl_rs
        fold._oos_pnl_rs = rs_oos  # type: ignore[attr-defined]

        result.folds.append(fold)
        fold_idx  += 1
        fold_start += step_ms

    log(f"[WF] {strategy}/{asset}: {fold_idx} Folds abgeschlossen. "
        f"Ø Sharpe OOS={result.mean_sharpe_oos:.3f}, Ø DSR={result.mean_dsr_oos:.3f}")
    return result


def _ts(ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
