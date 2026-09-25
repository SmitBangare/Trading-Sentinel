import useSWR from 'swr';
import { fetcher } from '../api/client';
import { evidenceFixtures, evidenceModeEnabled } from '../evidenceMode';

// [SMIT-FYERS-OPTIONS 2026-09-25] Is the Fyers client actually armed on
// the engine right now, and how many paper positions are open. This is
// the fastest "is the bot alive" signal for the TV->Fyers panel.
export function useTvFyersStatus() {
  const { data, error, isLoading, mutate } = useSWR(evidenceModeEnabled ? null : '/api/proxy/tv-fyers/status', fetcher, {
    refreshInterval: 20000,
    dedupingInterval: 5000,
    errorRetryCount: 3
  });

  return {
    status: evidenceModeEnabled ? evidenceFixtures.tvFyersStatus : (data || null),
    isLoading: evidenceModeEnabled ? false : isLoading,
    isError: evidenceModeEnabled ? null : error,
    mutate
  };
}
