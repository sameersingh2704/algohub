# PHASE 2: System Architecture, Folder Structure & Database Schema
## NIFTY50 Options Scalping System

---

## SECTION 1: SYSTEM ARCHITECTURE OVERVIEW

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         NIFTY ALGO TRADING SYSTEM                           │
│                                                                             │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────────────────┐  │
│  │  Config      │    │  Auth        │    │  Health Monitor              │  │
│  │  Manager     │───▶│  Manager     │    │  (Heartbeat, API, Data)      │  │
│  └──────────────┘    └──────┬───────┘    └──────────────────────────────┘  │
│                             │                                               │
│                    ┌────────▼────────┐                                      │
│                    │  Angel One      │                                      │
│                    │  Broker         │                                      │
│                    │  Connector      │                                      │
│                    └────────┬────────┘                                      │
│                             │                                               │
│              ┌──────────────┴──────────────┐                               │
│              ▼                             ▼                               │
│  ┌────────────────────┐     ┌──────────────────────┐                       │
│  │  REST API Client   │     │  WebSocket Client     │                       │
│  │  (Order/Chain/Hist)│     │  (Live Ticks/LTP)     │                       │
│  └────────┬───────────┘     └──────────┬───────────┘                       │
│           │                            │                                    │
│           └──────────┬─────────────────┘                                    │
│                      ▼                                                      │
│          ┌──────────────────────────┐                                       │
│          │   Market Data Engine      │                                       │
│          │  - Tick normalizer        │                                       │
│          │  - OHLCV builder          │                                       │
│          │  - VWAP calculator        │                                       │
│          │  - Option chain parser    │                                       │
│          │  - IV surface             │                                       │
│          │  - Greeks calculator      │                                       │
│          │  - OI tracker             │                                       │
│          └──────────┬───────────────┘                                       │
│                     │                                                       │
│          ┌──────────▼───────────────┐                                       │
│          │   Signal Engine           │                                       │
│          │  - Regime classifier      │                                       │
│          │  - ORB calculator         │                                       │
│          │  - VWAP analyzer          │                                       │
│          │  - EMA stack              │                                       │
│          │  - Volume analyzer        │                                       │
│          │  - Signal composer        │                                       │
│          └──────────┬───────────────┘                                       │
│                     │                                                       │
│          ┌──────────▼───────────────┐                                       │
│          │   Strategy Engine         │                                       │
│          │  - AVCS Strategy          │                                       │
│          │  - Gamma Mode (Tuesday)   │                                       │
│          │  - Strike selector        │                                       │
│          │  - Position sizer         │                                       │
│          └──────────┬───────────────┘                                       │
│                     │                                                       │
│          ┌──────────▼───────────────┐                                       │
│          │   Risk Engine             │                                       │
│          │  - Pre-trade check        │                                       │
│          │  - Daily limits           │                                       │
│          │  - Circuit breaker        │                                       │
│          │  - Kill switch            │                                       │
│          └──────────┬───────────────┘                                       │
│                     │  (approved signals only)                              │
│          ┌──────────▼───────────────┐                                       │
│          │   Execution Engine        │                                       │
│          │  ┌───────────┐  ┌──────┐ │                                       │
│          │  │  Paper    │  │Live  │ │  ← Paper mode active in Phase 1-3     │
│          │  │Simulator  │  │Engine│ │                                       │
│          │  └───────────┘  └──────┘ │                                       │
│          └──────────┬───────────────┘                                       │
│                     │                                                       │
│       ┌─────────────┼─────────────────┐                                    │
│       ▼             ▼                 ▼                                    │
│  ┌─────────┐  ┌──────────┐  ┌──────────────┐                              │
│  │ Position│  │ Expense  │  │   Journal    │                              │
│  │ Manager │  │ Engine   │  │   Engine     │                              │
│  └────┬────┘  └────┬─────┘  └──────┬───────┘                              │
│       │            │               │                                        │
│       └─────────┬──┴───────────────┘                                        │
│                 ▼                                                           │
│         ┌──────────────┐      ┌──────────────┐      ┌──────────────┐       │
│         │   Database   │      │  Analytics   │      │  Alert       │       │
│         │   Manager    │      │  Engine      │      │  Engine      │       │
│         │  (SQLite/PG) │      │  (Metrics)   │      │  (Telegram)  │       │
│         └──────────────┘      └──────────────┘      └──────────────┘       │
│                                      │                                     │
│                               ┌──────▼───────┐                             │
│                               │  Dashboard   │                             │
│                               │  (FastAPI)   │                             │
│                               └──────────────┘                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## SECTION 2: MODULE RESPONSIBILITIES

