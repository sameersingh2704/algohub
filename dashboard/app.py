"""
Multi-Strategy Trading Dashboard
===================================
FastAPI + plain HTML/JS — no external CDN dependencies.
Auto-refreshes every 2 seconds.

Panels:
  • Live clock + session info
  • Market: Spot, VWAP, RSI, ATR, EMA9/21/50, VIX, ORB
  • Risk: Daily P&L, loss%, consecutive losses, circuit breaker
  • Strategies: per-strategy state + trades + W/L + P&L
  • Open Positions: live positions with unrealised P&L
  • Closed Trades: today's completed trades, newest first
  • Equity Curve: SVG sparkline
"""

from __future__ import annotations

import csv
import glob
import os
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, List, Optional

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

# Folder where per-day trade CSVs live (relative to CWD = project root)
_TRADES_DIR = Path("data/live_trades")


# ── Serialisation helper ──────────────────────────────────────────────────────

def _j(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _j(asdict(value))
    if isinstance(value, dict):
        return {k: _j(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_j(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


# ── Factory ───────────────────────────────────────────────────────────────────

def create_dashboard_app(
    market_data,
    order_manager,
    health_monitor,
    risk_engine,
    strategy_manager=None,
    instrument_manager=None,
) -> FastAPI:

    _start_time = datetime.now()

    app = FastAPI(title="NIFTY Algo Dashboard", docs_url=None, redoc_url=None)

    # ── API ───────────────────────────────────────────────────────────────────

    @app.get("/api/status")
    async def status() -> dict:
        ms = market_data.get_market_state()
        risk = risk_engine.get_status_dict()
        uptime = int((datetime.now() - _start_time).total_seconds() / 60)

        # Market indicators
        market = {}
        if ms:
            market = {
                "spot":        round(ms.spot_price, 2),
                "vwap":        round(ms.vwap, 2),
                "vwap_slope":  round(getattr(ms, "vwap_slope", 0), 5),
                "rsi":         round(getattr(ms, "rsi", 50), 1),
                "atr":         round(getattr(ms, "atr", 0), 2),
                "ema9":        round(getattr(ms, "ema_9", 0) or 0, 2),
                "ema21":       round(getattr(ms, "ema_21", 0) or 0, 2),
                "ema50":       round(getattr(ms, "ema_50", 0) or 0, 2),
                "bb_upper":    round(getattr(ms, "bb_upper", 0) or 0, 2),
                "bb_lower":    round(getattr(ms, "bb_lower", 0) or 0, 2),
                "vix":         round(ms.india_vix, 2),
                "orb_high":    round(ms.orb_high, 2),
                "orb_low":     round(ms.orb_low, 2),
                "orb_range":   round(ms.orb_range, 2),
                "orb_locked":  getattr(ms, "orb_locked", ms.orb_range > 0),
                "is_fresh":    ms.is_data_fresh,
                "volume_ratio": round(getattr(ms, "volume_ratio", 1.0), 2),
            }

        # Strategy states
        strategies = []
        if strategy_manager:
            today = date.today()
            closed = order_manager.closed_trades
            for s in strategy_manager._strategies.values():
                strat_trades = [t for t in closed
                                if t.strategy_name == s.name and t.session_date == today]
                wins = sum(1 for t in strat_trades if t.net_pnl > 0)
                pnl  = sum(t.net_pnl for t in strat_trades)
                open_pos = order_manager.get_trade_for_strategy(s.name)
                state_val = s.state.value if hasattr(s.state, "value") else str(s.state)
                strategies.append({
                    "name":   s.name,
                    "state":  state_val,
                    "trades": len(strat_trades),
                    "wins":   wins,
                    "losses": len(strat_trades) - wins,
                    "pnl":    round(pnl, 2),
                    "has_position": open_pos is not None,
                })

        # Open positions + unrealised P&L
        open_positions = []
        for trade in order_manager.open_trades.values():
            quote = market_data.get_option_quote(trade.token) if hasattr(market_data, "get_option_quote") else None
            ltp = quote.ltp if quote and quote.ltp > 0 else trade.entry_price
            unreal_gross = (ltp - trade.entry_price) * trade.quantity
            open_positions.append({
                "symbol":      trade.symbol,
                "strategy":    trade.strategy_name or "—",
                "option_type": trade.option_type,
                "strike":      trade.strike,
                "lots":        trade.lots,
                "entry_price": round(trade.entry_price, 2),
                "ltp":         round(ltp, 2),
                "unreal_pnl":  round(unreal_gross, 2),
                "unreal_pct":  round((ltp / trade.entry_price - 1) * 100, 1) if trade.entry_price else 0,
                "entry_time":  trade.entry_time.strftime("%H:%M:%S") if trade.entry_time else "",
                "entry_spot":  round(trade.entry_spot, 2),
                "signal":      trade.signal_reason or "",
            })

        # Closed trades today — merge in-memory + today's CSV so trades
        # that exist only on disk (e.g. after a restart) still appear.
        today = date.today()
        today_str = today.isoformat()

        # Start with in-memory closed trades
        mem_rows: dict = {}
        for t in order_manager.closed_trades:
            if t.session_date == today:
                hold = int((t.exit_time - t.entry_time).total_seconds() / 60) \
                       if t.exit_time and t.entry_time else 0
                mem_rows[t.trade_id] = {
                    "exit_time":   t.exit_time.strftime("%H:%M:%S") if t.exit_time else "",
                    "strategy":    t.strategy_name or "—",
                    "symbol":      t.symbol,
                    "option_type": t.option_type,
                    "entry":       round(t.entry_price, 2),
                    "exit":        round(t.exit_price, 2),
                    "pnl_pct":     round(t.pnl_pct * 100, 1),
                    "net_pnl":     round(t.net_pnl, 2),
                    "reason":      t.exit_reason,
                    "hold_min":    hold,
                    "_sort_key":   t.exit_time or datetime.min,
                }

        # Merge today's CSV for any trades not already in memory
        csv_path = _TRADES_DIR / f"{today_str}_live_trades.csv"
        if csv_path.exists():
            csv_entries: dict = {}
            csv_exits:   dict = {}
            with open(csv_path, newline="") as _f:
                for row in csv.DictReader(_f):
                    tid = row.get("trade_id", "")
                    if not tid:
                        continue
                    if row["event_type"] == "ENTRY":
                        csv_entries[tid] = row
                    elif row["event_type"] == "EXIT":
                        csv_exits[tid] = row
            for tid, ex in csv_exits.items():
                if tid in mem_rows or tid not in csv_entries:
                    continue
                en = csv_entries[tid]
                net  = float(ex.get("net_pnl") or 0)
                hold = int(float(ex.get("hold_min") or 0))
                exit_t = ex.get("exit_time", "")
                try:
                    sort_key = datetime.strptime(exit_t, "%H:%M:%S").replace(
                        year=today.year, month=today.month, day=today.day)
                except Exception:
                    sort_key = datetime.min
                mem_rows[tid] = {
                    "exit_time":   exit_t,
                    "strategy":    en.get("strategy", "—"),
                    "symbol":      en.get("symbol", ""),
                    "option_type": en.get("option_type", ""),
                    "entry":       float(en.get("entry_price") or 0),
                    "exit":        float(ex.get("exit_price") or 0),
                    "pnl_pct":     float(ex.get("pnl_pct") or 0),
                    "net_pnl":     net,
                    "reason":      ex.get("exit_reason", ""),
                    "hold_min":    hold,
                    "_sort_key":   sort_key,
                }

        # Equity curve before popping _sort_key (oldest → newest)
        equity_pts = []
        running = 0.0
        for r in sorted(mem_rows.values(), key=lambda r: r["_sort_key"]):
            running += float(r.get("net_pnl") or 0)
            equity_pts.append(round(running, 2))

        closed_out = sorted(mem_rows.values(), key=lambda r: r["_sort_key"], reverse=True)
        for r in closed_out:
            r.pop("_sort_key", None)

        daily_pnl = sum(float(r.get("net_pnl") or 0) for r in closed_out)
        total_today = len(closed_out)
        wins_today  = sum(1 for r in closed_out if float(r.get("net_pnl") or 0) > 0)

        return {
            "ts":        datetime.now().isoformat(),
            "session":   today.strftime("%d %b %Y"),
            "mode":      "PAPER",
            "uptime_min": uptime,
            "market":    market,
            "risk":      risk,
            "health": {
                "ws_alive":     health_monitor._ws.is_alive(),
                "tick_age_sec": round(health_monitor._ws.last_tick_age(), 1),
                "trading_ok":   risk_engine.is_trading_permitted,
            },
            "summary": {
                "daily_pnl":  round(daily_pnl, 2),
                "trades":     total_today,
                "wins":       wins_today,
                "losses":     total_today - wins_today,
                "win_rate":   round(wins_today / total_today * 100, 1) if total_today else 0,
            },
            "strategies":      strategies,
            "open_positions":  open_positions,
            "closed_trades":   closed_out,
            "equity_curve":    equity_pts,
        }

    # ── Test-trade injection ──────────────────────────────────────────────────

    @app.post("/api/test-trade")
    async def inject_test_trade() -> dict:
        """
        Inject a test NIFTY CE open position using the live ATM strike and
        real instrument token so LTP and unrealised P&L update live.
        """
        from core.execution.order_manager import TradeRecord
        from datetime import date

        ms = market_data.get_market_state()
        spot = ms.spot_price if ms and ms.spot_price > 0 else 23300.0
        strike = round(spot / 50) * 50
        expiry_date = date(2026, 6, 9)

        # Resolve real Angel One token via instrument manager
        token = None
        symbol = f"NIFTY09JUN26{strike}CE"
        if instrument_manager:
            token = instrument_manager.get_option_token("NIFTY", strike, "CE", expiry_date)
            if token:
                info = instrument_manager.get_instrument(token)
                if info:
                    symbol = info.symbol

        # Fall back to token from live quote if instrument_manager lookup missed
        if not token:
            token = f"NIFTY_CE_{strike}_TEST"

        # Entry price: use live quote if available, else estimate
        quote = market_data.get_option_quote(token) if token else None
        entry_price = round(quote.ltp, 2) if quote and quote.ltp > 0 else round(spot * 0.006, 2)

        trade = TradeRecord(
            trade_id=str(uuid.uuid4()),
            session_date=date.today(),
            symbol=symbol,
            token=token,
            option_type="CE",
            strike=strike,
            expiry=expiry_date,
            lots=1,
            quantity=75,
            entry_price=entry_price,
            entry_time=datetime.now(),
            entry_spot=spot,
            entry_vwap=ms.vwap if ms else spot,
            signal_reason="DMD_ORB_BREAKOUT (TEST)",
            strategy_name="DailyMomentumDrive",
            status="OPEN",
        )

        order_manager.restore_open_trade(trade)
        return {
            "injected": True,
            "trade_id": trade.trade_id,
            "symbol": symbol,
            "token": token,
            "strike": strike,
            "entry_price": entry_price,
            "spot_at_entry": spot,
            "live_quote": quote is not None,
        }

    @app.post("/api/clear-test-trade")
    async def clear_test_trade() -> dict:
        """Remove the injected test trade from the dashboard."""
        tid = order_manager._strategy_positions.get("DailyMomentumDrive")
        if tid and tid in order_manager._open_trades:
            trade = order_manager._open_trades[tid]
            if trade.signal_reason and "(TEST)" in trade.signal_reason:
                del order_manager._open_trades[tid]
                del order_manager._strategy_positions["DailyMomentumDrive"]
                return {"cleared": True, "symbol": trade.symbol}
        return {"cleared": False, "reason": "No test trade found"}

    # ── Historical trade data ─────────────────────────────────────────────────

    @app.get("/api/history/dates")
    async def history_dates() -> dict:
        """Return available trading dates (newest first)."""
        dates = []
        for path in sorted(_TRADES_DIR.glob("*_live_trades.csv"), reverse=True):
            d = path.name.split("_live_trades")[0]
            try:
                date.fromisoformat(d)
                dates.append(d)
            except ValueError:
                pass
        return {"dates": dates}

    @app.get("/api/history")
    async def history(d: str = Query(default="")) -> dict:
        """
        Return closed trades for a given date (YYYY-MM-DD).
        Also returns a summary row (total, wins, losses, net P&L).
        If no date given, returns today merged with in-memory closed trades.
        """
        target = d or date.today().isoformat()
        csv_path = _TRADES_DIR / f"{target}_live_trades.csv"

        entries: dict = {}
        exits:   dict = {}

        # Always merge in-memory closed trades for today so live session data
        # appears immediately without waiting for a CSV flush.
        if target == date.today().isoformat():
            for t in order_manager.closed_trades:
                if t.session_date.isoformat() == target:
                    entries[t.trade_id] = {
                        "trade_id":    t.trade_id,
                        "strategy":    t.strategy_name,
                        "symbol":      t.symbol,
                        "option_type": t.option_type,
                        "strike":      t.strike,
                        "lots":        t.lots,
                        "entry_time":  t.entry_time.strftime("%H:%M:%S") if t.entry_time else "",
                        "entry_price": t.entry_price,
                        "entry_spot":  t.entry_spot,
                        "exit_time":   t.exit_time.strftime("%H:%M:%S") if t.exit_time else "",
                        "exit_price":  t.exit_price,
                        "gross_pnl":   t.gross_pnl,
                        "charges":     t.total_charges,
                        "net_pnl":     t.net_pnl,
                        "pnl_pct":     round(t.pnl_pct * 100, 1),
                        "exit_reason": t.exit_reason,
                        "hold_min":    int((t.exit_time - t.entry_time).total_seconds() / 60)
                                       if t.exit_time and t.entry_time else 0,
                        "signal":      t.signal_reason or "",
                    }

        # Merge CSV file (may fill gaps or provide past-day data)
        if csv_path.exists():
            with open(csv_path, newline="") as f:
                for row in csv.DictReader(f):
                    tid = row.get("trade_id", "")
                    if not tid:
                        continue
                    if row["event_type"] == "ENTRY":
                        entries.setdefault(tid, row)
                    elif row["event_type"] == "EXIT":
                        exits[tid] = row

            # Build complete records from CSV (for past days or CSV-only trades)
            for tid, ex in exits.items():
                if tid not in entries:
                    continue
                en = entries[tid]
                if tid in entries and "net_pnl" not in entries[tid]:
                    # Merge exit data into the entry record
                    net = float(ex.get("net_pnl") or 0)
                    entries[tid].update({
                        "exit_time":   ex.get("exit_time", ""),
                        "exit_price":  float(ex.get("exit_price") or 0),
                        "gross_pnl":   float(ex.get("gross_pnl") or 0),
                        "charges":     float(ex.get("charges") or 0),
                        "net_pnl":     net,
                        "pnl_pct":     float(ex.get("pnl_pct") or 0),
                        "exit_reason": ex.get("exit_reason", ""),
                        "hold_min":    int(ex.get("hold_min") or 0),
                        "signal":      en.get("signal_reason", ""),
                    })

        # Build final list: closed trades first (newest exit), then open/orphaned
        all_records = []
        for tid, en in entries.items():
            if en.get("exit_time"):
                # Fully closed
                en["status"] = "CLOSED"
                all_records.append(en)
            else:
                # ENTRY only — open, stuck, or force-exited without CSV flush
                en["status"] = "OPEN"
                en.setdefault("exit_time", "")
                en.setdefault("exit_price", "")
                en.setdefault("net_pnl", "")
                en.setdefault("pnl_pct", "")
                en.setdefault("exit_reason", "NO EXIT")
                all_records.append(en)

        all_records.sort(key=lambda r: (r["status"] == "OPEN", r.get("exit_time", "") or r.get("entry_time", "")), reverse=True)

        closed_only = [r for r in all_records if r["status"] == "CLOSED"]
        total = len(closed_only)
        wins  = sum(1 for r in closed_only if float(r.get("net_pnl") or 0) > 0)
        net   = sum(float(r.get("net_pnl") or 0) for r in closed_only)

        return {
            "date":   target,
            "trades": all_records,
            "summary": {
                "total":    total,
                "wins":     wins,
                "losses":   total - wins,
                "win_rate": round(wins / total * 100, 1) if total else 0,
                "net_pnl":  round(net, 2),
                "open":     len([r for r in all_records if r["status"] == "OPEN"]),
            },
        }

    # ── Force-exit any open position ─────────────────────────────────────────

    @app.post("/api/force-exit/{strategy_name}")
    async def force_exit(strategy_name: str) -> dict:
        """
        Manually close an open position at current market price.
        Fires all on_trade_close callbacks so the exit is written to CSV,
        journal, and risk engine — identical to a normal strategy exit.
        """
        tid = order_manager._strategy_positions.get(strategy_name)
        if not tid or tid not in order_manager._open_trades:
            return {"ok": False, "reason": f"No open position for {strategy_name}"}

        trade = order_manager._open_trades[tid]
        symbol = trade.symbol

        # Current price from live feed; fall back to entry price
        quote = market_data.get_option_quote(trade.token) if trade.token else None
        exit_price = quote.ltp if quote and quote.ltp > 0 else trade.entry_price

        # Use the proper close method — fires CSV write + journal callbacks
        closed = order_manager.close_trade_sync(
            trade_id=tid,
            exit_price=exit_price,
            exit_reason="MANUAL_FORCE_EXIT",
        )
        if not closed:
            return {"ok": False, "reason": "close_trade_sync failed"}

        # Reset strategy state machine
        if strategy_manager:
            strategy_manager.on_exit_filled(
                strategy_name=strategy_name,
                fill_price=exit_price,
                pnl=closed.net_pnl,
                exit_reason="MANUAL_FORCE_EXIT",
            )

        return {
            "ok": True,
            "strategy": strategy_name,
            "symbol": symbol,
            "exit_price": exit_price,
            "net_pnl": round(closed.net_pnl, 2),
            "pnl_pct": round(closed.pnl_pct * 100, 2),
        }

    # ── All-time history (across every CSV file) ──────────────────────────────

    @app.get("/api/history/all")
    async def history_all(strategy: str = "", status: str = "") -> dict:
        """
        Aggregate every trade from every CSV file for the full history view.
        Optional filters: strategy name, status (CLOSED / OPEN / all).
        Returns trades list + per-strategy summary + per-day equity.
        """
        all_trades = []

        # Pull from in-memory closed trades first (current session)
        for t in order_manager.closed_trades:
            all_trades.append({
                "date":         t.session_date.isoformat() if t.session_date else "",
                "trade_id":     t.trade_id,
                "strategy":     t.strategy_name or "—",
                "symbol":       t.symbol,
                "option_type":  t.option_type,
                "strike":       t.strike,
                "lots":         t.lots,
                "entry_time":   t.entry_time.strftime("%H:%M:%S") if t.entry_time else "",
                "entry_price":  t.entry_price,
                "entry_spot":   t.entry_spot,
                "exit_time":    t.exit_time.strftime("%H:%M:%S") if t.exit_time else "",
                "exit_price":   t.exit_price,
                "gross_pnl":    t.gross_pnl,
                "charges":      t.total_charges,
                "net_pnl":      t.net_pnl,
                "pnl_pct":      round(t.pnl_pct * 100, 2),
                "exit_reason":  t.exit_reason,
                "hold_min":     int((t.exit_time - t.entry_time).total_seconds() / 60)
                                if t.exit_time and t.entry_time else 0,
                "signal":       t.signal_reason or "",
                "status":       "CLOSED",
            })

        seen_ids = {t["trade_id"] for t in all_trades}

        # Merge all CSV files
        for csv_path in sorted(_TRADES_DIR.glob("*_live_trades.csv")):
            date_str = csv_path.name.split("_live_trades")[0]
            try:
                date.fromisoformat(date_str)
            except ValueError:
                continue

            entries: dict = {}
            exits:   dict = {}
            with open(csv_path, newline="") as f:
                for row in csv.DictReader(f):
                    tid = row.get("trade_id", "")
                    if not tid:
                        continue
                    if row["event_type"] == "ENTRY":
                        entries[tid] = row
                    elif row["event_type"] == "EXIT":
                        exits[tid] = row

            for tid, en in entries.items():
                if tid in seen_ids:
                    continue
                seen_ids.add(tid)
                ex = exits.get(tid)
                net = float(ex["net_pnl"]) if ex and ex.get("net_pnl") else None
                all_trades.append({
                    "date":        date_str,
                    "trade_id":    tid,
                    "strategy":    en.get("strategy", "—"),
                    "symbol":      en.get("symbol", ""),
                    "option_type": en.get("option_type", ""),
                    "strike":      en.get("strike", ""),
                    "lots":        en.get("lots", 1),
                    "entry_time":  en.get("entry_time", ""),
                    "entry_price": float(en.get("entry_price") or 0),
                    "entry_spot":  float(en.get("entry_spot") or 0),
                    "exit_time":   ex["exit_time"] if ex else "",
                    "exit_price":  float(ex["exit_price"]) if ex and ex.get("exit_price") else "",
                    "gross_pnl":   float(ex["gross_pnl"]) if ex and ex.get("gross_pnl") else "",
                    "charges":     float(ex["charges"]) if ex and ex.get("charges") else 0,
                    "net_pnl":     net if net is not None else "",
                    "pnl_pct":     float(ex["pnl_pct"]) if ex and ex.get("pnl_pct") else "",
                    "exit_reason": ex["exit_reason"] if ex else "NO EXIT",
                    "hold_min":    int(float(ex["hold_min"])) if ex and ex.get("hold_min") else 0,
                    "signal":      en.get("signal_reason", ""),
                    "status":      "CLOSED" if ex else "OPEN",
                })

        # Sort newest first
        all_trades.sort(key=lambda r: (r["date"], r.get("entry_time", "")), reverse=True)

        # Apply filters
        if strategy:
            all_trades = [t for t in all_trades if t["strategy"] == strategy]
        if status == "CLOSED":
            all_trades = [t for t in all_trades if t["status"] == "CLOSED"]
        elif status == "OPEN":
            all_trades = [t for t in all_trades if t["status"] == "OPEN"]

        closed = [t for t in all_trades if t["status"] == "CLOSED"]
        total  = len(closed)
        wins   = sum(1 for t in closed if float(t.get("net_pnl") or 0) > 0)
        net_total = sum(float(t.get("net_pnl") or 0) for t in closed)

        # Per-strategy breakdown
        strat_map: dict = {}
        for t in closed:
            s = t["strategy"]
            if s not in strat_map:
                strat_map[s] = {"trades": 0, "wins": 0, "net_pnl": 0.0}
            strat_map[s]["trades"] += 1
            strat_map[s]["net_pnl"] = round(strat_map[s]["net_pnl"] + float(t.get("net_pnl") or 0), 2)
            if float(t.get("net_pnl") or 0) > 0:
                strat_map[s]["wins"] += 1

        per_strategy = [
            {
                "name":     k,
                "trades":   v["trades"],
                "wins":     v["wins"],
                "losses":   v["trades"] - v["wins"],
                "win_rate": round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0,
                "net_pnl":  v["net_pnl"],
            }
            for k, v in sorted(strat_map.items(), key=lambda x: x[1]["net_pnl"], reverse=True)
        ]

        # Daily equity curve (running P&L per day, closed only)
        day_pnl: dict = {}
        for t in closed:
            d_key = t["date"]
            day_pnl[d_key] = round(day_pnl.get(d_key, 0.0) + float(t.get("net_pnl") or 0), 2)
        daily_equity = [{"date": k, "net_pnl": v} for k, v in sorted(day_pnl.items())]

        # Unique strategy names for filter dropdown
        strategies = sorted({t["strategy"] for t in all_trades if t["strategy"] != "—"})

        return {
            "trades":       all_trades,
            "summary": {
                "total":    total,
                "wins":     wins,
                "losses":   total - wins,
                "win_rate": round(wins / total * 100, 1) if total else 0,
                "net_pnl":  round(net_total, 2),
                "open":     len([t for t in all_trades if t["status"] == "OPEN"]),
                "days":     len(day_pnl),
            },
            "per_strategy":  per_strategy,
            "daily_equity":  daily_equity,
            "strategies":    strategies,
        }

    # ── HTML ──────────────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _HTML

    return app


# ── Dashboard HTML ────────────────────────────────────────────────────────────
# Professional redesign: stable CSS layout, overflow-safe tables, defensive JS.

_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NIFTY Algo · Live</title>
<style>
/* ── reset ── */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{font-size:13px;-webkit-text-size-adjust:100%}

/* ══════════════════════════════════════════════
   DESIGN TOKENS — Pure Black × Electric Blue
   Terminal aesthetic: black canvas, blue text.
══════════════════════════════════════════════ */
:root{
  /* ── backgrounds ── */
  --bg:        #0f1117;
  --surface:   #161b27;
  --surface2:  #1c2233;
  --surface3:  #212840;

  /* ── borders ── */
  --border:    #222c42;
  --border2:   #2a3655;
  --border3:   #3a4f7a;

  /* ── text ── */
  --text:      #e8eaed;
  --text2:     #8a93a8;
  --text3:     #4a5568;

  /* ── accent blue ── */
  --blue:      #2962ff;
  --blue-lt:   #5c8aff;
  --blue-bg:   rgba(41,98,255,.12);
  --blue-glow: rgba(41,98,255,.25);

  /* ── cyan ── */
  --cyan:      #00bcd4;
  --cyan-bg:   rgba(0,188,212,.08);

  /* ── status colours ── */
  --green:     #26a69a;
  --green-dim: #1b7a70;
  --green-bg:  rgba(38,166,154,.10);
  --red:       #ef5350;
  --red-dim:   #c62828;
  --red-bg:    rgba(239,83,80,.10);
  --yellow:    #ffa726;
  --yellow-bg: rgba(255,167,38,.08);
  --purple:    #ab47bc;
  --orange:    #ff7043;

  /* ── type ── */
  --num-font:"SF Mono","JetBrains Mono","Fira Code","Consolas","Menlo",monospace;
  --ui-font: -apple-system,BlinkMacSystemFont,"Segoe UI","Inter",sans-serif;

  /* shape */
  --r:  8px;
  --rs: 5px;
}
/* ══════════════════════════════════════════════
   BASE
══════════════════════════════════════════════ */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{font-size:13px}
body{background:var(--bg);color:var(--text);font-family:var(--ui-font);font-size:13px;
  min-height:100vh;overflow-x:hidden;-webkit-font-smoothing:antialiased;line-height:1.5;letter-spacing:.01em}
.num{font-family:var(--num-font)}

/* ══════════════════════════════════════════════
   TABS
══════════════════════════════════════════════ */
.tab-nav{display:flex;gap:2px;flex-shrink:0}
.tab-btn{padding:5px 16px;border-radius:var(--rs);font-size:11px;font-weight:700;letter-spacing:.3px;
  cursor:pointer;border:1px solid var(--border2);background:transparent;color:var(--text2);
  font-family:var(--ui-font);transition:all .15s ease;white-space:nowrap}
.tab-btn:hover{color:var(--text);border-color:var(--border3)}
.tab-btn.active{background:var(--blue-bg);color:var(--blue-lt);border-color:rgba(45,124,246,.5);
  box-shadow:0 0 10px rgba(26,140,255,.15)}
.tab-panel{display:none}
.tab-panel.active{display:block}

/* ══════════════════════════════════════════════
   TOP BAR
══════════════════════════════════════════════ */
.topbar{position:sticky;top:0;z-index:200;height:52px;display:flex;align-items:center;gap:14px;
  padding:0 22px;background:var(--surface);
  border-bottom:1px solid var(--border2);
  box-shadow:0 1px 0 var(--border3),0 4px 24px rgba(0,0,0,.5)}
.topbar-logo{font-family:var(--num-font);font-size:15px;font-weight:700;letter-spacing:.8px;
  color:var(--text);white-space:nowrap;flex-shrink:0}
.topbar-badge{padding:2px 9px;border-radius:var(--rs);font-size:9px;font-weight:800;letter-spacing:1px;
  text-transform:uppercase;background:var(--blue-bg);color:var(--blue-lt);
  border:1px solid rgba(45,124,246,.3);flex-shrink:0}
.topbar-divider{width:1px;height:20px;background:var(--border2);flex-shrink:0}
.topbar-item{color:var(--text2);font-size:12px;white-space:nowrap}
.topbar-right{margin-left:auto;display:flex;align-items:center;gap:10px;flex-shrink:0}
#topbar-clock{font-family:var(--num-font);font-size:14px;font-weight:600;color:var(--text);letter-spacing:.5px;white-space:nowrap}
.live-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--green);
  margin-right:6px;box-shadow:0 0 6px var(--green);animation:livepulse 2s ease-in-out infinite}
@keyframes livepulse{0%,100%{opacity:1;box-shadow:0 0 6px var(--green)}50%{opacity:.4;box-shadow:0 0 2px var(--green)}}

/* ══════════════════════════════════════════════
   BUTTONS
══════════════════════════════════════════════ */
.btn{display:inline-flex;align-items:center;gap:5px;padding:5px 13px;border-radius:var(--rs);
  font-size:11px;font-weight:700;letter-spacing:.2px;cursor:pointer;border:1px solid;
  font-family:var(--ui-font);transition:all .15s ease;white-space:nowrap}
.btn:hover{transform:translateY(-1px)}
.btn:active{transform:translateY(0)}
.btn-green{background:var(--green-bg);color:var(--green);border-color:var(--green-dim)}
.btn-green:hover{background:rgba(0,214,143,.15);box-shadow:0 4px 12px rgba(0,214,143,.2)}
.btn-red{background:var(--red-bg);color:var(--red);border-color:var(--red-dim)}
.btn-red:hover{background:rgba(255,77,106,.15);box-shadow:0 4px 12px rgba(255,77,106,.2)}
.btn:disabled{opacity:.35;cursor:not-allowed;transform:none;box-shadow:none}

/* ══════════════════════════════════════════════
   PAGE
══════════════════════════════════════════════ */
.page{padding:16px 20px;display:flex;flex-direction:column;gap:14px;max-width:1900px;margin:0 auto}

/* ══════════════════════════════════════════════
   MARKET STRIP
══════════════════════════════════════════════ */
.market-strip{display:flex;gap:8px;overflow-x:auto;padding-bottom:3px;
  scrollbar-width:thin;scrollbar-color:var(--border2) transparent}
.market-strip::-webkit-scrollbar{height:3px}
.market-strip::-webkit-scrollbar-thumb{background:var(--border2);border-radius:2px}
.mcard{flex:0 0 auto;min-width:112px;background:var(--surface);border:1px solid var(--border);
  border-top:2px solid var(--border3);border-radius:var(--r);padding:10px 13px;
  display:flex;flex-direction:column;gap:4px;transition:border-color .2s,background .2s}
.mcard:hover{background:var(--surface2);border-top-color:var(--blue-lt)}
.mcard-label{font-size:9px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:1px}
.mcard-val{font-family:var(--num-font);font-size:19px;font-weight:700;color:var(--text);line-height:1.1}
.mcard-sub{font-size:10px;color:var(--text2)}
.mcard-spot{border-top-color:var(--blue)}
.mcard-spot .mcard-val{font-size:24px}

/* ══════════════════════════════════════════════
   MAIN GRID
══════════════════════════════════════════════ */
.main-grid{display:grid;grid-template-columns:230px 1fr 230px;gap:14px;min-width:0}
@media(max-width:1200px){.main-grid{grid-template-columns:200px 1fr}}
@media(max-width:800px){.main-grid{grid-template-columns:1fr}}
.main-left,.main-right{display:flex;flex-direction:column;gap:14px;min-width:0}

/* ══════════════════════════════════════════════
   CARDS
══════════════════════════════════════════════ */
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);
  padding:16px;min-width:0;position:relative;overflow:hidden}
