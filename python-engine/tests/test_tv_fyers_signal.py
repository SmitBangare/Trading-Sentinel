"""
Unit tests for tv_fyers_signal.py -- TvFyersSignal model + ingest/log
functions (smit_fyers_options branch).
"""
from __future__ import annotations

import pytest
import aiosqlite

from tv_fyers_signal import (
    TvFyersSignal,
    init_tv_fyers_signal_db,
    ingest_tv_signal,
    mark_signal_handled,
)


def _payload(**overrides) -> dict:
    base = {
        "signal_id": "sig-1",
        "symbol": "NIFTY",
        "direction": "CE",
        "underlying_price": 24500.5,
        "strategy": "orb_momentum_v1",
        "signal_time": "2026-09-25T09:47:00+05:30",
    }
    base.update(overrides)
    return base


class TestTvFyersSignalModel:
    def test_valid_payload_parses(self):
        sig = TvFyersSignal.model_validate(_payload())
        assert sig.direction == "CE"
        assert sig.underlying_price == 24500.5

    def test_direction_must_be_ce_or_pe(self):
        with pytest.raises(Exception):
            TvFyersSignal.model_validate(_payload(direction="LONG"))

    def test_underlying_price_must_be_positive(self):
        with pytest.raises(Exception):
            TvFyersSignal.model_validate(_payload(underlying_price=-1))

    def test_strategy_is_optional(self):
        payload = _payload()
        del payload["strategy"]
        sig = TvFyersSignal.model_validate(payload)
        assert sig.strategy is None


class TestIngestTvSignal:
    @pytest.mark.asyncio
    async def test_ingest_persists_and_acks(self, tmp_path):
        db_path = str(tmp_path / "tv_test.db")
        await init_tv_fyers_signal_db(db_path)
        out = await ingest_tv_signal(db_path, _payload())
        assert out == {"received": True, "signal_id": "sig-1"}

        async with aiosqlite.connect(db_path) as db:
            cur = await db.execute(
                "SELECT symbol, direction, handled FROM tv_fyers_signals WHERE signal_id = ?",
                ("sig-1",),
            )
            row = await cur.fetchone()
        assert row == ("NIFTY", "CE", 0)

    @pytest.mark.asyncio
    async def test_ingest_rejects_malformed_payload(self, tmp_path):
        db_path = str(tmp_path / "tv_test.db")
        await init_tv_fyers_signal_db(db_path)
        with pytest.raises(Exception):
            await ingest_tv_signal(db_path, _payload(direction="INVALID"))

    @pytest.mark.asyncio
    async def test_duplicate_signal_id_is_ignored_not_errored(self, tmp_path):
        db_path = str(tmp_path / "tv_test.db")
        await init_tv_fyers_signal_db(db_path)
        await ingest_tv_signal(db_path, _payload())
        # Second call with the same signal_id must not raise (INSERT OR IGNORE).
        out = await ingest_tv_signal(db_path, _payload(underlying_price=99999))
        assert out == {"received": True, "signal_id": "sig-1"}

        async with aiosqlite.connect(db_path) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM tv_fyers_signals WHERE signal_id = ?", ("sig-1",)
            )
            (count,) = await cur.fetchone()
        assert count == 1

    @pytest.mark.asyncio
    async def test_mark_signal_handled_updates_row(self, tmp_path):
        db_path = str(tmp_path / "tv_test.db")
        await init_tv_fyers_signal_db(db_path)
        await ingest_tv_signal(db_path, _payload())
        await mark_signal_handled(db_path, "sig-1", "rejected:rsi_overbought")

        async with aiosqlite.connect(db_path) as db:
            cur = await db.execute(
                "SELECT handled, handled_result FROM tv_fyers_signals WHERE signal_id = ?",
                ("sig-1",),
            )
            row = await cur.fetchone()
        assert row == (1, "rejected:rsi_overbought")
