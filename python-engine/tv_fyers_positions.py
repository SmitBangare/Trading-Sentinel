"""
[SMIT-FYERS-OPTIONS 2026-09-25] Position store for the TradingView ->
Fyers pipeline.

[SCOPE NOTE -- deliberate v1 reduction] fno_positions.py (the module this
mirrors) implements a full two-phase-commit exit-receipt system:
claim -> execute -> durable receipt -> idempotent settle, specifically to
protect against a REAL broker fill happening but the local commit
failing (a double-submission risk that only exists once orders are
real). Per the approved plan's explicit non-goal ("No Fyers order/
position broker-reconciliation -- deferred until live-mode work
begins"), this pipeline stays paper-only for its entire build -- paper
fills are synthetic and deterministic, so that reconciliation machinery
has nothing real to protect yet. This module uses a simpler atomic
claim (a single `UPDATE ... WHERE status='OPEN'`, which SQLite still
serialises safely) instead. If live trading is ever seriously
considered, THIS is the module to upgrade to fno_positions.py's
two-phase-commit discipline first -- not optional at that point, only
deferred until it matters.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import aiosqlite
import structlog

logger = structlog.get_logger()


@dataclass
class TvFyersPosition:
    id: int
    source: str
    signal_id: str
    symbol: str
    direction: str            # "CE" or "PE"
    qty: int
    entry_time: str
    entry_premium: float
    entry_underlying: float
    stop_underlying: float
    target_underlying: float
    premium_stop: float
    trail_active: int
    trail_stop_underlying: Optional[float]
    best_underlying: float
    atr_at_entry: float
    status: str                # "OPEN" or "CLOSED"
    entry_order_id: Optional[str]


async def init_tv_fyers_positions_db(db_path: str) -> None:
    """Idempotent table creation, mirrors fno_positions.py's init style."""
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tv_fyers_positions (
                    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                    source                 TEXT NOT NULL,
                    signal_id              TEXT NOT NULL,
                    symbol                 TEXT NOT NULL,
                    direction              TEXT NOT NULL,
                    qty                    INTEGER NOT NULL,
                    entry_time             TEXT NOT NULL,
                    entry_premium          REAL NOT NULL,
                    entry_underlying       REAL NOT NULL,
                    stop_underlying        REAL NOT NULL,
                    target_underlying      REAL NOT NULL,
                    premium_stop           REAL NOT NULL,
                    trail_active           INTEGER NOT NULL DEFAULT 0,
                    trail_stop_underlying  REAL,
                    best_underlying        REAL NOT NULL,
                    atr_at_entry           REAL NOT NULL,
                    status                 TEXT NOT NULL DEFAULT 'OPEN',
                    entry_order_id         TEXT,
                    exit_time              TEXT,
                    exit_premium           REAL,
                    exit_underlying        REAL,
                    exit_reason            TEXT,
                    gross_pnl              REAL,
                    costs                  REAL,
                    pnl                    REAL,
                    r_multiple             REAL,
                    exit_order_id          TEXT
                )
            """)
            await db.commit()
    except Exception as e:
        logger.error("tv_fyers_positions_db_init_failed db=%s error=%s", db_path, str(e))


def _row_to_position(row) -> TvFyersPosition:
    return TvFyersPosition(
        id=row[0], source=row[1], signal_id=row[2], symbol=row[3],
        direction=row[4], qty=row[5], entry_time=row[6],
        entry_premium=row[7], entry_underlying=row[8],
        stop_underlying=row[9], target_underlying=row[10],
        premium_stop=row[11], trail_active=row[12],
        trail_stop_underlying=row[13], best_underlying=row[14],
        atr_at_entry=row[15], status=row[16], entry_order_id=row[17],
    )


_SELECT_COLUMNS = (
    "id, source, signal_id, symbol, direction, qty, entry_time, "
    "entry_premium, entry_underlying, stop_underlying, target_underlying, "
    "premium_stop, trail_active, trail_stop_underlying, best_underlying, "
    "atr_at_entry, status, entry_order_id"
)


async def open_positions(db_path: str, source: str) -> List[TvFyersPosition]:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            f"SELECT {_SELECT_COLUMNS} FROM tv_fyers_positions "
            "WHERE source = ? AND status = 'OPEN'",
            (source,),
        )
        rows = await cur.fetchall()
    return [_row_to_position(r) for r in rows]


_ALL_COLUMNS = _SELECT_COLUMNS + (
    ", exit_time, exit_premium, exit_underlying, exit_reason, "
    "gross_pnl, costs, pnl, r_multiple, exit_order_id"
)


async def list_positions(
    db_path: str, source: Optional[str] = None,
    status: Optional[str] = None, limit: int = 100,
) -> List[dict]:
    """Newest-first read of the position store (open + closed, all
    columns including exit/P&L fields), for the dashboard panel.
    Read-only, never called from the entry/exit execution path."""
    where = []
    params: list = []
    if source:
        where.append("source = ?")
        params.append(source)
    if status:
        where.append("status = ?")
        params.append(status)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    params.append(limit)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"SELECT {_ALL_COLUMNS} FROM tv_fyers_positions {clause} "
            "ORDER BY id DESC LIMIT ?",
            params,
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def insert_position(db_path: str, **fields) -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            """
            INSERT INTO tv_fyers_positions
                (source, signal_id, symbol, direction, qty, entry_time,
                 entry_premium, entry_underlying, stop_underlying,
                 target_underlying, premium_stop, trail_active,
                 trail_stop_underlying, best_underlying, atr_at_entry,
                 status, entry_order_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
            """,
            (
                fields["source"], fields["signal_id"], fields["symbol"],
                fields["direction"], fields["qty"], fields["entry_time"],
                fields["entry_premium"], fields["entry_underlying"],
                fields["stop_underlying"], fields["target_underlying"],
                fields["premium_stop"], fields.get("trail_active", 0),
                fields.get("trail_stop_underlying"), fields["best_underlying"],
                fields["atr_at_entry"], fields.get("entry_order_id"),
            ),
        )
        await db.commit()
        return cur.lastrowid


async def update_trail(
    db_path: str, position_id: int, trail_active: int,
    trail_stop_underlying: Optional[float], best_underlying: float,
) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE tv_fyers_positions SET trail_active = ?, "
            "trail_stop_underlying = ?, best_underlying = ? "
            "WHERE id = ? AND status = 'OPEN'",
            (trail_active, trail_stop_underlying, best_underlying, position_id),
        )
        await db.commit()


async def claim_exit(db_path: str, position_id: int) -> bool:
    """Atomically move a position from OPEN to CLOSING. Returns True if
    THIS call won the claim (SQLite serialises the UPDATE, so only one
    concurrent caller can ever see rowcount==1). Mirrors the spirit of
    fno_positions.claim_exit_intent without the full receipt machinery
    (see module docstring for why that's an acceptable v1 reduction)."""
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "UPDATE tv_fyers_positions SET status = 'CLOSING' "
            "WHERE id = ? AND status = 'OPEN'",
            (position_id,),
        )
        await db.commit()
        return cur.rowcount == 1


async def settle_position_close(
    db_path: str, position_id: int, *,
    exit_time_ist: datetime, exit_premium: float, exit_underlying: float,
    exit_reason: str, gross_pnl: float, costs: float, pnl: float,
    r_multiple: float, exit_order_id: Optional[str],
) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE tv_fyers_positions SET status = 'CLOSED', exit_time = ?, "
            "exit_premium = ?, exit_underlying = ?, exit_reason = ?, "
            "gross_pnl = ?, costs = ?, pnl = ?, r_multiple = ?, exit_order_id = ? "
            "WHERE id = ?",
            (
                exit_time_ist.isoformat(), exit_premium, exit_underlying,
                exit_reason, gross_pnl, costs, pnl, r_multiple, exit_order_id,
                position_id,
            ),
        )
        await db.commit()


async def unclaim_exit(db_path: str, position_id: int) -> None:
    """Revert a CLOSING claim back to OPEN when the executor call itself
    failed before any order was dispatched (e.g. an exception raised
    from execute_exit) -- so the position is retried next tick instead
    of being stuck permanently in CLOSING."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE tv_fyers_positions SET status = 'OPEN' "
            "WHERE id = ? AND status = 'CLOSING'",
            (position_id,),
        )
        await db.commit()
