"""
NIFTY50 Options Scalping System — Main Orchestrator
=====================================================
Entry point for the paper trading system.

Startup sequence:
1. Load configuration
2. Initialize all modules
3. Authenticate with Angel One
4. Connect WebSocket
5. Register instruments
6. Start health monitor
7. Run trading loop (session manager)
8. Handle graceful shutdown

Failure modes:
- If auth fails: abort (cannot trade without valid session)
- If WebSocket fails on startup: abort with alert
- If WebSocket fails mid-session: reconnect loop, halt entries
- If kill switch file appears: immediate stop

Usage:
    python main.py                    # Paper trading mode (default)
    python main.py --mode paper       # Explicit paper mode
    KILL_SWITCH: touch /tmp/algo_kill_switch
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import logging.handlers
import os
import signal
import sys
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Optional

import yaml

from core.auth.angel_auth import AngelAuthManager
from core.broker.angel_connector import AngelConnector
from core.broker.websocket_handler import WebSocketHandler
from core.broker.rate_limiter import RateLimiter
from core.data.market_data_engine import MarketDataEngine, OptionQuote
from core.data.greeks_calculator import GreeksCalculator
from core.data.instrument_manager import InstrumentManager
from core.data.option_chain_manager import OptionChainManager
from core.broker.websocket_handler import EXCHANGE_TYPE_NSE_CM, EXCHANGE_TYPE_NSE_FO
from core.strategy.signal_engine import SignalEngine
from core.strategy.avcs_strategy import AVCSStrategy
from core.strategy.base_strategy import BaseStrategy
from core.strategy.strategy_manager import StrategyManager
from core.strategy.execution_selector import ExecutionSelector
from core.risk.portfolio_risk import PortfolioRisk
from core.strategy.strategies.liquidity_sweep import LiquiditySweepReversal
from core.strategy.strategies.false_breakout import FalseBreakoutTrap
from core.strategy.strategies.bos_retest import BOSRetest
from core.strategy.strategies.fair_value_gap import FairValueGap
from core.strategy.strategies.ema_trend_ride import EMATrendRide
from core.strategy.strategies.vwap_mean_reversion import VWAPMeanReversion
from core.strategy.strategies.bollinger_reversion import BollingerReversion
from core.strategy.strategies.gamma_pinning import GammaPinning
from core.strategy.strategies.daily_momentum_drive import DailyMomentumDrive
from core.execution.paper_simulator import PaperSimulator, SimulationRealism
from core.execution.order_manager import OrderManager, TradeRecord
from core.expenses.expense_engine import ExpenseEngine
from core.risk.risk_engine import RiskEngine
from core.journal.trade_journal import TradeJournal
from analytics.analytics_engine import AnalyticsEngine
from alerts.telegram_alerts import TelegramAlerts
from monitoring.health_monitor import HealthMonitor
from dashboard.app import create_dashboard_app

# ─── Logging Setup ─────────────────────────────────────────

def setup_logging(log_dir: str, level: str = "INFO") -> None:
    """Configure structured logging with file rotation."""
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log_level = getattr(logging, level.upper(), logging.INFO)

    fmt = "%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Console handler
    console_h = logging.StreamHandler(sys.stdout)
    console_h.setLevel(log_level)
    console_h.setFormatter(logging.Formatter(fmt, datefmt))
    root_logger.addHandler(console_h)

    # Main trading log (rotating)
    trading_h = logging.handlers.RotatingFileHandler(
        f"{log_dir}/trading.log", maxBytes=50*1024*1024, backupCount=10
    )
    trading_h.setLevel(log_level)
    trading_h.setFormatter(logging.Formatter(fmt, datefmt))
    root_logger.addHandler(trading_h)

    # Error log
    error_h = logging.handlers.RotatingFileHandler(
        f"{log_dir}/errors.log", maxBytes=10*1024*1024, backupCount=5
    )
    error_h.setLevel(logging.ERROR)
    error_h.setFormatter(logging.Formatter(fmt, datefmt))
    root_logger.addHandler(error_h)

    # Journal log (separate)
    journal_h = logging.handlers.RotatingFileHandler(
        f"{log_dir}/journal.log", maxBytes=20*1024*1024, backupCount=20
    )
    journal_h.setLevel(logging.DEBUG)
    journal_h.setFormatter(logging.Formatter(fmt, datefmt))
    logging.getLogger("journal").addHandler(journal_h)
    logging.getLogger("journal").propagate = False

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("websocket").setLevel(logging.WARNING)


# ─── Config Loading ─────────────────────────────────────────

def load_config(config_dir: str) -> dict:
    """Load all configuration files into a single dict."""

    def load_yaml(filename: str) -> dict:
        path = Path(config_dir) / filename
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        with open(path) as f:
            return yaml.safe_load(f) or {}

    full_strategy = load_yaml("strategy_config.yaml")
    return {
        "credentials": load_yaml("credentials.yaml"),
        "strategy": full_strategy["strategy"],
        "strategies": full_strategy.get("strategies", {}),
        "risk": load_yaml("risk_config.yaml")["risk"],
        "expense": load_yaml("expense_config.yaml"),
        "system": load_yaml("system_config.yaml")["system"],
        "events": load_yaml("event_calendar.yaml"),
    }


def validate_config(config: dict) -> None:
    """Validate critical config values before startup."""
    creds = config["credentials"]["angel_one"]
    if creds["api_key"] == "YOUR_API_KEY":
        raise ValueError(
            "credentials.yaml not configured. "
            "Fill in api_key, client_id, client_password, totp_secret."
        )

    mode = config["strategy"]["mode"]
    if mode not in ("PAPER", "LIVE"):
        raise ValueError(f"Invalid strategy mode: {mode}. Must be PAPER or LIVE.")

    if mode == "LIVE":
        raise RuntimeError(
            "LIVE mode is not enabled in this version. "
            "Complete Phase 4 backtesting validation first."
        )


# ─── Trading Session Manager ─────────────────────────────────

logger = logging.getLogger(__name__)


class TradingSession:
    """
    Manages a single trading day session.

    Lifecycle:
    1. pre_session(): Regime classification, instrument registration
    2. run_session(): Main trading loop (9:15 → 15:30)
    3. post_session(): P&L summary, cleanup
    """

    # NSE NIFTY and VIX tokens (update if changed by NSE)
    NIFTY_TOKEN = "99926000"       # NIFTY 50 index token
    NIFTY_VIX_TOKEN = "99926002"   # India VIX token
    NIFTY_FUT_TOKEN = "99926009"   # NIFTY nearest future (for reference price)
    NFO_EXCHANGE = "NFO"
    NSE_EXCHANGE = "NSE"

    # Session timing
    MARKET_OPEN = dtime(9, 15)
    MARKET_CLOSE = dtime(15, 30)
    PRE_MARKET_PREP = dtime(9, 10)
    HARD_CLOSE = dtime(15, 15)
    ORB_READY = dtime(9, 35)
    EOD_SUMMARY = dtime(15, 45)

    def __init__(
        self,
        config: dict,
        auth: AngelAuthManager,
        connector: AngelConnector,
        ws_handler: WebSocketHandler,
        market_data: MarketDataEngine,
        instrument_manager: InstrumentManager,
        signal_engine: SignalEngine,
        avcs_strategy: AVCSStrategy,
        strategy_manager: StrategyManager,
        order_manager: OrderManager,
        risk_engine: RiskEngine,
        journal: TradeJournal,
        analytics: AnalyticsEngine,
        alerts: TelegramAlerts,
        health: HealthMonitor,
        account_capital: float,
        underlying_token: str,
        underlying_symbol: str,
        underlying_exchange: str = "NFO",
    ) -> None:
        self._cfg = config
        self._auth = auth
        self._connector = connector
        self._ws = ws_handler
        self._md = market_data
        self._instruments = instrument_manager
        self._signal_engine = signal_engine
        self._avcs = avcs_strategy           # Direct ref for ORB/regime callbacks
        self._strategy_manager = strategy_manager
        self._orders = order_manager
        self._risk = risk_engine
        self._journal = journal
        self._analytics = analytics
        self._alerts = alerts
        self._health = health
        self._capital = account_capital
        self._underlying_token = underlying_token
        self._underlying_symbol = underlying_symbol
        self._underlying_exchange = underlying_exchange

        self._session_date: Optional[date] = None
        self._current_expiry: Optional[date] = None
        self._option_chain: Optional[OptionChainManager] = None
        self._running = False
        self._csv_path: Optional[Path] = None

    async def run(self) -> None:
        """Main session loop. Runs for one trading day."""
        self._session_date = date.today()
        self._running = True

        logger.info(f"{'='*60}")
        logger.info(f"SESSION START: {self._session_date}")
        logger.info(f"{'='*60}")

        try:
            # Check if today is a no-trade day
            if self._is_no_trade_day(self._session_date):
                logger.info(f"Calendar no-trade day: {self._session_date} — system idle")
                await self._alerts.send(
                    f"🚫 <b>NO TRADE DAY</b> — {self._session_date.strftime('%d %b %Y')}\n"
                    f"System idle. No trading today."
                )
                return

            # Initialise live trade CSV for today
            self._init_trade_csv()

            # ── Pre-market idle wait ────────────────────────────────────────
            # If started before 9:10 AM, wait until PRE_MARKET_PREP time.
            # This prevents regime classification, option subscriptions, and
            # strategy signals from firing on pre-market or garbage data.
            now = datetime.now().time()
            if now < self.PRE_MARKET_PREP:
                wait_secs = (
                    (self.PRE_MARKET_PREP.hour * 3600 + self.PRE_MARKET_PREP.minute * 60)
                    - (now.hour * 3600 + now.minute * 60 + now.second)
                )
                wait_str = f"{self.PRE_MARKET_PREP.strftime('%H:%M')}"
                logger.info(
                    f"Pre-market: started at {now.strftime('%H:%M:%S')}, "
                    f"waiting {wait_secs // 60}m {wait_secs % 60}s until {wait_str}"
                )
                await self._alerts.send_startup(
                    session_date=self._session_date,
                    mode=self._cfg["strategy"]["mode"],
                    capital=self._capital,
                    strategies=len(self._strategy_manager._strategies),
                    waiting_until=f"{wait_str} AM",
                )
                while self._running and datetime.now().time() < self.PRE_MARKET_PREP:
                    await asyncio.sleep(10)
            else:
                # Started after 9:10 — send startup alert immediately
                await self._alerts.send_startup(
                    session_date=self._session_date,
                    mode=self._cfg["strategy"]["mode"],
                    capital=self._capital,
                    strategies=len(self._strategy_manager._strategies),
                )

            if not self._running:
                return

            # Initialize session
            self._avcs.start_session(self._session_date, self._capital)
            self._avcs.set_expiry(self._find_weekly_expiry())
            self._strategy_manager.reset_daily()
            self._risk.reset_daily_state()
            self._signal_engine.reset_session()
            self._md.reset_session()

            # Warm up indicators with prior session + today's bars
            await _seed_indicators_today(
                self._connector, self._md,
                self._underlying_token, self._underlying_symbol,
            )

            # Restore today's trades from CSV (handles mid-session restarts)
            self._restore_trades_from_csv()

            # Find weekly expiry
            self._current_expiry = self._find_weekly_expiry()
            logger.info(f"Weekly expiry: {self._current_expiry}")

            # Pre-session preparation
            await self._pre_session_prep()

            # Main trading loop
            await self._trading_loop()

            # Post session
            await self._post_session()

        except asyncio.CancelledError:
            logger.info("Session cancelled — cleaning up")
            await self._emergency_close_all("Session cancelled")
            raise
        except Exception as e:
            logger.critical(f"Session error: {e}", exc_info=True)
            await self._emergency_close_all(f"Session error: {e}")
            await self._alerts.send_risk_alert("SESSION_ERROR", str(e))
        finally:
            # Stop option chain poller if running
            if self._option_chain:
                await self._option_chain.stop()

    async def _pre_session_prep(self) -> None:
        """
        Pre-session setup at 9:10 AM.
        Classify regime, subscribe to instruments.
        """
        logger.info("Pre-session preparation started")
        # NOTE: NIFTY futures and VIX are already subscribed at startup (main()).
        # _pre_session_prep only waits for data to arrive then classifies regime.

        # Wait for initial data (up to 60 seconds)
        for _ in range(60):
            if self._md.get_spot_price():
                break
            await asyncio.sleep(1)

        # Fetch previous day OHLC for CPR
        prev_ohlc = await self._fetch_prev_day_ohlc()
        today_open = self._md.get_spot_price() or 0.0
        india_vix = self._md.get_india_vix() or 15.0

        is_tuesday = self._session_date.weekday() == 1

        # Classify regime
        regime = self._signal_engine.classify_regime(
            india_vix=india_vix,
            prev_high=prev_ohlc.get("high", today_open * 1.01),
            prev_low=prev_ohlc.get("low", today_open * 0.99),
            prev_close=prev_ohlc.get("close", today_open),
            today_open=today_open,
            today_is_tuesday=is_tuesday,
        )

        self._avcs.on_regime_classified(regime)

        # Send morning prep alert
        await self._alerts.send_morning_prep(
            session_date=self._session_date,
            regime=regime.regime.value,
            india_vix=india_vix,
            cpr_width=regime.cpr_width,
            gap_pct=regime.gap_size_pct * (1 if regime.gap_direction == "UP" else -1),
        )

        # Subscribe to option chain for current expiry (if trading day)
        # Always subscribe the option chain so regime_exempt strategies
        # (e.g. DailyMomentumDrive in evaluation mode) get live quotes.
        await self._subscribe_option_chain()

        logger.info(f"Pre-session complete: regime={regime.regime.value}")

    async def _trading_loop(self) -> None:
        """Main trading loop — processes 1-min bar closes."""
        logger.info("Trading loop started")

        # Register ORB callback — notifies AVCS when ORB is locked
        orb_locked = False

        def on_new_bar(token: str, bar) -> None:
            nonlocal orb_locked
            if token != self._underlying_token:
                return
            current_time = bar.bar_time.time()
            if not orb_locked and current_time >= self.ORB_READY:
                if self._md.is_orb_ready():
                    orb_locked = True
                    self._avcs.on_orb_locked(
                        self._md.orb_high,
                        self._md.orb_low,
                        self._md.orb_range,
                    )

        self._md.on_new_bar(on_new_bar)

        while self._running:
            await asyncio.sleep(1)
            current_time = datetime.now().time()

            # Do nothing before market opens — avoids stale pre-open ticks
            if current_time < self.MARKET_OPEN:
                continue

            if current_time >= self.HARD_CLOSE:
                logger.info("Hard close time reached — squaring off all positions")
                await self._emergency_close_all("Intraday hard close 15:15")
                break

            if current_time >= self.MARKET_CLOSE:
                break

            if not self._risk.is_trading_permitted:
                continue

            market_state = self._md.get_market_state()
            if not market_state or not market_state.is_data_fresh:
                continue

            # Check exits for ALL open strategy positions
            option_prices = self._collect_option_prices()
            option_chain_snapshot = self._option_chain.latest_snapshot if self._option_chain else None
            exit_instructions = self._strategy_manager.check_exits(market_state, option_prices)
            for exit_instr in exit_instructions:
                await self._execute_exit_instruction(exit_instr, market_state)

            # Evaluate entries. On NO_TRADE regime days, only regime_exempt
            # strategies (e.g. DailyMomentumDrive in evaluation mode) are
            # allowed through — all others are silently skipped.
            regime = self._signal_engine._regime
            regime_is_no_trade = regime is not None and regime.regime.value == "NO_TRADE"

            entry_instructions = self._strategy_manager.on_market_update(
                market_state=market_state,
                option_chain=option_chain_snapshot,
                instrument_mgr=self._instruments,
                market_data_engine=self._md,
                expiry=self._current_expiry,
                regime_no_trade=regime_is_no_trade,
            )
            for entry_instr in entry_instructions:
                await self._execute_entry_instruction(entry_instr, market_state)

    def _collect_option_prices(self) -> dict:
        """Return {token: ltp} for all tracked option tokens."""
        return self._md.get_all_option_prices()

    async def _execute_exit_instruction(self, exit_instr, market_state) -> None:
        """Execute an exit from StrategyManager.check_exits()."""
        trade = self._orders.get_trade_for_strategy(exit_instr.strategy_name)
        if not trade:
            return

        current_price = exit_instr.current_option_price or trade.entry_price

        if exit_instr.decision.action == "PARTIAL" and exit_instr.decision.partial_quantity:
            partial_fill = await self._orders.execute_partial_exit(
                trade_id=trade.trade_id,
                current_price=current_price,
                partial_quantity=exit_instr.decision.partial_quantity,
                exit_reason=exit_instr.decision.reason,
            )
            if partial_fill:
                self._strategy_manager.on_partial_exit_filled(
                    exit_instr.strategy_name, partial_fill, exit_instr.decision.partial_quantity
                )
            return

        closed = await self._orders.execute_exit(
            trade_id=trade.trade_id,
            current_price=current_price,
            exit_reason=exit_instr.decision.reason,
            spot_price=market_state.spot_price,
        )
        if closed:
            self._strategy_manager.on_exit_filled(
                strategy_name=exit_instr.strategy_name,
                fill_price=closed.exit_price,
                pnl=closed.net_pnl,
                exit_reason=closed.exit_reason,
            )
            self._on_trade_closed(closed)

    async def _execute_entry_instruction(self, entry_instr, market_state) -> None:
        """Execute an entry from StrategyManager.on_market_update()."""
        from core.strategy.strategy_manager import EntryInstruction
        instr: EntryInstruction = entry_instr
        signal = instr.signal
        instrument = instr.instrument

        quote = self._md.get_option_quote(instrument.token)
        limit_price = quote.ask if quote and quote.ask > 0 else (instrument.last_price or 0.0)
        if limit_price <= 0:
            # WebSocket quote not yet arrived — fetch LTP directly from broker
            ltp = await self._connector.get_ltp(
                exchange="NFO",
                symbol=instrument.symbol,
                token=instrument.token,
            )
            if ltp and ltp > 0:
                limit_price = ltp
                logger.info(f"Using broker LTP for {instrument.symbol}: ₹{ltp:.2f}")
            else:
                logger.warning(f"No valid price for {instrument.symbol} — skipping entry")
                self._strategy_manager.on_entry_failed(signal.strategy_name)
                return

        risk_result = self._risk.check_pre_trade(
            premium=limit_price,
            quantity=instr.quantity,
            option_oi=quote.oi if quote else 0,
            option_spread=quote.spread if quote else 0,
            option_mid_price=quote.mid_price if quote else limit_price,
            india_vix=market_state.india_vix,
            has_open_position=self._orders.has_position_for_strategy(signal.strategy_name),
            is_data_fresh=market_state.is_data_fresh,
        )

        if not risk_result.approved:
            logger.warning(
                f"Risk blocked {signal.strategy_name} entry: {risk_result.rejection_summary}"
            )
            self._strategy_manager.on_entry_failed(signal.strategy_name)
            return

        trade = await self._orders.execute_entry(
            symbol=instrument.symbol,
            token=instrument.token,
            option_type=instrument.option_type,
            strike=instrument.strike,
            expiry=self._current_expiry,
            lots=instr.lots,
            quantity=instr.quantity,
            limit_price=limit_price,
            spot_price=market_state.spot_price,
            vwap=market_state.vwap,
            signal_id=str(signal.meta.get("signal_id", "")),
            signal_reason=signal.signal_type,
            iv=instrument.iv or 0.0,
            delta=instrument.delta or 0.0,
            oi=quote.oi if quote else 0,
            strategy_name=signal.strategy_name,
        )

        if trade:
            self._strategy_manager.on_entry_filled(
                strategy_name=signal.strategy_name,
                trade_id=trade.trade_id,
                fill_price=trade.entry_price,
                quantity=trade.quantity,
                option_symbol=trade.symbol,
                option_token=trade.token,
                stop_price=signal.stop_price,
                target_price=signal.target_price,
            )
            self._journal.log_trade_entry(trade)
            self._append_trade_csv(trade, "ENTRY")
            logger.info(
                f"[{signal.strategy_name}] Entry filled: {trade.symbol} "
                f"@ ₹{trade.entry_price:.2f} x {trade.lots}L"
            )
            exits_cfg = self._cfg["strategy"].get("exits", {})
            await self._alerts.send_trade_entry(
                symbol=trade.symbol,
                option_type=trade.option_type,
                strike=trade.strike,
                lots=trade.lots,
                quantity=trade.quantity,
                entry_price=trade.entry_price,
                hard_stop=signal.stop_price,
                target=signal.target_price,
                signal_reason=signal.signal_type,
                strategy_name=signal.strategy_name,
                spot_price=market_state.spot_price,
                premium_stop_pct=exits_cfg.get("hard_stop_pct", -0.30),
                premium_target_pct=exits_cfg.get("profit_target_pct", 0.45),
            )


    def _on_trade_closed(self, trade: TradeRecord) -> None:
        """Handle trade close event."""
        self._journal.log_trade_exit(trade)
        self._risk.update_trade_result(trade.net_pnl, trade.net_pnl > 0)
        self._append_trade_csv(trade, "EXIT")
        # AVCS-specific callback (for session state tracking)
        if trade.strategy_name == "AVCS":
            self._avcs.on_trade_closed(trade.net_pnl, trade.net_pnl > 0)

        hold_min = (
            (trade.exit_time - trade.entry_time).total_seconds() / 60
            if trade.exit_time and trade.entry_time else 0
        )
        asyncio.create_task(self._alerts.send_trade_exit(
            symbol=trade.symbol,
            exit_price=trade.exit_price,
            entry_price=trade.entry_price,
            gross_pnl=trade.gross_pnl,
            net_pnl=trade.net_pnl,
            total_charges=trade.total_charges,
            exit_reason=trade.exit_reason,
            pnl_pct=trade.pnl_pct,
            hold_time_min=hold_min,
            strategy_name=trade.strategy_name,
        ))

    # ── CSV trade logging ─────────────────────────────────────────────────────

    _CSV_FIELDS = [
        "event_type", "timestamp", "trade_id", "strategy",
        "symbol", "option_type", "strike", "expiry", "lots", "quantity",
        "entry_time", "entry_price", "entry_spot",
        "exit_time", "exit_price",
        "gross_pnl", "charges", "net_pnl", "pnl_pct",
        "exit_reason", "hold_min", "signal_reason",
    ]

    def _init_trade_csv(self) -> None:
        """Create (or open) today's live trade CSV file."""
        csv_dir = Path("data/live_trades")
        csv_dir.mkdir(parents=True, exist_ok=True)
        self._csv_path = csv_dir / f"{self._session_date}_live_trades.csv"
        if not self._csv_path.exists():
            with open(self._csv_path, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=self._CSV_FIELDS).writeheader()
        logger.info(f"Live trade CSV: {self._csv_path}")

    def _append_trade_csv(self, trade: TradeRecord, event_type: str) -> None:
        """Append one row to today's live trade CSV. Never raises — failures are logged."""
        if not self._csv_path:
            return
        try:
            hold_min = ""
            if event_type == "EXIT" and trade.exit_time and trade.entry_time:
                hold_min = f"{(trade.exit_time - trade.entry_time).total_seconds() / 60:.1f}"
            row = {
                "event_type":   event_type,
                "timestamp":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "trade_id":     trade.trade_id,
                "strategy":     trade.strategy_name,
                "symbol":       trade.symbol,
                "option_type":  trade.option_type,
                "strike":       trade.strike,
                "expiry":       str(trade.expiry),
                "lots":         trade.lots,
                "quantity":     trade.quantity,
                "entry_time":   trade.entry_time.strftime("%H:%M:%S") if trade.entry_time else "",
                "entry_price":  f"{trade.entry_price:.2f}",
                "entry_spot":   f"{trade.entry_spot:.2f}",
                "exit_time":    trade.exit_time.strftime("%H:%M:%S") if trade.exit_time else "",
                "exit_price":   f"{trade.exit_price:.2f}" if event_type == "EXIT" else "",
                "gross_pnl":    f"{trade.gross_pnl:.2f}" if event_type == "EXIT" else "",
                "charges":      f"{trade.total_charges:.2f}" if event_type == "EXIT" else "",
                "net_pnl":      f"{trade.net_pnl:.2f}" if event_type == "EXIT" else "",
                "pnl_pct":      f"{trade.pnl_pct * 100:.1f}" if event_type == "EXIT" else "",
                "exit_reason":  trade.exit_reason if event_type == "EXIT" else "",
                "hold_min":     hold_min,
                "signal_reason": trade.signal_reason,
            }
            with open(self._csv_path, "a", newline="") as f:
                csv.DictWriter(f, fieldnames=self._CSV_FIELDS).writerow(row)
        except Exception as e:
            logger.error(f"CSV write failed (non-critical): {e}")

    def _restore_trades_from_csv(self) -> None:
        """
        On startup, read today's trade CSV and restore session state:
        - Closed trades → RiskEngine daily P&L + strategy win/loss counters
        - Open trades (ENTRY with no EXIT) → OrderManager open positions +
          strategy ACTIVE_POSITION state so exits are managed correctly.
        """
        if not self._csv_path or not self._csv_path.exists():
            return

        try:
            entries: dict[str, dict] = {}
            exits: dict[str, dict] = {}

            with open(self._csv_path, newline="") as f:
                for row in csv.DictReader(f):
                    tid = row["trade_id"]
                    if row["event_type"] == "ENTRY":
                        entries[tid] = row
                    elif row["event_type"] == "EXIT":
                        exits[tid] = row

            restored_closed = 0
            restored_open = 0

            for tid, entry in entries.items():
                strategy_name = entry["strategy"]
                if tid in exits:
                    # Closed trade — restore stats only
                    ex = exits[tid]
                    net_pnl = float(ex["net_pnl"] or 0)
                    self._risk.update_trade_result(net_pnl, won=net_pnl >= 0)
                    self._strategy_manager.restore_closed_trade(
                        strategy_name=strategy_name,
                        net_pnl=net_pnl,
                    )
                    restored_closed += 1
                else:
                    # Open trade — reconstruct TradeRecord and register with OrderManager
                    try:
                        expiry = date.fromisoformat(entry["expiry"]) if entry.get("expiry") else date.today()
                        trade = TradeRecord(
                            trade_id=tid,
                            session_date=self._session_date,
                            symbol=entry["symbol"],
                            token="",  # not critical for paper management
                            option_type=entry["option_type"],
                            strike=int(entry["strike"] or 0),
                            expiry=expiry,
                            lots=int(entry["lots"] or 1),
                            quantity=int(entry["quantity"] or 75),
                            entry_time=datetime.fromisoformat(entry["entry_time"]) if entry.get("entry_time") else datetime.now(),
                            entry_price=float(entry["entry_price"] or 0),
                            entry_spot=float(entry["entry_spot"] or 0),
                            signal_reason=entry.get("signal_reason", ""),
                            strategy_name=strategy_name,
                            status="OPEN",
                        )
                        self._orders.restore_open_trade(trade)
                        # Put strategy into ACTIVE_POSITION so it manages the exit
                        self._strategy_manager.restore_open_position(
                            strategy_name=strategy_name,
                            trade=trade,
                        )
                        restored_open += 1
                    except Exception as e:
                        logger.warning(f"Could not restore open trade {tid}: {e}")

            if restored_closed or restored_open:
                logger.info(
                    f"Restored from CSV: {restored_closed} closed trades, "
                    f"{restored_open} open positions"
                )
        except Exception as e:
            logger.error(f"Trade CSV restore failed (non-critical): {e}")

    async def _post_session(self) -> None:
        """End-of-session cleanup and reporting."""
        logger.info("Post-session reporting started")

        closed = self._orders.closed_trades
        today_trades = [t for t in closed if t.session_date == self._session_date]

        wins = sum(1 for t in today_trades if t.net_pnl > 0)
        gross = sum(t.gross_pnl for t in today_trades)
        net = sum(t.net_pnl for t in today_trades)
        charges = sum(t.total_charges for t in today_trades)

        # Log daily summary
        regime = self._signal_engine._regime
        self._journal.log_daily_summary(
            session_date=self._session_date,
            regime=regime.regime.value if regime else "UNKNOWN",
            india_vix=self._md.get_india_vix() or 0.0,
            trades=len(today_trades),
            wins=wins,
            gross_pnl=gross,
            net_pnl=net,
            total_charges=charges,
        )

        # Send Telegram daily summary
        await self._alerts.send_daily_summary(
            session_date=self._session_date,
            trades=len(today_trades),
            wins=wins,
            gross_pnl=gross,
            net_pnl=net,
            total_charges=charges,
            regime=regime.regime.value if regime else "UNKNOWN",
            equity=self._capital + net,
        )

        # Print analytics
        if today_trades:
            metrics = self._analytics.compute(today_trades)
            print(self._analytics.format_report(metrics))

    async def _emergency_close_all(self, reason: str) -> None:
        """Immediately close ALL open strategy positions."""
        open_trades = list(self._orders.open_trades.values())
        if not open_trades:
            return
        logger.warning(f"Emergency close ({len(open_trades)} positions): {reason}")
        for open_trade in open_trades:
            quote = self._md.get_option_quote(open_trade.token)
            price = quote.ltp if quote else open_trade.entry_price
            closed = await self._orders.execute_exit(
                trade_id=open_trade.trade_id,
                current_price=price,
                exit_reason=f"EMERGENCY: {reason}",
            )
            if closed:
                if closed.strategy_name:
                    self._strategy_manager.on_exit_filled(
                        closed.strategy_name, closed.exit_price,
                        closed.net_pnl, closed.exit_reason,
                    )
                self._on_trade_closed(closed)

    async def _fetch_prev_day_ohlc(self) -> dict:
        """
        Fetch previous trading day's OHLC for CPR calculation.

        CPR requires yesterday's high, low, and close — not today's data.
        We fetch a 5-day window of ONE_DAY candles and take the last closed
        candle (i.e. index [-1] since today hasn't closed yet, or the last
        entry if called before market open).

        Angel One historical data date strings must be "YYYY-MM-DD HH:MM"
        format. For ONE_DAY candles the time component is ignored.
        """
        try:
            from datetime import timedelta as _td

            # Find previous trading day (skip weekends)
            ref = date.today()
            prev_td = ref - _td(days=1)
            while prev_td.weekday() >= 5:  # 5=Saturday, 6=Sunday
                prev_td -= _td(days=1)

            # Fetch a 7-calendar-day window to guarantee we get at least 2 trading days
            window_start = prev_td - _td(days=7)

            result = await self._connector.get_historical_data(
                token=self._underlying_token,
                exchange=self._underlying_exchange,
                symbol=self._underlying_symbol,
                interval="ONE_DAY",
                from_date=f"{window_start.strftime('%Y-%m-%d')} 09:00",
                to_date=f"{prev_td.strftime('%Y-%m-%d')} 15:30",
            )

            if result and len(result) >= 1:
                # Last candle in the range IS the previous trading day
                # (Angel One skips holidays, so result[-1] is always the last traded session)
                prev = result[-1]
                actual_date = prev[0][:10]  # "YYYY-MM-DD" from the candle timestamp
                ohlc = {
                    "open":  float(prev[1]),
                    "high":  float(prev[2]),
                    "low":   float(prev[3]),
                    "close": float(prev[4]),
                }
                logger.info(
                    f"Prev day futures OHLC (actual={actual_date}, requested={prev_td}): "
                    f"O={ohlc['open']:.2f} H={ohlc['high']:.2f} "
                    f"L={ohlc['low']:.2f} C={ohlc['close']:.2f}"
                )
                return ohlc

        except Exception as e:
            logger.warning(f"Could not fetch prev day OHLC: {e}")

        # Fallback: approximate from current spot (CPR will be wide → no-trade bias)
        spot = self._md.get_spot_price() or 22000.0
        logger.warning(
            "Using fallback OHLC (spot ±0.5%) — CPR will be unreliable. "
            "Check broker connectivity."
        )
        return {
            "open":  spot,
            "high":  spot * 1.005,
            "low":   spot * 0.995,
            "close": spot,
        }

    async def _subscribe_option_chain(self) -> None:
        """
        Subscribe to ATM ±3 strikes (CE + PE) for the current weekly expiry.

        Token lookup uses InstrumentManager (NFO scrip master). Each option
        is subscribed on exchange_type=EXCHANGE_TYPE_NSE_FO (2) — NOT NSE CM (1).
        """
        if not self._instruments.is_initialized:
            logger.error("InstrumentManager not initialized — cannot subscribe options")
            return

        if not self._current_expiry:
            logger.error("No current expiry set — cannot subscribe options")
            return

        spot = self._md.get_spot_price() or 22000.0
        interval = self._cfg["strategy"]["options"]["strike_interval"]
        atm = GreeksCalculator.get_atm_strike(spot, interval)
        num_strikes = self._cfg["strategy"]["options"].get("num_strikes", 3)

        tokens: list[str] = []
        symbols: list[str] = []
        registered = 0
        missing = 0

        for offset in range(-num_strikes, num_strikes + 1):
            strike = atm + offset * interval
            for opt_type in ("CE", "PE"):
                token = self._instruments.get_option_token(
                    "NIFTY", strike, opt_type, self._current_expiry
                )
                if not token:
                    missing += 1
                    logger.warning(
                        f"Token not found in master: NIFTY {strike}{opt_type} "
                        f"expiry={self._current_expiry}"
                    )
                    continue

                info = self._instruments.get_instrument(token)
                symbol = info.symbol if info else f"NIFTY{strike}{opt_type}"

                # Register with MarketDataEngine
                self._md.register_instrument(
                    token=token,
                    symbol=symbol,
                    instrument_type="OPTION",
                )

                # Register option quote
                from datetime import date as _date
                quote = OptionQuote(
                    symbol=symbol,
                    token=token,
                    strike=strike,
                    option_type=opt_type,
                    expiry=self._current_expiry,
                    ltp=0.0,
                    bid=0.0,
                    ask=0.0,
                    volume=0,
                    oi=0,
                    change_oi=0,
                )
                self._md.register_option(quote)
                self._strategy_manager.register_option(symbol, token)

                tokens.append(token)
                symbols.append(symbol)
                registered += 1

        if tokens:
            # CRITICAL: options are on NFO exchange, not NSE CM
            self._ws.subscribe(
                tokens=tokens,
                symbols=symbols,
                exchange_type=EXCHANGE_TYPE_NSE_FO,
            )
            logger.info(
                f"Option chain subscribed: ATM={atm} ±{num_strikes} strikes "
                f"({registered} contracts, {missing} not found) "
                f"expiry={self._current_expiry}"
            )
        else:
            logger.error(
                f"No option tokens found for expiry={self._current_expiry}. "
                f"Check instrument master freshness."
            )

        # Start the option chain REST poller (PCR, OI change, max pain)
        self._option_chain = OptionChainManager(
            connector=self._connector,
            instrument_manager=self._instruments,
            expiry=self._current_expiry,
            spot_getter=self._md.get_spot_price,
        )
        await self._option_chain.start()

    def _find_weekly_expiry(self) -> date:
        """Find the next tradable NIFTY expiry from the instrument master."""
        master_expiry = self._instruments.get_next_nifty_expiry(date.today())
        if master_expiry:
            return master_expiry

        logger.warning("No NIFTY expiry found in instrument master; falling back to next Tuesday")
        today = date.today()
        days_to_tuesday = (1 - today.weekday()) % 7
        if days_to_tuesday == 0:
            days_to_tuesday = 7  # Next Tuesday
        from datetime import timedelta
        expiry = today + timedelta(days=days_to_tuesday)
        return expiry

    def _is_no_trade_day(self, d: date) -> bool:
        """Check if today is on the no-trade calendar."""
        no_trade = self._cfg.get("events", {}).get("no_trade_dates", [])
        return d.isoformat() in no_trade

    def stop(self) -> None:
        self._running = False


