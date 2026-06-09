"""
BaseStrategy — Abstract base class for all trading strategies.

Every strategy follows the same 8-state lifecycle:
  IDLE → WAITING → ARMED → ENTRY_PENDING → ACTIVE_POSITION
       → EXIT_PENDING → COOLDOWN → HALTED

Subclasses must implement the four hook methods:
  _check_setup, _check_armed, _check_entry_trigger, _check_exit
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Any

from core.data.market_data_engine import MarketState, OHLCVBar

logger = logging.getLogger(__name__)


class StrategyState(Enum):
    IDLE = "IDLE"
    WAITING = "WAITING"
    ARMED = "ARMED"
    ENTRY_PENDING = "ENTRY_PENDING"
    ACTIVE_POSITION = "ACTIVE_POSITION"
    EXIT_PENDING = "EXIT_PENDING"
    COOLDOWN = "COOLDOWN"
    HALTED = "HALTED"


class StrategyCategory(Enum):
    LIQUIDITY = "A"
    MARKET_STRUCTURE = "C"
    TREND = "D"
    MEAN_REVERSION = "E"
    OPTIONS = "F"


@dataclass
class StrategySignal:
    """Entry signal emitted by a strategy when all conditions are met."""
    strategy_name: str
    category: StrategyCategory
    direction: str              # "CE" | "PE"
    signal_type: str            # human-readable signal name
    confidence: float           # 0.0–1.0
    underlying_price: float     # Spot/futures price at signal time
    suggested_strike: Optional[int] = None
    stop_price: float = 0.0     # Underlying level that invalidates trade
    target_price: float = 0.0   # Underlying target (for sizing context)
    meta: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def is_valid(self) -> bool:
        return self.direction in ("CE", "PE") and self.underlying_price > 0


@dataclass
class ExitDecision:
    """Exit instruction from a strategy's position manager."""
    action: str             # "EXIT" | "PARTIAL"
    reason: str             # ExitReason string
    partial_quantity: Optional[int] = None
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PositionState:
    """Tracks an open position for a strategy."""
    strategy_name: str
    trade_id: str
    direction: str              # "CE" | "PE"
    entry_price: float          # Option premium at entry
    entry_time: datetime
    quantity: int               # Current quantity (decreases on partial exits)
    peak_price: float           # Highest LTP seen since entry (for trailing)
    stop_price: float           # Option premium hard-stop level (e.g. entry * 0.70)
    target_price: float         # Option premium profit-target level (e.g. entry * 1.45)
    # Underlying-level stop/target from the entry signal.
    # CE: exit if spot <= underlying_stop | PE: exit if spot >= underlying_stop
    underlying_stop: float = 0.0
    underlying_target: float = 0.0
    breakeven_activated: bool = False
    partial_exit_done: bool = False
    remaining_quantity: int = 0  # Set after partial exit
    option_symbol: str = ""
    option_token: str = ""


