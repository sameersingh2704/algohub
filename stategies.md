# Trade & Code — Strategy Playbook

> Personal strategy notes. For educational purposes only. Not financial advice.
> All strategies trade NIFTY Weekly Options (CE/PE) on NSE.
> Lot Size: 75 · Mode: PAPER

---

## Strategy Index

| # | Strategy | Category | Type | Direction | Status |
|---|----------|----------|------|-----------|--------|
| 1 | Iron Fly | Options Selling | Non-Directional | Both | ✅ Live |
| 2 | AVCS v2 Scalp | Framework | ORB + VWAP | Both | ✅ Live |
| 3 | Liquidity Sweep Reversal | A — Liquidity | Stop Hunt | CE / PE | ✅ Active |
| 4 | False Breakout Trap | A — Liquidity | Reversal | CE / PE | ✅ Active |
| 5 | BOS Retest | C — Market Structure | Continuation | CE / PE | ✅ Active |
| 6 | Fair Value Gap | C — Market Structure | Gap Fill | CE / PE | ✅ Active |
| 7 | PDH / PDL Retest | C — Market Structure | Continuation | CE / PE | ✅ Active |
| 8 | EMA Trend Ride | D — Trend | Pullback | CE / PE | ✅ Active |
| 9 | Daily Momentum Drive | D — Trend | Momentum | CE / PE | ✅ Active |
| 10 | CPR Breakout | D — Trend | Breakout | CE / PE | ✅ Active |
| 11 | VWAP Mean Reversion | E — Mean Reversion | Fade | CE / PE | ✅ Active |
| 12 | Bollinger Reversion | E — Mean Reversion | Fade | CE / PE | ✅ Active |
| 13 | Gap Fill Fade | E — Mean Reversion | Fade | CE / PE | ✅ Active |
| 14 | Morning Reversal | E — Mean Reversion | Exhaustion | CE / PE | ✅ Active |
| 15 | Gamma Pinning | F — Options | Expiry | CE / PE | ✅ Tuesdays |
| 16 | OI Wall Fade | F — Options | Rejection | CE / PE | ✅ Active |
| 17 | Max Pain Convergence | F — Options | Convergence | CE / PE | ✅ Active |
| 18 | VIX Spike Buy | F — Options | Momentum | PE | ✅ Active |

---

## Common Exit Rules (All Strategies Unless Overridden)

| Exit Type | Trigger |
|-----------|---------|
| Hard Stop | -30% on option premium |
| Profit Target | +45% on option premium |
| Trailing Activation | +15% profit → trail begins |
| Trailing Stop | 12% below peak |
| Breakeven Lock | At +15% → move stop to entry +2% |
| Partial Exit | Sell 50% of position at +20% |
| Time Stop | 15:15 IST mandatory exit |

---

## 1. Iron Fly

**Category:** Options Selling · **Direction:** Non-Directional

### Overview
Sell ATM CE + ATM PE, buy OTM protection on both sides. Profits when NIFTY stays range-bound near ATM strike. Max profit if NIFTY closes exactly at ATM on expiry.

**Best condition:** India VIX < 15, sideways market

### Legs

| Leg | Action | Strike | Type |
|-----|--------|--------|------|
| 1 | SELL | ATM | CE |
| 2 | SELL | ATM | PE |
| 3 | BUY | ATM + 100 | CE |
| 4 | BUY | ATM - 100 | PE |

### Entry
- Time: 9:20 AM IST
- VIX < 15
- NIFTY within 0.3% of ATM strike
- No major events / RBI announcements
- Strike: nearest 50-pt interval

### Exit
- **Target:** 40–50% of premium collected
- **Stop Loss:** Premium doubles (2×) OR NIFTY moves >1% from ATM
- **Time:** Exit by 3:00 PM, never carry to expiry

### Notes
- Best on Tuesday / Wednesday entries
- Skip Monday (weekend gap risk) and Thursday (expiry gamma risk)
- Capital: ~₹1.5L per lot

---

## 2. AVCS v2 Scalp (Main Framework)

**Category:** ORB + VWAP Confluence · **Direction:** Both

### Overview
Adaptive ORB-VWAP Confluence Scalper. The master framework that orchestrates all sub-strategies. Uses Opening Range Breakout as the primary signal combined with VWAP, EMA stack, RSI, and volume filters.

