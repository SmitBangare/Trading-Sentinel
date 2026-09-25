"""
[SMIT-FYERS-OPTIONS 2026-09-25] Inbound TradingView signal model + log for
the TradingView -> Fyers options pipeline. Receives the ALREADY-VALIDATED
payload node-gateway forwards (the TradingView shared secret was checked
and stripped there -- routes_tv_fyers.py's own auth is the existing
X-Internal-Secret gate, not this secret).

Mirrors fno_signal_log.py's append-only-log shape: idempotent table init,
best-effort writes that must never crash the request path.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import aiosqlite
import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger()


class TvFyersSignal(BaseModel):
    """The forwarded, already-secret-validated TradingView alert body.

    No stop_loss/target fields by design -- the bot derives and owns
    every exit itself (tv_fyers_orchestrator.py). TradingView signals
    entries only.
    """
    signal_id: str = Field(min_length=1, max_length=80)
    symbol: str = Field(min_length=1, max_length=20)
    direction: str = Field(pattern="^(CE|PE)$")
    underlying_price: float = Field(gt=0)
    strategy: Optional[str] = Field(default=None, max_length=80)
    signal_time: str = Field(min_length=10, max_length=40)


async def init_tv_fyers_signal_db(db_path: str) -> None:
    """Create the tv_fyers_signals table if absent. Idempotent."""
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tv_fyers_signals (
                    signal_id         TEXT PRIMARY KEY,
                    symbol            TEXT NOT NULL,
                    direction         TEXT NOT NULL,
                    underlying_price  REAL NOT NULL,
                    strategy          TEXT,
                    signal_time       TEXT NOT NULL,
                    received_at       TEXT NOT NULL,
                    handled           INTEGER NOT NULL DEFAULT 0,
                    handled_result    TEXT
                )
            """)
            await db.commit()
    except Exception as e:
        logger.error("tv_fyers_signal_db_init_failed db=%s error=%s", db_path, str(e))


async def ingest_tv_signal(db_path: str, payload: dict) -> dict:
    """Validate + persist one inbound (node-gateway-forwarded) TradingView
    signal. Returns an ack dict; raises ValueError on a malformed payload
    (the route layer maps that to a 422).

    Does NOT trigger execution -- that is tv_fyers_orchestrator.py's job,
    called separately by the route handler once the orchestrator exists
    (rollout step 5). This function's only responsibility is validate +
    record, matching fno_signal_log.py's append-only-log discipline.
    """
    signal = TvFyersSignal.model_validate(payload)
    received_at = datetime.now(timezone.utc).isoformat()

    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO tv_fyers_signals
                    (signal_id, symbol, direction, underlying_price, strategy,
                     signal_time, received_at, handled, handled_result)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL)
                """,
                (
                    signal.signal_id, signal.symbol, signal.direction,
                    signal.underlying_price, signal.strategy,
                    signal.signal_time, received_at,
                ),
            )
            await db.commit()
    except Exception as e:
        # Best-effort log write, matching fno_signal_log.py: a persistence
        # failure must not crash the webhook request path, but it must be
        # loud -- a silently-dropped signal is worse than a logged failure.
        logger.error(
            "tv_fyers_signal_persist_failed signal_id=%s error=%s",
            signal.signal_id, str(e),
        )

    logger.info(
        "tv_fyers_signal_ingested signal_id=%s symbol=%s direction=%s",
        signal.signal_id, signal.symbol, signal.direction,
    )
    return {"received": True, "signal_id": signal.signal_id}


async def mark_signal_handled(db_path: str, signal_id: str, result: str) -> None:
    """Record the orchestrator's outcome for a signal (wired in rollout
    step 5, once tv_fyers_orchestrator.py exists). Kept here now so the
    table schema/contract is settled from the first commit."""
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "UPDATE tv_fyers_signals SET handled = 1, handled_result = ? WHERE signal_id = ?",
                (result, signal_id),
            )
            await db.commit()
    except Exception as e:
        logger.error(
            "tv_fyers_signal_mark_handled_failed signal_id=%s error=%s",
            signal_id, str(e),
        )
