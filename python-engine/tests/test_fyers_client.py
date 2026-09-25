"""
Tests for fyers_client.py -- FyersClient (smit_fyers_options branch).

Mocks the fyers_apiv3 SDK object directly (fyers_client.FyersClient._fyers)
so no real Fyers calls are made, mirroring test_kite_client.py's approach
of mocking the transport layer rather than hitting a live broker.
"""
import os
import sys
import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fyers_client import FyersClient
from halt_switch import TradingHalted
from owner_entry_halt import EntryHaltVerdict


def _client_with_mock_sdk():
    client = FyersClient(db_path=":memory:")
    client._fyers = MagicMock()
    client.access_token = "test-token"
    return client


class TestFyersClientQuotes:
    @pytest.mark.asyncio
    async def test_get_quote_normalises_response(self):
        client = _client_with_mock_sdk()
        client._fyers.quotes.return_value = {
            "s": "ok",
            "d": [{"n": "NSE:NIFTY24SEP24500CE", "v": {"lp": 123.4}}],
        }
        out = await client.get_quote("NSE:NIFTY24SEP24500CE")
        assert out == {"NSE:NIFTY24SEP24500CE": {"lp": 123.4}}

    @pytest.mark.asyncio
    async def test_get_quote_empty_symbols_returns_empty(self):
        client = _client_with_mock_sdk()
        assert await client.get_quote([]) == {}
        client._fyers.quotes.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_quote_failed_response_returns_empty(self):
        client = _client_with_mock_sdk()
        client._fyers.quotes.return_value = {"s": "error", "message": "bad token"}
        out = await client.get_quote(["NSE:SBIN-EQ"])
        assert out == {}

    @pytest.mark.asyncio
    async def test_get_quote_raises_before_token_set(self):
        client = FyersClient(db_path=":memory:")
        with pytest.raises(RuntimeError):
            await client.get_quote(["NSE:SBIN-EQ"])


class TestFyersClientHistorical:
    @pytest.mark.asyncio
    async def test_get_historical_returns_candles(self):
        client = _client_with_mock_sdk()
        client._fyers.history.return_value = {
            "s": "ok",
            "candles": [[1727251200, 100, 105, 99, 103, 1000]],
        }
        out = await client.get_historical("NSE:NIFTY24SEP-INDEX", "5", "2026-09-20", "2026-09-25")
        assert out == [[1727251200, 100, 105, 99, 103, 1000]]

    @pytest.mark.asyncio
    async def test_get_historical_failed_response_returns_empty_list(self):
        client = _client_with_mock_sdk()
        client._fyers.history.return_value = {"s": "error"}
        out = await client.get_historical("NSE:NIFTY24SEP-INDEX", "5", "2026-09-20", "2026-09-25")
        assert out == []


class TestFyersClientOptionChain:
    @pytest.mark.asyncio
    async def test_get_option_chain_returns_raw_response_on_success(self):
        client = _client_with_mock_sdk()
        client._fyers.optionchain.return_value = {
            "s": "ok",
            "data": {"optionsChain": [{"symbol": "NSE:NIFTY25SEP24500CE", "ltp": 120.5}]},
        }
        out = await client.get_option_chain("NSE:NIFTY50-INDEX", strike_count=10)
        assert out["s"] == "ok"
        assert out["data"]["optionsChain"][0]["symbol"] == "NSE:NIFTY25SEP24500CE"
        sent_payload = client._fyers.optionchain.call_args[0][0]
        assert sent_payload == {"symbol": "NSE:NIFTY50-INDEX", "strikecount": 10, "timestamp": ""}

    @pytest.mark.asyncio
    async def test_get_option_chain_failed_response_returns_empty_dict(self):
        client = _client_with_mock_sdk()
        client._fyers.optionchain.return_value = {"s": "error", "message": "bad symbol"}
        out = await client.get_option_chain("NSE:BADSYMBOL")
        assert out == {}

    @pytest.mark.asyncio
    async def test_get_option_chain_raises_before_token_set(self):
        client = FyersClient(db_path=":memory:")
        with pytest.raises(RuntimeError):
            await client.get_option_chain("NSE:NIFTY50-INDEX")


