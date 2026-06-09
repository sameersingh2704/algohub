"""
GapFillFade — Category E (Mean Reversion)

On days where NIFTY opens with a meaningful gap (50–200 pts), the first impulse
often fades partially or fully back toward the previous close. This strategy buys
options in the fill direction during the first 45 minutes of the session.

Session context required (injected via set_session_context before market open):
  gap_pts    — today_open minus prev_close (positive = gap-up)
  prev_close — previous session's closing price
  session_open — the 9:15 candle open price

SETUP:  |gap_pts| 50–200, time 9:15–10:00
ARMED:  after 2 bars, RSI shows early momentum-loss in gap direction
TRIGGER: directional reversal bar with spot starting to fill the gap
EXIT:   standard; time-stop 11:30 (gap fills fast or not at all)
"""

from __future__ import annotations

import logging
from typing import Optional

from core.data.market_data_engine import MarketState
from core.strategy.base_strategy import (
    BaseStrategy, StrategyCategory, StrategySignal, ExitDecision, PositionState,
)

logger = logging.getLogger(__name__)

_WINDOW_START = (9, 15)
_WINDOW_END   = (10,  0)
_TIME_STOP    = (11, 30)

_GAP_MIN = 50.0
_GAP_MAX = 200.0


class GapFillFade(BaseStrategy):

    def __init__(self, config: dict, risk_config: dict) -> None:
        super().__init__("GapFillFade", StrategyCategory.MEAN_REVERSION, config, risk_config)
        cfg = config.get("gap_fill_fade", {})
        self._vix_max: float           = cfg.get("vix_max", 22.0)
        self._min_bars_before_arm: int = cfg.get("min_bars_before_arm", 2)
        self._rsi_exhaustion_ce: float = cfg.get("rsi_exhaustion_ce", 60.0)  # gap-down, too oversold
        self._rsi_exhaustion_pe: float = cfg.get("rsi_exhaustion_pe", 40.0)  # gap-up, too overbought

        # set from session context
        self._gap_pts: float    = 0.0
        self._direction: Optional[str] = None  # "CE" (gap-down fill) or "PE" (gap-up fill)

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

        gap = self._session_ctx.get("gap_pts", 0.0)
        self._gap_pts = gap
        if not (_GAP_MIN <= abs(gap) <= _GAP_MAX):
            return False

        self._direction = "PE" if gap > 0 else "CE"
        return True

    def _check_armed(self, ms: MarketState, option_chain=None) -> bool:
        # Need at least a couple bars for momentum to show exhaustion
        if ms.bars_count < self._min_bars_before_arm:
            return False
        if self._direction is None:
            return False

        # Gap-up fade (buy PE): RSI should be high but not still surging
        if self._direction == "PE" and ms.rsi >= self._rsi_exhaustion_pe:
            return True
        # Gap-down fade (buy CE): RSI should be low but not still plummeting
        if self._direction == "CE" and ms.rsi <= self._rsi_exhaustion_ce:
            return True

        return False

    def _check_entry_trigger(self, ms: MarketState, option_chain=None) -> Optional[StrategySignal]:
        if self._direction is None or ms.last_bar is None:
            return None

        bar = ms.last_bar
        d   = self._direction
        session_open = self._session_ctx.get("session_open", ms.spot_price)
        prev_close   = self._session_ctx.get("prev_close", 0.0)

        if d == "PE":
            # Gap-up fade: need bearish reversal bar, spot below session open
            if not bar.is_bearish:
                return None
            if ms.spot_price >= session_open:
                return None
            if ms.rsi < 40.0 or ms.rsi > 72.0:
                return None
        else:
            # Gap-down fade: need bullish reversal bar, spot above session open
            if not bar.is_bullish:
                return None
            if ms.spot_price <= session_open:
                return None
            if ms.rsi > 60.0 or ms.rsi < 28.0:
                return None

        avg_vol = self._avg_volume(ms.recent_bars)
        if avg_vol > 0 and bar.volume < 0.9 * avg_vol:
            return None

        atr = ms.atr or 30.0
        if d == "PE":
            stop_price   = session_open + atr * 0.5        # thesis fails if reclaims open
            target_price = prev_close if prev_close > 0 else ms.spot_price - abs(self._gap_pts) * 0.6
        else:
            stop_price   = session_open - atr * 0.5
            target_price = prev_close if prev_close > 0 else ms.spot_price + abs(self._gap_pts) * 0.6

        return StrategySignal(
            strategy_name    = self.name,
            category         = self.category,
            direction        = d,
            signal_type      = "GAP_FILL_FADE",
            confidence       = 0.65,
            underlying_price = ms.spot_price,
            stop_price       = stop_price,
            target_price     = target_price,
            meta             = {"gap_pts": self._gap_pts, "session_open": session_open, "rsi": ms.rsi},
        )

    def _check_exit(self, ms: MarketState, option_price: float,
                    pos: PositionState) -> Optional[ExitDecision]:
        return self._standard_exit_check(ms, option_price, pos, _TIME_STOP)