class BaseStrategy(ABC):
    """
    Abstract base for all NIFTY option scalping strategies.

    Lifecycle:
      Each call to on_market_update() advances the internal state machine.
      When ARMED and trigger fires, returns a StrategySignal.
      When ACTIVE_POSITION and exit condition met, returns an ExitDecision
      via check_exit().
    """

    def __init__(self, name: str, category: StrategyCategory,
                 config: dict, risk_config: dict) -> None:
        self.name = name
        self.category = category
        self._config = config
        self._risk = risk_config
        self.state: StrategyState = StrategyState.IDLE
        self._position: Optional[PositionState] = None
        self._daily_trades: int = 0
        self._consecutive_losses: int = 0
        self._option_registry: Dict[str, str] = {}  # symbol → token
        self._cooldown_bars: int = 0
        self._cooldown_duration: int = config.get("cooldown_bars", 5)
        self._max_trades_per_day: int = config.get("max_trades_per_day", 2)
        self._max_consecutive_losses: int = config.get("max_consecutive_losses", 3)
        # When True, this strategy runs even on NO_TRADE regime days.
        # Use only for evaluation/testing strategies that need end-to-end validation.
        self.regime_exempt: bool = False
        # Injected once per session before market open (CPR, gap, PDH/PDL, prev_vix).
        self._session_ctx: dict = {}

    # ── Public interface ─────────────────────────────────────────────────────

    def on_market_update(
        self,
        market_state: MarketState,
        option_chain=None,
    ) -> Optional[StrategySignal]:
        """
        Main entry point. Called on every bar close.
        Returns a StrategySignal if an entry condition is met, else None.
        """
        if self.state == StrategyState.HALTED:
            return None

        # Cooldown countdown
        if self.state == StrategyState.COOLDOWN:
            self._cooldown_bars -= 1
            if self._cooldown_bars <= 0:
                self.state = StrategyState.IDLE
                logger.debug(f"{self.name}: cooldown expired → IDLE")
            return None

        # Skip if already in a position or pending
        if self.state in (StrategyState.ENTRY_PENDING,
                          StrategyState.ACTIVE_POSITION,
                          StrategyState.EXIT_PENDING):
            return None

        # Daily trade cap
        if self._daily_trades >= self._max_trades_per_day:
            return None

        # State transitions
        if self.state == StrategyState.IDLE:
            if self._check_setup(market_state, option_chain):
                self.state = StrategyState.WAITING
                logger.debug(f"{self.name}: IDLE → WAITING")

        if self.state == StrategyState.WAITING:
            if self._check_armed(market_state, option_chain):
                self.state = StrategyState.ARMED
                logger.debug(f"{self.name}: WAITING → ARMED")
            elif not self._check_setup(market_state, option_chain):
                self.state = StrategyState.IDLE

        if self.state == StrategyState.ARMED:
            signal = self._check_entry_trigger(market_state, option_chain)
            if signal and signal.is_valid:
                self.state = StrategyState.ENTRY_PENDING
                self._daily_trades += 1
                logger.info(f"{self.name}: ARMED → ENTRY_PENDING | {signal.direction} {signal.signal_type}")
                return signal
            elif not self._check_armed(market_state, option_chain):
                self.state = StrategyState.WAITING

        return None

    def check_exit(
        self, market_state: MarketState, current_option_price: float
    ) -> Optional[ExitDecision]:
        """
        Called when strategy has ACTIVE_POSITION.
        Returns ExitDecision if exit condition met, else None.

        Checks underlying stop/target FIRST (fast structural invalidation),
        then delegates to the strategy's own _check_exit for premium-based rules.
        """
        if self.state != StrategyState.ACTIVE_POSITION or self._position is None:
            return None

        pos = self._position
        spot = market_state.spot_price

        # Underlying stop: trade thesis is invalidated when spot crosses the
        # level set by the signal, regardless of how the option premium moved.
        if pos.underlying_stop > 0:
            if pos.direction == "CE" and spot <= pos.underlying_stop:
                return ExitDecision(action="EXIT", reason="UNDERLYING_STOP")
            if pos.direction == "PE" and spot >= pos.underlying_stop:
                return ExitDecision(action="EXIT", reason="UNDERLYING_STOP")

        # Underlying target: lock in when the underlying reaches the signal target.
        if pos.underlying_target > 0:
            if pos.direction == "CE" and spot >= pos.underlying_target:
                return ExitDecision(action="EXIT", reason="UNDERLYING_TARGET")
            if pos.direction == "PE" and spot <= pos.underlying_target:
                return ExitDecision(action="EXIT", reason="UNDERLYING_TARGET")

        return self._check_exit(market_state, current_option_price, pos)

    def on_entry_filled(self, trade_id: str, fill_price: float, quantity: int,
                        option_symbol: str, option_token: str,
                        stop_price: float, target_price: float) -> None:
        """Called by order manager when entry order is filled."""
        exits = self._config.get("exits", {})
        premium_stop   = fill_price * (1 - abs(exits.get("hard_stop_pct", 0.30)))
        premium_target = fill_price * (1 + exits.get("profit_target_pct", 0.45))
        self._position = PositionState(
            strategy_name=self.name,
            trade_id=trade_id,
            direction="CE" if "CE" in option_symbol else "PE",
            entry_price=fill_price,
            entry_time=datetime.now(),
            quantity=quantity,
            peak_price=fill_price,
            stop_price=premium_stop,
            target_price=premium_target,
            # stop_price / target_price from the signal are UNDERLYING levels.
            # Stored separately so check_exit can validate both option premium
            # AND the underlying price simultaneously.
            underlying_stop=stop_price,
            underlying_target=target_price,
            remaining_quantity=quantity,
            option_symbol=option_symbol,
            option_token=option_token,
        )
        self.state = StrategyState.ACTIVE_POSITION
        logger.info(
            f"{self.name}: ENTRY_PENDING → ACTIVE_POSITION | fill={fill_price:.2f} qty={quantity} "
            f"| underlying stop={stop_price:.2f} target={target_price:.2f}"
        )

    def on_entry_failed(self) -> None:
        """Called when an entry order could not be placed (no price, risk block, etc.)."""
        if self.state == StrategyState.ENTRY_PENDING:
            self.state = StrategyState.ARMED
            logger.warning(f"{self.name}: ENTRY_PENDING → ARMED (entry failed, will retry)")

    def on_exit_filled(self, fill_price: float, pnl: float, exit_reason: str) -> None:
        """Called by order manager when exit order is filled."""
        is_loss = pnl < 0
        if is_loss:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

        self._position = None
        self.state = StrategyState.COOLDOWN
        self._cooldown_bars = self._cooldown_duration

        logger.info(
            f"{self.name}: EXIT_PENDING → COOLDOWN | "
            f"pnl={pnl:.2f} reason={exit_reason} consec_losses={self._consecutive_losses}"
        )

        if self._consecutive_losses >= self._max_consecutive_losses:
            self.state = StrategyState.HALTED
            logger.warning(f"{self.name}: HALTED — {self._consecutive_losses} consecutive losses")

    def on_partial_exit_filled(self, fill_price: float, partial_quantity: int) -> None:
        """Called after partial exit fills. Position remains open."""
        if self._position:
            self._position.partial_exit_done = True
            self._position.quantity -= partial_quantity
            logger.info(f"{self.name}: partial exit {partial_quantity} lots @ {fill_price:.2f}, remaining={self._position.quantity}")

    def register_option(self, symbol: str, token: str) -> None:
        """Register an option symbol → token mapping."""
        self._option_registry[symbol] = token
        self._option_registry[token] = symbol  # reverse lookup

    def set_session_context(self, ctx: dict) -> None:
        """Inject session-level data: gap_pts, prev_close, prev_high, prev_low,
        cpr_tc, cpr_bc, session_open, prev_vix. Called once from _pre_session_prep."""
        self._session_ctx = ctx
        logger.debug(f"{self.name}: session context set — {list(ctx.keys())}")

    def _standard_exit_check(
        self, ms: MarketState, option_price: float, pos: PositionState,
        time_stop_hhmm: tuple = (15, 0),
    ) -> Optional[ExitDecision]:
        """Common trailing-stop / target / time-stop exit logic for all strategies."""
        if option_price <= 0:
            return None
        exits = self._config.get("exits", {})
        hard_stop_pct           = exits.get("hard_stop_pct", -0.30)
        profit_target_pct       = exits.get("profit_target_pct", 0.60)
        trailing_activation_pct = exits.get("trailing_activation_pct", 0.20)
        trailing_stop_pct       = exits.get("trailing_stop_pct", 0.15)
        partial_exit_pct        = exits.get("partial_exit_pct", 0.25)

        pnl_pct = (option_price - pos.entry_price) / pos.entry_price
        if option_price > pos.peak_price:
            pos.peak_price = option_price

        if pnl_pct <= hard_stop_pct:
            return ExitDecision(action="EXIT", reason="HARD_STOP")
        if pnl_pct >= profit_target_pct:
            return ExitDecision(action="EXIT", reason="PROFIT_TARGET")

        if pnl_pct >= trailing_activation_pct:
            pos.breakeven_activated = True
        if pos.breakeven_activated:
            trail_stop = pos.peak_price * (1.0 - trailing_stop_pct)
            if option_price <= trail_stop:
                return ExitDecision(action="EXIT", reason="TRAILING_STOP")

        if pnl_pct >= partial_exit_pct and not pos.partial_exit_done:
            return ExitDecision(
                action="PARTIAL", reason="PARTIAL_TARGET",
                partial_quantity=max(1, pos.quantity // 2),
            )

        if (ms.timestamp.hour, ms.timestamp.minute) >= time_stop_hhmm:
            return ExitDecision(action="EXIT", reason="TIME_STOP")

        return None

    def reset_daily(self) -> None:
        """Reset daily counters. Call at session start."""
        self._daily_trades = 0
        self._consecutive_losses = 0
        if self.state != StrategyState.HALTED:
            self.state = StrategyState.IDLE
            self._position = None
            self._cooldown_bars = 0
        logger.info(f"{self.name}: daily reset")

    def halt(self) -> None:
        """Emergency halt — circuit breaker."""
        self.state = StrategyState.HALTED
        logger.warning(f"{self.name}: manually halted")

    def resume(self) -> None:
        """Resume from halt (manual reset)."""
        self.state = StrategyState.IDLE
        logger.info(f"{self.name}: resumed from halt")

    @property
    def has_position(self) -> bool:
        return self.state == StrategyState.ACTIVE_POSITION and self._position is not None

    @property
    def position(self) -> Optional[PositionState]:
        return self._position

    # ── Subclass hooks ───────────────────────────────────────────────────────

    @abstractmethod
    def _check_setup(self, ms: MarketState, option_chain=None) -> bool:
        """
        IDLE → WAITING: Are the broad preconditions for this strategy present?
        E.g., correct time window, VIX in range, trend direction.
        Should be fast — called every bar.
        """
        ...

    @abstractmethod
    def _check_armed(self, ms: MarketState, option_chain=None) -> bool:
        """
        WAITING → ARMED: Are we in position to take the trade?
        E.g., price approaching key level, structure forming.
        """
        ...

    @abstractmethod
    def _check_entry_trigger(self, ms: MarketState, option_chain=None) -> Optional[StrategySignal]:
        """
        ARMED → ENTRY_PENDING: Has the exact trigger fired?
        Return StrategySignal if yes, None otherwise.
        """
        ...

    @abstractmethod
    def _check_exit(self, ms: MarketState, option_price: float,
                    pos: PositionState) -> Optional[ExitDecision]:
        """
        ACTIVE_POSITION: Should we exit (or partially exit)?
        Return ExitDecision if yes, None to hold.
        """
        ...
