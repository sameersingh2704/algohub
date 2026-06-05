"""
AVCS v2 — 1-minute scalping backtest runner.

Bias rules:
- Indicators updated only after each 1-minute bar is closed.
- Entry signals evaluated on bar T are filled on option bar T+1 open.
- Exit checks use option bars strictly after entry.
- If stop and target both occur in one bar, stop is assumed first.
- NIFTY futures OHLCV drives all price signals — index is NOT used.
- SmartAPI historical candles do not carry OI; option volume-to-date
  is used for liquidity checks instead.

Signal types (aligned with live signal_engine.py v2):
  ORB_BREAKOUT — spot crosses ORB with buffer + VWAP + EMA + RSI + volume
  VWAP_BOUNCE  — spot near VWAP, bounces with slope + EMA + RSI
  EMA_SCALP    — EMA5 fresh cross of EMA13 + VWAP alignment + RSI

Exit types:
  HARD_STOP         — -30% on premium
  BREAKEVEN_STOP    — move stop to +2% after +15% gain
  PARTIAL_EXIT      — book 50% at +20% (remainder continues)
  PROFIT_TARGET     — +45% on premium
  TRAILING_STOP     — trail 12% below peak, activates at +15%
  VWAP_INVALIDATION — thesis breaks
  TIME_STOP         — 15:15 hard cutoff
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
from collections import deque
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Deque, Dict, Iterable, List, Optional

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.auth.angel_auth import AngelAuthManager
from core.broker.angel_connector import AngelConnector
from core.broker.rate_limiter import RateLimiter
from core.data.greeks_calculator import GreeksCalculator
from core.data.instrument_manager import InstrumentInfo, InstrumentManager
from core.expenses.expense_engine import ExpenseEngine

logger = logging.getLogger("backtest")

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
ORB_START = dtime(9, 25)
ORB_END = dtime(9, 35)


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass
class BacktestTrade:
    session_date: str
    symbol: str
    token: str
    direction: str
    strike: int
    expiry: str
    signal_time: str
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    quantity: int
    signal_type: str
    exit_reason: str
    gross_pnl: float
    charges: float
    net_pnl: float
    pnl_pct: float
    underlying_at_signal: float
    vwap_at_signal: float
    rsi_at_signal: float
    volume_ratio: float


@dataclass
class RunningState:
    vwap: float = 0.0
    vwap_slope: float = 0.0
    ema_5: float = 0.0
    ema_13: float = 0.0
    ema_21: float = 0.0
    volume_ratio: float = 1.0
    orb_high: float = 0.0
    orb_low: float = 0.0
    orb_range: float = 0.0
    rsi: float = 50.0

    # RSI Wilder state
    rsi_avg_gain: float = 0.0
    rsi_avg_loss: float = 0.0
    rsi_prev_close: float = 0.0
    rsi_warmup: int = 0

    # EMA5 cross history (last 5 bars: True = EMA5 > EMA13)
    ema5_above_13: Deque = None

    def __post_init__(self):
        if self.ema5_above_13 is None:
            self.ema5_above_13 = deque(maxlen=6)


class CandleStore:
    def __init__(
        self,
        connector: AngelConnector,
        cache_dir: Path,
        refresh: bool = False,
        fetch_delay_sec: float = 2.0,
    ) -> None:
        self._connector = connector
        self._cache_dir = cache_dir
        self._refresh = refresh
        self._fetch_delay_sec = fetch_delay_sec
        self._fetch_lock = asyncio.Lock()
        self._last_fetch_at: Optional[float] = None
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    async def get(
        self,
        exchange: str,
        symbol: str,
        token: str,
        start: date,
        end: date,
        interval: str = "ONE_MINUTE",
    ) -> List[Candle]:
        suffix = "1m" if interval == "ONE_MINUTE" else interval.lower()
        cache_path = self._cache_dir / f"{exchange}_{token}_{start}_{end}_{suffix}.csv"
        if cache_path.exists() and not self._refresh:
            return read_candles(cache_path)

        async with self._fetch_lock:
            if self._last_fetch_at is not None:
                elapsed = asyncio.get_running_loop().time() - self._last_fetch_at
                wait = self._fetch_delay_sec - elapsed
                if wait > 0:
                    await asyncio.sleep(wait)

            logger.info("Fetching %s %s %s %s -> %s", interval, exchange, symbol, start, end)
            raw = await self._connector.get_historical_data(
                token=token,
                exchange=exchange,
                symbol=symbol,
                interval=interval,
                from_date=f"{start.isoformat()} 09:15",
                to_date=f"{end.isoformat()} 15:30",
            )
            self._last_fetch_at = asyncio.get_running_loop().time()

        candles = parse_smartapi_candles(raw)
        if not candles:
            logger.warning("No candles for %s %s token=%s %s -> %s", exchange, symbol, token, start, end)
        write_candles(cache_path, candles)
        return candles


class AVCSBacktester:
    def __init__(
        self,
        strategy_cfg: dict,
        risk_cfg: dict,
        expense_cfg: dict,
        instruments: InstrumentManager,
        candles: CandleStore,
        capital: float,
        slippage_bps: float,
    ) -> None:
        self.strategy_cfg = strategy_cfg
        self.risk_cfg = risk_cfg
        self.instruments = instruments
        self.candles = candles
        self.capital = capital
        self.slippage_bps = slippage_bps
        self.expenses = ExpenseEngine(expense_cfg)

    async def run(self, start: date, end: date) -> List[BacktestTrade]:
        future = self.instruments.get_nearest_future("NIFTY", start)
        if not future:
            raise RuntimeError("NIFTY FUTIDX token not found in instrument master")

        future_candles = await self.candles.get("NFO", future.symbol, future.token, start, end)
        if not future_candles:
            raise RuntimeError(f"No NIFTY futures 1-min candles for {future.symbol} {start}->{end}")

        daily_candles = await self.candles.get(
            "NFO", future.symbol, future.token,
            start - timedelta(days=14), end,
            interval="ONE_DAY",
        )
        if not daily_candles:
            raise RuntimeError(f"No NIFTY futures daily candles for CPR calculation")

        future_by_day = group_by_day(future_candles)
        previous_daily = build_previous_daily_map(daily_candles)
        trades: List[BacktestTrade] = []

        for session_day in sorted(d for d in future_by_day if start <= d <= end):
            if session_day.weekday() >= 5:
                continue
            day_future = only_market_minutes(future_by_day.get(session_day, []))
            if len(day_future) < 60:
                logger.warning("Skipping %s: insufficient futures candles", session_day)
                continue
            day_trades = await self._run_day(session_day, day_future, previous_daily.get(session_day))
            trades.extend(day_trades)

        return trades

    async def _run_day(
        self,
        session_day: date,
        future_bars: List[Candle],
        previous_daily: Optional[Candle],
    ) -> List[BacktestTrade]:
        if not future_bars or not previous_daily:
            return []

        expiry = self.instruments.get_next_nifty_expiry(session_day)
        if not expiry:
            logger.warning("Skipping %s: no NIFTY expiry found", session_day)
            return []

        regime = classify_regime(
            self.strategy_cfg,
            india_vix=0.0,
            prev_high=previous_daily.high,
            prev_low=previous_daily.low,
            prev_close=previous_daily.close,
            today_open=future_bars[0].open,
            today_is_tuesday=session_day.weekday() == 1,
        )
        if regime == "NO_TRADE":
            return []

        state = RunningState()
        volume_window: List[int] = []
        vwap_values: List[float] = []
        session_pv = 0.0
        session_volume = 0
        orb_locked = False
        active: Optional[dict] = None
        trades: List[BacktestTrade] = []
        trades_today = 0
        consecutive_losses = 0
        daily_pnl = 0.0
        option_cache: Dict[str, List[Candle]] = {}

        for i, bar in enumerate(future_bars):
            current_time = bar.timestamp.time()
            volume = max(0, bar.volume)

            # — Update VWAP ─────────────────────────────────────
            typical = (bar.high + bar.low + bar.close) / 3
            session_pv += typical * volume
            session_volume += volume
            state.vwap = session_pv / session_volume if session_volume else bar.close
            vwap_values.append(state.vwap)
            if len(vwap_values) >= 5 and vwap_values[-5] != 0:
                state.vwap_slope = (vwap_values[-1] - vwap_values[-5]) / vwap_values[-5]

            # — Update EMAs ─────────────────────────────────────
            state.ema_5 = update_ema(state.ema_5, bar.close, 5)
            state.ema_13 = update_ema(state.ema_13, bar.close, 13)
            state.ema_21 = update_ema(state.ema_21, bar.close, 21)
            if volume_window:
                avg_vol = sum(volume_window[-20:]) / min(len(volume_window), 20)
                state.volume_ratio = (volume / avg_vol) if avg_vol > 0 else 1.0
            volume_window.append(volume)

            # — Update RSI(14) ───────────────────────────────────
            update_rsi(state, bar.close)

            # — Track EMA5 cross history ─────────────────────────
            state.ema5_above_13.append(state.ema_5 > state.ema_13)

            # — ORB ──────────────────────────────────────────────
            if ORB_START <= current_time < ORB_END:
                state.orb_high = max(state.orb_high, bar.high)
                state.orb_low = min(state.orb_low or bar.low, bar.low)
            elif current_time >= ORB_END and state.orb_high and state.orb_low and not orb_locked:
                state.orb_range = state.orb_high - state.orb_low
                orb_locked = True
                if state.orb_range > self.strategy_cfg["orb"]["max_range_points"]:
                    logger.info("%s halted: ORB too wide %.2f", session_day, state.orb_range)
                    break

            # — Manage open position ─────────────────────────────
            if active:
                result = self._check_exit(session_day, bar, active, state)
                if result == "PARTIAL":
                    # Partial exit done — continue managing remainder
                    pass
                elif result is not None:
                    trades.append(result)
                    daily_pnl += result.net_pnl
                    consecutive_losses = 0 if result.net_pnl > 0 else consecutive_losses + 1
                    active = None
                    if consecutive_losses >= self.strategy_cfg["consecutive_loss_halt"]:
                        break
                continue

            # — Entry gate ───────────────────────────────────────
            if trades_today >= self.strategy_cfg["max_trades_per_day"]:
                continue
            if daily_pnl <= -(self.capital * self.risk_cfg["daily"]["max_loss_pct"]):
                continue
            if not in_entry_window(current_time):
                continue
            if i + 1 >= len(future_bars):
                continue

            direction, sig_type = self._entry_signal(bar.close, state)
            if not direction:
                continue

            option = self._select_option(direction, bar.close, expiry)
            if not option:
                continue

            # Fetch option candles (cached)
            option_bars = option_cache.get(option.token)
            if option_bars is None:
                option_bars = await self.candles.get("NFO", option.symbol, option.token, session_day, session_day)
                option_cache[option.token] = option_bars

            option_by_ts = {c.timestamp: c for c in option_bars}
            signal_option_bar = option_by_ts.get(bar.timestamp)
            next_bar = future_bars[i + 1]
            entry_option_bar = option_by_ts.get(next_bar.timestamp)
            if not signal_option_bar or not entry_option_bar:
                continue

            # Liquidity check on option volume-to-date
            min_vol = self.strategy_cfg["options"]["min_volume_today"]
            option_vol_to_date = sum(c.volume for c in option_bars if c.timestamp <= bar.timestamp)
            if option_vol_to_date < min_vol:
                continue

            lots = self._calculate_lots(signal_option_bar.close)
            if lots < 1:
                continue
            quantity = lots * self.strategy_cfg["options"]["lot_size"]
            entry_price = apply_buy_slippage(entry_option_bar.open, self.slippage_bps)

            active = {
                "session_date": session_day,
                "symbol": option.symbol,
                "token": option.token,
                "direction": direction,
                "signal_type": sig_type,
                "strike": option.strike,
                "expiry": expiry,
                "signal_time": bar.timestamp,
                "entry_time": entry_option_bar.timestamp,
                "entry_price": entry_price,
                "quantity": quantity,
                "original_quantity": quantity,
                "option_bars": option_by_ts,
                "underlying_at_signal": bar.close,
                "vwap_at_signal": state.vwap,
                "rsi_at_signal": state.rsi,
                "volume_ratio": state.volume_ratio,
                # Exit tracking state
                "trailing_activated": False,
                "trailing_high": entry_price,
                "trailing_stop": 0.0,
                "breakeven_activated": False,
                "partial_done": False,
                "partial_booked_pnl": 0.0,
                "partial_booked_charges": 0.0,
            }
            trades_today += 1

        # EOD force-close
        if active:
            last_bar = max(
                (c for c in active["option_bars"].values() if c.timestamp >= active["entry_time"]),
                key=lambda c: c.timestamp,
                default=None,
            )
            if last_bar:
                trades.append(self._close_trade(active, last_bar.timestamp, last_bar.close, "EOD"))

        return trades

    # ──────────────────────────────────────────────────────────
    # SIGNAL DETECTION
    # ──────────────────────────────────────────────────────────

    def _entry_signal(self, spot: float, state: RunningState) -> tuple[Optional[str], str]:
        """
        Returns (direction, signal_type) or (None, '') if no signal.
        Evaluates ORB_BREAKOUT → VWAP_BOUNCE → EMA_SCALP in priority order.
        """
        cfg = self.strategy_cfg
        orb_cfg = cfg["orb"]
        vol_mult = cfg["volume"]["confirmation_multiplier"]
        rsi_cfg = cfg.get("rsi", {})
        rsi_min_ce = rsi_cfg.get("min_for_ce", 45)
        rsi_max_pe = rsi_cfg.get("max_for_pe", 55)
        rsi_ob = rsi_cfg.get("overbought", 75)
        rsi_os = rsi_cfg.get("oversold", 25)

        vol_ok_strict = state.volume_ratio >= vol_mult
        vol_ok_soft   = state.volume_ratio >= vol_mult * 0.75

        # ─── ORB BREAKOUT ─────────────────────────────
        if state.orb_range > 0 and vol_ok_strict:
            buffer = max(
                orb_cfg["breakout_buffer_points"],
                state.orb_range * orb_cfg["breakout_buffer_pct"],
            )
            if (spot > state.orb_high + buffer
                    and spot > state.vwap
                    and state.vwap_slope > 0
                    and state.ema_5 > state.ema_13 > state.ema_21
                    and rsi_min_ce <= state.rsi <= rsi_ob):
                return "CE", "ORB_BREAKOUT"

            if (spot < state.orb_low - buffer
                    and spot < state.vwap
                    and state.vwap_slope < 0
                    and state.ema_5 < state.ema_13 < state.ema_21
                    and rsi_os <= state.rsi <= rsi_max_pe):
                return "PE", "ORB_BREAKOUT"

        # ─── VWAP BOUNCE ──────────────────────────────
        if state.vwap > 0 and vol_ok_soft:
            proximity = abs(spot - state.vwap) / state.vwap
            near_vwap = proximity <= 0.0045  # within 0.45% of VWAP

            if (near_vwap
                    and spot > state.vwap
                    and state.vwap_slope > 0
                    and state.ema_5 > state.ema_13 > state.ema_21
                    and rsi_min_ce <= state.rsi <= rsi_ob):
                return "CE", "VWAP_BOUNCE"

            if (near_vwap
                    and spot < state.vwap
                    and state.vwap_slope < 0
                    and state.ema_5 < state.ema_13 < state.ema_21
                    and rsi_os <= state.rsi <= rsi_max_pe):
                return "PE", "VWAP_BOUNCE"

        # ─── EMA SCALP ────────────────────────────────
        if vol_ok_soft and len(state.ema5_above_13) >= 4:
            hist = list(state.ema5_above_13)
            freshly_bullish = hist[-1] and not hist[-4]  # crossed up in last 3 bars
            freshly_bearish = not hist[-1] and hist[-4]  # crossed down in last 3 bars

            if (freshly_bullish
                    and spot > state.vwap
                    and state.vwap_slope > 0
                    and state.ema_5 > state.ema_13 > state.ema_21
                    and rsi_min_ce <= state.rsi <= rsi_ob):
                return "CE", "EMA_SCALP"

            if (freshly_bearish
                    and spot < state.vwap
                    and state.vwap_slope < 0
                    and state.ema_5 < state.ema_13 < state.ema_21
                    and rsi_os <= state.rsi <= rsi_max_pe):
                return "PE", "EMA_SCALP"

        return None, ""

    def _select_option(self, direction: str, spot: float, expiry: date) -> Optional[InstrumentInfo]:
        interval = self.strategy_cfg["options"]["strike_interval"]
        atm = GreeksCalculator.get_atm_strike(spot, interval)
        candidates = [atm, atm + interval] if direction == "CE" else [atm, atm - interval]
        for strike in candidates:
            token = self.instruments.get_option_token("NIFTY", strike, direction, expiry)
            if token:
                return self.instruments.get_instrument(token)
        return None

    def _calculate_lots(self, premium: float) -> int:
        lot_size = self.strategy_cfg["options"]["lot_size"]
        hard_stop_pct = abs(self.strategy_cfg["exits"]["hard_stop_pct"])
        risk_per_lot = premium * hard_stop_pct * lot_size
        if risk_per_lot <= 0:
            return 0
        lots_risk = int((self.capital * self.risk_cfg["per_trade"]["max_risk_pct"]) / risk_per_lot)
        lots_exp  = int((self.capital * self.risk_cfg["per_trade"]["max_premium_exposure_pct"]) / (premium * lot_size))
        return max(0, min(lots_risk, lots_exp, self.risk_cfg["per_trade"]["max_lots"]))

    # ──────────────────────────────────────────────────────────
    # EXIT LOGIC
    # ──────────────────────────────────────────────────────────

    def _check_exit(
        self,
        session_day: date,
        underlying_bar: Candle,
        active: dict,
        state: RunningState,
    ) -> Optional[BacktestTrade | str]:
        """
        Check all exit conditions.
        Returns BacktestTrade on full close, "PARTIAL" on partial exit, None to hold.
        """
        option_bar = active["option_bars"].get(underlying_bar.timestamp)
        if not option_bar or option_bar.timestamp <= active["entry_time"]:
            return None

        cfg = self.strategy_cfg["exits"]
        entry = active["entry_price"]
        qty = active["quantity"]
        cur_price = option_bar.close
        pnl_pct = (cur_price - entry) / entry

        hard_stop = entry * (1 + cfg["hard_stop_pct"])       # e.g. entry * 0.70
        target    = entry * (1 + cfg["profit_target_pct"])   # e.g. entry * 1.45
        trail_act = cfg["trailing_activation_pct"]
        trail_pct = cfg["trailing_stop_pct"]
        be_act    = cfg.get("breakeven_activation_pct", 0.15)
        be_lock   = cfg.get("breakeven_lock_pct", 0.02)
        part_pct  = cfg.get("partial_exit_pct", 0.20)
        part_frac = cfg.get("first_partial_pct", 0.50)

        # 1. HARD STOP
        if option_bar.low <= hard_stop:
            return self._close_trade(active, option_bar.timestamp, hard_stop, "HARD_STOP")

        # 2. TIME STOP
        if underlying_bar.timestamp.time() >= dtime(15, 15):
            return self._close_trade(active, option_bar.timestamp, cur_price, "TIME_STOP")

        # 3. BREAKEVEN STOP (activate once, then act as a floor)
        if not active["breakeven_activated"] and pnl_pct >= be_act:
            active["breakeven_activated"] = True
            active["be_stop"] = entry * (1 + be_lock)
        if active["breakeven_activated"] and not active["trailing_activated"]:
            if option_bar.low <= active.get("be_stop", 0):
                return self._close_trade(active, option_bar.timestamp, active["be_stop"], "BREAKEVEN_STOP")

        # 4. PARTIAL EXIT (once only — book half, reduce qty, keep going)
        if not active["partial_done"] and pnl_pct >= part_pct:
            partial_qty = max(1, int(qty * part_frac))
            partial_qty = min(partial_qty, qty - 1)  # Leave at least 1
            if partial_qty > 0:
                partial_price = apply_sell_slippage(cur_price, self.slippage_bps)
                partial_gross = (partial_price - entry) * partial_qty
                partial_charges = self.expenses.calculate_round_trip(
                    trade_id=f"BT-{active['entry_time'].isoformat()}-P",
                    buy_price=entry,
                    sell_price=partial_price,
                    quantity=partial_qty,
                    session_date=active["session_date"],
                ).total_charges
                active["partial_done"] = True
                active["quantity"] -= partial_qty
                active["partial_booked_pnl"] += partial_gross - partial_charges
                active["partial_booked_charges"] += partial_charges
                logger.debug(
                    "PARTIAL EXIT %s qty=%d @ %.2f net=%.2f remaining=%d",
                    active["symbol"], partial_qty, partial_price,
                    partial_gross - partial_charges, active["quantity"],
                )
                return "PARTIAL"

        # 5. PROFIT TARGET (full)
        if option_bar.high >= target:
            return self._close_trade(active, option_bar.timestamp, target, "PROFIT_TARGET")

        # 6. TRAILING STOP
        if pnl_pct >= trail_act or option_bar.high >= entry * (1 + trail_act):
            active["trailing_activated"] = True
            active["trailing_high"] = max(active["trailing_high"], option_bar.high)
            active["trailing_stop"] = active["trailing_high"] * (1 - trail_pct)

        if active["trailing_activated"] and option_bar.low <= active["trailing_stop"]:
            return self._close_trade(active, option_bar.timestamp, active["trailing_stop"], "TRAILING_STOP")

        # 7. VWAP INVALIDATION
        if active["direction"] == "CE" and underlying_bar.close < state.vwap * 0.9995 and cur_price < entry:
            return self._close_trade(active, option_bar.timestamp, cur_price, "VWAP_INVALIDATION")
        if active["direction"] == "PE" and underlying_bar.close > state.vwap * 1.0005 and cur_price < entry:
            return self._close_trade(active, option_bar.timestamp, cur_price, "VWAP_INVALIDATION")

        return None

    def _close_trade(self, active: dict, exit_time: datetime, raw_exit_price: float, reason: str) -> BacktestTrade:
        exit_price = apply_sell_slippage(raw_exit_price, self.slippage_bps)
        qty = active["quantity"]
        gross = (exit_price - active["entry_price"]) * qty + active.get("partial_booked_pnl", 0.0)
        charges = self.expenses.calculate_round_trip(
            trade_id=f"BT-{active['entry_time'].isoformat()}",
            buy_price=active["entry_price"],
            sell_price=exit_price,
            quantity=qty,
            session_date=active["session_date"],
        ).total_charges + active.get("partial_booked_charges", 0.0)
        net = gross - charges
        pnl_pct = (exit_price - active["entry_price"]) / active["entry_price"]
        return BacktestTrade(
            session_date=active["session_date"].isoformat(),
            symbol=active["symbol"],
            token=active["token"],
            direction=active["direction"],
            strike=active["strike"],
            expiry=active["expiry"].isoformat(),
            signal_time=active["signal_time"].isoformat(),
            entry_time=active["entry_time"].isoformat(),
            exit_time=exit_time.isoformat(),
            entry_price=round(active["entry_price"], 2),
            exit_price=round(exit_price, 2),
            quantity=active["original_quantity"],
            signal_type=active.get("signal_type", "ORB_BREAKOUT"),
            exit_reason=reason,
            gross_pnl=round(gross, 2),
            charges=round(charges, 2),
            net_pnl=round(net, 2),
            pnl_pct=round(pnl_pct, 4),
            underlying_at_signal=round(active["underlying_at_signal"], 2),
            vwap_at_signal=round(active["vwap_at_signal"], 2),
            rsi_at_signal=round(active.get("rsi_at_signal", 50.0), 1),
            volume_ratio=round(active["volume_ratio"], 3),
        )


# ──────────────────────────────────────────────────────────────
# INDICATOR HELPERS
# ──────────────────────────────────────────────────────────────

def update_ema(previous: float, price: float, period: int) -> float:
    if previous <= 0:
        return price
    alpha = 2 / (period + 1)
    return price * alpha + previous * (1 - alpha)


def update_rsi(state: RunningState, price: float) -> None:
    """Wilder's RSI(14) — matches BarBuilder._update_rsi() exactly."""
    if state.rsi_prev_close == 0.0:
        state.rsi_prev_close = price
        return
    change = price - state.rsi_prev_close
    gain = max(0.0, change)
    loss = max(0.0, -change)
    state.rsi_prev_close = price
    state.rsi_warmup += 1

    period = 14
    if state.rsi_warmup <= period:
        state.rsi_avg_gain += gain
        state.rsi_avg_loss += loss
        if state.rsi_warmup == period:
            state.rsi_avg_gain /= period
            state.rsi_avg_loss /= period
            rs = state.rsi_avg_gain / state.rsi_avg_loss if state.rsi_avg_loss > 0 else 100.0
            state.rsi = 100.0 - (100.0 / (1.0 + rs))
    else:
        alpha = 1.0 / period
        state.rsi_avg_gain = state.rsi_avg_gain * (1 - alpha) + gain * alpha
        state.rsi_avg_loss = state.rsi_avg_loss * (1 - alpha) + loss * alpha
        rs = state.rsi_avg_gain / state.rsi_avg_loss if state.rsi_avg_loss > 0 else 100.0
        state.rsi = 100.0 - (100.0 / (1.0 + rs))


