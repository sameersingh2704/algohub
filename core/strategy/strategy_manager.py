"""
StrategyManager — Orchestrates all concurrent strategies.

Responsibilities:
  - Fan out MarketState to all registered strategies each bar
  - Gate new entries through PortfolioRisk
  - Manage per-strategy position lifecycle (entry fill, exit fill)
  - Route option registrations to all strategies

TradingSession replaces its single-strategy calls with:
    signal = strategy_manager.on_market_update(market_state, option_chain)
    exit   = strategy_manager.check_exits(market_state, option_prices)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from core.data.market_data_engine import MarketState
from core.strategy.base_strategy import BaseStrategy, StrategySignal, ExitDecision
from core.strategy.execution_selector import ExecutionSelector, SelectedOption
from core.risk.portfolio_risk import PortfolioRisk

logger = logging.getLogger(__name__)


@dataclass
class EntryInstruction:
    """Approved entry ready for order execution."""
    signal: StrategySignal
    instrument: SelectedOption
    lots: int
    quantity: int


@dataclass
class ExitInstruction:
    """Exit order to execute for a strategy position."""
    strategy_name: str
    trade_id: str
    decision: ExitDecision
    current_option_price: float


class StrategyManager:
    """
    Central coordinator for all concurrent strategies.

    Usage (called each bar by TradingSession):
        entries = await mgr.on_market_update(ms, option_chain, expiry)
        exits   = mgr.check_exits(ms, option_prices)
    """

    def __init__(
        self,
        strategies: List[BaseStrategy],
        portfolio_risk: PortfolioRisk,
        execution_selector: ExecutionSelector,
        lot_size: int = 75,
    ) -> None:
        self._strategies: Dict[str, BaseStrategy] = {s.name: s for s in strategies}
        self._portfolio_risk = portfolio_risk
        self._execution_selector = execution_selector
        self._lot_size = lot_size

    def on_market_update(
        self,
        market_state: MarketState,
        option_chain=None,
        instrument_mgr=None,
        market_data_engine=None,
        expiry: Optional[date] = None,
        regime_no_trade: bool = False,
    ) -> List[EntryInstruction]:
        """
        Fan market state to all strategies. Collect and gate entry signals.
        Returns approved EntryInstruction list (may be empty).

        When regime_no_trade=True, only strategies with regime_exempt=True
        are evaluated — all others are silently skipped.
        """
        approved: List[EntryInstruction] = []

        for strategy in self._strategies.values():
            # Regime gate: skip non-exempt strategies on NO_TRADE days
            if regime_no_trade and not strategy.regime_exempt:
                continue

            # Skip if already has position
            if strategy.has_position:
                continue

            signal = strategy.on_market_update(market_state, option_chain)
            if signal is None or not signal.is_valid:
                continue

            # Portfolio risk gate
            if instrument_mgr is None or expiry is None:
                logger.warning(f"StrategyManager: no instrument_mgr/expiry, skipping {strategy.name} entry")
                continue

            # Select instrument
            instrument = self._execution_selector.select_instrument(
                signal, instrument_mgr, market_data_engine, expiry
            )
            if instrument is None:
                logger.warning(f"StrategyManager: no instrument for {strategy.name} signal")
                continue

            # Lots capped at the risk-config hard limit (max_lots, default 1).
            lots = self._portfolio_risk._max_lots
            quantity = lots * self._lot_size

            allowed, reason = self._portfolio_risk.can_enter(
                strategy_name=strategy.name,
                direction=signal.direction,
                entry_premium=instrument.last_price or 1.0,
                quantity=lots,
                lot_size=self._lot_size,
            )

            if not allowed:
                logger.info(f"StrategyManager: {strategy.name} entry blocked — {reason}")
                continue

            approved.append(EntryInstruction(
                signal=signal,
                instrument=instrument,
                lots=lots,
                quantity=quantity,
            ))
            logger.info(
                f"StrategyManager: entry approved for {strategy.name} | "
                f"{signal.direction} {instrument.strike} @ {instrument.last_price:.2f}"
            )

        return approved

    def check_exits(
        self,
        market_state: MarketState,
        option_prices: Dict[str, float],  # token → current LTP
    ) -> List[ExitInstruction]:
        """
        Check all active positions for exit conditions.
        Returns ExitInstruction list for each position that should be closed.
        """
        exits: List[ExitInstruction] = []

        for strategy in self._strategies.values():
            if not strategy.has_position:
                continue
            pos = strategy.position
            if pos is None:
                continue

            current_price = option_prices.get(pos.option_token, 0.0)
            if current_price <= 0:
                continue

            decision = strategy.check_exit(market_state, current_price)
            if decision is not None:
                exits.append(ExitInstruction(
                    strategy_name=strategy.name,
                    trade_id=pos.trade_id,
                    decision=decision,
                    current_option_price=current_price,
                ))

        return exits

    def on_entry_filled(
        self,
        strategy_name: str,
        trade_id: str,
        fill_price: float,
        quantity: int,
        option_symbol: str,
        option_token: str,
        stop_price: float,
        target_price: float,
    ) -> None:
        """Notify strategy that its entry was filled."""
        strategy = self._strategies.get(strategy_name)
        if strategy:
            strategy.on_entry_filled(
                trade_id=trade_id,
                fill_price=fill_price,
                quantity=quantity,
                option_symbol=option_symbol,
                option_token=option_token,
                stop_price=stop_price,
                target_price=target_price,
            )
            self._portfolio_risk.register_entry(
                strategy_name=strategy_name,
                direction="CE" if "CE" in option_symbol else "PE",
                entry_premium=fill_price,
                quantity=quantity // self._lot_size,
                lot_size=self._lot_size,
            )

    def on_entry_failed(self, strategy_name: str) -> None:
        """Notify strategy that its entry could not be placed — roll back to ARMED."""
        strategy = self._strategies.get(strategy_name)
        if strategy:
            strategy.on_entry_failed()

    def on_exit_filled(
        self,
        strategy_name: str,
        fill_price: float,
        pnl: float,
        exit_reason: str,
    ) -> None:
        """Notify strategy that its exit was filled."""
        strategy = self._strategies.get(strategy_name)
        if strategy:
            strategy.on_exit_filled(fill_price, pnl, exit_reason)
        self._portfolio_risk.register_exit(strategy_name)

    def on_partial_exit_filled(
        self,
        strategy_name: str,
        fill_price: float,
        partial_quantity: int,
    ) -> None:
        strategy = self._strategies.get(strategy_name)
        if strategy:
            strategy.on_partial_exit_filled(fill_price, partial_quantity)

    def register_option(self, symbol: str, token: str) -> None:
        """Fan option registration to all strategies."""
        for strategy in self._strategies.values():
            strategy.register_option(symbol, token)

    def restore_closed_trade(self, strategy_name: str, net_pnl: float) -> None:
        """Replay a closed trade into a strategy's daily counters on session restore."""
        strategy = self._strategies.get(strategy_name)
        if strategy:
            strategy._daily_trades += 1
            if net_pnl >= 0:
                strategy._consecutive_losses = 0
            else:
                strategy._consecutive_losses += 1

    def restore_open_position(self, strategy_name: str, trade) -> None:
        """Restore an open position into a strategy so it can manage exits."""
        from core.strategy.base_strategy import StrategyState, PositionState
        strategy = self._strategies.get(strategy_name)
        if not strategy:
            return
        strategy._position = PositionState(
            strategy_name=strategy_name,
            trade_id=trade.trade_id,
            direction=trade.option_type,
            entry_price=trade.entry_price,
            entry_time=trade.entry_time or datetime.now(),
            quantity=trade.quantity,
            peak_price=trade.entry_price,
            stop_price=trade.entry_price * (1 - abs(strategy._config.get("exits", {}).get("hard_stop_pct", 0.30))),
            target_price=trade.entry_price * (1 + strategy._config.get("exits", {}).get("profit_target_pct", 0.45)),
            remaining_quantity=trade.quantity,
            option_symbol=trade.symbol,
            option_token=trade.token,
        )
        strategy.state = StrategyState.ACTIVE_POSITION
        self._portfolio_risk.register_entry(
            strategy_name=strategy_name,
            direction=trade.option_type,
            entry_premium=trade.entry_price,
            quantity=trade.lots,
            lot_size=self._lot_size,
        )
        logger.info(f"Restored open position for {strategy_name}: {trade.symbol} @ ₹{trade.entry_price:.2f}")

    def reset_daily(self) -> None:
        """Reset all strategies for new trading day."""
        for strategy in self._strategies.values():
            strategy.reset_daily()
        logger.info(f"StrategyManager: daily reset complete ({len(self._strategies)} strategies)")

    def halt_all(self) -> None:
        """Emergency halt — halt all strategies."""
        for strategy in self._strategies.values():
            strategy.halt()
        logger.warning("StrategyManager: ALL strategies halted")

    def get_status_summary(self) -> dict:
        """Returns status of all strategies for logging."""
        return {
            name: {
                "state": s.state.value,
                "has_position": s.has_position,
            }
            for name, s in self._strategies.items()
        }

    @property
    def strategies(self) -> Dict[str, BaseStrategy]:
        return dict(self._strategies)