.card::before{content:"";position:absolute;inset:0 0 auto 0;height:1px;
  background:linear-gradient(90deg,var(--blue),transparent);opacity:.5}
.card-title{font-size:9px;font-weight:800;color:var(--text3);text-transform:uppercase;
  letter-spacing:1.2px;margin-bottom:14px;display:flex;align-items:center;gap:7px}
.card-title::before{content:"";display:block;width:3px;height:12px;
  background:var(--blue);border-radius:2px;flex-shrink:0}
.pnl-big{font-family:var(--num-font);font-size:32px;font-weight:800;margin:4px 0 14px;letter-spacing:-.5px}
.kv-row{display:flex;justify-content:space-between;align-items:center;
  padding:7px 0;border-top:1px solid var(--border);font-size:12px}
.kv-row:first-of-type{border-top:none;padding-top:0}
.kv-key{color:var(--text2)}
.kv-val{font-family:var(--num-font);font-weight:600;color:var(--text)}
.risk-row{display:flex;justify-content:space-between;align-items:center;
  padding:8px 0;border-top:1px solid var(--border);font-size:12px;gap:8px}
.risk-row:first-of-type{border-top:none;padding-top:0}
.risk-label{color:var(--text2);white-space:nowrap}
.risk-val{font-family:var(--num-font);font-weight:600;text-align:right}

