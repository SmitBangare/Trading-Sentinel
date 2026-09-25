from fastapi import FastAPI, HTTPException, Request
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timedelta, timezone
import pytz
import os
import sys
import json
import hashlib
import asyncio
import time
import pandas as pd
import structlog
import aiosqlite
from pydantic import BaseModel
# [STRUCTLOG-CONFIGURE 2026-07-07] Configure structlog at module import
# time so EVERY logger in the engine gets a deterministic, timestamped
# formatter (rule 64). The previous behaviour relied on structlog's
# default config which delegated to stdlib logging; uvicorn's stdlib
# config at startup interacted badly with that, dropping timestamps on
# some `logger.warning(...)` calls. See logging_setup.py for the full
# rationale. configure_structlog() is idempotent.
from logging_setup import configure_structlog
configure_structlog(level="INFO")
from config import settings
from kite_client import KiteClient
from kite_client import latest_order_state as _kc_latest_order_state
from market_calendar import is_trading_day, prev_trading_day, is_market_open
from engine import evaluate_signal, calc_ema, evaluate_momentum_signal, calc_zerodha_costs, calc_rsi_series, calc_atr
from regime import RegimeEngine
from models import Regime
from contextlib import asynccontextmanager
from portfolio import filter_and_allocate, filter_momentum_signals
from risk_engine import RiskEngine
from position_tracker import update_daily_positions, get_open_positions, init_positions_db
from performance import (
    check_circuit_breakers,
    current_bankroll,
    init_ledger,
    nifty_bankroll,
    record_partial_realisation,
    record_trade_close,
)
from models import PortfolioResponse, HealthResponse, ManualPositionRequest, BankrollAdjustment, Signal
from backtest import run_backtest
from models import PerformanceReport, OpenPosition
from breadth import BreadthEngine
from universe import Universe
from momentum_shadow import (
    VARIANTS as MOMENTUM_SHADOW_VARIANTS,
    evaluate_momentum_shadows,
    momentum_shadow_execution_config,
    momentum_shadow_comparison,
    persist_momentum_shadow_results,
)
from penny_shadow import (
    VARIANTS as PENNY_SHADOW_VARIANTS,
    penny_shadow_comparison,
)

# ---------------------------------------------------------------------------
# [ROADMAP-4.1 2026-07-13] Extracted modules, re-exported here.
#
# The auth gate, the token lifecycle and the ops watchdogs now live in their
# own files. They are re-exported into main's namespace ON PURPOSE, and the
# re-export is load-bearing, not cosmetic:
#
#   * ~30 call sites and tests do `from main import _token_recon_mismatch_message`
#     / `main._scheduler_tick_job()` / `from main import _kite_probe_evaluate`.
#   * the scheduler registers these by REFERENCE, and the add_job census pins
#     each job as (callable, trigger, id) -- the callable's __name__ must not
#     change or a job looks dropped.
#
# So this is a move, not a rename. Same objects, same names, new home.
# ---------------------------------------------------------------------------
from engine_auth import (
    _check_internal_secret,
    _send_internal_secret_alert,
)
from token_lifecycle import (
    TOKEN_RECON_ALERT_MIN_INTERVAL_SEC,
    TokenPayload,
    _kite_token_cache_path,
    _load_persisted_kite_token_if_fresh,
    _persist_kite_token,
    _token_recon_mismatch_message,
    _token_recon_state,
    _token_reconciliation_tick,
    restore_kite_token_if_fresh,
)
from ops_watchdogs import (
    KITE_PROBE_ALERT_MIN_INTERVAL_SEC,
    KITE_PROBE_FAILURES_TO_ALERT,
    _kite_endpoint_probe_tick,
    _kite_probe_evaluate,
    _kite_probe_state,
    _ops_daily_snapshot,
    _scheduler_tick_job,
    _scheduler_tick_path,
    _scheduler_tick_state,
    _trading_readiness_tick,
)
# [OUTAGE-2026-07-13] The single way to page the operator. Re-exported as
# _notify_operator so route handlers reach it via _main.
from operator_alert import notify_operator as _notify_operator

# [ROADMAP-4.1 stage 2 2026-07-13] The two scheduler-registration functions
# moved to scheduler_setup.py. Re-exported here because lifespan() calls them
# by bare name, and tests import them from main.
#
# Import order matters and is safe: scheduler_setup imports main LAZILY (inside
# the register functions), so this top-level import cannot cycle.
from scheduler_setup import (
    register_fno_scheduler_jobs,
    register_partner_scheduler_jobs,
    register_penny_scheduler_jobs,
    register_tv_fyers_scheduler_jobs,
)


# [PENNY-MAIN 2026-06-21] Penny subsystem module imports.
from penny_universe import PennyUniverse, refresh_from_kite


# [AUDIT-FIX-1.2 2026-06-25] Single source of truth for "is this position
# intraday or delivery?" The previous code hardcoded is_intraday=True at
# 2 call sites regardless of pos['product_type'], which understated CNC
# costs (CNC STT = 0.1% sell side vs MIS 0.025%). Now callers derive
# the flag from product_type; missing/empty defaults to True (legacy
# behaviour preserved for older DB rows).
#
# Why not just default is_intraday=False? Because the legacy default
# for the close_position endpoint was True, and a silent flip would
# change every historical P&L number retroactively. We're a write-only
# ledger (we never re-derive historical costs), so the default doesn't
# matter for past data, but for new data we WANT to read product_type
# correctly.
def _is_intraday_from_product_type(product_type) -> bool:
    """Return True for MIS/NRML/empty/None, False for CNC.

    Empty/None: defaults to intraday=True (legacy default; matches the
    pre-fix hardcoded is_intraday=True in close_position).
    CNC: explicitly delivery, intraday=False.
    Anything else (futures/options product codes): treat as intraday.
    """
    if not product_type:
        return True
    return str(product_type).strip().upper() != "CNC"
from penny_regime import PennyRegimeEngine
from penny_scanner import PennyScanner
# app = FastAPI(title="Quant Engine Container B")
logger = structlog.get_logger()
kite = KiteClient(settings.DB_PATH)
# [SMIT-FYERS-OPTIONS 2026-09-25] Separate broker client for the
# TradingView -> Fyers options pipeline -- no shared state with `kite`
# above. Token is armed separately (own login flow, not yet wired as of
# rollout step 3); until set_token() is called, any use raises RuntimeError,
# which is correct: nothing should be calling it yet.
from fyers_client import FyersClient
fyers = FyersClient(settings.DB_PATH)
scheduler = AsyncIOScheduler(
    timezone="Asia/Kolkata",
    # 2026-06-22 fix: penny crons (hourly report, regime refresh, EOD,
    # daily reset) were being permanently skipped because the default
    # misfire_grace_time=1 was too tight. The Nifty momentum scan
    # takes ~5 minutes to scan 500 tickers at :00/:15/:30/:45 IST.
    # While it runs, the AsyncIOScheduler (single-threaded by default)
    # blocks any other job scheduled for the same minute. By the
    # time the Nifty scan finishes, the penny cron's 1-second grace
    # window has long passed and apscheduler marks it as "missed"
    # and skips it.
    #
    # Fix: extend the grace window to 10 minutes so penny crons still
    # fire even if they were blocked.
    #
    # We DO NOT set coalesce=False globally -- that would cause the
    # 30s penny_scan_interval to "catch up" by firing many missed
    # runs back-to-back after any long block (e.g., a 5-minute Nifty
    # scan). That burst would amplify contention and defeat the goal
    # of improving cron reliability. coalesce stays at the default
    # (True) so accumulated interval runs merge into one. The penny
    # cron jobs use the default coalesce too -- a missed hourly
    # report at :00 just shifts to the next :00 (60 min later).
    job_defaults={
        "misfire_grace_time": 600,
    },
)

# [PENNY-MAIN 2026-06-21] Penny subsystem globals + scheduler wiring.
# Mirrors the breadth pattern: lazy-init singletons, helper factories,
# and 7 scheduler jobs at the bottom of lifespan().
PENNY_UNIVERSE_JSON_PATH = "/data/penny_static.json"
PENNY_CORP_DATA_JSON_PATH = "/data/penny_company_data.json"

_penny_universe = None
_penny_regime_engine = PennyRegimeEngine()
_penny_scanner = None
# 2026-06-24 diagnostic add: most recent penny scan's universe size (sum
# of accept+reject+error). Read by run_penny_hourly_report so the
# "No action" message can show "Scanned: N | top rejects: ...". Reset
# on scanner singleton rebuild; 0 means "unknown" (older callers /
# pre-2026-06-24 deployments will show no diagnostic line).
_last_penny_scan_universe_size: int = 0
_last_penny_scan_at = None  # aware UTC datetime after successful scan_once only
# [AUDIT-FIX-CSV-SPAM 2026-06-26] Process-level one-shot gate for
# the universe_csv_missing_fallback warning. The fallback works
# (in-code NIFTY_500_TICKERS has 500 tickers), so emitting the
# warning every 15 minutes adds nothing but noise. The first warn
# tells the operator the CSV is missing; subsequent loads stay
# silent.
_universe_csv_warn_emitted: bool = False


def _classic_penny_source(paper_mode=None) -> str:
    """Static source contract for the classic Penny scanner.

    Historical ``PENNY`` rows are intentionally left ambiguous. New paper
    activity is ``PENNY_PAPER``; only an explicitly live scanner writes
    ``PENNY``.
    """
    if paper_mode is None:
        if _penny_scanner is not None and hasattr(_penny_scanner, "source_tag"):
            return _penny_scanner.source_tag
        paper_mode = not settings.PENNY_LIVE_TRADING
    return "PENNY_PAPER" if paper_mode else "PENNY"


def _make_penny_ledger_writer(source: str):
    if source not in ("PENNY", "PENNY_PAPER"):
        raise ValueError(f"invalid classic penny source: {source}")

    async def _writer(ticker: str, pnl: float) -> None:
        from performance import record_trade_close
        await record_trade_close(settings.DB_PATH, ticker, pnl, source=source)

    return _writer


def _get_penny_universe():
    """Lazy-load PennyUniverse from the static JSON. Returns None on failure."""
    global _penny_universe
    if _penny_universe is not None:
        return _penny_universe
    try:
        _penny_universe = PennyUniverse(
            json_path=PENNY_UNIVERSE_JSON_PATH,
            instrument_cache=kite.instrument_cache,
        )
        logger.info("penny_universe_loaded", path=PENNY_UNIVERSE_JSON_PATH)
    except Exception as e:
        logger.error("penny_universe_load_failed", error=str(e))
        _penny_universe = None
    return _penny_universe


def _get_penny_scanner():
    """Lazy-build PennyScanner singleton. Honors PENNY_LIVE_TRADING.

    [AUDIT-FIX-1.3 2026-06-25] Pass `regime` as a CALLABLE so the scanner
    re-reads the regime engine on every property access. The previous
    implementation froze the regime string at singleton-construction
    time, which meant a mid-day transition (PR1->PR2->PR3) was invisible
    to the 30s MIS scan loop until the singleton was rebuilt (which
    only happened in the 09:30 CNC scan).
    """
    global _penny_scanner
    if _penny_scanner is not None:
        return _penny_scanner
    paper_mode = not settings.PENNY_LIVE_TRADING
    source_tag = _classic_penny_source(paper_mode)

    # Live regime getter: re-reads the module-level engine on every call.
    # Returns the .value string (e.g. "PR2_ELEVATED").
    # [ROADMAP-3.6 2026-07-12] Fallback when the regime is not computed
    # yet is PR2_ELEVATED (2.5% sizing), no longer PR1_CALM (full 5%):
    # trading before the regime is known is elevated uncertainty, not
    # calm. Still trades (rule 15) -- just at reduced size.
    def _live_regime():
        if _penny_regime_engine is None:
            return "PR2_ELEVATED"
        tr = _penny_regime_engine.today_regime
        if tr is None:
            return "PR2_ELEVATED"
        return tr.value if hasattr(tr, "value") else str(tr)

    _penny_scanner = PennyScanner(
        kite=kite,
        universe_json_path=PENNY_UNIVERSE_JSON_PATH,
        paper_mode=paper_mode,
        regime=_live_regime,  # callable, not a string
        ledger_writer=_make_penny_ledger_writer(source_tag),
        source_tag=source_tag,
    )
    logger.info("penny_scanner_initialized", paper_mode=paper_mode, source=source_tag)
    return _penny_scanner


def _within_penny_market_hours(now_ist) -> bool:
    """[MARKET-HOURS-GATE 2026-07-11] True iff now_ist falls in the NSE
    session (09:15-15:30 IST inclusive). Module-level so tests can patch
    it and stay independent of the wall clock."""
    minutes = now_ist.hour * 60 + now_ist.minute
    return 9 * 60 + 15 <= minutes <= 15 * 60 + 30


async def run_penny_scanner_once():
    """30-second MIS leg (spec §9.1).

    [BUG-FIX 2026-07-01] Wrap scanner.scan_once in an asyncio
    timeout. Today the legacy scanner hung indefinitely on a
    stuck Kite quote call, blocking ALL overlapping cron jobs
    (including penny_edge_scan) with "maximum number of running
    instances reached". A 90-second hard ceiling combined with
    max_instances=1+coalesce=True on the scheduler entry
    prevents one stuck call from cascading into a system-wide
    stall.

    [CALENDAR-GATE 2026-07-03] P1 follow-up to PR-1. Skip on weekends +
    NSE holidays. Each 30s tick on a Saturday/Sunday/Holiday would
    otherwise log `penny_scan_complete accept=0 error=0 reject=0`
    (~5,760 lines per weekend day) and burn Kite rate-limit quota
    on empty quote bodies. Monday's first scan fires normally.
    """
    # [MARKET-HOURS-GATE 2026-07-11] Skip outside 09:15-15:30 IST. The
    # 30s tick previously ran off-hours all day: on 2026-07-10 that was
    # ~14.8k pre-market "evaluator returned None" rows + ~17.5k
    # "outside breakout time window" rejects in penny_signals.csv and
    # the matching wasted Kite quote calls. Entries are windowed
    # 10:30-14:30 inside the evaluator and exits live in the 15:00
    # force-close cron, so nothing needs ticks outside market hours.
    # Silent return (no log) -- mirrors the F&O tick gate; logging here
    # would emit ~2 lines/min all evening, the very storm this removes.
    now_ist = datetime.now(IST)
    if not _within_penny_market_hours(now_ist):
        return
    # [CALENDAR-GATE 2026-07-03] weekend / NSE-holiday early-return.
    today = now_ist.date()
    if not await is_trading_day(today, settings.DB_PATH):
        logger.info("penny_scanner_once_skip reason=non_trading_day")
        return
    # [FIX-PHASE3-AUDIT 2026-07-09] No-token guard, mirroring the swing
    # (run_screener) and momentum screeners. On 2026-07-09 the operator
    # missed the daily Zerodha login: the swing/momentum screeners
    # correctly logged `..._skipped reason=no_access_token`, but the
    # penny scanner had no guard -- it fired 26,311 doomed /quote calls
    # (HTTP 400 InputException), wrote 74,025 junk `evaluator returned
    # None` rows to penny_signals.csv, and log-storm-rotated the day's
    # breadcrumbs out of the 30 MB docker json-log buffer.
    if not kite.access_token:
        logger.warning("penny_scanner_once_skip reason=no_access_token")
        return
    global _last_penny_scan_universe_size, _last_penny_scan_at
    scanner = _get_penny_scanner()
    if scanner is None:
        # [PENNY-SCAN-SUMMARY 2026-07-06] Surface why the scanner is
        # None so silent "no work done" days stop being a black box.
        # Before this fix: the production log showed 1,376 penny_scan_complete
        # entries with accept=0 error=0 reject=0 over 12 hours and there was
        # no way to distinguish "scanner returned nothing" from "scanner is
        # not initialised yet" from "instrument cache is empty". The legacy
        # 0/0/0 line is misleading; this breadcrumb tells the operator what
        # actually happened.
        logger.warning(
            "penny_scan_summary scanner=None reason=scanner_not_initialised "
            "FIX=check penny_scanner_initialized was logged; otherwise the "
            "_penny_scanner singleton was never built (kite init failed?)"
        )
        return
    # A live penny entry has a broker-held protective stop.  Its paper twin
    # only has a PAPER-* evidence id, so without an explicit price check a
    # stop breach could rebound before the 14:30/15:00 exit jobs and leave a
    # fictitious winning paper outcome.  Run the paper-only stop check on the
    # existing market-hours cadence before looking for another entry.
    await run_penny_paper_stop_monitor()
    try:
        result = await asyncio.wait_for(
            scanner.scan_once(as_of=datetime.now(IST)),
            timeout=90.0,    # generous: scan normally takes <5s
        )
        logger.info("penny_scan_complete", **result)
        # [PENNY-SCAN-SUMMARY 2026-07-06] Companion summary line that
        # surfaces WHY the scan returned what it did. The legacy
        # penny_scan_complete is a single-line accept/error/reject counter;
        # on a 0/0/0 day the operator has no breadcrumb to explain it.
        # The scanner.scan_once() helper already logs `penny_scan_loop_summary`
        # internally (universe size + degraded count) -- this line adds the
        # CALLER's view (which universe+regime it saw) so silent-empty days
        # are debuggable in 30 seconds instead of needing a full re-deploy.
        try:
            universe = scanner._load_universe()
            cache_size = len(getattr(scanner.kite, "instrument_cache", {}) or {})
            logger.info(
                "penny_scan_summary caller_view accept=%d reject=%d error=%d "
                "universe_size=%d cache_size=%d regime=%s",
                int(result.get("accept", 0)),
                int(result.get("reject", 0)),
                int(result.get("error", 0)),
                len(universe),
                cache_size,
                scanner.regime,
            )
        except Exception as _summary_exc:
            # Summary must never fail the scan itself -- log and continue.
            logger.warning(
                "penny_scan_summary_failed err=%s", str(_summary_exc),
            )
        _last_penny_scan_universe_size = (
            int(result.get("accept", 0))
            + int(result.get("reject", 0))
            + int(result.get("error", 0))
        )
        _last_penny_scan_at = datetime.now(timezone.utc)
    except asyncio.TimeoutError:
        logger.error("penny_scan_timeout scan stuck on Kite; "
                     "next cron slot will resume")
        logger.warning(
            "penny_scan_summary caller_view "
            "accept=0 reject=0 error=0 reason=timeout_90s "
            "FIX=penny_scan_timeout was logged -- the scan_once call hung "
            "on a Kite API; next 30s tick will resume"
        )
    except Exception as e:
        logger.error("penny_scan_failed", error=str(e))
        logger.warning(
            "penny_scan_summary caller_view "
            "accept=0 reject=0 error=0 reason=exception err=%s", str(e),
        )


async def run_penny_connors_scan():
    """Once-daily 09:30 CNC leg (spec §4).

    [CALENDAR-GATE 2026-07-03] P1 follow-up to PR-1. Skip on weekends +
    NSE holidays. Connors time-stop counting uses trading_days_between_sync
    so weekend handling is already correct in the exit logic; this gate
    prevents a wasted 09:30 scan that would only produce rejects.
    """
    # [2026-07-03 BUG-FIX] Move the `from datetime import datetime, timezone`
    # import to BEFORE the calendar gate. Python's scoping rules mark
    # `datetime` as local for the whole function the moment any
    # `from datetime import datetime` statement appears -- and our gate
    # uses `datetime.now(IST).date()` at line 343. With the import BELOW
    # the gate, Python raised UnboundLocalError at runtime (and in
    # tests/test_calendar_gates.py). Found by PR-2 functional tests.
    from datetime import datetime, timezone  # noqa: I001  (must precede gate)
    # [CALENDAR-GATE 2026-07-03] weekend / NSE-holiday early-return.
    today = datetime.now(IST).date()
    if not await is_trading_day(today, settings.DB_PATH):
        logger.info("penny_connors_scan_skip reason=non_trading_day")
        return
    # [FIX-PHASE3-AUDIT 2026-07-09] No-token guard -- same rationale as
    # run_penny_scanner_once (2026-07-09 missed-login incident).
    if not kite.access_token:
        logger.warning("penny_connors_scan_skip reason=no_access_token")
        return
    global _last_penny_scan_universe_size
    scanner = _get_penny_scanner()
    if scanner is None:
        return
    try:
        # Reload scanner with fresh universe + regime (per 2026-06-22 wiring fix)
        global _penny_scanner
        _penny_scanner = None
        scanner = _get_penny_scanner()
        universe = scanner._load_universe()
        # 2026-06-24 diagnostic: cache universe size so the next hourly
        # report knows how many tickers were eligible.
        _last_penny_scan_universe_size = len(universe)
        # [FIX-PHASE3-AUDIT 2026-07-09] Persist every CNC evaluation to the
        # penny signal log. Pre-fix log_penny_signal was called ONLY from
        # penny_scanner.scan_once (the MIS path): all 215,814 lifetime rows
        # in /data/penny_signals.csv were leg=MIS and the CNC leg was
        # completely invisible to the CSV deep-audit (ops rule 75). One
        # scan_id groups the whole 09:30 pass, mirroring scan_once.
        from penny_signal_log import init_penny_signal_db, log_penny_signal
        from uuid import uuid4 as _uuid4
        cnc_scan_id = f"penny-cnc-{_uuid4().hex[:8]}"
        await init_penny_signal_db(settings.DB_PATH)

        async def _log_cnc(ticker, dec):
            # Never let signal-log I/O break the trading loop.
            try:
                dec = dec or {}
                await log_penny_signal(
                    settings.DB_PATH, scan_id=cnc_scan_id, ticker=ticker,
                    leg="CNC", accepted=bool(dec.get("accept")),
                    reject_reason=(
                        "" if dec.get("accept")
                        else dec.get("reject_reason",
                                     "evaluator returned None (see prior warn/error)")
                    ),
                    regime=str(scanner.regime),
                    close=float(dec.get("entry", 0.0) or 0.0),
                )
            except Exception as log_err:
                logger.warning("penny_cnc_signal_log_failed ticker=%s error=%s",
                               ticker, str(log_err))

        accept = reject = 0
        for t in universe:
            if scanner.risk_engine.is_disabled(t["symbol"]):
                continue
            decision = await scanner._evaluate_ticker_connors(
                # [FIX-PHASE3-AUDIT 2026-07-09] IST, not UTC. The evaluator's
                # late-day gate does as_of.replace(hour=9, minute=15) and
                # treats the result as IST market open; a UTC now() made
                # minutes_since_open negative (~-5.5h), so the gate passed
                # by accident rather than by design. The MIS path already
                # passes datetime.now(IST) -- this aligns the CNC path.
                t["symbol"], as_of=datetime.now(IST),
                prev_close=t.get("prev_close"),
            )
            await _log_cnc(t["symbol"], decision)
            if decision is None:
                reject += 1
                continue
            if not decision.get("accept"):
                reject += 1
                continue
            from penny_execution_journal import attempt_identity
            from penny_position_reservations import (
                persist_reserved_penny_position,
                reserve_penny_position,
                set_penny_reservation_state,
            )
            from position_tracker import init_positions_db
            candidate_identity = "|".join((
                datetime.now(IST).date().isoformat(), cnc_scan_id,
                t["symbol"], "CNC", "PEN_CONNORS",
                str(decision.get("entry")), str(decision.get("stop_loss")),
            ))
            attempt_id, candidate_key = attempt_identity(
                candidate_identity, t["symbol"], "CNC", scanner.source_tag,
            )
            event_context = {
                "attempt_id": attempt_id, "scan_id": cnc_scan_id,
                "candidate_key": candidate_key, "ticker": t["symbol"],
                "leg": "CNC", "source": scanner.source_tag,
                "mode": "paper" if scanner.executor.paper_mode else "live",
            }
            await init_positions_db(settings.DB_PATH)
            reservation = await reserve_penny_position(
                settings.DB_PATH, attempt_id=attempt_id,
                source=scanner.source_tag, ticker=t["symbol"],
                product_type="CNC",
                max_total=settings.PENNY_MAX_POSITIONS_TOTAL,
                max_leg=settings.PENNY_MAX_POSITIONS_CNC,
            )
            if not reservation.granted:
                logger.info(
                    "penny_cnc_reservation_rejected ticker=%s attempt_id=%s "
                    "reason=%s total=%d cnc=%d",
                    t["symbol"], attempt_id, reservation.reason,
                    reservation.total_occupied, reservation.leg_occupied,
                )
                reject += 1
                continue
            await scanner._record_execution_event(
                "CANDIDATE_ACCEPTED",
                {"signal_entry": decision.get("entry"),
                 "stop_loss": decision.get("stop_loss"),
                 "quantity": decision.get("shares")},
                event_context,
            )
            # Delegate to executor (per 2026-06-22 wiring fix)
            from penny_models import PennyLeg
            order_result = await scanner.executor.execute_entry(
                ticker=t["symbol"],
                leg=PennyLeg.CNC,
                entry_price=decision.get("entry", 0.0),
                stop_loss=decision.get("stop_loss", 0.0),
                shares=decision.get("shares", 0),
                attempt_context=event_context,
            )
            logger.info(
                "penny_cnc_entry_attempted ticker=%s entry=%.2f order_id=%s",
                t["symbol"], decision.get("entry", 0.0),
                order_result.get("entry_order_id"),
            )
            # [PENNY-G5 2026-06-25] Write CNC position row so the post-T1
            # trailing stop (evaluate_connors_exit) can actually read
            # atr_1min_post_t1. Pre-fix this INSERT was absent -- CNC
            # entries had no row in positions table, so the exit logic
            # was unreachable. The position is only written if the entry
            # actually FILLED (paper + live modes).
            entry_status = order_result.get("entry_status")
            if entry_status in ("filled", "paper"):
                try:
                    from datetime import datetime as _dt, timezone as _tz
                    fill_price = float(
                        order_result.get("fill_price") or decision.get("entry", 0.0)
                    )
                    row_id = await persist_reserved_penny_position(
                        settings.DB_PATH, attempt_id=attempt_id, values={
                            "ticker": t["symbol"], "exchange": "NSE",
                            "entry_date": _dt.now(_tz.utc).isoformat(),
                            "entry_price": fill_price,
                            "shares": decision.get("shares", 0),
                            "stop_loss_initial": decision.get("stop_loss", 0.0),
                            "trailing_stop_current": decision.get("stop_loss", 0.0),
                            "target_1": decision.get("target_1", 0.0),
                            "target_2": decision.get("target_2", 0.0),
                            "atr_14_at_entry": decision.get("atr_14", 0.0),
                            "highest_close_since_entry": fill_price,
                            "status": "OPEN", "source": scanner.source_tag,
                            "product_type": "CNC", "regime_at_entry": scanner.regime,
                            "atr_1min_post_t1": decision.get("atr_1min_post_t1", 0.0),
                            "t1_fired": 0,
                            "sl_order_id": order_result.get("sl_order_id"),
                        },
                    )
                    await scanner._record_execution_event(
                        "POSITION_CREATED",
                        {"position_rowid": row_id, "entry_price": fill_price,
                         "sl_order_id": order_result.get("sl_order_id")},
                        event_context,
                    )
                    logger.info(
                        "penny_cnc_position_written ticker=%s shares=%d atr_1min=%.4f",
                        t["symbol"], decision.get("shares", 0),
                        decision.get("atr_1min_post_t1", 0.0),
                    )
                except Exception as e:
                    # [AUDIT-FIX-1.5 2026-06-25] DB write failure after the
                    # entry actually filled (live mode) or was paper-recorded
                    # leaves the position unmanaged by our exit logic
                    # (time-stop / post-T1 trailing / 14:30 smart-EOD all
                    # query the DB).
                    #
                    # What we DON'T do: auto-fire a market-exit here.
                    # The executor already placed an SL-M at the broker in
                    # step 3 of execute_entry -- that SL-M is the safety
                    # net for the position. Firing a market order here
                    # could double-sell if both orders fill.
                    #
                    # What we DO: log loudly + send a Telegram alert so the
                    # operator knows the position is untracked by our
                    # software. The SL-M at the broker still protects the
                    # account; the operator can choose to manually close
                    # via the broker if they want full software tracking.
                    sl_id = order_result.get("sl_order_id") or "UNKNOWN"
                    entry_id = order_result.get("entry_order_id") or "UNKNOWN"
                    is_live = (entry_status == "filled")
                    logger.error(
                        "penny_cnc_position_write_failed "
                        "ticker=%s entry_id=%s sl_order_id=%s error=%s",
                        t["symbol"], entry_id, sl_id, str(e),
                    )
                    if is_live:
                        # Live mode: position is held by the broker. SL-M
                        # is the safety net (entry_id + sl_order_id logged
                        # so the operator can correlate). Send a CRITICAL
                        # alert so they know to intervene if desired.
                        try:
                            import httpx as _httpx
                            msg = (
                                f"🚨 **CNC POSITION UNTRACKED** 🚨\n"
                                f"Ticker: {t['symbol']}\n"
                                f"Entry: {decision.get('entry', 0):.2f} x "
                                f"{decision.get('shares', 0)} shares\n"
                                f"Entry order: {entry_id}\n"
                                f"SL-M order: {sl_id} (broker-side safety)\n"
                                f"DB write failed: {str(e)[:200]}\n"
                                f"Action: position is protected by SL-M at "
                                f"the broker. Manually close via broker if "
                                f"you want software tracking."
                            )
                            async with _httpx.AsyncClient() as _client:
                                await _client.post(
                                    f"{settings.CONTAINER_A_URL}/api/internal/notify",
                                    json={"message": msg},
                                    headers={"X-Internal-Secret": settings.INTERNAL_API_SECRET},
                                    timeout=5.0,
                                )
                        except Exception as notify_err:
                            logger.error(
                                "penny_cnc_untracked_alert_failed "
                                "ticker=%s error=%s",
                                t["symbol"], str(notify_err),
                            )
                    await set_penny_reservation_state(
                        settings.DB_PATH, attempt_id=attempt_id,
                        state="UNRESOLVED", note=f"position_persist_failed:{str(e)[:200]}",
                    )
            elif order_result.get("unwound"):
                # Release only after the executor confirmed the emergency
                # unwind; no broker exposure remains.
                await set_penny_reservation_state(
                    settings.DB_PATH, attempt_id=attempt_id,
                    state="RELEASED", note="confirmed_unwind",
                )
            elif entry_status in {
                "ticker_blocked", "drift_rejected", "stop_already_breached", "no_quote",
            }:
                await set_penny_reservation_state(
                    settings.DB_PATH, attempt_id=attempt_id,
                    state="RELEASED", note=f"local_validation:{entry_status}",
                )
            else:
                # Rejected/timeout/transport states are not proof that the
                # broker did nothing. Quarantine the slot until reconciliation.
                await set_penny_reservation_state(
                    settings.DB_PATH, attempt_id=attempt_id,
                    state="UNRESOLVED", note=f"entry_state:{entry_status or 'unknown'}",
                )
            accept += 1
        logger.info("penny_connors_scan_done accept=%d reject=%d", accept, reject)
    except Exception as e:
        logger.error("penny_connors_scan_failed", error=str(e))


