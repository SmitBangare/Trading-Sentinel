import React from 'react';
import { Activity, ArrowLeft, Radio, ShieldCheck, ShieldOff, TrendingDown, TrendingUp } from 'lucide-react';
import StatusBar from '../components/StatusBar';
import { useTvFyersStatus } from '../hooks/useTvFyersStatus';
import { useTvFyersSignals } from '../hooks/useTvFyersSignals';
import { useTvFyersPositions } from '../hooks/useTvFyersPositions';

// [SMIT-FYERS-OPTIONS 2026-09-25] Human-readable explanations for every
// outcome the orchestrator can write to a signal's `handled_result`
// (tv_fyers_orchestrator.py + tv_fyers_veto.py + tv_fyers_gates.py are
// the source of truth for these exact strings). Unknown/future reasons
// fall back to a title-cased version of the raw code rather than being
// hidden -- an unmapped reason is still shown, just less prettily.
const OUTCOME_INFO = {
  executed: { label: 'Entry taken (paper)', tone: 'good', desc: 'Passed every check; a simulated trade was opened. No real money moved.' },
  unparseable_signal_time: { label: 'Bad timestamp', tone: 'bad', desc: 'The alert\'s signal_time field could not be parsed — check the Pine Script alert format.' },
  signal_stale: { label: 'Too old', tone: 'warn', desc: 'The alert arrived later than TV_FYERS_SIGNAL_MAX_AGE_SEC allows. Stale prices are refused on purpose.' },
  paper_disabled: { label: 'Paper trading disabled', tone: 'bad', desc: 'TV_FYERS_DISABLE_PAPER is set — the pipeline is fully switched off right now.' },
  symbol_resolution_unavailable: { label: 'No option chain data', tone: 'bad', desc: 'Fyers\' live option-chain lookup failed or returned nothing usable — check the Fyers connection.' },
  concurrency_cap: { label: 'Too many open positions', tone: 'warn', desc: 'TV_FYERS_MAX_CONCURRENT open paper positions already exist; this signal was skipped to cap exposure.' },
  atr_unavailable: { label: 'No volatility data', tone: 'bad', desc: 'Could not compute ATR from recent candles, so no stop/target could be derived.' },
  pool_below_min_viable: { label: 'Bankroll too small for 1 lot', tone: 'warn', desc: 'The paper bankroll cannot safely afford even one lot at current prices/risk limits.' },
  rsi_unavailable: { label: 'No RSI data', tone: 'bad', desc: 'RSI could not be computed from recent candles.' },
  rsi_below_long_min: { label: 'RSI too weak for a CALL', tone: 'warn', desc: 'Momentum (RSI) is below the minimum required to buy a call option.' },
  rsi_overbought: { label: 'RSI overbought', tone: 'warn', desc: 'RSI is above the safe ceiling — chasing here is considered too risky.' },
  rsi_above_short_max: { label: 'RSI too strong for a PUT', tone: 'warn', desc: 'Momentum (RSI) is above the maximum allowed to buy a put option.' },
  rsi_oversold: { label: 'RSI oversold', tone: 'warn', desc: 'RSI is below the safe floor — considered too risky to add to the move.' },
  iv_unavailable: { label: 'No IV data', tone: 'bad', desc: 'Implied volatility could not be computed from the option quote.' },
  iv_out_of_band: { label: 'IV out of sane range', tone: 'warn', desc: 'Implied volatility looks abnormal (too low or too high) — likely a bad/stale quote.' },
  stale_quote: { label: 'Quote too old', tone: 'warn', desc: 'The option quote is older than TV_FYERS_MAX_QUOTE_AGE_SEC allows.' },
  oi_too_thin: { label: 'Open interest too thin', tone: 'warn', desc: 'Not enough open interest on this contract to trust the liquidity.' },
  volume_too_thin: { label: 'Volume too thin', tone: 'warn', desc: 'Not enough traded volume on this contract to trust the liquidity.' },
  one_sided_market: { label: 'No two-sided market', tone: 'warn', desc: 'Bid or ask is missing/zero — there is no real market to trade into.' },
  invalid_mid_price: { label: 'Invalid price', tone: 'bad', desc: 'The computed mid-price was zero or negative — treated as bad data.' },
  spread_too_wide: { label: 'Bid/ask spread too wide', tone: 'warn', desc: 'The gap between bid and ask is wider than allowed — often normal right after market close/open.' },
};

