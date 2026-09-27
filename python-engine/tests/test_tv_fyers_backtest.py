"""
Tests for tv_fyers_backtest.py. No network, no AI -- see that module's
docstring for what's real data (candles, RSI, ATR) vs. modelled (option
pricing, liquidity).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytz

import tv_fyers_backtest as bt

IST = pytz.timezone("Asia/Kolkata")


def _ts(y, m, d, hh, mm) -> int:
    return int(IST.localize(datetime(y, m, d, hh, mm)).astimezone(timezone.utc).timestamp())


def _flat_day_candles(trading_day: date, base_price: float, n_noise=0.0, seed=0) -> list:
    """One full session (09:15-15:25, 5-min bars) of gently oscillating
    candles around base_price -- used purely as ATR/EMA warmup filler,
    never expected to produce a signal itself."""
    import random
    rng = random.Random(seed)
    rows = []
    minute = 9 * 60 + 15
    price = base_price
    while minute <= 15 * 60 + 25:
        hh, mm = divmod(minute, 60)
        drift = rng.uniform(-n_noise, n_noise)
        price = price + drift
        o, h, l, c = price, price + 2, price - 2, price + drift
        rows.append([_ts(trading_day.year, trading_day.month, trading_day.day, hh, mm), o, h, l, c, 1000])
        price = c
        minute += 5
    return rows


def _breakout_day_candles(trading_day: date, or_price: float) -> list:
    """Opening-range bars flat around or_price, then a clean upward
    breakout well past OR-high + 0.25*ATR, sustained (fresh cross only
    once)."""
    rows = []
    # Opening range: 09:15, 09:20, 09:25 (15-min window -> 3 bars)
    for mm in (15, 20, 25):
        rows.append([_ts(trading_day.year, trading_day.month, trading_day.day, 9, mm),
                     or_price, or_price + 3, or_price - 3, or_price, 1000])
    # Post-OR: hold flat for a couple bars, then break out hard and stay up.
    rows.append([_ts(trading_day.year, trading_day.month, trading_day.day, 9, 30),
                 or_price, or_price + 3, or_price - 3, or_price, 1000])
    rows.append([_ts(trading_day.year, trading_day.month, trading_day.day, 9, 35),
                 or_price, or_price + 3, or_price - 3, or_price, 1000])
    breakout_price = or_price + 200.0  # comfortably past any ATR-scaled buffer
    minute = 9 * 60 + 40
    price = breakout_price
    while minute <= 15 * 60 + 25:
        hh, mm = divmod(minute, 60)
        rows.append([_ts(trading_day.year, trading_day.month, trading_day.day, hh, mm),
                     price, price + 3, price - 3, price, 1000])
        minute += 5
    return rows


def _warmup_plus_breakout_candles():
    """~10 warmup days (ATR(14)/EMA(50) need real history to be non-NaN)
    followed by one clean CE breakout day."""
    candles = []
    day0 = date(2026, 1, 5)  # a Monday
    trading_days = []
    d = day0
    while len(trading_days) < 10:
        if d.weekday() < 5:
            trading_days.append(d)
        d += timedelta(days=1)
    for i, td in enumerate(trading_days):
        candles.extend(_flat_day_candles(td, base_price=24000.0, n_noise=5.0, seed=i))
    breakout_day = trading_days[-1] + timedelta(days=1)
    while breakout_day.weekday() >= 5:
        breakout_day += timedelta(days=1)
    candles.extend(_breakout_day_candles(breakout_day, or_price=24000.0))
    return candles, breakout_day


class TestGenerateOrbSignals:
    def test_clean_breakout_produces_a_ce_signal(self):
        candles, breakout_day = _warmup_plus_breakout_candles()
        df = bt._candles_to_frame(candles)
        signals = bt.generate_orb_signals(df)
        ce_signals = [s for s in signals if s.dt_ist.date() == breakout_day and s.direction == "CE"]
        assert len(ce_signals) >= 1, "expected at least one CE breakout signal on the engineered day"
        first = ce_signals[0]
        assert first.atr is not None and first.atr > 0
        assert first.underlying_price > 24000.0

    def test_flat_market_produces_no_signals(self):
        candles = []
        day0 = date(2026, 1, 5)
        trading_days = []
        d = day0
        while len(trading_days) < 12:
            if d.weekday() < 5:
                trading_days.append(d)
            d += timedelta(days=1)
        for i, td in enumerate(trading_days):
            candles.extend(_flat_day_candles(td, base_price=24000.0, n_noise=0.0, seed=i))
        df = bt._candles_to_frame(candles)
        signals = bt.generate_orb_signals(df)
        assert signals == []


class TestExpiry:
    def test_thursday_signal_uses_same_day_expiry(self):
        thursday = date(2026, 1, 8)  # a Thursday
        assert bt._next_weekly_expiry(thursday) == thursday

    def test_monday_signal_rolls_to_that_weeks_thursday(self):
        monday = date(2026, 1, 5)
        assert bt._next_weekly_expiry(monday) == date(2026, 1, 8)

    def test_friday_signal_rolls_to_next_weeks_thursday(self):
        friday = date(2026, 1, 9)
        assert bt._next_weekly_expiry(friday) == date(2026, 1, 15)


class TestRunBacktestSmoke:
    def test_full_replay_does_not_crash_and_produces_a_coherent_result(self):
        candles, _ = _warmup_plus_breakout_candles()
        result = bt.run_backtest(candles, starting_bankroll=250_000.0)
        assert result.signals_generated >= 1
        assert result.entries_taken >= 0
        assert result.entries_taken == len(result.trades) or result.entries_taken > len(result.trades)
        # Every recorded trade is fully resolved (this window force-closes
        # anything still open at the end).
        for trade in result.trades:
            assert trade.exit_reason is not None
            assert trade.pnl is not None

    def test_no_candles_produces_empty_result(self):
        result = bt.run_backtest([])
        assert result.signals_generated == 0
        assert result.entries_taken == 0
        assert result.trades == []


class TestFetchNiftyHistory:
    @pytest.mark.asyncio
    async def test_chunks_requests_for_a_long_window_and_dedupes(self):
        fyers = MagicMock()
        calls = []

        async def _history(symbol, resolution, from_date, to_date):
            calls.append((from_date, to_date))
            # One overlapping-ish candle per chunk plus a unique one, to
            # prove de-dup by timestamp works.
            return [[1_700_000_000, 100, 101, 99, 100, 10], [1_700_000_000 + len(calls), 100, 101, 99, 100, 10]]

        fyers.get_historical = AsyncMock(side_effect=_history)
        candles = await bt.fetch_nifty_history(fyers, days_back=200)
        assert len(calls) >= 3  # 200 days / 90-day chunks -> at least 3 requests
        # No duplicate timestamps in the final series.
        ts_values = [c[0] for c in candles]
        assert len(ts_values) == len(set(ts_values))