### 2.1 Config Manager
- Single source of truth for all configuration
- Runtime config reload without restart
- Environment separation (paper/live)
- Secrets never in code — YAML with env var override
- Validates all config on startup

### 2.2 Auth Manager
- TOTP-based login with auto-refresh
- Session token lifecycle management
- Re-login on session expiry
- Audit log of all auth events

### 2.3 Broker Connector (Angel One SmartAPI)
- REST client with retry + exponential backoff
- Rate limit tracking (enforced at request layer)
- WebSocket connection with auto-reconnect
- Heartbeat monitoring
- Order acknowledgement tracking
- Duplicate order prevention

### 2.4 Market Data Engine
- Normalizes raw tick data to internal format
- Builds OHLCV bars from ticks
- Maintains rolling VWAP (volume-weighted)
- Parses and caches option chain
- Calculates IV using Black-76 model (appropriate for index options)
- Calculates Greeks: delta, gamma, theta, vega
- Stale data detection and alerting

### 2.5 Signal Engine
- CPR and regime classification at 9:10 AM
- ORB calculation at 9:35 AM
- Continuous VWAP trend tracking
- Volume analysis
- EMA stack monitoring
- Signal composition with all validation checks
- Signal logging for audit

### 2.6 Strategy Engine
- Implements exact AVCS rulebook
- Tuesday Gamma Mode variant
- Strike selection algorithm
- Position sizing calculation
- Strategy state machine (WAITING → SIGNAL → ENTRY → MANAGING → EXIT)

### 2.7 Risk Engine
- Pre-trade validation (all 11 entry conditions)
- Real-time position monitoring
- Daily limit tracking
- Circuit breaker logic
- Kill switch capability
- Never bypassed under any circumstances

### 2.8 Execution Engine
- **Paper Simulator**: Realistic simulation with spread, latency, partial fills
- **Live Engine** (Phase 5): Real orders via Angel One
- Order state machine: PENDING → OPEN → PARTIAL → FILLED / REJECTED / CANCELLED
- Duplicate order guard using order ID tracking

### 2.9 Expense Engine
- Calculates all charges on every trade
- Brokerage, STT, exchange charges, GST, SEBI, stamp duty
- Per-trade, daily, weekly, monthly charge aggregation
- Net vs. gross P&L separation

### 2.10 Position Manager
- Single source of truth for all open positions
- Real-time unrealized P&L calculation
- Position state updates from execution engine
- Square-off logic for forced exits

### 2.11 Journal Engine
- Immutable append-only trade log
- Captures: entry time, exit time, signal reasons, exit reasons, all fills
- Full audit trail for every decision

### 2.12 Analytics Engine
- Win rate, expectancy, profit factor
- Sharpe ratio, Sortino ratio
- Max drawdown, drawdown duration
- Equity curve generation
- Strategy health scoring

### 2.13 Dashboard Engine (FastAPI)
- REST API for dashboard data
- Live WebSocket feed for positions/P&L
- HTML dashboard with charts

### 2.14 Alert Engine (Telegram)
- Trade entries/exits
- Risk limit alerts
- System errors
- Daily summary

### 2.15 Health Monitor
- API connectivity check every 30 seconds
- Data staleness detection
- Position/order sync verification
- System resource monitoring (CPU, memory)

---

## SECTION 3: FULL FOLDER STRUCTURE