# [AUDIT-FIX-REFRESH-SKIP 2026-06-30] Guard against overlapping
# runs. The 08:00 cron fires once, but if the previous run is
# still in flight (e.g. a slow Kite, a network blip) the next cron
# tick at 09:00, 10:00 etc. would otherwise queue another
# concurrent refresh and double the Kite API cost. We use a
# module-level in-progress flag (re-entrancy-safe) -- the new
# run logs a clear "skipped, previous still in progress" line
# and returns immediately. The lock is cleared in a `finally`
# block so even an exception path releases it.
_penny_universe_refresh_in_progress: bool = False


async def run_penny_universe_refresh() -> bool:
    """Daily 08:00 IST refresh (spec §9.1). Returns True only when
    refresh_from_kite actually produced a ranked universe -- the
    daily_bootstrap registry ([BOOTSTRAP-2026-07-17]) uses the return to
    decide whether the task is done for the day or must be retried after
    the operator's login.

    [CALENDAR-GATE 2026-07-03] P1 follow-up to PR-1. Skip on weekends +
    NSE holidays. On a non-trading day the universe loader's network
    calls (Kite + Yahoo) waste quota; the file gets overwritten Monday
    morning so there's no data-staleness gap. Mirrors the run_screener
    gate at main.py:1583-1588.
    """
    # [CALENDAR-GATE 2026-07-03] weekend / NSE-holiday early-return.
    today = datetime.now(IST).date()
    if not await is_trading_day(today, settings.DB_PATH):
        logger.info("penny_universe_refresh_skip reason=non_trading_day")
        return True  # nothing to do IS the done state on a holiday
    global _penny_universe_refresh_in_progress
    if _penny_universe_refresh_in_progress:
        logger.warning(
            "penny_universe_refresh_skipped reason=previous_run_in_progress"
        )
        return False
    _penny_universe_refresh_in_progress = True
    # [AUDIT-FIX-REFRESH 2026-06-26] Loud start/end logging. The
    # earlier implementation had zero log lines on the happy path
    # (refresh_from_kite's own logs were swallowed if the function
    # returned early via one of its None-return paths), so the
    # operator had no way to tell whether the 08:00 cron even fired.
    # Wrap the call with explicit start/end + success-count logging
    # so a silent refresh is observable from the docker logs alone.
    import time as _time
    t0 = _time.monotonic()
    logger.info("penny_universe_refresh_start")
    try:
        ranked = await refresh_from_kite(
            kite=kite,
            out_json_path=PENNY_UNIVERSE_JSON_PATH,
            corp_json_path=PENNY_CORP_DATA_JSON_PATH,
        )
        # Force the universe singleton to reload next call
        global _penny_universe
        _penny_universe = None
        elapsed = _time.monotonic() - t0
        if ranked is None:
            # refresh_from_kite returned None -- it has already logged
            # the specific cause (e.g. all quote batches failed). Log
            # a single-line summary so the operator sees the silent
            # skip from one grep.
            logger.warning(
                "penny_universe_refresh_skipped "
                "(refresh_from_kite returned None -- see prior "
                "penny_universe_quote_* events for cause) elapsed=%.1fs",
                elapsed,
            )
            return False
        else:
            logger.info(
                "penny_universe_refresh_done count=%d as_of=%s elapsed=%.1fs",
                len(ranked),
                datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                elapsed,
            )
            return True
    except Exception as e:
        logger.error("penny_universe_refresh_failed", error=str(e))
        return False
    finally:
        _penny_universe_refresh_in_progress = False


async def run_penny_regime_compute():
    """Daily 09:20 IST regime compute (spec §6, §9.1).

    [CALENDAR-GATE 2026-07-03] P1 follow-up to PR-1. Skip on weekends +
    NSE holidays. Regime gets computed fresh Monday morning from kite's
    same-day index data so there's no data-staleness gap.
    """
    # [CALENDAR-GATE 2026-07-03] weekend / NSE-holiday early-return.
    today = datetime.now(IST).date()
    if not await is_trading_day(today, settings.DB_PATH):
        logger.info("penny_regime_compute_skip reason=non_trading_day")
        return
    try:
        await _penny_regime_engine.compute_today(kite=kite)
        # [AUDIT-FIX-REGIME-LOG 2026-06-26] Use .value (e.g. "PR1_CALM")
        # rather than str() which produces the Enum repr "PennyRegime.PR1_CALM".
        # str() output breaks operator grep + the daily attribution message
        # builder downstream.
        regime_val = _penny_regime_engine.today_regime
        regime_str = regime_val.value if hasattr(regime_val, "value") else str(regime_val)
        logger.info("penny_regime_computed", regime=regime_str)
    except Exception as e:
        logger.error("penny_regime_compute_failed", error=str(e))


async def run_penny_regime_refresh():
    """Daily 13:00 IST intraday regime refresh (spec §6, §9.1).

    [CALENDAR-GATE 2026-07-03] P1 follow-up to PR-1. Skip on weekends +
    NSE holidays. Monday's 09:20 compute overwrites any stale value.
    """
    # [CALENDAR-GATE 2026-07-03] weekend / NSE-holiday early-return.
    today = datetime.now(IST).date()
    if not await is_trading_day(today, settings.DB_PATH):
        logger.info("penny_regime_refresh_skip reason=non_trading_day")
        return
    try:
        await _penny_regime_engine.compute_today(kite=kite)
        regime_val = _penny_regime_engine.today_regime
        regime_str = regime_val.value if hasattr(regime_val, "value") else str(regime_val)
        logger.info("penny_regime_refreshed", regime=regime_str)
    except Exception as e:
        logger.error("penny_regime_refresh_failed", error=str(e))


async def _penny_ltp(ticker: str, fallback: float) -> float:
    """
    [TIER0-0.7 2026-07-14] Live price for a penny position.

    run_penny_eod_check used to read `p.get("current_price")` -- a column that
    does not exist on the positions table -- and fall back to entry_price. So the
    smart-EOD rule compared the entry price to itself, always concluded "in profit
    and far from target", and always held.

    On failure we return the entry price, which makes smart_eod_check hold. That
    is the safe direction: the 15:00 force-close still flattens the position.
    """
    try:
        symbol = str(ticker or "").split(":")[-1].upper()
        token = (getattr(kite, "instrument_cache", {}) or {}).get(symbol)
        if not token:
            logger.warning("penny_ltp_no_instrument_token ticker=%s", symbol)
            return fallback
        quote = await kite.get_quote([int(token)])
        data = (quote or {}).get(int(token)) or (quote or {}).get(str(token)) or {}
        ltp = float(data.get("last_price") or 0)
        if ltp > 0:
            return ltp
        logger.warning("penny_ltp_missing ticker=%s", ticker)
    except Exception as e:
        logger.warning("penny_ltp_failed ticker=%s error=%s", ticker, str(e))
    return fallback


async def _penny_exit_event_context(position: dict, reason: str) -> tuple[str, dict]:
    """Stable exit identity plus the entry's immutable journal identity."""
    from penny_execution_journal import attempt_identity, attempt_identity_fields

    source = position.get("source") or _classic_penny_source()
    mode = "paper" if source == "PENNY_PAPER" else "live"
    entry_attempt = position.get("penny_attempt_id")
    original = (
        await attempt_identity_fields(settings.DB_PATH, entry_attempt)
        if entry_attempt else None
    )
    if original is None:
        entry_attempt, candidate = attempt_identity(
            position.get("entry_date") or "legacy",
            position.get("ticker") or "UNKNOWN",
            position.get("product_type") or "MIS",
            source,
        )
        original = {
            "scan_id": "legacy-position",
            "candidate_key": candidate,
            "ticker": position.get("ticker") or "UNKNOWN",
            "leg": position.get("product_type") or "MIS",
            "source": source,
            "mode": mode,
        }
    # Quantity is part of the identity: after a confirmed partial, the
    # residual receives a new idempotent exit attempt on the next pass.
    # Reason is intentionally NOT part of the identity. A 14:30 smart-EOD
    # submission may still be OPEN/UNKNOWN at 15:00; the force-close job must
    # reconcile that same order, never submit a second SELL for the same shares.
    exit_attempt = f"{entry_attempt}-exit-{int(position.get('shares') or 0)}"
    return exit_attempt, original


async def _append_penny_exit_event(
    attempt_id: str, context: dict, event_type: str, payload: dict,
) -> None:
    from penny_execution_journal import append_execution_event
    await append_execution_event(
        settings.DB_PATH,
        attempt_id=attempt_id,
        scan_id=context["scan_id"],
        candidate_key=context["candidate_key"],
        ticker=context["ticker"],
        leg=context["leg"],
        source=context["source"],
        mode=context["mode"],
        event_type=event_type,
        payload=payload,
    )


async def _settle_confirmed_penny_exit(
    position: dict, *, confirmed_qty: int, fill_price: float, reason: str,
    clear_sl_order: bool = False,
) -> dict:
    """Atomically settle only a confirmed fill and its cost-bearing ledger row.

    The shares predicate is the restart-idempotency fence: after one process
    settles a fill, a replay with the old snapshot changes zero rows and writes
    no second ledger event.
    """
    from penny_risk import calc_penny_costs
    from performance import allocation_for_source

    requested = int(position.get("shares") or 0)
    quantity = min(requested, max(0, int(confirmed_qty or 0)))
    if quantity <= 0 or fill_price <= 0:
        return {"settled": 0, "remaining": requested, "pnl": 0.0}
    source = position.get("source") or _classic_penny_source()
    entry = float(position.get("entry_price") or 0)
    costs = calc_penny_costs(entry, fill_price, quantity, is_intraday=True)
    pnl = (fill_price - entry) * quantity - costs
    remaining = requested - quantity
    now_utc = datetime.now(timezone.utc).isoformat()
    where = (
        "ticker=? AND source=? AND entry_date=? AND shares=? "
        "AND exit_date IS NULL AND status IN ('OPEN','CLOSED_T1')"
    )
    params = (
        position.get("ticker"), source, position.get("entry_date"), requested,
    )
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        ledger_total = await (await db.execute(
            "SELECT COALESCE(SUM(pnl),0.0) FROM bankroll_ledger WHERE source=?",
            (source,),
        )).fetchone()
        before = allocation_for_source(source) + float(ledger_total[0] or 0.0)
        if remaining:
            cur = await db.execute(
                "UPDATE positions SET shares=?, status='OPEN', "
                "realised_pnl=COALESCE(realised_pnl,0)+?, "
                "sl_order_id=CASE WHEN ? THEN NULL ELSE sl_order_id END WHERE " + where,
                (remaining, pnl, 1 if clear_sl_order else 0, *params),
            )
            event_type = "TRADE_PARTIAL"
        else:
            total_pnl = float(position.get("realised_pnl") or 0) + pnl
            risk = float(position.get("initial_capital_at_risk") or 0)
            previous_realised = float(position.get("realised_pnl") or 0)
            if risk <= 0 and previous_realised == 0:
                risk = max(0.0, entry - float(position.get("stop_loss_initial") or entry)) * requested
            # A legacy row that already realised a partial has lost its original
            # quantity denominator.  NULL is honest; computing R from only the
            # residual quantity exaggerates both wins and losses.
            r_multiple = total_pnl / risk if risk > 0 else None
            cur = await db.execute(
                "UPDATE positions SET shares=0, status='CLOSED_TIME', "
                "exit_price=?, exit_date=?, realised_pnl=?, r_multiple=? "
                "WHERE " + where,
                (fill_price, now_utc, total_pnl, r_multiple, *params),
            )
            event_type = "TRADE_CLOSED"
        if cur.rowcount != 1:
            await db.rollback()
            return {"settled": 0, "remaining": requested, "pnl": 0.0}
        await db.execute(
            "INSERT INTO bankroll_ledger "
            "(timestamp,event_type,ticker,pnl,bankroll_before,bankroll_after,notes,source) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (now_utc, event_type, position.get("ticker"), pnl, before,
             before + pnl, f"confirmed_exit:{reason}", source),
        )
        await db.commit()
    return {"settled": quantity, "remaining": remaining, "pnl": pnl}


async def _execute_scheduled_penny_exit(
    position: dict, *, reason: str, reference_price: float,
) -> dict:
    """Submit/reconcile one scheduled exit and persist confirmed quantity only."""
    from penny_execution_journal import attempt_event_payload
    from penny_models import PennyLeg

    scanner = _get_penny_scanner()
    executor = scanner.executor
    attempt_id, context = await _penny_exit_event_context(position, reason)
    quantity = int(position.get("shares") or 0)
    leg = PennyLeg.MIS

    # Cancel a real protective order before a discretionary live exit. Paper
    # IDs are local evidence and must never reach the broker.
    sl_order_id = position.get("sl_order_id")
    if not executor.paper_mode and sl_order_id and not str(sl_order_id).startswith("PAPER-"):
        stop_truth = await _cancel_order_truth(executor.kite, sl_order_id, quantity)
        if stop_truth["filled_quantity"] > 0 and stop_truth["confirmed_fill"]:
            filled = stop_truth["filled_quantity"]
            result = {
                "status": "COMPLETE" if filled >= quantity else "PARTIAL",
                "order_id": sl_order_id, "requested_qty": quantity,
                "confirmed_qty": filled,
                "fill_price": stop_truth["average_price"],
                "paper": False, "message": "protective stop filled",
            }
        elif stop_truth["filled_quantity"] > 0:
            await executor._page_operator(
                f"{position.get('ticker')}: protective stop {sl_order_id} "
                "partially filled but has no average fill price. No second SELL "
                "was sent; reconcile manually."
            )
            return {
                "status": "UNKNOWN", "order_id": sl_order_id,
                "requested_qty": quantity, "confirmed_qty": 0,
                "fill_price": None, "paper": False,
                "message": "protective stop fill price unknown",
            }
        elif not stop_truth["safe_to_sell"]:
            await _append_penny_exit_event(
                attempt_id, context, "EXIT_UNKNOWN",
                {"order_id": None, "requested_qty": quantity,
                 "confirmed_qty": 0},
            )
            await executor._page_operator(
                f"{position.get('ticker')}: protective stop {sl_order_id} could "
                f"not be confirmed cancelled ({stop_truth['status']}). "
                "No discretionary exit was sent; "
                "reconcile the stop and position in Kite immediately."
            )
            return {
                "status": "UNKNOWN", "order_id": None,
                "requested_qty": quantity, "confirmed_qty": 0,
                "fill_price": None, "paper": False,
                "message": "protective stop cancellation unconfirmed",
            }

    existing = await attempt_event_payload(settings.DB_PATH, attempt_id, "EXIT_SUBMITTED")
    order_id = (existing or {}).get("order_id")
    if executor.paper_mode:
        result = await executor.execute_exit(
            position.get("ticker"), leg, quantity,
            reference_price=reference_price,
        )
        await _append_penny_exit_event(
            attempt_id, context, "EXIT_SIMULATED",
            {"order_id": result.get("order_id"), "quantity": quantity,
             "fill_price": result.get("fill_price")},
        )
    elif not (sl_order_id and 'result' in locals() and result.get("message") == "protective stop filled"):
        if not order_id:
            result = None
            # Persist a stable broker tag before crossing the network. On a
            # crash or lost response the next pass searches today's broker
            # order book by this identity and never blindly sends a second SELL.
            intent = await attempt_event_payload(
                settings.DB_PATH, attempt_id, "EXIT_INTENT"
            )
            intent_date = (intent or {}).get("intent_date")
            today_ist = datetime.now(IST).date().isoformat()
            unwind_tag = (intent or {}).get("tag") or (
                "PENX" + hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()[:16]
            )
            await _append_penny_exit_event(
                attempt_id, context, "EXIT_INTENT",
                {"tag": unwind_tag, "quantity": quantity,
                 "intent_date": intent_date or today_ist},
            )
            recovered = await executor.resolve_unwind_tag(
                unwind_tag, position.get("ticker"), leg, quantity,
            )
            unwind_tag = recovered.get("tag") or unwind_tag
            if recovered["status"] == "FOUND":
                order_id = recovered["order_id"]
            elif recovered["status"] == "NOT_FOUND":
                if intent and intent_date != today_ist:
                    order_id = None
                    result = {
                        "status": "UNKNOWN", "order_id": None,
                        "requested_qty": quantity, "confirmed_qty": 0,
                        "fill_price": None, "paper": False,
                        "message": "prior-session exit intent requires manual reconciliation",
                    }
                else:
                    try:
                        order_id = await executor._market_unwind(
                            position.get("ticker"), leg, quantity,
                            ltp=reference_price, tag=unwind_tag,
                            raise_on_ambiguous=True,
                        )
                    except Exception:
                        order_id = None
                        result = {
                            "status": "UNKNOWN", "order_id": None,
                            "requested_qty": quantity, "confirmed_qty": 0,
                            "fill_price": None, "paper": False,
                            "message": "exit submission outcome is ambiguous",
                        }
            else:
                result = {
                    "status": "UNKNOWN", "order_id": None,
                    "requested_qty": quantity, "confirmed_qty": 0,
                    "fill_price": None, "paper": False,
                    "message": "broker order book unavailable for recovery",
                }
            if not order_id and result and result.get("status") == "UNKNOWN":
                await executor._page_operator(
                    f"{position.get('ticker')}: scheduled exit submission is "
                    "ambiguous or cannot be reconciled from the broker order "
                    f"book; {quantity} shares remain locally open. Do not retry "
                    "manually until Kite is reconciled."
                )
            if order_id:
                await _append_penny_exit_event(
                    attempt_id, context, "EXIT_SUBMITTED",
                    {"order_id": order_id, "quantity": quantity},
                )
            elif result is None or result.get("status") != "UNKNOWN":
                result = {
                    "status": "REJECTED", "order_id": None,
                    "requested_qty": quantity, "confirmed_qty": 0,
                    "fill_price": None, "paper": False,
                    "message": "broker rejected exit submission",
                }
                await executor._page_operator(
                    f"{position.get('ticker')}: scheduled exit submission failed; "
                    f"{quantity} shares remain open. Resolve manually."
                )
        if order_id:
            result = await executor.execute_exit(
                position.get("ticker"), leg, quantity,
                reference_price=reference_price, existing_order_id=order_id,
            )

    status = result.get("status") or "UNKNOWN"
    event = {
        "COMPLETE": "EXIT_CONFIRMED", "PARTIAL": "EXIT_PARTIAL",
        "REJECTED": "EXIT_REJECTED", "UNKNOWN": "EXIT_UNKNOWN",
    }.get(status, "EXIT_UNKNOWN")
    await _append_penny_exit_event(
        attempt_id, context, event,
        {"order_id": result.get("order_id"), "requested_qty": quantity,
         "confirmed_qty": int(result.get("confirmed_qty") or 0)},
    )

    fill_price = float(result.get("fill_price") or 0)
    if int(result.get("confirmed_qty") or 0) > 0 and fill_price > 0:
        settlement = await _settle_confirmed_penny_exit(
            position,
            confirmed_qty=int(result["confirmed_qty"]),
            fill_price=fill_price,
            reason=reason,
            # Any live discretionary exit reached this point only after the
            # old protective stop was confirmed cancelled; a partial residual
            # must not advertise that dead stop as protection. Protective-stop
            # partials likewise consume and clear their cumulative fill.
            clear_sl_order=(not executor.paper_mode and bool(sl_order_id)),
        )
        if settlement["settled"]:
            await _append_penny_exit_event(
                attempt_id, context, "POSITION_EXIT_SETTLED",
                {"settled_qty": settlement["settled"],
                 "remaining_qty": settlement["remaining"],
                 "fill_price": fill_price, "pnl": settlement["pnl"]},
            )
        result["settlement"] = settlement
    elif int(result.get("confirmed_qty") or 0) > 0:
        result["status"] = "UNKNOWN"
        await executor._page_operator(
            f"{position.get('ticker')}: broker confirmed quantity but returned no "
            "average fill price. Position remains unsettled; reconcile manually."
        )
    return result


