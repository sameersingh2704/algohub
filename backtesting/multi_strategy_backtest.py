"""
Multi-Strategy Backtest Runner
================================
Runs all 9 strategies concurrently on historical NIFTY Futures 1-min OHLCV data.

Designed for paper trading evaluation:
  - Each strategy tracks its own trades independently
  - Portfolio risk overlay applied (concurrent position cap, premium limit)
  - Per-strategy stats: trades, win rate, P&L, Sharpe, max drawdown
  - Combined equity curve and strategy correlation

Input: CSV with columns — date,time,open,high,low,close,volume
       (same format as run_backtest.py)

Output: avcs_backtest_trades.multi_strategy.json

Usage:
    python backtesting/multi_strategy_backtest.py --data data/nifty_futures_2024.csv
    python backtesting/multi_strategy_backtest.py --data data/nifty_futures_2024.csv --capital 200000
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")


# ── Lightweight bar representation ──────────────────────────────────────────

@dataclass
class Bar:
    dt: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def body_size(self) -> float:
        return abs(self.close - self.open)


# ── Running state (indicators) ───────────────────────────────────────────────

@dataclass
class RunningState:
    # Session VWAP
    session_pv: float = 0.0
    session_vol: int = 0

    # EMAs
    ema5: Optional[float] = None
    ema9: Optional[float] = None
    ema13: Optional[float] = None
    ema21: Optional[float] = None
    ema50: Optional[float] = None

    # RSI(14)
    rsi_avg_gain: Optional[float] = None
    rsi_avg_loss: Optional[float] = None
    rsi_prev_close: Optional[float] = None
    rsi_warmup: int = 0
    rsi: float = 50.0

    # ATR(14)
    atr_avg: Optional[float] = None
    atr_prev_close: Optional[float] = None
    atr_warmup: int = 0
    atr: float = 0.0

    # Bollinger Bands (20, 2σ)
    bb_prices: Deque[float] = field(default_factory=lambda: deque(maxlen=20))
    bb_upper: float = 0.0
    bb_middle: float = 0.0
    bb_lower: float = 0.0

    # ORB
    orb_high: float = 0.0
    orb_low: float = float("inf")
    orb_locked: bool = False
    orb_range: float = 0.0

    # Recent bars
    recent_bars: Deque[Bar] = field(default_factory=lambda: deque(maxlen=50))

    # Volume window
    vol_window: Deque[int] = field(default_factory=lambda: deque(maxlen=20))

    # EMA cross tracking
    prev_ema9_above_ema21: Optional[bool] = None
    extension_bars_above: int = 0
    extension_bars_below: int = 0
    bb_outside_dir: Optional[str] = None

    # Day-level counters
    bars_today: int = 0

    @property
    def vwap(self) -> float:
        return self.session_pv / self.session_vol if self.session_vol > 0 else 0.0

    @property
    def vwap_slope(self) -> float:
        bars = list(self.recent_bars)
        if len(bars) < 5:
            return 0.0
        recent_vwaps = [b.close for b in bars[-5:]]  # Approx slope from close
        return (recent_vwaps[-1] - recent_vwaps[0]) / recent_vwaps[0] if recent_vwaps[0] > 0 else 0.0

    @property
    def vol_ratio(self) -> float:
        if len(self.vol_window) < 3:
            return 1.0
        avg = sum(self.vol_window) / len(self.vol_window)
        return self.vol_window[-1] / avg if avg > 0 else 1.0


def update_indicators(state: RunningState, bar: Bar) -> None:
    """Update all running indicators after a completed bar."""
    # VWAP — use equal-weight VWAP when volume data is unavailable (e.g. index CSV)
    tp = (bar.high + bar.low + bar.close) / 3
    vol = bar.volume if bar.volume > 0 else 1
    state.session_pv += tp * vol
    state.session_vol += vol

    # Save EMA cross state BEFORE this bar's EMA update (signal functions use this as "previous bar")
    if state.ema9 and state.ema21:
        state.prev_ema9_above_ema21 = state.ema9 > state.ema21

    # EMAs
    price = bar.close
    if state.ema5 is None:
        state.ema5 = state.ema9 = state.ema13 = state.ema21 = state.ema50 = price
    else:
        state.ema5 = price * (2/6) + state.ema5 * (4/6)
        state.ema9 = price * (2/10) + state.ema9 * (8/10)
        state.ema13 = price * (2/14) + state.ema13 * (12/14)
        state.ema21 = price * (2/22) + state.ema21 * (20/22)
        state.ema50 = price * (2/51) + state.ema50 * (49/51)

    # RSI(14)
    if state.rsi_prev_close is not None:
        change = price - state.rsi_prev_close
        gain = max(0.0, change)
        loss = max(0.0, -change)
        state.rsi_warmup += 1
        if state.rsi_warmup <= 14:
            if state.rsi_avg_gain is None:
                state.rsi_avg_gain = gain
                state.rsi_avg_loss = loss
            else:
                state.rsi_avg_gain += gain
                state.rsi_avg_loss += loss
            if state.rsi_warmup == 14:
                state.rsi_avg_gain /= 14
                state.rsi_avg_loss /= 14
                rs = state.rsi_avg_gain / state.rsi_avg_loss if state.rsi_avg_loss > 0 else 100.0
                state.rsi = 100.0 - (100.0 / (1.0 + rs))
        else:
            a = 1.0 / 14
            state.rsi_avg_gain = state.rsi_avg_gain * (1 - a) + gain * a
            state.rsi_avg_loss = state.rsi_avg_loss * (1 - a) + loss * a
            rs = state.rsi_avg_gain / state.rsi_avg_loss if state.rsi_avg_loss > 0 else 100.0
            state.rsi = 100.0 - (100.0 / (1.0 + rs))
    state.rsi_prev_close = price

    # ATR(14)
    if state.atr_prev_close is not None:
        tr = max(bar.high - bar.low, abs(bar.high - state.atr_prev_close), abs(bar.low - state.atr_prev_close))
        state.atr_warmup += 1
        if state.atr_warmup <= 14:
            state.atr_avg = (state.atr_avg or 0.0) + tr
            if state.atr_warmup == 14:
                state.atr_avg /= 14
                state.atr = state.atr_avg
        else:
            state.atr_avg = state.atr_avg * (13/14) + tr * (1/14)
            state.atr = state.atr_avg
    state.atr_prev_close = price

    # Bollinger Bands (20, 2σ)
    state.bb_prices.append(price)
    if len(state.bb_prices) >= 20:
        prices = list(state.bb_prices)
        mean = sum(prices) / 20
        var = sum((p - mean) ** 2 for p in prices) / 20
        std = var ** 0.5
        state.bb_middle = mean
        state.bb_upper = mean + 2 * std
        state.bb_lower = mean - 2 * std

    # ORB (9:25–9:35)
    if dtime(9, 25) <= bar.dt.time() < dtime(9, 35):
        state.orb_high = max(state.orb_high, bar.high)
        state.orb_low = min(state.orb_low, bar.low)
    elif bar.dt.time() >= dtime(9, 35) and not state.orb_locked and state.orb_high > 0:
        state.orb_locked = True
        state.orb_range = state.orb_high - (state.orb_low if state.orb_low != float("inf") else state.orb_high)

    # Volume + recent bars
    state.vol_window.append(bar.volume)
    state.recent_bars.append(bar)
    state.bars_today += 1


# ── Backtest trade record ─────────────────────────────────────────────────────

@dataclass
class BacktestTrade:
    strategy_name: str
    session_date: str
    direction: str
    signal_type: str
    entry_time: str
    exit_time: str
    entry_price: float          # Option premium (synthetic)
    exit_price: float
    underlying_at_entry: float
    underlying_at_exit: float
    quantity: int               # Lots
    gross_pnl: float
    net_pnl: float
    charges: float
    pnl_pct: float
    exit_reason: str
    partial_booked: float = 0.0
    rsi_at_signal: float = 50.0
    atr_at_signal: float = 0.0


# ── Strategy-specific entry signal logic (bar-level) ─────────────────────────

def _orb_signal(bar: Bar, state: RunningState, direction: str) -> bool:
    if not state.orb_locked:
        return False
    if state.orb_range < 15 or state.orb_range > 100:
        return False
    buf = max(3, state.orb_range * 0.03)
    if direction == "CE":
        return bar.close > state.orb_high + buf
    return bar.close < (state.orb_low if state.orb_low != float("inf") else 0) - buf


def _vwap_bounce_signal(bar: Bar, state: RunningState, direction: str) -> bool:
    if state.vwap <= 0 or state.ema9 is None:
        return False
    zone = state.vwap * 0.0045
    near_vwap = abs(bar.close - state.vwap) <= zone
    if not near_vwap:
        return False
    slope_ok = state.vwap_slope > 0 if direction == "CE" else state.vwap_slope < 0
    if not slope_ok:
        return False
    ema_ok = state.ema9 > state.vwap if direction == "CE" else state.ema9 < state.vwap
    if not ema_ok:
        return False
    rsi_ok = 45 <= state.rsi <= 75 if direction == "CE" else 25 <= state.rsi <= 55
    return rsi_ok


def _ema_scalp_signal(bar: Bar, state: RunningState, direction: str) -> bool:
    if state.ema5 is None or state.ema13 is None or state.prev_ema9_above_ema21 is None:
        return False
    if direction == "CE":
        cross = state.ema5 > state.ema13 and state.prev_ema9_above_ema21 is False
        return cross and state.ema9 > state.vwap and 40 <= state.rsi <= 75
    cross = state.ema5 < state.ema13 and state.prev_ema9_above_ema21 is True
    return cross and state.ema9 < state.vwap and 25 <= state.rsi <= 60


def _liquidity_sweep_signal(bar: Bar, state: RunningState) -> Optional[str]:
    if len(state.recent_bars) < 20:
        return None
    recent = list(state.recent_bars)[-20:]
    lows = sorted(b.low for b in recent)
    highs = sorted((b.high for b in recent), reverse=True)
    avg_vol = sum(b.volume for b in recent) / len(recent)
    # When volume data is unavailable (index data), skip volume filter
    vol_ok = avg_vol <= 0 or bar.volume > 1.5 * avg_vol

    # EQL sweep: wick below, close above
    if len(lows) >= 2 and abs(lows[0] - lows[1]) / lows[0] <= 0.001:
        eql = (lows[0] + lows[1]) / 2
        if bar.low < eql and bar.close > eql and vol_ok:
            return "CE"

    # EQH sweep: wick above, close below
    if len(highs) >= 2 and abs(highs[0] - highs[1]) / highs[0] <= 0.001:
        eqh = (highs[0] + highs[1]) / 2
        if bar.high > eqh and bar.close < eqh and vol_ok:
            return "PE"

    return None


def _false_breakout_signal(bar: Bar, state: RunningState, pending: dict) -> Optional[str]:
    if len(state.recent_bars) < 20:
        return None
    recent = list(state.recent_bars)[-20:]
    swing_high = max(b.high for b in recent)
    swing_low = min(b.low for b in recent)
    avg_vol = sum(b.volume for b in recent) / len(recent)

    if "fb_direction" not in pending:
        # Check for breakout
        if bar.close > swing_high * 1.001:
            pending["fb_direction"] = "UP"
            pending["fb_level"] = swing_high
            pending["fb_bars"] = 0
        elif bar.close < swing_low * 0.999:
            pending["fb_direction"] = "DOWN"
            pending["fb_level"] = swing_low
            pending["fb_bars"] = 0
        return None

    pending["fb_bars"] = pending.get("fb_bars", 0) + 1
    if pending["fb_bars"] > 3:
        pending.pop("fb_direction", None)
        return None

    # Check reversal — weak volume (skip volume check if data unavailable)
    weak_vol = avg_vol <= 0 or bar.volume < 0.8 * avg_vol
    if pending["fb_direction"] == "UP" and bar.close < pending["fb_level"] and weak_vol:
        pending.pop("fb_direction", None)
        return "PE"
    if pending["fb_direction"] == "DOWN" and bar.close > pending["fb_level"] and weak_vol:
        pending.pop("fb_direction", None)
        return "CE"
    return None


def _bos_retest_signal(bar: Bar, state: RunningState, pending: dict) -> Optional[str]:
    if len(state.recent_bars) < 21:
        return None
    recent = list(state.recent_bars)[-21:-1]
    swing_high = max(b.high for b in recent)
    swing_low = min(b.low for b in recent)

    if "bos_dir" not in pending:
        if bar.close > swing_high * 1.0015:
            pending["bos_dir"] = "BULLISH"
            pending["bos_level"] = swing_high
        elif bar.close < swing_low * 0.9985:
            pending["bos_dir"] = "BEARISH"
            pending["bos_level"] = swing_low
        return None

    # Waiting for retest
    pending["bos_bars"] = pending.get("bos_bars", 0) + 1
    if pending["bos_bars"] > 15:
        pending.pop("bos_dir", None)
        return None

    level = pending["bos_level"]
    in_zone = abs(bar.close - level) / level <= 0.002
    if not in_zone:
        return None

    rng = bar.high - bar.low
    if pending["bos_dir"] == "BULLISH" and bar.close > bar.open:
        lower_wick = bar.close - bar.low if bar.close > bar.open else bar.open - bar.low
        if rng > 0 and lower_wick / rng >= 0.5 and 30 < state.rsi < 70:
            pending.pop("bos_dir", None)
            return "CE"
    elif pending["bos_dir"] == "BEARISH" and bar.close < bar.open:
        upper_wick = bar.high - bar.open if bar.close < bar.open else bar.high - bar.close
        if rng > 0 and upper_wick / rng >= 0.5 and 30 < state.rsi < 70:
            pending.pop("bos_dir", None)
            return "PE"
    return None


def _fvg_signal(bar: Bar, state: RunningState, pending: dict) -> Optional[str]:
    if len(state.recent_bars) < 3:
        return None
    bars = list(state.recent_bars)

    # Detect FVG in last 3 bars
    if "fvg_zone" not in pending:
        b0, b2 = bars[-3], bars[-1]
        mid = (b0.close + b2.close) / 2 if b2.close > 0 else b2.close
        if b0.high < b2.low and mid > 0 and (b2.low - b0.high) / mid >= 0.001:
            pending["fvg_zone"] = ("BULLISH", b0.high, b2.low)
            pending["fvg_bars"] = 0
        elif b0.low > b2.high and (b0.low - b2.high) / b0.low >= 0.001:
            pending["fvg_zone"] = ("BEARISH", b2.high, b0.low)
            pending["fvg_bars"] = 0
        return None

    fvg_dir, gap_bottom, gap_top = pending["fvg_zone"]
    pending["fvg_bars"] = pending.get("fvg_bars", 0) + 1
    if pending["fvg_bars"] > 10:
        pending.pop("fvg_zone", None)
        return None

    if gap_bottom <= bar.close <= gap_top:
        pending.pop("fvg_zone", None)
        return "CE" if fvg_dir == "BULLISH" else "PE"
    return None


def _ema_trend_signal(bar: Bar, state: RunningState) -> Optional[str]:
    if state.ema9 is None or state.ema21 is None or state.ema50 is None:
        return None
    if state.bars_today < 55:
        return None
    e9, e21, e50 = state.ema9, state.ema21, state.ema50
    prox = 0.003
    if e9 > e21 > e50:
        near_e21 = abs(bar.close - e21) / e21 <= prox
        cross_up = state.prev_ema9_above_ema21 is False and e9 > e21
        if near_e21 and cross_up and 40 <= state.rsi <= 65:
            return "CE"
    elif e9 < e21 < e50:
        near_e21 = abs(bar.close - e21) / e21 <= prox
        cross_down = state.prev_ema9_above_ema21 is True and e9 < e21
        if near_e21 and cross_down and 35 <= state.rsi <= 60:
            return "PE"
    return None


def _vwap_reversion_signal(bar: Bar, state: RunningState, pending: dict) -> Optional[str]:
    if state.vwap <= 0:
        return None
    ext = (bar.close - state.vwap) / state.vwap

    if abs(ext) >= 0.008:
        direction = "ABOVE" if ext > 0 else "BELOW"
        if direction == pending.get("vwap_ext_dir"):
            pending["vwap_ext_bars"] = pending.get("vwap_ext_bars", 0) + 1
        else:
            pending["vwap_ext_dir"] = direction
            pending["vwap_ext_bars"] = 1
    else:
        pending.pop("vwap_ext_dir", None)
        pending["vwap_ext_bars"] = 0
        return None

    if pending.get("vwap_ext_bars", 0) >= 3:
        if pending["vwap_ext_dir"] == "ABOVE" and bar.close < bar.open and state.rsi >= 65:
            pending.pop("vwap_ext_dir", None)
            return "PE"
        if pending["vwap_ext_dir"] == "BELOW" and bar.close > bar.open and state.rsi <= 35:
            pending.pop("vwap_ext_dir", None)
            return "CE"
    return None


def _bollinger_signal(bar: Bar, state: RunningState, pending: dict) -> Optional[str]:
    if state.bb_middle <= 0 or state.bars_today < 22:
        return None

    if "bb_outside" not in pending:
        if bar.close > state.bb_upper and state.rsi >= 70:
            pending["bb_outside"] = "ABOVE"
        elif bar.close < state.bb_lower and state.rsi <= 30:
            pending["bb_outside"] = "BELOW"
        return None

    if pending["bb_outside"] == "ABOVE" and bar.close < state.bb_upper:
        pending.pop("bb_outside", None)
        return "PE"
    if pending["bb_outside"] == "BELOW" and bar.close > state.bb_lower:
        pending.pop("bb_outside", None)
        return "CE"
    return None


# ── Exit check ───────────────────────────────────────────────────────────────

def check_exit_generic(
    pos: dict, option_price: float, bar: Bar,
    cfg: dict, is_tuesday: bool
) -> Tuple[Optional[str], Optional[int]]:
    """Returns (exit_reason, partial_qty) or (None, None) to hold."""
    entry = pos["entry_price"]
    if entry <= 0:
        return None, None

    pnl_pct = (option_price - entry) / entry

    # Update peak
    if option_price > pos.get("peak_price", entry):
        pos["peak_price"] = option_price

    # Hard stop
    if pnl_pct <= cfg.get("hard_stop_pct", -0.30):
        return "HARD_STOP", None

    # Time stop
    cutoff = dtime(15, 0) if (is_tuesday and pos.get("strategy") == "GammaPinning") else dtime(15, 15)
    if bar.dt.time() >= cutoff:
        return "TIME_STOP", None

    # Breakeven stop
    be_act = cfg.get("breakeven_activation_pct", 0.15)
    be_lock = cfg.get("breakeven_lock_pct", 0.02)
    if not pos.get("be_activated") and pnl_pct >= be_act:
        pos["be_activated"] = True
        pos["be_stop"] = entry * (1 + be_lock)
    if pos.get("be_activated") and not pos.get("trailing_activated"):
        if option_price <= pos.get("be_stop", 0):
            return "BREAKEVEN_STOP", None

    # Partial exit — skip when only 1 lot (can't sell fractional lots)
    partial_pct = cfg.get("partial_exit_pct", 0.20)
    partial_frac = cfg.get("first_partial_pct", 0.50)
    if not pos.get("partial_done") and pnl_pct >= partial_pct:
        pos["partial_done"] = True
        if pos["quantity"] >= 2:
            partial_qty = max(1, int(pos["quantity"] * partial_frac))
            return "PARTIAL_EXIT", partial_qty
        # 1-lot: mark done, let trailing/target handle the full exit

    # Trailing stop
    trail_act = cfg.get("trailing_activation_pct", 0.15)
    trail_pct = cfg.get("trailing_stop_pct", 0.12)
    if pnl_pct >= trail_act:
        if not pos.get("trailing_activated"):
            pos["trailing_activated"] = True
        trail_stop = pos["peak_price"] * (1 - trail_pct)
        if option_price <= trail_stop:
            return "TRAILING_STOP", None

    # Profit target
    if pnl_pct >= cfg.get("profit_target_pct", 0.45):
        return "PROFIT_TARGET", None

    return None, None


# ── Synthetic option price model ─────────────────────────────────────────────

def synthetic_option_price(spot: float, strike: int, direction: str, atr: float) -> float:
    """
    Simple intrinsic + time-value proxy for backtesting.
    Real IV data unavailable in Kite CSV; ATR proxy captures volatility.
    """
    moneyness = (spot - strike) if direction == "CE" else (strike - spot)
    intrinsic = max(0.0, moneyness)
    time_value = atr * 0.8  # ATR as volatility proxy
    return max(5.0, intrinsic + time_value)


# ── Per-strategy P&L tracking ────────────────────────────────────────────────

@dataclass
class StrategyStats:
    name: str
    trades: List[BacktestTrade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.net_pnl > 0)

    @property
    def win_rate(self) -> float:
        return self.wins / self.total_trades if self.total_trades > 0 else 0.0

    @property
    def total_pnl(self) -> float:
        return sum(t.net_pnl for t in self.trades)

    @property
    def avg_pnl(self) -> float:
        return self.total_pnl / self.total_trades if self.total_trades > 0 else 0.0

    @property
    def max_drawdown(self) -> float:
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0]
        max_dd = 0.0
        for v in self.equity_curve:
            peak = max(peak, v)
            dd = (peak - v) / peak if peak > 0 else 0.0
            max_dd = max(max_dd, dd)
        return max_dd

    def to_dict(self) -> dict:
        return {
            "strategy": self.name,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "win_rate": round(self.win_rate * 100, 1),
            "total_pnl": round(self.total_pnl, 2),
            "avg_pnl_per_trade": round(self.avg_pnl, 2),
            "max_drawdown_pct": round(self.max_drawdown * 100, 2),
            "trades": [asdict(t) for t in self.trades],
        }


STRATEGY_NAMES = [
    "AVCS",
    "LiquiditySweepReversal",
    "FalseBreakoutTrap",
    "BOSRetest",
    "FairValueGap",
    "EMATrendRide",
    "VWAPMeanReversion",
    "BollingerReversion",
    "GammaPinning",
]

# ── Backtest engine ───────────────────────────────────────────────────────────

class MultiStrategyBacktester:
    """
    Runs all 9 strategies on the same bar stream.
    State is isolated per strategy (pending_signals, active_position).
    """

    def __init__(self, config: dict, capital: float) -> None:
        self._cfg = config
        self._capital = capital
        self._lot_size = config["strategy"].get("options", {}).get("lot_size", 75)
        self._strike_interval = config["strategy"].get("options", {}).get("strike_interval", 50)
        self._exit_cfg = config["strategy"].get("exits", {})

        # Per-strategy state (lifetime)
        self._stats: Dict[str, StrategyStats] = {n: StrategyStats(n) for n in STRATEGY_NAMES}

        self._max_premium = capital * config["risk"].get("per_trade", {}).get("max_premium_exposure_pct", 0.02)
        self._max_concurrent = config["risk"].get("daily", {}).get("max_trades", 4)

        # Per-strategy per-day limits from strategies config
        strategies_cfg = config.get("strategies", {})
        self._max_trades_per_day: Dict[str, int] = {}
        _name_map = {
            "LiquiditySweepReversal": "liquidity_sweep",
            "FalseBreakoutTrap": "false_breakout",
            "BOSRetest": "bos_retest",
            "FairValueGap": "fair_value_gap",
            "EMATrendRide": "ema_trend_ride",
            "VWAPMeanReversion": "vwap_mean_reversion",
            "BollingerReversion": "bollinger_reversion",
            "GammaPinning": "gamma_pinning",
        }
        for sname in STRATEGY_NAMES:
            cfg_key = _name_map.get(sname)
            max_td = strategies_cfg.get(cfg_key, {}).get("max_trades_per_day", 2) if cfg_key else 4
            self._max_trades_per_day[sname] = max_td

        # Session-scoped state (reset each day)
        self._pending: Dict[str, dict] = {n: {} for n in STRATEGY_NAMES}
        self._active: Dict[str, Optional[dict]] = {n: None for n in STRATEGY_NAMES}
        self._portfolio_premium: float = 0.0
        self._daily_trades: Dict[str, int] = {n: 0 for n in STRATEGY_NAMES}

    def reset_session(self) -> None:
        """Reset all session-scoped state at the start of each trading day."""
        self._pending = {n: {} for n in STRATEGY_NAMES}
        self._active = {n: None for n in STRATEGY_NAMES}
        self._portfolio_premium = 0.0
        self._daily_trades = {n: 0 for n in STRATEGY_NAMES}

    def _active_count(self) -> int:
        return sum(1 for v in self._active.values() if v is not None)

    def _can_enter(self, strategy_name: str, premium: float) -> bool:
        if self._active[strategy_name] is not None:
            return False
        if self._daily_trades[strategy_name] >= self._max_trades_per_day.get(strategy_name, 4):
            return False
        if self._active_count() >= self._max_concurrent:
            return False
        if self._portfolio_premium + premium * self._lot_size > self._max_premium:
            return False
        return True

    def _atm_strike(self, spot: float) -> int:
        return round(spot / self._strike_interval) * self._strike_interval

    def _synthetic_price(self, index_spot: float, direction: str, atr: float) -> float:
        """Option price uses index spot (options are priced on NIFTY index, not futures)."""
        strike = self._atm_strike(index_spot)
        return synthetic_option_price(index_spot, strike, direction, atr)

    def process_bar(self, bar: Bar, state: RunningState, session_date: date,
                    index_spot: Optional[float] = None) -> None:
        """
        Process one bar for all strategies.
        bar        — NIFTY Futures bar (signals, volume, indicators)
        index_spot — NIFTY Index close price for strike selection / option pricing
                     Falls back to futures close if not provided.
        """
        is_tuesday = session_date.weekday() == 1
        # Futures price drives signals; index price drives option selection
        opt_spot = index_spot if index_spot is not None else bar.close
        atr = state.atr or max(20.0, opt_spot * 0.005)

        # ── Check exits for all active positions ──────────────────────
        for name, pos in list(self._active.items()):
            if pos is None:
                continue
            option_price = self._synthetic_price(opt_spot, pos["direction"], atr)
            reason, partial_qty = check_exit_generic(pos, option_price, bar, self._exit_cfg, is_tuesday)
            if reason == "PARTIAL_EXIT" and partial_qty:
                partial_premium = option_price * partial_qty * self._lot_size
                pos["partial_booked"] = pos.get("partial_booked", 0.0) + partial_premium
                pos["quantity"] = max(1, pos["quantity"] - partial_qty)
                logger.debug(f"{name}: partial exit {partial_qty} lots @ {option_price:.2f}")
                continue
            if reason:
                self._close_position(name, pos, option_price, bar, session_date, reason)

        # ── Check entries for strategies with no position ─────────────
        t = bar.dt.time()
        in_window = (
            (dtime(9, 35) <= t <= dtime(10, 45)) or
            (dtime(11, 0) <= t <= dtime(11, 30)) or
            (dtime(13, 0) <= t <= dtime(14, 45))
        )

        if not in_window:
            return

        pending = self._pending

        def try_enter(name: str, direction: str, signal_type: str) -> None:
            opt_price = self._synthetic_price(opt_spot, direction, atr)
            if not self._can_enter(name, opt_price):
                return
            self._open_position(name, direction, signal_type, opt_price, bar, session_date, state, opt_spot)

        # AVCS
        for d in ("CE", "PE"):
            if _orb_signal(bar, state, d):
                try_enter("AVCS", d, "ORB_BREAKOUT"); break
            if _vwap_bounce_signal(bar, state, d):
                try_enter("AVCS", d, "VWAP_BOUNCE"); break
            if _ema_scalp_signal(bar, state, d):
                try_enter("AVCS", d, "EMA_SCALP"); break

        # LiquiditySweepReversal
        ls_dir = _liquidity_sweep_signal(bar, state)
        if ls_dir:
            try_enter("LiquiditySweepReversal", ls_dir, "LIQUIDITY_SWEEP")

        # FalseBreakoutTrap
        fb_dir = _false_breakout_signal(bar, state, pending["FalseBreakoutTrap"])
        if fb_dir:
            try_enter("FalseBreakoutTrap", fb_dir, "FALSE_BREAKOUT")

        # BOSRetest
        bos_dir = _bos_retest_signal(bar, state, pending["BOSRetest"])
        if bos_dir:
            try_enter("BOSRetest", bos_dir, "BOS_RETEST")

        # FairValueGap
        fvg_dir = _fvg_signal(bar, state, pending["FairValueGap"])
        if fvg_dir:
            try_enter("FairValueGap", fvg_dir, "FAIR_VALUE_GAP")

        # EMATrendRide
        ema_dir = _ema_trend_signal(bar, state)
        if ema_dir:
            try_enter("EMATrendRide", ema_dir, "EMA_TREND_RIDE")

        # VWAPMeanReversion
        vr_dir = _vwap_reversion_signal(bar, state, pending["VWAPMeanReversion"])
        if vr_dir:
            try_enter("VWAPMeanReversion", vr_dir, "VWAP_MEAN_REVERSION")

        # BollingerReversion
        bb_dir = _bollinger_signal(bar, state, pending["BollingerReversion"])
        if bb_dir:
            try_enter("BollingerReversion", bb_dir, "BOLLINGER_REVERSION")

        # GammaPinning (Tuesday only, 10:30+)
        # Uses index spot for ATM (options priced on index); futures for displacement signal
        if is_tuesday and t >= dtime(10, 30):
            atm = self._atm_strike(opt_spot)
            disp = abs(bar.close - atm) / atm  # displacement measured on futures
            if disp > 0.005:
                gp_dir = "PE" if bar.close > atm else "CE"
                try_enter("GammaPinning", gp_dir, "GAMMA_PINNING")

    def _open_position(
        self, name: str, direction: str, signal_type: str,
        opt_price: float, bar: Bar, session_date: date, state: RunningState,
        index_spot: Optional[float] = None,
    ) -> None:
        pos = {
            "strategy": name,
            "direction": direction,
            "signal_type": signal_type,
            "entry_price": opt_price,
            "entry_time": bar.dt,
            "entry_spot": index_spot if index_spot is not None else bar.close,
            "quantity": 1,  # lots
            "peak_price": opt_price,
            "partial_booked": 0.0,
            "session_date": session_date,
            "rsi": state.rsi,
            "atr": state.atr,
            "be_activated": False,
            "partial_done": False,
            "trailing_activated": False,
        }
        self._active[name] = pos
        self._portfolio_premium += opt_price * self._lot_size
        logger.info(f"[{name}] ENTRY {direction} @ {opt_price:.2f} | spot={bar.close:.2f} signal={signal_type}")

    def _close_position(
        self, name: str, pos: dict, exit_price: float,
        bar: Bar, session_date: date, reason: str,
    ) -> None:
        entry = pos["entry_price"]
        lots = pos["quantity"]
        qty = lots * self._lot_size
        gross = (exit_price - entry) * qty + pos.get("partial_booked", 0.0)
        # STT (sell side) + exchange txn + stamp duty ≈ 0.05% of premium turnover; brokerage ₹40 flat
        charges = (entry + exit_price) * qty * 0.0005 + 40.0
        net = gross - charges

        trade = BacktestTrade(
            strategy_name=name,
            session_date=str(session_date),
            direction=pos["direction"],
            signal_type=pos["signal_type"],
            entry_time=str(pos["entry_time"]),
            exit_time=str(bar.dt),
            entry_price=round(entry, 2),
            exit_price=round(exit_price, 2),
            underlying_at_entry=round(pos["entry_spot"], 2),
            underlying_at_exit=round(bar.close, 2),
            quantity=lots,
            gross_pnl=round(gross, 2),
            net_pnl=round(net, 2),
            charges=round(charges, 2),
            pnl_pct=round((exit_price - entry) / entry * 100, 2) if entry > 0 else 0.0,
            exit_reason=reason,
            partial_booked=round(pos.get("partial_booked", 0.0), 2),
            rsi_at_signal=round(pos["rsi"], 2),
            atr_at_signal=round(pos["atr"], 2),
        )

        self._stats[name].trades.append(trade)
        self._stats[name].equity_curve.append(
            (self._stats[name].equity_curve[-1] if self._stats[name].equity_curve else 0.0) + net
        )
        self._portfolio_premium -= entry * self._lot_size
        self._portfolio_premium = max(0.0, self._portfolio_premium)
        self._active[name] = None
        self._daily_trades[name] = self._daily_trades.get(name, 0) + 1

        outcome = "WIN" if net > 0 else "LOSS"
        logger.info(
            f"[{name}] EXIT {reason} @ {exit_price:.2f} | "
            f"net=₹{net:.2f} ({outcome}) pnl%={trade.pnl_pct:.1f}%"
        )

    def end_of_session(self, bar: Bar, session_date: date, state: RunningState,
                        index_spot: Optional[float] = None) -> None:
        """Close all open positions at session end."""
        opt_spot = index_spot if index_spot is not None else bar.close
        for name, pos in list(self._active.items()):
            if pos is None:
                continue
            opt_price = self._synthetic_price(opt_spot, pos["direction"], state.atr or 20.0)
            self._close_position(name, pos, opt_price, bar, session_date, "SESSION_END")

    def summary(self) -> dict:
        all_trades = [t for s in self._stats.values() for t in s.trades]
        total_pnl = sum(t.net_pnl for t in all_trades)
        wins = sum(1 for t in all_trades if t.net_pnl > 0)

        return {
            "total_trades": len(all_trades),
            "total_wins": wins,
            "overall_win_rate": round(wins / len(all_trades) * 100, 1) if all_trades else 0.0,
            "total_net_pnl": round(total_pnl, 2),
            "by_strategy": {n: s.to_dict() for n, s in self._stats.items()},
        }


# ── CSV loading ───────────────────────────────────────────────────────────────

def load_csv(path: str) -> List[Bar]:
    bars = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Support both "timestamp" ISO format and separate "date"+"time" columns
            if "timestamp" in row:
                ts = row["timestamp"].replace("T", " ")
                try:
                    dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    dt = datetime.strptime(ts, "%Y-%m-%d %H:%M")
            else:
                dt_str = f"{row['date']} {row['time']}"
                try:
                    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
            bars.append(Bar(
                dt=dt,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(float(row.get("volume", 0))),
            ))
    return sorted(bars, key=lambda b: b.dt)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-Strategy Backtest")
    parser.add_argument("--futures", "--data", required=True, dest="futures",
                        help="Path to NIFTY Futures 1-min CSV (signals + volume)")
    parser.add_argument("--index", default=None,
                        help="Path to NIFTY Index 1-min CSV (strike selection). "
                             "Falls back to futures if omitted.")
    parser.add_argument("--capital", type=float, default=100_000, help="Starting capital (₹)")
    parser.add_argument("--config", default="config", help="Config directory path")
    parser.add_argument("--output", default="avcs_backtest_trades.multi_strategy.json")
    args = parser.parse_args()

    # Load config
    config_dir = Path(args.config)
    with open(config_dir / "strategy_config.yaml") as f:
        full_strategy = yaml.safe_load(f)
    with open(config_dir / "risk_config.yaml") as f:
        risk_cfg = yaml.safe_load(f)

    config = {
        "strategy": full_strategy["strategy"],
        "strategies": full_strategy.get("strategies", {}),
        "risk": risk_cfg.get("risk", risk_cfg),
    }

    # Load futures bars
    logger.info(f"Loading futures bars from {args.futures}")
    futures_bars = load_csv(args.futures)
    logger.info(f"Loaded {len(futures_bars)} futures bars")

    # Load index bars and build timestamp→close lookup (optional)
    index_lookup: Dict[datetime, float] = {}
    if args.index:
        logger.info(f"Loading index bars from {args.index}")
        index_bars = load_csv(args.index)
        index_lookup = {b.dt: b.close for b in index_bars}
        logger.info(f"Loaded {len(index_bars)} index bars")
    else:
        logger.info("No index file provided — using futures price for option selection")

    backtester = MultiStrategyBacktester(config, args.capital)

    # Group futures bars by session date
    sessions: Dict[date, List[Bar]] = {}
    for bar in futures_bars:
        sessions.setdefault(bar.dt.date(), []).append(bar)

    total_sessions = len(sessions)
    for i, (session_date, bars) in enumerate(sorted(sessions.items()), 1):
        logger.info(f"Session {i}/{total_sessions}: {session_date} ({len(bars)} bars)")

        state = RunningState()
        backtester.reset_session()

        for bar in bars:
            if bar.dt.time() < dtime(9, 15):
                continue

            # Futures bar drives all indicators
            update_indicators(state, bar)

            if bar.dt.time() < dtime(9, 35):
                continue

            # Index close for this minute (for option strike selection)
            idx_spot = index_lookup.get(bar.dt)
            backtester.process_bar(bar, state, session_date, index_spot=idx_spot)

        # End of session
        if bars:
            last = bars[-1]
            backtester.end_of_session(
                last, session_date, state,
                index_spot=index_lookup.get(last.dt)
            )

    result = backtester.summary()

    # Print summary
    print("\n" + "="*60)
    print("MULTI-STRATEGY BACKTEST SUMMARY")
    print("="*60)
    print(f"Total trades:   {result['total_trades']}")
    print(f"Win rate:       {result['overall_win_rate']:.1f}%")
    print(f"Total net P&L:  ₹{result['total_net_pnl']:,.2f}")
    print()
    print(f"{'Strategy':<28} {'Trades':>6} {'WR%':>6} {'NetP&L':>12} {'MaxDD%':>8}")
    print("-" * 65)
    for n, s in result["by_strategy"].items():
        if s["total_trades"] > 0:
            print(
                f"{n:<28} {s['total_trades']:>6} {s['win_rate']:>6.1f} "
                f"₹{s['total_pnl']:>10,.2f} {s['max_drawdown_pct']:>7.2f}%"
            )
    print("="*60)

    # Write JSON output
    out_path = Path(args.output)
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2, default=str)
    logger.info(f"Results written to {out_path}")

    # Write CSV — one row per trade across all strategies
    csv_path = out_path.with_suffix(".csv")
    all_trades = [t for s in result["by_strategy"].values() for t in s["trades"]]
    if all_trades:
        fieldnames = list(all_trades[0].keys())
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_trades)
        logger.info(f"Trade CSV written to {csv_path} ({len(all_trades)} rows)")


if __name__ == "__main__":
    main()
