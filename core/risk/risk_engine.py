"""
Risk Engine
============
Institutional-grade pre-trade and intraday risk controls.

Responsibilities:
- Pre-trade validation (blocks invalid signals)
- Intraday risk monitoring
- Daily limit enforcement
- Circuit breaker logic
- Kill switch monitoring
- Position mismatch detection
- Forced exit orchestration

CRITICAL: This module MUST be called before every order.
It cannot be bypassed. Any code that skips this is a bug.

Fail-safe design: When in doubt, REJECT.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class RiskViolationType(str, Enum):
    """Types of risk violations."""
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    CONSECUTIVE_LOSSES = "CONSECUTIVE_LOSSES"
    MAX_TRADES = "MAX_TRADES"
    VIX_LIMIT = "VIX_LIMIT"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
    STALE_DATA = "STALE_DATA"
    API_DISCONNECT = "API_DISCONNECT"
    KILL_SWITCH = "KILL_SWITCH"
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"
    PREMIUM_EXPOSURE = "PREMIUM_EXPOSURE"
    MAX_DRAWDOWN = "MAX_DRAWDOWN"
    EXISTING_POSITION = "EXISTING_POSITION"
    SESSION_HALTED = "SESSION_HALTED"
    NO_TRADE_REGIME = "NO_TRADE_REGIME"


@dataclass
class RiskCheckResult:
    """Result of a pre-trade risk check."""
    approved: bool
    violations: List[RiskViolationType] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)

    def add_violation(self, violation: RiskViolationType, message: str) -> None:
        self.violations.append(violation)
        self.messages.append(message)
        self.approved = False

    @property
    def primary_violation(self) -> Optional[RiskViolationType]:
        return self.violations[0] if self.violations else None

    @property
    def rejection_summary(self) -> str:
        return " | ".join(self.messages)


@dataclass
class CircuitBreakerState:
    """State of the system circuit breaker."""
    tripped: bool = False
    trip_time: Optional[datetime] = None
    trip_reason: Optional[str] = None
    trip_count_today: int = 0
    requires_manual_reset: bool = True

    def trip(self, reason: str) -> None:
        self.tripped = True
        self.trip_time = datetime.now()
        self.trip_reason = reason
        self.trip_count_today += 1
        logger.critical(f"CIRCUIT BREAKER TRIPPED: {reason}")

    def reset(self) -> None:
        self.tripped = False
        self.trip_time = None
        self.trip_reason = None
        logger.warning("Circuit breaker manually reset")


class RiskEngine:
    """
    Central risk management system.

    Call order:
    1. check_pre_trade() — before every entry
    2. update_position_risk() — on every tick for open positions
    3. check_system_health() — on every heartbeat
    4. check_kill_switch() — in main loop

    No order should EVER be placed without passing check_pre_trade().
    """

    def __init__(
        self,
        risk_config: dict,
        account_capital: float,
    ) -> None:
        self._cfg = risk_config
        self._capital = account_capital

        # Daily tracking
        self._daily_pnl: float = 0.0
        self._daily_trades: int = 0
        self._consecutive_losses: int = 0
        self._peak_capital: float = account_capital

        # System state
        self._circuit_breaker = CircuitBreakerState()
        self._session_halted: bool = False
        self._api_connected: bool = True
        self._data_fresh: bool = True

        # Callbacks for forced exits
        self._force_exit_callbacks: List[Callable] = []

        # Kill switch file path
        self._kill_switch_path = Path(
            risk_config.get("kill_switch", {}).get("trigger_file", "/tmp/algo_kill_switch")
        )

        logger.info(
            f"Risk engine initialized: capital=₹{account_capital:,.0f}",
            extra={
                "daily_loss_limit": f"{risk_config['daily']['max_loss_pct']*100:.1f}%",
                "max_drawdown": f"{risk_config['system']['max_drawdown_pct']*100:.1f}%",
            }
        )

    def check_pre_trade(
        self,
        premium: float,
        quantity: int,
        option_oi: int,
        option_spread: float,
        option_mid_price: float,
        india_vix: float,
        has_open_position: bool,
        is_data_fresh: bool,
    ) -> RiskCheckResult:
        """
        Pre-trade risk gate. MUST be called before every order.

        Returns RiskCheckResult. If approved=False, DO NOT enter.
        No exceptions. No overrides.
        """
        result = RiskCheckResult(approved=True)

        # ─── 1. Kill switch ────────────────────────────
        if self._check_kill_switch():
            result.add_violation(
                RiskViolationType.KILL_SWITCH,
                "Kill switch activated — no trading permitted"
            )
            return result  # Early return — system is in emergency state

        # ─── 2. Circuit breaker ────────────────────────
        if self._circuit_breaker.tripped:
            result.add_violation(
                RiskViolationType.CIRCUIT_BREAKER,
                f"Circuit breaker tripped: {self._circuit_breaker.trip_reason}"
            )
            return result

        # ─── 3. Session halted ─────────────────────────
        if self._session_halted:
            result.add_violation(
                RiskViolationType.SESSION_HALTED,
                "Session halted by risk system"
            )
            return result

        # ─── 4. Data freshness ────────────────────────
        if not is_data_fresh:
            result.add_violation(
                RiskViolationType.STALE_DATA,
                f"Market data is stale — cannot trade safely"
            )

        # ─── 5. API connectivity ──────────────────────
        if not self._api_connected:
            result.add_violation(
                RiskViolationType.API_DISCONNECT,
                "API not connected — blocking new orders"
            )

        # ─── 6. No existing position ──────────────────
        if has_open_position:
            result.add_violation(
                RiskViolationType.EXISTING_POSITION,
                "Position already open — no pyramiding in v1"
            )

        # ─── 7. Daily loss limit ───────────────────────
        max_daily_loss = self._capital * self._cfg["daily"]["max_loss_pct"]
        if self._daily_pnl <= -max_daily_loss:
            result.add_violation(
                RiskViolationType.DAILY_LOSS_LIMIT,
                f"Daily loss limit hit: ₹{-self._daily_pnl:.2f} of limit ₹{max_daily_loss:.2f}"
            )
            self._halt_session("Daily loss limit reached")

        # ─── 8. Consecutive losses ────────────────────
        max_consec = self._cfg["daily"]["max_consecutive_losses"]
        if self._consecutive_losses >= max_consec:
            result.add_violation(
                RiskViolationType.CONSECUTIVE_LOSSES,
                f"Consecutive loss limit: {self._consecutive_losses}/{max_consec}"
            )
            self._halt_session(f"{self._consecutive_losses} consecutive losses")

        # ─── 9. Max trades per day ────────────────────
        max_trades = self._cfg["daily"]["max_trades"]
        if self._daily_trades >= max_trades:
            result.add_violation(
                RiskViolationType.MAX_TRADES,
                f"Max trades/day reached: {self._daily_trades}/{max_trades}"
            )

        # ─── 10. India VIX ────────────────────────────
        max_vix = self._cfg["market"]["max_vix"]
        if india_vix > max_vix and india_vix > 0:
            result.add_violation(
                RiskViolationType.VIX_LIMIT,
                f"India VIX={india_vix:.1f} exceeds limit={max_vix}"
            )

        # ─── 11. Option spread ────────────────────────
        max_spread_pct = self._cfg["market"]["max_spread_pct"]
        spread_pct = option_spread / option_mid_price if option_mid_price > 0 else 1.0
        if spread_pct > max_spread_pct:
            result.add_violation(
                RiskViolationType.SPREAD_TOO_WIDE,
                f"Spread too wide: {spread_pct*100:.1f}% (max={max_spread_pct*100:.1f}%)"
            )

        # ─── 12. Option liquidity ─────────────────────
        min_oi = self._cfg["market"]["min_option_oi"]
        if option_oi < min_oi:
            result.add_violation(
                RiskViolationType.INSUFFICIENT_LIQUIDITY,
                f"Option OI={option_oi:,} below minimum={min_oi:,}"
            )

        # ─── 13. Premium exposure ─────────────────────
        max_premium_pct = self._cfg["per_trade"]["max_premium_exposure_pct"]
        total_premium = premium * quantity
        max_exposure = self._capital * max_premium_pct
        if total_premium > max_exposure:
            result.add_violation(
                RiskViolationType.PREMIUM_EXPOSURE,
                f"Premium exposure ₹{total_premium:.0f} > limit ₹{max_exposure:.0f}"
            )

        # ─── 14. Drawdown check ───────────────────────
        drawdown = (self._peak_capital - (self._capital + self._daily_pnl)) / self._peak_capital
        max_dd = self._cfg["system"]["max_drawdown_pct"]
        if drawdown >= max_dd:
            result.add_violation(
                RiskViolationType.MAX_DRAWDOWN,
                f"Max drawdown {drawdown*100:.1f}% >= limit {max_dd*100:.1f}%"
            )
            self._trip_circuit_breaker(f"Max drawdown {drawdown*100:.1f}% reached")

        if result.violations:
            logger.warning(
                f"Pre-trade check FAILED: {result.rejection_summary}"
            )
        else:
            logger.debug("Pre-trade check PASSED")

        return result

    def update_trade_result(self, pnl: float, won: bool) -> None:
        """Update risk state after a trade closes."""
        self._daily_pnl += pnl
        self._daily_trades += 1

        if won:
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1

        # Update peak capital
        current_capital = self._capital + self._daily_pnl
        if current_capital > self._peak_capital:
            self._peak_capital = current_capital

        logger.info(
            f"Trade result recorded: pnl=₹{pnl:.2f} won={won} "
            f"daily_pnl=₹{self._daily_pnl:.2f} consec_losses={self._consecutive_losses}"
        )

    def update_api_status(self, connected: bool) -> None:
        """Update API connectivity status."""
        if not connected and self._api_connected:
            logger.warning("API disconnected — blocking new entries")
        elif connected and not self._api_connected:
            logger.info("API reconnected — entries re-enabled")
        self._api_connected = connected

    def update_data_freshness(self, is_fresh: bool) -> None:
        """Update data freshness status."""
        if not is_fresh and self._data_fresh:
            logger.warning("Market data gone stale — blocking new entries")
        self._data_fresh = is_fresh

    def _check_kill_switch(self) -> bool:
        """Check if kill switch file exists."""
        if self._kill_switch_path.exists():
            logger.critical(f"KILL SWITCH DETECTED: {self._kill_switch_path}")
            return True
        return False

    def _halt_session(self, reason: str) -> None:
        """Halt trading for the rest of the session."""
        if not self._session_halted:
            self._session_halted = True
            logger.warning(f"SESSION HALTED: {reason}")

    def _trip_circuit_breaker(self, reason: str) -> None:
        """Trip the system circuit breaker."""
        self._circuit_breaker.trip(reason)
        self._session_halted = True

        for cb in self._force_exit_callbacks:
            try:
                cb(reason)
            except Exception as e:
                logger.error(f"Force exit callback error: {e}")

    def on_force_exit(self, callback: Callable) -> None:
        """Register callback to be called when forced exit is required."""
        self._force_exit_callbacks.append(callback)

    def reset_daily_state(self) -> None:
        """Reset all daily counters. Call at session start."""
        self._daily_pnl = 0.0
        self._daily_trades = 0
        self._consecutive_losses = 0
        self._session_halted = False
        # DO NOT reset circuit breaker — needs manual reset

        logger.info("Risk engine daily state reset")

    @property
    def is_trading_permitted(self) -> bool:
        """Quick check: is ANY trading currently permitted?"""
        return (
            not self._circuit_breaker.tripped and
            not self._session_halted and
            not self._check_kill_switch() and
            self._api_connected and
            self._data_fresh
        )

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def daily_trades(self) -> int:
        return self._daily_trades

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    @property
    def circuit_breaker(self) -> CircuitBreakerState:
        return self._circuit_breaker

    def get_status_dict(self) -> dict:
        """Return complete risk status for dashboard."""
        max_daily_loss = self._capital * self._cfg["daily"]["max_loss_pct"]
        return {
            "trading_permitted": self.is_trading_permitted,
            "session_halted": self._session_halted,
            "circuit_breaker_tripped": self._circuit_breaker.tripped,
            "circuit_breaker_reason": self._circuit_breaker.trip_reason,
            "api_connected": self._api_connected,
            "data_fresh": self._data_fresh,
            "daily_pnl": round(self._daily_pnl, 2),
            "daily_trades": self._daily_trades,
            "max_daily_trades": self._cfg["daily"]["max_trades"],
            "consecutive_losses": self._consecutive_losses,
            "max_consecutive_losses": self._cfg["daily"]["max_consecutive_losses"],
            "daily_loss_used_pct": round((-self._daily_pnl / max_daily_loss * 100) if self._daily_pnl < 0 else 0, 1),
            "daily_loss_limit": round(max_daily_loss, 2),
        }