```
algo/
│
├── config/
│   ├── credentials.yaml          # API keys, secrets (NEVER in git)
│   ├── strategy_config.yaml      # Strategy parameters
│   ├── risk_config.yaml          # Risk limits
│   ├── expense_config.yaml       # Charge rates
│   ├── system_config.yaml        # System settings
│   └── event_calendar.yaml       # Macro blackout dates
│
├── core/
│   │
│   ├── auth/
│   │   ├── __init__.py
│   │   └── angel_auth.py         # TOTP login, session management
│   │
│   ├── broker/
│   │   ├── __init__.py
│   │   ├── angel_connector.py    # REST API wrapper
│   │   ├── websocket_handler.py  # WebSocket + reconnect
│   │   └── rate_limiter.py       # API rate limit enforcer
│   │
│   ├── data/
│   │   ├── __init__.py
│   │   ├── market_data_engine.py # Core data processing hub
│   │   ├── tick_normalizer.py    # Raw tick → internal format
│   │   ├── bar_builder.py        # Tick → OHLCV bars
│   │   ├── vwap_engine.py        # VWAP + VWAP slope
│   │   ├── option_chain.py       # Chain parsing, caching
│   │   ├── greeks_calculator.py  # Black-76 IV + Greeks
│   │   └── historical_loader.py  # NSE historical data loader
│   │
│   ├── strategy/
│   │   ├── __init__.py
│   │   ├── base_strategy.py      # Abstract base class
│   │   ├── signal_engine.py      # Regime + signal composition
│   │   ├── avcs_strategy.py      # AVCS main strategy
│   │   ├── gamma_mode.py         # Tuesday expiry variant
│   │   └── strike_selector.py    # Strike selection logic
│   │
│   ├── execution/
│   │   ├── __init__.py
│   │   ├── order_manager.py      # Order state machine
│   │   ├── paper_simulator.py    # Realistic paper trading engine
│   │   ├── position_manager.py   # Open position tracking
│   │   └── square_off.py         # Forced exit logic
│   │
│   ├── risk/
│   │   ├── __init__.py
│   │   ├── risk_engine.py        # Pre-trade + intraday risk
│   │   ├── circuit_breaker.py    # Trip conditions + recovery
│   │   └── kill_switch.py        # Emergency halt
│   │
│   ├── expenses/
│   │   ├── __init__.py
│   │   └── expense_engine.py     # Full Indian charges
│   │
│   └── journal/
│       ├── __init__.py
│       └── trade_journal.py      # Immutable trade log
│
├── analytics/
│   ├── __init__.py
│   ├── analytics_engine.py       # Performance metrics
│   ├── equity_curve.py           # Equity curve generation
│   └── monte_carlo.py            # Monte Carlo robustness test
│
├── dashboard/
│   ├── app.py                    # FastAPI app
│   ├── routers/
│   │   ├── positions.py
│   │   ├── trades.py
│   │   ├── analytics.py
│   │   └── system.py
│   └── static/
│       ├── index.html
│       └── dashboard.js
│
├── backtesting/
│   ├── __init__.py
│   ├── backtest_engine.py        # Main backtesting framework
│   ├── data_loader.py            # Historical data prep
│   ├── walk_forward.py           # Walk-forward testing
│   └── regime_tester.py          # Regime-specific analysis
│
├── alerts/
│   ├── __init__.py
│   └── telegram_alerts.py        # Telegram notification engine
│
├── monitoring/
│   ├── __init__.py
│   └── health_monitor.py         # System health checks
│
├── database/
│   ├── __init__.py
│   ├── models.py                 # SQLAlchemy ORM models
│   ├── db_manager.py             # DB operations
│   └── migrations/               # Schema migrations
│
├── tests/
│   ├── unit/
│   │   ├── test_expense_engine.py
│   │   ├── test_risk_engine.py
│   │   ├── test_signal_engine.py
│   │   └── test_paper_simulator.py
│   └── integration/
│       └── test_system_flow.py
│
├── scripts/
│   ├── download_historical.py    # NSE data download script
│   ├── verify_connection.py      # Broker API health check
│   └── run_backtest.py           # Backtest runner
│
├── logs/                         # Rotating log files
│   ├── trading.log
│   ├── signals.log
│   ├── errors.log
│   └── audit.log
│
├── data/                         # Local market data cache
│   ├── historical/
│   └── option_chains/
│
├── main.py                       # System entrypoint
├── requirements.txt
├── .env.example                  # Template for secrets
├── .gitignore                    # MUST include credentials.yaml, .env, logs/
└── README.md
```

---

## SECTION 4: DATABASE SCHEMA

### 4.1 Core Tables (SQLite for paper, PostgreSQL for production)

