"""
BollingerReversion — Category E

Fades price when it closes outside the Bollinger Band (20, 2σ).
Entry on the first bar that closes back inside the band, confirming
overextension rather than a band walk (trend continuation).

Signal source: NIFTY Futures
Execute via: PE (closed outside upper BB) / CE (closed outside lower BB)
"""

from __future__ import annotations

import logging
from typing import Optional

from core.data.market_data_engine import MarketState
from core.strategy.base_strategy import (
    BaseStrategy, StrategyCategory, StrategySignal, ExitDecision, PositionState
)

logger = logging.getLogger(__name__)

SIGNAL_TYPE = "BOLLINGER_REVERSION"


class BollingerReversion(BaseStrategy):
    """
    Setup:   Price closes outside Bollinger Band (20, 2σ)
    Armed:   First close outside; RSI confirms overextension (>70 or <30)
    Trigger: Next bar closes back inside the band (reversion confirmed)
    """

    def __init__(self, config: dict, risk_config: dict) -> None:
        super().__init__("BollingerReversion", StrategyCategory.MEAN_REVERSION, config, risk_config)
        cfg = config.get("bollinger_reversion", {})
        self._rsi_overbought: float = cfg.get("rsi_overbought", 70.0)
        self._rsi_oversold: float = cfg.get("rsi_oversold", 30.0)
        self._atr_stop_mult: float = cfg.get("atr_stop_mult", 0.5)
        exits = config.get("exits", {})
        self._hard_stop_pct: float = exits.get("hard_stop_pct", -0.30)
        self._profit_target_pct: float = exits.get("profit_target_pct", 0.45)
        self._trailing_activation_pct: float = exits.get("trailing_activation_pct", 0.15)
        self._trailing_stop_pct: float = exits.get("trailing_stop_pct", 0.12)

        self._outside_direction: Optional[str] = None  # "ABOVE" | "BELOW"
        self._band_extreme: float = 0.0  # The band level that was breached

    def _check_setup(self, ms: MarketState, option_chain=None) -> bool:
        # BB needs 20 bars to warm up
        if ms.bb_middle <= 0 or ms.bars_count < 22:
            return False
        if ms.india_vix > 25.0:
            return False
        return True

    def _check_armed(self, ms: MarketState, option_chain=None) -> bool:
        if ms.bb_upper <= 0:
            return False

        if ms.last_bar is None:
            return False

        bar = ms.last_bar

        # Price closed outside upper band
        if bar.close > ms.bb_upper and ms.rsi > self._rsi_overbought:
            self._outside_direction = "ABOVE"
            self._band_extreme = ms.bb_upper
            return True

        # Price closed outside lower band
        if bar.close < ms.bb_lower and ms.rsi < self._rsi_oversold:
            self._outside_direction = "BELOW"
            self._band_extreme = ms.bb_lower
            return True

        return self._outside_direction is not None

    def _check_entry_trigger(self, ms: MarketState, option_chain=None) -> Optional[StrategySignal]:
        if self._outside_direction is None or ms.last_bar is None:
            return None

        bar = ms.last_bar

        if self._outside_direction == "ABOVE":
            # Reversion: close drops back inside upper band
            reversion = bar.close < ms.bb_upper
            direction = "PE"
            stop_price = self._band_extreme + self._atr_stop_mult * ms.atr
        else:
            # Reversion: close rises back inside lower band
            reversion = bar.close > ms.bb_lower
            direction = "CE"
            stop_price = self._band_extreme - self._atr_stop_mult * ms.atr

        if not reversion:
            return None

        target_price = ms.bb_middle  # Target is the middle band (20 SMA)

        self._outside_direction = None

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
                "bb_upper": round(ms.bb_upper, 2),
                "bb_middle": round(ms.bb_middle, 2),
                "bb_lower": round(ms.bb_lower, 2),
                "bb_width": round(ms.bb_width, 4),
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
