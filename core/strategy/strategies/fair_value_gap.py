"""
FairValueGap (FVG) — Category C

Detects 3-bar price imbalance zones. When bar[i-2].high < bar[i].low
(bullish FVG) or bar[i-2].low > bar[i].high (bearish FVG), price left
a gap. Entry when price retraces into the gap.

Signal source: NIFTY Futures (recent_bars)
Execute via: CE (bullish FVG fill) / PE (bearish FVG fill)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from core.data.market_data_engine import MarketState, OHLCVBar
from core.strategy.base_strategy import (
    BaseStrategy, StrategyCategory, StrategySignal, ExitDecision, PositionState
)

logger = logging.getLogger(__name__)

SIGNAL_TYPE = "FAIR_VALUE_GAP"


@dataclass
class FVGZone:
    gap_top: float
    gap_bottom: float
    direction: str      # "BULLISH" | "BEARISH"
    age_bars: int = 0


class FairValueGap(BaseStrategy):
    """
    Setup:   3-bar FVG detected in recent bars. Gap ≥ min_gap_pct.
    Armed:   FVG is fresh (within max_age_bars) and price above/below gap
    Trigger: Current price enters the FVG zone (between gap_bottom and gap_top)
    """

    def __init__(self, config: dict, risk_config: dict) -> None:
        super().__init__("FairValueGap", StrategyCategory.MARKET_STRUCTURE, config, risk_config)
        cfg = config.get("fair_value_gap", {})
        self._min_gap_pct: float = cfg.get("min_gap_pct", 0.001)    # 0.1% minimum gap size
        self._max_age_bars: int = cfg.get("max_age_bars", 10)
        exits = config.get("exits", {})
        self._hard_stop_pct: float = exits.get("hard_stop_pct", -0.30)
        self._profit_target_pct: float = exits.get("profit_target_pct", 0.45)
        self._trailing_activation_pct: float = exits.get("trailing_activation_pct", 0.15)
        self._trailing_stop_pct: float = exits.get("trailing_stop_pct", 0.12)

        self._active_fvg: Optional[FVGZone] = None

    def _check_setup(self, ms: MarketState, option_chain=None) -> bool:
        if len(ms.recent_bars) < 3:
            return False
        if ms.india_vix > 25.0:
            return False

        bars = list(ms.recent_bars)
        # Scan recent bars for a fresh FVG
        for i in range(len(bars) - 1, 1, -1):
            b0, b2 = bars[i - 2], bars[i]
            mid_price = (b0.close + b2.close) / 2

            # Bullish FVG: gap between bar[-3] high and bar[-1] low
            if b0.high < b2.low:
                gap_pct = (b2.low - b0.high) / mid_price
                if gap_pct >= self._min_gap_pct:
                    self._active_fvg = FVGZone(
                        gap_top=b2.low,
                        gap_bottom=b0.high,
                        direction="BULLISH",
                    )
                    return True

            # Bearish FVG: gap between bar[-3] low and bar[-1] high
            if b0.low > b2.high:
                gap_pct = (b0.low - b2.high) / mid_price
                if gap_pct >= self._min_gap_pct:
                    self._active_fvg = FVGZone(
                        gap_top=b0.low,
                        gap_bottom=b2.high,
                        direction="BEARISH",
                    )
                    return True

        return False

    def _check_armed(self, ms: MarketState, option_chain=None) -> bool:
        if self._active_fvg is None:
            return False

        self._active_fvg.age_bars += 1
        if self._active_fvg.age_bars > self._max_age_bars:
            self._active_fvg = None
            return False

        fvg = self._active_fvg
        # Armed when price is approaching but not yet in gap
        if fvg.direction == "BULLISH":
            return ms.spot_price > fvg.gap_top
        else:
            return ms.spot_price < fvg.gap_bottom

    def _check_entry_trigger(self, ms: MarketState, option_chain=None) -> Optional[StrategySignal]:
        if self._active_fvg is None:
            return None

        fvg = self._active_fvg
        # Price has entered the gap
        in_gap = fvg.gap_bottom <= ms.spot_price <= fvg.gap_top

        if not in_gap:
            return None

        if fvg.direction == "BULLISH":
            direction = "CE"
            # Stop at midpoint of gap (invalidation)
            stop_price = fvg.gap_bottom - ms.atr * 0.25
            target_price = ms.spot_price + (ms.spot_price - stop_price) * 2
        else:
            direction = "PE"
            stop_price = fvg.gap_top + ms.atr * 0.25
            target_price = ms.spot_price - (stop_price - ms.spot_price) * 2

        self._active_fvg = None  # Consumed

        return StrategySignal(
            strategy_name=self.name,
            category=self.category,
            direction=direction,
            signal_type=SIGNAL_TYPE,
            confidence=0.68,
            underlying_price=ms.spot_price,
            stop_price=stop_price,
            target_price=target_price,
            meta={"gap_top": fvg.gap_top, "gap_bottom": fvg.gap_bottom},
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