```sql
-- ============================================================
-- TRADES TABLE: One row per completed trade (round trip)
-- ============================================================
CREATE TABLE trades (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id                VARCHAR(36) NOT NULL UNIQUE,  -- UUID
    session_date            DATE NOT NULL,
    
    -- Instrument
    symbol                  VARCHAR(30) NOT NULL,         -- e.g., NIFTY24DEC19500CE
    underlying              VARCHAR(20) NOT NULL DEFAULT 'NIFTY',
    option_type             VARCHAR(2) NOT NULL,          -- CE / PE
    strike                  INTEGER NOT NULL,
    expiry_date             DATE NOT NULL,
    
    -- Strategy
    strategy_name           VARCHAR(50) NOT NULL,         -- AVCS / GAMMA_MODE
    regime                  VARCHAR(30) NOT NULL,         -- TREND / NEUTRAL / NO_TRADE
    signal_reason           TEXT,                         -- JSON: all conditions at entry
    
    -- Entry
    entry_time              TIMESTAMP NOT NULL,
    entry_price             DECIMAL(10,2) NOT NULL,
    entry_spot_price        DECIMAL(10,2) NOT NULL,
    entry_vwap              DECIMAL(10,2),
    entry_iv                DECIMAL(8,4),
    entry_delta             DECIMAL(6,4),
    entry_oi                INTEGER,
    entry_spread            DECIMAL(8,2),
    lots                    INTEGER NOT NULL,
    quantity                INTEGER NOT NULL,             -- lots * 65
    
    -- Exit
    exit_time               TIMESTAMP,
    exit_price              DECIMAL(10,2),
    exit_spot_price         DECIMAL(10,2),
    exit_reason             VARCHAR(50),                  -- HARD_STOP / TRAILING / TARGET / TIME / etc.
    
    -- P&L
    gross_pnl               DECIMAL(12,2),
    total_charges           DECIMAL(10,2),
    net_pnl                 DECIMAL(12,2),
    pnl_pct                 DECIMAL(8,4),                -- % return on premium invested
    
    -- Status
    status                  VARCHAR(20) NOT NULL DEFAULT 'OPEN',
    -- OPEN / CLOSED / PARTIAL / CANCELLED
    
    -- Mode
    execution_mode          VARCHAR(10) NOT NULL DEFAULT 'PAPER',  -- PAPER / LIVE
    
    created_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_trades_session_date ON trades(session_date);
CREATE INDEX idx_trades_status ON trades(status);
CREATE INDEX idx_trades_execution_mode ON trades(execution_mode);


-- ============================================================
-- ORDERS TABLE: Individual order legs (can have multiple per trade)
-- ============================================================
CREATE TABLE orders (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id                VARCHAR(36) NOT NULL UNIQUE,  -- UUID internal
    broker_order_id         VARCHAR(50),                  -- Angel One order ID
    trade_id                VARCHAR(36) REFERENCES trades(trade_id),
    
    -- Order details
    symbol                  VARCHAR(30) NOT NULL,
    order_type              VARCHAR(20) NOT NULL,         -- MARKET / LIMIT / SL / SL-M
    transaction_type        VARCHAR(4) NOT NULL,          -- BUY / SELL
    quantity                INTEGER NOT NULL,
    price                   DECIMAL(10,2),                -- NULL for market orders
    trigger_price           DECIMAL(10,2),                -- For SL orders
    
    -- Fill details
    filled_quantity         INTEGER DEFAULT 0,
    average_fill_price      DECIMAL(10,2),
    fill_time               TIMESTAMP,
    
    -- Status
    status                  VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    -- PENDING / OPEN / PARTIAL / FILLED / REJECTED / CANCELLED
    rejection_reason        TEXT,
    
    -- Simulation details (paper mode)
    simulated_slippage      DECIMAL(8,2),
    simulated_spread_cost   DECIMAL(8,2),
    simulated_latency_ms    INTEGER,
    
    created_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_orders_trade_id ON orders(trade_id);
CREATE INDEX idx_orders_status ON orders(status);


-- ============================================================
-- CHARGES TABLE: Per-trade expense breakdown
-- ============================================================
CREATE TABLE charges (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id                VARCHAR(36) NOT NULL REFERENCES trades(trade_id),
    session_date            DATE NOT NULL,
    
    -- Charge breakdown
    brokerage               DECIMAL(10,2) NOT NULL DEFAULT 0,
    stt                     DECIMAL(10,2) NOT NULL DEFAULT 0,
    exchange_transaction    DECIMAL(10,2) NOT NULL DEFAULT 0,
    gst                     DECIMAL(10,2) NOT NULL DEFAULT 0,
    sebi_charges            DECIMAL(10,2) NOT NULL DEFAULT 0,
    stamp_duty              DECIMAL(10,2) NOT NULL DEFAULT 0,
    dp_charges              DECIMAL(10,2) NOT NULL DEFAULT 0,
    
    -- Summary
    total_charges           DECIMAL(10,2) NOT NULL DEFAULT 0,
    
    -- Turnover
    buy_turnover            DECIMAL(14,2),
    sell_turnover           DECIMAL(14,2),
    
    created_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_charges_trade_id ON charges(trade_id);
CREATE INDEX idx_charges_session_date ON charges(session_date);


-- ============================================================
-- POSITIONS TABLE: Live snapshot of open positions
-- ============================================================
CREATE TABLE positions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id             VARCHAR(36) NOT NULL UNIQUE,
    trade_id                VARCHAR(36) REFERENCES trades(trade_id),
    
    -- Instrument
    symbol                  VARCHAR(30) NOT NULL,
    option_type             VARCHAR(2) NOT NULL,
    strike                  INTEGER NOT NULL,
    expiry_date             DATE NOT NULL,
    
    -- Position details
    direction               VARCHAR(4) NOT NULL,          -- LONG (we only buy)
    lots                    INTEGER NOT NULL,
    quantity                INTEGER NOT NULL,
    entry_price             DECIMAL(10,2) NOT NULL,
    current_price           DECIMAL(10,2),
    
    -- Greeks snapshot
    current_delta           DECIMAL(6,4),
    current_gamma           DECIMAL(8,6),
    current_theta           DECIMAL(8,4),
    current_iv              DECIMAL(8,4),
    
    -- Risk state
    trailing_high           DECIMAL(10,2),
    trailing_activated      BOOLEAN DEFAULT FALSE,
    trailing_stop_price     DECIMAL(10,2),
    hard_stop_price         DECIMAL(10,2) NOT NULL,
    
    -- P&L
    unrealized_pnl          DECIMAL(12,2) DEFAULT 0,
    unrealized_pnl_pct      DECIMAL(8,4) DEFAULT 0,
    
    -- Status
    status                  VARCHAR(20) NOT NULL DEFAULT 'OPEN',
    -- OPEN / SQUAREDOFF / EXPIRED
    
    opened_at               TIMESTAMP NOT NULL,
    last_updated            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at               TIMESTAMP
);


-- ============================================================
-- SIGNALS TABLE: All generated signals (traded and skipped)
-- ============================================================
CREATE TABLE signals (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id               VARCHAR(36) NOT NULL UNIQUE,
    signal_time             TIMESTAMP NOT NULL,
    session_date            DATE NOT NULL,
    
    -- Signal
    direction               VARCHAR(4) NOT NULL,          -- CE / PE
    signal_type             VARCHAR(30) NOT NULL,         -- ORB_BREAKOUT / VWAP_BOUNCE / etc.
    strategy_name           VARCHAR(50) NOT NULL,
    
    -- Market state at signal
    spot_price              DECIMAL(10,2) NOT NULL,
    vwap                    DECIMAL(10,2),
    orb_high                DECIMAL(10,2),
    orb_low                 DECIMAL(10,2),
    india_vix               DECIMAL(6,2),
    regime                  VARCHAR(30),
    
    -- Proposed trade
    proposed_strike         INTEGER,
    proposed_option         VARCHAR(30),
    option_premium          DECIMAL(10,2),
    option_delta            DECIMAL(6,4),
    option_spread           DECIMAL(8,2),
    option_oi               INTEGER,
    
    -- Conditions (JSON)
    conditions_met          TEXT,                         -- JSON array of passed conditions
    conditions_failed       TEXT,                         -- JSON array of failed conditions
    
    -- Outcome
    action_taken            VARCHAR(20) NOT NULL,         -- TRADED / REJECTED / RISK_BLOCKED
    rejection_reason        TEXT,
    trade_id                VARCHAR(36),                  -- FK if traded
    
    created_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_signals_session_date ON signals(session_date);
CREATE INDEX idx_signals_action ON signals(action_taken);


-- ============================================================
-- DAILY_SUMMARY TABLE: End-of-day aggregated stats
-- ============================================================
CREATE TABLE daily_summary (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    session_date            DATE NOT NULL UNIQUE,
    
    -- Session metadata
    regime_classification   VARCHAR(30),
    india_vix_open          DECIMAL(6,2),
    india_vix_close         DECIMAL(6,2),
    nifty_open              DECIMAL(10,2),
    nifty_close             DECIMAL(10,2),
    gap_size_pct            DECIMAL(6,4),
    
    -- Trade stats
    total_trades            INTEGER DEFAULT 0,
    winning_trades          INTEGER DEFAULT 0,
    losing_trades           INTEGER DEFAULT 0,
    no_trade_days           BOOLEAN DEFAULT FALSE,
    signals_generated       INTEGER DEFAULT 0,
    signals_rejected        INTEGER DEFAULT 0,
    
    -- P&L
    gross_pnl               DECIMAL(12,2) DEFAULT 0,
    total_charges           DECIMAL(10,2) DEFAULT 0,
    net_pnl                 DECIMAL(12,2) DEFAULT 0,
    
    -- Charge breakdown
    total_brokerage         DECIMAL(10,2) DEFAULT 0,
    total_stt               DECIMAL(10,2) DEFAULT 0,
    total_exchange_charges  DECIMAL(10,2) DEFAULT 0,
    total_gst               DECIMAL(10,2) DEFAULT 0,
    
    -- Risk events
    circuit_breaker_trips   INTEGER DEFAULT 0,
    risk_blocks             INTEGER DEFAULT 0,
    api_disconnects         INTEGER DEFAULT 0,
    
    -- Execution
    execution_mode          VARCHAR(10) DEFAULT 'PAPER',
    
    created_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);


-- ============================================================
-- SYSTEM_EVENTS TABLE: Audit log for all system events
-- ============================================================
CREATE TABLE system_events (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    event_time              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    event_type              VARCHAR(50) NOT NULL,
    -- AUTH_LOGIN / API_DISCONNECT / API_RECONNECT / CIRCUIT_BREAKER_TRIP
    -- KILL_SWITCH / STALE_DATA / ORDER_REJECTED / RISK_BREACH / etc.
    severity                VARCHAR(10) NOT NULL DEFAULT 'INFO',
    -- DEBUG / INFO / WARNING / ERROR / CRITICAL
    message                 TEXT NOT NULL,
    details                 TEXT,                         -- JSON additional context
    resolved                BOOLEAN DEFAULT FALSE,
    resolved_at             TIMESTAMP
);

CREATE INDEX idx_events_type ON system_events(event_type);
CREATE INDEX idx_events_severity ON system_events(severity);
CREATE INDEX idx_events_time ON system_events(event_time);


-- ============================================================
-- TICK_DATA TABLE: Raw tick storage (optional — fills fast)
-- ============================================================
CREATE TABLE tick_data (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    token                   INTEGER NOT NULL,
    symbol                  VARCHAR(30) NOT NULL,
    timestamp               TIMESTAMP NOT NULL,
    ltp                     DECIMAL(10,2) NOT NULL,
    bid                     DECIMAL(10,2),
    ask                     DECIMAL(10,2),
    volume                  INTEGER,
    oi                      INTEGER
);

CREATE INDEX idx_tick_token_time ON tick_data(token, timestamp);
-- NOTE: Consider partitioning by date and purging daily to control DB size


-- ============================================================
-- OHLCV_BARS TABLE: 1-minute bars for strategy computation
-- ============================================================
CREATE TABLE ohlcv_bars (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    token                   INTEGER NOT NULL,
    symbol                  VARCHAR(30) NOT NULL,
    bar_time                TIMESTAMP NOT NULL,           -- Start of 1-min bar
    open                    DECIMAL(10,2) NOT NULL,
    high                    DECIMAL(10,2) NOT NULL,
    low                     DECIMAL(10,2) NOT NULL,
    close                   DECIMAL(10,2) NOT NULL,
    volume                  INTEGER NOT NULL DEFAULT 0,
    oi                      INTEGER,
    vwap                    DECIMAL(10,2),
    
    UNIQUE(token, bar_time)
);

CREATE INDEX idx_bars_token_time ON ohlcv_bars(token, bar_time);
```

