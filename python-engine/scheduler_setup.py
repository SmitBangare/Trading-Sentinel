"""[ROADMAP-4.1 stage 2, 2026-07-13] APScheduler job registration.

Extracted verbatim from main.py: register_fno_scheduler_jobs and
register_penny_scheduler_jobs, and the 8 async closures they define.

This is the piece stage 1 deliberately left behind. Python resolves a
function's globals at CALL time against its DEFINING module, so a closure that
moves house and loses a free name raises NameError only when the job fires --
in production, inside a `_safe` wrapper that catches it, logs it, and returns.
The scan then never runs, silently. Import still succeeds, the job census still
sees the registration, and nothing goes red. That is the 2026-07-13 failure
signature, and it is why this move waited for
tests/test_scheduler_closures_invoke.py, which CALLS all 8 closures.

The free-name set was enumerated by AST before the move, not guessed. Three
names must stay late-bound (`kite`, `is_trading_day`, `_fno_regime_str` --
resolved as `_main.X` on every call); the rest are aliased once at registration
time, which is semantically identical because add_job() captures the callable
then anyway, and which keeps every add_job() call site textually unchanged so
the 32-entry census can still prove nothing was dropped.
"""
import asyncio
from datetime import datetime
from time import monotonic

from config import settings


def _log_fno_watchdog_payload(logger, payload):
    """Emit the zero-accept diagnosis as queryable structured evidence.

    The Telegram text already carried the histogram, but the operational log
    only retained days/evaluations/dead_gate.  That made a later audit unable
    to distinguish a functioning capital guard from a uniformly dead gate
    once the notification itself was unavailable.
    """
    histogram = dict(payload.get("histogram") or {})
    reject_total = sum(histogram.values())
    top_reason, top_count = (
        max(histogram.items(), key=lambda item: item[1])
        if histogram else ("none", 0)
    )
    fields = {
        "days": list(payload.get("days") or []),
        "evaluations": int(payload.get("evaluations") or 0),
        "histogram": histogram,
        "self_regulating": bool(payload.get("self_regulating")),
        "dead_gate": payload.get("dead_gate") or "none",
        "top_reject_reason": top_reason,
        "top_reject_count": top_count,
        "top_reject_share": round(top_count / reject_total, 4)
        if reject_total else 0.0,
    }
    if fields["self_regulating"]:
        logger.info("fno_self_regulation_note", **fields)
    else:
        logger.warning("fno_zero_accept_alarm", **fields)



def register_fno_scheduler_jobs(scheduler):
    # [ROADMAP-4.1 stage 2] See register_penny_scheduler_jobs for why `kite`,
    # `is_trading_day` and `_fno_regime_str` are reached through `_main` on every
    # call rather than bound here: the suite patches them by name, and
    # _fno_regime_str reads main._last_regime_state, which mutates at runtime.
    import main as _main
    from scheduler_telemetry import telemetry_job

    IST = _main.IST
    logger = _main.logger
    """
    [FNO 2026-07-10] F&O subsystem scheduler jobs (spec §5/§9.3).
    Module-level function (like register_penny_scheduler_jobs) so tests
    can verify registration without booting the lifespan.

    Jobs:
      - fno_instruments_refresh: 08:00 IST daily (NFO dump -> keyed cache,
        persisted to disk for cold-start rehydration) + startup catchup
      - fno_tick: every FNO_SCAN_INTERVAL_SEC, self-gates to market hours
      - fno_hourly_report: minute=0, 10:00-15:00 IST
      - fno_accept_watchdog: 15:45 IST (zero-accept alarm, §9.2)
    """
    import httpx as _httpx

    async def _run_fno_instruments_refresh():
        # [Rule 55] First-line breadcrumb.
        logger.info(
            "fno_instruments_refresh_invoked now_ist=%s",
            datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
        )
        # [CALENDAR-GATE 2026-07-03] weekend / NSE-holiday early-return.
        # A Saturday NFO dump download is 60-90k rows of wasted fetch;
        # Monday's 08:05 cron owns the fresh book.
        today = datetime.now(IST).date()
        if not await _main.is_trading_day(today, settings.DB_PATH):
            logger.info("fno_instruments_refresh_skip reason=non_trading_day")
            return
        if not _main.kite.access_token:
            logger.warning("fno_instruments_refresh_skip reason=no_access_token")
            return
        try:
            from fno_instruments import get_fno_instruments
            await get_fno_instruments().refresh(_main.kite)
        except Exception as exc:
            logger.error("fno_instruments_refresh_failed err=%s", exc, exc_info=True)

    # [BOOTSTRAP-2026-07-17] The dedicated 08:05 cron + startup catchup are
    # gone: on 2026-07-17 the cron fired while the engine held no fresh
    # token (operator logged in at 08:05), failed once, never retried, and
    # F&O skipped every tick until the power cut. The NFO snapshot is now a
    # daily_bootstrap task -- token-gated at 08:00, re-run on login
    # (post_login_initialization) and by the 10-min safety tick registered
    # in register_penny_scheduler_jobs. _run_fno_instruments_refresh stays
    # callable for operator/manual use.
    _ = _run_fno_instruments_refresh  # kept: manual/ops entry point

    @telemetry_job(settings.DB_PATH, "fno_tick")
    async def _run_fno_tick_safe():
        from fno_orchestrator import format_fno_telegram, run_fno_tick
        # [Rule 55] First-line breadcrumb on EVERY invocation. The tick
        # self-gates below; a missing breadcrumb means the scheduler
        # never fired (rule 62 territory), not a quiet market.
        now_ist = datetime.now(IST)
        nm = now_ist.hour * 60 + now_ist.minute
        # Gate to session hours (09:15 - 15:25; exits incl. the 15:10
        # hard flat need ticks past the entry window).
        if not (9 * 60 + 15 <= nm <= 15 * 60 + 25):
            return
        logger.info("fno_tick_invoked now_ist=%s", now_ist.strftime("%H:%M:%S"))
        today = now_ist.date()
        if not await _main.is_trading_day(today, settings.DB_PATH):
            logger.info("fno_tick_skip reason=non_trading_day")
            return
        if not _main.kite.access_token:
            logger.warning("fno_tick_skip reason=no_access_token")
            return
        started = monotonic()
        summary = None
        outcome = "ok"
        try:
            summary = await run_fno_tick(
                _main.kite,
                regime=_main._fno_regime_str(),
                is_trading_day=True,
            )
            if (summary.get("entries") or summary.get("exits")
                    or summary.get("dr_opened") or summary.get("dr_exits")):
                try:
                    msg = format_fno_telegram(summary)
                    async with _httpx.AsyncClient() as _client:
                        _response = await _client.post(
                            f"{settings.CONTAINER_A_URL}/api/internal/notify",
                            json={"message": msg},
                            headers={"X-Internal-Secret": settings.INTERNAL_API_SECRET or ""},
                            timeout=5.0,
                        )
                        _response.raise_for_status()
                except Exception as notify_exc:
                    logger.warning("fno_tick_notify_failed err=%s", notify_exc)
        except Exception as exc:
            outcome = "failed"
            logger.error("fno_tick_failed err=%s", exc, exc_info=True)
        finally:
            elapsed = monotonic() - started
            fields = {
                "outcome": outcome,
                "elapsed_sec": round(elapsed, 3),
                "cadence_sec": settings.FNO_SCAN_INTERVAL_SEC,
                "note": (summary or {}).get("note") or "none",
                "entries": len((summary or {}).get("entries") or []),
                "exits": len((summary or {}).get("exits") or []),
                "dr_opened": len((summary or {}).get("dr_opened") or []),
                "dr_exits": int((summary or {}).get("dr_exits") or 0),
                "stage_durations_sec": (summary or {}).get("stage_durations_sec") or {},
            }
            logger.info("fno_tick_complete", **fields)
            if elapsed >= settings.FNO_SCAN_INTERVAL_SEC:
                logger.warning("fno_tick_overrun", **fields)
        return {
            "status": "FAILED" if outcome == "failed" else "COMPLETED",
            "reason": "fno_tick_exception" if outcome == "failed" else "none",
            "stage_durations_sec": fields["stage_durations_sec"],
        }

    scheduler.add_job(
        _run_fno_tick_safe, "interval",
        seconds=settings.FNO_SCAN_INTERVAL_SEC,
        id="fno_tick",
        max_instances=1, coalesce=True, misfire_grace_time=120,
    )
    logger.info(
        "fno_cron_registered id=fno_tick interval=%ds max_instances=1 coalesce=True",
        settings.FNO_SCAN_INTERVAL_SEC,
    )

    async def _run_fno_hourly_report_safe():
        from fno_hourly_report import build_hourly_report, is_in_report_window
        now_ist = datetime.now(IST)
        if not is_in_report_window(now_ist):
            return
        logger.info("fno_hourly_report_invoked now_ist=%s", now_ist.strftime("%H:%M:%S"))
        today = now_ist.date()
        if not await _main.is_trading_day(today, settings.DB_PATH):
            logger.info("fno_hourly_report_skip reason=non_trading_day")
            return
        try:
            msg = await build_hourly_report(
                settings.DB_PATH, now_ist, regime=_main._fno_regime_str(),
            )
            async with _httpx.AsyncClient() as _client:
                await _client.post(
                    f"{settings.CONTAINER_A_URL}/api/internal/notify",
                    json={"message": msg},
                    headers={"X-Internal-Secret": settings.INTERNAL_API_SECRET or ""},
                    timeout=5.0,
                )
        except Exception as exc:
            logger.error("fno_hourly_report_failed err=%s", exc, exc_info=True)

    scheduler.add_job(
        _run_fno_hourly_report_safe, "cron", minute=0,
        id="fno_hourly_report",
        max_instances=1, coalesce=True, misfire_grace_time=600,
    )

    # 15:45 IST zero-accept watchdog (§9.2). Read-only over fno_signals --
    # needs no Kite token, so no token guard: it must fire ESPECIALLY on
    # token-less days.
    async def _run_fno_accept_watchdog_safe():
        from fno_accept_watchdog import format_zero_accept_alert, zero_accept_scan
        logger.info(
            "fno_accept_watchdog_invoked now_ist=%s",
            datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
        )
        today = datetime.now(IST).date()
        if not await _main.is_trading_day(today, settings.DB_PATH):
            logger.info("fno_accept_watchdog_skip reason=non_trading_day")
            return
        try:
            payload = await zero_accept_scan(settings.DB_PATH)
            if payload is None:
                logger.info("fno_accept_watchdog_ok")
                return
            # [Rule 72] Degradation is a WARNING, never an INFO -- unless
            # it's the documented self-regulation case.  Preserve the full
            # diagnosis as structured fields, not only in the Telegram text.
            _log_fno_watchdog_payload(logger, payload)
            msg = format_zero_accept_alert(payload)
            async with _httpx.AsyncClient() as _client:
                await _client.post(
                    f"{settings.CONTAINER_A_URL}/api/internal/notify",
                    json={"message": msg},
                    headers={"X-Internal-Secret": settings.INTERNAL_API_SECRET or ""},
                    timeout=5.0,
                )
        except Exception as exc:
            logger.error("fno_accept_watchdog_failed err=%s", exc, exc_info=True)

    scheduler.add_job(
        _run_fno_accept_watchdog_safe, "cron",
        hour=15, minute=45,
        id="fno_accept_watchdog",
        max_instances=1, coalesce=True, misfire_grace_time=600,
    )
    logger.info(
        "fno_cron_registered id=fno_instruments_refresh,fno_hourly_report,"
        "fno_accept_watchdog"
    )




