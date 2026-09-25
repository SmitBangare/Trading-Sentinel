/**
 * [SMIT-FYERS-OPTIONS 2026-09-25] Fyers OAuth login flow. Mirrors
 * services/kite.js's getLoginURL/generateSession shape, but implemented
 * as raw HTTPS calls (not the official SDK, which is Python-only --
 * python-engine's fyers_client.py owns that side) since the auth-code
 * exchange is just a documented REST call, no SDK needed.
 *
 * URL format and the `auth_code` callback parameter name confirmed
 * against Fyers' own documentation/community examples (myapi.fyers.in)
 * before writing this -- not guessed.
 */
const crypto = require('crypto');
const https = require('https');
const config = require('../config');
const { logger } = require('../middleware/logger');

const FYERS_AUTH_BASE = 'api-t1.fyers.in';

function getLoginURL() {
  const params = new URLSearchParams({
    client_id: config.FYERS_CLIENT_ID,
    redirect_uri: config.FYERS_REDIRECT_URL,
    response_type: 'code',
    state: 'tv_fyers_login',
  });
  return `https://${FYERS_AUTH_BASE}/api/v3/generate-authcode?${params.toString()}`;
}

function _post(path, body) {
  return new Promise((resolve, reject) => {
    const payload = JSON.stringify(body);
    const req = https.request(
      {
        hostname: FYERS_AUTH_BASE,
        path,
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': Buffer.byteLength(payload),
        },
        timeout: 10000,
      },
      (res) => {
        let data = '';
        res.on('data', (chunk) => { data += chunk; });
        res.on('end', () => {
          try {
            resolve({ statusCode: res.statusCode, body: JSON.parse(data) });
          } catch (err) {
            reject(new Error(`Fyers auth response was not valid JSON: ${data.slice(0, 200)}`));
          }
        });
      },
    );
    req.on('error', reject);
    req.on('timeout', () => req.destroy(new Error('Fyers auth request timed out')));
    req.write(payload);
    req.end();
  });
}

/**
 * Exchange the auth_code (from the callback) for an access_token.
 * appIdHash is SHA-256(client_id:secret_key) per Fyers' documented
 * validate-authcode contract.
 */
async function generateSession(authCode) {
  const appIdHash = crypto
    .createHash('sha256')
    .update(`${config.FYERS_CLIENT_ID}:${config.FYERS_SECRET_KEY}`)
    .digest('hex');

  const { statusCode, body } = await _post('/api/v3/validate-authcode', {
    grant_type: 'authorization_code',
    appIdHash,
    code: authCode,
  });

  if (statusCode !== 200 || body.s !== 'ok' || !body.access_token) {
    logger.error({ event_type: 'fyers_token_exchange_failed', statusCode, body });
    throw new Error(`Fyers token exchange failed: ${body.message || `HTTP ${statusCode}`}`);
  }
  return body.access_token;
}

module.exports = { getLoginURL, generateSession };
