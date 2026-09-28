import { useState } from 'react';
import { postClient } from '../api/client';

// [SMIT-FYERS-OPTIONS-BACKTEST 2026-09-28] No SWR here -- this is a
// deliberate, user-triggered action (not a polled read), and a fresh
// (uncached) run is a real, possibly-slow Fyers fetch. See
// tv_fyers_backtest.py's module docstring for what's real data vs.
// modelled in the result.
export function useTvFyersBacktest() {
  const [result, setResult] = useState(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);

  const run = async ({ daysBack = 365, refreshCache = false } = {}) => {
    setRunning(true);
    setError(null);
    try {
      const query = `?days_back=${daysBack}${refreshCache ? '&refresh_cache=true' : ''}`;
      const data = await postClient(`/api/proxy/tv-fyers/backtest/run${query}`);
      setResult(data);
      return data;
    } catch (err) {
      setError(err.message || 'Backtest failed');
      throw err;
    } finally {
      setRunning(false);
    }
  };

  return { result, running, error, run };
}
