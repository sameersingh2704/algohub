"""
ExecutionSelector — Translates a StrategySignal into a concrete option instrument.

Given a direction (CE/PE) and underlying price, selects the best-fitting
option strike based on delta range, spread, OI, and volume filters.

Reuses InstrumentManager.get_atm_options() and get_option_token().
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

from core.strategy.base_strategy import StrategySignal

logger = logging.getLogger(__name__)


@dataclass
class SelectedOption:
    symbol: str
    token: str
    strike: int
    option_type: str        # "CE" | "PE"
    expiry: date
    last_price: float
    delta: Optional[float]
    iv: Optional[float]
    spread_pct: float


class ExecutionSelector:
    """
    Selects the best option instrument for a strategy signal.
    Preference: delta 0.30–0.65, tight spread, adequate OI.
    Falls back up to 1 strike OTM if primary ATM doesn't pass filters.
    """

    # Only NIFTY weekly/monthly options are supported.
    UNDERLYING = "NIFTY"

    def __init__(self, strategy_config: dict) -> None:
        opts = strategy_config.get("options", {})
        self._delta_min: float = opts.get("preferred_delta_min", 0.30)
        self._delta_max: float = opts.get("preferred_delta_max", 0.65)
        self._max_spread_pct: float = opts.get("max_spread_pct", 0.04)
        self._max_spread_abs: float = opts.get("max_spread_absolute", 3.0)
        self._min_oi: int = opts.get("min_oi", 50000)
        self._min_volume: int = opts.get("min_volume_today", 300)
        self._max_strikes_otm: int = opts.get("max_strikes_otm", 1)
        self._strike_interval: int = opts.get("strike_interval", 50)
        self._lot_size: int = opts.get("lot_size", 75)

    def select_instrument(
        self,
        signal: StrategySignal,
        instrument_mgr,
        market_data_engine,
        expiry: date,
    ) -> Optional[SelectedOption]:
        """
        Attempt to find a tradeable option for the given signal direction.

        Returns None if no instrument passes the filters.
        """
        spot = signal.underlying_price
        direction = signal.direction  # "CE" | "PE"

        # Instrument selection is NIFTY-only — all strategies must trade NIFTY options.
        underlying = self.UNDERLYING

        # Start from ATM, try up to max_strikes_otm away
        atm_strike = self._round_to_interval(spot, self._strike_interval)

        candidates = []
        for offset in range(0, self._max_strikes_otm + 1):
            # OTM direction: CE → higher strikes are OTM; PE → lower strikes are OTM
            if direction == "CE":
                strike = atm_strike + offset * self._strike_interval
            else:
                strike = atm_strike - offset * self._strike_interval
            candidates.append(strike)

        for strike in candidates:
            token = instrument_mgr.get_option_token(underlying, strike, direction, expiry)
            if not token:
                continue

            quote = market_data_engine.get_option_quote(token)
            if quote is None:
                # No live quote — accept with placeholder (paper mode early-session)
                logger.debug(f"ExecutionSelector: no quote for {direction} {strike}, accepting without filter")
                info = instrument_mgr.get_instrument(token)
                if info:
                    return SelectedOption(
                        symbol=info.symbol,
                        token=token,
                        strike=strike,
                        option_type=direction,
                        expiry=expiry,
                        last_price=0.0,
                        delta=None,
                        iv=None,
                        spread_pct=0.0,
                    )
                continue

            # Spread filter
            spread_pct = quote.spread_pct
            if quote.ask > 0 and quote.ask - quote.bid > self._max_spread_abs:
                logger.debug(f"ExecutionSelector: {direction} {strike} spread_abs too wide: {quote.ask - quote.bid:.2f}")
                continue
            if spread_pct > self._max_spread_pct:
                logger.debug(f"ExecutionSelector: {direction} {strike} spread_pct={spread_pct:.3f} > {self._max_spread_pct}")
                continue

            # OI filter (skip if not warmed up)
            if quote.oi > 0 and quote.oi < self._min_oi:
                logger.debug(f"ExecutionSelector: {direction} {strike} OI={quote.oi} < {self._min_oi}")
                continue

            # Delta filter
            if quote.delta is not None:
                abs_delta = abs(quote.delta)
                if not (self._delta_min <= abs_delta <= self._delta_max):
                    logger.debug(f"ExecutionSelector: {direction} {strike} delta={abs_delta:.2f} outside [{self._delta_min},{self._delta_max}]")
                    continue

            logger.info(f"ExecutionSelector: selected {direction} {strike} token={token} spread={spread_pct:.3f}")
            return SelectedOption(
                symbol=quote.symbol,
                token=token,
                strike=strike,
                option_type=direction,
                expiry=expiry,
                last_price=quote.ltp,
                delta=quote.delta,
                iv=quote.iv,
                spread_pct=spread_pct,
            )

        logger.warning(f"ExecutionSelector: no valid {direction} instrument found for spot={spot:.0f}")
        return None

    @staticmethod
    def _round_to_interval(price: float, interval: int) -> int:
        return round(price / interval) * interval
