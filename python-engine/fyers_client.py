"""
[SMIT-FYERS-OPTIONS 2026-09-25] Broker client for the TradingView -> Fyers
options pipeline (smit_fyers_options branch). Mirrors kite_client.py's
KiteClient method shape (get_quote, place_order, cancel_order,
order_history, set_token, __init__(db_path)) so tv_fyers_orchestrator.py
can be written against either broker without special-casing.

v1 scope only (per the approved plan): quotes + historical candles (needed
even in paper mode -- position sizing, RSI, and simulated fills all need
real market data), and a place_order/cancel_order/order_history surface
that exists and is fully wired, but is never reached while paper mode is
on (gated in tv_fyers_executor.py, the same paper/live split fno_executor.py
uses). Broker-position/order-book reconciliation (Kite's
get_broker_positions/orders_snapshot/order_trades) is deliberately NOT
ported here yet -- that only matters once live orders exist.

Uses the official `fyers-apiv3` SDK (fyers_apiv3.fyersModel.FyersModel),
which is a SYNCHRONOUS/blocking client -- every call here wraps the SDK
call in asyncio.to_thread so it does not block the event loop, the same
async-facing shape KiteClient presents even though the underlying
transport differs (KiteClient talks httpx.AsyncClient directly; Fyers has
no first-party async client).

Safety: place_order reuses the SAME halt-switch / owner-entry-halt gate
KiteClient.place_order uses (halt_switch.assert_not_halted,
owner_entry_halt.is_owner_entry_halted). This is broker-agnostic
system-wide safety infrastructure, not a Kite-specific thing -- a new
broker client must not bypass it.
"""
from __future__ import annotations

import asyncio
import os
from typing import Optional

import structlog

from kite_client import RateLimiter
from halt_switch import TradingHalted, assert_not_halted
from owner_entry_halt import is_owner_entry_halted

logger = structlog.get_logger()

# Fyers numeric codes (SDK requires these, not strings -- see
# https://myapi.fyers.in/docsv3 place_order payload).
_FYERS_SIDE = {"BUY": 1, "SELL": -1}
_FYERS_ORDER_TYPE = {"LIMIT": 1, "MARKET": 2, "SL_M": 3, "SL_L": 4}


