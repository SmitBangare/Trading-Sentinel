"""
Tests for tv_fyers_ai_gate.py -- the candle-trend AI double-check that
replaced the RSI-threshold veto (see tv_fyers_gates.py's module docstring).

Every failure mode must fail CLOSED (reject, never silently pass) -- that
is the entire safety point of this module, so it gets the same
per-failure-mode spot-check discipline as tv_fyers_gate_falsifiability.py,
plus explicit tests for the two success paths (PASS/REJECT parsed from a
well-formed model response).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tv_fyers_ai_gate import AiTradePlan, analyze_and_plan_trade, evaluate_trend_with_ai


def _candles(n=20):
    base_ts = 1_700_000_000
    out = []
    price = 24000.0
    for i in range(n):
        price += 3.0 if i % 2 == 0 else -1.5
        out.append([base_ts + i * 300, price - 2, price + 3, price - 4, price, 1000])
    return out


def _mock_response(text: str):
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(content=[block])


class TestFailClosed:
    @pytest.mark.asyncio
    async def test_no_api_key_fails_closed(self, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "TV_FYERS_ANTHROPIC_API_KEY", "")
        ok, reason = await evaluate_trend_with_ai(_candles(), "CE", 24500.0)
        assert ok is False
        assert reason == "ai_unavailable_no_key"

    @pytest.mark.asyncio
    async def test_no_candles_fails_closed(self, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "TV_FYERS_ANTHROPIC_API_KEY", "test-key")
        ok, reason = await evaluate_trend_with_ai([], "CE", 24500.0)
        assert ok is False
        assert reason == "ai_unavailable_no_candles"

    @pytest.mark.asyncio
    async def test_api_call_exception_fails_closed(self, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "TV_FYERS_ANTHROPIC_API_KEY", "test-key")
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.create = AsyncMock(side_effect=RuntimeError("network down"))
            ok, reason = await evaluate_trend_with_ai(_candles(), "CE", 24500.0)
        assert ok is False
        assert reason == "ai_unavailable_call_failed"

    @pytest.mark.asyncio
    async def test_non_json_response_fails_closed(self, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "TV_FYERS_ANTHROPIC_API_KEY", "test-key")
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.create = AsyncMock(return_value=_mock_response("not json at all"))
            ok, reason = await evaluate_trend_with_ai(_candles(), "CE", 24500.0)
        assert ok is False
        assert reason == "ai_malformed_response"

    @pytest.mark.asyncio
    async def test_missing_decision_field_fails_closed(self, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "TV_FYERS_ANTHROPIC_API_KEY", "test-key")
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.create = AsyncMock(
                return_value=_mock_response('{"reason": "looks_fine", "confidence": 0.8}')
            )
            ok, reason = await evaluate_trend_with_ai(_candles(), "CE", 24500.0)
        assert ok is False
        assert reason == "ai_malformed_response"

    @pytest.mark.asyncio
    async def test_invalid_decision_value_fails_closed(self, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "TV_FYERS_ANTHROPIC_API_KEY", "test-key")
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.create = AsyncMock(
                return_value=_mock_response('{"decision": "MAYBE", "reason": "unsure", "confidence": 0.5}')
            )
            ok, reason = await evaluate_trend_with_ai(_candles(), "CE", 24500.0)
        assert ok is False
        assert reason == "ai_malformed_response"

    @pytest.mark.asyncio
    async def test_confidence_out_of_range_fails_closed(self, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "TV_FYERS_ANTHROPIC_API_KEY", "test-key")
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.create = AsyncMock(
                return_value=_mock_response('{"decision": "PASS", "reason": "strong_uptrend", "confidence": 1.5}')
            )
            ok, reason = await evaluate_trend_with_ai(_candles(), "CE", 24500.0)
        assert ok is False
        assert reason == "ai_malformed_response"


class TestSuccessPaths:
    @pytest.mark.asyncio
    async def test_pass_decision_is_surfaced_with_ai_prefix(self, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "TV_FYERS_ANTHROPIC_API_KEY", "test-key")
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.create = AsyncMock(
                return_value=_mock_response('{"decision": "PASS", "reason": "strong_uptrend", "confidence": 0.82}')
            )
            ok, reason = await evaluate_trend_with_ai(_candles(), "CE", 24500.0, rsi=61.2)
        assert ok is True
        assert reason == "ai_pass_strong_uptrend"

    @pytest.mark.asyncio
    async def test_reject_decision_is_surfaced_with_ai_prefix(self, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "TV_FYERS_ANTHROPIC_API_KEY", "test-key")
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.create = AsyncMock(
                return_value=_mock_response('{"decision": "REJECT", "reason": "choppy no clear trend!!", "confidence": 0.3}')
            )
            ok, reason = await evaluate_trend_with_ai(_candles(), "PE", 24500.0)
        assert ok is False
        # Reason text is sanitised to lowercase snake_case -- spaces/punctuation stripped.
        assert reason == "ai_reject_choppynocleartrend"

    @pytest.mark.asyncio
    async def test_low_confidence_pass_is_treated_as_reject(self, monkeypatch):
        """[CONFIDENCE-FLOOR 2026-09-28] A PASS below TV_FYERS_AI_MIN_CONFIDENCE
        (default 0.6) must not be trusted -- the model said "yes, but I'm
        not sure", which is not the same as "yes"."""
        from config import settings
        monkeypatch.setattr(settings, "TV_FYERS_ANTHROPIC_API_KEY", "test-key")
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.create = AsyncMock(
                return_value=_mock_response('{"decision": "PASS", "reason": "maybe_uptrend", "confidence": 0.4}')
            )
            ok, reason = await evaluate_trend_with_ai(_candles(), "CE", 24500.0)
        assert ok is False
        assert reason == "ai_reject_low_confidence"

    @pytest.mark.asyncio
    async def test_markdown_fenced_response_is_still_rejected_not_parsed(self, monkeypatch):
        """The model was told not to wrap the JSON in a code fence; if it
        does anyway, this must fail closed rather than attempt fragile
        fence-stripping that could be gamed or misparsed."""
        from config import settings
        monkeypatch.setattr(settings, "TV_FYERS_ANTHROPIC_API_KEY", "test-key")
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.create = AsyncMock(
                return_value=_mock_response('```json\n{"decision": "PASS", "reason": "ok", "confidence": 0.9}\n```')
            )
            ok, reason = await evaluate_trend_with_ai(_candles(), "CE", 24500.0)
        assert ok is False
        assert reason == "ai_malformed_response"


