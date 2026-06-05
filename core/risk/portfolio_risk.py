"""
PortfolioRisk — Aggregate cross-strategy risk overlay.

Enforces:
  - Total premium exposure (sum of all active option positions)
  - Maximum concurrent open positions across strategies
  - Per-strategy one-trade-at-a-time (enforced here AND in BaseStrategy)

Called by StrategyManager before submitting any entry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class PositionRecord:
    strategy_name: str
    direction: str          # "CE" | "PE"
    entry_premium: float    # per-lot entry price
    quantity: int           # lots
    lot_size: int


class PortfolioRisk:
    """
    Tracks aggregate exposure across all concurrently active strategies.
    Call can_enter() before submitting any new entry order.
    Call register_entry() / register_exit() to keep state in sync.
    """

    def __init__(self, risk_config: dict, capital: float, lot_size: int = 75) -> None:
        per_trade = risk_config.get("per_trade", {})
        daily = risk_config.get("daily", {})
        self._max_premium_exposure_pct: float = per_trade.get("max_premium_exposure_pct", 0.02)
        self._max_concurrent_positions: int = daily.get("max_trades", 4)
        self._max_lots: int = per_trade.get("max_lots", 1)  # Hard cap: 1 lot per trade
        self._capital: float = capital
        self._default_lot_size: int = lot_size
        self._positions: Dict[str, PositionRecord] = {}  # strategy_name → record

    def can_enter(
        self,
        strategy_name: str,
        direction: str,
        entry_premium: float,
        quantity: int,
        lot_size: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """
        Returns (allowed, reason).
        reason is empty string when allowed=True.
        """
        ls = lot_size or self._default_lot_size

        if strategy_name in self._positions:
            return False, f"{strategy_name} already has an open position"

        if quantity > self._max_lots:
            return False, (
                f"lot size {quantity} exceeds max_lots={self._max_lots} — "
                f"reduce to {self._max_lots} lot(s)"
            )

        if len(self._positions) >= self._max_concurrent_positions:
            return False, (
                f"max concurrent positions ({self._max_concurrent_positions}) reached"
            )

        proposed_premium = entry_premium * quantity * ls
        current_premium = self._total_premium_deployed()
        max_allowed = self._capital * self._max_premium_exposure_pct

        if current_premium + proposed_premium > max_allowed:
            return False, (
                f"premium exposure limit: current={current_premium:.0f} "
                f"proposed={proposed_premium:.0f} max={max_allowed:.0f}"
            )

        return True, ""

    def register_entry(
        self,
        strategy_name: str,
        direction: str,
        entry_premium: float,
        quantity: int,
        lot_size: Optional[int] = None,
    ) -> None:
        ls = lot_size or self._default_lot_size
        self._positions[strategy_name] = PositionRecord(
            strategy_name=strategy_name,
            direction=direction,
            entry_premium=entry_premium,
            quantity=quantity,
            lot_size=ls,
        )
        logger.info(
            f"PortfolioRisk: registered entry for {strategy_name} | "
            f"{direction} premium={entry_premium:.2f} qty={quantity} "
            f"| active_positions={len(self._positions)}"
        )

    def register_exit(self, strategy_name: str) -> None:
        if strategy_name in self._positions:
            del self._positions[strategy_name]
            logger.info(
                f"PortfolioRisk: exit registered for {strategy_name} | "
                f"active_positions={len(self._positions)}"
            )

    def update_capital(self, new_capital: float) -> None:
        self._capital = new_capital

    def get_exposure_summary(self) -> dict:
        total = self._total_premium_deployed()
        max_allowed = self._capital * self._max_premium_exposure_pct
        return {
            "active_strategies": list(self._positions.keys()),
            "total_premium_deployed": round(total, 2),
            "max_premium_allowed": round(max_allowed, 2),
            "utilization_pct": round(total / max_allowed * 100, 1) if max_allowed > 0 else 0.0,
            "concurrent_positions": len(self._positions),
            "max_concurrent": self._max_concurrent_positions,
        }

    def _total_premium_deployed(self) -> float:
        return sum(
            r.entry_premium * r.quantity * r.lot_size
            for r in self._positions.values()
        )
