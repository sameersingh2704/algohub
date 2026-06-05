"""
NSE Instrument Master Manager
================================
Downloads and caches Angel One's scrip master for the NFO (NSE F&O) segment.

Angel One publishes a daily JSON instrument master at:
  https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json

This file contains all tradable instruments with their tokens, symbols, lot sizes,
tick sizes, expiry dates, strikes, etc. It is updated daily before market open.

Usage:
    mgr = InstrumentManager()
    await mgr.initialize()

    # Look up a specific option contract
    token = mgr.get_option_token("NIFTY", 22000, "CE", date(2024, 1, 2))

    # Get all NIFTY option strikes for an expiry
    strikes = mgr.get_nifty_option_strikes(date(2024, 1, 2))

    # Get instrument details by token
    info = mgr.get_instrument(token)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import aiohttp

logger = logging.getLogger(__name__)

# Angel One scrip master URL — updated daily by the broker before market open
SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
)

# Cache location — refresh daily
CACHE_DIR = Path("data") / "instrument_cache"
CACHE_FILE = CACHE_DIR / "scrip_master.json"
CACHE_DATE_FILE = CACHE_DIR / "scrip_master_date.txt"

# Segments we care about
NFO_SEGMENT = "NFO"
NSE_SEGMENT = "NSE"

# Well-known tokens that rarely change (but still verified against master on load)
NIFTY_SPOT_SYMBOL = "Nifty 50"
INDIA_VIX_SYMBOL = "India VIX"


@dataclass(frozen=True)
class InstrumentInfo:
    """Normalized instrument record from Angel One scrip master."""
    token: str
    symbol: str              # Angel One symbol (e.g. "NIFTY02JAN24C22000")
    name: str                # Underlying name (e.g. "NIFTY")
    expiry: Optional[date]   # None for indices/equities
    strike: int              # 0 for non-options
    option_type: str         # "CE", "PE", or "" for non-options
    lot_size: int
    tick_size: float
    exchange: str            # "NSE", "NFO", "MCX"
    instrument_type: str     # "OPTIDX", "FUTIDX", "UNDIDX", "EQ", etc.
    exchange_token: str      # Exchange-native token
    isin: str

    @property
    def is_option(self) -> bool:
        return self.option_type in ("CE", "PE")

    @property
    def is_nifty_option(self) -> bool:
        return self.name == "NIFTY" and self.is_option

    @property
    def is_banknifty_option(self) -> bool:
        return self.name == "BANKNIFTY" and self.is_option

    @property
    def is_futures(self) -> bool:
        return self.instrument_type in ("FUTIDX", "FUTSTK")


class InstrumentManager:
    """
    Maintains the Angel One instrument master and provides fast lookups.

    The scrip master is downloaded once per calendar day and cached locally.
    All lookups are O(1) via pre-built hash maps.

    Key lookup tables built on initialization:
      _token_map:         token → InstrumentInfo
      _option_key_map:    (name, strike, option_type, expiry) → token
      _expiry_strikes:    (name, expiry) → sorted list of strikes
      _symbol_map:        raw symbol string → token
    """

    def __init__(self, cache_dir: Optional[Path] = None) -> None:
        self._cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR
        self._token_map: Dict[str, InstrumentInfo] = {}
        self._option_key_map: Dict[Tuple, str] = {}
        self._expiry_strikes: Dict[Tuple[str, date], List[int]] = {}
        self._symbol_map: Dict[str, str] = {}  # symbol → token
        self._nifty_expiries: List[date] = []
        self._banknifty_expiries: List[date] = []
        self._nifty_spot_token: Optional[str] = None
        self._vix_token: Optional[str] = None
        self._initialized: bool = False

    async def initialize(self, force_refresh: bool = False) -> None:
        """
        Load instrument master. Downloads fresh copy if cache is stale (different date).
        Call once at startup, before any subscriptions are made.
        """
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        if not force_refresh and self._is_cache_fresh():
            logger.info("Instrument master: loading from cache")
            await self._load_from_cache()
        else:
            logger.info("Instrument master: downloading fresh copy")
            await self._download_and_cache()

        self._initialized = True
        logger.info(
            f"Instrument master loaded: {len(self._token_map)} instruments, "
            f"{len(self._option_key_map)} option contracts, "
            f"NIFTY expiries: {len(self._nifty_expiries)}"
        )

    def _is_cache_fresh(self) -> bool:
        """Return True if cache was downloaded today."""
        if not CACHE_FILE.exists() or not CACHE_DATE_FILE.exists():
            return False
        try:
            cache_date_str = CACHE_DATE_FILE.read_text().strip()
            cache_date = date.fromisoformat(cache_date_str)
            return cache_date == date.today()
        except Exception:
            return False

    async def _download_and_cache(self) -> None:
        """Download scrip master from Angel One CDN and cache it."""
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(SCRIP_MASTER_URL) as resp:
                    resp.raise_for_status()
                    raw = await resp.json(content_type=None)

            logger.info(f"Downloaded {len(raw)} instrument records")
            CACHE_FILE.write_text(json.dumps(raw))
            CACHE_DATE_FILE.write_text(date.today().isoformat())

            self._parse_and_index(raw)

        except aiohttp.ClientError as e:
            logger.error(f"Failed to download scrip master: {e}")
            # Fall back to cache if available, even if stale
            if CACHE_FILE.exists():
                logger.warning("Falling back to stale instrument cache")
                await self._load_from_cache()
            else:
                raise RuntimeError(
                    "No instrument master available — cannot start trading. "
                    "Check network and retry."
                ) from e

    async def _load_from_cache(self) -> None:
        """Load instrument master from local cache file."""
        raw = json.loads(CACHE_FILE.read_text())
        self._parse_and_index(raw)

    def _parse_and_index(self, records: List[dict]) -> None:
        """
        Parse raw scrip master records and build lookup tables.

        Angel One scrip master record fields (key ones):
          token, symbol, name, expiry, strike, lotsize, instrumenttype,
          exch_seg, tick_size, exchange_token, isin
        """
        self._token_map.clear()
        self._option_key_map.clear()
        self._expiry_strikes.clear()
        self._symbol_map.clear()

        nifty_expiry_set: Set[date] = set()
        banknifty_expiry_set: Set[date] = set()

        skipped = 0
        for rec in records:
            try:
                info = self._parse_record(rec)
                if info is None:
                    skipped += 1
                    continue

                self._token_map[info.token] = info
                self._symbol_map[info.symbol] = info.token

                # Index options for fast strike lookup
                if info.is_option and info.expiry:
                    key = (info.name, info.strike, info.option_type, info.expiry)
                    self._option_key_map[key] = info.token

                    exp_key = (info.name, info.expiry)
                    if exp_key not in self._expiry_strikes:
                        self._expiry_strikes[exp_key] = []
                    strikes_list = self._expiry_strikes[exp_key]
                    if info.strike not in strikes_list:
                        strikes_list.append(info.strike)

                    # Collect expiries per underlying
                    if info.name == "NIFTY":
                        nifty_expiry_set.add(info.expiry)
                    elif info.name == "BANKNIFTY":
                        banknifty_expiry_set.add(info.expiry)

                # Spot & VIX token detection (NSE segment)
                if info.exchange == "NSE":
                    if info.name == "Nifty 50" or info.symbol == "Nifty 50":
                        self._nifty_spot_token = info.token
                    elif info.name == "India VIX" or info.symbol == "India VIX":
                        self._vix_token = info.token

            except Exception as e:
                skipped += 1
                logger.debug(f"Skipped record {rec.get('token')}: {e}")

        # Sort strike lists
        for key in self._expiry_strikes:
            self._expiry_strikes[key].sort()

        # Sort expiries chronologically
        self._nifty_expiries = sorted(nifty_expiry_set)
        self._banknifty_expiries = sorted(banknifty_expiry_set)

        if skipped > 0:
            logger.debug(f"Skipped {skipped} unparseable records (normal for cash segment)")

    def _parse_record(self, rec: dict) -> Optional[InstrumentInfo]:
        """Parse a single scrip master record. Returns None if record should be skipped."""
        token = str(rec.get("token", "")).strip()
        if not token:
            return None

        exchange = str(rec.get("exch_seg", "")).strip()
        instrument_type = str(rec.get("instrumenttype", "")).strip()
        symbol = str(rec.get("symbol", "")).strip()
        name = str(rec.get("name", "")).strip()

        # Expiry parsing — format varies: "02JAN2024" or "2024-01-02"
        expiry: Optional[date] = None
        expiry_raw = str(rec.get("expiry", "")).strip()
        if expiry_raw:
            expiry = self._parse_expiry(expiry_raw)

        # Strike — stored as string "2200000" meaning 22000.00
        strike_raw = rec.get("strike", "0") or "0"
        try:
            strike_float = float(str(strike_raw).strip())
            # Angel One stores strike * 100 for some instruments; normalize
            strike = int(strike_float)
            if strike > 1_000_000:
                strike = strike // 100
        except (ValueError, TypeError):
            strike = 0

        # Option type — from instrumenttype or explicit field
        option_type = ""
        if instrument_type == "OPTIDX":
            # Symbol format: "NIFTY02JAN24C22000" — last char before strike is C/P
            # Or use the optiontype field if present
            opt_raw = str(rec.get("optiontype", "")).strip()
            if opt_raw in ("CE", "PE"):
                option_type = opt_raw
            else:
                # Try to extract from symbol
                option_type = self._extract_option_type(symbol)

        lot_size = int(rec.get("lotsize", 1) or 1)
        tick_size_raw = rec.get("tick_size", "0.05") or "0.05"
        try:
            tick_size = float(str(tick_size_raw).strip())
        except (ValueError, TypeError):
            tick_size = 0.05

        return InstrumentInfo(
            token=token,
            symbol=symbol,
            name=name,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            lot_size=lot_size,
            tick_size=tick_size,
            exchange=exchange,
            instrument_type=instrument_type,
            exchange_token=str(rec.get("exchange_token", "")).strip(),
            isin=str(rec.get("isin", "")).strip(),
        )

    @staticmethod
    def _parse_expiry(raw: str) -> Optional[date]:
        """
        Parse Angel One expiry string. Observed formats:
          "02JAN2024"  → date(2024, 1, 2)
          "2024-01-02" → date(2024, 1, 2)
          ""           → None
        """
        raw = raw.strip()
        if not raw:
            return None

        formats = [
            "%d%b%Y",   # 02JAN2024
            "%Y-%m-%d", # 2024-01-02
            "%d-%b-%Y", # 02-JAN-2024
        ]
        for fmt in formats:
            try:
                return datetime.strptime(raw.upper(), fmt).date()
            except ValueError:
                continue

        logger.debug(f"Cannot parse expiry: '{raw}'")
        return None

    @staticmethod
    def _extract_option_type(symbol: str) -> str:
        """
        Extract CE/PE from Angel symbols.

        Seen formats include both:
          - "NIFTY02JAN24C22000"  (older style)
          - "NIFTY09JUN2621200CE" (current scrip master style)
        """
        symbol = symbol.strip().upper()
        if symbol.endswith("CE"):
            return "CE"
        if symbol.endswith("PE"):
            return "PE"

        # Look for C/P followed only by digits to the end of the symbol.
        match = re.search(r"([CP])\d+$", symbol)
        if match:
            return "CE" if match.group(1) == "C" else "PE"
        return ""

    # ──────────────────────────────────────────────
    # Public lookup API
    # ──────────────────────────────────────────────

    def get_instrument(self, token: str) -> Optional[InstrumentInfo]:
        """Return InstrumentInfo for a token. None if unknown."""
        return self._token_map.get(token)

    def get_option_token(
        self,
        name: str,
        strike: int,
        option_type: str,
        expiry: date,
    ) -> Optional[str]:
        """
        Return the Angel One token for a specific option contract.

        Args:
            name:        Underlying name, e.g. "NIFTY"
            strike:      Integer strike, e.g. 22000
            option_type: "CE" or "PE"
            expiry:      Expiry date

        Returns:
            Token string, or None if not found.
        """
        return self._option_key_map.get((name, strike, option_type, expiry))

    def get_nifty_option_strikes(self, expiry: date) -> List[int]:
        """Return sorted list of available NIFTY option strikes for an expiry."""
        return self._expiry_strikes.get(("NIFTY", expiry), [])

    def get_atm_options(
        self, spot: float, expiry: date, num_strikes: int = 5
    ) -> List[InstrumentInfo]:
        """
        Return InstrumentInfo for ATM ± num_strikes strikes (CE + PE each) for NIFTY.

        Finds the ATM strike (nearest 50-pt interval to spot), then returns
        the surrounding strike ladder.
        """
        atm = GreeksCalculator_atm(spot, 50)
        strikes = self.get_nifty_option_strikes(expiry)
        if not strikes:
            return []

        # Find strikes within ±num_strikes of ATM
        atm_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - atm))
        lo = max(0, atm_idx - num_strikes)
        hi = min(len(strikes), atm_idx + num_strikes + 1)
        selected_strikes = strikes[lo:hi]

        results: List[InstrumentInfo] = []
        for strike in selected_strikes:
            for opt_type in ("CE", "PE"):
                token = self.get_option_token("NIFTY", strike, opt_type, expiry)
                if token:
                    info = self._token_map.get(token)
                    if info:
                        results.append(info)
        return results

    def get_nifty_spot_token(self) -> Optional[str]:
        """Return the token for NIFTY 50 index (NSE cash segment)."""
        return self._nifty_spot_token

    def get_vix_token(self) -> Optional[str]:
        """Return the token for India VIX."""
        return self._vix_token

    def get_next_nifty_expiry(self, from_date: Optional[date] = None) -> Optional[date]:
        """
        Return the nearest upcoming NIFTY weekly expiry (Tuesday).

        Args:
            from_date: Reference date. Defaults to today.
        """
        ref = from_date or date.today()
        for expiry in self._nifty_expiries:
            if expiry >= ref:
                return expiry
        return None

    def get_nifty_weekly_expiries(
        self, from_date: Optional[date] = None, count: int = 4
    ) -> List[date]:
        """Return the next N weekly NIFTY expiries from from_date."""
        ref = from_date or date.today()
        return [e for e in self._nifty_expiries if e >= ref][:count]

    def get_nearest_future(
        self, name: str = "NIFTY", from_date: Optional[date] = None
    ) -> Optional[InstrumentInfo]:
        """Return the nearest FUTIDX contract for an underlying from a date."""
        ref = from_date or date.today()
        futures = [
            info for info in self._token_map.values()
            if info.name == name and info.instrument_type == "FUTIDX" and info.expiry
            and info.expiry >= ref
        ]
        return min(futures, key=lambda info: info.expiry) if futures else None

    def get_token_by_symbol(self, symbol: str) -> Optional[str]:
        """Look up token by exact symbol string."""
        return self._symbol_map.get(symbol)

    def get_all_nifty_options_for_expiry(
        self, expiry: date
    ) -> List[InstrumentInfo]:
        """Return all NIFTY CE and PE contracts for a given expiry."""
        strikes = self.get_nifty_option_strikes(expiry)
        result = []
        for strike in strikes:
            for opt_type in ("CE", "PE"):
                token = self.get_option_token("NIFTY", strike, opt_type, expiry)
                if token and token in self._token_map:
                    result.append(self._token_map[token])
        return result

    def get_lot_size(self, name: str = "NIFTY") -> int:
        """
        Return lot size for the underlying.
        Falls back to known NIFTY lot size (65) if not found.
        """
        # Find from any option contract for this underlying
        for key, token in self._option_key_map.items():
            if key[0] == name:
                info = self._token_map.get(token)
                if info and info.lot_size > 0:
                    return info.lot_size
        logger.warning(f"Lot size not found for {name}, defaulting to 65")
        return 65

    def is_valid_token(self, token: str) -> bool:
        """Return True if token exists in the instrument master."""
        return token in self._token_map

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    @property
    def nifty_expiries(self) -> List[date]:
        return list(self._nifty_expiries)

    @property
    def total_instruments(self) -> int:
        return len(self._token_map)

    @property
    def total_nifty_options(self) -> int:
        return sum(
            1 for info in self._token_map.values()
            if info.is_nifty_option
        )


def GreeksCalculator_atm(spot: float, interval: int = 50) -> int:
    """Round spot to nearest strike interval."""
    return round(spot / interval) * interval
