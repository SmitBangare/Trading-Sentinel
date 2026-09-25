/**
 * [SMIT-FYERS-OPTIONS 2026-09-25] IN-MEMORY TOKEN STORE for the Fyers
 * access token. Separate instance from services/token-store.js (Zerodha)
 * -- different broker, different pipeline, no shared state. Same
 * constraint: never written to disk, logs, or localStorage.
 */
let currentAccessToken = null;
let isTokenExpired = true;
let tokenGeneratedAt = null;

module.exports = {
  setToken: (token) => {
    currentAccessToken = token;
    isTokenExpired = false;
    tokenGeneratedAt = new Date().toISOString();
  },

  getToken: () => currentAccessToken,

  isValid: () => !isTokenExpired && currentAccessToken !== null,

  markExpired: () => {
    isTokenExpired = true;
  },

  clearToken: () => {
    currentAccessToken = null;
    isTokenExpired = true;
    tokenGeneratedAt = null;
  },

  getStatus: () => ({
    status: isTokenExpired ? 'expired' : (currentAccessToken ? 'active' : 'none'),
    generatedAt: tokenGeneratedAt
  })
};
