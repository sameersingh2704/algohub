"""
PDHPDLRetest — Category C (Market Structure)

Previous Day High (PDH) and Previous Day Low (PDL) are key structural levels.
After a confirmed breakout above PDH (or below PDL), the first pullback back
to that level often offers a high-probability continuation entry — the level
acts as new support/resistance.

Session context required:
  prev_high — PDH (previous day's high)
  prev_low  — PDL (previous day's low)

SETUP:  PDH/PDL available from session context, time 9:35–14:00
ARMED:  a close above PDH occurred in today's session (for CE) → now retracing
        back to PDH; or a close below PDL (for PE) → now bouncing to PDL
TRIGGER: rejection candle at PDH/PDL with volume ≥ 1.2× avg + RSI not extreme
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
_WINDOW_END    = (14, 0)
_TIME_STOP     = (14, 30)
_RETEST_ZONE   = 25.0     # within this many pts = retest zone
_VOL_MULT      = 1.2
_RSI_CE_MIN    = 42.0
_RSI_PE_MAX    = 58.0


class PDHPDLRetest(BaseStrategy):

    def __init__(self, config: dict, risk_config: dict) -> None:
        super().__init__("PDHPDLRetest", StrategyCategory.MARKET_STRUCTURE, config, risk_config)
        cfg = config.get("pdh_pdl_retest", {})
        self._retest_zone: float   = cfg.get("retest_zone_pts", _RETEST_ZONE)
        self._vol_mult: float      = cfg.get("vol_mult", _VOL_MULT)
        self._rsi_ce_min: float    = cfg.get("rsi_ce_min", _RSI_CE_MIN)
        self._rsi_pe_max: float    = cfg.get("rsi_pe_max", _RSI_PE_MAX)
        self._vix_max: float       = cfg.get("vix_max", 22.0)
        self._atr_stop_mult: float = cfg.get("atr_stop_mult", 0.5)
        self._rr_target: float     = cfg.get("rr_target", 2.5)
        self._wick_ratio: float    = cfg.get("wick_ratio", 1.0)

        # runtime flags
        self._pdh: float           = 0.0
        self._pdl: float           = 0.0
        self._pdh_broken: bool     = False  # a close above PDH seen today
        self._pdl_broken: bool     = False  # a close below PDL seen today
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

        pdh = self._session_ctx.get("prev_high", 0.0)
        pdl = self._session_ctx.get("prev_low", 0.0)
        if pdh <= 0 or pdl <= 0:
            return False

        self._pdh = pdh
        self._pdl = pdl

        # Track whether a breakout has occurred today using last bar closes
        if ms.last_bar:
            close = ms.last_bar.close
            if close > self._pdh:
                self._pdh_broken = True
            if close < self._pdl:
                self._pdl_broken = True

        return self._pdh_broken or self._pdl_broken

    def _check_armed(self, ms: MarketState, option_chain=None) -> bool:
        spot = ms.spot_price

        # PDH breakout → pullback to PDH → buy CE
        if self._pdh_broken and spot > self._pdh - self._retest_zone and spot < self._pdh + self._retest_zone:
            # Still retreating toward PDH (spot hasn't gone back below PDH)
            if spot >= self._pdh - self._retest_zone:
                self._signal_dir = "CE"
                return True

        # PDL breakdown → bounce to PDL → buy PE
        if self._pdl_broken and spot < self._pdl + self._retest_zone and spot > self._pdl - self._retest_zone:
            if spot <= self._pdl + self._retest_zone:
                self._signal_dir = "PE"
                return True

        self._signal_dir = None
        return False

    def _check_entry_trigger(self, ms: MarketState, option_chain=None) -> Optional[StrategySignal]:
        if self._signal_dir is None or ms.last_bar is None:
            return None

        bar = ms.last_bar
        d   = self._signal_dir

        upper_wick = bar.high - max(bar.open, bar.close)
        lower_wick = min(bar.open, bar.close) - bar.low
        body       = max(bar.body_size, 0.5)

        if d == "CE":
            # Bounce from PDH support: bullish close or lower-wick bounce
            if not (bar.is_bullish or lower_wick >= self._wick_ratio * body):
                return None
            if ms.rsi < self._rsi_ce_min:
                return None
            # VWAP should be below PDH (structure intact)
            if ms.vwap > 0 and ms.vwap > self._pdh:
                return None
        else:
            # Rejection at PDL resistance: bearish close or upper-wick rejection
            if not (bar.is_bearish or upper_wick >= self._wick_ratio * body):
                return None
            if ms.rsi > self._rsi_pe_max:
                return None
            if ms.vwap > 0 and ms.vwap < self._pdl:
                return None

        avg_vol = self._avg_volume(ms.recent_bars)
        if avg_vol > 0 and bar.volume < self._vol_mult * avg_vol:
            return None

        atr  = ms.atr or 30.0
        risk = atr * self._atr_stop_mult
        if d == "CE":
            stop_price   = self._pdh - risk          # back below PDH = breakout failed
            target_price = ms.spot_price + risk * self._rr_target
        else:
            stop_price   = self._pdl + risk
            target_price = ms.spot_price - risk * self._rr_target

        return StrategySignal(
            strategy_name    = self.name,
            category         = self.category,
            direction        = d,
            signal_type      = "PDH_PDL_RETEST",
            confidence       = 0.72,
            underlying_price = ms.spot_price,
            stop_price       = stop_price,
            target_price     = target_price,
            meta             = {
                "pdh": self._pdh, "pdl": self._pdl,
                "rsi": ms.rsi, "vwap": ms.vwap,
                "pdh_broken": self._pdh_broken, "pdl_broken": self._pdl_broken,
            },
        )

    def _check_exit(self, ms: MarketState, option_price: float,
                    pos: PositionState) -> Optional[ExitDecision]:
        return self._standard_exit_check(ms, option_price, pos, _TIME_STOP)
