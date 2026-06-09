"""
CPRBreakout — Category D (Trend)

On narrow-CPR days (TC - BC < threshold), the market tends to trend once it
escapes the Central Pivot Range. A confirmed close above TC signals bullish
trending → buy CE; a confirmed close below BC signals bearish trending → buy PE.

CPR levels are computed from the previous day's High/Low/Close and injected
via set_session_context() before market open:
  cpr_tc  — Top Central Pivot (resistance)
  cpr_bc  — Bottom Central Pivot (support)

SETUP:  cpr_tc/bc available, cpr_width < 40 pts, ORB established, 9:35–13:00
ARMED:  spot approaching TC (from below) or BC (from above) within 15 pts
TRIGGER: close beyond TC/BC + volume ≥ 1.3× avg + RSI confirms direction
EXIT:   standard; time-stop 14:30
"""

from __future__ import annotations

import logging
from typing import Optional

from core.data.market_data_engine import MarketState
from core.strategy.base_strategy import (
    BaseStrategy, StrategyCategory, StrategySignal, ExitDecision, PositionState,
)

logger = logging.getLogger(__name__)

_WINDOW_START  = (9, 35)
_WINDOW_END    = (13, 0)
_TIME_STOP     = (14, 30)
_CPR_WIDTH_MAX = 40.0     # only trade narrow-CPR days
_APPROACH_PTS  = 15.0     # armed when within this many points of TC/BC
_VOL_MULT      = 1.3
_RSI_CE_MIN    = 55.0
_RSI_PE_MAX    = 45.0


class CPRBreakout(BaseStrategy):

    def __init__(self, config: dict, risk_config: dict) -> None:
        super().__init__("CPRBreakout", StrategyCategory.TREND, config, risk_config)
        cfg = config.get("cpr_breakout", {})
        self._cpr_width_max: float  = cfg.get("cpr_width_max", _CPR_WIDTH_MAX)
        self._approach_pts: float   = cfg.get("approach_pts", _APPROACH_PTS)
        self._vol_mult: float       = cfg.get("vol_mult", _VOL_MULT)
        self._rsi_ce_min: float     = cfg.get("rsi_ce_min", _RSI_CE_MIN)
        self._rsi_pe_max: float     = cfg.get("rsi_pe_max", _RSI_PE_MAX)
        self._atr_stop_mult: float  = cfg.get("atr_stop_mult", 0.5)
        self._rr_target: float      = cfg.get("rr_target", 2.0)
        self._vix_max: float        = cfg.get("vix_max", 22.0)

        self._cpr_tc: float          = 0.0
        self._cpr_bc: float          = 0.0
        self._signal_dir: Optional[str] = None

    @staticmethod
    def _in_window(ts) -> bool:
        t = (ts.hour, ts.minute)
        return _WINDOW_START <= t < _WINDOW_END

    @staticmethod
    def _avg_volume(bars) -> float:
        vols = [b.volume for b in bars if b.volume > 0]
        return sum(vols) / len(vols) if vols else 0.0

    def _check_setup(self, ms: MarketState, option_chain=None) -> bool:
        if not self._in_window(ms.timestamp):
            return False
        if ms.india_vix > self._vix_max:
            return False
        # ORB must be established (need enough bars)
        if ms.orb_high <= 0 or ms.orb_low <= 0 or ms.orb_low == float("inf"):
            return False

        tc = self._session_ctx.get("cpr_tc", 0.0)
        bc = self._session_ctx.get("cpr_bc", 0.0)
        if tc <= 0 or bc <= 0:
            return False

        self._cpr_tc = tc
        self._cpr_bc = bc
        cpr_width = tc - bc

        return 0 < cpr_width <= self._cpr_width_max

    def _check_armed(self, ms: MarketState, option_chain=None) -> bool:
        spot = ms.spot_price

        # Approaching TC from below → look for CE breakout
        if spot < self._cpr_tc and (self._cpr_tc - spot) <= self._approach_pts:
            if ms.vwap > 0 and spot > ms.vwap:  # VWAP below spot = bullish
                self._signal_dir = "CE"
                return True

        # Approaching BC from above → look for PE breakdown
        if spot > self._cpr_bc and (spot - self._cpr_bc) <= self._approach_pts:
            if ms.vwap > 0 and spot < ms.vwap:  # VWAP above spot = bearish
                self._signal_dir = "PE"
                return True

        self._signal_dir = None
        return False

    def _check_entry_trigger(self, ms: MarketState, option_chain=None) -> Optional[StrategySignal]:
        if self._signal_dir is None or ms.last_bar is None:
            return None

        bar = ms.last_bar
        d   = self._signal_dir

        if d == "CE":
            if bar.close <= self._cpr_tc:
                return None
            if ms.rsi < self._rsi_ce_min:
                return None
        else:
            if bar.close >= self._cpr_bc:
                return None
            if ms.rsi > self._rsi_pe_max:
                return None

        avg_vol = self._avg_volume(ms.recent_bars)
        if avg_vol > 0 and bar.volume < self._vol_mult * avg_vol:
            return None

        atr  = ms.atr or 30.0
        risk = atr * self._atr_stop_mult
        if d == "CE":
            stop_price   = self._cpr_tc - risk   # back inside CPR = invalid
            target_price = ms.spot_price + risk * self._rr_target
        else:
            stop_price   = self._cpr_bc + risk
            target_price = ms.spot_price - risk * self._rr_target

        return StrategySignal(
            strategy_name    = self.name,
            category         = self.category,
            direction        = d,
            signal_type      = "CPR_BREAKOUT",
            confidence       = 0.72,
            underlying_price = ms.spot_price,
            stop_price       = stop_price,
            target_price     = target_price,
            meta             = {
                "cpr_tc": self._cpr_tc, "cpr_bc": self._cpr_bc,
                "rsi": ms.rsi, "vwap": ms.vwap,
            },
        )

    def _check_exit(self, ms: MarketState, option_price: float,
                    pos: PositionState) -> Optional[ExitDecision]:
        return self._standard_exit_check(ms, option_price, pos, _TIME_STOP)
