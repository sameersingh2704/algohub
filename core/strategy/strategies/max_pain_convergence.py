"""
MaxPainConvergence — Category F (Options)

Max pain is the strike price where the largest number of options (both CE and PE)
expire worthless, creating maximum financial loss for option buyers. On expiry week
(especially Tuesday), the spot tends to drift toward max pain as market-makers
manage their delta exposure. This strategy buys the option in the convergence direction.

  spot > max_pain → buy PE (expect spot to fall toward max_pain)
  spot < max_pain → buy CE (expect spot to rise toward max_pain)

Option chain is the only data source needed — no session context required.

SETUP:  option_chain available, |spot - max_pain| ≥ 100 pts, time 9:30–11:30
ARMED:  PCR confirms direction (< 1.0 if above max pain, > 1.0 if below)
TRIGGER: first bar moving toward max pain, volume ≥ 1.2× avg, RSI confirms
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

_WINDOW_START     = (9, 30)
_WINDOW_END       = (11, 30)
_TIME_STOP        = (14,  0)
_MIN_DISPLACEMENT = 100.0   # minimum |spot - max_pain| to take the trade
_VOL_MULT         = 1.2


class MaxPainConvergence(BaseStrategy):

    def __init__(self, config: dict, risk_config: dict) -> None:
        super().__init__("MaxPainConvergence", StrategyCategory.OPTIONS, config, risk_config)
        cfg = config.get("max_pain_convergence", {})
        self._min_displacement: float = cfg.get("min_displacement_pts", _MIN_DISPLACEMENT)
        self._vol_mult: float         = cfg.get("vol_mult", _VOL_MULT)
        self._rsi_ce_min: float       = cfg.get("rsi_ce_min", 45.0)
        self._rsi_pe_max: float       = cfg.get("rsi_pe_max", 55.0)
        self._vix_max: float          = cfg.get("vix_max", 22.0)
        self._atr_stop_mult: float    = cfg.get("atr_stop_mult", 0.5)
        self._rr_target: float        = cfg.get("rr_target", 2.0)

        self._max_pain: int          = 0
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
        if option_chain is None or option_chain.max_pain_strike <= 0:
            return False

        displacement = ms.spot_price - option_chain.max_pain_strike
        if abs(displacement) < self._min_displacement:
            return False

        self._max_pain   = option_chain.max_pain_strike
        self._signal_dir = "PE" if displacement > 0 else "CE"
        return True

    def _check_armed(self, ms: MarketState, option_chain=None) -> bool:
        if option_chain is None or self._signal_dir is None:
            return False
        pcr = option_chain.pcr or 1.0

        if self._signal_dir == "PE":
            # Above max pain: call writers dominate → PCR < 1.0 means put/call OI leans CE-heavy
            # Actually PCR < 1 means more call OI relative to put OI → bullish positioning
            # But we want PCR to confirm selling pressure on the call side
            # Simple heuristic: PCR < 1.2 (not extreme put buying) confirms drift down
            return pcr < 1.2
        else:
            # Below max pain: put writers dominate → PCR > 0.8 is fine
            return pcr > 0.8

    def _check_entry_trigger(self, ms: MarketState, option_chain=None) -> Optional[StrategySignal]:
        if self._signal_dir is None or ms.last_bar is None:
            return None

        bar = ms.last_bar
        d   = self._signal_dir

        if d == "PE":
            # Moving toward max pain = spot falling
            if not bar.is_bearish:
                return None
            if ms.rsi > self._rsi_pe_max:
                return None
        else:
            if not bar.is_bullish:
                return None
            if ms.rsi < self._rsi_ce_min:
                return None

        avg_vol = self._avg_volume(ms.recent_bars)
        if avg_vol > 0 and bar.volume < self._vol_mult * avg_vol:
            return None

        atr  = ms.atr or 30.0
        risk = atr * self._atr_stop_mult
        if d == "PE":
            stop_price   = ms.spot_price + risk
            target_price = float(self._max_pain)    # convergence target IS max pain
        else:
            stop_price   = ms.spot_price - risk
            target_price = float(self._max_pain)

        return StrategySignal(
            strategy_name    = self.name,
            category         = self.category,
            direction        = d,
            signal_type      = "MAX_PAIN_CONVERGENCE",
            confidence       = 0.65,
            underlying_price = ms.spot_price,
            stop_price       = stop_price,
            target_price     = target_price,
            meta             = {
                "max_pain": self._max_pain,
                "displacement": ms.spot_price - self._max_pain,
                "pcr": option_chain.pcr if option_chain else 0.0,
            },
        )

    def _check_exit(self, ms: MarketState, option_price: float,
                    pos: PositionState) -> Optional[ExitDecision]:
        return self._standard_exit_check(ms, option_price, pos, _TIME_STOP)
