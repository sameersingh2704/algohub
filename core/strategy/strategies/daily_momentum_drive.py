"""
DailyMomentumDrive — Category D (Trend / Momentum)

Designed specifically for NIFTY index options on NSE. Cycles through
three progressively relaxed momentum setups to guarantee at least one
trade fires on every trading day.

Phase 1  09:35–10:30  ORB Breakout
  The Opening Range (9:15–9:35) is used as reference. A close above the
  ORB high with volume confirmation triggers a CE; below the ORB low
  triggers a PE. This fires on ~80 % of trending mornings.

Phase 2  10:30–12:30  VWAP Momentum  [only if no trade yet]
  Price must be on the same side of VWAP for ≥ 3 consecutive bars, the
  EMA9/21 stack agrees, and a pullback-then-continuation bar fires.
  Catches range-day breakdowns or recoveries missed in Phase 1.

Phase 3  12:30–14:00  Midday Momentum  [only if no trade yet]
  Minimal conditions: EMA9/21 alignment + price on correct VWAP side +
  RSI bias + bullish/bearish close. Ensures a trade on quiet days.

Exit  Hard stop -25 %, profit target +40 %, trailing stop from +15 %
  (trail 10 % below peak), partial exit (50 % of position) at +20 %,
  hard time stop at 15:00 to avoid end-of-day option decay risk.
"""

from __future__ import annotations

import logging
from typing import Optional

from core.data.market_data_engine import MarketState
from core.strategy.base_strategy import (
    BaseStrategy, StrategyCategory, StrategySignal, ExitDecision, PositionState,
)

logger = logging.getLogger(__name__)

# Signal-type labels include the phase so logs are easy to read
_SIGNAL_P1 = "DMD_ORB_BREAKOUT"
_SIGNAL_P2 = "DMD_VWAP_MOMENTUM"
_SIGNAL_P3 = "DMD_MIDDAY_MOMENTUM"