function humanizeReason(reason) {
  if (!reason) return { label: 'Received, not processed yet', tone: 'warn', desc: 'Either still in flight, or the orchestrator crashed before recording an outcome — investigate if this persists.' };
  if (OUTCOME_INFO[reason]) return OUTCOME_INFO[reason];
  if (reason.startsWith('entry_')) return { label: `Broker rejected: ${reason.slice(6)}`, tone: 'bad', desc: 'The paper/live executor itself reported a non-success status.' };
  return { label: reason.replace(/_/g, ' '), tone: 'warn', desc: 'Rejected by an entry safety gate. Raw reason code shown as the label.' };
}

const toneClass = { good: 'text-emerald-400', warn: 'text-amber-400', bad: 'text-red-400' };
const toneBg = { good: 'bg-emerald-950 text-emerald-200 border-emerald-800', warn: 'bg-amber-950 text-amber-200 border-amber-800', bad: 'bg-red-950 text-red-200 border-red-800' };

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function fmtMoney(v) {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return '—';
  return `₹${Number(v).toFixed(2)}`;
}

function pnlClass(v) {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return 'text-gray-400';
  return Number(v) >= 0 ? 'text-emerald-400' : 'text-red-400';
}

function Note({ children }) {
  return <p className="mt-1 text-xs text-gray-500">{children}</p>;
}

function SectionCard({ title, note, children, accent = 'gray' }) {
  const border = accent === 'violet' ? 'border-violet-900/70 bg-violet-950/10' : 'border-gray-800 bg-gray-900';
  return (
    <section className={`rounded-xl border ${border} p-4`}>
      <h2 className="text-lg font-bold text-white">{title}</h2>
      {note && <Note>{note}</Note>}
      <div className="mt-4">{children}</div>
    </section>
  );
}

function StatusTile({ icon: Icon, label, value, good, note }) {
  return (
    <div className="rounded border border-gray-800 bg-gray-950/70 p-3">
      <div className="flex items-center gap-2">
        <Icon size={16} className={good ? 'text-emerald-400' : 'text-red-400'} />
        <span className="text-sm font-semibold text-gray-100">{label}</span>
      </div>
      <div className={`mt-1 text-sm font-bold ${good ? 'text-emerald-400' : 'text-red-400'}`}>{value}</div>
      <Note>{note}</Note>
    </div>
  );
}

