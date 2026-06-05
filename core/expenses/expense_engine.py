"""
Expense Engine
===============
Calculates all Indian options trading charges with accuracy.

Charge types (as of 2024):
1. Brokerage: Flat ₹20 per order (Angel One)
2. STT: 0.05% on sell-side premium
3. NSE Exchange transaction charge: 0.053% on premium turnover
4. GST: 18% on (brokerage + exchange charges)
5. SEBI regulatory fee: ₹10 per crore of turnover
6. Stamp duty: 0.003% on buy-side premium (buyer pays)
7. IPFT: 0.001% on premium (small)

Critical: Net P&L = Gross P&L - Total Charges
NEVER evaluate strategy on gross P&L alone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class TradeCharges:
    """Complete charge breakdown for a single round-trip trade."""
    trade_id: str
    session_date: date

    # Per-side charges
    buy_brokerage: float = 0.0
    sell_brokerage: float = 0.0
    buy_stt: float = 0.0
    sell_stt: float = 0.0
    buy_exchange_txn: float = 0.0
    sell_exchange_txn: float = 0.0
    buy_gst: float = 0.0
    sell_gst: float = 0.0
    buy_sebi: float = 0.0
    sell_sebi: float = 0.0
    buy_stamp: float = 0.0
    sell_stamp: float = 0.0
    buy_ipft: float = 0.0
    sell_ipft: float = 0.0

    # Turnover
    buy_turnover: float = 0.0
    sell_turnover: float = 0.0

    @property
    def total_brokerage(self) -> float:
        return self.buy_brokerage + self.sell_brokerage

    @property
    def total_stt(self) -> float:
        return self.buy_stt + self.sell_stt

    @property
    def total_exchange_txn(self) -> float:
        return self.buy_exchange_txn + self.sell_exchange_txn

    @property
    def total_gst(self) -> float:
        return self.buy_gst + self.sell_gst

    @property
    def total_sebi(self) -> float:
        return self.buy_sebi + self.sell_sebi

    @property
    def total_stamp(self) -> float:
        return self.buy_stamp + self.sell_stamp

    @property
    def total_ipft(self) -> float:
        return self.buy_ipft + self.sell_ipft

    @property
    def total_charges(self) -> float:
        return (
            self.total_brokerage +
            self.total_stt +
            self.total_exchange_txn +
            self.total_gst +
            self.total_sebi +
            self.total_stamp +
            self.total_ipft
        )

    @property
    def charges_summary(self) -> Dict[str, float]:
        return {
            "brokerage": round(self.total_brokerage, 2),
            "stt": round(self.total_stt, 2),
            "exchange_txn": round(self.total_exchange_txn, 2),
            "gst": round(self.total_gst, 2),
            "sebi": round(self.total_sebi, 2),
            "stamp": round(self.total_stamp, 2),
            "ipft": round(self.total_ipft, 2),
            "total": round(self.total_charges, 2),
        }


@dataclass
class DailyChargesSummary:
    """Aggregated charge summary for a trading session."""
    session_date: date
    trade_count: int = 0
    total_brokerage: float = 0.0
    total_stt: float = 0.0
    total_exchange_txn: float = 0.0
    total_gst: float = 0.0
    total_sebi: float = 0.0
    total_stamp: float = 0.0
    total_ipft: float = 0.0

    @property
    def total_charges(self) -> float:
        return (
            self.total_brokerage + self.total_stt + self.total_exchange_txn +
            self.total_gst + self.total_sebi + self.total_stamp + self.total_ipft
        )


class ExpenseEngine:
    """
    Calculates exact trading charges for NIFTY options trades.

    Usage:
        engine = ExpenseEngine(config)
        charges = engine.calculate_round_trip(
            trade_id="abc",
            buy_price=100.0,
            sell_price=140.0,
            quantity=65,
        )
        net_pnl = gross_pnl - charges.total_charges
    """

    def __init__(self, expense_config: dict) -> None:
        cfg = expense_config["charges"]
        self._brokerage_flat = cfg["brokerage_flat_per_order"]
        self._stt_sell_pct = cfg["stt_sell_pct"]
        self._exchange_txn_pct = cfg["nse_exchange_txn_pct"]
        self._gst_pct = cfg["gst_pct"]
        self._sebi_per_crore = cfg["sebi_fee_per_crore"]
        self._stamp_buy_pct = cfg["stamp_duty_buy_pct"]
        self._ipft_pct = cfg["ipft_pct"]

        self._daily_charges: Dict[date, DailyChargesSummary] = {}
        self._all_trade_charges: Dict[str, TradeCharges] = {}

    def calculate_round_trip(
        self,
        trade_id: str,
        buy_price: float,
        sell_price: float,
        quantity: int,
        session_date: Optional[date] = None,
    ) -> TradeCharges:
        """
        Calculate all charges for a complete round-trip trade.

        Args:
            trade_id: Unique trade identifier
            buy_price: Entry price (average buy price)
            sell_price: Exit price (average sell price)
            quantity: Total number of units (lots × lot_size)
            session_date: Trade date (defaults to today)

        Returns:
            TradeCharges with complete breakdown
        """
        if session_date is None:
            session_date = date.today()

        buy_turnover = buy_price * quantity
        sell_turnover = sell_price * quantity

        charges = TradeCharges(
            trade_id=trade_id,
            session_date=session_date,
            buy_turnover=buy_turnover,
            sell_turnover=sell_turnover,
        )

        # ─── BUY SIDE ─────────────────────────────────
        charges.buy_brokerage = self._brokerage_flat

        # STT on options: ONLY on SELL side (not buy side)
        charges.buy_stt = 0.0

        # Exchange transaction charge on BUY
        charges.buy_exchange_txn = buy_turnover * self._exchange_txn_pct

        # GST on brokerage + exchange charges (BUY side)
        charges.buy_gst = (charges.buy_brokerage + charges.buy_exchange_txn) * self._gst_pct

        # SEBI fee (BUY side)
        charges.buy_sebi = (buy_turnover / 1_00_00_000) * self._sebi_per_crore

        # Stamp duty on BUY side only
        charges.buy_stamp = buy_turnover * self._stamp_buy_pct

        # IPFT (BUY side)
        charges.buy_ipft = buy_turnover * self._ipft_pct

        # ─── SELL SIDE ────────────────────────────────
        charges.sell_brokerage = self._brokerage_flat

        # STT on SELL side: 0.05% on sell turnover (options)
        charges.sell_stt = sell_turnover * self._stt_sell_pct

        # Exchange transaction charge on SELL
        charges.sell_exchange_txn = sell_turnover * self._exchange_txn_pct

        # GST on brokerage + exchange charges (SELL side)
        charges.sell_gst = (charges.sell_brokerage + charges.sell_exchange_txn) * self._gst_pct

        # SEBI fee (SELL side)
        charges.sell_sebi = (sell_turnover / 1_00_00_000) * self._sebi_per_crore

        # No stamp on sell side
        charges.sell_stamp = 0.0

        # IPFT (SELL side)
        charges.sell_ipft = sell_turnover * self._ipft_pct

        # Store for aggregation
        self._all_trade_charges[trade_id] = charges
        self._aggregate_daily(session_date, charges)

        logger.info(
            f"Charges calculated for trade {trade_id}: "
            f"Total=₹{charges.total_charges:.2f} "
            f"[Brk=₹{charges.total_brokerage:.2f} "
            f"STT=₹{charges.total_stt:.2f} "
            f"ExchTxn=₹{charges.total_exchange_txn:.2f} "
            f"GST=₹{charges.total_gst:.2f} "
            f"Stamp=₹{charges.total_stamp:.2f}]"
        )

        return charges

    def estimate_charges(
        self,
        premium: float,
        quantity: int,
        expected_pnl_pct: float = 0.50,
    ) -> Dict[str, float]:
        """
        Estimate charges before trade entry for viability check.
        Uses expected exit price based on P&L percentage.

        Returns dict with estimated charges and breakeven info.
        """
        buy_price = premium
        sell_price = premium * (1 + expected_pnl_pct)

        charges = self.calculate_round_trip(
            trade_id="__estimate__",
            buy_price=buy_price,
            sell_price=sell_price,
            quantity=quantity,
        )

        gross_pnl = (sell_price - buy_price) * quantity
        net_pnl = gross_pnl - charges.total_charges
        breakeven_move = charges.total_charges / (quantity if quantity > 0 else 1)
        charges_as_pct_of_premium = (charges.total_charges / (buy_price * quantity)) * 100 if buy_price > 0 else 0

        # Remove the estimate from tracking
        self._all_trade_charges.pop("__estimate__", None)

        return {
            "buy_price": round(buy_price, 2),
            "sell_price": round(sell_price, 2),
            "gross_pnl": round(gross_pnl, 2),
            "total_charges": round(charges.total_charges, 2),
            "net_pnl": round(net_pnl, 2),
            "breakeven_premium_move": round(breakeven_move, 2),
            "charges_as_pct_of_premium": round(charges_as_pct_of_premium, 2),
            "breakdown": charges.charges_summary,
        }

    def _aggregate_daily(self, session_date: date, charges: TradeCharges) -> None:
        """Aggregate charges into daily summary."""
        if session_date not in self._daily_charges:
            self._daily_charges[session_date] = DailyChargesSummary(session_date=session_date)

        daily = self._daily_charges[session_date]
        daily.trade_count += 1
        daily.total_brokerage += charges.total_brokerage
        daily.total_stt += charges.total_stt
        daily.total_exchange_txn += charges.total_exchange_txn
        daily.total_gst += charges.total_gst
        daily.total_sebi += charges.total_sebi
        daily.total_stamp += charges.total_stamp
        daily.total_ipft += charges.total_ipft

    def get_daily_charges(self, session_date: date) -> Optional[DailyChargesSummary]:
        """Get aggregated charges for a specific date."""
        return self._daily_charges.get(session_date)

    def get_trade_charges(self, trade_id: str) -> Optional[TradeCharges]:
        """Get charges for a specific trade."""
        return self._all_trade_charges.get(trade_id)

    def get_charges_summary(self) -> Dict[str, float]:
        """Get all-time charges summary."""
        totals = {
            "brokerage": 0.0, "stt": 0.0, "exchange_txn": 0.0,
            "gst": 0.0, "sebi": 0.0, "stamp": 0.0, "ipft": 0.0, "total": 0.0
        }
        for charges in self._all_trade_charges.values():
            s = charges.charges_summary
            for k in totals:
                totals[k] += s.get(k, 0.0)
        return {k: round(v, 2) for k, v in totals.items()}
