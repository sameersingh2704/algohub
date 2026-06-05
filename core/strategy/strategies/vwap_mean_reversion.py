"""
VWAPMeanReversion — Category E

Mean-reversion play when price extends significantly from VWAP.
Fades the extension back toward VWAP using a reversal candle trigger.

Signal source: NIFTY Futures
Execute via: PE (extended above VWAP) / CE (extended below VWAP)
"""

from __future__ import annotations

import logging
from typing import Optional

from core.data.market_data_engine import MarketState
from core.strategy.base_strategy import (
    BaseStrategy, StrategyCategory, StrategySignal, ExitDecision, PositionState
)

logger = logging.getLogger(__name__)

SIGNAL_TYPE = "VWAP_MEAN_REVERSION"


class VWAPMeanReversion(BaseStrategy):
    """
    Setup:   Price is ≥ extension_pct away from VWAP
    Armed:   Extended ≥ min_extension_bars consecutive bars
    Trigger: Reversal candle (closes toward VWAP) + volume declining
    """

    def __init__(self, config: dict, risk_config: dict) -> None:
        super().__init__("VWAPMeanReversion", StrategyCategory.MEAN_REVERSION, config, risk_config)
        cfg = config.get("vwap_mean_reversion", {})
        self._extension_pct: float = cfg.get("extension_pct", 0.008)    # 0.8%
        self._min_extension_bars: int = cfg.get("min_extension_bars", 3)
        self._rsi_extreme_ce: float = cfg.get("rsi_extreme_ce", 35.0)   # Oversold for CE
        self._rsi_extreme_pe: float = cfg.get("rsi_extreme_pe", 65.0)   # Overbought for PE
        self._stop_buffer_pct: float = cfg.get("stop_buffer_pct", 0.0025)  # 0.25% beyond extreme
        exits = config.get("exits", {})
        self._hard_stop_pct: float = exits.get("hard_stop_pct", -0.30)
        self._profit_target_pct: float = exits.get("profit_target_pct", 0.45)
        self._trailing_activation_pct: float = exits.get("trailing_activation_pct", 0.15)
        self._trailing_stop_pct: float = exits.get("trailing_stop_pct", 0.12)

        self._extension_bars: int = 0
        self._extension_direction: Optional[str] = None  # "ABOVE" | "BELOW"
        self._extension_extreme: float = 0.0
        self._prev_volume: int = 0

    def _check_setup(self, ms: MarketState, option_chain=None) -> bool:
        if ms.vwap <= 0:
            return False
        if ms.india_vix > 25.0:
            return False

        extension = (ms.spot_price - ms.vwap) / ms.vwap

        if abs(extension) >= self._extension_pct:
            direction = "ABOVE" if extension > 0 else "BELOW"
            if direction == self._extension_direction:
                self._extension_bars += 1
                if ms.spot_price > self._extension_extreme and direction == "ABOVE":
                    self._extension_extreme = ms.spot_price
                elif ms.spot_price < self._extension_extreme and direction == "BELOW":
                    self._extension_extreme = ms.spot_price
            else:
                self._extension_direction = direction
                self._extension_bars = 1
                self._extension_extreme = ms.spot_price
            return True

        self._extension_bars = 0
        self._extension_direction = None
        return False

    def _check_armed(self, ms: MarketState, option_chain=None) -> bool:
        return self._extension_bars >= self._min_extension_bars

    def _check_entry_trigger(self, ms: MarketState, option_chain=None) -> Optional[StrategySignal]:
        if self._extension_direction is None or ms.last_bar is None:
            return None

        bar = ms.last_bar

        # RSI must be at extreme
        if self._extension_direction == "ABOVE" and ms.rsi < self._rsi_extreme_pe:
            return None
        if self._extension_direction == "BELOW" and ms.rsi > self._rsi_extreme_ce:
            return None

        # Reversal candle check (close moves toward VWAP)
        if self._extension_direction == "ABOVE":
            reversal = bar.close < bar.open  # Bearish candle
            direction = "PE"
        else:
            reversal = bar.close > bar.open  # Bullish candle
            direction = "CE"

        if not reversal:
            return None

        # Volume declining
        if ms.last_bar.volume > 0 and self._prev_volume > 0:
            if ms.last_bar.volume > self._prev_volume:
                return None  # Volume increasing = not exhaustion

        self._prev_volume = ms.last_bar.volume if ms.last_bar else 0

        # Stop beyond the extreme
        if direction == "PE":
            stop_price = self._extension_extreme * (1 + self._stop_buffer_pct)
        else:
            stop_price = self._extension_extreme * (1 - self._stop_buffer_pct)

        target_price = ms.vwap  # Target is VWAP

        self._extension_bars = 0
        self._extension_direction = None

        return StrategySignal(
            strategy_name=self.name,
            category=self.category,
            direction=direction,
            signal_type=SIGNAL_TYPE,
            confidence=0.65,
            underlying_price=ms.spot_price,
            stop_price=stop_price,
            target_price=target_price,
            meta={
                "vwap": ms.vwap,
                "extension_pct": round((ms.spot_price - ms.vwap) / ms.vwap * 100, 2),
                "rsi": ms.rsi,
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