/* ══════════════════════════════════════════════
   TABLES
══════════════════════════════════════════════ */
.table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:12px;min-width:380px}
thead{background:linear-gradient(180deg,var(--surface2),var(--surface));border-bottom:1px solid var(--border2)}
thead th{color:var(--text3);font-weight:700;font-size:9px;text-transform:uppercase;
  letter-spacing:.8px;padding:9px 12px;text-align:left;white-space:nowrap}
tbody tr{border-top:1px solid var(--border);transition:background .1s}
tbody tr:hover{background:var(--surface3)}
td{padding:9px 12px;white-space:nowrap;font-size:12px;vertical-align:middle;color:var(--text)}

/* ══════════════════════════════════════════════
   STATE BADGES
══════════════════════════════════════════════ */
.badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:9px;font-weight:800;
  letter-spacing:.4px;text-transform:uppercase;border:1px solid}
.b-IDLE        {background:rgba(62,78,106,.2);color:var(--text3);border-color:rgba(62,78,106,.3)}
.b-WAITING     {background:var(--yellow-bg);color:var(--yellow);border-color:rgba(255,184,48,.3)}
.b-ARMED       {background:rgba(255,124,58,.1);color:var(--orange);border-color:rgba(255,124,58,.3)}
.b-ENTRY_PENDING{background:var(--blue-bg);color:var(--blue-lt);border-color:rgba(45,124,246,.35);box-shadow:0 0 8px rgba(45,124,246,.15)}
.b-ACTIVE_POSITION{background:var(--green-bg);color:var(--green);border-color:rgba(0,214,143,.35);box-shadow:0 0 8px rgba(0,214,143,.15)}
.b-EXIT_PENDING{background:rgba(255,124,58,.1);color:var(--orange);border-color:rgba(255,124,58,.3)}
.b-COOLDOWN    {background:rgba(157,114,255,.1);color:var(--purple);border-color:rgba(157,114,255,.3)}
.b-HALTED      {background:var(--red-bg);color:var(--red);border-color:rgba(255,77,106,.35);box-shadow:0 0 8px rgba(255,77,106,.15)}