# ─── Indicator Warm-up ─────────────────────────────────────

async def _seed_indicators_today(
    connector,
    market_data,
    token: str,
    symbol: str,
    lookback_days: int = 3,
) -> None:
    """
    Fetch the last `lookback_days` trading sessions of 1-min bars and seed them
    into the BarBuilder so EMAs, ATR, and Bollinger Bands are fully warmed up
    before live data arrives — even if started right at market open.

    Previous-day bars warm up EMA50/ATR/BB. Today's bars bring them current.
    Yesterday's final close also gives ATR the correct prev_close for the first
    True Range of the new session (accounts for overnight gap).
    """
    from datetime import timedelta
    from core.data.market_data_engine import OHLCVBar

    # Fetch from `lookback_days` ago (covers weekends/holidays automatically —
    # Angel One just returns whatever trading days exist in the range).
    from_date = date.today() - timedelta(days=lookback_days)
    from_dt = f"{from_date.strftime('%Y-%m-%d')} 09:15"

    # Fetch up to current minute minus 1 to avoid a partial bar
    to_minute = (datetime.now() - timedelta(minutes=1)).replace(second=0, microsecond=0)
    to_dt = to_minute.strftime("%Y-%m-%d %H:%M")

    logger.info(f"Seeding indicators: fetching {symbol} bars {from_dt} → {to_dt} ({lookback_days}d lookback)")
    try:
        raw = await connector.get_historical_data(
            token=token,
            exchange="NFO",
            symbol=symbol,
            interval="ONE_MINUTE",
            from_date=from_dt,
            to_date=to_dt,
        )
    except Exception as e:
        logger.warning(f"Indicator seed failed (non-critical): {e} — indicators will warm up live")
        return

    if not raw:
        logger.info("Indicator seed: no bars returned — continuing without pre-warm")
        return

    bars: list[OHLCVBar] = []
    for item in raw:
        if len(item) < 6:
            continue
        ts_raw = item[0][:19]
        try:
            dt = datetime.fromisoformat(ts_raw)
        except ValueError:
            continue
        t = (dt.hour, dt.minute)
        if t < (9, 15) or t >= (15, 30):
            continue
        bars.append(OHLCVBar(
            token=token,
            symbol=symbol,
            bar_time=dt,
            open=float(item[1]),
            high=float(item[2]),
            low=float(item[3]),
            close=float(item[4]),
            volume=int(item[5]),
            oi=0,
        ))

    market_data.seed_historical_bars(token, bars)


