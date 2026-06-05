"""
Market Data Engine
===================
Central hub for all market data processing:
- Tick ingestion and normalization
- 1-minute OHLCV bar construction
- VWAP calculation (session-level)
- EMA calculation
- Option chain caching and parsing
- Stale data detection
- India VIX tracking

Thread-safe via asyncio. All external access must go through
get_* methods which return immutable snapshots.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from typing import Callable, Dict, List, Optional, Deque

import numpy as np
import pandas as pd

from core.broker.websocket_handler import TickData
from core.data.greeks_calculator import GreeksCalculator, Greeks

logger = logging.getLogger(__name__)


@dataclass
class OHLCVBar:
    """Single 1-minute OHLCV bar."""
    token: str
    symbol: str
    bar_time: datetime  # Bar START time
    open: float
    high: float
    low: float
    close: float
    volume: int
    oi: int
    vwap: float = 0.0

    @property
    def body_size(self) -> float:
        return abs(self.close - self.open)

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open


@dataclass
class OptionQuote:
    """Full snapshot of an option contract at a point in time."""
    symbol: str
    token: str
    strike: int
    option_type: str
    expiry: date
    ltp: float
    bid: float
    ask: float
    volume: int
    oi: int
    change_oi: int
    iv: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def spread(self) -> float:
        return self.ask - self.bid if self.ask > 0 and self.bid > 0 else 0.0

    @property
    def mid_price(self) -> float:
        return (self.bid + self.ask) / 2 if self.bid > 0 and self.ask > 0 else self.ltp

    @property
    def spread_pct(self) -> float:
        return self.spread / self.mid_price if self.mid_price > 0 else 0.0

    def is_liquid(self, min_oi: int = 100000, max_spread_pct: float = 0.03) -> bool:
        return self.oi >= min_oi and self.spread_pct <= max_spread_pct


@dataclass
class MarketState:
    """
    Complete snapshot of market state at current moment.
    Returned by get_market_state() — immutable reference.
    """
    timestamp: datetime
    spot_price: float
    vwap: float
    vwap_slope: float            # Positive = rising VWAP
    ema_5: float
    ema_9: float
    ema_13: float
    ema_21: float
    ema_50: float
    volume_ratio: float          # Current bar vol / 20-bar avg
    rsi: float                   # RSI(14) — 50 default until warmed up
    atr: float                   # ATR(14) — Wilder's smoothed
    bb_upper: float              # Bollinger Band upper (20, 2σ)
    bb_middle: float             # Bollinger Band middle (20-SMA)
    bb_lower: float              # Bollinger Band lower (20, 2σ)
    bb_width: float              # (upper - lower) / middle
    india_vix: float
    orb_high: float
    orb_low: float
    orb_range: float
    bars_count: int              # Number of 1-min bars since open
    last_bar: Optional[OHLCVBar]
    recent_bars: List[OHLCVBar]  # Last 50 completed bars for structure analysis
    is_data_fresh: bool          # False if last tick > stale threshold


class BarBuilder:
    """Builds 1-minute OHLCV bars from tick stream."""

    def __init__(self, token: str, symbol: str) -> None:
        self._token = token
        self._symbol = symbol
        self._current_bar: Optional[OHLCVBar] = None
        self._completed_bars: Deque[OHLCVBar] = deque(maxlen=500)
        self._session_volume: int = 0
        self._session_pv: float = 0.0  # price * volume for VWAP

        # EMA state (exponential moving averages)
        self._ema_5: Optional[float] = None
        self._ema_9: Optional[float] = None
        self._ema_13: Optional[float] = None
        self._ema_21: Optional[float] = None
        self._ema_50: Optional[float] = None

        # ATR(14) — Wilder's smoothed average true range
        self._atr_period: int = 14
        self._atr_avg: Optional[float] = None
        self._atr: float = 0.0
        self._atr_prev_close: Optional[float] = None
        self._atr_warmup: int = 0

        # Bollinger Bands (20, 2σ)
        self._bb_period: int = 20
        self._bb_prices: Deque[float] = deque(maxlen=20)
        self._bb_upper: float = 0.0
        self._bb_middle: float = 0.0
        self._bb_lower: float = 0.0
        self._bb_width: float = 0.0

        # Recent bars buffer for structure analysis (last 50)
        self._recent_bars: Deque[OHLCVBar] = deque(maxlen=50)

        # Volume tracking for confirmation
        self._volume_window: Deque[int] = deque(maxlen=20)

        # RSI(14) state — Wilder's smoothed average gain/loss
        self._rsi_period: int = 14
        self._rsi_avg_gain: Optional[float] = None
        self._rsi_avg_loss: Optional[float] = None
        self._rsi_prev_close: Optional[float] = None
        self._rsi_warmup_bars: int = 0
        self._rsi: float = 50.0  # Default neutral until warmed up

        # Cumulative volume from exchange (volume_trade_for_the_day).
        # Angel One sends cumulative daily volume per tick, NOT per-tick volume.
        # We track the last seen value and compute bar-level delta ourselves.
        self._prev_cumulative_volume: int = 0

    def seed_bar(self, bar: OHLCVBar) -> None:
        """Feed a completed historical bar to warm up indicators. Does not emit callbacks."""
        typical_price = (bar.high + bar.low + bar.close) / 3
        self._session_pv += typical_price * bar.volume
        self._session_volume += bar.volume
        if self._session_volume > 0:
            bar.vwap = self._session_pv / self._session_volume

        self._update_emas(bar.close)
        self._update_rsi(bar.close)
        self._update_atr(bar)
        self._update_bollinger(bar.close)
        self._volume_window.append(bar.volume)
        self._recent_bars.append(bar)
        self._completed_bars.append(bar)

    def on_tick(self, tick: TickData) -> Optional[OHLCVBar]:
        """
        Process a tick. Returns completed bar if this tick closes a 1-min bar.

        Volume handling: Angel One sends volume_cumulative (total day volume so far).
        We compute the per-tick delta and accumulate it into bar.volume so each
        completed bar holds the true traded volume for that minute only.
        """
        tick_minute = tick.timestamp.replace(second=0, microsecond=0)

        # Per-tick volume delta (never negative — guards against stale/out-of-order ticks)
        tick_vol_delta = max(0, tick.volume_cumulative - self._prev_cumulative_volume)
        self._prev_cumulative_volume = tick.volume_cumulative

        if self._current_bar is None:
            self._current_bar = OHLCVBar(
                token=tick.token,
                symbol=tick.symbol,
                bar_time=tick_minute,
                open=tick.ltp,
                high=tick.ltp,
                low=tick.ltp,
                close=tick.ltp,
                volume=tick_vol_delta,
                oi=tick.oi,
            )
            return None

        if tick_minute > self._current_bar.bar_time:
            # Close the current bar
            completed = self._finalize_bar()

            # Start new bar — this tick's delta already attributed above, so start at 0
            # (tick_vol_delta was from the previous bar's last tick, which is fine —
            #  edge ticks that arrive exactly on the minute boundary go into the NEW bar)
            self._current_bar = OHLCVBar(
                token=tick.token,
                symbol=tick.symbol,
                bar_time=tick_minute,
                open=tick.ltp,
                high=tick.ltp,
                low=tick.ltp,
                close=tick.ltp,
                volume=tick_vol_delta,
                oi=tick.oi,
            )
            return completed
        else:
            # Update current bar — accumulate delta, do NOT assign cumulative
            bar = self._current_bar
            bar.high = max(bar.high, tick.ltp)
            bar.low = min(bar.low, tick.ltp)
            bar.close = tick.ltp
            bar.volume += tick_vol_delta
            bar.oi = tick.oi
            return None

    def _finalize_bar(self) -> OHLCVBar:
        """Close current bar and update running calculations."""
        bar = self._current_bar

        # Update VWAP numerator/denominator
        typical_price = (bar.high + bar.low + bar.close) / 3
        self._session_pv += typical_price * bar.volume
        self._session_volume += bar.volume
        if self._session_volume > 0:
            bar.vwap = self._session_pv / self._session_volume

        # Update indicators
        bar.vwap = bar.vwap
        self._update_emas(bar.close)
        self._update_rsi(bar.close)
        self._update_atr(bar)
        self._update_bollinger(bar.close)

        # Track volume and recent bars
        self._volume_window.append(bar.volume)
        self._recent_bars.append(bar)

        self._completed_bars.append(bar)
        logger.debug(
            f"Bar closed: {bar.symbol} {bar.bar_time.strftime('%H:%M')} "
            f"O={bar.open:.1f} H={bar.high:.1f} L={bar.low:.1f} C={bar.close:.1f} "
            f"V={bar.volume}"
        )
        return bar

    def _update_emas(self, price: float) -> None:
        """Update exponential moving averages."""
        if self._ema_5 is None:
            self._ema_5 = self._ema_9 = self._ema_13 = self._ema_21 = self._ema_50 = price
        else:
            alpha5 = 2 / (5 + 1)
            alpha9 = 2 / (9 + 1)
            alpha13 = 2 / (13 + 1)
            alpha21 = 2 / (21 + 1)
            alpha50 = 2 / (50 + 1)
            self._ema_5 = price * alpha5 + self._ema_5 * (1 - alpha5)
            self._ema_9 = price * alpha9 + self._ema_9 * (1 - alpha9)
            self._ema_13 = price * alpha13 + self._ema_13 * (1 - alpha13)
            self._ema_21 = price * alpha21 + self._ema_21 * (1 - alpha21)
            self._ema_50 = price * alpha50 + self._ema_50 * (1 - alpha50)

    def _update_atr(self, bar: OHLCVBar) -> None:
        """Wilder's ATR(14). True Range = max(H-L, |H-prevC|, |L-prevC|)."""
        if self._atr_prev_close is None:
            self._atr_prev_close = bar.close
            return

        tr = max(
            bar.high - bar.low,
            abs(bar.high - self._atr_prev_close),
            abs(bar.low - self._atr_prev_close),
        )
        self._atr_prev_close = bar.close
        self._atr_warmup += 1

        if self._atr_warmup <= self._atr_period:
            if self._atr_avg is None:
                self._atr_avg = tr
            else:
                self._atr_avg += tr
            if self._atr_warmup == self._atr_period:
                self._atr_avg /= self._atr_period
                self._atr = self._atr_avg
        else:
            alpha = 1.0 / self._atr_period
            self._atr_avg = self._atr_avg * (1 - alpha) + tr * alpha
            self._atr = self._atr_avg

    def _update_bollinger(self, price: float) -> None:
        """Bollinger Bands (20, 2σ). Updated each bar close."""
        self._bb_prices.append(price)
        if len(self._bb_prices) < self._bb_period:
            return
        prices = list(self._bb_prices)
        mean = sum(prices) / self._bb_period
        variance = sum((p - mean) ** 2 for p in prices) / self._bb_period
        std = variance ** 0.5
        self._bb_middle = mean
        self._bb_upper = mean + 2.0 * std
        self._bb_lower = mean - 2.0 * std
        self._bb_width = (self._bb_upper - self._bb_lower) / mean if mean > 0 else 0.0

    def _update_rsi(self, price: float) -> None:
        """
        Update Wilder's RSI(14).
        First 14 bars use simple average; subsequent bars use smoothed average.
        RSI < 30 = oversold (PE momentum); RSI > 70 = overbought (CE momentum).
        """
        if self._rsi_prev_close is None:
            self._rsi_prev_close = price
            return

        change = price - self._rsi_prev_close
        gain = max(0.0, change)
        loss = max(0.0, -change)
        self._rsi_prev_close = price
        self._rsi_warmup_bars += 1

        if self._rsi_warmup_bars <= self._rsi_period:
            # Accumulate for simple seed average
            if self._rsi_avg_gain is None:
                self._rsi_avg_gain = gain
                self._rsi_avg_loss = loss
            else:
                self._rsi_avg_gain += gain
                self._rsi_avg_loss += loss

            if self._rsi_warmup_bars == self._rsi_period:
                # Finalise seed averages
                self._rsi_avg_gain /= self._rsi_period
                self._rsi_avg_loss /= self._rsi_period
                rs = self._rsi_avg_gain / self._rsi_avg_loss if self._rsi_avg_loss > 0 else 100.0
                self._rsi = 100.0 - (100.0 / (1.0 + rs))
        else:
            # Wilder's smoothed average
            alpha = 1.0 / self._rsi_period
            self._rsi_avg_gain = self._rsi_avg_gain * (1 - alpha) + gain * alpha
            self._rsi_avg_loss = self._rsi_avg_loss * (1 - alpha) + loss * alpha
            rs = self._rsi_avg_gain / self._rsi_avg_loss if self._rsi_avg_loss > 0 else 100.0
            self._rsi = 100.0 - (100.0 / (1.0 + rs))

    def reset_session(self) -> None:
        """Reset all session-level accumulators (call at 9:15 AM each day)."""
        self._current_bar = None
        self._session_volume = 0
        self._session_pv = 0.0
        self._ema_5 = None
        self._ema_9 = None
        self._ema_13 = None
        self._ema_21 = None
        self._ema_50 = None
        self._volume_window.clear()
        self._rsi_avg_gain = None
        self._rsi_avg_loss = None
        self._rsi_prev_close = None
        self._rsi_warmup_bars = 0
        self._rsi = 50.0
        self._atr_avg = None
        self._atr = 0.0
        self._atr_prev_close = None
        self._atr_warmup = 0
        self._bb_prices.clear()
        self._bb_upper = 0.0
        self._bb_middle = 0.0
        self._bb_lower = 0.0
        self._bb_width = 0.0
        self._recent_bars.clear()
        # Reset cumulative tracker — exchange resets volume_trade_for_the_day at session start
        self._prev_cumulative_volume = 0
        logger.info(f"Bar builder reset for {self._symbol}")

    @property
    def bars(self) -> List[OHLCVBar]:
        return list(self._completed_bars)

    @property
    def last_bars(self) -> List[OHLCVBar]:
        return list(self._completed_bars)[-20:] if self._completed_bars else []

    @property
    def ema_5(self) -> Optional[float]:
        return self._ema_5

    @property
    def ema_9(self) -> Optional[float]:
        return self._ema_9

    @property
    def ema_13(self) -> Optional[float]:
        return self._ema_13

    @property
    def ema_21(self) -> Optional[float]:
        return self._ema_21

    @property
    def ema_50(self) -> Optional[float]:
        return self._ema_50

    @property
    def atr(self) -> float:
        return self._atr

    @property
    def bb_upper(self) -> float:
        return self._bb_upper

    @property
    def bb_middle(self) -> float:
        return self._bb_middle

    @property
    def bb_lower(self) -> float:
        return self._bb_lower

    @property
    def bb_width(self) -> float:
        return self._bb_width

    @property
    def recent_bars(self) -> List[OHLCVBar]:
        return list(self._recent_bars)

    @property
    def vwap(self) -> float:
        if self._completed_bars:
            return self._completed_bars[-1].vwap
        return 0.0

    @property
    def vwap_slope(self) -> float:
        """Slope of VWAP over last N bars. Positive = rising."""
        bars = list(self._completed_bars)
        if len(bars) < 5:
            return 0.0
        recent = [b.vwap for b in bars[-5:]]
        if recent[0] == 0:
            return 0.0
        return (recent[-1] - recent[0]) / recent[0]

    @property
    def volume_ratio(self) -> float:
        """Current bar vol relative to 20-bar average."""
        if not self._volume_window or len(self._volume_window) < 3:
            return 1.0
        avg = sum(self._volume_window) / len(self._volume_window)
        if avg == 0:
            return 1.0
        current = self._current_bar.volume if self._current_bar else 0
        return current / avg

    @property
    def rsi(self) -> float:
        return self._rsi

    @property
    def session_vwap(self) -> float:
        if self._session_volume > 0:
            return self._session_pv / self._session_volume
        return 0.0


