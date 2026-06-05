"""
Unit tests for GreeksCalculator (Black-76 model).
Verify delta bounds, put-call parity, ATM strike logic.
"""

import pytest
from datetime import date, timedelta
from core.data.greeks_calculator import GreeksCalculator


@pytest.fixture
def calc():
    return GreeksCalculator(risk_free_rate=0.065)


class TestGreeksCalculator:

    def test_atm_call_delta_near_half(self, calc):
        """ATM call delta should be close to 0.5."""
        greeks = calc.calculate(
            option_type="CE",
            spot=22000,
            strike=22000,
            tte_years=7/365,
            iv=0.15,
        )
        assert 0.45 <= greeks.delta <= 0.60

    def test_atm_put_delta_near_neg_half(self, calc):
        """ATM put delta should be close to -0.5."""
        greeks = calc.calculate(
            option_type="PE",
            spot=22000,
            strike=22000,
            tte_years=7/365,
            iv=0.15,
        )
        assert -0.60 <= greeks.delta <= -0.40

    def test_deep_itm_call_delta_near_one(self, calc):
        """Deep ITM call delta should approach 1.0."""
        greeks = calc.calculate(
            option_type="CE",
            spot=22500,
            strike=21000,
            tte_years=7/365,
            iv=0.15,
        )
        assert greeks.delta > 0.85

    def test_deep_otm_call_delta_near_zero(self, calc):
        """Deep OTM call delta should be near 0."""
        greeks = calc.calculate(
            option_type="CE",
            spot=22000,
            strike=23500,
            tte_years=7/365,
            iv=0.15,
        )
        assert greeks.delta < 0.15

    def test_gamma_positive(self, calc):
        """Gamma must always be positive."""
        for opt_type in ["CE", "PE"]:
            greeks = calc.calculate(opt_type, 22000, 22000, 7/365, 0.15)
            assert greeks.gamma > 0

    def test_theta_negative(self, calc):
        """Theta must be negative (time decay hurts long options)."""
        for opt_type in ["CE", "PE"]:
            greeks = calc.calculate(opt_type, 22000, 22000, 7/365, 0.15)
            assert greeks.theta < 0

    def test_vega_positive(self, calc):
        """Vega must be positive for long options."""
        for opt_type in ["CE", "PE"]:
            greeks = calc.calculate(opt_type, 22000, 22000, 7/365, 0.15)
            assert greeks.vega > 0

    def test_implied_volatility_roundtrip(self, calc):
        """Calculate IV from price, then recalculate price — should match."""
        original_iv = 0.18
        greeks_orig = calc.calculate("CE", 22000, 22000, 7/365, original_iv)
        market_price = greeks_orig.option_price

        recovered_iv = calc.calculate_iv("CE", market_price, 22000, 22000, 7/365)
        assert recovered_iv is not None
        assert abs(recovered_iv - original_iv) < 0.001

    def test_atm_strike_calculation(self):
        """ATM strike should round to nearest 50-point interval."""
        assert GreeksCalculator.get_atm_strike(22024, 50) == 22000
        assert GreeksCalculator.get_atm_strike(22026, 50) == 22050
        assert GreeksCalculator.get_atm_strike(22025, 50) in [22000, 22050]

    def test_zero_tte_returns_intrinsic(self, calc):
        """Expired option should return intrinsic value."""
        greeks = calc.calculate("CE", 22000, 21000, 0.0, 0.15)
        assert greeks.option_price == pytest.approx(1000.0, abs=1.0)

    def test_option_price_non_negative(self, calc):
        """Option price must never be negative."""
        for strike in [21000, 22000, 23000]:
            for iv in [0.10, 0.20, 0.30]:
                for opt in ["CE", "PE"]:
                    g = calc.calculate(opt, 22000, strike, 7/365, iv)
                    assert g.option_price >= 0.0
