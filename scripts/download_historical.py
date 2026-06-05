"""
Download historical NIFTY 1-minute OHLCV data from Angel One SmartAPI.

Downloads as far back as the API allows (typically ~2 years) in 30-day chunks.
Saves two CSVs:
  - data/historical/NIFTY_INDEX_<from>_<to>_1m.csv   (NSE:99926000, perpetual)
  - data/historical/NIFTY_FUT_<from>_<to>_1m.csv     (NFO:62329, current front-month)

Usage:
    python scripts/download_historical.py
    python scripts/download_historical.py --years 2        # default
    python scripts/download_historical.py --years 1 --futures-token 62329
"""

from __future__ import annotations

import asyncio
import csv
import logging
import sys
import time
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import List, Optional

import yaml

# Make sure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.auth.angel_auth import AngelAuthManager
from core.broker.angel_connector import AngelConnector
from core.broker.rate_limiter import RateLimiter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

CHUNK_DAYS = 30          # Angel One max per request for ONE_MINUTE
RATE_LIMIT_DELAY = 0.5   # seconds between API calls to avoid throttling

# Market hours (IST)
MARKET_OPEN  = (9, 15)
MARKET_CLOSE = (15, 30)


def load_credentials() -> dict:
    cfg_path = Path(__file__).parent.parent / "config" / "credentials.yaml"
    with open(cfg_path) as f:
        return yaml.safe_load(f)["angel_one"]


def date_range_chunks(start: date, end: date, chunk_days: int):
    """Yield (chunk_start, chunk_end) pairs as 'YYYY-MM-DD HH:MM' strings."""
    cur = start
    while cur < end:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), end)
        yield (
            f"{cur.strftime('%Y-%m-%d')} {MARKET_OPEN[0]:02d}:{MARKET_OPEN[1]:02d}",
            f"{chunk_end.strftime('%Y-%m-%d')} {MARKET_CLOSE[0]:02d}:{MARKET_CLOSE[1]:02d}",
        )
        cur = chunk_end + timedelta(days=1)


def parse_candles(raw: List) -> List[dict]:
    """Convert Angel One candle list to dicts with ISO timestamp."""
    rows = []
    for item in raw:
        # item = [timestamp_str, open, high, low, close, volume]
        if len(item) < 6:
            continue
        ts_raw = item[0]
        # Angel returns "2024-01-01T09:15:00+05:30" — strip timezone
        ts = ts_raw[:19]  # "2024-01-01T09:15:00"
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            continue
        # Filter to market hours only
        t = (dt.hour, dt.minute)
        if t < MARKET_OPEN or t >= MARKET_CLOSE:
            continue
        rows.append({
            "timestamp": ts,
            "open":   float(item[1]),
            "high":   float(item[2]),
            "low":    float(item[3]),
            "close":  float(item[4]),
            "volume": int(item[5]),
        })
    return rows


async def download_instrument(
    connector: AngelConnector,
    token: str,
    exchange: str,
    symbol: str,
    start: date,
    end: date,
    out_path: Path,
) -> int:
    """Download all 1-min bars for one instrument. Returns total bar count."""
    all_rows: List[dict] = []
    chunks = list(date_range_chunks(start, end, CHUNK_DAYS))
    logger.info(f"[{symbol}] Fetching {len(chunks)} chunk(s) from {start} to {end}")

    for i, (from_dt, to_dt) in enumerate(chunks, 1):
        logger.info(f"  [{symbol}] Chunk {i}/{len(chunks)}: {from_dt} → {to_dt}")
        try:
            raw = await connector.get_historical_data(
                token=token,
                exchange=exchange,
                symbol=symbol,
                interval="ONE_MINUTE",
                from_date=from_dt,
                to_date=to_dt,
            )
        except Exception as e:
            logger.warning(f"  [{symbol}] Chunk {i} failed: {e} — skipping")
            raw = []

        if raw:
            parsed = parse_candles(raw)
            all_rows.extend(parsed)
            logger.info(f"  [{symbol}] Got {len(parsed)} bars")
        else:
            logger.warning(f"  [{symbol}] Empty response for chunk {i}")

        if i < len(chunks):
            await asyncio.sleep(RATE_LIMIT_DELAY)

    if not all_rows:
        logger.warning(f"[{symbol}] No data downloaded")
        return 0

    # Deduplicate and sort
    seen = set()
    unique = []
    for row in all_rows:
        if row["timestamp"] not in seen:
            seen.add(row["timestamp"])
            unique.append(row)
    unique.sort(key=lambda r: r["timestamp"])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        writer.writerows(unique)

    logger.info(f"[{symbol}] Saved {len(unique)} bars → {out_path}")
    return len(unique)


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Download NIFTY historical 1-min data")
    parser.add_argument("--years", type=float, default=2.0, help="Years of history to fetch (default: 2)")
    parser.add_argument("--futures-token", default="62329", help="NIFTY Futures token (default: 62329)")
    parser.add_argument("--no-futures", action="store_true", help="Skip futures download")
    parser.add_argument("--no-index", action="store_true", help="Skip index download")
    args = parser.parse_args()

    end_date   = date.today()
    start_date = end_date - timedelta(days=int(args.years * 365))

    creds = load_credentials()
    auth  = AngelAuthManager(creds)
    await auth.initialize()
    connector = AngelConnector(auth, RateLimiter(requests_per_second=2.0))

    data_dir = Path(__file__).parent.parent / "data" / "historical"
    date_tag = f"{start_date.strftime('%Y-%m-%d')}_{end_date.strftime('%Y-%m-%d')}"

    try:
        if not args.no_index:
            out = data_dir / f"NIFTY_INDEX_{date_tag}_1m.csv"
            total = await download_instrument(
                connector,
                token="99926000",
                exchange="NSE",
                symbol="Nifty 50",
                start=start_date,
                end=end_date,
                out_path=out,
            )
            logger.info(f"Index download complete: {total} bars")

        if not args.no_futures:
            out = data_dir / f"NIFTY_FUT_{date_tag}_1m.csv"
            total = await download_instrument(
                connector,
                token=args.futures_token,
                exchange="NFO",
                symbol="NIFTY-FUT",
                start=start_date,
                end=end_date,
                out_path=out,
            )
            logger.info(f"Futures download complete: {total} bars")

    finally:
        await auth.shutdown()

    # Print run command
    date_tag_short = f"{start_date.strftime('%Y-%m-%d')}_{end_date.strftime('%Y-%m-%d')}"
    print("\n--- Run backtest with this data ---")
    print(f"python backtesting/multi_strategy_backtest.py \\")
    print(f"  --futures data/historical/NIFTY_FUT_{date_tag_short}_1m.csv \\")
    print(f"  --index   data/historical/NIFTY_INDEX_{date_tag_short}_1m.csv \\")
    print(f"  --capital 500000 \\")
    print(f"  --output  data/backtests/multi_strategy_{date_tag_short}.json")


if __name__ == "__main__":
    asyncio.run(main())
