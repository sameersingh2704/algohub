"""
Telegram Alert Engine
======================
Sends trading alerts and system notifications via Telegram.

Alert types:
- System startup / pre-market notification
- Trade entry confirmation (instant, with strategy name)
- Trade exit with full P&L (instant)
- Risk events (daily loss, circuit breaker, kill switch)
- Daily summary (end of session)
- Morning prep (pre-session regime classification)

Non-blocking — alert failures never stop trading.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from telegram import Bot
    from telegram.error import TelegramError
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    logger.warning("python-telegram-bot not installed — Telegram alerts disabled")


class TelegramAlerts:
    """
    Telegram notification sender.

    All send methods are async and fire-and-forget.
    Failures are logged but never propagated to caller.
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        enabled: bool = True,
    ) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        has_real_credentials = self._has_real_credentials(bot_token, chat_id)
        self._enabled = enabled and TELEGRAM_AVAILABLE and has_real_credentials
        self._bot: Optional[Bot] = None

        if self._enabled:
            self._bot = Bot(token=bot_token)
            logger.info("Telegram alerts enabled")
        else:
            logger.info("Telegram alerts disabled or unavailable")

    @staticmethod
    def _has_real_credentials(bot_token: str, chat_id: str) -> bool:
        token = (bot_token or "").strip()
        chat = str(chat_id or "").strip()
        placeholders = ("YOUR_", "REPLACE_", "TODO", "CHANGE_ME")
        return bool(token and chat) and not (
            token.upper().startswith(placeholders) or chat.upper().startswith(placeholders)
        )

    async def send(self, message: str) -> None:
        """Send a raw message. Non-blocking. Failures are silent."""
        if not self._enabled or not self._bot:
            return
        try:
            await asyncio.wait_for(
                self._bot.send_message(
                    chat_id=self._chat_id,
                    text=message,
                    parse_mode="HTML",
                ),
                timeout=10.0,
            )
        except asyncio.CancelledError:
            # Re-raise only if our task was genuinely cancelled (e.g. Ctrl+C).
            # A spurious CancelledError from the Telegram HTTP stack should not
            # kill the trading session.
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                raise
            logger.warning("Telegram send interrupted (non-critical)")
        except Exception as e:
            logger.warning(f"Telegram send failed (non-critical): {e}")

    async def send_startup(
        self,
        session_date: date,
        mode: str,
        capital: float,
        strategies: int,
        waiting_until: Optional[str] = None,
    ) -> None:
        """Send system startup notification."""
        wait_line = f"\n⏳ Waiting until {waiting_until} to start trading..." if waiting_until else ""
        msg = (
            f"🚀 <b>ALGO SYSTEM STARTED</b>\n\n"
            f"Date: {session_date.strftime('%d %b %Y')}\n"
            f"Mode: {mode}\n"
            f"Capital: ₹{capital:,.0f}\n"
            f"Strategies: {strategies} active"
            f"{wait_line}\n\n"
            f"<i>System ready. Monitoring started.</i>"
        )
        await self.send(msg)

    async def send_morning_prep(
        self,
        session_date: date,
        regime: str,
        india_vix: float,
        cpr_width: float,
        gap_pct: float,
    ) -> None:
        """Send pre-session regime summary."""
        emoji = "🟢" if "TREND" in regime else ("🔴" if regime == "NO_TRADE" else "🟡")
        msg = (
            f"{emoji} <b>MORNING PREP — {session_date.strftime('%d %b %Y')}</b>\n\n"
            f"Regime: <b>{regime}</b>\n"
            f"India VIX: {india_vix:.1f}\n"
            f"CPR Width: {cpr_width:.1f} pts\n"
            f"Gap: {gap_pct:+.2f}%\n\n"
            f"{'✅ Trading ACTIVE — signals live from 09:35' if regime != 'NO_TRADE' else '🚫 NO TRADE DAY'}"
        )
        await self.send(msg)

    async def send_trade_entry(
        self,
        symbol: str,
        option_type: str,
        strike: int,
        lots: int,
        quantity: int,
        entry_price: float,
        hard_stop: float,       # underlying price level from signal
        target: float,          # underlying price level from signal
        signal_reason: str,
        strategy_name: str = "",
        spot_price: float = 0.0,
        premium_stop_pct: float = -0.30,    # option-premium hard-stop (e.g. -0.30)
        premium_target_pct: float = 0.45,   # option-premium profit-target (e.g. 0.45)
    ) -> None:
        """Send trade entry notification — instant on fill."""
        direction_emoji = "📈" if option_type == "CE" else "📉"
        side_word = "CALL" if option_type == "CE" else "PUT"
        strategy_line = f"Strategy: <b>{strategy_name}</b>\n" if strategy_name else ""

        # ── Underlying move to stop / target (in pts and %) ─────────────────
        spot = spot_price or hard_stop  # fallback if spot not provided
        stop_pts  = hard_stop - spot
        tgt_pts   = target    - spot
        stop_pct_ul  = (stop_pts  / spot * 100) if spot > 0 else 0
        tgt_pct_ul   = (tgt_pts   / spot * 100) if spot > 0 else 0

        # ── Option premium stop / target ────────────────────────────────────
        prem_stop   = entry_price * (1 + premium_stop_pct)    # e.g. ×0.70
        prem_target = entry_price * (1 + premium_target_pct)  # e.g. ×1.45
        prem_stop_pct_disp   = abs(premium_stop_pct   * 100)
        prem_target_pct_disp = abs(premium_target_pct * 100)

        # Direction sign for display
        stop_sign  = "+" if stop_pts  >= 0 else ""
        tgt_sign   = "+" if tgt_pts   >= 0 else ""

        msg = (
            f"{direction_emoji} <b>ENTRY — NIFTY {strike} {side_word}</b>\n\n"
            f"{strategy_line}"
            f"<code>{symbol}</code>\n"
            f"Spot:    ₹{spot:,.2f}\n"
            f"Premium: <b>₹{entry_price:.2f}</b>  ×  {lots}L  ({quantity} qty)\n\n"
            f"<b>Risk / Reward</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🛑 SL (underlying):  ₹{hard_stop:,.2f}  "
            f"({stop_sign}{stop_pts:.0f} pts  {stop_sign}{stop_pct_ul:.2f}%)\n"
            f"   SL (premium):     ₹{prem_stop:.2f}  (-{prem_stop_pct_disp:.0f}%)\n"
            f"🎯 Target (underlying): ₹{target:,.2f}  "
            f"({tgt_sign}{tgt_pts:.0f} pts  {tgt_sign}{tgt_pct_ul:.2f}%)\n"
            f"   Target (premium):    ₹{prem_target:.2f}  (+{prem_target_pct_disp:.0f}%)\n\n"
            f"Signal: <i>{signal_reason}</i>\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        await self.send(msg)

    async def send_trade_exit(
        self,
        symbol: str,
        exit_price: float,
        entry_price: float,
        gross_pnl: float,
        net_pnl: float,
        total_charges: float,
        exit_reason: str,
        pnl_pct: float,
        hold_time_min: float,
        strategy_name: str = "",
    ) -> None:
        """Send trade exit notification with full P&L — instant on close."""
        won = net_pnl > 0
        emoji = "✅" if won else "❌"
        result_word = "WIN" if won else "LOSS"
        pnl_pct_display = pnl_pct * 100  # pnl_pct is stored as decimal (0.45 = 45%)
        strategy_line = f"Strategy: <b>{strategy_name}</b>\n" if strategy_name else ""
        msg = (
            f"{emoji} <b>EXIT ({result_word}): {symbol}</b>\n\n"
            f"{strategy_line}"
            f"Entry: ₹{entry_price:.2f}  →  Exit: <b>₹{exit_price:.2f}</b>\n"
            f"P&L on premium: <b>{pnl_pct_display:+.1f}%</b>\n"
            f"Gross: ₹{gross_pnl:+.2f}\n"
            f"Charges: ₹{total_charges:.2f}\n"
            f"<b>Net: ₹{net_pnl:+.2f}</b>\n\n"
            f"Reason: {exit_reason}\n"
            f"Hold: {hold_time_min:.0f} min\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        await self.send(msg)

    async def send_risk_alert(self, event_type: str, message: str) -> None:
        """Send risk/system alert."""
        msg = (
            f"⚠️ <b>RISK ALERT: {event_type}</b>\n\n"
            f"{message}\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        await self.send(msg)

    async def send_circuit_breaker(self, reason: str) -> None:
        """Send circuit breaker notification — CRITICAL."""
        msg = (
            f"🚨 <b>CIRCUIT BREAKER TRIPPED</b>\n\n"
            f"Reason: {reason}\n"
            f"All trading HALTED.\n"
            f"Manual intervention required.\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        await self.send(msg)

    async def send_daily_summary(
        self,
        session_date: date,
        trades: int,
        wins: int,
        gross_pnl: float,
        net_pnl: float,
        total_charges: float,
        regime: str,
        equity: float,
    ) -> None:
        """Send end-of-day summary."""
        win_rate = wins / trades * 100 if trades > 0 else 0.0
        losses = trades - wins
        emoji = "📈" if net_pnl > 0 else ("📉" if net_pnl < 0 else "➡️")
        msg = (
            f"{emoji} <b>DAILY SUMMARY — {session_date.strftime('%d %b %Y')}</b>\n\n"
            f"Regime: {regime}\n"
            f"Trades: {trades}  (W:{wins} / L:{losses})  WR: {win_rate:.0f}%\n\n"
            f"Gross P&L:  ₹{gross_pnl:+,.2f}\n"
            f"Charges:    ₹{total_charges:,.2f}\n"
            f"<b>Net P&L:   ₹{net_pnl:+,.2f}</b>\n\n"
            f"Account Equity: ₹{equity:,.0f}"
        )
        await self.send(msg)
