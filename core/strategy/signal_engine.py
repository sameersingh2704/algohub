"""
Signal Engine
==============
Pre-session regime classification and intraday signal generation.

Implements AVCS strategy entry logic:
1. Regime classifier (9:10 AM, pre-session)
2. ORB breakout detector
3. VWAP bounce detector (new scalp signal)
4. EMA crossover scalp detector (new scalp signal)
5. VWAP alignment checker
6. RSI momentum filter (new)
7. Volume confirmation
8. EMA stack validation
9. Signal composition

Signal types:
  ORB_BREAKOUT   — classic ORB breakout after 9:35
  VWAP_BOUNCE    — pullback to VWAP then momentum continuation
  EMA_SCALP      — fast EMA just crossed slow EMA with VWAP alignment
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from enum import Enum
from typing import Dict, List, Optional
import uuid

from core.data.market_data_engine import MarketDataEngine, MarketState, OptionQuote
from core.data.greeks_calculator import GreeksCalculator

logger = logging.getLogger(__name__)


class Regime(str, Enum):
    TREND_DAY_BIAS = "TREND_DAY_BIAS"
    NEUTRAL = "NEUTRAL"
    REDUCED = "REDUCED"
    NO_TRADE = "NO_TRADE"
    MOMENTUM_ONLY = "MOMENTUM_ONLY"
    GAMMA_MODE = "GAMMA_MODE"


class SignalDirection(str, Enum):
    BULLISH = "CE"
    BEARISH = "PE"


class SignalType(str, Enum):
    ORB_BREAKOUT = "ORB_BREAKOUT"
    VWAP_BOUNCE = "VWAP_BOUNCE"
    EMA_SCALP = "EMA_SCALP"
    GAMMA_EXPIRY = "GAMMA_EXPIRY"


@dataclass
class EntryConditionResult:
    name: str
    passed: bool
    value: float | str | bool
    threshold: float | str | bool
    message: str


@dataclass
class Signal:
    signal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    signal_time: datetime = field(default_factory=datetime.now)
    session_date: date = field(default_factory=date.today)
    direction: SignalDirection = SignalDirection.BULLISH
    signal_type: SignalType = SignalType.ORB_BREAKOUT
    regime: Regime = Regime.NEUTRAL

    spot_price: float = 0.0
    vwap: float = 0.0
    vwap_slope: float = 0.0
    orb_high: float = 0.0
    orb_low: float = 0.0
    india_vix: float = 0.0
    ema_5: float = 0.0
    ema_13: float = 0.0
    ema_21: float = 0.0
    volume_ratio: float = 0.0
    rsi: float = 50.0

    proposed_strike: int = 0
    proposed_symbol: str = ""
    proposed_token: str = ""
    option_premium: float = 0.0
    option_delta: float = 0.0
    option_spread: float = 0.0
    option_oi: int = 0

    conditions: List[EntryConditionResult] = field(default_factory=list)
    is_valid: bool = False
    rejection_reason: str = ""


@dataclass
class RegimeResult:
    regime: Regime
    reasons: List[str] = field(default_factory=list)
    india_vix: float = 0.0
    cpr_width: float = 0.0
    gap_size_pct: float = 0.0
    gap_direction: str = ""
    momentum_direction: Optional[str] = None


class SignalEngine:
    """
    Generates entry signals for the AVCS strategy.

    Three signal types are evaluated each bar:
    1. ORB_BREAKOUT  — spot crosses ORB with VWAP+EMA+volume alignment
    2. VWAP_BOUNCE   — spot pulls back to VWAP and resumes trend
    3. EMA_SCALP     — EMA(5) freshly crosses EMA(13) with trend context

    Entry windows (IST):
      Morning primary   : 09:35 – 10:45  (post ORB; widen for scalping)
      Mid-morning       : 11:00 – 11:30  (continuation after open)
      Afternoon primary : 13:00 – 14:45  (afternoon momentum)
    """

    # Entry windows — three windows for scalp frequency
    MORNING_WINDOW  = (dtime(9, 35), dtime(10, 45))
    MIDMORNING_WINDOW = (dtime(11, 0), dtime(11, 30))
    AFTERNOON_WINDOW = (dtime(13, 0), dtime(14, 45))

    # VWAP bounce detection: spot must have been within this pct of VWAP recently
    VWAP_BOUNCE_PROXIMITY_PCT = 0.0015   # 0.15% of VWAP = "near VWAP"
    # EMA scalp: EMA5 must have crossed EMA13 within this many bars
    EMA_CROSS_LOOKBACK = 3

    def __init__(
        self,
        market_data: MarketDataEngine,
        greeks_calc: GreeksCalculator,
        strategy_config: dict,
        risk_config: dict,
    ) -> None:
        self._md = market_data
        self._greeks = greeks_calc
        self._strategy_cfg = strategy_config
        self._risk_cfg = risk_config

        self._regime: Optional[RegimeResult] = None
        self._daily_trade_count: int = 0
        self._consecutive_losses: int = 0
        self._daily_pnl: float = 0.0
        self._session_halted: bool = False

        # EMA(5) cross tracking for EMA_SCALP signal
        # Stores True/False per bar: was EMA5 > EMA13?
        self._ema5_above_13_history: List[bool] = []

    # ──────────────────────────────────────────────────────────
    # REGIME CLASSIFICATION
    # ──────────────────────────────────────────────────────────

    def classify_regime(
        self,
        india_vix: float,
        prev_high: float,
        prev_low: float,
        prev_close: float,
        today_open: float,
        today_is_tuesday: bool = False,
    ) -> RegimeResult:
        """Pre-session regime classification at 9:10 AM."""
        reasons: List[str] = []

        pivot = (prev_high + prev_low + prev_close) / 3
        bc = (prev_high + prev_low) / 2
        tc = (pivot - bc) + pivot
        cpr_width = abs(tc - bc)

        gap_pct = ((today_open - prev_close) / prev_close) * 100 if prev_close else 0.0
        gap_direction = "UP" if gap_pct > 0.1 else ("DOWN" if gap_pct < -0.1 else "FLAT")

        result = RegimeResult(
            regime=Regime.NEUTRAL,
            india_vix=india_vix,
            cpr_width=cpr_width,
            gap_size_pct=abs(gap_pct),
            gap_direction=gap_direction,
        )

        if india_vix > self._strategy_cfg["regime"]["vix_max_reduced"]:
            result.regime = Regime.NO_TRADE
            reasons.append(f"VIX={india_vix:.1f} exceeds max={self._strategy_cfg['regime']['vix_max_reduced']}")
            result.reasons = reasons
            self._regime = result
            logger.info(f"Regime: NO_TRADE — {reasons}")
            return result

        shock_threshold = self._strategy_cfg["regime"]["gap_shock_threshold"]
        if abs(gap_pct) > shock_threshold:
            result.regime = Regime.NO_TRADE
            reasons.append(f"Shock gap: {gap_pct:.2f}% exceeds {shock_threshold}%")
            result.reasons = reasons
            self._regime = result
            logger.info(f"Regime: NO_TRADE — {reasons}")
            return result

        if india_vix > self._strategy_cfg["regime"]["vix_max_normal"]:
            result.regime = Regime.REDUCED
            reasons.append(f"Elevated VIX={india_vix:.1f} — reduced mode")
            result.reasons = reasons
            self._regime = result
            logger.info(f"Regime: REDUCED — {reasons}")
            return result

        if cpr_width > self._strategy_cfg["regime"]["cpr_wide_threshold"]:
            result.regime = Regime.NO_TRADE
            reasons.append(f"Wide CPR={cpr_width:.1f}pts — range day likely")
            result.reasons = reasons
            self._regime = result
            logger.info(f"Regime: NO_TRADE — {reasons}")
            return result

        if cpr_width < self._strategy_cfg["regime"]["cpr_narrow_threshold"]:
            result.regime = Regime.TREND_DAY_BIAS
            reasons.append(f"Narrow CPR={cpr_width:.1f}pts — trend day likely")

        if abs(gap_pct) > 1.5:
            result.regime = Regime.MOMENTUM_ONLY
            result.momentum_direction = gap_direction
            reasons.append(f"Large gap {gap_pct:.2f}% — momentum direction only ({gap_direction})")

        if today_is_tuesday and india_vix < 22:
            result.regime = Regime.GAMMA_MODE
            reasons.append("Tuesday expiry — Gamma mode activated post 10:30 AM")

        if not reasons:
            reasons.append(f"Normal conditions: VIX={india_vix:.1f}, CPR={cpr_width:.1f}pts")

        result.reasons = reasons
        self._regime = result
        logger.info(f"Regime classified: {result.regime.value} — {reasons}")
        return result

    # ──────────────────────────────────────────────────────────
    # ENTRY EVALUATION
    # ──────────────────────────────────────────────────────────

    def evaluate_entry(
        self,
        market_state: MarketState,
        current_time: dtime,
        current_bar_volume: int,
        option_quote: Optional[OptionQuote],
        option_type: str,
        expiry: date,
        existing_position: bool = False,
        daily_pnl: float = 0.0,
        trades_today: int = 0,
        consecutive_losses: int = 0,
    ) -> Optional[Signal]:
        """
        Evaluate entry conditions for ORB_BREAKOUT, VWAP_BOUNCE, and EMA_SCALP signals.
        Returns Signal if ALL required conditions pass; None otherwise.
        """
        # Track EMA5 cross history for EMA_SCALP detection
        if market_state.ema_5 > 0 and market_state.ema_13 > 0:
            self._ema5_above_13_history.append(market_state.ema_5 > market_state.ema_13)
            if len(self._ema5_above_13_history) > 10:
                self._ema5_above_13_history.pop(0)

        # Determine which signal type applies at this bar
        signal_type, direction = self._classify_signal(
            market_state=market_state,
            current_time=current_time,
            option_type=option_type,
        )

        if signal_type is None:
            return None

        signal = Signal()
        signal.spot_price = market_state.spot_price
        signal.vwap = market_state.vwap
        signal.vwap_slope = market_state.vwap_slope
        signal.orb_high = market_state.orb_high
        signal.orb_low = market_state.orb_low
        signal.india_vix = market_state.india_vix
        signal.ema_5 = market_state.ema_5
        signal.ema_13 = market_state.ema_13
        signal.ema_21 = market_state.ema_21
        signal.volume_ratio = market_state.volume_ratio
        signal.rsi = market_state.rsi
        signal.signal_type = signal_type
        signal.direction = direction
        signal.regime = self._regime.regime if self._regime else Regime.NEUTRAL

        conditions: List[EntryConditionResult] = []

        # ─── C1: Regime ────────────────────────────────────────
        regime_ok = signal.regime not in (Regime.NO_TRADE,)
        if signal.regime == Regime.MOMENTUM_ONLY and self._regime:
            md = self._regime.momentum_direction
            if md == "UP" and option_type == "PE":
                regime_ok = False
            elif md == "DOWN" and option_type == "CE":
                regime_ok = False
        conditions.append(EntryConditionResult(
            name="regime_filter", passed=regime_ok,
            value=signal.regime.value, threshold="NOT NO_TRADE",
            message=f"Regime={signal.regime.value}",
        ))

        # ─── C2: Entry time window ──────────────────────────────
        in_window = self._in_any_entry_window(current_time)
        conditions.append(EntryConditionResult(
            name="time_window", passed=in_window,
            value=current_time.strftime("%H:%M"),
            threshold="09:35-10:45 | 11:00-11:30 | 13:00-14:45",
            message=f"Time {current_time.strftime('%H:%M')} in window: {in_window}",
        ))

        # ─── C3: ORB locked (needed for ORB signal; relaxed for bounce/scalp) ───
        orb_ready = market_state.orb_range > 0
        if signal_type == SignalType.ORB_BREAKOUT:
            breakout_ok = self._check_orb_breakout(market_state, option_type)
            conditions.append(EntryConditionResult(
                name="orb_breakout", passed=breakout_ok,
                value=market_state.spot_price,
                threshold=f"ORB {'high' if option_type=='CE' else 'low'} ± buffer",
                message=f"ORB breakout={'YES' if breakout_ok else 'NO'} "
                        f"range={market_state.orb_range:.1f}",
            ))
        elif signal_type == SignalType.VWAP_BOUNCE:
            bounce_ok = self._check_vwap_bounce(market_state, option_type)
            conditions.append(EntryConditionResult(
                name="vwap_bounce", passed=bounce_ok,
                value=market_state.spot_price - market_state.vwap,
                threshold=self.VWAP_BOUNCE_PROXIMITY_PCT,
                message=f"VWAP bounce={'YES' if bounce_ok else 'NO'}",
            ))
        else:  # EMA_SCALP
            cross_ok = self._check_ema_cross(option_type)
            conditions.append(EntryConditionResult(
                name="ema_cross", passed=cross_ok,
                value=f"5={market_state.ema_5:.1f} 13={market_state.ema_13:.1f}",
                threshold=f"Fresh cross {'up' if option_type=='CE' else 'down'} ≤{self.EMA_CROSS_LOOKBACK} bars",
                message=f"EMA5 cross={'YES' if cross_ok else 'NO'}",
            ))

        # ─── C4: VWAP alignment ────────────────────────────────
        if option_type == "CE":
            vwap_aligned = market_state.spot_price > market_state.vwap and market_state.vwap_slope > 0
        else:
            vwap_aligned = market_state.spot_price < market_state.vwap and market_state.vwap_slope < 0
        conditions.append(EntryConditionResult(
            name="vwap_alignment", passed=vwap_aligned,
            value=market_state.spot_price - market_state.vwap, threshold=0.0,
            message=f"Spot-VWAP={market_state.spot_price-market_state.vwap:.2f} "
                    f"slope={market_state.vwap_slope:.6f}",
        ))

        # ─── C5: Volume confirmation (relaxed for scalp) ───────
        vol_mult = self._strategy_cfg["volume"]["confirmation_multiplier"]
        # VWAP_BOUNCE and EMA_SCALP use a softer volume threshold
        if signal_type == SignalType.ORB_BREAKOUT:
            vol_ok = market_state.volume_ratio >= vol_mult
        else:
            vol_ok = market_state.volume_ratio >= vol_mult * 0.75
        conditions.append(EntryConditionResult(
            name="volume_confirmation", passed=vol_ok,
            value=market_state.volume_ratio, threshold=vol_mult,
            message=f"Vol ratio={market_state.volume_ratio:.2f}x (need {vol_mult}x)",
        ))

        # ─── C6: EMA stack alignment ───────────────────────────
        if option_type == "CE":
            ema_aligned = market_state.ema_5 > market_state.ema_13 > market_state.ema_21
        else:
            ema_aligned = market_state.ema_5 < market_state.ema_13 < market_state.ema_21
        conditions.append(EntryConditionResult(
            name="ema_stack", passed=ema_aligned,
            value=f"5={market_state.ema_5:.1f} 13={market_state.ema_13:.1f} 21={market_state.ema_21:.1f}",
            threshold=f"Aligned {'bullish' if option_type=='CE' else 'bearish'}",
            message=f"EMA 5/13/21 aligned: {ema_aligned}",
        ))

        # ─── C7: RSI momentum filter (new) ─────────────────────
        rsi = market_state.rsi
        rsi_min = self._strategy_cfg.get("rsi", {}).get("min_for_ce", 45)
        rsi_max = self._strategy_cfg.get("rsi", {}).get("max_for_pe", 55)
        rsi_overbought = self._strategy_cfg.get("rsi", {}).get("overbought", 75)
        rsi_oversold = self._strategy_cfg.get("rsi", {}).get("oversold", 25)
        if option_type == "CE":
            rsi_ok = rsi_min <= rsi <= rsi_overbought  # Bullish momentum, not extreme overbought
        else:
            rsi_ok = rsi_oversold <= rsi <= rsi_max    # Bearish momentum, not extreme oversold
        conditions.append(EntryConditionResult(
            name="rsi_filter", passed=rsi_ok,
            value=rsi, threshold=f"{rsi_min}-{rsi_overbought}" if option_type == "CE" else f"{rsi_oversold}-{rsi_max}",
            message=f"RSI={rsi:.1f} ok={rsi_ok}",
        ))

        # ─── C8: India VIX ─────────────────────────────────────
        vix_max = self._strategy_cfg["regime"]["vix_max_normal"]
        vix_ok = market_state.india_vix < vix_max or market_state.india_vix == 0.0
        conditions.append(EntryConditionResult(
            name="vix_filter", passed=vix_ok,
            value=market_state.india_vix, threshold=vix_max,
            message=f"India VIX={market_state.india_vix:.2f} (max={vix_max})",
        ))

        # ─── C9: Option spread ─────────────────────────────────
        if option_quote:
            spread_abs_max = self._strategy_cfg["options"]["max_spread_absolute"]
            spread_pct_max = self._strategy_cfg["options"]["max_spread_pct"]
            spread_ok = (
                option_quote.spread <= spread_abs_max or
                option_quote.spread_pct <= spread_pct_max
            )
            spread_value = option_quote.spread
            signal.option_spread = option_quote.spread
        else:
            # Don't hard-block on missing quote in live — let risk engine validate
            spread_ok = True
            spread_value = 0.0
        conditions.append(EntryConditionResult(
            name="spread_filter", passed=spread_ok,
            value=spread_value,
            threshold=f"<={self._strategy_cfg['options']['max_spread_absolute']}",
            message=f"Option spread={spread_value:.2f}",
        ))

        # ─── C10: Option OI ────────────────────────────────────
        if option_quote:
            min_oi = self._strategy_cfg["options"]["min_oi"]
            oi_ok = option_quote.oi >= min_oi or option_quote.oi == 0  # 0 = not yet available
            oi_value = option_quote.oi
            signal.option_oi = option_quote.oi
        else:
            oi_ok = True
            oi_value = 0
            min_oi = self._strategy_cfg["options"]["min_oi"]
        conditions.append(EntryConditionResult(
            name="oi_liquidity", passed=oi_ok,
            value=oi_value, threshold=min_oi,
            message=f"Option OI={oi_value:,}",
        ))

        # ─── C11: Delta ────────────────────────────────────────
        if option_quote and option_quote.delta is not None:
            d_min = self._strategy_cfg["options"]["preferred_delta_min"]
            d_max = self._strategy_cfg["options"]["preferred_delta_max"]
            delta_ok = d_min <= abs(option_quote.delta) <= d_max
            delta_val = abs(option_quote.delta)
            signal.option_delta = option_quote.delta
        else:
            delta_ok = True
            delta_val = 0.0
            d_min, d_max = 0.35, 0.65
        conditions.append(EntryConditionResult(
            name="delta_filter", passed=delta_ok,
            value=delta_val, threshold=f"{d_min}-{d_max}",
            message=f"|delta|={delta_val:.3f}",
        ))

        # ─── C12: Risk state ───────────────────────────────────
        no_position = not existing_position
        trade_count_ok = trades_today < self._strategy_cfg["max_trades_per_day"]
        loss_streak_ok = consecutive_losses < self._strategy_cfg["consecutive_loss_halt"]
        halted = self._session_halted
        risk_ok = no_position and trade_count_ok and loss_streak_ok and not halted
        conditions.append(EntryConditionResult(
            name="risk_state", passed=risk_ok,
            value=f"pos={existing_position} trades={trades_today} losses={consecutive_losses}",
            threshold="No position, limits not breached",
            message=(
                f"NoPos={no_position}, Trades={trades_today}/{self._strategy_cfg['max_trades_per_day']}, "
                f"Losses={consecutive_losses}/{self._strategy_cfg['consecutive_loss_halt']}"
            ),
        ))

        # ─── COMPOSITE ─────────────────────────────────────────
        signal.conditions = conditions
        all_passed = all(c.passed for c in conditions)

        if all_passed:
            signal.is_valid = True
            if option_quote:
                signal.option_premium = option_quote.ltp
            logger.info(
                f"SIGNAL [{signal_type.value}]: {option_type} @ RSI={rsi:.0f} "
                f"vol={market_state.volume_ratio:.1f}x vwap_slope={market_state.vwap_slope:.5f}",
                extra={"signal_id": signal.signal_id}
            )
        else:
            failed = [c for c in conditions if not c.passed]
            signal.rejection_reason = " | ".join(f"{c.name}: {c.message}" for c in failed)
            logger.debug(f"Signal rejected: {option_type} [{signal_type.value}] — {signal.rejection_reason}")

        return signal if all_passed else None

    # ──────────────────────────────────────────────────────────
    # SIGNAL CLASSIFICATION HELPERS
    # ──────────────────────────────────────────────────────────

    def _classify_signal(
        self,
        market_state: MarketState,
        current_time: dtime,
        option_type: str,
    ) -> tuple[Optional[SignalType], SignalDirection]:
        """
        Determine which signal type applies at this bar.
        Priority: ORB_BREAKOUT > VWAP_BOUNCE > EMA_SCALP
        Returns (signal_type, direction) or (None, ...) if no signal applies.
        """
        direction = SignalDirection.BULLISH if option_type == "CE" else SignalDirection.BEARISH

        if not self._in_any_entry_window(current_time):
            return None, direction

        # ORB breakout — requires ORB locked
        if market_state.orb_range > 0:
            if self._check_orb_breakout(market_state, option_type):
                return SignalType.ORB_BREAKOUT, direction

        # VWAP bounce — available all windows
        if market_state.vwap > 0 and self._check_vwap_bounce(market_state, option_type):
            return SignalType.VWAP_BOUNCE, direction

        # EMA scalp — fresh EMA cross
        if self._check_ema_cross(option_type):
            return SignalType.EMA_SCALP, direction

        return None, direction

    def _in_any_entry_window(self, t: dtime) -> bool:
        return (
            self.MORNING_WINDOW[0] <= t <= self.MORNING_WINDOW[1] or
            self.MIDMORNING_WINDOW[0] <= t <= self.MIDMORNING_WINDOW[1] or
            self.AFTERNOON_WINDOW[0] <= t <= self.AFTERNOON_WINDOW[1]
        )

    def _check_orb_breakout(self, state: MarketState, option_type: str) -> bool:
        """True if spot has broken out of the ORB range with buffer."""
        if state.orb_range <= 0:
            return False
        cfg = self._strategy_cfg["orb"]
        buffer = max(
            cfg["breakout_buffer_points"],
            state.orb_range * cfg["breakout_buffer_pct"],
        )
        if option_type == "CE":
            return state.spot_price > state.orb_high + buffer
        return state.spot_price < state.orb_low - buffer

    def _check_vwap_bounce(self, state: MarketState, option_type: str) -> bool:
        """
        VWAP bounce: spot recently touched VWAP (within PROXIMITY_PCT) and
        has now moved away in the signal direction with positive slope.
        Uses completed bar history from market data engine.
        """
        if state.vwap <= 0:
            return False
        proximity = self.VWAP_BOUNCE_PROXIMITY_PCT
        near_vwap = abs(state.spot_price - state.vwap) / state.vwap <= proximity * 3

        if option_type == "CE":
            return (
                near_vwap and
                state.spot_price > state.vwap and
                state.vwap_slope > 0
            )
        return (
            near_vwap and
            state.spot_price < state.vwap and
            state.vwap_slope < 0
        )

    def _check_ema_cross(self, option_type: str) -> bool:
        """
        EMA scalp: EMA5 crossed EMA13 within the last EMA_CROSS_LOOKBACK bars
        in the direction of the trade.
        """
        hist = self._ema5_above_13_history
        if len(hist) < self.EMA_CROSS_LOOKBACK + 1:
            return False
        recent = hist[-(self.EMA_CROSS_LOOKBACK + 1):]
        current_above = recent[-1]
        prev_below = not recent[0]
        if option_type == "CE":
            return current_above and prev_below   # Cross up within lookback
        return not current_above and not prev_below  # Cross down within lookback

    # ──────────────────────────────────────────────────────────
    # SESSION STATE
    # ──────────────────────────────────────────────────────────

    def update_session_state(
        self,
        daily_pnl: float,
        trades_today: int,
        consecutive_losses: int,
    ) -> None:
        self._daily_pnl = daily_pnl
        self._daily_trade_count = trades_today
        self._consecutive_losses = consecutive_losses

    def halt_session(self, reason: str) -> None:
        self._session_halted = True
        logger.warning(f"Signal engine halted: {reason}")

    def resume_session(self) -> None:
        self._session_halted = False
        logger.info("Signal engine resumed")

    def reset_session(self) -> None:
        self._daily_pnl = 0.0
        self._daily_trade_count = 0
        self._consecutive_losses = 0
        self._session_halted = False
        self._regime = None
        self._ema5_above_13_history = []
        logger.info("Signal engine session reset")
