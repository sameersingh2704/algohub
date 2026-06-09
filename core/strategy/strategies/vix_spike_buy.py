"""
VIXSpikeBuy — Category F (Options)

A sudden spike in India VIX signals elevated fear and sharp directional moves.
When VIX jumps ≥ 15 % from the previous day's close AND the underlying drops
significantly, the market is in panic — buying PE options on the momentum
leg gives asymmetric upside because option premium expands WITH the move.

Session context required:
  prev_vix — India VIX closing level from the previous session

SETUP:  india_vix > prev_vix * 1.15 AND india_vix > 15,
        NIFTY ≥ 40 pts below session high, time 9:30–13:00
ARMED:  NIFTY forms a small pullback (2–3 bars settling) after initial spike
TRIGGER: new session low formed + RSI < 45 + volume spike → buy PE
EXIT:   standard; time-stop 14:00
"""

from __future__ import annotations

import logging
from typing import Optional

from core.data.market_data_engine import MarketState
from core.strategy.base_strategy import (
    BaseStrategy, StrategyCategory, StrategySignal, ExitDecision, PositionState,
)

logger = logging.getLogger(__name__)

_WINDOW_START       = (9, 30)
_WINDOW_END         = (13,  0)
_TIME_STOP          = (14,  0)
_VIX_SPIKE_RATIO    = 1.15    # 15 % above prev_vix
_VIX_ABS_MIN        = 15.0   # VIX must be at least 15
_DROP_FROM_HIGH_PTS = 40.0   # NIFTY must be ≥ 40 pts below session high
_RSI_MAX            = 45.0   # RSI must show bearish momentum
_VOL_MULT           = 1.3


class VIXSpikeBuy(BaseStrategy):

    def __init__(self, config: dict, risk_config: dict) -> None:
        super().__init__("VIXSpikeBuy", StrategyCategory.OPTIONS, config, risk_config)
        cfg = config.get("vix_spike_buy", {})
        self._vix_spike_ratio: float    = cfg.get("vix_spike_ratio", _VIX_SPIKE_RATIO)
        self._vix_abs_min: float        = cfg.get("vix_abs_min", _VIX_ABS_MIN)
        self._drop_from_high: float     = cfg.get("drop_from_high_pts", _DROP_FROM_HIGH_PTS)
        self._rsi_max: float            = cfg.get("rsi_max_entry", _RSI_MAX)
        self._vol_mult: float           = cfg.get("vol_mult", _VOL_MULT)
        self._atr_stop_mult: float      = cfg.get("atr_stop_mult", 0.6)
        self._rr_target: float          = cfg.get("rr_target", 2.0)
        self._settle_bars_min: int      = cfg.get("settle_bars_min", 2)

        self._session_high: float  = 0.0
        self._settle_count: int    = 0

    @staticmethod
    def _in_window(ts) -> bool:
        t = (ts.hour, ts.minute)
        return _WINDOW_START <= t < _WINDOW_END

    @staticmethod
    def _avg_volume(bars) -> float:
        vols = [b.volume for b in bars if b.volume > 0]
        return sum(vols) / len(vols) if vols else 0.0

    def _vix_spiked(self, ms: MarketState) -> bool:
        prev_vix = self._session_ctx.get("prev_vix", 0.0)
        if prev_vix <= 0:
            return ms.india_vix > self._vix_abs_min + 2.0   # fallback: VIX > 17
        return (ms.india_vix >= prev_vix * self._vix_spike_ratio
                and ms.india_vix >= self._vix_abs_min)

    def _check_setup(self, ms: MarketState, option_chain=None) -> bool:
        if not self._in_window(ms.timestamp):
            return False
        if not self._vix_spiked(ms):
            return False

        # Track session high to measure drawdown
        if ms.spot_price > self._session_high:
            self._session_high = ms.spot_price

        drop = self._session_high - ms.spot_price
        return drop >= self._drop_from_high

    def _check_armed(self, ms: MarketState, option_chain=None) -> bool:
        if not self._vix_spiked(ms):
            return False

        # Allow 2+ bars of price settling (consolidation after initial panic candle)
        if ms.last_bar and ms.last_bar.body_size < (ms.atr or 30.0) * 0.5:
            self._settle_count += 1
        else:
            self._settle_count = 0

        return self._settle_count >= self._settle_bars_min

    def _check_entry_trigger(self, ms: MarketState, option_chain=None) -> Optional[StrategySignal]:
        if ms.last_bar is None:
            return None

        bar = ms.last_bar

        # VIX must still be elevated
        if not self._vix_spiked(ms):
            return None

        # RSI confirms bearish momentum
        if ms.rsi >= self._rsi_max:
            return None

        # New session low forming
        session_low = ms.orb_low if ms.orb_low > 0 else ms.spot_price
        if not bar.is_bearish and bar.close >= session_low:
            return None

        avg_vol = self._avg_volume(ms.recent_bars)
        if avg_vol > 0 and bar.volume < self._vol_mult * avg_vol:
            return None

        atr  = ms.atr or 30.0
        risk = atr * self._atr_stop_mult

        prev_vix = self._session_ctx.get("prev_vix", 0.0)
        return StrategySignal(
            strategy_name    = self.name,
            category         = self.category,
            direction        = "PE",
            signal_type      = "VIX_SPIKE_PANIC",
            confidence       = 0.70,
            underlying_price = ms.spot_price,
            stop_price       = ms.spot_price + risk,          # thesis fails on recovery
            target_price     = ms.spot_price - risk * self._rr_target,
            meta             = {
                "india_vix": ms.india_vix,
                "prev_vix": prev_vix,
                "session_high": self._session_high,
                "drop_pts": self._session_high - ms.spot_price,
                "rsi": ms.rsi,
            },
        )

    def _check_exit(self, ms: MarketState, option_price: float,
                    pos: PositionState) -> Optional[ExitDecision]:
        return self._standard_exit_check(ms, option_price, pos, _TIME_STOP)
