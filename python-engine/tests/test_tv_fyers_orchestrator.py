"""
Tests for tv_fyers_orchestrator.py -- entry (event-driven) and exit
(polling) paths for the TradingView -> Fyers pipeline.

TestResolveOptionContract exercises the real optionchain()-based
resolution logic against a realistic mocked response (shaped exactly
like a real Fyers optionchain() response captured live on 2026-09-25).

The rest of the entry-path tests (TestHandleTvEntrySignal) monkeypatch
_resolve_option_contract to return a synthetic ResolvedContract, so the
REST of the pipeline -- staleness, RSI/ATR, double-check gates, sizing,
paper execution, position persistence -- is exercised in isolation from
contract-resolution specifics.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytz

import tv_fyers_orchestrator as orch
from tv_fyers_positions import init_tv_fyers_positions_db, open_positions, insert_position
from tv_fyers_signal import init_tv_fyers_signal_db

IST = pytz.timezone("Asia/Kolkata")


def _ist(*args) -> datetime:
    """Timezone-aware IST datetime, matching what datetime.now(IST)
    (the real caller's default) actually produces -- a naive datetime
    here would trigger the same defensive TypeError-catch the orchestrator
    uses for a genuinely malformed entry_time, which is not what these
    tests are exercising."""
    return IST.localize(datetime(*args))


def _mock_fyers_with_candles(n=40):
    fyers = MagicMock()
    # Oscillating-but-net-upward candles so RSI lands in the healthy
    # "long" band (well under the 80 overbought veto) rather than
    # pegging at exactly 100 the way a purely monotonic series would --
    # a monotonic uptrend is unrealistic market data and would trip the
    # overbought gate by construction, not by the scenario being tested.
    base_ts = 1_700_000_000
    candles = []
    price = 24000.0
    for i in range(n):
        price += 3.0 if i % 2 == 0 else -1.5
        candles.append([base_ts + i * 300, price - 2, price + 3, price - 4, price, 1000])
    fyers.get_historical = AsyncMock(return_value=candles)
    fyers.get_quote = AsyncMock(return_value={})
    return fyers


def _good_contract():
    return orch.ResolvedContract(
        symbol="NSE:NIFTY24SEP24500CE", lot_size=65, bid=119.5, ask=120.5,
        oi=10_000, volume=5_000, iv=0.20, quote_age_sec=5.0,
    )


def _real_shaped_option_chain(underlying_ltp=23140.5, forward=23190.0):
    """Shaped exactly like a real Fyers optionchain() response captured
    live on 2026-09-25 (field names/nesting verified against a real
    account, not guessed) -- trimmed to a few strikes either side of
    the money for CE and PE."""
    return {
        "code": 200,
        "s": "ok",
        "data": {
            "expiryData": [
                {"date": "29-09-2026", "expiry": "1790676600", "expiry_flag": "M"},
                {"date": "06-10-2026", "expiry": "1791281400", "expiry_flag": "W"},
            ],
            "optionsChain": [
                {
                    "symbol": "NSE:NIFTY50-INDEX", "ex_symbol": "NIFTY50-INDEX",
                    "option_type": "", "strike_price": -1,
                    "ltp": underlying_ltp, "fp": forward,
                },
                {
                    "symbol": "NSE:NIFTY26SEP23100CE", "option_type": "CE",
                    "strike_price": 23100, "bid": 149.05, "ask": 150.15,
                    "ltp": 150.15, "oi": 9300265, "volume": 349838515,
                },
                {
                    "symbol": "NSE:NIFTY26SEP23100PE", "option_type": "PE",
                    "strike_price": 23100, "bid": 58.3, "ask": 58.85,
                    "ltp": 58.3, "oi": 11453000, "volume": 467405770,
                },
                {
                    "symbol": "NSE:NIFTY26SEP23150CE", "option_type": "CE",
                    "strike_price": 23150, "bid": 118.75, "ask": 119.3,
                    "ltp": 119.25, "oi": 3817255, "volume": 218096385,
                },
                {
                    "symbol": "NSE:NIFTY26SEP23150PE", "option_type": "PE",
                    "strike_price": 23150, "bid": 76.6, "ask": 77.15,
                    "ltp": 76.75, "oi": 4174755, "volume": 177176675,
                },
            ],
        },
    }


def _signal_payload(**overrides):
    base = {
        "signal_id": "sig-orch-1",
        "symbol": "NIFTY",
        "direction": "CE",
        "underlying_price": 24500.0,
        "strategy": "orb_momentum_v1",
        "signal_time": datetime.now(timezone.utc).isoformat(),
    }
    base.update(overrides)
    return base


async def _init_dbs(db_path):
    await init_tv_fyers_signal_db(db_path)
    await init_tv_fyers_positions_db(db_path)


class TestDropUnclosedTrailingCandle:
    """[DATA-ACCURACY 2026-09-28] RSI/ATR/signal generation must never
    see a still-forming candle -- Fyers' history endpoint is queried by
    date, not exact timestamp, so this is a defensive trim regardless of
    what the server actually does with a mid-session query."""

    def test_drops_a_candle_whose_window_has_not_closed_yet(self):
        now = _ist(2026, 9, 25, 10, 3)  # 10:00-10:05 candle is still open
        candles = [
            [int(_ist(2026, 9, 25, 9, 55).timestamp()), 100, 101, 99, 100, 10],
            [int(_ist(2026, 9, 25, 10, 0).timestamp()), 100, 101, 99, 100, 10],  # closes 10:05 -- not yet
        ]
        out = orch._drop_unclosed_trailing_candle(candles, now)
        assert len(out) == 1
        assert out[0][0] == candles[0][0]

    def test_keeps_a_candle_at_the_exact_close_instant(self):
        now = _ist(2026, 9, 25, 10, 5)  # exactly when the 10:00 candle closes
        candles = [[int(_ist(2026, 9, 25, 10, 0).timestamp()), 100, 101, 99, 100, 10]]
        out = orch._drop_unclosed_trailing_candle(candles, now)
        assert len(out) == 1

    def test_empty_input_returns_empty(self):
        assert orch._drop_unclosed_trailing_candle([], _ist(2026, 9, 25, 10, 5)) == []


class TestResolveOptionContract:
    @pytest.mark.asyncio
    async def test_picks_nearest_strike_for_ce(self):
        fyers = MagicMock()
        fyers.get_option_chain = AsyncMock(return_value=_real_shaped_option_chain())
        contract = await orch._resolve_option_contract(fyers, "CE", 23140.0, _ist(2026, 9, 25, 10, 0))
        assert contract is not None
        # 23140 is 40 away from 23100, 10 away from 23150 -> nearest is 23150.
        assert contract.symbol == "NSE:NIFTY26SEP23150CE"
        assert contract.lot_size == 65
        assert contract.bid == 118.75
        assert contract.ask == 119.3
        assert contract.oi == 3817255

    @pytest.mark.asyncio
    async def test_picks_nearest_strike_for_pe(self):
        fyers = MagicMock()
        fyers.get_option_chain = AsyncMock(return_value=_real_shaped_option_chain())
        contract = await orch._resolve_option_contract(fyers, "PE", 23105.0, _ist(2026, 9, 25, 10, 0))
        assert contract is not None
        assert contract.symbol == "NSE:NIFTY26SEP23100PE"

    @pytest.mark.asyncio
    async def test_computes_iv_via_black_scholes(self):
        fyers = MagicMock()
        fyers.get_option_chain = AsyncMock(return_value=_real_shaped_option_chain())
        contract = await orch._resolve_option_contract(fyers, "CE", 23140.0, _ist(2026, 9, 25, 10, 0))
        assert contract.iv is not None
        # Sane band for a real short-dated NIFTY premium -- not a tight
        # pin (the exact value depends on years-to-expiry at test time
        # relative to the fixture's hardcoded 29-Sep-2026 expiry), just
        # confirms the solver produced a real, plausible number rather
        # than None or something wildly outside a realistic IV range.
        assert 0.01 < contract.iv < 3.0

    @pytest.mark.asyncio
    async def test_returns_none_when_chain_call_fails(self):
        fyers = MagicMock()
        fyers.get_option_chain = AsyncMock(return_value={})
        contract = await orch._resolve_option_contract(fyers, "CE", 23140.0, _ist(2026, 9, 25, 10, 0))
        assert contract is None

    @pytest.mark.asyncio
    async def test_returns_none_when_chain_has_no_candidates_for_direction(self):
        fyers = MagicMock()
        chain = _real_shaped_option_chain()
        # Strip every PE leg -- a CE-only chain should not fabricate a PE pick.
        chain["data"]["optionsChain"] = [
            r for r in chain["data"]["optionsChain"] if r.get("option_type") != "PE"
        ]
        fyers.get_option_chain = AsyncMock(return_value=chain)
        contract = await orch._resolve_option_contract(fyers, "PE", 23140.0, _ist(2026, 9, 25, 10, 0))
        assert contract is None

    @pytest.mark.asyncio
    async def test_degrades_to_spot_when_forward_missing(self):
        """The index row's `fp` field is the correct Black-Scholes
        forward; if it's ever absent, resolution should still produce a
        contract (degraded to spot) rather than fail outright."""
        fyers = MagicMock()
        chain = _real_shaped_option_chain()
        for row in chain["data"]["optionsChain"]:
            if row.get("option_type") == "":
                row.pop("fp", None)
        fyers.get_option_chain = AsyncMock(return_value=chain)
        contract = await orch._resolve_option_contract(fyers, "CE", 23140.0, _ist(2026, 9, 25, 10, 0))
        assert contract is not None


class TestHandleTvEntrySignal:
    @pytest.mark.asyncio
    async def test_stale_signal_is_rejected_without_executing(self, tmp_path):
        db_path = str(tmp_path / "orch.db")
        await _init_dbs(db_path)
        fyers = _mock_fyers_with_candles()
        stale_time = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        result = await orch.handle_tv_entry_signal(
            fyers, db_path, _signal_payload(signal_time=stale_time),
        )
        assert result["executed"] is False
        assert result["reason"] == "signal_stale"
        assert await open_positions(db_path, "TV_FYERS_PAPER") == []

    @pytest.mark.asyncio
    async def test_chain_unavailable_blocks_execution_without_fabricating(self, tmp_path):
        """When Fyers' options chain can't be fetched, no trade -- never
        a silently fabricated contract."""
        db_path = str(tmp_path / "orch.db")
        await _init_dbs(db_path)
        fyers = _mock_fyers_with_candles()
        fyers.get_option_chain = AsyncMock(return_value={})
        result = await orch.handle_tv_entry_signal(fyers, db_path, _signal_payload())
        assert result["executed"] is False
        assert result["reason"] == "symbol_resolution_unavailable"

    @pytest.mark.asyncio
    async def test_happy_path_executes_and_persists_position(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "orch.db")
        await _init_dbs(db_path)
        fyers = _mock_fyers_with_candles()
        monkeypatch.setattr(orch, "_resolve_option_contract", AsyncMock(return_value=_good_contract()))
        monkeypatch.setattr(orch, "evaluate_trend_with_ai", AsyncMock(return_value=(True, "ai_pass_test_stub")))

        with patch("operator_alert.notify_operator", AsyncMock(return_value=True)) as mock_notify:
            result = await orch.handle_tv_entry_signal(fyers, db_path, _signal_payload())
        assert result["executed"] is True, result["reason"]
        mock_notify.assert_awaited_once()
        assert "tv_fyers_entry_alert" == mock_notify.call_args.kwargs.get("event")

        open_now = await open_positions(db_path, "TV_FYERS_PAPER")
        assert len(open_now) == 1
        pos = open_now[0]
        assert pos.symbol == "NSE:NIFTY24SEP24500CE"
        assert pos.entry_premium == 120.5  # paper fill at ask
        assert pos.direction == "CE"
        assert pos.stop_underlying < pos.entry_underlying  # CE stop below entry
        assert pos.target_underlying > pos.entry_underlying

    @pytest.mark.asyncio
    async def test_alert_delivery_failure_never_blocks_the_trade(self, tmp_path, monkeypatch):
        """operator_alert.notify_operator's whole contract is "never raises"
        -- this proves the entry path doesn't depend on that contract
        holding: even if it somehow raised, the trade must already be
        recorded before the alert is even attempted."""
        db_path = str(tmp_path / "orch.db")
        await _init_dbs(db_path)
        fyers = _mock_fyers_with_candles()
        monkeypatch.setattr(orch, "_resolve_option_contract", AsyncMock(return_value=_good_contract()))
        monkeypatch.setattr(orch, "evaluate_trend_with_ai", AsyncMock(return_value=(True, "ai_pass_test_stub")))

        with patch("operator_alert.notify_operator", AsyncMock(side_effect=RuntimeError("telegram down"))):
            result = await orch.handle_tv_entry_signal(fyers, db_path, _signal_payload())
        assert result["executed"] is True, result["reason"]
        assert len(await open_positions(db_path, "TV_FYERS_PAPER")) == 1

    @pytest.mark.asyncio
    async def test_gate_rejection_blocks_execution(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "orch.db")
        await _init_dbs(db_path)
        fyers = _mock_fyers_with_candles()
        # Illiquid contract: thin OI should trip the liquidity veto.
        thin_contract = orch.ResolvedContract(
            symbol="NSE:NIFTY24SEP24500CE", lot_size=50, bid=119.5, ask=120.5,
            oi=10, volume=5, iv=0.20, quote_age_sec=5.0,
        )
        monkeypatch.setattr(orch, "_resolve_option_contract", AsyncMock(return_value=thin_contract))

        result = await orch.handle_tv_entry_signal(fyers, db_path, _signal_payload())
        assert result["executed"] is False
        assert result["reason"] == "oi_too_thin"
        assert await open_positions(db_path, "TV_FYERS_PAPER") == []

    @pytest.mark.asyncio
    async def test_no_key_skips_ai_gate_and_executes_on_deterministic_gates_alone(self, tmp_path, monkeypatch):
        """[COST 2026-09-25] No TV_FYERS_ANTHROPIC_API_KEY configured --
        the real, default, free-of-charge state. A signal that clears the
        deterministic ladder (RSI included) must execute without ever
        attempting an AI call -- the API is a real paid cost with no free
        tier, so this pipeline must keep working, at zero cost, with no
        key set at all."""
        db_path = str(tmp_path / "orch.db")
        await _init_dbs(db_path)
        fyers = _mock_fyers_with_candles()
        monkeypatch.setattr(orch, "_resolve_option_contract", AsyncMock(return_value=_good_contract()))
        ai_mock = AsyncMock()
        monkeypatch.setattr(orch, "evaluate_trend_with_ai", ai_mock)

        with patch("operator_alert.notify_operator", AsyncMock(return_value=True)):
            result = await orch.handle_tv_entry_signal(fyers, db_path, _signal_payload())
        assert result["executed"] is True, result["reason"]
        ai_mock.assert_not_called()
        assert len(await open_positions(db_path, "TV_FYERS_PAPER")) == 1

    @pytest.mark.asyncio
    async def test_ai_gate_rejection_blocks_execution_when_key_configured(self, tmp_path, monkeypatch):
        """With a key configured, the AI gate becomes an ADDITIONAL check
        on top of the (already-passing) deterministic ladder -- not a
        replacement for it."""
        db_path = str(tmp_path / "orch.db")
        await _init_dbs(db_path)
        from config import settings
        monkeypatch.setattr(settings, "TV_FYERS_ANTHROPIC_API_KEY", "test-key")
        fyers = _mock_fyers_with_candles()
        monkeypatch.setattr(orch, "_resolve_option_contract", AsyncMock(return_value=_good_contract()))
        monkeypatch.setattr(orch, "evaluate_trend_with_ai", AsyncMock(return_value=(False, "ai_reject_choppy_no_clear_trend")))

        result = await orch.handle_tv_entry_signal(fyers, db_path, _signal_payload())
        assert result["executed"] is False
        assert result["reason"] == "ai_reject_choppy_no_clear_trend"
        assert await open_positions(db_path, "TV_FYERS_PAPER") == []

    @pytest.mark.asyncio
    async def test_concurrency_cap_blocks_execution(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "orch.db")
        await _init_dbs(db_path)
        from config import settings
        monkeypatch.setattr(settings, "TV_FYERS_MAX_CONCURRENT", 1)
        await insert_position(
            db_path, source="TV_FYERS_PAPER", signal_id="prior", symbol="NSE:NIFTY24SEP24000CE",
            direction="CE", qty=50, entry_time=datetime.now().isoformat(),
            entry_premium=100.0, entry_underlying=24000.0, stop_underlying=23900.0,
            target_underlying=24200.0, premium_stop=75.0, best_underlying=24000.0,
            atr_at_entry=50.0,
        )
        fyers = _mock_fyers_with_candles()
        monkeypatch.setattr(orch, "_resolve_option_contract", AsyncMock(return_value=_good_contract()))
        result = await orch.handle_tv_entry_signal(fyers, db_path, _signal_payload())
        assert result["executed"] is False
        assert result["reason"] == "concurrency_cap"


class TestHandleAiCandleScan:
    """The autonomous entry path -- no TradingView alert at all. Every
    tick, including NO_TRADE, must persist a tv_fyers_signals row (the
    dashboard's whole point is showing what the AI saw and decided)."""

    @pytest.mark.asyncio
    async def test_no_key_skips_entirely_without_fetching_candles(self, tmp_path, monkeypatch):
        """The default, free state: no TV_FYERS_ANTHROPIC_API_KEY. This
        whole path must cost nothing and touch nothing -- not even a
        candle fetch -- rather than fail closed loudly every 5 minutes."""
        db_path = str(tmp_path / "orch.db")
        await _init_dbs(db_path)
        fyers = _mock_fyers_with_candles()
        ai_mock = AsyncMock()
        monkeypatch.setattr(orch, "analyze_and_plan_trade", ai_mock)

        result = await orch.handle_ai_candle_scan(fyers, db_path, _ist(2026, 9, 25, 10, 0))
        assert result["executed"] is False
        assert result["reason"] == "ai_unavailable_no_key"
        assert result["signal_id"] is None
        ai_mock.assert_not_called()
        fyers.get_historical.assert_not_called()
        assert await open_positions(db_path, "TV_FYERS_PAPER") == []

    @pytest.mark.asyncio
    async def test_no_trade_decision_persists_signal_without_executing(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "orch.db")
        await _init_dbs(db_path)
        from config import settings
        monkeypatch.setattr(settings, "TV_FYERS_ANTHROPIC_API_KEY", "test-key")
        fyers = _mock_fyers_with_candles()
        monkeypatch.setattr(orch, "analyze_and_plan_trade", AsyncMock(return_value=(None, "ai_no_trade_choppy")))

        result = await orch.handle_ai_candle_scan(fyers, db_path, _ist(2026, 9, 25, 10, 0))
        assert result["executed"] is False
        assert result["reason"] == "ai_no_trade_choppy"
        assert result["signal_id"] is not None
        assert await open_positions(db_path, "TV_FYERS_PAPER") == []

    @pytest.mark.asyncio
    async def test_paper_disabled_short_circuits_before_any_ai_call(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "orch.db")
        await _init_dbs(db_path)
        from config import settings
        monkeypatch.setattr(settings, "TV_FYERS_DISABLE_PAPER", True)
        fyers = _mock_fyers_with_candles()
        ai_mock = AsyncMock()
        monkeypatch.setattr(orch, "analyze_and_plan_trade", ai_mock)

        result = await orch.handle_ai_candle_scan(fyers, db_path, _ist(2026, 9, 25, 10, 0))
        assert result["executed"] is False
        assert result["reason"] == "paper_disabled"
        ai_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_happy_path_executes_and_persists_position(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "orch.db")
        await _init_dbs(db_path)
        from config import settings
        monkeypatch.setattr(settings, "TV_FYERS_ANTHROPIC_API_KEY", "test-key")
        fyers = _mock_fyers_with_candles()
        plan = orch.AiTradePlan(
            direction="CE", stop_underlying=24400.0, target_underlying=24700.0,
            reason="strong_uptrend", confidence=0.85,
        )
        monkeypatch.setattr(orch, "analyze_and_plan_trade", AsyncMock(return_value=(plan, "ai_plan_strong_uptrend")))
        monkeypatch.setattr(orch, "_resolve_option_contract", AsyncMock(return_value=_good_contract()))

        with patch("operator_alert.notify_operator", AsyncMock(return_value=True)) as mock_notify:
            result = await orch.handle_ai_candle_scan(fyers, db_path, _ist(2026, 9, 25, 10, 0))
        assert result["executed"] is True, result["reason"]
        mock_notify.assert_awaited_once()

        open_now = await open_positions(db_path, "TV_FYERS_PAPER")
        assert len(open_now) == 1
        pos = open_now[0]
        assert pos.direction == "CE"
        assert pos.stop_underlying == 24400.0
        assert pos.target_underlying == 24700.0
        assert pos.signal_id == result["signal_id"]

    @pytest.mark.asyncio
    async def test_deterministic_gate_still_blocks_an_ai_proposed_trade(self, tmp_path, monkeypatch):
        """The AI deciding to trade is not enough -- IV/liquidity still
        has to check out, exactly like the TradingView-triggered path."""
        db_path = str(tmp_path / "orch.db")
        await _init_dbs(db_path)
        from config import settings
        monkeypatch.setattr(settings, "TV_FYERS_ANTHROPIC_API_KEY", "test-key")
        fyers = _mock_fyers_with_candles()
        plan = orch.AiTradePlan(
            direction="CE", stop_underlying=24400.0, target_underlying=24700.0,
            reason="strong_uptrend", confidence=0.85,
        )
        monkeypatch.setattr(orch, "analyze_and_plan_trade", AsyncMock(return_value=(plan, "ai_plan_strong_uptrend")))
        thin_contract = orch.ResolvedContract(
            symbol="NSE:NIFTY24SEP24500CE", lot_size=65, bid=119.5, ask=120.5,
            oi=10, volume=5, iv=0.20, quote_age_sec=5.0,
        )
        monkeypatch.setattr(orch, "_resolve_option_contract", AsyncMock(return_value=thin_contract))

        result = await orch.handle_ai_candle_scan(fyers, db_path, _ist(2026, 9, 25, 10, 0))
        assert result["executed"] is False
        assert result["reason"] == "oi_too_thin"
        assert await open_positions(db_path, "TV_FYERS_PAPER") == []

    @pytest.mark.asyncio
    async def test_concurrency_cap_blocks_ai_proposed_trade(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "orch.db")
        await _init_dbs(db_path)
        from config import settings
        monkeypatch.setattr(settings, "TV_FYERS_ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(settings, "TV_FYERS_MAX_CONCURRENT", 1)
        await insert_position(
            db_path, source="TV_FYERS_PAPER", signal_id="prior", symbol="NSE:NIFTY24SEP24000CE",
            direction="CE", qty=50, entry_time=datetime.now().isoformat(),
            entry_premium=100.0, entry_underlying=24000.0, stop_underlying=23900.0,
            target_underlying=24200.0, premium_stop=75.0, best_underlying=24000.0,
            atr_at_entry=50.0,
        )
        fyers = _mock_fyers_with_candles()
        plan = orch.AiTradePlan(
            direction="CE", stop_underlying=24400.0, target_underlying=24700.0,
            reason="strong_uptrend", confidence=0.85,
        )
        monkeypatch.setattr(orch, "analyze_and_plan_trade", AsyncMock(return_value=(plan, "ai_plan_strong_uptrend")))
        monkeypatch.setattr(orch, "_resolve_option_contract", AsyncMock(return_value=_good_contract()))

        result = await orch.handle_ai_candle_scan(fyers, db_path, _ist(2026, 9, 25, 10, 0))
        assert result["executed"] is False
        assert result["reason"] == "concurrency_cap"


class TestRunTvFyersExitTick:
    def _open_position(self, **overrides):
        base = dict(
            source="TV_FYERS_PAPER", signal_id="sig-x", symbol="NSE:NIFTY24SEP24500CE",
            direction="CE", qty=50, entry_time=datetime.now().isoformat(),
            entry_premium=120.0, entry_underlying=24500.0, stop_underlying=24400.0,
            target_underlying=24680.0, premium_stop=90.0, best_underlying=24500.0,
            atr_at_entry=50.0,
        )
        base.update(overrides)
        return base

    @pytest.mark.asyncio
    async def test_no_open_positions_is_a_noop(self, tmp_path):
        db_path = str(tmp_path / "exit.db")
        await init_tv_fyers_positions_db(db_path)
        fyers = MagicMock()
        fyers.get_quote = AsyncMock(return_value={})
        out = await orch.run_tv_fyers_exit_tick(fyers, db_path, _ist(2026, 9, 25, 10, 0))
        assert out == {"exits": []}

    @pytest.mark.asyncio
    async def test_underlying_stop_closes_position(self, tmp_path):
        db_path = str(tmp_path / "exit.db")
        await init_tv_fyers_positions_db(db_path)
        await insert_position(db_path, **self._open_position())

        fyers = MagicMock()
        async def _quote(symbols):
            if symbols == [orch._UNDERLYING_INDEX_SYMBOL]:
                return {orch._UNDERLYING_INDEX_SYMBOL: {"lp": 24350.0}}  # below stop
            return {symbols[0]: {"bid": 85.0, "lp": 85.0}}
        fyers.get_quote = AsyncMock(side_effect=_quote)
        fyers.place_order = AsyncMock()  # must never be called (paper mode)

        with patch("operator_alert.notify_operator", AsyncMock(return_value=True)) as mock_notify:
            out = await orch.run_tv_fyers_exit_tick(fyers, db_path, _ist(2026, 9, 25, 10, 0))
        assert len(out["exits"]) == 1
        assert out["exits"][0]["reason"] == "underlying_stop"
        assert await open_positions(db_path, "TV_FYERS_PAPER") == []
        fyers.place_order.assert_not_called()
        mock_notify.assert_awaited_once()
        assert mock_notify.call_args.kwargs.get("event") == "tv_fyers_exit_alert"

    @pytest.mark.asyncio
    async def test_target_arms_trail_without_exiting_yet(self, tmp_path):
        db_path = str(tmp_path / "exit.db")
        await init_tv_fyers_positions_db(db_path)
        await insert_position(db_path, **self._open_position())

        fyers = MagicMock()
        async def _quote(symbols):
            if symbols == [orch._UNDERLYING_INDEX_SYMBOL]:
                return {orch._UNDERLYING_INDEX_SYMBOL: {"lp": 24700.0}}  # past target
            return {symbols[0]: {"bid": 140.0, "lp": 140.0}}
        fyers.get_quote = AsyncMock(side_effect=_quote)

        out = await orch.run_tv_fyers_exit_tick(fyers, db_path, _ist(2026, 9, 25, 10, 0))
        assert out["exits"] == []  # not exited, just trail-armed
        open_now = await open_positions(db_path, "TV_FYERS_PAPER")
        assert open_now[0].trail_active == 1

    @pytest.mark.asyncio
    async def test_premium_backstop_fires_even_without_spot_quote(self, tmp_path):
        db_path = str(tmp_path / "exit.db")
        await init_tv_fyers_positions_db(db_path)
        await insert_position(db_path, **self._open_position())

        fyers = MagicMock()
        async def _quote(symbols):
            if symbols == [orch._UNDERLYING_INDEX_SYMBOL]:
                return {}  # no spot quote available
            return {symbols[0]: {"bid": 85.0, "lp": 85.0}}  # below premium_stop=90
        fyers.get_quote = AsyncMock(side_effect=_quote)

        out = await orch.run_tv_fyers_exit_tick(fyers, db_path, _ist(2026, 9, 25, 10, 0))
        assert len(out["exits"]) == 1
        assert out["exits"][0]["reason"] == "premium_backstop"

    @pytest.mark.asyncio
    async def test_hard_flat_forces_exit_at_1510(self, tmp_path):
        db_path = str(tmp_path / "exit.db")
        await init_tv_fyers_positions_db(db_path)
        await insert_position(db_path, **self._open_position())

        fyers = MagicMock()
        async def _quote(symbols):
            if symbols == [orch._UNDERLYING_INDEX_SYMBOL]:
                return {orch._UNDERLYING_INDEX_SYMBOL: {"lp": 24500.0}}  # flat, no stop/target hit
            return {symbols[0]: {"bid": 120.0, "lp": 120.0}}
        fyers.get_quote = AsyncMock(side_effect=_quote)

        out = await orch.run_tv_fyers_exit_tick(fyers, db_path, _ist(2026, 9, 25, 15, 12))
        assert len(out["exits"]) == 1
        assert out["exits"][0]["reason"] == "hard_flat_1510"

    @pytest.mark.asyncio
    async def test_time_stop_defers_when_profitable(self, tmp_path):
        db_path = str(tmp_path / "exit.db")
        await init_tv_fyers_positions_db(db_path)
        old_entry = (_ist(2026, 9, 25, 10, 0) - timedelta(minutes=60)).isoformat()
        await insert_position(db_path, **self._open_position(entry_time=old_entry))

        fyers = MagicMock()
        async def _quote(symbols):
            if symbols == [orch._UNDERLYING_INDEX_SYMBOL]:
                return {orch._UNDERLYING_INDEX_SYMBOL: {"lp": 24505.0}}  # barely moved
            return {symbols[0]: {"bid": 125.0, "lp": 125.0}}  # but premium is UP (profitable)
        fyers.get_quote = AsyncMock(side_effect=_quote)

        out = await orch.run_tv_fyers_exit_tick(fyers, db_path, _ist(2026, 9, 25, 10, 0))
        assert out["exits"] == []  # deferred: profitable on premium despite stalled underlying
        assert len(await open_positions(db_path, "TV_FYERS_PAPER")) == 1

    @pytest.mark.asyncio
    async def test_time_stop_fires_when_not_profitable(self, tmp_path):
        db_path = str(tmp_path / "exit.db")
        await init_tv_fyers_positions_db(db_path)
        old_entry = (_ist(2026, 9, 25, 10, 0) - timedelta(minutes=60)).isoformat()
        await insert_position(db_path, **self._open_position(entry_time=old_entry))

        fyers = MagicMock()
        async def _quote(symbols):
            if symbols == [orch._UNDERLYING_INDEX_SYMBOL]:
                return {orch._UNDERLYING_INDEX_SYMBOL: {"lp": 24505.0}}  # barely moved
            return {symbols[0]: {"bid": 118.0, "lp": 118.0}}  # premium flat/down
        fyers.get_quote = AsyncMock(side_effect=_quote)

        out = await orch.run_tv_fyers_exit_tick(fyers, db_path, _ist(2026, 9, 25, 10, 0))
        assert len(out["exits"]) == 1
        assert out["exits"][0]["reason"] == "time_stop"
