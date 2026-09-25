"""
Tests for tv_fyers_orchestrator.py -- entry (event-driven) and exit
(polling) paths for the TradingView -> Fyers pipeline.

Entry-path happy-path tests monkeypatch _resolve_option_contract (an
explicit stub in v1, see the module's docstring) to return a synthetic
ResolvedContract, so the rest of the pipeline -- staleness, RSI/ATR,
double-check gates, sizing, paper execution, position persistence -- is
exercised end-to-end without needing real Fyers instrument data.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

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
        symbol="NSE:NIFTY24SEP24500CE", lot_size=50, bid=119.5, ask=120.5,
        oi=10_000, volume=5_000, iv=0.20, quote_age_sec=5.0,
    )


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
    async def test_symbol_resolution_stub_blocks_execution_by_default(self, tmp_path):
        """Confirms the v1 stub behaves honestly: no contract resolver
        wired up yet -> no trade, not a silent fabrication."""
        db_path = str(tmp_path / "orch.db")
        await _init_dbs(db_path)
        fyers = _mock_fyers_with_candles()
        result = await orch.handle_tv_entry_signal(fyers, db_path, _signal_payload())
        assert result["executed"] is False
        assert result["reason"] == "symbol_resolution_unavailable"

    @pytest.mark.asyncio
    async def test_happy_path_executes_and_persists_position(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "orch.db")
        await _init_dbs(db_path)
        fyers = _mock_fyers_with_candles()
        monkeypatch.setattr(orch, "_resolve_option_contract", AsyncMock(return_value=_good_contract()))

        result = await orch.handle_tv_entry_signal(fyers, db_path, _signal_payload())
        assert result["executed"] is True, result["reason"]

        open_now = await open_positions(db_path, "TV_FYERS_PAPER")
        assert len(open_now) == 1
        pos = open_now[0]
        assert pos.symbol == "NSE:NIFTY24SEP24500CE"
        assert pos.entry_premium == 120.5  # paper fill at ask
        assert pos.direction == "CE"
        assert pos.stop_underlying < pos.entry_underlying  # CE stop below entry
        assert pos.target_underlying > pos.entry_underlying

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

        out = await orch.run_tv_fyers_exit_tick(fyers, db_path, _ist(2026, 9, 25, 10, 0))
        assert len(out["exits"]) == 1
        assert out["exits"][0]["reason"] == "underlying_stop"
        assert await open_positions(db_path, "TV_FYERS_PAPER") == []
        fyers.place_order.assert_not_called()

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