---

## SECTION 5: CONFIGURATION FILES

### 5.1 strategy_config.yaml

```yaml
strategy:
  name: "AVCS_v1"
  version: "1.0.0"
  mode: "PAPER"  # PAPER | LIVE

  # Opening Range Breakout
  orb:
    start_time: "09:25:00"
    end_time: "09:35:00"
    min_range_points: 15
    max_range_points: 100
    breakout_buffer_points: 5
    breakout_buffer_pct: 0.05  # 5% of ORB range, whichever is larger

  # Entry windows
  entry_windows:
    - start: "09:45:00"
      end: "10:30:00"
    - start: "14:00:00"
      end: "14:45:00"

  # Technical filters
  vwap:
    slope_lookback_bars: 5

  ema:
    periods: [5, 13, 21]

  volume:
    confirmation_multiplier: 1.5
    lookback_periods: 20

  # Option selection
  options:
    lot_size: 65
    strike_interval: 50
    preferred_delta_min: 0.35
    preferred_delta_max: 0.65
    max_spread_absolute: 2.0
    max_spread_pct: 0.03
    min_oi: 100000
    min_volume_today: 500
    max_strikes_otm: 1

  # Exit parameters
  exits:
    hard_stop_pct: -0.35
    profit_target_pct: 0.50
    trailing_activation_pct: 0.20
    trailing_stop_pct: 0.15
    intraday_cutoff: "15:15:00"
    midday_exit_below_pct: 0.10

  # Tuesday gamma mode
  gamma_mode:
    enabled: true
    entry_start: "10:30:00"
    exit_cutoff: "15:00:00"
    hard_stop_pct: -0.40
    profit_target_pct: 0.60
    trailing_stop_pct: 0.20
```