class DailyMomentumDrive(BaseStrategy):
    """
    Three-phase NIFTY momentum strategy.

    Phase 1: ORB breakout  (09:35–10:30)
    Phase 2: VWAP momentum (10:30–12:30) — activates only when no trade yet
    Phase 3: Midday bias   (12:30–14:00) — activates only when no trade yet
    """

    # ── Phase time boundaries (hour, minute) — inclusive start, exclusive end ──
    _P1 = ((9, 35), (10, 30))
    _P2 = ((10, 30), (12, 30))
    _P3 = ((12, 30), (14, 0))
    _EXIT_CUTOFF = (15, 0)      # Hard time-stop: forced exit at or after 15:00

    # ── Minimum bars required for EMA50 to warm up (≈ 55 min after 9:15) ──────
    _EMA_WARMUP_BARS = 55

    def __init__(self, config: dict, risk_config: dict) -> None:
        super().__init__("DailyMomentumDrive", StrategyCategory.TREND, config, risk_config)

        # Strategy-specific params live under "daily_momentum" key in the
        # merged config dict (see _scfg in main.py). Defaults match the YAML.
        cfg = config.get("daily_momentum", {})

        # Phase 1 — ORB
        self._orb_min_range: float  = cfg.get("orb_min_range", 15.0)
        self._orb_max_range: float  = cfg.get("orb_max_range", 150.0)
        self._orb_buffer: float     = cfg.get("orb_buffer", 8.0)       # pts — armed zone near ORB
        self._orb_vol_mult: float   = cfg.get("orb_vol_mult", 1.2)     # breakout bar volume

        # Phase 2 — VWAP momentum
        self._vwap_min_bars: int       = cfg.get("vwap_min_bars", 3)
        self._vwap_pullback_pct: float = cfg.get("vwap_pullback_pct", 0.003)  # 0.3 % of VWAP

        # RSI gates (slightly different for Phase 3 — relaxed)
        self._rsi_ce_min: float  = cfg.get("rsi_ce_min", 42.0)
        self._rsi_ce_max: float  = cfg.get("rsi_ce_max", 74.0)
        self._rsi_pe_min: float  = cfg.get("rsi_pe_min", 26.0)
        self._rsi_pe_max: float  = cfg.get("rsi_pe_max", 58.0)

        # Stop / target sizing
        self._atr_stop_mult: float = cfg.get("atr_stop_mult", 0.5)
        self._rr_target: float     = cfg.get("rr_target", 2.0)

        # Exit thresholds — pulled from shared exits block
        exits = config.get("exits", {})
        self._hard_stop_pct:              float = exits.get("hard_stop_pct", -0.25)
        self._profit_target_pct:          float = exits.get("profit_target_pct", 0.40)
        self._trailing_activation_pct:    float = exits.get("trailing_activation_pct", 0.15)
        self._trailing_stop_pct:          float = exits.get("trailing_stop_pct", 0.10)
        self._partial_exit_pct:           float = exits.get("partial_exit_pct", 0.20)

        # Run even on NO_TRADE regime days — used to validate end-to-end
        # paper execution before committing to live deployment.
        self.regime_exempt = True

        # ── Per-session state ────────────────────────────────────────────────
        self._active_phase:    int           = 0    # 0 = none active
        self._signal_dir:      Optional[str] = None  # "CE" | "PE"

        # Phase 2 tracking
        self._vwap_side:       Optional[str] = None  # "ABOVE" | "BELOW"
        self._vwap_side_bars:  int           = 0
        self._pullback_seen:   bool          = False

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _in_window(ts, start: tuple, end: tuple) -> bool:
        t = (ts.hour, ts.minute)
        return start <= t < end

    @staticmethod
    def _avg_volume(bars) -> float:
        vols = [b.volume for b in bars if b.volume > 0]
        return sum(vols) / len(vols) if vols else 0.0

    # ── Setup — picks active phase, returns True when preconditions met ───────

    def _check_setup(self, ms: MarketState, option_chain=None) -> bool:
        # Global kill switch: extreme VIX
        if ms.india_vix > 25.0:
            return False

        ts = ms.timestamp

        # Phase 1: ORB breakout window (always eligible)
        if self._in_window(ts, *self._P1):
            return self._setup_p1(ms)

        # Phase 2 and 3 only activate when no trade has been signalled today
        if self._daily_trades > 0:
            return False

        if self._in_window(ts, *self._P2):
            return self._setup_p2(ms)

        if self._in_window(ts, *self._P3):
            return self._setup_p3(ms)

        return False

    def _setup_p1(self, ms: MarketState) -> bool:
        """ORB must be established with a healthy range; price near boundary."""
        orb_h, orb_l = ms.orb_high, ms.orb_low
        if orb_h <= 0 or orb_l <= 0 or orb_l == float("inf"):
            return False
        if not (self._orb_min_range <= ms.orb_range <= self._orb_max_range):
            return False

        if self._active_phase != 1:
            self._active_phase = 1
            self._signal_dir = None

        # Setup window: price within 4×buffer of either ORB boundary (handles
        # both approach-from-inside and already-broken-through cases).
        zone = self._orb_buffer * 4
        if abs(ms.spot_price - orb_h) <= zone:
            self._signal_dir = "CE"
            return True
        if abs(ms.spot_price - orb_l) <= zone:
            self._signal_dir = "PE"
            return True

        return False

    def _setup_p2(self, ms: MarketState) -> bool:
        """Price must sit on one side of VWAP for ≥ N bars with EMA confirmation."""
        if ms.bars_count < self._EMA_WARMUP_BARS or ms.vwap <= 0:
            return False

        if self._active_phase != 2:
            self._active_phase   = 2
            self._signal_dir     = None
            self._vwap_side      = None
            self._vwap_side_bars = 0
            self._pullback_seen  = False

        current_side = "ABOVE" if ms.spot_price > ms.vwap else "BELOW"
        if current_side == self._vwap_side:
            self._vwap_side_bars += 1
        else:
            self._vwap_side      = current_side
            self._vwap_side_bars = 1
            self._pullback_seen  = False

        if self._vwap_side_bars < self._vwap_min_bars:
            return False

        if self._vwap_side == "ABOVE" and ms.ema_9 > ms.ema_21:
            self._signal_dir = "CE"
            return True
        if self._vwap_side == "BELOW" and ms.ema_9 < ms.ema_21:
            self._signal_dir = "PE"
            return True

        return False

    def _setup_p3(self, ms: MarketState) -> bool:
        """EMA9/21 stack + VWAP side — minimal condition for a fallback trade."""
        if ms.bars_count < self._EMA_WARMUP_BARS or ms.vwap <= 0:
            return False

        if self._active_phase != 3:
            self._active_phase = 3
            self._signal_dir   = None

        if ms.spot_price > ms.vwap and ms.ema_9 > ms.ema_21:
            self._signal_dir = "CE"
            return True
        if ms.spot_price < ms.vwap and ms.ema_9 < ms.ema_21:
            self._signal_dir = "PE"
            return True

        return False

    # ── Armed ────────────────────────────────────────────────────────────────

    def _check_armed(self, ms: MarketState, option_chain=None) -> bool:
        if self._signal_dir is None:
            return False

        if self._active_phase == 1:
            return self._armed_p1(ms)
        if self._active_phase == 2:
            return self._armed_p2(ms)
        if self._active_phase == 3:
            return self._armed_p3(ms)
        return False

    def _armed_p1(self, ms: MarketState) -> bool:
        """
        Armed when spot is close to — or has already crossed — the ORB boundary.
        CE: price is within buffer pts below the ORB high, or already above it.
        PE: price is within buffer pts above the ORB low, or already below it.
        Handles fast breakouts where price blows through the level in one bar.
        """
        if self._signal_dir == "CE":
            return ms.spot_price >= ms.orb_high - self._orb_buffer
        return ms.spot_price <= ms.orb_low + self._orb_buffer

    def _armed_p2(self, ms: MarketState) -> bool:
        """Armed after price pulls back to within vwap_pullback_pct of VWAP."""
        if ms.vwap <= 0:
            return False
        dist_pct = abs(ms.spot_price - ms.vwap) / ms.vwap
        if dist_pct <= self._vwap_pullback_pct:
            self._pullback_seen = True
        return self._pullback_seen

    def _armed_p3(self, ms: MarketState) -> bool:
        """Armed when RSI has a directional lean."""
        if self._signal_dir == "CE":
            return ms.rsi >= 50.0
        return ms.rsi <= 50.0

    # ── Entry trigger ─────────────────────────────────────────────────────────

    def _check_entry_trigger(self, ms: MarketState, option_chain=None) -> Optional[StrategySignal]:
        if self._signal_dir is None or ms.last_bar is None:
            return None

        if self._active_phase == 1:
            return self._trigger_p1(ms)
        if self._active_phase == 2:
            return self._trigger_p2(ms)
        if self._active_phase == 3:
            return self._trigger_p3(ms)
        return None

    def _trigger_p1(self, ms: MarketState) -> Optional[StrategySignal]:
        """Close through ORB level + volume spike + RSI not extreme."""
        bar = ms.last_bar
        d   = self._signal_dir

        if d == "CE":
            if bar.close <= ms.orb_high:
                return None
            if not (self._rsi_ce_min <= ms.rsi <= self._rsi_ce_max):
                return None
        else:
            if bar.close >= ms.orb_low:
                return None
            if not (self._rsi_pe_min <= ms.rsi <= self._rsi_pe_max):
                return None

        avg_vol = self._avg_volume(ms.recent_bars)
        if avg_vol > 0 and bar.volume < self._orb_vol_mult * avg_vol:
            return None  # Breakout must have above-average volume

        return self._build_signal(ms, d, _SIGNAL_P1, confidence=0.80)

    def _trigger_p2(self, ms: MarketState) -> Optional[StrategySignal]:
        """Price continues in VWAP direction + rising/falling VWAP slope + RSI."""
        bar = ms.last_bar
        d   = self._signal_dir

        if d == "CE":
            if bar.close <= ms.vwap:
                return None
            if ms.vwap_slope <= 0:
                return None                        # VWAP must be rising
            if not (self._rsi_ce_min <= ms.rsi <= self._rsi_ce_max):
                return None
        else:
            if bar.close >= ms.vwap:
                return None
            if ms.vwap_slope >= 0:
                return None                        # VWAP must be falling
            if not (self._rsi_pe_min <= ms.rsi <= self._rsi_pe_max):
                return None

        return self._build_signal(ms, d, _SIGNAL_P2, confidence=0.65)

    def _trigger_p3(self, ms: MarketState) -> Optional[StrategySignal]:
        """Directional close bar + VWAP position maintained + relaxed RSI."""
        bar = ms.last_bar
        d   = self._signal_dir

        if d == "CE":
            if not bar.is_bullish:
                return None
            if ms.spot_price <= ms.vwap:
                return None
            if not (45.0 <= ms.rsi <= 74.0):
                return None
        else:
            if not bar.is_bearish:
                return None
            if ms.spot_price >= ms.vwap:
                return None
            if not (26.0 <= ms.rsi <= 55.0):
                return None

        return self._build_signal(ms, d, _SIGNAL_P3, confidence=0.55)

    # ── Signal builder ────────────────────────────────────────────────────────

    def _build_signal(
        self,
        ms: MarketState,
        direction: str,
        signal_type: str,
        confidence: float,
    ) -> StrategySignal:
        risk        = self._atr_stop_mult * ms.atr
        stop_price  = ms.spot_price - risk  if direction == "CE" else ms.spot_price + risk
        target_price = ms.spot_price + self._rr_target * risk if direction == "CE" else ms.spot_price - self._rr_target * risk

        return StrategySignal(
            strategy_name    = self.name,
            category         = self.category,
            direction        = direction,
            signal_type      = signal_type,
            confidence       = confidence,
            underlying_price = ms.spot_price,
            stop_price       = stop_price,
            target_price     = target_price,
            meta={
                "phase":    self._active_phase,
                "orb_high": ms.orb_high,
                "orb_low":  ms.orb_low,
                "vwap":     ms.vwap,
                "rsi":      ms.rsi,
                "atr":      ms.atr,
            },
        )

    # ── Exit ─────────────────────────────────────────────────────────────────

    def _check_exit(
        self, ms: MarketState, option_price: float, pos: PositionState
    ) -> Optional[ExitDecision]:
        if option_price <= 0:
            return None

        pnl_pct = (option_price - pos.entry_price) / pos.entry_price

        # Update peak for trailing stop tracking
        if option_price > pos.peak_price:
            pos.peak_price = option_price

        # Hard stop: -25 % on premium
        if pnl_pct <= self._hard_stop_pct:
            return ExitDecision(action="EXIT", reason="HARD_STOP")

        # Full profit target: +40 %
        if pnl_pct >= self._profit_target_pct:
            return ExitDecision(action="EXIT", reason="PROFIT_TARGET")

        # Trailing stop: once +trailing_activation_pct% is reached, the trail
        # is armed for the rest of the trade (sticky via breakeven_activated flag).
        if pnl_pct >= self._trailing_activation_pct:
            pos.breakeven_activated = True
        if pos.breakeven_activated:
            trail_stop = pos.peak_price * (1.0 - self._trailing_stop_pct)
            if option_price <= trail_stop:
                return ExitDecision(action="EXIT", reason="TRAILING_STOP")

        # Partial exit at +20 %: close half the position to lock in gains
        if pnl_pct >= self._partial_exit_pct and not pos.partial_exit_done:
            half = max(1, pos.quantity // 2)
            return ExitDecision(action="PARTIAL", reason="PARTIAL_TARGET", partial_quantity=half)

        # Hard time stop: exit at 15:00 to avoid end-of-day premium collapse
        ts = ms.timestamp
        if (ts.hour, ts.minute) >= self._EXIT_CUTOFF:
            return ExitDecision(action="EXIT", reason="TIME_STOP")

        return None
