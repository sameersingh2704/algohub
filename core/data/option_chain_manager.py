"""
Option Chain Manager
======================
Provides periodic (60-second) REST refresh of the NIFTY option chain and
derives sentiment indicators consumed by SignalEngine:

  - PCR (Put-Call Ratio): total PE OI / total CE OI across the full chain
  - Per-strike intraday OI change: current OI − opening OI
  - Max pain strike (sum of max payout for all strikes)
  - OI concentration (top-3 CE and PE strikes by OI)

Design:
  - One background asyncio task polls the broker REST API every 60 seconds
  - Results are stored as immutable snapshots; reads are non-blocking
  - Tuesday expiry mode activates after 12:00 PM for tighter gamma monitoring

Usage:
    mgr = OptionChainManager(connector, instrument_manager, expiry)
    await mgr.start()
    ...
    snapshot = mgr.latest_snapshot   # access at any time, thread-safe
    await mgr.stop()
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from typing import Dict, List, Optional, Tuple

from core.broker.angel_connector import AngelConnector
from core.data.instrument_manager import InstrumentManager

logger = logging.getLogger(__name__)

# ─── Data Structures ───────────────────────────────────────

@dataclass
class StrikeData:
    """OI, volume, and bid/ask for a single strike + option type."""
    strike: int
    option_type: str          # "CE" or "PE"
    token: str
    ltp: float
    bid: float
    ask: float
    oi: int
    oi_change: int            # current OI − opening OI (intraday flow)
    volume: int
    iv: Optional[float] = None


@dataclass
class OptionChainSnapshot:
    """
    Complete option chain state at a point in time.

    All per-strike data is keyed by (strike, option_type).
    Derived metrics (PCR, max_pain) are pre-computed.
    """
    timestamp: datetime
    expiry: date
    spot: float

    strikes: Dict[Tuple[int, str], StrikeData] = field(default_factory=dict)

    # Derived metrics
    pcr: float = 0.0          # Put-Call Ratio (OI-based): total_PE_OI / total_CE_OI
    pcr_volume: float = 0.0   # PCR by volume (complementary signal)
    max_pain_strike: int = 0  # Strike causing maximum loss to option buyers
    total_ce_oi: int = 0
    total_pe_oi: int = 0
    total_ce_vol: int = 0
    total_pe_vol: int = 0

    # Top OI concentrations
    top_ce_strikes: List[int] = field(default_factory=list)  # Top-3 CE OI strikes
    top_pe_strikes: List[int] = field(default_factory=list)  # Top-3 PE OI strikes

    @property
    def pcr_signal(self) -> str:
        """
        Qualitative sentiment based on PCR.
        PCR > 1.5  → strong support/bullish (heavy PE writing)
        PCR > 1.2  → mild bullish
        0.7–1.2    → neutral
        PCR < 0.7  → bearish/bearish (heavy CE writing)
        PCR < 0.5  → very bearish
        """
        if self.pcr >= 1.5:
            return "STRONG_BULLISH"
        elif self.pcr >= 1.2:
            return "MILD_BULLISH"
        elif self.pcr <= 0.5:
            return "STRONG_BEARISH"
        elif self.pcr <= 0.7:
            return "MILD_BEARISH"
        return "NEUTRAL"

    def get_strike(self, strike: int, option_type: str) -> Optional[StrikeData]:
        return self.strikes.get((strike, option_type))

    def get_oi_change(self, strike: int, option_type: str) -> int:
        """Return intraday OI change for a strike. Positive = fresh writing."""
        sd = self.strikes.get((strike, option_type))
        return sd.oi_change if sd else 0


# ─── Manager ───────────────────────────────────────────────

class OptionChainManager:
    """
    Background manager for NIFTY option chain data.

    Lifecycle:
        await mgr.start()   — begins polling loop
        mgr.latest_snapshot — read-only access (never None after first poll)
        await mgr.stop()    — cancels polling task, releases resources

    The first poll fires immediately on start() so data is available
    before the first 60-second interval elapses.
    """

    POLL_INTERVAL_NORMAL_SEC = 60
    POLL_INTERVAL_GAMMA_SEC = 30   # Tuesday after 12:00 PM — faster refresh
    MARKET_DATA_CHUNK_SIZE = 50

    GAMMA_MODE_START = dtime(12, 0)  # Tuesday expiry day tighter monitoring

    def __init__(
        self,
        connector: AngelConnector,
        instrument_manager: InstrumentManager,
        expiry: date,
        spot_getter,            # Callable[[], Optional[float]] — returns current spot
    ) -> None:
        self._connector = connector
        self._instruments = instrument_manager
        self._expiry = expiry
        self._spot_getter = spot_getter

        self._snapshot: Optional[OptionChainSnapshot] = None
        self._opening_oi: Dict[Tuple[int, str], int] = {}   # Strike OI at first poll
        self._poll_task: Optional[asyncio.Task] = None
        self._running = False

        # Track if today is expiry day for gamma mode
        self._is_expiry_today = expiry == date.today()

    async def start(self) -> None:
        """Start the polling loop. Returns immediately after scheduling."""
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(
            f"OptionChainManager started: expiry={self._expiry}, "
            f"gamma_mode={'yes' if self._is_expiry_today else 'no'}"
        )

    async def stop(self) -> None:
        """Cancel polling loop gracefully."""
        self._running = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        logger.info("OptionChainManager stopped")

    @property
    def latest_snapshot(self) -> Optional[OptionChainSnapshot]:
        """Current option chain snapshot. None until first poll completes."""
        return self._snapshot

    @property
    def pcr(self) -> float:
        """Current PCR. 0.0 if no snapshot yet."""
        return self._snapshot.pcr if self._snapshot else 0.0

    @property
    def max_pain(self) -> int:
        """Current max pain strike. 0 if no snapshot yet."""
        return self._snapshot.max_pain_strike if self._snapshot else 0

    def get_oi_change(self, strike: int, option_type: str) -> int:
        """Intraday OI change for a strike. Positive = writing activity."""
        if not self._snapshot:
            return 0
        return self._snapshot.get_oi_change(strike, option_type)

    # ─── Internal ──────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Main polling loop. Fires immediately, then on schedule."""
        # First poll: immediate
        await self._do_poll()

        while self._running:
            interval = self._get_poll_interval()
            await asyncio.sleep(interval)
            if not self._running:
                break
            await self._do_poll()

    def _get_poll_interval(self) -> int:
        """Return poll interval in seconds (shorter on expiry day afternoon)."""
        if self._is_expiry_today:
            now = datetime.now().time()
            if now >= self.GAMMA_MODE_START:
                return self.POLL_INTERVAL_GAMMA_SEC
        return self.POLL_INTERVAL_NORMAL_SEC

    async def _do_poll(self) -> None:
        """Fetch option chain from broker and update snapshot."""
        try:
            spot = self._spot_getter() or 0.0
            chain_data = await self._fetch_chain_market_data()
            if not chain_data:
                logger.warning("Empty option chain response")
                return

            snapshot = self._build_snapshot(chain_data, spot)
            self._snapshot = snapshot

            logger.debug(
                f"Option chain updated: PCR={snapshot.pcr:.2f} "
                f"({snapshot.pcr_signal}), "
                f"MaxPain={snapshot.max_pain_strike}, "
                f"CE_OI={snapshot.total_ce_oi:,}, PE_OI={snapshot.total_pe_oi:,}"
            )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Option chain poll failed: {e}", exc_info=True)

    async def _fetch_chain_market_data(self) -> List[dict]:
        """
        Fetch NIFTY option-chain-like data through SmartAPI getMarketData.

        smartapi-python 1.4.x does not expose a getOptionChain method. The
        supported route is getMarketData("FULL", {"NFO": tokens}), so we use
        the instrument master to build the expiry's token list and aggregate
        the broker's `fetched` records.
        """
        instruments = self._instruments.get_all_nifty_options_for_expiry(self._expiry)
        tokens = [info.token for info in instruments]
        if not tokens:
            logger.warning(f"No NIFTY option tokens available for expiry={self._expiry}")
            return []

        records: List[dict] = []
        for i in range(0, len(tokens), self.MARKET_DATA_CHUNK_SIZE):
            chunk = tokens[i:i + self.MARKET_DATA_CHUNK_SIZE]
            data = await self._connector.get_market_data("FULL", {"NFO": chunk})
            fetched = data.get("fetched", []) if isinstance(data, dict) else []
            records.extend(fetched)

            unfetched = data.get("unfetched", []) if isinstance(data, dict) else []
            if unfetched:
                logger.debug(
                    f"SmartAPI market data skipped {len(unfetched)} option tokens "
                    f"for expiry={self._expiry}"
                )

        return records

    def _build_snapshot(self, chain_data, spot: float) -> OptionChainSnapshot:
        """
        Parse broker option chain response into an OptionChainSnapshot.

        Supports both Angel option-chain style records with:
            strikePrice, expiryDate, optionType (CE/PE), openInterest,
            changeinOpenInterest, totalTradingVolume, lastPrice,
            bidPrice, askPrice, impliedVolatility
        and SmartAPI getMarketData("FULL") records with:
            symbolToken, tradingSymbol, ltp, tradeVolume, opnInterest, depth.
        """
        strikes: Dict[Tuple[int, str], StrikeData] = {}
        total_ce_oi = 0
        total_pe_oi = 0
        total_ce_vol = 0
        total_pe_vol = 0

        records = []
        # Handle both list and dict ({"data": [...]} wrapper)
        if isinstance(chain_data, dict):
            records = chain_data.get("data", [])
        elif isinstance(chain_data, list):
            records = chain_data

        for rec in records:
            try:
                token = str(rec.get("symbolToken") or rec.get("symboltoken") or rec.get("token") or "")
                info = self._instruments.get_instrument(token) if token else None

                strike = int(float(rec.get("strikePrice", 0) or 0))
                if strike <= 0 and info:
                    strike = info.strike

                opt_type = str(rec.get("optionType", "")).strip().upper()
                if opt_type not in ("CE", "PE") and info:
                    opt_type = info.option_type

                if opt_type not in ("CE", "PE") or strike <= 0:
                    continue

                oi = int(rec.get("openInterest", rec.get("opnInterest", 0)) or 0)
                volume = int(rec.get("totalTradingVolume", rec.get("tradeVolume", 0)) or 0)
                ltp = float(rec.get("lastPrice", rec.get("ltp", 0)) or 0)
                bid = float(rec.get("bidPrice", 0) or 0)
                ask = float(rec.get("askPrice", 0) or 0)

                depth = rec.get("depth") or {}
                if (bid <= 0 or ask <= 0) and isinstance(depth, dict):
                    buy_depth = depth.get("buy") or []
                    sell_depth = depth.get("sell") or []
                    if bid <= 0 and buy_depth:
                        bid = float(buy_depth[0].get("price", 0) or 0)
                    if ask <= 0 and sell_depth:
                        ask = float(sell_depth[0].get("price", 0) or 0)

                iv_raw = rec.get("impliedVolatility", rec.get("iv", None))
                iv = float(iv_raw) / 100 if iv_raw else None  # broker sends percentage

                # Look up token from instrument manager
                token = token or self._instruments.get_option_token(
                    "NIFTY", strike, opt_type, self._expiry
                ) or ""

                # Intraday OI change: lock opening OI on first poll
                key = (strike, opt_type)
                if key not in self._opening_oi:
                    self._opening_oi[key] = oi
                oi_change = oi - self._opening_oi[key]

                sd = StrikeData(
                    strike=strike,
                    option_type=opt_type,
                    token=token,
                    ltp=ltp,
                    bid=bid,
                    ask=ask,
                    oi=oi,
                    oi_change=oi_change,
                    volume=volume,
                    iv=iv,
                )
                strikes[key] = sd

                if opt_type == "CE":
                    total_ce_oi += oi
                    total_ce_vol += volume
                else:
                    total_pe_oi += oi
                    total_pe_vol += volume

            except Exception as e:
                logger.debug(f"Skipped chain record: {e}")

        # PCR calculations
        pcr = (total_pe_oi / total_ce_oi) if total_ce_oi > 0 else 0.0
        pcr_volume = (total_pe_vol / total_ce_vol) if total_ce_vol > 0 else 0.0

        # Max pain: strike where option buyers lose the most
        max_pain_strike = self._compute_max_pain(strikes)

        # Top OI concentration (top-3 strikes by OI per side)
        ce_by_oi = sorted(
            [sd for sd in strikes.values() if sd.option_type == "CE"],
            key=lambda x: x.oi,
            reverse=True,
        )
        pe_by_oi = sorted(
            [sd for sd in strikes.values() if sd.option_type == "PE"],
            key=lambda x: x.oi,
            reverse=True,
        )

        return OptionChainSnapshot(
            timestamp=datetime.now(),
            expiry=self._expiry,
            spot=spot,
            strikes=strikes,
            pcr=round(pcr, 4),
            pcr_volume=round(pcr_volume, 4),
            max_pain_strike=max_pain_strike,
            total_ce_oi=total_ce_oi,
            total_pe_oi=total_pe_oi,
            total_ce_vol=total_ce_vol,
            total_pe_vol=total_pe_vol,
            top_ce_strikes=[sd.strike for sd in ce_by_oi[:3]],
            top_pe_strikes=[sd.strike for sd in pe_by_oi[:3]],
        )

    @staticmethod
    def _compute_max_pain(strikes: Dict[Tuple[int, str], StrikeData]) -> int:
        """
        Max pain: the strike price at which option buyers (combined CE + PE)
        would suffer the maximum total loss at expiry.

        For each candidate expiry price (= each available strike), compute:
          total_loss_CE = sum(max(0, expiry - strike) * CE_OI for all CE strikes)
          total_loss_PE = sum(max(0, strike - expiry) * PE_OI for all PE strikes)
          total_pain_at_expiry = total_loss_CE + total_loss_PE

        The max pain strike is where total_pain_at_expiry is maximum.
        """
        # Collect unique strikes
        all_strikes = sorted(set(k[0] for k in strikes.keys()))
        if not all_strikes:
            return 0

        max_pain_val = -1
        max_pain_strike = all_strikes[0]

        for candidate in all_strikes:
            total_pain = 0.0

            for (strike, opt_type), sd in strikes.items():
                if opt_type == "CE":
                    # CE buyer profits if expiry > strike
                    total_pain += max(0, candidate - strike) * sd.oi
                else:
                    # PE buyer profits if expiry < strike
                    total_pain += max(0, strike - candidate) * sd.oi

            if total_pain > max_pain_val:
                max_pain_val = total_pain
                max_pain_strike = candidate

        return max_pain_strike

    def reset_for_new_expiry(self, expiry: date) -> None:
        """Switch to a new expiry (e.g. after Tuesday close). Resets all state."""
        self._expiry = expiry
        self._snapshot = None
        self._opening_oi.clear()
        self._is_expiry_today = expiry == date.today()
        logger.info(f"OptionChainManager reset for new expiry: {expiry}")