def in_entry_window(t: dtime) -> bool:
    return (
        dtime(9, 35) <= t <= dtime(10, 45) or
        dtime(11, 0) <= t <= dtime(11, 30) or
        dtime(13, 0) <= t <= dtime(14, 45)
    )


def classify_regime(cfg: dict, india_vix: float, prev_high: float, prev_low: float,
                    prev_close: float, today_open: float, today_is_tuesday: bool) -> str:
    pivot = (prev_high + prev_low + prev_close) / 3
    bc = (prev_high + prev_low) / 2
    tc = (pivot - bc) + pivot
    cpr_width = abs(tc - bc)
    gap_pct = ((today_open - prev_close) / prev_close) * 100 if prev_close else 0.0
    if india_vix > cfg["regime"]["vix_max_reduced"]:
        return "NO_TRADE"
    if abs(gap_pct) > cfg["regime"]["gap_shock_threshold"]:
        return "NO_TRADE"
    if cpr_width > cfg["regime"]["cpr_wide_threshold"]:
        return "NO_TRADE"
    if today_is_tuesday and india_vix < 22:
        return "GAMMA_MODE"
    if cpr_width < cfg["regime"]["cpr_narrow_threshold"]:
        return "TREND_DAY_BIAS"
    return "NEUTRAL"


