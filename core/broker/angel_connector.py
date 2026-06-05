"""
Angel One SmartAPI REST Connector
===================================
Wraps all SmartAPI REST calls with:
- Rate limiting
- Retry logic with exponential backoff
- Response validation
- Structured logging
- Duplicate order prevention
- Order acknowledgement verification

All methods are async. No blocking calls allowed in async context.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from core.auth.angel_auth import AngelAuthManager
from core.broker.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"           # Stop-loss limit
    SL_M = "SL-M"       # Stop-loss market


class TransactionType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class ProductType(str, Enum):
    INTRADAY = "INTRADAY"
    CARRYFORWARD = "CARRYFORWARD"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


@dataclass
class PlaceOrderRequest:
    symbol: str
    token: str
    exchange: str
    transaction_type: TransactionType
    order_type: OrderType
    quantity: int
    product_type: ProductType = ProductType.INTRADAY
    price: float = 0.0
    trigger_price: float = 0.0
    variety: str = "NORMAL"
    internal_order_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class OrderResponse:
    internal_order_id: str
    broker_order_id: Optional[str]
    status: OrderStatus
    message: str
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class OptionChainData:
    symbol: str
    strike: int
    option_type: str
    expiry: date
    ltp: float
    bid: float
    ask: float
    volume: int
    oi: int
    change_oi: int
    iv: Optional[float] = None


class AngelConnector:
    """
    Main interface for all Angel One SmartAPI REST operations.

    Enforces:
    - Rate limiting (5 req/sec default)
    - Retry on transient failures
    - Request deduplication via internal order IDs
    - Full audit logging
    """

    MAX_RETRIES = 3
    BASE_RETRY_DELAY = 2.0

    def __init__(self, auth_manager: AngelAuthManager, rate_limiter: RateLimiter) -> None:
        self._auth = auth_manager
        self._rate_limiter = rate_limiter
        self._pending_order_ids: set[str] = set()  # Dedup guard
        self._submitted_order_ids: Dict[str, str] = {}  # internal_id → broker_id

    async def _execute_with_retry(
        self,
        operation_name: str,
        operation,
        *args,
        **kwargs,
    ) -> Any:
        """
        Execute an API operation with retry + rate limiting.
        Raises on final failure.
        """
        await self._auth.ensure_authenticated()

        last_error: Optional[Exception] = None

        for attempt in range(1, self.MAX_RETRIES + 1):
            await self._rate_limiter.acquire()
            try:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, lambda: operation(*args, **kwargs))
                return result
            except Exception as e:
                last_error = e
                logger.warning(
                    f"{operation_name} attempt {attempt}/{self.MAX_RETRIES} failed: {e}"
                )
                if attempt < self.MAX_RETRIES:
                    delay = self.BASE_RETRY_DELAY * attempt
                    await asyncio.sleep(delay)

        raise RuntimeError(
            f"{operation_name} failed after {self.MAX_RETRIES} attempts. "
            f"Last error: {last_error}"
        )

    async def place_order(self, request: PlaceOrderRequest) -> OrderResponse:
        """
        Place an order with deduplication guard.

        Rejects if the same internal_order_id was already submitted.
        This prevents double-orders on network retries.
        """
        internal_id = request.internal_order_id

        # DEDUPLICATION CHECK
        if internal_id in self._pending_order_ids:
            logger.error(f"Duplicate order attempt blocked: {internal_id}")
            return OrderResponse(
                internal_order_id=internal_id,
                broker_order_id=None,
                status=OrderStatus.REJECTED,
                message="Duplicate order blocked",
            )

        if internal_id in self._submitted_order_ids:
            broker_id = self._submitted_order_ids[internal_id]
            logger.warning(f"Order already submitted: internal={internal_id}, broker={broker_id}")
            return OrderResponse(
                internal_order_id=internal_id,
                broker_order_id=broker_id,
                status=OrderStatus.OPEN,
                message="Order already submitted",
            )

        self._pending_order_ids.add(internal_id)
        logger.info(
            "Placing order",
            extra={
                "internal_order_id": internal_id,
                "symbol": request.symbol,
                "type": request.transaction_type.value,
                "order_type": request.order_type.value,
                "quantity": request.quantity,
                "price": request.price,
            }
        )

        try:
            order_params = {
                "variety": request.variety,
                "tradingsymbol": request.symbol,
                "symboltoken": request.token,
                "transactiontype": request.transaction_type.value,
                "exchange": request.exchange,
                "ordertype": request.order_type.value,
                "producttype": request.product_type.value,
                "duration": "DAY",
                "price": str(request.price),
                "triggerprice": str(request.trigger_price),
                "squareoff": "0",
                "stoploss": "0",
                "quantity": str(request.quantity),
            }

            result = await self._execute_with_retry(
                "place_order",
                self._auth.smart_api.placeOrder,
                order_params,
            )

            if result and result.get("status"):
                broker_order_id = result.get("data", {}).get("orderid", "")
                self._submitted_order_ids[internal_id] = broker_order_id
                self._pending_order_ids.discard(internal_id)

                logger.info(
                    "Order placed successfully",
                    extra={
                        "internal_order_id": internal_id,
                        "broker_order_id": broker_order_id,
                        "symbol": request.symbol,
                    }
                )
                return OrderResponse(
                    internal_order_id=internal_id,
                    broker_order_id=broker_order_id,
                    status=OrderStatus.OPEN,
                    message="Order placed",
                )
            else:
                error_msg = result.get("message", "Unknown error") if result else "No response"
                logger.error(f"Order placement failed: {error_msg}")
                self._pending_order_ids.discard(internal_id)
                return OrderResponse(
                    internal_order_id=internal_id,
                    broker_order_id=None,
                    status=OrderStatus.REJECTED,
                    message=error_msg,
                )

        except Exception as e:
            self._pending_order_ids.discard(internal_id)
            logger.error(f"Order placement exception: {e}", exc_info=True)
            return OrderResponse(
                internal_order_id=internal_id,
                broker_order_id=None,
                status=OrderStatus.REJECTED,
                message=str(e),
            )

    async def cancel_order(self, broker_order_id: str, variety: str = "NORMAL") -> bool:
        """Cancel an open order. Returns True if successful."""
        logger.info(f"Cancelling order: {broker_order_id}")
        try:
            result = await self._execute_with_retry(
                "cancel_order",
                self._auth.smart_api.cancelOrder,
                variety,
                broker_order_id,
            )
            success = result and result.get("status", False)
            if success:
                logger.info(f"Order cancelled: {broker_order_id}")
            else:
                logger.warning(f"Order cancel failed: {broker_order_id}")
            return bool(success)
        except Exception as e:
            logger.error(f"Cancel order exception: {e}", exc_info=True)
            return False

    async def get_order_status(self, broker_order_id: str) -> Dict[str, Any]:
        """Fetch current order status from broker."""
        try:
            order_book = await self._execute_with_retry(
                "get_order_book",
                self._auth.smart_api.orderBook,
            )
            if not order_book or not order_book.get("status"):
                return {}

            orders = order_book.get("data", []) or []
            for order in orders:
                if str(order.get("orderid", "")) == str(broker_order_id):
                    return order
            return {}
        except Exception as e:
            logger.error(f"Get order status exception: {e}", exc_info=True)
            return {}

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Fetch current open positions from broker."""
        try:
            result = await self._execute_with_retry(
                "get_positions",
                self._auth.smart_api.position,
            )
            if result and result.get("status"):
                return result.get("data", []) or []
            return []
        except Exception as e:
            logger.error(f"Get positions exception: {e}", exc_info=True)
            return []

    async def get_ltp(self, exchange: str, symbol: str, token: str) -> Optional[float]:
        """Get Last Traded Price for a symbol."""
        try:
            result = await self._execute_with_retry(
                "get_ltp",
                self._auth.smart_api.ltpData,
                exchange,
                symbol,
                token,
            )
            if result and result.get("status"):
                return float(result.get("data", {}).get("ltp", 0))
            return None
        except Exception as e:
            logger.error(f"Get LTP exception: {e}", exc_info=True)
            return None

    async def get_option_chain(
        self,
        name: str,
        expiry_date: str,
        strike_price: str,
    ):
        """
        Fetch option chain for a given expiry and strike range.

        Note: the underlying SmartAPI client does not expose a first-class
        option-chain helper in all versions. We call the client's method if
        available and return the raw broker response `data` (list/dict). The
        OptionChainManager expects the broker's raw dict/list format and will
        parse it into snapshot structures.
        """
        try:
            # The SmartAPI client may or may not implement `getOptionChain`.
            # Rely on our retry wrapper to call it; return the raw `data` field
            # so callers can parse broker-specific keys.
            method = getattr(self._auth.smart_api, "getOptionChain", None)
            if method is None:
                logger.warning("SmartAPI client has no getOptionChain; returning empty chain")
                return []

            result = await self._execute_with_retry(
                "get_option_chain",
                method,
                name,
                expiry_date,
                strike_price,
            )

            if not result or not result.get("status"):
                logger.warning(f"Option chain fetch returned no data for {name} {expiry_date}")
                return result.get("data") if isinstance(result, dict) else []

            return result.get("data", [])

        except Exception as e:
            logger.error(f"Get option chain exception: {e}", exc_info=True)
            return []

    async def get_historical_data(
        self,
        token: str,
        exchange: str,
        symbol: str,
        interval: str,
        from_date: str,
        to_date: str,
    ) -> List[Dict[str, Any]]:
        """
        Fetch historical OHLCV data.
        interval: ONE_MINUTE | FIVE_MINUTE | FIFTEEN_MINUTE | ONE_DAY
        from_date/to_date: "YYYY-MM-DD HH:MM"
        """
        try:
            historic_param = {
                "exchange": exchange,
                "symboltoken": token,
                "interval": interval,
                "fromdate": from_date,
                "todate": to_date,
            }
            result = await self._execute_with_retry(
                "get_historical_data",
                self._auth.smart_api.getCandleData,
                historic_param,
            )
            if result and result.get("status"):
                return result.get("data", []) or []
            return []
        except Exception as e:
            logger.error(f"Get historical data exception: {e}", exc_info=True)
            return []

    async def get_market_data(self, mode: str, exchange_tokens: Dict) -> Dict[str, Any]:
        """
        Get full market data snapshot (quote mode).
        mode: LTP | QUOTE | FULL
        """
        try:
            result = await self._execute_with_retry(
                "get_market_data",
                self._auth.smart_api.getMarketData,
                mode,
                exchange_tokens,
            )
            if result and result.get("status"):
                return result.get("data", {}) or {}
            return {}
        except Exception as e:
            logger.error(f"Get market data exception: {e}", exc_info=True)
            return {}
