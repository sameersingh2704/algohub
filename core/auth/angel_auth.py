"""
Angel One SmartAPI Authentication Manager
==========================================
Handles TOTP-based login, session management, token refresh,
and re-login on session expiry.

Production rules:
- Never log raw credentials
- Always validate session before use
- Auto-refresh token before expiry
- Full exception handling with structured logging
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import pyotp
from SmartApi import SmartConnect

logger = logging.getLogger(__name__)


@dataclass
class SessionState:
    """Immutable snapshot of a live session."""
    jwt_token: str
    refresh_token: str
    feed_token: str
    client_id: str
    login_time: datetime
    expiry_time: datetime

    @property
    def is_valid(self) -> bool:
        """Returns True if session has not expired (with 5-min buffer)."""
        return datetime.now() < (self.expiry_time - timedelta(minutes=5))

    @property
    def minutes_remaining(self) -> float:
        delta = self.expiry_time - datetime.now()
        return delta.total_seconds() / 60


class AuthError(Exception):
    """Raised on unrecoverable authentication failures."""
    pass


class AngelAuthManager:
    """
    Manages Angel One SmartAPI authentication lifecycle.

    Responsibilities:
    - TOTP-based login
    - Session token storage
    - Proactive token refresh
    - Re-login on expiry or failure
    - Audit logging of all auth events

    Usage:
        auth = AngelAuthManager(credentials)
        await auth.initialize()
        session = auth.current_session  # Always valid or raises
    """

    # SmartAPI JWT tokens are typically valid for 8 hours
    SESSION_DURATION_HOURS = 8
    MAX_LOGIN_RETRIES = 3
    RETRY_DELAY_SECONDS = 5

    def __init__(self, credentials: dict) -> None:
        self._api_key: str = credentials["api_key"]
        self._client_id: str = credentials["client_id"]
        self._password: str = credentials["client_password"]
        self._totp_secret: str = credentials["totp_secret"]
        self._session: Optional[SessionState] = None
        self._smart_api: Optional[SmartConnect] = None
        self._refresh_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize auth manager: perform login and start refresh loop."""
        logger.info("Initializing Angel One auth manager", extra={"client_id": self._client_id})
        await self._login_with_retry()
        self._refresh_task = asyncio.create_task(self._auto_refresh_loop())
        logger.info("Auth manager initialized successfully")

    async def shutdown(self) -> None:
        """Gracefully shut down: cancel refresh task and logout."""
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass

        if self._smart_api and self._session:
            try:
                self._smart_api.terminateSession(self._client_id)
                logger.info("Angel One session terminated")
            except Exception as e:
                logger.warning(f"Session termination failed (non-critical): {e}")

    @property
    def smart_api(self) -> SmartConnect:
        """Return the authenticated SmartConnect instance."""
        if self._smart_api is None:
            raise AuthError("SmartAPI not initialized. Call initialize() first.")
        if not self._session or not self._session.is_valid:
            raise AuthError("Session expired or invalid. Refresh required.")
        return self._smart_api

    @property
    def current_session(self) -> SessionState:
        """Return current session state (raises if invalid)."""
        if self._session is None or not self._session.is_valid:
            raise AuthError("No valid session. Call initialize() or check connection.")
        return self._session

    @property
    def feed_token(self) -> str:
        """Return feed token for WebSocket authentication."""
        return self.current_session.feed_token

    @property
    def jwt_token(self) -> str:
        """Return JWT token for REST API calls."""
        return self.current_session.jwt_token

    def is_authenticated(self) -> bool:
        """Non-raising check for session validity."""
        return self._session is not None and self._session.is_valid

    async def ensure_authenticated(self) -> None:
        """Guarantee session is valid; re-login if needed."""
        async with self._lock:
            if not self.is_authenticated():
                logger.warning("Session invalid — attempting re-login")
                await self._login_with_retry()

    def _generate_totp(self) -> str:
        """Generate current TOTP code from secret."""
        totp = pyotp.TOTP(self._totp_secret)
        code = totp.now()
        logger.debug("TOTP generated successfully")
        return code

    async def _login(self) -> SessionState:
        """
        Perform single login attempt.
        Returns SessionState on success.
        Raises AuthError on failure.
        """
        smart_api = SmartConnect(api_key=self._api_key)
        totp_code = self._generate_totp()

        loop = asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(
                None,
                lambda: smart_api.generateSession(self._client_id, self._password, totp_code)
            )
        except Exception as e:
            raise AuthError(f"Login API call failed: {e}") from e

        if not data or data.get("status") is False:
            error_msg = data.get("message", "Unknown error") if data else "No response"
            raise AuthError(f"Login failed: {error_msg}")

        token_data = data.get("data", {})
        jwt_token = token_data.get("jwtToken", "")
        refresh_token = token_data.get("refreshToken", "")

        if not jwt_token:
            raise AuthError("Login succeeded but no JWT token received")

        # Get feed token for WebSocket
        try:
            feed_token = smart_api.getfeedToken()
        except Exception as e:
            raise AuthError(f"Failed to get feed token: {e}") from e

        login_time = datetime.now()
        expiry_time = login_time + timedelta(hours=self.SESSION_DURATION_HOURS)

        session = SessionState(
            jwt_token=jwt_token,
            refresh_token=refresh_token,
            feed_token=feed_token,
            client_id=self._client_id,
            login_time=login_time,
            expiry_time=expiry_time,
        )

        self._smart_api = smart_api
        logger.info(
            "Login successful",
            extra={
                "client_id": self._client_id,
                "session_expires": expiry_time.isoformat(),
            }
        )
        return session

    async def _login_with_retry(self) -> None:
        """Attempt login with exponential backoff retry."""
        last_error: Optional[Exception] = None

        for attempt in range(1, self.MAX_LOGIN_RETRIES + 1):
            try:
                logger.info(f"Login attempt {attempt}/{self.MAX_LOGIN_RETRIES}")
                session = await self._login()
                self._session = session
                return
            except AuthError as e:
                last_error = e
                logger.error(f"Login attempt {attempt} failed: {e}")
                if attempt < self.MAX_LOGIN_RETRIES:
                    delay = self.RETRY_DELAY_SECONDS * attempt
                    logger.info(f"Retrying in {delay} seconds...")
                    await asyncio.sleep(delay)

        raise AuthError(
            f"All {self.MAX_LOGIN_RETRIES} login attempts failed. "
            f"Last error: {last_error}"
        )

    async def _try_token_refresh(self) -> bool:
        """
        Attempt to refresh token using refresh_token.
        Returns True if successful, False if re-login required.
        """
        if not self._session or not self._smart_api:
            return False

        try:
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(
                None,
                lambda: self._smart_api.generateToken(self._session.refresh_token)
            )
            if data and data.get("status"):
                token_data = data.get("data", {})
                new_jwt = token_data.get("jwtToken", "")
                new_refresh = token_data.get("refreshToken", "")

                if new_jwt:
                    old_session = self._session
                    self._session = SessionState(
                        jwt_token=new_jwt,
                        refresh_token=new_refresh or old_session.refresh_token,
                        feed_token=old_session.feed_token,
                        client_id=old_session.client_id,
                        login_time=datetime.now(),
                        expiry_time=datetime.now() + timedelta(hours=self.SESSION_DURATION_HOURS),
                    )
                    logger.info("Token refreshed successfully")
                    return True
        except Exception as e:
            logger.warning(f"Token refresh failed: {e}")

        return False

    async def _auto_refresh_loop(self) -> None:
        """
        Background task: refresh token 30 minutes before expiry.
        Falls back to full re-login if refresh fails.
        """
        logger.info("Auth auto-refresh loop started")

        while True:
            try:
                await asyncio.sleep(60)  # Check every minute

                if not self._session:
                    continue

                minutes_left = self._session.minutes_remaining
                logger.debug(f"Session minutes remaining: {minutes_left:.1f}")

                # Refresh 30 minutes before expiry
                if minutes_left <= 30:
                    logger.info(f"Session expiring in {minutes_left:.0f} min — refreshing")
                    async with self._lock:
                        success = await self._try_token_refresh()
                        if not success:
                            logger.warning("Token refresh failed — performing full re-login")
                            await self._login_with_retry()

            except asyncio.CancelledError:
                logger.info("Auth refresh loop cancelled")
                break
            except Exception as e:
                logger.error(f"Unexpected error in auth refresh loop: {e}", exc_info=True)
                await asyncio.sleep(30)  # Brief pause before retry