function SignalRow({ signal }) {
  const info = humanizeReason(signal.handled_result);
  return (
    <tr className="border-t border-gray-800">
      <td className="p-3 text-gray-400">{fmtTime(signal.received_at)}</td>
      <td className="p-3 font-semibold text-gray-100">{signal.symbol}</td>
      <td className="p-3">
        <span className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs font-bold ${signal.direction === 'CE' ? 'bg-emerald-950 text-emerald-300' : 'bg-red-950 text-red-300'}`}>
          {signal.direction === 'CE' ? <TrendingUp size={12} /> : <TrendingDown size={12} />}
          {signal.direction}
        </span>
      </td>
      <td className="p-3 text-gray-300">{fmtMoney(signal.underlying_price)}</td>
      <td className="p-3 text-gray-400">{signal.strategy || '—'}</td>
      <td className="p-3">
        <span className={`rounded px-2 py-0.5 text-xs font-semibold ${toneClass[info.tone]}`}>{info.label}</span>
        <div className="mt-0.5 max-w-xs text-[11px] text-gray-500">{info.desc}</div>
      </td>
    </tr>
  );
}

function OpenPositionRow({ p }) {
  return (
    <tr className="border-t border-gray-800">
      <td className="p-3 text-gray-400">{fmtTime(p.entry_time)}</td>
      <td className="p-3 font-semibold text-gray-100">{p.symbol}</td>
      <td className="p-3 text-gray-300">{p.qty}</td>
      <td className="p-3 text-gray-300">{fmtMoney(p.entry_premium)}</td>
      <td className="p-3 text-gray-300">{p.entry_underlying?.toFixed?.(1) ?? '—'}</td>
      <td className="p-3 text-gray-300">{p.stop_underlying?.toFixed?.(1) ?? '—'}</td>
      <td className="p-3 text-gray-300">{p.target_underlying?.toFixed?.(1) ?? '—'}</td>
      <td className="p-3">
        {p.trail_active
          ? <span className="rounded bg-cyan-950 px-1.5 py-0.5 text-[10px] font-bold text-cyan-300">TRAILING</span>
          : <span className="text-gray-500 text-xs">Fixed stop</span>}
      </td>
      <td className="p-3">
        <span className="rounded bg-violet-950 px-1.5 py-0.5 text-[10px] font-bold text-violet-300">{p.status}</span>
      </td>
    </tr>
  );
}

function ClosedPositionRow({ p }) {
  return (
    <tr className="border-t border-gray-800">
      <td className="p-3 text-gray-400">{fmtTime(p.exit_time)}</td>
      <td className="p-3 font-semibold text-gray-100">{p.symbol}</td>
      <td className="p-3 text-gray-300">{fmtMoney(p.entry_premium)}</td>
      <td className="p-3 text-gray-300">{fmtMoney(p.exit_premium)}</td>
      <td className="p-3 text-gray-400">{p.exit_reason || '—'}</td>
      <td className={`p-3 font-semibold ${pnlClass(p.pnl)}`}>{fmtMoney(p.pnl)}</td>
      <td className={`p-3 font-semibold ${pnlClass(p.r_multiple)}`}>{p.r_multiple !== null && p.r_multiple !== undefined ? `${Number(p.r_multiple).toFixed(2)}R` : '—'}</td>
    </tr>
  );
}

export default function TvFyers({ navigateToDashboard }) {
  const { status, isLoading: statusLoading } = useTvFyersStatus();
  const { signals, isLoading: signalsLoading } = useTvFyersSignals();
  const { positions, isLoading: positionsLoading } = useTvFyersPositions();

  const openPositions = positions.filter((p) => p.status === 'OPEN' || p.status === 'CLOSING');
  const closedPositions = positions.filter((p) => p.status === 'CLOSED');

  return (
    <div className="min-h-screen flex flex-col bg-gray-950 text-gray-200">
      <StatusBar />
      <div className="p-4 max-w-7xl mx-auto w-full space-y-6">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-gray-800 pb-4">
          <div className="flex items-center gap-4">
            <button onClick={navigateToDashboard} className="flex items-center gap-1 text-gray-400 hover:text-white transition">
              <ArrowLeft size={16} /> Back to Dashboard
            </button>
            <h1 className="text-2xl font-bold text-white">TradingView &rarr; Fyers</h1>
          </div>
          <span className="rounded-full border border-violet-500/50 bg-violet-950 px-2.5 py-1 text-[10px] font-bold tracking-widest text-violet-200">
            PAPER - SIMULATION
          </span>
        </div>

        <p className="text-sm text-gray-400 max-w-3xl">
          This is a second, independent options-trading pipeline. TradingView alerts pick a direction (call
          or put); this bot double-checks that idea itself (RSI, implied volatility, liquidity) before
          simulating an entry through Fyers, and owns every exit (stop-loss, trailing, target) on its own.
          It is currently <span className="font-semibold text-violet-300">paper-only</span> — every trade
          below is simulated, no real order has ever been placed by this pipeline.
        </p>

        <SectionCard title="Is it alive right now?" note="Three quick checks. If the Fyers connection is down, no new trades can be evaluated at all — everything else on this page keeps working off historical data.">
          <div className="grid gap-3 sm:grid-cols-3">
            <StatusTile
              icon={status?.fyers_token_armed ? ShieldCheck : ShieldOff}
              label="Fyers connection"
              value={statusLoading ? 'Checking…' : (status?.fyers_token_armed ? 'Connected' : 'Not connected')}
              good={!!status?.fyers_token_armed}
              note="Log in daily at /api/tv-fyers/auth/login — the token is kept in memory only and is lost every time the engine restarts."
            />
            <StatusTile
              icon={Activity}
              label="Trading mode"
              value={status?.live_trading_enabled ? 'LIVE (real money)' : 'Paper only'}
              good={!status?.live_trading_enabled}
              note="Should always read Paper only unless you have explicitly and deliberately armed live trading."
            />
            <StatusTile
              icon={Radio}
              label="Open paper positions"
              value={statusLoading ? '…' : (status?.open_paper_positions ?? 0)}
              good
              note="How many simulated trades are currently open and being managed by the 90-second exit check."
            />
          </div>
        </SectionCard>

        <SectionCard
          title="Recent signals"
          note="Every alert TradingView sends, newest first, and what the bot decided to do with it. A rejection is not a bug — it means a safety gate correctly said no. Hover-worthy: if a row sits at 'Received, not processed yet' for more than a few seconds, something crashed before finishing — worth investigating."
        >
          {signalsLoading ? (
            <div className="p-6 text-sm text-gray-500">Loading signals…</div>
          ) : !signals.length ? (
            <div className="rounded border border-gray-800 bg-gray-950/70 p-4 text-sm italic text-gray-500">No signals received yet.</div>
          ) : (
            <div className="overflow-x-auto rounded border border-gray-800">
              <table className="w-full text-left text-sm whitespace-nowrap">
                <thead className="bg-gray-800 text-gray-400">
                  <tr>
                    {['Received', 'Symbol', 'Direction', 'Underlying', 'Strategy', 'Outcome'].map((h) => (
                      <th key={h} className="p-3 font-medium">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>{signals.map((s) => <SignalRow key={s.signal_id} signal={s} />)}</tbody>
              </table>
            </div>
          )}
        </SectionCard>

        <SectionCard
          title="Open positions"
          note="Simulated trades still running. 'Stop'/'Target' are underlying-index levels the bot watches, not the option premium itself. Once price reaches the target, the stop starts trailing instead of staying fixed."
        >
          {positionsLoading ? (
            <div className="p-6 text-sm text-gray-500">Loading positions…</div>
          ) : !openPositions.length ? (
            <div className="rounded border border-gray-800 bg-gray-950/70 p-4 text-sm italic text-gray-500">No open positions right now.</div>
          ) : (
            <div className="overflow-x-auto rounded border border-gray-800">
              <table className="w-full text-left text-sm whitespace-nowrap">
                <thead className="bg-gray-800 text-gray-400">
                  <tr>
                    {['Entry time', 'Symbol', 'Qty', 'Entry premium', 'Entry spot', 'Stop (spot)', 'Target (spot)', 'Trail', 'Status'].map((h) => (
                      <th key={h} className="p-3 font-medium">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>{openPositions.map((p) => <OpenPositionRow key={p.id} p={p} />)}</tbody>
              </table>
            </div>
          )}
        </SectionCard>

        <SectionCard
          title="Trade history (closed)"
          note="Every simulated trade that has exited, with the reason (stop hit, target hit, trailing stop, time-cut, or forced flat before close) and its P&L. R-multiple is P&L divided by the amount risked — above 1R means the win was bigger than the planned risk."
        >
          {positionsLoading ? (
            <div className="p-6 text-sm text-gray-500">Loading history…</div>
          ) : !closedPositions.length ? (
            <div className="rounded border border-gray-800 bg-gray-950/70 p-4 text-sm italic text-gray-500">No closed trades yet.</div>
          ) : (
            <div className="overflow-x-auto rounded border border-gray-800">
              <table className="w-full text-left text-sm whitespace-nowrap">
                <thead className="bg-gray-800 text-gray-400">
                  <tr>
                    {['Exit time', 'Symbol', 'Entry premium', 'Exit premium', 'Exit reason', 'P&L', 'R-multiple'].map((h) => (
                      <th key={h} className="p-3 font-medium">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>{closedPositions.map((p) => <ClosedPositionRow key={p.id} p={p} />)}</tbody>
              </table>
            </div>
          )}
        </SectionCard>
      </div>
    </div>
  );
}
