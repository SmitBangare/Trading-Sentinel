"""
[SMIT-FYERS-OPTIONS-BACKTEST 2026-09-25] Mechanics-only backtest for the
TradingView -> Fyers pipeline: real Fyers NIFTY spot candle history, run
through the SAME deterministic gates, risk sizing and exit-management
math the live bot uses. Deliberately does NOT replay the AI gate/planner
(tv_fyers_ai_gate.py) -- the user's own explicit choice, since Claude API
calls are a real, paid, per-call cost with no free tier; this backtest
costs nothing to run, matching the live pipeline's own no-key default.

READ THIS BEFORE TRUSTING A NUMBER OUT OF THIS MODULE
---------------------------------------------------------------------
- ENTRY TRIGGER: a faithful line-by-line port of
  pine-scripts/tv_fyers_orb_alert.pine (opening-range breakout + EMA
  21/50 trend filter, session 09:15-15:25 IST). This is NOT a replay of
  your actual TradingView alert history -- TradingView keeps no such
  log, and Pine Script logic is whatever you configure. It is the one
  concrete, checked-in starter strategy, used as a stand-in.
- OPTION PRICING IS MODELLED, NOT HISTORICAL: no historical NIFTY
  options-chain data source exists anywhere in this codebase or via
  Fyers' API (confirmed via backtest_lab.py's own FnoUnavailableAdapter,
  which is disabled for exactly this reason). Premiums here come from
  Black-76 (options_math.black76_price) using a strike near the
  underlying at signal time, an expiry assumed to be the next Thursday
  (NIFTY's historical weekly expiry day), and a volatility assumed equal
  to REALISED volatility (trailing 20-trading-day close-to-close stdev,
  annualised) computed from the REAL underlying candle history -- the
  one input that can be honestly derived from real data rather than
  guessed.
- LIQUIDITY (OI/volume) CANNOT BE BACKTESTED: no historical options
  liquidity data exists either. The liquidity gate still RUNS (same
  tv_fyers_gates.evaluate_tv_entry ladder as live), but OI/volume inputs
  are fixed, comfortably-passing constants -- so oi_too_thin/
  volume_too_thin will never appear in backtest results. This is
  disclosed rather than faked as a real check. The spread/stale-quote
  parts of the liquidity gate DO use the modelled bid/ask, so
  spread_too_wide can still fire if the modelled premium is tiny.
- EXITS use the exact same ladder run_tv_fyers_exit_tick runs
  (structural stop -> trailing after target -> premium backstop -> time
  stop -> hard flat at 15:10), walked forward candle-by-candle using
  each candle's close as the exit price basis (no historical bid/ask to
  do better) and the modelled premium (re-priced each step for time
  decay) for the premium-backstop check.
- COSTS use the same fno_costs.calc_fno_costs the live pipeline charges.
- Position sizing/risk uses the same fno_risk.lots_for_pool /
  validate_position the live pipeline uses, against
  TV_FYERS_PAPER_BANKROLL and the same risk-percent settings.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import structlog

from config import settings
from engine import calc_atr, calc_rsi_series
from fno_costs import calc_fno_costs
from fno_chain import RISK_FREE_RATE
from fno_models import Leg, OptionType
from fno_risk import lots_for_pool, validate_position
from options_math import black76_price
from tv_fyers_gates import GateContext, evaluate_tv_entry

logger = structlog.get_logger()

_UNDERLYING_INDEX_SYMBOL = "NSE:NIFTY50-INDEX"
_NIFTY_LOT_SIZE = 65  # matches tv_fyers_orchestrator.py's live constant
_STRIKE_STEP = 50.0
_MODELLED_SPREAD_PCT = 0.01  # +-0.5% each side; comfortably inside TV_FYERS_MAX_SPREAD_PCT
_MODELLED_OI = 50_000
_MODELLED_VOLUME = 20_000
_REALIZED_VOL_WINDOW_DAYS = 20
_FYERS_HISTORY_CHUNK_DAYS = 90  # Fyers intraday history calls are capped per request
_HISTORY_CACHE_FILENAME = "nifty_5min_history_cache.json"


def _cache_path(db_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), _HISTORY_CACHE_FILENAME)


def load_cached_history(db_path: str) -> Optional[list]:
    path = _cache_path(db_path)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return payload.get("candles")


def save_cached_history(db_path: str, candles: list) -> None:
    path = _cache_path(db_path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"cached_at": datetime.now(timezone.utc).isoformat(), "candles": candles}, f)
    logger.info("tv_fyers_backtest_history_cached path=%s rows=%d", path, len(candles))


async def fetch_nifty_history(fyers, days_back: int = 365) -> list:
    """Pull `days_back` days of 5-min NIFTY spot candles from Fyers,
    chunked into <=90-day windows (Fyers' intraday history endpoint
    rejects a single request spanning a full year), concatenated and
    de-duplicated by timestamp. Real network call -- run this once,
    cache the result, then replay run_backtest() against the cache as
    many times as you like for free."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days_back)
    all_candles: list = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=_FYERS_HISTORY_CHUNK_DAYS), end)
        candles = await fyers.get_historical(
            _UNDERLYING_INDEX_SYMBOL, "5",
            cursor.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d"),
        )
        logger.info(
            "tv_fyers_backtest_history_chunk_fetched from=%s to=%s rows=%d",
            cursor.date(), chunk_end.date(), len(candles),
        )
        all_candles.extend(candles)
        cursor = chunk_end + timedelta(days=1)
    seen = set()
    deduped = []
    for row in sorted(all_candles, key=lambda r: r[0]):
        if row[0] in seen:
            continue
        seen.add(row[0])
        deduped.append(row)
    return deduped