class TestFyersClientPlaceOrder:
    @pytest.mark.asyncio
    async def test_intent_is_required_keyword_only(self):
        client = _client_with_mock_sdk()
        with pytest.raises(TypeError):
            await client.place_order(symbol="NSE:SBIN-EQ", quantity=1)

    @pytest.mark.asyncio
    async def test_intent_must_be_entry_or_exit(self):
        client = _client_with_mock_sdk()
        with pytest.raises(ValueError):
            await client.place_order(symbol="NSE:SBIN-EQ", quantity=1, intent="sideways")

    @pytest.mark.asyncio
    async def test_missing_symbol_or_nonpositive_qty_errors_without_calling_broker(self):
        client = _client_with_mock_sdk()
        out = await client.place_order(symbol="", quantity=1, intent="entry")
        assert out["status"] == "ERROR"
        out2 = await client.place_order(symbol="NSE:SBIN-EQ", quantity=0, intent="entry")
        assert out2["status"] == "ERROR"
        client._fyers.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_entry_blocked_by_global_halt(self):
        client = _client_with_mock_sdk()
        with patch("fyers_client.is_owner_entry_halted") as mock_owner, \
             patch("fyers_client.assert_not_halted") as mock_assert:
            mock_owner.return_value = EntryHaltVerdict(
                channel="TV_FYERS", allowed=True, global_halt=False,
                per_channel=False, reason="",
            )
            mock_assert.side_effect = TradingHalted(
                None, {"scope": "global", "by": "operator", "reason": "test halt"}
            )
            out = await client.place_order(symbol="NSE:SBIN-EQ", quantity=1, intent="entry")
        assert out["status"] == "ERROR"
        assert out["halted"] is True
        client._fyers.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_entry_blocked_by_owner_entry_halt(self):
        client = _client_with_mock_sdk()
        with patch("fyers_client.is_owner_entry_halted") as mock_owner:
            mock_owner.return_value = EntryHaltVerdict(
                channel="TV_FYERS", allowed=False, global_halt=False,
                per_channel=True, reason="owner off",
            )
            out = await client.place_order(symbol="NSE:SBIN-EQ", quantity=1, intent="entry")
        assert out["status"] == "ERROR"
        assert out["owner_entry_halted"] is True
        client._fyers.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_exit_never_blocked_by_halt(self):
        client = _client_with_mock_sdk()
        client._fyers.place_order.return_value = {"s": "ok", "id": "FY-1"}
        with patch("fyers_client.is_owner_entry_halted") as mock_owner, \
             patch("fyers_client.assert_not_halted") as mock_assert:
            mock_assert.side_effect = TradingHalted(
                None, {"scope": "global", "by": "operator", "reason": "test halt"}
            )
            out = await client.place_order(symbol="NSE:SBIN-EQ", quantity=1, intent="exit")
        assert out["status"] == "PLACED"
        mock_owner.assert_not_called()
        mock_assert.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_entry_places_order_and_maps_side_type(self):
        client = _client_with_mock_sdk()
        client._fyers.place_order.return_value = {"s": "ok", "id": "FY-42"}
        with patch("fyers_client.is_owner_entry_halted") as mock_owner, \
             patch("fyers_client.assert_not_halted"):
            mock_owner.return_value = EntryHaltVerdict(
                channel="TV_FYERS", allowed=True, global_halt=False,
                per_channel=False, reason="",
            )
            out = await client.place_order(
                symbol="NSE:NIFTY24SEP24500CE", quantity=50,
                transaction_type="BUY", order_type="MARKET", intent="entry",
            )
        assert out == {"order_id": "FY-42", "status": "PLACED", "message": "order placed"}
        sent_payload = client._fyers.place_order.call_args[0][0]
        assert sent_payload["side"] == 1       # BUY
        assert sent_payload["type"] == 2       # MARKET

    @pytest.mark.asyncio
    async def test_unsupported_transaction_or_order_type_rejected(self):
        client = _client_with_mock_sdk()
        out = await client.place_order(
            symbol="NSE:SBIN-EQ", quantity=1, transaction_type="HOLD", intent="entry",
        )
        assert out["status"] == "ERROR"
        client._fyers.place_order.assert_not_called()


class TestFyersClientCancelAndHistory:
    @pytest.mark.asyncio
    async def test_cancel_order_requires_order_id(self):
        client = _client_with_mock_sdk()
        out = await client.cancel_order("")
        assert out["status"] == "ERROR"
        client._fyers.cancel_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancel_order_success(self):
        client = _client_with_mock_sdk()
        client._fyers.cancel_order.return_value = {"s": "ok"}
        out = await client.cancel_order("FY-1")
        assert out == {"order_id": "FY-1", "status": "CANCELLED"}

    @pytest.mark.asyncio
    async def test_order_history_filters_to_requested_id(self):
        client = _client_with_mock_sdk()
        client._fyers.orderbook.return_value = {
            "s": "ok",
            "orderBook": [{"id": "FY-1", "status": 2}, {"id": "FY-2", "status": 6}],
        }
        out = await client.order_history("FY-2")
        assert out == [{"id": "FY-2", "status": 6}]

    @pytest.mark.asyncio
    async def test_order_history_not_found_returns_empty(self):
        client = _client_with_mock_sdk()
        client._fyers.orderbook.return_value = {"s": "ok", "orderBook": []}
        out = await client.order_history("FY-missing")
        assert out == []
