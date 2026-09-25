// [SMIT-FYERS-OPTIONS 2026-09-25] Fyers OAuth login flow -- mirrors
// routes/auth.js's Zerodha login/callback/status shape. Separate route
// file/token store: a different broker with no shared state.
const express = require('express');
const router = express.Router();
const fyersAuth = require('../services/tv-fyers-auth');
const tokenStore = require('../services/tv-fyers-token-store');
const config = require('../config');
const { logger } = require('../middleware/logger');
const { requireSession } = require('../middleware/auth');
const { limiters } = require('../middleware/security');
const { withRetry } = require('../utils/retry');

router.get('/login', limiters.authLogin, (req, res) => {
  logger.info({ event_type: 'tv_fyers_auth_initiated' }, 'Fyers OAuth initiated');
  res.redirect(fyersAuth.getLoginURL());
});

router.get('/callback', limiters.authCallback, async (req, res, next) => {
  try {
    const { auth_code, s } = req.query;

    if (s === 'error' || !auth_code) {
      logger.warn({ event_type: 'tv_fyers_auth_failed', s }, 'Fyers login failed or denied');
      return res.redirect('/?error=fyers_failed');
    }

    const accessToken = await fyersAuth.generateSession(auth_code);
    tokenStore.setToken(accessToken);

    req.session.tv_fyers_authenticated = true;
    req.session.tv_fyers_login_time = new Date().toISOString();

    // Provision to python-engine (non-fatal if unreachable -- same
    // resilience posture as the Zerodha token provisioning call).
    try {
      await withRetry(async () => {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 2000);
        const resp = await fetch(`${config.PYTHON_ENGINE_URL}/internal/tv-fyers/token`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Internal-Secret': config.INTERNAL_API_SECRET,
          },
          body: JSON.stringify({ access_token: accessToken }),
          signal: controller.signal,
        });
        clearTimeout(timeout);
        if (!resp.ok) throw new Error(`Engine returned ${resp.status}`);
      }, 3, 1000);
    } catch (err) {
      logger.warn(
        { event_type: 'tv_fyers_engine_provision_failed', err: err.message },
        'Failed to provision Fyers token to python-engine',
      );
    }

    const telegram = require('../services/telegram');
    telegram.sendAlert('✅ Fyers authenticated successfully (TV->Fyers paper pipeline).');
    logger.info({ event_type: 'tv_fyers_auth_success' }, 'Fyers login complete');

    res.redirect('/');
  } catch (err) {
    next(err);
  }
});

router.get('/status', (req, res) => {
  const tokenInfo = tokenStore.getStatus();
  let ageMinutes = null;
  if (tokenInfo.generatedAt) {
    ageMinutes = Math.floor((Date.now() - new Date(tokenInfo.generatedAt).getTime()) / 60000);
  }
  res.json({
    authenticated: req.session?.tv_fyers_authenticated || false,
    login_time: req.session?.tv_fyers_login_time || null,
    token_age_min: ageMinutes,
  });
});

router.post('/logout', requireSession, async (req, res) => {
  tokenStore.clearToken();
  try {
    await fetch(`${config.PYTHON_ENGINE_URL}/internal/tv-fyers/token/invalidate`, {
      method: 'POST',
      headers: { 'X-Internal-Secret': config.INTERNAL_API_SECRET },
    });
  } catch (err) {
    logger.warn({ event_type: 'tv_fyers_engine_invalidate_failed' }, 'Could not notify python-engine of Fyers logout');
  }
  req.session.tv_fyers_authenticated = false;
  res.json({ success: true, message: 'Fyers logged out successfully' });
});

module.exports = router;
