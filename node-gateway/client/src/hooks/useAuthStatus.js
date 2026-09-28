import useSWR from 'swr';
import { fetcher } from '../api/client';
import { evidenceModeEnabled } from '../evidenceMode';

// [SMIT-FYERS-OPTIONS 2026-09-25] Is this browser session logged into the
// dashboard at all -- independent of whether a Zerodha broker token is
// armed. App.jsx used to (wrongly) treat "Zerodha token active" as "you're
// allowed to see the dashboard"; this hook reads the actual session flag
// instead, which /api/auth/password-login (or Zerodha OAuth) can both set.
export function useAuthStatus() {
  const { data, error, isLoading, mutate } = useSWR(evidenceModeEnabled ? null : '/api/auth/status', fetcher, {
    refreshInterval: 60000,
    dedupingInterval: 5000,
    errorRetryCount: 3
  });

  return {
    authenticated: evidenceModeEnabled ? true : (data?.authenticated || false),
    isLoading: evidenceModeEnabled ? false : isLoading,
    isError: evidenceModeEnabled ? null : error,
    mutate
  };
}