async def run_penny_paper_stop_monitor() -> dict:
    """Settle breached protective stops for classic penny paper positions.

    Live ``PENNY`` rows are intentionally excluded: their stops are managed and
    filled by the broker, and local LTP polling is not execution truth.  The
    classic paper book has no broker order, however, so this monitor is needed
    to make its lifecycle comparable to the live strategy.  Settlement routes
    through the existing idempotent scheduled-exit path and therefore cannot
    double-book a close if it overlaps an EOD job.
    """
    scanner = _get_penny_scanner()
    executor = getattr(scanner, "executor", None) if scanner is not None else None
    if executor is None or not bool(getattr(executor, "paper_mode", False)):
        return {"checked": 0, "stopped": []}

    from position_tracker import get_open_positions

    stopped: list[str] = []
    checked = 0
    try:
        positions = await get_open_positions(settings.DB_PATH)
        paper_mis = [
            p for p in positions
            if p.get("source") == "PENNY_PAPER"
            and str(p.get("product_type") or "").upper() == "MIS"
        ]
        for position in paper_mis:
            checked += 1
            entry = float(position.get("entry_price") or 0.0)
            stop = float(
                position.get("trailing_stop_current")
                or position.get("stop_loss_initial")
                or 0.0
            )
            if stop <= 0:
                logger.error(
                    "penny_paper_stop_monitor_invalid_stop ticker=%s stop=%s",
                    position.get("ticker"), stop,
                )
                continue
            ltp = await _penny_ltp(position.get("ticker"), entry)
            if ltp > stop:
                continue
            result = await _execute_scheduled_penny_exit(
                position, reason="protective_stop_paper", reference_price=ltp,
            )
            settled = int((result.get("settlement") or {}).get("settled") or 0)
            if settled:
                stopped.append(str(position.get("ticker")))
                logger.info(
                    "penny_paper_stop_closed ticker=%s ltp=%.2f stop=%.2f "
                    "settled=%d",
                    position.get("ticker"), ltp, stop, settled,
                )
    except Exception as exc:
        # Paper bookkeeping must never prevent the live/paper signal scan.
        logger.error("penny_paper_stop_monitor_failed error=%s", str(exc))
    return {"checked": checked, "stopped": stopped}


async def run_penny_eod_check():
    """14:30 IST smart-EOD check on open MIS positions (spec §5.5).

    [CALENDAR-GATE 2026-07-03] P1 follow-up to PR-1. Skip on weekends +
    NSE holidays. With run_penny_force_close_mis also gated, no
    penny-MIS positions should be left in flight across weekends.
    """
    # [CALENDAR-GATE 2026-07-03] weekend / NSE-holiday early-return.
    today = datetime.now(IST).date()
    if not await is_trading_day(today, settings.DB_PATH):
        logger.info("penny_eod_check_skip reason=non_trading_day")
        return
    try:
        from penny_engine_breakout import smart_eod_check, time_stop_triggered
        from position_tracker import get_open_positions
        from penny_models import PennyLeg
        positions = await get_open_positions(settings.DB_PATH)

        # [TIER0-0.6/0.7 2026-07-14] THREE stacked bugs made this whole job a
        # no-op. It has never exited a single position:
        #
        #  1. It filtered on `p.get("leg") == "MIS"`. The positions table has NO
        #     `leg` column (it is `product_type`), so this list was ALWAYS empty
        #     and the loop below never ran once.
        #  2. It compared `decision.get("action") == "EXIT"`, but smart_eod_check
        #     returns "exit_now" / "hold" -- never "EXIT". So even with positions,
        #     the exit branch was unreachable and everything would HOLD.
        #  3. It read `p.get("current_price")`, another column that does not
        #     exist, and fell back to entry_price -- so smart_eod_check compared
        #     the entry price to itself, concluded "in profit, far from target",
        #     and held. Every time.
        #
        # Three independent reasons the answer was always HOLD. This is the penny
        # half of "we hold to the end of the day and never sell".
        penny_mis = [
            p for p in positions
            if p.get("product_type") == "MIS" and p.get("source") == _classic_penny_source()
        ]
        if not penny_mis:
            logger.info("penny_eod_check no_open_mis_positions")
            return
        exit_count = hold_count = 0
        for p in penny_mis:
            # Real LTP. Without it the check is comparing entry to entry.
            current_price = await _penny_ltp(p.get("ticker"), p.get("entry_price", 0.0))
            now_ist = datetime.now(IST)

            # [TIER0-0.6] PENNY_TIME_STOP_MIN was DEAD CONFIG: time_stop_triggered()
            # had zero non-test callers, so the 30-minute time stop could not fire.
            # A penny breakout that has not worked within 30 minutes is a failed
            # breakout; hold it and you are just donating the spread.
            if (
                current_price < p.get("entry_price", 0.0)
                and time_stop_triggered(p.get("entry_date"), now_ist)
            ):
                decision = {"action": "exit_now", "reason": "time_stop_30min_in_loss"}
            else:
                decision = smart_eod_check(p, current_price, now_ist)

            logger.info(
                "penny_eod_decision",
                ticker=p.get("ticker"),
                action=decision.get("action", "hold"),
                reason=decision.get("reason", ""),
                ltp=current_price,
            )
            if decision.get("action") == "exit_now":
                try:
                    exit_result = await _execute_scheduled_penny_exit(
                        p, reason="smart_eod",
                        reference_price=current_price,
                    )
                    settled = int((exit_result.get("settlement") or {}).get("settled") or 0)
                    if settled:
                        exit_count += 1
                    else:
                        hold_count += 1
                    logger.info(
                        "penny_eod_exit_result ticker=%s requested=%d confirmed=%d "
                        "settled=%d status=%s order_id=%s",
                        p.get("ticker"), p.get("shares"),
                        int(exit_result.get("confirmed_qty") or 0), settled,
                        exit_result.get("status"), exit_result.get("order_id"),
                    )
                except Exception as e:
                    logger.error(
                        "penny_eod_exit_failed ticker=%s error=%s",
                        p.get("ticker"), str(e),
                    )
            else:
                hold_count += 1
        logger.info("penny_eod_check_done exit=%d hold=%d", exit_count, hold_count)
    except Exception as e:
        logger.error("penny_eod_check_failed", error=str(e))


async def run_penny_force_close_mis():
    """
    [PENNY-G5 2026-06-25] Hard force-exit of ALL open MIS penny positions
    at 15:00 IST (PENNY_BREAKOUT_TIME_EXIT). This is mandatory -- MIS
    positions MUST NOT carry overnight because they use intraday product
    type and would be auto-squared-off by the broker at 15:20 anyway,
    but the broker auto-square-off can be at worse prices and we want a
    deterministic exit we control.

    Pre-2026-06-25 this was silently absent: mis_time_stop_active(now)
    was defined in penny_engine_breakout.py but never called anywhere.
    The 14:30 smart-EOD check (run_penny_eod_check) handles most cases,
    but if a position was held past 14:30 (e.g. fresh_loss branch fired
    hold), nothing else would force it out by 15:00. This is a real
    safety bug.

    This job is idempotent -- it just unwinds anything still open. If a
    position was already closed via EOD or SL-M, it's not in the open
    positions list and we skip it.

    [CALENDAR-GATE 2026-07-03] Skip on weekends + NSE holidays. Mis-time-stop
    force-exit on a non-trading day would place exit orders against stale
    weekend quotes for any positions held across the weekend (rare given the
    1-day holding pattern, but real). Mirrors main.py:1583-1588.
    """
    # [CALENDAR-GATE 2026-07-03] weekend / NSE-holiday early-return.
    today = datetime.now(IST).date()
    if not await is_trading_day(today, settings.DB_PATH):
        logger.info("penny_force_close_mis_skip reason=non_trading_day")
        return
    try:
        from penny_engine_breakout import mis_time_stop_active
        from position_tracker import get_open_positions
        from penny_models import PennyLeg

        now = datetime.now(IST)
        if not mis_time_stop_active(now):
            # Outside the 15:00 IST force-exit window -- nothing to do.
            return

        positions = await get_open_positions(settings.DB_PATH)
        penny_mis = [
            p for p in positions
            if p.get("product_type") == "MIS" and p.get("source") == _classic_penny_source()
        ]
        if not penny_mis:
            logger.info("penny_force_close_mis no_open_positions")
            return

        close_count = 0
        for p in penny_mis:
            try:
                current_price = await _penny_ltp(
                    p.get("ticker"), p.get("entry_price", 0.0),
                )
                exit_result = await _execute_scheduled_penny_exit(
                    p, reason="mis_time_stop",
                    reference_price=current_price,
                )
                settled = int((exit_result.get("settlement") or {}).get("settled") or 0)
                logger.warning(
                    "penny_force_close_mis_exit ticker=%s requested=%d confirmed=%d "
                    "settled=%d status=%s order_id=%s reason=15:00_IST_time_stop",
                    p.get("ticker"), p.get("shares"),
                    int(exit_result.get("confirmed_qty") or 0), settled,
                    exit_result.get("status"), exit_result.get("order_id"),
                )
                if settled:
                    close_count += 1
            except Exception as e:
                logger.error(
                    "penny_force_close_mis_failed ticker=%s error=%s",
                    p.get("ticker"), str(e),
                )
        logger.warning(
            "penny_force_close_mis_done confirmed_settlements=%d "
            "(15:00 IST time-stop fired)",
            close_count,
        )
    except Exception as e:
        logger.error("penny_force_close_mis_crashed error=%s", str(e))


async def _run_penny_daily_attribution():
    """
    [TIER3-DAILY-ATTRIBUTION 2026-06-25] 15:30 IST daily P&L summary.
    Reads from bankroll_ledger WHERE source='PENNY' for today and
    emits a compact Telegram message via the same transport as the
    hourly report. See penny_daily_attribution.build_daily_attribution
    for the message contract.

    [CALENDAR-GATE 2026-07-03] Skip on weekends + NSE holidays. On a
    non-trading day there is nothing to attribute; the Telegram message
    would claim "+Rs 0 / 0 trades today" anyway. Noise reduction.
    """
    # [CALENDAR-GATE 2026-07-03] weekend / NSE-holiday early-return.
    today = datetime.now(IST).date()
    if not await is_trading_day(today, settings.DB_PATH):
        logger.info("penny_daily_attribution_skip reason=non_trading_day")
        return
    try:
        from penny_daily_attribution import build_daily_attribution
        body = build_daily_attribution(
            db_path=settings.DB_PATH, source=_classic_penny_source()
        )
        # Local log (mandatory heartbeat -- matches hourly pattern).
        logger.info("penny_daily_attribution body=%s", body)
        # Telegram primary, webhook fallback -- reuse the transport
        # the hourly report uses, which has all the credentials wired.
        from penny_hourly_report import PennyHourlyReport
        sender = PennyHourlyReport(db_path=settings.DB_PATH)
        await sender.send(
            body=body,
            webhook_url=settings.PENNY_HOURLY_REPORT_WEBHOOK,
            telegram_token=settings.TELEGRAM_BOT_TOKEN,
            telegram_chat_id=settings.TELEGRAM_CHAT_ID,
        )
    except Exception as e:
        logger.error("penny_daily_attribution_crashed error=%s", str(e))


async def _run_penny_eod_digest():
    """
    [PHASE-C-EOD-DIGEST 2026-06-25] 16:00 IST end-of-day digest.

    Fires after the 15:30 daily attribution (T3-A) and the 15:00
    force-close (G5). Sends the operator a single Telegram message
    summarising both pools' P&L, open positions held overnight, and
    closing regimes. Body builder: operator_status.cmd_eod_digest /
    operator_status.build_eod_digest_snapshot_async.

    [ASYNC-SYNC-SPLIT 2026-07-01] Previously called cmd_eod_digest,
    which internally did asyncio.run(build_status_snapshot(...)) --
    that raises RuntimeError from inside this already-running event
    loop, so the digest was silently silent every 16:00 IST. Now we
    await the async snapshot builder directly and feed it through
    format_eod_digest. Loud-but-non-blocking: any exception is logged
    and swallowed so the scheduler keeps running.

    [CALENDAR-GATE 2026-07-03] Skip on weekends + NSE holidays. The
    EOD digest at 16:00 IST on a Saturday would publish a misleading
    "today's P&L summary" for a non-trading day.
    """
    # [CALENDAR-GATE 2026-07-03] weekend / NSE-holiday early-return.
    today = datetime.now(IST).date()
    if not await is_trading_day(today, settings.DB_PATH):
        logger.info("penny_eod_digest_skip reason=non_trading_day")
        return
    try:
        from operator_status import (
            build_eod_digest_snapshot_async,
            format_eod_digest,
        )
        snap = await build_eod_digest_snapshot_async(settings.DB_PATH)
        body = format_eod_digest(snap)
        logger.info("penny_eod_digest_sent")
        from penny_hourly_report import PennyHourlyReport
        sender = PennyHourlyReport(db_path=settings.DB_PATH)
        await sender.send(
            body=body,
            webhook_url=settings.PENNY_HOURLY_REPORT_WEBHOOK,
            telegram_token=settings.TELEGRAM_BOT_TOKEN,
            telegram_chat_id=settings.TELEGRAM_CHAT_ID,
        )
    except Exception as e:
        logger.error("penny_eod_digest_crashed error=%s", str(e))


async def _run_penny_heatmap():
    """
    [TIER3-POSITION-HEATMAP 2026-06-25] Mid-day position heat-map.
    Scheduled every 15 minutes during market hours. Reads open penny
    positions, fetches live prices in one batched Kite call, and
    emits a sector-grouped heatmap via the same transport as the
    hourly report.

    [CALENDAR-GATE 2026-07-03] P1 follow-up to PR-1. Skip on weekends +
    NSE holidays. Heatmap from a stale portfolio is misleading on
    non-trading days.
    """
    # [CALENDAR-GATE 2026-07-03] weekend / NSE-holiday early-return.
    today = datetime.now(IST).date()
    if not await is_trading_day(today, settings.DB_PATH):
        logger.info("penny_heatmap_skip reason=non_trading_day")
        return
    try:
        from penny_heatmap import build_heatmap
        body, _buckets, total_open, priced_count = await build_heatmap(
            db_path=settings.DB_PATH,
            kite=kite,
            sectors_csv_path=settings.PENNY_SECTORS_CSV_PATH,
            # [AUDIT-FIX-2.5] Read the operator-tuned threshold from
            # config. Default 0.01 (1%) matches pre-fix hardcoded value.
            near_sl_warn_pct=settings.PENNY_HEATMAP_WARN_PCT,
            warn_pct_is_fraction=True,  # config is a fraction, not percent
            source=_classic_penny_source(),
        )
        logger.info(
            "penny_heatmap_sent total_open=%d priced=%d",
            total_open, priced_count,
        )
        # Only send if there are open positions (don't spam Telegram
        # with empty messages every 15 min when nothing's open).
        if total_open == 0:
            return
        from penny_hourly_report import PennyHourlyReport
        sender = PennyHourlyReport(db_path=settings.DB_PATH)
        await sender.send(
            body=body,
            webhook_url=settings.PENNY_HOURLY_REPORT_WEBHOOK,
            telegram_token=settings.TELEGRAM_BOT_TOKEN,
            telegram_chat_id=settings.TELEGRAM_CHAT_ID,
        )
    except Exception as e:
        logger.error("penny_heatmap_crashed error=%s", str(e))


async def run_penny_hourly_report():
    """Top-of-hour status report (spec §9.4). 10:00 through 14:00 IST.

    [CALENDAR-GATE 2026-07-03] P1 follow-up to PR-1. Skip on weekends +
    NSE holidays. The Telegram status message is meaningful only when
    penny might have taken positions during the day.
    """
    # [2026-07-03 BUG-FIX] Use module-level `datetime` (already imported at
    # top of file as `from datetime import datetime`). The original code
    # later did `from datetime import date, datetime as _dt` inside the
    # try block; that re-bind shadows our module-level `datetime` and
    # would crash the calendar gate with UnboundLocalError. Found by
    # PR-2 functional tests.
    # [MARKET-HOURS-GATE 2026-09-02] The scheduler is also hour-bounded, but
    # keep this explicit IST gate so manual calls, stale scheduler instances,
    # and UTC-configured containers cannot emit the heartbeat after hours.
    from penny_hourly_report import is_in_report_window
    now_ist = datetime.now(IST)
    if not is_in_report_window(now_ist):
        return
    # [CALENDAR-GATE 2026-07-03] weekend / NSE-holiday early-return.
    today = now_ist.date()
    if not await is_trading_day(today, settings.DB_PATH):
        logger.info("penny_hourly_report_skip reason=non_trading_day")
        return
    try:
        from penny_hourly_report import run_hourly_report
        from position_tracker import get_open_positions
        from penny_risk import PennyRiskEngine
        positions = await get_open_positions(settings.DB_PATH)
        penny_source = _classic_penny_source()
        penny_pos = [p for p in positions if p.get("source") == penny_source]
        bankroll = settings.PENNY_PAPER_BANKROLL if not settings.PENNY_LIVE_TRADING else settings.PENNY_LIVE_BANKROLL
        # 2026-06-24 bankroll fix: pass the module-level ledger_writer so
        # penny P&L closes flow into the dashboard's bankroll_ledger with
        # source='PENNY'.
        risk = PennyRiskEngine(
            bankroll=bankroll,
            ledger_writer=_make_penny_ledger_writer(penny_source),
        )
        deployed = sum((p.get("entry_price", 0.0) * p.get("shares", 0)) for p in penny_pos)
        # [MTM-WIRED 2026-09-13] Replace the silent-zero expression with
        # ``mark_to_market.mark_open_positions`` so the hourly report
        # reflects actual LTP. Pre-fix the orchestrator read
        # ``p.get("current_price", 0.0)``; because ``positions`` has no
        # ``current_price`` column, every row contributed
        # ``-entry_price * shares`` and the report silently produced a
        # false -Rs 30,000 loss on a 10-share TCS position. Now we
        # fetch a single batched Kite quote per ticker (one call, not
        # one per row) and feed the result into the MTM module.
        # When Kite has no access_token (paper-mode CI, after-hours
        # cron in a fresh container, etc.) we degrade to UNKNOWN
        # quotes rather than crashing the report.
        unrealised = None if penny_pos else 0.0
        try:
            from datetime import timezone as _tz
            from mark_to_market import (
                mark_open_positions, QuoteTick,
                MAX_QUOTE_AGE_SECONDS,
            )
            _quote_cache: dict = {}
            _now_utc = datetime.now(_tz.utc)
            if kite.access_token and penny_pos:
                # Resolve ticker -> instrument token via the Kite
                # instrument cache, then batch-fetch LTP for all
                # tickers in one call (not one-per-row).
                _tokens: list = []
                for _p in penny_pos:
                    _tk_raw = _p.get("instrument_token") or _p.get("token")
                    _tk = None
                    if _tk_raw is not None:
                        try:
                            _tk = int(_tk_raw)
                        except (TypeError, ValueError):
                            _tk = None
                    if _tk is None:
                        # Fall back to the instrument cache by ticker.
                        _tk = kite.instrument_cache.get(_p.get("ticker", ""))
                    if isinstance(_tk, int) and _tk > 0:
                        _tokens.append(_tk)
                if _tokens:
                    try:
                        _raw_quotes = await kite.get_quote(_tokens)
                        for _tk, _payload in _raw_quotes.items():
                            if isinstance(_payload, dict):
                                _lp = _payload.get("last_price")
                                if isinstance(_lp, (int, float)):
                                    _quote_cache[int(_tk)] = QuoteTick.build(
                                        float(_lp), _now_utc,
                                    )
                    except Exception as _exc:
                        logger.warning(
                            "penny_hourly_mtm_kite_quote_failed err=%s",
                            type(_exc).__name__,
                        )
            _mtm = mark_open_positions(
                equity_rows=penny_pos,
                fno_rows=[],
                fno_dr_rows=[],
                quotes=_quote_cache,
                now_utc=_now_utc,
                freshness_seconds=MAX_QUOTE_AGE_SECONDS,
            )
            unrealised = _mtm.complete_unrealised_pnl
            from mark_to_market import QuoteStatus as _QS
            _stale_count = _mtm.count_by_status.get(_QS.STALE, 0)
            if _stale_count:
                logger.info(
                    "penny_hourly_mtm_stale_quotes count=%d total=%d",
                    _stale_count, len(_mtm.marks),
                )
        except Exception as _exc:
            # Preserve unknown valuation in the operator report, not zero.
            logger.warning(
                "penny_hourly_mtm_unavailable err=%s", type(_exc).__name__,
            )
            unrealised = None if penny_pos else 0.0
        # [AUDIT-FIX-2.4] Plumb universe as_of / age_days into the hourly
        # report so stale data is visible to the operator. Read directly
        # from the JSON (cheap -- one file read + 2 string fields).
        try:
            import json as _json
            with open(PENNY_UNIVERSE_JSON_PATH) as _f:
                _uni_meta = _json.load(_f)
            _uni_as_of = _uni_meta.get("as_of")
        except Exception:
            _uni_as_of = None
        _uni_age_days = None
        if _uni_as_of:
            try:
                from datetime import date, datetime as _dt
                _uni_age_days = (date.today() - _dt.strptime(_uni_as_of, "%Y-%m-%d").date()).days
            except Exception as e:
                # [ROADMAP-4.3] Cosmetic (the report just omits the age),
                # but a parse failure here means the universe's as_of stamp
                # is malformed -- worth one debug line, not silence.
                logger.debug("penny_universe_age_parse_failed as_of=%s error=%s",
                             _uni_as_of, str(e))
        await run_hourly_report(
            db_path=settings.DB_PATH,
            regime=_penny_regime_engine.today_regime.value
            if _penny_regime_engine.today_regime is not None
            else "PR2_ELEVATED",  # [ROADMAP-3.6] fail-open is PR2 now
            open_positions=penny_pos,
            deployed_capital=deployed,
            unrealised_pnl=unrealised,
            kill_switch_active=risk.kill_switch_active(),
            circuit_blocks=0,
            universe_size=_last_penny_scan_universe_size,
            universe_as_of=_uni_as_of,
            universe_age_days=_uni_age_days,
            now=now_ist,
        )
    except Exception as e:
        logger.error("penny_hourly_report_failed", error=str(e))


# Module-level breadth engine global -- set once when the feature flag is on,
# reused across scan cycles. Mirrors the `risk_engine` pattern above.
breadth_engine = None


# -- Breadth wiring helpers (Task 7, 2026-06-14) ------------------
# These are extracted as module-level functions so they're testable in
# isolation. The scan loop in run_screener() calls them in two places:
#   1) Once at scan start:  breadth_engine = build_breadth_engine(kite, settings)
#   2) Once per ticker:     kwargs.update(build_breadth_kwargs(token, breadth_result))
# The helpers are pure (no I/O of their own) so tests can drive them with
# mocks and small tmp data dirs.

def build_breadth_engine(kite, settings):
    """Build the BreadthEngine singleton. Returns None when the feature flag
    is off or Universe load fails (fail-soft -- engine.py will just no-op).

    Wraps kite.get_historical (which is symbol-keyed) behind a token-keyed
    adapter using Universe.token_to_symbol().
    """
    if not settings.BREADTH_ENRICHMENT_ENABLED:
        return None
    try:
        from universe import Universe  # local import keeps main.py import-clean

        breadth_universe = Universe(
            os.path.join(os.path.dirname(__file__), settings.BREADTH_DATA_DIR, "nifty100.json"),
            instrument_cache=kite.instrument_cache,
        )

        async def kite_historical_async(token: int, period: str, interval: str):
            # Universe stores tokens; kite.get_historical wants a symbol.
            symbol = breadth_universe.token_to_symbol(token)
            if symbol is None:
                raise ValueError(f"token {token} not in Nifty 100 universe")
            # Convert period+interval ("60d" / "day") to a date range.
            days = int(period.rstrip("d")) if period.endswith("d") else 60
            tz = pytz.timezone("Asia/Kolkata")
            to_date = datetime.now(tz).strftime("%Y-%m-%d")
            from_date = (datetime.now(tz) - timedelta(days=days)).strftime("%Y-%m-%d")
            return await kite.get_historical(symbol, from_date, to_date)

        engine = BreadthEngine(
            universe=breadth_universe,
            kite_historical_fn=kite_historical_async,
            cache_ttl_seconds=settings.BREADTH_CACHE_TTL_SECONDS,
            degraded_threshold=settings.BREADTH_DATA_DEGRADED_THRESHOLD,
            tier1_parallelism=settings.BREADTH_TIER1_PARALLELISM,
        )
        logger.info(
            "breadth_engine_enabled",
            tokens=len(breadth_universe.get_tokens()),
        )
        return engine
    except Exception as e:
        logger.error("breadth_engine_init_failed", error=str(e))
        return None


