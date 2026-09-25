"""
Tests for tv_fyers_executor.py -- TvFyersExecutor (smit_fyers_options branch).

Mocks the FyersClient instance directly (not the fyers_apiv3 SDK -- that's
fyers_client.py's own concern, already tested in test_fyers_client.py).
Structure mirrors what fno_executor.py's equivalent coverage would look
like: paper mode never touches the broker, live mode calls through
correctly.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from tv_fyers_executor import TvFyersExecutor


def _mock_fyers():
    fyers = MagicMock()
    fyers.place_order = AsyncMock()
    fyers.order_history = AsyncMock()
    fyers.cancel_order = AsyncMock()
    return fyers


class TestPaperModeNeverTouchesBroker:
    @pytest.mark.asyncio
    async def test_paper_entry_never_calls_place_order(self):
        fyers = _mock_fyers()
        executor = TvFyersExecutor(fyers, paper_mode=True)
        out = await executor.execute_entry("NSE:NIFTY24SEP24500CE", 50, 120.5)
        assert out["status"] == "paper"
        assert out["fill_price"] == 120.5
        assert out["order_id"].startswith("PAPER-TVF-ENT-")
        fyers.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_paper_exit_never_calls_place_order(self):
        fyers = _mock_fyers()
        executor = TvFyersExecutor(fyers, paper_mode=True)
        out = await executor.execute_exit("NSE:NIFTY24SEP24500CE", 50, 118.0, tick_size=0.05)
        assert out["status"] == "paper"
        assert out["fill_price"] == 118.0
        assert out["order_id"].startswith("PAPER-TVF-EXT-")
        fyers.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_paper_hard_flat_exit_never_calls_place_order(self):
        fyers = _mock_fyers()
        executor = TvFyersExecutor(fyers, paper_mode=True)
        out = await executor.execute_exit(
            "NSE:NIFTY24SEP24500CE", 50, 118.0, tick_size=0.05, hard_flat=True,
        )
        assert out["status"] == "paper"
        fyers.place_order.assert_not_called()


class TestLiveModeEntry:
    @pytest.mark.asyncio
    async def test_live_entry_fills_successfully(self):
        fyers = _mock_fyers()
        fyers.place_order.return_value = {"order_id": "FY-1", "status": "PLACED"}
        fyers.order_history.return_value = [{"status": 2, "tradedPrice": 121.0}]
        executor = TvFyersExecutor(fyers, paper_mode=False)
        out = await executor.execute_entry("NSE:NIFTY24SEP24500CE", 50, 120.5)
        assert out == {"status": "filled", "order_id": "FY-1", "fill_price": 121.0}
        sent = fyers.place_order.call_args.kwargs
        assert sent["transaction_type"] == "BUY"
        assert sent["intent"] == "entry"

    @pytest.mark.asyncio
    async def test_live_entry_times_out_and_cancels_never_chases(self):
        fyers = _mock_fyers()
        fyers.place_order.return_value = {"order_id": "FY-2", "status": "PLACED"}
        fyers.order_history.return_value = [{"status": 6}]  # pending forever
        executor = TvFyersExecutor(fyers, paper_mode=False)
        executor.fill_timeout_sec = 0.1
        executor.poll_interval_sec = 0.05
        out = await executor.execute_entry("NSE:NIFTY24SEP24500CE", 50, 120.5)
        assert out["status"] == "timeout"
        fyers.cancel_order.assert_called_once_with("FY-2")

    @pytest.mark.asyncio
    async def test_live_entry_rejected_at_broker_returns_rejected(self):
        fyers = _mock_fyers()
        fyers.place_order.return_value = {"order_id": None, "status": "ERROR"}
        executor = TvFyersExecutor(fyers, paper_mode=False)
        out = await executor.execute_entry("NSE:NIFTY24SEP24500CE", 50, 120.5)
        assert out["status"] == "rejected"
        fyers.order_history.assert_not_called()

    @pytest.mark.asyncio
    async def test_filled_with_no_price_field_treated_as_no_fill(self):
        fyers = _mock_fyers()
        fyers.place_order.return_value = {"order_id": "FY-3", "status": "PLACED"}
        fyers.order_history.return_value = [{"status": 2}]  # filled, no price
        executor = TvFyersExecutor(fyers, paper_mode=False)
        executor.fill_timeout_sec = 0.1
        executor.poll_interval_sec = 0.05
        out = await executor.execute_entry("NSE:NIFTY24SEP24500CE", 50, 120.5)
        assert out["status"] == "timeout"  # never filled -> falls through to timeout/cancel


class TestLiveModeExit:
    @pytest.mark.asyncio
    async def test_live_exit_fills_successfully(self):
        fyers = _mock_fyers()
        fyers.place_order.return_value = {"order_id": "FY-4", "status": "PLACED"}
        fyers.order_history.return_value = [{"status": 2, "tradedPrice": 117.5}]
        executor = TvFyersExecutor(fyers, paper_mode=False)
        out = await executor.execute_exit("NSE:NIFTY24SEP24500CE", 50, 118.0, tick_size=0.05)
        assert out == {"status": "filled", "order_id": "FY-4", "fill_price": 117.5}
        sent = fyers.place_order.call_args.kwargs
        assert sent["transaction_type"] == "SELL"
        assert sent["intent"] == "exit"

    @pytest.mark.asyncio
    async def test_live_exit_ambiguous_fill_is_not_retried_blindly(self):
        fyers = _mock_fyers()
        fyers.place_order.return_value = {"order_id": "FY-5", "status": "PLACED"}
        fyers.order_history.return_value = []  # no info either way
        executor = TvFyersExecutor(fyers, paper_mode=False)
        out = await executor.execute_exit("NSE:NIFTY24SEP24500CE", 50, 118.0, tick_size=0.05)
        assert out["status"] == "unfilled"

    @pytest.mark.asyncio
    async def test_hard_flat_uses_deep_marketable_limit_and_pages_operator_if_unfilled(self):
        fyers = _mock_fyers()
        fyers.place_order.return_value = {"order_id": "FY-6", "status": "PLACED"}
        fyers.order_history.return_value = [{"status": 6}]  # never fills
        executor = TvFyersExecutor(fyers, paper_mode=False)
        executor.fill_timeout_sec = 0.1
        executor.poll_interval_sec = 0.05
        out = await executor.execute_exit(
            "NSE:NIFTY24SEP24500CE", 50, 118.0, tick_size=0.05, hard_flat=True,
        )
        assert out["status"] == "unfilled"
        sent_price = fyers.place_order.call_args.kwargs["limit_price"]
        assert sent_price < 118.0  # marketable limit priced through the bid
