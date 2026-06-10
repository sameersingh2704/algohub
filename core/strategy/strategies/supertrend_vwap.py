"""
SupertrendVWAP — Category D (Trend-Following)

Enters on Supertrend(ATR-10, mult 2.5) flip confirmed by VWAP side + RSI momentum.
Supertrend adapts dynamically to ATR, making it more responsive to NIFTY volatility
than fixed EMA stacks. VWAP filters out counter-trend moves during the session.

Signal source: NIFTY Futures (ms.last_bar OHLC)
Execute via:   CE on bullish flip / PE on bearish flip
"""

from __future__ import annotations

import logging
from datetime import time as dtime
from typing import Optional

from core.data.market_data_engine import MarketState
from core.strategy.base_strategy import (
    BaseStrategy, ExitDecision, PositionState, StrategyCategory, StrategySignal,
)

logger = logging.getLogger(__name__)

SIGNAL_TYPE = "SUPERTREND_VWAP"
_ST_PERIOD = 10
_ST_MULT = 2.5
_ENTRY_START = dtime(9, 35)
_ENTRY_END = dtime(14, 30)
_TIME_STOP = dtime(15, 15)


class SupertrendVWAP(BaseStrategy):
    """
    Setup:   Supertrend ATR(10)×2.5 warmed up AND flipped within last 3 bars
    Armed:   Price on correct side of VWAP (CE above, PE below)
    Trigger: RSI in momentum range + volume ≥ 1.2× + within entry window
    Exit:    Hard stop –30%, target +45%, trailing 12% after +15%,
             Supertrend flip-back (thesis invalidated), time stop 15:15
    """

    def __init__(self, config: dict, risk_config: dict) -> None:
        super().__init__("SupertrendVWAP", StrategyCategory.TREND, config, risk_config)
        cfg = config.get("supertrend_vwap", {})
        self._rsi_min_ce: float = cfg.get("rsi_min_ce", 45.0)
        self._rsi_max_ce: float = cfg.get("rsi_max_ce", 68.0)
        self._rsi_min_pe: float = cfg.get("rsi_min_pe", 32.0)
        self._rsi_max_pe: float = cfg.get("rsi_max_pe", 55.0)
        self._vol_min: float = cfg.get("volume_min_ratio", 1.2)
        self._flip_lookback: int = cfg.get("flip_lookback_bars", 3)
        exits = config.get("exits", {})
        self._hard_stop_pct: float = exits.get("hard_stop_pct", -0.30)
        self._profit_target_pct: float = exits.get("profit_target_pct", 0.45)
        self._trailing_activation_pct: float = exits.get("trailing_activation_pct", 0.15)
        self._trailing_stop_pct: float = exits.get("trailing_stop_pct", 0.12)

        # Internal Supertrend state — updated every bar via on_market_update override
        self._st_atr_avg: Optional[float] = None
        self._st_atr_warmup: int = 0
        self._st_atr_prev_close: Optional[float] = None
        self._st_final_upper: float = 0.0
        self._st_final_lower: float = 0.0
        self._st_trend: int = 0          # +1 bullish, -1 bearish, 0 uninit
        self._st_bars_since_flip: int = 0

        self._direction: Optional[str] = None  # "CE" | "PE" once ARMED

    # ── Overrides ────────────────────────────────────────────────────────────

    def on_market_update(self, market_state: MarketState,
                         option_chain=None) -> Optional[StrategySignal]:
        # Keep Supertrend current regardless of lifecycle state
        self._update_supertrend(market_state)
        return super().on_market_update(market_state, option_chain)

    def reset_daily(self) -> None:
        super().reset_daily()
        self._st_atr_avg = None
        self._st_atr_warmup = 0
        self._st_atr_prev_close = None
        self._st_final_upper = 0.0
        self._st_final_lower = 0.0
        self._st_trend = 0
        self._st_bars_since_flip = 0
        self._direction = None

    # ── Supertrend computation ────────────────────────────────────────────────

    def _update_supertrend(self, ms: MarketState) -> None:
        if ms.last_bar is None:
            return
        bar = ms.last_bar
        prev_close = self._st_atr_prev_close

        if prev_close is not None:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - prev_close),
                abs(bar.low  - prev_close),
            )
            self._st_atr_warmup += 1
            if self._st_atr_warmup <= _ST_PERIOD:
                self._st_atr_avg = (self._st_atr_avg or 0.0) + tr
                if self._st_atr_warmup == _ST_PERIOD:
                    self._st_atr_avg /= _ST_PERIOD
            else:
                alpha = 1.0 / _ST_PERIOD
                self._st_atr_avg = self._st_atr_avg * (1 - alpha) + tr * alpha

        self._st_atr_prev_close = bar.close

        if self._st_atr_warmup < _ST_PERIOD:
            return

        hl2 = (bar.high + bar.low) / 2
        raw_upper = hl2 + _ST_MULT * self._st_atr_avg
        raw_lower = hl2 - _ST_MULT * self._st_atr_avg

        if self._st_final_upper == 0.0:
            self._st_final_upper = raw_upper
            self._st_final_lower = raw_lower
        else:
            self._st_final_upper = (
                min(raw_upper, self._st_final_upper)
                if bar.close <= self._st_final_upper
                else raw_upper
            )
            self._st_final_lower = (
                max(raw_lower, self._st_final_lower)
                if bar.close >= self._st_final_lower
                else raw_lower
            )

        prev_trend = self._st_trend
        if self._st_trend <= 0:
            self._st_trend = 1 if bar.close > self._st_final_upper else -1
        else:
            self._st_trend = -1 if bar.close < self._st_final_lower else 1

        if self._st_trend != prev_trend and prev_trend != 0:
            self._st_bars_since_flip = 0
        else:
            self._st_bars_since_flip += 1

    # ── Strategy lifecycle hooks ──────────────────────────────────────────────

    def _check_setup(self, ms: MarketState, option_chain=None) -> bool:
        if ms.india_vix > 25.0:
            return False
        return (
            self._st_atr_warmup >= _ST_PERIOD
            and self._st_trend != 0
            and self._st_bars_since_flip <= self._flip_lookback
        )

    def _check_armed(self, ms: MarketState, option_chain=None) -> bool:
        if self._st_trend == 1 and ms.spot_price > ms.vwap:
            self._direction = "CE"
            return True
        if self._st_trend == -1 and ms.spot_price < ms.vwap:
            self._direction = "PE"
            return True
        self._direction = None
        return False

    def _check_entry_trigger(self, ms: MarketState,
                              option_chain=None) -> Optional[StrategySignal]:
        if self._direction is None:
            return None

        t = ms.timestamp.time()
        if not (_ENTRY_START <= t <= _ENTRY_END):
            return None

        if ms.volume_ratio < self._vol_min:
            return None

        if self._direction == "CE":
            if not (self._rsi_min_ce <= ms.rsi <= self._rsi_max_ce):
                return None
        else:
            if not (self._rsi_min_pe <= ms.rsi <= self._rsi_max_pe):
                return None

        atr = ms.atr if ms.atr > 0 else max(20.0, ms.spot_price * 0.005)
        stop_dist = atr * 1.5
        if self._direction == "CE":
            stop_price   = ms.spot_price - stop_dist
            target_price = ms.spot_price + stop_dist * 2.0
        else:
            stop_price   = ms.spot_price + stop_dist
            target_price = ms.spot_price - stop_dist * 2.0

        return StrategySignal(
            strategy_name=self.name,
            category=self.category,
            direction=self._direction,
            signal_type=SIGNAL_TYPE,
            confidence=0.72,
            underlying_price=ms.spot_price,
            stop_price=stop_price,
            target_price=target_price,
            meta={
                "st_trend": self._st_trend,
                "st_bars_since_flip": self._st_bars_since_flip,
                "vwap": ms.vwap,
                "rsi": ms.rsi,
                "vol_ratio": ms.volume_ratio,
            },
        )

    def _check_exit(self, ms: MarketState, option_price: float,
                    pos: PositionState) -> Optional[ExitDecision]:
        if option_price <= 0:
            return None

        pnl_pct = (option_price - pos.entry_price) / pos.entry_price

        if option_price > pos.peak_price:
            pos.peak_price = option_price

        # Hard stop
        if pnl_pct <= self._hard_stop_pct:
            return ExitDecision(action="EXIT", reason="HARD_STOP")

        # Supertrend flipped against — thesis invalidated
        if pos.direction == "CE" and self._st_trend == -1:
            return ExitDecision(action="EXIT", reason="SUPERTREND_FLIP")
        if pos.direction == "PE" and self._st_trend == 1:
            return ExitDecision(action="EXIT", reason="SUPERTREND_FLIP")

        # Profit target
        if pnl_pct >= self._profit_target_pct:
            return ExitDecision(action="EXIT", reason="PROFIT_TARGET")

        # Trailing stop (activates after reaching activation threshold)
        if pnl_pct >= self._trailing_activation_pct:
            trail_stop = pos.peak_price * (1 - self._trailing_stop_pct)
            if option_price <= trail_stop:
                return ExitDecision(action="EXIT", reason="TRAILING_STOP")

        # Time stop
        if ms.timestamp.time() >= _TIME_STOP:
            return ExitDecision(action="EXIT", reason="TIME_STOP")

        return None