# ──────────────────────────────────────────────────────────────
# FILE I/O HELPERS
# ──────────────────────────────────────────────────────────────

def parse_smartapi_candles(rows: Iterable[list]) -> List[Candle]:
    candles: List[Candle] = []
    for row in rows or []:
        ts = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00")).replace(tzinfo=None)
        candles.append(Candle(ts, float(row[1]), float(row[2]), float(row[3]), float(row[4]), int(row[5] or 0)))
    return sorted(candles, key=lambda c: c.timestamp)


def read_candles(path: Path) -> List[Candle]:
    with path.open(newline="") as f:
        return [
            Candle(
                timestamp=datetime.fromisoformat(r["timestamp"]),
                open=float(r["open"]), high=float(r["high"]),
                low=float(r["low"]), close=float(r["close"]),
                volume=int(float(r["volume"])),
            )
            for r in csv.DictReader(f)
        ]


def write_candles(path: Path, candles: List[Candle]) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["timestamp", "open", "high", "low", "close", "volume"])
        w.writeheader()
        for c in candles:
            w.writerow({"timestamp": c.timestamp.isoformat(), "open": c.open,
                        "high": c.high, "low": c.low, "close": c.close, "volume": c.volume})


def group_by_day(candles: List[Candle]) -> Dict[date, List[Candle]]:
    grouped: Dict[date, List[Candle]] = {}
    for c in candles:
        grouped.setdefault(c.timestamp.date(), []).append(c)
    return grouped


