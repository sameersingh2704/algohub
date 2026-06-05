# AVCS Backtesting

Run a 1-minute SmartAPI-backed backtest:

```bash
.venv-py311/bin/python backtesting/run_backtest.py \
  --from 2026-05-25 \
  --to 2026-05-27 \
  --output data/backtests/avcs_backtest_2026-05-25_2026-05-27.csv
```

Add `--refresh-cache` to force a fresh SmartAPI download. Candles are cached in
`data/historical/`.

## Data Rules

- NIFTY futures OHLCV drives the entire underlying side of the backtest:
  price, ORB, EMA, breakout, VWAP, CPR/gap regime, and volume confirmation.
- NIFTY index candles are not used because the index has no real traded volume.
- Option entries/exits use historical option 1-minute candles for the selected
  strike.
- SmartAPI historical candles do not include historical OI or full order-book
  depth. The runner does not use current OI as a substitute because that would
  introduce forward bias. It uses option volume-to-date for liquidity instead.

## Bias Rules

- Signals are evaluated only after the signal bar has closed.
- Entries are filled on the next option candle open.
- Exits are checked only on bars after entry.
- If stop and target both occur in the same option bar, the stop is assumed
  first.
- Previous-day CPR uses only the previous daily candle.

## Current Limitation

The Angel instrument master only contains currently listed contracts. For older
expired options/futures, import the old token-level 1-minute CSVs into
`data/historical/` using the same cache filename format, or run tests on dates
whose contracts are still in the current master.
