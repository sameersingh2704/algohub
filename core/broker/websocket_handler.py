"""
Angel One SmartAPI WebSocket Handler
=======================================
Manages the live data WebSocket connection with:
- Auto-reconnect with exponential backoff
- Heartbeat monitoring
- Stale data detection
- Subscription management
- Tick callback dispatch

The WebSocket provides real-time LTP, bid/ask, and OI data.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional, Set
from dataclasses import dataclass, field

from SmartApi.smartWebSocketV2 import SmartWebSocketV2

from core.auth.angel_auth import AngelAuthManager

logger = logging.getLogger(__name__)

# ─── Exchange type constants (Angel One SmartAPI) ──────────
# These map to the exchangeType field in WebSocket subscription requests.
EXCHANGE_TYPE_NSE_CM  = 1   # NSE Cash Market (equities, indices like NIFTY spot, VIX)
EXCHANGE_TYPE_NSE_FO  = 2   # NSE Futures & Options (NIFTY/BANKNIFTY options & futures)
EXCHANGE_TYPE_BSE_CM  = 3   # BSE Cash Market
EXCHANGE_TYPE_BSE_FO  = 4   # BSE Futures & Options
EXCHANGE_TYPE_MCX     = 5   # MCX Commodities


@dataclass
class TickData:
    """Normalized tick from WebSocket feed."""
    token: str
    symbol: str
    timestamp: datetime
    ltp: float
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    # IMPORTANT: volume here is the raw cumulative day volume from the exchange.
    # Per-bar volume delta is computed in BarBuilder, NOT here.
    volume_cumulative: int = 0
    oi: int = 0
    bid: float = 0.0
    ask: float = 0.0
    bid_qty: int = 0       # Quantity available at best bid
    ask_qty: int = 0       # Quantity available at best ask
    exchange: str = "NSE"
    # Full depth (up to 5 levels each side)
    depth_buy: List[Dict] = field(default_factory=list)   # [{"price": x, "qty": y}, ...]
    depth_sell: List[Dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Populate L1 bid/ask from depth when callers provide depth directly."""
        if self.bid <= 0 and self.depth_buy:
            self.bid = float(self.depth_buy[0].get("price", 0) or 0)
        if self.ask <= 0 and self.depth_sell:
            self.ask = float(self.depth_sell[0].get("price", 0) or 0)
        if self.bid_qty <= 0 and self.depth_buy:
            self.bid_qty = int(self.depth_buy[0].get("qty", self.depth_buy[0].get("quantity", 0)) or 0)
        if self.ask_qty <= 0 and self.depth_sell:
            self.ask_qty = int(self.depth_sell[0].get("qty", self.depth_sell[0].get("quantity", 0)) or 0)

    @property
    def spread(self) -> float:
        return self.ask - self.bid if self.ask > 0 and self.bid > 0 else 0.0

    @property
    def mid_price(self) -> float:
        return (self.bid + self.ask) / 2 if self.bid > 0 and self.ask > 0 else self.ltp

    @property
    def spread_pct(self) -> float:
        if self.mid_price > 0:
            return self.spread / self.mid_price
        return 0.0

    @property
    def has_valid_depth(self) -> bool:
        return bool(self.bid > 0 and self.ask > 0)


# Type alias for tick callback
TickCallback = Callable[[TickData], None]


