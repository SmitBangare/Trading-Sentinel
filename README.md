# Trading Sentinel

A personal algorithmic-trading system: a Python engine that runs strategy logic, risk gates and order execution, and a Node.js gateway that sits in front of it for the dashboard, broker OAuth, and webhooks.

**Nothing in this repo places real orders unless a module's own live-trading flag is explicitly turned on. Every module below defaults to paper (simulated) trading.**

---

## What's in here

| Piece | What it does |
|---|---|
| `python-engine/` | FastAPI service — strategy engines, risk gates, position tracking, backtesting, scheduler jobs |
| `node-gateway/` | Express server + React dashboard — login, broker OAuth, webhooks, Telegram alerts, proxies the dashboard to the engine |
| `pine-scripts/` | TradingView Pine Script alert templates |
| `docs/` | System guide, architecture notes, ops runbooks |

The engine hosts several independent trading modules (Zerodha-based NIFTY swing/momentum, penny-stock scanning, F&O options). This branch's active focus is the newest one:

## TradingView → Fyers options pipeline

A second, independent options-trading pipeline, separate from the Zerodha-based modules above — **Fyers only, paper-only, triple-disarmed by default.**

**How it decides to trade** (two ways, both active):
- **TradingView alert** — a Pine Script webhook proposes a direction (call/put); the bot independently double-checks it before doing anything.
- **Autonomous candle scanner** — runs every 5 minutes on its own, reading NIFTY candles straight from Fyers, no TradingView alert needed.

**The double-check, by default, costs nothing:** RSI, implied-volatility sanity, and liquidity checks — all deterministic math, no external API calls. An optional AI layer (Claude reading the actual candles) can be turned on with an Anthropic API key, but that's real, paid, opt-in — the system is fully functional and free without it.

**Every exit is bot-owned:** structural stop → trailing stop once a target is hit → premium-loss backstop → time-based cut → forced flat before close. TradingView (or the scanner) only ever signals *entries*.

**Real-time alerts:** every paper entry and exit pings Telegram.

**Backtesting:** a mechanics-only replay engine — real Fyers candle history, real Black-76 option pricing off realised volatility, the exact same gates/sizing/exit code the live bot runs. No AI cost to run. See `python-engine/tv_fyers_backtest.py` for exactly what's real data vs. modelled.

**Dashboard:** password-protected (no broker login required to view it), with a live panel showing connection status, every signal the bot has seen (including ones it decided *not* to trade), open positions, and closed-trade history — each section explained in plain English on the page itself.

Full setup instructions: **[`TV_FYERS_SETUP_MANUAL.txt`](TV_FYERS_SETUP_MANUAL.txt)**.

---

## Safety posture

- Every module starts paper-only. Going live is always a separate, explicit, deliberate config change — never a side effect of anything else.
- A backtest result is evidence to consider, not proof of a profitable strategy. Modelled option pricing and skipped liquidity checks mean real-world slippage is not captured.
- The AI layer (where enabled) can veto an entry but is never the only thing standing between a signal and an order — deterministic risk/liquidity checks always run too.

## General setup

See **[`SETUP_MANUAL.txt`](SETUP_MANUAL.txt)** for the base Zerodha-side stack, and `docs/` for architecture/ops detail.
