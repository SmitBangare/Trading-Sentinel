"""
[SMIT-FYERS-OPTIONS 2026-09-25] Order path for the TradingView -> Fyers
pipeline. Direct structural copy of fno_executor.py's FnoExecutor
(paper/live split, LIMIT-only philosophy, never-chase-a-missed-fill
discipline) against FyersClient instead of KiteClient.

Paper mode fills honestly against the real book: entry at ask, exit at
bid -- paying the spread in paper is deliberate, the same reasoning
fno_executor.py documents (a mid-price paper fill would make the paper
leg lie about real trading costs).

[LIVE-PATH VERIFICATION NEEDED] The live order-status polling below
(_wait_for_fill) assumes Fyers' documented orderbook status codes
(2 = Filled/Traded per https://myapi.fyers.in/docsv3) and a
`tradedPrice` fill-price field. This is UNVERIFIED against a real Fyers
account (no credentials exist yet -- see the approved plan's rollout
step 7). Structurally correct, but the exact field names must be
confirmed against a live orderbook response before TV_FYERS_LIVE_TRADING
is ever considered. paper_mode=True (the only mode this pipeline runs in
for now) never reaches this code path at all.
"""
from __future__ import annotations

import asyncio
from typing import Optional
from uuid import uuid4

import structlog

from config import settings

logger = structlog.get_logger()

# Fyers orderbook status codes (per https://myapi.fyers.in/docsv3).
_FYERS_STATUS_FILLED = 2
_FYERS_STATUS_REJECTED = 5
_FYERS_STATUS_CANCELLED = 1

# Hard-flat marketable-limit depth, same rationale as fno_executor.py's
# HARD_FLAT_TICKS_THROUGH: deep enough to cross any sane NIFTY-option
# book, still a LIMIT so a broken book (bid=0.05) cannot fill us at 0.
HARD_FLAT_TICKS_THROUGH = 100


