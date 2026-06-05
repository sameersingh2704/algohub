"""
Unit tests for ExpenseEngine.
These numbers must be verified against real broker calculations.
"""

import pytest
from datetime import date
from core.expenses.expense_engine import ExpenseEngine


EXPENSE_CONFIG = {
    "charges": {
        "brokerage_flat_per_order": 20.00,
        "stt_sell_pct": 0.0005,
        "nse_exchange_txn_pct": 0.00053,
        "gst_pct": 0.18,
        "sebi_fee_per_crore": 10.0,
        "stamp_duty_buy_pct": 0.00003,
        "ipft_pct": 0.000001,
        "dp_charges_per_trade": 0.0,
    }
}


@pytest.fixture
def engine():
    return ExpenseEngine(EXPENSE_CONFIG)


class TestExpenseEngine:
    """Test charge calculations against known formulas."""

    def test_brokerage_flat_per_side(self, engine):
        """Angel One charges Rs.20 flat per order — Rs.40 round trip."""
        charges = engine.calculate_round_trip(
            trade_id="test1",
            buy_price=100.0,
            sell_price=100.0,
            quantity=65,
        )
        assert charges.total_brokerage == pytest.approx(40.0, abs=0.01)

    def test_stt_only_on_sell(self, engine):
        """STT must be 0 on buy side, 0.05% on sell side premium."""
        charges = engine.calculate_round_trip(
            trade_id="test2",
            buy_price=100.0,
            sell_price=100.0,
            quantity=65,
        )
        sell_turnover = 100.0 * 65
        expected_stt = sell_turnover * 0.0005
        assert charges.buy_stt == 0.0
        assert charges.sell_stt == pytest.approx(expected_stt, abs=0.01)

    def test_stamp_only_on_buy(self, engine):
        """Stamp duty must only be on buy side."""
        charges = engine.calculate_round_trip(
            trade_id="test3",
            buy_price=100.0,
            sell_price=100.0,
            quantity=65,
        )
        buy_turnover = 100.0 * 65
        expected_stamp = buy_turnover * 0.00003
        assert charges.buy_stamp == pytest.approx(expected_stamp, abs=0.001)
        assert charges.sell_stamp == 0.0

    def test_realistic_trade_charges(self, engine):
        """
        Verify charges on a typical trade:
        - Entry: NIFTY ATM CE @ Rs.100
        - Exit: Rs.150 (50% profit)
        - 1 lot = 65 qty
        """
        charges = engine.calculate_round_trip(
            trade_id="real_trade",
            buy_price=100.0,
            sell_price=150.0,
            quantity=65,
        )
        # Brokerage: Rs.40
        assert charges.total_brokerage == pytest.approx(40.0, abs=0.01)
        # STT sell: 0.05% * 150 * 65 = Rs.4.875
        assert charges.sell_stt == pytest.approx(4.875, abs=0.01)
        # Total should be meaningful but not excessive vs profit
        gross_pnl = (150 - 100) * 65  # Rs. 3250
        assert charges.total_charges < gross_pnl * 0.1  # Charges < 10% of gross

    def test_net_pnl_less_than_gross(self, engine):
        """Net P&L must always be less than gross P&L."""
        charges = engine.calculate_round_trip(
            trade_id="pnl_test",
            buy_price=80.0,
            sell_price=120.0,
            quantity=65,
        )
        gross_pnl = (120 - 80) * 65
        net_pnl = gross_pnl - charges.total_charges
        assert net_pnl < gross_pnl
        assert charges.total_charges > 0

    def test_loss_trade_charges(self, engine):
        """Charges must still be calculated correctly on losing trades."""
        charges = engine.calculate_round_trip(
            trade_id="loss_trade",
            buy_price=100.0,
            sell_price=65.0,  # -35% stop hit
            quantity=65,
        )
        assert charges.total_charges > 0
        assert charges.total_brokerage == pytest.approx(40.0, abs=0.01)
        # STT on sell: 0.05% * 65 * 65 = Rs.2.1125
        assert charges.sell_stt == pytest.approx(65 * 65 * 0.0005, abs=0.01)

    def test_estimate_roundtrip(self, engine):
        """estimate_charges should return positive breakeven point."""
        est = engine.estimate_charges(
            premium=100.0,
            quantity=65,
            expected_pnl_pct=0.50,
        )
        assert est["total_charges"] > 0
        assert est["gross_pnl"] > est["total_charges"]
        assert est["net_pnl"] > 0
        assert est["breakeven_premium_move"] > 0
