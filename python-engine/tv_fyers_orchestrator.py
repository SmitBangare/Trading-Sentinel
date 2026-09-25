"""
[SMIT-FYERS-OPTIONS 2026-09-25] Orchestrator for the TradingView -> Fyers
pipeline: two entry points, deliberately split because entries are
event-driven (one TradingView webhook = one evaluation) while exits need
continuous price monitoring regardless of whether a webhook has fired
recently.

  handle_tv_entry_signal() -- called once per validated webhook arrival
      (routes_tv_fyers.py). Staleness -> candles/RSI/ATR off the
      underlying index -> contract resolution -> deterministic gates
      (freshness/IV/liquidity) -> AI candle-trend gate (tv_fyers_ai_gate.py)
      -> sizing -> paper execution.

  handle_ai_candle_scan() -- [ADDED 2026-09-25] a second, independent
      entry path: its own APScheduler interval (scheduler_setup.py's
      tv_ai_scan_tick), no TradingView alert involved at all. Claude reads
      recent candles and decides the whole trade itself -- direction,
      stop-loss and target -- see tv_fyers_ai_gate.py's "FULL PLANNER"
      docstring section. Deterministic IV/liquidity gates and sizing
      still run afterward, unchanged.

  run_tv_fyers_exit_tick() -- polling job (own APScheduler interval,
      scheduler_setup.py), manages every open TV_FYERS_* position: ports
      fno_orchestrator.py's _manage_open_positions exit ladder (structural
      stop -> trailing stop after target -> premium backstop -> time
      stop -> hard flat), adapted to track the UNDERLYING SPOT INDEX
      rather than a futures price (see module docstring's "OPEN ITEM"
      below for why). Manages positions from EITHER entry path
      identically -- it only ever looks at TV_FYERS_PAPER position rows.

[OPEN ITEM -- flagged in the approved plan, not a bug] fno_orchestrator.py
tracks NIFTY FUTURES price as its stop/trail state variable. Fyers'
futures-quote availability/liquidity for this purpose is unknown until
real Fyers credentials exist (see rollout step 7). This orchestrator
tracks the underlying SPOT INDEX instead -- universally available across
brokers -- as the safe default; revisit once real Fyers market data has
been explored.

[RESOLVED 2026-09-25] Contract resolution was a stub until real Fyers
credentials existed. Now wired against the real `optionchain()` endpoint
(fyers_client.get_option_chain), confirmed live against a real Fyers
account -- see _resolve_option_contract() below. Two real gaps that
endpoint's response has, both handled explicitly rather than guessed:
  - No IV field: implied volatility is derived via Black-Scholes
    (options_math.implied_vol, the SAME function fno_chain.py uses for
    the Zerodha-based module) from the live premium.
  - No lot size field: NIFTY's lot size is a periodically-revised NSE/
    SEBI constant, not something the API exposes -- hardcoded as
    _NIFTY_LOT_SIZE below with the revision date cited, not fetched.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytz
import structlog

import tv_fyers_positions as tvpos
from config import settings
from engine import calc_rsi_series, calc_atr
from fno_chain import RISK_FREE_RATE, years_to_expiry
from fno_costs import calc_fno_costs  # exchange-mandated STT/GST dominate; brokerage delta is small -- v1 approximation
from options_math import implied_vol
from tv_fyers_ai_gate import AiTradePlan, analyze_and_plan_trade, evaluate_trend_with_ai
from tv_fyers_executor import TvFyersExecutor
from tv_fyers_gates import GateContext as VetoContext, evaluate_tv_entry
from tv_fyers_signal import TvFyersSignal, ingest_tv_signal, mark_signal_handled

logger = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")

# NIFTY spot index symbol on Fyers (needed for RSI/ATR even before any
# option contract is resolved -- this part needs no instrument dump).
_UNDERLYING_INDEX_SYMBOL = "NSE:NIFTY50-INDEX"


async def _alert_entry(source_note: str, symbol: str, direction: str, lots: int,
                        fill: float, stop_underlying: float, target_underlying: float) -> None:
    """[REALTIME-ALERTS 2026-09-25] Best-effort Telegram page on every
    paper entry, via the one alert helper this codebase trusts not to
    fail silently (see operator_alert.py's own docstring). Never raises
    -- a failed alert must not take down the trade it's reporting on."""
    try:
        from operator_alert import notify_operator
        await notify_operator(
            f"\U0001F4C8 TV->Fyers PAPER entry ({source_note}): {direction} {symbol} "
            f"x{lots} lot(s) @ {fill:.2f}. Stop {stop_underlying:.1f} / "
            f"Target {target_underlying:.1f} (underlying). Simulation only, "
            f"no real capital at risk.",
            event="tv_fyers_entry_alert",
        )
    except Exception as exc:
        logger.error("tv_fyers_entry_alert_failed err=%s", str(exc))


async def _alert_exit(symbol: str, reason: str, entry: float, exit_px: float,
                       pnl: float, r_mult: float) -> None:
    try:
        from operator_alert import notify_operator
        emoji = "\U0001F7E2" if pnl >= 0 else "\U0001F534"
        await notify_operator(
            f"{emoji} TV->Fyers PAPER exit: {symbol} closed ({reason}). "
            f"Entry {entry:.2f} -> Exit {exit_px:.2f}. PnL {pnl:+.0f} "
            f"({r_mult:+.2f}R). Simulation only, no real capital at risk.",
            event="tv_fyers_exit_alert",
        )
    except Exception as exc:
        logger.error("tv_fyers_exit_alert_failed err=%s", str(exc))

# [LOT-SIZE 2026-09-25] NIFTY options lot size, effective from the
# January 2026 NSE/SEBI index-derivatives contract-size revision
# (lot sizes were rebased to keep notional value inside the SEBI-
# mandated Rs 10-15L band). Confirmed via public NSE/market-data
# sources at the time this was wired up -- NOT fetched dynamically,
# because Fyers' optionchain() response carries no lot-size field.
# Re-verify against NSE's circular if this is ever revised again.
_NIFTY_LOT_SIZE = 65


@dataclass
class ResolvedContract:
    """What execution needs to place (paper) orders and size a position."""
    symbol: str
    lot_size: int
    bid: float
    ask: float
    oi: int
    volume: int
    iv: Optional[float]
    quote_age_sec: float


async def _resolve_option_contract(
    fyers, direction: str, underlying_price: float, now_ist: datetime,
) -> Optional[ResolvedContract]:
    """Fetch the live Fyers options chain for NIFTY (nearest expiry by
    default) and pick the strike nearest `underlying_price` for the
    requested direction. Returns None (never fabricates a plausible-
    looking contract) if the chain is unavailable or has no candidate
    for that direction."""
    chain = await fyers.get_option_chain(_UNDERLYING_INDEX_SYMBOL, strike_count=20)
    if not chain or chain.get("s") != "ok":
        logger.warning("tv_fyers_option_chain_unavailable direction=%s", direction)
        return None

    data = chain.get("data", {})
    options = data.get("optionsChain", [])
    expiry_data = data.get("expiryData", [])
    if not options or not expiry_data:
        logger.warning("tv_fyers_option_chain_empty direction=%s", direction)
        return None

    # The index's own entry in optionsChain (option_type=="") carries
    # "fp" (forward/futures price) -- the correct Black-Scholes forward,
    # not the spot. Degrade to spot rather than fail if it's absent.
    forward = underlying_price
    for row in options:
        if row.get("option_type") == "" and row.get("fp"):
            forward = float(row["fp"])
            break

    candidates = [
        row for row in options
        if row.get("option_type") == direction and row.get("strike_price", -1) > 0
    ]
    if not candidates:
        logger.warning("tv_fyers_no_candidates_for_direction direction=%s", direction)
        return None

    best = min(candidates, key=lambda r: abs(r["strike_price"] - underlying_price))

    bid = float(best.get("bid") or 0.0)
    ask = float(best.get("ask") or 0.0)
    ltp = float(best.get("ltp") or 0.0)
    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else ltp

    # Nearest expiry's date, from the chain's own expiryData (avoids
    # re-parsing the option symbol's date encoding, which differs
    # between weekly and monthly contracts).
    expiry_epoch = int(expiry_data[0]["expiry"])
    expiry_date = datetime.fromtimestamp(expiry_epoch, tz=timezone.utc).astimezone(IST).date()

    T = years_to_expiry(expiry_date, now_ist)
    iv = None
    if mid > 0 and forward > 0 and T > 0:
        iv = implied_vol(
            mid, forward, float(best["strike_price"]), T,
            RISK_FREE_RATE, direction == "CE",
        )

    return ResolvedContract(
        symbol=best["symbol"],
        lot_size=_NIFTY_LOT_SIZE,
        bid=bid, ask=ask,
        oi=int(best.get("oi") or 0),
        volume=int(best.get("volume") or 0),
        iv=iv,
        # Fetched synchronously moments before use; Fyers' chain response
        # carries no per-quote timestamp to measure real staleness against.
        quote_age_sec=0.0,
    )


async def _fetch_candles_rsi_atr(fyers, now_ist: datetime) -> tuple:
    """Recent underlying-INDEX candles plus RSI(14)/ATR(14) off them -- no
    instrument dump needed, unlike option-contract resolution. RSI is no
    longer used to gate (see tv_fyers_ai_gate.py); it's still computed
    cheaply and passed to the AI gate as supplementary context. ATR is
    still load-bearing -- stop/target derivation below needs it."""
    from_date = (now_ist - timedelta(days=30)).strftime("%Y-%m-%d")
    to_date = now_ist.strftime("%Y-%m-%d")
    candles = await fyers.get_historical(
        _UNDERLYING_INDEX_SYMBOL, "5", from_date, to_date,
    )
    if not candles or len(candles) < settings.TV_FYERS_RSI_LENGTH + 1:
        return candles or [], None, None
    import pandas as pd
    df = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "volume"])
    rsi_series = calc_rsi_series(df["close"], length=settings.TV_FYERS_RSI_LENGTH)
    atr_series = calc_atr(df["high"], df["low"], df["close"])
    rsi = float(rsi_series.iloc[-1]) if not rsi_series.empty and rsi_series.iloc[-1] == rsi_series.iloc[-1] else None
    atr = float(atr_series.iloc[-1]) if not atr_series.empty and atr_series.iloc[-1] == atr_series.iloc[-1] else None
    return candles, rsi, atr


