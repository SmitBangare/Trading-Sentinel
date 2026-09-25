// Browser-evidence mode is deliberately unavailable in normal builds.  It is
// a local Dev rendering fixture for visual verification of the real Dashboard
// components, never a replacement for authenticated service evidence.
export const evidenceModeEnabled = import.meta.env.DEV && import.meta.env.VITE_EVIDENCE_DEMO === 'true';

export const evidenceFixtures = {
  health: { token_status: 'active', market_open: false, circuit_breaker_halted: false },
  proactiveActivity: {
    modes: {
      SHADOW: { unique_opportunities: 4, scan_evaluations: 5, stages: { SETUP: 4, DEFERRED: 1, FILLED: 1, CLOSED: 1 } },
      LIVE: { unique_opportunities: 0, scan_evaluations: 0, stages: {} },
      PAPER: { unique_opportunities: 0, scan_evaluations: 0, stages: {} },
      REPLAY: { unique_opportunities: 0, scan_evaluations: 0, stages: {} },
    },
    shadow_positions: [{
      account_id: 'demo-synthetic-account', run_id: 'demo-multisession-v1', open_positions: 1,
      closed_positions: 1, scenario_capital: 150, free_cash: 42.5, reserved_capital: 104,
      gross_pnl: 8, fees: 0.62, net_pnl: 7.38, marked_unrealized_pnl: 1.5,
      unrealized_state: 'MARKED_COMPLETED_BAR',
    }],
    note: 'DEV EVIDENCE FIXTURE — synthetic scenario only; not broker-reconciled profit.',
  },
  partnerCards: {
    mode: 'SHADOW',
    cards: [{
      evaluation_id: 'demo-superseded-card', phase: 'phase2', kind: 'covered_call_recommendation',
      underlying: 'NIFTY', contracts: ['NIFTY-DEMO-CE'], rendered_text: 'Synthetic hedge review: NIFTY coverage.',
      evaluated_at: '2026-09-07T09:30:00+05:30', valid_until: null, portfolio_revision: 1,
      current_portfolio_revision: 3, portfolio_state: 'SUPERSEDED', is_superseded: true,
      delivery_state: 'NOT_SENT_SHADOW_EVIDENCE', can_send: false, can_trade: false,
    }],
    note: 'DEV EVIDENCE FIXTURE — persisted-card layout only; no delivery is available.',
  },
  optionalAi: {
    mode: 'OPTIONAL_ANNOTATION', state: 'OUTAGE_CIRCUIT_OPEN', stale: false,
    reported_at: '2026-09-07T09:32:00+05:30', execution_authority: 'NONE', can_place_orders: false,
    detail: { queue: { pending: 0, cached: 0, daily_requests: 3, daily_budget: 40, max_pending: 16, circuit_state: 'OPEN' } },
    note: 'DEV EVIDENCE FIXTURE — provider outage leaves deterministic processing independent.',
  },
  tvFyersStatus: {
    fyers_token_armed: true, open_paper_positions: 1, live_trading_enabled: false,
  },
  tvFyersSignals: {
    signals: [
      { signal_id: 'demo-1', symbol: 'NIFTY', direction: 'CE', underlying_price: 24512.3, strategy: 'orb_momentum_v1', signal_time: '2026-09-25T04:16:00Z', received_at: '2026-09-25T04:16:00Z', handled: 1, handled_result: 'executed' },
      { signal_id: 'demo-2', symbol: 'NIFTY', direction: 'PE', underlying_price: 24488.1, strategy: 'orb_momentum_v1', signal_time: '2026-09-25T05:02:00Z', received_at: '2026-09-25T05:02:00Z', handled: 1, handled_result: 'spread_too_wide' },
      { signal_id: 'demo-3', symbol: 'NIFTY', direction: 'CE', underlying_price: 24530.0, strategy: 'orb_momentum_v1', signal_time: '2026-09-25T06:47:00Z', received_at: '2026-09-25T06:47:00Z', handled: 1, handled_result: 'rsi_overbought' },
    ],
  },
  tvFyersPositions: {
    positions: [
      { id: 2, source: 'TV_FYERS_PAPER', signal_id: 'demo-1', symbol: 'NSE:NIFTY26SEP24500CE', direction: 'CE', qty: 65, entry_time: '2026-09-25T04:16:05Z', entry_premium: 142.5, entry_underlying: 24512.3, stop_underlying: 24462.3, target_underlying: 24602.3, premium_stop: 106.9, trail_active: 1, trail_stop_underlying: 24540.0, best_underlying: 24575.0, atr_at_entry: 50.0, status: 'OPEN', entry_order_id: 'PAPER-2' },
      { id: 1, source: 'TV_FYERS_PAPER', signal_id: 'demo-0', symbol: 'NSE:NIFTY26SEP24400PE', direction: 'PE', qty: 65, entry_time: '2026-09-25T03:20:00Z', entry_premium: 98.0, entry_underlying: 24450.0, stop_underlying: 24500.0, target_underlying: 24350.0, premium_stop: 73.5, trail_active: 0, trail_stop_underlying: null, best_underlying: 24450.0, atr_at_entry: 45.0, status: 'CLOSED', entry_order_id: 'PAPER-1', exit_time: '2026-09-25T03:55:00Z', exit_premium: 121.0, exit_underlying: 24398.0, exit_reason: 'target_hit', gross_pnl: 1495.0, costs: 40.0, pnl: 1455.0, r_multiple: 1.8, exit_order_id: 'PAPER-1-X' },
    ],
  },
  sessionDiagnostics: {
    mode: 'OBSERVATION_ONLY', session_count: 5, can_place_orders: false, authorization_effect: 'NONE', findings: [],
    reports: [{
      scope: { policy_id: 'trend_pullback_v1', account_id: 'demo-sparse-activity', mode: 'SHADOW' },
      eligible_sessions: ['2026-09-01', '2026-09-02', '2026-09-03', '2026-09-04', '2026-09-07'],
      scan_health: { expected_sessions: 5, successful_sessions: 5, unavailable_sessions: 0, missing_sessions: 0 },
      activity: { viable_events: 1, fills: 1 },
      findings: [{ code: 'TWO_ELIGIBLE_SESSIONS_NO_VIABLE_CANDIDATES' }, { code: 'FIVE_ELIGIBLE_SESSIONS_SPARSE_FILLS' }],
    }],
  },
};
