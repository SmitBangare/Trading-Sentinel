"""
[SMIT-FYERS-OPTIONS 2026-09-25] Falsifiability tests for tv_fyers_gates.py.

Mirrors tests/test_fno_gate_falsifiability.py's structure and rationale:
every gate MUST admit at least one passing input. A gate without a
witness does not merge -- this is the same project rule the FNO module's
falsifiability tests enforce, applied to this pipeline's own (separate)
gate ladder.
"""
import dataclasses

import pytest

from tv_fyers_gates import (
    ALL_ENTRY_GATES, GateContext, evaluate_tv_entry, make_witness_context,
)


@pytest.mark.parametrize("gate", ALL_ENTRY_GATES, ids=lambda g: g.name)
def test_gate_is_satisfiable(gate):
    """Every gate MUST admit at least one passing input."""
    ok, _ = gate.accepts(gate.witness_input())
    assert ok, f"{gate.name} is unsatisfiable"


def test_every_gate_has_a_witness():
    for gate in ALL_ENTRY_GATES:
        assert callable(gate.witness_input), f"{gate.name} ships no witness"


def test_full_ladder_passes_on_witness():
    ok, reason = evaluate_tv_entry(make_witness_context())
    assert ok, f"witness context rejected by: {reason}"
    assert reason == ""


def test_ladder_reports_first_failure_in_spec_order():
    ctx = dataclasses.replace(make_witness_context(), signal_age_sec=999.0, oi=0)
    ok, reason = evaluate_tv_entry(ctx)
    assert not ok
    # signal_freshness is evaluated before liquidity_veto
    assert reason == "signal_stale"


# ---------------------------------------------------------------------------
# Per-gate rejection spot-checks: each gate must also actually gate.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("overrides,expected", [
    ({"signal_age_sec": 9999.0}, "signal_stale"),
    ({"direction": "CE", "rsi": 30.0}, "rsi_below_long_min"),
    ({"direction": "CE", "rsi": 90.0}, "rsi_overbought"),
    ({"direction": "PE", "rsi": 70.0}, "rsi_above_short_max"),
    ({"direction": "PE", "rsi": 10.0}, "rsi_oversold"),
    ({"rsi": None}, "rsi_unavailable"),
    ({"iv": None}, "iv_unavailable"),
    ({"iv": 0.01}, "iv_out_of_band"),
    ({"iv": 2.5}, "iv_out_of_band"),
    ({"quote_age_sec": 999.0}, "stale_quote"),
    ({"oi": 10}, "oi_too_thin"),
    ({"volume": 1}, "volume_too_thin"),
    ({"bid": 0.0}, "one_sided_market"),
    ({"ask": 0.0}, "one_sided_market"),
    ({"bid": 90.0, "ask": 110.0}, "spread_too_wide"),
])
def test_each_gate_rejects_its_failure_mode(overrides, expected):
    ctx = dataclasses.replace(make_witness_context(), **overrides)
    ok, reason = evaluate_tv_entry(ctx)
    assert not ok
    assert reason == expected


def test_direction_short_side_passes_symmetric_witness():
    """PE (short bias) has its own satisfiable witness, mirroring the CE
    default witness -- RSI 40 is inside the short band (20-50)."""
    ctx = dataclasses.replace(make_witness_context(), direction="PE", rsi=40.0)
    ok, reason = evaluate_tv_entry(ctx)
    assert ok, f"short-side witness rejected by: {reason}"


def test_unknown_direction_is_rejected_not_silently_accepted():
    ctx = dataclasses.replace(make_witness_context(), direction="LONG")
    ok, reason = evaluate_tv_entry(ctx)
    assert not ok
    assert reason == "rsi_unknown_direction"