async def handle_tv_entry_signal(fyers, db_path: str, payload: dict) -> dict:
    """Event-driven entry path: called once per validated, forwarded
    TradingView webhook. Returns a result dict; never raises (a bad
    signal is logged and marked handled, not propagated as a 500 to
    node-gateway, which has already accepted the webhook)."""
    signal = TvFyersSignal.model_validate(payload)
    now_ist = datetime.now(IST)
    result = {"signal_id": signal.signal_id, "executed": False, "reason": ""}

    # ---- staleness --------------------------------------------------
    try:
        signal_dt = datetime.fromisoformat(signal.signal_time)
    except ValueError:
        result["reason"] = "unparseable_signal_time"
        await mark_signal_handled(db_path, signal.signal_id, result["reason"])
        return result
    signal_age_sec = (datetime.now(timezone.utc) - signal_dt.astimezone(timezone.utc)).total_seconds()
    if signal_age_sec > settings.TV_FYERS_SIGNAL_MAX_AGE_SEC:
        result["reason"] = "signal_stale"
        logger.warning(
            "tv_fyers_entry_signal_stale signal_id=%s age_sec=%.0f",
            signal.signal_id, signal_age_sec,
        )
        await mark_signal_handled(db_path, signal.signal_id, result["reason"])
        return result

    if settings.TV_FYERS_DISABLE_PAPER:
        result["reason"] = "paper_disabled"
        await mark_signal_handled(db_path, signal.signal_id, result["reason"])
        return result

    # ---- candles/RSI/ATR off the underlying index (needs no instrument dump) --
    candles, rsi, atr = await _fetch_candles_rsi_atr(fyers, now_ist)

    # ---- contract resolution (real optionchain()-based lookup) --------
    contract = await _resolve_option_contract(
        fyers, signal.direction, signal.underlying_price, now_ist,
    )
    if contract is None:
        result["reason"] = "symbol_resolution_unavailable"
        await mark_signal_handled(db_path, signal.signal_id, result["reason"])
        return result

    # ---- concurrency / daily trade caps --------------------------------
    open_now = await tvpos.open_positions(db_path, "TV_FYERS_PAPER")
    if len(open_now) >= settings.TV_FYERS_MAX_CONCURRENT:
        result["reason"] = "concurrency_cap"
        await mark_signal_handled(db_path, signal.signal_id, result["reason"])
        return result

    # ---- double-check gates (RSI/IV/liquidity veto over the TV alert) --
    ctx = VetoContext(
        direction=signal.direction,
        rsi=rsi, iv=contract.iv,
        oi=contract.oi, volume=contract.volume,
        bid=contract.bid, ask=contract.ask,
        quote_age_sec=contract.quote_age_sec,
        signal_age_sec=signal_age_sec,
        max_signal_age_sec=float(settings.TV_FYERS_SIGNAL_MAX_AGE_SEC),
    )
    ok, reject_reason = evaluate_tv_entry(ctx)
    if not ok:
        result["reason"] = reject_reason
        logger.info(
            "tv_fyers_entry_rejected signal_id=%s reason=%s", signal.signal_id, reject_reason,
        )
        await mark_signal_handled(db_path, signal.signal_id, result["reason"])
        return result

    # ---- AI candle-trend gate (replaces the old RSI-threshold veto; see
    # tv_fyers_ai_gate.py's module docstring for the safety posture) ------
    ai_ok, ai_reason = await evaluate_trend_with_ai(
        candles, signal.direction, signal.underlying_price, rsi=rsi,
    )
    if not ai_ok:
        result["reason"] = ai_reason
        logger.info(
            "tv_fyers_entry_rejected signal_id=%s reason=%s", signal.signal_id, ai_reason,
        )
        await mark_signal_handled(db_path, signal.signal_id, result["reason"])
        return result

    # ---- stop/target derivation (TradingView sends direction only; the
    # bot derives and owns every exit itself, per the approved plan) ----
    if atr is None or atr <= 0:
        result["reason"] = "atr_unavailable"
        await mark_signal_handled(db_path, signal.signal_id, result["reason"])
        return result
    entry_u = signal.underlying_price
    stop_dist = settings.TV_FYERS_TRAIL_ATR_MULT * atr
    if signal.direction == "CE":
        stop_underlying = entry_u - stop_dist
        target_underlying = entry_u + settings.TV_FYERS_TARGET_R * stop_dist
    else:
        stop_underlying = entry_u + stop_dist
        target_underlying = entry_u - settings.TV_FYERS_TARGET_R * stop_dist

    # ---- sizing (reuse the broker-agnostic FNO risk math) --------------
    from fno_risk import lots_for_pool, validate_position
    from fno_models import Leg, OptionType
    pool = float(settings.TV_FYERS_PAPER_BANKROLL)
    lots = lots_for_pool(
        pool, contract.ask, contract.lot_size,
        settings.TV_FYERS_STOP_PREMIUM_PCT, settings.TV_FYERS_MAX_RISK_PCT,
        settings.TV_FYERS_MAX_LOTS,
    )
    if lots < 1:
        result["reason"] = "pool_below_min_viable"
        await mark_signal_handled(db_path, signal.signal_id, result["reason"])
        return result

    opt_type = OptionType.CE if signal.direction == "CE" else OptionType.PE
    legs = [Leg(opt_type=opt_type, strike=0.0, quantity=lots, premium=contract.ask)]
    ok_ml, reject_ml, max_loss = validate_position(legs, contract.lot_size)
    if not ok_ml:
        result["reason"] = reject_ml
        await mark_signal_handled(db_path, signal.signal_id, result["reason"])
        return result

    # ---- execute (paper unless TV_FYERS_LIVE_TRADING, itself False by
    # default -- see config.py's triple-disarm block) --------------------
    executor = TvFyersExecutor(
        fyers, paper_mode=not settings.TV_FYERS_LIVE_TRADING, source_tag="TV_FYERS_PAPER",
    )
    qty = lots * contract.lot_size
    exec_result = await executor.execute_entry(contract.symbol, qty, contract.ask)
    if exec_result["status"] not in ("paper", "filled"):
        result["reason"] = f"entry_{exec_result['status']}"
        await mark_signal_handled(db_path, signal.signal_id, result["reason"])
        return result

    fill = float(exec_result["fill_price"])
    premium_stop = round((1.0 - settings.TV_FYERS_STOP_PREMIUM_PCT) * fill, 2)
    await tvpos.insert_position(
        db_path,
        source="TV_FYERS_PAPER", signal_id=signal.signal_id,
        symbol=contract.symbol, direction=signal.direction, qty=qty,
        entry_time=now_ist.isoformat(), entry_premium=fill,
        entry_underlying=entry_u, stop_underlying=stop_underlying,
        target_underlying=target_underlying, premium_stop=premium_stop,
        best_underlying=entry_u, atr_at_entry=atr,
        entry_order_id=exec_result.get("order_id"),
    )
    result["executed"] = True
    result["reason"] = ""
    logger.info(
        "tv_fyers_entry_submitted signal_id=%s symbol=%s direction=%s lots=%d "
        "fill=%.2f stop_u=%.1f target_u=%.1f",
        signal.signal_id, contract.symbol, signal.direction, lots, fill,
        stop_underlying, target_underlying,
    )
    await _alert_entry("TradingView alert", contract.symbol, signal.direction, lots,
                        fill, stop_underlying, target_underlying)
    await mark_signal_handled(db_path, signal.signal_id, "executed")
    return result


