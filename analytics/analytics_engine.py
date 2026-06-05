"""
Analytics Engine
=================
Computes strategy performance metrics from trade history.

Metrics:
- Win rate, expectancy, profit factor
- Sharpe ratio, Sortino ratio
- Max drawdown, drawdown duration
- Average hold time
- Charge impact analysis
- Equity curve

All calculations on NET P&L after charges.
Never evaluate on gross P&L.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple

import numpy as np

from core.execution.order_manager import TradeRecord

logger = logging.getLogger(__name__)


@dataclass
class PerformanceMetrics:
    """Complete strategy performance summary."""
    # Trade stats
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0

    # P&L
    total_gross_pnl: float = 0.0
    total_charges: float = 0.0
    total_net_pnl: float = 0.0

    # Return stats
    avg_winner: float = 0.0
    avg_loser: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0

    # Risk metrics
    profit_factor: float = 0.0
    expectancy: float = 0.0           # avg P&L per trade in Rs.
    expectancy_r: float = 0.0         # avg P&L per trade in R multiples
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    max_consecutive_losses: int = 0
    max_consecutive_wins: int = 0

    # Time stats
    avg_hold_time_min: float = 0.0

    # Risk-adjusted
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0

    # Charge impact
    total_charges_as_pct_of_gross: float = 0.0
    avg_charges_per_trade: float = 0.0


class AnalyticsEngine:
    """
    Performance analytics for the paper trading system.

    Computes all metrics from a list of closed TradeRecords.
    All metrics based on NET P&L.
    """

    TRADING_DAYS_PER_YEAR = 252
    RISK_FREE_RATE_DAILY = 0.065 / TRADING_DAYS_PER_YEAR  # 6.5% annual

    def compute(self, trades: List[TradeRecord]) -> PerformanceMetrics:
        """Compute all performance metrics from trade list."""
        if not trades:
            return PerformanceMetrics()

        closed = [t for t in trades if t.status == "CLOSED"]
        if not closed:
            return PerformanceMetrics()

        m = PerformanceMetrics()
        m.total_trades = len(closed)

        net_pnls = [t.net_pnl for t in closed]
        gross_pnls = [t.gross_pnl for t in closed]
        charges = [t.total_charges for t in closed]

        winners = [t for t in closed if t.net_pnl > 0]
        losers = [t for t in closed if t.net_pnl <= 0]

        m.winning_trades = len(winners)
        m.losing_trades = len(losers)
        m.win_rate = m.winning_trades / m.total_trades

        m.total_gross_pnl = sum(gross_pnls)
        m.total_charges = sum(charges)
        m.total_net_pnl = sum(net_pnls)

        if winners:
            m.avg_winner = np.mean([t.net_pnl for t in winners])
            m.avg_win_pct = np.mean([t.pnl_pct for t in winners]) * 100
            m.largest_win = max(t.net_pnl for t in winners)

        if losers:
            m.avg_loser = np.mean([t.net_pnl for t in losers])
            m.avg_loss_pct = np.mean([t.pnl_pct for t in losers]) * 100
            m.largest_loss = min(t.net_pnl for t in losers)

        # Profit factor
        gross_wins = sum(t.net_pnl for t in winners) if winners else 0
        gross_losses = abs(sum(t.net_pnl for t in losers)) if losers else 0
        m.profit_factor = gross_wins / gross_losses if gross_losses > 0 else float("inf")

        # Expectancy
        m.expectancy = np.mean(net_pnls)
        if abs(m.avg_loser) > 0:
            m.expectancy_r = (
                m.win_rate * abs(m.avg_winner / m.avg_loser) - (1 - m.win_rate)
            )

        # Drawdown
        equity_curve = self._build_equity_curve(closed, starting_equity=0)
        m.max_drawdown, m.max_drawdown_pct = self._compute_max_drawdown(equity_curve)

        # Consecutive wins/losses
        m.max_consecutive_wins, m.max_consecutive_losses = self._compute_streaks(closed)

        # Hold time
        hold_times = []
        for t in closed:
            if t.entry_time and t.exit_time:
                hold_min = (t.exit_time - t.entry_time).total_seconds() / 60
                hold_times.append(hold_min)
        m.avg_hold_time_min = float(np.mean(hold_times)) if hold_times else 0.0

        # Sharpe ratio (daily returns)
        daily_returns = self._compute_daily_returns(closed)
        m.sharpe_ratio = self._compute_sharpe(daily_returns)
        m.sortino_ratio = self._compute_sortino(daily_returns)

        # Charge impact
        if m.total_gross_pnl != 0:
            m.total_charges_as_pct_of_gross = abs(m.total_charges / m.total_gross_pnl) * 100
        m.avg_charges_per_trade = m.total_charges / m.total_trades if m.total_trades > 0 else 0

        return m

    def _build_equity_curve(
        self,
        trades: List[TradeRecord],
        starting_equity: float = 0.0,
    ) -> List[float]:
        """Build cumulative equity curve from trades."""
        curve = [starting_equity]
        for t in sorted(trades, key=lambda x: x.exit_time or datetime.now()):
            curve.append(curve[-1] + t.net_pnl)
        return curve

    def _compute_max_drawdown(
        self, equity_curve: List[float]
    ) -> Tuple[float, float]:
        """Returns (max_drawdown_rs, max_drawdown_pct)."""
        if len(equity_curve) < 2:
            return 0.0, 0.0

        arr = np.array(equity_curve)
        cummax = np.maximum.accumulate(arr)
        drawdowns = cummax - arr
        max_dd = float(np.max(drawdowns))

        # Percentage drawdown (relative to peak)
        pct_dds = np.where(cummax > 0, drawdowns / cummax, 0)
        max_dd_pct = float(np.max(pct_dds)) if len(pct_dds) > 0 else 0.0

        return max_dd, max_dd_pct

    def _compute_streaks(self, trades: List[TradeRecord]) -> Tuple[int, int]:
        """Returns (max_win_streak, max_loss_streak)."""
        max_wins = max_losses = cur_wins = cur_losses = 0
        for t in trades:
            if t.net_pnl > 0:
                cur_wins += 1
                cur_losses = 0
            else:
                cur_losses += 1
                cur_wins = 0
            max_wins = max(max_wins, cur_wins)
            max_losses = max(max_losses, cur_losses)
        return max_wins, max_losses

    def _compute_daily_returns(self, trades: List[TradeRecord]) -> List[float]:
        """Aggregate net P&L by day."""
        daily: Dict[date, float] = {}
        for t in trades:
            d = t.session_date
            daily[d] = daily.get(d, 0.0) + t.net_pnl
        return list(daily.values())

    def _compute_sharpe(self, daily_returns: List[float]) -> float:
        """Annualized Sharpe ratio."""
        if len(daily_returns) < 5:
            return 0.0
        arr = np.array(daily_returns)
        excess = arr - self.RISK_FREE_RATE_DAILY
        std = np.std(excess, ddof=1)
        if std == 0:
            return 0.0
        return float(np.mean(excess) / std * math.sqrt(self.TRADING_DAYS_PER_YEAR))

    def _compute_sortino(self, daily_returns: List[float]) -> float:
        """Annualized Sortino ratio (downside deviation)."""
        if len(daily_returns) < 5:
            return 0.0
        arr = np.array(daily_returns)
        downside = arr[arr < 0]
        if len(downside) == 0:
            return float("inf")
        downside_std = np.std(downside, ddof=1) if len(downside) > 1 else abs(downside[0])
        if downside_std == 0:
            return float("inf")
        return float(
            np.mean(arr - self.RISK_FREE_RATE_DAILY) / downside_std *
            math.sqrt(self.TRADING_DAYS_PER_YEAR)
        )

    def format_report(self, metrics: PerformanceMetrics) -> str:
        """Format metrics as readable report string."""
        return (
            f"{'='*50}\n"
            f"PERFORMANCE REPORT — AVCS Paper Trading\n"
            f"{'='*50}\n"
            f"Trades:      {metrics.total_trades} "
            f"(W:{metrics.winning_trades} L:{metrics.losing_trades})\n"
            f"Win Rate:    {metrics.win_rate*100:.1f}%\n"
            f"Expectancy:  ₹{metrics.expectancy:+.2f} / trade\n"
            f"Expectancy R:{metrics.expectancy_r:+.2f}R\n"
            f"Profit Factor:{metrics.profit_factor:.2f}\n"
            f"\n"
            f"Avg Winner:  ₹{metrics.avg_winner:+.2f} ({metrics.avg_win_pct:+.1f}%)\n"
            f"Avg Loser:   ₹{metrics.avg_loser:+.2f} ({metrics.avg_loss_pct:+.1f}%)\n"
            f"Largest Win: ₹{metrics.largest_win:+.2f}\n"
            f"Largest Loss:₹{metrics.largest_loss:+.2f}\n"
            f"\n"
            f"Gross P&L:   ₹{metrics.total_gross_pnl:+.2f}\n"
            f"Charges:     ₹{metrics.total_charges:.2f} "
            f"({metrics.total_charges_as_pct_of_gross:.1f}% of gross)\n"
            f"NET P&L:     ₹{metrics.total_net_pnl:+.2f}\n"
            f"\n"
            f"Max Drawdown:₹{metrics.max_drawdown:.2f} "
            f"({metrics.max_drawdown_pct*100:.1f}%)\n"
            f"Max Loss Streak: {metrics.max_consecutive_losses}\n"
            f"Max Win Streak:  {metrics.max_consecutive_wins}\n"
            f"\n"
            f"Sharpe:      {metrics.sharpe_ratio:.2f}\n"
            f"Sortino:     {metrics.sortino_ratio:.2f}\n"
            f"Avg Hold:    {metrics.avg_hold_time_min:.0f} min\n"
            f"{'='*50}"
        )


# Avoid circular import
from datetime import datetime
