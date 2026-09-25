"""
Unit tests for tv_fyers_positions.py -- the simplified (deliberately
paper-only-scoped, see module docstring) position store.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from tv_fyers_positions import (
    init_tv_fyers_positions_db, insert_position, open_positions,
    update_trail, claim_exit, unclaim_exit, settle_position_close,
)


def _position_fields(**overrides):
    base = dict(
        source="TV_FYERS_PAPER", signal_id="sig-1", symbol="NSE:NIFTY24SEP24500CE",
        direction="CE", qty=50, entry_time="2026-09-25T09:47:00+05:30",
        entry_premium=120.0, entry_underlying=24500.0, stop_underlying=24400.0,
        target_underlying=24680.0, premium_stop=90.0, best_underlying=24500.0,
        atr_at_entry=50.0,
    )
    base.update(overrides)
    return base


class TestPositionLifecycle:
    @pytest.mark.asyncio
    async def test_insert_and_read_open_position(self, tmp_path):
        db_path = str(tmp_path / "pos_test.db")
        await init_tv_fyers_positions_db(db_path)
        pos_id = await insert_position(db_path, **_position_fields())
        assert pos_id > 0

        open_now = await open_positions(db_path, "TV_FYERS_PAPER")
        assert len(open_now) == 1
        assert open_now[0].id == pos_id
        assert open_now[0].status == "OPEN"
        assert open_now[0].symbol == "NSE:NIFTY24SEP24500CE"

    @pytest.mark.asyncio
    async def test_different_source_is_isolated(self, tmp_path):
        db_path = str(tmp_path / "pos_test.db")
        await init_tv_fyers_positions_db(db_path)
        await insert_position(db_path, **_position_fields())
        open_live = await open_positions(db_path, "TV_FYERS_LIVE")
        assert open_live == []

    @pytest.mark.asyncio
    async def test_update_trail_persists(self, tmp_path):
        db_path = str(tmp_path / "pos_test.db")
        await init_tv_fyers_positions_db(db_path)
        pos_id = await insert_position(db_path, **_position_fields())
        await update_trail(db_path, pos_id, 1, 24450.0, 24600.0)
        open_now = await open_positions(db_path, "TV_FYERS_PAPER")
        assert open_now[0].trail_active == 1
        assert open_now[0].trail_stop_underlying == 24450.0
        assert open_now[0].best_underlying == 24600.0

    @pytest.mark.asyncio
    async def test_claim_exit_only_succeeds_once(self, tmp_path):
        db_path = str(tmp_path / "pos_test.db")
        await init_tv_fyers_positions_db(db_path)
        pos_id = await insert_position(db_path, **_position_fields())
        first = await claim_exit(db_path, pos_id)
        second = await claim_exit(db_path, pos_id)
        assert first is True
        assert second is False  # already CLOSING, not OPEN anymore

    @pytest.mark.asyncio
    async def test_claiming_position_disappears_from_open_positions(self, tmp_path):
        db_path = str(tmp_path / "pos_test.db")
        await init_tv_fyers_positions_db(db_path)
        pos_id = await insert_position(db_path, **_position_fields())
        await claim_exit(db_path, pos_id)
        assert await open_positions(db_path, "TV_FYERS_PAPER") == []

    @pytest.mark.asyncio
    async def test_unclaim_exit_restores_to_open(self, tmp_path):
        db_path = str(tmp_path / "pos_test.db")
        await init_tv_fyers_positions_db(db_path)
        pos_id = await insert_position(db_path, **_position_fields())
        await claim_exit(db_path, pos_id)
        await unclaim_exit(db_path, pos_id)
        open_now = await open_positions(db_path, "TV_FYERS_PAPER")
        assert len(open_now) == 1
        assert open_now[0].id == pos_id

    @pytest.mark.asyncio
    async def test_settle_position_close_removes_from_open(self, tmp_path):
        db_path = str(tmp_path / "pos_test.db")
        await init_tv_fyers_positions_db(db_path)
        pos_id = await insert_position(db_path, **_position_fields())
        await claim_exit(db_path, pos_id)
        await settle_position_close(
            db_path, pos_id,
            exit_time_ist=datetime(2026, 9, 25, 10, 0), exit_premium=140.0,
            exit_underlying=24700.0, exit_reason="target_hit", gross_pnl=1000.0,
            costs=50.0, pnl=950.0, r_multiple=1.5, exit_order_id="PAPER-1",
        )
        assert await open_positions(db_path, "TV_FYERS_PAPER") == []