class WebSocketHandler:
    """
    Angel One SmartAPI WebSocket connection manager.

    Provides:
    - subscribe(tokens): Subscribe to live feed for tokens
    - unsubscribe(tokens): Unsubscribe from tokens
    - on_tick(callback): Register callback for tick events
    - is_alive(): Returns True if connection is healthy
    - last_tick_age(): Seconds since last tick received
    """

    MAX_RECONNECT_ATTEMPTS = 10      # attempts in first pass before long-retry mode
    RECONNECT_BASE_DELAY = 30        # first retry after 30s (was 5 — too aggressive)
    RECONNECT_MAX_DELAY = 300        # cap at 5 min per attempt (was 120)
    RECONNECT_LONG_RETRY_DELAY = 600 # after exhausting fast retries, retry every 10 min
    HEARTBEAT_INTERVAL = 30
    STALE_THRESHOLD_SECONDS = 30
    MODE_FULL = 3  # Full market data mode (LTP + OHLC + depth + OI)

    def __init__(
        self,
        auth_manager: AngelAuthManager,
        stale_threshold_sec: int = 30,
    ) -> None:
        self._auth = auth_manager
        self._stale_threshold = stale_threshold_sec
        self._ws: Optional[SmartWebSocketV2] = None

        # Separate subscription registries by exchange type
        # Key: exchangeType (int), Value: set of token strings
        self._subscriptions_by_exchange: Dict[int, Set[str]] = {
            EXCHANGE_TYPE_NSE_CM: set(),
            EXCHANGE_TYPE_NSE_FO: set(),
        }
        self._token_symbol_map: Dict[str, str] = {}
        self._token_exchange_map: Dict[str, int] = {}  # token → exchangeType

        self._tick_callbacks: List[TickCallback] = []
        self._disconnect_callbacks: List[Callable] = []
        self._reconnect_callbacks: List[Callable] = []
        self._last_tick_time: float = 0.0
        self._connected: bool = False
        self._closing: bool = False
        self._reconnect_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._reconnect_count: int = 0
        # Store the asyncio event loop so sync callbacks can schedule coroutines safely
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def on_tick(self, callback: TickCallback) -> None:
        """Register a callback to receive TickData objects."""
        self._tick_callbacks.append(callback)

    def on_disconnect(self, callback: Callable) -> None:
        """Register callback for disconnect events."""
        self._disconnect_callbacks.append(callback)

    def on_reconnect(self, callback: Callable) -> None:
        """Register callback for reconnect events."""
        self._reconnect_callbacks.append(callback)

    async def connect(self) -> None:
        """Establish WebSocket connection."""
        logger.info("Connecting WebSocket...")
        self._closing = False
        # Capture the running event loop NOW, while we are in async context.
        # The _on_close/_on_open callbacks fire from a non-async thread,
        # so they cannot call asyncio.create_task() directly — they must use
        # loop.call_soon_threadsafe() with this stored reference.
        self._loop = asyncio.get_event_loop()
        await self._auth.ensure_authenticated()
        self._create_ws_client()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info("WebSocket connection initiated")

    def _create_ws_client(self) -> None:
        """Create and configure SmartWebSocketV2 client.

        Always closes the previous connection before creating a new one so
        Angel One's server-side connection count doesn't accumulate across
        restarts — which triggers their 429 'Connection Limit Exceeded' error.
        """
        # Close previous connection so Angel One drops the server-side socket
        # before we open a new one. Without this, every reconnect burns one of
        # Angel One's per-key connection slots and quickly triggers 429.
        if self._ws is not None:
            try:
                self._ws.close_connection()
            except Exception:
                pass
            self._ws = None
            time.sleep(1)   # brief pause so the close reaches Angel One's server

        session = self._auth.current_session

        self._ws = SmartWebSocketV2(
            auth_token=session.jwt_token,
            api_key=self._auth._api_key,
            client_code=session.client_id,
            feed_token=session.feed_token,
        )

        self._ws.on_open = self._on_open
        self._ws.on_data = self._on_data
        self._ws.on_error = self._on_error
        self._ws.on_close = self._on_close

        asyncio.get_event_loop().run_in_executor(None, self._ws.connect)

    def _on_open(self, ws) -> None:
        """Called when WebSocket connection is established."""
        logger.info("WebSocket connected")
        self._connected = True
        self._reconnect_count = 0
        self._last_tick_time = time.time()

        # Re-subscribe to all tokens after reconnect
        if any(self._subscriptions_by_exchange.values()):
            self._resubscribe_all()

        for cb in self._reconnect_callbacks:
            try:
                cb()
            except Exception as e:
                logger.error(f"Reconnect callback error: {e}")

    def _on_data(self, ws, message: dict) -> None:
        """Called on every tick from the feed."""
        try:
            self._last_tick_time = time.time()
            tick = self._parse_tick(message)
            if tick:
                for cb in self._tick_callbacks:
                    try:
                        cb(tick)
                    except Exception as e:
                        logger.error(f"Tick callback error: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Tick processing error: {e}", exc_info=True)

    def _on_error(self, ws, error) -> None:
        """Called on WebSocket error."""
        err_str = str(error).lower()
        if "429" in err_str or "too many requests" in err_str or "connection limit" in err_str:
            logger.warning(
                f"WebSocket rate-limited by Angel One (429). "
                f"Will retry every {self.RECONNECT_LONG_RETRY_DELAY}s. "
                f"No action needed — app will auto-recover when limit resets."
            )
        else:
            logger.error(f"WebSocket error: {error}")

    def _on_close(self, ws) -> None:
        """
        Called when WebSocket connection closes.

        IMPORTANT: This is invoked from the WebSocket library's own thread,
        NOT from the asyncio event loop thread. Never call asyncio.create_task()
        here directly — it will raise RuntimeError: "no running event loop".
        Use loop.call_soon_threadsafe() to schedule work on the correct loop.
        """
        if not self._closing:
            logger.warning("WebSocket disconnected")
        self._connected = False

        for cb in self._disconnect_callbacks:
            try:
                cb()
            except Exception as e:
                logger.error(f"Disconnect callback error: {e}")

        if self._closing:
            return

        # Schedule reconnect coroutine onto the asyncio loop from this sync thread
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(
                self._loop.create_task,
                self._reconnect_loop(),
            )
        else:
            logger.error("Cannot schedule reconnect — event loop not available")

    def _parse_tick(self, data: dict) -> Optional[TickData]:
        """
        Parse raw Angel One WebSocket message into TickData.

        Key notes:
        - All prices arrive in paise (integer), divide by 100 for rupees.
        - volume_trade_for_the_day is CUMULATIVE for the session, not per-tick.
          BarBuilder is responsible for computing per-bar volume delta.
        - best_5_buy_data / best_5_sell_data have up to 5 depth levels.
          We extract all levels for depth tracking, and L1 for bid/ask.
        """
        try:
            token = str(data.get("token", ""))
            if not token:
                return None

            symbol = self._token_symbol_map.get(token, token)
            ltp = float(data.get("last_traded_price", 0)) / 100
            if ltp <= 0:
                return None  # Skip zero-price ticks

            timestamp_raw = data.get("exchange_timestamp", time.time() * 1000)
            timestamp = datetime.fromtimestamp(timestamp_raw / 1000)

            # ── Depth parsing ──────────────────────────────────
            raw_buy = data.get("best_5_buy_data") or []
            raw_sell = data.get("best_5_sell_data") or []

            depth_buy = []
            for level in raw_buy:
                price = float(level.get("price", 0)) / 100
                qty = int(level.get("quantity", 0))
                if price > 0:
                    depth_buy.append({"price": price, "qty": qty})

            depth_sell = []
            for level in raw_sell:
                price = float(level.get("price", 0)) / 100
                qty = int(level.get("quantity", 0))
                if price > 0:
                    depth_sell.append({"price": price, "qty": qty})

            # Best bid = highest buy price, best ask = lowest sell price
            bid = depth_buy[0]["price"] if depth_buy else 0.0
            ask = depth_sell[0]["price"] if depth_sell else 0.0
            bid_qty = depth_buy[0]["qty"] if depth_buy else 0
            ask_qty = depth_sell[0]["qty"] if depth_sell else 0

            return TickData(
                token=token,
                symbol=symbol,
                timestamp=timestamp,
                ltp=ltp,
                open=float(data.get("open_price_of_the_day", 0)) / 100,
                high=float(data.get("high_price_of_the_day", 0)) / 100,
                low=float(data.get("low_price_of_the_day", 0)) / 100,
                close=float(data.get("closed_price", 0)) / 100,
                volume_cumulative=int(data.get("volume_trade_for_the_day", 0)),
                oi=int(data.get("open_interest", 0)),
                bid=bid,
                ask=ask,
                bid_qty=bid_qty,
                ask_qty=ask_qty,
                depth_buy=depth_buy,
                depth_sell=depth_sell,
            )
        except Exception as e:
            logger.debug(f"Tick parse error: {e}", exc_info=True)
            return None

    def subscribe(
        self,
        tokens: List[str],
        symbols: List[str],
        exchange_type: int = EXCHANGE_TYPE_NSE_CM,
    ) -> None:
        """
        Subscribe to live feed for given tokens.

        exchange_type MUST match the instrument's exchange:
          EXCHANGE_TYPE_NSE_CM (1)  → NIFTY spot, VIX, equities
          EXCHANGE_TYPE_NSE_FO (2)  → NIFTY/BANKNIFTY options & futures

        Mixing exchange types in a single call is allowed — this method
        will send separate subscription requests per exchange type.
        """
        if not tokens:
            return

        for token, symbol in zip(tokens, symbols):
            self._subscriptions_by_exchange.setdefault(exchange_type, set()).add(token)
            self._token_symbol_map[token] = symbol
            self._token_exchange_map[token] = exchange_type

        token_list = [{"exchangeType": exchange_type, "tokens": tokens}]

        if self._ws and self._connected:
            try:
                self._ws.subscribe(
                    correlation_id="primary",
                    mode=self.MODE_FULL,
                    token_list=token_list,
                )
                logger.info(
                    f"Subscribed {len(tokens)} tokens on exchangeType={exchange_type}"
                )
            except Exception as e:
                logger.error(f"Subscription error: {e}")
        else:
            logger.warning(
                f"WebSocket not connected — {len(tokens)} tokens queued "
                f"(exchangeType={exchange_type})"
            )

    def unsubscribe(self, tokens: List[str]) -> None:
        """Unsubscribe from tokens, using correct exchange type per token."""
        if not self._ws or not self._connected:
            return

        # Group by exchange type for correct unsubscribe packets
        groups: Dict[int, List[str]] = {}
        for token in tokens:
            ex_type = self._token_exchange_map.get(token, EXCHANGE_TYPE_NSE_CM)
            groups.setdefault(ex_type, []).append(token)
            self._subscriptions_by_exchange.get(ex_type, set()).discard(token)
            self._token_exchange_map.pop(token, None)
            self._token_symbol_map.pop(token, None)

        for ex_type, toks in groups.items():
            try:
                self._ws.unsubscribe(
                    correlation_id="primary",
                    mode=self.MODE_FULL,
                    token_list=[{"exchangeType": ex_type, "tokens": toks}],
                )
                logger.info(f"Unsubscribed {len(toks)} tokens (exchangeType={ex_type})")
            except Exception as e:
                logger.error(f"Unsubscribe error: {e}")

    def _resubscribe_all(self) -> None:
        """
        Re-subscribe to all tokens after reconnect.
        Groups tokens by their correct exchange type.
        """
        total = sum(len(t) for t in self._subscriptions_by_exchange.values())
        if total == 0:
            return

        for ex_type, tokens in self._subscriptions_by_exchange.items():
            if not tokens:
                continue
            try:
                self._ws.subscribe(
                    correlation_id="primary",
                    mode=self.MODE_FULL,
                    token_list=[{"exchangeType": ex_type, "tokens": list(tokens)}],
                )
                logger.info(
                    f"Re-subscribed {len(tokens)} tokens "
                    f"(exchangeType={ex_type}) after reconnect"
                )
            except Exception as e:
                logger.error(f"Re-subscription error for exchangeType={ex_type}: {e}")

    async def _reconnect_loop(self) -> None:
        """
        Reconnect loop that never permanently gives up.

        Phase 1 — exponential backoff (30s → 300s) for up to MAX_RECONNECT_ATTEMPTS.
        Phase 2 — if all Phase-1 attempts fail (e.g. 429 rate-limit from Angel One),
                   keep retrying every RECONNECT_LONG_RETRY_DELAY seconds until the
                   session ends. This prevents the app from dying on a temporary
                   Angel One connection-limit error.
        """
        if self._reconnect_task and not self._reconnect_task.done():
            return  # already reconnecting

        self._reconnect_task = asyncio.current_task()
        try:
            # ── Phase 1: exponential backoff ─────────────────────────────
            for attempt in range(1, self.MAX_RECONNECT_ATTEMPTS + 1):
                delay = min(
                    self.RECONNECT_BASE_DELAY * (2 ** (attempt - 1)),
                    self.RECONNECT_MAX_DELAY,
                )
                logger.info(
                    f"WebSocket reconnect attempt {attempt}/{self.MAX_RECONNECT_ATTEMPTS} "
                    f"in {delay}s"
                )
                await asyncio.sleep(delay)
                if self._closing:
                    return

                try:
                    await self._auth.ensure_authenticated()
                    self._create_ws_client()
                    self._reconnect_count += 1
                    logger.info(f"WebSocket reconnect initiated (attempt {attempt})")
                    return
                except Exception as e:
                    logger.error(f"Reconnect attempt {attempt} failed: {e}")

            # ── Phase 2: long-retry mode (never give up) ─────────────────
            # Reaches here when Angel One is rate-limiting (HTTP 429 / connection
            # limit exceeded). Keep trying every 10 minutes until the session ends.
            logger.warning(
                f"Phase-1 reconnects exhausted — switching to long-retry mode "
                f"(every {self.RECONNECT_LONG_RETRY_DELAY}s). "
                f"Possible Angel One connection-limit (429). App will auto-recover."
            )
            long_attempt = 0
            while not self._closing:
                long_attempt += 1
                logger.info(
                    f"WebSocket long-retry attempt {long_attempt} "
                    f"in {self.RECONNECT_LONG_RETRY_DELAY}s"
                )
                await asyncio.sleep(self.RECONNECT_LONG_RETRY_DELAY)
                if self._closing:
                    return

                try:
                    await self._auth.ensure_authenticated()
                    self._create_ws_client()
                    self._reconnect_count += 1
                    logger.info(f"WebSocket long-retry succeeded (attempt {long_attempt})")
                    return
                except Exception as e:
                    logger.error(f"Long-retry attempt {long_attempt} failed: {e}")

        finally:
            self._reconnect_task = None

    async def _heartbeat_loop(self) -> None:
        """Monitor data freshness. Alert on stale data."""
        while True:
            await asyncio.sleep(self.HEARTBEAT_INTERVAL)
            age = self.last_tick_age()
            if age > self._stale_threshold and self._connected:
                logger.warning(f"Stale data detected: last tick {age:.0f}s ago")

    def last_tick_age(self) -> float:
        """Return seconds since last tick received."""
        if self._last_tick_time == 0:
            return float("inf")
        return time.time() - self._last_tick_time

    def is_alive(self) -> bool:
        """Return True if connected and data is fresh."""
        return self._connected and self.last_tick_age() < self._stale_threshold

    def is_data_stale(self) -> bool:
        """Return True if no tick received within stale threshold."""
        return self.last_tick_age() > self._stale_threshold

    async def close(self) -> None:
        """Gracefully close WebSocket connection."""
        self._closing = True
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()

        if self._ws:
            try:
                self._ws.close_connection()
            except Exception as e:
                logger.warning(f"WebSocket close error: {e}")

        logger.info("WebSocket handler closed")
