"""
BOSRetest — Category C

Break of Structure (BOS) + retest entry.
After a structural break (close beyond prior swing), waits for price to
pull back to the broken level and enter on rejection.

Signal source: NIFTY Futures (recent_bars)
Execute via: CE (bullish BOS retest) / PE (bearish BOS retest)
"""

from __future__ import annotations

import logging
from typing import List, Optional

from core.data.market_data_engine import MarketState, OHLCVBar
from core.strategy.base_strategy import (
    BaseStrategy, StrategyCategory, StrategySignal, ExitDecision, PositionState
)

logger = logging.getLogger(__name__)

SIGNAL_TYPE = "BOS_RETEST"


class BOSRetest(BaseStrategy):
    """
    Setup:   Price closes >breakout_pct% beyond prior swing high (bullish BOS)
             or below prior swing low (bearish BOS)
    Armed:   BOS confirmed; watching for pullback to broken level
    Trigger: Price enters retest zone + rejection candle (wick rejection)
    """

    def __init__(self, config: dict, risk_config: dict) -> None:
        super().__init__("BOSRetest", StrategyCategory.MARKET_STRUCTURE, config, risk_config)
        cfg = config.get("bos_retest", {})
        self._lookback: int = cfg.get("lookback_bars", 20)
        self._bos_pct: float = cfg.get("bos_pct", 0.0015)         # 0.15% conviction break
        self._retest_zone_pct: float = cfg.get("retest_zone_pct", 0.002)  # 0.2% from level
        self._wick_ratio: float = cfg.get("wick_ratio", 0.5)       # wick must be >50% of bar
        self._rsi_max: float = cfg.get("rsi_max", 70.0)
        self._rsi_min: float = cfg.get("rsi_min", 30.0)
        self._atr_stop_mult: float = cfg.get("atr_stop_mult", 0.5)
        self._rr_min: float = cfg.get("rr_min", 2.0)
        exits = config.get("exits", {})
        self._hard_stop_pct: float = exits.get("hard_stop_pct", -0.30)
        self._profit_target_pct: float = exits.get("profit_target_pct", 0.45)
        self._trailing_activation_pct: float = exits.get("trailing_activation_pct", 0.15)
        self._trailing_stop_pct: float = exits.get("trailing_stop_pct", 0.12)

        self._bos_level: float = 0.0
        self._bos_direction: Optional[str] = None  # "BULLISH" | "BEARISH"
        self._swing_high: float = 0.0
        self._swing_low: float = float("inf")

    def _check_setup(self, ms: MarketState, option_chain=None) -> bool:
        if len(ms.recent_bars) < self._lookback + 1:
            return False
        if ms.india_vix > 25.0:
            return False

        bars = list(ms.recent_bars)
        prior = bars[-(self._lookback + 1):-1]
        self._swing_high = max(b.high for b in prior)
        self._swing_low = min(b.low for b in prior)

        last = bars[-1]
        # Bullish BOS: close breaks above prior swing high
        if last.close > self._swing_high * (1 + self._bos_pct):
            self._bos_level = self._swing_high
            self._bos_direction = "BULLISH"
            return True

        # Bearish BOS: close breaks below prior swing low
        if last.close < self._swing_low * (1 - self._bos_pct):
            self._bos_level = self._swing_low
            self._bos_direction = "BEARISH"
            return True

        return False

    def _check_armed(self, ms: MarketState, option_chain=None) -> bool:
        if self._bos_direction is None:
            return False
        # BOS should still be intact (price hasn't collapsed back through opposite side)
        if self._bos_direction == "BULLISH":
            return ms.spot_price > self._bos_level * (1 - 0.003)
        else:
            return ms.spot_price < self._bos_level * (1 + 0.003)

    def _check_entry_trigger(self, ms: MarketState, option_chain=None) -> Optional[StrategySignal]:
        if self._bos_direction is None or ms.last_bar is None:
            return None

        bar = ms.last_bar
        level = self._bos_level

        # In retest zone?
        in_zone = abs(ms.spot_price - level) / level <= self._retest_zone_pct

        if not in_zone:
            return None

        # RSI filter — not extreme
        if self._bos_direction == "BULLISH" and ms.rsi > self._rsi_max:
            return None
        if self._bos_direction == "BEARISH" and ms.rsi < self._rsi_min:
            return None

        # Rejection candle check
        bar_range = bar.high - bar.low
        if bar_range == 0:
            return None

        if self._bos_direction == "BULLISH":
            # Support retest → look for lower wick rejection (bullish candle)
            lower_wick = bar.open - bar.low if bar.close > bar.open else bar.close - bar.low
            if lower_wick / bar_range < self._wick_ratio:
                return None
            direction = "CE"
        else:
            # Resistance retest → look for upper wick rejection
            upper_wick = bar.high - bar.open if bar.close < bar.open else bar.high - bar.close
            if upper_wick / bar_range < self._wick_ratio:
                return None
            direction = "PE"

        stop_price = (
            level - self._atr_stop_mult * ms.atr
            if direction == "CE"
            else level + self._atr_stop_mult * ms.atr
        )
        risk = abs(ms.spot_price - stop_price)
        target_price = (
            ms.spot_price + self._rr_min * risk
            if direction == "CE"
            else ms.spot_price - self._rr_min * risk
        )

        self._bos_direction = None  # Consumed

        return StrategySignal(
            strategy_name=self.name,
            category=self.category,
            direction=direction,
            signal_type=SIGNAL_TYPE,
            confidence=0.72,
            underlying_price=ms.spot_price,
            stop_price=stop_price,
            target_price=target_price,
            meta={"bos_level": level},
        )

    def _check_exit(self, ms: MarketState, option_price: float,
                    pos: PositionState) -> Optional[ExitDecision]:
        if option_price <= 0:
            return None

        pnl_pct = (option_price - pos.entry_price) / pos.entry_price

        if option_price > pos.peak_price:
            pos.peak_price = option_price

        if pnl_pct <= self._hard_stop_pct:
            return ExitDecision(action="EXIT", reason="HARD_STOP")

        if pnl_pct >= self._profit_target_pct:
            return ExitDecision(action="EXIT", reason="PROFIT_TARGET")

        if pnl_pct >= self._trailing_activation_pct:
            trail_stop = pos.peak_price * (1 - self._trailing_stop_pct)
            if option_price <= trail_stop:
                return ExitDecision(action="EXIT", reason="TRAILING_STOP")

        if ms.timestamp.hour >= 15 and ms.timestamp.minute >= 15:
            return ExitDecision(action="EXIT", reason="TIME_STOP")

        return None
