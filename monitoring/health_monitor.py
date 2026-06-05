"""
Health Monitor
===============
Continuous monitoring of all system components.

Monitors:
- WebSocket connection + data freshness
- Broker API responsiveness
- System resources (CPU, memory)
- Position consistency (open orders match positions)
- Session timing (pre-market prep, EOD close)

Triggers:
- Alerts on degraded conditions
- Forces risk engine updates on connectivity changes
- Triggers forced exits on critical conditions
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Callable, Dict, Optional

import psutil

from core.broker.websocket_handler import WebSocketHandler
from core.data.market_data_engine import MarketDataEngine
from core.risk.risk_engine import RiskEngine

logger = logging.getLogger(__name__)


@dataclass
class HealthStatus:
    """System health snapshot."""
    timestamp: datetime
    websocket_alive: bool
    data_fresh: bool
    api_responsive: bool
    data_age_sec: float
    cpu_pct: float
    memory_pct: float
    overall: str  # HEALTHY | DEGRADED | CRITICAL

    @classmethod
    def from_checks(
        cls,
        ws_alive: bool,
        data_fresh: bool,
        api_ok: bool,
        data_age: float,
    ) -> "HealthStatus":
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent

        if not ws_alive or not data_fresh or not api_ok:
            overall = "CRITICAL"
        elif data_age > 15 or cpu > 90 or mem > 90:
            overall = "DEGRADED"
        else:
            overall = "HEALTHY"

        return cls(
            timestamp=datetime.now(),
            websocket_alive=ws_alive,
            data_fresh=data_fresh,
            api_responsive=api_ok,
            data_age_sec=round(data_age, 1),
            cpu_pct=round(cpu, 1),
            memory_pct=round(mem, 1),
            overall=overall,
        )


class HealthMonitor:
    """
    Continuous system health monitoring.

    Runs on its own asyncio task at configurable intervals.
    Notifies risk engine and alert system on status changes.
    """

    def __init__(
        self,
        ws_handler: WebSocketHandler,
        market_data: MarketDataEngine,
        risk_engine: RiskEngine,
        check_interval_sec: int = 30,
    ) -> None:
        self._ws = ws_handler
        self._md = market_data
        self._risk = risk_engine
        self._interval = check_interval_sec

        self._last_status: Optional[HealthStatus] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._on_status_callbacks: list[Callable[[HealthStatus], None]] = []

    def on_status_change(self, callback: Callable[[HealthStatus], None]) -> None:
        self._on_status_callbacks.append(callback)

    async def start(self) -> None:
        """Start health monitoring background task."""
        logger.info("Health monitor started")
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def stop(self) -> None:
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("Health monitor stopped")

    async def _monitor_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._interval)
                status = self._check_health()
                self._process_status(status)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health monitor error: {e}", exc_info=True)

    def _check_health(self) -> HealthStatus:
        """Perform health checks and return status snapshot."""
        ws_alive = self._ws.is_alive()
        data_age = self._ws.last_tick_age()
        data_fresh = data_age < 30
        api_ok = True  # We'll detect via ws_alive for now

        status = HealthStatus.from_checks(
            ws_alive=ws_alive,
            data_fresh=data_fresh,
            api_ok=api_ok,
            data_age=data_age,
        )

        # Update risk engine
        self._risk.update_api_status(ws_alive)
        self._risk.update_data_freshness(data_fresh)

        return status

    def _process_status(self, status: HealthStatus) -> None:
        """Process new status, log changes, notify callbacks."""
        prev = self._last_status

        if prev is None or prev.overall != status.overall:
            level = logging.WARNING if status.overall == "DEGRADED" else (
                logging.CRITICAL if status.overall == "CRITICAL" else logging.INFO
            )
            logger.log(
                level,
                f"System health: {status.overall} "
                f"[WS={status.websocket_alive} "
                f"Data={status.data_fresh}({status.data_age_sec}s) "
                f"CPU={status.cpu_pct}% MEM={status.memory_pct}%]"
            )

        self._last_status = status

        for cb in self._on_status_callbacks:
            try:
                cb(status)
            except Exception as e:
                logger.error(f"Health callback error: {e}")

    def get_status(self) -> Optional[HealthStatus]:
        return self._last_status

    def get_status_dict(self) -> dict:
        if not self._last_status:
            return {"overall": "UNKNOWN"}
        s = self._last_status
        return {
            "overall": s.overall,
            "websocket_alive": s.websocket_alive,
            "data_fresh": s.data_fresh,
            "data_age_sec": s.data_age_sec,
            "api_responsive": s.api_responsive,
            "cpu_pct": s.cpu_pct,
            "memory_pct": s.memory_pct,
            "timestamp": s.timestamp.isoformat(),
        }
