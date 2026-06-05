"""
Options Greeks Calculator
==========================
Implements Black-76 model for index options (futures-based).
Calculates: IV, Delta, Gamma, Theta, Vega.

Black-76 is the correct model for index options (not Black-Scholes)
because NIFTY options are settled against futures price.

All functions are pure (no side effects) for easy testing.
"""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq

logger = logging.getLogger(__name__)

N = norm.cdf    # Cumulative distribution function
n = norm.pdf    # Probability density function


@dataclass
class Greeks:
    """Container for all computed option Greeks."""
    delta: float        # Directional sensitivity (-1 to +1)
    gamma: float        # Delta sensitivity (always positive)
    theta: float        # Time decay per day (negative for long)
    vega: float         # IV sensitivity per 1% change
    rho: float          # Interest rate sensitivity
    iv: float           # Implied volatility (annualized)
    option_price: float # Theoretical price


class GreeksCalculator:
    """
    Black-76 options pricer and Greeks calculator for Indian index options.

    Usage:
        calc = GreeksCalculator(risk_free_rate=0.065)  # RBI repo rate ~6.5%
        greeks = calc.calculate(
            option_type="CE",
            spot=22000,
            strike=22000,
            tte_years=0.05,   # time to expiry in years
            iv=0.15,          # 15% annualized IV
        )
    """

    def __init__(self, risk_free_rate: float = 0.065) -> None:
        """
        risk_free_rate: Annual risk-free rate. Use RBI repo rate (~6.5% as of 2024).
        """
        self._r = risk_free_rate

    def calculate(
        self,
        option_type: str,
        spot: float,
        strike: float,
        tte_years: float,
        iv: float,
    ) -> Greeks:
        """
        Calculate option price and all Greeks using Black-76.

        Args:
            option_type: "CE" or "PE"
            spot: Current spot price of underlying (treat as futures price)
            strike: Option strike price
            tte_years: Time to expiry in years (e.g., 7/252 for 7 trading days)
            iv: Implied volatility as decimal (0.15 = 15%)

        Returns:
            Greeks dataclass with all values.
        """
        if tte_years <= 0:
            return self._expired_greeks(option_type, spot, strike)

        if iv <= 0 or iv > 5.0:
            iv = 0.001  # Fallback for invalid IV

        F = spot  # Forward price (use spot as proxy for cash-settled index)
        K = strike
        T = tte_years
        r = self._r
        sigma = iv

        sqrt_T = math.sqrt(T)
        d1 = (math.log(F / K) + 0.5 * sigma**2 * T) / (sigma * sqrt_T)
        d2 = d1 - sigma * sqrt_T

        discount = math.exp(-r * T)

        if option_type.upper() == "CE":
            price = discount * (F * N(d1) - K * N(d2))
            delta = discount * N(d1)
            rho = -T * price  # Simplified
        else:  # PE
            price = discount * (K * N(-d2) - F * N(-d1))
            delta = discount * (N(d1) - 1)
            rho = -T * price

        gamma = discount * n(d1) / (F * sigma * sqrt_T)
        vega = F * discount * n(d1) * sqrt_T / 100  # Per 1% IV change
        theta = (
            -(F * discount * n(d1) * sigma / (2 * sqrt_T)) -
            r * price * discount
        ) / 365  # Per calendar day

        return Greeks(
            delta=round(delta, 6),
            gamma=round(gamma, 8),
            theta=round(theta, 4),
            vega=round(vega, 4),
            rho=round(rho, 4),
            iv=iv,
            option_price=round(max(price, 0.0), 2),
        )

    def calculate_iv(
        self,
        option_type: str,
        market_price: float,
        spot: float,
        strike: float,
        tte_years: float,
        tolerance: float = 1e-5,
    ) -> Optional[float]:
        """
        Calculate Implied Volatility from market price using Brent's method.

        Returns IV as decimal (e.g., 0.15 = 15%), or None if IV cannot be found.
        """
        if tte_years <= 1e-6 or market_price <= 0:
            return None

        intrinsic = max(0.0, spot - strike) if option_type == "CE" else max(0.0, strike - spot)
        if market_price < intrinsic * 0.99:
            return None  # Below intrinsic — bad market price

        def objective(sigma: float) -> float:
            greeks = self.calculate(option_type, spot, strike, tte_years, sigma)
            return greeks.option_price - market_price

        try:
            iv = brentq(objective, 0.001, 5.0, xtol=tolerance, maxiter=100)
            return round(iv, 6)
        except ValueError:
            logger.debug(
                f"IV calculation failed: {option_type} spot={spot} strike={strike} "
                f"price={market_price} tte={tte_years:.4f}"
            )
            return None

    def calculate_greeks_from_market_price(
        self,
        option_type: str,
        market_price: float,
        spot: float,
        strike: float,
        tte_years: float,
    ) -> Optional[Greeks]:
        """
        Calculate all Greeks from market price (first computes IV, then Greeks).
        Returns None if IV cannot be determined.
        """
        iv = self.calculate_iv(option_type, market_price, spot, strike, tte_years)
        if iv is None:
            return None
        return self.calculate(option_type, spot, strike, tte_years, iv)

    def _expired_greeks(self, option_type: str, spot: float, strike: float) -> Greeks:
        """Greeks for an expired option (tte=0)."""
        intrinsic = max(0.0, spot - strike) if option_type == "CE" else max(0.0, strike - spot)
        delta = 1.0 if (option_type == "CE" and spot > strike) else (
            -1.0 if (option_type == "PE" and spot < strike) else 0.5
        )
        return Greeks(
            delta=delta,
            gamma=0.0,
            theta=0.0,
            vega=0.0,
            rho=0.0,
            iv=0.0,
            option_price=intrinsic,
        )

    @staticmethod
    def time_to_expiry_years(expiry_date, current_datetime=None) -> float:
        """
        Calculate time to expiry in years.
        Uses calendar days / 365 (standard for index options).
        """
        from datetime import datetime, date
        if current_datetime is None:
            current_datetime = datetime.now()

        if isinstance(expiry_date, date) and not isinstance(expiry_date, datetime):
            expiry_datetime = datetime.combine(expiry_date, datetime.min.time().replace(hour=15, minute=30))
        else:
            expiry_datetime = expiry_date

        delta = expiry_datetime - current_datetime
        tte_years = max(0.0, delta.total_seconds() / (365 * 24 * 3600))
        return tte_years

    @staticmethod
    def get_atm_strike(spot: float, strike_interval: int = 50) -> int:
        """Return the ATM strike closest to spot."""
        return int(round(spot / strike_interval) * strike_interval)

    @staticmethod
    def get_strike_ladder(
        spot: float,
        strike_interval: int = 50,
        num_strikes: int = 5,
    ) -> list[int]:
        """
        Return a list of strikes centered on ATM.
        num_strikes on each side.
        """
        atm = GreeksCalculator.get_atm_strike(spot, strike_interval)
        strikes = []
        for i in range(-num_strikes, num_strikes + 1):
            strikes.append(atm + i * strike_interval)
        return sorted(strikes)