def register_penny_scheduler_jobs(scheduler):
    # [ROADMAP-4.1 stage 2] Resolve main's namespace at REGISTRATION time.
    #
    # Early-bound aliases below: add_job() captures the function object at
    # registration anyway, so aliasing them changes nothing semantically -- and
    # it keeps the add_job() call sites textually identical, which is what lets
    # the 32-entry job census prove nothing was dropped in this move.
    #
    # NOT aliased, and this is the load-bearing part: `kite` and
    # `is_trading_day` are rewritten to `_main.kite` / `_main.is_trading_day`
    # inside the closures, i.e. looked up on every call. The suite patches both
    # BY NAME on main (monkeypatch.setattr(main, "kite", ...) and
    # patch("main.is_trading_day", ...) in ~10 places). Binding them here would
    # capture the real objects once and silently detach every one of those
    # patches -- tests would look like they exercise a fake while hitting the
    # real Kite client.
    import main as _main

    IST = _main.IST
    logger = _main.logger
    run_penny_universe_refresh = _main.run_penny_universe_refresh
    run_penny_regime_compute = _main.run_penny_regime_compute
    run_penny_regime_refresh = _main.run_penny_regime_refresh
    run_penny_scanner_once = _main.run_penny_scanner_once
    run_penny_connors_scan = _main.run_penny_connors_scan
    run_penny_eod_check = _main.run_penny_eod_check
    run_penny_force_close_mis = _main.run_penny_force_close_mis
    run_penny_hourly_report = _main.run_penny_hourly_report
    _run_penny_daily_attribution = _main._run_penny_daily_attribution
    _run_penny_heatmap = _main._run_penny_heatmap
    _run_penny_eod_digest = _main._run_penny_eod_digest
    """
    [PENNY-MAIN 2026-06-21] Register all 7 penny subsystem scheduler jobs
    on the given scheduler instance. Extracted from the FastAPI
    lifespan() so the test suite (which does not boot the lifespan)
    can verify registration by calling this directly with
    `main.scheduler`.
    """
    # [BOOTSTRAP-2026-07-17] The 08:00 slot no longer calls the universe
    # refresh directly -- it goes through the daily_bootstrap registry,
    # which (a) checks a token issued TODAY is armed before running,
    # (b) sends ONE Telegram reminder and defers when it is not, and
    # (c) re-runs automatically on login + via a 10-min safety tick.
    # On 2026-07-17 the direct cron ran token-blind at 08:00, failed, and
    # the whole day scanned yesterday's universe with the corp gates
    # degraded. run_penny_universe_refresh itself is unchanged and remains
    # the task runner underneath (and the manual/ops entry point).
    from daily_bootstrap import (
        bootstrap_0800_job, bootstrap_safety_tick, premarket_login_nudge,
    )

    scheduler.add_job(
        bootstrap_0800_job, "cron",
        hour=settings.PENNY_REFRESH_HOUR, minute=0,
        id="daily_bootstrap_0800",
        max_instances=1, coalesce=True, misfire_grace_time=600,
    )
    # [LOGIN-NUDGE 2026-07-26] 10 minutes ahead of the bootstrap cron, so the
    # operator is reminded BEFORE the window is missed rather than after. Silent
    # when the token is already fresh. Deliberately not misfire-graced: a nudge
    # delivered late is worse than no nudge (it would arrive after the 08:00
    # deferral alert has already said the same thing, louder).
    scheduler.add_job(
        premarket_login_nudge, "cron",
        hour=settings.PENNY_REFRESH_HOUR - 1, minute=50,
        id="premarket_login_nudge",
        max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        bootstrap_safety_tick, "interval",
        seconds=600,
        id="daily_bootstrap_tick",
        max_instances=1, coalesce=True,
    )

    # [HALT 2026-08-05] Make the circuit breakers bite.
    #
    # check_circuit_breakers has returned a correct verdict since the system was
    # built and NOTHING ever read it on an entry path -- the candour is already
    # in the tree at partner_orchestrator.py:533. This job closes that loop: a
    # breached breaker trips the filesystem sentinel, and every order path
    # checks the sentinel before an entry.
    #
    # 120s, not 600s: the breakers exist to stop a bleeding day, and a
    # ten-minute blind spot is long enough for the momentum book to open
    # another position after the daily-loss limit is already gone. The check is
    # three cheap indexed reads against cache.db.
    async def _enforce_circuit_breakers_safe():
        # [CALENDAR-GATE 2026-07-03] Documented exception: NO is_trading_day
        # gate, deliberately. This handler places no orders and fetches no
        # market data -- it reads the ledger and may set a kill switch. Gating
        # it would mean a Friday-evening breach (a late fill reconciled after
        # 15:30, or a close recorded by hand over the weekend) does not engage
        # until Monday's first tick, which is the one moment the protection has
        # to already be in place. Cost off-hours is three indexed reads on a
        # 120s timer against a ledger that is not changing.
        if not settings.HALT_AUTO_TRIP_ON_CIRCUIT_BREAKER:
            return
        try:
            from performance import enforce_circuit_breakers
            halted, reasons, newly = await enforce_circuit_breakers(settings.DB_PATH)
            if newly:
                # Page once, per scope, on the transition only.
                # [HALT-SCOPE 2026-08-05] Name the scope. The old text said
                # "blocked in every book", which is now wrong and was the kind
                # of wrong that makes an operator stop looking for other trades.
                scopes = ", ".join(newly)
                blocked = (
                    "New entries are blocked in EVERY book."
                    if newly == ["global"] else
                    f"New entries are blocked in: {scopes}. "
                    "Other books are unaffected -- the breakers only measure "
                    "swing + momentum P&L."
                )
                resume = " ".join(f"/resume {s}" for s in newly)
                from operator_alert import notify_operator
                await notify_operator(
                    f"TRADING HALTED ({scopes}) — circuit breaker\n\n"
                    f"reasons: {', '.join(reasons)}\n\n"
                    f"{blocked} Exits (stops, unwinds, square-off) still work "
                    "normally.\n\n"
                    f"Review, then clear with: {resume}",
                    event="circuit_breaker_halt",
                )
        except Exception as exc:
            logger.error("circuit_breaker_enforce_failed err=%s", exc, exc_info=True)

    scheduler.add_job(
        _enforce_circuit_breakers_safe, "interval",
        seconds=120,
        id="circuit_breaker_enforce",
        max_instances=1, coalesce=True, misfire_grace_time=60,
    )
    # [PENNY-PREMARKET 2026-06-24] Pre-market Telegram digest -- fires
    # once per weekday at PENNY_PREMARKET_REPORT_HOUR:PENNY_PREMARKET_REPORT_MIN
    # IST (default 07:50). Reads the universe JSON, lists size + top-N,
    # delivers via Telegram -> webhook fallback. Gives the operator a
    # "universe is N today" signal BEFORE market opens so silence at
    # 10:30 / 11:30 / 12:30 isn't ambiguous (was the universe empty, or
    # did the strategies reject?). Setting hour=0 disables the job.
    if settings.PENNY_PREMARKET_REPORT_HOUR > 0:
        async def _run_penny_premarket_report():
            # [CALENDAR-GATE 2026-07-03] P1 follow-up to PR-1. Skip on
            # weekends + NSE holidays. A Saturday premarket digest
            # would just re-publish Friday's data.
            today = datetime.now(IST).date()
            if not await _main.is_trading_day(today, settings.DB_PATH):
                logger.info("penny_premarket_report_skip reason=non_trading_day")
                return
            from penny_premarket_report import run_premarket_report
            try:
                await run_premarket_report()
            except Exception as e:
                logger.error("penny_premarket_report_failed", error=str(e))
        scheduler.add_job(
            _run_penny_premarket_report, "cron",
            hour=settings.PENNY_PREMARKET_REPORT_HOUR,
            minute=settings.PENNY_PREMARKET_REPORT_MIN,
            id="penny_premarket_report",
        )
    scheduler.add_job(
        run_penny_regime_compute, "cron",
        hour=9, minute=20,
        id="penny_regime_compute",
    )
    scheduler.add_job(
        run_penny_regime_refresh, "cron",
        hour=13, minute=0,
        id="penny_regime_refresh",
    )
    scheduler.add_job(
        run_penny_scanner_once, "interval",
        seconds=settings.PENNY_SCAN_INTERVAL_SEC,
        id="penny_scan_interval",
        # [BUG-FIX 2026-07-01] Single-instance + coalesce.
        # Today the scanner hung on Kite calls and apscheduler refused
        # to launch any overlapping jobs (including penny_edge_scan),
        # deadlocking the entire 09:30 IST cron for the rest of
        # the day. coalesce=True means missed-trigger slots
        # collapse into one rather than pile up. Combined with
        # the asyncio.wait_for in run_penny_scanner_once, this
        # breaks the deadlock.
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_penny_connors_scan, "cron",
        hour=9, minute=30,
        id="penny_connors_scan",
    )
    # [PENNY-EDGE 2026-07-01] Adaptive MR+MO signal scanner.
    # TWO legs run side-by-side each morning:
    #   - PAPER leg: bankroll = PENNY_EDGE_PAPER_BANKROLL (default Rs 100,000)
    #   - LIVE leg:  bankroll = PENNY_EDGE_LIVE_BANKROLL (default Rs 1,000)
    # Both share the same signal engine but each sizes positions
    # against its own bankroll. They are tracked separately in the
    # positions table (source='EDGE_PAPER' vs 'EDGE_LIVE') so they
    # don't double up or see each other's idempotency rows.
    # Fires at 09:30 IST daily, in parallel with the connors scan.
    async def _run_penny_edge_scan_safe():
        import httpx as _httpx
        from penny_edge_orchestrator import (
            run_penny_edge_scan,
            format_telegram,
        )
        # [PENNY-EDGE-BREADCRUMB 2026-07-06] First-line diagnostic log.
        # Today's incident: penny_edge_scan was registered at 07:09 IST
        # but NEVER FIRED at 09:30 IST. Without this breadcrumb there is
        # no way to distinguish "scheduler skipped the trigger" from
        # "handler ran and returned no candidates" from "handler raised
        # silently". Rule 49 (trading-sentinel-ops): every wall-clock
        # cron needs a startup-catchup + first-line breadcrumb so the
        # absence of any penny_edge_scan log lines at 09:30 is debuggable
        # in 30 seconds instead of needing a full re-deploy.
        logger.info(
            "penny_edge_scan_invoked now_ist=%s source=cron_or_catchup",
            datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
        )
        # [CALENDAR-GATE 2026-07-03] weekend / NSE-holiday early-return.
        # No orders are placed here (Telegram notify only), but the gate
        # keeps the cron pattern consistent with _run_penny_edge_exit_safe
        # so the guard test sees one rule for every edge closure.
        today = datetime.now(IST).date()
        if not await _main.is_trading_day(today, settings.DB_PATH):
            logger.info("penny_edge_scan_skip reason=non_trading_day")
            return
        # [FIX-PHASE3-AUDIT 2026-07-09] No-token guard -- same rationale
        # as run_penny_scanner_once (2026-07-09 missed-login incident).
        if not _main.kite.access_token:
            logger.warning("penny_edge_scan_skip reason=no_access_token")
            return
        try:
            summary = await run_penny_edge_scan(_main.kite)
            try:
                msg = format_telegram(summary, header="Penny Edge scan")
                async with _httpx.AsyncClient() as _client:
                    await _client.post(
                        f"{settings.CONTAINER_A_URL}/api/internal/notify",
                        json={"message": msg},
                        headers={"X-Internal-Secret": settings.INTERNAL_API_SECRET or ""},
                        timeout=5.0,
                    )
            except Exception as notify_exc:
                logger.warning("penny_edge_notify_failed err=%s", notify_exc)
        except Exception as exc:
            logger.error("penny_edge_scan_failed err=%s", exc, exc_info=True)

    # [PENNY-EDGE-CRON-GUARD 2026-07-01] Single-instance + coalesce.
    # Rule 39 (trading-sentinel-ops): any cron sharing the scheduler
    # with the legacy penny_scanner_once (which is also fixed with
    # max_instances=1+coalesce=True today) MUST use the same
    # discipline -- otherwise a hung tick of EITHER subsystem can
    # deadlock the other. The default max_instances=1 is fine, but
    # we make it explicit and add coalesce=True so missed triggers
    # collapse instead of pile up. Same loud-but-non-blocking pattern
    # as the live-trading-audit-fix-pattern skill.
    #
    # [PENNY-EDGE-MISFIRE-GUARD 2026-07-06] Job-level
    # misfire_grace_time=600 (10 minutes). The scheduler's
    # job_defaults sets it globally, but defending-in-depth at the
    # job level protects against future scheduler-default regressions
    # and documents the intent at the registration site. If the 09:30
    # IST cron trigger fires while a 30s penny_scan_interval tick is
    # mid-flight, APScheduler marks the trigger as pending; without
    # this grace window APScheduler's default of 1s drops the trigger
    # forever and the daily scan is silently missed. 600s gives the
    # longest realistic scan timeout (90s) ample headroom.
    scheduler.add_job(
        _run_penny_edge_scan_safe, "cron",
        hour=9, minute=30,
        id="penny_edge_scan",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    logger.info(
        "penny_edge_cron_registered id=penny_edge_scan "
        "schedule=\"09:30 IST daily\" max_instances=1 coalesce=True "
        "misfire_grace_time=600"
    )

    # [PENNY-EDGE-STARTUP-CATCHUP 2026-07-02] If the container starts
    # after the cron fire-time (today's incident: 14:53 IST startup,
    # 09:30 IST cron already missed -> zero signals all day), fire
    # the scan ONCE on startup. Same for the 15:15 exit if the
    # container is alive past market close.
    #
    # Coalesce=True means the missed 09:30 trigger gets dropped when
    # the scheduler next evaluates, so without this catchup the cron
    # is silently never fired for that day. The catchup is one-shot
    # per startup, fires immediately (no delay), and the normal cron
    # still owns subsequent days.
    try:
        from datetime import datetime, time as _dt_time, timezone as _dt_tz
        from zoneinfo import ZoneInfo
        _now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
        _today_0930 = _now_ist.replace(hour=9, minute=30, second=0, microsecond=0)
        _today_1515 = _now_ist.replace(hour=15, minute=15, second=0, microsecond=0)
        if _now_ist >= _today_0930 and _now_ist < _today_1515:
            logger.warning(
                "penny_edge_scan_startup_catchup_firing "
                "reason=container_started_after_0930_IST "
                "now_ist=%s",
                _now_ist.strftime("%H:%M:%S"),
            )
            # Resolve the loop before constructing the coroutine. Registration
            # is also exercised from synchronous tests/tooling; constructing
            # first and then failing ``create_task`` leaked an unawaited
            # coroutine even though the outer startup guard caught the error.
            try:
                _startup_loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.warning(
                    "penny_edge_scan_startup_catchup_deferred "
                    "reason=no_running_event_loop"
                )
            else:
                _startup_loop.create_task(_run_penny_edge_scan_safe())
        elif _now_ist >= _today_1515:
            logger.warning(
                "penny_edge_scan_startup_skipped "
                "reason=container_started_after_market_close "
                "now_ist=%s -- exit catchup will fire instead",
                _now_ist.strftime("%H:%M:%S"),
            )
        else:
            logger.info(
                "penny_edge_scan_startup_skipped reason=before_0930_IST "
                "now_ist=%s -- normal cron will fire on schedule",
                _now_ist.strftime("%H:%M:%S"),
            )
    except Exception as _catchup_exc:
        # Loud-but-non-blocking: never fail startup over the catchup.
        logger.warning(
            "penny_edge_scan_startup_catchup_failed err=%s",
            str(_catchup_exc),
        )

    # [PENNY-EDGE 2026-07-01] EOD exit job: force-close any EDGE-sourced
    # positions (both PAPER and LIVE legs) older than 3 days.
    async def _run_penny_edge_exit_safe():
        import httpx as _httpx
        from penny_edge_orchestrator import (
            run_penny_edge_exit,
            format_exit_telegram,
        )
        # [PENNY-EDGE-BREADCRUMB 2026-07-06] First-line diagnostic log.
        # Same rationale as the breadcrumb in _run_penny_edge_scan_safe.
        # The 15:15 IST EOD exit was also silently not firing today --
        # without this log line, a missed 15:15 fire looks identical
        # to "no EDGE positions held, no work to do" in the logs.
        logger.info(
            "penny_edge_exit_invoked now_ist=%s source=cron_or_catchup",
            datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
        )
        # [CALENDAR-GATE 2026-07-03] weekend / NSE-holiday early-return.
        # REAL FINANCIAL RISK: this closure calls run_penny_edge_exit which
        # places exit orders via _main.kite. Firing on a non-trading day would
        # exit positions at stale weekend prices for any EDGE positions
        # held across a weekend (rare but possible). Mirrors
        # run_penny_force_close_mis and auto_square_momentum gates.
        today = datetime.now(IST).date()
        if not await _main.is_trading_day(today, settings.DB_PATH):
            logger.info("penny_edge_exit_skip reason=non_trading_day")
            return
        try:
            summary = await run_penny_edge_exit(_main.kite)
            try:
                msg = format_exit_telegram(summary)
                if len(summary.get("closed_paper", [])) + len(summary.get("closed_live", [])) > 0:
                    async with _httpx.AsyncClient() as _client:
                        await _client.post(
                            f"{settings.CONTAINER_A_URL}/api/internal/notify",
                            json={"message": msg},
                            headers={"X-Internal-Secret": settings.INTERNAL_API_SECRET or ""},
                            timeout=5.0,
                        )
            except Exception as notify_exc:
                logger.warning("penny_edge_exit_notify_failed err=%s", notify_exc)
        except Exception as exc:
            logger.error("penny_edge_exit_failed err=%s", exc, exc_info=True)

    # [PENNY-EDGE-CRON-GUARD 2026-07-01] max_instances=1 + coalesce=True.
    # Same rationale as the penny_edge_scan cron guard above: the
    # 15:15 IST EOD exit must not be allowed to deadlock the
    # scheduler if the _main.kite-stub interaction stalls.
    #
    # [PENNY-EDGE-MISFIRE-GUARD 2026-07-06] misfire_grace_time=600.
    # See the rationale on the penny_edge_scan registration above;
    # 15:15 IST exit must also be protected from the same silent
    # drop. REAL FINANCIAL RISK: a missed 15:15 exit leaves EDGE
    # positions open overnight (the 3-day age rule doesn't fire).
    scheduler.add_job(
        _run_penny_edge_exit_safe, "cron",
        hour=15, minute=15,
        id="penny_edge_exit",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    logger.info(
        "penny_edge_cron_registered id=penny_edge_exit "
        "schedule=\"15:15 IST daily\" max_instances=1 coalesce=True "
        "misfire_grace_time=600"
    )

    # [PENNY-EDGE-STARTUP-CATCHUP 2026-07-02] Companion catchup for the
    # 15:15 IST EOD exit. Fires only if the container was offline at
    # 15:15 IST AND it's now after-market. The startup_scan catchup
    # above already fires the scan if needed; this one only handles
    # exit-time catchup (e.g. container restart right around 15:15).
    try:
        from datetime import datetime as _dt2, timezone as _dt2_tz
        from zoneinfo import ZoneInfo
        _now_ist2 = _dt2.now(ZoneInfo("Asia/Kolkata"))
        _today_1515_b = _now_ist2.replace(hour=15, minute=15, second=0, microsecond=0)
        if _now_ist2 >= _today_1515_b:
            logger.warning(
                "penny_edge_exit_startup_catchup_firing "
                "reason=container_started_after_1515_IST now_ist=%s",
                _now_ist2.strftime("%H:%M:%S"),
            )
            # Resolve the loop before constructing the coroutine.  If scheduler
            # registration is invoked from a synchronous diagnostic/test path,
            # get_running_loop() fails cleanly without leaking an un-awaited
            # coroutine object.
            asyncio.get_running_loop().create_task(_run_penny_edge_exit_safe())
        else:
            logger.info(
                "penny_edge_exit_startup_skipped reason=before_1515_IST "
                "now_ist=%s -- normal cron will fire on schedule",
                _now_ist2.strftime("%H:%M:%S"),
            )
    except Exception as _exit_catchup_exc:
        logger.warning(
            "penny_edge_exit_startup_catchup_failed err=%s",
            str(_exit_catchup_exc),
        )

    scheduler.add_job(
        run_penny_eod_check, "cron",
        hour=settings.PENNY_MIS_SMART_EOD_TIME // 60,
        minute=settings.PENNY_MIS_SMART_EOD_TIME % 60,
        id="penny_eod_check",
    )
    # [PENNY-G5 2026-06-25] 15:00 IST force-exit of all open MIS positions.
    # Was silently missing before this commit -- mis_time_stop_active()
    # was defined but never invoked. This scheduler entry fires once at
    # 15:00 IST; the job itself is a no-op if the window has already passed.
    scheduler.add_job(
        run_penny_force_close_mis, "cron",
        hour=settings.PENNY_BREAKOUT_TIME_EXIT // 60,
        minute=settings.PENNY_BREAKOUT_TIME_EXIT % 60,
        id="penny_force_close_mis",
    )
    # [TIER3-DAILY-ATTRIBUTION 2026-06-25] 15:30 IST daily P&L attribution.
    # Fires 30 min after the 15:00 force-close so all MIS positions
    # have been closed and the bankroll_ledger has the day's trades.
    scheduler.add_job(
        _run_penny_daily_attribution, "cron",
        hour=settings.PENNY_DAILY_ATTRIBUTION_HOUR,
        minute=settings.PENNY_DAILY_ATTRIBUTION_MIN,
        id="penny_daily_attribution",
    )
    # [TIER3-POSITION-HEATMAP 2026-06-25] Mid-day position heat-map.
    # Fires every 15 minutes from 10:00 to 14:45 IST (5 min before
    # the smart-EOD at 14:30, so the operator sees the EOD-relevant
    # state). Out-of-hours the job is a no-op (build_heatmap returns
    # the "0 open positions" body when nothing is open).
    scheduler.add_job(
        _run_penny_heatmap, "interval", minutes=15,
        id="penny_heatmap",
    )
    # [PHASE-C-EOD-DIGEST 2026-06-25] 16:00 IST end-of-day digest.
    # Fires after the 15:30 daily attribution (T3-A) and the 15:00
    # force-close (G5). At 16:00 all MIS positions are closed, CNC
    # positions are held overnight, and the day's trades are in the
    # [PENNY-HOURLY-BREADCRUMB 2026-09-13] First-line diagnostic log
    # inside the hourly report. Rule 49 (trading-sentinel-ops): every
    # wall-clock cron needs a startup-catchup + first-line breadcrumb
    # so the absence of any penny_hourly_report log lines at 10:00,
    # 11:00, ..., 14:00 IST is debuggable in 30 seconds instead of
    # needing a full re-deploy. PROD-RISK: a missing first-line log
    # at any of these hours means the cron silently did NOT fire;
    # without a breadcrumb that is indistinguishable from "fired
    # and reported nothing".
    #
    # [CALENDAR-GATE 2026-09-13] The wrapper carries its own
    # is_trading_day gate so the test
    # test_every_cron_handler_has_a_gate_or_is_allowlisted (in
    # tests/test_penny_cron_gating.py) accepts this new cron.
    # Without the gate here the inner function would still gate,
    # but the rule is "every scheduler-registered handler must
    # gate at its top" -- the wrapper IS the handler from the
    # scheduler's point of view.
    #
    # [PROD-DEFENSE 2026-09-13] The calendar gate is INSIDE the
    # try/except so a substrate failure in ``is_trading_day`` is
    # caught and logged, NOT propagated. The whole body of the
    # wrapper is the protected region: breadcrumb -> gate -> run.
    # Anything that raises in any of those three steps is caught
    # and logged with exc_info=True. This is the only penny
    # subsystem handler whose failure mode would silently kill
    # the cron (it is the only one without a _safe wrapper prior
    # to H1), so the wrapper's defence is comprehensive.
    async def run_penny_hourly_report_safe():
        logger.info(
            "penny_hourly_report_invoked now_ist=%s source=cron_or_catchup",
            datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
        )
        try:
            today = datetime.now(IST).date()
            if not await _main.is_trading_day(today, settings.DB_PATH):
                logger.info("penny_hourly_report_skip reason=non_trading_day")
                return
            await run_penny_hourly_report()
        except Exception as exc:
            # [PROD-DEFENSE 2026-09-13] The previous behaviour was to
            # let any NameError/AttributeError/KeyError propagate out
            # of the cron (the bare ``run_penny_hourly_report`` was
            # the only penny subsystem job registered without a
            # ``_safe`` wrapper). With the F3 wiring that pulls in
            # ``mark_to_market.mark_open_positions`` and the F-series
            # substrates, a transient substrate read failure would
            # take the whole hourly-report cron down for the day.
            # This wrapper is a defensive belt-and-braces that
            # matches the discipline already applied to every other
            # penny subsystem job.
            logger.error(
                "penny_hourly_report_failed err=%s", exc, exc_info=True,
            )

    # bankroll_ledger. Body builder lives in operator_status.py.
    scheduler.add_job(
        _run_penny_eod_digest, "cron",
        hour=16, minute=0,
        id="penny_eod_digest",
    )
    scheduler.add_job(
        # [PROD-DEFENSE 2026-09-13] Re-registered to use the
        # ``_safe`` wrapper instead of the bare
        # ``run_penny_hourly_report``. The cron contract is
        # unchanged from the scheduler's point of view; the wrapper
        # only catches failures so a transient substrate error
        # does not silently kill the hourly report.
        run_penny_hourly_report_safe, "cron",
        hour=(
            f"{settings.PENNY_HOURLY_REPORT_START_HOUR}-"
            f"{settings.PENNY_HOURLY_REPORT_END_HOUR}"
        ),
        minute=0,
        id="penny_hourly_report",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    logger.info(
        "penny_hourly_report_cron_registered id=penny_hourly_report "
        "schedule=\"%s:00 IST\" max_instances=1 coalesce=True "
        "misfire_grace_time=600",
        f"{settings.PENNY_HOURLY_REPORT_START_HOUR}-"
        f"{settings.PENNY_HOURLY_REPORT_END_HOUR}",
    )

    # [GAP-2 ZERO-ACCEPT ALARM 2026-07-10] 15:45 IST accept-rate
    # watchdog, backported from the F&O spec §9.2. Fires after the
    # 15:30 close so the day's rows are final. 215,814 evaluations and
    # 0 accepts (BUG-1) produced no alert of any kind for nine months;
    # this job would have caught it on day
    # PENNY_ZERO_ACCEPT_ALERT_DAYS (default 2). Read-only over
    # penny_signals -- needs no Kite token, so no no_access_token
    # guard (it must fire ESPECIALLY on token-less days).
    async def _run_penny_accept_watchdog_safe():
        import httpx as _httpx
        from penny_accept_watchdog import (
            zero_accept_scan,
            format_zero_accept_alert,
        )
        # [Rule 55] First-line breadcrumb.
        logger.info(
            "penny_accept_watchdog_invoked now_ist=%s",
            datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
        )
        today = datetime.now(IST).date()
        if not await _main.is_trading_day(today, settings.DB_PATH):
            logger.info("penny_accept_watchdog_skip reason=non_trading_day")
            return
        # [TIER0-0.5 2026-07-14] Scan EACH LEG SEPARATELY.
        #
        # This used to call zero_accept_scan() with no `leg`, i.e. across the whole
        # penny book at once. That is a masking hazard: one healthy leg's accepts
        # satisfy the "any accept in the window?" check and the alarm goes quiet --
        # while another leg sits at zero accepts indefinitely. Exactly the shape we
        # are digging out of: the MIS breakout leg (0 accepts / 349,297 evals) and
        # the CNC Connors leg (0 accepts / 240 evals) have BOTH never traded, and
        # the EDGE leg -- the only one placing live orders -- wrote no rows at all.
        #
        # A per-leg scan means a dead leg cannot hide behind a live one.
        for _leg in ("MIS", "CNC", "EDGE"):
            try:
                payload = await zero_accept_scan(
                    settings.DB_PATH,
                    n_days=settings.PENNY_ZERO_ACCEPT_ALERT_DAYS,
                    leg=_leg,
                )
                if payload is None:
                    logger.info(
                        "penny_accept_watchdog_ok leg=%s accepts_in_window=yes_or_insufficient_data",
                        _leg,
                    )
                    continue
                # [Rule 72] Degradation is a WARNING, never an INFO.
                logger.warning(
                    "penny_zero_accept_alarm leg=%s days=%s evaluations=%d "
                    "dead_gate=%s suspect_gate=%s",
                    _leg, ",".join(payload["days"]), payload["evaluations"],
                    payload.get("dead_gate") or "none",
                    payload.get("suspect_gate") or "none",
                )
                try:
                    msg = format_zero_accept_alert(payload)
                    async with _httpx.AsyncClient() as _client:
                        await _client.post(
                            f"{settings.CONTAINER_A_URL}/api/internal/notify",
                            json={"message": msg},
                            headers={"X-Internal-Secret": settings.INTERNAL_API_SECRET or ""},
                            timeout=5.0,
                        )
                except Exception as notify_exc:
                    logger.warning(
                        "penny_accept_watchdog_notify_failed leg=%s err=%s", _leg, notify_exc,
                    )
            except Exception as exc:
                logger.error(
                    "penny_accept_watchdog_failed leg=%s err=%s", _leg, exc, exc_info=True,
                )

    scheduler.add_job(
        _run_penny_accept_watchdog_safe, "cron",
        hour=15, minute=45,
        id="penny_accept_watchdog",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    logger.info(
        "penny_accept_watchdog_registered id=penny_accept_watchdog "
        "schedule=\"15:45 IST daily\" n_days=%d",
        settings.PENNY_ZERO_ACCEPT_ALERT_DAYS,
    )

    # [Rule 49] Startup catchup: a container started after 15:45 IST
    # still audits the day (a restart evening is exactly when silent
    # zero-accept days sneak past).
    try:
        from zoneinfo import ZoneInfo as _ZI_wd
        _now_ist_wd = datetime.now(_ZI_wd("Asia/Kolkata"))
        _today_1545 = _now_ist_wd.replace(hour=15, minute=45, second=0, microsecond=0)
        if _now_ist_wd >= _today_1545:
            logger.info(
                "penny_accept_watchdog_startup_catchup_firing now_ist=%s",
                _now_ist_wd.strftime("%H:%M:%S"),
            )
            # As above, do not construct a coroutine until a running loop has
            # been established.
            asyncio.get_running_loop().create_task(_run_penny_accept_watchdog_safe())
    except Exception as _wd_catchup_exc:
        logger.warning(
            "penny_accept_watchdog_startup_catchup_failed err=%s",
            str(_wd_catchup_exc),
        )


def register_partner_scheduler_jobs(scheduler):
    """
    [PARTNER-TIPS 2026-07-18] Partner tips bot jobs. The original five plus
    the hedge review job all fail closed when disabled or missing safe inputs.
    jobs no-op instantly when PARTNER_BOT_ENABLED is false (the enabled
    check is the FIRST line of every job in partner_orchestrator), so
    registration is unconditional like the other subsystems.

    Scheduling is deliberately OFF the quarter-hour grid: momentum and
    penny scans burst the shared Kite limiter at :00/:15/:30/:45, and
    partner calls must never queue ahead of the trading path there.
      - partner_scan_tick:      minute */2 at second 40 (09:45-15:05 self-gate)
      - partner_analytics_tick: minute 2-57/5 (09:20-15:30 self-gate)
      - partner_morning_brief / partner_eod_wrap / partner_rv_refresh: crons.
    """
    import main as _main

    logger = _main.logger
    from scheduler_telemetry import telemetry_job

    async def _run_partner_scan_tick_safe():
        # [CALENDAR-GATE 2026-07-03] gate delegated: partner_orchestrator.
        # _gates_open checks PARTNER_BOT_ENABLED, the session window,
        # is_trading_day AND the access token before any work.
        try:
            from partner_orchestrator import partner_scan_tick
            await partner_scan_tick()
        except Exception as exc:
            logger.error("partner_scan_tick_crashed err=%s", exc, exc_info=True)

    @telemetry_job(settings.DB_PATH, "partner_manual_advisory_tick")
    async def _run_partner_manual_advisory_tick_safe():
        # [CALENDAR-GATE 2026-07-03] delegated to _gates_open in the tick:
        # trading day, session, enabled destination and provider token.
        try:
            from partner_orchestrator import partner_manual_advisory_tick
            await partner_manual_advisory_tick()
        except Exception as exc:
            logger.error("partner_manual_advisory_tick_crashed err=%s", exc, exc_info=True)
            return {"status": "FAILED", "reason": type(exc).__name__}

    @telemetry_job(settings.DB_PATH, "partner_manual_advisory_lifecycle_tick")
    async def _run_partner_manual_advisory_lifecycle_tick_safe():
        # [CALENDAR-GATE 2026-07-03] exception: local off-session retirement
        # must continue. Any transport is separately final-authorized.
        try:
            from partner_orchestrator import partner_manual_advisory_lifecycle_tick
            await partner_manual_advisory_lifecycle_tick()
        except Exception as exc:
            logger.error("partner_manual_advisory_lifecycle_tick_crashed err=%s", exc, exc_info=True)
            return {"status": "FAILED", "reason": type(exc).__name__}

    async def _run_partner_analytics_tick_safe():
        # [CALENDAR-GATE 2026-07-03] gate delegated: partner_orchestrator.
        # _gates_open checks PARTNER_BOT_ENABLED, the session window,
        # is_trading_day AND the access token before any work.
        try:
            from partner_orchestrator import partner_analytics_tick
            await partner_analytics_tick()
        except Exception as exc:
            logger.error("partner_analytics_tick_crashed err=%s", exc, exc_info=True)

    @telemetry_job(settings.DB_PATH, "research_quote_collection")
    async def _run_research_quote_collection_safe():
        # [CALENDAR-GATE 2026-07-03] delegated to the collector tick, which
        # checks trading day/session before provider calls and journals gaps.
        # Independent market-data observation.  It has no partner delivery,
        # profile, qualification, or order dependency; its own entry point
        # handles calendar/session/token availability and records gaps.
        try:
            from research_quote_collector import research_quote_collection_tick
            return await research_quote_collection_tick()
        except Exception as exc:
            logger.error("research_quote_collection_crashed err=%s", exc, exc_info=True)
            return {"status": "FAILED", "reason": type(exc).__name__}

    async def _run_partner_morning_brief_safe():
        # [CALENDAR-GATE 2026-07-03] gate delegated: partner_orchestrator.
        # _gates_open checks PARTNER_BOT_ENABLED, the session window,
        # is_trading_day AND the access token before any work.
        try:
            from partner_orchestrator import partner_morning_brief
            await partner_morning_brief()
        except Exception as exc:
            logger.error("partner_morning_brief_crashed err=%s", exc, exc_info=True)

    async def _run_partner_eod_wrap_safe():
        # [CALENDAR-GATE 2026-07-03] gate delegated: partner_orchestrator.
        # _gates_open checks PARTNER_BOT_ENABLED, the session window,
        # is_trading_day AND the access token before any work.
        try:
            from partner_orchestrator import partner_eod_wrap
            await partner_eod_wrap()
        except Exception as exc:
            logger.error("partner_eod_wrap_crashed err=%s", exc, exc_info=True)

    async def _run_partner_rv_refresh_safe():
        # [CALENDAR-GATE 2026-07-03] gate delegated: partner_orchestrator.
        # _gates_open checks PARTNER_BOT_ENABLED, the session window,
        # is_trading_day AND the access token before any work.
        try:
            from partner_orchestrator import partner_rv_refresh
            await partner_rv_refresh()
        except Exception as exc:
            logger.error("partner_rv_refresh_crashed err=%s", exc, exc_info=True)

    async def _run_partner_hedge_tick_safe():
        # [CALENDAR-GATE 2026-07-03] delegated to partner_hedge_tick, which
        # checks the trading day before any broker or advisory work.
        try:
            from hedge_advisory import partner_hedge_tick
            await partner_hedge_tick()
        except Exception as exc:
            logger.error("partner_hedge_tick_crashed err=%s", exc, exc_info=True)

    @telemetry_job(settings.DB_PATH, "partner_hedge_delivery_recovery")
    async def _run_partner_hedge_delivery_recovery_safe():
        # [CALENDAR-GATE 2026-07-03] exception: sweep/retirement must run
        # off-session. _recovery_retirement_reason and final dispatch gate
        # prevent an expired or non-trading-day proposal from being sent.
        try:
            from hedge_advisory import recover_pending_hedge_deliveries
            await recover_pending_hedge_deliveries()
        except Exception as exc:
            logger.error("partner_hedge_delivery_recovery_crashed err=%s", exc, exc_info=True)
            return {"status": "FAILED", "reason": type(exc).__name__}

    async def _run_partner_input_refresh_safe():
        # [CALENDAR-GATE 2026-07-03] exception: approved source-neutral
        # reconciliation may arrive off-session; this path cannot advise
        # or order. Account, completeness and freshness checks still apply.
        try:
            from partner_input_refresh import refresh_partner_input_once
            await refresh_partner_input_once()
        except Exception as exc:
            logger.error("partner_input_refresh_crashed err=%s", exc, exc_info=True)

    async def _run_partner_hedge_morning_summary_safe():
        # [CALENDAR-GATE 2026-07-03] delegated to daily_summary, which checks
        # is_trading_day before portfolio lookup or transport.
        try:
            from hedge_advisory import partner_hedge_daily_summary
            await partner_hedge_daily_summary("MORNING")
        except Exception as exc:
            logger.error("partner_hedge_morning_summary_crashed err=%s", exc, exc_info=True)

    async def _run_partner_hedge_eod_summary_safe():
        # [CALENDAR-GATE 2026-07-03] delegated to daily_summary's trading-day
        # check before portfolio lookup or transport.
        try:
            from hedge_advisory import partner_hedge_daily_summary
            await partner_hedge_daily_summary("EOD")
        except Exception as exc:
            logger.error("partner_hedge_eod_summary_crashed err=%s", exc, exc_info=True)

    async def _run_partner_hedge_phase2_tick_safe():
        # [CALENDAR-GATE 2026-07-03] delegated to the Phase 2 tick before
        # any broker access or advisory work.
        # Separate cadence and feature gate: premium-selling reviews remain
        # dormant until their live-chain verification switch is enabled.
        try:
            from hedge_advisory import partner_hedge_phase2_tick
            await partner_hedge_phase2_tick()
        except Exception as exc:
            logger.error("partner_hedge_phase2_tick_crashed err=%s", exc, exc_info=True)

    async def _run_partner_hedge_phase3_tick_safe():
        # [CALENDAR-GATE 2026-07-03] delegated to the Phase 3 tick before
        # any calendar, position, broker, or advisory work.
        try:
            from hedge_advisory import partner_hedge_phase3_tick
            await partner_hedge_phase3_tick()
        except Exception as exc:
            logger.error("partner_hedge_phase3_tick_crashed err=%s", exc, exc_info=True)

    async def _run_proactive_shadow_workflow_safe():
        # [CALENDAR-GATE 2026-07-03] exception: disabled-by-default SHADOW
        # research accepts explicit fixtures or a validated completed-bar
        # provider. Off-session research has no order/delivery authority;
        # unavailable/stale provider evidence is recorded, not made a fill.
        try:
            from proactive_intelligence import run_configured_shadow_workflow
            await run_configured_shadow_workflow()
        except Exception as exc:
            logger.error("proactive_shadow_workflow_crashed err=%s", exc, exc_info=True)

    scheduler.add_job(
        _run_partner_scan_tick_safe, "cron",
        minute="*/2", second=40,
        id="partner_scan_tick",
        max_instances=1, coalesce=True, misfire_grace_time=60,
    )
    scheduler.add_job(
        _run_partner_manual_advisory_tick_safe, "cron",
        minute="*/2", second=50,
        id="partner_manual_advisory_tick",
        max_instances=1, coalesce=True, misfire_grace_time=60,
    )
    scheduler.add_job(
        _run_partner_manual_advisory_lifecycle_tick_safe, "cron",
        minute="*/1", second=5,
        id="partner_manual_advisory_lifecycle_tick",
        max_instances=1, coalesce=True, misfire_grace_time=120,
    )
    scheduler.add_job(
        _run_partner_analytics_tick_safe, "cron",
        minute="2-57/5",
        id="partner_analytics_tick",
        max_instances=1, coalesce=True, misfire_grace_time=120,
    )
    scheduler.add_job(
        _run_research_quote_collection_safe, "cron",
        minute="*/1", second=25,
        id="research_quote_collection",
        max_instances=1, coalesce=True, misfire_grace_time=55,
    )
    scheduler.add_job(
        _run_partner_morning_brief_safe, "cron",
        hour=settings.PARTNER_MORNING_BRIEF_HOUR,
        minute=settings.PARTNER_MORNING_BRIEF_MIN,
        id="partner_morning_brief",
        max_instances=1, coalesce=True, misfire_grace_time=600,
    )
    scheduler.add_job(
        _run_partner_eod_wrap_safe, "cron",
        hour=settings.PARTNER_EOD_HOUR, minute=settings.PARTNER_EOD_MIN,
        id="partner_eod_wrap",
        max_instances=1, coalesce=True, misfire_grace_time=600,
    )
    scheduler.add_job(
        _run_partner_rv_refresh_safe, "cron",
        hour=9, minute=10,
        id="partner_rv_refresh",
        max_instances=1, coalesce=True, misfire_grace_time=600,
    )
    scheduler.add_job(
        _run_partner_input_refresh_safe, "cron",
        minute="*/2", second=5,
        id="partner_input_refresh",
        max_instances=1, coalesce=True, misfire_grace_time=60,
    )
    scheduler.add_job(
        _run_partner_hedge_tick_safe, "cron",
        minute="7-52/15", second=10,
        id="partner_hedge_tick",
        max_instances=1, coalesce=True, misfire_grace_time=120,
    )
    scheduler.add_job(
        _run_partner_hedge_delivery_recovery_safe, "cron",
        minute="*/2", second=35,
        id="partner_hedge_delivery_recovery",
        max_instances=1, coalesce=True, misfire_grace_time=60,
    )
    scheduler.add_job(
        _run_partner_hedge_morning_summary_safe, "cron",
        hour=settings.PARTNER_MORNING_BRIEF_HOUR,
        minute=settings.PARTNER_MORNING_BRIEF_MIN,
        id="partner_hedge_morning_summary",
        max_instances=1, coalesce=True, misfire_grace_time=600,
    )
    scheduler.add_job(
        _run_partner_hedge_eod_summary_safe, "cron",
        hour=settings.PARTNER_EOD_HOUR, minute=settings.PARTNER_EOD_MIN,
        id="partner_hedge_eod_summary",
        max_instances=1, coalesce=True, misfire_grace_time=600,
    )
    scheduler.add_job(
        _run_partner_hedge_phase2_tick_safe, "cron",
        hour="9-15", minute="3,33", second=20,
        id="partner_hedge_phase2_tick",
        max_instances=1, coalesce=True, misfire_grace_time=120,
    )
    scheduler.add_job(
        _run_partner_hedge_phase3_tick_safe, "cron",
        hour="9-15", minute="11,41", second=50,
        id="partner_hedge_phase3_tick",
        max_instances=1, coalesce=True, misfire_grace_time=120,
    )
    scheduler.add_job(
        _run_proactive_shadow_workflow_safe, "cron",
        minute="*/5", second=55,
        id="proactive_shadow_workflow",
        max_instances=1, coalesce=True, misfire_grace_time=60,
    )
    logger.info(
        "partner_cron_registered jobs=15 enabled=%s hedge_enabled=%s "
        "hedge_phase2_enabled=%s hedge_phase3_enabled=%s shadow_enabled=%s off_grid=true",
        settings.PARTNER_BOT_ENABLED,
        settings.PARTNER_HEDGE_ENABLED,
        settings.PARTNER_HEDGE_PHASE2_ENABLED,
        settings.PARTNER_HEDGE_PHASE3_ENABLED,
        settings.PROACTIVE_SHADOW_ENABLED,
    )


# ---------------------------------------------------------------------------
# [SMIT-FYERS-OPTIONS 2026-09-25] TradingView -> Fyers options pipeline.
# ---------------------------------------------------------------------------

def register_tv_fyers_scheduler_jobs(scheduler):
    """Register the exit-management polling job, plus the autonomous
    AI candle-scan entry job [ADDED 2026-09-25].

    tv_fyers_exit_tick: TradingView-triggered entries are event-driven
    (one webhook = one handle_tv_entry_signal call, dispatched directly
    from routes_tv_fyers.py, not from a scheduler tick), but exits need
    continuous price monitoring regardless of webhook timing, so this
    runs on its own interval -- mirrors fno_tick's registration shape
    (telemetry_job, market-hours self-gate, token guard) but against
    `_main.fyers` instead of `_main.kite`.

    tv_ai_scan_tick: the autonomous entry path -- no TradingView alert at
    all. Claude reads recent candles on this interval and decides the
    whole trade itself; see tv_fyers_orchestrator.handle_ai_candle_scan
    and tv_fyers_ai_gate.py's "FULL PLANNER" docstring section.
    """
    import main as _main
    from scheduler_telemetry import telemetry_job

    IST = _main.IST
    logger = _main.logger

    @telemetry_job(settings.DB_PATH, "tv_fyers_exit_tick")
    async def _run_tv_fyers_exit_tick_safe():
        from tv_fyers_orchestrator import run_tv_fyers_exit_tick
        now_ist = datetime.now(IST)
        nm = now_ist.hour * 60 + now_ist.minute
        # Same session-hours gate as fno_tick: 09:15-15:25 covers the
        # 15:10 hard-flat exit too.
        if not (9 * 60 + 15 <= nm <= 15 * 60 + 25):
            return
        # [CALENDAR-GATE 2026-07-03] Explicit trading-day check, same
        # convention as fno_tick -- required by
        # tests/test_penny_cron_gating.py's cross-handler audit so a
        # weekend/holiday misfire can never reach the exit-management path.
        today = now_ist.date()
        if not await _main.is_trading_day(today, settings.DB_PATH):
            return
        if settings.TV_FYERS_DISABLE_PAPER:
            return
        if not _main.fyers.access_token:
            # Expected until a Fyers login flow exists and real
            # credentials are armed -- not an error, just nothing to do.
            return
        started = monotonic()
        outcome = "ok"
        exits = []
        try:
            result = await run_tv_fyers_exit_tick(_main.fyers, settings.DB_PATH, now_ist)
            exits = result.get("exits") or []
        except Exception as exc:
            outcome = "failed"
            logger.error("tv_fyers_exit_tick_failed err=%s", exc, exc_info=True)
        finally:
            elapsed = monotonic() - started
            logger.info(
                "tv_fyers_exit_tick_complete outcome=%s elapsed_sec=%.3f exits=%d",
                outcome, elapsed, len(exits),
            )
        return {
            "status": "FAILED" if outcome == "failed" else "COMPLETED",
            "reason": "tv_fyers_exit_tick_exception" if outcome == "failed" else "none",
        }

    scheduler.add_job(
        _run_tv_fyers_exit_tick_safe, "interval",
        seconds=settings.TV_FYERS_SCAN_INTERVAL_SEC,
        id="tv_fyers_exit_tick",
        max_instances=1, coalesce=True, misfire_grace_time=120,
    )
    logger.info(
        "tv_fyers_cron_registered id=tv_fyers_exit_tick interval=%ds max_instances=1 coalesce=True",
        settings.TV_FYERS_SCAN_INTERVAL_SEC,
    )

    # [AI-SCAN 2026-09-25] Autonomous candle-scan entry path -- no
    # TradingView alert involved. Same registration shape as the exit
    # tick above (telemetry_job, market-hours self-gate, calendar gate,
    # token guard), against tv_fyers_orchestrator.handle_ai_candle_scan.
    @telemetry_job(settings.DB_PATH, "tv_ai_scan_tick")
    async def _run_tv_ai_scan_tick_safe():
        from tv_fyers_orchestrator import handle_ai_candle_scan
        now_ist = datetime.now(IST)
        nm = now_ist.hour * 60 + now_ist.minute
        if not (9 * 60 + 15 <= nm <= 15 * 60 + 25):
            return
        today = now_ist.date()
        if not await _main.is_trading_day(today, settings.DB_PATH):
            return
        if settings.TV_FYERS_DISABLE_PAPER:
            return
        if not _main.fyers.access_token:
            return
        started = monotonic()
        outcome = "ok"
        try:
            result = await handle_ai_candle_scan(_main.fyers, settings.DB_PATH, now_ist)
        except Exception as exc:
            outcome = "failed"
            result = {}
            logger.error("tv_ai_scan_tick_failed err=%s", exc, exc_info=True)
        finally:
            elapsed = monotonic() - started
            logger.info(
                "tv_ai_scan_tick_complete outcome=%s elapsed_sec=%.3f executed=%s reason=%s",
                outcome, elapsed, result.get("executed"), result.get("reason"),
            )
        return {
            "status": "FAILED" if outcome == "failed" else "COMPLETED",
            "reason": "tv_ai_scan_tick_exception" if outcome == "failed" else "none",
        }

    scheduler.add_job(
        _run_tv_ai_scan_tick_safe, "interval",
        seconds=settings.TV_FYERS_AI_SCAN_INTERVAL_SEC,
        id="tv_ai_scan_tick",
        max_instances=1, coalesce=True, misfire_grace_time=120,
    )
    logger.info(
        "tv_fyers_cron_registered id=tv_ai_scan_tick interval=%ds max_instances=1 coalesce=True",
        settings.TV_FYERS_AI_SCAN_INTERVAL_SEC,
    )

    # [LOGIN-NUDGE 2026-09-28] Mirrors daily_bootstrap.py's
    # premarket_login_nudge (same rationale: the engine starts every
    # session token-blind, and the only prior signal was silence until
    # someone happened to check the dashboard). Fyers has no equivalent
    # of the penny module's ~55-min universe refresh -- there is nothing
    # to "finish in time" here, just a token that needs to exist before
    # 09:15 -- so a flat 25-minute lead at 08:50 is generous rather than
    # timed against a downstream job's own runtime. No-ops silently when
    # the token is already armed, so a punctual operator hears nothing.
    @telemetry_job(settings.DB_PATH, "tv_fyers_premarket_login_nudge")
    async def _tv_fyers_premarket_login_nudge():
        now_ist = datetime.now(IST)
        if not await _main.is_trading_day(now_ist.date(), settings.DB_PATH):
            return
        if settings.TV_FYERS_DISABLE_PAPER:
            return
        if _main.fyers.access_token:
            logger.info("tv_fyers_premarket_login_nudge_skipped reason=token_already_armed")
            return
        from operator_alert import notify_operator
        await notify_operator(
            "🔑 TV->Fyers: no Fyers token armed yet and the market opens in "
            "25 minutes. Log in at /api/tv-fyers/auth/login before 09:15, or "
            "neither entry path (TradingView webhook or the AI scanner) can "
            "do anything today.",
            event="tv_fyers_premarket_login_nudge",
        )

    scheduler.add_job(
        _tv_fyers_premarket_login_nudge, "cron",
        hour=8, minute=50,
        id="tv_fyers_premarket_login_nudge",
        max_instances=1, coalesce=True, misfire_grace_time=600,
    )
    logger.info("tv_fyers_cron_registered id=tv_fyers_premarket_login_nudge trigger=cron(08:50) max_instances=1")
