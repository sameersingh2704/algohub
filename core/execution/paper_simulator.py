"""
Paper Trading Simulator
========================
Realistic simulation engine that mimics live trading conditions.

Simulation features:
- Spread-crossing cost on market orders (you buy at ask, sell at bid)
- Latency simulation (50–200ms order processing delay)
- Partial fill simulation (especially for large orders)
- Slippage on market orders in low-liquidity conditions
- Order rejection simulation (occasional, realistic rates)
- Stale quote detection
- Order queue simulation

This simulator is designed to OVERESTIMATE costs slightly —
it is better to be conservative in paper mode and be
pleasantly surprised in live mode.

Key principle: If the strategy looks profitable in this simulator,
it has a realistic chance in live markets. If it struggles here,
it will fail live.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Callable

from core.broker.angel_connector import (
    OrderType, TransactionType, ProductType,
    PlaceOrderRequest, OrderResponse, OrderStatus,
)

logger = logging.getLogger(__name__)


class SimulationRealism(str, Enum):
    """Simulation realism level."""
    OPTIMISTIC = "OPTIMISTIC"   # Favorable fills — DO NOT use for evaluation
    REALISTIC = "REALISTIC"     # Default — market-realistic
    CONSERVATIVE = "CONSERVATIVE"  # Worse-than-expected — stress test


@dataclass
class SimulatedFill:
    """Result of a simulated order fill."""
    order_id: str
    internal_order_id: str
    symbol: str
    transaction_type: str
    quantity: int
    filled_quantity: int
    average_fill_price: float
    slippage: float               # Rs. slippage vs. quote
    spread_cost: float            # Rs. spread cost paid
    latency_ms: int
    rejected: bool = False
    rejection_reason: str = ""
    partial: bool = False
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def fill_pct(self) -> float:
        return self.filled_quantity / self.quantity if self.quantity > 0 else 0.0


@dataclass
class MarketQuote:
    """Current market quote for simulation."""
    ltp: float
    bid: float
    ask: float
    volume: int
    spread: float
    timestamp: datetime


class PaperSimulator:
    """
    Realistic paper trading order simulator.

    Simulates the imperfections of live execution:
    1. Market orders fill at ask (buy) or bid (sell), not LTP
    2. Slippage: random ±0-2 ticks on market orders
    3. Latency: 50-200ms delay before fill confirmation
    4. Partial fills: large orders may not fill completely
    5. Order rejection: ~1-2% rejection rate (realistic)
    6. Stale quotes: if no tick for >10s, apply extra slippage

    All fills are logged with exact simulation parameters.
    """

    def __init__(
        self,
        realism: SimulationRealism = SimulationRealism.REALISTIC,
        lot_size: int = 65,
    ) -> None:
        self._realism = realism
        self._lot_size = lot_size
        self._pending_orders: Dict[str, PlaceOrderRequest] = {}
        self._filled_orders: Dict[str, SimulatedFill] = {}
        self._on_fill_callbacks: List[Callable[[SimulatedFill], None]] = []

        # Quote cache: symbol → MarketQuote
        self._quotes: Dict[str, MarketQuote] = {}

        # Simulation parameters by realism level
        self._params = {
            SimulationRealism.OPTIMISTIC: {
                "slippage_ticks": (0, 1),
                "latency_ms": (30, 80),
                "rejection_rate": 0.005,
                "partial_fill_rate": 0.02,
                "partial_fill_pct": (0.8, 1.0),
            },
            SimulationRealism.REALISTIC: {
                "slippage_ticks": (0, 2),
                "latency_ms": (50, 200),
                "rejection_rate": 0.015,
                "partial_fill_rate": 0.05,
                "partial_fill_pct": (0.7, 1.0),
            },
            SimulationRealism.CONSERVATIVE: {
                "slippage_ticks": (1, 3),
                "latency_ms": (100, 400),
                "rejection_rate": 0.03,
                "partial_fill_rate": 0.10,
                "partial_fill_pct": (0.6, 0.9),
            },
        }

    def update_quote(self, symbol: str, ltp: float, bid: float, ask: float, volume: int) -> None:
        """Update market quote for simulation reference."""
        self._quotes[symbol] = MarketQuote(
            ltp=ltp,
            bid=bid,
            ask=ask,
            volume=volume,
            spread=ask - bid,
            timestamp=datetime.now(),
        )

    async def place_order(self, request: PlaceOrderRequest) -> SimulatedFill:
        """
        Simulate order placement with realistic fill behavior.
        This is async to simulate the latency of real order placement.
        """
        params = self._params[self._realism]
        order_id = str(uuid.uuid4())

        # Simulate latency
        latency_ms = random.randint(*params["latency_ms"])
        await asyncio.sleep(latency_ms / 1000.0)

        # Simulate rejection
        if random.random() < params["rejection_rate"]:
            rejection_reasons = [
                "Insufficient margin",
                "Order rate limit exceeded",
                "Exchange reject - RMS",
                "Symbol not found",
                "Session expired",
            ]
            reason = random.choice(rejection_reasons)
            fill = SimulatedFill(
                order_id=order_id,
                internal_order_id=request.internal_order_id,
                symbol=request.symbol,
                transaction_type=request.transaction_type.value,
                quantity=request.quantity,
                filled_quantity=0,
                average_fill_price=0.0,
                slippage=0.0,
                spread_cost=0.0,
                latency_ms=latency_ms,
                rejected=True,
                rejection_reason=reason,
            )
            logger.warning(f"Simulated order rejection: {reason} ({request.symbol})")
            return fill

        # Get current quote
        quote = self._quotes.get(request.symbol)
        if not quote:
            # No quote available — use price from order
            fill_price = request.price if request.price > 0 else 0.0
            if fill_price == 0:
                return SimulatedFill(
                    order_id=order_id,
                    internal_order_id=request.internal_order_id,
                    symbol=request.symbol,
                    transaction_type=request.transaction_type.value,
                    quantity=request.quantity,
                    filled_quantity=0,
                    average_fill_price=0.0,
                    slippage=0.0,
                    spread_cost=0.0,
                    latency_ms=latency_ms,
                    rejected=True,
                    rejection_reason="No market quote available",
                )
            bid = ask = fill_price
        else:
            bid = quote.bid if quote.bid > 0 else quote.ltp * 0.995
            ask = quote.ask if quote.ask > 0 else quote.ltp * 1.005

        # Check for stale quote
        quote_age_sec = (datetime.now() - quote.timestamp).total_seconds() if quote else 0
        stale_multiplier = 1.5 if quote_age_sec > 10 else 1.0

        # Determine base fill price
        if request.order_type == OrderType.MARKET:
            if request.transaction_type == TransactionType.BUY:
                base_price = ask  # Market buy fills at ask
            else:
                base_price = bid  # Market sell fills at bid
        elif request.order_type == OrderType.LIMIT:
            # Limit: fill if market is favorable, else simulate fill at limit
            if request.transaction_type == TransactionType.BUY:
                base_price = min(request.price, ask)
            else:
                base_price = max(request.price, bid)
        else:
            # SL/SL-M — simplified
            base_price = request.trigger_price if request.trigger_price > 0 else (bid if request.transaction_type == TransactionType.SELL else ask)

        # Apply slippage (in Rs. ticks — for options, 1 tick = 0.05)
        tick_size = 0.05
        slippage_ticks = random.randint(*params["slippage_ticks"])
        slippage_rs = slippage_ticks * tick_size * stale_multiplier

        if request.transaction_type == TransactionType.BUY:
            slippage_rs = slippage_rs  # Buy gets worse (higher price)
        else:
            slippage_rs = -slippage_rs  # Sell gets worse (lower price)

        fill_price = max(0.05, base_price + slippage_rs)

        # Calculate spread cost
        spread = ask - bid
        if request.transaction_type == TransactionType.BUY:
            spread_cost = max(0, fill_price - quote.ltp) if quote else 0.0
        else:
            spread_cost = max(0, quote.ltp - fill_price) if quote else 0.0

        # Simulate partial fill
        filled_qty = request.quantity
        is_partial = False
        if random.random() < params["partial_fill_rate"]:
            fill_pct = random.uniform(*params["partial_fill_pct"])
            filled_qty = max(self._lot_size, int(request.quantity * fill_pct))
            filled_qty = (filled_qty // self._lot_size) * self._lot_size  # Round to lot size
            is_partial = filled_qty < request.quantity

        fill = SimulatedFill(
            order_id=order_id,
            internal_order_id=request.internal_order_id,
            symbol=request.symbol,
            transaction_type=request.transaction_type.value,
            quantity=request.quantity,
            filled_quantity=filled_qty,
            average_fill_price=round(fill_price, 2),
            slippage=round(slippage_rs, 2),
            spread_cost=round(spread_cost * filled_qty, 2),
            latency_ms=latency_ms,
            partial=is_partial,
        )

        logger.info(
            f"Simulated fill: {request.symbol} {request.transaction_type.value} "
            f"{filled_qty}/{request.quantity} @ ₹{fill_price:.2f} "
            f"(slip=₹{slippage_rs:.2f} lat={latency_ms}ms)",
        )

        self._filled_orders[order_id] = fill

        for cb in self._on_fill_callbacks:
            try:
                cb(fill)
            except Exception as e:
                logger.error(f"Fill callback error: {e}")

        return fill

    async def cancel_order(self, order_id: str) -> bool:
        """Simulate order cancellation."""
        # Small chance cancellation arrives after fill in real markets
        await asyncio.sleep(random.randint(20, 80) / 1000.0)
        logger.info(f"Simulated cancel: {order_id}")
        return True

    def on_fill(self, callback: Callable[[SimulatedFill], None]) -> None:
        """Register callback for fill events."""
        self._on_fill_callbacks.append(callback)

    def get_fill(self, order_id: str) -> Optional[SimulatedFill]:
        return self._filled_orders.get(order_id)

    @property
    def total_fills(self) -> int:
        return len(self._filled_orders)

    @property
    def rejected_fills(self) -> int:
        return sum(1 for f in self._filled_orders.values() if f.rejected)

    @property
    def total_slippage(self) -> float:
        return sum(
            f.slippage * f.filled_quantity
            for f in self._filled_orders.values()
            if not f.rejected
        )

    def get_simulation_summary(self) -> dict:
        """Return simulation statistics for review."""
        fills = [f for f in self._filled_orders.values() if not f.rejected]
        return {
            "total_orders": len(self._filled_orders),
            "filled": len(fills),
            "rejected": self.rejected_fills,
            "partial_fills": sum(1 for f in fills if f.partial),
            "total_slippage_rs": round(self.total_slippage, 2),
            "avg_latency_ms": round(
                sum(f.latency_ms for f in fills) / len(fills) if fills else 0, 1
            ),
            "realism_level": self._realism.value,
        }