### Entry Windows
| Window | Time | Description |
|--------|------|-------------|
| Morning Post-ORB | 09:35–10:45 | Primary ORB breakout trades |
| Mid-Morning | 11:00–11:30 | Secondary momentum entries |
| Afternoon | 13:00–14:45 | Late-day continuation |
| Chop Window | 11:31–12:59 | No new entries — midday noise zone |

### Entry
- ORB range established between 9:25–9:35 AM
- ORB range must be 15–100 pts (skip day if outside)
- Breakout: close above ORB High (+3 pt buffer) → CE
- Breakout: close below ORB Low (-3 pt buffer) → PE
- Volume ≥ 1.2× 20-bar average
- RSI ≥ 45 for CE, RSI ≤ 55 for PE
- Delta: 0.30–0.65 (slightly OTM preferred for scalp)

### Exit (Scalp Profile)
- Hard Stop: -30% on premium
- Target: +45% on premium
- Trailing: activates at +15%, trails 12% below peak
- Breakeven: lock +2% after +15% profit
- Partial: sell 50% at +20%
- Time Stop: 15:15 IST

### Gamma Mode (Tuesdays)
- Activates after 10:30 AM
- Target: +55% · Stop: -35% · Trail: activates at +15%
- Mandatory exit at 15:00 (not 15:15)

---

## 3. Liquidity Sweep Reversal

**Category:** A — Liquidity · **Direction:** CE (after EQL sweep) / PE (after EQH sweep)

### Overview
Detects institutional stop hunts. When price wicks through a cluster of equal lows (EQL) or equal highs (EQH) and snaps back inside, it signals a liquidity grab reversal.

### Entry
- **Setup:** Identify ≥2 equal lows (EQL) or equal highs (EQH) within 0.1% tolerance over last 20 bars
- **Armed:** Price approaches within 0.2% of the identified level
- **Trigger:** Candle wicks THROUGH the level but CLOSES back inside + volume ≥ 1.5× average
- EQL sweep → buy CE (bullish reversal)
- EQH sweep → buy PE (bearish reversal)
- VIX must be < 25

### Exit
- Hard Stop: -30% · Target: +45%
- Trailing: activates at +15%, 12% trail
- Stop placed 0.5× ATR beyond the wick
- Target: 2:1 R:R from entry
- Max 2 trades/day · Cooldown: 5 bars after any trade

---

## 4. False Breakout Trap

**Category:** A — Liquidity · **Direction:** PE (failed up breakout) / CE (failed down breakout)

### Overview
Fades failed breakouts. When price "breaks" a swing level but immediately reverses back inside with weak volume, the breakout was a trap — trade the return.

### Entry
- **Setup:** Identify prior swing high and swing low over 20 bars
- **Armed:** Price closes beyond swing high/low by ≥ 0.1%
- **Trigger:** Within 3 bars, price closes BACK inside the level + volume < 0.8× average (weak breakout)
- Failed up breakout → buy PE
- Failed down breakout → buy CE
- VIX < 25

### Exit
- Hard Stop: -30% · Target: +45%
- Stop: 0.5× ATR beyond the breakout level
- Target: back to opposite swing extreme
- Max 2 trades/day · Cooldown: 5 bars

---

## 5. BOS Retest (Break of Structure)

**Category:** C — Market Structure · **Direction:** CE (bullish BOS) / PE (bearish BOS)

### Overview
After a clean structural break (close beyond prior swing), waits for price to pull back and retest the broken level. Enter on rejection — the level now acts as new support/resistance.

### Entry
- **Setup:** Swing high/low identified over 20 bars
- **Armed:** Price closes beyond swing level by ≥ 0.15% (confirmed BOS)
- **Trigger:** Price retraces back into 0.2% zone of broken level + rejection candle (wick ≥ 50% of bar range) + RSI between 30–70
- Bullish BOS → buy CE on retest
- Bearish BOS → buy PE on retest

### Exit
- Hard Stop: -30% · Target: +45%
- Stop: 0.5× ATR beyond retest level
- Minimum R:R of 2:1
- Max 2 trades/day · Cooldown: 5 bars

---

## 6. Fair Value Gap (FVG)

**Category:** C — Market Structure · **Direction:** CE (bullish FVG) / PE (bearish FVG)

### Overview
A 3-bar price imbalance. When Bar[-2].high < Bar[0].low, a bullish gap exists. Price tends to return and fill this gap before continuing. Entry when price retraces into the gap zone.

