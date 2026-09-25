"""
[SMIT-FYERS-OPTIONS-AI 2026-09-25] Candle-trend double-check for the
TradingView -> Fyers pipeline, replacing the old mechanical RSI-threshold
veto (tv_fyers_gates.py's `rsi_veto` was removed; see that module's
docstring). TradingView still supplies the direction; this gate reads the
recent NIFTY candles and asks Claude whether the trend actually supports
taking that trade right now.

[SAFETY NOTE] optional_ai_status.py's separate AI worker is explicitly
NEVER allowed to approve a signal, place an order, or change a
deterministic decision -- that module's docstring and the dashboard's
"EXECUTION AUTHORITY: NONE" badge enforce this hard rule for that system.
THIS gate is a deliberate, explicit exception to that rule, scoped only
to this paper-only pipeline, at the user's explicit request. To keep the
exception visible rather than accidentally load-bearing everywhere:
  - every reason string this module returns is prefixed `ai_`, so it can
    never be mistaken for a deterministic gate's reason in logs or on the
    dashboard;
  - IV, liquidity and signal-freshness stay fully deterministic (see
    tv_fyers_gates.py) -- only the directional-momentum judgment is
    AI-driven;
  - on ANY failure (no API key, SDK missing, network error, timeout,
    malformed response) this gate FAILS CLOSED, exactly like every other
    veto in the ladder when its input data is missing. An AI outage must
    never silently let a trade through unchecked.

[FULL PLANNER 2026-09-25] `analyze_and_plan_trade()` below is a second,
larger step: instead of vetoing a TradingView-supplied direction, it
decides the WHOLE entry from scratch -- direction, stop-loss and target,
purely from recent candles -- for the autonomous candle-scan path
(tv_fyers_orchestrator.handle_ai_candle_scan, scheduler_setup.py's
tv_ai_scan_tick job). No TradingView alert is involved in that path at
all. The same fail-closed and `ai_`-prefixed-reason discipline applies.
Money-risk sizing (lots_for_pool/validate_position) and the deterministic
IV/liquidity gates are NOT delegated to the AI -- they still run
afterward, unchanged, on whatever contract the AI's direction resolves
to. The AI decides the trade IDEA; deterministic math still guards
whether that idea is affordable/tradeable.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional, Tuple

import structlog

from config import settings

logger = structlog.get_logger()

_SYSTEM_PROMPT = (
    "You are a strict, risk-averse options-trading gatekeeper for a "
    "paper-trading (simulation only, no real money) system. You are shown "
    "recent 5-minute candles for the NIFTY index and a proposed direction: "
    "CE means betting the index rises, PE means betting it falls. Decide "
    "ONLY whether the recent candle trend supports taking that directional "
    "bet right now. Reject if the trend is flat, choppy, exhausted, or "
    "contradicts the proposed direction. Reply with EXACTLY one JSON "
    "object and nothing else, no markdown fences, no commentary: "
    '{"decision": "PASS" or "REJECT", "reason": "<lowercase_snake_case, '
    'max 40 chars, e.g. strong_uptrend or choppy_no_clear_trend>", '
    '"confidence": <number between 0.0 and 1.0>}'
)


def _format_candles(candles: list, max_candles: int) -> str:
    rows = candles[-max_candles:] if max_candles > 0 else candles
    lines = ["timestamp,open,high,low,close,volume"]
    for row in rows:
        ts, o, h, l, c, v = row[0], row[1], row[2], row[3], row[4], row[5]
        lines.append(f"{ts},{float(o):.2f},{float(h):.2f},{float(l):.2f},{float(c):.2f},{int(v)}")
    return "\n".join(lines)


def _sanitize_reason(raw: str) -> str:
    cleaned = "".join(ch for ch in str(raw).lower() if ch.isalnum() or ch == "_")
    return cleaned[:40] or "unspecified"


async def evaluate_trend_with_ai(
    candles: list, direction: str, underlying_price: float, rsi: Optional[float] = None,
) -> Tuple[bool, str]:
    """Ask Claude whether the recent candle trend supports `direction`
    ("CE" or "PE"). Returns (ok, reason) exactly like every other veto in
    tv_fyers_gates.py's ladder. Fails closed on any error -- see module
    docstring."""
    if not settings.TV_FYERS_ANTHROPIC_API_KEY:
        return False, "ai_unavailable_no_key"
    if not candles or len(candles) < 5:
        return False, "ai_unavailable_no_candles"

    try:
        import anthropic
    except ImportError:
        logger.error("tv_fyers_ai_gate_sdk_missing")
        return False, "ai_unavailable_sdk_missing"

    candle_text = _format_candles(candles, settings.TV_FYERS_AI_MAX_CANDLES)
    rsi_line = f"Computed RSI(14): {rsi:.1f}\n" if rsi is not None else ""
    user_prompt = (
        f"Direction proposed: {direction}\n"
        f"Latest underlying price: {underlying_price:.2f}\n"
        f"{rsi_line}\n"
        f"Recent NIFTY 5-minute candles (oldest to newest):\n{candle_text}"
    )

    try:
        client = anthropic.AsyncAnthropic(api_key=settings.TV_FYERS_ANTHROPIC_API_KEY)
        response = await client.messages.create(
            model=settings.TV_FYERS_AI_MODEL,
            max_tokens=200,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            timeout=settings.TV_FYERS_AI_TIMEOUT_SEC,
        )
        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        ).strip()
    except Exception as exc:
        logger.error("tv_fyers_ai_gate_call_failed err=%s", str(exc))
        return False, "ai_unavailable_call_failed"

    try:
        parsed = json.loads(text)
        decision = parsed.get("decision")
        reason = _sanitize_reason(parsed.get("reason") or "")
        confidence = parsed.get("confidence")
        if decision not in ("PASS", "REJECT"):
            raise ValueError(f"decision must be PASS/REJECT, got {decision!r}")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not (0.0 <= float(confidence) <= 1.0):
            raise ValueError(f"confidence out of range: {confidence!r}")
    except (json.JSONDecodeError, ValueError, AttributeError, TypeError) as exc:
        logger.error("tv_fyers_ai_gate_malformed_response text=%r err=%s", text[:200], str(exc))
        return False, "ai_malformed_response"

    if decision == "REJECT":
        logger.info(
            "tv_fyers_ai_gate_reject direction=%s reason=%s confidence=%.2f",
            direction, reason, confidence,
        )
        return False, f"ai_reject_{reason}"
    logger.info(
        "tv_fyers_ai_gate_pass direction=%s reason=%s confidence=%.2f",
        direction, reason, confidence,
    )
    return True, f"ai_pass_{reason}"


@dataclass
class AiTradePlan:
    direction: str          # "CE" or "PE"
    stop_underlying: float
    target_underlying: float
    reason: str
    confidence: float


_PLANNER_SYSTEM_PROMPT = (
    "You are a strict, risk-averse options-trading planner for a "
    "paper-trading (simulation only, no real money) system. You are shown "
    "recent 5-minute candles for the NIFTY index. Decide whether there is "
    "a good options trade opportunity RIGHT NOW: buying a call (CE) if you "
    "expect the index to rise, buying a put (PE) if you expect it to fall, "
    "or no trade at all if the setup is not clear. Be conservative -- most "
    "of the time the correct answer is NO_TRADE; only propose a trade when "
    "the recent trend is genuinely clear. If you do decide to trade, you "
    "must also propose a stop-loss and a target, both expressed as NIFTY "
    "INDEX price levels (not option premium), sized off the volatility you "
    "observe in the candles. For ENTER_CE: stop_underlying must be BELOW "
    "the current price and target_underlying must be ABOVE it. For "
    "ENTER_PE: stop_underlying must be ABOVE the current price and "
    "target_underlying must be BELOW it. Reply with EXACTLY one JSON "
    "object and nothing else, no markdown fences, no commentary: "
    '{"decision": "ENTER_CE" or "ENTER_PE" or "NO_TRADE", '
    '"stop_underlying": <number, omit or null if NO_TRADE>, '
    '"target_underlying": <number, omit or null if NO_TRADE>, '
    '"reason": "<lowercase_snake_case, max 40 chars>", '
    '"confidence": <number between 0.0 and 1.0>}'
)


async def analyze_and_plan_trade(
    candles: list, underlying_price: float, rsi: Optional[float] = None,
) -> Tuple[Optional[AiTradePlan], str]:
    """Ask Claude to decide, from scratch, whether there is a trade here
    and what its stop/target should be -- the autonomous candle-scan path
    (no TradingView alert involved). Returns (plan_or_None, reason); reason
    is always one of: ai_unavailable_* (gate itself failed, fail-closed),
    ai_no_trade_<reason> (AI looked and declined), ai_malformed_response,
    or ai_plan_<reason> (a plan was produced). Mirrors
    evaluate_trend_with_ai's fail-closed contract exactly."""
    if not settings.TV_FYERS_ANTHROPIC_API_KEY:
        return None, "ai_unavailable_no_key"
    if not candles or len(candles) < 5:
        return None, "ai_unavailable_no_candles"

    try:
        import anthropic
    except ImportError:
        logger.error("tv_ai_scan_sdk_missing")
        return None, "ai_unavailable_sdk_missing"

    candle_text = _format_candles(candles, settings.TV_FYERS_AI_MAX_CANDLES)
    rsi_line = f"Computed RSI(14): {rsi:.1f}\n" if rsi is not None else ""
    user_prompt = (
        f"Latest underlying price: {underlying_price:.2f}\n"
        f"{rsi_line}\n"
        f"Recent NIFTY 5-minute candles (oldest to newest):\n{candle_text}"
    )

    try:
        client = anthropic.AsyncAnthropic(api_key=settings.TV_FYERS_ANTHROPIC_API_KEY)
        response = await client.messages.create(
            model=settings.TV_FYERS_AI_MODEL,
            max_tokens=300,
            system=_PLANNER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            timeout=settings.TV_FYERS_AI_TIMEOUT_SEC,
        )
        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        ).strip()
    except Exception as exc:
        logger.error("tv_ai_scan_call_failed err=%s", str(exc))
        return None, "ai_unavailable_call_failed"

    try:
        parsed = json.loads(text)
        decision = parsed.get("decision")
        reason = _sanitize_reason(parsed.get("reason") or "")
        confidence = parsed.get("confidence")
        if decision not in ("ENTER_CE", "ENTER_PE", "NO_TRADE"):
            raise ValueError(f"decision must be ENTER_CE/ENTER_PE/NO_TRADE, got {decision!r}")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not (0.0 <= float(confidence) <= 1.0):
            raise ValueError(f"confidence out of range: {confidence!r}")
        if decision == "NO_TRADE":
            logger.info("tv_ai_scan_no_trade reason=%s confidence=%.2f", reason, confidence)
            return None, f"ai_no_trade_{reason}"
        stop = parsed.get("stop_underlying")
        target = parsed.get("target_underlying")
        if isinstance(stop, bool) or not isinstance(stop, (int, float)):
            raise ValueError(f"stop_underlying missing/invalid: {stop!r}")
        if isinstance(target, bool) or not isinstance(target, (int, float)):
            raise ValueError(f"target_underlying missing/invalid: {target!r}")
        stop = float(stop)
        target = float(target)
        direction = "CE" if decision == "ENTER_CE" else "PE"
        if direction == "CE":
            if not (stop < underlying_price < target):
                raise ValueError("CE stop/target on the wrong side of current price")
        else:
            if not (target < underlying_price < stop):
                raise ValueError("PE stop/target on the wrong side of current price")
    except (json.JSONDecodeError, ValueError, AttributeError, TypeError) as exc:
        logger.error("tv_ai_scan_malformed_response text=%r err=%s", text[:200], str(exc))
        return None, "ai_malformed_response"

    logger.info(
        "tv_ai_scan_plan direction=%s stop=%.1f target=%.1f reason=%s confidence=%.2f",
        direction, stop, target, reason, confidence,
    )
    plan = AiTradePlan(
        direction=direction, stop_underlying=stop, target_underlying=target,
        reason=reason, confidence=confidence,
    )
    return plan, f"ai_plan_{reason}"