class TvFyersExecutor:
    def __init__(self, fyers, paper_mode: bool = True, source_tag: str = "TV_FYERS_PAPER"):
        self.fyers = fyers
        self.paper_mode = paper_mode
        self.source_tag = source_tag
        self.fill_timeout_sec = 30.0
        self.poll_interval_sec = 2.0

    # ------------------------------------------------------------------
    # entry
    # ------------------------------------------------------------------

    async def execute_entry(self, symbol: str, qty: int, ask: float) -> dict:
        """BUY LIMIT at ask. Returns {status, order_id, fill_price}.
        status in: paper | filled | timeout | rejected."""
        if self.paper_mode:
            logger.info(
                "tv_fyers_paper_entry symbol=%s qty=%d fill=ask=%.2f tag=%s",
                symbol, qty, ask, self.source_tag,
            )
            return {
                "status": "paper",
                "order_id": f"PAPER-TVF-ENT-{uuid4().hex[:8]}",
                "fill_price": ask,
            }
        resp = await self._place_limit(symbol, "BUY", qty, ask, intent="entry")
        order_id = resp.get("order_id")
        if not order_id:
            return {"status": "rejected", "order_id": None, "fill_price": None}
        fill = await self._wait_for_fill(order_id)
        if fill is None:
            await self._cancel_quietly(order_id)
            logger.warning(
                "tv_fyers_entry_timeout_cancelled symbol=%s order_id=%s -- never chase",
                symbol, order_id,
            )
            return {"status": "timeout", "order_id": order_id, "fill_price": None}
        return {"status": "filled", "order_id": order_id, "fill_price": fill}

    # ------------------------------------------------------------------
    # exit
    # ------------------------------------------------------------------

    async def execute_exit(
        self, symbol: str, qty: int, bid: float,
        tick_size: float, hard_flat: bool = False,
    ) -> dict:
        """Submit one SELL LIMIT. An uncertain fill requires reconciliation
        (mirrors fno_executor.py -- never replace an order merely because
        polling did not prove its final fill)."""
        if self.paper_mode:
            logger.info(
                "tv_fyers_paper_exit symbol=%s qty=%d fill=bid=%.2f hard_flat=%s tag=%s",
                symbol, qty, bid, hard_flat, self.source_tag,
            )
            return {
                "status": "paper",
                "order_id": f"PAPER-TVF-EXT-{uuid4().hex[:8]}",
                "fill_price": bid,
            }

        if hard_flat:
            price = max(tick_size, bid - HARD_FLAT_TICKS_THROUGH * tick_size)
            resp = await self._place_limit(symbol, "SELL", qty, price, intent="exit")
            order_id = resp.get("order_id")
            fill = await self._wait_for_fill(order_id) if order_id else None
            if fill is None:
                logger.critical(
                    "tv_fyers_hard_flat_unfilled symbol=%s order_id=%s -- OPERATOR "
                    "MUST FLATTEN MANUALLY (position may carry overnight)",
                    symbol, order_id,
                )
                return {"status": "unfilled", "order_id": order_id, "fill_price": None}
            return {"status": "filled", "order_id": order_id, "fill_price": fill}

        resp = await self._place_limit(symbol, "SELL", qty, bid, intent="exit")
        order_id = resp.get("order_id")
        if not order_id:
            return {"status": "rejected", "order_id": None, "fill_price": None}
        fill = await self._wait_for_fill(order_id, timeout=15.0)
        if fill is not None:
            return {"status": "filled", "order_id": order_id, "fill_price": fill}
        logger.critical(
            "tv_fyers_exit_ambiguous symbol=%s order_id=%s -- reconcile before retry",
            symbol, order_id,
        )
        return {"status": "unfilled", "order_id": order_id, "fill_price": None}

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------

    async def _place_limit(
        self, symbol: str, txn: str, qty: int, price: float, *, intent: str,
    ) -> dict:
        try:
            return await self.fyers.place_order(
                symbol=symbol,
                quantity=qty,
                transaction_type=txn,
                product_type="INTRADAY",   # intraday only in v1, matches FNO's MIS-equivalent
                order_type="LIMIT",
                limit_price=round(price, 2),
                validity="DAY",
                intent=intent, channel="tv_fyers",
            )
        except Exception as e:
            logger.error(
                "tv_fyers_place_order_failed symbol=%s txn=%s error=%s",
                symbol, txn, str(e),
            )
            return {"order_id": None, "status": "ERROR"}

    async def _wait_for_fill(
        self, order_id: Optional[str], timeout: Optional[float] = None,
    ) -> Optional[float]:
        """Poll order_history until Fyers reports the order filled.
        Returns the traded price, or None on timeout/rejection/cancel."""
        if not order_id:
            return None
        deadline = timeout if timeout is not None else self.fill_timeout_sec
        elapsed = 0.0
        while elapsed < deadline:
            try:
                history = await self.fyers.order_history(order_id)
                if history:
                    row = history[0]
                    status = row.get("status")
                    if status == _FYERS_STATUS_FILLED:
                        price = row.get("tradedPrice") or row.get("limitPrice")
                        if not price:
                            logger.warning(
                                "tv_fyers_filled_order_no_price order_id=%s "
                                "-- status=filled but no price field present; "
                                "treating as no-fill to avoid double-entry risk",
                                order_id,
                            )
                            return None
                        return float(price)
                    if status in (_FYERS_STATUS_REJECTED, _FYERS_STATUS_CANCELLED):
                        logger.warning(
                            "tv_fyers_order_terminal order_id=%s status=%s", order_id, status,
                        )
                        return None
            except Exception as e:
                logger.error("tv_fyers_order_history_failed order_id=%s error=%s", order_id, str(e))
            await asyncio.sleep(self.poll_interval_sec)
            elapsed += self.poll_interval_sec
        return None

    async def _cancel_quietly(self, order_id: str) -> None:
        try:
            await self.fyers.cancel_order(order_id)
        except Exception as e:
            logger.error("tv_fyers_cancel_failed order_id=%s error=%s", order_id, str(e))
