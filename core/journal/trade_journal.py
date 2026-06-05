"""
Trade Journal
==============
Immutable append-only trade and event log.

All trades, signals, system events are recorded here.
Journal entries are never modified — only appended.
This creates a complete audit trail for analysis.

Writes to both database (via db_manager) and structured log files.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from core.execution.order_manager import TradeRecord
from core.strategy.signal_engine import Signal

logger = logging.getLogger(__name__)

# Dedicated journal logger — outputs to journal.log
journal_logger = logging.getLogger("journal")


class TradeJournal:
    """
    Immutable trade journal.

    Captures:
    - Every signal generated (traded and rejected)
    - Every trade entry and exit
    - All system events (API status, risk events, etc.)
    - Daily summaries
    """

    def __init__(self) -> None:
        self._signals: List[Dict] = []
        self._trades: List[Dict] = []
        self._events: List[Dict] = []

    def log_signal(self, signal: Signal, action: str, trade_id: Optional[str] = None) -> None:
        """
        Log a generated signal.
        action: TRADED | REJECTED | RISK_BLOCKED
        """
        conditions_met = [c.name for c in signal.conditions if c.passed]
        conditions_failed = [c.name for c in signal.conditions if not c.passed]

        entry = {
            "signal_id": signal.signal_id,
            "signal_time": signal.signal_time.isoformat(),
            "session_date": signal.session_date.isoformat(),
            "direction": signal.direction.value if signal.direction else None,
            "signal_type": signal.signal_type.value if signal.signal_type else None,
            "regime": signal.regime.value if signal.regime else None,
            "spot_price": signal.spot_price,
            "vwap": signal.vwap,
            "orb_high": signal.orb_high,
            "orb_low": signal.orb_low,
            "india_vix": signal.india_vix,
            "proposed_strike": signal.proposed_strike,
            "option_premium": signal.option_premium,
            "option_delta": signal.option_delta,
            "option_spread": signal.option_spread,
            "option_oi": signal.option_oi,
            "conditions_met": conditions_met,
            "conditions_failed": conditions_failed,
            "action": action,
            "rejection_reason": signal.rejection_reason,
            "trade_id": trade_id,
        }

        self._signals.append(entry)
        journal_logger.info(
            f"SIGNAL|{action}|{signal.signal_id[:8]}|"
            f"{signal.direction.value if signal.direction else 'N/A'}|"
            f"spot={signal.spot_price:.2f}|"
            f"pass=[{','.join(conditions_met)}]|"
            f"fail=[{','.join(conditions_failed)}]"
        )

    def log_trade_entry(self, trade: TradeRecord) -> None:
        """Log trade entry."""
        entry = {
            "event": "TRADE_ENTRY",
            "trade_id": trade.trade_id,
            "timestamp": trade.entry_time.isoformat() if trade.entry_time else None,
            "session_date": trade.session_date.isoformat(),
            "symbol": trade.symbol,
            "option_type": trade.option_type,
            "strike": trade.strike,
            "lots": trade.lots,
            "quantity": trade.quantity,
            "entry_price": trade.entry_price,
            "entry_spot": trade.entry_spot,
            "entry_vwap": trade.entry_vwap,
            "entry_iv": trade.entry_iv,
            "entry_delta": trade.entry_delta,
            "entry_oi": trade.entry_oi,
            "signal_id": trade.signal_id,
            "signal_reason": trade.signal_reason,
            "entry_slippage": trade.entry_slippage,
            "entry_latency_ms": trade.entry_latency_ms,
        }
        self._trades.append(entry)
        journal_logger.info(
            f"ENTRY|{trade.trade_id[:8]}|{trade.symbol}|"
            f"{trade.quantity}qty@₹{trade.entry_price:.2f}|"
            f"slip=₹{trade.entry_slippage:.2f}"
        )

    def log_trade_exit(self, trade: TradeRecord) -> None:
        """Log trade exit with complete P&L."""
        entry = {
            "event": "TRADE_EXIT",
            "trade_id": trade.trade_id,
            "timestamp": trade.exit_time.isoformat() if trade.exit_time else None,
            "session_date": trade.session_date.isoformat(),
            "symbol": trade.symbol,
            "exit_price": trade.exit_price,
            "exit_reason": trade.exit_reason,
            "gross_pnl": trade.gross_pnl,
            "total_charges": trade.total_charges,
            "net_pnl": trade.net_pnl,
            "pnl_pct": round(trade.pnl_pct * 100, 2),
            "exit_slippage": trade.exit_slippage,
            "exit_latency_ms": trade.exit_latency_ms,
            "won": trade.net_pnl > 0,
            "hold_time_min": (
                (trade.exit_time - trade.entry_time).total_seconds() / 60
                if trade.exit_time and trade.entry_time else 0
            ),
        }
        self._trades.append(entry)
        won_str = "WIN" if trade.net_pnl > 0 else "LOSS"
        journal_logger.info(
            f"EXIT|{trade.trade_id[:8]}|{trade.symbol}|"
            f"@₹{trade.exit_price:.2f}|reason={trade.exit_reason}|"
            f"gross=₹{trade.gross_pnl:.2f}|charges=₹{trade.total_charges:.2f}|"
            f"net=₹{trade.net_pnl:.2f}({trade.pnl_pct*100:.1f}%)|{won_str}"
        )

    def log_system_event(
        self,
        event_type: str,
        severity: str,
        message: str,
        details: Optional[Dict] = None,
    ) -> None:
        """Log a system event (API status, risk event, error, etc.)"""
        entry = {
            "event_type": event_type,
            "severity": severity,
            "message": message,
            "details": details or {},
            "timestamp": datetime.now().isoformat(),
        }
        self._events.append(entry)
        journal_logger.log(
            getattr(logging, severity.upper(), logging.INFO),
            f"EVENT|{event_type}|{message}"
        )

    def log_daily_summary(
        self,
        session_date: date,
        regime: str,
        india_vix: float,
        trades: int,
        wins: int,
        gross_pnl: float,
        net_pnl: float,
        total_charges: float,
    ) -> None:
        """Log end-of-day summary."""
        win_rate = wins / trades * 100 if trades > 0 else 0.0
        entry = {
            "event": "DAILY_SUMMARY",
            "session_date": session_date.isoformat(),
            "regime": regime,
            "india_vix": india_vix,
            "trades": trades,
            "wins": wins,
            "losses": trades - wins,
            "win_rate_pct": round(win_rate, 1),
            "gross_pnl": round(gross_pnl, 2),
            "total_charges": round(total_charges, 2),
            "net_pnl": round(net_pnl, 2),
            "timestamp": datetime.now().isoformat(),
        }
        self._events.append(entry)
        journal_logger.info(
            f"DAILY_SUMMARY|{session_date}|regime={regime}|"
            f"trades={trades}|wins={wins}({win_rate:.0f}%)|"
            f"gross=₹{gross_pnl:.2f}|charges=₹{total_charges:.2f}|"
            f"net=₹{net_pnl:.2f}"
        )

    @property
    def all_signals(self) -> List[Dict]:
        return list(self._signals)

    @property
    def all_trades(self) -> List[Dict]:
        return list(self._trades)

    @property
    def all_events(self) -> List[Dict]:
        return list(self._events)
