"""
Order Manager
==============
Manages complete order lifecycle in paper mode:
- Order creation and tracking
- Fill processing
- Position updates
- P&L tracking per trade

In live mode (Phase 5), this routes to the real broker connector.
Currently hard-wired to paper simulator.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Callable

from core.broker.angel_connector import (
    OrderType, TransactionType, ProductType, PlaceOrderRequest
)
from core.execution.paper_simulator import PaperSimulator, SimulatedFill
from core.expenses.expense_engine import ExpenseEngine, TradeCharges

logger = logging.getLogger(__name__)


@dataclass
class TradeRecord:
    """Complete record of a round-trip trade."""
    trade_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_date: date = field(default_factory=date.today)
    symbol: str = ""
    token: str = ""
    option_type: str = ""
    strike: int = 0
    expiry: date = field(default_factory=date.today)
    lots: int = 0
    quantity: int = 0

    # Entry
    entry_order_id: str = ""
    entry_time: Optional[datetime] = None
    entry_price: float = 0.0
    entry_spot: float = 0.0
    entry_vwap: float = 0.0
    entry_iv: float = 0.0
    entry_delta: float = 0.0
    entry_oi: int = 0
    signal_id: str = ""
    signal_reason: str = ""

    # Exit
    exit_order_id: str = ""
    exit_time: Optional[datetime] = None
    exit_price: float = 0.0
    exit_reason: str = ""

    # P&L
    gross_pnl: float = 0.0
    total_charges: float = 0.0
    net_pnl: float = 0.0
    pnl_pct: float = 0.0  # % on premium

    # Status
    status: str = "OPEN"  # OPEN / CLOSED
    strategy_name: str = ""  # Which strategy originated this trade

    # Simulation details
    entry_slippage: float = 0.0
    exit_slippage: float = 0.0
    entry_latency_ms: int = 0
    exit_latency_ms: int = 0


class OrderManager:
    """
    Manages paper trading order lifecycle.

    Entry flow:
    1. execute_entry() → creates order → paper simulator fills
    2. on_fill() → updates trade record, creates position

    Exit flow:
    1. execute_exit() → creates sell order → paper simulator fills
    2. on_fill() → closes trade record, calculates P&L
    """

    def __init__(
        self,
        paper_simulator: PaperSimulator,
        expense_engine: ExpenseEngine,
    ) -> None:
        self._simulator = paper_simulator
        self._expense_engine = expense_engine

        self._open_trades: Dict[str, TradeRecord] = {}  # trade_id → TradeRecord
        self._strategy_positions: Dict[str, str] = {}   # strategy_name → trade_id
        self._closed_trades: List[TradeRecord] = []
        self._on_trade_open_callbacks: List[Callable[[TradeRecord], None]] = []
        self._on_trade_close_callbacks: List[Callable[[TradeRecord], None]] = []

    async def execute_entry(
        self,
        symbol: str,
        token: str,
        option_type: str,
        strike: int,
        expiry: date,
        lots: int,
        quantity: int,
        limit_price: float,
        spot_price: float,
        vwap: float,
        signal_id: str,
        signal_reason: str,
        iv: float = 0.0,
        delta: float = 0.0,
        oi: int = 0,
        strategy_name: str = "",
    ) -> Optional[TradeRecord]:
        """
        Execute paper entry order.
        Returns TradeRecord if fill succeeds, None if rejected.
        """
        request = PlaceOrderRequest(
            symbol=symbol,
            token=token,
            exchange="NFO",
            transaction_type=TransactionType.BUY,
            order_type=OrderType.LIMIT,
            quantity=quantity,
            price=limit_price,
        )

        fill = await self._simulator.place_order(request)

        if fill.rejected or fill.filled_quantity == 0:
            logger.warning(
                f"Entry rejected: {symbol} — {fill.rejection_reason}"
            )
            return None

        trade = TradeRecord(
            symbol=symbol,
            token=token,
            option_type=option_type,
            strike=strike,
            expiry=expiry,
            lots=lots,
            quantity=fill.filled_quantity,
            entry_order_id=fill.order_id,
            entry_time=fill.timestamp,
            entry_price=fill.average_fill_price,
            entry_spot=spot_price,
            entry_vwap=vwap,
            entry_iv=iv,
            entry_delta=delta,
            entry_oi=oi,
            signal_id=signal_id,
            signal_reason=signal_reason,
            entry_slippage=fill.slippage,
            entry_latency_ms=fill.latency_ms,
            status="OPEN",
            strategy_name=strategy_name,
        )

        self._open_trades[trade.trade_id] = trade
        if strategy_name:
            self._strategy_positions[strategy_name] = trade.trade_id

        logger.info(
            f"ENTRY FILLED: {symbol} {quantity}qty @ ₹{fill.average_fill_price:.2f} "
            f"(slip=₹{fill.slippage:.2f} lat={fill.latency_ms}ms)",
            extra={"trade_id": trade.trade_id}
        )

        for cb in self._on_trade_open_callbacks:
            try:
                cb(trade)
            except Exception as e:
                logger.error(f"Trade open callback error: {e}")

        return trade

    async def execute_exit(
        self,
        trade_id: str,
        current_price: float,
        exit_reason: str,
        spot_price: float = 0.0,
    ) -> Optional[TradeRecord]:
        """
        Execute paper exit order for an open trade.
        Returns completed TradeRecord or None if trade not found.
        """
        trade = self._open_trades.get(trade_id)
        if not trade:
            logger.error(f"Cannot exit: trade {trade_id} not found in open trades")
            return None

        # Use market sell order (bid-based fill in simulator)
        request = PlaceOrderRequest(
            symbol=trade.symbol,
            token=trade.token,
            exchange="NFO",
            transaction_type=TransactionType.SELL,
            order_type=OrderType.MARKET,
            quantity=trade.quantity,
        )

        fill = await self._simulator.place_order(request)

        if fill.rejected:
            logger.error(
                f"Exit order rejected for trade {trade_id}: {fill.rejection_reason}. "
                f"FORCING exit at current price {current_price:.2f}"
            )
            # Force exit at current price even on rejection (safety mechanism)
            fill.average_fill_price = current_price * 0.998  # Apply 0.2% force-exit penalty
            fill.filled_quantity = trade.quantity
            fill.rejected = False
            fill.rejection_reason = "FORCE_EXIT_ON_REJECTION"

        trade.exit_order_id = fill.order_id
        trade.exit_time = fill.timestamp
        trade.exit_price = fill.average_fill_price
        trade.exit_reason = exit_reason
        trade.exit_slippage = fill.slippage
        trade.exit_latency_ms = fill.latency_ms
        trade.status = "CLOSED"

        # Calculate P&L
        trade.gross_pnl = (trade.exit_price - trade.entry_price) * trade.quantity

        # Calculate charges
        charges = self._expense_engine.calculate_round_trip(
            trade_id=trade.trade_id,
            buy_price=trade.entry_price,
            sell_price=trade.exit_price,
            quantity=trade.quantity,
            session_date=trade.session_date,
        )
        trade.total_charges = charges.total_charges
        trade.net_pnl = trade.gross_pnl - trade.total_charges
        trade.pnl_pct = (
            (trade.exit_price - trade.entry_price) / trade.entry_price
            if trade.entry_price > 0 else 0.0
        )

        # Move to closed
        del self._open_trades[trade_id]
        if trade.strategy_name in self._strategy_positions:
            del self._strategy_positions[trade.strategy_name]
        self._closed_trades.append(trade)

        won = trade.net_pnl > 0
        logger.info(
            f"EXIT FILLED: {trade.symbol} @ ₹{fill.average_fill_price:.2f} "
            f"reason={exit_reason} "
            f"gross=₹{trade.gross_pnl:.2f} charges=₹{trade.total_charges:.2f} "
            f"net=₹{trade.net_pnl:.2f} ({trade.pnl_pct*100:.1f}%) "
            f"{'WIN' if won else 'LOSS'}",
            extra={"trade_id": trade_id}
        )

        for cb in self._on_trade_close_callbacks:
            try:
                cb(trade)
            except Exception as e:
                logger.error(f"Trade close callback error: {e}")

        return trade

    async def execute_partial_exit(
        self,
        trade_id: str,
        current_price: float,
        partial_quantity: int,
        exit_reason: str,
    ) -> Optional[float]:
        """
        Execute a partial exit — reduce position size, keep trade open.
        Returns the fill price of the partial, or None on failure.
        The trade remains in open_trades with reduced quantity.
        """
        trade = self._open_trades.get(trade_id)
        if not trade:
            logger.error(f"Cannot partial exit: trade {trade_id} not found")
            return None

        partial_quantity = min(partial_quantity, trade.quantity - 1)  # Always leave at least 1
        if partial_quantity <= 0:
            return None

        request = PlaceOrderRequest(
            symbol=trade.symbol,
            token=trade.token,
            exchange="NFO",
            transaction_type=TransactionType.SELL,
            order_type=OrderType.MARKET,
            quantity=partial_quantity,
        )

        fill = await self._simulator.place_order(request)
        if fill.rejected:
            logger.warning(f"Partial exit rejected for {trade_id}")
            return None

        fill_price = fill.average_fill_price
        partial_gross = (fill_price - trade.entry_price) * partial_quantity
        charges = self._expense_engine.calculate_round_trip(
            trade_id=f"{trade_id}-PARTIAL",
            buy_price=trade.entry_price,
            sell_price=fill_price,
            quantity=partial_quantity,
            session_date=trade.session_date,
        )
        partial_net = partial_gross - charges.total_charges

        # Reduce open position quantity (do NOT close the trade)
        trade.quantity -= partial_quantity

        logger.info(
            f"PARTIAL EXIT: {trade.symbol} {partial_quantity}qty @ ₹{fill_price:.2f} "
            f"net=₹{partial_net:.2f} | remaining={trade.quantity}qty",
            extra={"trade_id": trade_id}
        )
        return fill_price

    def close_trade_sync(
        self,
        trade_id: str,
        exit_price: float,
        exit_reason: str = "MANUAL_FORCE_EXIT",
    ) -> Optional["TradeRecord"]:
        """
        Synchronously close an open trade at a given price.
        Fires all on_trade_close callbacks (CSV write, journal, risk update)
        so the exit is fully persisted — identical to a normal strategy exit.
        Use for dashboard force-exit, emergency closes, or end-of-session sweeps.
        """
        trade = self._open_trades.get(trade_id)
        if not trade:
            logger.warning(f"close_trade_sync: trade {trade_id} not found")
            return None

        trade.exit_price  = exit_price
        trade.exit_time   = datetime.now()
        trade.exit_reason = exit_reason
        trade.status      = "CLOSED"

        trade.gross_pnl = (trade.exit_price - trade.entry_price) * trade.quantity
        try:
            charges = self._expense_engine.calculate_round_trip(
                trade_id=trade.trade_id,
                buy_price=trade.entry_price,
                sell_price=trade.exit_price,
                quantity=trade.quantity,
                session_date=trade.session_date,
            )
            trade.total_charges = charges.total_charges
        except Exception:
            trade.total_charges = 0.0
        trade.net_pnl = trade.gross_pnl - trade.total_charges
        trade.pnl_pct = (
            (trade.exit_price - trade.entry_price) / trade.entry_price
            if trade.entry_price > 0 else 0.0
        )

        del self._open_trades[trade_id]
        if trade.strategy_name in self._strategy_positions:
            del self._strategy_positions[trade.strategy_name]
        self._closed_trades.append(trade)

        logger.info(
            f"FORCE CLOSE: {trade.symbol} @ ₹{exit_price:.2f} "
            f"reason={exit_reason} net=₹{trade.net_pnl:.2f}"
        )

        for cb in self._on_trade_close_callbacks:
            try:
                cb(trade)
            except Exception as e:
                logger.error(f"close_trade_sync callback error: {e}")

        return trade

    def on_trade_open(self, callback: Callable[[TradeRecord], None]) -> None:
        self._on_trade_open_callbacks.append(callback)

    def on_trade_close(self, callback: Callable[[TradeRecord], None]) -> None:
        self._on_trade_close_callbacks.append(callback)

    @property
    def open_trades(self) -> Dict[str, TradeRecord]:
        return dict(self._open_trades)

    @property
    def closed_trades(self) -> List[TradeRecord]:
        return list(self._closed_trades)

    @property
    def has_open_position(self) -> bool:
        return len(self._open_trades) > 0

    def get_open_trade(self) -> Optional[TradeRecord]:
        """Return the single open trade (legacy single-strategy use)."""
        if self._open_trades:
            return next(iter(self._open_trades.values()))
        return None

    def has_position_for_strategy(self, strategy_name: str) -> bool:
        """Return True if strategy has an open position."""
        return strategy_name in self._strategy_positions

    def get_trade_for_strategy(self, strategy_name: str) -> Optional[TradeRecord]:
        """Return the open TradeRecord for a strategy, or None."""
        trade_id = self._strategy_positions.get(strategy_name)
        if trade_id:
            return self._open_trades.get(trade_id)
        return None

    def restore_open_trade(self, trade: "TradeRecord") -> None:
        """Restore an open trade from CSV on session restart."""
        self._open_trades[trade.trade_id] = trade
        if trade.strategy_name:
            self._strategy_positions[trade.strategy_name] = trade.trade_id

    def get_daily_pnl(self, session_date: Optional[date] = None) -> float:
        """Total net P&L for a given day."""
        if session_date is None:
            session_date = date.today()
        return sum(
            t.net_pnl for t in self._closed_trades
            if t.session_date == session_date
        )

    def get_daily_trade_count(self, session_date: Optional[date] = None) -> int:
        if session_date is None:
            session_date = date.today()
        return sum(1 for t in self._closed_trades if t.session_date == session_date)