/* ══════════════════════════════════════════════
   COLOUR HELPERS
══════════════════════════════════════════════ */
.c-green{color:var(--green)!important}.c-red{color:var(--red)!important}
.c-yellow{color:var(--yellow)!important}.c-blue{color:var(--blue-lt)!important}
.c-cyan{color:var(--cyan)!important}.c-muted{color:var(--text2)!important}
.c-dim{color:var(--text3)!important}
.c-ce{color:#26a69a;font-weight:700}.c-pe{color:#ef5350;font-weight:700}
.pnl-pos{color:var(--green)}.pnl-neg{color:var(--red)}.pnl-zero{color:var(--text2)}

/* ══════════════════════════════════════════════
   SECTION HEADERS / BADGES / MISC
══════════════════════════════════════════════ */
.section-header{display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap}
.section-title{font-size:9px;font-weight:800;color:var(--text3);text-transform:uppercase;letter-spacing:1.2px}
.count-badge{background:var(--blue-bg);border:1px solid rgba(45,124,246,.3);color:var(--blue-lt);
  font-size:10px;font-weight:700;padding:1px 8px;border-radius:20px;font-family:var(--num-font)}
#eq-wrap{height:120px;position:relative}
#eq-svg{width:100%;height:100%;overflow:visible}
#eq-empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  color:var(--text3);font-size:11px;letter-spacing:.5px}
.reason{display:inline-block;padding:2px 7px;border-radius:var(--rs);font-size:9px;font-weight:700;
  letter-spacing:.3px;background:var(--surface2);color:var(--text3);border:1px solid var(--border);text-transform:uppercase}
select{background:var(--surface2);color:var(--text);border:1px solid var(--border2);
  border-radius:var(--rs);padding:5px 10px;font-size:12px;cursor:pointer;
  font-family:var(--num-font);outline:none;transition:border-color .15s}
select:hover,select:focus{border-color:var(--blue)}
.footer{padding:10px 20px;text-align:right;color:var(--text3);font-size:11px;
  letter-spacing:.3px;border-top:1px solid var(--border);background:var(--surface)}
.empty{padding:24px 10px;color:var(--text3);font-size:11px;text-align:center;letter-spacing:.3px}

</style>
</head>
<body>
<nav class="topbar">
  <span class="topbar-logo">NIFTY ALGO</span>
  <span class="topbar-badge">PAPER</span>
  <span class="topbar-divider"></span>
  <div class="tab-nav">
    <button class="tab-btn active" onclick="showTab('live',this)">Live</button>
    <button class="tab-btn" onclick="showTab('history',this)">History</button>
  </div>
  <span class="topbar-divider"></span>
  <span class="topbar-item" id="t-session">—</span>
  <span class="topbar-divider"></span>
  <span class="topbar-item"><span class="live-dot"></span><span id="t-ws">connecting…</span></span>
  <span class="topbar-divider"></span>
  <span class="topbar-item">uptime&nbsp;<span id="t-uptime">—</span></span>
  <div class="topbar-right">
    <span id="topbar-clock">—</span>
    <button id="btn-inject" class="btn btn-green" onclick="injectTest()">＋ Test CE</button>
    <button class="btn btn-red" onclick="clearTest()">✕ Clear</button>
  </div>
</nav>

<div class="tab-panel active" id="tab-live">
<div class="page">

  <div class="market-strip">
    <div class="mcard mcard-spot">
      <div class="mcard-label">Spot</div>
      <div class="mcard-val num" id="m-spot">—</div>
      <div class="mcard-sub" id="m-spot-sub">vs VWAP —</div>
    </div>
    <div class="mcard">
      <div class="mcard-label">VWAP</div>
      <div class="mcard-val num" id="m-vwap">—</div>
      <div class="mcard-sub" id="m-vwap-sub">—</div>
    </div>
    <div class="mcard">
      <div class="mcard-label">RSI 14</div>
      <div class="mcard-val num" id="m-rsi">—</div>
      <div class="mcard-sub" id="m-rsi-sub">—</div>
    </div>
    <div class="mcard">
      <div class="mcard-label">ATR 14</div>
      <div class="mcard-val num" id="m-atr">—</div>
      <div class="mcard-sub">pts</div>
    </div>
    <div class="mcard" style="min-width:164px">
      <div class="mcard-label">EMA 9 · 21 · 50</div>
      <div class="mcard-val num" style="font-size:13px" id="m-ema">—</div>
      <div class="mcard-sub" id="m-ema-sub">—</div>
    </div>
    <div class="mcard" style="min-width:155px">
      <div class="mcard-label">Bollinger Bands</div>
      <div class="mcard-val num" style="font-size:12px" id="m-bb">—</div>
      <div class="mcard-sub" id="m-bb-sub">—</div>
    </div>
    <div class="mcard">
      <div class="mcard-label">India VIX</div>
      <div class="mcard-val num" id="m-vix">—</div>
      <div class="mcard-sub" id="m-vix-sub">—</div>
    </div>
    <div class="mcard" style="min-width:155px">
      <div class="mcard-label">ORB</div>
      <div class="mcard-val num" style="font-size:13px" id="m-orb">—</div>
      <div class="mcard-sub" id="m-orb-sub">—</div>
    </div>
    <div class="mcard">
      <div class="mcard-label">Vol Ratio</div>
      <div class="mcard-val num" id="m-vol">—</div>
      <div class="mcard-sub">vs 20-bar avg</div>
    </div>
    <div class="mcard">
      <div class="mcard-label">Data Feed</div>
      <div class="mcard-val" id="m-feed">—</div>
      <div class="mcard-sub" id="m-feed-sub">—</div>
    </div>
  </div>

  <div class="main-grid">
    <div class="main-left">
      <div class="card">
        <div class="card-title">Today's P&amp;L</div>
        <div class="pnl-big num" id="g-pnl">—</div>
        <div class="kv-row"><span class="kv-key">Trades</span><span class="kv-val" id="g-trades">—</span></div>
        <div class="kv-row"><span class="kv-key">Wins / Losses</span><span class="kv-val" id="g-wl">—</span></div>
        <div class="kv-row"><span class="kv-key">Win Rate</span><span class="kv-val" id="g-wr">—</span></div>
      </div>
      <div class="card">
        <div class="card-title">Risk</div>
        <div class="risk-row"><span class="risk-label">Trading</span><span class="risk-val" id="r-trading">—</span></div>
        <div class="risk-row"><span class="risk-label">Daily Loss</span><span class="risk-val" id="r-loss">—</span></div>
        <div class="risk-row"><span class="risk-label">Consec. Losses</span><span class="risk-val" id="r-consec">—</span></div>
        <div class="risk-row"><span class="risk-label">Circuit Breaker</span><span class="risk-val" id="r-cb">—</span></div>
        <div class="risk-row"><span class="risk-label">Drawdown</span><span class="risk-val" id="r-dd">—</span></div>
      </div>
      <div class="card">
        <div class="card-title">System</div>
        <div class="risk-row"><span class="risk-label">WebSocket</span><span class="risk-val" id="s-ws">—</span></div>
        <div class="risk-row"><span class="risk-label">Last Tick</span><span class="risk-val" id="s-tick">—</span></div>
        <div class="risk-row"><span class="risk-label">Mode</span><span class="risk-val c-cyan">PAPER</span></div>
        <div class="risk-row"><span class="risk-label">Kill Switch</span><span class="risk-val" id="s-kill">—</span></div>
      </div>
    </div>

    <div style="min-width:0">
      <div class="card" style="height:100%">
        <div class="card-title">Strategies</div>
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>Strategy</th><th>State</th>
              <th style="text-align:right">Trades</th>
              <th style="text-align:right">W / L</th>
              <th style="text-align:right">Win %</th>
              <th style="text-align:right">Net P&amp;L</th>
            </tr></thead>
            <tbody id="strat-body"></tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="main-right">
      <div class="card" style="flex:1">
        <div class="card-title">Equity Curve — Today</div>
        <div id="eq-wrap"><div id="eq-empty">No closed trades yet</div></div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="section-header">
      <span class="section-title">Open Positions</span>
      <span class="count-badge" id="open-count">0</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Strategy</th><th>Symbol</th><th>Side</th>
          <th style="text-align:right">Entry ₹</th>
          <th style="text-align:right">LTP ₹</th>
          <th style="text-align:right">Unreal P&amp;L</th>
          <th style="text-align:right">Chg %</th>
          <th>Time In</th><th>Signal</th><th></th>
        </tr></thead>
        <tbody id="open-body"></tbody>
      </table>
    </div>
  </div>

  <div class="card">
    <div class="section-header">
      <span class="section-title">Closed Trades — Today</span>
      <span class="count-badge" id="closed-count">0</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Time</th><th>Strategy</th><th>Symbol</th><th>Side</th>
          <th style="text-align:right">Entry ₹</th>
          <th style="text-align:right">Exit ₹</th>
          <th style="text-align:right">P&amp;L %</th>
          <th style="text-align:right">Net P&amp;L</th>
          <th style="text-align:right">Hold</th>
          <th>Reason</th>
        </tr></thead>
        <tbody id="closed-body"></tbody>
      </table>
    </div>
  </div>

</div><!-- /page -->
</div><!-- /tab-live -->

<div class="tab-panel" id="tab-history">
<div class="page">

  <!-- All-time summary bar -->
  <div class="market-strip">
    <div class="mcard mcard-spot">
      <div class="mcard-label">Net P&amp;L</div>
      <div class="mcard-val num" id="ha-pnl">—</div>
      <div class="mcard-sub" id="ha-pnl-sub">all time</div>
    </div>
    <div class="mcard">
      <div class="mcard-label">Trades</div>
      <div class="mcard-val num" id="ha-trades">—</div>
      <div class="mcard-sub" id="ha-days">— days</div>
    </div>
    <div class="mcard">
      <div class="mcard-label">Win Rate</div>
      <div class="mcard-val num" id="ha-wr">—</div>
      <div class="mcard-sub" id="ha-wl">—W / —L</div>
    </div>
    <div class="mcard">
      <div class="mcard-label">Avg P&amp;L / Trade</div>
      <div class="mcard-val num" id="ha-avg">—</div>
      <div class="mcard-sub">net</div>
    </div>
  </div>

  <!-- Per-strategy breakdown -->
  <div class="card">
    <div class="card-title">Strategy Breakdown — All Time</div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Strategy</th>
          <th style="text-align:right">Trades</th>
          <th style="text-align:right">W / L</th>
          <th style="text-align:right">Win %</th>
          <th style="text-align:right">Net P&amp;L</th>
        </tr></thead>
        <tbody id="ha-strat-body"></tbody>
      </table>
    </div>
  </div>

  <!-- Daily equity list -->
  <div class="card">
    <div class="card-title">Daily P&amp;L — All Time</div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Date</th>
          <th style="text-align:right">Net P&amp;L</th>
          <th style="min-width:200px">Bar</th>
        </tr></thead>
        <tbody id="ha-daily-body"></tbody>
      </table>
    </div>
  </div>

  <!-- Trade log with filters -->
  <div class="card">
    <div class="section-header" style="flex-wrap:wrap;gap:12px">
      <span class="section-title">Trade Log</span>
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <label style="font-size:10px;color:var(--text3);font-weight:700;letter-spacing:.5px;text-transform:uppercase">Date</label>
        <select id="hist-date"><option value="">All Dates</option></select>
        <label style="font-size:10px;color:var(--text3);font-weight:700;letter-spacing:.5px;text-transform:uppercase">Strategy</label>
        <select id="hist-strategy"><option value="">All</option></select>
        <label style="font-size:10px;color:var(--text3);font-weight:700;letter-spacing:.5px;text-transform:uppercase">Status</label>
        <select id="hist-status">
          <option value="">All</option>
          <option value="CLOSED">Closed</option>
          <option value="OPEN">Open</option>
        </select>
        <div id="hist-summary" style="display:flex;gap:16px;font-size:12px;align-items:center"></div>
      </div>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Date</th><th>Time</th><th>Strategy</th><th>Symbol</th><th>Side</th>
          <th style="text-align:right">Entry ₹</th>
          <th style="text-align:right">Exit ₹</th>
          <th style="text-align:right">Charges</th>
          <th style="text-align:right">P&amp;L %</th>
          <th style="text-align:right">Net P&amp;L</th>
          <th style="text-align:right">Hold</th>
          <th>Reason</th><th>Signal</th>
        </tr></thead>
        <tbody id="hist-body"></tbody>
      </table>
    </div>
  </div>

</div><!-- /page -->
</div><!-- /tab-history -->

<div class="footer">Last updated: <span id="t-last">—</span></div>

<script>
// ── safe helpers (never produce NaN / undefined in DOM) ─────────────────────
const G = id => document.getElementById(id);
const safe = v => (v == null || v !== v) ? 0 : Number(v);
const fmtN = (v, d=2) => safe(v).toLocaleString('en-IN',{minimumFractionDigits:d,maximumFractionDigits:d});
const fmtI = v => safe(v).toLocaleString('en-IN',{maximumFractionDigits:0});
const fmtPnl = v => {
  const n = safe(v);
  const cls = n > 0 ? 'c-green' : n < 0 ? 'c-red' : 'c-muted';
  const sign = n > 0 ? '+' : '';
  return `<span class="num ${cls}">${sign}₹${fmtN(n)}</span>`;
};
const pnlPct = v => {
  const n = safe(v);
  const cls = n > 0 ? 'c-green' : n < 0 ? 'c-red' : 'c-muted';
  const sign = n > 0 ? '+' : '';
  return `<span class="num ${cls}">${sign}${fmtN(n,1)}%</span>`;
};
const set = (id, html, isHTML=false) => {
  const el = G(id);
  if (!el) return;
  if (isHTML) el.innerHTML = html;
  else el.textContent = html;
};

// ── clock ────────────────────────────────────────────────────────────────────
setInterval(() => {
  G('topbar-clock').textContent = new Date().toLocaleTimeString('en-IN',
    {hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
}, 1000);

// ── equity curve ─────────────────────────────────────────────────────────────
function drawEquity(pts) {
  const wrap = G('eq-wrap');
  const empty = G('eq-empty');
  if (!pts || pts.length < 2) {
    if (empty) empty.style.display = 'flex';
    const existing = wrap.querySelector('svg');
    if (existing) existing.remove();
    return;
  }
  if (empty) empty.style.display = 'none';

  const W = 400, H = 110, P = 14;
  const mn = Math.min(...pts), mx = Math.max(...pts);
  const rng = mx - mn || 1;
  const xi = (i) => P + (i / (pts.length - 1)) * (W - 2*P);
  const yi = (v) => P + (1 - (v - mn) / rng) * (H - 2*P);

  const xs = pts.map((_, i) => xi(i));
  const ys = pts.map(v => yi(v));
  const pathD = xs.map((x,i) => `${i===0?'M':'L'}${x.toFixed(1)},${ys[i].toFixed(1)}`).join(' ');
  const areaD = pathD + ` L${xs[xs.length-1].toFixed(1)},${(H-P).toFixed(1)} L${P},${(H-P).toFixed(1)} Z`;

  const last = pts[pts.length-1];
  const col = last >= 0 ? '#22c55e' : '#ef4444';
  const lx = xs[xs.length-1], ly = ys[ys.length-1];

  let existing = wrap.querySelector('svg');
  if (!existing) {
    existing = document.createElementNS('http://www.w3.org/2000/svg','svg');
    existing.setAttribute('id','eq-svg');
    existing.setAttribute('viewBox',`0 0 ${W} ${H}`);
    existing.setAttribute('preserveAspectRatio','none');
    wrap.appendChild(existing);
  }
  existing.innerHTML = `
    <defs>
      <linearGradient id="eg" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="${col}" stop-opacity=".3"/>
        <stop offset="100%" stop-color="${col}" stop-opacity="0"/>
      </linearGradient>
    </defs>
    <path d="${areaD}" fill="url(#eg)"/>
    <path d="${pathD}" fill="none" stroke="${col}" stroke-width="1.8" stroke-linejoin="round"/>
    <circle cx="${lx.toFixed(1)}" cy="${ly.toFixed(1)}" r="3" fill="${col}"/>
    <text x="${(W-P).toFixed(1)}" y="${(P-2).toFixed(1)}"
      fill="${col}" font-size="11" text-anchor="end" font-family="monospace">
      ${last>=0?'+':''}₹${fmtN(last)}
    </text>`;
}

// ── main refresh ─────────────────────────────────────────────────────────────
let _lastError = false;

async function refresh() {
  let d;
  try {
    const r = await fetch('/api/status');
    if (!r.ok) throw new Error(r.status);
    d = await r.json();
    if (_lastError) { G('t-ws').className = 'c-green'; _lastError = false; }
  } catch(e) {
    G('t-ws').textContent = 'fetch error';
    G('t-ws').className = 'c-red';
    _lastError = true;
    return;
  }

  // ── header ──────────────────────────────────────────────────────────────
  set('t-session', d.session || '—');
  set('t-uptime', (d.uptime_min || 0) + 'm');
  set('t-last', new Date(d.ts).toLocaleTimeString('en-IN',{hour12:false}));

  // ── market strip ────────────────────────────────────────────────────────
  const m = d.market || {};
  const spot = safe(m.spot), vwap = safe(m.vwap);

  const spotEl = G('m-spot');
  const vwapDiff = vwap > 0 ? ((spot - vwap) / vwap * 100) : 0;
  spotEl.textContent = fmtI(spot);
  spotEl.className = 'mcard-val num ' + (vwapDiff >= 0 ? 'c-green' : 'c-red');
  set('m-spot-sub', `vs VWAP ${vwapDiff >= 0 ? '+' : ''}${vwapDiff.toFixed(2)}%`);

  set('m-vwap', fmtI(vwap));
  const slope = safe(m.vwap_slope);
  G('m-vwap-sub').innerHTML = `slope <span class="${slope>=0?'c-green':'c-red'}">${slope>=0?'▲':'▼'} ${Math.abs(slope).toFixed(4)}</span>`;

  const rsi = safe(m.rsi) || 50;
  const rsiEl = G('m-rsi');
  rsiEl.textContent = rsi.toFixed(1);
  rsiEl.className = 'mcard-val num ' + (rsi > 70 ? 'c-red' : rsi < 30 ? 'c-green' : '');
  set('m-rsi-sub', rsi > 70 ? '⚠ Overbought' : rsi < 30 ? '⚠ Oversold' : rsi > 55 ? 'Bullish' : rsi < 45 ? 'Bearish' : 'Neutral');

  set('m-atr', fmtN(m.atr, 1));

  const e9=safe(m.ema9), e21=safe(m.ema21), e50=safe(m.ema50);
  if (e9 && e21 && e50) {
    G('m-ema').innerHTML =
      `<span class="${e9>e21?'c-green':'c-red'}">${fmtI(e9)}</span>` +
      `<span class="c-dim"> / </span>${fmtI(e21)}<span class="c-dim"> / </span>${fmtI(e50)}`;
    const trend = e9>e21&&e21>e50?'▲ Bullish':e9<e21&&e21<e50?'▼ Bearish':'↔ Mixed';
    G('m-ema-sub').innerHTML = `<span class="${e9>e21?'c-green':'c-red'}">${trend}</span>`;
  }

  const bbu=safe(m.bb_upper), bbl=safe(m.bb_lower);
  if (bbu && bbl) {
    set('m-bb', `${fmtI(bbl)} — ${fmtI(bbu)}`);
    const outside = spot>bbu||spot<bbl;
    G('m-bb-sub').className = 'mcard-sub ' + (outside?'c-yellow':'c-muted');
    set('m-bb-sub', spot>bbu?'⚠ Above upper':spot<bbl?'⚠ Below lower':'Inside bands');
  }

  const vix = safe(m.vix);
  const vixEl = G('m-vix');
  vixEl.textContent = vix.toFixed(1);
  vixEl.className = 'mcard-val num '+(vix>25?'c-red':vix>18?'c-yellow':'c-green');
  set('m-vix-sub', vix>25?'Extreme':vix>18?'Elevated':'Normal');

  if (m.orb_locked && safe(m.orb_high)) {
    G('m-orb').innerHTML = `<span class="c-green">${fmtI(m.orb_high)}</span><span class="c-dim"> / </span><span class="c-red">${fmtI(m.orb_low)}</span>`;
    G('m-orb-sub').innerHTML = `<span class="c-green">LOCKED</span> · ${fmtN(m.orb_range,0)} pts`;
  } else {
    set('m-orb', 'Building…');
    G('m-orb-sub').innerHTML = '<span class="c-yellow">not locked</span>';
  }

  const vr = safe(m.volume_ratio) || 1;
  const vrEl = G('m-vol');
  vrEl.textContent = vr.toFixed(2) + '×';
  vrEl.className = 'mcard-val num '+(vr>1.5?'c-green':vr<0.5?'c-red':'');

  const fresh = m.is_fresh;
  const feedEl = G('m-feed');
  feedEl.textContent = fresh ? 'LIVE' : 'STALE';
  feedEl.className = 'mcard-val '+(fresh?'c-green':'c-red');
  set('m-feed-sub', `tick ${safe(d.health?.tick_age_sec).toFixed(1)}s ago`);

  // ── WS status ──────────────────────────────────────────────────────────
  const wsOk = d.health?.ws_alive;
  const wsEl = G('t-ws');
  wsEl.textContent = wsOk ? 'WebSocket live' : 'WebSocket DOWN';
  wsEl.className = wsOk ? 'c-green' : 'c-red';

  // ── summary ─────────────────────────────────────────────────────────────
  const s = d.summary || {};
  G('g-pnl').innerHTML = fmtPnl(s.daily_pnl);
  set('g-trades', s.trades ?? 0);
  G('g-wl').innerHTML = `<span class="c-green">${s.wins??0}W</span> <span class="c-dim">/</span> <span class="c-red">${s.losses??0}L</span>`;
  set('g-wr', s.trades ? (s.win_rate ?? 0).toFixed(1) + '%' : '—');

  // ── risk ────────────────────────────────────────────────────────────────
  const risk = d.risk || {};
  const tradingOk = d.health?.trading_ok;
  G('r-trading').innerHTML = tradingOk
    ? '<span class="c-green">✓ ACTIVE</span>'
    : '<span class="c-red">✗ HALTED</span>';

  const dlPct = safe(risk.daily_loss_pct)*100, dlLim = safe(risk.daily_loss_limit_pct)*100;
  G('r-loss').innerHTML = `<span class="${dlPct>dlLim*.8?'c-red':'c-muted'}">${dlPct.toFixed(1)}% / ${dlLim.toFixed(1)}%</span>`;

  const cl = safe(risk.consecutive_losses);
  G('r-consec').innerHTML = `<span class="${cl>=3?'c-red':cl>=1?'c-yellow':'c-green'}">${cl}</span>`;
  G('r-cb').innerHTML = risk.circuit_breaker_tripped
    ? '<span class="c-red">TRIPPED</span>'
    : '<span class="c-green">OK</span>';
  const dd = safe(risk.current_drawdown_pct)*100;
  G('r-dd').innerHTML = `<span class="${dd>10?'c-red':dd>3?'c-yellow':'c-muted'}">${dd.toFixed(1)}%</span>`;

  // ── system ──────────────────────────────────────────────────────────────
  G('s-ws').innerHTML = wsOk
    ? '<span class="c-green">Connected</span>'
    : '<span class="c-red">Disconnected</span>';
  set('s-tick', safe(d.health?.tick_age_sec).toFixed(1) + 's ago');
  G('s-kill').innerHTML = '<span class="c-green">OFF</span>';

  // ── strategy table ──────────────────────────────────────────────────────
  const strats = d.strategies || [];
  G('strat-body').innerHTML = strats.length
    ? strats.map(st => {
        const wr = st.trades ? ((st.wins/st.trades)*100).toFixed(0)+'%' : '—';
        const hasp = st.has_position;
        return `<tr>
          <td><span style="font-weight:600">${st.name}</span>${hasp?' <span class="c-green" title="Active position">●</span>':''}</td>
          <td><span class="badge b-${st.state}">${st.state}</span></td>
          <td class="num" style="text-align:right">${st.trades??0}</td>
          <td style="text-align:right"><span class="c-green">${st.wins??0}W</span> <span class="c-dim">/</span> <span class="c-red">${st.losses??0}L</span></td>
          <td class="num" style="text-align:right">${wr}</td>
          <td style="text-align:right">${fmtPnl(st.pnl)}</td>
        </tr>`;
      }).join('')
    : '<tr><td colspan="6" class="empty">No strategies loaded</td></tr>';

  // ── open positions ───────────────────────────────────────────────────────
  const op = d.open_positions || [];
  set('open-count', op.length);
  G('open-body').innerHTML = op.length
    ? op.map(p => {
        const side = p.option_type === 'CE'
          ? '<span class="c-ce">CE ▲</span>'
          : '<span class="c-pe">PE ▼</span>';
        const upct = safe(p.unreal_pct);
        return `<tr>
          <td class="c-muted">${p.strategy||'—'}</td>
          <td style="font-weight:600">${p.symbol||'—'}</td>
          <td>${side}</td>
          <td class="num" style="text-align:right">₹${fmtN(p.entry_price)}</td>
          <td class="num" style="text-align:right">₹${fmtN(p.ltp)}</td>
          <td style="text-align:right">${fmtPnl(p.unreal_pnl)}</td>
          <td style="text-align:right">${pnlPct(upct)}</td>
          <td class="c-muted">${p.entry_time||'—'}</td>
          <td class="c-dim" style="font-size:11px;max-width:140px;overflow:hidden;text-overflow:ellipsis">${p.signal||'—'}</td>
          <td><button class="btn btn-red" style="padding:3px 8px;font-size:10px"
            onclick="forceExit('${p.strategy}',this)">Force Exit</button></td>
        </tr>`;
      }).join('')
    : '<tr><td colspan="9" class="empty">No open positions</td></tr>';

  // ── equity curve ─────────────────────────────────────────────────────────
  drawEquity(d.equity_curve);

  // ── closed trades ────────────────────────────────────────────────────────
  const ct = d.closed_trades || [];
  set('closed-count', ct.length);
  G('closed-body').innerHTML = ct.length
    ? ct.map(t => {
        const side = t.option_type === 'CE'
          ? '<span class="c-ce">CE</span>' : '<span class="c-pe">PE</span>';
        return `<tr>
          <td class="num c-muted">${t.exit_time||'—'}</td>
          <td>${t.strategy||'—'}</td>
          <td style="font-weight:600;font-size:11px">${t.symbol||'—'}</td>
          <td>${side}</td>
          <td class="num" style="text-align:right">₹${fmtN(t.entry)}</td>
          <td class="num" style="text-align:right">₹${fmtN(t.exit)}</td>
          <td style="text-align:right">${pnlPct(t.pnl_pct)}</td>
          <td style="text-align:right">${fmtPnl(t.net_pnl)}</td>
          <td class="num c-muted" style="text-align:right">${t.hold_min??0}m</td>
          <td><span class="reason">${t.reason||'—'}</span></td>
        </tr>`;
      }).join('')
    : '<tr><td colspan="10" class="empty">No closed trades yet</td></tr>';
}

// ── test trade buttons ────────────────────────────────────────────────────────
async function forceExit(strategy, btn) {
  if (!confirm(`Force-close ${strategy} position at current market price?`)) return;
  btn.textContent = '…'; btn.disabled = true;
  try {
    const r = await fetch(`/api/force-exit/${encodeURIComponent(strategy)}`, {method:'POST'});
    const d = await r.json();
    if (d.ok) {
      btn.textContent = '✓ Done';
      setTimeout(refresh, 500);
    } else {
      btn.textContent = d.reason || 'Error';
      btn.disabled = false;
    }
  } catch(e) { btn.textContent = 'Error'; btn.disabled = false; }
}

async function injectTest() {
  const btn = G('btn-inject');
  const orig = btn.textContent;
  btn.textContent = '…'; btn.disabled = true;
  try {
    const r = await fetch('/api/test-trade', {method:'POST'});
    const d = await r.json();
    btn.textContent = d.injected ? '✓ Injected' : '✗ Failed';
    if (d.injected) refresh();
  } catch(e) { btn.textContent = '✗ Error'; }
  setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 2500);
}

async function clearTest() {
  await fetch('/api/clear-test-trade', {method:'POST'});
  refresh();
}

// ── tab switching ─────────────────────────────────────────────────────────────
let _activeTab = 'live';
function showTab(name, btn) {
  _activeTab = name;
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  const panel = G('tab-' + name);
  if (panel) panel.classList.add('active');
  if (btn) btn.classList.add('active');
  if (name === 'history') loadHistoryAll();
}

// ── all-time history ──────────────────────────────────────────────────────────
let _allTrades = [];
let _allStrategies = [];

async function loadHistoryAll() {
  try {
    const stratFilter = (G('hist-strategy')||{}).value || '';
    const statusFilter = (G('hist-status')||{}).value || '';
    const params = new URLSearchParams();
    if (stratFilter) params.set('strategy', stratFilter);
    if (statusFilter) params.set('status', statusFilter);
    const r = await fetch('/api/history/all?' + params.toString());
    const d = await r.json();

    // Populate strategy dropdown (once)
    if (d.strategies && d.strategies.length && !_allStrategies.length) {
      _allStrategies = d.strategies;
      const sel = G('hist-strategy');
      if (sel) sel.innerHTML = '<option value="">All</option>' +
        d.strategies.map(s => `<option value="${s}">${s}</option>`).join('');
    }

    // Summary bar
    const s = d.summary || {};
    const netPnl = safe(s.net_pnl);
    const pnlCol = netPnl > 0 ? 'c-green' : netPnl < 0 ? 'c-red' : 'c-muted';
    const sign = netPnl >= 0 ? '+' : '';
    const avg = s.total ? netPnl / s.total : 0;
    const avgCol = avg > 0 ? 'c-green' : avg < 0 ? 'c-red' : 'c-muted';
    G('ha-pnl').innerHTML = `<span class="${pnlCol}">${sign}₹${fmtN(netPnl)}</span>`;
    set('ha-pnl-sub', 'all time');
    set('ha-trades', s.total ?? 0);
    set('ha-days', (s.days ?? 0) + ' day' + (s.days !== 1 ? 's' : ''));
    G('ha-wr').innerHTML = `<span class="${s.win_rate>50?'c-green':s.win_rate<40?'c-red':''}">${(s.win_rate??0).toFixed(1)}%</span>`;
    G('ha-wl').innerHTML = `<span class="c-green">${s.wins??0}W</span> / <span class="c-red">${s.losses??0}L</span>`;
    G('ha-avg').innerHTML = `<span class="${avgCol}">${avg>=0?'+':''}₹${fmtN(avg)}</span>`;

    // Per-strategy table
    const ps = d.per_strategy || [];
    G('ha-strat-body').innerHTML = ps.length
      ? ps.map(st => {
          const wr = st.trades ? ((st.wins/st.trades)*100).toFixed(1)+'%' : '—';
          return `<tr>
            <td style="font-weight:600">${st.name}</td>
            <td class="num" style="text-align:right">${st.trades}</td>
            <td style="text-align:right"><span class="c-green">${st.wins}W</span> <span class="c-dim">/</span> <span class="c-red">${st.losses}L</span></td>
            <td class="num" style="text-align:right"><span class="${st.win_rate>50?'c-green':st.win_rate<40?'c-red':'c-muted'}">${wr}</span></td>
            <td style="text-align:right">${fmtPnl(st.net_pnl)}</td>
          </tr>`;
        }).join('')
      : '<tr><td colspan="5" class="empty">No strategy data</td></tr>';

    // Daily P&L table with inline bars
    const de = d.daily_equity || [];
    const maxAbs = de.reduce((m, x) => Math.max(m, Math.abs(x.net_pnl)), 1);
    G('ha-daily-body').innerHTML = de.length
      ? [...de].reverse().map(row => {
          const v = safe(row.net_pnl);
          const col = v > 0 ? 'var(--green)' : v < 0 ? 'var(--red)' : 'var(--text2)';
          const pct = Math.abs(v) / maxAbs * 100;
          const sign2 = v >= 0 ? '+' : '';
          return `<tr>
            <td class="c-muted">${row.date}</td>
            <td class="num" style="text-align:right;color:${col};font-weight:600">${sign2}₹${fmtN(v)}</td>
            <td style="padding:6px 12px">
              <div style="display:flex;align-items:center;gap:6px">
                <div style="width:${pct.toFixed(1)}%;min-width:2px;height:8px;background:${col};border-radius:2px;transition:width .3s"></div>
              </div>
            </td>
          </tr>`;
        }).join('')
      : '<tr><td colspan="3" class="empty">No daily data</td></tr>';

    // Trade log
    _allTrades = d.trades || [];
    renderHistTrades(_allTrades);

  } catch(e) { console.warn('history/all error', e); }
}

function renderHistTrades(trades) {
  // Apply date filter from selector
  const dateSel = G('hist-date');
  const dateFilter = dateSel ? dateSel.value : '';
  const filtered = dateFilter ? trades.filter(t => t.date === dateFilter) : trades;

  // Summary chips
  const closed = filtered.filter(t => t.status === 'CLOSED');
  const wins = closed.filter(t => parseFloat(t.net_pnl||0) > 0).length;
  const netTotal = closed.reduce((a, t) => a + parseFloat(t.net_pnl||0), 0);
  const pnlCol = netTotal > 0 ? 'c-green' : netTotal < 0 ? 'c-red' : 'c-muted';
  G('hist-summary').innerHTML = [
    `<span class="c-muted">${filtered.length} trades</span>`,
    closed.length ? `<span class="c-green">${wins}W</span> <span class="c-dim">/</span> <span class="c-red">${closed.length-wins}L</span>` : '',
    closed.length ? `<span class="c-muted">${(wins/closed.length*100).toFixed(0)}% win</span>` : '',
    `<span class="${pnlCol} num">${netTotal>=0?'+':''}₹${fmtN(netTotal)}</span>`,
  ].filter(Boolean).join('<span class="c-dim" style="margin:0 2px">·</span>');

  G('hist-body').innerHTML = filtered.length
    ? filtered.map(t => {
        const isOpen = t.status === 'OPEN';
        const net = parseFloat(t.net_pnl) || 0;
        const pct = parseFloat(t.pnl_pct) || 0;
        const charges = parseFloat(t.charges) || 0;
        const side = (t.option_type||'') === 'CE'
          ? '<span class="c-ce">CE ▲</span>'
          : '<span class="c-pe">PE ▼</span>';
        const pnlCls = isOpen ? 'c-muted' : net > 0 ? 'c-green' : net < 0 ? 'c-red' : 'c-muted';
        const pctCls = isOpen ? 'c-muted' : pct > 0 ? 'c-green' : pct < 0 ? 'c-red' : 'c-muted';
        const statusBadge = isOpen
          ? '<span class="badge b-ACTIVE_POSITION" style="font-size:8px">OPEN</span>'
          : '';
        const rowStyle = isOpen ? 'background:rgba(26,140,255,.05)' : '';
        return `<tr style="${rowStyle}">
          <td class="c-muted">${t.date||'—'}</td>
          <td class="num c-muted">${t.exit_time||t.entry_time||'—'}</td>
          <td>${t.strategy||'—'}</td>
          <td style="font-weight:600;font-size:11px">${t.symbol||'—'}</td>
          <td>${side}</td>
          <td class="num" style="text-align:right">₹${fmtN(t.entry_price)}</td>
          <td class="num" style="text-align:right">${isOpen?'<span class="c-muted">—</span>':'₹'+fmtN(t.exit_price)}</td>
          <td class="num c-muted" style="text-align:right">${charges>0?'₹'+fmtN(charges):'—'}</td>
          <td class="num ${pctCls}" style="text-align:right">${isOpen?'—':((pct>=0?'+':'')+fmtN(pct,1)+'%')}</td>
          <td class="num ${pnlCls}" style="text-align:right;font-weight:600">${isOpen?'<span class="c-muted">—</span>':(net>=0?'+':'')+'₹'+fmtN(net)}</td>
          <td class="num c-muted" style="text-align:right">${t.hold_min??0}m</td>
          <td>${statusBadge}<span class="reason">${t.exit_reason||'—'}</span></td>
          <td class="c-dim" style="font-size:11px;max-width:130px;overflow:hidden;text-overflow:ellipsis">${t.signal||'—'}</td>
        </tr>`;
      }).join('')
    : '<tr><td colspan="13" class="empty">No trades match the selected filters</td></tr>';
}

async function loadHistoryDates() {
  try {
    const r = await fetch('/api/history/dates');
    const d = await r.json();
    const sel = G('hist-date');
    if (!sel) return;
    sel.innerHTML = '<option value="">All Dates</option>' +
      (d.dates || []).map(dt => `<option value="${dt}">${dt}</option>`).join('');
  } catch(e) { console.warn('history dates error', e); }
}

G('hist-date') && G('hist-date').addEventListener('change', () => {
  if (_allTrades.length) renderHistTrades(_allTrades);
  else loadHistoryAll();
});
G('hist-strategy') && G('hist-strategy').addEventListener('change', loadHistoryAll);
G('hist-status') && G('hist-status').addEventListener('change', loadHistoryAll);

// ── boot ─────────────────────────────────────────────────────────────────────
refresh();
loadHistoryDates();
setInterval(refresh, 2000);
// Refresh all-time history every 60 s when on history tab
setInterval(() => {
  if (_activeTab === 'history') loadHistoryAll();
}, 60000);
</script>
</body>
</html>"""
