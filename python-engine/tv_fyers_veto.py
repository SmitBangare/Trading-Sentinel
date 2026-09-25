"""
[SMIT-FYERS-OPTIONS 2026-09-25] The "double-check" layer: independently
verifies an incoming TradingView entry signal against RSI, IV, and option
liquidity BEFORE the bot ever executes it. TradingView says "buy" -- these
functions decide whether the bot actually trusts that, per the user's
explicit requirement that the bot must not blindly execute an external
alert.

Small, pure, independently-testable functions -- composed into the full
gate ladder by tv_fyers_gates.py (mirrors fno_gates.py's Gate/witness
pattern; these functions are the checks that ladder wraps).
"""
from __future__ import annotations

from typing import Optional, Tuple

from config import settings


def verify_rsi(direction: str, rsi_value: Optional[float]) -> Tuple[bool, str]:
    """RSI is used as an EXHAUSTION filter, not a standalone entry signal:
    reject a CE (long) breakout that's already overbought, and a PE
    (short) breakout that's already oversold -- the cases where the move
    TradingView is flagging has likely already run.

    direction: "CE" (long bias) or "PE" (short bias), matching
    fno_models.OptionType values so no translation layer is needed
    upstream of this function.
    """
    if rsi_value is None:
        return False, "rsi_unavailable"
    if direction == "CE":
        if rsi_value < settings.TV_FYERS_RSI_LONG_MIN:
            return False, "rsi_below_long_min"
        if rsi_value > settings.TV_FYERS_RSI_LONG_MAX:
            return False, "rsi_overbought"
        return True, ""
    if direction == "PE":
        if rsi_value > settings.TV_FYERS_RSI_SHORT_MAX:
            return False, "rsi_above_short_max"
        if rsi_value < settings.TV_FYERS_RSI_SHORT_MIN:
            return False, "rsi_oversold"
        return True, ""
    return False, "rsi_unknown_direction"


def verify_iv(iv: Optional[float]) -> Tuple[bool, str]:
    """Sanity band on implied volatility. Reuses the same "IV sanity"
    concept fno_gates.py's iv_sanity gate applies to the Zerodha FNO
    module -- an IV outside a plausible band usually means a stale or
    bad quote, not a real market condition, and buying into it means
    overpaying for premium that theta/IV-crush will eat regardless of
    direction being right."""
    if iv is None:
        return False, "iv_unavailable"
    if not (settings.TV_FYERS_IV_SANITY_MIN <= iv <= settings.TV_FYERS_IV_SANITY_MAX):
        return False, "iv_out_of_band"
    return True, ""


def verify_liquidity(
    *, oi: int, volume: int, bid: float, ask: float, quote_age_sec: float,
) -> Tuple[bool, str]:
    """A great underlying signal on an illiquid strike with a wide
    bid-ask spread can lose more to slippage than the strategy's edge --
    this is checked independently of whatever TradingView's Pine Script
    logic did or didn't check on its own side."""
    if quote_age_sec > settings.TV_FYERS_MAX_QUOTE_AGE_SEC:
        return False, "stale_quote"
    if oi < settings.TV_FYERS_MIN_OI:
        return False, "oi_too_thin"
    if volume < settings.TV_FYERS_MIN_VOL:
        return False, "volume_too_thin"
    if bid <= 0 or ask <= 0:
        return False, "one_sided_market"
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return False, "invalid_mid_price"
    spread_pct = (ask - bid) / mid
    if spread_pct > settings.TV_FYERS_MAX_SPREAD_PCT:
        return False, "spread_too_wide"
    return True, ""