class FyersClient:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.access_token = ""
        self.client_id = os.getenv("FYERS_CLIENT_ID", "")
        self.limiter = RateLimiter(rate=3.0, burst=1)
        self._fyers = None  # built in set_token; no token, no client

    def set_token(self, token: str) -> None:
        """Arm the client with a fresh access token.

        Mirrors KiteClient.set_token's shape: read the app's client id from
        the environment (not settings) at call time, same placement
        convention as ZERODHA_API_KEY in kite_client.py.
        """
        self.access_token = token
        self.client_id = os.getenv("FYERS_CLIENT_ID", "")
        from fyers_apiv3 import fyersModel  # imported lazily: optional dep
        self._fyers = fyersModel.FyersModel(
            token=token,
            is_async=False,
            client_id=self.client_id,
            log_path="",
        )
        logger.info(
            "fyers_token_set suffix=...%s",
            token[-4:] if token and len(token) >= 4 else "?",
        )

    def _require_client(self):
        if self._fyers is None:
            raise RuntimeError("FyersClient.set_token() must be called before use")
        return self._fyers

    # ------------------------------------------------------------------
    # Market data (needed even in paper mode)
    # ------------------------------------------------------------------

    async def get_quote(self, symbols) -> dict:
        """Fetch live quotes for one or more Fyers symbols (e.g. "NSE:NIFTY24SEP24500CE").

        Fyers endpoint: FyersModel.quotes({"symbols": "SYM1,SYM2"})
        Returns: dict {symbol: {...fyers quote packet...}, ...} -- keyed by
        symbol string (Fyers has no numeric instrument-token concept the
        way Kite does), normalised from Fyers' own {"s": "ok", "d": [...]}
        response shape so callers don't need to know Fyers' envelope.
        """
        fyers = self._require_client()
        symbol_list = [symbols] if isinstance(symbols, str) else list(symbols)
        if not symbol_list:
            return {}
        await self.limiter.acquire()
        try:
            resp = await asyncio.to_thread(
                fyers.quotes, {"symbols": ",".join(symbol_list)}
            )
            if resp.get("s") != "ok":
                logger.warning("fyers_quote_failed response=%s", resp)
                return {}
            out = {}
            for row in resp.get("d", []):
                sym = row.get("n")
                if sym:
                    out[sym] = row.get("v", {})
            if len(out) != len(symbol_list):
                logger.warning(
                    "fyers_quote_partial requested=%d returned=%d",
                    len(symbol_list), len(out),
                )
            return out
        except Exception as exc:
            logger.error("fyers_quote_failed err=%s", str(exc))
            return {}

    async def get_historical(
        self, symbol: str, resolution: str, from_date: str, to_date: str
    ) -> list:
        """Fetch historical/candle data for RSI/ATR computation.

        Fyers endpoint: FyersModel.history({...}); range_from/range_to are
        accepted as epoch seconds OR "yyyy-mm-dd" with date_format=1 -- this
        wrapper always uses date_format=1 so callers pass plain date
        strings, matching the rest of this repo's date-handling style.
        Returns: raw `candles` list, each [timestamp, open, high, low, close, volume].
        """
        fyers = self._require_client()
        await self.limiter.acquire()
        try:
            resp = await asyncio.to_thread(
                fyers.history,
                {
                    "symbol": symbol,
                    "resolution": resolution,
                    "date_format": "1",
                    "range_from": from_date,
                    "range_to": to_date,
                    "cont_flag": "1",
                },
            )
            if resp.get("s") != "ok":
                logger.warning("fyers_history_failed symbol=%s response=%s", symbol, resp)
                return []
            return resp.get("candles", [])
        except Exception as exc:
            logger.error("fyers_history_failed symbol=%s err=%s", symbol, str(exc))
            return []

    async def get_option_chain(
        self, symbol: str, strike_count: int = 20, timestamp: str = "",
    ) -> dict:
        """Fetch the options chain around `symbol` (the underlying, e.g.
        "NSE:NIFTY50-INDEX"). This is what tv_fyers_orchestrator.py's
        `_resolve_option_contract()` needs to pick a real, currently-listed
        strike/expiry -- unlike Kite, Fyers has no bulk instrument-dump
        endpoint; the options chain call is the source of truth for which
        contracts actually exist right now.

        `timestamp`: empty string = nearest expiry (per Fyers' documented
        default); pass a specific expiry's epoch timestamp to select a
        different one.

        Returns the raw `{"s": ..., "data": {"optionsChain": [...], ...}}`
        response un-normalised -- callers pick the fields they need (this
        avoids guessing at a "correct" shape for data this repo has not
        exercised against a real account before).
        """
        fyers = self._require_client()
        await self.limiter.acquire()
        try:
            resp = await asyncio.to_thread(
                fyers.optionchain,
                {"symbol": symbol, "strikecount": strike_count, "timestamp": timestamp},
            )
            if resp.get("s") != "ok":
                logger.warning("fyers_option_chain_failed symbol=%s response=%s", symbol, resp)
                return {}
            return resp
        except Exception as exc:
            logger.error("fyers_option_chain_failed symbol=%s err=%s", symbol, str(exc))
            return {}

    # ------------------------------------------------------------------
    # Order placement -- exists and is fully wired, but tv_fyers_executor.py
    # never calls this while paper_mode=True (see fno_executor.py's
    # identical paper/live split for the pattern being mirrored).
    # ------------------------------------------------------------------

    async def place_order(
        self,
        symbol: str = "",
        quantity: int = 0,
        transaction_type: str = "BUY",
        product_type: str = "INTRADAY",
        order_type: str = "MARKET",
        limit_price: float = 0,
        stop_price: float = 0,
        validity: str = "DAY",
        *,
        intent: str,
        channel: Optional[str] = None,
    ) -> dict:
        """
        Place an order via Fyers. Same intent-required, keyword-only halt
        boundary as KiteClient.place_order -- there is deliberately no
        default so a future call site cannot silently inherit the wrong
        side of the entry/exit distinction (see kite_client.py's docstring
        for the full rationale; copied verbatim here, not reinvented).

        `intent="entry"` is gated by the global halt switch and the
        owner-entry-halt predicate; `intent="exit"` always proceeds.
        """
        if intent not in ("entry", "exit"):
            raise ValueError(f"intent must be 'entry' or 'exit', got {intent!r}")
        if not symbol or quantity <= 0:
            return {"order_id": None, "status": "ERROR",
                     "message": "symbol and positive quantity are required"}
        if transaction_type not in _FYERS_SIDE:
            return {"order_id": None, "status": "ERROR",
                     "message": f"unsupported transaction_type: {transaction_type!r}"}
        if order_type not in _FYERS_ORDER_TYPE:
            return {"order_id": None, "status": "ERROR",
                     "message": f"unsupported order_type: {order_type!r}"}

        def entry_blocker():
            if intent != "entry":
                return None
            owner_halt = is_owner_entry_halted(channel)
            if not owner_halt.allowed:
                logger.error(
                    "fyers_order_blocked_by_owner_entry_halt",
                    symbol=symbol, channel=channel, reason=owner_halt.reason,
                )
                return {"order_id": None, "status": "ERROR", "halted": True,
                         "owner_entry_halted": True,
                         "message": f"Owner entry halt: {owner_halt.reason}"}
            try:
                assert_not_halted(channel)
            except TradingHalted as exc:
                logger.error(
                    "fyers_order_blocked_by_halt",
                    symbol=symbol, channel=channel,
                    scope=exc.attribution.get("scope"),
                    by=exc.attribution.get("by"),
                    reason=exc.attribution.get("reason"),
                )
                return {"order_id": None, "status": "ERROR", "halted": True,
                         "message": str(exc)}
            return None

        blocked = entry_blocker()
        if blocked is not None:
            return blocked

        fyers = self._require_client()
        payload = {
            "symbol": symbol,
            "qty": int(quantity),
            "type": _FYERS_ORDER_TYPE[order_type],
            "side": _FYERS_SIDE[transaction_type],
            "productType": product_type,
            "limitPrice": float(limit_price or 0),
            "stopPrice": float(stop_price or 0),
            "validity": validity,
            "disclosedQty": 0,
            "offlineOrder": False,
            "isSliceOrder": False,
        }

        await self.limiter.acquire()
        # The limiter can yield while an operator trips either entry halt.
        # Recheck at dispatch, mirroring kite_client.py's same double-check.
        blocked = entry_blocker()
        if blocked is not None:
            return blocked
        try:
            resp = await asyncio.to_thread(fyers.place_order, payload)
            if resp.get("s") != "ok":
                logger.error("fyers_place_order_failed response=%s", resp)
                return {"order_id": None, "status": "ERROR",
                         "message": resp.get("message", str(resp))}
            return {"order_id": resp.get("id"), "status": "PLACED",
                     "message": "order placed"}
        except Exception as exc:
            logger.error("fyers_place_order_failed err=%s", str(exc))
            return {"order_id": None, "status": "ERROR", "message": str(exc)}

    async def cancel_order(self, order_id: str) -> dict:
        """Cancel a pending order. Fyers endpoint: FyersModel.cancel_order({"id": ...})."""
        if not order_id:
            return {"order_id": None, "status": "ERROR", "message": "order_id required"}
        fyers = self._require_client()
        await self.limiter.acquire()
        try:
            resp = await asyncio.to_thread(fyers.cancel_order, {"id": order_id})
            if resp.get("s") != "ok":
                return {"order_id": order_id, "status": "ERROR",
                         "message": resp.get("message", str(resp))}
            return {"order_id": order_id, "status": "CANCELLED"}
        except Exception as exc:
            logger.error("fyers_cancel_order_failed order_id=%s err=%s", order_id, str(exc))
            return {"order_id": order_id, "status": "ERROR", "message": str(exc)}

    async def order_history(self, order_id: str) -> list:
        """Fetch order status via the Fyers orderbook, filtered to one order_id.

        Fyers has no dedicated per-order-history endpoint the way Kite does
        (GET /orders/{order_id}); the orderbook() call returns all of
        today's orders, so this wrapper filters client-side. Returns a
        single-element list (or empty) for shape-compatibility with
        KiteClient.order_history's list return, even though Fyers has no
        chronological status-transition history to offer -- only current
        state.
        """
        if not order_id:
            return []
        fyers = self._require_client()
        await self.limiter.acquire()
        try:
            resp = await asyncio.to_thread(fyers.orderbook)
            if resp.get("s") != "ok":
                logger.warning("fyers_order_history_failed response=%s", resp)
                return []
            for row in resp.get("orderBook", []):
                if str(row.get("id")) == str(order_id):
                    return [row]
            return []
        except Exception as exc:
            logger.error("fyers_order_history_failed order_id=%s err=%s", order_id, str(exc))
            return []
