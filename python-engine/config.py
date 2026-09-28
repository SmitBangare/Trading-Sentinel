from pathlib import Path
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import ClassVar

# [2026-07-19] Resolve env files by ABSOLUTE path from this module's location,
# not a CWD-relative ".env". Order is low->high precedence: the local
# python-engine/.env first, then the repo-root .env overriding it.
#   Container (config.py at /app/config.py): loads /app/.env (the operator
#     creds baked into the image); the repo-root "/.env" does not exist and is
#     silently skipped -> byte-identical to the old relative ".env".
#   Local (config.py at <repo>/python-engine/config.py): overlays the repo-root
#     .env (Zerodha/MiniMax/partner creds) that docker-compose injects as env
#     vars in prod, so scripts/tests run outside Docker see the same config.
# OS environment variables still outrank both files, so compose's env_file
# injection (the production source of truth) is unaffected.
_ENGINE_DIR = Path(__file__).resolve().parent

class Settings(BaseSettings):
    """
    POSITION SIZING PHILOSOPHY:
    At Rs5,000 bankroll:
      risk_per_trade (1%)       = Rs50
      max_positions             = 4
      max_total_risk (4%)       = Rs200  across all open trades
      max_per_trade capital     = Rs1,500 (30%)
      daily_loss_halt_threshold = Rs100  (2%)
      drawdown_halt_threshold   = Rs500  (10%)

    This is deliberately conservative. The primary goal at this
    bankroll size is capital preservation and system validation -
    not return maximisation. All limits scale naturally as bankroll
    grows because they are expressed as percentages.
    """
    model_config = SettingsConfigDict(
        env_file=(str(_ENGINE_DIR / ".env"), str(_ENGINE_DIR.parent / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )
    
    STRATEGY_VERSION: str = "1.0.0"
    DB_PATH: str = "/data/cache.db"
    # Offline proactive research is opt-in.  The scheduled consumer accepts
    # only an explicitly marked local SHADOW fixture and has no broker,
    # transport, or partner-delivery dependency.
    # Enabled scheduler consumer; it remains inert until an explicitly marked
    # local SHADOW fixture is configured, and has no broker/delivery path.
    PROACTIVE_SHADOW_ENABLED: bool = True
    PROACTIVE_SHADOW_FIXTURE_PATH: str = ""
    # ``LEGACY_FIXTURE_V1`` preserves the existing demonstration workflow.
    # ``RECORDED_COMPLETED_BARS_V1`` is an explicit, read-only provider
    # contract; it still has no order, delivery or broker-consumer path.
    PROACTIVE_SHADOW_DATA_SOURCE: str = "LEGACY_FIXTURE_V1"
    PROACTIVE_SHADOW_COMPLETED_BAR_FIXTURE_PATH: str = ""
    # Dated, archived-master-verified NIFTY/SENSEX bar mappings, e.g.
    # {"NIFTY":{"token":...,"basis":"FUTURE","master_sha256":"..."}}.
    # A bare positive token is intentionally rejected: tokens may be reused.
    PROACTIVE_SHADOW_KITE_TOKENS_JSON: str = ""
    PROACTIVE_SHADOW_MAX_DATA_AGE_SECONDS: int = 1800
    PROACTIVE_SHADOW_ACCOUNT_ID: str = "dev-shadow"
    PROACTIVE_SHADOW_RUN_ID: str = "dev-shadow-v1"
    PROACTIVE_SHADOW_SCENARIO_CAPITAL: float = 8000.0
    # Optional account label for imported broker-statement evidence. Empty is
    # deliberately unavailable; it does not trigger a broker connection.
    BROKER_RECONCILIATION_ACCOUNT_ID: str = ""
    # [WORKFLOW-J.2 2026-09-13] Operator-supplied Phase 1 F&O eligibility
    # list for CAS. Per NSE/CMTR/72394 (effective 2026-01-19), CAS Phase 1
    # applies only to cash-segment stocks on which derivative contracts
    # are available. The full list lives in the NSE contract-master API;
    # Dev has no live fetch, so the list is operator-curated.
    #
    # Wire format (env var, comma-separated): one symbol per token,
    # whitespace tolerated around commas. Examples:
    #   CAS_PHASE1_FNO_UNDERLYINGS="RELIANCE"
    #   CAS_PHASE1_FNO_UNDERLYINGS="RELIANCE, HDFCBANK, INFY"
    #
    # We store it as the raw CSV string -- NOT a typed tuple / list --
    # because pydantic-settings v2.2.1 parses complex (list/tuple)
    # fields as JSON by default, which would force operators to write
    # JSON syntax in their .env. Keeping the wire format as a plain
    # string is operator-friendly AND lets us validate / normalise at
    # the point of use (``is_cas_eligible`` strips whitespace,
    # uppercases, drops empty tokens).
    CAS_PHASE1_FNO_UNDERLYINGS: str = ""
    # Separate proof of broker ORDER permission from token/quote readiness.
    # Empty means a state file beside DB_PATH.  BLOCKED persists across restart.
    ORDER_EXECUTION_STATE_PATH: str = ""
    UNIVERSE_PATH: str = "/data/nifty500.csv"
    # [PENNY 2026-06-24] Penny universe JSON path (the static-rank JSON
    # loaded by PennyUniverse at startup, refreshed daily by
    # penny_universe.refresh_from_kite). Single source of truth so the
    # pre-market digest module reads the same file the scanner reads.
    PENNY_UNIVERSE_JSON_PATH: str = "/data/penny_static.json"
    # TOKEN_INJECTION_SECRET removed -- the /token endpoint no longer uses it.
    # The old commented-out endpoint that checked this secret has been removed.
    
        # Core Bankroll (Only used for INITIAL seeding)
    # [CAPITAL-REALLOC 2026-07-26] 5,000 -> 4,500. The Nifty seed funds swing +
    # momentum, split by MOMENTUM_POOL_PCT. Uru moved Rs 500 of REAL capital out
    # of swing and into the penny edge live book, which is the only division that
    # has actually made money (+Rs 39 live over 6 trades, 67% win rate, vs swing's
    # -Rs 169 over 12). Momentum is deliberately held whole at Rs 2,500 -- see
    # MOMENTUM_POOL_PCT below for the arithmetic that keeps it there.
    INITIAL_BANKROLL: float = 4500.0
    RISK_PCT: float = 0.10 # 10% Risk per Swing Trade (Hyper-Aggressive)

    # Portfolio Limits
    MAX_OPEN_POSITIONS: int = 6 
    MAX_CAPITAL_PER_TRADE_PCT: float = 0.50 # Increased to allow larger trades
    MAX_SECTOR_EXPOSURE_PCT: float = 0.40
    MAX_CORRELATED_POSITIONS: int = 2
    MAX_TOTAL_RISK_PCT: float = 0.6 # Increased to match high risk

    # Circuit Breakers
    CB_DAILY_LOSS_PCT: float = 0.20 # Allow 20% daily loss (allows 2 full stop-outs)
    CB_MAX_CONSECUTIVE_LOSSES: int = 5 
    CB_MAX_DRAWDOWN_PCT: float = 0.50 # Allow 50% total drawdown
    CB_FLOOR_PCT: float = 0.40

    # [HALT 2026-08-05] Whether a breached circuit breaker trips the filesystem
    # kill switch (/data/HALT) and actually stops new entries.
    #
    # Default True, because the alternative is what we had: a correct `halted`
    # boolean that no entry path ever read. The failure mode of True is that a
    # buggy breaker stops trading until the operator clears it -- which pages
    # immediately and costs missed trades, not money. The failure mode of False
    # is the 2026-07-24 shape, where the system keeps sizing into a book it has
    # already decided is broken.
    #
    # Set False only to diagnose a suspected false trip; the sentinel can still
    # be tripped by hand from Telegram while it is off.
    HALT_AUTO_TRIP_ON_CIRCUIT_BREAKER: bool = True

    # [HALT-SCOPE 2026-08-05] Which channels an auto-trip stops.
    #
    # The breakers above are measured against `source IN ('SYSTEM','MOMENTUM')`
    # only -- penny, edge and F&O P&L are deliberately excluded from every
    # threshold (performance.py, the 2026-06-24 strict separation; penny has its
    # own kill switch in PennyRiskEngine). A GLOBAL trip on that evidence would
    # silence three books on the strength of none of them, and the trip does not
    # self-clear, so a five-loss momentum streak at 09:30 would kill the whole
    # day everywhere.
    #
    # So the trip is scoped to the channels the breakers actually measure.
    # 'momentum' is the only live order channel among SYSTEM/MOMENTUM -- swing
    # has no distinct order path today (the only channels at any place_order
    # call site are momentum, penny, fno). If swing ever gets one, add it here
    # or the breakers will not cover it.
    #
    # Comma-separated, not a JSON list: pydantic-settings JSON-parses complex
    # types straight from the environment, so `CB_HALT_CHANNELS=momentum` would
    # raise at import and the engine would not start at all. A kill switch's
    # configuration must not be able to do that.
    #
    # An EMPTY string means trip globally. That is the escape hatch, not the
    # default; a manual `/halt <reason>` is still global.
    CB_HALT_CHANNELS: str = "momentum"

    # [WORKFLOW-A1 2026-09-20] Owner entry-only global halt.
    #
    # When True, the engine refuses every NEW entry (F&O, EDGE,
    # Momentum, Penny) outside shadow. Exit handling, position
    # management, and cash-ledger reconciliation continue. The
    # motivation per the audit
    # (docs/2026-09-20-independent-system-readiness-audit.md §4):
    # manual Momentum Telegram execution remains broker-capable
    # even after the per-sleeve live-disable switches, which is
    # not a global live halt. This knob makes the global halt
    # explicit and visible to operators.
    #
    # Default OFF. Read at process start; flipping it requires a
    # restart (documented below). When set via the env var the
    # value ``1``, ``true``, ``yes`` (case-insensitive) means True;
    # anything else means False.
    OWNER_LIVE_ENTRY_HALT: bool = False

    # [WORKFLOW-A1 2026-09-20] Per-channel kill-switch defaults.
    #
    # Comma-separated, NOT a JSON list (see CB_HALT_CHANNELS note
    # above for the rationale). Channels are:
    #   momentum, penny, edge, fno
    # An EMPTY string means no per-channel controls. A channel
    # listed here is BLOCKED at the entry gate; exits continue.
    # This is the per-channel companion to OWNER_LIVE_ENTRY_HALT
    # so operators can silence one sleeve without halting the rest.
    OWNER_LIVE_ENTRY_HALT_CHANNELS: str = ""

    # Momentum
    MAX_MOMENTUM_POSITIONS:   int   = 5
    # [CAPITAL-REALLOC 2026-07-26] 0.50 -> 5/9. The Rs 500 that left the Nifty
    # seed had to come entirely out of swing, not be shared with momentum, so the
    # split moves to keep momentum's rupee allocation unchanged:
    #     momentum = 4500 * 5/9 = 2500  (was 5000 * 0.50 = 2500)
    #     swing    = 4500 * 4/9 = 2000  (was 5000 * 0.50 = 2500)
    # Written as a fraction rather than 0.5556 so the intent survives edits.
    MOMENTUM_POOL_PCT:        float = 5.0 / 9.0
    MOMENTUM_POOL_FREEZE_PCT: float = 0.80    
    MOMENTUM_MIN_CANDLES:     int   = 4
    # [MOMENTUM-SEMAPHORE 2026-07-15] Bounded concurrency over the per-ticker
    # scan. The 500-ticker asyncio.gather opened ~1000 concurrent sqlite
    # connections to cache.db (2 candle fetches each), exhausting the OS
    # file-handle ceiling -- exactly the 2026-07-07 penny incident, which is why
    # 500-scan tails logged "unable to open database file" (52 tickers on
    # 2026-07-15). 50 mirrors PENNY_HISTORY_SQLITE_MAX_CONCURRENT: well above
    # Kite's 3 req/s so the API stays the bottleneck, well below the fd ceiling.
    MOMENTUM_SCAN_SQLITE_MAX_CONCURRENT: int = 50
    MOMENTUM_VOL_SURGE_PCT:   float = 1.5     # [Q13] Lowered from 2.0x - see Known Quirks
    MOMENTUM_R_TARGET:        float = 1.6   # [TARGET-REACH 2026-07-31] legacy default, moved with R1
    MOMENTUM_MAX_COST_RATIO:  float = 0.25
    # [MOMENTUM-REGIME 2026-06-16] Legacy 10% risk per trade.
    # Kept for backward compat -- new code should use MOMENTUM_RISK_PCT_R{1,2,3}.
    # R1 default (0.07) is MORE conservative than the legacy 0.10 -- a deliberate
    # tightening since we now have regime gating to back it up.
    MOMENTUM_RISK_PCT:        float = 0.10    # Legacy 10% -- pre-regime value
    # [STOP-FLOOR 2026-07-26] Minimum stop distance as a fraction of entry.
    #
    # The momentum stop is the low of the breakout candle -- a ONE-MINUTE bar. On
    # the 10 real positions traded to date that produced stops of 0.07%, 0.09%,
    # 0.26%, 0.46%, 0.66%, 0.79%, 0.98%, 0.99%, 1.57% and 1.92% from entry. The
    # tightest of those are inside the bid-ask spread plus ordinary one-minute
    # noise, so the exit is close to a coin flip: 2 wins in 8 momentum trades.
    #
    # Worse, sizing is risk-based (shares = risk_budget / risk_per_share), so a
    # noise-width stop asks for a huge position and is then truncated by the pool
    # cap -- meaning the TIGHTER the stop, the LARGER the position. URBANCO sized
    # to 33 shares (Rs 4,376, ~88% of the account) off a Rs 0.09 stop, and its
    # +0.97% move was recorded as +14.35R. Every R-based statistic in the system
    # inherits that distortion.
    #
    # 0.5% is set just outside round-trip intraday costs (~0.1-0.2% on these
    # notionals) and typical one-minute noise on NSE mid-caps. This does not make
    # the strategy profitable; it makes its risk, its sizing and its R-multiples
    # mean what they claim to mean, which is the precondition for judging it.
    # [STOP-NOISE 2026-07-31] Raised 0.005 -> 0.012. Round-trip cost as a
    # fraction of 1R is a function of STOP WIDTH ALONE -- it is independent of
    # price and (above small sizes) of share count, because both turnover and
    # risk scale with the position:
    #     0.5% stop -> 0.12-0.24 R of cost      1.2% stop -> 0.06-0.12 R
    # A 0.5% floor therefore hands ~a fifth of every unit of risk to the broker
    # before the trade has an opinion, which is exactly how 27-30 Jul produced a
    # gross P&L of MINUS Rs 0.35 alongside Rs 17.90 of costs. It also sat inside
    # the noise: 8 of 8 trades ran 0.52-1.16% stops and not one was ever hit.
    MOMENTUM_MIN_STOP_PCT:    float = 0.012   # 1.2% floor on the momentum stop
    # [STOP-NOISE 2026-07-31] The 0.5% percentage floor is still inside intraday
    # noise for most mid-caps. Four days of live trades (27-30 Jul) ran stops of
    # 0.52-1.16%, and all 8 exited on the clock between -0.71R and +0.49R: the
    # stop was never hit, the target was never reached, and the R-multiple
    # measured nothing. Because sizing divides the risk budget by the stop
    # distance, a too-tight stop ALSO maximises share count and therefore
    # turnover -- those 8 trades produced a gross P&L of MINUS Rs 0.35 against
    # Rs 17.90 of costs. Costs were 10-23% of 1R.
    #
    # An ATR-proportional floor fixes both ends at once: the stop sits outside
    # the day's noise, and at constant rupee risk the wider stop cuts share
    # count, turnover and therefore cost drag (a 0.6% -> 1.8% stop on the same
    # risk budget is ~3x fewer shares and ~3x less cost).
    # The multiplier is NOT free: it has to coexist with the MC5 fuel gate,
    # which requires target_distance <= 0.85 * (daily ATR - range already
    # consumed). Since target_distance = stop_mult * R_target * ATR, the gate
    # permits entry only while
    #     consumed/ATR  <=  1 - (stop_mult * R_target) / 0.85
    # At 0.55x with a 2.0R target that bound is NEGATIVE (-0.29): the gate
    # could never pass and momentum would have stopped trading entirely and
    # silently. At 0.35x with a 1.6R target it is +0.34, i.e. entries are
    # allowed until about a third of the day's range is used up -- outside the
    # noise, still reachable, and it correctly refuses late entries into an
    # already-exhausted day. See MOMENTUM_R_TARGET_R1 below, which moved with it.
    MOMENTUM_MIN_STOP_ATR_MULT: float = 0.35  # stop >= 0.35x daily ATR below entry
    # Reject a signal whose round-trip cost exceeds this fraction of 1R. The
    # existing MOMENTUM_MAX_COST_RATIO measures cost against profit AT TARGET,
    # which assumes a 2R exit that 0 of 8 live trades achieved. Measuring
    # against 1R prices the risk actually being underwritten.
    MOMENTUM_MAX_COST_PER_R:  float = 0.12
    # [MOMENTUM-PAPER 2026-07-26] Paper twin of the live momentum book.
    #
    # Live momentum entries are MANUAL: the screener sends a Telegram EXEC button
    # and a human decides. So the ledger records what the operator did, never what
    # the strategy proposed -- 8 recorded trades in months, which is why nothing
    # can be concluded about it. The paper book takes EVERY accepted signal
    # automatically, sized off its own pool, so the strategy accumulates a record
    # of its own decisions independent of whether anyone was at the keyboard.
    #
    # This matters now specifically: MOMENTUM_MIN_STOP_PCT changes the stop, the
    # sizing and the R distribution all at once, and there is no way to evaluate
    # that on ~2 manual trades a month. Rs 50,000 sizes comfortably across
    # Nifty500 names (the live Rs 2,500 pool buys 0-1 shares of most of them, so
    # tick size and costs dominate any signal).
    #
    # Paper NEVER touches the broker -- see momentum_paper.py, which has no
    # order-placing code path at all rather than a flag that must stay False.
    MOMENTUM_PAPER_ENABLED:   bool  = True
    MOMENTUM_PAPER_BANKROLL:  float = 50000.0
    # Broker-free research side-channel. It evaluates declared variants using
    # frames already fetched by the live scanner and never reaches sizing or
    # order execution, so evidence collection is safe to enable by default.
    MOMENTUM_SHADOW_ENABLED:  bool  = True
    MOMENTUM_ATR_FUEL_BUFFER:          float = 0.85   # [MC5] ATR exhaustion gate: target must fit within remaining_fuel * buffer
    MOMENTUM_VOL_SURGE_LUNCHTIME:      float = 1.75   # [MC3-T] Volume threshold during lunchtime dead zone (11:30-13:15 IST)
    MOMENTUM_LUNCHTIME_START_HOUR:     int   = 11     # [MC3-T] Lunchtime start hour (IST)
    MOMENTUM_LUNCHTIME_START_MIN:      int   = 30     # [MC3-T] Lunchtime start minute (IST)
    MOMENTUM_LUNCHTIME_END_HOUR:       int   = 13     # [MC3-T] Lunchtime end hour (IST)
    MOMENTUM_LUNCHTIME_END_MIN:        int   = 15     # [MC3-T] Lunchtime end minute (IST)
    MOMENTUM_MORPHOLOGY_MIN_SCORE:     float = 0.65   # [MC6] Minimum close_position_score to reject shooting-star candles
    MOMENTUM_R_TARGET_BEAR:            float = 1.3    # [MR2] R target in BEAR_RS_ONLY regime (moved with R1/R2 2026-07-31)

    # [MOMENTUM-REGIME 2026-06-16] Regime-aware momentum settings.
    # Replaces the single 'market_regime' string dispatch (BULL/BEAR_RS_ONLY)
    # with the 3-regime system already used for swing. Slight-risk tilt
    # toward activity: R1/R2 sized aggressively, R3 has the *option* to trade
    # (default risk=0% is a guardrail -- flip to 0.05+ in .env to allow R3 entries).
    #
    # [MOMENTUM-AGGRESSIVE 2026-06-16] User feedback: momentum was 1-2 sigs/day,
    # wanted more P&L. Restored pre-regime sizes (10% / 7%) for R1/R2.
    # MOMENTUM_BLOCK_R3_ENTRIES default flipped to False (R3 *can* trade when
    # R3 risk > 0), defense-in-depth via MOMENTUM_RISK_PCT_R3=0% remains.
    MOMENTUM_BLOCK_R3_ENTRIES:  bool  = False   # OFF: don't hard-block R3; guardrail is RISK_PCT_R3=0.00
    MOMENTUM_RISK_PCT_R1:       float = 0.10    # 10% of momentum pool in R1 (calm, aggressive)
    MOMENTUM_RISK_PCT_R2:       float = 0.07    # 7% in R2 (elevated) -- smaller position
    MOMENTUM_RISK_PCT_R3:       float = 0.00    # 0% in R3 default (defense-in-depth: raise in .env to enable)
    # [TARGET-REACH 2026-07-31] Lowered 2.0/1.5 -> 1.6/1.3 to match the wider
    # ATR-floored stop. The target is denominated in R, so widening the stop
    # pushes the target further away in rupees; at the old 2.0R the target sat
    # 1.1 daily ATRs from entry, which is a move the stock has to make AFTER we
    # are already in. The 27-30 Jul book reached a peak of +0.49R across 8
    # trades -- the target was not the binding constraint, reachability was.
    # 1.6R * 0.35 ATR = 0.56 ATR is inside a normal day's remaining range, and
    # the trail (breakeven at +0.6R, then 1.5x ATR behind price) is what
    # captures the trades that run further than the target in a strong market.
    MOMENTUM_R_TARGET_R1:       float = 1.6     # 1.6R target in R1 (trail carries the runners)
    MOMENTUM_R_TARGET_R2:       float = 1.3     # 1.3R target in R2 (faster take-profit)

    # [MOMENTUM-CAS-BOUNDARY 2026-09-16] Auto-square starts at 15:13 IST
    # with a hard 15:14:30 submit cutoff, before the 15:15 CAS boundary.
    # (MIS = intraday product; broker auto-squares anyway). Flip to True in .env
    # to let momentum winners run past 3:15 IST. Only effective when the engine
    # has switched positions to CNC (see evaluate_momentum_signal [MR3]).
    MOMENTUM_ALLOW_OVERNIGHT:   bool  = False   # False = 15:13 auto-square stays; True = hold to trailing-stop only
    MOMENTUM_R3_MAX_POSITIONS:  int   = 1       # Soft cap for R3 entries (replaces hard block)

    # [TIER0-0.1 2026-07-14] Intraday exit management for MIS momentum.
    #
    # Until today there was NO exit logic at all: Zerodha GTT is CNC-only so MIS
    # got no broker-side stop (executor.js `if (!isIntraday)`), and no scheduled
    # job evaluated stop or target between the fill and the 15:15 auto-square. The
    # stop_loss and target_1 that evaluate_momentum_signal computes -- which SIZE
    # the position -- were enforced by nothing. Live proof: 7 of 7 momentum trades
    # exited on the 15:15 clock; not one hit its target or its stop.
    #
    # The stop now rests at the broker as an SL-M. This monitor owns the rest:
    # target, breakeven ratchet, ATR trail, and a time stop for dead trades.
    MOMENTUM_INTRADAY_MONITOR_SEC: int   = 60    # How often to check open MIS positions
    # [BREAKEVEN 2026-07-31] Lowered 1.0 -> 0.6. The ratchet is what converts a
    # trade that WORKED but did not reach target into a small win instead of a
    # round trip to zero. At +1.0R it essentially never engaged: the best of the
    # 27-30 Jul trades peaked at +0.49R at its exit, so every one of them gave
    # back whatever it had and paid costs on the way out. 0.6R is above the
    # noise band the ATR stop floor now establishes, and it is reachable.
    MOMENTUM_BREAKEVEN_R:          float = 0.6   # Ratchet stop to cost-adjusted breakeven at +0.6R
    MOMENTUM_TRAIL_ATR_MULT:       float = 1.5   # Trail at 1.5x daily ATR once past breakeven
    MOMENTUM_USE_TRAIL:            bool  = True  # Trail after breakeven (False = flat stop at BE)

    # [TIME-STOP-V2 2026-07-31] Two-tier, regime-aware.
    #
    # The single 45-minute / +0.5R rule fired on 8 of 8 live trades between
    # 27-30 Jul. It was simultaneously too SLOW for failures (a trade already
    # negative at 25 minutes was held another 20) and far too FAST for
    # winners: a 2R target on a ~1% stop needs a ~2% move, which 45 minutes
    # does not supply, so the rule guaranteed a ~0R exit minus costs. The
    # +0.5R bar sat exactly above where every trade actually got to.
    #
    # Two tiers now:
    #   FAST -- a trade that is NEGATIVE after MOMENTUM_TIME_STOP_FAST_MIN has
    #           already falsified its thesis; cut it and stop paying for it.
    #   SLOW -- a trade that is merely going nowhere gets much longer to work,
    #           and the bar it must clear to survive is lowered to a level that
    #           is actually reachable in the window.
    # A trade above the bar is still never cut on the clock -- the trail owns it.
    MOMENTUM_TIME_STOP_FAST_MIN:   int   = 25    # Cut a NEGATIVE trade after 25 min
    MOMENTUM_TIME_STOP_FAST_R:     float = 0.0   # "Negative" means below 0R
    # [THESIS-EXIT 2026-08-04] At the 25-min checkpoint, decide with the SETUP
    # rather than the clock: a trade still holding above the VWAP it broke out
    # from has not failed, it just has not paid yet.
    #
    # "r_now < 0 after 25 minutes" cannot separate a dead trade from a slow
    # one, and that distinction is most of the P&L. INDIACEM on 2026-08-04 was
    # -0.45R at 25 minutes and +1.10R at 65 (the fast tier would have cut it at
    # the low, for -Rs 8.21 instead of a scale-out at +Rs 5.47). SUMICHEM on
    # 2026-08-03 was scratched at 11:27 and printed its target at 12:00.
    #
    # VWAP-at-entry is not a new tuned number -- it is the level the entry was
    # already measured against (see the MC gates no_recent_vwap_crossover and
    # crossed_but_failed_holding_vwap). Nothing here was fitted to rescue a
    # specific trade. Risk stays bounded by the broker stop, the slow tier and
    # the 15:15 square-off; only the REASON for cutting changes.
    #
    # False falls back to the pure r_now < FAST_R test, as do positions with no
    # vwap_at_entry recorded.
    MOMENTUM_FAST_STOP_USES_THESIS: bool = True
    MOMENTUM_TIME_STOP_MIN:        int   = 90    # Cut a going-nowhere trade after 90 min
    MOMENTUM_TIME_STOP_MIN_R:      float = 0.25  # Survives the 90-min cut above +0.25R
    # Regime scaling on the SLOW window: a strong trend deserves more runway,
    # chop deserves less. Multiplies MOMENTUM_TIME_STOP_MIN.
    MOMENTUM_TIME_STOP_R1_MULT:    float = 1.0   # Normal/trending -- full 90 min
    MOMENTUM_TIME_STOP_R2_MULT:    float = 0.67  # Elevated vol -- ~60 min
    MOMENTUM_TIME_STOP_R3_MULT:    float = 0.5   # Crisis -- ~45 min, get out

    # [MOMENTUM-ENTRY-V2 2026-06-16] Optional additive entry filters.
    # All default OFF -- opt-in via .env. Each filter is independently gated by
    # its own MOMENTUM_USE_* flag. Skip first 15-min + last 30-min chop by default
    # (MOMENTUM_USE_TIME_GATE=True is the recommended safe default).
    # References: 4-variant ORB study (dailybulls.in 2026), Nifty 8yr backtest
    # (intradaylab.com 2026), 190k-trade ORB study (orbsetups.com 2026).
    MOMENTUM_USE_RVOL:          bool  = False   # [MC7] Relative-volume vs 20-bar 15-min avg
    MOMENTUM_RVOL_MIN_RATIO:    float = 1.5     # [MC7] RVOL threshold (last bar vol / avg)
    MOMENTUM_RVOL_LOOKBACK:     int   = 20      # [MC7] Lookback bars (15-min each)
    MOMENTUM_USE_TIME_GATE:     bool  = True    # [MC0] Skip 9:15-9:30 + 15:00-15:30
    MOMENTUM_ENTRY_START_MIN:   int   = 45      # [MC0] Minutes from 9:15 IST when entries allowed (45 = 10:00)
    # [MC0-DEADLINE 2026-08-04] Was 840, commented "= 14:45". It is not:
    # 840 minutes past 09:15 is 23:15, so the late gate has never once fired
    # and MC0 has only ever been a "too early" check. On 2026-08-03 that let
    # TRITURBINE alert at 14:55 for a book that auto-squares at 15:15 -- a
    # 15-minute window against a 45-minute time stop, i.e. an entry that could
    # only ever exit on the clock.
    #
    # The deadline has to be derived from the square-off, not picked. The
    # binding tier is the FAST time stop, not the slow one -- a 14:30 entry
    # never sees the 90-min slow cut, but the 15:15 square-off reaches the same
    # outcome, so that is acceptable. What is not acceptable is an entry with
    # less life than the fast cut needs to evaluate the thesis at all:
    #     square-off                     15:15
    #   - 1.5x MOMENTUM_TIME_STOP_FAST_MIN  :38   room for the thesis to work
    #   - alert + EXEC latency              :15   3-min poll, MiniMax, human
    #   = last usable bar close           ~14:22  -> rounded down to 14:15
    # test_momentum_entry_deadline_is_consistent_with_square_off pins this so
    # the number cannot drift away from its justification again.
    MOMENTUM_ENTRY_END_MIN:     int   = 300     # [MC0] 14:15 IST -- last bar close that can still get a full time-stop window
    MOMENTUM_USE_RSI_TRIM:      bool  = False   # [MC8] Partial trim 50% at RSI(7)>=70 on 15-min
    MOMENTUM_RSI_TRIM_LENGTH:   int   = 7       # [MC8] RSI length (7 is the orbsetups sweet spot)
    MOMENTUM_RSI_TRIM_THRESHOLD: float = 70.0   # [MC8] RSI >= this -> partial trim fires

    # [SCALE-OUT 2026-08-04] Bank part of the position at +1R and move the
    # runner's stop to cost-adjusted breakeven.
    #
    # The evidence for this is the shape of every closed momentum trade so far:
    # 27 Jul - 03 Aug produced thirteen exits, one at the target and twelve on
    # the clock or a ratcheted stop, spread between -0.71R and +0.49R. The
    # direction call was frequently right and collected nothing, because the
    # only two ways to book a gain were "price prints the target" and "price
    # prints the trail". SUMICHEM on 2026-08-03 is the whole problem in one
    # trade: stopped at breakeven 11:27, printed its target 12:00.
    #
    # A partial at +1R changes what the strategy is being asked to predict. It
    # no longer needs the full move to happen before the clock runs out -- it
    # needs the move to START. The cost is the upper tail: a 2R winner now
    # collects ~1.5R.
    #
    # Honest limitation at this bankroll: a 2,428 momentum pool buys 1-4 shares,
    # and 50% of 1 share is 0. This fires on maybe a third of current signals
    # and logs momentum_scale_out_skipped_size on the rest. That is a position-
    # sizing constraint, not an exit-logic one, and the log makes it countable.
    MOMENTUM_USE_SCALE_OUT:     bool  = True
    MOMENTUM_SCALE_OUT_R:       float = 1.0     # R-multiple at which the partial fires
    MOMENTUM_SCALE_OUT_FRAC:    float = 0.5     # fraction of the position sold

    # [MOMENTUM-LOG 2026-06-16] Append-only signal log to /data/momentum_signals.csv
    # and SQLite table `momentum_signals`. Both are opt-in so the operator can
    # disable on disk-constrained systems. The CSV is the easy one to grep /
    # backtest on; the SQLite table is for future API-driven backtest queries.
    MOMENTUM_LOG_ENABLED:       bool  = True    # Master switch -- set False to disable entirely
    MOMENTUM_LOG_CSV_PATH:      str   = "/data/momentum_signals.csv"
    # [MED-002 / ROADMAP-4.6 2026-07-12] Container B's plain-text scan
    # summaries duplicated the agent's button alerts (two messages per
    # cycle, only one actionable). OFF by default; the data lives on in
    # ops_metrics + hourly reports. Flip via .env to restore.
    SCREENER_PLAIN_SUMMARY_ENABLED: bool = False
    MOMENTUM_LOG_DB_TABLE:      str   = "momentum_signals"

    MOMENTUM_FIRST_SCAN_HOUR: int   = 10
    MOMENTUM_FIRST_SCAN_MIN:  int   = 15
    CONTAINER_A_URL:          str   = "http://node-gateway:3000"
    INTERNAL_API_SECRET:      str   = ""      # must be set in .env

    # [HIGH-001 2026-07-12] A whitespace-only secret must not count as
    # "configured" -- strip it so the auth gate's empty check (503 +
    # operator alert) catches it. Deliberately NOT a required field:
    # per operator mandate 2026-06-25 a misconfigured secret must not
    # block boot during market hours; the gate degrades to 503 instead.
    @field_validator("INTERNAL_API_SECRET")
    @classmethod
    def _strip_internal_api_secret(cls, v: str) -> str:
        return v.strip()

    # RS Module
    RS_PERIODS:               int   = 20
    RS_MIN_THRESHOLD:         float = 5.0
    RS_MIN_DAYS_ABOVE_AVG:    int   = 3
    RS_LOOKBACK_DAYS:         int   = 5

    # Cost model
    ZERODHA_BROKERAGE_PCT:    float = 0.0003  # 0.03%
    ZERODHA_BROKERAGE_MAX:    float = 20.0    # Rs20 cap
    ZERODHA_STT_CNC:          float = 0.001   # 0.1% sell side

    # --- Telegram (for penny hourly report delivery) ---
    # 2026-06-23: penny hourly report now tries Telegram first, falls back
    # to PENNY_HOURLY_REPORT_WEBHOOK. Both creds are read from env.
    TELEGRAM_BOT_TOKEN:       str   = ""       # @BotFather token
    TELEGRAM_CHAT_ID:         str   = ""       # numeric chat ID (user/group)

    # --- Penny subsystem brokerage/fees (mirrored from Nifty, scoped to penny) ---
    # 2026-06-22 deviation: penny code now does its own cost accounting per
    # the isolation rule (no import from engine.calc_zerodha_costs).
    PENNY_STT_MIS:             float = 0.00025   # 0.025% sell side (intraday)
    PENNY_STT_CNC:             float = 0.001     # 0.1% sell side (delivery)
    PENNY_BROKERAGE_PCT:       float = 0.0003    # 0.03% per side
    PENNY_BROKERAGE_MAX:       float = 20.0      # Rs 20 cap per order
    PENNY_EXCHANGE_PCT:        float = 0.0000307  # NSE cash 0.00307%, both sides
    PENNY_STAMP_DUTY_PCT:      float = 0.00003   # intraday 0.003%, buy side
    PENNY_SEBI_PCT:            float = 0.000001   # Rs 10/cr, both sides
    PENNY_IPFT_PCT:            float = 0.000000001 # NSE Rs 0.01/crore, both sides
    PENNY_GST_PCT:             float = 0.18       # 18% on brokerage+exchange
    # [PENNY-TEST 2026-06-24] When True, calc_penny_costs() returns 0.0 so
    # P&L math is isolated from Rs 2,500 brokerage erosion. Use only in
    # test/paper mode to measure system proactiveness (how many trades it
    # fires, what the gross edge would be). Live trading MUST keep this False
    # -- a zeroed-cost live run understates real P&L and breaks the ledger.
    PENNY_BROKERAGE_BYPASS:    bool  = False
    ZERODHA_STT_MIS:          float = 0.00025 # 0.025% sell side (intraday)
    ZERODHA_EXCHANGE_PCT:     float = 0.0000307
    ZERODHA_STAMP_DUTY_PCT:   float = 0.00003
    ZERODHA_SEBI_PCT:         float = 0.000001
    ZERODHA_IPFT_PCT:         float = 0.000000001 # NSE Rs 0.01/crore, both sides
    ZERODHA_GST_PCT:          float = 0.18

    # ============================================================
    # PENNY STOCK SUBSYSTEM (2026-06-21, spec docs/superpowers/specs/2026-06-21-penny-stock-expansion-design.md)
    # ============================================================
    # All settings default OFF / safe. Live trade is opt-in via PENNY_LIVE_TRADING=true.

    # Universe
    PENNY_PRICE_MIN:               float = 1.0
    PENNY_PRICE_MAX:               float = 55.0
    PENNY_UNIVERSE_SIZE:           int   = 100
    PENNY_MIN_20D_TV:              float = 500_000.0   # Rs 5 lakh, 20-day median traded value floor
    PENNY_MAX_PROMOTER_HOLD:       float = 0.75        # see MIN_PROMOTER_HOLD below
    PENNY_MIN_PROMOTER_HOLD:       float = 0.25        # strictly > 25% AND strictly < 75%
    PENNY_MAX_PB_RATIO:            float = 2.0         # Price-to-Book <= 2.0 (loose asset floor)
    PENNY_REFRESH_HOUR:            int   = 8
    # [PENNY-HISTORY-SEMAPHORE 2026-07-07] Bounded concurrency over
    # the per-ticker sqlite open in compute_metrics_from_history. The
    # 2026-07-07 incident showed that fanning 9,769 concurrent sqlite
    # opens hits the OS file-handle ceiling and the WAL pragma can't
    # recover. 50 is well above Kite's 3 req/s HTTP rate limit (so we
    # don't bottleneck on the API) but well below the OS-level ceiling
    # on /data/cache.db. Tunable per deployment via env var.
    PENNY_HISTORY_SQLITE_MAX_CONCURRENT: int = 50

    # Connors strategy
    # [CONNORS-EVIDENCE 2026-08-04] DO NOT loosen the RSI-rising confirmation
    # gate in penny_engine_connors (`rsi > rsi_prev1 > rsi_prev2`).
    #
    # It looks like the obvious culprit for this book never trading: of the
    # 3,231 candidates that clear the trend filters and the RSI(2)<10 trigger
    # over 2.5 years and 2,532 tickers, that gate rejects 3,225 -- 99.8%. The
    # two conditions are nearly mutually exclusive by construction, because
    # two consecutive up days in RSI(2) from a sub-10 reading usually carries
    # it back above 10.
    #
    # It was measured before being touched (tools/connors_backtest.py, real
    # daily bars, entry at next-day open, real Zerodha costs):
    #
    #   config                signals   win%    meanR   PF      t
    #   RSI<10 rising=Y             1    0.0  -0.2035  0.00   0.00
    #   RSI<10 rising=N         1,202   46.4  -0.0365  0.93  -1.29
    #   RSI<15 rising=Y             6   50.0  +0.3608  8.55   0.88
    #   RSI<20 rising=Y             9   66.7  +0.3661 41.41   1.29
    #   RSI<20 rising=N         1,359   46.7  -0.0283  0.91  -1.06
    #   RSI<25 rising=Y            11   63.6  +0.3802 19.30   1.26
    #
    # Removing the gate buys ~1,300 trades that LOSE money (PF 0.87-0.97).
    # Keeping it leaves a handful of trades with a good-looking mean R and a
    # t-stat around 1.3 -- which is not significance, it is nine trades.
    #
    # So: the gate stays, and RSI2_BUY stays at 10 until there is enough
    # history to justify moving it. Raising it to 20 is the most promising
    # single change available (9x the signals at the same expectancy), but
    # nine trades cannot authorise real capital. Re-run the backtest once
    # DAILY_HISTORY_DAYS has actually accumulated depth.
    PENNY_CONNORS_RSI2_BUY:        float = 10.0
    PENNY_CONNORS_RSI2_SELL:       float = 65.0
    # [EXIT-EVIDENCE 2026-08-05] These four are UNCHANGED, and the evidence
    # below is why they are worth changing later but must not be changed now.
    #
    # The random-control skill test (skill_test.py) asks a question the P&L
    # column cannot: on the days the screener fired, would a RANDOM eligible
    # NSE name run through the same exits have done as well? Over 1,437 trades
    # at RSI<25 rising=N:
    #
    #     observed mean R        -0.029
    #     random control mean R  -0.092     <- random LOSES with these exits
    #     excess over random     +0.063   (p = 0.005)
    #
    # So the screener carries real information and the EXIT rules eat it. The
    # exit-quality decomposition (exit_quality.py) then named the two leaks:
    #
    #     t1     387 trades capped at exactly +1.000R, 512R left inside 5 bars
    #     stop   559 stop-outs, price recovered 1.88R on average afterwards
    #     median capture ratio 0.16 -- we keep a sixth of the move we get
    #
    # An exit sweep on the same entries (`--exits --skill`) found:
    #
    #     shipped (stop 3%, hold 3)     mean R -0.029  PF 0.87  skill t 2.62
    #     stop 5%, hold 6               mean R +0.025  PF 1.48  skill t 5.17
    #                                   train t 3.69, test t 2.98, no sign flip
    #
    # That is the strongest result this system has produced. It is still NOT a
    # licence to edit these numbers, for two reasons:
    #
    #   1. The verdict is `train_only`. 48 configurations were searched, so the
    #      bar is the Harvey-Liu-Zhu 3.5 rather than 2.0, and the test half
    #      lands at 2.98 -- convincing at the conventional bar, short at the
    #      corrected one.
    #   2. It was measured at RSI<25 rising=N. We SHIP RSI<10 rising=Y, which
    #      produces ONE trade over the whole history. Porting exit parameters
    #      from a configuration we do not run is the same in-sample reasoning
    #      this apparatus exists to catch.
    #
    # Re-run once history has depth:
    #   docker exec -e PYTHONPATH=/app:/app/.venv/lib/python3.11/site-packages \
    #     python-engine python /app/tools/connors_backtest.py --exits --skill
    PENNY_CONNORS_T1_PCT:          float = 0.03
    PENNY_CONNORS_T2_PCT:          float = 0.06
    PENNY_CONNORS_STOP_PCT:        float = 0.03
    PENNY_CONNORS_MAX_HOLD_DAYS:   int   = 3
    # [TIER2-CONNORS-REFINEMENT 2026-06-25] Cumulative-RSI and absolute
    # floor gates (Q1+A2 from the brainstorm). Defaults preserve pre-fix
    # behaviour so we can A/B test by raising the cumulative-RSI days
    # later without redeploying code.
    #   RSI2_FLOOR: absolute minimum RSI(2) to enter. 1.0 disables
    #               (current behaviour: any RSI(2) < threshold works).
    #               Recommended value once validated: 5.0.
    #   CUMULATIVE_RSI_DAYS: minimum consecutive daily bars with
    #               RSI(2) < threshold before entry. 1 disables
    #               (current behaviour: any single-day trigger).
    #               Connors' original refinement uses 2; 1 means
    #               "the current day is enough".
    PENNY_CONNORS_RSI2_FLOOR:           float = 1.0
    PENNY_CONNORS_CUMULATIVE_RSI_DAYS:  int   = 1
    # Time-of-day gate: reject CNC signals after this many minutes past
    # market open (09:15 IST). Default 195 min = 12:30 IST. The Connors
    # mean-reversion signal fires best in the morning; signals after
    # lunch often mean-revert against the operator.
    PENNY_CONNORS_LAST_ENTRY_MIN:       int   = 195
    PENNY_CONNORS_TRAIL_ATR_MULT:  float = 2.0         # 2x ATR_1min trail after T1

    # Breakout strategy
    # [PENNY-AGGRESSIVE 2026-06-24] Relaxed from 3.0 -> 1.8 to allow more
    # entries. Web research (ORB / opening-range-breakout literature) shows
    # the 5-min ORB sweet spot is 1.5-2.0x median volume for momentum setups.
    # 3.0x was effectively blocking any setup that wasn't already in a
    # confirmed trend -- too tight for a penny subsystem that needs to
    # fire several trades per day to validate the edge.
    PENNY_BREAKOUT_VOL_MULT:       float = 1.8
    PENNY_BREAKOUT_TARGET_R:       float = 2.0
    PENNY_BREAKOUT_TIME_START:     int   = 10*60 + 30  # 10:30 IST in minutes
    PENNY_BREAKOUT_TIME_END:       int   = 14*60 + 30  # 14:30 IST in minutes
    # Broker-free evidence side-channel; never reaches PennyExecutor.
    PENNY_SHADOW_ENABLED:          bool  = True
    PENNY_BREAKOUT_TIME_EXIT:      int   = 15*60       # 15:00 IST
    # [TIER3-DAILY-ATTRIBUTION 2026-06-25] 15:30 IST = 30 min after the
    # 15:00 force-close fires. Gives time for the broker to confirm
    # all MIS positions are closed and the ledger to be updated.
    PENNY_DAILY_ATTRIBUTION_TIME: int = 15*60 + 30    # 15:30 IST
    PENNY_DAILY_ATTRIBUTION_HOUR: int = 15
    PENNY_DAILY_ATTRIBUTION_MIN:  int = 30
    # [TIER2-BREAKOUT-REFINEMENT 2026-06-25] VWAP-anchored breakout and
    # adaptive threshold. Both default to False to preserve current
    # behaviour (close > day_high + 0.3%); enable via config after A/B
    # validation.
    #   USE_VWAP: replace day_high anchor with VWAP. The breakout is then
    #             "close > VWAP + 0.3%" instead of "close > day_high + 0.3%".
    #             Volume-confirmed breakout is statistically more robust.
    #   ADAPTIVE_THRESHOLD: scale the 0.3% buffer by current_volatility /
    #             typical_volatility (ATR(20) / median_ATR(20,60min)). Calm
    #             ticker -> tighter threshold; volatile ticker -> wider.
    #             Per de Prado, Advances in Financial ML ch. 16.
    PENNY_BREAKOUT_USE_VWAP:            bool  = False
    PENNY_BREAKOUT_ADAPTIVE_THRESHOLD:  bool  = False
    # [FIX-PHASE3-AUDIT 2026-07-09] Time-of-day-adjusted relative volume.
    # Pre-fix the volume gate compared *running cumulative* volume against
    # the *full-day* 20-day median: at the 10:30 window open only ~20% of
    # the session has elapsed, so demanding 1.8x the full-day median means
    # demanding ~9x normal pace -- the gate produced 37,521 lifetime
    # rejects and starved the breakout leg. When True, the median is
    # scaled by the fraction of the session elapsed (09:15-15:30 =
    # 375 min), turning the gate into a true RVOL check: "is today's
    # volume running 1.8x its NORMAL PACE for this time of day?".
    # Default True because the unscaled comparison is considered a bug,
    # not a tuning choice. Set False to restore pre-fix behaviour.
    PENNY_BREAKOUT_RVOL_TIME_ADJUSTED:  bool  = True
    # [FIX-PHASE3-AUDIT 2026-07-09] RSI(14) overbought ceiling for the
    # breakout entry (was hardcoded 70; now a setting). A genuine 1-min
    # volume breakout often prints RSI(14) > 70, so 70 may reject real
    # signals -- BUT the penny_backtest_v2 daily-bar sweep (2026-04-01 to
    # 2026-07-08, config "phase3" vs "baseline") showed the extra trades
    # admitted between RSI 70 and 80 LOST ~Rs 13,500 net.
    # [ROADMAP-3.5 2026-07-12] That sweep ran on DAILY bars while this
    # gate reads 1-min RSI -- a different distribution entirely -- so
    # the Rs 13,500 figure is directional evidence, NOT validation.
    # This value is formally UNVALIDATED for the live engine (see the
    # derivation-invalid banner in penny_backtest_v2.py). 70 stays as
    # the proven-shipped conservative default until a 1-min rebuild
    # (60 days of Kite minute candles) justifies a change either way.
    # Operators can experiment via .env without a code change.
    PENNY_BREAKOUT_RSI_MAX:             float = 70.0
    # [GAP-2 ZERO-ACCEPT ALARM 2026-07-10] Consecutive evaluation days
    # with evaluations > 0 and accepts == 0 before the 15:45 IST
    # watchdog fires a Telegram alert with the reject-reason histogram
    # (F&O spec §9.2, backported to penny). BUG-1 ran for nine months
    # undetected; at the default of 2 it is caught on day two.
    PENNY_ZERO_ACCEPT_ALERT_DAYS:       int   = 2
    # Buffer pct applied as breakout margin (was hardcoded 0.3% in the
    # engine). Made a setting so adaptive mode can override it.
    PENNY_BREAKOUT_BUFFER_PCT:           float = 0.003
    # [TIER2-SECTOR-FILTER 2026-06-25] Sector-relative strength gate.
    # The filter is ALWAYS opt-in via the CSV; missing CSV == filter off.
    #   USE_SECTOR_FILTER: master toggle (default True so the CSV path is
    #     exercised; effective state is determined by CSV presence).
    #   TOP_LOSERS_PCT: how deep into "losers" we look. With 10% threshold,
    #     only sectors in the bottom decile intraday get blocked.
    #   ETF_CHANGE_THRESHOLD_PCT: minimum negative move to consider
    #     "weak" (default -1.5%). Sectors between -1.5% and severe are
    #     allowed through (preserves proactiveness).
    #   SECTORS_CSV_PATH: location of (symbol, sector) operator-curated
    #     CSV. Empty or missing file -> filter is effectively OFF.
    PENNY_USE_SECTOR_FILTER:               bool   = True
    PENNY_SECTOR_TOP_LOSERS_PCT:           float = 0.10
    PENNY_SECTOR_ETF_CHANGE_THRESHOLD_PCT: float = -0.015
    PENNY_SECTORS_CSV_PATH:                str   = "python-engine/data/penny_sectors.csv"
    # [ROADMAP-3.10 2026-07-12] Earnings/event no-trade windows. Same
    # curatorial-CSV philosophy as the sector filter: the CSV is the
    # operator's lever, missing CSV / missing ticker = ALLOW (never
    # kill proactiveness for lack of data). Format per line:
    #   ticker,event_date(YYYY-MM-DD),event_type
    # e.g. "SUZLON,2026-07-25,RESULTS". Entries are blocked from
    # EVENT_BLOCK_DAYS_BEFORE calendar days before the event through
    # EVENT_BLOCK_DAYS_AFTER days after it (default: 2 before, 0 after
    # -- results gaps hurt on the way IN; the day after, price has
    # already repriced). On /data so the operator can update it without
    # a rebuild.
    PENNY_USE_EVENT_FILTER:                bool   = True
    EVENT_CALENDAR_CSV_PATH:               str    = "/data/event_calendar.csv"
    EVENT_BLOCK_DAYS_BEFORE:               int    = 2
    EVENT_BLOCK_DAYS_AFTER:                int    = 0
    # [TIER3-INTERACTIVE-COMMANDS 2026-06-25] Path to the runtime
    # override JSON. Telegram /penny skip and /penny unskip write here;
    # penny_risk.is_disabled() reads here on every call. Tests override
    # this to a tmp path.
    PENNY_DISABLE_OVERRIDES_PATH:          str   = "python-engine/data/penny_disable_overrides.json"
    # [PENNY-TIME-STOP 2026-06-24] Soft time-stop: if entry fires but the
    # position is NOT in profit within PENNY_TIME_STOP_MIN minutes, cut at
    # market (modular exit path; spec §7.2 exit chain). Default 30 min --
    # the consensus from intraday-trading literature for breakout trades
    # in less-volatile setups. 0 disables.
    PENNY_TIME_STOP_MIN:           int   = 30
    # [PENNY-PREMARKET 2026-06-24] Pre-market universe Telegram digest
    # sent at HH:MM IST every weekday. Defaults to 07:50 (10 min before
    # penny_universe_refresh at PENNY_REFRESH_HOUR=8). 0 disables.
    PENNY_PREMARKET_REPORT_HOUR:   int   = 7
    PENNY_PREMARKET_REPORT_MIN:    int   = 50
    PENNY_PREMARKET_TOP_N:         int   = 10          # how many tickers to list in the body
    PENNY_MIS_SMART_EOD_TIME:      int   = 14*60 + 30  # 14:30 IST smart-EOD check
    PENNY_MIS_SMART_EOD_WITHIN_R:  float = 0.5
    PENNY_MIS_SMART_EOD_LOSS_MIN:  int   = 30

    # Risk + bankroll
    PENNY_LIVE_BANKROLL:           float = 2000.0
    # [CAPITAL-REALLOC 2026-07-26] 500 -> 100,000. Rs 500 was too small to be
    # informative: the penny breakout book has taken 0 trades in its lifetime, and
    # at Rs 500 many candidates cannot be sized to a single share, so the paper
    # book could not have told us whether the strategy works even if it did.
    # Standardised to the Rs 1,00,000 paper allocation used across all books.
    PENNY_PAPER_BANKROLL:          float = 100000.0
    PENNY_RISK_PCT_PR1:            float = 0.05
    PENNY_RISK_PCT_PR2:            float = 0.025
    # [TIER0-0.4 2026-07-14] PR3 THROTTLES, it no longer SHUTS DOWN.
    #
    # 0.0 turned a risk signal into a kill switch: in PR3 the sizer returns 0
    # shares, so every candidate is rejected with "regime PR3_HOT (no new
    # entries)" -- 93.4% of a typical day's rejects. Combined with the
    # running-max vol_rank ratchet (see penny_regime.update_vol_rank), the book
    # sat in PR3 permanently and took 0 trades in 349,297 lifetime evaluations.
    #
    # A 0-size regime is also UNFALSIFIABLE: it can never produce an accept, so
    # no watchdog, backtest or A/B can ever tell you whether PR3 was right. A hot
    # tape should be traded SMALL, not not-at-all.
    PENNY_RISK_PCT_PR3:            float = 0.01
    PENNY_DAILY_KILL_SWITCH_PCT:   float = 0.20
    PENNY_PER_STOCK_CAP:           float = 500.0
    PENNY_MAX_POSITIONS_TOTAL:     int   = 5
    PENNY_MAX_POSITIONS_CNC:       int   = 2
    PENNY_MAX_POSITIONS_MIS:       int   = 3
    PENNY_CIRCUIT_SKIP_DISTANCE:   float = 0.005      # 0.5% of band
    PENNY_CIRCUIT_FROM_HIGH_PCT:   float = 0.03       # 3% from day high
    # [AUDIT-FIX-2.5 2026-06-25] Penny heat-map "near SL" warn threshold.
    # When a position's current P&L % is within this distance (above)
    # of its stop_loss, the heatmap surfaces a WARN line. Previously
    # hardcoded to 1.0% in penny_heatmap.build_heatmap (operator-mandated
    # audit point). For a Rs 2,500 paper bankroll 1% may be too noisy;
    # for a Rs 50k+ account 1% may be too lax. Operator tunes here.
    PENNY_HEATMAP_WARN_PCT:        float = 0.01       # 1% from SL

    # Cadence + safety
    # Production scans routinely exceed 30s; scheduling faster only creates
    # max_instances skips and warning noise.  A 60s cadence preserves the
    # strategy's minute-level responsiveness without guaranteed overlap.
    PENNY_SCAN_INTERVAL_SEC:       int   = 60
    # Fail-safe code default: a missing/partial environment must never place
    # real Penny orders. Production explicitly opts in with
    # PENNY_LIVE_TRADING=true after its capital and broker checks are ready.
    PENNY_LIVE_TRADING:            bool  = False
    PENNY_DISABLE_TICKERS:         str   = ""         # comma-separated manual kill-switch
    PENNY_LOG_CSV_PATH:            str   = "/data/penny_signals.csv"
    PENNY_ENTRY_FILL_TIMEOUT_SEC:  float = 60.0      # max wait for LIMIT entry to fill before cancel
    PENNY_SL_M_MAX_ATTEMPTS:       int   = 2         # SL-M placement retries before unwind

    # [PENNY-EDGE 2026-07-01] Adaptive engine subsystem overrides.
    # The edge subsystem runs in parallel with the connors scanner.
    # TWO bankrolls run side-by-side:
    #   - PENNY_EDGE_PAPER_BANKROLL = 100_000 (paper, no real orders)
    #   - PENNY_EDGE_LIVE_BANKROLL = 1_000 (live, real orders at Kite)
    # The operator can disable either leg via PENNY_EDGE_DISABLE_PAPER /
    # PENNY_EDGE_DISABLE_LIVE. Live is fail-closed by default because the
    # current after-cost cohort is negative and below the promotion sample.
    PENNY_EDGE_DISABLE_PAPER:        bool  = False
    # Fail closed while EDGE_LIVE has no positive after-cost evidence.  Paper
    # evaluation remains enabled; live must be an explicit environment opt-in
    # after the promotion gates pass rather than an accidental code default.
    PENNY_EDGE_DISABLE_LIVE:         bool  = True
    PENNY_EDGE_PAPER_BANKROLL:       float = 100000.0  # 100k paper
    # Allocation is retained for paper-comparable reporting and any future
    # explicitly approved promotion; it is not permission to trade while the
    # live-disable switch remains true.
    PENNY_EDGE_LIVE_BANKROLL:        float = 1500.0    # 1.5k live
    PENNY_EDGE_MAX_POSITIONS:        int   = 3
    PENNY_EDGE_MIN_STRENGTH:         float = 0.45
    PENNY_EDGE_MAX_HOLD_DAYS:        int   = 3

    # Hourly report (spec §9.4)
    PENNY_HOURLY_REPORT_START_HOUR: int  = 10        # first hourly report at HH:00 IST (10 = 10:00)
    PENNY_HOURLY_REPORT_END_HOUR:   int  = 14        # last hourly report at HH:00 IST (14 = 14:00)
    PENNY_HOURLY_REPORT_WEBHOOK:   str   = ""        # optional webhook URL for delivery (Telegram/Slack)

    # ============================================================
    # F&O SUBSYSTEM (2026-07-10, spec docs/superpowers/specs/fno-module.md)
    # ============================================================
    # NIFTY-options intraday module. P1 = paper only; the live leg
    # refuses to arm until fno_go_live_check() returns []. Every
    # threshold below is spec §12 verbatim; all are guesses until the
    # paper log says otherwise -- do not hand-tune without data.

    # --- pools -------------------------------------------------------------
    # [ROADMAP-3.1 2026-07-12] Raised 100k -> 250k (operator decision):
    # at 100k the feasible premium band was <= ~Rs 106.67 while 0.55-delta
    # NIFTY weeklies typically print 120-250, so the §3 "self-regulation"
    # rejected essentially every candidate every day and the 60-trade
    # go-live bar was unreachable. At 250k the pool-derived cap is
    # ~Rs 266.67 -- it covers the typical band and STAYS the binding gate
    # (see the per-trade caps below, scaled to preserve that).
    # [CAPITAL-REALLOC 2026-07-26] 250,000 -> 100,000, standardising on the
    # Rs 1,00,000 paper allocation used by every other book. My call, per Uru.
    #
    # The old figure was not a pool, it was an alibi. At Rs 250,000 the promotion
    # ladder's drawdown budget was Rs 62,500 -- more than 7x the entire REAL
    # account (Rs 8,000 across all live books) -- so Rs 10,841 of paper losses,
    # from the worst-performing book in the system (1 win in 8), still reported as
    # "within budget". FNO_MAX_OPEN_PREMIUM_PCT=0.15 also licensed Rs 37,500 of
    # premium per trade, which is how 2026-07-24 ended with ~Rs 30k concentrated
    # on a single NIFTY strike.
    #
    # Rs 250,000 -- Uru's call, reaffirmed 2026-07-26 after the trade-off below
    # was raised. Recorded here so the reasoning is not re-litigated each audit.
    #
    # Why the size is not arbitrary: sizing requires
    #   min_viable_pool = premium * lot_size * FNO_STOP_PREMIUM_PCT / FNO_MAX_RISK_PCT
    #                   = premium * lot_size * 12.5
    # The real NIFTY premiums this book traded (Rs 151.85-165.10 on a 65 lot)
    # demand Rs 123,375-134,150 for ONE lot, so anything at or below ~Rs 135,000
    # rejects every trade `pool_below_min_viable` and silently switches the book
    # off. 250,000 clears that comfortably.
    #
    # The known cost: the promotion ladder's drawdown budget is
    # FNO_PAPER_BANKROLL * PROMOTION_MAX_DD_PCT = Rs 62,500, which is ~7.8x the
    # entire REAL account (Rs 8,000). That is how Rs 10,841 of losses from the
    # worst-performing book in the system (1 win in 8) still reported as "within
    # budget" through July.
    #
    # What stops that mattering is no longer the pool number: promotion_report's
    # structural-viability gate blocks F&O on the ground that one lot
    # (cheapest ever entered: Rs 5,967) costs more than the whole real account
    # (Rs 4,500 Nifty seed). That check reads REAL capital and is completely
    # independent of this figure, so the book cannot be promoted at any pool size
    # while the account is this small. Sizing realism here, promotion safety
    # there.
    FNO_PAPER_BANKROLL:        float = 250000.0
    FNO_LIVE_BANKROLL:         float = 0.0        # not armed (spec §0)
    FNO_LIVE_TRADING:          bool  = False      # master switch
    FNO_DISABLE_PAPER:         bool  = False
    FNO_DISABLE_LIVE:          bool  = True
    # Broker-free opening-range research sidecar. Disabling it removes all
    # local CPU/SQLite work without changing any F&O strategy or arming flag.
    FNO_SHADOW_ENABLED:        bool  = True
    # Independent kill-switch for the Phase-2 defined-risk paper book. It rides
    # the same tick as the single-leg engine but is newer/experimental, so it
    # gets its own lever: flip this True to silence ONLY the DR book (e.g. if it
    # misbehaves) without also disabling the proven single-leg paper book, which
    # FNO_DISABLE_PAPER would do. Default False = DR book active (current behaviour).
    FNO_DR_DISABLE_PAPER:      bool  = False

    # --- universe ----------------------------------------------------------
    FNO_UNDERLYING:            str   = "NIFTY"    # NIFTY only in P1
    FNO_STRIKE_WINDOW:         int   = 5          # ATM +/- N strikes to snapshot
    FNO_TARGET_DELTA:          float = 0.55       # ATM / 1-strike ITM, never OTM

    # --- sizing / risk -----------------------------------------------------
    FNO_MAX_RISK_PCT:          float = 0.02       # per trade, of pool
    FNO_STOP_PREMIUM_PCT:      float = 0.25       # premium backstop (-25%)
    FNO_MAX_OPEN_PREMIUM_PCT:  float = 0.15       # total committed premium
    FNO_MAX_CONCURRENT:        int   = 2          # implied by the premium cap
    FNO_MAX_TRADES_PER_DAY:    int   = 3
    # FNO_MAX_LOSS_PER_TRADE caps the STOP-BASED risk (premium * qty *
    # stop_pct), consistent with §3's risk_per_lot arithmetic.
    # [SPEC-DEVIATION 2026-07-10] The spec draft enforced this same
    # Rs 2,500 against structural max_loss(), which for a long option is
    # the FULL premium paid (~Rs 7,500/lot at Rs 100) -- that gate is
    # mathematically unsatisfiable for any affordable contract, i.e.
    # exactly the BUG-1 dead-gate class §9.1 exists to catch (the
    # witness test caught it at design time). Structural max loss gets
    # its own, looser cap below: it bounds the frozen-engine catastrophe
    # case, not the working risk.
    # [ROADMAP-3.1 2026-07-12] Both rupee caps scaled x2.5 with the pool
    # (they were 2.5% and 12% of the old 100k pool). Left at their old
    # absolute values they would have re-created the deadlock the pool
    # raise fixes: 2500 binds at premium <= 133.33, BELOW the typical
    # weekly band. Scaled, the binding gate is back to the pool-derived
    # min_viable_pool (~Rs 266.67) -- the spec §3 volatility filter.
    FNO_MAX_LOSS_PER_TRADE:    float = 6250.0
    FNO_MAX_STRUCTURAL_LOSS_PER_TRADE: float = 30000.0
    FNO_MAX_LOTS:              int   = 2

    # --- kill switches -----------------------------------------------------
    # Weekly + monthly exist because options bleed slowly enough to walk
    # under a daily limit every day for a month (spec §7.6).
    FNO_DAILY_KILL_PCT:        float = 0.06
    FNO_WEEKLY_KILL_PCT:       float = 0.12
    FNO_MONTHLY_KILL_PCT:      float = 0.20
    FNO_MAX_CONSECUTIVE_LOSSES: int  = 6

    # --- microstructure ----------------------------------------------------
    FNO_MIN_OI:                int   = 5000
    FNO_MIN_VOL:               int   = 1000
    FNO_MAX_SPREAD_PCT:        float = 0.015
    FNO_MAX_QUOTE_AGE_SEC:     int   = 120
    FNO_IV_SANITY_MIN:         float = 0.05
    FNO_IV_SANITY_MAX:         float = 1.00
    FNO_MAX_CHAIN_AGE_SEC:     int   = 60         # chain snapshot staleness kill (§7.6)

    # --- session -----------------------------------------------------------
    FNO_ENTRY_START_MIN:       int   = 9*60 + 45  # 09:45 IST
    FNO_ENTRY_END_MIN:         int   = 14*60 + 45 # 14:45 IST
    FNO_HARD_FLAT_MIN:         int   = 15*60 + 10 # 15:10 IST, unconditional
    FNO_EXPIRY_DAY_ENTRIES:    bool  = False      # no entries on expiry day in P1
    FNO_OR_MINUTES:            int   = 30         # opening range window

    # --- strategy (FNO-MOM) --------------------------------------------------
    FNO_OR_BUFFER_ATR:         float = 0.25
    FNO_STOP_ATR_MULT:         float = 1.5
    # [NAKED-LEG-EXPECTANCY 2026-07-31] Raised 1.5 -> 1.8. The R target is set
    # in UNDERLYING points, but the position is a long option: the premium
    # backstop (FNO_STOP_PREMIUM_PCT) truncates the downside independently, and
    # the bid-ask is paid twice, so a 1.5R geometry in the underlying arrived as
    # ~0.78:1 in rupees on 2026-07-30. Widening the target restores the margin
    # the spread and the backstop take out. Record that motivated it: 12 naked
    # legs since 2026-07-16, 2 winners, -Rs 15,474.
    FNO_TARGET_R:              float = 1.8
    # Minimum reward:risk, measured on the PREMIUM after round-trip spread, that
    # a naked long option must clear. Below this the trade needs a win rate the
    # book has never shown (it runs ~17%). The defined-risk spreads are not
    # subject to this gate -- they carry their own max_profit/max_loss geometry
    # (~1.7:1) and have run roughly flat over the same period, which is why the
    # gate is deliberately placed on the single-leg path only.
    FNO_MIN_REWARD_RISK:       float = 1.30
    FNO_TRAIL_ATR_MULT:        float = 1.0
    FNO_TIME_STOP_MIN:         int   = 45
    FNO_TIME_STOP_MIN_R:       float = 0.5
    # [TIME-STOP-PREMIUM 2026-08-04] The time stop measures UNDERLYING points
    # but the book is paid in PREMIUM, and delta separates the two. With this
    # True the clock only cuts a trade that is going nowhere on the underlying
    # AND not in profit on premium. See fno_orchestrator for the record: 8
    # time-stop exits, -Rs 7,010, two of them profitable when cut.
    FNO_TIME_STOP_RESPECTS_PREMIUM: bool = True
    FNO_MIN_RVOL:              float = 1.2
    FNO_EMA_FAST:              int   = 21
    FNO_EMA_SLOW:              int   = 50
    FNO_ATR_LEN:               int   = 14
    FNO_RVOL_LOOKBACK_DAYS:    int   = 10         # per-slot RVOL baseline window

    # --- execution ---------------------------------------------------------
    FNO_FILL_TIMEOUT_SEC:      int   = 30
    FNO_TICK_SIZE:             float = 0.05
    # Full-chain evaluation can exceed one minute.  Leave headroom so the next
    # tick is useful work rather than an APScheduler max_instances warning.
    FNO_SCAN_INTERVAL_SEC:     int   = 90

    # --- backtest model params ([ROADMAP-3.11 2026-07-12]) ------------------
    # fno_backtest.py replays evaluate_fno_mom on REAL futures bars but
    # must MODEL the option leg (historical NIFTY option chains are not
    # available through Kite): premiums are Black-76 at a constant IV
    # with a symmetric spread. These three constants parameterise that
    # model -- sweep FNO_BT_IV (e.g. 0.10/0.12/0.15) to see how
    # conclusions move with the vol assumption.
    FNO_BT_IV:                 float = 0.12       # flat IV for the synthetic chain
    FNO_BT_SPREAD_PCT:         float = 0.006      # full bid-ask spread / mid
    FNO_BT_STRIKE_STEP:        float = 50.0       # NIFTY strike ladder
    # Weekly expiry weekday, Mon=0. NIFTY weeklies expire Tuesday since
    # 2025 (VERIFY-3 in fno_instruments: the day has changed several
    # times -- confirm before trusting a long historical run).
    FNO_BT_EXPIRY_WEEKDAY:     int   = 1

    # --- cost model (fno_costs.py; VERIFY-4/VERIFY-5 resolved 2026-07-10) ---
    # There is deliberately NO FNO_BROKERAGE_BYPASS (spec §10.2): cost is a
    # first-order term for options and hiding it would make paper fills lie.
    # Current schedule: option SELL-premium STT is 0.15% from 2026-04-01;
    # NSE transaction charge is 0.03553% from 2026-03-01. Exercised ITM
    # options also use 0.15% of intrinsic, moot here because P1 exits intraday.
    FNO_BROKERAGE_FLAT:        float = 20.0       # Rs 20 per executed order
    FNO_STT_SELL_PCT:          float = 0.0015     # 0.15% sell premium
    FNO_EXCHANGE_TXN_PCT:      float = 0.0003553  # NSE 0.03553%, both sides
    FNO_SEBI_PCT:              float = 0.000001   # Rs 10/crore, both sides
    FNO_STAMP_DUTY_PCT:        float = 0.00003    # 0.003% buy side
    FNO_IPFT_PCT:              float = 0.000000001 # NSE IPFT Rs 0.01/crore, both sides
    FNO_GST_PCT:               float = 0.18       # on brokerage + txn + sebi

    # --- observability -----------------------------------------------------
    FNO_SIGNAL_LOG_PATH:       str   = "/data/fno_signals.csv"
    FNO_ZERO_ACCEPT_ALERT_DAYS: int  = 2
    FNO_INSTRUMENTS_JSON_PATH: str   = "/data/fno_nifty_instruments.json"

    # --- go-live gate (§11; fixed NOW, before there is an equity curve) -----
    FNO_GO_LIVE_MIN_TRADING_DAYS: int   = 40
    FNO_GO_LIVE_MIN_TRADES:       int   = 60
    FNO_GO_LIVE_MIN_PROFIT_FACTOR: float = 1.2
    FNO_GO_LIVE_LIVENESS_DAYS:    int   = 30
    # Operator attestation that the liveness heartbeat has logged 30
    # consecutive clean days (no gap > 5 min). Checked by runbook grep
    # (ops rule 62 recipe); flipped in .env only after the grep passes.
    FNO_LIVENESS_30D_CLEAN:       bool  = False

    # --- multi-underlying analytics ([PARTNER-TIPS 2026-07-18]) -------------
    # Read-only chain analytics + ORB signal-gen for the partner tips bot.
    # The TRADING path stays FNO_UNDERLYING=NIFTY only; these settings feed
    # fno_underlyings/fno_signal_scan/fno_analytics and nothing else.
    FNO_ANALYTICS_UNDERLYINGS: str   = "NIFTY,BANKNIFTY,SENSEX"
    # Wide window for PCR / max pain / OI walls. The trading path keeps the
    # narrow FNO_STRIKE_WINDOW; +/-15 x CE/PE = 62 tokens + future per
    # underlying, still one batched /quote well inside Kite's ~500 cap.
    FNO_ANALYTICS_STRIKE_WINDOW: int = 15
    FNO_ANALYTICS_INTERVAL_SEC: int  = 300
    FNO_OI_RETENTION_DAYS:      int  = 7          # disk at 86% -- purge is load-bearing

    # --- research evidence archive (data plan D1-D7, 2026-09-08) ----------
    # This is deliberately outside the operational SQLite database.  It is
    # evidence collection only: it never places orders, sends partner advice,
    # or changes a strategy qualification.  The default path is on the
    # existing persistent /data volume, so it survives a container restart.
    RESEARCH_ARCHIVE_ENABLED: bool = True
    RESEARCH_ARCHIVE_PATH: str = "/data/research"
    RESEARCH_ARCHIVE_UNDERLYINGS: str = "NIFTY,SENSEX"
    # A failed archive must be conspicuous before the short-retention cache is
    # purged.  It does not silently delete unpreserved research input.
    RESEARCH_ARCHIVE_REQUIRED_BEFORE_FNO_PURGE: bool = True
    # REST full quotes are an explicitly lower-frequency fallback.  A future
    # supported WebSocket producer can call the same append API with mode
    # KITE_WS_FULL; neither source is an order or delivery consumer.
    RESEARCH_QUOTE_COLLECTION_ENABLED: bool = True
    RESEARCH_QUOTE_INTERVAL_SEC: int = 60
    # [WORKFLOW-C.F2 2026-09-16] Soft cap on per-tick runtime.
    # When the tick exceeds this, it returns early with whatever
    # it has collected so far (partial_collected) and the audit
    # log surfaces ``runtime_capped=True`` + ``elapsed_sec``. The
    # default 80% of the trigger interval leaves headroom for the
    # scheduler overhead + archive_journal write at the end.
    # Per the audit: avg runtime = 14s, max = 114s on a 60s
    # trigger. The cap brings tail latency under control without
    # changing the trigger interval (operators want fresh data
    # every 60s -- "earn good and fast profit").
    RESEARCH_QUOTE_RUNTIME_CAP_SEC: float = 48.0
    RESEARCH_QUOTE_STRIKE_WINDOW: int = 5
    # Exact legs selected by a research/advisory decision take priority over
    # the rolling ATM discovery universe. A bounded cap makes saturation an
    # explicit evidence gap instead of silently dropping old contracts.
    RESEARCH_ACTIVE_LEG_MAX_TOKENS: int = 120
    RESEARCH_QUOTE_MAX_QUEUE: int = 2_000
    # Awaiting archive work must not hold advisory construction indefinitely.
    # The guarded writer continues to serialize disk access; a timed-out write
    # is reported as outcome-unknown and never retried as if it certainly failed.
    RESEARCH_CAPTURE_WAIT_TIMEOUT_SEC: float = 2.0
    RESEARCH_RAW_RETENTION_DAYS: int = 7
    RESEARCH_COMPRESSED_RETENTION_DAYS: int = 90
    RESEARCH_RESERVED_FREE_BYTES: int = 1_073_741_824  # 1 GiB operational floor
    RESEARCH_SESSION_MAX_BYTES: int = 268_435_456  # 256 MiB hard passive-collection budget

    # ============================================================
    # PARTNER TIPS BOT ([PARTNER-TIPS 2026-07-18])
    # Outbound-only second Telegram bot (own token + chat) sending
    # information/inferences to the operator's trading partner. NO
    # execution surface, NO fallback into the operator chat (a
    # misrouted partner message is worse than a dropped one).
    # Explicit disable gate: with PARTNER_BOT_ENABLED=false every
    # partner job returns immediately -- zero Kite calls, zero sends.
    # ============================================================
    PARTNER_BOT_ENABLED:        bool  = True
    PARTNER_TELEGRAM_BOT_TOKEN: str   = ""
    PARTNER_TELEGRAM_CHAT_ID:   str   = ""
    PARTNER_MORNING_BRIEF_HOUR: int   = 9
    PARTNER_MORNING_BRIEF_MIN:  int   = 50
    PARTNER_EOD_HOUR:           int   = 15
    PARTNER_EOD_MIN:            int   = 40
    # IV/RV ratio bands for the premium rich/cheap read (option-buyer lens).
    PARTNER_IV_RICH_RATIO:      float = 1.25
    PARTNER_IV_CHEAP_RATIO:     float = 0.80
    # Intraday event thresholds + per-(kind,underlying) message throttle.
    PARTNER_PCR_ALERT_DELTA:    float = 0.15
    PARTNER_IV_MOVE_ALERT_PCT:  float = 0.10      # relative ATM-IV intraday move
    PARTNER_EVENT_MIN_GAP_MIN:  int   = 30
    # [PARTNER-ENRICH 2026-07-19] Tier-1/2 enrichment knobs.
    # Sizing line: fraction of capital a buyer should risk per trade.
    PARTNER_SIZING_RISK_PCT:    float = 0.02
    # Rolling track-record window + minimum sample before quoting one
    # (a 2-signal "record" is noise dressed as evidence).
    PARTNER_TRACK_LOOKBACK_DAYS: int  = 30
    PARTNER_TRACK_MIN_N:        int   = 5
    # Signal rows outlive OI snapshots (FNO_OI_RETENTION_DAYS): the track
    # record needs weeks of history, OI forensics only needs days. A few
    # signal rows/day is negligible disk.
    PARTNER_SIGNAL_RETENTION_DAYS: int = 60
    # OI wall build/unwind: report when the wall strike's OI moved this
    # fraction vs the open baseline (and again on each further move of
    # the same size).
    PARTNER_WALL_DELTA_PCT:     float = 0.15

    # Hedge-first partner advisory. This is deliberately independent from
    # PARTNER_BOT_ENABLED. It is enabled for Phase 1, while reconciled holdings
    # and fresh quote gates still fail closed before any message can be sent.
    # Personalised portfolio monitoring is not part of the manual-advisory
    # rollout.  It stays off unless a separately reconciled holdings service
    # is deliberately commissioned.
    PARTNER_HEDGE_ENABLED:              bool  = False
    PARTNER_HEDGE_PROTECTIVE_PUT:       bool  = True
    # Automatic collars stay off in Phase 1: a short call is only safe when
    # the exact deliverable holding and option contract are reconciled.
    PARTNER_HEDGE_COLLAR:               bool  = False
    PARTNER_HEDGE_FUTURES:              bool  = True
    PARTNER_HEDGE_TARGET_RATIO:         float = 0.50
    PARTNER_HEDGE_PUT_DELTA:            float = 0.20
    PARTNER_HEDGE_CALL_DELTA:           float = 0.20
    PARTNER_HEDGE_MAX_SPREAD_PCT:       float = 0.15
    PARTNER_HEDGE_MAX_QUOTE_AGE_SEC:    int   = 120
    PARTNER_HEDGE_MIN_OI:               int   = 1
    PARTNER_HEDGE_MIN_VOLUME:           int   = 1
    PARTNER_HEDGE_MIN_DTE:              int   = 21
    PARTNER_HEDGE_MAX_DTE:              int   = 60
    PARTNER_HEDGE_POSITION_MAX_AGE_MIN: int   = 5
    PARTNER_HEDGE_DELIVERABLE_MAX_AGE_MIN: int = 5
    PARTNER_HEDGE_VIX_MAX_AGE_MIN:      int   = 15
    # An approved source-neutral adapter returns complete, timestamped partner
    # snapshots. Empty by default: no account/source is guessed in Dev or Prod.
    PARTNER_HEDGE_INPUT_REFRESH_ENABLED: bool = False
    PARTNER_HEDGE_INPUT_ADAPTER_URL:     str   = ""
    PARTNER_HEDGE_INPUT_REFRESH_TIMEOUT_SEC: float = 5.0
    PARTNER_HEDGE_INPUT_ADAPTER_TOKEN:   str   = ""
    PARTNER_HEDGE_INPUT_EXPECTED_SOURCE: str   = ""
    PARTNER_HEDGE_INPUT_EXPECTED_ACCOUNT_ID: str = ""
    PARTNER_HEDGE_INPUT_MAX_AGE_MIN:     int   = 10
    PARTNER_HEDGE_INPUT_MAX_FUTURE_SKEW_SEC: int = 60
    # Bounded transport attempts prevent a persistent Telegram rejection from
    # becoming an unbounded delivery loop. A new materially different review
    # receives a different dedup key and is evaluated independently.
    PARTNER_HEDGE_DELIVERY_MAX_ATTEMPTS: int  = 3
    # When the hedge pipeline is deliberately enabled, retire the legacy
    # directional/noisy surfaces instead of mixing two different mandates.
    PARTNER_HEDGE_SUPPRESS_DIRECTIONAL: bool  = True
    PARTNER_HEDGE_SUPPRESS_ANALYTICS:   bool  = True
    PARTNER_HEDGE_SUPPRESS_LEGACY_BRIEF: bool = True
    PARTNER_HEDGE_SUPPRESS_LEGACY_EOD:   bool = True

    # Scoped manual-trader advisory for NIFTY 50 (NSE) and SENSEX (BSE).
    # Owner-approved delivery remains advisory-only: the hardened transport
    # ledger can send a validated card, but no partner order consumer exists.
    # Owner-approved advisory rollout: messages remain manual decision
    # support only; no code path here can place a partner order.
    PARTNER_MANUAL_ADVISORY_ENABLED: bool = True
    PARTNER_MANUAL_ADVISORY_SHADOW_ENABLED: bool = True
    PARTNER_MANUAL_ADVISORY_DELIVERY_ENABLED: bool = True
    # [WORKFLOW-ITEMS-5/6/9 2026-09-20] Operations freshness thresholds.
    #
    # The audit requires the operator to be alerted BEFORE useful
    # session data is lost. The freshness diagnostic surfaces
    # login / input / candidate / ledger ages against these
    # thresholds. Each channel has its own bound because
    # Kite logins are daily while public inputs are minutes.
    OPS_FRESHNESS_MAX_LOGIN_AGE_SECONDS: int = 86_400  # 24 hours
    OPS_FRESHNESS_MAX_INPUT_AGE_SECONDS: int = 1_800  # 30 minutes
    OPS_FRESHNESS_MAX_LEDGER_AGE_SECONDS: int = 300  # 5 minutes
    PARTNER_MANUAL_ADVISORY_DAILY_CAP: int = 2
    # Deprecated compatibility setting: false cannot bypass artifact or delivery
    # evidence verification. Kept to avoid breaking existing environment files.
    PARTNER_VERIFY_RESEARCH_ARTIFACTS: bool = True
    # Curated immutable authorization packages inside the persistent data mount.
    PARTNER_ARTIFACT_ROOT: str = "/data/research/qualifications"
    # Material invalidation/target updates use a separate small budget so a
    # morning entry cannot silence a later risk-relevant follow-up.
    PARTNER_MANUAL_ADVISORY_UPDATE_DAILY_CAP: int = 4
    PARTNER_MANUAL_ADVISORY_MAX_QUOTE_AGE_SEC: int = 30
    PARTNER_MANUAL_ADVISORY_MAX_SPREAD_PCT: float = 0.15
    PARTNER_MANUAL_ADVISORY_MIN_OI: int = 1
    PARTNER_MANUAL_ADVISORY_MIN_VOLUME: int = 1
    PARTNER_MANUAL_ADVISORY_MIN_DEPTH_UNITS: int = 1
    PARTNER_MANUAL_ADVISORY_QUOTE_TTL_SEC: int = 30
    # Intraday policy times in IST. New entries end before management/exit
    # observation so a late alert cannot become an overnight recommendation.
    PARTNER_MANUAL_ADVISORY_ENTRY_START_MINUTE: int = 9 * 60 + 45
    PARTNER_MANUAL_ADVISORY_ENTRY_END_MINUTE: int = 14 * 60 + 45
    PARTNER_MANUAL_ADVISORY_EXIT_REMINDER_MINUTE: int = 15 * 60 + 10
    PARTNER_MANUAL_ADVISORY_MANAGEMENT_END_MINUTE: int = 15 * 60 + 15
    # Management uses the just-closed futures bar when no new ORB direction
    # exists.  This bounds public-condition updates without requiring a full
    # option-chain fetch on every quiet entry tick.
    PARTNER_MANUAL_ADVISORY_MANAGEMENT_MAX_OBSERVATION_AGE_SEC: int = 360

    # Advanced partner-facing phases require current readiness evidence. They
    # start disabled; Phase 1 protection and status messages remain available.
    PARTNER_HEDGE_PHASE2_ENABLED:         bool  = False
    PARTNER_HEDGE_PHASE2_SHADOW_ENABLED:  bool  = True
    PARTNER_HEDGE_COVERED_CALL:           bool  = True
    PARTNER_HEDGE_BULL_PUT_SPREAD:        bool  = True
    PARTNER_HEDGE_BEAR_CALL_SPREAD:       bool  = True
    PARTNER_HEDGE_IRON_CONDOR:            bool  = True
    PARTNER_HEDGE_DELTA_REBALANCE:        bool  = True
    PARTNER_HEDGE_COVERED_CALL_DELTA:     float = 0.30
    PARTNER_HEDGE_SPREAD_SHORT_DELTA:     float = 0.30
    PARTNER_HEDGE_CONDOR_SHORT_DELTA:     float = 0.16
    PARTNER_HEDGE_SPREAD_WIDTH:           float = 200.0
    PARTNER_HEDGE_CONDOR_WING_WIDTH:      float = 200.0
    PARTNER_HEDGE_MIN_CREDIT_POINTS:      float = 10.0
    PARTNER_HEDGE_COVERED_CALL_IV_RANK:   float = 0.45
    PARTNER_HEDGE_SPREAD_IV_RANK:         float = 0.40
    PARTNER_HEDGE_CONDOR_IV_RANK:         float = 0.50
    PARTNER_HEDGE_RANGE_MAX_MOVE_PCT:      float = 0.003
    PARTNER_HEDGE_IV_HISTORY_DAYS:        int   = 252
    PARTNER_HEDGE_IV_MIN_OBSERVATIONS:    int   = 20
    PARTNER_HEDGE_PHASE2_MIN_DTE:         int   = 7
    PARTNER_HEDGE_PHASE2_MAX_DTE:         int   = 45
    PARTNER_HEDGE_DELTA_THRESHOLD_LOTS:   float = 0.15
    PARTNER_HEDGE_PHASE2_PREMIUM_GAP_MIN: int   = 240
    PARTNER_HEDGE_PHASE2_DELTA_GAP_MIN:   int   = 30
    PARTNER_HEDGE_PHASE2_PREMIUM_DAILY_CAP: int = 4
    PARTNER_HEDGE_PHASE2_DELTA_DAILY_CAP: int   = 8

    # Phase 3 also remains staged until its independent evidence gate passes.
    PARTNER_HEDGE_PHASE3_ENABLED:          bool  = False
    PARTNER_HEDGE_PHASE3_SHADOW_ENABLED:   bool  = True
    PARTNER_HEDGE_GAMMA_ALERT:             bool  = True
    PARTNER_HEDGE_LONG_STRADDLE:           bool  = True
    PARTNER_HEDGE_LONG_STRANGLE:           bool  = True
    PARTNER_HEDGE_CALENDAR_SPREAD:         bool  = True
    PARTNER_HEDGE_IRON_BUTTERFLY:          bool  = True
    PARTNER_HEDGE_RATIO_SPREAD:            bool  = False
    PARTNER_HEDGE_EARNINGS_EVENT:          bool  = False
    PARTNER_HEDGE_PORTFOLIO_OVERLAY:       bool  = True
    PARTNER_HEDGE_OVERLAY_LOOKBACK_ROWS:   int   = 51
    PARTNER_HEDGE_OVERLAY_CORRELATION_MIN: float = 0.75
    PARTNER_HEDGE_OVERLAY_BREADTH_MAX:     float = 0.25
    PARTNER_HEDGE_OVERLAY_DRAWDOWN_MIN:    float = 0.06
    PARTNER_HEDGE_PHASE3_LOW_IV_RANK:      float = 0.30
    PARTNER_HEDGE_BUTTERFLY_IV_RANK:       float = 0.55
    PARTNER_HEDGE_BUTTERFLY_MAX_DTE:       int   = 3
    PARTNER_HEDGE_CALENDAR_MIN_IV_GAP:     float = 0.035
    PARTNER_HEDGE_PHASE3_WING_WIDTH:       float = 200.0
    PARTNER_HEDGE_PHASE3_LONG_DELTA:       float = 0.25
    PARTNER_HEDGE_PHASE3_GAMMA_THRESHOLD:  float = 1000.0
    PARTNER_HEDGE_PHASE3_GAP_MIN:          int   = 240
    PARTNER_HEDGE_PHASE3_DAILY_CAP:        int   = 4

    # ============================================================
    # REGIME ENGINE -- VIX-Free Volatility Detection
    # Replaces India VIX with ATR Compression + Realized Volatility
    # ============================================================

    # ATR Compression Ratio -- replaces VIX as primary volatility driver
    # rv_ratio = ATR_14 / ATR_14_SMA_200
    # rv_ratio <= 0.70 = compressed (calm baseline -> score 100)
    # rv_ratio  1.00  = normal                       -> score ~50
    # rv_ratio >= 1.20 = expansion (elevated stress) -> score ~20
    RV_ATR_COMPRESS_THRESHOLD: float = 0.70   # compressed = calm baseline
    RV_ATR_NORMAL:              float = 0.95   # mid-point of normal range
    RV_ATR_EXPANSION:           float = 1.20   # expansion threshold
    RV_ATR_CB_THRESHOLD:        float = 1.50   # circuit breaker -- forces R3
    RV_ATR_SPAN:                float = 0.50   # (RV_ATR_EXPANSION - RV_ATR_COMPRESS_THRESHOLD)
    RV_ATR_SCORE_SCALE:         float = 200.0  # scale factor for linear mapping

    # Realized Volatility -- secondary volatility signal (20-day, annualized)
    # rv_12% -> score 100; rv_20% -> score 60; rv_32% -> score 0
    RV_NORMAL_ANNUAL:  float = 0.18   # 18% annualized = normal vol baseline
    RV_CRISIS_ANNUAL:   float = 0.28   # 28% annualized = crisis threshold
    RV_SPAN:           float = 0.16   # (RV_CRISIS_ANNUAL - RV_NORMAL_ANNUAL)
    RV_SCORE_SCALE:    float = 625.0  # scale factor: 100 / 0.16

    # Volatility component weights (must sum to 1.0)
    RV_ATR_WEIGHT: float = 0.60   # ATR compression = primary (60%)
    RV_RV_WEIGHT:  float = 0.40   # realized vol   = secondary (40%)

    # Circuit breaker override
    ATR_CB_THRESHOLD: float = 1.50   # rv_ratio > 1.50 -> force REGIME_3_CRISIS

    # Nifty/BankNifty ratio -- breadth proxy (replaces weak EMA50-proxy)
    # nb_ratio percentile below 0.30 -> weak breadth -> x0.8 penalty
    NB_RATIO_LO_PCT:    float = 0.30   # breadth penalty threshold
    NB_RATIO_WINDOW:    int   = 60     # lookback window for percentile rank

    # VIX parameters -- DECOMMISSIONED (kept for backward compat with tests)
    # India VIX unavailable via Kite -> replaced by ATR compression + RV
    REGIME_VIX_BOUNDARY_12: float = 18.0
    REGIME_VIX_BOUNDARY_23: float = 25.0
    VIX_CB_THRESHOLD:        float = 40.0   # DEPRECATED -- use ATR_CB_THRESHOLD

    # RSI Percentile thresholds (bottom % of 6-month rolling range)
    RSI_PERCENTILE_REGIME1: float = 20.0   # Regime 1: bottom 20%
    RSI_PERCENTILE_REGIME2: float = 15.0   # Regime 2: bottom 15% (tighter)

    # Volume Z-score thresholds
    VOL_ZSCORE_REGIME1: float = 1.5       # Regime 1: 1.5 std devs above mean
    VOL_ZSCORE_REGIME2: float = 2.0       # Regime 2: 2.0 std devs
    VOL_ZSCORE_REGIME3: float = 2.5       # Regime 3: 2.5 std devs

    # Position sizing by regime (% of bankroll per trade)
    RISK_PCT_REGIME1: float = 0.10        # 10% -- normal market
    RISK_PCT_REGIME2: float = 0.07        # 7%  -- elevated uncertainty
    RISK_PCT_REGIME3: float = 0.05        # 5%  -- crisis

    # Stop loss by regime (ATR multipliers)
    STOP_ATR_REGIME1: float = 1.5        # 1.5x ATR
    STOP_ATR_REGIME2: float = 2.0        # 2.0x ATR
    STOP_ATR_REGIME3: float = 2.0        # 2.0x ATR

    # Stop loss by regime (% of close below price -- for pct_stop branch)
    STOP_PCT_REGIME1: float = 0.05      # 5% stop
    STOP_PCT_REGIME2: float = 0.05      # 5% stop
    STOP_PCT_REGIME3: float = 0.08      # 8% stop (wider in crisis)

    # [ROADMAP-3.4 2026-07-12] Overnight gap-risk sizing multiplier.
    # The swing chandelier stop is close-based and only evaluated EOD, so
    # a gap-down realizes MORE than the stop distance -- historically an
    # unbounded hole in the risk math (the stop distance was treated as
    # the true worst case, which overnight it never was). Sizing now
    # assumes the true risk per share is stop_distance x this multiplier
    # (2.0 = "a gap can travel twice the stop distance before the EOD
    # exit fires"), which halves share counts at the default. Stop level
    # and R-multiple targets are unchanged -- only the share count is.
    # Set to 1.0 in .env to restore pre-3.4 sizing.
    SWING_GAP_RISK_MULT: float = 2.0

    # Target structure (R-multiples)
    TARGET1_R: float = 1.5                # T1 = 1.5R (all regimes)
    TARGET2_R_REGIME1: float = 4.5        # T2 = 4.5R (Regime 1, was 3.0 -- let winners run)
    TARGET2_R_REGIME2: float = 3.0        # T2 = 3.0R (Regime 2)
    TARGET2_R_REGIME3: float = 1.0        # T2 = 1.0R (Regime 3 -- no T2, exit at T1)

    # Hard ceiling on Regime 1 positions -- never hold past this R-multiple.
    # Rationale: the trailing Chandelier can technically let a runaway trend
    # sit forever; this is a safety valve to ensure we bank the gains.
    HARD_CAP_R_REGIME1: float = 5.0       # Absolute ceiling in Regime 1

    # Partial exit at T1 (fraction of shares to exit)
    PARTIAL_EXIT_T1_PCT: float = 0.50    # Exit 50% at T1

    # Chandelier trailing stop -- legacy single multiplier (kept for backward compat)
    CHANDELIER_ATR_MULT: float = 3.0      # Highest close since entry - (3 * ATR)

    # Regime-aware Chandelier multipliers (override the legacy single setting)
    # Regime 1 (calm): wider trail (3.5x) -- gives mid-cap trends room to breathe
    # Regime 2 (elevated): default trail (3.0x) -- unchanged
    # Regime 3 (crisis): tighter trail (2.5x) -- cut losses fast
    CHANDELIER_ATR_REGIME1_MULT: float = 3.5
    CHANDELIER_ATR_REGIME2_MULT: float = 3.0
    CHANDELIER_ATR_REGIME3_MULT: float = 2.5

    # Regime transition guards
    REGIME_TRANSITION_SCANS: int = 2      # Score must hold for 2 consecutive scans
    REGIME_HYSTERESIS: float = 5.0       # Must cross threshold by 5 points to transition

    # RS vs Nifty filter (Regime 3 only)
    RS_VS_NIFTY_THRESHOLD: float = 0.05  # 5% outperformance required

    # Drawdown governor (post-crisis recovery)
    DRAWDOWN_RECOVERY_TRADES: int = 5    # Reduced sizing for next 5 trades post-crisis
    DRAWDOWN_RECOVERY_MULT: float = 0.7  # 30% size reduction during recovery

    # Kite endpoint -- direct (prod/VPS) or via OCI relay (home desktop).
    # Relay is a path-preserving forward proxy; auth + X-Kite-Version headers pass through.
    # Override with KITE_BASE_URL in .env (e.g. http://161.118.160.180:31527).
    KITE_BASE_URL: str = "https://api.kite.trade"

    # === Breadth Enrichment (2026-06-14) ===
    BREADTH_ENRICHMENT_ENABLED:         bool  = False   # Feature flag -- OFF by default
    BREADTH_UNIVERSE:                   str   = "NIFTY100"
    BREADTH_CACHE_TTL_SECONDS:          int   = 3600    # Tier 1 stale-while-revalidate window
    BREADTH_FETCH_TIMEOUT_SECONDS:      int   = 90      # Max time for Tier 1 fetch
    BREADTH_NARROW_RALLY_THRESHOLD:     float = 0.40    # R1 gate fires below this
    BREADTH_NARROW_GATE_EXEMPT_RANK:    float = 0.80    # Top quintile bypasses R1 gate
    BREADTH_RANK_BONUS_TOP:             int   = 15      # +15 if rank >= 0.80
    BREADTH_RANK_BONUS_MID:             int   = 7       # +7 if rank >= 0.60
    BREADTH_RANK_PENALTY_BOTTOM:        int   = -10     # -10 if rank < 0.20
    BREADTH_RANK_MULTIPLIER:            float = 1.2     # Top quintile score x this
    BREADTH_DATA_DEGRADED_THRESHOLD:    float = 0.10    # >10% fetch failures = degraded
    BREADTH_TIER1_PARALLELISM:          int   = 4       # Concurrent Kite historical fetches
    BREADTH_DATA_DIR:                   str   = "data"   # Path (relative to python-engine/) to nifty100.json

    # === Universe Expansion (2026-06-15) ===
    UNIVERSE_SIZE:                     int   = 500      # 100 or 500 — current trading universe size
    UNIVERSE_TICKERS_FILE:             str   = "nifty500.json"  # Filename inside BREADTH_DATA_DIR; same format as nifty100.json
    UNIVERSE_MIN_ADV_CRORE:            float = 2.0      # Drop tickers with 20-day median ADV below this (₹ crore)
    UNIVERSE_LIQUIDITY_LOOKBACK_DAYS:  int   = 20       # Lookback window for the median ADV computation

    # === Intraday history retention ([INTRADAY-RETENTION 2026-08-04]) ===
    # How many days of 15-minute candles to keep in intraday_cache.
    #
    # This was effectively 1 (clear_intraday_cache deleted everything before
    # yesterday), which is the reason momentum and F&O have never been
    # backtested: they are intraday strategies and the system kept no intraday
    # history to test them on. Daily bars go back 2.5 years in ohlcv_cache;
    # intraday went back three days.
    #
    # 365 sessions at a measured 2.53 MB/day is ~633 MB. Disk sits at 86% with
    # 17 GB free, and /data is already carrying ~800 MB of stale
    # cache.db.bak-* files, so this is affordable today -- but it is the
    # largest single consumer added here, so it is a knob rather than a
    # constant. Lower it if disk pressure becomes real; every day removed is a
    # day of evidence the strategies cannot be judged on.
    INTRADAY_RETENTION_DAYS:           int   = 365

    # Rolling daily-history lookback for strategies that need long SMAs
    # (Connors needs 250 bars for SMA-200). Replaces a hard-coded
    # "2025-01-01" anchor in penny_scanner, which pinned every ticker in
    # ohlcv_cache to exactly 394 bars and left the Connors backtest with ~140
    # evaluation days per name. 3 years covers the 250-bar floor several times
    # over and lets the daily cache accumulate a corpus worth testing on.
    # Cost is one-off: get_historical caches by window coverage, so widening
    # re-fetches once per ticker and then hits cache.
    DAILY_HISTORY_DAYS:                int   = 1095

    # === H4 Intraday-cache semantics (2026-09-13, plan section 12) ===
    # §12 mandates: "Cache only with explicit instrument, interval,
    # completed-bar cutoff and freshness semantics." The two knobs
    # below make the existing ``get_intraday`` cache HIT path
    # honour those semantics explicitly. The defaults preserve the
    # existing behaviour; the knobs are operator-tunable.
    #
    # INTRADAY_CACHE_FRESHNESS_SECONDS:
    #   Maximum age (seconds) of the most recent cached candle
    #   relative to ``to_datetime``. A candle whose age is older
    #   than this triggers a Kite round-trip; a candle newer than
    #   this is served as a HIT. The existing freshness check uses
    #   ``interval_mins`` (1 minute for interval="minute", etc.).
    #   We default to 0 -- strict "must be the candle covering up
    #   to the most recent interval boundary" semantics, matching
    #   the pre-H4 behaviour. An operator can relax to e.g. 60 if
    #   they want a 1-minute staleness budget; the knob exists for
    #   that.
    INTRADAY_CACHE_FRESHNESS_SECONDS:   int   = 0
    # INTRADAY_CACHE_INCLUDE_FORMING:
    #   If True, candles whose ``datetime`` is in the future
    #   relative to ``to_datetime`` (a forming candle) ARE served
    #   from cache. If False (the §12 default), forming candles
    #   are EXCLUDED from the returned set. The cache continues
    #   to contain forming candles (the writer has no way to
    #   know); the HIT path simply doesn't return them.
    #   Section 12: "Do not mix mutable forming bars with
    #   completed historical bars."
    INTRADAY_CACHE_INCLUDE_FORMING:    bool  = False
    # INTRADAY_CACHE_MIN_CANDLES:
    #   The minimum number of candles required to serve a HIT.
    #   Existing behaviour: 4 (the VWAP floor). §12 makes this
    #   explicit and operator-tunable. Lowering this risks
    #   VWAP instability on partial data; raising it requires
    #   more pre-cache data per ticker.
    INTRADAY_CACHE_MIN_CANDLES:        int   = 4


    # ============================================================
    # F6 CAPITAL POLICY (2026-09-13, plan section 10.5)
    # ============================================================
    # The third gate in the live-growth chain:
    #   promotion-bridge (signed state)        -- who may promote
    #       -> affordability guard (F2)        -- can the live pool grow
    #           -> capital policy guard (F6)    -- should the live pool grow
    #
    # Every knob below is a professional-conservative default that
    # the operator can override by editing config.py (or by setting
    # the corresponding env var, since this is a BaseSettings).
    #
    # CAPITAL_POLICY_LOSS_TOLERANCE_PCT is the EXPLICIT USER INPUT
    # the plan mandates ("Leave the user's loss tolerance as an
    # explicit input if not supplied"). Unknown until explicitly set;
    # do not infer a user's loss tolerance from engineering defaults.
    CAPITAL_POLICY_LOSS_TOLERANCE_PCT:           float | None = None
    # Current realised drawdown cap. Independent of loss tolerance
    # because they measure different things: loss tolerance is the
    # *worst-case* loss the user accepts on a single growth event;
    # max drawdown is the *current* realised drawdown that gates
    # all growth until it recovers.
    CAPITAL_POLICY_MAX_DRAWDOWN_PCT:            float = 15.0
    # Minimum closed-trade win rate required. Default 50% (half
    # profitable). Lower for early systems with few trades.
    CAPITAL_POLICY_MIN_WIN_RATE_PCT:             float = 50.0
    # Minimum average R-multiple of closed trades. Default 0.0
    # (breakeven on R). Raise to require positive expectancy.
    CAPITAL_POLICY_MIN_AVG_R_MULTIPLE:           float = 0.0
    # Maximum consecutive losing closed trades. Default 5
    # (industry-standard operational stability gate).
    CAPITAL_POLICY_MAX_CONSECUTIVE_LOSSES:       int   = 5
    # Minimum current live bankroll before any increase is even
    # evaluated. Default 1500 (matches PENNY_EDGE_LIVE_BANKROLL).
    CAPITAL_POLICY_MIN_LIVE_BANKROLL_INR:        float = 1500.0
    # If True, an UNRESOLVED or UNAVAILABLE broker statement refuses
    # the increase. Never grow live capital on unverified broker
    # truth.
    CAPITAL_POLICY_REQUIRE_BROKER_RECONCILIATION: bool  = True
    # If True, the absence of a proactive research artifact on
    # file returns INSUFFICIENT_EVIDENCE. Never grow live capital
    # without a recorded research basis.
    CAPITAL_POLICY_REQUIRE_PROACTIVE_EVIDENCE:   bool  = True

    # ========================================================================
    # TradingView -> Fyers options pipeline (smit_fyers_options branch)
    # ------------------------------------------------------------------------
    # Signal source: TradingView Pine Script webhook alerts (entries only).
    # Execution broker: Fyers (separate from the Zerodha-based FNO module
    # above -- no shared state, no shared broker client). The bot itself
    # owns every exit (stop/trail/target); TradingView never sends an exit.
    #
    # Triple-disarm, mirrors the FNO_* pattern above exactly: live trading
    # is false, live bankroll is zero, and live is explicitly disabled --
    # all three independently, all three default-safe from this first
    # commit. Do not flip any of these without an explicit, separate,
    # deliberate decision once a paper-mode qualification test has been
    # reviewed.
    # ========================================================================
    TV_FYERS_PAPER_BANKROLL:      float = 250000.0
    TV_FYERS_LIVE_BANKROLL:       float = 0.0        # not armed
    TV_FYERS_LIVE_TRADING:        bool  = False       # master switch
    TV_FYERS_DISABLE_PAPER:       bool  = False
    TV_FYERS_DISABLE_LIVE:        bool  = True

    # Shared secret TradingView embeds in its alert body (node-gateway
    # validates this; python-engine never sees the TradingView secret,
    # only the existing INTERNAL_API_SECRET from node-gateway itself).
    TV_FYERS_WEBHOOK_SECRET:      str   = ""          # must be set in .env

    # --- entry double-check thresholds (server-side veto over TV alerts) ---
    TV_FYERS_RSI_LENGTH:          int   = 14
    TV_FYERS_RSI_LONG_MIN:        float = 50.0        # CE only if RSI >= this
    TV_FYERS_RSI_LONG_MAX:        float = 80.0        # reject if overbought
    TV_FYERS_RSI_SHORT_MAX:       float = 50.0        # PE only if RSI <= this
    TV_FYERS_RSI_SHORT_MIN:       float = 20.0        # reject if oversold

    TV_FYERS_IV_SANITY_MIN:       float = 0.05
    TV_FYERS_IV_SANITY_MAX:       float = 1.00

    # [SMIT-FYERS-OPTIONS-AI 2026-09-25] Candle-trend double-check now asks
    # Claude to read the recent NIFTY candles and judge whether they
    # support the TradingView alert's proposed direction, replacing the
    # old mechanical RSI-threshold veto (rsi_veto in tv_fyers_gates.py was
    # removed; see tv_fyers_ai_gate.py). IV/liquidity/signal-freshness
    # stay deterministic on purpose -- only the directional-momentum
    # judgment call is now AI-driven, and only for this paper-only
    # pipeline. Empty key => the AI gate fails closed (ai_unavailable_no_key),
    # same fail-closed posture every other veto here already has.
    TV_FYERS_ANTHROPIC_API_KEY:   str   = ""
    TV_FYERS_AI_MODEL:            str   = "claude-sonnet-5"
    TV_FYERS_AI_TIMEOUT_SEC:      float = 15.0
    TV_FYERS_AI_MAX_CANDLES:      int   = 30
    # [CONFIDENCE-FLOOR 2026-09-28] A PASS/ENTER decision below this
    # confidence is treated as a reject/no-trade -- a model that says
    # "yes, but I'm not sure" is not the same as a model that's sure.
    TV_FYERS_AI_MIN_CONFIDENCE:  float = 0.6

    # [AI-SCAN 2026-09-25] Autonomous candle-scan entry path (no
    # TradingView alert -- see tv_fyers_orchestrator.handle_ai_candle_scan).
    # 300s matches the 5-minute candle resolution the AI actually reads;
    # scanning faster would mostly re-show the same not-yet-closed candle.
    # Governed by the SAME TV_FYERS_DISABLE_PAPER kill switch as every
    # other entry path here -- no separate disarm flag needed.
    TV_FYERS_AI_SCAN_INTERVAL_SEC: int  = 300

    TV_FYERS_MIN_OI:              int   = 5000
    TV_FYERS_MIN_VOL:             int   = 1000
    TV_FYERS_MAX_SPREAD_PCT:      float = 0.015
    TV_FYERS_MAX_QUOTE_AGE_SEC:   int   = 120

    # --- sizing / risk -------------------------------------------------
    TV_FYERS_STOP_PREMIUM_PCT:    float = 0.25
    TV_FYERS_MAX_RISK_PCT:        float = 0.02
    TV_FYERS_MAX_LOTS:            int   = 2
    TV_FYERS_MAX_CONCURRENT:      int   = 2
    TV_FYERS_MAX_TRADES_PER_DAY:  int   = 3

    # --- exit management (bot-owned; TradingView never signals exits) --
    TV_FYERS_TARGET_R:            float = 1.8
    TV_FYERS_TRAIL_ATR_MULT:      float = 1.0
    TV_FYERS_TIME_STOP_MIN:       int   = 45

    # --- scheduling ------------------------------------------------------
    TV_FYERS_SCAN_INTERVAL_SEC:   int   = 90          # exit-management poll
    TV_FYERS_SIGNAL_MAX_AGE_SEC:  int   = 300          # webhook staleness


settings = Settings()
