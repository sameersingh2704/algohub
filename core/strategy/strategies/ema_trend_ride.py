"""
EMATrendRide — Category D

Trend-following strategy using EMA9/EMA21/EMA50 alignment.
Enters on pullbacks to EMA21 in the direction of the primary trend.

Signal source: NIFTY Futures
Execute via: CE (bullish trend) / PE (bearish trend)
"""

from __future__ import annotations

import logging
from typing import Optional

from core.data.market_data_engine import MarketState
from core.strategy.base_strategy import (
    BaseStrategy, StrategyCategory, StrategySignal, ExitDecision, PositionState
)

logger = logging.getLogger(__name__)

SIGNAL_TYPE = "EMA_TREND_RIDE"


class EMATrendRide(BaseStrategy):
    """
    Setup:   EMA9 > EMA21 > EMA50 (bullish) or EMA9 < EMA21 < EMA50 (bearish)
    Armed:   Price pulls back to within proximity_pct of EMA21
    Trigger: Price closes with EMA9 recrossing EMA21 + RSI 40–65
    """

    def __init__(self, config: dict, risk_config: dict) -> None:
        super().__init__("EMATrendRide", StrategyCategory.TREND, config, risk_config)
        cfg = config.get("ema_trend_ride", {})
        self._proximity_pct: float = cfg.get("proximity_pct", 0.003)   # 0.3% of EMA21
        self._rsi_min: float = cfg.get("rsi_min", 40.0)
        self._rsi_max: float = cfg.get("rsi_max", 65.0)
        self._atr_stop_mult: float = cfg.get("atr_stop_mult", 0.3)
        exits = config.get("exits", {})
        self._hard_stop_pct: float = exits.get("hard_stop_pct", -0.30)
        self._profit_target_pct: float = exits.get("profit_target_pct", 0.45)
        self._trailing_activation_pct: float = exits.get("trailing_activation_pct", 0.15)
        self._trailing_stop_pct: float = exits.get("trailing_stop_pct", 0.12)

        self._trend_direction: Optional[str] = None  # "BULLISH" | "BEARISH"
        self._prev_ema9_above: Optional[bool] = None

    def _check_setup(self, ms: MarketState, option_chain=None) -> bool:
        if ms.india_vix > 25.0:
            return False

        # Require EMA50 to be warmed up (bars_count proxy)
        if ms.bars_count < 55:
            return False

        e9, e21, e50 = ms.ema_9, ms.ema_21, ms.ema_50

        if e9 > e21 > e50:
            self._trend_direction = "BULLISH"
            return True
        if e9 < e21 < e50:
            self._trend_direction = "BEARISH"
            return True

        self._trend_direction = None
        return False

    def _check_armed(self, ms: MarketState, option_chain=None) -> bool:
        if self._trend_direction is None:
            return False

        e21 = ms.ema_21
        distance = abs(ms.spot_price - e21) / e21

        # Track EMA9/21 relationship for cross detection
        self._prev_ema9_above = ms.ema_9 > ms.ema_21

        return distance <= self._proximity_pct

    def _check_entry_trigger(self, ms: MarketState, option_chain=None) -> Optional[StrategySignal]:
        if self._trend_direction is None:
            return None

        # RSI filter
        if not (self._rsi_min <= ms.rsi <= self._rsi_max):
            return None

        # EMA9 recross above EMA21 in bullish trend (or recross below in bearish)
        current_cross_up = ms.ema_9 > ms.ema_21
        if self._trend_direction == "BULLISH":
            if not (current_cross_up and self._prev_ema9_above == False):
                return None
            direction = "CE"
        else:
            if not (not current_cross_up and self._prev_ema9_above == True):
                return None
            direction = "PE"

        e50 = ms.ema_50
        stop_price = (
            e50 - self._atr_stop_mult * ms.atr
            if direction == "CE"
            else e50 + self._atr_stop_mult * ms.atr
        )

        # Target: prior swing in trend direction
        risk = abs(ms.spot_price - stop_price)
        target_price = (
            ms.spot_price + 2.0 * risk
            if direction == "CE"
            else ms.spot_price - 2.0 * risk
        )

        return StrategySignal(
            strategy_name=self.name,
            category=self.category,
            direction=direction,
            signal_type=SIGNAL_TYPE,
            confidence=0.70,
            underlying_price=ms.spot_price,
            stop_price=stop_price,
            target_price=target_price,
            meta={
                "trend": self._trend_direction,
                "ema9": ms.ema_9,
                "ema21": ms.ema_21,
                "ema50": ms.ema_50,
            },
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