### 5.2 risk_config.yaml

```yaml
risk:
  # Per-trade limits
  per_trade:
    max_risk_pct: 0.005          # 0.5% of capital per trade
    max_premium_exposure_pct: 0.02  # 2% of capital max in premium
    max_lots: 3                  # Never more than 3 lots in paper

  # Daily limits
  daily:
    max_loss_pct: 0.02           # 2% daily loss halt
    max_consecutive_losses: 3
    max_trades: 2
    cooldown_on_limit_hit: true

  # System limits
  system:
    max_drawdown_pct: 0.05       # 5% → circuit breaker
    circuit_breaker_reset: "manual"  # manual | next_day

  # Market conditions
  market:
    max_vix: 20.0
    min_option_oi: 100000
    max_spread_pct: 0.03
    stale_data_threshold_sec: 30

  # Kill switch
  kill_switch:
    enabled: true
    trigger_file: "/tmp/algo_kill_switch"  # Touch this file to halt
```

### 5.3 expense_config.yaml

```yaml
charges:
  # Angel One flat brokerage (options)
  brokerage_flat: 20.00          # Rs. 20 per order flat

  # STT: 0.05% on sell side premium
  stt_sell_pct: 0.0005

  # Exchange transaction charges (NSE): 0.053% of premium
  exchange_txn_pct: 0.00053

  # GST: 18% on (brokerage + exchange charges)
  gst_pct: 0.18

  # SEBI charges: Rs. 10 per crore of turnover
  sebi_per_crore: 10.0

  # Stamp duty: 0.003% on buy side
  stamp_duty_buy_pct: 0.00003

  # DP charges: not applicable for options (no delivery)
  dp_charges: 0.0
```