def build_breadth_kwargs(token, breadth_result) -> dict:
    """Return the kwargs dict to pass to evaluate_signal() for a single ticker.

    Pulls the token's breadth rank and the engine-wide breadth_pct_above_sma50.
    Returns {} when the engine is not initialized, the token is missing/None,
    or the token is not in the Nifty 100 universe (small-caps outside breadth
    coverage). engine.py treats empty kwargs as "no breadth adjustment" -- the
    existing scoring path runs untouched.
    """
    if breadth_result is None:
        return {}
    if token is None or token not in breadth_result.rank_map:
        return {}
    return {
        "breadth_pct_above_sma50": breadth_result.breadth_pct_above_sma50,
        "breadth_rank": breadth_result.rank_map[token],
    }


async def _filter_by_liquidity(
    universe: pd.DataFrame,
    kite,
    today: pd.Timestamp,
) -> pd.DataFrame:
    """Drop tickers below the 20-day median ADV floor (DD2).

    For each ticker in the universe, fetch the last 20 days of OHLCV,
    compute median daily traded value (close × volume), and drop any
    ticker whose median is below `UNIVERSE_MIN_ADV_CRORE`.

    Returns the filtered DataFrame. Failures (empty df, fetch error)
    result in the ticker being dropped — better to skip a name than
    to enter a position without liquidity data.

    If `UNIVERSE_MIN_ADV_CRORE <= 0`, returns the input unchanged
    (escape hatch for "disable filtering" via .env).
    """
    from config import settings as cfg
    min_adv_crore = cfg.UNIVERSE_MIN_ADV_CRORE
    lookback_days = cfg.UNIVERSE_LIQUIDITY_LOOKBACK_DAYS

    if min_adv_crore <= 0:
        return universe

    from datetime import timedelta
    from_date = (today - timedelta(days=lookback_days + 5)).strftime("%Y-%m-%d")
    to_date = today.strftime("%Y-%m-%d")

    kept_rows = []
    dropped = 0
    for _, row in universe.iterrows():
        ticker = row["tradingsymbol"]
        try:
            df = await kite.get_historical(ticker, from_date, to_date)
            if df.empty or len(df) < lookback_days // 2:
                dropped += 1
                continue
            # Compute median traded value
            traded_value = (df["close"] * df["volume"]).tail(lookback_days)
            median_tv_crore = float(traded_value.median()) / 1e7  # ₹ → ₹ crore
            if median_tv_crore >= min_adv_crore:
                kept_rows.append(row)
            else:
                dropped += 1
        except Exception as e:
            logger.warning("liquidity_filter_fetch_failed", ticker=ticker, error=str(e))
            dropped += 1

    if dropped > 0:
        logger.info(
            "liquidity_filter_complete",
            kept=len(kept_rows),
            dropped=dropped,
            threshold_crore=min_adv_crore,
        )
    return pd.DataFrame(kept_rows) if kept_rows else universe.iloc[0:0]


def snap_to_tick(price: float, direction: int = -1) -> float:
    """
    Snap a price to the nearest valid NSE tick (0.10 rupee).
    0.10 is the LCM of all NSE equity tick sizes (0.05 and 0.10).
    direction=-1 -> round DOWN (sell orders, ensures limit is below current price)
    direction=+1 -> round UP  (buy orders)
    Uses integer arithmetic to avoid IEEE-754 floating-point drift.
    """
    import math
    in_tenths = round(price * 10 * 100) / 100  # guard against micro-errors
    fn = math.ceil if direction >= 0 else math.floor
    return fn(in_tenths) / 10

# Shared State
state_lock = asyncio.Lock()

# [MOMENTUM-CAS-BOUNDARY 2026-09-16] One owner may mutate momentum exits at a
# time.  EOD ownership begins before the 15:15 CAS reference-price window and
# no new square-off may be submitted at/after the hard cutoff.
MOMENTUM_EOD_OWNERSHIP_HOUR = 15
MOMENTUM_EOD_OWNERSHIP_MINUTE = 13
MOMENTUM_EOD_SUBMIT_CUTOFF_SECOND = 15 * 60 * 60 + 14 * 60 + 30
_momentum_exit_ownership_lock = asyncio.Lock()


def _momentum_exit_clock(now_ist=None) -> dict[str, bool]:
    """Pure ownership/deadline verdict for live momentum exit writers."""
    observed = now_ist or datetime.now(IST)
    second = observed.hour * 3600 + observed.minute * 60 + observed.second
    ownership_start = (
        MOMENTUM_EOD_OWNERSHIP_HOUR * 3600
        + MOMENTUM_EOD_OWNERSHIP_MINUTE * 60
    )
    return {
        "monitor_allowed": second < ownership_start,
        "eod_started": second >= ownership_start,
        "submit_allowed": second < MOMENTUM_EOD_SUBMIT_CUTOFF_SECOND,
    }
current_signals = []
rejected_signals = []
current_momentum_signals = []
market_regime = "UNKNOWN"
last_run = None

# Risk engine singleton -- initialized in post_login_initialization
# after bankroll and regime risk_pct are known. Governs position sizing,
# partial exits, and post-drawdown recovery sizing.
risk_engine = None

# Regime engine singleton -- initialized once on first scan and reused
# across subsequent scheduler runs (09:20 and 14:45 IST). This preserves
# _consecutive_in_range counter and current_regime for the 2-scan
# confirmation logic for regime transitions.
_global_regime_engine = None

# Tracks when a Regime-3 (Crisis) scan was observed so daily_post_market
# can enter drawdown recovery mode after a crisis period ends.
_last_regime_was_crisis = False

# Stores the last RegimeState from run_screener so get_signals() can
# expose regime + regime_score in the PortfolioResponse.
_last_regime_state = None

# [MR-3REG] Cached 3-regime state for the momentum screener. Set by
# run_screener() after it computes the daily regime at 09:20 IST, then
# read by run_momentum_screener() on each of its 19 daily scans.
# Momentum does NOT recompute regime intraday (would flip too often with
# 19 scans/day); it carries forward the swing's value for the day.
# Set to Regime.REGIME_1_NORMAL (or whatever the engine returns) by swing;
# to None on startup until the first swing scan completes.
_momentum_regime_for_today = None

# Guard against concurrent post_login_initialization runs.
# node-gateway retries the /token endpoint up to 4 times (3 retries + initial)
# because post_login_initialization blocks for 20+ seconds while the handler
# has a 2-second AbortController. Without this flag, 4 concurrent screener
# runs fire simultaneously, each fetching the full universe from the Kite API.
_init_running = False

# [CRIT] FIX: Add short-term memory to prevent 15-minute spam
signaled_momentum_today = set()
last_momentum_date = None

# [MOM-FUNNEL 2026-07-11] Cumulative accepted momentum signals for the
# current trading day, deduped by ticker (first accept wins -- the alert
# the operator saw). current_momentum_signals is only the LATEST scan's
# snapshot, overwritten every 15 minutes; on 2026-07-10 the agent's
# hourly poll of that snapshot saw 3 of 17 accepted signals and the
# gateway's EXEC-button lookup failed for any signal older than one
# scan ("Momentum signal not found in Engine state"). /momentum-signals
# now serves THIS list. Reset with signaled_momentum_today on day roll.
momentum_signals_today = []


# @app.on_event("startup")
# async def startup():
#     await init_positions_db(settings.DB_PATH)
#     await init_ledger(settings.DB_PATH)
#     await kite.refresh_instrument_cache()
    
    # Run backtest if not run
#    try:
 #       df = await kite.get_historical("NSE: RELIANCE", "2015-01-01", datetime.utcnow().strftime("%Y-%m-%d"))
 #       if not df.empty:
 #           await run_backtest(settings.DB_PATH, {"NSE: RELIANCE": df}, settings.STRATEGY_VERSION)
  #  except Exception as e:
  #      logger.error("initial_backtest_error", error=str(e))
        
    # scheduler.add_job(kite.refresh_instrument_cache, 'cron', hour=8, minute=0)
    # scheduler.add_job(run_screener, 'cron', hour=9, minute=20)
    # scheduler.add_job(run_screener, 'cron', hour=14, minute=45)
    # scheduler.add_job(daily_post_market, 'cron', hour=15, minute=45)
    # scheduler.start()

IST = pytz.timezone("Asia/Kolkata")

from contextlib import asynccontextmanager


def _fno_regime_str() -> str:
    """Read-only view of the swing regime for the F&O gate (spec §7.2).
    Sizing is unaffected (always 1-2 lots); the regime gate is on/off."""
    try:
        if _last_regime_state is not None:
            return _last_regime_state.regime.value
    except Exception as e:
        # [ROADMAP-4.3 2026-07-13] "UNKNOWN" closes the F&O regime gate, so
        # a silent exception here disables the whole F&O book and looks
        # identical to a legitimately unclassified market. Log it.
        logger.warning("fno_regime_read_failed error=%s", str(e))
    return "UNKNOWN"


async def lifespan(app: FastAPI):
    # [AUDIT-FIX-2.2 2026-06-25] Loud startup warning if INTERNAL_API_SECRET
    # is empty. The auth gate (in `_check_internal_secret`) also
    # catches this at request time, but seeing it at startup is the
    # most visible -- a misconfigured deployment should never get
    # through init silently.
    if not settings.INTERNAL_API_SECRET:
        logger.critical(
            "internal_api_secret_not_configured_at_startup "
            "FIX=set INTERNAL_API_SECRET env var. Internal endpoints will "
            "return HTTP 503 until configured. Scanner + read-only "
            "endpoints continue normally (operator mandate: don't block "
            "the system during market hours)."
        )
        # [HIGH-001 2026-07-12] Also page the operator at boot, not just on
        # the first blocked request (which could be hours later, mid-day).
        # Fire-and-forget; a notify failure must not block startup.
        asyncio.create_task(_send_internal_secret_alert())

    db_dir = os.path.dirname(settings.DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    await init_positions_db(settings.DB_PATH)
    await init_ledger(settings.DB_PATH)
    # [ANALYTICS 2026-06-16] Create the trade_outcomes table for the
    # self-improvement loop. Idempotent.
    from analytics import init_analytics_db
    await init_analytics_db(settings.DB_PATH)
    # [SMIT-FYERS-OPTIONS 2026-09-25] TradingView -> Fyers signal log. Idempotent.
    from tv_fyers_signal import init_tv_fyers_signal_db
    await init_tv_fyers_signal_db(settings.DB_PATH)
    # [ROADMAP-2.8 2026-07-12] ops_liveness_daily / ops_funnel_daily.
    try:
        from ops_metrics import init_ops_metrics_db
        await init_ops_metrics_db(settings.DB_PATH)
    except Exception as _ops_exc:
        logger.warning("ops_metrics_init_failed err=%s", str(_ops_exc))

    asyncio.create_task(kite.refresh_instrument_cache())
    scheduler.add_job(kite.refresh_instrument_cache, 'cron', hour=8, minute=0)
    scheduler.add_job(run_screener, 'cron', hour=9, minute=20)
    scheduler.add_job(run_screener, 'cron', hour=14, minute=45)
    scheduler.add_job(daily_post_market, 'cron', hour=15, minute=45)
    scheduler.add_job(momentum_eod_warning, 'cron', hour=15, minute=10, id="momentum_eod_warning")
    # [MOMENTUM-CAS-BOUNDARY 2026-09-16] Start at 15:13 so the final
    # broker submission precedes the 15:15 CAS reference-price window.
    # When True, let momentum winners run past 3:15 IST (operator takes the risk).
    if not settings.MOMENTUM_ALLOW_OVERNIGHT:
        scheduler.add_job(
            auto_square_momentum, 'cron', hour=MOMENTUM_EOD_OWNERSHIP_HOUR,
            minute=MOMENTUM_EOD_OWNERSHIP_MINUTE, id="momentum_auto_square",
            max_instances=1, coalesce=True, misfire_grace_time=60,
        )
    else:
        logger.info("momentum_overnight_enabled", message="15:13 auto-square DISABLED per MOMENTUM_ALLOW_OVERNIGHT=True")

    # [TIER0-0.1 2026-07-14] Manage open MIS momentum positions intraday.
    # Without this the ONLY jobs touching a momentum position were the 15:10
    # warning and the 15:15 square-off -- so the stop and target computed at entry
    # were enforced by nothing, and all 7 momentum trades ever taken exited on the
    # clock rather than on a target or a stop. max_instances=1: a slow Kite call
    # must not stack monitors on top of each other and double-cancel an SL-M.
    scheduler.add_job(
        momentum_intraday_monitor, 'interval',
        seconds=settings.MOMENTUM_INTRADAY_MONITOR_SEC,
        id="momentum_intraday_monitor", max_instances=1,
    )

    # [MOMENTUM-PAPER 2026-07-26] The paper book's own monitor + square-off.
    # Deliberately NOT folded into the live jobs above: those are dense with
    # broker calls (SL-M reconcile, order cancel, square-off POST) and threading
    # a "paper" flag through them would put the paper book one bad branch away
    # from placing a real order. momentum_paper has no order-placing code at all,
    # so the separation is structural rather than conditional.
    if settings.MOMENTUM_PAPER_ENABLED:
        scheduler.add_job(
            _run_momentum_paper_monitor, 'interval',
            seconds=settings.MOMENTUM_INTRADAY_MONITOR_SEC,
            id="momentum_paper_monitor", max_instances=1,
        )
        scheduler.add_job(
            _run_momentum_paper_square_off, 'cron', hour=15, minute=15,
            id="momentum_paper_square_off", max_instances=1,
            coalesce=True, misfire_grace_time=600,
        )

    for hour in [10, 11, 12, 13, 14]:
        for minute in [0, 15, 30, 45]:
            if hour == 10 and minute == 0:
                continue
            scheduler.add_job(run_momentum_screener, 'cron', hour=hour, minute=minute, id=f"momentum_scan_{hour}{minute}")

    scheduler.add_job(kite.clear_intraday_cache, 'cron', hour=0, minute=5, id="intraday_cache_cleanup")

    # [ROADMAP-2.1 2026-07-12] Scans-vs-execution token reconciliation.
    # Function self-gates to 09:15-15:30 IST trading days; defined near
    # the /token endpoint with the other token-lifecycle code.
    scheduler.add_job(
        _token_reconciliation_tick, 'cron', minute='*/15',
        id="token_reconciliation",
        max_instances=1, coalesce=True, misfire_grace_time=300,
    )

    # [ROADMAP-2.4 2026-07-12] Loop-progress tick for the agent's
    # external freeze watchdog. Runs 24/7 (NOT market-gated) so the
    # watchdog can't false-positive outside scan windows: a stale file
    # always means the scheduler stopped firing jobs.
    scheduler.add_job(
        _scheduler_tick_job, 'interval', seconds=60,
        id="scheduler_tick",
        max_instances=1, coalesce=True, misfire_grace_time=30,
    )

    # [ROADMAP-2.6 2026-07-12] OCI relay / Kite endpoint liveness probe.
    # Function self-gates to 09:15-15:30 IST trading days.
    scheduler.add_job(
        _kite_endpoint_probe_tick, 'cron', minute='*/3',
        id="kite_endpoint_probe",
        max_instances=1, coalesce=True, misfire_grace_time=120,
    )

    # [ROADMAP-2.8 2026-07-12] Persist the day's gate funnels after close.
    # Function self-gates on is_trading_day.
    scheduler.add_job(
        _ops_daily_snapshot, 'cron', hour=15, minute=50,
        id="ops_daily_snapshot",
        max_instances=1, coalesce=True, misfire_grace_time=3600,
    )

    # [OUTAGE-2026-07-13 DEFECT 3+4] Trading-readiness watchdog. Asks, every 5
    # minutes during market hours, the one question none of the other watchdogs
    # asked on 2026-07-13: "the market is open -- CAN I TRADE?"
    #
    # The engine ran unarmed for six hours that day while the scheduler ticked,
    # /health returned 200, every container reported healthy, and not one
    # Telegram message was sent. Single-sided by design: unlike the token
    # reconciliation cron it does not need node-gateway to be reachable, so a
    # second fault cannot silence it. Function self-gates to 09:30-15:15 IST on
    # trading days.
    scheduler.add_job(
        _trading_readiness_tick, 'cron', minute='*/5',
        id="trading_readiness",
        max_instances=1, coalesce=True, misfire_grace_time=120,
    )

    # 2026-06-22: daily reset of penny risk state at 00:05 IST (05:30 UTC isn't right;
    # 00:05 UTC = 05:35 IST, just after midnight IST).
    def _penny_daily_reset():
        from penny_risk import PennyRiskEngine
        from config import settings
        bankroll = settings.PENNY_PAPER_BANKROLL if _penny_scanner is None or _penny_scanner.paper_mode else settings.PENNY_LIVE_BANKROLL
        # 2026-06-24 bankroll fix: re-attach the shared ledger_writer after
        # the daily reset (the new risk engine replaces the old singleton).
        source_tag = _classic_penny_source(
            _penny_scanner.paper_mode if _penny_scanner is not None
            else not settings.PENNY_LIVE_TRADING
        )
        new_risk = PennyRiskEngine(
            bankroll=bankroll,
            ledger_writer=_make_penny_ledger_writer(source_tag),
        )
        if _penny_scanner is not None:
            _penny_scanner.risk_engine = new_risk
        # [RETRY-STORM 2026-07-31] Broker MIS-blocked / ban lists are published
        # per session, so yesterday's per-ticker entry blocks must not persist.
        from penny_executor import reset_entry_blocks
        reset_entry_blocks()
        logger.info("penny_daily_reset bankroll=%s", bankroll)
    scheduler.add_job(_penny_daily_reset, 'cron', hour=0, minute=5, id="penny_daily_reset")
    # [PENNY-MAIN 2026-06-21] 7 penny subsystem scheduler jobs.
    # All gated by PENNY_* feature flags + settings; failures isolated.
    # Extracted to a module-level function so the test suite can verify
    # registration without booting the FastAPI lifespan.
    register_penny_scheduler_jobs(scheduler)
    # [FNO 2026-07-10] F&O subsystem jobs (instruments refresh, scan tick,
    # hourly report, zero-accept watchdog). Paper-only in P1; the live leg
    # refuses to arm until fno_go_live_check() returns [].
    register_fno_scheduler_jobs(scheduler)
    # [PARTNER-TIPS 2026-07-18] Partner tips bot jobs. Registration is
    # unconditional; every job's first line checks PARTNER_BOT_ENABLED
    # and returns instantly when off (zero Kite calls, zero sends).
    register_partner_scheduler_jobs(scheduler)
    # [SMIT-FYERS-OPTIONS 2026-09-25] TradingView -> Fyers options pipeline
    # exit-management tick. Paper-only, triple-disarmed (see config.py);
    # self-gates to market hours and skips entirely until a Fyers token
    # is armed (no login flow wired up yet).
    register_tv_fyers_scheduler_jobs(scheduler)

    # Durable timing facts complement the existing liveness heartbeat.  The
    # listener records APScheduler misfires/max-instance rejections while the
    # selected latency-sensitive jobs record actual completion timing.  It is
    # observational only and cannot alter scan, exit, delivery or order paths.
    from scheduler_telemetry import attach_scheduler_listener
    attach_scheduler_listener(scheduler, settings.DB_PATH)

    # [FNO 2026-07-10] fno tables exist before the first tick (rule 57
    # preflight would catch it, but creating them at startup keeps the
    # first day's log complete).
    try:
        from fno_positions import init_fno_positions_db
        from fno_signal_log import init_fno_signal_db
        await init_fno_positions_db(settings.DB_PATH)
        await init_fno_signal_db(settings.DB_PATH)
        # [PARTNER-TIPS 2026-07-18] OI store + partner dedup tables.
        from fno_oi_store import init_oi_db
        from partner_orchestrator import init_partner_db
        from hedge_advisory import init_hedge_advisory_db
        await init_oi_db(settings.DB_PATH)
        await init_partner_db(settings.DB_PATH)
        await init_hedge_advisory_db(settings.DB_PATH)
    except Exception as _fno_db_exc:
        logger.warning("fno_db_init_failed err=%s", str(_fno_db_exc))

    # [LIVENESS-HEARTBEAT 2026-07-07] Per-minute liveness tick. The
    # 2026-07-07 incident showed the penny 30s scanner + scheduler
    # froze for 6h32min during market hours without ANY visible signal
    # to the operator. The breadcrumbs in penny_edge_scan / penny_edge_exit
    # detect "did the handler run" but NOT "is the scheduler event loop
    # blocked". A per-minute tick from a SEPARATE THREAD catches the
    # freeze case: if the asyncio event loop is frozen, this thread
    # (which uses stdlib time.sleep + stdlib logging) keeps ticking
    # and the operator can grep
    #     docker logs python-engine --since 10m | grep penny_liveness_tick
    # and see fresh ticks => loop is responsive; missing ticks for
    # 5+ minutes => freeze alert.
    #
    # We use stdlib logging (NOT structlog) for this heartbeat because
    # structlog's `cache_logger_on_first_use=True` means the heartbeat
    # might end up using a cached wrapper from before configure_structlog
    # was called. Stdlib logging is always going through the same
    # StreamHandler we attached at startup.
    import logging as _stdlib_logging
    _liveness_log = _stdlib_logging.getLogger("penny_liveness")

    def _liveness_tick_loop():
        """Stdlib-time heartbeat in a daemon thread. Decoupled from the
        asyncio loop so a frozen event loop doesn't silence the tick."""
        import threading as _threading
        import time as _time
        from memory_metrics import current_rss_kb, peak_rss_kb
        tick_count = 0
        while not _LIVENESS_TICK_STOP.is_set():
            try:
                tick_count += 1
                # `rss_kb` is deliberately the *current* resident set.  Keep
                # the non-decreasing ru_maxrss separately so a temporary
                # scanner peak is not misdiagnosed as a live memory leak.
                rss_kb = current_rss_kb()
                max_rss_kb = peak_rss_kb()
                _liveness_log.info(
                    "penny_liveness_tick count=%d rss_kb=%d max_rss_kb=%d threads=%d",
                    tick_count,
                    int(rss_kb),
                    int(max_rss_kb),
                    _threading.active_count(),
                )
            except Exception as exc:
                # Heartbeat itself must NEVER crash. If stdlib logging
                # is also broken, swallow and keep ticking.
                try:
                    sys.stderr.write(
                        f"[penny_liveness_tick_failed err={exc}]\n"
                    )
                    sys.stderr.flush()
                except Exception:
                    pass
            # Sleep 60s in 5s chunks so a container stop signal is
            # picked up within 5s (not 60s).
            for _ in range(12):
                if _LIVENESS_TICK_STOP.is_set():
                    return
                _time.sleep(5)

    import threading as _threading
    _LIVENESS_TICK_STOP = _threading.Event()
    _liveness_thread = _threading.Thread(
        target=_liveness_tick_loop,
        name="penny-liveness-heartbeat",
        daemon=True,  # dies with the process; no shutdown coordination needed
    )
    _liveness_thread.start()

    # [FIX-PHASE3-AUDIT 2026-07-09] Re-arm from the persisted same-day
    # token (if any) so a mid-day container restart doesn't silently
    # disarm every strategy until the operator manually logs in again.
    # restore_kite_token_if_fresh is defined near the /token endpoint;
    # by lifespan-run time the module is fully imported.
    try:
        if restore_kite_token_if_fresh():
            asyncio.create_task(post_login_initialization())
    except Exception as e:
        logger.warning("kite_token_restore_startup_failed error=%s", str(e))

    scheduler.start()
    # [STARTUP-ATTESTATION 2026-08-13] Write proof immediately after
    # APScheduler starts.  A bounded `docker logs --tail` audit once mistook
    # lines displaced by cache noise for a 6.5-hour scheduler outage.  Waiting
    # for the normal 60-second interval leaves the same ambiguity at boot.
    #
    # This deliberately does NOT force-run any trading job: cron/interval
    # semantics remain untouched.  It writes the existing loop-progress
    # heartbeat, recovers any cross-process gap from its prior timestamp, and
    # records the registered-job count while the process is known to be alive.
    startup_tick = await _scheduler_tick_job(recover_previous=True)
    logger.info(
        "scheduler_startup_attested running=%s jobs=%d "
        "recovered_previous=%s prior_gap_seconds=%s",
        scheduler.running,
        len(scheduler.get_jobs()),
        startup_tick["recovered_previous"],
        startup_tick["prior_gap_seconds"],
    )
    yield

    # [LIVENESS-HEARTBEAT 2026-07-07] On shutdown, signal the heartbeat
    # thread to exit so it doesn't log a final tick during interpreter
    # teardown. Daemon=True means it dies anyway, but the Event
    # makes the exit deterministic.
    _LIVENESS_TICK_STOP.set()
    _liveness_thread.join(timeout=10)

app = FastAPI(title="Quant Engine Container B", lifespan=lifespan)
# (Delete the old @app.on_event("startup") and async def startup(): lines completely)

# @app.on_event("startup")
# async def startup():
#     await init_positions_db(settings.DB_PATH)
#     await init_ledger(settings.DB_PATH)
    
#     # Refresh cache on startup so it's never empty
#     asyncio.create_task(kite.refresh_instrument_cache())
    
#     scheduler.add_job(kite.refresh_instrument_cache, 'cron', hour=8, minute=0)

#     scheduler.add_job(run_screener, 'cron', hour=9, minute=20)
#     scheduler.add_job(run_screener, 'cron', hour=14, minute=45)
#     #scheduler.add_job(run_screener, 'cron', minute=0)
#     scheduler.add_job(daily_post_market, 'cron', hour=15, minute=45)
#     scheduler.add_job(
#         momentum_eod_warning, 'cron',
#         hour=15, minute=10, id="momentum_eod_warning"
#     )
#     scheduler.add_job(
#         auto_square_momentum, 'cron',
#         hour=15, minute=15, id="momentum_auto_square"
#     )
#     for hour in [10, 11, 12, 13, 14]:
#         for minute in [0, 15, 30, 45]:
#             # Skip 10:00 AM because 4 candles (09:15, 09:30, 09:45, 10:00) don't exist until 10:00:01
#             # Actually at 10:00, the 10:00 candle just STARTS. So we only have 3 COMPLETED candles.
#             if hour == 10 and minute == 0:
#                 continue
#             scheduler.add_job(
#                 run_momentum_screener, 'cron',
#                 hour=hour, minute=minute,
#                 id=f"momentum_scan_{hour}{minute}"
#             )



#     # Intraday cache cleanup at midnight
#     scheduler.add_job(
#         kite.clear_intraday_cache, 'cron',
#         hour=0, minute=5, id="intraday_cache_cleanup"
#     )
#     scheduler.start()

# async def post_login_initialization():
#     try:
#         logger.info("running_post_login_setup")
#         await kite.refresh_instrument_cache()
        
#         df = await kite.get_historical("RELIANCE", "2024-01-01", datetime.utcnow().strftime("%Y-%m-%d"))
#         if not df.empty:
#             await run_backtest(settings.DB_PATH, {"RELIANCE": df}, settings.STRATEGY_VERSION)
#             logger.info("initial_backtest_complete")
#         await run_screener()
#     except Exception as e:
#         logger.error("initial_backtest_error", error=str(e))

async def post_login_initialization():
    global _init_running, risk_engine
    if _init_running:
        logger.info("post_login_init_skipped_already_running")
        return
    _init_running = True
    try:
        logger.info("running_post_login_setup")
        try:
            await kite.refresh_instrument_cache()
        except Exception as e:
            logger.warning("instrument_cache_refresh_skipped", error=str(e))
        # [BOOTSTRAP-2026-07-17] "The moment there is a login, it runs once
        # again": any daily bootstrap task the 08:00 cron had to defer (no
        # fresh token) or that failed runs NOW. No-op when everything for
        # today already succeeded, so repeat logins are free.
        try:
            import daily_bootstrap
            await daily_bootstrap.run_pending("post_login")
        except Exception as e:
            logger.error("post_login_bootstrap_failed", error=str(e))
        df = await kite.get_historical(
            "RELIANCE", "2024-01-01",
            datetime.now(IST).strftime("%Y-%m-%d")
        )
        if not df.empty:
            pass  # backtest disabled -- signature mismatch; use backtest.py directly
        await run_screener()           # existing swing screener
        await run_momentum_screener()  # NEW: momentum scan on login
    except Exception as e:
        logger.error("post_login_init_error", error=str(e))
    finally:
        _init_running = False

NIFTY_100_TICKERS = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "HINDUNILVR", "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK",
    "LT", "BAJFINANCE", "AXISBANK", "ASIANPAINT", "MARUTI", "TITAN", "SUNPHARMA", "HCLTECH", "ADANIENT", "TATAMOTORS",
    "NTPC", "JSWSTEEL", "ONGC", "M&M", "POWERGRID", "TATASTEEL", "ADANIPORTS", "COALINDIA", "BAJAJFINSV", "NESTLEIND",
    "GRASIM", "TECHM", "EICHERMOT", "BRITANNIA", "HINDALCO", "INDUSINDBK", "ADANIPOWER", "TATACONSUM", "HDFCLIFE", "SBILIFE",
    "DRREDDY", "CIPLA", "APOLLOHOSP", "DIVISLAB", "LTIM", "BAJAJ-AUTO", "HEROMOTOCO", "ULTRACEMCO", "BPCL", "WIPRO"
]

