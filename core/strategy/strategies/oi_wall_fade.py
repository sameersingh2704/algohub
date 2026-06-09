"""
OIWallFade — Category F (Options)

Fades NIFTY when it approaches a heavy OI "wall" (the strike with the highest
open interest on one side of the chain). The OI wall acts as a magnetic ceiling
(call wall) or floor (put wall) because market-makers delta-hedge by selling the
underlying as the call wall is tested, or buying when the put wall is tested.

SETUP:  option_chain available, time 9:30–14:30, VIX < 22
ARMED:  spot within approach_zone of top CE strike (→ buy PE)
        or top PE strike (→ buy CE)
TRIGGER: rejection bar (upper/lower wick > 1.5 × body) or directional close
EXIT:   standard trailing-stop / target / time-stop
"""

from __future__ import annotations

import logging
from typing import Optional

from core.data.market_data_engine import MarketState
from core.strategy.base_strategy import (
    BaseStrategy, StrategyCategory, StrategySignal, ExitDecision, PositionState,
)

logger = logging.getLogger(__name__)

_WINDOW_START = (9, 30)
_WINDOW_END   = (14, 30)
_TIME_STOP    = (15, 0)


class OIWallFade(BaseStrategy):

    def __init__(self, config: dict, risk_config: dict) -> None:
        super().__init__("OIWallFade", StrategyCategory.OPTIONS, config, risk_config)
        cfg = config.get("oi_wall_fade", {})
        self._approach_zone: float        = cfg.get("approach_zone_pts", 50.0)
        self._vol_mult: float             = cfg.get("vol_mult", 1.0)
        self._rsi_min: float              = cfg.get("rsi_min", 30.0)
        self._rsi_max: float              = cfg.get("rsi_max", 70.0)
        self._vix_max: float              = cfg.get("vix_max", 22.0)
        self._rejection_wick_ratio: float = cfg.get("rejection_wick_ratio", 1.5)
        self._atr_stop_mult: float        = cfg.get("atr_stop_mult", 0.3)
        self._atr_target_mult: float      = cfg.get("atr_target_mult", 1.5)

        # per-session state
        self._wall_level: float       = 0.0
        self._signal_dir: Optional[str] = None

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _in_window(ts) -> bool:
        t = (ts.hour, ts.minute)
        return _WINDOW_START <= t < _WINDOW_END

    @staticmethod
    def _avg_volume(bars) -> float:
        vols = [b.volume for b in bars if b.volume > 0]
        return sum(vols) / len(vols) if vols else 0.0

    # ── state machine ─────────────────────────────────────────────────────────

    def _check_setup(self, ms: MarketState, option_chain=None) -> bool:
        if not self._in_window(ms.timestamp):
            return False
        if ms.india_vix > self._vix_max:
            return False
        if option_chain is None:
            return False
        return bool(option_chain.top_ce_strikes or option_chain.top_pe_strikes)

    def _check_armed(self, ms: MarketState, option_chain=None) -> bool:
        if option_chain is None:
            return False
        spot = ms.spot_price

        # Spot approaching call wall from below → fade → buy PE
        if option_chain.top_ce_strikes:
            wall = option_chain.top_ce_strikes[0]
            if spot < wall and (wall - spot) <= self._approach_zone:
                self._wall_level = wall
                self._signal_dir = "PE"
                return True

        # Spot approaching put wall from above → bounce → buy CE
        if option_chain.top_pe_strikes:
            wall = option_chain.top_pe_strikes[0]
            if spot > wall and (spot - wall) <= self._approach_zone:
                self._wall_level = wall
                self._signal_dir = "CE"
                return True

        self._signal_dir = None
        return False

    def _check_entry_trigger(self, ms: MarketState, option_chain=None) -> Optional[StrategySignal]:
        if self._signal_dir is None or ms.last_bar is None:
            return None

        bar = ms.last_bar
        d   = self._signal_dir

        if not (self._rsi_min <= ms.rsi <= self._rsi_max):
            return None

        upper_wick = bar.high - max(bar.open, bar.close)
        lower_wick = min(bar.open, bar.close) - bar.low
        body = max(bar.body_size, 0.5)  # avoid div-by-zero on doji

        if d == "PE":
            # Wall held: spot still below wall, rejection wick or bearish close
            if ms.spot_price >= self._wall_level:
                self._signal_dir = None
                return None
            if not (upper_wick >= self._rejection_wick_ratio * body or bar.is_bearish):
                return None
        else:
            # Floor held: spot still above wall, lower-wick bounce or bullish close
            if ms.spot_price <= self._wall_level:
                self._signal_dir = None
                return None
            if not (lower_wick >= self._rejection_wick_ratio * body or bar.is_bullish):
                return None

        avg_vol = self._avg_volume(ms.recent_bars)
        if avg_vol > 0 and bar.volume < self._vol_mult * avg_vol:
            return None

        atr = ms.atr or 30.0
        if d == "PE":
            stop_price   = self._wall_level + atr * self._atr_stop_mult
            target_price = ms.spot_price - atr * self._atr_target_mult
        else:
            stop_price   = self._wall_level - atr * self._atr_stop_mult
            target_price = ms.spot_price + atr * self._atr_target_mult

        return StrategySignal(
            strategy_name    = self.name,
            category         = self.category,
            direction        = d,
            signal_type      = "OI_WALL_REJECTION",
            confidence       = 0.70,
            underlying_price = ms.spot_price,
            stop_price       = stop_price,
            target_price     = target_price,
            meta             = {"wall_level": self._wall_level, "rsi": ms.rsi, "vwap": ms.vwap},
        )

    def _check_exit(self, ms: MarketState, option_price: float,
                    pos: PositionState) -> Optional[ExitDecision]:
        return self._standard_exit_check(ms, option_price, pos, _TIME_STOP)