def build_previous_daily_map(daily_candles: List[Candle]) -> Dict[date, Candle]:
    ordered = sorted(daily_candles, key=lambda c: c.timestamp.date())
    result: Dict[date, Candle] = {}
    previous: Optional[Candle] = None
    for candle in ordered:
        if previous is not None:
            result[candle.timestamp.date()] = previous
        previous = candle
    return result


def only_market_minutes(candles: List[Candle]) -> List[Candle]:
    return [c for c in candles if MARKET_OPEN <= c.timestamp.time() <= MARKET_CLOSE]


def apply_buy_slippage(price: float, bps: float) -> float:
    return max(0.05, price * (1 + bps / 10_000))


def apply_sell_slippage(price: float, bps: float) -> float:
    return max(0.05, price * (1 - bps / 10_000))


def write_trades(path: Path, trades: List[BacktestTrade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        fieldnames = list(asdict(trades[0]).keys()) if trades else list(BacktestTrade.__dataclass_fields__.keys())
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for trade in trades:
            w.writerow(asdict(trade))


def summarize(trades: List[BacktestTrade]) -> dict:
    gross = sum(t.gross_pnl for t in trades)
    charges = sum(t.charges for t in trades)
    net = sum(t.net_pnl for t in trades)
    wins = sum(1 for t in trades if t.net_pnl > 0)
    by_type: Dict[str, dict] = {}
    for t in trades:
        st = t.signal_type
        e = by_type.setdefault(st, {"trades": 0, "wins": 0, "net_pnl": 0.0})
        e["trades"] += 1
        e["wins"] += 1 if t.net_pnl > 0 else 0
        e["net_pnl"] = round(e["net_pnl"] + t.net_pnl, 2)
    return {
        "trades": len(trades),
        "wins": wins,
        "losses": len(trades) - wins,
        "win_rate_pct": round((wins / len(trades) * 100) if trades else 0.0, 2),
        "gross_pnl": round(gross, 2),
        "charges": round(charges, 2),
        "net_pnl": round(net, 2),
        "avg_net_per_trade": round((net / len(trades)) if trades else 0.0, 2),
        "by_signal_type": by_type,
    }


def load_config() -> dict:
    def read(name: str) -> dict:
        with (ROOT / "config" / name).open() as f:
            return yaml.safe_load(f) or {}
    return {
        "credentials": read("credentials.yaml"),
        "strategy": read("strategy_config.yaml")["strategy"],
        "risk": read("risk_config.yaml")["risk"],
        "expense": read("expense_config.yaml"),
        "system": read("system_config.yaml")["system"],
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run AVCS v2 scalp backtest")
    parser.add_argument("--from", dest="start", required=True)
    parser.add_argument("--to", dest="end", required=True)
    parser.add_argument("--capital", type=float, default=100000)
    parser.add_argument("--slippage-bps", type=float, default=10.0)
    parser.add_argument("--refresh-cache", action="store_true")
    parser.add_argument("--output", default="data/backtests/avcs_backtest_trades.csv")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--historical-delay-sec", type=float, default=2.0)
    parser.add_argument("--api-rps", type=float, default=0.5)
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    cfg = load_config()

    auth = AngelAuthManager(cfg["credentials"]["angel_one"])
    await auth.initialize()
    try:
        rate_limiter = RateLimiter(args.api_rps)
        connector = AngelConnector(auth, rate_limiter)
        instruments = InstrumentManager()
        await instruments.initialize()
        store = CandleStore(connector, ROOT / "data" / "historical",
                            refresh=args.refresh_cache,
                            fetch_delay_sec=args.historical_delay_sec)
        runner = AVCSBacktester(
            strategy_cfg=cfg["strategy"],
            risk_cfg=cfg["risk"],
            expense_cfg=cfg["expense"],
            instruments=instruments,
            candles=store,
            capital=args.capital,
            slippage_bps=args.slippage_bps,
        )
        trades = await runner.run(start, end)
        output = ROOT / args.output
        write_trades(output, trades)
        summary = summarize(trades)
        summary_path = output.with_suffix(".summary.json")
        summary_path.write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary, indent=2))
        print(f"\nTrades CSV : {output}")
        print(f"Summary    : {summary_path}")
    finally:
        await auth.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
