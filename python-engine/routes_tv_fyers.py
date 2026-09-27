"""
[SMIT-FYERS-OPTIONS 2026-09-25] TradingView -> Fyers options pipeline,
python-engine internal route.

Same pattern as every other internal route in this repo (routes_ops.py
etc.): `import main as _main` and call `_main._check_internal_secret`
at call time, not import time -- see routes_ops.py's module docstring for
why (main's globals get rebound at runtime, and the test suite patches
names on main by attribute, not by imported binding).

This is the ONLY inbound route for the new pipeline. node-gateway has
already validated TradingView's shared-secret body field (see
node-gateway/server/routes/tv-fyers-webhook.js) before forwarding here
with the existing X-Internal-Secret header -- python-engine never sees
the TradingView secret at all, only the trust boundary it already has
with node-gateway.

Ingest (validate + persist) happens first; the entry-orchestration call
runs after, and its own internal failures are caught and reflected in
`handled_result` (via tv_fyers_signal.mark_signal_handled) rather than
turning into a 500 back to node-gateway, which has already accepted the
webhook and has nothing useful to retry.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

import main as _main
from config import settings
from tv_fyers_signal import ingest_tv_signal, list_signals
import tv_fyers_positions as tv_positions
from token_lifecycle import TokenPayload
import structlog

logger = structlog.get_logger()
router = APIRouter()


@router.post("/internal/tv-fyers/token")
async def post_tv_fyers_token(payload: TokenPayload, request: Request):
    """Arm the Fyers client with a fresh access token. Mirrors routes_ops.py's
    Kite-equivalent `inject_token` -- node-gateway provisions this right
    after completing the Fyers OAuth callback (routes/tv-fyers-auth.js)."""
    _main._check_internal_secret(request, "post_tv_fyers_token")
    _main.fyers.set_token(payload.access_token)
    logger.info("tv_fyers_token_armed")
    return {"armed": True}


@router.post("/internal/tv-fyers/token/invalidate")
async def post_tv_fyers_token_invalidate(request: Request):
    _main._check_internal_secret(request, "post_tv_fyers_token_invalidate")
    _main.fyers.set_token("")
    logger.info("tv_fyers_token_invalidated")
    return {"armed": False}


@router.get("/internal/tv-fyers/optionchain")
async def get_tv_fyers_option_chain(
    request: Request, symbol: str = "NSE:NIFTY50-INDEX", strike_count: int = 20,
):
    """Read-only ops/debug endpoint: raw Fyers options chain for an
    underlying. Exists for two reasons: (1) operational visibility into
    what Fyers is actually returning, same spirit as the repo's other
    read-only internal routes, and (2) this is the exact data
    tv_fyers_orchestrator.py's `_resolve_option_contract()` stub needs --
    exposing it as a route makes it inspectable without a one-off script."""
    _main._check_internal_secret(request, "get_tv_fyers_option_chain")
    return await _main.fyers.get_option_chain(symbol, strike_count=strike_count)


@router.get("/internal/tv-fyers/status")
async def get_tv_fyers_status(request: Request):
    """Dashboard read: is the Fyers client actually armed right now, and
    how many paper positions are open. Reads FyersClient.access_token
    directly (the thing execution actually checks) rather than
    node-gateway's session-scoped auth-status flag, which only reflects
    the browser session that completed OAuth, not engine state."""
    _main._check_internal_secret(request, "get_tv_fyers_status")
    armed = bool(getattr(_main.fyers, "access_token", "") or "")
    open_paper = await tv_positions.open_positions(settings.DB_PATH, "TV_FYERS_PAPER")
    return {
        "fyers_token_armed": armed,
        "open_paper_positions": len(open_paper),
        "live_trading_enabled": settings.TV_FYERS_LIVE_TRADING,
    }


@router.get("/internal/tv-fyers/signals")
async def get_tv_fyers_signals(request: Request, limit: int = 50):
    """Dashboard read: newest-first TradingView signal log."""
    _main._check_internal_secret(request, "get_tv_fyers_signals")
    rows = await list_signals(settings.DB_PATH, limit=limit)
    return {"signals": rows}


@router.get("/internal/tv-fyers/positions")
async def get_tv_fyers_positions_route(request: Request, status: str = "ALL", limit: int = 100):
    """Dashboard read: paper position history (open + closed)."""
    _main._check_internal_secret(request, "get_tv_fyers_positions_route")
    rows = await tv_positions.list_positions(
        settings.DB_PATH, status=None if status == "ALL" else status, limit=limit,
    )
    return {"positions": rows}


@router.post("/internal/tv-fyers/backtest/run")
async def post_tv_fyers_backtest_run(
    request: Request, days_back: int = 365, refresh_cache: bool = False,
):
    """[SMIT-FYERS-OPTIONS-BACKTEST 2026-09-25] Mechanics-only backtest
    (see tv_fyers_backtest.py's module docstring for exactly what's real
    data vs. modelled -- no AI, no cost). Fetches real NIFTY candle
    history from Fyers (needs an armed token) ONLY when no local cache
    exists or refresh_cache=true; otherwise replays against the cached
    history for free and instantly. A fresh fetch for days_back=365 is a
    real, possibly slow, chunked network operation."""
    _main._check_internal_secret(request, "post_tv_fyers_backtest_run")
    from tv_fyers_backtest import (
        fetch_nifty_history, load_cached_history, result_to_dict,
        run_backtest, save_cached_history,
    )
    candles = None if refresh_cache else load_cached_history(settings.DB_PATH)
    fetched_fresh = False
    if candles is None:
        if not _main.fyers.access_token:
            raise HTTPException(
                status_code=409,
                detail="No cached history and no armed Fyers token -- log in "
                       "via /api/tv-fyers/auth/login first, or retry once armed.",
            )
        candles = await fetch_nifty_history(_main.fyers, days_back=days_back)
        save_cached_history(settings.DB_PATH, candles)
        fetched_fresh = True
    result = run_backtest(candles)
    logger.info(
        "tv_fyers_backtest_run_complete candles=%d signals=%d entries=%d trades=%d pnl=%.0f fetched_fresh=%s",
        len(candles), result.signals_generated, result.entries_taken,
        len(result.trades), result.total_pnl, fetched_fresh,
    )
    return {"fetched_fresh": fetched_fresh, "candle_count": len(candles), **result_to_dict(result)}


@router.post("/internal/tv-fyers/signal")
async def post_tv_fyers_signal(request: Request):
    _main._check_internal_secret(request, "post_tv_fyers_signal")
    payload = await request.json()
    try:
        result = await ingest_tv_signal(settings.DB_PATH, payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        from tv_fyers_orchestrator import handle_tv_entry_signal
        exec_result = await handle_tv_entry_signal(_main.fyers, settings.DB_PATH, payload)
        result["executed"] = exec_result.get("executed", False)
        result["reason"] = exec_result.get("reason", "")
    except Exception as exc:
        # A signal that fails to orchestrate is already durably persisted
        # (ingest above); never turn this into a 500 for node-gateway,
        # which has already accepted the webhook and cannot retry
        # anything useful. Loud log instead.
        logger.error(
            "tv_fyers_entry_orchestration_failed signal_id=%s err=%s",
            result.get("signal_id"), str(exc), exc_info=True,
        )
        result["executed"] = False
        result["reason"] = "orchestration_error"
    return result
