"""
AVCS Strategy — Adaptive ORB-VWAP Confluence Scalper
======================================================
Extends BaseStrategy to integrate with the multi-strategy framework.

Implements:
- Pre-session regime setup (WAITING state)
- ORB lock detection (ARMED state)
- Signal evaluation via SignalEngine (ENTRY_PENDING trigger)
- Comprehensive 8-level exit logic (ACTIVE_POSITION management)
- Tuesday gamma mode with tighter parameters
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from enum import Enum
from typing import Dict, List, Optional, Tuple
import uuid

from core.data.market_data_engine import MarketDataEngine, MarketState, OptionQuote
from core.data.greeks_calculator import GreeksCalculator
from core.strategy.signal_engine import Signal, SignalEngine, SignalDirection, Regime
from core.strategy.base_strategy import (
    BaseStrategy, StrategyCategory, StrategySignal, StrategyState,
    ExitDecision, PositionState,
)

logger = logging.getLogger(__name__)


class ExitReason(str, Enum):
    HARD_STOP = "HARD_STOP"
    TRAILING_STOP = "TRAILING_STOP"
    PROFIT_TARGET = "PROFIT_TARGET"
    PARTIAL_EXIT = "PARTIAL_EXIT"
    BREAKEVEN_STOP = "BREAKEVEN_STOP"
    MOMENTUM_FADE = "MOMENTUM_FADE"
    VWAP_INVALIDATION = "VWAP_INVALIDATION"
    TIME_STOP = "TIME_STOP"
    MIDDAY_EXIT = "MIDDAY_EXIT"
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"
    MANUAL = "MANUAL"
    HOLD = "HOLD"


@dataclass
class AVCSPositionState:
    """Extended position state for AVCS-specific exit tracking."""
    trade_id: str
    direction: str
    entry_price: float
    entry_time: datetime
    current_price: float
    lots: int
    quantity: int
    strike: int
    symbol: str
    token: str
    expiry: date
    trailing_activated: bool = False
    trailing_high: float = 0.0
    trailing_stop_price: float = 0.0
    breakeven_activated: bool = False
    partial_exit_done: bool = False
    remaining_quantity: int = 0

    @property
    def pnl_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        return (self.current_price - self.entry_price) / self.entry_price

    @property
    def pnl_rs(self) -> float:
        return (self.current_price - self.entry_price) * self.quantity


@dataclass
class AVCSExitDecision:
    """Detailed AVCS exit decision (internal representation)."""
    action: ExitReason
    message: str
    price: Optional[float] = None
    partial_quantity: Optional[int] = None


@dataclass
class EntryDecision:
    """Entry decision (used internally and by main.py for direct AVCS use)."""
    should_enter: bool
    direction: str
    strike: int
    symbol: str
    token: str
    lots: int
    quantity: int
    suggested_price: float
    hard_stop_price: float
    signal: Optional[Signal] = None
    reason: str = ""


class AVCSStrategy(BaseStrategy):
    """
    Adaptive ORB-VWAP Confluence Scalper.

    Extends BaseStrategy to integrate with StrategyManager.
    Also supports direct use via evaluate_entry() / check_exit() for
    backwards compatibility with main.py until full migration.
    """

    INTRADAY_CUTOFF = dtime(15, 15)
    MIDDAY_START = dtime(11, 30)
    MIDDAY_END = dtime(14, 0)
    GAMMA_MODE_START = dtime(10, 30)
    GAMMA_MODE_CUTOFF = dtime(15, 0)

    def __init__(
        self,
        market_data: MarketDataEngine,
        signal_engine: SignalEngine,
        greeks_calc: GreeksCalculator,
        strategy_config: dict,
        risk_config: dict,
        account_capital: float,
    ) -> None:
        super().__init__(
            name="AVCS",
            category=StrategyCategory.TREND,
            config=strategy_config,
            risk_config=risk_config,
        )
        self._md = market_data
        self._signal_engine = signal_engine
        self._greeks = greeks_calc
        self._cfg = strategy_config
        self._risk_cfg = risk_config
        self._capital = account_capital

        self._daily_pnl: float = 0.0
        self._session_date: Optional[date] = None
        self._is_tuesday: bool = False
        self._regime_ready: bool = False
        self._orb_ready: bool = False
        self._orb_range: float = 0.0

        # AVCS-specific position tracking (more detailed than base PositionState)
        self._avcs_position: Optional[AVCSPositionState] = None
        # Expiry for current session
        self._current_expiry: Optional[date] = None

    # ── Session lifecycle (direct use / main.py compat) ─────────────────────

    def start_session(self, session_date: date, account_capital: float) -> None:
        self._session_date = session_date
        self._is_tuesday = session_date.weekday() == 1
        self._capital = account_capital
        self._regime_ready = False
        self._orb_ready = False
        self._orb_range = 0.0
        self._avcs_position = None
        self.state = StrategyState.IDLE
        self._daily_trades = 0
        self._consecutive_losses = 0
        self._daily_pnl = 0.0
        self._signal_engine.reset_session()
        logger.info(
            f"AVCS session started: {session_date} "
            f"(Tuesday={'YES' if self._is_tuesday else 'NO'}, Capital=₹{account_capital:,.0f})"
        )

    def on_regime_classified(self, regime_result) -> None:
        self._regime_ready = True
        if self.state == StrategyState.IDLE:
            self.state = StrategyState.WAITING
        logger.info(f"AVCS regime classified → WAITING | Regime: {regime_result.regime.value}")

    def on_orb_locked(self, orb_high: float, orb_low: float, orb_range: float) -> None:
        orb_min = self._cfg["orb"]["min_range_points"]
        orb_max = self._cfg["orb"]["max_range_points"]

        if orb_range < orb_min:
            logger.warning(f"ORB range {orb_range:.1f}pts < minimum {orb_min}pts — NARROW ORB")

        if orb_range > orb_max:
            self.state = StrategyState.HALTED
            logger.warning(f"ORB range {orb_range:.1f}pts > maximum — HALTED")
            return

        self._orb_ready = True
        self._orb_range = orb_range
        self.state = StrategyState.ARMED
        logger.info(f"AVCS ARMED — ORB: {orb_low:.2f}–{orb_high:.2f} Range: {orb_range:.2f}pts")

    # ── BaseStrategy abstract hooks ──────────────────────────────────────────

    def _check_setup(self, ms: MarketState, option_chain=None) -> bool:
        """IDLE → WAITING: regime/session conditions met."""
        if ms.india_vix > self._cfg.get("regime", {}).get("vix_max_reduced", 25.0):
            return False
        # If start_session was called, regime_ready may already be set
        return self._regime_ready or ms.bars_count >= 1

    def _check_armed(self, ms: MarketState, option_chain=None) -> bool:
        """WAITING → ARMED: ORB locked and range acceptable."""
        if self._orb_ready:
            return True
        # Auto-arm when ORB time has passed (for StrategyManager flow without explicit callback)
        if ms.orb_range > 0 and ms.timestamp.time() >= dtime(9, 36):
            orb_max = self._cfg["orb"]["max_range_points"]
            if ms.orb_range > orb_max:
                return False
            self._orb_ready = True
            self._orb_range = ms.orb_range
            return True
        return False

    def _check_entry_trigger(self, ms: MarketState, option_chain=None) -> Optional[StrategySignal]:
        """ARMED → ENTRY_PENDING: signal engine fires."""
        if self._current_expiry is None:
            return None

        expiry = self._current_expiry
        current_time = ms.timestamp.time()

        for option_type in ["CE", "PE"]:
            signal = self._try_generate_signal(option_type, ms, current_time, expiry)
            if signal and signal.is_valid:
                strike, symbol, token = self._select_strike(option_type, ms.spot_price, expiry)
                if not strike:
                    continue
                quote = self._md.get_option_quote(token)
                if not quote:
                    continue

                stop_price = quote.ltp * (1 + self._cfg["exits"]["hard_stop_pct"])
                lots = self._calculate_lots(quote.ltp, stop_price)
                if lots < 1:
                    continue

                return StrategySignal(
                    strategy_name=self.name,
                    category=self.category,
                    direction=option_type,
                    signal_type=signal.signal_type if hasattr(signal, "signal_type") else "ORB_SIGNAL",
                    confidence=0.75,
                    underlying_price=ms.spot_price,
                    suggested_strike=strike,
                    stop_price=stop_price,
                    target_price=quote.ltp * (1 + self._cfg["exits"]["profit_target_pct"]),
                    meta={
                        "symbol": symbol,
                        "token": token,
                        "strike": strike,
                        "lots": lots,
                        "quantity": lots * self._cfg["options"]["lot_size"],
                        "ask_price": quote.ask,
                        "signal_id": getattr(signal, "signal_id", str(uuid.uuid4())),
                        "signal_reason": str(getattr(signal, "signal_type", "AVCS")),
                    },
                )
        return None

    def _check_exit(self, ms: MarketState, option_price: float,
                    pos: PositionState) -> Optional[ExitDecision]:
        """ACTIVE_POSITION: delegate to detailed AVCS exit logic."""
        if self._avcs_position is None:
            return None

        avcs_pos = self._avcs_position
        avcs_pos.current_price = option_price
        current_time = ms.timestamp.time()

        result = self._avcs_check_exit(avcs_pos, ms, current_time)
        if result.action == ExitReason.HOLD:
            return None

        if result.action == ExitReason.PARTIAL_EXIT:
            return ExitDecision(
                action="PARTIAL",
                reason=result.action.value,
                partial_quantity=result.partial_quantity,
            )

        return ExitDecision(action="EXIT", reason=result.action.value)

    # ── Override on_entry_filled to sync AVCS position ──────────────────────

    def on_entry_filled(
        self, trade_id: str, fill_price: float, quantity: int,
        option_symbol: str, option_token: str,
        stop_price: float, target_price: float,
    ) -> None:
        super().on_entry_filled(
            trade_id, fill_price, quantity, option_symbol, option_token,
            stop_price, target_price,
        )
        # Set AVCS position for detailed exit tracking
        self._avcs_position = AVCSPositionState(
            trade_id=trade_id,
            direction="CE" if "CE" in option_symbol else "PE",
            entry_price=fill_price,
            entry_time=datetime.now(),
            current_price=fill_price,
            lots=quantity // self._cfg["options"]["lot_size"],
            quantity=quantity,
            strike=0,
            symbol=option_symbol,
            token=option_token,
            expiry=self._current_expiry or date.today(),
            remaining_quantity=quantity,
        )

    def on_exit_filled(self, fill_price: float, pnl: float, exit_reason: str) -> None:
        super().on_exit_filled(fill_price, pnl, exit_reason)
        self._avcs_position = None
        self._daily_pnl += pnl

        won = pnl > 0
        self._signal_engine.update_session_state(
            daily_pnl=self._daily_pnl,
            trades_today=self._daily_trades,
            consecutive_losses=self._consecutive_losses,
        )

        if self._consecutive_losses >= self._cfg.get("consecutive_loss_halt", 3):
            self._signal_engine.halt_session(
                f"{self._consecutive_losses} consecutive losses"
            )

    def on_partial_exit_filled(self, fill_price: float, partial_quantity: int) -> None:
        super().on_partial_exit_filled(fill_price, partial_quantity)
        if self._avcs_position:
            self._avcs_position.partial_exit_done = True
            self._avcs_position.quantity -= partial_quantity

    # ── Direct entry API (backwards compat with main.py) ────────────────────

    def evaluate_entry(
        self,
        market_state: MarketState,
        current_time: dtime,
        expiry: date,
    ) -> Optional[EntryDecision]:
        """
        Direct entry evaluation. Used by main.py before full StrategyManager migration.
        """
        if self.state not in (StrategyState.ARMED, StrategyState.WAITING):
            return None
        if not market_state.is_data_fresh:
            return None

        self._current_expiry = expiry

        for option_type in ["CE", "PE"]:
            signal = self._try_generate_signal(option_type, market_state, current_time, expiry)
            if signal and signal.is_valid:
                strike, symbol, token = self._select_strike(option_type, market_state.spot_price, expiry)
                if not strike:
                    continue
                quote = self._get_option_quote(token, symbol, strike, option_type, expiry)
                if not quote:
                    continue

                stop_price = quote.ltp * (1 + self._cfg["exits"]["hard_stop_pct"])
                lots = self._calculate_lots(quote.ltp, stop_price)
                if lots < 1:
                    continue

                quantity = lots * self._cfg["options"]["lot_size"]
                return EntryDecision(
                    should_enter=True,
                    direction=option_type,
                    strike=strike,
                    symbol=symbol,
                    token=token,
                    lots=lots,
                    quantity=quantity,
                    suggested_price=quote.ask,
                    hard_stop_price=stop_price,
                    signal=signal,
                    reason=f"ORB breakout {option_type}: spot={market_state.spot_price:.2f}",
                )
        return None

    def check_exit_direct(
        self,
        position: AVCSPositionState,
        market_state: MarketState,
        current_time: dtime,
    ) -> AVCSExitDecision:
        """Direct exit check. Used by main.py before full StrategyManager migration."""
        return self._avcs_check_exit(position, market_state, current_time)

    # ── Internal AVCS exit logic (detailed 8-level) ──────────────────────────

    def _avcs_check_exit(
        self,
        position: AVCSPositionState,
        market_state: MarketState,
        current_time: dtime,
    ) -> AVCSExitDecision:
        cfg = self._get_exit_config()

        # 1. Hard stop
        if position.pnl_pct <= cfg["hard_stop_pct"]:
            return AVCSExitDecision(
                action=ExitReason.HARD_STOP,
                message=f"Hard stop: pnl={position.pnl_pct*100:.1f}%",
            )

        # 2. Time stop
        if current_time >= self.INTRADAY_CUTOFF:
            return AVCSExitDecision(action=ExitReason.TIME_STOP, message="Intraday cutoff 15:15")
        if self._is_tuesday and current_time >= self.GAMMA_MODE_CUTOFF:
            return AVCSExitDecision(action=ExitReason.TIME_STOP, message="Gamma mode cutoff 15:00")

        # 3. Breakeven stop
        be_activation = cfg.get("breakeven_activation_pct", 0.15)
        be_lock_pct = cfg.get("breakeven_lock_pct", 0.02)
        if not position.breakeven_activated and position.pnl_pct >= be_activation:
            position.breakeven_activated = True
            be_price = position.entry_price * (1 + be_lock_pct)
            position.trailing_stop_price = be_price
            logger.info(f"AVCS: breakeven stop activated @ {be_price:.2f}")
        if position.breakeven_activated and not position.trailing_activated:
            if position.current_price <= position.trailing_stop_price:
                return AVCSExitDecision(action=ExitReason.BREAKEVEN_STOP, message="Breakeven stop hit")

        # 4. Partial exit — skip when only 1 lot (can't sell a fractional lot on NSE)
        lot_size = self._cfg["options"]["lot_size"]
        partial_pct = cfg.get("partial_exit_pct", 0.20)
        if not position.partial_exit_done and position.pnl_pct >= partial_pct:
            position.partial_exit_done = True
            total_lots = position.quantity // lot_size
            if total_lots >= 2:
                first_partial = cfg.get("first_partial_pct", 0.50)
                partial_lots = max(1, int(total_lots * first_partial))
                partial_qty = partial_lots * lot_size
                return AVCSExitDecision(
                    action=ExitReason.PARTIAL_EXIT,
                    message=f"Partial exit {partial_lots}L at +{position.pnl_pct*100:.1f}%",
                    partial_quantity=partial_qty,
                )
            # 1-lot position: mark done and let trailing/target handle full exit

        # 5. Trailing stop
        if position.pnl_pct >= cfg["trailing_activation_pct"]:
            if not position.trailing_activated:
                position.trailing_activated = True
                position.trailing_high = position.current_price
                position.trailing_stop_price = position.trailing_high * (1 - cfg["trailing_stop_pct"])
                logger.info(f"AVCS: trailing stop activated @ {position.trailing_stop_price:.2f}")
            else:
                if position.current_price > position.trailing_high:
                    position.trailing_high = position.current_price
                    position.trailing_stop_price = position.trailing_high * (1 - cfg["trailing_stop_pct"])
            if position.current_price <= position.trailing_stop_price:
                return AVCSExitDecision(action=ExitReason.TRAILING_STOP, message="Trailing stop hit")

        # 6. Profit target
        if position.pnl_pct >= cfg["profit_target_pct"]:
            return AVCSExitDecision(action=ExitReason.PROFIT_TARGET, message="Profit target hit")

        # 7. VWAP invalidation
        spot = market_state.spot_price
        vwap = market_state.vwap
        if vwap > 0:
            if position.direction == "CE" and spot < vwap * 0.9995 and position.pnl_pct < 0:
                return AVCSExitDecision(action=ExitReason.VWAP_INVALIDATION,
                                        message="CE thesis broken: spot below VWAP")
            elif position.direction == "PE" and spot > vwap * 1.0005 and position.pnl_pct < 0:
                return AVCSExitDecision(action=ExitReason.VWAP_INVALIDATION,
                                        message="PE thesis broken: spot above VWAP")

        # 8. Midday exit
        if (self.MIDDAY_START <= current_time < self.MIDDAY_END and
                position.pnl_pct < self._cfg["exits"].get("midday_exit_below_pct", 0.05)):
            return AVCSExitDecision(action=ExitReason.MIDDAY_EXIT, message="Midday chop exit")

        return AVCSExitDecision(action=ExitReason.HOLD, message="No exit condition")

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _try_generate_signal(
        self, option_type: str, market_state: MarketState, current_time: dtime, expiry: date
    ) -> Optional[Signal]:
        strike_options = self._get_candidate_strikes(option_type, market_state.spot_price, expiry)
        for strike in strike_options:
            symbol = self._build_option_symbol(strike, option_type, expiry)
            token = self._option_registry.get(symbol)
            if not token:
                continue
            quote = self._md.get_option_quote(token)
            signal = self._signal_engine.evaluate_entry(
                market_state=market_state,
                current_time=current_time,
                current_bar_volume=0,
                option_quote=quote,
                option_type=option_type,
                expiry=expiry,
                existing_position=self._avcs_position is not None,
                daily_pnl=self._daily_pnl,
                trades_today=self._daily_trades,
                consecutive_losses=self._consecutive_losses,
            )
            if signal and signal.is_valid:
                return signal
        return None

    def _select_strike(
        self, option_type: str, spot: float, expiry: date
    ) -> Tuple[Optional[int], Optional[str], Optional[str]]:
        interval = self._cfg["options"]["strike_interval"]
        atm = GreeksCalculator.get_atm_strike(spot, interval)
        candidates = [atm, atm + interval] if option_type == "CE" else [atm, atm - interval]

        for strike in candidates:
            symbol = self._build_option_symbol(strike, option_type, expiry)
            token = self._option_registry.get(symbol)
            if not token:
                continue
            quote = self._md.get_option_quote(token)
            if not quote:
                continue
            if quote.is_liquid(self._cfg["options"]["min_oi"], self._cfg["options"]["max_spread_pct"]):
                if quote.delta is not None:
                    d = abs(quote.delta)
                    if self._cfg["options"]["preferred_delta_min"] <= d <= self._cfg["options"]["preferred_delta_max"]:
                        return strike, symbol, token
                else:
                    return strike, symbol, token

        return None, None, None

    def _calculate_lots(self, premium: float, stop_price: float) -> int:
        return 1  # Fixed at 1 lot per trade — scale up only after live validation

    def _get_exit_config(self) -> dict:
        if self._is_tuesday and datetime.now().time() >= self.GAMMA_MODE_START:
            return self._cfg["gamma_mode"]
        return self._cfg["exits"]

    def _get_candidate_strikes(self, option_type: str, spot: float, expiry: date) -> List[int]:
        interval = self._cfg["options"]["strike_interval"]
        atm = GreeksCalculator.get_atm_strike(spot, interval)
        return [atm, atm + interval] if option_type == "CE" else [atm, atm - interval]

    def _get_option_quote(
        self, token: str, symbol: str, strike: int, option_type: str, expiry: date
    ) -> Optional[OptionQuote]:
        return self._md.get_option_quote(token)

    def _build_option_symbol(self, strike: int, option_type: str, expiry: date) -> str:
        expiry_str = expiry.strftime("%d%b%Y").upper()
        return f"NIFTY{expiry_str}{strike}{option_type}"

    # ── Backwards compat properties ──────────────────────────────────────────

    @property
    def current_position(self) -> Optional[AVCSPositionState]:
        return self._avcs_position

    def set_current_position(self, position: AVCSPositionState) -> None:
        self._avcs_position = position
        self.state = StrategyState.ACTIVE_POSITION

    def set_expiry(self, expiry: date) -> None:
        self._current_expiry = expiry

    def on_trade_closed(self, pnl: float, won: bool) -> None:
        """Legacy callback from main.py."""
        self._daily_trades += 1
        self._daily_pnl += pnl
        if won:
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
        self._avcs_position = None
        if self.state != StrategyState.HALTED:
            self.state = StrategyState.ARMED

        max_consec = self._cfg.get("consecutive_loss_halt", 3)
        if self._consecutive_losses >= max_consec:
            self._signal_engine.halt_session(f"{self._consecutive_losses} consecutive losses")
            self.state = StrategyState.HALTED

        self._signal_engine.update_session_state(
            daily_pnl=self._daily_pnl,
            trades_today=self._daily_trades,
            consecutive_losses=self._consecutive_losses,
        )

    def reset_daily(self) -> None:
        super().reset_daily()
        self._daily_pnl = 0.0
        self._regime_ready = False
        self._orb_ready = False
        self._orb_range = 0.0
        self._avcs_position = None