# ─── Main Entry Point ──────────────────────────────────────

async def main(args) -> None:
    """System entrypoint. Initializes all components and runs session."""
    # Load config
    config = load_config("config")
    validate_config(config)

    # Setup logging
    setup_logging(
        log_dir=config["system"]["logging"]["log_dir"],
        level=config["system"]["logging"]["level"],
    )

    logger.info("="*60)
    logger.info("NIFTY OPTIONS SCALPING SYSTEM — STARTUP")
    logger.info(f"Mode: {config['strategy']['mode']}")
    logger.info(f"Date: {date.today()}")
    logger.info("="*60)

    # Initialize components
    creds = config["credentials"]["angel_one"]
    account_capital = float(os.environ.get("ACCOUNT_CAPITAL", "100000"))

    # Auth
    auth = AngelAuthManager(creds)
    rate_limiter = RateLimiter(config["system"]["api"]["rate_limit_requests_per_second"])
    connector = AngelConnector(auth, rate_limiter)
    ws_handler = WebSocketHandler(
        auth_manager=auth,
        stale_threshold_sec=config["system"]["websocket"]["stale_threshold_seconds"],
    )

    # Data layer
    greeks = GreeksCalculator(risk_free_rate=0.065)
    market_data = MarketDataEngine(greeks)
    instrument_manager = InstrumentManager()

    # NIFTY futures and VIX registration happens after instrument_manager loads
    # (see startup sequence below) so we can cross-validate tokens from master.

    # Strategy layer
    signal_engine = SignalEngine(
        market_data=market_data,
        greeks_calc=greeks,
        strategy_config=config["strategy"],
        risk_config=config["risk"],
    )

    strategy_cfg = config["strategy"]
    risk_cfg = config["risk"]
    strategies_cfg = config.get("strategies", {})

    def _scfg(name: str) -> dict:
        """Build per-strategy config: merge strategy-level exits/options with strategy-specific keys."""
        base = {
            "exits": strategy_cfg.get("exits", {}),
            "options": strategy_cfg.get("options", {}),
            "gamma_mode": strategy_cfg.get("gamma_mode", {}),
        }
        base.update(strategies_cfg.get(name, {}))
        return base

    avcs_strategy = AVCSStrategy(
        market_data=market_data,
        signal_engine=signal_engine,
        greeks_calc=greeks,
        strategy_config=strategy_cfg,
        risk_config=risk_cfg,
        account_capital=account_capital,
    )

    lot_size = strategy_cfg.get("options", {}).get("lot_size", 75)
    all_strategies = [avcs_strategy]

    if strategies_cfg.get("liquidity_sweep", {}).get("enabled", True):
        all_strategies.append(LiquiditySweepReversal(_scfg("liquidity_sweep"), risk_cfg))
    if strategies_cfg.get("false_breakout", {}).get("enabled", True):
        all_strategies.append(FalseBreakoutTrap(_scfg("false_breakout"), risk_cfg))
    if strategies_cfg.get("bos_retest", {}).get("enabled", True):
        all_strategies.append(BOSRetest(_scfg("bos_retest"), risk_cfg))
    if strategies_cfg.get("fair_value_gap", {}).get("enabled", True):
        all_strategies.append(FairValueGap(_scfg("fair_value_gap"), risk_cfg))
    if strategies_cfg.get("ema_trend_ride", {}).get("enabled", True):
        all_strategies.append(EMATrendRide(_scfg("ema_trend_ride"), risk_cfg))
    if strategies_cfg.get("vwap_mean_reversion", {}).get("enabled", True):
        all_strategies.append(VWAPMeanReversion(_scfg("vwap_mean_reversion"), risk_cfg))
    if strategies_cfg.get("bollinger_reversion", {}).get("enabled", True):
        all_strategies.append(BollingerReversion(_scfg("bollinger_reversion"), risk_cfg))
    if strategies_cfg.get("gamma_pinning", {}).get("enabled", True):
        all_strategies.append(GammaPinning(_scfg("gamma_pinning"), risk_cfg))
    if strategies_cfg.get("daily_momentum", {}).get("enabled", True):
        all_strategies.append(DailyMomentumDrive(_scfg("daily_momentum"), risk_cfg))

    portfolio_risk = PortfolioRisk(risk_cfg, account_capital, lot_size)
    execution_selector = ExecutionSelector(strategy_cfg)
    strategy_manager = StrategyManager(
        strategies=all_strategies,
        portfolio_risk=portfolio_risk,
        execution_selector=execution_selector,
        lot_size=lot_size,
    )

    logger.info(
        f"StrategyManager initialized: {len(all_strategies)} strategies — "
        + ", ".join(s.name for s in all_strategies)
    )

    # Execution layer
    paper_sim = PaperSimulator(realism=SimulationRealism.REALISTIC)
    expense_engine = ExpenseEngine(config["expense"])
    order_manager = OrderManager(paper_sim, expense_engine)

    # Risk layer
    risk_engine = RiskEngine(config["risk"], account_capital)

    # Journal and analytics
    journal = TradeJournal()
    analytics = AnalyticsEngine()

    # Alerts
    tg_cfg = config["credentials"].get("telegram", {})
    alerts = TelegramAlerts(
        bot_token=tg_cfg.get("bot_token", ""),
        chat_id=tg_cfg.get("chat_id", ""),
        enabled=config["system"]["alerts"]["enabled"],
    )

    # Health monitor
    health = HealthMonitor(
        ws_handler=ws_handler,
        market_data=market_data,
        risk_engine=risk_engine,
        check_interval_sec=config["system"]["health"]["check_interval_seconds"],
    )

    # Wire up WebSocket → market data
    ws_handler.on_tick(market_data.on_tick)

    # Wire up paper simulator → quote updates
    def update_sim_quotes(token, bar) -> None:
        if bar:
            paper_sim.update_quote(
                symbol=bar.symbol,
                ltp=bar.close,
                bid=bar.close * 0.998,
                ask=bar.close * 1.002,
                volume=bar.volume,
            )
    market_data.on_new_bar(update_sim_quotes)

    # Graceful shutdown handling
    shutdown_event = asyncio.Event()

    def handle_shutdown(signum, frame):
        logger.info(f"Signal {signum} received — initiating graceful shutdown")
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    try:
        # Authenticate
        logger.info("Authenticating with Angel One...")
        await auth.initialize()
        logger.info("Authentication successful")

        # Download instrument master (must come before any option subscriptions)
        logger.info("Loading NSE instrument master...")
        await instrument_manager.initialize()
        logger.info(
            f"Instrument master ready: {instrument_manager.total_instruments} instruments, "
            f"{instrument_manager.total_nifty_options} NIFTY options"
        )

        # Register NIFTY futures as the underlying. Index candles/ticks do not
        # have real traded volume, so the live paper strategy uses futures OHLCV
        # for ORB, VWAP, EMA, breakout, and volume confirmation.
        nifty_future = instrument_manager.get_nearest_future("NIFTY")
        if not nifty_future:
            raise RuntimeError("NIFTY FUTIDX contract not found in instrument master")
        underlying_token = nifty_future.token
        underlying_symbol = nifty_future.symbol

        vix_token = (
            instrument_manager.get_vix_token() or TradingSession.NIFTY_VIX_TOKEN
        )
        market_data.register_instrument(
            token=underlying_token,
            symbol=underlying_symbol,
            instrument_type="NIFTY",
        )
        market_data.register_instrument(
            token=vix_token,
            symbol="INDIA_VIX",
            instrument_type="VIX",
        )
        logger.info(
            f"Underlying futures: {underlying_symbol} token={underlying_token} | "
            f"VIX token: {vix_token}"
        )

        # Subscribe NIFTY futures on NFO and VIX on NSE cash market.
        ws_handler.subscribe(
            tokens=[underlying_token],
            symbols=[underlying_symbol],
            exchange_type=EXCHANGE_TYPE_NSE_FO,
        )
        ws_handler.subscribe(
            tokens=[vix_token],
            symbols=["INDIA_VIX"],
            exchange_type=EXCHANGE_TYPE_NSE_CM,
        )

        # Connect WebSocket
        logger.info("Connecting WebSocket...")
        await ws_handler.connect()

        # Start health monitor
        await health.start()

        # Build session
        session = TradingSession(
            config=config,
            auth=auth,
            connector=connector,
            ws_handler=ws_handler,
            market_data=market_data,
            instrument_manager=instrument_manager,
            signal_engine=signal_engine,
            avcs_strategy=avcs_strategy,
            strategy_manager=strategy_manager,
            order_manager=order_manager,
            risk_engine=risk_engine,
            journal=journal,
            analytics=analytics,
            alerts=alerts,
            health=health,
            account_capital=account_capital,
            underlying_token=underlying_token,
            underlying_symbol=underlying_symbol,
            underlying_exchange="NFO",
        )

        dashboard_server = None
        dashboard_task: Optional[asyncio.Task] = None
        if config["system"]["dashboard"].get("enabled", False):
            import uvicorn

            dash_cfg = config["system"]["dashboard"]
            dashboard_app = create_dashboard_app(
                market_data=market_data,
                order_manager=order_manager,
                health_monitor=health,
                risk_engine=risk_engine,
                strategy_manager=strategy_manager,
                instrument_manager=instrument_manager,
            )
            dashboard_server = uvicorn.Server(
                uvicorn.Config(
                    dashboard_app,
                    host=dash_cfg.get("host", "0.0.0.0"),
                    port=int(dash_cfg.get("port", 8080)),
                    log_level="warning",
                )
            )
            dashboard_task = asyncio.create_task(dashboard_server.serve())
            _dash_port = int(dash_cfg.get("port", 8080))
            try:
                import socket
                _local_ip = socket.gethostbyname(socket.gethostname())
            except Exception:
                _local_ip = "0.0.0.0"
            logger.info(
                "Dashboard → local: http://localhost:%s  |  network: http://%s:%s",
                _dash_port, _local_ip, _dash_port,
            )
            print(f"\n  Dashboard running:")
            print(f"    Local   → http://localhost:{_dash_port}")
            print(f"    Network → http://{_local_ip}:{_dash_port}\n")

        # Run trading session
        session_task = asyncio.create_task(session.run())

        shutdown_task = asyncio.create_task(shutdown_event.wait())
        if dashboard_task:
            # Keep dashboard visible after the trading session completes; stop
            # with Ctrl+C/SIGTERM. If shutdown arrives first, cancel session.
            done, pending = await asyncio.wait(
                [session_task, shutdown_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            if shutdown_task in done and not session_task.done():
                session_task.cancel()
                try:
                    await session_task
                except asyncio.CancelledError:
                    pass
            elif session_task in done:
                logger.info("Trading session complete; dashboard remains online until shutdown")
                await shutdown_task
        else:
            done, pending = await asyncio.wait(
                [session_task, shutdown_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in pending:
                task.cancel()

        if dashboard_server:
            dashboard_server.should_exit = True
        if dashboard_task and not dashboard_task.done():
            await dashboard_task

    except Exception as e:
        logger.critical(f"Fatal startup error: {e}", exc_info=True)
        await alerts.send_risk_alert("FATAL_ERROR", str(e))
    finally:
        logger.info("Shutting down...")
        await health.stop()
        await ws_handler.close()
        await auth.shutdown()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NIFTY Options Scalping System")
    parser.add_argument(
        "--mode", choices=["paper"], default="paper",
        help="Trading mode (only 'paper' supported in Phase 3)"
    )
    parser.add_argument(
        "--capital", type=float, default=10000000,
        help="Account capital in Rs. (default ₹1 crore — paper mode, unlimited)"
    )
    args = parser.parse_args()

    os.environ["ACCOUNT_CAPITAL"] = str(args.capital)

    asyncio.run(main(args))
