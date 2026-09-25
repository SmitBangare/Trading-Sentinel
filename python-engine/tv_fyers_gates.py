"""
[SMIT-FYERS-OPTIONS 2026-09-25] Entry-gate ladder for the TradingView ->
Fyers pipeline. Own GateContext/Gate ladder, own witnesses -- a SEPARATE
ladder for a separate signal source, not shared with fno_gates.py (that
module gates the Zerodha-based NIFTY engine's own ORB signal; this one
gates an externally-supplied TradingView alert).

Mirrors fno_gates.py's Gate/witness discipline exactly: every gate ships
a witness_input() that passes it, and a project-wide rule (established
after the penny module ran a mathematically unsatisfiable gate for 9
months, rejecting 100% of signals with no health check ever catching it)
requires this -- see tests/test_tv_fyers_gate_falsifiability.py, which
mirrors tests/test_fno_gate_falsifiability.py's structure.

[AI GATE 2026-09-25, restored 2026-09-25] The RSI-threshold veto
(`rsi_veto`) was briefly removed in favour of an AI candle-trend gate,
then RESTORED as the default here once the user flagged that
TV_FYERS_ANTHROPIC_API_KEY is a real, paid cost with no free tier --
this ladder must keep working (and cost nothing) with no key configured
at all. Current behaviour, controlled entirely by
tv_fyers_orchestrator.py, not this file:
  - No AI key configured (the default): this deterministic ladder,
    RSI included, is the ENTIRE double-check. $0 cost.
  - AI key configured: Claude's candle-trend read
    (tv_fyers_ai_gate.evaluate_trend_with_ai) runs as an ADDITIONAL
    check AFTER this ladder passes, not a replacement for it -- see
    that module's docstring. Nothing here changes based on whether a
    key is configured; this ladder is unconditionally free and
    unconditionally deterministic.
IV, liquidity, signal-freshness and RSI all stay here, deterministic,
on purpose.

[MODULE NAMING] This file is intentionally `tv_fyers_gates.py`, not
`fno_tv_gates.py` or similar -- tests/test_fno_isolation.py AST-walks
every `fno_*`-prefixed module and forbids it from importing `engine`,
`risk_engine`, `portfolio`, etc. This ladder needs no such import itself,
but tv_fyers_orchestrator.py (which calls engine.calc_rsi_series) does,
and keeping the whole pipeline off the `fno_` prefix keeps that firewall
unambiguous rather than needing a module-by-module exception.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from tv_fyers_veto import verify_iv, verify_liquidity, verify_rsi


@dataclass
class GateContext:
    """Everything the double-check ladder needs, snapshot at the moment
    a TradingView entry signal is evaluated."""
    direction: str               # "CE" or "PE"
    rsi: Optional[float]
    iv: Optional[float]
    oi: int
    volume: int
    bid: float
    ask: float
    quote_age_sec: float
    signal_age_sec: float = 0.0
    max_signal_age_sec: float = 300.0


@dataclass(frozen=True)
class Gate:
    name: str
    _check: Callable[[GateContext], Tuple[bool, str]]
    _witness: Callable[[], GateContext]

    def accepts(self, ctx: GateContext) -> Tuple[bool, str]:
        return self._check(ctx)

    def witness_input(self) -> GateContext:
        return self._witness()


def make_witness_context() -> GateContext:
    """One context that passes EVERY gate under default settings."""
    return GateContext(
        direction="CE",
        rsi=60.0,
        iv=0.20,
        oi=10_000,
        volume=5_000,
        bid=99.5,
        ask=100.5,
        quote_age_sec=10.0,
        signal_age_sec=5.0,
        max_signal_age_sec=300.0,
    )


def _check_signal_freshness(ctx: GateContext) -> Tuple[bool, str]:
    if ctx.signal_age_sec > ctx.max_signal_age_sec:
        return False, "signal_stale"
    return True, ""


def _check_rsi(ctx: GateContext) -> Tuple[bool, str]:
    return verify_rsi(ctx.direction, ctx.rsi)


def _check_iv(ctx: GateContext) -> Tuple[bool, str]:
    return verify_iv(ctx.iv)


def _check_liquidity(ctx: GateContext) -> Tuple[bool, str]:
    return verify_liquidity(
        oi=ctx.oi, volume=ctx.volume, bid=ctx.bid, ask=ctx.ask,
        quote_age_sec=ctx.quote_age_sec,
    )


ALL_ENTRY_GATES: List[Gate] = [
    Gate("signal_freshness", _check_signal_freshness, make_witness_context),
    Gate("rsi_veto", _check_rsi, make_witness_context),
    Gate("iv_veto", _check_iv, make_witness_context),
    Gate("liquidity_veto", _check_liquidity, make_witness_context),
]


def evaluate_tv_entry(ctx: GateContext) -> Tuple[bool, str]:
    """Run the double-check ladder in order. Returns (ok, first_reject_reason).

    Each gate's own check function returns its own specific reason string
    (e.g. "rsi_overbought", "spread_too_wide") rather than just the gate
    name, since a single gate here wraps several distinct failure modes
    (unlike fno_gates.py's one-condition-per-gate shape).
    """
    for gate in ALL_ENTRY_GATES:
        ok, reason = gate.accepts(ctx)
        if not ok:
            return False, reason
    return True, ""
