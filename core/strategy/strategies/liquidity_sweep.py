"""
LiquiditySweepReversal — Category A

Detects institutional stop hunts on equal lows (EQL) or equal highs (EQH).
When price wicks through a cluster of prior lows/highs and closes back inside,
it signals a liquidity sweep reversal.

Signal source: NIFTY Futures (recent_bars from MarketState)
Execute via: CE (bullish reversal after EQL sweep) / PE (bearish after EQH sweep)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from core.data.market_data_engine import MarketState, OHLCVBar
from core.strategy.base_strategy import (
    BaseStrategy, StrategyCategory, StrategySignal, ExitDecision, PositionState
)

logger = logging.getLogger(__name__)

SIGNAL_TYPE = "LIQUIDITY_SWEEP_REVERSAL"


@dataclass
class SweepLevel:
    price: float
    level_type: str  # "EQL" | "EQH"
    bar_indices: List[int] = field(default_factory=list)


class LiquiditySweepReversal(BaseStrategy):
    """
    Setup:   Identify EQL (≥2 equal lows within tolerance) or EQH over last N bars
    Armed:   Price approaches within proximity_pct of the level
    Trigger: Candle wicks THROUGH level but CLOSES back inside + volume spike
    """

    def __init__(self, config: dict, risk_config: dict) -> None:
        super().__init__("LiquiditySweepReversal", StrategyCategory.LIQUIDITY, config, risk_config)
        cfg = config.get("liquidity_sweep", {})
        self._lookback: int = cfg.get("lookback_bars", 20)
        self._tolerance_pct: float = cfg.get("eql_tolerance_pct", 0.001)  # 0.1%
        self._proximity_pct: float = cfg.get("proximity_pct", 0.002)      # 0.2%
        self._volume_spike_mult: float = cfg.get("volume_spike_mult", 1.5)
        self._atr_stop_mult: float = cfg.get("atr_stop_mult", 0.5)
        self._rr_target: float = cfg.get("rr_target", 2.0)
        exits = config.get("exits", {})
        self._hard_stop_pct: float = exits.get("hard_stop_pct", -0.30)
        self._profit_target_pct: float = exits.get("profit_target_pct", 0.45)
        self._trailing_activation_pct: float = exits.get("trailing_activation_pct", 0.15)
        self._trailing_stop_pct: float = exits.get("trailing_stop_pct", 0.12)

        self._identified_level: Optional[SweepLevel] = None

    # ── BaseStrategy hooks ────────────────────────────────────────────────────

    def _check_setup(self, ms: MarketState, option_chain=None) -> bool:
        if len(ms.recent_bars) < self._lookback:
            return False
        if ms.india_vix > 25.0:
            return False
        level = self._find_level(ms.recent_bars)
        if level:
            self._identified_level = level
            return True
        return False

    def _check_armed(self, ms: MarketState, option_chain=None) -> bool:
        if self._identified_level is None:
            return False
        level_price = self._identified_level.price
        distance_pct = abs(ms.spot_price - level_price) / level_price
        return distance_pct <= self._proximity_pct

    def _check_entry_trigger(self, ms: MarketState, option_chain=None) -> Optional[StrategySignal]:
        if self._identified_level is None or ms.last_bar is None:
            return None

        bar = ms.last_bar
        level = self._identified_level
        avg_volume = self._avg_volume(ms.recent_bars)

        if level.level_type == "EQL":
            # Wick below but close above
            swept = bar.low < level.price and bar.close > level.price
            direction = "CE"
        else:  # EQH
            # Wick above but close below
            swept = bar.high > level.price and bar.close < level.price
            direction = "PE"

        if not swept:
            return None

        if avg_volume > 0 and bar.volume < self._volume_spike_mult * avg_volume:
            return None

        stop_price = (
            ms.spot_price - self._atr_stop_mult * ms.atr
            if direction == "CE"
            else ms.spot_price + self._atr_stop_mult * ms.atr
        )
        risk = abs(ms.spot_price - stop_price)
        target_price = (
            ms.spot_price + self._rr_target * risk
            if direction == "CE"
            else ms.spot_price - self._rr_target * risk
        )

        self._identified_level = None  # Consumed

        return StrategySignal(
            strategy_name=self.name,
            category=self.category,
            direction=direction,
            signal_type=SIGNAL_TYPE,
            confidence=0.75,
            underlying_price=ms.spot_price,
            stop_price=stop_price,
            target_price=target_price,
            meta={"level_type": level.level_type, "level_price": level.price},
        )

    def _check_exit(self, ms: MarketState, option_price: float,
                    pos: PositionState) -> Optional[ExitDecision]:
        if option_price <= 0:
            return None

        pnl_pct = (option_price - pos.entry_price) / pos.entry_price

        # Update peak
        if option_price > pos.peak_price:
            pos.peak_price = option_price

        # Hard stop
        if pnl_pct <= self._hard_stop_pct:
            return ExitDecision(action="EXIT", reason="HARD_STOP")

        # Profit target
        if pnl_pct >= self._profit_target_pct:
            return ExitDecision(action="EXIT", reason="PROFIT_TARGET")

        # Trailing stop
        if pnl_pct >= self._trailing_activation_pct:
            trail_stop = pos.peak_price * (1 - self._trailing_stop_pct)
            if option_price <= trail_stop:
                return ExitDecision(action="EXIT", reason="TRAILING_STOP")

        # Intraday cutoff 15:15
        if ms.timestamp.hour >= 15 and ms.timestamp.minute >= 15:
            return ExitDecision(action="EXIT", reason="TIME_STOP")

        return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _find_level(self, bars: List[OHLCVBar]) -> Optional[SweepLevel]:
        recent = list(bars)[-self._lookback:]
        # EQL — cluster of matching lows
        lows = [(i, b.low) for i, b in enumerate(recent)]
        lows.sort(key=lambda x: x[1])
        level = self._find_cluster(lows, "EQL")
        if level:
            return level
        # EQH — cluster of matching highs
        highs = [(i, b.high) for i, b in enumerate(recent)]
        highs.sort(key=lambda x: x[1], reverse=True)
        return self._find_cluster(highs, "EQH")

    def _find_cluster(self, sorted_prices: List[Tuple[int, float]], level_type: str) -> Optional[SweepLevel]:
        for i in range(len(sorted_prices) - 1):
            p1, v1 = sorted_prices[i]
            p2, v2 = sorted_prices[i + 1]
            if v1 == 0:
                continue
            if abs(v1 - v2) / v1 <= self._tolerance_pct:
                avg_price = (v1 + v2) / 2
                return SweepLevel(price=avg_price, level_type=level_type, bar_indices=[p1, p2])
        return None

    @staticmethod
    def _avg_volume(bars: List[OHLCVBar]) -> float:
        vols = [b.volume for b in bars if b.volume > 0]
        return sum(vols) / len(vols) if vols else 0.0