### Entry
- **Setup:** Detect 3-bar gap: Bar[-2].high < Bar[0].low (bullish) or Bar[-2].low > Bar[0].high (bearish)
- **Armed:** Price starts retracing toward the FVG zone
- **Trigger:** Price enters the gap zone — buy CE (bullish FVG) or PE (bearish FVG)
- FVG must be filled within 10 bars of formation

### Exit
- Standard exit rules
- Max 2 trades/day · Cooldown: 5 bars

---

## 7. PDH / PDL Retest

**Category:** C — Market Structure · **Direction:** CE (PDH breakout retest) / PE (PDL breakdown retest)

### Overview
Previous Day High (PDH) and Previous Day Low (PDL) are key structural levels. After a confirmed breakout above PDH or below PDL, the first pullback back to that level offers a high-probability continuation entry.

### Entry
- **Context needed:** prev_high (PDH), prev_low (PDL) — set before market open
- **Setup:** Today's session has a confirmed close above PDH (for CE) or below PDL (for PE)
- **Armed:** Price retracing back toward PDH/PDL within 25 pts
- **Trigger:** Rejection candle at PDH/PDL + volume ≥ 1.2× avg + RSI not extreme (42–58)
- Entry window: 9:35–14:00

### Exit
- Hard Stop: -30% · Target: +70%
- Trailing: activates at +20%, 15% trail
- Partial: 50% at +30%
- Time stop: 14:30
- Max 2 trades/day · Cooldown: 8 bars

---

## 8. EMA Trend Ride

**Category:** D — Trend · **Direction:** CE (uptrend pullback) / PE (downtrend pullback)

### Overview
Trend-following using EMA5/EMA13/EMA21 alignment. Enters on pullbacks to EMA21 in the direction of the primary trend.

### Entry
- **Setup:** All 3 EMAs aligned (EMA5 > EMA13 > EMA21 for bull trend; reverse for bear)
- **Armed:** Price within 0.3% of EMA21
- **Trigger:** Bullish/bearish close off EMA21 + RSI between 40–65 (bull) or 35–60 (bear)
- CE on EMA21 pullback in uptrend
- PE on EMA21 pullback in downtrend

### Exit
- Hard Stop: -30% · Target: +45%
- Stop: 0.3× ATR from entry
- Max 2 trades/day · Cooldown: 5 bars

---

## 9. Daily Momentum Drive

**Category:** D — Trend · **Direction:** Both

### Overview
3-phase momentum system designed to fire at least one trade every session. Phases cascade — if Phase 1 fires, Phases 2 and 3 are skipped.

### Phase 1 — ORB Breakout (9:35–10:30)
- ORB established 9:15–9:35, range 15–150 pts
- Close above ORB High + 8 pt buffer + volume ≥ 1.2× → CE
- Close below ORB Low - 8 pt buffer + volume ≥ 1.2× → PE
- RSI: 42–74 for CE, 26–58 for PE

### Phase 2 — VWAP Momentum (10:30–12:30, only if no trade yet)
- Price on same side of VWAP for ≥ 3 consecutive bars
- EMA9/21 stack agrees with direction
- Pullback then continuation bar triggers entry
- RSI confirms directional bias

### Phase 3 — Midday Momentum (12:30–14:00, only if no trade yet)
- Minimal conditions: EMA9/21 alignment + price on correct VWAP side + RSI bias
- Bullish/bearish candle close → entry
- Fires on quiet/ranging days to ensure data collection

### Exit
- Hard Stop: -35% · Target: +70%
- Trailing: activates at +20%, 18% trail
- Partial: 50% at +30%
- Stop: 0.8× ATR from entry · R:R target 2:1
- Max 1 trade/day · Cooldown: 10 bars

---

## 10. CPR Breakout

**Category:** D — Trend · **Direction:** CE (break above TC) / PE (break below BC)

### Overview
On narrow-CPR days, the market tends to trend once it escapes the Central Pivot Range. Entry on confirmed break of TC (Top Central Pivot) or BC (Bottom Central Pivot).

### Entry
- **Context needed:** cpr_tc (Top Central Pivot), cpr_bc (Bottom Central Pivot) — set before open
- **Condition:** CPR width (TC - BC) < 40 pts (narrow day = trending potential)
- **Armed:** Price within 15 pts of TC (approaching from below) or BC (from above)
- **Trigger:** Close beyond TC/BC + volume ≥ 1.3× avg + RSI ≥ 55 (CE) or ≤ 45 (PE)
- Entry window: 9:35–13:00 · VIX < 22

