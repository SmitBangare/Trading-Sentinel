-- Enable Write-Ahead Logging for concurrency
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS received_signals (
  signal_id       TEXT PRIMARY KEY,
  ticker          TEXT NOT NULL,
  signal_time     TEXT NOT NULL,
  received_at     TEXT NOT NULL,
  payload_json    TEXT NOT NULL,
  telegram_msg_id INTEGER,
  status          TEXT NOT NULL CHECK (status IN ('PENDING','EXECUTING','EXECUTED','REJECTED','EXPIRED')),
  -- Fine-grained safety state. Kept separate from the legacy workflow status
  -- so existing SQLite CHECK constraints remain migration-compatible.
  execution_state TEXT NOT NULL DEFAULT 'IDLE',
  -- [WORKFLOW-J.9 2026-09-13] Bounded session phase at the moment
  -- of signal insertion (NOT signal_time). Stamped by the Node
  -- ``stampSessionPhaseForSignal`` helper from the J.6 mirror.
  -- The 10-string CHECK constraint mirrors
  -- ``market-hours.js::STAMPABLE_PHASES``; drift between the
  -- two is a category-1 invariant failure. DEFAULT 'UNKNOWN'
  -- covers existing rows (pre-J.9) so the additive migration
  -- does not break the production SQLite DB.
  session_phase   TEXT NOT NULL DEFAULT 'UNKNOWN'
    CHECK (session_phase IN (
      'CLOSED','PRE_MARKET','CONTINUOUS_TRADING',
      'CAS_REFERENCE_PRICE_WINDOW','CAS_ORDER_ENTRY',
      'CAS_LIMIT_ENTRY_ONLY','CAS_MATCHING','CAS_POST',
      'DERIVATIVES_CAS_ALIGNED','UNKNOWN'
    ))
);

CREATE TABLE IF NOT EXISTS executed_orders (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_id       TEXT NOT NULL REFERENCES received_signals(signal_id),
  ticker          TEXT NOT NULL,
  order_id        TEXT NOT NULL UNIQUE,
  order_type      TEXT NOT NULL, -- "MARKET" | "GTT"
  entry_price     REAL,
  shares          INTEGER NOT NULL,
  status          TEXT NOT NULL CHECK (status IN ('PLACED','COMPLETE','REJECTED','CANCELLED')),
  gtt_stop_id     TEXT,
  gtt_target_id   TEXT,
  sl_order_id     TEXT,  -- broker-side SL-M protecting an MIS position (GTT is CNC-only)
  placed_at       TEXT NOT NULL,
  filled_at       TEXT,
  sync_to_b       INTEGER DEFAULT 0, -- 0=pending, 1=done, 2=failed
  notes           TEXT,
  execution_state TEXT NOT NULL DEFAULT 'SUBMITTED'
);

CREATE TABLE IF NOT EXISTS app_state (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- [HIGH-007 / ROADMAP-4.5 2026-07-13] The approved snapshot.
--
-- The EXEC/EM callback handlers used to RE-FETCH the signal from the engine
-- at button-press time and execute whatever came back. So the price, share
-- count and stop that were shown in the Telegram message -- the numbers the
-- operator actually approved -- were not necessarily the numbers that got
-- executed. Two ways that bites:
--
--   1. Swing: /signals serves `current_signals`, which run_screener REPLACES
--      wholesale on every run. A re-run between alert and press changes the
--      numbers under the operator's feet.
--   2. Either book: if the re-fetch no longer contains the ticker, the press
--      dies with "signal not found" and the approved trade is simply lost.
--      The engine's momentum list is in-memory, so a restart wipes it -- as
--      happened on 2026-07-13 at 09:44.
--
-- Fix: the sender (agent) registers the EXACT payload it is about to display,
-- keyed by the SAME id it puts in callback_data. The handler then executes
-- that row. What you approved is what executes.
--
-- Immutable by construction: writes are INSERT OR IGNORE, so a re-alert for
-- the same id can never rewrite an already-approved snapshot.
CREATE TABLE IF NOT EXISTS approved_snapshots (
  signal_id    TEXT PRIMARY KEY,   -- exactly the callback_data id (TICKER / TICKER_MOM)
  ticker       TEXT NOT NULL,
  action       TEXT NOT NULL CHECK (action IN ('EXEC','EM')),
  payload_json TEXT NOT NULL,
  created_at   TEXT NOT NULL
);

-- Performance Indexes
CREATE INDEX IF NOT EXISTS idx_signals_status ON received_signals(status);
CREATE INDEX IF NOT EXISTS idx_orders_ticker ON executed_orders(ticker);

-- [HIGH-009] Status integrity triggers — enforce valid status values on EXISTING tables too.
-- SQLite CHECK constraints only apply at table creation; triggers cover live tables.
CREATE TRIGGER IF NOT EXISTS enforce_signal_status_insert
BEFORE INSERT ON received_signals FOR EACH ROW
BEGIN
  SELECT CASE
    WHEN NEW.status NOT IN ('PENDING','EXECUTING','EXECUTED','REJECTED','EXPIRED')
    THEN RAISE(ABORT, 'Invalid status for received_signals')
  END;
END;

CREATE TRIGGER IF NOT EXISTS enforce_signal_status_update
BEFORE UPDATE ON received_signals FOR EACH ROW
BEGIN
  SELECT CASE
    WHEN NEW.status NOT IN ('PENDING','EXECUTING','EXECUTED','REJECTED','EXPIRED')
    THEN RAISE(ABORT, 'Invalid status for received_signals')
  END;
END;

CREATE TRIGGER IF NOT EXISTS enforce_order_status_insert
BEFORE INSERT ON executed_orders FOR EACH ROW
BEGIN
  SELECT CASE
    WHEN NEW.status NOT IN ('PLACED','COMPLETE','REJECTED','CANCELLED')
    THEN RAISE(ABORT, 'Invalid status for executed_orders')
  END;
END;

CREATE TRIGGER IF NOT EXISTS enforce_order_status_update
BEFORE UPDATE ON executed_orders FOR EACH ROW
BEGIN
  SELECT CASE
    WHEN NEW.status NOT IN ('PLACED','COMPLETE','REJECTED','CANCELLED')
    THEN RAISE(ABORT, 'Invalid status for executed_orders')
  END;
END;

-- [SMIT-FYERS-OPTIONS 2026-09-25] TradingView -> Fyers options pipeline.
-- Entry signals only (the bot owns every exit -- see tv_fyers_orchestrator.py
-- in python-engine). Separate table from received_signals: different
-- schema/source, and this pipeline must never be confused with the
-- Zerodha-based equity signal path above.
CREATE TABLE IF NOT EXISTS tv_fyers_signals (
  signal_id        TEXT PRIMARY KEY,
  symbol           TEXT NOT NULL,
  direction        TEXT NOT NULL CHECK (direction IN ('CE','PE')),
  underlying_price REAL NOT NULL,
  strategy         TEXT,
  signal_time      TEXT NOT NULL,
  received_at      TEXT NOT NULL,
  payload_json     TEXT NOT NULL,
  forwarded        INTEGER NOT NULL DEFAULT 0,
  forward_status   TEXT,
  telegram_msg_id  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_tv_fyers_signals_symbol_time
  ON tv_fyers_signals(symbol, signal_time);