NIFTY_100_TICKERS = [
    "ABB", "ADANIENSOL", "ADANIENT", "ADANIGREEN", "ADANIPORTS", "ADANIPOWER", "ATGL", "AMBUJACEM", "APOLLOHOSP", "ASIANPAINT",
    "DMART", "AXISBANK", "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BAJAJHLDNG", "BANKBARODA", "BERGEPAINT", "BEL", "BHARTIARTL",
    "BPCL", "BRITANNIA", "CANBK", "CHOLAFIN", "CIPLA", "COALINDIA", "COLPAL", "DLF", "DRREDDY", "EICHERMOT",
    "GAIL", "GICRE", "GODREJCP", "GRASIM", "HAVELLS", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO", "HINDALCO",
    "HAL", "HINDUNILVR", "ICICIBANK", "ICICIGI", "ICICIPRULI", "IDBI", "ITC", "IOC", "IRCTC", "IRFC",
    "INDUSINDBK", "INFY", "INDIGO", "JSWSTEEL", "JSL", "KOTAKBANK", "LT", "LTM", "LICHSGFIN", "LICI",
    "M&M", "MARICO", "MARUTI", "NTPC", "NESTLEIND", "ONGC", "PIDILITIND", "PFC", "POWERGRID", "PNB",
    "RELIANCE", "RECLTD", "SBICARD", "SBILIFE", "SRF", "MOTHERSON", "SHREECEM", "SHRIRAMFIN", "SIEMENS", "SBIN",
    "SUNPHARMA", "SUNTV", "TATACOMM", "TATACONSUM", "TATAELXSI", "TMPV", "TATAPOWER", "TATASTEEL", "TCS", "TECHM",
    "TITAN", "TRENT", "TVSMOTOR", "ULTRACEMCO", "UNITDSPR", "VBL", "VEDL", "WIPRO", "ETERNAL", "ZYDUSLIFE"
]

# NIFTY_500_TICKERS — loaded from data/nifty500.json at module init.
# This is the in-code fallback when the CSV at UNIVERSE_PATH is missing.
# The JSON file is the source of truth (committed alongside the CSV).
# 500 EQ-series tickers from NSE's official Nifty 500 list, 2026-06-16.
try:
    _DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
    with open(os.path.join(_DATA_DIR, "nifty500.json")) as _f:
        NIFTY_500_TICKERS = [t["symbol"] for t in json.load(_f)["tickers"]]
except (FileNotFoundError, KeyError, json.JSONDecodeError):
    # Catastrophic: data file missing/broken. Hard fail loudly.
    raise RuntimeError(
        "NIFTY_500_TICKERS could not be loaded from data/nifty500.json. "
        "This file is the source of truth for the Nifty 500 universe. "
        "Re-run the universe expansion setup or restore the file from git."
    )

def _load_universe_with_fallback() -> pd.DataFrame:
    """
    3-tier universe loader (Task 8, 2026-06-15).

    1. Try CSV at UNIVERSE_PATH (operator-editable, supports custom universes)
    2. Try in-code NIFTY_500_TICKERS (hand-curated, always available)
    3. Crash loudly with a clear error (no silent fallback to old NIFTY_100)

    Returns a DataFrame with columns: tradingsymbol, exchange, sector.

    [AUDIT-FIX-CSV-SPAM 2026-06-26] The CSV-missing warning fires
    every time the screener runs (every momentum scan = every 15min).
    When the file is permanently missing, the warning floods the log
    with 19+ identical entries per day. The fallback still works, so
    demote to a single-shot INFO after the first warn. The first warn
    is sufficient to alert the operator that the CSV is missing; the
    fallback path is documented.
    """
    global _universe_csv_warn_emitted
    try:
        universe = pd.read_csv(settings.UNIVERSE_PATH)
        logger.info("universe_loaded_from_csv", path=settings.UNIVERSE_PATH, count=len(universe))
        return universe
    except FileNotFoundError:
        if not _universe_csv_warn_emitted:
            logger.warning(
                "universe_csv_missing_fallback",
                path=settings.UNIVERSE_PATH,
                fallback_count=len(NIFTY_500_TICKERS),
                note="this warning fires once per process; subsequent loads use the in-code fallback silently",
            )
            _universe_csv_warn_emitted = True
    except Exception as e:
        if not _universe_csv_warn_emitted:
            logger.warning("universe_csv_load_failed", error=str(e))
            _universe_csv_warn_emitted = True

    if NIFTY_500_TICKERS:
        logger.info("universe_loaded_from_code", count=len(NIFTY_500_TICKERS))
        return pd.DataFrame({
            "tradingsymbol": NIFTY_500_TICKERS,
            "exchange": ["NSE"] * len(NIFTY_500_TICKERS),
            "sector": ["UNKNOWN"] * len(NIFTY_500_TICKERS),
        })

    logger.error(
        "universe_load_failed_all_paths",
        csv=settings.UNIVERSE_PATH,
        code_list="NIFTY_500_TICKERS",
    )
    raise RuntimeError(
        f"Cannot load universe: CSV at {settings.UNIVERSE_PATH} not found, "
        f"and NIFTY_500_TICKERS is empty or missing. "
        f"Add a CSV at the path, or restore data/nifty500.json."
    )