def _candles_to_frame(candles: list) -> pd.DataFrame:
    df = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "volume"])
    df["dt_utc"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    df["dt_ist"] = df["dt_utc"].dt.tz_convert("Asia/Kolkata")
    df["date_ist"] = df["dt_ist"].dt.date
    df["minute_of_day"] = df["dt_ist"].dt.hour * 60 + df["dt_ist"].dt.minute
    df = df.sort_values("ts").reset_index(drop=True)
    return df


@dataclass
class OrbSignal:
    ts: int
    dt_ist: datetime
    direction: str  # "CE" or "PE"
    underlying_price: float
    rsi: Optional[float]
    atr: Optional[float]


def generate_orb_signals(
    df: pd.DataFrame, *, or_minutes: int = 15, or_buffer_atr: float = 0.25,
    atr_length: int = 14, ema_fast_len: int = 21, ema_slow_len: int = 50,
) -> list[OrbSignal]:
    """Python port of pine-scripts/tv_fyers_orb_alert.pine, bar for bar:
    opening-range breakout (fresh cross only, matching the Pine script's
    close > level and close[1] <= level condition) with an EMA 21/50
    trend filter, session 09:15-15:25 IST."""
    df = df.copy()
    # calc_atr is fixed at Wilder ATR(14) internally (no length param);
    # atr_length is kept as a documented parameter for parity with the
    # Pine Script input, but this port always gets ATR(14) like the live
    # bot does via the same shared engine.calc_atr function.
    df["atr"] = calc_atr(df["high"], df["low"], df["close"])
    df["rsi"] = calc_rsi_series(df["close"], length=settings.TV_FYERS_RSI_LENGTH)
    df["ema_fast"] = df["close"].ewm(span=ema_fast_len, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=ema_slow_len, adjust=False).mean()

    session_start_min = 9 * 60 + 15
    or_end_min = session_start_min + or_minutes
    session_end_min = 15 * 60 + 25

    signals: list[OrbSignal] = []
    for trading_day, day_df in df.groupby("date_ist"):
        day_df = day_df.sort_values("ts")
        or_rows = day_df[
            (day_df["minute_of_day"] >= session_start_min)
            & (day_df["minute_of_day"] < or_end_min)
        ]
        if or_rows.empty:
            continue
        or_high = float(or_rows["high"].max())
        or_low = float(or_rows["low"].min())

        after_rows = day_df[
            (day_df["minute_of_day"] >= or_end_min)
            & (day_df["minute_of_day"] <= session_end_min)
        ]
        prev_close: Optional[float] = None
        for _, row in after_rows.iterrows():
            atr_val = row["atr"]
            close = float(row["close"])
            if prev_close is None or pd.isna(atr_val):
                prev_close = close
                continue
            long_level = or_high + or_buffer_atr * atr_val
            short_level = or_low - or_buffer_atr * atr_val
            long_break = close > long_level and prev_close <= long_level
            short_break = close < short_level and prev_close >= short_level
            ema_fast, ema_slow = row["ema_fast"], row["ema_slow"]
            long_signal = long_break and ema_fast > ema_slow
            short_signal = short_break and ema_fast < ema_slow
            if long_signal or short_signal:
                signals.append(OrbSignal(
                    ts=int(row["ts"]), dt_ist=row["dt_ist"],
                    direction="CE" if long_signal else "PE",
                    underlying_price=close,
                    rsi=None if pd.isna(row["rsi"]) else float(row["rsi"]),
                    atr=None if pd.isna(atr_val) else float(atr_val),
                ))
            prev_close = close
    return signals


def _next_weekly_expiry(signal_date: date) -> date:
    """NIFTY's historical weekly options expiry day was Thursday. Rolls
    to the SAME Thursday if the signal falls on or before it, otherwise
    the following week's."""
    days_ahead = (3 - signal_date.weekday()) % 7  # Monday=0 ... Thursday=3
    return signal_date + timedelta(days=days_ahead)


def _years_to_expiry(signal_dt_ist: datetime) -> float:
    expiry = _next_weekly_expiry(signal_dt_ist.date())
    expiry_dt = signal_dt_ist.replace(
        year=expiry.year, month=expiry.month, day=expiry.day,
        hour=15, minute=30, second=0, microsecond=0,
    )
    seconds = max((expiry_dt - signal_dt_ist).total_seconds(), 3600.0)  # floor: 1hr
    return seconds / (365.0 * 24 * 3600)


def _realized_vol_series(df: pd.DataFrame, window_days: int = _REALIZED_VOL_WINDOW_DAYS) -> pd.Series:
    """Annualised close-to-close realised volatility off DAILY closes
    (last candle of each trading day), trailing `window_days`, forward-
    filled back onto the 5-min index it's looked up against."""
    daily_close = df.groupby("date_ist")["close"].last()
    log_ret = pd.Series(daily_close).apply(lambda x: x).pipe(
        lambda s: (s / s.shift(1)).apply(lambda r: math.log(r) if r and r > 0 else float("nan"))
    )
    realized = log_ret.rolling(window_days).std() * math.sqrt(252)
    return realized


@dataclass
class BacktestTrade:
    signal_ts: int
    entry_dt_ist: str
    exit_dt_ist: Optional[str]
    direction: str
    strike: float
    entry_underlying: float
    entry_premium: float
    exit_underlying: Optional[float]
    exit_premium: Optional[float]
    exit_reason: Optional[str]
    qty: int
    gross_pnl: Optional[float]
    costs: Optional[float]
    pnl: Optional[float]
    r_multiple: Optional[float]


@dataclass
class BacktestResult:
    signals_generated: int
    entries_taken: int
    rejections_by_reason: dict = field(default_factory=dict)
    trades: list = field(default_factory=list)
    total_pnl: float = 0.0
    win_count: int = 0
    loss_count: int = 0
    win_rate: Optional[float] = None
    avg_r_multiple: Optional[float] = None
    max_drawdown: float = 0.0


def run_backtest(candles: list, *, starting_bankroll: Optional[float] = None) -> BacktestResult:
    """Deterministic, no-network, no-AI replay. See module docstring for
    exactly what is real data vs. modelled."""
    pool = float(starting_bankroll if starting_bankroll is not None else settings.TV_FYERS_PAPER_BANKROLL)
    df = _candles_to_frame(candles)
    signals = generate_orb_signals(df)
    realized_vol = _realized_vol_series(df)

    result = BacktestResult(signals_generated=len(signals), entries_taken=0)
    open_positions: list[dict] = []

    def _reject(reason: str) -> None:
        result.rejections_by_reason[reason] = result.rejections_by_reason.get(reason, 0) + 1

    for sig in signals:
        # Manage/close any open positions against candles up to this signal
        # before considering a new entry (single-pass walk-forward).
        open_positions = _advance_exits(df, open_positions, up_to_ts=sig.ts, result=result)

        if len(open_positions) >= settings.TV_FYERS_MAX_CONCURRENT:
            _reject("concurrency_cap")
            continue

        vol = realized_vol.get(sig.dt_ist.date())
        if vol is None or pd.isna(vol) or vol <= 0:
            _reject("iv_unavailable")  # no realised-vol estimate yet (early in the window)
            continue

        strike = round(sig.underlying_price / _STRIKE_STEP) * _STRIKE_STEP
        T = _years_to_expiry(sig.dt_ist)
        is_call = sig.direction == "CE"
        mid = black76_price(sig.underlying_price, strike, T, float(vol), RISK_FREE_RATE, is_call)
        if mid <= 0:
            _reject("invalid_mid_price")
            continue
        half_spread = mid * (_MODELLED_SPREAD_PCT / 2.0)
        bid = max(mid - half_spread, 0.01)
        ask = mid + half_spread

        ctx = GateContext(
            direction=sig.direction, rsi=sig.rsi, iv=float(vol),
            oi=_MODELLED_OI, volume=_MODELLED_VOLUME,
            bid=bid, ask=ask, quote_age_sec=0.0,
            signal_age_sec=0.0, max_signal_age_sec=float(settings.TV_FYERS_SIGNAL_MAX_AGE_SEC),
        )
        ok, reason = evaluate_tv_entry(ctx)
        if not ok:
            _reject(reason)
            continue

        if sig.atr is None or sig.atr <= 0:
            _reject("atr_unavailable")
            continue
        stop_dist = settings.TV_FYERS_TRAIL_ATR_MULT * sig.atr
        if is_call:
            stop_u = sig.underlying_price - stop_dist
            target_u = sig.underlying_price + settings.TV_FYERS_TARGET_R * stop_dist
        else:
            stop_u = sig.underlying_price + stop_dist
            target_u = sig.underlying_price - settings.TV_FYERS_TARGET_R * stop_dist

        lots = lots_for_pool(
            pool, ask, _NIFTY_LOT_SIZE,
            settings.TV_FYERS_STOP_PREMIUM_PCT, settings.TV_FYERS_MAX_RISK_PCT,
            settings.TV_FYERS_MAX_LOTS,
        )
        if lots < 1:
            _reject("pool_below_min_viable")
            continue
        opt_type = OptionType.CE if is_call else OptionType.PE
        legs = [Leg(opt_type=opt_type, strike=strike, quantity=lots, premium=ask)]
        ok_ml, reject_ml, _ = validate_position(legs, _NIFTY_LOT_SIZE)
        if not ok_ml:
            _reject(reject_ml)
            continue

        qty = lots * _NIFTY_LOT_SIZE
        premium_stop = round((1.0 - settings.TV_FYERS_STOP_PREMIUM_PCT) * ask, 2)
        open_positions.append({
            "signal_ts": sig.ts, "entry_dt_ist": sig.dt_ist, "direction": sig.direction,
            "strike": strike, "entry_underlying": sig.underlying_price, "entry_premium": ask,
            "stop_underlying": stop_u, "target_underlying": target_u, "premium_stop": premium_stop,
            "qty": qty, "atr_at_entry": sig.atr, "trail_active": False, "trail_stop": None,
            "best_underlying": sig.underlying_price, "vol": float(vol),
        })
        result.entries_taken += 1

    # Force-close anything still open at the end of the fetched window.
    open_positions = _advance_exits(df, open_positions, up_to_ts=None, result=result, force_close=True)

    pnls = [t.pnl for t in result.trades if t.pnl is not None]
    result.total_pnl = sum(pnls)
    result.win_count = sum(1 for p in pnls if p > 0)
    result.loss_count = sum(1 for p in pnls if p <= 0)
    if pnls:
        result.win_rate = result.win_count / len(pnls)
    r_mults = [t.r_multiple for t in result.trades if t.r_multiple is not None]
    if r_mults:
        result.avg_r_multiple = sum(r_mults) / len(r_mults)
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        running += p
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)
    result.max_drawdown = max_dd
    return result


