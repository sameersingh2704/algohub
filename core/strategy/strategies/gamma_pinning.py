"""
GammaPinning — Category F (Tuesday only)

Exploits max pain mechanics on NIFTY weekly expiry day (Tuesday).
When spot is displaced from max pain, gamma forces market makers to
drive price toward max pain as expiry approaches.

Signal source: Options Chain (max_pain, pcr from OptionChainSnapshot)
              NIFTY Futures (EMA slope filter)
Execute via: PE (spot above max pain) / CE (spot below max pain)
Active:      Tuesdays only, after 10:30 AM, exit cutoff 15:00
"""

from __future__ import annotations

import logging
from datetime import time as dtime
from typing import Optional

from core.data.market_data_engine import MarketState
from core.strategy.base_strategy import (
    BaseStrategy, StrategyCategory, StrategySignal, ExitDecision, PositionState
)

logger = logging.getLogger(__name__)

SIGNAL_TYPE = "GAMMA_PINNING"

ACTIVATION_TIME = dtime(10, 30, 0)
EXIT_CUTOFF_TIME = dtime(15, 0, 0)


class GammaPinning(BaseStrategy):
    """
    Setup:   Tuesday, after 10:30. Max pain available. |spot - max_pain| > displacement_pct.
    Armed:   Displacement maintained ≥ min_displacement_bars. Weak directional trend.
    Trigger: PCR near neutral (1.0 ± pcr_band) and OI building at max pain strike.
    """

    def __init__(self, config: dict, risk_config: dict) -> None:
        super().__init__("GammaPinning", StrategyCategory.OPTIONS, config, risk_config)
        cfg = config.get("gamma_pinning", {})
        self._displacement_pct: float = cfg.get("displacement_pct", 0.005)    # 0.5%
        self._min_displacement_bars: int = cfg.get("min_displacement_bars", 3)
        self._ema_slope_max: float = cfg.get("ema_slope_max", 0.001)          # Weak trend threshold
        self._pcr_neutral_min: float = cfg.get("pcr_neutral_min", 0.85)
        self._pcr_neutral_max: float = cfg.get("pcr_neutral_max", 1.15)
        self._stop_additional_pct: float = cfg.get("stop_additional_pct", 0.005)  # 0.5% further
        exits = config.get("exits", {})
        self._hard_stop_pct: float = exits.get("hard_stop_pct", -0.35)        # Gamma day: tighter
        self._profit_target_pct: float = exits.get("profit_target_pct", 0.55)

        self._max_pain: float = 0.0
        self._displacement_bars: int = 0
        self._displacement_direction: Optional[str] = None

    def _check_setup(self, ms: MarketState, option_chain=None) -> bool:
        # Tuesday only
        if ms.timestamp.weekday() != 1:  # 1 = Tuesday
            return False

        # After activation time
        if ms.timestamp.time() < ACTIVATION_TIME:
            return False

        # Before exit cutoff
        if ms.timestamp.time() >= EXIT_CUTOFF_TIME:
            return False

        if option_chain is None:
            return False

        max_pain = getattr(option_chain, "max_pain", 0)
        if max_pain <= 0:
            return False

        self._max_pain = float(max_pain)

        displacement = (ms.spot_price - self._max_pain) / self._max_pain
        if abs(displacement) >= self._displacement_pct:
            direction = "ABOVE" if displacement > 0 else "BELOW"
            if direction == self._displacement_direction:
                self._displacement_bars += 1
            else:
                self._displacement_direction = direction
                self._displacement_bars = 1
            return True

        self._displacement_bars = 0
        self._displacement_direction = None
        return False

    def _check_armed(self, ms: MarketState, option_chain=None) -> bool:
        if self._displacement_bars < self._min_displacement_bars:
            return False

        # Weak trend check: EMA9 slope should be small
        # Use VWAP slope as proxy (available in MarketState)
        if abs(ms.vwap_slope) > self._ema_slope_max:
            return False

        return True

    def _check_entry_trigger(self, ms: MarketState, option_chain=None) -> Optional[StrategySignal]:
        if self._displacement_direction is None or option_chain is None:
            return None

        # PCR near neutral
        pcr = getattr(option_chain, "pcr", None)
        if pcr is None:
            return None

        if not (self._pcr_neutral_min <= pcr <= self._pcr_neutral_max):
            return None

        if self._displacement_direction == "ABOVE":
            direction = "PE"
            stop_price = ms.spot_price * (1 + self._stop_additional_pct)
        else:
            direction = "CE"
            stop_price = ms.spot_price * (1 - self._stop_additional_pct)

        target_price = self._max_pain  # Target is max pain strike

        self._displacement_direction = None
        self._displacement_bars = 0

        return StrategySignal(
            strategy_name=self.name,
            category=self.category,
            direction=direction,
            signal_type=SIGNAL_TYPE,
            confidence=0.72,
            underlying_price=ms.spot_price,
            suggested_strike=int(self._max_pain),
            stop_price=stop_price,
            target_price=target_price,
            meta={
                "max_pain": self._max_pain,
                "pcr": pcr,
                "displacement_pct": round((ms.spot_price - self._max_pain) / self._max_pain * 100, 2),
            },
        )

    def _check_exit(self, ms: MarketState, option_price: float,
                    pos: PositionState) -> Optional[ExitDecision]:
        if option_price <= 0:
            return None

        # Gamma day exit cutoff: 15:00
        if ms.timestamp.time() >= EXIT_CUTOFF_TIME:
            return ExitDecision(action="EXIT", reason="TIME_STOP")

        pnl_pct = (option_price - pos.entry_price) / pos.entry_price

        if option_price > pos.peak_price:
            pos.peak_price = option_price

        if pnl_pct <= self._hard_stop_pct:
            return ExitDecision(action="EXIT", reason="HARD_STOP")

        if pnl_pct >= self._profit_target_pct:
            return ExitDecision(action="EXIT", reason="PROFIT_TARGET")

        # Trailing stop: 15% below peak
        if pnl_pct >= 0.15:
            trail_stop = pos.peak_price * 0.85
            if option_price <= trail_stop:
                return ExitDecision(action="EXIT", reason="TRAILING_STOP")

        return None