async def run_screener():

    global current_signals, rejected_signals, market_regime, last_run
    
    now_ist = datetime.now(IST)
    today = now_ist.date()
    
    if not await is_trading_day(today, settings.DB_PATH):
        logger.info("market_closed")
        return

        # Check for login/token
    if not kite.access_token:
        logger.warning("screener_skipped", reason="no_access_token")
        return


        # Regime Filter [MR1]
    # NIFTY 50 ticker might be different depending on Kite's instrument list
    # Usually it's "NIFTY 50" but we should be sure.
    nifty_df = await kite.get_historical("NIFTY 50", (today - pd.Timedelta(days=365)).strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"))
    if nifty_df.empty:
        # Fallback to "NIFTY BANK" or log error
        logger.error("nifty_data_missing", ticker="NIFTY 50")
        return

    
    nifty_close = nifty_df['close'].iloc[-1]
    nifty_ema50 = calc_ema(50, nifty_df['close']).iloc[-1]
    # Bug 7 fix: compute 1-day nifty return for RS vs Nifty filter in REGIME_3_CRISIS
    nifty_return_1d = (nifty_close / nifty_df['close'].iloc[-2] - 1) if len(nifty_df) >= 2 else 0.0

    # VIX-free regime signals [VIX-FREE]
    # Compute ATR from Nifty data
    nifty_atr = calc_atr(nifty_df['high'], nifty_df['low'], nifty_df['close'])
    nifty_atr_current = float(nifty_atr.iloc[-1])
    nifty_atr_sma200 = nifty_atr.rolling(200).mean()
    nifty_atr_baseline = float(nifty_atr_sma200.iloc[-1])

    # Compute 20-day annualized realized volatility
    import numpy as np
    log_returns = np.diff(np.log(nifty_df['close'].values))
    realized_vol = float(np.std(log_returns[-20:]) * np.sqrt(252)) if len(log_returns) >= 20 else 0.18

    # Fetch BankNifty for Nifty/BankNifty ratio history
    banknifty_df = await kite.get_historical("NIFTY BANK", (today - pd.Timedelta(days=90)).strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"))
    banknifty_close = float(banknifty_df['close'].iloc[-1])

    # Build 60-day rolling Nifty/BankNifty ratio history
    # Guard: cap at available rows to prevent IndexError on thin data
    max_days = min(60, len(nifty_df), len(banknifty_df))
    nb_ratios = []
    for i in range(max_days):
        idx = -(i + 1)
        nb_ratios.append(float(nifty_df['close'].iloc[idx] / banknifty_df['close'].iloc[idx]))
    nb_ratio_history = nb_ratios[::-1]

    # Breadth: Nifty 50 EMA50 proximity as a live market-internal breadth proxy.
    # close/ema50 ratio > 1.0 -> Nifty above its average -> healthy breadth.
    # Clamped to [0.30, 0.70] to stay within the regime engine's breadth penalty zone.
    # Full constituent breadth (Nifty 500 stocks above their SMA50) requires a separate
    # batch download and is tracked as a future enhancement.
    nifty_breadth_ratio = nifty_close / nifty_ema50 if nifty_ema50 > 0 else 1.0
    breadth = max(0.30, min(0.70, 0.30 + (nifty_breadth_ratio - 0.98) * 10.0))

    # Initialize regime engine once on first scan, reuse on subsequent scans
    # to preserve _consecutive_in_range counter and current_regime across
    # scheduler runs (09:20 and 14:45 IST) for 2-scan confirmation logic.
    global _global_regime_engine
    if _global_regime_engine is None:
        _global_regime_engine = RegimeEngine()
    regime_engine = _global_regime_engine
    nifty_ema20 = calc_ema(20, nifty_df['close']).iloc[-1]
    regime_state = regime_engine.update_regime(
        nifty_atr_current=nifty_atr_current,
        nifty_atr_baseline=nifty_atr_baseline,
        realized_vol=realized_vol,
        nifty_close=nifty_close,
        nifty_ema20=nifty_ema20,
        banknifty_close=banknifty_close,
        nb_ratio_history=nb_ratio_history,
        breadth=breadth,
        vix=None,  # VIX-free mode
        nifty_50=nifty_close,
    )
    global _last_regime_state
    _last_regime_state = regime_state
    current_regime = regime_state.regime

    # [MR-3REG] Cache today's 3-regime for the momentum screener.
    # Momentum does 19 scans/day; if it recomputed each time, the regime
    # would flip 5x in a noisy session. We carry forward the swing's
    # 09:20 IST computation for the rest of the day.
    global _momentum_regime_for_today
    _momentum_regime_for_today = current_regime

    if nifty_close < nifty_ema50:
        market_regime = "BEAR_RS_ONLY"
        logger.info("regime_filter", regime="BEAR_RS_ONLY",
                    reason="Nifty below EMA50 - switching to RS-only mode")
        # DO NOT return early - fall through to screener loop
        # The screener loop will apply RS filters based on market_regime
    elif nifty_close < nifty_ema50 * 1.02:
        market_regime = "CAUTION"
    else:
        market_regime = "BULL"

    # 2026-06-24 strict separation: Nifty-subsystem balance (swing + momentum),
    # excludes penny. Swing RiskEngine never sizes off a penny-contaminated number.
    bankroll = await nifty_bankroll(settings.DB_PATH)
    risk_pct = regime_engine.get_risk_pct()

    # Initialize RiskEngine singleton with known bankroll and regime risk_pct.
    # Used for: post-drawdown recovery sizing governor, partial exit checks, and
    # share count recalculation if needed during the scan cycle.
    global risk_engine
    if risk_engine is None:
        from config import settings as cfg
        risk_engine = RiskEngine(bankroll=bankroll, regime_risk_pct=risk_pct)
        logger.info("risk_engine_initialized", bankroll=bankroll, risk_pct=risk_pct)

    # 3-tier universe loader (Task 8, 2026-06-15): CSV → in-code NIFTY_500_TICKERS → RuntimeError
    universe = _load_universe_with_fallback()

    # Liquidity filter (Task 7+8, 2026-06-15): drop tickers with 20-day
    # median ADV below UNIVERSE_MIN_ADV_CRORE. This prevents the scan
    # from wasting compute on illiquid Nifty 500 names.
    from datetime import datetime as _dt
    universe = await _filter_by_liquidity(universe, kite, today=_dt.now())
    logger.info("universe_after_liquidity_filter", count=len(universe))


    # -- Breadth enrichment wiring (Task 7, 2026-06-14) -------------
    # Init the breadth engine singleton + run Tier 1 (hourly SMA50 cache)
    # BEFORE the scan loop. Tier 2 (per-scan rank) needs the live LTPs
    # collected during the loop, so it runs AFTER the loop. The scan
    # itself runs in two passes:
    #   Pass 1: fetch df + collect scan_ltp_by_token (breadth Tier 2 input)
    #   Pass 2: re-walk the cached dfs, call evaluate_signal with the
    #           now-computed breadth_pct_above_sma50 + breadth_rank
    # The two-pass split is necessary because Tier 2 needs ALL the LTPs
    # to compute the percentile rank, but the gate in engine.py needs
    # the rank. No extra Kite calls -- only the dict cache adds overhead.
    global breadth_engine
    breadth_result = None
    if breadth_engine is None:
        breadth_engine = build_breadth_engine(kite, settings)
    if breadth_engine is not None:
        try:
            breadth_result = await breadth_engine.compute_tier1()
            if breadth_result.degraded:
                logger.warning(
                    "breadth_tier1_degraded",
                    reason="tier1 fetch failures exceeded threshold",
                    n_resolved=breadth_result.n_resolved,
                )
        except Exception as e:
            logger.error("breadth_tier1_failed", error=str(e))
            breadth_result = None

    raw_signals = []
    total_evaluated = 0
    raw_rejected = []
    # Pass 1 cache: ticker -> historical df (reused in Pass 2) and
    # token -> live LTP (fed to Tier 2).
    df_cache: Dict[str, pd.DataFrame] = {}
    scan_ltp_by_token: Dict[int, float] = {}
    for _, row in universe.iterrows():
        total_evaluated += 1
        ticker = row['tradingsymbol']
        df = await kite.get_historical(ticker, (today - pd.Timedelta(days=365)).strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"))
        if df.empty:
            raw_rejected.append({"ticker": ticker, "reject_reason": "historical_data_empty"})
            continue

        # Cache the df for Pass 2 + capture the live LTP for Tier 2.
        df_cache[ticker] = df
        token = kite.instrument_cache.get(ticker)
        if token is not None:
            scan_ltp_by_token[token] = float(df['close'].iloc[-1])

    # Pass 2: now that Tier 2 has been computed, re-walk the cached dfs
    # and call evaluate_signal with the breadth kwargs. Skipped entirely
    # when the breadth feature flag is off (or Tier 1 was cold/degraded) --
    # in that case `breadth_result` is None and build_breadth_kwargs
    # returns {} for every ticker, so evaluate_signal runs untouched.
    if breadth_engine is not None and scan_ltp_by_token:
        try:
            breadth_result = await breadth_engine.compute_tier2(scan_ltp_by_token)
            if breadth_result.degraded:
                logger.warning("breadth_tier2_degraded", n_resolved=breadth_result.n_resolved)
        except Exception as e:
            logger.error("breadth_tier2_failed", error=str(e))
            # Don't clear breadth_result -- Tier 1's pct is still useful for the gate.
            # Just leave rank_map empty (gate fires for low-pct tickers, which is
            # the conservative/safe behavior in a degraded Tier 2).

    for _, row in universe.iterrows():
        ticker = row['tradingsymbol']
        df = df_cache.get(ticker)
        if df is None:
            # Already rejected in Pass 1 (empty df). Skip in Pass 2.
            continue

        # Build RSI history for adaptive RSI percentile filter
        try:
            rsi_hist = calc_rsi_series(df["close"])
        except (IndexError, Exception):
            rsi_hist = None

        token = kite.instrument_cache.get(ticker)
        valid, sig_data = evaluate_signal(
            ticker, df, bankroll, risk_pct,
            regime=current_regime,
            market_regime=market_regime,
            nifty_50_current=nifty_close,
            nifty_ema20=nifty_ema20,
            nifty_return_1d=nifty_return_1d,
            rsi_history=rsi_hist,
            **build_breadth_kwargs(token, breadth_result),
        )
        if not valid:
            sig_data["ticker"] = ticker
            raw_rejected.append(sig_data)
            continue


        # [RS-FILTER] BEAR_RS_ONLY regime: apply RS gate
        if market_regime == "BEAR_RS_ONLY":
            from engine import calc_relative_strength, calc_volume_consistency
            rs_score = calc_relative_strength(df['close'], nifty_df['close'], periods=settings.RS_PERIODS)
            vol_consistent = calc_volume_consistency(df['volume'], n_days=settings.RS_MIN_DAYS_ABOVE_AVG, lookback=settings.RS_PERIODS)

            if rs_score < settings.RS_MIN_THRESHOLD:
                logger.info("rs_filter_reject", ticker=ticker,
                            rs=rs_score, reason=f"RS below {settings.RS_MIN_THRESHOLD} in BEAR regime")
                continue

            if not vol_consistent:
                logger.info("rs_filter_reject", ticker=ticker,
                            reason="Volume inconsistency in BEAR regime")
                continue

            sig_data['rs_score'] = rs_score
            sig_data['volume_consistent'] = vol_consistent
            logger.info("rs_filter_pass", ticker=ticker, rs=rs_score)
        else:
            from engine import calc_relative_strength, calc_volume_consistency
            sig_data['rs_score'] = calc_relative_strength(df['close'], nifty_df['close'], periods=settings.RS_PERIODS)
            sig_data['volume_consistent'] = calc_volume_consistency(df['volume'], n_days=settings.RS_MIN_DAYS_ABOVE_AVG, lookback=settings.RS_PERIODS)

        # SWING WINS: skip if this ticker already has an open momentum position
        open_pos_for_swing = await get_open_positions(settings.DB_PATH) # Refetch to be safe
        open_momentum_tickers = {
            p['ticker'] for p in open_pos_for_swing if p.get('source') == 'MOMENTUM'
        }
        if ticker in open_momentum_tickers:
            logger.info("swing_priority", ticker=ticker,
                        reason="Momentum position already open - swing wins")
            continue

        sig_data.update({
            "ticker": ticker, "exchange": row.get('exchange', 'NSE'),
            "sector": row.get('sector', 'UNKNOWN'), "signal_time": datetime.now(timezone.utc),
            "strategy_version": settings.STRATEGY_VERSION,
            "strategy_type": "SWING",
            # [TRAILING-EXITS 2026-06-16] Pass regime to the executor so the
            # position row records regime_at_entry and position_tracker can
            # pick the regime-aware Chandelier multiplier.
            "regime": current_regime,
        })
        raw_signals.append(sig_data)

    open_pos = await get_open_positions(settings.DB_PATH)

    
    async with state_lock:
        current_signals, rejected_signals = filter_and_allocate(raw_signals, open_pos, bankroll, regime=current_regime)
        # Combine all rejections
        from typing import List, Dict
        all_rejected: List[Dict] = raw_rejected + rejected_signals
        last_run = datetime.now(timezone.utc)
        if is_market_open():
            await notify_screener_results("SWING", current_signals, all_rejected, market_regime, bankroll)
        else:
            logger.info("swing_scan_silent", reason="outside_market_hours_notification_suppressed",
                        signals_found=len(current_signals))
            # [SWING-PREMARKET 2026-07-17] Full-report suppression outside
            # market hours is right (pre-open data is yesterday's dailies),
            # but on 2026-07-17 the 08:13 scan found 1 signal and the
            # operator never heard about it -- the 09:20 rescan superseded
            # it silently. A signal found is worth one line even when
            # indicative; stay silent when nothing was found.
            if current_signals:
                try:
                    from operator_alert import notify_operator
                    names = ", ".join(
                        s.get("ticker", "?") for s in current_signals[:5]
                    )
                    await notify_operator(
                        f"🌅 *Pre-market swing scan* found "
                        f"{len(current_signals)} candidate(s): {names}\n"
                        "_Indicative only (pre-open data). The 09:20 "
                        "in-market scan is authoritative and will alert "
                        "normally._",
                        event="swing_premarket_headsup",
                    )
                except Exception as _pm_exc:
                    logger.warning("swing_premarket_headsup_failed error=%s",
                                   str(_pm_exc))

    # Track crisis regime so daily_post_market can react accordingly
    global _last_regime_was_crisis
    if current_regime == Regime.REGIME_3_CRISIS:
        _last_regime_was_crisis = True


async def daily_post_market():
    """Daily 15:45 IST post-market reconciliation.

    [CALENDAR-GATE 2026-07-03] P1 follow-up to PR-1. Skip on weekends +
    NSE holidays. Monday's 15:45 catches up any Saturday/Sunday drift
    via the same open_positions snapshot diff. (This is a swing handler,
    not penny.)
    """
    # [CALENDAR-GATE 2026-07-03] weekend / NSE-holiday early-return.
    today = datetime.now(IST).date()
    if not await is_trading_day(today, settings.DB_PATH):
        logger.info("daily_post_market_skip reason=non_trading_day")
        return
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    global risk_engine

    # Snapshot open positions BEFORE update so we can diff closed trades
    open_pos_before = await get_open_positions(settings.DB_PATH)
    open_tickers_before = {p["ticker"] for p in open_pos_before}

    # Update all daily positions (calls record_trade_close for each closed trade)
    # [TIER0-0.2 2026-07-14] Book each close to ITS OWN pool. This lambda used to
    # drop the source, so record_trade_close fell back to source="SYSTEM" -- and
    # since update_daily_positions walks every open position, the EDGE_PAPER leg
    # (sized off a ₹100,000 imaginary bankroll) wrote its P&L straight into the
    # real ₹5,000 book. The ledger read ₹8,842 when the account held ₹5,016.
    await update_daily_positions(
        settings.DB_PATH, kite, today_str,
        lambda t, p, src: record_trade_close(settings.DB_PATH, t, p, source=src),
    )

    # Snapshot open positions AFTER update -- anything gone was closed today
    open_pos_after = await get_open_positions(settings.DB_PATH)
    closed_tickers = open_tickers_before - {p["ticker"] for p in open_pos_after}

    # Record outcomes in RiskEngine for recovery governor tracking
    if risk_engine is not None and closed_tickers:
        async with aiosqlite.connect(settings.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            placeholders = ",".join(["?"] * len(closed_tickers))
            cursor = await db.execute(
                f"SELECT ticker, realised_pnl FROM positions WHERE ticker IN ({placeholders}) AND exit_date=?",
                list(closed_tickers) + [today_str]
            )
            closed_trades = [dict(row) for row in await cursor.fetchall()]

        for trade in closed_trades:
            win = trade["realised_pnl"] > 0
            in_recovery = risk_engine._recovery_trades_remaining > 0
            risk_engine.record_trade_outcome(win=win, in_recovery=in_recovery)

    # Sync RiskEngine bankroll to ledger after all closes are recorded.
    # Strict-separation: use the Nifty-subsystem balance so a recent penny
    # close doesn't get read as the swing bankroll.
    if risk_engine is not None:
        new_bankroll = await nifty_bankroll(settings.DB_PATH)
        risk_engine.update_bankroll(new_bankroll)
        logger.info("risk_engine_bankroll_updated", new_bankroll=new_bankroll)

    # If run_screener observed a Regime-3 (Crisis) scan at any point today,
    # enter drawdown recovery mode to govern sizing for subsequent trades.
    global _last_regime_was_crisis
    if _last_regime_was_crisis and risk_engine is not None:
        risk_engine.enter_recovery_mode()
        logger.info("drawdown_recovery_entered_daily",
                    recovery_trades=risk_engine._recovery_trades_remaining)
    _last_regime_was_crisis = False

# [MOMENTUM-SKIP-IF-RUNNING 2026-06-30] Re-entrancy guard. The
# momentum scan fires every 15 min via cron; if the previous
# run is still in flight (e.g. a slow Kite + 500-ticker universe)
# the next tick queues another concurrent run, which doubles
# the API cost and starves the next-after-that. Same pattern as
# the penny_universe_refresh guard. The flag is module-level
# (this function is the only writer; the next instance sees
# True and short-circuits with a clear log line).
_momentum_scan_in_progress: bool = False


async def run_momentum_screener():
    """
    Hourly intraday momentum scanner.
    """
    global current_momentum_signals, _momentum_scan_in_progress
    if _momentum_scan_in_progress:
        logger.warning(
            "momentum_scan_skipped reason=previous_run_in_progress"
        )
        return
    _momentum_scan_in_progress = True
    import time as _time
    _t0 = _time.monotonic()
    try:
        return await _run_momentum_screener_impl(_t0)
    finally:
        _momentum_scan_in_progress = False


async def _run_momentum_screener_impl(t0):
    """Implementation of run_momentum_screener, separated so the
    skip-if-running guard lives in the wrapper. See comment
    on the wrapper for the rationale."""
    global current_momentum_signals

    now_ist = datetime.now(IST)
    today = now_ist.date()

    if not await is_trading_day(today, settings.DB_PATH):
        logger.info("momentum_scan_skip", reason="market_closed_today")
        return

    if not kite.access_token:
        logger.warning("momentum_screener_skipped", reason="no_access_token")
        return

    # Strict separation: momentum pool sized off Nifty-subsystem balance,
    # not the last ledger row (which could be a penny close).
    bankroll       = await nifty_bankroll(settings.DB_PATH)
    momentum_pool  = bankroll * settings.MOMENTUM_POOL_PCT  # 50% of bankroll = Rs2,500 at Rs5k

    # Market opens at 09:15 IST, closes at 15:30 IST
    market_open  = now_ist.replace(hour=9,  minute=15, second=0, microsecond=0)
    market_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)

    if now_ist < market_open:
        logger.info("momentum_scan_skip", reason="before_market_open_ist")
        return

    if now_ist > market_close:
        logger.info("momentum_scan_skip", reason="after_market_close_ist")
        return

    from_dt = market_open.strftime('%Y-%m-%d %H:%M:%S')
    to_dt   = now_ist.strftime('%Y-%m-%d %H:%M:%S')

    logger.info("momentum_scan_start", from_dt=from_dt, to_dt=to_dt)


    # 3-tier universe loader (Task 8, 2026-06-15): same as run_screener
    universe = _load_universe_with_fallback()

    # Liquidity filter (Task 7+8, 2026-06-15): same as run_screener.
    # Note: this also runs from the in-code NIFTY_500_TICKERS fallback
    # (it does N+1 historical fetches per scan — see runbook cost analysis).
    from datetime import datetime as _dt
    universe = await _filter_by_liquidity(universe, kite, today=_dt.now())
    logger.info("momentum_universe_after_liquidity_filter", count=len(universe))


    open_pos          = await get_open_positions(settings.DB_PATH)
    open_momentum_pos = [p for p in open_pos if p.get('source') == 'MOMENTUM']
    open_swing_tickers = {
        p['ticker'] for p in open_pos if p.get('source') != 'MOMENTUM'
    }

    # [MR-3REG] Read today's regime from the swing screener's cache.
    # If swing hasn't run yet (e.g., first scan of the day at 10:15),
    # fall back to Regime.REGIME_1_NORMAL (safe default, no block).
    today_regime = _momentum_regime_for_today or Regime.REGIME_1_NORMAL

    raw_momentum = []
    raw_rejected_momentum = []
    shadow_evaluations = []

    # [MC3-T] Time-aware volume threshold: elevated during lunchtime dead zone
    lunchtime_start = now_ist.replace(
        hour=settings.MOMENTUM_LUNCHTIME_START_HOUR,
        minute=settings.MOMENTUM_LUNCHTIME_START_MIN,
        second=0, microsecond=0
    )
    lunchtime_end = now_ist.replace(
        hour=settings.MOMENTUM_LUNCHTIME_END_HOUR,
        minute=settings.MOMENTUM_LUNCHTIME_END_MIN,
        second=0, microsecond=0
    )
    vol_threshold = (
        settings.MOMENTUM_VOL_SURGE_LUNCHTIME
        if lunchtime_start <= now_ist <= lunchtime_end
        else settings.MOMENTUM_VOL_SURGE_PCT
    )

    # [MOMENTUM-PARALLEL 2026-06-30] Parallel per-ticker evaluation.
    # The earlier serial loop took 15+ min per scan for 500 tickers
    # at ~3 Kite calls each (intraday + daily + prev_trading_day).
    # With asyncio.gather, all 500 fetches queue at the Kite rate
    # limiter (3 req/s) in parallel; wall-clock cost drops to
    # 500 * 3 / 3 = 500s = 8 min cold cache, 30-60s hot cache.
    # Result processing (filtering, logging, persist) stays
    # serial AFTER gather returns, so the data structures
    # downstream are unchanged.
    yesterday_date = await prev_trading_day(today, settings.DB_PATH)
    from_date_for_daily = (yesterday_date - pd.Timedelta(days=30)).strftime("%Y-%m-%d")
    to_date_for_daily = today.strftime("%Y-%m-%d")
    universe_rows = list(universe.iterrows())

    # [MOMENTUM-SEMAPHORE 2026-07-15] Cap concurrent per-ticker work so the
    # 500-ticker gather cannot open ~1000 sqlite connections at once and exhaust
    # the OS file-handle ceiling (the cause of the "unable to open database
    # file" tail failures). Kite's own 3 req/s limiter remains the throughput
    # bound, so this does not slow a healthy scan.
    _mom_sem = asyncio.Semaphore(
        int(getattr(settings, "MOMENTUM_SCAN_SQLITE_MAX_CONCURRENT", 50))
    )

    async def _eval_one_momentum_ticker(row):
        """One ticker's intraday + daily fetch + evaluate.

        Returns a tuple: (ticker, sig_data_or_None, error_or_None, shadow_rows)
        - sig_data is the dict returned by evaluate_momentum_signal
          (whether fired=True or False) so the caller can log +
          persist both accept and reject.
        - error is non-None when the per-ticker work raised; the
          outer loop counts it as an error row and continues.
        """
        ticker = row['tradingsymbol']
        async with _mom_sem:
          try:
            if ticker in open_swing_tickers:
                return (ticker, {"fired": False, "ticker": ticker, "reject_reason": "swing_position_exists"}, None, [])
            df_intra = await kite.get_intraday(ticker, from_dt, to_dt)
            if df_intra.empty:
                return (ticker, {"fired": False, "ticker": ticker, "reject_reason": "intraday_data_empty"}, None, [])
            if len(df_intra) < 4:
                return (ticker, {"fired": False, "ticker": ticker, "reject_reason": "insufficient_intraday_candles", "count": len(df_intra)}, None, [])
            df_daily = await kite.get_historical(
                ticker, from_date_for_daily, to_date_for_daily
            )
            if df_daily.empty or len(df_daily) < 1:
                return (ticker, {"fired": False, "ticker": ticker, "reject_reason": "daily_data_missing_for_prev_high"}, None, [])
            df_prev = df_daily[df_daily.index.date < today]
            if df_prev.empty:
                return (ticker, {"fired": False, "ticker": ticker, "reject_reason": "prev_day_data_not_found"}, None, [])
            prev_day_high = float(df_prev['high'].iloc[-1])
            fired, sig_data = evaluate_momentum_signal(
                ticker=ticker,
                df=df_intra,
                prev_day_high=prev_day_high,
                bankroll=bankroll,
                momentum_pool=momentum_pool,
                df_daily=df_prev,
                vol_surge_threshold=vol_threshold,
                market_regime=market_regime,
                regime=today_regime,
            )
            sig_data["ticker"] = ticker
            sig_data["fired"] = bool(fired)
            if fired:
                sig_data.update({
                    "exchange":         row.get('exchange', 'NSE'),
                    "sector":           row.get('sector', 'UNKNOWN'),
                    "signal_time":      datetime.now(timezone.utc),
                    "strategy_version": settings.STRATEGY_VERSION,
                    "ema_21": 0.0, "ema_50": 0.0, "ema_200": 0.0,
                    "atr_14": 0.0, "rsi_14": 0.0, "slope_5": 0.0,
                    "target_2": sig_data["target_1"],
                    "regime":  today_regime.name,
                })
            shadow_rows = []
            if getattr(settings, "MOMENTUM_SHADOW_ENABLED", True):
                try:
                    shadow_rows = evaluate_momentum_shadows(
                        ticker=ticker,
                        df=df_intra,
                        prev_day_high=prev_day_high,
                        bankroll=bankroll,
                        momentum_pool=momentum_pool,
                        df_daily=df_prev,
                        vol_surge_threshold=vol_threshold,
                        market_regime=market_regime,
                        regime=today_regime,
                        trading_date=today,
                        bar_ts=df_intra.index[-1],
                    )
                except Exception as shadow_exc:
                    logger.warning(
                        "momentum_shadow_evaluation_failed",
                        ticker=ticker,
                        error=str(shadow_exc),
                    )
            return (ticker, sig_data, None, shadow_rows)
          except Exception as exc:
            return (ticker, None, str(exc), [])

    import time as _time
    _t0 = _time.monotonic()
    gathered = await asyncio.gather(
        *[_eval_one_momentum_ticker(row) for _, row in universe_rows],
        return_exceptions=True,
    )
    _elapsed = _time.monotonic() - _t0
    logger.info(
        "momentum_per_ticker_eval_done count=%d elapsed=%.1fs",
        len(gathered), _elapsed,
    )
    for result in gathered:
        if isinstance(result, Exception):
            # Should not normally happen -- the per-ticker eval
            # catches its own exceptions. Be defensive.
            logger.error("momentum_gather_exception error=%s", str(result))
            continue
        ticker, sig_data, error, ticker_shadow_rows = result
        shadow_evaluations.extend(ticker_shadow_rows)
        if error is not None:
            logger.error("momentum_scan_error", ticker=ticker, error=error)
            raw_rejected_momentum.append({
                "ticker": ticker, "reject_reason": f"exception: {error}"
            })
            continue
        if sig_data is None:
            continue
        if sig_data.get("fired"):
            # Strip the helper field; downstream uses presence in
            # raw_momentum as the "fired" signal.
            sig_data.pop("fired", None)
            raw_momentum.append(sig_data)
        else:
            sig_data.pop("fired", None)
            raw_rejected_momentum.append(sig_data)

    # One write transaction per scan, after all concurrent workers have
    # completed. Shadow storage is deliberately outside the decision funnel:
    # any failure leaves raw_momentum and its rejects byte-for-byte unchanged.
    if getattr(settings, "MOMENTUM_SHADOW_ENABLED", True):
        try:
            await persist_momentum_shadow_results(
                settings.DB_PATH, shadow_evaluations
            )
        except Exception as shadow_exc:
            logger.warning(
                "momentum_shadow_persistence_failed",
                error=str(shadow_exc),
                evaluations=len(shadow_evaluations),
            )

    # [MOMENTUM-R3-CAP 2026-06-16] Soft cap: total R3 positions (open + newly
    # accepted this scan) <= MOMENTUM_R3_MAX_POSITIONS. Replaces hard block.
    if today_regime == Regime.REGIME_3_CRISIS:
        r3_count_open = sum(1 for p in open_momentum_pos if p.get('regime_at_entry') == 'REGIME_3_CRISIS')
        cap_remaining = max(0, settings.MOMENTUM_R3_MAX_POSITIONS - r3_count_open)
        if cap_remaining == 0:
            logger.info("momentum_r3_cap_reached", open_r3=r3_count_open, cap=settings.MOMENTUM_R3_MAX_POSITIONS)
            raw_momentum = []
        else:
            # Truncate this scan's R3 candidates to remaining capacity
            r3_candidates = [s for s in raw_momentum if s.get('regime') == 'REGIME_3_CRISIS']
            non_r3 = [s for s in raw_momentum if s.get('regime') != 'REGIME_3_CRISIS']
            raw_momentum = non_r3 + r3_candidates[:cap_remaining]
            logger.info("momentum_r3_cap_truncated", open_r3=r3_count_open, cap=settings.MOMENTUM_R3_MAX_POSITIONS,
                        candidates=len(r3_candidates), kept=min(len(r3_candidates), cap_remaining))

    accepted, rejected_mom = filter_momentum_signals(
        raw_momentum, open_momentum_pos, momentum_pool,
        settings.MAX_MOMENTUM_POSITIONS
    )

    # [MOMENTUM-LOG 2026-06-16] Append every signal (accepted + rejected) to the
    # CSV + SQLite log. Source of truth for future backtests. Gated by
    # MOMENTUM_LOG_ENABLED. Failures here must NOT break the live scan.
    if settings.MOMENTUM_LOG_ENABLED:
        try:
            from signal_log import (
                build_row, init_momentum_log_db, log_momentum_batch,
                make_scan_id, now_utc_iso,
            )
            # Lazy table creation -- only needs to run once but is idempotent.
            await init_momentum_log_db(settings.DB_PATH)
            scan_id = make_scan_id()
            scanned_at = now_utc_iso()
            rows = []
            for s in raw_momentum:
                rows.append(build_row(
                    ticker=s.get("ticker", "UNKNOWN"),
                    accepted=True,
                    result=s,
                    scan_id=scan_id,
                    scanned_at=scanned_at,
                    regime=today_regime.name if today_regime else None,
                    bankroll=bankroll,
                    momentum_pool=momentum_pool,
                ))
            # Rejected signals: include both pre-gate (raw_rejected_momentum) and
            # post-gate (rejected_mom) so the log captures the full funnel.
            for s in (raw_rejected_momentum + rejected_mom):
                ticker = s.get("ticker", "UNKNOWN") if isinstance(s, dict) else getattr(s, "ticker", "UNKNOWN")
                rows.append(build_row(
                    ticker=ticker,
                    accepted=False,
                    result=s if isinstance(s, dict) else vars(s),
                    scan_id=scan_id,
                    scanned_at=scanned_at,
                    regime=today_regime.name if today_regime else None,
                    bankroll=bankroll,
                    momentum_pool=momentum_pool,
                ))
            await log_momentum_batch(settings.DB_PATH, rows)
        except Exception as e:
            logger.error("momentum_log_failed", error=str(e))

    async with state_lock:
        global signaled_momentum_today, last_momentum_date, momentum_signals_today
        # Clear short-term memory at the start of a new trading day
        if today != last_momentum_date:
            signaled_momentum_today.clear()
            momentum_signals_today = []
            last_momentum_date = today

        current_momentum_signals = accepted
        all_rejected_mom = raw_rejected_momentum + rejected_mom

        # Filter for completely new signals that haven't been alerted today
        new_alerts = []
        for s in accepted:
            ticker = s.ticker if hasattr(s, 'ticker') else s.get('ticker')
            if ticker not in signaled_momentum_today:
                new_alerts.append(s)
                signaled_momentum_today.add(ticker)

        # [MOM-FUNNEL 2026-07-11] new_alerts is exactly the deduped-by-ticker
        # delta, so extending here keeps momentum_signals_today cumulative
        # for the day with first-accept-wins semantics.
        momentum_signals_today.extend(new_alerts)

        # [MOMENTUM-PAPER 2026-07-26] Take every accepted signal in the paper
        # book, whether or not the operator presses EXEC on the alert. This is
        # the only record of what the STRATEGY decided rather than what the
        # human did. Uses new_alerts (deduped by ticker) so a signal repeating
        # across scans does not open the same paper position twice.
        #
        # Wrapped: paper bookkeeping must never break the live scan that
        # produced the signal. momentum_paper places no orders -- the module has
        # no order-placing code path at all.
        if new_alerts:
            try:
                from momentum_paper import open_momentum_paper_positions
                await open_momentum_paper_positions(settings.DB_PATH, new_alerts)
            except Exception as _e:
                logger.warning("momentum_paper_hook_failed", error=str(_e))

        # Only send Telegram notifications during market hours (BUG-001 fix: mirrors swing screener guard).
        # The Q4 ignition call still runs this function pre-market to populate the cache,
        # but we must not spam Telegram at 08:30 IST with empty scan results.
        if is_market_open():
            if len(new_alerts) > 0:
                await notify_screener_results("MOMENTUM", new_alerts, all_rejected_mom, market_regime, bankroll, momentum_pool)
            else:
                logger.info("momentum_scan_silent", reason="no_new_signals_found")
                # Heartbeat: notify user the scan ran even with no signals
                await _notify_momentum_heartbeat(
                    now_ist, len(universe), len(raw_momentum),
                    len(accepted), all_rejected_mom, momentum_pool
                )
        else:
            logger.info("momentum_scan_pre_market", reason="outside_market_hours_notification_suppressed",
                        accepted=len(accepted), scan_time=now_ist.isoformat())


    logger.info("momentum_scan_complete",
                tickers_scanned=len(universe),
                signals_found=len(accepted),
                elapsed=time.monotonic() - t0)

    # [MOMENTUM-SIGNAL-LOG 2026-06-16] Append-only CSV of every scan's outcome
    # (accepted AND rejected) for offline backtest + post-hoc review.
    # Path: /data/momentum_signals.csv (operator-mountable volume).
    try:
        import csv as _csv
        from pathlib import Path as _Path

        def _get(obj, key, default=""):
            """Compat accessor: works for dict, pydantic model, and unknown objects."""
            if obj is None:
                return default
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default)

        _log_path = _Path(settings.DB_PATH).parent / "momentum_signals.csv"
        _log_exists = _log_path.exists()
        with _log_path.open("a", newline="") as _f:
            _w = _csv.writer(_f)
            if not _log_exists:
                _w.writerow([
                    "scan_time_ist", "regime", "ticker", "accepted",
                    "close", "stop_loss", "target_1", "shares",
                    "r_target", "vol_ratio", "reject_reason",
                ])
            for s in accepted:
                _w.writerow([
                    now_ist.isoformat(),
                    today_regime.name,
                    _get(s, "ticker"),
                    1,
                    _get(s, "close"),
                    _get(s, "stop_loss"),
                    _get(s, "target_1"),
                    _get(s, "shares"),
                    _get(s, "effective_r_target"),
                    _get(s, "volume_ratio"),
                    "",
                ])
            for r in all_rejected_mom:
                _w.writerow([
                    now_ist.isoformat(),
                    today_regime.name,
                    _get(r, "ticker"),
                    0,
                    "", "", "", "", "", "",
                    _get(r, "reject_reason"),
                ])
    except Exception as _e:
        logger.warning("momentum_signal_log_failed", error=str(_e))

def _momentum_initial_risk(pos: dict) -> float:
    """Immutable full-position R denominator, with a legacy-row fallback."""
    stored = float(pos.get("initial_capital_at_risk") or 0.0)
    if stored > 0:
        return stored
    per_share = float(pos["entry_price"]) - float(
        pos.get("stop_loss_initial", float(pos["entry_price"]) * 0.95)
    )
    return max(0.0, per_share * int(pos.get("shares") or 0))


def _aggregate_momentum_close(pos: dict, final_leg_pnl: float) -> tuple[float, float]:
    """Return whole-trade P&L/R after an optional earlier scale-out."""
    total_pnl = float(pos.get("realised_pnl") or 0.0) + final_leg_pnl
    initial_risk = _momentum_initial_risk(pos)
    return total_pnl, total_pnl / initial_risk if initial_risk > 0 else 0.0


async def _close_momentum_position(pos: dict, exit_price: float, reason: str, notes: str):
    """Record a momentum close in the DB + ledger. Shared by the intraday
    monitor and the SL-M reconciler so both write the same shape.

    [LEDGER-INTEGRITY 2026-07-26] Returns True only if a position row actually
    closed. The ledger write is gated on that, because a WHERE clause that
    matches nothing used to still book P&L -- see auto_square_momentum for what
    that cost in prod.
    """
    ticker = pos['ticker']
    gross = (exit_price - pos['entry_price']) * pos['shares']
    costs = calc_zerodha_costs(
        pos['entry_price'], exit_price, pos['shares'],
        is_intraday=_is_intraday_from_product_type(pos.get('product_type')),
    )
    realised_pnl = gross - costs
    total_pnl, r_multiple = _aggregate_momentum_close(pos, realised_pnl)

    async with aiosqlite.connect(settings.DB_PATH) as db:
        # [SCALE-OUT 2026-08-04] ADD rather than assign -- after a partial the
        # row already carries the banked half and this close covers only the
        # runner. COALESCE keeps the no-partial case byte-identical.
        cur = await db.execute("""
            UPDATE positions
            SET status=?, exit_price=?, exit_date=?,
                realised_pnl=?, r_multiple=?
            WHERE ticker=? AND source='MOMENTUM'
              AND status IN ('OPEN', 'CLOSED_T1') AND exit_date IS NULL
        """, (reason, exit_price, datetime.now(timezone.utc).isoformat(),
              total_pnl, r_multiple, ticker))
        await db.commit()
        rows_closed = cur.rowcount

    if rows_closed != 1:
        logger.error(
            "momentum_close_not_persisted", ticker=ticker,
            rows_affected=rows_closed, reason=reason,
            message="no open position row matched -- ledger NOT written",
        )
        return False

    # [SOURCE-REQUIRED 2026-07-26] Attribute to the position's own division. This
    # call omitted `source` and so booked every momentum close into the SWING pool
    # via the old "SYSTEM" default -- see record_trade_close for the full history.
    await record_trade_close(settings.DB_PATH, ticker, realised_pnl,
                            r_multiple=r_multiple, notes=notes,
                            outcome_pnl=total_pnl,
                            outcome_r_multiple=r_multiple,
                            source=pos.get('source') or 'MOMENTUM')
    logger.info("momentum_position_closed", ticker=ticker, exit_price=exit_price,
                reason=notes, pnl=round(total_pnl, 2), r=round(r_multiple, 4))
    return True


async def _record_momentum_scale_out(
    pos: dict, exit_price: float, sold_shares: int,
    runner_shares: int, new_stop: float, reason: str,
) -> bool:
    """[SCALE-OUT 2026-08-04] Book a PARTIAL momentum exit.

    Deliberately not _close_momentum_position: this must not close the row.
    The position stays OPEN with fewer shares, a raised stop, and t1_fired=1
    so the partial can only ever fire once.

    P&L is booked on the shares actually sold, with costs computed for that
    quantity -- not prorated from a full-size figure, because Zerodha's
    brokerage is a flat-plus-percentage mix and the prorated number would be
    wrong in the direction that flatters us.

    The r_multiple is deliberately NOT written here. The position is still
    live, so its final R is unknown. The partial ledger event changes cash
    equity without creating a closed outcome; the runner's close later records
    one aggregate P&L/R outcome against the immutable full-position risk.
    """
    ticker = pos['ticker']
    gross = (exit_price - pos['entry_price']) * sold_shares
    costs = calc_zerodha_costs(
        pos['entry_price'], exit_price, sold_shares,
        is_intraday=_is_intraday_from_product_type(pos.get('product_type')),
    )
    realised_pnl = gross - costs

    async with aiosqlite.connect(settings.DB_PATH) as db:
        cur = await db.execute("""
            UPDATE positions
            SET shares=?, trailing_stop_current=?, t1_fired=1,
                realised_pnl=COALESCE(realised_pnl, 0) + ?
            WHERE ticker=? AND source='MOMENTUM'
              AND status='OPEN' AND exit_date IS NULL AND t1_fired=0
        """, (runner_shares, new_stop, realised_pnl, ticker))
        await db.commit()
        rows = cur.rowcount

    if rows != 1:
        # Same discipline as _close_momentum_position: a WHERE that matched
        # nothing must not book money. t1_fired=0 in the predicate also makes
        # this idempotent -- a retry after a partial failure cannot double-book.
        logger.error(
            "momentum_scale_out_not_persisted", ticker=ticker,
            rows_affected=rows,
            message="no matching open position -- ledger NOT written",
        )
        return False

    await record_partial_realisation(
        settings.DB_PATH, ticker, realised_pnl,
        notes=f"momentum_scale_out {reason}",
        source=pos.get('source') or 'MOMENTUM',
    )
    return True


def _square_off_fill_evidence(response, requested_qty: int) -> dict:
    """Parse Container A's confirmation contract; HTTP success is not a fill."""
    try:
        body = response.json() or {}
    except Exception:
        body = {}
    requested = max(0, int(requested_qty or 0))
    filled = min(requested, max(0, int(body.get("filled_quantity") or 0)))
    price = float(body.get("average_price") or 0)
    state = str(body.get("state") or "UNKNOWN").upper()
    terminal = bool(body.get("terminal"))
    return {
        "state": state,
        "order_id": body.get("order_id"),
        "filled_quantity": filled,
        "remaining_quantity": requested - filled,
        "average_price": price if price > 0 else None,
        "confirmed": filled > 0 and price > 0,
        "complete": (
            filled == requested and price > 0
            and (state == "COMPLETE" or (state == "PARTIAL" and terminal))
        ),
        "terminal": terminal,
    }


async def _cancel_order_truth(client, order_id: str, requested_qty: int) -> dict:
    """Cancel then read broker history; terminal partial fills are not zero."""
    cancel = {}
    latest = {}
    error = None
    try:
        cancel = await client.cancel_order(order_id=str(order_id))
    except Exception as exc:
        error = str(exc)
    try:
        history = await client.order_history(order_id=str(order_id))
        latest = _kc_latest_order_state(history) if history else {}
    except Exception as exc:
        error = error or str(exc)
    status = str(latest.get("status") or cancel.get("status") or "UNKNOWN").upper()
    requested = max(0, int(requested_qty or 0))
    filled = int(latest.get("filled_quantity") or 0)
    if status == "COMPLETE" and filled <= 0:
        filled = requested
    filled = min(requested, max(0, filled))
    price = float(latest.get("average_price") or 0)
    return {
        "status": status, "filled_quantity": filled,
        "average_price": price if price > 0 else None,
        "confirmed_fill": filled > 0 and price > 0,
        "safe_to_sell": filled == 0 and status in {"CANCELLED", "REJECTED"},
        "error": error or cancel.get("message"),
    }


def _momentum_square_off_key(pos: dict) -> str:
    """One stable broker mutation key for the current local residual."""
    identity = "|".join((
        str(pos.get("source") or "MOMENTUM"),
        str(pos.get("ticker") or "").upper(),
        str(pos.get("entry_date") or "legacy"),
        str(int(pos.get("shares") or 0)),
        str(pos.get("product_type") or "MIS").upper(),
    ))
    return "MXSQ-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


async def _record_confirmed_momentum_partial(
    pos: dict, *, sold_shares: int, fill_price: float,
    new_stop: float | None, reason: str, mark_t1: bool = False,
) -> bool:
    """Settle a broker-confirmed partial while preserving its residual."""
    original_shares = int(pos.get("shares") or 0)
    sold = min(original_shares, max(0, int(sold_shares or 0)))
    remaining = original_shares - sold
    if sold <= 0 or remaining <= 0 or fill_price <= 0:
        return False
    costs = calc_zerodha_costs(
        pos["entry_price"], fill_price, sold,
        is_intraday=_is_intraday_from_product_type(pos.get("product_type")),
    )
    pnl = (fill_price - pos["entry_price"]) * sold - costs
    stop = float(new_stop if new_stop is not None else pos.get("trailing_stop_current") or pos.get("stop_loss_initial") or 0)
    async with aiosqlite.connect(settings.DB_PATH) as db:
        cur = await db.execute("""
            UPDATE positions SET shares=?, trailing_stop_current=?,
                t1_fired=CASE WHEN ? THEN 1 ELSE t1_fired END,
                realised_pnl=COALESCE(realised_pnl,0)+?
            WHERE ticker=? AND source='MOMENTUM' AND shares=?
              AND status IN ('OPEN','CLOSED_T1') AND exit_date IS NULL
        """, (remaining, stop, 1 if mark_t1 else 0, pnl,
              pos["ticker"], original_shares))
        await db.commit()
        changed = cur.rowcount
    if changed != 1:
        logger.error("momentum_partial_not_persisted", ticker=pos["ticker"],
                     sold=sold, expected_shares=original_shares)
        return False
    await record_partial_realisation(
        settings.DB_PATH, pos["ticker"], pnl,
        notes=f"confirmed_square_off_partial:{reason}",
        source=pos.get("source") or "MOMENTUM",
    )
    return True


async def _page_unconfirmed_square_off(ticker: str, evidence: dict, context: str) -> None:
    await _notify_operator(
        f"🚨 `{ticker}` {context} is {evidence.get('state','UNKNOWN')}; "
        f"broker-confirmed {evidence.get('filled_quantity',0)} shares, "
        f"residual {evidence.get('remaining_quantity','?')}, order "
        f"`{evidence.get('order_id')}`. Residual remains open in accounting; "
        "reconcile in Zerodha before retrying.",
        event="momentum_square_off_unconfirmed",
    )


async def _post_square_off_with_reconcile(url: str, payload: dict):
    """Retry one ambiguous HTTP outcome with the same broker idempotency key."""
    import httpx
    last_error = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    url, json=payload,
                    headers={"X-Internal-Secret": settings.INTERNAL_API_SECRET},
                    timeout=30.0,
                )
            response.raise_for_status()
            return response
        except Exception as exc:
            last_error = exc
            # The identical key makes the second request reconciliation, not a
            # second broker mutation. Retry once for timeout/5xx/202 transport.
            if attempt == 0:
                continue
    await _notify_operator(
        f"🚨 `{payload.get('ticker')}` square-off HTTP outcome is UNKNOWN after "
        f"idempotent reconciliation (`{payload.get('idempotency_key')}`). "
        f"No local settlement was recorded. Check Zerodha now. Error: "
        f"{str(last_error)[:180]}",
        event="momentum_square_off_transport_unknown",
    )
    return None


async def _rearm_momentum_residual_stop(pos: dict, quantity: int) -> str | None:
    """Protect a confirmed ACTION_EXIT residual after its old SL was cancelled."""
    if quantity <= 0:
        return None
    trigger = snap_to_tick(
        float(pos.get("trailing_stop_current") or pos.get("stop_loss_initial") or 0), -1,
    )
    if trigger <= 0:
        return None
    limit = snap_to_tick(trigger * 0.99, -1)
    try:
        response = await kite.place_order(
            variety="regular", exchange="NSE", tradingsymbol=pos["ticker"],
            transaction_type="SELL", quantity=int(quantity),
            product=pos.get("product_type") or "MIS", order_type="SL",
            trigger_price=trigger, price=limit, validity="DAY",
            tag="QM_RESIDUAL_SL", intent="exit", channel="momentum",
        )
        order_id = (response or {}).get("order_id")
        return str(order_id) if order_id else None
    except Exception as exc:
        logger.error("momentum_residual_sl_failed", ticker=pos.get("ticker"),
                     quantity=quantity, error=str(exc))
        return None


async def momentum_intraday_monitor():
    """Run the intraday manager only while it owns the momentum book."""
    # [CALENDAR-GATE 2026-07-03] The scheduler-visible financial-risk
    # handler owns its gate directly; the inner implementation repeats it as
    # defence in depth for direct callers.
    today = datetime.now(IST).date()
    if not await is_trading_day(today, settings.DB_PATH):
        logger.info("momentum_monitor_skip reason=non_trading_day")
        return
    if not _momentum_exit_clock()["monitor_allowed"]:
        logger.info("momentum_monitor_skip reason=eod_ownership")
        return
    async with _momentum_exit_ownership_lock:
        # The lock may have been held across the 15:13 handoff.
        if not _momentum_exit_clock()["monitor_allowed"]:
            logger.info("momentum_monitor_skip reason=eod_ownership_after_lock")
            return
        await _momentum_intraday_monitor_owned()


async def _momentum_intraday_monitor_owned():
    """
    [TIER0-0.1 2026-07-14] Manage open MIS momentum positions DURING the day.

    Before this job existed, nothing evaluated a momentum position's stop or
    target between the fill and the 15:15 auto-square -- so all 7 momentum trades
    in the system's history exited on the clock, none on a target or a stop. See
    momentum_exits.py for the full write-up.

    Responsibilities, in order:
      1. Reconcile a broker-side SL-M that has already filled. This is the one
         that MUST NOT be skipped: if the SL-M fills and the DB still says OPEN,
         auto_square_momentum would later sell shares we no longer own -- which
         opens a SHORT.
      2. Take the target (cancel the SL-M first -- Zerodha has no OCO for MIS, so
         a resting stop plus our sell could both fill).
      3. Ratchet the SL-M trigger to breakeven / trail.
    """
    from momentum_exits import (
        evaluate_momentum_exit, momentum_exit_status,
        ACTION_EXIT, ACTION_TRAIL, ACTION_SCALE_OUT,
    )
    from datetime import time as dt_time
    import httpx as _httpx

    now_ist = datetime.now(IST)
    if not await is_trading_day(now_ist.date(), settings.DB_PATH):
        return

    # Only manage while the market is actually open. Outside those hours there is
    # no LTP to act on, and from 15:13 auto_square_momentum owns the book.
    if not (dt_time(9, 15) <= now_ist.time() < dt_time(15, 13)):
        return

    open_pos = await get_open_positions(settings.DB_PATH)
    momentum_pos = [
        p for p in open_pos
        if p.get('source') == 'MOMENTUM' and p.get('status') == 'OPEN'
    ]
    if not momentum_pos:
        return

    container_a_url = settings.CONTAINER_A_URL
    now_utc = datetime.now(timezone.utc)

    for pos in momentum_pos:
        ticker = pos['ticker']
        try:
            # ---- 1. Did the broker-side stop already fill? ------------------
            sl_order_id = pos.get('sl_order_id')
            if sl_order_id:
                # [ORDER-HISTORY-2026-07-17] Kite history is chronological;
                # history[0] is the oldest event. latest_order_state() reads
                # the current one -- history[0] could never show COMPLETE,
                # so a broker-filled SL-M was invisible to this monitor and
                # the exit ladder would try to sell a second time.
                history = await kite.order_history(order_id=str(sl_order_id))
                latest = _kc_latest_order_state(history) if history else None
                if latest and latest.get("status") == "COMPLETE":
                    fill = float(latest.get("average_price") or pos['stop_loss_initial'])
                    await _close_momentum_position(
                        pos, fill, "STOPPED_OUT", "sl_m_filled",
                    )
                    continue

            # ---- 2/3. Evaluate the exit ladder ------------------------------
            async with _httpx.AsyncClient() as _client:
                ltp_resp = await _client.get(
                    f"{container_a_url}/api/orders/ltp",
                    headers={"X-Internal-Secret": settings.INTERNAL_API_SECRET},
                    params={"ticker": ticker},
                    timeout=5.0,
                )
            ltp = float(ltp_resp.json().get("ltp", 0) or 0)
            if ltp <= 0:
                logger.warning("momentum_monitor_no_ltp", ticker=ticker)
                continue

            decision = evaluate_momentum_exit(pos, ltp, now_utc)
            action = decision["action"]

            if action == ACTION_EXIT:
                # Cancel the resting SL-M BEFORE selling. If we sold first and the
                # SL-M were still live, a dip could trigger it and sell a second
                # time -- leaving us short.
                if sl_order_id:
                    stop_truth = await _cancel_order_truth(
                        kite, str(sl_order_id), int(pos["shares"]),
                    )
                    if stop_truth["confirmed_fill"]:
                        if stop_truth["filled_quantity"] >= int(pos["shares"]):
                            await _close_momentum_position(
                                pos, stop_truth["average_price"],
                                "STOPPED_OUT", "sl_m_filled_during_cancel",
                            )
                        else:
                            await _record_confirmed_momentum_partial(
                                pos, sold_shares=stop_truth["filled_quantity"],
                                fill_price=stop_truth["average_price"],
                                new_stop=None, reason="sl_m_partial_during_cancel",
                            )
                            await _page_unconfirmed_square_off(
                                ticker, {
                                    "state": stop_truth["status"],
                                    "filled_quantity": stop_truth["filled_quantity"],
                                    "remaining_quantity": int(pos["shares"]) - stop_truth["filled_quantity"],
                                    "order_id": sl_order_id,
                                }, "protective-stop cancel",
                            )
                        continue
                    if stop_truth["filled_quantity"] > 0 or not stop_truth["safe_to_sell"]:
                        await _notify_operator(
                            f"🚨 `{ticker}` exit refused: protective stop "
                            f"`{sl_order_id}` is {stop_truth['status']} and its "
                            "fill/cancel state is not safely reconciled. No second "
                            "SELL was sent.",
                            event="momentum_sl_cancel_unresolved",
                        )
                        continue

                payload = {
                    "ticker": ticker,
                    "shares": pos['shares'],
                    "order_type": "LIMIT",
                    "limit_price": snap_to_tick(ltp * 0.999, -1),
                    "product_type": pos.get('product_type', 'MIS'),
                    "reason": f"MOMENTUM_EXIT_{decision['reason']}",
                    "idempotency_key": _momentum_square_off_key(pos),
                }
                resp = await _post_square_off_with_reconcile(
                    f"{container_a_url}/api/orders/square-off", payload,
                )
                if resp is None:
                    continue
                evidence = _square_off_fill_evidence(resp, int(pos["shares"]))
                if evidence["complete"]:
                    await _close_momentum_position(
                        pos, evidence["average_price"],
                        momentum_exit_status(decision["reason"]),
                        decision["reason"],
                    )
                elif evidence["confirmed"]:
                    persisted = await _record_confirmed_momentum_partial(
                        pos, sold_shares=evidence["filled_quantity"],
                        fill_price=evidence["average_price"], new_stop=None,
                        reason=decision["reason"],
                    )
                    residual = evidence["remaining_quantity"]
                    new_sl_id = await _rearm_momentum_residual_stop(pos, residual)
                    if new_sl_id and persisted:
                        async with aiosqlite.connect(settings.DB_PATH) as db:
                            await db.execute(
                                "UPDATE positions SET sl_order_id=? WHERE ticker=? "
                                "AND source='MOMENTUM' AND exit_date IS NULL",
                                (new_sl_id, ticker),
                            )
                            await db.commit()
                    if not new_sl_id:
                        await _notify_operator(
                            f"🚨 `{ticker}` partial exit left {residual} shares "
                            "UNPROTECTED because the residual SL could not be armed. "
                            "Place protection or flatten manually now.",
                            event="momentum_residual_unprotected",
                        )
                    await _page_unconfirmed_square_off(ticker, evidence, "exit")
                else:
                    await _page_unconfirmed_square_off(ticker, evidence, "exit")

            elif action == ACTION_SCALE_OUT:
                # [SCALE-OUT 2026-08-04] Order of operations is a safety
                # property, not a preference.
                #
                # The resting SL covers the FULL position. If we sold the
                # partial first and the stop then triggered, it would sell the
                # full quantity again and leave us SHORT -- the exact failure
                # the target path avoids by cancelling before it sells.
                #
                # So: shrink the stop to the runner's quantity FIRST, then sell.
                # Between the two calls the position is briefly over-protected
                # (full size held, partial size stopped), which risks giving
                # back some of the partial's gain. That is a bounded, monetary
                # cost. The other ordering risks an unintended short position,
                # which is not.
                scale_shares = int(decision["scale_shares"])
                runner_shares = int(pos["shares"]) - scale_shares
                new_stop = decision["new_stop"]

                if sl_order_id:
                    res = await kite.modify_order(
                        order_id=str(sl_order_id),
                        quantity=runner_shares,
                        trigger_price=snap_to_tick(new_stop, -1),
                    )
                    if res.get("status") == "ERROR":
                        # Could not resize the stop, so we must not sell. Hold
                        # the full position under its existing (full-size,
                        # correct) stop and try again next tick.
                        logger.error(
                            "momentum_scale_out_sl_resize_failed_holding",
                            ticker=ticker, sl_order_id=sl_order_id,
                            runner_shares=runner_shares,
                            message=res.get("message"),
                        )
                        continue

                payload = {
                    "ticker": ticker,
                    "shares": scale_shares,
                    "order_type": "LIMIT",
                    "limit_price": snap_to_tick(ltp * 0.999, -1),
                    "product_type": pos.get('product_type', 'MIS'),
                    "reason": f"MOMENTUM_SCALE_OUT_{decision['reason']}",
                    "idempotency_key": _momentum_square_off_key(pos),
                }
                resp = await _post_square_off_with_reconcile(
                    f"{container_a_url}/api/orders/square-off", payload,
                )
                if resp is None:
                    # Submission may have filled. Do not blindly resize the
                    # stop again; the operator page owns reconciliation.
                    continue

                evidence = _square_off_fill_evidence(resp, scale_shares)
                if not evidence["confirmed"]:
                    # Stop was pre-shrunk, but no sale is confirmed. Restore
                    # full coverage before leaving the position unchanged.
                    if sl_order_id and evidence["state"] in {"REJECTED", "CANCELLED"}:
                        await kite.modify_order(
                            order_id=str(sl_order_id), quantity=int(pos["shares"]),
                            trigger_price=snap_to_tick(new_stop, -1),
                        )
                    await _page_unconfirmed_square_off(ticker, evidence, "scale-out")
                    continue
                actual_runner = int(pos["shares"]) - evidence["filled_quantity"]
                if sl_order_id and actual_runner != runner_shares:
                    await kite.modify_order(
                        order_id=str(sl_order_id), quantity=actual_runner,
                        trigger_price=snap_to_tick(new_stop, -1),
                    )
                await _record_confirmed_momentum_partial(
                    pos, sold_shares=evidence["filled_quantity"],
                    fill_price=evidence["average_price"], new_stop=new_stop,
                    reason=decision["reason"], mark_t1=True,
                )
                if not evidence["complete"]:
                    await _page_unconfirmed_square_off(ticker, evidence, "scale-out")
                logger.info(
                    "momentum_scaled_out", ticker=ticker,
                    fill_price=evidence["average_price"],
                    sold=evidence["filled_quantity"], runner=actual_runner,
                    new_stop=new_stop, reason=decision["reason"],
                )

            elif action == ACTION_TRAIL:
                new_stop = decision["new_stop"]
                if sl_order_id:
                    # Modify in place rather than cancel+replace: a cancel/place
                    # pair leaves the position momentarily unprotected.
                    res = await kite.modify_order(
                        order_id=str(sl_order_id),
                        trigger_price=snap_to_tick(new_stop, -1),
                    )
                    if res.get("status") == "ERROR":
                        logger.error("momentum_trail_modify_failed", ticker=ticker,
                                     new_stop=new_stop, message=res.get("message"))
                        continue

                async with aiosqlite.connect(settings.DB_PATH) as db:
                    await db.execute(
                        "UPDATE positions SET trailing_stop_current=? "
                        "WHERE ticker=? AND source='MOMENTUM' AND status='OPEN'",
                        (new_stop, ticker),
                    )
                    await db.commit()
                logger.info("momentum_trail_ratcheted", ticker=ticker,
                            new_stop=new_stop, reason=decision["reason"], ltp=ltp)

        except Exception as e:
            logger.error("momentum_monitor_failed", ticker=ticker, error=str(e))


async def _restore_momentum_protection_after_eod_abort(
    pos: dict, old_order_id: str, context: str,
) -> None:
    """Re-arm and persist protection after EOD cancelled the prior stop."""
    ticker = pos["ticker"]
    replacement = await _rearm_momentum_residual_stop(pos, int(pos["shares"]))
    persisted = False
    if replacement:
        try:
            async with aiosqlite.connect(settings.DB_PATH) as db:
                cur = await db.execute(
                    "UPDATE positions SET sl_order_id=? WHERE ticker=? "
                    "AND source='MOMENTUM' AND exit_date IS NULL "
                    "AND shares=? AND sl_order_id=?",
                    (replacement, ticker, int(pos["shares"]), str(old_order_id)),
                )
                await db.commit()
                persisted = cur.rowcount == 1
        except Exception as exc:
            logger.error(
                "momentum_eod_replacement_stop_persist_failed", ticker=ticker,
                replacement_order_id=replacement, error=str(exc),
            )
    if replacement and persisted:
        await _notify_operator(
            f"⚠️ `{ticker}` EOD square-off aborted ({context}); protective "
            f"stop was restored as `{replacement}`. Position remains open; "
            "verify it in Zerodha.",
            event="momentum_eod_deadline_stop_restored",
        )
        return
    await _notify_operator(
        f"🚨 `{ticker}` EOD square-off aborted ({context}) after protective "
        "stop cancellation, and replacement protection could not be durably "
        "confirmed. Position may be UNPROTECTED; reconcile or flatten manually.",
        event="momentum_eod_deadline_unprotected",
    )


async def auto_square_momentum():
    """Acquire exclusive EOD ownership and enforce the pre-CAS deadline."""
    # [CALENDAR-GATE 2026-07-03] Keep the gate in the scheduler-visible
    # handler.  This path can place real broker orders.
    today = datetime.now(IST).date()
    if not await is_trading_day(today, settings.DB_PATH):
        logger.info("auto_square_momentum_skip reason=non_trading_day")
        return
    initial = _momentum_exit_clock()
    if not initial["eod_started"]:
        logger.warning("auto_square_momentum_skip reason=before_1513")
        return
    if not initial["submit_allowed"]:
        await _notify_operator(
            "🚨 Momentum EOD square-off did not start before the 15:14:30 IST "
            "submission cutoff. No protective stop was cancelled and no late "
            "sell was sent; verify all open momentum positions manually.",
            event="momentum_eod_start_deadline_missed",
        )
        return
    async with _momentum_exit_ownership_lock:
        if not _momentum_exit_clock()["submit_allowed"]:
            await _notify_operator(
                "🚨 Momentum EOD ownership was delayed beyond 15:14:30 IST. "
                "No protective stop was cancelled and no late sell was sent; "
                "verify all open momentum positions manually.",
                event="momentum_eod_lock_deadline_missed",
            )
            return
        await _auto_square_momentum_owned()


async def _auto_square_momentum_owned():
    """
    [AUTO-SQUARE] 15:13 IST: Square off all open MOMENTUM positions before CAS.
    Calls Container A's internal square-off API.
    Uses smart order selection based on P&L state and market conditions.

    [CALENDAR-GATE 2026-07-03] Skip on weekends + NSE holidays. Real
    financial risk: this places exit orders via Container A. On a
    non-trading day any momentum position held across the weekend
    would be squared at stale weekend prices (or wrong if Container A
    is also unwary). Mirrors run_force_close_mis and penny_edge_exit
    gates. Same two-gate pattern as run_screener.
    """
    import httpx as _httpx
    from datetime import time

    # [CALENDAR-GATE 2026-07-03] weekend / NSE-holiday early-return.
    today = datetime.now(IST).date()
    if not await is_trading_day(today, settings.DB_PATH):
        logger.info("auto_square_momentum_skip reason=non_trading_day")
        return

    open_pos = await get_open_positions(settings.DB_PATH)
    momentum_pos = [p for p in open_pos if p.get('source') == 'MOMENTUM']

    # [PHANTOM-GUARD 2026-07-26] Never send an exit order for a position that
    # already carries an exit_date. get_open_positions() now filters these out,
    # so reaching here means a new writer regressed -- refuse the order and page
    # the operator rather than selling stock the account may not hold.
    #
    # This is the guard that was missing on 2026-07-21..24: two fully-exited
    # positions stayed classified as open and this job placed 8 real Zerodha
    # MIS sell orders against them, one pair per session, with no order record
    # written anywhere. Defence in depth, because the blast radius is real money.
    already_exited = [p for p in momentum_pos if p.get('exit_date')]
    if already_exited:
        for p in already_exited:
            logger.error(
                "auto_square_refused_phantom", ticker=p['ticker'],
                exit_date=p.get('exit_date'), status=p.get('status'),
                message="position already has an exit_date -- refusing to place an exit order",
            )
        await _notify_operator(
            "🛑 *Auto-square refused* — "
            + ", ".join(f"`{p['ticker']}`" for p in already_exited)
            + " already carry an exit_date but were returned as open. "
            "No order was sent. The open-position query or a status writer has regressed.",
            event="auto_square_refused_phantom",
        )
        momentum_pos = [p for p in momentum_pos if not p.get('exit_date')]

    if not momentum_pos:
        logger.info("auto_square_none", message="no_momentum_positions")
        return


    container_a_url = settings.CONTAINER_A_URL

    for pos in momentum_pos:
        ticker = pos['ticker']
        protective_stop_cancelled = False
        square_off_submitted = False
        try:
            if not _momentum_exit_clock()["submit_allowed"]:
                await _notify_operator(
                    f"🚨 `{ticker}` was not processed before the 15:14:30 IST "
                    "submission cutoff. Its protective stop was left unchanged; "
                    "verify the position manually.",
                    event="momentum_eod_position_deadline_missed",
                )
                continue
            # Never stack the EOD SELL beside a resting full-size SL. Confirm
            # cancellation (or consume an already-COMPLETE stop) first.
            sl_order_id = pos.get("sl_order_id")
            if sl_order_id:
                stop_truth = await _cancel_order_truth(
                    kite, str(sl_order_id), int(pos["shares"]),
                )
                if stop_truth["confirmed_fill"]:
                    if stop_truth["filled_quantity"] >= int(pos["shares"]):
                        await _close_momentum_position(
                            pos, stop_truth["average_price"],
                            "STOPPED_OUT", "sl_m_filled_at_eod",
                        )
                    else:
                        await _record_confirmed_momentum_partial(
                            pos, sold_shares=stop_truth["filled_quantity"],
                            fill_price=stop_truth["average_price"], new_stop=None,
                            reason="sl_m_partial_at_eod",
                        )
                        await _page_unconfirmed_square_off(
                            ticker, {
                                "state": stop_truth["status"],
                                "filled_quantity": stop_truth["filled_quantity"],
                                "remaining_quantity": int(pos["shares"]) - stop_truth["filled_quantity"],
                                "order_id": sl_order_id,
                            }, "auto-square protective-stop cancel",
                        )
                    continue
                if stop_truth["filled_quantity"] > 0 or not stop_truth["safe_to_sell"]:
                    await _notify_operator(
                        f"🚨 `{ticker}` auto-square refused: protective stop "
                        f"`{sl_order_id}` is {stop_truth['status']}, not safely reconciled. "
                        "No second SELL was submitted; reconcile manually.",
                        event="auto_square_sl_unresolved",
                    )
                    continue
                protective_stop_cancelled = True
                if not _momentum_exit_clock()["submit_allowed"]:
                    await _restore_momentum_protection_after_eod_abort(
                        pos, str(sl_order_id), "deadline elapsed after stop cancellation",
                    )
                    protective_stop_cancelled = False
                    continue
            # Fetch current LTP to decide order type
            async with _httpx.AsyncClient() as _client:
                ltp_resp = await _client.get(
                    f"{container_a_url}/api/orders/ltp",
                    headers={"X-Internal-Secret": settings.INTERNAL_API_SECRET},
                    params={"ticker": ticker},
                    timeout=5.0
                )
            ltp_data = ltp_resp.json()
            ltp = float(ltp_data.get("ltp", pos['entry_price']))

            current_pnl   = (ltp - pos['entry_price']) * pos['shares']
            is_profitable = current_pnl > 0

            # Smart order selection [as per user-confirmed factors]:
            # 1. In profit -> limit order to protect gains
            # 2. After 15:00 IST -> always market order (time constraint)
            # 3. Fast-moving stock (LTP far from entry) -> market order
            # 4. Low liquidity -> limit order to avoid slippage
            now_ist = datetime.now(IST)

            price_movement_pct = abs(ltp - pos['entry_price']) / pos['entry_price']
            is_fast_moving     = price_movement_pct > 0.02

            # [FIX] Zerodha API rejects MARKET orders without market_protection.
            # Use LIMIT everywhere; a SELL LIMIT slightly below LTP fills essentially
            # instantly on any liquid NSE stock, so there is no EOD fill-miss risk.
            # snap_to_tick(..., -1) rounds DOWN to the nearest 0.10-rupee tick,
            # which satisfies both 0.05 and 0.10 tick-size stocks.
            if is_profitable and not is_fast_moving and now_ist.time() < time(15, 0):
                order_type  = "LIMIT"
                limit_price = snap_to_tick(ltp * 0.999, -1)  # 0.1% below LTP -- protect gains
            else:
                order_type  = "LIMIT"
                limit_price = snap_to_tick(ltp * 0.995, -1)  # 0.5% below LTP -- aggressive fill for EOD exit

            payload = {

                "ticker":       ticker,
                "shares":       pos['shares'],
                "order_type":   order_type,
                "limit_price":  limit_price,
                "product_type": pos.get('product_type', 'MIS'),
                "reason":       "AUTO_SQUARE_EOD",
                "idempotency_key": _momentum_square_off_key(pos),
            }

            if not _momentum_exit_clock()["submit_allowed"]:
                if protective_stop_cancelled:
                    await _restore_momentum_protection_after_eod_abort(
                        pos, str(sl_order_id), "deadline elapsed before square-off submission",
                    )
                    protective_stop_cancelled = False
                else:
                    await _notify_operator(
                        f"🚨 `{ticker}` missed the 15:14:30 IST submission "
                        "cutoff. No late sell was sent; verify manually.",
                        event="momentum_eod_submit_deadline_missed",
                    )
                continue
            square_off_submitted = True
            resp = await _post_square_off_with_reconcile(
                f"{container_a_url}/api/orders/square-off", payload,
            )
            if resp is None:
                continue
            evidence = _square_off_fill_evidence(resp, int(pos["shares"]))
            _order_id = evidence["order_id"]
            logger.info("auto_square_sent", ticker=ticker,
                        order_type=order_type, pnl_estimate=current_pnl,
                        order_id=_order_id, shares=pos['shares'],
                        state=evidence["state"],
                        filled=evidence["filled_quantity"])

            if not evidence["complete"]:
                if evidence["confirmed"]:
                    await _record_confirmed_momentum_partial(
                        pos, sold_shares=evidence["filled_quantity"],
                        fill_price=evidence["average_price"], new_stop=None,
                        reason="auto_square",
                    )
                await _page_unconfirmed_square_off(
                    ticker, evidence, "auto-square",
                )
                continue

            confirmed_fill = evidence["average_price"]

            # Settle strictly from broker evidence, never from the pre-order LTP.
            gross = (confirmed_fill - pos['entry_price']) * pos['shares']
            # [AUDIT-FIX-1.2] Derive is_intraday from product_type (was
            # hardcoded True, understating CNC costs).
            costs = calc_zerodha_costs(
                pos['entry_price'], confirmed_fill, pos['shares'],
                is_intraday=_is_intraday_from_product_type(pos.get('product_type')),
            )
            realised_pnl = gross - costs
            total_pnl, r_multiple = _aggregate_momentum_close(
                pos, realised_pnl
            )

            # [LEDGER-INTEGRITY 2026-07-26] The close must actually persist before
            # its P&L is booked. This UPDATE used to match on status='OPEN' only,
            # so a CLOSED_T1 momentum row matched ZERO rows -- silently. The
            # position therefore stayed "open" while record_trade_close() below
            # ran regardless, writing a fresh loss computed against the original
            # entry price. Repeated daily that produced 8 fabricated TRADE_CLOSED
            # rows (-Rs 178.31) and tripped CB_CONSECUTIVE_LOSSES on losses that
            # never happened.
            #
            # Two changes: accept CLOSED_T1 as a closable state (a runner being
            # squared at EOD is legitimate), and gate the ledger write on rowcount.
            async with aiosqlite.connect(settings.DB_PATH) as db:
                cur = await db.execute("""
                    UPDATE positions
                    SET status='CLOSED_MANUAL', exit_price=?, exit_date=?,
                        realised_pnl=?, r_multiple=?
                    WHERE ticker=? AND source='MOMENTUM'
                      AND status IN ('OPEN', 'CLOSED_T1') AND exit_date IS NULL
                """, (confirmed_fill, datetime.now(timezone.utc).isoformat(),
                      total_pnl, r_multiple, ticker))
                await db.commit()
                rows_closed = cur.rowcount

            if rows_closed != 1:
                # Never book P&L for a close that did not land. One order has
                # already gone to the broker, so this needs a human.
                logger.error(
                    "auto_square_close_not_persisted", ticker=ticker,
                    rows_affected=rows_closed, order_id=_order_id,
                    message="exit order placed but no position row closed -- ledger NOT written",
                )
                await _notify_operator(
                    f"🚨 *Auto-square inconsistency* — `{ticker}`: exit order "
                    f"(id `{_order_id}`) was placed but {rows_closed} position rows "
                    "closed. No P&L was booked. Reconcile against the Zerodha "
                    "orderbook before the next session.",
                    event="auto_square_close_not_persisted",
                )
                continue

            # [SOURCE-REQUIRED 2026-07-26] Was booking momentum square-offs into
            # the swing pool via the old "SYSTEM" default.
            await record_trade_close(settings.DB_PATH, ticker, realised_pnl,
                                     r_multiple=r_multiple, notes="auto_square",
                                     outcome_pnl=total_pnl,
                                     outcome_r_multiple=r_multiple,
                                     source=pos.get('source') or 'MOMENTUM')
            logger.info("auto_square_position_closed", ticker=ticker,
                        exit_price=confirmed_fill, pnl=round(total_pnl, 2), r=round(r_multiple, 4))

        except Exception as e:
            logger.error("auto_square_failed", ticker=ticker, error=str(e))
            if protective_stop_cancelled and not square_off_submitted:
                await _restore_momentum_protection_after_eod_abort(
                    pos, str(pos.get("sl_order_id") or ""),
                    "pre-submission failure",
                )
            # On failure: send Telegram alert for manual intervention
            await _notify_telegram_square_off_failure(ticker, pos)


async def _paper_ltp(ticker: str):
    """Read-only LTP for the paper book.

    [MOMENTUM-PAPER 2026-07-26] GET only. This is the paper book's single
    outbound call and it cannot mutate anything at the broker.
    """
    import httpx as _httpx
    try:
        async with _httpx.AsyncClient() as _client:
            resp = await _client.get(
                f"{settings.CONTAINER_A_URL}/api/orders/ltp",
                headers={"X-Internal-Secret": settings.INTERNAL_API_SECRET},
                params={"ticker": ticker}, timeout=5.0,
            )
        return float((resp.json() or {}).get("ltp") or 0.0) or None
    except Exception as exc:
        logger.warning("paper_ltp_failed", ticker=ticker, error=str(exc))
        return None


async def _run_momentum_paper_monitor():
    """Interval job: manage open momentum paper positions during market hours."""
    from datetime import time as _dt_time

    from momentum_paper import momentum_paper_monitor

    now_ist = datetime.now(IST)
    if not await is_trading_day(now_ist.date(), settings.DB_PATH):
        return
    # Same window as the live monitor: after 15:15 the square-off owns the book.
    if not (_dt_time(9, 15) <= now_ist.time() <= _dt_time(15, 15)):
        return
    await momentum_paper_monitor(settings.DB_PATH, _paper_ltp, now_ist)


async def _run_momentum_paper_square_off():
    """15:15 IST: flatten the momentum paper book (intraday strategy)."""
    from momentum_paper import momentum_paper_square_off

    now_ist = datetime.now(IST)
    if not await is_trading_day(now_ist.date(), settings.DB_PATH):
        return
    await momentum_paper_square_off(settings.DB_PATH, _paper_ltp, now_ist)


async def momentum_eod_warning():
    """15:10 IST: Send 3-minute warning before auto-square.

    [CALENDAR-GATE 2026-07-03] Skip on weekends + NSE holidays. On a
    non-trading day the auto-square it warns about is also suppressed
    (auto_square_momentum gates too), so this warning becomes a false
    alarm.
    """
    # [CALENDAR-GATE 2026-07-03] weekend / NSE-holiday early-return.
    today = datetime.now(IST).date()
    if not await is_trading_day(today, settings.DB_PATH):
        logger.info("momentum_eod_warning_skip reason=non_trading_day")
        return
    open_pos = await get_open_positions(settings.DB_PATH)
    momentum_pos = [p for p in open_pos if p.get('source') == 'MOMENTUM']
    if not momentum_pos:
        return

    tickers = ", ".join(p['ticker'] for p in momentum_pos)
    # Uses existing Telegram notification mechanism in Container A
    import httpx as _httpx
    try:
        async with _httpx.AsyncClient() as _client:
            await _client.post(
                f"{settings.CONTAINER_A_URL}/api/internal/notify",
                json={"message": f"⚠️ AUTO-SQUARE in 3 min: {tickers}"},
                headers={"X-Internal-Secret": settings.INTERNAL_API_SECRET},
                timeout=5.0
            )
    except Exception as e:
        logger.error("eod_warning_failed", error=str(e))

async def _notify_telegram_square_off_failure(ticker: str, pos: dict):
    """Notify Telegram about auto-square failure for manual intervention."""
    import httpx as _httpx
    msg = f"🚨 **CRITICAL: Auto-Square Failed** 🚨\nTicker: {ticker}\nShares: {pos['shares']}\nPlease square off manually in Zerodha immediately!"
    try:
        async with _httpx.AsyncClient() as _client:
            await _client.post(
                f"{settings.CONTAINER_A_URL}/api/internal/notify",
                json={"message": msg},
                headers={"X-Internal-Secret": settings.INTERNAL_API_SECRET},
                timeout=5.0
            )
    except Exception as e:
        logger.error("telegram_notification_failed", error=str(e))


async def _notify_momentum_heartbeat(
    scan_time,
    tickers_scanned: int,
    raw_signals_count: int,
    accepted_count: int,
    rejected: list,
    momentum_pool: float
):
    """Send a detailed heartbeat to Telegram showing per-gate rejection breakdown."""
    import httpx as _httpx
    time_str      = scan_time.strftime("%H:%M IST")
    rejected_count = len(rejected)

    msg = (
        f"⏱ **Momentum Scan @ {time_str}**\n"
        f"Scanned: `{tickers_scanned}` | Raw hits: `{raw_signals_count}` | Accepted: `{accepted_count}`\n"
        f"Rejected: `{rejected_count}` | Pool: `Rs{momentum_pool:,.2f}`\n"
    )
    if accepted_count == 0:
        msg += "❌ No new signals - all gates filtered out.\n"

    if rejected:
        # Group rejections by reason
        reason_counts: dict = {}
        for r in rejected:
            reason = r.get("reject_reason", "unknown")
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

        msg += "\n[STATS] **Gate Rejection Breakdown:**\n"
        for reason, count in sorted(reason_counts.items(), key=lambda x: x[1], reverse=True):
            display = reason.replace("_", " ").title()
            msg += f"* {display}: `{count}`\n"

        # Show up to 8 informative rejected tickers (skip trivial data-missing reasons)
        _skip = {
            "intraday_data_empty", "insufficient_intraday_candles",
            "swing_position_exists", "daily_data_missing_for_prev_high",
            "prev_day_data_not_found",
        }
        interesting = [r for r in rejected if r.get("reject_reason") not in _skip]
        if interesting:
            msg += "\n🔍 **Sample Gate Failures:**\n"
            for r in interesting[:8]:
                ticker = r.get("ticker", "???")
                reason = r.get("reject_reason", "unknown").replace("_", " ").title()
                detail = ""
                try:
                    if "ratio" in r:
                        detail = f" [vol: {r['ratio']:.2f}x]"
                    elif "intraday_high" in r:
                        detail = f" [close:{r.get('close', 0):.1f} hi:{r['intraday_high']:.1f} thr:{r.get('threshold', 0):.1f}]"
                    elif "current_vwap" in r:
                        detail = f" [close:{r.get('current_close', 0):.1f} vwap:{r['current_vwap']:.1f}]"
                    elif "prev_high" in r:
                        detail = f" [close:{r.get('close', 0):.1f} prevhi:{r['prev_high']:.1f}]"
                except (TypeError, ValueError, KeyError):
                    detail = ""
                msg += f"* **{ticker}**: {reason}{detail}\n"

    try:
        async with _httpx.AsyncClient() as _client:
            await _client.post(
                f"{settings.CONTAINER_A_URL}/api/internal/notify",
                json={"message": msg},
                headers={"X-Internal-Secret": settings.INTERNAL_API_SECRET},
                timeout=5.0
            )
    except Exception as e:
        logger.error("momentum_heartbeat_failed", error=str(e))


async def compute_performance_report(db_path: str) -> PerformanceReport:
    """
    [AUDIT-FIX-2.6 2026-06-25] Shared async helper for performance data.

    Pre-fix: `cmd_performance` in operator_status.py called the /performance
    HTTP route via fastapi.testclient.TestClient. That worked but was
    awkward -- it spun up an in-process test client to call a route
    that was 5 lines away in the same module, and required the FastAPI
    app to be importable in the cmd path (which sometimes it isn't in
    test contexts).

    Now both the HTTP route (get_performance) and the Telegram cmd
    handler (cmd_performance) call this shared async function. The
    HTTP route is just a thin wrapper around it.
    """
    # Strict separation: /performance reports the Nifty-subsystem balance.
    # Penny trades are not swing positions -- they're listed separately via
    # /bankroll/breakdown.penny.
    bankroll = await nifty_bankroll(db_path)
    open_pos = await get_open_positions(db_path)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row   # named column access -- never use positional indices
        async with db.execute(
            "SELECT * FROM positions WHERE status NOT IN ('OPEN', 'CLOSED_T1')"
        ) as cursor:
            closed_trades = [dict(row) for row in await cursor.fetchall()]

    def _pnl(t: dict) -> float:
        return float(t.get("realised_pnl") or 0)

    def _r(t: dict) -> float:
        return float(t.get("r_multiple") or 0)

    total_trades = len(closed_trades) + len(open_pos)
    win_count    = sum(1 for t in closed_trades if _pnl(t) > 0)
    loss_count   = sum(1 for t in closed_trades if _pnl(t) < 0)
    total_pnl    = sum(_pnl(t) for t in closed_trades)
    avg_r        = sum(_r(t) for t in closed_trades) / len(closed_trades) if closed_trades else 0.0

    return PerformanceReport(
        as_of=datetime.now(timezone.utc),
        total_trades_taken=total_trades,
        open_positions_count=len(open_pos),
        closed_trades_count=len(closed_trades),
        win_count=win_count,
        loss_count=loss_count,
        win_rate=win_count / len(closed_trades) if closed_trades else 0.0,
        avg_r_multiple=avg_r,
        avg_winner_r=0.0,
        avg_loser_r=0.0,
        profit_factor=0.0,
        total_realised_pnl=total_pnl,
        current_bankroll=bankroll,
        max_drawdown_pct=0.0,
        current_drawdown_pct=0.0,
        consecutive_losses=0,
        max_consecutive_losses=0,
        best_trade_r=0.0,
        worst_trade_r=0.0,
        avg_hold_days=0.0
    )
async def notify_screener_results(
    strategy_type: str,
    accepted: list,
    rejected: list,
    regime: str,
    bankroll: float,
    pool: float = None
):
    """
    Sends a detailed summary of the screener run to Telegram via Container A.

    [MED-002 / ROADMAP-4.6 2026-07-12] Default OFF: the agent (Container
    C) sends the AI-enriched alert WITH working EXEC buttons for the
    same scan, so the operator got two messages per cycle and only one
    was actionable. The agent path is now guarded (2.2 healthcheck +
    autoheal + veto notices), and the reject histograms this summary
    carried are persisted daily by ops_metrics (2.8). Re-enable with
    SCREENER_PLAIN_SUMMARY_ENABLED=true in .env if you miss it.
    """
    if not settings.SCREENER_PLAIN_SUMMARY_ENABLED:
        logger.info(
            "screener_plain_summary_suppressed strategy=%s accepted=%d rejected=%d",
            strategy_type, len(accepted), len(rejected),
        )
        return
    import httpx as _httpx

    msg = f"🔍 **{strategy_type} Screener Run**\n"
    msg += f"Regime: `{regime}` | Bankroll: `Rs{bankroll:,.2f}`\n"
    if pool:
        msg += f"Strategy Pool: `Rs{pool:,.2f}`\n"
    msg += "---"
    
    if accepted:
        msg += f"\n✅ **Signals Found ({len(accepted)}):**\n"
        for s in accepted:
            ticker = s.ticker if hasattr(s, 'ticker') else s.get('ticker')
            price = s.close if hasattr(s, 'close') else s.get('close')
            shares = s.shares if hasattr(s, 'shares') else s.get('shares')
            msg += f"* **{ticker}** @ {price} (Qty: {shares})\n"
    else:
        msg += "\n❌ No signals passed all filters."


    if rejected:
        # Group rejections by reason
        reason_counts = {}
        for r in rejected:
            reason = r.get('reject_reason', 'unknown')
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        
        msg += "\n\n[STATS] **Rejection Summary:**\n"
        # Sort by count descending
        for reason, count in sorted(reason_counts.items(), key=lambda x: x[1], reverse=True):
            # Clean up reason string for display
            display_reason = reason.replace("_", " ").title()
            msg += f"* {display_reason}: {count}\n"
        
        if len(rejected) > 0:
            # Group specific rejections by ticker for meaningful examples
            msg += "\n🔍 **Rejected Tickers:**\n"
            # Sort rejections to show the most "interesting" ones first (e.g. not empty data)
            interesting_rejections = [r for r in rejected if "empty" not in r.get('reject_reason', '').lower()]
            if not interesting_rejections:
                interesting_rejections = rejected

            # Show up to 10 examples to be comprehensive
            for r in interesting_rejections[:10]:
                ticker = r.get('ticker', '???')
                reason = r.get('reject_reason', 'unknown').replace("_", " ").title()
                msg += f"* {ticker}: {reason}\n"

            
    # Send to Container A for Telegram delivery

    try:
        async with _httpx.AsyncClient() as _client:
            await _client.post(
                f"{settings.CONTAINER_A_URL}/api/internal/notify",
                json={"message": msg},
                headers={"X-Internal-Secret": settings.INTERNAL_API_SECRET},
                timeout=5.0
            )
    except Exception as e:
        logger.error("telegram_notification_failed", error=str(e))


# ---------------------------------------------------------------------------
# [ROADMAP-4.1 stage 3 2026-07-13] The 24 HTTP handlers moved to routes_*.py.
#
# Imported HERE, at the bottom of the module, and not at the top: the route
# modules do `import main as _main` themselves, so importing them before main's
# globals exist would be legal (sys.modules already holds this module) but
# needlessly fragile. By this point main is fully defined.
#
# include_router preserves paths, methods, endpoint names and response models
# exactly -- the 24-route characterization golden is the proof, and it is
# unchanged by this commit.
# ---------------------------------------------------------------------------
from routes_ops import router as _ops_router
from routes_portfolio import router as _portfolio_router
from routes_commands import router as _commands_router
from routes_backtest import router as _backtest_router
from routes_fno_experiments import router as _fno_experiments_router
from routes_penny_experiments import router as _penny_experiments_router
from routes_promotion_readiness import router as _promotion_readiness_router
from routes_hedge import router as _hedge_router
from routes_holidays import router as _holidays_router  # WORKFLOW-J.5
from routes_market_session import router as _market_session_router
from routes_tv_fyers import router as _tv_fyers_router  # SMIT-FYERS-OPTIONS

app.include_router(_ops_router)
app.include_router(_portfolio_router)
app.include_router(_commands_router)
app.include_router(_backtest_router)
app.include_router(_fno_experiments_router)
app.include_router(_penny_experiments_router)
app.include_router(_promotion_readiness_router)
app.include_router(_hedge_router)
app.include_router(_holidays_router)  # WORKFLOW-J.5
app.include_router(_market_session_router)
app.include_router(_tv_fyers_router)  # SMIT-FYERS-OPTIONS