# ---------------------------------------------------------------------------
# autonomous AI candle scan (no TradingView alert -- Claude decides the
# whole trade itself: direction, stop-loss and target, straight from
# recent candles). See tv_fyers_ai_gate.py's "FULL PLANNER" docstring
# section for the safety posture. Deliberately NOT refactored to share
# code with handle_tv_entry_signal() above -- this pipeline already has a
# documented precedent (tv_fyers_positions.py's module docstring) for
# preferring a duplicated-but-simple path over a shared abstraction when
# the two paths' safety-relevant details (a TradingView-supplied
# direction to double-check vs. an AI-originated direction to size and
# execute) are different enough that sharing code would risk silently
# blurring which trigger source is doing what.
# ---------------------------------------------------------------------------

async def handle_ai_candle_scan(fyers, db_path: str, now_ist: datetime) -> dict:
    """One scheduler tick of the autonomous scan: fetch recent candles,
    ask Claude whether there's a trade, and if so run it through the same
    deterministic IV/liquidity gates and sizing math as every other entry
    here before ever touching the paper executor. Every tick (including a
    NO_TRADE decision) is persisted to tv_fyers_signals so the dashboard
    shows what the AI saw and decided, not just the trades it took."""
    result = {"signal_id": None, "executed": False, "reason": ""}

    if settings.TV_FYERS_DISABLE_PAPER:
        result["reason"] = "paper_disabled"
        return result

    candles, rsi, atr = await _fetch_candles_rsi_atr(fyers, now_ist)
    if not candles:
        result["reason"] = "ai_unavailable_no_candles"
        return result
    underlying_price = float(candles[-1][4])  # last candle's close

    plan, plan_reason = await analyze_and_plan_trade(candles, underlying_price, rsi=rsi)

    signal_id = f"ai-scan-{now_ist.strftime('%Y%m%dT%H%M%S')}"
    result["signal_id"] = signal_id
    await ingest_tv_signal(db_path, {
        "signal_id": signal_id, "symbol": "NIFTY",
        "direction": plan.direction if plan else "CE",  # schema requires CE/PE even for a no-trade tick
        "underlying_price": underlying_price,
        "strategy": "ai_candle_scan",
        "signal_time": now_ist.isoformat(),
    })

    if plan is None:
        result["reason"] = plan_reason
        await mark_signal_handled(db_path, signal_id, plan_reason)
        return result

    open_now = await tvpos.open_positions(db_path, "TV_FYERS_PAPER")
    if len(open_now) >= settings.TV_FYERS_MAX_CONCURRENT:
        result["reason"] = "concurrency_cap"
        await mark_signal_handled(db_path, signal_id, result["reason"])
        return result

    contract = await _resolve_option_contract(fyers, plan.direction, underlying_price, now_ist)
    if contract is None:
        result["reason"] = "symbol_resolution_unavailable"
        await mark_signal_handled(db_path, signal_id, result["reason"])
        return result

    ctx = VetoContext(
        direction=plan.direction, rsi=None, iv=contract.iv,
        oi=contract.oi, volume=contract.volume,
        bid=contract.bid, ask=contract.ask,
        quote_age_sec=contract.quote_age_sec,
        signal_age_sec=0.0, max_signal_age_sec=float(settings.TV_FYERS_SIGNAL_MAX_AGE_SEC),
    )
    ok, reject_reason = evaluate_tv_entry(ctx)
    if not ok:
        result["reason"] = reject_reason
        logger.info("tv_ai_scan_entry_rejected signal_id=%s reason=%s", signal_id, reject_reason)
        await mark_signal_handled(db_path, signal_id, result["reason"])
        return result

    from fno_risk import lots_for_pool, validate_position
    from fno_models import Leg, OptionType
    pool = float(settings.TV_FYERS_PAPER_BANKROLL)
    lots = lots_for_pool(
        pool, contract.ask, contract.lot_size,
        settings.TV_FYERS_STOP_PREMIUM_PCT, settings.TV_FYERS_MAX_RISK_PCT,
        settings.TV_FYERS_MAX_LOTS,
    )
    if lots < 1:
        result["reason"] = "pool_below_min_viable"
        await mark_signal_handled(db_path, signal_id, result["reason"])
        return result

    opt_type = OptionType.CE if plan.direction == "CE" else OptionType.PE
    legs = [Leg(opt_type=opt_type, strike=0.0, quantity=lots, premium=contract.ask)]
    ok_ml, reject_ml, max_loss = validate_position(legs, contract.lot_size)
    if not ok_ml:
        result["reason"] = reject_ml
        await mark_signal_handled(db_path, signal_id, result["reason"])
        return result

    executor = TvFyersExecutor(
        fyers, paper_mode=not settings.TV_FYERS_LIVE_TRADING, source_tag="TV_FYERS_PAPER",
    )
    qty = lots * contract.lot_size
    exec_result = await executor.execute_entry(contract.symbol, qty, contract.ask)
    if exec_result["status"] not in ("paper", "filled"):
        result["reason"] = f"entry_{exec_result['status']}"
        await mark_signal_handled(db_path, signal_id, result["reason"])
        return result

    fill = float(exec_result["fill_price"])
    premium_stop = round((1.0 - settings.TV_FYERS_STOP_PREMIUM_PCT) * fill, 2)
    await tvpos.insert_position(
        db_path,
        source="TV_FYERS_PAPER", signal_id=signal_id,
        symbol=contract.symbol, direction=plan.direction, qty=qty,
        entry_time=now_ist.isoformat(), entry_premium=fill,
        entry_underlying=underlying_price, stop_underlying=plan.stop_underlying,
        target_underlying=plan.target_underlying, premium_stop=premium_stop,
        best_underlying=underlying_price, atr_at_entry=atr or 0.0,
        entry_order_id=exec_result.get("order_id"),
    )
    result["executed"] = True
    result["reason"] = ""
    logger.info(
        "tv_ai_scan_entry_submitted signal_id=%s symbol=%s direction=%s lots=%d "
        "fill=%.2f stop_u=%.1f target_u=%.1f ai_reason=%s ai_confidence=%.2f",
        signal_id, contract.symbol, plan.direction, lots, fill,
        plan.stop_underlying, plan.target_underlying, plan.reason, plan.confidence,
    )
    await _alert_entry(f"AI scan, {plan.reason}, {plan.confidence:.0%} confidence",
                        contract.symbol, plan.direction, lots, fill,
                        plan.stop_underlying, plan.target_underlying)
    await mark_signal_handled(db_path, signal_id, "executed")
    return result


