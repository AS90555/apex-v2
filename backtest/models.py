"""Shared Backtest-Dataclasses — kein Circular-Import-Risiko."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BtSignal:
    ts:            int
    strategy:      str
    asset:         str
    direction:     str   # 'long' | 'short'
    entry_price:   float
    stop_loss:     float
    take_profit_1: float
    take_profit_2: float
    size:          float
    risk_usd:      float


@dataclass
class BtTrade:
    signal:      BtSignal
    entry_ts:    int
    exit_ts:     Optional[int]   = None
    exit_price:  Optional[float] = None
    exit_reason: Optional[str]   = None   # 'sl', 'tp1', 'tp1_be_sl', 'tp2', 'timeout'
    pnl_usd:     float           = 0.0
    pnl_r:       float           = 0.0
    # Partial-TP-Felder (v6)
    tp1_hit:          bool           = False
    remaining_size:   float          = 0.0
    realized_pnl_tp1: float          = 0.0
    be_sl_active:     bool           = False
    intrabar_model_used: str         = "static"   # 'static' | '1m_zoom' | 'gbm'

    @property
    def closed(self) -> bool:
        return self.exit_ts is not None


@dataclass
class BtResult:
    strategy: str
    asset:    str
    trades:   list[BtTrade] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl_r > 0)

    @property
    def winrate(self) -> float:
        return self.wins / self.total if self.total > 0 else 0.0

    @property
    def total_r(self) -> float:
        return sum(t.pnl_r for t in self.trades)

    @property
    def avg_r(self) -> float:
        return self.total_r / self.total if self.total > 0 else 0.0