---

## SECTION 6: DATA FLOW SEQUENCE

```
09:10 AM  → Auth check, session refresh if needed
09:10 AM  → Regime classification: VIX, CPR, gap analysis → REGIME_FLAG set
09:10 AM  → Load event calendar → check for NO_TRADE day
09:15 AM  → WebSocket connect → start tick ingestion for NIFTY spot + option chain
09:15 AM  → Begin bar building, VWAP calculation
09:25 AM  → ORB collection begins
09:35 AM  → ORB locked: ORB_HIGH, ORB_LOW, ORB_RANGE calculated
09:45 AM  → Entry window OPEN → Signal engine armed
             [Continuous loop every 1-min bar close:]
               → Update VWAP, EMA stack, volume averages
               → Check for ORB breakout
               → If breakout → run all 11 entry conditions
               → If all pass → Risk engine pre-trade check
               → If approved → Execute (paper or live)
               → If in position → Check exit conditions every tick
10:30 AM  → Primary window closes (if in position, continue managing)
11:30 AM  → Midday chop filter: close low-profit positions
14:00 AM  → Secondary entry window opens
14:45 AM  → Secondary entry window closes
15:15 PM  → HARD CLOSE: force exit all open positions regardless of P&L
15:30 PM  → Session close: calculate daily summary, update DB, send Telegram report
```

---

**Phase 2 Complete. Proceed to Phase 3: Code Implementation.**
