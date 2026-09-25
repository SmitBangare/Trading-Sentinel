import useSWR from 'swr';
import { fetcher } from '../api/client';
import { evidenceFixtures, evidenceModeEnabled } from '../evidenceMode';

// [SMIT-FYERS-OPTIONS 2026-09-25] Paper position history (open + closed)
// for the TV->Fyers dashboard panel. Fetches ALL statuses once; the page
// splits them into "open" and "closed" client-side.
export function useTvFyersPositions() {
  const { data, error, isLoading, mutate } = useSWR(evidenceModeEnabled ? null : '/api/proxy/tv-fyers/positions?status=ALL&limit=100', fetcher, {
    refreshInterval: 20000,
    dedupingInterval: 5000,
    errorRetryCount: 3
  });

  const source = evidenceModeEnabled ? evidenceFixtures.tvFyersPositions : data;
  return {
    positions: Array.isArray(source?.positions) ? source.positions : [],
    isLoading: evidenceModeEnabled ? false : isLoading,
    isError: evidenceModeEnabled ? null : error,
    mutate
  };
}
