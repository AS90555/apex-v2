"""
Portfolio-Exposure-Engine (Phase 6).

Prüft vor jedem Signal:
  - Max-Exposure pro Asset
  - Max-Exposure pro Asset-Cluster (z. B. L1-Coins)
  - Max Total Open Risk
  - Korrelations-Budget
"""
from __future__ import annotations

from typing import Tuple

from core.db import get_connection
from core.models import Signal
from core.utils import log
from config.settings import (
    PORTFOLIO_MAX_EXPOSURE_USDT,
    CLUSTER_MAP,
    MAX_OPEN_RISK_USDT,
    PORTFOLIO_MAX_CLUSTER_USDT,
    PORTFOLIO_CORR_LIMIT,
)
from governance.gate import BaseGovernanceCheck


class PortfolioExposureCheck(BaseGovernanceCheck):
    """Blockiert Signale die Portfolio-Grenzen verletzen würden."""

    @property
    def name(self) -> str:
        return "portfolio_exposure"

    def evaluate(self, signal: Signal) -> Tuple[bool, str]:
        if signal.mode == "shadow":
            return True, "shadow_skip"

        conn = get_connection()
        try:
            return self._check(signal, conn)
        finally:
            conn.close()

    def _check(self, signal: Signal, conn) -> Tuple[bool, str]:
        # Bestehende offene Positionen aus active_deployments + trades
        open_trades = conn.execute(
            """SELECT t.asset, t.size, t.entry_price, t.side
               FROM trades t
               JOIN active_deployments d ON t.strategy_key = d.strategy_key
               WHERE t.status IN ('executed', 'open') AND d.active = 1"""
        ).fetchall()

        # Asset-Exposure: Summe |size * entry_price| pro Asset
        asset_exposure: dict[str, float] = {}
        total_exposure = 0.0
        for t in open_trades:
            asset   = t["asset"]
            notional = abs((t["size"] or 0) * (t["entry_price"] or 0))
            asset_exposure[asset] = asset_exposure.get(asset, 0) + notional
            total_exposure += notional

        signal_notional = abs((signal.size or 0) * (signal.entry_price or 0))
        signal_asset    = signal.asset

        # Prüfung 1: Max Exposure pro Asset
        projected_asset = asset_exposure.get(signal_asset, 0) + signal_notional
        if projected_asset > PORTFOLIO_MAX_EXPOSURE_USDT:
            return False, (
                f"portfolio_exposure: {signal_asset} "
                f"{projected_asset:.0f} > {PORTFOLIO_MAX_EXPOSURE_USDT:.0f} USDT"
            )

        # Prüfung 2: Max Total Open Risk
        if total_exposure + signal_notional > MAX_OPEN_RISK_USDT:
            return False, (
                f"portfolio_exposure: total "
                f"{total_exposure + signal_notional:.0f} > {MAX_OPEN_RISK_USDT:.0f} USDT"
            )

        # Prüfung 3: Cluster-Limit
        signal_cluster = CLUSTER_MAP.get(signal_asset, "other")
        cluster_exposure = 0.0
        for asset, exp in asset_exposure.items():
            if CLUSTER_MAP.get(asset, "other") == signal_cluster:
                cluster_exposure += exp
        projected_cluster = cluster_exposure + signal_notional
        if projected_cluster > PORTFOLIO_MAX_CLUSTER_USDT:
            return False, (
                f"portfolio_exposure: cluster={signal_cluster} "
                f"{projected_cluster:.0f} > {PORTFOLIO_MAX_CLUSTER_USDT:.0f} USDT"
            )

        # Prüfung 4: Korrelations-Budget (vereinfacht: gleiche Richtung × Cluster-Size)
        same_dir_cluster_exp = 0.0
        for t in open_trades:
            if (CLUSTER_MAP.get(t["asset"], "other") == signal_cluster
                    and t["side"] == signal.direction):
                same_dir_cluster_exp += abs((t["size"] or 0) * (t["entry_price"] or 0))
        if same_dir_cluster_exp + signal_notional > PORTFOLIO_CORR_LIMIT:
            return False, (
                f"portfolio_exposure: korrelations_budget cluster={signal_cluster} "
                f"dir={signal.direction} "
                f"{same_dir_cluster_exp + signal_notional:.0f} > {PORTFOLIO_CORR_LIMIT:.0f} USDT"
            )

        return True, (
            f"portfolio_exposure=ok asset={projected_asset:.0f} "
            f"total={total_exposure + signal_notional:.0f} "
            f"cluster={signal_cluster}:{projected_cluster:.0f}"
        )
