import json
from core.db import get_state, set_state
from core.utils import now_iso


def get_regime() -> dict:
    raw = get_state("regime")
    if raw:
        return json.loads(raw)
    return {"regime": "unknown", "risk_modifier": 0.5}


def set_regime(regime: str, risk_modifier: float, details: dict = None):
    payload = {"regime": regime, "risk_modifier": risk_modifier, "updated_at": now_iso()}
    if details:
        payload.update(details)
    set_state("regime", json.dumps(payload))


def get_hwm() -> float:
    raw = get_state("hwm")
    return float(raw) if raw else 0.0


def set_hwm(value: float):
    set_state("hwm", str(value))


def get_daily_pnl() -> dict:
    raw = get_state("daily_pnl")
    if raw:
        return json.loads(raw)
    return {"date": "", "pnl_r": 0.0, "pnl_usd": 0.0, "trades": 0}


def set_daily_pnl(date: str, pnl_r: float, pnl_usd: float, trades: int):
    set_state("daily_pnl", json.dumps({
        "date": date, "pnl_r": pnl_r, "pnl_usd": pnl_usd, "trades": trades
    }))


def get_strategy_mode(strategy: str, asset: str) -> str:
    from config.settings import STRATEGY_MODES
    return STRATEGY_MODES.get(strategy, {}).get(asset, "shadow")