class TestAnalyzeAndPlanTrade:
    """analyze_and_plan_trade() is the autonomous full-planner path (no
    TradingView alert). Previously only exercised indirectly through
    mocks in test_tv_fyers_orchestrator.py; this covers its own parsing/
    validation logic directly."""

    @pytest.mark.asyncio
    async def test_no_key_fails_closed(self, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "TV_FYERS_ANTHROPIC_API_KEY", "")
        plan, reason = await analyze_and_plan_trade(_candles(), 24500.0)
        assert plan is None
        assert reason == "ai_unavailable_no_key"

    @pytest.mark.asyncio
    async def test_no_trade_decision_returns_none(self, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "TV_FYERS_ANTHROPIC_API_KEY", "test-key")
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.create = AsyncMock(
                return_value=_mock_response('{"decision": "NO_TRADE", "reason": "choppy", "confidence": 0.7}')
            )
            plan, reason = await analyze_and_plan_trade(_candles(), 24500.0)
        assert plan is None
        assert reason == "ai_no_trade_choppy"

    @pytest.mark.asyncio
    async def test_valid_enter_ce_produces_a_plan(self, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "TV_FYERS_ANTHROPIC_API_KEY", "test-key")
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.create = AsyncMock(
                return_value=_mock_response(
                    '{"decision": "ENTER_CE", "stop_underlying": 24400.0, "target_underlying": 24700.0, "reason": "strong_uptrend", "confidence": 0.85}'
                )
            )
            plan, reason = await analyze_and_plan_trade(_candles(), 24500.0)
        assert isinstance(plan, AiTradePlan)
        assert plan.direction == "CE"
        assert plan.stop_underlying == 24400.0
        assert plan.target_underlying == 24700.0
        assert reason == "ai_plan_strong_uptrend"

    @pytest.mark.asyncio
    async def test_valid_enter_pe_produces_a_plan(self, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "TV_FYERS_ANTHROPIC_API_KEY", "test-key")
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.create = AsyncMock(
                return_value=_mock_response(
                    '{"decision": "ENTER_PE", "stop_underlying": 24600.0, "target_underlying": 24300.0, "reason": "strong_downtrend", "confidence": 0.85}'
                )
            )
            plan, reason = await analyze_and_plan_trade(_candles(), 24500.0)
        assert plan.direction == "PE"
        assert plan.stop_underlying == 24600.0
        assert plan.target_underlying == 24300.0

    @pytest.mark.asyncio
    async def test_ce_stop_on_wrong_side_of_price_fails_closed(self, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "TV_FYERS_ANTHROPIC_API_KEY", "test-key")
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.create = AsyncMock(
                return_value=_mock_response(
                    '{"decision": "ENTER_CE", "stop_underlying": 24600.0, "target_underlying": 24700.0, "reason": "bad_plan", "confidence": 0.85}'
                )
            )
            plan, reason = await analyze_and_plan_trade(_candles(), 24500.0)
        assert plan is None
        assert reason == "ai_malformed_response"

    @pytest.mark.asyncio
    async def test_missing_stop_underlying_on_enter_fails_closed(self, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "TV_FYERS_ANTHROPIC_API_KEY", "test-key")
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.create = AsyncMock(
                return_value=_mock_response(
                    '{"decision": "ENTER_CE", "target_underlying": 24700.0, "reason": "no_stop", "confidence": 0.85}'
                )
            )
            plan, reason = await analyze_and_plan_trade(_candles(), 24500.0)
        assert plan is None
        assert reason == "ai_malformed_response"

    @pytest.mark.asyncio
    async def test_low_confidence_enter_is_treated_as_no_trade(self, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "TV_FYERS_ANTHROPIC_API_KEY", "test-key")
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.create = AsyncMock(
                return_value=_mock_response(
                    '{"decision": "ENTER_CE", "stop_underlying": 24400.0, "target_underlying": 24700.0, "reason": "unsure_uptrend", "confidence": 0.35}'
                )
            )
            plan, reason = await analyze_and_plan_trade(_candles(), 24500.0)
        assert plan is None
        assert reason == "ai_no_trade_low_confidence"

    @pytest.mark.asyncio
    async def test_invalid_decision_value_fails_closed(self, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "TV_FYERS_ANTHROPIC_API_KEY", "test-key")
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_cls.return_value.messages.create = AsyncMock(
                return_value=_mock_response('{"decision": "MAYBE", "reason": "unsure", "confidence": 0.9}')
            )
            plan, reason = await analyze_and_plan_trade(_candles(), 24500.0)
        assert plan is None
        assert reason == "ai_malformed_response"

    @pytest.mark.asyncio
    async def test_no_candles_fails_closed(self, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "TV_FYERS_ANTHROPIC_API_KEY", "test-key")
        plan, reason = await analyze_and_plan_trade([], 24500.0)
        assert plan is None
        assert reason == "ai_unavailable_no_candles"