### Exit
- Hard Stop: -30% · Target: +70%
- Trailing: activates at +20%, 15% trail
- Partial: 50% at +30%
- Time stop: 14:30
- Max 1 trade/day · Cooldown: 8 bars

---

## 11. VWAP Mean Reversion

**Category:** E — Mean Reversion · **Direction:** PE (extended above) / CE (extended below)

### Overview
Fades overextensions from VWAP. When price extends ≥ 0.8% from VWAP for 3+ bars and RSI reaches extreme, the mean-reversion back to VWAP is imminent.

### Entry
- **Setup:** Price extended ≥ 0.8% from VWAP for ≥ 3 consecutive bars
- **Trigger:** RSI ≤ 35 (oversold, extended below → CE) or RSI ≥ 65 (overbought, extended above → PE)
- PE: price ≥ 0.8% above VWAP + RSI ≥ 65
- CE: price ≥ 0.8% below VWAP + RSI ≤ 35
- Stop: 0.25% beyond the extreme point

### Exit
- Hard Stop: -30% · Target: +45%
- Trailing: activates at +15%, 12% trail
- Max 2 trades/day · Cooldown: 5 bars

---

## 12. Bollinger Reversion

**Category:** E — Mean Reversion · **Direction:** PE (outside upper band) / CE (outside lower band)

### Overview
Fades price when it closes outside the Bollinger Band (20, 2σ). Entry on the first bar that closes back inside — confirms overextension rather than a band walk.

### Entry
- **Setup:** Price closes outside upper/lower Bollinger Band (20-period, 2σ)
- **Trigger:** NEXT bar closes back inside the band (reversal confirmation)
- Outside upper BB → PE on first close back inside
- Outside lower BB → CE on first close back inside
- RSI overbought > 70 (for PE), oversold < 30 (for CE)

### Exit
- Hard Stop: -30% · Target: +45%
- Stop: 0.5× ATR
- Max 2 trades/day · Cooldown: 5 bars

---

## 13. Gap Fill Fade

**Category:** E — Mean Reversion · **Direction:** PE (gap-up fill) / CE (gap-down fill)

### Overview
On days with a meaningful gap open (50–200 pts), the first impulse often fades back toward the previous close. Trades the fill in the first 45 minutes.

### Entry
- **Context needed:** gap_pts, prev_close, session_open — set before open
- **Condition:** Gap size 50–200 pts (too small = no edge; too large = panic move, skip)
- **Setup:** Time 9:15–10:00 AM
- **Armed:** After 2 bars, RSI showing early momentum loss in gap direction
- **Trigger:** Directional reversal bar with spot starting to move back toward prev_close
- Gap-up → buy PE (fade the gap up, expect fill)
- Gap-down → buy CE (fade the gap down, expect fill)
- VIX < 22

