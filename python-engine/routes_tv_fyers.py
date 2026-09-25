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
from tv_fyers_signal import ingest_tv_signal
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
