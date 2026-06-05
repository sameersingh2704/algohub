"""
FalseBreakoutTrap — Category A

Detects failed breakouts above prior swing highs or below prior swing lows.
When price "breaks out" but quickly reverses back inside with weak volume,
the breakout trap is triggered.

Signal source: NIFTY Futures (recent_bars)
Execute via: PE (failed upside breakout) / CE (failed downside breakout)
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from core.data.market_data_engine import MarketState, OHLCVBar
from core.strategy.base_strategy import (
    BaseStrategy, StrategyCategory, StrategySignal, ExitDecision, PositionState
)

logger = logging.getLogger(__name__)

SIGNAL_TYPE = "FALSE_BREAKOUT_TRAP"


class FalseBreakoutTrap(BaseStrategy):
    """
    Setup:   Identify prior swing high/low over lookback window
    Armed:   Price closes beyond swing level (breakout confirmed)
    Trigger: Next bar closes back inside the level + volume < weak_vol_mult * avg
    """

    def __init__(self, config: dict, risk_config: dict) -> None:
        super().__init__("FalseBreakoutTrap", StrategyCategory.LIQUIDITY, config, risk_config)
        cfg = config.get("false_breakout", {})
        self._lookback: int = cfg.get("lookback_bars", 20)
        self._breakout_pct: float = cfg.get("breakout_pct", 0.001)    # 0.1% beyond level
        self._reversal_bars: int = cfg.get("reversal_bars", 3)        # bars to confirm reversal
        self._weak_vol_mult: float = cfg.get("weak_vol_mult", 0.8)
        self._atr_stop_mult: float = cfg.get("atr_stop_mult", 0.5)
        exits = config.get("exits", {})
        self._hard_stop_pct: float = exits.get("hard_stop_pct", -0.30)
        self._profit_target_pct: float = exits.get("profit_target_pct", 0.45)
        self._trailing_activation_pct: float = exits.get("trailing_activation_pct", 0.15)
        self._trailing_stop_pct: float = exits.get("trailing_stop_pct", 0.12)

        self._swing_high: float = 0.0
        self._swing_low: float = float("inf")
        self._breakout_direction: Optional[str] = None  # "UP" | "DOWN"
        self._breakout_level: float = 0.0
        self._bars_since_breakout: int = 0

    def _check_setup(self, ms: MarketState, option_chain=None) -> bool:
        if len(ms.recent_bars) < self._lookback:
            return False
        if ms.india_vix > 25.0:
            return False
        bars = list(ms.recent_bars)[-self._lookback:]
        self._swing_high = max(b.high for b in bars)
        self._swing_low = min(b.low for b in bars)
        return True

    def _check_armed(self, ms: MarketState, option_chain=None) -> bool:
        if ms.last_bar is None:
            return False
        bar = ms.last_bar

        # Check for breakout close
        if bar.close > self._swing_high * (1 + self._breakout_pct):
            self._breakout_direction = "UP"
            self._breakout_level = self._swing_high
            self._bars_since_breakout = 0
            return True

        if bar.close < self._swing_low * (1 - self._breakout_pct):
            self._breakout_direction = "DOWN"
            self._breakout_level = self._swing_low
            self._bars_since_breakout = 0
            return True

        # Count bars since breakout
        if self._breakout_direction is not None:
            self._bars_since_breakout += 1
            if self._bars_since_breakout > self._reversal_bars:
                self._breakout_direction = None
                return False
            return True

        return False

    def _check_entry_trigger(self, ms: MarketState, option_chain=None) -> Optional[StrategySignal]:
        if self._breakout_direction is None or ms.last_bar is None:
            return None

        bar = ms.last_bar
        avg_volume = self._avg_volume(ms.recent_bars)

        if self._breakout_direction == "UP":
            # Failed breakout up → price closes back below breakout level
            reversed_ = bar.close < self._breakout_level
            direction = "PE"
        else:
            # Failed breakout down → price closes back above breakout level
            reversed_ = bar.close > self._breakout_level
            direction = "CE"

        if not reversed_:
            return None

        # Weak volume confirms false breakout (not a strong move)
        if avg_volume > 0 and bar.volume > self._weak_vol_mult * avg_volume:
            return None

        stop_price = (
            self._breakout_level + self._atr_stop_mult * ms.atr
            if direction == "PE"
            else self._breakout_level - self._atr_stop_mult * ms.atr
        )

        # Target: back to opposite end of prior range
        target_price = (
            self._swing_low if direction == "PE" else self._swing_high
        )

        self._breakout_direction = None

        return StrategySignal(
            strategy_name=self.name,
            category=self.category,
            direction=direction,
            signal_type=SIGNAL_TYPE,
            confidence=0.70,
            underlying_price=ms.spot_price,
            stop_price=stop_price,
            target_price=target_price,
            meta={"breakout_level": self._breakout_level},
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

    @staticmethod
    def _avg_volume(bars) -> float:
        vols = [b.volume for b in bars if b.volume > 0]
        return sum(vols) / len(vols) if vols else 0.0
