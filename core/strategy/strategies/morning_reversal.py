"""
MorningExhaustionReversal — Category E (Mean Reversion)

After a sharp directional morning move (≥ 80 pts from session open), RSI
reaches overbought/oversold territory and volume starts fading. The first
reversal candle signals exhaustion and a mean-reversion opportunity.

Session context required:
  session_open — the 9:15 AM open price (to measure morning move magnitude)

SETUP:  |spot - session_open| ≥ 80 pts, time 10:30–12:00, VIX < 22
ARMED:  RSI > 65 (for PE) or RSI < 35 (for CE), VWAP distance ≥ 40 pts,
        volume declining on last 2 bars
TRIGGER: reversal bar (upper wick > body for PE, lower wick > body for CE)
EXIT:   standard; time-stop 13:00
"""

from __future__ import annotations

import logging
from typing import Optional

from core.data.market_data_engine import MarketState
from core.strategy.base_strategy import (
    BaseStrategy, StrategyCategory, StrategySignal, ExitDecision, PositionState,
)

logger = logging.getLogger(__name__)

_WINDOW_START    = (10, 30)
_WINDOW_END      = (12,  0)
_TIME_STOP       = (13,  0)
_MIN_MOVE_PTS    = 80.0
_MIN_VWAP_DIST   = 40.0
_RSI_OB          = 65.0   # overbought → fade rally → buy PE
_RSI_OS          = 35.0   # oversold   → fade decline → buy CE


class MorningExhaustionReversal(BaseStrategy):

    def __init__(self, config: dict, risk_config: dict) -> None:
        super().__init__("MorningExhaustionReversal", StrategyCategory.MEAN_REVERSION, config, risk_config)
        cfg = config.get("morning_reversal", {})
        self._min_move_pts: float    = cfg.get("min_move_pts", _MIN_MOVE_PTS)
        self._min_vwap_dist: float   = cfg.get("min_vwap_dist", _MIN_VWAP_DIST)
        self._rsi_ob: float          = cfg.get("rsi_overbought", _RSI_OB)
        self._rsi_os: float          = cfg.get("rsi_oversold", _RSI_OS)
        self._vix_max: float         = cfg.get("vix_max", 22.0)
        self._atr_stop_mult: float   = cfg.get("atr_stop_mult", 0.6)
        self._rr_target: float       = cfg.get("rr_target", 2.0)
        self._wick_ratio: float      = cfg.get("wick_ratio", 1.0)  # wick ≥ ratio × body

        self._signal_dir: Optional[str] = None

    @staticmethod
    def _in_window(ts) -> bool:
        t = (ts.hour, ts.minute)
        return _WINDOW_START <= t < _WINDOW_END

    @staticmethod
    def _volume_declining(bars) -> bool:
        vols = [b.volume for b in bars[-3:] if b.volume > 0]
        return len(vols) >= 2 and vols[-1] < vols[-2]

    def _check_setup(self, ms: MarketState, option_chain=None) -> bool:
        if not self._in_window(ms.timestamp):
            return False
        if ms.india_vix > self._vix_max:
            return False

        session_open = self._session_ctx.get("session_open", 0.0)
        if session_open <= 0:
            return False

        move = ms.spot_price - session_open
        if abs(move) < self._min_move_pts:
            return False

        # Directional: rally → look for PE, sell-off → look for CE
        self._signal_dir = "PE" if move > 0 else "CE"
        return True

    def _check_armed(self, ms: MarketState, option_chain=None) -> bool:
        if self._signal_dir is None:
            return False

        if self._signal_dir == "PE":
            if ms.rsi < self._rsi_ob:
                return False
        else:
            if ms.rsi > self._rsi_os:
                return False

        if ms.vwap > 0 and abs(ms.spot_price - ms.vwap) < self._min_vwap_dist:
            return False

        if not self._volume_declining(ms.recent_bars):
            return False

        return True

    def _check_entry_trigger(self, ms: MarketState, option_chain=None) -> Optional[StrategySignal]:
        if self._signal_dir is None or ms.last_bar is None:
            return None

        bar = ms.last_bar
        d   = self._signal_dir

        upper_wick = bar.high - max(bar.open, bar.close)
        lower_wick = min(bar.open, bar.close) - bar.low
        body       = max(bar.body_size, 0.5)

        if d == "PE":
            if not (upper_wick >= self._wick_ratio * body or bar.is_bearish):
                return None
        else:
            if not (lower_wick >= self._wick_ratio * body or bar.is_bullish):
                return None

        atr  = ms.atr or 30.0
        risk = atr * self._atr_stop_mult
        if d == "PE":
            stop_price   = ms.spot_price + risk    # rally continues = thesis wrong
            target_price = ms.spot_price - risk * self._rr_target
        else:
            stop_price   = ms.spot_price - risk
            target_price = ms.spot_price + risk * self._rr_target

        session_open = self._session_ctx.get("session_open", ms.spot_price)
        return StrategySignal(
            strategy_name    = self.name,
            category         = self.category,
            direction        = d,
            signal_type      = "MORNING_EXHAUSTION",
            confidence       = 0.68,
            underlying_price = ms.spot_price,
            stop_price       = stop_price,
            target_price     = target_price,
            meta             = {
                "session_open": session_open,
                "morning_move": ms.spot_price - session_open,
                "rsi": ms.rsi,
                "vwap_dist": abs(ms.spot_price - ms.vwap),
            },
        )

    def _check_exit(self, ms: MarketState, option_price: float,
                    pos: PositionState) -> Optional[ExitDecision]:
        return self._standard_exit_check(ms, option_price, pos, _TIME_STOP)