class MarketDataEngine:
    """
    Central market data hub.

    Manages bar builders for all subscribed instruments.
    Provides clean market state snapshots for strategy consumption.

    Subscribed instruments:
    - NIFTY underlying (live paper uses NIFTY futures)
    - India VIX
    - All tracked NIFTY option strikes
    """

    ORB_START = dtime(9, 25, 0)
    ORB_END = dtime(9, 35, 0)
    SESSION_START = dtime(9, 15, 0)

    def __init__(self, greeks_calculator: GreeksCalculator) -> None:
        self._greeks_calc = greeks_calculator
        self._bar_builders: Dict[str, BarBuilder] = {}
        self._latest_ticks: Dict[str, TickData] = {}
        self._option_quotes: Dict[str, OptionQuote] = {}
        self._token_to_type: Dict[str, str] = {}  # token → "NIFTY" / "VIX" / "OPTION"
        self._nifty_token: Optional[str] = None
        self._vix_token: Optional[str] = None

        # ORB tracking
        self._orb_collecting: bool = False
        self._orb_locked: bool = False
        self._orb_high: float = 0.0
        self._orb_low: float = float("inf")
        self._orb_range: float = 0.0

        self._stale_threshold_sec: int = 30
        self._new_bar_callbacks: List[Callable[[str, OHLCVBar], None]] = []
        self._lock = asyncio.Lock()

    def register_instrument(
        self, token: str, symbol: str, instrument_type: str
    ) -> None:
        """Register an instrument for tracking. instrument_type: NIFTY/VIX/OPTION"""
        self._bar_builders[token] = BarBuilder(token, symbol)
        self._token_to_type[token] = instrument_type

        if instrument_type == "NIFTY":
            self._nifty_token = token
        elif instrument_type == "VIX":
            self._vix_token = token

        logger.info(f"Registered instrument: {symbol} ({instrument_type}) token={token}")

    def on_new_bar(self, callback: Callable[[str, OHLCVBar], None]) -> None:
        """Register callback invoked whenever a 1-min bar completes."""
        self._new_bar_callbacks.append(callback)

    def on_tick(self, tick: TickData) -> None:
        """
        Process incoming tick. Called by WebSocketHandler on every tick.
        Non-blocking: dispatches to appropriate bar builder.
        """
        token = tick.token
        self._latest_ticks[token] = tick

        # Keep registered option quotes in sync with live ticks so that
        # get_option_quote(token).ltp always reflects the current WebSocket price.
        if token in self._option_quotes:
            q = self._option_quotes[token]
            q.ltp = tick.ltp
            if tick.bid > 0:
                q.bid = tick.bid
            if tick.ask > 0:
                q.ask = tick.ask
            if tick.oi > 0:
                q.oi = tick.oi
            if tick.volume_cumulative > 0:
                q.volume = tick.volume_cumulative

        if token not in self._bar_builders:
            return

        # Update ORB if in collection window
        self._update_orb(tick)

        # Update bar builder
        builder = self._bar_builders[token]
        completed_bar = builder.on_tick(tick)

        if completed_bar:
            # Update option Greeks if this is an option tick
            if self._token_to_type.get(token) == "OPTION":
                self._update_option_greeks(token, tick)

            # Notify subscribers
            for cb in self._new_bar_callbacks:
                try:
                    cb(token, completed_bar)
                except Exception as e:
                    logger.error(f"New bar callback error: {e}", exc_info=True)

    def _update_orb(self, tick: TickData) -> None:
        """Track ORB high/low during collection window."""
        if tick.token != self._nifty_token:
            return

        current_time = tick.timestamp.time()

        if self.ORB_START <= current_time < self.ORB_END:
            self._orb_collecting = True
            self._orb_high = max(self._orb_high, tick.ltp)
            self._orb_low = min(self._orb_low, tick.ltp)

        elif current_time >= self.ORB_END and self._orb_collecting and not self._orb_locked:
            self._orb_locked = True
            self._orb_collecting = False
            self._orb_range = self._orb_high - self._orb_low

            logger.info(
                f"ORB LOCKED — High: {self._orb_high:.2f}, "
                f"Low: {self._orb_low:.2f}, "
                f"Range: {self._orb_range:.2f}"
            )

    def _update_option_greeks(self, token: str, tick: TickData) -> None:
        """Recalculate Greeks for an option on tick update."""
        if token not in self._option_quotes:
            return

        quote = self._option_quotes[token]
        if not self._nifty_token:
            return

        nifty_tick = self._latest_ticks.get(self._nifty_token)
        if not nifty_tick:
            return

        tte = GreeksCalculator.time_to_expiry_years(quote.expiry)
        if tte <= 0:
            return

        greeks = self._greeks_calc.calculate_greeks_from_market_price(
            option_type=quote.option_type,
            market_price=tick.ltp,
            spot=nifty_tick.ltp,
            strike=quote.strike,
            tte_years=tte,
        )

        if greeks:
            quote.iv = greeks.iv
            quote.delta = greeks.delta
            quote.gamma = greeks.gamma
            quote.theta = greeks.theta
            quote.vega = greeks.vega
            quote.timestamp = tick.timestamp

    def register_option(self, quote: OptionQuote) -> None:
        """Register an option contract for tracking."""
        self._option_quotes[quote.token] = quote
        self._token_to_type[quote.token] = "OPTION"
        if quote.token not in self._bar_builders:
            self._bar_builders[quote.token] = BarBuilder(quote.token, quote.symbol)

    def update_option_quote(
        self,
        token: str,
        ltp: float,
        bid: float,
        ask: float,
        volume: int,
        oi: int,
        change_oi: int,
    ) -> None:
        """Update option quote from chain refresh."""
        if token in self._option_quotes:
            q = self._option_quotes[token]
            q.ltp = ltp
            q.bid = bid
            q.ask = ask
            q.volume = volume
            q.oi = oi
            q.change_oi = change_oi
            q.timestamp = datetime.now()

    def seed_historical_bars(self, token: str, bars: List[OHLCVBar]) -> None:
        """
        Warm up indicator state by replaying completed historical bars.
        Call before live trading starts so EMAs/ATR/BB are ready from bar 1.
        Also reconstructs the ORB from today's bars if the ORB window has passed.
        """
        if token not in self._bar_builders:
            logger.warning(f"seed_historical_bars: token {token} not registered — skipping")
            return
        if token != self._nifty_token:
            builder = self._bar_builders[token]
            for bar in bars:
                builder.seed_bar(bar)
            logger.info(f"Seeded {len(bars)} historical bars for token={token} — indicators warmed up")
            return

        builder = self._bar_builders[token]
        today = datetime.now().date()
        orb_high = 0.0
        orb_low = float("inf")
        orb_found = False

        for bar in bars:
            builder.seed_bar(bar)
            # Reconstruct ORB from today's bars in the 9:25–9:35 window
            if bar.bar_time.date() == today:
                bt = bar.bar_time.time()
                if self.ORB_START <= bt < self.ORB_END:
                    orb_high = max(orb_high, bar.high)
                    orb_low = min(orb_low, bar.low)
                    orb_found = True

        # If ORB window has fully passed and we found bars in it, lock the ORB
        now_t = datetime.now().time()
        if orb_found and now_t >= self.ORB_END:
            self._orb_high = orb_high
            self._orb_low = orb_low
            self._orb_range = orb_high - orb_low
            self._orb_locked = True
            self._orb_collecting = False
            logger.info(
                f"ORB reconstructed from history — "
                f"High: {orb_high:.2f}, Low: {orb_low:.2f}, Range: {self._orb_range:.2f}"
            )
        elif orb_found and now_t < self.ORB_END:
            # Still in ORB window — seed partial state, live ticks will complete it
            self._orb_high = orb_high
            self._orb_low = orb_low
            self._orb_collecting = True

        logger.info(f"Seeded {len(bars)} historical bars for token={token} — indicators warmed up")

    def reset_session(self) -> None:
        """Reset all session state. Call at market open each day."""
        for builder in self._bar_builders.values():
            builder.reset_session()

        self._orb_collecting = False
        self._orb_locked = False
        self._orb_high = 0.0
        self._orb_low = float("inf")
        self._orb_range = 0.0
        self._latest_ticks.clear()

        logger.info("Market data engine session reset")

    def get_spot_price(self) -> Optional[float]:
        """Return latest NIFTY underlying price."""
        if not self._nifty_token:
            return None
        tick = self._latest_ticks.get(self._nifty_token)
        return tick.ltp if tick else None

    def get_india_vix(self) -> Optional[float]:
        """Return latest India VIX."""
        if not self._vix_token:
            return None
        tick = self._latest_ticks.get(self._vix_token)
        return tick.ltp if tick else None

    def get_vwap(self) -> Optional[float]:
        """Return current session VWAP for NIFTY."""
        if not self._nifty_token:
            return None
        return self._bar_builders[self._nifty_token].session_vwap

    def get_market_state(self) -> Optional[MarketState]:
        """
        Return complete market state snapshot.
        Returns None if data is insufficient.
        """
        if not self._nifty_token:
            return None

        spot = self.get_spot_price()
        if not spot:
            return None

        builder = self._bar_builders[self._nifty_token]
        nifty_tick = self._latest_ticks.get(self._nifty_token)
        is_fresh = nifty_tick is not None and (
            (datetime.now() - nifty_tick.timestamp).total_seconds() < self._stale_threshold_sec
        )

        bars = builder.bars
        return MarketState(
            timestamp=datetime.now(),
            spot_price=spot,
            vwap=builder.session_vwap,
            vwap_slope=builder.vwap_slope,
            ema_5=builder.ema_5 or spot,
            ema_9=builder.ema_9 or spot,
            ema_13=builder.ema_13 or spot,
            ema_21=builder.ema_21 or spot,
            ema_50=builder.ema_50 or spot,
            volume_ratio=builder.volume_ratio,
            rsi=builder.rsi,
            atr=builder.atr,
            bb_upper=builder.bb_upper or spot,
            bb_middle=builder.bb_middle or spot,
            bb_lower=builder.bb_lower or spot,
            bb_width=builder.bb_width,
            india_vix=self.get_india_vix() or 0.0,
            orb_high=self._orb_high,
            orb_low=self._orb_low if self._orb_low != float("inf") else 0.0,
            orb_range=self._orb_range,
            bars_count=len(bars),
            last_bar=bars[-1] if bars else None,
            recent_bars=builder.recent_bars,
            is_data_fresh=is_fresh,
        )

    def get_option_quote(self, token: str) -> Optional[OptionQuote]:
        """Return current option quote for a token."""
        return self._option_quotes.get(token)

    def get_all_option_prices(self) -> Dict[str, float]:
        """Return {token: ltp} for every registered option with a live price."""
        return {
            token: q.ltp
            for token, q in self._option_quotes.items()
            if q.ltp > 0
        }

    def get_option_quotes_for_strike(
        self, strike: int, expiry: date
    ) -> Dict[str, Optional[OptionQuote]]:
        """Return CE and PE quotes for a given strike/expiry."""
        result = {"CE": None, "PE": None}
        for token, quote in self._option_quotes.items():
            if quote.strike == strike and quote.expiry == expiry:
                result[quote.option_type] = quote
        return result

    def is_orb_ready(self) -> bool:
        """Return True if ORB has been locked for the session."""
        return self._orb_locked

    @property
    def orb_high(self) -> float:
        return self._orb_high

    @property
    def orb_low(self) -> float:
        return self._orb_low if self._orb_low != float("inf") else 0.0

    @property
    def orb_range(self) -> float:
        return self._orb_range

    def is_data_stale(self) -> bool:
        """Return True if NIFTY data is stale."""
        if not self._nifty_token:
            return True
        tick = self._latest_ticks.get(self._nifty_token)
        if not tick:
            return True
        age = (datetime.now() - tick.timestamp).total_seconds()
        return age > self._stale_threshold_sec
