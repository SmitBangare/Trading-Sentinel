import useSWR from 'swr';
import { fetcher } from '../api/client';
import { evidenceFixtures, evidenceModeEnabled } from '../evidenceMode';

// [SMIT-FYERS-OPTIONS 2026-09-25] Newest-first TradingView webhook log
// for the TV->Fyers dashboard panel.
export function useTvFyersSignals() {
  const { data, error, isLoading, mutate } = useSWR(evidenceModeEnabled ? null : '/api/proxy/tv-fyers/signals', fetcher, {
    refreshInterval: 20000,
    dedupingInterval: 5000,
    errorRetryCount: 3
  });

  const source = evidenceModeEnabled ? evidenceFixtures.tvFyersSignals : data;
  return {
    signals: Array.isArray(source?.signals) ? source.signals : [],
    isLoading: evidenceModeEnabled ? false : isLoading,
    isError: evidenceModeEnabled ? null : error,
    mutate
  };
}