def _advance_exits(
    df: pd.DataFrame, open_positions: list[dict], *, up_to_ts: Optional[int],
    result: BacktestResult, force_close: bool = False,
) -> list[dict]:
    """Walk each open position forward through candles up to (but not
    including) up_to_ts, applying the same exit ladder
    run_tv_fyers_exit_tick uses live: structural stop -> trailing after
    target -> premium backstop -> time stop -> hard flat at 15:10.
    force_close=True closes everything unconditionally at the last
    available candle (end of the backtest window)."""
    if not open_positions:
        return []
    still_open = []
    for pos in open_positions:
        window = df[df["ts"] > pos["signal_ts"]]
        if up_to_ts is not None:
            window = window[window["ts"] <= up_to_ts]
        closed = False
        for _, row in window.iterrows():
            spot = float(row["close"])
            minute_of_day = int(row["minute_of_day"])
            long_view = pos["direction"] == "CE"
            hard_flat = minute_of_day >= (15 * 60 + 10)

            best = pos["best_underlying"]
            best = max(best, spot) if long_view else min(best, spot)
            pos["best_underlying"] = best
            target_hit = spot >= pos["target_underlying"] if long_view else spot <= pos["target_underlying"]
            if target_hit and not pos["trail_active"]:
                pos["trail_active"] = True
            if pos["trail_active"]:
                dist = settings.TV_FYERS_TRAIL_ATR_MULT * pos["atr_at_entry"]
                new_trail = best - dist if long_view else best + dist
                pos["trail_stop"] = new_trail if pos["trail_stop"] is None else (
                    max(pos["trail_stop"], new_trail) if long_view else min(pos["trail_stop"], new_trail)
                )

            stopped = spot <= pos["stop_underlying"] if long_view else spot >= pos["stop_underlying"]
            trailed = (
                pos["trail_active"] and pos["trail_stop"] is not None
                and (spot <= pos["trail_stop"] if long_view else spot >= pos["trail_stop"])
            )
            dt_ist = row["dt_ist"]
            T = _years_to_expiry(dt_ist)
            mark_premium = black76_price(
                spot, pos["strike"], T, pos["vol"], RISK_FREE_RATE, long_view,
            )
            premium_stopped = mark_premium <= pos["premium_stop"]

            timed_out = False
            if not pos["trail_active"]:
                age_min = (row["ts"] - pos["signal_ts"]) / 60.0
                if age_min >= settings.TV_FYERS_TIME_STOP_MIN:
                    r_points = abs(pos["entry_underlying"] - pos["stop_underlying"])
                    progress = (
                        spot - pos["entry_underlying"] if long_view
                        else pos["entry_underlying"] - spot
                    )
                    if progress < 0.5 * r_points and not (mark_premium - pos["entry_premium"] > 0):
                        timed_out = True

            exit_reason = None
            if hard_flat:
                exit_reason = "hard_flat_1510"
            elif stopped:
                exit_reason = "underlying_stop"
            elif trailed:
                exit_reason = "trail_stop"
            elif premium_stopped:
                exit_reason = "premium_backstop"
            elif timed_out:
                exit_reason = "time_stop"

            if exit_reason:
                gross = (mark_premium - pos["entry_premium"]) * pos["qty"]
                costs = calc_fno_costs(pos["entry_premium"], mark_premium, pos["qty"])
                pnl = gross - costs
                risk_rupees = pos["entry_premium"] * settings.TV_FYERS_STOP_PREMIUM_PCT * pos["qty"]
                r_mult = pnl / risk_rupees if risk_rupees > 0 else 0.0
                result.trades.append(BacktestTrade(
                    signal_ts=pos["signal_ts"], entry_dt_ist=str(pos["entry_dt_ist"]),
                    exit_dt_ist=str(dt_ist), direction=pos["direction"], strike=pos["strike"],
                    entry_underlying=pos["entry_underlying"], entry_premium=pos["entry_premium"],
                    exit_underlying=spot, exit_premium=mark_premium, exit_reason=exit_reason,
                    qty=pos["qty"], gross_pnl=gross, costs=costs, pnl=pnl, r_multiple=r_mult,
                ))
                closed = True
                break
        if not closed:
            if force_close and not window.empty:
                last = window.iloc[-1]
                spot = float(last["close"])
                T = _years_to_expiry(last["dt_ist"])
                mark_premium = black76_price(
                    spot, pos["strike"], T, pos["vol"], RISK_FREE_RATE, pos["direction"] == "CE",
                )
                gross = (mark_premium - pos["entry_premium"]) * pos["qty"]
                costs = calc_fno_costs(pos["entry_premium"], mark_premium, pos["qty"])
                pnl = gross - costs
                risk_rupees = pos["entry_premium"] * settings.TV_FYERS_STOP_PREMIUM_PCT * pos["qty"]
                r_mult = pnl / risk_rupees if risk_rupees > 0 else 0.0
                result.trades.append(BacktestTrade(
                    signal_ts=pos["signal_ts"], entry_dt_ist=str(pos["entry_dt_ist"]),
                    exit_dt_ist=str(last["dt_ist"]), direction=pos["direction"], strike=pos["strike"],
                    entry_underlying=pos["entry_underlying"], entry_premium=pos["entry_premium"],
                    exit_underlying=spot, exit_premium=mark_premium, exit_reason="backtest_window_end",
                    qty=pos["qty"], gross_pnl=gross, costs=costs, pnl=pnl, r_multiple=r_mult,
                ))
            else:
                still_open.append(pos)
    return still_open


def result_to_dict(result: BacktestResult) -> dict:
    """JSON-safe summary for the dashboard/ops route -- trades capped to
    the most recent 200 so a year-long run doesn't blow up a response
    body; the aggregate stats above always reflect the FULL run."""
    return {
        "signals_generated": result.signals_generated,
        "entries_taken": result.entries_taken,
        "rejections_by_reason": result.rejections_by_reason,
        "total_pnl": round(result.total_pnl, 2),
        "win_count": result.win_count,
        "loss_count": result.loss_count,
        "win_rate": round(result.win_rate, 4) if result.win_rate is not None else None,
        "avg_r_multiple": round(result.avg_r_multiple, 3) if result.avg_r_multiple is not None else None,
        "max_drawdown": round(result.max_drawdown, 2),
        "trades": [asdict(t) for t in result.trades[-200:]],
        "trade_count_total": len(result.trades),
    }