### Exit
- Hard Stop: -30% · Target: +65%
- Trailing: activates at +20%, 15% trail
- Partial: 50% at +30%
- **Time stop: 11:30** (gap fills fast or doesn't fill at all)
- Max 1 trade/day · Cooldown: 10 bars

---

## 14. Morning Reversal

**Category:** E — Mean Reversion · **Direction:** PE (morning spike exhaustion) / CE (morning dump exhaustion)

### Overview
After a sharp morning move ≥ 80 pts from session open, RSI reaches extreme and volume starts fading. The first reversal candle = exhaustion signal.

### Entry
- **Context needed:** session_open — the 9:15 candle open
- **Condition:** |spot - session_open| ≥ 80 pts
- **Window:** 10:30–12:00 AM · VIX < 22
- **Armed:** RSI > 65 (bearish) or RSI < 35 (bullish) + VWAP distance ≥ 40 pts + volume declining last 2 bars
- **Trigger:** Reversal candle — upper wick > body (for PE) or lower wick > body (for CE)

### Exit
- Hard Stop: -30% · Target: +65%
- Trailing: activates at +20%, 15% trail
- Stop: 0.6× ATR · R:R target: 2:1
- **Time stop: 13:00**
- Max 1 trade/day · Cooldown: 10 bars

---

## 15. Gamma Pinning

**Category:** F — Options · **Direction:** PE (spot above max pain) / CE (spot below max pain) · **Tuesdays Only**

### Overview
On NIFTY weekly expiry (Tuesday), market makers are forced to delta-hedge as expiry approaches — this mechanically drives the spot toward max pain. Trade the convergence.

### Entry
- **Active:** Tuesdays only, after 10:30 AM
- **Condition:** Spot displaced ≥ 0.5% from max pain for ≥ 3 bars
- **Trigger:** EMA slope flat (< 0.001 threshold, weak trend) + PCR neutral (0.85–1.15) + first bar moving toward max pain
- Spot > max pain → buy PE
- Spot < max pain → buy CE

### Exit
- Hard Stop: -35% · Target: +55%
- Trailing: activates at +15%, 15% trail
- Partial: 50% at +20%
- **Mandatory exit: 15:00** (never hold through final 15 mins)
- Max 1 trade/expiry · Cooldown: 10 bars

---

## 16. OI Wall Fade

**Category:** F — Options · **Direction:** PE (at call wall) / CE (at put wall)

### Overview
Heavy OI at a strike creates a magnetic ceiling (call wall) or floor (put wall). Market makers delta-hedge by selling the underlying as the call wall is tested — fading this rejection gives high edge.

### Entry
- **Condition:** VIX < 22, time 9:30–14:30, option chain available
- **Armed:** Spot within 50 pts of highest CE OI strike (→ look for PE) or highest PE OI strike (→ look for CE)
- **Trigger:** Rejection bar — upper/lower wick ≥ 1.5× body size at the wall level
- At call wall → buy PE
- At put wall → buy CE
- Volume ≥ 1.0× avg

### Exit
- Hard Stop: -30% · Target: +60%
- Trailing: activates at +20%, 15% trail
- Partial: 50% at +25%
- Stop: 0.3× ATR beyond the wall · Target: 1.5× ATR
- Max 2 trades/day · Cooldown: 8 bars

---

## 17. Max Pain Convergence

**Category:** F — Options · **Direction:** CE (spot below max pain) / PE (spot above max pain)

### Overview
Max pain is the strike where most options expire worthless. On expiry week, the spot drifts toward max pain as market makers manage delta. Trade the convergence direction.

### Entry
- **Condition:** |spot - max_pain| ≥ 100 pts, time 9:30–11:30, option chain available
- **Armed:** PCR < 1.0 if spot above max pain (confirms PE bias), PCR > 1.0 if below (CE bias)
- **Trigger:** First bar moving toward max pain + volume ≥ 1.2× avg + RSI ≥ 45 (CE) or ≤ 55 (PE)
- Spot > max pain → buy PE
- Spot < max pain → buy CE
- VIX < 22

### Exit
- Hard Stop: -30% · Target: +60%
- Trailing: activates at +20%, 15% trail
- Partial: 50% at +25%
- **Time stop: 14:00**
- Max 1 trade/day · Cooldown: 10 bars

---

## 18. VIX Spike Buy

**Category:** F — Options · **Direction:** PE only (panic buying)

### Overview
A sudden India VIX spike signals market fear. When VIX jumps ≥ 15% from previous close AND NIFTY drops, option premium expands with the move — buying PE gives asymmetric upside.

### Entry
- **Context needed:** prev_vix — previous session's India VIX close
- **Condition:** india_vix > prev_vix × 1.15 AND india_vix > 15 (absolute floor)
- **Condition:** NIFTY ≥ 40 pts below session high
- **Window:** 9:30–13:00 · Time in market: wait 2–3 bars for initial spike to settle
- **Trigger:** New session low formed + RSI < 45 + volume spike → buy PE

### Exit
- Hard Stop: -30% · **Target: +80%** (VIX spikes = massive premium expansion)
- Trailing: activates at +25%, 20% trail
- Partial: 50% at +35%
- **Time stop: 14:00**
- Max 1 trade/day · Cooldown: 12 bars

---

## Risk Management (Global Rules)

| Rule | Value |
|------|-------|
| Max trades per day (all strategies) | 30 |
| Consecutive loss halt | Disabled in PAPER mode |
| VIX normal ceiling | 20 |
| VIX reduced-size ceiling | 25 |
| VIX skip-all ceiling | >25 |
| Skip condition | Major events (RBI, Budget, expiry news) |
| Lot size | 75 |
| Mode | PAPER — do not switch to LIVE without full validation |

---

## Backtest Results

Backtest data available in: `algohub/data/backtests/`

- `avcs_backtest_trades.summary.json` — AVCS single strategy results
- `multi_strategy_2024-05-27_2026-05-27.json` — 2-year multi-strategy backtest
- Run: `cd algohub && python backtesting/run_backtest.py`

---

*For educational purposes only · Not financial advice*
*Last updated: Day 1 stream — June 9, 2026*