# ---------------------------------------------------------------------------
# exit management (polling, ported from fno_orchestrator._manage_open_positions)
# ---------------------------------------------------------------------------

def _now_min(now_ist: datetime) -> int:
    return now_ist.hour * 60 + now_ist.minute


async def run_tv_fyers_exit_tick(fyers, db_path: str, now_ist: Optional[datetime] = None) -> dict:
    """Manage every open TV_FYERS_PAPER position: structural stop ->
    trailing stop (armed after target) -> premium backstop -> time stop
    -> hard flat. Ported from fno_orchestrator.py's _manage_open_positions;
    tracks the underlying SPOT INDEX rather than futures (see module
    docstring's OPEN ITEM)."""
    now_ist = now_ist or datetime.now(IST)
    closed = []
    positions = await tvpos.open_positions(db_path, "TV_FYERS_PAPER")
    if not positions:
        return {"exits": closed}

    spot_quote = await fyers.get_quote([_UNDERLYING_INDEX_SYMBOL])
    spot = spot_quote.get(_UNDERLYING_INDEX_SYMBOL) or {}
    spot_price = float(spot.get("lp") or 0.0) or None

    hard_flat = _now_min(now_ist) >= (15 * 60 + 10)

    for p in positions:
        # One quote per held contract for the exit price basis.
        quote_map = await fyers.get_quote([p.symbol])
        q = quote_map.get(p.symbol) or {}
        bid = float(q.get("bid") or 0.0)
        ltp = float(q.get("lp") or 0.0)
        exit_px_basis = bid if bid > 0 else ltp

        exit_reason = ""
        long_view = p.direction == "CE"

        if hard_flat:
            exit_reason = "hard_flat_1510"
        elif spot_price is not None and spot_price > 0:
            best = p.best_underlying
            best = max(best, spot_price) if long_view else min(best, spot_price)
            trail_active = bool(p.trail_active)
            trail_stop = p.trail_stop_underlying

            target_hit = (
                spot_price >= p.target_underlying if long_view
                else spot_price <= p.target_underlying
            )
            if target_hit and not trail_active:
                trail_active = True
                logger.info(
                    "tv_fyers_trail_armed id=%d symbol=%s spot=%.1f target=%.1f",
                    p.id, p.symbol, spot_price, p.target_underlying,
                )
            if trail_active:
                dist = settings.TV_FYERS_TRAIL_ATR_MULT * (p.atr_at_entry or 0.0)
                new_trail = best - dist if long_view else best + dist
                trail_stop = new_trail if trail_stop is None else (
                    max(trail_stop, new_trail) if long_view else min(trail_stop, new_trail)
                )

            stopped = (
                spot_price <= p.stop_underlying if long_view
                else spot_price >= p.stop_underlying
            )
            trailed = (
                trail_active and trail_stop is not None
                and (spot_price <= trail_stop if long_view else spot_price >= trail_stop)
            )
            premium_stopped = exit_px_basis > 0 and exit_px_basis <= p.premium_stop

            timed_out = False
            if not trail_active:
                try:
                    entry_dt = datetime.fromisoformat(p.entry_time)
                    if entry_dt.tzinfo is None:
                        entry_dt = IST.localize(entry_dt)
                    age_min = (now_ist - entry_dt).total_seconds() / 60.0
                except (ValueError, TypeError):
                    logger.warning(
                        "tv_fyers_time_stop_age_parse_failed id=%d entry_time=%r",
                        p.id, p.entry_time,
                    )
                    age_min = 0.0
                if age_min >= settings.TV_FYERS_TIME_STOP_MIN:
                    r_points = abs(p.entry_underlying - p.stop_underlying)
                    progress = (
                        spot_price - p.entry_underlying if long_view
                        else p.entry_underlying - spot_price
                    )
                    if progress < 0.5 * r_points:
                        # Same profit-aware deferral fno_orchestrator.py applies:
                        # never cut a position that is currently profitable on
                        # premium just because the underlying looks stalled.
                        premium_pnl = exit_px_basis - p.entry_premium
                        if not (exit_px_basis > 0 and premium_pnl > 0):
                            timed_out = True

            if stopped:
                exit_reason = "underlying_stop"
            elif trailed:
                exit_reason = "trail_stop"
            elif premium_stopped:
                exit_reason = "premium_backstop"
            elif timed_out:
                exit_reason = "time_stop"

            if not exit_reason:
                await tvpos.update_trail(db_path, p.id, 1 if trail_active else 0, trail_stop, best)
        else:
            if exit_px_basis > 0 and exit_px_basis <= p.premium_stop:
                exit_reason = "premium_backstop"

        if not exit_reason:
            continue

        if exit_px_basis <= 0:
            logger.critical(
                "tv_fyers_exit_no_quote id=%d symbol=%s reason=%s -- cannot price "
                "the exit; position will carry (operator should check manually)",
                p.id, p.symbol, exit_reason,
            )
            try:
                from operator_alert import notify_operator
                await notify_operator(
                    f"TV->Fyers paper position id={p.id} symbol={p.symbol} has no "
                    f"tradeable quote for its {exit_reason} exit. Paper-only, no "
                    f"real capital at risk, but check the position manually.",
                    event="tv_fyers_exit_no_quote",
                )
            except Exception as notify_exc:
                logger.error("tv_fyers_operator_page_failed id=%d err=%s", p.id, notify_exc)
            continue

        if not await tvpos.claim_exit(db_path, p.id):
            continue

        executor = TvFyersExecutor(
            fyers, paper_mode=not settings.TV_FYERS_LIVE_TRADING, source_tag="TV_FYERS_PAPER",
        )
        try:
            exec_result = await executor.execute_exit(
                p.symbol, p.qty, exit_px_basis, tick_size=0.05,
                hard_flat=exit_reason.startswith("hard_flat"),
            )
        except Exception:
            logger.exception("tv_fyers_exit_dispatch_ambiguous id=%s; reconcile before retry", p.id)
            await tvpos.unclaim_exit(db_path, p.id)
            continue
        if exec_result["status"] not in ("paper", "filled"):
            logger.warning(
                "tv_fyers_exit_not_filled id=%d symbol=%s status=%s",
                p.id, p.symbol, exec_result["status"],
            )
            await tvpos.unclaim_exit(db_path, p.id)
            continue

        fill = float(exec_result["fill_price"])
        gross = (fill - p.entry_premium) * p.qty
        costs = calc_fno_costs(p.entry_premium, fill, p.qty)
        pnl = gross - costs
        risk_rupees = p.entry_premium * settings.TV_FYERS_STOP_PREMIUM_PCT * p.qty
        r_mult = pnl / risk_rupees if risk_rupees > 0 else 0.0

        await tvpos.settle_position_close(
            db_path, p.id,
            exit_time_ist=now_ist, exit_premium=fill,
            exit_underlying=spot_price or 0.0, exit_reason=exit_reason,
            gross_pnl=gross, costs=costs, pnl=pnl, r_multiple=r_mult,
            exit_order_id=exec_result.get("order_id"),
        )
        logger.info(
            "tv_fyers_position_closed id=%d symbol=%s reason=%s entry=%.2f exit=%.2f "
            "pnl=%.0f r=%.2f",
            p.id, p.symbol, exit_reason, p.entry_premium, fill, pnl, r_mult,
        )
        await _alert_exit(p.symbol, exit_reason, p.entry_premium, fill, pnl, r_mult)
        closed.append({
            "symbol": p.symbol, "reason": exit_reason, "entry": p.entry_premium,
            "exit": fill, "pnl": pnl, "r": r_mult,
        })

    return {"exits": closed}
