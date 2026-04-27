from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Candle:
    asset: str
    interval: str
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    fetched_at: str = ""


@dataclass
class Signal:
    strategy: str
    asset: str
    direction: str
    mode: str
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit_1: Optional[float] = None
    take_profit_2: Optional[float] = None
    size: Optional[float] = None
    risk_usd: Optional[float] = None
    session: Optional[str] = None
    created_at: str = ""
    status: str = "pending"
    reject_reason: Optional[str] = None
    id: Optional[int] = None


@dataclass
class Trade:
    signal_id: Optional[int]
    strategy: str
    asset: str
    direction: str
    mode: str
    entry_price: Optional[float] = None
    entry_ts: Optional[str] = None
    size: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit_1: Optional[float] = None
    take_profit_2: Optional[float] = None
    exit_price: Optional[float] = None
    exit_ts: Optional[str] = None
    exit_reason: Optional[str] = None
    pnl_usd: Optional[float] = None
    pnl_r: Optional[float] = None
    be_applied: int = 0
    order_id: Optional[str] = None
    context_json: Optional[str] = None
    id: Optional[int] = None


@dataclass
class Heartbeat:
    component: str
    status: str
    ts: str = ""
    message: Optional[str] = None
    latency_ms: Optional[float] = None
